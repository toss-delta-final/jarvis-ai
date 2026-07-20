"""get_catalog_store() 프로덕션 전환 테스트 (이슈 #31) — 실 pg-catalog 없이 팩토리 배선만 검증.

PgCatalogArtifactStore 클래스 자체를 fake 로 대체해 라이브 DB 연결 없이 "get_catalog_store()가
싱글턴으로 PgCatalogArtifactStore를 생성·캐시하고, reset_catalog_store()가 close() 후 리셋하는지"만
확인한다. PgCatalogArtifactStore 자체 동작은 tests/integration/test_pg_artifact_store.py 소관.
"""

from __future__ import annotations

import threading
import time

from app.core.config import get_settings
from app.pipelines import artifact_store as store_mod


class _FakePgStore:
    created_dsns: list[str] = []
    closed: list[bool] = []

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        _FakePgStore.created_dsns.append(dsn)

    def close(self) -> None:
        _FakePgStore.closed.append(True)


class _SlowFakePgStore(_FakePgStore):
    """생성자에 인위적 지연을 둬 최초 호출 시 스레드 경합 창을 벌린다."""

    def __init__(self, dsn: str) -> None:
        time.sleep(0.05)
        super().__init__(dsn)


def test_get_catalog_store_returns_cached_pg_backed_singleton(monkeypatch):
    _FakePgStore.created_dsns.clear()
    _FakePgStore.closed.clear()
    monkeypatch.setattr("app.pipelines.pg_artifact_store.PgCatalogArtifactStore", _FakePgStore)
    store_mod.reset_catalog_store()

    first = store_mod.get_catalog_store()
    second = store_mod.get_catalog_store()

    assert first is second
    assert isinstance(first, _FakePgStore)
    assert first.dsn == get_settings().catalog_db_url
    assert _FakePgStore.created_dsns == [get_settings().catalog_db_url]  # 1회만 생성(캐시)

    store_mod.reset_catalog_store()
    assert _FakePgStore.closed == [True]


def test_get_catalog_store_is_thread_safe_under_concurrent_first_call(monkeypatch):
    """PR #42 리뷰 — 락 없는 check-then-act 는 동시 최초호출 시 커넥션 풀 중복 생성 위험."""
    _FakePgStore.created_dsns.clear()
    _FakePgStore.closed.clear()
    monkeypatch.setattr("app.pipelines.pg_artifact_store.PgCatalogArtifactStore", _SlowFakePgStore)
    store_mod.reset_catalog_store()

    results: list[object] = []

    def call() -> None:
        results.append(store_mod.get_catalog_store())

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_FakePgStore.created_dsns) == 1  # 딱 한 번만 생성
    assert len({id(r) for r in results}) == 1  # 전 스레드가 같은 인스턴스를 받음

    store_mod.reset_catalog_store()
