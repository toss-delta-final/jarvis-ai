"""전역 호출 페이서 — 가상 시간으로 RPM·TPM 창을 검증한다 (#260).

실제로 자지 않는다. clock/sleep 을 주입받는 설계라 60초 창 검증이 0초에 끝난다.
"""

from __future__ import annotations

from evals.intent_probe.pacer import GlobalPacer, PacerLimits


class FakeClock:
    """가상 시계 — sleep 이 시간을 앞으로 밀기만 한다."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _pacer(clock: FakeClock, **overrides: object) -> GlobalPacer:
    limits = PacerLimits(**overrides)  # type: ignore[arg-type]
    return GlobalPacer(limits, clock=clock.time, sleep=clock.sleep)


async def test_calls_within_rpm_do_not_wait() -> None:
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=50, max_tpm=10_000_000)
    for _ in range(50):
        await pacer.acquire()
    assert clock.slept == []
    assert clock.now == 0.0


async def test_call_over_rpm_waits_until_window_expires() -> None:
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=50, max_tpm=10_000_000)
    for _ in range(51):
        await pacer.acquire()
    assert len(clock.slept) == 1
    assert clock.now >= 60.0


async def test_no_60s_window_ever_exceeds_max_rpm() -> None:
    # #240: 페이서 없이 돌린 런은 429 로 표본이 비어 폐기됐다. 창 불변식이 그 재발 방지선이다.
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=50, max_tpm=10_000_000)
    for _ in range(200):
        await pacer.acquire()
    granted = pacer.granted_at
    assert len(granted) == 200
    for index, stamp in enumerate(granted):
        in_window = [other for other in granted[: index + 1] if other > stamp - 60.0]
        assert len(in_window) <= 50


async def test_token_budget_binds_before_rpm_when_rpm_is_loose() -> None:
    # 3_100 토큰 × 64콜 = 198.4k < 200k, 65번째가 넘긴다.
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=1_000, max_tpm=200_000, estimated_tokens_per_call=3_100)
    for _ in range(64):
        await pacer.acquire()
    assert clock.slept == []
    await pacer.acquire()
    assert len(clock.slept) == 1


async def test_per_call_token_estimate_overrides_default() -> None:
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=1_000, max_tpm=10_000, estimated_tokens_per_call=1)
    await pacer.acquire(estimated_tokens=9_000)
    assert clock.slept == []
    await pacer.acquire(estimated_tokens=9_000)
    assert len(clock.slept) == 1


async def test_single_oversized_call_does_not_hang() -> None:
    # 한 콜이 TPM 상한보다 크면 영원히 못 지나간다 — 첫 콜은 통과시켜 교착을 막는다.
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=50, max_tpm=1_000)
    await pacer.acquire(estimated_tokens=5_000)
    assert clock.slept == []


async def test_snapshot_reports_waiting_for_the_report() -> None:
    clock = FakeClock()
    pacer = _pacer(clock, max_rpm=2, max_tpm=10_000_000)
    # rpm=2 → 1·2콜은 즉시, 3콜에서 한 번 자고(창이 통째로 비워진다) 4콜은 즉시, 5콜에서 또 한 번.
    for _ in range(5):
        await pacer.acquire()
    snapshot = pacer.snapshot()
    assert snapshot["maxRpm"] == 2
    assert snapshot["acquireCount"] == 5
    assert snapshot["waitCount"] == 2
    assert snapshot["totalWaitS"] > 0
    assert snapshot["maxWaitS"] > 0
