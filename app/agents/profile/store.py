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
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from weakref import WeakValueDictionary

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError

from app.agents.profile import graph_journal, processed_events, session_activity
from app.agents.profile.graph_models import GraphDocument
from app.core.config import get_settings
from app.core.logging import safe_fingerprint
from app.core.pg_resilience import (
    hardened_pg_conninfo,
    mutation_lock,
    run_with_query_timeout,
    state_store_pool_config,
)
from app.core.pii import contains_hard_pii, redact
from app.pipelines.embedding import embed_texts

logger = logging.getLogger(__name__)

_PROFILE_NS_ROOT = "profile"
_FACTS_NS_ROOT = "facts"
_SESSION_NS_ROOT = "session_ctx"
_GRAPH_NS_ROOT = "graph"
_SUMMARY_KEY = "summary"
_SESSION_KEY = "buffer"
# 그래프는 사용자당 항목 **1개**다(SPEC-PROFILE-GRAPH-149 §7.1) — per-user advisory 잠금이 별도
# 연결 풀에서 잡혀 store 트랜잭션과 결합되지 않아 다중 항목 원자성이 없고, N개로 쪼개면 전부
# 찢어진 쓰기 상태를 만든다. 키를 "v1" 로 고정하는 것이 그 단일성의 표현이다.
_GRAPH_KEY = "v1"
# 전체 초기화가 훑는 fact 상한. `profile_max_facts`(200) 보다 넉넉히 잡아, cap 이 커진 뒤에도
# "일부만 지워졌다"가 조용히 생기지 않게 한다 — 초기화는 남기면 안 되는 동작이다.
_PURGE_SCAN_LIMIT = 10_000


def _as_iso(value: object) -> str:
    """store item 의 created_at 을 ISO-8601 문자열로 — 백엔드가 datetime/str 중 무엇을 줘도 같게.

    InMemoryStore 와 AsyncPostgresStore 가 같은 타입을 준다는 보장이 없는데, 병합이 이 값을
    정렬 키로 쓰므로(REQ-PGRAPH-015) 타입이 섞이면 비교 자체가 터진다.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_utterance(text: str) -> str:
    """세션 버퍼 반복 판정용 정규화 (#119, REQ-PROF-026).

    앞뒤 공백·연속 공백·대소문자만 접는다 — 조사/어미까지 건드리는 과한 정규화는 서로 다른
    발화를 병합해 **정당한 취향 신호를 잃는다**. 의미 유사 dedup 은 임베딩이 필요하고 척도
    타당성을 실측해야 하므로(docs/lessons.md 2026-07-30) 여기서 하지 않는다.
    """
    return " ".join(text.split()).casefold()


def _fact_or_triples_contain_hard_pii(fact: str, graph_triples: list[dict] | None) -> bool:
    """add_fact 초크포인트 판정 (이슈 #321) — fact 원문뿐 아니라 triple 의 `node.label`·
    `node.resolution.anchor_phrase` 도 검사한다(payload 모양은 `resolver.ResolvedTriple.as_payload`).
    `anchorPhrase` 는 `_DELTA_SYSTEM` 이 "발화에서 그대로 인용"하라고 지시하므로 fact 텍스트가
    깨끗해도 이쪽에 원문 PII 가 남을 수 있다.
    """
    if contains_hard_pii(fact):
        return True
    for triple in graph_triples or ():
        node = triple.get("node") if isinstance(triple, dict) else None
        if not isinstance(node, dict):
            continue
        if contains_hard_pii(node.get("label")):
            return True
        resolution = node.get("resolution")
        if isinstance(resolution, dict) and contains_hard_pii(resolution.get("anchor_phrase")):
            return True
    return False


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


# user_id 별 asyncio.Lock — set_summary() 의 "임베딩 carryover read(get_summary)→aput" 구간을
# 직렬화한다. facts 락(_fact_locks)과 키를 분리한 이유(#323):
#
# set_summary 의 RMW 대상은 profile 네임스페이스의 summary 아이템뿐이고 add_fact 의 RMW 대상은
# facts 네임스페이스라 겹치는 상태가 없다 — 필요한 상호 배제는 "요약 쓰기 ↔ 요약 쓰기"(배치
# consolidation vs #150 사용자 편집)뿐이다. facts 키를 공유하면 record_remember hot-path 의
# add_fact 가 요약 쓰기와 불필요하게 직렬화된다. 반대로 "fact 를 읽은 시점과 요약을 쓰는 시점
# 사이의 fact 변경"까지 막으려면 consolidate() 가 LLM 왕복(수 초) 내내 이 락을 쥐어야 하는데,
# 그건 hot-path 를 초 단위로 막는 트레이드오프라 채택하지 않는다 — 그 정합성은 #150/#358
# (revision CAS·억제 필터) 축에서 다룬다.
_summary_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _summary_lock(key: str) -> asyncio.Lock:
    lock = _summary_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _summary_locks[key] = lock
    return lock


# user_id 별 asyncio.Lock — 그래프 문서(단일 jsonb)의 read-modify-write 를 직렬화한다 (#356).
#
# 락 키를 facts·summary 와 분리하는 이유는 #323 과 같다: RMW 대상 상태가 겹치지 않는다.
# **그래프 락을 쥔 채 set_summary 를 부르지 않는다** — set_summary 는 스스로 summary 락을
# 잡으므로(#323) 중첩하면 advisory 풀(전역 state_store_pool_max_size)에서 커넥션을 동시에
# 둘 점유하게 되고, 동시 세션 종료 몇 건이면 풀이 말라 구매자 턴 경로(append_session_ctx)까지
# 3초 타임아웃으로 죽는다. consolidate() 는 그래프 락을 놓은 뒤 요약을 쓴다.
_graph_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _graph_lock(key: str) -> asyncio.Lock:
    lock = _graph_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _graph_locks[key] = lock
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
    # 단조 증가 쓰기 seq — 요약 compare-and-set 의 비교 대상이다(#323 잔여, #358).
    # 잠금은 "동시에 쓰는 것"만 막는데, `consolidate` 는 그래프 락을 놓고 LLM 왕복(수 초)을 한
    # 뒤에 쓰므로 그 창의 사용자 편집을 **시간상 겹치지 않은 채** 덮는다. 그건 CAS 로만 닫힌다.
    # 구 요약은 키가 없어 0 으로 흡수된다.
    seq: int = 0
    # 개인화 중지가 내리는 사용 표식(REQ-PGRAPH-100). 본문은 그대로 두고 이 값만 내린다 —
    # 중지는 삭제가 아니라서, 지우면 다시 켰을 때 되살릴 것이 없다. 구 요약은 True 로 흡수.
    usable: bool = True


@dataclass
class FactRecord:
    """승격된 fact 하나 + 그래프 병합에 필요한 메타 (#356).

    `get_facts()` 가 돌려주는 `list[str]` 로는 그래프를 만들 수 없다:
      - `fact_key` — `GraphEdge.evidence_refs` 가 **fact key 참조**다(SPEC-PROFILE-GRAPH-149 §5.2).
      - `created_at` — 병합이 관측을 `(observed_at, fact_key)` 오름차순으로 처리한다(REQ-PGRAPH-015).
      - `graph_triples` — 쓰기 시점에 resolver 가 확정한 트리플. 배치마다 다시 resolve 하면
        거리 임계·통제 어휘가 바뀔 때 같은 fact 가 다른 node_id 로 붙어 tombstone 을 우회한다
        (REQ-PGRAPH-010, SPEC-PROFILE-001 REQ-PROF-086 v0.7.1 보강).
    """

    fact_key: str
    fact: str
    created_at: str  # ISO-8601
    graph_triples: list[dict]


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
            # 신설 필드도 같은 규약으로 흡수한다 — 없다고 기존 사용자의 프로필이 사라지면 안 된다.
            seq=int(item.value.get("seq", 0)),
            usable=bool(item.value.get("usable", True)),
        )

    async def set_summary(
        self,
        user_id: str,
        markdown: str,
        generated_at: str,
        *,
        expected_seq: int | None = None,
    ) -> bool:
        """요약을 저장한다. **[#148] 홈 추천용 취향 벡터를 함께 만들어 둔다.**

        여기는 sleep-time consolidation 경로라(요청 경로 아님) 임베딩 API 왕복을 감당할 수 있다.
        I-22 가 요청 시점에 임베딩하면 예산(연결 2s/응답 3s)을 위협하므로 미리 만드는 것이 요점이다.

        **임베딩이 실패하면 기존 벡터를 살려 둔다.** `aput` 은 값을 통째로 덮어쓰므로 그냥 두면
        재-consolidation 때 일시 실패(레이트리밋·네트워크) 한 번으로 **이미 벡터를 갖고 있던
        사용자의 개인화 항이 조용히 사라진다** — 신규 프로필뿐 아니라 기존 사용자에게도 회귀다.
        살려 둔 벡터는 직전 요약 기준이라 새 요약과 약간 어긋나지만, 프로필 취향은 천천히 변하고
        **개인화가 통째로 빠지는 것보다 낫다.** 다음 성공한 consolidation 이 갱신한다.

        **[#323] carryover read(get_summary)~aput 구간은 per-user 락으로 감싼다** — 배치
        consolidation 과 (#150 이후) 사용자 편집이 동시에 set_summary 를 부르면 무잠금 RMW 라
        먼저 읽은 쪽의 aput 이 나중에 실행돼 상대 쓰기를 소리 없이 덮어쓸 수 있다. `_embed_summary`
        (외부 Google API 왕복)는 저장된 상태를 읽지 않으므로 락 범위 밖에 둔다 — 락 안에 넣으면
        API 왕복(초 단위) 동안 다른 요약 쓰기가 불필요하게 막힌다.

        **[#358] `expected_seq` 를 주면 compare-and-set 이다.** 잠금은 "동시에 쓰는 것"만 막는데,
        `consolidate` 는 그래프 락을 놓고 LLM 왕복(수 초)을 한 뒤에 쓰므로 그 창의 사용자 편집을
        **시간상 겹치지 않은 채** 덮는다(SPEC §7.4 "남은 부분"). 읽은 시점의 `seq` 를 지참하면
        그 사이 바뀐 경우 쓰지 않고 `False` 를 돌려준다. `None` 이면 종전대로 무조건 쓴다.

        `expected_seq=0` 과 `None` 은 **다른 뜻**이다 — 0 은 "요약이 없는 것을 봤다"이고 None 은
        "검사하지 않는다"다. 0 을 falsy 로 흘려보내면 첫 요약 경합에서만 CAS 가 조용히 꺼진다.

        돌려주는 값은 "실제로 썼는가"다.
        """
        # [이슈 #321] `_embed_summary` 가 외부(Google) API 로 나가기 **직전** 마지막 관문이다 —
        # 임베딩 호출보다 먼저 검사해야 PII 가 외부로 나가지 않는다. 히트하면 쓰지 않고 기존
        # 요약을 그대로 둔다("빈 요약으로 덮지 않는다" 규칙과 같은 취지 — 억제의 정반대 상태를
        # 만들지 않는다).
        if contains_hard_pii(markdown):
            logger.warning(
                "profile_summary_pii_blocked", extra={"user_fp": safe_fingerprint(user_id)}
            )
            return False
        embedding = await _embed_summary(markdown)
        async with mutation_lock(
            self._store,
            f"profile:summary:{user_id}",
            _summary_lock(user_id),
        ):
            # **무조건 읽는다**(#359). 종전에는 `embedding is None or expected_seq is not None`
            # 일 때만 읽었는데, 그러면 임베딩이 성공하고 CAS 를 안 쓰는 호출에서 `existing` 이
            # `None` 인 채로 아래 `usable` 이 `True` 로 리셋된다 — 개인화 중지가 조용히 풀린다.
            # 지금 프로덕션이 안전한 유일한 이유는 `builder.consolidate` 가 늘 `expected_seq=` 를
            # 넘기기 때문이고, **호출자 한 명의 규율에 [HARD] 보장을 걸어 둔 상태**였다.
            # 추가 비용은 sleep-time 배치의 get 1회다.
            #
            # 폴백 조회 자체도 실패할 수 있다(pg-profile 일시 장애·타임아웃) — 여기서 안 잡으면
            # 아래 요약 저장까지 통째로 죽어 "임베딩 실패가 요약 저장을 막지 않는다"는 보장이
            # 깨진다(PR #213 리뷰). 벡터를 못 살리는 건 degrade, 요약 저장은 필수다.
            existing: ProfileSummary | None = None
            try:
                existing = await self.get_summary(user_id)
            except Exception:
                logger.warning("profile_summary_embedding_carryover_failed")
                existing = None
            if embedding is None:
                embedding = existing.embedding if existing else None

            current_seq = existing.seq if existing else 0
            if expected_seq is not None and expected_seq != current_seq:
                # 내가 읽은 뒤로 바뀌었다 — 낡은 스냅샷으로 만든 요약이 사용자 편집을 덮지 않게
                # 여기서 멈춘다. 호출부가 다시 읽고 판단한다.
                logger.warning("profile_summary_cas_conflict")
                return False

            value: dict = {
                "markdown": markdown,
                "generated_at": generated_at,
                "seq": current_seq + 1,
                # 사용 표식은 승계한다 — 요약을 새로 썼다고 개인화 중지가 풀리면 안 된다
                # (REQ-PGRAPH-063 과 같은 취지).
                "usable": existing.usable if existing else True,
            }
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
            return True

    async def mark_summary_usable(self, user_id: str, usable: bool) -> None:
        """요약의 사용 표식만 내리거나 올린다 — 본문·벡터는 건드리지 않는다 (REQ-PGRAPH-100).

        개인화 중지가 부르는 경로다. 중지는 **삭제가 아니라서** 본문을 지우면 사용자가 다시
        켰을 때 되살릴 것이 없다. 요약이 없으면 조용히 넘어간다 — 중지 토글이 프로필 유무에
        따라 실패하면 안 된다.

        `seq` 는 올리지 않는다. 이 쓰기는 배치와 경합하는 내용 변경이 아니라 표식 전환이고,
        올리면 진행 중인 배치의 정당한 CAS 를 이유 없이 실패시킨다.
        """
        async with mutation_lock(
            self._store,
            f"profile:summary:{user_id}",
            _summary_lock(user_id),
        ):
            item = await run_with_query_timeout(
                self._store.aget((_PROFILE_NS_ROOT, user_id), _SUMMARY_KEY)
            )
            if not item:
                return
            value = dict(item.value)
            if bool(value.get("usable", True)) == usable:
                return
            value["usable"] = usable
            await run_with_query_timeout(
                self._store.aput((_PROFILE_NS_ROOT, user_id), _SUMMARY_KEY, value, index=False)
            )

    # ── 장기 fact (승격 결과·consolidation 입력) — fact 1개 = store item 1개(semantic 인덱스) ──
    async def get_facts(self, user_id: str) -> list[str]:
        return [record.fact for record in await self.get_fact_records(user_id)]

    async def get_fact_records(self, user_id: str) -> list[FactRecord]:
        """fact + key·생성 시각·확정 트리플 (#356).

        `get_facts()` 를 깨지 않고 더한다 — 호출부 대부분이 문자열만 필요하고, 시그니처를 바꾸면
        회귀 표면만 넓어진다. 정렬은 `created_at` 오름차순이라 병합의 관측 순서와 같다
        (REQ-PGRAPH-015).
        """
        settings = get_settings()
        limit = settings.profile_max_facts + settings.profile_facts_query_margin
        items = await run_with_query_timeout(
            self._store.asearch((_FACTS_NS_ROOT, user_id), limit=limit)
        )
        items.sort(key=lambda it: it.created_at)
        return [
            FactRecord(
                fact_key=it.key,
                fact=it.value["fact"],
                created_at=_as_iso(it.created_at),
                # 구 fact(전환 이전 저장분)에는 이 필드가 없다 — 없는 것이 정상이고
                # 투영되지 않은 채 unprojected_count 로만 집계된다(REQ-PGRAPH-004).
                graph_triples=list(it.value.get("graph_triples") or []),
            )
            for it in items
        ]

    async def add_fact(
        self,
        user_id: str,
        fact: str,
        *,
        cap: int | None = None,
        graph_triples: list[dict] | None = None,
    ) -> None:
        if not fact:
            return
        # [이슈 #321] 초크포인트(심층 방어) — fact 뿐 아니라 triple 의 label·anchorPhrase 도
        # 검사한다(`_DELTA_SYSTEM` 이 anchorPhrase 를 "발화 그대로 인용"하라고 지시하므로 여기가
        # 두 번째 문이다). 어느 한쪽만 히트해도 **통째로 버린다**(REQ-PGRAPH-071 "파생 취향도
        # 만들지 않는다") — fact 만 살리고 triple 만 버리는 절충은 하지 않는다.
        if _fact_or_triples_contain_hard_pii(fact, graph_triples):
            logger.warning("profile_fact_pii_blocked", extra={"user_fp": safe_fingerprint(user_id)})
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
            existing = next((it for it in items if it.value["fact"] == fact), None)
            if existing is not None:
                # **트리플이 비어 있으면 채운다** — resolver 는 임베딩 백엔드 장애를 예외 전파
                # 대신 드롭으로 처리하므로(그래야 배치가 영구 RETRYABLE 이 안 된다) 장애 중에는
                # 트리플 없는 fact 가 저장된다. 여기서 무조건 return 하면 복구 후 같은 취향이
                # 다시 승격돼도 새 트리플이 버려져 **일시적 장애가 영구 손실**이 되고, 그 취향은
                # 계속 unprojected 로만 잡혀 그래프에 영영 안 실린다(PR #410 리뷰).
                # 채우는 것은 값뿐이고 항목은 그대로라, 중복 방지(PR #47)는 유지된다.
                if graph_triples and not existing.value.get("graph_triples"):
                    await run_with_query_timeout(
                        self._store.aput(
                            (_FACTS_NS_ROOT, user_id),
                            existing.key,
                            {**existing.value, "graph_triples": graph_triples},
                        )
                    )
                return
            # fact 항목은 증거 저장소로 유지하고 **값에 필드만 더한다**(SPEC-PROFILE-GRAPH-149
            # §7.1) — fact 쪽 스키마 마이그레이션은 없다. 트리플이 없으면 키를 아예 넣지 않아
            # 기존 항목과 모양이 같다(구 fact 와 신 fact 를 저장 레벨에서 구분하지 않는다).
            value: dict = {"fact": fact}
            if graph_triples:
                value["graph_triples"] = graph_triples
            await run_with_query_timeout(
                self._store.aput((_FACTS_NS_ROOT, user_id), uuid.uuid4().hex, value)
            )
            # cap 트리밍은 cap 이 지정된 경우에만 — 방금 추가분 포함 초과 시 최신 cap 개만 유지.
            if cap and cap > 0 and len(items) + 1 > cap:
                items.sort(key=lambda it: it.created_at)
                for stale in items[: len(items) + 1 - cap]:  # recency-wins
                    await run_with_query_timeout(
                        self._store.adelete((_FACTS_NS_ROOT, user_id), stale.key)
                    )

    # ── 개인화 그래프 문서 (#356, SPEC-PROFILE-GRAPH-149 §5.3·§7.1) ──
    async def purge_personal_data(self, user_id: str) -> dict[str, int]:
        """전체 초기화 — fact·요약·세션버퍼를 물리 삭제한다 (#358, REQ-PGRAPH-061).

        **그래프 문서와 전사록은 여기서 지우지 않는다.** 그래프는 호출부가 `revision` 을 이어받아
        **교체**해야 하고(빈 문서로 지우면 revision 이 0 으로 되돌아간다 — REQ-PGRAPH-042),
        전사록은 다른 저장소(pg-profile `conversation_turns`)라 소유자가 다르다.

        세션 버퍼는 `("session_ctx", "{user_id}:{session_id}")` 라 사용자로 열거해야 찾을 수
        있다 — 네임스페이스를 훑어 접두어가 맞는 것만 지운다. 다른 사용자의 버퍼를 건드리지
        않도록 접두어는 `:` 까지 포함해 비교한다("35" 가 "358:..." 에 걸리지 않게).
        """
        counts = {"facts": 0, "summary": 0, "buffers": 0}

        async with mutation_lock(self._store, f"profile:facts:{user_id}", _fact_lock(user_id)):
            items = await run_with_query_timeout(
                self._store.asearch((_FACTS_NS_ROOT, user_id), limit=_PURGE_SCAN_LIMIT)
            )
            for item in items:
                await run_with_query_timeout(
                    self._store.adelete((_FACTS_NS_ROOT, user_id), item.key)
                )
                counts["facts"] += 1

        async with mutation_lock(self._store, f"profile:summary:{user_id}", _summary_lock(user_id)):
            if await self.get_summary(user_id) is not None:
                await run_with_query_timeout(
                    self._store.adelete((_PROFILE_NS_ROOT, user_id), _SUMMARY_KEY)
                )
                counts["summary"] = 1

        prefix = f"{user_id}:"
        namespaces = await run_with_query_timeout(
            self._store.alist_namespaces(prefix=(_SESSION_NS_ROOT,), max_depth=2)
        )
        for namespace in namespaces:
            if len(namespace) < 2 or not namespace[1].startswith(prefix):
                continue
            async with mutation_lock(
                self._store, f"profile:session:{namespace[1]}", _session_lock(namespace[1])
            ):
                await run_with_query_timeout(self._store.adelete(namespace, _SESSION_KEY))
            counts["buffers"] += 1

        return counts

    async def delete_facts_backing(self, user_id: str, edge_ids: set[str]) -> int:
        """주어진 edge 를 근거로 하는 fact 를 **물리 삭제**한다 (#358, REQ-PGRAPH-025 [HARD]).

        사용자가 edge 를 지우면 라벨뿐 아니라 **그 근거 fact 원문까지** 그 자리에서 사라져야
        한다 — 안 그러면 "지웠다"고 믿는 문장이 저장소에 그대로 남는다.

        **한 fact 가 여러 취향을 담고 그중 하나만 지워졌어도 통째로 지운다.** 그 원문에는 지운
        취향이 그대로 적혀 있어 살려 두면 다음 배치가 다시 읽는다 — 삭제가 이긴다. `_summary_input`
        이 이미 같은 규칙(부분 일치도 통째 제외)을 쓰고 있어 두 경로가 일관된다.

        판정은 fact 자신의 트리플로 한다 — edge 의 `evidence_refs` 는 `graph_evidence_refs_max`
        로 잘린 저장용 목록이라, 그걸 쓰면 상한을 넘겨 언급된 오래된 근거가 지워지지 않고 남는다.
        """
        if not edge_ids:
            return 0
        removed = 0
        async with mutation_lock(self._store, f"profile:facts:{user_id}", _fact_lock(user_id)):
            items = await run_with_query_timeout(
                self._store.asearch((_FACTS_NS_ROOT, user_id), limit=_PURGE_SCAN_LIMIT)
            )
            for item in items:
                triples = item.value.get("graph_triples") or []
                if not any(triple.get("edge_id") in edge_ids for triple in triples):
                    continue
                await run_with_query_timeout(
                    self._store.adelete((_FACTS_NS_ROOT, user_id), item.key)
                )
                removed += 1
        return removed

    def graph_lock(self, user_id: str) -> AbstractAsyncContextManager[None]:
        """그래프 문서 RMW 직렬화 잠금.

        **이 잠금을 쥔 채 `set_summary` 를 부르지 않는다** — 그쪽도 자기 잠금을 잡으므로(#323)
        중첩하면 advisory 풀에서 커넥션을 동시에 둘 점유한다. 풀은 전역
        `state_store_pool_max_size` 하나이고 구매자 턴의 `append_session_ctx` 도 같은 풀을 쓴다.
        """
        return mutation_lock(self._store, f"profile:graph:{user_id}", _graph_lock(user_id))

    async def get_graph(self, user_id: str) -> GraphDocument | None:
        """사용자 그래프 문서. 없으면 None(첫 배치 전 정상 상태)."""
        if not user_id:
            return None
        item = await run_with_query_timeout(self._store.aget((_GRAPH_NS_ROOT, user_id), _GRAPH_KEY))
        if item is None:
            return None
        try:
            return GraphDocument.model_validate(item.value)
        except ValidationError:
            # 스키마가 안 맞는 문서(구 형식·손상)로 배치를 죽이지 않는다 — 없는 것으로 보고
            # 다음 병합이 fact 증거에서 다시 만든다. fact 가 정본 증거라 복원이 가능하다.
            logger.warning("profile_graph_document_invalid")
            return None

    async def set_graph(self, user_id: str, document: GraphDocument) -> None:
        """문서 전체를 재작성한다 — 버전 단위가 사용자당 그래프 전체다(REQ-PGRAPH-041).

        `index=False`: 그래프 문서는 semantic 인덱스 대상이 아니다(REQ-PROF-071 — facts 전용).
        명시하지 않으면 인덱스 설정의 `fields` 가 바뀌는 순간 조용히 임베딩 대상이 된다.
        """
        if not user_id:
            return
        await run_with_query_timeout(
            self._store.aput(
                (_GRAPH_NS_ROOT, user_id),
                _GRAPH_KEY,
                document.model_dump(mode="json"),
                index=False,
            )
        )

    # ── transient 세션 버퍼 (승격 전 격리, REQ-PROF transient) ──
    async def append_session_ctx(
        self, key: str, text: str, *, cap: int | None = None, repeat_cap: int | None = None
    ) -> None:
        if not text:
            return
        # [이슈 #321] 여기는 저장물이 아니라 델타 추출 LLM 의 입력이다 — 치환하면 그 LLM 이
        # 원문 숫자를 애초에 못 봐서, 모델이 fact/label/anchorPhrase 로 옮겨 적는 세탁 경로가
        # 구조적으로 닫힌다(드롭이 아니라 치환인 이유).
        text, _ = redact(text)
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
            if repeat_cap and repeat_cap > 0:
                normalized = _normalize_utterance(text)
                seen = sum(1 for _, t in buf if _normalize_utterance(t) == normalized)
                if seen >= repeat_cap:
                    # 반복 **횟수**가 취향 강도로 환산되는 걸 상한한다(#119, REQ-PROF-026).
                    # 전부 지우지 않는 이유: 게이트가 `explicit OR repeated` 라 반복은 명시 표명
                    # 없이 승격시키는 독립 경로다 — 1 건만 남기면 그 경로가 죽는다.
                    # put 자체를 하지 않으므로 next_seq 가 그대로라 워터마크 불변식이 안전하다.
                    # 발화 원문·user_id 는 로그에 싣지 않는다(PII).
                    logger.info(
                        "profile_buffer_repeat_capped",
                        extra={"repeat_cap": repeat_cap, "buffered": len(buf)},
                    )
                    return
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


async def _drain_pending_cleanup(*, propagate_errors: bool = False) -> None:
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
    first_error: Exception | None = None
    while _pending_cleanup:
        ctx = _pending_cleanup.pop()
        try:
            await ctx.__aexit__(None, None, None)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception as exc:
            logger.warning("profile store context cleanup failed", exc_info=True)
            if first_error is None:
                first_error = exc
    if propagate_errors and first_error is not None:
        raise first_error


async def close_store() -> None:
    """지금 열려 있는 store ctx(내부 커넥션 풀)를 **이 이벤트 루프에서** 닫는다 (이슈 #208).

    sync `set_store()` 가 미룬 close 는 보통 다른 루프에서 실행된다. 살아 있는 풀을 남긴 채
    루프가 닫히면 teardown 의 `_cancel_all_tasks()` 가 취소를 삼키는 psycopg 워커와 교착한다
    (app/agents/profile/processed_events.py `close_pool` 과 동일 근거).
    """
    set_store(None)
    await _drain_pending_cleanup(propagate_errors=True)


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
    graph_journal.reset()  # 감사·멱등 원장·중지 플래그(#358)도 같은 격리 경계에 든다
    _init_lock = asyncio.Lock()
    _session_locks.clear()
    _fact_locks.clear()
    _summary_locks.clear()
    _graph_locks.clear()
