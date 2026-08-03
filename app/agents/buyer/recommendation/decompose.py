"""decompose 노드 — Haiku 1회로 질의 분해 + intent 라우팅 (SPEC-RECOMMEND-001 §6.1, 이슈 #2 MVP).

멀티턴: 직전 필터를 규약 JSON 으로 함께 넘겨 병합(add/replace)을 **프롬프트 안에서** 처리한다
(REQ-REC-051 — 병합 로직을 코드에 두지 않음). intent(recommend/general)도 같은 출력에서 파생 —
별도 분류 호출을 두지 않는다(EX-7). reset/carry·priority·sources 태깅·예산 scope 정밀화는 후속.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agents.buyer.recommendation.state import (
    CartIntent,
    CategoryQuery,
    RouteDecision,
    extract_json,
)
from app.core.llm import LLMClient, LLMError
from app.schemas.spring import ProductSearchFilters

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 커머스 어시스턴트의 질의 분해기입니다.
사용자 발화를 분석해 intent 를 정하고, 추천이면 구조화 필터/의미쿼리를, 장바구니면 상품/옵션/수량을 산출합니다.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{
  "intent": "recommend" | "cart_add" | "cart_view" | "order_status" | "general",
  "reply": "intent가 general일 때만 줄 짧은 한국어 답변, 아니면 빈 문자열",
  "case": 1 | 2 | 3,
  "semanticQuery": "정형 제약을 제외한 벡터 검색용 자연어",
  "attrConditions": { "<속성축>": "<희망값>" },
  "attrRemovals": [ "<제거할 속성축>" ],
  "categoryQueries": [ {"category": string|null, "query": string|null} ],
  "filters": {
    "priceMin": int|null, "priceMax": int|null,
    "brand": [string]|null, "ratingMin": number|null, "keyword": string|null,
    "color": string|null
  },
  "cart": { "productId": int|null, "optionId": int|null, "quantity": int },
  "revertCategories": [string],
  "repurchaseProducts": [string]
}
규칙:
- intent 판별: 상품을 찾아달라는 요청이면 recommend, "담아줘/장바구니에 넣어/2번 담아줘"처럼
  명시적으로 담기를 요청하면 cart_add, USER_MESSAGE에 **장바구니를 직접 명시하고 그 내용 조회를
  요청할 때만** cart_view, 회원 본인의 최근 주문·배송 진행 상태를 묻는 요청이면 order_status,
  그 외 잡담·무관 질문이면 general.
- order_status 긍정 예: "내 주문 어디까지 왔어?", "배송 상태 알려줘", "최근 주문 진행 상황".
- order_status로 분류하지 않는 예: "배송 빠른 상품 추천해줘"는 recommend,
  "이 상품 주문하고 싶어"는 기존 상품 추천/장바구니 의미, "주문 취소 방법"은 general,
  "예전에 뭘 샀지?"는 이번 주문 상태 조회 범위가 아니므로 general.
- intent는 다음 순서로 판정하세요:
  0) PENDING_CART가 있고 USER_MESSAGE가 options의 이름·번호·순번("드럼형", "2번", "2번으로",
     "두 번째")을 고르면 먼저 cart_add로 분류하고 그 optionId를 고르세요.
  1) "담아줘"·"장바구니에 넣어" 같은 **명시적 담기 동사**가 있으면 cart_add.
  2) 그 외에는 USER_MESSAGE에 "장바구니"가 직접 나오면서 그 내용을 조회할 때만 cart_view.
  3) 그 외에 "그거"·"저번에 그거" 같은 상품 지시대명사가 있으면 항상 recommend.
  4) 그 외에는 PENDING_CART를 무시하고 위의 일반 5-way intent 규칙으로 끝까지 판정하세요.
     그 결과 인사·잡담·무관 질문이면 general입니다.
- PENDING_CART가 있다는 사실만으로 이번 발화를 옵션 답변으로 보지 마세요. USER_MESSAGE가 options의
  이름·번호·순번을 실제로 고르는 0) 발화는 먼저 옵션 답변으로 처리하고, 그 외 "그거 보여줘"·
  "그거 사고 싶어" 같은 비옵션 발화에만 위 1)~4) 순서를 적용하세요.
- cart_view로 분류하지 않는 예: "그거 보여줘" → recommend, "저번에 그거 다시 보여줘" →
  recommend, "그거 또 사고 싶어" → recommend. "보여줘"·"뭐 있어?" 동사만으로 cart_view를
  선택하지 마세요. "사고 싶어"는 명시적 담기 동사가 아니므로 cart_add가 아니라 recommend입니다.
- 상품명 없는 지시대명사는 PRIOR_FILTERS.semanticQuery 또는 LAST_RECOMMENDATIONS 맥락의 **상품**을
  가리킵니다. 두 맥락이 비어 있어도 상품 요청으로 보고 recommend로 분류하세요. 이 경계는
  PENDING_CART가 있어도 옵션 답변이 아닌 상품 요청에 그대로 적용합니다.
- JSON 출력 직전에 위에서 선택한 가지의 결론과 필드가 일치하는지만 검산하고, 같은 발화를
  다시 분류하지 마세요. 단, cart_view인데 USER_MESSAGE에 "장바구니"가 없으면 recommend로 고치세요.
- recommend: 정확한 수치 제약은 filters 에 넣고 semanticQuery 로 근사하지 마세요.
  PRIOR_FILTERS 가 있으면 병합(좁히면 add, 모순되면 replace)하세요.
  색상 조건(예: "빨간", "검정")이 있으면 filters.color 에 넣으세요.
  semanticQuery 는 **찾는 상품의 의미**(예: "무선 이어폰")입니다. "더 저렴한 걸로", "다른 브랜드로"
  같은 **조건 다듬기 발화면 그 문구를 semanticQuery 로 쓰지 말고** PRIOR_FILTERS.semanticQuery(직전
  상품 의미)를 그대로 유지하세요 — 가격·브랜드 다듬기는 filters(priceMax·brand 등)로 가고,
  semanticQuery 는 직전 상품 의미를 이어야 벡터 재정렬이 뜻을 잃지 않습니다.
  semanticQuery 는 **동의어·상위어를 함께 담은 의미 중심** 자연어로 쓰세요 — 표현이 상품명과 달라도
  임베딩 재정렬이 잡도록(예: "청바지" → "청바지 데님 팬츠", "운동화" → "운동화 스니커즈",
  "무선 이어폰" → "무선 블루투스 이어폰"). 사전에 없는 억지 동의어는 넣지 말고 흔한 표현만.
- case: **발화가 무엇을 주는지**로 판정합니다(SPEC-RECOMMEND-001 REQ-REC-002).
  1 = **상품명이 발화에 있음** — "청바지", "무선 이어폰 추천해줘", "나이키 운동화"
  2 = 상품명 없이 **구조화 조건만** 있음 — "5만원 이하 아무거나", "평점 높은 거"
  3 = **상황·목적만 있고 무엇을 살지는 사용자가 말하지 않음** — "집들이 선물", "유럽여행 준비물",
      "부모님 환갑 선물", "발이 시려워", "자취 시작할 때 필요한거".
      또는 **서로 다른 상품 2개 이상**을 한 번에 요구 — "이어폰이랑 노트북 추천해줘".
  판정 기준: 사용자가 **살 물건을 지목했으면 1**, 조건만 말했으면 2, **목적/상황만 말했으면 3**입니다.
  "선물"·"준비물"·"필요한 것" 같은 말은 물건 이름이 아니므로 1이 아니라 **3**입니다.
- attrConditions/attrRemovals: 사용자가 **명시한** 상품 속성만 다룹니다(추측 선호는 넣지 말고
  semanticQuery/발화 맥락에 맡김 — 재랭킹이 판단). 축은 소재·핏·용도·방수 등, 값은 짧은 자연어.
  attrConditions = 이번 턴에 **새로 설정하거나 바꾸는** {축: 값}만(예: "린넨 오버핏 셔츠" →
  {"소재":"린넨","핏":"오버핏"}, "방수 파우치" → {"방수":"true"}). **이전 속성은 코드가 자동 유지
  (merge)하므로, 안 바뀌는 축은 다시 안 적어도 됩니다** — 이번 턴에 새/변경 속성이 없으면 생략하세요.
  attrRemovals = 사용자가 **명시적으로 빼라**고 한 축만(예: "핏은 상관없어" → ["핏"], "속성 다
  빼줘" → PRIOR_FILTERS.attrConditions 의 축 전부). 뺄 게 없으면 생략하세요.
  색상은 filters.color 로 갑니다(중복 금지).
- categoryQueries: 사용자가 살 만한 **구체적 상품**별로 하나씩 담으세요(최대 CATEGORY_FANOUT_MAX 개).
  query = 그 상품을 가리키는 **구체적 상품명**(짧은 명사구). category = 그 상품이 속할 카테고리
  best-guess — **확실하지 않으면 null 로 두세요. 없는 카테고리명을 지어내지 마세요**(실제 카테고리
  사전과 표기가 다른 라벨은 검색을 망칩니다. query 만 있어도 정상 동작합니다).
  · 단일 상품 질의: "무선 이어폰" → [{"category":"음향가전","query":"무선 이어폰"}]
  · **목적·상황·선물형 질의는 "무엇을 살지"를 먼저 떠올려 상품 단위로 나누세요**:
    "부모님 환갑 선물" → 홍삼 / 안마의자 / 한우 선물세트 / 영양제
    "자취 시작할 때 필요한거" → 전자레인지 / 이불 / 냄비 / 빨래바구니 / 행거
    "유럽여행 준비물" → 여행용 캐리어 / 멀티 어댑터 / 목베개 / 여행용 파우치
  · ❌ "선물용품"·"생활용품"·"주방용품"·"가전/전기" 처럼 **매장 코너 이름으로 뭉개지 마세요** —
    무엇을 살지가 빠지면 상품을 찾을 수 없습니다.
  · query 는 **순수 상품명만** 담으세요. 발화를 그대로 복사하지 말고 상품명으로 번역하고,
    가격·평가 수식어("갓성비", "가성비", "저렴한")와 상황 설명("~할 때 신을 수 있는")은 빼세요.
    가격 조건은 filters, 발화 전체 의미는 semanticQuery 가 담당하므로 정보가 사라지지 않습니다.
    "갓성비 무선이어폰" → query "무선 이어폰" (❌ "갓성비 무선 이어폰")
    "발 시려울 때 신을 수 있는거" → query "방한 부츠" (❌ "발 시려울 때 신을 수 있는 신발")
  이번 발화가 **새 카테고리·상품을 언급하지 않은 조건 다듬기**(예: "더 저렴한 걸로", "다른 브랜드")
  이고 PRIOR_FILTERS.category 가 있으면, 그 값을 categoryQueries 에 그대로 실어 **이전 카테고리를
  유지**하세요(카테고리를 비우면 직전 맥락이 사라집니다).
- cart_add: LAST_RECOMMENDATIONS(직전 추천 목록: productId+이름)에서 사용자가 가리킨 상품의
  productId 를 고르세요. 못 고르면 productId=null. quantity 기본 1.
- PENDING_CART(옵션 되물음 대기) 필드는 위 사다리의 결론에 따라 채우세요. 0)의 "고르면"은
  USER_MESSAGE가 options 중 한 이름과 정확히 일치하거나 그 이름에 선택 조사만 붙은 경우, 또는
  번호·순번이 한 option 위치와 정확히 일치하는 경우만 뜻합니다. 이때 답에 맞는 optionId만 고르고
  cart.productId=null로 두세요 — 서버가 되물은 상품을 이미 압니다. 1)에서 다른 상품 담기로 결정됐다면
  먼저 LAST_RECOMMENDATIONS에서 PENDING_CART.productId와 같은 상품을 제외하고, 남은 목록에서 사용자가
  가리킨 productId를 고르세요. 담기를 취소·중단하려 하면 intent=general로 전환하세요.
- revertCategories: 사용자가 특정 카테고리를 "다시 추천받기"(되돌리기 칩) 하거나 최근 구매로
  가려진 카테고리를 다시 보고 싶어하면 그 카테고리명을 넣으세요(예: ["조미료"]). 아니면 [].
- repurchaseProducts: 사용자가 **최근에 산 특정 상품을 다시 사거나 다시 추천받고 싶다**고 하면
  그 상품을 가리키는 **상품명**을 넣으세요(예: "최근에 산 무선이어폰 또 추천해줘" → ["무선 이어폰"]).
  "그거 또 사고 싶어"처럼 상품명이 빠진 지시대명사면 PRIOR_FILTERS 맥락에서 가리키는 **상품명**을
  해소해 넣으세요. 사용자가 재구매를 말로 지목한 상품만 넣고, LAST_RECOMMENDATIONS 에 있다는
  이유로 직전 추천 상품을 복사하지 마세요. 보통 상품 1개만 넣으며 재구매 의도가 없으면 [].
  카테고리 단위 되돌리기는 revertCategories 가 담당하니 카테고리명은 넣지 마세요.
- general: intent=general, reply 에 짧게 답하세요."""


# 검색 WHERE 로 나가는 하드필터 축 — 관측 대상(#119). semantic_query(의미검색 앵커)·
# exclude_product_ids(dedup)·limit(top-K)은 후보를 **거르는** 조건이 아니라 제외한다.
# 새 하드필터가 생기면 여기도 늘어나야 한다 — 드리프트는 테스트가 잡는다.
_FILTER_AXES = (
    "category",
    "price_min",
    "price_max",
    "brand",
    "rating_min",
    "keyword",
    "color",
    # attr_conditions 도 같은 성격의 하드필터다(attributes 매칭) — 프로필의 소재·핏 같은
    # 속성 선호가 새는 경로라 관측에서 빠지면 유출 대조가 그 경로만 놓친다(PR #223 리뷰).
    "attr_conditions",
)


def _filter_axes(filters: ProductSearchFilters) -> list[str]:
    """값이 설정된 하드필터 축 이름 (#119 관측 — 값은 담지 않는다).

    빈 컨테이너(`[]`/`{}`/`""`)는 "설정 안 됨"으로 본다. 다만 `0`은 LLM 이 실제로 내보낸
    값이므로 설정된 것으로 남긴다 — 유출 관측은 "무엇이 붙었나"를 보는 것이지 그 값이
    유효한지를 판정하는 자리가 아니다.
    """
    return [name for name in _FILTER_AXES if getattr(filters, name, None) not in (None, [], {}, "")]


async def decompose(
    llm: LLMClient,
    *,
    query: str,
    prior_filters: ProductSearchFilters | None,
    profile_summary: str | None,
    tier: str,
    last_recommendations: list[tuple[int, str]] | None = None,
    pending_cart: dict | None = None,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
) -> RouteDecision:
    """Haiku 1회 호출로 intent(추천/담기/장바구니조회/주문상태/일반)와 필터를 산출한다.

    prior_filters(추천 멀티턴)·last_recommendations(담기 productId 해소)·pending_cart(옵션 되물음)를
    프롬프트에 실어 문맥을 위임한다. LLM 오류/타임아웃/JSON·스키마 파싱 실패는 LLMError 로 전파.
    """
    import json

    prior_json = (
        "null"
        if prior_filters is None
        else json.dumps(
            prior_filters.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False
        )
    )
    reco_json = json.dumps(
        [{"productId": pid, "name": name} for pid, name in (last_recommendations or [])],
        ensure_ascii=False,
    )
    pending_json = "null" if not pending_cart else json.dumps(pending_cart, ensure_ascii=False)
    prof = profile_summary or "(없음)"
    user = (
        f"CATEGORY_FANOUT_MAX: {category_fanout_max}\n"
        f"PRIOR_FILTERS: {prior_json}\n"
        f"LAST_RECOMMENDATIONS: {reco_json}\n"
        f"PENDING_CART: {pending_json}\n"
        f"PROFILE_SUMMARY: {prof}\n"
        f"USER_MESSAGE: {query}"
    )

    raw = await llm.complete(system=_SYSTEM, user=user, tier=tier, max_tokens=800)
    data = extract_json(raw)

    intent_raw = data.get("intent")
    intent = (
        intent_raw
        if intent_raw in ("recommend", "cart_add", "cart_view", "order_status", "general")
        else "recommend"
    )
    # JSON 파싱은 됐지만 필드 값이 스키마와 안 맞을 수 있다 → extract_json 처럼 LLMError 로 통일해
    # 상위(graph.py)의 LLM_* error 이벤트로 흐르게 한다(첫 프레임 이전 raw 예외 → 500 방지).
    try:
        filters = ProductSearchFilters.model_validate(data.get("filters") or {})
        case = int(data.get("case") or 2)
        cart = _parse_cart(data.get("cart"))
        raw_revert = data.get("revertCategories")
        revert_categories = (
            [
                str(c) for c in raw_revert if isinstance(c, str) and c.strip()
            ]  # 공백-only 제외(PR#166)
            if isinstance(raw_revert, list)
            else []
        )
        repurchase_products = _parse_repurchase_products(
            data.get("repurchaseProducts"), repurchase_max
        )
        category_queries = _parse_category_queries(data.get("categoryQueries"), category_fanout_max)
        # semanticQuery 는 filters 밖(최상위)에 오는 의미검색 입력 — 검색 백엔드까지 흐르도록
        # filters 에 실어준다(#101). 폴백 순서(PR#166 리뷰):
        #   LLM 값 → (단일 카테고리면) 그 leg query → 직전 턴 값 → 이번 턴 원문(query).
        # cat_signal 은 **단일 카테고리**일 때만 그 leg query(자연어 검색어)를 전역으로 승격한다.
        # (1) raw_category("가전 > 이어폰/헤드폰" 분류 경로 breadcrumb)는 앵커로 부적합해 query 만 본다.
        # (2) 멀티 카테고리면 승격하지 않는다 — 전역 semantic_query 는 graph 의 query-null leg 폴백으로
        #     재사용되므로, 한 leg 검색어("여행 자물쇠")가 전역이 되면 무관한 leg(전자기기) 앵커로 샌다.
        #     멀티는 broad 한 prior_sq/원문으로 폴백하고, query 있는 멀티 leg 는 graph 가 자기 query 로
        #     override 한다(len(legs)>1). 단일 정제발화(query=null)면 cat_signal=None → prior_sq(#6).
        # 각 후보는 공백-only 를 falsy 로 정규화(spring_client._search_query_params 와 동일 규약,
        # PR#166) — LLM 이 "   " 를 내도 truthy 라 폴백 체인을 밀어내지 않게 한다. cat_signal 은
        # _parse_category_queries 가 이미 공백을 None 으로 거른다. 최종 원문(query)만 raw 폴백.
        cat_signal = category_queries[0].query if len(category_queries) == 1 else None
        prior_sq = (prior_filters.semantic_query or "").strip() if prior_filters else ""
        # data.get("semanticQuery")는 미검증 raw LLM 값 — 비문자열 truthy(숫자·리스트)면 `x or ""`가
        # 그 값을 그대로 반환해 .strip()에서 AttributeError(위 except 가 안 잡아 SSE 를 깬다). 형제
        # 필드(revert·categoryQueries)처럼 isinstance(str) 가드 후 strip 한다(구 str() 안전장치 복원).
        raw_sq = data.get("semanticQuery")
        llm_sq = raw_sq.strip() if isinstance(raw_sq, str) else ""
        filters.semantic_query = llm_sq or cat_signal or prior_sq or query
        # 명시 속성 하드조건(PR②) — search_catalog 가 SpringProduct.attributes 와 관대 매칭한다.
        # 멀티턴 모델(PR#169 리뷰): 기본은 **merge**(prior ∪ 이번 턴 설정값). 제거는 사용자가
        # 명시한 경우("핏 빼줘")만 attrRemovals 신호로 처리한다. 이렇게 하면 LLM 이 정제발화에서
        # 이전 축을 일부/전부 빠뜨려도(fast tier 실수) 조용히 유실되지 않고(merge 로 유지), '실수
        # 누락'과 '의도적 제거'를 dict 모양 추측이 아니라 명시 신호로 구분한다. attrRemovals 는 이번
        # 턴 지시라 저장하지 않고(결과 attr_conditions 만 영속), 적용 후 버린다.
        parsed_attr = _parse_attr_conditions(data.get("attrConditions"))
        prior_attr = prior_filters.attr_conditions if prior_filters else None
        merged = {**(prior_attr or {}), **(parsed_attr or {})}
        for axis in _parse_attr_removals(data.get("attrRemovals")):
            merged.pop(axis, None)
        filters.attr_conditions = merged or None
    except (ValidationError, ValueError, TypeError) as exc:
        raise LLMError("decompose 필터/케이스/장바구니 파싱 실패") from exc
    # [#198 §10] 관측 — recommend 턴의 case·leg 요약. **"case==3(전개 필요를 인지)인데 legs<=1
    # (전개 실패)"인 턴의 빈도가 #198 의 핵심 지표**다. 이 로그가 없어 지금까지 진단 스크립트로만
    # 측정할 수 있었다. leg_queries 는 D3 marker 튜닝(#198 OPEN-1)의 입력이기도 하다.
    # cart/general 턴은 case 가 의미 없어 제외한다 — 지표는 한 가지를 뜻해야 한다(category_unmapped
    # 를 인프라 실패와 섞지 않는 것과 같은 취지).
    if intent == "recommend":
        logger.info(
            "decompose_case",
            extra={
                "case": case,
                "legs": len(category_queries),
                "leg_queries": [q.query for q in category_queries],
                # [#119] 이번 턴에 값이 설정된 하드필터 **축 이름만** — 값은 싣지 않는다(PII,
                # tracing 카나리 오탐 회피). 같은 발화의 회원/게스트 턴을 대조하면 프로필이
                # 하드필터로 새는지 휴리스틱 없이 객관적으로 드러난다.
                "filters_set": _filter_axes(filters),
                "profile_injected": bool(profile_summary),
            },
        )
    return RouteDecision(
        intent=intent,
        filters=filters,
        case=case,
        reply=str(data.get("reply") or ""),
        cart=cart,
        revert_categories=revert_categories,
        repurchase_products=repurchase_products,
        category_queries=category_queries,
    )


def _parse_attr_conditions(raw: object) -> dict[str, str] | None:
    """decompose 의 attrConditions → {축: 값} (PR②, 명시 속성 하드조건).

    dict 가 아니면 None. 키·값이 str 이고 공백-only 가 아닌 항목만 남긴다(빈 dict 면 None) —
    LLM 이 비문자열/공백을 내도 관대 매칭(값.strip() 부분비교)이 크래시·오염되지 않게 한다
    (PR① 리뷰 교훈: 미검증 raw LLM 값은 isinstance + strip 가드). 값은 strip 해 저장한다.
    """
    if not isinstance(raw, dict):
        return None
    out = {
        k.strip(): v.strip()
        for k, v in raw.items()
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
    }
    return out or None


def _parse_attr_removals(raw: object) -> list[str]:
    """decompose 의 attrRemovals → 제거할 속성축 리스트 (PR②, PR#169 리뷰).

    사용자가 명시적으로 뺀 축("핏 상관없어" → ["핏"])만 담는다. 리스트가 아니면 빈 리스트,
    비문자열·공백 항목은 제외한다(revertCategories 와 동일 규약).
    """
    if not isinstance(raw, list):
        return []
    return [x.strip() for x in raw if isinstance(x, str) and x.strip()]


def _parse_repurchase_products(raw: object, cap: int) -> list[str]:
    """decompose 의 repurchaseProducts → 재구매 지목 상품명 리스트 (#120).

    리스트가 아니면 빈 리스트, 비문자열·공백 항목은 제외한다(revertCategories 와 동일 규약).
    `cap` 으로 절단해 LLM 이 긴 목록을 내도 파싱·전달 크기를 유계로 유지한다
    (`_parse_category_queries` 의 fanout_max 절단과 같은 규약 — slice 절단). 실제 해제 범위는
    graph 의 단일 지목 가드가 결정한다.
    """
    if not isinstance(raw, list):
        return []
    return [x.strip() for x in raw if isinstance(x, str) and x.strip()][:cap]


def _parse_category_queries(raw: object, fanout_max: int) -> list[CategoryQuery]:
    """decompose 의 categoryQueries → list[CategoryQuery] (방식 A, 이슈 #59).

    리스트가 아니면 빈 리스트(카테고리 신호 없음 → 그래프에서 무필터 검색, #22). 각 원소 dict 에서 category(str|None)·
    query(str|None)를 관대 파싱하고, fanout_max 로 개수를 절단한다(하드코딩 금지 상한).
    """
    if not isinstance(raw, list):
        return []
    out: list[CategoryQuery] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cat = item.get("category")
        qry = item.get("query")
        # 공백-only('  ')는 None 으로 정규화 — LLM 텍스트를 truthy 로 두면 빈 카테고리/검색어가
        # 신호로 잡혀 cat_signal 승격·leg 앵커를 오염시킨다(spring_client 와 동일 blank=falsy 규약, PR#166).
        out.append(
            CategoryQuery(
                raw_category=str(cat) if isinstance(cat, str) and cat.strip() else None,
                query=str(qry) if isinstance(qry, str) and qry.strip() else None,
            )
        )
    # 신호(raw·query) 있는 leg 만 남기고 절단 — 빈 leg(둘 다 없음)는 map_categories 에서 어차피
    # 스킵되므로, 절단 전에 빼지 않으면 LLM 이 앞쪽에 빈 항목을 섞어낼 때 fanout 예산만 먹고 뒤쪽
    # 실제 카테고리를 밀어낸다(§9 상한 의도 훼손, PR #73 리뷰).
    signal = [q for q in out if q.raw_category or q.query]
    # slice 절단 — category_mapping 의 dedup_truncate·_merge_fanout_results 와 동일 규약
    # (fanout_max<=0 이면 정확히 0개; append 후 체크는 첫 항목이 남아 절단 의미가 어긋난다, PR #73 리뷰).
    return signal[:fanout_max]


def _as_int(value: object) -> int | None:
    """LLM JSON 변형(int/float/숫자문자열)을 관대하게 int 로 변환한다(bool 제외).

    LLM 이 "quantity": 2.0 이나 "2" 처럼 내보내도 조용한 폴백(수량 1·productId None) 없이
    의도대로 해석되게 한다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_cart(raw: object) -> CartIntent | None:
    """decompose 의 cart 객체 → CartIntent (없거나 형식 오류면 빈 의도)."""
    if not isinstance(raw, dict):
        return CartIntent()
    qty = _as_int(raw.get("quantity"))
    return CartIntent(
        product_id=_as_int(raw.get("productId")),
        option_id=_as_int(raw.get("optionId")),
        # api-spec §4.1 수량 1~99 — 상한 초과 발화("100개")가 AddToCartRequest 검증에서
        # ValidationError 로 스트림을 끊지 않게 파싱 시점에 클램프한다.
        quantity=min(max(qty, 1), 99) if qty is not None else 1,
    )
