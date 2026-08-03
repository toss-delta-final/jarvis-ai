"""승인된 색상 동의어의 런타임 조회·TTL 캐시 (이슈 #258)."""

from __future__ import annotations

import threading
import time

_pools: dict[str, object] = {}
_pool_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}


def _norm(value: str) -> str:
    """search_service._norm_attr 관례와 같은 strip + casefold 정규화."""
    return value.strip().casefold()


def _get_pool(dsn: str):
    pool = _pools.get(dsn)
    if pool is None:
        with _pool_lock:
            pool = _pools.get(dsn)
            if pool is None:
                from psycopg_pool import ConnectionPool  # noqa: PLC0415 - lazy DB dependency

                # 와이어 확장 플래그 활성화 전에는 풀 max_size 설정화와 DB 장애 negative
                # caching을 함께 도입해야 요청별 연결 재시도 비용이 검색 경로에 누적되지 않는다.
                pool = ConnectionPool(dsn, open=True)
                _pools[dsn] = pool
    return pool


def load_synonym_map(dsn: str) -> dict[str, list[str]]:
    """승인되고 canonical 이 있는 행만 읽어 정규화 표기→결정적 묶음 사전을 만든다."""
    from app.core.config import get_settings  # noqa: PLC0415 - 순환 임포트 회피(모듈 관례)

    timeout_ms = int(get_settings().catalog_store_query_timeout_s * 1000)
    with _get_pool(dsn).connection() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
        rows = conn.execute(
            """
            SELECT term, canonical, doc_count
            FROM color_synonyms
            WHERE status = 'approved' AND canonical IS NOT NULL
            ORDER BY canonical, doc_count DESC NULLS LAST, term
            """
        ).fetchall()

    groups: dict[str, list[tuple[str, int]]] = {}
    for term, canonical, doc_count in rows:
        groups.setdefault(canonical, []).append((term, doc_count or 0))

    mapping: dict[str, list[str]] = {}
    for members in groups.values():
        ordered = [term for term, _ in sorted(members, key=lambda item: (-item[1], item[0]))]
        for term in ordered:
            mapping[_norm(term)] = ordered.copy()
    return mapping


def reset_cache() -> None:
    """테스트·운영 수동 갱신용 인프로세스 사전 캐시 초기화."""
    with _cache_lock:
        _cache.clear()


def get_synonym_map(dsn: str, *, ttl_s: float) -> dict[str, list[str]]:
    """dsn별 승인 사전을 monotonic TTL 동안 재사용한다."""
    now = time.monotonic()
    cached = _cache.get(dsn)
    if cached is not None and now < cached[0]:
        return cached[1]
    with _cache_lock:
        now = time.monotonic()
        cached = _cache.get(dsn)
        if cached is not None and now < cached[0]:
            return cached[1]
        mapping = load_synonym_map(dsn)
        _cache[dsn] = (now + max(0.0, ttl_s), mapping)
        return mapping


def expand_color(value: str, mapping: dict[str, list[str]]) -> list[str]:
    """색상 한 값을 승인 묶음으로 확장하며 미등록 표기는 원문 그대로 둔다.

    복합 표기도 분해하지 않고 전체 문자열로만 lookup한다. 미등록이면 다른 표기와 똑같이 원문
    하나를 반환하므로 BE attributes LIKE의 의미를 바꾸지 않는다.
    """
    if not value.strip():
        return [value]
    return list(mapping.get(_norm(value), [value]))
