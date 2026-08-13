"""멀티 카테고리 fan-out 검색·병합 (이슈 #59, DESIGN-CATEGORY-HYBRID-59 §6).

canonical 카테고리마다 Spring I-1 leg 를 병렬 실행하고 결과를 병합한다:
productId dedup + round-robin 인터리브(한 카테고리 독점 방지) + merge_cap 절단.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.agents.buyer.graph import get_thread_store
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.recommendation.category_mapping import map_categories as _real_map_categories
from app.agents.buyer.recommendation.graph import _merge_fanout_results
from app.agents.buyer.recommendation.state import build_condition_chips
from app.agents.buyer.session_state import context_thread_key
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


def _event(record: logging.LogRecord, event: str) -> bool:
    """JSON message 구조화 이벤트를 caplog에서 찾는다."""
    return getattr(record, "event", None) == event


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


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    """R6-1 검증용 — `run_buyer_turn` 이 내부에서 만드는 것과 같은 thread_key 를 재계산한다.

    `touch` 는 같은 세션·스레드에 다시 불러도 같은 `context_id` 를 낸다(멱등) — `test_multiturn_
    category_intent_84.py` 와 같은 패턴으로, 실행이 끝난 뒤 `get_thread_store().get(key)` 로
    영속된 `filters` 를 직접 읽는다.
    """
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


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
    # limit = category_fanout_merge_cap(기본 30) — #89: leg 사전 절단은 leg 수와 무관하게
    # merge_cap 을 쓴다(per_cat_limit 은 더 이상 소비되지 않음).
    assert by_cat["여행/캠핑 > 여행용품"].keyword is None
    assert by_cat["여행/캠핑 > 여행용품"].semantic_query == "파우치"
    assert by_cat["가전 > 어댑터"].keyword is None
    assert by_cat["가전 > 어댑터"].semantic_query == "어댑터"
    assert by_cat["가전 > 어댑터"].limit == 30


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
    rerank 입력 후보가 줄면 추천 품질이 조용히 저하된다(PR #73 리뷰).

    #89 이후로는 leg 수와 무관하게 항상 merge_cap 이므로 단일 카테고리만의 특례는 아니다 — 이
    테스트는 그중 단일 leg 경로의 폭을 계속 고정한다."""
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


def _three_leg_mapper():
    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return _mapping(
            [
                ("여행/캠핑 > 여행용품", "파우치"),
                ("가전 > 어댑터", "어댑터"),
                ("패션 > 의류", "의류"),
            ]
        )

    return _map


async def test_fanout_partial_failure_keeps_survivor_candidate_width() -> None:
    """[#89] 3-leg 중 2개가 SpringUnavailableError 로 죽어도 생존 leg 의 후보 폭이
    per_cat_limit(10) 이 아니라 merge_cap(30) 으로 유지된다 — leg 사전 절단 상한은 요청 시점
    leg 수가 아니라 merge_cap 을 쓴다(#89, 재조정 자체가 불필요).

    fake search 는 `filters.limit` 을 **존중**해 반환을 자른다(방식1/미래 백엔드 시뮬레이션) —
    지금 다른 fake 들은 limit 을 무시해서 이 결함을 못 잡는다.
    """
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        if filters.category != "여행/캠핑 > 여행용품":
            raise SpringUnavailableError("leg down")
        all_products = _res(*range(1, 41)).products  # 40건 보유
        return ProductSearchResult(
            products=all_products[: filters.limit], total_count=len(all_products)
        )

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=push,
            map_categories=_three_leg_mapper(),
        )
    )
    assert "error" not in [e["type"] for e in events]

    survivor_calls = [f for f in calls if f.category == "여행/캠핑 > 여행용품"]
    assert len(survivor_calls) == 1  # 생존 leg 검색은 정확히 1회 — 재조회 없음
    limit_used = survivor_calls[0].limit
    assert limit_used == 30  # per_cat_limit(10) 이 아니라 merge_cap(30)

    # limit 이 30으로 전달된 것만으로는 후보 폭이 실제로 넓어졌는지 증명하지 못한다 — limit 을
    # 존중하는 경로에서 병합 산출까지 재현해 폭 자체를 단언한다(노출 단계는 expose_max(9) 로
    # 잘려 30을 관측할 수 없으므로 _merge_fanout_results 산출을 직접 본다).
    survivor_result = ProductSearchResult(
        products=_res(*range(1, 41)).products[:limit_used], total_count=40
    )
    merged, _ = _merge_fanout_results(
        [(0, survivor_result)], cap=get_settings().category_fanout_merge_cap
    )
    assert len(merged.products) == 30  # per_cat_limit(10) 이 아니라 merge_cap(30) 만큼 후보 확보


async def test_fanout_normal_path_unchanged_when_all_legs_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#89] 정상 경로 고정핀 — 전 leg 생존 시 검색 호출 수 회귀 0, 병합 후보가 merge_cap 을
    넘지 않고 round-robin 대표성(한 leg 독점 아님)이 유지된다. limit-존중 fake 로 leg 사전
    절단이 실제로 작동하는 경로에서 검증한다."""
    monkeypatch.setattr(get_settings(), "relaxation_min_results", 0)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        if "여행용품" in filters.category:
            all_products = _res(*range(1, 41)).products
        else:
            all_products = _res(*range(101, 141)).products
        return ProductSearchResult(
            products=all_products[: filters.limit], total_count=len(all_products)
        )

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
    assert len(calls) == 2  # leg 수만큼만 호출 — 완화 재조회 외 추가 왕복 0

    by_cat = {f.category: f for f in calls}
    leg0 = ProductSearchResult(
        products=_res(*range(1, 41)).products[: by_cat["여행/캠핑 > 여행용품"].limit],
        total_count=40,
    )
    leg1 = ProductSearchResult(
        products=_res(*range(101, 141)).products[: by_cat["가전 > 어댑터"].limit],
        total_count=40,
    )
    merge_cap = get_settings().category_fanout_merge_cap
    merged, _ = _merge_fanout_results([(0, leg0), (1, leg1)], cap=merge_cap)
    assert len(merged.products) <= merge_cap  # 병합 후보가 merge_cap 을 넘지 않음
    ids = _ids(merged)
    # round-robin — 두 leg 모두 대표된다(한 leg 가 병합 앞부분을 독점하지 않음).
    assert set(ids[:2]) == {1, 101}
    assert any(pid <= 40 for pid in ids) and any(pid >= 101 for pid in ids)


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


def test_condition_chips_multi_category_one_chip_per_value() -> None:
    """[#434] 멀티 카테고리는 값당 칩 1개 — value 는 항상 스칼라이고 입력 순서를 보존한다."""
    cats = ["여행/캠핑 > 여행용품", "가전 > 어댑터", "패션 > 의류"]
    chips = build_condition_chips(ProductSearchFilters(category=cats[0]), categories=cats)
    cat_chips = [c for c in chips if c.field == "category"]
    assert len(cat_chips) == len(cats)
    assert [c.value for c in cat_chips] == cats
    assert [c.label for c in cat_chips] == [f"카테고리 · {c}" for c in cats]
    assert all(isinstance(c.value, str) for c in cat_chips)  # 스칼라 — 계약(§3.1) 정합


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
    assert len(cat_chips) == 2  # [#434] 값당 칩 1개
    values = {c["value"] for c in cat_chips}
    assert all(isinstance(v, str) for v in values)  # 스칼라 문자열(계약 정합)
    assert values == {"여행/캠핑 > 여행용품", "가전 > 어댑터"}


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


def test_condition_chips_multi_brand_one_chip_per_value() -> None:
    """[#434] 멀티 브랜드는 값당 칩 M개, label==value==브랜드, 입력 순서 보존."""
    brands = ["삼성", "LG", "애플"]
    chips = build_condition_chips(ProductSearchFilters(brand=brands))
    brand_chips = [c for c in chips if c.field == "brand"]
    assert len(brand_chips) == len(brands)
    assert [c.value for c in brand_chips] == brands
    assert [c.label for c in brand_chips] == brands


def test_condition_chips_single_brand_value_is_scalar_not_list() -> None:
    """[#434, §3.1 v0.32.14 정정] 단일 브랜드는 label 종전 동일, value 는 스칼라 문자열(리스트 아님)."""
    chips = build_condition_chips(ProductSearchFilters(brand=["삼성"]))
    brand_chip = next(c for c in chips if c.field == "brand")
    assert brand_chip.label == "삼성"
    assert brand_chip.value == "삼성"
    assert isinstance(brand_chip.value, str)


def test_condition_chips_dedup_by_field_and_value() -> None:
    """[#434] leg 매핑이 같은 canonical 을 두 번 내도 (field, value) 중복 칩은 생기지 않는다."""
    cats = ["가전 > 이어폰", "가전 > 이어폰", "패션 > 의류"]
    chips = build_condition_chips(ProductSearchFilters(category=cats[0]), categories=cats)
    cat_chips = [c for c in chips if c.field == "category"]
    assert [c.value for c in cat_chips] == ["가전 > 이어폰", "패션 > 의류"]

    brand_chips = build_condition_chips(ProductSearchFilters(brand=["삼성", "삼성", "LG"]))
    values = [c.value for c in brand_chips if c.field == "brand"]
    assert values == ["삼성", "LG"]


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


async def test_case3_reasons_are_scoped_to_their_list(monkeypatch) -> None:
    """근거는 그 상품이 속한 목록에만 실린다 — 목록 간 reason 누수 금지(§4.2 productId 키잉)."""
    # 이 테스트는 서로 다른 legacy reason의 목록 귀속을 보는 것이 목적이다. Production C의
    # 템플릿 렌더링은 별도 grounding 테스트가 맡으므로 A rollback으로 문자열을 보존한다.
    monkeypatch.setattr(get_settings(), "rerank_grounding_arm", "current")
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
    monkeypatch.setattr(settings, "rerank_ranking_arm", "current")
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


@pytest.mark.parametrize("ranking_arm", ["current", "structured", "hybrid"])
async def test_production_graph_passes_independent_ranking_and_grounding_arms(
    monkeypatch, ranking_arm: str
) -> None:
    from app.agents.buyer.recommendation import graph as recommendation_graph

    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", ranking_arm)
    observed: list[dict[str, object]] = []
    real_rerank = recommendation_graph.rerank

    async def _spy(llm, **kwargs):
        observed.append(
            {
                "grounding_arm": kwargs.get("grounding_arm"),
                "ranking_arm": kwargs.get("ranking_arm"),
                "rrf_alpha": kwargs.get("rrf_alpha"),
                "rrf_k": kwargs.get("rrf_k"),
            }
        )
        # 이 테스트의 legacy fake 응답을 파싱할 수 있게 호출 관찰 뒤 A로 돌린다. C의 출력
        # 검증 자체는 test_rerank_grounding.py가 구조화 응답으로 별도 고정한다.
        kwargs["grounding_arm"] = "current"
        kwargs["ranking_arm"] = "current"
        return await real_rerank(llm, **kwargs)

    monkeypatch.setattr(recommendation_graph, "rerank", _spy)
    await _run_case3(
        _needs_llm([{"productId": 101, "rationale": "a"}]),
        _RecordingPush(),
    )

    assert observed == [
        {
            "grounding_arm": "validated",
            "ranking_arm": ranking_arm,
            "rrf_alpha": 0.65,
            "rrf_k": 60,
        }
    ]


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


# ── 광역 발화 → leaf fan-out 폴백 (#222) ──────────────────────────────────
#
# 이슈 원안(top-k 공통 조상으로 광역/협소 판정)은 오케스트레이터 실측으로 기각됐다(정확도 0.50,
# 우연 수준). 대신 매핑이 canonical 을 하나도 못 낸 turn(`category_legs == []`)에서
# `CategoryMapping.expansion_leaves`(§4 거리컷·택일 null 로 이미 조회된 의미 기반 top-N leaf)를
# 그대로 fan-out leg 으로 쓴다. 협소 발화는 canonical 이 나오므로 이 경로에 애초에 진입하지 않아
# 협소 회귀가 구조적으로 0 이다(테스트 1이 그 불변식을 고정한다).

_BROAD_LEAVES = [  # 실측 "화장품 추천해줘" → 8 leaf / 3~4 중분류(§6 오케스트레이터 데이터)
    ("메이크업 > 페이스메이크업", "화장품"),
    ("스킨케어 > 스킨/토너", "화장품"),
    ("뷰티소품 > 메이크업소품", "화장품"),
    ("스킨케어 > 에센스/세럼", "화장품"),
    ("메이크업 > 립메이크업", "화장품"),
    ("스킨케어 > 클렌징", "화장품"),
    ("뷰티소품 > 화장솜/면봉", "화장품"),
    ("메이크업 > 아이메이크업", "화장품"),
]
_BROAD_MIDS = ["메이크업", "스킨케어", "뷰티소품"]  # 중복 제거·첫 등장 순서


def _broad_decompose(query: str = "화장품", *, case: int = 2) -> dict:
    """광역 발화 decompose 산출 — 기본은 case != 3(needs_expansion 게이트 밖, #198 과 직교).

    `case=3` 로 호출하면 R4-1(PR #318 리뷰) 재현용 — case 3 + 매핑 전량 실패 + 확장 폴백이
    `split_by_need` 를 잘못 통과하지 않는지 검증하는 테스트가 쓴다.
    """
    return {
        "intent": "recommend",
        "reply": "",
        "case": case,
        "filters": {},
        "categoryQueries": [{"category": None, "query": query}],
    }


def _broad_mapper(leaves=_BROAD_LEAVES, unresolved=("화장품",)):
    """매핑이 canonical 을 하나도 못 내고 확장 후보만 낸 상황을 흉내 낸다(§4 ①·② 트리거)."""

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(legs=[], unresolved=list(unresolved), expansion_leaves=list(leaves))

    return _map


def _narrow_mapper_with_expansion_noise(leaves=_BROAD_LEAVES):
    """협소 발화(canonical 1개 이상)인데 매퍼가 expansion_leaves 도 함께 낸 경우 — 실제로는 안
    나오지만, "legs 가 하나라도 있으면 확장이 절대 발동하지 않는다"는 불변식을 매퍼 구현 디테일과
    무관하게 고정하려고 일부러 채워 넣는다."""

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[("뷰티 > 립스틱", "립스틱")], unresolved=[], expansion_leaves=list(leaves)
        )

    return _map


async def test_narrow_turn_with_any_canonical_leg_is_never_expanded() -> None:
    """[테스트 1 — 협소 회귀 고정] canonical leg 이 하나라도 있으면 category_legs 는 종전과
    완전히 동일하고 확장이 발동하지 않는다. 이 PR 의 핵심 안전장치다.
    """
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101, 102)

    events = await _collect(
        run_buyer_turn(
            _req(message="립스틱 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose("립스틱")),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_narrow_mapper_with_expansion_noise(),
        )
    )
    assert calls == ["뷰티 > 립스틱"]  # 확장 leaf 로 fan-out 하지 않음 — 단일 leg 그대로
    conditions = next(e for e in events if e["type"] == "conditions")["data"]
    cat_chips = [c for c in conditions["chips"] if c["field"] == "category"]
    assert len(cat_chips) == 1 and cat_chips[0]["value"] == "뷰티 > 립스틱"  # 칩도 억제 안 됨
    assert not any("메이크업" in e["data"].get("text", "") for e in events if e["type"] == "token")


async def test_broad_turn_fans_out_to_expansion_leaves() -> None:
    """[테스트 6] legs=[] + expansion_leaves 있음 → category_legs 가 확장 leaf 로 채워져 그
    카테고리마다 fan-out 검색이 나간다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101, 102)

    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert set(calls) == {c for c, _ in _BROAD_LEAVES}  # 8 leaf 전부 검색 leg 이 됐다


async def test_expand_disabled_flag_keeps_legacy_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """[테스트 7] `category_expand_enabled=False` 면 확장이 발동하지 않고 종전 무필터 degrade 그대로다."""
    monkeypatch.setattr(get_settings(), "category_expand_enabled", False)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert calls == [None]  # fan-out 없이 단일 무필터 검색(종전 canonical-or-null degrade)


async def test_expand_zero_leaves_degrades_to_no_category_filter() -> None:
    """[테스트 11 — degrade] 확장 leaf 가 0개면 category_legs=[] → filters.category is None
    (중분류·도메인 이름이 categoryName 으로 Spring 에 나가지 않는다)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    await _collect(
        run_buyer_turn(
            _req(message="애매한 발화"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose("애매한 발화")),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(leaves=[]),
        )
    )
    assert calls == [None]


async def test_expanded_turn_omits_category_condition_chip() -> None:
    """[테스트 9] 확장 턴은 카테고리 조건 칩을 내지 않는다 — leg 이 최대 8개라 칩 하나를 지웠을 때
    무엇이 빠지는지 사용자가 알 수 없으면 "표시=실제"(#51)가 깨진다."""

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    conditions = next(e for e in events if e["type"] == "conditions")["data"]
    assert not any(c["field"] == "category" for c in conditions["chips"])


async def test_expand_notice_lists_deduped_mids_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """[테스트 10] 고지 문구가 확장 leaf 의 중복 제거된 중분류로 조립되고,
    `category_expand_notice_enabled=False` 면 종전 문구 그대로다(고지 미발신)."""

    # 이 테스트는 확장 고지와 legacy 모델 코멘트가 별도 token인지 본다. C는 모델 코멘트를
    # 결정론 템플릿으로 대체하므로 A rollback으로 기존 문자열 관찰 범위를 고정한다.
    monkeypatch.setattr(get_settings(), "rerank_grounding_arm", "current")

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    expected = get_settings().category_expand_notice.format(items=" · ".join(_BROAD_MIDS))
    assert expected in tokens
    # 종전 rerank comment token 은 그대로 별도로 남는다(새 이벤트 아님, 기존 token 을 대체하지 않음).
    assert "요청 조건에 맞는 추천이에요" in tokens

    monkeypatch.setattr(get_settings(), "category_expand_notice_enabled", False)
    events2 = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘", thread_id="t2"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    tokens2 = [e["data"]["text"] for e in events2 if e["type"] == "token"]
    assert expected not in tokens2
    assert "요청 조건에 맞는 추천이에요" in tokens2  # 종전 문구는 그대로 남는다


async def test_expand_notice_excludes_mids_of_failed_legs() -> None:
    """[R14-1] 확장 fan-out 의 leg 하나(뷰티소품 계열)가 `SpringUnavailableError` 로 부분
    실패하면, 확장 고지 mids 에는 그 leg 의 mid("뷰티소품")가 **빠지고** 생존 leg 의 mid
    (메이크업·스킨케어)만 남는다 — 실패한 leg 는 실제로 검색하지 못했는데 "찾아봤어요"라고
    고지하면 #51 표시=실제가 깨진다."""
    failing_mid = "뷰티소품"

    async def _search(filters, exclude_product_ids=None):
        if filters.category.split(" > ", 1)[0] == failing_mid:
            raise SpringUnavailableError("leg down")
        return _res(101)

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    survived_mids = [m for m in _BROAD_MIDS if m != failing_mid]
    expected = get_settings().category_expand_notice.format(items=" · ".join(survived_mids))
    assert expected in tokens
    failed_notice = get_settings().category_expand_notice.format(items=" · ".join(_BROAD_MIDS))
    assert failed_notice not in tokens  # 실패한 뷰티소품 leg 이 섞여 들어가지 않았다


async def test_default_settings_combination_expands_broad_turn() -> None:
    """[기본값 조합 시뮬레이션] `category_expand_*` 를 아무것도 오버라이드하지 않은 기본값
    조합에서도 확장이 정상 동작한다 — 모든 테스트가 값을 오버라이드하면 배포되는 기본값 조합이
    깨져도 아무도 모른다(과거 실제 사례)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert set(calls) == {c for c, _ in _BROAD_LEAVES}
    conditions = next(e for e in events if e["type"] == "conditions")["data"]
    assert not any(c["field"] == "category" for c in conditions["chips"])
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any("메이크업" in t and "스킨케어" in t and "뷰티소품" in t for t in tokens)


# ── F-1 (라운드 2) — 확장 fan-out 전량 0건 → 무필터 1회 재검색 ─────────────────
#
# 확장 leg 은 거리컷이 "맞는 칸이 없다"고 이미 판정해 버린 후보다. 8개가 전부 빗나가면 Spring 은
# leg 마다 0건을 내고, `_merge_fanout_results` 도 정상적으로 빈 결과를 병합한다 — `search_bundle`
# 은 None 이 아니므로(leg 자체는 살아있다) `SEARCH_FAILED` 로도 안 걸린다. 확장 이전엔 같은 발화가
# `legs=[]` → 카테고리 무필터 검색으로 결과가 나왔으므로, 손대지 않으면 이 PR 이 "결과 있음"을
# "0건"으로 바꾸는 회귀가 된다(이슈 #222 ⑤). `relaxation` 은 category 를 완화 대상으로 다루지
# 않아 이 경로를 구제하지 못한다.


async def test_zero_result_expansion_falls_back_to_unfiltered_search_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[F-1] 확장 leg 이 전부 0건 → 카테고리 없이 1회 재검색해 결과를 노출하고, 확장 고지
    token 은 내지 않는다(무필터로 찾았는데 "중분류를 훑었다"고 하면 거짓 고지가 된다).

    [PR #411 Claude 리뷰 2라운드] 이 재검색은 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 **실제로 성공**하는 경로 자체를 보므로 가드를 끈다."""
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101, 102) if filters.category is None else _res()  # 확장 leg 은 전부 0건

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    # 확장 leg 8개 검색 + 무필터 재검색 정확히 1회 — 무한 폴백이 아니다.
    assert calls.count(None) == 1
    assert {c for c in calls if c is not None} == {c for c, _ in _BROAD_LEAVES}
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"  # 결과가 노출됐다
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert not any("메이크업" in t for t in tokens)  # 확장 고지 미발신


async def test_zero_result_expansion_fallback_still_empty_degrades_to_zero_result() -> None:
    """[F-1] 무필터 재검색도 0건이면 종전과 같은 zero_result 로 정상 종료한다(SEARCH_FAILED 아님) —
    재검색 자체가 실패한 게 아니라 정말 맞는 상품이 없는 경우다."""

    async def _search(filters, exclude_product_ids=None):
        return _res()  # 확장 leg 도 무필터 재검색도 전부 0건

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert not any(e["type"] == "error" for e in events)
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"


async def test_normal_fanout_zero_result_is_not_rescued_by_unfiltered_fallback() -> None:
    """[F-1 경계] 확장 턴이 **아닌** 일반 fan-out(사용자가 명시한 카테고리)의 0건은 종전대로
    무필터로 되돌리지 않는다 — 명시 카테고리를 조용히 풀면 "표시=실제"(#51)가 깨진다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res()  # 명시 카테고리 leg 도 0건

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),  # DEFAULT_DECOMPOSE — categoryQueries 로 명시 매핑(확장 아님)
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),  # legs=[...] 로 바로 채워짐 → category_expanded=False
        )
    )
    assert None not in calls  # 무필터 재검색이 붙지 않았다
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"


async def test_expanded_turn_unfiltered_rescue_skips_spring_when_payload_empty() -> None:
    """[PR #411 Claude 리뷰 2라운드] 확장 턴은 `filters.category` 가 이미 None 이고(PR #318
    R6-1) 다른 축도 없으면(`_broad_decompose` 의 `filters: {}`) F-1 무필터 재검색(`_run_search_
    unfiltered`) 의 payload 가 파라미터 0개다 — 이 PR 이 막으려는 바로 그 12.3MB 무필터 I-1
    이라 Spring 을 아예 부르지 않는다. 운영에서는 어차피 3초 타임아웃으로 예외가 나 원래(0건)
    결과를 유지하는 것과 결과가 같다 — 3초만 아낀다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res()  # 확장 leg 전부 0건

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )

    # 본 검색(확장 leaf 8개)만 나갔다 — F-1 무필터 재검색(category=None)은 Spring 을 안 불렀다.
    assert len(calls) == len(_BROAD_LEAVES)
    assert None not in calls
    assert not any(e["type"] == "error" for e in events)
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"


async def test_expanded_turn_unfiltered_rescue_can_be_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[롤백] `search_filter_guard_enabled=False` 면 종전대로 F-1 무필터 재검색이 나간다."""
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res()  # 확장 leg + 무필터 재검색 전부 0건

    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )

    # 확장 leaf 8개 + 무필터 재검색(category=None) 1회 — 검색 호출이 한 번 더 늘어난다.
    assert len(calls) == len(_BROAD_LEAVES) + 1
    assert calls.count(None) == 1


# ── #343 — 억제-후 재판정: 확장 leg 검색은 히트를 내는데 최근구매 exact 제외·소모품 카테고리
# 억제(`_post_filter`)가 그 전량을 지워 candidates 가 0이 되는 턴을 무필터 재검색으로 구제한다.
# 위 F-1 은 억제 **이전** `search_result.total_count` 만 보므로 이 갭을 못 잡는다(PR #318 리뷰
# R6-4). 이 절의 테스트는 `_member()`(user_id="u1", 비숫자) 대신 숫자 sub 회원을 써야 최근구매
# 조회(I-19)가 실제로 돈다(lessons "popular_fn 미주입" 항목과 같은 종류의 함정 — 여기서는
# `get_recent_purchases` 를 fake 로 주입해도 identity.user_id 가 `int()` 로 안 바뀌면 조용히
# dedup 이 스킵된다).

import app.services.spring_client as _sc_mod  # noqa: E402
from app.schemas.spring import OrderHistory, OrderHistoryItem, RecentPurchases  # noqa: E402

# `_member_num` 은 아래 R11-1 절(~2952행)에 이미 정의돼 있다 — 이 절도 그 정의를 그대로 쓴다
# (같은 이름을 두 번 정의하면 나중 정의가 이겨 앞 정의가 죽은 코드가 되고, 한쪽만 고치면 조용히
# 갈라진다).


def _fix_now(monkeypatch: pytest.MonkeyPatch, when=datetime(2026, 7, 19)) -> None:
    monkeypatch.setattr("app.agents.buyer.recommendation.graph._now", lambda: when)


def _purchases_cat(*items: tuple[int, str, str]):
    """items = (productId, category, name) — 최근 구매 이력 fake(exact 제외·소모품 억제 공용)."""

    async def _fn(user_id, status=None):
        return RecentPurchases(
            orders=[
                OrderHistory(
                    order_id=1,
                    ordered_at="2026-07-15T00:00:00",
                    items=[
                        OrderHistoryItem(
                            order_item_id=idx, product_id=pid, category=cat, product_name=name
                        )
                        for idx, (pid, cat, name) in enumerate(items, 1)
                    ],
                )
            ]
        )

    return _fn


def _res_cat(*pairs: tuple[int, str]) -> ProductSearchResult:
    """productId·category 쌍으로 결과를 만든다(`_res` 는 category 를 "c" 로 고정해 소모품
    카테고리 억제를 재현할 수 없다)."""
    products = [
        SpringProduct(
            product_id=pid, name=f"P{pid}", price=1000, rating=4.0, category=cat, brand="b"
        )
        for pid, cat in pairs
    ]
    return ProductSearchResult(products=products, total_count=len(products))


async def test_post_suppress_zero_result_rescued_by_unfiltered_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#343 갭 증명] 확장 leg 검색은 히트를 내지만(101) 그 전량이 최근구매 exact 제외에 걸려
    candidates 가 0이 된다 — 무필터 재검색이 억제 대상이 아닌 상품을 돌려주면 채택해 노출하고,
    확장 고지 token 은 내지 않는다(무필터로 찾았는데 "중분류를 훑었다"는 거짓 고지가 된다).

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        if filters.category is None:  # 무필터 재검색
            return _res(201, 202)
        return _res(101)  # 확장 leg 은 전부 최근구매 101 만 낸다 → 사후필터가 전량 제외

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert calls.count(None) == 1  # 무필터 재검색 정확히 1회 — 무한 폴백이 아니다
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"  # 결과가 노출됐다
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert not any("메이크업" in t for t in tokens)  # 확장 고지 미발신


async def test_post_suppress_fallback_reapplies_post_filter_to_unfiltered_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#343 이중 억제] 무필터 재검색 결과에 1라운드에서 억제된 상품(101)이 다시 섞여 있어도,
    재적용된 `_post_filter` 가 다시 걸러낸다 — 채택된 결과에 101 이 없어야 한다.

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:
            return _res(101, 201)  # 무필터 결과에 억제 대상 101 이 다시 섞여 있다
        return _res(101)

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=push,
            map_categories=_broad_mapper(),
        )
    )
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"
    exposed = set(push.pushes[0].lists[0].product_ids)
    assert 101 not in exposed  # 재적용된 사후필터가 다시 억제했다
    assert 201 in exposed


async def test_post_suppress_fallback_also_fully_suppressed_degrades_to_zero_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#343 재검색도 전량 억제] 무필터 재검색 결과도 전량 소모품 억제 대상이면 채택하지 않고
    zero_result 로 정상 종료한다(error 아님) — 되돌리기 칩은 원래(1라운드) 억제 상태 기준으로
    나가야 한다(재검색분 suppressed_by_cat 으로 교체되면 안 된다). `recommend_zero_result` 로그의
    `post_suppress_fallback_attempted` 로 폴백 시도 여부를 관측할 수 있어야 한다."""
    monkeypatch.setattr(get_settings(), "consumable_categories", ["생활용품"])
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "생활용품", "휴지")))
    leaf_order = [c for c, _ in _BROAD_LEAVES]

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:
            return _res_cat((501, "생활용품"))  # 무필터 재검색도 전량 소모품 억제 대상
        idx = leaf_order.index(filters.category)
        return _res_cat((100 + idx, "생활용품"))  # 8 leg 전부 소모품 카테고리 상품만 낸다

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert not any(e["type"] == "error" for e in events)
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"
    suggestions = next(e for e in events if e["type"] == "suggestions")["data"]
    revert_chip = next(c for c in suggestions["chips"] if c.get("revert") is not None)
    assert revert_chip["estCount"] == 8  # 원래(1라운드) 억제 수 그대로 — 재검색분으로 안 바뀜
    zero_log = next(r for r in caplog.records if _event(r, "recommend_zero_result"))
    assert zero_log.post_suppress_fallback_attempted is True


async def test_post_suppress_fallback_reapply_failure_keeps_original_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#343 F-1 리뷰] 무필터 재검색은 성공하는데 그 결과에 재적용된 `_post_filter` 가 예외를
    내면(부가 기능 실패) 원래(억제된) 상태를 유지한 채 zero_result 로 정상 종료한다 — conditions·
    zero_result 안내 없이 스트림이 죽으면 안 된다(§7 "부가 기능 실패가 턴을 죽이지 않는다").

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    class _ExplodingProduct:
        """`.category` 접근 시 예외를 낸다 — 재적용된 `_post_filter` 만 이 상품을 만난다."""

        product_id = 999

        @property
        def category(self):
            raise RuntimeError("post_filter boom")

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:  # 무필터 재검색 — 성공하지만 사후필터 재적용이 터진다
            return SimpleNamespace(products=[_ExplodingProduct()], total_count=1)
        return _res(101)  # 확장 leg 전량 최근구매 101 만 낸다 → 사후필터가 전량 제외

    caplog.set_level("WARNING", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert not any(e["type"] == "error" for e in events)  # conditions·zero_result 로 정상 종료
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"
    warn = next(
        r for r in caplog.records if r.msg == "category_expand_post_suppress_fallback_failed"
    )
    assert warn.reason == "post_filter boom"


async def test_post_suppress_fallback_flag_off_keeps_prior_zero_result_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#343 플래그 off] `category_expand_post_suppress_fallback_enabled=False` 면 억제-후
    재판정이 발동하지 않는다(무필터 재검색 없음) — 종전 동작(zero_result) 고정."""
    monkeypatch.setattr(get_settings(), "category_expand_post_suppress_fallback_enabled", False)
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        if filters.category is None:
            return _res(201, 202)  # 재검색이 붙는다면 이 결과가 노출됐어야 한다
        return _res(101)  # 확장 leg 전량 최근구매 101 만 낸다 → 사후필터가 전량 제외

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert None not in calls  # 무필터 재검색 미발동
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"


async def test_post_suppress_fallback_does_not_trigger_on_non_expanded_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#343 비확장 경계] `category_expanded=False` 인 일반 fan-out(사용자가 명시한 카테고리)
    턴에서 전량 억제돼도 재검색은 발동하지 않는다 — 명시 카테고리를 조용히 풀면 "표시=실제"(#51)
    가 깨진다(기존 F-1 경계 테스트의 억제 버전)."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)  # 명시 카테고리 leg 이 히트를 내지만 전량 최근구매로 억제된다

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),  # DEFAULT_DECOMPOSE — categoryQueries 로 명시 매핑(확장 아님)
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),  # legs=[...] 로 바로 채워짐 → category_expanded=False
        )
    )
    assert None not in calls  # 무필터 재검색이 붙지 않았다
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"


async def test_post_suppress_fallback_skipped_when_pre_suppress_f1_already_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#343 상호배타] 검색 자체가 0건이면 기존(억제-이전) F-1 폴백이 무필터 재검색을 이미
    소비한다 — 그 결과가 전량 억제돼도 두 번째 무필터 재검색은 없다(턴당 무필터 재검색 왕복은
    최대 1회, `category_expand_notice_suppressed` 상호배타 가드).

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        if filters.category is None:
            # F-1 이 이미 소비한 무필터 재검색 — 히트는 있지만(101) 최근구매로 전량 억제된다.
            return _res(101)
        return _res()  # 확장 leg 전량 0건 → 기존 F-1 발동

    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert calls.count(None) == 1  # F-1 이 쓴 1회뿐 — #343 이 두 번째를 돌리지 않았다
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"


# ── #363 — 구제 체인 지연 계측 + 최악 경로 순차 왕복 상한 회귀 가드 ──────────────────


async def test_f1_fallback_success_log_includes_elapsed_ms(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[#363 AC1] F-1 성공 로그(`category_expand_zero_fallback`)에 이 왕복의 `elapsed_ms` 가
    실린다 — 배포 후 지연 분포를 로그만으로 관측하기 위한 계측(설계 근거는
    docs/specs/MEASURE-FIRST-TOKEN-363.md).

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102) if filters.category is None else _res()  # 확장 leg 은 전부 0건

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    record = next(r for r in caplog.records if _event(r, "category_expand_zero_fallback"))
    assert isinstance(record.elapsed_ms, int)
    assert record.elapsed_ms >= 0


async def test_post_suppress_fallback_success_log_includes_elapsed_ms(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#363 AC1] #343 성공 로그(`category_expand_post_suppress_fallback`)에도 재검색 +
    `_post_filter` 재적용까지의 `elapsed_ms` 가 실린다.

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:  # 무필터 재검색
            return _res(201, 202)
        return _res(101)  # 확장 leg 은 전부 최근구매 101 만 낸다 → 사후필터가 전량 제외

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"  # 재판정이 구제했다
    record = next(r for r in caplog.records if _event(r, "category_expand_post_suppress_fallback"))
    assert isinstance(record.elapsed_ms, int)
    assert record.elapsed_ms >= 0


async def test_post_suppress_fallback_reapply_failure_counts_rescue_elapsed_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#363 R4] `_post_filter` 재적용 자체가 예외를 내는 경로(원본 시나리오는
    `test_post_suppress_fallback_reapply_failure_keeps_original_state`, 그 fake 패턴을 그대로
    쓴다)에서 `rescue_elapsed_ms`가 이 왕복을 **정확히 1회만** 반영하는지 지연을 주입해 수치로
    확인한다. 고쳐진 코드는 `_post_filter` 성공 시점에만 값을 미리 계산해 두고(예외 시엔 None
    유지), `finally` 한 곳에서만 더한다 — try 본문과 except 양쪽에서 각자 더하던 이전 구조라면
    이 경로에서도(그리고 `_post_filter` 성공 뒤 상태 반영 단계가 나중에 실패하는, 로그로는
    관측되지 않는 다른 하위 경로에서는 확실히) 이중 계상이 재발할 수 있다.

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는(그 뒤 재적용이 실패하는) 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    delay_s = 0.05  # [#363 R5 와 같은 이유] 이중 계상 여부를 시간값으로 구분하려면 실측 가능한
    # 지연이 있어야 한다 — 0이면 두 번 더해도 여전히 0이라 회귀를 못 잡는다.

    class _ExplodingProduct:
        """`.category` 접근 시 예외를 낸다 — 재적용된 `_post_filter` 만 이 상품을 만난다."""

        product_id = 999

        @property
        def category(self):
            raise RuntimeError("post_filter boom")

    async def _search(filters, exclude_product_ids=None):
        await asyncio.sleep(delay_s)
        if filters.category is None:  # 무필터 재검색 — 성공하지만 사후필터 재적용이 터진다
            return SimpleNamespace(products=[_ExplodingProduct()], total_count=1)
        return _res(101)  # 확장 leg 전량 최근구매 101 만 낸다 → 사후필터가 전량 제외

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert not any(e["type"] == "error" for e in events)
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"
    warn = next(
        r for r in caplog.records if r.msg == "category_expand_post_suppress_fallback_failed"
    )
    assert warn.reason == "post_filter boom"

    zero_log = next(r for r in caplog.records if _event(r, "recommend_zero_result"))
    # [#363 R4] 왕복 1회분(delay_s ≈ 50ms)만 반영돼야 한다 — 이중 계상 버그가 재발하면 같은
    # 구간이 두 번 더해져 대략 2배(≈100ms)로 튄다. 상한을 1.5배 지점에 둬 그 둘을 가른다.
    assert zero_log.rescue_elapsed_ms >= round(delay_s * 1000 * 0.5)
    assert zero_log.rescue_elapsed_ms < round(delay_s * 1000 * 1.5)


async def test_post_suppress_fallback_unfiltered_search_failure_counts_rescue_elapsed_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#363 R4] 무필터 재검색 자체가 실패해 `_run_search_unfiltered()`가 None 을 돌려주는
    (`fallback_bundle is None`) 경로 — 기존 6건 어디도 이 하위 경로를 caplog 로 수치 검증하지
    않았다. `_post_filter`를 아예 못 부르니 이중 계상 위험 자체는 없지만(else 분기가 단일
    누적), 세 경로(성공·`_post_filter`예외·재검색자체실패) 모두 정확히 1회라는 R4 요구를
    이 경로까지 실제로 채워 고정한다.

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막아
    `search()` 콜러블 자체가 안 불린다 — 이 테스트는 **`search()` 가 실제로 불렸다가 실패하는**
    경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    delay_s = 0.05

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:  # 무필터 재검색 — 재시도까지 전부 실패
            await asyncio.sleep(delay_s)
            raise SpringUnavailableError("unfiltered search down")
        return _res(101)  # 확장 leg 전량 최근구매 101 만 낸다 → 사후필터가 전량 제외

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert not any(e["type"] == "error" for e in events)
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"

    zero_log = next(r for r in caplog.records if _event(r, "recommend_zero_result"))
    assert zero_log.post_suppress_fallback_attempted is True
    assert zero_log.rescue_elapsed_ms >= round(delay_s * 1000 * 0.5)
    assert zero_log.rescue_elapsed_ms < round(delay_s * 1000 * 1.5)


async def test_recommend_pipeline_logs_rescue_elapsed_when_fallback_succeeds_may_auto_relax_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#363 R7] 구제(#343)가 **성공**해 `not candidates` 분기를 안 타는 턴은
    `recommend_zero_result`가 아니라 `recommend_pipeline`으로 내려간다 — 이 이슈가 재려는
    "지연을 감수하고 구제된" 표본은 그 로그에만 있다. `_broad_decompose()` 기본값은 비카테고리
    완화 필드가 하나도 안 걸려 `may_auto_relax=False`다(비교 대상은 아래 `..._true` 테스트).
    지연을 주입해 `rescue_elapsed_ms > 0`이 우연이 아님을 수치로 보장한다(0 비교만 하면
    vacuous하게 통과할 수 있다 — 상한도 같이 걸어 다른 값이 새어 들어온 게 아님을 확인).

    [PR #411 Claude 리뷰 2라운드] 재검색이 payload 파라미터 0개라 기본값에선 가드가 막는다 —
    이 테스트는 재검색이 실제로 성공하는 경로를 보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    delay_s = 0.05

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:  # 무필터 재검색 — 성공, 201 이 살아남는다
            await asyncio.sleep(delay_s)
            return _res(101, 201)
        return _res(101)  # 확장 leg 전량 최근구매 101 만 낸다 → 사후필터가 전량 제외

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=_broad_decompose()),  # filters={} — 완화 후보 없음
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"  # 재판정이 구제해 성공 종결로 내려갔다

    pipeline_log = next(r for r in caplog.records if _event(r, "recommend_pipeline"))
    assert not any(_event(r, "recommend_zero_result") for r in caplog.records)  # 상호 배타 확인
    assert pipeline_log.rescue_elapsed_ms >= round(delay_s * 1000 * 0.5)
    assert pipeline_log.rescue_elapsed_ms < round(delay_s * 1000 * 1.5)
    # candidates 가 #343 에서 이미 채워져 자동완화 루프 자체가 안 돈다(게이트가 `not candidates`).
    assert pipeline_log.relax_auto_elapsed_ms == 0
    assert pipeline_log.relax_chip_elapsed_ms >= 0
    assert pipeline_log.may_auto_relax is False


async def test_recommend_pipeline_logs_may_auto_relax_true_when_relaxable_field_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#363 R7] 위 테스트와 짝을 이뤄 `may_auto_relax` 가 상수가 아니라 실제로 True/False 로
    갈리는지 확인한다(한쪽만 보면 구현이 상수를 실어도 테스트가 통과한다) — `ratingMin` 이
    설정된 턴은 `may_auto_relax=True`(§3 근거: `build_relaxation_candidates` 가 후보를 내는
    턴과 conditions 를 검색 뒤로 미루는 턴은 같은 판정을 공유한다). 구제(#343) 자체는 여기서도
    성공해 `recommend_pipeline` 으로 내려간다.

    [PR #411 Claude 리뷰 2라운드] `ratingMin` 은 Spring payload 축이 아니라 재검색이 여전히
    파라미터 0개라 기본값에선 가드가 막는다 — 이 테스트는 재검색이 실제로 성공하는 경로를
    보므로 가드를 끈다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((101, "c", "이전 구매")))
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)

    async def _search(filters, exclude_product_ids=None):
        if filters.category is None:  # 무필터 재검색 — 성공, 201 이 살아남는다
            return _res(101, 201)
        return _res(101)  # 확장 leg 전량 최근구매 101 만 낸다 → 사후필터가 전량 제외

    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "filters": {"ratingMin": 4.5},  # 비카테고리 완화 후보 1개 — may_auto_relax=True 를 만든다
        "categoryQueries": [{"category": None, "query": "화장품"}],
    }
    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    events = await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member_num(),
            llm=FakeLLM(decompose=decompose),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"

    pipeline_log = next(r for r in caplog.records if _event(r, "recommend_pipeline"))
    assert pipeline_log.may_auto_relax is True


async def test_worst_case_rescue_chain_sequential_stages_before_first_sse(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[#363 최악 경로 회귀 가드] 확장 턴이 검색 히트를 내지만(①) 최근구매 억제가 전량 제거하고
    (②) #343 무필터 재검색도 전량 억제되며(③) 자동완화 probe 도 실패하는(④) 턴에서, **첫 SSE
    이벤트(conditions) 발신 이전** 순차 Spring 왕복은 정확히 3단이다: 초기 fan-out(leg 8개 병렬
    1단) + #343 무필터 폴백(1단) + 자동완화 probe(`relaxation_auto_fields=["ratingMin"]` 하나뿐
    이라 후보도 1개 — 최대 1라운드, 1단). PR #362 리뷰가 지적한 "3단 순차 적층 ≈9s"
    (spring_timeout_s=3s × 3단)와 정확히 일치한다(docs/specs/MEASURE-FIRST-TOKEN-363.md §4 산출
    근거).

    완화 **칩** probe(잠재적 4번째 단)는 conditions 가 이미 나간 **뒤**에 돈다 — 비카테고리 완화
    후보(ratingMin)가 있는 턴은 `may_auto_relax=True` 라 conditions 를 자동완화 루프 직후·칩
    probe **이전**에 내보내기 때문이다(graph.py `if may_auto_relax: yield sse("conditions", ...)`,
    자동완화 루프 다음·칩 probe 앞). 이 순서가 "구제 체인이 first-token 을 얼마나 미루는가"라는
    이 이슈의 실측 핵심이다 — 칩 probe 가 conditions 보다 앞으로 오게 바뀌면(=first-token 이 한
    단 더 늦어지면) 이 테스트가 실패해야 한다.

    [PR #411 Claude 리뷰 2라운드] `ratingMin` 은 Spring payload 축이 아니라 #343 무필터 재검색이
    여전히 파라미터 0개라 기본값에선 가드가 막는다 — 이 테스트는 3단이 실제로 순차 실행되는
    경로를 보므로 가드를 끈다.
    """
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["생활용품"])
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "생활용품", "휴지")))
    leaf_order = [c for c, _ in _BROAD_LEAVES]

    # [#363 R5] 0.01s(임계 5ms)는 부하 걸린 CI 러너에서 병렬 fan-out 8개의 시작 시각 산포가
    # 임계를 넘어 한 단이 둘로 갈릴 수 있다 — 0.05s(임계 25ms)로 올려 "병렬 시작 시각의 산포
    # ≪ 단 간격" 여유를 넉넉히 둔다(4단 × 0.05s = 총 0.2s 수준의 추가 실행 시간만 든다).
    delay_s = 0.05  # [지연 특성 관측] 단마다 이만큼 걸린다고 두고 합산 지연을 하한으로 잰다.
    call_starts: list[float] = []

    async def _search(filters, exclude_product_ids=None):
        call_starts.append(time.monotonic())
        await asyncio.sleep(delay_s)
        if filters.category is None:
            return _res_cat((501, "생활용품"))  # #343 무필터 재검색도 전량 소모품 억제 대상
        if filters.rating_min is not None and filters.rating_min < 4.5:
            # 자동완화·칩 probe 가 쓰는 완화 필터(평점 4.0) — 둘 다 전량 실패시킨다.
            raise SpringUnavailableError("relaxation probe down")
        idx = leaf_order.index(filters.category)
        return _res_cat((100 + idx, "생활용품"))  # 8 leg 전부 소모품 카테고리 상품만 낸다

    caplog.set_level("INFO", logger="app.agents.buyer.recommendation.graph")
    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "filters": {"ratingMin": 4.5},  # 비카테고리 완화 후보 1개 — may_auto_relax=True 를 만든다
        "categoryQueries": [{"category": None, "query": "화장품"}],
    }
    turn_started_at = time.monotonic()
    events: list[tuple[float, dict]] = []
    async for frame in run_buyer_turn(
        _req(message="화장품 추천해줘"),
        _member_num(),
        llm=FakeLLM(decompose=decompose),
        search=_search,
        push_fn=_RecordingPush(),
        map_categories=_broad_mapper(),
    ):
        line = frame.strip()
        if line.startswith("data:"):
            events.append((time.monotonic(), json.loads(line[len("data:") :].strip())))

    # progress 다회 emit(#396) — analyzing·mapping·searching·relaxing 4개가 conditions 앞에
    # 온다(이 턴은 may_auto_relax=True 라 conditions 가 자동완화 루프 뒤로 미뤄지고, 그 루프가
    # 실제로 probe 하므로 relaxing 도 낀다). 이 테스트가 재는 "첫 SSE" 는 순차 단(fan-out) 완료
    # 뒤 나가는 첫 실질 이벤트(conditions)라 events[4] 로 옮긴다 — progress 프레임 자체는 I/O
    # 없이 즉시 나가 call_starts 클러스터링에 영향을 주지 않는다.
    assert [e["type"] for _, e in events[:4]] == ["progress"] * 4
    assert [e["data"]["stage"] for _, e in events[:4]] == [
        "analyzing",
        "mapping",
        "searching",
        "relaxing",
    ]
    assert events[4][1]["type"] == "conditions"  # 이 턴의 첫 실질 SSE 이벤트
    first_sse_at = events[4][0]
    done = next(e for _, e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"

    # 순차 "단" 경계는 시작 시각 간격으로 클러스터링한다 — 병렬 fan-out 은 거의 동시에 시작하니
    # (간격 ≪ delay_s) 한 단으로 뭉치고, 단 사이는 실제 검색 지연(delay_s)만큼 벌어진다.
    ordered = sorted(call_starts)
    stages: list[list[float]] = []
    for t in ordered:
        if stages and t - stages[-1][-1] <= delay_s / 2:
            stages[-1].append(t)
        else:
            stages.append([t])

    # 호출 수(병렬 fan-out 포함 총 25회)와 순차 단 수(4단)를 구분해서 기록한다.
    assert [len(s) for s in stages] == [8, 1, 8, 8]  # fan-out / #343 폴백 / 자동완화 / 칩 probe
    assert sum(len(s) for s in stages) == 25

    stages_before_first_sse = [s for s in stages if s[0] < first_sse_at]
    # [#363 회귀 가드] 여기가 3보다 커지면 first-token 이전 순차 왕복이 한 단 더 늘어난 것이다.
    assert len(stages_before_first_sse) == 3
    calls_before_first_sse = sum(len(s) for s in stages_before_first_sse)
    assert calls_before_first_sse == 17  # 8(fan-out) + 1(#343 폴백) + 8(자동완화 probe)

    # 단 수만큼 합산 지연이 실제로 나타난다 — flaky 방지를 위해 하한만 assert(PR #248 패턴과 동일).
    elapsed_before_first_sse = first_sse_at - turn_started_at
    assert elapsed_before_first_sse >= 3 * delay_s

    zero_log = next(r for r in caplog.records if _event(r, "recommend_zero_result"))
    assert zero_log.post_suppress_fallback_attempted is True
    assert zero_log.rescue_elapsed_ms > 0  # #343 폴백 왕복(결과는 전량 억제)에 쓴 소요
    assert zero_log.relax_probes == 2  # 자동완화 1 + 칩 probe 1(같은 유일 필드 ratingMin 재시도)
    # [#363 R3] 자동완화(first SSE 이전)와 칩 probe(first SSE 이후)를 별도 필드로 갈라 관측한다 —
    # 합쳐진 단일 필드였다면 아직 스트림에 영향 없는 칩 probe 소요까지 first-token 지연에
    # 섞여 들어간다.
    assert zero_log.relax_auto_elapsed_ms > 0  # 자동완화 왕복(실패)에 쓴 소요 — first SSE 이전
    assert zero_log.relax_chip_elapsed_ms > 0  # 칩 probe 왕복(실패)에 쓴 소요 — first SSE 이후


# ── R4-1 (PR #318 리뷰) — 확장 턴은 split_by_need 를 통과하면 안 된다 ──────────────
#
# 확장 leaf(§4·`_collect_expansion_leaves`)는 **한 실패 leg 에서 파생된 같은 의도의 후보들**이지,
# 사용자가 말한 서로 다른 니즈가 아니다 — 8개가 전부 같은 query 텍스트를 공유한다
# (`category_mapping.py` 의 `(canonical, qtexts[i])` 규약). `split_by_need` 가 이를 모르고
# `case==3 and len(need_legs)>1 and bool(leg_of)` 만 보면, case 3 턴에서 원 매핑도 #217 전개도
# 전부 실패해 #222 확장 폴백이 발동한 경우 **이름이 같은 목록 8개**로 쪼개진다(`_need_label` 이
# 공유 query 를 그대로 라벨로 씀) — 조건 칩을 `category_expanded` 로 억제한 것과 같은 원칙
# (#51 표시=실제)이 목록 분할에는 빠져 있었다. `buy_all_mode` 도 `split_by_need` 를 참조하므로
# 가짜 니즈 단위 BUY_ALL 예산 세트까지 함께 새어 나갈 수 있었다.


async def test_expanded_case3_turn_pushes_single_list_not_split_by_expansion_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R4-1] case 3 + 매핑 전량 실패 + 확장 폴백 발동 → 목록이 1개다(확장 leaf 8개로 안 쪼개진다)."""
    # #217 을 끄고 더 쉬운 경로로 간다(리뷰가 지적한 재현 경로 ①) — 매핑이 바로 빈 legs 를 내고
    # #222 확장 폴백만 발동한다.
    monkeypatch.setattr(get_settings(), "needs_expansion_enabled", False)
    leaf_order = [c for c, _ in _BROAD_LEAVES]

    async def _search(filters, exclude_product_ids=None):
        # leg 마다 **서로 다른** productId 를 내야 round-robin 병합이 leg 마다 다른 leg_of 를
        # 배정한다 — 모든 leg 가 같은 productId 를 내면 병합 dedup 이 전부 leg 0 로 흡수해
        # split_by_need 값과 무관하게 목록이 우연히 1개가 되므로(가짜 통과), 결함을 재현하지 못한다.
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose(case=3)),
            search=_search,
            push_fn=push,
            map_categories=_broad_mapper(),
        )
    )
    assert len(push.pushes[0].lists) == 1
    assert push.pushes[0].list_type == "PICK_ONE"


async def test_expanded_case3_turn_does_not_trigger_buy_all_budget_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R4-1] 확장 폴백 + `buyAll=True` + `budget_set_enabled=True`(기본값) 여도 BUY_ALL 예산
    세트가 발동하지 않는다 — `buy_all_mode` 가 `split_by_need` 를 참조하므로 975행만 고치면
    자동으로 함께 막힌다(1075행 `buy_all_mode` 줄 자체는 건드리지 않았다)."""
    monkeypatch.setattr(get_settings(), "needs_expansion_enabled", False)
    assert get_settings().budget_set_enabled is True  # 전제 확인 — off 라 안 막힌 게 아니다

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102)

    decompose = _broad_decompose(case=3)
    decompose["buyAll"] = True
    decompose["totalBudget"] = 50_000

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=decompose),
            search=_search,
            push_fn=push,
            map_categories=_broad_mapper(),
        )
    )
    assert push.pushes[0].list_type == "PICK_ONE"  # BUY_ALL 아님


async def test_non_expanded_case3_multi_need_still_splits_after_fix() -> None:
    """[R4-1 회귀 고정] 확장이 **아닌** 진짜 case-3 멀티 니즈(매핑 성공, `category_expanded=False`)
    는 종전대로 니즈별로 쪼개진다 — 이 수정이 #209/#168 니즈 분할 경로를 죽이지 않았다는 증거."""
    push = _RecordingPush()
    llm = _needs_llm(
        [
            {"productId": 101, "rationale": "수납이 좋아요"},
            {"productId": 201, "rationale": "220V 지원이에요"},
        ]
    )

    await _run_case3(llm, push)  # map_categories=_two_leg_mapper() → legs 바로 채움(확장 아님)

    assert len(push.pushes[0].lists) == 2
    assert [entry.label for entry in push.pushes[0].lists] == ["파우치", "어댑터"]
    assert push.pushes[0].list_type == "PICK_ONE"


async def test_expanded_turn_with_two_unresolved_legs_splits_by_need() -> None:
    """[이슈 #168 T3] unresolved leg 이 2개(서로 다른 query "캠핑용품"·"낚시용품")이고 둘 다
    확장 leaf 로 대체된 턴은 목록이 **니즈(query) 단위로 2개**로 쪼개진다 — leaf 인덱스 그대로
    분할하면(leaf 4개, 니즈 2개) leaf 당 목록(라벨 중복 "캠핑용품"×2·"낚시용품"×2)이 되어 R4-1
    이 재발하므로, `leg_of`/`need_legs` 를 leaf→니즈 인덱스로 번역해 니즈 단위로 나눈다.

    이전(R12-2)에는 이 케이스가 "목록 1개(분할 안 함)"로 고정돼 있었고, 그 테스트 docstring 이
    "#168 이 니즈 단위 그룹핑을 구현하면 의도적으로 바뀐다"고 예고한 바로 그 지점이다 — #168
    구현으로 이 테스트가 그 예고대로 갱신됐다. 단일 query 확장 턴(leaf 8개가 전부 같은 query
    공유)은 여전히 목록 1개로 남는다(아래
    `test_expanded_case3_turn_pushes_single_list_not_split_by_expansion_leaves` 참조).
    """
    leaves = [
        ("캠핑 > 텐트", "캠핑용품"),
        ("낚시 > 릴", "낚시용품"),
        ("캠핑 > 침낭", "캠핑용품"),
        ("낚시 > 낚싯대", "낚시용품"),
    ]
    leaf_order = [c for c, _ in leaves]

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[], unresolved=["캠핑용품", "낚시용품"], expansion_leaves=list(leaves)
        )

    async def _search(filters, exclude_product_ids=None):
        # leg 마다 다른 productId 를 내야 병합이 leg 마다 다른 leg_of 를 배정한다(위 R4-1
        # 테스트와 같은 이유 — 같은 id 면 dedup 이 흡수해 분할 여부와 무관하게 목록이 1개가
        # 되는 가짜 통과가 나온다).
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="캠핑용품이랑 낚시용품 추천해줘"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [
                        {"category": None, "query": "캠핑용품"},
                        {"category": None, "query": "낚시용품"},
                    ],
                }
            ),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    lists = push.pushes[0].lists
    assert len(lists) == 2
    assert [entry.label for entry in lists] == ["캠핑용품", "낚시용품"]
    # leaf 100·102 는 "캠핑용품" 니즈, leaf 101·103 은 "낚시용품" 니즈 — 니즈 간 상품이 섞이지
    # 않는다(#168 T3 의 핵심 불변식, R4-1 재발 방지의 반대편 증거).
    assert set(lists[0].product_ids) == {100, 102}
    assert set(lists[1].product_ids) == {101, 103}
    assert push.pushes[0].list_type == "PICK_ONE"


# ── PR #351 리뷰 R3-1 — T3 그룹핑은 query 가 전부 실재할 때만 ──────────────────────────
#
# `query=None` 인 확장 leaf(raw 만 있던 unresolved leg 파생)가 서로 다른 니즈 2개 이상에서
# 나오면 `None` 하나의 키로 뭉쳐 서로 다른 니즈의 상품이 한 그룹에 섞인다 — None 이 하나라도
# 섞이면 번역하지 않고 단일 목록(T3 이전 동작)으로 안전 후퇴해야 한다.


async def test_expanded_turn_with_none_query_legs_falls_back_to_single_list() -> None:
    """[PR #351 R3-1 fail-first] query=None 인 leaf 가 서로 다른 두 leg 에서 나오고, 실제
    query 를 가진 leaf 도 하나 섞인 확장 턴 → 목록이 **1개**다(섞인 그룹이 만들어지지 않는다).

    수정 전 코드는 `None` 을 하나의 키로 취급해 distinct query 가 2(None·"실니즈")로 세지고,
    서로 무관한 두 leg(leafA·leafB)의 상품이 "None" 그룹 하나로 뭉쳐 목록 2개로 쪼개졌다 —
    이 테스트는 수정 전엔 `len(lists) == 2`(그것도 섞인 그룹 포함)로 실패해야 한다."""
    leaves = [
        ("카테고리A > leafA", None),  # raw 만 있던 unresolved leg 1 파생
        ("실니즈 > leafC", "실니즈"),  # query 가 있는 leaf
        ("카테고리B > leafB", None),  # raw 만 있던 unresolved leg 2 파생 — leafA 와 다른 leg 기원
    ]
    leaf_order = [c for c, _ in leaves]

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[], unresolved=["카테고리A", "실니즈", "카테고리B"], expansion_leaves=list(leaves)
        )

    async def _search(filters, exclude_product_ids=None):
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="이것저것 다 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose(case=3)),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    assert len(push.pushes[0].lists) == 1  # None 충돌로 안전 후퇴 — leafA·leafB 가 섞이지 않는다


async def test_expanded_turn_all_none_query_legs_stays_single_list() -> None:
    """[PR #351 R3-1 명시 고정] 확장 leaf 전부가 query=None 이면(현행이지만 명시 고정) 목록은
    1개다 — distinct query 가 `{None}` 하나뿐이라 애초에 번역 조건(`> 1`)에 도달하지 않는다."""
    leaves = [
        ("카테고리A > leafA", None),
        ("카테고리B > leafB", None),
    ]
    leaf_order = [c for c, _ in leaves]

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[], unresolved=["카테고리A", "카테고리B"], expansion_leaves=list(leaves)
        )

    async def _search(filters, exclude_product_ids=None):
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="이것저것 다 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose(case=3)),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    assert len(push.pushes[0].lists) == 1


async def test_expanded_turn_same_text_query_legs_merge_into_one_group() -> None:
    """[PR #351 R3-1 병합 의도 고정] 서로 다른 두 unresolved leg 이 우연히 같은 query 텍스트
    ("아웃도어용품")를 내고, 세 번째 leg 은 다른 query("캠핑용품")를 내는 확장 턴 → 목록은
    **2개**이고 "아웃도어용품" 목록엔 **두 leg 의 상품이 함께** 담긴다.

    라벨이 같으면 사용자 관점에선 같은 니즈다 — 원본 leg 인덱스로 갈라 라벨이 같은 목록 2개를
    내는 것(리뷰어 제안)은 R4-1(PR #318)이 결함으로 규정한 바로 그 출력이라 채택하지 않는다."""
    leaves = [
        ("카테고리A > leafA", "아웃도어용품"),  # unresolved leg 1
        (
            "카테고리B > leafB",
            "아웃도어용품",
        ),  # unresolved leg 2 — 다른 leg 기원, 같은 query 텍스트
        ("카테고리C > leafC", "캠핑용품"),  # unresolved leg 3
    ]
    leaf_order = [c for c, _ in leaves]

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[],
            unresolved=["카테고리A", "카테고리B", "카테고리C"],
            expansion_leaves=list(leaves),
        )

    async def _search(filters, exclude_product_ids=None):
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="아웃도어용품이랑 캠핑용품 다 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose(case=3)),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    lists = push.pushes[0].lists
    assert len(lists) == 2
    assert [entry.label for entry in lists] == ["아웃도어용품", "캠핑용품"]
    # leafA(100)·leafB(101) 가 같은 "아웃도어용품" 목록에 함께 담긴다 — 병합이 의도다.
    assert set(lists[0].product_ids) == {100, 101}
    assert set(lists[1].product_ids) == {102}


# ── PR #351 리뷰 R4-1 — T1(effective_cap)·T3(그룹핑) 판정 축 정합 ──────────────────────
#
# R3-1 이 T3 를 "distinct query 2개 이상이고 None 이 안 섞였을 때만 니즈 단위 그룹핑"으로
# 강화했는데, T1(검색 전 need_count 계산)은 그 None 예외를 반영하지 않아 두 축이 어긋났다 —
# None 이 섞여 T3 가 그룹핑을 포기(목록 1개)해도 T1 은 여전히 넓은 need_count 로 예산을
# 넓혀, 분할되지 않을 턴에 Spring 페이로드·rerank 입력만 낭비했다.


async def test_expanded_turn_with_none_query_does_not_widen_effective_cap() -> None:
    """[PR #351 R4-1 fail-first] 확장 턴의 query 가 {"A","B","C",None} 이면(None 혼재) T3 는
    그룹핑을 포기해 목록 1개로 나가는데, T1 은 **need_count=1**(→ effective_cap == merge_cap)
    로 떨어져야 한다 — 수정 전엔 need_count 를 distinct 값 그대로 4로 세어 effective_cap 이
    40 으로 넓혀졌고(분할되지 않을 턴에 불필요한 예산 확장), 이 테스트는 그 상태에서
    `c.limit == 40`으로 실패해야 한다."""
    leaves = [
        ("카테고리A > leafA", "A"),
        ("카테고리B > leafB", "B"),
        ("카테고리C > leafC", "C"),
        ("카테고리D > leafD", None),
    ]
    leaf_order = [c for c, _ in leaves]
    calls: list = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[],
            unresolved=["카테고리A", "카테고리B", "카테고리C", "카테고리D"],
            expansion_leaves=list(leaves),
        )

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="이것저것 다 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose(case=3)),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    assert len(calls) == 4
    assert all(c.limit == 30 for c in calls)  # merge_cap 그대로 — None 혼재로 넓히지 않는다
    assert len(push.pushes[0].lists) == 1  # T3 도 그룹핑을 포기해 목록 1개(R3-1 과 일관)


async def test_expanded_turn_all_real_queries_still_widens_effective_cap() -> None:
    """[PR #351 R4-1 회귀 고정] query 가 전부 실재({"A","B","C","D"}, None 없음)면 R4-1 수정
    이후에도 need_count=4 로 종전대로 예산이 넓혀지고(effective_cap=40) T3 그룹핑도 니즈
    4개로 정상 분할된다 — None 예외 처리가 all-real 경로를 건드리면 안 된다."""
    leaves = [
        ("카테고리A > leafA", "A"),
        ("카테고리B > leafB", "B"),
        ("카테고리C > leafC", "C"),
        ("카테고리D > leafD", "D"),
    ]
    leaf_order = [c for c, _ in leaves]
    calls: list = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[],
            unresolved=["카테고리A", "카테고리B", "카테고리C", "카테고리D"],
            expansion_leaves=list(leaves),
        )

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        idx = leaf_order.index(filters.category)
        return _res(100 + idx)

    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="이것저것 다 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose(case=3)),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    assert len(calls) == 4
    assert all(c.limit == 40 for c in calls)  # 4니즈 × 10 = 40 > merge_cap(30) 이라 넓어진다
    assert len(push.pushes[0].lists) == 4  # T3 도 None 없이 니즈 4개로 정상 분할된다


# ── R6-1 (PR #318 리뷰 3차) — 확장 턴은 filters.category 를 영속하지 않는다 ────────────
#
# `category_legs[0][0]` 은 확장 leaf 8개 중 임의의 하나일 뿐이다. 그걸 그대로
# `thread_store` 에 영속하면 (a) F-1 무필터 폴백이 걸린 턴은 실제로 안 쓰인 카테고리가 저장되고,
# (b) 폴백이 안 걸려도 다음 리파인 턴("더 저렴한 걸로")이 그 leaf 하나로 조용히 좁혀진다
# (`action=="carry"`) — 칩·고지에서 지킨 "표시=실제"(#51)가 멀티턴 영속 경로에서 깨졌었다.


async def test_expanded_turn_does_not_persist_representative_category() -> None:
    """[R6-1] 확장 턴 후 `thread_store` 에 저장된 category 는 None 이다(8개 중 임의의 leaf 아님)."""

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102)

    req = _req(message="화장품 추천해줘")
    identity = _member()
    await _collect(
        run_buyer_turn(
            req,
            identity,
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    stored = await (await get_thread_store()).get(await _thread_key(req, identity))
    assert stored is not None
    assert stored.category is None


async def test_non_expanded_turn_still_persists_representative_category() -> None:
    """[R6-1 회귀 고정] 확장이 **아닌** 일반 턴은 종전대로 `category_legs[0][0]` 이 저장된다."""

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102)

    req = _req()
    identity = _member()
    await _collect(
        run_buyer_turn(
            req,
            identity,
            llm=FakeLLM(),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_two_leg_mapper(),
        )
    )
    stored = await (await get_thread_store()).get(await _thread_key(req, identity))
    assert stored is not None
    assert stored.category == "여행/캠핑 > 여행용품"  # _two_leg_mapper 의 첫 leg(대표 canonical)


async def test_expanded_turn_search_still_uses_all_expansion_legs() -> None:
    """[R6-1] `filters.category` 를 비워도 이번 턴의 fan-out 검색 자체는 8개 leg 그대로 나간다 —
    fan-out(`_run_search`)은 `decision.category_legs` 로 돌고 `_leg` 가 leg 마다 `category` 를
    override 하므로 `base.category` 를 읽지 않는다(검색은 안 망가진다는 근거)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101, 102)

    await _collect(
        run_buyer_turn(
            _req(message="화장품 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=_broad_decompose()),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_broad_mapper(),
        )
    )
    assert set(calls) == {c for c, _ in _BROAD_LEAVES}


# ── R6-3 (PR #318 리뷰 3차) — needs_expansion 합집합이 `expansion_leaves` 를 버리지 않는다 ──
#
# 원 매핑이 D1(신호 자체 없음)로 `expansion_leaves` 가 비어 있고, #217 전개 아이템들이 전부
# 거리컷에 드롭돼 `expanded.expansion_leaves` 만 채워진 턴은, `replace(mapping, legs=..., ...)`
# 가 `expansion_leaves` 를 같이 넘기지 않으면 #222 폴백이 아예 발동하지 않는다 — 쓸 수 있는
# 후보가 있는데 조용히 버려진다.


async def test_expansion_leaves_survive_needs_expansion_union() -> None:
    """[R6-3] 원 매핑 expansion_leaves 비어 있음 + 전개 매핑 expansion_leaves 채워짐 → 폴백이
    발동해 category_legs 가 전개 쪽 확장 후보로 채워진다."""
    seen, expand = _expansion_probe()

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        # 원 발화("집들이 선물로 뭐 사갈까")는 D1(신호 없음) → 매핑 자체가 없어 expansion_leaves 도
        # 없다. 전개 아이템(디퓨저·식기 세트·핸드워시 세트)에만 매핑을 태워 전부 거리컷 드롭시키고
        # 그 앵커의 top-N 을 expansion_leaves 로 채운다.
        if any(q.query for q in category_queries):
            return CategoryMapping(
                legs=[],
                unresolved=[q.query for q in category_queries if q.query],
                expansion_leaves=[
                    (f"{q.query} 관련 카테고리 > 종류1", q.query)
                    for q in category_queries
                    if q.query
                ],
            )
        return CategoryMapping()  # 원 발화 — D1, expansion_leaves 없음

    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
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
                    "categoryQueries": [],  # D1(no_legs) — 신호 있는 leg 자체가 없다
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
            expand_needs=expand,
        )
    )
    assert seen == ["집들이 선물로 뭐 사갈까"]  # 전개가 발동했다
    # 전개 아이템(디퓨저·식기 세트·핸드워시 세트) 각각의 확장 후보가 검색까지 도달했다 —
    # replace() 가 expansion_leaves 를 버렸다면 calls 는 전부 None(무필터 degrade)이었을 것이다.
    assert calls  # 최소 하나는 확장 leaf 로 검색됐다
    assert any(c is not None for c in calls)


# ── R11-1 (PR #318 리뷰 6차) — zero-result 턴 구조화 로그 ──────────────────────────
#
# `if not candidates:` 분기는 곧장 `return` 해 `recommend_pipeline` 구조화 로그(하류)까지 못
# 간다 — 특히 "검색은 히트가 있었는데 최근구매·소모품 억제가 전량을 지운" 턴(F-1 폴백이 못
# 잡는 R10 갭, 이 PR 이 발생 확률을 높인다고 인정한 케이스)의 빈도를 잴 수단이 없었다.

import logging  # noqa: E402


def _member_num() -> Identity:
    """숫자 sub 회원(실제 JWT sub 는 숫자 BIGINT, §2.6) — 최근구매 조회 경로 검증용.

    위 #343 절도 이 정의를 공유한다(최근구매 dedup 경로가 실제로 도는 데 필요)."""
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


def _recent_purchases(*product_ids: int):
    async def _fn(user_id, status=None):
        return RecentPurchases(
            orders=[
                OrderHistory(
                    order_id=1,
                    ordered_at="2026-07-10T00:00:00",
                    items=[
                        OrderHistoryItem(order_item_id=i, product_id=pid)
                        for i, pid in enumerate(product_ids, 1)
                    ],
                )
            ]
        )

    return _fn


async def test_expanded_turn_zero_result_after_exact_exclusion_logs_had_candidates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[R11-1] 확장 턴 + 검색은 히트가 있었으나 최근구매 exact 제외가 전량을 지운 턴 →
    `recommend_zero_result` 가 `had_candidates=True, category_expanded=True` 로 남는다."""
    monkeypatch.setattr("app.agents.buyer.recommendation.graph._now", lambda: datetime(2026, 7, 19))
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _recent_purchases(101, 102))

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102)  # 확장 leg 8개 전부 같은 두 상품 히트 — 전량 최근구매와 겹친다

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(message="화장품 추천해줘"),
                _member_num(),
                llm=FakeLLM(decompose=_broad_decompose()),
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_broad_mapper(),
            )
        )
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"  # 전량 억제로 후보가 남지 않았다
    zero_result_logs = [r for r in caplog.records if _event(r, "recommend_zero_result")]
    assert zero_result_logs, "recommend_zero_result 로그가 없다"
    record = zero_result_logs[0]
    assert record.had_candidates is True  # 검색 자체는 히트가 있었다
    assert record.category_expanded is True  # #222 확장 턴 — R10 갭 빈도 관측 대상


async def test_search_itself_zero_result_logs_had_candidates_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[R11-1] 검색 자체가 0건(비-확장 턴, fan-out leg 도 0건)이면 `recommend_zero_result` 가
    `had_candidates=False` 로 남는다 — 억제가 아니라 애초에 매칭이 없던 턴과 구분한다."""

    async def _search(filters, exclude_product_ids=None):
        return _res()  # 명시 카테고리 leg 도 0건

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),  # DEFAULT_DECOMPOSE — 명시 매핑(확장 아님)
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_two_leg_mapper(),
            )
        )
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "zero_result"
    zero_result_logs = [r for r in caplog.records if _event(r, "recommend_zero_result")]
    assert zero_result_logs, "recommend_zero_result 로그가 없다"
    record = zero_result_logs[0]
    assert record.had_candidates is False
    assert record.category_expanded is False


# ── 이슈 #168 T1 — rerank 입력 예산을 니즈 수에 비례시킨다(effective_cap) ────────────────
#
# 실측(실 카탈로그 leaf 폭 9~17): merge_cap=30 은 case3 5니즈 턴에서 니즈당 6개로 자연
# 공급량보다 아래를 절단해 per-need expose_max(9) 도달이 원천 불가능했다. `effective_cap` 은
# case3 다중 leg 턴에만 `max(merge_cap, min(need_count, MAX_LISTS) * category_group_per_need_
# candidates)` 로 넓히고, 그 외 턴(비-case3·단일 leg·3니즈 이하)은 정확히 merge_cap(30) 그대로다.


def _n_leg_mapper(n: int):
    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return _mapping([(f"카테고리{i} > 세부{i}", f"니즈{i}") for i in range(n)])

    return _map


def _n_leg_case3_decompose(n: int) -> dict:
    return {
        "intent": "recommend",
        "reply": "",
        "case": 3,
        "semanticQuery": "다목적 쇼핑",
        "categoryQueries": [{"category": None, "query": f"니즈{i}"} for i in range(n)],
        "filters": {},
    }


async def test_case3_three_needs_effective_cap_stays_at_merge_cap() -> None:
    """[T1 회귀 0] case3 + 3니즈(경계, 3×10=30=merge_cap)는 effective_cap 이 정확히 30 이다 —
    이 축에 걸리는 턴이라도 니즈 수가 임계 이하면 기존과 동일해야 한다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        return _res(1)

    await _collect(
        run_buyer_turn(
            _req(message="세 가지 다 필요해"),
            _member(),
            llm=FakeLLM(decompose=_n_leg_case3_decompose(3)),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_n_leg_mapper(3),
        )
    )
    assert len(calls) == 3
    assert all(c.limit == 30 for c in calls)


async def test_case3_five_needs_effective_cap_widens_to_fifty() -> None:
    """[T1] case3 + 5니즈는 effective_cap 이 max(30, 5*10)=50 으로 넓어진다 — leg_limit(①)·
    merge cap(②)·embedding_rerank_limit 압축(③) 세 지점 모두 이 값을 쓴다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        return _res(*range(1, 3))  # leg 마다 후보 2개 — cap 자체 확인이 목적이라 폭은 작게

    await _collect(
        run_buyer_turn(
            _req(message="다섯 가지 다 필요해"),
            _member(),
            llm=FakeLLM(decompose=_n_leg_case3_decompose(5)),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_n_leg_mapper(5),
        )
    )
    assert len(calls) == 5
    assert all(c.limit == 50 for c in calls)


async def test_non_widened_turn_still_respects_lowered_embedding_rerank_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[PR #168 리뷰 R2-1] `embedding_rerank_limit` 을 merge_cap 미만(예: 20, rerank 토큰 절감
    튜닝)으로 낮춘 배포에서, effective_cap 이 안 넓어진 턴(3니즈 이하 case3)은 그 낮춘 값을
    그대로 존중해야 한다 — `rerank_input_limit = max(embedding_rerank_limit, effective_cap)` 을
    조건 없이 걸면 이 턴까지 30(merge_cap)으로 조용히 커진다(T1 이 의도한 "넓힌 턴만 하한을
    올린다"가 아니라 "이 슬라이스 자체의 하한을 올린다"가 돼버리는 설정 조합 회귀)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_rerank_limit", 20)

    async def _search(filters, exclude_product_ids=None):
        idx = int(filters.category.removeprefix("카테고리").split(" ", 1)[0])
        base = idx * 100
        return _res(*range(base + 1, base + 11))  # leg 당 10개 × 3leg = merge_cap(30) 정확히 채움

    with caplog.at_level(logging.INFO):
        await _collect(
            run_buyer_turn(
                _req(message="세 가지 다 필요해"),
                _member(),
                llm=FakeLLM(decompose=_n_leg_case3_decompose(3)),
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_n_leg_mapper(3),
            )
        )
    pipeline_logs = [r for r in caplog.records if _event(r, "recommend_pipeline")]
    assert pipeline_logs, "recommend_pipeline 로그가 없다"
    assert pipeline_logs[0].compressed == 20  # embedding_rerank_limit 이 그대로 존중된다(30 아님)


async def test_widened_split_turn_keeps_effective_cap_despite_lowered_embedding_rerank_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[PR #168 리뷰 R2-1 고정] `embedding_rerank_limit=20` 이어도 effective_cap 이 실제로
    넓어진 턴(5니즈 split, 50)은 그 넓힌 값을 유지한다 — R2-1 수정이 T1 의 원래 의도(넓힌 턴은
    안 잘림)까지 되돌리면 안 된다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_rerank_limit", 20)

    async def _search(filters, exclude_product_ids=None):
        idx = int(filters.category.removeprefix("카테고리").split(" ", 1)[0])
        base = idx * 100
        return _res(*range(base + 1, base + 13))  # leg 당 12개 — 실측 leaf 폭 하한대

    with caplog.at_level(logging.INFO):
        await _collect(
            run_buyer_turn(
                _req(message="다섯 가지 다 필요해"),
                _member(),
                llm=FakeLLM(decompose=_n_leg_case3_decompose(5)),
                search=_search,
                push_fn=_RecordingPush(),
                map_categories=_n_leg_mapper(5),
            )
        )
    pipeline_logs = [r for r in caplog.records if _event(r, "recommend_pipeline")]
    assert pipeline_logs, "recommend_pipeline 로그가 없다"
    assert pipeline_logs[0].compressed == 50  # effective_cap(5*10) 이 유지된다(20 아님)


async def test_non_case3_multi_leg_effective_cap_unaffected() -> None:
    """[T1 회귀 0] case3 이 아닌 멀티 leg(예: 종전 §6 fan-out) 턴은 니즈 수와 무관하게
    effective_cap 이 merge_cap(30) 그대로다 — 판정 축은 `case==3 and len(legs)>1` 뿐이다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        # leg 마다 서로 다른 productId 를 내야 병합 후보가 relaxation_min_results 이상 남는다 —
        # 전부 같은 id(1)를 내면 dedup 으로 후보가 1개가 되어 완화 칩 probe(§113)가 별도로
        # `_run_search` 를 다시 불러 호출 수가 이 테스트의 관심사(effective_cap)와 무관하게
        # 두 배로 뛴다.
        idx = int(filters.category.removeprefix("카테고리").split(" ", 1)[0])
        return _res(100 + idx)

    await _collect(
        run_buyer_turn(
            _req(),  # DEFAULT_DECOMPOSE — case 2
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_n_leg_mapper(5),
        )
    )
    assert len(calls) == 5
    assert all(c.limit == 30 for c in calls)


async def test_case3_five_needs_groups_fill_to_expose_max_with_ample_supply() -> None:
    """[T4-2] 5니즈 split 턴에서 니즈마다 공급이 충분하면(merge cap 50 안에서) 그룹이
    expose_max 까지 채워진다 — 니즈당 rerank 입력이 6개(구 merge_cap=30/5)로 절단되던 종전엔
    도달 불가능했다."""
    settings = get_settings()

    async def _search(filters, exclude_product_ids=None):
        idx = int(filters.category.removeprefix("카테고리").split(" ", 1)[0])
        base = idx * 100
        return _res(*range(base + 1, base + 13))  # leg 당 12개 — 실측 leaf 폭(9~17)의 하한대

    # rerank 는 니즈별 균형을 프롬프트로만 지시할 뿐 코드로 강제하지 않는다(`rerank.py` 의
    # `len(ranked) >= expose_max` 는 니즈 구분 없는 **전역** 컷이다) — 니즈 순서대로 몰아 랭킹을
    # 주면 전역 예산(expose_max*니즈수)이 마지막 니즈 도달 전에 바닥나 그 니즈만 굶는다. 라운드
    # 로빈(니즈마다 1개씩 순서대로)으로 줘야 각 니즈가 정확히 `expose_max` 개씩 받는다.
    ranked = [
        {"productId": i * 100 + (k + 1), "rationale": "그룹 채우기용"}
        for k in range(settings.expose_max)
        for i in range(5)
    ]
    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(message="다섯 가지 다 필요해"),
            _member(),
            llm=FakeLLM(
                decompose=_n_leg_case3_decompose(5),
                rerank={"ranked": ranked, "overallComment": "다 골라봤어요"},
            ),
            search=_search,
            push_fn=push,
            map_categories=_n_leg_mapper(5),
        )
    )
    lists = push.pushes[0].lists
    assert len(lists) == 5
    assert all(len(entry.product_ids) == settings.expose_max for entry in lists)


# ── 이슈 #168 T3 — BUY_ALL 은 buyAll=True 일 때만 니즈 단위로 발동한다 ─────────────────


async def test_expanded_turn_multi_query_buy_all_triggers_need_level_budget_sets() -> None:
    """[T3] distinct query 2개 확장 턴 + buyAll=True 는 leaf(4개) 단위가 아니라 니즈(2개)
    단위로 BUY_ALL 예산 세트를 만든다 — split_by_need 가 이제 True 이므로 buy_all_mode 도
    함께 열린다(단일 query 확장 턴은 여전히 막힌다,
    `test_expanded_case3_turn_does_not_trigger_buy_all_budget_sets` 참조). focus 라벨이 있다면
    "{니즈} 중심" 형태인데, 니즈 이름만 나올 뿐 leaf 4개의 canonical 은 등장하지 않는다 —
    leaf 단위로 새면 라벨이 "캠핑 > 텐트 중심" 같은 값이 돼 버린다."""
    leaves = [
        ("캠핑 > 텐트", "캠핑용품"),
        ("낚시 > 릴", "낚시용품"),
        ("캠핑 > 침낭", "캠핑용품"),
        ("낚시 > 낚싯대", "낚시용품"),
    ]
    leaf_order = [c for c, _ in leaves]

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[], unresolved=["캠핑용품", "낚시용품"], expansion_leaves=list(leaves)
        )

    async def _search(filters, exclude_product_ids=None):
        idx = leaf_order.index(filters.category)
        return ProductSearchResult(
            products=[
                SpringProduct(
                    product_id=100 + idx * 10 + j,
                    name=f"P{idx}{j}",
                    price=10_000 * (j + 1),
                    rating=4.0,
                    category=leaf_order[idx],
                    brand="b",
                )
                for j in range(2)
            ],
            total_count=2,
        )

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(message="캠핑용품이랑 낚시용품 예산 안에서 다 사줘"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "buyAll": True,
                    "totalBudget": 100_000,
                    "categoryQueries": [
                        {"category": None, "query": "캠핑용품"},
                        {"category": None, "query": "낚시용품"},
                    ],
                }
            ),
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )
    assert not any(e["type"] == "error" for e in events)
    assert push.pushes[0].list_type == "BUY_ALL"
    focus_labels = {e.label for e in push.pushes[0].lists if e.label and "중심" in e.label}
    assert focus_labels <= {"캠핑용품 중심", "낚시용품 중심"}
    for leaf_canonical, _query in leaves:
        assert not any(leaf_canonical in (e.label or "") for e in push.pushes[0].lists)


# ── 이슈 #168 T2 — split 턴 그룹 서술 token ────────────────────────────────────────


async def test_split_turn_emits_group_notice_with_need_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[T2] split 턴은 니즈 라벨과 노출 개수를 담은 그룹 서술 token 을 낸다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "current")
    # expose_min 을 1로 낮춘다 — 기본값이면 `_split_by_need` 가 각 니즈를 fallback(검색순서)으로
    # expose_min 까지 채워 그룹 개수가 랭킹 1건이 아니라 그 이상이 돼(가용 후보가 있는 한)
    # "1개"라는 이 테스트의 기대와 어긋난다(개수 자체는 T1/T2 관심사가 아니라 여기선 고정한다).
    monkeypatch.setattr(settings, "expose_min", 1)
    push = _RecordingPush()
    llm = _needs_llm(
        [
            {"productId": 101, "rationale": "수납이 좋아요"},
            {"productId": 201, "rationale": "220V 지원이에요"},
        ]
    )
    events = await _run_case3(llm, push)
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    expected = settings.group_notice.format(items="파우치 1개 · 어댑터 1개")
    assert expected in tokens


async def test_group_notice_disabled_flag_suppresses_token() -> None:
    """[T2 회귀] `group_notice_enabled=False` 면 그룹 서술 token 이 없다(다른 token 은 그대로)."""
    settings = get_settings()
    original = settings.group_notice_enabled
    settings.group_notice_enabled = False
    try:
        push = _RecordingPush()
        llm = _needs_llm(
            [
                {"productId": 101, "rationale": "수납이 좋아요"},
                {"productId": 201, "rationale": "220V 지원이에요"},
            ]
        )
        events = await _run_case3(llm, push)
    finally:
        settings.group_notice_enabled = original
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert not any("니즈별로 나눠 담았어요" in t for t in tokens)


async def test_non_split_turn_does_not_emit_group_notice() -> None:
    """[T2 회귀] split 되지 않는 턴(단일 leg)은 그룹 서술 token 이 없다."""

    async def _search(filters, exclude_product_ids=None):
        return _res(101, 102)

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_search,
            push_fn=push,
        )
    )
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert not any("니즈별로 나눠 담았어요" in t for t in tokens)


async def test_default_settings_combination_splits_and_groups_case3_turn() -> None:
    """[기본값 조합 시뮬레이션] `category_group_per_need_candidates`·`group_notice_*` 를
    아무것도 오버라이드하지 않은 기본값 조합에서도 T1(effective_cap)·T2(그룹 서술)가 함께
    정상 동작한다 — #222 테스트가 지킨 것과 같은 회귀 방지 관례(모든 테스트가 값을
    오버라이드하면 배포되는 기본값 조합이 깨져도 아무도 모른다)."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        idx = int(filters.category.removeprefix("카테고리").split(" ", 1)[0])
        return _res(*range(idx * 100 + 1, idx * 100 + 4))

    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(message="네 가지 다 필요해"),
            _member(),
            llm=FakeLLM(decompose=_n_leg_case3_decompose(4)),
            search=_search,
            push_fn=push,
            map_categories=_n_leg_mapper(4),
        )
    )
    assert all(c.limit == 40 for c in calls)  # 4니즈 × 10 = 40 > merge_cap(30) 이라 넓어진다
    assert len(push.pushes[0].lists) == 4
    tokens = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any("니즈별로 나눠 담았어요" in t for t in tokens)


# ── #428 전개 후 재매핑 배선 — sibling_expansion 플래그 ───────────────────────────
#
# 전개 아이템(#217)은 하나의 니즈에서 나온 형제라 매퍼의 대분류 합의 필터를 켤 수 있지만, 첫
# 매핑(원 발화, 서로 다른 니즈들일 수 있음)은 그 필터를 켜면 안 된다("캠핑용품이랑
# 낚시용품"처럼 대분류가 갈리는 것이 정상인 턴을 오염시킨다). 그래프가 두 호출에 각각 옳은
# 값을 넘기는지 배선을 고정한다.
#
# [#428 리뷰 5차 R5-1] 전개 재매핑 호출도 **무조건 True 가 아니다** — 원 발화가 이미 서로 다른
# 니즈를 2개 이상 명시했으면(`case=3` 은 다중 상품도 포함, `"이어폰이랑 노트북"`) 전개 산출도
# 그 니즈들에 걸쳐 섞일 수 있어(Claude PR Review, PR #444) 형제 전제가 깨진다 — 그때는 False 로
# 끈다. 아래 세 테스트가 니즈 개수(0·1·2)로 게이트가 갈리는 것을 함께 고정한다.


async def test_sibling_expansion_flag_wired_false_first_true_second() -> None:
    """[#428 배선 고정 / 리뷰 5차 R5-1] 원 발화 니즈 **0개**(`categoryQueries: []`) 턴에서,
    매퍼가 받은 `sibling_expansion` 이 첫 호출은 False, 전개 후 재매핑(두 번째) 호출은 True 다
    — 니즈가 없으면(=멀티 니즈가 아니면) R5-1 게이트를 통과해 필터가 켜진다.
    `test_sibling_expansion_flag_gated_off_when_two_needs_signaled`(니즈 2개 → 전개도 False)와
    짝을 이룬다 — 니즈 개수가 게이트를 가른다는 것을 두 테스트가 함께 고정한다."""
    seen: list[bool] = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **kw):
        seen.append(kw.get("sibling_expansion"))
        # 첫 호출(원 발화)은 신호가 없어 매핑 실패, 전개 호출은 그대로 canonical 로 흘려보낸다.
        if not category_queries:
            return CategoryMapping(legs=[], unresolved=[])
        return CategoryMapping(legs=[(q.query, q.query) for q in category_queries])

    async def _expand(utterance, **_):
        return ["디퓨저", "식기 세트"]

    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
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
                    "categoryQueries": [],
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
            expand_needs=_expand,
        )
    )
    assert seen == [False, True]  # 첫 호출 False / 전개 재매핑 True


async def test_sibling_expansion_flag_gated_off_when_two_needs_signaled() -> None:
    """[#428 리뷰 5차 R5-1 배선 고정] 원 발화가 서로 다른 니즈를 **2개** 명시했으면
    (`categoryQueries` 신호 leg 2개, `"이어폰이랑 노트북 추천해줘"`) 그 중 하나 이상이 매핑
    실패해 전개가 트리거되는 턴에서도, 매퍼가 받은 `sibling_expansion` 이 **첫 호출 False /
    전개 재매핑 호출도 False** 다 — 니즈가 2개면 전개 산출이 그 니즈들에 걸쳐 섞일 수 있어
    합의 필터를 끈다. `test_sibling_expansion_flag_wired_false_first_true_second`(니즈 0개 →
    전개 True)와 짝을 이룬다."""
    seen: list[bool] = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **kw):
        seen.append(kw.get("sibling_expansion"))
        if len(category_queries) == 2:
            # 첫 호출(원 발화) — 이어폰은 매핑되고 노트북은 실패해 전개를 트리거한다.
            return CategoryMapping(legs=[("가전 > 이어폰/헤드폰", "이어폰")], unresolved=["노트북"])
        return CategoryMapping(legs=[(q.query, q.query) for q in category_queries])

    async def _expand(utterance, **_):
        return ["디퓨저", "식기 세트"]

    async def _search(filters, exclude_product_ids=None):
        return _res(101)

    await _collect(
        run_buyer_turn(
            _req(message="이어폰이랑 노트북 추천해줘"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [
                        {"category": None, "query": "이어폰"},
                        {"category": None, "query": "노트북"},
                    ],
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_map,
            expand_needs=_expand,
        )
    )
    assert seen == [False, False]  # 니즈 2개 → 전개 재매핑도 False


async def test_sibling_expansion_flag_gated_on_when_one_need_signaled() -> None:
    """[#428 리뷰 5차 R5-1 배선 고정] 원 발화가 니즈 **1개**만 명시했으면(`categoryQueries` 신호
    leg 1개) 그 leg 이 매핑 실패해 전개가 트리거돼도 형제 전제가 성립하므로 합의 필터를 켠다 —
    전개 재매핑(두 번째) 호출의 `sibling_expansion` 이 True 다(단일 니즈는 R5-1 게이트를
    통과한다)."""
    seen: list[bool] = []

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **kw):
        seen.append(kw.get("sibling_expansion"))
        if len(category_queries) == 1:
            return CategoryMapping(legs=[], unresolved=["집들이 선물"])
        return CategoryMapping(legs=[(q.query, q.query) for q in category_queries])

    async def _expand(utterance, **_):
        return ["디퓨저", "식기 세트"]

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
            expand_needs=_expand,
        )
    )
    assert seen == [False, True]  # 니즈 1개 → 전개 재매핑은 True(형제 전제 성립)


# #428 로컬 pg-catalog 실측(2026-08-07, 사전 1,007행 / 임베딩 결측 0) — 4형제 전개의 top-8 히트.
# `test_category_mapping.py` 의 `_FRUIT_HITS` 와 같은 실측값이다(독립 테스트 모듈이라 복제 유지).
_FRUIT_HITS = {
    "바나나": [
        ("과일 > 수입과일", 0.2908),
        ("과일 > 국산과일", 0.3220),
        ("과일 > 냉동/간편과일", 0.3278),
        ("과자/간식 > 원물간식", 0.3287),
        ("과일 > 과일선물세트", 0.3307),
        ("꽃/원예 > 꽃/식물", 0.3332),
        ("유아동식/영양제 > 유아동 간식", 0.3367),
        ("과자/간식 > 빵/베이커리", 0.3467),
    ],
    "사과": [
        ("과일 > 국산과일", 0.2732),
        ("과일 > 수입과일", 0.2812),
        ("과일 > 과일선물세트", 0.2960),
        ("과일 > 냉동/간편과일", 0.3232),
        ("과자/간식 > 원물간식", 0.3249),
        ("꽃/원예 > 꽃/식물", 0.3323),
        ("커피/생수/음료 > 주스/과즙음료", 0.3348),
        ("건과/견과 > 견과류", 0.3433),
    ],
    "배": [
        ("여성가방 > 백팩", 0.3184),
        ("신생아의류 (0~24개월) > 배냇저고리", 0.3248),
        ("여성가방 > 스포츠가방", 0.3292),
        ("실버용품 > 환자용 배변용품", 0.3297),
        ("유아목욕/스킨케어 > 유아목욕용품", 0.3323),
        ("과일 > 국산과일", 0.3330),
        ("과자/간식 > 빵/베이커리", 0.3354),
        ("구기/라켓/스포츠 > 야구", 0.3358),
    ],
    "오렌지": [
        ("커피/생수/음료 > 주스/과즙음료", 0.3164),
        ("과일 > 수입과일", 0.3216),
        ("과일 > 과일선물세트", 0.3244),
        ("과일 > 국산과일", 0.3287),
        ("꽃/원예 > 꽃/식물", 0.3417),
        ("과자/간식 > 원물간식", 0.3419),
        ("가공식품 > 잼", 0.3455),
        ("과일 > 냉동/간편과일", 0.3461),
    ],
}


def _fruit_probe_mapper():
    """실물 `map_categories` 에 #428 실측 히트를 주입하는 파샬 — embed/search 만 fake, 나머지
    (거리컷·인터리브·합의 필터)는 실제 프로덕션 로직 그대로 태운다."""
    embedded: list[str] = []

    def _embed(texts: list[str]) -> list[list[float]]:
        embedded[:] = texts
        return [[float(i)] for i in range(len(texts))]

    def _search(vec, dsn, *, k):
        text = embedded[int(vec[0])]
        return _FRUIT_HITS.get(text, [])[:k]

    def _exact(values, dsn):
        return set()

    return functools.partial(
        _real_map_categories, embed=_embed, search_top_k=_search, exact_lookup=_exact
    )


async def test_fruit_recommend_turn_expands_to_fruit_only_legs_not_popular_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#428 이 이슈의 사용자 가시 회귀, 리뷰 1차 F-1 갱신] "나 아기 키우는데 과일 추천해줘"
    — decompose 가 D1(`categoryQueries: []`, `case=3`)을 내도 전개 후 재매핑이 **과일 계열로만**
    fan-out 된다.

    수정 전에는 "배"의 동음이의어(가방·배냇저고리·배변용품) 노이즈가 살아남아 8개 leg 중
    절반이 무관 카테고리였고, 그 노이즈가 fan-out 검색·rerank 입력을 그대로 먹어 인기 상품
    폴백·rerank 지연(운영 실측 8.80s)의 원인이 됐다(#428 이슈 코멘트 ③). 형제 합의 필터가
    "배"의 노이즈만 걸러 이제는 **과일 대분류만** 남는다(리뷰 1차 F-1 — 지지 집계를 top-1 로
    좁혀 "과자/간식" 같은 꼬리 순위 우연한 겹침이 승자가 되지 않는다).

    [#443] **주입이 켜진 프로덕션에서는 이 발화가 더 이상 이 경로에 도달하지 않는다.** 사전 기반
    보강이 파싱 시점에 `과일` leg 을 채워 `needs_expansion`(#217) 게이트가 열리지 않기 때문이고,
    그 우회가 바로 #443 이 비용으로 지목한 것이다(전개 LLM 1회 + fan-out N건 + 지연 14.52s).
    이 경로의 남은 정의역은 **카탈로그 사전에 없는 카테고리 발화**이므로, 여기서는 보강을 꺼서
    #428 이 지키려던 노이즈 필터링을 계속 재게 한다.
    """
    monkeypatch.setattr(get_settings(), "category_leg_injection_enabled", False)
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters.category)
        return _res(101)

    async def _expand(utterance, **_):
        return ["바나나", "사과", "배", "오렌지"]

    events = await _collect(
        run_buyer_turn(
            _req(message="나 아기 키우는데 과일 추천해줘"),
            _member(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [],
                }
            ),
            search=_search,
            push_fn=_RecordingPush(),
            map_categories=_fruit_probe_mapper(),
            expand_needs=_expand,
        )
    )
    assert calls  # fan-out 검색이 실제로 발동했다(인기 상품 폴백으로 새지 않음)
    mids = {c.split(" > ", 1)[0] for c in calls if c}
    # [F-3] 노이즈 부재 단언을 등호 단언보다 먼저 둔다 — 실패 시 어느 노이즈가 살아남았는지가
    # 먼저 드러나야 진단이 빠르다(아래 등호 단언에 이미 포함돼 항상 참이긴 하지만, 실패 원인을
    # 이름으로 남기는 것이 이 단언의 목적이다).
    assert not any(mid in mids for mid in ("여성가방", "신생아의류", "실버용품"))
    assert mids == {"과일"}
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] != "zero_result"
