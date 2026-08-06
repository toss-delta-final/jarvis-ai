"""MariaDB `category` 덤프 파서 테스트 (이슈 #401).

합성 픽스처만 사용한다 — repo 밖 덤프 파일(`~/inte-final/_sql`)에 의존하지 않는다(CI 에 없다).
"""

from __future__ import annotations

import pytest

from scripts.derive_category_seed import (
    DumpParseError,
    codepoint_fingerprint,
    extract_leaves,
    parse_category_dump,
)

_HEADER = "-- category: 3 rows\n"
_TAIL = (
    "\nON DUPLICATE KEY UPDATE `parent_id`=VALUES(`parent_id`),"
    "`name`=VALUES(`name`),`attribute_schema`=VALUES(`attribute_schema`);\n"
)
_MARK = "INSERT INTO `category` (`id`,`parent_id`,`name`,`attribute_schema`) VALUES\n"


def _dump(rows_sql: str, *, header: str = _HEADER) -> str:
    return f"{header}{_MARK}{rows_sql}{_TAIL}"


def test_splits_domain_and_leaf_by_parent_id() -> None:
    text = _dump(
        "(1,NULL,'가전',NULL),\n"
        "(2,1,'가전 > TV','[{\"a\":1}]'),\n"
        "(3,1,'가전 > 냉장고','[{\"a\":2}]')"
    )
    rows = parse_category_dump(text)
    assert [r.row_id for r in rows] == ["1", "2", "3"]
    domain = [r for r in rows if r.parent_id is None]
    leaf = [r for r in rows if r.parent_id is not None]
    assert [r.name for r in domain] == ["가전"]
    assert [r.name for r in leaf] == ["가전 > TV", "가전 > 냉장고"]


def test_unescapes_mysql_single_quote_escape() -> None:
    text = _dump(
        "(1,NULL,'가전',NULL),\n(2,1,'가전 > 세탁기\\'미니\\'','[{}]')",
        header="-- category: 2 rows\n",
    )
    leaves = extract_leaves(text)
    assert leaves == ["가전 > 세탁기'미니'"]


def test_unescapes_backspace_and_substitute_control_chars() -> None:
    """`\\b`→백스페이스(0x08), `\\Z`→SUB(0x1A) — 표에서 빠지면 문자 그대로("b"·"Z")로 잘못 풀린다
    (#401 라운드 2 리뷰 F3, MySQL 문자열 리터럴 이스케이프 규약).
    """
    text = _dump(
        "(1,NULL,'가전',NULL),\n(2,1,'가전 > TV\\b\\Z','[{}]')",
        header="-- category: 2 rows\n",
    )
    leaves = extract_leaves(text)
    assert leaves == ["가전 > TV\b\x1a"]


def test_preserves_backslash_before_percent_and_underscore() -> None:
    """`\\%`·`\\_` 는 LIKE 전용 이스케이프라 문자열 리터럴에서는 MySQL 이 백슬래시를 보존한다 —
    다른 미등록 이스케이프처럼 벗겨내면(백슬래시만 버리면) 회귀다(#401 라운드 2 리뷰 F3).
    """
    text = _dump(
        "(1,NULL,'가전',NULL),\n(2,1,'가전 > 50\\%할인\\_특가','[{}]')",
        header="-- category: 2 rows\n",
    )
    leaves = extract_leaves(text)
    assert leaves == ["가전 > 50\\%할인\\_특가"]


def test_string_value_may_contain_literal_newline_and_paren_comma_paren() -> None:
    """값 안의 개행·`),(` 가 튜플 경계로 오인되면 안 된다 — attribute_schema JSON 이 실제로 이런 값을 담는다."""
    attribute_schema = '[{"source_hint": "line1\nline2, 예: A),(B 처럼 보이는 문자열"}]'
    text = _dump(
        f"(1,NULL,'가전',NULL),\n(2,1,'가전 > TV','{attribute_schema}')",
        header="-- category: 2 rows\n",
    )
    leaves = extract_leaves(text)
    assert leaves == ["가전 > TV"]


def test_multiple_insert_statements_are_all_parsed() -> None:
    """실제 덤프는 배치별로 여러 `INSERT ... VALUES ... ON DUPLICATE KEY UPDATE ...;` 블록을 낸다."""
    text = (
        "-- category: 4 rows\n"
        f"{_MARK}(1,NULL,'가전',NULL),\n(2,1,'가전 > TV','[{{}}]'){_TAIL}"
        f"{_MARK}(3,NULL,'식품',NULL),\n(4,3,'식품 > 라면','[{{}}]'){_TAIL}"
    )
    leaves = extract_leaves(text)
    assert leaves == ["가전 > TV", "식품 > 라면"]


def test_duplicate_leaf_name_is_rejected() -> None:
    text = _dump(
        "(1,NULL,'가전',NULL),\n(2,1,'가전 > TV','[{}]'),\n(3,1,'가전 > TV','[{}]')",
        header="-- category: 3 rows\n",
    )
    with pytest.raises(DumpParseError, match="중복"):
        extract_leaves(text)


def test_header_row_count_mismatch_is_rejected() -> None:
    text = _dump(
        "(1,NULL,'가전',NULL),\n(2,1,'가전 > TV','[{}]')",
        header="-- category: 5 rows\n",  # 실제론 2행
    )
    with pytest.raises(DumpParseError, match="헤더"):
        extract_leaves(text)


def test_missing_insert_marker_is_rejected() -> None:
    with pytest.raises(DumpParseError):
        parse_category_dump("SELECT 1;\n")


def test_extract_leaves_sorts_by_codepoint() -> None:
    text = _dump(
        "(1,NULL,'가전',NULL),\n(2,1,'가전 > 냉장고','[{}]'),\n(3,1,'가전 > TV','[{}]')",
        header="-- category: 3 rows\n",
    )
    leaves = extract_leaves(text)
    assert leaves == sorted(["가전 > 냉장고", "가전 > TV"])


def test_codepoint_fingerprint_matches_manual_sha256() -> None:
    import hashlib

    leaves = ["가전 > TV", "PC부품 > CPU"]
    fingerprint = codepoint_fingerprint(leaves)
    assert fingerprint["rowCount"] == 2
    expected = hashlib.sha256("\n".join(sorted(leaves)).encode("utf-8")).hexdigest()
    assert fingerprint["sha256"] == expected
