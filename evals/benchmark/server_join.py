"""X-Request-Id로 클라이언트 측정과 chat_request 로그를 조인한다."""

from __future__ import annotations

import re
from collections.abc import Mapping
from numbers import Real
from pathlib import Path
from typing import Any

from scripts.aggregate_observability import parse_log_line

_UNKNOWN_NOT_EMITTED = {"value": None, "unknown_reason": "not_emitted_by_server"}
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_MAX_MODELS_IN_REASON = 5


def _price_missing_reason(models: list[str]) -> str:
    """가격 누락 모델을 정렬하고 개수·길이를 제한한 bounded 사유로 만든다."""
    bounded = [model if _MODEL_ID_PATTERN.fullmatch(model) else "unknown" for model in models]
    unique = sorted(set(bounded))
    visible = unique[:_MAX_MODELS_IN_REASON]
    if len(unique) == 1:
        return f"price_missing(model={visible[0]})"
    suffix = f",+{len(unique) - len(visible)}" if len(unique) > len(visible) else ""
    return f"price_missing(models={','.join(visible)}{suffix})"


def apply_price_evidence(
    records: list[dict[str, Any]],
    *,
    input_prices: Mapping[str, float],
    output_prices: Mapping[str, float],
) -> None:
    """모든 사용 모델의 양쪽 단가가 있을 때만 서버 costUsd를 측정값으로 인정한다."""
    for record in records:
        if record.get("server_join") != "joined":
            record["cost_usd"] = None
            record["cost_unknown_reason"] = (
                record.get("server_join_reason") or "server_join_unavailable"
            )
            continue
        models = record.get("model_ids")
        if not isinstance(models, list) or not models:
            record["cost_usd"] = None
            record["cost_unknown_reason"] = "model_ids_not_observed"
            continue
        if not all(isinstance(model, str) and model for model in models):
            record["cost_usd"] = None
            record["cost_unknown_reason"] = "model_ids_invalid"
            continue
        missing = [
            model for model in models if model not in input_prices or model not in output_prices
        ]
        if missing:
            record["cost_usd"] = None
            record["cost_unknown_reason"] = _price_missing_reason(missing)
            continue
        cost = record.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, Real):
            record["cost_usd"] = None
            record["cost_unknown_reason"] = "cost_not_emitted_by_server"
            continue
        record["cost_unknown_reason"] = None


def load_server_records(path: Path) -> dict[str, dict[str, Any]]:
    """chat_request 줄만 파싱해 requestId의 마지막 레코드로 색인한다."""
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as file:
        for line in file:
            record = parse_log_line(line)
            request_id = record.get("requestId") if record else None
            if isinstance(request_id, str) and request_id:
                result[request_id] = record
    return result


def join_records(
    client_records: list[dict[str, Any]], server_records: dict[str, dict[str, Any]] | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """서버 미제공·개별 불일치를 모두 명시적 unknown으로 보존한다."""
    output: list[dict[str, Any]] = []
    measured = sum(record.get("phase") == "measured" for record in client_records)
    joined = 0
    for client_record in client_records:
        record = dict(client_record)
        server = (
            server_records.get(record.get("request_id")) if server_records is not None else None
        )
        if server is None:
            reason = "server_log_not_provided" if server_records is None else "request_id_not_found"
            record.update(
                server_join="unavailable",
                server_join_reason=reason,
                model_ids=None,
                prompt_tokens=None,
                completion_tokens=None,
                cost_usd=None,
                lane=None,
                degraded=None,
                degrade_reason=None,
                error_type=None,
                stream_status=None,
                server_first_event_ms=None,
                server_first_event_unknown_reason="not_in_chat_request_log",
                server_first_text_token_ms=None,
            )
        else:
            if record.get("phase") == "measured":
                joined += 1
            record.update(
                server_join="joined",
                server_join_reason=None,
                model_ids=server.get("model"),
                prompt_tokens=server.get("promptTokens"),
                completion_tokens=server.get("completionTokens"),
                cost_usd=server.get("costUsd"),
                lane=server.get("lane"),
                degraded=server.get("degraded"),
                degrade_reason=server.get("degradeReason"),
                error_type=server.get("errorType"),
                stream_status=server.get("streamStatus"),
                server_first_event_ms=None,
                server_first_event_unknown_reason="not_in_chat_request_log",
                # chat_request latencyFirstToken은 #141의 server_first_text_token_ms다.
                server_first_text_token_ms=server.get("latencyFirstToken"),
            )
        record["provider_ttft_ms"] = None
        record["provider_ttft_unknown_reason"] = "not_in_chat_request_log"
        record["reasoning_tokens"] = dict(_UNKNOWN_NOT_EMITTED)
        record["cache_tokens"] = dict(_UNKNOWN_NOT_EMITTED)
        output.append(record)
    return output, {
        "joined": joined,
        "measured": measured,
        "rate": joined / measured if measured else None,
    }
