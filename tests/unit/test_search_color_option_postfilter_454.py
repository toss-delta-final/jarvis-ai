"""검색 사후필터 — 색상 지정 턴에서 옵션에 그 색이 없는 후보를 제외한다 (이슈 #454 Phase 2).

판정식 A~D 는 `docs/specs/MEASURE-OPTION-COLOR-454.md` §"판정식", `evals/option_color` 하네스와
같다. D 는 `app.agents.buyer.cart.options.narrow_options` 를 그대로 호출하므로(재구현 아님)
여기서는 A~C 게이팅과 0건 가드·설정 배선만 고정한다.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters, SpringProduct
from app.services import search_service
from app.services.search_service import _filter_unbuyable_color_options, _is_color_unbuyable

_SYNONYMS = {"검정": ["블랙", "검정", "흑색"], "블랙": ["블랙", "검정", "흑색"]}


def _product(
    pid: int,
    *,
    colors: list[str] | None = None,
    options: list[str] | None = None,
    option_count: int | None = None,
) -> SpringProduct:
    attributes = {"색상": colors} if colors is not None else None
    return SpringProduct(
        product_id=pid,
        name=f"상품{pid}",
        attributes=attributes,
        options=options,
        option_count=option_count
        if option_count is not None
        else (len(options) if options else None),
    )


def _mock_synonym_map(mapping):
    async def _load(settings):
        return mapping

    return _load


# ─────────── _is_color_unbuyable — 판정식 A~D 단독 게이팅 ───────────


def test_is_color_unbuyable_true_when_multi_color_not_truncated_no_match() -> None:
    """B(복수)∧C(절단 아님)∧D(매칭 없음) 전부 참이면 unbuyable."""
    p = _product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])
    assert _is_color_unbuyable(p, "검정", _SYNONYMS, get_settings()) is True


def test_is_color_unbuyable_false_when_single_color() -> None:
    """(B 단독 테스트) 단일색이면 옵션에 그 색이 없어도 정상 — 제외하지 않는다."""
    p = _product(1, colors=["블랙"], options=["S", "M", "L"])
    assert _is_color_unbuyable(p, "검정", _SYNONYMS, get_settings()) is False


def test_is_color_unbuyable_false_when_no_color_axis() -> None:
    """attributes.색상 자체가 없으면(B 판정 불가) 제외하지 않는다."""
    p = _product(1, colors=None, options=["S", "M", "L"])
    assert _is_color_unbuyable(p, "검정", _SYNONYMS, get_settings()) is False


def test_is_color_unbuyable_false_when_truncated() -> None:
    """(C 단독 테스트) optionCount != len(options)(절단)면 안 보이는 옵션에 그 색이 있을 수
    있어 제외하지 않는다 — B·D 는 참이어도."""
    p = _product(1, colors=["블랙", "화이트"], options=["S", "M", "L"], option_count=25)
    assert _is_color_unbuyable(p, "검정", _SYNONYMS, get_settings()) is False


def test_is_color_unbuyable_false_when_option_names_contain_color() -> None:
    """(D 단독 테스트) 옵션 이름에 등가 표기가 있으면(D 거짓) 제외하지 않는다 — 승인 동의어
    "검정"→"블랙" 등가가 실제로 적용됨을 함께 확인한다."""
    p = _product(1, colors=["블랙", "화이트"], options=["블랙 / M", "화이트 / M"])
    assert _is_color_unbuyable(p, "검정", _SYNONYMS, get_settings()) is False


def test_is_color_unbuyable_false_when_no_options() -> None:
    """옵션 자체가 없는 단일 SKU 상품은 판정 대상이 아니다."""
    p = _product(1, colors=["블랙", "화이트"], options=None)
    assert _is_color_unbuyable(p, "검정", _SYNONYMS, get_settings()) is False


# ─────────── _filter_unbuyable_color_options — 배선·0건 가드·degrade ───────────


async def test_filter_removes_unbuyable_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.search_service.spring_client._load_color_synonym_map",
        _mock_synonym_map(_SYNONYMS),
    )
    unbuyable = _product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])
    ok = _product(2, colors=["블랙"], options=["S", "M", "L"])

    result = await _filter_unbuyable_color_options(
        [unbuyable, ok], ProductSearchFilters(color="검정")
    )

    assert [p.product_id for p in result] == [2]


async def test_filter_no_color_condition_leaves_products_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """색상 조건 없는 턴은 사전 조회도 안 하고 후보가 그대로다(바이트 동일)."""
    monkeypatch.setattr(
        "app.services.search_service.spring_client._load_color_synonym_map",
        lambda settings: pytest.fail("색상 조건 없는데 사전을 조회했다"),
    )
    products = [_product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])]

    result = await _filter_unbuyable_color_options(products, ProductSearchFilters())

    assert result == products
    assert result is products


async def test_filter_zero_result_guard_cancels_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """0건 가드 — 제외 후 후보가 0건이면 제외를 통째로 취소하고 원본을 돌려준다."""
    monkeypatch.setattr(
        "app.services.search_service.spring_client._load_color_synonym_map",
        _mock_synonym_map(_SYNONYMS),
    )
    products = [_product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])]

    result = await _filter_unbuyable_color_options(products, ProductSearchFilters(color="검정"))

    assert result == products  # 취소돼 원본 그대로


async def test_filter_synonym_load_failure_degrades_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(settings):
        raise RuntimeError("catalog offline")

    monkeypatch.setattr("app.services.search_service.spring_client._load_color_synonym_map", _raise)
    products = [_product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])]

    result = await _filter_unbuyable_color_options(products, ProductSearchFilters(color="검정"))

    assert result == products


async def test_filter_synonym_none_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """사전이 없으면(적재 결과 None) 판정을 건너뛴다."""
    monkeypatch.setattr(
        "app.services.search_service.spring_client._load_color_synonym_map",
        _mock_synonym_map(None),
    )
    products = [_product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])]

    result = await _filter_unbuyable_color_options(products, ProductSearchFilters(color="검정"))

    assert result == products


async def test_filter_config_off_never_loads_dictionary(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정 off 면 사전을 아예 조회하지 않고 오늘 동작(무필터) 그대로다."""
    monkeypatch.setattr(get_settings(), "search_color_option_postfilter_enabled", False)
    monkeypatch.setattr(
        "app.services.search_service.spring_client._load_color_synonym_map",
        lambda settings: pytest.fail("설정 off 인데 사전을 조회했다"),
    )
    unbuyable = _product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])

    result = await _filter_unbuyable_color_options([unbuyable], ProductSearchFilters(color="검정"))

    assert result == [unbuyable]


# ─────────── search_catalog 배선 — 실제 검색 경로에 물려 있는지 ───────────


async def test_search_catalog_applies_color_postfilter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.search_service.spring_client._load_color_synonym_map",
        _mock_synonym_map(_SYNONYMS),
    )
    unbuyable = _product(1, colors=["블랙", "화이트"], options=["S", "M", "L"])
    ok = _product(2, colors=["블랙"], options=["S", "M", "L"])

    class _StubBackend:
        async def search(self, filters):
            from app.schemas.spring import ProductSearchResult

            return ProductSearchResult(products=[unbuyable, ok], total_count=2)

    result = await search_service.search_catalog(
        ProductSearchFilters(color="검정"), backend=_StubBackend()
    )

    assert [p.product_id for p in result.products] == [2]
    assert result.total_count == 1
