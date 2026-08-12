"""app/agents/seller/tools.py(ToolRuntime 도구 + READ_TOOLS) 단위 테스트.

DESIGN-SELLER-TOOLS-STAGE1 §6 테스트 목록. 실 Spring 호출 없이 FakeSpringClient 로
브랜드 스코프 주입(IDOR 방지)·degrade 문자열 반환을 검증한다.

[#620] 상품/주문 쓰기 도구(create_product/update_product/delete_product/
update_order_status @tool)와 그 레지스트리(PRODUCT_TOOLS/ORDER_WRITE_TOOLS)는 어느
에이전트에도 바인딩되지 않는 죽은 코드였다 — 제거됐다. 실행은 hitl._execute_draft 가
코드로 SpringClient 를 직접 호출한다(HITL 모듈 결정 1). 이 파일은 이제 조회·계산
도구(READ_TOOLS)만 검증한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from langchain_core.tools import BaseTool

from app.agents.seller.analysis_records import RecommendationRecord, ReportRecord
from app.agents.seller.context import SellerContext
from app.agents.seller import tools as seller_tools
from app.agents.seller.tools import (
    READ_TOOLS,
    get_account_events,
    get_latest_report,
    get_behavior_events,
    get_churn_cohort,
    get_funnel,
    get_order_events,
    get_orders,
    get_reviews,
    get_sales_timeseries,
    list_my_products,
)
from app.services import spring_client as spring_client_module
from app.schemas.spring import (
    AccountEventsResult,
    BehaviorEventsResult,
    BehaviorProductRow,
    ChurnMember,
    ChurnResult,
    FunnelResult,
    PreChurnSignals,
    OrderEventsResult,
    ProductChangeLogResult,
    ProductChangeLogRow,
    OrderItemStatusResult,
    ProductCreateResult,
    ProductDeleteResult,
    ProductUpdateResult,
    SalesResult,
    SalesSeriesPoint,
    SellerOrderItemRow,
    SellerOrderList,
    SellerOrderRow,
    SellerProductList,
    SellerReviewList,
    SellerReviewProductStat,
    SellerReviewRow,
    SellerReviewStats,
)
from app.services.spring_client import SpringUnavailableError
from app.core.tracing import (
    FakeTraceExporter,
    LangSmithTraceExporter,
    TraceFactory,
    bind_request_trace,
)

FORBIDDEN_IDENTITY_KEYS = {"sellerId", "brandId", "seller_id", "brand_id"}


class FakeSpringClient:
    """SpringClient 이중(double). 실 HTTP 없이 브랜드 스코프 주입·오류 경로만 검증한다."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.recorded_brand_id: int | None = None
        self.recorded_stats: bool | None = None
        self.recorded_event_args: tuple | None = None
        self.behavior_result = BehaviorEventsResult()  # I-13 기본 빈 응답(3형 공통)
        self.order_events_result = OrderEventsResult()  # I-14 기본 빈 응답(rows/total, #194)
        # I-16 기본 응답(#197 — churnRate 는 fraction: 0.05 = 5%. 구 fixture 의 5.0 은
        # 그 자체가 단위 오독의 산물이었다). 코호트 존재·이탈 회원 없음 상태.
        self.churn_result = ChurnResult(
            churn_rate=0.05, cohort_size=20, pre_churn_signals=PreChurnSignals(), members=[]
        )
        self.recorded_churn_args: tuple | None = None
        self.churn_calls: list[tuple] = []  # [#290] 현재+직전 기간 2회 호출 기록
        self.funnel_calls: list[tuple] = []  # [#290] 현재+직전 기간 2회 호출 기록
        self.account_events_result = AccountEventsResult()  # I-8 기본 빈 응답(rows, #197)
        self.recorded_account_args: tuple | None = None
        # [#518] bucket 팬아웃은 도구 호출 1회가 I-31 집계를 N번 부른다 — 마지막 인자만
        # 남기는 recorded_review_stats_args 로는 구간 경계를 검증할 수 없어 전 호출을
        # 누적한다. review_stats_by_range 는 (from_, to) → 결과 또는 예외 매핑이다.
        self.review_stats_calls: list[tuple] = []
        self.review_stats_by_range: dict[tuple, object] | None = None
        self._fail = fail or set()

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail:
            raise SpringUnavailableError(f"Spring 콜백 타임아웃(3.0s): {method}")

    async def get_sales(self, brand_id, from_, to, granularity="daily"):
        self.recorded_brand_id = brand_id
        self._maybe_fail("get_sales")
        return SalesResult(
            series=[
                SalesSeriesPoint(
                    date="2026-07-01",
                    sales=1000,
                    order_count=3,
                    is_anomaly=False,
                    deviation_pct=0.0,
                )
            ]
        )

    async def get_funnel(self, brand_id, from_, to):
        self.recorded_brand_id = brand_id
        self.funnel_calls.append((from_, to))  # [#290] 직전 기간 자동 비교 조회 검증용
        self._maybe_fail("get_funnel")
        return FunnelResult(view=100, cart=10, checkout=5, purchase=3)

    async def get_events(
        self, brand_id, from_, to, event_type=None, product_id=None, group_by=None
    ):
        self.recorded_brand_id = brand_id
        self.recorded_event_args = (event_type, product_id, group_by)
        self._maybe_fail("get_events")
        return self.behavior_result  # 기본 빈 응답 — 테스트가 형태별로 교체

    async def get_order_events(
        self, brand_id, from_, to, to_status=None, actor_type=None, group_by=None, stats=None
    ):
        self.recorded_brand_id = brand_id
        self.recorded_stats = stats
        self._maybe_fail("get_order_events")
        return self.order_events_result

    async def get_product_changes(self, brand_id, from_, to, change_type=None, product_id=None):
        self.recorded_brand_id = brand_id
        self._maybe_fail("get_product_changes")
        return ProductChangeLogResult()  # I-15 기본 빈 응답(rows/total, #194)

    async def get_churn(self, brand_id, from_, to, inactive_days):
        self.recorded_brand_id = brand_id
        # [#290] 직전 기간 자동 비교로 호출이 2회가 됐다 — recorded_churn_args 는
        # 첫 호출(현재 기간) 유지, 전체 호출은 churn_calls 로 검증한다.
        if self.recorded_churn_args is None:
            self.recorded_churn_args = (from_, to, inactive_days)
        self.churn_calls.append((from_, to, inactive_days))
        self._maybe_fail("get_churn")
        return self.churn_result

    async def get_account_events(self, brand_id, from_, to, event_type=None, group_by=None):
        # [#481] 브랜드 스코프 전환 — brand_id 가 첫 인자로 필수다(자사 코호트 경로).
        self.recorded_account_args = (brand_id, from_, to, event_type, group_by)
        self._maybe_fail("get_account_events")
        return self.account_events_result

    async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
        self.recorded_brand_id = brand_id
        self.recorded_limit = limit  # 기본 limit(Settings) 주입 검증용
        self._maybe_fail("list_products")
        return SellerProductList(rows=[])

    async def create_product(self, brand_id, payload):
        self.recorded_brand_id = brand_id
        self._maybe_fail("create_product")
        return ProductCreateResult(product_id=101, status="ON_SALE")

    async def update_product(self, brand_id, product_id, patch):
        self.recorded_brand_id = brand_id
        self.recorded_patch = patch  # 전 필드 노출(name 등) 전달 검증용
        self._maybe_fail("update_product")
        return ProductUpdateResult(product_id=product_id)

    async def delete_product(self, brand_id, product_id):
        self.recorded_brand_id = brand_id
        self._maybe_fail("delete_product")
        return ProductDeleteResult(product_id=product_id, status="DELETED")

    # ── [#297] I-29/I-30/I-31 주문·리뷰 ──

    async def get_orders(
        self, brand_id, *, status=None, order_id=None, from_=None, to=None, limit=None, offset=None
    ):
        self.recorded_brand_id = brand_id
        self.recorded_orders_args = (status, order_id, from_, to, limit, offset)
        self._maybe_fail("get_orders")
        return getattr(self, "orders_result", SellerOrderList())

    async def update_order_item_status(self, brand_id, order_item_id, payload):
        self.recorded_brand_id = brand_id
        self.recorded_order_status_args = (order_item_id, payload)
        self._maybe_fail("update_order_item_status")
        if getattr(self, "order_status_error", None) is not None:
            raise self.order_status_error
        return OrderItemStatusResult(
            order_item_id=order_item_id,
            from_status="ORDERED",
            to_status=payload.to_status,
            changed_at="2026-08-05T10:00:00+09:00",
        )

    async def get_reviews(
        self,
        brand_id,
        *,
        from_=None,
        to=None,
        product_id=None,
        rating=None,
        sort=None,
        limit=None,
        offset=None,
    ):
        self.recorded_brand_id = brand_id
        self.recorded_reviews_args = (from_, to, product_id, rating, sort, limit, offset)
        self._maybe_fail("get_reviews")
        return getattr(self, "reviews_result", SellerReviewList())

    async def get_review_stats(
        self, brand_id, *, from_=None, to=None, product_id=None, rating=None
    ):
        self.recorded_brand_id = brand_id
        self.recorded_review_stats_args = (from_, to, product_id, rating)
        self.review_stats_calls.append((from_, to, product_id, rating))
        self._maybe_fail("get_review_stats")
        if self.review_stats_by_range is not None:
            outcome = self.review_stats_by_range.get((from_, to))
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        return getattr(self, "review_stats_result", SellerReviewStats())


class FakeRuntime:
    """ToolRuntime 이중 — 도구 본문은 runtime.context 만 읽으므로 덕 타이핑으로 충분하다."""

    def __init__(self, brand_id: int = 42) -> None:
        self.context = SellerContext(seller_id=1, brand_id=brand_id)


async def _call_runtime_tool(tool: BaseTool, args: dict, fake, brand_id: int = 42):
    """ToolRuntime 도구를 단위 테스트에서 직접 호출한다.

    에이전트 런타임 없이 원본 코루틴(tool.coroutine)에 FakeRuntime 을 키워드로 넘기고,
    SpringClient 싱글턴을 이중으로 교체했다가 반드시 원복한다.
    """
    spring_client_module.set_spring_client(fake)
    try:
        return await tool.coroutine(runtime=FakeRuntime(brand_id), **args)
    finally:
        spring_client_module.set_spring_client(None)


def test_no_write_tools_exist() -> None:
    """[#620] 상품/주문 쓰기 도구는 모듈에 아예 없다 — 실행은 hitl._execute_draft 가
    코드로 SpringClient 를 직접 호출한다(HITL 모듈 결정 1). 예전엔 PRODUCT_TOOLS·
    ORDER_WRITE_TOOLS 라는 전용 레지스트리에 격리돼 있었는데, 어느 에이전트에도
    바인딩되지 않는 죽은 코드로 확인돼 레지스트리째 제거했다 — 되살아나는 회귀를
    이 테스트가 잡는다.
    """
    read_names = {t.name for t in READ_TOOLS}
    for write_name in ("create_product", "update_product", "delete_product", "update_order_status"):
        assert write_name not in read_names
        assert not hasattr(seller_tools, write_name)
    assert not hasattr(seller_tools, "PRODUCT_TOOLS")
    assert not hasattr(seller_tools, "ORDER_WRITE_TOOLS")
    # 신설 조회 2종은 read 에 있다.
    assert {"get_orders", "get_reviews"} <= read_names


def test_no_identity_params_in_any_tool() -> None:
    """모든 도구의 args_schema 에 sellerId/brandId 류 키가 없다(IDOR — 신원 미노출)."""
    for t in READ_TOOLS:
        arg_keys = set(t.args.keys())
        assert not (arg_keys & FORBIDDEN_IDENTITY_KEYS), (
            f"{t.name} exposes identity arg: {arg_keys}"
        )
        # ToolRuntime 파라미터는 LLM 스키마에서 은닉되어야 한다(v1 주입 계약).
        assert "runtime" not in arg_keys, f"{t.name} exposes runtime arg"


async def test_context_injects_brand_id() -> None:
    """도구 인자로 brand_id 를 넘기지 않아도 runtime.context 의 brand_id 가 client 에 전달된다."""
    fake = FakeSpringClient()

    await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-14"},
        fake,
        brand_id=777,
    )

    assert fake.recorded_brand_id == 777


async def test_tool_returns_error_string_on_spring_failure() -> None:
    """Spring 실패(SpringUnavailableError) 시 도구는 raise 없이 "Error:" 문자열을 반환한다."""
    fake = FakeSpringClient(fail={"get_sales"})

    result = await _call_runtime_tool(
        get_sales_timeseries, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert result.startswith("Error:")


async def test_caught_tool_error_exports_only_safe_error_code() -> None:
    fake = FakeSpringClient(fail={"get_sales"})
    exporter = FakeTraceExporter()
    trace = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1).start_request(
        name="seller_chat_turn",
        request_id="req-safe",
        conversation_id="conversation-safe",
        thread_id="thread-safe",
        lane="analysis",
        environment="test",
    )

    with bind_request_trace(trace):
        result = await _call_runtime_tool(
            get_sales_timeseries,
            {"from_date": "2026-07-01", "to_date": "2026-07-14"},
            fake,
        )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert result.startswith("Error:")
    tool_span = next(
        node for node in exporter.exported[0] if node.name == "tool.get_sales_timeseries"
    )
    assert tool_span.error_type == "TOOL_ERROR"
    payload = repr(tool_span)
    assert "2026-07-01" not in payload
    assert "2026-07-14" not in payload
    assert "Spring 콜백 타임아웃" not in payload


async def test_caught_tool_error_survives_pinned_sdk_serialization_without_payload(
    monkeypatch,
) -> None:
    """Removing child error serialization must lose TOOL_ERROR and fail this regression."""
    from langsmith import Client

    serialized_operations = []

    def capture_after_sdk_serialization(self, operations, **kwargs) -> None:
        del self, kwargs
        serialized_operations.extend(operations)

    monkeypatch.setattr(Client, "_batch_ingest_run_ops", capture_after_sdk_serialization)
    client = Client(
        api_key="lsv2_pt_abcdefghijklmnop1234",
        auto_batch_tracing=False,
        omit_traced_runtime_info=True,
    )
    exporter = LangSmithTraceExporter(client, "jarvis-ai-test", 0.5)
    trace = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1).start_request(
        name="seller_chat_turn",
        request_id="req-safe",
        conversation_id="conversation-safe",
        thread_id="thread-safe",
        lane="analysis",
        environment="test",
    )
    fake = FakeSpringClient(fail={"get_sales"})

    with bind_request_trace(trace):
        result = await _call_runtime_tool(
            get_sales_timeseries,
            {"from_date": "2026-07-01", "to_date": "2026-07-14"},
            fake,
            brand_id=987654321,
        )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert result.startswith("Error:")
    run_info = [operation.deserialize_run_info() for operation in serialized_operations]
    tool_run = next(run for run in run_info if run["name"] == "tool.get_sales_timeseries")
    assert tool_run["extra"]["metadata"]["errorType"] == "TOOL_ERROR"
    assert all(operation.inputs == b"{}" for operation in serialized_operations)
    assert all(operation.outputs == b"{}" for operation in serialized_operations)
    serialized = repr(run_info)
    for private_value in (
        "2026-07-01",
        "2026-07-14",
        "987654321",
        "Spring 콜백 타임아웃",
        "Error: 매출 데이터를 불러오지 못했습니다",
    ):
        assert private_value not in serialized


async def test_tool_returns_error_string_on_timeout() -> None:
    """타임아웃(SpringUnavailableError 로 이미 변환됨)도 "Error:" 문자열로 degrade 된다."""
    fake = FakeSpringClient(fail={"get_funnel"})

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert result.startswith("Error:")


async def test_sales_tool_summary_includes_reference_period() -> None:
    """매출 조회 도구의 반환 문자열에 기준 기간 고지가 포함된다."""
    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-14"},
        FakeSpringClient(),
    )

    assert "기준:" in result
    assert "2026-07-01" in result and "2026-07-14" in result


async def test_search_analysis_guide_is_stub() -> None:
    """내부 NotImplementedError 를 노출하지 않고 degrade 문자열만 반환한다."""
    tool = next(t for t in READ_TOOLS if t.name == "search_analysis_guide")

    result = await tool.ainvoke({"query": "전환율 정의"})

    assert result.startswith("Error:")
    assert "NotImplementedError" not in result


def test_list_my_products_in_read_tools() -> None:
    """list_my_products 는 READ_TOOLS 에 있다 — product_agent 는 workers.py 의
    PRODUCT_DRAFT_TOOLS(별도 리스트, #620 이후 조회·계산 전용)에서 같은 함수를
    재사용한다."""
    assert "list_my_products" in {t.name for t in READ_TOOLS}


async def test_calculate_tool_handles_division_by_zero() -> None:
    """0 나눗셈(ZeroDivisionError, ArithmeticError 하위)도 raise 없이 degrade 된다(opus 리뷰 M2).

    safe_eval 은 화이트리스트 위반만 ValueError 로 막고, 화이트리스트 안 연산(0 나눗셈 등)의
    파이썬 예외는 그대로 전파되므로 도구가 (ValueError, ArithmeticError, TypeError) 를 모두
    잡아야 한다. 분모 0 은 전환율 계산에서 흔한 입력이라 특히 중요하다.
    """
    tool = next(t for t in READ_TOOLS if t.name == "calculate")

    result = await tool.ainvoke({"expression": "1/0"})

    assert result.startswith("Error:")


async def test_calculate_tool_handles_round_type_error() -> None:
    """round() 인자 오류(TypeError)도 raise 없이 degrade 된다(opus 리뷰 M2 연장)."""
    tool = next(t for t in READ_TOOLS if t.name == "calculate")

    result = await tool.ainvoke({"expression": "round(1, 2, 3)"})

    assert result.startswith("Error:")


async def test_sales_tool_skips_anomaly_detection_for_non_daily_granularity() -> None:
    """granularity 가 daily 가 아니면 이상 감지를 생략한다(opus 리뷰 m5).

    이동평균 window(seller_ma_window, §5)는 "일" 단위를 전제하므로 weekly/monthly 시계열에
    그대로 적용하면 window 정렬이 깨진다 — daily 일 때만 detect_sales_anomalies 를 돈다.
    """

    class SpikySalesClient(FakeSpringClient):
        """이상 감지 임계값(기본 30%)을 확실히 넘는 급증 시계열을 반환하는 이중."""

        async def get_sales(self, brand_id, from_, to, granularity="daily"):
            self.recorded_brand_id = brand_id
            points = [
                SalesSeriesPoint(date=f"2026-07-{day:02d}", sales=100, order_count=1)
                for day in range(1, 8)
            ]
            points.append(SalesSeriesPoint(date="2026-07-08", sales=10000, order_count=50))
            return SalesResult(series=points)

    fake = SpikySalesClient()
    daily_result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-08", "granularity": "daily"},
        fake,
    )
    weekly_result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-08", "granularity": "weekly"},
        fake,
    )

    # 상세 포맷 변경(안 1): daily 는 이상 감지 문구(편차율 포함), weekly 는 판정 자체를 생략.
    assert "이상 감지" in daily_result
    assert "이상 감지" not in weekly_result


async def test_sales_tool_degrades_when_anomaly_config_invalid(monkeypatch) -> None:
    """[#194 리뷰 3 계승, #290] detect_seasonal_anomalies 의 ValueError 가 도구 밖으로
    전파되지 않는다(§3.4 degrade 규약) — 기동 시점 검증(config)이 우회·회귀로 뚫려도
    매출 요약은 살리고 이상 감지만 생략한다."""
    from app.agents.seller.analysis import timeseries as timeseries_module

    def _boom(*_args, **_kwargs):
        raise ValueError("dates(1)/values(2) 길이가 다르다")

    monkeypatch.setattr(timeseries_module, "detect_seasonal_anomalies", _boom)

    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-14", "granularity": "daily"},
        FakeSpringClient(),
    )

    assert "총매출" in result  # 매출 요약은 유지된다
    assert "이상 감지 판정 불가" in result
    assert not result.startswith("Error:")  # 전체 실패로 격하하지 않는다


class RecordingSalesClient(FakeSpringClient):
    """조회 인자·시계열을 기록/주입하는 이중 — lookback 확장 검증용(#290)."""

    def __init__(self, series=None) -> None:
        super().__init__()
        self.recorded_sales_args: tuple | None = None
        self._series = series or []

    async def get_sales(self, brand_id, from_, to, granularity="daily"):
        self.recorded_brand_id = brand_id
        self.recorded_sales_args = (from_, to, granularity)
        from app.schemas.spring import SalesResult

        return SalesResult(series=self._series)


async def test_sales_tool_extends_daily_fetch_by_lookback_but_reports_requested_window() -> None:
    """[#290] daily 는 STL 학습용으로 lookback(28일)만큼 앞당겨 조회하되, 총매출·상세
    나열·이상 보고는 요청 기간 내로 한정한다 — 판매자가 묻지 않은 기간의 수치·이상을
    노출하지 않는다(질문 범위 준수)."""
    series = [
        # lookback 구간(요청 밖) — 여기 급증(9만원)은 보고되면 안 된다.
        SalesSeriesPoint(date="2026-06-20", sales=1000, order_count=1),
        SalesSeriesPoint(date="2026-06-21", sales=90000, order_count=9),
        SalesSeriesPoint(date="2026-06-22", sales=1000, order_count=1),
        # 요청 기간.
        SalesSeriesPoint(date="2026-07-01", sales=1000, order_count=1),
        SalesSeriesPoint(date="2026-07-02", sales=2000, order_count=2),
    ]
    fake = RecordingSalesClient(series)

    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-02", "granularity": "daily"},
        fake,
    )

    # 조회는 28일 앞당긴 2026-06-03 부터(설정 기본값 seller_analysis_lookback_days=28).
    assert fake.recorded_sales_args == ("2026-06-03", "2026-07-02", "daily")
    # 총매출·상세는 요청 기간(3,000원/3건)만 — lookback 의 9만원이 섞이면 안 된다.
    assert "총매출 3,000원" in result
    assert "주문 3건" in result
    assert "2026-06-21" not in result  # lookback 구간 수치·이상 미노출
    assert "기간 2026-07-01~2026-07-02" in result


async def test_sales_tool_non_daily_keeps_bucket_starting_before_from_date() -> None:
    """[PR 리뷰] weekly/monthly 버킷 date 가 버킷 시작일이라 요청 from 보다 이르더라도
    합계·상세에서 제외하지 않는다 — 요청 기간 필터는 lookback 확장을 한 daily 전용이다
    (I-6 계약에 버킷 date 의미 정의가 없어 검증 안 된 전제로 정상 버킷을 버리지 않는다)."""
    series = [
        # ISO 주 시작(월요일)이 요청 from(수요일)보다 이른 첫 버킷 — 정상 데이터다.
        SalesSeriesPoint(date="2026-06-29", sales=7000, order_count=7),
        SalesSeriesPoint(date="2026-07-06", sales=5000, order_count=5),
    ]
    fake = RecordingSalesClient(series)

    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-12", "granularity": "weekly"},
        fake,
    )

    assert "총매출 12,000원" in result  # 첫 버킷(7,000원) 포함 — 조용한 축소 없음
    assert "주문 12건" in result
    assert "2026-06-29" in result  # 상세 나열에서도 제외되지 않는다


async def test_sales_tool_non_daily_fetch_is_not_extended() -> None:
    """[#290] weekly/monthly 는 이상 감지를 안 하므로 lookback 확장도 없다 — 요청
    기간 그대로 조회한다(불필요한 집계 비용·구간 왜곡 방지)."""
    fake = RecordingSalesClient([SalesSeriesPoint(date="2026-07-01", sales=1000, order_count=1)])

    await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "granularity": "weekly"},
        fake,
    )

    assert fake.recorded_sales_args == ("2026-07-01", "2026-07-31", "weekly")


async def test_funnel_tool_fetches_previous_adjacent_period_and_reports_significance() -> None:
    """[#290] 퍼널은 직전 인접 동일 길이 기간을 자동 추가 조회하고, 단계별 Wilson CI 와
    z-검정 판정(유의한 하락/상승/변화없음)을 붙인다 — drop_pct 단순 임계 대체."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, fake
    )

    # 7일 요청 → 직전 7일(2026-07-01~2026-07-07)을 추가 조회한다.
    assert fake.funnel_calls == [("2026-07-08", "2026-07-14"), ("2026-07-01", "2026-07-07")]
    # 동일 fixture 를 두 번 받으므로 모든 단계는 "유의한 변화 없음"이다.
    assert "CI" in result
    assert "유의한 변화 없음" in result
    assert "유의한 하락" not in result
    assert "직전 기간 2026-07-01~2026-07-07 대비" in result


async def test_funnel_tool_survives_previous_period_failure() -> None:
    """[#290] 직전 기간 조회 실패는 보조 조회 실패 — 본 요약·CI 는 유지하고 비교만
    생략한다(§3.4 관용). 전체를 Error 로 격하하지 않는다."""

    class SecondCallFails(FakeSpringClient):
        async def get_funnel(self, brand_id, from_, to):
            self.funnel_calls.append((from_, to))
            if len(self.funnel_calls) > 1:
                raise SpringUnavailableError("Spring 콜백 타임아웃(3.0s): get_funnel")
            return FunnelResult(view=100, cart=10, checkout=5, purchase=3)

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, SecondCallFails()
    )

    assert not result.startswith("Error:")
    assert "전환율" in result and "CI" in result
    assert "기간 비교 생략" in result


async def test_funnel_tool_excludes_uncomputable_stage_from_test() -> None:
    """[#290+#184] 미집계 단계(checkout 등)는 CI·검정 대상에서 빠지고 '미집계'로
    표기된다 — 0% 로 위장해 유의성 검정에 들어가면 안 된다."""

    class UncomputableCheckout(FakeSpringClient):
        async def get_funnel(self, brand_id, from_, to):
            self.funnel_calls.append((from_, to))
            return FunnelResult(
                view=100, cart=10, checkout=0, purchase=3, uncomputable_stages=["checkout"]
            )

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, UncomputableCheckout()
    )

    assert "cart→checkout 미집계" in result
    assert "checkout→purchase 미집계" in result
    assert "view→cart 10.0%" in result  # 집계 가능한 단계는 정상 판정
    assert "판단 제외" in result  # 미집계 주의 문구 유지


async def test_churn_tool_fetches_previous_period_and_reports_significance() -> None:
    """[#290] 이탈률에 Wilson CI 가 붙고, 직전 인접 동일 길이 기간과의 z-검정 판정이
    보고된다. 신호는 코호트 대비 정규화 + 직전 대비 변화(%p)로 순위화된다."""
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=0.6,
        cohort_size=20,
        pre_churn_signals=PreChurnSignals(
            cancel_count=12,
            return_reasons_top=[{"reason": "사이즈 불만", "count": 5}],
            price_increase_exposed=8,
        ),
        members=[],
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, fake
    )

    assert fake.churn_calls[0][:2] == ("2026-07-08", "2026-07-14")
    assert fake.churn_calls[1][:2] == ("2026-07-01", "2026-07-07")
    assert fake.churn_calls[0][2] == fake.churn_calls[1][2]  # inactiveDays 동일 적용
    assert "이탈률 60.0%" in result and "CI" in result
    assert "유의한 변화 없음" in result  # 동일 fixture 2회 → 변화 없음
    # 신호 순위화: 취소(12) > 가격인상(8) > 반품(5) — 정규화 비중·직전 대비 병기.
    assert "1) 취소 12건·코호트 60.0%" in result
    assert "+0.0%p" in result  # 동일 fixture → 변화 0
    assert "상관이지 인과가 아니다" in result  # 주의 문구 상시 부착


async def test_funnel_tool_degrades_stage_with_inconsistent_counts() -> None:
    """[PR 리뷰] cart>view 같은 단계 역전 카운트(이벤트 유실로 실데이터 가능)가 와도
    도구는 raise 하지 않고(§3.4) 해당 단계만 판정 생략한다 — 나머지 단계·요약 유지."""

    class InvertedFunnel(FakeSpringClient):
        async def get_funnel(self, brand_id, from_, to):
            self.funnel_calls.append((from_, to))
            return FunnelResult(view=10, cart=25, checkout=5, purchase=3)  # cart > view

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, InvertedFunnel()
    )

    assert not result.startswith("Error:")  # raise 도 전체 Error 격하도 아니다
    assert "단계 카운트 정합 이상" in result  # view→cart 는 판정 생략 + 사유 표기
    assert "cart→checkout 20.0%" in result and "CI" in result  # 정합한 단계는 정상 판정
    assert "조회 10→장바구니 25" in result  # 원 카운트 요약은 유지(위장 없음)


async def test_funnel_tool_keeps_current_ci_when_previous_counts_inconsistent() -> None:
    """[PR 리뷰] 직전 기간 쪽만 카운트 정합 이상이면 현재 기간 CI 는 유지하고
    기간 비교만 생략한다(부분 degrade)."""

    class PrevInverted(FakeSpringClient):
        async def get_funnel(self, brand_id, from_, to):
            self.funnel_calls.append((from_, to))
            if len(self.funnel_calls) > 1:  # 직전 기간 응답만 역전
                return FunnelResult(view=10, cart=25, checkout=5, purchase=3)
            return FunnelResult(view=100, cart=10, checkout=5, purchase=3)

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, PrevInverted()
    )

    assert not result.startswith("Error:")
    assert "view→cart 10.0% [95% CI" in result  # 현재 기간 CI 유지
    assert "직전 기간 검정 불가 — 카운트 정합 이상" in result


async def test_churn_tool_holds_judgment_on_out_of_range_rate() -> None:
    """[PR 리뷰] churnRate 가 fraction [0,1] 밖(BE 정합 이상)이면 raise 없이 판정
    보류로 표기한다 — clamp 로 정상 CI 위장하지 않는다."""
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=1.2, cohort_size=10, pre_churn_signals=PreChurnSignals(), members=[]
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, fake
    )

    assert not result.startswith("Error:")
    assert "이탈률 값 이상" in result and "판정 보류" in result
    assert "CI" not in result  # 정합 깨진 값으로 CI 를 만들지 않는다


async def test_churn_tool_skips_comparison_on_out_of_range_previous_rate() -> None:
    """[PR 리뷰] 직전 기간 churnRate 가 구간 밖이면 비교만 생략한다(현재 판정 유지)."""

    class PrevBadRate(FakeSpringClient):
        async def get_churn(self, brand_id, from_, to, inactive_days):
            self.churn_calls.append((from_, to, inactive_days))
            if len(self.churn_calls) > 1:
                return ChurnResult(churn_rate=-0.3, cohort_size=10)
            return ChurnResult(
                churn_rate=0.5, cohort_size=10, pre_churn_signals=PreChurnSignals(), members=[]
            )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-08", "to_date": "2026-07-14"}, PrevBadRate()
    )

    assert not result.startswith("Error:")
    assert "이탈률 50.0%" in result and "CI" in result  # 현재 기간 판정 유지
    assert "직전 기간 비교 불가" in result


async def test_churn_tool_skips_comparison_when_previous_cohort_empty() -> None:
    """[#290] 직전 코호트 0명이면 비교 불가로 표기하고 현재 기간 판정은 유지한다."""

    class EmptyPreviousCohort(FakeSpringClient):
        async def get_churn(self, brand_id, from_, to, inactive_days):
            self.churn_calls.append((from_, to, inactive_days))
            if len(self.churn_calls) > 1:
                return ChurnResult(churn_rate=0.0, cohort_size=0)
            return ChurnResult(
                churn_rate=0.5, cohort_size=10, pre_churn_signals=PreChurnSignals(), members=[]
            )

    result = await _call_runtime_tool(
        get_churn_cohort,
        {"from_date": "2026-07-08", "to_date": "2026-07-14"},
        EmptyPreviousCohort(),
    )

    assert "이탈률 50.0%" in result and "CI" in result
    assert "직전 기간 비교 불가" in result
    assert "직전 −" in result  # 신호 변화율도 비교 불가 표기


async def test_get_order_events_tool_passes_stats_through() -> None:
    """도구의 stats 인자가 client.get_order_events 호출로 그대로 전달된다(opus 리뷰 m6)."""
    fake = FakeSpringClient()

    await _call_runtime_tool(
        get_order_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-14", "stats": True},
        fake,
    )

    assert fake.recorded_stats is True


class _FilterRecordingClient(FakeSpringClient):
    """[#215] get_order_events 로 전달된 필터 인자를 기록하는 이중 — 가드 검증용."""

    async def get_order_events(
        self, brand_id, from_, to, to_status=None, actor_type=None, group_by=None, stats=None
    ):
        self.recorded_brand_id = brand_id
        self.recorded_filters = (to_status, actor_type, group_by)
        return self.order_events_result


async def test_order_events_member_agg_guard_strips_denominator_filters() -> None:
    """[#215] memberId 집계 가드 — to_status/actor_type 이 Spring 호출에서 None 으로
    씻겨 전달된다(분모 오염 차단). 프롬프트·docstring 은 소프트 가드라 코드 동작을
    테스트로 고정한다(리팩터링 회귀 방지)."""
    fake = _FilterRecordingClient()

    result = await _call_runtime_tool(
        get_order_events,
        {
            "from_date": "2026-07-01",
            "to_date": "2026-07-14",
            "to_status": "CANCELLED",
            "actor_type": "USER",
            "group_by": "memberId",
        },
        fake,
    )

    assert fake.recorded_filters == (None, None, "memberId")
    assert "무시됨" in result  # 무시 사실이 요약에 고지된다


async def test_order_events_member_agg_guard_normalizes_group_by_variants() -> None:
    """[#215] group_by 는 LLM 자유 문자열 — 대소문자·공백 변형("memberid" 등)에도
    가드가 작동하고, Spring 에는 정규화된 "memberId" 로 전달된다(등호 비교 라우팅)."""
    fake = _FilterRecordingClient()

    await _call_runtime_tool(
        get_order_events,
        {
            "from_date": "2026-07-01",
            "to_date": "2026-07-14",
            "to_status": "CANCELLED",
            "group_by": " MemberID ",
        },
        fake,
    )

    assert fake.recorded_filters == (None, None, "memberId")


async def test_order_events_filters_pass_through_without_member_agg() -> None:
    """[#215] 가드는 memberId 집계에만 작동 — 목록 조회의 정당한 필터는 그대로 전달된다."""
    fake = _FilterRecordingClient()

    result = await _call_runtime_tool(
        get_order_events,
        {
            "from_date": "2026-07-01",
            "to_date": "2026-07-14",
            "to_status": "CANCELLED",
            "actor_type": "USER",
        },
        fake,
    )

    assert fake.recorded_filters == (["CANCELLED"], "USER", None)
    assert "무시됨" not in result


async def test_sales_tool_includes_point_detail_and_caps_output() -> None:
    """시계열 상세 나열(안 1, 2026-07-17 확정) — 포인트별 수치를 포함하되
    seller_summary_max_points 초과분은 "외 N개 포인트 생략" 으로 접는다."""

    class LongSeriesClient(FakeSpringClient):
        """상한(기본 60)을 넘는 90일 시계열을 반환하는 이중."""

        async def get_sales(self, brand_id, from_, to, granularity="daily"):
            self.recorded_brand_id = brand_id
            return SalesResult(
                series=[
                    SalesSeriesPoint(date=f"2026-04-{(d % 30) + 1:02d}", sales=100, order_count=1)
                    for d in range(90)
                ]
            )

    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-04-01", "to_date": "2026-06-29"},
        LongSeriesClient(),
    )

    assert "100원/1건" in result  # 포인트 상세가 포함된다
    assert "외 30개 포인트 생략" in result  # 90 - 상한 60 = 30


class FlatSeriesClient(FakeSpringClient):
    """[#512] 길이·수량을 지정해 평탄한 일별 시계열을 돌려주는 이중."""

    def __init__(self, days: int, *, sales_count: int | None = None) -> None:
        super().__init__()
        self._days = days
        self._sales_count = sales_count

    async def get_sales(self, brand_id, from_, to, granularity="daily"):
        self.recorded_brand_id = brand_id
        return SalesResult(
            series=[
                SalesSeriesPoint(
                    date=f"2026-07-{day:02d}",
                    sales=100,
                    order_count=1,
                    sales_count=self._sales_count,
                )
                for day in range(1, self._days + 1)
            ]
        )


async def test_sales_tool_rejects_summary_granularity() -> None:
    """[#512] granularity=summary 는 조회 전에 차단한다 — "총매출 0원" 을 만들지 않는다.

    I-6 summary 응답에는 `series` 가 없고 SalesResult 는 extra="allow" 라, 호출하면
    ValidationError 도 degrade 도 없이 series=[] 로 파싱돼 언제나 0원이 나갔다.
    정상값과 구별되지 않는 0 을 내보내느니 명시적으로 거절한다.
    """
    fake = FakeSpringClient()
    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-14", "granularity": "summary"},
        fake,
    )

    assert result.startswith("Error:")
    assert "summary" in result
    assert "0원" not in result
    assert fake.recorded_brand_id is None, "Spring 을 호출하기 전에 막아야 한다"


async def test_sales_tool_rejects_non_canonical_iso_dates() -> None:
    """[#512] 파싱되지만 정규형이 아닌 ISO 날짜(기본형·주 표기)는 즉시 오류다.

    `date.fromisoformat` 은 "20260801"·"2026W311" 을 받는다. 그 원문이 window 필터
    경계값(`p.date >= from_date`)이 되면 사전식 비교가 어긋나 전 포인트가 탈락하고
    "총매출 0원" 이 오류 없이 나갔다 — Spring 조회 전에 끊는다.
    """
    for from_date, to_date in (
        ("20260701", "2026-07-14"),  # 기본형(무하이픈)
        ("2026-07-01", "2026W311"),  # 주 표기 — to_date 는 종전에 본문 검증 자체가 없었다
        ("2026-7-1", "2026-07-14"),  # 무패딩(종전에도 차단, 회귀 확인)
    ):
        fake = FakeSpringClient()
        result = await _call_runtime_tool(
            get_sales_timeseries, {"from_date": from_date, "to_date": to_date}, fake
        )

        assert result.startswith("Error:"), f"{from_date}~{to_date} 가 통과했다"
        assert "YYYY-MM-DD" in result
        assert "총매출" not in result
        assert fake.recorded_brand_id is None


async def test_sales_tool_holds_judgment_when_samples_too_few() -> None:
    """[#512] 표본 3개 미만은 "이상 감지 없음" 이 아니라 "판정 보류" 다.

    워커 프롬프트가 정확히 금지하는 것 — "판정 보류(표본 부족·미집계·결측)는
    이상 없음과 다르다". 같은 파일의 Tukey 경로가 이미 쓰던 어휘를 따른다.
    """
    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-02", "granularity": "daily"},
        FlatSeriesClient(2),
    )

    assert "이상 감지 판정 보류(표본 2개 < 최소 3개)." in result
    assert "이상 감지 없음" not in result
    assert "총매출 200원" in result  # 매출 요약 자체는 그대로 나간다


async def test_sales_tool_keeps_no_anomaly_wording_when_decided() -> None:
    """[#512 회귀] 표본이 충분하고 이상 0건이면 종전 문구를 글자 그대로 유지한다."""
    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-20", "granularity": "daily"},
        FlatSeriesClient(20),
    )

    assert "이상 감지 없음(STL 계절조정·GESD)." in result
    assert "판정 보류" not in result


async def test_sales_tool_keeps_sales_count_note_with_new_detection_result() -> None:
    """[#489 회귀] 반환 타입 교체(#512)가 salesCount 표기 경로를 건드리지 않았다."""
    result = await _call_runtime_tool(
        get_sales_timeseries,
        {"from_date": "2026-07-01", "to_date": "2026-07-20", "granularity": "daily"},
        FlatSeriesClient(20, sales_count=2),
    )

    assert "판매 40개" in result  # 20 포인트 × 2개 — 전량 집계라 비율 주석 없음
    assert "개 포인트 집계)" not in result


async def test_order_events_tool_summarizes_kv_with_cap() -> None:
    """[#194] I-14 rows(BE 실측)를 kv 로 상위 N건 노출 — Row/MemberRow 이형 대응."""

    class EventfulClient(FakeSpringClient):
        """상한(기본 5)을 넘는 7건 rows 를 반환하는 이중."""

        async def get_order_events(
            self, brand_id, from_, to, to_status=None, actor_type=None, group_by=None, stats=None
        ):
            self.recorded_brand_id = brand_id
            self.recorded_stats = stats
            return OrderEventsResult(
                rows=[{"orderId": 5000 + i, "toStatus": "CANCELLED"} for i in range(7)],
                total=7,
            )

    result = await _call_runtime_tool(
        get_order_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, EventfulClient()
    )

    assert "toStatus=CANCELLED" in result  # kv 노출
    assert "외 2건" in result  # 7 - 상한 5 = 2


async def test_order_events_tool_notes_total_when_rows_truncated() -> None:
    """[#194] rows 는 limit 절단본 — total 이 더 크면 전수를 고지해 표본=전수 오해석을 막는다."""

    class TruncatedClient(FakeSpringClient):
        async def get_order_events(
            self, brand_id, from_, to, to_status=None, actor_type=None, group_by=None, stats=None
        ):
            self.recorded_brand_id = brand_id
            return OrderEventsResult(
                rows=[{"orderId": i, "toStatus": "PAID"} for i in range(3)], total=250
            )

    result = await _call_runtime_tool(
        get_order_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, TruncatedClient()
    )

    assert "전체 250건 중 3건 표시" in result


async def test_order_events_tool_stats_mode_summarizes_by_status_and_reasons() -> None:
    """[#194] stats=true 응답(byStatus + cancelReasonsTop)을 집계 노트로 노출한다 —
    구 코드는 Spring 에 없는 stats 필드를 읽어 집계 질의가 항상 0건이었다."""

    class StatsClient(FakeSpringClient):
        async def get_order_events(
            self, brand_id, from_, to, to_status=None, actor_type=None, group_by=None, stats=None
        ):
            self.recorded_brand_id = brand_id
            self.recorded_stats = stats
            return OrderEventsResult(
                by_status={"PAID": 120, "CANCELLED": 7},
                cancel_reasons_top=[{"reason": "단순변심", "count": 4}],
            )

    result = await _call_runtime_tool(
        get_order_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-14", "stats": True},
        StatsClient(),
    )

    assert "PAID=120" in result and "CANCELLED=7" in result
    assert "단순변심(4건)" in result
    assert "주문 상태 전이 0건" not in result


async def test_list_products_uses_default_limit_from_settings() -> None:
    """limit 미지정 시 Settings 기본값(seller_list_default_limit)으로 요청한다."""
    from app.core.config import get_settings

    fake = FakeSpringClient()
    await _call_runtime_tool(list_my_products, {}, fake)

    assert fake.recorded_limit == get_settings().seller_list_default_limit


# [#620] update_product 도구(및 ProductUpdate.category 인자) 테스트는 제거됐다 — 도구
# 자체가 죽은 코드로 확인돼 삭제됐고, category 필드도 스키마에서 빠졌다
# (app/agents/seller/hitl.py 의 validate_draft 가 카드 표시 전에 선차단한다).

# ── I-13 행동 이벤트 도구 (REALIGN ②-3 — 07/17 확정 명세) ──


async def test_behavior_tool_passes_filters_to_client() -> None:
    """eventType(복수)/productId(숫자)/groupBy 가 client 까지 그대로 전달된다."""
    fake = FakeSpringClient()

    await _call_runtime_tool(
        get_behavior_events,
        {
            "from_date": "2026-07-01",
            "to_date": "2026-07-14",
            "event_type": ["product_view", "add_to_cart"],
            "product_id": 101,
            "group_by": "date",
        },
        fake,
    )

    assert fake.recorded_event_args == (["product_view", "add_to_cart"], 101, "date")


async def test_behavior_tool_summarizes_product_rows_with_purchase_rules_note() -> None:
    """groupBy=product — 상품별 카운트 요약 + purchaseComplete 집계 규칙 문구."""
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=101,
                product_name="에어 러너 2",
                counts={
                    "productView": 1820,
                    "addToCart": 240,
                    "checkoutStart": 96,
                    "purchaseComplete": 61,
                },
                view_to_cart_rate=0.132,
                unique_visitors=1503,
            )
        ],
        total=1,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "[101] 에어 러너 2" in result
    assert "조회 1820" in result and "담기 240" in result
    assert "13.2%" in result  # viewToCartRate 백분율 표기
    # [#488] purchaseComplete 집계 규칙 노트 — 권위 위임이 아니라 단위 고지다.
    assert "주문 기준 집계" in result
    assert "건수이지 수량이 아니" in result  # ① 수량 오용 차단
    assert "합계(eventType 집계)보다 클 수 있다" in result  # ② 상품별 합 > 합계


def _behavior_row(pid: int, view: int, cart: int, checkout: int, purchase: int):
    """군집화 테스트용 I-13 상품 행 헬퍼(#290)."""
    return BehaviorProductRow(
        product_id=pid,
        product_name=f"상품{pid}",
        counts={
            "productView": view,
            "addToCart": cart,
            "checkoutStart": checkout,
            "purchaseComplete": purchase,
        },
        view_to_cart_rate=(cart / view) if view else None,
        unique_visitors=max(1, view // 2),
    )


async def test_behavior_tool_appends_cluster_labels_for_product_rows() -> None:
    """[#290] 상품 수가 충분하면 k-means 군집 요약(규칙 라벨·중심 비율)이 붙는다 —
    LLM 이 상품 나열이 아니라 군집 단위로 해석·액션 연결하게."""
    rows = []
    for i in range(6):  # 전환직결형 패턴
        rows.append(_behavior_row(100 + i, 200 + 10 * i, 80 + 4 * i, 60 + 3 * i, 50 + 2 * i))
    for i in range(6):  # 카트이탈형 패턴(담기율 높고 결제 진입 급락)
        rows.append(_behavior_row(200 + i, 220 + 10 * i, 90 + 4 * i, 5 + i, 2))
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(group_by="product", rows=rows, total=len(rows))

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "행동 군집" in result and "실루엣" in result
    assert "카트이탈형" in result
    assert "담기율" in result and "결제진입률" in result
    assert "주문 기준 집계" in result  # 기존 노트(#488 교체분) 유지


async def test_behavior_tool_skips_clustering_for_few_products_with_reason() -> None:
    """[#290] 상품 수 미달이면 군집을 생략하되 사유를 명시한다 — '군집 없음 = 패턴
    없음' 오해석 방지."""
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[_behavior_row(100 + i, 100, 10, 5, 2) for i in range(3)],
        total=3,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "군집 생략" in result
    assert "상품 3개" in result
    assert "행동 군집" not in result


async def test_behavior_tool_date_series_flags_spike_with_price_change_overlap() -> None:
    """[#290 abuse Point 트랙] date-groupBy 일별 총량의 MAD 스파이크를 표기하고,
    스파이크일이 가격/재고 변경일과 겹치면 '정상 설명 후보'를 동봉한다(오탐 통제)."""

    class SpikyWithPriceChange(FakeSpringClient):
        async def get_events(
            self, brand_id, from_, to, event_type=None, product_id=None, group_by=None
        ):
            self.recorded_brand_id = brand_id
            series = [
                {"date": f"2026-07-{d:02d}", "productView": 100, "addToCart": 10}
                for d in range(1, 10)
            ]
            series.append({"date": "2026-07-10", "productView": 5000, "addToCart": 400})
            return BehaviorEventsResult(group_by="date", series=series)

        async def get_product_changes(self, brand_id, from_, to, change_type=None, product_id=None):
            return ProductChangeLogResult(
                rows=[
                    ProductChangeLogRow(
                        product_id=7,
                        change_type="PRICE",
                        old_value="20000",
                        new_value="9900",
                        created_at="2026-07-10T09:00:00+09:00",
                    )
                ],
                total=1,
            )

    result = await _call_runtime_tool(
        get_behavior_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-10", "group_by": "date"},
        SpikyWithPriceChange(),
    )

    assert "일별 볼륨 스파이크 1건" in result
    assert "2026-07-10" in result and "robust z" in result
    assert "정상 설명 후보" in result  # 가격 인하와 겹침 — 봇 단정 오탐 통제


async def test_behavior_tool_date_series_survives_change_log_failure() -> None:
    """[#290] I-15 대조 실패는 보조 실패 — 스파이크 보고는 유지, 겹침 미확인만 고지."""

    class SpikyChangeLogFails(FakeSpringClient):
        async def get_events(
            self, brand_id, from_, to, event_type=None, product_id=None, group_by=None
        ):
            series = [{"date": f"2026-07-{d:02d}", "productView": 100} for d in range(1, 10)]
            series.append({"date": "2026-07-10", "productView": 5000})
            return BehaviorEventsResult(group_by="date", series=series)

        async def get_product_changes(self, brand_id, from_, to, change_type=None, product_id=None):
            raise SpringUnavailableError("Spring 콜백 타임아웃(3.0s): get_product_changes")

    result = await _call_runtime_tool(
        get_behavior_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-10", "group_by": "date"},
        SpikyChangeLogFails(),
    )

    assert not result.startswith("Error:")
    assert "일별 볼륨 스파이크 1건" in result
    assert "겹침 미확인" in result


async def test_behavior_tool_product_rows_flag_ratio_outliers() -> None:
    """[#290 abuse Contextual 트랙] 브랜드 내 비율 분포의 Tukey 상위 fence 초과 상품을
    표기한다 — '조회 폭증+구매 0' 패턴은 패턴명을 병기한다."""
    rows = [_behavior_row(100 + i, 100, 10, 5, 4) for i in range(6)]
    rows.append(_behavior_row(999, 5000, 12, 1, 0))  # 조회 폭증 + 구매 0
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(group_by="product", rows=rows, total=len(rows))

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "상품 비율 이상치" in result
    assert "[999]" in result
    assert "구매 0 — 조회 폭증 패턴" in result


async def test_behavior_tool_ratio_outlier_not_masked_by_zero_purchase_volume() -> None:
    """[PR 리뷰] 구매 0 대량 조회 상품이 '조회/구매' 분포를 부풀려 진짜 비율 이상치를
    가리던 오미탐 회귀 — 비율 트랙(purchase>0 전용)과 구매 0 조회 폭증 트랙(브랜드
    조회량 분포)을 분리해 둘 다 잡는다."""
    rows = [_behavior_row(100 + i, 500, 50, 25, 5) for i in range(6)]  # 조회/구매 = 100
    rows.append(_behavior_row(777, 5000, 60, 30, 10))  # 조회/구매 = 500 — 진짜 비율 이상치
    rows.append(_behavior_row(999, 4000, 10, 1, 0))  # 구매 0 + 대량 조회 — 별도 트랙
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(group_by="product", rows=rows, total=len(rows))

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    # 구 방식이면 [999]의 4,000(원시 조회수)이 분포에 섞여 [777](500)이 fence 아래 숨었다.
    assert "[777] 조회/구매 500.0" in result
    assert "[999]" in result and "구매 0 — 조회 폭증 패턴" in result


def test_abuse_prompt_mandates_date_group_by_for_point_track() -> None:
    """[PR 리뷰] Point 트랙(일별 볼륨 스파이크)은 group_by="date" 호출에만 붙는다 —
    abuse 프롬프트가 그 호출을 명시하지 않으면 트랙이 확률적으로 통째로 빠진다."""
    from app.agents.seller.prompts import ABUSE_PROMPT

    assert 'group_by="date"' in ABUSE_PROMPT
    assert "Point 트랙" in ABUSE_PROMPT


async def test_account_events_hour_group_reports_night_share() -> None:
    """[#290 abuse Collective 트랙] hour-groupBy 는 심야(0~6시) 활동 비중을 계산해
    붙인다 — 심야 편중은 봇 신호(Tan & Kumar)다."""
    fake = FakeSpringClient()
    fake.account_events_result = AccountEventsResult(
        group_by="hour",
        rows=[{"key": 2, "count": 300}, {"key": 4, "count": 200}, {"key": 15, "count": 500}],
    )

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "hour"}, fake
    )

    assert "심야(0~6시) 활동 비중 50.0%(500/1000건)" in result


async def test_account_events_ip_group_sorts_by_suspicious_member_count() -> None:
    """[#290 abuse Collective 트랙, #481 개정] ip-groupBy 는 suspiciousMemberCount
    내림차순으로 정렬해 다계정 정황을 상단에 노출하고 합계를 요약한다 —
    구 failCount·isSuspicious 는 2026-08-06 개정으로 응답에서 제거됐다."""
    fake = FakeSpringClient()
    fake.account_events_result = AccountEventsResult(
        group_by="ip",
        scope="brand",
        rows=[
            {"ipMasked": "1.2.3.*", "suspiciousMemberCount": 0, "distinctMembers": 2},
            {"ipMasked": "9.9.9.*", "suspiciousMemberCount": 5, "distinctMembers": 7},
        ],
    )

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "ip"}, fake
    )

    assert result.index("9.9.9.*") < result.index("1.2.3.*")  # suspiciousMemberCount 상위 우선
    assert "교차 회원 합계 5명" in result
    assert "특정 회원 지목 불가" in result


async def test_behavior_tool_shows_all_seed_products_within_cap() -> None:
    """[#196] 상품별 rows 상한을 I-13 전용 seller_summary_max_products(10)로 분리 —
    시드 브랜드 7종이 구 공용 상한(5)에 잘리지 않고 전부 상세 노출된다."""
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=100 + i,
                product_name=f"상품{i}",
                counts={"productView": 700 - i * 10},
            )
            for i in range(7)
        ],
        total=7,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "상품별 7건" in result
    assert "[106] 상품6" in result  # 구 상한 5 에서 상시 잘리던 하위 상품도 노출
    assert "외" not in result.split("※")[0]  # 상한 내 — 꼬리 합계 없음


async def test_behavior_tool_caps_product_rows_with_tail_totals() -> None:
    """[#196] 상한(10) 초과 rows 는 '외 N건(저활동) 합계' 로 압축 — 개수만 남기고
    수치가 소실되던 구 '외 N건' 표기를 대체한다(잘림 = 표본 누락 → 요약)."""
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=200 + i,
                product_name=f"상품{i}",
                counts={
                    "productView": 120 - i * 10,
                    "addToCart": 12 - i,
                    "checkoutStart": 2,
                    "purchaseComplete": 0,
                },
            )
            for i in range(12)  # BE 정렬(활동량 내림차순) 그대로 12건
        ],
        total=12,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "상품별 10건" in result  # 상한 = seller_summary_max_products
    assert "[209] 상품9" in result and "[210]" not in result  # 11번째부터 접힘
    # 꼬리 = 11·12번째 행 합계: 조회 20+10, 담기 2+1, 삭제 0, 결제시작 2+2, 구매 0.
    # [#489] removeFromCart 편입으로 4종 → 5종 — 키 목록은 _BEHAVIOR_COUNT_KEYS 단일 출처.
    assert "외 2건(저활동) 합계: 조회 30 담기 3 삭제 0 결제시작 4 구매 0" in result
    # 전 행 salesQuantity=None(미조회) 이면 수량 꼬리 합계는 아예 붙지 않는다 —
    # null 을 0 으로 섞어 "0개 팔림"으로 오독시키지 않는다.
    assert "판매 0개(" not in result


async def test_behavior_tool_shows_new_row_fields() -> None:
    """[#489] 상품 행에 removeFromCart·salesQuantity·체류시간이 함께 실린다.

    개정 전에는 AI 가 판매 수량에 도달할 경로가 하나도 없었다 — 수신만 하고 표기하지
    않으면 데드 필드라 이슈 목적이 절반만 달성된다.
    """
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=101,
                product_name="에어 러너 2",
                counts={
                    "productView": 1820,
                    "addToCart": 240,
                    "removeFromCart": 35,
                    "checkoutStart": 96,
                    "purchaseComplete": 61,
                },
                sales_quantity=74,
                median_dwell_seconds=42.0,
                avg_dwell_seconds=71.3,
                dwell_sample_count=1180,
                dwell_source="next_event",
                view_to_cart_rate=0.132,
                unique_visitors=1503,
            )
        ],
        total=1,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "삭제 35" in result  # 4종 → 5종 편입분
    assert "판매 74개" in result  # 수량 — 구매 61건과 단위가 다르다
    assert "체류 중앙 42초·평균 71초(n=1180)" in result  # median 이 주 지표라 앞
    # dwellSource 한계는 행마다 반복하지 않고 요약 말미에 1회 각주로.
    assert result.count("세션의 마지막 조회가 표본에서 빠진다") == 1
    # 수량 권위 문구 — purchaseComplete 경고가 신설 지표까지 싸잡아 불신시키지 않게.
    # [#488 병합] 권위 노트가 주문 기준 규칙으로 교체됐다 — ① 항이 수량 질문을
    # 막기만 하지 않고 salesQuantity 로 보내는지 확인한다.
    assert "수량은 같은 행의 salesQuantity" in result


async def test_behavior_tool_distinguishes_zero_and_null_sales_quantity() -> None:
    """[#489] salesQuantity 0("안 팔림")과 null("미조회")을 절대 뭉개지 않는다.

    `x or '-'` 같은 falsy 축약을 쓰면 0 이 '-' 로 뭉개져 churn_rate 에서 잡았던
    silent-mismatch(#197)를 그대로 재도입한다. 이 테스트가 그 회귀를 잡는다.
    """
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(product_id=101, product_name="안팔린상품", sales_quantity=0),
            BehaviorProductRow(product_id=102, product_name="미조회상품", sales_quantity=None),
        ],
        total=2,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    sold_none, unmeasured = result.split("[102]")[0], result.split("[102]")[1]
    assert "판매 0개" in sold_none  # 조회했고 값이 0 = 안 팔림
    assert "판매수량 -(미조회)" in unmeasured  # BE 가 계산조차 안 함
    assert "판매수량 -(미조회)" not in sold_none


async def test_behavior_tool_hides_dwell_without_sample_count() -> None:
    """[#489] dwellSampleCount 없이 평균·중앙값만 내보내지 않는다.

    명세: 표본 없이 해석 금지(conversion 워커 유의성 판정 원칙과 동일). 표본이 0/None
    이면 수치가 실려 와도 감추고 사유만 남겨 LLM 이 표본 1건짜리 중앙값을 근거로
    쓰지 않게 한다.
    """
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=101,
                product_name="표본없음",
                median_dwell_seconds=42.0,
                avg_dwell_seconds=71.3,
                dwell_sample_count=0,
            )
        ],
        total=1,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "체류 -(표본 없음)" in result
    assert "42초" not in result and "71초" not in result


async def test_behavior_tool_tail_totals_sum_measured_sales_quantity_only() -> None:
    """[#489] 꼬리 수량 합계는 null(미조회) 행을 0 으로 섞지 않고 집계 건수를 밝힌다."""
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=200 + i,
                product_name=f"상품{i}",
                counts={"productView": 120 - i * 10, "removeFromCart": 1},
                # 11번째만 수량이 있고 12번째는 미조회 — 합계는 5, 표기는 1/2건.
                sales_quantity=5 if i == 10 else None,
            )
            for i in range(12)
        ],
        total=12,
    )

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "삭제 2" in result.split("외 2건(저활동) 합계:")[1]  # 꼬리 5종 합계
    assert "판매 5개(1/2건 집계)" in result


async def test_behavior_tool_summarizes_event_type_counts() -> None:
    """groupBy=eventType — counts 합계 요약."""
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="eventType",
        counts={"productView": 8120, "addToCart": 1490},
    )

    result = await _call_runtime_tool(
        get_behavior_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-14", "group_by": "eventType"},
        fake,
    )

    assert "productView=8120" in result and "addToCart=1490" in result


async def test_behavior_tool_caps_date_series_by_settings() -> None:
    """groupBy=date — seller_summary_max_points 초과분은 '외 N일'로 접는다."""
    from app.core.config import get_settings

    cap = get_settings().seller_summary_max_points
    fake = FakeSpringClient()
    fake.behavior_result = BehaviorEventsResult(
        group_by="date",
        series=[{"date": f"2026-07-{d:02d}", "productView": d} for d in range(1, cap + 4)],
    )

    result = await _call_runtime_tool(
        get_behavior_events,
        {"from_date": "2026-07-01", "to_date": "2026-09-30", "group_by": "date"},
        fake,
    )

    assert "외 3일" in result


async def test_behavior_tool_empty_result() -> None:
    """3형 모두 비어 있으면 0건 안내."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "행동 이벤트 0건" in result


# ── I-14/I-15 기록 규칙 주의 문구 (REALIGN ②-4 — D32/D34 해석 규칙) ──


async def test_order_events_output_includes_log_rules_note() -> None:
    """전이가 있으면 기록 규칙 주의(완료만 기록·아이템 단위 행·customerLabel
    사례번호 규약, #481 개정)가 함께 나간다."""
    fake = FakeSpringClient()
    fake.order_events_result = OrderEventsResult(
        rows=[
            {
                "orderId": 5001,
                "orderItemId": 5551,
                "toStatus": "CANCELLED",
                "actorType": "USER",
                "customerLabel": "A3F29C",
            }
        ],
        total=1,
    )

    result = await _call_runtime_tool(
        get_order_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "구매확정·클레임 신청은 로그에 없음" in result
    assert "같은 orderId 행 복수는 아이템별 전이(중복 아님)" in result
    assert "customerLabel은 개인정보 보호용 사례번호" in result
    assert "사례번호 XXXXXX로 관리자 문의" in result


async def test_product_change_logs_output_includes_log_rules_note() -> None:
    """상품 변경 이력 응답에 기록 규칙 주의(재고 차감 미기록·품절=STOCK→0)가 나간다."""
    from app.agents.seller.tools import get_product_change_logs

    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_product_change_logs, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "주문에 의한 재고 차감은 미기록" in result
    assert "new_value 0" in result


async def test_product_change_logs_lists_details_with_cap() -> None:
    """[#194] I-15 rows(BE 실측)를 변경 상세(무엇이 언제 어떻게)로 나열한다 — 건수만 주던
    구 출력으로는 워커가 '이상 직전 가격/재고/상태 변경'을 짚을 수 없었다."""
    from app.agents.seller.tools import get_product_change_logs

    class ChangefulClient(FakeSpringClient):
        async def get_product_changes(self, brand_id, from_, to, change_type=None, product_id=None):
            self.recorded_brand_id = brand_id
            rows = [
                ProductChangeLogRow(
                    product_id=101,
                    product_name="린넨 셔츠",
                    change_type="PRICE",
                    old_value="29000",
                    new_value="19000",
                    created_at="2026-07-10T09:00:00+09:00",
                )
            ] + [
                ProductChangeLogRow(
                    product_id=200 + i, change_type="STOCK", old_value="10", new_value="0"
                )
                for i in range(6)
            ]
            return ProductChangeLogResult(rows=rows, total=8)  # rows 7건, 전수 8건(절단)

    result = await _call_runtime_tool(
        get_product_change_logs,
        {"from_date": "2026-07-01", "to_date": "2026-07-14"},
        ChangefulClient(),
    )

    assert "상품 변경 이력 8건" in result  # 전수는 total 기준
    assert "[101] 린넨 셔츠 PRICE 29000→19000" in result  # 변경 상세 노출
    assert "외 3건" in result  # 8 - 상한 5 = 3


async def test_product_change_logs_empty_says_zero() -> None:
    """[#194] rows 가 비면 0건 안내 + 기록 규칙 주의는 유지된다."""
    from app.agents.seller.tools import get_product_change_logs

    result = await _call_runtime_tool(
        get_product_change_logs,
        {"from_date": "2026-07-01", "to_date": "2026-07-14"},
        FakeSpringClient(),
    )

    assert "상품 변경 이력 0건" in result


# ── I-16 이탈 코호트 도구 (#197) ──────────────────────────────────────────────


async def test_churn_tool_passes_period_and_formats_fraction_rate_as_percent() -> None:
    """[#197 회귀] from/to 가 client 에 전달되고, fraction 이탈률이 % 로 표시된다.

    구 코드는 (1) from/to 미전달로 무조건 400 degrade, (2) ":.1f}%" 포맷으로
    0.6(=60%)을 "0.6%" 로 왜곡해 워커가 이탈 미미로 오판했다.
    """
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=0.6, cohort_size=5, pre_churn_signals=PreChurnSignals(), members=[]
    )

    result = await _call_runtime_tool(
        get_churn_cohort,
        {"from_date": "2026-06-01", "to_date": "2026-07-31", "inactive_days": 45},
        fake,
        brand_id=93,
    )

    assert fake.recorded_brand_id == 93
    assert fake.recorded_churn_args == ("2026-06-01", "2026-07-31", 45)
    assert "이탈률 60.0%" in result
    assert "0.6%" not in result  # 구 왜곡 표기 회귀 방지
    assert "코호트 5명" in result
    assert "2026-06-01~2026-07-31" in result  # _reference_note 부착


async def test_churn_tool_defaults_inactive_days_from_settings() -> None:
    """inactive_days 미지정 시 Settings 기본값(seller_churn_inactive_days)을 쓴다."""
    from app.core.config import get_settings

    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake
    )

    expected = get_settings().seller_churn_inactive_days
    assert fake.recorded_churn_args == ("2026-07-01", "2026-07-31", expected)
    assert f"inactiveDays={expected}" in result


async def test_churn_tool_summarizes_signals_and_members() -> None:
    """[#197] 이탈 전 신호 상세(취소·반품 사유·가격인상 노출)와 이탈 회원 요약을 노출한다.

    구 출력은 "신호 N건" 건수뿐이라 워커가 원인 가설을 세울 재료가 없었다.
    검색 무결과 세션 상시 0(미적재) 주의 문구도 상시 부착된다.
    """
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=0.6,
        cohort_size=5,
        pre_churn_signals=PreChurnSignals(
            cancel_count=3,
            return_reasons_top=[{"reason": "사이즈 불만", "count": 2}],
            zero_result_search_sessions=0,
            price_increase_exposed=2,
        ),
        members=[
            ChurnMember(
                customer_label="A3F29C",
                last_activity_at="2026-06-15T10:00:00+09:00",
                sessions_30d=0,
                pre_churn_event="RETURNED(상품불량)",
            ),
            ChurnMember(customer_label="B71D04", last_activity_at="2026-06-01T09:00:00+09:00"),
        ],
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert "취소 3건" in result
    assert "사이즈 불만(2건)" in result
    assert "가격인상 노출 2명" in result
    assert "이탈 회원 2명" in result
    # [#487] 회원 노출은 customerLabel(사례번호)뿐 — 구 memberId 표기는 폐기.
    assert "[A3F29C]" in result and "RETURNED(상품불량)" in result
    assert "[B71D04]" in result
    assert "쓰지 말 것" in result  # _CHURN_SIGNAL_RULES_NOTE 상시 부착


async def test_churn_tool_never_exposes_raw_member_id_from_legacy_response() -> None:
    """[#487] 구응답(memberId 포함·customerLabel 부재)을 먹여도 요약에 원시 회원 키가
    등장하지 않는다 — 라벨 결측은 "[라벨없음]"으로만 떨어진다(#495 표기).

    ChurnMember 는 SellerAggregateModel(extra="allow") 상속이라 BE 미배포 구간의
    구응답이 와도 ValidationError 없이 model_extra 로 흡수된다. 이 테스트가 지키는
    것은 "흡수된 값이 표시 계층으로 새지 않는다"는 것 — memberId 폴백을 되살리면
    여기서 깨진다(#487 이 고친 결함 그 자체).
    """
    fake = FakeSpringClient()
    # 코호트 규모·비율·날짜와 우연히 겹치지 않도록 6자리 구분값을 쓴다.
    fake.churn_result = ChurnResult.model_validate(
        {
            "cohortSize": 5,
            "churnRate": 0.6,
            "preChurnSignals": {},
            "members": [
                {
                    "memberId": 987654,
                    "lastActivityAt": "2026-06-15T10:00:00+09:00",
                    "lastLoginAt": "2026-06-10T10:00:00+09:00",
                    "sessions30d": 0,
                    "preChurnEvent": "RETURNED(상품불량)",
                }
            ],
        }
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert "987654" not in result  # 원시 회원 키가 LLM 표면에 실리지 않는다
    assert "[라벨없음]" in result  # [#495] 라벨 미수신은 전용 어휘로만 떨어진다
    # [#495] 같은 줄의 다른 결측('?')과 섞이면 개명 미반영 버그(#487 증상)와 구분되지 않는다.
    assert "[?]" not in result
    assert "이탈 회원 1명" in result  # 흡수 자체는 성공 — 항목이 사라지는 게 아니다


async def test_customer_label_note_attached_to_both_order_and_churn_outputs() -> None:
    """[#487] 사례번호 규약 문구는 상수 1벌(_CUSTOMER_LABEL_NOTE)로 I-14·I-16 양쪽
    출력에 붙는다 — 복붙본이 갈라져 한쪽 경로의 규약만 낡는 것을 막는다."""
    from app.agents.seller.tools import _CUSTOMER_LABEL_NOTE, _ORDER_LOG_RULES_NOTE

    # I-14 기록 규칙 노트는 같은 문구를 같은 자리(맨 끝)에 그대로 유지한다(무회귀).
    assert _ORDER_LOG_RULES_NOTE.endswith(_CUSTOMER_LABEL_NOTE)

    fake = FakeSpringClient()
    # rows 가 비면 "0건" 조기 반환 경로라 기록 규칙 노트가 붙지 않는다 — 목록 경로로 태운다.
    fake.order_events_result = OrderEventsResult(
        rows=[{"orderId": 5001, "toStatus": "CANCELLED", "customerLabel": "A3F29C"}], total=1
    )
    order_result = await _call_runtime_tool(
        get_order_events, {"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake
    )
    churn_result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake
    )

    assert _CUSTOMER_LABEL_NOTE in order_result
    assert _CUSTOMER_LABEL_NOTE in churn_result


async def test_churn_tool_reports_missing_rate_as_unreceived_not_zero() -> None:
    """[#197 PR 리뷰] churnRate 결측(None)은 "이탈률 0.0%"가 아니라 미수신으로
    명시 표기한다 — 워커가 "이탈 없음"으로 오판하지 않고 판정을 보류하게."""
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=None, cohort_size=5, pre_churn_signals=PreChurnSignals(), members=[]
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert "이탈률 미수신" in result
    assert "판정 보류" in result
    # 결측의 0% 위장 금지 — 단, [#290] 신호 정규화 비중("취소 0건·코호트 0.0%")은
    # 실측 0 이라 정당하다. 금지 대상은 이탈률 표기뿐이므로 단언을 그 구간으로 좁힌다.
    assert "이탈률 0.0%" not in result


async def test_churn_tool_distinguishes_empty_cohort_from_zero_churn() -> None:
    """[#197] 코호트 0명(기간 내 활동 회원 없음)은 "이탈률 0%"와 구분해 표기한다.

    BE 는 코호트 0명 시 cohortSize=0·churnRate=0.0 short-circuit — 기본 기간(최근
    7일) 질의에서 흔한 상태라, 워커가 "이탈 없음"으로 단정하지 않게 한다.
    """
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(churn_rate=0.0, cohort_size=0)

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-25", "to_date": "2026-07-31"}, fake
    )

    assert "코호트 0명" in result
    assert "이탈률" not in result


async def test_churn_tool_caps_member_lines_by_settings() -> None:
    """이탈 회원 나열은 I-16 전용 상한(seller_churn_member_max)으로 절단하고 잔여를
    고지한다 — I-14 kv 상한과 분리돼 서로의 조정에 영향받지 않는다(#197 리뷰)."""
    from app.core.config import get_settings

    cap = get_settings().seller_churn_member_max
    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=0.5,
        cohort_size=cap * 4,
        pre_churn_signals=PreChurnSignals(),
        members=[ChurnMember(customer_label=f"L{i:05d}") for i in range(cap + 3)],
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert f"이탈 회원 {cap + 3}명" in result
    assert "외 3명" in result


async def test_churn_tool_server_cap_note_follows_settings(monkeypatch) -> None:
    """[#495] "서버 상한 N" 고지는 하드코딩이 아니라 Settings 주입값을 따른다.

    N 은 I-16 명세에 없는 BE 구현 실측값(CHURN_LIST_CAP)이라, 문자열에 박아두면 BE 가
    값을 바꾼 순간 판매자에게 거짓 고지가 나간다. 기본값과 다른 값으로 바꿔 문구가
    실제로 따라오는지(=배선이 살아 있는지) 검증한다 — 기본값끼리 비교하면 하드코딩이
    남아 있어도 통과하므로 의미가 없다.
    """
    from app.agents.seller import tools as tools_module
    from app.core.config import get_settings

    overridden = get_settings().model_copy(update={"seller_churn_server_list_cap": 7})
    monkeypatch.setattr(tools_module, "get_settings", lambda: overridden)

    fake = FakeSpringClient()
    fake.churn_result = ChurnResult(
        churn_rate=0.5,
        cohort_size=20,
        pre_churn_signals=PreChurnSignals(),
        members=[ChurnMember(customer_label="A3F29C")],
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert "서버 상한 7 절단본일 수 있음" in result
    assert "서버 상한 50" not in result


async def test_churn_tool_degrades_on_spring_failure() -> None:
    """get_churn 실패 시 raise 없이 "Error:" 문자열로 degrade 한다(§3.4)."""
    fake = FakeSpringClient(fail={"get_churn"})

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake
    )

    assert result.startswith("Error: 이탈 코호트")


# ── I-8 계정/보안 이벤트 도구 (#197) ──────────────────────────────────────────


async def _call_account_events(args: dict, fake) -> str:
    """[#481] 브랜드 스코프 전환 — runtime(brand_id=42) 주입 호출 헬퍼."""
    return await _call_runtime_tool(get_account_events, args, fake)


def _disable_account_events(monkeypatch) -> None:
    """[#481] 운영 킬스위치 검증용 — 기본 활성 플래그를 테스트에서만 끈다."""
    from app.agents.seller import tools as tools_module
    from app.core.config import get_settings

    disabled = get_settings().model_copy(update={"seller_account_events_enabled": False})
    monkeypatch.setattr(tools_module, "get_settings", lambda: disabled)


async def test_account_events_tool_kill_switch_blocks_call(monkeypatch) -> None:
    """[#481] 플래그를 끄면 Spring 호출 없이 "Error:" 로 차단된다(운영 킬스위치).

    기본값은 활성(브랜드 스코프 전환으로 #197 보류 사유 해소)이지만, 되돌릴
    스위치는 유지한다 — 끈 상태의 차단 경로가 살아 있는지 검증한다.
    """
    _disable_account_events(monkeypatch)
    fake = FakeSpringClient()

    result = await _call_account_events({"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake)

    assert result.startswith("Error:")
    assert "비활성" in result
    assert fake.recorded_account_args is None  # Spring 호출 자체가 차단된다


async def test_account_events_tool_rejects_unknown_group_by_locally() -> None:
    """[#197 PR 리뷰 2] groupBy 화이트리스트(eventType|hour|ip) 밖 값은 Spring 왕복
    없이 즉시 "Error:" 로 거른다 — BE 400 INVALID_GROUP_BY 까지 가는 타임아웃 예산
    낭비 방지. 오류 문구에 유효값을 실어 LLM 재시도를 유도한다."""
    fake = FakeSpringClient()

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "date"}, fake
    )

    assert result.startswith("Error:")
    assert "eventType/hour/ip" in result
    assert fake.recorded_account_args is None  # 호출 전 차단 — 왕복 없음

    # 유효 3종은 그대로 통과한다(선검증이 과차단하지 않는다).
    for valid in ("eventType", "hour", "ip"):
        fake2 = FakeSpringClient()
        ok = await _call_account_events(
            {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": valid}, fake2
        )
        assert not ok.startswith("Error:"), valid
        assert fake2.recorded_account_args == (42, "2026-07-01", "2026-07-31", None, valid)


async def test_account_events_tool_passes_brand_period_and_summarizes_rows() -> None:
    """[#197 회귀 + #481] runtime 의 brand_id 와 from/to 가 전달되고, rows 내용이
    노출된다.

    구 스키마는 events 필드를 기대해 Spring rows 응답이 extra="allow" 로 조용히
    버려져 항상 "0건 집계됨"이었다(I-14/I-15 #194 와 동일 패턴). brand_id 는
    #481 브랜드 스코프 전환으로 필수가 됐다 — LLM 인자가 아니라 신원 컨텍스트에서
    온다(IDOR 방지).
    """
    fake = FakeSpringClient()
    fake.account_events_result = AccountEventsResult(
        group_by="eventType",
        rows=[{"key": "LOGIN_FAIL", "count": 7}, {"key": "LOGIN", "count": 40}],
    )

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "event_type": "LOGIN_FAIL"},
        fake,
    )

    assert fake.recorded_account_args == (42, "2026-07-01", "2026-07-31", "LOGIN_FAIL", None)
    assert "계정 이벤트 2건" in result
    assert "자사 코호트" in result
    assert "groupBy=eventType" in result
    assert "key=LOGIN_FAIL" in result and "count=7" in result
    assert "2026-07-01~2026-07-31" in result


async def test_account_events_tool_empty_rows_says_zero_with_group_by() -> None:
    """빈 rows 는 0건 + 적용 groupBy 를 함께 고지한다(정상 0건 표기 — 코호트 없음도
    정상 결과다, #481)."""
    fake = FakeSpringClient()

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "ip"}, fake
    )

    assert "계정 이벤트 0건" in result
    assert "자사 코호트" in result
    assert "groupBy=ip" in result


async def test_account_events_tool_degrades_on_spring_failure() -> None:
    """get_account_events 실패 시 "Error:" 문자열로 degrade 한다(보조 소스 규약 —
    #481 이후 BE 신경로 미배포 구간의 404 도 이 경로로 흡수된다)."""
    fake = FakeSpringClient(fail={"get_account_events"})

    result = await _call_account_events({"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake)

    assert result.startswith("Error: 계정 이벤트")


def test_worker_prompts_contain_log_interpretation_rules() -> None:
    """워커 프롬프트에 해석 규칙(완료만 기록·purchaseComplete 집계 단위)이 남아
    있다(회귀 방지)."""
    from app.agents.seller.prompts import (
        ABUSE_PROMPT,
        BEHAVIOR_PROMPT,
        CHURN_PROMPT,
        SALES_ANOMALY_PROMPT,
    )

    assert "해석 주의" in SALES_ANOMALY_PROMPT
    assert "완료" in CHURN_PROMPT and "교환" in CHURN_PROMPT
    assert "신청 미기록" in ABUSE_PROMPT
    assert "purchaseComplete" in BEHAVIOR_PROMPT
    # [#197 리뷰] 워커에 전달되는 리터럴의 번호 목록 구조 회귀 방지 — 연속 문장이
    # 3칸 들여쓰기를 잃으면 목록 밖 독립 문장처럼 보인다(충돌 해결 중 실제 발생).
    # [#215] purchaseComplete 금지 문구가 3단계로 재작성돼 리터럴을 새 문구로 갱신.
    # [#488] 그 문구가 '금지'에서 '신뢰해도 된다'로 뒤집혀 리터럴을 다시 갱신했다 —
    # 고정하는 것은 의미가 아니라 들여쓰기 구조다.
    assert "\n   purchaseComplete 는 주문 기준 집계라" in ABUSE_PROMPT
    assert "\npurchaseComplete 는 주문 기준 집계라" not in ABUSE_PROMPT
    assert "'구매 0'을 그대로 신뢰해도 된다" in ABUSE_PROMPT


# [#488] 2026-07-31 개정으로 폐기된 I-13 purchaseComplete 구 규정의 어휘. 이 문구가
# LLM 이 읽는 표면에 남으면 미반영이 아니라 **능동적 오정보**가 된다 — 워커가 실재하는
# 구매 데이터를 '신뢰 불가'로 취급하고 다른 도구로 우회한다(3개월 방치된 실제 결함).
_DEPRECATED_PURCHASE_WORDING = (
    "이벤트 기준",
    "미귀속",
    "구매 전무",
    "0 집계될 수 있다",
    "권위는 매출 조회",
    "권위는 I-6",
    "권위는 get_order_events",
)


async def test_behavior_surfaces_drop_deprecated_purchase_wording() -> None:
    """[#488] 역방향 회귀 — 폐기된 구 규정 어휘가 **LLM 주입 표면**(I-13 도구 출력
    3형 + behavior·abuse 워커 프롬프트)에 하나도 남아 있지 않다.

    문구 드리프트가 이번처럼 오래 방치되지 않게 '무엇이 있어야 하나'가 아니라
    '무엇이 없어야 하나'를 고정한다. 검사 대상은 파일이 아니라 실제로 LLM 에
    실리는 문자열 객체다 — 주석·개정 이력에 남긴 폐기 사실 기록까지 잡지 않도록.
    """
    from app.agents.seller.prompts import ABUSE_PROMPT, BEHAVIOR_PROMPT

    fake = FakeSpringClient()
    surfaces: dict[str, str] = {"BEHAVIOR_PROMPT": BEHAVIOR_PROMPT, "ABUSE_PROMPT": ABUSE_PROMPT}

    # groupBy 3형 전부 — 노트는 어느 형태로 조회해도 상시 부착된다.
    fake.behavior_result = BehaviorEventsResult(
        group_by="product",
        rows=[_behavior_row(101, 1820, 240, 96, 61)],
        total=1,
    )
    surfaces["tool:product"] = await _call_runtime_tool(
        get_behavior_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )
    fake.behavior_result = BehaviorEventsResult(
        group_by="eventType",
        counts={"productView": 1820, "addToCart": 240, "checkoutStart": 96, "purchaseComplete": 61},
    )
    surfaces["tool:eventType"] = await _call_runtime_tool(
        get_behavior_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-14", "group_by": "eventType"},
        fake,
    )
    fake.behavior_result = BehaviorEventsResult(
        group_by="date",
        series=[
            {"date": "2026-07-01", "productView": 900, "purchaseComplete": 30},
            {"date": "2026-07-02", "productView": 920, "purchaseComplete": 31},
        ],
    )
    surfaces["tool:date"] = await _call_runtime_tool(
        get_behavior_events,
        {"from_date": "2026-07-01", "to_date": "2026-07-02", "group_by": "date"},
        fake,
    )

    for name, text in surfaces.items():
        for phrase in _DEPRECATED_PURCHASE_WORDING:
            assert phrase not in text, f"{name} 에 폐기 문구 '{phrase}' 잔존"

    # 걷어낸 자리를 신규정이 실제로 채웠는지도 함께 고정(공백 회귀 방지).
    for name in ("tool:product", "tool:eventType", "tool:date"):
        assert "주문 기준 집계" in surfaces[name], f"{name} 에 집계 규칙 노트 미부착"
    assert "주문 기준 집계" in BEHAVIOR_PROMPT and "주문 기준 집계" in ABUSE_PROMPT
    # 스키마 docstring 은 LLM 표면이 아니라 개발자 문서라 위 부재 검사 대상이 아니다
    # (거기엔 "구 … 규정은 폐기" 기록을 의도적으로 남긴다) — 신규정 서술만 확인한다.
    assert "주문 기준" in (BehaviorEventsResult.__doc__ or "")


# ── [#297] get_orders (I-29 자사 주문 조회, §4.18) ────────────────────────────────


def _order_fixture() -> SellerOrderList:
    return SellerOrderList(
        tab_counts={"ALL": 2, "ORDERED": 1, "SHIPPING": 1, "DELIVERED": 0, "CLAIM": 0},
        rows=[
            SellerOrderRow(
                order_id=342,
                order_no="ORD-20260716-0342",
                ordered_at="2026-07-16T09:42:00+09:00",
                recipient_name="김서연",
                payment_method="MOCK_CARD",
                my_items_amount=89000,
                status="ORDERED",
                claim_status=None,
                items=[
                    SellerOrderItemRow(
                        order_item_id=5551,
                        product_id=1,
                        name="벨티드 린넨 원피스",
                        option_name="블루/M",
                        quantity=2,
                        price=44500,
                        status="ORDERED",
                    )
                ],
            )
        ],
        total=2,
    )


async def test_get_orders_injects_brand_and_passes_args() -> None:
    """brand_id 는 runtime.context 에서만 주입되고 조회 인자가 client 에 전달된다."""
    fake = FakeSpringClient()
    fake.orders_result = _order_fixture()

    await _call_runtime_tool(
        get_orders, {"status": "ORDERED", "order_id": 342, "limit": 5}, fake, brand_id=777
    )

    assert fake.recorded_brand_id == 777
    assert fake.recorded_orders_args == ("ORDERED", 342, None, None, 5, None)


async def test_get_orders_formats_items_with_order_item_id() -> None:
    """응답에 orderItemId·아이템 상태가 노출된다 — 발송 대상 해소(I-30 선행) 재료."""
    fake = FakeSpringClient()
    fake.orders_result = _order_fixture()

    result = await _call_runtime_tool(get_orders, {}, fake)

    assert "orderItemId=5551" in result
    assert "ORD-20260716-0342" in result
    assert "89,000원" in result
    assert "ORDERED 1" in result  # 탭별 건수


async def test_get_orders_empty_without_order_id() -> None:
    """빈 rows 는 정상 결과 — 주문 없음 안내(오류 아님)."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_orders, {}, fake)

    assert not result.startswith("Error:")
    assert "주문이 없습니다" in result


async def test_get_orders_hidden_existence_for_order_id() -> None:
    """orderId 직조회 빈 rows → '해당 주문이 없습니다'(존재 은닉, 확정 2026-08-04)."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_orders, {"order_id": 999}, fake)

    assert "해당 주문(orderId=999)이 없습니다" in result


async def test_get_orders_degrades_on_spring_failure() -> None:
    fake = FakeSpringClient(fail={"get_orders"})

    result = await _call_runtime_tool(get_orders, {}, fake)

    assert result.startswith("Error:")


async def test_get_orders_caps_rows_by_settings() -> None:
    """seller_summary_max_orders 상한 초과분은 '외 N건' 꼬리로 남는다(정보 소실 없음)."""
    from app.core.config import get_settings

    cap = get_settings().seller_summary_max_orders
    fixture = _order_fixture()
    row = fixture.rows[0]
    fixture.rows = [row.model_copy(update={"order_id": 1000 + i}) for i in range(cap + 3)]
    fixture.total = cap + 3
    fake = FakeSpringClient()
    fake.orders_result = fixture

    result = await _call_runtime_tool(get_orders, {}, fake)

    assert "외 3건" in result


# ── [#297] get_reviews (I-31 리뷰 조회, §4.20) ────────────────────────────────────


async def test_get_reviews_list_formats_rows() -> None:
    fake = FakeSpringClient()
    fake.reviews_result = SellerReviewList(
        rows=[
            SellerReviewRow(
                review_id=7,
                product_id=3,
                product_name="여행용 파우치",
                rating=2,
                content="지퍼가 일주일 만에 고장났어요",
                author_nickname="자비스",
                created_at="2026-07-21T12:00:00+09:00",
            )
        ],
        total=47,
    )

    result = await _call_runtime_tool(
        get_reviews, {"rating": "1,2", "sort": "ratingAsc"}, fake, brand_id=12
    )

    assert fake.recorded_brand_id == 12
    assert fake.recorded_reviews_args == (None, None, None, "1,2", "ratingAsc", None, None)
    assert "★2" in result and "여행용 파우치" in result
    assert "지퍼가 일주일 만에 고장났어요" in result
    assert "리뷰 47건" in result
    assert "최근 7일 기본 적용" in result  # 기간 생략 시 기본 고지


async def test_get_reviews_rejects_retired_sort_vocabulary() -> None:
    """[#496] 폐기된 구 어휘 sort="rating" 은 Spring 왕복 없이 로컬에서 거른다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_reviews, {"sort": "rating"}, fake, brand_id=12)

    assert result.startswith("Error:")
    assert "ratingAsc" in result  # 유효 어휘를 실어 재시도를 유도한다
    assert not hasattr(fake, "recorded_reviews_args")  # Spring 호출 자체가 없다


async def test_get_reviews_stats_mode_ignores_sort() -> None:
    """stats 모드는 sort 를 서버에 싣지 않으므로 화이트리스트 검증 대상이 아니다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_reviews, {"stats": True, "sort": "rating"}, fake)

    assert not result.startswith("Error:")
    assert fake.recorded_review_stats_args == (None, None, None, None)


async def test_get_reviews_stats_mode_null_average() -> None:
    """stats=True 는 집계 경로 — 0건이면 '평점 0점'이 아니라 리뷰 없음으로 안내."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_reviews, {"stats": True}, fake)

    assert fake.recorded_review_stats_args == (None, None, None, None)
    assert "리뷰가 없습니다" in result
    assert "0점" not in result
    # [#494 회귀] rating 미지정 출력은 종전과 바이트 동일 — 스코프 문구가 끼지 않는다.
    assert result == "조회 기간에 리뷰가 없습니다. (기준: 최근 7일 기본 적용)"


async def test_get_reviews_stats_mode_formats_distribution() -> None:
    fake = FakeSpringClient()
    fake.review_stats_result = SellerReviewStats(
        total_count=47,
        average_rating=3.8,
        distribution={"5": 12, "4": 15, "3": 8, "2": 7, "1": 5},
        by_product=[
            SellerReviewProductStat(
                product_id=3, product_name="여행용 파우치", count=21, average_rating=3.1
            )
        ],
    )

    result = await _call_runtime_tool(
        get_reviews, {"stats": True, "from_date": "2026-07-01", "to_date": "2026-07-31"}, fake
    )

    assert "총 47건" in result and "평균 3.8점" in result
    assert "1점 5건" in result
    assert "여행용 파우치" in result and "평균 3.1점" in result
    assert "2026-07-01~2026-07-31" in result
    # [#494 회귀] rating 미지정이면 집계 헤더가 종전 그대로다(스코프 표기 없음).
    assert result.startswith("리뷰 집계: 총 47건")
    assert "한정" not in result


async def test_get_reviews_stats_mode_forwards_rating_filter() -> None:
    """[#494] stats=True + rating 이면 별점 필터가 집계 호출에 전달되고 스코프가 명시된다.

    전달이 빠지면 전 별점 합산 byProduct 가 돌아오는데 에러가 없어, 워커가 그것을
    '1–2점이 몰린 상품'으로 서술한다 — 명세(I-31)의 대표 사용례가 조용히 틀린다.
    """
    fake = FakeSpringClient()
    fake.review_stats_result = SellerReviewStats(
        total_count=18,
        average_rating=1.4,
        distribution={"5": 0, "4": 0, "3": 0, "2": 11, "1": 7},
        by_product=[
            SellerReviewProductStat(
                product_id=3, product_name="여행용 파우치", count=12, average_rating=1.3
            )
        ],
    )

    result = await _call_runtime_tool(
        get_reviews, {"stats": True, "rating": "1,2"}, fake, brand_id=12
    )

    assert fake.recorded_brand_id == 12
    assert fake.recorded_review_stats_args == (None, None, None, "1,2")
    assert "리뷰 집계(별점 1,2 한정)" in result
    assert "총 18건" in result and "평균 1.4점" in result
    assert "여행용 파우치" in result


async def test_get_reviews_stats_mode_forwards_product_and_rating() -> None:
    """[#494] product_id 와 rating 을 함께 주면 둘 다 집계 호출에 실린다."""
    fake = FakeSpringClient()
    fake.review_stats_result = SellerReviewStats(
        total_count=5,
        average_rating=1.2,
        distribution={"5": 0, "4": 0, "3": 0, "2": 1, "1": 4},
        by_product=[],
    )

    result = await _call_runtime_tool(
        get_reviews,
        {
            "stats": True,
            "rating": "1,2",
            "product_id": 3,
            "from_date": "2026-07-01",
            "to_date": "2026-07-31",
        },
        fake,
    )

    assert fake.recorded_review_stats_args == ("2026-07-01", "2026-07-31", 3, "1,2")
    assert "리뷰 집계(별점 1,2 한정)" in result


async def test_get_reviews_stats_mode_empty_with_rating_states_scope() -> None:
    """[#494] 별점을 걸고 0건인 것은 '리뷰가 없다'와 다르다 — 스코프를 밝힌다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_reviews, {"stats": True, "rating": "1,2"}, fake)

    assert "별점 1,2 리뷰가 없습니다" in result


async def test_get_reviews_degrades_on_spring_failure() -> None:
    fake = FakeSpringClient(fail={"get_reviews"})

    result = await _call_runtime_tool(get_reviews, {}, fake)

    assert result.startswith("Error:")


# ── [#518] null content 수신 · 감성 비율 · bucket 추이 ────────────────────────


def _review_stats(total: int, average: float | None, dist: dict[str, int]) -> SellerReviewStats:
    return SellerReviewStats(
        total_count=total, average_rating=average, distribution=dist, by_product=[]
    )


async def test_get_reviews_renders_null_content_and_nickname() -> None:
    """[#518] content·authorNickname 이 null 인 행도 목록에 나오고 'None' 을 찍지 않는다.

    별점만 남기는 리뷰가 실재한다(DDL `content TEXT NULL`). 구 스키마는 이 행 하나로
    조회 전체를 ValidationError → degrade 시켰고, 그걸 고친 뒤에도 폴백이 없으면
    판매자 화면에 "None" 이 노출되고 워커가 그 행을 불만 유형으로 분류한다.
    """
    fake = FakeSpringClient()
    fake.reviews_result = SellerReviewList(
        rows=[
            SellerReviewRow(
                review_id=9,
                product_id=3,
                product_name="여행용 파우치",
                rating=5,
                content=None,
                author_nickname=None,
                created_at="2026-07-21T12:00:00+09:00",
            )
        ],
        total=1,
    )

    result = await _call_runtime_tool(get_reviews, {}, fake)

    assert "None" not in result
    assert "(내용 없음)" in result and "익명" in result
    assert "★5" in result and "여행용 파우치" in result


async def test_get_reviews_stats_mode_reports_sentiment_ratio() -> None:
    """[#518] 감성 비율은 도구가 계산해 출력한다 — 워커가 암산하면 F2 가 잡는다.

    verifier.check_evidence_grounded 는 finding 의 유의 수치를 도구 출력과 대조하므로,
    비율이 출력에 없으면 "긍정 62.5%" 서술이 근거 없는 수치로 강등된다.
    """
    fake = FakeSpringClient()
    fake.review_stats_result = _review_stats(48, 3.9, {"5": 20, "4": 10, "3": 6, "2": 8, "1": 4})

    result = await _call_runtime_tool(get_reviews, {"stats": True}, fake)

    assert "긍정(4-5점) 30건 62.5%" in result
    assert "중립(3점) 6건 12.5%" in result
    assert "부정(1-2점) 12건 25.0%" in result


async def test_get_reviews_stats_mode_omits_sentiment_when_rating_filtered() -> None:
    """[#518 회귀] rating 을 걸고 온 집계에는 감성 비율을 붙이지 않는다.

    분모가 그 별점 범위라 "부정 100%" 같은 자명한 수가 나오고, #494 가 세운 rating
    지정 경로의 출력이 회귀한다.
    """
    fake = FakeSpringClient()
    fake.review_stats_result = _review_stats(12, 1.4, {"2": 8, "1": 4})

    result = await _call_runtime_tool(get_reviews, {"stats": True, "rating": "1,2"}, fake)

    assert "감성:" not in result
    assert "리뷰 집계(별점 1,2 한정)" in result


async def test_get_reviews_bucket_splits_period_and_fans_out() -> None:
    """[#518] bucket 은 도구 호출 1회로 구간별 I-31 집계를 모아 온다."""
    fake = FakeSpringClient()
    fake.review_stats_by_range = {
        ("2026-07-01", "2026-07-07"): _review_stats(10, 4.5, {"5": 7, "4": 2, "1": 1}),
        ("2026-07-08", "2026-07-14"): _review_stats(6, 2.0, {"2": 4, "1": 2}),
        ("2026-07-15", "2026-07-15"): _review_stats(0, None, {}),
    }

    result = await _call_runtime_tool(
        get_reviews,
        {
            "stats": True,
            "bucket": "weekly",
            "from_date": "2026-07-01",
            "to_date": "2026-07-15",
        },
        fake,
        brand_id=12,
    )

    assert fake.recorded_brand_id == 12
    assert [(call[0], call[1]) for call in fake.review_stats_calls] == [
        ("2026-07-01", "2026-07-07"),
        ("2026-07-08", "2026-07-14"),
        ("2026-07-15", "2026-07-15"),
    ]
    assert "리뷰 추이(주별, 3구간)" in result
    assert "2026-07-01~2026-07-07 10건 평균 4.5점(부정 1건·긍정 9건)" in result
    assert "2026-07-15~2026-07-15 0건" in result
    assert "조회 실패" not in result


async def test_get_reviews_bucket_marks_failed_span_without_calling_it_zero() -> None:
    """[#518] 실패한 구간은 '조회 실패' 다 — '0건' 으로 뭉개면 없는 급락이 서술된다."""
    fake = FakeSpringClient()
    fake.review_stats_by_range = {
        ("2026-07-01", "2026-07-01"): _review_stats(4, 4.0, {"4": 4}),
        ("2026-07-02", "2026-07-02"): SpringUnavailableError("Spring 콜백 타임아웃(3.0s)"),
        ("2026-07-03", "2026-07-03"): _review_stats(3, 2.0, {"2": 3}),
    }

    result = await _call_runtime_tool(
        get_reviews,
        {
            "stats": True,
            "bucket": "daily",
            "from_date": "2026-07-01",
            "to_date": "2026-07-03",
        },
        fake,
    )

    assert not result.startswith("Error:")
    assert "2026-07-02~2026-07-02 조회 실패" in result
    assert "2026-07-01~2026-07-01 4건" in result and "2026-07-03~2026-07-03 3건" in result
    assert "0건과 다릅니다" in result


async def test_get_reviews_bucket_all_spans_failed_degrades() -> None:
    """[#518] 전 구간이 실패하면 부분 성공이 아니라 degrade 다."""
    fake = FakeSpringClient(fail={"get_review_stats"})

    result = await _call_runtime_tool(
        get_reviews,
        {
            "stats": True,
            "bucket": "daily",
            "from_date": "2026-07-01",
            "to_date": "2026-07-02",
        },
        fake,
    )

    assert result.startswith("Error:")


async def test_get_reviews_bucket_requires_stats_mode() -> None:
    """[#518] 목록 모드로 열면 구간 수 × limit 만큼 원문이 쏟아진다 — 막는다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_reviews,
        {"bucket": "weekly", "from_date": "2026-07-01", "to_date": "2026-07-15"},
        fake,
    )

    assert result.startswith("Error:") and "stats=True" in result
    assert fake.review_stats_calls == []


async def test_get_reviews_bucket_requires_explicit_period() -> None:
    """[#518] 서버 기본 7일은 요청마다 오늘이 달라 버킷 경계를 고정할 수 없다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_reviews, {"stats": True, "bucket": "weekly"}, fake)

    assert result.startswith("Error:") and "from_date" in result
    assert fake.review_stats_calls == []


async def test_get_reviews_bucket_rejects_over_limit_before_calling_spring() -> None:
    """[#518] 상한 초과는 조회 **전에** 거절한다 — 한 번도 왕복하지 않는다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_reviews,
        {
            "stats": True,
            "bucket": "daily",
            "from_date": "2026-07-01",
            "to_date": "2026-08-31",
        },
        fake,
    )

    assert result.startswith("Error:")
    assert fake.review_stats_calls == []


async def test_get_reviews_bucket_rejects_unknown_unit() -> None:
    """[#518] 어휘 밖 bucket 은 화이트리스트에서 걸린다(_REVIEW_SORT 와 같은 패턴)."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_reviews,
        {
            "stats": True,
            "bucket": "yearly",
            "from_date": "2026-07-01",
            "to_date": "2026-07-15",
        },
        fake,
    )

    assert result.startswith("Error:")
    assert fake.review_stats_calls == []


async def test_get_reviews_without_bucket_keeps_legacy_output() -> None:
    """[#518 회귀] bucket 미지정 경로는 팬아웃을 타지 않고 종전 출력 그대로다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_reviews, {"stats": True}, fake)

    assert result == "조회 기간에 리뷰가 없습니다. (기준: 최근 7일 기본 적용)"
    assert fake.review_stats_calls == [(None, None, None, None)]


# [#620] update_order_status 도구(ORDER_WRITE_TOOLS 전용, #297) 테스트는 제거됐다 —
# 도구 자체가 어느 에이전트에도 바인딩되지 않는 죽은 코드로 확인돼 삭제됐다. I-30
# 발송 실행 경로(성공/이미 발송/장애)는 app/agents/seller/hitl.py 의 _execute_draft
# 가 담당하며 test_seller_hitl.py 에서 검증한다.

# ─────────── 기간 인자 백스톱 가드 (이슈 #346) ───────────


async def test_period_arg_guard_rejects_range_over_upper_limit() -> None:
    """[#346] 상한 밖 기간은 Spring 을 부르기 전에 "Error:" 로 끊는다.

    두 레인 모두 기간을 코드가 환산해 입력 메시지로 주지만, 그 값을 도구 인자로 옮기는
    것은 LLM 이다 — 무시하고 제 손으로 날짜를 지어내면 period.py 의 상한(R4)이 통째로
    비켜간다. 프롬프트 한 줄에 기대는 대신 호출 경계에서 한 번 더 막는다
    (판정과 집행을 분리한다 — docs/lessons.md 2026-08-07).
    """
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_sales_timeseries, {"from_date": "2000-01-01", "to_date": "2026-01-01"}, fake
    )

    assert result.startswith("Error:")
    assert "상한" in result
    assert fake.recorded_brand_id is None, "가드가 걸렸는데 Spring 을 불렀다"


async def test_period_arg_guard_rejects_reversed_range() -> None:
    """역전 범위(from > to)도 호출 전에 끊는다 — 빈 결과를 정상 답으로 읽지 않게."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_funnel, {"from_date": "2026-07-14", "to_date": "2026-07-01"}, fake
    )

    assert result.startswith("Error:")
    assert fake.recorded_brand_id is None


async def test_period_arg_guard_rejects_malformed_dates() -> None:
    """YYYY-MM-DD 가 아닌 값은 Spring 400 을 기다리지 않고 즉시 되돌린다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(
        get_sales_timeseries, {"from_date": "지난달", "to_date": "2026-07-14"}, fake
    )

    assert result.startswith("Error:")
    assert "형식" in result


async def test_period_arg_guard_allows_valid_range() -> None:
    """정상 범위는 그대로 통과한다 — 가드가 본래 경로를 막지 않는다(회귀 방지)."""
    fake = FakeSpringClient()

    await _call_runtime_tool(
        get_sales_timeseries, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake, brand_id=9
    )

    assert fake.recorded_brand_id == 9


async def test_period_arg_guard_skips_optional_unset_period() -> None:
    """기간이 선택 인자인 도구(I-29 주문 조회)는 미지정을 통과시킨다."""
    fake = FakeSpringClient()

    result = await _call_runtime_tool(get_orders, {}, fake)

    assert not result.startswith("Error:")


# ── [#591] get_latest_report — 보고서 조회 도구는 이것 하나뿐(결정 10) ──────────


def _report(brand_id: int = 42) -> ReportRecord:
    return ReportRecord(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        brand_id=brand_id,
        trigger_type="scheduled_daily",
        period_from=date(2026, 8, 3),
        period_to=date(2026, 8, 9),
        title="주간 매출 진단",
        summary="전환율이 결제 단계에서 유의하게 떨어졌습니다.",
        report_md="# 본문",
        verified=True,
        attempts=1,
        created_at=datetime(2026, 8, 10, 5, 0, tzinfo=UTC),
    )


def _recommendation(rank: int, title: str) -> RecommendationRecord:
    return RecommendationRecord(
        id=UUID(f"2222222{rank}-2222-4222-8222-222222222222"),
        report_id=UUID("11111111-1111-4111-8111-111111111111"),
        brand_id=42,
        rank=rank,
        action_type="price_adjust",
        title=title,
        rationale="근거",
    )


async def test_get_latest_report_returns_summary_and_ranked_recommendations(
    monkeypatch,
) -> None:
    """요약 + rank 번호가 함께 나간다 — 채팅에서 바로 "N번 적용해줘"로 이어지는 근거다."""

    async def _reports(brand_id: int, *, limit: int):
        assert (brand_id, limit) == (42, 1)
        return [_report()]

    async def _recs(report_id, *, brand_id: int):
        assert brand_id == 42
        return [_recommendation(1, "가격 인하"), _recommendation(2, "재고 보충")]

    monkeypatch.setattr(seller_tools.analysis_store, "list_reports", _reports)
    monkeypatch.setattr(seller_tools.analysis_store, "list_recommendations_by_report", _recs)

    result = await get_latest_report.coroutine(runtime=FakeRuntime())

    assert not result.startswith("Error:")
    assert "주간 매출 진단" in result
    assert "2026-08-03~2026-08-09" in result  # 보고서 기간을 그대로 인용한다
    assert "1. 가격 인하" in result and "2. 재고 보충" in result


async def test_get_latest_report_empty_is_not_an_error(monkeypatch) -> None:
    """보고서 0건은 장애가 아니다 — "Error:" 로 나가면 한 번도 분석된 적 없음이 고장으로 안내된다."""

    async def _reports(brand_id: int, *, limit: int):
        return []

    monkeypatch.setattr(seller_tools.analysis_store, "list_reports", _reports)

    result = await get_latest_report.coroutine(runtime=FakeRuntime())

    assert not result.startswith("Error:")
    assert "아직" in result


async def test_get_latest_report_degrades_on_store_failure(monkeypatch) -> None:
    """조회 장애는 degrade 문자열(§3.4) — raise 하면 스트림이 통째로 죽는다."""

    async def _boom(brand_id: int, *, limit: int):
        raise RuntimeError("pool down")

    monkeypatch.setattr(seller_tools.analysis_store, "list_reports", _boom)

    result = await get_latest_report.coroutine(runtime=FakeRuntime())

    assert result.startswith("Error:")


async def test_get_latest_report_never_takes_brand_id_arg() -> None:
    """신원은 runtime.context 에서만 온다 — 인자로 열면 남의 보고서를 읽는다(IDOR)."""
    tool = next(t for t in READ_TOOLS if t.name == "get_latest_report")

    assert set(tool.args) == set()
