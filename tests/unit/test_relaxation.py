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
from dataclasses import replace
from fractions import Fraction

import pytest
from pydantic import ValidationError

from app.agents.buyer import graph as buyer_graph
from app.agents.buyer.recommendation import graph as recommendation_graph
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.recommendation.relaxation import build_relaxation_candidates
from app.core.auth import Identity
from app.core.config import Settings, get_settings
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services.spring_client import SpringUnavailableError, _search_query_params
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


def _conditions(events) -> list[dict]:
    return [c for e in events if e["type"] == "conditions" for c in e["data"]["chips"]]


async def test_conditions_chips_reflect_the_auto_relaxed_value(monkeypatch) -> None:  # noqa: ANN001
    """[PR #248 리뷰 A] 자동 완화가 걸리면 조건 칩도 **완화된 값**으로 나간다.

    조건 칩은 원래 검색 전에 나가는데 자동 완화는 검색 후에 조건을 바꾼다 — 먼저 내보내면
    "평점 4.5 이상" 칩이 떠 있는데 실제 상품은 4.0 기준인 표시-실제 불일치가 남는다.
    §3.1 이 conditions 를 0~1회로 못박아 재전송이 불가하므로 **순서**로 푼다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

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

    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.0  # 완화된 값 — 사용자가 보는 조건과 실제가 같다
    types = _types(events)
    assert types.count("conditions") == 1  # §3.1 0~1회 — 재전송하지 않는다
    # §3.1 순서 계약: conditions → token(완화 고지 포함) → products.ready
    assert types.index("conditions") < types.index("token") < types.index("products.ready")


@pytest.mark.parametrize("chip_budget", [0, 2])
async def test_conditions_match_the_search_regardless_of_chip_budget(
    chip_budget: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조건 칩 지연은 **자동 완화 자신의 조건**만 보고 정한다 — 칩 예산과 무관하다.

    두 손잡이를 엮으면 칩을 끈 설정(`relaxation_max_probes=0`)에서 자동 완화는 도는데 조건 칩만
    미리 나가, 화면엔 "평점 4.5" 인데 실제는 4.0 인 표시-실제 불일치가 되살아난다.
    """
    monkeypatch.setattr(get_settings(), "relaxation_max_probes", chip_budget)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

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

    assert any("넓혔어요" in e["data"].get("text", "") for e in events if e["type"] == "token")
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.0  # 실제 검색값과 일치


async def test_conditions_chips_are_not_delayed_when_auto_relax_cannot_fire() -> None:
    """완화가 불가능한 턴(평점 조건 없음)은 종전대로 **검색 전에** 칩을 내보낸다.

    모든 턴을 미루면 드문 경우 하나 때문에 절대다수 턴의 첫 프레임이 늦어진다.
    """
    seen: list[str] = []

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        seen.append("search")
        return ProductSearchResult(products=[_product(101, 39000)], total_count=1)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    events = []
    async for frame in run_buyer_turn(
        _req(), _member(), llm=FakeLLM(), search=_search, push_fn=_push
    ):
        line = frame.strip()
        if line.startswith("data:"):
            evt = json.loads(line[len("data:") :].strip())
            if evt["type"] == "conditions":
                seen.append("conditions")
            events.append(evt)

    assert seen[0] == "conditions"  # 검색보다 먼저 나갔다
    assert _types(events).count("conditions") == 1


def _three_axis_catalog():
    """완화 축마다 **하나씩만** 살아나는 카탈로그 — 축별 estCount 를 독립적으로 검증한다."""
    return [
        _product(101, 30000, rating=4.8, brand="BrandX"),  # 원 조건에 맞는 유일한 상품
        _product(102, 60000, rating=4.8, brand="BrandX"),  # 가격을 풀어야 나온다
        _product(103, 30000, rating=4.2, brand="BrandX"),  # 평점을 풀어야 나온다
        _product(104, 30000, rating=4.8, brand="BrandY"),  # 브랜드를 풀어야 나온다
    ]


def _three_axis_decompose():
    return _decompose_with(priceMax=50000, ratingMin=4.5, brand=["BrandX"])


async def test_default_probe_budget_covers_every_default_chip_field() -> None:
    """[PR #248 2차 리뷰] 기본 설정이 켜 둔 완화 필드는 **전부** 칩이 된다.

    probe 예산이 후보 수보다 적으면 뒤쪽 후보는 estCount 를 못 구하고, estCount 없는 칩은 만들 수
    없어 조용히 사라진다 — 실제로 그 조건을 풀면 결과가 있는데도. 기본값이 `chip_fields` 4개를
    켜 두고 예산은 2 라서 앞 2개만 동작하던 자기모순을 고정한다(축 3개면 3개 다 나와야 한다).
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_three_axis_decompose()),
            search=_filtered_search(_three_axis_catalog()),
            push_fn=_push,
        )
    )

    relax = {c["relaxation"]["field"]: c for c in _suggestions(events) if c.get("relaxation")}
    assert set(relax) == {"priceMax", "ratingMin", "brand"}  # 셋 다 살아남는다
    assert all(c["estCount"] == 2 for c in relax.values())  # 원본 1건 + 그 축으로 살아난 1건


async def test_probe_budget_shortfall_drops_chips_but_says_so(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """예산을 조이면 뒤쪽 축이 빠지되 **조용히** 빠지지는 않는다.

    상한을 낮추는 건 운영의 선택이라 막지 않는다. 다만 잘린 사실이 로그에 없으면 "브랜드를 풀면
    결과가 있는데 칩이 안 뜬다"를 아무도 관측할 수 없어, 다음 사람이 근거 없이 예산을 만지게 된다.
    """
    monkeypatch.setattr(get_settings(), "relaxation_max_probes", 2)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_three_axis_decompose()),
                search=_filtered_search(_three_axis_catalog()),
                push_fn=_push,
            )
        )

    fields = {c["relaxation"]["field"] for c in _suggestions(events) if c.get("relaxation")}
    assert fields == {"priceMax", "ratingMin"}  # 우선순위 앞 2개만 — brand 는 잘렸다
    # 무엇이 빠졌는지까지 남는다 — `extra` 는 메시지가 아니라 레코드 속성이라 record 로 본다.
    dropped = next(r for r in caplog.records if r.message == "relaxation_chips_truncated")
    assert dropped.dropped == ["brand"] and dropped.budget == 2


async def test_disabling_auto_relaxation_also_disables_the_conditions_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`relaxation_max_rounds=0` 은 자동 완화와 **함께 지연도** 끈다.

    평점 조건이 걸린 턴은 완화가 실제로 안 일어나도 조건 칩이 검색만큼 늦는다(검색 전에 나가는
    이벤트가 conditions 하나뿐이라 첫 프레임이 통째로 밀린다). 그 비용이 아까운 배포를 위한
    탈출구가 이 손잡이인데, 게이트가 `max_rounds` 를 안 보면 0 으로 꺼도 지연만 남는다 —
    완화는 루프 진입 즉시 break 되므로 **아무 이득 없이** 느려지기만 한다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "relaxation_max_rounds", 0)
    seen: list[str] = []

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        seen.append("search")
        return ProductSearchResult(products=[_product(101, 39000)], total_count=1)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    events = []
    async for frame in run_buyer_turn(
        _req(),
        _member(),
        # 평점 조건 有 — 게이트가 max_rounds 를 안 보면 이 턴이 미뤄진다
        llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
        search=_search,
        push_fn=_push,
    ):
        line = frame.strip()
        if line.startswith("data:"):
            evt = json.loads(line[len("data:") :].strip())
            if evt["type"] == "conditions":
                seen.append("conditions")
            events.append(evt)

    assert seen[0] == "conditions"  # 검색보다 먼저 나갔다
    assert _types(events).count("conditions") == 1
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.5  # 완화가 꺼져 있으니 미리 내보내도 거짓이 아니다


async def test_deferred_conditions_survive_fanout_merge_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 리뷰] fan-out 병합이 터져도 미뤄 둔 조건 칩은 나간다.

    `_leg` 는 leg 별 실패를 잡는데 그 결과를 합치는 `_merge_fanout_results` 호출만 방어 밖이라,
    거기서 예외가 나면 `search_bundle is None` 분기(→ SEARCH_FAILED, 미뤄 둔 conditions 발신)에
    **도달하지 못하고** 스트림이 끊겼다 — conditions 를 검색 뒤로 미루면서 생긴 새 실패 경로다.
    """

    async def _two_leg(*, category_queries, utterance, settings, llm=None, tier="fast", **_):  # noqa: ANN001
        return CategoryMapping(legs=[("무선이어폰", "이어폰"), ("파우치", "파우치")], unresolved=[])

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("merge boom")

    monkeypatch.setattr(recommendation_graph, "_merge_fanout_results", _boom)
    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),  # 미루는 턴
                search=_filtered_search([_product(101, 30000, rating=4.8)]),
                push_fn=None,
                map_categories=_two_leg,
            )
        )

    types = _types(events)
    assert types.count("conditions") == 1  # 조건 칩이 사라지지 않는다
    assert types.index("conditions") < types.index("error")
    assert types[-1] == "error"  # 병합 실패는 SEARCH_FAILED 로 degrade
    assert "search_merge_failed" in caplog.text
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.5  # 완화가 일어나지 않았으므로 원래 값


async def test_post_filter_failure_is_tagged_before_it_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 3차 리뷰] 사후필터 실패는 다시 올리되 **이름표를 남기고** 올린다.

    이 가드는 예외를 삼키지 않으니 유실 문제는 없다 — 후보가 확정되지 않은 채 추천을 이어갈 수는
    없어서 **의도적으로** 다시 올린다. 잃는 건 **어느 단계였는지**다: 이 함수의 다른 실패 경로는
    전부 이벤트명으로 단계를 특정하는데 여기만 없으면 프로덕션에서 원인 태그 없는 raw traceback 만
    남아 집계·분류가 안 된다. 올리기 **전에** 미룬 조건 칩을 내보내는 것도 같이 고정한다.
    """

    class _BrokenResult:
        total_count = 0

        @property
        def products(self):  # noqa: ANN201
            raise RuntimeError("post-filter boom")

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        return _BrokenResult()

    no_fanout = json.loads(json.dumps(DEFAULT_DECOMPOSE))
    no_fanout["categoryQueries"] = []  # fan-out 병합 가드보다 먼저 걸리지 않게
    no_fanout["filters"] = {"ratingMin": 4.5}  # 미루는 턴이어야 conditions 보장을 같이 본다

    types: list[str] = []
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="post-filter boom"):
        # 예외가 전파되는 게 정상이라 `_collect` 대신 직접 돌며 그 **전까지** 나간 이벤트를 본다.
        async for frame in run_buyer_turn(
            _req(), _member(), llm=FakeLLM(decompose=no_fanout), search=_search, push_fn=None
        ):
            line = frame.strip()
            if line.startswith("data:"):
                types.append(json.loads(line[len("data:") :].strip())["type"])

    assert "search_post_filter_failed" in caplog.text  # 단계가 특정된다
    assert types.count("conditions") == 1  # 올리기 전에 미룬 조건 칩이 나갔다


async def test_relaxation_notice_is_sanitized_like_every_other_outbound_text() -> None:
    """[PR #248 3차 리뷰] 자동 완화 안내문도 다른 출력 텍스트와 같은 정제를 거친다.

    지금 문구는 하드코딩 한국어 + 숫자 포맷뿐이라 실질 위험이 없지만, 이 자리만 방어가 빠져 있으면
    나중에 config·가변 텍스트를 섞는 순간 조용히 구멍으로 남는다. 그때 깨지는 게 아니라 **지금**
    깨지도록 고정한다 — 문구 생성기가 위험 문자를 내도 스트림에는 안 나가야 한다.
    """
    nasty = "​‮\x07"  # zero-width + bidi override + 제어문자

    def _tainted(filters, settings):  # noqa: ANN001
        return [
            replace(c, notice=nasty + c.notice + nasty)
            for c in build_relaxation_candidates(filters, settings)
        ]

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(recommendation_graph, "build_relaxation_candidates", _tainted)
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
                search=_rating_search([_product(101, 39000, rating=4.2)], []),
                push_fn=_push,
            )
        )

    notice = next(
        e["data"]["text"]
        for e in events
        if e["type"] == "token" and "넓혔어요" in e["data"]["text"]
    )
    assert not any(ch in notice for ch in nasty), f"정제되지 않은 문자가 남았다: {notice!r}"


async def test_deferred_conditions_still_emitted_when_search_fails() -> None:
    """미룬 조건 칩이 검색 실패 턴에서 통째로 사라지지 않는다.

    `SEARCH_FAILED` 는 완화 판정보다 **앞에서** 종료하므로, 미루기만 하고 이 경로를 빠뜨리면
    평점 조건이 걸린 턴만 조건 칩이 사라진다(미루기 전에는 항상 나갔다).
    """

    async def _dead_search(filters, exclude_product_ids=None):  # noqa: ANN001
        raise SpringUnavailableError("spring down")

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),  # 완화 가능 턴 → 칩 미뤄짐
            search=_dead_search,
            push_fn=None,
        )
    )

    types = _types(events)
    assert types.count("conditions") == 1
    assert types.index("conditions") < types.index("error")
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.5  # 완화가 일어나지 않았으므로 원래 값


# ─────────── 자동 완화의 다음 턴 승계 (팀 합의 설계) ───────────


def _rating_search(products, calls):
    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        calls.append(filters.rating_min)
        kept = [
            p
            for p in products
            if filters.rating_min is None or (p.rating or 0) >= filters.rating_min
        ]
        return ProductSearchResult(products=kept, total_count=len(kept))

    return _search


async def _relaxed_first_turn(calls):
    """턴1 — 평점 4.5 로 0건 → 4.0 자동 완화 채택."""

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    products = [_product(101, 39000, rating=4.2), _product(102, 48000, rating=4.1)]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
            search=_rating_search(products, calls),
            push_fn=_push,
        )
    )
    assert any("넓혔어요" in e["data"].get("text", "") for e in events if e["type"] == "token")
    return products


async def test_plain_refine_does_not_carry_the_auto_relaxation() -> None:
    """["더 저렴한 걸로"] 직전 결과를 가리키지 않으면 **원래 조건(4.5)** 으로 되돌아간다.

    자동 완화는 사용자가 동의한 적 없는 서버 조치라, 참조 없는 리파인은 사용자가 말한 제약을
    다시 존중한다 — 그 턴에 또 완화가 필요하면 또 고지한다(SPEC "매 완화 알림").
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    calls: list = []
    products = await _relaxed_first_turn(calls)

    turn2 = len(calls)
    await _collect(
        run_buyer_turn(
            _req(message="더 저렴한 걸로"),
            _member(),
            # 참조 없는 리파인 — scopedToPrevious 미산출(기본 False)
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
            search=_rating_search(products, calls),
            push_fn=_push,
        )
    )

    assert calls[turn2] == 4.5  # 원래 조건으로 되돌아간다


async def test_scoped_refine_carries_the_auto_relaxation(caplog: pytest.LogCaptureFixture) -> None:
    """["그 중에 더 저렴한 걸로"] 직전 결과를 가리키면 완화(4.0)를 이어받는다.

    사용자가 완화된 결과를 자기 후보로 인정한 것이라 칩 클릭과 같은 동의 신호로 본다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    calls: list = []
    products = await _relaxed_first_turn(calls)

    scoped = _decompose_with(ratingMin=4.5)
    scoped["scopedToPrevious"] = True
    turn2 = len(calls)
    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(message="그 중에 더 저렴한 걸로"),
                _member(),
                llm=FakeLLM(decompose=scoped),
                search=_rating_search(products, calls),
                push_fn=_push,
            )
        )

    assert calls[turn2] == 4.0  # 완화를 이어받아 **헛검색 없이** 바로 결과
    # 이어받은 턴은 완화가 새로 일어난 게 아니므로 고지가 반복되지 않는다.
    assert not any("넓혔어요" in e["data"].get("text", "") for e in events if e["type"] == "token")
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.0  # 상태는 조건 칩으로 드러난다
    # 승계 턴도 "사용자가 처음 말한 조건이 아닌 상태"라 관측에 남아야 한다 — 안 남기면
    # `relax_field`(이번 턴 채택분)만 세는 지표가 완화된 턴 절반을 놓친다.
    assert "relaxation_carried" in caplog.text


async def test_carry_turn_reads_the_store_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """[PR #248 리뷰] 승계 턴도 스냅샷을 **한 번만** 읽는다.

    `offers`(칩 클릭 판정)와 `applied`(승계)를 따로 읽으면 왕복이 2회로 늘 뿐 아니라, 그 사이에
    다른 턴의 `put` 이 끼었을 때 옛 offers + 새 applied 라는 찢어진 조합을 본다 — 쓰기를 단일
    `aput` 으로 묶어 없앤 상태가 읽기 쪽에서 되살아난다.
    """
    from app.agents.buyer.recommendation.state import RelaxationOfferStore

    real = RelaxationOfferStore()
    reads: list = []

    class _Counting:
        async def get_snapshot(self, key):  # noqa: ANN001
            reads.append(key)
            return await real.get_snapshot(key)

        async def put(self, key, offers, applied):  # noqa: ANN001
            await real.put(key, offers, applied)

    async def _factory():
        return _Counting()

    monkeypatch.setattr(buyer_graph, "get_relaxation_offer_store", _factory)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    calls: list = []
    products = await _relaxed_first_turn(calls)

    scoped = _decompose_with(ratingMin=4.5)
    scoped["scopedToPrevious"] = True
    turn2 = len(calls)
    reads.clear()
    await _collect(
        run_buyer_turn(
            _req(message="그 중에 더 저렴한 걸로"),
            _member(),
            llm=FakeLLM(decompose=scoped),
            search=_rating_search(products, calls),
            push_fn=_push,
        )
    )

    assert calls[turn2] == 4.0  # 승계는 실제로 일어났다(읽기를 줄이려고 기능을 끈 게 아니다)
    assert len(reads) == 1, f"승계 턴의 스토어 왕복은 1회여야 한다 — 실제 {len(reads)}회"


async def test_store_read_failure_skips_carry_without_a_second_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """스냅샷 읽기가 통째로 실패하면 승계는 **조용히** 빠진다 — 실패 로그는 하나뿐이다.

    읽기를 하나로 합치면서 `applied` 를 try 앞에서 초기화해 둔 이유다. 초기화가 없으면 읽기 실패
    턴에 승계 분기가 **미정의 지역변수**를 건드려 `UnboundLocalError` 가 나고, 아래 가드가 그걸
    삼켜 `relaxation_carry_failed` 라는 **가짜 2차 실패**를 남긴다 — 원인이 하나인데 로그가 둘로
    갈라져 관측이 어긋난다.
    """

    class _Broken:
        async def get_snapshot(self, key):  # noqa: ANN001
            raise RuntimeError("pg down")

        async def put(self, key, offers, applied):  # noqa: ANN001
            return None

    async def _factory():
        return _Broken()

    monkeypatch.setattr(buyer_graph, "get_relaxation_offer_store", _factory)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    scoped = _decompose_with(ratingMin=4.5)
    scoped["scopedToPrevious"] = True  # 승계 분기에 실제로 들어가는 턴
    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(message="그 중에 더 저렴한 걸로"),
                _member(),
                llm=FakeLLM(decompose=scoped),
                search=_filtered_search([_product(101, 39000, rating=4.8)]),
                push_fn=_push,
            )
        )

    assert _types(events)[-1] == "done" and "error" not in _types(events)
    assert "relaxation_offer_read_failed" in caplog.text  # 원인은 이것 하나
    assert "relaxation_carry_failed" not in caplog.text  # 파생된 가짜 실패가 없다


async def test_corrupt_stored_relaxation_does_not_kill_the_carry_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """저장된 `applied` 가 손상돼도 승계 턴은 죽지 않고 사용자가 말한 조건으로 검색한다.

    저장소는 신뢰 경계 밖이라 `field` 에 **해시 불가능한 값**(list 등)이 들어올 수 있는데,
    그러면 `FIELD_TO_ATTR.get(...)` 이 `TypeError` 를 던진다 — `isinstance` 검사만으로는 못 막는
    자리라 승계 블록의 가드가 실제로 맡는 일이다(가드를 지우면 추천 턴 전체가 죽는다).
    """

    class _Corrupt:
        async def get_snapshot(self, key):  # noqa: ANN001
            return {}, {"field": ["ratingMin"], "value": 4.0}  # field 가 해시 불가

        async def put(self, key, offers, applied):  # noqa: ANN001
            return None

    async def _factory():
        return _Corrupt()

    monkeypatch.setattr(buyer_graph, "get_relaxation_offer_store", _factory)

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    calls: list = []
    products = await _relaxed_first_turn(calls)  # prior(4.5) 를 만든다

    scoped = _decompose_with(ratingMin=4.5)
    scoped["scopedToPrevious"] = True
    turn2 = len(calls)
    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(message="그 중에 더 저렴한 걸로"),
                _member(),
                llm=FakeLLM(decompose=scoped),
                search=_rating_search(products, calls),
                push_fn=_push,
            )
        )

    assert _types(events)[-1] == "done" and "error" not in _types(events)
    assert "relaxation_carry_failed" in caplog.text
    assert calls[turn2] == 4.5  # 승계만 조용히 빠지고 사용자가 말한 조건은 그대로 산다


async def test_conditions_survive_a_broken_relaxation_gate(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 리뷰] 완화 판정이 터져도 조건 칩은 나간다.

    이 판정은 conditions 발신 **이전** 경로라 감싸지 않으면 이벤트가 하나도 없이 스트림이 죽는다
    (`_merge_fanout_results` 와 같은 모양). 미루지 않는 쪽으로 떨구는 게 맞다 — 같은 이유로 아래
    완화 블록도 실패해 완화가 일어나지 않으므로 표시-실제 불일치가 생기지 않는다.

    **0건 턴으로 본다** — 결과가 넉넉하면 완화 블록에 들어가지도 않아 "미리 내보내도 안전하다"는
    바로 그 주장이 검증되지 않는다. 0건이어야 자동 완화가 시도되고, 그게 같은 이유로 실패하는
    것까지 확인할 수 있다.
    """

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("gate boom")

    monkeypatch.setattr(recommendation_graph, "build_relaxation_candidates", _boom)
    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
                search=_filtered_search([]),  # 0건 — 자동 완화가 시도되는 턴
                push_fn=None,
            )
        )

    types = _types(events)
    assert types.count("conditions") == 1  # 조건 칩이 사라지지 않는다
    assert types[-1] == "done" and "error" not in types  # 스트림은 정상 완주
    assert "relaxation_gate_failed" in caplog.text  # 판정이 degrade 됐고
    assert "relaxation_auto_failed" in caplog.text  # 같은 이유로 자동 완화도 안 돌았다
    # 그래서 미리 내보낸 4.5 는 거짓말이 아니다 — 완화된 상품이 실리지 않았다.
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.5
    assert "products.ready" not in types


async def test_scoped_refine_keeps_this_turn_new_condition() -> None:
    """["그 중에 **더 저렴한** 걸로"] 승계하면서 **이번 턴에 새로 말한 조건**도 지킨다.

    승계 기준을 직전 턴 필터(prior)로 잡으면 이번 턴 발화의 새 조건이 통째로 버려진다 —
    사용자는 "더 저렴한"이라고 말했는데 가격 조건이 사라진다. 완화 축 하나만 덮어써야 한다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    seen: list = []

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        seen.append((filters.rating_min, filters.price_max))
        kept = [
            p
            for p in [_product(101, 39000, rating=4.2), _product(102, 48000, rating=4.1)]
            if (filters.rating_min is None or (p.rating or 0) >= filters.rating_min)
            and (filters.price_max is None or (p.price or 0) <= filters.price_max)
        ]
        return ProductSearchResult(products=kept, total_count=len(kept))

    # 턴1 — 평점 4.5 로 0건 → 4.0 자동 완화
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
            search=_search,
            push_fn=_push,
        )
    )

    # 턴2 — "그 중에 더 저렴한 걸로": decompose 가 새 가격 조건을 냈다
    scoped = _decompose_with(ratingMin=4.5, priceMax=40000)
    scoped["scopedToPrevious"] = True
    turn2 = len(seen)
    await _collect(
        run_buyer_turn(
            _req(message="그 중에 더 저렴한 걸로"),
            _member(),
            llm=FakeLLM(decompose=scoped),
            search=_search,
            push_fn=_push,
        )
    )

    assert seen[turn2] == (4.0, 40000)  # 완화 승계(4.0) + 이번 턴 새 조건(40,000) 둘 다


@pytest.mark.parametrize(
    ("label", "turn2_rating", "expected"),
    [
        ("그 축을 언급 안 함(prior 그대로)", 4.5, 4.0),  # 승계
        ("같은 축을 낮춰 새로 말함", 3.0, 3.0),  # 사용자 값 우선
        ("같은 축을 높여 새로 말함", 4.8, 4.8),  # 사용자 값 우선
        ("같은 축을 아예 지움", None, None),  # 되살리지 않는다
    ],
)
async def test_scoped_refine_never_overwrites_a_restated_axis(
    label: str, turn2_rating: float | None, expected: float | None
) -> None:
    """[PR #248 리뷰] 승계가 **이번 턴에 다시 말한 같은 축**을 덮어쓰지 않는다.

    "그 중에 평점 3.0 이상도 볼래" 에서 저장된 완화값(4.0)으로 덮으면 방금 말한 3.0 이 흔적도
    없이 사라진다. 승계는 "직전 결과를 그대로 받아들인다"는 뜻이라, 그 축을 새로 말한 턴에는
    성립하지 않는다. 축을 **지운** 경우도 되살리지 않는다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    seen: list = []

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        seen.append(filters.rating_min)
        kept = [
            p
            for p in [_product(1, 30000, rating=4.2), _product(2, 35000, rating=3.5)]
            if filters.rating_min is None or (p.rating or 0) >= filters.rating_min
        ]
        return ProductSearchResult(products=kept, total_count=len(kept))

    # 턴1 — 평점 4.5 로 0건 → 4.0 자동 완화 채택
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
            search=_search,
            push_fn=_push,
        )
    )

    scoped = _decompose_with(**({"ratingMin": turn2_rating} if turn2_rating is not None else {}))
    scoped["scopedToPrevious"] = True
    turn2 = len(seen)
    await _collect(
        run_buyer_turn(
            _req(message="그 중에 골라줘"),
            _member(),
            llm=FakeLLM(decompose=scoped),
            search=_search,
            push_fn=_push,
        )
    )

    assert seen[turn2] == expected, label


@pytest.mark.parametrize(
    "stored",
    [None, {}, {"offers": "손상", "applied": "손상"}, {"offers": [], "applied": 7}],
    ids=["없음", "빈스냅샷", "문자열", "타입불일치"],
)
async def test_snapshot_falls_back_to_empty_offers_never_none(stored) -> None:  # noqa: ANN001
    """저장분이 없거나 손상돼도 `offers` 는 **항상 dict**, `applied` 는 None 이다.

    호출부가 `offers.get(message)` 를 바로 부르므로, None 을 돌려주면 저장분이 없는 **첫 턴마다**
    `AttributeError` → 경고 로그가 뜬다. 사용자에게 보이는 증상은 없지만 정상 동작이 장애로
    기록되어 관측을 오염시킨다. 폴백 타입은 계약이라 값이 아니라 **타입**을 고정한다.
    """
    from app.agents.buyer.recommendation.state import RelaxationOfferStore

    class _Store:
        async def aget(self, ns, key):  # noqa: ANN001
            return type("Item", (), {"value": stored})() if stored is not None else None

    offers, applied = await RelaxationOfferStore(_Store()).get_snapshot("t1")
    assert offers == {} and isinstance(offers, dict)
    assert applied is None


async def test_turn_snapshot_is_read_and_written_atomically() -> None:
    """[PR #248 리뷰] 칩 제안과 자동 완화는 **한 번의 쓰기·한 번의 읽기**로 오간다.

    둘은 "이번 턴 화면에 무엇이 있었나"라는 한 스냅샷이다. 따로 두 번 쓰면 뒤엣것만 pg 일시
    장애로 실패했을 때 `offers` 는 이번 턴인데 `applied` 는 지난 턴인 **찢어진 상태**가 남고,
    다음 턴 "그 중에" 가 화면에 보여준 적 없는 완화를 이어붙인다 — 단일 쓰기로 그 부분 실패를
    구조적으로 없앤다.

    **읽기도 같은 이유로 한 번이다**(2차 리뷰) — 쓰기만 묶고 읽기를 offers/applied 로 나누면,
    두 `aget` 사이에 다른 턴의 `put` 이 끼었을 때 같은 찢어진 조합이 읽기 쪽에서 되살아난다.
    """
    from app.agents.buyer.recommendation.state import RelaxationOfferStore

    writes: list = []
    reads: list = []

    class _CountingStore:
        def __init__(self):
            self._data: dict = {}

        async def aput(self, ns, key, value):  # noqa: ANN001
            writes.append((ns, key))
            self._data[(ns, key)] = value

        async def aget(self, ns, key):  # noqa: ANN001
            reads.append((ns, key))
            value = self._data.get((ns, key))
            return type("Item", (), {"value": value})() if value is not None else None

    store = RelaxationOfferStore(_CountingStore())
    await store.put(
        "t1", {"칩": {"field": "priceMax", "value": 65000}}, {"field": "ratingMin", "value": 4.0}
    )

    assert len(writes) == 1, f"스냅샷은 단일 쓰기여야 한다 — 실제 {len(writes)}회"
    assert await store.get_snapshot("t1") == (
        {"칩": {"field": "priceMax", "value": 65000}},
        {"field": "ratingMin", "value": 4.0},
    )
    # [PR #248 리뷰] **읽기도 1회**다 — 쓰기를 원자적으로 묶어 찢어짐을 없애 놓고 읽기를
    # offers/applied 로 나누면, 두 aget 사이에 다른 턴의 put 이 끼었을 때 옛 offers + 새 applied
    # 라는 같은 찢어짐이 읽기 쪽에서 되살아난다(승계 경로의 pg 왕복도 2회가 된다).
    assert len(reads) == 1, f"스냅샷은 단일 읽기여야 한다 — 실제 {len(reads)}회"

    # 다음 턴에 완화 채택이 없으면 같은 쓰기로 applied 가 비워진다(옛 값이 남지 않는다).
    await store.put("t1", {}, None)
    assert len(writes) == 2
    assert await store.get_snapshot("t1") == ({}, None)


@pytest.mark.parametrize("field", ["relaxation_chip_fields", "relaxation_auto_fields"])
def test_duplicate_relaxation_fields_fail_startup(field: str) -> None:
    """[PR #248 리뷰] 목록에 같은 필드가 중복되면 기동 실패.

    존재 여부만 `set()` 으로 보면 통과하는데, 후보 생성기는 **리스트를 순회**하므로 같은 필드의
    후보가 두 개 생긴다 — 같은 조건으로 Spring 을 두 번 재검색하고 같은 칩이 화면에 두 번 뜬다.
    """
    kwargs = (
        {"relaxation_chip_fields": ["priceMax", "priceMax", "ratingMin"]}
        if field == "relaxation_chip_fields"
        else {"relaxation_auto_fields": ["ratingMin", "ratingMin"]}
    )
    with pytest.raises(ValidationError) as exc:
        Settings(**kwargs)
    assert field.upper() in str(exc.value)


async def test_carry_is_skipped_on_general_turns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 리뷰] `general` 턴에서는 승계를 계산하지 않는다.

    general 은 `stream_fallback` 으로 바로 빠져 `decision.filters` 를 아무도 쓰지 않는다 —
    승계를 계산해 봐야 조용히 버려지고 "영속시킨다"는 주석만 거짓이 된다. 칩 클릭 분기처럼
    intent 를 강제하지도 않는다: 저쪽은 우리가 만든 label 과 정확히 일치해 오해의 여지가 없지만
    `scopedToPrevious` 는 LLM 판정이라 정보성 질문("그 중에 뭐가 인기 많아?")까지 납치한다.

    **승계가 성립하는 상태를 실제로 만들어 놓고 본다** — 신규 스레드는 `prior` 가 없어 intent 와
    무관하게 승계가 안 걸리므로, 그 상태로 "승계 안 됨"을 단언하면 intent 게이트를 지워도 통과하는
    빈 테스트가 된다. 그래서 턴1을 완화까지 태워 `prior`(4.5)·`applied`(4.0)를 만든 뒤,
    **같은 조건을 general 로만 바꿔** 승계가 막히는지 본다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    calls: list = []
    products = await _relaxed_first_turn(calls)

    # 턴2 — 승계 조건은 그대로 갖추고(scopedToPrevious + prior 와 같은 축 값) intent 만 general.
    general = _decompose_with(ratingMin=4.5)
    general["intent"] = "general"
    general["reply"] = "네 그럼요"
    general["scopedToPrevious"] = True
    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(message="그 중에 뭐가 제일 인기 많아?"),
                _member(),
                llm=FakeLLM(decompose=general),
                search=_rating_search(products, calls),
                push_fn=_push,
            )
        )

    # 승계할 `applied`(4.0)가 저장돼 있고 조건도 맞는데 이어받지 않는다. 읽기 자체는 칩 클릭
    # 판정 때문에 어차피 하므로(스냅샷 1회로 합쳐졌다) "승계했는가"는 `relaxation_carried` 로 본다.
    assert "relaxation_carried" not in caplog.text
    assert "products.ready" not in _types(events)  # 추천으로 납치하지 않는다


async def test_scoped_refine_without_prior_relaxation_is_a_noop() -> None:
    """완화가 없었던 스레드에서 "그 중에" 가 와도 아무것도 되살리지 않는다."""

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    calls: list = []
    products = [_product(101, 39000, rating=4.8)]
    # 턴1 — 완화 없이 정상 결과
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
            search=_rating_search(products, calls),
            push_fn=_push,
        )
    )

    scoped = _decompose_with(ratingMin=4.5)
    scoped["scopedToPrevious"] = True
    turn2 = len(calls)
    await _collect(
        run_buyer_turn(
            _req(message="그 중에 더 저렴한 걸로"),
            _member(),
            llm=FakeLLM(decompose=scoped),
            search=_rating_search(products, calls),
            push_fn=_push,
        )
    )

    assert calls[turn2] == 4.5  # 이어받을 완화가 없으니 그대로


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


@pytest.mark.parametrize("budget", [1, 2])
async def test_auto_relaxation_does_not_starve_the_chip_budget(
    budget: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[PR #248 리뷰] 자동 완화가 칩 예산을 먹어치우지 않는다.

    예산을 공유하면 `relaxation_max_probes=1` + 평점 조건인 턴에서 자동 완화가 예산을 다 써
    **가격 칩을 아예 만들어보지도 못한다.** 정작 칩은 자동 완화가 실패했을 때 쓰라고 있는
    폴백이라, 사용자는 "조건을 바꿔볼까요?"만 듣고 누를 게 없는 화면을 받는다.
    """
    monkeypatch.setattr(get_settings(), "relaxation_max_probes", budget)
    seen: list = []

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        seen.append((filters.rating_min, filters.price_max))
        # 평점은 만족하지만 가격만 초과 — **가격만 넓히면** 결과가 나오는 상황
        kept = [
            p
            for p in [_product(201, 60000, rating=4.8), _product(202, 62000, rating=4.9)]
            if (filters.rating_min is None or (p.rating or 0) >= filters.rating_min)
            and (filters.price_max is None or (p.price or 0) <= filters.price_max)
        ]
        return ProductSearchResult(products=kept, total_count=len(kept))

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            # 평점(자동 완화 대상) + 가격(칩 대상)이 **동시에** 걸린 0건 턴
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5, priceMax=50000)),
            search=_search,
            push_fn=None,
        )
    )

    chips = _suggestions(events)
    assert [c["relaxation"]["field"] for c in chips] == ["priceMax"]
    assert chips[0]["estCount"] == 2
    assert (4.0, 50000) in seen  # 자동 완화 probe 는 돌았고(실패)
    assert (4.5, 65000) in seen  # 칩 probe 도 굶지 않았다


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

        async def put(self, key, offers, applied):  # noqa: ANN001
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


# ─────────── 하드 불변식·신뢰경계 (PR #248 리뷰) ───────────


@pytest.mark.parametrize("field", ["priceMax", "brand", "color", "priceMin"])
def test_auto_relaxing_explicit_constraints_is_rejected_at_startup(field: str) -> None:
    """[REQ-REC-043 · AC-REC-08] 명시 제약을 자동 완화 목록에 넣으면 **기동이 실패**한다.

    이 하드 불변식을 지키는 게 config 기본값뿐이면 환경변수 한 줄로 꺼진다 — "5만원 이하"라고
    말한 사용자에게 6만 5천원짜리가 동의 없이 노출되는데 서버는 멀쩡히 돈다(#133 이 '고지 여부를
    튜너블로 두면 정직성이 옵션이 된다'로 막은 것과 같은 종류).
    """
    with pytest.raises(ValidationError) as exc:
        Settings(relaxation_auto_fields=["ratingMin", field])

    assert "RELAXATION_AUTO_FIELDS" in str(exc.value)
    assert field in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("priceMax", -50000), ("ratingMin", -0.5)],
    ids=["가격상한", "평점하한"],
)
async def test_out_of_range_stored_offer_never_reaches_spring(
    field: str,
    value: float,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 3차 리뷰] 손상된 저장 값의 **범위**도 스키마가 막고, 막았다는 사실이 로그에 남는다.

    타입만 보면 `{"field":"priceMax","value":-50000}` 은 멀쩡한 int 다. 스키마에 `ge=0` 이 없으면
    `model_validate` 를 통과해 그대로 Spring 쿼리 파라미터(`maxPrice=-50000`)로 나가고, 오류 없이
    **조용한 0건**으로만 드러나 원인 추적이 안 된다. 여기서 걸러 검색 자체를 안 하게 한다.
    """
    label = "손상된 칩"

    class _Corrupt:
        async def get_snapshot(self, key):  # noqa: ANN001
            return {label: {"field": field, "value": value}}, None

        async def put(self, key, offers, applied):  # noqa: ANN001
            return None

    async def _factory():
        return _Corrupt()

    monkeypatch.setattr(buyer_graph, "get_relaxation_offer_store", _factory)
    calls: list = []
    with caplog.at_level(logging.WARNING):
        await _collect(
            run_buyer_turn(
                _req(message=label),  # 칩을 누른 것처럼 label 을 그대로 보낸다
                _member(),
                llm=FakeLLM(decompose=_decompose_with(priceMax=50000)),
                search=_filtered_search([_product(101, 39000)], calls=calls),
                push_fn=None,
            )
        )

    assert calls, "검색은 정상 조건으로 수행된다(칩 적용만 거부)"
    assert all(getattr(f, "price_max", 0) >= 0 for f in calls)  # 음수가 Spring 으로 안 나간다
    assert all(getattr(f, "price_min", 0) is None or f.price_min >= 0 for f in calls)
    assert all(f.rating_min is None or f.rating_min >= 0 for f in calls)
    assert "relaxation_offer_rejected" in caplog.text  # 거부가 조용하지 않다


@pytest.mark.parametrize("attr", ["price_min", "price_max", "rating_min"])
def test_negative_numeric_filters_are_rejected_by_the_schema(attr: str) -> None:
    """수치 하한/상한은 스키마가 음수를 거부한다 — 검증을 여기 한 곳에 모은다.

    위 완화 오퍼 경로 말고도 출처가 더 있다(decompose LLM 산출·멀티턴 병합). 호출부마다 범위
    체크를 두면 스키마보다 좁거나 넓어져 조용히 어긋나므로, 스키마에서 한 번에 막는다.
    `price_min` 은 완화 대상 필드가 아니라 오퍼 경로로는 닿지 않아 여기서만 커버된다.
    """
    with pytest.raises(ValidationError):
        ProductSearchFilters.model_validate({attr: -1})
    assert getattr(ProductSearchFilters.model_validate({attr: 0}), attr) == 0  # 0 은 정상값


async def test_unreadable_thread_filters_never_escape_regardless_of_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """저장 값 해석이 **어떤 이유로** 실패해도 새어나가지 않는다 — `ValidationError` 로 좁히지 않는다.

    이 try 는 "저장된 값을 해석한다" 전체를 맡고, 그 안에는 검증 말고 모순 구간 보정 호출도 있다.
    좁혀 두면 거기서 난 다른 예외가 감싸이지 않은 호출부(`prior = await thread_store.get(...)`)로
    새어나가 스레드가 영구 broken 이 된다 — 이 가드가 애초에 막으려던 바로 그 상태다.
    """

    class _Weird:
        async def aget(self, ns, key):  # noqa: ANN001
            # 검증은 통과하지만 뒤이은 해석 단계에서 터지는 모양(스키마 변경·신구 혼재의 대역)
            return type("Item", (), {"value": {"price_min": 1, "price_max": 2}})()

    store = buyer_graph.ThreadFilterStore(_Weird())
    with pytest.MonkeyPatch.context() as mp:

        def _boom(filters, prior):  # noqa: ANN001
            raise RuntimeError("resolver boom")  # ValidationError 가 **아닌** 예외

        mp.setattr(buyer_graph, "_resolve_contradictory_price_range", _boom)
        with caplog.at_level(logging.WARNING):
            assert await store.get("t1") is None  # 터지지 않고 "이전 맥락 없음"

    assert "thread_filters_unreadable" in caplog.text


async def test_stored_contradictory_range_does_not_survive_a_chip_click() -> None:
    """저장된 모순 구간이 **칩 클릭 경로로 우회**해 Spring 에 나가지 않는다.

    `_resolve_contradictory_price_range` 는 decompose 의 **자기 산출**만 고치는데, 저장된 prior 는
    칩 클릭에서 `_relaxed_filters_from_offer` 의 base 로 **직접** 쓰여 그 보정을 건너뛴다. 이
    수정 이전에 저장된 레코드가 남아 있을 수 있으므로 저장소 경계에서 한 번 더 막는다.
    """

    class _Poisoned:
        async def aget(self, ns, key):  # noqa: ANN001
            return type("Item", (), {"value": {"price_min": 30000, "price_max": 20000}})()

        async def aput(self, ns, key, value):  # noqa: ANN001
            return None

    prior = await buyer_graph.ThreadFilterStore(_Poisoned()).get("t1")
    assert (prior.price_min, prior.price_max) == (None, 20000)  # 읽는 순간 풀린다

    clicked = buyer_graph._relaxed_filters_from_offer({"field": "priceMax", "value": 26000}, prior)
    assert clicked.price_min is None and clicked.price_max == 26000
    assert _search_query_params(clicked) == {"maxPrice": 26000}  # 모순이 안 나간다


async def test_pre_existing_negative_thread_filters_do_not_brick_the_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 3차 리뷰] `ge=0` 이 **소급 적용**돼도 옛 레코드가 스레드를 죽이지 않는다.

    이 PR 이전에는 음수가 검증을 통과해 pg-profile 에 영속될 수 있었다. `ThreadFilterStore.get()`
    은 `run_buyer_turn` 진입 직후 **decompose 보다도 먼저** 불리므로, 감싸지 않으면 그런 스레드는
    매 턴 여기서 죽고 LLM degrade 같은 정상 오류 이벤트조차 못 낸다(그 코드에 닿기 전이다).
    pg_store 에 TTL 도 없어 스스로 낫지 않는 **영구 broken** 상태가 된다.
    """
    poisoned = {"price_max": -50000, "keyword": "이어폰"}  # 옛 스키마에서는 정상이던 값

    class _Poisoned:
        async def aget(self, ns, key):  # noqa: ANN001
            return type("Item", (), {"value": poisoned})()

        async def aput(self, ns, key, value):  # noqa: ANN001
            return None

    # 스토어 계층 — 터지지 않고 "이전 맥락 없음"으로 떨어진다.
    with caplog.at_level(logging.WARNING):
        assert await buyer_graph.ThreadFilterStore(_Poisoned()).get("t1") is None
    assert "thread_filters_unreadable" in caplog.text  # 조용히 삼키지 않는다

    # 턴 계층 — 그 스레드가 정상적으로 완주한다.
    async def _push(push) -> bool:  # noqa: ANN001
        return True

    with pytest.MonkeyPatch.context() as mp:

        async def _store():
            return buyer_graph.ThreadFilterStore(_Poisoned())

        mp.setattr(buyer_graph, "get_thread_store", _store)
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_decompose_with(priceMax=50000)),
                search=_filtered_search([_product(101, 39000)]),
                push_fn=_push,
            )
        )

    assert _types(events)[-1] == "done" and "error" not in _types(events)


def test_auto_relaxation_can_be_disabled_but_not_widened() -> None:
    """목록을 **비우는 것**(자동 완화 전면 off)은 정상적인 의사표현이라 허용한다."""
    assert Settings(relaxation_auto_fields=[]).relaxation_auto_fields == []
    assert Settings(relaxation_auto_fields=["ratingMin"]).relaxation_auto_fields == ["ratingMin"]


@pytest.mark.parametrize("bad", ["pricemax", "price_max", "category", "오타"])
def test_unknown_chip_field_fails_startup_instead_of_silently_doing_nothing(bad: str) -> None:
    """[PR #248 리뷰] 완화 칩 대상 오타는 **기동 실패**로 드러낸다.

    후보 생성기가 모르는 이름을 `continue` 로 건너뛰므로, 오타 하나면 기동은 멀쩡히 성공하고
    그 필드의 완화 칩만 영구히 안 나오는데 아무도 이유를 모른다 — 형제 설정
    (`relaxation_auto_fields`)은 기동 시점에 검증하는데 이쪽만 조용히 무해화되던 비대칭이다.
    """
    with pytest.raises(ValidationError) as exc:
        Settings(relaxation_chip_fields=["priceMax", bad])

    assert "RELAXATION_CHIP_FIELDS" in str(exc.value)
    assert bad in str(exc.value)


def test_auto_fields_must_be_a_subset_of_chip_fields() -> None:
    """[PR #248 리뷰] 자동 목록이 칩 목록 밖이면 기동 실패.

    후보는 `relaxation_chip_fields` 를 순회해서만 만들어지므로, 칩 목록에서 빠진 필드는 자동
    목록에 있어도 후보 자체가 안 생겨 **자동 완화가 조용히 영구 비활성화**된다. 두 값이 개별로는
    유효해 기동은 성공하고, `may_auto_relax` 는 자동 목록만 보므로 매 턴 conditions 만 헛되이
    지연된다 — 설정 **조합**을 막아야 잡힌다.
    """
    with pytest.raises(ValidationError) as exc:
        Settings(relaxation_chip_fields=["priceMax"], relaxation_auto_fields=["ratingMin"])
    assert "RELAXATION_CHIP_FIELDS" in str(exc.value)

    # 부분집합이면 통과하고, 자동을 비우는 것도 정상이다.
    assert Settings(
        relaxation_chip_fields=["priceMax", "ratingMin"], relaxation_auto_fields=["ratingMin"]
    ).relaxation_auto_fields == ["ratingMin"]
    assert (
        Settings(
            relaxation_chip_fields=["priceMax"], relaxation_auto_fields=[]
        ).relaxation_auto_fields
        == []
    )


async def test_chips_after_auto_relaxation_use_the_relaxed_basis() -> None:
    """[PR #248 리뷰] 자동 완화가 채택되면 **완화 칩 후보도 그 값 기준**으로 만든다.

    원본 기준으로 만들면 화면엔 "평점 4.0" 이 떠 있는데 그 옆 가격 칩은 4.5 로 probe 돼,
    표시-실제가 어긋날 뿐 아니라 4.5 로 재보면 0건이라 **칩이 통째로 사라진다**.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    seen: list = []

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        seen.append((filters.rating_min, filters.price_max))
        kept = [
            p
            for p in [_product(101, 45000, rating=4.2), _product(201, 60000, rating=4.3)]
            if (filters.rating_min is None or (p.rating or 0) >= filters.rating_min)
            and (filters.price_max is None or (p.price or 0) <= filters.price_max)
        ]
        return ProductSearchResult(products=kept, total_count=len(kept))

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5, priceMax=50000)),
            search=_search,
            push_fn=_push,
        )
    )

    assert (4.0, 50000) in seen  # 자동 완화 채택
    assert (4.0, 65000) in seen  # 칩 probe 도 **완화된 평점** 기준
    assert (4.5, 65000) not in seen  # 완화 전 기준으로 재보지 않는다
    chips = _suggestions(events)
    assert [c["relaxation"]["field"] for c in chips] == ["priceMax"]
    assert chips[0]["estCount"] == 2  # 사라지지 않는다
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.0  # 조건 칩과 칩 probe 기준이 같다


def test_chip_fields_can_be_emptied_only_together_with_auto_fields() -> None:
    """완화 기능 전면 off 는 **두 목록을 함께** 비워야 한다.

    칩 목록만 비우면 후보 생성기가 아무것도 못 만들어 자동 완화까지 조용히 죽는다 — 부분집합
    검증이 그 조합을 기동 시점에 막으므로, 끄려면 의도를 명시적으로 적어야 한다.
    """
    off = Settings(relaxation_chip_fields=[], relaxation_auto_fields=[])
    assert off.relaxation_chip_fields == [] and off.relaxation_auto_fields == []

    # 칩만 비우면 기본 자동 목록(["ratingMin"])이 고아가 되므로 거부한다.
    with pytest.raises(ValidationError):
        Settings(relaxation_chip_fields=[])


async def test_probe_failure_after_search_does_not_kill_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 리뷰] probe 가 **검색 이후 단계**에서 터져도 추천 턴은 살아남는다.

    `_run_search` 호출만 감쌌을 때는 그 뒤(병합·사후필터)에서 난 예외가
    `asyncio.gather`(return_exceptions 없음)로 그대로 전파돼, **칩 하나 만들려던 부가 조회가
    SSE 스트림 전체를 죽였다.** 여기서는 완화 probe 응답만 후처리에서 터지는 객체로 돌려준다.

    **fan-out 이 아닌 턴으로 본다**(2차 리뷰) — `categoryQueries` 가 있으면 같은 응답이
    `_merge_fanout_results` 가드에 **먼저** 걸려(`search_merge_failed`) probe 가드까지 오지 않는다.
    그러면 이 테스트는 probe 가드를 지워도 통과하는 빈 테스트가 된다. leg 이 없으면 병합 단계가
    없어 `_post_filter` 에서 터지고, 그게 이 가드가 실제로 맡은 자리다.
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    no_fanout = json.loads(json.dumps(DEFAULT_DECOMPOSE))
    no_fanout["categoryQueries"] = []

    class _BrokenResult:
        """후처리(`.products` 접근) 시점에 터지는 응답 — 검색 호출 자체는 성공한 상황."""

        total_count = 0

        @property
        def products(self):  # noqa: ANN201
            raise RuntimeError("post-search boom")

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        if filters.price_max == 65000:  # 완화 probe 만 망가진 응답을 받는다
            return _BrokenResult()
        return ProductSearchResult(products=[_product(101, 39000)], total_count=1)

    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=no_fanout),
                search=_search,
                push_fn=_push,
            )
        )

    assert _types(events)[-1] == "done"
    assert "error" not in _types(events)
    assert "products.ready" in _types(events)  # 본 경로는 멀쩡히 완주한다
    assert _suggestions(events) == []  # 터진 후보의 칩만 빠진다
    assert "relaxation_probe_failed" in caplog.text  # probe 가드가 잡았다(병합 가드가 아니라)


@pytest.mark.parametrize(
    "stored",
    [
        "그냥 문자열",  # 구 스키마 — dict 가 아님
        {"field": "priceMax"},  # value 키 누락 — None(조건 해제)으로 뭉개면 검색이 더 넓어진다
        {"field": "categoryQueries", "value": "x"},  # 완화 대상 아닌 필드
        {"field": "priceMax", "value": True},  # bool 은 int 서브클래스 — 상한 1 로 둔갑
        {"field": "priceMax", "value": {"nested": 1}},  # 스칼라 아님
        {"field": "priceMax", "value": "비싼거"},  # 숫자로 강제 변환 불가
    ],
)
async def test_corrupt_stored_offer_is_ignored_not_crashed(stored) -> None:  # noqa: ANN001
    """저장소는 신뢰 경계 밖 — 값이 망가져 있어도 턴을 죽이지 않고 조용히 무시한다.

    읽기만 감싸고 해석을 밖에 두면 `AttributeError` 로 턴 전체가 죽는다(PR #248 리뷰).
    """

    async def _push(push) -> bool:  # noqa: ANN001
        return True

    class _Store:
        async def get(self, key):  # noqa: ANN001
            return {"눌린 칩": stored}

        async def put(self, key, offers, applied):  # noqa: ANN001
            return None

    async def _fake():
        return _Store()

    calls: list[ProductSearchFilters] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(buyer_graph, "get_relaxation_offer_store", _fake)
        events = await _collect(
            run_buyer_turn(
                _req(message="눌린 칩"),
                _member(),
                llm=FakeLLM(),
                search=_filtered_search([_product(101, 39000)], calls=calls),
                push_fn=_push,
            )
        )

    assert _types(events)[-1] == "done"
    assert "error" not in _types(events)
    # 망가진 값이 필터로 새지 않는다 — decompose 산출(50,000)이 그대로 쓰인다.
    assert calls[0].price_max == 50000


def test_offer_value_validation_delegates_to_schema_not_a_narrower_allowlist() -> None:
    """[PR #248 2차 리뷰] 값 검증은 스키마에 맡긴다 — 사전 목록이 스키마보다 좁으면 안 된다.

    `brand: list[str] | None` 처럼 리스트를 받는 필드가 있으므로, 스칼라만 허용하는 사전 목록을
    두면 brand 완화를 "일부 브랜드만 남기기"로 확장하는 순간 칩 클릭이 영구 무동작이 된다.
    **단 `bool` 만은 예외** — Pydantic 이 `price_max=True` 를 거부하지 않고 `1` 로 강제 변환해
    "가격 상한 1원"으로 둔갑시키므로 스키마가 못 잡는 유일한 케이스다(실측).
    """
    base = ProductSearchFilters(price_max=50000, category="무선이어폰")

    # 리스트 값도 스키마가 받으면 통과해야 한다(향후 brand 완화 확장 대비).
    listed = buyer_graph._relaxed_filters_from_offer({"field": "brand", "value": ["A", "B"]}, base)
    assert listed is not None and listed.brand == ["A", "B"]

    # bool 은 스키마가 1 로 삼켜버리므로 여기서 막는다.
    assert (
        buyer_graph._relaxed_filters_from_offer({"field": "priceMax", "value": True}, base) is None
    )
    # 스키마가 거부하는 값은 그대로 폐기된다(사전 목록 없이도).
    assert (
        buyer_graph._relaxed_filters_from_offer({"field": "brand", "value": "단일문자열"}, base)
        is None
    )
    assert (
        buyer_graph._relaxed_filters_from_offer({"field": "priceMax", "value": [1, 2]}, base)
        is None
    )


async def test_conditions_survive_auto_relaxation_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[PR #248 2차 리뷰] 자동 완화 구간이 터져도 미룬 conditions 는 반드시 나간다.

    미루기 전에는 conditions 가 검색 **전** 순수 계산이라 실패할 일이 없었다. 미룬 뒤로는 그
    사이에서 예외가 나면 조건 칩이 통째로 사라지므로, 이 구간을 감싸 발신을 보장한다.
    """

    def _boom(filters, settings):  # noqa: ANN001
        raise RuntimeError("candidate build boom")

    monkeypatch.setattr(recommendation_graph, "build_relaxation_candidates", _boom)
    with caplog.at_level(logging.WARNING):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),  # 미루는 턴
                search=_filtered_search([]),  # 0건 → 자동 완화 진입
                push_fn=None,
            )
        )

    types = _types(events)
    assert types.count("conditions") == 1  # 조건 칩이 사라지지 않는다
    assert types[-1] == "done"  # 완화 실패는 턴을 죽이지 않는다
    assert "error" not in types
    # 자동 완화 루프와 완화 칩 조립 **양쪽** 이 같은 후보 생성기를 쓰므로 둘 다 흡수돼야 한다.
    assert "relaxation_auto_failed" in caplog.text
    assert "relaxation_chips_failed" in caplog.text
    rating_chip = next(c for c in _conditions(events) if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.5  # 완화가 채택되지 않았으므로 원래 값


async def test_stored_offer_rejects_wrong_typed_value_from_store_roundtrip() -> None:
    """저장소 왕복으로 타입이 어긋나 돌아오면 적용하지 않는다 — model_copy 는 검증을 건너뛴다."""
    base = ProductSearchFilters(price_max=50000, category="무선이어폰")

    assert buyer_graph._relaxed_filters_from_offer({"field": "priceMax", "value": 65000}, base)
    assert buyer_graph._relaxed_filters_from_offer({"field": "brand", "value": None}, base)
    # 문자열 숫자는 Pydantic 이 강제 변환하므로 통과하되 **타입이 정규화**돼야 한다.
    coerced = buyer_graph._relaxed_filters_from_offer({"field": "priceMax", "value": "65000"}, base)
    assert coerced is not None and coerced.price_max == 65000
    # 검증 불가 값은 조용히 폐기.
    assert (
        buyer_graph._relaxed_filters_from_offer({"field": "priceMax", "value": "비싼거"}, base)
        is None
    )
    assert buyer_graph._relaxed_filters_from_offer(None, base) is None


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
