"""과소지정 발화 — 되물음 답변 턴 + 완화칩 상호작용 (이슈 #372, followup of #336/PR #364).

적대적 심사가 짚은 두 갭을 메운다: ① 되묻는 턴까지만 테스트되고 **답변 다음 턴**이 검증된 적이
없다. ② `SPEC-UNDERSPECIFIED-336.md` §7-4 "완화칩이 과소지정 턴에서 완전히 꺼진다"가 회귀
테스트도 우선순위 규칙 문서도 없는 "알려진 한계"로만 적혀 있다.

`test_underspecified_graph.py` 와 별개 파일로 둔 이유: 저 파일은 "flag on 에서 되묻는 턴 자체"에
집중하고, 이 파일은 "되묻는 턴 **다음**에 무슨 일이 벌어지는가"(멀티턴 답변 처리 + 완화칩 복원)를
다뤄 관심사가 다르다. 하네스(`run_buyer_turn`·fake 들)는 `test_recommendation.py` 에서 그대로
가져온다.
"""

from __future__ import annotations

import pytest

from app.agents.buyer.recommendation.relaxation import build_relaxation_candidates
from app.agents.buyer.recommendation.state import RouteDecision
from app.agents.buyer.recommendation.underspecified import is_underspecified_turn
from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM
from tests.unit.test_recommendation import (
    _collect,
    _counting_search_calls,
    _guest,
    _member,
    _recording_popular,
    _req,
    _RecordingPush,
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

# A-1 2턴째 — LLM 의 PRIOR_FILTERS 병합 산출을 모사한 fixture(실 LLM 호출 없음, CI 결정론 규약).
# 실제로는 decompose 프롬프트가 PRIOR_FILTERS.priceMax 를 보고 이 값을 병합해 내지만, FakeLLM 은
# 프롬프트 내용과 무관하게 고정 JSON 을 돌려주므로 "병합됐다고 가정한 산출물"을 손으로 채운다.
_CATEGORY_ANSWER_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "categoryQueries": [{"category": "무선이어폰", "query": "무선 이어폰"}],
    "filters": {"priceMax": 50000},
}

# A-2 — 카테고리 답이 아닌 잡담/감사 발화. RouteDecision.intent 실 스키마 값(app/agents/buyer/
# recommendation/state.py) 중 "general" 이 유일하게 맞는 값이다(decompose 는 reply 도 함께 낸다,
# app/agents/buyer/fallback/__init__.py 참조 — 별도 LLM 호출 없이 이 reply 를 그대로 스트리밍).
_UNRELATED_ANSWER_DECOMPOSE = {
    "intent": "general",
    "reply": "천만에요! 더 필요하신 게 있으면 말씀해 주세요.",
    "case": 2,
    "categoryQueries": [],
    "filters": {},
}

# A-3 — 사용자가 좁히기를 거부하고 "그냥 아무거나" 를 반복한 답변(2턴째). 1턴째와 발화는 같은
# 모양이지만 **의미가 다르다**: 1턴째는 "첫 요청이 원래 무조건"이고 2턴째는 "되물음에 대한 명시적
# 거부 응답"이다 — `test_second_bare_turn_does_not_repeat_reask`(test_underspecified_graph.py)의
# "같은 발화 반복" 재현과는 관측 목적이 다르므로 별도로 둔다(그 테스트는 반복 억제만 보고, 이
# 테스트는 거부 답변 턴이 실사용자에게 실제로 무엇을 주는지까지 고정한다).
_BARE_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "categoryQueries": [],
    "filters": {},
}

# [리뷰 R1-F5] 추천 레인 **안**에서 카테고리가 아닌 축(색상)만 답한 2턴 — intent 는 general 로
# 새지 않고 recommend 로 남는다. A-2(`_UNRELATED_ANSWER_DECOMPOSE`)는 추천 레인 밖(intent=general)
# 만 커버해 "되물음 맥락에서 카테고리 아닌 축만 답한 턴"의 위험 구간을 비켜 간다 — 이 fixture 가
# 그 구간을 메운다.
_COLOR_ONLY_ANSWER_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "categoryQueries": [],
    "filters": {"priceMax": 50000, "color": "블랙"},
}


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "underspecified_reask_enabled", True)
    return get_settings()


def _reask_tokens(events) -> list[str]:
    return [e["data"]["text"] for e in events if e["type"] == "token"]


def _assert_no_reask_leak(texts: list[str]) -> None:
    """되물음 미반복 — generic 문구뿐 아니라 예시 템플릿의 판별 표지도 검사한다(리뷰 F5 계승).

    `test_underspecified_graph.py::_assert_no_reask_leak` 와 동일 로직 — 별 파일이라 재정의한다.
    """
    generic = get_settings().underspecified_reask_question
    example_marker = get_settings().underspecified_reask_question_examples.replace(
        "{categories}", ""
    )
    assert not any(generic in t or example_marker in t for t in texts)


def _suggestion_chips(events) -> list[dict]:
    return [chip for e in events if e["type"] == "suggestions" for chip in e["data"]["chips"]]


def _relaxation_chips(events) -> list[dict]:
    return [c for c in _suggestion_chips(events) if c.get("relaxation")]


# ═══════════════ A. 되물음 답변 턴 — 같은 thread_id 2턴 구동 (#372 A) ═══════════════


async def test_category_answer_turn_uses_carried_price_and_search_path(flag_on) -> None:
    """A-1 정상 답변 — "5만원 이하로 아무거나"(되물음) → "이어폰"(카테고리 답) 이 정상 추천으로 이어진다.

    1턴은 buy-under-0003 과 같은 모양(가격 제약만)이라 되묻는다. 2턴은 그 되물음에 대한 카테고리
    답변으로, decompose fixture 는 **LLM 이 PRIOR_FILTERS.priceMax 를 병합해 낸 실제 산출을 모사**한다
    (SPEC §5 — "새 상태 없음, prior 병합은 decompose 프롬프트 경유"). 이 테스트는 그 배관 자체를
    실측한다 — `FakeLLM.calls` 로 2턴째 decompose 호출의 user 프롬프트에 1턴째 저장된 priceMax 가
    실제로 실렸는지 확인하고, 그 병합 산출(fixture)이 하류에서 I-1 검색 경로로 정상 소비되는지를
    나눠 검증한다(실 LLM 호출은 CI 결정론 규약 위반이라 여기서 하지 않는다).

    [리뷰 R1-F1] `assert search_calls`(호출됨)·`assert popular_calls == []`(I-3 미호출)만으로는
    "**해당 카테고리** 추천으로 이어진다"는 완료 조건을 검증하지 못한다 — A-3
    (`test_refusal_answer_turn_falls_back_to_unfiltered_search`)의 완전 무필터 검색도 똑같이
    `search_calls` 를 채우므로, 이 두 단언만으로는 "카테고리 추천"과 "무필터 검색"을 구별할 수
    없다(graph 가 `category_legs` 를 통째로 무시하는 회귀가 나도 이 단언들은 그대로 초록이다).
    그래서 실제 검색 호출의 필터 값(`search_calls[0].category`·`.price_max`)을 관측값 그대로
    직접 단언한다 — 아래 self-check 로 이 단언이 실제로 무언가를 지키는지 확인했다.

    **self-check(확인 후 원복)**: `_CATEGORY_ANSWER_DECOMPOSE["categoryQueries"]` 를 잠시 `[]`
    로 비우고 재실행하면 `search_calls[0].category` 가 `None` 이 되어(카테고리 신호가 없어져
    `filters.category` 가 채워지지 않는다) 아래 `assert search_calls[0].category == "무선이어폰"`
    이 실패한다 — 즉 이 단언은 실제로 카테고리 배관을 지킨다(2026-08-06 확인, `price_max` 단언은
    그 상태에서도 통과 — fixture 의 `filters.priceMax` 는 그대로라 두 단언이 서로 다른 것을 지킴을
    함께 확인했다).
    """
    identity = _member()
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    thread_id = "answer-turn-ok"

    first_events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    assert any(
        get_settings().underspecified_reask_question in t or "이어폰" in t
        for t in _reask_tokens(first_events)
    )  # 1턴은 되묻는다 — 이 셋업이 실제로 과소지정 턴인지 확인

    search_calls.clear()
    popular_calls.clear()
    llm2 = FakeLLM(decompose=_CATEGORY_ANSWER_DECOMPOSE)
    push2 = _RecordingPush()

    second_events = await _collect(
        run_buyer_turn(
            _req(message="이어폰", thread_id=thread_id),
            identity,
            llm=llm2,
            search=search,
            push_fn=push2,
            popular_fn=popular,
        )
    )

    # PRIOR_FILTERS 승계 배관 — 2턴째 decompose user 프롬프트에 1턴째 저장 priceMax(50000)가 실린다.
    fast_prompts = [user for tier, user in llm2.calls if tier == "fast"]
    assert fast_prompts, "2턴째 decompose(fast) 호출이 있어야 한다"
    assert '"priceMax": 50000' in fast_prompts[0]

    # I-1 검색 경로 — popular(I-3) 이 아니라 search(I-1) 로 해당 카테고리를 찾는다.
    assert search_calls  # I-1 호출됨
    assert popular_calls == []  # I-3 는 새지 않는다

    # [리뷰 R1-F1] "해당 카테고리 추천으로 이어진다"를 실제 검색 필터값으로 직접 고정한다 —
    # 호출 여부만으로는 A-3(완전 무필터)와 구별되지 않는다(위 self-check 참조).
    assert search_calls[0].category == "무선이어폰"  # 답변한 카테고리가 검색 필터에 실제로 실린다
    assert search_calls[0].price_max == 50000  # 승계된 priceMax 가 하류 검색까지 그대로 간다

    assert len(push2.pushes) == 1
    types = _types(second_events)
    assert "products.ready" in types
    assert types.index("products.ready") < types.index("done")

    _assert_no_reask_leak(_reask_tokens(second_events))  # 되물음 미반복


async def test_unrelated_answer_turn_completes_without_dying(flag_on) -> None:
    """A-2 무관 답변 — 되물음 다음 턴이 카테고리 답이 아닌 잡담/감사여도 턴이 죽지 않는다.

    decompose fixture 는 `RouteDecision.intent` 의 실 스키마 값(state.py Literal) 중 "general" 을
    쓴다 — decompose 가 산출하는 `reply` 를 별도 LLM 호출 없이 그대로 스트리밍한다
    (`app/agents/buyer/fallback/__init__.py::stream_fallback`).

    **실측한 경로**: intent=general 은 추천 서브그래프에 진입하지 않고 `stream_fallback` 산문 답변
    → `done`(finish_reason=stop) 으로 곧장 끝난다. search(I-1)·popular(I-3) 어느 쪽도 호출되지
    않는다 — 이 턴은 애초에 추천을 시도하지 않으므로 "카테고리 없이 되묻는다"는 이슈의 우려
    (완화칩 차단 등)와도 무관하다.
    """
    identity = _member()
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    thread_id = "answer-turn-unrelated"

    await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    search_calls.clear()
    popular_calls.clear()

    second_events = await _collect(
        run_buyer_turn(
            _req(message="고마워요", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_UNRELATED_ANSWER_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    types = _types(second_events)
    assert "error" not in types
    assert types[-1] == "done"
    assert search_calls == []  # 관찰: general 턴은 추천 서브그래프에 진입하지 않는다
    assert popular_calls == []
    texts = _reask_tokens(second_events)
    assert any("천만에요" in t for t in texts)  # fallback reply 가 그대로 나간다
    _assert_no_reask_leak(texts)


async def test_refusal_answer_turn_falls_back_to_popular_not_unfiltered_search(flag_on) -> None:
    """A-3 거부 답변 — "그냥 아무거나 줘"(좁히기 거부) 도 실사용자에게 실제로 뭔가는 준다.

    1턴은 되묻는다. 2턴은 사용자가 카테고리를 답하지 않고 무조건 발화를 반복해 **좁히기를
    명시적으로 거부**한 시나리오다(1턴째의 "첫 요청이 원래 무조건"과는 성격이 다르다).

    **종전 동결(#372) → #393 이 좁힌 경계**: 2턴째는 `prior is not None` 이라
    `is_underspecified_turn`·`is_no_condition_turn` 이 둘 다 그 자리에서 False 로 떨어진다
    (§2 조건 ②, 둘 다 첫 턴 한정). #372 는 그 결과 이 턴이 **무필터 I-1 검색**(파라미터 0개 =
    운영 실측 7.74초·12.3MB)으로 떨어지는 것을 "prior 유무로 갈리는 기존 멀티턴 경계"로 보고
    관찰로만 남겼다(#393 로 처리하라는 지시와 함께). #393 의 최소 필터 가드(`search_guard.
    is_unfiltered_payload`)는 **의도 판정이 아니라 payload 사실 판정**이라 no_condition/
    underspecified 와 달리 첫 턴에 한정되지 않는다 — 답이 "파라미터 0개"면 턴 번호와 무관하게
    12.3MB 응답을 막는 마지막 방어선이 발동해야 하기 때문이다(되묻기 다음 턴이 실사용에서 실제로
    밟는 경로라 오히려 더 중요하다). `search_filter_guard_enabled=False` 면 종전 동작(무필터
    I-1)이 그대로 재현된다 — 아래 롤백 테스트가 그 회귀를 지킨다.
    """
    identity = _member()
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    thread_id = "answer-turn-refusal"

    await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    search_calls.clear()
    popular_calls.clear()
    push2 = _RecordingPush()

    second_events = await _collect(
        run_buyer_turn(
            _req(message="그냥 아무거나 줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_BARE_DECOMPOSE),
            search=search,
            push_fn=push2,
            popular_fn=popular,
        )
    )

    assert search_calls == []  # 무필터 I-1 이 나가지 않는다 — #393 이 좁힌 경계의 핵심
    assert popular_calls == [get_settings().popular_candidate_size]  # I-3 로 갔다

    assert len(push2.pushes) == 1
    pushed_ids = {pid for entry in push2.pushes[0].lists for pid in entry.product_ids}
    assert pushed_ids == {p.product_id for p in DEFAULT_PRODUCTS}  # 인기 후보 집합과 일치

    types = _types(second_events)
    assert "error" not in types
    assert "products.ready" in types
    assert types[-1] == "done"
    texts = _reask_tokens(second_events)
    _assert_no_reask_leak(texts)  # 되물음 미반복
    # 사용자가 인기 상품을 자기 조건이 반영된 결과로 오해하지 않게 고지가 나간다.
    assert any(get_settings().no_condition_notice_popular in t for t in texts)


async def test_refusal_answer_turn_falls_back_to_unfiltered_search_when_guard_disabled(
    flag_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[#393 롤백] `search_filter_guard_enabled=False` 면 종전 동작(무필터 I-1)이 그대로
    재현된다 — 종전 동작이 회귀 검출 없이 사라지지 않게 남겨 둔다."""
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    identity = _member()
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    thread_id = "answer-turn-refusal-guard-off"

    await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    search_calls.clear()
    popular_calls.clear()
    push2 = _RecordingPush()

    second_events = await _collect(
        run_buyer_turn(
            _req(message="그냥 아무거나 줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_BARE_DECOMPOSE),
            search=search,
            push_fn=push2,
            popular_fn=popular,
        )
    )

    assert search_calls  # 무필터 I-1 로 떨어진다 — 필터가 전부 빈 채로 호출된다
    assert search_calls[0].category is None
    assert search_calls[0].price_max is None
    assert popular_calls == []  # I-3 폴백은 없다(prior 존재로 #162 경로도 막힘, 가드도 off)

    assert len(push2.pushes) == 1
    pushed_ids = {pid for entry in push2.pushes[0].lists for pid in entry.product_ids}
    assert pushed_ids == {p.product_id for p in DEFAULT_PRODUCTS}  # 실제로 상품을 받는다(전량)

    types = _types(second_events)
    assert "error" not in types
    assert "products.ready" in types
    assert types[-1] == "done"
    _assert_no_reask_leak(_reask_tokens(second_events))  # 되물음 미반복


async def test_recommend_lane_color_only_answer_carries_filters_downstream(flag_on) -> None:
    """A-5(리뷰 R1-F5) — 추천 레인 **안**에서 카테고리가 아닌 축(색상)만 답해도 죽지 않는다.

    A-2 는 `intent="general"` 이라 애초에 추천 서브그래프에 진입하지 않는 안전한 경로만
    커버한다. 이 테스트는 `intent="recommend"` 를 유지한 채 사용자가 카테고리 대신 색상만
    답한 턴("검정색이요")을 구동해, 추천 레인 **안**에서 카테고리 아닌 축만 답한 위험 구간을
    관측값으로 고정한다.

    **실측한 경로**: 2턴째도 A-3 과 같은 이유(`prior is not None`)로 `is_underspecified_turn`이
    False 라 되묻지 않고 I-1 검색으로 간다 — 다만 A-3(완전 무필터)과 달리 이번 턴에 답한
    `color`("블랙")와 승계된 `price_max`(50000)가 검색 필터에 실제로 실린다(`category` 는
    카테고리 신호가 없어 `None` 그대로). 죽지 않고 정상적으로 push 까지 이어진다 — "카테고리가
    아닌 축만 답해도 recommend 레인은 정상 동작한다"는 것을 관측으로 고정한다.
    """
    identity = _member()
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    thread_id = "answer-turn-color-only"

    await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    search_calls.clear()
    popular_calls.clear()
    push2 = _RecordingPush()

    second_events = await _collect(
        run_buyer_turn(
            _req(message="검정색이요", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_COLOR_ONLY_ANSWER_DECOMPOSE),
            search=search,
            push_fn=push2,
            popular_fn=popular,
        )
    )

    assert search_calls  # I-1 로 간다(prior 존재 — A-3 와 같은 경계)
    assert search_calls[0].category is None  # 카테고리 신호가 없었으니 None 그대로
    assert search_calls[0].color == "블랙"  # 답한 색상이 검색 필터에 실린다
    assert search_calls[0].price_max == 50000  # 승계된 priceMax 도 함께 간다
    assert popular_calls == []

    types = _types(second_events)
    assert "error" not in types
    assert "products.ready" in types
    assert types[-1] == "done"
    assert len(push2.pushes) == 1  # 죽지 않고 정상적으로 push 까지 이어진다
    _assert_no_reask_leak(_reask_tokens(second_events))  # 되물음 미반복


# ═══════════════ B. 완화칩 상호작용 회귀 — flag on (#372 B) ═══════════════


async def test_underspecified_turn_zero_result_blocks_relaxation_chip(flag_on) -> None:
    """B-1 차단 재현(핀 테스트) — 과소지정 턴 + 가격 필터로 전멸(0건) 은 완화칩을 내지 않는다.

    시나리오는 `test_underspecified_graph.py::test_price_filter_zero_results_asks_generic_
    question` 과 같은 셀(가격 필터 전멸)을 차용하되, 이 테스트는 **완화칩 부재**를 직접 단언한다.
    SPEC §7-4 승격 절(우선순위 규칙: reask > relaxation chips, 이 이슈 C-1)의 근거가 되는 핀이다 —
    과소지정 턴에서는 되물음이 완화칩 UX 를 대체하므로 `suggestions` 이벤트 자체가 나가지 않고
    대신 되물음 질문이 나간다.

    **자가 검증(D 공통)** — `graph.py` 의 완화칩 probe 게이트(`if not underspecified and (not
    candidates or len(candidates) < relaxation_min_results)`)에서 `not underspecified` 를 지우면
    이 테스트는 깨진다: 가격 필터 전멸 후에도 `build_relaxation_candidates` 는 filters.price_max
    로 priceMax 완화 후보를 만들고, `_probe` 가 그 후보로 재검색해(이 테스트의 `search` fake 는
    필터와 무관하게 `DEFAULT_PRODUCTS` 3건을 돌려준다) estCount=3 > 0 인 칩이 조립되어
    `suggestions` 이벤트가 나가 아래 `"suggestions" not in types` 단언이 실패한다.
    """
    expensive = [p.model_copy(update={"price": 90000}) for p in DEFAULT_PRODUCTS]
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=expensive)

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="relax-blocked-zero"),
            _guest(),
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls
    assert search_calls == []  # 0건도 성공이라 무필터 검색으로 폴백하지 않는다(§4.17)
    types = _types(events)
    assert "suggestions" not in types  # 완화칩(및 되돌리기 칩) 자체가 없다 — 이 이슈의 핀
    texts = _reask_tokens(events)
    assert get_settings().underspecified_reask_question in texts  # 대신 되물음이 나간다


async def test_non_underspecified_turn_relaxation_chip_unaffected_by_flag(flag_on) -> None:
    """B-2 비과소지정 턴 회귀 가드 — flag on 이어도 조건 있는 일반 턴의 완화칩은 그대로 나간다.

    `keyword`(what-축)가 있어 `is_underspecified_turn` 은 flag 값과 무관하게 False 다(§2.1 —
    what-축이 하나라도 있으면 과소지정이 아니다). 소량 결과(<`relaxation_min_results`=3)로
    완화칩 probe 조건을 만족시켜, flag on 의 `not underspecified` 게이트가 **과소지정이 아닌
    턴까지** 잘못 죽이지 않는지를 고정한다 — 새면 "flag on 이 일반 완화칩 UX 를 깬다"는 진짜
    회귀다(이슈 배경 갭 ②).
    """
    small = DEFAULT_PRODUCTS[:2]  # relaxation_min_results(3) 미만
    search, search_calls = _counting_search_calls(products=small)
    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "categoryQueries": [],
        "filters": {"priceMax": 50000, "keyword": "이어폰"},
    }

    events = await _collect(
        run_buyer_turn(
            _req(message="이어폰 5만원 이하로 추천해줘", thread_id="relax-unaffected"),
            _guest(),
            llm=FakeLLM(decompose=decompose),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=_recording_popular()[0],
        )
    )

    assert search_calls  # I-1 경로(과소지정이 아니므로 popular 로 새지 않는다)
    chips = _relaxation_chips(events)
    assert any(c["relaxation"]["field"] == "priceMax" for c in chips)  # 완화칩이 정상적으로 나간다
    texts = _reask_tokens(events)
    assert not any(
        get_settings().underspecified_reask_question in t
        or get_settings().underspecified_notice in t
        for t in texts
    )  # 과소지정 턴이 아니므로 되물음·인기 고지도 섞이지 않는다


async def test_answer_turn_restores_relaxation_chip_after_one_skipped_turn(flag_on) -> None:
    """B-3 답변 턴 완화칩 복원 — 되물음 다음 턴(카테고리 답변, 소량 결과)은 완화칩이 되살아난다.

    1턴(과소지정, 되물음)에서는 B-1 이 고정한 대로 완화칩이 안 나간다. 2턴은 카테고리를 답해
    `underspecified=False` 가 되고, 검색 결과도 소량(<3)이라 완화칩 probe 조건을 만족한다 — 되물음
    UX 가 "그 턴 한정"으로 칩 UX 를 대신하고, 다음 턴엔 한 턴만 쉬고 정상 복원됨을 고정한다
    (SPEC-UNDERSPECIFIED-336.md §7-4 승격 절, C-1 우선순위 규칙 참조).

    [리뷰 R1-F3] 1턴도 popular 후보를 **소량(<`relaxation_min_results`=3)** 으로 맞춘다 — 이전
    판(기본 `DEFAULT_PRODUCTS` 3건, 가격 필터 후에도 3건 그대로) 은 probe 조건
    (`not candidates or len(candidates) < relaxation_min_results`) 자체가 성립하지 않아
    "결과가 넉넉해서 칩이 없다"만 증명했지 "과소지정 게이트가 막아서 칩이 없다"는 증명하지
    못했다(1264행 게이트를 지워도 1턴 단언은 그대로 통과했다 — 공허 통과). popular 후보를 2건으로
    줄여 **게이트가 없었다면 확실히 칩이 났을 셋업**으로 바꾼다.

    **self-check(확인 후 원복)**: `graph.py` 1264행의 `not underspecified` 를 지우고 이 테스트만
    돌리면 1턴 `assert "suggestions" not in _types(first_events)` 가 실제로 깨진다(popular 2건이
    가격 필터를 통과해 그대로 남고, priceMax 완화 후보가 estCount>0 으로 조립돼 `suggestions`
    이벤트가 나간다 — 2026-08-06 확인).
    """
    identity = _member()
    popular, popular_calls = _recording_popular(products=DEFAULT_PRODUCTS[:2])
    small = DEFAULT_PRODUCTS[:2]
    search, search_calls = _counting_search_calls(products=small)
    thread_id = "relax-restored"

    first_events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_PRICE_MAX_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    assert "suggestions" not in _types(first_events)  # 1턴 — 소량 결과인데도 과소지정 턴이라 칩 없음

    search_calls.clear()
    popular_calls.clear()

    second_events = await _collect(
        run_buyer_turn(
            _req(message="이어폰", thread_id=thread_id),
            identity,
            llm=FakeLLM(decompose=_CATEGORY_ANSWER_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert search_calls  # 2턴은 I-1 경로
    chips = _relaxation_chips(second_events)
    assert any(c["relaxation"]["field"] == "priceMax" for c in chips)  # 완화칩 복원
    _assert_no_reask_leak(_reask_tokens(second_events))  # 되물음은 반복되지 않는다


def test_underspecified_turn_can_never_carry_an_auto_relaxable_field(flag_on) -> None:
    """B-4(리뷰 R1-F2) — `may_auto_relax`(약 527행)·자동완화 루프(약 1188행)의 `not underspecified`
    가 유효한 설정에서 관측 가능한 차이를 만들 수 없음을 구조적으로 고정한다.

    B-1(완화칩 probe 게이트, 약 1264행)은 `run_buyer_turn` 을 직접 구동해 행동 수준으로
    지킨다. 나머지 두 게이트는 그렇게 지킬 수 없다 — **`may_auto_relax`·자동완화 루프에서
    `not underspecified` 를 지워도, `build_relaxation_candidates(decision.filters, settings)`
    가 만드는 후보 중 `settings.relaxation_auto_fields`(`app/core/config.py` 의 기동 검증
    `_forbid_auto_relaxing_explicit_constraints` 가 `{"ratingMin"}` 부분집합으로 강제)와
    교집합이 나려면 `filters.rating_min` 이 채워져 있어야 하는데,
    `is_underspecified_turn` 은 `rating_min`(차단-축, §2.1)이 하나라도 있으면 그 자리에서
    False 를 반환한다** — 즉 "과소지정 턴이면서 자동완화 대상 필드를 가진" 조합은 이 두 불변식이
    겹쳐 **어떤 유효한 설정에서도 존재할 수 없다**. 자동완화 루프도 같은 `relax_candidates`(같은
    `build_relaxation_candidates` 호출)를 순회하므로 동일 논증이 적용된다.

    **직접 게이트를 지우고 실행해 확인**(2026-08-06, 확인 후 원복) — `graph.py` 527·1188행의
    `not underspecified` 를 각각 지우고 `test_underspecified_graph.py`(14건) +
    `test_underspecified_answer_turn.py`(당시 6건) 를 돌렸다: **20건 전부 그대로 통과** —
    기본 config 로 도달 가능한 어떤 시나리오도 이 두 게이트의 유무로 갈리지 않았다. 강제로
    `relaxation_auto_fields` 에 `"priceMax"` 를 넣으면(스크래치 실험, 커밋 안 함) 차이를
    관측할 수 있었지만, 그 설정 자체가 `_forbid_auto_relaxing_explicit_constraints`(REQ-REC-043·
    AC-REC-08 "가격 제약 불가침"의 하드 불변식)가 **기동 시점에 막는 상태**라 실서비스에서는
    절대 도달할 수 없다 — 그런 상태를 monkeypatch 로 억지로 만들어 통과하는 테스트는 "실제로
    지켜지는 동작"이 아니라 지어낸 관측이 된다(공통 규칙 위반).

    따라서 이 두 게이트는 **행동 회귀 테스트로 지킬 수 없는 결함**으로 보고하고(패치는 `app/**`
    금지 범위 밖), 대신 그 결론의 **근거가 되는 불변식**(자동완화 허용 목록 ⊆ {ratingMin} ∧
    ratingMin 은 항상 과소지정을 배제)을 이 테스트로 고정한다 — 미래에 `relaxation_auto_fields`
    허용 목록이 넓어지거나 `rating_min` 이 차단-축에서 빠지면(§2.2) 이 테스트가 먼저 깨져, 그
    시점부터 527·1188행의 `not underspecified` 가 실제로 관측 가능한 역할을 하게 됐음을 알린다
    — 그때는 이 테스트를 걷어내고 `run_buyer_turn` 기반 행동 테스트로 승격해야 한다.
    SPEC §7.2 정정(§7.2 참조)과 짝이다.
    """
    settings = get_settings()
    assert set(settings.relaxation_auto_fields) <= {"ratingMin"}  # 기동 검증이 강제하는 상한

    decision = RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(price_max=50000),
        semantic_query_is_fallback=True,  # is_underspecified_turn ④ 조건 충족
    )
    assert is_underspecified_turn(decision, None, settings)  # 이 턴은 실제로 과소지정이다

    candidates = build_relaxation_candidates(decision.filters, settings)
    assert [c.field for c in candidates] == ["priceMax"]  # rating_min 후보는 아예 없다
    # 두 게이트가 지키려는 "과소지정 턴 + 자동완화 대상 필드" 조합이 아예 존재하지 않는다.
    assert not any(c.field in settings.relaxation_auto_fields for c in candidates)
