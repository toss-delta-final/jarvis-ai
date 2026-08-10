"""판매자 카테고리 스냅샷 카탈로그 (#506, api-spec §3.2 v0.31.0).

BE 조회 없이 AI 가 로컬 JSON(`settings.seller_category_snapshot_path`)으로 카테고리를
보유한다. 이 스냅샷이 다음 세 가지의 **단일 원천**이다:

1. 등록 초안의 카테고리 **선택지** — vision 힌트·판매자 발화로 후보를 검색해 product
   에이전트에 주입하고, LLM 은 후보 id 중에서만 고른다(목록 밖 값은 hitl 이 거른다).
2. `preview.categoryPath` 문자열 생성 — "패션의류/잡화 > 남성의류 > 셔츠/남방".
3. confirm 시 Spring I-10 `categoryId` 쓰기 값 — `spring_category_id` 한 곳에서만 캐스팅.

"계약값은 코드" 원칙(hitl.DraftRecord docstring)과 동일 사상: LLM 은 id 를 고를 뿐
path 조립·쓰기 값 변환은 전부 이 모듈(코드)이 담당한다.

계층 규약 — **path 는 항상 2칸**이다 (2026-08-09 실데이터 정합):
    path = ["<대분류>", "<중분류> > <소분류>"]
정본 DB(`category`)가 2단이고(02 D20 — parent NULL=대분류, 값 있음=소분류) 소분류
`name` 이 이미 "중분류 > 소분류" 병합형이기 때문이다. 판매자가 채우는 칸이 언제나
[대분류]+[중소] 2개가 되게 하려는 계약이며, 3단 소스는 생성기가 중+소를 병합해 낮춘다.
표시(`path_str`)는 3토막으로 펼쳐 사람이 읽기 좋게 하고, 매칭은 대/중/소 각 조각을
따로 본다(`search`).

스냅샷 정합 주의: BE 를 조회하지 않으므로 파일이 낡으면 존재하지 않는 카테고리로
등록될 수 있다 — 파일 교체가 곧 배포이며 meta `version` 으로 추적한다(CHANGELOG 기록).
파일은 손으로 고치지 않는다: `scripts/build_seller_category_snapshot.py` 가 정본 DB
(또는 mysqldump)에서 생성한다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

SEPARATOR = " > "

# 검색어에서 걷어낼 잡음 — 판매자 발화는 "이 셔츠 3만원에 50개 등록해줘" 처럼 카테고리와
# 무관한 어휘가 섞여 온다. 남겨두면 짧은 n-gram 이 엉뚱한 카테고리를 끌어온다.
_QUERY_NOISE = re.compile(
    r"(등록|올려|올릴|판매|팔아|팔려|팔게|상품|물건|제품|해줘|해주세요|주세요|하고싶|"
    r"할래|싶어|입니다|이에요|예요|이야|이거|저거|그거|같은|정도|원에|원짜리|개씩|"
    r"[0-9]+\s*(원|개|만원|천원|장|박스|세트))"
)
# 남길 문자 — 한글·영숫자만. 구분자·조사 주변 기호는 n-gram 을 갈라 매칭을 방해한다.
_KEEP = re.compile(r"[^0-9a-z가-힣]+")

_MIN_GRAM = 2
_MAX_GRAM = 8

# 매칭 계층 가중치 — 작을수록 좋은 후보. 소분류가 상품군을 가장 정확히 지목한다.
_LEVEL_MINOR = 0
_LEVEL_SYNONYM = 4
_LEVEL_MIDDLE = 8
_LEVEL_MAJOR = 16


@dataclass(frozen=True)
class CategoryEntry:
    """스냅샷 1건 — id 는 문자열(BIGINT 가 JS Number 정밀도를 넘는다, FE 계약 동일).

    path 는 2칸 고정: (대분류, "중분류 > 소분류"). 셋째 칸이 오면 생성기 버그다.
    """

    id: str
    path: tuple[str, ...]
    synonyms: tuple[str, ...] = ()

    @property
    def major(self) -> str:
        """대분류 — DB `category` 의 parent 행 이름."""
        return self.path[0]

    @property
    def leaf(self) -> str:
        """소분류 — DB `category.name` 그대로("중분류 > 소분류" 병합형)."""
        return self.path[-1]

    @property
    def middle(self) -> str | None:
        """병합명 앞 조각(중분류). 병합되지 않은 이름이면 None."""
        head, sep, _ = self.leaf.partition(SEPARATOR)
        return head.strip() if sep else None

    @property
    def minor(self) -> str:
        """병합명 뒤 조각(소분류). 병합되지 않았으면 leaf 그대로."""
        _, sep, tail = self.leaf.partition(SEPARATOR)
        return tail.strip() if sep else self.leaf

    @property
    def path_str(self) -> str:
        """FE preview.categoryPath 표기 — 구분자는 계약 고정(" > ").

        leaf 가 이미 " > " 를 품고 있어 결과는 "대 > 중 > 소" 3토막으로 펼쳐진다.
        """
        return SEPARATOR.join(self.path)

    @property
    def sub_path(self) -> str:
        """[#541] 대분류를 뺀 나머지 — 판매자가 채우는 **둘째 칸**("중분류 > 소분류").

        `major` 와 이 값이 preview 의 2칸 표기이며 다음 **불변식**을 지킨다:

            path_str == major + SEPARATOR + sub_path   (path 가 2칸 이상일 때)

        2칸 스냅샷에서는 `leaf` 와 값이 같지만 `leaf`(= path[-1])로 만들지 **않는다** —
        3칸짜리 낡은 스냅샷이 섞이면 중분류가 표기에서 조용히 사라져 카드가 보여준
        카테고리와 실제 등록될 카테고리가 달라진다. 카테고리는 등록 후 변경 불가라
        그 오차의 비용이 크다("보여준 것 == 실행하는 것", preview 모듈 docstring).
        """
        return SEPARATOR.join(self.path[1:])

    def terms(self) -> list[tuple[int, str]]:
        """(계층 가중치, 매칭어) 목록 — 검색 인덱스와 정밀 채점이 함께 쓴다."""
        items: list[tuple[int, str]] = [(_LEVEL_MINOR, self.minor)]
        if (middle := self.middle) is not None:
            items.append((_LEVEL_MIDDLE, middle))
        items.append((_LEVEL_MAJOR, self.major))
        # 대분류가 "패션의류/잡화" 처럼 슬래시 복합어면 조각도 매칭어로 쓴다.
        items.extend((_LEVEL_MAJOR, part) for part in self.major.split("/") if len(part) >= 2)
        items.extend((_LEVEL_SYNONYM, syn) for syn in self.synonyms)
        return [(level, term) for level, term in items if term]


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
    if not entry_id.strip().isdigit():
        # BE `categoryId` 는 Long 이다 — 여기서 막지 않으면 confirm 시점(사용자가 버튼을
        # 누른 뒤)에야 캐스팅이 터진다. 기동 시 fail-fast 가 훨씬 싸다.
        raise CategorySnapshotError(f"카테고리 id 는 숫자 문자열이어야 합니다: {entry_id!r}")
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
    # meta(version) 표기를 권장하되 강제하지 않는다(테스트 픽스처 편의).
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


@lru_cache(maxsize=1)
def _gram_index() -> dict[str, set[str]]:
    """문자 n-gram → 후보 entry id 집합 (스냅샷당 1회 구축).

    1000건짜리 스냅샷을 발화마다 완전 탐색하면(부분일치 판정 3000회 × 문자열 연산)
    SSE hot path 예산을 갉아먹는다. 조회는 dict 히트만 하도록 미리 펼쳐 둔다.
    """
    index: dict[str, set[str]] = {}
    for entry in _catalog().values():
        for _level, term in entry.terms():
            token = _normalize(term)
            for size in range(_MIN_GRAM, min(len(token), _MAX_GRAM) + 1):
                for start in range(len(token) - size + 1):
                    index.setdefault(token[start : start + size], set()).add(entry.id)
    return index


def reset_catalog_cache() -> None:
    """테스트 전용 — 스냅샷 경로(settings)를 바꾼 뒤 재로드할 때 호출한다."""
    _catalog.cache_clear()
    _gram_index.cache_clear()


def all_entries() -> list[CategoryEntry]:
    """스냅샷 전체 — 진단·테스트용(정렬은 path 기준)."""
    return sorted(_catalog().values(), key=lambda e: e.path_str)


def get(category_id: str) -> CategoryEntry | None:
    """id 조회 — 스냅샷에 없으면 None(호출부가 되묻기로 전환)."""
    return _catalog().get(category_id)


def path_str(category_id: str) -> str | None:
    entry = get(category_id)
    return entry.path_str if entry else None


def _normalize(text: str) -> str:
    return _KEEP.sub("", text.strip().lower())


def _normalize_query(text: str) -> str:
    """발화 정규화 — 잡음 어휘를 먼저 걷어낸 뒤 한글·영숫자만 남긴다."""
    return _normalize(_QUERY_NOISE.sub(" ", text.strip().lower()))


def _query_grams(needle: str) -> list[str]:
    """질의의 모든 n-gram(긴 것 우선, 중복 제거) — 인덱스 조회 키."""
    grams: list[str] = []
    seen: set[str] = set()
    for size in range(min(len(needle), _MAX_GRAM), _MIN_GRAM - 1, -1):
        for start in range(len(needle) - size + 1):
            gram = needle[start : start + size]
            if gram not in seen:
                seen.add(gram)
                grams.append(gram)
    return grams


def _score(entry: CategoryEntry, needle: str) -> tuple[tuple[int, int, int], int] | None:
    """((계층, 일치유형, -일치길이), -커버리지) — 작을수록 좋은 후보. 접점 없으면 None.

    일치유형: 0 완전일치 · 1 매칭어가 발화에 포함 · 2 발화가 매칭어에 포함 ·
    3 최장 공통 n-gram. "티셔츠 팔아요" vs "남성의류 > 반팔티셔츠" 처럼 어느 쪽도
    상대를 통째로 품지 않는 실제 발화가 흔해서 4번째 유형이 반드시 필요하다.

    커버리지(계층별 일치 길이의 합)가 동점을 가른다 — "강아지 사료" 는 소분류만 보면
    `강아지용품 > 사료` 와 `고양이용품 > 사료` 가 완전 동점이지만, 중분류까지 세면
    앞의 것이 발화를 더 많이 설명한다.
    """
    best: tuple[int, int, int] | None = None
    coverage = 0
    for level, term in entry.terms():
        token = _normalize(term)
        if not token:
            continue
        if token == needle:
            candidate = (level, 0, -len(token))
        elif len(token) >= _MIN_GRAM and token in needle:
            candidate = (level, 1, -len(token))
        elif len(needle) >= _MIN_GRAM and needle in token:
            candidate = (level, 2, -len(needle))
        else:
            overlap = _longest_common_gram(token, needle)
            if overlap < _MIN_GRAM:
                continue
            candidate = (level, 3, -overlap)
        coverage += -candidate[2]
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None
    return best, -coverage


def _longest_common_gram(token: str, needle: str) -> int:
    """token 과 needle 의 최장 공통 부분문자열 길이 — 짧은 쪽 기준 상한 _MAX_GRAM.

    후보(수십 건)에만 도는 정밀 채점이라 단순 슬라이딩으로 충분하다.
    """
    limit = min(len(token), len(needle), _MAX_GRAM)
    for size in range(limit, _MIN_GRAM - 1, -1):
        for start in range(len(token) - size + 1):
            if token[start : start + size] in needle:
                return size
    return 0


def search(query: str, k: int | None = None) -> list[CategoryEntry]:
    """카테고리 후보 검색 — 소분류 > 중분류 > 대분류 순으로 발화와 겹치는 항목.

    2단계다: ① n-gram 인덱스로 접점 있는 항목만 추려(회수) ② 계층·일치유형으로
    정렬(정밀). 발화 전체를 하나의 needle 로 보던 구 구현은 "강아지 사료 팔아요"
    처럼 카테고리 어휘가 문장 안에 박힌 실제 입력을 거의 못 찾았다.

    임베딩을 쓰지 않는 이유: 1000건 인메모리 문자열 매칭이면 충분하고, 애매한
    경우는 LLM 택1(category_resolver)이 받는다. 부족해지면 같은 시그니처로
    pgvector 검색으로 교체한다.
    """
    if k is None:
        k = get_settings().seller_category_candidates_k
    if k <= 0:
        return []
    needle = _normalize_query(query)
    if len(needle) < _MIN_GRAM:
        return []

    index = _gram_index()
    candidate_ids: set[str] = set()
    for gram in _query_grams(needle):
        candidate_ids |= index.get(gram, set())
        if len(candidate_ids) > 400:  # 회수 상한 — 정밀 채점 비용을 묶어 둔다
            break

    catalog = _catalog()
    scored: list[tuple[tuple[tuple[int, int, int], int], str, CategoryEntry]] = []
    for entry_id in candidate_ids:
        entry = catalog[entry_id]
        score = _score(entry, needle)
        if score is not None:
            scored.append((score, entry.path_str, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _score_value, _path, entry in scored[:k]]


def candidates_block(entries: list[CategoryEntry]) -> str:
    """product 에이전트 입력에 주입할 [카테고리 후보] 블록 — "id | path" 1줄 1건."""
    return "\n".join(f"- {entry.id} | {entry.path_str}" for entry in entries)


def spring_category_id(category_id: str) -> int | None:
    """confirm 시 I-10 `categoryId`(Long) 에 쓸 값 — BE 와 맞출 유일한 지점.

    BE `SellerProductCreateRequest.categoryId` 는 **숫자 필수**다(이름·경로 문자열을
    받는 필드가 아니다). 구 구현은 leaf 명칭 문자열을 `category` 키로 보냈고, BE 는
    모르는 키를 버린 뒤 categoryId 누락으로 등록을 거부했다 — 이 함수가 그 구멍이다.

    스냅샷에 없는 id 는 None — validate_draft 가 선검증하므로 정상 경로에서는
    발생하지 않지만, 스냅샷 교체(배포) 사이의 draft 는 여기서 걸린다.
    """
    entry = get(category_id)
    if entry is None:
        return None
    return int(entry.id)
