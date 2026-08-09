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

import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from functools import wraps
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from app.agents.seller import calc
from app.agents.seller.analysis import outliers, proportions, segmentation, timeseries
from app.agents.seller.context import SellerContext
from app.core.config import get_settings
from app.core.tracing import trace_span
from app.schemas.spring import (
    BehaviorEventsResult,
    OrderItemStatusUpdate,
    ProductCreate,
    ProductUpdate,
)
from app.services.spring_client import (
    OrderAlreadyShipped,
    OrderInvalidTransition,
    OrderItemNotFound,
    ProductAlreadyDeleted,
    ProductDeletedNotEditable,
    SpringUnavailableError,
    get_spring_client,
)

_log = logging.getLogger(__name__)


def _traced_tool(
    name: str,
) -> Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]]:
    """Add a payload-free bounded tool span without changing LangChain's tool schema."""

    def decorate(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @wraps(func)
        async def wrapped(*args: Any, **kwargs: Any) -> str:
            with trace_span(name, "tool") as span:
                result = await func(*args, **kwargs)
                if span is not None and result.startswith("Error:"):
                    span.error_type = "TOOL_ERROR"
                return result

        return wrapped

    return decorate


def _period_arg_error(from_date: str | None, to_date: str | None) -> str | None:
    """도구 인자로 들어온 기간의 형식·역전·상한을 재검증한다 — 위반이면 "Error:" 문구.

    [#346] 두 레인 모두 기간은 코드가 환산해 입력 메시지로 **주어지지만**, 그 값을 실제
    도구 인자로 옮기는 것은 LLM 이다. 주어진 from/to 를 무시하고 제 손으로 날짜를 지어내면
    period.py 의 상한(R4)·역전 가드가 통째로 비켜간다 — 프롬프트 한 줄에 기대는 대신
    호출 경계에서 한 번 더 막는다("판정과 집행을 분리한다", docs/lessons.md 2026-08-07).

    되묻기가 아니라 "Error:" 인 이유: 이 시점은 이미 스트림 안이라 되묻기로 되돌아갈 수
    없고, 도구 실패 문자열은 프롬프트의 degrade 규약(§3.4)이 이미 다루는 어휘다.
    None(기간 미지정)은 통과 — get_orders·get_reviews 는 기간이 선택 인자다.
    """
    if from_date is None or to_date is None:
        return None
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except (TypeError, ValueError):
        return (
            f"Error: 기간 형식이 올바르지 않습니다(from_date={from_date!r}, "
            f"to_date={to_date!r}). 입력에 주어진 기간을 YYYY-MM-DD 그대로 쓰세요."
        )
    if end < start:
        return (
            f"Error: 시작일이 종료일보다 뒤입니다({from_date}~{to_date}). "
            "입력에 주어진 기간을 그대로 쓰세요."
        )
    limit = get_settings().seller_period_max_days
    if (end - start).days + 1 > limit:
        return (
            f"Error: 조회 기간이 상한({limit}일)을 넘습니다({from_date}~{to_date}). "
            "입력에 주어진 기간을 그대로 쓰세요."
        )
    return None


def _guard_period_args(
    func: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    """from_date/to_date 인자에 기간 규칙을 재적용하는 백스톱 (#346).

    `functools.wraps` 가 `__wrapped__` 를 남기므로 `inspect.signature` 는 원 함수의
    시그니처를 따라간다 — LangChain 이 만드는 도구 스키마는 바뀌지 않는다(_traced_tool
    과 같은 규약).
    """
    signature = inspect.signature(func)

    @wraps(func)
    async def wrapped(*args: Any, **kwargs: Any) -> str:
        bound = signature.bind_partial(*args, **kwargs)
        error = _period_arg_error(bound.arguments.get("from_date"), bound.arguments.get("to_date"))
        if error is not None:
            _log.warning("도구 기간 인자 위반 — %s", error)
            return error
        return await func(*args, **kwargs)

    return wrapped


def _reference_note(from_date: str, to_date: str) -> str:
    """모든 조회 도구 응답에 기준 시점을 고지한다(답변 신뢰성)."""
    return f"(기준: {from_date}~{to_date} 집계값)"


def _previous_period(from_date: str, to_date: str) -> tuple[str, str] | None:
    """직전 인접 동일 길이 기간 (#290 — funnel·churn 기간 비교 공용).

    날짜 형식 오류·역전 범위면 None — 비교만 생략하고 본 조회는 계속한다
    (형식 검증은 Spring 오류 경로 소관, 여기서 raise 하지 않는다).
    """
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError:
        return None
    if end < start:
        return None
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=(end - start).days)
    return prev_start.isoformat(), prev_end.isoformat()


# RateComparison.verdict → 판매자 표면 어휘 (proportions 모듈과 짝).
_VERDICT_LABELS = {
    "significant_drop": "유의한 하락",
    "significant_rise": "유의한 상승",
    "no_significant_change": "유의한 변화 없음",
}


def _summarize_events(events: list[dict]) -> str:
    """I-14 rows 를 kv 나열로 요약한다(중간 상세도).

    [#194] rows 는 groupBy 에 따라 Row/MemberRow 이형(異形) dict 라 키를 고정하지 않고
    상위 seller_summary_max_events 건까지 "key=value" 형태로 그대로 노출한다 —
    워커가 어느 shape 든 최소한의 재료를 갖는다.
    """
    settings = get_settings()
    shown = events[: settings.seller_summary_max_events]
    lines = ["{" + ", ".join(f"{k}={v}" for k, v in event.items()) + "}" for event in shown]
    omitted = len(events) - len(shown)
    omitted_note = f" 외 {omitted}건" if omitted > 0 else ""
    return "; ".join(lines) + omitted_note


# ── 조회 도구 (읽기 전용) ──


@tool
@_traced_tool("tool.get_sales_timeseries")
@_guard_period_args
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
    # [#290] daily 는 요청 기간 앞에 lookback 을 붙여 조회한다 — STL(period 7)은
    # 최소 2주기 이력이 있어야 계절 성분을 추정한다. 요약·상세 나열은 요청 기간 내만
    # 쓰고, lookback 구간은 이상 감지 학습에만 쓴다(판매자에게 요청 밖 수치 미노출).
    fetch_from = from_date
    if granularity == "daily":
        try:
            parsed_from = date.fromisoformat(from_date)
        except ValueError:
            pass  # 형식 오류는 확장 없이 그대로 — Spring 검증/오류 경로에 맡긴다.
        else:
            fetch_from = (
                parsed_from - timedelta(days=settings.seller_analysis_lookback_days)
            ).isoformat()
    try:
        result = await get_spring_client().get_sales(brand_id, fetch_from, to_date, granularity)
    except SpringUnavailableError as exc:
        return (
            f"Error: 매출 데이터를 불러오지 못했습니다({exc}). "
            "다른 기간으로 다시 시도하거나 없이 진행하세요."
        )
    # 요청 기간 내 포인트(ISO 날짜라 문자열 비교 = 날짜 비교). lookback 은 학습 전용.
    # [PR 리뷰] 이 필터는 **daily 전용**이다 — lookback 확장 조회를 한 유일한 경로라
    # 요청 밖 구간을 걷어낼 필요가 있다. weekly/monthly 는 확장 조회가 없고, 버킷
    # date 가 버킷 시작일(요청 from 이전일 수 있음)인지 계약(I-6)에 정의가 없어
    # 무조건 필터하면 정상 첫 버킷이 조용히 빠져 합계가 축소될 수 있다 — 검증 안 된
    # 전제에 기대지 않고 종전(PR 이전) 동작대로 전체 series 를 합산한다.
    window_points = (
        [p for p in result.series if p.date >= from_date]
        if granularity == "daily"
        else list(result.series)
    )
    total_sales = sum(point.sales for point in window_points)
    total_orders = sum(point.order_count for point in window_points)
    # [#489] salesCount(판매 **수량**)는 nullable — granularity=summary 응답에는
    # 수량 필드가 없고 BE 미배포 구간에서도 null 이다. null 을 0 으로 섞으면
    # "안 팔림"으로 오독되므로 값이 있는 포인트만 더하고, 전량 null 이면 표기
    # 자체를 생략한다(#197 silent-mismatch 재도입 금지).
    measured_quantities = [p.sales_count for p in window_points if p.sales_count is not None]
    if not measured_quantities:
        quantity_note = ""
    elif len(measured_quantities) == len(window_points):
        quantity_note = f", 판매 {sum(measured_quantities):,}개"
    else:
        # 일부 포인트만 수량이 오면(미배포 구간 혼재) 몇 개 기준 합계인지 밝힌다.
        quantity_note = (
            f", 판매 {sum(measured_quantities):,}개"
            f"({len(measured_quantities)}/{len(window_points)}개 포인트 집계)"
        )
    # 상세 포함+상한(안 1, 2026-07-17 확정): 워커가 추이를 직접 서술할 수 있도록
    # 포인트별 수치를 나열하되 seller_summary_max_points 로 컨텍스트 폭주를 막는다.
    shown = window_points[: settings.seller_summary_max_points]
    detail_lines = ", ".join(
        f"{p.date} {p.sales:,}원/{p.order_count}건"
        + (f"/{p.sales_count}개" if p.sales_count is not None else "")
        for p in shown
    )
    omitted = len(window_points) - len(shown)
    omitted_note = f" (외 {omitted}개 포인트 생략)" if omitted > 0 else ""
    # STL period(seller_stl_period)는 "일" 단위 요일 주기 전제 — daily 일 때만 이상 감지.
    if granularity == "daily":
        # Spring 의 isAnomaly/deviationPct 는 참고치일 뿐 — 원시 sales 로 재판정한다(§0.1 D).
        # [#290] SMA 편차 → S-H-ESD(STL 잔차 + robust GESD) 교체. 요일 효과를 분해로
        # 걷어내 "주말이라 원래 낮음"을 급락으로 오탐하지 않는다(worker-papers.md).
        try:
            anomalies = timeseries.detect_seasonal_anomalies(
                [p.date for p in result.series],
                [float(p.sales) for p in result.series],
                period=settings.seller_stl_period,
                alpha=settings.seller_gesd_alpha,
                max_anomalies_ratio=settings.seller_gesd_max_anomalies_ratio,
                min_history_for_stl=settings.seller_min_history_for_stl,
            )
        except ValueError as exc:
            # [#194 리뷰 3 계승] §3.4 degrade 규약 — 설정 검증은 기동 시점(config.py)이
            # 1차 방어지만, 그 안전망이 우회·회귀로 뚫려도 이 도구만 죽지 않는다.
            _log.warning("이상 감지 판정 불가 — 분석 설정 오류: %s", exc)
            anomaly_note = " 이상 감지 판정 불가(분석 설정 오류)."
        else:
            # lookback 구간에서 검출된 이상은 요청 밖이라 보고하지 않는다(질문 범위 준수).
            flagged = [_format_seasonal_anomaly(a) for a in anomalies if a.date >= from_date]
            seasonal_adjusted = len(result.series) >= settings.seller_min_history_for_stl
            method_note = (
                "STL 계절조정·GESD"
                if seasonal_adjusted
                else f"robust 판정 — 이력 {len(result.series)}일"
                f"<{settings.seller_min_history_for_stl}일이라 계절 미조정"
            )
            anomaly_note = (
                f" 이상 감지 {len(flagged)}건({method_note}): " + ", ".join(flagged) + "."
                if flagged
                else f" 이상 감지 없음({method_note})."
            )
    else:
        anomaly_note = ""
    return (
        f"기간 {from_date}~{to_date} 총매출 {total_sales:,}원, 주문 {total_orders}건"
        f"{quantity_note}.\n"
        f"{granularity} 상세: {detail_lines}{omitted_note}.{anomaly_note} "
        f"{_reference_note(from_date, to_date)}"
    )


def _format_seasonal_anomaly(anomaly) -> str:
    """SeasonalAnomaly 1건 → 요약 문구. 편차% 미정의(기대 0 이하)·무한 σ를 구분 표기한다."""
    direction = "급락" if anomaly.direction == "drop" else "급증"
    pct = (
        f"계절조정 {anomaly.deviation_pct:+.1f}%"
        if anomaly.deviation_pct is not None
        else "편차% 산정 불가(기대 0원 이하)"
    )
    sigma = "σ산정 불가(무변동 이력)" if anomaly.sigma == float("inf") else f"{anomaly.sigma:.1f}σ"
    return (
        f"{anomaly.date} 실측 {anomaly.actual:,.0f}원·기대 {anomaly.expected:,.0f}원"
        f" ({pct}, {sigma}, {direction})"
    )


@tool
@_traced_tool("tool.get_funnel")
@_guard_period_args
async def get_funnel(runtime: ToolRuntime[SellerContext], from_date: str, to_date: str) -> str:
    """구매전환 퍼널(조회→장바구니→결제→구매) 단계별 인원·전환율을 요약한다.

    conversion·behavior·chart 워커가 참고한다(I-7, api-spec §4.4).

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
    """
    brand_id = runtime.context.brand_id
    settings = get_settings()
    try:
        result = await get_spring_client().get_funnel(brand_id, from_date, to_date)
    except SpringUnavailableError as exc:
        return f"Error: 퍼널 데이터를 불러오지 못했습니다({exc})."
    # [#290] 직전 인접 동일 길이 기간 자동 추가 조회 — 기간 비교 z-검정(Sismeiro
    # 단계분해 축약형)의 비교쌍. 직전 기간 실패는 보조 조회라 본 요약을 죽이지
    # 않고 비교만 생략한다(§3.4 degrade 관용).
    prev_result = None
    prev_range = _previous_period(from_date, to_date)
    prev_skip_reason = "직전 기간 산정 불가(날짜 해석 실패)" if prev_range is None else ""
    if prev_range is not None:
        try:
            prev_result = await get_spring_client().get_funnel(brand_id, *prev_range)
        except SpringUnavailableError as exc:
            _log.warning("퍼널 직전 기간 조회 실패 — 비교 생략: %s", exc)
            prev_skip_reason = f"직전 기간({prev_range[0]}~{prev_range[1]}) 조회 실패"
    rates = calc.conversion_rates(result)
    # [PR#184 리뷰 반영] 미집계 단계(I-7 count=null·computable=false)는 "실제 0건"이
    # 아니다 — 카운트·전환율 모두 "미집계"로 표기해 LLM 이 0% 전환으로 오해석하지
    # 않게 한다(0 으로 내보내면 워커가 "전환 전무"로 보고할 위험).
    uncomputable = set(result.uncomputable_stages)
    prev_rates = calc.conversion_rates(prev_result) if prev_result is not None else None

    def _count(field: str, value: int) -> str:
        return "미집계" if field in uncomputable else str(value)

    def _stage_summary(
        label: str, key: str, successes: int, trials: int, prev_successes: int, prev_trials: int
    ) -> str:
        """단계 1개의 전환율 + Wilson CI + 직전 기간 z-검정 문구 (#290).

        [PR 리뷰] I-7 은 이벤트 기반 카운트라 단계 역전(cart>view — view 이벤트
        유실·목록 페이지 직행 담기 등)이 실데이터에서 가능하고, 스키마(FunnelResult)는
        단계 간 관계를 강제하지 않는다. wilson_interval/compare_rates 는 그 입력을
        ValueError 로 거부하므로 여기서 잡아 **해당 단계만** 판정을 생략한다(§3.4 —
        다른 신규 호출부와 동일 패턴). clamp 로 정상 CI 처럼 위장하지 않는다
        (정합 깨진 데이터의 0/100% 위장 금지 원칙과 동일 취지).
        """
        rate = rates[key]
        if rate is None:
            return f"{label} 미집계"
        segment = f"{label} {rate:.1f}%"
        if trials > 0:
            try:
                est = proportions.wilson_interval(
                    successes, trials, confidence=settings.seller_wilson_confidence
                )
            except ValueError as exc:
                _log.warning("퍼널 단계 %s CI·검정 불가 — 카운트 정합 이상: %s", label, exc)
                return (
                    f"{label} {rate:.1f}% [CI·검정 불가 — 단계 카운트 정합 이상"
                    f"({successes}>{trials})]"
                )
            segment += (
                f" [{settings.seller_wilson_confidence:.0%} CI"
                f" {est.ci_low:.1%}~{est.ci_high:.1%}, n={trials}]"
            )
        prev_rate = prev_rates[key] if prev_rates is not None else None
        if prev_rate is not None and trials > 0 and prev_trials > 0:
            try:
                comparison = proportions.compare_rates(
                    successes,
                    trials,
                    prev_successes,
                    prev_trials,
                    alpha=settings.seller_rate_test_alpha,
                    confidence=settings.seller_wilson_confidence,
                )
            except ValueError as exc:
                # 직전 기간 쪽 카운트 정합 이상 — 현재 기간 CI 는 유효하므로 유지하고
                # 기간 비교만 생략한다(부분 degrade).
                _log.warning("퍼널 단계 %s 기간 비교 불가 — 카운트 정합 이상: %s", label, exc)
                segment += "(직전 기간 검정 불가 — 카운트 정합 이상)"
            else:
                segment += (
                    f"(직전 {prev_rate:.1f}%, p={comparison.p_value:.3f}"
                    f" — {_VERDICT_LABELS[comparison.verdict]})"
                )
        elif prev_result is not None:
            # 직전 기간은 받았지만 이 단계가 미집계/표본 0 — 단계 단위로 검정 제외.
            segment += "(직전 기간 검정 제외 — 미집계/표본 없음)"
        return segment

    stage_segments = [
        _stage_summary(
            "view→cart",
            "view_to_cart",
            result.cart,
            result.view,
            prev_result.cart if prev_result else 0,
            prev_result.view if prev_result else 0,
        ),
        _stage_summary(
            "cart→checkout",
            "cart_to_checkout",
            result.checkout,
            result.cart,
            prev_result.checkout if prev_result else 0,
            prev_result.cart if prev_result else 0,
        ),
        _stage_summary(
            "checkout→purchase",
            "checkout_to_purchase",
            result.purchase,
            result.checkout,
            prev_result.purchase if prev_result else 0,
            prev_result.checkout if prev_result else 0,
        ),
    ]
    uncomputable_note = (
        " ※ '미집계' 단계는 해당 구간 집계가 불가한 것(0건 아님) — 관련 전환율은 판단 제외."
        if uncomputable
        else ""
    )
    prev_note = (
        f" ※ {prev_skip_reason} — 기간 비교 생략."
        if prev_skip_reason
        else (
            f" (직전 기간 {prev_range[0]}~{prev_range[1]} 대비 양측 z-검정,"
            f" α={settings.seller_rate_test_alpha})"
            if prev_result is not None
            else ""
        )
    )
    return (
        f"조회 {_count('view', result.view)}→장바구니 {_count('cart', result.cart)}"
        f"→결제 {_count('checkout', result.checkout)}"
        f"→구매 {_count('purchase', result.purchase)}, "
        f"전환율 {' · '.join(stage_segments)}.{prev_note}{uncomputable_note} "
        f"{_reference_note(from_date, to_date)}"
    )


# [#488] purchaseComplete 는 **주문 기준** 집계다 — order_item × product × brand
# 조인의 PAID(paid_at) 건을 COUNT(DISTINCT order_id) 한 값이라 I-7 퍼널 4단과 같은
# 정본이고, 이벤트 유실·미귀속과 무관하며 과거 구간도 소급 복구된다. 구 규정
# ("이벤트 기준, 권위는 I-6/I-14", #196)은 근본 수정 배포(jarvis-backend#62,
# 2026-07-31 개정)로 폐기됐다 — 그 문구를 남겨 두면 워커가 실재하는 구매를
# '신뢰 불가'로 취급하고 다른 도구로 우회한다(능동적 오정보).
# 남는 오해석 위험은 '권위'가 아니라 '집계 단위'(건수≠수량, 상품별 합 > 합계)라
# 노트도 그쪽만 통제한다.
# [#489] 그 '집계 단위' 통제의 ① 항이 이제 대안 필드를 지시한다 — salesQuantity
# 가 같은 행에 실리기 시작했으므로, 수량 질문을 막기만 하고 어디로 보낼지
# 알려주지 않으면 워커가 건수를 수량으로 우회 사용한다.
_BEHAVIOR_PURCHASE_RULES_NOTE = (
    "※ purchaseComplete 는 주문 기준 집계(PAID 주문의 상품별 주문 건수)이며 "
    "퍼널 4단(구매)과 같은 정본이다 — 이벤트 유실과 무관하니 0 은 실제 '구매 "
    "없음'으로 읽어도 된다. ① 한 주문에 같은 상품을 여러 개 담아도 1 — 건수이지 "
    "수량이 아니다. 수량은 같은 행의 salesQuantity(PAID SUM(quantity), 취소·"
    "반품 제외, #489)를 쓸 것. ② 상품별 값의 합이 "
    "합계(eventType 집계)보다 클 수 있다(한 주문에 자사 상품이 여러 종) — 버그 "
    "아님. ③ 부분 취소·반품이 반영돼 과거 기간을 재조회하면 값이 줄 수 있다."
)


# [#489] I-13 counts 키 단일 출처 — 2026-08-06 개정으로 4종 → 5종(removeFromCart
# 편입). 종전엔 표시 행·꼬리 합계 키 튜플·꼬리 출력 3곳이 각각 4종을 하드코딩해
# 어휘가 늘 때마다 누락 지점이 생겼다. 순서는 퍼널 진행 순이며, 집합 자체는
# rows 정렬 기준(활동량 = counts 합)과 동일하다.
_BEHAVIOR_COUNT_KEYS = (
    "productView",
    "addToCart",
    "removeFromCart",
    "checkoutStart",
    "purchaseComplete",
)
_BEHAVIOR_COUNT_LABELS = {
    "productView": "조회",
    "addToCart": "담기",
    "removeFromCart": "삭제",
    "checkoutStart": "결제시작",
    "purchaseComplete": "구매",
}


# 군집 문구에 나열할 소속 상품 id 상한 — 대군집이 컨텍스트를 폭주시키지 않게.
_CLUSTER_PRODUCT_ID_MAX = 5


def _summarize_behavior_clusters(rows: list, settings) -> str:
    """I-13 상품 rows 를 k-means 군집 요약 문구로 바꾼다 (#290 — behavior 워커).

    빈 결과는 군집 생략 사유(상품 수 미달·분리 불능)를 명시한다 — LLM 이 "군집이
    없다 = 패턴이 없다"로 오해석하지 않게. 중심값은 원 피처 단위(비율·log 조회)라
    LLM 이 그대로 서술 근거로 쓸 수 있다.
    """
    # [#489] 피처는 의도적으로 4종 유지 — removeFromCart 를 넣으면 군집 라벨·
    # 실루엣·중심값 해석이 전부 바뀐다(Moe 2003 유형론 재정의). 어휘 확장과
    # 분석 피처 변경은 별개 결정이라 이번 스코프 밖이다.
    activities = [
        {
            "product_id": row.product_id,
            "view": row.counts.get("productView", 0),
            "cart": row.counts.get("addToCart", 0),
            "checkout": row.counts.get("checkoutStart", 0),
            "purchase": row.counts.get("purchaseComplete", 0),
            "visitors": row.unique_visitors,
        }
        for row in rows
    ]
    try:
        clusters = segmentation.cluster_products(
            activities,
            k_min=settings.seller_behavior_kmeans_k_min,
            k_max=settings.seller_behavior_kmeans_k_max,
            random_state=settings.seller_kmeans_random_state,
        )
    except ValueError as exc:
        # §3.4 degrade — 설정 오류(기동 검증 우회·회귀)에도 상품 나열 요약은 살린다.
        _log.warning("행동 군집화 불가 — 분석 설정 오류: %s", exc)
        return " 군집 분석 불가(분석 설정 오류)."
    if not clusters:
        floor = settings.seller_behavior_kmeans_k_min * 3
        reason = (
            f"상품 {len(rows)}개 < 최소 {floor}개"
            if len(rows) < floor
            else "전 상품 행동 패턴 동일(분리 불능)"
        )
        return f" 군집 생략({reason}) — 위 상품별 수치로 직접 판단."
    parts = []
    for cluster in clusters:
        ids = ", ".join(str(pid) for pid in cluster.product_ids[:_CLUSTER_PRODUCT_ID_MAX])
        ids_note = (
            f"{ids} 외 {cluster.size - _CLUSTER_PRODUCT_ID_MAX}개"
            if cluster.size > _CLUSTER_PRODUCT_ID_MAX
            else ids
        )
        centroid = cluster.centroid
        parts.append(
            f"[{cluster.label}] {cluster.size}개(id: {ids_note}) — 중심: "
            f"담기율 {centroid['cart_per_view']:.1%}·결제진입률 {centroid['checkout_per_cart']:.1%}"
            f"·구매완료율 {centroid['purchase_per_checkout']:.1%}"
        )
    return (
        f" 행동 군집 {len(clusters)}개(k-means, 실루엣 {clusters[0].silhouette:.2f}): "
        + "; ".join(parts)
        + "."
    )


def _summarize_ratio_outliers(rows: list, settings) -> str:
    """상품별 비율의 브랜드 내 Tukey 상위 fence 초과 요약 (#290 abuse Contextual 트랙).

    지표는 전부 "높을수록 의심" 방향(Tan & Kumar 봇 피처의 집계 단위 번역):
    - 조회/구매(view/purchase): **purchase>0 상품만** — 순수 비율 분포로 검정한다.
      [PR 리뷰] 구 방식(purchase=0 → 분모 1 치환)은 비율과 원시 조회수를 한 분포에
      섞어 fence 를 왜곡했다 — 구매 0 대량 조회 상품이 분포를 부풀려 진짜 비율
      이상치가 fence 아래 숨는 오미탐 경로.
    - 구매 0 조회 폭증: purchase=0 상품은 별도 판정 — **브랜드 전체 조회량 분포**의
      상위 fence 초과일 때만 "조회 폭증+구매 0" 패턴으로 표기한다(정의 그대로).
    - 담기율(cart/view): 장바구니 어뷰징(담기 봇) 신호.
    - 방문자당 조회(view/visitors): 소수 방문자의 반복 조회(크롤러/봇) 신호.
      visitors 결측 상품은 이 지표에서 제외한다(결측 0 위장 금지).
    """
    # [#489] 지표는 의도적으로 종전 3종 유지 — removeFromCart 기반 "삭제율" 신설은
    # Tukey fence 를 새 분포 위에 세우는 일이라 별도 검증이 필요하다(이번 스코프 밖).
    per_metric: dict[str, list[tuple[str, float]]] = {
        "조회/구매": [],
        "담기율": [],
        "방문자당 조회": [],
    }
    view_items: list[tuple[str, float]] = []  # 전 상품 조회량 — 구매 0 폭증 판정의 분포
    zero_purchase_ids: set[str] = set()
    for row in rows:
        counts = row.counts
        view = counts.get("productView", 0)
        purchase = counts.get("purchaseComplete", 0)
        cart = counts.get("addToCart", 0)
        target = f"[{row.product_id}]"
        if view > 0:
            view_items.append((target, float(view)))
            if purchase > 0:
                per_metric["조회/구매"].append((target, view / purchase))
            else:
                zero_purchase_ids.add(target)
            per_metric["담기율"].append((target, cart / view))
            if row.unique_visitors:
                per_metric["방문자당 조회"].append((target, view / row.unique_visitors))
    flags = []
    for metric, items in per_metric.items():
        flags.extend(outliers.tukey_upper_outliers(items, k=settings.seller_tukey_k, metric=metric))
    # 구매 0 트랙 — fence 는 브랜드 전체 조회량 분포로 만들되, 구매 0 상품의 초과만
    # 이상으로 본다(구매가 있는 고조회 상품은 인기이지 어뷰징 신호가 아니다).
    zero_purchase_flags = [
        flag
        for flag in outliers.tukey_upper_outliers(
            view_items, k=settings.seller_tukey_k, metric="조회량"
        )
        if flag.target in zero_purchase_ids
    ]
    if not flags and not zero_purchase_flags:
        # 최대 표본 기준으로 판정 수행 여부를 구분한다 — "없음"과 "판정 보류"는 다르다.
        max_items = max((len(items) for items in (*per_metric.values(), view_items)), default=0)
        if max_items >= 4:
            return f" 상품 비율 이상치 없음(Tukey Q3+{settings.seller_tukey_k:g}×IQR 기준)."
        return " 상품 비율 이상치 판정 보류(비교 가능 상품 4개 미만)."
    parts = [
        f"{flag.target} {flag.metric} {flag.value:,.1f}(상위 기준 {flag.threshold:,.1f} 초과)"
        for flag in flags
    ]
    parts.extend(
        f"{flag.target} 조회량 {flag.value:,.0f}"
        f"(브랜드 상위 기준 {flag.threshold:,.0f} 초과, 구매 0 — 조회 폭증 패턴)"
        for flag in zero_purchase_flags
    )
    total = len(flags) + len(zero_purchase_flags)
    return (
        f" 상품 비율 이상치 {total}건"
        f"(브랜드 내 Tukey Q3+{settings.seller_tukey_k:g}×IQR 초과): " + ", ".join(parts) + "."
    )


def _format_sales_quantity(row) -> str:
    """I-13 salesQuantity 표기 (#489). 0 과 null 을 절대 뭉개지 않는다.

    `0` = "안 팔림"(조회했고 값이 0), `null` = "미조회"(eventType 필터에
    purchase_complete 가 없어 BE 가 계산조차 하지 않음). `x or '-'` 같은 falsy
    축약을 쓰면 0 이 '-' 로 뭉개져 churn_rate 에서 잡았던 silent-mismatch(#197)를
    그대로 재현한다 — 명시 분기를 유지할 것.
    """
    if row.sales_quantity is None:
        return "판매수량 -(미조회)"
    return f"판매 {row.sales_quantity}개"


def _format_dwell(row) -> str:
    """I-13 체류시간 표기 (#489). 표본 수 없이 평균·중앙값만 내보내지 않는다.

    명세: `dwellSampleCount` 없이 평균·중앙값만 해석하면 안 된다(표본이 적으면
    판정 보류 — conversion 워커의 유의성 판정 원칙과 동일). 그래서 표본이 없거나
    0 이면 중앙값·평균이 실려 와도 수치를 감추고 사유만 남긴다. 주 지표는
    medianDwellSeconds 이고 avg 는 롱테일 왜곡 확인용 참고값이라 순서를 고정한다.
    """
    if not row.dwell_sample_count:
        return ", 체류 -(표본 없음)"
    parts = []
    if row.median_dwell_seconds is not None:
        parts.append(f"중앙 {row.median_dwell_seconds:.0f}초")
    if row.avg_dwell_seconds is not None:
        parts.append(f"평균 {row.avg_dwell_seconds:.0f}초")
    if not parts:
        # 표본 수만 오고 수치가 비는 건 계약상 없는 조합 — 관대 수신하되 표면화한다.
        return f", 체류 -(수치 없음, n={row.dwell_sample_count})"
    return f", 체류 {'·'.join(parts)}(n={row.dwell_sample_count})"


def _dwell_source_note(rows: list) -> str:
    """체류 산출 근거 각주 — 행마다 반복하지 않고 요약 말미에 1회 붙인다 (#489).

    next_event 기준은 세션의 마지막 조회가 표본에서 빠진다(page_leave 미수집 —
    구조적 한계). 이 한계를 안 알리면 LLM 이 체류시간을 실제 관심도의 완전한
    측정치로 서술한다. 기준이 섞여 있으면(향후 page_leave 승격 구간) 구간 비교
    금지를 경고한다 — 판정 기준 변경 시 집계 구간을 나눈다는 명세 규약.
    """
    sources = {row.dwell_source for row in rows if row.dwell_source}
    if not sources:
        return ""
    if sources == {"next_event"}:
        return (
            " ※ 체류시간은 다음 이벤트까지의 차분(next_event)이라 세션의 마지막"
            " 조회가 표본에서 빠진다 — 실제보다 짧게 나올 수 있다."
        )
    return (
        f" ※ 체류시간 산출 기준 혼재({'/'.join(sorted(sources))}) — 기준이 바뀐"
        " 구간이라 앞뒤 값을 직접 비교하지 말 것."
    )


def _summarize_behavior(result: BehaviorEventsResult) -> str:
    """I-13 응답을 groupBy 3형에 맞춰 요약한다(REALIGN ②-3 — 확정 명세 기준)."""
    settings = get_settings()
    if result.rows:  # groupBy=product (기본)
        # [#196] 상한은 seller_summary_max_products(I-13 전용) — I-14 용
        # seller_summary_max_events 와 분리(브랜드 상품 수 > 상한이면 상시 잘리던 문제).
        # rows 는 BE 가 활동량(4종 합계) 내림차순으로 내려준다(api-spec §4.4 I-13) —
        # 잘리는 건 저활동 상품이고, 잘린 행도 아래 꼬리 합계로 남겨 정보 소실을 막는다.
        shown = result.rows[: settings.seller_summary_max_products]
        lines = []
        for row in shown:
            counts_text = " ".join(
                f"{_BEHAVIOR_COUNT_LABELS[key]} {row.counts.get(key, 0)}"
                for key in _BEHAVIOR_COUNT_KEYS
            )
            rate = f"{row.view_to_cart_rate:.1%}" if row.view_to_cart_rate is not None else "-"
            visitors = row.unique_visitors if row.unique_visitors is not None else "-"
            lines.append(
                f"[{row.product_id}] {row.product_name or '이름없음'} "
                f"{counts_text} {_format_sales_quantity(row)} "
                f"(조회→담기 {rate}, 방문자 {visitors}{_format_dwell(row)})"
            )
        omitted_rows = result.rows[len(shown) :]
        if omitted_rows:
            tail_text = " ".join(
                f"{_BEHAVIOR_COUNT_LABELS[key]} {sum(r.counts.get(key, 0) for r in omitted_rows)}"
                for key in _BEHAVIOR_COUNT_KEYS
            )
            # [#489] 수량 꼬리 합계는 null(미조회)을 0 으로 섞으면 안 되므로 값이
            # 있는 행만 더하고 몇 건 기준인지 남긴다. 전량 null 이면 생략한다.
            measured = [r.sales_quantity for r in omitted_rows if r.sales_quantity is not None]
            tail_quantity = (
                f" 판매 {sum(measured)}개({len(measured)}/{len(omitted_rows)}건 집계)"
                if measured
                else ""
            )
            omitted_note = f" 외 {len(omitted_rows)}건(저활동) 합계: {tail_text}{tail_quantity}"
        else:
            omitted_note = ""
        # [#290] 상품 군집화(Chen 2012 k-means + Moe 2003 유형론) — 절단 전 전체 rows 로
        # 군집을 만들어 LLM 이 상품 나열이 아니라 군집 단위(카트이탈형 등)로 해석하게 한다.
        cluster_note = _summarize_behavior_clusters(result.rows, settings)
        # [#290] abuse Contextual 트랙 — 브랜드 내 비율 분포의 Tukey 상위 fence 초과.
        ratio_note = _summarize_ratio_outliers(result.rows, settings)
        dwell_note = _dwell_source_note(result.rows)
        return (
            f"상품별 {len(shown)}건: "
            + "; ".join(lines)
            + omitted_note
            + cluster_note
            + ratio_note
            + dwell_note
        )
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
@_traced_tool("tool.get_behavior_events")
@_guard_period_args
async def get_behavior_events(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    event_type: list[str] | None = None,
    product_id: int | None = None,
    group_by: str | None = None,
) -> str:
    """브랜드 상품의 행동 이벤트를 집계 조회한다(I-13, behavior_events 원천).

    상품 연계 **5종**(product_view/add_to_cart/remove_from_cart/checkout_start/
    purchase_complete)만 집계된다 — 전역 행동(검색·페이지뷰 등)은 이 도구로
    조회 불가. [2026-08-06 개정, #489] remove_from_cart 편입(4종 → 5종).

    groupBy=product 행에는 판매 수량(salesQuantity)과 체류시간 4종이 함께 온다.
    ⚠️ event_type 을 좁혀 호출하면 그 필터에 없는 지표는 **0 이 아니라 미조회**로
    떨어진다 — purchase_complete 를 빼면 판매 수량이, product_view 를 빼면
    체류시간이 계산되지 않는다. 수량·체류가 궁금하면 필터를 걸지 말 것.

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
        event_type: 이벤트 유형 필터(선택, 복수) — 5종 중 선택, 미지정 시 전체.
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
    # [#290] abuse Point 트랙 — date-groupBy 일별 총량의 MAD 스파이크 + 가격/재고
    # 변경일 겹침 대조(오탐 통제). product-groupBy 의 Contextual 트랙은
    # _summarize_behavior 내부(_summarize_ratio_outliers)에서 부착된다.
    spike_note = ""
    if result.series:
        spike_note = await _point_spike_note(brand_id, from_date, to_date, result.series)
    return (
        f"{summary}.{spike_note} {_BEHAVIOR_PURCHASE_RULES_NOTE} "
        f"{_reference_note(from_date, to_date)}"
    )


async def _point_spike_note(brand_id: int, from_date: str, to_date: str, series: list) -> str:
    """I-13 일별 시계열의 볼륨 스파이크 요약 (#290 abuse Point 트랙 — Chandola).

    스파이크일이 가격/재고 변경 이력(I-15)과 겹치면 "정상 설명 후보"를 동봉한다 —
    프로모션 가격 인하로 인한 트래픽 급증을 봇으로 단정하는 오탐을 통제한다.
    I-15 실패는 보조 대조 실패 — 스파이크 보고는 유지하고 대조만 생략(§3.4 관용).
    """
    settings = get_settings()
    dates: list[str] = []
    totals: list[float] = []
    for point in series:
        day = point.get("date")
        if day is None:
            continue  # 비정상 행 관대 수신
        dates.append(str(day))
        # [#489] series 키는 동적이라 date 외 정수값을 전부 더한다 — 2026-08-06
        # removeFromCart 편입으로 **코드 변경 없이 총량 정의가 5종 합으로 바뀐다**.
        # 스파이크 판정은 같은 분포 안의 상대 검정(robust z)이라 유효하지만, 개정
        # 전후 구간의 절대 총량을 직접 비교하면 안 된다.
        totals.append(float(sum(v for k, v in point.items() if k != "date" and isinstance(v, int))))
    try:
        spikes = outliers.mad_spikes(
            dates, totals, threshold=settings.seller_mad_threshold, metric="일별 이벤트 총량"
        )
    except ValueError as exc:
        _log.warning("볼륨 스파이크 판정 불가 — 분석 설정 오류: %s", exc)
        return " 일별 볼륨 스파이크 판정 불가(분석 설정 오류)."
    if not spikes:
        return f" 일별 볼륨 스파이크 없음(robust z<{settings.seller_mad_threshold})."
    change_dates: set[str] = set()
    overlap_checked = True
    try:
        changes = await get_spring_client().get_product_changes(
            brand_id, from_date, to_date, None, None
        )
        change_dates = {row.created_at[:10] for row in changes.rows if row.created_at}
    except SpringUnavailableError as exc:
        _log.warning("스파이크-변경 이력 대조 불가(I-15 실패) — 대조 생략: %s", exc)
        overlap_checked = False
    parts = []
    for spike in spikes:
        z_note = "∞" if spike.value == float("inf") else f"{spike.value:.1f}"
        raw = totals[dates.index(spike.target)]
        note = f"{spike.target} 총 {raw:,.0f}건(robust z {z_note}≥{spike.threshold:g})"
        if spike.target in change_dates:
            note += " ※당일 가격/재고 변경 이력 있음 — 정상 설명 후보"
        parts.append(note)
    unchecked_note = " (변경 이력 대조 실패 — 겹침 미확인)" if not overlap_checked else ""
    return (
        f" 일별 볼륨 스파이크 {len(spikes)}건(봇 유입 신호 후보): "
        + ", ".join(parts)
        + f".{unchecked_note}"
    )


# customerLabel 규약 문구 (I-14·I-16 공용, #487 — 노션 2026-08-06 개정).
# I-14 기록 규칙 안에 섞여 있던 것을 상수로 뽑았다 — 복붙본이 갈라지면 한쪽 경로의
# 규약만 낡는다(#487 이 고친 결함이 정확히 "I-16 만 미반영"이었다).
_CUSTOMER_LABEL_NOTE = (
    "※ customerLabel은 개인정보 보호용 사례번호(회원 키 아님) — 실명·연락처 추정, "
    "orderId 대조 유도, IP와 개별 고객 직접 연결 금지. 조치가 필요하면 "
    "'사례번호 XXXXXX로 관리자 문의'로 안내."
)

# I-14/I-15 기록 규칙 주의 문구 (REALIGN ②-4 — schema.sql D32/D34 확정 반영).
# 도구 출력에 상시 부착해 워커가 로그 부재를 '데이터 이상'으로 오해석하는 것을 막는다.
_ORDER_LOG_RULES_NOTE = (
    "※ 기록 규칙: 구매확정·클레임 신청은 로그에 없음(취소/반품은 완료 시점만). "
    "아이템 전이(SHIPPING/DELIVERED/CANCELLED/RETURNED)는 아이템 단위 1행"
    "(orderItemId 값 있음) — 같은 orderId 행 복수는 아이템별 전이(중복 아님). "
    "주문 전이(PENDING/PAID/PAYMENT_FAILED)는 orderItemId null이며 "
    "개정(2026-08-06) 이전 로그도 null일 수 있음. "
) + _CUSTOMER_LABEL_NOTE
_PRODUCT_LOG_RULES_NOTE = (
    "※ 기록 규칙: 주문에 의한 재고 차감은 미기록(수동 조정·품절/재입고 전환만). "
    "품절 신호 = STOCK 변경의 new_value 0 (SOLD_OUT 상태는 없음)."
)


@tool
@_traced_tool("tool.get_order_events")
@_guard_period_args
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

    [개정 2026-08-06 해석 규칙, #481]
    - 회원 노출은 customerLabel(사례번호, 문자열)뿐 — 같은 회원=같은 라벨.
      실명·연락처 추정, orderId 대조 유도 금지. 조치 안내는 "사례번호 X로 관리자 문의".
    - rows 의 orderItemId: 아이템 전이만 값이 있고 주문 전이는 null. 같은 orderId
      행 복수 = 아이템별 전이(중복 아님).
    - 자사 스코프 축소 — 타사 아이템 발송 행이 더는 안 보인다. 과거 동일 기간보다
      행 수·byStatus 가 줄 수 있으며 회귀가 아니다.
    - 단위: byStatus 는 SHIPPING·DELIVERED=아이템 수 / PAID·PAYMENT_FAILED=주문 수
      (층위 상이 — 합산 금지). cancelReasonsTop 의 count 는 아이템 수.
    - 발송(ORDERED→SHIPPING)은 판매자 행위 — actorType "SELLER" 가 정상 값이다.

    Args:
        from_date: 조회 시작일(YYYY-MM-DD).
        to_date: 조회 종료일(YYYY-MM-DD).
        to_status: 전이 대상 상태(선택) — 주문: PENDING/PAID/PAYMENT_FAILED/CANCELLED,
            아이템: SHIPPING/DELIVERED/CANCELLED/RETURNED (교환 어휘 없음).
        actor_type: 전이 주체(선택) — USER/SELLER/ADMIN/SYSTEM.
            memberId 집계 시에는 무시된다(아래 group_by 참조).
        group_by: "memberId" 하나뿐(선택) — 회원별 어뷰징 집계로 전환된다.
            rows 가 customerLabel/orderCount/cancelCount/cancelRatio/
            maxOrdersPerHour/isSuspicious(코드 판정)로 바뀐다.
            ※ to_status·actor_type 이 함께 오면 무시한다(코드 강제) —
            분모(orderCount)까지 필터돼 cancelRatio 가 왜곡되기 때문
            (예: CANCELLED 만 남기면 전원 1.0, USER 만 남기면 취소 전이만
            분모에 남음).
        stats: 집계 모드로 조회할지 여부(선택, api-spec §4.4 `stats` 쿼리) —
            rows 없이 byStatus·cancelReasonsTop 만 반환된다.
    """
    brand_id = runtime.context.brand_id
    # [#215 리뷰] memberId 집계에 to_status·actor_type 이 걸리면 Spring 이 분모
    # (orderCount)까지 필터해 cancelRatio 가 왜곡된다 — BE 쿼리(aggregate·maxPerHour
    # 양쪽)의 두 필터가 GROUP BY 이전에 적용되기 때문. 예: to_status=CANCELLED 면
    # 전원 1.0, actor_type=USER 면 취소(USER 전이)만 분모에 남아 정상 회원도 1.0.
    # 왜곡된 isSuspicious 는 '코드 판정 번복 금지' 규칙 탓에 그대로 보고되므로,
    # 프롬프트·docstring(소프트 가드)에만 맡기지 않고 코드에서 무시를 강제한다.
    # [#215 리뷰 2] group_by 는 LLM 이 채우는 자유 문자열(str | None)이라 정확 일치에만
    # 의존하면 "memberid"·" MemberId " 같은 변형에서 가드가 조용히 무력화된다 — Spring 도
    # 등호 비교("memberId".equals)라 변형은 회원 집계 대신 목록 조회로 오동작하므로,
    # 정규화해 가드와 Spring 라우팅을 함께 바로잡는다.
    if group_by is not None and group_by.strip().lower() == "memberid":
        group_by = "memberId"
    ignored_status_note = ""
    if group_by == "memberId" and (to_status or actor_type):
        to_status = None
        actor_type = None
        ignored_status_note = (
            " ※ memberId 집계에서 to_status/actor_type 필터는 무시됨(비율 왜곡 방지)."
        )
    try:
        status_filter = [to_status] if to_status else None
        result = await get_spring_client().get_order_events(
            brand_id, from_date, to_date, status_filter, actor_type, group_by, stats
        )
    except SpringUnavailableError as exc:
        return f"Error: 주문 이벤트 데이터를 불러오지 못했습니다({exc})."
    # [#194] BE 실측 응답(rows/total/byStatus/cancelReasonsTop) 기준으로 요약한다 —
    # 구 events/stats 필드는 Spring 에 없어 항상 0건으로 새던 버그의 원인.
    # stats=true 모드: byStatus + cancelReasonsTop 이 집계 질의의 주 재료.
    stats_note = ""
    if result.by_status is not None:
        stats_note = " 상태별 집계: " + ", ".join(f"{k}={v}" for k, v in result.by_status.items())
        if result.cancel_reasons_top:
            reasons = ", ".join(
                f"{r.get('reason', '?')}({r.get('count', '?')}건)"
                for r in result.cancel_reasons_top
            )
            stats_note += f". 취소 사유 상위: {reasons}"
        stats_note += "."
    if not result.rows and not stats_note:
        # 0건 조기 반환도 무시 고지를 유지한다 — 빠지면 워커가 필터 무시 사실을
        # 모른 채 재호출하거나, 회귀 테스트(#215)가 이 경로에서 깨진다.
        return f"주문 상태 전이 0건.{ignored_status_note} {_reference_note(from_date, to_date)}"
    # rows 는 limit 절단본 — total 이 더 크면 전수를 고지해 표본=전수 오해석을 막는다.
    total = result.total if result.total is not None else len(result.rows)
    total_note = (
        f" (전체 {total}건 중 {len(result.rows)}건 표시)" if total > len(result.rows) else ""
    )
    rows_note = (
        f"주문 상태 전이 {len(result.rows)}건{total_note}: {_summarize_events(result.rows)}."
        if result.rows
        else "주문 상태 전이 목록 없음(집계 모드)."
    )
    return (
        f"{rows_note}{stats_note}{ignored_status_note} "
        f"{_ORDER_LOG_RULES_NOTE} {_reference_note(from_date, to_date)}"
    )


@tool
@_traced_tool("tool.get_product_change_logs")
@_guard_period_args
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
    settings = get_settings()
    try:
        result = await get_spring_client().get_product_changes(
            brand_id, from_date, to_date, change_type, product_id
        )
    except SpringUnavailableError as exc:
        return f"Error: 상품 변경 이력을 불러오지 못했습니다({exc})."
    # [#194] BE 실측 응답(rows/total) 기준 — 구 logs 필드는 Spring 에 없어 항상 0건으로
    # 새던 버그의 원인. 건수만 주던 출력도 상세 나열로 바꾼다 — 워커가 "이상 직전
    # 가격/재고/상태 변경"을 직접 짚을 수 있어야 한다(sales_anomaly 분석 재료).
    if not result.rows:
        return (
            f"상품 변경 이력 0건(가격/재고/상태). "
            f"{_PRODUCT_LOG_RULES_NOTE} {_reference_note(from_date, to_date)}"
        )
    shown = result.rows[: settings.seller_summary_max_events]
    lines = [
        f"[{row.product_id}] {row.product_name or '이름없음'} {row.change_type} "
        f"{row.old_value if row.old_value is not None else '?'}"
        f"→{row.new_value if row.new_value is not None else '?'}"
        f" ({row.created_at or '시각불명'})"
        for row in shown
    ]
    total = result.total if result.total is not None else len(result.rows)
    omitted = total - len(shown)
    omitted_note = f" 외 {omitted}건" if omitted > 0 else ""
    return (
        f"상품 변경 이력 {total}건(가격/재고/상태): " + "; ".join(lines) + omitted_note + ". "
        f"{_PRODUCT_LOG_RULES_NOTE} {_reference_note(from_date, to_date)}"
    )


# I-16 신호 해석 주의 문구 — 상시 부착(#197, _BEHAVIOR_PURCHASE_RULES_NOTE 와 같은 패턴).
_CHURN_SIGNAL_RULES_NOTE = (
    "※ 검색 무결과 세션은 현 수집 스키마상 상시 0(미적재) — '검색 불만 없음'의 "
    "근거로 쓰지 말 것. 이탈률 분모는 기간 내 자사 상품 상호작용 회원(코호트)이다. "
    "신호 순위는 상관이지 인과가 아니다 — 원인 단정 금지."
)


def _reason_total(reasons: list[dict]) -> int:
    """ReasonCount 목록의 count 합 — 비정상 값(비수치)은 0 으로 관대 수신."""
    total = 0
    for reason in reasons:
        count = reason.get("count", 0)
        total += count if isinstance(count, int) else 0
    return total


def _summarize_churn_signals(result, prev_result, *, top_k: int) -> str:
    """pre_churn_signals 를 코호트 대비 정규화해 원인 후보 순위로 요약한다 (#290).

    Ahn 2020 피처 카탈로그의 신호(취소·반품·가격노출·검색무결과)를 count/cohort_size
    비중으로 정규화하고 직전 기간 비중과의 변화(%p)를 병기한다 — LLM 이 "많아
    보이는 것"이 아니라 비중·변화 수치로 원인 후보를 고르게 한다. 원 count 는
    그대로 유지한다(analysis_judge 검증 가능 — 도구 원출력 수치 보존 규약).
    prev_result 는 비교 가능할 때만 넘어온다(코호트 0·결측·실패 시 None → "직전 −").
    """
    signals = result.pre_churn_signals
    if signals is None:
        return " 이탈 전 신호: 미수신."
    cohort = result.cohort_size or 0
    prev_signals = prev_result.pre_churn_signals if prev_result is not None else None
    prev_cohort = prev_result.cohort_size if prev_result is not None else 0

    entries = [
        ("취소", signals.cancel_count, "건", prev_signals.cancel_count if prev_signals else None),
        (
            "반품",
            _reason_total(signals.return_reasons_top),
            "건",
            _reason_total(prev_signals.return_reasons_top) if prev_signals else None,
        ),
        (
            "가격인상 노출",
            signals.price_increase_exposed,
            "명",
            prev_signals.price_increase_exposed if prev_signals else None,
        ),
        (
            "검색 무결과 세션",
            signals.zero_result_search_sessions,
            "건",
            prev_signals.zero_result_search_sessions if prev_signals else None,
        ),
    ]
    # 분모(코호트)가 공통이라 비중 순위 = count 순위 — 동률은 이름순(결정론).
    ranked = sorted(entries, key=lambda e: (-e[1], e[0]))

    def _entry(rank: int, name: str, count: int, unit: str, prev_count: int | None) -> str:
        share = f"{count / cohort:.1%}" if cohort else "?"
        if prev_count is not None and prev_cohort:
            prev_share = prev_count / prev_cohort
            change = (count / cohort - prev_share) * 100 if cohort else None
            prev_note = f"(직전 {prev_share:.1%}, {change:+.1f}%p)" if change is not None else ""
        else:
            prev_note = "(직전 −)"
        return f"{rank}) {name} {count}{unit}·코호트 {share}{prev_note}"

    top = [_entry(i + 1, *e) for i, e in enumerate(ranked[:top_k])]
    rest = ", ".join(f"{name} {count}{unit}" for name, count, unit, _ in ranked[top_k:])
    reasons = (
        ", ".join(
            f"{r.get('reason', '?')}({r.get('count', '?')}건)" for r in signals.return_reasons_top
        )
        or "없음"
    )
    rest_note = f" 그 외: {rest}." if rest else ""
    return (
        f" 이탈 전 신호(원인 후보 상위 {len(top)}, 코호트 대비 정규화): "
        + " ".join(top)
        + f".{rest_note} 반품 사유 상위: {reasons}."
    )


@tool
@_traced_tool("tool.get_churn_cohort")
@_guard_period_args
async def get_churn_cohort(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    inactive_days: int | None = None,
) -> str:
    """이탈 코호트(무활동 고객) 이탈률·이탈 전 신호를 요약한다(I-16, api-spec §4.4).

    코호트 = from~to 기간에 자사 상품과 상호작용한 회원. 이탈 = 그중 최근
    inactive_days 동안 무활동인 회원.

    [단위 주의, 2026-08-06 I-14 개정 파급] 이탈 전 신호의 반품 사유
    (returnReasonsTop) count 는 취소·반품 완료 **아이템 수** 기준이다(구 주문 수 —
    로그가 아이템 단위가 되며 숫자가 커질 수 있고 순위는 대체로 유지).
    cancelCount 는 DISTINCT 주문 수 그대로라 두 신호의 단위가 다르다 — 합산 금지.

    [개정 2026-08-06 해석 규칙, #487 — #481 잔여분]
    - 이탈 회원 노출은 customerLabel(사례번호, 문자열)뿐이다 — 구 memberId(Long)·
      lastLoginAt 은 제거됐다. 같은 브랜드에서 같은 회원이면 같은 라벨이라 I-14
      집계와 대조가 되지만, 실명·연락처 추정과 orderId 대조 유도는 금지다.
    - 라벨 미수신(구응답 구간)은 "[?]"로 떨어진다 — 원시 회원 키로 되돌아가지 않는다.

    Args:
        from_date: 코호트 기간 시작일(YYYY-MM-DD, 필수).
        to_date: 코호트 기간 종료일(YYYY-MM-DD, 필수).
        inactive_days: 무활동 판정 기준일(선택, 미지정 시 설정 기본값).
    """
    brand_id = runtime.context.brand_id
    settings = get_settings()
    # 기본값을 호출 시점에 해석한다 — 임포트 시점 고정 방지(Settings 주입 원칙).
    effective_days = (
        inactive_days if inactive_days is not None else settings.seller_churn_inactive_days
    )
    try:
        result = await get_spring_client().get_churn(brand_id, from_date, to_date, effective_days)
    except SpringUnavailableError as exc:
        return f"Error: 이탈 코호트 데이터를 불러오지 못했습니다({exc})."
    # 코호트 0명(기간 내 상호작용 회원 없음)은 "이탈률 0%"와 다른 상태 — 구분 표기(#197).
    # 직전 기간 조회 전에 조기 반환한다 — 비교 대상 자체가 없어 보조 조회 비용을 아낀다.
    if result.cohort_size == 0:
        return (
            f"코호트 0명 — 기간 내 자사 상품과 상호작용한 회원이 없어 이탈 판정 대상이 "
            f"없습니다. (기준: inactiveDays={effective_days}) "
            f"{_reference_note(from_date, to_date)}"
        )
    # [#290] 직전 인접 동일 길이 기간 추가 조회 — 이탈률 z-검정·신호 변화율의 비교쌍.
    # 보조 조회라 실패해도 본 요약은 계속한다(§3.4 관용).
    prev_result = None
    prev_range = _previous_period(from_date, to_date)
    if prev_range is not None:
        try:
            prev_result = await get_spring_client().get_churn(
                brand_id, prev_range[0], prev_range[1], effective_days
            )
        except SpringUnavailableError as exc:
            _log.warning("이탈 코호트 직전 기간 조회 실패 — 비교 생략: %s", exc)

    # [PR 리뷰] churnRate 는 스키마가 [0,1] 구간을 강제하지 않는다 — BE 정합 이상으로
    # 1 초과·음수가 오면 round(rate×cohort)가 wilson_interval 의 successes 범위를 벗어나
    # ValueError 로 도구가 죽는다(§3.4 위반). fraction 정의역 밖 값은 계산 전에 걸러
    # 판정 보류로 표기한다 — clamp 로 100% CI 처럼 위장하지 않는다(#197 위장 금지 계승).
    def _valid_fraction(rate: float | None) -> bool:
        return rate is not None and 0.0 <= rate <= 1.0

    prev_comparable = (
        prev_result is not None
        and prev_result.cohort_size
        and _valid_fraction(prev_result.churn_rate)
    )
    cohort_note = f"코호트 {result.cohort_size}명 중 " if result.cohort_size is not None else ""
    # churn_rate 는 fraction(0.6=60%) — ":.1%" 로만 변환한다(#197 — 구 ":.1f}%" 는
    # 60% 를 "0.6%" 로 왜곡해 워커가 이탈 미미로 오판하던 수치 버그).
    # [#197 리뷰] 결측(None)은 0.0% 로 위장하지 않고 미수신으로 명시한다 — 워커가
    # "이탈 없음"이 아니라 "판정 보류"로 해석하게(silent-mismatch 방어 일관성).
    if result.churn_rate is not None and not _valid_fraction(result.churn_rate):
        rate_note = (
            f"이탈률 값 이상(churnRate={result.churn_rate!r} — fraction [0,1] 밖) "
            "— 이탈 규모 판정 보류"
        )
    elif result.churn_rate is not None and result.cohort_size:
        # [#290] Wilson CI + 직전 기간 z-검정. I-16 은 이탈 '수'가 아니라 fraction 을
        # 주므로 churned = round(rate×cohort) 근사로 표본을 복원한다(결정론 —
        # rate∈[0,1] 가드 통과 후라 churned∈[0,cohort] 가 보장돼 raise 경로가 없다).
        churned = round(result.churn_rate * result.cohort_size)
        estimate = proportions.wilson_interval(
            churned, result.cohort_size, confidence=settings.seller_wilson_confidence
        )
        rate_note = (
            f"이탈률 {result.churn_rate:.1%}"
            f" [{settings.seller_wilson_confidence:.0%} CI"
            f" {estimate.ci_low:.1%}~{estimate.ci_high:.1%}]"
        )
        if prev_comparable:
            prev_churned = round(prev_result.churn_rate * prev_result.cohort_size)
            comparison = proportions.compare_rates(
                churned,
                result.cohort_size,
                prev_churned,
                prev_result.cohort_size,
                alpha=settings.seller_rate_test_alpha,
                confidence=settings.seller_wilson_confidence,
            )
            rate_note += (
                f", 직전 기간({prev_range[0]}~{prev_range[1]},"
                f" 코호트 {prev_result.cohort_size}명) {prev_result.churn_rate:.1%} 대비"
                f" p={comparison.p_value:.3f} — {_VERDICT_LABELS[comparison.verdict]}"
            )
        else:
            rate_note += ", 직전 기간 비교 불가(코호트 0명/조회 실패/결측/값 이상)"
    elif result.churn_rate is not None:
        # cohortSize 결측 — 비율은 표기하되 표본이 없어 CI·검정은 정의 불가.
        rate_note = f"이탈률 {result.churn_rate:.1%} (코호트 규모 미수신 — CI·검정 생략)"
    else:
        rate_note = "이탈률 미수신(churnRate 결측 — 이탈 규모 판정 보류)"
    head = f"{cohort_note}{rate_note} (기준: inactiveDays={effective_days})."
    signals_note = _summarize_churn_signals(
        result,
        prev_result if prev_comparable else None,
        top_k=settings.seller_churn_signal_top_k,
    )
    if result.members:
        # [#197 리뷰] I-16 전용 상한 — I-14 kv 상한(seller_summary_max_events)과 분리.
        shown = result.members[: settings.seller_churn_member_max]
        # [#487] 라벨 결측(BE 미배포 구간)에 memberId 폴백을 두지 않는다 — 이번에 막으려는
        # 원시 회원 키 노출이 조용히 되살아난다(I-8 "404 시 구경로 폴백 금지"와 같은 원칙).
        # [#495] 그 결측을 '?' 가 아니라 '라벨없음' 으로 적는다 — 같은 줄의 마지막 활동·세션도
        # 결측을 '?' 로 쓰기 때문에 "[?]" 가 라벨 미수신인지 개명 미반영(#487 증상)인지
        # 문자열로 구분되지 않았다. 폴백 금지 원칙은 그대로고 표기만 갈라 세운다.
        member_lines = "; ".join(
            f"[{m.customer_label or '라벨없음'}] "
            f"마지막 활동 {m.last_activity_at or '?'}"
            f"·최근30일 세션 {m.sessions_30d if m.sessions_30d is not None else '?'}"
            f"·이탈 전 이벤트 {m.pre_churn_event or '-'}"
            for m in shown
        )
        omitted = len(result.members) - len(shown)
        omitted_note = f" 외 {omitted}명" if omitted > 0 else ""
        # members 는 서버 CHURN_LIST_CAP 절단본일 수 있다 — 표본=전수 오해석 방지
        # 고지(I-14 total_note 와 같은 취지, 전수는 코호트×이탈률로 유추 가능).
        # [#495] 상한값은 Settings 주입이다 — 판매자에게 보이는 문구에 숫자를 박아두면
        # I-16 명세에 없는 BE 구현 실측값이라 BE 가 바꾼 순간 거짓 고지가 된다.
        members_note = (
            f" 이탈 회원 {len(result.members)}명"
            f"(서버 상한 {settings.seller_churn_server_list_cap} 절단본일 수 있음): "
            f"{member_lines}{omitted_note}."
        )
    else:
        members_note = ""
    return (
        f"{head}{signals_note}{members_note} "
        f"{_CHURN_SIGNAL_RULES_NOTE} {_CUSTOMER_LABEL_NOTE} "
        f"{_reference_note(from_date, to_date)}"
    )


# I-8 groupBy 화이트리스트(BE AccountEventAggregateResponse 실측, api-spec §4.4 v0.19.1)
# — 그 외 값은 BE 400 INVALID_GROUP_BY. 도구가 호출 전 선검증한다(#197 리뷰 2).
_ACCOUNT_EVENTS_GROUP_BY = ("eventType", "hour", "ip")


@tool
@_traced_tool("tool.get_account_events")
@_guard_period_args
async def get_account_events(
    runtime: ToolRuntime[SellerContext],
    from_date: str,
    to_date: str,
    event_type: str | None = None,
    group_by: str | None = None,
) -> str:
    """자사 코호트 계정 이벤트 집계를 조회해 요약한다(I-8, api-spec §4.4).

    [개정 2026-08-06, #481] 전역 → 자사 코호트(브랜드 스코프) 전환 — 기간 내 자사
    상품과 상호작용한 회원의 계정 이벤트만 집계된다(I-16 churn 과 동일 조인).
    groupBy=ip 의 suspiciousMemberCount(해당 IP 코호트 회원 중 I-14 어뷰징 기준
    해당 **회원 수** — 코드 판정)가 다계정 정황의 재료다. 개수만 제공되므로
    특정 회원을 지목·추정하지 않는다. 전부 0도 정상 결과다.

    Args:
        from_date: 조회 시작일(YYYY-MM-DD, 필수).
        to_date: 조회 종료일(YYYY-MM-DD, 필수).
        event_type: 이벤트 종류 필터(선택) — SIGNUP/LOGIN_SUCCESS/LOGIN_FAIL/
            LOGOUT/WITHDRAW.
        group_by: eventType(기본, 유형별 합계) | hour(시간대별) | ip(IP별 —
            suspiciousMemberCount·distinctMembers 등). 이 3종 외 값은 오류다.
    """
    # [#481] 브랜드 스코프 전환으로 #197 보류 사유(전역 데이터·admin 소유 협의
    # 미완 🔴)가 해소돼 기본 활성이다 — 플래그는 운영 킬스위치로만 유지한다.
    if not get_settings().seller_account_events_enabled:
        return (
            "Error: 계정 이벤트 집계(I-8)가 설정(seller_account_events_enabled)으로 "
            "비활성 상태입니다."
        )
    # [#197 PR 리뷰 2] groupBy 화이트리스트 선검증 — BE 도 400 INVALID_GROUP_BY 로
    # 거부하지만(api-spec §4.4), LLM 오타·환각 값 때문에 왕복 1회(3s 타임아웃 예산)를
    # 쓰고 나서야 degrade 하는 낭비를 막는다. 문구에 유효값을 실어 재시도를 유도한다.
    if group_by is not None and group_by not in _ACCOUNT_EVENTS_GROUP_BY:
        return (
            f"Error: group_by '{group_by}' 는 지원되지 않습니다 — "
            f"{'/'.join(_ACCOUNT_EVENTS_GROUP_BY)} 중 하나를 사용하세요."
        )
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().get_account_events(
            brand_id, from_date, to_date, event_type, group_by
        )
    except SpringUnavailableError as exc:
        return f"Error: 계정 이벤트 데이터를 불러오지 못했습니다({exc})."
    # [#197] BE 실측 응답(rows — groupBy 별 Bucket/IpRow 이형) 기준으로 요약한다 —
    # 구 events 필드는 Spring 에 없어 항상 "0건 집계됨"으로 새던 버그의 원인.
    applied = result.group_by or group_by or "eventType"
    if not result.rows:
        return (
            f"계정 이벤트 0건(자사 코호트, groupBy={applied}) — 코호트 없음도 정상 결과. "
            f"{_reference_note(from_date, to_date)}"
        )
    # [#290] abuse Collective 트랙 — hour: 심야 활동 비중 / ip: suspiciousMemberCount
    # 내림차순 정렬 + 합계 요약(#481 개정 — 구 failCount·isSuspicious 는 응답에서 제거됨).
    rows = result.rows
    collective_note = ""
    settings = get_settings()
    if applied == "hour":
        share = outliers.night_activity_share(
            rows,
            start=settings.seller_night_hours_start,
            end=settings.seller_night_hours_end,
        )
        collective_note = (
            f" 심야({settings.seller_night_hours_start}~{settings.seller_night_hours_end}시)"
            f" 활동 비중 {share[0]:.1%}({share[1]}/{share[2]}건)."
            if share is not None
            else " 심야 활동 비중 판정 보류(유효 집계 없음)."
        )
    elif applied == "ip":
        # [#481] 2026-08-06 개정 — failCount·isSuspicious 는 응답에서 제거됐다
        # (브랜드 스코프에선 상시 0/false 오보 위험). suspiciousMemberCount(I-14
        # 어뷰징 기준 SQL 교차, 코드 판정) 내림차순으로 다계정 정황을 상단 노출한다.

        def _suspicious_members(row: dict) -> int:
            count = row.get("suspiciousMemberCount", 0)
            return count if isinstance(count, int) else 0

        rows = sorted(rows, key=_suspicious_members, reverse=True)
        total_suspicious = sum(_suspicious_members(row) for row in rows)
        collective_note = (
            f" suspiciousMemberCount 내림차순 정렬 — I-14 어뷰징 기준 교차 회원 합계 "
            f"{total_suspicious}명(코드 판정·개수만 제공, 특정 회원 지목 불가)."
        )
    return (
        f"계정 이벤트 {len(rows)}건(자사 코호트, groupBy={applied}): "
        f"{_summarize_events(rows)}.{collective_note} {_reference_note(from_date, to_date)}"
    )


@tool
@_traced_tool("tool.list_my_products")
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
        status: ON_SALE/HIDDEN 중 하나로 좁힐 때(선택). DELETED 는 BE 가 목록에서
            제외하므로 지정해도 빈 결과다(§4.5).
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
@_traced_tool("tool.get_orders")
@_guard_period_args
async def get_orders(
    runtime: ToolRuntime[SellerContext],
    status: str | None = None,
    order_id: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    """자사 주문의 현재 상태 스냅샷을 조회한다(I-29, api-spec §4.18).

    "신규 주문 뭐 있어?" 같은 현재 상태 질문과 발송 처리 대상(orderItemId) 확인에
    쓴다 — 전이 이력·집계는 get_order_events(I-14) 소관이다(역할 분리). 아이템별
    orderItemId·현재 status 가 함께 반환되므로 발송 처리는 이 조회 결과의
    orderItemId 로 특정한다.

    Args:
        status: 탭 어휘 ORDERED/SHIPPING/DELIVERED/CLAIM 중 하나로 좁힐 때(선택, 생략=전체).
        order_id: 특정 주문 번호 단건 직조회(선택).
        from_date: 조회 시작일 YYYY-MM-DD(선택 — 생략 시 기간 무관 전체 주문).
        to_date: 조회 종료일 YYYY-MM-DD(선택).
        limit: 반환 상한(선택, 서버 기본 20·최대 100).
        offset: 페이지 오프셋(선택).
    """
    brand_id = runtime.context.brand_id
    settings = get_settings()
    try:
        result = await get_spring_client().get_orders(
            brand_id,
            status=status,
            order_id=order_id,
            from_=from_date,
            to=to_date,
            limit=limit,
            offset=offset,
        )
    except SpringUnavailableError as exc:
        return f"Error: 주문 조회에 실패했습니다({exc})."
    if not result.rows:
        # orderId 직조회의 미존재·타사 주문은 200 + 빈 rows(존재 은닉, 확정 2026-08-04)
        # — "해당 주문이 없습니다"로 안내하고 존재 여부를 구분해 말하지 않는다.
        if order_id is not None:
            return f"해당 주문(orderId={order_id})이 없습니다."
        return "조회 조건에 해당하는 주문이 없습니다."
    tab_note = ""
    if result.tab_counts:
        tab_note = (
            " [탭별 건수: " + ", ".join(f"{k} {v}" for k, v in result.tab_counts.items()) + "]"
        )
    shown = result.rows[: settings.seller_summary_max_orders]
    lines = []
    for row in shown:
        claim_note = f" 클레임 {row.claim_status}" if row.claim_status else ""
        items = ", ".join(
            f"[orderItemId={item.order_item_id}] {item.name}"
            + (f"({item.option_name})" if item.option_name else "")
            + f" ×{item.quantity} {item.status}"
            + (f"(클레임 {item.active_claim_status})" if item.active_claim_status else "")
            for item in row.items
        )
        lines.append(
            f"[주문 {row.order_id} | {row.order_no}] {row.status}{claim_note} "
            f"주문일 {row.ordered_at[:10]} 수령인 {row.recipient_name} "
            f"자사금액 {row.my_items_amount:,}원 — 아이템: {items}"
        )
    omitted = len(result.rows) - len(shown)
    omitted_note = f" 외 {omitted}건(offset 으로 이어서 조회 가능)" if omitted > 0 else ""
    period_note = (
        f" {_reference_note(from_date, to_date)}"
        if from_date and to_date
        else " (기준: 전체 기간, 현재 상태 스냅샷)"
    )
    return f"주문 {result.total}건{tab_note}: " + "; ".join(lines) + omitted_note + period_note


# I-31 sort 화이트리스트(api-spec §4.20) — `rating` 은 2026-08-06 협의로 `ratingAsc` 로
# 개명되며 폐기됐다(P-3 의 `rating` 은 높은 순이라 같은 이름·반대 방향을 갈랐다). 그 외
# 값은 BE 400 VALIDATION_ERROR. 도구가 호출 전 선검증한다(#496, _ACCOUNT_EVENTS_GROUP_BY 패턴).
_REVIEW_SORT = ("latest", "ratingAsc")


@tool
@_traced_tool("tool.get_reviews")
@_guard_period_args
async def get_reviews(
    runtime: ToolRuntime[SellerContext],
    from_date: str | None = None,
    to_date: str | None = None,
    product_id: int | None = None,
    rating: str | None = None,
    sort: str | None = None,
    stats: bool = False,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    """자사 상품 리뷰(VISIBLE 만)를 조회한다(I-31, api-spec §4.20).

    기간을 생략하면 최근 7일이 기본 적용된다(서버 규칙). stats=True 면 목록 대신
    집계(총건수·평균·별점 분포·상품별)만 반환한다 — 전반 요약은 집계를 먼저 보고,
    문제 리뷰 원문이 필요할 때 rating 필터로 목록을 조회하는 순서를 권장한다.

    Args:
        from_date: 조회 시작일 YYYY-MM-DD(선택 — 생략 시 최근 7일).
        to_date: 조회 종료일 YYYY-MM-DD(선택).
        product_id: 특정 상품으로 한정(선택, 자사 상품만).
        rating: 별점 필터, 1~5 콤마 나열(선택, 예: "1,2" — 낮은 별점만).
            stats=True 집계에도 동일하게 적용된다 — 총건수·평균·분포·상품별이
            해당 별점만으로 계산된다(I-31 확정, #494).
        sort: latest(기본)/ratingAsc — ratingAsc 는 낮은 별점부터(문제 파악용, 선택).
            ratingDesc 는 존재하지 않는다 — 높은 별점은 rating="4,5" 필터로 얻는다.
        stats: True 면 집계 모드(총건수/평균/분포/상품별) — 목록 대신 통계만.
        limit: 반환 상한(선택, 서버 기본 20·최대 100).
        offset: 페이지 오프셋(선택).
    """
    brand_id = runtime.context.brand_id
    settings = get_settings()
    period_note = (
        _reference_note(from_date, to_date)
        if from_date and to_date
        else "(기준: 최근 7일 기본 적용)"
    )
    if stats:
        # [#494] rating 은 집계에도 적용되는 필터다(I-31) — 안 넘기면 전 별점 합산
        # byProduct 가 돌아오는데 에러도 경고도 없어, 워커가 그것을 '저평점이 몰린
        # 상품'으로 서술한다. 조용히 틀리는 경로라 전달 + 스코프 명시를 함께 건다.
        try:
            agg = await get_spring_client().get_review_stats(
                brand_id, from_=from_date, to=to_date, product_id=product_id, rating=rating
            )
        except SpringUnavailableError as exc:
            return f"Error: 리뷰 집계 조회에 실패했습니다({exc})."
        # 집계가 어느 별점 범위로 계산됐는지 워커에게 명시한다 — get_order_events 의
        # ignored_status_note 와 같은 결(거긴 '무시됨', 여긴 '적용됨' 고지).
        # rating 미지정 시 빈 문자열이라 기존 출력과 바이트 동일하다(회귀 방지).
        rating_scope = f"(별점 {rating} 한정)" if rating else ""
        if agg.total_count == 0:
            # 별점을 걸고 0건인 것은 '리뷰가 없다'와 다르다 — 스코프를 빼면 워커가
            # 리뷰 자체의 부재로 오독한다.
            empty_scope = f"별점 {rating} " if rating else ""
            return f"조회 기간에 {empty_scope}리뷰가 없습니다. {period_note}"
        # averageRating null 은 "평점 0점"이 아니라 "산정 불가"다(I-16 규칙).
        avg_note = (
            f"평균 {agg.average_rating}점" if agg.average_rating is not None else "평균 산정 불가"
        )
        dist_note = ", ".join(
            f"{star}점 {agg.distribution.get(star, 0)}건" for star in ("5", "4", "3", "2", "1")
        )
        by_product = "; ".join(
            f"{p.product_name}(productId={p.product_id}) {p.count}건"
            + (f" 평균 {p.average_rating}점" if p.average_rating is not None else "")
            for p in agg.by_product
        )
        by_product_note = f" 상품별: {by_product}." if by_product else ""
        return (
            f"리뷰 집계{rating_scope}: 총 {agg.total_count}건, {avg_note}. 분포: {dist_note}."
            f"{by_product_note} {period_note}"
        )
    # [#496] sort 화이트리스트 선검증 — BE 도 400 VALIDATION_ERROR 로 거부하지만(api-spec
    # §4.20), 폐기된 구 어휘 `rating` 은 LLM 이 여전히 낼 수 있고 왕복 1회(3s 타임아웃
    # 예산)를 쓰고 나서야 실패한다. stats 모드는 sort 를 서버에 싣지 않아 검증 대상이 아니다.
    if sort is not None and sort not in _REVIEW_SORT:
        return (
            f"Error: sort '{sort}' 는 지원되지 않습니다 — "
            f"{'/'.join(_REVIEW_SORT)} 중 하나를 사용하세요"
            '(높은 별점은 rating="4,5" 필터로 얻습니다).'
        )
    try:
        result = await get_spring_client().get_reviews(
            brand_id,
            from_=from_date,
            to=to_date,
            product_id=product_id,
            rating=rating,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    except SpringUnavailableError as exc:
        return f"Error: 리뷰 조회에 실패했습니다({exc})."
    if not result.rows:
        return f"조회 조건에 해당하는 리뷰가 없습니다. {period_note}"
    shown = result.rows[: settings.seller_summary_max_reviews]
    lines = [
        f"[리뷰 {row.review_id}] {row.product_name}(productId={row.product_id}) "
        f"★{row.rating} {row.created_at[:10]} {row.author_nickname}: {row.content}"
        for row in shown
    ]
    omitted = len(result.rows) - len(shown)
    omitted_note = f" 외 {omitted}건(offset 으로 이어서 조회 가능)" if omitted > 0 else ""
    return f"리뷰 {result.total}건: " + "; ".join(lines) + omitted_note + f" {period_note}"


@tool
@_traced_tool("tool.calculate")
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
@_traced_tool("tool.search_analysis_guide")
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
@_traced_tool("tool.create_product")
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
@_traced_tool("tool.update_product")
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
        status: ON_SALE/HIDDEN 중 하나로 변경(선택). DELETED 는 지정할 수 없다
            — 삭제는 I-12 전용 전이라 BE 가 거부한다(§4.5).
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
    except ProductDeletedNotEditable:
        # 재시도가 무의미한 "안 되는 일" — 장애 문구로 뭉개면 에이전트가 재시도를 권한다.
        return f"Error: 이미 삭제된 상품이라 수정할 수 없습니다(productId={product_id})."
    except SpringUnavailableError as exc:
        return f"Error: 상품 수정에 실패했습니다({exc})."
    return f"수정됨: productId={result.product_id}"


@tool
@_traced_tool("tool.delete_product")
async def delete_product(runtime: ToolRuntime[SellerContext], product_id: int) -> str:
    """상품을 삭제한다(I-12, api-spec §4.5). status=DELETED 전환 — HITL 승인 후 호출.

    물리 삭제는 없지만 숨김(HIDDEN)과 다른 상태다 — 숨김은 판매자 목록에 남아 되돌릴 수
    있고, 삭제는 목록에서도 빠지며 되돌릴 수 없다. 잠시 내릴 목적이면 I-11 로 HIDDEN 을 쓴다.

    Args:
        product_id: 대상 상품 식별자.
    """
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().delete_product(brand_id, product_id)
    except ProductAlreadyDeleted:
        # 멱등 200 이 아니다 — "이미 된 일"과 "방금 한 일"을 구분해야 거짓 성공을 막는다.
        return f"Error: 이미 삭제된 상품입니다(productId={product_id})."
    except SpringUnavailableError as exc:
        return f"Error: 상품 삭제에 실패했습니다({exc})."
    return f"삭제됨: productId={result.product_id} (status={result.status})"


@tool
@_traced_tool("tool.update_order_status")
async def update_order_status(
    runtime: ToolRuntime[SellerContext],
    order_item_id: int,
    to_status: str = "SHIPPING",
    reason: str | None = None,
) -> str:
    """주문 아이템을 발송 처리한다(I-30, api-spec §4.19). HITL 승인 후에만 호출한다.

    MVP 허용 전이는 ORDERED→SHIPPING 하나뿐 — 발송 이후의 취소·역전이는 불가
    (구매자 구제는 반품 경로만). 아이템 단위이며 bulk 가 없어 복수 발송은 반복 호출한다.

    Args:
        order_item_id: 발송 대상 주문 아이템 식별자(get_orders 조회 결과의 orderItemId).
        to_status: 전이 목표 상태 — MVP 유효값은 SHIPPING 뿐.
        reason: 전이 사유(선택) — order_status_logs.reason 에 기록.
    """
    brand_id = runtime.context.brand_id
    try:
        result = await get_spring_client().update_order_item_status(
            brand_id,
            order_item_id,
            OrderItemStatusUpdate(to_status=to_status, reason=reason),
        )
    except OrderAlreadyShipped:
        return f"Error: 이미 발송 처리된 주문 아이템입니다(orderItemId={order_item_id})."
    except OrderInvalidTransition:
        return (
            f"Error: 발송 처리할 수 없는 상태입니다(orderItemId={order_item_id}) — "
            "이미 배송 단계로 넘어갔거나 취소 요청 등 클레임이 걸려 있습니다."
        )
    except OrderItemNotFound:
        return f"Error: 해당 주문 아이템을 찾을 수 없습니다(orderItemId={order_item_id})."
    except SpringUnavailableError as exc:
        return f"Error: 발송 처리에 실패했습니다({exc}). 반영 여부가 확인되지 않았습니다."
    return (
        f"발송 처리됨: orderItemId={result.order_item_id} "
        f"{result.from_status}→{result.to_status} ({result.changed_at})"
    )


# ── 도구 배정 상수 (SPEC-SELLER-001 §4 소비 노드 — 에이전트 팩토리가 그대로 사용) ──

# 분석 워커·general·recommend 용(조회만) — 쓰기 도구가 구조적으로 없다(§3.1).
READ_TOOLS: list[BaseTool] = [
    get_sales_timeseries,
    get_funnel,
    get_behavior_events,
    get_order_events,
    get_orders,
    get_product_change_logs,
    get_churn_cohort,
    get_account_events,
    get_reviews,
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

# [#297] 주문 쓰기(발송, I-30) — HITL 실행 레인용. 상품 쓰기(PRODUCT_TOOLS)와 분리해
# 배정 실수를 타입 수준에서 드러낸다. 어떤 draft 생성 에이전트에도 바인딩하지 않는다
# — 실행은 hitl._execute_draft 코드 경로가 SpringClient 를 직접 호출한다(안전장치 ①).
ORDER_WRITE_TOOLS: list[BaseTool] = [
    update_order_status,
]
