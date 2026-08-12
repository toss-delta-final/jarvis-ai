"""첫 턴의 과소지정 여부를 decompose와 분리해 판정한다 (#463)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.buyer.recommendation.state import extract_json
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.llm import resolve_model_id
from app.core.tracing import trace_span

logger = logging.getLogger(__name__)


# 이 식은 판정기가 아니다. 상품·용도 신호가 분명한 첫 턴에 보조 smart 호출을 열지 않기 위한
# 비용 상한이며, 실제 과소지정 여부는 반드시 아래 LLM 분류기가 확정한다. 가격/예산-only와
# "아무거나" 계열은 넓게 포함하되, 평점-only는 #336의 보수 경계를 따라 제외한다.
_LOW_INFORMATION_MARKERS = re.compile(
    r"아무거나|뭐든|뭐라도|그냥\s*추천|좋은\s*거\s*(?:없|추천)|하나\s*골라"
    r"|(?:가격대|예산|총\s*\d|\d+(?:\.\d+)?\s*만\s*원|\d+\s*원)",
    re.IGNORECASE,
)
_RATING_MARKERS = re.compile(r"평점|별점|리뷰")

_SYSTEM = """쇼핑 대화에서 사용자가 지금 무엇을 살지 지목했는지만 판정하세요.
아래 JSON 만 출력하세요(설명·코드펜스 금지): {"underspecified": true | false}
- true: 상품 종류·용도·목적·브랜드·색상 같은 상품 단서 없이 가격·수량 같은 조건만 있거나
  "아무거나"처럼 무엇을 살지 말하지 않은 발화입니다. 예: "5만원 이하 아무거나", "3개 필요해".
- false: 찾는 상품의 종류·용도·목적·브랜드·색상 중 하나라도 말했습니다. 예: "무선 이어폰",
  "캠핑 갈 때 쓸 거", "나이키 운동화", "검정 가방".
- false: 평점 조건만 있는 발화도 false 입니다. 현재 재질문 계약은 평점 필터를 보수적으로
  유지하므로, 이 호출이 그 계약을 넓혀서는 안 됩니다.
- 확신이 없으면 false 입니다."""


def could_be_underspecified_message(message: str) -> bool:
    """보조 분류기를 열 만한 저정보량 첫 턴인지 보수적으로 거른다.

    이 빠른 게이트가 ``False``여도 추천 결과를 바꾸지 않는다. 호출을 생략할 뿐이며, ``True``인
    경우에만 LLM의 true 판정이 #430 fallback을 복원한다.
    """
    return bool(_LOW_INFORMATION_MARKERS.search(message)) and not bool(
        _RATING_MARKERS.search(message)
    )


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
