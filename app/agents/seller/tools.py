"""판매자 도구(@tool) — ToolRuntime 방식 (SPEC-SELLER-001 §4, 2026-07-18 전환 완료).

신원 주입 원칙: 어떤 @tool 시그니처에도 sellerId/brandId 가 없다(IDOR 방지, api-spec §2.6).
도구는 모듈 레벨에 한 번만 정의하고, 신원은 `ToolRuntime[SellerContext]` 로 요청마다
주입받는다(runtime 파라미터는 LLM 스키마에 노출되지 않음). SpringClient 는 앱 소유
싱글턴(get_spring_client), 컨텍스트에는 신원만 담는다(2026-07-18 확정).

조회 도구는 실패 시 raise 하지 않고 `"Error: ..."` 문자열을 반환한다(§3.4 degrade 규약) —
스트림/파이프라인이 부분 실패로도 계속 진행되도록 한다. 쓰기 도구(create/update/delete)는
`PRODUCT_TOOLS` 에만 배정해 타입 수준에서 오분류를 차단한다(§3.1) — 분석·일반 에이전트는
`READ_TOOLS` 만 받는다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from app.agents.seller import calc
from app.agents.seller.context import SellerContext
from app.core.config import get_settings
from app.schemas.spring import BehaviorEventsResult, ProductCreate, ProductUpdate
from app.services.spring_client import SpringUnavailableError, get_spring_client


def _reference_note(from_date: str, to_date: str) -> str:
    """모든 조회 도구 응답에 기준 시점을 고지한다(답변 신뢰성)."""
    return f"(기준: {from_date}~{to_date} 집계값)"


def _summarize_events(events: list[dict]) -> str:
    """I-13/I-14 이벤트를 kv 나열로 요약한다(중간 상세도 — 계약 🔴 미확정 대응).

    응답 필드가 확정되지 않아(extra="allow" dict) 어떤 키가 와도 상위
    seller_summary_max_events 건까지 "key=value" 형태로 그대로 노출한다 —
    워커가 최소한의 재료를 갖되, 확정 후(C-13) 필드 선별 요약으로 교체한다.
    """
    settings = get_settings()
    shown = events[: settings.seller_summary_max_events]
    lines = ["{" + ", ".join(f"{k}={v}" for k, v in event.items()) + "}" for event in shown]
    omitted = len(events) - len(shown)
    omitted_note = f" 외 {omitted}건" if omitted > 0 else ""
    return "; ".join(lines) + omitted_note


# ── 조회 도구 (읽기 전용) ──


@tool
async def get_sales_timeseries(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    granularity: str = "daily",
) -> str:
    """지정 기간의 일/주/월별 매출·주문수 시계열과 이상 감지 결과를 요약한다.

    매출 통계 Q&A(sales_anomaly·general·recommend·chart)의 데이터 원천이다(I-6, api-spec §4.4).

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
        granularity: daily/weekly/monthly/summary 중 하나(기본 daily).
    """
    brand_id = runtime.context.brand_id  # 검증된 JWT 클레임 유래 — LLM 이 만들 수 없다.
    settings = get_settings()
    try:
        result = await get_spring_client().get_sales(brand_id, from_date, to_date, granularity)
    except SpringUnavailableError as exc:
        return (
            f"Error: 매출 데이터를 불러오지 못했습니다({exc}). "
            "다른 기간으로 다시 시도하거나 없이 진행하세요."
        )
    total_sales = sum(point.sales for point in result.series)
    total_orders = sum(point.order_count for point in result.series)
    # 상세 포함+상한(안 1, 2026-07-17 확정): 워커가 추이를 직접 서술할 수 있도록
    # 포인트별 수치를 나열하되 seller_summary_max_points 로 컨텍스트 폭주를 막는다.
    shown = result.series[: settings.seller_summary_max_points]
    detail_lines = ", ".join(f"{p.date} {p.sales:,}원/{p.order_count}건" for p in shown)
    omitted = len(result.series) - len(shown)
    omitted_note = f" (외 {omitted}개 포인트 생략)" if omitted > 0 else ""
    # 이동평균 window(seller_ma_window, §5)는 "일" 단위 전제 — daily 일 때만 이상 감지.
    if granularity == "daily":
        # Spring 의 isAnomaly/deviationPct 는 참고치일 뿐 — 원시 sales 로 재판정한다(§0.1 D).
        anomalies = calc.detect_sales_anomalies(
            result.series,
            window=settings.seller_ma_window,
            threshold_pct=settings.seller_anomaly_deviation_pct,
        )
        flagged = [
            f"{date} ({deviation:+.1f}%)" for date, deviation, is_anom in anomalies if is_anom
        ]
        anomaly_note = (
            f" 이상 감지 {len(flagged)}건(직전 {settings.seller_ma_window}일 평균 대비): "
            + ", ".join(flagged)
            + "."
            if flagged
            else " 이상 감지 없음."
        )
    else:
        anomaly_note = ""
    return (
        f"기간 {from_date}~{to_date} 총매출 {total_sales:,}원, 주문 {total_orders}건.\n"
        f"{granularity} 상세: {detail_lines}{omitted_note}.{anomaly_note} "
        f"{_reference_note(from_date, to_date)}"
    )


@tool
async def get_funnel(runtime: ToolRuntime[SellerContext], from_date: str, to_date: str) -> str:
    """구매전환 퍼널(조회→장바구니→결제→구매) 단계별 인원·전환율을 요약한다.

    conversion·behavior·chart 워커가 참고한다(I-7, api-spec §4.4).

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
    """
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().get_funnel(brand_id, from_date, to_date)
    except SpringUnavailableError as exc:
        return f"Error: 퍼널 데이터를 불러오지 못했습니다({exc})."
    rates = calc.conversion_rates(result)
    return (
        f"조회 {result.view}→장바구니 {result.cart}→결제 {result.checkout}"
        f"→구매 {result.purchase}, 전환율 view→cart {rates['view_to_cart']:.1f}% · "
        f"cart→checkout {rates['cart_to_checkout']:.1f}% · "
        f"checkout→purchase {rates['checkout_to_purchase']:.1f}%. "
        f"{_reference_note(from_date, to_date)}"
    )


_BEHAVIOR_AUTHORITY_NOTE = (
    "※ purchaseComplete 는 이벤트 기준(완료 페이지 발사) — "
    "매출·주문수의 권위는 매출 조회(I-6)/주문 전이(I-14)다."
)


def _summarize_behavior(result: BehaviorEventsResult) -> str:
    """I-13 응답을 groupBy 3형에 맞춰 요약한다(REALIGN ②-3 — 확정 명세 기준)."""
    settings = get_settings()
    if result.rows:  # groupBy=product (기본)
        shown = result.rows[: settings.seller_summary_max_events]
        lines = []
        for row in shown:
            c = row.counts
            rate = f"{row.view_to_cart_rate:.1%}" if row.view_to_cart_rate is not None else "-"
            lines.append(
                f"[{row.product_id}] {row.product_name or '이름없음'} "
                f"조회 {c.get('productView', 0)} 담기 {c.get('addToCart', 0)} "
                f"결제시작 {c.get('checkoutStart', 0)} 구매 {c.get('purchaseComplete', 0)} "
                f"(조회→담기 {rate}, 방문자 {row.unique_visitors if row.unique_visitors is not None else '-'})"
            )
        omitted = (result.total or len(result.rows)) - len(shown)
        omitted_note = f" 외 {omitted}건" if omitted > 0 else ""
        return f"상품별 {len(shown)}건: " + "; ".join(lines) + omitted_note
    if result.counts:  # groupBy=eventType
        return "유형별 합계: " + ", ".join(f"{k}={v}" for k, v in result.counts.items())
    if result.series:  # groupBy=date — 키 동적(date + camelCase 카운트)
        shown = result.series[: settings.seller_summary_max_points]
        lines = ["{" + ", ".join(f"{k}={v}" for k, v in p.items()) + "}" for p in shown]
        omitted = len(result.series) - len(shown)
        omitted_note = f" 외 {omitted}일" if omitted > 0 else ""
        return f"일자별 {len(shown)}건: " + "; ".join(lines) + omitted_note
    return ""


@tool
async def get_behavior_events(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    event_type: list[str] | None = None,
    product_id: int | None = None,
    group_by: str | None = None,
) -> str:
    """브랜드 상품의 행동 이벤트를 집계 조회한다(I-13, behavior_events 원천 — 07/17 확정).

    상품 연계 4종(product_view/add_to_cart/checkout_start/purchase_complete)만
    집계된다 — 전역 행동(검색·페이지뷰 등)은 이 도구로 조회 불가.

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
        event_type: 이벤트 유형 필터(선택, 복수) — 4종 중 선택, 미지정 시 전체.
        product_id: 특정 상품으로 좁힐 때(선택, 숫자).
        group_by: product(기본, 상품별) | eventType(유형 합계) | date(일자별 시계열).
    """
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().get_events(
            brand_id, from_date, to_date, event_type, product_id, group_by
        )
    except SpringUnavailableError as exc:
        return f"Error: 행동 이벤트 데이터를 불러오지 못했습니다({exc})."
    summary = _summarize_behavior(result)
    if not summary:
        return f"행동 이벤트 0건. {_reference_note(from_date, to_date)}"
    return f"{summary}. {_BEHAVIOR_AUTHORITY_NOTE} {_reference_note(from_date, to_date)}"


# I-14/I-15 기록 규칙 주의 문구 (REALIGN ②-4 — schema.sql D32/D34 확정 반영).
# 도구 출력에 상시 부착해 워커가 로그 부재를 '데이터 이상'으로 오해석하는 것을 막는다.
_ORDER_LOG_RULES_NOTE = (
    "※ 기록 규칙: 구매확정·클레임 신청은 로그에 없음(취소/반품은 완료 시점만). "
    "배치 전이(배송 등)는 주문 단위 1행 — 건수를 아이템 수로 해석 금지."
)
_PRODUCT_LOG_RULES_NOTE = (
    "※ 기록 규칙: 주문에 의한 재고 차감은 미기록(수동 조정·품절/재입고 전환만). "
    "품절 신호 = STOCK 변경의 new_value 0 (SOLD_OUT 상태는 없음)."
)


@tool
async def get_order_events(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    to_status: str | None = None,
    actor_type: str | None = None,
    group_by: str | None = None,
    stats: bool | None = None,
) -> str:
    """주문 상태 전이 이력을 조회해 요약한다(I-14, order_status_logs 원천).

    구매자 get_recent_purchases(I-19, §4.7)와 다른 계약이다(혼동 금지).
    기록 규칙(07/17 D32/D34 확정): 구매확정(CONFIRMED)·클레임 신청(*_REQUESTED)·
    ORDERED 는 기록되지 않는다 — 취소/반품은 '완료' 시점만 남는다. 교환 어휘 없음.

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
        to_status: 전이 대상 상태(선택) — 주문: PENDING/PAID/PAYMENT_FAILED/CANCELLED,
            아이템: SHIPPING/DELIVERED/CANCELLED/RETURNED (교환 어휘 없음).
        actor_type: 전이 주체(선택) — USER/SELLER/ADMIN/SYSTEM.
        group_by: 집계 그룹 기준(선택).
        stats: 집계 모드로 조회할지 여부(선택, api-spec §4.4 `stats` 쿼리).
    """
    brand_id = runtime.context.brand_id
    try:
        status_filter = [to_status] if to_status else None
        result = await get_spring_client().get_order_events(
            brand_id, from_date, to_date, status_filter, actor_type, group_by, stats
        )
    except SpringUnavailableError as exc:
        return f"Error: 주문 이벤트 데이터를 불러오지 못했습니다({exc})."
    # stats 모드 응답(stats dict)이 있으면 함께 노출 — 집계 질의의 주 재료.
    stats_note = (
        " 집계: " + ", ".join(f"{k}={v}" for k, v in result.stats.items()) if result.stats else ""
    )
    if not result.events and not stats_note:
        return f"주문 상태 전이 0건. {_reference_note(from_date, to_date)}"
    return (
        f"주문 상태 전이 {len(result.events)}건: {_summarize_events(result.events)}."
        f"{stats_note} {_ORDER_LOG_RULES_NOTE} {_reference_note(from_date, to_date)}"
    )


@tool
async def get_product_change_logs(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    change_type: str | None = None,
    product_id: int | None = None,
) -> str:
    """상품 변경 이력(가격/재고/상태, 판매자 감사 로그)을 조회해 요약한다.

    [혼동 금지] 구매자 fetch_product_changes(I-17, §4.8 AI 생성물 배치)와 다른 계약이다.
    (I-15, product_change_logs 원천 — 07/17 D32 확정)
    기록 규칙: 주문에 의한 재고 차감·전후 동일값 변경은 기록되지 않는다.
    품절 신호 = STOCK 변경의 new_value 0 (SOLD_OUT 상태 미도입).

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
        change_type: PRICE/STOCK/STATUS 중 하나(선택).
        product_id: 특정 상품으로 좁힐 때(선택, 숫자).
    """
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().get_product_changes(
            brand_id, from_date, to_date, change_type, product_id
        )
    except SpringUnavailableError as exc:
        return f"Error: 상품 변경 이력을 불러오지 못했습니다({exc})."
    return (
        f"상품 변경 이력 {len(result.logs)}건(가격/재고/상태). "
        f"{_PRODUCT_LOG_RULES_NOTE} {_reference_note(from_date, to_date)}"
    )


@tool
async def get_churn_cohort(
    runtime: ToolRuntime[SellerContext], inactive_days: int | None = None
) -> str:
    """이탈 코호트(무활동 고객) 이탈률·이탈 전 신호를 요약한다(I-16, api-spec §4.4).

    Args:
        inactive_days: 무활동 판정 기준일(선택, 미지정 시 설정 기본값).
    """
    brand_id = runtime.context.brand_id
    # 기본값을 호출 시점에 해석한다 — 임포트 시점 고정 방지(Settings 주입 원칙).
    effective_days = (
        inactive_days if inactive_days is not None else get_settings().seller_churn_inactive_days
    )
    try:
        result = await get_spring_client().get_churn(brand_id, effective_days)
    except SpringUnavailableError as exc:
        return f"Error: 이탈 코호트 데이터를 불러오지 못했습니다({exc})."
    return (
        f"이탈률 {result.churn_rate:.1f}%, 이탈 전 신호 {len(result.pre_churn_signals)}건. "
        f"(기준: inactiveDays={effective_days})"
    )


@tool
async def get_account_events(
    event_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    group_by: str | None = None,
) -> str:
    """계정/보안 이벤트 집계를 조회해 요약한다.

    [주의] I-8 은 brandId path 가 없는 전역(admin 소유 🔴) 계약이다(api-spec §4.4) —
    신원 컨텍스트가 필요 없어 runtime 파라미터도 없다.

    Args:
        event_type: 이벤트 종류(선택).
        from_date: 조회 시작일(선택, YYYY-MM-DD).
        to_date: 조회 종료일(선택, YYYY-MM-DD).
        group_by: 집계 그룹 기준(선택).
    """
    try:
        result = await get_spring_client().get_account_events(
            event_type, from_date, to_date, group_by
        )
    except SpringUnavailableError as exc:
        return f"Error: 계정 이벤트 데이터를 불러오지 못했습니다({exc})."
    return f"계정/보안 이벤트 {len(result.events)}건 집계됨(전역)."


@tool
async def list_my_products(
    runtime: ToolRuntime[SellerContext],
    status: str | None = None,
    q: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    """자사 상품 목록을 조회해 요약한다(I-9, api-spec §4.5).

    recommend·general 워커는 조회용으로, product_agent 는 쓰기 전 `before` 확보용으로
    사용한다(§4.5 — 구 I-7 상세 읽기 대체).

    Args:
        status: ON_SALE/HIDDEN 중 하나로 좁힐 때(선택).
        q: 상품명 검색어(선택).
        limit: 반환 상한(선택, 미지정 시 설정 기본값).
        offset: 페이지 오프셋(선택).
    """
    brand_id = runtime.context.brand_id
    settings = get_settings()
    # limit 미지정 시 Settings 기본값 — 상품 수백 건 나열로 인한 컨텍스트 폭주 방지.
    effective_limit = limit if limit is not None else settings.seller_list_default_limit
    try:
        result = await get_spring_client().list_products(
            brand_id, status, q, effective_limit, offset
        )
    except SpringUnavailableError as exc:
        return f"Error: 상품 목록을 불러오지 못했습니다({exc})."
    if not result.rows:
        return "상품이 없습니다."
    lines = [
        f"[{row.product_id}] {row.name} 가격 {row.price:,}원 재고 {row.stock_quantity}건 "
        f"상태 {row.status}"
        for row in result.rows
    ]
    # 상한만큼 꽉 찼으면 더 있을 수 있음을 고지 — LLM 이 offset 으로 이어서 조회 가능.
    more_note = (
        f" (상한 {effective_limit}건 표시 — 더 보려면 offset을 지정하세요)"
        if len(result.rows) >= effective_limit
        else ""
    )
    return f"상품 {len(result.rows)}건: " + "; ".join(lines) + more_note


@tool
async def calculate(expression: str) -> str:
    """사칙연산·round()·비율 등 수치 확인이 필요할 때 사용하는 안전 계산기.

    `__import__`·속성 접근 등 임의 코드는 구조적으로 차단된다(calc.safe_eval, ast 화이트리스트).
    신원·외부 호출이 없어 runtime 파라미터도 없다.

    Args:
        expression: 계산할 수식 문자열(예: "1200000 / 45 * 100").
    """
    try:
        value = calc.safe_eval(expression)
    # 화이트리스트 밖 요소는 ValueError, 화이트리스트 안에서도 0 나눗셈(ZeroDivisionError)·
    # 오버플로(OverflowError, 둘 다 ArithmeticError 하위)·round() 인자 오류(TypeError)는
    # 자연스러운 연산 예외로 전파된다 — 분모 0 은 전환율 계산의 흔한 입력이라 raise 금지.
    except (ValueError, ArithmeticError, TypeError) as exc:
        return f"Error: 계산식을 처리할 수 없습니다({exc})."
    return f"계산 결과: {value}"


@tool
async def search_analysis_guide(query: str) -> str:
    """판매자 분석 기준서(용어·산식 정의)를 검색한다.

    기준서 문서가 아직 없어(SPEC-SELLER-001 §9.2, 🔴) 1단계는 인터페이스만 제공하고
    degrade 문자열을 반환한다 — 4단계에서 RAG 로 활성화된다.

    Args:
        query: 찾고자 하는 기준서 주제/용어.
    """
    try:
        raise NotImplementedError("analysis guide RAG not implemented yet (SPEC-SELLER-001 §9.2)")
    except NotImplementedError:
        # 내부 스텁 사유는 로그/개발자용 — 사용자 표면에는 degrade 문구만 노출한다.
        return "Error: 분석 기준서 검색은 아직 준비 중입니다."


# ── 쓰기 도구 (product_agent 전용, HITL 승인 후 호출, §3.4 §4.5) ──


@tool
async def create_product(
    runtime: ToolRuntime[SellerContext],
    name: str,
    price: int,
    stock_quantity: int,
    original_price: int | None = None,
    category: str | None = None,
    description: str | None = None,
) -> str:
    """신규 상품을 등록한다(I-10, api-spec §4.5). HITL 승인 후에만 호출한다.

    Args:
        name: 상품명.
        price: 판매가(originalPrice 이하).
        stock_quantity: 초기 재고 수량(0 이상).
        original_price: 정가(선택).
        category: 카테고리(선택).
        description: 상세 설명(선택).
    """
    brand_id = runtime.context.brand_id
    try:
        payload = ProductCreate(
            name=name,
            price=price,
            stock_quantity=stock_quantity,
            original_price=original_price,
            category=category,
            description=description,
        )
        result = await get_spring_client().create_product(brand_id, payload)
    except SpringUnavailableError as exc:
        return f"Error: 상품 등록에 실패했습니다({exc})."
    return f"등록됨: productId={result.product_id} (status={result.status})"


@tool
async def update_product(
    runtime: ToolRuntime[SellerContext],
    product_id: int,
    name: str | None = None,
    price: int | None = None,
    original_price: int | None = None,
    description: str | None = None,
    category: str | None = None,
    image_url: str | None = None,
    status: str | None = None,
    stock_quantity: int | None = None,
) -> str:
    """기존 상품을 수정한다(I-11, api-spec §4.5). 바꿀 필드만 전달, HITL 승인 후 호출.

    ProductUpdate 스키마 전 필드 노출(2026-07-18 사용자 확정 — CSV 4필드 언급과의 차이는
    C-14 협의 대상, 미지원 필드 400 은 Error 문자열로 degrade). 재고도 이 도구로 통합
    처리한다(별도 재고 API 없음, 절대값 인자 — 2단계에서 delta 환산 프롬프트와 정합).

    Args:
        product_id: 대상 상품 식별자.
        name: 변경할 상품명(선택).
        price: 변경할 판매가(선택, originalPrice 이하).
        original_price: 변경할 정가(선택).
        description: 변경할 상세 설명(선택).
        category: 변경할 카테고리(선택).
        image_url: 변경할 대표 이미지 URL(선택).
        status: ON_SALE/HIDDEN 중 하나로 변경(선택).
        stock_quantity: 변경할 재고 수량(절대값, 선택).
    """
    brand_id = runtime.context.brand_id
    try:
        patch = ProductUpdate(
            name=name,
            price=price,
            original_price=original_price,
            description=description,
            category=category,
            image_url=image_url,
            status=status,
            stock_quantity=stock_quantity,
        )
        result = await get_spring_client().update_product(brand_id, product_id, patch)
    except SpringUnavailableError as exc:
        return f"Error: 상품 수정에 실패했습니다({exc})."
    return f"수정됨: productId={result.product_id}"


@tool
async def delete_product(runtime: ToolRuntime[SellerContext], product_id: int) -> str:
    """상품을 삭제(숨김)한다(I-12, api-spec §4.5). 물리 삭제 없음 — HITL 승인 후 호출.

    Args:
        product_id: 대상 상품 식별자.
    """
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().delete_product(brand_id, product_id)
    except SpringUnavailableError as exc:
        return f"Error: 상품 삭제에 실패했습니다({exc})."
    return f"삭제(숨김)됨: productId={result.product_id} (status={result.status})"


# ── 도구 배정 상수 (SPEC-SELLER-001 §4 소비 노드 — 에이전트 팩토리가 그대로 사용) ──

# 분석 워커·general·recommend 용(조회만) — 쓰기 도구가 구조적으로 없다(§3.1).
READ_TOOLS: list[BaseTool] = [
    get_sales_timeseries,
    get_funnel,
    get_behavior_events,
    get_order_events,
    get_product_change_logs,
    get_churn_cohort,
    get_account_events,
    list_my_products,
    calculate,
    search_analysis_guide,
]

# product_agent 전용(list_my_products=쓰기 전 before 확보 + 쓰기 3종, HITL 승인 후 호출).
PRODUCT_TOOLS: list[BaseTool] = [
    list_my_products,
    create_product,
    update_product,
    delete_product,
]
