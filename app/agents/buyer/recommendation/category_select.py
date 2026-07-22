"""카테고리 LLM 택일 (이슈 #59) — **방식 B용 미사용 예비**(메인 경로 미배선).

⚠️ 메인 매핑 경로는 **방식 A**(`category_mapping.map_categories`: 추측→임베딩 최근접,
LLM 0회)를 채택했고 이 함수는 어디서도 호출되지 않는다(설계 §8 "삭제 않고 예비 유지").
방식 B(임베딩 top-k 후보 → LLM 택일)로 전환할 경우를 위한 예비 구현이라 유지만 한다 —
지금은 임베딩 top-1 nearest 를 그대로 canonical 로 확정한다(방식 A).

임베딩 top-k 로 좁힌 소수 후보(category_search)를 LLM 에 주고 발화에 가장 맞는 canonical
카테고리 1개를 고른다. 별도 2단계 폐쇄분류가 아니라 후보 내 단일 택일(1회 호출)이다.

가드:
- membership: LLM 이 후보에 없는 값을 내도(환각) 그대로 쓰지 않고 None 으로 떨어뜨린다.
- non-blocking: 후보가 없거나 LLM 오류·JSON 파싱 실패면 None(categoryName 생략) — 카테고리는
  선택 필터라 매핑 실패가 추천 스트림을 끊게 두지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.buyer.recommendation.state import extract_json
from app.core.llm import LLMClient, LLMError

_SYSTEM = """당신은 커머스 카테고리 매퍼입니다.
사용자 발화와 카테고리 후보 목록을 보고, 발화에 가장 잘 맞는 카테고리를 후보 중에서 정확히 하나 고르세요.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{"category": "<후보 문자열 그대로>" | null}
규칙:
- category 는 반드시 후보 목록에 있는 문자열과 글자까지 동일해야 합니다(임의 변형·새 카테고리 금지).
- 어느 후보도 발화와 맞지 않으면 null 을 주세요."""


async def select_category(
    llm: LLMClient,
    *,
    query: str,
    candidates: Sequence[str],
    tier: str,
) -> str | None:
    """top-k 후보 중 발화에 맞는 카테고리 1개를 LLM 택일한다. 없거나 실패하면 None."""
    if not candidates:
        return None
    numbered = "\n".join(f"- {c}" for c in candidates)
    user = f"USER_MESSAGE: {query}\nCANDIDATES:\n{numbered}"
    try:
        raw = await llm.complete(system=_SYSTEM, user=user, tier=tier, max_tokens=200)
        choice = extract_json(raw).get("category")
    except LLMError:
        # 카테고리 매핑 실패는 non-blocking — categoryName 생략(null)로 degrade 한다.
        return None
    # membership 가드 — 후보에 정확히 존재하는 값만 채택, 그 외(환각·null)는 None.
    return choice if isinstance(choice, str) and choice in candidates else None
