"""rule cards — 논문 지식을 코드가 소유한다 (이슈 #597, `12-EVAL.md` §2.2 · 결정 115).

논문을 RAG 로 주입하지 않는다(결정 114). 그러면 *"연구에 따르면 장바구니 이탈은 배송비
노출 시점과 관련이 있는 것으로 알려져 있습니다"* 같은 문장이 나오는데, 이 문장은
**검증층을 전부 통과한다** — 숫자가 없어 D2 를, 헤지 표현이라 C1 을, 날짜가 없어
`period_grounded` 를 그냥 지나가고 judge 는 오히려 가점을 준다. 우리 데이터로 확인한 적
없는 문장이 그대로 판매자에게 간다.

대신 **사람이 논문에서 뽑은 "조건 + 문장 + 인용"을 코드 산출물로** 만든다.
`condition` 을 코드가 평가하고, LLM 은 **걸린 카드의 문장을 옮길 뿐** 해석하지 않는다.
`statement` 의 `{}` 에는 ctx 에 이미 있는 값만 들어가므로 D2 허용 집합 안이다.

`search_analysis_guide` 를 도구로 부활시키지 않는 이유가 여기 있다(결정 116) — 도구면
LLM 이 검색어를 정하고 결과를 자유 해석한다. 주입이면 조건을 코드가 평가한다.

[임계는 Settings 가 아니다]
Moe (2003) 이 "2배"라고 쓰지 않았다. 임계는 **우리가 정한 값에 논문 인용을 붙인 것**이고,
그 사실은 `strength="empirical"` 로 표면에 드러난다. 카드 정의의 일부이지 런타임
튜너블이 아니므로 모듈 상수로 둔다(`render.RATIO_MARK_FACTOR` 선례 — 표기·판정 규약은
Settings 가 아니다). 주입 자체의 on/off 와 상한만 Settings 가 쥔다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.agents.seller.sop.context import AnalysisContext, FiredRuleCard
from app.core.config import Settings

logger = logging.getLogger(__name__)

# Moe (2003) 방문 유형론 — 탐색 중심 방문(hedonic/exploratory browsing) 판정 임계.
# 군집 라벨 어휘("탐색형")가 애초에 이 유형론에서 왔다(`12-EVAL` §1.1).
MOE2003_VIEW_RATIO_MIN = 2.0
MOE2003_ORDER_RATIO_MAX = 0.5


@dataclass(frozen=True)
class RuleCard:
    """논문 지식 1장 — 조건(코드) + 문장 + 인용.

    `condition` 은 scope 에 맞는 대상 1건을 받아 bool 을 돌려준다. `values` 는 그 대상에서
    `statement.format()` 인자를 뽑는다 — **ctx 에 있는 값만** 넣어야 D2 를 통과한다.
    """

    id: str
    scope: Literal["segment", "product", "brand"]
    condition: Callable[[Any], bool]
    statement: str
    citation: str
    strength: Literal["definitional", "empirical"]
    values: Callable[[Any], dict[str, str]]


def _moe2003_exploratory_fired(segment: Any) -> bool:
    """조회는 평균을 크게 웃도는데 구매는 크게 밑도는 군집.

    `ratio_to_mean` 은 **전체 평균이 0 인 키를 아예 빼고** 만든다(`clustering._ratio_to_mean`
    — 0 나눗셈을 1.0 으로 위장하지 않는다). 키가 없으면 배수를 모르는 것이므로 걸리지
    않은 것으로 본다 — 없는 값을 0 으로 읽으면 "구매 0.0배"가 사실처럼 서술된다.
    """
    views = segment.ratio_to_mean.get("productViews")
    orders = segment.ratio_to_mean.get("orderCount")
    if views is None or orders is None:
        return False
    return views > MOE2003_VIEW_RATIO_MIN and orders < MOE2003_ORDER_RATIO_MAX


def _moe2003_exploratory_values(segment: Any) -> dict[str, str]:
    return {
        "views": f"{segment.ratio_to_mean['productViews']:.1f}",
        "orders": f"{segment.ratio_to_mean['orderCount']:.1f}",
    }


RULE_CARDS: tuple[RuleCard, ...] = (
    RuleCard(
        id="moe2003_exploratory",
        scope="segment",
        condition=_moe2003_exploratory_fired,
        statement=(
            "상품 조회가 브랜드 평균의 {views}배인데 주문은 {orders}배에 그칩니다."
            " 구매 목적이 뚜렷하지 않은 탐색 중심 방문에 가까운 패턴입니다."
        ),
        citation="Moe (2003), J. Consumer Psychology 13(1-2)",
        strength="empirical",
        values=_moe2003_exploratory_values,
    ),
)


def _subjects(ctx: AnalysisContext, scope: str) -> list[tuple[str, Any]]:
    """scope 별 평가 대상 — `(표시 이름, 대상 객체)`."""
    if scope == "segment":
        return [(segment.display_label or segment.rule_label, segment) for segment in ctx.segments]
    if scope == "product":
        return [(f"상품 {flag.product_id}", flag) for flag in ctx.product_flags]
    return [("", ctx)]


def evaluate_rule_cards(
    ctx: AnalysisContext,
    *,
    settings: Settings,
    cards: Sequence[RuleCard] | None = None,
) -> None:
    """조건이 걸린 카드를 `ctx.rule_cards` 에 담는다 — LLM 0회.

    걸린 게 없으면 **아무것도 담지 않는다**. 도구와 달리 실패를 판매자에게 안내하지
    않는다(`12-EVAL` §2.3) — 안 걸린 카드는 그냥 없는 것이다.

    카드 1장의 조건이 터져도 나머지는 계속 평가한다. 카드는 사람이 손으로 쓴 지식이라
    필드 오타 하나가 상주 파이프라인 전체를 멈추게 두면 안 된다.
    """
    if not settings.seller_rule_cards_enabled or settings.seller_rule_cards_max <= 0:
        return
    registry = RULE_CARDS if cards is None else cards

    fired: list[FiredRuleCard] = []
    for card in registry:
        for subject, target in _subjects(ctx, card.scope):
            try:
                if not card.condition(target):
                    continue
                statement = card.statement.format(**card.values(target))
            except Exception as exc:  # noqa: BLE001 — 카드 1장의 실패를 흡수한다
                logger.warning(
                    "rule_card 평가 실패(card=%s, subject=%s): %r", card.id, subject, exc
                )
                continue
            fired.append(
                FiredRuleCard(
                    card_id=card.id,
                    scope=card.scope,
                    subject=subject,
                    statement=statement,
                    citation=card.citation,
                    strength=card.strength,
                )
            )

    fired.sort(key=lambda item: (item.scope, item.card_id, item.subject))
    ctx.rule_cards.extend(fired[: settings.seller_rule_cards_max])
