"""app.main lifespan 배선 테스트 (이슈 #31/#221).

TestClient(app)를 `with`로 감싸야 lifespan 이 실제로 발동한다(경험적으로 확인 —
`with` 없이 쓰는 이 저장소의 기존 TestClient 테스트들은 lifespan 영향을 받지 않는다).
스케줄러와 종료 API는 fake 로 대체해 호출 순서와 실패 격리를 검증한다.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient
import pytest

import app.main as main_mod
from app.core.config import Settings
from app.pipelines.category_seed import CategoryDictionaryError


def _patch_lifespan_dependencies(
    monkeypatch,
    calls,
    *,
    failing_resource=None,
    cancelling_resource=None,
    hanging_resource=None,
    record_category_check=False,
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

    def category_check(dsn, *, mode):
        # 실 DB I/O 없는 기본 no-op — `_lifespan` 에 진입하는 유닛 테스트가 진짜 psycopg.connect
        # 를 하지 않게 한다(#401 라운드 2 리뷰 F2). `record_category_check=True` 면 호출 여부를
        # `calls` 에 남겨 배선 검증에 쓴다.
        if record_category_check:
            calls.append("category_dictionary_check")

    monkeypatch.setattr(main_mod, "check_category_dictionary", category_check)
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
    # 실 DB I/O 없이(#401 라운드 2 리뷰 F2) — 이 테스트는 `_patch_lifespan_dependencies` 를 쓰지
    # 않아 그 헬퍼의 기본 no-op 패치를 못 받으므로 직접 패치한다.
    monkeypatch.setattr(main_mod, "check_category_dictionary", lambda dsn, *, mode: None)

    with TestClient(main_mod.app) as client:
        assert calls == ["start"]
        resp = client.get("/health")
        assert resp.status_code == 200

    assert calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_lifespan_entry_calls_category_dictionary_startup_check(monkeypatch):
    """`_lifespan` 진입이 실제로 카테고리 사전 가드를 부르는지 배선을 검증한다(#401 라운드 2 F2).

    이전 라운드의 3건은 `_check_category_dictionary_startup()` 을 직접 호출해 가드
    함수 자체의 계약만 검증했다 — `_lifespan` 안의 `await _check_category_dictionary_startup()`
    한 줄이 지워져도 그 3건은 그대로 통과한다. 이 테스트는 `_lifespan` 을 통해서만 통과할 수
    있게 `check_category_dictionary` 호출 여부를 `calls` 에 기록해 배선 자체를 검증한다.
    """
    calls = []
    _patch_lifespan_dependencies(monkeypatch, calls, record_category_check=True)

    async with main_mod._lifespan(main_mod.app):
        pass

    assert "category_dictionary_check" in calls
    # initialize_session_lifecycle 보다 먼저 불려야 한다(§요구 동작: 기동 초반 가드).
    assert calls.index("category_dictionary_check") < calls.index("initialize")


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
                "lifespan_resource_close_floor_s": 0.001,
                "lifespan_cleanup_budget_s": 0.2,
                # `_lifespan` 이 `_check_category_dictionary_startup` 도 거치므로(이슈 #401)
                # 이 fake 객체도 그 경로가 읽는 속성을 갖춰야 한다 — 없으면 이 테스트가 검증하려는
                # 자원 정리 타이밍이 아니라 엉뚱한 AttributeError 로 실패한다.
                "catalog_db_url": "postgresql://x",
                "category_dictionary_startup_check": "log",
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
                "lifespan_resource_close_floor_s": 0.001,
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
        "lifespan resource cleanup timed out resource=session_lifecycle timeout_s=5.0"
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
                "lifespan_resource_close_timeout_s": 0.05,
                "lifespan_resource_close_floor_s": 0.002,
                "lifespan_cleanup_budget_s": 0.08,
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
    assert "lifespan resource cleanup timed out resource=session_lifecycle" in caplog.text
    assert (
        "lifespan resource cleanup budget exhausted resource=session_lifecycle" not in caplog.text
    )
    assert "lifespan resource cleanup budget exhausted resource=advisory_pool" not in caplog.text
    assert "lifespan resource cleanup complete succeeded=8 failed=1" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_preserves_floor_after_slow_resources_nearly_consume_budget(
    monkeypatch, caplog
):
    calls = []
    completed = []

    async def hang(name):
        calls.append(name)
        await asyncio.Event().wait()

    async def complete_after_suspension(name):
        calls.append(name)
        await asyncio.sleep(0.005)
        completed.append(name)

    monkeypatch.setattr(main_mod, "close_session_lifecycle", lambda: hang("session_lifecycle"))
    monkeypatch.setattr(
        main_mod,
        "close_seller_history_store",
        lambda: hang("seller_history_store"),
    )
    for attribute, resource_name in (
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
                "lifespan_resource_close_timeout_s": 0.5,
                "lifespan_resource_close_floor_s": 0.02,
                "lifespan_cleanup_budget_s": 0.8,
            },
        )(),
    )

    with caplog.at_level("INFO"):
        await main_mod._close_owned_resources()

    assert completed == calls[2:]
    assert "lifespan resource cleanup timed out resource=session_lifecycle" in caplog.text
    assert "lifespan resource cleanup budget exhausted resource=seller_history_store" in caplog.text
    assert "lifespan resource cleanup complete succeeded=7 failed=2" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_clamps_negative_allowance_and_warns_without_startup_failure(
    monkeypatch, caplog
):
    calls = []
    _patch_lifespan_dependencies(monkeypatch, calls, hanging_resource="*")
    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "lifespan_resource_close_timeout_s": 0.1,
                "lifespan_resource_close_floor_s": 0.01,
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
    assert "lifespan cleanup budget cannot reserve resource floor" in caplog.text
    assert (
        "lifespan resource cleanup budget exhausted resource=session_lifecycle timeout_s=0.0"
        in caplog.text
    )
    assert "lifespan resource cleanup complete succeeded=0 failed=9" in caplog.text


# --- 카테고리 사전 0행/0임베딩 기동 가드 (이슈 #401) ------------------------------------------


@pytest.mark.asyncio
async def test_category_dictionary_startup_check_survives_db_connection_failure(
    monkeypatch, caplog
) -> None:
    """DB 연결 실패(예: pg-catalog 미기동)는 기동을 죽이지 않는다 — WARNING 로그 후 계속."""

    def raise_connection_error(dsn: str, *, mode: str) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_connection_error)

    with caplog.at_level("WARNING"):
        await main_mod._check_category_dictionary_startup()  # 예외를 던지면 안 됨

    assert "category dictionary startup check could not reach the database" in caplog.text


@pytest.mark.asyncio
async def test_category_dictionary_startup_check_propagates_config_error_in_fail_mode(
    monkeypatch,
) -> None:
    """구성 오류(0행/0임베딩) + `fail` 모드는 `CategoryDictionaryError` 를 그대로 올린다.

    "연결 실패"와 "구성 오류"는 다른 사실이다 — 앞의 테스트가 전자를 삼키는 걸 확인했으니
    후자는 삼키지 않는지도 확인해야 회귀를 잡는다(둘 다 Exception 을 뭉뚱그려 잡으면 이 테스트가
    실패한다).
    """

    def raise_dictionary_error(dsn: str, *, mode: str) -> None:
        raise CategoryDictionaryError("카테고리 사전 0행")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_dictionary_error)

    with pytest.raises(CategoryDictionaryError):
        await main_mod._check_category_dictionary_startup()


@pytest.mark.asyncio
async def test_category_dictionary_startup_check_runs_in_thread_with_settings_dsn_and_mode(
    monkeypatch,
) -> None:
    """설정의 dsn·모드를 그대로 전달하는지 — 블로킹 psycopg 호출이므로 스레드 오프로딩도 확인."""
    captured: dict = {}

    def record(dsn: str, *, mode: str) -> None:
        captured["dsn"] = dsn
        captured["mode"] = mode
        captured["thread"] = threading.current_thread().name

    monkeypatch.setattr(main_mod, "check_category_dictionary", record)
    settings = Settings(_env_file=None, category_dictionary_startup_check="fail")
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    await main_mod._check_category_dictionary_startup()

    assert captured["dsn"] == settings.catalog_db_url
    assert captured["mode"] == "fail"
    assert captured["thread"] != "MainThread"


# --- 도달 불가 vs 예상 못 한 가드 버그 (이슈 #401 라운드 5 리뷰 F7, 라운드 7 리뷰 F8) --------
#
# 라운드 4 까지는 CategoryDictionaryError 가 아니면 전부 "연결 실패"로 뭉뚱그려, 가드 코드
# 자체의 버그(TypeError 등)도 조용히 삼켜졌다. F7 이 세 갈래(구성 오류·도달 불가·예상 못 한
# 버그)로 나눴는데, F7 은 "도달 불가는 모든 모드에서 WARNING"으로 뒀었다. 리뷰가 실측으로
# 반박했다 — `psycopg.OperationalError` 는 일시적 도달 불가(연결 거부·타임아웃)와 영구적
# 구성 오류(비밀번호·dbname 오타)를 구조화된 판별자 없이 같은 타입으로 낸다. 그래서 F8 은
# 예외 타입 분류를 더 정교하게 하는 대신 `fail` 의 의미를 바꿨다 — "확인하지 못하면 거부".
# `log`/`off` 의 동작(도달 불가는 WARNING 후 계속)은 F7 그대로 변경 없음.


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "log"])
async def test_category_dictionary_startup_check_survives_psycopg_operational_error_outside_fail_mode(
    monkeypatch, caplog, mode
) -> None:
    """`psycopg.OperationalError` 는 `OSError` 를 상속하지 않는다 — `OSError` 만 잡으면 이
    타입을 놓친다. `off`/`log`(F7 이 정한 기존 동작, F8 에서도 변경 없음)에서는 WARNING 후
    계속돼야 한다.
    """
    import psycopg

    def raise_operational_error(dsn: str, *, mode: str) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_operational_error)
    settings = Settings(_env_file=None, category_dictionary_startup_check=mode)
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    with caplog.at_level("WARNING"):
        await main_mod._check_category_dictionary_startup()  # 예외를 던지면 안 됨

    assert "category dictionary startup check could not reach the database" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [OSError, lambda: __import__("psycopg").OperationalError("connection refused")],
    ids=["OSError", "psycopg.OperationalError"],
)
async def test_category_dictionary_startup_check_unreachable_rejects_in_fail_mode(
    monkeypatch, caplog, exc_factory
) -> None:
    """[F8] `fail` 은 도달 불가도 "확인 실패"로 다뤄 기동을 거부해야 한다 — 잘못된 DSN(비밀번호·
    dbname 오타)이 `psycopg.OperationalError` 로만 나오고 `fail` 을 건 운영자가 가장 잡고 싶은
    실수이기 때문이다. 로그는 ERROR 이고 "도달 불가" 원인을 정직하게 남긴다(뭉개지 않는다).
    """

    def raise_unreachable(dsn: str, *, mode: str) -> None:
        raise exc_factory()

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_unreachable)
    settings = Settings(_env_file=None, category_dictionary_startup_check="fail")
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    with caplog.at_level("ERROR"):
        with pytest.raises(Exception) as exc_info:  # OSError·psycopg.OperationalError 둘 다 받는다
            await main_mod._check_category_dictionary_startup()

    # CategoryDictionaryError 로 둔갑시키지 않고 원인 예외 그대로 전파돼야 진단이 보존된다.
    assert not isinstance(exc_info.value, CategoryDictionaryError)
    assert "ERROR" in caplog.text
    assert "could not reach the database" in caplog.text
    assert "failing startup" in caplog.text


@pytest.mark.asyncio
async def test_category_dictionary_startup_check_unexpected_bug_propagates_in_fail_mode(
    monkeypatch, caplog
) -> None:
    """가드 코드 자체의 버그(예: `TypeError`)는 `fail` 모드에서 전파돼야 `fail` 계약("확인하지
    못하면 반드시 기동을 거부한다")이 그 버그 앞에서 무력화되지 않는다. 로그 문구는 "도달 불가"라고
    오진단하면 안 된다.
    """

    def raise_type_error(dsn: str, *, mode: str) -> None:
        raise TypeError("unexpected bug in guard")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_type_error)
    settings = Settings(_env_file=None, category_dictionary_startup_check="fail")
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    with caplog.at_level("ERROR"):
        with pytest.raises(TypeError):
            await main_mod._check_category_dictionary_startup()

    assert "could not reach the database" not in caplog.text
    assert "failed unexpectedly" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "log"])
async def test_category_dictionary_startup_check_unexpected_bug_only_logs_outside_fail_mode(
    monkeypatch, caplog, mode
) -> None:
    """같은 버그가 `off`/`log` 에서는 서버를 죽이면 안 된다 — 리뷰어의 "무조건 좁혀 잡기"
    제안을 그대로 받으면 기본값(`log`)의 회복력이 깨진다(사소한 가드 버그가 전면 중단이 됨).
    """

    def raise_type_error(dsn: str, *, mode: str) -> None:
        raise TypeError("unexpected bug in guard")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_type_error)
    settings = Settings(_env_file=None, category_dictionary_startup_check=mode)
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    with caplog.at_level("ERROR"):
        await main_mod._check_category_dictionary_startup()  # 예외를 던지면 안 됨

    assert "failed unexpectedly" in caplog.text


@pytest.mark.asyncio
async def test_category_dictionary_startup_check_all_causes_reject_in_fail_mode(
    monkeypatch,
) -> None:
    """[F8] `fail` 에서는 세 갈래(구성 오류·도달 불가·예상 못 한 버그) 모두 기동을 거부해야
    한다 — 원인이 뭐든 "확인하지 못하면 거부"가 `fail` 의 계약이다. 하나라도 삼켜지면(예:
    도달 불가만 예외적으로 통과) 이 테스트가 실패해야 한다.
    """
    settings = Settings(_env_file=None, category_dictionary_startup_check="fail")
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    def raise_category_dictionary_error(dsn: str, *, mode: str) -> None:
        raise CategoryDictionaryError("카테고리 사전 0행")

    def raise_operational_error(dsn: str, *, mode: str) -> None:
        raise OSError("connection refused")

    def raise_type_error(dsn: str, *, mode: str) -> None:
        raise TypeError("unexpected bug in guard")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_category_dictionary_error)
    with pytest.raises(CategoryDictionaryError):
        await main_mod._check_category_dictionary_startup()

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_operational_error)
    with pytest.raises(OSError):  # [F8] 이제 fail 에서는 삼켜지지 않는다
        await main_mod._check_category_dictionary_startup()

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_type_error)
    with pytest.raises(TypeError):  # fail 모드라 전파돼야 함(CategoryDictionaryError 로 둔갑 금지)
        await main_mod._check_category_dictionary_startup()


@pytest.mark.asyncio
async def test_category_dictionary_startup_check_log_messages_stay_distinct_in_fail_mode(
    monkeypatch, caplog
) -> None:
    """[F8] `fail` 에서 거부 여부는 세 갈래 모두 같아졌지만(전부 거부), **로그 문구는 원인별로
    계속 구분**돼야 한다 — 진단 정보까지 뭉개면 안 된다(이 수정의 회귀 위험).
    """
    settings = Settings(_env_file=None, category_dictionary_startup_check="fail")
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    def raise_operational_error(dsn: str, *, mode: str) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_operational_error)
    with caplog.at_level("ERROR"):
        with pytest.raises(OSError):
            await main_mod._check_category_dictionary_startup()
    unreachable_log = caplog.text
    assert "could not reach the database" in unreachable_log
    caplog.clear()

    def raise_type_error(dsn: str, *, mode: str) -> None:
        raise TypeError("unexpected bug in guard")

    monkeypatch.setattr(main_mod, "check_category_dictionary", raise_type_error)
    with caplog.at_level("ERROR"):
        with pytest.raises(TypeError):
            await main_mod._check_category_dictionary_startup()
    unexpected_log = caplog.text
    assert "failed unexpectedly" in unexpected_log

    # 두 원인이 서로의 문구를 침범하면 안 된다 — 진단이 뭉개졌다는 신호다.
    assert "failed unexpectedly" not in unreachable_log
    assert "could not reach the database" not in unexpected_log
