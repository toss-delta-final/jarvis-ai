"""실제 모델 평가의 사전·실행 중 예산 gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CALLS_PER_CASE = 3


class BudgetExceeded(RuntimeError):
    """이미 초과한 예산 또는 다음 논리 호출의 call 상한을 알린다."""


@dataclass(frozen=True)
class BudgetLimits:
    """한 run에서 허용하는 호출·토큰·비용 상한."""

    max_calls: int
    max_total_tokens: int
    max_cost_usd: float

    def allows_calls(self, calls: int) -> bool:
        return calls <= self.max_calls


def estimate_calls(*, case_count: int, repeats: int) -> int:
    """decompose·rerank·stream 세 호출 기준 사전 호출 수를 계산한다."""
    return case_count * repeats * CALLS_PER_CASE


def preflight(*, case_count: int, repeats: int, limits: BudgetLimits) -> dict[str, Any]:
    """네트워크를 만들기 전에 호출 수 상한을 판정한다."""
    expected_calls = estimate_calls(case_count=case_count, repeats=repeats)
    return {
        "caseCount": case_count,
        "repeats": repeats,
        "callsPerCase": CALLS_PER_CASE,
        "expectedCalls": expected_calls,
        "maxCalls": limits.max_calls,
        "maxTotalTokens": limits.max_total_tokens,
        "maxCostUsd": limits.max_cost_usd,
        "allowed": limits.allows_calls(expected_calls),
    }


class BudgetTracker:
    """논리 호출을 선점하고 완료 usage로 토큰·비용 상한 상태를 갱신한다."""

    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self.call_count = 0
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self.unknown_token_call_count = 0
        self.unknown_cost_call_count = 0
        self.exceeded_reason: str | None = None

    def reserve(self) -> None:
        """provider 호출 전에 슬롯을 선점해 초과 호출이 밖으로 나가지 않게 한다.

        SDK 내부 재시도는 평가 충실도를 위해 프로덕션 설정을 유지하며 논리 호출 1건으로 센다.
        """
        if self.exceeded_reason is not None:
            raise BudgetExceeded(self.exceeded_reason)
        if self.call_count + 1 > self.limits.max_calls:
            self.exceeded_reason = "maxCallsExceeded"
            raise BudgetExceeded(self.exceeded_reason)
        self.call_count += 1

    def record(self, call: dict[str, Any]) -> None:
        """완료 usage를 누적한다. 원래 provider 예외를 덮지 않도록 절대 raise하지 않는다."""
        input_tokens = call.get("inputTokens")
        output_tokens = call.get("outputTokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            self.total_tokens += input_tokens + output_tokens
        else:
            self.unknown_token_call_count += 1
        cost = call.get("costUsd")
        if isinstance(cost, int | float):
            self.total_cost_usd += float(cost)
        else:
            self.unknown_cost_call_count += 1

        reason: str | None = None
        if self.total_tokens > self.limits.max_total_tokens:
            reason = "maxTotalTokensExceeded"
        elif self.total_cost_usd > self.limits.max_cost_usd:
            reason = "maxCostUsdExceeded"
        if reason is not None:
            self.exceeded_reason = reason

    def snapshot(self) -> dict[str, Any]:
        return {
            "callCount": self.call_count,
            "totalTokens": self.total_tokens,
            "totalCostUsd": self.total_cost_usd,
            "unknownTokenCallCount": self.unknown_token_call_count,
            "unknownCostCallCount": self.unknown_cost_call_count,
            "tokenGateStatus": "unknown" if self.unknown_token_call_count else "passed",
            "costGateStatus": "unknown" if self.unknown_cost_call_count else "passed",
            "budgetExceeded": self.exceeded_reason is not None,
            "budgetExceededReason": self.exceeded_reason,
            "limits": {
                "maxCalls": self.limits.max_calls,
                "maxTotalTokens": self.limits.max_total_tokens,
                "maxCostUsd": self.limits.max_cost_usd,
            },
        }
