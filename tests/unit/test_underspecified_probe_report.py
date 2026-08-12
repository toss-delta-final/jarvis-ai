"""산출물 6종 — dry-run 결정론·샘플 CSV 컬럼 (#380)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from evals.underspecified_probe.cli import main
from evals.underspecified_probe.report import (
    _axis_row,
    _sample_size_badge,
    normalized_artifact_bytes,
)

ARTIFACT_NAMES = {
    "results.json",
    "run_manifest.json",
    "report.md",
    "samples.csv",
    "cells.csv",
    "failures.csv",
}


def _run(out: Path, *extra: str) -> int:
    return main(["--out", str(out), "--dry-run", "--n", "3", *extra])


def test_dry_run_writes_every_artifact_and_exits_zero(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    assert {path.name for path in out.iterdir()} == ARTIFACT_NAMES
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert results["cellCount"] == 30
    assert results["unfilledCells"] == []
    assert results["dryRun"] is True
    assert results["judgment"] == {"underspecifiedReaskEnabled": True}


def test_dry_run_artifacts_match_across_two_runs_after_normalizing_intrinsic_nondeterminism(
    tmp_path: Path,
) -> None:
    """[§D14 항목 9, F-4] **바이트 동일이 아니다** — `normalized_artifact_bytes` 가 실행마다
    본질적으로 달라지는 필드(지연 `latencyMs`, 실행 메타 `run`·`dirty`·`timestamp` 등,
    `report.VOLATILE_JSON_KEYS`/`VOLATILE_CSV_COLUMNS` 참조)를 제거한 **뒤**의 산출물이 두
    dry-run 사이에 동일한지를 본다. 무상태 fake 의 결정론(§D7)은 정규화 이후 값에서 확인된다."""
    first, second = tmp_path / "a", tmp_path / "b"
    assert _run(first) == 0
    assert _run(second) == 0
    assert normalized_artifact_bytes(first) == normalized_artifact_bytes(second)


def test_samples_csv_has_the_cause_axis_and_intent_columns(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    with (out / "samples.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 30 * 3
    assert set(rows[0]) == {
        "caseId",
        "n",
        "slice",
        "intent",
        "case",
        "semanticQueryIsFallback",
        "semanticQuery",
        "verdict",
        "expectedReask",
        "outcome",
        "causeAxes",
        "blockingAxes",
        "attrConditionAxes",
        "attrConditionsSuppressedAxes",
        "expansionReason",
        "latencyMs",
    }
    outcomes = {row["outcome"] for row in rows}
    assert outcomes <= {"hit", "miss", "falseAlarm", "correctReject"}
    # [§D7] dry-run fake 는 미탐 1건·오탐 1건 이상을 반드시 낸다 — miss/falseAlarm 배관이
    # 실제로 태워지는지 이 산출물로 고정한다.
    assert "miss" in outcomes
    assert "falseAlarm" in outcomes
    # [F-1] dry-run fake 는 intent="recommend" 만 내므로(fakes.py `_envelope`), 이 산출물에서는
    # intent 필터가 아무것도 걸러내지 않는다 — 실 런에서만 nonRecommendIntentCount 가 채워진다.
    assert {row["intent"] for row in rows} == {"recommend"}


def test_report_md_contains_the_invariant_baseline_and_cause_axis_sections(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    report_text = (out / "report.md").read_text(encoding="utf-8")
    assert "flagOffInvariant" in report_text
    assert "priorGateInvariant" in report_text
    assert "LLM vs baseline" in report_text
    assert "원인 축 분해" in report_text
    # [F-2] blockingAxes 조합별 실측 표 — 서술이 아니라 산출물에서 바로 읽혀야 한다.
    assert "blockingAxes 조합" in report_text
    # [F-1] 비-recommend 포함판 축이 표에 실린다.
    assert "missRateWithNonRecommendIntent" in report_text
    assert "falseAlarmRateWithNonRecommendIntent" in report_text
    # [F-3] measurementScope 문구가 detect_expansion_need 호출을 인정한다(runner.py 와 모순 없음).
    assert "detect_expansion_need" in report_text


# ─────────── F-5 — 분모 0 은 "표본 적음"이 아니라 "해당 없음" ───────────


def _axis(*, numerator: float, denominator: int, below_threshold: bool) -> dict:
    return {
        "axisId": "x",
        "title": "테스트 축",
        "numerator": numerator,
        "denominator": denominator,
        "ratio": (numerator / denominator) if denominator else None,
        "ci95": None,
        "nature": "exploratory",
        "belowSampleThreshold": below_threshold,
        "definition": {"numerator": "n", "denominator": "d"},
    }


def test_sample_size_badge_shows_not_applicable_when_denominator_is_zero() -> None:
    zero = _axis(numerator=0, denominator=0, below_threshold=True)
    assert _sample_size_badge(zero) == " (해당 없음)"


def test_sample_size_badge_shows_exploratory_when_denominator_is_positive_but_small() -> None:
    small = _axis(numerator=1, denominator=32, below_threshold=True)
    assert _sample_size_badge(small) == " (exploratory: N<40)"


def test_sample_size_badge_is_empty_when_denominator_meets_threshold() -> None:
    full = _axis(numerator=10, denominator=40, below_threshold=False)
    assert _sample_size_badge(full) == ""


def test_axis_row_marks_not_applicable_instead_of_exploratory_when_denominator_is_zero() -> None:
    zero = _axis(numerator=0, denominator=0, below_threshold=True)
    row = _axis_row("missRate", zero)
    assert "해당 없음" in row
    assert "N<40 → exploratory" not in row


def test_dry_run_what_axis_slice_of_miss_rate_shows_not_applicable_not_exploratory(
    tmp_path: Path,
) -> None:
    """[F-5] `missRate` 는 `expectedReask=true` 앵커만 분모에 넣는데 `what_axis` 슬라이스는
    전부 `expectedReask=false` 다 — 그 칸은 "표본이 적어서"가 아니라 구조적으로 잴 수 없다."""
    out = tmp_path / "run"
    assert _run(out) == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    what_axis_miss_rate = results["slices"]["missRate"]["what_axis"]
    assert what_axis_miss_rate["denominator"] == 0
    report_text = (out / "report.md").read_text(encoding="utf-8")
    # missRate 슬라이스 표에서 what_axis 행을 찾아 "해당 없음"인지 확인한다.
    what_axis_row = next(
        line for line in report_text.splitlines() if line.startswith("| what_axis |")
    )
    assert "해당 없음" in what_axis_row
