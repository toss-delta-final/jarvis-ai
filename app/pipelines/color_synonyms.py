"""승인된 색상 동의어의 런타임 조회·TTL 캐시 (이슈 #258)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable

_log = logging.getLogger(__name__)

_pools: dict[str, object] = {}
_pool_lock = threading.Lock()
_cache_lock = threading.Lock()
# 성공 항목은 사전을 담고, 실패 항목은 아래 _LOAD_FAILED 센티널을 담는다(negative caching) —
# DB 가 죽어 있는 동안 TTL 창당 재연결 시도를 1회로 유계화한다(#258 CI hang 회귀).
_cache: dict[str, tuple[float, dict[str, list[str]] | object]] = {}
_LOAD_FAILED = object()
_empty_map_warning_lock = threading.Lock()
_empty_map_warning_until: dict[str, float] = {}


class ColorSynonymLoadUnavailable(Exception):
    """직전 TTL 창 안에서 승인 사전 로드가 실패해 재시도를 보류 중임을 알린다."""


def _norm(value: str) -> str:
    """search_service._norm_attr 관례와 같은 strip + casefold 정규화."""
    return value.strip().casefold()


def _get_pool(dsn: str):
    pool = _pools.get(dsn)
    if pool is None:
        with _pool_lock:
            pool = _pools.get(dsn)
            if pool is None:
                from app.core.config import get_settings  # noqa: PLC0415 - lazy 설정 경로
                from app.core.pg_resilience import hardened_pg_conninfo  # noqa: PLC0415
                from pgvector.psycopg import register_vector  # noqa: PLC0415
                from psycopg_pool import ConnectionPool  # noqa: PLC0415 - lazy DB dependency

                # 런타임 승인 조회와 배치 수확이 이 dsn별 풀 하나를 공유해 두 플래그를 함께
                # 켜도 색상 보조 경로의 총 연결 수가 config 상한을 넘지 않는다. 배치의 pgvector
                # 입출력도 같은 풀에서 안전하도록 모든 연결에 vector adapter를 등록한다.
                # 캐시 키는 원본 dsn 그대로 둔다 — color_synonym_seed._get_pool 등 같은 dsn 을
                # 넘기는 호출부와 풀을 공유하려면 키가 갈라지면 안 된다. connect_timeout·
                # keepalives·tcp_user_timeout 병합(이 저장소 pg 연결 공통 관례, category_seed.py 등)은
                # 실제 접속 문자열에만 적용한다.
                max_size = get_settings().color_synonym_pool_max_size
                pool = ConnectionPool(
                    hardened_pg_conninfo(dsn),
                    configure=register_vector,
                    open=True,
                    min_size=min(4, max_size),
                    max_size=max_size,
                )
                _pools[dsn] = pool
    return pool


def build_synonym_map(rows: Iterable[tuple[str, str, int | None]]) -> dict[str, list[str]]:
    """`(term, canonical, doc_count)` 행에서 정규화 표기→결정적 묶음 사전을 만든다(순수 함수).

    `load_synonym_map`의 DB 질의 결과와 정본 JSON(검수 완료 승인 행)이 같은 규칙으로
    묶이는지를 이 함수 하나로 검증할 수 있도록 DB I/O 와 그룹핑 로직을 분리했다(#258 리뷰 F2)
    — DB 호출부는 이 함수를 감싸기만 하고, 정렬·`_norm` 적용 순서는 이 함수가 유일한 정본이다.
    행은 이미 `status='approved' AND canonical IS NOT NULL` 로 걸러졌다고 가정한다.
    """
    groups: dict[str, list[tuple[str, int]]] = {}
    for term, canonical, doc_count in rows:
        groups.setdefault(canonical, []).append((term, doc_count or 0))

    mapping: dict[str, list[str]] = {}
    for members in groups.values():
        ordered = [term for term, _ in sorted(members, key=lambda item: (-item[1], item[0]))]
        for term in ordered:
            mapping[_norm(term)] = ordered.copy()
    return mapping


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

    return build_synonym_map(rows)


def reset_cache() -> None:
    """테스트·운영 수동 갱신용 인프로세스 사전 캐시 초기화."""
    with _cache_lock:
        _cache.clear()
    with _empty_map_warning_lock:
        _empty_map_warning_until.clear()


def _warn_empty_map_once(dsn: str, *, expires_at: float, enabled: bool) -> None:
    """확장 중 빈 승인 사전의 무동작을 해당 TTL 창에 한 번만 알린다."""
    if not enabled:
        return
    with _empty_map_warning_lock:
        if _empty_map_warning_until.get(dsn, 0.0) >= expires_at:
            return
        _empty_map_warning_until[dsn] = expires_at
    _log.warning(
        "색상 동의어 사전 비어 있음 — status='approved' AND canonical IS NOT NULL 0행, 확장 무동작"
    )


def get_synonym_map(dsn: str, *, ttl_s: float, warn_if_empty: bool = False) -> dict[str, list[str]]:
    """dsn별 승인 사전을 monotonic TTL 동안 재사용하며 DB 조회 중 캐시 락을 놓는다.

    실패도 같은 TTL 동안 캐시한다(negative caching, #258 CI hang 회귀) — DB 가 죽어 있으면
    `load_synonym_map` 성공 전까지 캐시가 영원히 안 채워져 호출마다 새 연결을 시도했다.
    TTL 이 지나면 다시 시도한다(영구 차단 아님 — DB 복구를 스스로 감지).
    """
    now = time.monotonic()
    cached = _cache.get(dsn)
    if cached is not None and now < cached[0]:
        if cached[1] is _LOAD_FAILED:
            raise ColorSynonymLoadUnavailable(dsn)
        mapping = cached[1]  # type: ignore[assignment]
        if not mapping:
            _warn_empty_map_once(dsn, expires_at=cached[0], enabled=warn_if_empty)
        return mapping  # type: ignore[return-value]
    # TTL 갱신 창에는 같은 dsn 쿼리가 겹칠 수 있지만, DB 호출 동안 전역 캐시 락을 잡아 다른
    # 검색 스레드의 wait_for 예산을 락 대기로 소모시키는 것보다 낫다. 결과 반영 시 먼저 끝난
    # 스레드의 유효 값을 재확인해 같은 dsn 캐시를 불필요하게 덮어쓰지 않는다.
    try:
        mapping = load_synonym_map(dsn)
    except Exception:
        with _cache_lock:
            now = time.monotonic()
            cached = _cache.get(dsn)
            if cached is not None and now < cached[0]:
                if cached[1] is _LOAD_FAILED:
                    raise ColorSynonymLoadUnavailable(dsn) from None
                return cached[1]  # type: ignore[return-value]
            _cache[dsn] = (now + max(0.0, ttl_s), _LOAD_FAILED)
        _log.warning("색상 동의어 사전 로드 실패 — TTL 만료까지 재시도 보류", exc_info=True)
        raise
    with _cache_lock:
        now = time.monotonic()
        cached = _cache.get(dsn)
        if cached is not None and now < cached[0] and cached[1] is not _LOAD_FAILED:
            if not cached[1]:
                _warn_empty_map_once(dsn, expires_at=cached[0], enabled=warn_if_empty)
            return cached[1]  # type: ignore[return-value]
        expires_at = now + max(0.0, ttl_s)
        _cache[dsn] = (expires_at, mapping)
        if not mapping:
            _warn_empty_map_once(dsn, expires_at=expires_at, enabled=warn_if_empty)
        return mapping


def expand_color(value: str, mapping: dict[str, list[str]]) -> list[str]:
    """색상 한 값을 승인 묶음으로 확장하며 미등록 표기는 원문 그대로 둔다.

    복합 표기도 분해하지 않고 전체 문자열로만 lookup한다. 미등록이면 다른 표기와 똑같이 원문
    하나를 반환하므로 BE attributes LIKE의 의미를 바꾸지 않는다.
    """
    if not value.strip():
        return [value]
    return list(mapping.get(_norm(value), [value]))
