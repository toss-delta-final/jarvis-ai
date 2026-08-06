"""v1→v2 이관 스크립트(evals.goldenset.migrate_v1_to_v2)의 예외 마커 부여 로직 단위 테스트.

#333 리뷰 라운드1 F-5-1: 순위 평가 대상 케이스가 narrow-domain/relevant-ratio-exempt 하한을
넘으면 migrate가 notes에 필요한 마커를 자동으로 붙여야 한다.
"""

from __future__ import annotations

from evals.goldenset.migrate_v1_to_v2 import _apply_ranking_exemption_notes, _is_ranking_eligible
from evals.goldenset.schema import has_note_marker


def test_is_ranking_eligible_uses_subset_not_count_comparison() -> None:
    # F-1: 후보 수(2) < 정답 수(3)라도 오답이 섞여 있으면 "전부 정답"이 아니다.
    assert _is_ranking_eligible(["search"], [1, 3, 4], [1, 2]) is True


def test_is_ranking_eligible_false_when_candidates_are_a_subset_of_relevant() -> None:
    assert _is_ranking_eligible(["search"], [1, 2, 3], [1, 2]) is False


def test_apply_ranking_exemption_notes_adds_narrow_domain_when_thin() -> None:
    notes = _apply_ranking_exemption_notes(
        "원래 근거.",
        slices=["search"],
        relevant_product_ids=[1],
        candidate_ids=[1, 2],
        min_ranking_candidates=20,
        max_relevant_ratio=1.0,
    )
    assert has_note_marker(notes, "narrow-domain")
    assert "원래 근거." in notes


def test_apply_ranking_exemption_notes_adds_relevant_ratio_exempt_when_ratio_high() -> None:
    notes = _apply_ranking_exemption_notes(
        "원래 근거.",
        slices=["search"],
        relevant_product_ids=[1],
        candidate_ids=[1, 2, 3, 4],
        min_ranking_candidates=1,
        max_relevant_ratio=0.2,
    )
    assert has_note_marker(notes, "relevant-ratio-exempt")
    assert not has_note_marker(notes, "narrow-domain")


def test_apply_ranking_exemption_notes_combines_both_markers_when_needed() -> None:
    notes = _apply_ranking_exemption_notes(
        "원래 근거.",
        slices=["search"],
        relevant_product_ids=[1],
        candidate_ids=[1, 2],
        min_ranking_candidates=20,
        max_relevant_ratio=0.2,
    )
    assert has_note_marker(notes, "narrow-domain")
    assert has_note_marker(notes, "relevant-ratio-exempt")
    assert "원래 근거." in notes


def test_apply_ranking_exemption_notes_is_idempotent() -> None:
    once = _apply_ranking_exemption_notes(
        "원래 근거.",
        slices=["search"],
        relevant_product_ids=[1],
        candidate_ids=[1, 2],
        min_ranking_candidates=20,
        max_relevant_ratio=0.2,
    )
    twice = _apply_ranking_exemption_notes(
        once,
        slices=["search"],
        relevant_product_ids=[1],
        candidate_ids=[1, 2],
        min_ranking_candidates=20,
        max_relevant_ratio=0.2,
    )
    assert once == twice


def test_apply_ranking_exemption_notes_leaves_non_ranking_eligible_cases_untouched() -> None:
    notes = _apply_ranking_exemption_notes(
        "원래 근거.",
        slices=["failure"],
        relevant_product_ids=[],
        candidate_ids=[1, 2],
        min_ranking_candidates=20,
        max_relevant_ratio=0.2,
    )
    assert notes == "원래 근거."


def test_apply_ranking_exemption_notes_leaves_well_within_thresholds_untouched() -> None:
    notes = _apply_ranking_exemption_notes(
        "원래 근거.",
        slices=["search"],
        relevant_product_ids=[1],
        candidate_ids=list(range(1, 31)),
        min_ranking_candidates=20,
        max_relevant_ratio=0.5,
    )
    assert notes == "원래 근거."
