"""목적·상황형 발화의 상품 전개 (이슈 #198, `DESIGN-NEEDS-EXPANSION-198.md`).

`"집들이 선물로 뭐 사갈까"` 처럼 **무엇을 살지 사용자가 말하지 않은 발화**를 구체 상품 목록으로
전개한다(정본 `SPEC-RECOMMEND-001` §5.1 `shopping_list` 분해). 이 단계가 없으면 매핑
(`category_mapping`)은 물건이 아닌 문자열(`'집들이 선물'`)을 입력으로 받아 canonical 을 낼 수 없다.

**감지는 코드가, 생성은 LLM 이** 담당한다(§3). `decompose` 프롬프트에 전개를 맡기는 방식은 실측
39회에서 1~2/3 확률로만 성립했고(규칙 강화·예시 확대 모두 실패), 근본 원인은 `fast` tier 한 호출에
intent·filters·cart·attributes 가 함께 얹혀 "무엇을 살지 떠올리기"라는 생성·추론 여력이 없는 것이다.

**#217 로 감지가 매핑 뒤로 옮겨졌다** — 목적 marker 열거로 미리 맞히지 않고, 매핑이 canonical 을
못 낸 leg 이 있으면 전개한다(§4). 열거는 `재료`·`아이디어`·`필수템` 을 놓치는데 목록을 늘리면
이미 정답 매핑되는 표현이 파괴되므로(§4.0), 열거 자체를 폐기했다. 전개 결과는 원 legs 를 교체하지
않고 **더한다**(§6 합집합) — 전개가 빗나가도 원 leg 을 잃지 않는다.

`case` 의 역할은 **트리거가 아니라 게이트**다 — 요구되는 신뢰도가 다르다.

- **트리거로는 쓰지 않는다**(§4.1): `case` 는 전개와 **같은 LLM 호출의 산출물**이라 전개가 실패한
  회차의 값을 신뢰할 근거가 없다(실측 `"부모님 환갑 선물"`: `case=[3,3,3]` 인데 `legs=[1,1,1]`).
  "전개가 **안 됐다**"는 사실은 결과를 직접 검사해야 알 수 있다.
- **게이트로는 쓴다**(§4.2): `case != 3` 이면 어떤 규칙도 발동하지 않는다. case 2
  (`"5만원 이하 아무거나"`·`"평점 높은 거 보여줘"`)는 **의도적으로 카테고리 무관**이라 좁히면 안 되는데
  (#22 가 테스트로 고정·#162 가 개선할 경로), leg 유무로도 매핑 실패로도 case 3 과 구분되지 않는다.
  오히려 case 2 leg 은 맞는 칸이 없는 것이 정상이라 **매핑 실패가 구조적으로 발생한다.**
  게이트는 case 1/2 를 **배제**하는 데만 쓰이고 그 판정은 실측이 뒷받침한다(목적형 15/15 `case=3`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

from app.agents.buyer.recommendation.state import CategoryQuery, extract_json
from app.core.llm import LLMError, resolve_model_id
from app.core.tracing import trace_span

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


def count_signal_legs(legs: Sequence[CategoryQuery]) -> int:
    """ "신호 있는 leg" 개수 — 저장소 규약대로 **`raw_category or query`** 로 본다(PR #203 리뷰).

    `detect_expansion_need` 의 D1(`no_legs`) 판정식과 **동일 식**을 이 함수 하나로 통일한다
    (규칙은 한 벌 — `resolve_category_action`·`has_new_category_signal` 등에서 이 저장소가
    지켜 온 규약과 같다). `graph.py` 가 전개 후 재매핑의 `sibling_expansion` 게이트(#428 리뷰
    5차 R5-1)에도 이 식을 그대로 쓴다 — 판정을 두 벌로 만들면 그래프와 이 모듈이 갈라진다.
    """
    return sum(
        1
        for q in legs
        if (q.raw_category and q.raw_category.strip()) or (q.query and q.query.strip())
    )


def detect_expansion_need(
    legs: Sequence[CategoryQuery],
    *,
    case: int,
    unresolved: Sequence[str],
) -> str | None:
    """전개가 필요하면 사유 문자열, 아니면 None (§4 — 결정적 판정).

    사유는 관측 로그(`needs_expansion_triggered.reason`)로 그대로 나가며, 분포를 보고 거리·마진
    임계를 재튜닝한다(§10).

    **`case != 3` 이면 어떤 규칙도 발동하지 않는다**(§4.2 게이트 — case 2 무필터 의도 보호).

    - **D1 `no_legs`** — 신호 있는 leg 이 하나도 없다.
    - **D2 `mapping_failed`** — 매핑이 canonical 을 못 낸 leg 이 있다(`unresolved`).

    **#217 로 판정 시점이 바뀌었다** — 초판은 목적 marker 열거로 매핑 **전에** 미리 맞혔는데
    (`utterance_copy`·`purpose_marker`), 열거는 목록에 없는 표현(`재료`·`아이디어`·`필수템`)을
    놓치고 목록을 늘리면 이미 정답 매핑되는 표현이 파괴됐다(§4.0). 이제는 **매핑을 해보고 그
    결과를 본다** — 판정식은 #115 §4.5 의 거리·마진을 그대로 재사용하므로 새 튜너블이 없다.

    무엇이 `unresolved` 에 담기는지는 **매핑 쪽 계약**이다(`CategoryMapping`) — 거리컷 드롭과 택일
    null 만 담고, 조회 예외·히트 0건은 담지 않는다. 여기서는 그 판정을 신뢰하고 유무만 본다.

    "신호 있는 leg" 은 저장소 규약대로 **`raw_category or query`** 로 본다(PR #203 리뷰) —
    `decompose._parse_category_queries`·graph 멀티턴 승계 판정과 동일. `category` 만 채우고
    `query=null` 인 leg 은 **이미 올바른 카테고리 신호**이므로 D1 로 오탐하면 안 된다(그 leg 은
    매핑에 태워져 raw 앵커로 판정된다).

    D1 을 먼저 보는 이유는 **사유가 턴당 하나여야** 하기 때문이다 — 신호가 없으면 매핑도 시도되지
    않아 `unresolved` 가 비지만, 혹시 상류가 잔여 값을 넘기더라도 `no_legs` 로 라벨해야 관측 분포가
    오염되지 않는다.
    """
    # §4.2 게이트 — case 3(목적·상황형)이 아니면 어떤 규칙도 발동하지 않는다.
    # case 2("5만원 이하 아무거나"·"평점 높은 거 보여줘")는 **의도적으로 카테고리 무관**이라 좁히면
    # 안 되는데(#22 가 테스트로 고정·#162 가 개선할 경로), leg 유무(D1)로도 매핑 실패(D2)로도
    # case 3 과 구분되지 않는다. **#217 이후 이 게이트는 더 중요해졌다** — case 2 leg 은 taxonomy 에
    # 맞는 칸이 없는 것이 정상이라 매핑 실패가 **구조적으로** 발생한다(`'평점 높은 거'` →
    # `게임 > PC게임` 0.3420 / 마진 0.0171, §4.5 실측).
    # 실패 방향이 비대칭이라 게이트를 전 규칙에 건다: 과하게 막으면 목적 표현 leg 이 남아 하류
    # 거리컷이 드롭해 무필터로 흡수되지만(안전), 새면 지어낸 카테고리로 검색이 좁혀진다(유해).
    if case != 3:
        return None

    if count_signal_legs(legs) == 0:
        return "no_legs"

    # 매핑이 canonical 을 못 낸 leg 이 하나라도 있으면 전개한다. **전부**를 요구하지 않는 이유:
    # 전개 결과는 원 legs 를 교체하지 않고 **더하므로**(§6 합집합), 성공한 leg 을 잃을 위험이 없다.
    # 초판 D3 가 "모든 leg 이 목적 표현일 때만"이라는 보수 조건을 달았던 것은 교체 배선 때문이었다.
    if unresolved:
        return "mapping_failed"
    return None


async def _llm_expand(utterance: str, *, llm, settings, observer=None) -> list[str]:
    """전용 LLM 호출 1회로 구체 상품명 목록을 만든다 (방식 A, §5).

    `decompose` 와 분리한 이유(§2): 한 호출에 intent·filters·cart·attributes 가 함께 얹히면
    "무엇을 살지 떠올리기"라는 생성·추론 여력이 없어 전개 성공률이 1~2/3 에 그쳤다. 단일 작업으로
    떼면 프롬프트가 짧아지고(system ~500자) 판정도 명확해진다.

    실패(LLM 오류·타임아웃·JSON 파싱·형식 불일치)는 **빈 리스트**로 흡수한다 — 호출부가 `decompose`
    원본을 그대로 쓰게 해 **기존 경로를 악화시키지 않는다**(§7 후퇴 없음).

    `llm` 미구성 가드가 `expand_needs` 가 아니라 **여기** 있는 이유: 방식 B(카탈로그 기반)·C(캐시)
    전개기는 LLM 을 쓰지 않으므로, 상위에서 막으면 주입형 seam(§3.2)이 무력해진다. **관측 기록도
    같은 이유로 여기 있다**(PR #203 리뷰) — 호출부에 두면 전개기 종류와 무관하게 기록돼 LLM 을 쓰지
    않는 전개기에도 유령 모델 호출이 남는다.
    """
    if llm is None:
        logger.info("needs_expansion_skipped", extra={"reason": "llm_unavailable"})
        return []
    # api-spec §6.3 — chat_request 로그의 model·토큰 합산에 이 조건부 호출을 싣는다. 정본
    # SPEC-RECOMMEND-001 AC-REC-37·§비기능이 "2 + 1 호출"이라고 명시하므로, 빠지면 그 주장을
    # 운영 로그로 검증할 수 없다. `decompose`(graph.py)·rerank 와 같이 **호출 전** 기록한다 —
    # 실패한 호출도 비용이 발생한다.
    if observer is not None:
        observer.record_model_call(resolve_model_id(settings, settings.needs_expansion_tier))
    try:
        with trace_span(
            "llm.needs_expansion",
            "llm",
            {"model": resolve_model_id(settings, settings.needs_expansion_tier)},
        ):
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
    observer=None,
) -> list[str]:
    """목적·상황형 발화를 구체 상품명 목록으로 전개한다. 실패하면 **빈 리스트**(§7).

    `expand` 는 주입형 seam(§3.2) — 기본값은 방식 A(LLM 전개)이고, 나중에 B(카탈로그 기반)나
    C(캐시)로 갈아끼우는 것이 **주입 한 줄**이 되게 한다. 저장소 공통 패턴(embed·search_top_k·
    exact_lookup·select_category)과 동일하다.

    `observer` 는 전개기에 그대로 넘긴다 — **모델 호출을 실제로 하는 쪽이 기록**하므로(§10,
    api-spec §6.3), LLM 을 쓰지 않는 전개기는 받고도 무시하면 된다.

    상한은 `category_fanout_max` 를 **재사용**한다 — 매핑·검색이 이미 그 값으로 절단하므로 별도
    튜너블을 두면 두 상한이 어긋난다(§6). `needs_expansion_min_items` 미만이면 전개 실패로 본다
    (1개면 발화 복사로 되돌아간다).
    """
    items = await expand(utterance, llm=llm, settings=settings, observer=observer)
    items = items[: settings.category_fanout_max]
    if len(items) < settings.needs_expansion_min_items:
        logger.info(
            "needs_expansion_failed",
            extra={"reason": "below_min_items", "items": items},
        )
        return []
    logger.info("needs_expanded", extra={"items": items, "count": len(items)})
    return items
