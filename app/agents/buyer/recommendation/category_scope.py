"""카테고리 범위 해제 분류기 — "이번 발화가 상품 종류를 놓겠다는 말인가" (이슈 #84).

멀티턴 승계 가드는 "이번 턴 카테고리 신호 없음"을 리파인으로 읽어, `"5만원 이하 아무거나"` 같은
**카테고리-무관 리셋** 발화까지 직전 카테고리(이어폰)로 좁혔다. 그 판정을 `decompose` 프롬프트
안의 필드(`categoryAction`)로 받으려 했으나 **fast 티어에서 작동하지 않았다.**

실측(`evals/intent_probe`, 픽스처 v2 앵커 b, N=8, 리셋 기대 32건 중 LLM 이 `clear` 를 낸 횟수):

| 후보 | 바꾼 것 | clear/32 | 부작용 |
|---|---|---|---|
| 인라인 필드(초판) | — | 0 | — |
| 불릿·스켈레톤을 categoryQueries 앞으로 | 위치 | 0 | — |
| + categoryQueries 불릿에 clear 예외 문장 | 충돌 제거 | 0 | — |
| + "먼저 clear 부터 판정" 강화 문면 | 문면 | 1 | replace 기대가 clear 로 6/24 오염 |
| 이분 boolean `scopeFree` 로 교체 | 필드 모양 | 6 | 오탐 0 |
| 스켈레톤 최상단 + 맨 끝 검산 불릿 | 최신성 | 21 | carry→clear 3/32 · replace 10/24 붕괴 |

같은 프롬프트를 `--tier smart` 로 재면 `clear 32/32`·`carry 32/32`·`replace 24/24` 로 완벽했다 —
**문면 문제가 아니라 `gpt-5-nano` 가 133줄 `_SYSTEM` 안에서 이 판정을 못 한다는 역량 한계**다.
반대로 **짧고 초점이 하나뿐인 전용 호출은 fast 에서도 완벽**했다(독립 3회 × 88 표본:
`clear 32/32` · carry 오탐 0/32 · replace 오탐 0/24). `needs_expansion` 이 같은 이유로 전용
호출이 된 것과 정확히 같은 구조다(그쪽 §2: "한 호출에 6가지 작업이 얹히면 여력이 없다").

인라인 `categoryAction` 은 **지우지 않는다** — smart 티어에서 32/32 로 검증됐고 이 분류기와
`or` 로 합류하므로(`resolve_category_action`), 티어가 올라가면 자동으로 이득이다. 지금 fast 에서는
사실상 잠들어 있을 뿐이다.
"""

from __future__ import annotations

import logging

from app.agents.buyer.recommendation.state import extract_json
from app.core.llm import resolve_model_id
from app.core.tracing import trace_span

logger = logging.getLogger(__name__)

# ⚠️ 이 문면이 위 32/32 를 낸 **실측 대상**이다. 동의어로 바꾸면 그 측정이 무효가 된다 —
# 문구를 고치려면 `evals/intent_probe` 로 다시 재고 근거를 남길 것(#240 교훈).
_SYSTEM = """쇼핑 대화에서 사용자가 방금 한 말 하나만 봅니다.
직전까지 좁혀 둔 **상품 종류(카테고리)** 를 사용자가 이번 말에서 **더 이상 가리지 않겠다**고 했는지 판정하세요.
아래 JSON 만 출력하세요(설명·코드펜스 금지): {"scopeFree": true | false}
- true = 종류를 놓겠다는 말입니다. 예: "아무거나", "종류 상관없이", "가리지 않고", "뭐든", "전부 다 보여줘",
  "5만원 이하 아무거나".
- false = 종류는 그대로 두고 **가격·브랜드·평점·배송** 같은 조건만 다듬는 말입니다.
  예: "더 저렴한 걸로", "다른 브랜드로", "평점 4.5 이상인 걸로", "배송 빠른 거".
- **가격·평점 조건이 함께 있어도** "아무거나"·"상관없이" 가 있으면 true 입니다 — 조건은 종류와 별개입니다.
  예: "5만원 이하 아무거나 있어?" → true, "카테고리 상관없이 3만원대로 골라줘" → true.
- false = **다른 상품 종류를 새로 지목**하는 말입니다. 예: "이번엔 노트북", "청바지 추천해줘", "운동화로 바꿔줘".
  (종류를 바꾸는 것은 놓는 것이 아닙니다.)
확신이 없으면 false 입니다."""


async def classify_category_scope(
    llm, *, message: str, prior_category: str, settings, observer=None
) -> bool | None:
    """이번 발화가 **상품 종류를 놓겠다**는 말인가. 판정 못 하면 None(=신호 없음).

    **어떤 예외도 밖으로 내보내지 않는다.** 이 호출은 보조 신호이고 실패하면 오늘 동작(carry)으로
    돌아갈 뿐인데, 여기서 예외가 올라가면 무관한 추천 턴이 통째로 죽는다 — `_map_or_empty`(매핑)·
    완화 칩 조회와 같은 degrade 원칙이다. `CancelledError` 는 `BaseException` 이라
    `except Exception` 에 걸리지 않고 그대로 전파된다(그래프가 태스크를 취소할 수 있어야 한다).

    `scopeFree` 는 **`is True`/`is False` 만** 인정하고 그 밖(누락·문자열 `"true"`·숫자)은 None 이다.
    `RouteDecision.scoped_to_previous` 의 `is True` 규약과 같은 이유 — 애매한 산출을 강한 쪽으로
    해석하면 사용자가 좁혀 둔 카테고리가 조용히 풀린다.

    모델 호출 기록은 **여기서** 한다(api-spec §6.3 — 실제로 부르는 쪽이 기록한다). 호출 **전에**
    기록하는 것도 형제 경로(`needs_expansion`·`decompose`)와 같다: 실패한 호출도 비용이 든다.
    """
    if observer is not None:
        observer.record_model_call(resolve_model_id(settings, settings.category_scope_tier))
    try:
        with trace_span(
            "llm.category_scope",
            "llm",
            {"model": resolve_model_id(settings, settings.category_scope_tier)},
        ):
            raw = await llm.complete(
                system=_SYSTEM,
                user=f"직전 상품 종류: {prior_category}\n사용자 발화: {message}",
                tier=settings.category_scope_tier,
                max_tokens=settings.category_scope_max_tokens,
            )
        value = extract_json(raw).get("scopeFree")
    except Exception as exc:  # noqa: BLE001 - 보조 신호의 실패가 턴을 죽이지 않게(degrade)
        logger.warning("category_scope_classify_failed", extra={"reason": str(exc)})
        return None
    if value is True:
        return True
    if value is False:
        return False
    logger.info("category_scope_unparsed", extra={"type": type(value).__name__})
    return None
