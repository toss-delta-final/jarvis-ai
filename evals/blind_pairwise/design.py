"""사전 등록된 pair 목록의 blind presentation과 평가자 배정."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

RANDOMIZATION_ALGORITHM = "sha256-seeded-constrained-balanced-left-right-v2"
ASSIGNMENT_SCHEMA_VERSION = "blind-pairwise-assignment-v1"
MIN_PAIRS = 20
MIN_EVALUATORS = 5
DEFAULT_RATINGS_PER_PAIR = 3
_PII_PATTERN = re.compile(r"(?:\b[^\s@]+@[^\s@]+\.[^\s@]+\b|\+?\d[\d .-]{7,}\d)")
_PII_ID_PATTERN = re.compile(
    r"(?i)\b(?:user|member|account|customer|order|주문|회원|계정)\s*(?:(?:id|no|number|번호)\s*[:#-]?\s*[A-Za-z0-9-]{3,}|[:#-]\s*[A-Za-z0-9-]{3,})\b"
)
_DISCLOSURE_PATTERN = re.compile(
    r"(?i)(?:\bbaseline\b|\brecommendation[\s_-]*v2\b|\b(?:model|algorithm|system)[\s_-]*(?:version|v\d+(?:\.\d+)*)\b|\bversion\s*\d+(?:\.\d+)*|\bv\d+(?:\.\d+)*\b|\bsha256\b|\brandomi[sz]ation\b)"
)
_SAFE_PAIR_ID_PATTERN = re.compile(r"^pair-[a-z0-9][a-z0-9_-]{0,63}$")
_EVALUATOR_ID_PATTERN = re.compile(r"^eval-[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PairInput:
    """한 buyer case의 두 variant 출력. 아직 평가자에게 공개하지 않는다."""

    pair_id: str
    prompt: str
    baseline_text: str
    recommendation_v2_text: str

    def __post_init__(self) -> None:
        for name in ("pair_id", "prompt", "baseline_text", "recommendation_v2_text"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not _SAFE_PAIR_ID_PATTERN.fullmatch(self.pair_id):
            raise ValueError("pair_id must be an opaque safe pair identifier")
        _validate_evaluator_facing_text(self.pair_id, "pair_id")
        for field in ("prompt", "baseline_text", "recommendation_v2_text"):
            _validate_evaluator_facing_text(getattr(self, field), field)
        if self.baseline_text == self.recommendation_v2_text:
            raise ValueError("pair variants must not be identical")


def _validate_evaluator_facing_text(value: str, field: str) -> None:
    if _PII_PATTERN.search(value) or _PII_ID_PATTERN.search(value):
        raise ValueError(f"{field} must pass de-identification: PII is not allowed")
    if _DISCLOSURE_PATTERN.search(value):
        raise ValueError(f"{field} must not disclose algorithm identity or version")


@dataclass(frozen=True, slots=True)
class Assignment:
    """한 평가자에게 배정되는 pair와 내부 A/B 매핑.

    ``left_variant``는 coordinator 전용 값이다. ``to_public_dict``와
    ``public_presentation``에는 절대로 기록하지 않아 평가자에게 알고리즘 신원을
    노출하지 않는다.
    """

    assignment_id: str
    pair_id: str
    evaluator_id: str
    prompt: str
    left_text: str
    right_text: str
    left_variant: str
    right_variant: str

    @property
    def public_presentation(self) -> dict[str, str]:
        """평가자 UI에 전달할 수 있는 A/B payload."""
        return {
            "pairId": self.pair_id,
            "leftLabel": "A",
            "leftText": self.left_text,
            "rightLabel": "B",
            "rightText": self.right_text,
            "prompt": self.prompt,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """알고리즘 이름·variant 매핑·평가자 ID를 제거한 공개 표현."""
        return {
            "assignmentId": self.assignment_id,
            "pairId": self.pair_id,
            "presentation": self.public_presentation,
        }

    def to_internal_dict(self) -> dict[str, Any]:
        """분석 coordinator만 읽는 assignment artifact 행."""
        return {
            "assignmentId": self.assignment_id,
            "pairId": self.pair_id,
            "evaluatorId": self.evaluator_id,
            "prompt": self.prompt,
            "leftText": self.left_text,
            "rightText": self.right_text,
            "leftVariant": self.left_variant,
            "rightVariant": self.right_variant,
        }


def generate_assignments(
    pairs: Iterable[PairInput],
    *,
    evaluator_ids: Iterable[str],
    ratings_per_pair: int = DEFAULT_RATINGS_PER_PAIR,
    seed: int,
) -> list[Assignment]:
    """최소 표본 설계에 맞는 결정론 assignment를 만든다.

    각 pair는 서로 다른 evaluator ``ratings_per_pair``명에게만 배정된다. pair별
    evaluator 순서는 SHA-256으로 섞어 evaluator별 전체 부담도 최대 한 건 차이로
    유지한다. A/B 방향은 seed 고정 제약 배정으로 전체·evaluator별 좌우 노출을
    균형화한다.
    """
    pair_list = list(pairs)
    evaluator_list = list(evaluator_ids)
    if len(pair_list) < MIN_PAIRS:
        raise ValueError(f"at least {MIN_PAIRS} pairs are required")
    if len(evaluator_list) < MIN_EVALUATORS:
        raise ValueError(f"at least {MIN_EVALUATORS} eligible evaluators are required")
    if len(set(evaluator_list)) != len(evaluator_list):
        raise ValueError("evaluator IDs must be unique")
    if any(not _EVALUATOR_ID_PATTERN.fullmatch(value) for value in evaluator_list):
        raise ValueError("evaluator IDs must be pseudonymous eval-* aliases")
    if ratings_per_pair != DEFAULT_RATINGS_PER_PAIR:
        raise ValueError("ratings_per_pair must be 3 independent evaluators")
    if ratings_per_pair > len(evaluator_list):
        raise ValueError("ratings_per_pair must not exceed the number of independent evaluators")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if len({pair.pair_id for pair in pair_list}) != len(pair_list):
        raise ValueError("pair IDs must be unique")

    # 정렬은 호출자의 입력 순서에 의존하지 않도록 한다.
    pair_list.sort(key=lambda pair: pair.pair_id)
    evaluator_list.sort()
    evaluator_order = _stable_permutation(evaluator_list, seed, "all-evaluators")
    pending: list[tuple[str, str, str, str, str, str]] = []
    for pair_index, pair in enumerate(pair_list):
        offset = (pair_index * ratings_per_pair) % len(evaluator_order)
        selected = [
            evaluator_order[(offset + index) % len(evaluator_order)]
            for index in range(ratings_per_pair)
        ]
        for rating_index, evaluator_id in enumerate(selected):
            assignment_id = f"asgn-{pair.pair_id}-{rating_index + 1:02d}"
            texts = {
                "baseline": pair.baseline_text,
                "recommendation_v2": pair.recommendation_v2_text,
            }
            pending.append(
                (
                    assignment_id,
                    pair.pair_id,
                    evaluator_id,
                    pair.prompt,
                    texts["baseline"],
                    texts["recommendation_v2"],
                )
            )
    left_variants = _balanced_left_variants(pending, seed)
    assignments: list[Assignment] = []
    for assignment_id, pair_id, evaluator_id, prompt, baseline_text, recommendation_text in pending:
        left_variant = left_variants[assignment_id]
        right_variant = "recommendation_v2" if left_variant == "baseline" else "baseline"
        texts = {"baseline": baseline_text, "recommendation_v2": recommendation_text}
        assignments.append(
            Assignment(
                assignment_id=assignment_id,
                pair_id=pair_id,
                evaluator_id=evaluator_id,
                prompt=prompt,
                left_text=texts[left_variant],
                right_text=texts[right_variant],
                left_variant=left_variant,
                right_variant=right_variant,
            )
        )
    # UI 표시 순서도 재현 가능하게 섞는다. pair/evaluator 배정은 이미 고정됐다.
    return sorted(
        assignments,
        key=lambda item: _sort_key(seed, item.assignment_id),
    )


def _stable_permutation(values: list[str], seed: int, pair_id: str) -> list[str]:
    return sorted(values, key=lambda value: _sort_key(seed, f"{pair_id}|evaluator|{value}"))


def _sort_key(seed: int, token: str) -> str:
    return hashlib.sha256(f"{seed}|{token}".encode("utf-8")).hexdigest()


def _balanced_left_variants(
    pending: list[tuple[str, str, str, str, str, str]], seed: int
) -> dict[str, str]:
    """전역·평가자별 좌측 variant 수를 seed 고정으로 균형화한다."""
    by_evaluator: dict[str, list[str]] = {}
    for assignment_id, _, evaluator_id, *_ in pending:
        by_evaluator.setdefault(evaluator_id, []).append(assignment_id)
    target_baseline = len(pending) // 2
    floor_total = sum(len(ids) // 2 for ids in by_evaluator.values())
    extra = target_baseline - floor_total
    odd_evaluators = sorted(
        (evaluator_id for evaluator_id, ids in by_evaluator.items() if len(ids) % 2),
        key=lambda evaluator_id: _sort_key(seed, f"left-extra|{evaluator_id}"),
    )
    baseline_counts = {
        evaluator_id: len(ids) // 2 for evaluator_id, ids in by_evaluator.items()
    }
    for evaluator_id in odd_evaluators[:extra]:
        baseline_counts[evaluator_id] += 1
    result: dict[str, str] = {}
    for evaluator_id, ids in by_evaluator.items():
        ordered = sorted(ids, key=lambda assignment_id: _sort_key(seed, assignment_id))
        baseline_ids = set(ordered[: baseline_counts[evaluator_id]])
        for assignment_id in ids:
            result[assignment_id] = (
                "baseline" if assignment_id in baseline_ids else "recommendation_v2"
            )
    return result


def _validate_seed_mapping(assignments: Iterable[Assignment], seed: int) -> None:
    """재계산한 constrained mapping과 내부 A/B variant를 대조한다."""
    rows = list(assignments)
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if len({row.assignment_id for row in rows}) != len(rows):
        raise ValueError("assignment IDs must be unique")
    pending = [
        (row.assignment_id, row.pair_id, row.evaluator_id, "", "", "") for row in rows
    ]
    expected_left = _balanced_left_variants(pending, seed)
    for row in rows:
        expected = expected_left[row.assignment_id]
        expected_right = "recommendation_v2" if expected == "baseline" else "baseline"
        if row.left_variant != expected or row.right_variant != expected_right:
            raise ValueError(
                f"assignment left/right mapping is inconsistent with seed {seed}"
            )


def _validate_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical lowercase SHA-256 hex digest")
    return value


def write_assignments(
    path: Path,
    assignments: Iterable[Assignment],
    *,
    seed: int,
    pair_input_sha256: str,
    preregistration_sha256: str,
    pair_count: int | None = None,
    ratings_per_pair: int = DEFAULT_RATINGS_PER_PAIR,
    minimum_eligible_evaluators: int = MIN_EVALUATORS,
    confidence: float = 0.95,
    routing: Mapping[str, str] | None = None,
) -> None:
    """내부 assignment artifact를 JSON으로 기록한다."""
    rows = list(assignments)
    if not pair_input_sha256:
        raise ValueError("pair_input_sha256 is required")
    if not preregistration_sha256:
        raise ValueError("preregistration_sha256 is required")
    if confidence != 0.95:
        raise ValueError("confidence must be fixed at 0.95")
    validate_assignment_plan(
        rows,
        minimum_pairs=pair_count if pair_count is not None else MIN_PAIRS,
        ratings_per_pair=ratings_per_pair,
        minimum_eligible_evaluators=minimum_eligible_evaluators,
    )
    if pair_count is not None and len({row.pair_id for row in rows}) != pair_count:
        raise ValueError("pair count does not match the frozen plan")
    _validate_sha256(pair_input_sha256, "pair_input_sha256")
    _validate_sha256(preregistration_sha256, "preregistration_sha256")
    _validate_seed_mapping(rows, seed)
    evaluator_ids = sorted({row.evaluator_id for row in rows})
    route_payload = {
        evaluator_id: {
            "artifact": (
                routing[evaluator_id]
                if routing is not None and evaluator_id in routing
                else f"evaluator-set-{index + 1:03d}.json"
            ),
            "assignmentIds": [
                row.assignment_id for row in rows if row.evaluator_id == evaluator_id
            ],
        }
        for index, evaluator_id in enumerate(evaluator_ids)
    }
    payload = {
        "schemaVersion": ASSIGNMENT_SCHEMA_VERSION,
        "seed": seed,
        "pairInputSha256": pair_input_sha256,
        "preregistrationSha256": preregistration_sha256,
        "pairCount": pair_count if pair_count is not None else len({row.pair_id for row in rows}),
        "ratingsPerPair": ratings_per_pair,
        "minimumEligibleEvaluators": minimum_eligible_evaluators,
        "confidence": confidence,
        "routing": route_payload,
        "randomizationAlgorithm": RANDOMIZATION_ALGORITHM,
        "assignmentCount": len(rows),
        "assignments": [row.to_internal_dict() for row in rows],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def validate_assignment_plan(
    assignments: Iterable[Assignment],
    *,
    minimum_pairs: int = MIN_PAIRS,
    ratings_per_pair: int = DEFAULT_RATINGS_PER_PAIR,
    minimum_eligible_evaluators: int = MIN_EVALUATORS,
) -> None:
    """planned pair마다 정확한 독립 evaluator 수가 있는지 검증한다."""
    rows = list(assignments)
    if ratings_per_pair != DEFAULT_RATINGS_PER_PAIR:
        raise ValueError("ratings_per_pair must be exactly 3")
    pair_ids = {row.pair_id for row in rows}
    if len(pair_ids) < minimum_pairs:
        raise ValueError(f"assignment plan requires at least {minimum_pairs} pairs")
    if len({row.assignment_id for row in rows}) != len(rows):
        raise ValueError("assignment IDs must be unique")
    evaluators = {row.evaluator_id for row in rows}
    if len(evaluators) < minimum_eligible_evaluators:
        raise ValueError("assignment plan has fewer than 5 eligible evaluators")
    for pair_id in sorted(pair_ids):
        pair_rows = [row for row in rows if row.pair_id == pair_id]
        pair_evaluators = {row.evaluator_id for row in pair_rows}
        if len(pair_rows) != ratings_per_pair or len(pair_evaluators) != ratings_per_pair:
            raise ValueError(f"pair {pair_id} must have exactly 3 distinct evaluator assignments")


def write_public_assignments(
    path: Path, assignments: Iterable[Assignment], *, evaluator_id: str | None = None
) -> None:
    """평가자에게 전달할 공개 artifact를 기록한다.

    seed·알고리즘·평가자 ID·variant 매핑은 coordinator artifact에만 존재한다.
    """
    rows = list(assignments)
    evaluator_ids = {row.evaluator_id for row in rows}
    if evaluator_id is None:
        if len(evaluator_ids) != 1:
            raise ValueError("public artifact must contain one evaluator")
        evaluator_id = next(iter(evaluator_ids))
    if evaluator_id not in evaluator_ids:
        raise ValueError("evaluator has no assignments")
    rows = [row for row in rows if row.evaluator_id == evaluator_id]
    if len({row.pair_id for row in rows}) != len(rows):
        raise ValueError("one evaluator cannot receive duplicate pair presentations")
    payload = {
        "schemaVersion": "blind-pairwise-presentation-v1",
        "assignmentCount": len(rows),
        "assignments": [row.to_public_dict() for row in rows],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_public_assignments_by_evaluator(
    directory: Path, assignments: Iterable[Assignment]
) -> dict[str, Path]:
    """평가자별로 분리된 blind presentation artifact를 만든다."""
    rows = list(assignments)
    directory.mkdir(parents=True, exist_ok=True)
    routes: dict[str, Path] = {}
    for index, evaluator_id in enumerate(sorted({row.evaluator_id for row in rows}), 1):
        path = directory / f"evaluator-set-{index:03d}.json"
        write_public_assignments(path, rows, evaluator_id=evaluator_id)
        routes[evaluator_id] = path
    return routes


def load_assignment_artifact(path: Path) -> tuple[list[Assignment], dict[str, Any]]:
    """내부 assignment artifact를 읽고 스키마와 A/B 매핑을 확인한다."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != ASSIGNMENT_SCHEMA_VERSION:
        raise ValueError("unsupported assignment schemaVersion")
    if payload.get("randomizationAlgorithm") != RANDOMIZATION_ALGORITHM:
        raise ValueError("unsupported randomizationAlgorithm")
    seed = payload.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("assignment artifact seed must be a non-negative integer")
    _validate_sha256(payload.get("pairInputSha256"), "pairInputSha256")
    _validate_sha256(payload.get("preregistrationSha256"), "preregistrationSha256")
    for field in ("pairCount", "ratingsPerPair", "minimumEligibleEvaluators", "confidence"):
        if not payload.get(field):
            raise ValueError(f"assignment artifact missing {field}")
    rows = payload.get("assignments")
    if not isinstance(rows, list) or payload.get("assignmentCount") != len(rows):
        raise ValueError("assignmentCount does not match assignments")
    assignments: list[Assignment] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("assignment row must be an object")
        expected = {
            "assignmentId",
            "pairId",
            "evaluatorId",
            "prompt",
            "leftText",
            "rightText",
            "leftVariant",
            "rightVariant",
        }
        if set(row) != expected:
            raise ValueError("assignment row fields are invalid")
        if {row["leftVariant"], row["rightVariant"]} != {
            "baseline",
            "recommendation_v2",
        }:
            raise ValueError("assignment row must map both variants exactly once")
        assignments.append(
            Assignment(
                assignment_id=row["assignmentId"],
                pair_id=row["pairId"],
                evaluator_id=row["evaluatorId"],
                prompt=row["prompt"],
                left_text=row["leftText"],
                right_text=row["rightText"],
                left_variant=row["leftVariant"],
                right_variant=row["rightVariant"],
            )
        )
    routing = payload.get("routing")
    expected_evaluators = {assignment.evaluator_id for assignment in assignments}
    if not isinstance(routing, dict) or set(routing) != expected_evaluators:
        raise ValueError("assignment routing manifest does not match evaluator rows")
    for evaluator_id, route in routing.items():
        if not isinstance(route, dict) or set(route) != {"artifact", "assignmentIds"}:
            raise ValueError("assignment routing manifest has invalid fields")
        if not isinstance(route["artifact"], str) or not route["artifact"]:
            raise ValueError("assignment routing manifest has invalid artifact")
        assignment_ids = route["assignmentIds"]
        if not isinstance(assignment_ids, list) or any(
            not isinstance(assignment_id, str) for assignment_id in assignment_ids
        ):
            raise ValueError("assignment routing manifest has invalid assignmentIds")
        expected_ids = {
            assignment.assignment_id
            for assignment in assignments
            if assignment.evaluator_id == evaluator_id
        }
        if len(assignment_ids) != len(set(assignment_ids)) or set(assignment_ids) != expected_ids:
            raise ValueError("assignment routing manifest does not match assignment rows")
    _validate_seed_mapping(assignments, payload["seed"])
    metadata = {
        key: payload[key]
        for key in (
            "schemaVersion",
            "seed",
            "pairInputSha256",
            "preregistrationSha256",
            "pairCount",
            "ratingsPerPair",
            "minimumEligibleEvaluators",
            "confidence",
            "randomizationAlgorithm",
            "assignmentCount",
        )
    }
    return assignments, metadata


def load_assignments(path: Path) -> list[Assignment]:
    """내부 assignment artifact에서 assignment 행만 반환한다."""
    assignments, _ = load_assignment_artifact(path)
    return assignments


__all__ = [
    "ASSIGNMENT_SCHEMA_VERSION",
    "DEFAULT_RATINGS_PER_PAIR",
    "MIN_EVALUATORS",
    "MIN_PAIRS",
    "RANDOMIZATION_ALGORITHM",
    "Assignment",
    "PairInput",
    "generate_assignments",
    "load_assignments",
    "load_assignment_artifact",
    "write_assignments",
    "write_public_assignments",
    "write_public_assignments_by_evaluator",
    "validate_assignment_plan",
]
