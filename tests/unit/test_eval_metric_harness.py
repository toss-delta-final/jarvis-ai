"""기본 eval adapter가 실제 앱 HTTP 경계를 통과하는지 검증한다."""

import httpx
import pytest

from app.core.config import get_settings
from app.schemas.spring import LIST_MAX_PRODUCTS
from evals.goldenset.loader import load_cases
from evals.metrics.harness import (
    OfflineBuyerAdapter,
    _filter_products_by_requested_color,
    _filter_products_by_requested_price,
)
from evals.metrics.runner import evaluate, load_evaluation_fixtures


def test_offline_adapter_runs_real_search_and_push_boundaries() -> None:
    case = load_cases("dev")[0]
    fixtures = load_evaluation_fixtures()
    adapter = OfflineBuyerAdapter()

    output = adapter(case, fixtures)

    # v2 후보 depth(30)는 실제 push 경계(LIST_MAX_PRODUCTS=9, I-21)보다 깊다 — 스크립트
    # rerank가 검색 순서를 보존하므로 push된 목록은 fixture 순서의 접두(prefix)여야 한다.
    fixture = fixtures.search_responses[case.search_fixture_id]
    assert len(output["rankedProductIds"]) <= LIST_MAX_PRODUCTS
    assert output["rankedProductIds"] == fixture["productIds"][: len(output["rankedProductIds"])]
    assert output["extractedFilters"] == case.expected_filters
    assert [request["path"] for request in adapter.last_requests] == [
        "/internal/products/search",
        "/internal/recommendations",
    ]
    assert all(
        request["headers"]["x-internal-token"] == "eval-internal-token"
        for request in adapter.last_requests
    )


def test_offline_adapter_names_case_when_search_fixture_is_missing() -> None:
    case = load_cases("dev")[0].model_copy(
        update={"case_id": "buy-fail-9999", "search_fixture_id": None}
    )

    with pytest.raises(ValueError, match="buy-fail-9999"):
        OfflineBuyerAdapter()(case, load_evaluation_fixtures())


def test_offline_adapter_ignores_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(case for case in load_cases("dev") if case.case_id == "buy-srch-0002")
    fixtures = load_evaluation_fixtures()

    monkeypatch.setenv("EXPOSE_MAX", "5")
    monkeypatch.setenv("CATEGORY_FANOUT_MAX", "0")
    monkeypatch.setenv("EVAL_BUYER_K_LIST", "[1]")
    get_settings.cache_clear()
    constrained = OfflineBuyerAdapter()
    constrained_output = constrained(case, fixtures)
    constrained_report = evaluate(
        cases=[case],
        fixtures=fixtures,
        adapter=constrained,
    )

    monkeypatch.setenv("EXPOSE_MAX", "9")
    monkeypatch.setenv("CATEGORY_FANOUT_MAX", "5")
    monkeypatch.setenv("EVAL_BUYER_K_LIST", "[5]")
    get_settings.cache_clear()
    permissive = OfflineBuyerAdapter()
    permissive_output = permissive(case, fixtures)
    permissive_report = evaluate(
        cases=[case],
        fixtures=fixtures,
        adapter=permissive,
    )
    get_settings.cache_clear()

    assert constrained.settings.expose_max == permissive.settings.expose_max == 9
    assert constrained.settings.category_fanout_max == permissive.settings.category_fanout_max == 5
    assert constrained_report["kList"] == permissive_report["kList"] == [5, 10, 20]
    assert constrained_output == permissive_output
    assert constrained_report == permissive_report


# ---------------------------------------------------------------------------
# #370 결정 01 — _CaseTransport 가격 mock 필터
# ---------------------------------------------------------------------------


def _product(product_id: int, price: int | None) -> dict:
    return {"productId": product_id, "price": price}


def test_price_filter_passes_through_when_no_price_params() -> None:
    products = [_product(1, 5_000), _product(2, None)]
    assert _filter_products_by_requested_price(products, httpx.QueryParams()) == products


def test_price_filter_applies_max_price_inclusive_boundary() -> None:
    products = [_product(1, 10_000), _product(2, 10_001), _product(3, 9_999)]
    result = _filter_products_by_requested_price(products, httpx.QueryParams({"maxPrice": "10000"}))
    assert [p["productId"] for p in result] == [1, 3]


def test_price_filter_applies_min_price_inclusive_boundary() -> None:
    products = [_product(1, 1_000), _product(2, 999), _product(3, 1_001)]
    result = _filter_products_by_requested_price(products, httpx.QueryParams({"minPrice": "1000"}))
    assert [p["productId"] for p in result] == [1, 3]


def test_price_filter_never_drops_unknown_price_products() -> None:
    products = [_product(1, None), _product(2, 999_999)]
    result = _filter_products_by_requested_price(
        products, httpx.QueryParams({"minPrice": "0", "maxPrice": "10000"})
    )
    assert [p["productId"] for p in result] == [1]  # price=None은 판정 불가로 통과, 999999는 컷


def test_price_filter_never_crashes_on_non_numeric_price() -> None:
    # #370 리뷰 라운드2 F-5 — catalog_snapshot.json은 현재 int/None만 쓰지만, 이 mock은
    # 검증 없는 raw dict를 읽으므로 방어한다(TypeError로 죽지 않고 None과 같이 통과).
    products = [{"productId": 1, "price": "1000원"}]
    result = _filter_products_by_requested_price(
        products, httpx.QueryParams({"minPrice": "0", "maxPrice": "500"})
    )
    assert [p["productId"] for p in result] == [1]


# #474 — I-1 색상 배열 계약과 동일한 mock 필터
def test_color_filter_keeps_missing_none_and_blank_color_axes_as_passthrough() -> None:
    products = [
        {"productId": 1},
        {"productId": 2, "attributes": {"색상": None}},
        {"productId": 3, "attributes": {"색상": "  "}},
        {"productId": 4, "attributes": {"색상": "레드"}},
    ]

    result = _filter_products_by_requested_color(products, httpx.QueryParams([("color", "네이비")]))

    assert [product["productId"] for product in result] == [1, 2, 3]


def test_color_filter_uses_all_repeated_params_with_normalized_partial_or_match() -> None:
    products = [
        {"productId": 1, "attributes": {"색상": "다크그레이"}},
        {"productId": 2, "attributes": {"색상": "네이비"}},
        {"productId": 3, "attributes": {"색상": "레드"}},
    ]

    result = _filter_products_by_requested_color(
        products,
        httpx.QueryParams([("color", "  그레이 "), ("color", " 네이비 ")]),
    )

    assert [product["productId"] for product in result] == [1, 2]


def test_case_transport_applies_price_filter_to_search_response() -> None:
    from evals.metrics.harness import _CaseTransport

    case = next(case for case in load_cases("dev") if case.case_id == "buy-budg-0002")
    fixtures = load_evaluation_fixtures()
    transport = _CaseTransport(case, fixtures, internal_token="tok")

    request = httpx.Request(
        "GET",
        "http://spring/internal/products/search",
        params={"maxPrice": "30000"},
        headers={"X-Internal-Token": "tok"},
    )
    response = transport.handler(request)
    body = response.json()["data"]

    price_max = case.hard_constraints.price_max
    assert price_max is not None
    assert all((p["price"] is None or p["price"] <= price_max) for p in body)
    # 이번 이슈에서 주입한 price_violation 후보는 mock을 통과하면 안 된다.
    fixture = fixtures.search_responses[case.search_fixture_id]
    injected_violation_ids = {
        c["productId"] for c in fixture["candidates"] if c["rule"] == "price_violation"
    }
    assert injected_violation_ids  # 이 케이스는 실제로 주입 대상이었다(전제 확인)
    returned_ids = {p["productId"] for p in body}
    assert not (injected_violation_ids & returned_ids)
