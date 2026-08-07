"""산출물 리포트 테스트 — #331. 배너·분자/분모 문구·못 채운 셀 표기를 확인한다."""

from __future__ import annotations

import csv
from pathlib import Path

from evals.category_probe.baseline import score_baseline
from evals.category_probe.metrics import (
    build_confusion,
    diagnostics,
    distance_distribution,
    score_all,
)
from evals.category_probe.report import BANNER, build_results, render_report, write_artifacts
from evals.category_probe.runner import CellResult, Sample
from evals.category_probe.schema import AnchorSet

ANCHORS = AnchorSet.model_validate(
    {
        "fixtureVersion": "v1",
        "cells": [
            {
                "cellId": "single-1",
                "utterance": "이어폰 사고 싶어",
                "sliceId": "single",
                "testType": "MFT",
                "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
                "boundaryNote": "이어폰 계열만 정답이다.",
            }
        ],
    }
)


def _sample(index: int, legs) -> Sample:
    return Sample(
        cell_id="single-1",
        sample_index=index,
        legs=legs,
        unresolved=[],
        expansion_leaves=[],
        select_calls=0,
        decompose_legs=[(c, None) for c, _ in legs],
        events=[],
        hits=[],
        latency_ms=1,
    )


def _build(filled: bool, samples) -> dict:
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = samples
    result.attempts = len(samples) + 1
    result.filled = filled
    axes = score_all([result], ANCHORS, n=2)
    baseline = score_baseline([], ANCHORS.model_copy(update={"cells": []}))
    unfilled = (
        []
        if filled
        else [
            {
                "cellId": "single-1",
                "got": len(samples),
                "want": 2,
                "attempts": result.attempts,
                "errorTypes": [],
            }
        ]
    )
    results = build_results(
        cells=[result],
        axes=axes,
        baseline=baseline,
        diagnostics_payload=diagnostics([result], ANCHORS),
        confusion=build_confusion([result], ANCHORS),
        distance_distribution=distance_distribution([result], ANCHORS),
        unfilled=unfilled,
        tier="fast",
        model_config={"fastModel": "test-model", "smartModel": "test-smart"},
        fixture={"name": "anchors.json", "version": "v1", "sha256": "abc"},
        n=2,
        pacer={"waitCount": 0, "maxRpm": 45},
        budget={},
        dry_run=True,
    )
    return results


def test_report_contains_banner_and_axis_definitions() -> None:
    results = _build(
        True, [_sample(0, [("음향가전 > 이어폰", None)]), _sample(1, [("음향가전 > 이어폰", None)])]
    )
    text = render_report(results)
    assert BANNER in text
    assert "top1Single" in text
    assert "최종 legs 에 기대 leg 의 accept 중 하나가 존재" in text  # 분자 정의 동봉
    assert "single(MFT+INV) 30셀 × N" in text  # 분모 정의 동봉(#428 v2: 22 → 30셀)
    assert "이건 골든셋이 아니다" in text


def test_report_marks_unfilled_cells() -> None:
    results = _build(False, [_sample(0, [("음향가전 > 이어폰", None)])])
    text = render_report(results)
    assert "채우지 못한 셀" in text
    assert "single-1" in text
    assert "1/2" in text


def test_write_artifacts_produces_all_files(tmp_path: Path) -> None:
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [_sample(0, [("음향가전 > 이어폰", None)])]
    result.filled = True
    axes = score_all([result], ANCHORS, n=1)
    baseline = score_baseline([], ANCHORS.model_copy(update={"cells": []}))
    results = build_results(
        cells=[result],
        axes=axes,
        baseline=baseline,
        diagnostics_payload=diagnostics([result], ANCHORS),
        confusion=build_confusion([result], ANCHORS),
        distance_distribution=distance_distribution([result], ANCHORS),
        unfilled=[],
        tier="fast",
        model_config={"fastModel": "m"},
        fixture={"name": "anchors.json", "version": "v1", "sha256": "abc"},
        n=1,
        pacer={"waitCount": 0, "maxRpm": 45},
        budget={},
        dry_run=True,
    )
    manifest = {"run": {"runId": "x"}}
    out = tmp_path / "run1"
    write_artifacts(out, results=results, manifest=manifest, cells=[result])
    for name in (
        "results.json",
        "report.md",
        "samples.csv",
        "cells.csv",
        "failures.csv",
        "hits.csv",
        "confusion.csv",
        "run_manifest.json",
    ):
        assert (out / name).exists(), name


def test_samples_csv_has_adopted_distance_margin_anchor_kind_and_drop_reasons(
    tmp_path: Path,
) -> None:
    """F1-8 (패킷 §5 미이행) — 원 패킷이 요구한 채택 distance·margin·anchorKind·dropReason 칸이
    samples.csv 에 없었다. 캡처 events 에서 표본당 뽑아 칸으로 채워야 한다."""
    sample_with_events = Sample(
        cell_id="single-1",
        sample_index=0,
        legs=[("음향가전 > 이어폰", "이어폰")],
        unresolved=[],
        expansion_leaves=[],
        select_calls=1,
        decompose_legs=[("음향가전 > 이어폰", "이어폰")],
        events=[
            {"event": "category_distance_rejected"},
            {
                "event": "category_repaired",
                "canonical": "음향가전 > 이어폰",
                "distance": 0.21,
                "margin": 0.015,
                "anchor_kind": "query",
            },
        ],
        hits=[],
        latency_ms=1,
    )
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [sample_with_events]
    result.filled = True
    axes = score_all([result], ANCHORS, n=1)
    baseline = score_baseline([], ANCHORS.model_copy(update={"cells": []}))
    results = build_results(
        cells=[result],
        axes=axes,
        baseline=baseline,
        diagnostics_payload=diagnostics([result], ANCHORS),
        confusion=build_confusion([result], ANCHORS),
        distance_distribution=distance_distribution([result], ANCHORS),
        unfilled=[],
        tier="fast",
        model_config={"fastModel": "m"},
        fixture={"name": "anchors.json", "version": "v1", "sha256": "abc"},
        n=1,
        pacer={"waitCount": 0, "maxRpm": 45},
        budget={},
        dry_run=True,
    )
    out = tmp_path / "run1"
    write_artifacts(out, results=results, manifest={"run": {"runId": "x"}}, cells=[result])

    rows = list(csv.DictReader((out / "samples.csv").read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 1
    row = rows[0]
    for column in ("adoptedDistance", "adoptedMargin", "adoptedAnchorKind", "dropReasons"):
        assert column in row, f"samples.csv 에 {column} 칸이 없다(패킷 §5 미이행)"
    assert row["adoptedDistance"] == "0.21"
    assert row["adoptedMargin"] == "0.015"
    assert row["adoptedAnchorKind"] == "query"
    assert row["dropReasons"] == "category_distance_rejected"


def test_samples_csv_adopted_columns_are_empty_when_no_events() -> None:
    """이벤트가 없는 표본(exact match)은 빈 칸으로 남아야 한다 — 값 날조 금지."""
    from evals.category_probe.report import _sample_row

    sample = Sample(
        cell_id="single-1",
        sample_index=0,
        legs=[("음향가전 > 이어폰", "이어폰")],
        unresolved=[],
        expansion_leaves=[],
        select_calls=0,
        decompose_legs=[("음향가전 > 이어폰", "이어폰")],
        events=[],
        hits=[],
        latency_ms=1,
    )
    row = _sample_row("single-1", "single", "MFT", sample)
    assert row["adoptedDistance"] == ""
    assert row["adoptedMargin"] == ""
    assert row["adoptedAnchorKind"] == ""
    assert row["dropReasons"] == ""
