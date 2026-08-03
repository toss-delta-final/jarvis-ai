"""멀티 카테고리 fan-out 검색·병합 (이슈 #59, DESIGN-CATEGORY-HYBRID-59 §6).

canonical 카테고리마다 Spring I-1 leg 를 병렬 실행하고 결과를 병합한다:
productId dedup + round-robin 인터리브(한 카테고리 독점 방지) + merge_cap 절단.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
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

    append 후 체크 방식이면 첫 상품이 항상 남아 decompose·dedup_truncate 의 slice 절단과
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


def _mapping(legs, unresolved=()) -> CategoryMapping:
    """매퍼 fake 의 반환값 — #217 로 `map_categories` 가 `CategoryMapping` 을 낸다.

    `unresolved` 를 **명시적으로** 채우게 두는 이유: 기본값으로 뭉개면 "전개가 발동하지 않는" 쪽으로
    조용히 기울어 트리거 회귀를 놓친다. 리스트를 그대로 돌려주는 구식 fake 를 그래프가 관대하게
    받아주지 않는 것도 같은 이유다 — 실제 매퍼가 계약을 어겨도 조용히 전개가 죽는다
    (conftest `_fake_map` 주석의 "시그니처 드리프트 → 조용한 degrade" 교훈과 같은 부류).
    """
    return CategoryMapping(legs=list(legs), unresolved=list(unresolved))


def _two_leg_mapper():
    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return _mapping([("여행/캠핑 > 여행용품", "파우치"), ("가전 > 어댑터", "어댑터")])

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


async def test_fanout_single_category_preserves_candidate_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """단일 카테고리(leg 1개)는 후보 폭을 좁히지 않게 per_cat_limit(10) 이 아니라 merge_cap(30) 을
    size 로 쓴다. 매핑된 단일 질의(leg 1개)도 fan-out 경로를 타므로, 기존 단일검색(limit 30) 대비
    rerank 입력 후보가 줄면 추천 품질이 조용히 저하된다(PR #73 리뷰)."""
    # [#113] 이 테스트의 관심사는 leg 검색 **폭**이라 완화 probe 를 끈다 — 결과 2건은 기본 임계
    # (relaxation_min_results=3) 아래라 소량 완화 재검색이 붙어 호출 수 단언이 흐려진다.
    monkeypatch.setattr(get_settings(), "relaxation_min_results", 0)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        return _res(101, 102)

    async def _one_leg(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return _mapping([("가전 > 이어폰/헤드폰", "무선 이어폰")])

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

    async def _map_leg(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return _mapping([("여행 > 여행용품", "파우치")])

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

    async def _boom(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
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

    async def _boom(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
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

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        legs = [(q.raw_category, q.query) for q in category_queries if q.raw_category]
        return _mapping(legs or [("매퍼우회검증_garbage카테고리", None)])

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

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        # raw 있으면 그대로, null-raw+query 는 그 query 로 canonical 매핑(테스트용 lookup)
        qmap = {"여행 파우치": "여행 > 여행용품"}
        legs = []
        for q in category_queries:
            if q.raw_category:
                legs.append((q.raw_category, q.query))
            elif q.query:
                legs.append((qmap.get(q.query, "미상"), q.query))
        return _mapping(legs)

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

    async def _map_empty(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return _mapping([])  # 매핑 전량 실패(미시드·하드실패)

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


async def _run_recommend(
    message: str, decompose: dict, *, expand=None, unmapped: set[str] | None = None, **kw
) -> list:
    """단일 턴을 돌리고 각 검색 leg 의 category 를 반환한다.

    `unmapped` 는 **매핑이 canonical 을 못 내는 leg query** 집합이다(#217 §4 D2 트리거 입력).
    실제 매퍼에서는 거리컷·택일 null 이 판정하는 자리를, 여기서는 이름으로 지정해 결정적으로 만든다.
    """
    calls: list = []
    unmapped = unmapped or set()

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        # leg query 를 그대로 canonical 처럼 흘려 전개 결과가 검색까지 도달하는지 본다.
        return _mapping(
            [(q.query, q.query) for q in category_queries if q.query and q.query not in unmapped],
            [q.query for q in category_queries if q.query and q.query in unmapped],
        )

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
    """[#198·#217] 목적형 발화가 전개되어 **구체 상품**이 검색 leg 이 된다 — 이 이슈의 목표.

    종전: `['집들이 선물']` 이 그대로 leg 이 되어 매핑 불가(거리컷 드롭) → 카테고리 없이 검색.
    #217 이후로는 **그 매핑 실패 자체가 트리거**다 — marker 목록을 보지 않는다.
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
        unmapped={"집들이 선물"},  # 실측 0.2904 / 0.0073 → 거리컷 드롭
    )
    assert seen == ["집들이 선물로 뭐 사갈까"]  # 발화가 전개기로 전달됨
    assert calls == ["디퓨저", "식기 세트", "핸드워시 세트"]  # 전개 결과가 fan-out 검색까지 도달


async def test_marker_free_purpose_expression_is_expanded() -> None:
    """[#217] 목적 marker 목록에 **없는** 표현도 전개된다 — 이 이슈가 고치는 것.

    `"김밥 재료"`(0.3027 / 마진 0.0054)는 초판 marker 에 `재료` 가 없어 미검출이었다. 목록에
    `재료` 를 넣는 처방은 이미 정답 매핑되는 `한방재료`·`떡볶이 재료` 를 파괴해 기각됐다(§4.0).
    """
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "감자탕 재료 사려고",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "감자탕 재료"}],
        },
        expand=expand,
        unmapped={"감자탕 재료"},
    )
    assert seen == ["감자탕 재료 사려고"]
    assert calls == ["디퓨저", "식기 세트", "핸드워시 세트"]


async def test_mapped_purpose_expression_is_not_expanded() -> None:
    """[#217] 매핑에 성공한 표현은 목적 표현처럼 보여도 전개하지 않는다 — 오탐 0 보장.

    §4.5 ① 대조군이 여기 걸린다. `"수예 재료"` 는 `홈패브릭/수예 > 수예용품` 0.1590 으로 정확히
    매핑되고, 전개하면 오히려 `지퍼`→집업·`바늘`→당뇨침으로 오염된다(§4.5 ③).
    """
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "수예 재료 사려고",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "수예 재료"}],
        },
        expand=expand,  # unmapped 없음 = 매핑 성공
    )
    assert seen == []  # 전개 미호출
    assert calls == ["수예 재료"]


async def test_partial_failure_keeps_mapped_leg_and_adds_expansion() -> None:
    """[#217] 일부 leg 만 실패하면 **성공한 leg 을 지키고 전개 leg 을 더한다**(§6 합집합).

    종전 교체 배선은 전개가 트리거되면 legs 를 통째로 갈아엎어, 사용자가 **명시한** 카테고리까지
    날아갔다. 합집합이면 냉장고를 지키면서 나머지를 푼다. 원 leg 이 **앞**에 오는 것도 계약이다 —
    `category_fanout_max` 절단 시 명시 카테고리가 먼저 살아남아야 한다.
    """
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "이사 가는데 냉장고랑 필요한 것들",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [
                {"category": None, "query": "냉장고"},
                {"category": None, "query": "이사 필요한 것들"},
            ],
        },
        expand=expand,
        unmapped={"이사 필요한 것들"},
    )
    assert seen == ["이사 가는데 냉장고랑 필요한 것들"]
    assert calls == ["냉장고", "디퓨저", "식기 세트", "핸드워시 세트"]


async def test_expansion_runs_at_most_once_per_turn() -> None:
    """[#217] 재매핑이 또 실패해도 다시 전개하지 않는다 — 무한 루프 방지(§6.1).

    전개 결과가 전부 매핑 실패하는 회차가 실제로 있다(§4.5 ③ `김밥 재료` → 김·단무지·시금치가
    전부 거리컷). 그때 "실패했으니 또 전개"로 돌면 턴이 끝나지 않는다.
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
        # 원 leg 도 전개 결과도 전부 매핑 실패
        unmapped={"집들이 선물", "디퓨저", "식기 세트", "핸드워시 세트"},
    )
    assert len(seen) == 1  # 전개 호출은 정확히 1회
    assert calls == [None]  # 살아남은 leg 이 없어 무필터 검색(§7 degrade)


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


async def test_case_gate_blocks_expansion_even_when_mapping_fails() -> None:
    """[#217] case 2 는 **매핑이 실패해도** 전개하지 않는다 — 무필터 계약 보존(#22·#162).

    `'평점 높은 거'` 는 `게임 > PC게임` 0.3420 / 마진 0.0171 로 매핑 실패다. case 2 leg 은 맞는
    칸이 없는 것이 정상이라 매핑 실패가 **구조적으로** 발생하므로, 게이트가 없으면 "카테고리 무관"
    의도가 지어낸 상품 목록으로 좁혀진다(§4.2).
    """
    seen, expand = _expansion_probe()
    calls = await _run_recommend(
        "평점 높은 거 보여줘",
        {
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "filters": {},
            "categoryQueries": [{"category": None, "query": "평점 높은 거"}],
        },
        expand=expand,
        unmapped={"평점 높은 거"},
    )
    assert seen == []  # 전개 미호출
    assert calls == [None]  # 카테고리 없이(무필터) 검색


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


async def test_expansion_failure_keeps_mapping_result() -> None:
    """전개가 실패하면(빈 리스트) **원 매핑 결과**를 그대로 쓴다 — 후퇴 없음(§7).

    전개는 **개선 시도**이며, 실패가 기존 경로를 악화시켜서는 안 된다. 최악이 "지금과 동일".

    #217 로 기준점이 옮겨졌다 — 종전에는 전개가 매핑보다 앞이라 "원본 `category_queries` 유지"가
    후퇴 없음이었는데, 이제는 매핑을 이미 지난 뒤라 **매핑 결과 유지**가 그 자리다. 여기서는 원 leg
    이 거리컷에 걸린 상태이므로 무필터 검색이 되는데, 그것이 정확히 "전개가 없었을 때의 동작"이다.
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
        unmapped={"집들이 선물"},
    )
    assert calls == [None]  # 매핑 결과(빈 legs) 유지 → 무필터 + semanticQuery


async def test_expansion_failure_keeps_mapped_legs_intact() -> None:
    """전개 실패가 **이미 매핑된 leg** 을 건드리지 않는다 — 합집합 배선의 후퇴 없음 보장(§7).

    합집합이라 "전개가 성공했으나 내용이 엉뚱한" 경우에도 원 leg 을 잃는 경로가 구조적으로 없다.
    종전 교체 배선에는 그 구멍이 있었다.
    """

    async def _expand_fail(utterance, **_):
        return []

    calls = await _run_recommend(
        "이사 가는데 냉장고랑 필요한 것들",
        {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "filters": {},
            "categoryQueries": [
                {"category": None, "query": "냉장고"},
                {"category": None, "query": "이사 필요한 것들"},
            ],
        },
        expand=_expand_fail,
        unmapped={"이사 필요한 것들"},
    )
    assert calls == ["냉장고"]  # 성공한 leg 은 그대로


async def test_category_agnostic_case2_is_never_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    """[PR #203 리뷰] case 2(구조화 조건만) 발화는 전개하지 않는다 — 무필터 계약 보존(#22·#162).

    `"5만원 이하 아무거나"` 도 `categoryQueries` 가 비어 D1 조건에 걸린다. 전개 LLM 은 "최소 2개"를
    강제하므로 목적이 없는 입력에도 상품명을 지어내고, 그것이 legs 를 교체하면 `filters.category` 가
    채워져 "카테고리 무관·가격만 필터"라는 사용자 의도가 파괴된다. #162 가 개선할 경로이기도 하다.
    """
    # [#113] 검색에 실리는 category 만 보는 테스트라 완화 probe 를 끈다(결과 1건 = 소량 임계 아래).
    monkeypatch.setattr(get_settings(), "relaxation_min_results", 0)
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
        unmapped={"집들이 선물"},
    )
    assert calls == ["디퓨저", "식기 세트"]  # 전개가 실제로 발동한 턴
    assert got == [observer]  # 같은 observer 가 seam 까지 도달했다


async def test_mapper_receives_observer_for_select_model_call_logging() -> None:
    """[PR #188 리뷰] 그래프는 `observer` 를 매퍼까지 내려보낸다 — §4.4 택일 호출 기록의 전제.

    기록은 모델을 실제로 부르는 `select_category` 가 하므로(주입형 seam 에 유령 호출을 남기지
    않기 위해), 그래프의 책임은 observer 전달이다. 이 배선이 끊기면 애매한 leg 이 많은 턴의
    LLM 호출이 `chat_request` 집계(api-spec §6.3)와 요청 트레이싱(#141)에서 조용히 빠진다.
    """
    got: list = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **kw):
        got.append(kw.get("observer"))
        return _mapping([("가전 > 이어폰/헤드폰", "무선 이어폰")])

    observer = _ProbeObserver()
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    await _collect(
        run_buyer_turn(
            _req(message="무선 이어폰 추천"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 1,
                    "filters": {},
                    "categoryQueries": [{"category": None, "query": "무선 이어폰"}],
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
            observer=observer,
        )
    )
    assert calls == ["가전 > 이어폰/헤드폰"]
    assert got == [observer]  # 같은 observer 가 매퍼까지 도달했다


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
        # 시그니처가 프로덕션 호출과 맞아야 한다 — `llm`·`tier`·`observer`·`select_max_calls` 를
        # 안 받으면 TypeError 가 `_map_or_empty` 의 방어 except 에 먹혀 **매퍼가 아예 안 도는데도**
        # 무필터 검색으로 목록 1건이 나와 이 테스트가 통과한다(실제로 그 상태였다).
        # lessons.md "주입 seam 시그니처를 바꾸면 모든 fake 를 함께 고친다" 참조.
        async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
            return _mapping([("여행/캠핑 > 여행용품", "파우치")])

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

    async def _spy(llm, **kwargs):
        seen.append(kwargs["expose_max"])
        return await real_rerank(llm, **kwargs)

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


async def test_case3_rerank_prompt_carries_need_boundaries() -> None:
    """니즈별 턴이면 rerank 입력에 니즈 경계가 실제로 실려 나간다 (PR #212 리뷰, 배선 확인)."""
    push = _RecordingPush()
    llm = _needs_llm([{"productId": 101, "rationale": "a"}, {"productId": 201, "rationale": "b"}])

    await _run_case3(llm, push)

    smart_user = next(user for tier, user in llm.calls if tier == "smart")
    assert "NEEDS" in smart_user
    assert '"need": "파우치"' in smart_user and '"need": "어댑터"' in smart_user


async def test_non_case3_rerank_prompt_has_no_need_section() -> None:
    """분할하지 않는 턴의 rerank 입력은 종전 그대로다 — 흔한 경로를 건드리지 않는다."""
    push = _RecordingPush()
    llm = FakeLLM()  # DEFAULT_DECOMPOSE = case 2

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

    smart_user = next(user for tier, user in llm.calls if tier == "smart")
    assert "NEEDS" not in smart_user
    assert '"need"' not in smart_user


def test_need_label_falls_back_to_canonical_after_sanitizing() -> None:
    """정제 결과가 비면 canonical 로 폴백한다 (PR #212 리뷰).

    query 는 decompose LLM 산출 자유 텍스트라 zero-width·제어문자만 남는 경우가 있다.
    `query or canonical` 을 정제 **전에** 판정하면 query 가 truthy 라 canonical 을 못 보고
    label 이 조용히 사라진다 — 니즈별 목록에서 이름 없는 목록이 나온다.
    """
    from app.agents.buyer.recommendation.graph import _need_label

    assert _need_label(("가전 > 어댑터", "어댑터")) == "어댑터"
    assert _need_label(("가전 > 어댑터", "​​")) == "가전 > 어댑터"  # 정제 후 폴백
    assert _need_label(("가전 > 어댑터", None)) == "가전 > 어댑터"
    assert _need_label(("​", "​")) is None  # 양쪽 다 비면 라벨 없음


def test_need_label_truncates_to_contract_cap() -> None:
    """label 은 계약 상한(§4.2 ≤50자)으로 자른다 — 초과하면 Spring 이 400 이다."""
    from app.agents.buyer.recommendation.graph import _need_label
    from app.schemas.spring import LIST_LABEL_MAX_LEN

    label = _need_label(("c", "가" * 80))
    assert len(label) == LIST_LABEL_MAX_LEN


def test_need_names_fall_back_to_ordinal_for_rerank_only() -> None:
    """라벨이 전부 정제로 비어도 rerank 에는 니즈 경계가 남는다 (PR #212 리뷰).

    빈 dict 를 넘기면 rerank 가 `if need_of:` 에서 falsy 로 걸러 **단일 목록 경로와 똑같이**
    경계 없이 정렬한다 — 그런데 하류는 여전히 목록을 쪼개므로 근거 없는 카드가 나간다.
    rerank 에 필요한 건 사람이 읽는 이름이 아니라 **구분되는 토큰**이라 순번으로 채운다.
    이 순번은 rerank 입력 전용이며 push `label`(사용자 노출)로는 새지 않는다.
    """
    from app.agents.buyer.recommendation.graph import _need_names

    legs = [("​", "​"), ("​", "​")]  # 양쪽 다 정제하면 빈 문자열
    names = _need_names(legs, leg_of={101: 0, 201: 1}, product_ids=[101, 201])

    assert set(names.values()) == {"니즈 1", "니즈 2"}, "경계가 구분되기만 하면 된다"


def test_need_names_prefer_real_labels() -> None:
    """실제 니즈 이름이 있으면 그대로 쓴다 — 순번은 이름이 없을 때만."""
    from app.agents.buyer.recommendation.graph import _need_names

    legs = [("여행/캠핑 > 여행용품", "파우치"), ("​", "​")]
    names = _need_names(legs, leg_of={101: 0, 201: 1}, product_ids=[101, 201])

    assert names == {101: "파우치", 201: "니즈 2"}


def test_need_names_disambiguate_colliding_labels() -> None:
    """두 니즈가 같은 라벨을 내면 구분자를 붙인다 (PR #212 리뷰).

    rerank 는 `dict.fromkeys(need_of.values())` 로 NEEDS 목록을 만들어서, 토큰이 겹치면
    **두 leg 를 하나의 니즈로 뭉갠다** — "니즈마다 상위 N개" 지시가 둘을 구분하지 못해
    이 PR 이 고치려던 쏠림이 그대로 재발한다. rerank 는 정상 성공이라 드러나지도 않는다.
    """
    from app.agents.buyer.recommendation.graph import _need_names

    # 서로 다른 두 니즈가 같은 canonical 로 매핑되고 query 는 둘 다 정제 후 빈 경우.
    legs = [("패션 > 가방", "​"), ("패션 > 가방", "​")]
    names = _need_names(legs, leg_of={101: 0, 201: 1}, product_ids=[101, 201])

    assert names[101] != names[201], "겹치면 rerank 가 두 니즈를 하나로 본다"
    assert len(set(names.values())) == 2


def test_need_names_keep_clean_labels_when_distinct() -> None:
    """겹치지 않으면 이름을 그대로 둔다 — 구분자는 충돌할 때만 붙인다."""
    from app.agents.buyer.recommendation.graph import _need_names

    legs = [("여행/캠핑 > 여행용품", "파우치"), ("가전 > 어댑터", "어댑터")]
    names = _need_names(legs, leg_of={101: 0, 201: 1}, product_ids=[101, 201])

    assert names == {101: "파우치", 201: "어댑터"}


def test_split_by_need_logs_products_without_leg(caplog) -> None:
    """leg 를 모르는 상품이 섞이면 조용히 leg 0 에 넣지 않고 로그를 남긴다 (PR #212 리뷰).

    같은 PR 의 reco_lists_truncated 와 같은 기준이다 — 도달하지 않아야 하는 경계가 도달하면
    어느 니즈에도 속하지 않는 상품이 남의 목록에 섞이는데, 로그가 없으면 영영 안 드러난다.
    """
    import logging

    from app.agents.buyer.recommendation.graph import _split_by_need

    with caplog.at_level(logging.WARNING):
        groups = _split_by_need(
            [101, 999],  # 999 는 leg_of 에 없다
            _res(101, 999).products,
            leg_of={101: 0},
            leg_count=2,
            expose_min=1,
            expose_max=5,
        )

    assert groups  # 동작은 종전대로 — 진단만 추가한다
    assert any("leg" in r.message for r in caplog.records)


async def test_mapper_exception_skips_expansion_and_degrades_to_unfiltered() -> None:
    """[#217 PR 리뷰] `case 3` 인데 매퍼 **호출 자체**가 예외로 죽으면 전개하지 않고 무필터로 간다.

    #217 로 전개 트리거가 매핑 결과에 종속되면서 생긴 동작이라 명시적으로 고정한다. `_map_or_empty`
    가 예외를 `CategoryMapping()`(legs·unresolved 모두 빈 값)로 흡수하므로 D1(신호는 있으니 해당
    없음)·D2(unresolved 비어 해당 없음) 둘 다 걸리지 않는다.

    **결과적 손실은 없다** — 전개가 낸 상품명도 결국 같은 매퍼를 타야 canonical 이 되는데, 그 매퍼가
    죽어 있다. 종전 배선(전개 먼저 → 매핑 1회)에서도 매핑 예외는 그대로 `category_legs = []` 로
    떨어져 최종 상태가 **무필터로 동일**했고, 차이는 종전이 헛된 LLM 전개 호출을 한 번 더 썼다는
    것뿐이다. 즉 이 변경은 실패 모드에서 비용만 줄인다.
    """
    seen, expand = _expansion_probe()
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _boom(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        raise RuntimeError("mapper bug")

    await _collect(
        run_buyer_turn(
            _req(message="집들이 선물로 뭐 사갈까"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [{"category": None, "query": "집들이 선물"}],
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_boom,
            expand_needs=expand,
        )
    )
    assert seen == []  # 매퍼가 죽었으면 전개 LLM 을 부르지 않는다(헛된 비용)
    assert calls == [None]  # canonical-or-null — 미검증 raw 가 검색에 안 실린다


async def test_select_budget_is_shared_across_both_mapping_calls() -> None:
    """[#217 PR 리뷰] `category_select_max_calls` 는 **턴당** 상한이다 — 매핑 2회가 예산을 나눠 쓴다.

    #217 로 매핑이 턴에 2회(원 legs·전개 legs) 불리는데, 각 호출이 `settings` 값을 독립적으로 쓰면
    턴당 택일 LLM 호출이 상한의 **2배**까지 나간다(기본 2 → 4). config 주석이 "턴당 택일 LLM 호출
    상한"이라고 못 박고 있고 `SPEC-RECOMMEND-001 §비기능` 의 턴당 호출 예산도 그 전제에 선다.

    호출부가 첫 매핑이 쓴 몫(`CategoryMapping.select_calls`)을 빼고 남은 예산을 두 번째 호출에
    넘기는지 고정한다. `pool >= 2 × fanout_max`(pg 커넥션)는 순차 호출이라 무관하지만 LLM 총량은
    별개 리소스라 따로 막아야 한다.
    """
    budgets: list[int | None] = []
    seen, expand = _expansion_probe()

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **kw):
        budgets.append(kw.get("select_max_calls"))
        # 첫 호출이 예산을 **전부** 썼다고 보고한다 → 두 번째 호출에 남는 몫은 0 이어야 한다.
        # settings 값을 그대로 쓰는 이유: 기본값(2)을 테스트에 박으면 config 를 튜닝했을 때
        # 불변식과 무관하게 깨진다. 검증 대상은 "쓴 만큼 빠지는가"이지 특정 숫자가 아니다.
        used = settings.category_select_max_calls if len(budgets) == 1 else 0
        return CategoryMapping(
            legs=[(q.query, q.query) for q in category_queries if q.query and len(budgets) > 1],
            unresolved=[q.query for q in category_queries if q.query and len(budgets) == 1],
            select_calls=used,
        )

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    await _collect(
        run_buyer_turn(
            _req(message="집들이 선물로 뭐 사갈까"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [{"category": None, "query": "집들이 선물"}],
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
            expand_needs=expand,
        )
    )
    assert seen  # 전개가 실제로 발동한 턴이어야 두 번째 매핑이 존재한다
    assert len(budgets) == 2  # 원 legs + 전개 legs
    assert budgets[0] is None  # 첫 호출은 settings 기본값을 쓴다
    assert budgets[1] == 0  # 첫 호출이 예산을 다 썼으므로 남은 몫 0


async def test_union_keeps_select_budget_accounting(caplog) -> None:
    """[#217] `needs_expansion_union` 이 **두 매핑의 택일 소비 합계**를 보고한다.

    상한이 **턴당**이므로 사후 검증도 턴 단위여야 한다 — 호출별 숫자만 남으면 "상한이 실제로
    지켜졌나"를 운영 로그로 확인할 수 없다. 어느 한쪽을 빠뜨리면 예산 회계가 거짓이 된다.

    **이 테스트가 덮지 않는 것**: 합집합 객체(`CategoryMapping.select_calls`)의 값 자체. 로그를
    `replace` 앞에서 찍으므로 객체 쪽을 되돌려도 이 단언은 통과한다 — 그 값은 현재 하류에서 읽는
    곳이 없어 동작으로 관측되지 않는다. 그럼에도 `dataclasses.replace` 로 이어붙이는 이유는
    **새 필드가 조용히 기본값으로 리셋되는 것을 구조적으로 막기 위한 위생**이고(실제로 그 상태였다),
    테스트가 아니라 코드 주석이 그 근거를 진다.
    """
    seen, expand = _expansion_probe()
    calls: list = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        first = not calls
        calls.append(1)
        return CategoryMapping(
            legs=[] if first else [(q.query, q.query) for q in category_queries if q.query],
            unresolved=[q.query for q in category_queries if q.query] if first else [],
            select_calls=1,  # 두 호출이 각각 1회씩 → 합계 2 여야 한다
        )

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    with caplog.at_level("INFO"):
        await _collect(
            run_buyer_turn(
                _req(message="집들이 선물로 뭐 사갈까"),
                _member(),
                llm=FakeLLM(
                    decompose={
                        "intent": "recommend",
                        "reply": "",
                        "case": 3,
                        "filters": {},
                        "categoryQueries": [{"category": None, "query": "집들이 선물"}],
                    }
                ),
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_map,
                expand_needs=expand,
            )
        )

    assert seen and len(calls) == 2  # 전개가 발동해 매핑이 2회 불렸다
    union = next(r for r in caplog.records if r.msg == "needs_expansion_union")
    assert union.select_calls == 2  # 1(원 매핑) + 1(전개 매핑) — 어느 쪽도 잃지 않는다
    assert union.base_legs == 0
    assert union.expanded_legs == 3
