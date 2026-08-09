"""색상 동의어 정본(`db/catalog/seed/color_synonyms.json`) 불변식 + SQL 드리프트 테스트 (이슈 #258).

정본 JSON·사람 검수 오버레이(`color_synonyms_review.json`)·부트스트랩 SQL(`db/catalog/init/
05_color_synonyms_seed.sql`)이 같은 원천에서 나왔는지를 커밋된 산출물 대 산출물로 대조한다.
DB 는 열지 않는다(CI 엔 pg 가 없다) — 파생 스크립트의 검증 실패 경로는
`test_derive_color_synonym_seed.py` 소관.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.pipelines import color_synonyms
from scripts.derive_color_synonym_seed import SeedRow, render_bootstrap_sql, row_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]
JSON_PATH = REPO_ROOT / "db" / "catalog" / "seed" / "color_synonyms.json"
REVIEW_PATH = REPO_ROOT / "db" / "catalog" / "seed" / "color_synonyms_review.json"
SQL_PATH = REPO_ROOT / "db" / "catalog" / "init" / "05_color_synonyms_seed.sql"

# 리터럴로 박는다 — 프로덕션 코드 상수를 그대로 읽어오면 "파일이 스스로와 같다"는 공허한
# 단언이 된다. 이 값은 이 이슈에서 실제로 계산·재현한 codepoint sha256 이다
# (test_category_seed_data.py 관례, §4.7).
EXPECTED_ROW_COUNT = 789
EXPECTED_APPROVED_COUNT = 90
EXPECTED_CODEPOINT_SHA256 = "37d4aeffe1b62e1163b89f17af6535256ac9a972070e8a853834a5df2607ab4e"

_VALID_STATUSES = frozenset({"pending_review", "approved", "rejected"})
_VALID_PROVENANCES = frozenset({"seed_llm_assignment", "batch_embedding_unverified", "human"})


def _load_rows() -> list[dict]:
    return json.loads(JSON_PATH.read_bytes().decode("utf-8"))


def _load_overlay() -> dict:
    return json.loads(REVIEW_PATH.read_bytes().decode("utf-8"))


def _seed_rows_from_json(rows: list[dict]) -> list[SeedRow]:
    return [
        SeedRow(r["term"], r["canonical"], r["status"], r["provenance"], r["doc_count"])
        for r in rows
    ]


def _mapping_from_seed_rows(rows: list[dict]) -> dict[str, list[str]]:
    """정본 JSON에서 `(term, canonical, doc_count)` 튜플만 뽑아 **프로덕션**
    `app.pipelines.color_synonyms.build_synonym_map` 을 그대로 호출한다(테스트 전용 헬퍼 —
    DB 를 열지 않고 §4.7 항목8 을 검증하기 위함). 그룹핑 로직 자체를 테스트가 재구현하면
    `load_synonym_map` 이 다르게 묶여도 이 테스트는 초록으로 남는다(#258 리뷰 F2) — 그래서
    필터링(`status='approved' AND canonical IS NOT NULL`)까지만 테스트가 하고, 정렬·묶음
    규칙은 반드시 프로덕션 함수를 거친다."""
    approved = [r for r in rows if r["status"] == "approved" and r["canonical"] is not None]
    return color_synonyms.build_synonym_map(
        (row["term"], row["canonical"], row["doc_count"]) for row in approved
    )


# --- 1. 형식·enum·필수 키·타입 ---------------------------------------------------------


def test_seed_json_has_expected_row_count() -> None:
    assert len(_load_rows()) == EXPECTED_ROW_COUNT


def test_seed_json_rows_have_required_keys_and_types() -> None:
    for row in _load_rows():
        assert isinstance(row["term"], str) and row["term"].strip()
        assert row["canonical"] is None or isinstance(row["canonical"], str)
        assert row["status"] in _VALID_STATUSES
        assert row["provenance"] in _VALID_PROVENANCES
        assert row["doc_count"] is None or (
            isinstance(row["doc_count"], int) and not isinstance(row["doc_count"], bool)
        )


def test_seed_json_entries_have_no_duplicate_terms() -> None:
    terms = [row["term"] for row in _load_rows()]
    assert len(terms) == len(set(terms))


def test_seed_json_is_codepoint_sorted_by_term() -> None:
    terms = [row["term"] for row in _load_rows()]
    assert terms == sorted(terms)


# --- 2. 행 수 + codepoint sha256 지문 ---------------------------------------------------


def test_seed_json_codepoint_sha256_matches_recorded_fingerprint() -> None:
    rows = _seed_rows_from_json(_load_rows())
    fingerprint = row_fingerprint(rows)
    assert fingerprint["rowCount"] == EXPECTED_ROW_COUNT
    assert fingerprint["sha256"] == EXPECTED_CODEPOINT_SHA256


# --- 3·4. canonical 정합성(자기 포함 집합·앵커 self-canonical·2단계 체인 없음·고아 승인 없음) ---


def test_approved_row_count_matches_recorded_constant() -> None:
    rows = _load_rows()
    approved = [row for row in rows if row["status"] == "approved"]
    assert len(approved) == EXPECTED_APPROVED_COUNT


def test_every_canonical_is_a_term_in_the_dictionary() -> None:
    rows = _load_rows()
    terms = {row["term"] for row in rows}
    canonicals = {row["canonical"] for row in rows if row["canonical"] is not None}
    assert canonicals <= terms


def test_anchors_are_self_canonical() -> None:
    """앵커(term == canonical)는 전부 승인 상태여야 한다."""
    rows = _load_rows()
    anchors = [
        row for row in rows if row["canonical"] == row["term"] and row["status"] == "approved"
    ]
    anchor_terms = {row["term"] for row in anchors}
    expected_anchors = {
        "블랙",
        "화이트",
        "그레이",
        "네이비",
        "블루",
        "브라운",
        "핑크",
        "그린",
        "실버",
        "레드",
        "옐로우",
        "퍼플",
        "골드",
        "오렌지",
        "스카이블루",
        "베이지",
        "차콜",
        "크림",
        "와인",
    }
    assert anchor_terms == expected_anchors


def test_no_two_step_canonical_chain_among_approved_rows() -> None:
    """canonical 의 canonical 은 항상 자기 자신이어야 한다(체인·순환 금지)."""
    rows = _load_rows()
    approved_canonical_of = {
        row["term"]: row["canonical"] for row in rows if row["status"] == "approved"
    }
    for term, canonical in approved_canonical_of.items():
        assert approved_canonical_of.get(canonical) == canonical, (
            f"{term} → {canonical} → {approved_canonical_of.get(canonical)!r} (체인/순환 위반)"
        )


def test_approved_rows_canonical_is_also_approved() -> None:
    """고아 승인 금지 — 승인 행의 canonical 도 반드시 승인 행이어야 한다."""
    rows = _load_rows()
    approved_terms = {row["term"] for row in rows if row["status"] == "approved"}
    for row in rows:
        if row["status"] == "approved":
            assert row["canonical"] in approved_terms


# --- 5. _norm 기준 term 충돌 없음 --------------------------------------------------------


def test_terms_have_no_normalization_collision() -> None:
    rows = _load_rows()
    normalized = [color_synonyms._norm(row["term"]) for row in rows]
    assert len(normalized) == len(set(normalized))


# --- 6. overlay ↔ 정본 JSON 일치 ---------------------------------------------------------


def test_overlay_approved_rows_match_seed_json_exactly() -> None:
    overlay = _load_overlay()
    by_term = {row["term"]: row for row in _load_rows()}
    assert len(overlay["approved"]) == EXPECTED_APPROVED_COUNT
    for entry in overlay["approved"]:
        seed_row = by_term[entry["term"]]
        assert seed_row["status"] == "approved"
        assert seed_row["provenance"] == "human"
        assert seed_row["canonical"] == entry["canonical"]


def test_overlay_konsaek_correction_is_reflected_in_seed_json() -> None:
    """곤색은 LLM이 블루로 배정했으나 검수에서 네이비로 정정한 대표 사례(패킷 §3)."""
    by_term = {row["term"]: row for row in _load_rows()}
    assert by_term["곤색"]["canonical"] == "네이비"
    assert by_term["곤색"]["status"] == "approved"
    assert by_term["곤색"]["provenance"] == "human"


def test_seed_json_rows_not_in_overlay_remain_pending_review() -> None:
    overlay = _load_overlay()
    overlay_terms = {e["term"] for e in overlay["approved"]} | {
        e["term"] for e in overlay["rejected"]
    }
    for row in _load_rows():
        if row["term"] not in overlay_terms:
            assert row["status"] == "pending_review"


# --- 7. 생성물 동기화 — SQL 이 JSON 과 같은 (term, canonical, status, provenance, doc_count) 집합 ---


def test_bootstrap_sql_matches_json_source_byte_for_byte() -> None:
    rows = _seed_rows_from_json(_load_rows())
    regenerated = render_bootstrap_sql(rows)
    committed = SQL_PATH.read_bytes().decode("utf-8")
    assert regenerated == committed


def test_bootstrap_sql_contains_every_row_once() -> None:
    rows = _load_rows()
    committed = SQL_PATH.read_bytes().decode("utf-8")
    assert committed.count("INSERT INTO color_synonyms") >= 1
    assert committed.count("ON CONFLICT (term) DO UPDATE SET") >= 1
    value_lines = [line for line in committed.splitlines() if line.strip().startswith("('")]
    assert len(value_lines) == len(rows)


def test_bootstrap_sql_upsert_refreshes_seed_fields_without_embedding() -> None:
    committed = SQL_PATH.read_bytes().decode("utf-8")
    update_clause = (
        "ON CONFLICT (term) DO UPDATE SET\n"
        "    canonical = EXCLUDED.canonical,\n"
        "    status = EXCLUDED.status,\n"
        "    provenance = EXCLUDED.provenance,\n"
        "    doc_count = EXCLUDED.doc_count;"
    )
    assert committed.count(update_clause) >= 1
    assert "embedding = EXCLUDED.embedding" not in committed
    assert "embedding_model = EXCLUDED.embedding_model" not in committed


# --- 8. 대표 묶음 실측 검증 (expand_color) ------------------------------------------------


def test_expand_color_groups_known_synonyms_into_their_anchor() -> None:
    mapping = _mapping_from_seed_rows(_load_rows())
    assert "네이비" in color_synonyms.expand_color("남색", mapping)
    assert "블랙" in color_synonyms.expand_color("검정", mapping)
    assert "화이트" in color_synonyms.expand_color("흰색", mapping)
    assert "레드" in color_synonyms.expand_color("빨강", mapping)
    assert "네이비" in color_synonyms.expand_color("곤색", mapping)


def test_expand_color_group_order_puts_anchor_first() -> None:
    mapping = _mapping_from_seed_rows(_load_rows())
    assert color_synonyms.expand_color("검정", mapping)[0] == "블랙"
    assert color_synonyms.expand_color("남색", mapping)[0] == "네이비"


def test_expand_color_unregistered_term_returns_itself_only() -> None:
    mapping = _mapping_from_seed_rows(_load_rows())
    assert color_synonyms.expand_color("없는색상어", mapping) == ["없는색상어"]


def test_non_color_sentinels_are_not_approved() -> None:
    by_term = {row["term"]: row for row in _load_rows()}
    for term in ("혼합색상", "기타", "투명"):
        assert by_term[term]["status"] != "approved"


def test_unapproved_composite_terms_are_absent_from_mapping() -> None:
    mapping = _mapping_from_seed_rows(_load_rows())
    assert color_synonyms._norm("다크그린") not in mapping
    assert color_synonyms._norm("화이트+네이비") not in mapping
