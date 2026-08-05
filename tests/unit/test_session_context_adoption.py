from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import psycopg
from langgraph.store.memory import InMemoryStore

from app.agents.buyer import session_state as session_state_module
from app.agents.buyer.cart import state as cart_state
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.graph import ThreadFilterStore, run_buyer_turn
from app.agents.buyer.recommendation.state import (
    RelaxationOfferStore,
    RepurchaseStore,
    RevertStore,
)
from app.agents.buyer.session_state import (
    adopt_legacy_thread,
    clear_context,
    context_thread_key,
    ensure_thread_adopted,
)
from app.core import pg_store, session_context
from app.core.auth import Identity
from app.core.conversation import ConversationStore
from app.core.session_context import (
    BuyerSessionInput,
    SessionClaimConflict,
    SessionContextRepository,
    SessionStateUnavailable,
)
from app.schemas.spring import CartOption, ProductSearchFilters

pytestmark = pytest.mark.asyncio


def _member() -> Identity:
    return Identity(user_id="42", is_guest=False, seller_id=None, subject="member-sub")


async def _guest_context(repo: SessionContextRepository):
    return await repo.touch(BuyerSessionInput("session-a", "thread-a", "guest", "guest-a"))


async def test_clear_context_removes_only_requested_thread_state() -> None:
    store = InMemoryStore()
    pg_store.set_store(store)
    target = context_thread_key("context-a", "thread-a")
    other = context_thread_key("context-a", "thread-b")
    filters = ThreadFilterStore(store)
    cart = CartStateStore(store)
    revert = RevertStore(store)
    repurchase = RepurchaseStore(store)
    relaxation = RelaxationOfferStore(store)
    await filters.put(target, ProductSearchFilters(category="대상"))
    await filters.put(other, ProductSearchFilters(category="보존"))
    await cart.set_pending(
        target,
        PendingAdd(
            product_id=1,
            quantity=1,
            options=[CartOption(option_id=2, name="파랑")],
        ),
    )
    await cart.set_last_reco(target, [(1, "대상 상품")])
    await cart.set_last_reco(other, [(2, "보존 상품")])
    await revert.add(target, ["세제"])
    await revert.add(other, ["조미료"])
    await repurchase.add(target, [1], cap=20)
    await repurchase.add(other, [2], cap=20)
    await relaxation.put(
        target,
        {"65,000원까지 볼까요?": {"field": "max_price", "value": 65000}},
        {"field": "max_price", "value": 65000},
    )
    await relaxation.put(other, {"보존 칩": {"field": "max_price", "value": 30000}}, None)

    counts = await clear_context("context-a", ["thread-a"])

    assert counts.filters == 1
    assert counts.pending == 1
    assert counts.last_recommendation == 1
    assert counts.local_names == 1
    assert counts.revert == 1
    assert counts.repurchase == 1
    assert counts.relaxation_offers == 1
    assert await filters.get(target) is None
    assert await cart.get_pending(target) is None
    assert await cart.get_last_reco(target) == []
    assert await revert.get(target) == set()
    assert await repurchase.get(target) == []
    assert await relaxation.get_snapshot(target) == ({}, None)
    assert (await filters.get(other)).category == "보존"
    assert await cart.get_last_reco(other) == [(2, "보존 상품")]
    assert await revert.get(other) == {"조미료"}
    assert await repurchase.get(other) == [2]
    assert await relaxation.get_snapshot(other) == (
        {"보존 칩": {"field": "max_price", "value": 30000}},
        None,
    )


async def test_adoption_keeps_v2_scalars_unions_revert_and_deletes_legacy_last() -> None:
    repo = SessionContextRepository()
    context = await _guest_context(repo)
    session_context._default_repository = repo
    store = InMemoryStore()
    pg_store.set_store(store)
    legacy_key = "guest-a:thread-a"
    target_key = context_thread_key(context.context_id, "thread-a")
    legacy_filters = ("buyer_thread_filters", legacy_key)
    legacy_cart = ("buyer_cart", legacy_key)
    legacy_revert = ("buyer_revert", legacy_key)
    await store.aput(legacy_filters, "filters", {"category": "legacy"})
    await store.aput(
        legacy_cart,
        "pending",
        {"product_id": 1, "quantity": 1, "options": [], "attempts": 0},
    )
    await store.aput(legacy_cart, "last_reco", {"product_ids": [1]})
    cart_state._last_reco_names[legacy_key] = {1: "legacy-name"}
    await store.aput(legacy_revert, "categories", {"categories": ["A", "C"]})
    await ThreadFilterStore(store).put(target_key, ProductSearchFilters(category="v2"))
    await CartStateStore(store).set_last_reco(target_key, [(2, "v2-name")])
    await RevertStore(store).add(target_key, ["B", "A"])

    result = await adopt_legacy_thread(context, "thread-a", "guest-a")

    assert result.adopted
    assert (await ThreadFilterStore(store).get(target_key)).category == "v2"
    assert await CartStateStore(store).get_last_reco(target_key) == [(2, "v2-name")]
    pending = await CartStateStore(store).get_pending(target_key)
    assert pending is not None and pending.product_id == 1
    assert await RevertStore(store).get(target_key) == {"A", "B", "C"}
    assert await store.aget(legacy_filters, "filters") is None
    assert await store.aget(legacy_cart, "pending") is None
    assert await store.aget(legacy_cart, "last_reco") is None
    assert await store.aget(legacy_revert, "categories") is None
    assert cart_state._last_reco_names.get(legacy_key) is None


class _FailOnceStore(InMemoryStore):
    fail_target_read = True

    async def aget(self, namespace, key, *, refresh_ttl=None):
        if self.fail_target_read and namespace[0].endswith("_v2") and key == "categories":
            self.fail_target_read = False
            raise TimeoutError("verification unavailable")
        return await super().aget(namespace, key, refresh_ttl=refresh_ttl)


async def test_adoption_failure_is_closed_and_retry_resumes_incomplete_state() -> None:
    repo = SessionContextRepository()
    context = await _guest_context(repo)
    session_context._default_repository = repo
    store = _FailOnceStore()
    pg_store.set_store(store)
    legacy_key = "guest-a:thread-a"
    await store.aput(("buyer_revert", legacy_key), "categories", {"categories": ["A"]})

    with pytest.raises(SessionStateUnavailable):
        await adopt_legacy_thread(context, "thread-a", "guest-a")
    assert await store.aget(("buyer_revert", legacy_key), "categories") is not None

    result = await adopt_legacy_thread(context, "thread-a", "guest-a")
    assert result.adopted
    assert await RevertStore(store).get(context_thread_key(context.context_id, "thread-a")) == {"A"}
    assert await store.aget(("buyer_revert", legacy_key), "categories") is None


async def test_memory_adoptions_for_different_threads_share_legacy_root_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SessionContextRepository()
    first = await repo.touch(BuyerSessionInput("session-a", "thread-a", "guest", "guest-a"))
    second = await repo.touch(BuyerSessionInput("session-b", "thread-b", "guest", "guest-b"))
    session_context._default_repository = repo
    store = InMemoryStore()
    pg_store.set_store(store)
    await store.aput(("buyer_thread_filters", "guest-a:thread-a"), "filters", {"category": "A"})
    await store.aput(("buyer_thread_filters", "guest-b:thread-b"), "filters", {"category": "B"})
    first_read = asyncio.Event()
    resume = asyncio.Event()
    original_item = session_state_module._item

    async def paused_item(store_arg, root, key, name):  # noqa: ANN001
        item = await original_item(store_arg, root, key, name)
        if root == "buyer_thread_filters" and key == "guest-a:thread-a":
            first_read.set()
            await resume.wait()
        return item

    monkeypatch.setattr(session_state_module, "_item", paused_item)
    first_task = asyncio.create_task(adopt_legacy_thread(first, "thread-a", "guest-a"))
    await asyncio.wait_for(first_read.wait(), timeout=1)
    second_task = asyncio.create_task(adopt_legacy_thread(second, "thread-b", "guest-b"))
    await asyncio.sleep(0)
    assert not second_task.done()
    resume.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result.adopted and second_result.adopted


async def test_cancelled_memory_adoption_releases_legacy_root_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SessionContextRepository()
    context = await _guest_context(repo)
    session_context._default_repository = repo
    store = InMemoryStore()
    pg_store.set_store(store)
    await store.aput(
        ("buyer_thread_filters", "guest-a:thread-a"),
        "filters",
        {"category": "legacy"},
    )
    entered = asyncio.Event()
    block = asyncio.Event()
    original_item = session_state_module._item

    async def blocked_item(store_arg, root, key, name):  # noqa: ANN001
        item = await original_item(store_arg, root, key, name)
        if root == "buyer_thread_filters" and key == "guest-a:thread-a":
            entered.set()
            await block.wait()
        return item

    monkeypatch.setattr(session_state_module, "_item", blocked_item)
    task = asyncio.create_task(adopt_legacy_thread(context, "thread-a", "guest-a"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    monkeypatch.setattr(session_state_module, "_item", original_item)
    result = await asyncio.wait_for(adopt_legacy_thread(context, "thread-a", "guest-a"), timeout=1)
    assert result.adopted


async def test_member_adoption_uses_pre_claim_guest_owner_history() -> None:
    repo = SessionContextRepository()
    guest = await _guest_context(repo)
    claimed = await repo.claim_owner("session-a", "guest-a", 42)
    assert claimed.context.context_id == guest.context_id
    session_context._default_repository = repo
    store = InMemoryStore()
    pg_store.set_store(store)
    await store.aput(
        ("buyer_thread_filters", "guest-a:thread-a"),
        "filters",
        {"category": "guest-state"},
    )

    await ensure_thread_adopted(guest.context_id, "thread-a", "42")

    adopted = await ThreadFilterStore(store).get(context_thread_key(guest.context_id, "thread-a"))
    assert adopted is not None and adopted.category == "guest-state"


async def test_conversation_commit_and_adoption_share_canonical_memory_repository() -> None:
    store = ConversationStore()
    session_context.reset()
    buyer_session = BuyerSessionInput("canonical-session", "canonical-thread", "member", "42")

    committed = await store.save_user_message(
        "canonical-session",
        "42",
        "member",
        "추천",
        thread_id="canonical-thread",
        buyer_session=buyer_session,
    )

    canonical = await session_context._default_repository.get_context("canonical-session")
    assert canonical is not None
    assert committed.context_id == canonical.context_id
    result = await ensure_thread_adopted(canonical.context_id, "canonical-thread", "42")
    assert result.adopted


async def test_unknown_memory_context_preserves_domain_conflict_without_fabrication() -> None:
    session_context.reset()

    with pytest.raises(SessionClaimConflict):
        await ensure_thread_adopted("unknown-context", "thread", "42")

    assert await session_context._default_repository.get_context("missing-session") is None


@pytest.mark.parametrize(
    "failure",
    [
        psycopg.ProgrammingError("bad sql"),
        psycopg.IntegrityError("broken invariant"),
        ValueError("domain bug"),
    ],
)
async def test_ensure_thread_adopted_preserves_non_infra_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """adoption 경계는 programming/integrity/domain 오류를 503 원인으로 마스킹하지 않는다."""

    async def fail(*_args):
        raise failure

    monkeypatch.setattr(session_state_module, "_resolve_context_and_legacy_owner", fail)

    with pytest.raises(type(failure)):
        await ensure_thread_adopted("context", "thread", "42")


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("deadline"), psycopg.OperationalError("connection lost")],
)
async def test_ensure_thread_adopted_wraps_only_state_store_unavailability(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """adoption 경계의 실제 state-store I/O 장애만 SessionStateUnavailable로 변환한다."""

    async def fail(*_args):
        raise failure

    monkeypatch.setattr(session_state_module, "_resolve_context_and_legacy_owner", fail)

    with pytest.raises(SessionStateUnavailable) as exc:
        await ensure_thread_adopted("context", "thread", "42")
    assert exc.value.__cause__ is failure


async def test_graph_adopts_after_commit_context_before_first_state_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    async def ensure(context_id, thread_id, owner_id):
        events.append(("adopt", context_id, thread_id, owner_id))

    class StopAtFirstRead:
        async def get(self, key):
            events.append(("filter", key))
            raise RuntimeError("stop after first transient read")

    async def get_thread_store():
        return StopAtFirstRead()

    monkeypatch.setattr("app.agents.buyer.graph.ensure_thread_adopted", ensure)
    monkeypatch.setattr("app.agents.buyer.graph.get_thread_store", get_thread_store)
    observer = SimpleNamespace(
        context_id="committed-context",
        request_id="request-a",
        record_model_call=lambda *_: None,
    )

    stream = run_buyer_turn(
        SimpleNamespace(session_id="session-a", thread_id="thread-a", message="추천"),
        _member(),
        llm=object(),
        observer=observer,
    )
    with pytest.raises(RuntimeError, match="stop after first transient read"):
        await anext(stream)

    assert events == [
        ("adopt", "committed-context", "thread-a", "member-sub"),
        ("filter", "committed-context:thread-a"),
    ]


async def test_graph_rejects_observer_without_committed_context_before_state_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_store():
        raise AssertionError("transient store must not be opened")

    monkeypatch.setattr("app.agents.buyer.graph.get_thread_store", unexpected_store)
    observer = SimpleNamespace(
        context_id=None,
        request_id="request-a",
        record_model_call=lambda *_: None,
    )
    stream = run_buyer_turn(
        SimpleNamespace(session_id="session-a", thread_id="thread-a", message="추천"),
        _member(),
        llm=object(),
        observer=observer,
    )

    with pytest.raises(SessionStateUnavailable):
        await anext(stream)


@pytest.mark.parametrize("observer", [None, SimpleNamespace(request_id="request-a")])
async def test_graph_requires_observer_context_before_llm_or_state_access(
    monkeypatch: pytest.MonkeyPatch,
    observer,
) -> None:
    accessed: list[str] = []

    def unexpected_llm():
        accessed.append("llm")
        raise AssertionError

    async def unexpected_store():
        accessed.append("state")
        raise AssertionError

    monkeypatch.setattr("app.agents.buyer.graph.get_llm", unexpected_llm)
    monkeypatch.setattr("app.agents.buyer.graph.get_thread_store", unexpected_store)
    stream = run_buyer_turn(
        SimpleNamespace(session_id="session-a", thread_id="thread-a", message="추천"),
        _member(),
        observer=observer,
    )

    with pytest.raises(SessionStateUnavailable):
        await anext(stream)
    assert accessed == []
