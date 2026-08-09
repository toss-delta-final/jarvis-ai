"""카테고리 스냅샷 카탈로그 (#506) — 로드 fail-fast·검색 랭킹·path/쓰기 값 계약.

실 파일 대신 tmp_path 스냅샷을 쓰고, settings 는 모듈 지역 get_settings 를 대체한다
(catalog 는 lru_cache 1회 로드라 reset_catalog_cache 로 격리).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.seller import category_catalog

_ENTRIES = [
    {"id": "100", "path": ["패션의류/잡화", "남성의류", "셔츠"]},
    {"id": "101", "path": ["패션의류/잡화", "남성의류", "니트/스웨터"]},
    {"id": "102", "path": ["패션의류/잡화", "잡화", "셔츠클립"], "synonyms": ["타이바"]},
    {"id": "103", "path": ["신발", "운동화", "러닝화"], "synonyms": ["조깅화"]},
]


@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    """tmp 스냅샷을 로드 대상으로 주입하고 테스트 후 캐시를 되돌린다."""
    snapshot = tmp_path / "categories.json"
    snapshot.write_text(
        json.dumps({"version": "test.1", "categories": _ENTRIES}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        category_catalog,
        "get_settings",
        lambda: SimpleNamespace(
            seller_category_snapshot_path=str(snapshot),
            seller_category_candidates_k=5,
            seller_category_write_mode="leaf",
        ),
    )
    category_catalog.reset_catalog_cache()
    yield snapshot
    category_catalog.reset_catalog_cache()


def _use_snapshot(monkeypatch, tmp_path, payload, **settings_overrides):
    snapshot = tmp_path / "bad.json"
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        category_catalog,
        "get_settings",
        lambda: SimpleNamespace(
            seller_category_snapshot_path=str(snapshot),
            seller_category_candidates_k=5,
            seller_category_write_mode="leaf",
            **settings_overrides,
        ),
    )
    category_catalog.reset_catalog_cache()
    return snapshot


def test_get_and_path_str(catalog) -> None:
    entry = category_catalog.get("100")
    assert entry is not None and entry.leaf == "셔츠"
    assert category_catalog.path_str("100") == "패션의류/잡화 > 남성의류 > 셔츠"
    assert category_catalog.get("999") is None
    assert category_catalog.path_str("999") is None


def test_search_ranks_exact_leaf_first(catalog) -> None:
    """leaf 완전일치("셔츠") > 부분일치("셔츠클립") — 후보 순서가 곧 LLM 노출 순서다."""
    results = category_catalog.search("셔츠")
    assert [e.id for e in results][:2] == ["100", "102"]


def test_search_matches_leaf_inside_utterance(catalog) -> None:
    """수정 턴 발화("남방 말고 셔츠야")에서도 leaf 가 부분 문자열로 잡힌다."""
    results = category_catalog.search("남방 말고 셔츠야")
    assert any(e.id == "100" for e in results)


def test_search_synonyms(catalog) -> None:
    assert any(e.id == "103" for e in category_catalog.search("조깅화"))


def test_search_empty_query(catalog) -> None:
    assert category_catalog.search("   ") == []


def test_candidates_block_format(catalog) -> None:
    block = category_catalog.candidates_block(category_catalog.search("셔츠", 1))
    assert block == "- 100 | 패션의류/잡화 > 남성의류 > 셔츠"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("leaf", "셔츠"), ("path", "패션의류/잡화 > 남성의류 > 셔츠"), ("id", "100")],
)
def test_spring_write_value_modes(catalog, monkeypatch, mode, expected) -> None:
    """I-10 쓰기 값은 write_mode 설정 한 곳이 결정한다 — BE 정렬 지점."""
    base = category_catalog.get_settings()
    monkeypatch.setattr(
        category_catalog,
        "get_settings",
        lambda: SimpleNamespace(
            seller_category_snapshot_path=base.seller_category_snapshot_path,
            seller_category_candidates_k=5,
            seller_category_write_mode=mode,
        ),
    )
    assert category_catalog.spring_write_value("100") == expected
    assert category_catalog.spring_write_value("999") is None


def test_load_bare_list_allowed(monkeypatch, tmp_path) -> None:
    """최상위가 meta 없는 순수 배열이어도 로드된다(초기 수기 관리 관용)."""
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
        category_catalog,
        "get_settings",
        lambda: SimpleNamespace(
            seller_category_snapshot_path=str(tmp_path / "absent.json"),
            seller_category_candidates_k=5,
            seller_category_write_mode="leaf",
        ),
    )
    category_catalog.reset_catalog_cache()
    with pytest.raises(category_catalog.CategorySnapshotError):
        category_catalog.get("1")
    category_catalog.reset_catalog_cache()


def test_repo_snapshot_file_loads() -> None:
    """저장소에 실린 기본 스냅샷(app/data/seller_categories.json)이 실제로 로드 가능해야 한다."""
    category_catalog.reset_catalog_cache()
    entries = category_catalog._load_file(  # noqa: SLF001 — 파일 단독 검증(설정 격리 목적)
        __import__("pathlib").Path("app/data/seller_categories.json")
    )
    assert len(entries) >= 10
