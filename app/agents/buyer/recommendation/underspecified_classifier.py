"""첫 턴의 과소지정 여부를 decompose와 분리해 판정한다 (#463)."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.buyer.recommendation.state import extract_json
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.llm import resolve_model_id
from app.core.tracing import trace_span

logger = logging.getLogger(__name__)

_SYSTEM = """쇼핑 대화에서 사용자가 지금 무엇을 살지 지목했는지만 판정하세요.
아래 JSON 만 출력하세요(설명·코드펜스 금지): {"underspecified": true | false}
- true: 상품 종류·용도·목적·브랜드·색상 같은 상품 단서 없이 가격·수량 같은 조건만 있거나
  "아무거나"처럼 무엇을 살지 말하지 않은 발화입니다. 예: "5만원 이하 아무거나", "3개 필요해".
- false: 찾는 상품의 종류·용도·목적·브랜드·색상 중 하나라도 말했습니다. 예: "무선 이어폰",
  "캠핑 갈 때 쓸 거", "나이키 운동화", "검정 가방".
- false: 평점 조건만 있는 발화도 false 입니다. 현재 재질문 계약은 평점 필터를 보수적으로
  유지하므로, 이 호출이 그 계약을 넓혀서는 안 됩니다.
- 확신이 없으면 false 입니다."""


async def classify_underspecified(
    llm: Any, *, message: str, settings: Any, observer: Any | None = None
) -> bool | None:
    """과소지정이면 True, 특정 상품 신호면 False, 호출 실패면 None으로 퇴화한다."""
    if observer is not None:
        observer.record_model_call(
            resolve_model_id(settings, settings.underspecified_classifier_tier)
        )
    try:
        with trace_span(
            "llm.underspecified_classifier",
            "llm",
            {"model": resolve_model_id(settings, settings.underspecified_classifier_tier)},
        ):
            raw = await llm.complete(
                system=_SYSTEM,
                user=f"사용자 발화: {message}",
                tier=settings.underspecified_classifier_tier,
                max_tokens=settings.underspecified_classifier_max_tokens,
            )
        value = extract_json(raw).get("underspecified")
    except Exception as exc:  # noqa: BLE001 - 보조 판정 실패가 원 요청을 죽이지 않게 한다.
        logger.warning("underspecified_classifier_failed", extra={"reason": str(exc)})
        return None
    if value is True:
        return True
    if value is False:
        return False
    logger.info("underspecified_classifier_unparsed", extra={"type": type(value).__name__})
    return False


def apply_underspecified_classification(
    decision: RouteDecision, *, message: str, verdict: bool | None
) -> bool:
    """확정된 첫-턴 과소지정 신호만 #430의 후처리 상태로 복원한다.

    decompose가 화면·카테고리 맥락을 보고 임의 semantic/category 신호를 낸 경우에도, 전용
    분류기가 ``True``라면 그것은 이번 첫 무맥락 발화의 상품 단서가 아니다. #430에서 빈
    ``semanticQuery``가 만들던 원문 폴백과 같은 상태로 되돌린다. ``False``·``None`` 및
    비추천 intent는 보수적으로 원본 결정을 그대로 둔다.
    """
    if verdict is not True or decision.intent != "recommend":
        return False

    decision.filters.semantic_query = message
    decision.filters.category = None
    decision.filters.brand = None
    decision.filters.keyword = None
    decision.filters.color = None
    decision.filters.attr_conditions = None
    decision.category_queries = []
    decision.category_leg_injected = False
    decision.category_legs = []
    decision.category_expanded = False
    decision.category_legs_restored = False
    decision.repurchase_products = []
    decision.revert_categories = []
    decision.case = 2
    decision.semantic_query_is_fallback = True
    return True
