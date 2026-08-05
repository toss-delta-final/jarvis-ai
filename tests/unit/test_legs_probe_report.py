"""report.py 산출 — 섹션·행 단위 술어로 report.md 내용을 검증한다 (#332, R3-1).

문자열 전체 스냅샷은 고정하지 않는다(사소한 문구 수정마다 깨진다) — 대신 R1 에서 실제로 잡힌
"빈 promptExample 표"(리터럴 자리표시 문자열)와 R2-3 의 "제외판·포함판 정의 문자열 동일"
결함의 회귀 가드로, 섹션 존재·행 실값·정의 문자열 차이·못 채운 셀 노출·exploratory 라벨을
의미 단위 술어로 확인한다. `render_report` 는 이미 순수 함수(`results: dict -> str`)라 리팩터링
없이 직접 호출할 수 있다.
"""

from __future__ import annotations

from app.agents.buyer.recommendation.state import CategoryQuery
from evals.legs_probe.loader import load_anchor_set
from evals.legs_probe.metrics import (
    compute_baseline,
    diagnostics,
    pair_diagnostics,
    prompt_example_diagnostics,
    score_all,
)
from evals.legs_probe.report import build_results, render_report
from evals.legs_probe.runner import CellResult, Sample, unfilled_cells

ANCHORS = load_anchor_set()
BY_ID = {anchor.case_id: anchor for anchor in ANCHORS.utterances}
N = 8

REQUIRED_HEADERS = (
    "## Primary confirmatory 지표",
    "## 축 전체",
    "## 슬라이스별 병기",
    "## LLM vs baseline",
    "## pair 진단",
    "## promptExample 앵커",
    "## 진단 (합불 아님)",
    "## 셀별 intent 분포",
    "## 채우지 못한 셀",
)


def _sample(case_id: str, index: int, **overrides: object) -> Sample:
    base = {
        "case_id": case_id,
        "sample_index": index,
        "intent": "recommend",
        "case": 3,
        "category_queries": (),
        "buy_all": False,
        "total_budget": None,
        "latency_ms": 0,
    }
    base.update(overrides)
    return Sample(**base)  # type: ignore[arg-type]


def _cell_result(anchor_id: str, samples: list[Sample], *, n: int = N) -> CellResult:
    anchor = BY_ID[anchor_id]
    return CellResult(
        cell_id=anchor.case_id,
        case_id=anchor.case_id,
        slice=anchor.slice,
        samples=samples,
        attempts=len(samples),
        filled=len(samples) == n,
    )


def _build(cell_results: list[CellResult], *, n: int = N) -> tuple[dict, str]:
    scored = score_all(cell_results, ANCHORS)
    diag = diagnostics(cell_results, ANCHORS)
    baseline = compute_baseline(ANCHORS)
    pairs = pair_diagnostics(cell_results, ANCHORS)
    prompt_examples = prompt_example_diagnostics(cell_results, ANCHORS)
    unfilled = unfilled_cells(cell_results, n=n)
    results = build_results(
        cells=cell_results,
        scored=scored,
        diagnostics_payload=diag,
        baseline=baseline,
        pair_rows=pairs,
        prompt_example_rows=prompt_examples,
        unfilled=unfilled,
        prompt={"sha12": "deadbeef1234", "source": "test:synthetic", "sha256": "a" * 64},
        tier="fast",
        model_config={"provider": "dry-run", "fastModel": "scripted", "smartModel": "scripted"},
        fixture={"name": "anchors.json", "version": "legs-anchors-v1", "sha256": "b" * 64},
        n=n,
        category_fanout_max=5,
        repurchase_max=5,
        pacer={"maxRpm": 45, "waitCount": 0},
        budget={"budgetExceeded": False},
        dry_run=True,
    )
    return results, render_report(results)


def _scenario() -> list[CellResult]:
    """[R3-1] 최소 시나리오 — 1개는 promptExample 앵커(실값 검증)로, 둘 다 표본이 N 미만이라
    "못 채운 셀"에 나오고, 전체 표본 수가 적어 confirmatory 축이 슬라이스 임계(40) 밑으로
    떨어져 exploratory 라벨을 유도한다.
    """
    # legs-situ-0009 (promptExample=True) — recommend 2건(그중 1건 과소전개) + general 1건.
    prompt_example_samples = [
        _sample("legs-situ-0009", 0, case=3, category_queries=(CategoryQuery(query="a"),)),
        _sample(
            "legs-situ-0009",
            1,
            case=3,
            category_queries=(CategoryQuery(query="a"), CategoryQuery(query="b")),
        ),
        _sample("legs-situ-0009", 2, intent="general", case=2, category_queries=()),
    ]
    # legs-situ-0001 (promptExample=False) — recommend 1건, 과소전개.
    normal_samples = [
        _sample("legs-situ-0001", 0, case=3, category_queries=(CategoryQuery(query="a"),)),
    ]
    return [
        _cell_result("legs-situ-0009", prompt_example_samples),
        _cell_result("legs-situ-0001", normal_samples),
    ]


def test_all_required_sections_are_present() -> None:
    _, report = _build(_scenario())
    for header in REQUIRED_HEADERS:
        assert header in report, f"'{header}' 섹션이 report.md 에 없다"


def test_prompt_example_row_has_real_values_not_placeholders() -> None:
    """[R1 회귀 가드] 슬라이스 열이 '-', 값 열이 '(samples.csv 참조)' 리터럴이면 실패한다."""
    _, report = _build(_scenario())
    assert "(samples.csv 참조)" not in report
    # legs-situ-0009: recommend 표본 2건, 그중 case==3 ∧ legs<=1 은 1건, 두 leg 모두 어떤
    # coverageGroups synonym 과도 안 맞아 평균 legCoverage 는 0.0%.
    assert "| `legs-situ-0009` | situational | 2 | 1 | 0.0% |" in report


def test_prompt_example_row_slice_column_is_never_a_bare_dash() -> None:
    lines = _build(_scenario())[1].splitlines()
    table_start = next(i for i, line in enumerate(lines) if line.startswith("## promptExample"))
    table_end = next(
        i for i, line in enumerate(lines[table_start:], table_start) if line.startswith("## 진단")
    )
    rows = [
        line
        for line in lines[table_start:table_end]
        if line.startswith("| `") and "caseId" not in line
    ]
    assert rows, "promptExample 표에 앵커 행이 하나도 없다"
    for row in rows:
        columns = [cell.strip() for cell in row.strip("|").split("|")]
        assert columns[1] != "-", f"슬라이스 열이 자리표시 '-' 다: {row}"


def test_axis_definitions_are_printed_and_differ_between_excluded_and_included() -> None:
    """[R2-3 회귀 가드] legCoverage·case3UnderExpansionRate 의 제외판·포함판이 서로 다른
    분모 정의 문자열을 인쇄한다."""
    _, report = _build(_scenario())
    lines = report.splitlines()

    def _row_for(axis_id: str) -> str:
        prefix = f"| `{axis_id}` "
        return next(line for line in lines if line.startswith(prefix))

    leg_coverage_excluded = _row_for("legCoverage")
    leg_coverage_included = _row_for("legCoverageWithPromptExamples")
    assert "(promptExample 제외)" in leg_coverage_excluded
    assert "(promptExample 포함)" in leg_coverage_included
    assert leg_coverage_excluded != leg_coverage_included

    case3_excluded = _row_for("case3UnderExpansionRate")
    case3_included = _row_for("case3UnderExpansionRateWithPromptExamples")
    assert "(promptExample 제외)" in case3_excluded
    assert "(promptExample 포함)" in case3_included
    assert case3_excluded != case3_included


def test_unfilled_cells_appear_in_the_report_when_present() -> None:
    results, report = _build(_scenario())
    assert results["unfilledCells"]  # 둘 다 표본 < N=8 이라 못 채운 셀이다
    assert "## 채우지 못한 셀\n\n(없음)" not in report
    assert "`legs-situ-0009`" in report
    assert "`legs-situ-0001`" in report


def test_unfilled_cells_report_got_matches_actual_sample_count() -> None:
    results, report = _build(_scenario())
    unfilled_by_case_id = {row["cellId"]: row for row in results["unfilledCells"]}
    assert unfilled_by_case_id["legs-situ-0009"]["got"] == 3
    assert unfilled_by_case_id["legs-situ-0009"]["want"] == N
    assert unfilled_by_case_id["legs-situ-0001"]["got"] == 1
    assert f"| `legs-situ-0009` | 3/{N} |" in report
    assert f"| `legs-situ-0001` | 1/{N} |" in report


def test_below_sample_threshold_axes_get_the_exploratory_annotation() -> None:
    """[§3] 슬라이스 표본 임계(기본 40) 미만이면 report 가 스스로 exploratory 라벨을 단다 —
    이 시나리오는 confirmatory 축 분모가 1~2 표본뿐이라 반드시 임계 아래다."""
    results, report = _build(_scenario())
    assert results["axes"]["case3UnderExpansionRate"]["denominator"] < 40
    assert results["axes"]["legCoverage"]["denominator"] < 40
    lines = report.splitlines()
    case3_row = next(line for line in lines if line.startswith("| `case3UnderExpansionRate` "))
    leg_coverage_row = next(line for line in lines if line.startswith("| `legCoverage` "))
    assert "(N<40 → exploratory)" in case3_row
    assert "(N<40 → exploratory)" in leg_coverage_row


def test_empty_scenario_still_renders_without_error() -> None:
    """빈 입력(표본 0)에서도 예외 없이 렌더링되고 '없음' 플레이스홀더가 정상적으로 나온다."""
    results, report = _build([])
    assert results["unfilledCells"] == []
    assert "## 채우지 못한 셀\n\n(없음)" in report
