"""구매자 conditionActions 계약·멀티턴 필터 제거 회귀 (#278, #442)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from app.agents.buyer.graph import (
    _remove_condition_actions,
    get_thread_store,
    run_buyer_turn as _production_run_buyer_turn,
)
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.recommendation.state import build_condition_chips
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.chat import BuyerChatRequest, CONDITION_FIELD_TO_FILTER, ConditionAction
from app.schemas.seller import SellerChatRequest
from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM


def _buyer_payload(**updates):
    payload = {"sessionId": "s1", "threadId": "t1", "message": "추천해줘"}
    payload.update(updates)
    return payload


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        request_id="condition-actions-test",
        context_id=context.context_id,
        record_model_call=lambda *_: None,
    )


async def _run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
        yield frame


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


class _RecordingSearch:
    def __init__(self) -> None:
        self.filters: list[ProductSearchFilters] = []

    async def __call__(self, filters, exclude_product_ids=None):  # noqa: ANN001
        self.filters.append(filters)
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )


async def _push_ok(push) -> bool:  # noqa: ANN001
    return True


class _RecordingPush:
    def __init__(self) -> None:
        self.pushes: list = []

    async def __call__(self, push) -> bool:  # noqa: ANN001
        self.pushes.append(push)
        return True


async def _collect(gen) -> list[dict]:  # noqa: ANN001
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


def _refine_llm() -> FakeLLM:
    return FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "categoryQueries": [],
            "filters": {"priceMax": 50000},
        }
    )


def test_condition_actions_parse_camel_case_and_default_empty() -> None:
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(conditionActions=[{"op": "remove", "field": "category"}])
    )
    assert [action.field for action in parsed.condition_actions] == ["category"]
    assert BuyerChatRequest.model_validate(_buyer_payload()).condition_actions == []


@pytest.mark.parametrize(
    ("action", "invalid_value"),
    [
        ({"op": "add", "field": "category"}, "add"),
        # color 는 검색 필터에는 있지만 conditions 칩 계약 6종에는 없다.
        ({"op": "remove", "field": "color"}, "color"),
    ],
)
def test_condition_actions_reject_unknown_contract_values(action, invalid_value) -> None:  # noqa: ANN001
    with pytest.raises(ValidationError, match=invalid_value):
        BuyerChatRequest.model_validate(_buyer_payload(conditionActions=[action]))


@pytest.mark.parametrize("message_payload", [{}, {"message": "   "}])
def test_condition_action_allows_missing_or_blank_message(message_payload) -> None:
    request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            **message_payload,
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )
    assert request.message.strip() == ""


@pytest.mark.parametrize("message_payload", [{}, {"message": "   "}])
def test_empty_message_and_empty_actions_are_rejected(message_payload) -> None:
    with pytest.raises(ValidationError):
        BuyerChatRequest.model_validate({"sessionId": "s1", "threadId": "t1", **message_payload})


def test_duplicate_fields_are_deduplicated_in_first_seen_order() -> None:
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            conditionActions=[
                {"op": "remove", "field": "priceMax"},
                {"op": "remove", "field": "category"},
                {"op": "remove", "field": "priceMax"},
            ]
        )
    )
    assert [action.field for action in request.condition_actions] == ["priceMax", "category"]


def test_buyer_field_name_guard() -> None:
    # graph 의 덕타이핑 getattr 이 리네임에 조용히 무력화되지 않게 필드명을 고정한다.
    assert "condition_actions" in BuyerChatRequest.model_fields


def test_seller_contract_does_not_accept_condition_actions() -> None:
    assert "condition_actions" not in SellerChatRequest.model_fields
    seller = SellerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "message": "상품 등록 도와줘",
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )
    assert not hasattr(seller, "condition_actions")
    assert "conditionActions" not in seller.model_dump(by_alias=True)


def test_condition_chip_fields_match_removable_action_fields() -> None:
    filters = ProductSearchFilters(
        category="거실가구 > 소파",
        price_max=50000,
        price_min=10000,
        brand=["브랜드"],
        rating_min=4.0,
        keyword="소파",
    )
    chip_fields = {chip.field for chip in build_condition_chips(filters)}
    assert chip_fields == set(CONDITION_FIELD_TO_FILTER)


def test_condition_action_literal_fields_match_filter_mapping() -> None:
    literal_fields = set(get_args(ConditionAction.model_fields["field"].annotation))
    assert literal_fields == set(CONDITION_FIELD_TO_FILTER)


async def test_remove_action_strips_only_target_from_search_and_conditions() -> None:
    identity = _member()
    first_request = BuyerChatRequest.model_validate(_buyer_payload())
    first_search = _RecordingSearch()
    await _collect(
        _run_buyer_turn(
            first_request,
            identity,
            llm=FakeLLM(),
            search=first_search,
            push_fn=_push_ok,
        )
    )

    second_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )
    second_search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            second_request,
            identity,
            llm=_refine_llm(),
            search=second_search,
            push_fn=_push_ok,
        )
    )

    assert second_search.filters[-1].category is None
    assert second_search.filters[-1].price_max == 50000
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    assert "category" not in {chip["field"] for chip in chips}
    assert "priceMax" in {chip["field"] for chip in chips}


async def test_action_only_removal_skips_decompose_and_preserves_mutated_prior(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """빈 발화의 칩 제거는 mutated prior 를 그대로 추천 경계로 사용한다."""
    identity = _member()
    first_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "semanticQuery": "고정 의미 검색어",
            "categoryQueries": [],
            "filters": {
                "priceMax": 50000,
                "priceMin": 10000,
                "brand": ["A", "B"],
                "ratingMin": 4.2,
                "keyword": "무선",
            },
        }
    )
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload()),
            identity,
            llm=first_llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )

    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [
                {"op": "remove", "field": "priceMax"},
                {"op": "remove", "field": "brand", "value": "B"},
            ],
        }
    )
    action_llm = FakeLLM(decompose_error=True)
    search = _RecordingSearch()
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=action_llm,
                search=search,
                push_fn=_push_ok,
            )
        )

    final_filters = search.filters[-1]
    assert final_filters.price_max is None
    assert final_filters.price_min == 10000
    assert final_filters.brand == ["A"]
    assert final_filters.rating_min == 4.2
    assert final_filters.keyword == "무선"
    assert final_filters.semantic_query == "고정 의미 검색어"
    assert [tier for tier, _ in action_llm.calls] == ["smart"]

    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["action_only"] is True
    assert extra["cleared_fields"] == ["priceMax"]
    assert extra["changed_fields"] == ["priceMax", "brand"]
    assert extra["category_legs_restored"] is False
    assert extra["no_op"] is False
    assert extra["unmatched_values"] == 0
    serialized = json.dumps(extra, ensure_ascii=False, default=str)
    assert "고정 의미 검색어" not in serialized
    assert '"B"' not in serialized


async def test_priorless_action_only_does_not_invent_search_or_call_llm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """prior 없는 액션-only 턴은 임의 검색 상태를 만들지 않고 종료한다."""
    identity = _member()
    request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t-fresh",
            "message": "   ",
            "conditionActions": [{"op": "remove", "field": "priceMax"}],
        }
    )
    action_llm = FakeLLM(decompose_error=True)
    search = _RecordingSearch()
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        events = await _collect(
            _run_buyer_turn(
                request,
                identity,
                llm=action_llm,
                search=search,
                push_fn=_push_ok,
            )
        )

    assert action_llm.calls == []
    assert search.filters == []
    assert events[-1]["type"] == "done"
    skipped = _graph_records(caplog, "condition_actions_skipped_no_prior")
    assert len(skipped) == 1
    assert skipped[0].__dict__["action_only"] is True


async def test_remove_action_is_idempotent_across_retries() -> None:
    identity = _member()
    first_request = BuyerChatRequest.model_validate(_buyer_payload())
    await _collect(
        _run_buyer_turn(
            first_request,
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )

    observed: list[dict] = []
    for _ in range(2):
        search = _RecordingSearch()
        events = await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=_refine_llm(),
                search=search,
                push_fn=_push_ok,
            )
        )
        observed.append(
            {
                "filters": search.filters[-1].model_dump(),
                "chips": next(event for event in events if event["type"] == "conditions")["data"][
                    "chips"
                ],
            }
        )

    assert observed[0] == observed[1]
    assert observed[0]["filters"]["category"] is None


async def test_remove_action_is_persisted_immediately() -> None:
    identity = _member()
    first_request = BuyerChatRequest.model_validate(_buyer_payload())
    await _collect(
        _run_buyer_turn(
            first_request,
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "message": "일반 질문",
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )
    general_llm = FakeLLM(
        decompose={"intent": "general", "reply": "도와드릴게요", "case": 2, "filters": {}}
    )
    await _collect(
        _run_buyer_turn(
            remove_request,
            identity,
            llm=general_llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )

    stored = await (await get_thread_store()).get(await _thread_key(remove_request, identity))
    assert stored is not None
    assert stored.category is None
    assert stored.price_max == 50000


async def test_existing_three_field_request_keeps_prior_and_event_sequence() -> None:
    identity = _member()
    first_request = SimpleNamespace(session_id="s1", thread_id="t1", message="추천해줘")
    await _collect(
        _run_buyer_turn(
            first_request,
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )

    legacy_request = SimpleNamespace(session_id="s1", thread_id="t1", message="그중에 골라줘")
    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            legacy_request,
            identity,
            llm=_refine_llm(),
            search=search,
            push_fn=_push_ok,
        )
    )

    assert search.filters[-1].category == "무선이어폰"
    assert search.filters[-1].price_max == 50000
    # progress 다회 emit(#396) — 리파인(carry) 턴은 카테고리를 승계해 매핑을 태우지 않으므로
    # mapping 은 없고, analyzing 뒤 searching·reranking·publishing 이 각 지점에서 낀다.
    assert [event["type"] for event in events] == [
        "progress",
        "conditions",
        "progress",
        "progress",
        "token",
        "progress",
        "products.ready",
        "done",
    ]
    progress_stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert progress_stages == ["analyzing", "searching", "reranking", "publishing"]


def _graph_records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "app.agents.buyer.graph" and record.getMessage() == event
    ]


async def test_condition_actions_applied_turn_logs_requested_and_cleared_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """적용 턴 — 요청 축 = 비워진 축, no-op=False, requestId 상관키가 실려 있음 (#442)."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload()),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=_refine_llm(),
                search=_RecordingSearch(),
                push_fn=_push_ok,
            )
        )

    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["requested_fields"] == ["category"]
    assert extra["cleared_fields"] == ["category"]
    assert extra["no_op"] is False
    assert extra["request_id"] == "condition-actions-test"
    # 값(가격 등)은 로그에 실리지 않는다 — 축 이름만 (#119 PII 규약).
    assert "50000" not in json.dumps(extra, ensure_ascii=False, default=str)


async def test_condition_actions_no_op_turn_logs_empty_cleared_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """no-op 턴 — prior 에 없던 축 제거 → no-op=True, 비워진 축 빈 목록 (#442)."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload()),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    # 첫 턴(FakeLLM 기본 decompose)은 brand 를 채우지 않는다 — 이미 비어 있는 축을 지운다.
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "brand"}],
        }
    )
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=_refine_llm(),
                search=_RecordingSearch(),
                push_fn=_push_ok,
            )
        )

    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["requested_fields"] == ["brand"]
    assert extra["cleared_fields"] == []
    assert extra["no_op"] is True


async def test_condition_actions_without_prior_logs_distinguishable_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """prior 없는 턴(첫 턴에 conditionActions) → 구분되는 로그 한 줄, 동작은 무시 그대로 (#442)."""
    identity = _member()
    first_turn_actions = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t-fresh",
            "conditionActions": [{"op": "remove", "field": "category"}],
        }
    )
    general_llm = FakeLLM(
        decompose={"intent": "general", "reply": "도와드릴게요", "case": 2, "filters": {}}
    )
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        await _collect(
            _run_buyer_turn(
                first_turn_actions,
                identity,
                llm=general_llm,
                search=_RecordingSearch(),
                push_fn=_push_ok,
            )
        )

    assert _graph_records(caplog, "condition_actions_applied") == []
    skipped = _graph_records(caplog, "condition_actions_skipped_no_prior")
    assert len(skipped) == 1
    extra = skipped[0].__dict__
    assert extra["requested_fields"] == ["category"]
    assert extra["request_id"] == "condition-actions-test"


# ─────────── 값 지정 제거 — 멀티 값 축(category·brand) 값당 제거 (이슈 #434) ───────────


def _action(field: str, value=None) -> ConditionAction:
    return ConditionAction.model_validate({"op": "remove", "field": field, "value": value})


def test_value_removal_strips_only_target_from_brand_list() -> None:
    """값 지정 제거 — brand=["A","B"] + {field:"brand", value:"B"} → 남은 값은 A 만(§3.1)."""
    prior = ProductSearchFilters(brand=["A", "B"], price_max=50000)
    result = _remove_condition_actions(prior, [_action("brand", "B")])
    assert result.brand == ["A"]
    assert result.price_max == 50000  # 지목하지 않은 축은 그대로


def test_value_removal_empties_brand_axis_to_none() -> None:
    """값 지정 제거로 브랜드가 비면 축이 None 이 된다(§3.1)."""
    prior = ProductSearchFilters(brand=["A", "B"])
    result = _remove_condition_actions(prior, [_action("brand", "A"), _action("brand", "B")])
    assert result.brand is None


def test_value_removal_category_matching_prior_clears_axis_when_no_stored_set() -> None:
    """[저장 집합 없음 → 강등] `_remove_condition_actions` 를 직접 호출(=chip_categories 저장
    집합을 모른다)하면 대표값 일치 판정만 한다 — category 가 승계 값과 일치하면 축 제거
    (정규화 비교, §3.1). 이슈 #434 라운드2에서 `run_buyer_turn` 호출부가 저장된 실제 검색
    집합을 읽을 수 있으면 이 결과를 남은 집합 기준으로 덮어쓴다(e2e 는
    test_value_scoped_category_removal_* 테스트 참조)."""
    prior = ProductSearchFilters(category="가전 > 이어폰")
    result = _remove_condition_actions(prior, [_action("category", "가전 > 이어폰")])
    assert result.category is None


def test_value_removal_category_mismatch_is_gracefully_ignored_when_no_stored_set() -> None:
    """[저장 집합 없음 → 강등] `_remove_condition_actions` 를 직접 호출(=chip_categories 저장
    집합을 모른다)하면 대표 카테고리와 불일치하는 값 지정 제거는 관대 무시(no-op)한다 —
    멀티 카테고리는 한 턴 fan-out leg 이고 승계 상태에는 대표 카테고리 1개만 남기 때문이다.
    호출부가 저장 집합을 읽을 수 있는 정상 스레드에서는 이 no-op 이 아니라 실제 남은 집합
    기준 제거가 일어난다(이슈 #434 라운드2, e2e 는 test_value_scoped_category_removal_*)."""
    prior = ProductSearchFilters(category="가전 > 이어폰")
    result = _remove_condition_actions(prior, [_action("category", "패션 > 의류")])
    assert result.category == "가전 > 이어폰"  # 지울 것이 없어 no-op


def test_value_removal_unknown_brand_value_is_gracefully_ignored() -> None:
    """미지 value(서버가 들고 있지 않은 값)는 관대 무시 — 리스트가 그대로 남는다."""
    prior = ProductSearchFilters(brand=["A", "B"])
    result = _remove_condition_actions(prior, [_action("brand", "Z")])
    assert result.brand == ["A", "B"]


@pytest.mark.parametrize("field_name", ["priceMax", "priceMin", "ratingMin", "keyword"])
def test_value_removal_ignores_value_for_single_value_axes(field_name) -> None:  # noqa: ANN001
    """단일 값 축 4종은 value 를 무시하고 축 전체를 제거한다(값 일치 요구는 무동작만 만든다)."""
    prior = ProductSearchFilters(price_max=50000, price_min=10000, rating_min=4.0, keyword="무선")
    attr = CONDITION_FIELD_TO_FILTER[field_name]
    # 저장된 값과 다른(불일치) value 를 보내도 무시하고 축을 지운다.
    result = _remove_condition_actions(prior, [_action(field_name, "전혀 다른 값")])
    assert getattr(result, attr) is None


def test_value_removal_accepts_numeric_value_and_clears_axis() -> None:
    """숫자 value(예: priceMax=50000)를 관대 수용(파싱 통과)하고 축을 제거한다."""
    prior = ProductSearchFilters(price_max=50000)
    result = _remove_condition_actions(prior, [_action("priceMax", 50000)])
    assert result.price_max is None


def test_value_removal_two_distinct_values_both_apply() -> None:
    """같은 축 서로 다른 값 2건 → dedup 이 하나로 접지 않고 둘 다 적용된다(§3.1)."""
    prior = ProductSearchFilters(brand=["A", "B", "C"])
    result = _remove_condition_actions(prior, [_action("brand", "A"), _action("brand", "C")])
    assert result.brand == ["B"]


def test_value_removal_mixed_full_and_value_actions_wins_full_removal() -> None:
    """같은 축에 value 없는 액션 + value 액션 혼재 → 축 전체 제거가 이긴다(전체 제거가 상위집합)."""
    prior = ProductSearchFilters(brand=["A", "B"])
    result = _remove_condition_actions(prior, [_action("brand", "A"), _action("brand", None)])
    assert result.brand is None


def test_condition_action_value_over_length_cap_is_rejected() -> None:
    """value 문자열 길이 상한 초과 → 400(ValidationError, §3.1)."""
    cap = get_settings().condition_action_value_max_chars
    with pytest.raises(ValidationError):
        BuyerChatRequest.model_validate(
            _buyer_payload(
                conditionActions=[{"op": "remove", "field": "brand", "value": "x" * (cap + 1)}]
            )
        )


def test_request_dedup_key_is_field_and_value_keeps_distinct_values() -> None:
    """[이슈 #434] BuyerChatRequest dedup 키는 (field, value) — 같은 축 다른 값은 둘 다 남는다."""
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            conditionActions=[
                {"op": "remove", "field": "brand", "value": "A"},
                {"op": "remove", "field": "brand", "value": "B"},
                {"op": "remove", "field": "brand", "value": "A"},  # 동일 (field,value) 중복
            ]
        )
    )
    values = [action.value for action in request.condition_actions]
    assert values == ["A", "B"]


def test_request_full_removal_absorbs_value_specific_actions_same_axis() -> None:
    """같은 축에 value 없는(전체 제거) 액션이 있으면 값 지정 액션은 흡수되고 전체 제거만 남는다."""
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            conditionActions=[
                {"op": "remove", "field": "brand", "value": "A"},
                {"op": "remove", "field": "brand"},  # value 없음 — 전체 제거
            ]
        )
    )
    assert len(request.condition_actions) == 1
    assert request.condition_actions[0].value is None
    assert request.condition_actions[0].field == "brand"


async def test_value_scoped_brand_removal_end_to_end_reflects_in_search_and_chips() -> None:
    """[#434 e2e] 값 지정 브랜드 제거가 실제 재검색 필터·conditions 칩에 반영된다."""
    identity = _member()
    brand_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "categoryQueries": [],
            "filters": {"brand": ["A", "B"]},
        }
    )
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="A 랑 B 브랜드 보여줘")),
            identity,
            llm=brand_llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "brand", "value": "B"}],
        }
    )
    # 값 지정 제거 턴의 decompose 는 prior 를 그대로 반영한 것으로 본다(실 LLM 의 병합 역할을
    # fake 가 대신함) — B 는 이미 서버가 지웠으므로 A 만 남는다.
    carry_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "categoryQueries": [],
            "filters": {"brand": ["A"]},
        }
    )
    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            remove_request,
            identity,
            llm=carry_llm,
            search=search,
            push_fn=_push_ok,
        )
    )
    assert search.filters[-1].brand == ["A"]
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    brand_chips = [chip for chip in chips if chip["field"] == "brand"]
    assert [chip["value"] for chip in brand_chips] == ["A"]


async def test_condition_actions_applied_logs_changed_fields_and_unmatched_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[#434] changed_fields·unmatched_values 가 로그에 실리고 no_op 은 changed_fields 기준이다."""
    identity = _member()
    brand_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "categoryQueries": [],
            "filters": {"brand": ["A", "B"]},
        }
    )
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="A 랑 B 브랜드 보여줘")),
            identity,
            llm=brand_llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    # 브랜드 부분 제거 — None 이 되지 않는 변경이라 cleared_fields 에는 안 잡히지만 changed_fields
    # 에는 잡혀야 하고, no_op 은 False 여야 한다(브랜드 부분 제거가 no_op 로 오판되면 안 된다).
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [
                {"op": "remove", "field": "brand", "value": "B"},
                {"op": "remove", "field": "category", "value": "미지값"},  # 관대 무시 대상
            ],
        }
    )
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=_refine_llm(),
                search=_RecordingSearch(),
                push_fn=_push_ok,
            )
        )

    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["cleared_fields"] == []  # 브랜드는 None 이 안 됨(부분 제거)
    assert "brand" in extra["changed_fields"]
    assert extra["no_op"] is False
    assert extra["unmatched_values"] == 1  # category 값 불일치 1건
    # 값 자체는 로그에 실리지 않는다(#119 PII).
    assert "미지값" not in json.dumps(extra, ensure_ascii=False, default=str)
    assert '"B"' not in json.dumps(extra, ensure_ascii=False, default=str)


# ─────────── 값 지정 category 제거 — 남은 집합 복원 (이슈 #434 라운드2) ───────────
#
# 라운드1은 category 값 지정 제거가 대표값 일치 판정만 해 A·B·C 중 B 를 지목하면 관대 무시
# (no-op)였다 — 카테고리 칩이 이슈 헤드라인인데 기능의 절반이 비어 있었다. 라운드2는 이
# 스레드가 **실제로 검색한** 카테고리 집합(chip_categories, ThreadFilterStore 신규 키)을
# 매 추천 턴마다 무조건 덮어써 두고, 값 지정 제거 턴에만 그 집합에서 지목 값을 뺀 나머지로
# 재검색한다(수용 기준: "value 를 실으면 그 값만 제거된 필터로 재검색된다"). 일반 승계(carry)
# 턴은 이 신호가 없으므로 절대 안 바뀐다.

_LEG_A = "여행/캠핑 > 여행용품"
_LEG_B = "가전 > 어댑터"
_LEG_C = "패션 > 의류"


def _three_leg_mapper():
    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(legs=[(_LEG_A, "파우치"), (_LEG_B, "어댑터"), (_LEG_C, "의류")])

    return _map


async def test_value_scoped_category_removal_searches_remaining_legs_and_conditions_chips() -> None:
    """[B-4 핵심] 카테고리 3개(A·B·C) fan-out 턴 → 다음 턴 값 지정 제거(value=B) → **검색 호출
    인자**가 정확히 A·C 두 leg 이고, 그 턴의 conditions 칩도 A·C 2개다(칩만 보지 말고 실제
    검색 인자를 본다)."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": _LEG_B}],
        }
    )
    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            remove_request,
            identity,
            llm=_refine_llm(),
            search=search,
            push_fn=_push_ok,
        )
    )
    assert [filters.category for filters in search.filters] == [_LEG_A, _LEG_C]
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    cat_chips = [chip for chip in chips if chip["field"] == "category"]
    assert [chip["value"] for chip in cat_chips] == [_LEG_A, _LEG_C]


async def test_action_only_value_scoped_category_removal_restores_remaining_legs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """액션-only 카테고리 value 제거도 남은 멀티 leg 을 LLM 없이 복원한다."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": _LEG_B}],
        }
    )
    action_llm = FakeLLM(decompose_error=True)
    search = _RecordingSearch()
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        events = await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=action_llm,
                search=search,
                push_fn=_push_ok,
                map_categories=_three_leg_mapper(),
            )
        )

    assert [filters.category for filters in search.filters] == [_LEG_A, _LEG_C]
    assert [tier for tier, _ in action_llm.calls] == ["smart"]
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    assert [chip["value"] for chip in chips if chip["field"] == "category"] == [_LEG_A, _LEG_C]
    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["action_only"] is True
    assert extra["cleared_fields"] == []
    assert extra["changed_fields"] == []
    assert extra["category_legs_restored"] is True
    assert extra["no_op"] is False
    assert extra["unmatched_values"] == 0
    serialized = json.dumps(extra, ensure_ascii=False, default=str)
    assert _LEG_A not in serialized
    assert _LEG_B not in serialized
    assert _LEG_C not in serialized


async def test_restored_category_removal_logs_no_op_false_and_restoration_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[이슈 #434 라운드3, #442 재발형] 대표(A)가 안 바뀌는 값 지정 제거(A·B·C 중 비대표 B 를
    지목)는 `before.category == after.category` 라 `cleared_fields`·`changed_fields` 만으로는
    `no_op: true`로 찍힌다 — 그러나 이 턴은 실제로 leg 을 A·C 로 복원해 재검색했다. 로그가
    그 복원 사실을 실어야 하고, `no_op` 은 `false`여야 한다. 값(A·B·C 문자열)은 로그에 없어야
    한다(#119 PII)."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": _LEG_B}],
        }
    )
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=_refine_llm(),
                search=_RecordingSearch(),
                push_fn=_push_ok,
            )
        )

    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    # 대표가 안 바뀌었으니 기존 두 필드는 여전히 빈 채로다(기존 의미·이름 불변).
    assert extra["cleared_fields"] == []
    assert extra["changed_fields"] == []
    assert extra["category_legs_restored"] is True  # 실제로 leg 이 복원된 사실
    assert extra["no_op"] is False  # 복원을 반영해 무동작과 구분된다
    # 값 자체(A·B·C)는 로그에 실리지 않는다(#119 PII 규약).
    serialized = json.dumps(extra, ensure_ascii=False, default=str)
    assert _LEG_A not in serialized
    assert _LEG_B not in serialized
    assert _LEG_C not in serialized


async def test_unmatched_category_value_does_not_trigger_multi_leg_fanout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[이슈 #434 라운드4, PR #566 Claude 리뷰] 저장 집합이 다건(A·B·C)이어도 지목한 value 가
    **어느 것과도 매칭되지 않으면** 관대 무시(no-op)여야 한다 — 대표 1개만 검색하던 일반 carry
    턴이 저장 집합 전체로 멀티 leg fan-out 하면 안 된다(이 PR 이 명시적으로 기각한 "일반 승계의
    멀티 leg 화"가 미매칭 경로로 새어 들어오는 결함).

    옛 게이트(`if remaining:`)는 "남은 게 있는가"만 봐서, 교집합이 전혀 없어
    `remaining == stored_categories`(변화 0)여도 비어 있지 않다는 이유로 통과했다."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": "무관한 값"}],
        }
    )
    search = _RecordingSearch()
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.graph"):
        events = await _collect(
            _run_buyer_turn(
                remove_request,
                identity,
                llm=_refine_llm(),
                search=search,
                push_fn=_push_ok,
            )
        )

    # 1. 다음 검색이 단일 leg(대표 카테고리 1개)다.
    assert [filters.category for filters in search.filters] == [_LEG_A]
    # 2. conditions 카테고리 칩이 1개다(여러 개로 재구성되지 않는다).
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    cat_chips = [chip for chip in chips if chip["field"] == "category"]
    assert [chip["value"] for chip in cat_chips] == [_LEG_A]
    # 3. 이 턴은 저장 집합을 [A, B, C] → 뭔가 지워진 것처럼 훼손하지 않는다 — 값 지정 제거
    # 블록 자체는 아무것도 다시 쓰지 않는다(길이 비교 게이트가 조기 반환한다, 이번 수정의
    # 핵심). 다만 이 턴은 검색을 대표 1개(A)로만 했으므로, **라운드2의 별개·불변 메커니즘**
    # ("매 턴 무조건 덮어쓴다" — `_prepare_recommendation`, 이번 수정 범위 밖)이 턴 끝에
    # 저장 집합을 "이번 턴이 실제로 검색한 것"(=A 하나)으로 다시 정규화한다. 이것은 결함이
    # 아니라 그 메커니즘이 원래 하는 일이다(오래된 값이 부활하지 않게) — B·C 는 사라진 게
    # 아니라 다음에 그 카테고리를 다시 fan-out 해야 값 지정 제거 대상으로 복귀한다.
    thread_key = await _thread_key(remove_request, identity)
    stored = await (await get_thread_store()).get_chip_categories(thread_key)
    assert stored == [_LEG_A]
    # 4. 로그 — 실제 결과 기준(예측식 아님): 복원 안 됨, no_op, 미매칭 1건.
    records = _graph_records(caplog, "condition_actions_applied")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["category_legs_restored"] is False
    assert extra["no_op"] is True
    assert extra["unmatched_values"] == 1


async def test_value_scoped_category_removal_to_empty_clears_axis() -> None:
    """마지막 값까지 제거 → `filters.category is None`(축 제거, 종전 degrade 동작 불변)."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [
                {"op": "remove", "field": "category", "value": _LEG_A},
                {"op": "remove", "field": "category", "value": _LEG_B},
                {"op": "remove", "field": "category", "value": _LEG_C},
            ],
        }
    )
    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            remove_request,
            identity,
            llm=_refine_llm(),
            search=search,
            push_fn=_push_ok,
        )
    )
    assert search.filters[-1].category is None
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    assert not any(chip["field"] == "category" for chip in chips)


async def test_normal_refine_turn_after_fanout_still_single_leg() -> None:
    """[B-1 회귀 방지선] 카테고리 3개 턴 뒤 **제거가 아닌** 일반 리파인("더 저렴한 걸로")은
    오늘처럼 단일 leg(대표 카테고리)로 검색한다 — 일반 승계 동작은 이 개정에서 바뀌지 않는다."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    refine_request = BuyerChatRequest.model_validate(_buyer_payload(message="더 저렴한 걸로"))
    search = _RecordingSearch()
    await _collect(
        _run_buyer_turn(
            refine_request,
            identity,
            llm=_refine_llm(),
            search=search,
            push_fn=_push_ok,
        )
    )
    assert [filters.category for filters in search.filters] == [_LEG_A]  # 대표 1개, 오늘과 동일


async def test_value_scoped_category_removal_degrades_when_stored_set_missing() -> None:
    """저장 집합 없음(스레드 상태 유실 시뮬레이션 — filters 만 직접 심고 chip_categories 는
    쓰지 않음) → 강등 경로: 대표와 일치하면 제거, 불일치면 관대 무시. 400 없음."""
    identity = _member()
    request = BuyerChatRequest.model_validate(_buyer_payload())
    thread_key = await _thread_key(request, identity)
    # chip_categories 를 쓰지 않고 filters 만 직접 심어 "구 스레드"(#434 이전에 저장된 필터)를
    # 흉내낸다 — get_chip_categories 는 None 을 돌려줘야 한다.
    await (await get_thread_store()).put(thread_key, ProductSearchFilters(category=_LEG_A))
    assert (await get_thread_store()).get_chip_categories is not None  # 메서드 존재 확인
    stored = await (await get_thread_store()).get_chip_categories(thread_key)
    assert stored is None  # 강등 전제 확인

    # 불일치 값 — 관대 무시(no-op), 400 없음.
    mismatch_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": _LEG_B}],
        }
    )
    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            mismatch_request, identity, llm=_refine_llm(), search=search, push_fn=_push_ok
        )
    )
    assert "error" not in [event["type"] for event in events]
    assert search.filters[-1].category == _LEG_A  # 강등: 대표값 유지(관대 무시)

    # 대표와 일치하는 값 — 강등 경로에서도 제거된다.
    match_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": _LEG_A}],
        }
    )
    search2 = _RecordingSearch()
    events2 = await _collect(
        _run_buyer_turn(
            match_request, identity, llm=_refine_llm(), search=search2, push_fn=_push_ok
        )
    )
    assert "error" not in [event["type"] for event in events2]
    assert search2.filters[-1].category is None


async def test_replace_turn_overwrites_stored_categories_no_revival() -> None:
    """`replace`(새 카테고리 지목) 턴이 저장 집합을 덮어쓴다 → 그 다음 값 지정 제거 턴이 그
    새 카테고리 기준으로만 판정한다(죽은 카테고리가 되살아나지 않는다).

    첫 턴을 **확장 턴**(chip_categories=`[]`, 이 값은 확장 여부와 무관하게 항상 쓰인다)으로
    잡는다 — 그래야 "확장 턴일 때만 지운다"로 변이해도 첫 기록 자체는 살아남아, 그 다음
    `replace` 턴이 **실제로 덮어썼는지**만 이 테스트가 가려낸다(§B-5 변이 2). 일반 fan-out
    턴으로 시작하면 그 변이가 첫 기록 자체를 막아 이 테스트가 아니라 B-4-1 이 먼저 깨져
    무엇을 검증하는지 흐려진다.
    """
    identity = _member()

    async def _expand_mapper(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[], unresolved=["가전"], expansion_leaves=[("가전 > 잡화", "잡화")]
        )

    expand_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "categoryQueries": [{"category": "가전", "query": "가전"}],
            "filters": {},
        }
    )
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="가전 아이템 아무거나")),
            identity,
            llm=expand_llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_expand_mapper,
        )
    )

    async def _single_leg_mapper(
        *, category_queries, utterance, settings, llm=None, tier="fast", **_
    ):
        return CategoryMapping(legs=[("가전 > 노트북", "노트북")])

    replace_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "categoryQueries": [{"category": "가전 > 노트북", "query": "노트북"}],
            "filters": {},
        }
    )
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="노트북 볼래")),
            identity,
            llm=replace_llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_single_leg_mapper,
        )
    )

    # 값이 "노트북"과 불일치하는 값 지정 제거 — 저장 집합이 올바르게 ["가전 > 노트북"]로
    # 덮어써졌으면 관대 무시(그대로 유지)여야 한다. 덮어쓰기가 빠졌으면(변이) 저장 집합이 여전히
    # `[]`(확장 턴 것) 라 "남은 집합이 비어 있다" 경로를 타 카테고리가 통째로 None 이 된다 —
    # 두 결과가 뚜렷이 갈리므로 이 변이에 민감하다.
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": "무관한 값"}],
        }
    )
    search = _RecordingSearch()
    await _collect(
        _run_buyer_turn(
            remove_request, identity, llm=_refine_llm(), search=search, push_fn=_push_ok
        )
    )
    assert search.filters[-1].category == "가전 > 노트북"  # 덮어쓰기가 됐어야 살아남는다


async def test_expansion_turn_stores_empty_chip_categories() -> None:
    """확장 턴(#222) → 저장 집합이 `[]` 이고, 다음 승계 턴이 leaf 8개로 fan-out 하지 않는다."""
    identity = _member()

    async def _expand_mapper(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        return CategoryMapping(
            legs=[], unresolved=["패션"], expansion_leaves=[("패션 > 상의", "상의")]
        )

    expand_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "categoryQueries": [{"category": "패션", "query": "패션"}],
            "filters": {},
        }
    )
    async for _ in _run_buyer_turn(
        BuyerChatRequest.model_validate(_buyer_payload(message="패션 아이템 추천해줘")),
        identity,
        llm=expand_llm,
        search=_RecordingSearch(),
        push_fn=_push_ok,
        map_categories=_expand_mapper,
    ):
        pass

    thread_key = await _thread_key(BuyerChatRequest.model_validate(_buyer_payload()), identity)
    stored = await (await get_thread_store()).get_chip_categories(thread_key)
    assert stored == []  # R6-1 과 같은 원칙 — 실제로 쓰이지 않은 대표값을 영속하지 않는다

    # 다음 승계(carry) 턴 — 확장 leaf 8개가 아니라 저장된 빈 집합을 따라 무필터로 진행한다
    # (오늘 동작 불변 — 이 assert 는 leaf 가 되살아나지 않음을 검증한다).
    search = _RecordingSearch()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="더 저렴한 걸로")),
            identity,
            llm=_refine_llm(),
            search=search,
            push_fn=_push_ok,
        )
    )
    assert len(search.filters) == 1  # 8-leg fan-out 이 아니다


async def test_restored_category_removal_case3_forced_still_single_list() -> None:
    """[B-4 split_by_need 가드] `case == 3` 을 강제한 복원 턴 → 목록(products.ready)이 정확히
    1건이다(split_by_need 가 열리지 않는다 — 복원 legs 는 새 니즈 전개가 아니다)."""
    identity = _member()
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="유럽여행 준비물 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_three_leg_mapper(),
        )
    )
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": _LEG_B}],
        }
    )
    case3_llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 3,  # 강제 — 복원 턴이 우연히 case==3 이 되어도 분할되면 안 된다
            "categoryQueries": [],
            "filters": {},
        }
    )

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )

    push = _RecordingPush()
    await _collect(
        _run_buyer_turn(remove_request, identity, llm=case3_llm, search=_search, push_fn=push)
    )
    assert len(push.pushes) == 1
    assert len(push.pushes[0].lists) == 1  # split_by_need 안 열림


async def test_restored_category_legs_truncated_to_fanout_max() -> None:
    """상한 — 저장 집합이 `category_fanout_max` 보다 크면 그 수로 잘린다."""
    identity = _member()
    cap = get_settings().category_fanout_max
    over_cap = cap + 2
    legs = [(f"카테고리{i}", f"쿼리{i}") for i in range(over_cap)]

    async def _over_cap_mapper(
        *, category_queries, utterance, settings, llm=None, tier="fast", **_
    ):
        return CategoryMapping(legs=legs)

    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload(message="여러 카테고리 추천")),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
            map_categories=_over_cap_mapper,
        )
    )
    # 하나를 지목 제거해도 남는 값(over_cap - 1)이 여전히 cap 을 넘는다.
    remove_request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "conditionActions": [{"op": "remove", "field": "category", "value": "카테고리0"}],
        }
    )
    search = _RecordingSearch()
    await _collect(
        _run_buyer_turn(
            remove_request, identity, llm=_refine_llm(), search=search, push_fn=_push_ok
        )
    )
    assert len(search.filters) == cap  # category_fanout_max 로 절단
