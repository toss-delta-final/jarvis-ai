"""구매자 conditionActions 계약·멀티턴 필터 제거 회귀 (#278)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from app.agents.buyer.graph import get_thread_store, run_buyer_turn as _production_run_buyer_turn
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
    assert [event["type"] for event in events] == [
        "conditions",
        "token",
        "products.ready",
        "done",
    ]
