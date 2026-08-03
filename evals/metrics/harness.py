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
