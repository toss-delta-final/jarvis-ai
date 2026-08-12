"""프로필 흐름 E2E 스모크 (이슈 #35) — 발화 누적 → session-end → 델타·consolidation.

api-spec §3.4(프로필 조회)·§3.5(I-20 세션 종료 통지, 멱등) + SPEC-PROFILE-001 의 2단 비동기 쓰기가
실제로 맞물리는지 확인한다. 턴 중에는 write 하지 않고(transient 격리) 세션 종료에 승격된다.
"""

from __future__ import annotations

import jwt

from tests.integration.conftest import auth_header, parse_sse

USER_ID = "42"


def _chat(
    client,
    message: str,
    *,
    session: str = "sess-prof",
    thread: str = "th-prof",
    headers=None,
):
    return client.post(
        "/chat",
        json={"sessionId": session, "threadId": thread, "message": message},
        headers=auth_header(USER_ID) if headers is None else headers,
    )


def _session_end(client, *, session: str = "sess-prof", user_id: int = int(USER_ID)):
    return client.post(
        "/events/session-end",
        json={"userId": user_id, "sessionId": session},
    )


def _buyer_session_header(subject: str, sub_type: str, session_id: str) -> dict[str, str]:
    token = jwt.encode(
        {"sub": subject, "sub_type": sub_type, "sessionId": session_id},
        "dev-only-not-a-secret-0123456789",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def test_profile_empty_before_any_session(client, spring, llm) -> None:
    """세션 종료 전에는 프로필이 없다 — 턴 중 write 금지(transient 격리)."""
    _chat(client, "3만원 이하 여행용 파우치 추천해줘")

    from app.agents.profile.reader import read_profile_summary

    assert await read_profile_summary(USER_ID) is None


async def test_session_end_builds_profile_after_session_end(
    client, spring, llm, monkeypatch
) -> None:
    """세션 종료 → 델타 추출·게이트 승격 → consolidation까지 완료한다."""
    import app.agents.profile.finalizer as profile_finalizer

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: llm)
    _chat(client, "3만원 이하 여행용 파우치 추천해줘")

    resp = _session_end(client)
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"

    from app.agents.profile.reader import read_profile_summary

    summary = await read_profile_summary(USER_ID)
    assert summary is not None and "여행용품" in summary["markdown"]
    # 델타(Sonnet) + consolidation(Sonnet) 각 1회 — 세션 종료에서만 LLM 을 쓴다
    assert llm.calls_of("delta") == 1
    assert llm.calls_of("consolidate") == 1


def test_session_end_is_idempotent(client, spring, llm, monkeypatch) -> None:
    """같은 세션 종료 재통지는 202 duplicate 이며 프로필을 중복 처리하지 않는다."""
    import app.agents.profile.finalizer as profile_finalizer

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: llm)
    _chat(client, "3만원 이하 여행용 파우치 추천해줘")

    first = _session_end(client)
    second = _session_end(client)

    assert first.json()["status"] == "accepted"
    assert second.status_code == 202 and second.json()["status"] == "duplicate"
    assert llm.calls_of("delta") == 1, "중복 통지가 LLM 을 재호출하면 안 된다"


async def test_profile_is_injected_into_next_session(client, spring, llm, monkeypatch) -> None:
    """승격된 프로필이 다음 턴 rerank 컨텍스트로 주입된다 (개인화 루프 종단)."""
    import app.agents.profile.finalizer as profile_finalizer

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: llm)
    _chat(client, "3만원 이하 여행용 파우치 추천해줘")
    _session_end(client)
    llm.calls.clear()

    resp = _chat(client, "이번엔 캐리어 추천해줘", session="sess-prof-2", thread="th-prof-2")
    assert resp.status_code == 200
    assert parse_sse(resp.text)[-1]["type"] == "done"
    # 프로필 마크다운이 rerank 프롬프트에 실렸는지는 store 조회로 확인(프롬프트 원문 비의존)
    from app.agents.profile.reader import read_profile_summary

    assert await read_profile_summary(USER_ID) is not None


async def test_remember_command_promotes_immediately(client, spring, llm) -> None:
    """ "기억해" 명시 명령은 게이트 없이 즉시 승격된다 (hot-path, 세션 종료 대기 없음)."""
    _chat(client, "나 브랜드 트래블러 좋아하니까 기억해줘")

    from app.agents.profile.store import get_profile_store

    store = await get_profile_store()
    facts = await store.get_facts(USER_ID)
    assert any("트래블러" in fact for fact in facts)


def test_session_end_degrades_without_llm(client, spring, monkeypatch) -> None:
    """LLM 미구성이어도 세션 종료는 202 로 받는다 — best-effort degrade(§3.5, 500 금지)."""
    import app.agents.profile.finalizer as profile_finalizer

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: None)
    resp = _session_end(client)
    assert resp.status_code == 202


async def test_guest_claim_preserves_transcripts_but_promotes_only_member_facts(
    client, spring, llm, monkeypatch
) -> None:
    """세 탭 claim 뒤 기록은 보존하되 guest 발화는 회원 장기 fact 입력에서 격리한다."""
    import app.agents.profile.finalizer as profile_finalizer
    from app.agents.profile.store import get_profile_store
    from app.core import session_context
    from app.core.conversation import conversation_key, get_conversation_store
    from app.core.session_context import SessionContextRepository

    repo = SessionContextRepository()
    monkeypatch.setattr(session_context, "_default_repository", repo)
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: llm)
    original_complete = llm.complete
    delta_inputs: list[str] = []

    async def capture_delta_input(**kwargs):
        if llm._classify(kwargs["system"], kwargs["tier"]) == "delta":
            delta_inputs.append(kwargs["user"])
        return await original_complete(**kwargs)

    monkeypatch.setattr(llm, "complete", capture_delta_input)
    guest_headers = _buyer_session_header("G1", "guest", "S1")
    member_headers = _buyer_session_header("1", "member", "S1")
    guest_messages = {
        "T1": "GUEST_ONLY_SECRET_1 파우치 추천",
        "T2": "GUEST_ONLY_SECRET_2 파우치 추천",
        "T3": "GUEST_ONLY_SECRET_3 파우치 추천",
    }

    for thread_id, message in guest_messages.items():
        response = _chat(client, message, session="S1", thread=thread_id, headers=guest_headers)
        assert response.status_code == 200
    before_claim = await repo.get_context("S1")
    assert before_claim is not None

    claim = client.post(
        "/events/session-claim",
        json={"sessionId": "S1", "guestId": "G1", "userId": 1},
        headers={"X-Internal-Token": "e2e-internal-token"},
    )
    assert claim.status_code == 202
    assert claim.json() == {"status": "accepted"}

    member_messages = {
        "T1": "회원 전환 후 첫 번째 탭 계속",
        "T2": "회원 전환 후 두 번째 탭 계속",
        "T3": "회원 전환 후 세 번째 탭 계속",
    }
    for thread_id, message in member_messages.items():
        response = _chat(client, message, session="S1", thread=thread_id, headers=member_headers)
        assert response.status_code == 200
        current = await repo.get_context("S1")
        assert current is not None and current.context_id == before_claim.context_id

    conversation_store = await get_conversation_store()
    guest_key = conversation_key("G1", "S1")
    member_key = conversation_key("1", "S1")
    guest_turns = await conversation_store.turns_for(guest_key)
    member_turns = await conversation_store.turns_for(member_key)
    assert [turn.user_text for turn in guest_turns] == list(guest_messages.values())
    assert [turn.user_text for turn in member_turns] == list(member_messages.values())

    old_guest = _chat(
        client,
        "GUEST_ONLY_SECRET_BRANCH",
        session="S1",
        thread="T4",
        headers=guest_headers,
    )
    assert old_guest.status_code == 403
    assert old_guest.json()["error"]["code"] == "SESSION_FORBIDDEN"
    assert await repo.get_threads(before_claim.context_id) == ["T1", "T2", "T3"]
    assert len(await conversation_store.turns_for(guest_key)) == len(guest_turns)

    profile_store = await get_profile_store()
    member_buffer = await profile_store.get_session_ctx(member_key)
    guest_buffer = await profile_store.get_session_ctx(guest_key)
    assert member_buffer == list(member_messages.values())
    assert guest_buffer == []
    assert not any("GUEST_ONLY_SECRET" in item for item in member_buffer)

    llm._delta = {
        "deltas": [
            {
                "fact": "회원 전환 후 세 탭에서 여행용품을 탐색한다",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.8,
            }
        ]
    }
    ended = _session_end(client, session="S1", user_id=1)
    assert ended.status_code == 202
    assert ended.json() == {"status": "accepted"}

    assert len(delta_inputs) == 1
    assert delta_inputs[0].splitlines() == list(member_messages.values())
    assert "GUEST_ONLY_SECRET" not in delta_inputs[0]

    facts = await profile_store.get_facts("1")
    assert facts == ["회원 전환 후 세 탭에서 여행용품을 탐색한다"]
    assert not any("GUEST_ONLY_SECRET" in fact for fact in facts)
    assert [turn.user_text for turn in await conversation_store.turns_for(guest_key)] == list(
        guest_messages.values()
    )
    assert [turn.user_text for turn in await conversation_store.turns_for(member_key)] == list(
        member_messages.values()
    )
