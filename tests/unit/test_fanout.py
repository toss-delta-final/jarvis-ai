"""멀티 카테고리 fan-out 검색·병합 (이슈 #59, DESIGN-CATEGORY-HYBRID-59 §6).

canonical 카테고리마다 Spring I-1 leg 를 병렬 실행하고 결과를 병합한다:
productId dedup + round-robin 인터리브(한 카테고리 독점 방지) + merge_cap 절단.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.graph import _merge_fanout_results
from app.agents.buyer.recommendation.state import build_condition_chips
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services.spring_client import SpringUnavailableError
from tests._fakes import FakeLLM


def _res(*product_ids: int) -> ProductSearchResult:
    products = [
        SpringProduct(
            product_id=pid, name=f"P{pid}", price=1000, rating=4.0, category="c", brand="b"
        )
        for pid in product_ids
    ]
    return ProductSearchResult(products=products, total_count=len(products))


def _ids(result: ProductSearchResult) -> list[int]:
    return [p.product_id for p in result.products]


def test_merge_interleaves_round_robin() -> None:
    """leg 순서대로 한 개씩 번갈아 뽑는다 — 한 카테고리가 앞을 독점하지 않는다."""
    merged, _ = _merge_fanout_results([(0, _res(1, 2, 3)), (1, _res(4, 5))], cap=30)
    assert _ids(merged) == [1, 4, 2, 5, 3]


def test_merge_dedups_by_product_id() -> None:
    """leg 간 중복 productId 는 최초 등장만 남긴다(round-robin 순서 기준)."""
    merged, _ = _merge_fanout_results([(0, _res(1, 2)), (1, _res(2, 3))], cap=30)
    assert _ids(merged) == [1, 2, 3]  # legB 의 2 는 legA 2 와 중복 → 드롭


def test_merge_truncates_to_cap() -> None:
    """병합 결과를 merge_cap 으로 절단한다(rerank 입력 상한)."""
    merged, _ = _merge_fanout_results([(0, _res(1, 2, 3, 4, 5))], cap=2)
    assert _ids(merged) == [1, 2]
    assert merged.total_count == 2


def test_merge_skips_empty_legs() -> None:
    """빈 leg 는 인터리브에서 건너뛴다(실패·0건 leg 가 순서를 어긋내지 않음)."""
    merged, _ = _merge_fanout_results([(0, _res()), (1, _res(1)), (2, _res())], cap=30)
    assert _ids(merged) == [1]


def test_merge_cap_zero_yields_empty() -> None:
    """cap<=0(운영 설정 실수)면 정확히 0개로 절단한다 — slice 의미와 일치(PR #73 리뷰).

    append 후 체크 방식이면 첫 상품이 항상 남아 decompose·_dedup_truncate 의 slice 절단과
    어긋난다. 세 절단 지점(_parse·merge·_dedup)을 같은 slice 규약으로 통일한다.
    """
    merged, leg_of = _merge_fanout_results([(0, _res(1, 2, 3)), (1, _res(4, 5))], cap=0)
    assert _ids(merged) == []
    assert merged.total_count == 0
    assert leg_of == {}  # 절단된 상품의 leg 정체성은 남기지 않는다


def test_merge_records_leg_of_each_survivor() -> None:
    """살아남은 상품마다 어느 leg(니즈)에서 왔는지 기록한다 (#209, REQ-REC-024).

    니즈별 목록 분할의 유일한 근거다 — 병합이 leg 를 버리면 하류에서 복원할 방법이 없다.
    """
    merged, leg_of = _merge_fanout_results([(0, _res(1, 2, 3)), (1, _res(4, 5))], cap=30)
    assert _ids(merged) == [1, 4, 2, 5, 3]
    assert leg_of == {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}


def test_merge_leg_of_uses_round_robin_first_occurrence() -> None:
    """중복 상품의 leg 는 **round-robin 최초 등장** 기준이다 — leg 순회 순서가 아니다.

    leg0 의 3번째 상품 9 와 leg1 의 1번째 상품 9 가 겹치면, 병합이 실제로 채택하는 건
    depth 0 에서 나온 leg1 쪽이다. leg 단위로 순회하며 setdefault 하면 leg0 으로 잘못 적히고,
    그 상품이 엉뚱한 니즈 목록에 들어간다.
    """
    merged, leg_of = _merge_fanout_results([(0, _res(1, 2, 9)), (1, _res(9, 8))], cap=30)
    assert _ids(merged) == [1, 9, 2, 8]
    assert leg_of[9] == 1


def test_merge_leg_of_keys_are_original_leg_indexes() -> None:
    """leg 인덱스는 **원본 legs 기준**이다 — 실패해 빠진 leg 때문에 밀리면 라벨이 어긋난다."""
    # leg 1 이 검색 실패로 빠지고 leg 0·2 만 살아남은 상황.
    merged, leg_of = _merge_fanout_results([(0, _res(1)), (2, _res(7))], cap=30)
    assert _ids(merged) == [1, 7]
    assert leg_of == {1: 0, 7: 2}


# ─────────── fan-out 오케스트레이션 (stream_recommendation §6) ───────────


def _req(message: str = "유럽여행 준비물 추천", session_id: str = "s1", thread_id: str = "t1"):
    return SimpleNamespace(session_id=session_id, thread_id=thread_id, message=message)


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


async def _committed_observer(request, identity, observer=None):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    if observer is None:
        observer = SimpleNamespace(
            request_id="unit-request",
            record_model_call=lambda *_: None,
        )
    observer.context_id = context.context_id
    return observer


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity, kwargs.pop("observer", None))
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
        yield frame


class _RecordingPush:
    def __init__(self) -> None:
        self.pushes: list = []

    async def __call__(self, push) -> bool:
        self.pushes.append(push)
        return True


def _two_leg_mapper():
    async def _map(*, category_queries, utterance, settings):
        return [("여행/캠핑 > 여행용품", "파우치"), ("가전 > 어댑터", "어댑터")]

    return _map


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


async def test_fanout_searches_each_canonical_category() -> None:
    """category_legs 2개 → 카테고리마다 leg 검색(§6). leg 마다 canonical·query·per_cat_limit 적용."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        return _res(101, 102) if "여행용품" in filters.category else _res(201)

    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),
        )
    )
    by_cat = {f.category: f for f in calls}
    assert set(by_cat) == {"여행/캠핑 > 여행용품", "가전 > 어댑터"}
    # [#51] canonical category 가 있으면 keyword(상품명 LIKE)는 드롭한다 — leg query 는
    # semantic_query 로 흘러 임베딩 rerank 를 담당(동의어가 retrieval 을 원천 배제하지 않게).
    # limit = category_fanout_per_cat_limit(기본 10).
    assert by_cat["여행/캠핑 > 여행용품"].keyword is None
    assert by_cat["여행/캠핑 > 여행용품"].semantic_query == "파우치"
    assert by_cat["가전 > 어댑터"].keyword is None
    assert by_cat["가전 > 어댑터"].semantic_query == "어댑터"
    assert by_cat["가전 > 어댑터"].limit == 10


async def test_fanout_merges_results_from_all_legs() -> None:
    """여러 leg 결과가 병합돼 rerank·push 후보에 모두 오른다(한 카테고리 독점 아님)."""

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102) if "여행용품" in filters.category else _res(201)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=push,
            map_categories=_two_leg_mapper(),
        )
    )
    pushed = set(push.pushes[0].lists[0].product_ids)
    assert 201 in pushed  # 두 번째 카테고리 leg 결과도 병합돼 노출
    assert pushed & {101, 102}  # 첫 카테고리 leg 결과도 포함


async def test_fanout_all_legs_fail_emits_search_failed() -> None:
    """모든 leg 가 Spring 실패 → SEARCH_FAILED(§6 전량 실패)."""

    async def _search(filters, exclude_product_ids=None):
        raise SpringUnavailableError("down")

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),
        )
    )
    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["code"] == "SEARCH_FAILED"


async def test_fanout_single_category_preserves_candidate_width() -> None:
    """단일 카테고리(leg 1개)는 후보 폭을 좁히지 않게 per_cat_limit(10) 이 아니라 merge_cap(30) 을
    size 로 쓴다. 매핑된 단일 질의(leg 1개)도 fan-out 경로를 타므로, 기존 단일검색(limit 30) 대비
    rerank 입력 후보가 줄면 추천 품질이 조용히 저하된다(PR #73 리뷰)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        return _res(101, 102)

    async def _one_leg(*, category_queries, utterance, settings):
        return [("가전 > 이어폰/헤드폰", "무선 이어폰")]

    await _collect(
        run_buyer_turn(
            _req("무선 이어폰 추천"),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_one_leg,
        )
    )
    assert len(calls) == 1
    # merge_cap(기본 30) — per_cat_limit(10) 로 좁히지 않는다(단일 = rerank 입력 예산 전량)
    assert calls[0].limit == 30


async def test_fanout_partial_leg_failure_uses_survivors() -> None:
    """일부 leg 만 실패하면 살아남은 leg 결과로 계속 진행한다(§6 leg 별 실패 흡수)."""

    async def _search(filters, exclude_product_ids=None):
        if "어댑터" in filters.category:
            raise SpringUnavailableError("leg down")
        return _res(101, 102)

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=push,
            map_categories=_two_leg_mapper(),
        )
    )
    assert "error" not in [e["type"] for e in events]
    assert set(push.pushes[0].lists[0].product_ids) <= {101, 102}


async def test_fanout_leg_unexpected_exception_isolated_not_stream_crash() -> None:
    """leg 하나가 SpringUnavailable 아닌 예상외 예외를 던져도 그 leg 만 드롭하고 스트림은 계속한다(PR #73 리뷰).

    _leg 이 SpringUnavailableError 만 삼키면 다른 예외가 gather → _run_search → stream 상위로
    전파돼 SSE 스트림 전체가 미처리 예외로 죽는다(주석이 약속한 leg 격리 미보장). 예상외 예외도
    그 leg 만 격리해 살아남은 leg 로 계속해야 한다 — category_mapping fan-out(return_exceptions)과 정합.
    """

    async def _search(filters, exclude_product_ids=None):
        if "어댑터" in filters.category:
            raise RuntimeError("unexpected leg bug")  # SpringUnavailable 아님
        return _res(101, 102)

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=push,
            map_categories=_two_leg_mapper(),
        )
    )
    assert "error" not in [e["type"] for e in events]  # 스트림 안 죽음
    assert set(push.pushes[0].lists[0].product_ids) <= {101, 102}  # 살아남은 leg 결과로 진행


# ─────────── conditions 칩 멀티 카테고리 반영 (PR #73 리뷰 #6) ───────────


def test_condition_chips_multi_category_joined_string_value() -> None:
    """멀티 카테고리는 카테고리 칩 1개에 조인 문자열 값으로 담는다 — api-spec §3.1 예시가 value 를
    스칼라 문자열로 명시하므로(계약 정합) 리스트가 아니라 문자열로 전체를 표현한다(칩 제거 왕복 유지)."""
    cats = ["여행/캠핑 > 여행용품", "가전 > 어댑터", "패션 > 의류"]
    chips = build_condition_chips(ProductSearchFilters(category=cats[0]), categories=cats)
    cat_chips = [c for c in chips if c.field == "category"]
    assert len(cat_chips) == 1
    assert isinstance(cat_chips[0].value, str)  # 스칼라 문자열 — 계약(§3.1) 정합
    assert all(c in cat_chips[0].value for c in cats)  # 값에 전체 포함
    assert all(c in cat_chips[0].label for c in cats)


def test_condition_chips_single_category_keeps_string_value() -> None:
    """단일 카테고리는 기존처럼 문자열 값·라벨을 유지한다(계약 무변경)."""
    chips = build_condition_chips(
        ProductSearchFilters(category="가전 > 이어폰"), categories=["가전 > 이어폰"]
    )
    cat = next(c for c in chips if c.field == "category")
    assert cat.value == "가전 > 이어폰"
    assert cat.label == "카테고리 · 가전 > 이어폰"


def test_condition_chips_fallback_to_filters_when_no_categories() -> None:
    """categories 미지정(비-fan-out 경로)이면 filters.category 로 파생한다(기존 동작 보존)."""
    chips = build_condition_chips(ProductSearchFilters(category="가전 > TV"))
    cat = next(c for c in chips if c.field == "category")
    assert cat.value == "가전 > TV"


async def test_fanout_conditions_reflect_all_categories() -> None:
    """멀티 fan-out 시 conditions 이벤트가 대표 1개가 아니라 검색한 카테고리 전체를 표시한다(#6)."""

    async def _search(filters, exclude_product_ids=None):
        return _res(101) if "여행용품" in filters.category else _res(201)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),
        )
    )
    conditions = next(e for e in events if e["type"] == "conditions")["data"]
    cat_chips = [c for c in conditions["chips"] if c["field"] == "category"]
    assert len(cat_chips) == 1
    val = cat_chips[0]["value"]
    assert isinstance(val, str)  # 스칼라 문자열(계약 정합)
    assert "여행/캠핑 > 여행용품" in val and "가전 > 어댑터" in val


# ─────────── 멀티턴 카테고리 승계 (PR #73 리뷰 #10) ───────────


async def test_multiturn_prior_category_fed_to_decompose_prompt() -> None:
    """이전 턴 카테고리가 다음 턴 decompose 프롬프트(PRIOR_FILTERS)에 실려, LLM 이 승계할 수 있다.

    카테고리가 filters→categoryQueries 로 분리됐지만, 저장된 filters.category 는 여전히 다음 턴
    프롬프트에 실린다 — LLM 이 "PRIOR_FILTERS 병합" 규칙으로 이어붙인다(price/brand 와 동일한
    LLM 주도 메커니즘, PR #73 #10 (a)). 배선(프롬프트 주입)을 검증한다.
    """

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    async def _map_leg(*, category_queries, utterance, settings):
        return [("여행 > 여행용품", "파우치")]

    llm = FakeLLM()
    # 턴 1 — 카테고리 확립·저장
    await _collect(
        run_buyer_turn(
            _req(thread_id="tm"),
            _member(),
            llm=llm,
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map_leg,
        )
    )
    # 턴 2 — 직전 카테고리가 decompose 프롬프트(PRIOR_FILTERS)에 실렸는지 확인
    llm.calls.clear()
    await _collect(
        run_buyer_turn(
            _req(thread_id="tm", message="더 저렴한 걸로"),
            _member(),
            llm=llm,
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map_leg,
        )
    )
    decompose_prompts = [u for (m, u) in llm.calls if m == "fast"]
    assert decompose_prompts and "여행 > 여행용품" in decompose_prompts[0]


async def test_mapper_failure_is_logged(caplog) -> None:
    """mapper() 예외 시 최후 방어 경로가 관측 로그를 남긴다(PR #73 #11 — 무로그 삼킴 방지)."""

    async def _boom(*, category_queries, utterance, settings):
        raise RuntimeError("boom")

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    with caplog.at_level("WARNING"):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_boom,
            )
        )
    assert any(r.msg == "category_map_failed" for r in caplog.records)


async def test_mapper_failure_degrades_to_null_not_raw() -> None:
    """mapper() 호출 자체가 예외면 raw(DB 미검증 추측)를 신뢰하지 않고 빈 legs 로 degrade한다 —
    filters.category=None(canonical-or-null 불변식). embed/DB 하드실패(§5·#20, 매퍼 내부에서 빈 legs
    degrade)와 마찬가지로 호출 버그엔 raw 를 믿을 근거가 없어, 미검증 원문이 Spring·칩·멀티턴에 안 새게(PR #73 리뷰)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _boom(*, category_queries, utterance, settings):
        raise RuntimeError("mapper bug")

    d = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "filters": {},
        "categoryQueries": [{"category": "미검증_추측카테고리", "query": "q"}],
    }
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=d),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_boom,
        )
    )
    assert calls[0] is None  # raw "미검증_추측카테고리" 가 검색에 안 실림


def _garbage_mapper():
    """가드 우회 검증용 카나리: raw 있으면 그대로, 없으면 눈에 띄는 garbage leg 를 낸다.

    가드가 매핑을 우회하면 이 매퍼는 호출조차 안 되므로 garbage 가 검색에 실리지 않는다 — 실제
    매퍼는 신호 없는 턴엔 빈 legs 를 내지만(#22), 여기선 prior 승계를 또렷이 검증하려 garbage 를 쓴다.
    """

    async def _map(*, category_queries, utterance, settings):
        legs = [(q.raw_category, q.query) for q in category_queries if q.raw_category]
        return legs or [("매퍼우회검증_garbage카테고리", None)]

    return _map


async def _run_two_turns(turn2_decompose: dict) -> list:
    """턴1(카테고리 확립)→턴2(turn2_decompose) 를 돌리고 각 턴의 검색 카테고리를 반환한다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    d1 = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
        "categoryQueries": [{"category": "여행 > 여행용품", "query": "파우치"}],
    }
    await _collect(
        run_buyer_turn(
            _req(thread_id="tm"),
            _member(),
            llm=FakeLLM(decompose=d1),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_garbage_mapper(),
        )
    )
    await _collect(
        run_buyer_turn(
            _req(thread_id="tm", message="더 저렴한 걸로"),
            _member(),
            llm=FakeLLM(decompose=turn2_decompose),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_garbage_mapper(),
        )
    )
    return calls


async def test_multiturn_empty_queries_carries_prior_not_utterance() -> None:
    """리파인 턴에 LLM 이 categoryQueries 를 비우면, 매퍼 출력으로 오염되지 않고
    prior.category(이미 canonical)를 그대로 승계해 검색에 실린다(PR #73 리뷰 #12).

    가드가 없으면 turn2 는 빈 queries → 매퍼가 prior 아닌 값을 내어(실제론 빈 legs→category=None,
    이 테스트 카나리로는 garbage) 리파인인데 직전 카테고리가 풀려버린다.
    """
    calls = await _run_two_turns({"intent": "recommend", "reply": "", "case": 2, "filters": {}})
    assert calls[0] == "여행 > 여행용품"  # 턴 1
    assert calls[-1] == "여행 > 여행용품"  # 턴 2 도 prior 승계(매퍼 garbage 아님)


async def test_multiturn_new_situational_query_not_hijacked_by_prior() -> None:
    """이전 카테고리가 있어도 새 상황형 질의(raw=null 이지만 유의미한 query)는 prior 로 덮지 않고
    매핑한다 — query 가 있으면 검색 의도가 있는 것이라 fan-out 이 동작해야 한다(PR #73 리뷰 #19).

    가드가 raw 만 보면(query 무시) 신규 상황형 질의를 "신호 없음"으로 오판해 prior 로 하이재킹 —
    이슈 #59 가 풀려던 문제(엉뚱한 카테고리로 검색)가 멀티턴에서 재발한다.
    """
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _map(*, category_queries, utterance, settings):
        # raw 있으면 그대로, null-raw+query 는 그 query 로 canonical 매핑(테스트용 lookup)
        qmap = {"여행 파우치": "여행 > 여행용품"}
        legs = []
        for q in category_queries:
            if q.raw_category:
                legs.append((q.raw_category, q.query))
            elif q.query:
                legs.append((qmap.get(q.query, "미상"), q.query))
        return legs

    d1 = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "filters": {},
        "categoryQueries": [{"category": "가전 > 이어폰", "query": "이어폰"}],
    }
    await _collect(
        run_buyer_turn(
            _req(thread_id="tx"),
            _member(),
            llm=FakeLLM(decompose=d1),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
        )
    )
    # 턴 2 — 새 상황형: raw 는 null 이지만 유의미한 query
    d2 = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "filters": {},
        "categoryQueries": [{"category": None, "query": "여행 파우치"}],
    }
    await _collect(
        run_buyer_turn(
            _req(thread_id="tx", message="유럽여행 준비물 뭐 사야해?"),
            _member(),
            llm=FakeLLM(decompose=d2),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
        )
    )
    assert calls[0] == "가전 > 이어폰"  # 턴 1
    assert calls[-1] == "여행 > 여행용품"  # 턴 2: 새 query 로 매핑됨(prior "이어폰" 하이재킹 아님)


# ─────────── 미검증 category 유출 차단 (PR #73 리뷰 #13/#15/#16) ───────────


async def test_empty_legs_clears_unvalidated_filters_category() -> None:
    """매핑 결과가 없으면(category_legs 빈) LLM 이 echo 한 미검증 filters.category 를 비운다 —
    canonical 아닌 원문이 Spring 단일검색 fallback 으로 새지 않게(PR #73 #13/#15)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _map_empty(*, category_queries, utterance, settings):
        return []  # 매핑 전량 실패(미시드·하드실패)

    # decompose 가 구식 습관으로 filters.category 를 echo
    d = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {"category": "미검증_원문카테고리"},
    }
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=d),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map_empty,
        )
    )
    assert calls[0] is None  # 미검증 category 가 검색에 안 실림


def test_condition_chips_empty_categories_no_fallback() -> None:
    """categories=[] (fan-out 매핑 결과 없음)이면 filters.category 로 폴백하지 않는다 — 미검증
    category 가 칩에 새지 않게. None(미지정)만 filters.category 파생(PR #73 #16)."""
    chips = build_condition_chips(ProductSearchFilters(category="미검증"), categories=[])
    assert not any(c.field == "category" for c in chips)
    # 미지정(None)은 기존대로 filters.category 파생 유지
    chips2 = build_condition_chips(ProductSearchFilters(category="가전 > TV"))
    assert any(c.field == "category" and c.value == "가전 > TV" for c in chips2)


# ── #198 목적·상황형 발화의 상품 전개 배선 (DESIGN-NEEDS-EXPANSION-198 §4·§6·§7) ──


def _expansion_probe():
    """전개 호출 여부를 기록하는 주입형 전개기 — 호출되면 고정 상품 목록을 낸다."""
    seen: list[str] = []

    async def _expand(utterance, **_):
        seen.append(utterance)
        return ["디퓨저", "식기 세트", "핸드워시 세트"]

    return seen, _expand


async def _run_recommend(message: str, decompose: dict, *, expand=None, **kw) -> list:
    """단일 턴을 돌리고 각 검색 leg 의 category 를 반환한다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast"):
        # leg query 를 그대로 canonical 처럼 흘려 전개 결과가 검색까지 도달하는지 본다.
        return [(q.query, q.query) for q in category_queries if q.query]

    await _collect(
        run_buyer_turn(
            _req(message=message),
            _member(),
            llm=FakeLLM(decompose=decompose),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
            expand_needs=expand,
            **kw,
        )
    )
    return calls


async def test_purpose_utterance_is_expanded_into_products() -> None:
    """[#198] 목적형 발화가 전개되어 **구체 상품**이 검색 leg 이 된다 — 이 이슈의 목표.

    종전: `['집들이 선물']` 이 그대로 leg 이 되어 매핑 불가(거리컷 드롭) → 카테고리 없이 검색.
    """
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "집들이 선물로 뭐 사갈까",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "집들이 선물"}],
        },
        expand=expand,
    )
    assert seen == ["집들이 선물로 뭐 사갈까"]  # 발화가 전개기로 전달됨
    assert calls == ["디퓨저", "식기 세트", "핸드워시 세트"]  # 전개 결과가 fan-out 검색까지 도달


async def test_normal_product_utterance_is_not_expanded() -> None:
    """단일 상품 질의는 전개하지 않는다 — 불필요한 LLM 호출·엉뚱한 확장 방지(§4 정밀도 우선)."""
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "청바지",
        {
            "intent": "recommend",
            "reply": "",
            "case": 1,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "청바지"}],
        },
        expand=expand,
    )
    assert seen == []  # 전개 미호출
    assert calls == ["청바지"]


async def test_refine_turn_carries_prior_and_is_never_expanded() -> None:
    """[회귀 방지] 리파인 턴("더 저렴한 걸로")은 **전개하지 않고** 직전 카테고리를 승계한다.

    D1(`no_legs`) 조건과 멀티턴 승계 조건이 겹친다 — 둘 다 "이번 턴 카테고리 신호 없음"이다.
    전개를 승계 가드보다 먼저 놓으면 리파인 턴이 엉뚱한 상품 목록으로 바뀌어 직전 맥락이 날아간다
    (PR #73 #12/#19 가 세운 승계 규약이 반대 방향으로 깨진다). 전개는 **승계 대상이 아닐 때만**.
    """
    seen: list[str] = []

    async def _expand(utterance, **_):
        seen.append(utterance)
        return ["엉뚱한상품A", "엉뚱한상품B"]

    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    d1 = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "filters": {},
        "categoryQueries": [{"category": "여행 > 여행용품", "query": "파우치"}],
    }
    d2 = {"intent": "recommend", "reply": "", "case": 2, "filters": {}}  # 신호 없음
    for msg, d in (("여행 파우치", d1), ("더 저렴한 걸로", d2)):
        await _collect(
            run_buyer_turn(
                _req(thread_id="tx", message=msg),
                _member(),
                llm=FakeLLM(decompose=d),
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_garbage_mapper(),
                expand_needs=_expand,
            )
        )
    assert seen == []  # 리파인 턴에 전개 호출이 없어야 한다
    assert calls[-1] == "여행 > 여행용품"  # 직전 카테고리 승계 유지


async def test_expansion_failure_keeps_original_legs() -> None:
    """전개가 실패하면(빈 리스트) `decompose` 원본 legs 를 그대로 쓴다 — 후퇴 없음(§7).

    전개는 **개선 시도**이며, 실패가 기존 경로를 악화시켜서는 안 된다. 최악이 "지금과 동일".
    """

    async def _expand_fail(utterance, **_):
        return []

    calls = await _run_recommend(
        "집들이 선물로 뭐 사갈까",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "집들이 선물"}],
        },
        expand=_expand_fail,
    )
    assert calls == ["집들이 선물"]  # 원본 leg 유지(종전 동작)


async def test_category_agnostic_case2_is_never_expanded() -> None:
    """[PR #203 리뷰] case 2(구조화 조건만) 발화는 전개하지 않는다 — 무필터 계약 보존(#22·#162).

    `"5만원 이하 아무거나"` 도 `categoryQueries` 가 비어 D1 조건에 걸린다. 전개 LLM 은 "최소 2개"를
    강제하므로 목적이 없는 입력에도 상품명을 지어내고, 그것이 legs 를 교체하면 `filters.category` 가
    채워져 "카테고리 무관·가격만 필터"라는 사용자 의도가 파괴된다. #162 가 개선할 경로이기도 하다.
    """
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "5만원 이하 아무거나",
        {
            "intent": "recommend",
            "reply": "",
            "case": 2,  # 구조화 조건만 — 좁히면 안 되는 질의
            "filters": {"priceMax": 50000},
            "categoryQueries": [],
        },
        expand=expand,
    )
    assert seen == []  # 전개 미호출
    assert calls == [None]  # 카테고리 없이(무필터) 검색


async def test_raw_only_leg_is_not_replaced_by_expansion() -> None:
    """[PR #203 리뷰] `category` 만 있고 `query=null` 인 leg 은 신호이므로 전개로 교체되지 않는다.

    저장소 규약은 `raw_category or query` 다. query 만 보면 D1 이 오탐해 정상 분류된 카테고리를
    지어낸 상품 목록으로 통째로 교체한다.
    """
    seen, expand = _expansion_probe()
    await _run_recommend(
        "무선 이어폰 추천해줘",
        {
            "intent": "recommend",
            "reply": "",
            "case": 1,
            "filters": {},
            "categoryQueries": [{"category": "음향가전", "query": None}],
        },
        expand=expand,
    )
    assert seen == []  # 전개 미호출 — raw 가 신호로 인정됨


class _ProbeObserver:
    """그래프가 observer 에 실제로 쓰는 두 면만 갖는 스텁 — request_id 조회 + 모델 호출 기록."""

    request_id = "req-probe"

    def __init__(self) -> None:
        self.models: list[str] = []

    def record_model_call(
        self, model: str, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        self.models.append(model)


async def test_expander_receives_observer_for_model_call_logging() -> None:
    """[PR #203 리뷰] 그래프는 `observer` 를 전개기까지 내려보낸다 — §6.3 모델 호출 기록의 전제.

    전개는 **조건부 +1 LLM 호출**이고(정본 SPEC-RECOMMEND-001 AC-REC-37·§비기능 `2 + 1`),
    `chat_request` 로그의 `model`/토큰 합산이 그 호출을 담아야 비용·사용량 집계가 맞는다. 기록은
    LLM 을 실제로 쓰는 `_llm_expand` 가 하므로(방식 B·C 전개기에 유령 호출을 남기지 않기 위해),
    그래프의 책임은 **observer 를 seam 으로 전달**하는 것이다. 이 배선이 끊기면 기록이 조용히 사라진다.
    """
    got: list = []

    async def _expand(utterance, *, observer=None, **_):
        got.append(observer)
        return ["디퓨저", "식기 세트"]

    observer = _ProbeObserver()
    calls = await _run_recommend(
        "집들이 선물로 뭐 사갈까",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "집들이 선물"}],
        },
        expand=_expand,
        observer=observer,
    )
    assert calls == ["디퓨저", "식기 세트"]  # 전개가 실제로 발동한 턴
    assert got == [observer]  # 같은 observer 가 seam 까지 도달했다


# ─────────── 니즈별 목록 분할 (#209, REQ-REC-024 / api-spec §4.2 PICK_ONE×N) ───────────


def _case3_decompose() -> dict:
    """목적·상황형 발화 — case 3 + 니즈 2개(파우치·어댑터)."""
    return {
        "intent": "recommend",
        "reply": "",
        "case": 3,
        "semanticQuery": "유럽여행 준비물",
        "categoryQueries": [
            {"category": None, "query": "파우치"},
            {"category": None, "query": "어댑터"},
        ],
        "filters": {},
    }


def _needs_llm(ranked: list[dict]) -> FakeLLM:
    return FakeLLM(
        decompose=_case3_decompose(),
        rerank={"ranked": ranked, "overallComment": "여행 준비물이에요"},
    )


async def _leg_search(filters, exclude_product_ids=None):
    """파우치 leg → 101·102·103, 어댑터 leg → 201·202·203."""
    return _res(101, 102, 103) if "여행용품" in (filters.category or "") else _res(201, 202, 203)


async def _run_case3(llm, push, **kwargs):
    return await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_leg_search,
            push_fn=push,
            map_categories=_two_leg_mapper(),
            **kwargs,
        )
    )


async def test_case3_pushes_one_list_per_need() -> None:
    """case 3 + 니즈 2개 → 목록 2건, 니즈마다 자기 상품만 담고 label 은 니즈 이름이다.

    종전엔 두 니즈가 한 묶음으로 병합돼 파우치 후보와 어댑터 후보가 같은 카드 목록에 섞였다.
    """
    push = _RecordingPush()
    llm = _needs_llm(
        [
            {"productId": 101, "rationale": "수납이 좋아요"},
            {"productId": 201, "rationale": "220V 지원이에요"},
            {"productId": 102, "rationale": "가벼워요"},
            {"productId": 202, "rationale": "컴팩트해요"},
        ]
    )

    events = await _run_case3(llm, push)

    sent = push.pushes[0]
    assert sent.list_type == "PICK_ONE"  # 니즈별 = 각 목록 안에서 하나를 고른다
    assert len(sent.lists) == 2
    assert [entry.label for entry in sent.lists] == ["파우치", "어댑터"]
    # 니즈 경계가 지켜진다 — 파우치 목록에 어댑터 상품이 섞이지 않는다.
    assert all(pid < 200 for pid in sent.lists[0].product_ids)
    assert all(pid >= 200 for pid in sent.lists[1].product_ids)
    # rerank 순서는 목록 안에서 보존된다.
    assert sent.lists[0].product_ids[:2] == [101, 102]
    assert sent.lists[1].product_ids[:2] == [201, 202]
    # listId 는 목록마다 다르다 — 멱등 키 (recommendationRequestId, listId) 충돌 금지(§4.2).
    assert len({entry.list_id for entry in sent.lists}) == 2

    ready = next(e for e in events if e["type"] == "products.ready")["data"]
    assert ready["listIds"] == [entry.list_id for entry in sent.lists]


async def test_case3_reasons_are_scoped_to_their_list() -> None:
    """근거는 그 상품이 속한 목록에만 실린다 — 목록 간 reason 누수 금지(§4.2 productId 키잉)."""
    push = _RecordingPush()
    llm = _needs_llm(
        [
            {"productId": 101, "rationale": "수납이 좋아요"},
            {"productId": 201, "rationale": "220V 지원이에요"},
        ]
    )

    await _run_case3(llm, push)

    pouch, adapter = push.pushes[0].lists
    assert {r.product_id for r in pouch.reasons} <= set(pouch.product_ids)
    assert {r.product_id for r in adapter.reasons} <= set(adapter.product_ids)
    assert {r.product_id: r.reason for r in pouch.reasons}[101] == "수납이 좋아요"
    assert {r.product_id: r.reason for r in adapter.reasons}[201] == "220V 지원이에요"


async def test_case3_applies_expose_max_per_list_not_globally(monkeypatch) -> None:
    """상한은 목록마다 걸린다 — 전역 절단이면 두 목록 합이 expose_max 를 못 넘는다 (REQ-REC-021/024)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 2)
    push = _RecordingPush()
    # 니즈를 번갈아 낸 랭킹 — 어느 한 니즈가 상위를 독식하지 않은 정상 분포.
    llm = _needs_llm(
        [
            {"productId": 101, "rationale": "a"},
            {"productId": 201, "rationale": "d"},
            {"productId": 102, "rationale": "b"},
            {"productId": 202, "rationale": "e"},
            {"productId": 103, "rationale": "c"},
            {"productId": 203, "rationale": "f"},
        ]
    )

    await _run_case3(llm, push)

    lists = push.pushes[0].lists
    assert [len(entry.product_ids) for entry in lists] == [2, 2]  # 전역 상한이었다면 합이 2다
    assert lists[0].product_ids == [101, 102]
    assert lists[1].product_ids == [201, 202]


async def test_case3_tops_up_a_starved_need_to_expose_min(monkeypatch) -> None:
    """랭킹이 한 니즈에 쏠려도 굶은 니즈는 자기 leg 의 검색순서로 expose_min 까지 채운다.

    전역 보정이면 이미 총량이 차 있어 아무것도 채우지 않고, 그 니즈 목록은 비거나 1건이 된다 —
    `PICK_ONE` 인데 고를 것이 없는 목록이다(REQ-REC-096 v0.11.0 개정 근거).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 2)
    monkeypatch.setattr(settings, "expose_max", 3)
    push = _RecordingPush()
    llm = _needs_llm(  # 파우치 쪽으로 완전히 쏠린 랭킹 — 어댑터는 201 하나뿐
        [
            {"productId": 101, "rationale": "a"},
            {"productId": 102, "rationale": "b"},
            {"productId": 103, "rationale": "c"},
            {"productId": 201, "rationale": "d"},
        ]
    )

    await _run_case3(llm, push)

    pouch, adapter = push.pushes[0].lists
    assert pouch.product_ids == [101, 102, 103]
    # 201 은 랭킹에서, 202 는 어댑터 leg 검색순서 보충 — 다른 니즈(10x)가 섞이지 않는다.
    assert adapter.product_ids == [201, 202]


async def test_case3_drops_needs_with_no_surviving_candidate() -> None:
    """후보가 하나도 안 남은 니즈는 목록을 만들지 않는다 — 빈 목록은 보내지 않는다(§4.2)."""

    async def _one_empty_leg(filters, exclude_product_ids=None):
        return _res(101, 102) if "여행용품" in (filters.category or "") else _res()

    push = _RecordingPush()
    llm = _needs_llm([{"productId": 101, "rationale": "수납이 좋아요"}])

    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_one_empty_leg,
            push_fn=push,
            map_categories=_two_leg_mapper(),
        )
    )

    lists = push.pushes[0].lists
    assert len(lists) == 1
    assert lists[0].label == "파우치"


async def test_single_need_still_sends_one_list() -> None:
    """니즈가 1개면 종전대로 목록 1건이다 — 분할은 니즈가 여럿일 때만 의미가 있다."""

    def _one_leg_mapper():
        async def _map(*, category_queries, utterance, settings):
            return [("여행/캠핑 > 여행용품", "파우치")]

        return _map

    push = _RecordingPush()
    llm = _needs_llm([{"productId": 101, "rationale": "수납이 좋아요"}])

    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_leg_search,
            push_fn=push,
            map_categories=_one_leg_mapper(),
        )
    )

    assert len(push.pushes[0].lists) == 1


async def test_non_case3_multi_leg_stays_single_list() -> None:
    """case 3 이 아니면 leg 이 여럿이어도 목록 1건이다 — 니즈 전개가 일어난 턴만 분할한다."""
    push = _RecordingPush()
    llm = FakeLLM(  # DEFAULT_DECOMPOSE = case 2
        rerank={
            "ranked": [
                {"productId": 101, "rationale": "a"},
                {"productId": 201, "rationale": "b"},
            ],
            "overallComment": "추천이에요",
        }
    )

    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_leg_search,
            push_fn=push,
            map_categories=_two_leg_mapper(),
        )
    )

    assert len(push.pushes[0].lists) == 1
    assert push.pushes[0].lists[0].label is None


async def test_case3_split_does_not_add_llm_calls() -> None:
    """니즈별 분할은 LLM 호출을 늘리지 않는다 — 전역 rerank 1회 결과를 leg 로 나눌 뿐이다.

    REQ-REC-024 의 `shall not` 이자 결정 14-E 의 "니즈 수만큼 무제한 fan-out 금지"(REQ-REC-023)다.
    leg 마다 rerank 를 돌리면 니즈가 5개일 때 Sonnet 호출이 5배가 된다.
    """
    push = _RecordingPush()
    llm = _needs_llm(
        [
            {"productId": 101, "rationale": "a"},
            {"productId": 201, "rationale": "b"},
        ]
    )

    await _run_case3(llm, push)

    assert len(push.pushes[0].lists) == 2  # 실제로 분할된 턴에서 센다
    tiers = [tier for tier, _ in llm.calls]
    assert tiers.count("smart") == 1, "rerank(smart)는 니즈 수와 무관하게 1회"
    assert tiers.count("fast") == 1, "decompose(fast)도 1회 — 전개는 이 턴에 트리거되지 않았다"


async def test_case3_budget_counts_only_needs_with_candidates(monkeypatch) -> None:
    """rerank 예산은 **후보가 실제로 남은 니즈** 수로 잡는다 (PR #212 리뷰).

    요청한 leg 수로 잡으면 검색 0건·최근구매 dedup 으로 비워진 니즈까지 예산에 세어, rerank 가
    쓰지도 못할 항목 수를 요구하고 출력 예산만 부풀린다.
    """
    from app.agents.buyer.recommendation import graph as recommendation_graph

    seen: list[int] = []
    real_rerank = recommendation_graph.rerank

    async def _spy(llm, *, query, candidates, profile_summary, tier, expose_max):
        seen.append(expose_max)
        return await real_rerank(
            llm,
            query=query,
            candidates=candidates,
            profile_summary=profile_summary,
            tier=tier,
            expose_max=expose_max,
        )

    monkeypatch.setattr(recommendation_graph, "rerank", _spy)
    settings = get_settings()
    monkeypatch.setattr(settings, "expose_max", 3)

    async def _one_empty_leg(filters, exclude_product_ids=None):
        # 어댑터 leg 은 0건 — 니즈는 2개로 요청됐지만 후보가 남은 건 파우치뿐이다.
        # 후보를 넉넉히 둬야 len(candidates) 상한이 아니라 **니즈 수** 산정이 드러난다.
        if "여행용품" in (filters.category or ""):
            return _res(101, 102, 103, 104, 105, 106, 107, 108)
        return _res()

    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=_needs_llm([{"productId": 101, "rationale": "a"}]),
            search=_one_empty_leg,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),
        )
    )

    # 후보가 있는 니즈는 1개 → 예산 expose_max×1 = 3. 요청 leg 수(2)로 셌다면 6이 된다.
    assert seen == [3]
