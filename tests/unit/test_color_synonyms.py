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
