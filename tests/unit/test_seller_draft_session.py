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

from app.agents.seller import draft_lifecycle, draft_session, hitl
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


# ── [#622] load_pending_state — 3상태(none/found/unknown) ──────────────────────


def test_load_pending_state_none_when_absent() -> None:
    """대기 없음 — "none", pending 은 None."""
    state, pending = asyncio.run(draft_session.load_pending_state(_CONTEXT, _THREAD))
    assert (state, pending) == ("none", None)


def test_load_pending_state_found_when_present() -> None:
    """정상 대기 — "found", pending 채워짐."""

    async def run():
        await draft_session.save_pending(_CONTEXT, _THREAD, _pending())
        return await draft_session.load_pending_state(_CONTEXT, _THREAD)

    state, pending = asyncio.run(run())
    assert state == "found"
    assert pending is not None and pending.draft_id == "d-1"


def test_load_pending_state_ttl_expiry_is_none_not_unknown() -> None:
    """TTL 만료는 "조회 실패"가 아니라 진짜 "없음"이다 — 폐기까지 수행하고 "none"."""
    old = datetime.now(UTC) - timedelta(minutes=11)

    async def run():
        await draft_session.save_pending(_CONTEXT, _THREAD, _pending(created_at=old))
        state, pending = await draft_session.load_pending_state(_CONTEXT, _THREAD)
        # 폐기까지 확인 — 재조회해도 여전히 없음.
        state2, _ = await draft_session.load_pending_state(_CONTEXT, _THREAD)
        return state, pending, state2

    state, pending, state2 = asyncio.run(run())
    assert (state, pending) == ("none", None)
    assert state2 == "none"


def test_load_pending_state_format_mismatch_is_none_and_discards() -> None:
    """저장형이 손상됐으면(형식 불일치) "unknown"이 아니라 "none"으로 취급하고 폐기한다."""

    async def run():
        graph = await draft_session._get_graph()
        await graph.aupdate_state(
            draft_session._config(_CONTEXT, _THREAD),
            {"pending": {"garbage": True}},  # draft_id 없음 — _load 가 KeyError
            as_node=draft_session._RECORDER_NODE,
        )
        state, pending = await draft_session.load_pending_state(_CONTEXT, _THREAD)
        state2, _ = await draft_session.load_pending_state(_CONTEXT, _THREAD)
        return state, pending, state2

    state, pending, state2 = asyncio.run(run())
    assert (state, pending) == ("none", None)
    assert state2 == "none"


def test_load_pending_state_query_failure_is_unknown(monkeypatch) -> None:
    """조회 자체가 실패(쿼리 타임아웃 등)하면 "unknown" — "none"과 구분되는 유일한 경로다.

    draft_lifecycle.lookup_pending 이 이 상태만 보고 발급 경로를 차단한다(UNKNOWN_STATE_BLOCK_TEXT).
    """

    async def _boom():
        raise RuntimeError("query timeout")

    monkeypatch.setattr(draft_session, "_get_graph", _boom)

    state, pending = asyncio.run(draft_session.load_pending_state(_CONTEXT, _THREAD))

    assert state == "unknown"
    assert pending is None


def test_load_pending_wrapper_folds_unknown_into_none(monkeypatch) -> None:
    """하위호환 wrapper(`load_pending`)는 "unknown"도 기존 계약대로 None 으로 뭉갠다."""

    async def _boom():
        raise RuntimeError("query timeout")

    monkeypatch.setattr(draft_session, "_get_graph", _boom)

    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is None


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


def test_gate_intercepts_apply_shortcut_while_pending(monkeypatch) -> None:
    """[리뷰 M-1a] 초안 대기 중 "N번 적용해줘"는 ①.5 apply 가 아니라 게이트가 먼저
    받는다 — 우회 시 두 번째 draft 가 발급돼 이전 create draft 와 동시 생존한다."""

    async def classify(message):
        return "offtopic"

    monkeypatch.setattr(seller_api, "_classify_pending_utterance", classify)

    def _no_apply(message):
        raise AssertionError("초안 대기 중에는 apply 선판정에 도달하면 안 된다")

    monkeypatch.setattr(seller_api, "parse_apply_message", _no_apply)
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    events = _collect_seller(_request("2번 적용해줘"))
    assert events[-1]["data"]["panel"] == "keep"
    assert asyncio.run(draft_session.load_pending(_CONTEXT, _THREAD)) is not None


def test_gate_llm_not_configured_falls_through(monkeypatch) -> None:
    """[리뷰 H-2] 게이트 LLM 미구성은 INTERNAL 로 죽지 않고 일반 흐름으로 낙하해
    route_question 이 LLM_UNAVAILABLE 계약 이벤트로 응답한다(여기서는 낙하만 검증)."""
    from app.core.llm import LLMNotConfigured

    class _Boom:
        def with_structured_output(self, schema):
            raise LLMNotConfigured("no provider")

    monkeypatch.setattr(seller_api, "init_seller_model", lambda role: _Boom())
    asyncio.run(draft_session.save_pending(_CONTEXT, _THREAD, _pending()))

    # scope 차단 발화 — 낙하가 성립하면 refused 레인으로 끝난다(예외 전파 없음).
    events = _collect_seller(_request("경쟁사 매출 알려줘"))
    assert events[0]["data"]["lane"] == "refused"
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


# ── [#622] pending_unknown 게이트 — draft *발급* 경로만 차단, 조회/대화는 그대로 ──


def _force_lookup_unknown(monkeypatch) -> None:
    """draft_lifecycle.lookup_pending 을 "unknown"으로 고정 — 조회 실패를 재현."""

    async def _unknown(context, thread_id):
        return draft_lifecycle.PendingLookup(state="unknown")

    monkeypatch.setattr(seller_api.draft_lifecycle, "lookup_pending", _unknown)


def test_unknown_pending_state_blocks_image_direct_entry(monkeypatch) -> None:
    """[#622] 조회 실패 중 사진 첨부 턴(product 직행) — 새 draft 발급 대신 차단 안내."""
    _force_lookup_unknown(monkeypatch)
    request = SellerChatRequest(
        session_id="s-1",
        thread_id=_THREAD,
        message="이 사진으로 등록해줘",
        image_urls=["https://cdn.example.com/new.jpg"],
    )

    events = _collect_seller(request)

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "product"
    assert events[1]["data"]["text"] == draft_lifecycle.UNKNOWN_STATE_BLOCK_TEXT
    assert events[-1]["data"]["panel"] == "keep"


def test_unknown_pending_state_blocks_apply_shortcut(monkeypatch) -> None:
    """[#622] 조회 실패 중 "N번 적용해줘" — apply 도 새 draft 발급이므로 동일하게 차단."""
    _force_lookup_unknown(monkeypatch)

    events = _collect_seller(_request("2번 적용해줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "apply"
    assert events[1]["data"]["text"] == draft_lifecycle.UNKNOWN_STATE_BLOCK_TEXT
    assert events[-1]["data"]["panel"] == "keep"


def test_unknown_pending_state_blocks_supervisor_routed_product(monkeypatch) -> None:
    """[#622] ③ supervisor 가 product 로 라우팅한 경우에도 동일 가드가 적용된다."""
    _force_lookup_unknown(monkeypatch)

    async def _route_to_product(question, context, recent_turns=(), screen=None):
        from app.agents.seller.schemas import RouteDecision

        return RouteDecision(category="product", reason="stub", confidence=0.9)

    monkeypatch.setattr(seller_api, "route_question", _route_to_product)

    # [기존 회귀 테스트(test_seller_api.py)에서 실제로 product 로 라우팅됨이 확인된 발화.
    events = _collect_seller(_request("감귤청 가격 12900원으로 바꿔줘"))

    assert events[0]["data"]["lane"] == "product"
    assert events[1]["data"]["text"] == draft_lifecycle.UNKNOWN_STATE_BLOCK_TEXT
    assert events[-1]["data"]["panel"] == "keep"


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
