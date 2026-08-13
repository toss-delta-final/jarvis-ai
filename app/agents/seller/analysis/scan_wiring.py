"""Spring 조회 → `analysis/scan.py` 순수 판정 함수 배선 (이슈 #601).

`analysis/scan.py`는 원시 수치만 받는 순수 함수다(Settings 도, Spring 도 모른다) — 그래야
null 시뮬레이션이 운영과 같은 함수를 응답 위조 없이 부를 수 있다(그 모듈 docstring). 이
모듈이 그 경계를 넘는 자리다: Spring 조회를 묶어 `Tier1Inputs`/`Tier2Inputs`를 만들고
`scan()`을 부른다. **I/O 전용** — 판정 로직은 한 줄도 없다.

[창 규약 — 트리거마다 다르다, `scan.py`·`proportions.compare_counts` 실측 그대로]
브랜드 축 대부분(매출·전환율·어뷰징·상품 판매량·장바구니 이탈률·재구매율)은 **직전
7일(대상일 포함) 대 그 이전 7일**을 쓴다(`Tier1Inputs`/`proportions.compare_counts` 의
"매출 트리거는 7일 대 7일" 서술) — 이것이 10-TRIGGER 결정 97 원안("1일 대 직전 7일 평균")의
**의도된 예외**다(`scan.py` 모듈 docstring 근거). 유일한 예외가 신규 고객(트리거5)이다 —
`evaluate_new_customer_trigger`가 `current_days`를 넘기지 않아 `compare_counts` 기본값
1일이 적용된다(`proportions.compare_counts` "신규 고객은 1일 대 7일" 서술). 두 "7일"을
같은 창으로 섞으면 안 된다.

[호출 예산 — 이슈 원문의 "3콜"/"5콜"과 다르다, 정직하게 남긴다]
이슈 본문의 대략치는 `Tier1Inputs`/`Tier2Inputs`가 실제로 요구하는 원시 수치와 맞지 않는다
— 대부분의 Spring 집계 엔드포인트가 "기간 1개의 집계"만 반환하는 계약이라, 현재·기준
두 구간을 비교하려면 구간마다 별도 왕복이 필요하다(예외: `get_sales`는 시계열이라 한 콜에
양쪽 구간이 다 담긴다). 이 모듈은 값 정확성을 이슈 문서의 콜 수 근사보다 우선한다.

  티어1 = get_sales(1) + get_funnel×2 + get_order_events(groupBy=memberId)×2 = 5콜
  티어2 = get_events(groupBy=product)×2 + get_account_events(groupBy=eventType)×2 = 4콜
         (신호 있을 때만, `scan()`이 이미 "opened 아니면 tier2 호출 자체를 생략" 계약)

I-13 `groupBy=product` 응답의 `counts`에 `addToCart`/`removeFromCart`가 상품별로 이미
실려 있어, 브랜드 합산으로 장바구니 이탈률(트리거4)까지 같은 2콜에서 뽑는다 — eventType
전용 콜을 추가하지 않는다(`10-TRIGGER` §3.4 "호출 1회로 둘을 잰다"와 같은 절약 원리를
I-13에도 적용한 것). 마찬가지로 I-14 `groupBy=memberId` 응답을 어뷰징(티어1)과 재구매율
(티어2)이 공유한다 — 티어2 호출부는 이 모듈이 아니라 `fetch_scan_inputs`가 티어1 응답을
그대로 재사용해 넘긴다.

[I-14 `groupBy=memberId` rows 절단 — 알려진 한계, 정직하게 남긴다]
`OrderEventsResult.rows`는 `limit`(기본 100) 절단본이고 이 클라이언트는 `limit`을 조정할
파라미터를 노출하지 않는다(api-spec §4.4 계약). 분모(활성 회원 수)는 절단 전 전수인
`total`을 쓰지만, 분자(재구매 회원 수·의심 회원 수)는 절단된 `rows`에서만 셀 수 있어
활성 회원이 100명을 넘는 브랜드는 과소 집계될 수 있다. `scan.py`의 `TriggerEvaluation`에는
이 사실을 실을 훅(Hold 류)이 없어 — 코드 주석과 PR 설명으로만 남긴다. BE가 `limit`
파라미터나 전용 집계 엔드포인트를 열면 해소된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from app.agents.seller.analysis.scan import Tier1Inputs, Tier2Inputs, scan
from app.agents.seller.analysis.types import ScanResult
from app.agents.seller.sop.scan_params import thresholds_from_settings
from app.core.config import Settings
from app.schemas.spring import OrderEventsResult
from app.services.spring_client import SpringClient

logger = logging.getLogger(__name__)

_GROUP_BY_MEMBER = "memberId"
_GROUP_BY_PRODUCT = "product"
_GROUP_BY_EVENT_TYPE = "eventType"
_SIGNUP_KEY = "SIGNUP"

# I-13 groupBy=product의 counts 키(BehaviorProductRow docstring, remove_from_cart 편입 이후 5종).
_COUNT_ADD_TO_CART = "addToCart"
_COUNT_REMOVE_FROM_CART = "removeFromCart"
_COUNT_PRODUCT_VIEW = "productView"


@dataclass(frozen=True)
class _Windows:
    """브랜드 축 표준 7일 대 7일 창 — target_date(전날, KST) 기준."""

    current_from: date
    current_to: date
    baseline_from: date
    baseline_to: date

    @property
    def sales_from(self) -> date:
        """`get_sales` 시계열 시작일 — baseline_from과 같다(2×baseline_days 길이가 딱 맞음)."""
        return self.baseline_from


def _brand_axis_windows(target_date: date, baseline_days: int) -> _Windows:
    current_to = target_date
    current_from = target_date - timedelta(days=baseline_days - 1)
    baseline_to = current_from - timedelta(days=1)
    baseline_from = baseline_to - timedelta(days=baseline_days - 1)
    return _Windows(
        current_from=current_from,
        current_to=current_to,
        baseline_from=baseline_from,
        baseline_to=baseline_to,
    )


def cart_boundary_blocked(*, baseline_to: date, current_to: date, settings: Settings) -> bool:
    """비교 구간이 `remove_from_cart` 편입일(2026-08-06)을 가로지르는가(트리거4 전용).

    `sop/validate._boundary_defect`와 같은 부등식(`baseline_to < boundary <= current_to`)
    이다 — 그쪽은 대화형 `AnalysisContext.comparisons` 전체를 훑는 범용 검사라 여기서
    import 하지 않는다(이 모듈은 브랜드 축 단일 비교 1건만 보는 순수 함수 경계를 유지한다).
    """
    return any(
        baseline_to < boundary <= current_to
        for boundary in settings.seller_comparison_boundary_dates
    )


def _member_row_stats(result: OrderEventsResult) -> tuple[int, int, int]:
    """I-14 groupBy=memberId 응답 → (의심 회원 수, 재구매 회원 수, 활성 회원 수).

    `members`는 절단 전 전수(`total`, 없으면 rows 길이로 대체)를 쓰고, `suspicious`·
    `repeat`(orderCount>=2)는 절단된 rows에서만 셀 수 있다(모듈 docstring의 알려진 한계).
    """
    rows = result.rows
    suspicious = sum(1 for row in rows if bool(row.get("isSuspicious")))
    repeat = sum(1 for row in rows if int(row.get("orderCount") or 0) >= 2)
    members = result.total if result.total is not None else len(rows)
    return suspicious, repeat, members


def _signup_count(rows: list[dict]) -> int:
    """I-8 groupBy=eventType 버킷에서 SIGNUP 건수 — 0인 날은 버킷 자체가 없다(zero-fill 안 함)."""
    for row in rows:
        if row.get("key") == _SIGNUP_KEY:
            return int(row.get("count") or 0)
    return 0


def _product_and_cart_counts(rows: list) -> tuple[dict[int, tuple[int, int]], int, int]:
    """I-13 groupBy=product rows → ({productId: (판매수량, 조회수)}, addToCart 합, removeFromCart 합)."""
    products: dict[int, tuple[int, int]] = {}
    adds = 0
    removes = 0
    for row in rows:
        quantity = row.sales_quantity if row.sales_quantity is not None else 0
        views = row.counts.get(_COUNT_PRODUCT_VIEW, 0)
        products[row.product_id] = (quantity, views)
        adds += row.counts.get(_COUNT_ADD_TO_CART, 0)
        removes += row.counts.get(_COUNT_REMOVE_FROM_CART, 0)
    return products, adds, removes


@dataclass(frozen=True)
class ScanFetchResult:
    """`fetch_scan_inputs` 산출 — `scan()` 결과 + 티어2 조회가 실제로 실행됐는지."""

    result: ScanResult
    tier2_fetched: bool


async def fetch_scan_inputs(
    client: SpringClient,
    brand_id: int,
    *,
    target_date: date,
    settings: Settings,
    retries: int = 0,
) -> ScanFetchResult:
    """브랜드 1개의 무인 스캔 1회 — Spring 조회(최대 9콜) → `scan()`.

    `settings.seller_trigger_tier2_enabled=False`면 티어1이 열려도 티어2를 조회하지
    않는다(킬스위치, `10-TRIGGER.md` §6) — `ScanResult.tier2`는 빈 목록으로 남고
    `opened`만 보고서 생성 여부를 결정한다(호출부 소관, `scan()` docstring).
    """
    thresholds = thresholds_from_settings(settings)
    baseline_days = settings.seller_scan_baseline_days
    windows = _brand_axis_windows(target_date, baseline_days)

    sales_result = await client.get_sales(
        brand_id,
        windows.sales_from.isoformat(),
        windows.current_to.isoformat(),
        "daily",
        retries=retries,
    )
    funnel_current = await client.get_funnel(
        brand_id,
        windows.current_from.isoformat(),
        windows.current_to.isoformat(),
        retries=retries,
    )
    funnel_baseline = await client.get_funnel(
        brand_id,
        windows.baseline_from.isoformat(),
        windows.baseline_to.isoformat(),
        retries=retries,
    )
    member_current = await client.get_order_events(
        brand_id,
        windows.current_from.isoformat(),
        windows.current_to.isoformat(),
        group_by=_GROUP_BY_MEMBER,
        retries=retries,
    )
    member_baseline = await client.get_order_events(
        brand_id,
        windows.baseline_from.isoformat(),
        windows.baseline_to.isoformat(),
        group_by=_GROUP_BY_MEMBER,
        retries=retries,
    )

    suspicious_current, current_repeat_members, current_members = _member_row_stats(member_current)
    suspicious_baseline, baseline_repeat_members, baseline_members = _member_row_stats(
        member_baseline
    )

    # 날짜 오름차순 정렬을 여기서 강제한다 — evaluate_sales_trigger 는 `dates.index(target_date)`
    # 로 위치를 찾고 그 앞뒤를 슬라이스하므로, 응답 순서가 어긋나면 조용히 다른 구간을 비교하게
    # 된다(I-6 응답 정렬은 계약에 명시돼 있지 않다 — 방어적으로 여기서 보장한다).
    sales_series = sorted(sales_result.series, key=lambda point: point.date)

    tier1_inputs = Tier1Inputs(
        sales_dates=[point.date for point in sales_series],
        sales_values=[point.sales for point in sales_series],
        sales_order_counts=[point.order_count for point in sales_series],
        target_date=target_date.isoformat(),
        current_view=funnel_current.view,
        current_purchase=funnel_current.purchase,
        baseline_view=funnel_baseline.view,
        baseline_purchase=funnel_baseline.purchase,
        uncomputable_stages=[
            *funnel_current.uncomputable_stages,
            *funnel_baseline.uncomputable_stages,
        ],
        suspicious_current=suspicious_current,
        suspicious_baseline=suspicious_baseline,
    )

    tier1_only = scan(tier1_inputs, None, thresholds=thresholds)
    if not tier1_only.opened or not settings.seller_trigger_tier2_enabled:
        # 열리지 않았거나(scan() 자체가 tier2를 비운다) 킬스위치가 꺼져 있으면 티어2를
        # 조회하지 않는다 — `scan()`의 "opened 아니면 tier2 인자 자체를 안 만든다" 계약과
        # 같은 절약을 킬스위치에도 적용한다.
        return ScanFetchResult(result=tier1_only, tier2_fetched=False)

    # ── 티어1이 열렸다 — 티어2 4콜 ───────────────────────────────────────────
    events_current = await client.get_events(
        brand_id,
        windows.current_from.isoformat(),
        windows.current_to.isoformat(),
        group_by=_GROUP_BY_PRODUCT,
        retries=retries,
    )
    events_baseline = await client.get_events(
        brand_id,
        windows.baseline_from.isoformat(),
        windows.baseline_to.isoformat(),
        group_by=_GROUP_BY_PRODUCT,
        retries=retries,
    )
    product_current, current_adds, current_removes = _product_and_cart_counts(events_current.rows)
    product_baseline, baseline_adds, baseline_removes = _product_and_cart_counts(
        events_baseline.rows
    )

    # 신규 고객(트리거5)만 1일 대 7일 창이다(모듈 docstring) — target_date 당일 vs
    # 그 직전(target_date 미포함) 7일. 다른 트리거의 7일 대 7일 창(windows)과는 별개 계산이다
    # — windows.current_from을 재사용하면 대상일을 뺀 만큼 창이 하루씩 밀려 버린다.
    signup_baseline_to = target_date - timedelta(days=1)
    signup_baseline_from = signup_baseline_to - timedelta(days=baseline_days - 1)
    account_current = await client.get_account_events(
        brand_id,
        target_date.isoformat(),
        target_date.isoformat(),
        group_by=_GROUP_BY_EVENT_TYPE,
        retries=retries,
    )
    account_baseline = await client.get_account_events(
        brand_id,
        signup_baseline_from.isoformat(),
        signup_baseline_to.isoformat(),
        group_by=_GROUP_BY_EVENT_TYPE,
        retries=retries,
    )

    tier2_inputs = Tier2Inputs(
        product_current=product_current,
        product_baseline=product_baseline,
        current_removes=current_removes,
        current_adds=current_adds,
        baseline_removes=baseline_removes,
        baseline_adds=baseline_adds,
        cart_boundary_blocked=cart_boundary_blocked(
            baseline_to=windows.baseline_to, current_to=windows.current_to, settings=settings
        ),
        current_signups=_signup_count(account_current.rows),
        baseline_signups=_signup_count(account_baseline.rows),
        current_repeat_members=current_repeat_members,
        current_members=current_members,
        baseline_repeat_members=baseline_repeat_members,
        baseline_members=baseline_members,
    )
    result = scan(tier1_inputs, tier2_inputs, thresholds=thresholds)
    return ScanFetchResult(result=result, tier2_fetched=True)
