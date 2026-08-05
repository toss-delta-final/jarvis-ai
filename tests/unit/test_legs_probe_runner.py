"""N 채우기 러너 — ScriptedLLM 으로 dry-run 전체 배관을 검증한다 (#332).

핵심 불변식은 intent_probe 와 같다: **실패는 표본이 아니다.**
"""

from __future__ import annotations

from evals.intent_probe.client import build_probe_llm
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.legs_probe.cli import main
from evals.legs_probe.fakes import ScriptedDecomposeLLM
from evals.legs_probe.loader import build_cells, load_anchor_set
from evals.legs_probe.report import normalized_artifact_bytes
from evals.legs_probe.runner import run_cell, run_probe, unfilled_cells
from evals.model_eval.budget import BudgetExceeded

ANCHORS = load_anchor_set()
CELLS = build_cells(ANCHORS)

ARTIFACT_NAMES = {
    "results.json",
    "run_manifest.json",
    "report.md",
    "samples.csv",
    "cells.csv",
    "failures.csv",
}


async def _no_sleep(seconds: float) -> None:
    return None


def _loose_pacer() -> GlobalPacer:
    return GlobalPacer(PacerLimits(max_rpm=10_000, max_tpm=10_000_000_000))


async def _run_one(llm, *, cell=None, n=8, attempt_multiplier=3, tier="fast"):
    return await run_cell(
        llm=llm,
        cell=cell or CELLS[0],
        n=n,
        tier=tier,
        attempt_multiplier=attempt_multiplier,
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
    """[R2-1] 실제 `RecordingLLM`처럼 처음 `allowed_calls`회는 성공하고 그 뒤로는 항상
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
    """[R2-1] `run_cell` 은 BudgetExceeded 를 잡아 이미 모은 표본을 보존한 채 조용히 멈춘다 —
    예외가 `run_probe`(asyncio.gather)까지 올라가면 다른 셀의 결과가 통째로 사라진다."""
    llm = _BudgetLimitedLLM(ANCHORS, allowed_calls=3)
    result = await _run_one(llm, n=8, attempt_multiplier=3)
    assert len(result.samples) == 3
    assert result.filled is False
    assert result.failures[-1].error_type == "BudgetExceeded"


async def test_run_probe_returns_every_cell_when_budget_runs_out_mid_run() -> None:
    """[R2-1] 예산이 소진돼도 run_probe 는 요청한 셀 전부를 반환한다 — 완주 못 한 셀뿐 아니라
    호출조차 못 받은 셀도 표본 0짜리 CellResult 로 나와야 unfilled_cells 가 39셀 전체를
    빠짐없이 본다(#260 "실패는 표본이 아니다"의 대칭 규약)."""
    llm = _BudgetLimitedLLM(ANCHORS, allowed_calls=5)
    results = await run_probe(
        llm=llm,
        cells=CELLS,
        n=8,
        tier="fast",
        attempt_multiplier=3,
        concurrency=4,
        sleep=_no_sleep,
    )
    assert len(results) == len(CELLS)
    total_samples = sum(len(result.samples) for result in results)
    assert total_samples == 5  # 예산이 허용한 성공 호출 수와 정확히 같다(공유 카운터, 경쟁 없음)
    # 어느 셀도 8개를 채울 만큼 성공 호출을 받지 못했으니 39셀 전부가 못 채운 셀로 나온다.
    unfilled = unfilled_cells(results, n=8)
    assert len(unfilled) == len(CELLS)
    assert {row["cellId"] for row in unfilled} == {cell.cell_id for cell in CELLS}


async def test_every_attempt_passes_the_pacer_including_failures() -> None:
    pacer = _loose_pacer()
    llm = build_probe_llm(ScriptedDecomposeLLM(ANCHORS, fail_first=3), pacer=pacer, system=None)
    result = await _run_one(llm)
    assert result.attempts == 11
    assert len(pacer.granted_at) == 11


async def test_decompose_is_called_with_the_fixed_single_turn_conditions() -> None:
    """[§0] prior_filters·last_recommendations·pending_cart·screen 은 전부 None 이어야 한다."""
    fake = ScriptedDecomposeLLM(ANCHORS)
    await _run_one(fake, n=1)
    user = fake.calls[0]["user"]
    assert "PRIOR_FILTERS: null" in user
    assert "LAST_RECOMMENDATIONS: []" in user
    assert "PENDING_CART: null" in user
    assert "SCREEN" not in user


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
        concurrency=1,
        sleep=_no_sleep,
    )
    parallel = await run_probe(
        llm=ScriptedDecomposeLLM(ANCHORS),
        cells=CELLS,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        concurrency=8,
        sleep=_no_sleep,
    )
    assert [result.cell_id for result in sequential] == [result.cell_id for result in parallel]


# ─────────── CLI dry-run — 전체 배관, CI 실 API 콜 0 ───────────


def _run(out, *extra: str) -> int:
    return main(["--out", str(out), "--dry-run", "--n", "2", *extra])


def _load(out) -> dict:
    import json

    return json.loads((out / "results.json").read_text(encoding="utf-8"))


def test_dry_run_writes_every_artifact_and_exits_zero(tmp_path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    assert {path.name for path in out.iterdir()} == ARTIFACT_NAMES
    results = _load(out)
    assert results["cellCount"] == 39
    assert results["unfilledCells"] == []
    assert results["dryRun"] is True


def test_dry_run_is_deterministic(tmp_path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert _run(first) == 0
    assert _run(second) == 0
    assert normalized_artifact_bytes(first) == normalized_artifact_bytes(second)


def test_existing_out_dir_is_refused(tmp_path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    assert _run(out) == 2


def test_unfilled_cells_are_reported_with_got_and_want() -> None:
    """CLI 의 종료 코드 4 는 이 목록이 비지 않았을 때 켜진다(cli.main 참조) — 못 채운 셀이
    산출물에 드러나는지는 run_cell 이 이미 검증했다(위 test_unfillable_cell_is_reported_not_raised).
    """
    from evals.legs_probe.runner import CellResult

    unfilled = unfilled_cells(
        [
            CellResult(
                cell_id="x", case_id="x", slice="single", samples=[], attempts=3, filled=False
            )
        ],
        n=8,
    )
    assert len(unfilled) == 1
    assert unfilled[0]["cellId"] == "x"
