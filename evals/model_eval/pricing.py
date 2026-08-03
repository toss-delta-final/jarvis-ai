"""버전 관리 단가 manifest와 호출 비용·coverage 계산."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PRICING_PATH = Path(__file__).with_name("pricing_manifest.json")


@dataclass(frozen=True)
class PriceBook:
    """모델별 USD/1,000 token 단가."""

    version: str
    entries: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path = DEFAULT_PRICING_PATH) -> "PriceBook":
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = {entry["model"]: dict(entry) for entry in payload["entries"]}
        return cls(version=str(payload["version"]), entries=entries)

    def cost(
        self,
        *,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        entry = self.entries.get(model)
        if entry is None or input_tokens is None or output_tokens is None:
            return None
        return (
            input_tokens * float(entry["inPer1k"]) + output_tokens * float(entry["outPer1k"])
        ) / 1000

    @staticmethod
    def coverage(calls: list[dict[str, Any]]) -> dict[str, float]:
        if not calls:
            return {"costCoverage": 1.0, "tokenCoverage": 1.0}
        cost_known = sum(isinstance(call.get("costUsd"), int | float) for call in calls)
        token_known = sum(
            isinstance(call.get("inputTokens"), int) and isinstance(call.get("outputTokens"), int)
            for call in calls
        )
        return {
            "costCoverage": cost_known / len(calls),
            "tokenCoverage": token_known / len(calls),
        }


def release_coverage_complete(coverage: dict[str, float]) -> bool:
    """release 판정은 token·cost coverage가 모두 정확히 100%여야 한다."""
    return coverage.get("costCoverage") == 1.0 and coverage.get("tokenCoverage") == 1.0
