"""색상 동의어 파생 스크립트(`scripts/derive_color_synonym_seed.py`) 유닛테스트 (이슈 #258).

DB 를 열지 않는다(CI 엔 pg 가 없다) — `apply_overlay`·`render_*`·`load_review_overlay` 등
순수 함수만 검증한다. `fetch_harvested_rows`(psycopg 직접 질의)는 대상 밖이다.
"""

from __future__ import annotations

import json

import pytest

from scripts.derive_color_synonym_seed import (
    HarvestedRow,
    ReviewOverlay,
    ReviewOverlayEntry,
    SeedDeriveError,
    SeedRow,
    apply_overlay,
    load_review_overlay,
    render_bootstrap_sql,
    render_json,
    row_fingerprint,
)


def _harvested(
    term: str, canonical: str | None, provenance: str = "seed_llm_assignment", doc_count: int = 1
) -> HarvestedRow:
    return HarvestedRow(term, canonical, provenance, doc_count)


def _entry(term: str, canonical: str | None = None, note: str = "") -> ReviewOverlayEntry:
    return ReviewOverlayEntry(term, canonical, note)


# --- apply_overlay: 정상 경로 ------------------------------------------------------------


def test_apply_overlay_marks_approved_terms_with_human_provenance() -> None:
    harvested = [_harvested("블랙", "블랙"), _harvested("검정", "블루")]
    overlay = ReviewOverlay(approved=(_entry("블랙", "블랙"), _entry("검정", "블랙")), rejected=())

    rows = apply_overlay(harvested, overlay)

    by_term = {row.term: row for row in rows}
    assert by_term["블랙"] == SeedRow("블랙", "블랙", "approved", "human", 1)
    assert by_term["검정"] == SeedRow("검정", "블랙", "approved", "human", 1)


def test_apply_overlay_overrides_llm_canonical_with_overlay_value() -> None:
    """곤색 사례 — LLM은 블루로 배정했으나 검수 overlay가 네이비로 정정한다."""
    harvested = [_harvested("네이비", "네이비"), _harvested("곤색", "블루")]
    overlay = ReviewOverlay(
        approved=(_entry("네이비", "네이비"), _entry("곤색", "네이비")), rejected=()
    )

    rows = apply_overlay(harvested, overlay)

    by_term = {row.term: row for row in rows}
    assert by_term["곤색"].canonical == "네이비"
    assert by_term["곤색"].provenance == "human"


def test_apply_overlay_rejected_terms_get_null_canonical() -> None:
    harvested = [_harvested("블랙", "블랙"), _harvested("미상토큰", None)]
    overlay = ReviewOverlay(approved=(_entry("블랙", "블랙"),), rejected=(_entry("미상토큰"),))

    rows = apply_overlay(harvested, overlay)

    rejected_row = next(row for row in rows if row.term == "미상토큰")
    assert rejected_row.status == "rejected"
    assert rejected_row.canonical is None
    assert rejected_row.provenance == "human"


def test_apply_overlay_rows_outside_overlay_keep_pending_review_and_db_values() -> None:
    harvested = [_harvested("블랙", "블랙"), _harvested("다크그린", "그린", doc_count=22)]
    overlay = ReviewOverlay(approved=(_entry("블랙", "블랙"),), rejected=())

    rows = apply_overlay(harvested, overlay)

    pending_row = next(row for row in rows if row.term == "다크그린")
    assert pending_row.status == "pending_review"
    assert pending_row.canonical == "그린"
    assert pending_row.provenance == "seed_llm_assignment"
    assert pending_row.doc_count == 22


def test_apply_overlay_output_is_sorted_by_term() -> None:
    harvested = [_harvested("다", None), _harvested("가", None), _harvested("나", None)]
    overlay = ReviewOverlay(approved=(), rejected=())

    rows = apply_overlay(harvested, overlay)

    assert [row.term for row in rows] == ["가", "나", "다"]


# --- apply_overlay: 검증 실패 경로(§4.4 필수 항목, 변이 시험 대상) -------------------------


def test_apply_overlay_rejects_overlay_term_absent_from_harvest() -> None:
    harvested = [_harvested("블랙", "블랙")]
    overlay = ReviewOverlay(approved=(_entry("없는표기", "블랙"),), rejected=())

    with pytest.raises(SeedDeriveError, match="수확 집합에 없음"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_rejects_overlay_canonical_absent_from_harvest() -> None:
    harvested = [_harvested("검정", "블루")]
    overlay = ReviewOverlay(approved=(_entry("검정", "없는캐노니컬"),), rejected=())

    with pytest.raises(SeedDeriveError, match="canonical이 DB 수확 집합에 없음"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_rejects_orphan_approval() -> None:
    """canonical(네이비)이 harvest 에는 있지만 overlay approved 자신으로는 없으면 고아 승인."""
    harvested = [_harvested("네이비", "네이비"), _harvested("남색", "네이비")]
    overlay = ReviewOverlay(approved=(_entry("남색", "네이비"),), rejected=())

    with pytest.raises(SeedDeriveError, match="고아 승인"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_rejects_two_step_canonical_chain() -> None:
    harvested = [_harvested("A", "A"), _harvested("B", "A"), _harvested("C", "B")]
    overlay = ReviewOverlay(
        approved=(_entry("A", "A"), _entry("B", "A"), _entry("C", "B")), rejected=()
    )

    with pytest.raises(SeedDeriveError, match="2단계 체인/순환 금지"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_rejects_duplicate_term_within_approved() -> None:
    harvested = [_harvested("블랙", "블랙")]
    overlay = ReviewOverlay(approved=(_entry("블랙", "블랙"), _entry("블랙", "블랙")), rejected=())

    with pytest.raises(SeedDeriveError, match="중복"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_rejects_term_in_both_approved_and_rejected() -> None:
    harvested = [_harvested("블랙", "블랙")]
    overlay = ReviewOverlay(approved=(_entry("블랙", "블랙"),), rejected=(_entry("블랙"),))

    with pytest.raises(SeedDeriveError, match="교집합"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_rejects_norm_collision() -> None:
    """DB unique 는 원문 term 기준이라, 공백/대소문자만 다른 두 term 이 공존할 수 있다 —
    `_norm` 기준으로는 충돌이므로 파생 단계에서 잡아야 한다."""
    harvested = [_harvested("Black", "Black"), _harvested(" black ", "Black")]
    overlay = ReviewOverlay(approved=(_entry("Black", "Black"),), rejected=())

    with pytest.raises(SeedDeriveError, match="_norm 기준 term 충돌"):
        apply_overlay(harvested, overlay)


def test_apply_overlay_accepts_human_row_covered_by_overlay() -> None:
    """DB 복원 후 재파생 정상 경로 — human 행이 오버레이(approved)로 덮이면 통과한다."""
    harvested = [
        _harvested("네이비", "네이비", provenance="human"),
        _harvested("곤색", "네이비", provenance="human"),
    ]
    overlay = ReviewOverlay(
        approved=(_entry("네이비", "네이비"), _entry("곤색", "네이비")), rejected=()
    )

    rows = apply_overlay(harvested, overlay)

    by_term = {row.term: row for row in rows}
    assert by_term["곤색"].status == "approved"
    assert by_term["곤색"].provenance == "human"


def test_apply_overlay_rejects_uncovered_human_provenance_row() -> None:
    """DB 복원 경로로 들어온 과거 검수 산출물(provenance=human)을 오버레이가 빠뜨리면
    canonical/provenance 가 되돌려진 적 없는 사람 판단 그대로 pending_review 로 새는 것을
    막아야 한다(#258 리뷰 F1)."""
    harvested = [
        _harvested("네이비", "네이비", provenance="human"),
        _harvested("곤색", "네이비", provenance="human"),
    ]
    overlay = ReviewOverlay(approved=(_entry("네이비", "네이비"),), rejected=())

    with pytest.raises(SeedDeriveError, match="과거 검수 산출물"):
        apply_overlay(harvested, overlay)


# --- load_review_overlay: 형식 검증 -------------------------------------------------------


def test_load_review_overlay_parses_valid_file(tmp_path) -> None:
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(
            {
                "reviewed_at": "2026-08-07",
                "note": "테스트",
                "approved": [{"term": "블랙", "canonical": "블랙", "note": "앵커"}],
                "rejected": [{"term": "미상", "note": "불명"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_review_overlay(path)

    assert overlay.approved == (ReviewOverlayEntry("블랙", "블랙", "앵커"),)
    assert overlay.rejected == (ReviewOverlayEntry("미상", None, "불명"),)


def test_load_review_overlay_requires_canonical_on_approved_entries(tmp_path) -> None:
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"approved": [{"term": "블랙"}], "rejected": []}), encoding="utf-8")

    with pytest.raises(SeedDeriveError, match="canonical"):
        load_review_overlay(path)


def test_load_review_overlay_rejects_non_object_root(tmp_path) -> None:
    path = tmp_path / "review.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(SeedDeriveError, match="객체"):
        load_review_overlay(path)


# --- 렌더링: JSON/SQL 이 apply_overlay 출력과 같은 원천에서 함께 만들어짐 ------------------


def test_render_json_and_bootstrap_sql_share_the_same_row_count() -> None:
    rows = [
        SeedRow("검정", "블랙", "approved", "human", 11),
        SeedRow("블랙", "블랙", "approved", "human", 2358),
    ]

    json_text = render_json(rows)
    sql_text = render_bootstrap_sql(rows)

    parsed = json.loads(json_text)
    assert len(parsed) == 2
    assert sql_text.count("INSERT INTO color_synonyms") == 1
    value_lines = [line for line in sql_text.splitlines() if line.strip().startswith("('")]
    assert len(value_lines) == 2


def test_render_bootstrap_sql_uses_null_literal_for_missing_canonical() -> None:
    rows = [SeedRow("미상토큰", None, "pending_review", "seed_llm_assignment", 3)]

    sql_text = render_bootstrap_sql(rows)

    assert "('미상토큰', NULL, 'pending_review', 'seed_llm_assignment', 3)" in sql_text


def test_render_bootstrap_sql_escapes_apostrophes_in_term_and_canonical() -> None:
    """현재 커밋된 789행에는 작은따옴표가 없어 이 경로가 실측으로는 안 도는데, 셀러 표기는
    자유 텍스트라 다음 수확에 `IT'S` 류가 들어오면 처음 실행된다(#258 리뷰 F5) — 여기서
    합성 입력으로 미리 검증한다."""
    rows = [SeedRow("IT'S PINK", "블랙'X", "pending_review", "seed_llm_assignment", 1)]

    sql_text = render_bootstrap_sql(rows)

    assert "('IT''S PINK', '블랙''X', 'pending_review', 'seed_llm_assignment', 1)" in sql_text
    # 이스케이프 실패 시 나올 원문(따옴표 미중복)은 나오지 않아야 한다.
    assert "'IT'S PINK'" not in sql_text


def test_row_fingerprint_changes_when_status_changes() -> None:
    """지문이 term뿐 아니라 status/canonical/provenance/doc_count 변화도 잡아야 한다."""
    approved = [SeedRow("검정", "블랙", "approved", "human", 11)]
    pending = [SeedRow("검정", "블랙", "pending_review", "seed_llm_assignment", 11)]

    assert row_fingerprint(approved)["sha256"] != row_fingerprint(pending)["sha256"]
