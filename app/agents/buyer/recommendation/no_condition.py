"""조건이 하나도 없는 추천 발화 처리 (이슈 #162, api-spec §4.17).

"아무거나 추천해줘"·"뭐 살까?" 는 decompose 가 `general` 이 아니라 **추천 레인**으로 보낸다
(프롬프트: "상품을 찾아달라는 요청이면 recommend"). 그런데 필터가 전부 비어 `_search_query_params`
가 파라미터 0개를 만들고, 그대로 I-1 에 나가 매칭 전량(실측 7,245건·13.33MB·1.112s,
`docs/specs/MEASURE-I1-RESPONSE-132.md`)을 받는다.

**이건 계약 위반이다** — I-1 정본은 후보 수 상한을 폐지하며 "정형조건이 하나도 없는 요청은
LLM 단에서 차단하므로 BE 는 별도 가드를 두지 않는다"를 전제로 걸었고, 0건 시 폴백 대상으로
I-3(§4.17)를 지목했다. 이 모듈이 그 차단을 구현한다.

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
#
# **`_FILTER_AXES` 만으로는 부족하다** — 그 목록이 답하는 질문은 "Spring WHERE 로 나가는
# 하드필터는 무엇인가"이고, 이 모듈이 물어야 하는 질문은 "사용자가 조건을 하나라도 줬는가"다.
# 두 질문이 다르다는 걸 놓쳐 `RouteDecision` 쪽 축이 통째로 빠져 있었다(PR #311 리뷰).
# 아래는 `filters` 밖, **`RouteDecision` 에 직접 실리는 사용자 의도 축**이다.
_DECISION_CONDITION_AXES = (
    # 최근 구매로 가려진 것을 다시 보겠다는 지목 — 상품 단위(repurchase)·카테고리 단위(revert).
    # 사용자가 **무언가를 명시적으로 지목한** 발화이므로 조건이다. 오늘은 그런 발화면
    # decompose 가 `semanticQuery` 를 채워 ③에서도 걸리지만, 그건 우연이라 여기서 못박는다.
    "repurchase_products",
    "revert_categories",
    # [PR #311 리뷰] **매핑 전 원시 카테고리 신호.** `category_legs`(매핑 후)가 대표한다고
    # 봤던 것이 틀렸다 — 매핑이 실패하면 legs 는 비지만 **사용자가 지목한 상품은 여기 남아
    # 있다.** "이어폰이랑 노트북 추천해줘"처럼 상품을 2개 이상 지목한 턴은 `cat_signal` 승격이
    # leg 1개 조건에 걸려(`decompose.py`: `len(category_queries) == 1`) ③도 통과하므로,
    # 두 카테고리가 모두 매핑에 실패하면 "조건 없음"으로 오판정돼 사용자가 말한 상품군을
    # 통째로 버리고 인기 상품이 나간다(재현 확인). 매핑 성공 여부와 무관하게 **신호의 유무**로
    # 본다 — 종전 동작(무필터 검색 + 원문 rerank)이 그 케이스에서는 더 안전했다.
    "category_queries",
)


def has_total_budget(decision: RouteDecision) -> bool:
    """이번 턴에 **총액 예산**이 실렸는가(`total_budget`).

    `filters.price_max`(상품 하나당 상한)와 **다른 축**이라(state.py) `_FILTER_AXES` 에 없다.
    그렇다고 `is_no_condition_turn` 을 막는 축으로 두지도 **않는다** — 막으면 그 턴이 무필터
    I-1(실측 7,245건·13.33MB)로 되돌아가는데, 그래 봐야 예산이 반영되지 않는다.
    `build_budget_sets`(#60)는 **니즈(leg)가 2개 이상**일 때만 도는데(`split_by_need`), 조건 없는
    턴은 정의상 leg 가 비어 있어 어느 경로로 가도 예산 세트가 만들어지지 않기 때문이다.

    대신 **후보 확보 방식**을 가르는 데 쓴다:
      · 취향 벡터 랭킹을 **막는다** — AI 카탈로그 인덱스(`CatalogArtifact`)에 **가격이 없어**
        예산 준수를 확인할 방법이 없다. 5만원이라 말한 사용자에게 8만원짜리가 나갈 수 있다.
      · 인기 상품(I-3)은 응답에 `price` 가 있어 **예산 안의 후보만 남길 수 있다**.

    그래서 "총 5만원 있어 아무거나"는 **세트 조합이 아니라 예산 안의 대안 + 되묻기**로 처리한다.
    무엇을 몇 개 살지 사용자가 말하지 않은 턴에 조합을 지어내면 "이어폰+샴푸+등산화 합쳐 5만원"
    같은 근거 없는 묶음이 나온다 — 고를 기준이 없기 때문이다.

    **`buy_all` 은 보지 않는다.** 예산 없는 "다 사줘"에는 확인할 가격이 애초에 없어 취향 경로를
    막을 근거가 없고, leg 가 없어 어느 경로로 가도 `PICK_ONE` 으로 끝나 결과도 같다. 그런 턴은
    평소대로 취향(회원)·인기(게스트) 최적 상품을 추천한다.
    """
    return decision.total_budget is not None


def within_budget(products: list, budget: int) -> list:
    """가격이 예산 이하인 상품만 남긴다.

    **가격이 없는 상품(`price is None`)은 뺀다** — 예산 준수를 확인할 수 없는 후보를 남기면
    "5만원이라 했는데 8만원짜리를 줬다"가 된다. `build_budget_sets` 도 가격 없는 후보를
    `unavailable_legs` 로 빼는 같은 규약이다(평점 사후필터의 "데이터 부재는 보존"과 반대인 이유:
    평점은 **반증**이 필요하지만 예산은 **입증**이 필요하다).
    """
    return [p for p in products if p.price is not None and p.price <= budget]


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

    다섯을 **모두** 만족해야 한다:
      ① 첫 턴 (`prior is None`)
      ② `category_legs` 가 빔 — 카테고리가 매핑됐으면 조건이 있는 턴이다
      ③ **의미 신호가 없음** (`semantic_query_is_fallback`)
      ④ `filters` 의 하드필터 축이 전부 빔 (`_FILTER_AXES`)
      ⑤ **`RouteDecision` 에 실린 지목 축이 전부 빔** (`_DECISION_CONDITION_AXES`)

    ③을 값의 유무로 판정하면 **영영 트리거되지 않는다.** decompose 는 `semantic_query` 를
    `llm_sq or cat_signal or prior_sq or query` 로 채워(decompose.py) 아무 신호가 없어도
    **이번 턴 원문**이 들어가기 때문이다 — "아무거나 추천해줘"에서도 값은 "아무거나 추천해줘"다.
    그래서 값이 아니라 **출처**를 본다.

    ⑤는 `filters` 밖의 축이다 — `_FILTER_AXES` 는 "Spring WHERE 로 나가는 하드필터"의 정본이지
    "사용자가 조건을 줬는가"의 정본이 아니라, 재사용할 때 두 질문이 같은지 확인했어야 했다
    (PR #311 리뷰, `docs/lessons.md`).

    **총액 예산(`total_budget`)·전부구매(`buy_all`)는 여기서 막지 않는다** — 막으면 그 턴이
    무필터 I-1(13.33MB)로 되돌아가는데 예산이 반영되지도 않는다. `total_budget` 은 대신
    **후보 확보 방식**을 가르고(취향 경로 차단 + 인기 상품을 예산으로 거름), `buy_all` 단독은
    아무것도 바꾸지 않는다 — 자세한 근거는 `has_total_budget` docstring 에 있다.

    ②가 멀티턴 리파인도 함께 막는다 — `_carry_prior_category`(buyer/graph.py)가 직전 턴
    카테고리를 `category_legs` 로 승계하기 때문이다. ①은 그 승계가 없는 경우까지 막는
    이중 방어다: 멀티턴의 "리파인 / 칩 제거 / 카테고리-무관 리셋" 세 의도 구분은 #84 소관이라
    이 경로는 **첫 턴에 한정**한다.

    **애매하면 False 로 기운다** — 오탐(조건 있는 턴을 조건 없음으로 봄)은 사용자가 말한
    조건을 버리는 반면, 미탐은 종전 동작(무필터 검색)이라 새로 나빠지지 않는다.
    """
    if prior is not None:
        return False
    if decision.category_legs:
        return False
    if not decision.semantic_query_is_fallback:
        return False
    if any(not _is_blank(getattr(decision, axis, None)) for axis in _DECISION_CONDITION_AXES):
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
