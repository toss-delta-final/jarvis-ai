"""승인된 색상 동의어 런타임 사전·캐시 테스트 (이슈 #258)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from app.core.config import Settings
from app.pipelines import color_synonym_seed
from app.pipelines import color_synonyms


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Result(self.rows)

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Pool:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def connection(self):
        return _Conn(self.rows, self.calls)


def test_runtime_and_seed_share_one_vector_configured_pool_per_dsn(monkeypatch) -> None:
    from app.core import config
    import psycopg_pool

    created: list[tuple[str, dict]] = []

    class Pool:
        def __init__(self, dsn, **kwargs):
            created.append((dsn, kwargs))

    settings = Settings(_env_file=None, color_synonym_pool_max_size=7)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    monkeypatch.setattr(color_synonyms, "_pools", {})

    runtime_pool = color_synonyms._get_pool("postgresql://shared")
    seed_pool = color_synonym_seed._get_pool("postgresql://shared")

    assert runtime_pool is seed_pool
    assert len(created) == 1
    assert created[0][1]["max_size"] == 7
    assert created[0][1]["configure"].__name__ == "register_vector"


def test_runtime_connection_pool_uses_configured_max_size(monkeypatch) -> None:
    from app.core import config
    import psycopg_pool

    captured = {}

    class Pool:
        def __init__(self, dsn, **kwargs):
            captured.update(kwargs)

    settings = Settings(_env_file=None, color_synonym_pool_max_size=7)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    monkeypatch.setattr(color_synonyms, "_pools", {})
    color_synonyms._get_pool("postgresql://size-check")
    assert captured["max_size"] == 7


def test_runtime_connection_pool_accepts_configured_boundary_size_two(monkeypatch) -> None:
    from app.core import config
    import psycopg_pool

    real_pool = psycopg_pool.ConnectionPool

    def closed_pool(dsn, **kwargs):
        return real_pool(dsn, **{**kwargs, "open": False})

    settings = Settings(
        _env_file=None,
        color_synonym_pool_max_size=2,
        color_synonym_harvest_max_concurrency=1,
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", closed_pool)
    monkeypatch.setattr(color_synonyms, "_pools", {})

    pool = color_synonyms._get_pool("postgresql://example.invalid/catalog")
    try:
        assert pool.min_size <= pool.max_size == 2
    finally:
        pool.close()


def test_load_synonym_map_uses_approved_non_null_rows_and_deterministic_order(monkeypatch) -> None:
    calls = []
    # 쿼리 자체가 pending/null 을 제외하고, 반환 묶음은 doc_count desc → term asc.
    rows = [("남색", "네이비", 8), ("네이비", "네이비", 631), ("NAVY", "네이비", 8)]
    monkeypatch.setattr(color_synonyms, "_get_pool", lambda dsn: _Pool(rows, calls))
    mapping = color_synonyms.load_synonym_map("postgresql://x")
    assert mapping["남색"] == ["네이비", "NAVY", "남색"]
    assert mapping["navy"] == ["네이비", "NAVY", "남색"]
    assert calls[0] == ("SET LOCAL statement_timeout = 2500", None)
    sql = calls[1][0]
    assert "status = 'approved'" in sql
    assert "canonical IS NOT NULL" in sql


def test_expand_color_normalizes_lookup_but_preserves_catalog_spelling() -> None:
    mapping = {
        "남색": ["네이비", "남색"],
        "navy": ["네이비", "남색"],
        "화이트+네이비": ["화이트+네이비", "화이트네이비"],
    }
    assert color_synonyms.expand_color("  남색 ", mapping) == ["네이비", "남색"]
    assert color_synonyms.expand_color("NAVY", mapping) == ["네이비", "남색"]
    assert color_synonyms.expand_color("청록", mapping) == ["청록"]
    assert color_synonyms.expand_color("실버+블랙", mapping) == ["실버+블랙"]
    assert color_synonyms.expand_color("화이트+네이비", mapping) == [
        "화이트+네이비",
        "화이트네이비",
    ]
    assert color_synonyms.expand_color("  ", mapping) == ["  "]


def test_cache_uses_monotonic_ttl_and_reset(monkeypatch) -> None:
    color_synonyms.reset_cache()
    now = [100.0]
    loads: list[str] = []
    monkeypatch.setattr(color_synonyms.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        color_synonyms,
        "load_synonym_map",
        lambda dsn: loads.append(dsn) or {"남색": ["네이비", "남색"]},
    )

    assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0)["남색"]
    now[0] = 109.9
    color_synonyms.get_synonym_map("dsn", ttl_s=10.0)
    assert loads == ["dsn"]
    now[0] = 110.0
    color_synonyms.get_synonym_map("dsn", ttl_s=10.0)
    assert loads == ["dsn", "dsn"]
    color_synonyms.reset_cache()
    color_synonyms.get_synonym_map("dsn", ttl_s=10.0)
    assert loads == ["dsn", "dsn", "dsn"]


def test_empty_approved_map_warns_once_per_ttl_when_expansion_enabled(monkeypatch, caplog) -> None:
    """확장이 켜진 채 approved 0건이면 조용한 무동작을 경고한다(#461)."""
    import logging

    color_synonyms.reset_cache()
    now = [100.0]
    monkeypatch.setattr(color_synonyms.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(color_synonyms, "load_synonym_map", lambda dsn: {})

    with caplog.at_level(logging.WARNING, logger="app.pipelines.color_synonyms"):
        assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0, warn_if_empty=True) == {}
        assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0, warn_if_empty=True) == {}

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert warnings == [
        "색상 동의어 사전 비어 있음 — status='approved' AND canonical IS NOT NULL 0행, 확장 무동작"
    ]


def test_empty_approved_map_is_quiet_when_expansion_disabled(monkeypatch, caplog) -> None:
    """확장이 꺼진 환경은 빈 사전이어도 경고하지 않는다(#461)."""
    import logging

    color_synonyms.reset_cache()
    monkeypatch.setattr(color_synonyms, "load_synonym_map", lambda dsn: {})

    with caplog.at_level(logging.WARNING, logger="app.pipelines.color_synonyms"):
        assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0, warn_if_empty=False) == {}

    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_nonempty_approved_map_does_not_warn_when_expansion_enabled(monkeypatch, caplog) -> None:
    """승인 사전이 있으면 확장 활성화 경고를 내지 않는다(#461)."""
    import logging

    color_synonyms.reset_cache()
    monkeypatch.setattr(
        color_synonyms,
        "load_synonym_map",
        lambda dsn: {"그레이": ["그레이", "회색"]},
    )

    with caplog.at_level(logging.WARNING, logger="app.pipelines.color_synonyms"):
        assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0, warn_if_empty=True) == {
            "그레이": ["그레이", "회색"]
        }

    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_concurrent_cache_misses_do_not_hold_cache_lock_during_db_load(monkeypatch) -> None:
    color_synonyms.reset_cache()
    both_loaders_entered = threading.Barrier(2)

    def load(dsn: str) -> dict[str, list[str]]:
        both_loaders_entered.wait(timeout=1.0)
        return {"남색": ["네이비", "남색"]}

    monkeypatch.setattr(color_synonyms, "load_synonym_map", load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(color_synonyms.get_synonym_map, "dsn", ttl_s=10.0)
            for _ in range(2)
        ]
        assert [future.result(timeout=2.0) for future in futures] == [
            {"남색": ["네이비", "남색"]},
            {"남색": ["네이비", "남색"]},
        ]


def test_failed_load_is_negative_cached_within_ttl_window(monkeypatch) -> None:
    """DB 가 죽어 있을 때(#258 CI hang 회귀) TTL 창당 실제 연결 시도는 1회여야 한다.

    성공 캐시(`test_cache_uses_monotonic_ttl_and_reset`)와 짝을 이루는 실패 경로 버전 — 실패는
    지금까지 전혀 캐시되지 않아 매 호출이 재시도였다. 여기서는 `load_synonym_map`(DB I/O 경계)
    자체를 실패시켜 `get_synonym_map` 이 그 호출 횟수를 TTL 창당 1회로 유계화하는지 잰다.
    """
    import pytest

    color_synonyms.reset_cache()
    now = [100.0]
    load_calls: list[str] = []
    monkeypatch.setattr(color_synonyms.time, "monotonic", lambda: now[0])

    def failing_load(dsn: str) -> dict[str, list[str]]:
        load_calls.append(dsn)
        raise TimeoutError("dead dsn")

    monkeypatch.setattr(color_synonyms, "load_synonym_map", failing_load)

    for _ in range(5):
        with pytest.raises(Exception):  # noqa: B017,PT011 - 실패 전파 자체가 계약, 타입은 자유
            color_synonyms.get_synonym_map("dead-dsn", ttl_s=10.0)
    assert load_calls == ["dead-dsn"]  # TTL 창 안에서는 실제 DB 호출이 1회뿐이어야 한다

    now[0] = 110.0  # TTL 만료 — 다시 시도해야 한다(영구 차단 금지)
    with pytest.raises(Exception):  # noqa: B017,PT011
        color_synonyms.get_synonym_map("dead-dsn", ttl_s=10.0)
    assert load_calls == ["dead-dsn", "dead-dsn"]


def test_failed_load_logs_warning_not_silently_swallowed(monkeypatch, caplog) -> None:
    """실패가 조용히 삼켜지면 안 된다 — 최소 1회 warning 로그를 남긴다."""
    import logging

    import pytest

    color_synonyms.reset_cache()

    def failing_load(dsn: str) -> dict[str, list[str]]:
        raise TimeoutError("dead dsn")

    monkeypatch.setattr(color_synonyms, "load_synonym_map", failing_load)

    with caplog.at_level(logging.WARNING, logger="app.pipelines.color_synonyms"):
        with pytest.raises(Exception):  # noqa: B017,PT011
            color_synonyms.get_synonym_map("dead-dsn", ttl_s=10.0)

    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_success_after_failure_clears_negative_cache(monkeypatch) -> None:
    """실패 캐시가 성공 결과를 영원히 막지 않는다 — TTL 만료 뒤 성공하면 정상 반환된다."""
    color_synonyms.reset_cache()
    now = [100.0]
    monkeypatch.setattr(color_synonyms.time, "monotonic", lambda: now[0])

    attempts = {"n": 0}

    def flaky_load(dsn: str) -> dict[str, list[str]]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("dead dsn")
        return {"남색": ["네이비", "남색"]}

    monkeypatch.setattr(color_synonyms, "load_synonym_map", flaky_load)

    import pytest

    with pytest.raises(Exception):  # noqa: B017,PT011
        color_synonyms.get_synonym_map("dsn", ttl_s=10.0)

    now[0] = 110.0  # TTL 만료 후 재시도
    assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0) == {"남색": ["네이비", "남색"]}
