"""Arm/case/repeat execution for rerank-grounding evaluation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter

from app.agents.buyer.recommendation.overall_comment_grounding import (
    FinalRecommendationView,
    OverallGroundingDecision,
    validate_and_render_overall_comment,
)
from app.agents.buyer.recommendation.rerank import rerank
from app.agents.buyer.recommendation.rerank_grounding import (
    CandidateGroundingFacts,
    GroundingArm,
    GroundingDecision,
)
from app.agents.buyer.recommendation.state import extract_json
from app.core.config import get_settings
from app.core.llm import LLMClient
from app.schemas.spring import SpringProduct
from evals.model_eval.budget import BudgetExceeded
from evals.rerank_grounding.metrics import MetricItem, MetricSample, detect_overall_claims
from evals.rerank_grounding.schema import FixtureSet, GroundingCase

_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


class _CaptureLLM:
    def __init__(self, delegate: LLMClient) -> None:
        self.delegate = delegate
        self.system = ""
        self.user = ""
        self.raw_response = ""

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        self.system = system
        self.user = user
        self.raw_response = await self.delegate.complete(
            system=system,
            user=user,
            tier=tier,
            max_tokens=max_tokens,
            json_output=json_output,
        )
        return self.raw_response


@dataclass(frozen=True)
class Sample:
    case_id: str
    test_type: str
    pair_id: str | None
    arm: GroundingArm
    sample_index: int
    latency_ms: int
    ranked_product_ids: tuple[int, ...]
    displayed_rationales: tuple[str, ...]
    raw_response: str
    raw_overall_comment: str
    raw_overall_claims: tuple[Mapping[str, object], ...]
    final_view: FinalRecommendationView
    displayed_overall_comment: str
    detected_overall_claim_codes: tuple[str, ...]
    overall_grounding_decision: OverallGroundingDecision | None
    allowed_overall_claim_codes: tuple[str, ...]
    forbidden_overall_claim_codes: tuple[str, ...]
    grounding_decisions: tuple[GroundingDecision, ...]
    metric_items: tuple[MetricItem, ...]
    validator_downgrade_count: int
    out_of_candidate_id_count: int
    duplicate_id_count: int
    valid_rank_coverage: float
    candidate_count: int
    failure_type: str | None = None

    def as_metric_sample(self) -> MetricSample:
        return MetricSample(
            case_id=self.case_id,
            test_type=self.test_type,
            pair_id=self.pair_id,
            arm=self.arm,
            items=self.metric_items,
            candidate_count=self.candidate_count,
            displayed_overall_comment=self.displayed_overall_comment,
            requested_overall_claim_codes=(
                self.overall_grounding_decision.requested_claim_codes
                if self.overall_grounding_decision is not None
                else ()
            ),
            supported_overall_claim_codes=(
                self.overall_grounding_decision.supported_claim_codes
                if self.overall_grounding_decision is not None
                else ()
            ),
            overall_validator_downgraded=bool(
                self.overall_grounding_decision and self.overall_grounding_decision.downgraded
            ),
            overall_failure_reasons=(
                self.overall_grounding_decision.failure_reasons
                if self.overall_grounding_decision is not None
                else ()
            ),
            allowed_overall_claim_codes=self.allowed_overall_claim_codes,
            forbidden_overall_claim_codes=self.forbidden_overall_claim_codes,
            out_of_candidate_id_count=self.out_of_candidate_id_count,
            duplicate_id_count=self.duplicate_id_count,
            failure_type=self.failure_type,
        )


@dataclass(frozen=True)
class FailureRecord:
    case_id: str
    arm: GroundingArm
    attempt: int
    error_type: str
    message: str


@dataclass
class CellResult:
    case_id: str
    arm: GroundingArm
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    filled: bool = False


@dataclass(frozen=True)
class ProbeRun:
    fixture_version: str
    arms: tuple[GroundingArm, ...]
    repeats: int
    cells: tuple[CellResult, ...]

    def metric_samples(self) -> list[MetricSample]:
        return [sample.as_metric_sample() for cell in self.cells for sample in cell.samples]

    def failures(self) -> list[FailureRecord]:
        return [failure for cell in self.cells for failure in cell.failures]

    def unfilled_cells(self) -> list[dict[str, object]]:
        return [
            {
                "caseId": cell.case_id,
                "arm": cell.arm,
                "got": len(cell.samples),
                "want": self.repeats,
                "attempts": cell.attempts,
            }
            for cell in self.cells
            if not cell.filled
        ]


def _products(case: GroundingCase) -> list[SpringProduct]:
    return [
        SpringProduct(
            product_id=candidate.product_id,
            name=candidate.name,
            price=candidate.price,
            rating=candidate.rating,
            review_count=candidate.review_count,
            category=candidate.category_name,
            brand=candidate.brand,
        )
        for candidate in case.candidates
    ]


def _facts_from_user(user: str) -> dict[int, CandidateGroundingFacts]:
    marker = "CANDIDATES: "
    if marker not in user:
        return {}
    payload = json.loads(user.split(marker, 1)[1])
    if not isinstance(payload, list):
        return {}
    facts: dict[int, CandidateGroundingFacts] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        product_id = item.get("productId")
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            continue
        facts[product_id] = CandidateGroundingFacts(
            product_id=product_id,
            rating_level=str(item.get("ratingLevel") or ""),
            review_level=str(item.get("reviewLevel") or ""),
            price_level=str(item.get("priceLevel") or ""),
        )
    return facts


def _raw_id_counts(raw_response: str, candidate_ids: set[int]) -> tuple[int, int]:
    data = extract_json(raw_response)
    ranked = data.get("ranked")
    if not isinstance(ranked, list):
        return 0, 0
    out_of_candidate = 0
    duplicates = 0
    seen: set[int] = set()
    for item in ranked:
        product_id = item.get("productId") if isinstance(item, dict) else None
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id not in candidate_ids
        ):
            out_of_candidate += 1
            continue
        if product_id in seen:
            duplicates += 1
        seen.add(product_id)
    return out_of_candidate, duplicates


async def _run_attempt(
    *,
    llm: LLMClient,
    case: GroundingCase,
    arm: GroundingArm,
    sample_index: int,
    expose_max: int,
) -> Sample:
    capture = _CaptureLLM(llm)
    products = _products(case)
    started = perf_counter()
    result = await rerank(
        capture,
        query=case.query,
        candidates=products,
        profile_summary=case.profile_summary,
        tier="smart",
        expose_max=min(expose_max, len(case.candidates)),
        need_of=case.need_of,
        per_need=case.per_need,
        grounding_arm=arm,
    )
    latency_ms = round((perf_counter() - started) * 1000)
    facts_by_id = _facts_from_user(capture.user)
    decisions_by_id = {decision.product_id: decision for decision in result.grounding_decisions}
    metric_items = tuple(
        MetricItem(
            product_id=product_id,
            displayed_rationale=rationale,
            facts=facts_by_id[product_id],
            grounding_supported=(
                decisions_by_id[product_id].supported if product_id in decisions_by_id else None
            ),
            validator_downgraded=(
                decisions_by_id[product_id].downgraded if product_id in decisions_by_id else False
            ),
        )
        for product_id, rationale in result.ranked
    )
    candidate_ids = {candidate.product_id for candidate in case.candidates}
    out_of_candidate, duplicates = _raw_id_counts(capture.raw_response, candidate_ids)
    final_view = case.final_view.as_final_view()
    overall_grounding_decision = None
    displayed_overall_comment = result.overall_comment
    if arm != "current":
        overall_grounding_decision = validate_and_render_overall_comment(
            result.overall_claims,
            final_view=final_view,
            products_by_id={product.product_id: product for product in products},
            settings=get_settings(),
        )
        if arm == "validated":
            displayed_overall_comment = overall_grounding_decision.rendered_comment
    return Sample(
        case_id=case.case_id,
        test_type=case.test_type,
        pair_id=case.pair_id,
        arm=arm,
        sample_index=sample_index,
        latency_ms=latency_ms,
        ranked_product_ids=tuple(product_id for product_id, _ in result.ranked),
        displayed_rationales=tuple(rationale for _, rationale in result.ranked),
        raw_response=capture.raw_response,
        raw_overall_comment=result.overall_comment,
        raw_overall_claims=result.overall_claims,
        final_view=final_view,
        displayed_overall_comment=displayed_overall_comment,
        detected_overall_claim_codes=detect_overall_claims(displayed_overall_comment),
        overall_grounding_decision=overall_grounding_decision,
        allowed_overall_claim_codes=case.overall_oracle.allowed_claim_codes,
        forbidden_overall_claim_codes=case.overall_oracle.forbidden_claim_codes,
        grounding_decisions=tuple(result.grounding_decisions),
        metric_items=metric_items,
        validator_downgrade_count=sum(
            decision.downgraded for decision in result.grounding_decisions
        ),
        out_of_candidate_id_count=out_of_candidate,
        duplicate_id_count=duplicates,
        valid_rank_coverage=len(result.ranked) / len(case.candidates),
        candidate_count=len(case.candidates),
    )


async def run_probe(
    *,
    llm: LLMClient,
    fixture: FixtureSet,
    arms: tuple[GroundingArm, ...],
    repeats: int,
    attempt_multiplier: int,
    expose_max: int,
    on_cell_done: Callable[[CellResult], None] | None = None,
) -> ProbeRun:
    if repeats <= 0 or attempt_multiplier <= 0 or expose_max <= 0:
        raise ValueError("repeats, attempt_multiplier, and expose_max must be positive")
    cells: list[CellResult] = []
    for arm in arms:
        for case in fixture.cases:
            cell = CellResult(case_id=case.case_id, arm=arm)
            max_attempts = repeats * attempt_multiplier
            while len(cell.samples) < repeats and cell.attempts < max_attempts:
                cell.attempts += 1
                try:
                    sample = await _run_attempt(
                        llm=llm,
                        case=case,
                        arm=arm,
                        sample_index=len(cell.samples),
                        expose_max=expose_max,
                    )
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001 - failures are probe data
                    cell.failures.append(
                        FailureRecord(
                            case_id=case.case_id,
                            arm=arm,
                            attempt=cell.attempts,
                            error_type=type(exc).__name__,
                            message=scrub_message(str(exc)),
                        )
                    )
                    continue
                cell.samples.append(sample)
            cell.filled = len(cell.samples) == repeats
            cells.append(cell)
            if on_cell_done is not None:
                on_cell_done(cell)
    return ProbeRun(
        fixture_version=fixture.fixture_version,
        arms=arms,
        repeats=repeats,
        cells=tuple(cells),
    )
