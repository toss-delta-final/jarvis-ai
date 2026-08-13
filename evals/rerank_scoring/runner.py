"""Goldenset conversion and paired execution for rerank scoring arms."""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from time import perf_counter

from app.agents.buyer.recommendation.rerank import (
    _SYSTEM,
    _SYSTEM_CODE_ASSISTED,
    _SYSTEM_STRUCTURED_GROUNDING,
    _SYSTEM_STRUCTURED_SCORING,
    rerank,
)
from app.agents.buyer.recommendation.rerank_code_assisted import CodeScoringContext
from app.agents.buyer.recommendation.rerank_grounding import GroundingArm
from app.agents.buyer.recommendation.rerank_scoring import RankingArm
from app.agents.buyer.recommendation.state import extract_json
from app.core.llm import LLMClient, LLMError
from app.schemas.spring import ProductSearchFilters, SpringProduct
from evals.goldenset.schema import GoldenCase
from evals.metrics.metrics import hard_constraint_violations, ndcg_at_k
from evals.metrics.runner import EvaluationFixtures
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.budget import BudgetExceeded
from evals.personalization.fixtures import derive_case_preferences
from evals.personalization.profile_markdown import render_profile_markdown
from evals.rerank_scoring.fakes import ReplayLLM
from evals.rerank_scoring.schema import (
    CaseArmResult,
    RankingCaseInput,
    RankingFailure,
    RankingProbeRun,
    RankingSample,
)
from evals.scoring.hard_filter import HardConstraints, apply_hard_filters

_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")
_ARMS = frozenset({"current", "structured", "hybrid", "code_assisted"})


def _scrub_message(message: str) -> str:
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


def _profile_summary(case: GoldenCase, fixtures: EvaluationFixtures) -> str | None:
    if case.identity.kind == "guest":
        return None
    preferences = derive_case_preferences("clean", case, fixtures)
    if preferences is None:
        return None
    settings = EvaluationSettings()
    return render_profile_markdown(
        preferences,
        max_chars=settings.profile_summary_max_chars,
        strength_bands=settings.personalization_eval_profile_strength_bands,
    )


def build_case_input(case: GoldenCase, fixtures: EvaluationFixtures) -> RankingCaseInput:
    """Build the read-only rerank boundary without opening sealed holdout labels."""

    if case.split != "dev":
        raise ValueError("rerank scoring runner accepts labeled dev cases only")
    fixture = fixtures.search_responses.get(case.search_fixture_id or "")
    if fixture is None:
        raise ValueError(f"{case.case_id}: search fixture가 없습니다")
    products = [
        fixtures.catalog[str(product_id)]
        for product_id in fixture.get("productIds", [])
        if str(product_id) in fixtures.catalog
    ]
    hard = case.hard_constraints
    filtered = apply_hard_filters(
        products,
        HardConstraints(
            price_max=hard.price_max,
            price_min=hard.price_min,
            forbidden_categories=frozenset(hard.forbidden_categories),
            forbidden_product_ids=frozenset(hard.forbidden_product_ids),
            must_exclude_product_ids=frozenset(case.must_exclude_product_ids),
        ),
    )
    candidates = tuple(SpringProduct.model_validate(product) for product in filtered.products)
    return RankingCaseInput(
        case_id=case.case_id,
        query=case.query,
        candidates=candidates,
        search_rank_by_id={product.product_id: rank for rank, product in enumerate(candidates, 1)},
        profile_summary=_profile_summary(case, fixtures),
        relevance_grades=(
            dict(case.relevance_grades)
            if case.test_type == "MFT" and case.case_id not in fixtures.non_discriminative_case_ids
            else {}
        ),
        hard_constraints=hard.model_dump(by_alias=True),
        must_exclude_product_ids=tuple(case.must_exclude_product_ids),
        slices=tuple(case.slices),
    )


class _CaptureLLM:
    def __init__(self, delegate: LLMClient) -> None:
        self.delegate = delegate
        self.raw_response = ""

    async def complete(self, **kwargs: object) -> str:
        self.raw_response = await self.delegate.complete(**kwargs)
        return self.raw_response


def _raw_counts(raw_response: str, candidate_ids: set[int], *, arm: RankingArm) -> tuple[int, int]:
    data = extract_json(raw_response)
    raw = data.get("ranked") if arm == "code_assisted" else data.get("evaluations")
    if not isinstance(raw, list):
        return 0, 0
    valid_ids: list[int] = []
    foreign = 0
    for item in raw:
        product_id = item.get("productId") if isinstance(item, Mapping) else None
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            continue
        if product_id not in candidate_ids:
            foreign += 1
            continue
        valid_ids.append(product_id)
    counts = Counter(valid_ids)
    duplicates = sum(max(0, count - 1) for count in counts.values())
    return foreign, duplicates


def _hard_violation_count(case_input: RankingCaseInput, ranked_ids: Sequence[int]) -> int:
    catalog = {
        str(product.product_id): product.model_dump(by_alias=True)
        for product in case_input.candidates
    }
    return len(
        hard_constraint_violations(
            ranked_ids,
            case_input.hard_constraints,
            case_input.must_exclude_product_ids,
            catalog,
        )
    )


def _code_scoring_context(case_input: RankingCaseInput) -> CodeScoringContext:
    hard = case_input.hard_constraints
    return CodeScoringContext(
        filters=ProductSearchFilters(
            price_min=hard.get("priceMin") if type(hard.get("priceMin")) is int else None,
            price_max=hard.get("priceMax") if type(hard.get("priceMax")) is int else None,
        ),
        search_rank_by_id=case_input.search_rank_by_id,
    )


def _failure(
    *,
    case_input: RankingCaseInput,
    arm: RankingArm,
    order_seed: int,
    repeat: int,
    attempt: int,
    exc: Exception,
    raw_response: str,
    provider_called: bool,
) -> CaseArmResult:
    record = RankingFailure(
        case_id=case_input.case_id,
        arm=arm,
        order_seed=order_seed,
        repeat=repeat,
        attempt=attempt,
        error_type=type(exc).__name__,
        message=_scrub_message(str(exc)),
        full_fallback=bool(raw_response) and isinstance(exc, LLMError),
    )
    return CaseArmResult(
        arm=arm,
        sample=None,
        failure=record,
        raw_response_sha256=(
            hashlib.sha256(raw_response.encode()).hexdigest() if raw_response else ""
        ),
        provider_called=provider_called,
    )


async def _execute_arm(
    case_input: RankingCaseInput,
    llm: LLMClient,
    *,
    arm: RankingArm,
    grounding_arm: GroundingArm,
    expose_max: int,
    order_seed: int,
    repeat: int,
    attempt: int,
    candidates: list[SpringProduct],
    provider_called: bool,
    alpha: float,
    k: int,
) -> tuple[CaseArmResult, str]:
    capture = _CaptureLLM(llm)
    started = perf_counter()
    try:
        result = await rerank(
            capture,
            query=case_input.query,
            candidates=candidates,
            profile_summary=case_input.profile_summary,
            tier="smart",
            expose_max=min(expose_max, len(candidates)),
            grounding_arm=grounding_arm,
            ranking_arm=arm,
            rrf_alpha=alpha,
            rrf_k=k,
            search_rank_by_id=case_input.search_rank_by_id,
            code_scoring_context=_code_scoring_context(case_input),
        )
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - failures are probe data
        return (
            _failure(
                case_input=case_input,
                arm=arm,
                order_seed=order_seed,
                repeat=repeat,
                attempt=attempt,
                exc=exc,
                raw_response=capture.raw_response,
                provider_called=provider_called,
            ),
            capture.raw_response,
        )

    latency_ms = round((perf_counter() - started) * 1000)
    ranked_ids = tuple(product_id for product_id, _ in result.ranked)
    candidate_ids = {product.product_id for product in case_input.candidates}
    foreign, duplicates = (
        _raw_counts(capture.raw_response, candidate_ids, arm=arm) if arm != "current" else (0, 0)
    )
    score_decisions = (
        result.code_assisted_decisions if arm == "code_assisted" else result.ranking_decisions
    )
    invalid = sum(not decision.score_valid for decision in score_decisions)
    valid = len(score_decisions) - invalid
    sample = RankingSample(
        case_id=case_input.case_id,
        arm=arm,
        order_seed=order_seed,
        repeat=repeat,
        attempt=attempt,
        candidate_order=tuple(product.product_id for product in candidates),
        ranked_product_ids=ranked_ids,
        top3_product_ids=ranked_ids[:3],
        top1_product_id=ranked_ids[0] if ranked_ids else None,
        latency_ms=latency_ms,
        raw_response_sha256=hashlib.sha256(capture.raw_response.encode()).hexdigest(),
        provider_called=provider_called,
        ranking_decisions=tuple(result.ranking_decisions),
        grounding_decisions=tuple(result.grounding_decisions),
        relevance_grades=case_input.relevance_grades,
        hard_constraints=case_input.hard_constraints,
        must_exclude_product_ids=case_input.must_exclude_product_ids,
        slices=case_input.slices,
        code_assisted_decisions=tuple(result.code_assisted_decisions),
        foreign_evaluation_count=foreign,
        duplicate_evaluation_count=duplicates,
        invalid_score_count=invalid,
        evaluated_coverage=(valid / len(score_decisions) if score_decisions else 1.0),
        partial_fallback=bool(invalid and valid),
        hard_constraint_violation_count=_hard_violation_count(case_input, ranked_ids),
        ndcg_at_10=ndcg_at_k(ranked_ids, case_input.relevance_grades, 10),
    )
    return (
        CaseArmResult(
            arm=arm,
            sample=sample,
            failure=None,
            raw_response_sha256=sample.raw_response_sha256,
            provider_called=provider_called,
        ),
        capture.raw_response,
    )


def _validated_arms(arms: Sequence[str]) -> tuple[RankingArm, ...]:
    normalized = tuple(arms)
    if not normalized or len(normalized) != len(set(normalized)) or not set(normalized) <= _ARMS:
        raise ValueError("arms must be unique current, structured, hybrid, or code_assisted values")
    return normalized  # type: ignore[return-value]


async def run_case_arms(
    case_input: RankingCaseInput,
    llm: LLMClient,
    *,
    arms: tuple[str, ...],
    grounding_arm: GroundingArm,
    expose_max: int,
    order_seed: int,
    repeat: int = 0,
    attempt: int = 1,
    alpha: float = 0.65,
    k: int = 60,
) -> dict[str, CaseArmResult]:
    """Run requested arms while sharing one provider response across structured/hybrid."""

    resolved_arms = _validated_arms(arms)
    if expose_max <= 0:
        raise ValueError("expose_max must be positive")
    candidates = list(case_input.candidates)
    random.Random(order_seed).shuffle(candidates)
    results: dict[str, CaseArmResult] = {}
    if "current" in resolved_arms:
        current, _ = await _execute_arm(
            case_input,
            llm,
            arm="current",
            grounding_arm=grounding_arm,
            expose_max=expose_max,
            order_seed=order_seed,
            repeat=repeat,
            attempt=attempt,
            candidates=candidates,
            provider_called=True,
            alpha=alpha,
            k=k,
        )
        results["current"] = current

    scored_arms = tuple(arm for arm in resolved_arms if arm in ("structured", "hybrid"))
    if scored_arms:
        owner = "structured" if "structured" in scored_arms else scored_arms[0]
        provider_result, raw_response = await _execute_arm(
            case_input,
            llm,
            arm="structured",
            grounding_arm=grounding_arm,
            expose_max=expose_max,
            order_seed=order_seed,
            repeat=repeat,
            attempt=attempt,
            candidates=candidates,
            provider_called=True,
            alpha=alpha,
            k=k,
        )
        if owner == "structured":
            results["structured"] = provider_result
        elif provider_result.failure is not None:
            results[owner] = replace(
                provider_result,
                arm=owner,
                failure=replace(provider_result.failure, arm=owner),
            )
        else:
            replayed, _ = await _execute_arm(
                case_input,
                ReplayLLM(raw_response),
                arm=owner,
                grounding_arm=grounding_arm,
                expose_max=expose_max,
                order_seed=order_seed,
                repeat=repeat,
                attempt=attempt,
                candidates=candidates,
                provider_called=True,
                alpha=alpha,
                k=k,
            )
            results[owner] = replayed

        for arm in scored_arms:
            if arm == owner:
                continue
            if provider_result.failure is not None:
                results[arm] = replace(
                    provider_result,
                    arm=arm,
                    failure=replace(provider_result.failure, arm=arm),
                    provider_called=False,
                )
                continue
            replayed, _ = await _execute_arm(
                case_input,
                ReplayLLM(raw_response),
                arm=arm,
                grounding_arm=grounding_arm,
                expose_max=expose_max,
                order_seed=order_seed,
                repeat=repeat,
                attempt=attempt,
                candidates=candidates,
                provider_called=False,
                alpha=alpha,
                k=k,
            )
            results[arm] = replayed

    if "code_assisted" in resolved_arms:
        code_assisted, _ = await _execute_arm(
            case_input,
            llm,
            arm="code_assisted",
            grounding_arm=grounding_arm,
            expose_max=expose_max,
            order_seed=order_seed,
            repeat=repeat,
            attempt=attempt,
            candidates=candidates,
            provider_called=True,
            alpha=alpha,
            k=k,
        )
        results["code_assisted"] = code_assisted
    return {arm: results[arm] for arm in resolved_arms}


async def run_input_probe(
    llm: LLMClient,
    *,
    case_inputs: tuple[RankingCaseInput, ...],
    dataset_version: str,
    dataset_hash: str,
    arms: tuple[str, ...],
    repeats: int,
    attempt_multiplier: int,
    order_seeds: tuple[int, ...],
    grounding_arm: GroundingArm = "validated",
    expose_max: int = 9,
    alpha: float = 0.65,
    k: int = 60,
) -> RankingProbeRun:
    """Evaluate prepared rerank inputs with explicit immutable dataset provenance."""

    resolved_arms = _validated_arms(arms)
    if repeats <= 0 or attempt_multiplier <= 0 or not order_seeds:
        raise ValueError("repeats, attempt_multiplier, and order_seeds must be positive")
    if not dataset_version or not dataset_hash:
        raise ValueError("dataset_version and dataset_hash must be non-empty")
    samples: list[RankingSample] = []
    failures: list[RankingFailure] = []
    for case_input in case_inputs:
        for order_seed in order_seeds:
            got = {arm: 0 for arm in resolved_arms}
            attempts = {arm: 0 for arm in resolved_arms}
            max_attempts = repeats * attempt_multiplier
            while active := tuple(
                arm for arm in resolved_arms if got[arm] < repeats and attempts[arm] < max_attempts
            ):
                for arm in active:
                    attempts[arm] += 1
                attempt = max(attempts[arm] for arm in active)
                arm_results = await run_case_arms(
                    case_input,
                    llm,
                    arms=active,
                    grounding_arm=grounding_arm,
                    expose_max=expose_max,
                    order_seed=order_seed,
                    repeat=min(got[arm] for arm in active),
                    attempt=attempt,
                    alpha=alpha,
                    k=k,
                )
                for arm in active:
                    arm_result = arm_results[arm]
                    if arm_result.sample is not None:
                        if arm == "code_assisted":
                            prompt = _SYSTEM_CODE_ASSISTED
                        elif arm in ("structured", "hybrid"):
                            prompt = _SYSTEM_STRUCTURED_SCORING
                        else:
                            prompt = (
                                _SYSTEM
                                if grounding_arm == "current"
                                else _SYSTEM_STRUCTURED_GROUNDING
                            )
                        model_config = getattr(
                            llm,
                            "model_config",
                            {"provider": type(llm).__name__},
                        )
                        samples.append(
                            replace(
                                arm_result.sample,
                                repeat=got[arm],
                                attempt=attempts[arm],
                                dataset_hash=dataset_hash,
                                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                                model_config_json=json.dumps(
                                    model_config,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            )
                        )
                        got[arm] += 1
                    elif arm_result.failure is not None:
                        failures.append(
                            replace(arm_result.failure, attempt=attempts[arm], repeat=got[arm])
                        )
    return RankingProbeRun(
        samples=tuple(samples),
        failures=tuple(failures),
        arms=resolved_arms,
        repeats=repeats,
        order_seeds=order_seeds,
        dataset_version=dataset_version,
        dataset_hash=dataset_hash,
        grounding_arm=grounding_arm,
        alpha=alpha,
        k=k,
    )


async def run_probe(
    llm: LLMClient,
    *,
    cases: tuple[GoldenCase, ...],
    fixtures: EvaluationFixtures,
    arms: tuple[str, ...],
    repeats: int,
    attempt_multiplier: int,
    order_seeds: tuple[int, ...],
    grounding_arm: GroundingArm = "validated",
    expose_max: int = 9,
    alpha: float = 0.65,
    k: int = 60,
) -> RankingProbeRun:
    """Preserve the legacy goldenset entry point over the input-oriented probe."""

    return await run_input_probe(
        llm,
        case_inputs=tuple(build_case_input(case, fixtures) for case in cases),
        dataset_version=str(fixtures.manifest.get("datasetVersion") or "unknown"),
        dataset_hash=str(fixtures.manifest.get("datasetHash") or "unknown"),
        arms=arms,
        repeats=repeats,
        attempt_multiplier=attempt_multiplier,
        order_seeds=order_seeds,
        grounding_arm=grounding_arm,
        expose_max=expose_max,
        alpha=alpha,
        k=k,
    )
