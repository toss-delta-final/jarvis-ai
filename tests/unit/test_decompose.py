"""decompose 카테고리 추출 파싱 테스트 (이슈 #59, 방식 A).

decompose 가 `categoryQueries: [{category, query}]` 를 `RouteDecision.category_queries`
(list[CategoryQuery])로 파싱하는지 검증한다. 실제 매핑(임베딩 보정)은 그래프 단계 소관.
"""

from __future__ import annotations

import json

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

    append 후 체크 방식이면 첫 항목이 항상 남아 매핑의 _dedup_truncate(out[:cap])와 절단 의미가
    어긋난다. 두 절단 지점을 같은 slice 규약으로 통일해 상한 전제를 지킨다.
    """
    many = [{"category": f"c{i} > m{i}", "query": f"q{i}"} for i in range(3)]
    d = await _run(_raw(categoryQueries=many), category_fanout_max=0)
    assert d.category_queries == []
