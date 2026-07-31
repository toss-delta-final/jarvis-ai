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
        with self._pool.connection() as conn:
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
        """질의 벡터에 가까운 상위 k productId — **HNSW 인덱스로 DB 에서** 정렬한다 (I-22, #148).

        전량을 파이썬으로 끌어와 코사인을 도는 방식은 7,220건 기준 **p50 3.3초**로 I-22 예산
        (연결 2s/응답 3s, §3.7)을 그 자체로 초과했다. 실측 후 SQL 로 밀었다.

        `<#>` (음의 내적)를 쓰는 이유는 카탈로그 HNSW 인덱스가 `vector_ip_ops` 로 만들어져 있어
        이 연산자만 인덱스를 타기 때문이다. **임베딩이 L2 정규화돼 있으면 내적 순위 = 코사인 순위**이며
        (`embedding.py::_l2_normalize`, `normalized` 컬럼이 그 사실을 기록한다) 정규화가 깨지면
        순위 의미도 깨진다 — 정규화는 이 경로의 전제다.

        동점은 `product_id` 오름차순으로 tiebreak 해 결정적이다(동일 snapshot → 동일 ranking).
        HNSW 는 근사 탐색이지만 인덱스·질의가 같으면 같은 결과를 준다.

        **HNSW + WHERE(구매 이력 제외) 조합의 recall** — PR 리뷰 지적. HNSW 는 `ef_search` 범위만
        그래프를 돌고 그 결과에 필터를 적용하므로, 제외 대상이 탐색 상위권에 몰리면 후보가 충분해도
        k 개 미만이 나올 수 있다(가짜 INSUFFICIENT_CANDIDATES). 실측(2026-07-31, 7,220건)으로는
        **재현되지 않았다** — EXPLAIN 확인 결과 이 쿼리 모양(OR + 배열 필터)에서 플래너가 Seq Scan
        (정확 탐색)을 택해, 최근접 3,000개를 제외해도 24/24 가 나온다. 다만 카탈로그가 커져 플래너가
        인덱스 경로로 넘어가면 지적이 유효해지므로 `SET LOCAL hnsw.iterative_scan = strict_order`
        (pgvector ≥0.8, 현재 0.8.5)를 미리 걸어 둔다 — 필터로 k 개를 못 채우면 정확한 순서를 유지한
        채 탐색을 이어가는 모드다. SET LOCAL 이라 풀에 반환된 커넥션에 새지 않고, Seq Scan 플랜에는
        영향이 없다.
        """
        # statement_timeout — 호출측 asyncio.wait_for(504 변환)와 이중 방어다. to_thread 취소는
        # 밑에서 도는 쿼리를 죽이지 못해, DB 쪽 상한이 없으면 지연 쿼리가 풀 커넥션을 계속 붙들어
        # 후속 요청까지 말려든다(PR #213 리뷰). SET LOCAL 이라 트랜잭션 밖으로 새지 않는다.
        from app.core.config import get_settings  # noqa: PLC0415 - 순환 임포트 회피(모듈 관례)

        timeout_ms = int(get_settings().home_reco_store_timeout_s * 1000)
        skip = list(exclude or ())
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            rows = conn.execute(
                """
                SELECT product_id
                FROM products
                WHERE embedding IS NOT NULL
                  AND (%s::bigint[] IS NULL OR product_id <> ALL(%s::bigint[]))
                ORDER BY embedding <#> %s, product_id
                LIMIT %s
                """,
                (skip or None, skip or None, Vector(query_vec), k),
            ).fetchall()
        return [r[0] for r in rows]

    def get_cursor(self) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT cursor FROM batch_state WHERE id = 1").fetchone()
        return row[0] if row else None

    def set_cursor(self, cursor: str | None) -> None:
        with self._pool.connection() as conn:
            self._set_cursor(conn, cursor)
