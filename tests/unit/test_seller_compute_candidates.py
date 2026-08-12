"""sop/compute/candidates.py — 추천 후보 4슬롯 (이슈 #597, `06-REPORT` §3).

이슈 완료 조건을 여기서 고정한다: **슬롯 4종** + **옵션 제외 규칙**. 옵션 제외는
`seller_stock_wire_mode` 와 무관해야 한다 — 모드를 따라가면 운영 스위치를 켜는 날
추천 목록이 조용히 줄어든다(`06` §0.2).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.seller.sop.compute.candidates import compute_candidates
from app.agents.seller.sop.context import AnalysisContext, CauseCandidate
from app.core.config import Settings
from app.schemas.spring import (
    BehaviorProductRow,
    ProductChangeLogResult,
    ProductChangeLogRow,
    SellerProductRow,
    SellerStockRow,
)

_FROM = date(2026, 8, 1)
_TO = date(2026, 8, 10)  # 10일 구간 — 일평균은 판매수량/10 이다


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _ctx(**overrides) -> AnalysisContext:
    return AnalysisContext(
        worker="sales_anomaly", brand_id=7, period_from=_FROM, period_to=_TO, **overrides
    )


def _product(
    product_id: int = 101,
    *,
    name: str = "감귤청",
    price: int = 15900,
    stock: int = 40,
    status: str = "ON_SALE",
    stocks: list[SellerStockRow] | None = None,
) -> SellerProductRow:
    return SellerProductRow(
        productId=product_id,
        name=name,
        price=price,
        stockQuantity=stock,
        status=status,
        stocks=stocks or [],
    )


def _behavior(product_id: int = 101, *, views: int = 0, sold: int | None = 0) -> BehaviorProductRow:
    return BehaviorProductRow(
        productId=product_id, counts={"productView": views}, salesQuantity=sold
    )


def _price_cause(product_id: int = 101, *, day: date = date(2026, 8, 1)) -> CauseCandidate:
    return CauseCandidate(
        target_key="sales_anomaly:2026-08-03",
        target_desc="8월 3일 매출 하락",
        event_kind="price_change",
        event_at=day,
        event_desc="8월 1일 감귤청 가격 12,900 → 15,900원 (+23.3%)",
        lag_days=2,
        strength="correlated",
        product_id=product_id,
    )


def _price_log(*, product_id: int = 101, old: str, new: str, day: str) -> ProductChangeLogRow:
    return ProductChangeLogRow(
        productId=product_id,
        productName="감귤청",
        changeType="PRICE",
        oldValue=old,
        newValue=new,
        createdAt=f"{day}T10:00:00+09:00",
    )


def _slots(ctx: AnalysisContext) -> list[str]:
    return [candidate.slot for candidate in ctx.candidate_actions]


# ── 슬롯 1·2 — 재고 ───────────────────────────────────────────────────────────


def test_슬롯1_재고_부족은_일평균_판매x커버일수_올림이다(settings: Settings) -> None:
    ctx = _ctx()
    compute_candidates(
        ctx,
        products=[_product(stock=3)],
        behavior_rows=[_behavior(sold=9)],
        settings=settings,
    )

    assert _slots(ctx) == ["restock"]
    change = ctx.candidate_actions[0].changes[0]
    assert change.field == "stock_quantity"
    assert change.before == "3"
    assert change.after == "13"  # ceil(9/10 × 14) = ceil(12.6)


def test_슬롯2_품절은_별도_슬롯이고_슬롯1과_겹치지_않는다(settings: Settings) -> None:
    """`06` §3.2 표대로면 재고 0 인 상품이 두 슬롯에 겹쳐 든다 — 배타로 가른다."""
    ctx = _ctx()
    compute_candidates(
        ctx,
        products=[_product(stock=0)],
        behavior_rows=[_behavior(sold=21)],
        settings=settings,
    )

    assert _slots(ctx) == ["stockout"]
    assert ctx.candidate_actions[0].changes[0].after == "30"  # ceil(21/10 × 14)


def test_재고_임계를_넘으면_후보가_아니다(settings: Settings) -> None:
    ctx = _ctx()
    compute_candidates(
        ctx, products=[_product(stock=6)], behavior_rows=[_behavior(sold=9)], settings=settings
    )
    assert ctx.candidate_actions == []


def test_판매_이력이_없으면_보충할_근거가_없다(settings: Settings) -> None:
    ctx = _ctx()
    compute_candidates(
        ctx, products=[_product(stock=0)], behavior_rows=[_behavior(sold=0)], settings=settings
    )
    assert ctx.candidate_actions == []


def test_미조회_판매수량은_0으로_읽지_않는다(settings: Settings) -> None:
    """`sales_quantity=None` 은 "미조회"다 — 0 으로 읽으면 후보 없음이 사실처럼 굳는다."""
    ctx = _ctx()
    compute_candidates(
        ctx, products=[_product(stock=0)], behavior_rows=[_behavior(sold=None)], settings=settings
    )
    assert ctx.candidate_actions == []


# ── 옵션 제외 ─────────────────────────────────────────────────────────────────


def _multi_option_product() -> SellerProductRow:
    return _product(
        stock=3,
        stocks=[
            SellerStockRow(optionId=1, optionName="S", quantity=1),
            SellerStockRow(optionId=2, optionName="M", quantity=2),
        ],
    )


@pytest.mark.parametrize("wire_mode", ["quantity", "stocks"])
def test_옵션_2개_이상_상품은_모드와_무관하게_재고_후보에서_빠진다(wire_mode: str) -> None:
    settings = Settings(_env_file=None, seller_stock_wire_mode=wire_mode)
    ctx = _ctx()
    compute_candidates(
        ctx,
        products=[_multi_option_product()],
        behavior_rows=[_behavior(sold=10)],
        settings=settings,
    )
    assert ctx.candidate_actions == []


def test_옵션_1개는_제외_대상이_아니다(settings: Settings) -> None:
    """옵션 없는 상품의 `stocks` 는 optionId 가 null 인 1행이거나 빈 목록이다."""
    ctx = _ctx()
    single = _product(stock=3, stocks=[SellerStockRow(optionId=7, optionName="단일", quantity=3)])
    compute_candidates(
        ctx, products=[single], behavior_rows=[_behavior(sold=10)], settings=settings
    )
    assert _slots(ctx) == ["restock"]


def test_옵션_상품도_숨김_해제는_막지_않는다(settings: Settings) -> None:
    """제외 사유는 `ProposedChange` 에 옵션 축이 없다는 것이다 — status 는 옵션이 없다."""
    ctx = _ctx()
    hidden = _product(
        stock=3,
        status="HIDDEN",
        stocks=[
            SellerStockRow(optionId=1, optionName="S", quantity=1),
            SellerStockRow(optionId=2, optionName="M", quantity=2),
        ],
    )
    compute_candidates(
        ctx, products=[hidden], behavior_rows=[_behavior(views=55, sold=3)], settings=settings
    )
    assert _slots(ctx) == ["unhide"]


# ── 슬롯 3 — 가격 롤백 ────────────────────────────────────────────────────────


def test_슬롯3_롤백가는_I15_oldValue_그대로다(settings: Settings) -> None:
    """임의 할인율(예: 10% 인하)은 근거 없는 수치다 — 실재하는 직전 가격만 쓴다."""
    ctx = _ctx(causes=[_price_cause()])
    logs = ProductChangeLogResult(rows=[_price_log(old="12900", new="15900", day="2026-08-01")])
    compute_candidates(ctx, products=[_product(price=15900)], change_logs=logs, settings=settings)

    assert _slots(ctx) == ["price_rollback"]
    change = ctx.candidate_actions[0].changes[0]
    assert (change.field, change.before, change.after) == ("price", "15900", "12900")
    assert ctx.candidate_actions[0].cause_ref == "sales_anomaly:2026-08-03"


def test_슬롯3_temporal_only_후보는_롤백_대상이_아니다(settings: Settings) -> None:
    cause = _price_cause()
    ctx = _ctx(causes=[cause.model_copy(update={"strength": "temporal_only"})])
    logs = ProductChangeLogResult(rows=[_price_log(old="12900", new="15900", day="2026-08-01")])
    compute_candidates(ctx, products=[_product(price=15900)], change_logs=logs, settings=settings)
    assert ctx.candidate_actions == []


def test_슬롯3_현재가가_이미_다르면_되돌릴_인상분이_없다(settings: Settings) -> None:
    ctx = _ctx(causes=[_price_cause()])
    logs = ProductChangeLogResult(rows=[_price_log(old="12900", new="15900", day="2026-08-01")])
    compute_candidates(ctx, products=[_product(price=14000)], change_logs=logs, settings=settings)
    assert ctx.candidate_actions == []


def test_슬롯3_이미_되돌린_이력이_있으면_중복_추천하지_않는다(settings: Settings) -> None:
    ctx = _ctx(causes=[_price_cause()])
    logs = ProductChangeLogResult(
        rows=[
            _price_log(old="12900", new="15900", day="2026-08-01"),
            _price_log(old="15900", new="12900", day="2026-08-04"),  # 롤백 시도
            _price_log(old="12900", new="15900", day="2026-08-06"),  # 재인상
        ]
    )
    compute_candidates(ctx, products=[_product(price=15900)], change_logs=logs, settings=settings)
    assert ctx.candidate_actions == []


def test_슬롯3_목록에_없는_상품은_후보가_아니다(settings: Settings) -> None:
    """실존 확인이 프롬프트 절차가 아니라 코드 보장이 된 자리다."""
    ctx = _ctx(causes=[_price_cause(product_id=999)])
    logs = ProductChangeLogResult(
        rows=[_price_log(product_id=999, old="12900", new="15900", day="2026-08-01")]
    )
    compute_candidates(ctx, products=[_product(price=15900)], change_logs=logs, settings=settings)
    assert ctx.candidate_actions == []


# ── 슬롯 4 — 숨김 해제 ────────────────────────────────────────────────────────


def test_슬롯4_숨김_상품에_조회_이력이_있으면_해제_후보다(settings: Settings) -> None:
    ctx = _ctx()
    compute_candidates(
        ctx,
        products=[_product(status="HIDDEN", stock=12)],
        behavior_rows=[_behavior(views=55, sold=0)],
        settings=settings,
    )

    assert _slots(ctx) == ["unhide"]
    change = ctx.candidate_actions[0].changes[0]
    assert (change.field, change.before, change.after) == ("status", "HIDDEN", "ON_SALE")


def test_슬롯4_아무도_찾지_않는_상품은_다시_노출할_근거가_없다(settings: Settings) -> None:
    ctx = _ctx()
    compute_candidates(
        ctx,
        products=[_product(status="HIDDEN", stock=12)],
        behavior_rows=[_behavior(views=0, sold=0)],
        settings=settings,
    )
    assert ctx.candidate_actions == []


# ── 전체 ─────────────────────────────────────────────────────────────────────


def test_promotion과_order_fulfillment는_생성하지_않는다(settings: Settings) -> None:
    """어휘는 남기되 쓰기를 중단한다(결정 40) — 실행 수단이 없는 추천은 되묻기를 부른다."""
    ctx = _ctx(causes=[_price_cause()])
    logs = ProductChangeLogResult(rows=[_price_log(old="12900", new="15900", day="2026-08-01")])
    compute_candidates(
        ctx,
        products=[
            _product(price=15900),
            _product(102, name="유자청", stock=0),
            _product(103, name="레몬청", status="HIDDEN"),
        ],
        behavior_rows=[_behavior(102, sold=21), _behavior(103, views=55, sold=3)],
        change_logs=logs,
        settings=settings,
    )

    assert ctx.candidate_actions
    types = {candidate.action_type for candidate in ctx.candidate_actions}
    assert types.isdisjoint({"promotion", "order_fulfillment", "description_update"})


def test_슬롯_우선순위대로_정렬된다(settings: Settings) -> None:
    ctx = _ctx(causes=[_price_cause()])
    logs = ProductChangeLogResult(rows=[_price_log(old="12900", new="15900", day="2026-08-01")])
    compute_candidates(
        ctx,
        products=[
            _product(price=15900),
            _product(102, name="유자청", stock=0),
            _product(103, name="레몬청", status="HIDDEN"),
            _product(104, name="소량", stock=3),
        ],
        behavior_rows=[
            _behavior(102, sold=21),
            _behavior(103, views=55, sold=3),
            _behavior(104, sold=9),
        ],
        change_logs=logs,
        settings=settings,
    )
    assert _slots(ctx) == ["price_rollback", "stockout", "restock", "unhide"]


def test_후보_상한을_넘기지_않는다() -> None:
    settings = Settings(_env_file=None, seller_recommend_candidate_max=2)
    ctx = _ctx()
    products = [_product(pid, name=f"상품{pid}", stock=0) for pid in range(101, 110)]
    behavior = [_behavior(pid, sold=10) for pid in range(101, 110)]
    compute_candidates(ctx, products=products, behavior_rows=behavior, settings=settings)
    assert len(ctx.candidate_actions) == 2
