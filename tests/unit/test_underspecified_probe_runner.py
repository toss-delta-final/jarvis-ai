"""N 채우기 러너 — ScriptedDecomposeLLM 으로 dry-run 전체 배관을 검증한다 (#380).

핵심 불변식은 legs_probe/intent_probe 와 같다: **실패는 표본이 아니다.**
"""

from __future__ import annotations

from app.schemas.spring import ProductSearchFilters
from evals.model_eval.budget import BudgetExceeded
from evals.underspecified_probe.fakes import ScriptedDecomposeLLM
from evals.underspecified_probe.loader import build_cells, load_anchor_set
from evals.underspecified_probe.runner import (
    JudgmentSettings,
    run_cell,
    run_probe,
    unfilled_cells,
)

ANCHORS = load_anchor_set()
CELLS = build_cells(ANCHORS)
JUDGMENT = JudgmentSettings(underspecified_reask_enabled=True)


async def _no_sleep(_seconds: float) -> None:
    return None


async def _run_one(llm, *, cell=None, n=8, attempt_multiplier=3, tier="fast"):
    return await run_cell(
        llm=llm,
        cell=cell or CELLS[0],
        n=n,
        tier=tier,
        attempt_multiplier=attempt_multiplier,
        judgment_settings=JUDGMENT,
        sleep=_no_sleep,
    )


async def test_retries_fill_n_and_failures_never_become_samples() -> None:
    llm = ScriptedDecomposeLLM(ANCHORS, fail_first=3)
    result = await _run_one(llm)
    assert len(result.samples) == 8
    assert result.attempts == 11
    assert len(result.failures) == 3
    assert result.filled is True
    assert [sample.sample_index for sample in result.samples] == list(range(8))


async def test_unfillable_cell_is_reported_not_raised() -> None:
    llm = ScriptedDecomposeLLM(ANCHORS, always_fail=True)
    result = await _run_one(llm, n=8, attempt_multiplier=3)
    assert result.samples == []
    assert result.filled is False
    assert result.attempts == 24
    assert len(result.failures) == 24


class _BudgetLimitedLLM:
    """[R2-1, legs_probe 승계] 처음 `allowed_calls`회는 성공하고 그 뒤로는 항상
    `BudgetExceeded` 를 낸다 — 진행 중 예산이 소진되는 상황을 흉내낸다."""

    def __init__(self, anchors, *, allowed_calls: int) -> None:
        self._inner = ScriptedDecomposeLLM(anchors)
        self._allowed_calls = allowed_calls
        self._calls = 0

    async def complete(self, **kwargs: object) -> str:
        if self._calls >= self._allowed_calls:
            raise BudgetExceeded("maxCallsExceeded")
        self._calls += 1
        return await self._inner.complete(**kwargs)


async def test_budget_exceeded_no_longer_escapes_run_cell() -> None:
    llm = _BudgetLimitedLLM(ANCHORS, allowed_calls=3)
    result = await _run_one(llm, n=8, attempt_multiplier=3)
    assert len(result.samples) == 3
    assert result.filled is False
    assert result.failures[-1].error_type == "BudgetExceeded"


async def test_run_probe_returns_every_cell_when_budget_runs_out_mid_run() -> None:
    """[R2-1] 예산이 소진돼도 run_probe 는 요청한 셀 전부(30개)를 반환한다."""
    llm = _BudgetLimitedLLM(ANCHORS, allowed_calls=5)
    results = await run_probe(
        llm=llm,
        cells=CELLS,
        n=8,
        tier="fast",
        attempt_multiplier=3,
        judgment_settings=JUDGMENT,
        concurrency=4,
        sleep=_no_sleep,
    )
    assert len(results) == len(CELLS)
    total_samples = sum(len(result.samples) for result in results)
    assert total_samples == 5
    unfilled = unfilled_cells(results, n=8)
    assert len(unfilled) == len(CELLS)
    assert {row["cellId"] for row in unfilled} == {cell.cell_id for cell in CELLS}


async def test_decompose_is_called_with_the_fixed_single_turn_conditions_when_no_prior() -> None:
    """[§D2/§D5] priorExists=false 앵커는 decompose·판정 둘 다 prior=None 이어야 한다."""
    no_prior_cell = next(cell for cell in CELLS if not cell.anchor.prior_exists)
    fake = ScriptedDecomposeLLM(ANCHORS)
    result = await _run_one(fake, cell=no_prior_cell, n=1)
    assert result.samples[0].prior is None
    user = fake.calls[0]["user"]
    assert "PRIOR_FILTERS: null" in user
    assert "LAST_RECOMMENDATIONS: []" in user
    assert "PENDING_CART: null" in user
    assert "SCREEN" not in user


async def test_decompose_is_called_with_the_same_prior_as_judgment_when_prior_exists() -> None:
    """[§D5] priorExists=true 앵커는 decompose·판정에 **같은** `ProductSearchFilters()` 를 넘긴다."""
    gate_cell = next(cell for cell in CELLS if cell.anchor.prior_exists)
    fake = ScriptedDecomposeLLM(ANCHORS)
    result = await _run_one(fake, cell=gate_cell, n=1)
    assert result.samples[0].prior == ProductSearchFilters()
    user = fake.calls[0]["user"]
    assert "PRIOR_FILTERS: null" not in user
    # 판정도 항상 False(첫 턴 게이트)로 나와야 한다 — expectedReask=false 슬라이스다.
    assert result.samples[0].verdict is False


async def test_tier_reaches_the_provider() -> None:
    fake = ScriptedDecomposeLLM(ANCHORS)
    await _run_one(fake, n=2, tier="smart")
    assert {call["tier"] for call in fake.calls} == {"smart"}


async def test_run_probe_order_is_independent_of_concurrency() -> None:
    sequential = await run_probe(
        llm=ScriptedDecomposeLLM(ANCHORS),
        cells=CELLS,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        judgment_settings=JUDGMENT,
        concurrency=1,
        sleep=_no_sleep,
    )
    parallel = await run_probe(
        llm=ScriptedDecomposeLLM(ANCHORS),
        cells=CELLS,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        judgment_settings=JUDGMENT,
        concurrency=8,
        sleep=_no_sleep,
    )
    assert [result.cell_id for result in sequential] == [result.cell_id for result in parallel]


def test_unfilled_cells_are_reported_with_got_and_want() -> None:
    from evals.underspecified_probe.runner import CellResult

    unfilled = unfilled_cells(
        [
            CellResult(
                cell_id="x", case_id="x", slice="no_condition", samples=[], attempts=3, filled=False
            )
        ],
        n=8,
    )
    assert len(unfilled) == 1
    assert unfilled[0]["cellId"] == "x"
