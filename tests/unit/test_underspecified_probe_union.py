"""union 측정 모드(#432) — 전부 fake 다. 네트워크·pg 콜 0(`tests/conftest.py` 의 autouse
`reset_store()`/`reset_revert_store()` 가 `_prepare_recommendation` 의 `get_revert_store()` 를
InMemoryStore 로 고정한다).

각 테스트는 "엔진을 무조건 pass 로 바꿔도 통과하는가"를 스스로 묻는다 — 손계산한 기대값과
대조하고, `keys()`·존재 여부만 보는 검사는 넣지 않는다(공허한 검증 금지, #380 패킷 §D14 승계).
"""

from __future__ import annotations

from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.config import Settings
from app.schemas.spring import ProductSearchFilters
from evals.intent_probe.client import PacedLLM, SystemPromptOverrideLLM, build_probe_llm
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.underspecified_probe.loader import load_anchor_set
from evals.underspecified_probe.metrics import (
    UNION_AXIS_BUILDERS,
    axis_expansion_gate_fired_rate,
    axis_expansion_suppression_rate,
    axis_miss_rate,
    axis_miss_rate_after_expansion,
    diagnostics,
    score_all,
)
from evals.underspecified_probe.report import UNION_SAMPLE_COLUMNS, _axis_row, write_artifacts
from evals.underspecified_probe.runner import CellResult, JudgmentSettings, Sample
from evals.underspecified_probe.union import (
    UnionSampleResult,
    run_union_stage,
    skipped_union_result,
)

ANCHORS = load_anchor_set()
BY_ID = {anchor.case_id: anchor for anchor in ANCHORS.utterances}
JUDGMENT = JudgmentSettings(underspecified_reask_enabled=True)


async def _virtual_pacer() -> GlobalPacer:
    state = {"now": 0.0}

    async def _sleep(seconds: float) -> None:
        state["now"] += seconds

    return GlobalPacer(
        PacerLimits(max_rpm=10_000, max_tpm=10_000_000), clock=lambda: state["now"], sleep=_sleep
    )


def _blank_decision(**overrides: object) -> RouteDecision:
    base: dict[str, object] = {
        "intent": "recommend",
        "filters": ProductSearchFilters(),
        "case": 2,
        "semantic_query_is_fallback": True,
    }
    base.update(overrides)
    return RouteDecision(**base)  # type: ignore[arg-type]


def _sample(
    case_id: str,
    *,
    verdict: bool,
    decision: RouteDecision | None = None,
    index: int = 0,
    union: UnionSampleResult | None = None,
) -> Sample:
    return Sample(
        case_id=case_id,
        sample_index=index,
        decision=decision if decision is not None else _blank_decision(),
        prior=None,
        verdict=verdict,
        expansion_reason=None,
        latency_ms=1,
        union=union,
    )


def _cell(case_id: str, samples: list[Sample]) -> CellResult:
    anchor = BY_ID[case_id]
    return CellResult(
        cell_id=case_id,
        case_id=case_id,
        slice=anchor.slice,
        samples=samples,
        attempts=len(samples),
        filled=True,
    )


def _union_ok(*, verdict: bool, expansion_reason: str | None = None) -> UnionSampleResult:
    return UnionSampleResult(
        verdict=verdict,
        mapped_leg_count=0,
        category_expanded=False,
        filters_category=None,
        expansion_reason=expansion_reason,
        blocking_axes=[],
        stage_latency_ms=1,
        stage_error=None,
    )


def _union_error(reason: str = "boom") -> UnionSampleResult:
    return UnionSampleResult(
        verdict=None,
        mapped_leg_count=None,
        category_expanded=None,
        filters_category=None,
        expansion_reason=None,
        blocking_axes=[],
        stage_latency_ms=1,
        stage_error=reason,
    )


# ─────────── (a) --union 없으면 union 축·컬럼이 산출물에 없다 ───────────


def test_union_axes_absent_when_union_disabled() -> None:
    samples = [_sample("buy-under-0002", verdict=False, union=None)]
    cells = [_cell("buy-under-0002", samples)]
    scored = score_all(cells, ANCHORS, union_enabled=False)
    for axis_id in UNION_AXIS_BUILDERS:
        assert axis_id not in scored["axes"]


def test_union_columns_absent_from_samples_csv_when_union_disabled(tmp_path) -> None:
    samples = [_sample("buy-under-0002", verdict=False, union=None)]
    cells = [_cell("buy-under-0002", samples)]
    scored = score_all(cells, ANCHORS, union_enabled=False)
    from evals.underspecified_probe.metrics import cause_axis_summary, compute_baseline, sample_rows
    from evals.underspecified_probe.report import build_results

    rows = sample_rows(cells, ANCHORS, JUDGMENT)
    results = build_results(
        cells=cells,
        scored=scored,
        diagnostics_payload=diagnostics(cells, ANCHORS),
        baseline=compute_baseline(cells, ANCHORS),
        cause_summary=cause_axis_summary(rows),
        sample_rows_payload=rows,
        unfilled=[],
        prompt={"source": "x", "sha256": "0" * 64, "sha12": "0" * 12, "charCount": 0},
        tier="fast",
        model_config={"fastModel": "x", "smartModel": "y"},
        fixture={"name": "a", "version": "v", "sha256": "0" * 64},
        n=1,
        judgment={"underspecifiedReaskEnabled": True},
        pacer={"waitCount": 0, "maxRpm": 1},
        budget={"totalCostUsd": 0.0, "unknownCostCallCount": 0, "costGateStatus": "ok"},
        dry_run=True,
        union_enabled=False,
    )
    manifest = {"hashes": {}}
    write_artifacts(
        tmp_path,
        results=results,
        manifest=manifest,
        cells=cells,
        sample_rows_payload=rows,
        union_enabled=False,
    )
    header = (tmp_path / "samples.csv").read_text(encoding="utf-8").splitlines()[0]
    for column in UNION_SAMPLE_COLUMNS:
        assert column not in header


# ─────────── (b) union 축 분모 = 대응 기존 축과 정확히 같은 표본 집합(union 실패 제외) ───────────


def test_miss_rate_after_expansion_denominator_matches_miss_rate_minus_union_errors() -> None:
    """buy-under-0002(no_condition, expectedReask=true) 표본 4개 — 3개는 union 성공, 1개는 실패."""
    samples = [
        _sample("buy-under-0002", verdict=True, index=0, union=_union_ok(verdict=True)),
        _sample("buy-under-0002", verdict=False, index=1, union=_union_ok(verdict=False)),
        _sample("buy-under-0002", verdict=True, index=2, union=_union_ok(verdict=False)),
        _sample("buy-under-0002", verdict=True, index=3, union=_union_error()),
    ]
    cells = [_cell("buy-under-0002", samples)]
    scored = score_all(cells, ANCHORS, union_enabled=True)
    miss = scored["axes"]["missRate"]
    miss_after = scored["axes"]["missRateAfterExpansion"]
    assert miss.denominator == 4  # decompose 단계 표본 전부(union 성패 무관)
    assert miss.numerator == 1  # verdict False 1건(index 1)
    assert miss_after.denominator == 3  # union 실패 1건(index 3) 제외
    assert miss_after.numerator == 2  # union verdict False 2건(index 1, 2)


def test_false_alarm_rate_after_expansion_denominator_matches_false_alarm_rate_minus_union_errors() -> (
    None
):
    """buy-under-0004(what_axis, expectedReask=false) 표본 3개."""
    samples = [
        _sample("buy-under-0004", verdict=False, index=0, union=_union_ok(verdict=False)),
        _sample("buy-under-0004", verdict=False, index=1, union=_union_ok(verdict=True)),
        _sample("buy-under-0004", verdict=False, index=2, union=_union_error()),
    ]
    cells = [_cell("buy-under-0004", samples)]
    scored = score_all(cells, ANCHORS, union_enabled=True)
    fa = scored["axes"]["falseAlarmRate"]
    fa_after = scored["axes"]["falseAlarmRateAfterExpansion"]
    assert fa.denominator == 3
    assert fa.numerator == 0
    assert fa_after.denominator == 2  # union 실패 1건 제외
    assert fa_after.numerator == 1  # union verdict True 1건(index 1)


def test_union_stage_error_count_diagnostic_matches_excluded_samples() -> None:
    samples = [
        _sample("buy-under-0002", verdict=True, index=0, union=_union_ok(verdict=True)),
        _sample("buy-under-0002", verdict=True, index=1, union=_union_error()),
        _sample("buy-under-0002", verdict=True, index=2, union=_union_error()),
    ]
    cells = [_cell("buy-under-0002", samples)]
    diag = diagnostics(cells, ANCHORS)
    assert diag["unionStageErrorCount"] == 2


# ─────────── (c) union 은 사본에서 돈다 — decompose 단계 축이 오염되지 않는다 ───────────


async def test_union_stage_runs_on_a_clone_decompose_decision_untouched() -> None:
    original = _blank_decision(
        category_queries=[],
        semantic_query_is_fallback=True,
        case=1,
    )
    original_legs_before = list(original.category_legs)
    original_category_before = original.filters.category
    original_semantic_before = original.semantic_query_is_fallback

    async def _mutating_map_categories(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[("전자기기", "노트북"), ("가전", "청소기")])

    settings = Settings()
    result = await run_union_stage(
        decision=original,
        prior=None,
        utterance="아무거나 추천해줘",
        llm=None,
        settings=settings,
        judgment_settings=JUDGMENT,
        thread_key="test:clone:1",
        map_categories=_mutating_map_categories,
    )
    assert result.ok
    assert result.mapped_leg_count == 2  # union 단계는 실제로 legs 를 채웠다
    # 원본은 제자리 변경되지 않았다 — union 단계가 legs 를 2개 채웠어도 원본은 여전히 비어 있다.
    assert original.category_legs == original_legs_before == []
    assert original.filters.category == original_category_before is None
    assert original.semantic_query_is_fallback == original_semantic_before is True


# ─────────── (d) union 단계 예외 → 표본은 산다, union 축 분모에서만 제외, decompose 축 불변 ───────────


async def test_union_stage_exception_does_not_raise_and_marks_stage_error() -> None:
    """`map_categories` 호출 자체의 예외는 프로덕션(`_map_or_empty`)이 흡수해(canonical-or-null
    불변식) `run_union_stage` 까지 올라오지 않는다 — 실제로 예외가 새는 지점은 니즈 전개
    (`expand_needs`, 매핑 뒤·감싸이지 않은 호출)다. 그 경로로 union 단계 실패를 재현한다."""
    from app.agents.buyer.recommendation.state import CategoryQuery

    decision = _blank_decision(case=3)
    decision.category_queries = [CategoryQuery(raw_category=None, query="집들이 선물")]

    async def _unresolved_map_categories(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[], unresolved=["집들이 선물"])

    async def _raising_expand_needs(message, *, llm, settings, observer=None):
        raise RuntimeError("전개 LLM 호출 실패(스크립트)")

    result = await run_union_stage(
        decision=decision,
        prior=None,
        utterance="집들이 선물로 뭐 사갈까",
        llm=None,
        settings=Settings(),
        judgment_settings=JUDGMENT,
        thread_key="test:error:1",
        map_categories=_unresolved_map_categories,
        expand_needs=_raising_expand_needs,
    )
    assert not result.ok
    assert result.stage_error is not None
    assert "RuntimeError" in result.stage_error
    assert result.verdict is None


def test_union_stage_error_sample_survives_and_decompose_axis_is_unaffected() -> None:
    """decompose 단계 표본 수·missRate 는 union 실패와 무관하게 그대로다."""
    samples = [
        _sample("buy-under-0002", verdict=False, index=0, union=_union_error()),
        _sample("buy-under-0002", verdict=True, index=1, union=_union_ok(verdict=True)),
    ]
    cells = [_cell("buy-under-0002", samples)]
    pairs_missing_union_disabled = score_all(cells, ANCHORS, union_enabled=False)
    pairs_union_enabled = score_all(cells, ANCHORS, union_enabled=True)
    # union 활성 여부가 decompose 축(missRate)의 분자·분모를 바꾸지 않는다.
    assert (
        pairs_missing_union_disabled["axes"]["missRate"].as_dict()
        == pairs_union_enabled["axes"]["missRate"].as_dict()
    )
    assert pairs_union_enabled["axes"]["missRate"].numerator == 1
    assert pairs_union_enabled["axes"]["missRate"].denominator == 2
    # union 축은 실패 표본을 뺀 1건만 본다.
    assert pairs_union_enabled["axes"]["missRateAfterExpansion"].denominator == 1


# ─────────── (e) 분모 0 인 union 축은 report.md 에 "해당 없음"으로 렌더된다 ───────────


def test_zero_denominator_union_axis_renders_not_applicable_not_exploratory_badge() -> None:
    samples = [_sample("buy-under-0002", verdict=True, index=0, union=_union_error())]
    cells = [_cell("buy-under-0002", samples)]
    scored = score_all(cells, ANCHORS, union_enabled=True)
    axis = scored["axes"]["missRateAfterExpansion"]
    assert axis.denominator == 0
    row = _axis_row("missRateAfterExpansion", axis.as_dict())
    assert "해당 없음" in row
    assert "N<40" not in row


# ─────────── (f) 변이 시험(양방향) — 전개가 판정을 뒤집으면 축이 그것을 실제로 잡는다 ───────────


async def test_expansion_suppression_rate_flips_when_mapping_fills_legs_bidirectional() -> None:
    """decompose 판정 True 인 표본에 no-op union 을 물리면 억제율 0, 뒤집는 union 을 물리면
    억제율이 0 이 아니게 된다 — 양방향 모두 실제로 확인한다(공허한 검증 금지)."""
    decision_template = _blank_decision(category_queries=[], case=1)
    settings = Settings()

    async def _noop_map_categories(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[])  # 아무것도 못 찾음 — 판정을 못 뒤집는다

    async def _flipping_map_categories(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[("전자기기", "노트북")])  # legs 를 채워 판정을 뒤집는다

    noop_result = await run_union_stage(
        decision=decision_template,
        prior=None,
        utterance="아무거나 추천해줘",
        llm=None,
        settings=settings,
        judgment_settings=JUDGMENT,
        thread_key="test:mut:noop",
        map_categories=_noop_map_categories,
    )
    flip_result = await run_union_stage(
        decision=decision_template,
        prior=None,
        utterance="아무거나 추천해줘",
        llm=None,
        settings=settings,
        judgment_settings=JUDGMENT,
        thread_key="test:mut:flip",
        map_categories=_flipping_map_categories,
    )
    assert noop_result.ok and flip_result.ok
    assert noop_result.verdict is True  # 아무것도 안 바뀌었으니 판정도 안 바뀐다
    assert flip_result.verdict is False  # legs 가 채워져 판정이 뒤집혔다

    flip_sample = _sample(
        "buy-under-0002", verdict=True, index=0, decision=decision_template, union=flip_result
    )
    noop_sample = _sample(
        "buy-under-0002", verdict=True, index=0, decision=decision_template, union=noop_result
    )

    noop_axis = axis_expansion_suppression_rate([(noop_sample, BY_ID["buy-under-0002"])])
    flip_axis = axis_expansion_suppression_rate([(flip_sample, BY_ID["buy-under-0002"])])
    assert noop_axis.numerator == 0  # 방향 1: no-op 이면 억제 0건
    assert flip_axis.numerator == 1  # 방향 2: 뒤집는 fake 를 물리면 억제 1건 — 실제로 잡힌다

    # 이 표본은 expectedReask=true 앵커의 decompose True(정답과 일치, 미탐 아님) 표본이다 —
    # union 이 그 표본을 억제(True→False)하면 최종 판정이 정답(reask 기대)과 어긋나 그 표본은
    # union 관점에서 "미탐"이 된다. 그래서 flip 쪽 missRateAfterExpansion 은 base missRate 보다
    # **높아진다**(억제가 늘수록 최종 미탐이 늘어난다는, expansionSuppressionRate 축의 정의상
    # 당연한 결과다) — no-op 쪽은 아무것도 안 바뀌었으니 두 값이 같다.
    noop_miss_after = axis_miss_rate_after_expansion([(noop_sample, BY_ID["buy-under-0002"])])
    flip_miss_after = axis_miss_rate_after_expansion([(flip_sample, BY_ID["buy-under-0002"])])
    base_miss = axis_miss_rate([(noop_sample, BY_ID["buy-under-0002"])])
    assert noop_miss_after.ratio == base_miss.ratio  # no-op: missRateAfterExpansion == missRate
    assert flip_miss_after.ratio is not None and base_miss.ratio is not None
    assert (
        flip_miss_after.ratio > base_miss.ratio
    )  # flip: 억제된 만큼 missRateAfterExpansion 이 커진다


# ─────────── (g) union 단계 LLM 은 SystemPromptOverrideLLM 이 아니다 ───────────


class _RecordingDelegate:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        self.calls.append({"system": system})
        return "{}"


async def test_union_llm_is_not_system_prompt_override_llm() -> None:
    delegate = _RecordingDelegate()
    pacer = await _virtual_pacer()
    decompose_llm = build_probe_llm(delegate, pacer=pacer, system="CANDIDATE PROMPT OVERRIDE")
    union_llm = PacedLLM(delegate, pacer=pacer)  # cli.py 가 실제로 만드는 것과 같은 구성

    assert not isinstance(union_llm, SystemPromptOverrideLLM)

    await decompose_llm.complete(system="ORIGINAL SYSTEM", user="u", tier="fast")
    await union_llm.complete(system="ORIGINAL SYSTEM", user="u", tier="fast")

    assert delegate.calls[0]["system"] == "CANDIDATE PROMPT OVERRIDE"  # decompose 는 갈아끼워진다
    assert delegate.calls[1]["system"] == "ORIGINAL SYSTEM"  # union 은 원본 그대로 통과한다


# ─────────── (h) --union pre-flight — 카탈로그가 비었으면 종료 코드 2 ───────────


def test_union_preflight_rejects_before_any_llm_call(tmp_path, monkeypatch) -> None:
    import evals.underspecified_probe.cli as cli_module
    from evals.underspecified_probe.union import UnionPreflightError

    def _fake_preflight(dsn: str) -> dict:
        raise UnionPreflightError("categories 사전에 embedding 이 채워진 행이 없습니다(fake)")

    monkeypatch.setattr(cli_module, "preflight_catalog", _fake_preflight)
    out = tmp_path / "run"
    exit_code = cli_module.main(["--out", str(out), "--tier", "fast", "--union"])
    assert exit_code == 2
    assert not out.exists()


# ─────────── dry-run --union 은 pg 접근 0 — skipped_union_result 로 표본을 산다 ───────────


def test_skipped_union_result_marks_stage_error_and_no_verdict() -> None:
    result = skipped_union_result("dry-run: union 단계 건너뜀")
    assert not result.ok
    assert result.verdict is None
    assert result.stage_error == "dry-run: union 단계 건너뜀"


# ─────────── expansionGateFiredRate — 실제 unresolved 로 detect_expansion_need 를 부른다 ───────────


async def test_expansion_gate_fired_rate_uses_real_unresolved_not_assumption() -> None:
    """[§2-4] union 단계가 실제 unresolved 로 detect_expansion_need 를 부르는지 — 가정판
    (unresolved=[])과 다른 값이 나와야 이 spy 가 실제로 실행 중임을 보인다."""
    decision = _blank_decision(
        category_queries=[{"raw_category": None, "query": "집들이 선물"}],
        case=3,
    )
    from app.agents.buyer.recommendation.state import CategoryQuery

    decision.category_queries = [CategoryQuery(raw_category=None, query="집들이 선물")]

    async def _unresolved_map_categories(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[], unresolved=["집들이 선물"])

    result = await run_union_stage(
        decision=decision,
        prior=None,
        utterance="집들이 선물로 뭐 사갈까",
        llm=None,
        settings=Settings(),
        judgment_settings=JUDGMENT,
        thread_key="test:gate:1",
        map_categories=_unresolved_map_categories,
    )
    assert result.ok
    assert result.expansion_reason is not None  # 가정판(unresolved=[])이면 항상 None 이었을 것

    axis = axis_expansion_gate_fired_rate(
        [
            (
                _sample("buy-under-0002", verdict=True, decision=decision, union=result),
                BY_ID["buy-under-0002"],
            )
        ]
    )
    assert axis.numerator == 1
    assert axis.denominator == 1


async def test_expansion_gate_fired_rate_is_none_when_needs_expansion_disabled() -> None:
    decision = _blank_decision(case=3)
    from app.agents.buyer.recommendation.state import CategoryQuery

    decision.category_queries = [CategoryQuery(raw_category=None, query="집들이 선물")]

    async def _unresolved_map_categories(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[], unresolved=["집들이 선물"])

    settings = Settings(needs_expansion_enabled=False)
    result = await run_union_stage(
        decision=decision,
        prior=None,
        utterance="집들이 선물로 뭐 사갈까",
        llm=None,
        settings=settings,
        judgment_settings=JUDGMENT,
        thread_key="test:gate:2",
        map_categories=_unresolved_map_categories,
    )
    assert result.ok
    assert result.expansion_reason is None  # 게이트 자체가 안 불렸다(플래그 off)
