"""판매자 카테고리 스냅샷 카탈로그 (#506, api-spec §3.2 v0.30.0).

BE 조회 없이 AI 가 로컬 JSON(`settings.seller_category_snapshot_path`)으로 카테고리를
보유한다. 이 스냅샷이 다음 세 가지의 **단일 원천**이다:

1. 등록 초안의 카테고리 **선택지** — vision 힌트로 후보를 검색해 product 에이전트에
   주입하고, LLM 은 후보 id 중에서만 고른다(목록 밖 값은 hitl 검증이 되묻기로 전환).
2. `preview.categoryPath` 문자열 생성 — "패션의류/잡화 > 남성의류 > 셔츠".
3. confirm 시 Spring I-10 `category` 쓰기 값 — `seller_category_write_mode` 로 결정.

"계약값은 코드" 원칙(hitl.DraftRecord docstring)과 동일 사상: LLM 은 id 를 고를 뿐
path 조립·쓰기 값 변환은 전부 이 모듈(코드)이 담당한다.

스냅샷 정합 주의: BE 를 조회하지 않으므로 파일이 낡으면 존재하지 않는 카테고리로
등록될 수 있다 — 파일 교체가 곧 배포이며 meta `version` 으로 추적한다(CHANGELOG 기록).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CategoryEntry:
    """스냅샷 1건 — id 는 문자열(스노플레이크가 JS Number 정밀도를 넘는다, FE 계약 동일)."""

    id: str
    path: tuple[str, ...]
    synonyms: tuple[str, ...] = ()

    @property
    def leaf(self) -> str:
        return self.path[-1]

    @property
    def path_str(self) -> str:
        """FE preview.categoryPath 표기 — 구분자는 계약 고정(" > ")."""
        return " > ".join(self.path)


class CategorySnapshotError(ValueError):
    """스냅샷 파일 자체의 결함 — 기동 시 fail-fast 대상(부분 로드 금지)."""


def _parse_entry(raw: object, seen_ids: set[str]) -> CategoryEntry:
    """항목 1건 검증 — category_seed.load_leaves 의 '비문자열은 거부' 스타일."""
    if not isinstance(raw, dict):
        raise CategorySnapshotError(f"카테고리 항목은 객체여야 합니다: {raw!r}")
    entry_id = raw.get("id")
    path = raw.get("path")
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise CategorySnapshotError(f"카테고리 id 는 비어있지 않은 문자열이어야 합니다: {raw!r}")
    if entry_id in seen_ids:
        raise CategorySnapshotError(f"카테고리 id 중복: {entry_id}")
    if (
        not isinstance(path, list)
        or not path
        or any(not isinstance(seg, str) or not seg.strip() for seg in path)
    ):
        raise CategorySnapshotError(
            f"카테고리 path 는 비어있지 않은 문자열 배열이어야 합니다: {raw!r}"
        )
    synonyms_raw = raw.get("synonyms", [])
    if not isinstance(synonyms_raw, list) or any(not isinstance(s, str) for s in synonyms_raw):
        raise CategorySnapshotError(f"카테고리 synonyms 는 문자열 배열이어야 합니다: {raw!r}")
    seen_ids.add(entry_id)
    # 모르는 키는 무시한다 — FE 의 "모르는 kind 무시"와 같은 확장 규칙(예: medianPrice).
    return CategoryEntry(
        id=entry_id,
        path=tuple(seg.strip() for seg in path),
        synonyms=tuple(s.strip() for s in synonyms_raw if s.strip()),
    )


def _load_file(path: Path) -> tuple[CategoryEntry, ...]:
    if not path.exists():
        raise CategorySnapshotError(f"카테고리 스냅샷 파일이 없습니다: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CategorySnapshotError(f"카테고리 스냅샷 JSON 파싱 실패: {path} ({exc})") from exc
    # 최상위는 {"version":…, "categories":[…]} 또는 순수 배열 둘 다 허용한다 —
    # meta(version) 표기를 권장하되 강제하지 않는다(초기 수기 관리 편의).
    raw_entries = data.get("categories") if isinstance(data, dict) else data
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CategorySnapshotError(f"카테고리 스냅샷이 비어 있습니다: {path}")
    seen: set[str] = set()
    entries = tuple(_parse_entry(raw, seen) for raw in raw_entries)
    version = data.get("version") if isinstance(data, dict) else None
    logger.info("seller category snapshot loaded entries=%d version=%s", len(entries), version)
    return entries


@lru_cache(maxsize=1)
def _catalog() -> dict[str, CategoryEntry]:
    settings = get_settings()
    entries = _load_file(Path(settings.seller_category_snapshot_path))
    return {entry.id: entry for entry in entries}


def reset_catalog_cache() -> None:
    """테스트 전용 — 스냅샷 경로(settings)를 바꾼 뒤 재로드할 때 호출한다."""
    _catalog.cache_clear()


def get(category_id: str) -> CategoryEntry | None:
    """id 조회 — 스냅샷에 없으면 None(호출부가 되묻기로 전환)."""
    return _catalog().get(category_id)


def path_str(category_id: str) -> str | None:
    entry = get(category_id)
    return entry.path_str if entry else None


def _normalize(text: str) -> str:
    return text.strip().lower().replace(" ", "")


def search(query: str, k: int | None = None) -> list[CategoryEntry]:
    """카테고리 후보 검색 — leaf 완전일치 > leaf 부분일치 > path 부분일치 > synonyms.

    임베딩을 쓰지 않는 이유: 수천 건 인메모리 문자열 매칭이면 충분하고, vision 이
    category_hint 를 "상품군 표준 명사 1개"로 내놓도록 프롬프트가 유도한다. 부족해지면
    같은 시그니처로 pgvector 검색으로 교체한다.
    """
    if k is None:
        k = get_settings().seller_category_candidates_k
    needle = _normalize(query)
    if not needle:
        return []
    scored: list[tuple[int, CategoryEntry]] = []
    for entry in _catalog().values():
        leaf = _normalize(entry.leaf)
        full = _normalize(entry.path_str)
        if leaf == needle:
            score = 0
        elif needle in leaf or leaf in needle:
            score = 1
        elif needle in full:
            score = 2
        elif any(_normalize(s) == needle or needle in _normalize(s) for s in entry.synonyms):
            score = 3
        else:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda item: (item[0], item[1].path_str))
    return [entry for _, entry in scored[:k]]


def candidates_block(entries: list[CategoryEntry]) -> str:
    """product 에이전트 입력에 주입할 [카테고리 후보] 블록 — "id | path" 1줄 1건."""
    return "\n".join(f"- {entry.id} | {entry.path_str}" for entry in entries)


def spring_write_value(category_id: str) -> str | None:
    """confirm 시 I-10 `category` 필드에 쓸 값 — BE 와 맞출 유일한 지점(config 주석).

    스냅샷에 없는 id 는 None — validate_draft 가 선검증하므로 정상 경로에서는
    발생하지 않지만, 스냅샷 교체(배포) 사이의 draft 는 여기서 걸릴 수 있다.
    """
    entry = get(category_id)
    if entry is None:
        return None
    mode = get_settings().seller_category_write_mode
    if mode == "id":
        return entry.id
    if mode == "path":
        return entry.path_str
    return entry.leaf
