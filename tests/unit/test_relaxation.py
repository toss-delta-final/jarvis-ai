"""0건/소량 조건 완화 (이슈 #113) — 완화 후보 생성 · 칩 emit · 자동 완화 안내 · 칩 클릭 되받기.

계약: api-spec §3.1 `suggestions`.`relaxation`={field,value}·estCount.
SPEC-RECOMMEND-001 §6.6(REQ-REC-040~046), AC-REC-08(가격 제약 불가침).
자동 완화 고지는 **token 산문**이 담당한다 — `done` 은 정본(CH-2)대로 `finishReason` 뿐이다.

estCount 는 page-local 로 못 구한다 — priceMax·brand·color 는 Spring I-1 쿼리 파라미터라 탈락
상품이 응답에 아예 없다. 그래서 완화 필터로 **재검색(probe)** 해 실제 매칭 수를 센다. 아래 fake
search 는 그 전제를 재현하려고 **필터를 실제로 적용**한다(기존 _make_search 는 전량 반환).
"""

from __future__ import annotations

import json
import logging
import math
from fractions import Fraction

import pytest

from app.agents.buyer import graph as buyer_graph
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
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


def test_price_relaxation_is_free_of_float_rounding_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """비율을 바꿔도 부동소수점 오차로 한 단위 튀지 않는다.

    `50000 * 1.1 == 55000.00000000001` 이라 곱셈 결과를 그대로 올림하면 55,000 이어야 할 제안이
    56,000 으로 튄다 — 기본 비율(0.3)에서는 안 걸리지만 비율은 config 주입이라 운영이 바꾸는
    순간 조용히 틀린 숫자가 칩에 찍힌다. 정확한 유리수 계산과 대조해 고정한다.
    """
    settings = get_settings()
    for ratio in (0.1, 0.15, 0.2, 0.25, 0.3, 0.33, 0.5):
        monkeypatch.setattr(settings, "relaxation_price_step_ratio", ratio)
        unit = settings.relaxation_price_round_unit
        for price in (10000, 50000, 55000, 90000, 100000, 123000):
            exact = math.ceil(Fraction(price) * (1 + Fraction(str(ratio))) / unit) * unit
            got = build_relaxation_candidates(ProductSearchFilters(price_max=price), settings)
            assert got[0].value == exact, f"ratio={ratio} price={price}"


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
    """[AC②] 약한 조건(평점)은 자동 완화하되 **반드시 token 으로 고지**한다."""
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

    assert _done(events)["finishReason"] == "stop"
    # 투명 안내는 **token 산문**이 담당한다(REQ-REC-042) — 조용히 조건을 바꾸지 않는다.
    # done 에는 싣지 않는다: 정본(CH-2)이 done 을 finishReason 만으로 확정했고 FE 도 안 읽는다.
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any("평점" in t and "넓혔어요" in t for t in tokens)
    assert "relaxationNotice" not in _done(events)
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

    assert _done(events)["finishReason"] == "zero_result"  # 완화 칩만 제안하고 스스로 넘지 않는다
    assert "products.ready" not in _types(events)
    # 자동 완화가 안 걸렸으므로 "넓혔어요" 안내도 없다 — 사용자는 상한이 지켜졌음을 신뢰할 수 있다.
    assert not [e for e in events if e["type"] == "token" and "넓혔어요" in e["data"]["text"]]


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


async def test_probe_budget_caps_extra_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    """[비용 가드] 완화 후보가 여럿이어도 추가 검색은 config 상한을 넘지 않는다.

    probe 는 0건 턴에 붙는 **추가 Spring 왕복**이라 상한이 없으면 후보 수만큼 곱해진다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "relaxation_max_probes", 1)
    # 완화 가능한 후보 3종(가격·브랜드·색상)을 주고도 probe 는 1회여야 한다.
    calls: list[ProductSearchFilters] = []
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(priceMax=50000, brand=["BrandZ"], color="검정")),
            search=_filtered_search([], calls=calls),
            push_fn=None,
        )
    )

    assert len(calls) == 2  # 원 검색 1 + probe 1(상한)
    assert _done(events)["finishReason"] == "zero_result"


async def test_fanout_relaxation_probes_every_leg_with_relaxed_filter() -> None:
    """fan-out(카테고리 여럿) 턴도 완화 probe 가 leg 구성을 그대로 유지한다.

    probe 가 leg 를 재현하지 않고 단일 검색으로 세면 본 검색과 조건이 어긋나 estCount 가 거짓이 된다.
    """

    async def _two_leg(*, category_queries, utterance, settings, llm=None, tier="fast", **_):  # noqa: ANN001
        return CategoryMapping(
            legs=[("무선이어폰", "무선 이어폰"), ("파우치", "파우치")], unresolved=[]
        )

    calls: list[ProductSearchFilters] = []
    products = [_product(201, 60000)]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_filtered_search(products, calls=calls),
            push_fn=None,
            map_categories=_two_leg,
        )
    )

    # leg 2개 × (원 검색 + 완화 probe) = 4회. 완화분은 상향된 상한을 싣는다.
    relaxed = [f for f in calls if f.price_max == 65000]
    assert len(relaxed) == 2
    assert {f.category for f in relaxed} == {"무선이어폰", "파우치"}
    chips = _suggestions(events)
    assert [c["relaxation"]["field"] for c in chips] == ["priceMax"]


# ─────────── 칩 클릭 되받기 (FE 는 label 을 다음 턴 message 로 보낸다) ───────────


async def test_clicking_chip_applies_exact_stored_value() -> None:
    """[핵심] 칩 label 이 그대로 다음 턴 message 로 오면 저장된 값을 **정확히** 적용한다.

    FE 는 `applySuggestion` 이 `send(chip.label)` 이라 "65,000원까지 볼까요?" 라는 **의문문**이
    메시지로 들어온다. LLM 해석에 맡기면 숫자를 못 뽑거나 되물음으로 흘러 칩이 무동작이 된다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    products = [_product(201, 60000), _product(202, 62000)]
    calls: list[ProductSearchFilters] = []
    search = _filtered_search(products, calls=calls)

    # 1턴 — 0건 → 완화 칩 제안(+ 스레드에 기억)
    first = await _collect(
        run_buyer_turn(_req(), _member(), llm=FakeLLM(), search=search, push_fn=_push)
    )
    label = _suggestions(first)[0]["label"]
    assert "65,000" in label

    # 2턴 — FE 가 그 label 을 그대로 message 로 보낸다. decompose 는 이 문장에서 조건을 못 뽑는
    # 상황을 재현하려고 **빈 filters** 를 내도록 둔다 — 그래도 저장된 값이 이겨야 한다.
    turn2 = len(calls)
    second = await _collect(
        run_buyer_turn(
            _req(message=label),
            _member(),
            llm=FakeLLM(decompose=_decompose_with()),
            search=search,
            push_fn=_push,
        )
    )

    # 2턴의 **본 검색**(뒤따르는 건 소량 완화 probe 라 상한이 더 높다)
    assert calls[turn2].price_max == 65000  # 우리가 계산해 둔 정확한 값
    assert "products.ready" in _types(second)  # 완화된 조건의 상품이 실제로 나간다


async def test_clicking_chip_preserves_other_prior_filters() -> None:
    """칩 클릭 턴은 **직전 필터 전체**를 유지한 채 그 조건 하나만 푼다.

    estCount 는 "직전 필터 + 이 조건만 완화"로 센 값인데, 멀티턴 병합은 코드가 아니라 LLM 이
    한다(decompose PRIOR_FILTERS 병합 지시). 의문문 label 을 받은 decompose 가 축을 빠뜨리면
    priceMin·brand 가 조용히 사라져 **약속한 건수와 실제 결과가 어긋난다.**
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    products = [_product(201, 60000)]
    calls: list[ProductSearchFilters] = []
    search = _filtered_search(products, calls=calls)

    # 1턴 — priceMin 도 함께 걸린 상태에서 0건 → priceMax 완화 칩
    first = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(priceMax=50000, priceMin=10000)),
            search=search,
            push_fn=_push,
        )
    )
    label = _suggestions(first)[0]["label"]

    # 2턴 — decompose 가 아무 조건도 못 뽑은 최악의 경우
    turn2 = len(calls)
    await _collect(
        run_buyer_turn(
            _req(message=label),
            _member(),
            llm=FakeLLM(decompose=_decompose_with()),
            search=search,
            push_fn=_push,
        )
    )

    assert calls[turn2].price_max == 65000  # 완화된 축
    assert calls[turn2].price_min == 10000  # **유실되면 안 되는 축**


async def test_clicking_chip_forces_recommend_route() -> None:
    """칩 클릭은 decompose 가 general 로 라우팅해도 추천 턴으로 처리한다.

    label 이 의문문이라 일반 대화로 새기 쉽다 — 그러면 버튼을 눌렀는데 잡담 응답이 돌아온다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    products = [_product(201, 60000)]
    search = _filtered_search(products)
    first = await _collect(
        run_buyer_turn(_req(), _member(), llm=FakeLLM(), search=search, push_fn=_push)
    )
    label = _suggestions(first)[0]["label"]

    second = await _collect(
        run_buyer_turn(
            _req(message=label),
            _member(),
            llm=FakeLLM(decompose={"intent": "general", "reply": "네 그럼요", "filters": {}}),
            search=search,
            push_fn=_push,
        )
    )

    assert "products.ready" in _types(second)
    assert "conditions" in _types(second)  # 추천 경로를 탔다


async def test_unmatched_message_does_not_apply_stored_offer() -> None:
    """저장된 칩과 다른 발화는 기존 경로 그대로 — 옛 제안이 조용히 되살아나지 않는다."""

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    products = [_product(201, 60000)]
    calls: list[ProductSearchFilters] = []
    search = _filtered_search(products, calls=calls)
    await _collect(run_buyer_turn(_req(), _member(), llm=FakeLLM(), search=search, push_fn=_push))

    before = len(calls)
    await _collect(
        run_buyer_turn(
            _req(message="다른 거 보여줘"),
            _member(),
            llm=FakeLLM(),  # DEFAULT_DECOMPOSE — priceMax 50000
            search=search,
            push_fn=_push,
        )
    )

    assert calls[before].price_max == 50000  # 완화가 적용되지 않았다


async def test_offer_store_failure_does_not_break_the_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[degrade] 칩 기억 저장소가 죽어도 턴은 정상 완료된다 — 편의 기능이 본류를 막지 않는다.

    읽기·쓰기 **양쪽 가드가 실제로 실행됐는지**를 로그로 확인한다 — "그냥 안 깨졌다"만 보면
    가드를 지워도 통과하는(호출 자체가 없는) 테스트가 되어 회귀를 못 잡는다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    class _BrokenStore:
        async def get(self, key):  # noqa: ANN001
            raise TimeoutError("pg down")

        async def put(self, key, offers):  # noqa: ANN001
            raise TimeoutError("pg down")

    async def _broken():
        return _BrokenStore()

    monkeypatch.setattr(buyer_graph, "get_relaxation_offer_store", _broken)
    products = [_product(101, 39000), _product(102, 48000)]  # 2건 = 소량 → 칩 생성 → 쓰기 경로 진입
    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(), _member(), llm=FakeLLM(), search=_filtered_search(products), push_fn=_push
            )
        )

    assert _types(events)[-1] == "done"
    assert "error" not in _types(events)
    assert "products.ready" in _types(events)
    assert "relaxation_offer_read_failed" in caplog.text
    assert "relaxation_offer_write_failed" in caplog.text


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
