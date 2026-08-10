"""카테고리 스냅샷 카탈로그 (#506) — 로드 fail-fast·검색 랭킹·path/쓰기 값 계약.

실 파일 대신 tmp_path 스냅샷을 쓰고, settings 는 모듈 지역 get_settings 를 대체한다
(catalog 는 lru_cache 1회 로드라 reset_catalog_cache 로 격리).

스냅샷 path 는 **2칸 고정**이다: ["<대분류>", "<중분류> > <소분류>"] — 정본 DB 의
`category` 가 2단이고 소분류 이름이 이미 병합형이기 때문(2026-08-09 실데이터 정합).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.seller import category_catalog

_ENTRIES = [
    {"id": "100", "path": ["패션의류/잡화", "남성의류 > 셔츠/남방"]},
    {"id": "101", "path": ["패션의류/잡화", "남성의류 > 니트/스웨터"]},
    {"id": "102", "path": ["패션의류/잡화", "패션잡화 > 셔츠클립"], "synonyms": ["타이바"]},
    {"id": "103", "path": ["패션의류/잡화", "남성신발 > 운동화"], "synonyms": ["조깅화"]},
    {"id": "104", "path": ["반려동물", "강아지용품 > 사료"]},
    {"id": "105", "path": ["반려동물", "고양이용품 > 사료"]},
]


def _settings(snapshot, **overrides) -> SimpleNamespace:
    values = {
        "seller_category_snapshot_path": str(snapshot),
        "seller_category_candidates_k": 5,
        "seller_category_fallback_k_factor": 3,
        "seller_category_resolve_timeout_s": 12.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    """tmp 스냅샷을 로드 대상으로 주입하고 테스트 후 캐시를 되돌린다."""
    snapshot = tmp_path / "categories.json"
    snapshot.write_text(
        json.dumps({"version": "test.1", "categories": _ENTRIES}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(category_catalog, "get_settings", lambda: _settings(snapshot))
    category_catalog.reset_catalog_cache()
    yield snapshot
    category_catalog.reset_catalog_cache()


def _use_snapshot(monkeypatch, tmp_path, payload):
    snapshot = tmp_path / "bad.json"
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(category_catalog, "get_settings", lambda: _settings(snapshot))
    category_catalog.reset_catalog_cache()
    return snapshot


def test_get_and_path_str(catalog) -> None:
    entry = category_catalog.get("100")
    assert entry is not None
    assert entry.leaf == "남성의류 > 셔츠/남방"
    assert category_catalog.path_str("100") == "패션의류/잡화 > 남성의류 > 셔츠/남방"
    assert category_catalog.get("999") is None
    assert category_catalog.path_str("999") is None


def test_entry_splits_merged_leaf(catalog) -> None:
    """병합명은 대/중/소 세 조각으로 분해돼야 매칭이 계층별로 동작한다."""
    entry = category_catalog.get("100")
    assert entry is not None
    assert (entry.major, entry.middle, entry.minor) == ("패션의류/잡화", "남성의류", "셔츠/남방")


def test_sub_path_keeps_path_reconstructable(catalog) -> None:
    """[#541] 판매자가 채우는 둘째 칸 — major 와 이어 붙이면 path_str 이 그대로 나온다."""
    entry = category_catalog.get("100")
    assert entry is not None
    assert entry.sub_path == "남성의류 > 셔츠/남방"
    assert f"{entry.major} > {entry.sub_path}" == entry.path_str


def test_sub_path_survives_three_slot_snapshot(monkeypatch, tmp_path) -> None:
    """3칸 스냅샷(낡은 파일·수기 픽스처)에서도 중분류가 표기에서 사라지지 않는다.

    `leaf`(= path[-1])로 둘째 칸을 만들면 "남성의류" 가 조용히 증발해 카드가 보여준
    카테고리와 등록될 카테고리가 달라진다 — 카테고리는 등록 후 변경 불가라 비용이 크다.
    """
    _use_snapshot(
        monkeypatch, tmp_path, [{"id": "1", "path": ["패션의류/잡화", "남성의류", "셔츠"]}]
    )
    entry = category_catalog.get("1")
    assert entry is not None
    assert entry.leaf == "셔츠"  # leaf 는 마지막 칸 그대로
    assert entry.sub_path == "남성의류 > 셔츠"  # 둘째 칸은 나머지 전부
    assert f"{entry.major} > {entry.sub_path}" == entry.path_str
    category_catalog.reset_catalog_cache()


def test_entry_without_merge_separator(monkeypatch, tmp_path) -> None:
    """병합되지 않은 이름(구 스냅샷·수기 픽스처)도 minor 로 다뤄 깨지지 않는다."""
    _use_snapshot(monkeypatch, tmp_path, [{"id": "1", "path": ["식품", "커피"]}])
    entry = category_catalog.get("1")
    assert entry is not None
    assert entry.middle is None
    assert entry.minor == "커피"
    category_catalog.reset_catalog_cache()


def test_search_ranks_exact_minor_first(catalog) -> None:
    """소분류 완전일치 > 부분일치 — 후보 순서가 곧 LLM 노출 순서다."""
    results = category_catalog.search("셔츠클립")
    assert results[0].id == "102"


def test_search_matches_term_inside_utterance(catalog) -> None:
    """실제 발화("이 셔츠 3만원에 50개 등록해줘")에서 카테고리 어휘를 뽑아낸다.

    구 구현은 발화 전체를 하나의 needle 로 봐서 이런 입력을 거의 못 찾았다 —
    카테고리가 비면 등록이 BE 에서 거부되므로 회수(recall)가 곧 기능이다.
    """
    results = category_catalog.search("이 셔츠 3만원에 50개 등록해줘")
    assert any(e.id == "100" for e in results)


def test_search_partial_overlap(catalog) -> None:
    """어느 쪽도 상대를 통째로 품지 않는 부분 겹침("남방" ⊂ "셔츠/남방")도 잡는다."""
    assert any(e.id == "100" for e in category_catalog.search("남방 팔아요"))


def test_search_coverage_breaks_ties(catalog) -> None:
    """소분류가 동점이면 중분류까지 발화를 설명하는 쪽이 앞선다."""
    results = category_catalog.search("강아지 사료 등록하고 싶어요")
    assert results[0].id == "104"
    assert any(e.id == "105" for e in results)  # 고양이용품 > 사료도 후보로는 남는다


def test_search_synonyms(catalog) -> None:
    assert any(e.id == "103" for e in category_catalog.search("조깅화"))


def test_search_empty_query(catalog) -> None:
    assert category_catalog.search("   ") == []
    assert category_catalog.search("등록해줘") == []  # 잡음 어휘만 남으면 후보 없음


def test_search_respects_k(catalog) -> None:
    assert len(category_catalog.search("사료", 1)) == 1
    assert category_catalog.search("사료", 0) == []


def test_candidates_block_format(catalog) -> None:
    block = category_catalog.candidates_block(category_catalog.search("셔츠클립", 1))
    assert block == "- 102 | 패션의류/잡화 > 패션잡화 > 셔츠클립"


def test_spring_category_id_returns_int(catalog) -> None:
    """I-10 `categoryId` 는 Long — 여기서만 캐스팅한다(BE 정렬 지점)."""
    assert category_catalog.spring_category_id("100") == 100
    assert category_catalog.spring_category_id("999") is None


def test_load_bare_list_allowed(monkeypatch, tmp_path) -> None:
    """최상위가 meta 없는 순수 배열이어도 로드된다(테스트 픽스처 관용)."""
    _use_snapshot(monkeypatch, tmp_path, _ENTRIES)
    assert category_catalog.get("100") is not None
    category_catalog.reset_catalog_cache()


@pytest.mark.parametrize(
    "payload",
    [
        {"version": "x", "categories": []},  # 빈 스냅샷
        {"categories": [{"id": "1", "path": []}]},  # 빈 path
        {"categories": [{"id": "", "path": ["a"]}]},  # 빈 id
        {"categories": [{"id": "1", "path": ["a"]}, {"id": "1", "path": ["b"]}]},  # 중복 id
        {"categories": [{"id": 1, "path": ["a"]}]},  # 비문자열 id
        {"categories": [{"id": "셔츠", "path": ["a"]}]},  # 숫자 아닌 id — categoryId 캐스팅 불가
    ],
)
def test_load_fail_fast(monkeypatch, tmp_path, payload) -> None:
    """결함 스냅샷은 부분 로드 없이 CategorySnapshotError — 기동 실패가 정답이다."""
    _use_snapshot(monkeypatch, tmp_path, payload)
    with pytest.raises(category_catalog.CategorySnapshotError):
        category_catalog.get("1")
    category_catalog.reset_catalog_cache()


def test_load_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        category_catalog, "get_settings", lambda: _settings(tmp_path / "absent.json")
    )
    category_catalog.reset_catalog_cache()
    with pytest.raises(category_catalog.CategorySnapshotError):
        category_catalog.get("1")
    category_catalog.reset_catalog_cache()


def test_repo_snapshot_file_loads() -> None:
    """저장소에 실린 기본 스냅샷(app/data/seller_categories.json)의 실데이터 계약.

    id 는 실 DB `category.id` 라 반드시 숫자여야 하고(→ categoryId Long), path 는 2칸
    ["대분류", "중분류 > 소분류"] 이어야 한다. 어긋나면 상품 등록이 통째로 죽는다 —
    스냅샷은 scripts/build_seller_category_snapshot.py 로만 갱신한다.
    """
    category_catalog.reset_catalog_cache()
    entries = category_catalog._load_file(  # noqa: SLF001 — 파일 단독 검증(설정 격리 목적)
        Path("app/data/seller_categories.json")
    )
    assert len(entries) >= 100  # 실데이터 스냅샷(1,000건대) — 자리표시자 15건으로 되돌아가면 실패
    for entry in entries:
        assert entry.id.isdigit(), entry
        assert len(entry.path) == 2, entry
        assert category_catalog.SEPARATOR in entry.leaf or entry.middle is None
