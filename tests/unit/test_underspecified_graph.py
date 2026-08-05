"""과소지정 발화 — 그래프 통합 (이슈 #336).

`run_buyer_turn`(fake LLM/검색/push/popular_fn 전 주입, `docs/lessons.md` 2026-08-05 "popular_fn
미주입" 참조)을 직접 구동한다. 기본 설정(flag off)에서 현행과 동일함을 먼저 고정하고,
flag on 에서만 새 경로를 검증한다 — "테스트가 전부 flag on 으로 override" 하는 함정을 피한다.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services.spring_client import SpringUnavailableError
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM
from tests.unit.test_recommendation import (
    _collect,
    _counting_search_calls,
    _guest,
    _member,
    _recording_popular,
    _req,
    _RecordingPush,
    _thread_key,
    _types,
    run_buyer_turn,
)

# ─────────── decompose 픽스처 ───────────

_PRICE_MAX_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "categoryQueries": [],
    "filters": {"priceMax": 50000},
}

_BUDGET_BUY_ALL_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "buyAll": True,
    "totalBudget": 50000,
    "categoryQueries": [],
    "filters": {},
}

_BARE_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "categoryQueries": [],
    "filters": {},
}


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "underspecified_reask_enabled", True)
    return get_settings()


def _reask_tokens(events) -> list[str]:
    return [e["data"]["text"] for e in events if e["type"] == "token"]


def _assert_no_reask_leak(texts: list[str]) -> None:
    """되물음이 전혀 없다 — generic 문구뿐 아니라 **예시 템플릿의 판별 표지**도 검사한다(리뷰 F5).

    generic 부정만으로는 예시형 질문("무선이어폰 중에 찾으시는 게 있을까요?")이 새는 걸 못
    잡는다 — 그 문장은 `underspecified_reask_question` 을 포함하지 않는다. 예시 템플릿에서
    `{categories}` 자리표시자를 뺀 고정 부분을 판별 표지로 쓴다.
    """
    generic = get_settings().underspecified_reask_question
    example_marker = get_settings().underspecified_reask_question_examples.replace(
        "{categories}", ""
    )
    assert not any(generic in t or example_marker in t for t in texts)


# ─────────── 1) flag on + 가격 제약만 있는 턴 ───────────


async def test_price_constraint_only_turn_uses_popular_and_price_filters(flag_on) -> None:
    """ "5만원 이하로 아무거나"(buy-under-0003) — I-3 로 답하고 가격으로 클라이언트 필터한다."""
    products = [
        DEFAULT_PRODUCTS[0],  # 39000 — 예산 이하
        DEFAULT_PRODUCTS[1],  # 48000 — 예산 이하
        DEFAULT_PRODUCTS[2].model_copy(update={"price": 90000}),  # 90000 — 예산 초과
    ]
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=products)
    push = _RecordingPush()

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="us-price"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert search_calls == []  # I-1 은 나가지 않는다
    assert popular_calls  # I-3 를 불렀다
    assert len(push.pushes) == 1
    pushed_ids = {pid for entry in push.pushes[0].lists for pid in entry.product_ids}
    assert 103 not in pushed_ids  # 90000원 상품은 예산 초과라 제외됐다(값은 변경했지만 id 103 유지)

    types = _types(events)
    assert types.index("conditions") < types.index("products.ready") < types.index("done")
    # priceMax 칩이 conditions 에 실린다(가격 제약은 여전히 filters 에 남아 칩으로 파생된다).
    conditions_event = next(e for e in events if e["type"] == "conditions")
    chip_fields = {c["field"] for c in conditions_event["data"]["chips"]}
    assert "priceMax" in chip_fields

    reask_texts = _reask_tokens(events)
    assert any(get_settings().underspecified_reask_question in t for t in reask_texts) or any(
        "이어폰" in t for t in reask_texts
    )

    # [리뷰 F2] push 가 성공했으니 예시 되물음은 **products.ready 뒤**에 나가야 한다
    # (표시=실제 #51 — 노출되지도 않은 상품군을 먼저 가리키면 안 된다).
    reask_index = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "token"
        and (
            get_settings().underspecified_reask_question in e["data"]["text"]
            or "이어폰" in e["data"]["text"]
        )
    )
    assert reask_index > _types(events).index("products.ready")


async def test_price_constraint_only_turn_push_failure_asks_generic_after_notice(
    flag_on,
) -> None:
    """[리뷰 F2] push 실패 → 카드가 없으니 **예시 없이 generic 질문만**, `push_skipped_notice` 뒤."""
    from tests.unit.test_recommendation import _failing_push

    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="us-price-push-fail"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_failing_push,
            popular_fn=popular,
        )
    )

    assert popular_calls
    types = _types(events)
    assert "products.ready" not in types  # push 실패 — 카드 없음

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    reask_text = next(t for t in texts if get_settings().underspecified_reask_question in t)
    # generic 문구 정확히 그대로 — 예시 카테고리("이어폰" 등)가 섞여 있지 않다.
    assert reask_text == get_settings().underspecified_reask_question

    push_notice_index = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "token" and get_settings().push_skipped_notice in e["data"]["text"]
    )
    reask_index = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "token"
        and get_settings().underspecified_reask_question in e["data"]["text"]
    )
    assert reask_index > push_notice_index


async def test_bare_turn_profile_path_push_failure_asks_generic_after_notice(
    flag_on,
) -> None:
    """[리뷰 F2] profile 경로도 push 실패면 되물음이 `push_skipped_notice` 뒤로 온다."""
    from tests.unit.test_recommendation import _catalog_store, _failing_push, _inject_profile

    monkeypatch = pytest.MonkeyPatch()
    try:
        _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([201, 202]))
        search, _ = _counting_search_calls()
        popular, _ = _recording_popular()

        events = await _collect(
            run_buyer_turn(
                _req(message="아무거나 추천해줘", thread_id="us-profile-push-fail"),
                _member(),
                llm=FakeLLM(decompose=_BARE_DECOMPOSE),
                search=search,
                push_fn=_failing_push,
                popular_fn=popular,
            )
        )
    finally:
        monkeypatch.undo()

    assert "products.ready" not in _types(events)
    push_notice_index = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "token" and get_settings().push_skipped_notice in e["data"]["text"]
    )
    reask_index = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "token"
        and get_settings().underspecified_reask_question in e["data"]["text"]
    )
    assert reask_index > push_notice_index


# ─────────── 2) flag on + 0001 셀(총액 예산 + buy_all) ───────────


async def test_budget_buy_all_cell_keeps_162_filter_and_asks(flag_on) -> None:
    """buy-under-0001 — "5만원 이내로 아무거나 세트로": #162 예산 필터 + 되물음이 함께 나간다."""
    products = [
        DEFAULT_PRODUCTS[0],  # 39000
        DEFAULT_PRODUCTS[1],  # 48000
        DEFAULT_PRODUCTS[2].model_copy(update={"price": 90000}),
    ]
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=products)
    push = _RecordingPush()

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이내로 아무거나 세트로 추천해줘", thread_id="us-budget"),
            _guest(),
            llm=FakeLLM(decompose=_BUDGET_BUY_ALL_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert search_calls == []
    assert popular_calls
    pushed_ids = {pid for entry in push.pushes[0].lists for pid in entry.product_ids}
    assert 103 not in pushed_ids  # 예산 초과 상품 제외(#162 within_budget)

    texts = _reask_tokens(events)
    assert any(get_settings().underspecified_reask_question in t for t in texts) or any(
        "이어폰" in t for t in texts
    )


# ─────────── 3) flag on + 충분지정 턴 — 오발동 회귀 없음 ───────────


async def test_fully_specified_turn_never_reasks(flag_on) -> None:
    """카테고리·brand 가 있는 턴은 flag on 이어도 종전 경로 그대로 — 되묻지 않는다."""
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(thread_id="us-conditioned"),
            _guest(),
            llm=FakeLLM(),  # DEFAULT_DECOMPOSE — categoryQueries·priceMax·keyword 있음
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls == []
    assert search_calls  # 종전 검색 경로
    texts = _reask_tokens(events)
    assert not any(
        get_settings().underspecified_reask_question in t
        or get_settings().underspecified_notice in t
        for t in texts
    )


async def test_multiturn_never_reasks(flag_on) -> None:
    """멀티턴(2턴째)은 prior 가 있어 flag on 이어도 되묻지 않는다 — case 6 회귀."""
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="이어폰 추천해줘", thread_id="us-multiturn"),
            _member(),
            llm=FakeLLM(),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    search_calls.clear()
    popular_calls.clear()

    events = await _collect(
        run_buyer_turn(
            _req(message="그중에 5만원 이하", thread_id="us-multiturn"),
            _member(),
            llm=FakeLLM(),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls == []  # 2턴째도 조건이 있어 popular 로 새지 않는다
    texts = _reask_tokens(events)
    assert not any(get_settings().underspecified_reask_question in t for t in texts)


# ─────────── 4) flag off(기본값) — 현행과 동일 ───────────


async def test_flag_off_default_keeps_current_behavior_for_price_constraint_turn() -> None:
    """flag 를 건드리지 않은 **기본 설정 조합** — 가격 제약만 있는 턴도 종전 검색 경로 그대로.

    `underspecified_reask_enabled` 기본값이 False 라는 사실 자체를 검증한다(override 금지).
    """
    assert get_settings().underspecified_reask_enabled is False

    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="us-flag-off"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls == []  # popular 스왑이 일어나지 않는다
    assert search_calls  # 종전 I-1 그대로
    _assert_no_reask_leak(_reask_tokens(events))


async def test_flag_off_default_keeps_current_behavior_for_bare_turn() -> None:
    """flag off 에서 무조건 턴은 #162 no_condition 경로만 타고 되묻지 않는다."""
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="us-flag-off-bare"),
            _guest(),
            llm=FakeLLM(decompose=_BARE_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert search_calls == []
    assert popular_calls  # no_condition(#162) 경로는 여전히 동작한다
    _assert_no_reask_leak(_reask_tokens(events))


async def test_flag_off_price_max_zero_does_not_zero_out_popular_candidates() -> None:
    """[리뷰 F1] `filters.price_max=0` 인 턴 — flag off·`no_condition`(#162) 경로만으로도
    회귀가 재현됐던 셀. `0` 은 `_is_blank` 규약대로 미지정이라 인기 후보가 줄어들면 안 된다.
    """
    assert get_settings().underspecified_reask_enabled is False
    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "categoryQueries": [],
        "filters": {"priceMax": 0},
    }
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=DEFAULT_PRODUCTS)
    push = _RecordingPush()

    await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="us-price-zero"),
            _guest(),
            llm=FakeLLM(decompose=decompose),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert search_calls == []
    assert popular_calls  # no_condition 경로(#162) — flag 와 무관하게 항상 동작
    pushed_ids = {pid for entry in push.pushes[0].lists for pid in entry.product_ids}
    assert pushed_ids == {p.product_id for p in DEFAULT_PRODUCTS}  # 전량 유지 — 0 이 상한이 아니다


async def test_empty_reask_question_only_disables_the_question(
    flag_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[리뷰 F3] 되물음 문구가 빈 값이면 질문 token 만 꺼진다 — 후보 스왑·products.ready 는 그대로.

    기동 검증은 없다(config 주석 참조) — "빈 값 = 그 고지만 끄는 스위치" 관례를 핀 테스트로
    고정한다. `underspecified_reask_examples_max=0` 을 함께 고정해 generic-only 분기를
    강제한다(안 그러면 DEFAULT_PRODUCTS 의 category 로 예시 문구가 만들어져 "generic 이
    비었다"는 전제를 검증하지 못한다) — 이 셋업은 비어있는/채워진 두 실행에서 동일하다.
    """
    monkeypatch.setattr(get_settings(), "underspecified_reask_examples_max", 0)

    async def _run(thread_id: str) -> list[dict]:
        # 매번 새 thread_id — 같은 스레드를 재사용하면 1회차의 (빈) 저장 필터가 prior 로
        # 돌아와 2회차가 popular 경로를 타지 않는다(D6 실측, SPEC §5.2 와 동일 함정).
        search, _ = _counting_search_calls()
        popular, popular_calls = _recording_popular()
        push = _RecordingPush()
        events = await _collect(
            run_buyer_turn(
                _req(message="아무거나 추천해줘", thread_id=thread_id),
                _guest(),
                llm=FakeLLM(decompose=_BARE_DECOMPOSE),
                search=search,
                push_fn=push,
                popular_fn=popular,
            )
        )
        assert popular_calls  # 후보 소스 스왑은 그대로
        assert "products.ready" in _types(events)
        return events

    baseline_texts = _reask_tokens(
        await _run("us-empty-question-baseline")
    )  # 되물음 문구가 채워진 채 — generic 이 나간다
    assert any(get_settings().underspecified_reask_question in t for t in baseline_texts)

    monkeypatch.setattr(get_settings(), "underspecified_reask_question", "")
    empty_texts = _reask_tokens(await _run("us-empty-question-empty"))

    # 질문 token 만 사라진다 — 다른 token(#162 인기 고지·rerank comment)은 그대로다.
    assert len(empty_texts) == len(baseline_texts) - 1
    assert not any(t == "" for t in empty_texts)


# ─────────── 5) I-3 실패 → 검색 폴백 + 인기 고지 스킵 + 되물음은 나감 ───────────


async def test_popular_failure_still_asks_without_false_popular_claim(flag_on) -> None:
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(error=SpringUnavailableError("popular down"))

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="us-degrade"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls  # 시도는 했다
    assert search_calls  # 검색으로 degrade
    texts = _reask_tokens(events)
    assert not any(get_settings().underspecified_notice in t for t in texts)  # 인기 주장 스킵
    # 되물음은 나간다 — degrade 폴백(_run_search)의 DEFAULT_PRODUCTS 는 category 가 있어
    # 예시 질문("무선이어폰 중에 …")으로 나간다(generic 은 예시가 없을 때만).
    assert any(get_settings().underspecified_reask_question in t or "이어폰" in t for t in texts)


async def test_price_filter_exception_degrades_to_search_instead_of_killing_stream(
    flag_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[리뷰 R1] `within_price_range` 가 예외를 던져도 스트림은 done 으로 끝난다(§7).

    이 예외가 방어 밖(`asyncio.gather` 코루틴 안, try 밖)에 있으면 제너레이터가 done 도
    error 도 없이 그대로 죽는다 — `no_condition.rank_by_profile` [PR #311 리뷰] 와 같은
    실패 모양. I-3 자체 실패(`test_popular_failure_still_asks_without_false_popular_claim`)
    와 동일하게 검색 폴백 + 되물음으로 degrade 해야 한다.
    """
    from app.agents.buyer.recommendation import graph as recommendation_graph

    def _boom(*args, **kwargs):
        raise RuntimeError("price filter boom")

    monkeypatch.setattr(recommendation_graph, "within_price_range", _boom)

    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="us-filter-boom"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls  # I-3 는 시도했다(실패는 그 이후 필터링에서 났다)
    assert search_calls  # 무필터 검색으로 degrade 했다
    types = _types(events)
    assert types  # 제너레이터가 통째로 죽지 않았다 — 이벤트가 나왔다
    assert types[-1] == "done"  # error 가 아니라 정상 종료
    assert "error" not in types
    # 인기 주장은 스킵되지만(§7 정직성 규약과 동형), 되물음은 그대로 나간다.
    texts = _reask_tokens(events)
    assert not any(get_settings().underspecified_notice in t for t in texts)
    assert any(get_settings().underspecified_reask_question in t or "이어폰" in t for t in texts)


# ─────────── 6) popular 필터 후 0건 → zero-result + generic 되물음 ───────────


async def test_price_filter_zero_results_asks_generic_question(flag_on) -> None:
    """가격 필터로 전량이 걸러지면 0건 경로 + generic 되물음(카드 없는 답)."""
    expensive = [p.model_copy(update={"price": 90000}) for p in DEFAULT_PRODUCTS]
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=expensive)

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="us-zero"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls
    assert search_calls == []  # 폴백하지 않는다(§4.17 0건도 성공)
    types = _types(events)
    assert "products.ready" not in types
    assert types[-1] == "done"
    texts = _reask_tokens(events)
    assert get_settings().underspecified_reask_question in texts


# ─────────── 7) 되물음 다음 턴(prior 존재) → 무효화 ───────────


async def test_second_bare_turn_does_not_repeat_reask(flag_on) -> None:
    """D6 실측 — `ThreadFilterStore.put` 은 빈 필터도 저장한다(무조건 근거).

    1턴째는 무조건 턴이라 되묻는다. **2턴째도 같은 발화**를 반복해도, 1턴째 저장된
    (빈) `ProductSearchFilters` 가 prior 로 돌아와 `is_underspecified_turn` 의
    `prior is not None` 가드에 걸려 판정이 False 가 된다 — 되물음이 반복되지 않는다.
    """
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    identity = _member()
    request = _req(message="아무거나 추천해줘", thread_id="us-repeat")

    first_events = await _collect(
        run_buyer_turn(
            request,
            identity,
            llm=FakeLLM(decompose=_BARE_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    assert any(
        get_settings().underspecified_reask_question in t or "이어폰" in t
        for t in _reask_tokens(first_events)
    )

    key = await _thread_key(request, identity)
    from app.agents.buyer.graph import get_thread_store

    thread_store = await get_thread_store()
    stored = await thread_store.get(key)
    assert stored is not None  # ← D6 실측: 빈 필터도 저장되어 prior 가 None 으로 남지 않는다

    popular_calls.clear()
    second_events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="us-repeat"),
            identity,
            llm=FakeLLM(decompose=_BARE_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    second_texts = _reask_tokens(second_events)
    assert not any(get_settings().underspecified_reask_question in t for t in second_texts)
    assert not any("중에 찾으시는" in t for t in second_texts)  # 예시 되물음도 재발하지 않는다
