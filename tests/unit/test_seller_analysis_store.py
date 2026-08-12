"""판매자 분석 저장소 pool 수명주기 회귀 테스트."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.agents.seller import analysis_store
from app.agents.seller.context import SellerContext


async def test_get_pool_closes_partially_opened_pool_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fire-and-forget 등록 취소가 psycopg worker를 이벤트 루프에 남기지 않는다."""
    closed = False

    class CancellingPool:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def open(self, *, wait: bool) -> None:
            del wait
            raise asyncio.CancelledError

        async def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(analysis_store, "AsyncConnectionPool", CancellingPool)
    analysis_store.reset_pool()

    with pytest.raises(asyncio.CancelledError):
        await analysis_store._get_pool()

    assert closed is True


async def test_write_uses_parameterizable_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL이 `$1`을 거부하는 SET 문 대신 set_config 함수로 로컬 timeout을 설정한다."""
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args
            return False

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args
            return False

        def transaction(self):
            return Transaction()

        async def execute(self, statement: str, params: tuple[object, ...]) -> None:
            statements.append((statement, params))

    class Pool:
        def connection(self, *, timeout: float):
            assert timeout == 3.0
            return Connection()

    async def fake_get_pool():
        return Pool()

    monkeypatch.setattr(analysis_store, "_get_pool", fake_get_pool)
    monkeypatch.setattr(
        analysis_store,
        "get_settings",
        lambda: SimpleNamespace(
            seller_analysis_write_timeout_s=15.0,
            seller_db_write_retries=0,
            state_store_query_timeout_s=3.0,
        ),
    )

    result = await analysis_store._write(lambda _conn: asyncio.sleep(0, result="written"))

    assert result == "written"
    assert statements == [
        ("SELECT set_config('statement_timeout', %s, true)", ("15000",)),
    ]


async def test_target_registration_failure_log_omits_raw_seller_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """자동 등록 실패 로그는 raw brandId/sellerId를 노출하지 않는다."""
    raw_seller = 700000000007
    raw_brand = 300000000003

    async def fail_registration(*args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("registration failed")

    monkeypatch.setattr(analysis_store, "register_target", fail_registration)

    with caplog.at_level(logging.WARNING, logger="app.agents.seller.analysis_store"):
        await analysis_store._register_quietly(SellerContext(raw_seller, raw_brand))

    assert str(raw_seller) not in caplog.text
    assert str(raw_brand) not in caplog.text
    assert "SELLER_ANALYSIS_TARGET_REGISTER_FAILED" in caplog.text
