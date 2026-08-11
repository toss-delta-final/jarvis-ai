"""사람 평가 원시 artifact의 선호·rubric·agreement 분석."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.blind_pairwise.design import Assignment, load_assignment_artifact, validate_assignment_plan
from evals.blind_pairwise.preregistration import CONFIG_PATH, load_preregistration, sha256_file, validate_preregistration
from evals.blind_pairwise.schema import DIMENSIONS, PREFERENCES, RawResponse, validate_raw_response

VARIANT_PREFERENCES = ("baseline", "recommendation_v2", "tie", "abstain")
_ORDINAL_VALUES = (1, 2, 3, 4, 5)


def analyze_responses(
    responses: Iterable[RawResponse | Mapping[str, Any]],
    assignments: Iterable[Assignment],
    *,
    llm_judgments: Iterable[Mapping[str, Any]] | None = None,
    confidence: float = 0.95,
    disagreement_limit: int = 10,
) -> dict[str, Any]:
    """assignment artifact와 raw response를 결합해 결정론 분석 결과를 만든다.

    응답이 아직 없거나 목표보다 적어도 분석은 가능한 만큼 산출하되, coverage와
    분모를 명시한다. 실제 사람 응답이 없는 상태를 성공으로 가장하지 않는다.
    """
    assignment_list = list(assignments)
    validate_assignment_plan(assignment_list)
    assignment_map = _assignment_map(assignment_list)
    response_list = [validate_raw_response(row) for row in responses]
    _validate_response_joins(response_list, assignment_map)
    if confidence != 0.95:
        raise ValueError("confidence must be fixed at 0.95")
    if disagreement_limit < 0:
        raise ValueError("disagreement_limit must not be negative")

    preference_counts = Counter({variant: 0 for variant in VARIANT_PREFERENCES})
    raw_preference_counts = Counter({choice: 0 for choice in PREFERENCES})
    by_pair: dict[str, list[tuple[RawResponse, str]]] = defaultdict(list)
    ordinal_values: dict[str, dict[str, list[int]]] = {
        dimension: {variant: [] for variant in ("baseline", "recommendation_v2")}
        for dimension in DIMENSIONS
    }
    tag_counts: Counter[str] = Counter()

    for response in response_list:
        assignment = assignment_map[response.assignment_id]
        preference = _map_preference(response.preference, assignment)
        preference_counts[preference] += 1
        raw_preference_counts[response.preference] += 1
        by_pair[response.pair_id].append((response, preference))
        tag_counts.update(response.disagreement_tags)
        for dimension in DIMENSIONS:
            scores = response.dimension_scores[dimension]
            for label, variant in (
                ("A", assignment.left_variant),
                ("B", assignment.right_variant),
            ):
                score = scores[label]
                if score is not None:
                    ordinal_values[dimension][variant].append(score)

    response_count = len(response_list)
    non_abstain = response_count - preference_counts["abstain"]
    decisive = preference_counts["baseline"] + preference_counts["recommendation_v2"]
    denominators = {
        "responses": response_count,
        "nonAbstain": non_abstain,
        "decisive": decisive,
    }
    preference_intervals = {
        variant: _interval(
            preference_counts[variant], decisive, confidence=confidence
        )
        for variant in ("baseline", "recommendation_v2")
    }
    preference_intervals["tie"] = _interval(
        preference_counts["tie"], non_abstain, confidence=confidence
    )
    preference_intervals["abstain"] = _interval(
        preference_counts["abstain"], response_count, confidence=confidence
    )

    ordinal_distributions = {
        dimension: {
            variant: _ordinal_distribution(
                ordinal_values[dimension][variant], confidence=confidence
            )
            for variant in ("baseline", "recommendation_v2")
        }
        for dimension in DIMENSIONS
    }

    coverage = _coverage(response_list, assignment_list)
    result: dict[str, Any] = {
        "schemaVersion": "blind-pairwise-analysis-v1",
        "status": (
            "HUMAN_INPUT_COMPLETE"
            if coverage["humanInputComplete"]
            else "HUMAN_INPUT_REQUIRED"
        ),
        "confidence": confidence,
        "preferenceCounts": dict(preference_counts),
        "rawPreferenceCounts": dict(raw_preference_counts),
        "denominators": denominators,
        "preferenceIntervals95": preference_intervals,
        "intervalEstimand": {
            "method": "wilson",
            "scope": "descriptive-conditional-response-level",
            "accountsForCrossedPairEvaluatorDependence": False,
            "interpretation": (
                "descriptive conditional response-level interval; crossed pair/evaluator "
                "dependence is not accounted for and it has no population coverage claim"
            ),
        },
        "ordinalDistributions": ordinal_distributions,
        "agreement": _agreement(response_list, assignment_map),
        "disagreementExamples": _disagreement_examples(
            by_pair, assignment_map=assignment_map, limit=disagreement_limit
        ),
        "disagreementTagCounts": dict(sorted(tag_counts.items())),
        "coverage": coverage,
        "caveat": (
            "Exploratory evidence only: these ratings are not generalizable to a population "
            "and must not support population-superiority or production A/B claims."
        ),
    }
    if llm_judgments is not None:
        judge_rows = list(llm_judgments)
        if judge_rows:
            result["llmJudge"] = _llm_judge_result(
                response_list,
                assignment_map,
                judge_rows,
                confidence=confidence,
            )
    return result


def reproduce_analysis(
    raw_path: Path,
    assignment_path: Path,
    *,
    preregistration_path: Path = CONFIG_PATH,
    llm_judge_path: Path | None = None,
) -> dict[str, Any]:
    """raw JSONL과 assignment JSON만으로 동일 분석을 재실행한다."""
    responses = _load_raw_jsonl(raw_path)
    assignments, assignment_metadata = load_assignment_artifact(assignment_path)
    preregistration = load_preregistration(preregistration_path)
    validate_preregistration(preregistration)
    preregistration_sha256 = sha256_file(preregistration_path)
    _verify_assignment_binding(assignment_metadata, preregistration, preregistration_sha256)
    if len({assignment.pair_id for assignment in assignments}) != preregistration["pairCount"]:
        raise ValueError("assignment pair count differs from frozen preregistration")
    judge_rows = _load_raw_jsonl(llm_judge_path) if llm_judge_path is not None else None
    result = analyze_responses(
        responses,
        assignments,
        llm_judgments=judge_rows,
        confidence=preregistration["confidence"],
    )
    result["artifacts"] = {
        "rawSha256": _sha256(raw_path),
        "assignmentsSha256": _sha256(assignment_path),
        "rawPath": raw_path.name,
        "assignmentsPath": assignment_path.name,
    }
    result["provenance"] = {
        "rawSha256": _sha256(raw_path),
        "assignmentSha256": _sha256(assignment_path),
        "pairInputSha256": assignment_metadata["pairInputSha256"],
        "preregistrationSha256": preregistration_sha256,
        "preregistrationPath": preregistration_path.name,
    }
    if llm_judge_path is not None:
        result["provenance"]["llmJudgeSha256"] = _sha256(llm_judge_path)
    return result


def _verify_assignment_binding(
    metadata: Mapping[str, Any], preregistration: Mapping[str, Any], preregistration_sha256: str
) -> None:
    if metadata["preregistrationSha256"] != preregistration_sha256:
        raise ValueError("assignment preregistration hash does not match supplied preregistration")
    expected = {
        "seed": preregistration["seed"],
        "pairCount": preregistration["pairCount"],
        "ratingsPerPair": preregistration["ratingsPerPair"],
        "minimumEligibleEvaluators": preregistration["minimumEligibleEvaluators"],
        "confidence": preregistration["confidence"],
    }
    for key, value in expected.items():
        if metadata[key] != value:
            raise ValueError(f"assignment {key} differs from frozen preregistration")
    if metadata["randomizationAlgorithm"] != preregistration["randomization"]["algorithm"]:
        raise ValueError("assignment randomization algorithm differs from preregistration")


def _load_raw_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid raw JSONL at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"raw JSONL line {line_number} must be an object")
        rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assignment_map(assignments: Sequence[Assignment]) -> dict[str, Assignment]:
    if len({item.assignment_id for item in assignments}) != len(assignments):
        raise ValueError("assignment IDs must be unique")
    if any(item.left_variant == item.right_variant for item in assignments):
        raise ValueError("assignment must map two distinct variants")
    return {item.assignment_id: item for item in assignments}


def _validate_response_joins(
    responses: Sequence[RawResponse], assignment_map: Mapping[str, Assignment]
) -> None:
    seen_response_ids: set[str] = set()
    seen_assignment_ids: set[str] = set()
    seen_pair_evaluators: set[tuple[str, str]] = set()
    for response in responses:
        if response.response_id in seen_response_ids:
            raise ValueError(f"duplicate responseId: {response.response_id}")
        seen_response_ids.add(response.response_id)
        if response.assignment_id in seen_assignment_ids:
            raise ValueError(f"duplicate assignmentId: {response.assignment_id}")
        seen_assignment_ids.add(response.assignment_id)
        assignment = assignment_map.get(response.assignment_id)
        if assignment is None:
            raise ValueError(f"response references unknown assignment: {response.assignment_id}")
        if response.pair_id != assignment.pair_id:
            raise ValueError("response pairId does not match assignment")
        if response.evaluator_id != assignment.evaluator_id:
            raise ValueError("response evaluatorId does not match assignment")
        key = (response.pair_id, response.evaluator_id)
        if key in seen_pair_evaluators:
            raise ValueError("a pair must have independent evaluator responses")
        seen_pair_evaluators.add(key)


def _map_preference(choice: str, assignment: Assignment) -> str:
    if choice == "tie" or choice == "abstain":
        return choice
    if choice == "A":
        return assignment.left_variant
    if choice == "B":
        return assignment.right_variant
    raise ValueError(f"unsupported raw preference: {choice}")


def _coverage(responses: Sequence[RawResponse], assignments: Sequence[Assignment]) -> dict[str, Any]:
    assignment_pairs = {assignment.pair_id for assignment in assignments}
    expected_by_pair = Counter(assignment.pair_id for assignment in assignments)
    expected_evaluators_by_pair = {
        pair_id: {assignment.evaluator_id for assignment in assignments if assignment.pair_id == pair_id}
        for pair_id in assignment_pairs
    }
    observed_by_pair = Counter(response.pair_id for response in responses)
    observed_evaluators_by_pair: dict[str, set[str]] = defaultdict(set)
    for response in responses:
        observed_evaluators_by_pair[response.pair_id].add(response.evaluator_id)
    eligible_evaluators = {assignment.evaluator_id for assignment in assignments}
    target_ratings = 3 if expected_by_pair and set(expected_by_pair.values()) == {3} else 0
    pair_completeness = {
        pair_id: {
            "expected": expected_by_pair[pair_id],
            "observed": observed_by_pair[pair_id],
            "expectedEvaluators": len(expected_evaluators_by_pair[pair_id]),
            "observedDistinctEvaluators": len(observed_evaluators_by_pair[pair_id]),
            "complete": (
                observed_by_pair[pair_id] == expected_by_pair[pair_id]
                and observed_evaluators_by_pair[pair_id] == expected_evaluators_by_pair[pair_id]
            ),
        }
        for pair_id in sorted(assignment_pairs)
    }
    complete_pairs = sum(
        item["complete"] for item in pair_completeness.values()
    )
    return {
        "plannedPairs": len(assignment_pairs),
        "plannedAssignments": len(assignments),
        "eligibleEvaluators": len(eligible_evaluators),
        "observedHumanEvaluators": len({response.evaluator_id for response in responses}),
        "targetRatingsPerPair": target_ratings,
        "observedResponses": len(responses),
        "completePairs": complete_pairs,
        "pairCompleteness": pair_completeness,
        "minimumPlanSatisfied": (
            len(assignment_pairs) >= 20
            and target_ratings >= 3
            and len(eligible_evaluators) >= 5
        ),
        "humanInputComplete": (
            bool(assignments)
            and len(responses) == len(assignments)
            and len({response.evaluator_id for response in responses}) >= 5
            and target_ratings == 3
            and complete_pairs == len(assignment_pairs)
        ),
    }


def _interval(successes: int, denominator: int, *, confidence: float) -> dict[str, Any]:
    if denominator == 0:
        return {
            "numerator": successes,
            "denominator": denominator,
            "low": None,
            "high": None,
            "confidence": confidence,
            "method": "wilson",
        }
    if not 0 <= successes <= denominator:
        raise ValueError("interval numerator must be within denominator")
    z = _normal_quantile((1 + confidence) / 2)
    p = successes / denominator
    denominator_adjusted = 1 + z**2 / denominator
    center = (p + z**2 / (2 * denominator)) / denominator_adjusted
    half = (
        z
        * math.sqrt((p * (1 - p) + z**2 / (4 * denominator)) / denominator)
        / denominator_adjusted
    )
    return {
        "numerator": successes,
        "denominator": denominator,
        "low": max(0.0, center - half),
        "high": min(1.0, center + half),
        "confidence": confidence,
        "method": "wilson",
    }


def _normal_quantile(probability: float) -> float:
    # Python 3.12의 statistics.NormalDist는 외부 통계 의존성 없이 재현 가능하다.
    from statistics import NormalDist

    return NormalDist().inv_cdf(probability)


def _ordinal_distribution(values: Sequence[int], *, confidence: float) -> dict[str, Any]:
    counts = {str(value): values.count(value) for value in _ORDINAL_VALUES}
    denominator = len(values)
    distribution = {
        "counts": counts,
        "denominator": denominator,
        "mean": sum(values) / denominator if denominator else None,
        "median": _median(values),
        "confidence": confidence,
        "scale": {"min": 1, "max": 5, "labels": list(_ORDINAL_VALUES)},
    }
    return distribution


def _median(values: Sequence[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _agreement(
    responses: Sequence[RawResponse], assignment_map: Mapping[str, Assignment]
) -> dict[str, Any]:
    preference_units: dict[str, list[str]] = defaultdict(list)
    ordinal_units: dict[str, dict[str, list[int]]] = {
        dimension: {
            variant: [] for variant in ("baseline", "recommendation_v2")
        }
        for dimension in DIMENSIONS
    }
    ordinal_by_unit: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for response in responses:
        assignment = assignment_map[response.assignment_id]
        if response.preference != "abstain":
            preference_units[response.pair_id].append(
                _map_preference(response.preference, assignment)
            )
        for dimension in DIMENSIONS:
            scores = response.dimension_scores[dimension]
            for label, variant in (
                ("A", assignment.left_variant),
                ("B", assignment.right_variant),
            ):
                score = scores[label]
                if score is not None:
                    ordinal_by_unit[(response.pair_id, dimension, variant)].append(score)
                    ordinal_units[dimension][variant].append(score)
    ordinal_result: dict[str, Any] = {}
    for dimension in DIMENSIONS:
        variant_result: dict[str, Any] = {}
        for variant in ("baseline", "recommendation_v2"):
            units = {
                unit_pair: values
                for (unit_pair, unit_dimension, unit_variant), values in ordinal_by_unit.items()
                if unit_dimension == dimension and unit_variant == variant
            }
            variant_result[variant] = _alpha_payload(units, level="ordinal")
        ordinal_result[dimension] = variant_result
    preference_payload = _alpha_payload(preference_units, level="nominal")
    preference_payload["missingPolicy"] = "abstain-as-missing"
    return {
        "method": "krippendorff-alpha",
        "ordinalDistance": "pooled-marginal-cumulative",
        "preference": preference_payload,
        "dimensions": {"preference": preference_payload},
        "ordinal": ordinal_result,
    }


def _alpha_payload(units: Mapping[str, Sequence[Any]], *, level: str) -> dict[str, Any]:
    usable_units = {
        unit: list(values) for unit, values in units.items() if len(values) >= 2
    }
    values = [value for unit in usable_units.values() for value in unit]
    return {
        "alpha": krippendorff_alpha(usable_units, level=level),
        "level": level,
        "unitCount": len(usable_units),
        "observations": len(values),
    }


def krippendorff_alpha(
    units: Mapping[str, Sequence[Any]], *, level: str = "nominal"
) -> float | None:
    """결측을 허용하는 Krippendorff alpha.

    선호는 nominal, 1–5 rubric 점수는 pooled marginal cumulative ordinal 거리로
    계산한다. 단위 내 관측이 하나뿐이면 observed agreement를 추정할 수 없어
    ``None``을 반환하며, 해당 singleton 값은 categories와 pooled expected margins에서도
    제외한다.
    """
    if level not in {"nominal", "ordinal", "interval"}:
        raise ValueError("level must be nominal, ordinal, or interval")
    observations = [list(values) for values in units.values() if len(values) >= 2]
    if not observations:
        return None
    all_values = [value for values in observations for value in values]
    categories = list(dict.fromkeys(all_values))
    if len(all_values) < 2:
        return None
    pooled = Counter(all_values)

    observed_disagreement = 0.0
    coincidence_total = 0.0
    for values in observations:
        counts = Counter(values)
        n = len(values)
        for left in counts:
            for right in counts:
                coincidence = counts[left] * (counts[right] - (1 if left == right else 0))
                if n > 1:
                    coincidence /= n - 1
                observed_disagreement += coincidence * _distance(
                    left, right, categories, level, pooled
                )
                coincidence_total += coincidence
    if coincidence_total == 0:
        return None
    observed_disagreement /= coincidence_total

    expected_disagreement = 0.0
    expected_total = 0.0
    n_total = len(all_values)
    for left in pooled:
        for right in pooled:
            coincidence = pooled[left] * (pooled[right] - (1 if left == right else 0))
            if n_total > 1:
                coincidence /= n_total - 1
            expected_disagreement += coincidence * _distance(
                left, right, categories, level, pooled
            )
            expected_total += coincidence
    if expected_total == 0:
        return 1.0 if observed_disagreement == 0 else 0.0
    expected_disagreement /= expected_total
    if expected_disagreement == 0:
        return 1.0 if observed_disagreement == 0 else 0.0
    return 1 - observed_disagreement / expected_disagreement


def _distance(
    left: Any,
    right: Any,
    categories: Sequence[Any],
    level: str,
    marginals: Mapping[Any, int],
) -> float:
    if level == "nominal":
        return 0.0 if left == right else 1.0
    if level == "interval" and isinstance(left, int | float) and isinstance(right, int | float):
        return float(left - right) ** 2
    ordered = sorted(categories, key=_ordinal_sort_key)
    if left == right:
        return 0.0
    low, high = sorted((ordered.index(left), ordered.index(right)))
    cumulative_margin = sum(marginals[value] for value in ordered[low : high + 1])
    endpoint_margin = (marginals[left] + marginals[right]) / 2
    return float(cumulative_margin - endpoint_margin) ** 2


def _ordinal_sort_key(value: Any) -> tuple[int, str]:
    if isinstance(value, int):
        return (value, "")
    try:
        return (int(value), str(value))
    except (TypeError, ValueError):
        return (0, str(value))


def _disagreement_examples(
    by_pair: Mapping[str, Sequence[tuple[RawResponse, str]]],
    *,
    assignment_map: Mapping[str, Assignment],
    limit: int,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for pair_id in sorted(by_pair):
        rows = by_pair[pair_id]
        preferences = Counter(preference for _, preference in rows)
        dimension_ranges: dict[str, dict[str, list[int]]] = {}
        for dimension in DIMENSIONS:
            values_by_variant: dict[str, list[int]] = {
                "baseline": [],
                "recommendation_v2": [],
            }
            for response, _ in rows:
                assignment = assignment_map[response.assignment_id]
                scores = response.dimension_scores[dimension]
                for label, variant in (
                    ("A", assignment.left_variant),
                    ("B", assignment.right_variant),
                ):
                    score = scores[label]
                    if score is not None:
                        values_by_variant[variant].append(score)
            for variant in ("baseline", "recommendation_v2"):
                values = [
                    value for value in values_by_variant[variant] if value is not None
                ]
                if values and len(set(values)) > 1:
                    dimension_ranges.setdefault(dimension, {})[variant] = [
                        min(values),
                        max(values),
                    ]
        if len(preferences) > 1 or dimension_ranges:
            examples.append(
                {
                    "pairId": pair_id,
                    "responseCount": len(rows),
                    "preferences": dict(sorted(preferences.items())),
                    "dimensionRanges": dimension_ranges,
                }
            )
        if len(examples) >= limit:
            break
    return examples


def _llm_judge_result(
    responses: Sequence[RawResponse],
    assignment_map: Mapping[str, Assignment],
    judgments: Sequence[Mapping[str, Any]],
    *,
    confidence: float,
) -> dict[str, Any]:
    judge_by_pair: dict[str, str] = {}
    for row in judgments:
        if set(row) != {"pairId", "preference"}:
            raise ValueError("LLM judge rows must contain only pairId and preference")
        pair_id, preference = row["pairId"], row["preference"]
        if not isinstance(pair_id, str) or preference not in VARIANT_PREFERENCES:
            raise ValueError("LLM judge preference must be a known variant")
        if pair_id in judge_by_pair:
            raise ValueError("duplicate LLM judge pairId")
        judge_by_pair[pair_id] = preference

    labels = list(VARIANT_PREFERENCES)
    confusion = {human: {judge: 0 for judge in labels} for human in labels}
    compared = 0
    matches = 0
    for response in responses:
        judge = judge_by_pair.get(response.pair_id)
        if judge is None:
            continue
        human = _map_preference(response.preference, assignment_map[response.assignment_id])
        confusion[human][judge] += 1
        compared += 1
        matches += human == judge
    return {
        "confusionMatrix": confusion,
        "numerator": matches,
        "denominator": compared,
        "agreement": matches / compared if compared else None,
        "agreementCi95": _interval(matches, compared, confidence=confidence),
    }


__all__ = [
    "VARIANT_PREFERENCES",
    "analyze_responses",
    "krippendorff_alpha",
    "reproduce_analysis",
]
