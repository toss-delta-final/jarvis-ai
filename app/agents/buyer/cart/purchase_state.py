"""구매 가능 상태 안내 문구 — 상태를 문구로 가르는 결정론적 매핑 (api-spec §4.9·§4.16, 이슈 #310).

장바구니 조회·삭제 되물음·찜 되물음 세 흐름이 **같은 라벨**을 쓰므로 한 곳에 둔다. 문구를
각 모듈에 흩어 두면 한쪽만 고쳐져 같은 상품이 "품절"과 "재고없음"으로 갈리는 드리프트가
생긴다 — 순환 임포트 회피로 `identity.py` 를 뺀 것과 같은 이유의 분리다.

**LLM 을 태우지 않는다.** 상태→문구는 유한한 분기라 프롬프트로 만들 이유가 없고, 프롬프트로
만들면 단위 테스트로 고정할 수도 없다(REQ-CART-037). 이 모듈의 함수는 전부 순수 함수다.

`AVAILABLE` 과 미수신(`None`)은 **둘 다 빈 문자열**로 떨어진다. 앞은 "살 수 있다"를 굳이 말하지
않는 표현 정책이고, 뒤는 모름을 주장으로 바꾸지 않겠다는 것이다(#310 기본값 재검토) — 근거는
다르지만 사용자에게 보이는 결과가 같아 한 분기로 합친다.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from app.schemas.spring import PURCHASE_STATE_LABEL, PurchaseState

# 상태별 안내 문장 — 사용자가 취해야 할 **다음 행동**을 담는다. 품절과 판매 종료를 가르는 이유가
# 곧 이것이다(품절은 기다리면 되고, 판매 종료는 돌아오지 않아 빼는 편이 낫다). 목록 줄마다
# 반복하지 않고 문단 끝에 상태당 한 번만 싣는다 — 항목이 여럿이면 같은 문장이 N번 나온다.
#
# `PURCHASE_STATE_LABEL`(전사 매핑, 테스트로 강제)과 달리 **이쪽은 의도적 부분 매핑**이다 —
# `AVAILABLE` 에는 권할 행동이 없어 항목 자체가 없어야 정상이다. 새 상태가 추가됐을 때 라벨은
# 반드시 필요하지만(그래서 전사), 안내 문장은 그 상태가 행동을 요구할 때만 넣는다. `_ADVICE_ORDER`
# 도 같은 이유로 부분 목록이며, 여기 없는 상태는 라벨만 붙고 안내 줄은 생기지 않는다.
_STATE_ADVICE: Mapping[PurchaseState, str] = MappingProxyType(
    {
        "SOLD_OUT": "품절된 상품은 재입고되면 다시 담을 수 있어요.",
        "HIDDEN": "판매가 종료된 상품은 빼는 걸 추천드려요",
    }
)

# 안내 순서 — 품절을 먼저 둔다(되돌릴 수 있는 상태가 먼저, 되돌릴 수 없는 상태가 나중).
_ADVICE_ORDER: tuple[PurchaseState, ...] = ("SOLD_OUT", "HIDDEN")


def state_suffix(state: PurchaseState | None) -> str:
    """목록 한 줄 뒤에 붙일 라벨 — `" (품절)"` 처럼 앞 공백을 포함해 돌려준다.

    `AVAILABLE`·미수신(`None`)·계약 밖 값은 빈 문자열이라 호출부가 조건 분기 없이 이어붙일 수
    있다. 괄호 표기는 `remove.py::_display_name_with_option` 이 못박은 규약("새 형식을 발명하지
    않는다")을 따른 것이다.
    """
    label = PURCHASE_STATE_LABEL.get(state, "") if state is not None else ""
    return f" ({label})" if label else ""


def state_advice_lines(
    states: list[PurchaseState | None], hidden_example: str | None = None
) -> list[str]:
    """목록에 실제로 등장한 상태에 대해서만 안내 줄을 만든다(상태당 최대 1줄).

    `hidden_example` 은 판매 종료 항목 중 첫 번째의 표시명이다. 이슈 #310 의 목표 문구는
    *"뺄 상품을 추천해드릴까요?"* 라는 제안형인데, **그대로 쓰면 답할 수 없는 질문이 된다** —
    이 되물음은 상태를 저장하지 않아 사용자가 "응"이라 답해도 `classify_cart_utterance` 가 삭제
    표지를 찾지 못해 `cart_add` 로 샌다(`remove.py::_unresolved_notice` docstring 이 이미 문서화한
    함정). 그래서 제안 어투는 살리되 `remove.py` 규약대로 **예시 발화로 유도**한다.
    """
    present = {state for state in states if state is not None}
    lines: list[str] = []
    for state in _ADVICE_ORDER:
        if state not in present:
            continue
        advice = _STATE_ADVICE[state]
        if state == "HIDDEN":
            advice = (
                f"{advice} — '{hidden_example} 빼줘'처럼 말씀해 주세요."
                if hidden_example
                else f"{advice}."
            )
        lines.append(advice)
    return lines
