"""셀별 N 채우기 루프 — intent_probe 와 같은 원칙: **실패는 표본이 아니다.**

429·타임아웃·파싱 실패는 버리고 다시 호출해 성공 N개를 채운다. 못 채운 셀은 숨기지 않고
산출물에 드러내 종료 코드 4로 알린다(#260 함정 2, 이 프로브도 승계).

[§0] decompose 호출 조건은 고정이다: `prior_filters=None`·`profile_summary=None`·
`last_recommendations=None`·`pending_cart=None`·`screen=None` — 단일 턴만 잰다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.state import CategoryQuery, RouteDecision
from app.core.llm import LLMClient
from evals.legs_probe.loader import Cell
from evals.model_eval.budget import BudgetExceeded

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0

# provider 오류 메시지에 섞여 오는 계정·키 식별자 — intent_probe.runner.scrub_message 와 같은 규약
# (산출물이 리포에 커밋되므로 지우고 남긴다).
_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    """실패 메시지에서 계정 식별자를 지운다 — 원인 판별에 필요한 문구는 남긴다."""
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


@dataclass(frozen=True)
class Sample:
    """성공한 decompose 호출 1건. 실패는 여기 들어오지 않는다.

    leg 그룹 매칭·커버리지·과전개 판정은 여기서 하지 않는다 — `metrics.compute_leg_diagnostics`
    가 (Sample, Anchor) 를 함께 받아 계산한다(정답지가 있어야 판정이 성립하므로).
    """

    case_id: str
    sample_index: int
    intent: str
    case: int
    category_queries: tuple[CategoryQuery, ...]
    buy_all: bool
    total_budget: int | None
    latency_ms: int

    @classmethod
    def from_decision(
        cls, decision: RouteDecision, *, case_id: str, sample_index: int, latency_ms: int
    ) -> "Sample":
        return cls(
            case_id=case_id,
            sample_index=sample_index,
            intent=decision.intent,
            case=decision.case,
            category_queries=tuple(decision.category_queries),
            buy_all=decision.buy_all,
            total_budget=decision.total_budget,
            latency_ms=latency_ms,
        )


@dataclass(frozen=True)
class FailureRecord:
    """버린 시도 1건. 표본이 아니라 '몇 번 만에 채웠나'의 근거다."""

    cell_id: str
    attempt: int
    error_type: str
    message: str


@dataclass
class CellResult:
    """한 셀(앵커 1건)의 측정 결과."""

    cell_id: str
    case_id: str
    slice: str
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    filled: bool = False

    def intent_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sample in self.samples:
            counts[sample.intent] = counts.get(sample.intent, 0) + 1
        return dict(sorted(counts.items()))


def backoff_seconds(failure_count: int) -> float:
    """연속 실패에 붙이는 추가 대기 — 페이서 위에 얹는 보호막이다."""
    return min(BACKOFF_BASE_S * (2 ** max(failure_count - 1, 0)), BACKOFF_MAX_S)


async def run_cell(
    *,
    llm: LLMClient,
    cell: Cell,
    n: int,
    tier: str,
    attempt_multiplier: int,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CellResult:
    """성공 표본 N개를 채울 때까지 재시도한다.

    [R2-1] 예산 초과(`BudgetExceeded`)는 더 이상 밖으로 던지지 않는다 — 잡아서 **지금까지
    모은 CellResult 를 그대로 보존**한 채 이 셀의 재시도만 멈춘다(재시도해도 예산은 그대로라
    즉시 다시 예산 초과가 난다). 예외가 `run_probe`(`asyncio.gather`)까지 올라가면 완주하지
    못한·시작조차 못 한 다른 셀의 결과가 통째로 사라져 "cellCount=0·unfilledCells=[]" 인 거짓
    부분 산출물이 나온다(#260 "실패는 표본이 아니다"의 대칭 위반) — 그래서 이 함수는 항상
    정상 반환하고, 예산 소진 여부는 호출부가 `BudgetTracker.snapshot()` 으로 사후 판정한다.
    """
    result = CellResult(cell_id=cell.cell_id, case_id=cell.anchor.case_id, slice=cell.anchor.slice)
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        try:
            # [§0] 고정 호출 조건 — prior_filters·last_recommendations·pending_cart·screen 은
            # 전부 None, 단일 턴만 잰다.
            decision = await decompose(
                llm,
                query=cell.anchor.utterance,
                prior_filters=None,
                profile_summary=None,
                tier=tier,
                last_recommendations=None,
                pending_cart=None,
                screen=None,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
            )
        except BudgetExceeded as exc:
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            break  # [R2-1] 이 셀은 그만 — 남은 표본은 예산 소진으로 채우지 못했다
        except Exception as exc:  # provider·파싱 실패 전부 — 표본으로 세지 않는다
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            await sleep(backoff_seconds(len(result.failures)))
            continue
        result.samples.append(
            Sample.from_decision(
                decision,
                case_id=cell.anchor.case_id,
                sample_index=len(result.samples),
                latency_ms=int(round((perf_counter() - started) * 1000)),
            )
        )
    result.filled = len(result.samples) == n
    return result


async def run_probe(
    *,
    llm: LLMClient,
    cells: list[Cell],
    n: int,
    tier: str,
    attempt_multiplier: int,
    concurrency: int = 1,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_cell_done: Callable[[CellResult], None] | None = None,
) -> list[CellResult]:
    """모든 셀을 돌린다. 결과는 항상 cellId 정렬이라 동시성이 순서를 바꾸지 않는다."""
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _one(cell: Cell) -> CellResult:
        async with semaphore:
            result = await run_cell(
                llm=llm,
                cell=cell,
                n=n,
                tier=tier,
                attempt_multiplier=attempt_multiplier,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
                sleep=sleep,
            )
        if on_cell_done is not None:
            on_cell_done(result)
        return result

    results = await asyncio.gather(*(_one(cell) for cell in cells))
    return sorted(results, key=lambda result: result.cell_id)


def unfilled_cells(results: list[CellResult], *, n: int) -> list[dict[str, Any]]:
    """못 채운 셀 목록 — 리포트와 종료 코드 4의 근거."""
    return [
        {
            "cellId": result.cell_id,
            "got": len(result.samples),
            "want": n,
            "attempts": result.attempts,
            "errorTypes": sorted({failure.error_type for failure in result.failures}),
        }
        for result in results
        if not result.filled
    ]
