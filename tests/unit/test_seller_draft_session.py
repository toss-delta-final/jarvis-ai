"""등록 초안 세션 저장소 + 입구 ①.8 게이트 (#506) — 실 LLM·PG 없음.

checkpointer 는 tests/unit/conftest.py 가 InMemory 로 자동 주입한다(hitl 공용).
게이트 LLM 이 필요 없는 경로만 입구 통합 테스트로 고정한다: 취소 단축경로(코드 판정),
대기 없음 낙하. 분류 LLM 경로는 _classify_pending_utterance 를 monkeypatch 로 대체한다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.seller import draft_session, hitl
from app.agents.seller.context import SellerContext
from app.agents.seller.hitl import DraftRecord
from app.agents.seller.schemas import DraftChange
from app.api import seller as seller_api
from app.core.auth import Identity
from app.schemas.seller import SellerChatRequest

_IDENTITY = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="3")
_CONTEXT = SellerContext(seller_id=7, brand_id=3)
_THREAD = "t-1"


@pytest.fixture(autouse=True)
def _hitl_memory_checkpointer():
    hitl.set_checkpointer(InMemorySaver())
    yield
    hitl.set_checkpointer(None)


def _pending(
    draft_id: str = "d-1", created_at: datetime | None = None
) -> draft_session.PendingCreate:
    return draft_session.PendingCreate(
        draft_id=draft_id,
        image_urls=("https://cdn.example.com/a.jpg",),
        analysis={"name": "셔츠", "summary": "s", "description": "d", "category_hint": "셔츠"},
        changes={"name": "셔츠", "price": "32000"},
        created_at=created_at or datetime.now(UTC),
    )


def _request(message: str) -> SellerChatRequest:
    return SellerChatRequest(session_id="s-1", thread_id=_THREAD, message=message)


def _collect_seller(request: SellerChatRequest) -> list[dict]:
    async def run() -> list[str]:
        return [line async for line in seller_api._seller_stream(request, _IDENTITY)]

    return [json.loads(line[len("data: ") :]) for line in asyncio.run(run())]


# ── 저장소 수명주기 ──────────────────────────────────────────────────────────────


def test_pending_roundtrip() -> None:
    async def run():
        assert await draft_session.save_pending(_CONTEXT, _THREAD, _pending())
        return await draft_session.load_pending(_CONTEXT, _THREAD)

    loaded = asyncio.run(run())
    assert loaded is not None
    assert loaded.draft_id == "d-1"
    assert loaded.image_urls == ("https://cdn.example.com/a.jpg",)
    assert loaded.analysis["category_hint"] == "셔츠"
    assert loaded.changes == {"name": "셔츠", "price": "32000"}


def test_pending_absent_and_clear() -> None:
    async def run():
        first = await draft_session.load_pending(_CONTEXT, _THREAD)
        await draft_session.save_pending(_CONTEXT, _THREAD, _pending())
        await draft_session.clear_pending(_CONTEXT, _THREAD)
        second = await draft_session.load_pending(_CONTEXT, _THREAD)
        return first, second

    assert asyncio.run(run()) == (None, None)


def test_pending_ttl_expiry_clears() -> None:
    """TTL(초안과 동일값) 경과 대기는 조회 시점에 폐기된다 — FE 10분 타이머와 정렬."""
    old = datetime.now(UTC) - timedelta(minutes=11)

    async def run():
        await draft_session.save_pending(_CONTEXT, _THREAD, _pending(created_at=old))
        return await draft_session.load_pending(_CONTEXT, _THREAD)

    assert asyncio.run(run()) is None


def test_pending_namespace_is_seller_scoped() -> None:
    """seller_id 접두 = IDOR 차단 — 타 판매자는 같은 threadId 로도 대기를 못 본다."""
    other = SellerContext(seller_id=8, brand_id=3)

    async def run():
        await draft_session.save_pending(_CONTEXT, _THREAD, _pending())
        return await draft_session.load_pending(other, _THREAD)

    assert asyncio.run(run()) is None


# ── 입구 ①.8 게이트 ─────────────────────────────────────────────────────────────


def _start_draft(draft_id: str = "d-1") -> None:
    record = DraftRecord(
        draft_id=draft_id,
        op="create",
        product_id=None,
        changes=[
            DraftChange(field="name", before="", after="셔츠"),
            DraftChange(field="price", before="", after="32000"),
            DraftChange(field="stock_quantity", before="", after="50"),
        ],
        summary="새 상품 1건 등록 초안",
        seller_id=7,
        brand_id=3,
        created_at=datetime.now(UTC).isoformat(),
    )
    asyncio.run(hitl.start_draft(record))


def test_cancel_shortcut_invalidates_and_clears(monkeypatch) -> None:
    """'취소' 정형 발화 — LLM 0회로 초안 무효화 + 세션 폐기 + done{replace}."""
    monkeypatch.setattr(
        seller_api,
        "_classify_pending_utterance",
        _fail_classify,
    )
    _start_draft()
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    events = _collect_seller(_request("취소"))
    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[-1]["data"]["panel"] == "replace"
    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is None
    # 무효화된 draftId 의 confirm 은 not_found — 옛 카드 승인 사고 차단(§5.6).
    outcome = asyncio.run(hitl.confirm_draft("d-1", seller_id=7, brand_id=3))
    assert outcome.status == "not_found"


async def _fail_classify(message):
    raise AssertionError("취소 단축경로에서 게이트 LLM 을 호출하면 안 된다")


def test_gate_approve_guides_to_button(monkeypatch) -> None:
    """승인 의도 텍스트 — 실행하지 않고 버튼 안내 + 초안·대기 유지(발화 ≠ 동의)."""

    async def classify(message):
        return "approve"

    monkeypatch.setattr(seller_api, "_classify_pending_utterance", classify)
    _start_draft()
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    events = _collect_seller(_request("응 등록해줘"))
    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[-1]["data"]["panel"] == "keep"
    assert "[등록]" in events[1]["data"]["text"]
    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is not None


def test_gate_offtopic_blocks_with_escape_hatch(monkeypatch) -> None:
    """딴 주제 — 차단 안내(탈출구 '취소' 문안 포함) + 초안 유지 + done{keep}."""

    async def classify(message):
        return "offtopic"

    monkeypatch.setattr(seller_api, "_classify_pending_utterance", classify)
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    events = _collect_seller(_request("이번 달 매출 얼마야?"))
    assert events[-1]["data"]["panel"] == "keep"
    assert "취소" in events[1]["data"]["text"]
    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is not None


def test_gate_failure_falls_through_to_normal_flow(monkeypatch) -> None:
    """게이트 판정 실패 — 초안을 유지한 채 일반 흐름(scope 등)으로 낙하한다(비파괴)."""

    async def classify(message):
        return None

    monkeypatch.setattr(seller_api, "_classify_pending_utterance", classify)
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    # scope 차단 발화 — 낙하 후 ②(scope)가 거절 레인으로 처리하면 낙하가 증명된다
    # ("경쟁사"는 SCOPE_BLOCK_RULES 트리거 — LLM 없이 코드 판정).
    events = _collect_seller(_request("경쟁사 매출 알려줘"))
    assert events[0]["data"]["lane"] == "refused"
    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is not None


def test_confirm_executed_clears_pending(monkeypatch) -> None:
    """create confirm 성공 — 대기 세션 해제(§5.8 refresh 흐름의 서버측 대응)."""

    async def fake_confirm(draft_id, *, seller_id, brand_id):
        return hitl.ConfirmOutcome("executed", "상품을 등록했습니다")

    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    request = SellerChatRequest(
        session_id="s-1", thread_id=_THREAD, action="confirm", draft_id="d-1"
    )
    events = _collect_seller(request)
    assert events[-1]["data"]["panel"] == "refresh"
    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is None
