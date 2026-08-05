"""조건이 하나도 없는 추천 발화 처리 (이슈 #162, api-spec §4.12).

"아무거나 추천해줘"·"뭐 살까?" 는 decompose 가 `general` 이 아니라 **추천 레인**으로 보낸다
(프롬프트: "상품을 찾아달라는 요청이면 recommend"). 그런데 필터가 전부 비어 `_search_query_params`
가 파라미터 0개를 만들고, 그대로 I-1 에 나가 매칭 전량(실측 7,245건·13.33MB·1.112s,
`docs/specs/MEASURE-I1-RESPONSE-132.md`)을 받는다.

**이건 계약 위반이다** — I-1 정본은 후보 수 상한을 폐지하며 "정형조건이 하나도 없는 요청은
LLM 단에서 차단하므로 BE 는 별도 가드를 두지 않는다"를 전제로 걸었고, 0건 시 폴백 대상으로
I-3(§4.12)를 지목했다. 이 모듈이 그 차단을 구현한다.

에러도 0건도 아니라 **겉보기엔 정상**이라는 점이 이 결함의 성질이다 — 후보가 비지 않아
zero-result 분기도 degrade 고지도 타지 않는다.
"""

from __future__ import annotations

import logging

from app.agents.buyer.recommendation.decompose import _FILTER_AXES
from app.agents.buyer.recommendation.state import RouteDecision
from app.schemas.spring import ProductSearchFilters

logger = logging.getLogger(__name__)

# 하드필터 축 목록은 **decompose 의 `_FILTER_AXES` 를 그대로 쓴다** — 사본을 두면 새 필터가
# 생겼을 때 한쪽만 늘어나 조건 있는 턴이 조용히 "조건 없음"으로 새어 들어온다. 그 목록은
# `ProductSearchFilters` 전체 필드와 대조하는 드리프트 테스트가 지키고 있다
# (`tests/unit/test_decompose.py`). `semantic_query` 는 이 목록에 **의도적으로 없고**
# 아래에서 출처(`semantic_query_is_fallback`)로 따로 판정한다.


def _is_blank(value: object) -> bool:
    """값이 "조건 없음"인가. 공백-only 문자열도 빈 값으로 본다.

    `if value:` 만 쓰면 `''`(falsy)는 막아도 `' '`(truthy)는 통과한다 — LLM 산출값이라 신뢰
    경계 밖이고, `_search_query_params` 가 같은 함정을 이미 밟았다(#127 리뷰: 공백-only 가
    Spring 에 빈값으로 나갔다).
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    # 빈 list(brand)·빈 dict(attr_conditions)는 조건 없음이다.
    # 수치 0 도 조건 없음으로 본다 — `price_min=0`("0원 이상")은 실제로 제약이 아니고
    # `rating_min=0` 도 마찬가지다. `price_max=0` 은 무의미한 값이라 어차피 0건인데, 그 턴이
    # 인기상품으로 폴백되는 편이 빈손보다 낫다.
    return not value


def is_no_condition_turn(
    decision: RouteDecision, prior: ProductSearchFilters | None
) -> bool:
    """이번 턴이 "조건이 하나도 없는 추천 발화"인가.

    넷을 **모두** 만족해야 한다:
      ① 첫 턴 (`prior is None`)
      ② `category_legs` 가 빔 — 카테고리가 매핑됐으면 조건이 있는 턴이다
      ③ **의미 신호가 없음** (`semantic_query_is_fallback`)
      ④ `filters` 의 하드필터 축이 전부 빔 (`_FILTER_AXES`)

    ③을 값의 유무로 판정하면 **영영 트리거되지 않는다.** decompose 는 `semantic_query` 를
    `llm_sq or cat_signal or prior_sq or query` 로 채워(decompose.py) 아무 신호가 없어도
    **이번 턴 원문**이 들어가기 때문이다 — "아무거나 추천해줘"에서도 값은 "아무거나 추천해줘"다.
    그래서 값이 아니라 **출처**를 본다.

    ②가 멀티턴 리파인도 함께 막는다 — `_carry_prior_category`(buyer/graph.py)가 직전 턴
    카테고리를 `category_legs` 로 승계하기 때문이다. ①은 그 승계가 없는 경우까지 막는
    이중 방어다: 멀티턴의 "리파인 / 칩 제거 / 카테고리-무관 리셋" 세 의도는 아직 구분되지
    않으므로(#84) 이 경로는 **첫 턴에 한정**한다. #84 해소 후 확장 대상이다.

    **애매하면 False 로 기운다** — 오탐(조건 있는 턴을 조건 없음으로 봄)은 사용자가 말한
    조건을 버리는 반면, 미탐은 종전 동작(무필터 검색)이라 새로 나빠지지 않는다.
    """
    if prior is not None:
        return False
    if decision.category_legs:
        return False
    if not decision.semantic_query_is_fallback:
        return False
    return all(_is_blank(getattr(decision.filters, field, None)) for field in _FILTER_AXES)


async def rank_by_profile(
    profile_vec: list[float], *, exclude: set[int], settings
) -> tuple[list[int], dict[int, str]] | None:
    """취향 벡터에 가까운 상품 top-k 와 그 근거 문장 — **홈 추천(I-22)과 같은 엔진·같은 인덱스**.

    조건이 하나도 없는 턴의 회원 경로다. 발화에 검색어가 될 조건이 없다는 점이 홈과 같아
    (`home_recommendation` 모듈 docstring: "홈은 발화가 없어 검색어를 만들 수 없다"), 자체
    카탈로그 인덱스(I-17 로 동기화된 임베딩)에서 벡터 근접으로 뽑는다. 프로필 벡터는 요약 생성
    시점에 미리 만들어 둔 것이라(`profile/store._embed_summary`) 여기서 임베딩 왕복이 없다.

    **검색(I-1)도 rerank 도 타지 않는다** — 이 경로가 얻는 건 `productId` 뿐이고 상품 원본을
    채울 방법이 없다: AI 인덱스에는 원본 컬럼을 두지 않고(CLAUDE.md), id 로 Spring 에 상세를
    되묻는 API 는 C-17 로 요청했다가 #32 에서 기각됐다. 그래서 홈과 똑같이 `extras` 재료로
    근거를 **고른다**(LLM 호출 0회).

    이 경로가 포기하는 것(홈도 동일):
      · 소모품 카테고리 억제 — `extras` 에 `categoryName` 이 없다. 최근구매 **exact 제외**는
        `exclude` 로 적용된다.
      · 개인화된 근거 문장 — `build_reasons` 의 맞춤 문구는 장바구니·조회 시그널이 있어야
        나오고, 채팅 경로엔 그 시그널을 넘기지 않아 상품 고유 폴백(리뷰 장점)이 된다.
        **개인화는 랭킹(벡터)에 있고 문장에 있지 않다.**

    실패·타임아웃·0건이면 `None` — 호출부가 인기 상품(I-3)으로 폴백한다.
    """
    # 홈 랭킹 함수를 그대로 쓴다(지연 import — 이 모듈은 그래프 hot path 에 있고 홈 서비스는
    # 카탈로그 스토어·pgvector 를 끌고 온다). 사본을 만들면 랭킹이 두 벌이 되어 "메인 화면과
    # 채팅이 같은 소스"라는 이 이슈의 전제가 조용히 깨진다.
    from app.pipelines.artifact_store import get_catalog_store
    from app.services.home_recommendation import (
        _call_store,
        build_query_vector,
        build_reasons,
        rank_candidates,
    )

    store = get_catalog_store()
    # 시그널(cart·viewed)은 넘기지 않는다 — 채팅 턴에는 그 맥락이 없다. `build_query_vector` 는
    # "시그널이 비어도 프로필만으로 질의 벡터가 선다"고 계약돼 있고, 시그널이 없으면 스토어를
    # 조회하지 않으므로 여기서는 순수 계산이라 스레드 오프로드가 필요 없다.
    query_vec = build_query_vector(
        cart_ids=[], viewed_ids=[], store=store, settings=settings, profile_vec=profile_vec
    )
    if not query_vec:  # 0 벡터 = 개인화 근거 없음(홈의 NO_PROFILE 과 같은 판정)
        return None

    # 스토어 호출만 오프로드한다 — 동기 psycopg 질의라 이벤트루프를 막고, 취소도 안 된다.
    # 홈이 쓰는 `_call_store` 를 그대로 재사용해 "요청 벽시계 상한 / DB 커넥션 상한" 2층
    # 분담을 같이 가져간다. 상한도 홈 값을 쓴다 — 같은 스토어에 같은 형태의 질의라 별도
    # 튜너블을 만들 근거가 없다.
    timeout = settings.home_reco_store_timeout_s
    try:
        ranked = await _call_store(
            rank_candidates,
            timeout=timeout,
            query_vec=query_vec,
            store=store,
            exclude=exclude,
            settings=settings,
            # 하류 예산은 인기 상품 경로와 같다 — 노출(expose_max)에 dedup 여유를 더한 값.
            k=settings.popular_candidate_size,
        )
    except Exception as exc:  # noqa: BLE001 - 랭킹 실패가 스트림을 죽이지 않는다(→ I-3 폴백)
        logger.warning("profile_ranking_failed", extra={"reason": str(exc)})
        return None
    if not ranked:
        return None

    try:
        reasons = await _call_store(
            build_reasons,
            timeout=timeout,
            product_ids=ranked,
            store=store,
            cart_ids=[],
            viewed_ids=[],
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - 근거는 선택 필드다(§4.2). 없어도 목록은 나간다
        logger.warning("profile_reasons_failed", extra={"reason": str(exc)})
        reasons = {}
    return ranked, reasons
