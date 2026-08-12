"""sop/compute/causes.py — 원인 후보 7규칙 (이슈 #597, `06-REPORT` §2).

이슈 완료 조건을 여기서 고정한다: **규칙 7종 각각 성립 1건** + **후행 이벤트 폐기** +
**14일 밖 이벤트 폐기**. 앞의 둘이 이 모듈의 존재 이유다 — 후행 이벤트를 남기면
"매출이 떨어진 날 가격을 내렸다"가 "가격 때문에 떨어졌다"로 읽히는 후보가 LLM 에 간다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.seller.sop.compute.causes import compute_causes
from app.agents.seller.sop.context import (
    CAUSE_EVENT_KINDS,
    AnalysisContext,
    Comparison,
    PastAction,
    Verdict,
)
from app.core.config import Settings
from app.schemas.spring import (
    ChurnResult,
    OrderEventsResult,
    ProductChangeLogResult,
    ProductChangeLogRow,
    SellerReviewList,
    SellerReviewRow,
)

_FROM = date(2026, 8, 1)
_TO = date(2026, 8, 10)
# 매출 이상일(8/3)이 대상이면 지표 변화일도 8/3 이다 — key 에 날짜가 붙어 있기 때문.
_ANOMALY_KEY = "sales_anomaly:2026-08-03"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _ctx(worker: str = "sales_anomaly", **overrides) -> AnalysisContext:
    return AnalysisContext(worker=worker, brand_id=7, period_from=_FROM, period_to=_TO, **overrides)


def _anomaly_ctx() -> AnalysisContext:
    return _ctx(
        verdicts=[
            Verdict(
                key=_ANOMALY_KEY,
                verdict="significant_drop",
                method="stl_gesd",
                detail={"actual": 4_120_000.0, "deviation_pct": -42.0},
            )
        ]
    )


def _log(
    *, product_id: int = 101, change_type: str, old: str, new: str, day: str
) -> ProductChangeLogRow:
    return ProductChangeLogRow(
        productId=product_id,
        productName="감귤청",
        changeType=change_type,
        oldValue=old,
        newValue=new,
        createdAt=f"{day}T10:00:00+09:00",
    )


def _churn(*, exposed: int, cohort: int = 112) -> ChurnResult:
    return ChurnResult(
        cohortSize=cohort,
        churnRate=0.2,
        preChurnSignals={
            "cancelCount": 0,
            "returnReasonsTop": [],
            "zeroResultSearchSessions": 0,
            "priceIncreaseExposed": exposed,
        },
    )


def _kinds(ctx: AnalysisContext) -> list[str]:
    return [cause.event_kind for cause in ctx.causes]


# ── 규칙 1~3 — I-15 한 원천 ────────────────────────────────────────────────────


def test_규칙1_가격_인상은_선행_원인_후보가_된다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="12900", new="15900", day="2026-08-01")]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)

    assert _kinds(ctx) == ["price_change"]
    cause = ctx.causes[0]
    assert cause.lag_days == 2
    assert cause.product_id == 101
    assert cause.strength == "temporal_only"  # I-16 대조 근거가 없으면 승격하지 않는다
    assert "12,900 → 15,900원" in cause.event_desc
    assert "+23.3%" in cause.event_desc


def test_규칙1_가격_인하는_후보가_아니다(settings: Settings) -> None:
    """하락을 설명하는 자리에 인하를 놓으면 방향이 반대다."""
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="15900", new="12900", day="2026-08-01")]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)
    assert ctx.causes == []


def test_규칙1_이탈자_노출이_있으면_correlated로_승격한다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="12900", new="15900", day="2026-08-01")]
    )
    compute_causes(ctx, change_logs=logs, churn_now=_churn(exposed=34), settings=settings)

    cause = ctx.causes[0]
    assert cause.strength == "correlated"
    assert "112명 중 34명" in cause.corroboration
    assert "30.4%" in cause.corroboration


def test_규칙1_노출_0명이면_승격하지_않는다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="12900", new="15900", day="2026-08-01")]
    )
    compute_causes(ctx, change_logs=logs, churn_now=_churn(exposed=0), settings=settings)

    assert ctx.causes[0].strength == "temporal_only"
    assert ctx.causes[0].corroboration == ""


def test_규칙2_품절은_STOCK_newValue_0으로_판정한다(settings: Settings) -> None:
    """`SOLD_OUT` 상태값은 계약에 없다 — 품절 신호는 STOCK 행의 새 값 0 이다."""
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[
            _log(change_type="STOCK", old="4", new="0", day="2026-08-02"),
            _log(product_id=102, change_type="STOCK", old="10", new="3", day="2026-08-02"),
        ]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)

    assert _kinds(ctx) == ["stock_out"]
    assert "품절" in ctx.causes[0].event_desc


def test_규칙3_노출_중단은_STATUS_HIDDEN만_본다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[
            _log(change_type="STATUS", old="ON_SALE", new="HIDDEN", day="2026-08-02"),
            _log(
                product_id=102, change_type="STATUS", old="HIDDEN", new="ON_SALE", day="2026-08-02"
            ),
        ]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)

    assert _kinds(ctx) == ["status_change"]
    assert ctx.causes[0].product_id == 101


# ── 규칙 4 — I-14 목록 모드 일자 버킷팅 ────────────────────────────────────────


def _order_rows(spike_day: str) -> list[dict]:
    rows: list[dict] = []
    for day in range(3, 10):  # 8/03~8/09 — 실패 0건
        for _ in range(20):
            rows.append({"toStatus": "PAID", "createdAt": f"2026-08-{day:02d}T10:00:00+09:00"})
    for index in range(20):  # 급증일 — 20건 중 15건 실패
        rows.append(
            {
                "toStatus": "PAYMENT_FAILED" if index < 15 else "PAID",
                "createdAt": f"{spike_day}T10:00:00+09:00",
            }
        )
    return rows


def test_규칙4_결제_실패율_급증일이_후보가_된다(settings: Settings) -> None:
    ctx = _ctx(
        "conversion",
        verdicts=[
            Verdict(
                key="conversion:checkout_to_purchase",
                verdict="significant_drop",
                method="two_proportion_z",
                p_value=0.003,
            )
        ],
        comparisons=[
            Comparison(
                key="conversion:checkout_to_purchase",
                current=0.42,
                baseline=0.61,
                delta_pct=-31.1,
                baseline_from=date(2026, 7, 22),
                baseline_to=date(2026, 7, 31),
            )
        ],
    )
    events = OrderEventsResult(rows=_order_rows("2026-08-02"))
    compute_causes(ctx, order_events=events, settings=settings)

    assert _kinds(ctx) == ["payment_failure"]
    cause = ctx.causes[0]
    assert cause.event_at == date(2026, 8, 2)
    assert cause.strength == "correlated"  # 검정이 유의할 때만 후보라 항상 correlated
    assert "2-proportion z" in cause.corroboration
    assert "결제 시작→구매 전환율" in cause.target_desc


def test_규칙4_평탄한_실패율은_후보를_만들지_않는다(settings: Settings) -> None:
    ctx = _ctx(
        "conversion",
        verdicts=[Verdict(key="conversion:view_to_cart", verdict="significant_drop", method="z")],
    )
    rows = [
        {
            "toStatus": "PAID" if index % 10 else "PAYMENT_FAILED",
            "createdAt": f"2026-08-{day:02d}T10:00:00+09:00",
        }
        for day in range(2, 10)
        for index in range(20)
    ]
    compute_causes(ctx, order_events=OrderEventsResult(rows=rows), settings=settings)
    assert ctx.causes == []


# ── 규칙 5 — ctx 내부(세그먼트 순유출) ─────────────────────────────────────────


def _shift_ctx() -> AnalysisContext:
    return _ctx(
        "churn",
        verdicts=[
            Verdict(key="churn_rate", verdict="significant_rise", method="z", p_value=0.01),
            Verdict(
                key="segment_size:충성형",
                verdict="significant_drop",
                method="two_proportion_z",
                p_value=0.031,
            ),
        ],
        comparisons=[
            Comparison(
                key="segment_size:충성형",
                current=180,
                baseline=240,
                delta_pct=-25.0,
                baseline_from=date(2026, 7, 28),
                baseline_to=date(2026, 8, 3),
            )
        ],
    )


def test_규칙5_세그먼트_순유출은_비교_스냅샷_다음날이_이벤트일이다(settings: Settings) -> None:
    ctx = _shift_ctx()
    compute_causes(ctx, settings=settings)

    assert _kinds(ctx) == ["segment_shift"]
    cause = ctx.causes[0]
    assert cause.event_at == date(2026, 8, 4)
    assert cause.target_key == "churn_rate"  # 이벤트 자신(segment_size)은 대상에서 빠진다
    assert cause.strength == "correlated"
    assert "240명 → 180명" in cause.event_desc


def test_규칙5_충성형_증가는_순유출이_아니다(settings: Settings) -> None:
    ctx = _shift_ctx()
    ctx.verdicts[1] = Verdict(
        key="segment_size:충성형", verdict="significant_rise", method="z", p_value=0.02
    )
    compute_causes(ctx, settings=settings)
    assert ctx.causes == []


# ── 규칙 6 — I-31 목록 모드 일자 버킷팅 ────────────────────────────────────────


def _review_rows(spike_day: str) -> list[SellerReviewRow]:
    rows: list[SellerReviewRow] = []
    review_id = 0
    for day in range(3, 10):
        for _ in range(10):
            review_id += 1
            rows.append(
                SellerReviewRow(
                    reviewId=review_id, rating=5, createdAt=f"2026-08-{day:02d}T10:00:00+09:00"
                )
            )
    for index in range(10):
        review_id += 1
        rows.append(
            SellerReviewRow(
                reviewId=review_id,
                rating=1 if index < 8 else 5,
                createdAt=f"{spike_day}T10:00:00+09:00",
            )
        )
    return rows


def test_규칙6_저평점_급증일이_후보가_되고_강도는_temporal_only다(settings: Settings) -> None:
    """검정으로 **탐지**하되 승격은 하지 않는다 — 리뷰와 전환을 코드가 대조한 적은 없다."""
    ctx = _ctx(
        "conversion",
        verdicts=[Verdict(key="conversion:view_to_cart", verdict="significant_drop", method="z")],
    )
    compute_causes(
        ctx, reviews=SellerReviewList(rows=_review_rows("2026-08-02")), settings=settings
    )

    assert _kinds(ctx) == ["review_drop"]
    cause = ctx.causes[0]
    assert cause.event_at == date(2026, 8, 2)
    assert cause.strength == "temporal_only"
    assert "저평점(1~2점)" in cause.event_desc


# ── 규칙 7 — 과거 적용 액션 ────────────────────────────────────────────────────


def test_규칙7_과거_액션은_applied_at이_있어야_후보다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    ctx.past_actions.append(
        PastAction(
            rec_id="r-1",
            action_type="price_adjust",
            target="상품 101",
            applied_at=date(2026, 8, 1),
        )
    )
    compute_causes(ctx, settings=settings)

    assert _kinds(ctx) == ["past_action"]
    assert ctx.causes[0].lag_days == 2


def test_규칙7_applied_at이_없으면_lag_기준이_없어_후보가_아니다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    ctx.past_actions.append(PastAction(rec_id="r-1", action_type="price_adjust", target="상품 101"))
    compute_causes(ctx, settings=settings)
    assert ctx.causes == []


# ── 공통 가드 ─────────────────────────────────────────────────────────────────


def test_후행_이벤트는_폐기된다(settings: Settings) -> None:
    """결과가 원인을 앞설 수 없다 — 같은 날(lag 0)과 뒤(lag<0) 모두 버린다."""
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[
            _log(change_type="PRICE", old="1000", new="2000", day="2026-08-03"),  # lag 0
            _log(product_id=102, change_type="PRICE", old="1000", new="2000", day="2026-08-05"),
        ]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)
    assert ctx.causes == []


def test_창_밖_이벤트는_폐기된다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="1000", new="2000", day="2026-07-01")]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)
    assert ctx.causes == []

    narrow = Settings(_env_file=None, seller_cause_window_days=1)
    ctx2 = _anomaly_ctx()
    logs2 = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="1000", new="2000", day="2026-08-01")]
    )
    compute_causes(ctx2, change_logs=logs2, settings=narrow)
    assert ctx2.causes == []  # lag 2 > 창 1


def test_후보_상한을_넘기지_않고_lag_오름차순이다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[
            _log(
                product_id=200 + offset,
                change_type="STOCK",
                old="4",
                new="0",
                day=f"2026-07-{31 - offset:02d}",
            )
            for offset in range(8)
        ]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)

    assert len(ctx.causes) == settings.seller_cause_max_candidates
    lags = [cause.lag_days for cause in ctx.causes]
    assert lags == sorted(lags)


def test_동률_lag는_correlated가_먼저다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[
            _log(product_id=101, change_type="PRICE", old="12900", new="15900", day="2026-08-01"),
            _log(product_id=102, change_type="STOCK", old="4", new="0", day="2026-08-01"),
        ]
    )
    compute_causes(ctx, change_logs=logs, churn_now=_churn(exposed=34), settings=settings)

    assert [cause.strength for cause in ctx.causes] == ["correlated", "temporal_only"]


def test_유의_판정이_없으면_후보를_만들지_않는다(settings: Settings) -> None:
    ctx = _ctx(verdicts=[Verdict(key="sales_anomaly", verdict="undecided", method="stl_gesd")])
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="12900", new="15900", day="2026-08-01")]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)
    assert ctx.causes == []


def test_워커_밖_규칙은_후보를_만들지_않는다(settings: Settings) -> None:
    """가격 인상은 매출·전환을 설명한다 — churn ctx 에 달면 대상이 어긋난다."""
    ctx = _ctx(
        "churn",
        verdicts=[Verdict(key="churn_rate", verdict="significant_rise", method="z")],
    )
    logs = ProductChangeLogResult(
        rows=[_log(change_type="PRICE", old="12900", new="15900", day="2026-08-01")]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)
    assert ctx.causes == []


def test_생성기는_정해진_어휘_밖의_event_kind를_내지_않는다(settings: Settings) -> None:
    ctx = _anomaly_ctx()
    logs = ProductChangeLogResult(
        rows=[
            _log(change_type="PRICE", old="12900", new="15900", day="2026-08-01"),
            _log(product_id=102, change_type="STOCK", old="4", new="0", day="2026-08-02"),
            _log(
                product_id=103, change_type="STATUS", old="ON_SALE", new="HIDDEN", day="2026-08-02"
            ),
        ]
    )
    compute_causes(ctx, change_logs=logs, settings=settings)

    assert ctx.causes
    assert {cause.event_kind for cause in ctx.causes} <= CAUSE_EVENT_KINDS


def test_원천이_없으면_그_규칙만_조용히_건너뛴다(settings: Settings) -> None:
    """조회 실패를 Hold 로 남기는 것은 load 스텝 소관이라 여기서 중복 발행하지 않는다."""
    ctx = _anomaly_ctx()
    compute_causes(ctx, settings=settings)
    assert ctx.causes == []
    assert ctx.holds == []
