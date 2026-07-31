"""수명주기 전환 전 legacy import를 위한 얇은 호환 어댑터.

스케줄러 정본은 ``app.core.session_lifecycle.run_session_context_sweep``이며 이 모듈은
``profile_session_activity``를 touch/claim/finalize하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.session_lifecycle import SessionLifecycleCoordinator


@dataclass(frozen=True)
class IdleSweepResult:
    claimed: int = 0
    accepted: int = 0
    duplicate: int = 0
    retryable: int = 0
    skipped: int = 0


async def run_idle_sweep() -> IdleSweepResult:
    """기존 호출자를 lifecycle 단일 authority sweep으로 위임한다."""
    result = await SessionLifecycleCoordinator().run_session_context_sweep()
    return IdleSweepResult(
        claimed=result.claimed,
        accepted=result.completed,
        retryable=result.retryable,
        skipped=result.skipped,
    )
