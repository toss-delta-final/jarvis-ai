"""프로필 파이프라인 (이슈 #6, SPEC-PROFILE-001) — reader·gate·builder·멱등·transient.

LLM(델타·consolidation)은 주입형 fake 로 구동(라이브 Anthropic 불필요). POST /events/session-end 는
TestClient. 저장소는 pg-profile BaseStore(테스트는 InMemoryStore,
conftest reset — 이슈 #33).
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging

import jwt
import pytest
from fastapi.testclient import TestClient

from app.agents.profile import finalizer as profile_finalizer
from app.agents.profile import processed_events
from app.agents.profile.finalizer import (
    ActiveProfileTaskRegistry,
    ProfilePhaseResult,
    ProfilePhaseStatus,
    process_profile_checkpoint,
)
from app.agents.profile.builder import (
    ConsolidationResult,
    consolidate,
    generate_session_delta,
    record_remember,
)
from app.agents.profile.gate import is_remember_command, should_promote
from app.agents.profile.reader import read_profile_summary
from app.agents.profile.store import ProfileStore, get_profile_store
from app.core.config import get_settings
from app.core import session_context
from app.core.conversation import conversation_key
from app.core.llm import LLMError
from app.main import app
from app.schemas.profile import SessionEndEvent

client = TestClient(app)


def _member_bearer(sub: str) -> dict:
    token = jwt.encode({"sub": sub}, "test-secret-key-0123456789abcdef", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


class _ProfileLLM:
    """델타 추출(JSON)·consolidation(마크다운)을 system 프롬프트로 분기하는 fake."""

    def __init__(self, deltas=None, summary="# 취향 요약\n- 3~5만원 무선이어폰 선호") -> None:
        self._deltas = (
            deltas
            if deltas is not None
            else [
                {
                    "fact": "3~5만원 무선이어폰 선호",
                    "salience": 0.9,
                    "explicit": True,
                    "repetitionEma": 0.0,
                }
            ]
        )
        self._summary = summary

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        if "델타 추출기" in system:
            return json.dumps({"deltas": self._deltas}, ensure_ascii=False)
        return self._summary

    async def stream(self, *, system, user, tier, max_tokens=1024):
        yield "x"


# ─────────── reader ───────────


async def test_reader_none_for_guest_and_missing() -> None:
    assert await read_profile_summary(None) is None
    assert await read_profile_summary("") is None
    assert await read_profile_summary("999") is None  # 미보유


async def test_reader_returns_stored_summary() -> None:
    store = await get_profile_store()
    await store.set_summary("5", "# 요약\n- x", "2026-07-20T00:00:00+00:00")
    got = await read_profile_summary("5")
    assert got and got["markdown"] == "# 요약\n- x" and got["generated_at"].startswith("2026")


async def test_summary_embedding_survives_a_failed_re_embed(monkeypatch) -> None:
    """[#148] 재-consolidation 때 임베딩이 실패해도 **기존 벡터를 잃지 않는다**.

    `aput` 은 값을 통째로 덮어쓰므로, 실패 시 embedding 키를 빼면 이미 벡터를 갖고 있던 사용자의
    홈 추천(I-22) 개인화 항이 조용히 사라진다 — 신규 프로필뿐 아니라 기존 사용자에게도 회귀다.
    """
    from app.agents.profile import store as store_mod

    async def _ok(_markdown: str) -> list[float]:
        return [0.5, 0.5]

    async def _fail(_markdown: str) -> None:
        return None

    store = await get_profile_store()
    monkeypatch.setattr(store_mod, "_embed_summary", _ok)
    await store.set_summary("7", "# 요약 v1", "2026-07-20T00:00:00+00:00")
    assert (await store.get_summary("7")).embedding == [0.5, 0.5]

    monkeypatch.setattr(store_mod, "_embed_summary", _fail)
    await store.set_summary("7", "# 요약 v2", "2026-07-21T00:00:00+00:00")
    got = await store.get_summary("7")
    assert got.markdown == "# 요약 v2", "요약 본문은 갱신된다"
    assert got.embedding == [0.5, 0.5], "임베딩 실패는 기존 벡터를 지우지 않는다"


async def test_summary_save_survives_carryover_lookup_failure(monkeypatch) -> None:
    """벡터 살리기용 조회가 실패해도 **요약 저장은 성공한다** — 조회 실패는 degrade 다.

    폴백 `get_summary` 를 안 감싸면 pg-profile 일시 장애 때 요약 텍스트 저장까지 함께 죽는다 —
    PR 이전엔 성공했을 저장이 실패하는 회귀(PR 리뷰).
    """
    from app.agents.profile import store as store_mod

    async def _fail_embed(_markdown: str) -> None:
        return None

    monkeypatch.setattr(store_mod, "_embed_summary", _fail_embed)
    store = await get_profile_store()
    orig_get_summary = store.get_summary

    async def _boom(user_id: str):
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(store, "get_summary", _boom)
    await store.set_summary("9", "# 요약", "2026-07-20T00:00:00+00:00")  # 예외가 없어야 한다

    monkeypatch.setattr(store, "get_summary", orig_get_summary)
    got = await store.get_summary("9")
    assert got.markdown == "# 요약", "요약 저장은 조회 실패와 무관하게 성공한다"
    assert got.embedding is None


async def test_summary_embedding_is_none_when_never_embedded(monkeypatch) -> None:
    """기존 벡터가 없으면 그대로 None — 살릴 것이 없다."""
    from app.agents.profile import store as store_mod

    async def _fail(_markdown: str) -> None:
        return None

    monkeypatch.setattr(store_mod, "_embed_summary", _fail)
    store = await get_profile_store()
    await store.set_summary("8", "# 요약", "2026-07-20T00:00:00+00:00")
    assert (await store.get_summary("8")).embedding is None


# ─────────── gate ───────────


def test_should_promote_gate_rule() -> None:
    # salient AND (explicit OR repeated)
    assert should_promote(salience=0.8, explicit=True, repetition_ema=0.0)  # 명시
    assert should_promote(salience=0.8, explicit=False, repetition_ema=0.7)  # 반복
    assert not should_promote(
        salience=0.8, explicit=False, repetition_ema=0.1
    )  # 현저하나 미반복·비명시
    assert not should_promote(salience=0.2, explicit=True, repetition_ema=0.9)  # 현저성 미달


def test_is_remember_command() -> None:
    assert is_remember_command("이거 기억해줘")
    assert is_remember_command("remember this please")
    assert not is_remember_command("무선 이어폰 추천해줘")
    assert not is_remember_command("저번에 뭐 담았는지 기억해?")  # 질문 오탐 제외
    assert not is_remember_command("이거 기억해두면 좋을까?")
    assert not is_remember_command("어제 일 기억해내려고 했는데 잘 안 되네")  # 비명령 부분매칭 제외
    assert is_remember_command("이거 기억해두세요")
    assert is_remember_command(
        "매운맛 좋아해요, 기억해줘! 다른 것도 추천해줄래?"
    )  # 명령+질문 혼합도 인식
    assert not is_remember_command("이거 안 잊게 기억해줘야 할 것 같아")  # 활용형(줘야) 제외
    assert not is_remember_command("기억해줘도 상관없어")  # 활용형(줘도) 제외


# ─────────── builder (델타·consolidation) ───────────


async def test_generate_session_delta_promotes_via_gate() -> None:
    store = await get_profile_store()
    key = conversation_key("7", "s1")
    await store.append_session_ctx(key, "3만원대 무선 이어폰 위주로 보고 있어요")
    promoted, watermark = await generate_session_delta(
        "7", key, profile_watermark=1, llm=_ProfileLLM(), settings=get_settings()
    )
    assert promoted == ["3~5만원 무선이어폰 선호"]
    assert watermark == 1
    assert "3~5만원 무선이어폰 선호" in await store.get_facts("7")


async def test_generate_session_delta_uses_only_supplied_watermark() -> None:
    store = await get_profile_store()
    key = conversation_key("7", "bounded")
    await store.append_session_ctx(key, "워터마크 안쪽")
    await store.append_session_ctx(key, "워터마크 바깥쪽")

    class _CapturingLLM(_ProfileLLM):
        prompt = ""

        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
            if "델타 추출기" in system:
                self.prompt = user
            return await super().complete(
                system=system,
                user=user,
                tier=tier,
                max_tokens=max_tokens,
                json_output=json_output,
            )

    llm = _CapturingLLM()
    promoted, watermark = await generate_session_delta(
        "7",
        key,
        profile_watermark=1,
        llm=llm,
        settings=get_settings(),
    )

    assert promoted == ["3~5만원 무선이어폰 선호"]
    assert watermark == 1
    assert llm.prompt == "워터마크 안쪽"


async def test_newer_append_cannot_drive_promoted_fact() -> None:
    store = await get_profile_store()
    key = conversation_key("7", "bounded-promotion")
    await store.append_session_ctx(key, "워터마크 안쪽 일반 대화")
    await store.append_session_ctx(key, "워터마크 바깥쪽 빨간색 선호")

    class _ConditionalLLM(_ProfileLLM):
        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
            if "델타 추출기" in system:
                deltas = (
                    [
                        {
                            "fact": "빨간색 선호",
                            "salience": 0.9,
                            "explicit": True,
                            "repetitionEma": 0.0,
                        }
                    ]
                    if "빨간색 선호" in user
                    else []
                )
                return json.dumps({"deltas": deltas}, ensure_ascii=False)
            return await super().complete(
                system=system,
                user=user,
                tier=tier,
                max_tokens=max_tokens,
                json_output=json_output,
            )

    promoted, watermark = await generate_session_delta(
        "7",
        key,
        profile_watermark=1,
        llm=_ConditionalLLM(),
        settings=get_settings(),
    )

    assert watermark == 1
    assert promoted == []
    assert "빨간색 선호" not in await store.get_facts("7")


async def test_active_profile_registry_joins_context_task_with_original_identity() -> None:
    registry = ActiveProfileTaskRegistry()
    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def first_factory() -> ProfilePhaseResult:
        entered.set()
        await proceed.wait()
        return ProfilePhaseResult(ProfilePhaseStatus.COMPLETED)

    first = asyncio.create_task(registry.join_or_start("ctx", "f1", "e1", first_factory))
    await entered.wait()
    second = asyncio.create_task(
        registry.join_or_start(
            "ctx",
            "f2",
            "e2",
            lambda: asyncio.sleep(0, result=ProfilePhaseResult(ProfilePhaseStatus.NO_WORK)),
        )
    )
    proceed.set()

    owner, joined = await asyncio.gather(first, second)
    assert owner.finalization_id == joined.finalization_id == "f1"
    assert owner.event_id == joined.event_id == "e1"
    assert owner.joined is False
    assert joined.joined is True


async def test_joiner_cancellation_does_not_cancel_shared_profile_task() -> None:
    registry = ActiveProfileTaskRegistry()
    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def factory() -> ProfilePhaseResult:
        entered.set()
        await proceed.wait()
        return ProfilePhaseResult(ProfilePhaseStatus.COMPLETED)

    owner = asyncio.create_task(registry.join_or_start("ctx", "f1", "e1", factory))
    await entered.wait()
    joiner = asyncio.create_task(registry.join_or_start("ctx", "f1", "e1", factory))
    await asyncio.sleep(0)
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joiner

    assert not owner.done()
    proceed.set()
    assert (await owner).result.status is ProfilePhaseStatus.COMPLETED
    assert registry._active == {}


async def test_owner_waiter_cancellation_keeps_background_task_and_cleans_registry() -> None:
    registry = ActiveProfileTaskRegistry()
    entered = asyncio.Event()
    proceed = asyncio.Event()
    completed = asyncio.Event()

    async def factory() -> ProfilePhaseResult:
        entered.set()
        await proceed.wait()
        completed.set()
        return ProfilePhaseResult(ProfilePhaseStatus.COMPLETED)

    owner = asyncio.create_task(registry.join_or_start("ctx", "f1", "e1", factory))
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    proceed.set()
    await completed.wait()
    await asyncio.sleep(0)
    assert registry._active == {}


async def test_shared_profile_task_cancellation_reaches_waiters_and_cleans_registry() -> None:
    registry = ActiveProfileTaskRegistry()
    entered = asyncio.Event()

    async def factory() -> ProfilePhaseResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    owner = asyncio.create_task(registry.join_or_start("ctx", "f1", "e1", factory))
    await entered.wait()
    joiner = asyncio.create_task(registry.join_or_start("ctx", "f1", "e1", factory))
    active = registry._active["ctx"]
    active.task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(asyncio.CancelledError):
        await joiner
    await asyncio.sleep(0)
    assert registry._active == {}


async def test_empty_bounded_checkpoint_is_success_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agents.profile.finalizer as profile_finalizer

    store = await get_profile_store()
    key = conversation_key("7", "trimmed")
    await store.append_session_ctx(key, "새 발화", cap=1)

    def _unexpected_llm():
        raise AssertionError("빈 bounded snapshot은 LLM을 호출하면 안 됨")

    monkeypatch.setattr(profile_finalizer, "get_llm", _unexpected_llm)
    result = await process_profile_checkpoint(
        7,
        "trimmed",
        event_id="chat-profile:ctx:1:idle",
        profile_watermark=0,
        settings=get_settings(),
    )

    assert result.status is ProfilePhaseStatus.NO_WORK
    assert await processed_events.get_status("chat-profile:ctx:1:idle") == "completed"
    assert await store.get_session_ctx(key) == ["새 발화"]


async def test_checkpoint_becomes_no_work_when_cap_trims_bounded_slice_midflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    class _Store:
        async def get_session_ctx_upto(self, key: str, watermark: int) -> list[str]:
            nonlocal reads
            reads += 1
            return ["old"] if reads == 1 else []

        async def clear_session_ctx_upto(self, key: str, watermark: int) -> None:
            raise AssertionError("watermark 밖 항목은 clear 대상이 아님")

    async def _delta(*args, **kwargs):
        return None

    monkeypatch.setattr(
        profile_finalizer, "get_profile_store", lambda: asyncio.sleep(0, result=_Store())
    )
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    monkeypatch.setattr(profile_finalizer, "generate_session_delta", _delta)

    result = await process_profile_checkpoint(
        7,
        "cap-trimmed-midflight",
        event_id="chat-profile:ctx:2:idle",
        profile_watermark=1,
        settings=get_settings(),
    )

    assert result.status is ProfilePhaseStatus.NO_WORK
    assert reads == 2


async def test_generate_session_delta_gate_rejects_low_signal() -> None:
    store = await get_profile_store()
    key = conversation_key("7", "s2")
    await store.append_session_ctx(key, "음")
    llm = _ProfileLLM(
        deltas=[{"fact": "잡담", "salience": 0.1, "explicit": False, "repetitionEma": 0.0}]
    )
    promoted, watermark = await generate_session_delta(
        "7", key, profile_watermark=1, llm=llm, settings=get_settings()
    )
    assert promoted == [] and await store.get_facts("7") == []  # 처리됨(non-None)이나 승격 0
    assert watermark == 1


async def test_clear_session_ctx_upto_preserves_concurrent_append() -> None:
    """LLM 호출(스냅샷~clear 사이)에 새로 추가된 발화는 clear_session_ctx_upto 가 지우지 않는다.

    session-end 처리 중 같은 세션에 새 턴이 들어오는 레이스(§events.session_end) 회귀 테스트.
    """
    store = await get_profile_store()
    key = conversation_key("7", "race")
    await store.append_session_ctx(key, "A")
    promoted, watermark = await generate_session_delta(
        "7", key, profile_watermark=1, llm=_ProfileLLM(), settings=get_settings()
    )
    assert promoted == ["3~5만원 무선이어폰 선호"] and watermark == 1
    # LLM 호출이 끝나고 clear 하기 전, 그 사이에 새 턴이 도착했다고 가정.
    await store.append_session_ctx(key, "B")
    await store.clear_session_ctx_upto(key, watermark)
    assert await store.get_session_ctx(key) == ["B"]  # A(스냅샷분)만 지워지고 B는 보존


async def test_clear_session_ctx_upto_survives_cap_eviction_during_llm_call() -> None:
    """LLM 호출 중 버퍼 상한(cap)을 넘겨 스냅샷 항목 자체가 앞에서 밀려나도, 그 이후 추가분은 보존된다.

    PR #37 리뷰 지적 — count(개수) 기반이면 cap 트리밍으로 위치가 밀린 새 항목을 스냅샷 항목으로
    착각해 잘못 지울 수 있었다. seq 워터마크 기반으로 바꿔 위치와 무관하게 안전하도록 회귀 방지.
    """
    store = await get_profile_store()
    key = conversation_key("7", "cap-race")
    await store.append_session_ctx(key, "A", cap=2)
    promoted, watermark = await generate_session_delta(
        "7", key, profile_watermark=1, llm=_ProfileLLM(), settings=get_settings()
    )
    assert promoted == ["3~5만원 무선이어폰 선호"] and watermark == 1
    # LLM 호출 중 cap(2)을 넘겨 A가 앞에서 밀려나는 상황(둘 다 미분석 상태).
    await store.append_session_ctx(key, "B", cap=2)
    await store.append_session_ctx(key, "C", cap=2)  # len=3 > cap=2 → A 트리밍됨, 버퍼=[B, C]
    await store.clear_session_ctx_upto(key, watermark)
    assert await store.get_session_ctx(key) == ["B", "C"]  # A는 이미 없었고, B/C는 미분석이라 보존


async def test_generate_session_delta_degrades_without_llm_or_buffer() -> None:
    # 버퍼 없음 → None(degrade 신호)
    assert (
        await generate_session_delta(
            "7",
            conversation_key("7", "empty"),
            profile_watermark=0,
            llm=_ProfileLLM(),
            settings=get_settings(),
        )
        is None
    )
    # LLM 미구성 → None
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("7", "s3"), "x")
    assert (
        await generate_session_delta(
            "7",
            conversation_key("7", "s3"),
            profile_watermark=1,
            llm=None,
            settings=get_settings(),
        )
        is None
    )


async def test_consolidate_writes_summary() -> None:
    store = await get_profile_store()
    await store.add_fact("8", "무선이어폰 선호")
    result = await consolidate("8", llm=_ProfileLLM(), settings=get_settings())
    assert result is ConsolidationResult.UPDATED
    summary = await store.get_summary("8")
    assert summary and "취향 요약" in summary.markdown and summary.generated_at


async def test_consolidate_degrades_without_facts() -> None:
    result = await consolidate("nofacts", llm=_ProfileLLM(), settings=get_settings())
    assert result is ConsolidationResult.NO_WORK


async def test_record_remember_hot_path() -> None:
    await record_remember("9", "겨울 등산 자주 감")
    store = await get_profile_store()
    assert "겨울 등산 자주 감" in await store.get_facts("9")


async def test_record_remember_caps_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "profile_fact_char_cap", 10)
    await record_remember("cap", "가" * 100)
    store = await get_profile_store()
    facts = await store.get_facts("cap")
    assert len(facts[0]) == 10


async def test_consolidate_respects_char_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "profile_summary_max_chars", 10)
    store = await get_profile_store()
    await store.add_fact("10", "x")
    await consolidate("10", llm=_ProfileLLM(summary="가" * 50), settings=get_settings())
    summary = await store.get_summary("10")
    assert len(summary.markdown) == 10


# ─────────── POST /events/session-end ───────────


async def test_session_end_202_and_processes_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    store = await get_profile_store()
    key = conversation_key("777", "sess-9")
    await store.append_session_ctx(key, "3만원 무선이어폰 찾아줘")
    payload = {"userId": 777, "sessionId": "sess-9", "reason": "logout"}
    r1 = client.post("/events/session-end", json=payload)
    assert r1.status_code == 202 and r1.json()["status"] == "accepted"
    # 프로필 생성됨 + 버퍼 정리됨
    assert await store.get_summary("777") is not None
    assert await store.get_session_ctx(key) == []


async def test_session_end_dedups_same_session_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """같은 terminal lifecycle 재전송은 generation journal 기준 duplicate다."""
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("777", "sess-dup"), "발화")
    first = client.post("/events/session-end", json={"userId": 777, "sessionId": "sess-dup"})
    duplicate = client.post("/events/session-end", json={"userId": 777, "sessionId": "sess-dup"})
    assert first.status_code == 202 and first.json()["status"] == "accepted"
    assert duplicate.status_code == 202 and duplicate.json()["status"] == "duplicate"


async def test_session_end_same_session_second_is_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 sessionId 의 두 번째 session-end 는 duplicate — 하나의 sessionId=하나의 논리적 종료.

    Spring 이 쏘는 종료(NEW_CONVERSATION·LOGOUT)는 모두 세션을 삭제하므로 세션당 한 번만 온다.
    같은 (userId, sessionId) 재수신은 at-least-once 재전송으로 보고 고정 멱등키로 중복 처리한다.
    """

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    store = await get_profile_store()
    key = conversation_key("900", "sess-multi")
    await store.append_session_ctx(key, "무선이어폰 3만원대")
    r1 = client.post("/events/session-end", json={"userId": 900, "sessionId": "sess-multi"})
    assert r1.json()["status"] == "accepted"
    assert await store.get_session_ctx(key) == []  # 처리·정리
    # 같은 sessionId 재수신(재전송) — 새 발화가 있어도 고정키로 중복 처리(세션당 1회 종료 전제)
    await store.append_session_ctx(key, "겨울 등산화도 볼래")
    r2 = client.post("/events/session-end", json={"userId": 900, "sessionId": "sess-multi"})
    assert r2.status_code == 202 and r2.json()["status"] == "duplicate"
    assert await store.get_session_ctx(key) == ["겨울 등산화도 볼래"]  # 중복이라 미처리·미정리


async def test_session_end_no_buffer_is_noop_accepted() -> None:
    """빈 버퍼도 신규 통지는 accepted 로 기록하며 재전송은 duplicate 로 판정한다."""
    payload = {"userId": 5, "sessionId": "empty-sess"}

    first = client.post("/events/session-end", json=payload)
    second = client.post("/events/session-end", json=payload)

    assert first.status_code == 202 and first.json()["status"] == "accepted"
    assert second.status_code == 202 and second.json()["status"] == "duplicate"
    context = await session_context._default_repository.get_context("empty-sess")
    assert context is not None
    assert await processed_events.seen_event(
        f"chat-profile:{context.context_id}:{context.generation}:terminal"
    )


def test_session_end_rejects_missing_identity() -> None:
    """userId·sessionId 누락은 400(§2.5) — 멱등키·프로필 스코프에 필수(이슈 #62)."""
    assert client.post("/events/session-end", json={"sessionId": "s"}).status_code == 400
    assert client.post("/events/session-end", json={"userId": 1}).status_code == 400


def test_session_end_rejects_empty_session_id() -> None:
    """빈 sessionId 는 400 — 필수 불투명 키(§3.5 essential), 퇴화 멱등키/버퍼 키 방어(PR #64 리뷰)."""
    assert (
        client.post("/events/session-end", json={"userId": 1, "sessionId": ""}).status_code == 400
    )


def test_session_end_reason_has_64_character_safety_cap() -> None:
    """reason은 enum을 강제하지 않되 서비스 경계에서 무제한 문자열은 거부한다."""
    accepted = client.post(
        "/events/session-end",
        json={"userId": 1, "sessionId": "reason-cap-ok", "reason": "r" * 64},
    )
    rejected = client.post(
        "/events/session-end",
        json={"userId": 1, "sessionId": "reason-cap-over", "reason": "r" * 65},
    )

    assert accepted.status_code == 202
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "BAD_REQUEST"


def test_session_end_rejects_userid_out_of_bigint_range() -> None:
    """userId 는 양의 BIGINT 범위만 허용 — 거대 정수 키 남용 방어(int 전환 후에도 유지)."""
    assert (
        client.post("/events/session-end", json={"userId": 0, "sessionId": "s"}).status_code == 400
    )
    assert (
        client.post("/events/session-end", json={"userId": 2**63, "sessionId": "s"}).status_code
        == 400
    )


@pytest.mark.parametrize("invalid_user_id", ["1", 1.0, True])
def test_session_end_rejects_non_integer_json_userid(invalid_user_id: object) -> None:
    """userId는 JSON number 정수만 허용하며 문자열·실수·bool 강제 변환은 하지 않는다."""
    response = client.post(
        "/events/session-end",
        json={"userId": invalid_user_id, "sessionId": "s"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_session_end_rejects_different_owner_for_terminal_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessionId lifecycle owner가 정해진 뒤 다른 userId의 I-20은 중복으로 위장하지 않는다."""
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("111", "shared-sess"), "x")
    r1 = client.post("/events/session-end", json={"userId": 111, "sessionId": "shared-sess"})
    r2 = client.post("/events/session-end", json={"userId": 222, "sessionId": "shared-sess"})
    assert r1.json()["status"] == "accepted"
    assert r2.status_code == 403


def test_session_end_service_token_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_mode", "jwks")  # 운영: 서비스 토큰 fail-closed
    monkeypatch.setattr(get_settings(), "internal_api_token", "secret-xyz")
    payload = {"userId": 1, "sessionId": "s"}
    # 토큰 없음 → 401
    assert client.post("/events/session-end", json=payload).status_code == 401
    # 일치 → 202
    r = client.post("/events/session-end", json=payload, headers={"X-Internal-Token": "secret-xyz"})
    assert r.status_code == 202


async def test_session_end_degrades_without_llm() -> None:
    """LLM 미구성이어도 세션 종료는 202(best-effort, 프로필 미갱신)."""
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("55", "s"), "x")
    r = client.post("/events/session-end", json={"userId": 55, "sessionId": "s"})
    assert r.status_code == 202
    assert await store.get_summary("55") is None  # LLM 없어 미갱신
    # degrade 시 transient 버퍼는 보존(성공 시에만 정리) — 회수 여지
    assert await store.get_session_ctx(conversation_key("55", "s")) != []


# ─────────── e2e (채팅 transient → 세션종료 → 저장) ───────────


async def test_end_to_end_profile_from_chat(monkeypatch: pytest.MonkeyPatch, buyer_fakes) -> None:
    """회원 채팅 → 세션 종료 → 프로필 생성."""
    import app.agents.profile.finalizer as profile_finalizer

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())
    hdr = _member_bearer("888")
    # 회원 채팅 1턴 → transient 버퍼 누적
    client.post(
        "/chat",
        json={"sessionId": "e2e", "threadId": "t", "message": "3만원 무선이어폰 추천"},
        headers=hdr,
    )
    store = await get_profile_store()
    assert await store.get_session_ctx(conversation_key("888", "e2e"))  # 버퍼에 쌓임
    # 세션 종료 → 델타·consolidation
    client.post("/events/session-end", json={"userId": 888, "sessionId": "e2e"})
    store = await get_profile_store()
    summary = await store.get_summary("888")
    assert summary is not None and "무선이어폰" in summary.markdown


def test_session_end_rejects_oversized_session_id() -> None:
    """sessionId 길이 상한 초과는 400(불투명 스레드 키·스토어 키 남용 방어)."""
    big = "x" * 100000
    r = client.post("/events/session-end", json={"userId": 1, "sessionId": big})
    assert r.status_code == 400  # 앱이 검증 오류를 400 봉투로 매핑(§2.5)


async def test_session_end_clears_buffer_on_normal_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 이 정상 실행됐으나 게이트가 전부 반려한 경우도 '처리됨'이라 버퍼를 정리한다(무한 보존 방지)."""

    low = _ProfileLLM(
        deltas=[{"fact": "잡담", "salience": 0.1, "explicit": False, "repetitionEma": 0.0}]
    )
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: low)
    key = conversation_key("66", "s")
    store = await get_profile_store()
    await store.append_session_ctx(key, "음 별로")
    r = client.post("/events/session-end", json={"userId": 66, "sessionId": "s"})
    assert r.status_code == 202
    assert await store.get_session_ctx(key) == []  # 정상 처리 → 버퍼 정리


async def test_add_fact_count_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """사용자별 fact 개수 상한 — 최신 cap 개만 유지(무제한 누적 방어)."""
    monkeypatch.setattr(get_settings(), "profile_max_facts", 3)
    for i in range(10):
        await record_remember("cap2", f"fact-{i}")
    store = await get_profile_store()
    facts = await store.get_facts("cap2")
    assert len(facts) == 3 and facts == ["fact-7", "fact-8", "fact-9"]


async def test_add_fact_dedup_skips_duplicate_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """동일 텍스트 재승격은 스킵된다(멱등) — session_end 재처리(clear 실패·재전송·다음 배치)로
    같은 델타가 다시 뽑혀도 중복 fact 가 누적되지 않는다(PR #47 후속 리뷰)."""
    monkeypatch.setattr(get_settings(), "profile_max_facts", 5)
    store = await get_profile_store()
    await store.add_fact("dup1", "좋아하는 색은 파랑", cap=5)
    await store.add_fact("dup1", "좋아하는 색은 파랑", cap=5)  # 동일 텍스트 재승격
    await store.add_fact("dup1", "알러지: 땅콩", cap=5)
    facts = await store.get_facts("dup1")
    assert facts.count("좋아하는 색은 파랑") == 1  # 중복 저장 안 됨
    assert set(facts) == {"좋아하는 색은 파랑", "알러지: 땅콩"}


async def test_add_fact_dedup_without_cap() -> None:
    """cap 미지정이어도 동일 텍스트 재승격은 스킵된다 — 호출부가 cap 인자를 실수로 빠뜨려도
    dedup·무제한 누적 방어가 조용히 무력화되지 않는다(PR #47 후속 리뷰)."""
    store = await get_profile_store()
    await store.add_fact("nocap", "탄산수 좋아함")
    await store.add_fact("nocap", "탄산수 좋아함")  # cap 없이 동일 텍스트 재승격
    assert (await store.get_facts("nocap")).count("탄산수 좋아함") == 1


async def test_set_summary_serializes_concurrent_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """배치 consolidation 과 사용자 편집이 동시에 set_summary 를 부르면(#323), 락이 없으면
    무잠금 read-then-write(carryover) 경합으로 나중에 aput 하는 쪽이 이긴다 — 순서상 사용자
    편집(B)이 이겨야 하는데 락이 없으면 먼저 멈춘 배치(A)가 나중에 덮어써 유실될 수 있다.

    유닛 환경은 google_api_key 미구성이라 `_embed_summary` 가 None → set_summary 가 carryover
    read(get_summary) 경로를 타므로, 그 aget 호출을 계측해 결정론적으로 경합을 재현한다.
    """
    store = await get_profile_store()
    user_id = "323"
    await store.set_summary(user_id, "initial md", "t0")

    entered = asyncio.Event()
    proceed = asyncio.Event()
    original_aget = type(store._store).aget
    call_count = 0

    async def _blocking_aget(self, namespace, key, **kwargs):
        nonlocal call_count
        if self is store._store and namespace == ("profile", user_id) and key == "summary":
            call_count += 1
            if call_count == 1:  # 배치(A)의 carryover read만 멈춘다
                entered.set()
                await proceed.wait()
        return await original_aget(self, namespace, key, **kwargs)

    monkeypatch.setattr(type(store._store), "aget", _blocking_aget)

    task_a = asyncio.create_task(store.set_summary(user_id, "batch md", "t1"))
    await entered.wait()  # A가 carryover read에서 멈춤(락 있으면 락을 쥔 채)
    task_b = asyncio.create_task(store.set_summary(user_id, "user md", "t2"))
    try:
        # 락이 없으면 B는 곧바로 완료된다 — 완료되면 A가 재개되기 전에 B의 쓰기 결과를 관찰할 수
        # 있게 한다. 락이 있으면 B는 락 획득 대기로 여기서 타임아웃되고(정상), 아래에서 A를
        # 재개시킨 뒤 정상적으로 완료된다.
        await asyncio.wait_for(asyncio.shield(task_b), timeout=1.0)
    except TimeoutError:
        pass
    proceed.set()
    await asyncio.gather(task_a, task_b)

    final = await store.get_summary(user_id)
    assert final is not None
    assert final.markdown == "user md"  # 락이 있으면 B(사용자 편집)가 마지막에 쓴다


async def test_append_session_ctx_caps_count() -> None:
    """세션 버퍼 개수 상한 — 최신 cap 개만 유지."""
    store = await get_profile_store()
    for i in range(10):
        await store.append_session_ctx("k", f"turn-{i}", cap=3)
    assert await store.get_session_ctx("k") == ["turn-7", "turn-8", "turn-9"]


async def test_session_end_unmarks_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """처리 실패(LLM 오류) 시 멱등키 마킹 해제 + 버퍼 보존 — 재전송이 재처리 가능(멱등은 성공에만)."""
    from app.core.llm import LLMError

    class _Raise:
        async def complete(self, **k):
            raise LLMError("transient")

        async def stream(self, **k):
            yield "x"

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _Raise())
    key = conversation_key("44", "s")
    store = await get_profile_store()
    await store.append_session_ctx(key, "취향 신호")
    r = client.post("/events/session-end", json={"userId": 44, "sessionId": "s"})
    assert r.status_code == 202
    # 언마크 → 재전송 시 재처리 (고정 멱등키)
    assert not await processed_events.seen_event("session-end:44:s")
    assert await store.get_session_ctx(key) != []  # 버퍼 보존


async def test_session_end_preserves_buffer_when_consolidation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """델타 성공 뒤 consolidation 실패도 미처리로 남겨 재시도할 수 있어야 한다."""

    class _ConsolidationFails(_ProfileLLM):
        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
            if "델타 추출기" in system:
                return await super().complete(
                    system=system,
                    user=user,
                    tier=tier,
                    max_tokens=max_tokens,
                    json_output=json_output,
                )
            raise LLMError("consolidation unavailable")

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ConsolidationFails())
    key = conversation_key("45", "consolidation-failure")
    store = await get_profile_store()
    await store.append_session_ctx(key, "파란색 상품을 선호해")

    response = client.post(
        "/events/session-end",
        json={"userId": 45, "sessionId": "consolidation-failure"},
    )

    assert response.status_code == 202 and response.json()["status"] == "accepted"
    assert await store.get_session_ctx(key) == ["파란색 상품을 선호해"]
    assert await store.get_summary("45") is None
    assert not await processed_events.seen_event("session-end:45:consolidation-failure")


async def test_session_end_releases_claim_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """선마킹 뒤 요청이 취소돼도 처리 중 claim이 영구 멱등 마커로 남지 않는다."""
    import app.api.events as ev

    started = asyncio.Event()

    async def _block_until_cancelled():
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(profile_finalizer, "get_profile_store", _block_until_cancelled)
    task = asyncio.create_task(
        ev.session_end(
            SessionEndEvent(userId=46, sessionId="cancelled-session"),
            None,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not await processed_events.seen_event("session-end:46:cancelled-session")


async def test_processed_event_stale_claim_can_be_reclaimed_but_completed_cannot() -> None:
    """crash로 남은 PROCESSING claim만 lease 만료 후 재선점하고 완료 row는 영구 중복 처리한다."""
    event_id = "session-end:47:lease-recovery"
    first = await processed_events.claim_event(event_id, lease_s=0.001)
    assert first is not None

    await asyncio.sleep(0.01)
    second = await processed_events.claim_event(event_id, lease_s=0.001)
    assert second is not None and second != first
    assert not await processed_events.release_claim(event_id, first)
    assert await processed_events.complete_claim(event_id, second)

    await asyncio.sleep(0.01)
    assert await processed_events.claim_event(event_id, lease_s=0.001) is None
    assert await processed_events.get_status(event_id) == "completed"


async def test_session_end_release_failure_falls_back_to_lease_recovery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """claim 해제 DB까지 실패해도 202를 유지하고 lease 만료 뒤 영구 poison 없이 재선점한다."""

    async def _store_failure():
        raise RuntimeError("profile store unavailable")

    async def _release_failure(*args, **kwargs):
        raise RuntimeError("processed_events unavailable")

    original_release = processed_events.release_claim
    monkeypatch.setattr(get_settings(), "session_end_claim_ttl_s", 0.001)
    monkeypatch.setattr(profile_finalizer, "get_profile_store", _store_failure)
    monkeypatch.setattr(processed_events, "release_claim", _release_failure)

    with caplog.at_level(logging.WARNING, logger="app.agents.profile.finalizer"):
        response = client.post(
            "/events/session-end",
            json={"userId": 48, "sessionId": "release-failure"},
        )
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    from app.core import session_context

    context = await session_context._default_repository.get_context("release-failure")
    assert context is not None
    event_id = f"chat-profile:{context.context_id}:{context.generation}:terminal"
    assert await processed_events.seen_event(event_id)
    assert "session-end claim 해제 실패" in caplog.text

    monkeypatch.setattr(processed_events, "release_claim", original_release)
    await asyncio.sleep(0.01)
    reclaimed = await processed_events.claim_event(
        event_id,
        lease_s=1,
    )
    assert reclaimed is not None
    assert await processed_events.release_claim(event_id, reclaimed)


async def test_session_end_records_retryable_profile_on_claim_ownership_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile claim ownership race는 terminal 완료를 되돌리지 않고 재시도로 남긴다."""

    async def _lose_claim(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(processed_events, "complete_claim", _lose_claim)
    response = client.post(
        "/events/session-end",
        json={"userId": 49, "sessionId": "ownership-lost"},
    )

    assert response.status_code == 202 and response.json()["status"] == "accepted"
    from app.core import session_context

    context = await session_context._default_repository.get_context("ownership-lost")
    assert context is not None and context.state == "terminal"
    [candidate] = await session_context._default_repository.list_recoverable_profile_phases(10)
    journal = await session_context._default_repository.get_finalization(candidate.finalization_id)
    assert journal.transient_status == "completed"
    assert journal.profile_status == "retryable"


async def test_release_claim_best_effort_retrieves_internal_task_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """outer 요청과 무관하게 release task가 취소돼도 task를 재-await해 결과를 회수한다."""

    class _CountingCancelledFuture(asyncio.Future):
        await_count = 0

        def __await__(self):
            self.await_count += 1
            return super().__await__()

    cancelled = _CountingCancelledFuture()
    cancelled.cancel()

    def _create_cancelled_task(coro):
        coro.close()
        return cancelled

    monkeypatch.setattr(profile_finalizer.asyncio, "create_task", _create_cancelled_task)
    with caplog.at_level(logging.WARNING, logger="app.agents.profile.finalizer"):
        await profile_finalizer.release_processed_claim_best_effort(
            "chat-profile:ctx:1:terminal",
            "token",
        )

    assert cancelled.await_count == 2
    assert "session-end claim 해제 task 취소" in caplog.text


async def test_session_end_returns_202_when_profile_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_profile_store() 자체가 실패해도(pg-profile 일시 장애) 500 이 아니라 202(PR #47 후속 리뷰).

    이관 전엔 인메모리 싱글턴이라 이 호출이 실패할 수 없었지만, 운영(jwks)은 pg-profile 연결
    실패 시 폴백 없이 raise 한다 — try 밖에 있으면 일시적 DB 장애만으로 §3.5(항상 202) 위반.
    """

    async def _raise() -> None:
        raise RuntimeError("pg-profile 일시 장애")

    monkeypatch.setattr(profile_finalizer, "get_profile_store", _raise)
    r = client.post("/events/session-end", json={"userId": 44, "sessionId": "s"})
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    context = await session_context._default_repository.get_context("s")
    assert context is not None and context.state == "terminal"


async def test_session_end_returns_202_and_preserves_buffer_on_store_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pg-profile query deadline도 §3.5 best-effort 경계에서 202 + 버퍼 보존으로 강등한다."""
    key = conversation_key("44", "timeout-session")
    store = await get_profile_store()
    await store.append_session_ctx(key, "재시도할 취향 신호")

    async def _timeout(*args, **kwargs):
        raise TimeoutError("pg query deadline")

    monkeypatch.setattr(processed_events, "claim_event", _timeout)
    response = client.post(
        "/events/session-end",
        json={"userId": 44, "sessionId": "timeout-session"},
    )

    assert response.status_code == 202
    assert await store.get_session_ctx(key) == ["재시도할 취향 신호"]


def test_internal_token_required_in_jwks_mode() -> None:
    """운영(jwks)에서 internal_api_token 미주입이면 Settings 기동 실패(inbound fail-open 방지)."""
    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(auth_mode="jwks", jwks_url="http://x", pii_hash_pepper="p", internal_api_token="")
    Settings(
        auth_mode="jwks",
        jwks_url="http://x",
        pii_hash_pepper="p",
        internal_api_token="tok",
        google_api_key="k",
    )  # ok


async def test_profile_store_all_operations_have_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ProfileStore 공개 I/O가 모두 동일 deadline을 사용한다(이슈 #50)."""

    class _HangStore:
        async def aget(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def aput(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def asearch(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def adelete(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.01)
    store = ProfileStore(_HangStore())
    operations = [
        lambda: store.get_summary("u"),
        lambda: store.set_summary("u", "m", "t"),
        lambda: store.get_facts("u"),
        lambda: store.add_fact("u", "f"),
        lambda: store.append_session_ctx("k", "x"),
        lambda: store.get_session_ctx("k"),
        lambda: store.get_session_ctx_snapshot("k"),
        lambda: store.clear_session_ctx_upto("k", 1),
    ]
    for operation in operations:
        with pytest.raises(TimeoutError):
            await operation()


def test_profile_lock_registries_release_idle_keys() -> None:
    """유휴 lock key는 GC로 회수되고, 사용 중 lock은 호출자가 참조하는 동안 유지된다."""
    from app.agents.profile import store as profile_store_module

    session_lock = profile_store_module._session_lock("session")
    fact_lock = profile_store_module._fact_lock("user")
    assert len(profile_store_module._session_locks) == 1
    assert len(profile_store_module._fact_locks) == 1

    del session_lock, fact_lock
    gc.collect()

    assert len(profile_store_module._session_locks) == 0
    assert len(profile_store_module._fact_locks) == 0


async def test_processed_events_operations_have_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session-end 멱등 테이블의 모든 쿼리가 멈춘 DB에서 유한 시간 내 종료한다."""

    class _HangConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args, **kwargs):
            await asyncio.sleep(10)

    class _HangPool:
        closed = False

        def connection(self):
            return _HangConn()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.01)
    processed_events.set_pool(_HangPool())
    try:
        operations = [
            lambda: processed_events.mark_if_new("e"),
            lambda: processed_events.seen_event("e"),
            lambda: processed_events.get_status("e"),
            lambda: processed_events.mark_event("e"),
            lambda: processed_events.unmark_event("e"),
            lambda: processed_events.claim_event("e", lease_s=1),
            lambda: processed_events.complete_claim("e", "token"),
            lambda: processed_events.claim_is_current("e", "token"),
            lambda: processed_events.release_claim("e", "token"),
        ]
        for operation in operations:
            with pytest.raises(TimeoutError):
                await operation()
    finally:
        processed_events.set_pool(None)


# ─────────── #119 세션 버퍼 반복 발화 상한 ───────────


async def test_session_buffer_caps_repeated_utterances() -> None:
    """[#119] 같은 발화를 4번 해도 버퍼에는 repeat_cap 개까지만 남는다.

    버퍼는 generate_session_delta 에 "\n".join 으로 통째로 실리고 LLM 이 그 중복을 보고
    repetitionEma 를 산출하므로, 반복 **횟수**가 그대로 취향 강도가 된다 — 증폭만 자른다.
    """
    store = await get_profile_store()
    for _ in range(4):
        await store.append_session_ctx("k", "무선 이어폰 추천해줘", cap=100, repeat_cap=2)

    assert await store.get_session_ctx("k") == ["무선 이어폰 추천해줘"] * 2


async def test_session_buffer_keeps_repetition_signal_observable() -> None:
    """상한은 2 이상이라 "반복했다"는 신호 자체는 델타 추출 LLM 에 남는다.

    게이트는 `salience AND (explicit OR repeated)` 라 반복은 명시 표명 없이 승격시키는
    **독립 경로**다. 1 건만 남기면 그 경로가 죽고, 세션 간 누적(GateState)은 미구현이라
    (OPEN-P12) 다음 세션이 대신 살려주지도 않는다.
    """
    store = await get_profile_store()
    for _ in range(3):
        await store.append_session_ctx("k", "소니 이어폰", cap=100, repeat_cap=2)

    assert (await store.get_session_ctx("k")).count("소니 이어폰") >= 2


async def test_session_buffer_repeat_cap_normalizes_whitespace_and_case() -> None:
    """정규화 규약 고정 — 앞뒤 공백·연속 공백·대소문자 차이는 같은 발화로 본다."""
    store = await get_profile_store()
    for text in ("Sony 이어폰", " sony 이어폰 ", "sony  이어폰", "SONY 이어폰"):
        await store.append_session_ctx("k", text, cap=100, repeat_cap=2)

    assert await store.get_session_ctx("k") == ["Sony 이어폰", " sony 이어폰 "]


async def test_session_buffer_repeat_cap_keeps_distinct_utterances() -> None:
    """서로 다른 발화는 그대로 쌓인다 — 과한 정규화로 정당한 취향 신호를 잃지 않는다."""
    store = await get_profile_store()
    await store.append_session_ctx("k", "무선 이어폰 추천해줘", cap=100, repeat_cap=2)
    await store.append_session_ctx("k", "노이즈캔슬링 되는 걸로", cap=100, repeat_cap=2)

    assert await store.get_session_ctx("k") == ["무선 이어폰 추천해줘", "노이즈캔슬링 되는 걸로"]


async def test_session_buffer_repeat_cap_off_preserves_legacy() -> None:
    """롤백 경로 — repeat_cap 미지정이면 종전대로 전부 적재한다(하위 호환)."""
    store = await get_profile_store()
    for _ in range(3):
        await store.append_session_ctx("k", "같은 말", cap=100)

    assert await store.get_session_ctx("k") == ["같은 말"] * 3


async def test_session_buffer_watermark_survives_repeat_cap() -> None:
    """상한 스킵은 aput 자체를 하지 않으므로 seq 워터마크 불변식이 깨지지 않는다.

    finalizer 는 snapshot 워터마크로 소비 범위를 고정하고 그 지점까지 clear 한다 —
    스킵된 발화가 seq 를 앞당기면 미소비 발화가 조용히 삭제될 수 있다.
    """
    store = await get_profile_store()
    for _ in range(3):
        await store.append_session_ctx("k", "첫 발화", cap=100, repeat_cap=2)  # 3번째는 스킵
    await store.append_session_ctx("k", "둘째 발화", cap=100, repeat_cap=2)

    items, watermark = await store.get_session_ctx_snapshot("k")
    assert items == ["첫 발화", "첫 발화", "둘째 발화"]
    assert await store.get_session_ctx_upto("k", watermark) == items

    await store.clear_session_ctx_upto("k", watermark)
    assert await store.get_session_ctx("k") == []
