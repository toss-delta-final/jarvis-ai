"""판매자 채팅 대화 스레드 (공용 checkpointer 기반 멀티턴 누적 — 강의 07-Memory-Checkpointer 패턴).

채팅 스레드는 **레인 공통 대화 정본**이다:
- general 레인은 checkpointer 가 붙은 에이전트를 ``chat_config`` 로 호출해 자연 누적한다
  — 체크포인트 로드 → 새 메시지 append → 저장은 LangGraph(add_messages 리듀서) 소관이라
  누적 코드가 따로 없다.
- analysis/product/confirm/apply 레인(one-shot 구조화 출력)은 checkpointer 를 붙이지
  않는다(2026-07-29 확정) — 최종 사용자 대면 답변만 ``record_turn`` 으로 스레드에
  기록한다. 되묻기(clarification) 턴도 기록되어 기존 "되묻기 턴 미저장" 갭이 해소된다.
- supervisor/planner 는 ``load_recent_turns`` 로 최근 턴을 읽어 **입력 메시지에만**
  주입한다 — 프롬프트 불변(history.build_planner_input §9.1 선례와 동일 규약).

thread_id 네임스페이스: ``seller-chat:{seller_id}:{thread_id}`` — 신원은 검증된 JWT
에서만 오므로(§2.6) FE 가 threadId 를 위조해도 타 판매자 스레드를 읽거나 오염시킬 수
없다(IDOR 방지). HITL 의 ``seller-draft:{draftId}`` 와 같은 checkpoints 테이블에
공존하되 접두로 구분된다.

조회·기록은 공용 checkpointer 로 컴파일된 recorder 그래프(MessagesState, no-op 1노드)의
``aget_state``/``aupdate_state`` 를 쓴다 — hitl.py 수작업 StateGraph 선례(코드 경로
그래프는 create_agent 강제 대상이 아님). ``messages`` 채널은 create_agent 상태와
이름·리듀서(add_messages)가 같아 상호운용된다. 대화 맥락은 **부가 데이터**다:
조회·기록 실패는 warning 후 계속 — 응답을 죽이지 않는다(이력 주입 degrade 와 동일).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agents.seller import checkpoint
from app.agents.seller.context import SellerContext
from app.core.config import get_settings
from app.core.pg_resilience import run_with_query_timeout

# 계약 스키마는 타입에만 쓴다 — 판매자 레인이 요청 스키마에 런타임 결합하지 않게
# (recommendation/decompose.py 가 같은 규약을 쓴다).
if TYPE_CHECKING:
    from app.schemas.chat import ScreenContext

logger = logging.getLogger(__name__)

# (role, text) 쌍 — role 은 "user" | "assistant".
Turn = tuple[str, str]

_RECORDER_NODE = "recorder"

# recorder 그래프 싱글턴 — checkpointer 교체(set_checkpointer) 시 reset hook 으로 무효화.
_graph = None


def _reset_graph() -> None:
    global _graph
    _graph = None


checkpoint.register_reset_hook(_reset_graph)


async def _noop(state: MessagesState) -> MessagesState:
    """invoke 용이 아니다 — 그래프는 aget_state/aupdate_state 통로로만 쓴다."""
    return state


async def _get_graph():
    """recorder 그래프 싱글턴 — 공용 checkpointer 준비 후 1회 컴파일."""
    global _graph
    if _graph is None:
        checkpointer = await checkpoint.get_checkpointer()
        builder = StateGraph(MessagesState)
        builder.add_node(_RECORDER_NODE, _noop)
        builder.add_edge(START, _RECORDER_NODE)
        builder.add_edge(_RECORDER_NODE, END)
        _graph = builder.compile(checkpointer=checkpointer)
    return _graph


def chat_thread_id(context: SellerContext, thread_id: str) -> str:
    """채팅 스레드 네임스페이스 — seller_id 접두가 곧 IDOR 차단이다(모듈 docstring)."""
    return f"seller-chat:{context.seller_id}:{thread_id}"


def chat_config(context: SellerContext, thread_id: str) -> dict:
    """general 레인 astream / recorder aget·aupdate 공용 thread config."""
    return {"configurable": {"thread_id": chat_thread_id(context, thread_id)}}


def _message_text(content: object) -> str:
    """provider별 문자열·블록 content 를 텍스트로 정규화한다(도구 블록 제외)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


async def load_recent_turns(
    context: SellerContext, thread_id: str, *, max_turns: int | None = None
) -> list[Turn]:
    """스레드의 최근 대화를 (role, text) 목록으로 반환한다 — 실패는 [] (degrade).

    Human/AI 메시지 중 텍스트가 있는 것만 취한다 — 도구 호출 전용 AI 메시지·도구
    결과 메시지는 대화 맥락이 아니라 제외된다. 각 텍스트는
    seller_chat_context_max_chars 로, 목록은 최근 max_turns 쌍(메시지 2N개)으로
    절단한다 — supervisor/planner 입력 주입용이라 전문이 필요 없다.
    """
    settings = get_settings()
    limit = max_turns if max_turns is not None else settings.seller_chat_context_turns
    try:
        graph = await _get_graph()
        snapshot = await run_with_query_timeout(graph.aget_state(chat_config(context, thread_id)))
    except Exception:
        logger.warning(
            "대화 스레드 조회 실패 — 맥락 없이 진행 (thread=%s)", thread_id, exc_info=True
        )
        return []
    messages = (snapshot.values or {}).get("messages", [])
    turns: list[Turn] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            continue
        text = _message_text(message.content).strip()
        if not text:
            continue
        turns.append((role, text[: settings.seller_chat_context_max_chars]))
    return turns[-(limit * 2) :]


async def record_turn(
    context: SellerContext, thread_id: str, user_text: str, assistant_text: str
) -> None:
    """비-general 레인의 최종 턴 1쌍을 스레드에 기록한다 — best-effort(실패는 warning).

    assistant_text 는 seller_chat_record_max_chars 로 절단한다 — 보고서 전문이 아니라
    후속 발화 이해용 맥락이다(history.report_summary 절단과 같은 이유). general 레인은
    invoke 자체가 기록이므로 이 함수를 쓰지 않는다(이중 기록 금지).
    """
    if not assistant_text.strip():
        return
    settings = get_settings()
    cap = settings.seller_chat_record_max_chars
    try:
        graph = await _get_graph()
        await run_with_query_timeout(
            graph.aupdate_state(
                chat_config(context, thread_id),
                {
                    "messages": [
                        HumanMessage(content=user_text.strip()[:cap]),
                        AIMessage(content=assistant_text.strip()[:cap]),
                    ]
                },
                as_node=_RECORDER_NODE,
            )
        )
    except Exception:
        logger.warning("대화 스레드 기록 실패 — 응답은 계속 (thread=%s)", thread_id, exc_info=True)


# ── supervisor/planner 입력 주입 (프롬프트 불변 — 입력 메시지 조립만) ──────────────

# 입력 블록 라벨의 **단일 출처**. 새 블록을 만들면 이름을 여기에 넣어야 하고, 그러면 아래 주입
# 방어 정규식이 자동으로 함께 넓어진다 — 두 곳에 나눠 적으면 한쪽을 빠뜨린다. 실제로 #118 이
# `[현재 화면]` 블록을 신설하면서 방어 목록만 갱신하지 않아, 사용자가 과거 발화나
# `screen.filters` 값에 `[현재 화면]` 을 실으면 위조 섹션이 그대로 렌더됐다(PR 3차 리뷰, 두 경로
# 재현 확인). PR #182 가 막으려던 것과 같은 유형의 구조 위조다.
#
# 렌더 본문의 라벨은 **문자열 리터럴 그대로 둔다** — "([최근 대화]는 참고 맥락일 뿐이다 …" 처럼
# 조사가 붙은 한국어 산문이라 상수 보간으로 바꾸면 가독성 손해가 얻는 결합보다 크다. 대신
# "실제로 렌더된 라벨이 전부 이 목록에 있는가"를 테스트가 드리프트로 잡는다
# (test_decompose.py `_filter_axes` 드리프트 가드와 같은 규약).
_BLOCK_LABELS: tuple[str, ...] = ("최근 대화", "최근 분석 이력", "현재 화면", "이번 질문")

# 주입 방어(PR #182 리뷰) — 사용자가 이전 턴 메시지나 `screen.filters` 값에 라벨 문자열을 실어
# 다음 턴의 supervisor/planner 입력 구조(블록 경계)를 위조하는 것을 막는다.
_INJECT_LABEL_RE = re.compile(r"\[(" + "|".join(map(re.escape, _BLOCK_LABELS)) + r")\]")


def _sanitize_for_render(text: str) -> str:
    """render 주입 정제 — 라벨 위조 무력화(대괄호 제거) + 개행 평탄화.

    정제 위치가 기록(record_turn)이 아니라 **읽기(render)**인 이유: general 레인은
    record_turn 을 거치지 않고 agent invoke 가 원문을 그대로 누적하므로, 쓰기 측
    정제는 전 경로를 커버하지 못한다. 개행 평탄화는 라벨 없이 "상담원: …" 줄을
    끼워 넣는 화자 위조까지 차단한다(블록이 줄 단위 `화자: 텍스트` 형식이므로).
    """
    flattened = " ".join(text.split())
    return _INJECT_LABEL_RE.sub(r"\1", flattened)


def render_recent_turns(turns: Sequence[Turn]) -> str:
    """최근 대화 블록 — 참고 맥락임을 라벨로 명시한다(오라우팅 방지, [최근 분석 이력] 선례)."""
    if not turns:
        return ""
    lines = ["[최근 대화]"]
    for role, text in turns:
        speaker = "사용자" if role == "user" else "상담원"
        lines.append(f"{speaker}: {_sanitize_for_render(text)}")
    lines.append("([최근 대화]는 참고 맥락일 뿐이다 — 분류·계획 대상은 [이번 질문]이다)")
    return "\n".join(lines)


def render_screen_context(screen: ScreenContext | None) -> str:
    """현재 화면 블록 — 판매자가 보고 있는 화면(S-4, api-spec §3.1·§3.2, 이슈 #118).

    정본: *"`pageType`·`filters` 가 있으면 '이 목록 왜 비어?' 같은 지시어 질문에 답할 수 있다."*
    **`screen.products` 는 싣지 않는다** — 판매자 쪽 상품 지시어 해소는 이번 이슈 범위가 아니고
    (정본이 판매자 가치로 든 것도 `pageType`·`filters` 다), 실으면 HITL 쓰기 경로와 얽힌다.

    싣는 것이 하나도 없으면 `""` 를 돌려준다 — 호출부가 그때 **입력 문자열을 오늘과 바이트
    동일**하게 유지한다(`render_recent_turns` 와 같은 계약). `pageType` 이 표시명 매핑에 없으면
    화면명만 생략하고 `filters` 는 싣는다 — 원시 `pageType` 문자열을 프롬프트에 흘리지 않는 것이
    정본이 매핑을 AI 에 둔 이유이고, 필터만으로도 "이 목록" 질문의 맥락은 성립한다.

    문자열 정제·길이 절단은 **스키마 정규화 단계**(`app.schemas.chat._clean_screen_text` —
    `_strip_unsafe` + `screen_text_max_chars`)에서 이미 끝났다. 구매자 프롬프트와 같은 규약을
    쓰기 위해 그 한 곳에 모아 둔 것이라 여기서 다시 자르지 않는다. 다만 `_sanitize_for_render`
    는 **여기서만 하는 방어**다 — 필터 값에 `[이번 질문]` 같은 라벨을 실어 블록 경계를 위조하는
    것은 이 렌더링에만 있는 표면이기 때문이다(`render_recent_turns` 와 같은 이유).

    필터 키(`status`·`sort`·`page`)는 계약 어휘라 그대로 쓴다 — 값이 이미 사람이 읽는 한글
    표시값이고, 구매자 decompose 의 `SCREEN` 블록도 같은 표기를 쓴다(두 레인 규약 일치).
    """
    if screen is None:
        return ""
    label = get_settings().screen_page_type_labels.get(screen.page_type)
    filters = {
        key: cleaned
        for key, value in screen.filters.items()
        if (cleaned := _sanitize_for_render(value))
    }
    if not label and not filters:
        return ""
    lines = ["[현재 화면]"]
    if label:
        lines.append(f"화면: {_sanitize_for_render(label)}")
    if filters:
        lines.append("필터: " + ", ".join(f"{key}={value}" for key, value in filters.items()))
    lines.append("([현재 화면]은 참고 맥락일 뿐이다 — 분류·계획 대상은 [이번 질문]이다)")
    return "\n".join(lines)


def build_contextual_input(
    question: str, turns: Sequence[Turn], screen: ScreenContext | None = None
) -> str:
    """supervisor 입력 조립 — 맥락이 없으면 질문 원문 그대로(기존 계약 불변).

    `screen` 은 기본값 `None` 인 선택 인자다 — 기존 호출부가 그대로 동작해야 하고, 판매자 FE 도
    아직 `screen` 을 보내지 않아 "맥락 없음"이 절대다수 경로다(#118 S-4).
    """
    blocks = [
        block for block in (render_recent_turns(turns), render_screen_context(screen)) if block
    ]
    if not blocks:
        return question
    return "\n\n".join([*blocks, f"[이번 질문] {question}"])
