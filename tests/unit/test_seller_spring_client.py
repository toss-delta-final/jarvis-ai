"""SpringClient(판매자 조회/쓰기) 단위 테스트 — httpx.MockTransport (respx 미설치, §0).

DESIGN-SELLER-TOOLS-STAGE1 §6 테스트 목록. 실 네트워크 없이 요청 URL·헤더·쿼리·바디와
응답 파싱, 오류 매핑(SpringUnavailableError)을 검증한다.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import get_settings
from app.schemas.spring import ChurnMember, ProductCreate, ProductUpdate
from app.services.spring_client import SpringClient, SpringUnavailableError

BASE_URL = "http://spring.internal.test"
TOKEN = "svc-token-123"


def _client(
    handler, *, internal_token: str | None = TOKEN, timeout: float | None = None
) -> SpringClient:
    transport = httpx.MockTransport(handler)
    return SpringClient(BASE_URL, internal_token, transport=transport, timeout=timeout)


async def test_get_sales_hits_brand_path_with_internal_token() -> None:
    """요청 URL이 /internal/seller/{brandId}/sales, X-Internal-Token 헤더, from/to/granularity 쿼리."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"series": []})

    client = _client(handler)
    await client.get_sales("brand-1", "2026-07-01", "2026-07-14", "daily")

    assert "/internal/seller/brand-1/sales" in captured["url"]
    assert captured["headers"]["X-Internal-Token"] == TOKEN
    assert "from=2026-07-01" in captured["url"]
    assert "to=2026-07-14" in captured["url"]
    assert "granularity=daily" in captured["url"]


async def test_get_sales_parses_camel_response() -> None:
    """camelCase 응답(series[{date,sales,orderCount,isAnomaly,deviationPct}]) → SalesResult 파싱."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "series": [
                    {
                        "date": "2026-07-01",
                        "sales": 10000,
                        "orderCount": 5,
                        "isAnomaly": False,
                        "deviationPct": 1.2,
                    }
                ]
            },
        )

    client = _client(handler)
    result = await client.get_sales("brand-1", "2026-07-01", "2026-07-01")

    assert len(result.series) == 1
    point = result.series[0]
    assert point.date == "2026-07-01"
    assert point.sales == 10000
    assert point.order_count == 5
    assert point.is_anomaly is False
    assert point.deviation_pct == 1.2


async def test_get_sales_accepts_null_deviation_pct() -> None:
    """[회귀 2026-07-30] 실 Spring(SellerSalesService)은 이동평균 구간 미달 포인트에
    deviationPct=null 을 내려보낸다 — float 고정 스키마가 ValidationError 를 내며
    매출 조회 전체가 '응답 형식 오류' degrade 로 실패하던 버그."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "brandId": 93,
                    "granularity": "daily",
                    "series": [
                        {
                            "date": "2026-06-01",
                            "sales": 120000,
                            "orderCount": 4,
                            "salesCount": 5,
                            "isAnomaly": False,
                            "deviationPct": None,
                        },
                        {
                            "date": "2026-06-08",
                            "sales": 30000,
                            "orderCount": 1,
                            "salesCount": 1,
                            "isAnomaly": True,
                            "deviationPct": -71.4,
                        },
                    ],
                    "config": {"windowDays": 7, "deviationPct": 30.0},
                },
            },
        )

    client = _client(handler)
    result = await client.get_sales("93", "2026-06-01", "2026-06-30")

    assert [p.deviation_pct for p in result.series] == [None, -71.4]
    assert result.series[0].sales == 120000


async def test_get_funnel_flattens_stages_response() -> None:
    """[회귀 2026-07-30] 실 Spring(SellerFunnelResponse)은 stages[] 형태 — 종전 평면
    스키마는 기본값 0 으로 조용히 통과해 전 단계 0 퍼널로 오분석되던 문제.
    checkout v1 미계산 구간(count=null)은 0 으로 수렴한다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "brandId": 93,
                    "from": "2026-06-01",
                    "to": "2026-06-30",
                    "stages": [
                        {"stage": "product_view", "count": 5000, "source": "events"},
                        {"stage": "add_to_cart", "count": 800, "source": "events"},
                        {
                            "stage": "checkout_start",
                            "count": None,
                            "source": "events",
                            "computable": False,
                        },
                        {"stage": "purchase_complete", "count": 120, "source": "orders"},
                    ],
                    "conversionRates": {"viewToCart": 0.16, "overall": 0.024},
                },
            },
        )

    client = _client(handler)
    result = await client.get_funnel("93", "2026-06-01", "2026-06-30")

    assert (result.view, result.cart, result.checkout, result.purchase) == (5000, 800, 0, 120)


async def test_account_events_uses_brand_scoped_path_and_sends_period() -> None:
    """[#481] I-8은 브랜드 스코프 /internal/seller/{brandId}/account-events 를 탄다
    (2026-08-06 자사 코호트 전환 — 구 전역 /internal/account-events 는 admin 존치분).

    from/to 는 Spring AnalysisPeriod.of 필수 — 종전 optional 미전달 호출은 400 이었다.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"groupBy": "eventType", "rows": []})

    client = _client(handler)
    await client.get_account_events(93, "2026-07-01", "2026-07-31", event_type="LOGIN_FAIL")

    assert captured["url"].startswith(f"{BASE_URL}/internal/seller/93/account-events")
    assert captured["params"]["from"] == "2026-07-01"
    assert captured["params"]["to"] == "2026-07-31"
    assert captured["params"]["eventType"] == "LOGIN_FAIL"


async def test_account_events_parses_rows_and_group_by_echo() -> None:
    """I-8 실측 응답(rows — groupBy 별 이형) 파싱(#197 — 구 events 스키마는 상시 0건).

    [#481] 2026-08-06 개정 응답 — ip IpRow 는 suspiciousMemberCount·eventCount
    (failCount·isSuspicious·nullMemberRatio 제거), scope="brand" 에코가 실린다.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "groupBy": "ip",
                    "scope": "brand",
                    "eventType": "LOGIN_FAIL",
                    "from": "2026-07-01",
                    "to": "2026-07-31",
                    "rows": [
                        {
                            "ipMasked": "121.140.xx.xx",
                            "distinctMembers": 7,
                            "suspiciousMemberCount": 5,
                            "eventCount": 87,
                            "firstSeen": "2026-07-30T01:00:00+09:00",
                            "lastSeen": "2026-07-30T02:00:00+09:00",
                        }
                    ],
                },
            },
        )

    client = _client(handler)
    result = await client.get_account_events(93, "2026-07-01", "2026-07-31", group_by="ip")

    assert result.group_by == "ip"
    assert result.scope == "brand"  # 구버전(전역) 응답 오독 차단 에코
    assert len(result.rows) == 1
    assert result.rows[0]["suspiciousMemberCount"] == 5
    assert "failCount" not in result.rows[0]  # 2026-08-06 개정으로 제거된 필드
    assert "isSuspicious" not in result.rows[0]


async def test_get_churn_sends_period_and_inactive_days() -> None:
    """I-16 쿼리에 from/to/inactiveDays 3종이 실린다(#197 회귀 방지).

    구 코드는 inactiveDays 만 보내 AnalysisPeriod.of 가 400 INVALID_PERIOD 로
    거부 — 이탈 코호트 조회가 무조건 실패(주 소스 전면 불능)하던 원인.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "cohortSize": 0,
                "churnRate": 0.0,
                "preChurnSignals": {
                    "cancelCount": 0,
                    "returnReasonsTop": [],
                    "zeroResultSearchSessions": 0,
                    "priceIncreaseExposed": 0,
                },
                "members": [],
            },
        )

    client = _client(handler)
    await client.get_churn("93", "2026-06-01", "2026-07-31", 45)

    assert captured["params"] == {"from": "2026-06-01", "to": "2026-07-31", "inactiveDays": "45"}


async def test_get_churn_parses_measured_response_shape() -> None:
    """I-16 실측 응답(SellerChurnResponse) 파싱(#197 — 구 list[dict] 스키마는
    preChurnSignals 객체에 ValidationError → degrade 로 새던 원인).

    churnRate 는 fraction 그대로 보존(0.6 — % 변환은 도구 표시 계층 소관),
    sessions30d 카멜 alias 매핑까지 고정한다.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "brandId": 93,
                    "from": "2026-06-01",
                    "to": "2026-07-31",
                    "inactiveDays": 30,
                    "cohortSize": 5,
                    "churnRate": 0.6,
                    "preChurnSignals": {
                        "cancelCount": 3,
                        "returnReasonsTop": [{"reason": "사이즈 불만", "count": 2}],
                        "zeroResultSearchSessions": 0,
                        "priceIncreaseExposed": 2,
                    },
                    "members": [
                        {
                            "customerLabel": "A3F29C",
                            "lastActivityAt": "2026-06-15T10:00:00+09:00",
                            "sessions30d": 0,
                            "preChurnEvent": "RETURNED(상품불량)",
                        }
                    ],
                },
            },
        )

    client = _client(handler)
    result = await client.get_churn("93", "2026-06-01", "2026-07-31", 30)

    assert result.churn_rate == 0.6  # fraction 보존 — 스키마에서 ×100 하지 않는다
    assert result.cohort_size == 5
    assert result.pre_churn_signals is not None
    assert result.pre_churn_signals.cancel_count == 3
    assert result.pre_churn_signals.return_reasons_top == [{"reason": "사이즈 불만", "count": 2}]
    assert result.pre_churn_signals.price_increase_exposed == 2
    assert len(result.members) == 1
    member = result.members[0]
    assert member.customer_label == "A3F29C"  # [#487] 구 memberId(Long) 대체
    assert member.sessions_30d == 0  # camel alias "sessions30d" 매핑 고정
    assert member.pre_churn_event == "RETURNED(상품불량)"


async def test_churn_member_drops_raw_identifier_fields() -> None:
    """[#487] ChurnMember 는 memberId·lastLoginAt 을 필드로 갖지 않는다.

    구응답이 와도 extra="allow" 로 흡수돼 파싱은 계속 성공하지만(배포 순서 무관),
    model_extra 에 남을 뿐 속성으로는 읽히지 않는다 — 표시 계층이 실수로도 원시
    회원 키를 집어들 수 없게 하는 것이 이 단언의 목적이다.
    """
    assert "member_id" not in ChurnMember.model_fields
    assert "last_login_at" not in ChurnMember.model_fields
    assert "customer_label" in ChurnMember.model_fields

    legacy = ChurnMember.model_validate(
        {"memberId": 103, "lastLoginAt": "2026-06-10T10:00:00+09:00", "sessions30d": 0}
    )

    assert legacy.customer_label is None  # 라벨 미수신 — memberId 로 대체되지 않는다
    assert not hasattr(legacy, "member_id")
    assert legacy.model_extra.get("memberId") == 103  # 흡수는 되되 표면엔 안 나온다


async def test_get_churn_missing_rate_parses_as_none_not_zero() -> None:
    """[#197 PR 리뷰] churnRate 키 결측은 0.0 이 아니라 None 으로 남는다.

    기본값 0.0 이면 BE 필드 누락이 조용히 "이탈률 0.0%"로 렌더링된다 — 이 PR 이
    제거한 silent-mismatch 패턴을 이 필드만 재도입하지 않게 스키마 기본값을 막는다.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "cohortSize": 5,
                "preChurnSignals": {
                    "cancelCount": 0,
                    "returnReasonsTop": [],
                    "zeroResultSearchSessions": 0,
                    "priceIncreaseExposed": 0,
                },
                "members": [],
            },
        )

    client = _client(handler)
    result = await client.get_churn("93", "2026-06-01", "2026-07-31", 30)

    assert result.churn_rate is None  # 0.0 위장 금지 — 표시 계층이 미수신으로 처리


async def test_get_churn_signals_shape_drift_maps_to_spring_unavailable() -> None:
    """preChurnSignals 가 다시 배열 등으로 drift 하면 조용한 통과가 아니라
    SpringUnavailableError 로 관측된다(_validate 단일 지점, #197)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"cohortSize": 5, "churnRate": 0.6, "preChurnSignals": [{"x": 1}]}
        )

    client = _client(handler)
    with pytest.raises(SpringUnavailableError):
        await client.get_churn("93", "2026-06-01", "2026-07-31", 30)


async def test_create_product_posts_body_by_alias() -> None:
    """POST 바디가 camelCase(stockQuantity 등), 201 파싱."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"productId": 101, "status": "ON_SALE"})

    client = _client(handler)
    payload = ProductCreate(name="여행용 파우치", price=10000, stock_quantity=5)
    result = await client.create_product("brand-1", payload)

    assert captured["method"] == "POST"
    assert captured["body"]["stockQuantity"] == 5
    assert captured["body"]["name"] == "여행용 파우치"
    assert result.product_id == 101
    assert result.status == "ON_SALE"


async def test_create_product_excludes_unset_optional_fields() -> None:
    """미설정 옵션 필드(originalPrice/category/description/imageUrl)는 바디에서 아예 빠진다.

    (opus 리뷰 m4 — exclude_none 누락 시 null 로 전송되던 문제)
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"productId": 101, "status": "ON_SALE"})

    client = _client(handler)
    payload = ProductCreate(name="여행용 파우치", price=10000, stock_quantity=5)
    await client.create_product("brand-1", payload)

    assert captured["body"] == {"name": "여행용 파우치", "price": 10000, "stockQuantity": 5}
    for absent_key in ("originalPrice", "category", "description", "imageUrl"):
        assert absent_key not in captured["body"]


async def test_update_product_uses_patch_and_product_path() -> None:
    """PATCH /…/products/{productId}, 바꿀 필드만 전송."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"productId": 101})

    client = _client(handler)
    patch = ProductUpdate(price=9000)
    await client.update_product("brand-1", 101, patch)

    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/internal/seller/brand-1/products/101")
    # 바꿀 필드(price)만 전송 — 미지정 필드(name 등)는 바디에 없어야 한다.
    assert captured["body"] == {"price": 9000}


async def test_delete_product_uses_delete_and_returns_hidden() -> None:
    """DELETE, 응답 status=HIDDEN."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        return httpx.Response(200, json={"productId": 101, "status": "HIDDEN"})

    client = _client(handler)
    result = await client.delete_product("brand-1", 101)

    assert captured["method"] == "DELETE"
    assert result.status == "HIDDEN"
    assert result.product_id == 101


async def test_timeout_maps_to_spring_unavailable() -> None:
    """httpx.TimeoutException → SpringUnavailableError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    client = _client(handler)
    with pytest.raises(SpringUnavailableError):
        await client.get_sales("brand-1", "2026-07-01", "2026-07-14")


async def test_4xx_maps_to_spring_unavailable() -> None:
    """404/500 등 오류 응답 → SpringUnavailableError."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND"})

    client = _client(handler)
    with pytest.raises(SpringUnavailableError):
        await client.get_sales("brand-1", "2026-07-01", "2026-07-14")


async def test_client_uses_configured_timeout_3s() -> None:
    """timeout 미지정 시 settings.spring_timeout_s(기본 3.0) 를 사용한다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"series": []})

    client = _client(handler, timeout=None)
    assert client._timeout == get_settings().spring_timeout_s
    assert client._timeout == 3.0


async def test_invalid_response_schema_maps_to_spring_unavailable() -> None:
    """필수 필드 누락 응답(pydantic ValidationError) → SpringUnavailableError (opus 리뷰 M1).

    스키마가 전부 초안(🔴 C-13/C-14)이라 실측 응답과 어긋날 수 있다 — model_validate 실패도
    타임아웃/4xx 와 동일하게 단일 지점(_validate)에서 SpringUnavailableError 로 변환되어야
    도구 계층이 raise 없이 "Error: ..." 문자열로 degrade 할 수 있다.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        # SalesSeriesPoint 는 sales/orderCount 가 필수 — date 만 있는 응답은 스키마 위반.
        return httpx.Response(200, json={"series": [{"date": "2026-07-01"}]})

    client = _client(handler)
    with pytest.raises(SpringUnavailableError):
        await client.get_sales("brand-1", "2026-07-01", "2026-07-01")


async def test_get_funnel_parses_spring_stages_payload() -> None:
    """[#194 리뷰] I-7 BE 실측 페이로드(stages[] snake_case)를 평면 4필드로 정확히 변환한다.

    stage 어휘는 snake_case — BE SellerAnalyticsService.funnel 실측
    ("product_view"/"add_to_cart"/"checkout_start"/"purchase_complete", I-13 counts 의
    camelCase 와 다름). 이 casing 을 와이어 형태로 검증하는 테스트가 없어 매핑이
    어긋나도 전 단계 0 으로 새는 것을 잡지 못했다(회귀 방지)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "brandId": 42,
                "stages": [
                    {"stage": "product_view", "count": 1000, "source": "events"},
                    {"stage": "add_to_cart", "count": 100, "source": "events"},
                    {
                        "stage": "checkout_start",
                        "count": 50,
                        "source": "events",
                        "computable": True,
                    },
                    {"stage": "purchase_complete", "count": 40, "source": "orders"},
                ],
                "conversionRates": {"viewToCart": 0.1},
            },
        )

    client = _client(handler)
    result = await client.get_funnel("brand-1", "2026-07-01", "2026-07-14")

    assert (result.view, result.cart, result.checkout, result.purchase) == (1000, 100, 50, 40)
    assert result.uncomputable_stages == []


async def test_get_funnel_unknown_stage_vocabulary_surfaces_as_uncomputable() -> None:
    """[#194 리뷰] 매핑에 없는 stage 어휘(casing 변경·오탈자 등)는 조용히 0 으로 새지 않고
    해당 단계를 미집계로 편입한다 — I-14/I-15 와 동일한 '조용한 빈 값' 은폐 패턴 차단."""

    def handler(_request: httpx.Request) -> httpx.Response:
        # BE 가 어휘를 camelCase 로 바꿨다고 가정한 오염 페이로드.
        return httpx.Response(
            200,
            json={
                "stages": [
                    {"stage": "productView", "count": 1000},
                    {"stage": "addToCart", "count": 100},
                    {"stage": "checkout_start", "count": 50},
                    {"stage": "purchaseComplete", "count": 40},
                ]
            },
        )

    client = _client(handler)
    result = await client.get_funnel("brand-1", "2026-07-01", "2026-07-14")

    # 매핑된 단계(checkout)만 값이 실리고, 나머지는 0 이 아니라 "미집계"다.
    assert result.checkout == 50
    assert set(result.uncomputable_stages) == {"view", "cart", "purchase"}
    # 하류 전환율도 0% 가 아니라 판정 불가(None)여야 한다.
    from app.agents.seller import calc

    rates = calc.conversion_rates(result)
    assert rates["view_to_cart"] is None
    assert rates["cart_to_checkout"] is None


async def test_get_funnel_duplicate_stage_last_entry_wins() -> None:
    """[#194 리뷰 2] 동일 stage 중복(BE 이상 응답) 시 값·미집계 판정 모두 마지막 항목
    기준 — 값은 덮이는데 앞 항목의 미집계 판정만 남아 '값은 정상인데 미집계 표시'가
    되는 불일치 방지."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stages": [
                    {"stage": "product_view", "count": 1000},
                    {"stage": "add_to_cart", "count": 100},
                    # checkout 중복: 앞 항목은 미집계(count null) → 뒤 유효 항목이 승리해야 함.
                    {"stage": "checkout_start", "count": None, "computable": False},
                    {"stage": "checkout_start", "count": 50, "computable": True},
                    # purchase 중복(역순): 유효 → 미집계. 마지막 기준이므로 미집계로 남아야 함.
                    {"stage": "purchase_complete", "count": 40},
                    {"stage": "purchase_complete", "count": None},
                ]
            },
        )

    client = _client(handler)
    result = await client.get_funnel("brand-1", "2026-07-01", "2026-07-14")

    assert result.checkout == 50
    assert "checkout" not in result.uncomputable_stages  # 유효값으로 확정 — 미집계 아님
    assert "purchase" in result.uncomputable_stages  # 마지막 항목이 미집계


async def test_get_order_events_passes_stats_param() -> None:
    """stats 플래그가 쿼리 파라미터로 전달된다(opus 리뷰 m6, api-spec §4.4 stats)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"byStatus": {"PAID": 1}, "cancelReasonsTop": []})

    client = _client(handler)
    await client.get_order_events("brand-1", "2026-07-01", "2026-07-14", stats=True)

    assert "stats=true" in captured["url"]


async def test_get_order_events_parses_rows_total_and_stats_shape() -> None:
    """[#194] I-14 BE 실측 응답(rows/total·byStatus/cancelReasonsTop)을 필드 유실 없이
    파싱한다 — 구 스키마(events/stats)는 extra="allow" 탓에 rows 가 통째로 버려져
    도구가 항상 '주문 상태 전이 0건'을 반환했다(회귀 방지)."""

    def list_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "brandId": 42,
                "rows": [
                    {
                        "orderId": 5001,
                        "orderItemId": None,  # 주문 상태 전이는 null(2026-08-06 개정)
                        "fromStatus": "PAID",
                        "toStatus": "CANCELLED",
                        "actorType": "USER",
                        "reason": "단순변심",
                        "customerLabel": "A3F29C",  # 구 buyerMemberId(Long) 대체
                        "createdAt": "2026-07-10T09:00:00+09:00",
                    }
                ],
                "total": 130,
            },
        )

    client = _client(list_handler)
    result = await client.get_order_events("brand-1", "2026-07-01", "2026-07-14")
    assert len(result.rows) == 1
    assert result.rows[0]["toStatus"] == "CANCELLED"
    assert result.total == 130
    assert result.by_status is None  # 목록 모드에선 byStatus 부재(NON_NULL)

    def stats_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "byStatus": {"PAID": 120, "CANCELLED": 7},
                "cancelReasonsTop": [{"reason": "단순변심", "count": 4}],
            },
        )

    client = _client(stats_handler)
    result = await client.get_order_events("brand-1", "2026-07-01", "2026-07-14", stats=True)
    assert result.by_status == {"PAID": 120, "CANCELLED": 7}
    assert result.cancel_reasons_top == [{"reason": "단순변심", "count": 4}]
    assert result.rows == []  # stats 모드에선 rows 부재


async def test_get_product_changes_parses_rows_and_total() -> None:
    """[#194] I-15 BE 실측 응답(rows/total)을 타입 모델로 파싱한다 — 구 스키마(logs)는
    rows 를 통째로 버려 항상 0건이었다. oldValue/newValue 는 문자열(숫자도 문자열)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "brandId": 42,
                "rows": [
                    {
                        "productId": 101,
                        "productName": "린넨 셔츠",
                        "changeType": "STOCK",
                        "oldValue": "10",
                        "newValue": "0",
                        "createdAt": "2026-07-10T09:00:00+09:00",
                    }
                ],
                "total": 8,
            },
        )

    client = _client(handler)
    result = await client.get_product_changes("brand-1", "2026-07-01", "2026-07-14")

    assert result.total == 8
    row = result.rows[0]
    assert row.product_id == 101
    assert row.change_type == "STOCK"
    assert row.new_value == "0"  # 품절 신호 = STOCK newValue "0" (문자열)


# ── I-13 행동 이벤트 (REALIGN ②-3 — 07/17 확정 명세) ──


async def test_get_events_serializes_filters_as_query() -> None:
    """[#196] eventType 복수는 CSV 1개 파라미터로 직렬화 — 반복 쿼리 금지.

    BE 계약은 String eventType + comma split(api-spec §4.4 I-13 v0.17.4) —
    구 반복 쿼리(eventType=a&eventType=b)는 Spring 암묵 변환 의존이었다.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"success": True, "data": {"groupBy": "date", "series": []}}
        )

    client = _client(handler)
    await client.get_events(
        "brand-1",
        "2026-07-01",
        "2026-07-14",
        event_type=["product_view", "add_to_cart"],
        product_id=101,
        group_by="date",
    )

    url = captured["url"]
    assert "/internal/seller/brand-1/events" in url
    assert "eventType=product_view%2Cadd_to_cart" in url  # CSV(콤마 URL 인코딩) 1회
    assert url.count("eventType=") == 1  # 반복 쿼리로 새지 않는다
    assert "productId=101" in url
    assert "groupBy=date" in url


async def test_get_events_unwraps_success_envelope_and_parses_rows() -> None:
    """{success,data} 봉투를 벗기고 groupBy=product rows 를 파싱한다(숫자 productId)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "groupBy": "product",
                    "rows": [
                        {
                            "productId": 101,
                            "productName": "에어 러너 2",
                            "counts": {
                                "productView": 1820,
                                "addToCart": 240,
                                "checkoutStart": 96,
                                "purchaseComplete": 61,
                            },
                            "viewToCartRate": 0.132,
                            "uniqueVisitors": 1503,
                        }
                    ],
                    "total": 17,
                },
            },
        )

    client = _client(handler)
    result = await client.get_events("brand-1", "2026-07-01", "2026-07-14")

    assert result.group_by == "product"
    assert result.total == 17
    row = result.rows[0]
    assert row.product_id == 101
    assert row.counts["productView"] == 1820
    assert row.view_to_cart_rate == 0.132
    assert row.unique_visitors == 1503


async def test_get_events_null_rate_and_event_type_shape() -> None:
    """viewToCartRate null 수용 + groupBy=eventType 은 counts 만 채워진다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"groupBy": "eventType", "counts": {"productView": 8120}},
            },
        )

    client = _client(handler)
    result = await client.get_events("brand-1", "2026-07-01", "2026-07-14", group_by="eventType")

    assert result.counts == {"productView": 8120}
    assert result.rows == [] and result.series == []


async def test_non_envelope_response_passes_through() -> None:
    """봉투 없는 응답(과도기·타 API)은 그대로 파싱된다 — 하위 호환."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"groupBy": "product", "rows": [], "total": 0})

    client = _client(handler)
    result = await client.get_events("brand-1", "2026-07-01", "2026-07-14")

    assert result.total == 0


# ── I-29 자사 주문 조회 (§4.18, 이슈 #297 — 🔶 초안) ──────────────────────────────


async def test_get_orders_url_params_and_parsing() -> None:
    """URL /internal/seller/{brandId}/orders + 선택 쿼리 조건부 전송 + 응답 파싱."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "tabCounts": {"ALL": 9, "ORDERED": 3},
                    "rows": [
                        {
                            "orderId": 342,
                            "orderNo": "ORD-20260716-0342",
                            "orderedAt": "2026-07-16T09:42:00+09:00",
                            "recipientName": "김서연",
                            "paymentMethod": "MOCK_CARD",
                            "myItemsAmount": 89000,
                            "status": "ORDERED",
                            "claimStatus": None,
                            "items": [
                                {
                                    "orderItemId": 5551,
                                    "productId": 1,
                                    "name": "벨티드 린넨 원피스",
                                    "optionName": "블루/M",
                                    "quantity": 2,
                                    "price": 44500,
                                    "status": "ORDERED",
                                    "activeClaimStatus": None,
                                }
                            ],
                        }
                    ],
                    "total": 9,
                },
            },
        )

    client = _client(handler)
    result = await client.get_orders(12, status="ORDERED", limit=20)

    assert "/internal/seller/12/orders" in captured["url"]
    assert captured["headers"]["X-Internal-Token"] == TOKEN
    assert "status=ORDERED" in captured["url"]
    assert "limit=20" in captured["url"]
    # 미지정 선택 쿼리는 전송하지 않는다(None 미전송 규약).
    assert "orderId" not in captured["url"]
    assert "from" not in captured["url"]
    assert result.total == 9
    assert result.tab_counts["ORDERED"] == 3
    item = result.rows[0].items[0]
    assert (item.order_item_id, item.status) == (5551, "ORDERED")


async def test_get_orders_empty_rows_is_normal() -> None:
    """orderId 직조회 미존재·타사 = 200 + 빈 rows(존재 은닉) — 예외가 아니다."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "orderId=999" in str(request.url)
        return httpx.Response(
            200, json={"success": True, "data": {"tabCounts": {}, "rows": [], "total": 0}}
        )

    client = _client(handler)
    result = await client.get_orders(12, order_id=999)

    assert result.rows == [] and result.total == 0


# ── I-30 발송 처리 (§4.19, 이슈 #297 — 🔶 초안) ──────────────────────────────────


async def test_update_order_item_status_sends_camel_body() -> None:
    """PATCH /internal/seller/{brandId}/order-items/{id}/status + camelCase 본문 + 응답 파싱."""
    from app.schemas.spring import OrderItemStatusUpdate

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "orderItemId": 5551,
                    "fromStatus": "ORDERED",
                    "toStatus": "SHIPPING",
                    "changedAt": "2026-08-04T14:30:00+09:00",
                },
            },
        )

    client = _client(handler)
    result = await client.update_order_item_status(
        12, 5551, OrderItemStatusUpdate(to_status="SHIPPING", reason=None)
    )

    assert captured["method"] == "PATCH"
    assert "/internal/seller/12/order-items/5551/status" in captured["url"]
    assert captured["body"] == {"toStatus": "SHIPPING", "reason": None}
    assert (result.from_status, result.to_status) == ("ORDERED", "SHIPPING")


@pytest.mark.parametrize(
    ("status_code", "error_code", "expected_exc"),
    [
        (409, "ORDER_ALREADY_SHIPPED", "OrderAlreadyShipped"),
        (409, "ALREADY_SHIPPED", "OrderAlreadyShipped"),  # 과도기 — 개명 전 코드 낙성
        (404, "ORDER_ITEM_NOT_FOUND", "OrderItemNotFound"),
        (400, "ORDER_INVALID_TRANSITION", "OrderInvalidTransition"),
    ],
)
async def test_update_order_item_status_maps_error_codes(
    status_code: int, error_code: str, expected_exc: str
) -> None:
    """I-30 실패 코드는 전용 예외로 — HITL 이 '이미 된 일/안 되는 일'을 구분한다."""
    from app.schemas.spring import OrderItemStatusUpdate
    from app.services import spring_client as spring_module

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"success": False, "error": {"code": error_code}})

    client = _client(handler)
    with pytest.raises(getattr(spring_module, expected_exc)):
        await client.update_order_item_status(12, 5551, OrderItemStatusUpdate(to_status="SHIPPING"))


async def test_update_order_item_status_unknown_code_falls_back() -> None:
    """매핑에 없는 코드(401 INTERNAL_TOKEN_INVALID 등)·본문 없는 500 은 종전대로
    SpringUnavailableError — 호출부는 성공 보고 금지 + 재시도 안내."""
    from app.schemas.spring import OrderItemStatusUpdate

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client = _client(handler)
    with pytest.raises(SpringUnavailableError):
        await client.update_order_item_status(12, 5551, OrderItemStatusUpdate(to_status="SHIPPING"))


# ── I-31 리뷰 조회 (§4.20, 이슈 #297 — 🔶 초안) ──────────────────────────────────


async def test_get_reviews_url_params_and_parsing() -> None:
    """URL /internal/seller/{brandId}/reviews + rating CSV·sort 쿼리 + 응답 파싱."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "rows": [
                        {
                            "reviewId": 7,
                            "productId": 3,
                            "productName": "여행용 파우치",
                            "rating": 2,
                            "content": "지퍼가 일주일 만에 고장났어요",
                            "authorNickname": "자비스",
                            "createdAt": "2026-07-21T12:00:00+09:00",
                        }
                    ],
                    "total": 47,
                },
            },
        )

    client = _client(handler)
    result = await client.get_reviews(
        12, from_="2026-07-01", to="2026-07-31", rating="1,2", sort="rating"
    )

    assert "/internal/seller/12/reviews" in captured["url"]
    assert "rating=1%2C2" in captured["url"] or "rating=1,2" in captured["url"]
    assert "sort=rating" in captured["url"]
    assert result.total == 47
    assert result.rows[0].rating == 2
    assert result.rows[0].product_name == "여행용 파우치"


async def test_get_review_stats_parses_null_average() -> None:
    """stats=true 쿼리 강제 + 리뷰 0건이면 averageRating null(0 아님, I-16 규칙) 보존."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "totalCount": 0,
                    "averageRating": None,
                    "distribution": {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0},
                    "byProduct": [],
                },
            },
        )

    client = _client(handler)
    result = await client.get_review_stats(12)

    assert "stats=true" in captured["url"]
    assert result.total_count == 0
    assert result.average_rating is None


async def test_get_review_stats_sends_rating_and_product_filters() -> None:
    """[#494] rating·productId·기간이 집계 요청 쿼리스트링에 전부 실린다(I-31).

    응답 픽스처를 고정하는 계약 테스트로는 이 회귀가 안 잡힌다 — shape 가 아니라
    **요청 파라미터** 누락이라 HTTP 200 이 그대로 돌아온다. 그래서 요청 URL 을
    직접 스냅샷한다.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "totalCount": 18,
                    "averageRating": 1.4,
                    "distribution": {"5": 0, "4": 0, "3": 0, "2": 11, "1": 7},
                    "byProduct": [
                        {
                            "productId": 3,
                            "productName": "여행용 파우치",
                            "count": 12,
                            "averageRating": 1.3,
                        }
                    ],
                },
            },
        )

    client = _client(handler)
    result = await client.get_review_stats(
        12, from_="2026-07-01", to="2026-07-31", product_id=3, rating="1,2"
    )

    url = captured["url"]
    assert "stats=true" in url
    assert "rating=1%2C2" in url or "rating=1,2" in url
    assert "productId=3" in url
    assert "from=2026-07-01" in url and "to=2026-07-31" in url
    # 집계 모드에는 rows 가 없다 — 목록 전용 파라미터는 실리지 않아야 한다.
    assert "sort=" not in url and "limit=" not in url and "offset=" not in url
    assert result.total_count == 18
    assert result.by_product[0].product_id == 3


async def test_get_review_stats_omits_rating_when_absent() -> None:
    """[#494 회귀] rating 미지정이면 쿼리스트링에 rating 이 실리지 않는다."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "totalCount": 47,
                    "averageRating": 3.8,
                    "distribution": {"5": 12, "4": 15, "3": 8, "2": 7, "1": 5},
                    "byProduct": [],
                },
            },
        )

    client = _client(handler)
    await client.get_review_stats(12)

    assert "rating" not in captured["url"]
