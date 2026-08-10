"""실제 구매자 앱 경로의 rerank만 결정론 scoring으로 교체하는 평가 adapter."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx

from app.agents.buyer.recommendation import graph as recommendation_graph
from app.agents.buyer.recommendation.decompose import decompose
from app.core.auth import Identity
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.services import search_service, spring_client
from evals.goldenset.schema import GoldenCase
from evals.metrics.harness import EvalScriptedLLM, OfflineBuyerAdapter, _CaseTransport
from evals.metrics.runner import EvaluationFixtures
from evals.metrics.settings import EvaluationSettings
from evals.scoring.embeddings import EmbeddingFixture, load_embeddings
from evals.scoring.hard_filter import HardConstraints, apply_hard_filters
from evals.scoring.scorer import ScoringResult, ScoringWeights, score_products

_INTERNAL_TOKEN = "eval-internal-token"


def _reference_now(settings: Settings) -> datetime:
    """골든셋 기준일 자정을 앱 dedup과 같은 naive UTC datetime으로 만든다."""
    return datetime.fromisoformat(settings.scoring_reference_date)


class ReferenceClockBuyerAdapter:
    """passthrough arm의 앱 dedup clock을 골든셋 기준일로 고정한다.

    절대 날짜 구매 fixture를 실시간 clock으로 판정하면 실행일에 따라 후보가 달라지므로,
    scoring arm과 동일한 기준일을 적용하되 나머지 OfflineBuyerAdapter 경로는 그대로 둔다.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.adapter = OfflineBuyerAdapter(settings=settings)
        self.settings = self.adapter.settings
        self.model_config = self.adapter.model_config
        self.last_requests: list[dict[str, Any]] = []

    def __call__(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        with patch.object(
            recommendation_graph, "_now", return_value=_reference_now(self.settings)
        ):
            output = self.adapter(case, fixtures)
        self.last_requests = self.adapter.last_requests
        return output


def _preferences(
    history: dict[str, object] | None, catalog: dict[str, dict]
) -> dict[str, dict[str, float]] | None:
    if not history:
        return None
    categories: Counter[str] = Counter()
    brands: Counter[str] = Counter()
    for order in history.get("orders", []):
        for item in order.get("items", []):
            if item.get("categoryName"):
                categories[str(item["categoryName"])] += 1
            product = catalog.get(str(item.get("productId")), {})
            if product.get("brandName"):
                brands[str(product["brandName"])] += 1

    def normalize(counts: Counter[str]) -> dict[str, float]:
        maximum = max(counts.values(), default=0)
        return {key: value / maximum for key, value in counts.items()} if maximum else {}

    if not categories and not brands:
        return None
    return {"categories": normalize(categories), "brands": normalize(brands)}


def _recent_ids(
    history: dict[str, object] | None, reference_date: str, window_days: int
) -> list[int]:
    if not history:
        return []
    lower = date.fromisoformat(reference_date) - timedelta(days=window_days)
    recent: set[int] = set()
    for order in history.get("orders", []):
        ordered = datetime.fromisoformat(str(order["orderedAt"])).date()
        if lower <= ordered <= date.fromisoformat(reference_date):
            recent.update(int(item["productId"]) for item in order.get("items", []))
    return sorted(recent)


def weights_from_settings(settings: Settings) -> ScoringWeights:
    """Settings의 모든 scoring 튜너블을 scorer 입력으로 옮긴다."""
    return ScoringWeights(
        semantic=settings.scoring_weight_semantic,
        profile_match=settings.scoring_weight_profile_match,
        popularity=settings.scoring_weight_popularity,
        recency=settings.scoring_weight_recency,
        diversity_bonus=settings.scoring_weight_diversity_bonus,
        recent_purchase_penalty=settings.scoring_weight_recent_purchase_penalty,
    )


class ScoringBuyerAdapter:
    """decompose/search/push는 기준 arm과 같고 scripted rerank 순서만 scoring으로 바꾼다."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embeddings: EmbeddingFixture | None = None,
        recency_by_product: dict[int, float] | None = None,
    ) -> None:
        self.settings = settings or EvaluationSettings(
            auth_mode="dev",
            internal_api_token=_INTERNAL_TOKEN,
            search_backend="spring",
        )
        self.embeddings = embeddings or load_embeddings()
        self.recency_by_product = recency_by_product
        self.last_requests: list[dict[str, Any]] = []
        self.score_records: dict[str, dict[str, object]] = {}
        weights = weights_from_settings(self.settings)
        self.model_config = {
            "provider": "scripted",
            "decompose": "expectedFilters",
            "rerank": "deterministicScoringBaseline",
            "weights": weights._asdict(),
            "referenceDate": self.settings.scoring_reference_date,
            "recentPurchaseWindowDays": self.settings.scoring_recent_purchase_window_days,
        }

    def __call__(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        return asyncio.run(self._run(case, fixtures))

    def _score(self, case: GoldenCase, fixtures: EvaluationFixtures) -> ScoringResult:
        fixture = fixtures.search_responses.get(case.search_fixture_id or "")
        if fixture is None:
            raise ValueError(f"{case.case_id}: search fixture가 없습니다")
        products = [
            fixtures.catalog[str(product_id)]
            for product_id in fixture["productIds"]
            if str(product_id) in fixtures.catalog
        ]
        filters = case.expected_filters
        filtered = apply_hard_filters(
            products,
            HardConstraints(
                price_max=filters.get("priceMax"),
                price_min=filters.get("priceMin"),
            ),
        )
        history = (
            fixtures.purchase_history.get(case.identity.persona_id or "")
            if case.identity.kind != "guest"
            else None
        )
        result = score_products(
            filtered.products,
            query_embedding=self.embeddings.queries.get(case.case_id),
            product_embeddings=self.embeddings.documents,
            profile_preferences=_preferences(history, fixtures.catalog),
            recency_by_product=self.recency_by_product,
            recent_product_ids=_recent_ids(
                history,
                self.settings.scoring_reference_date,
                self.settings.scoring_recent_purchase_window_days,
            ),
            weights=weights_from_settings(self.settings),
        )
        self.score_records[case.case_id] = {
            "caseId": case.case_id,
            "cuts": [
                {"productId": cut.product_id, "reason": cut.reason}
                for cut in filtered.cuts
            ],
            "scores": [
                {
                    "productId": row.product_id,
                    "finalScore": row.final_score,
                    "components": {
                        name: {
                            "value": component.value,
                            "weight": component.weight,
                            "contribution": component.contribution,
                            "degraded": component.degraded,
                            "reason": component.reason,
                        }
                        for name, component in row.components.items()
                    },
                }
                for row in result.scores
            ],
        }
        return result

    async def _run(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        scored = self._score(case, fixtures)
        scripted = EvalScriptedLLM(case, scored.ranked_product_ids)
        transport = _CaseTransport(
            case,
            fixtures,
            internal_token=self.settings.internal_api_token,
        )

        def _client(*, timeout: float | None = None) -> httpx.AsyncClient:
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
                "app.agents.buyer.recommendation.rerank.get_settings",
                return_value=self.settings,
            ),
            patch.object(spring_client, "get_settings", return_value=self.settings),
            patch.object(
                recommendation_graph,
                "_now",
                return_value=_reference_now(self.settings),
            ),
        ):
            async for _ in recommendation_graph.stream_recommendation(
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
