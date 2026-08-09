from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.buyer import graph as buyer_graph
from app.core.config import Settings
from app.schemas.spring import ProductSearchFilters
from evals.personalization import live_adapter as live_adapter_module
from evals.personalization.live_adapter import (
    PersonalizationLiveBuyerAdapter,
    estimate_live_matrix,
    filter_axis_leakage,
    profile_for_scope,
)
from evals.personalization.overreach import clean_noisy_drop_verdict, count_verdict
from evals.personalization.profile_markdown import render_profile_markdown
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


class _StubLiveAdapter:
    """실 LLM·검색 왕복 없이 wrapper 의 프로필 해석만 통과시키는 내부 어댑터 대역."""

    def __init__(self) -> None:
        self.last_output: dict[str, object] = {}
        self.model_config: dict[str, object] = {}

    def __call__(self, case, fixtures) -> dict[str, object]:
        return {}


def _resolved_markdowns(monkeypatch, cases, **adapter_kwargs) -> list[str | None]:
    """`profile_for_scope` 입력을 가로채 arm 이 케이스마다 어떤 프로필을 골랐는지만 본다.

    실 `LiveBuyerAdapter` 를 태우지 않는다 — 그쪽은 `asyncio.run` 을 쓰는데 Windows 의
    ProactorEventLoop 는 self-pipe 를 **TCP 루프백**으로 만들어 conftest 의 TCP 차단에
    걸린다(Linux 는 AF_UNIX 라 통과). 주입 경로만 보는 테스트가 OS 에 따라 갈릴 이유가 없다.
    """
    seen: list[str | None] = []
    original = live_adapter_module.profile_for_scope

    def capture(profile_markdown, scope):
        seen.append(profile_markdown)
        return original(profile_markdown, scope)

    monkeypatch.setattr(live_adapter_module, "profile_for_scope", capture)
    adapter = PersonalizationLiveBuyerAdapter(
        SimpleNamespace(calls=[]),
        identity_kind="member",
        settings=Settings(
            _env_file=None,
            auth_mode="dev",
            internal_api_token="model-eval-internal-token",
            profile_injection_scope="both",
        ),
        **adapter_kwargs,
    )
    adapter.adapter = _StubLiveAdapter()
    for case in cases:
        adapter(case, object())
    return seen


def test_live_wrapper_resolves_markdown_per_case(monkeypatch) -> None:
    """[#484] 케이스마다 resolver 를 다시 부른다 — 고정값이면 전 케이스가 같은 프로필이 된다."""
    from evals.goldenset.loader import load_cases

    cases = sorted(load_cases("dev"), key=lambda case: case.case_id)[:3]
    seen = _resolved_markdowns(
        monkeypatch,
        cases,
        profile_markdown="생성자-고정",
        markdown_resolver=lambda case, _fixtures: f"프로필-{case.case_id}",
    )
    assert seen == [f"프로필-{case.case_id}" for case in cases]


def test_live_wrapper_without_resolver_keeps_constructor_markdown(monkeypatch) -> None:
    """resolver 미지정 경로의 동작은 그대로다(기존 arm 회귀 가드)."""
    from evals.goldenset.loader import load_cases

    cases = sorted(load_cases("dev"), key=lambda case: case.case_id)[:2]
    seen = _resolved_markdowns(monkeypatch, cases, profile_markdown="생성자-고정")
    assert seen == ["생성자-고정", "생성자-고정"]


def test_four_arm_three_repeat_run_needs_an_explicit_budget_override() -> None:
    """[#483] `--repeats 3` 은 기본 상한(800)으로는 못 돈다 — env override 가 **의도된 절차**다.

    예산 상한은 튜너블이 아니라 운영 안전장치라 기본값을 올리지 않는다(`evals/model_eval/README`).
    이 테스트는 그 절차가 필요하다는 사실 자체를 고정한다 — 기본값이 조용히 올라가면 같은
    상한을 공유하는 model_eval·ablation 하네스의 방어선까지 함께 풀린다.

    실지출은 상한과 무관하게 작다: 실측 단가(≈$0.000478/call)로 3,924호출 ≈ $1.9 (비용 상한 $20).
    """
    from evals.goldenset.loader import load_cases

    case_count = len(list(load_cases("dev")))

    def table(max_calls: int) -> dict:
        return estimate_live_matrix(
            case_count=case_count,
            arm_count=4,
            repeats=3,
            model="gpt-5.6-luna",
            # `_env_file=None` 필수 — 로컬 .env 의 override 가 새면 결과가 환경마다 달라진다.
            settings=Settings(_env_file=None, model_eval_max_calls_per_run=max_calls),
        )

    default_cap = Settings(_env_file=None).model_eval_max_calls_per_run
    assert table(default_cap)["expectedCalls"] == case_count * 4 * 3 * 3
    assert table(default_cap)["allowed"] is False
    assert table(4000)["allowed"] is True


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
    # [#484] 강도 임계도 실제 소비자(마크다운 렌더러)의 출력을 바꾼다.
    preferences = {"brands": {"이니스프리": 1.0, "설화수": 2 / 3}, "categories": {}}
    assert "설화수 (중)" in render_profile_markdown(
        preferences,
        max_chars=strict.profile_summary_max_chars,
        strength_bands=strict.personalization_eval_profile_strength_bands,
    )
    assert "설화수 (강)" in render_profile_markdown(
        preferences,
        max_chars=strict.profile_summary_max_chars,
        strength_bands=Settings(
            _env_file=None, personalization_eval_profile_strength_bands=(0.5, 0.3)
        ).personalization_eval_profile_strength_bands,
    )
