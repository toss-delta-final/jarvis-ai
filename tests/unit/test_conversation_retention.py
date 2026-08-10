"""대화 전사록 보존 스윕 (이슈 #321, SPEC-PROFILE-001 OPEN-P5 해소).

인메모리 `ConversationStore` 는 주입 clock 으로 시간을 전진시켜 검증한다(`time.sleep` 금지).
pg-profile 실측은 `tests/integration/test_pg_conversation_store.py` 참고.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.core.conversation import ConversationStore, TurnStatus
from app.pipelines import scheduler as scheduler_module


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


async def test_purge_expired_turns_deletes_only_before_cutoff() -> None:
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = ConversationStore(clock=clock)
    old = await store.save_user_message("conv-1", "u1", "user", "old")
    clock.advance(days=100)
    new = await store.save_user_message("conv-1", "u1", "user", "new")

    deleted = await store.purge_expired_turns(retention_days=90)

    assert deleted == 1
    assert await store.get_turn(old.turn_id) is None
    assert await store.get_turn(new.turn_id) is not None


async def test_purge_expired_turns_deletes_pending_turns_too() -> None:
    """`_evict_if_needed` 는 PENDING 을 건너뛰지만, 시간 만료는 그 예외를 따르지 않는다 —
    90일 된 PENDING 은 죽은 스트림이라 예외를 두면 TTL 이 지우려던 것이 정확히 그만큼 남는다."""
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = ConversationStore(clock=clock)
    pending = await store.save_user_message("conv-2", "u2", "user", "질문")
    saved_turn = await store.get_turn(pending.turn_id)
    assert saved_turn is not None
    assert saved_turn.status is TurnStatus.PENDING
    clock.advance(days=100)

    deleted = await store.purge_expired_turns(retention_days=90)

    assert deleted == 1
    assert await store.get_turn(pending.turn_id) is None


async def test_purge_expired_turns_respects_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "conversation_retention_batch_size", 2)
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = ConversationStore(clock=clock)
    for i in range(5):
        await store.save_user_message("conv-3", "u3", "user", f"turn-{i}")
    clock.advance(days=100)

    first_batch = await store.purge_expired_turns(retention_days=90)
    assert first_batch == 2
    second_batch = await store.purge_expired_turns(retention_days=90)
    assert second_batch == 2
    third_batch = await store.purge_expired_turns(retention_days=90)
    assert third_batch == 1


async def test_purge_expired_turns_removes_from_conversation_index() -> None:
    """지운 턴이 `turns_for()` 조회에서도 사라진다 — 인덱스 정리가 안 되면 유령 turn_id 가 남는다."""
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = ConversationStore(clock=clock)
    await store.save_user_message("conv-4", "u4", "user", "old")
    clock.advance(days=100)

    await store.purge_expired_turns(retention_days=90)

    assert await store.turns_for("conv-4") == []


# ─────────── 스케줄러 job — 배치 반복·cap_reached 로그 ───────────


class _CountingStore:
    """`purge_expired_turns` 호출마다 미리 정해진 배치 크기 시퀀스를 반환하는 fake."""

    def __init__(self, batches: list[int]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def purge_expired_turns(self, retention_days: float) -> int:
        del retention_days
        self.calls += 1
        return self._batches.pop(0) if self._batches else 0


async def test_sweep_stops_when_a_batch_is_smaller_than_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_retention_batch_size", 5)
    monkeypatch.setattr(settings, "conversation_retention_max_batches", 20)
    monkeypatch.setattr(settings, "conversation_retention_sweep_enabled", True)
    store = _CountingStore([5, 5, 3])
    monkeypatch.setattr(scheduler_module, "get_conversation_store", _fake_getter(store))

    await scheduler_module.run_conversation_retention_sweep()

    assert store.calls == 3  # 세 번째 배치(3 < 5)에서 멈춘다


async def test_sweep_logs_error_when_batch_cap_is_reached(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#325] apscheduler 는 예외를 삼킨다 — cap 도달은 백로그 신호라 error 로 드러나야 한다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_retention_batch_size", 2)
    monkeypatch.setattr(settings, "conversation_retention_max_batches", 3)
    monkeypatch.setattr(settings, "conversation_retention_sweep_enabled", True)
    store = _CountingStore([2, 2, 2])  # 매 배치가 꽉 참 — 더 남았을 수 있다
    monkeypatch.setattr(scheduler_module, "get_conversation_store", _fake_getter(store))

    with caplog.at_level("ERROR"):
        await scheduler_module.run_conversation_retention_sweep()

    assert store.calls == 3
    assert any("cap_reached=True" in record.message for record in caplog.records)


async def test_sweep_is_no_op_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_retention_sweep_enabled", False)
    store = _CountingStore([5])
    monkeypatch.setattr(scheduler_module, "get_conversation_store", _fake_getter(store))

    await scheduler_module.run_conversation_retention_sweep()

    assert store.calls == 0


def _fake_getter(store):
    async def _get():
        return store

    return _get
