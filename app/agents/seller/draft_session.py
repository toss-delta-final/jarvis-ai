"""판매자 등록 초안 세션 저장소 (#506, api-spec §3.2 v0.31.0).

이미지 기반 create 초안의 **스레드별 대기 상태** — `thread.py` 의 recorder
그래프 패턴(no-op 1노드 + aget_state/aupdate_state)을 그대로 따른다.

보관하는 것: 진행 중인 create 초안의 원천 재료 전부 —
- draft_id: 최신 초안(수정 턴마다 갱신·이전 것은 hitl.invalidate_draft 로 무효화)
- image_urls / analysis: vision 1회 분석 결과 — **수정 턴에서 재분석하지 않기 위한
  캐시**다(FE 계약 §5.6 — 새 사진을 첨부한 턴에만 재분석).
- changes: 최신 초안의 changes 스냅샷 — 수정 턴에서 [기존 초안] 블록으로 주입되고,
  preview sections `note`(수정 반영 내역) diff 의 기준이 된다.

세 가지 소비처: ① 입구 초안 대기 게이트(수정/승인안내/취소/딴주제 판정의 존재 근거)
② 수정 턴의 초안 원천 복원 ③ 새 draft 발급 시 이전 draftId 무효화 대상 조회.

thread_id 네임스페이스: ``seller-draft-session:{seller_id}:{thread_id}`` —
seller_id 접두가 곧 IDOR 차단이다 — 신원은 검증된 JWT 에서만 온다.

대기 상태는 **부가 데이터**다: 조회·기록 실패는 warning 후 계속한다(응답을 죽이지
않는다). TTL 은 draft 본체와 같은 `seller_draft_ttl_minutes` — FE 의 10분 타이머,
hitl 의 confirm TTL 과 한 값으로 정렬된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.seller import checkpoint
from app.agents.seller.context import SellerContext
from app.core.config import get_settings
from app.core.pg_resilience import run_with_query_timeout

logger = logging.getLogger(__name__)

_RECORDER_NODE = "recorder"

_graph = None


class _SessionState(TypedDict, total=False):
    """대기 스레드 상태 — 빈 dict 가 곧 '대기 없음'이다(키 삭제 대신 덮어쓰기)."""

    pending: dict


def _reset_graph() -> None:
    global _graph
    _graph = None


checkpoint.register_reset_hook(_reset_graph)


async def _noop(state: _SessionState) -> _SessionState:
    """invoke 용이 아니다 — 그래프는 aget_state/aupdate_state 통로로만 쓴다."""
    return state


async def _get_graph():
    global _graph
    if _graph is None:
        checkpointer = await checkpoint.get_checkpointer()
        builder = StateGraph(_SessionState)
        builder.add_node(_RECORDER_NODE, _noop)
        builder.add_edge(START, _RECORDER_NODE)
        builder.add_edge(_RECORDER_NODE, END)
        _graph = builder.compile(checkpointer=checkpointer)
    return _graph


def session_thread_id(context: SellerContext, thread_id: str) -> str:
    """네임스페이스 — seller_id 접두가 곧 IDOR 차단이다(모듈 docstring)."""
    return f"seller-draft-session:{context.seller_id}:{thread_id}"


def _config(context: SellerContext, thread_id: str) -> dict:
    return {"configurable": {"thread_id": session_thread_id(context, thread_id)}}


@dataclass(frozen=True)
class PendingCreate:
    """진행 중인 create 초안 1건 — 수정 턴이 재분석 없이 이어갈 재료 전부."""

    draft_id: str
    image_urls: tuple[str, ...]
    # vision 분석 직렬화형(ProductImageAnalysis.model_dump()) — None 이면 분석 실패/미수행.
    analysis: dict | None
    # 최신 초안 changes 스냅샷: {field: after} — 수정 턴 [기존 초안] 블록·note diff 기준.
    changes: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _dump(pending: PendingCreate) -> dict:
    """checkpoint 저장형 — 내부 저장이라 snake_case(와이어 계약 아님)."""
    return {
        "draft_id": pending.draft_id,
        "image_urls": list(pending.image_urls),
        "analysis": pending.analysis,
        "changes": dict(pending.changes),
        "created_at": pending.created_at.isoformat(),
    }


def _load(data: dict) -> PendingCreate:
    """저장형 → PendingCreate. 형식 불일치는 예외로 올려 호출부가 폐기한다."""
    analysis = data.get("analysis")
    return PendingCreate(
        draft_id=str(data["draft_id"]),
        image_urls=tuple(str(url) for url in data.get("image_urls", [])),
        analysis=analysis if isinstance(analysis, dict) else None,
        changes={str(k): str(v) for k, v in (data.get("changes") or {}).items()},
        created_at=datetime.fromisoformat(data["created_at"]),
    )


async def save_pending(context: SellerContext, thread_id: str, pending: PendingCreate) -> bool:
    """대기를 저장한다 — 스레드당 1건(새 초안은 이전 것을 덮어쓴다). 실패는 False.

    저장 실패 시에도 draft 이벤트는 그대로 나간다 — 후속 수정 턴이 vision 캐시 없이
    새 등록 요청으로 처리될 뿐, 승인·취소(버튼 경로)는 checkpoint 의 draft 본체가
    담당하므로 안전성은 깨지지 않는다(부가 데이터 degrade 원칙).
    """
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
            "등록 초안 세션 저장 실패 — draft 이벤트는 그대로 내보낸다 (thread=%s)",
            thread_id,
            exc_info=True,
        )
        return False


async def load_pending_state(
    context: SellerContext, thread_id: str
) -> tuple[Literal["none", "found", "unknown"], PendingCreate | None]:
    """load_pending 의 3상태 버전(#622) — 조회 실패(unknown)와 정상 없음(none)을 구분한다.

    형식 불일치·TTL 만료는 그대로 "none"이다 — 그건 진짜 "없음"이지 조회 실패가 아니다
    (draft_lifecycle 모듈독스트링 결정과 동일). "unknown"은 쿼리 타임아웃 등 조회 자체가
    실패한 경우뿐이다. TTL 경계는 포함(>=) — 시계 분해능 때문이다(#346, docs/lessons.md).

    `draft_lifecycle.lookup_pending`이 이 함수로 3상태 게이트 판정을 만든다. 이 구분이
    필요 없는 소비처는 `load_pending`(아래, 하위호환 wrapper)을 그대로 쓴다.
    """
    try:
        graph = await _get_graph()
        snapshot = await run_with_query_timeout(graph.aget_state(_config(context, thread_id)))
    except Exception:
        logger.warning(
            "등록 초안 세션 조회 실패 — 상태 불명(unknown) (thread=%s)", thread_id, exc_info=True
        )
        return "unknown", None

    data = (snapshot.values or {}).get("pending") or None
    if not isinstance(data, dict) or not data:
        return "none", None

    try:
        pending = _load(data)
    except Exception:
        logger.warning("등록 초안 세션 형식 불일치 — 폐기 (thread=%s)", thread_id, exc_info=True)
        await clear_pending(context, thread_id)
        return "none", None

    ttl = timedelta(minutes=get_settings().seller_draft_ttl_minutes)
    if datetime.now(UTC) - pending.created_at >= ttl:
        await clear_pending(context, thread_id)
        return "none", None
    return "found", pending


async def load_pending(context: SellerContext, thread_id: str) -> PendingCreate | None:
    """유효한 대기를 반환한다 — 없음·만료·손상·조회 실패는 전부 None(만료·손상은 폐기까지).

    [#622] 조회 실패와 정상 없음을 구분해야 하는 소비처(등록 초안 대기 게이트)는
    `load_pending_state`를 직접 쓴다. 이 함수는 그 구분이 필요 없던 기존 호출부
    (confirm 완료 후 대기 세션 정리 등, `pending is not None` 만 보는 자리) 호환용이다 —
    unknown 도 기존 계약대로 None 으로 뭉갠다.
    """
    _, pending = await load_pending_state(context, thread_id)
    return pending


async def clear_pending(context: SellerContext, thread_id: str) -> None:
    """대기를 폐기한다 — 등록 성공·취소·만료·새 등록 시작 공통. 실패는 warning 후 계속."""
    try:
        graph = await _get_graph()
        await run_with_query_timeout(
            graph.aupdate_state(
                _config(context, thread_id), {"pending": {}}, as_node=_RECORDER_NODE
            )
        )
    except Exception:
        logger.warning(
            "등록 초안 세션 폐기 실패 — 응답은 계속 (thread=%s)", thread_id, exc_info=True
        )
