"""fixture profile을 scoring 경로에 주입하는 Tier D adapter."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from typing import Any

from evals.metrics.runner import EvaluationFixtures
from evals.scoring.adapter import (
    ScoringBuyerAdapter,
    _recent_ids,
    weights_from_settings,
)
from evals.scoring.hard_filter import HardConstraints, apply_hard_filters
from evals.scoring.scorer import ScoringResult, score_products


class ProfileScoringBuyerAdapter(ScoringBuyerAdapter):
    """구매 이력 파생 대신 profile arm의 구조화 선호를 scoring에 넣는다."""

    def __init__(
        self,
        *,
        profile_preferences: dict[str, dict[str, float]] | None,
        preference_resolver: Callable[
            [object, EvaluationFixtures], dict[str, dict[str, float]] | None
        ]
        | None = None,
        arm_name: str = "custom",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.profile_preferences = profile_preferences
        self.preference_resolver = preference_resolver
        self.model_config = {
            **self.model_config,
            "profileArm": arm_name,
            "profileSource": "caseDerivedFixtureArm",
        }

    def __call__(self, case, fixtures: EvaluationFixtures) -> dict[str, object]:
        """검색 mock에도 동일 hard-filter를 적용해 graph의 최소노출 보충 재유입을 막는다."""
        fixture_id = case.search_fixture_id or ""
        fixture = fixtures.search_responses.get(fixture_id)
        if fixture is None:
            raise ValueError(f"{case.case_id}: search fixture가 없습니다")
        products = [
            fixtures.catalog[str(product_id)]
            for product_id in fixture["productIds"]
            if str(product_id) in fixtures.catalog
        ]
        hard = case.hard_constraints
        constrained = apply_hard_filters(
            products,
            HardConstraints(
                price_max=hard.price_max,
                price_min=hard.price_min,
                forbidden_categories=frozenset(hard.forbidden_categories),
                forbidden_product_ids=frozenset(hard.forbidden_product_ids),
                must_exclude_product_ids=frozenset(case.must_exclude_product_ids),
            ),
        )
        responses = dict(fixtures.search_responses)
        responses[fixture_id] = {
            **fixture,
            "productIds": [int(product["productId"]) for product in constrained.products],
        }
        return super().__call__(case, replace(fixtures, search_responses=responses))

    def _score(self, case, fixtures: EvaluationFixtures) -> ScoringResult:
        fixture = fixtures.search_responses.get(case.search_fixture_id or "")
        if fixture is None:
            raise ValueError(f"{case.case_id}: search fixture가 없습니다")
        products = [
            fixtures.catalog[str(product_id)]
            for product_id in fixture["productIds"]
            if str(product_id) in fixtures.catalog
        ]
        hard = case.hard_constraints
        filtered = apply_hard_filters(
            products,
            HardConstraints(
                price_max=hard.price_max,
                price_min=hard.price_min,
                forbidden_categories=frozenset(hard.forbidden_categories),
                forbidden_product_ids=frozenset(hard.forbidden_product_ids),
                must_exclude_product_ids=frozenset(case.must_exclude_product_ids),
            ),
        )
        history: dict[str, Any] | None = None
        if case.identity.kind != "guest":
            history = fixtures.purchase_history.get(case.identity.persona_id or "")
        result = score_products(
            filtered.products,
            query_embedding=self.embeddings.queries.get(case.case_id),
            product_embeddings=self.embeddings.documents,
            profile_preferences=(
                self.preference_resolver(case, fixtures)
                if self.preference_resolver is not None
                else self.profile_preferences
            ),
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
            "cuts": [{"productId": cut.product_id, "reason": cut.reason} for cut in filtered.cuts],
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
