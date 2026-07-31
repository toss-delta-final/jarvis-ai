"""카테고리 LLM 택일 (이슈 #59) — **방식 A 메인 경로에서 조건부 호출**(#115 §4.4 배선).

**[2026-07-30 #115] 이 함수는 실전 트래픽에서 호출된다 — "미사용 예비"가 아니다.**
메인 매핑은 여전히 방식 A(`category_mapping.map_categories`: 추측→임베딩 최근접)이고 기본 LLM
호출은 0회지만, **최근접 판정이 애매한 leg 에만**(마진 ≤ `category_select_margin_max`, 턴당
`category_select_max_calls` 회 상한) `map_categories` 가 이 함수를 부른다(설계 §4.4). 이유는 거리컷
(§4)이 못 막는 구멍 — 추상 라벨('선물용품')은 거리가 가까운데(컷 통과) 뜻이 틀리고, 그때 1·2위가
사실상 동점이므로 후보 중 택일로 판정한다. `None`(맞는 후보 없음)은 그 leg 을 드롭시킨다.
(#59 당시엔 방식 B 전환 대비 "삭제 않고 예비 유지" 상태였고, 그 판단이 여기서 값을 했다.)

임베딩 top-k 로 좁힌 소수 후보(category_search)를 LLM 에 주고 발화에 가장 맞는 canonical
카테고리 1개를 고른다. 별도 2단계 폐쇄분류가 아니라 후보 내 단일 택일(1회 호출)이다.

가드:
- membership: LLM 이 후보에 없는 값을 내도(환각) 그대로 쓰지 않고 None 으로 떨어뜨린다.
- None 의 의미는 하나다 — **"후보 중 맞는 것이 없음"**(후보 0건·LLM 이 null·환각). 호출부는 그 leg 를
  드롭한다. **LLM 오류·JSON 파싱 실패는 None 이 아니라 `LLMError` 전파**(#115) — 후속 조치가
  반대(top-1 유지)인 "판정 실패"를 판정 결과와 섞지 않는다.
- non-blocking: 카테고리는 선택 필터라 매핑 실패가 추천 스트림을 끊어선 안 되는데, 그 보장은
  **호출부**(`map_categories` 의 `gather(return_exceptions=True)` + top-1 유지)가 담당한다.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.buyer.recommendation.state import extract_json
from app.core.llm import LLMClient, resolve_model_id

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
    settings=None,
    observer=None,
) -> str | None:
    """top-k 후보 중 발화에 맞는 카테고리 1개를 LLM 택일한다.

    `None` = **"후보 중 맞는 것이 없음"이라는 판정 결과**(후보 0건·LLM 이 null·환각 포함).
    호출부(§4.4)는 이걸 "그 leg 를 드롭하라"로 해석한다.
    **`LLMError` 는 삼키지 않고 전파한다(#115 §4.4)** — "판정을 못 했음"은 위 판정과 후속 조치가
    반대(top-1 유지)라서, None 으로 뭉개면 LLM 타임아웃이 카테고리를 삭제해버린다(거리컷 도입 이전
    보다 후퇴). non-blocking 보장은 호출부가 gather(return_exceptions=True) + top-1 유지로 담당한다.

    `observer` 기록이 **호출부(map_categories)가 아니라 여기** 있는 이유(PR #188 리뷰): 택일은
    주입형 seam(`select_category=`)이라 호출부에 두면 **구현 종류와 무관하게** 기록돼, LLM 을 쓰지
    않는 대체 구현(규칙 기반·캐시)에도 유령 모델 호출이 남는다. `needs_expansion._llm_expand` 와
    동일 원칙 — **모델을 실제로 부르는 쪽이 기록한다**. 후보 0건이면 호출 자체가 없어 기록도 없다.

    `settings` 는 `resolve_model_id`(tier → 모델 ID)에만 쓴다. 미주입이면 기록을 건너뛴다 —
    관측 누락이 판정 자체를 막아선 안 된다. 기록 중 예외가 나도 호출부의
    `gather(return_exceptions=True)` 가 흡수해 임베딩 top-1 로 퇴화한다(§4.4).
    """
    if not candidates:
        return None
    numbered = "\n".join(f"- {c}" for c in candidates)
    user = f"USER_MESSAGE: {query}\nCANDIDATES:\n{numbered}"
    # api-spec §6.3 — chat_request 의 model·토큰 합산에 이 조건부 호출을 싣는다. decompose·전개·
    # rerank 와 같이 **호출 전** 기록한다(실패한 호출도 비용이 발생한다).
    if observer is not None and settings is not None:
        observer.record_model_call(resolve_model_id(settings, tier))
    # LLMError(타임아웃·미구성·JSON 파싱 실패)는 전파 — 위 docstring 참조.
    raw = await llm.complete(system=_SYSTEM, user=user, tier=tier, max_tokens=200)
    choice = extract_json(raw).get("category")
    # membership 가드 — 후보에 정확히 존재하는 값만 채택, 그 외(환각·null)는 None.
    return choice if isinstance(choice, str) and choice in candidates else None
