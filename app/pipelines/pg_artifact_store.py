"""AI 생성물 카탈로그 스토어 — pg-catalog(pgvector) 프로덕션 구현 (이슈 #31, api-spec §4.8).

CatalogArtifactStore(artifact_store.py, 인메모리)와 동일한 메서드 시그니처를 제공하는 동기
구현체 — 호출부(artifacts_batch.py·search_service.py)는 store 를 주입받아 쓰므로 무변경이다.
유닛 테스트는 계속 CatalogArtifactStore(인메모리)를 주입해 pg-catalog 없이도 빠르게 돈다
(tests/conftest.py InMemory 격리 컨벤션, 커밋 5066ecf 와 동일 원칙) — 이 구현체 자체의 테스트는
tests/integration/에 별도로 둔다(@pytest.mark.integration, 실 pg-catalog 필요).

배치 커서는 products 와 별도로 batch_state(단일 행) 테이블에 영속한다(db/catalog/init/00_products.sql).
"""

from __future__ import annotations

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.pipelines.artifact_store import CatalogArtifact


def _to_list(value: object) -> list[float]:
    """pgvector 조회 결과(Vector | ndarray | list)를 list[float] 로 정규화한다."""
    if hasattr(value, "to_list"):
        return value.to_list()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)  # type: ignore[arg-type]


def _row_to_artifact(row: tuple) -> CatalogArtifact:
    product_id, search_doc, embedding, extras, embed_model, embed_dim, embed_task, normalized = row
    return CatalogArtifact(
        product_id=product_id,
        search_doc=search_doc,
        embedding=_to_list(embedding),
        extras=extras or {},
        embed_model=embed_model,
        embed_dim=embed_dim,
        embed_task=embed_task,
        normalized=normalized,
    )


_SELECT_COLS = (
    "product_id, search_doc, embedding, extras, embed_model, embed_dim, embed_task, normalized"
)


class PgCatalogArtifactStore:
    """pg-catalog products/batch_state 테이블 기반 스토어. CatalogArtifactStore 와 동일 인터페이스."""

    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(dsn, configure=register_vector, open=True)

    def close(self) -> None:
        self._pool.close()

    def upsert(self, artifact: CatalogArtifact) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO products
                    (product_id, search_doc, embedding, extras,
                     embed_model, embed_dim, embed_task, normalized, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (product_id) DO UPDATE SET
                    search_doc = EXCLUDED.search_doc,
                    embedding = EXCLUDED.embedding,
                    extras = EXCLUDED.extras,
                    embed_model = EXCLUDED.embed_model,
                    embed_dim = EXCLUDED.embed_dim,
                    embed_task = EXCLUDED.embed_task,
                    normalized = EXCLUDED.normalized,
                    updated_at = now()
                """,  # noqa: S608 - 컬럼 상수만 사용, 사용자 입력 없음
                (
                    artifact.product_id,
                    artifact.search_doc,
                    Vector(artifact.embedding),
                    Jsonb(artifact.extras),
                    artifact.embed_model,
                    artifact.embed_dim,
                    artifact.embed_task,
                    artifact.normalized,
                ),
            )

    def delete(self, product_id: int) -> None:  # HIDDEN — 생성물 제거(유령 상품 방지, §4.8)
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM products WHERE product_id = %s", (product_id,))

    def clear(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM products")

    @staticmethod
    def _replace_all(conn, artifacts: list[CatalogArtifact]) -> None:
        conn.execute("DELETE FROM products")
        for artifact in artifacts:
            conn.execute(
                """
                INSERT INTO products
                    (product_id, search_doc, embedding, extras,
                     embed_model, embed_dim, embed_task, normalized, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    artifact.product_id,
                    artifact.search_doc,
                    Vector(artifact.embedding),
                    Jsonb(artifact.extras),
                    artifact.embed_model,
                    artifact.embed_dim,
                    artifact.embed_task,
                    artifact.normalized,
                ),
            )

    @staticmethod
    def _set_cursor(conn, cursor: str | None) -> None:
        conn.execute(
            """
            INSERT INTO batch_state (id, cursor) VALUES (1, %s)
            ON CONFLICT (id) DO UPDATE SET cursor = EXCLUDED.cursor
            """,
            (cursor,),
        )

    def replace_all(self, artifacts: list[CatalogArtifact]) -> None:
        """전체 재구축 원자 교체 — 단일 트랜잭션(중간 실패 시 기존 데이터 보존, §4.8)."""
        with self._pool.connection() as conn, conn.transaction():
            self._replace_all(conn, artifacts)

    def replace_all_and_set_cursor(
        self, artifacts: list[CatalogArtifact], cursor: str | None
    ) -> None:
        """전체 생성물과 커서를 하나의 DB 트랜잭션으로 교체한다."""
        with self._pool.connection() as conn, conn.transaction():
            self._replace_all(conn, artifacts)
            self._set_cursor(conn, cursor)

    def get(self, product_id: int) -> CatalogArtifact | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLS} FROM products WHERE product_id = %s", (product_id,)
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def get_many(self, product_ids: list[int]) -> dict[int, CatalogArtifact]:
        """요청 id 를 1회 쿼리(WHERE product_id = ANY(%s))로 조회 — 방식2 재정렬 N+1 제거(#101).

        psycopg 는 list[int] 를 ANY(%s) 에 바인딩한다. 빈 입력은 쿼리 없이 빈 dict. 없는 id 는 생략.
        """
        if not product_ids:
            return {}
        # statement_timeout — top_k_by_vector 와 같은 이유(PR #213 리뷰). I-22 만이 아니라
        # 채팅 rerank 도 이 메서드를 타므로, PK 배치 조회가 2초를 넘기는 병리 상황(예: I-17
        # replace_all 의 테이블 락)에서 풀 커넥션이 무한정 붙잡히는 것을 모든 호출자에 대해 막는다.
        from app.core.config import get_settings  # noqa: PLC0415 - 순환 임포트 회피(모듈 관례)

        timeout_ms = int(get_settings().catalog_store_query_timeout_s * 1000)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM products WHERE product_id = ANY(%s)", (product_ids,)
            ).fetchall()
        return {row[0]: _row_to_artifact(row) for row in rows}

    def all(self) -> list[CatalogArtifact]:
        with self._pool.connection() as conn:
            rows = conn.execute(f"SELECT {_SELECT_COLS} FROM products").fetchall()
        return [_row_to_artifact(row) for row in rows]

    def count(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT count(*) FROM products").fetchone()
        return row[0] if row else 0

    def top_k_by_vector(
        self, query_vec: list[float], *, k: int, exclude: set[int] | None = None
    ) -> list[int]:
        """질의 벡터에 가까운 상위 k productId — 코사인 거리(`<=>`)로 DB 에서 자른다 (I-22, #148).

        전량을 파이썬으로 끌어와 코사인을 도는 방식은 7,220건 기준 **p50 3.3초**로 I-22 예산
        (연결 2s/응답 3s, §3.7)을 그 자체로 초과했다. 실측 후 SQL 로 밀었다.

        **연산자는 `<=>`(코사인 거리)다** — 앱 DDL(`db/catalog/init/00_products.sql`)의 HNSW
        인덱스가 `vector_cosine_ops` 라서다. 초기 구현은 `<#>`(내적)을 썼는데 **연산자 클래스
        불일치로 인덱스가 이 쿼리에 절대 쓰일 수 없었다**(PR #213 리뷰 #3 검증 중 발견 —
        enable_seqscan=off 로도 Seq Scan. "인덱스가 vector_ip_ops" 라던 구 주석은 시드 덤프가
        만든 별개 테이블의 인덱스를 잘못 본 것). 코사인은 인메모리 구현·`vector_rank` 와 같은
        척도라 정규화 여부와 무관하게 순위 의미가 일치한다.

        **정렬키는 거리 하나다** — `ORDER BY dist, product_id` 처럼 2차 키가 붙으면 ANN pushdown
        이 깨져 인덱스 스캔 위에 전체 Sort 가 얹힌다(PR #213 리뷰 #3). 결정성 tiebreak 은 반환된
        k 행을 파이썬에서 `(dist, product_id)` 로 재정렬해 유지한다 — 같은 snapshot 이면 인덱스
        상태가 같아 k 경계도 재현된다.

        recall 방어 — HNSW 는 `ef_search` 범위만 돌고 필터를 적용하므로 제외 대상이 상위권에
        몰리면 k 미만이 나올 수 있다(가짜 INSUFFICIENT_CANDIDATES). `iterative_scan =
        strict_order`(pgvector ≥0.8)로 필터에 걸린 만큼 탐색을 이어가게 한다. 현 규모에선
        플래너가 Seq Scan(정확 탐색)을 택해 어느 쪽이든 안전하다(실측: 최근접 3,000 제외에도
        24/24).

        statement_timeout — 호출측 asyncio.wait_for(504 변환)와 이중 방어다. to_thread 취소는
        밑에서 도는 쿼리를 죽이지 못해, DB 쪽 상한이 없으면 지연 쿼리가 풀 커넥션을 계속 붙들어
        후속 요청까지 말려든다. SET LOCAL 이라 트랜잭션 밖으로 새지 않는다.
        """
        from app.core.config import get_settings  # noqa: PLC0415 - 순환 임포트 회피(모듈 관례)

        timeout_ms = int(get_settings().catalog_store_query_timeout_s * 1000)
        skip = list(exclude or ())
        qvec = Vector(query_vec)
        # 제외 유무로 쿼리 모양을 가른다 — `(%s IS NULL OR ...)` 패턴은 플래너가 인덱스 경로를
        # 잡기 어렵게 만든다. 술어는 단순할수록 pushdown 이 산다.
        if skip:
            sql = """
                SELECT product_id, embedding <=> %s AS dist
                FROM products
                WHERE embedding IS NOT NULL AND product_id <> ALL(%s::bigint[])
                ORDER BY embedding <=> %s
                LIMIT %s
                """
            params: tuple = (qvec, skip, qvec, k)
        else:
            sql = """
                SELECT product_id, embedding <=> %s AS dist
                FROM products
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s
                LIMIT %s
                """
            params = (qvec, qvec, k)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            rows = conn.execute(sql, params).fetchall()
        # 결정적 tiebreak — 동거리(중복 상품 등)는 productId 오름차순. SQL 2차 정렬키 대신
        # 여기서 하는 이유는 위 docstring(ANN pushdown) 참조.
        rows.sort(key=lambda r: (r[1], r[0]))
        return [r[0] for r in rows]

    def get_cursor(self) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT cursor FROM batch_state WHERE id = 1").fetchone()
        return row[0] if row else None

    def set_cursor(self, cursor: str | None) -> None:
        with self._pool.connection() as conn:
            self._set_cursor(conn, cursor)
