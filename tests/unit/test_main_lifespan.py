"""app.main lifespan 배선 테스트 (이슈 #31/#221).

TestClient(app)를 `with`로 감싸야 lifespan 이 실제로 발동한다(경험적으로 확인 —
`with` 없이 쓰는 이 저장소의 기존 TestClient 테스트들은 lifespan 영향을 받지 않는다).
스케줄러와 종료 API는 fake 로 대체해 호출 순서와 실패 격리를 검증한다.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
import pytest

import app.main as main_mod
from app.core.config import Settings


def _patch_lifespan_dependencies(
    monkeypatch,
    calls,
    *,
    failing_resource=None,
    cancelling_resource=None,
    hanging_resource=None,
):
    async def record(name):
        calls.append(name)
        if name == failing_resource:
            raise RuntimeError(f"{name} close failed")
        if name == cancelling_resource:
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        if name == hanging_resource or (hanging_resource == "*" and name != "initialize"):
            await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "initialize_session_lifecycle", lambda: record("initialize"))
    monkeypatch.setattr(main_mod, "start_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr(main_mod, "stop_scheduler", lambda: calls.append("stop"))
    monkeypatch.setattr(main_mod, "close_session_lifecycle", lambda: record("session_lifecycle"))
    monkeypatch.setattr(
        main_mod,
        "close_profile_store",
        lambda: record("profile_store"),
        raising=False,
    )
    monkeypatch.setattr(
        main_mod,
        "close_seller_history_store",
        lambda: record("seller_history_store"),
        raising=False,
    )
    monkeypatch.setattr(
        main_mod,
        "close_seller_checkpointer",
        lambda: record("seller_checkpointer"),
        raising=False,
    )
    monkeypatch.setattr(
        main_mod,
        "close_session_activity_pool",
        lambda: record("session_activity_pool"),
        raising=False,
    )
    monkeypatch.setattr(
        main_mod,
        "close_processed_events_pool",
        lambda: record("processed_events_pool"),
        raising=False,
    )
    monkeypatch.setattr(
        main_mod,
        "close_conversation_store",
        lambda: record("conversation_store"),
        raising=False,
    )
    monkeypatch.setattr(main_mod, "close_pg_store", lambda: record("pg_store"), raising=False)
    monkeypatch.setattr(main_mod, "close_advisory_pool", lambda: record("advisory_pool"))


def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod, "start_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr(main_mod, "stop_scheduler", lambda: calls.append("stop"))

    with TestClient(main_mod.app) as client:
        assert calls == ["start"]
        resp = client.get("/health")
        assert resp.status_code == 200

    assert calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_lifespan_closes_all_owned_resources_in_reverse_order(monkeypatch):
    calls = []
    _patch_lifespan_dependencies(monkeypatch, calls)

    async with main_mod._lifespan(main_mod.app):
        assert calls == ["initialize", "start"]

    assert calls == [
        "initialize",
        "start",
        "stop",
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]


@pytest.mark.asyncio
async def test_lifespan_continues_cleanup_after_resource_failure(monkeypatch, caplog):
    calls = []
    _patch_lifespan_dependencies(monkeypatch, calls, failing_resource="session_activity_pool")

    with caplog.at_level("INFO"):
        async with main_mod._lifespan(main_mod.app):
            pass

    assert calls == [
        "initialize",
        "start",
        "stop",
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]
    assert "lifespan resource cleanup failed resource=session_activity_pool" in caplog.text
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_finishes_cleanup_before_propagating_task_cancellation(monkeypatch, caplog):
    calls = []
    _patch_lifespan_dependencies(monkeypatch, calls, cancelling_resource="seller_history_store")

    async def run_lifespan() -> None:
        async with main_mod._lifespan(main_mod.app):
            pass

    with caplog.at_level("INFO"):
        task = asyncio.create_task(run_lifespan())
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls == [
        "initialize",
        "start",
        "stop",
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]
    assert task.cancelled()
    assert "lifespan resource cleanup cancelled resource=seller_history_store" in caplog.text
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_consumes_external_cancellation_until_cleanup_finishes(monkeypatch):
    started = asyncio.Event()
    observed_cancellation_counts = []

    async def block_until_cancelled():
        started.set()
        await asyncio.Event().wait()

    task = None

    async def observe_parent_cancellation():
        assert task is not None
        observed_cancellation_counts.append(task.cancelling())

    monkeypatch.setattr(main_mod, "close_session_lifecycle", block_until_cancelled)
    for name in (
        "close_seller_history_store",
        "close_seller_checkpointer",
        "close_profile_store",
        "close_session_activity_pool",
        "close_processed_events_pool",
        "close_conversation_store",
        "close_pg_store",
        "close_advisory_pool",
    ):
        monkeypatch.setattr(main_mod, name, observe_parent_cancellation)

    task = asyncio.create_task(main_mod._close_owned_resources())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed_cancellation_counts == [0] * 8


@pytest.mark.asyncio
async def test_lifespan_times_out_hung_resource_and_continues_cleanup(monkeypatch, caplog):
    calls = []
    _patch_lifespan_dependencies(monkeypatch, calls, hanging_resource="seller_history_store")
    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "lifespan_resource_close_timeout_s": 0.01,
                "lifespan_cleanup_budget_s": 0.2,
            },
        )(),
    )

    with caplog.at_level("INFO"):
        async with main_mod._lifespan(main_mod.app):
            pass

    assert calls == [
        "initialize",
        "start",
        "stop",
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]
    assert "lifespan resource cleanup timed out resource=seller_history_store" in caplog.text
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_uses_remaining_budget_before_resource_timeout(monkeypatch, caplog):
    calls = []

    async def slower_than_budget():
        calls.append("session_lifecycle")
        await asyncio.sleep(0.03)

    _patch_lifespan_dependencies(monkeypatch, calls)
    monkeypatch.setattr(main_mod, "close_session_lifecycle", slower_than_budget)
    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "lifespan_resource_close_timeout_s": 0.1,
                "lifespan_cleanup_budget_s": 0.01,
            },
        )(),
    )

    with caplog.at_level("INFO"):
        await main_mod._close_owned_resources()

    assert calls == [
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]
    assert "lifespan resource cleanup budget exhausted resource=session_lifecycle" in caplog.text
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_default_timeout_leaves_time_for_remaining_resources(monkeypatch, caplog):
    calls = []
    completed = []

    async def hang_first():
        calls.append("session_lifecycle")
        await asyncio.Event().wait()

    async def complete_after_suspension(name):
        calls.append(name)
        await asyncio.sleep(0)
        completed.append(name)

    monkeypatch.setattr(main_mod, "close_session_lifecycle", hang_first)
    for attribute, resource_name in (
        ("close_seller_history_store", "seller_history_store"),
        ("close_seller_checkpointer", "seller_checkpointer"),
        ("close_profile_store", "profile_store"),
        ("close_session_activity_pool", "session_activity_pool"),
        ("close_processed_events_pool", "processed_events_pool"),
        ("close_conversation_store", "conversation_store"),
        ("close_pg_store", "pg_store"),
        ("close_advisory_pool", "advisory_pool"),
    ):
        monkeypatch.setattr(
            main_mod,
            attribute,
            lambda resource_name=resource_name: complete_after_suspension(resource_name),
        )
    monkeypatch.setattr(main_mod, "get_settings", lambda: Settings(_env_file=None))

    with caplog.at_level("INFO"):
        await main_mod._close_owned_resources()

    assert calls == [
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]
    assert completed == calls[1:]
    assert (
        "lifespan resource cleanup timed out resource=session_lifecycle timeout_s=0.75"
        in caplog.text
    )
    assert (
        "lifespan resource cleanup budget exhausted resource=session_lifecycle" not in caplog.text
    )
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_reserves_budget_for_every_remaining_close(monkeypatch, caplog):
    calls = []
    completed = []

    async def hang_first():
        calls.append("session_lifecycle")
        await asyncio.Event().wait()

    async def complete_after_suspension(name):
        calls.append(name)
        await asyncio.sleep(0)
        completed.append(name)

    monkeypatch.setattr(main_mod, "close_session_lifecycle", hang_first)
    for attribute, resource_name in (
        ("close_seller_history_store", "seller_history_store"),
        ("close_seller_checkpointer", "seller_checkpointer"),
        ("close_profile_store", "profile_store"),
        ("close_session_activity_pool", "session_activity_pool"),
        ("close_processed_events_pool", "processed_events_pool"),
        ("close_conversation_store", "conversation_store"),
        ("close_pg_store", "pg_store"),
        ("close_advisory_pool", "advisory_pool"),
    ):
        monkeypatch.setattr(
            main_mod,
            attribute,
            lambda resource_name=resource_name: complete_after_suspension(resource_name),
        )
    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "lifespan_resource_close_timeout_s": 0.1,
                "lifespan_cleanup_budget_s": 0.01,
            },
        )(),
    )

    with caplog.at_level("INFO"):
        await main_mod._close_owned_resources()

    assert calls == [
        "session_lifecycle",
        "seller_history_store",
        "seller_checkpointer",
        "profile_store",
        "session_activity_pool",
        "processed_events_pool",
        "conversation_store",
        "pg_store",
        "advisory_pool",
    ]
    assert completed == calls[1:]
    assert "lifespan resource cleanup budget exhausted resource=session_lifecycle" in caplog.text
    assert "lifespan resource cleanup budget exhausted resource=advisory_pool" not in caplog.text
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text
