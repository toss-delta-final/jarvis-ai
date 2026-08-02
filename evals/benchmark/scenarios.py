"""버전 관리되는 벤치마크 시나리오를 로딩하고 검증한다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_ALLOWED_ROLES = {"buyer", "seller"}
_ENDPOINTS = {"buyer": "/chat", "seller": "/seller/chat"}


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
    if not isinstance(expected, dict) or expected.get("terminal_event") != "done":
        raise ValueError(f"{source}: expected terminal_event must be done")
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
