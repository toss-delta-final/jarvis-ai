"""합성 브랜드 생성기 — **집계 응답 수준**, 결정론 (이슈 #595, `12-EVAL` §6.1).

만드는 것은 일별 집계 시계열 하나다. 원시 이벤트 행도, SQL 도 만들지 않는다
(이유는 패키지 docstring).

[null 의 정의 — 무엇을 "정상"이라 부르는가]
- **추세·시즌·프로모션 0.** 레벨은 상수다. 이상 주입도 0건이다.
- **요일 효과만 있다.** 가중치는 `data-analysis/stats_jarvis.json` 의
  ``time_weights_utc`` 168칸을 isodow 로 주변화한 REES46 실측이다.
  ⚠️ `generate_dummy.py` 는 그 UTC isodow 를 KST 달력일에 그대로 붙여 ~9시간 위상
  오차가 있는데, 여기서는 답습하지 않는다 — 시작일을 월요일로 고정해 KST 요일에 맞춘다.
- **퍼널은 중첩 이항이다.** views ~ Poisson, carts ~ Binom(views, p), … 로 만들어
  ``cart <= view`` 가 구조적으로 보장된다. 실데이터는 이벤트 카운트라 역전이 나오지만
  (그래서 `scan._rate_comparison` 이 그 경우를 보류로 옮긴다), null 은 검정의 전제가
  성립하는 이상적 표본이어야 AND 조건이 얼마나 누르는지를 분리해 볼 수 있다.

[그래서 이 시뮬레이션이 못 재는 것]
정상 변동이 검정이 가정하는 분포와 **정확히 일치**한다. 실제 브랜드의 과분산
(overdispersion)·자기상관·프로모션은 없으므로, 운영 발동률은 여기서 잰 값보다
**높게** 나올 가능성이 크다. 게이트는 하한 검증이지 상한 보증이 아니다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path

from app.agents.seller.analysis.scan import Tier1Inputs, Tier2Inputs

# REES46 2019-10 실측 요일 가중치(isodow 1=월 … 7=일). 합 1.0.
# 출처: data-analysis/stats_jarvis.json 의 time_weights_utc 168칸을 isodow 로 주변화.
# 상수로 박아 두는 이유는 하네스를 자족적으로 만들기 위함이고, 원본과 어긋나면
# tests/eval/test_seller_trigger_null_sim.py 가 잡는다(load_dow_weights 로 재유도해 대조).
DEFAULT_DOW_WEIGHTS: dict[int, float] = {
    1: 0.125357,
    2: 0.160223,
    3: 0.156695,
    4: 0.150311,
    5: 0.137382,
    6: 0.132072,
    7: 0.137960,
}

# 골든셋 "계절 함정" 케이스용 — 주말 −30%. REES46 실측 요일 진폭은 ±11% 뿐이라
# STL 이 일하는지 보려면 더 센 계절성이 필요하다(`12-EVAL` §6.2 케이스 2).
WEEKEND_DIP_DOW_WEIGHTS: dict[int, float] = {
    **dict.fromkeys((1, 2, 3, 4, 5), 1.0 / 5.6),
    6: 0.7 / 5.6,
    7: 0.7 / 5.6,
}

# 시작 요일을 월요일로 고정한다 — 요일 가중치를 KST 달력에 맞추기 위한 앵커이고,
# STL(period=7)의 위상이 시뮬레이션마다 흔들리지 않게 한다. 2024-01-01 은 월요일.
DEFAULT_START_DATE = "2024-01-01"

# 상품 인기 분포 지수 — generate_dummy.DEFAULTS["zipf_alpha"] 와 같은 값(REES46 실측).
ZIPF_ALPHA = 0.8835


@dataclass(frozen=True)
class NullBrandParams:
    """합성 브랜드 1개의 모수. 전부 상수 레벨 — 추세 항이 없다는 것이 이 표의 요점이다.

    앵커 출처: ``p_cart`` 는 ``session_funnel.share_with_cart``(0.0620),
    ``p_purchase`` 는 ``direct_purchase`` 를 뺀 ``p_purchase_given_checkout``(0.60,
    generate_dummy 기본값), ``p_repeat`` 은 ``repeat_buyer_ratio_of_buyers``(0.3217),
    ``p_remove`` 는 generate_dummy 의 ``remove_per_cart`` 기본값(0.25 — cosmetics
    실측 0.685 는 과하다고 그쪽이 이미 판단해 뒀다).
    """

    days: int
    seed: int = 20260811
    start_date: str = DEFAULT_START_DATE
    dow_weights: Mapping[int, float] = field(default_factory=lambda: dict(DEFAULT_DOW_WEIGHTS))
    daily_views: float = 2600.0
    p_cart: float = 0.062
    p_checkout: float = 0.45
    p_purchase: float = 0.60
    p_remove: float = 0.25
    unit_price_mu: float = 10.5  # 로그정규 — exp(10.5) ~= 36,000원
    unit_price_sigma: float = 0.55
    daily_signups: float = 6.0
    daily_order_members: float = 40.0
    p_repeat: float = 0.3217
    daily_suspicious: float = 0.0  # null 에는 어뷰징이 없다
    products: int = 40
    daily_product_views: float = 900.0
    p_product_purchase: float = 0.03


@dataclass(frozen=True)
class NullBrandSeries:
    """일별 집계 시계열 — 각 리스트의 index 가 같은 날짜다.

    필드 이름은 원천 계약의 어휘를 따른다: ``carts``/``removes`` 는 I-13
    ``addToCart``/``removeFromCart``, ``signups`` 는 I-8 ``SIGNUP``, ``members``/
    ``repeat_members`` 는 I-14 ``groupBy=memberId`` 의 회원 수와 ``orderCount >= 2`` 인
    회원 수다(결정 101 — I-38 의 동명 필드와 정의가 다르다).
    """

    dates: list[str]
    sales: list[float]
    views: list[int]
    carts: list[int]
    checkouts: list[int]
    purchases: list[int]
    removes: list[int]
    signups: list[int]
    members: list[int]
    repeat_members: list[int]
    suspicious_members: list[int]
    product_views: list[dict[int, int]]
    product_quantities: list[dict[int, int]]

    def __len__(self) -> int:
        return len(self.dates)


def load_dow_weights(stats_path: Path | None = None) -> dict[int, float]:
    """`stats_jarvis.json` 에서 isodow 주변 가중치를 재유도한다(상수 대조용).

    운영 경로가 아니라 **검증 경로**다 — `DEFAULT_DOW_WEIGHTS` 가 원본과 어긋나면
    테스트가 여기서 재계산해 잡는다. 파일이 없으면 FileNotFoundError 를 그대로 낸다
    (조용히 상수로 물러나면 대조가 무의미해진다).
    """
    path = stats_path or Path(__file__).resolve().parents[2] / "data-analysis" / "stats_jarvis.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    totals: dict[int, float] = {}
    for cell in payload["time_weights_utc"]:
        isodow = int(cell["isodow"])
        totals[isodow] = totals.get(isodow, 0.0) + float(cell["weight"])
    grand = sum(totals.values())
    return {k: totals[k] / grand for k in sorted(totals)}


def _zipf_shares(count: int) -> list[float]:
    """상품 인기 분포 — 1/(i+1)^α 정규화. 균등하면 상품별 표본이 전부 같아 비현실적이다."""
    raw = [1.0 / ((i + 1) ** ZIPF_ALPHA) for i in range(count)]
    total = sum(raw)
    return [value / total for value in raw]


def generate_null_brand(params: NullBrandParams) -> NullBrandSeries:
    """이상 0건 합성 브랜드 — 같은 params 면 같은 시리즈(결정론).

    난수는 ``numpy.random.default_rng(seed)`` 단일 스트림이다. 스트림이 하나라
    모수를 하나라도 바꾸면 전체 수열이 재배열된다 — 그래서 "null 을 고정하고 이상만
    주입"하는 골든셋은 재생성이 아니라 **후처리 주입**(`inject_*`)으로 만든다.
    """
    if params.days < 1:
        raise ValueError(f"days 는 1 이상이어야 한다(got {params.days})")

    import numpy as np

    rng = np.random.default_rng(params.seed)
    start = date.fromisoformat(params.start_date)
    weights = dict(params.dow_weights)
    shares = _zipf_shares(params.products)

    dates: list[str] = []
    sales: list[float] = []
    views: list[int] = []
    carts: list[int] = []
    checkouts: list[int] = []
    purchases: list[int] = []
    removes: list[int] = []
    signups: list[int] = []
    members: list[int] = []
    repeat_members: list[int] = []
    suspicious: list[int] = []
    product_views: list[dict[int, int]] = []
    product_quantities: list[dict[int, int]] = []

    for offset in range(params.days):
        day = start + timedelta(days=offset)
        # 가중치 평균이 1 이 되도록 7 을 곱한다 — 레벨은 params 가 정하고 요일은 형태만 준다.
        weight = weights[day.isoweekday()] * 7.0

        day_views = int(rng.poisson(params.daily_views * weight))
        day_carts = int(rng.binomial(day_views, params.p_cart)) if day_views else 0
        day_checkouts = int(rng.binomial(day_carts, params.p_checkout)) if day_carts else 0
        day_purchases = int(rng.binomial(day_checkouts, params.p_purchase)) if day_checkouts else 0
        day_removes = int(rng.binomial(day_carts, params.p_remove)) if day_carts else 0
        day_sales = (
            float(
                np.sum(rng.lognormal(params.unit_price_mu, params.unit_price_sigma, day_purchases))
            )
            if day_purchases
            else 0.0
        )

        day_members = int(rng.poisson(params.daily_order_members * weight))
        day_repeat = int(rng.binomial(day_members, params.p_repeat)) if day_members else 0

        day_product_views: dict[int, int] = {}
        day_product_quantities: dict[int, int] = {}
        for product_index, share in enumerate(shares):
            product_id = 1000 + product_index
            seen = int(rng.poisson(params.daily_product_views * weight * share))
            day_product_views[product_id] = seen
            day_product_quantities[product_id] = (
                int(rng.binomial(seen, params.p_product_purchase)) if seen else 0
            )

        dates.append(day.isoformat())
        sales.append(day_sales)
        views.append(day_views)
        carts.append(day_carts)
        checkouts.append(day_checkouts)
        purchases.append(day_purchases)
        removes.append(day_removes)
        signups.append(int(rng.poisson(params.daily_signups * weight)))
        members.append(day_members)
        repeat_members.append(day_repeat)
        suspicious.append(
            int(rng.poisson(params.daily_suspicious)) if params.daily_suspicious else 0
        )
        product_views.append(day_product_views)
        product_quantities.append(day_product_quantities)

    return NullBrandSeries(
        dates=dates,
        sales=sales,
        views=views,
        carts=carts,
        checkouts=checkouts,
        purchases=purchases,
        removes=removes,
        signups=signups,
        members=members,
        repeat_members=repeat_members,
        suspicious_members=suspicious,
        product_views=product_views,
        product_quantities=product_quantities,
    )


def _window(values: Sequence[int], start: int, end: int) -> int:
    return int(sum(values[start:end]))


def tier1_inputs_at(
    series: NullBrandSeries, index: int, *, lookback_days: int, baseline_days: int
) -> Tier1Inputs | None:
    """index 일을 "분석 대상일"로 놓은 티어1 입력. 창이 안 차면 None(그날은 판정 불가).

    매출·전환율 모두 직전 ``baseline_days`` 일 합 대 그 이전 ``baseline_days`` 일 합이라
    (`scan.Tier1Inputs` 주석) 이 함수는 ``2 × baseline_days`` 일치 이력을 요구한다.
    매출 트리거의 주문 건수 축은 ``purchases`` 시계열이 그대로 I-6 ``orderCount`` 다.

    퍼널은 이벤트 카운트라 일별 합이 곧 구간 조회 결과다(회원 중복 문제가 없다) —
    회원 지표의 합산 한계는 `tier2_inputs_at` 주 참조.
    """
    start = index - lookback_days + 1
    if start < 0 or index - 2 * baseline_days + 1 < 0:
        return None
    current = slice(index - baseline_days + 1, index + 1)
    baseline = slice(index - 2 * baseline_days + 1, index - baseline_days + 1)
    return Tier1Inputs(
        sales_dates=series.dates[start : index + 1],
        sales_values=series.sales[start : index + 1],
        sales_order_counts=series.purchases[start : index + 1],
        target_date=series.dates[index],
        current_view=int(sum(series.views[current])),
        current_purchase=int(sum(series.purchases[current])),
        baseline_view=int(sum(series.views[baseline])),
        baseline_purchase=int(sum(series.purchases[baseline])),
        suspicious_current=series.suspicious_members[index],
        suspicious_baseline=max(series.suspicious_members[index - baseline_days : index] or [0]),
    )


def tier2_inputs_at(
    series: NullBrandSeries, index: int, *, baseline_days: int
) -> Tier2Inputs | None:
    """index 일 기준 티어2 입력. 창이 안 차면 None.

    ⚠️ **회원 지표(재구매율)의 기준 구간은 일별 합이라 중복 제거가 안 돼 있다.** 실
    운영의 I-14 는 7일 구간을 한 번에 조회해 distinct 회원을 주므로 분모가 이보다
    작다. 즉 여기서는 표본이 과대평가되고 검정이 더 민감해진다 — 발동률이 **과대**
    추정되는 방향이라 게이트로서는 보수적이다(놓치는 쪽이 아니라 더 잡는 쪽).
    """
    if index - baseline_days < 0:
        return None
    base = slice(index - baseline_days, index)
    product_baseline: dict[int, tuple[int, int]] = {}
    for daily_views, daily_quantities in zip(
        series.product_views[base], series.product_quantities[base], strict=True
    ):
        for product_id, seen in daily_views.items():
            quantity, total_views = product_baseline.get(product_id, (0, 0))
            product_baseline[product_id] = (
                quantity + daily_quantities.get(product_id, 0),
                total_views + seen,
            )
    product_current = {
        product_id: (series.product_quantities[index].get(product_id, 0), seen)
        for product_id, seen in series.product_views[index].items()
    }
    return Tier2Inputs(
        product_current=product_current,
        product_baseline=product_baseline,
        current_removes=series.removes[index],
        current_adds=series.carts[index],
        baseline_removes=_window(series.removes, index - baseline_days, index),
        baseline_adds=_window(series.carts, index - baseline_days, index),
        current_signups=series.signups[index],
        baseline_signups=_window(series.signups, index - baseline_days, index),
        current_repeat_members=series.repeat_members[index],
        current_members=series.members[index],
        baseline_repeat_members=_window(series.repeat_members, index - baseline_days, index),
        baseline_members=_window(series.members, index - baseline_days, index),
    )


def inject_sales_multiplier(
    series: NullBrandSeries, *, target_date: str, factor: float
) -> NullBrandSeries:
    """이미 만든 null 시리즈의 하루 매출·주문 건수에 배수를 건다 — **후처리 주입**.

    재생성이 아니라 후처리인 이유: 난수 스트림이 하나라 모수를 바꿔 다시 생성하면
    나머지 999일까지 전부 달라진다. 그러면 "이 하루를 심었더니 잡혔다"가 아니라
    "다른 시리즈에서 잡혔다"가 되어 케이스가 증명하는 바가 흐려진다.
    """
    if target_date not in series.dates:
        raise ValueError(f"주입 대상일 {target_date} 이 시리즈에 없다")
    index = series.dates.index(target_date)
    sales = list(series.sales)
    purchases = list(series.purchases)
    sales[index] = sales[index] * factor
    purchases[index] = round(purchases[index] * factor)
    return replace(series, sales=sales, purchases=purchases)


def inject_zero_sales_run(
    series: NullBrandSeries, *, start_date: str, days: int
) -> NullBrandSeries:
    """연속 무매출 구간을 심는다 — "무매출 직후 매출 발생" 케이스의 앞부분.

    매출 금액만 0 으로 만들면 주문 건수가 남아 "0원인데 주문은 있었다"는 불가능한
    상태가 된다 — 두 축을 함께 0 으로 만든다(트리거 1 이 두 축을 다 쓴다).
    """
    if start_date not in series.dates:
        raise ValueError(f"주입 시작일 {start_date} 이 시리즈에 없다")
    index = series.dates.index(start_date)
    sales = list(series.sales)
    purchases = list(series.purchases)
    for offset in range(days):
        if index + offset < len(sales):
            sales[index + offset] = 0.0
            purchases[index + offset] = 0
    return replace(series, sales=sales, purchases=purchases)


def truncate(series: NullBrandSeries, days: int) -> NullBrandSeries:
    """앞에서 days 일만 남긴다 — 표본 부족 케이스용."""
    return NullBrandSeries(
        dates=series.dates[:days],
        sales=series.sales[:days],
        views=series.views[:days],
        carts=series.carts[:days],
        checkouts=series.checkouts[:days],
        purchases=series.purchases[:days],
        removes=series.removes[:days],
        signups=series.signups[:days],
        members=series.members[:days],
        repeat_members=series.repeat_members[:days],
        suspicious_members=series.suspicious_members[:days],
        product_views=series.product_views[:days],
        product_quantities=series.product_quantities[:days],
    )
