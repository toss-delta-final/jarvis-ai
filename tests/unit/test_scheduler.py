"""I-17 + 프로필 idle 스케줄러 테스트 (이슈 #31/#79, 실제 대기 없음).

_run_incremental_batch()는 executor 워커 스레드에서 동기로 호출되는 잡 함수라
(내부에서 asyncio.run 으로 자체 이벤트루프를 새로 연다) 여기 테스트도 동기(def)로 호출한다 —
이미 실행 중인 이벤트루프 안에서 asyncio.run()을 부르면 RuntimeError 가 나기 때문에
async def 테스트로 감싸면 안 된다.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app import main as main_mod
from app.pipelines import scheduler as sched_mod
from app.pipelines.artifacts_batch import BatchResult


@pytest.fixture(autouse=True)
def _reset_scheduler():
    sched_mod.stop_scheduler()
    yield
    sched_mod.stop_scheduler()


async def test_start_scheduler_registers_lifecycle_job_with_configured_interval(monkeypatch):
    settings = Settings(
        _env_file=None,
        catalog_batch_interval_s=123.0,
        profile_idle_sweep_interval_s=17.0,
        google_api_key="test-key",
    )
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    scheduler = sched_mod.start_scheduler()

    i17 = scheduler.get_job(sched_mod._I17_JOB_ID)
    idle = scheduler.get_job(sched_mod._SESSION_CONTEXT_JOB_ID)
    assert i17 is not None and i17.trigger.interval.total_seconds() == 123.0
    assert idle is not None and idle.trigger.interval.total_seconds() == 17.0
    assert scheduler.get_job("profile_idle_timeout") is None


async def test_start_scheduler_is_idempotent(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key")
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    first = sched_mod.start_scheduler()
    second = sched_mod.start_scheduler()

    assert first is second
    # i17 + session_context_sweep + conversation_retention_sweep
    # + [#601] seller_analysis_daily + seller_analysis_weekly
    assert len(first.get_jobs()) == 5


async def test_stop_scheduler_allows_fresh_restart(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key")
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    first = sched_mod.start_scheduler()
    sched_mod.stop_scheduler()
    second = sched_mod.start_scheduler()

    assert first is not second


async def test_missing_google_key_skips_only_i17_but_keeps_idle_job(monkeypatch):
    """이슈 #79 — I-17 provider 조건이 프로필 inactivity 회수까지 끄면 안 된다."""
    settings = Settings(_env_file=None, google_api_key="")
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    scheduler = sched_mod.start_scheduler()

    assert scheduler.get_job(sched_mod._I17_JOB_ID) is None
    assert scheduler.get_job(sched_mod._SESSION_CONTEXT_JOB_ID) is not None


async def test_scripted_provider_skips_i17_but_keeps_lifecycle_jobs(monkeypatch, caplog):
    """scripted 부하 테스트가 실 카탈로그 enrichment를 가짜 값으로 덮어쓰면 안 된다."""
    settings = Settings(
        _env_file=None,
        app_environment="test",
        llm_provider="scripted",
        google_api_key="test-key",
    )
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    scheduler = sched_mod.start_scheduler()

    assert scheduler.get_job(sched_mod._I17_JOB_ID) is None
    assert scheduler.get_job(sched_mod._SESSION_CONTEXT_JOB_ID) is not None
    assert scheduler.get_job(sched_mod._CONVERSATION_RETENTION_JOB_ID) is not None
    assert "scripted LLM" in caplog.text


async def test_start_scheduler_registers_conversation_retention_job_by_default(monkeypatch):
    settings = Settings(
        _env_file=None, google_api_key="", conversation_retention_sweep_interval_s=456.0
    )
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    scheduler = sched_mod.start_scheduler()

    job = scheduler.get_job(sched_mod._CONVERSATION_RETENTION_JOB_ID)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 456.0
    assert job.max_instances == 1
    assert job.coalesce is True


async def test_start_scheduler_skips_conversation_retention_job_when_disabled(monkeypatch):
    settings = Settings(
        _env_file=None, google_api_key="", conversation_retention_sweep_enabled=False
    )
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    scheduler = sched_mod.start_scheduler()

    assert scheduler.get_job(sched_mod._CONVERSATION_RETENTION_JOB_ID) is None


async def test_idle_job_prevents_overlap_and_coalesces_missed_ticks(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="")
    monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

    scheduler = sched_mod.start_scheduler()

    idle = scheduler.get_job(sched_mod._SESSION_CONTEXT_JOB_ID)
    assert idle.max_instances == 1
    assert idle.coalesce is True


def test_run_incremental_batch_calls_run_artifacts_batch_with_full_rebuild_false(monkeypatch):
    calls = []

    async def fake_run_artifacts_batch(*, full_rebuild):
        calls.append(full_rebuild)
        return BatchResult(processed=1, hidden=0, pages=1, cursor="c1")

    monkeypatch.setattr(sched_mod, "run_artifacts_batch", fake_run_artifacts_batch)

    sched_mod._run_incremental_batch()

    assert calls == [False]


def test_run_incremental_batch_logs_error_and_summary_when_failed(monkeypatch, caplog):
    """[#325] 부분 실패는 apscheduler 가 삼키지 않도록 error 로 남기고 요약에 failed 를 포함한다."""

    async def fake_run_artifacts_batch(*, full_rebuild):
        return BatchResult(processed=2, hidden=0, pages=1, cursor="c1", failed=1)

    monkeypatch.setattr(sched_mod, "run_artifacts_batch", fake_run_artifacts_batch)

    with caplog.at_level("INFO"):
        sched_mod._run_incremental_batch()

    assert "failed=1" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_run_incremental_batch_no_error_log_when_no_failures(monkeypatch, caplog):
    async def fake_run_artifacts_batch(*, full_rebuild):
        return BatchResult(processed=2, hidden=0, pages=1, cursor="c1", failed=0)

    monkeypatch.setattr(sched_mod, "run_artifacts_batch", fake_run_artifacts_batch)

    with caplog.at_level("INFO"):
        sched_mod._run_incremental_batch()

    assert "failed=0" in caplog.text
    assert not any(record.levelname == "ERROR" for record in caplog.records)


def test_run_incremental_batch_swallows_exceptions(monkeypatch):
    async def fake_run_artifacts_batch(*, full_rebuild):
        raise RuntimeError("boom")

    monkeypatch.setattr(sched_mod, "run_artifacts_batch", fake_run_artifacts_batch)

    sched_mod._run_incremental_batch()  # 예외가 전파되지 않으면 통과(스케줄러 프로세스 보호)


async def test_run_session_context_sweep_uses_current_event_loop(monkeypatch):
    loops = []
    calls = []

    async def fake_run_session_context_sweep():
        import asyncio

        loops.append(asyncio.get_running_loop())
        calls.append("lifecycle")

    async def gc():
        calls.append("gc")

    monkeypatch.setattr(
        sched_mod, "run_configured_session_context_sweep", fake_run_session_context_sweep
    )
    monkeypatch.setattr(sched_mod, "run_legacy_gc_batch", gc)

    await sched_mod.run_session_context_sweep()

    import asyncio

    assert loops == [asyncio.get_running_loop()]
    assert calls == ["lifecycle", "gc"]


async def test_session_context_sweep_logs_lifecycle_and_gc_progress(monkeypatch, caplog):
    from app.core.session_lifecycle import IdleSweepResult

    async def lifecycle():
        return IdleSweepResult(
            claimed=6,
            examined=7,
            completed=3,
            retryable=1,
            skipped=2,
            superseded_skipped=8,
            invalid_recovery=9,
            recovered=4,
            examined_limit_reached=True,
        )

    async def gc():
        return 5

    async def counters():
        return (11, 12, 13)

    monkeypatch.setattr(sched_mod, "run_configured_session_context_sweep", lifecycle)
    monkeypatch.setattr(sched_mod, "run_legacy_gc_batch", gc)
    monkeypatch.setattr(sched_mod, "get_legacy_gc_counters", counters, raising=False)
    with caplog.at_level("INFO"):
        await sched_mod.run_session_context_sweep()
    assert "examined=7" in caplog.text
    assert "claimed=6" in caplog.text
    assert "completed=3" in caplog.text
    assert "retryable=1" in caplog.text
    assert "skipped=2" in caplog.text
    assert "recovered=4" in caplog.text
    assert "superseded_skipped=8" in caplog.text
    assert "invalid_recovery=9" in caplog.text
    assert "examined_limit_reached=True" in caplog.text
    assert "gc_deleted=5" in caplog.text
    assert "filters_deleted=11" in caplog.text
    assert "cart_deleted=12" in caplog.text
    assert "revert_deleted=13" in caplog.text


async def test_run_session_context_sweep_swallows_exceptions(monkeypatch):
    async def fake_run_session_context_sweep():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        sched_mod, "run_configured_session_context_sweep", fake_run_session_context_sweep
    )

    await sched_mod.run_session_context_sweep()


async def test_lifespan_orders_initialization_scheduler_and_shutdown(monkeypatch):
    calls = []

    async def initialize():
        calls.append("initialize")

    async def close():
        calls.append("close")

    async def close_lifecycle():
        calls.append("close-lifecycle")

    monkeypatch.setattr(main_mod, "initialize_session_lifecycle", initialize)
    monkeypatch.setattr(main_mod, "start_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr(main_mod, "stop_scheduler", lambda: calls.append("stop"))
    monkeypatch.setattr(main_mod, "close_advisory_pool", close)
    monkeypatch.setattr(main_mod, "close_session_lifecycle", close_lifecycle)

    async with main_mod._lifespan(main_mod.app):
        assert calls == ["initialize", "start"]

    assert calls == ["initialize", "start", "stop", "close-lifecycle", "close"]


async def test_lifespan_does_not_start_scheduler_when_initialization_fails(monkeypatch):
    calls = []

    async def initialize():
        calls.append("initialize")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(main_mod, "initialize_session_lifecycle", initialize)

    async def close_lifecycle():
        calls.append("close_lifecycle")

    async def close_advisory():
        calls.append("close_advisory")

    monkeypatch.setattr(main_mod, "start_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr(main_mod, "stop_scheduler", lambda: calls.append("stop"))
    monkeypatch.setattr(main_mod, "close_session_lifecycle", close_lifecycle, raising=False)
    monkeypatch.setattr(main_mod, "close_advisory_pool", close_advisory)

    with pytest.raises(RuntimeError, match="migration failed"):
        async with main_mod._lifespan(main_mod.app):
            pass

    assert calls == ["initialize", "close_lifecycle", "close_advisory"]


def test_legacy_grace_cannot_be_shorter_than_24_hours():
    with pytest.raises(ValueError, match="at least 86400"):
        Settings(_env_file=None, session_lifecycle_legacy_grace_s=86399)


def test_session_backfill_startup_batch_limit_must_be_positive():
    with pytest.raises(ValueError, match="BACKFILL_MAX_BATCHES must be positive"):
        Settings(_env_file=None, session_lifecycle_backfill_max_batches=0)


def test_legacy_writer_quiet_window_covers_stream_lifetime():
    with pytest.raises(ValueError, match="LEGACY_QUIET_S"):
        Settings(
            _env_file=None,
            stream_total_timeout_s=91,
            session_lifecycle_legacy_quiet_s=90,
        )
