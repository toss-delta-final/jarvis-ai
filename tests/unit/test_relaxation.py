"""0건/소량 조건 완화 (이슈 #113) — 완화 후보 생성 · 완화 칩 emit · 자동 완화 안내.

계약: api-spec §3.1 `suggestions`.`relaxation`={field,value}·estCount, `done.data.relaxationNotice`.
SPEC-RECOMMEND-001 §6.6(REQ-REC-040~046), AC-REC-08(가격 제약 불가침).

estCount 는 page-local 로 못 구한다 — priceMax·brand·color 는 Spring I-1 쿼리 파라미터라 탈락
상품이 응답에 아예 없다. 그래서 완화 필터로 **재검색(probe)** 해 실제 매칭 수를 센다. 아래 fake
search 는 그 전제를 재현하려고 **필터를 실제로 적용**한다(기존 _make_search 는 전량 반환).
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.recommendation.relaxation import build_relaxation_candidates
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services.spring_client import SpringUnavailableError
from tests._fakes import DEFAULT_DECOMPOSE, FakeLLM
from tests.unit.test_recommendation import _collect, _req, _types, run_buyer_turn


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


def _product(product_id: int, price: int, rating: float = 4.5, brand: str = "BrandX"):
    return SpringProduct(
        product_id=product_id,
        name=f"이어폰{product_id}",
        price=price,
        rating=rating,
        review_count=10,
        category="무선이어폰",
        brand=brand,
    )


def _filtered_search(products, *, calls=None, fail_when=None):
    """필터를 실제로 적용하는 fake search — 완화 전/후 결과가 달라져야 probe 를 검증할 수 있다.

    fail_when: filters -> bool. True 인 호출만 SpringUnavailableError 를 던진다(probe 실패 격리 검증).
    """

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        if calls is not None:
            calls.append(filters)
        if fail_when is not None and fail_when(filters):
            raise SpringUnavailableError("spring down")
        kept = []
        for p in products:
            if filters.price_max is not None and (p.price or 0) > filters.price_max:
                continue
            if filters.rating_min is not None and (p.rating or 0) < filters.rating_min:
                continue
            if filters.brand and p.brand not in filters.brand:
                continue
            kept.append(p)
        return ProductSearchResult(products=kept, total_count=len(kept))

    return _search


def _decompose_with(**filters):
    """DEFAULT_DECOMPOSE 의 filters 만 교체한 decompose 응답."""
    payload = json.loads(json.dumps(DEFAULT_DECOMPOSE))
    payload["filters"] = filters
    return payload


def _suggestions(events) -> list[dict]:
    return [c for e in events if e["type"] == "suggestions" for c in e["data"]["chips"]]


def _done(events) -> dict:
    return next(e["data"] for e in events if e["type"] == "done")


# ─────────── 단위 — 완화 후보 생성 ───────────


def test_price_max_candidate_uses_config_ratio_and_round_unit() -> None:
    """priceMax 5만 → config 비율(0.3) 상향 후 올림 단위로 반올림 → 65,000."""
    settings = get_settings()
    candidates = build_relaxation_candidates(ProductSearchFilters(price_max=50000), settings)

    assert [c.field for c in candidates] == ["priceMax"]
    assert candidates[0].value == 65000
    assert candidates[0].filters.price_max == 65000
    assert "65,000" in candidates[0].label


def test_category_is_never_a_relaxation_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """[AC④] 카테고리는 완화 대상이 아니다 — #84 소관이라 여기서 풀면 중복이다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "relaxation_chip_fields", ["category", "priceMax"])
    candidates = build_relaxation_candidates(
        ProductSearchFilters(category="무선이어폰", price_max=50000), settings
    )

    assert [c.field for c in candidates] == ["priceMax"]


def test_rating_min_relaxes_by_step_and_drops_condition_at_floor() -> None:
    settings = get_settings()

    lowered = build_relaxation_candidates(ProductSearchFilters(rating_min=4.5), settings)
    assert lowered[0].field == "ratingMin"
    assert lowered[0].value == 4.0
    assert lowered[0].filters.rating_min == 4.0

    # 하한 아래로 내려가면 값 완화가 아니라 조건 해제(None)다.
    dropped = build_relaxation_candidates(ProductSearchFilters(rating_min=0.5), settings)
    assert dropped[0].value is None
    assert dropped[0].filters.rating_min is None


def test_unset_and_unlisted_fields_are_not_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "relaxation_chip_fields", ["priceMax"])
    # brand 는 설정돼 있지만 config 목록 밖 → 후보 아님. ratingMin 은 아예 미설정 → 후보 아님.
    candidates = build_relaxation_candidates(
        ProductSearchFilters(price_max=50000, brand=["BrandX"]), settings
    )

    assert [c.field for c in candidates] == ["priceMax"]


def test_candidate_order_follows_config_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "relaxation_chip_fields", ["brand", "priceMax"])
    candidates = build_relaxation_candidates(
        ProductSearchFilters(price_max=50000, brand=["BrandX"]), settings
    )

    assert [c.field for c in candidates] == ["brand", "priceMax"]


# ─────────── 스트림 — 0건 완화 칩 ───────────


async def test_zero_result_emits_price_relaxation_chip_with_probed_est_count() -> None:
    """[AC①] 0건이면 완화 칩을 제안하고, estCount 는 완화 재검색으로 센 실제 매칭 수다."""
    # 전부 5만원 초과 → 원 조건(priceMax=50000)으로는 0건, 65,000 완화면 3건.
    products = [_product(201, 55000), _product(202, 60000), _product(203, 62000)]
    calls: list[ProductSearchFilters] = []
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_filtered_search(products, calls=calls),
            push_fn=None,
        )
    )

    chips = _suggestions(events)
    assert [c["relaxation"]["field"] for c in chips] == ["priceMax"]
    assert chips[0]["relaxation"]["value"] == 65000
    assert chips[0]["estCount"] == 3
    assert _done(events)["finishReason"] == "zero_result"
    assert "error" not in _types(events)
    # 원 검색 + 완화 probe — probe 없이는 estCount 를 구할 수 없다.
    assert [f.price_max for f in calls] == [50000, 65000]


async def test_chip_with_zero_est_count_is_excluded() -> None:
    """[§3.1] 완화해도 0건인 칩은 목록에서 제외 — 누르면 빈 화면인 제안을 주지 않는다."""
    products = [_product(201, 90000)]  # 65,000 로 완화해도 여전히 0건
    events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_filtered_search(products), push_fn=None
        )
    )

    assert _suggestions(events) == []
    assert "suggestions" not in _types(events)
    assert _done(events)["finishReason"] == "zero_result"


async def test_probe_failure_drops_only_that_chip() -> None:
    """probe 재검색이 실패해도 그 칩만 빠지고 스트림은 정상 종료한다(degrade 규칙)."""
    products = [_product(201, 55000)]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_filtered_search(
                products, fail_when=lambda f: f.price_max != 50000
            ),  # 완화 probe 만 실패
            push_fn=None,
        )
    )

    assert _suggestions(events) == []
    assert "error" not in _types(events)
    assert _done(events)["finishReason"] == "zero_result"


# ─────────── 스트림 — 자동 완화 + 투명 안내 ───────────


async def test_auto_relaxation_emits_notice_and_recovers_products() -> None:
    """[AC②] 약한 조건(평점)은 자동 완화하고 token 안내 + done.relaxationNotice 로 알린다."""
    push_calls: list = []

    async def _push(push) -> bool:  # noqa: ANN001
        push_calls.append(push)
        return True

    # 평점 4.5 하한으로는 0건, 4.0 으로 완화하면 2건.
    products = [_product(101, 39000, rating=4.2), _product(102, 48000, rating=4.1)]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
            search=_filtered_search(products),
            push_fn=_push,
        )
    )

    done = _done(events)
    assert done["finishReason"] == "stop"
    assert done["relaxationNotice"]  # 기계 판독 플래그 — null 이 아니다
    assert "평점" in done["relaxationNotice"]
    # 산문 안내도 token 으로 흐른다(§3.1) — 조용히 조건을 바꾸지 않는다.
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any("평점" in t for t in tokens)
    assert "products.ready" in _types(events)
    assert push_calls


async def test_explicit_price_constraint_is_never_auto_relaxed() -> None:
    """[AC-REC-08 회귀] 가격은 자동 완화 대상이 아니다 — 상한 초과 상품이 조용히 노출되면 안 된다."""
    products = [_product(201, 55000), _product(202, 60000)]
    events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_filtered_search(products), push_fn=None
        )
    )

    done = _done(events)
    assert done["finishReason"] == "zero_result"  # 완화 칩만 제안하고 스스로 넘지 않는다
    assert done["relaxationNotice"] is None
    assert "products.ready" not in _types(events)


# ─────────── 스트림 — 소량 ───────────


async def test_few_results_emit_chips_before_products_ready() -> None:
    """[AC①] 결과가 소량이면 상품은 그대로 보여주면서 완화 칩을 함께 제안한다."""

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    # priceMax=50000 통과 2건(임계 3 미만) + 완화 시 추가되는 1건.
    products = [_product(101, 39000), _product(102, 48000), _product(201, 60000)]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_filtered_search(products),
            push_fn=_push,
        )
    )

    chips = _suggestions(events)
    assert [c["relaxation"]["field"] for c in chips] == ["priceMax"]
    assert chips[0]["estCount"] == 3  # 완화 후 전량
    types = _types(events)
    # 순서 계약(§3.1): suggestions 는 products.ready 앞이다.
    assert types.index("suggestions") < types.index("products.ready")
    assert _done(events)["finishReason"] == "stop"


async def test_enough_results_do_not_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """결과가 충분하면 완화 probe 를 돌리지 않는다 — 정상 경로에 추가 Spring 호출을 얹지 않는다."""

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    monkeypatch.setattr(get_settings(), "relaxation_min_results", 1)
    products = [_product(101, 39000), _product(102, 48000)]
    calls: list[ProductSearchFilters] = []
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_filtered_search(products, calls=calls),
            push_fn=_push,
        )
    )

    assert [f.price_max for f in calls] == [50000]  # probe 없음
