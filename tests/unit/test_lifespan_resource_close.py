"""기존 pg 종료 API가 실제 close 실패를 lifespan까지 전파하는지 검증한다."""

from __future__ import annotations

import pytest

from app.agents.profile import processed_events, session_activity
from app.agents.profile import store as profile_store
from app.core import conversation, pg_store


class _FailingContext:
    async def __aexit__(self, *_args):
        raise RuntimeError("context close failed")


class _FailingPool:
    async def close(self):
        raise RuntimeError("pool close failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "setter_name", "close_name", "resource"),
    [
        (profile_store, "set_store", "close_store", _FailingContext()),
        (session_activity, "set_pool", "close_pool", _FailingPool()),
        (processed_events, "set_pool", "close_pool", _FailingPool()),
        (conversation, "set_store", "close_store", _FailingPool()),
        (pg_store, "set_store", "close_store", _FailingContext()),
    ],
)
async def test_explicit_close_propagates_pending_cleanup_failure(
    monkeypatch, module, setter_name, close_name, resource
):
    monkeypatch.setattr(module, setter_name, lambda _value: None)
    monkeypatch.setattr(module, "_pending_cleanup", [resource])

    with pytest.raises(RuntimeError, match="close failed"):
        await getattr(module, close_name)()
