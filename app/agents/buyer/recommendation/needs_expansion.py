"""목적·상황형 발화의 상품 전개 (이슈 #198, `DESIGN-NEEDS-EXPANSION-198.md`).

`"집들이 선물로 뭐 사갈까"` 처럼 **무엇을 살지 사용자가 말하지 않은 발화**를 구체 상품 목록으로
전개한다(정본 `SPEC-RECOMMEND-001` §5.1 `shopping_list` 분해). 이 단계가 없으면 매핑
(`category_mapping`)은 물건이 아닌 문자열(`'집들이 선물'`)을 입력으로 받아 canonical 을 낼 수 없다.

**감지는 코드가, 생성은 LLM 이** 담당한다(§3). `decompose` 프롬프트에 전개를 맡기는 방식은 실측
39회에서 1~2/3 확률로만 성립했고(규칙 강화·예시 확대 모두 실패), 근본 원인은 `fast` tier 한 호출에
intent·filters·cart·attributes 가 함께 얹혀 "무엇을 살지 떠올리기"라는 생성·추론 여력이 없는 것이다.

`case` 를 감지 신호로 쓰지 않는 이유(§4.1): `case` 는 전개와 **같은 LLM 호출의 산출물**이라 전개가
실패한 회차의 값을 신뢰할 근거가 없다(실측 `"부모님 환갑 선물"`: `case=[3,3,3]` 인데 `legs=[1,1,1]`).
LLM 산출물로 같은 LLM 산출물의 실패를 감지하는 것은 성립하지 않는다 — 결과를 직접 검사한다.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.buyer.recommendation.state import CategoryQuery


def detect_expansion_need(
    utterance: str,
    legs: Sequence[CategoryQuery],
    *,
    markers: Sequence[str],
) -> str | None:
    """전개가 필요하면 사유 문자열, 아니면 None (§4 D1~D3 — 결정적 판정).

    사유는 관측 로그(`needs_expansion_triggered.reason`)로 그대로 나가며, 분포를 보고 marker 목록을
    조정한다(설계 OPEN-1).

    - **D1 `no_legs`** — 신호(query) 있는 leg 이 하나도 없다.
    - **D2 `utterance_copy`** — leg 이 1개이고 그 query 가 발화와 서로 포함 관계이며 **목적 marker 로
      끝난다**(전개가 아니라 복사). marker 조건이 필요한 이유: `"청바지"` → `['청바지']` 도 복사지만
      **발화 자체가 상품명이라 올바른 leg** 이다. 문제는 복사 자체가 아니라 **목적 표현을 복사한 것**.
    - **D3 `purpose_marker`** — **모든** 신호 leg 의 query 가 목적 marker 로 **끝난다**.

    설계 원칙 — **재현율보다 정밀도**: 오탐하면 `"청바지"` 같은 정상 질의에 불필요한 LLM 호출과
    엉뚱한 확장이 붙는다. 미검출은 종전 동작(그대로 통과)이라 손해가 없다.
    """
    signals = [q.query.strip() for q in legs if q.query and q.query.strip()]
    if not signals:
        return "no_legs"

    def _is_purpose(text: str) -> bool:
        return any(text.endswith(m) for m in markers)

    utt = utterance.strip()
    # D2 는 leg 이 1개일 때만 본다 — 여럿이면 이미 전개가 일어난 것이므로 나머지를 버리지 않는다.
    # marker 조건이 함께 필요하다: `"청바지"` → `['청바지']` 도 복사지만 발화가 곧 상품명이라 올바른
    # leg 이다(테스트가 잡아낸 오탐). 복사 자체가 아니라 **목적 표현을 복사한 것**이 문제다.
    if len(signals) == 1 and utt and _is_purpose(signals[0]):
        if signals[0] in utt or utt in signals[0]:
            return "utterance_copy"

    # D3 는 `endswith` 로 판정한다. `in` 으로 보면 `'한우 선물세트'`·`'과일 선물세트'` 같은 **정당한
    # 상품명**이 marker `'선물'` 에 걸려 전부 오탐된다 — `'집들이 선물'.endswith('선물')` 은 True,
    # `'한우 선물세트'.endswith('선물')` 은 False 라 목적 표현만 잡힌다.
    # **모든** leg 이 목적 표현일 때만 트리거한다 — 전개는 legs 를 교체하므로(§6), 좋은 leg 이 섞여
    # 있을 때 트리거하면 그것까지 날아간다. 남은 목적 표현 leg 은 하류의 거리컷·택일이 흡수한다.
    if all(_is_purpose(s) for s in signals):
        return "purpose_marker"
    return None
