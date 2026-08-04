"""셀별 N 채우기 루프 — 이 프로브의 심장.

원칙 하나: **실패는 표본이 아니다.** 429·타임아웃·파싱 실패는 버리고 다시 호출해 성공 N개를
채운다. 못 채운 셀은 숨기지 않고 산출물에 드러내 종료 코드 4로 알린다 — 굶은 런이 깨끗한 런으로
위장하면 그 표를 근거로 잘못된 채택 판정이 나온다(#240).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.llm import LLMClient
from evals.intent_probe.loader import Cell, build_context_kwargs
from evals.intent_probe.schema import AnchorSet
from evals.model_eval.budget import BudgetExceeded

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0

# provider 오류 메시지에 섞여 오는 계정·키 식별자. 산출물은 리포에 커밋되므로 지우고 남긴다
# (429 본문에 org id 가 그대로 들어온다 — 실측에서 확인).
_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    """실패 메시지에서 계정 식별자를 지운다 — 원인 판별에 필요한 문구는 남긴다."""
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


@dataclass(frozen=True)
class Sample:
    """성공한 호출 1건. 실패는 여기 들어오지 않는다."""

    cell_id: str
    sample_index: int
    intent: str
    product_id: int | None
    option_id: int | None
    quantity: int | None
    case: int
    scoped_to_previous: bool
    latency_ms: int

    @classmethod
    def from_decision(
        cls, decision: RouteDecision, *, cell_id: str, sample_index: int, latency_ms: int
    ) -> "Sample":
        cart = decision.cart
        return cls(
            cell_id=cell_id,
            sample_index=sample_index,
            intent=decision.intent,
            product_id=cart.product_id if cart else None,
            option_id=cart.option_id if cart else None,
            quantity=cart.quantity if cart else None,
            case=decision.case,
            scoped_to_previous=decision.scoped_to_previous,
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
    """한 셀의 측정 결과."""

    cell_id: str
    utterance_id: str
    context_id: str
    group: str
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
    anchors: AnchorSet,
    n: int,
    tier: str,
    attempt_multiplier: int,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CellResult:
    """성공 표본 N개를 채울 때까지 재시도한다. 예산 초과만 밖으로 던진다."""
    result = CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group=cell.utterance.group,
    )
    context_kwargs = build_context_kwargs(anchors, cell.context)
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        try:
            decision = await decompose(
                llm,
                query=cell.utterance.text,
                profile_summary=None,
                tier=tier,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
                **context_kwargs,
            )
        except BudgetExceeded:
            raise
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
                cell_id=cell.cell_id,
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
    anchors: AnchorSet,
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
                anchors=anchors,
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
