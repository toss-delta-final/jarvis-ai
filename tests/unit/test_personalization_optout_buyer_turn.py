"""개인화 중지 — 구매자 턴 배선 (REQ-PGRAPH-051/052, 이슈 #359).

**`buyer/graph.py` 를 실제로 통과시키는 CI 게이트다.** 리더·게이트 단위 테스트는 각 부품이
옳은지만 재고, 그 부품이 hot-path 에 실제로 꽂혔는지는 재지 못한다 — 주입 지점 한 줄이 지워져도
전부 초록불이다.

`evals/personalization` 의 `member_no_profile` arm 은 이 검증에 **쓸 수 없다**:
`live_adapter` 가 `decompose`/`stream_recommendation` 의 kwargs 를 직접 patch 해 arm 을 만들어
플래그·리더·주입 지점을 하나도 타지 않는다. 그 arm 은 baseline 무회귀 확인용이다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.buyer import graph as buyer_graph
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.profile import graph_journal
from app.agents.profile.store import get_profile_store, reset_profile_store
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from tests._fakes import FakeLLM

MEMBER_ID = "123"


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


def _member() -> Identity:
    return Identity(user_id=MEMBER_ID, is_guest=False, seller_id=None, subject=MEMBER_ID)


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject="guest-uuid")


def _req(message: str, thread_id: str) -> SimpleNamespace:
    # 세션 id 를 스레드마다 가른다 — 세션 소유권은 owner(회원/게스트)에 묶여 있어
    # 한 session_id 를 회원과 게스트가 나눠 쓰면 `SessionForbidden` 이 난다.
    return SimpleNamespace(session_id=f"s-{thread_id}", thread_id=thread_id, message=message)


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        context_id=context.context_id,
        request_id="optout-buyer-test",
        record_model_call=lambda *_: None,
    )


async def _collect(request, identity, **kwargs) -> list[dict]:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    events: list[dict] = []
    async for frame in _production_run_buyer_turn(request, identity, observer=observer, **kwargs):
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


# 임포트 시점의 **생산 함수**를 붙잡아 둔다. 스파이를 두 번 설치할 때
# `buyer_graph.stream_recommendation` 을 그때그때 읽으면 두 번째 스파이가 첫 번째를 감싸게 되고,
# 두 번째 실행이 첫 스파이의 기록을 **덮어써** 테스트가 조용히 공허해진다(실측: 소비 게이트를
# 통째로 지워도 4건 전부 통과했다).
_PRODUCTION_STREAM = buyer_graph.stream_recommendation


class _Spy:
    """`stream_recommendation` 에 넘어간 프로필 인자를 붙잡는다 — 주입 지점의 실제 산출물."""

    def __init__(self) -> None:
        self.profile: object = "<not called>"
        self.profile_vec: object = "<not called>"

    def install(self, monkeypatch) -> None:  # noqa: ANN001
        async def spy(*args, **kwargs):  # noqa: ANN001
            self.profile = kwargs.get("profile")
            self.profile_vec = kwargs.get("profile_vec")
            async for frame in _PRODUCTION_STREAM(*args, **kwargs):
                yield frame

        monkeypatch.setattr(buyer_graph, "stream_recommendation", spy)


async def _seed_profile() -> None:
    store = await get_profile_store()
    await store.set_summary(MEMBER_ID, "# 취향\n- 소니 선호", "2026-08-01T00:00:00+00:00")


async def _run_recommend(identity, monkeypatch, thread_id: str) -> _Spy:  # noqa: ANN001
    spy = _Spy()
    spy.install(monkeypatch)
    llm = FakeLLM(decompose={"intent": "recommend", "semanticQuery": "이어폰"})
    await _collect(_req("이어폰 추천해줘", thread_id), identity, llm=llm)
    return spy


async def test_disabled_member_turn_passes_the_same_profile_args_as_a_guest(
    monkeypatch,
) -> None:
    """**중지 회원의 rerank 인자가 게스트와 동일하다** (REQ-PGRAPH-051, #119 안전 속성).

    "회원 프롬프트가 게스트와 바이트 동일해진다" 를 프롬프트 문자열이 아니라 **주입 인자**로
    잰다 — 프롬프트 빌더는 `profile_summary or "(없음)"` 이라 인자가 같으면 문자열도 같고,
    인자 쪽이 회귀 지점에 더 가깝다.
    """
    await _seed_profile()
    await graph_journal.set_personalization_flag(
        user_id=int(MEMBER_ID), enabled=False, now="2026-08-10T00:00:00+00:00"
    )

    disabled = await _run_recommend(_member(), monkeypatch, "t-disabled")
    guest = await _run_recommend(_guest(), monkeypatch, "t-guest")

    assert (disabled.profile, disabled.profile_vec) == (guest.profile, guest.profile_vec)
    assert disabled.profile is None


async def test_enabled_member_turn_still_injects_the_profile(monkeypatch) -> None:
    """게이트가 켜져 있으면 종전대로 주입된다 — 차단이 상시 발동하면 개인화가 죽는다."""
    await _seed_profile()

    spy = await _run_recommend(_member(), monkeypatch, "t-enabled")

    assert isinstance(spy.profile, str) and "소니" in spy.profile


async def test_remember_command_is_not_stored_while_disabled(monkeypatch) -> None:
    """**전역 중지가 개별 "기억해" 요청보다 우선한다** (REQ-PGRAPH-052, api-spec §3.9.5)."""
    await graph_journal.set_personalization_flag(
        user_id=int(MEMBER_ID), enabled=False, now="2026-08-10T00:00:00+00:00"
    )
    calls: list[str] = []

    async def fake_record_remember(user_id, fact):  # noqa: ANN001
        calls.append(fact)

    monkeypatch.setattr(buyer_graph, "record_remember", fake_record_remember)

    llm = FakeLLM(decompose={"intent": "recommend", "semanticQuery": "이어폰"})
    await _collect(_req("소니 좋아하는 거 기억해줘", "t-remember-off"), _member(), llm=llm)

    assert calls == []


async def test_remember_command_is_stored_while_enabled(monkeypatch) -> None:
    """반대 방향 — 켜져 있으면 종전대로 즉시 승격된다(hot-path 가 죽지 않았다)."""
    calls: list[str] = []

    async def fake_record_remember(user_id, fact):  # noqa: ANN001
        calls.append(fact)

    monkeypatch.setattr(buyer_graph, "record_remember", fake_record_remember)

    llm = FakeLLM(decompose={"intent": "recommend", "semanticQuery": "이어폰"})
    await _collect(_req("소니 좋아하는 거 기억해줘", "t-remember-on"), _member(), llm=llm)

    assert calls == ["소니 좋아하는 거 기억해줘"]


class _FakeProfileStore:
    """`get_profile_store()` 대체 — `append_session_ctx` 호출 여부만 기록한다(실 pg-profile 무관).

    `test_profile_buffer_intent_exclusion.py` 의 템플릿과 같다. `buyer/graph.py` 가
    `from ... import get_profile_store` 로 이름을 들여왔으므로 **소비 모듈의 바인딩**을 패치해야
    실제로 가로채진다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def append_session_ctx(self, key, message, *, cap, repeat_cap):  # noqa: ANN001
        self.calls.append((key, message))


def _patch_profile_store(monkeypatch) -> _FakeProfileStore:  # noqa: ANN001
    fake = _FakeProfileStore()

    async def fake_get_profile_store():
        return fake

    monkeypatch.setattr(buyer_graph, "get_profile_store", fake_get_profile_store)
    return fake


async def test_session_buffer_does_not_grow_while_disabled(monkeypatch) -> None:
    """**중지 중에는 세션 버퍼가 늘지 않는다** (REQ-PGRAPH-052).

    수집 차단의 나머지 절반이다 — "기억해" 는 명시 명령이고 이쪽은 모든 발화가 지나가는
    상시 경로라, 막지 않으면 중지해도 취향 원문이 계속 쌓인다. 다음 배치가 그것을 델타로
    뽑으면 "수집 중지" 가 사후에 무너진다.
    """
    await graph_journal.set_personalization_flag(
        user_id=int(MEMBER_ID), enabled=False, now="2026-08-10T00:00:00+00:00"
    )
    fake_store = _patch_profile_store(monkeypatch)

    llm = FakeLLM(decompose={"intent": "recommend", "semanticQuery": "이어폰"})
    await _collect(_req("소니 이어폰 좋아해", "t-buffer-off"), _member(), llm=llm)

    assert fake_store.calls == []


async def test_session_buffer_still_grows_while_enabled(monkeypatch) -> None:
    """반대 방향 — 켜져 있으면 종전대로 쌓인다. 차단이 상시 발동하면 수집이 통째로 죽는다."""
    fake_store = _patch_profile_store(monkeypatch)

    llm = FakeLLM(decompose={"intent": "recommend", "semanticQuery": "이어폰"})
    await _collect(_req("소니 이어폰 좋아해", "t-buffer-on"), _member(), llm=llm)

    assert [message for _, message in fake_store.calls] == ["소니 이어폰 좋아해"]
