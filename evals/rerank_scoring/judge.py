"""Pure loading, blinding, and analysis helpers for rerank LLM judging."""

from __future__ import annotations

import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agents.buyer.recommendation.rerank import (
    _price_medians,
    _price_tier,
    _rating_tier,
    _review_tier,
)
from app.core.config import get_settings
from app.schemas.spring import SpringProduct
from evals.rerank_holdout_v2.io import load_dataset
from evals.rerank_scoring.judge_schema import (
    BlindPresentation,
    CandidateFact,
    CoordinatorMapping,
    JudgeArm,
    JudgeResponse,
)


@dataclass(frozen=True)
class SourcePair:
    pair_id: str
    case_id: str
    order_seed: int
    repeat: int
    query: str
    profile_summary: str | None
    candidate_product_ids: tuple[int, ...]
    candidates: tuple[SpringProduct, ...]
    rankings: Mapping[JudgeArm, tuple[int, ...]]
    source_response_sha256: Mapping[JudgeArm, str]
    slices: tuple[str, ...]
    identity: str
    stratum: str
    dataset_hash: str


def _json_int_tuple(raw: str, *, field: str) -> tuple[int, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {field} JSON") from exc
    if (
        not isinstance(value, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} must be a list of distinct integers")
    return tuple(value)


def _pair_id(dataset_hash: str, case_id: str, order_seed: int, repeat: int) -> str:
    identity = f"{dataset_hash}\0{case_id}\0{order_seed}\0{repeat}".encode()
    return f"rbj-{hashlib.sha256(identity).hexdigest()[:20]}"


def load_source_pairs(samples_path: Path, *, dataset_root: Path) -> tuple[SourcePair, ...]:
    """Load saved arm outputs while deliberately requesting no ranking labels."""

    if not samples_path.is_file():
        raise ValueError(f"source samples not found: {samples_path}")
    dataset = load_dataset(dataset_root, label_policy="none")
    cases = {case.case_id: case for case in dataset.ranking_cases}
    grouped: dict[tuple[str, int, int], dict[JudgeArm, dict[str, str]]] = {}
    with samples_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "caseId",
            "arm",
            "orderSeed",
            "repeat",
            "candidateOrder",
            "rankedProductIds",
            "rawResponseSha256",
            "datasetHash",
        }
        if reader.fieldnames is None or not required <= set(reader.fieldnames):
            raise ValueError("source samples are missing required columns")
        for row in reader:
            arm = row["arm"]
            if arm not in {"current", "structured"}:
                continue
            case_id = row["caseId"]
            try:
                key = (case_id, int(row["orderSeed"]), int(row["repeat"]))
            except ValueError as exc:
                raise ValueError(f"{case_id}: invalid seed or repeat") from exc
            bucket = grouped.setdefault(key, {})
            typed_arm: JudgeArm = arm  # type: ignore[assignment]
            if typed_arm in bucket:
                raise ValueError(f"duplicate source sample: {key}/{arm}")
            bucket[typed_arm] = row

    source_pairs: list[SourcePair] = []
    for (case_id, order_seed, repeat), rows in sorted(grouped.items()):
        if set(rows) != {"current", "structured"}:
            continue
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"unknown source case: {case_id}")
        dataset_hashes = {row["datasetHash"] for row in rows.values()}
        if dataset_hashes != {dataset.manifest.dataset_hash}:
            raise ValueError(f"{case_id}: source dataset hash mismatch")
        candidate_orders = {
            _json_int_tuple(row["candidateOrder"], field="candidateOrder") for row in rows.values()
        }
        if len(candidate_orders) != 1:
            raise ValueError(f"{case_id}: paired arms used different candidate orders")
        candidate_ids = next(iter(candidate_orders))
        if not candidate_ids or not set(candidate_ids) <= set(case.candidate_product_ids):
            raise ValueError(f"{case_id}: candidate order is outside the dataset case")
        rankings: dict[JudgeArm, tuple[int, ...]] = {}
        response_hashes: dict[JudgeArm, str] = {}
        for arm, row in rows.items():
            ranking = _json_int_tuple(row["rankedProductIds"], field="rankedProductIds")
            if not ranking or not set(ranking) <= set(candidate_ids):
                raise ValueError(f"{case_id}/{arm}: ranking is outside the candidate order")
            response_hash = row["rawResponseSha256"]
            if len(response_hash) != 64 or any(
                ch not in "0123456789abcdef" for ch in response_hash
            ):
                raise ValueError(f"{case_id}/{arm}: invalid raw response hash")
            rankings[arm] = ranking
            response_hashes[arm] = response_hash
        products = tuple(
            SpringProduct.model_validate(dataset.catalog[str(product_id)])
            for product_id in candidate_ids
        )
        source_pairs.append(
            SourcePair(
                pair_id=_pair_id(dataset.manifest.dataset_hash, case_id, order_seed, repeat),
                case_id=case_id,
                order_seed=order_seed,
                repeat=repeat,
                query=case.query,
                profile_summary=case.profile_summary,
                candidate_product_ids=candidate_ids,
                candidates=products,
                rankings=rankings,
                source_response_sha256=response_hashes,
                slices=tuple(case.slices),
                identity=case.identity.kind,
                stratum=case.stratum,
                dataset_hash=dataset.manifest.dataset_hash,
            )
        )
    if not source_pairs:
        raise ValueError("source samples contain no complete current/structured pairs")
    return tuple(source_pairs)


def _candidate_facts(pair: SourcePair) -> tuple[CandidateFact, ...]:
    settings = get_settings()
    medians = _price_medians(list(pair.candidates), None, settings)
    facts = [
        CandidateFact(
            product_id=product.product_id,
            name=product.name,
            brand=product.brand,
            category=product.category,
            price_level=_price_tier(product.price, median_price, settings),
            rating_level=_rating_tier(product, settings),
            review_level=_review_tier(product, settings),
        )
        for product, median_price in zip(pair.candidates, medians, strict=True)
    ]
    return tuple(sorted(facts, key=lambda row: row.product_id))


def _current_is_a(pair_id: str, mapping_seed: int) -> bool:
    digest = hashlib.sha256(f"{mapping_seed}\0{pair_id}".encode()).digest()
    return digest[0] % 2 == 0


def build_presentations(
    pairs: Sequence[SourcePair], *, mapping_seed: int
) -> tuple[tuple[BlindPresentation, ...], tuple[CoordinatorMapping, ...]]:
    presentations: list[BlindPresentation] = []
    mappings: list[CoordinatorMapping] = []
    for pair in pairs:
        first_a: JudgeArm = "current" if _current_is_a(pair.pair_id, mapping_seed) else "structured"
        first_b: JudgeArm = "structured" if first_a == "current" else "current"
        facts = _candidate_facts(pair)
        for orientation, (side_a, side_b) in enumerate(((first_a, first_b), (first_b, first_a))):
            presentation_id = f"{pair.pair_id}-o{orientation}"
            presentations.append(
                BlindPresentation(
                    presentation_id=presentation_id,
                    pair_id=pair.pair_id,
                    query=pair.query,
                    profile_summary=pair.profile_summary or "(없음)",
                    candidates=facts,
                    ranking_a=pair.rankings[side_a],
                    ranking_b=pair.rankings[side_b],
                    orientation=orientation,  # type: ignore[arg-type]
                )
            )
            mappings.append(
                CoordinatorMapping(
                    presentation_id=presentation_id,
                    pair_id=pair.pair_id,
                    orientation=orientation,  # type: ignore[arg-type]
                    case_id=pair.case_id,
                    order_seed=pair.order_seed,
                    repeat=pair.repeat,
                    side_a_arm=side_a,
                    side_b_arm=side_b,
                    slices=pair.slices,
                    identity=pair.identity,  # type: ignore[arg-type]
                    stratum=pair.stratum,
                    source_response_sha256=dict(pair.source_response_sha256),
                )
            )
    return tuple(presentations), tuple(mappings)


def _actual_outcome(response: JudgeResponse, mapping: CoordinatorMapping) -> str:
    if response.verdict.winner == "tie":
        return "tie"
    return mapping.side_a_arm if response.verdict.winner == "A" else mapping.side_b_arm


def _outcome_counts(outcomes: Sequence[str]) -> dict[str, int]:
    counts = Counter(outcomes)
    return {
        "structured": counts["structured"],
        "current": counts["current"],
        "tie": counts["tie"],
        "unstable": counts["unstable"],
    }


def _decisive_summary(outcomes: Sequence[str]) -> dict[str, int | float | None]:
    counts = Counter(outcomes)
    denominator = counts["structured"] + counts["current"]
    return {
        "denominator": denominator,
        "structuredWins": counts["structured"],
        "currentWins": counts["current"],
        "structuredWinRate": counts["structured"] / denominator if denominator else None,
    }


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * probability)
    return ordered[index]


def _clustered_preference_share(
    case_scores: Mapping[str, Sequence[float]], *, seed: int, samples: int
) -> dict[str, object]:
    if samples <= 0:
        raise ValueError("bootstrap_samples must be positive")
    eligible = tuple(sorted(case_scores))
    if not eligible:
        return {"eligibleCaseCount": 0, "value": None, "ci95": [None, None]}
    per_case = {
        case_id: sum(case_scores[case_id]) / len(case_scores[case_id]) for case_id in eligible
    }
    value = sum(per_case.values()) / len(per_case)
    rng = random.Random(seed)
    bootstrap = []
    for _ in range(samples):
        selected = rng.choices(eligible, k=len(eligible))
        bootstrap.append(sum(per_case[case_id] for case_id in selected) / len(selected))
    return {
        "eligibleCaseCount": len(eligible),
        "value": value,
        "ci95": [_percentile(bootstrap, 0.025), _percentile(bootstrap, 0.975)],
        "bootstrapSeed": seed,
        "bootstrapSamples": samples,
        "unit": "caseId",
    }


def _case_outcome(outcomes: Sequence[str], *, incomplete: bool) -> str:
    if incomplete:
        return "incomplete"
    stable = Counter(outcome for outcome in outcomes if outcome != "unstable")
    if not stable:
        return "unstable"
    if stable["structured"] > stable["current"]:
        return "structured"
    if stable["current"] > stable["structured"]:
        return "current"
    return "tie"


def analyze_judgments(
    responses: Sequence[JudgeResponse],
    mappings: Sequence[CoordinatorMapping],
    *,
    bootstrap_seed: int = 631200,
    bootstrap_samples: int = 10_000,
) -> dict[str, object]:
    """Map blinded A/B verdicts back to arms only after swap collection is complete."""

    mapping_by_presentation: dict[str, CoordinatorMapping] = {}
    mappings_by_pair: dict[str, list[CoordinatorMapping]] = defaultdict(list)
    for mapping in mappings:
        if mapping.presentation_id in mapping_by_presentation:
            raise ValueError(f"duplicate mapping: {mapping.presentation_id}")
        mapping_by_presentation[mapping.presentation_id] = mapping
        mappings_by_pair[mapping.pair_id].append(mapping)
    for pair_id, rows in mappings_by_pair.items():
        if sorted(row.orientation for row in rows) != [0, 1]:
            raise ValueError(f"{pair_id}: mappings must contain orientations 0 and 1")

    response_by_presentation: dict[str, JudgeResponse] = {}
    for response in responses:
        if response.presentation_id in response_by_presentation:
            raise ValueError(f"duplicate response: {response.presentation_id}")
        mapping = mapping_by_presentation.get(response.presentation_id)
        if mapping is None or mapping.pair_id != response.pair_id:
            raise ValueError(f"response has no matching mapping: {response.presentation_id}")
        response_by_presentation[response.presentation_id] = response

    complete_pair_rows: list[tuple[CoordinatorMapping, str]] = []
    pair_outcomes: list[str] = []
    incomplete_pairs: list[str] = []
    same_side_a = 0
    same_side_b = 0
    display_counts: Counter[str] = Counter()
    confidence_values: list[float] = []
    for pair_id, pair_mappings in sorted(mappings_by_pair.items()):
        ordered_mappings = sorted(pair_mappings, key=lambda row: row.orientation)
        pair_responses = [
            response_by_presentation.get(mapping.presentation_id) for mapping in ordered_mappings
        ]
        if any(response is None for response in pair_responses):
            incomplete_pairs.append(pair_id)
            continue
        complete_responses = [response for response in pair_responses if response is not None]
        display_winners = [response.verdict.winner for response in complete_responses]
        display_counts.update(display_winners)
        confidence_values.extend(response.verdict.confidence for response in complete_responses)
        if display_winners == ["A", "A"]:
            same_side_a += 1
        elif display_winners == ["B", "B"]:
            same_side_b += 1
        actual = [
            _actual_outcome(response, mapping)
            for response, mapping in zip(complete_responses, ordered_mappings, strict=True)
        ]
        outcome = actual[0] if actual[0] == actual[1] else "unstable"
        pair_outcomes.append(outcome)
        complete_pair_rows.append((ordered_mappings[0], outcome))

    planned_presentations = len(mapping_by_presentation)
    completed_presentations = len(response_by_presentation)
    complete_pairs = len(complete_pair_rows)
    coverage = {
        "plannedPresentations": planned_presentations,
        "completedPresentations": completed_presentations,
        "failedPresentations": planned_presentations - completed_presentations,
        "plannedPairs": len(mappings_by_pair),
        "completePairs": complete_pairs,
        "incompletePairs": len(incomplete_pairs),
    }

    case_pair_outcomes: dict[str, list[str]] = defaultdict(list)
    case_incomplete: set[str] = set()
    case_scores: dict[str, list[float]] = defaultdict(list)
    for mapping, outcome in complete_pair_rows:
        case_pair_outcomes[mapping.case_id].append(outcome)
        if outcome == "structured":
            case_scores[mapping.case_id].append(1.0)
        elif outcome == "current":
            case_scores[mapping.case_id].append(0.0)
        elif outcome == "tie":
            case_scores[mapping.case_id].append(0.5)
    for pair_id in incomplete_pairs:
        case_incomplete.add(mappings_by_pair[pair_id][0].case_id)
    all_case_ids = set(case_pair_outcomes) | case_incomplete
    case_outcomes = [
        _case_outcome(case_pair_outcomes[case_id], incomplete=case_id in case_incomplete)
        for case_id in sorted(all_case_ids)
    ]
    eligible_scores = {
        case_id: scores
        for case_id, scores in case_scores.items()
        if case_id not in case_incomplete and scores
    }

    slice_outcomes: dict[str, list[str]] = defaultdict(list)
    for mapping, outcome in complete_pair_rows:
        for slice_name in mapping.slices:
            slice_outcomes[slice_name].append(outcome)
    slices = {
        name: {
            "completePairs": len(outcomes),
            "pairOutcomes": _outcome_counts(outcomes),
            "decisive": _decisive_summary(outcomes),
        }
        for name, outcomes in sorted(slice_outcomes.items())
        if name != "ranking"
    }

    stable_pair_count = sum(outcome != "unstable" for outcome in pair_outcomes)
    return {
        "schemaVersion": "rerank-blind-analysis-v1",
        "status": "exploratory",
        "coverage": coverage,
        "pairOutcomes": _outcome_counts(pair_outcomes),
        "swapConsistencyRate": stable_pair_count / complete_pairs if complete_pairs else None,
        "decisive": _decisive_summary(pair_outcomes),
        "positionBias": {
            "displaySelectionCounts": {
                "A": display_counts["A"],
                "B": display_counts["B"],
                "tie": display_counts["tie"],
            },
            "sameSideASelections": same_side_a,
            "sameSideBSelections": same_side_b,
            "sameDisplaySideRate": (
                (same_side_a + same_side_b) / complete_pairs if complete_pairs else None
            ),
        },
        "meanConfidence": (
            sum(confidence_values) / len(confidence_values) if confidence_values else None
        ),
        "caseClusteredPreferenceShare": _clustered_preference_share(
            eligible_scores,
            seed=bootstrap_seed,
            samples=bootstrap_samples,
        ),
        "caseOutcomes": {
            "structured": case_outcomes.count("structured"),
            "current": case_outcomes.count("current"),
            "tie": case_outcomes.count("tie"),
            "unstable": case_outcomes.count("unstable"),
            "incomplete": case_outcomes.count("incomplete"),
        },
        "slices": slices,
        "caveat": (
            "Exploratory synthetic judge evidence; not confirmatory population superiority."
        ),
    }
