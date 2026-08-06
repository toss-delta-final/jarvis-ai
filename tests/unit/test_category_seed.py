"""카테고리 시드 페어링 로직 테스트 (이슈 #59) + 0행/0임베딩 가드 테스트 (이슈 #401).

leaf 목록을 임베딩(주입형)해 (category, vector) 목록으로 짝짓는 순수 로직만 검증한다.
DB upsert(pg-catalog)는 통합 테스트 소관(@pytest.mark.integration). 가드 판정
(`evaluate_dictionary_counts`)도 DB 없이 순수 함수로 검증한다.
"""

from __future__ import annotations

import pytest

import app.pipelines.category_seed as category_seed
from app.pipelines.category_seed import (
    CategoryDictionaryError,
    DictionaryCounts,
    check_category_dictionary,
    embed_categories,
    evaluate_dictionary_counts,
)


def test_pairs_each_leaf_with_its_vector() -> None:
    """leaf 순서 그대로 임베딩 벡터와 1:1로 짝짓는다."""
    leaves = ["가전 > TV", "PC부품 > CPU"]

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[float(i)] for i, _ in enumerate(texts)]

    rows = embed_categories(leaves, fake_embed)
    assert rows == [("가전 > TV", [0.0]), ("PC부품 > CPU", [1.0])]


def test_deduplicates_preserving_order() -> None:
    """중복 leaf 는 한 번만 임베딩·수록한다(순서 보존)."""
    seen: list[list[str]] = []

    def fake_embed(texts: list[str]) -> list[list[float]]:
        seen.append(texts)
        return [[0.0] for _ in texts]

    rows = embed_categories(["A", "B", "A"], fake_embed)
    assert [c for c, _ in rows] == ["A", "B"]
    assert seen == [["A", "B"]]  # 중복 제거 후 한 번만 임베딩 호출


def test_empty_leaves_skip_embed() -> None:
    """빈 입력이면 임베딩을 호출하지 않고 빈 목록을 돌려준다."""
    called = False

    def fake_embed(texts: list[str]) -> list[list[float]]:
        nonlocal called
        called = True
        return []

    assert embed_categories([], fake_embed) == []
    assert called is False


def test_seed_from_file_default_embed_uses_document_task_type(monkeypatch, tmp_path) -> None:
    """embed 미주입(프로덕션 시드 경로)이면 문서 task_type(RETRIEVAL_DOCUMENT)로 임베딩한다.

    categories 테이블 저장 임베딩은 문서 쪽 — artifacts_batch 처럼 embedding_task_document 를
    실어야 map_categories 질의(RETRIEVAL_QUERY)와 비대칭 검색 관례가 맞는다(이슈 #65·PR #73 리뷰).
    """
    captured: dict = {}

    def fake_embed_texts(texts, *, task_type=None):
        captured["task_type"] = task_type
        return [[0.0] for _ in texts]

    monkeypatch.setattr(category_seed, "_embed_texts", fake_embed_texts)
    monkeypatch.setattr(category_seed, "upsert_categories", lambda dsn, rows, model: len(rows))
    src = tmp_path / "categories.json"
    src.write_text('["가전 > TV"]', encoding="utf-8")

    category_seed.seed_from_file(str(src), "postgresql://x")
    assert captured["task_type"] == "RETRIEVAL_DOCUMENT"


# --- 0행/0임베딩 가드 (이슈 #401) ------------------------------------------------------------


def test_evaluate_dictionary_counts_zero_rows_is_error_and_names_seed_path() -> None:
    """총 행 0 → 구성 오류. 메시지에 정본 경로와 복구 방법이 있어야 사람이 바로 대응할 수 있다."""
    level, message = evaluate_dictionary_counts(DictionaryCounts(total=0, embedded=0))
    assert level == "error"
    assert "db/catalog/seed/categories.json" in message
    assert "04_categories_seed.sql" in message


def test_evaluate_dictionary_counts_rows_but_zero_embedded_is_error() -> None:
    """행은 있지만 embedding 이 전부 NULL → 검색 쿼리(embedding IS NOT NULL) 입장에선 0행과 같다.

    행 수만 세는 가드였다면 이 케이스를 정상으로 오판했을 것 — 회귀 시 실패해야 한다.
    """
    level, message = evaluate_dictionary_counts(DictionaryCounts(total=1007, embedded=0))
    assert level == "error"
    assert "임베딩 배치" in message


def test_evaluate_dictionary_counts_both_nonzero_is_ok() -> None:
    """총 행·embedding 채워진 행이 둘 다 > 0 이면 정상 — 행 수를 메시지에 남겨 관측 가능하게 한다."""
    level, message = evaluate_dictionary_counts(DictionaryCounts(total=1007, embedded=1007))
    assert level == "ok"
    assert "1007" in message


def test_check_category_dictionary_mode_off_skips_query(monkeypatch) -> None:
    """`off` 는 DB 조회 자체를 생략한다 — dictionary_counts 가 호출되면 안 된다."""
    called = False

    def fail_if_called(dsn: str) -> DictionaryCounts:
        nonlocal called
        called = True
        return DictionaryCounts(total=0, embedded=0)

    monkeypatch.setattr(category_seed, "dictionary_counts", fail_if_called)
    check_category_dictionary("postgresql://x", mode="off")
    assert called is False


def test_check_category_dictionary_mode_log_records_error_but_does_not_raise(
    caplog, monkeypatch
) -> None:
    monkeypatch.setattr(
        category_seed, "dictionary_counts", lambda dsn: DictionaryCounts(total=0, embedded=0)
    )
    with caplog.at_level("ERROR"):
        check_category_dictionary("postgresql://x", mode="log")  # 예외 없이 통과해야 함
    assert "카테고리 사전 0행" in caplog.text


def test_check_category_dictionary_mode_fail_raises_on_zero_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        category_seed, "dictionary_counts", lambda dsn: DictionaryCounts(total=0, embedded=0)
    )
    with pytest.raises(CategoryDictionaryError):
        check_category_dictionary("postgresql://x", mode="fail")


def test_check_category_dictionary_mode_fail_raises_on_zero_embedded(monkeypatch) -> None:
    monkeypatch.setattr(
        category_seed, "dictionary_counts", lambda dsn: DictionaryCounts(total=1007, embedded=0)
    )
    with pytest.raises(CategoryDictionaryError):
        check_category_dictionary("postgresql://x", mode="fail")


def test_check_category_dictionary_mode_fail_does_not_raise_when_ok(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        category_seed, "dictionary_counts", lambda dsn: DictionaryCounts(total=1007, embedded=1007)
    )
    with caplog.at_level("INFO"):
        check_category_dictionary("postgresql://x", mode="fail")  # 예외를 던지면 안 됨
    assert "카테고리 사전 정상" in caplog.text
