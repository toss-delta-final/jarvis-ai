"""축 채점 테스트 — #331. 합성 CellResult/Sample 으로 분자·분모 정의를 검증한다."""

from __future__ import annotations

from evals.category_probe.metrics import (
    build_confusion,
    diagnostics,
    distance_distribution,
    score_all,
    score_inv_agreement,
)
from evals.category_probe.runner import CellResult, HitRecord, Sample
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
                "expectedLegs": [{"accept": ["음향가전 > 이어폰", "브랜드음향 > 이어폰"]}],
                "boundaryNote": "이어폰 계열만 정답, 스피커는 오답이다.",
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
                "cellId": "noc-1",
                "utterance": "드론 추천해줘",
                "sliceId": "notInCatalog",
                "testType": "MFT",
                "expectedLegs": [],
                "absentKeyword": "드론",
                "boundaryNote": "사전에 없다, 빈 배열이 정답.",
            },
            {
                "cellId": "inv-a1",
                "utterance": "빨간 이어폰",
                "sliceId": "single",
                "testType": "INV",
                "invGroupId": "g1",
                "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
                "boundaryNote": "색상 무관 같은 카테고리.",
            },
            {
                "cellId": "inv-a2",
                "utterance": "레드 이어폰",
                "sliceId": "single",
                "testType": "INV",
                "invGroupId": "g1",
                "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
                "boundaryNote": "색상 무관 같은 카테고리.",
            },
        ],
    }
)


def _sample(
    cell_id: str, index: int, legs, *, hits=None, decompose_legs=None, events=None
) -> Sample:
    return Sample(
        cell_id=cell_id,
        sample_index=index,
        legs=legs,
        unresolved=[],
        expansion_leaves=[],
        select_calls=0,
        decompose_legs=decompose_legs
        if decompose_legs is not None
        else [(c, None) for c, _ in legs],
        events=events or [],
        hits=hits or [],
        latency_ms=1,
    )


def test_top1_single_and_topk_inclusion() -> None:
    hit = HitRecord(
        leg_index=0,
        anchor_kind="query",
        anchor_text="이어폰",
        rank=1,
        canonical="음향가전 > 이어폰",
        distance=0.05,
    )
    correct = _sample("single-1", 0, [("음향가전 > 이어폰", "이어폰")], hits=[hit])
    wrong = _sample("single-1", 1, [("스피커 > 블루투스", "이어폰")], hits=[])
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [correct, wrong]
    result.filled = True

    axes = score_all([result], ANCHORS, n=2)
    assert axes["top1Single"].numerator == 1
    assert axes["top1Single"].denominator == 2
    assert axes["topKInclusion"].numerator == 1  # only the hit-bearing sample counts
    assert axes["topKInclusion"].denominator == 2


def test_topk_inclusion_counts_exact_match_with_no_hits() -> None:
    """F1-2 리뷰어 재현: exact match 는 임베딩/검색을 생략해 hits 가 없다 — top1Single=1 인데
    topKInclusion 이 hits 만 보면 0 이 된다. 파이프라인 후보 풀에 정답이 있었다는 뜻이므로
    최종 legs canonical 도 포함 풀에 넣어야 한다."""
    exact = _sample("single-1", 0, [("음향가전 > 이어폰", "이어폰")], hits=[])
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [exact]

    axes = score_all([result], ANCHORS, n=1)
    assert axes["top1Single"].numerator == 1
    assert axes["topKInclusion"].numerator == 1


def test_topk_inclusion_zero_when_decompose_yields_no_legs() -> None:
    empty = _sample("single-1", 0, [], hits=[], decompose_legs=[])
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [empty]
    axes = score_all([result], ANCHORS, n=1)
    assert axes["topKInclusion"].numerator == 0
    assert axes["top1Single"].numerator == 0


def test_multi_coverage_and_exact_set() -> None:
    both = _sample("multi-1", 0, [("음향가전 > 이어폰", "이어폰"), ("캠핑 > 텐트", "텐트")])
    one_only = _sample("multi-1", 1, [("음향가전 > 이어폰", "이어폰")])
    extra = _sample(
        "multi-1",
        2,
        [("음향가전 > 이어폰", "이어폰"), ("캠핑 > 텐트", "텐트"), ("여성의류 > 원피스", "원피스")],
    )
    result = CellResult(
        cell_id="multi-1", utterance_id="multi-1", slice_id="multi", test_type="MFT"
    )
    result.samples = [both, one_only, extra]

    axes = score_all([result], ANCHORS, n=3)
    # coverage: both→2, one_only→1, extra→2 (both legs present even with an extra) = 5 / (3 samples * 2 legs = 6)
    assert axes["multiCoverage"].numerator == 5
    assert axes["multiCoverage"].denominator == 6
    # exact set: only `both` has exactly the 2 expected legs, no extras
    assert axes["multiExactSet"].numerator == 1
    assert axes["multiExactSet"].denominator == 3


def test_none_and_not_in_catalog_no_force() -> None:
    none_ok = _sample("none-1", 0, [])
    none_bad = _sample("none-1", 1, [("음향가전 > 이어폰", "이어폰")])
    noc_ok = _sample("noc-1", 0, [])
    result_none = CellResult(
        cell_id="none-1", utterance_id="none-1", slice_id="none", test_type="MFT"
    )
    result_none.samples = [none_ok, none_bad]
    result_noc = CellResult(
        cell_id="noc-1", utterance_id="noc-1", slice_id="notInCatalog", test_type="MFT"
    )
    result_noc.samples = [noc_ok]

    axes = score_all([result_none, result_noc], ANCHORS, n=2)
    assert axes["noneNoForce"].numerator == 1
    assert axes["noneNoForce"].denominator == 2
    assert axes["notInCatalogNoForce"].numerator == 1
    assert axes["notInCatalogNoForce"].denominator == 1


def test_inv_agreement_true_when_majorities_match() -> None:
    a1 = CellResult(cell_id="inv-a1", utterance_id="inv-a1", slice_id="single", test_type="INV")
    a1.samples = [_sample("inv-a1", 0, [("음향가전 > 이어폰", None)])] * 3
    a2 = CellResult(cell_id="inv-a2", utterance_id="inv-a2", slice_id="single", test_type="INV")
    a2.samples = [_sample("inv-a2", 0, [("음향가전 > 이어폰", None)])] * 3

    axis = score_inv_agreement([a1, a2], ANCHORS)
    assert axis.numerator == 1
    assert axis.denominator == 1


def test_inv_agreement_false_when_majorities_diverge() -> None:
    a1 = CellResult(cell_id="inv-a1", utterance_id="inv-a1", slice_id="single", test_type="INV")
    a1.samples = [_sample("inv-a1", 0, [("음향가전 > 이어폰", None)])]
    a2 = CellResult(cell_id="inv-a2", utterance_id="inv-a2", slice_id="single", test_type="INV")
    a2.samples = [_sample("inv-a2", 0, [("스피커 > 블루투스", None)])]

    axis = score_inv_agreement([a1, a2], ANCHORS)
    assert axis.numerator == 0


def test_inv_agreement_tie_is_non_agreement_not_arbitrary_pass() -> None:
    """F1-3 리뷰어 재현: Counter.most_common(1) 은 4:4 동률에서 삽입 순서로 대표값을 뽑는다 —
    inv-a1 이 진짜로는 무승부(합의 없음)인데 그 임의 대표값이 우연히 inv-a2 의 진짜 다수결과
    같으면 그룹이 '합의'로 잘못 집계된다. 동률은 항상 불합의여야 한다."""
    a1 = CellResult(cell_id="inv-a1", utterance_id="inv-a1", slice_id="single", test_type="INV")
    # 4:4 동률 — "음향가전 > 이어폰"이 먼저 삽입돼 구식 Counter.most_common(1) 로는 그게 이긴다.
    a1.samples = [
        _sample("inv-a1", 0, [("음향가전 > 이어폰", None)]),
        _sample("inv-a1", 1, [("음향가전 > 이어폰", None)]),
        _sample("inv-a1", 2, [("스피커 > 블루투스", None)]),
        _sample("inv-a1", 3, [("스피커 > 블루투스", None)]),
    ]
    a2 = CellResult(cell_id="inv-a2", utterance_id="inv-a2", slice_id="single", test_type="INV")
    # a2 는 "음향가전 > 이어폰"으로 유일한 다수결 — 구식 코드라면 a1 의 임의 대표값과 우연히
    # 일치해 합의로 잘못 집계된다.
    a2.samples = [_sample("inv-a2", 0, [("음향가전 > 이어폰", None)])] * 4

    axis = score_inv_agreement([a1, a2], ANCHORS)
    assert axis.numerator == 0, "4:4 동률인 셀은 불합의로 떨어져야 한다"
    assert axis.denominator == 1


def test_diagnostics_counts_events_and_no_leg() -> None:
    no_leg = _sample("single-1", 0, [], decompose_legs=[])
    with_events = _sample(
        "single-1",
        1,
        [("음향가전 > 이어폰", "이어폰")],
        events=[
            {"event": "category_distance_rejected"},
            {"event": "category_select_null"},
            {"event": "category_selected", "changed": True},
            {"event": "category_mapped"},
        ],
    )
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [no_leg, with_events]
    result.intent_slip_count = 2

    diag = diagnostics([result], ANCHORS)
    assert diag["intentSlipCount"] == 2
    assert diag["noLegCount"] == 1
    assert diag["distanceRejectedCount"] == 1
    assert diag["selectNullCount"] == 1
    assert diag["selectChangedCount"] == 1
    assert diag["exactHitCount"] == 1


def test_confusion_table_only_counts_wrong_samples() -> None:
    wrong = _sample("single-1", 0, [("스피커 > 블루투스", "이어폰")])
    right = _sample("single-1", 1, [("음향가전 > 이어폰", "이어폰")])
    none_leg = _sample("single-1", 2, [])
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [wrong, right, none_leg]

    rows = build_confusion([result], ANCHORS)
    actuals = {row["actual"] for row in rows}
    assert "스피커 > 블루투스" in actuals
    assert "∅" in actuals
    assert sum(row["count"] for row in rows) == 2  # right sample excluded


def test_confusion_table_reports_second_leg_miss_in_multi_cell() -> None:
    """F1-5 리뷰어 재현: multi 셀에서 첫 leg(A)는 정답, 둘째 leg(B)는 통째로 누락됐다.
    구판은 expectedLegs[0] 만 보고 "A 가 커버됐다"만 확인한 뒤 표본 전체를 건너뛰어(continue)
    B 누락이 혼동 표에서 완전히 사라진다(multiCoverage 1/2 인데 confusion 빈 목록). 수정 후엔
    표본마다 기대 leg 별로 미커버를 세어 'B → ∅' 1건이 나와야 한다."""
    a_only = _sample("multi-1", 0, [("음향가전 > 이어폰", "이어폰")])  # B(캠핑 > 텐트) 누락
    result = CellResult(
        cell_id="multi-1", utterance_id="multi-1", slice_id="multi", test_type="MFT"
    )
    result.samples = [a_only]

    rows = build_confusion([result], ANCHORS)
    assert rows, "A 정답 + B 누락은 오답 표본이라 혼동 표에 나와야 한다"
    assert {"expected": "캠핑 > 텐트", "actual": "∅", "count": 1} in rows
    # A(음향가전 > 이어폰)는 커버됐으므로 A 를 "기대→실제" 오답 행으로 다시 세면 안 된다.
    assert not any(row["expected"] == "음향가전 > 이어폰" for row in rows)


def test_distance_distribution_splits_correct_and_incorrect() -> None:
    correct = _sample(
        "single-1",
        0,
        [("음향가전 > 이어폰", "이어폰")],
        events=[{"event": "category_repaired", "canonical": "음향가전 > 이어폰", "distance": 0.1}],
    )
    wrong = _sample(
        "single-1",
        1,
        [("스피커 > 블루투스", "이어폰")],
        events=[
            {"event": "category_fallback_top1", "canonical": "스피커 > 블루투스", "distance": 0.3}
        ],
    )
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [correct, wrong]

    dist = distance_distribution([result], ANCHORS)
    assert dist["correct"]["count"] == 1
    assert dist["correct"]["median"] == 0.1
    assert dist["incorrect"]["count"] == 1
    assert dist["incorrect"]["median"] == 0.3


def test_distance_distribution_uses_adopted_event_not_closer_unwon_anchor() -> None:
    """F1-4 리뷰어 재현: 최종 채택은 query 앵커(0.30)인데 raw 앵커의 hit(0.10)이 더 가까우면
    구판(동명 hit 전체의 min)은 채택하지 않은 raw 값을 낙관적으로 보고한다. 배포 자체 계측
    (category_repaired 이벤트의 distance)을 써야 실제 채택값 0.30 이 나온다."""
    optimistic_raw_hit = HitRecord(
        leg_index=0,
        anchor_kind="raw",
        anchor_text="이어폰 오타",
        rank=1,
        canonical="음향가전 > 이어폰",
        distance=0.10,
    )
    adopted_query_hit = HitRecord(
        leg_index=0,
        anchor_kind="query",
        anchor_text="이어폰",
        rank=1,
        canonical="음향가전 > 이어폰",
        distance=0.30,
    )
    sample = _sample(
        "single-1",
        0,
        [("음향가전 > 이어폰", "이어폰")],
        hits=[optimistic_raw_hit, adopted_query_hit],
        events=[{"event": "category_repaired", "canonical": "음향가전 > 이어폰", "distance": 0.30}],
    )
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [sample]

    dist = distance_distribution([result], ANCHORS)
    assert dist["correct"]["count"] == 1
    assert dist["correct"]["median"] == 0.30, "hit 최솟값(0.10)이 아니라 채택값(0.30)이어야 한다"


def test_distance_distribution_excludes_exact_match_samples() -> None:
    """exact match 는 채택 이벤트(category_repaired 등)가 없다 — 거리 개념이 없으므로 분포에서 제외."""
    exact = _sample(
        "single-1",
        0,
        [("음향가전 > 이어폰", "이어폰")],
        hits=[],
        events=[{"event": "category_mapped"}],
    )
    result = CellResult(
        cell_id="single-1", utterance_id="single-1", slice_id="single", test_type="MFT"
    )
    result.samples = [exact]

    dist = distance_distribution([result], ANCHORS)
    assert dist["correct"]["count"] == 0
    assert dist["incorrect"]["count"] == 0
