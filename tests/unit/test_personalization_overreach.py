from __future__ import annotations

from evals.personalization.overreach import (
    clean_noisy_drop_verdict,
    explicit_intent_contradictions,
    new_forbidden_or_recent_inclusions,
)


def test_explicit_intent_contradiction_fires_and_stays_quiet() -> None:
    assert explicit_intent_contradictions({"brand": ["Apple"]}, {"brand": ["Samsung"]}) == ["brand"]
    assert explicit_intent_contradictions({"brand": ["Apple"]}, {"brand": ["Apple"]}) == []


def test_forbidden_and_recent_inclusion_fires_and_stays_quiet() -> None:
    assert new_forbidden_or_recent_inclusions(
        guest_ranked=[1],
        profile_ranked=[1, 2, 3],
        forbidden_ids={2},
        recent_ids={3},
        repurchase_intent=False,
        top_k=10,
    ) == {"forbiddenProductIds": [2], "recentProductIds": [3], "count": 2}
    assert (
        new_forbidden_or_recent_inclusions(
            guest_ranked=[1, 2, 3],
            profile_ranked=[1, 2, 3],
            forbidden_ids={2},
            recent_ids={3},
            repurchase_intent=False,
            top_k=10,
        )["count"]
        == 0
    )


def test_clean_noisy_drop_uses_declared_margin_and_ci_rule() -> None:
    passed = clean_noisy_drop_verdict({"low": -0.02, "high": 0.01}, margin=0.03)
    failed = clean_noisy_drop_verdict({"low": -0.08, "high": -0.04}, margin=0.03)
    assert passed["verdict"] == "pass"
    assert failed["verdict"] == "regression"
