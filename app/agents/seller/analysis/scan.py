"""트리거 판정 순수 함수 7종 — **고정 임계 AND 통계 유의** (이슈 #595, `10-TRIGGER` §3).

`10-TRIGGER` 결정 94 가 정한 이중 조건을 코드로 옮긴다::

    발동 = ( |변화| >= 고정 임계 )  AND  ( 통계적으로 유의 )

어느 한쪽만으로는 발동하지 않는다. 고정 임계 단독이면 주말 매출 변동(평일 대비 20~30%)에
거의 매일 걸려 `01` §8(*"이상 없음을 보고서로 만들지 않는다"*)이 무너지고, 통계 유의
단독이면 표본이 큰 브랜드에서 1% 변화도 유의해져 판매자에게 "왜 오늘 보고서가 왔는지"를
설명할 수 없다. 둘을 곱하면 서로의 약점을 막는다.

[이 모듈이 Settings 를 읽지 않는 이유 — 이중 구현 방지]
`analysis/__init__` 규약대로 튜너블은 전부 `TriggerThresholds` 로 주입받는다. 그래야
null 시뮬레이션(`evals/seller_trigger/`)이 **운영과 같은 함수**를 Spring 응답 위조 없이
부를 수 있다. 이슈 #595 가 이 함수를 스케줄러(#16)보다 먼저 만들라고 정한 이유가 그것이다
— 시뮬레이션이 판정식을 따로 구현하면 "검증한 것"과 "운영이 도는 것"이 갈린다.
Settings→TriggerThresholds 어댑터는 `sop/scan_params.thresholds_from_settings` 하나뿐이다.

[방향성 — 트리거마다 다르다]
매출·전환율은 **양방향**(급락도 급증도 알린다)이고, 상품 판매량·신규 고객·재구매율은
**하락만**, 장바구니 이탈률은 **상승만** 본다(`10-TRIGGER` §3.2 표의 부호를 그대로 옮겼다).
방향을 무시하고 절대값만 보면 "이탈률이 10%p 개선됐다"가 경보로 나간다.

[유의수준 창 보정 — null 시뮬레이션이 강제한 설계 변경]
비율·카운트 검정의 유의수준은 ``rate_alpha / lookback_days`` 다. 이유는 실측이다.

매출 트리거는 GESD 가 **28일 창 전체에 FWER 를 통제**하므로 하루 단위 오탐이 α/창
수준으로 눌린다(합성 null 800일에서 0회). 반면 비율 검정은 매일 새 검정을 α 로 돌려
발동률이 정확히 α 에 고정됐다 — 티어1 열림률 실측 5.0~6.2%(`12-EVAL` §6.1 판정표의
🔴 구간). 그리고 고정 임계를 올려도 듣지 않았다: 소규모 브랜드는 임계 0.40 에서도
4.56% 그대로다. **표본이 작으면 "통계적으로 유의한 날"이 곧 "상대변화가 큰 날"이라
두 조건이 거의 같은 사건이 되고 AND 가 α 로 퇴화한다.** `12-EVAL` §6.1 이 처방한
"1~5%면 고정 임계 상향"은 소·중 브랜드에서 무효다.

그래서 GESD 가 이미 창 안에서 하는 것과 **같은 종류의 보정**을 비율 검정에도 준다.
새 Settings 키를 만들지 않는다(신설 금지 규약 유지) — 기존 ``seller_rate_test_alpha``
를 기존 ``seller_analysis_lookback_days`` 로 나눌 뿐이다. 보정 후 실측 열림률 0%.

[매출 트리거가 S-H-ESD 를 쓰지 않는 이유 — null 시뮬레이션 실측 2건]
① **STL 창의 마지막 날은 구조적으로 안 걸린다.** 경계에서 LOESS 추세가 마지막 점을
그대로 따라가 잔차가 0 에 수렴한다(실측: 원값 716,235 대 STL 기대 717,271, robust
σ=0.01). 168 창 중 0 회. 매출을 −60% 떨어뜨려도 미검출이고, 같은 이상을 창 중간에
두면 σ=12.28 로 잡힌다. **무인 스캔은 항상 "어제"를 판정하므로 항상 이 경계에 선다.**
② `detect_seasonal_anomalies` 는 순수 null 에서 28일 창당 평균 4.36건(15.6%)을 이상으로
뱉는다 — 28점/period=7 이면 계절 성분이 위상당 4점으로 추정돼 과적합되고 잔차 MAD 가
붕괴한다. 창을 90일로 늘려도 7.8% 다. ①을 고치면 ② 때문에 발동률이 ~12% 로 뛴다.

그래서 무인 스캔의 매출 판정은 **주문 건수 포아송 검정(직전 7일 대 그 이전 7일)**이다.
7일 합끼리 비교하면 요일 효과가 정의상 상쇄돼 STL 이 하던 일이 필요 없어지고, 카운트
데이터라 정확검정을 쓸 수 있다. 실측 null 발동률 0%, 7일 지속 −30% 하락 검출(p=6.8e-05).
**대가는 하루짜리 급락을 못 잡는 것이다** — 주간 합에서 −42% 하루는 −5.8% 라 묻힌다.
그 검출은 후속 이슈로 분리했고, 골든셋이 현재 미검출임을 xfail 로 고정한다.
S-H-ESD 는 대화형 `sales_anomaly` 워커(기간을 판매자가 지정)에 그대로 남는다 — 거기서는
대상이 창 경계가 아니다.

[판정 보류는 이상 없음이 아니다]
표본 부족·기준 구간 결측·기준값 0·계약 불일치는 전부 `decided=False` 다. `fired=False`
하나만 보고 넘어가면 "못 봤다"가 "봤는데 정상이다"로 둔갑한다(#512 규약 승계).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.agents.seller.analysis import proportions
from app.agents.seller.analysis.types import RateComparison, ScanResult, TriggerEvaluation

# 트리거 식별자 — `10-TRIGGER` §3.2 표의 7종. 보고서·로그·리포트가 공유하는 어휘라
# 문자열 리터럴을 흩뿌리지 않는다.
TRIGGER_SALES = "sales"
TRIGGER_CONVERSION = "conversion"
TRIGGER_ABUSE = "abuse"
TRIGGER_PRODUCT_SALES = "product_sales"
TRIGGER_CART_ABANDON = "cart_abandon"
TRIGGER_NEW_CUSTOMER = "new_customer"
TRIGGER_REPURCHASE = "repurchase"

# 티어1 = 문을 여는 3종(`10-TRIGGER` §4.2). 매출·전환율은 다른 모든 것의 하류라 상품이
# 안 팔리거나 신규가 줄면 결국 둘 중 하나에 나타난다. 어뷰징만 예외로 얹는다 —
# 어뷰징은 매출을 **올리기** 때문에 매출·전환율에 안 나타날 수 있다.
TIER1_TRIGGERS = (TRIGGER_SALES, TRIGGER_CONVERSION, TRIGGER_ABUSE)
TIER2_TRIGGERS = (
    TRIGGER_PRODUCT_SALES,
    TRIGGER_CART_ABANDON,
    TRIGGER_NEW_CUSTOMER,
    TRIGGER_REPURCHASE,
)

_METHOD_Z = "two_proportion_z"
_METHOD_POISSON = "poisson_rate"
_METHOD_BE_FLAG = "be_flag"

# 재구매율 정의 각인(`10-TRIGGER` 결정 101) — I-14 groupBy=memberId 의 orderCount 는
# "상태 전이가 있었던 distinct 주문"(취소 포함)이다. I-38 의 동명 필드는 PAID only 라
# 뜻이 다르므로, I-38 이 붙은 뒤 값이 달라져도 버그가 아니라는 근거를 남긴다.
BASIS_I14_TRANSITION = "i14_transition"

_UNIT_PCT = "pct"
_UNIT_PP = "pp"
_UNIT_COUNT = "count"


@dataclass(frozen=True)
class TriggerThresholds:
    """트리거 판정 튜너블 묶음 — `sop/scan_params.thresholds_from_settings` 가 만든다.

    고정 임계 6종은 신설 Settings(`seller_trigger_*`)이고, 유의수준·구간은 **기존 키
    재사용**이다(`seller_rate_test_alpha`·`seller_analysis_lookback_days`) — 새 alpha 를
    만들면 골든셋이 운영과 다른 값으로 재게 된다(`12-EVAL` §8).

    STL 계열 튜너블(`seller_stl_period`·`seller_gesd_*`·`seller_min_history_for_stl`)은
    여기 없다 — 무인 스캔이 S-H-ESD 를 쓰지 않기 때문이다(모듈 docstring). 그 키들은
    대화형 `sop/compute/sales_anomaly.py` 가 계속 쓴다.
    """

    sales_pct: float
    conversion_pct: float
    product_drop_pct: float
    cart_abandon_pp: float
    new_customer_drop_pct: float
    repurchase_drop_pp: float
    baseline_days: int
    lookback_days: int
    rate_alpha: float
    wilson_confidence: float
    abuse_min_members: int = 1

    @property
    def effective_rate_alpha(self) -> float:
        """비율·카운트 검정에 실제로 쓰는 유의수준 — ``rate_alpha / lookback_days``.

        모듈 docstring 의 "유의수준 창 보정" 참조. 이 값을 `sop/compute/*` 가 쓰는
        ``seller_rate_test_alpha`` 원값과 혼동하면 안 된다 — 저쪽은 판매자가 물어본
        기간을 한 번 검정하는 대화형 경로라 매일 반복되지 않는다.
        """
        return self.rate_alpha / self.lookback_days


@dataclass(frozen=True)
class Tier1Inputs:
    """티어1 3콜 분량의 입력 — I-6 시계열 · I-7 퍼널 2기간 · I-14 stats.

    Spring 응답 객체가 아니라 **원시 수치**를 받는다. 스키마를 받으면 시뮬레이션이
    응답을 위조해야 하고, 그 위조본이 실제 BE 와 어긋나도 아무도 모른다.

    **매출과 전환율은 같은 창 규약을 쓴다** — 직전 ``baseline_days`` 일(대상일 포함) 대
    그 이전 ``baseline_days`` 일. 하루치 표본으로는 어느 쪽도 검정력이 안 나온다는 것을
    null 시뮬레이션에서 실측했다(중브랜드 전환율 −30% 하락이 1일 창에서 p=0.059 로 α 조차
    못 넘고, 매출은 STL 창 경계라 −60% 도 미검출). 결정 97 의 "분석 기간 = 전날 1일"에
    대한 예외이고, 근거는 `evals/seller_trigger/reports/` 의 발동률 리포트다.

    ``sales_dates``/``sales_values``/``sales_order_counts`` 는 대상일 이전 이력을 포함한
    시계열이고(길이가 같아야 한다), ``current_*``/``baseline_*`` 퍼널 카운트는 이미
    구간 합으로 넘어온다.
    """

    sales_dates: Sequence[str]
    sales_values: Sequence[float]
    sales_order_counts: Sequence[int]
    target_date: str
    current_view: int
    current_purchase: int
    baseline_view: int
    baseline_purchase: int
    uncomputable_stages: Sequence[str] = ()
    suspicious_current: int = 0
    suspicious_baseline: int = 0


@dataclass(frozen=True)
class Tier2Inputs:
    """티어2 5콜 분량의 입력 — I-13 product/eventType · I-8 SIGNUP · I-14 memberId.

    ``product_*`` 는 ``{product_id: (판매수량, 조회수)}`` 다 — I-13 의 ``salesQuantity``
    와 ``counts.productView``. ``counts.purchaseComplete`` 를 쓰지 않는 이유는 그것이
    **주문 건수**라 수량과 단위가 다르고 취소·반품 처리 규칙까지 다르기 때문이다(#489).

    ``cart_boundary_blocked`` 는 비교 구간이 ``remove_from_cart`` 편입일(2026-08-06 결정 /
    08-07 구현)을 가로지르는가다. **날짜 판정 자체는 호출부 소관**이고 이 모듈은 결과만
    받는다 — 순수 함수가 달력을 알면 테스트가 시간에 묶인다.
    """

    product_current: Mapping[int, tuple[int, int]] = field(default_factory=dict)
    product_baseline: Mapping[int, tuple[int, int]] = field(default_factory=dict)
    current_removes: int = 0
    current_adds: int = 0
    baseline_removes: int = 0
    baseline_adds: int = 0
    cart_boundary_blocked: bool = False
    current_signups: int = 0
    baseline_signups: int = 0
    current_repeat_members: int = 0
    current_members: int = 0
    baseline_repeat_members: int = 0
    baseline_members: int = 0


def _hold(
    trigger: str,
    tier: int,
    threshold: float,
    unit: str,
    method: str,
    reason: str,
    *,
    threshold_met: bool | None = None,
    change: float | None = None,
    detail: dict[str, float] | None = None,
    basis: str = "",
) -> TriggerEvaluation:
    """판정 보류 1건 — ``decided=False``. **"이상 없음"이 아니다.**"""
    return TriggerEvaluation(
        trigger=trigger,
        tier=tier,
        fired=False,
        decided=False,
        threshold_met=threshold_met,
        significant=None,
        change=change,
        change_unit=unit,
        threshold=threshold,
        method=method,
        basis=basis,
        detail=detail or {},
        hold_reason=reason,
    )


def _rate_comparison(
    current_successes: int,
    current_trials: int,
    baseline_successes: int,
    baseline_trials: int,
    *,
    thresholds: TriggerThresholds,
) -> RateComparison | str:
    """compare_rates 를 돌리되 계약 불일치는 보류 사유 문자열로 돌려준다.

    I-13/I-7 은 이벤트 카운트라 ``cart > view`` 같은 역전이 실데이터에서 나온다
    (`sop/compute/conversion.py` 가 이미 정한 규약) — clamp 로 정상 CI 처럼 위장하지
    않고 그 판정만 보류한다.
    """
    if current_trials <= 0 or baseline_trials <= 0:
        return f"no_trials: 표본이 없다(current={current_trials}, baseline={baseline_trials})"
    try:
        return proportions.compare_rates(
            current_successes,
            current_trials,
            baseline_successes,
            baseline_trials,
            alpha=thresholds.effective_rate_alpha,
            confidence=thresholds.wilson_confidence,
        )
    except ValueError as exc:
        return f"inconsistent_counts: 카운트 정합 이상으로 검정 불가 — {exc}"


def evaluate_sales_trigger(
    dates: Sequence[str],
    sales_values: Sequence[float],
    order_counts: Sequence[int],
    *,
    target_date: str,
    thresholds: TriggerThresholds,
) -> TriggerEvaluation:
    """트리거 1 — 매출 변화 ±5% AND 주문 건수 포아송 검정(양방향).

    창은 **직전 baseline_days일(대상일 포함) 대 그 이전 baseline_days일**이다.

    - 고정 임계(표면 문구): 두 구간 **매출 금액 합**의 상대 변화율. 보고서 1부가
      *"매출이 전주 대비 8.2% 감소했습니다"* 로 쓰는 값이 이것이다(`10-TRIGGER` §3.1 —
      *"표면 문구는 고정 임계로 쓴다"*).
    - 통계 판정: 같은 두 구간의 **주문 건수 합**을 포아송 비율로 검정한다. 금액을 검정하지
      않는 이유는 그것이 단가 분포에 좌우되는 연속량이라 분포 가정이 서지 않는 반면,
      건수는 카운트라 정확검정이 성립하기 때문이다. I-6 이 ``orderCount`` 를 함께 준다.

    두 축이 다른 것은 의도다 — 표면 수치(금액)와 발동을 정하는 검정(건수)을 분리해 두면,
    단가가 흔들려 금액만 움직인 날에 검정이 제동을 건다.

    S-H-ESD 를 쓰지 않는 근거는 모듈 docstring(창 경계 무검출 · 과다 발화 실측) 참조.
    """
    if not (len(dates) == len(sales_values) == len(order_counts)):
        raise ValueError(
            f"dates({len(dates)})/sales({len(sales_values)})/orders({len(order_counts)})"
            " 길이가 다르다"
        )
    threshold = thresholds.sales_pct
    dates = list(dates)
    sales = [float(value) for value in sales_values]
    orders = [int(value) for value in order_counts]
    span = thresholds.baseline_days

    if target_date not in dates:
        return _hold(
            TRIGGER_SALES,
            1,
            threshold,
            _UNIT_PCT,
            _METHOD_POISSON,
            f"target_missing: 분석 대상일 {target_date} 이 시계열에 없다",
        )
    index = dates.index(target_date)
    if index - 2 * span + 1 < 0:
        return _hold(
            TRIGGER_SALES,
            1,
            threshold,
            _UNIT_PCT,
            _METHOD_POISSON,
            (
                f"no_baseline: 비교에 {2 * span}일 이력이 필요한데 {index + 1}일뿐이라"
                " 판정을 보류한다"
            ),
            detail={"available_days": float(index + 1), "required_days": float(2 * span)},
        )

    current = slice(index - span + 1, index + 1)
    baseline = slice(index - 2 * span + 1, index - span + 1)
    current_sales = sum(sales[current])
    baseline_sales = sum(sales[baseline])
    current_orders = int(sum(orders[current]))
    baseline_orders = int(sum(orders[baseline]))

    if baseline_sales <= 0 and current_sales <= 0:
        return _hold(
            TRIGGER_SALES,
            1,
            threshold,
            _UNIT_PCT,
            _METHOD_POISSON,
            "no_sales: 두 구간 모두 매출이 없어 변화가 정의되지 않는다",
        )
    if baseline_sales <= 0:
        # 무매출 이력 직후 매출 발생 — 상대 변화율은 정의 불가(무한대)라 change 를 채우지
        # 않는다(0% 로 위장 금지, #194 계승). 다만 무한대는 어떤 임계도 넘으므로 임계는
        # 통과다. `timeseries` 가 이 케이스를 sigma=inf 로 다루던 것과 같은 사건이다.
        change: float | None = None
        threshold_met = True
        direction = "spike"
    else:
        change = (current_sales - baseline_sales) / baseline_sales
        threshold_met = abs(change) >= threshold
        direction = "drop" if change < 0 else "spike"

    detail = {
        "current_sales": current_sales,
        "baseline_sales": baseline_sales,
        "current_orders": float(current_orders),
        "baseline_orders": float(baseline_orders),
        "window_days": float(span),
    }
    try:
        comparison = proportions.compare_counts(
            current_orders,
            baseline_orders,
            span,
            alpha=thresholds.effective_rate_alpha,
            current_days=span,
        )
    except ValueError as exc:
        return _hold(
            TRIGGER_SALES,
            1,
            threshold,
            _UNIT_PCT,
            _METHOD_POISSON,
            f"no_orders: {exc}",
            threshold_met=threshold_met,
            change=change,
            detail=detail,
        )

    significant = comparison.verdict != "no_significant_change"
    detail["expected_orders"] = comparison.expected
    if comparison.rate_ratio is not None:
        detail["order_rate_ratio"] = comparison.rate_ratio
    return TriggerEvaluation(
        trigger=TRIGGER_SALES,
        tier=1,
        fired=threshold_met and significant,
        decided=True,
        threshold_met=threshold_met,
        significant=significant,
        change=change,
        change_unit=_UNIT_PCT,
        threshold=threshold,
        method=_METHOD_POISSON,
        direction=direction,
        p_value=comparison.p_value,
        alpha=comparison.alpha,
        basis="i6_order_count",
        detail=detail,
    )


def evaluate_conversion_trigger(
    *,
    current_view: int,
    current_purchase: int,
    baseline_view: int,
    baseline_purchase: int,
    thresholds: TriggerThresholds,
    uncomputable_stages: Sequence[str] = (),
) -> TriggerEvaluation:
    """트리거 2 — 전환율 변화 ±10% AND two-proportion z(양방향).

    창은 **직전 baseline_days일 vs 그 이전 baseline_days일**이다(`Tier1Inputs` 주석).
    매출과 달리 하루치로는 검정력이 없다 — 그 실측이 이 창 확대의 근거다.

    기준 지표는 **overall(view→purchase) 단일**이다(사용자 확정). 3단계 각각을 재면
    검정이 3회라 다중비교로 1종 오류가 α 를 넘고, 보고서 1부의 표면 문구
    (*"전환율이 전주 대비 X% 하락"*)가 어느 단계를 말하는지 모호해진다. 단계 분해는
    티어2 서술 재료(`sop/compute/conversion.py`)가 이미 담당한다.
    """
    threshold = thresholds.conversion_pct
    blocked = {"view", "purchase"} & set(uncomputable_stages)
    if blocked:
        # count=null·computable=false 는 "0건"이 아니다(`02` §4).
        return _hold(
            TRIGGER_CONVERSION,
            1,
            threshold,
            _UNIT_PCT,
            _METHOD_Z,
            f"uncomputable_stage: {sorted(blocked)} 미집계 — 판정 보류",
        )

    comparison = _rate_comparison(
        current_purchase, current_view, baseline_purchase, baseline_view, thresholds=thresholds
    )
    if isinstance(comparison, str):
        return _hold(TRIGGER_CONVERSION, 1, threshold, _UNIT_PCT, _METHOD_Z, comparison)
    if comparison.baseline.rate <= 0:
        return _hold(
            TRIGGER_CONVERSION,
            1,
            threshold,
            _UNIT_PCT,
            _METHOD_Z,
            "baseline_zero: 기준 전환율이 0 이라 상대 변화율이 정의되지 않는다",
            detail={"current_rate": comparison.current.rate},
        )

    change = (comparison.current.rate - comparison.baseline.rate) / comparison.baseline.rate
    threshold_met = abs(change) >= threshold
    significant = comparison.verdict != "no_significant_change"
    return TriggerEvaluation(
        trigger=TRIGGER_CONVERSION,
        tier=1,
        fired=threshold_met and significant,
        decided=True,
        threshold_met=threshold_met,
        significant=significant,
        change=change,
        change_unit=_UNIT_PCT,
        threshold=threshold,
        method=_METHOD_Z,
        direction=("drop" if change < 0 else "spike"),
        p_value=comparison.p_value,
        alpha=comparison.alpha,
        detail={
            "current_rate": comparison.current.rate,
            "baseline_rate": comparison.baseline.rate,
            "ci_low": comparison.current.ci_low,
            "ci_high": comparison.current.ci_high,
            "current_trials": float(comparison.current.trials),
            "baseline_trials": float(comparison.baseline.trials),
        },
    )


def evaluate_abuse_trigger(
    *, suspicious_current: int, suspicious_baseline: int, thresholds: TriggerThresholds
) -> TriggerEvaluation:
    """트리거 7 — 이상 주문 패턴. **BE `isSuspicious` 를 그대로 쓴다**(결정 103).

    ⚠️ 이 트리거만 AND 예외다. 임계식이 우리 것이 아니라 BE 소유(`cancelRatio > 0.5`
    또는 `maxOrdersPerHour > 10`)이고, 우리가 재계산·번복하지 않기로 정했으므로 통계
    검정을 붙일 대상이 없다 — ``significant=None`` 이 그 사실의 표기다. BE 가 임계를
    바꿔도 우리는 통지받지 못한다는 한계(감사 C-6)가 그대로 남는다.

    티어1 에 얹는 이유: 어뷰징은 매출을 **올리므로** 트리거 1·2 에 안 나타날 수 있다.
    """
    fired = (
        suspicious_current >= thresholds.abuse_min_members
        or suspicious_current > suspicious_baseline
    )
    return TriggerEvaluation(
        trigger=TRIGGER_ABUSE,
        tier=1,
        fired=fired,
        decided=True,
        threshold_met=fired,
        significant=None,
        change=float(suspicious_current - suspicious_baseline),
        change_unit=_UNIT_COUNT,
        threshold=float(thresholds.abuse_min_members),
        method=_METHOD_BE_FLAG,
        basis="be_is_suspicious",
        detail={
            "suspicious_current": float(suspicious_current),
            "suspicious_baseline": float(suspicious_baseline),
        },
    )


def evaluate_product_sales_rows(
    current: Mapping[int, tuple[int, int]],
    baseline: Mapping[int, tuple[int, int]],
    *,
    thresholds: TriggerThresholds,
) -> list[TriggerEvaluation]:
    """트리거 3 상품별 판정 목록 — 수량/조회 비, **하락 방향만**.

    ``detail["product_id"]`` 로 대상을 싣는다(`detail` 이 float 사전이라 id 를 여기
    담는다 — `sop/compute/sales_anomaly.py` 가 날짜를 key 에 실은 것과 같은 제약).
    양쪽 기간에 모두 등장하는 상품만 본다 — 신규/단종 상품의 등장·소멸을 급감으로
    읽으면 안 된다. 반환 순서는 product_id 오름차순(결정론).
    """
    threshold = thresholds.product_drop_pct
    results: list[TriggerEvaluation] = []
    for product_id in sorted(set(current) & set(baseline)):
        cur_qty, cur_views = current[product_id]
        base_qty, base_views = baseline[product_id]
        pid = float(product_id)
        comparison = _rate_comparison(
            cur_qty, cur_views, base_qty, base_views, thresholds=thresholds
        )
        if isinstance(comparison, str):
            results.append(
                _hold(
                    TRIGGER_PRODUCT_SALES,
                    2,
                    threshold,
                    _UNIT_PCT,
                    _METHOD_Z,
                    comparison,
                    detail={"product_id": pid},
                )
            )
            continue
        if comparison.baseline.rate <= 0:
            results.append(
                _hold(
                    TRIGGER_PRODUCT_SALES,
                    2,
                    threshold,
                    _UNIT_PCT,
                    _METHOD_Z,
                    "baseline_zero: 기준 판매 전환이 0 이라 변화율이 정의되지 않는다",
                    detail={"product_id": pid},
                )
            )
            continue
        change = (comparison.current.rate - comparison.baseline.rate) / comparison.baseline.rate
        threshold_met = change <= -threshold
        significant = comparison.verdict == "significant_drop"
        results.append(
            TriggerEvaluation(
                trigger=TRIGGER_PRODUCT_SALES,
                tier=2,
                fired=threshold_met and significant,
                decided=True,
                threshold_met=threshold_met,
                significant=significant,
                change=change,
                change_unit=_UNIT_PCT,
                threshold=threshold,
                method=_METHOD_Z,
                direction="drop",
                p_value=comparison.p_value,
                alpha=comparison.alpha,
                detail={
                    "product_id": pid,
                    "current_rate": comparison.current.rate,
                    "baseline_rate": comparison.baseline.rate,
                    "current_trials": float(comparison.current.trials),
                    "baseline_trials": float(comparison.baseline.trials),
                },
            )
        )
    return results


def evaluate_product_sales_trigger(
    current: Mapping[int, tuple[int, int]],
    baseline: Mapping[int, tuple[int, int]],
    *,
    thresholds: TriggerThresholds,
) -> TriggerEvaluation:
    """트리거 3 브랜드 단위 집계 — 급감 상품이 1건 이상이면 발동.

    ⚠️ 상품 수만큼 검정을 돌리고 α 를 보정하지 않는다. 티어2 는 발동 조건이 아니라
    서술 재료라 게이트를 흐리지 않지만, 보고서에 오탐 상품이 섞일 수 있다 — null
    시뮬레이션 리포트의 ``product.falsePositivePerDay`` 가 그 크기를 드러낸다.
    """
    rows = evaluate_product_sales_rows(current, baseline, thresholds=thresholds)
    decided = [row for row in rows if row.decided]
    fired_rows = [row for row in decided if row.fired]
    threshold = thresholds.product_drop_pct
    if not decided:
        return _hold(
            TRIGGER_PRODUCT_SALES,
            2,
            threshold,
            _UNIT_PCT,
            _METHOD_Z,
            "no_decidable_product: 양 기간에 함께 등장하며 판정 가능한 상품이 없다",
            detail={"product_count": float(len(rows))},
        )
    worst = min(
        fired_rows or decided, key=lambda row: row.change if row.change is not None else 0.0
    )
    return TriggerEvaluation(
        trigger=TRIGGER_PRODUCT_SALES,
        tier=2,
        fired=bool(fired_rows),
        decided=True,
        threshold_met=any(row.threshold_met for row in decided),
        significant=any(row.significant for row in decided),
        change=worst.change,
        change_unit=_UNIT_PCT,
        threshold=threshold,
        method=_METHOD_Z,
        direction="drop",
        p_value=worst.p_value,
        alpha=thresholds.effective_rate_alpha,
        detail={
            "product_count": float(len(rows)),
            "decided_count": float(len(decided)),
            "fired_count": float(len(fired_rows)),
            "worst_product_id": worst.detail.get("product_id", -1.0),
        },
    )


def evaluate_cart_abandon_trigger(
    *,
    current_removes: int,
    current_adds: int,
    baseline_removes: int,
    baseline_adds: int,
    thresholds: TriggerThresholds,
    boundary_blocked: bool = False,
) -> TriggerEvaluation:
    """트리거 4 — 장바구니 이탈률 +10%p AND two-proportion z, **상승 방향만**.

    이탈률 = ``removeFromCart / addToCart``(I-13 groupBy=eventType). 임계 단위가
    **퍼센트포인트**다 — 상대 변화율로 재면 이탈률 2%→3% 가 +50% 로 부풀어 저이탈
    브랜드에서 상시 발동한다.

    ⚠️ ``remove_from_cart`` 는 2026-08-06 계약 개정(구현 08-07)으로 편입된 이벤트다.
    그 날짜를 가로지르는 비교는 "삭제가 늘었다"가 아니라 "삭제를 이제 센다"라서
    **전면 보류**한다(`06` §4.0.2 V1 의 비교 경계 규약이 이 트리거에도 적용된다).
    """
    threshold = thresholds.cart_abandon_pp
    if boundary_blocked:
        return _hold(
            TRIGGER_CART_ABANDON,
            2,
            threshold,
            _UNIT_PP,
            _METHOD_Z,
            (
                "remove_from_cart_boundary: 비교 구간이 remove_from_cart 편입일을"
                " 가로질러 이탈률 비교가 성립하지 않는다"
            ),
        )
    comparison = _rate_comparison(
        current_removes, current_adds, baseline_removes, baseline_adds, thresholds=thresholds
    )
    if isinstance(comparison, str):
        return _hold(TRIGGER_CART_ABANDON, 2, threshold, _UNIT_PP, _METHOD_Z, comparison)

    change = comparison.current.rate - comparison.baseline.rate  # 퍼센트포인트
    threshold_met = change >= threshold
    significant = comparison.verdict == "significant_rise"
    return TriggerEvaluation(
        trigger=TRIGGER_CART_ABANDON,
        tier=2,
        fired=threshold_met and significant,
        decided=True,
        threshold_met=threshold_met,
        significant=significant,
        change=change,
        change_unit=_UNIT_PP,
        threshold=threshold,
        method=_METHOD_Z,
        direction="spike",
        p_value=comparison.p_value,
        alpha=comparison.alpha,
        detail={
            "current_rate": comparison.current.rate,
            "baseline_rate": comparison.baseline.rate,
            "current_trials": float(comparison.current.trials),
            "baseline_trials": float(comparison.baseline.trials),
        },
    )


def evaluate_new_customer_trigger(
    *, current_signups: int, baseline_signups: int, thresholds: TriggerThresholds
) -> TriggerEvaluation:
    """트리거 5 — 신규 고객 −30% AND **포아송 비율 검정**, 하락 방향만.

    원천은 I-8 브랜드 스코프 ``groupBy=eventType`` 의 ``SIGNUP`` 카운트다(결정 102 —
    I-38 ``firstSeenDaysAgo`` 가 정확하지만 미구현이고, 스캔이 스냅샷에 종속되면 안 된다).

    2-proportion z 를 쓰지 않는 이유는 `proportions.compare_counts` docstring 참조 —
    **분모가 계약에 없다.** 기대값은 기준 구간의 일평균이다.
    """
    threshold = thresholds.new_customer_drop_pct
    try:
        comparison = proportions.compare_counts(
            current_signups,
            baseline_signups,
            thresholds.baseline_days,
            alpha=thresholds.effective_rate_alpha,
        )
    except ValueError as exc:
        return _hold(
            TRIGGER_NEW_CUSTOMER,
            2,
            threshold,
            _UNIT_PCT,
            _METHOD_POISSON,
            f"no_baseline_signup: {exc}",
            detail={"current_signups": float(current_signups)},
        )

    if comparison.expected <= 0:
        # 기준 구간 가입이 0 이면 "감소"가 정의되지 않는다 — 증가는 검정이 잡지만 이
        # 트리거는 하락 전용이라 판정할 것이 없다.
        return _hold(
            TRIGGER_NEW_CUSTOMER,
            2,
            threshold,
            _UNIT_PCT,
            _METHOD_POISSON,
            "no_baseline_signup: 기준 구간 가입이 0 이라 감소율이 정의되지 않는다",
            detail={"current_signups": float(current_signups)},
        )
    change = (current_signups - comparison.expected) / comparison.expected
    threshold_met = change <= -threshold
    significant = comparison.verdict == "significant_drop"
    return TriggerEvaluation(
        trigger=TRIGGER_NEW_CUSTOMER,
        tier=2,
        fired=threshold_met and significant,
        decided=True,
        threshold_met=threshold_met,
        significant=significant,
        change=change,
        change_unit=_UNIT_PCT,
        threshold=threshold,
        method=_METHOD_POISSON,
        direction="drop",
        p_value=comparison.p_value,
        alpha=comparison.alpha,
        basis="i8_signup",
        detail={
            "current_signups": float(current_signups),
            "expected": comparison.expected,
            "rate_ratio": comparison.rate_ratio if comparison.rate_ratio is not None else 0.0,
            "baseline_total": float(comparison.baseline_total),
        },
    )


def evaluate_repurchase_trigger(
    *,
    current_repeat_members: int,
    current_members: int,
    baseline_repeat_members: int,
    baseline_members: int,
    thresholds: TriggerThresholds,
) -> TriggerEvaluation:
    """트리거 6 — 재구매율 −10%p AND two-proportion z, 하락 방향만.

    재구매율 = ``orderCount >= 2 인 회원 수 / 전체 회원 수``, **I-14 groupBy=memberId
    기준으로 고정**한다(결정 101). I-38 의 ``orderCount`` 와 혼용 금지 — 저쪽은 PAID
    only 라 정의가 다르다. 그 사실을 ``basis`` 에 각인해, I-38 이 붙은 뒤 값이 달라져도
    버그가 아니라는 근거를 남긴다.
    """
    threshold = thresholds.repurchase_drop_pp
    comparison = _rate_comparison(
        current_repeat_members,
        current_members,
        baseline_repeat_members,
        baseline_members,
        thresholds=thresholds,
    )
    if isinstance(comparison, str):
        return _hold(
            TRIGGER_REPURCHASE,
            2,
            threshold,
            _UNIT_PP,
            _METHOD_Z,
            comparison,
            basis=BASIS_I14_TRANSITION,
        )

    change = comparison.current.rate - comparison.baseline.rate  # 퍼센트포인트
    threshold_met = change <= -threshold
    significant = comparison.verdict == "significant_drop"
    return TriggerEvaluation(
        trigger=TRIGGER_REPURCHASE,
        tier=2,
        fired=threshold_met and significant,
        decided=True,
        threshold_met=threshold_met,
        significant=significant,
        change=change,
        change_unit=_UNIT_PP,
        threshold=threshold,
        method=_METHOD_Z,
        direction="drop",
        p_value=comparison.p_value,
        alpha=comparison.alpha,
        basis=BASIS_I14_TRANSITION,
        detail={
            "current_rate": comparison.current.rate,
            "baseline_rate": comparison.baseline.rate,
            "current_trials": float(comparison.current.trials),
            "baseline_trials": float(comparison.baseline.trials),
        },
    )


def scan_tier1(inputs: Tier1Inputs, *, thresholds: TriggerThresholds) -> list[TriggerEvaluation]:
    """티어1 3종 — 매출 · 전환율 · 이상 주문. 순서 고정(결정론)."""
    return [
        evaluate_sales_trigger(
            inputs.sales_dates,
            inputs.sales_values,
            inputs.sales_order_counts,
            target_date=inputs.target_date,
            thresholds=thresholds,
        ),
        evaluate_conversion_trigger(
            current_view=inputs.current_view,
            current_purchase=inputs.current_purchase,
            baseline_view=inputs.baseline_view,
            baseline_purchase=inputs.baseline_purchase,
            thresholds=thresholds,
            uncomputable_stages=inputs.uncomputable_stages,
        ),
        evaluate_abuse_trigger(
            suspicious_current=inputs.suspicious_current,
            suspicious_baseline=inputs.suspicious_baseline,
            thresholds=thresholds,
        ),
    ]


def scan_tier2(inputs: Tier2Inputs, *, thresholds: TriggerThresholds) -> list[TriggerEvaluation]:
    """티어2 4종 — 상품 판매량 · 장바구니 이탈률 · 신규 고객 · 재구매율. 순서 고정."""
    return [
        evaluate_product_sales_trigger(
            inputs.product_current, inputs.product_baseline, thresholds=thresholds
        ),
        evaluate_cart_abandon_trigger(
            current_removes=inputs.current_removes,
            current_adds=inputs.current_adds,
            baseline_removes=inputs.baseline_removes,
            baseline_adds=inputs.baseline_adds,
            thresholds=thresholds,
            boundary_blocked=inputs.cart_boundary_blocked,
        ),
        evaluate_new_customer_trigger(
            current_signups=inputs.current_signups,
            baseline_signups=inputs.baseline_signups,
            thresholds=thresholds,
        ),
        evaluate_repurchase_trigger(
            current_repeat_members=inputs.current_repeat_members,
            current_members=inputs.current_members,
            baseline_repeat_members=inputs.baseline_repeat_members,
            baseline_members=inputs.baseline_members,
            thresholds=thresholds,
        ),
    ]


def scan(
    tier1_inputs: Tier1Inputs,
    tier2_inputs: Tier2Inputs | None = None,
    *,
    thresholds: TriggerThresholds,
) -> ScanResult:
    """2티어 스캔 1회 — **티어1이 열릴 때만 티어2**(`10-TRIGGER` 결정 100).

    티어2 를 매일 돌리면 브랜드당 Spring 호출이 3콜에서 8콜로 늘고, 대부분의 날은
    아무 신호도 없다. ``opened`` 가 False 면 ``tier2`` 는 빈 목록이고 호출부는 조회
    자체를 생략한다 — 이 함수가 인자를 이미 받은 뒤라도 판정을 돌리지 않는 것이,
    호출부가 "열렸을 때만 조회"를 지키는지 테스트로 확인할 수 있게 한다.
    """
    tier1 = scan_tier1(tier1_inputs, thresholds=thresholds)
    opened = any(evaluation.fired for evaluation in tier1)
    tier2: list[TriggerEvaluation] = []
    if opened and tier2_inputs is not None:
        tier2 = scan_tier2(tier2_inputs, thresholds=thresholds)
    return ScanResult(
        tier1=tier1,
        tier2=tier2,
        opened=opened,
        fired=[e.trigger for e in (*tier1, *tier2) if e.fired],
    )
