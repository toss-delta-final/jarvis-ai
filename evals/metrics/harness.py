"""ScriptedLLM과 MockTransport로 실제 구매자 추천 코드 경로를 실행하는 adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import httpx

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.graph import stream_recommendation
from app.core.auth import Identity
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.services import search_service, spring_client
from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures
from evals.metrics.settings import EvaluationSettings

_INTERNAL_TOKEN = "eval-internal-token"


class EvalScriptedLLM:
    """케이스 정답 필터와 검색 순서를 그대로 내는 최소 결정론 LLM."""

    def __init__(self, case: GoldenCase, ranked_ids: list[int]) -> None:
        self.extracted_filters = dict(case.expected_filters)
        self._decompose = {
            "intent": case.expected_route,
            "reply": "",
            "case": 2,
            "semanticQuery": case.query,
            "categoryQueries": [],
            "filters": self.extracted_filters,
            "cart": {"productId": None, "optionId": None, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
        }
        self._rerank = {
            "ranked": [
                {"productId": product_id, "rationale": "검색 순서 유지"}
                for product_id in ranked_ids
            ],
            "overallComment": "고정 검색 순서 결과입니다.",
        }
        self.calls: list[str] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        del system, user, max_tokens, json_output
        self.calls.append(tier)
        payload = self._decompose if tier == "fast" else self._rerank
        return json.dumps(payload, ensure_ascii=False)

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        del system, user, tier, max_tokens
        yield "고정 응답"


def _filter_products_by_requested_price(
    products: list[dict[str, Any]], params: httpx.QueryParams
) -> list[dict[str, Any]]:
    """요청 쿼리의 minPrice/maxPrice로만 거른다(#370, 결정 01).

    실 Spring은 가격을 서버사이드로 거른다(`app/services/spring_client.py`가 `minPrice`/
    `maxPrice`를 I-1 쿼리에 싣는다) — 이 mock은 그 동작만 흉내낸다. 케이스의
    `hardConstraints`가 아니라 **앱이 실제로 보낸 요청 파라미터**를 기준으로 걸러야 decompose가
    가격 축을 놓치는 상황(아래 채널 비공허성 테스트)을 그대로 계측할 수 있다. 가격이 없는
    (`None`) 상품은 판정 불가로 보고 항상 통과시킨다 — `evals/scoring/hard_filter`의
    `priceUnknown` 컷(scoring arm 정책)과는 다른 계층이라 통일하지 않는다.

    `keyword`/`categoryName`/`brandName` 충실도는 이번에 올리지 않는다(범위 밖 — 올리면 이
    이슈와 무관한 기존 케이스 전부의 노출 집합이 흔들린다). `evals/goldenset/GUIDE.md` 위반
    네거티브 절에 이 한계를 명시했다.
    """
    min_price = params.get("minPrice")
    max_price = params.get("maxPrice")
    if min_price is None and max_price is None:
        return products
    min_price_int = int(min_price) if min_price is not None else None
    max_price_int = int(max_price) if max_price is not None else None
    filtered = []
    for product in products:
        price = product.get("price")
        if not isinstance(price, (int, float)):
            # None(가격 미상)과 마찬가지로 판정 불가 — 항상 통과(#370 리뷰 라운드2 F-5, 정책은
            # 위 docstring 참조). catalog_snapshot.json은 현재 int/None만 쓰지만, 이
            # mock은 검증 없는 raw dict를 읽으므로 문자열 등도 크래시 없이 같은 규칙을 탄다.
            filtered.append(product)
            continue
        if min_price_int is not None and price < min_price_int:
            continue
        if max_price_int is not None and price > max_price_int:
            continue
        filtered.append(product)
    return filtered


class _CaseTransport:
    """I-1/I-19/I-21만 제공하고 모든 요청을 감사용으로 기록한다."""

    def __init__(
        self,
        case: GoldenCase,
        fixtures: EvaluationFixtures,
        *,
        internal_token: str,
    ) -> None:
        self.case = case
        self.fixtures = fixtures
        self.internal_token = internal_token
        self.requests: list[dict[str, Any]] = []
        self.pushed_product_ids: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if request.headers.get("X-Internal-Token") != self.internal_token:
            return httpx.Response(401, json={"success": False})
        if request.method == "GET" and request.url.path == "/internal/products/search":
            fixture = self.fixtures.search_responses[self.case.search_fixture_id]
            products = [
                self.fixtures.catalog[str(product_id)]
                for product_id in fixture["productIds"]
                if str(product_id) in self.fixtures.catalog
            ]
            products = _filter_products_by_requested_price(products, request.url.params)
            return httpx.Response(200, json={"success": True, "data": products})
        if request.method == "GET" and request.url.path.startswith("/internal/members/"):
            persona_id = self.case.identity.persona_id
            history = self.fixtures.purchase_history.get(persona_id or "", {"orders": []})
            return httpx.Response(200, json={"success": True, "data": history})
        if request.method == "POST" and request.url.path == "/internal/recommendations":
            self.pushed_product_ids = [
                product_id
                for entry in (body or {}).get("lists", [])
                for product_id in entry.get("productIds", [])
            ]
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(404, json={"success": False})


class OfflineBuyerAdapter:
    """실제 decompose→search/I-19→rerank→I-21 경로를 케이스별 실행한다."""

    model_config = {
        "provider": "scripted",
        "decompose": "expectedFilters",
        "rerank": "searchOrderPassthrough",
    }

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or EvaluationSettings(
            auth_mode="dev",
            internal_api_token=_INTERNAL_TOKEN,
            search_backend="spring",
        )
        self.last_requests: list[dict[str, Any]] = []

    def __call__(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        return asyncio.run(self._run(case, fixtures))

    async def _run(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        fixture_id = case.search_fixture_id
        fixture = fixtures.search_responses.get(fixture_id) if fixture_id is not None else None
        if fixture is None:
            raise ValueError(f"{case.case_id}: search fixture가 없습니다")
        scripted = EvalScriptedLLM(case, list(fixture["productIds"]))
        transport = _CaseTransport(
            case,
            fixtures,
            internal_token=self.settings.internal_api_token,
        )

        def _client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=self.settings.spring_base_url,
                timeout=self.settings.spring_timeout_s,
                headers={"X-Internal-Token": self.settings.internal_api_token},
                transport=httpx.MockTransport(transport.handler),
            )

        decision = await decompose(
            scripted,
            query=case.query,
            prior_filters=None,
            profile_summary=None,
            tier="fast",
            category_fanout_max=self.settings.category_fanout_max,
            repurchase_max=self.settings.dedup_repurchase_max,
        )
        identity = Identity(
            user_id=None if case.identity.kind == "guest" else "42",
            is_guest=case.identity.kind == "guest",
            seller_id=None,
        )
        request = ChatRequest(
            session_id=f"eval-{case.case_id}",
            thread_id=f"eval-{case.case_id}",
            message=case.query,
        )

        async def _search(filters, exclude_product_ids=None):
            return await search_service.search_catalog(
                filters,
                exclude_product_ids,
                backend=search_service.SpringSearchBackend(),
            )

        with (
            patch.object(spring_client, "_client", _client),
            patch(
                "app.agents.buyer.recommendation.rerank.get_settings", return_value=self.settings
            ),
            patch.object(spring_client, "get_settings", return_value=self.settings),
        ):
            async for _ in stream_recommendation(
                request=request,
                decision=decision,
                llm=scripted,
                search=_search,
                push_fn=spring_client.push_recommendations,
                identity=identity,
                profile=None,
                settings=self.settings,
                request_id=f"eval-{case.case_id}",
            ):
                pass

        self.last_requests = transport.requests
        return {
            "rankedProductIds": transport.pushed_product_ids,
            "extractedFilters": scripted.extracted_filters,
            "modelConfig": dict(self.model_config),
        }
