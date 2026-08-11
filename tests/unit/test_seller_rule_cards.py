"""sop/rule_cards.py — 논문 지식 카드 (이슈 #597, `12-EVAL` §2.2 · 결정 115).

이슈 완료 조건을 여기서 고정한다: **조건 걸림 / 안 걸림**. 그리고 이 층의 존재 이유인
"문장에 들어가는 수치는 ctx 값뿐"을 회귀로 못 박는다 — 그게 깨지면 D2 자동 통과라는
전제가 무너지고 카드가 곧 환각 경로가 된다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.seller.sop.context import AnalysisContext, ProductFlag, Segment
from app.agents.seller.sop.rule_cards import (
    MOE2003_ORDER_RATIO_MAX,
    MOE2003_VIEW_RATIO_MIN,
    RULE_CARDS,
    RuleCard,
    evaluate_rule_cards,
)
from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _ctx(**overrides) -> AnalysisContext:
    return AnalysisContext(
        worker="behavior",
        brand_id=7,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 10),
        **overrides,
    )


def _segment(label: str = "탐색형", *, views: float | None, orders: float | None) -> Segment:
    ratios: dict[str, float] = {}
    if views is not None:
        ratios["productViews"] = views
    if orders is not None:
        ratios["orderCount"] = orders
    return Segment(rule_label=label, display_label=label, size=120, ratio_to_mean=ratios)


# ── Moe 2003 탐색형 카드 ──────────────────────────────────────────────────────


def test_조회는_높고_구매는_낮은_군집에_카드가_걸린다(settings: Settings) -> None:
    ctx = _ctx(segments=[_segment(views=2.4, orders=0.3)])
    evaluate_rule_cards(ctx, settings=settings)

    assert len(ctx.rule_cards) == 1
    card = ctx.rule_cards[0]
    assert card.card_id == "moe2003_exploratory"
    assert card.scope == "segment"
    assert card.subject == "탐색형"
    assert card.strength == "empirical"  # 임계는 우리가 정했다 — 그 사실을 감추지 않는다
    assert "Moe (2003)" in card.citation


def test_문장에_들어가는_수치는_ctx의_배수뿐이다(settings: Settings) -> None:
    """`{}` 자리에 ctx 밖 값이 들어가면 D2 자동 통과 전제가 무너진다."""
    ctx = _ctx(segments=[_segment(views=2.4, orders=0.3)])
    evaluate_rule_cards(ctx, settings=settings)

    statement = ctx.rule_cards[0].statement
    assert "2.4배" in statement
    assert "0.3배" in statement
    assert "{" not in statement  # 포맷이 끝난 완성 문장이다


def test_임계_미달_군집에는_걸리지_않는다(settings: Settings) -> None:
    ctx = _ctx(
        segments=[
            _segment("충성형", views=MOE2003_VIEW_RATIO_MIN, orders=0.3),  # 초과가 아니라 동일
            _segment("탐색형", views=3.0, orders=MOE2003_ORDER_RATIO_MAX),
            _segment("휴면형", views=0.4, orders=0.2),
        ]
    )
    evaluate_rule_cards(ctx, settings=settings)
    assert ctx.rule_cards == []


def test_배수를_모르는_축은_0으로_읽지_않는다(settings: Settings) -> None:
    """`ratio_to_mean` 은 전체 평균 0 인 키를 아예 뺀다 — 없는 값을 0 으로 읽으면
    "구매 0.0배"가 사실처럼 서술된다."""
    ctx = _ctx(segments=[_segment(views=3.0, orders=None)])
    evaluate_rule_cards(ctx, settings=settings)
    assert ctx.rule_cards == []


def test_세그먼트가_없으면_아무것도_담지_않는다(settings: Settings) -> None:
    evaluate_rule_cards(ctx := _ctx(), settings=settings)
    assert ctx.rule_cards == []


# ── 주입 제어 ─────────────────────────────────────────────────────────────────


def test_킬스위치가_꺼져_있으면_평가하지_않는다() -> None:
    settings = Settings(_env_file=None, seller_rule_cards_enabled=False)
    ctx = _ctx(segments=[_segment(views=2.4, orders=0.3)])
    evaluate_rule_cards(ctx, settings=settings)
    assert ctx.rule_cards == []


def test_상한_0도_평가하지_않는다() -> None:
    settings = Settings(_env_file=None, seller_rule_cards_max=0)
    ctx = _ctx(segments=[_segment(views=2.4, orders=0.3)])
    evaluate_rule_cards(ctx, settings=settings)
    assert ctx.rule_cards == []


def test_상한을_넘기지_않는다() -> None:
    settings = Settings(_env_file=None, seller_rule_cards_max=2)
    ctx = _ctx(
        segments=[
            _segment("탐색형", views=2.4, orders=0.3),
            _segment("구매망설임형", views=3.1, orders=0.2),
            _segment("휴면형", views=2.6, orders=0.1),
            _segment("이탈위험형", views=4.0, orders=0.4),
        ]
    )
    evaluate_rule_cards(ctx, settings=settings)
    assert len(ctx.rule_cards) == 2


# ── 레지스트리 계약 ───────────────────────────────────────────────────────────


def test_카드_1장의_실패가_나머지를_멈추지_않는다(settings: Settings) -> None:
    """카드는 사람이 손으로 쓴 지식이라 필드 오타 하나가 파이프라인을 멈추면 안 된다."""

    def _boom(_target: object) -> bool:
        raise KeyError("없는 축")

    broken = RuleCard(
        id="broken",
        scope="segment",
        condition=_boom,
        statement="터진다",
        citation="—",
        strength="empirical",
        values=lambda _target: {},
    )
    ctx = _ctx(segments=[_segment(views=2.4, orders=0.3)])
    evaluate_rule_cards(ctx, settings=settings, cards=(broken, *RULE_CARDS))

    assert [card.card_id for card in ctx.rule_cards] == ["moe2003_exploratory"]


def test_상품_스코프_카드는_product_flags를_대상으로_돈다(settings: Settings) -> None:
    card = RuleCard(
        id="product_probe",
        scope="product",
        condition=lambda flag: flag.flag == "low_stock",
        statement="상품 플래그가 걸렸습니다.",
        citation="테스트",
        strength="definitional",
        values=lambda _flag: {},
    )
    ctx = _ctx(product_flags=[ProductFlag(product_id=101, flag="low_stock")])
    evaluate_rule_cards(ctx, settings=settings, cards=(card,))

    assert [entry.subject for entry in ctx.rule_cards] == ["상품 101"]


def test_브랜드_스코프_카드는_ctx를_대상으로_돌고_주어가_비어_있다(settings: Settings) -> None:
    card = RuleCard(
        id="brand_probe",
        scope="brand",
        condition=lambda ctx: bool(ctx.segments),
        statement="브랜드 단위 해석입니다.",
        citation="테스트",
        strength="definitional",
        values=lambda _ctx: {},
    )
    ctx = _ctx(segments=[_segment(views=1.0, orders=1.0)])
    evaluate_rule_cards(ctx, settings=settings, cards=(card,))

    assert [entry.subject for entry in ctx.rule_cards] == [""]


def test_등록된_카드는_전부_같은_계약을_지킨다() -> None:
    for card in RULE_CARDS:
        assert card.id and card.citation
        assert card.scope in ("segment", "product", "brand")
        assert card.strength in ("definitional", "empirical")
        assert callable(card.condition) and callable(card.values)
