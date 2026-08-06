"""오프라인 임계 스윕 테스트 (#344). 합성 hits.csv 로 채택/오버라이드/드롭/트리거 분기와
런타임(`category_mapping.py` §4)과 같은 부등호 경계를 고정한다. API·pg·LLM 콜 없음."""

from __future__ import annotations

import csv
from pathlib import Path

from evals.category_probe.schema import AnchorSet
from evals.category_probe.sweep import (
    HitRow,
    _run_sample_count,
    is_adopted,
    is_select_triggered,
    load_hits,
    pick_winner,
    sweep_one,
    top1_with_margin,
)

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
            },
            {
                "cellId": "multi-1",
                "utterance": "이어폰이랑 텐트 사고 싶어",
                "sliceId": "multi",
                "testType": "MFT",
                "expectedLegs": [
                    {"accept": ["음향가전 > 이어폰"]},
                    {"accept": ["캠핑 > 텐트"]},
                ],
                "boundaryNote": "두 leg 모두 나와야 한다.",
            },
            {
                "cellId": "none-1",
                "utterance": "5만원 이하 아무거나",
                "sliceId": "none",
                "testType": "MFT",
                "expectedLegs": [],
                "boundaryNote": "카테고리 무지정, 빈 배열이 정답.",
            },
            {
                "cellId": "nic-1",
                "utterance": "드론 추천해줘",
                "sliceId": "notInCatalog",
                "testType": "MFT",
                "expectedLegs": [],
                "absentKeyword": "드론",
                "boundaryNote": "카탈로그에 드론 잎이 없다.",
            },
        ],
    }
)

HITS_FIELDS = ["cellId", "sampleIndex", "legIndex", "anchorKind", "rank", "canonical", "distance"]


def _write_hits(tmp_path: Path, rows: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with (run_dir / "hits.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HITS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return run_dir


def _row(cell_id, sample, leg, kind, rank, canonical, distance):
    return {
        "cellId": cell_id,
        "sampleIndex": sample,
        "legIndex": leg,
        "anchorKind": kind,
        "rank": rank,
        "canonical": canonical,
        "distance": distance,
    }


# ── 순수 함수 단위 ────────────────────────────────────────────────────────────


def test_top1_with_margin_no_hits_is_none() -> None:
    assert top1_with_margin([]) is None


def test_top1_with_margin_single_hit_has_no_margin() -> None:
    canonical, d1, margin = top1_with_margin([(1, "음향가전 > 이어폰", 0.21)])
    assert canonical == "음향가전 > 이어폰"
    assert d1 == 0.21
    assert margin is None


def test_top1_with_margin_rounds_to_4_places() -> None:
    _canonical, d1, margin = top1_with_margin([(1, "a", 0.123456), (2, "b", 0.20000)])
    assert d1 == 0.1235
    assert margin == 0.0765


def test_pick_winner_prefers_query_over_raw() -> None:
    kinds = {
        "raw": [(1, "raw-canon", 0.10)],
        "query": [(1, "query-canon", 0.30)],
    }
    canonical, d1, _margin = pick_winner(kinds)
    assert canonical == "query-canon"
    assert (
        d1 == 0.30
    )  # query 우선은 거리 비교가 아니다(§4.3.1) — raw 가 더 가까워도 query 가 이긴다


def test_pick_winner_falls_back_to_raw_when_no_query_hits() -> None:
    kinds = {"raw": [(1, "raw-canon", 0.15)], "query": []}
    canonical, d1, _margin = pick_winner(kinds)
    assert canonical == "raw-canon"
    assert d1 == 0.15


def test_is_adopted_within_dmax() -> None:
    assert is_adopted(0.20, None, dmax=0.26, override_margin=0.035) is True


def test_is_adopted_boundary_d1_equals_dmax_is_adopted() -> None:
    # category_mapping.py 는 `picked[1] > distance_max` 를 초과일 때만 드롭으로 판정한다 —
    # 동일값(<=)은 채택이다. 부등호 방향이 뒤집히면 스윕이 런타임과 어긋난다.
    assert is_adopted(0.26, None, dmax=0.26, override_margin=0.035) is True


def test_is_adopted_over_dmax_without_margin_is_dropped() -> None:
    assert is_adopted(0.27, None, dmax=0.26, override_margin=0.035) is False


def test_is_adopted_over_dmax_with_small_margin_is_dropped() -> None:
    assert is_adopted(0.27, 0.03, dmax=0.26, override_margin=0.035) is False


def test_is_adopted_boundary_margin_equals_override_is_adopted() -> None:
    # `picked[2] >= override_margin` — 동일값은 채택(구제)이다.
    assert is_adopted(0.30, 0.035, dmax=0.26, override_margin=0.035) is True


def test_is_adopted_over_dmax_with_large_margin_is_rescued() -> None:
    assert is_adopted(0.30, 0.05, dmax=0.26, override_margin=0.035) is True


def test_is_select_triggered_requires_margin_within_dmax() -> None:
    assert is_select_triggered(0.20, 0.01, dmax=0.26, select_margin=0.02) is True
    assert is_select_triggered(0.20, 0.03, dmax=0.26, select_margin=0.02) is False  # 마진 큼(확신)
    assert is_select_triggered(0.30, 0.01, dmax=0.26, select_margin=0.02) is False  # 거리컷 밖
    assert is_select_triggered(0.20, None, dmax=0.26, select_margin=0.02) is False  # 히트 1건


# ── sweep_one 통합 — 슬라이스별 집계 ──────────────────────────────────────────


def test_single_adopted_correct(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [_row("single-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.20)],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_total == 1
    assert result.single_adopted_correct == 1
    assert result.single_adopted_wrong == 0
    assert result.single_dropped == 0


def test_single_adopted_wrong_canonical(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [_row("single-1", 0, 0, "query", 1, "가전 > 청소기", 0.20)],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_adopted_wrong == 1
    assert result.single_adopted_correct == 0


def test_single_dropped_when_over_dmax_and_margin_thin(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [
            _row("single-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.30),
            _row("single-1", 0, 0, "query", 2, "가전 > 청소기", 0.29),
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_dropped == 1
    assert result.single_dropped_cells == {"single-1": 1}
    assert result.single_adopted_correct == 0


def test_single_override_rescues_far_correct_pick(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [
            _row("single-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.30),
            _row("single-1", 0, 0, "query", 2, "가전 > 청소기", 0.35),
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_adopted_correct == 1
    assert result.single_dropped == 0


def test_single_raw_fallback_when_query_has_no_hits(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [_row("single-1", 0, 0, "raw", 1, "음향가전 > 이어폰", 0.10)],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_adopted_correct == 1


def test_single_no_signal_sample_counted_separately(tmp_path: Path) -> None:
    # sample 1 은 hits.csv 에 행이 아예 없다(신호 0건) — 드롭(거리 초과)과 구분돼야 한다.
    run_dir = _write_hits(
        tmp_path,
        [_row("single-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.20)],
    )
    # sample 1 이 존재했다는 것만 다른 셀의 행으로 알린다(run 전체 N 추론용).
    rows = load_hits(run_dir) + [
        HitRow(
            cell_id="none-1",
            sample_index=1,
            leg_index=0,
            anchor_kind="query",
            rank=1,
            canonical="상관없음 > 상관없음",
            distance=0.99,
        )
    ]
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_total == 2  # N=2 로 추론(전 셀 공통)
    assert result.single_no_hit == 1  # sample 1 의 single-1 은 신호가 없다
    assert result.single_adopted_correct == 1


def test_run_sample_count_uses_max_plus_one_not_distinct_count() -> None:
    # [A-2, #344 라운드 2] `_run_sample_count` 를 `max(sample_index)+1` 대신
    # `len({sample_index})` 로 바꿔도(같은 뜻처럼 보인다) 기존 23개 테스트는 전부 통과했다 —
    # 기존 유일한 회귀 테스트(`test_single_no_signal_sample_counted_separately`)가 sampleIndex
    # {0,1} 연속이라 두 식의 값이 같기 때문이다. 여기서는 sampleIndex 가 **비연속**({0,2}, 1이
    # 어떤 셀에도 없다)이게 만들어 두 식이 갈리게 한다.
    rows = [
        HitRow(
            cell_id="c",
            sample_index=0,
            leg_index=0,
            anchor_kind="query",
            rank=1,
            canonical="x",
            distance=0.1,
        ),
        HitRow(
            cell_id="c",
            sample_index=2,
            leg_index=0,
            anchor_kind="query",
            rank=1,
            canonical="x",
            distance=0.1,
        ),
    ]
    assert _run_sample_count(rows) == 3  # max(sample_index)+1 — sample 1 도 시도된 것으로 센다
    assert _run_sample_count(rows) != len({row.sample_index for row in rows})  # == 2, 다른 값


def test_single_total_counts_a_sample_missing_from_every_cell(tmp_path: Path) -> None:
    # [A-2, #344 라운드 2] 위 단위 테스트를 `sweep_one` 통합 레벨로도 고정한다 — 이 표본이 빠지면
    # 분모(single_total)뿐 아니라 sample_index=2 의 정답 표본까지 조용히 통째로 사라진다
    # (`range(len(distinct))` 는 2 를 아예 순회하지 않는다).
    run_dir = _write_hits(
        tmp_path,
        [
            _row("single-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.20),
            # sample_index=1 은 어떤 셀에서도 히트가 없다(비연속) — 그래도 시도는 됐다.
            _row("single-1", 2, 0, "query", 1, "음향가전 > 이어폰", 0.20),
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.single_total == 3  # max(sampleIndex)+1 = 3
    assert result.single_adopted_correct == 2  # sample 0 과 sample 2 둘 다 정답
    assert result.single_no_hit == 1  # sample 1 은 신호 0건


def test_select_trigger_counted_when_ambiguous(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [
            _row("single-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.20),
            _row("single-1", 0, 0, "query", 2, "가전 > 청소기", 0.21),  # margin 0.01 <= 0.02
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.select_trigger_total == 1


def test_none_slice_forced_when_adopted(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [_row("none-1", 0, 0, "query", 1, "가전 > 청소기", 0.10)],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.none_total == 1
    assert result.none_forced == 1


def test_not_in_catalog_not_forced_when_dropped(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [
            _row("nic-1", 0, 0, "query", 1, "수예 > 재료", 0.30),
            _row("nic-1", 0, 0, "query", 2, "완구 > 조립", 0.31),
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.nic_total == 1
    assert result.nic_forced == 0


def test_multi_leg_coverage_counts_each_expected_leg(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [
            _row("multi-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.20),
            _row("multi-1", 0, 1, "query", 1, "캠핑 > 텐트", 0.21),
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.multi_leg_total == 2
    assert result.multi_leg_covered == 2


def test_multi_leg_coverage_partial_when_one_leg_dropped(tmp_path: Path) -> None:
    run_dir = _write_hits(
        tmp_path,
        [
            _row("multi-1", 0, 0, "query", 1, "음향가전 > 이어폰", 0.20),
            _row("multi-1", 0, 1, "query", 1, "가전 > 청소기", 0.31),  # 거리컷 초과, margin 없음
        ],
    )
    rows = load_hits(run_dir)
    result = sweep_one(rows, ANCHORS, dmax=0.26, override_margin=0.035, select_margin=0.02)
    assert result.multi_leg_total == 2
    assert result.multi_leg_covered == 1
