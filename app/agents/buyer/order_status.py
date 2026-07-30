"""회원 JWT 신원으로 I-4 주문 상태를 결정적으로 표시하는 buyer route."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.buyer._frames import sse
from app.core.logging import get_logger
from app.core.text import _strip_unsafe
from app.schemas.chat import DoneData, TokenData
from app.schemas.spring import ORDER_STATUS_RECENT
from app.services.spring_client import OrderStatusUnavailableError

ORDER_STATUS_ITEM_DISPLAY_LIMIT = 3
_MAX_MEMBER_ID = 2**63 - 1
_MAX_MEMBER_ID_TEXT = str(_MAX_MEMBER_ID)
_SEOUL = ZoneInfo("Asia/Seoul")

_EMPTY_MESSAGE = "최근 주문 내역이 없어요."
_GUEST_MESSAGE = "주문 상태를 확인하려면 로그인해 주세요."
_SELLER_MESSAGE = "구매자 계정으로 다시 로그인해 주세요."
_IDENTITY_MESSAGE = "회원 정보를 확인할 수 없어요. 다시 로그인해 주세요."
_UPSTREAM_MESSAGE = "주문 상태를 불러오지 못했어요. 잠시 후 다시 시도해 주세요."

logger = get_logger(__name__)


def member_order_identity(identity) -> tuple[int | None, str]:
    """I-4 path에 쓸 signed-BIGINT 회원 ID와 유한 실패 category를 반환한다."""
    if identity.is_guest:
        return None, "guest"
    if identity.seller_id is not None:
        return None, "seller"

    value = identity.user_id
    if value is None:
        return None, "missing_user_id"
    if type(value) is int:
        user_id = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal():
        significant = value.lstrip("0")
        if not significant:
            return None, "invalid_user_id"
        if len(significant) > len(_MAX_MEMBER_ID_TEXT) or (
            len(significant) == len(_MAX_MEMBER_ID_TEXT) and significant > _MAX_MEMBER_ID_TEXT
        ):
            return None, "invalid_user_id"
        # Lexical checks bound conversion to at most 19 digits, avoiding Python's
        # configurable large-decimal conversion limit while retaining leading-zero input.
        user_id = int(significant)
    else:
        return None, "invalid_user_id"
    if not 1 <= user_id <= _MAX_MEMBER_ID:
        return None, "invalid_user_id"
    return user_id, "none"


def _order_date(value: datetime) -> str:
    local = value.astimezone(_SEOUL)
    return f"{local.month}월 {local.day}일"


def format_order_status(summary) -> str:
    """검증된 I-4 summary를 Spring 순서 그대로 최대 3×3 plain text로 표시한다."""
    orders = summary.orders
    if not orders:
        return _EMPTY_MESSAGE

    lines = ["최근 주문 상태예요."]
    for index, order in enumerate(orders[:ORDER_STATUS_RECENT], start=1):
        lines.append(
            f"{index}. {_order_date(order.ordered_at)} · 주문번호 {order.order_id} · "
            f"{_strip_unsafe(order.representative_status)}"
        )
        shown = order.items[:ORDER_STATUS_ITEM_DISPLAY_LIMIT]
        for item in shown:
            lines.append(
                f"- {_strip_unsafe(item.product_name)} — {_strip_unsafe(item.status_text)}"
            )
        hidden = len(order.items) - len(shown)
        if hidden:
            lines.append(f"- 외 {hidden}개")
    return "\n".join(lines)


def _route_record(
    *,
    request_id: str,
    outcome: str,
    error_category: str,
    order_count: int,
    started_at: float,
) -> None:
    record = {
        "event": "order_status_route",
        "requestId": request_id,
        "outcome": outcome,
        "errorCategory": error_category,
        "orderCount": order_count,
        "elapsedMs": max(0, int((time.monotonic() - started_at) * 1000)),
    }
    logger.info(json.dumps(record, ensure_ascii=False))


async def stream_order_status(
    *,
    identity,
    fetch_order_status: Callable,
    request_id: str,
) -> AsyncIterator[str]:
    """I-4를 한 번 호출하고 모든 결과를 정확히 token 하나와 done 하나로 종료한다."""
    if not callable(fetch_order_status):
        raise TypeError("fetch_order_status must be callable")

    started_at = time.monotonic()
    user_id, identity_category = member_order_identity(identity)
    if user_id is None:
        message = {
            "guest": _GUEST_MESSAGE,
            "seller": _SELLER_MESSAGE,
            "missing_user_id": _IDENTITY_MESSAGE,
            "invalid_user_id": _IDENTITY_MESSAGE,
        }[identity_category]
        outcome = "identity_blocked"
        error_category = identity_category
        order_count = 0
    else:
        try:
            summary = await fetch_order_status(user_id)
        except OrderStatusUnavailableError as exc:
            message = _UPSTREAM_MESSAGE
            outcome = "upstream_degraded"
            error_category = exc.category
            order_count = 0
        else:
            order_count = len(summary.orders)
            message = format_order_status(summary)
            outcome = "success" if order_count else "empty"
            error_category = "none"

    _route_record(
        request_id=request_id,
        outcome=outcome,
        error_category=error_category,
        order_count=order_count,
        started_at=started_at,
    )
    yield sse("token", TokenData(text=message).model_dump(by_alias=True))
    yield sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))
