"""`candidates` 생성기 — 추천 후보 4슬롯 (이슈 #597, `06-REPORT.md` §3).

추천도 코드가 만든다(결정 39). LLM 은 **선별·순서·문장화만** 한다. 이 배분의 효과가
크다 — 존재하지 않는 상품 추천 · 이미 시도된 변경 중복 · 실행 불가 초안 세 가지가
구조적으로 불가능해진다. 기존 `RECOMMEND_PROMPT` 가 절차 1~2번(`list_my_products` 실존
확인, `get_product_change_logs` 중복 회피)으로 프롬프트에 맡기던 일이 여기로 내려온다.

[v1 이 만들지 않는 것]
- `promotion` — 실행 수단이 코드베이스 전체 0건이다(결정 40). Literal 에는 남긴다
  (기존 저장 이력 역직렬화 보존) — **쓰기만 중단**한다.
- `order_fulfillment` — 어휘만 확정. `history.apply_recommendation` 이 `op="update"`
  고정이라 `op="ship"` 추천은 "N번 적용" 경로를 탈 수 없다(`06` §3.4).
- `description_update` — 근거가 상품 축 피처라 상품 트랙 후속.

[가격 조정을 롤백으로 한정하는 근거]
"10% 인하" 같은 임의 할인율은 근거 없는 수치다. D2 에 걸리는 게 정상이고, 걸리지 않게
하려고 finding 에 심으면 그게 환각의 세탁이다. 반면 "인상 직전 가격으로 되돌린다"는
I-15 에 실재하는 값이라 근거가 있다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

from app.agents.seller.sop.compute.causes import parse_event_date, parse_wire_int
from app.agents.seller.sop.context import (
    ActionCandidate,
    AnalysisContext,
    CandidateChange,
    CauseCandidate,
)
from app.core.config import Settings
from app.schemas.spring import BehaviorProductRow, ProductChangeLogResult, SellerProductRow

# 슬롯 우선순위 — 목록 순서가 곧 LLM 이 보는 순서다(최종 "N번"은 LLM 이 정한다).
# 되돌릴 변경(롤백) → 매출이 이미 0인 품절 → 임박한 재고 → 노출 복구.
_SLOT_ORDER: tuple[str, ...] = ("price_rollback", "stockout", "restock", "unhide")

_STATUS_HIDDEN = "HIDDEN"
_STATUS_ON_SALE = "ON_SALE"


def _is_multi_option(row: SellerProductRow) -> bool:
    """옵션별 재고 상품 판정 — `stocks` 에 `optionId` 가 있는 행이 2개 이상인가.

    ⚠️ **`seller_stock_wire_mode` 를 읽지 않는다.** `history._option_stock_blocker` 는
    `stocks` 모드에서만 도는데, 후보 생성기가 모드를 따라가면 운영 스위치를 켜는 날
    추천 목록이 조용히 줄어든다(`06` §0.2·§3.2). 판정 기준은 모드가 아니라 데이터다.

    제외하는 이유(#524): `ProposedChange` 에 `option_name` 이 없어 (a) 카드의 before 가
    옵션 **합계**로 표시돼 거짓을 보여주고 (b) [적용]을 누른 **뒤에야** 되묻기가 발생한다.
    HITL 은 승인 전에 거르는 장치이므로 후보 단계에서 뺀다.
    """
    return len([stock for stock in row.stocks if stock.option_id is not None]) >= 2


def _period_days(ctx: AnalysisContext) -> int:
    return max((ctx.period_to - ctx.period_from).days + 1, 1)


def _behavior_index(rows: Sequence[BehaviorProductRow]) -> dict[int, BehaviorProductRow]:
    return {row.product_id: row for row in rows}


def _sold(row: BehaviorProductRow | None) -> int:
    """기간 내 판매 **수량**. `sales_quantity=None` 은 "미조회"라 0 으로 읽지 않는다.

    `counts["purchaseComplete"]` 는 주문 **건수**이고 아이템 취소·반품을 포함해 규칙이
    다르다(`spring.BehaviorProductRow` 주) — 재고 산식에 섞으면 단위가 어긋난다.
    """
    if row is None or row.sales_quantity is None:
        return 0
    return max(int(row.sales_quantity), 0)


def _viewed(row: BehaviorProductRow | None) -> int:
    if row is None:
        return 0
    return max(int(row.counts.get("productView", 0)), 0)


def _restock_after(sold: int, days: int, settings: Settings) -> int:
    """일평균 판매 × 커버 일수, 올림. 최소 1 — 0 을 제안하면 변경이 되지 않는다."""
    daily = sold / days
    return max(math.ceil(daily * settings.seller_restock_cover_days), 1)


def _stock_basis(row: SellerProductRow, sold: int, days: int, settings: Settings) -> str:
    daily = sold / days
    head = (
        f"재고 {row.stock_quantity:,}건으로 임계 {settings.seller_stock_alert_threshold:,}건 이하"
        if row.stock_quantity > 0
        else "재고 0건(품절)"
    )
    return (
        f"{head}이고, 기간 {days}일 동안 {sold:,}건 팔렸습니다"
        f" (일평균 {daily:.1f}건 × 커버 {settings.seller_restock_cover_days}일)."
    )


def _stock_candidate(
    row: SellerProductRow,
    behavior: BehaviorProductRow | None,
    *,
    slot: str,
    days: int,
    settings: Settings,
) -> ActionCandidate | None:
    sold = _sold(behavior)
    if sold <= 0:
        return None  # 판매 이력이 없으면 보충할 근거가 없다
    after = _restock_after(sold, days, settings)
    return ActionCandidate(
        slot=slot,
        action_type="stock_adjust",
        product_id=row.product_id,
        product_name=row.name,
        changes=[
            CandidateChange(
                field="stock_quantity",
                before=f"{row.stock_quantity}",
                after=f"{after}",
            )
        ],
        basis=_stock_basis(row, sold, days, settings),
    )


def _price_rows(change_logs: ProductChangeLogResult | None) -> list[tuple[int, date, int, int]]:
    """I-15 PRICE 행 → `(product_id, 변경일, old, new)`. 숫자로 못 읽는 행은 버린다."""
    if change_logs is None:
        return []
    parsed: list[tuple[int, date, int, int]] = []
    for row in change_logs.rows:
        if (row.change_type or "").upper() != "PRICE":
            continue
        changed_at = parse_event_date(row.created_at)
        old = parse_wire_int(row.old_value)
        new = parse_wire_int(row.new_value)
        if changed_at is None or old is None or new is None:
            continue
        parsed.append((row.product_id, changed_at, old, new))
    return parsed


def _rollback_candidate(
    cause: CauseCandidate,
    row: SellerProductRow,
    price_rows: Sequence[tuple[int, date, int, int]],
) -> ActionCandidate | None:
    """규칙 1 후보가 correlated 로 성립한 상품의 가격 롤백 (`06` §3.2 슬롯 3)."""
    hike = next(
        (
            item
            for item in price_rows
            if item[0] == cause.product_id and item[1] == cause.event_at and item[3] > item[2]
        ),
        None,
    )
    if hike is None:
        return None
    _, changed_at, old, new = hike
    if row.price != new:
        return None  # 그 사이 다른 가격 조정이 있었다 — "인상분 되돌리기"가 성립하지 않는다
    if any(
        item[0] == cause.product_id and item[1] > changed_at and item[3] == old
        for item in price_rows
    ):
        return None  # 이미 한 번 되돌린 이력 — 중복 시도
    return ActionCandidate(
        slot="price_rollback",
        action_type="price_adjust",
        product_id=row.product_id,
        product_name=row.name,
        changes=[CandidateChange(field="price", before=f"{new}", after=f"{old}")],
        basis=(
            f"{cause.event_desc} 이후 하락이 관측됐습니다 (대상: {cause.target_desc})."
            f" 인상 직전 가격은 {old:,}원입니다."
        ),
        cause_ref=cause.target_key,
    )


def _unhide_candidate(
    row: SellerProductRow, behavior: BehaviorProductRow | None
) -> ActionCandidate | None:
    viewed = _viewed(behavior)
    sold = _sold(behavior)
    if viewed <= 0 and sold <= 0:
        return None  # 아무도 찾지 않는 상품을 다시 노출할 근거가 없다
    return ActionCandidate(
        slot="unhide",
        action_type="product_visibility",
        product_id=row.product_id,
        product_name=row.name,
        changes=[CandidateChange(field="status", before=row.status, after=_STATUS_ON_SALE)],
        basis=(f"현재 숨김 상태이나 기간 내 조회 {viewed:,}회 · 판매 {sold:,}건이 있습니다."),
    )


def compute_candidates(
    ctx: AnalysisContext,
    *,
    products: Sequence[SellerProductRow],
    behavior_rows: Sequence[BehaviorProductRow] = (),
    change_logs: ProductChangeLogResult | None = None,
    settings: Settings,
) -> None:
    """추천 후보 생성 — LLM 0회, Spring 0회, DB 0회.

    후보가 비면 `ctx.candidate_actions` 를 빈 목록으로 둔다 — 억지 추천 금지 규약이고,
    보고서 3부는 `render.render_candidate_block` 이 넣는 한 줄이 된다(`06` §3.5).
    """
    days = _period_days(ctx)
    behavior = _behavior_index(behavior_rows)
    price_rows = _price_rows(change_logs)
    by_id = {row.product_id: row for row in products}

    found: list[ActionCandidate] = []

    for row in products:
        stats = behavior.get(row.product_id)
        status = (row.status or _STATUS_ON_SALE).upper()

        if not _is_multi_option(row):
            # 슬롯 1·2 는 배타다 — `06` §3.2 표대로면 재고 0 인 상품이 두 슬롯에 겹쳐 든다.
            if row.stock_quantity == 0:
                slot = "stockout"
            elif 0 < row.stock_quantity <= settings.seller_stock_alert_threshold:
                slot = "restock"
            else:
                slot = ""
            if slot:
                candidate = _stock_candidate(row, stats, slot=slot, days=days, settings=settings)
                if candidate is not None:
                    found.append(candidate)

        if status == _STATUS_HIDDEN:
            candidate = _unhide_candidate(row, stats)
            if candidate is not None:
                found.append(candidate)

    for cause in ctx.causes:
        if cause.event_kind != "price_change" or cause.strength != "correlated":
            continue
        row = by_id.get(cause.product_id) if cause.product_id is not None else None
        if row is None:
            continue
        candidate = _rollback_candidate(cause, row, price_rows)
        if candidate is not None:
            found.append(candidate)

    seen: set[tuple[int, str]] = set()
    ordered = sorted(found, key=lambda item: (_SLOT_ORDER.index(item.slot), item.product_id))
    for candidate in ordered:
        if len(ctx.candidate_actions) >= settings.seller_recommend_candidate_max:
            break
        key = (candidate.product_id, candidate.slot)
        if key in seen:
            continue
        seen.add(key)
        ctx.candidate_actions.append(candidate)
