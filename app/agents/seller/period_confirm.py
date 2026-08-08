"""판매자 기간 확인 대기 저장소 (docs/specs/DESIGN-SELLER-PERIOD.md §5, 이슈 #345).

코드가 값을 보충한 기간 해석("이번 달"·"올해"·"최근 3개월")은 곧바로 실행하지 않고
판매자에게 확인받는다. 그 확인을 기다리는 동안 **최초 계획(ResolvedPlan)을 통째로**
여기 저장했다가, 승인 발화가 오면 planner 를 다시 부르지 않고 그대로 재개한다
(#269 완료 조건 — 승인 경로 planner 재호출 0회).

`thread.py` 의 recorder 그래프 패턴을 따른다(no-op 1노드 + aget_state/aupdate_state).
`hitl.py` 의 interrupt/resume 를 쓰지 않는 이유: 재개 대상이 "중단된 그래프"가 아니라
**저장된 값으로 다시 도는 파이프라인**이라, interrupt 의 재개 의미론이 필요 없다.

thread_id 네임스페이스: ``seller-period:{seller_id}:{thread_id}``.
- **키가 threadId 인 이유**: 승인 발화에는 식별자가 실리지 않는다(자유 텍스트 승인 —
  DESIGN §5.2, 와이어 계약 무변경). 후속 요청에서 대기를 복원할 수 있는 키는
  `(seller_id, threadId)` 뿐이다.
- seller_id 접두가 곧 IDOR 차단이다 — 신원은 검증된 JWT 에서만 온다(§2.6).
  FE 가 threadId 를 위조해도 타 판매자의 대기를 읽거나 오염시킬 수 없다.

대기 상태는 **부가 데이터**다: 조회·기록 실패는 warning 후 계속한다. 저장이 실패하면
확인 문구는 그대로 나가고 후속 "응" 은 대기가 없어 신규 질문으로 처리된다(planner 가
대화 맥락으로 재계획) — 응답을 죽이지 않는다(thread.py·history 와 같은 degrade 원칙).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.seller import checkpoint
from app.agents.seller.context import SellerContext
from app.agents.seller.pipeline import ResolvedPlan
from app.agents.seller.schemas import AnalysisType
from app.core.config import get_settings
from app.core.pg_resilience import run_with_query_timeout

logger = logging.getLogger(__name__)

_RECORDER_NODE = "recorder"

# 대기 그래프 싱글턴 — checkpointer 교체(set_checkpointer) 시 reset hook 으로 무효화.
_graph = None


class _PendingState(TypedDict, total=False):
    """대기 스레드 상태 — 빈 dict 가 곧 '대기 없음'이다(키 삭제 대신 덮어쓰기)."""

    pending: dict


def _reset_graph() -> None:
    global _graph
    _graph = None


checkpoint.register_reset_hook(_reset_graph)


async def _noop(state: _PendingState) -> _PendingState:
    """invoke 용이 아니다 — 그래프는 aget_state/aupdate_state 통로로만 쓴다."""
    return state


async def _get_graph():
    """대기 recorder 그래프 싱글턴 — 공용 checkpointer 준비 후 1회 컴파일."""
    global _graph
    if _graph is None:
        checkpointer = await checkpoint.get_checkpointer()
        builder = StateGraph(_PendingState)
        builder.add_node(_RECORDER_NODE, _noop)
        builder.add_edge(START, _RECORDER_NODE)
        builder.add_edge(_RECORDER_NODE, END)
        _graph = builder.compile(checkpointer=checkpointer)
    return _graph


def pending_thread_id(context: SellerContext, thread_id: str) -> str:
    """대기 스레드 네임스페이스 — seller_id 접두가 곧 IDOR 차단이다(모듈 docstring)."""
    return f"seller-period:{context.seller_id}:{thread_id}"


def _config(context: SellerContext, thread_id: str) -> dict:
    return {"configurable": {"thread_id": pending_thread_id(context, thread_id)}}


@dataclass(frozen=True)
class PendingPeriod:
    """확인 대기 1건 — 승인 시 그대로 재개할 재료 전부.

    question 을 함께 들고 있는 이유: 워커 입력·이력 저장이 원 질문을 요구하는데,
    승인 발화("응")는 질문이 아니기 때문이다. 승인 턴에서 원 질문을 복원해 쓴다.
    """

    question: str
    plan: ResolvedPlan
    created_at: datetime


def _dump(pending: PendingPeriod) -> dict:
    """checkpoint 에 넣을 직렬화형 — 내부 저장이라 snake_case(와이어 계약 아님)."""
    plan = pending.plan
    return {
        "question": pending.question,
        "analyses": list(plan.analyses),
        "date_from": plan.date_from.isoformat(),
        "date_to": plan.date_to.isoformat(),
        "wants_chart": plan.wants_chart,
        "period_expr": plan.period_expr,
        "period_clipped": plan.period_clipped,
        # [#346] 비교 기간도 함께 저장한다 — 빠지면 승인 재개가 **대조군 없는 다른 분석**을
        # 돌린다. 확인 문구에는 두 기간이 다 적혀 있으므로 판매자는 어긋남을 알 수 없다.
        "comparison_expr": plan.comparison_expr,
        "compare_from": plan.compare_from.isoformat() if plan.compare_from else None,
        "compare_to": plan.compare_to.isoformat() if plan.compare_to else None,
        "created_at": pending.created_at.isoformat(),
    }


def _optional_date(value: object) -> "date | None":
    """저장형의 선택 날짜 필드 → date. 형식 불일치는 ValueError 로 올려 대기를 폐기시킨다."""
    if value is None:
        return None
    return date.fromisoformat(str(value))


def _load(data: dict) -> PendingPeriod:
    """직렬화형 → PendingPeriod. 형식 불일치는 ValueError/KeyError 로 올려 호출부가 폐기한다."""
    analyses: tuple[AnalysisType, ...] = tuple(data["analyses"])
    plan = ResolvedPlan(
        analyses=analyses,
        date_from=date.fromisoformat(data["date_from"]),
        date_to=date.fromisoformat(data["date_to"]),
        wants_chart=bool(data.get("wants_chart", False)),
        # 재개 시점에는 확인이 끝났으므로 needs_confirmation 을 되살리지 않는다 —
        # 그대로 True 로 두면 승인했는데 다시 확인을 묻는 무한 왕복이 된다.
        needs_confirmation=False,
        period_expr=str(data.get("period_expr", "")),
        period_clipped=bool(data.get("period_clipped", False)),
        comparison_expr=str(data.get("comparison_expr", "")),
        compare_from=_optional_date(data.get("compare_from")),
        compare_to=_optional_date(data.get("compare_to")),
    )
    return PendingPeriod(
        question=str(data["question"]),
        plan=plan,
        created_at=datetime.fromisoformat(data["created_at"]),
    )


async def save_pending(
    context: SellerContext, thread_id: str, *, question: str, plan: ResolvedPlan
) -> bool:
    """확인 대기를 저장한다 — 성공 여부를 돌려준다(실패는 warning 후 False).

    스레드당 대기는 최대 1건이다: 새 확인은 이전 것을 덮어쓴다. 판매자가 확인에
    답하지 않고 다른 질문을 하면 입구 선판정이 먼저 폐기하므로, 여기까지 오는 덮어쓰기는
    "확인 질문이 연달아 두 번 나온" 경우뿐이고 그때는 최신 것만 유효한 게 맞다.
    """
    pending = PendingPeriod(question=question, plan=plan, created_at=datetime.now(UTC))
    try:
        graph = await _get_graph()
        await run_with_query_timeout(
            graph.aupdate_state(
                _config(context, thread_id),
                {"pending": _dump(pending)},
                as_node=_RECORDER_NODE,
            )
        )
        return True
    except Exception:
        logger.warning(
            "기간 확인 대기 저장 실패 — 확인 문구는 그대로 내보내고 후속 발화는 신규 질문으로 "
            "처리된다 (thread=%s)",
            thread_id,
            exc_info=True,
        )
        return False


async def load_pending(context: SellerContext, thread_id: str) -> PendingPeriod | None:
    """유효한 확인 대기를 반환한다 — 없음·만료·손상은 전부 None(만료·손상은 폐기까지).

    TTL 판정을 조회 쪽에 두는 이유: 만료는 "대기가 없는 것"과 같아야 하고, 그 판정이
    입구 선판정 한 곳에서만 일어나야 경로가 갈리지 않는다(DESIGN §5.1 — 3경로).
    """
    try:
        graph = await _get_graph()
        snapshot = await run_with_query_timeout(graph.aget_state(_config(context, thread_id)))
    except Exception:
        logger.warning(
            "기간 확인 대기 조회 실패 — 대기 없음으로 진행 (thread=%s)", thread_id, exc_info=True
        )
        return None

    data = (snapshot.values or {}).get("pending") or None
    if not isinstance(data, dict) or not data:
        return None

    try:
        pending = _load(data)
    except Exception:
        logger.warning("기간 확인 대기 형식 불일치 — 폐기 (thread=%s)", thread_id, exc_info=True)
        await clear_pending(context, thread_id)
        return None

    ttl = timedelta(minutes=get_settings().seller_period_confirm_ttl_minutes)
    if datetime.now(UTC) - pending.created_at > ttl:
        await clear_pending(context, thread_id)
        return None
    return pending


async def clear_pending(context: SellerContext, thread_id: str) -> None:
    """대기를 폐기한다 — 승인 소비·새 질문·만료 공통. 실패는 warning 후 계속."""
    try:
        graph = await _get_graph()
        await run_with_query_timeout(
            graph.aupdate_state(
                _config(context, thread_id), {"pending": {}}, as_node=_RECORDER_NODE
            )
        )
    except Exception:
        logger.warning(
            "기간 확인 대기 폐기 실패 — 응답은 계속 (thread=%s)", thread_id, exc_info=True
        )
