"""AI 생성물 카탈로그 스토어 (api-spec §4.8, I-17 배치).

CatalogArtifact·CatalogArtifactStore(인메모리)·ArtifactStore(공유 계약)를 정의한다.
[2026-07-20 이슈 #31] 프로덕션 진입점 get_catalog_store()는 pg-catalog(pgvector)로 이관 완료 —
PgCatalogArtifactStore(pg_artifact_store.py)를 반환한다. CatalogArtifactStore(인메모리)는
테스트 주입용(격리, tests/conftest.py InMemory 컨벤션과 동일 원칙)과 full_rebuild 임시 버퍼로
계속 쓰인다. AI 생성물(extras·search_doc·임베딩)만 보관하고 상품 원본 컬럼 사본은 두지 않는다(CLAUDE.md).

[이슈 #416] ``FailureStreakTable`` 은 artifacts_batch.py 의 2선·3선(연속 실패 스트릭) cross-cycle
상태를 담는 최소 단위다. ArtifactStore 가 이미 배치 커서를 들고 있어 "배치 영속 상태"의 자연스러운
자리이고, 유닛 테스트가 이미 인메모리 스토어를 주입하고 있어 새 전역 싱글턴을 추가하지 않아도 된다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_log = logging.getLogger(__name__)

# [이슈 #416] FailureStreakTable.bump() 의 kind 인자 — 오타 방지용 상수 2종.
FAILURE_STREAK_KIND_ITEM = "item"  # key = str(product_id), artifacts_batch 2선
FAILURE_STREAK_KIND_PAGE = "page"  # key = cursor, artifacts_batch 3선
EXTRAS_NAME_PRESENT_KEY = "name_present"

# 방어적 메모리 상한(튜너블 아님) — 구 _ITEM_FAILURE_STREAK_MAX_ENTRIES/_PAGE_FAILURE_STREAK_MAX_ENTRIES
# (artifacts_batch.py, #325)와 동일 값(10,000)을 kind 별로 독립 적용한다(item 10,000 · page
# 10,000 — 합산이 아니다). [T4, PR 리뷰 라운드 2] 한 dict(kind, key) 로 합치면서 상한을
# "합산 10,000"으로 잘못 적용해 구 동작(각 10,000, 총량 20,000)의 절반으로 줄어 있었다 —
# FailureStreakTable 을 kind 별 하위 dict 로 나눠 다시 독립시킨다.
_FAILURE_STREAK_MAX_ENTRIES = 10_000


@dataclass
class _FailureStreakEntry:
    count: int
    updated_at: float


class FailureStreakTable:
    """(kind, key) 별 연속 실패 횟수 인메모리 표 — cross-cycle TTL 리셋 지원(이슈 #416).

    시각 소스를 주입 가능하게 해(기본 ``time.monotonic``) 테스트가 TTL 경과를 조작할 수 있다.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # [T4, PR 리뷰 라운드 2] kind 별 독립 하위 dict — 방어적 상한(_FAILURE_STREAK_MAX_ENTRIES)과
        # 상한 초과 시 비움을 kind 단위로 적용하기 위해서다(합산 dict 는 한 kind 의 항목 수가
        # 다른 kind 의 방어적 비움에 영향을 주고, 대량 실패 상황에서 그 비움이 #416 이 고치려던
        # "상한에 영영 도달하지 못한다"를 재현한다).
        self._entries: dict[str, dict[str, _FailureStreakEntry]] = {}

    def bump(self, kind: str, key: str, *, ttl_s: float) -> int:
        """(kind, key) 스트릭을 1 늘리고 반환한다. 마지막 갱신이 ttl_s 보다 오래됐으면 1로 리셋."""
        now = self._clock()
        kind_entries = self._entries.setdefault(kind, {})
        entry = kind_entries.get(key)
        if entry is None:
            if len(kind_entries) >= _FAILURE_STREAK_MAX_ENTRIES:
                _log.warning(
                    "I-17 실패 스트릭 캐시가 kind=%s 상한(%d)에 도달 — 그 kind 만 방어적으로 비움",
                    kind,
                    _FAILURE_STREAK_MAX_ENTRIES,
                )
                kind_entries.clear()
            kind_entries[key] = _FailureStreakEntry(count=1, updated_at=now)
            return 1
        if now - entry.updated_at > ttl_s:
            entry.count = 1
        else:
            entry.count += 1
        entry.updated_at = now
        return entry.count

    def peek(self, kind: str, key: str, *, ttl_s: float) -> int:
        """(kind, key) 의 현재 스트릭을 변경 없이 반환한다(없거나 만료됐으면 0).

        [T8, PR 리뷰 라운드 6] ``PgCatalogArtifactStore.bump_failure_streak`` 가 DB 성공
        경로에서 이 폴백 표에 남아 있는 진행분(간헐적 DB 실패로 여기 쌓인 것)을 흡수할 때
        쓴다 — ``bump()`` 와 같은 TTL 기준으로 "연속"이 끊겼으면 0 을 돌려줘 무관한 과거
        진행분이 새 DB 스트릭에 섞이지 않게 한다.
        """
        kind_entries = self._entries.get(kind)
        if kind_entries is None:
            return 0
        entry = kind_entries.get(key)
        if entry is None or self._clock() - entry.updated_at > ttl_s:
            return 0
        return entry.count

    def clear(self, kind: str, key: str) -> None:
        kind_entries = self._entries.get(kind)
        if kind_entries is not None:
            kind_entries.pop(key, None)

    def purge_stale(self, ttl_s: float) -> int:
        """마지막 갱신이 ttl_s 보다 오래된 엔트리를 모든 kind 에서 지우고 지운 개수를 반환한다."""
        now = self._clock()
        removed = 0
        for kind_entries in self._entries.values():
            stale = [k for k, entry in kind_entries.items() if now - entry.updated_at > ttl_s]
            for k in stale:
                del kind_entries[k]
            removed += len(stale)
        return removed


@dataclass
class CatalogArtifact:
    """상품 1건의 AI 생성물. 상품 원본 필드는 별도 컬럼으로 보관하지 않는다.

    임베딩 프로비넌스(embed_model·embed_dim·embed_task·normalized)는 벡터의 출처 메타로,
    모델 교체 후 낡은 행을 판별하는 근거다. 인메모리 생성 호환 위해 기본 None.
    """

    product_id: int
    search_doc: str
    embedding: list[float]
    extras: dict = field(default_factory=dict)
    embed_model: str | None = None
    embed_dim: int | None = None
    embed_task: str | None = None
    normalized: bool | None = None


@runtime_checkable
class ArtifactStore(Protocol):
    """CatalogArtifactStore(인메모리)·PgCatalogArtifactStore(pg-catalog) 공유 계약 (이슈 #31).

    인터페이스가 바뀌면 양쪽 구현체를 함께 고쳐야 한다 — 타입체커가 시그니처 드리프트를 잡아준다.
    """

    def upsert(self, artifact: CatalogArtifact) -> None: ...
    def delete(self, product_id: int) -> None: ...
    def clear(self) -> None: ...
    def replace_all(self, artifacts: list[CatalogArtifact]) -> None: ...
    def replace_all_and_set_cursor(
        self, artifacts: list[CatalogArtifact], cursor: str | None
    ) -> None: ...
    def get(self, product_id: int) -> CatalogArtifact | None: ...
    def get_many(self, product_ids: list[int]) -> dict[int, CatalogArtifact]: ...
    def all(self) -> list[CatalogArtifact]: ...
    def count(self) -> int: ...
    def top_k_by_vector(
        self,
        query_vec: list[float],
        *,
        k: int,
        exclude: set[int] | None = None,
        include: set[int] | None = None,
    ) -> list[int]: ...
    def get_cursor(self) -> str | None: ...
    def set_cursor(self, cursor: str | None) -> None: ...
    def bump_failure_streak(self, kind: str, key: str, *, ttl_s: float) -> int: ...
    def clear_failure_streak(self, kind: str, key: str) -> None: ...
    def purge_stale_failure_streaks(self, ttl_s: float) -> int: ...


class CatalogArtifactStore:
    """AI 생성물 인메모리 스토어 (productId 키) + 배치 커서 + 실패 스트릭(#416)."""

    def __init__(self) -> None:
        self._items: dict[int, CatalogArtifact] = {}
        self._cursor: str | None = None
        self._failure_streaks = FailureStreakTable()

    def upsert(self, artifact: CatalogArtifact) -> None:
        self._items[artifact.product_id] = artifact

    def delete(self, product_id: int) -> None:
        self._items.pop(product_id, None)  # HIDDEN — 생성물 제거(유령 상품 방지, §4.8)

    def clear(self) -> None:
        self._items.clear()

    def replace_all(self, artifacts: list[CatalogArtifact]) -> None:
        """전체 재구축 원자 교체 — 성공한 임시 결과로 한 번에 스왑(중간 실패 시 기존 데이터 보존)."""
        self._items = {a.product_id: a for a in artifacts}

    def replace_all_and_set_cursor(
        self, artifacts: list[CatalogArtifact], cursor: str | None
    ) -> None:
        """전체 생성물과 커서를 한 상태 전환으로 교체한다."""
        self._items = {a.product_id: a for a in artifacts}
        self._cursor = cursor

    def get(self, product_id: int) -> CatalogArtifact | None:
        return self._items.get(product_id)

    def get_many(self, product_ids: list[int]) -> dict[int, CatalogArtifact]:
        """요청 id 를 productId→artifact dict 로 반환(없는 id 는 생략) — 방식2 재정렬 N+1 제거(#101)."""
        return {pid: self._items[pid] for pid in product_ids if pid in self._items}

    def all(self) -> list[CatalogArtifact]:
        return list(self._items.values())

    def count(self) -> int:
        return len(self._items)

    def top_k_by_vector(
        self,
        query_vec: list[float],
        *,
        k: int,
        exclude: set[int] | None = None,
        include: set[int] | None = None,
    ) -> list[int]:
        """질의 벡터에 가까운 상위 k productId (I-22 랭킹, #148).

        인메모리 구현은 **정확한 코사인**을 쓴다 — 건수가 작아 근사가 불필요하고, 테스트가 기대하는
        순서가 명확해야 한다(pg 구현은 HNSW 근사, `pg_artifact_store` 주석 참조).
        include 가 주어지면 해당 productId 집합 안에서만 채점하며, 빈 집합은 즉시 빈 결과다.
        exclude 와 함께 주어지면 두 필터를 모두 적용한다.
        동점은 `product_id` 오름차순으로 tiebreak 해 결정적이다.
        """
        from app.services.search_service import cosine_similarity  # noqa: PLC0415 - 순환 임포트 회피

        if include == set():
            return []
        skip = exclude or set()
        scored = [
            (cosine_similarity(query_vec, a.embedding), a.product_id)
            for a in self._items.values()
            if a.embedding
            and a.product_id not in skip
            and (include is None or a.product_id in include)
        ]
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [pid for _, pid in scored[:k]]

    def get_cursor(self) -> str | None:
        return self._cursor

    def set_cursor(self, cursor: str | None) -> None:
        self._cursor = cursor

    def bump_failure_streak(self, kind: str, key: str, *, ttl_s: float) -> int:
        return self._failure_streaks.bump(kind, key, ttl_s=ttl_s)

    def clear_failure_streak(self, kind: str, key: str) -> None:
        self._failure_streaks.clear(kind, key)

    def purge_stale_failure_streaks(self, ttl_s: float) -> int:
        return self._failure_streaks.purge_stale(ttl_s)


_store: ArtifactStore | None = None
_store_lock = threading.Lock()


def get_catalog_store() -> ArtifactStore:
    """전역 스토어 싱글턴 — pg-catalog(PgCatalogArtifactStore) 반환 (이슈 #31).

    pg_artifact_store 를 함수 내부에서 LAZY import 한다(artifact_store.py → pg_artifact_store.py
    → artifact_store.py 순환 임포트 회피). 테스트는 store 를 직접 주입해 이 경로를 타지 않는다.

    이중 확인 락(double-checked locking) — 스케줄러(별도 OS 스레드)와 요청 처리(이벤트루프)가
    최초 호출에 동시 진입하면 락 없이는 커넥션 풀이 중복 생성되고 하나가 샌다(PR #42 리뷰).
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from app.core.config import get_settings  # noqa: PLC0415
                from app.pipelines.pg_artifact_store import PgCatalogArtifactStore  # noqa: PLC0415

                _store = PgCatalogArtifactStore(get_settings().catalog_db_url)
    return _store


def reset_catalog_store() -> None:
    """테스트 격리용 리셋 — 연결 풀이 있으면 정리 후 캐시를 비운다."""
    global _store
    if _store is not None and hasattr(_store, "close"):
        _store.close()
    _store = None
