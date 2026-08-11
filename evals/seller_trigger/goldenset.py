"""주입형 골든셋 — 알려진 이상을 심고 잡는지 본다 (이슈 #595, `12-EVAL` 결정 120).

`analysis/` 단위 테스트 41개는 **"함수가 안 터지는지"** 를 본다. 이 골든셋은 **"판정이
맞는지"** 를 본다 — 다른 층이다. `12-EVAL` §4 가 지적한 *"현재 검증에 정답 대조가
0건"* 의 그 자리다.

각 케이스는 자기가 증명하는 명제(``claim``)를 들고 다닌다. 케이스가 깨졌을 때 "무엇을
못 지키게 됐는지"가 테스트 이름이 아니라 데이터에 적혀 있어야, 나중에 기대값을 고쳐서
초록불을 만드는 유혹을 막을 수 있다.

[``known_gap`` 케이스에 관하여]
현재 **검출되지 않음이 확인된** 결함을 고정하는 케이스다. 통과 조건이 "잡는다"가 아니라
"못 잡는다"이므로, 후속 이슈가 고치면 이 케이스가 실패해서 알려준다. 실패가 곧 신호다 —
그때 ``known_gap`` 을 지우고 기대를 뒤집으면 된다. 결함을 주석으로만 남기면 고쳐진
사실을 아무도 모른 채 지나간다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.seller.analysis import scan, segmentation
from app.agents.seller.analysis.scan import TriggerThresholds
from evals.seller_trigger import synth

# 골든셋 전용 시드 — null 시뮬레이션과 분리한다. 같은 시드를 쓰면 "이 시리즈에서만
# 되는" 결과를 두 하네스가 함께 물려받아 서로를 검증하지 못한다.
GOLDEN_SEED = 595001
# 케이스가 도는 합성 브랜드 기본 규모 — 구매 약 44건/일(중간 규모).
GOLDEN_VIEWS = 2600.0
GOLDEN_PRODUCT_VIEWS = 900.0


@dataclass(frozen=True)
class GoldenOutcome:
    """케이스 1건의 결과. ``claim`` 이 이 케이스의 존재 이유다."""

    case_id: str
    title: str
    claim: str
    passed: bool
    observed: dict[str, Any] = field(default_factory=dict)
    known_gap: bool = False
    follow_up: str = ""


def _base_series(days: int = 90, **overrides: Any) -> synth.NullBrandSeries:
    params = synth.NullBrandParams(
        days=days,
        seed=GOLDEN_SEED,
        daily_views=GOLDEN_VIEWS,
        daily_product_views=GOLDEN_PRODUCT_VIEWS,
        **overrides,
    )
    return synth.generate_null_brand(params)


def _sales_evaluation(series: synth.NullBrandSeries, index: int, *, thresholds: TriggerThresholds):
    lookback = thresholds.lookback_days
    start = max(0, index - lookback + 1)
    return scan.evaluate_sales_trigger(
        series.dates[start : index + 1],
        series.sales[start : index + 1],
        series.purchases[start : index + 1],
        target_date=series.dates[index],
        thresholds=thresholds,
    )


def case_sales_sustained_drop(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-01 — 7일 지속 −30% 매출 급락을 잡는다."""
    series = _base_series()
    index = 60
    for offset in range(thresholds.baseline_days):
        series = synth.inject_sales_multiplier(
            series, target_date=series.dates[index - offset], factor=0.70
        )
    result = _sales_evaluation(series, index, thresholds=thresholds)
    return GoldenOutcome(
        case_id="gs-01",
        title="매출 지속 급락 검출",
        claim="7일 동안 −30% 가 이어지면 매출 트리거가 발동한다",
        passed=bool(result.fired and result.direction == "drop" and result.decided),
        observed={
            "fired": result.fired,
            "threshold_met": result.threshold_met,
            "significant": result.significant,
            "change": result.change,
            "p_value": result.p_value,
            "direction": result.direction,
        },
    )


def case_seasonal_trap(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-02 — 주말 −30% 계절성만 있고 이상은 0건일 때 발동하지 않는다."""
    series = _base_series(dow_weights=synth.WEEKEND_DIP_DOW_WEIGHTS)
    fired_dates = []
    for index in range(2 * thresholds.baseline_days - 1, len(series)):
        result = _sales_evaluation(series, index, thresholds=thresholds)
        if result.fired:
            fired_dates.append(series.dates[index])
    return GoldenOutcome(
        case_id="gs-02",
        title="계절 함정 미검출",
        claim="요일 효과만 있는 구간에서는 매출 트리거가 걸리지 않는다"
        " (7일 합끼리 비교하면 요일 효과가 정의상 상쇄된다)",
        passed=not fired_dates,
        observed={
            "fired_dates": fired_dates,
            "scanned": len(series) - 2 * thresholds.baseline_days + 1,
        },
    )


def case_zero_sales_recovery(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-03 — 무매출 이력 직후 매출 발생을 잡되, 변화율은 정의 불가로 남긴다."""
    series = _base_series()
    index = 60
    series = synth.inject_zero_sales_run(
        series,
        start_date=series.dates[index - 2 * thresholds.baseline_days + 1],
        days=thresholds.baseline_days,
    )
    result = _sales_evaluation(series, index, thresholds=thresholds)
    return GoldenOutcome(
        case_id="gs-03",
        title="무매출 직후 매출 발생 검출",
        claim="기준 구간이 전부 0 이면 변화율은 정의 불가(change=None)지만"
        " 어떤 임계도 넘으므로 임계는 통과하고, 검정이 발동을 확정한다",
        passed=bool(result.fired and result.decided and result.change is None),
        observed={
            "fired": result.fired,
            "change": result.change,
            "threshold_met": result.threshold_met,
            "significant": result.significant,
            "p_value": result.p_value,
        },
    )


def case_insufficient_sample(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-04 — 표본이 부족하면 `decided=False`. **"이상 없음"이 아니다.**"""
    series = synth.truncate(_base_series(), 2)
    result = scan.evaluate_sales_trigger(
        series.dates,
        series.sales,
        series.purchases,
        target_date=series.dates[-1],
        thresholds=thresholds,
    )
    return GoldenOutcome(
        case_id="gs-04",
        title="표본 부족은 판정 보류",
        claim="2일치로는 판정할 수 없다 — decided=False 이고, 이것을"
        " fired=False 하나로 뭉뚱그리면 '못 봤다'가 '정상이다'로 둔갑한다",
        passed=bool(not result.decided and not result.fired and result.hold_reason),
        observed={"decided": result.decided, "hold_reason": result.hold_reason},
    )


def case_conversion_rise(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-05 — 표본이 충분한 전환율 상승을 잡는다."""
    result = scan.evaluate_conversion_trigger(
        current_view=3000,
        current_purchase=144,  # 4.8%
        baseline_view=3000,
        baseline_purchase=90,  # 3.0%
        thresholds=thresholds,
    )
    return GoldenOutcome(
        case_id="gs-05",
        title="전환율 상승 검출",
        claim="3.0% → 4.8%(n=3000)는 임계·유의를 모두 넘어 발동한다",
        passed=bool(result.fired and result.direction == "spike"),
        observed={
            "fired": result.fired,
            "threshold_met": result.threshold_met,
            "significant": result.significant,
            "p_value": result.p_value,
            "change": result.change,
        },
    )


def case_low_volume_noise(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-06 — 저볼륨 노이즈는 임계를 넘어도 발동하지 않는다."""
    result = scan.evaluate_conversion_trigger(
        current_view=40,
        current_purchase=1,  # 2.5%
        baseline_view=280,
        baseline_purchase=9,  # 3.2%
        thresholds=thresholds,
    )
    return GoldenOutcome(
        case_id="gs-06",
        title="저볼륨 오탐 방지",
        claim="n=40 의 −22% 변동은 임계를 넘지만 유의하지 않다 —"
        " **AND 의 통계 쪽이 실제로 막는다**",
        passed=bool(not result.fired and result.decided and result.threshold_met),
        observed={
            "fired": result.fired,
            "threshold_met": result.threshold_met,
            "significant": result.significant,
            "p_value": result.p_value,
        },
    )


def four_pattern_products() -> list[dict]:
    """4유형이 명확히 갈리는 상품 집합 — 전환직결 / 카트이탈 / 구경 / 저활동.

    그룹 안에서 ``index % n`` 으로 작게 변주해 실루엣이 1.0 으로 퇴화하지 않게 한다
    (`tests/unit/_seller_feature_fixtures.four_type_rows` 와 같은 수법).
    """
    rows: list[dict] = []
    for i in range(12):  # 전환직결형 — 전 단계 우량
        rows.append(
            {
                "product_id": 100 + i,
                "view": 900 + i * 7,
                "cart": 270 + i * 2,
                "checkout": 200 + i,
                "purchase": 170 + i,
                "visitors": 700 + i * 5,
            }
        )
    for i in range(12):  # 카트이탈형 — 담기는 많고 결제로 안 넘어간다
        rows.append(
            {
                "product_id": 200 + i,
                "view": 880 + i * 6,
                "cart": 300 + i * 2,
                "checkout": 30 + i,
                "purchase": 8 + i % 3,
                "visitors": 690 + i * 4,
            }
        )
    for i in range(12):  # 구경형 — 조회만 많다
        rows.append(
            {
                "product_id": 300 + i,
                "view": 1500 + i * 9,
                "cart": 25 + i,
                "checkout": 8 + i % 3,
                "purchase": 3 + i % 2,
                "visitors": 1200 + i * 7,
            }
        )
    for i in range(12):  # 저활동형 — 전 단계가 낮다
        # ⚠️ 이 그룹만 변주를 비율이 아니라 절대량에 준다. 비율(cart/view 등)을 흔들면
        # 분모가 작아 비율 분산이 커지고 k=5 에서 이 그룹이 둘로 갈린다(실측).
        rows.append(
            {
                "product_id": 400 + i,
                "view": 40 + i,
                "cart": 8,
                "checkout": 4,
                "purchase": 2,
                "visitors": 34 + i,
            }
        )
    return rows


def case_cluster_four_types(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-07 — 4유형이 분명한 입력에서 k=4 를 고르고 라벨 4종이 나온다."""
    clusters = segmentation.cluster_products(
        four_pattern_products(), k_min=2, k_max=5, random_state=42
    )
    labels = [cluster.label for cluster in clusters]
    silhouette = clusters[0].silhouette if clusters else 0.0
    return GoldenOutcome(
        case_id="gs-07",
        title="군집 4유형 복원",
        claim="유형이 분명한 입력에서 실루엣 최대 k 가 4 로 잡히고 군집이 4개 나온다",
        passed=bool(len(clusters) == 4 and silhouette > 0.5),
        observed={"k": len(clusters), "silhouette": silhouette, "labels": labels},
    )


def case_cart_abandon_spike(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-08 — 장바구니 이탈률 20% → 32% 급증을 잡는다(픽스처 직접 작성)."""
    result = scan.evaluate_cart_abandon_trigger(
        current_removes=160,
        current_adds=500,  # 32%
        baseline_removes=700,
        baseline_adds=3500,  # 20%
        thresholds=thresholds,
    )
    return GoldenOutcome(
        case_id="gs-08",
        title="장바구니 이탈률 급증 검출",
        claim="20% → 32%(+12%p)는 임계(+10%p)와 유의를 모두 넘어 발동한다",
        passed=bool(result.fired and result.change_unit == "pp"),
        observed={
            "fired": result.fired,
            "change": result.change,
            "change_unit": result.change_unit,
            "p_value": result.p_value,
        },
    )


def case_cart_abandon_boundary(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-09 — 편입일을 가로지르는 비교는 전면 보류."""
    result = scan.evaluate_cart_abandon_trigger(
        current_removes=160,
        current_adds=500,
        baseline_removes=700,
        baseline_adds=3500,
        thresholds=thresholds,
        boundary_blocked=True,
    )
    return GoldenOutcome(
        case_id="gs-09",
        title="remove_from_cart 편입일 경계 보류",
        claim="2026-08-06 편입일을 가로지르는 비교는 '삭제가 늘었다'가 아니라"
        " '삭제를 이제 센다'라서 같은 입력이라도 판정하지 않는다",
        passed=bool(not result.decided and not result.fired and result.hold_reason),
        observed={"decided": result.decided, "hold_reason": result.hold_reason},
    )


def case_single_day_crash(thresholds: TriggerThresholds) -> GoldenOutcome:
    """gs-10 — 하루짜리 급락은 **현재 검출되지 않는다**(고정된 결함)."""
    series = _base_series()
    index = 60
    series = synth.inject_sales_multiplier(series, target_date=series.dates[index], factor=0.40)
    result = _sales_evaluation(series, index, thresholds=thresholds)
    return GoldenOutcome(
        case_id="gs-10",
        title="하루짜리 급락 — 현재 미검출(고정)",
        claim="주간 합끼리 비교하므로 하루 −60% 는 주간 −8.6% 로 희석돼 유의해지지 않는다."
        " 후속 이슈가 하루 단위 검출을 붙이면 이 케이스가 실패해서 알려준다",
        passed=not result.fired,
        observed={
            "fired": result.fired,
            "threshold_met": result.threshold_met,
            "significant": result.significant,
            "change": result.change,
            "p_value": result.p_value,
        },
        known_gap=True,
        follow_up="무인 스캔의 하루 단위 매출 급락 검출 — STL 창 경계 무검출·과다 발화 해소",
    )


CASES = (
    case_sales_sustained_drop,
    case_seasonal_trap,
    case_zero_sales_recovery,
    case_insufficient_sample,
    case_conversion_rise,
    case_low_volume_noise,
    case_cluster_four_types,
    case_cart_abandon_spike,
    case_cart_abandon_boundary,
    case_single_day_crash,
)


def run_goldenset(thresholds: TriggerThresholds) -> list[GoldenOutcome]:
    """전 케이스를 돌린다. 순서 고정(결정론)."""
    return [case(thresholds) for case in CASES]
