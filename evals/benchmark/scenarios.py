"""버전 관리되는 벤치마크 시나리오를 로딩하고 검증한다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_ALLOWED_ROLES = {"buyer", "seller"}
_ENDPOINTS = {"buyer": "/chat", "seller": "/seller/chat"}
_ALLOWED_LANES = {
    "recommend",
    "fallback",
    "cart",
    "analysis",
    "product",
    "general",
    "confirm",
    "apply",
    "refused",
}
_ALLOWED_EVENT_TYPES = {
    "token",
    "conditions",
    "action",
    "suggestions",
    "budget",
    "products.ready",
    "draft",
    "done",
    "error",
}


@dataclass(frozen=True)
class Scenario:
    """한 개의 secret-free 벤치마크 요청 정의."""

    id: str
    role: str
    endpoint: str
    description: str
    payload: dict[str, Any]
    expected_outcome: dict[str, Any]
    induced_by: str | None = None


def evaluate_outcome(record: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    """관측 결과를 fixture 기대와 대조하되 미조인 서버 값은 unknown으로 보존한다."""
    expected = scenario.expected_outcome
    mismatches: list[str] = []
    unknowns: list[str] = []

    token_observed = record.get("client_ttft_ms") is not None
    if token_observed != expected["expect_token"]:
        mismatches.append("token_not_observed" if expected["expect_token"] else "token_unexpected")

    terminal = record.get("terminal_event")
    if terminal != expected["terminal_event"]:
        actual = terminal if terminal in _ALLOWED_EVENT_TYPES else "unknown"
        mismatches.append(
            f"terminal_mismatch(expected={expected['terminal_event']},actual={actual})"
        )

    joined = record.get("server_join") == "joined"
    lane = record.get("lane")
    if not joined or not isinstance(lane, str):
        unknowns.append("lane_not_observed")
    elif lane not in expected["expect_lane"]:
        expected_lanes = ",".join(expected["expect_lane"])
        actual_lane = lane if lane in _ALLOWED_LANES else "unknown"
        mismatches.append(f"lane_mismatch(expected=[{expected_lanes}],actual={actual_lane})")

    degraded = record.get("degraded")
    if not joined or not isinstance(degraded, bool):
        unknowns.append("degrade_not_observed")
    elif degraded != expected["expect_degraded"]:
        mismatches.append(
            "degrade_not_observed" if expected["expect_degraded"] else "unexpected_degrade"
        )

    outcome_match: bool | str
    if mismatches:
        outcome_match = False
    elif unknowns:
        outcome_match = "unknown"
    else:
        outcome_match = True
    return {
        "outcome_match": outcome_match,
        "outcome_mismatch_reasons": mismatches,
        "outcome_unknown_reasons": unknowns,
    }


def _parse(item: object, source: Path) -> Scenario:
    """fixture 항목을 엄격한 실행 모델로 변환한다."""
    if not isinstance(item, dict):
        raise ValueError(f"{source}: scenario must be an object")
    required = {"id", "role", "endpoint", "description", "payload", "expected_outcome"}
    missing = required - item.keys()
    if missing:
        raise ValueError(f"{source}: missing fields: {sorted(missing)}")
    role = item["role"]
    if role not in _ALLOWED_ROLES or item["endpoint"] != _ENDPOINTS.get(role):
        raise ValueError(f"{source}: role and endpoint do not match")
    payload = item["payload"]
    expected = item["expected_outcome"]
    if not isinstance(payload, dict) or not {"sessionId", "threadId", "message"} <= payload.keys():
        raise ValueError(f"{source}: invalid payload")
    if (
        not isinstance(expected, dict)
        or expected.get("terminal_event") != "done"
        or not isinstance(expected.get("expect_token"), bool)
        or not isinstance(expected.get("expect_degraded"), bool)
        or not isinstance(expected.get("expect_lane"), list)
        or not expected["expect_lane"]
        or not all(lane in _ALLOWED_LANES for lane in expected["expect_lane"])
    ):
        raise ValueError(f"{source}: invalid expected_outcome")
    serialized = json.dumps(item, ensure_ascii=False).upper()
    if any(marker in serialized for marker in ("API_KEY", "PASSWORD", "AUTHORIZATION")):
        raise ValueError(f"{source}: secret-shaped field is forbidden")
    return Scenario(
        id=str(item["id"]),
        role=str(role),
        endpoint=str(item["endpoint"]),
        description=str(item["description"]),
        payload=dict(payload),
        expected_outcome=dict(expected),
        induced_by=str(item["induced_by"]) if item.get("induced_by") else None,
    )


def load_scenarios(selected: set[str] | None = None) -> list[Scenario]:
    """buyer/seller fixture를 읽고 선택된 시나리오를 원래 순서로 반환한다."""
    scenarios: list[Scenario] = []
    for path in (_FIXTURE_DIR / "buyer.json", _FIXTURE_DIR / "seller.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{path}: root must be an array")
        scenarios.extend(_parse(item, path) for item in raw)
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be unique")
    if selected is None:
        return scenarios
    missing = selected - set(ids)
    if missing:
        raise ValueError(f"unknown scenarios: {','.join(sorted(missing))}")
    return [scenario for scenario in scenarios if scenario.id in selected]
