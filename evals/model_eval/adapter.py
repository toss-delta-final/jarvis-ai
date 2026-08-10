"""고정 Spring fixture와 실제 LLM을 결합한 구매자 평가 adapter."""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from typing import Any
from unittest.mock import patch

import httpx

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.graph import stream_recommendation
from app.core.auth import Identity
from app.core.config import Settings
from app.core.llm import LLMClient
from app.schemas.chat import ChatRequest
from app.services import search_service, spring_client
from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.budget import BudgetExceeded
from evals.model_eval.recording import RecordingLLM

_INTERNAL_TOKEN = "model-eval-internal-token"
# 이 필드들은 모델의 라벨 예측이 아니라 검색 경로가 채우는 기계적 기본값이다.
_EXTRACTED_FILTER_EXCLUSIONS = frozenset({"limit", "excludeProductIds"})


def _evaluation_filters(filters: Any) -> dict[str, Any]:
    """Filter Accuracy에는 모델이 실제 산출한 비어 있지 않은 필드만 전달한다."""
    dumped = filters.model_dump(by_alias=True, exclude_none=True)
    return {
        key: value
        for key, value in dumped.items()
        if key not in _EXTRACTED_FILTER_EXCLUSIONS
        and not (isinstance(value, (str, list)) and not value)
    }


class _CaseTransport:
    """카탈로그 snapshot의 I-1/I-19/I-21 응답만 제공한다."""

    def __init__(
        self, case: GoldenCase, fixtures: EvaluationFixtures, *, internal_token: str
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
                int(product_id)
                for entry in (body or {}).get("lists", [])
                for product_id in entry.get("productIds", [])
            ]
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(404, json={"success": False})


class LiveBuyerAdapter:
    """실제 decompose→고정 검색→rerank→I-21 경로를 케이스별 실행한다."""

    def __init__(
        self,
        llm: RecordingLLM | LLMClient,
        *,
        settings: Settings | None = None,
        model_config: dict[str, Any] | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or EvaluationSettings(
            auth_mode="dev",
            internal_api_token=_INTERNAL_TOKEN,
            search_backend="spring",
        )
        self.model_config = model_config or {"provider": "custom"}
        self.last_output: dict[str, Any] = {}
        self.last_requests: list[dict[str, Any]] = []

    def __call__(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        return asyncio.run(self._run(case, fixtures))

    async def _run(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        started = perf_counter()
        call_start = len(getattr(self.llm, "calls", []))
        transport = _CaseTransport(
            case,
            fixtures,
            internal_token=self.settings.internal_api_token,
        )
        failure_reason: str | None = None
        fixture = fixtures.search_responses.get(case.search_fixture_id or "", {})
        expected_zero_candidates = not fixture.get("productIds")
        extracted_filters: dict[str, Any] = {}
        try:
            decision = await decompose(
                self.llm,
                query=case.query,
                prior_filters=None,
                profile_summary=None,
                tier="fast",
                category_fanout_max=self.settings.category_fanout_max,
                repurchase_max=self.settings.dedup_repurchase_max,
            )
            extracted_filters = _evaluation_filters(decision.filters)
            if decision.intent != "recommend":
                failure_reason = f"nonRecommendIntent:{decision.intent}"
            else:

                def _client(*, timeout: float | None = None) -> httpx.AsyncClient:
                    return httpx.AsyncClient(
                        base_url=self.settings.spring_base_url,
                        timeout=self.settings.spring_timeout_s,
                        headers={"X-Internal-Token": self.settings.internal_api_token},
                        transport=httpx.MockTransport(transport.handler),
                    )

                async def _search(filters, exclude_product_ids=None):
                    return await search_service.search_catalog(
                        filters,
                        exclude_product_ids,
                        backend=search_service.SpringSearchBackend(),
                    )

                identity = Identity(
                    user_id=None if case.identity.kind == "guest" else "42",
                    is_guest=case.identity.kind == "guest",
                    seller_id=None,
                )
                request = ChatRequest(
                    session_id=f"model-eval-{case.case_id}",
                    thread_id=f"model-eval-{case.case_id}",
                    message=case.query,
                )
                with (
                    patch.object(spring_client, "_client", _client),
                    patch(
                        "app.agents.buyer.recommendation.rerank.get_settings",
                        return_value=self.settings,
                    ),
                    patch.object(spring_client, "get_settings", return_value=self.settings),
                ):
                    async for _ in stream_recommendation(
                        request=request,
                        decision=decision,
                        llm=self.llm,
                        search=_search,
                        push_fn=spring_client.push_recommendations,
                        identity=identity,
                        profile=None,
                        settings=self.settings,
                        request_id=f"model-eval-{case.case_id}",
                    ):
                        pass
                if not transport.pushed_product_ids and not expected_zero_candidates:
                    failure_reason = "emptyPush"
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - 한 반복 실패를 전체 run과 격리
            failure_reason = f"{type(exc).__name__}:{exc}"

        calls = list(getattr(self.llm, "calls", [])[call_start:])
        if failure_reason is None and any(call.get("error") for call in calls):
            failure_reason = "providerCallFailed"
        self.last_requests = transport.requests
        self.last_output = {
            "rankedProductIds": transport.pushed_product_ids if failure_reason is None else [],
            "extractedFilters": extracted_filters,
            "modelConfig": dict(self.model_config),
            "providerCalls": calls,
            "latencyMs": int(round((perf_counter() - started) * 1000)),
            "hardFailure": failure_reason is not None,
            "failureReason": failure_reason,
            "expectedZeroCandidates": expected_zero_candidates,
        }
        return self.last_output
