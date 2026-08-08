"""기간 확인 흐름 — 대기 저장소 + 입구 3경로 + 승인 재개 (이슈 #345).

docs/specs/DESIGN-SELLER-PERIOD.md §5·§6 의 계약을 고정한다.
- §5.1 3경로: 승인 / 새 질문(승인 아닌 모든 발화) / TTL 만료
- §5.4 저장: `seller-period:{seller_id}:{threadId}` 네임스페이스, 스레드당 1건
- §5.5 입구 순서: ①.7 은 ②(scope)보다 앞이고 ①·①.5 보다 뒤다
- §6 승인 재개: planner 재호출 0회 (#269 완료 조건)

실 LLM·PG 없음 — checkpointer 는 tests/unit/conftest.py 가 InMemory 로 자동 주입한다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.seller import hitl
from app.agents.seller import period_confirm as seller_period_confirm
from app.agents.seller.context import SellerContext
from app.agents.seller.orchestrator import PipelineResult, VerifiedReport
from app.agents.seller.pipeline import ResolvedPlan
from app.api import seller as seller_api
from app.core.auth import Identity
from app.schemas.seller import SellerChatRequest

_IDENTITY = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="3")
_CONTEXT = SellerContext(seller_id=7, brand_id=3)
_THREAD = "t-1"

_PLAN = ResolvedPlan(
    analyses=("sales_anomaly",),
    date_from=dt.date(2026, 8, 1),
    date_to=dt.date(2026, 8, 5),
    wants_chart=False,
    needs_confirmation=True,
    period_expr="이번 달",
    period_clipped=True,
)


@pytest.fixture(autouse=True)
def _hitl_memory_checkpointer():
    """product/confirm 레인과 같은 이유로 InMemory 주입 — PG 연결 없이 돈다."""
    hitl.set_checkpointer(InMemorySaver())
    yield
    hitl.set_checkpointer(None)


def _request(message: str) -> SellerChatRequest:
    return SellerChatRequest(session_id="s-1", thread_id=_THREAD, message=message)


def _collect_seller(request: SellerChatRequest) -> list[dict]:
    """_seller_stream(통합 입구)을 전부 소비해 SSE 페이로드 목록으로 파싱한다."""

    async def run() -> list[str]:
        return [line async for line in seller_api._seller_stream(request, _IDENTITY)]

    return [json.loads(line[len("data: ") :]) for line in asyncio.run(run())]


def _no_route(question, context, recent_turns=(), screen=None):
    raise AssertionError("이 경로에서는 라우팅(LLM)을 호출하면 안 된다")


def _no_planner_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
    raise AssertionError("승인 경로에서 planner 파이프라인을 호출하면 안 된다(#269 완료 조건)")


# ── 대기 저장소 (DESIGN §5.4) ───────────────────────────────────────────────────


def test_pending_roundtrip_preserves_plan() -> None:
    """저장한 계획이 그대로 돌아온다 — 승인 재개가 이 값만 보고 실행한다."""

    async def run():
        assert await seller_period_confirm.save_pending(
            _CONTEXT, _THREAD, question="이번 달 매출 분석해줘", plan=_PLAN
        )
        return await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    pending = asyncio.run(run())
    assert pending is not None
    assert pending.question == "이번 달 매출 분석해줘"
    assert pending.plan.analyses == ("sales_anomaly",)
    assert pending.plan.date_from == dt.date(2026, 8, 1)
    assert pending.plan.date_to == dt.date(2026, 8, 5)


def test_pending_reload_clears_confirmation_flag() -> None:
    """재개용 계획의 needs_confirmation 은 False 다.

    True 로 되살리면 승인했는데 다시 확인을 묻는 무한 왕복이 된다.
    """

    async def run():
        await seller_period_confirm.save_pending(_CONTEXT, _THREAD, question="q", plan=_PLAN)
        return await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    assert asyncio.run(run()).plan.needs_confirmation is False


def test_pending_absent_by_default_and_after_clear() -> None:
    """대기가 없으면 None — 폐기 후에도 같다(빈 dict 가 곧 '대기 없음')."""

    async def run():
        before = await seller_period_confirm.load_pending(_CONTEXT, _THREAD)
        await seller_period_confirm.save_pending(_CONTEXT, _THREAD, question="q", plan=_PLAN)
        await seller_period_confirm.clear_pending(_CONTEXT, _THREAD)
        return before, await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    before, after = asyncio.run(run())
    assert before is None
    assert after is None


def test_pending_is_scoped_by_seller_id() -> None:
    """네임스페이스에 seller_id 가 들어가 타 판매자 대기를 읽을 수 없다(IDOR 방지).

    threadId 는 FE 가 보내는 값이라 위조될 수 있다 — 신원은 검증된 JWT 에서만 온다.
    """
    other = SellerContext(seller_id=8, brand_id=3)
    assert seller_period_confirm.pending_thread_id(_CONTEXT, _THREAD) != (
        seller_period_confirm.pending_thread_id(other, _THREAD)
    )

    async def run():
        await seller_period_confirm.save_pending(_CONTEXT, _THREAD, question="q", plan=_PLAN)
        return await seller_period_confirm.load_pending(other, _THREAD)

    assert asyncio.run(run()) is None


def test_pending_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 이 지난 대기는 없는 것과 같다 — 조회 시점에 폐기까지 한다(DESIGN §5.1 TTL 경로).

    ttl=0 은 "즉시 만료" 다. 판정이 엄격 부등호(>)면 이 단언이 **시계 분해능에 걸린다** —
    Windows 기본 타이머 틱(~15.6ms)에서는 저장→조회가 같은 틱에 끝나 경과가 정확히 0 이
    되고, 리눅스(µs 분해능)에서만 통과하는 플랫폼 의존 테스트가 된다.
    """

    class _ZeroTtlSettings:
        seller_period_confirm_ttl_minutes = 0

    async def run():
        await seller_period_confirm.save_pending(_CONTEXT, _THREAD, question="q", plan=_PLAN)
        monkeypatch.setattr(seller_period_confirm, "get_settings", lambda: _ZeroTtlSettings())
        first = await seller_period_confirm.load_pending(_CONTEXT, _THREAD)
        return first

    assert asyncio.run(run()) is None


def test_pending_expires_when_elapsed_is_exactly_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """경과가 **정확히 0** 이어도 ttl=0 이면 만료다 — 경계 포함(>=) 회귀 가드.

    시계를 얼려 OS 타이머 분해능을 지운다. 엄격 부등호(>)로 되돌아가면 이 테스트만
    깨지고, 위 test_pending_expires_after_ttl 은 리눅스에서 계속 통과해 회귀를 놓친다
    (실제로 그렇게 Windows 에서만 깨져 있었다 — docs/lessons.md 2026-08-08).
    """
    frozen = dt.datetime(2026, 8, 8, 12, 0, 0, tzinfo=dt.UTC)

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 — stdlib 시그니처 그대로
            return frozen

    class _ZeroTtlSettings:
        seller_period_confirm_ttl_minutes = 0

    monkeypatch.setattr(seller_period_confirm, "datetime", _FrozenDatetime)

    async def run():
        await seller_period_confirm.save_pending(_CONTEXT, _THREAD, question="q", plan=_PLAN)
        monkeypatch.setattr(seller_period_confirm, "get_settings", lambda: _ZeroTtlSettings())
        return await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    assert asyncio.run(run()) is None


def test_pending_survives_within_ttl_with_frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """반대 방향 가드 — 정상 TTL 에서는 경과 0 이 만료가 아니다(>= 가 과잉 만료로 새지 않는다)."""
    frozen = dt.datetime(2026, 8, 8, 12, 0, 0, tzinfo=dt.UTC)

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return frozen

    monkeypatch.setattr(seller_period_confirm, "datetime", _FrozenDatetime)

    async def run():
        await seller_period_confirm.save_pending(_CONTEXT, _THREAD, question="q", plan=_PLAN)
        return await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    assert asyncio.run(run()) is not None


# ── 입구 3경로 (DESIGN §5.1·§5.5) ───────────────────────────────────────────────


def _seed_pending() -> None:
    asyncio.run(
        seller_period_confirm.save_pending(
            _CONTEXT, _THREAD, question="이번 달 매출 분석해줘", plan=_PLAN
        )
    )


def test_confirmation_question_saves_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """확인 필요 결과는 token+done(keep)으로 나가고 대기가 저장된다 — report 이벤트 없음."""

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(
            kind="period_confirmation",
            text="'이번 달' 을 2026-08-01 ~ 2026-08-05 기간으로 보고 분석하겠습니다.",
            resolved=_PLAN,
        )

    monkeypatch.setattr(seller_api, "route_question", _route_analysis)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("이번 달 매출 분석해줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[-1]["data"]["panel"] == "keep"  # 확인은 대화지 보고서가 아니다
    assert asyncio.run(seller_period_confirm.load_pending(_CONTEXT, _THREAD)) is not None


def test_approval_resumes_without_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    """승인 경로 — 저장된 계획으로 즉시 실행하고 planner 를 다시 부르지 않는다.

    #269 완료 조건이자 이 이슈의 핵심 계약이다. run_analysis_pipeline 스텁이 호출되면
    즉시 실패하므로, "재호출 0회"가 주석이 아니라 테스트로 고정된다.
    """
    seen: dict = {}

    async def fake_resolved(question, resolved, context, *, emit):
        seen["question"] = question
        seen["plan"] = resolved
        return PipelineResult(
            kind="report",
            text="이번 달 매출 보고서",
            verified=VerifiedReport(
                "이번 달 매출 보고서", passed=True, attempts=1, last_score=None
            ),
        )

    _seed_pending()
    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", _no_planner_pipeline)
    monkeypatch.setattr(seller_api, "run_resolved_pipeline", fake_resolved)

    events = _collect_seller(_request("응"))

    assert [e["type"] for e in events] == ["meta", "token", "report", "done"]
    assert events[0]["data"]["lane"] == "analysis"
    assert events[-1]["data"]["panel"] == "replace"
    # 승인 발화가 아니라 **원 질문**으로 실행한다 — "응" 을 워커 입력에 쓰면 맥락이 사라진다.
    assert seen["question"] == "이번 달 매출 분석해줘"
    assert seen["plan"].date_from == dt.date(2026, 8, 1)
    # 승인은 1회성 — 소비 후 대기가 남아 있으면 다음 "응" 이 같은 분석을 또 돌린다.
    assert asyncio.run(seller_period_confirm.load_pending(_CONTEXT, _THREAD)) is None


def test_non_approval_discards_pending_and_routes_as_new_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수정·새 질문 경로 — 대기를 폐기하고 평소대로 라우팅한다(수정 전용 파서 없음).

    "아니 7월로" 는 승인 어휘가 아니므로 새 질문이 된다. 직전 확인 문구가 대화 스레드에
    남아 있어 planner 가 맥락을 보고 재계획한다(DESIGN §5.1).
    """
    called: dict = {}

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        called["question"] = question
        return PipelineResult(kind="clarification", text="어느 기간을 말씀하시는 걸까요?")

    _seed_pending()
    monkeypatch.setattr(seller_api, "route_question", _route_analysis)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("아니 7월로"))

    assert called["question"] == "아니 7월로"  # planner 가 새 질문으로 받는다
    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert asyncio.run(seller_period_confirm.load_pending(_CONTEXT, _THREAD)) is None


def test_expired_pending_treats_approval_as_new_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 경로 — 만료된 대기의 "응" 은 승인이 아니라 신규 질문으로 흐른다.

    한참 전 확인 질문에 대한 "응" 이 엉뚱한 기간의 분석을 돌리는 것을 막는다.
    """

    class _ZeroTtlSettings:
        seller_period_confirm_ttl_minutes = 0

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(kind="clarification", text="무엇을 분석할까요?")

    _seed_pending()
    monkeypatch.setattr(seller_period_confirm, "get_settings", lambda: _ZeroTtlSettings())
    monkeypatch.setattr(seller_api, "route_question", _route_analysis)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)
    monkeypatch.setattr(seller_api, "run_resolved_pipeline", _no_resumed)

    events = _collect_seller(_request("응"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[1]["data"]["text"] == "무엇을 분석할까요?"


def test_pending_does_not_intercept_scope_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """①.7 은 승인일 때만 가로챈다 — 도메인 밖 발화는 대기가 있어도 ② scope 로 간다."""
    _seed_pending()
    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_resolved_pipeline", _no_resumed)

    events = _collect_seller(_request("경쟁사 매출 알려줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "refused"


def test_confirm_action_wins_over_pending_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """입구 순서 — ①(HITL 승인 구조화 필드)이 ①.7 보다 앞이다(DESIGN §5.5).

    기간 확인 대기가 떠 있어도 action=="confirm" 요청은 HITL 레인으로 간다.
    """
    _seed_pending()
    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_resolved_pipeline", _no_resumed)

    request = SellerChatRequest(
        session_id="s-1", thread_id=_THREAD, message="", action="confirm", draft_id="d-없음"
    )
    events = _collect_seller(request)

    assert events[0]["data"]["lane"] == "confirm"
    # 기간 대기는 그대로 남는다 — 다른 레인의 요청이 남의 대기를 지우지 않는다.
    assert asyncio.run(seller_period_confirm.load_pending(_CONTEXT, _THREAD)) is not None


async def _no_resumed(question, resolved, context, *, emit):
    raise AssertionError("이 경로에서는 승인 재개를 호출하면 안 된다")


def _route_analysis(question, context, recent_turns=(), screen=None):
    from app.agents.seller.schemas import RouteDecision

    async def _decide():
        return RouteDecision(category="analysis", reason="stub", confidence=0.9)

    return _decide()


# ── 비교(기준) 기간 저장 (#346) ────────────────────────────────────────────────


def test_pending_roundtrip_preserves_comparison_period() -> None:
    """[#346] 비교 기간도 대기에 실린다.

    빠지면 승인 재개가 **대조군 없는 다른 분석**을 돌린다 — 확인 문구에는 두 기간이 다
    적혀 있으므로 판매자는 자기가 승인한 것과 다른 게 돌았다는 사실을 알 수 없다.
    """
    plan = ResolvedPlan(
        analyses=("sales_anomaly",),
        date_from=dt.date(2026, 8, 1),
        date_to=dt.date(2026, 8, 5),
        needs_confirmation=True,
        period_expr="이번 달",
        period_clipped=True,
        comparison_expr="지난달 대비",
        compare_from=dt.date(2026, 7, 1),
        compare_to=dt.date(2026, 7, 5),
    )

    async def run():
        assert await seller_period_confirm.save_pending(
            _CONTEXT, _THREAD, question="지난달 대비 이번 달 매출 분석해줘", plan=plan
        )
        return await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    pending = asyncio.run(run())
    assert pending is not None
    assert pending.plan.comparison_expr == "지난달 대비"
    assert pending.plan.compare_from == dt.date(2026, 7, 1)
    assert pending.plan.compare_to == dt.date(2026, 7, 5)


def test_pending_roundtrip_without_comparison_stays_empty() -> None:
    """비교가 없던 계획은 재개 후에도 비어 있다 — 없던 대조군을 만들어내지 않는다."""

    async def run():
        assert await seller_period_confirm.save_pending(
            _CONTEXT, _THREAD, question="이번 달 매출 분석해줘", plan=_PLAN
        )
        return await seller_period_confirm.load_pending(_CONTEXT, _THREAD)

    pending = asyncio.run(run())
    assert pending is not None
    assert pending.plan.comparison_expr == ""
    assert pending.plan.compare_from is None and pending.plan.compare_to is None
