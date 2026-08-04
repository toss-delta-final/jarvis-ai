"""app/agents/seller/tools.py(ToolRuntime 도구 + READ_TOOLS/PRODUCT_TOOLS) 단위 테스트.

DESIGN-SELLER-TOOLS-STAGE1 §6 테스트 목록. 실 Spring 호출 없이 FakeSpringClient 로
브랜드 스코프 주입(IDOR 방지)·degrade 문자열 반환·쓰기/조회 분리를 검증한다.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.agents.seller.context import SellerContext
from app.agents.seller.tools import (
    PRODUCT_TOOLS,
    READ_TOOLS,
    get_account_events,
    get_behavior_events,
    get_churn_cohort,
    get_funnel,
    get_order_events,
    get_sales_timeseries,
    list_my_products,
    update_product,
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
    ProductCreateResult,
    ProductDeleteResult,
    ProductUpdateResult,
    SalesResult,
    SalesSeriesPoint,
    SellerProductList,
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

    async def get_account_events(self, from_, to, event_type=None, group_by=None):
        self.recorded_account_args = (from_, to, event_type, group_by)
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
        return ProductDeleteResult(product_id=product_id, status="HIDDEN")


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


def test_write_tools_isolated_from_read() -> None:
    """read_tools 에는 create/update/delete 가 없고 product_tools 에만 존재한다."""
    read_names = {t.name for t in READ_TOOLS}
    product_names = {t.name for t in PRODUCT_TOOLS}

    for write_name in ("create_product", "update_product", "delete_product"):
        assert write_name not in read_names
        assert write_name in product_names


def test_no_identity_params_in_any_tool() -> None:
    """모든 도구의 args_schema 에 sellerId/brandId 류 키가 없다(IDOR — 신원 미노출)."""
    all_tools = {t.name: t for t in (*READ_TOOLS, *PRODUCT_TOOLS)}.values()
    for t in all_tools:
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


def test_list_my_products_in_both_lists() -> None:
    """list_my_products 는 read_tools·product_tools 양쪽에 모두 존재한다."""
    assert "list_my_products" in {t.name for t in READ_TOOLS}
    assert "list_my_products" in {t.name for t in PRODUCT_TOOLS}


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


async def test_update_product_exposes_all_schema_fields() -> None:
    """update_product 는 ProductUpdate 전 필드를 인자로 노출한다(2026-07-17 사용자 확정)."""
    fake = FakeSpringClient()
    result = await _call_runtime_tool(
        update_product, {"product_id": 9, "name": "새 이름", "category": "패션"}, fake
    )

    assert "9" in result
    assert fake.recorded_patch.name == "새 이름"  # name 이 스키마까지 전달된다
    assert fake.recorded_patch.category == "패션"
    assert fake.recorded_patch.price is None  # 미지정 필드는 None(부분 수정)


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


async def test_behavior_tool_summarizes_product_rows_with_authority_note() -> None:
    """groupBy=product — 상품별 카운트 요약 + purchaseComplete 권위 주의 문구."""
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
    assert "권위는 매출 조회(I-6)" in result  # 이벤트≠주문 권위(명세 집계 규칙)
    # [#196] purchaseComplete 미귀속 경고 — 0 을 '구매 전무'로 오해석 금지 문구.
    assert "구매 전무" in result and "0 집계될 수 있다" in result


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
    assert "권위는 매출 조회(I-6)" in result  # 기존 노트 유지


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


async def test_account_events_hour_group_reports_night_share(monkeypatch) -> None:
    """[#290 abuse Collective 트랙] hour-groupBy 는 심야(0~6시) 활동 비중을 계산해
    붙인다 — 심야 편중은 봇 신호(Tan & Kumar)다."""
    _enable_account_events(monkeypatch)
    fake = FakeSpringClient()
    fake.account_events_result = AccountEventsResult(
        group_by="hour",
        rows=[{"key": 2, "count": 300}, {"key": 4, "count": 200}, {"key": 15, "count": 500}],
    )

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "hour"}, fake
    )

    assert "심야(0~6시) 활동 비중 50.0%(500/1000건)" in result


async def test_account_events_ip_group_sorts_by_fail_count(monkeypatch) -> None:
    """[#290 abuse Collective 트랙] ip-groupBy 는 failCount 내림차순으로 정렬해 무차별
    대입 신호를 상단에 노출하고 isSuspicious 건수를 요약한다."""
    _enable_account_events(monkeypatch)
    fake = FakeSpringClient()
    fake.account_events_result = AccountEventsResult(
        group_by="ip",
        rows=[
            {"ipMasked": "1.2.3.*", "failCount": 2, "isSuspicious": False},
            {"ipMasked": "9.9.9.*", "failCount": 42, "isSuspicious": True},
        ],
    )

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "ip"}, fake
    )

    assert result.index("9.9.9.*") < result.index("1.2.3.*")  # failCount 상위 우선
    assert "isSuspicious(코드 판정) 1건" in result


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
    # 꼬리 = 11·12번째 행 합계: 조회 20+10, 담기 2+1, 결제시작 2+2, 구매 0.
    assert "외 2건(저활동) 합계: 조회 30 담기 3 결제시작 4 구매 0" in result


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
    """전이가 있으면 기록 규칙 주의(완료만 기록·주문 단위 1행)가 함께 나간다."""
    fake = FakeSpringClient()
    fake.order_events_result = OrderEventsResult(
        rows=[{"orderId": 5001, "toStatus": "CANCELLED", "actorType": "USER"}], total=1
    )

    result = await _call_runtime_tool(
        get_order_events, {"from_date": "2026-07-01", "to_date": "2026-07-14"}, fake
    )

    assert "구매확정·클레임 신청은 로그에 없음" in result
    assert "아이템 수로 해석 금지" in result


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
                member_id=103,
                last_activity_at="2026-06-15T10:00:00+09:00",
                sessions_30d=0,
                pre_churn_event="RETURNED(상품불량)",
            ),
            ChurnMember(member_id=104, last_activity_at="2026-06-01T09:00:00+09:00"),
        ],
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert "취소 3건" in result
    assert "사이즈 불만(2건)" in result
    assert "가격인상 노출 2명" in result
    assert "이탈 회원 2명" in result
    assert "[103]" in result and "RETURNED(상품불량)" in result
    assert "쓰지 말 것" in result  # _CHURN_SIGNAL_RULES_NOTE 상시 부착


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
        members=[ChurnMember(member_id=i) for i in range(cap + 3)],
    )

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-06-01", "to_date": "2026-07-31"}, fake
    )

    assert f"이탈 회원 {cap + 3}명" in result
    assert "외 3명" in result


async def test_churn_tool_degrades_on_spring_failure() -> None:
    """get_churn 실패 시 raise 없이 "Error:" 문자열로 degrade 한다(§3.4)."""
    fake = FakeSpringClient(fail={"get_churn"})

    result = await _call_runtime_tool(
        get_churn_cohort, {"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake
    )

    assert result.startswith("Error: 이탈 코호트")


# ── I-8 계정/보안 이벤트 도구 (#197) ──────────────────────────────────────────


async def _call_account_events(args: dict, fake) -> str:
    """runtime 없는 전역 도구 호출 헬퍼 — 싱글턴 교체 후 반드시 원복."""
    spring_client_module.set_spring_client(fake)
    try:
        return await get_account_events.coroutine(**args)
    finally:
        spring_client_module.set_spring_client(None)


def _enable_account_events(monkeypatch) -> None:
    """[#197 PR 리뷰] I-8 노출 보류 플래그를 테스트에서만 켠다.

    admin 소유 협의 미완(🔴, api-spec §4.4 v0.19.1)으로 기본 false — 활성 상태의
    요약/전달 로직은 협의 종결 후에도 그대로 쓰이므로 플래그만 켜서 검증한다.
    """
    from app.agents.seller import tools as tools_module
    from app.core.config import get_settings

    enabled = get_settings().model_copy(update={"seller_account_events_enabled": True})
    monkeypatch.setattr(tools_module, "get_settings", lambda: enabled)


async def test_account_events_tool_disabled_by_default() -> None:
    """[#197 PR 리뷰] I-8 은 admin 소유 협의(🔴) 전까지 기본 비활성 — 도구가
    Spring 호출 없이 "Error:" 문자열을 반환한다(전역 데이터 노출 보류).

    구 코드에선 쿼리 400·스키마 미스매치가 사실상 차단막이었는데 #197 정합이
    그 차단막을 제거했으므로, 의도된 보류를 플래그로 명시해 회귀를 방지한다.
    """
    fake = FakeSpringClient()

    result = await _call_account_events({"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake)

    assert result.startswith("Error:")
    assert "비활성" in result
    assert fake.recorded_account_args is None  # Spring 호출 자체가 차단된다


async def test_account_events_tool_rejects_unknown_group_by_locally(monkeypatch) -> None:
    """[#197 PR 리뷰 2] groupBy 화이트리스트(eventType|hour|ip) 밖 값은 Spring 왕복
    없이 즉시 "Error:" 로 거른다 — BE 400 INVALID_GROUP_BY 까지 가는 타임아웃 예산
    낭비 방지. 오류 문구에 유효값을 실어 LLM 재시도를 유도한다."""
    _enable_account_events(monkeypatch)
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
        assert fake2.recorded_account_args == ("2026-07-01", "2026-07-31", None, valid)


async def test_account_events_tool_passes_period_and_summarizes_rows(monkeypatch) -> None:
    """[#197 회귀] from/to 가 필수 전달되고, rows(구 events 아님) 내용이 노출된다.

    구 스키마는 events 필드를 기대해 Spring rows 응답이 extra="allow" 로 조용히
    버려져 항상 "0건 집계됨"이었다(I-14/I-15 #194 와 동일 패턴).
    """
    _enable_account_events(monkeypatch)
    fake = FakeSpringClient()
    fake.account_events_result = AccountEventsResult(
        group_by="eventType",
        rows=[{"key": "LOGIN_FAIL", "count": 7}, {"key": "LOGIN", "count": 40}],
    )

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "event_type": "LOGIN_FAIL"},
        fake,
    )

    assert fake.recorded_account_args == ("2026-07-01", "2026-07-31", "LOGIN_FAIL", None)
    assert "계정/보안 이벤트 2건" in result
    assert "groupBy=eventType" in result
    assert "key=LOGIN_FAIL" in result and "count=7" in result
    assert "2026-07-01~2026-07-31" in result


async def test_account_events_tool_empty_rows_says_zero_with_group_by(monkeypatch) -> None:
    """빈 rows 는 0건 + 적용 groupBy 를 함께 고지한다(정상 0건 표기)."""
    _enable_account_events(monkeypatch)
    fake = FakeSpringClient()

    result = await _call_account_events(
        {"from_date": "2026-07-01", "to_date": "2026-07-31", "group_by": "ip"}, fake
    )

    assert "계정/보안 이벤트 0건" in result
    assert "groupBy=ip" in result


async def test_account_events_tool_degrades_on_spring_failure(monkeypatch) -> None:
    """get_account_events 실패 시 "Error:" 문자열로 degrade 한다(보조 소스 규약)."""
    _enable_account_events(monkeypatch)
    fake = FakeSpringClient(fail={"get_account_events"})

    result = await _call_account_events({"from_date": "2026-07-01", "to_date": "2026-07-31"}, fake)

    assert result.startswith("Error: 계정 이벤트")


def test_worker_prompts_contain_log_interpretation_rules() -> None:
    """워커 프롬프트에 해석 규칙(완료만 기록·이벤트≠주문 권위)이 남아 있다(회귀 방지)."""
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
    assert "\n   구매·주문 수치의 권위는 get_order_events" in ABUSE_PROMPT
    assert "\n구매·주문 수치의 권위는" not in ABUSE_PROMPT
    assert "'구매 0'" in ABUSE_PROMPT  # 금지 문구 자체의 존치도 함께 고정
