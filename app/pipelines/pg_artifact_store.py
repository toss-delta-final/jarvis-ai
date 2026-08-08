"""AI 생성물 카탈로그 스토어 — pg-catalog(pgvector) 프로덕션 구현 (이슈 #31, api-spec §4.8).

CatalogArtifactStore(artifact_store.py, 인메모리)와 동일한 메서드 시그니처를 제공하는 동기
구현체 — 호출부(artifacts_batch.py·search_service.py)는 store 를 주입받아 쓰므로 무변경이다.
유닛 테스트는 계속 CatalogArtifactStore(인메모리)를 주입해 pg-catalog 없이도 빠르게 돈다
(tests/conftest.py InMemory 격리 컨벤션, 커밋 5066ecf 와 동일 원칙) — 이 구현체 자체의 테스트는
tests/integration/에 별도로 둔다(@pytest.mark.integration, 실 pg-catalog 필요).

배치 커서는 products 와 별도로 batch_state(단일 행) 테이블에 영속한다(db/catalog/init/00_products.sql).

[이슈 #416] 실패 스트릭(bump/clear/purge_stale)은 batch_failure_state 테이블에 영속한다 —
첫 사용 시 idempotent DDL 을 1회(advisory lock 으로 중복 방지) 적용한다
(app/agents/profile/session_activity.py::ensure_schema_on_connection 과 같은 관례, 다만 이
스토어는 동기 ConnectionPool 이라 asyncio.Lock 대신 threading.Lock 으로 인스턴스 내 중복
초기화만 막고, 인스턴스 간(다른 프로세스) 경합은 pg_advisory_xact_lock 으로 막는다). 3개 메서드
모두 실패 시 예외를 삼키고 인메모리 FailureStreakTable 폴백으로 위임한다 — 폴백 동작이 곧
현행(프로세스 메모리) 동작이라 테이블 부재·pg 순단 상황에서도 배치가 죽지 않고 하한이 보장된다.
"""

from __future__ import annotations

import logging
import threading

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.pipelines.artifact_store import CatalogArtifact, FailureStreakTable

_log = logging.getLogger(__name__)

# pg_advisory_xact_lock 키 — session_activity.py/processed_events.py 와 같은 관례
# (hashtextextended 로 해시해 사용, 다른 스키마 락 키와 충돌하지 않게 이름공간을 문자열에 담음).
_FAILURE_STATE_SCHEMA_LOCK_KEY = "schema:batch_failure_state"


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
        self._failure_streak_schema_ready = False
        self._failure_streak_schema_lock = threading.Lock()
        self._failure_streak_fallback = FailureStreakTable()
        self._failure_streak_fallback_warned = False

    def close(self) -> None:
        self._pool.close()

    def _ensure_failure_streak_schema(self) -> None:
        """batch_failure_state idempotent DDL — 인스턴스당 1회(#416).

        session_activity.py::ensure_schema_on_connection 과 같은 관례: 인스턴스 내 중복
        실행은 threading.Lock 으로, 다른 프로세스/인스턴스와의 경합은 advisory lock 으로 막는다.
        """
        if self._failure_streak_schema_ready:
            return
        with self._failure_streak_schema_lock:
            if self._failure_streak_schema_ready:
                return
            with self._pool.connection() as conn, conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_FAILURE_STATE_SCHEMA_LOCK_KEY,),
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS batch_failure_state (
                        kind       text        NOT NULL,
                        state_key  text        NOT NULL,
                        streak     int         NOT NULL,
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (kind, state_key)
                    )
                    """
                )
            self._failure_streak_schema_ready = True

    def _warn_failure_streak_fallback(self) -> None:
        """폴백 전환을 1회만 WARNING 남긴다 — 매 호출마다 남기면 로그가 폭주한다(#416)."""
        if not self._failure_streak_fallback_warned:
            _log.warning(
                "batch_failure_state 저장 실패 — 인메모리 폴백으로 전환"
                "(재시작 시 스트릭 리셋, 종전(#325) 동작과 같은 하한)",
                exc_info=True,
            )
            self._failure_streak_fallback_warned = True

    def _mark_failure_streak_healthy(self) -> None:
        """DB 경로가 정상 종료하면 폴백 WARNING latch 를 되돌린다(T3, PR 리뷰 라운드 2).

        수정 전에는 ``_failure_streak_fallback_warned`` 가 한 번 True 가 되면 인스턴스 수명
        동안 다시 False 로 돌아가지 않아, pg 순단 1회로 latch 된 뒤 DB 가 복구돼도 그 뒤
        더 심각한 장애(테이블 삭제 등)가 나면 조용히 넘어갔다 — F5(``clear_failure_streak``)
        가 "폴백을 프로세스 수명 latch 로 승격하면 안 된다"고 정한 원칙이 이 WARNING
        플래그에는 지켜지지 않고 있었다. 3개 DB 경로가 정상 종료할 때마다 호출해, 다음
        장애가 다시 WARNING 을 낼 수 있게 한다.
        """
        self._failure_streak_fallback_warned = False

    def bump_failure_streak(self, kind: str, key: str, *, ttl_s: float) -> int:
        """단일 원자 UPSERT — 다중 인스턴스에서도 정확하도록(#416).

        마지막 갱신이 ttl_s 보다 오래됐으면 1로 리셋, 아니면 +1 을 SQL 한 문장으로 수행한다
        (읽고-쓰는 두 단계로 나누면 동시 인스턴스 사이에 레이스가 생긴다).

        [T8, PR 리뷰 라운드 6] **DB 성공 경로가 폴백의 진행분을 흡수한다.** DB 가 간헐적으로
        실패/성공을 오가면(pg 순단·네트워크 flap 등) 실패한 호출은 인메모리 폴백에 쌓이고
        성공한 호출은 DB 에 쌓여, 두 저장소가 각자 자기 몫만 세면 실제 연속 실패 횟수보다
        낮은 값 사이를 오간다 — DB 행 자체는 지워지지 않고 성공한 bump 마다 단조 증가하므로
        **정지가 아니라 지연**이지만(DB 성공률 p 면 대략 1/p 배 느리게라도 상한엔 도달한다),
        지연은 실재하고 고칠 값은 싸다. 이번 DB 성공 직전까지 같은 (kind, key) 로 폴백에
        쌓인 진행분을 ``peek`` 하고(TTL 은 폴백의 ``bump()`` 와 동일 기준 — 끊겼으면 0),
        **이번 호출 자체가 그 진행분 위에 이어지는 다음 1회**이므로 ``peek 값 + 1`` 과 DB
        가 자체적으로 계산한 값 중 **더 큰 쪽을 최종 스트릭으로 삼는다**(둘 다 폴백이 비어
        있으면 항상 DB 값과 같아 기존 동작과 다르지 않다). 더 크면 그 값으로 DB 를 맞춰
        UPDATE 해 이후 DB 단독 경로가 이어받게 하고, 어느 쪽이든 폴백 엔트리는 흡수 완료로
        지운다(같은 진행분을 두 번 세지 않는다). 흡수 UPDATE 가 실패하면 이 메서드를 감싼
        예외 처리로 자연히 흘러가 폴백이 이어받는다(psycopg 커넥션은 `with` 블록 예외 시
        트랜잭션 전체를 롤백하므로 방금 성공했던 원 UPSERT 도 함께 취소돼 이중 계수가
        생기지 않는다) — 이 함수는 여전히 절대 예외를 밖으로 내지 않는다.

        **남는 한계(알고 받아들인 것)**: 폴백을 흡수 후 비우기 때문에, 흡수 **직후** 다시
        DB 가 실패하면 그 다음 폴백 ``bump()`` 는(폴백 자신의 기록이 막 지워졌으므로) 0
        부터 다시 세기 시작한다 — 그래서 이 흡수는 임의의 교차 패턴에서 "정확히 호출
        횟수만큼"을 수학적으로 보장하지는 못하고, **상한 도달까지의 지연을 줄이는 것**까지만
        보장한다(실측: 실패→성공→실패→성공 4회에서 수정 전 2, 수정 후 3 — 상한 3 기준으로
        6회째가 아니라 4회째에 도달). 완벽한 재현(모든 교차 패턴에서 호출 수 = 스트릭)을
        하려면 폴백을 지우지 않고 흡수값으로 동기화해 둬야 하는데, 그러면 폴백이 실패
        전용 임시 저장소가 아니라 DB 의 상시 그림자 사본이 돼 이 이슈의 범위를 넘어선다.
        그 외 남는 한계는 ``clear_failure_streak`` docstring 의 "남는 드리프트 창"(실패한
        DELETE 로 DB 에 남는 stale streak) 뿐이다.
        """
        try:
            self._ensure_failure_streak_schema()
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO batch_failure_state (kind, state_key, streak, updated_at)
                    VALUES (%s, %s, 1, now())
                    ON CONFLICT (kind, state_key) DO UPDATE SET
                        streak = CASE
                            WHEN batch_failure_state.updated_at
                                < now() - make_interval(secs => %s) THEN 1
                            ELSE batch_failure_state.streak + 1 END,
                        updated_at = now()
                    RETURNING streak
                    """,
                    (kind, key, ttl_s),
                ).fetchone()
                streak = row[0]
                # [T8] 이 호출 자체가 폴백 진행분 위에 이어지는 "다음 1회"이므로 +1 해 비교한다
                # — 안 그러면(단순 max) 실패 1회 뒤 성공 1회처럼 폴백이 딱 1만큼만 앞서 있는
                # 흔한 패턴에서 DB 자체 값을 절대 못 넘어서 흡수가 사실상 죽은 코드가 된다.
                fallback_progress = self._failure_streak_fallback.peek(kind, key, ttl_s=ttl_s) + 1
                if fallback_progress > streak:
                    # [T8] 폴백 쪽 진행분이 더 크다 — DB 를 그 값으로 맞춰 이어받게 한다.
                    conn.execute(
                        """
                        UPDATE batch_failure_state SET streak = %s, updated_at = now()
                        WHERE kind = %s AND state_key = %s
                        """,
                        (fallback_progress, kind, key),
                    )
                    streak = fallback_progress
                # [T8] 흡수 완료(또는 애초에 폴백이 더 작았음) — 두 번 세지 않도록 지운다.
                self._failure_streak_fallback.clear(kind, key)
            self._mark_failure_streak_healthy()
            return streak
        except Exception:  # noqa: BLE001 - 실패 격리 폴백(#416) — 배치를 죽이지 않는다
            self._warn_failure_streak_fallback()
            return self._failure_streak_fallback.bump(kind, key, ttl_s=ttl_s)

    def clear_failure_streak(self, kind: str, key: str) -> None:
        """DB DELETE 를 시도하고, 성공 여부와 무관하게 인메모리 폴백 표도 항상 clear 한다.

        [F5, PR 리뷰 라운드 1] 폴백을 latch 하지 않고(#416 이 고치려던 잦은 재시작 시나리오를
        도로 무력화하므로) 매 호출 DB↔인메모리를 오가는 구조상, "성공 처리 후 DELETE 만 DB
        오류"가 나면 인메모리만 clear 되고 DB 쪽엔 stale streak 이 남는 드리프트가 생긴다.
        인메모리 clear 는 dict pop 이라 비용이 0 이므로 DB 결과와 무관하게 항상 적용해
        (bump 는 성공했는데 clear 만 실패하는) 드리프트의 절반을 없앤다.

        **남는 드리프트 창(알고 받아들인 한계)**: DELETE 자체가 실패하면 DB 쪽에는 stale
        streak 이 남는다 — 다음 단일 실패가 그 stale 값 위에 누적돼 실제보다 빨리 상한에
        닿아 오격리될 수 있다. 이 창은 막지 않는다(폴백을 프로세스 수명 latch 로 승격하면
        #416 의 "재시작이 잦아도 유계 수렴" 목표가 무력화된다). 대신
        ``artifacts_batch_failure_streak_ttl_s``(기본 1h) 로 유계이고, 결과는 dead-letter
        ERROR 로그로 드러나며 ``run_batch --full`` 로 복구 가능하다.
        """
        try:
            self._ensure_failure_streak_schema()
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM batch_failure_state WHERE kind = %s AND state_key = %s",
                    (kind, key),
                )
            self._mark_failure_streak_healthy()
        except Exception:  # noqa: BLE001 - 실패 격리 폴백(#416)
            self._warn_failure_streak_fallback()
        finally:
            self._failure_streak_fallback.clear(kind, key)

    def purge_stale_failure_streaks(self, ttl_s: float) -> int:
        try:
            self._ensure_failure_streak_schema()
            with self._pool.connection() as conn:
                rows = conn.execute(
                    """
                    DELETE FROM batch_failure_state
                    WHERE updated_at < now() - make_interval(secs => %s)
                    RETURNING 1
                    """,
                    (ttl_s,),
                ).fetchall()
            self._mark_failure_streak_healthy()
            return len(rows)
        except Exception:  # noqa: BLE001 - 실패 격리 폴백(#416)
            self._warn_failure_streak_fallback()
            return self._failure_streak_fallback.purge_stale(ttl_s)

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
        self,
        query_vec: list[float],
        *,
        k: int,
        exclude: set[int] | None = None,
        include: set[int] | None = None,
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

        include 가 주어지면 `product_id = ANY(...)`로 후보 집합 안에서만 순위화한다(#254).
        빈 집합은 DB 왕복 없이 빈 결과다. 실 pg-catalog 300건 후보는 PK Index Scan, 3,000건은
        Seq Scan 뒤 top-N heapsort를 택해 HNSW 근사가 아닌 정확 탐색이었고, 상위 120은 기존
        파이썬 코사인 순서와 완전히 같았다. 규모가 커져 HNSW를 택해도 위 strict_order 설정이
        필터를 통과한 결과를 충분히 찾을 때까지 탐색을 잇는다.

        statement_timeout — 호출측 asyncio.wait_for(504 변환)와 이중 방어다. to_thread 취소는
        밑에서 도는 쿼리를 죽이지 못해, DB 쪽 상한이 없으면 지연 쿼리가 풀 커넥션을 계속 붙들어
        후속 요청까지 말려든다. SET LOCAL 이라 트랜잭션 밖으로 새지 않는다.
        """
        from app.core.config import get_settings  # noqa: PLC0415 - 순환 임포트 회피(모듈 관례)

        if include == set():
            return []
        timeout_ms = int(get_settings().catalog_store_query_timeout_s * 1000)
        qvec = Vector(query_vec)
        predicates = ["embedding IS NOT NULL"]
        predicate_params: list = []
        if include is not None:
            predicates.append("product_id = ANY(%s::bigint[])")
            predicate_params.append(list(include))
        if exclude:
            predicates.append("product_id <> ALL(%s::bigint[])")
            predicate_params.append(list(exclude))
        # `IS NULL OR` 죽은 분기를 SQL에 두면 플래너가 인덱스 경로를 잡기 어렵다. 고정 술어
        # 조각만 조립하고 값은 전부 파라미터로 바인딩해 필요한 조건만 쿼리 모양에 남긴다.
        where_clause = " AND ".join(predicates)
        sql = f"""
            SELECT product_id, embedding <=> %s AS dist
            FROM products
            WHERE {where_clause}
            ORDER BY embedding <=> %s
            LIMIT %s
            """
        params = (qvec, *predicate_params, qvec, k)
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
