"""decompose 카테고리 추출 파싱 테스트 (이슈 #59, 방식 A).

decompose 가 `categoryQueries: [{category, query}]` 를 `RouteDecision.category_queries`
(list[CategoryQuery])로 파싱하는지 검증한다. 실제 매핑(임베딩 보정)은 그래프 단계 소관.
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest

from app.agents.buyer.recommendation.decompose import decompose


class _FakeLLM:
    """지정 raw JSON 문자열을 fast tier 에서 돌려주는 최소 LLM."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        return self._raw

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "x"


def _raw(**over) -> str:
    base = {"intent": "recommend", "reply": "", "semanticQuery": "q", "filters": {}}
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


async def _run(raw: str, **kw):
    return await decompose(
        _FakeLLM(raw), query="발화", prior_filters=None, profile_summary=None, tier="fast", **kw
    )


async def test_semantic_query_lands_on_filters() -> None:
    """[#101] semanticQuery 는 의미검색 입력이라 filters.semantic_query 로 실려 백엔드까지 흐른다."""
    d = await _run(_raw(semanticQuery="시원한 여름 셔츠"))
    assert d.filters.semantic_query == "시원한 여름 셔츠"


async def test_semantic_query_falls_back_to_user_query_when_missing() -> None:
    """semanticQuery 누락/빈값 시 사용자 발화(query)로 폴백한다(재정렬이 항상 입력을 갖도록)."""
    d = await _run(_raw(semanticQuery=""))
    assert d.filters.semantic_query == "발화"


async def test_semantic_query_falls_back_to_prior_before_raw_query() -> None:
    """[#101 PR#166 리뷰] 정제발화("더 저렴한 걸로")로 LLM 이 semanticQuery 를 비우면, 이번 턴
    원문(query)이 아니라 **직전 턴의 semantic_query** 로 폴백한다.

    "더 저렴한 걸로" 같은 문구를 임베딩 재정렬 앵커로 쓰면 의미 신호가 오염돼(가격순 정렬도 못 하면서
    '무선 이어폰' 의미만 잃음) 이 PR 이 개선한 recall 을 되레 해친다. 가격 선호는 filters·Sonnet
    재랭킹이 처리하고, semantic_query 는 직전 턴의 상품 의미를 이어받는다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(semantic_query="무선 이어폰")
    d = await decompose(
        _FakeLLM(_raw(semanticQuery="")),
        query="더 저렴한 걸로",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.semantic_query == "무선 이어폰"  # 이번 턴 원문 아님, 직전 값 승계


async def test_semantic_query_prefers_current_category_over_prior_on_topic_change() -> None:
    """[#101 PR#166 리뷰] 주제 전환("운동화")에서 LLM 이 semanticQuery 를 비워도, 직전 상품 의미가
    아니라 **이번 턴 categoryQueries 신호**로 폴백한다.

    폴백이 prior_sq 만 보면, category 는 운동화로 바뀌었는데 재정렬 앵커는 직전 상품(원피스)이 되는
    불일치가 생긴다. categoryQueries 파생을 prior_sq 보다 우선해 앵커를 category 와 정합시킨다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(semantic_query="빨간 원피스")
    d = await decompose(
        _FakeLLM(
            _raw(semanticQuery="", categoryQueries=[{"category": "운동화", "query": "운동화"}])
        ),
        query="이번엔 운동화 찾아줘",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.semantic_query == "운동화"  # prior("빨간 원피스") 아님, 이번 카테고리 신호


async def test_semantic_query_carries_prior_when_only_category_carried_no_query() -> None:
    """[#101 PR#166 리뷰] 정제발화에서 카테고리만 승계(categoryQueries query=null)하고 semanticQuery
    를 비우면, raw_category(분류 경로 breadcrumb)가 아니라 직전 자연어 semantic_query 로 폴백한다.

    cat_signal 은 cq.query 있는 leg 만 취한다 — 순수 카테고리 승계(정제발화)는 신호가 아니다.
    raw_category("가전 > 이어폰/헤드폰")를 앵커로 쓰면 breadcrumb 문자열로 재정렬돼 dc5094b 가
    고친 정제발화 오염이 categoryQueries 경로로 재발한다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(semantic_query="가성비 좋은 무선 이어폰")
    d = await decompose(
        _FakeLLM(
            _raw(
                semanticQuery="",
                categoryQueries=[{"category": "가전 > 이어폰/헤드폰", "query": None}],
            )
        ),
        query="더 저렴한 걸로",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    # raw_category breadcrumb 아님 — 직전 자연어 semantic_query 유지.
    assert d.filters.semantic_query == "가성비 좋은 무선 이어폰"


async def test_semantic_query_not_promoted_from_single_leg_when_multi_category() -> None:
    """[#101 PR#166 리뷰] 멀티 카테고리에서 LLM 이 top-level semanticQuery 를 비우면, 한 leg 의
    구체 검색어를 전역 앵커로 승격하지 않는다.

    전역 semantic_query 는 graph 의 query-null leg 폴백으로 재사용되므로, 첫 leg 검색어("여행
    자물쇠")가 전역이 되면 무관한 leg(전자기기)의 재정렬 앵커로 샌다. 멀티면 cat_signal 을 쓰지 않고
    broad 한 원문(query)으로 폴백한다 — query 있는 멀티 leg 는 graph 가 자기 query 로 override 한다.
    """
    d = await decompose(
        _FakeLLM(
            _raw(
                semanticQuery="",
                categoryQueries=[
                    {"category": "여행용품", "query": "여행 자물쇠"},
                    {"category": "전자기기", "query": None},
                ],
            )
        ),
        query="유럽여행 준비물",
        prior_filters=None,
        profile_summary=None,
        tier="fast",
    )
    # 첫 leg "여행 자물쇠" 아님 — broad 한 원문(전 leg 관련)으로.
    assert d.filters.semantic_query == "유럽여행 준비물"


async def test_semantic_query_blank_only_treated_as_missing() -> None:
    """[#101 PR#166 리뷰] LLM 이 semanticQuery 를 공백-only('   ')로 내도 빈 값으로 보고 폴백한다.

    공백 문자열은 Python truthy 라 가드 없이 두면 폴백 체인(cat_signal/prior_sq/query)이 통째로
    건너뛰어져, 정제발화 오염(공백이 벡터 재정렬 앵커가 됨)이 dc5094b 수정에도 재발한다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(semantic_query="무선 이어폰")
    d = await decompose(
        _FakeLLM(_raw(semanticQuery="   ")),
        query="더 저렴한 걸로",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.semantic_query == "무선 이어폰"  # 공백 아님, 직전 값 유지


async def test_semantic_query_non_string_value_does_not_crash() -> None:
    """[#101 PR#166 리뷰] LLM 이 semanticQuery 를 문자열 아닌 truthy(숫자·리스트·dict)로 내도
    AttributeError 로 스트림을 깨지 않는다.

    `(123 or "").strip()` 은 123 이 truthy 라 그대로 반환돼 123.strip() 에서 AttributeError 가
    나는데, 그 예외는 except (ValidationError, ValueError, TypeError) 에 안 잡혀 SSE 를 깬다.
    형제 필드(revert·categoryQueries)처럼 isinstance(str) 가드가 필요하다(구 str() 안전장치 복원).
    """
    for bad in (123, ["여행", "이어폰"], {"k": "v"}):
        d = await decompose(
            _FakeLLM(_raw(semanticQuery=bad)),
            query="원문 발화",
            prior_filters=None,
            profile_summary=None,
            tier="fast",
        )
        assert d.filters.semantic_query == "원문 발화"  # 비문자열 무시 → 원문 폴백


async def test_blank_category_query_not_promoted_to_cat_signal() -> None:
    """[#101 PR#166 리뷰] 공백-only categoryQuery.query 는 신호가 아니다 — cat_signal 로 승격되지
    않고 폴백한다(_parse_category_queries 가 공백을 None 으로 정규화)."""
    d = await decompose(
        _FakeLLM(_raw(semanticQuery="", categoryQueries=[{"category": "운동화", "query": "   "}])),
        query="운동화 찾아줘",
        prior_filters=None,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.semantic_query == "운동화 찾아줘"  # 공백 query 아님, 원문 폴백


async def test_blank_revert_category_excluded() -> None:
    """[#101 PR#166 리뷰] 공백-only revertCategories 항목은 제외한다(LLM 공백 텍스트 = falsy 일관)."""
    d = await _run(_raw(revertCategories=["   ", "조미료", ""]))
    assert d.revert_categories == ["조미료"]


async def test_repurchase_products_parsed() -> None:
    """repurchaseProducts 상품명이 RouteDecision 재구매 신호로 정상 파싱됨을 보장한다."""
    d = await _run(_raw(repurchaseProducts=["무선 이어폰", "소금"]))
    assert d.repurchase_products == ["무선 이어폰", "소금"]


async def test_invalid_repurchase_products_excluded() -> None:
    """목록이 아니거나 비문자열·공백 항목이면 재구매 신호를 오염시키지 않음을 보장한다."""
    not_list = await _run(_raw(repurchaseProducts="무선 이어폰"))
    mixed = await _run(_raw(repurchaseProducts=[1, None, "   ", " 소금 ", ""]))
    assert not_list.repurchase_products == []
    assert mixed.repurchase_products == ["소금"]


async def test_repurchase_products_truncated_to_max() -> None:
    """repurchase_max 상한으로 긴 LLM 재구매 목록의 파싱·전달 크기를 제한함을 보장한다."""
    d = await _run(
        _raw(repurchaseProducts=["상품1", "상품2", "상품3", "상품4"]),
        repurchase_max=2,
    )
    assert d.repurchase_products == ["상품1", "상품2"]


def test_repurchase_prompt_rejects_last_recommendations_echo() -> None:
    """재구매 규칙은 PRIOR_FILTERS만 해소에 쓰고 직전 추천 목록 복사·복수 지목을 금지한다."""
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    rule = _SYSTEM.split("- repurchaseProducts:", 1)[1].split("- general:", 1)[0]
    assert "PRIOR_FILTERS" in rule
    assert "LAST_RECOMMENDATIONS" in rule and "복사하지 마세요" in rule
    assert "보통 상품 1개" in rule


async def test_attr_conditions_extracted() -> None:
    """[PR②] decompose 가 명시 속성조건을 filters.attr_conditions(축→값)로 추출한다."""
    d = await _run(_raw(attrConditions={"소재": "린넨", "핏": "오버핏"}))
    assert d.filters.attr_conditions == {"소재": "린넨", "핏": "오버핏"}


async def test_attr_conditions_absent_is_none() -> None:
    """attrConditions 누락 → None(속성 하드필터 미적용)."""
    d = await _run(_raw())
    assert d.filters.attr_conditions is None


async def test_attr_conditions_carries_prior_when_llm_omits() -> None:
    """[PR② PR#169 리뷰] LLM 이 정제발화에서 attrConditions 를 누락하면 코드가 직전 값으로 폴백한다.

    프롬프트 순응에만 의존하면 Haiku 가 정제발화("더 저렴한 걸로")에서 attrConditions 를 빠뜨릴 때
    하드 필터가 영속(thread_store) 유실된다 — semantic_query 처럼 코드 레벨 폴백으로 이중 방어한다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(attr_conditions={"소재": "린넨"})
    d = await decompose(
        _FakeLLM(_raw()),  # attrConditions 누락
        query="더 저렴한 걸로",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.attr_conditions == {"소재": "린넨"}  # 직전 하드 조건 유지


async def test_attr_conditions_merge_keeps_unmentioned_prior_axis() -> None:
    """[PR② PR#169 리뷰] 기본은 merge — LLM 이 일부 축만 내도(핏 깜빡) 이전 축이 유지된다.

    "완전 vs 일부" 폴백 고민을 없앤다: 제거는 명시 신호(attrRemovals)로만 하고, 그 외에는 prior 와
    이번 턴 값을 병합해 언급 안 한 이전 축이 조용히 유실되지 않게 한다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(attr_conditions={"소재": "린넨", "핏": "오버핏"})
    d = await decompose(
        _FakeLLM(_raw(attrConditions={"소재": "면"})),  # 핏 미언급
        query="면으로 바꿔",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.attr_conditions == {"소재": "면", "핏": "오버핏"}  # 소재 변경, 핏 유지


async def test_attr_conditions_explicit_removal() -> None:
    """[PR② PR#169 리뷰] 사용자가 명시 제거("핏은 상관없어")하면 attrRemovals 로 그 축만 뺀다.

    일부러 빼는 건 항상 사용자 발화에 드러나므로, dict 모양 추측이 아니라 명시 신호로 제거한다.
    """
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(attr_conditions={"소재": "린넨", "핏": "오버핏"})
    d = await decompose(
        _FakeLLM(_raw(attrRemovals=["핏"])),
        query="핏은 상관없어",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.attr_conditions == {"소재": "린넨"}  # 핏만 제거, 소재 유지


async def test_attr_conditions_removal_all_yields_none() -> None:
    """모든 축을 명시 제거하면 None(속성 하드필터 미적용)."""
    from app.schemas.spring import ProductSearchFilters

    prior = ProductSearchFilters(attr_conditions={"소재": "린넨"})
    d = await decompose(
        _FakeLLM(_raw(attrRemovals=["소재"])),
        query="속성 다 빼줘",
        prior_filters=prior,
        profile_summary=None,
        tier="fast",
    )
    assert d.filters.attr_conditions is None


async def test_decompose_logs_case_and_leg_summary(caplog) -> None:
    """[#198 §10] recommend 턴마다 `case`·leg 수·leg query 를 구조화 로그로 남긴다.

    **"case==3(전개 필요를 인지) 인데 legs<=1(전개 실패)" 인 턴의 빈도가 #198 의 핵심 지표**인데,
    로그가 없어 지금까지 진단 스크립트를 돌려야만 측정할 수 있었다. 운영에서 자동으로 쌓여야
    (a) 이 이슈의 우선순위를 데이터로 정하고 (b) D3 marker 튜닝(§OPEN-1)에 leg query 분포를 쓸 수 있다.
    """
    with caplog.at_level("INFO"):
        await _run(_raw(case=3, categoryQueries=[{"category": None, "query": "집들이 선물"}]))
    recs = [r for r in caplog.records if r.msg == "decompose_case"]
    assert recs, f"decompose_case 로그 없음 — 방출된 msg: {[r.msg for r in caplog.records]}"
    assert recs[0].case == 3
    assert recs[0].legs == 1
    assert recs[0].leg_queries == ["집들이 선물"]


async def test_decompose_case_log_only_for_recommend(caplog) -> None:
    """cart/general 턴은 `case` 가 의미 없으므로 남기지 않는다 — 지표 오염 방지.

    (category_unmapped 를 인프라 실패와 섞지 않는 것과 같은 취지 — 지표는 한 가지를 뜻해야 한다.)
    """
    with caplog.at_level("INFO"):
        await _run(_raw(intent="general", reply="안녕하세요"))
    assert not [r for r in caplog.records if r.msg == "decompose_case"]


async def test_case_prompt_defines_the_three_types() -> None:
    """[#198] 프롬프트가 `case` 1/2/3 의 뜻을 정의한다 — 정의가 없으면 값이 노이즈다.

    종전 프롬프트는 JSON 스키마에 `"case": 1 | 2 | 3` 만 있고 **1/2/3 이 무슨 뜻인지 어디에도
    설명이 없었다**(규칙 절 0줄). 그래서 LLM 이 뜻을 모른 채 숫자를 채웠고, 실측에서 8발화 중 6종
    오판·7종이 회차마다 흔들렸다(`"집들이 선물"` 은 3/3 Case 1 로 오분류 — Case 3 인데).
    정의를 넣은 뒤 **Case 3 판정 5발화 × 3회 = 15/15 정확·전부 일관**.

    부수적으로 **전개율까지 개선**됐다 — `"집들이 선물"` 의 `categoryQueries` 가 `legs 0/3`(발화
    복사)에서 `3/3`(구체 상품 3개)으로 뒤집혔다. "선물·준비물·필요한 것은 물건 이름이 아니다"라는
    문장이 `categoryQueries` 산출에도 작용했기 때문이다(#198 §4.1). 이 정의가 사라지면 case 신뢰성과
    전개율이 함께 퇴행하므로 회귀를 테스트로 막는다.
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    rule = _SYSTEM.split("- case:", 1)[1].split("- attrConditions", 1)[0]
    assert "상품명" in rule  # 1 의 정의
    assert "구조화" in rule  # 2 의 정의
    assert "상황" in rule and "목적" in rule  # 3 의 정의
    assert "물건 이름이 아니" in rule  # 선물·준비물 → 3 (전개율 개선의 핵심 문장)


async def test_attr_conditions_prompt_teaches_merge_and_removal() -> None:
    """[PR②] 프롬프트가 merge 기본(이전 유지)과 명시 제거(attrRemovals)를 지시한다."""
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    assert "attrConditions" in _SYSTEM and "attrRemovals" in _SYSTEM
    attr_rule = _SYSTEM.split("attrConditions", 1)[1].split("- categoryQueries", 1)[0]
    assert "유지" in attr_rule and "attrRemovals" in attr_rule


async def test_category_queries_prompt_teaches_concrete_product_expansion() -> None:
    """[#115 §6.0] 프롬프트가 목적·상황형 질의를 **구체 상품** 단위로 전개하도록 지시한다.

    실측: LLM 이 '선물용품'·'생활용품' 같은 매장 코너 이름으로 뭉개면 임베딩 앵커에 정보가 없어
    엉뚱한 leaf 로 꽂혔다('선물용품' → 취미 > 수집용품 0.2074 / '생활용품' → 고양이용품 >
    생활용품 0.1689). 반대로 구체 상품명 앵커는 16/16 정답(거리 0.046~0.217)이었다. 이 지시가
    프롬프트에서 사라지면 #115 가 그대로 재발하므로 회귀를 테스트로 막는다.
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    rule = _SYSTEM.split("- categoryQueries", 1)[1].split("- cart_add", 1)[0]
    assert "구체" in rule  # 구체적 상품 단위로 전개
    assert "코너" in rule  # 매장 코너 이름으로 뭉개지 말라는 금지 지시
    assert "홍삼" in rule  # 선물형 전개 예시(추상 라벨이 아닌 실제 상품)
    assert "지어내지" in rule  # 없는 카테고리명 창작 금지(표기 불일치로 exact 히트 0)


async def test_category_query_prompt_forbids_modifiers_and_utterance_copy() -> None:
    """[#115 §6.0] query 는 수식어·상황 설명을 뺀 **순수 상품명**이어야 한다(거리 밀림 방지).

    실측: query 에 수식어가 붙거나 발화를 그대로 복사하면 정답이어도 거리가 밀려 올라가
    거리컷(§4, 0.22)에 걸린다 — '무선 이어폰' 0.1955(통과) vs '가성비 무선 이어폰' 0.2556(드롭),
    '발 시려울 때 신을 수 있는 신발' 0.2478(드롭). 가격·평가 조건은 filters 로, 발화 전체 의미는
    semanticQuery 로 가므로 leg query 에서 수식어를 빼도 정보가 유실되지 않는다.
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    rule = _SYSTEM.split("- categoryQueries", 1)[1].split("- cart_add", 1)[0]
    assert "수식어" in rule  # 갓성비·저렴한 등은 query 에서 제외
    assert "복사" in rule  # 발화를 그대로 복사 금지(상품명으로 번역)


async def test_attr_conditions_type_and_blank_guards() -> None:
    """[PR② — PR① 교훈] 비-dict·비문자열 값·공백 키/값은 걸러 매칭 오염·크래시를 막는다."""
    # 비 dict → None (리스트·문자열)
    assert (await _run(_raw(attrConditions=["소재", "린넨"]))).filters.attr_conditions is None
    assert (await _run(_raw(attrConditions="린넨"))).filters.attr_conditions is None
    # 비문자열 값(123)·공백 값('   ')·공백 키('  ') 제외, 유효 항목만 남김
    d = await _run(_raw(attrConditions={"소재": "린넨", "핏": "   ", "용도": 123, "  ": "x"}))
    assert d.filters.attr_conditions == {"소재": "린넨"}
    # 전부 무효 → None(빈 dict 아님)
    assert (await _run(_raw(attrConditions={"핏": "  "}))).filters.attr_conditions is None


async def test_parses_single_category_query() -> None:
    """단일 카테고리 추측 → category_queries 길이 1, raw/query 매핑."""
    d = await _run(
        _raw(categoryQueries=[{"category": "가전 > 이어폰/헤드폰", "query": "무선 이어폰"}])
    )
    assert len(d.category_queries) == 1
    assert d.category_queries[0].raw_category == "가전 > 이어폰/헤드폰"
    assert d.category_queries[0].query == "무선 이어폰"


async def test_parses_multiple_category_queries() -> None:
    """상황형 → 여러 카테고리 추출."""
    d = await _run(
        _raw(
            categoryQueries=[
                {"category": "여행/캠핑 > 여행용품", "query": "여행 자물쇠"},
                {"category": "가전 > 어댑터", "query": "여행용 어댑터"},
            ]
        )
    )
    assert [c.raw_category for c in d.category_queries] == ["여행/캠핑 > 여행용품", "가전 > 어댑터"]


async def test_missing_category_queries_yields_empty() -> None:
    """categoryQueries 누락 → 빈 리스트(카테고리 신호 없음 → 그래프에서 무필터 검색, #22)."""
    d = await _run(_raw())
    assert d.category_queries == []


async def test_parses_color_filter() -> None:
    """[#100 P1] decompose 가 filters.color(색상 조건)를 파싱한다."""
    d = await _run(_raw(filters={"color": "빨강", "keyword": "원피스"}))
    assert d.filters.color == "빨강"


async def test_null_category_allowed() -> None:
    """category=null 추측 허용(query 있으면 그 leg 의 query 로 매핑해 흡수, #17)."""
    d = await _run(_raw(categoryQueries=[{"category": None, "query": "집들이 선물"}]))
    assert len(d.category_queries) == 1
    assert d.category_queries[0].raw_category is None
    assert d.category_queries[0].query == "집들이 선물"


async def test_truncates_to_fanout_max() -> None:
    """category_fanout_max 로 추출 개수를 절단한다(하드코딩 금지)."""
    many = [{"category": f"c{i} > m{i}", "query": f"q{i}"} for i in range(10)]
    d = await _run(_raw(categoryQueries=many), category_fanout_max=3)
    assert len(d.category_queries) == 3


async def test_empty_legs_do_not_consume_fanout_budget() -> None:
    """category·query 둘 다 없는 빈 leg 가 앞에 섞여도 실제 신호 leg 를 밀어내지 않는다.

    LLM 이 [{null,null} x N, {실제}...] 처럼 빈 항목을 앞에 내보내면, 원본 순서 절단(out[:max])은
    빈 항목이 fanout 예산을 먹어 뒤쪽 실제 카테고리를 잘라낸다. map_categories 는 어차피 빈 leg 를
    스킵하므로, 절단 전에 신호(raw·query) 있는 leg 만 남겨 §9 상한 의도를 지킨다(PR #73 리뷰).
    """
    cq = [
        {"category": None, "query": None},
        {"category": None, "query": None},
        {"category": "c1 > m1", "query": "q1"},
        {"category": "c2 > m2", "query": "q2"},
    ]
    d = await _run(_raw(categoryQueries=cq), category_fanout_max=2)
    assert [c.raw_category for c in d.category_queries] == ["c1 > m1", "c2 > m2"]


async def test_fanout_max_zero_truncates_to_empty() -> None:
    """fanout_max<=0(운영 설정 실수)면 정확히 0개로 절단한다 — slice 의미와 일치(PR #73 리뷰).

    append 후 체크 방식이면 첫 항목이 항상 남아 매핑의 dedup_truncate(out[:cap])와 절단 의미가
    어긋난다. 두 절단 지점을 같은 slice 규약으로 통일해 상한 전제를 지킨다.
    """
    many = [{"category": f"c{i} > m{i}", "query": f"q{i}"} for i in range(3)]
    d = await _run(_raw(categoryQueries=many), category_fanout_max=0)
    assert d.category_queries == []


async def test_system_prompt_includes_synonym_guidance() -> None:
    """[#51 B] semanticQuery 규칙에 동의어·상위어 지침이 있어야 한다 — 임베딩 rerank 가
    표현 차이(청바지=데님)를 잡도록 semanticQuery 를 의미 중심으로 풍부하게 쓰게 유도(프롬프트 회귀 가드).
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    assert "동의어" in _SYSTEM


async def test_order_status_intent_is_preserved() -> None:
    decision = await _run(_raw(intent="order_status"))
    assert decision.intent == "order_status"


def test_order_status_prompt_has_five_way_positive_and_negative_rules() -> None:
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    assert '"order_status"' in _SYSTEM
    for phrase in ("내 주문 어디까지 왔어", "배송 상태 알려줘", "최근 주문 진행 상황"):
        assert phrase in _SYSTEM
    for phrase in (
        "배송 빠른 상품 추천해줘",
        "이 상품 주문하고 싶어",
        "주문 취소 방법",
        "예전에 뭘 샀지",
    ):
        assert phrase in _SYSTEM


def test_pronoun_intent_prompt_keeps_product_requests_out_of_cart_view() -> None:
    """[#234] 상품 지시대명사는 장바구니 명시/담기 동사가 없으면 추천 레인에 남는다.

    수정 전 실 LLM N=8 프로브에서 ``그거 보여줘``는 맥락 없음 ``cart_view×8``, 직전 추천
    ``recommend×3 / cart_view×5``, pending-cart ``cart_view×8``이었다. ``그거 또 사고 싶어``도
    직전 추천·pending-cart에서 각각 ``cart_add×8``로 흔들려, FakeLLM 출력 주입 테스트로는 잡히지
    않는 프롬프트 계층 회귀를 필수 규칙 문구로 고정한다.
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    rule = _SYSTEM.split("- order_status로 분류하지 않는 예:", 1)[1].split("- recommend:", 1)[0]
    assert "cart_view로 분류하지 않는 예:" in rule
    for phrase in ("그거 보여줘", "저번에 그거 다시 보여줘", "그거 또 사고 싶어"):
        assert phrase in rule
    assert "PRIOR_FILTERS.semanticQuery" in rule
    assert "LAST_RECOMMENDATIONS" in rule
    assert "상품" in rule and "recommend" in rule
    assert "명시적 담기 동사" in rule and "cart_add" in rule


def test_pending_cart_option_answer_precedes_general_intent_ladder() -> None:
    """[#234 R1/R7] pending-cart 옵션 답변 조건은 일반 intent 사다리와 모순되지 않는다.

    라운드 2 실 LLM N=8에서 원본은 ``2번으로``를 ``cart_add×8``로 분류하고 두 번째 optionId를
    7/8 골랐지만, 1)~3) 사다리를 먼저 적용한 프롬프트는 ``cart_view×6 / cart_add×2``와 올바른
    optionId 0/8로 퇴행했다. 번호만 있는 정상 옵션 답변이 다시 사다리 밖으로 밀리지 않게 고정한다.
    동시에 PENDING_CART 자체를 옵션 답변으로 간주하던 기존 문장이 0) 단계와 모순하지 않도록,
    실제 옵션 이름·번호·순번 선택일 때만 답변으로 보는 조건도 고정한다.
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    rule = _SYSTEM.split("- order_status로 분류하지 않는 예:", 1)[1].split("- recommend:", 1)[0]
    assert rule.index("0)") < rule.index("1)")
    for phrase in ("PENDING_CART", "이름", "번호", "순번", "2번으로", "두 번째", "optionId"):
        assert phrase in rule
    assert "있으면 보통 이번 발화는 옵션 답변" not in _SYSTEM
    assert "USER_MESSAGE가 options의 이름·번호·순번을 실제로 고른" in _SYSTEM
    assert "경우에만 옵션 답변" in _SYSTEM


async def test_unknown_intent_still_falls_back_to_recommend() -> None:
    decision = await _run(_raw(intent="unknown"))
    assert decision.intent == "recommend"


# ─────────── #119 유출 관측 축 (PR #223 리뷰) ───────────


def test_filter_axes_covers_every_hard_filter_field() -> None:
    """새 하드필터 축이 생기면 관측 로그도 함께 늘어나야 한다 — 드리프트 방지.

    관측이 한 축이라도 빠지면 "회원/게스트 filters_set 대조로 프로필 유출을 잡는다"는
    검증 절차가 그 경로만 조용히 놓친다(attr_conditions 누락, PR #223 리뷰).
    """
    from app.agents.buyer.recommendation.decompose import _FILTER_AXES
    from app.schemas.spring import ProductSearchFilters

    # 후보를 거르는 조건이 아닌 필드 — 의미검색 앵커·dedup 제외목록·top-K 상한
    not_hard_filters = {"semantic_query", "exclude_product_ids", "limit"}
    assert set(_FILTER_AXES) == set(ProductSearchFilters.model_fields) - not_hard_filters


def test_filter_axes_reports_attr_conditions() -> None:
    """속성 조건도 하드필터라 관측에 잡힌다 — 빈 dict 는 '설정 안 됨'."""
    from app.agents.buyer.recommendation.decompose import _filter_axes
    from app.schemas.spring import ProductSearchFilters

    assert _filter_axes(ProductSearchFilters(attr_conditions={"소재": "면"})) == ["attr_conditions"]
    assert _filter_axes(ProductSearchFilters(attr_conditions={})) == []


def test_filter_axes_keeps_zero_but_drops_empty_containers() -> None:
    """0 은 LLM 이 실제로 내보낸 값이라 남기고, 빈 컨테이너는 설정 안 됨으로 본다."""
    from app.agents.buyer.recommendation.decompose import _filter_axes
    from app.schemas.spring import ProductSearchFilters

    assert _filter_axes(ProductSearchFilters(price_min=0)) == ["price_min"]
    assert _filter_axes(ProductSearchFilters(brand=[], keyword="")) == []


@pytest.mark.parametrize(
    ("merged", "prior", "expected"),
    [
        # "3만~5만" 다음 턴 "2만원 이하만" — 하한이 지난 턴에서 딸려왔다
        (
            {"price_min": 30000, "price_max": 20000},
            {"price_min": 30000, "price_max": 50000},
            (None, 20000),
        ),
        # "2만 이하" 다음 턴 "5만원 이상" — 이번엔 상한이 딸려왔다(같은 규칙, 반대 방향)
        ({"price_min": 50000, "price_max": 20000}, {"price_max": 20000}, (50000, None)),
        # prior 없음(한 턴에 모순) — 판별 불가라 하한을 버린다(상한은 AC-REC-08 보호 대상)
        ({"price_min": 50000, "price_max": 20000}, None, (None, 20000)),
        # 모순이 그대로 저장돼 있던 경우 — 양쪽 다 낡아 판별 불가, 같은 폴백
        (
            {"price_min": 50000, "price_max": 20000},
            {"price_min": 50000, "price_max": 20000},
            (None, 20000),
        ),
    ],
    ids=["하한이_낡음", "상한이_낡음", "prior_없음", "양쪽_낡음"],
)
def test_contradictory_price_range_keeps_only_what_was_just_said(
    merged: dict, prior: dict | None, expected: tuple
) -> None:
    """[PR #248 3차 리뷰] `price_min > price_max` 는 **방금 말한 쪽**만 남긴다.

    이 쌍은 스키마를 통과해 Spring 에 `minPrice=30000&maxPrice=20000` 으로 나가고 **오류 없는
    0건**으로만 드러나 추적이 안 된다. 스키마에서 거부하지 않는 이유는 그 `ValidationError` 가
    `LLMError` 로 통일돼 턴 전체가 오류로 끝나기 때문이다 — 사용자는 정상적인 발화를 했고 병합을
    그르친 건 LLM 인데 대화가 끊기는 건 과하다.
    """
    from app.agents.buyer.recommendation.decompose import _resolve_contradictory_price_range
    from app.schemas.spring import ProductSearchFilters

    out = _resolve_contradictory_price_range(
        ProductSearchFilters(**merged),
        ProductSearchFilters(**prior) if prior is not None else None,
    )
    assert (out.price_min, out.price_max) == expected


def test_narrowing_price_range_is_left_alone() -> None:
    """모순이 **아닌** 좁히기는 건드리지 않는다 — 사용자가 유지한 하한이 사라지면 안 된다.

    "3만~5만" 다음 턴 "그 중에 4만 이하만"은 정상적인 좁히기다. 여기서 하한까지 버리면
    1만 5천원짜리가 섞여 나와, 모순을 고치려다 멀쩡한 조건을 깨는 꼴이 된다.
    """
    from app.agents.buyer.recommendation.decompose import _resolve_contradictory_price_range
    from app.schemas.spring import ProductSearchFilters

    merged = ProductSearchFilters(price_min=30000, price_max=40000)
    prior = ProductSearchFilters(price_min=30000, price_max=50000)
    assert _resolve_contradictory_price_range(merged, prior) is merged  # 사본조차 만들지 않는다


async def test_contradictory_range_never_reaches_the_filters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """모순 구간이 `decompose` 산출에 남지 않고, 남았다는 사실이 로그에 남는다."""
    from app.schemas.spring import ProductSearchFilters

    with caplog.at_level(logging.WARNING):
        decision = await decompose(
            _FakeLLM(_raw(filters={"priceMin": 30000, "priceMax": 20000})),
            query="그 중에 2만원 이하만",
            prior_filters=ProductSearchFilters(price_min=30000, price_max=50000),
            profile_summary=None,
            tier="fast",
        )

    assert decision.filters.price_min is None and decision.filters.price_max == 20000
    assert "contradictory_price_range" in caplog.text


def test_every_parsed_key_appears_in_the_output_template() -> None:
    """[PR #248 리뷰] 파싱하는 키는 **출력 JSON 템플릿에도** 있어야 한다.

    시스템 프롬프트는 "반드시 아래 JSON 만 출력하세요"로 키 집합을 강제한다. 규칙 절에만 설명하고
    템플릿에서 빠뜨리면 두 지시가 충돌해 모델이 그 키를 누락하고, 파싱은 조용히 기본값으로
    떨어진다 — **테스트는 통과하는데 프로덕션에서만 기능이 죽는다**(실제로 `scopedToPrevious`
    가 이렇게 빠져 있었다. fake LLM 응답에 키를 직접 주입하는 테스트는 이를 잡지 못한다).

    템플릿 = `_SYSTEM` 의 첫 `{` ~ 대응하는 `}` 구간. 파싱 키 = `_parse_route` 가 `data.get(...)`
    으로 읽는 최상위 키 전부(소스에서 추출해 목록이 뒤처지지 않게 한다).
    """
    import re

    from app.agents.buyer.recommendation import decompose as mod
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    template = _SYSTEM[_SYSTEM.index("{") : _SYSTEM.index("\n}") + 2]
    source = inspect.getsource(mod)
    parsed = set(re.findall(r'data\.get\("(\w+)"', source))
    assert parsed, "파싱 키를 찾지 못했다 — 추출 정규식이 코드와 어긋났을 수 있다"

    missing = sorted(k for k in parsed if f'"{k}"' not in template)
    assert not missing, f"출력 템플릿에 없는 파싱 키: {missing}"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(True, True), ("true", False), (1, False), (None, False)],
)
async def test_buy_all_uses_strict_boolean_parsing(raw_value, expected: bool) -> None:
    decision = await _run(_raw(buyAll=raw_value))
    assert decision.buy_all is expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (50_000, 50_000),
        (0, 0),
        (-1, None),
        (None, None),
        (True, None),
        (50_000.0, None),
        ("50000", None),
        ([], None),
    ],
)
async def test_total_budget_accepts_only_nonnegative_json_integers(raw_value, expected) -> None:
    decision = await _run(_raw(totalBudget=raw_value, filters={"priceMax": 50_000}))
    assert decision.total_budget == expected
    assert decision.filters.price_max == 50_000


def test_total_budget_prompt_field_distinguishes_total_from_per_item_limit() -> None:
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    template = _SYSTEM[_SYSTEM.index("{") : _SYSTEM.index("\n}") + 2]
    assert '"totalBudget": int|null' in template
    assert '"priceScope"' not in template
    assert (
        "- totalBudget: 사용자가 **전부 합쳐서** 얼마라고 말했을 때만 그 금액(원)을 넣으세요"
        in _SYSTEM
    )
    assert "**상품 하나당** 상한이면" in _SYSTEM


def test_issue_60_prompt_keeps_measured_intent_load_bearing_rules() -> None:
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    assert (
        '  3) 그 외에 "그거"·"저번에 그거" 같은 상품 지시대명사가 있으면 항상 recommend.' in _SYSTEM
    )
    assert (
        '- JSON 출력 직전에 intent를 검산하세요. cart_view인데 USER_MESSAGE에 "장바구니"가 없으면 recommend로'
        in _SYSTEM
    )
    assert (
        "이 경계는\n  PENDING_CART가 있어도 옵션 답변이 아닌 상품 요청에 그대로 적용합니다."
        in _SYSTEM
    )
    assert (
        "- cart_add: LAST_RECOMMENDATIONS(직전 추천 목록: productId+이름)에서 사용자가 가리킨 상품의"
        in _SYSTEM
    )
