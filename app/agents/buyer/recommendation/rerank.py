"""rerank 노드 — Sonnet 1회로 후보를 프로필 기반 재랭킹 + 근거 생성 (SPEC-RECOMMEND-001 §6.4).

MVP 슬라이스: 후보 외 id 미노출(REQ-REC-081 — 값싼 부분집합 대조만)·노출 상한(config)만 강제한다.
근거 속성 결정적 대조(REQ-REC-082)·순서 무작위화(REQ-REC-080)·출력검증 degrade 상태 기록은 후속.
실패/타임아웃/유효 후보 0건은 LLMError 로 전파 — 상위가 검색순서 degrade 로 처리한다(§7).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from statistics import median

from app.agents.buyer.recommendation.rerank_grounding import (
    CandidateGroundingFacts,
    GroundingArm,
    GroundingDecision,
    validate_and_render_grounding,
)
from app.agents.buyer.recommendation.rerank_code_assisted import (
    CodeAssistedSchemaError,
    CodeScoringContext,
    build_candidate_code_signals,
    fallback_reason_for_signals,
    parse_code_assisted_ranking,
)
from app.agents.buyer.recommendation.rerank_scoring import (
    RankingArm,
    ScoringSchemaError,
    compute_scored_ranking,
)
from app.agents.buyer.recommendation.state import RerankResult, extract_json
from app.core.config import get_settings
from app.core.llm import LLMClient, LLMError
from app.schemas.spring import ProductSearchFilters, SpringProduct

_SYSTEM = """당신은 커머스 추천 재랭킹기입니다. 후보 상품과 사용자 질의(+프로필)를 받아
가장 적합한 순서로 재랭킹하고 상품마다 한글 40자 이내의 1문장 한국어 근거를 답니다.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{"ranked": [{"productId": int, "rationale": "한글 40자 이내 1문장 근거"}], "overallComment": "전체 1~2문장 코멘트"}
규칙:
- productId 는 반드시 후보 목록(CANDIDATES)에 있는 값만 쓰세요. 없는 id 를 만들지 마세요.
- 후보가 실제로 갖지 않은 속성(브랜드·평점 등)을 근거로 주장하지 마세요.
- ratingLevel(평가없음/낮음/보통/높음/매우높음)·reviewLevel(정보없음/없음/적음/보통/많음/매우많음)·priceLevel(매우저렴/저렴/보통/비쌈/매우비쌈/정보없음)은 등급이며, 근거는 정성적으로만 쓰세요(예: "평점이 높아요", "리뷰가 많아요"). priceLevel은 후보군 안에서의 상대 등급이며 절대 가격 수준이 아닙니다. 금액·가격 숫자를 쓰지 마세요(정확한 가격은 화면 카드가 보여줍니다). '평가없음'·'정보없음'은 데이터가 없다는 뜻이니 해당 평점·리뷰·가격을 근거로 삼지 마세요. 등급을 그대로 인용해도 좋지만 없는 숫자를 지어내지 마세요.
- rationale 은 한글 40자 이내 1문장으로 간결하게 — 개행 없이.
- overallComment 는 마크다운을 쓰지 마세요 — 제목(#)·표·코드블럭·링크·목록 기호 금지, 평문 산문으로.
- 가장 적합한 순으로 정렬하고 상위만 남기세요."""

_SYSTEM_STRUCTURED_GROUNDING = """당신은 커머스 추천 재랭킹기입니다. 후보 상품과 사용자 질의(+프로필)를 받아
가장 적합한 순서로 재랭킹하고 상품마다 검증 가능한 근거를 답니다.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{"ranked": [{"productId": int, "rationale": "한글 40자 이내 1문장", "reasonCode": "RATING_HIGH|REVIEW_MANY|PRICE_RELATIVE_LOW|NO_VERIFIABLE_EVIDENCE", "evidenceFields": ["ratingLevel|reviewLevel|priceLevel"]}], "overallComment": "전체 1~2문장 코멘트", "overallClaims": [{"claimCode": "TOP_REVIEW_COUNT|ALL_RATING_HIGH|ALL_WITHIN_TOTAL_BUDGET|NO_VERIFIABLE_OVERALL_CLAIM", "scope": "FINAL_EXPOSED_PRODUCTS|FINAL_RECOMMENDATION_LISTS", "subjectProductIds": [int], "evidenceFields": ["reviewCount|ratingLevel|price|totalBudget"]}]}
규칙:
- productId 는 반드시 후보 목록(CANDIDATES)에 있는 값만 쓰세요. 없는 id 를 만들지 마세요.
- RATING_HIGH 는 ratingLevel 이 높음/매우높음일 때만 쓰고 evidenceFields 는 ["ratingLevel"]입니다.
- REVIEW_MANY 는 reviewLevel 이 많음/매우많음일 때만 쓰고 evidenceFields 는 ["reviewLevel"]입니다.
- PRICE_RELATIVE_LOW 는 priceLevel 이 저렴/매우저렴일 때만 쓰고 evidenceFields 는 ["priceLevel"]입니다.
- 셋 중 검증 가능한 근거가 없으면 NO_VERIFIABLE_EVIDENCE 와 빈 evidenceFields 를 쓰고 rationale 은
  "요청과의 관련도를 기준으로 추천했어요"로 답하세요.
- ratingLevel·reviewLevel·priceLevel은 정성 등급입니다. 정확한 금액·평점·리뷰 수를 만들지 마세요.
- rationale 은 한글 40자 이내 1문장으로 간결하게 쓰고 개행하지 마세요.
- overallComment 는 마크다운을 쓰지 말고 평문 산문으로 쓰세요.
- overallClaims 는 전체 코멘트의 검증 가능한 주장만 최대 2개 제안하세요.
- TOP_REVIEW_COUNT 는 첫 추천 상품 하나를 subjectProductIds 로 쓰고 scope 는
  FINAL_EXPOSED_PRODUCTS, evidenceFields 는 ["reviewCount"]로 쓰세요.
- ALL_RATING_HIGH 는 추천한 모든 상품을 subjectProductIds 로 쓰고 scope 는
  FINAL_EXPOSED_PRODUCTS, evidenceFields 는 ["ratingLevel"]로 쓰세요.
- ALL_WITHIN_TOTAL_BUDGET 는 최종 추천 조합 전체를 subjectProductIds 로 쓰고 scope 는
  FINAL_RECOMMENDATION_LISTS, evidenceFields 는 ["price", "totalBudget"]로 쓰세요.
- 인기·가성비처럼 CANDIDATES 필드로 증명할 수 없는 최상급을 제안하지 마세요.
- 검증 가능한 전체 주장이 없으면 NO_VERIFIABLE_OVERALL_CLAIM, FINAL_EXPOSED_PRODUCTS,
  빈 subjectProductIds, 빈 evidenceFields 한 건만 쓰세요.
- 가장 적합한 순으로 정렬하고 상위만 남기세요."""

_SYSTEM_STRUCTURED_SCORING = """당신은 커머스 추천 평가기입니다. 후보 상품과 사용자 질의(+프로필)를 받아
각 후보의 적합도와 검증 가능한 근거를 평가합니다. 최종 순위는 코드가 계산하므로 정렬하거나 상위만
고르지 마세요. 반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{"evaluations": [{"productId": int, "intentFit": int, "needFit": int, "profileFit": int, "rationale": "한글 40자 이내 1문장", "reasonCode": "RATING_HIGH|REVIEW_MANY|PRICE_RELATIVE_LOW|NO_VERIFIABLE_EVIDENCE", "evidenceFields": ["ratingLevel|reviewLevel|priceLevel"]}], "overallComment": "전체 1~2문장 코멘트", "overallClaims": [{"claimCode": "TOP_REVIEW_COUNT|ALL_RATING_HIGH|ALL_WITHIN_TOTAL_BUDGET|NO_VERIFIABLE_OVERALL_CLAIM", "scope": "FINAL_EXPOSED_PRODUCTS|FINAL_RECOMMENDATION_LISTS", "subjectProductIds": [int], "evidenceFields": ["reviewCount|ratingLevel|price|totalBudget"]}]}
규칙:
- CANDIDATES의 모든 후보를 입력 순서와 무관하게 평가하고, 각 productId를 정확히 한 번 쓰세요.
- 후보 밖 productId를 만들거나 후보를 누락·중복하지 마세요.
- intentFit은 현재 QUERY의 핵심 의도 충족도이며 정수 0..4입니다.
- needFit은 후보의 need 필드가 있으면 해당 니즈 충족도, 없으면 QUERY의 구체 조건 충족도이며 정수 0..3입니다.
- profileFit은 QUERY 적합도가 같은 후보 사이의 동점 처리용 개인화 적합도이며 정수 0..1입니다.
- PROFILE_SUMMARY가 '(없음)'이면 profileFit은 반드시 0입니다. 프로필로 QUERY에 덜 맞는 후보를 우선하지 마세요.
- RATING_HIGH 는 ratingLevel 이 높음/매우높음일 때만 쓰고 evidenceFields 는 ["ratingLevel"]입니다.
- REVIEW_MANY 는 reviewLevel 이 많음/매우많음일 때만 쓰고 evidenceFields 는 ["reviewLevel"]입니다.
- PRICE_RELATIVE_LOW 는 priceLevel 이 저렴/매우저렴일 때만 쓰고 evidenceFields 는 ["priceLevel"]입니다.
- 셋 중 검증 가능한 근거가 없으면 NO_VERIFIABLE_EVIDENCE 와 빈 evidenceFields 를 쓰고 rationale 은
  "요청과의 관련도를 기준으로 추천했어요"로 답하세요.
- ratingLevel·reviewLevel·priceLevel은 정성 등급입니다. 정확한 금액·평점·리뷰 수를 만들지 마세요.
- rationale 은 한글 40자 이내 1문장으로 간결하게 쓰고 개행하지 마세요.
- overallComment 는 마크다운을 쓰지 말고 평문 산문으로 쓰세요.
- overallClaims 는 전체 코멘트의 검증 가능한 주장만 최대 2개 제안하세요.
- TOP_REVIEW_COUNT 는 첫 추천 상품 하나를 subjectProductIds 로 쓰고 scope 는
  FINAL_EXPOSED_PRODUCTS, evidenceFields 는 ["reviewCount"]로 쓰세요.
- ALL_RATING_HIGH 는 추천한 모든 상품을 subjectProductIds 로 쓰고 scope 는
  FINAL_EXPOSED_PRODUCTS, evidenceFields 는 ["ratingLevel"]로 쓰세요.
- ALL_WITHIN_TOTAL_BUDGET 는 최종 추천 조합 전체를 subjectProductIds 로 쓰고 scope 는
  FINAL_RECOMMENDATION_LISTS, evidenceFields 는 ["price", "totalBudget"]로 쓰세요.
- 인기·가성비처럼 CANDIDATES 필드로 증명할 수 없는 최상급을 제안하지 마세요.
- 검증 가능한 전체 주장이 없으면 NO_VERIFIABLE_OVERALL_CLAIM, FINAL_EXPOSED_PRODUCTS,
  빈 subjectProductIds, 빈 evidenceFields 한 건만 쓰세요."""

_SYSTEM_CODE_ASSISTED = """당신은 커머스 추천 최종 선택기입니다. 코드가 계산한 객관 신호와 사용자
질의(+프로필)를 함께 보고 가장 적합한 상품만 고릅니다. codeSignals의 값과 evidence는 코드가 검증한
권위 있는 입력이므로 다시 계산하거나 반대로 주장하지 마세요. 반드시 아래 JSON만 출력하세요
(설명·코드펜스 금지):
{"ranked": [{"productId": int, "semanticIntentFit": int, "useCaseFit": int, "profileFit": int, "semanticReasonCode": "DIRECT_INTENT_MATCH|USE_CASE_MATCH|PROFILE_TIEBREAK|NO_SEMANTIC_REASON", "evidenceRefs": ["후보 codeSignals.evidence[].ref 중 하나"], "rationale": "한글 40자 이내 1문장"}], "overallComment": "전체 1~2문장 코멘트", "overallClaims": [{"claimCode": "TOP_REVIEW_COUNT|ALL_RATING_HIGH|ALL_WITHIN_TOTAL_BUDGET|NO_VERIFIABLE_OVERALL_CLAIM", "scope": "FINAL_EXPOSED_PRODUCTS|FINAL_RECOMMENDATION_LISTS", "subjectProductIds": [int], "evidenceFields": ["reviewCount|ratingLevel|price|totalBudget"]}]}
규칙:
- CANDIDATES가 하나라도 있으면 ranked는 반드시 1개 이상 반환하고 EXPOSE_MAX를 넘지 마세요.
  적합한 후보가 없다고 판단해도 빈 배열을 반환하지 말고 상대적으로 가장 나은 후보를 고르세요.
- 각 ranked 항목은 위 JSON 예시의 모든 필드를 빠짐없이 쓰고, 세 적합도 점수와 productId는
  문자열이 아닌 JSON 정수로 쓰세요.
- productId는 후보에 있는 값만 정확히 한 번 사용하세요.
- semanticIntentFit은 QUERY 핵심 의미 적합도 정수 0..4입니다.
- useCaseFit은 용도·사용 상황·trade-off 적합도 정수 0..3입니다.
- profileFit은 QUERY 적합도가 같은 후보 사이의 개인화 tie-break 정수 0..1입니다.
- PROFILE_SUMMARY가 '(없음)'이면 profileFit은 반드시 0입니다. 프로필 때문에 QUERY에 덜 맞는
  후보를 우선하지 마세요.
- evidenceRefs는 반드시 그 후보의 codeSignals.evidence에 실제로 있는 ref만 사용하세요.
- 객관적 가격·평점·리뷰·브랜드·카테고리·속성은 codeSignals를 신뢰하고 별도 점수를 만들지 마세요.
- DIRECT_INTENT_MATCH는 semanticIntentFit 3 이상, USE_CASE_MATCH는 useCaseFit 2 이상일 때만
  사용하세요. PROFILE_TIEBREAK는 프로필이 있고 profileFit이 1일 때만 사용하세요.
- rationale은 evidenceRefs와 semanticReasonCode만 자연스럽게 풀어 쓴 한글 40자 이내 문장입니다.
  정확한 금액·평점·리뷰 수나 후보 입력에 없는 장점을 만들지 마세요.
- overallComment는 마크다운 없는 평문 산문으로 쓰세요.
- 검증 가능한 전체 주장이 없으면 NO_VERIFIABLE_OVERALL_CLAIM, FINAL_EXPOSED_PRODUCTS,
  빈 subjectProductIds, 빈 evidenceFields 한 건만 쓰세요."""

# [#119] 프로필이 있는 턴에만 user 메시지에 덧붙는 동점 처리 지시. `_SYSTEM` 에 두지 않는 이유는
# rerank() docstring 참조(프로필 없는 경로의 프롬프트 불변 유지). 테스트는 리터럴을 복붙하지 말고
# 이 상수를 import 해 검증한다 — 문구를 다듬어도 테스트가 깨지지 않으면서 배선은 보증된다.
_PROFILE_TIEBREAK = (
    "- PROFILE_SUMMARY 는 QUERY 를 만족하는 후보들 사이의 **동점 처리**에만 쓰세요. "
    "프로필 때문에 QUERY 에 덜 맞는 상품을 위로 올리지 말고, 프로필에만 있고 QUERY 에 "
    "없는 조건을 근거문에 쓰지 마세요.\n"
)

# [#132] 사용자가 평점을 **명시**한 턴에만 붙는다. rating 사후필터가 '반증된 것만' 제거해
# (무평점 보존, #100 P0 / #171) 리뷰 없는 신상품이 그대로 올라오는데, 기존 지시는 "평가없음을
# 근거로 삼지 말라"까지라 **거짓 주장만 막고 고지는 하지 않았다**. `_SYSTEM` 이 아니라 user
# 메시지에 조건부로 두는 이유는 `_PROFILE_TIEBREAK` 와 같다 — 평점을 안 물어본 턴의 프롬프트를
# 한 글자도 바꾸지 않기 위해서다(#198: 프롬프트에 지시를 얹었다 성공률이 3/3 → 1/3 로 희석된 전례).
# 코드(graph `_apply_unrated_disclosure`)가 고지를 이미 보장하므로 이 지시는 문장을 자연스럽게
# 만드는 보조다 — LLM 이 무시해도 고지 자체는 빠지지 않는다.
_UNRATED_DISCLOSURE = (
    "- 사용자가 평점 조건을 말했습니다. ratingLevel 이 '평가없음'인 후보를 추천한다면 근거문에 "
    "아직 평점이 없다는 사실을 드러내세요 — 평점이 조건을 만족한다고 암시하지 마세요.\n"
)


def _rating_tier(product: SpringProduct, settings) -> str:
    """rerank LLM 에 넘길 평점 등급(정확한 숫자 대신) — 비표시 수치 유출 원천 차단(#171 PR#172).

    정확한 rating(4.8)을 주면 LLM 이 근거문에 "4.8 평점"처럼 흘려 CH-5 표시값과 어긋날 수 있어,
    LLM 에는 등급만 준다. 정확한 rating 은 SpringProduct.rating 에 남아 코드 필터(rating_min)·예산이
    계속 정확히 쓴다(질의 "평점 4.5 이상"은 search_catalog 사후필터 소관, 티어화 무영향). review_count
    ==0(리뷰 없음)·rating None 은 데이터 부재로 '평가없음'(#171 rating=0 판별 유지). 경계는 config
    주입(settings) — 호출부가 get_settings()를 1회 조회해 전달한다(핫패스 루프 내 반복 조회 회피).
    """
    if product.rating is None or product.review_count == 0:
        return "평가없음"
    r = product.rating
    if r >= settings.rating_tier_excellent:
        return "매우높음"
    if r >= settings.rating_tier_good:
        return "높음"
    if r >= settings.rating_tier_fair:
        return "보통"
    return "낮음"


def _review_tier(product: SpringProduct, settings) -> str:
    """rerank LLM 에 넘길 리뷰량 등급(정확한 개수 대신) — 유출 원천 차단(#171 PR#172).

    정확한 reviewCount(128)를 주면 LLM 이 "리뷰 128개"처럼 흘릴 수 있어 등급만 준다. None(미전송)은
    '정보없음', 0 은 '없음'(신상). 정확한 개수는 원본에 남아 계산용으로 쓴다. 경계는 config 주입.
    """
    rc = product.review_count
    if rc is None:
        return "정보없음"
    if rc == 0:
        return "없음"
    if rc >= settings.review_tier_many:
        return "매우많음"
    if rc >= settings.review_tier_some:
        return "많음"
    if rc >= settings.review_tier_few:
        return "보통"
    return "적음"


def _price_tier(price: int | None, median_price: float | None, settings) -> str:
    """rerank LLM 에 넘길 후보군 상대 가격 등급 — 비표시 정밀값 유출 원천 차단(#173).

    절대 가격 기준은 상품군마다 달라 같은 need 후보들의 중앙값 대비 비율로만 등급화한다.
    정확한 price 는 SpringProduct 원본과 필터 경로에 남고 LLM 에는 전달하지 않는다.
    """
    if price is None or median_price is None or median_price <= 0:
        return "정보없음"
    if price <= median_price * settings.price_tier_very_cheap_ratio:
        return "매우저렴"
    if price <= median_price * settings.price_tier_cheap_ratio:
        return "저렴"
    if price >= median_price * settings.price_tier_very_pricey_ratio:
        return "매우비쌈"
    if price >= median_price * settings.price_tier_pricey_ratio:
        return "비쌈"
    return "보통"


def _group_key(product: SpringProduct, need_of: dict[int, str] | None) -> str | None:
    """priceLevel 중앙값 그룹 키를 산출한다(#173, #236).

    `need_of` 가 오면 **니즈가 그룹**이다 — 상위가 "이 후보들은 서로 다른 니즈"라고 내린 판정이라
    상품의 category 보다 권위 있다. 여기서 category 로 보조 분할하면 #173 이 고정한 니즈 경계가
    흔들린다. 그래서 `need_of.get(...) or category` 같은 `or` 배선이 아니라 **하드 분기**다 —
    `or` 로 쓰면 니즈에 미매핑된 후보(키 `None`)가 category 로 새어 #173 동작이 깨진다.

    [#236] `need_of` 가 없는 턴(대분류 leg 1개·리파인 승계·leg 없는 단일 filters 검색)은 종전에
    전 후보가 한 그룹(`None`)이라, 2만원 마우스와 150만원 노트북이 같은 중앙값을 공유해 등급이
    **적극적으로 틀렸다**. I-1 요청 categoryName 은 대분류여도 응답 `[].categoryName` 은 leaf 라
    (api-spec §4.6) 후보의 category 가 곧 상품군 파티션이다.
    빈 문자열·공백만인 category 는 `None` 과 **같은 '미상' 버킷**이다 — BE 자유 문자열이라(검증기
    없는 `str | None`) `""` 가 실제로 오는데, 별도 버킷이 되면 미상이 갈라져 각각 하한 미달로
    떨어진다. casefold 는 하지 않는다 — leaf 가 한국어라 얻는 게 없고 라틴 leaf 만 잘못 합친다.

    판정은 `is not None` 이 아니라 **truthy** 다(PR #274 리뷰) — 빈 dict 는 "니즈 매핑이 하나도
    없다"라 의미상 `None` 과 같은데, `is not None` 으로 보면 전 후보가 `need_of.get()` → `None`
    으로 묶여 **이 이슈가 고치려는 단일 그룹 버그가 그대로 재발**한다. `rerank()` 본문의
    `if need_of:`·`if need_of and per_need:` 도 truthy 라 기준을 여기에 맞춰야 프롬프트와 그룹핑이
    갈리지 않는다. 오늘은 `_merge_fanout_results` 가 `leg_of` 를 생존 id 로 잘라내(graph.py) 빈
    dict 가 만들어지지 않지만, 그 보장은 타입에도 호출부에도 강제돼 있지 않다.
    """
    if need_of:
        return need_of.get(product.product_id)
    return (product.category or "").strip() or None


def _price_medians(
    candidates: list[SpringProduct], need_of: dict[int, str] | None, settings
) -> list[float | None]:
    """후보 순서에 **1:1 정렬된** priceLevel 기준 중앙값을 낸다(#173, #236).

    반환을 그룹 키 dict 가 아니라 후보 정렬 리스트로 잡는 이유(#236): 종전엔 호출부가 `_group_key`
    를 한 번 더 불러 `group_medians[key]` 로 조회해, 두 곳의 키 산출이 어긋나는 순간 KeyError 로
    턴 전체가 미처리 예외로 죽었다(rerank 의 LLMError degrade 로도 안 잡힌다). 키를 한 번만
    산출해 후보 위치로 답을 확정하면 그 어긋남 자체가 성립 불가능해진다. productId 키도 아니다 —
    비-fanout 경로엔 productId dedup 이 없어(`_parse_search_response`·`search_catalog`) 중복이
    오면 조용히 한쪽으로 접힌다. 위치 정렬은 `zip(..., strict=True)` 로 어긋남이 즉시 터진다.

    하한 폴백은 **`need_of` 가 없는 턴에만** 쓴다(빈 dict 도 없는 것으로 본다 — `_group_key`
    docstring 참조). 니즈 경계는 상위 판정이라 그룹이 곧 정답이지만
    category 는 휴리스틱 파티션이고, leaf 가 잘게 쪼개져 유효 price 가 1건이면 중앙값이 자기
    자신이라 비율이 항상 1.0 → 전원 '보통' 으로 신호가 죽는다. 그런 그룹은 **전역 중앙값으로
    폴백하지 않고** 중앙값을 `None` 으로 둬 `_price_tier` 가 '정보없음' 을 내게 한다 — 전역
    중앙값은 후보 전체가 섞인 값이라 거기로 보내면 이 이슈가 고치려는 왜곡을 그 그룹에만 다시
    씌운다(노트북 20건이 지배하는 중앙값에 마우스 2건을 재는 꼴).
    """
    keys = [_group_key(c, need_of) for c in candidates]  # 후보 순서와 1:1 — 키 산출은 여기 한 번뿐
    grouped: dict[str | None, list[int]] = {}
    for candidate, key in zip(candidates, keys, strict=True):
        grouped.setdefault(key, [])
        if candidate.price is not None:
            grouped[key].append(candidate.price)
    medians: dict[str | None, float | None] = {
        key: median(prices) if prices else None for key, prices in grouped.items()
    }
    if not need_of:
        # 세는 대상은 멤버 수가 아니라 **유효 price 를 가진 멤버 수**다 — 중앙값이 그 표본에서만
        # 나오므로, 5건 중 1건만 가격이 있으면 중앙값이 곧 그 1건이라 같은 퇴화가 남는다.
        for key, prices in grouped.items():
            if len(prices) < settings.price_group_min_size:
                medians[key] = None
    return [medians[key] for key in keys]


def code_assisted_fallback_reasons(
    candidates: list[SpringProduct], context: CodeScoringContext
) -> dict[int, str]:
    """Render factual reasons for graph-owned search-order fallback products."""

    settings = get_settings()
    medians = _price_medians(candidates, dict(context.need_of or {}), settings)
    facts_by_id = {
        candidate.product_id: CandidateGroundingFacts(
            product_id=candidate.product_id,
            rating_level=_rating_tier(candidate, settings),
            review_level=_review_tier(candidate, settings),
            price_level=_price_tier(candidate.price, price_median, settings),
        )
        for candidate, price_median in zip(candidates, medians, strict=True)
    }
    signals_by_id = build_candidate_code_signals(candidates, facts_by_id, context)
    return {
        product_id: fallback_reason_for_signals(signals)
        for product_id, signals in signals_by_id.items()
    }


def _parse_overall_claims(
    data: dict[str, object], grounding_arm: GroundingArm
) -> tuple[dict[str, object], ...]:
    """Preserve B/C proposals, including malformed values for downstream audit."""

    if grounding_arm == "current" or "overallClaims" not in data:
        return ()
    raw_claims = data["overallClaims"]
    if not isinstance(raw_claims, list):
        return ({"__invalidShape": raw_claims},)
    return tuple(
        dict(value) if isinstance(value, dict) else {"__invalidShape": value}
        for value in raw_claims
    )


async def rerank(
    llm: LLMClient,
    *,
    query: str,
    candidates: list[SpringProduct],
    profile_summary: str | None,
    tier: str,
    expose_max: int,
    need_of: dict[int, str] | None = None,
    per_need: int | None = None,
    rating_min_requested: bool = False,
    grounding_arm: GroundingArm = "current",
    ranking_arm: RankingArm = "current",
    rrf_alpha: float = 0.65,
    rrf_k: int = 60,
    search_rank_by_id: Mapping[int, int] | None = None,
    code_scoring_context: CodeScoringContext | None = None,
) -> RerankResult:
    """Sonnet 1회 호출로 재랭킹 결과를 산출한다(후보 외 id 는 코드로 제거).

    `need_of`(productId → 니즈 이름)와 `per_need` 가 오면 **니즈별 분할 턴**이다(REQ-REC-024).
    이때만 후보에 소속 니즈를 싣고 "니즈마다 상위 N개" 지시를 덧붙인다 — 안 넘기면 LLM 은
    후보를 전역 관련도로만 정렬해 한 니즈가 상위권을 쓸 수 있고, 굶은 니즈는 하류에서
    검색순서로 보충된다. 그 보충분엔 rationale 이 없어 **근거 없는 카드**가 나가는데
    rerank 는 "정상 성공"이라 rerank_degraded 로도 드러나지 않는다(PR #212 리뷰).

    분기를 `_SYSTEM` 이 아니라 user 메시지에 둔 이유: 니즈가 없는 단일 목록 경로의 프롬프트를
    **한 글자도 바꾸지 않기 위해서**다. 이 저장소엔 프롬프트에 지시를 얹었다가 기존 성공
    케이스가 3/3 → 1/3 로 희석된 실측 전례가 있다(#198, SPEC §EX-7).
    """
    settings = get_settings()  # 티어 경계 조회 — 후보 루프 밖에서 1회(캐시 싱글턴, 관례 정합)
    price_medians = _price_medians(candidates, need_of, settings)
    cand = [
        {
            "productId": c.product_id,
            "name": c.name,
            "brand": c.brand,
            # [#236] 기준 중앙값은 need(있으면)·아니면 후보의 category 그룹 — 위에서 확정된다.
            "priceLevel": _price_tier(c.price, med, settings),
            # rating·reviewCount 는 정확한 숫자 대신 등급으로 — LLM 이 "4.8 평점"·"리뷰 128개"처럼
            # 흘려 CH-5 표시값과 어긋나는 걸 원천 차단한다(#171 PR#172). 정확한 값은 원본에 남아
            # 코드 필터·예산이 계속 쓴다. review_count==0 → 평가없음(#171 rating=0 판별 유지).
            "ratingLevel": _rating_tier(c, settings),
            "reviewLevel": _review_tier(c, settings),
            "category": c.category,
        }
        # strict — 중앙값이 후보와 1:1 로 정렬돼 있음을 여기서 강제한다(#236).
        for c, med in zip(candidates, price_medians, strict=True)
    ]
    if need_of:
        # 니즈별 분할 턴에서만 실린다 — 상품의 category(분류 경로)와 달리 "이 후보가 어느
        # 니즈를 위해 검색됐는가"라, LLM 이 니즈 경계를 알아야 균형 있게 고를 수 있다.
        for item in cand:
            need = need_of.get(item["productId"])
            if need:
                item["need"] = need

    facts_by_id = {
        int(item["productId"]): CandidateGroundingFacts(
            product_id=int(item["productId"]),
            rating_level=str(item["ratingLevel"]),
            review_level=str(item["reviewLevel"]),
            price_level=str(item["priceLevel"]),
        )
        for item in cand
    }
    code_signals_by_id = None
    if ranking_arm == "code_assisted":
        if code_scoring_context is None:
            ranks: dict[int, int] = {}
            for rank, candidate in enumerate(candidates, 1):
                ranks.setdefault(candidate.product_id, rank)
            code_scoring_context = CodeScoringContext(
                filters=ProductSearchFilters(),
                search_rank_by_id=search_rank_by_id or ranks,
                need_of=need_of,
            )
        code_signals_by_id = build_candidate_code_signals(
            candidates,
            facts_by_id,
            code_scoring_context,
        )
        for item in cand:
            signals = code_signals_by_id[int(item["productId"])]
            item["searchRank"] = signals.search_rank
            item["codeSignals"] = signals.prompt_dict()
    prof = profile_summary or "(없음)"
    # [#119] 취향은 발화를 누르지 않는다 — 프로필이 있을 때만 동점 처리 지시를 덧붙인다.
    # 프로필 없는(게스트) 경로의 프롬프트는 한 글자도 바뀌지 않는다(위 니즈 지시와 동일 규약).
    # PROFILE_SUMMARY/QUERY 순서는 바꾸지 않는다 — 게스트도 `PROFILE_SUMMARY: (없음)` 줄을
    # 받으므로 순서를 뒤집으면 잘 도는 경로까지 바뀐다. 위치 효과보다 명시 규칙이 일한다.
    profile_line = ""
    if profile_summary and settings.profile_rerank_influence == "tiebreak":
        profile_line = _PROFILE_TIEBREAK
    # 니즈 지시는 user 메시지에만 덧붙인다 — 단일 목록 경로의 프롬프트를 그대로 두기 위해서다.
    needs_line = ""
    if need_of and per_need:
        names = list(dict.fromkeys(need_of.values()))  # 등장 순서 유지 + 중복 제거
        needs_line = (
            f"NEEDS: {json.dumps(names, ensure_ascii=False)}\n"
            f"- 후보의 need 필드가 그 상품이 속한 니즈입니다. 니즈마다 상위 {per_need}개까지"
            " 균형 있게 고르세요 — 한 니즈에 몰아주지 마세요.\n"
        )
    # [#132] 평점 명시 턴에만 — 위 두 지시와 같은 규약(user 메시지 조건부 덧붙임).
    unrated_line = _UNRATED_DISCLOSURE if rating_min_requested else ""
    code_assisted_line = ""
    if ranking_arm == "code_assisted":
        code_assisted_line = (
            f"EXPOSE_MAX: {expose_max}\n"
            "CODE_CONTEXT: "
            + json.dumps(
                {
                    "totalBudgetPresent": bool(
                        code_scoring_context and code_scoring_context.total_budget is not None
                    )
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    user = (
        f"PROFILE_SUMMARY: {prof}\nQUERY: {query}\n"
        f"{profile_line}"
        f"{needs_line}"
        f"{unrated_line}"
        f"{code_assisted_line}"
        f"CANDIDATES: {json.dumps(cand, ensure_ascii=False)}"
    )

    # current와 code_assisted는 최종 노출 개수만 생성한다. scored arm은 누락 여부까지 코드가
    # 검증해야 하므로 최종 노출 상한과 무관하게 모든 후보 평가 예산을 확보한다(#631).
    # reasoning reserve도 전 후보 표를 쓰는 structured/hybrid에만 더한다.
    scored_arm = ranking_arm in ("structured", "hybrid")
    output_item_count = len(candidates) if scored_arm else expose_max
    max_tokens = (
        settings.rerank_max_tokens_base + settings.rerank_max_tokens_per_item * output_item_count
    )
    if scored_arm:
        max_tokens += settings.rerank_scoring_reasoning_token_reserve
    if ranking_arm == "code_assisted":
        system = _SYSTEM_CODE_ASSISTED
    elif ranking_arm == "current":
        system = _SYSTEM if grounding_arm == "current" else _SYSTEM_STRUCTURED_GROUNDING
    else:
        system = _SYSTEM_STRUCTURED_SCORING
    raw = await llm.complete(system=system, user=user, tier=tier, max_tokens=max_tokens)
    data = extract_json(raw)

    valid_ids = {c.product_id for c in candidates}
    ranked: list[tuple[int, str]] = []
    grounding_decisions: list[GroundingDecision] = []
    if ranking_arm == "code_assisted":
        if code_signals_by_id is None:
            raise LLMError("code-assisted signals unavailable")
        try:
            computation = parse_code_assisted_ranking(
                data.get("ranked"),
                code_signals_by_id,
                profile_available=bool(profile_summary),
                expose_max=expose_max,
            )
        except CodeAssistedSchemaError as exc:
            raise LLMError(str(exc)) from exc

        decisions_by_id = {
            decision.product_id: decision for decision in computation.grounding_decisions
        }
        for pid in computation.ordered_product_ids:
            item = computation.model_items_by_id[pid]
            decision = decisions_by_id[pid]
            rationale = (
                decision.rendered_rationale
                if grounding_arm == "validated"
                else str(item.get("rationale") or "")
            )
            ranked.append((pid, rationale))
        return RerankResult(
            ranked=ranked,
            overall_comment=str(data.get("overallComment") or ""),
            overall_claims=_parse_overall_claims(data, grounding_arm),
            grounding_decisions=list(computation.grounding_decisions),
            code_assisted_decisions=list(computation.decisions),
        )

    if scored_arm:
        try:
            computation = compute_scored_ranking(
                [candidate.product_id for candidate in candidates],
                data.get("evaluations"),
                arm=ranking_arm,
                profile_available=bool(profile_summary),
                alpha=rrf_alpha,
                k=rrf_k,
                search_rank_by_id=search_rank_by_id,
            )
        except ScoringSchemaError as exc:
            raise LLMError(str(exc)) from exc

        for pid in computation.ordered_product_ids[:expose_max]:
            item = computation.model_items_by_id.get(pid)
            if item is None:
                ranked.append((pid, ""))
                continue
            if grounding_arm == "current":
                rationale = str(item.get("rationale") or "")
            else:
                decision = validate_and_render_grounding(item, facts_by_id[pid])
                grounding_decisions.append(decision)
                rationale = (
                    decision.rendered_rationale
                    if grounding_arm == "validated"
                    else decision.model_rationale
                )
            ranked.append((pid, rationale))

        return RerankResult(
            ranked=ranked,
            overall_comment=str(data.get("overallComment") or ""),
            overall_claims=_parse_overall_claims(data, grounding_arm),
            grounding_decisions=grounding_decisions,
            ranking_decisions=list(computation.decisions),
        )

    seen: set[int] = set()
    for item in data.get("ranked") or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("productId")
        if not isinstance(pid, int) or pid not in valid_ids or pid in seen:
            continue  # 후보 외/중복 id 제거 (REQ-REC-081)
        seen.add(pid)
        if grounding_arm == "current":
            rationale = str(item.get("rationale") or "")
        else:
            decision = validate_and_render_grounding(item, facts_by_id[pid])
            grounding_decisions.append(decision)
            rationale = (
                decision.rendered_rationale
                if grounding_arm == "validated"
                else decision.model_rationale
            )
        ranked.append((pid, rationale))
        if len(ranked) >= expose_max:
            break

    if not ranked:
        raise LLMError("rerank 가 유효한 후보 id 를 내지 않음")
    return RerankResult(
        ranked=ranked,
        overall_comment=str(data.get("overallComment") or ""),
        overall_claims=_parse_overall_claims(data, grounding_arm),
        grounding_decisions=grounding_decisions,
    )
