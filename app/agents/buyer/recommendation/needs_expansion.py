"""목적·상황형 발화의 상품 전개 (이슈 #198, `DESIGN-NEEDS-EXPANSION-198.md`).

`"집들이 선물로 뭐 사갈까"` 처럼 **무엇을 살지 사용자가 말하지 않은 발화**를 구체 상품 목록으로
전개한다(정본 `SPEC-RECOMMEND-001` §5.1 `shopping_list` 분해). 이 단계가 없으면 매핑
(`category_mapping`)은 물건이 아닌 문자열(`'집들이 선물'`)을 입력으로 받아 canonical 을 낼 수 없다.

**감지는 코드가, 생성은 LLM 이** 담당한다(§3). `decompose` 프롬프트에 전개를 맡기는 방식은 실측
39회에서 1~2/3 확률로만 성립했고(규칙 강화·예시 확대 모두 실패), 근본 원인은 `fast` tier 한 호출에
intent·filters·cart·attributes 가 함께 얹혀 "무엇을 살지 떠올리기"라는 생성·추론 여력이 없는 것이다.

`case` 의 역할은 **트리거가 아니라 게이트**다 — 요구되는 신뢰도가 다르다.

- **트리거로는 쓰지 않는다**(§4.1): `case` 는 전개와 **같은 LLM 호출의 산출물**이라 전개가 실패한
  회차의 값을 신뢰할 근거가 없다(실측 `"부모님 환갑 선물"`: `case=[3,3,3]` 인데 `legs=[1,1,1]`).
  "전개가 **안 됐다**"는 사실은 결과를 직접 검사해야 알 수 있다(D1~D3).
- **게이트로는 쓴다**(§4.2): `case != 3` 이면 어떤 규칙도 발동하지 않는다. case 2
  (`"5만원 이하 아무거나"`·`"평점 높은 거 보여줘"`)는 **의도적으로 카테고리 무관**이라 좁히면 안 되는데
  (#22 가 테스트로 고정·#162 가 개선할 경로), leg 유무로도 marker 로도 case 3 과 구분되지 않는다.
  게이트는 case 1/2 를 **배제**하는 데만 쓰이고 그 판정은 실측이 뒷받침한다(목적형 15/15 `case=3`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

from app.agents.buyer.recommendation.state import CategoryQuery, extract_json
from app.core.llm import LLMError

logger = logging.getLogger(__name__)

ExpandFn = Callable[..., Awaitable[list[str]]]

_SYSTEM = """당신은 커머스 어시스턴트의 쇼핑 목록 작성기입니다.
사용자가 **무엇을 살지 말하지 않고 목적·상황만** 말했습니다. 그 목적에 맞게 **실제로 살 만한 구체
상품**을 떠올려 나열하세요.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{"items": ["<상품명>", "<상품명>", ...]}
규칙:
- 각 항목은 **가게에서 그 이름으로 집어올 수 있는 물건**이어야 합니다.
  ❌ "집들이 선물", "준비물", "생활용품", "선물세트 아이템" — 목적·매장 코너 이름이라 물건이 아닙니다.
- 사용자의 표현을 그대로 되풀이하거나 접두로 붙이지 마세요("집들이 선물 캔들" ❌ → "캔들" ⭕).
- 가격·평가 수식어("가성비", "저렴한")와 상황 설명("~할 때 쓰는")은 빼고 **상품명만** 쓰세요.
- 서로 다른 종류로 2~5개. 하나뿐이면 다시 생각해 최소 2개를 채우세요.
- 방법 예시(형식만 참고하세요 — **아래 상품을 그대로 쓰지 말고 이번 발화에 맞게 새로 떠올릴 것**):
  USER_MESSAGE: 이사 갈 때 필요한 것들
  {"items": ["행거", "수납 박스", "커튼", "이불"]}"""


def detect_expansion_need(
    utterance: str,
    legs: Sequence[CategoryQuery],
    *,
    markers: Sequence[str],
    case: int,
) -> str | None:
    """전개가 필요하면 사유 문자열, 아니면 None (§4 D1~D3 — 결정적 판정).

    사유는 관측 로그(`needs_expansion_triggered.reason`)로 그대로 나가며, 분포를 보고 marker 목록을
    조정한다(설계 OPEN-1).

    **`case != 3` 이면 어떤 규칙도 발동하지 않는다**(§4.2 게이트 — case 2 무필터 의도 보호).

    - **D1 `no_legs`** — 신호 있는 leg 이 하나도 없다.
    - **D2 `utterance_copy`** — leg 이 1개이고 그 query 가 발화와 서로 포함 관계이며 **목적 marker 로
      끝난다**(전개가 아니라 복사). marker 조건이 필요한 이유: `"청바지"` → `['청바지']` 도 복사지만
      **발화 자체가 상품명이라 올바른 leg** 이다. 문제는 복사 자체가 아니라 **목적 표현을 복사한 것**.
    - **D3 `purpose_marker`** — **모든** 신호 leg 의 query 가 목적 marker 로 **끝난다**.

    "신호 있는 leg" 은 저장소 규약대로 **`raw_category or query`** 로 본다(PR #203 리뷰) —
    `decompose._parse_category_queries`·graph 멀티턴 승계 판정과 동일. `category` 만 채우고
    `query=null` 인 leg 은 **이미 올바른 카테고리 신호**이므로 D1 로 오탐해 교체하면 유실된다.

    설계 원칙 — **재현율보다 정밀도**: 오탐하면 `"청바지"` 같은 정상 질의에 불필요한 LLM 호출과
    엉뚱한 확장이 붙는다. 미검출은 종전 동작(그대로 통과)이라 손해가 없다.
    """
    # §4.2 게이트 — case 3(목적·상황형)이 아니면 어떤 규칙도 발동하지 않는다.
    # case 2("5만원 이하 아무거나"·"평점 높은 거 보여줘")는 **의도적으로 카테고리 무관**이라 좁히면
    # 안 되는데(#22 가 테스트로 고정·#162 가 개선할 경로), leg 유무(D1)로도 marker(D2·D3)로도
    # case 3 과 구분되지 않는다 — `['평점 높은 거']` 는 marker `'거'` 로 끝나 D3 에 걸린다.
    # 실패 방향이 비대칭이라 게이트를 전 규칙에 건다: 과하게 막으면 목적 표현 leg 이 남아 하류
    # 거리컷이 드롭해 무필터로 흡수되지만(안전), 새면 지어낸 카테고리로 검색이 좁혀진다(유해).
    if case != 3:
        return None

    signals = [
        q
        for q in legs
        if (q.raw_category and q.raw_category.strip()) or (q.query and q.query.strip())
    ]
    if not signals:
        return "no_legs"

    def _is_purpose(text: str) -> bool:
        return any(text.endswith(m) for m in markers)

    # D2·D3 의 텍스트 판정은 query 만 대상이다 — raw 만 있는 leg 은 검색 키워드가 없어 "목적 표현
    # 복사" 판정 대상이 아니고, 그 leg 이 섞이면 아래 D3 의 `all()` 을 깨서 전개를 막는다(의도).
    queries = [q.query.strip() for q in signals if q.query and q.query.strip()]
    utt = utterance.strip()
    # D2 는 leg 이 1개일 때만 본다 — 여럿이면 이미 전개가 일어난 것이므로 나머지를 버리지 않는다.
    # marker 조건이 함께 필요하다: `"청바지"` → `['청바지']` 도 복사지만 발화가 곧 상품명이라 올바른
    # leg 이다(테스트가 잡아낸 오탐). 복사 자체가 아니라 **목적 표현을 복사한 것**이 문제다.
    if len(signals) == 1 and len(queries) == 1 and utt and _is_purpose(queries[0]):
        if queries[0] in utt or utt in queries[0]:
            return "utterance_copy"

    # D3 는 `endswith` 로 판정한다. `in` 으로 보면 `'한우 선물세트'`·`'과일 선물세트'` 같은 **정당한
    # 상품명**이 marker `'선물'` 에 걸려 전부 오탐된다 — `'집들이 선물'.endswith('선물')` 은 True,
    # `'한우 선물세트'.endswith('선물')` 은 False 라 목적 표현만 잡힌다.
    # **모든** 신호 leg 이 목적 표현일 때만 트리거한다 — 전개는 legs 를 교체하므로(§6), 좋은 leg 이
    # 섞여 있을 때 트리거하면 그것까지 날아간다(raw 만 있는 leg 도 여기서 걸러 전개를 막는다).
    # 남은 목적 표현 leg 은 하류의 거리컷·택일이 흡수한다.
    if len(queries) == len(signals) and all(_is_purpose(q) for q in queries):
        return "purpose_marker"
    return None


async def _llm_expand(utterance: str, *, llm, settings) -> list[str]:
    """전용 LLM 호출 1회로 구체 상품명 목록을 만든다 (방식 A, §5).

    `decompose` 와 분리한 이유(§2): 한 호출에 intent·filters·cart·attributes 가 함께 얹히면
    "무엇을 살지 떠올리기"라는 생성·추론 여력이 없어 전개 성공률이 1~2/3 에 그쳤다. 단일 작업으로
    떼면 프롬프트가 짧아지고(system ~500자) 판정도 명확해진다.

    실패(LLM 오류·타임아웃·JSON 파싱·형식 불일치)는 **빈 리스트**로 흡수한다 — 호출부가 `decompose`
    원본을 그대로 쓰게 해 **기존 경로를 악화시키지 않는다**(§7 후퇴 없음).

    `llm` 미구성 가드가 `expand_needs` 가 아니라 **여기** 있는 이유: 방식 B(카탈로그 기반)·C(캐시)
    전개기는 LLM 을 쓰지 않으므로, 상위에서 막으면 주입형 seam(§3.2)이 무력해진다.
    """
    if llm is None:
        logger.info("needs_expansion_skipped", extra={"reason": "llm_unavailable"})
        return []
    try:
        raw = await llm.complete(
            system=_SYSTEM,
            user=f"USER_MESSAGE: {utterance}",
            tier=settings.needs_expansion_tier,
            max_tokens=200,
        )
        items = extract_json(raw).get("items")
    except LLMError as exc:
        logger.info("needs_expansion_failed", extra={"reason": str(exc)})
        return []
    if not isinstance(items, list):
        logger.info("needs_expansion_failed", extra={"reason": "items_not_list"})
        return []
    # 공백-only·비문자열은 거른다 — 미검증 LLM 값이 leg query 로 새면 임베딩 앵커가 오염된다
    # (decompose `_parse_category_queries` 의 blank=falsy 규약과 동일).
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


async def expand_needs(
    utterance: str,
    *,
    llm,
    settings,
    expand: ExpandFn = _llm_expand,
) -> list[str]:
    """목적·상황형 발화를 구체 상품명 목록으로 전개한다. 실패하면 **빈 리스트**(§7).

    `expand` 는 주입형 seam(§3.2) — 기본값은 방식 A(LLM 전개)이고, 나중에 B(카탈로그 기반)나
    C(캐시)로 갈아끼우는 것이 **주입 한 줄**이 되게 한다. 저장소 공통 패턴(embed·search_top_k·
    exact_lookup·select_category)과 동일하다.

    상한은 `category_fanout_max` 를 **재사용**한다 — 매핑·검색이 이미 그 값으로 절단하므로 별도
    튜너블을 두면 두 상한이 어긋난다(§6). `needs_expansion_min_items` 미만이면 전개 실패로 본다
    (1개면 발화 복사로 되돌아간다).
    """
    items = await expand(utterance, llm=llm, settings=settings)
    items = items[: settings.category_fanout_max]
    if len(items) < settings.needs_expansion_min_items:
        logger.info(
            "needs_expansion_failed",
            extra={"reason": "below_min_items", "items": items},
        )
        return []
    logger.info("needs_expanded", extra={"items": items, "count": len(items)})
    return items
