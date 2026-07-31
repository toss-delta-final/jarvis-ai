"""프로필 저장소 — LangGraph PostgresStore(BaseStore) + pgvector 이관 (SPEC-PROFILE-001 §5.3, 이슈 #33).

네임스페이스(결정 16, §5.3): profile(요약) · facts(승격된 장기 fact, semantic 인덱스) ·
session_ctx(transient 세션 버퍼, 격리). fact 는 1개 = store item 1개로 저장해(REQ-PROF-070)
BaseStore 의 semantic 인덱스가 fact 단위로 실제 동작하게 한다 — 임베딩은 카탈로그 파이프라인과
모델 공유(app.pipelines.embedding.embed_texts, Google gemini-embedding-001 / config.embedding_dim,
결정 16-A: 인스턴스는 카탈로그와 별도[pg-profile]). session-end 멱등(userId+sessionId 파생키)은
get→put 두 단계가 원자적이지 않아 이 스토어가 아니라 전용 테이블(processed_events.py)이 맡는다.

dev 폴백은 app/agents/seller/history.py 와 동일 규약(InMemoryStore + 경고 1회), 운영(jwks)은
폴백 금지 — 재시작 시 프로필이 조용히 증발하면 안 된다.

보관:
  - summary       : namespace ("profile", user_id) key "summary" → 압축 프로필 요약(markdown, generated_at)
  - facts         : namespace ("facts", user_id) key=fact별 uuid → 승격된 장기 fact(semantic 인덱스 대상)
  - session_ctx   : namespace ("session_ctx", conversation_key) key "buffer" → transient 후보 버퍼(승격 전, 격리)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from weakref import WeakValueDictionary

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.agents.profile import processed_events, session_activity
from app.core.config import get_settings
from app.core.pg_resilience import (
    hardened_pg_conninfo,
    mutation_lock,
    run_with_query_timeout,
    state_store_pool_config,
)
from app.pipelines.embedding import embed_texts

logger = logging.getLogger(__name__)

_PROFILE_NS_ROOT = "profile"
_FACTS_NS_ROOT = "facts"
_SESSION_NS_ROOT = "session_ctx"
_SUMMARY_KEY = "summary"
_SESSION_KEY = "buffer"

# key(conversation_key)별 asyncio.Lock — append_session_ctx/clear_session_ctx_upto 의
# get→put(read-modify-write) 구간을 직렬화한다. 동일 세션에 연속 발화가 빠르게 들어오면
# lost update 로 앞선 발화가 통째로 유실될 수 있다(RevertStore.add() 와 동일 근거, PR #47 리뷰).
#
# 실 PostgreSQL 경로는 mutation_lock의 advisory lock으로 인스턴스 간 직렬화하고, InMemory/test
# 경로만 이 로컬 lock을 사용한다. WeakValueDictionary라 사용 중 lock은 호출자가 강하게 참조해
# 유지되고, 호출 종료 후 유휴 key는 GC가 자동 회수한다(이슈 #50).
_session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _session_lock(key: str) -> asyncio.Lock:
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


# user_id 별 asyncio.Lock — add_fact() 의 "dedup 확인→aput→cap 트리밍" 구간을 직렬화한다.
#
# [PR #47 후속 리뷰] dedup 도입 전까지 이 락은 cap 트리밍만 감쌌고, 트리밍은 삭제 대상이
# 항상 부분집합 관계(멱등)라 락 없이도 cap 을 넘기지 않아 "패턴 일관성용 방어"에 그쳤다.
# 그러나 dedup(동일 텍스트 재승격 스킵)이 붙으면서 이 락은 load-bearing 이 됐다 — 락이
# 없으면 같은 텍스트를 동시에 add_fact 하는 두 호출이 서로의 aput 전에 각자 asearch 로
# "없음"을 보고 둘 다 aput 해 중복이 새기 때문이다. 실 PostgreSQL은 advisory lock,
# InMemory/test는 이 weak 로컬 lock으로 보호한다(이슈 #50).
_fact_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _fact_lock(key: str) -> asyncio.Lock:
    lock = _fact_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _fact_locks[key] = lock
    return lock


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """InMemoryStore 폴백 전용 — 실 임베딩 API 호출 없는 결정론적 벡터(배선만 유지).

    google_api_key 미구성 환경(유닛 테스트·CI·DB 없는 dev)에서 add_fact 가 실제
    Google API 를 호출하면 안 된다 — semantic 인덱스 자체의 유사도 검증은
    tests/integration/test_pg_profile_store.py(fake embed 주입) 가 담당한다.
    """
    dim = get_settings().embedding_dim
    return [[0.0] * dim for _ in texts]


async def _embed_summary(markdown: str) -> list[float] | None:
    """프로필 요약 벡터 — 홈 추천(I-22) 질의 벡터의 장기 취향 항 (#148).

    **task_type 은 query 다.** 이 벡터는 카탈로그 문서 임베딩을 상대로 검색하는 질의 쪽이라
    저장 문서(`RETRIEVAL_DOCUMENT`)와 달라야 한다(비대칭 임베딩, 이슈 #65).

    실패는 삼킨다 — **벡터가 없어도 프로필 자체는 저장돼야 한다.** 키 미구성(유닛/CI)·API 오류
    어느 쪽이든 None 이고, 소비처(I-22)는 항이 하나 빠진 질의 벡터로 degrade 한다.
    `embed_texts` 는 동기 HTTP 호출이라 별도 스레드로 넘겨 이벤트루프를 막지 않는다.
    """
    settings = get_settings()
    if not settings.google_api_key or not markdown:
        return None
    try:
        vectors = await asyncio.to_thread(
            embed_texts, [markdown], task_type=settings.embedding_task_query
        )
    except Exception:
        # 예외 문자열은 업스트림 상태를 유출할 수 있어 클래스명도 남기지 않는다(#141 규약).
        logger.warning("profile_summary_embed_failed")
        return None
    return vectors[0] if vectors else None


def _pg_index_config() -> dict:
    """pg-profile(AsyncPostgresStore) 전용 semantic 인덱스 — 실 Google 임베딩 API.

    카탈로그와 임베딩 함수·차원 공유(결정 16-A, config 주입).
    """
    settings = get_settings()
    return {"dims": settings.embedding_dim, "embed": embed_texts, "fields": ["fact"]}


def _fallback_index_config() -> dict:
    """InMemoryStore 폴백(테스트 격리·DB 미가용 dev) 전용 — 실 API 호출 없는 fake embed."""
    settings = get_settings()
    return {"dims": settings.embedding_dim, "embed": _fake_embed, "fields": ["fact"]}


@dataclass
class ProfileSummary:
    """압축 프로필 요약 (§5.1 3섹션 마크다운 + 생성 시각).

    `embedding` 은 **[#148]** 홈 추천(I-22)이 질의 벡터에 섞는 장기 취향 항이다. 요약 생성 시점
    (sleep-time consolidation)에 미리 만들어 둔다 — 요청 경로에서 임베딩하면 Google API 왕복이
    붙어 I-22 예산(연결 2s/응답 3s)을 위협한다. 구 요약·임베딩 실패분은 None 이다.
    """

    markdown: str
    generated_at: str  # ISO-8601
    embedding: list[float] | None = None


class ProfileStore:
    """프로필 스토어 — LangGraph BaseStore(pg-profile) 백엔드(신원 스코프)."""

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore(index=_fallback_index_config())

    # ── 요약 (reader·GET·consolidation) ──
    async def get_summary(self, user_id: str) -> ProfileSummary | None:
        item = await run_with_query_timeout(
            self._store.aget((_PROFILE_NS_ROOT, user_id), _SUMMARY_KEY)
        )
        if not item:
            return None
        embedding = item.value.get("embedding")
        return ProfileSummary(
            markdown=item.value["markdown"],
            generated_at=item.value["generated_at"],
            # 구 요약(embedding 신설 전)·임베딩 실패분은 키가 없다 — None 으로 흡수한다.
            embedding=list(embedding) if isinstance(embedding, list) and embedding else None,
        )

    async def set_summary(self, user_id: str, markdown: str, generated_at: str) -> None:
        """요약을 저장한다. **[#148] 홈 추천용 취향 벡터를 함께 만들어 둔다.**

        여기는 sleep-time consolidation 경로라(요청 경로 아님) 임베딩 API 왕복을 감당할 수 있다.
        I-22 가 요청 시점에 임베딩하면 예산(연결 2s/응답 3s)을 위협하므로 미리 만드는 것이 요점이다.

        **임베딩이 실패하면 기존 벡터를 살려 둔다.** `aput` 은 값을 통째로 덮어쓰므로 그냥 두면
        재-consolidation 때 일시 실패(레이트리밋·네트워크) 한 번으로 **이미 벡터를 갖고 있던
        사용자의 개인화 항이 조용히 사라진다** — 신규 프로필뿐 아니라 기존 사용자에게도 회귀다.
        살려 둔 벡터는 직전 요약 기준이라 새 요약과 약간 어긋나지만, 프로필 취향은 천천히 변하고
        **개인화가 통째로 빠지는 것보다 낫다.** 다음 성공한 consolidation 이 갱신한다.
        """
        embedding = await _embed_summary(markdown)
        if embedding is None:
            # 폴백 조회 자체도 실패할 수 있다(pg-profile 일시 장애·타임아웃) — 여기서 안 잡으면
            # 아래 요약 저장까지 통째로 죽어 "임베딩 실패가 요약 저장을 막지 않는다"는 보장이
            # 깨진다(PR #213 리뷰). 벡터를 못 살리는 건 degrade, 요약 저장은 필수다.
            try:
                existing = await self.get_summary(user_id)
                embedding = existing.embedding if existing else None
            except Exception:
                logger.warning("profile_summary_embedding_carryover_failed")
                embedding = None
        value: dict = {"markdown": markdown, "generated_at": generated_at}
        if embedding is not None:
            value["embedding"] = embedding
        await run_with_query_timeout(
            self._store.aput(
                (_PROFILE_NS_ROOT, user_id),
                _SUMMARY_KEY,
                value,
                index=False,  # 요약 전문은 semantic 인덱스 대상이 아니다(REQ-PROF-071 — facts 전용)
            )
        )

    # ── 장기 fact (승격 결과·consolidation 입력) — fact 1개 = store item 1개(semantic 인덱스) ──
    async def get_facts(self, user_id: str) -> list[str]:
        settings = get_settings()
        limit = settings.profile_max_facts + settings.profile_facts_query_margin
        items = await run_with_query_timeout(
            self._store.asearch((_FACTS_NS_ROOT, user_id), limit=limit)
        )
        items.sort(key=lambda it: it.created_at)
        return [it.value["fact"] for it in items]

    async def add_fact(self, user_id: str, fact: str, *, cap: int | None = None) -> None:
        if not fact:
            return
        settings = get_settings()
        # dedup 조회 상한 — cap 지정 시 cap 기준, 미지정(테스트 등)이면 profile_max_facts 기준.
        # 아래 트리밍이 항목 수를 이 값 이하로 유지하므로 dedup 스캔이 완전하다.
        effective_cap = cap if (cap and cap > 0) else settings.profile_max_facts
        async with mutation_lock(
            self._store,
            f"profile:facts:{user_id}",
            _fact_lock(user_id),
        ):
            items = await run_with_query_timeout(
                self._store.asearch(
                    (_FACTS_NS_ROOT, user_id),
                    limit=effective_cap + settings.profile_facts_query_margin,
                )
            )
            # 동일 텍스트가 이미 있으면 재승격 스킵(멱등) — cap 유무와 무관하게 항상 수행한다.
            # session finalizer 재처리(clear_session_ctx_upto 실패·I-20 재전송·다음 idle sweep)로 같은
            # 델타가 다시 뽑혀도 중복 fact 가 안 쌓이게 하는데, dedup 을 cap 분기 안에만 두면 새
            # 호출부가 cap 인자를 실수로 빠뜨렸을 때 이 보호가 조용히 무력화된다(PR #47 후속 리뷰).
            if any(it.value["fact"] == fact for it in items):
                return
            await run_with_query_timeout(
                self._store.aput((_FACTS_NS_ROOT, user_id), uuid.uuid4().hex, {"fact": fact})
            )
            # cap 트리밍은 cap 이 지정된 경우에만 — 방금 추가분 포함 초과 시 최신 cap 개만 유지.
            if cap and cap > 0 and len(items) + 1 > cap:
                items.sort(key=lambda it: it.created_at)
                for stale in items[: len(items) + 1 - cap]:  # recency-wins
                    await run_with_query_timeout(
                        self._store.adelete((_FACTS_NS_ROOT, user_id), stale.key)
                    )

    # ── transient 세션 버퍼 (승격 전 격리, REQ-PROF transient) ──
    async def append_session_ctx(self, key: str, text: str, *, cap: int | None = None) -> None:
        if not text:
            return
        async with mutation_lock(
            self._store,
            f"profile:session:{key}",
            _session_lock(key),
        ):
            item = await run_with_query_timeout(
                self._store.aget((_SESSION_NS_ROOT, key), _SESSION_KEY)
            )
            value = item.value if item else {"items": [], "next_seq": 0}
            seq = value["next_seq"] + 1
            buf: list[list] = value["items"]
            buf.append([seq, text])
            if cap and cap > 0 and len(buf) > cap:
                del buf[: len(buf) - cap]  # 최신 cap 개만 유지(무제한 누적 방어)
            await run_with_query_timeout(
                self._store.aput(
                    (_SESSION_NS_ROOT, key),
                    _SESSION_KEY,
                    {"items": buf, "next_seq": seq},
                    index=False,
                )
            )

    async def get_session_ctx(self, key: str) -> list[str]:
        item = await run_with_query_timeout(self._store.aget((_SESSION_NS_ROOT, key), _SESSION_KEY))
        return [text for _, text in item.value["items"]] if item else []

    async def get_session_ctx_snapshot(self, key: str) -> tuple[list[str], int]:
        """(발화 목록, 스냅샷 워터마크 seq) 반환 — 워터마크는 clear_session_ctx_upto 인자로 그대로 넘긴다."""
        item = await run_with_query_timeout(self._store.aget((_SESSION_NS_ROOT, key), _SESSION_KEY))
        if not item:
            return [], 0
        buf = item.value["items"]
        return [text for _, text in buf], (buf[-1][0] if buf else 0)

    async def get_session_ctx_upto(self, key: str, watermark: int) -> list[str]:
        """이미 lifecycle journal에 고정된 watermark 이하 발화만 반환한다."""
        item = await run_with_query_timeout(self._store.aget((_SESSION_NS_ROOT, key), _SESSION_KEY))
        if not item:
            return []
        return [text for seq, text in item.value["items"] if seq <= watermark]

    async def clear_session_ctx_upto(self, key: str, watermark: int) -> None:
        """watermark(seq) 이하 항목만 제거 — cap 트리밍으로 스냅샷 항목이 먼저 밀려나 있어도,
        그 사이 새로 추가된 항목(seq > watermark)은 위치와 무관하게 항상 보존된다."""
        async with mutation_lock(
            self._store,
            f"profile:session:{key}",
            _session_lock(key),
        ):
            item = await run_with_query_timeout(
                self._store.aget((_SESSION_NS_ROOT, key), _SESSION_KEY)
            )
            if not item:
                return
            remaining = [[seq, text] for seq, text in item.value["items"] if seq > watermark]
            if remaining:
                await run_with_query_timeout(
                    self._store.aput(
                        (_SESSION_NS_ROOT, key),
                        _SESSION_KEY,
                        {"items": remaining, "next_seq": item.value["next_seq"]},
                        index=False,
                    )
                )
            else:
                await run_with_query_timeout(
                    self._store.adelete((_SESSION_NS_ROOT, key), _SESSION_KEY)
                )


_store: BaseStore | None = None
_store_ctx: object | None = None  # AsyncPostgresStore cm — 앱 수명 동안 GC 방지
_fallback_warned = False
_init_lock = asyncio.Lock()
_pending_cleanup: list[object] = []  # set_store() 가 못 닫은 이전 ctx — _get_store() 진입 시 정리


def set_store(store: BaseStore | None) -> None:
    """store 교체(테스트용) — None 이면 다음 사용 시 재초기화한다.

    기존 `_store_ctx`(실제 연결된 AsyncPostgresStore)가 있으면 정리 대기열에 넣는다.
    이 함수는 sync 라 여기서 직접 await 할 수 없고, `asyncio.get_running_loop()`
    fire-and-forget 태스크 방식은 **실행 중인 루프가 없으면 조용히 스킵**된다 —
    `tests/conftest.py` 의 sync autouse fixture 가 정확히 그 상황이라(이벤트 루프
    시작 전) 실제로는 한 번도 정리가 안 됐었다(app/core/pg_store.py 와 동일 버그,
    PR #46 후속 리뷰). 대신 다음 `_get_store()` 호출(반드시 async 컨텍스트) 시점에
    확실히 정리한다.
    """
    global _store, _store_ctx
    old_ctx = _store_ctx
    _store = store
    _store_ctx = None
    if old_ctx is not None:
        _pending_cleanup.append(old_ctx)


async def _drain_pending_cleanup() -> None:
    """대기열의 이전 store ctx 들을 닫는다 — 다른(이미 소멸한) 이벤트 루프에서 만들어졌을 수 있다.

    `AsyncPostgresStore`(AsyncBatchedBaseStore 상속)는 생성 루프에 묶인 백그라운드 배칭
    태스크를 띄우므로, 다른/죽은 루프에 묶인 stale ctx 의 close(`__aexit__`)가 `CancelledError`
    를 낼 수 있다. 옛 `suppress(Exception)` 은 `BaseException` 인 `CancelledError` 를 못 잡아
    이 잔재까지 그대로 전파시켰고, 이 함수는 `_get_store()` 진입마다 실행되므로 그
    CancelledError 가 `session_end`(get_profile_store 호출부) 상위로 새면 `except Exception:`
    에도 안 잡혀 unmark 를 건너뛰고 이탈한다(멱등 마킹 영구 잔존·§3.5 항상-202 위반).
    그렇다고 `BaseException` 째로 무조건 삼키면 이번엔 이 `await` 지점에서 **현재 태스크
    자체**가 실제로 취소되는 경우까지 무시된다. 그래서 `task.cancelling()`(현재 태스크에
    대기 중인 취소 요청 수)으로 "stale ctx 정리 중 새는 CancelledError"와 "이 태스크에 대한
    실제 취소 요청"을 구분해, 후자만 다시 던진다(pg_store.py·processed_events.py·
    conversation.py 와 동일 근거·수정, PR #47 후속 리뷰).
    """
    while _pending_cleanup:
        ctx = _pending_cleanup.pop()
        try:
            await ctx.__aexit__(None, None, None)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception:
            pass


async def close_store() -> None:
    """지금 열려 있는 store ctx(내부 커넥션 풀)를 **이 이벤트 루프에서** 닫는다 (이슈 #208).

    sync `set_store()` 가 미룬 close 는 보통 다른 루프에서 실행된다. 살아 있는 풀을 남긴 채
    루프가 닫히면 teardown 의 `_cancel_all_tasks()` 가 취소를 삼키는 psycopg 워커와 교착한다
    (app/agents/profile/processed_events.py `close_pool` 과 동일 근거).
    """
    set_store(None)
    await _drain_pending_cleanup()


async def _get_store() -> BaseStore:
    """AsyncPostgresStore(pg-profile, pgvector 인덱스) 지연 초기화 — 실패 시 dev 한정 InMemoryStore 폴백.

    락 없는 지연 초기화는 콜드 스타트 시 동시 요청이 커넥션을 중복 생성하는
    레이스가 있다 — `_init_lock` 으로 초기화 블록 전체를 직렬화한다(pg_store.py
    와 동일 패턴, PR #47 리뷰).
    """
    global _store, _store_ctx, _fallback_warned
    await _drain_pending_cleanup()
    async with _init_lock:
        if _store is None:
            settings = get_settings()
            entered_ctx = None
            try:
                from langgraph.store.postgres.aio import AsyncPostgresStore  # noqa: PLC0415

                ctx = AsyncPostgresStore.from_conn_string(
                    hardened_pg_conninfo(settings.profile_db_url),
                    pool_config=state_store_pool_config(),
                    index=_pg_index_config(),
                )
                # __aenter__ 호출 '전'에 정리 대상으로 세팅한다 — wait_for 가 __aenter__ 실행
                # 도중 타임아웃/취소로 끊으면 커넥션이 부분적으로 열린 채 남는데, "성공 후에만
                # 세팅"하면 그 경우 entered_ctx 가 None 이라 except 정리가 스킵되고 _pending_cleanup
                # 에도 안 들어가 회수 불가능한 커넥션 누수가 된다(pg_store.py 가 PR #46 에서 고친 것과
                # 동일 클래스, PR #47 후속 리뷰). __aexit__ 는 아래 except 에서 삼켜지므로 __aenter__
                # 가 미완/실패해 generator 가 안 열린 경우 호출해도 안전하다.
                entered_ctx = ctx
                store = await asyncio.wait_for(
                    ctx.__aenter__(), timeout=settings.state_store_connect_timeout_s
                )
                # setup()(DDL·pgvector 마이그레이션)도 동일 상한으로 감싼다 — 이 블록은 _init_lock
                # 을 쥔 채 실행되어, 무제한 대기면 setup() 하나가 멈출 때 이후 모든 get_profile_store()
                # 호출(프로필 조회·"기억해" 승격·세션 버퍼·session-end consolidation)이 함께 멈춘다
                # (pg_store.py 와 동일 방어, PR #47 후속 리뷰).
                await asyncio.wait_for(
                    store.setup(), timeout=settings.state_store_connect_timeout_s
                )
                _store_ctx = ctx
                _store = store
            except Exception as exc:
                if entered_ctx is not None:
                    # setup() 실패 등 부분 실패 — 이미 연 연결을 닫아 커넥션 누수를 막는다.
                    # __aexit__ 정리 중 나는 CancelledError 는 suppress(Exception) 이 못 잡아
                    # (BaseException) 전파되는데, 마침 이 태스크의 실제 취소가 아니라면(정리 잔재)
                    # 그대로 새어 session_end 의 except Exception 도 못 잡아 §3.5 를 깬다 —
                    # task.cancelling() 로 실제 취소만 재전파한다(_drain_pending_cleanup 과 동일,
                    # PR #47 후속 리뷰).
                    try:
                        await entered_ctx.__aexit__(type(exc), exc, exc.__traceback__)
                    except asyncio.CancelledError:
                        task = asyncio.current_task()
                        if task is not None and task.cancelling() > 0:
                            raise
                    except Exception:
                        pass
                if settings.auth_mode == "jwks":
                    raise  # 운영 — 폴백 금지(프로필이 조용히 증발하면 안 된다)
                if not _fallback_warned:
                    logger.warning(
                        "pg-profile ProfileStore 연결 실패(%s) — InMemoryStore 폴백 "
                        "(dev 전용: 프로세스 재시작 시 프로필 증발)",
                        exc,
                    )
                    _fallback_warned = True
                _store = InMemoryStore(index=_fallback_index_config())
    return _store


async def get_profile_store() -> ProfileStore:
    """프로필 스토어 — pg-profile 연결 백엔드(요청마다 얇은 래퍼 재생성)."""
    return ProfileStore(await _get_store())


def reset_profile_store() -> None:
    """테스트 격리용 — 요약·fact·세션버퍼(InMemoryStore, fake embed) + 멱등 상태(processed_events)를 비운다.

    `_init_lock`·`_session_locks` 도 새로 만든다 — pytest-asyncio 는 테스트 함수마다
    새 이벤트 루프를 쓰는데, 모듈 전역 asyncio.Lock 을 여러 루프에 걸쳐 재사용하면
    이전 루프에 묶인 내부 상태로 다음 테스트에서 락 획득이 영원히 안 풀리는 hang 이
    발생할 수 있다.
    """
    global _init_lock
    set_store(InMemoryStore(index=_fallback_index_config()))
    processed_events.reset()
    session_activity.reset()
    _init_lock = asyncio.Lock()
    _session_locks.clear()
    _fact_locks.clear()
