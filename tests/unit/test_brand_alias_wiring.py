"""I-1 브랜드 법인 표기 확장 배선 (#466) — 와이어에서만 넓히고 표시·축은 안 건드린다."""

from __future__ import annotations

from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters
from app.services import spring_client as sc


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"success": True, "data": []}


class _Client:
    def __init__(self, seen):
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path, params=None):
        self.seen.append(params)
        return _Response()


def test_search_query_params_without_brand_values_is_unchanged() -> None:
    """확장값을 안 주면 종전 그대로 — off 경로가 바이트 동일해야 롤백이 성립한다."""
    filters = ProductSearchFilters(brand=["삼성"])
    assert sc._search_query_params(filters) == {"brandName": ["삼성"]}


def test_search_query_params_uses_expanded_values_when_given() -> None:
    filters = ProductSearchFilters(brand=["삼성"])
    params = sc._search_query_params(filters, brand_values=["삼성", "삼성전자"])
    assert params["brandName"] == ["삼성", "삼성전자"]


def test_expanded_values_do_not_resurrect_blank_only_brands() -> None:
    """`filters.brand` 가 공백뿐이면 확장이 켜져도 `brandName` 을 싣지 않는다."""
    filters = ProductSearchFilters(brand=["  "])
    assert "brandName" not in sc._search_query_params(filters, brand_values=[])


def test_filter_axes_unchanged_by_expansion() -> None:
    """축 집합은 확장과 무관하다 — `brandName` 키 유무는 `filters.brand` 만 정한다.

    `search_guard.is_popular_fallback_safe`·`is_unfiltered_payload` 가 이 축 집합을 읽으므로,
    여기서 흔들리면 확장이 폴백 판정을 조용히 바꾼다.
    """
    filters = ProductSearchFilters(brand=["삼성"])
    assert sc.search_filter_axes(filters) == {"brandName"}


async def test_search_products_expands_brand_on_the_wire(monkeypatch) -> None:
    """기본 on — 실제 GET 파라미터에 법인 표기가 실린다."""
    settings = get_settings().model_copy(
        update={"brand_alias_expansion_enabled": True, "brand_alias_max_values": 12}
    )
    seen: list = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))

    await sc.search_products(ProductSearchFilters(brand=["삼성"]))

    assert seen and seen[0]["brandName"][0] == "삼성"
    assert "삼성전자" in seen[0]["brandName"]


async def test_search_products_flag_off_sends_verbatim_only(monkeypatch) -> None:
    """off 면 와이어가 종전과 동일 — 한 번에 전체 롤백이 가능해야 한다."""
    settings = get_settings().model_copy(update={"brand_alias_expansion_enabled": False})
    seen: list = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))

    await sc.search_products(ProductSearchFilters(brand=["삼성"]))

    assert seen and seen[0]["brandName"] == ["삼성"]


async def test_search_products_does_not_mutate_filters(monkeypatch) -> None:
    """`filters.brand` 는 그대로다 — 조건칩(state.condition_chips)이 사용자 표기를 보여준다."""
    settings = get_settings().model_copy(update={"brand_alias_expansion_enabled": True})
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client([]))

    filters = ProductSearchFilters(brand=["삼성"])
    await sc.search_products(filters)

    assert filters.brand == ["삼성"]
