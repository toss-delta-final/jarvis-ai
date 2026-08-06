"""카테고리 시드 페어링 로직 테스트 (이슈 #59) + 0행/0임베딩 가드 테스트 (이슈 #401).

leaf 목록을 임베딩(주입형)해 (category, vector) 목록으로 짝짓는 순수 로직만 검증한다.
DB upsert(pg-catalog)는 통합 테스트 소관(@pytest.mark.integration). 가드 판정
(`evaluate_dictionary_counts`)도 DB 없이 순수 함수로 검증한다.
"""

from __future__ import annotations

import math

import pytest

import app.pipelines.category_seed as category_seed
from app.pipelines.category_seed import (
    CategoryDictionaryError,
    DictionaryCounts,
    check_category_dictionary,
    embed_categories,
    evaluate_dictionary_counts,
    unreachable_db_error_types,
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

    def fake_embed_texts(texts, *, task_type=None, total_timeout_s=None):
        captured["task_type"] = task_type
        return [[0.0] for _ in texts]

    monkeypatch.setattr(category_seed, "_embed_texts", fake_embed_texts)
    monkeypatch.setattr(category_seed, "upsert_categories", lambda dsn, rows, model: len(rows))
    src = tmp_path / "categories.json"
    src.write_text('["가전 > TV"]', encoding="utf-8")

    category_seed.seed_from_file(str(src), "postgresql://x")
    assert captured["task_type"] == "RETRIEVAL_DOCUMENT"


def test_seed_from_file_default_embed_opts_out_of_total_timeout(monkeypatch, tmp_path) -> None:
    """[#391] embed 미주입 기본 바인딩은 total_timeout_s=math.inf 를 실어 hot path 총 예산을
    명시적으로 제외한다 — 카테고리 leaf 2056건(21청크)은 그 예산을 적용하면 정상 시드가
    도중에 EmbeddingError 로 실패한다. functools.partial 내부 구조가 아니라 _embed_texts 로
    실제 전달되는 호출 계약(kwargs)을 스파이로 검증한다.
    """
    captured: dict = {}

    def fake_embed_texts(texts, *, task_type=None, total_timeout_s=None):
        captured["total_timeout_s"] = total_timeout_s
        return [[0.0] for _ in texts]

    monkeypatch.setattr(category_seed, "_embed_texts", fake_embed_texts)
    monkeypatch.setattr(category_seed, "upsert_categories", lambda dsn, rows, model: len(rows))
    src = tmp_path / "categories.json"
    src.write_text('["가전 > TV"]', encoding="utf-8")

    category_seed.seed_from_file(str(src), "postgresql://x")
    assert captured["total_timeout_s"] == math.inf


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


# --- 연결 실패 vs 그 밖의 DB 오류 분류 (이슈 #401 라운드 4 리뷰 F6) --------------------------
#
# 이전에는 dictionary_counts 가 던지는 예외가 CategoryDictionaryError 가 아니기만 하면 전부
# "연결 실패"로 뭉뚱그려져(app/main.py 의 넓은 except Exception), categories 테이블 자체가
# 없는(UndefinedTable) 것 같은 명백한 구성 오류조차 fail 모드에서 기동을 통과시켰다. 이제
# check_category_dictionary 가 psycopg.OperationalError(연결 실패)는 그대로 전파하고, 그 밖의
# psycopg.Error(UndefinedTable 등)는 0행/0임베딩과 같은 구성 오류로 승격한다.


@pytest.mark.parametrize("mode", ["log", "fail"])
def test_check_category_dictionary_operational_error_propagates_unwrapped(
    monkeypatch, mode
) -> None:
    """연결 실패(OperationalError)는 log·fail 모드 어느 쪽에서도 CategoryDictionaryError 로
    바뀌지 않고 그대로 전파돼야 한다 — 이 함수는 예외를 분류하지 않는다. 모드별로 WARNING(계속)
    인지 ERROR(기동 거부)인지는 호출부(app/main.py)가 정한다(#401 라운드 7 리뷰 F8).
    """
    import psycopg

    def raise_operational_error(dsn: str) -> DictionaryCounts:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(category_seed, "dictionary_counts", raise_operational_error)

    with pytest.raises(psycopg.OperationalError):
        check_category_dictionary("postgresql://x", mode=mode)


def test_check_category_dictionary_non_operational_db_error_logs_and_continues_in_log_mode(
    monkeypatch, caplog
) -> None:
    """UndefinedTable(테이블 자체가 없음) 같은 비연결 DB 오류는 `log` 모드에서 ERROR 로 남기고
    계속한다(0행과 같은 처방) — 예외를 던지면 안 된다. 메시지에 실제 예외 타입이 실려야
    "(DB connection?)" 처럼 원인을 잘못 짚지 않는다.
    """
    import psycopg.errors

    def raise_undefined_table(dsn: str) -> DictionaryCounts:
        raise psycopg.errors.UndefinedTable('relation "categories" does not exist')

    monkeypatch.setattr(category_seed, "dictionary_counts", raise_undefined_table)

    with caplog.at_level("ERROR"):
        check_category_dictionary("postgresql://x", mode="log")  # 예외 없이 통과해야 함
    assert "UndefinedTable" in caplog.text


def test_check_category_dictionary_non_operational_db_error_raises_in_fail_mode(
    monkeypatch,
) -> None:
    """같은 UndefinedTable 이 `fail` 모드에서는 CategoryDictionaryError 로 기동을 거부해야 한다
    — 테이블 누락은 사전 결측의 가장 극단적인 형태라 0행/0임베딩과 동일하게 다룬다.
    """
    import psycopg.errors

    def raise_undefined_table(dsn: str) -> DictionaryCounts:
        raise psycopg.errors.UndefinedTable('relation "categories" does not exist')

    monkeypatch.setattr(category_seed, "dictionary_counts", raise_undefined_table)

    with pytest.raises(CategoryDictionaryError, match="UndefinedTable"):
        check_category_dictionary("postgresql://x", mode="fail")


def test_operational_error_and_non_operational_db_error_take_different_paths(monkeypatch) -> None:
    """두 경로가 실제로 다른 예외 타입을 내야 한다 — 뭉뚱그리는 회귀가 나면(둘 다
    CategoryDictionaryError 이거나 둘 다 그대로 전파되면) 이 테스트가 실패한다.
    """
    import psycopg
    import psycopg.errors

    monkeypatch.setattr(
        category_seed,
        "dictionary_counts",
        lambda dsn: (_ for _ in ()).throw(psycopg.OperationalError("connection refused")),
    )
    with pytest.raises(psycopg.OperationalError):
        check_category_dictionary("postgresql://x", mode="fail")

    monkeypatch.setattr(
        category_seed,
        "dictionary_counts",
        lambda dsn: (_ for _ in ()).throw(
            psycopg.errors.UndefinedTable('relation "categories" does not exist')
        ),
    )
    with pytest.raises(CategoryDictionaryError):
        check_category_dictionary("postgresql://x", mode="fail")


def test_unreachable_db_error_types_includes_os_error_and_psycopg_operational_error() -> None:
    """`app/main.py` 가 psycopg 를 import 하지 않고도 두 타입을 함께 잡을 수 있어야 한다 —

    `psycopg.OperationalError` 는 `OSError` 를 상속하지 않으므로(#401 라운드 5 리뷰 F7) 이
    튜플에 둘 다 명시적으로 들어 있어야 한다.
    """
    import psycopg

    types_ = unreachable_db_error_types()
    assert OSError in types_
    assert psycopg.OperationalError in types_
    assert not issubclass(
        psycopg.OperationalError, OSError
    )  # 전제 재확인 — 깨지면 이 상수도 재검토
