"""app/agents/seller/low_sales.py 저성과 참고 문구 검증 (이슈 #659) — 실 LLM·HTTP 없음."""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.seller import low_sales as low_sales_module
from app.core.config import get_settings
from app.schemas.spring import BehaviorEventsResult, BehaviorProductRow
from app.services.spring_client import SpringUnavailableError


class _FakeSpring:
    """`get_events` 하나만 흉내 — 인자·응답을 테스트가 자유롭게 조립한다."""

    def __init__(self) -> None:
        self.result = BehaviorEventsResult(rows=[])
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    async def get_events(
        self, brand_id, from_=None, to=None, event_type=None, product_id=None, group_by=None
    ):
        self.calls.append((brand_id, from_, to, event_type, product_id, group_by))
        if self.error is not None:
            raise self.error
        return self.result


async def test_low_sales_note_empty_when_quantity_above_threshold() -> None:
    spring = _FakeSpring()
    spring.result = BehaviorEventsResult(
        rows=[BehaviorProductRow(productId=101, salesQuantity=50, counts={"productView": 200})]
    )

    note = await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    assert note == ""


async def test_low_sales_note_warns_when_quantity_at_or_below_threshold() -> None:
    spring = _FakeSpring()
    threshold = get_settings().seller_low_sales_quantity_threshold
    spring.result = BehaviorEventsResult(
        rows=[
            BehaviorProductRow(productId=101, salesQuantity=threshold, counts={"productView": 12})
        ]
    )

    note = await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    window_days = get_settings().seller_low_sales_window_days
    assert f"최근 {window_days}일" in note
    assert f"{threshold}개" in note
    assert "조회 12건" in note
    assert "재고나 가격" in note


async def test_low_sales_note_treats_missing_row_as_zero_sales() -> None:
    """product_id 에 대응하는 rows[] 행이 없으면(이벤트 0건) 판매량 0 으로 간주한다."""
    spring = _FakeSpring()
    spring.result = BehaviorEventsResult(rows=[])

    note = await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    assert "0개" in note


async def test_low_sales_note_ignores_rows_for_other_products() -> None:
    spring = _FakeSpring()
    spring.result = BehaviorEventsResult(
        rows=[BehaviorProductRow(productId=999, salesQuantity=50, counts={})]
    )

    note = await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    assert "0개" in note  # 101 행이 없어 0 으로 간주 — 999 행의 50 이 새어 들어가지 않는다


async def test_low_sales_note_soft_fails_on_spring_unavailable() -> None:
    """Spring 조회 실패는 조용히 무시한다 — 참고 문구 하나 때문에 안내를 막지 않는다."""
    spring = _FakeSpring()
    spring.error = SpringUnavailableError("conn refused")

    note = await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    assert note == ""


async def test_low_sales_note_disabled_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = get_settings().model_copy(update={"seller_low_sales_alert_enabled": False})
    monkeypatch.setattr(low_sales_module, "get_settings", lambda: disabled)
    spring = _FakeSpring()
    spring.result = BehaviorEventsResult(rows=[BehaviorProductRow(productId=101, salesQuantity=0)])

    note = await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    assert note == ""
    assert spring.calls == []  # 꺼져 있으면 조회 자체를 하지 않는다


async def test_low_sales_note_queries_requested_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """조회 기간이 오늘 기준 seller_low_sales_window_days 일이다(설정 주입 확인)."""
    overridden = get_settings().model_copy(update={"seller_low_sales_window_days": 14})
    monkeypatch.setattr(low_sales_module, "get_settings", lambda: overridden)
    spring = _FakeSpring()
    spring.result = BehaviorEventsResult(rows=[BehaviorProductRow(productId=101, salesQuantity=99)])

    await low_sales_module.low_sales_note(spring, brand_id=3, product_id=101)

    (brand_id, from_, to, event_type, product_id, group_by) = spring.calls[0]
    assert brand_id == 3
    assert product_id == 101
    assert group_by == "product"
    assert event_type is None
    assert (date.fromisoformat(to) - date.fromisoformat(from_)).days == 14
