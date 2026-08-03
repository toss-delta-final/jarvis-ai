from __future__ import annotations

import pytest
from pydantic import ValidationError

from evals.metrics.settings import EvaluationSettings


def test_scoring_settings_reject_negative_weight() -> None:
    with pytest.raises(ValidationError, match="추천 scoring 가중치는 음수일 수 없습니다"):
        EvaluationSettings(scoring_weight_semantic=-0.1)


@pytest.mark.parametrize("weight", [float("nan"), float("inf")])
def test_scoring_settings_reject_non_finite_weight(weight: float) -> None:
    with pytest.raises(ValidationError, match="추천 scoring 가중치는 유한한 수여야 합니다"):
        EvaluationSettings(scoring_weight_semantic=weight)


def test_scoring_settings_reject_all_zero_weights() -> None:
    with pytest.raises(
        ValidationError,
        match=(
            "추천 scoring 양의 신호 가중치"
            r"\(semantic·profile·popularity·recency·diversity\)"
            " 중 하나 이상은 양수여야 합니다"
        ),
    ):
        EvaluationSettings(
            scoring_weight_semantic=0,
            scoring_weight_profile_match=0,
            scoring_weight_popularity=0,
            scoring_weight_recency=0,
            scoring_weight_diversity_bonus=0,
            scoring_weight_recent_purchase_penalty=0,
        )


def test_scoring_settings_reject_penalty_only_weight() -> None:
    with pytest.raises(
        ValidationError,
        match=(
            "추천 scoring 양의 신호 가중치"
            r"\(semantic·profile·popularity·recency·diversity\)"
            " 중 하나 이상은 양수여야 합니다"
        ),
    ):
        EvaluationSettings(
            scoring_weight_semantic=0,
            scoring_weight_profile_match=0,
            scoring_weight_popularity=0,
            scoring_weight_recency=0,
            scoring_weight_diversity_bonus=0,
            scoring_weight_recent_purchase_penalty=0.5,
        )


def test_scoring_settings_accept_single_positive_signal_without_penalty() -> None:
    settings = EvaluationSettings(
        scoring_weight_semantic=0.5,
        scoring_weight_profile_match=0,
        scoring_weight_popularity=0,
        scoring_weight_recency=0,
        scoring_weight_diversity_bonus=0,
        scoring_weight_recent_purchase_penalty=0,
    )

    assert settings.scoring_weight_semantic == 0.5
    assert settings.scoring_weight_recent_purchase_penalty == 0


@pytest.mark.parametrize("reference_date", ["2026-13-01", "not-a-date"])
def test_scoring_settings_require_iso_reference_date(reference_date: str) -> None:
    with pytest.raises(ValidationError, match="추천 scoring 기준일은 ISO 날짜 형식이어야 합니다"):
        EvaluationSettings(scoring_reference_date=reference_date)


def test_scoring_settings_require_positive_purchase_window() -> None:
    with pytest.raises(ValidationError, match="추천 scoring 최근 구매 window는 0보다 커야 합니다"):
        EvaluationSettings(scoring_recent_purchase_window_days=0)
