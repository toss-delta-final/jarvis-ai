"""니즈 priority 분류기 배선 — 게이트·회수·제외 순서·degrade (이슈 #281, #60 후속).

`tests/unit/test_need_priority.py` 가 분류기 자체(파싱·degrade·관측)를 고정하고,
`tests/unit/test_budget_sets.py` 가 `priorities=` 소비(제외 순서 산술)를 고정한다.
여기 테스트는 그 둘을 잇는 **배관**만 본다 — `stream_recommendation` 이 게이트를 지키고,
분류기 산출을 실제로 `build_budget_sets` 에 넘기고, 실패해도 스트림이 정상 종료되는지.
가짜 LLM 만 쓴다(CI API 콜 0).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.recommendation.need_priority import _SYSTEM as _PRIORITY_SYSTEM
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.llm import LLMError
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import ProductSearchResult, SpringProduct
from tests._fakes import FakeLLM

# 정본 예시(REQ-REC-004/결정 14-H) 그대로 — 등뼈=필수(1)·들깨가루=권장(2)·청양고추=선택(3).
_LEGS = [("A", "등뼈"), ("B", "들깨가루"), ("C", "청양고추")]
_PRICES = {"A": 15_000, "B": 10_000, "C": 5_000}
_PRODUCT_ID = {"A": 11, "B": 21, "C": 31}


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


async def _map(**kwargs):  # noqa: ANN001
    return CategoryMapping(legs=list(_LEGS))


async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
    category = filters.category
    return ProductSearchResult(
        products=[
            SpringProduct(
                product_id=_PRODUCT_ID[category],
                name="상품",
                price=_PRICES[category],
                rating=4.0,
                category=category,
                brand="b",
            )
        ],
        total_count=1,
    )


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


def _req(message: str, *, total_budget: int | None = 27_000, buy_all: bool = True):
    decompose = {
        "intent": "recommend",
        "case": 3,
        "buyAll": buy_all,
        "categoryQueries": [{"category": c, "query": q} for c, q in _LEGS],
        "filters": {},
    }
    if total_budget is not None:
        decompose["totalBudget"] = total_budget
    return SimpleNamespace(session_id="s1", thread_id="t1", message=message), decompose


class _PriorityAwareLLM(FakeLLM):
    """분류기 호출을 system 시그니처로 가른다 — 두 호출 모두 tier="fast" 라 tier 로는 안 갈린다."""

    def __init__(
        self,
        *,
        decompose,
        priorities: list[int] | None = None,
        priority_error: Exception | None = None,
    ) -> None:
        super().__init__(decompose=decompose)
        self._priorities = priorities
        self._priority_error = priority_error
        self.priority_calls = 0

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if system is _PRIORITY_SYSTEM:
            self.priority_calls += 1
            if self._priority_error is not None:
                raise self._priority_error
            if self._priorities is None:
                return "{}"
            return json.dumps({"priorities": self._priorities}, ensure_ascii=False)
        return await super().complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )


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
        request_id="need-priority-wiring-test",
        context_id=context.context_id,
        record_model_call=lambda *_: None,
    )


async def _run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(
        request, identity, observer=observer, map_categories=_map, search=_search, **kwargs
    ):
        yield frame


# ─────────── 게이트 ───────────


@pytest.mark.parametrize(
    ("buy_all", "total_budget", "classifier_enabled"),
    [
        (False, 27_000, True),  # buy_all=False
        (True, None, True),  # 예산 없음
        (True, 27_000, False),  # 롤백 스위치 off
    ],
)
async def test_gate_closed_means_zero_priority_calls(
    monkeypatch: pytest.MonkeyPatch, buy_all, total_budget, classifier_enabled
) -> None:
    monkeypatch.setattr(get_settings(), "need_priority_classifier_enabled", classifier_enabled)
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    request, decompose = _req("감자탕 재료 전부", total_budget=total_budget, buy_all=buy_all)
    llm = _PriorityAwareLLM(decompose=decompose, priorities=[1, 2, 3])
    push = _RecordingPush()

    await _collect(_run_buyer_turn(request, _member(), llm=llm, push_fn=push))

    assert llm.priority_calls == 0


async def test_gate_open_calls_the_classifier_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    request, decompose = _req("감자탕 재료 전부")
    llm = _PriorityAwareLLM(decompose=decompose, priorities=[1, 2, 3])
    push = _RecordingPush()

    await _collect(_run_buyer_turn(request, _member(), llm=llm, push_fn=push))

    assert llm.priority_calls == 1


# ─────────── priority 가 실제로 제외 순서를 바꾼다 ───────────


async def test_priority_signal_changes_which_need_gets_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """priority 없이는 최저가가 가장 비싼 "등뼈"(필수)가 먼저 빠지지만, priority 를 적용하면
    "청양고추"(선택)가 대신 빠진다 — 같은 픽스처로 두 산출이 달라야 배선이 실제로 값을 쓴다는
    증거가 된다(vacuous 테스트 방지)."""
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    request, decompose = _req("감자탕 재료 전부")

    baseline_llm = _PriorityAwareLLM(decompose=decompose, priorities=None)
    baseline_push = _RecordingPush()
    baseline_events = await _collect(
        _run_buyer_turn(request, _member(), llm=baseline_llm, push_fn=baseline_push)
    )
    baseline_notice = next(
        event["data"]["text"]
        for event in baseline_events
        if event["type"] == "token"
        and ("등뼈" in event["data"]["text"] or "청양고추" in event["data"]["text"])
    )
    assert (
        "등뼈" in baseline_notice
    )  # priority 없으면 최저가가 가장 비싼 필수 니즈가 빠진다(오늘 동작)

    priority_llm = _PriorityAwareLLM(decompose=decompose, priorities=[1, 2, 3])
    priority_push = _RecordingPush()
    priority_events = await _collect(
        _run_buyer_turn(request, _member(), llm=priority_llm, push_fn=priority_push)
    )
    priority_notice = next(
        event["data"]["text"]
        for event in priority_events
        if event["type"] == "token"
        and ("등뼈" in event["data"]["text"] or "청양고추" in event["data"]["text"])
    )
    assert "청양고추" in priority_notice  # priority 적용 시 선택(3) 이 먼저 빠진다(REQ-REC-076)
    assert priority_push.pushes[0].list_type == "BUY_ALL"


async def test_dropped_notice_precedes_products_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-REC-075 투명 안내 — priority 로 바뀐 제외 이름도 기존 token 고지 배관을 그대로 탄다."""
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    request, decompose = _req("감자탕 재료 전부")
    llm = _PriorityAwareLLM(decompose=decompose, priorities=[1, 2, 3])
    push = _RecordingPush()

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm, push_fn=push))

    notice_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "token" and "청양고추" in event["data"]["text"]
    )
    ready_index = next(
        index for index, event in enumerate(events) if event["type"] == "products.ready"
    )
    assert notice_index < ready_index


# ─────────── 분류기 실패해도 스트림은 산다 ───────────


async def test_classifier_failure_still_completes_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    request, decompose = _req("감자탕 재료 전부")
    llm = _PriorityAwareLLM(decompose=decompose, priority_error=LLMError("boom"))
    push = _RecordingPush()

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm, push_fn=push))

    types = [event["type"] for event in events]
    assert "error" not in types
    assert types[-1] == "done"
    assert push.pushes and push.pushes[0].list_type == "BUY_ALL"


# ─────────── [FINDINGS-2 F-1] 바깥 취소가 finally 를 실제로 밟는다 ───────────


async def test_outer_cancellation_cancels_the_orphaned_priority_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`finally: _cancel_priority_task(...)` 가 없으면 분류기 태스크가 고아로 남는다(#84 라운드 4
    와 같은 함정) — 이 테스트는 그것을 기계적으로 증명한다.

    priority 분류기와 rerank(smart tier) 를 **둘 다** 무한정 재운다. 바깥 소비자를 rerank 대기
    지점에서 취소하면, asyncio 의 fut_waiter 위임은 **rerank 쪽 future** 를 취소할 뿐 priority_task
    에는 닿지 않는다 — priority_task 를 실제로 걷는 것은 오직 `finally` 의 동기 `task.cancel()`
    뿐이다. (반대로 취소가 `_collect_priority_task` 의 `await task` 지점에서 오면 위임이 그
    태스크를 직접 건드려 `finally` 없이도 우연히 통과하므로, 이 함정을 증명하려면 반드시 **다른
    await**(rerank)에서 취소가 와야 한다 — `app/agents/buyer/graph.py::_cancel_scope_task`
    docstring 의 위임 설명 참조.)
    """
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    request, decompose = _req("감자탕 재료 전부")
    priority_started = asyncio.Event()
    rerank_started = asyncio.Event()

    class _HangingLLM(_PriorityAwareLLM):
        def __init__(self) -> None:
            super().__init__(decompose=decompose, priorities=[1, 2, 3])
            self.priority_cancelled = False

        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
            if system is _PRIORITY_SYSTEM:
                self.priority_calls += 1
                priority_started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    self.priority_cancelled = True
                    raise
                return json.dumps({"priorities": [1, 2, 3]})
            if tier == "smart":
                rerank_started.set()
                await asyncio.sleep(3600)  # 바깥 취소가 여기(rerank await)로 오게 만든다
            return await FakeLLM.complete(
                self,
                system=system,
                user=user,
                tier=tier,
                max_tokens=max_tokens,
                json_output=json_output,
            )

    llm = _HangingLLM()
    push = _RecordingPush()
    before = {task for task in asyncio.all_tasks()}

    async def _drive() -> None:
        await _collect(_run_buyer_turn(request, _member(), llm=llm, push_fn=push))

    consumer = asyncio.create_task(_drive())
    await asyncio.wait_for(priority_started.wait(), timeout=5)
    await asyncio.wait_for(rerank_started.wait(), timeout=5)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    for _ in range(5):  # [라운드 4] 동기 취소 — 배달은 이벤트루프가 한다
        await asyncio.sleep(0)

    leftover = [task for task in asyncio.all_tasks() if task not in before and not task.done()]
    assert llm.priority_calls == 1
    assert llm.priority_cancelled, "분류기 태스크가 취소되지 않았다 — finally 가 걷히지 않았다"
    assert not leftover, f"정리되지 않은 태스크: {leftover}"


# ─────────── [FINDINGS-2 F-2] 라벨 없는 leg 은 분류기를 아예 돌리지 않는다 ───────────


async def test_blank_label_leg_disables_the_classifier_but_keeps_the_budget_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_need_label` 이 하나라도 None 이면(정제 후 canonical·query 가 모두 빈 leg) 인덱스 정합을
    보장할 수 없어 분류기를 아예 돌리지 않는다 — 그래도 예산 세트는 폴백(균일 priority)으로
    그대로 만들어진다(오늘 동작)."""
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    blank_legs = [("A", "등뼈"), ("", ""), ("C", "청양고추")]
    prices = {"A": 15_000, "": 10_000, "C": 5_000}
    product_id = {"A": 11, "": 21, "C": 31}

    async def _map_blank(**kwargs):  # noqa: ANN001
        return CategoryMapping(legs=list(blank_legs))

    async def _search_blank(filters, exclude_product_ids=None):  # noqa: ANN001
        category = filters.category
        return ProductSearchResult(
            products=[
                SpringProduct(
                    product_id=product_id[category],
                    name="상품",
                    price=prices[category],
                    rating=4.0,
                    category=category or None,
                    brand="b",
                )
            ],
            total_count=1,
        )

    request, decompose = _req("감자탕 재료 전부", total_budget=27_000)
    llm = _PriorityAwareLLM(decompose=decompose, priorities=[1, 2, 3])
    push = _RecordingPush()

    observer = await _committed_observer(request, _member())
    events = await _collect(
        _production_run_buyer_turn(
            request,
            _member(),
            observer=observer,
            llm=llm,
            map_categories=_map_blank,
            search=_search_blank,
            push_fn=push,
        )
    )

    types = [event["type"] for event in events]
    assert llm.priority_calls == 0
    assert "error" not in types
    assert types[-1] == "done"
    assert push.pushes and push.pushes[0].list_type == "BUY_ALL"  # 폴백으로도 세트는 만들어진다
