"""pg-profile 공통 리질리언스 유틸 회귀 테스트 (이슈 #50)."""

from __future__ import annotations

import asyncio

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.core.config import get_settings
from app.core.pg_resilience import BoundedLRUCache, hardened_pg_conninfo, run_with_query_timeout


def test_hardened_conninfo_applies_socket_and_statement_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "state_store_connect_timeout_s", 4.2)
    monkeypatch.setattr(settings, "state_store_query_timeout_s", 1.25)
    monkeypatch.setattr(settings, "state_store_keepalives_idle_s", 7)
    monkeypatch.setattr(settings, "state_store_keepalives_interval_s", 2)
    monkeypatch.setattr(settings, "state_store_keepalives_count", 4)
    monkeypatch.setattr(settings, "state_store_tcp_user_timeout_ms", 1700)

    params = conninfo_to_dict(hardened_pg_conninfo(settings.profile_db_url))

    assert params["connect_timeout"] == "5"  # libpq 정수 초 단위라 올림
    assert params["keepalives"] == "1"
    assert params["keepalives_idle"] == "7"
    assert params["keepalives_interval"] == "2"
    assert params["keepalives_count"] == "4"
    assert params["tcp_user_timeout"] == "1700"
    assert "statement_timeout=1250" in params["options"]


async def test_run_with_query_timeout_stops_hung_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.01)

    async def hangs() -> None:
        await asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        await run_with_query_timeout(hangs())


def test_bounded_lru_cache_evicts_oldest_and_refreshes_reads() -> None:
    cache: BoundedLRUCache[str, int] = BoundedLRUCache(max_entries=2)
    cache["a"] = 1
    cache["b"] = 2
    assert cache.get("a") == 1  # a를 MRU로 승격

    cache["c"] = 3

    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_bounded_lru_cache_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError):
        BoundedLRUCache(max_entries=0)
