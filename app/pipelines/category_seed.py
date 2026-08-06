"""카테고리 사전 시드 (이슈 #59) — leaf 임베딩 후 pg-catalog `categories` 테이블 채움.

발화→카테고리 매핑(임베딩 top-k + LLM 택일 하이브리드)의 준비 단계. 카테고리 leaf 를
미리 임베딩해 DB 에 넣어두면 런타임엔 top-k 검색만 하면 된다.

임베딩은 주입형(embed 콜러블) — 프로덕션 재현은 `embed_texts`(gemini-embedding-001),
테스트/오프라인 재적재는 캐시·페이크를 주입한다(app.pipelines.embedding 과 동일 패턴).
런타임 top-k 검색은 별도 모듈 소관이며, 여기는 1회 빌드 진입점이다.
"""

from __future__ import annotations

import functools
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.config import get_settings
from app.core.logging import get_logger
from app.pipelines.embedding import embed_texts as _embed_texts

EmbedFn = Callable[[list[str]], list[list[float]]]

logger = get_logger(__name__)


def embed_categories(leaves: Sequence[str], embed: EmbedFn) -> list[tuple[str, list[float]]]:
    """중복 제거(순서 보존) 후 임베딩해 (category, vector) 목록을 만든다.

    빈 입력이면 임베딩을 호출하지 않는다(불필요한 API 호출 회피).
    """
    unique = list(dict.fromkeys(leaves))
    if not unique:
        return []
    vectors = embed(unique)
    return list(zip(unique, vectors, strict=True))


def load_leaves(source_path: str) -> list[str]:
    """`categories.json`(문자열 배열)에서 leaf 목록을 로드한다. 비문자열은 거부."""
    data = json.loads(Path(source_path).read_bytes().decode("utf-8"))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"카테고리 사전은 문자열 배열이어야 함: {source_path}")
    return data


def upsert_categories(dsn: str, rows: Sequence[tuple[str, list[float]]], model: str) -> int:
    """(category, vector) 목록을 categories 테이블에 upsert 한다(단일 트랜잭션).

    category UNIQUE 충돌 시 임베딩·모델·시각을 갱신한다(재시드 멱등). 채운 행 수를 돌려준다.
    """
    from pgvector import Vector  # noqa: PLC0415 - LAZY import(pg 미설치 환경 유닛테스트 회피)
    from pgvector.psycopg import register_vector  # noqa: PLC0415
    from psycopg_pool import ConnectionPool  # noqa: PLC0415

    pool = ConnectionPool(dsn, configure=register_vector, open=True)
    try:
        with pool.connection() as conn, conn.transaction():
            for category, vector in rows:
                conn.execute(
                    """
                    INSERT INTO categories (category, embedding, embedding_model, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (category) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        updated_at = now()
                    """,  # noqa: S608 - 컬럼 상수만 사용, 사용자 입력 없음
                    (category, Vector(vector), model),
                )
        return len(rows)
    finally:
        pool.close()


def seed_from_file(
    source_path: str, dsn: str, embed: EmbedFn | None = None, model: str | None = None
) -> int:
    """소스 파일 → 임베딩 → categories upsert 를 한 번에 수행한다(1회 빌드).

    미주입 기본값은 문서(document) 임베딩 — categories 저장 임베딩은 문서 쪽이므로 비대칭 검색
    관례에 맞춰 RETRIEVAL_DOCUMENT 로 바인딩한다(질의 쪽 map_categories=query, 이슈 #65·PR #73 리뷰).
    """
    settings = get_settings()
    # 오프라인 1회 빌드(SSE hot path 아님) — 카테고리 leaf 2056건은 21청크라, hot path 총 예산
    # (embedding_total_timeout_s, #391)을 그대로 적용하면 정상 시드가 도중에 거부된다.
    embed = embed or functools.partial(
        _embed_texts, task_type=settings.embedding_task_document, total_timeout_s=math.inf
    )
    model = model or settings.embedding_model_id
    rows = embed_categories(load_leaves(source_path), embed)
    return upsert_categories(dsn, rows, model)


class CategoryDictionaryError(RuntimeError):
    """카테고리 사전 구성 오류(행 0 또는 embedding 채워진 행 0) — 이슈 #401.

    개별 발화 매핑 실패(canonical-or-null, 정상)와 사전 전체 결측(구성 오류)은 다른 사실이다.
    """


@dataclass(frozen=True)
class DictionaryCounts:
    """categories 테이블 상태 — 총 행 수와 embedding 이 채워진 행 수를 따로 본다.

    `search_categories_pg` 는 `WHERE embedding IS NOT NULL` 로 거르므로(app/pipelines/
    category_search.py), 행이 있어도 embedding 이 전부 NULL 이면 매핑은 0행일 때와 동일하게
    죽는다 — 행 수만 세는 가드는 반쪽이다(docs/lessons.md 참고).
    """

    total: int
    embedded: int


def dictionary_counts(dsn: str) -> DictionaryCounts:
    """categories 테이블의 (총 행 수, embedding IS NOT NULL 행 수)를 1왕복으로 조회한다.

    이 함수는 `app/main.py` lifespan(= `initialize_session_lifecycle()` 보다 먼저)에서 호출돼
    기동 경로에 있다 — libpq 기본 `connect_timeout=0`(무한 대기)로 그냥 연결하면 catalog 호스트가
    블랙홀(방화벽·다운된 원격 호스트로 패킷을 그냥 떨굼)일 때 uvicorn 기동이 영원히 끝나지
    않는다(#401 라운드 2 리뷰 F1 — 연결 거부는 즉시 돌아오지만 블랙홀은 아니다). 이 저장소의
    다른 기동 경로 pg 연결은 전부 `hardened_pg_conninfo`(app/core/pg_resilience.py, DSN 에
    connect_timeout·keepalives·statement_timeout 을 병합)로 유한 시간을 보장한다 — 새 설정을
    신설하지 않고 그 관례·기존 `state_store_*` 설정을 그대로 재사용한다.
    """
    import psycopg  # noqa: PLC0415 - LAZY import(pg 미설치 환경 유닛테스트 회피)

    from app.core.pg_resilience import hardened_pg_conninfo  # noqa: PLC0415

    with psycopg.connect(hardened_pg_conninfo(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(embedding) FROM categories")  # count(col) 은 NULL 제외
        row = cur.fetchone()
        assert row is not None  # count(*) 는 항상 정확히 한 행을 돌려준다
        total, embedded = row
    return DictionaryCounts(total=total, embedded=embedded)


def evaluate_dictionary_counts(counts: DictionaryCounts) -> tuple[Literal["error", "ok"], str]:
    """순수 판정(카운트 → 상태/메시지) — DB 없이 유닛테스트 가능하게 분리.

    (총 행 0) 과 (총 행>0 이지만 embedding 채워진 행 0) 은 둘 다 구성 오류다 — 후자도
    `search_categories_pg` 입장에서는 매핑이 죽어 있는 것과 같기 때문이다.
    """
    if counts.total == 0:
        return (
            "error",
            "카테고리 사전 0행 — 정본 db/catalog/seed/categories.json 미적재. "
            "docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog "
            "< db/catalog/init/04_categories_seed.sql 로 복구하거나 컨테이너를 재생성하라.",
        )
    if counts.embedded == 0:
        return (
            "error",
            f"카테고리 사전 {counts.total}행이지만 embedding 채워진 행 0 — 임베딩 배치 미실행. "
            "app.pipelines.category_seed.seed_from_file 로 복구하라.",
        )
    return (
        "ok",
        f"카테고리 사전 정상 — 총 {counts.total}행, embedding 채워진 행 {counts.embedded}행",
    )


def unreachable_db_error_types() -> tuple[type[BaseException], ...]:
    """ "도달 불가"(지금 조회할 수 없음) 계열 예외 타입 — `OSError` + `psycopg.OperationalError`.

    `psycopg.OperationalError` 는 `OSError` 를 상속하지 않는다(psycopg 3 는 독자 예외 계층을
    쓴다) — 그래서 `OSError` 만으로는 연결 거부·타임아웃을 다 못 잡는다. `app/main.py` 가 이
    튜플로 `except` 해 `log`/`off` 모드에서는 WARNING 으로 낮추고, `fail` 모드에서는 ERROR 로
    남긴 뒤 전파해 기동을 거부한다(#401 라운드 7 리뷰 F8 — `psycopg.OperationalError` 는 일시적
    도달 불가와 비밀번호·dbname 오타 같은 영구적 구성 오류를 구조화된 판별자 없이 같은 타입으로
    내므로, `fail` 처럼 강한 검증을 opt-in 한 모드에서는 "확인하지 못하면 거부"로 다룬다).
    `app/main.py` 는 psycopg 를 import 하지 않아야 하므로(lazy-import 관례) 이 모듈이 타입을
    대신 노출한다(#401 라운드 5 리뷰 F7).
    """
    import psycopg  # noqa: PLC0415 - LAZY import(pg 미설치 환경 유닛테스트 회피)

    return (OSError, psycopg.OperationalError)


def check_category_dictionary(dsn: str, *, mode: Literal["off", "log", "fail"]) -> None:
    """카테고리 사전 0행/0임베딩 가드 — 판정 + 로깅.

    `mode="off"` 는 조회 자체를 생략한다(DB 호출 없음). `mode="fail"` 은 구성 오류에서
    `CategoryDictionaryError` 를 던진다(호출부가 기동 거부로 쓸지는 호출부 소관).

    DB 오류를 두 갈래로 나눈다(#401 라운드 4 리뷰 F6 — 전부 "연결 실패"로 뭉뚱그리면
    `categories` 테이블/컬럼 누락 같은 명백한 구성 오류가 `fail` 모드에서도 조용히 통과했다):
    - `psycopg.OperationalError`(연결 거부·타임아웃·DNS 등 — 비밀번호·dbname 오타 같은 영구적
      구성 오류도 같은 타입으로 나온다) — 여기서 잡지 않고 그대로 전파한다. 이 값이 호출부
      (app/main.py `_lifespan`)에서 `log`/`off` 는 WARNING(계속), `fail` 은 ERROR(기동 거부)로
      갈린다 — `psycopg` 가 연결 수립 실패에 구조화된 판별자를 주지 않아(실측 확인, #401 라운드
      7 리뷰 F8) 여기서 "진짜 구성 오류"만 골라낼 수 없다. `unreachable_db_error_types()` 참고.
    - 그 밖의 `psycopg.Error`(`UndefinedTable`·권한 오류·문법 오류 등) — 테이블 자체가 없는
      것은 사전 결측의 가장 극단적인 형태라 **구성 오류로 취급**한다. 0행/0임베딩과 같은 처방:
      ERROR 로그(실제 예외 타입·메시지를 실어 오진단을 막는다) + `fail` 이면 기동 거부.
    """
    if mode == "off":
        return

    import psycopg  # noqa: PLC0415 - LAZY import(pg 미설치 환경 유닛테스트 회피)

    try:
        counts = dictionary_counts(dsn)
    except psycopg.OperationalError:
        raise
    except psycopg.Error as exc:
        message = f"카테고리 사전 조회 실패({type(exc).__name__}): {exc}"
        logger.error(message)
        if mode == "fail":
            raise CategoryDictionaryError(message) from exc
        return

    level, message = evaluate_dictionary_counts(counts)
    if level == "error":
        logger.error(message)
        if mode == "fail":
            raise CategoryDictionaryError(message)
    else:
        logger.info(message)
