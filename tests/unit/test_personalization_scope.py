from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.buyer import graph as buyer_graph
from app.core.config import Settings
from app.schemas.spring import ProductSearchFilters
from evals.personalization.live_adapter import (
    PersonalizationLiveBuyerAdapter,
    estimate_live_matrix,
    filter_axis_leakage,
    profile_for_scope,
)
from evals.personalization.overreach import clean_noisy_drop_verdict, count_verdict
from evals.scoring.adapter import weights_from_settings


@pytest.mark.parametrize(
    ("scope", "decompose", "rerank"),
    [("off", None, None), ("rerank_only", None, "profile"), ("both", "profile", "profile")],
)
async def test_eval_scope_gate_matches_graph_behavior(
    scope: str, decompose: str | None, rerank: str | None, monkeypatch
) -> None:
    from tests.unit import test_recommendation as recommendation_test

    captured = {}
    original_decompose = buyer_graph.decompose
    original_stream = buyer_graph.stream_recommendation

    async def capture_decompose(*args, **kwargs):
        captured["decompose"] = kwargs["profile_summary"]
        return await original_decompose(*args, **kwargs)

    async def capture_stream(*args, **kwargs):
        captured["rerank"] = kwargs["profile"]
        async for frame in original_stream(*args, **kwargs):
            yield frame

    monkeypatch.setattr(recommendation_test.get_settings(), "profile_injection_scope", scope)
    monkeypatch.setattr(buyer_graph, "decompose", capture_decompose)
    monkeypatch.setattr(buyer_graph, "stream_recommendation", capture_stream)
    await recommendation_test._seed_profile(markdown="profile")
    await recommendation_test._collect(
        recommendation_test.run_buyer_turn(
            recommendation_test._req(),
            recommendation_test._member(),
            llm=recommendation_test.FakeLLM(),
            search=recommendation_test._make_search(recommendation_test.DEFAULT_PRODUCTS),
            push_fn=recommendation_test._RecordingPush(),
        )
    )
    expected = profile_for_scope("profile", scope)
    assert (captured["decompose"], captured["rerank"]) == expected == (decompose, rerank)


def test_personalization_settings_defaults_and_validation() -> None:
    settings = Settings(_env_file=None)
    assert settings.personalization_eval_weight_sweep == (0.0, 0.075, 0.15, 0.3, 0.6)
    for kwargs in (
        {"personalization_eval_clean_noisy_drop_margin": -0.1},
        {"personalization_eval_weight_sweep": (0.0, 0.15, 0.1)},
        {"personalization_eval_weight_sweep": (0.0, 0.2)},
        {"personalization_eval_hard_filter_violation_max": -1},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **kwargs)


def test_live_dry_run_multiplies_calls_by_arm_count() -> None:
    table = estimate_live_matrix(case_count=3, arm_count=2, repeats=1, model="gpt-5.6-luna")
    assert table["expectedCalls"] == 18
    assert table["caseCount"] == 3 and table["armCount"] == 2
    assert table["estimatedCostUsd"] is not None


def test_live_dry_run_uses_injected_budget_limits() -> None:
    settings = Settings(
        _env_file=None,
        model_eval_max_calls_per_run=7,
        model_eval_max_total_tokens_per_run=11,
        model_eval_max_cost_usd_per_run=0.25,
    )
    table = estimate_live_matrix(
        case_count=1, arm_count=2, repeats=1, model="gpt-5.6-luna", settings=settings
    )
    assert (table["maxCalls"], table["maxTotalTokens"], table["maxCostUsd"]) == (7, 11, 0.25)


def test_live_wrapper_routes_profile_for_all_scopes(monkeypatch) -> None:
    from evals.goldenset.loader import load_cases
    from evals.metrics.runner import load_evaluation_fixtures
    from evals.model_eval import adapter as live_module

    case = list(load_cases("dev"))[0]
    fixtures = load_evaluation_fixtures()
    captured = []

    async def fake_decompose(*args, **kwargs):
        captured.append(("decompose", kwargs["profile_summary"]))
        return SimpleNamespace(intent="recommend", filters=ProductSearchFilters())

    async def fake_stream(*args, **kwargs):
        captured.append(("rerank", kwargs["profile"]))
        if False:
            yield None

    monkeypatch.setattr(live_module, "decompose", fake_decompose)
    monkeypatch.setattr(live_module, "stream_recommendation", fake_stream)
    for scope in ("off", "rerank_only", "both"):
        settings = Settings(
            _env_file=None,
            auth_mode="dev",
            internal_api_token="model-eval-internal-token",
            profile_injection_scope=scope,
        )
        adapter = PersonalizationLiveBuyerAdapter(
            SimpleNamespace(calls=[]),
            profile_markdown="profile",
            identity_kind="member",
            settings=settings,
        )
        adapter(case, fixtures)
    assert captured == [
        ("decompose", None),
        ("rerank", None),
        ("decompose", None),
        ("rerank", "profile"),
        ("decompose", "profile"),
        ("rerank", "profile"),
    ]


def test_filter_axis_leakage_reuses_app_axes_and_includes_attr_conditions() -> None:
    assert filter_axis_leakage(
        {"keyword": "이어폰"}, {"keyword": "이어폰", "attrConditions": {"color": "black"}}
    ) == ["attr_conditions"]


def test_every_personalization_setting_changes_a_consumer() -> None:
    strict = Settings(_env_file=None)
    relaxed = Settings(
        _env_file=None,
        personalization_eval_hard_filter_violation_max=1,
        personalization_eval_intent_contradiction_max=1,
        personalization_eval_forbidden_inclusion_max=1,
        personalization_eval_clean_noisy_drop_margin=0.1,
        personalization_eval_weight_sweep=(0.0, 0.15, 0.9),
    )
    assert count_verdict(1, strict.personalization_eval_hard_filter_violation_max) == "regression"
    assert count_verdict(1, relaxed.personalization_eval_hard_filter_violation_max) == "pass"
    assert count_verdict(1, relaxed.personalization_eval_intent_contradiction_max) == "pass"
    assert count_verdict(1, relaxed.personalization_eval_forbidden_inclusion_max) == "pass"
    assert (
        clean_noisy_drop_verdict(
            {"low": -0.05, "high": -0.04},
            margin=strict.personalization_eval_clean_noisy_drop_margin,
        )["verdict"]
        == "regression"
    )
    assert (
        clean_noisy_drop_verdict(
            {"low": -0.05, "high": -0.04},
            margin=relaxed.personalization_eval_clean_noisy_drop_margin,
        )["verdict"]
        == "pass"
    )
    assert (
        weights_from_settings(
            Settings(
                _env_file=None,
                scoring_weight_profile_match=relaxed.personalization_eval_weight_sweep[-1],
            )
        ).profile_match
        == 0.9
    )
