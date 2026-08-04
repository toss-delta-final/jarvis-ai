"""전역 호출 페이서 — 실 LLM 반복 측정의 필수 부품 (#260).

없이 돌리면 429 로 표본이 비고, **빈 칸을 오답으로 세면 분포 전체가 거짓**이 된다
(#240 초기 2런이 그렇게 폐기됐다). org 한도는 500 RPM / 200k TPM 이라 **TPM 이 먼저 묶는다**.

토큰 버킷이 아니라 **슬라이딩 윈도**로 만든다 — 버킷은 처음 N콜을 한꺼번에 쏟고 그 뒤 429를
먹는데, 그게 #240 이 기록한 실패 양식이다. 제공자 한도 자체가 60초 롤링 창이므로 창을 그대로
모사하는 편이 정확하다.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# 창 경계에서 부동소수 오차로 되풀이 대기하지 않도록 얹는 여유.
_WAIT_EPSILON_S = 0.001


@dataclass(frozen=True)
class PacerLimits:
    """측정 대상 org 의 레이트 한도. 전부 CLI 로 덮을 수 있고 매니페스트에 기록된다."""

    max_rpm: int = 45
    max_tpm: int = 200_000
    # 프롬프트 ≈3.1k(#240 실측) + max_tokens 800 **예약분**. provider 는 응답 실사용이 아니라
    # 요청이 예약한 상한까지 TPM 으로 센다 — 2026-08-04 fast 기준선 런에서 3.1k 로 잡고
    # 50 rpm 을 돌렸더니 155k 예상과 달리 `Limit 200000, Used 200000` 429 를 78회 먹었다.
    # 재시도가 전부 흡수해 셀은 다 채웠지만(exit 0), 추정이 틀리면 페이서는 이름만 남는다.
    estimated_tokens_per_call: int = 3_900
    window_s: float = 60.0


class GlobalPacer:
    """호출 시각·토큰을 60초 창으로 세어 RPM·TPM 을 동시에 지킨다.

    `clock`/`sleep` 을 주입받으므로 테스트는 가상 시간으로 창 불변식을 0초에 검증한다.
    `asyncio.Lock` 으로 전체를 감싸 동시 실행에서도 **하나의** 전역 게이트로 동작한다.
    """

    def __init__(
        self,
        limits: PacerLimits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limits = limits or PacerLimits()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._window: deque[tuple[float, int]] = deque()
        self._window_tokens = 0
        self.granted_at: list[float] = []
        self._wait_count = 0
        self._total_wait_s = 0.0
        self._max_wait_s = 0.0

    async def acquire(self, *, estimated_tokens: int | None = None) -> None:
        """이번 호출이 창에 들어갈 수 있을 때까지 기다린 뒤 자리를 차지한다."""
        tokens = (
            estimated_tokens
            if estimated_tokens is not None
            else self.limits.estimated_tokens_per_call
        )
        async with self._lock:
            while True:
                now = self._clock()
                self._prune(now)
                if not self._window or self._fits(tokens):
                    break
                oldest_ts = self._window[0][0]
                wait = max(
                    oldest_ts + self.limits.window_s - now + _WAIT_EPSILON_S, _WAIT_EPSILON_S
                )
                await self._sleep(wait)
                self._wait_count += 1
                self._total_wait_s += wait
                self._max_wait_s = max(self._max_wait_s, wait)
            granted = self._clock()
            self._window.append((granted, tokens))
            self._window_tokens += tokens
            self.granted_at.append(granted)

    def snapshot(self) -> dict[str, Any]:
        """리포트에 싣는 페이싱 실측 — 대기가 0이면 페이서가 놀았다는 뜻이다."""
        return {
            "maxRpm": self.limits.max_rpm,
            "maxTpm": self.limits.max_tpm,
            "estimatedTokensPerCall": self.limits.estimated_tokens_per_call,
            "windowS": self.limits.window_s,
            "acquireCount": len(self.granted_at),
            "waitCount": self._wait_count,
            "totalWaitS": round(self._total_wait_s, 3),
            "maxWaitS": round(self._max_wait_s, 3),
        }

    def _prune(self, now: float) -> None:
        cutoff = now - self.limits.window_s
        while self._window and self._window[0][0] <= cutoff:
            _, tokens = self._window.popleft()
            self._window_tokens -= tokens

    def _fits(self, tokens: int) -> bool:
        # 창이 비어 있으면(첫 콜) 한도보다 큰 호출도 통과시킨다 — 아니면 영원히 못 지나간다.
        return (
            len(self._window) < self.limits.max_rpm
            and self._window_tokens + tokens <= self.limits.max_tpm
        )
