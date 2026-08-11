from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evals.blind_pairwise.cli import main
from evals.blind_pairwise.design import (
    PairInput,
    generate_assignments,
    load_assignments,
    write_assignments,
    write_public_assignments,
    write_public_assignments_by_evaluator,
    validate_assignment_plan,
)
from evals.blind_pairwise.analysis import _alpha_payload, analyze_responses, krippendorff_alpha
from evals.blind_pairwise.report import render_report
from evals.blind_pairwise.preregistration import CONFIG_PATH, load_preregistration, sha256_file

PAIR_INPUT_SHA256 = "0" * 64


def _pairs(count: int = 20) -> list[PairInput]:
    return [
        PairInput(
            pair_id=f"pair-{index:02d}",
            prompt=f"query {index}",
            baseline_text=f"option alpha {index}",
            recommendation_v2_text=f"option beta {index}",
        )
        for index in range(count)
    ]


def _write_pairs(path: Path, pairs: list[PairInput]) -> None:
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "pairId": pair.pair_id,
                    "prompt": pair.prompt,
                    "baselineText": pair.baseline_text,
                    "recommendationV2Text": pair.recommendation_v2_text,
                },
                sort_keys=True,
            )
            for pair in pairs
        )
        + "\n",
        encoding="utf-8",
    )


def test_assign_cli_rejects_seed_that_differs_from_frozen_preregistration(tmp_path: Path) -> None:
    pairs_path = tmp_path / "pairs.jsonl"
    _write_pairs(pairs_path, _pairs())
    with pytest.raises(ValueError, match="seed"):
        main(
            [
                "assign",
                "--pairs",
                str(pairs_path),
                "--evaluators",
                "eval-01,eval-02,eval-03,eval-04,eval-05",
                "--seed",
                "1",
                "--coordinator-out",
                str(tmp_path / "assignments.json"),
                "--public-dir",
                str(tmp_path / "public"),
            ]
        )


def test_assign_cli_embeds_pair_and_preregistration_hash_provenance(tmp_path: Path) -> None:
    pairs_path = tmp_path / "pairs.jsonl"
    _write_pairs(pairs_path, _pairs())
    coordinator_path = tmp_path / "assignments.json"
    main(
        [
            "assign",
            "--pairs",
            str(pairs_path),
            "--evaluators",
            "eval-01,eval-02,eval-03,eval-04,eval-05",
            "--seed",
            str(load_preregistration()["seed"]),
            "--coordinator-out",
            str(coordinator_path),
            "--public-dir",
            str(tmp_path / "public"),
        ]
    )
    payload = json.loads(coordinator_path.read_text(encoding="utf-8"))
    assert payload["pairInputSha256"]
    assert payload["preregistrationSha256"]
    assert payload["pairCount"] == 20
    assert payload["ratingsPerPair"] == 3
    assert payload["minimumEligibleEvaluators"] == 5
    assert payload["confidence"] == 0.95


def test_assignment_writer_requires_provenance_for_reproducible_analysis(tmp_path: Path) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    with pytest.raises(TypeError, match="pair_input_sha256"):
        write_assignments(tmp_path / "assignments.json", assignments, seed=20260811)


def test_public_delivery_is_one_artifact_per_evaluator_and_routes_exact_rows(tmp_path: Path) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    routes = write_public_assignments_by_evaluator(tmp_path, assignments)
    assert set(routes) == {f"eval-0{index}" for index in range(1, 6)}
    for evaluator_id, artifact_path in routes.items():
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        internal = [row for row in assignments if row.evaluator_id == evaluator_id]
        assert len(payload["assignments"]) == len(internal)
        assert {row["assignmentId"] for row in payload["assignments"]} == {
            row.assignment_id for row in internal
        }
        assert len({row["pairId"] for row in payload["assignments"]}) == len(internal)
        serialized = json.dumps(payload)
        assert "evaluatorId" not in serialized
        assert "leftVariant" not in serialized
        assert "rightVariant" not in serialized
        assert "seed" not in payload
        assert "randomizationAlgorithm" not in payload


def test_single_public_writer_refuses_to_emit_all_evaluator_assignments(tmp_path: Path) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    with pytest.raises(ValueError, match="one evaluator"):
        write_public_assignments(tmp_path / "public.json", assignments)


def test_assignment_loader_rejects_routing_manifest_that_does_not_match_rows(tmp_path: Path) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    path = tmp_path / "assignments.json"
    write_assignments(
        path,
        assignments,
        seed=20260811,
        pair_input_sha256=PAIR_INPUT_SHA256,
        preregistration_sha256=sha256_file(CONFIG_PATH),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["routing"]["eval-01"]["assignmentIds"].append("asgn-not-routed")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="routing"):
        load_assignments(path)


def test_assignment_writer_rejects_rows_generated_with_a_different_seed(tmp_path: Path) -> None:
    forged = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=1,
    )
    with pytest.raises(ValueError, match="mapping|seed"):
        write_assignments(
            tmp_path / "forged.json",
            forged,
            seed=20260811,
            pair_input_sha256=PAIR_INPUT_SHA256,
            preregistration_sha256=sha256_file(CONFIG_PATH),
        )


def test_assignment_loader_recomputes_and_rejects_forged_left_right_mapping(
    tmp_path: Path,
) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    path = tmp_path / "assignments.json"
    write_assignments(
        path,
        assignments,
        seed=20260811,
        pair_input_sha256=PAIR_INPUT_SHA256,
        preregistration_sha256=sha256_file(CONFIG_PATH),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = payload["assignments"][0]
    row["leftVariant"], row["rightVariant"] = row["rightVariant"], row["leftVariant"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="mapping|seed"):
        load_assignments(path)


@pytest.mark.parametrize("bad_hash", ("pair-input-test", "A" * 64, "0" * 63, "0" * 65))
def test_assignment_writer_rejects_noncanonical_sha256_provenance(
    tmp_path: Path, bad_hash: str
) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    with pytest.raises(ValueError, match="SHA-256"):
        write_assignments(
            tmp_path / "bad-hash.json",
            assignments,
            seed=20260811,
            pair_input_sha256=bad_hash,
            preregistration_sha256=sha256_file(CONFIG_PATH),
        )


def test_assignment_loader_rejects_noncanonical_sha256_provenance(tmp_path: Path) -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    path = tmp_path / "assignments.json"
    write_assignments(
        path,
        assignments,
        seed=20260811,
        pair_input_sha256=PAIR_INPUT_SHA256,
        preregistration_sha256=sha256_file(CONFIG_PATH),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["preregistrationSha256"] = "A" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        load_assignments(path)


def test_singleton_alpha_units_are_excluded_from_margins_and_payload_counts() -> None:
    base = {"u1": [1, 2], "u2": [1, 1]}
    with_singleton = {**base, "singleton-only": [5]}
    base_payload = _alpha_payload(base, level="ordinal")
    singleton_payload = _alpha_payload(with_singleton, level="ordinal")
    assert singleton_payload["alpha"] == pytest.approx(base_payload["alpha"])
    assert singleton_payload["unitCount"] == base_payload["unitCount"] == 2
    assert singleton_payload["observations"] == base_payload["observations"] == 4


def test_seeded_left_order_is_globally_and_per_evaluator_balanced() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    global_counts = Counter(row.left_variant for row in assignments)
    assert global_counts == {"baseline": 30, "recommendation_v2": 30}
    for evaluator_id in {row.evaluator_id for row in assignments}:
        counts = Counter(row.left_variant for row in assignments if row.evaluator_id == evaluator_id)
        assert abs(counts["baseline"] - counts["recommendation_v2"]) <= 1
    other = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260812,
    )
    assert [row.left_variant for row in assignments] != [row.left_variant for row in other]


def _raw_response(assignment, response_id: str) -> dict[str, object]:
    return {
        "schemaVersion": "blind-pairwise-response-v1",
        "responseId": response_id,
        "assignmentId": assignment.assignment_id,
        "pairId": assignment.pair_id,
        "evaluatorId": assignment.evaluator_id,
        "responseOrigin": "human",
        "consent": True,
        "preference": "A",
        "dimensionScores": {
            dimension: {"A": 4, "B": 3}
            for dimension in ("relevance_fit", "explainability", "trustworthiness")
        },
        "disagreementTags": [],
        "submittedAt": "2026-08-11T00:00:00+00:00",
    }


def test_assignment_plan_requires_exactly_three_distinct_evaluators_per_pair() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    malformed = assignments[:-1]
    with pytest.raises(ValueError, match="exactly 3"):
        validate_assignment_plan(malformed)


def test_coverage_requires_all_planned_assignments_and_observed_evaluators() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    incomplete = analyze_responses([], assignments)
    assert incomplete["coverage"]["observedHumanEvaluators"] == 0
    assert incomplete["coverage"]["humanInputComplete"] is False
    assert incomplete["coverage"]["completePairs"] == 0
    assert incomplete["status"] == "HUMAN_INPUT_REQUIRED"
    report = render_report(incomplete)
    assert "HUMAN_INPUT_REQUIRED" in report
    assert "Observed human evaluator aliases: 0" in report
    assert "pair-00" in report
    rows = [_raw_response(assignment, f"resp-{index:03d}") for index, assignment in enumerate(assignments)]
    complete = analyze_responses(rows, assignments)
    assert complete["coverage"]["observedHumanEvaluators"] == 5
    assert complete["coverage"]["humanInputComplete"] is True
    assert complete["status"] == "HUMAN_INPUT_COMPLETE"
    assert all(item["complete"] for item in complete["coverage"]["pairCompleteness"].values())


def test_coverage_does_not_accept_an_extra_or_duplicate_assignment() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    rows = [_raw_response(assignment, f"resp-{index:03d}") for index, assignment in enumerate(assignments)]
    rows.append(rows[0].copy())
    with pytest.raises(ValueError, match="duplicate responseId"):
        analyze_responses(rows, assignments)


def test_disagreement_ranges_map_reversed_a_b_scores_back_to_variants() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    pair_rows = [row for row in assignments if row.pair_id == "pair-00"]
    responses = []
    for index, assignment in enumerate(pair_rows):
        scores = {
            "relevance_fit": {
                "A": 4 if assignment.left_variant == "baseline" else 3,
                "B": 3 if assignment.left_variant == "baseline" else 4,
            },
            "explainability": {
                "A": 4 if assignment.left_variant == "baseline" else 3,
                "B": 3 if assignment.left_variant == "baseline" else 4,
            },
            "trustworthiness": {
                "A": 4 if assignment.left_variant == "baseline" else 3,
                "B": 3 if assignment.left_variant == "baseline" else 4,
            },
        }
        row = _raw_response(assignment, f"range-resp-{index}")
        row["preference"] = "A" if assignment.left_variant == "recommendation_v2" else "B"
        row["dimensionScores"] = scores
        responses.append(row)
    result = analyze_responses(responses, assignments)
    assert all(item["pairId"] != "pair-00" for item in result["disagreementExamples"])


def test_ordinal_alpha_is_not_interval_distance_for_uneven_category_marginals() -> None:
    units = {
        "u1": [1, 2],
        "u2": [1, 1],
        "u3": [2, 5],
        "u4": [5, 5],
        "u5": [1, 1],
        "u6": [1, 1],
        "u7": [1, 5],
    }
    ordinal = krippendorff_alpha(units, level="ordinal")
    interval = krippendorff_alpha(units, level="interval")
    assert ordinal is not None
    assert interval is not None
    assert ordinal != pytest.approx(interval)


def test_ordinal_alpha_matches_pooled_marginal_known_answer() -> None:
    units = {
        "u1": [1, 2],
        "u2": [1, 1],
        "u3": [2, 5],
    }
    # Pooled margins are n(1)=3, n(2)=2, n(5)=1. With
    # delta^2(c,k)=(sum margins from c through k - (n_c+n_k)/2)^2,
    # Do=17/6 and De=36/6, so alpha=1-17/36.
    assert krippendorff_alpha(units, level="ordinal") == pytest.approx(
        1 - 17 / 36
    )


def test_preference_alpha_treats_abstain_as_missing_but_preserves_count() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    pair_rows = [row for row in assignments if row.pair_id == "pair-00"]
    rows = [_raw_response(pair_rows[0], "alpha-01"), _raw_response(pair_rows[1], "alpha-02")]
    abstain = _raw_response(pair_rows[2], "alpha-03")
    abstain["preference"] = "abstain"
    abstain["dimensionScores"] = {
        dimension: {"A": None, "B": None}
        for dimension in ("relevance_fit", "explainability", "trustworthiness")
    }
    rows.append(abstain)
    result = analyze_responses(rows, assignments)
    assert result["preferenceCounts"]["abstain"] == 1
    assert result["agreement"]["preference"]["missingPolicy"] == "abstain-as-missing"
    assert result["agreement"]["preference"]["observations"] == 2
    report = render_report(result)
    assert "abstain as missing" in report.lower()
    assert "A/B/tie/abstain preference" not in report


def test_wilson_intervals_are_explicitly_descriptive_response_level_estimands() -> None:
    assignments = generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        seed=20260811,
    )
    result = analyze_responses([], assignments)
    assert result["confidence"] == 0.95
    assert result["intervalEstimand"]["method"] == "wilson"
    assert result["intervalEstimand"]["scope"] == "descriptive-conditional-response-level"
    assert result["intervalEstimand"]["accountsForCrossedPairEvaluatorDependence"] is False
    with pytest.raises(ValueError, match="0.95"):
        analyze_responses([], assignments, confidence=0.90)
    report = render_report(result)
    assert "conditional response-level" in report
    assert "crossed pair/evaluator" in report
    assert "population" in report.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "추천 요청은 user@example.com 입니다"),
        ("baseline_text", "연락처 010-1234-5678 로 문의"),
        ("recommendation_v2_text", "recommendation-v2 모델이 선택한 결과"),
        ("pair_id", "pair-baseline-v2-01"),
    ],
)
def test_pair_input_rejects_pii_and_algorithm_identity_in_all_evaluator_facing_fields(
    field: str, value: str
) -> None:
    values = {
        "pair_id": "pair-01",
        "prompt": "safe buyer query",
        "baseline_text": "safe option one",
        "recommendation_v2_text": "safe option two",
    }
    values[field] = value
    with pytest.raises(ValueError, match="de-ident|PII|identity|opaque"):
        PairInput(**values)


def test_analyze_cli_hashes_optional_judge_and_renders_confusion_matrix(tmp_path: Path) -> None:
    pairs_path = tmp_path / "pairs.jsonl"
    _write_pairs(pairs_path, _pairs())
    coordinator_path = tmp_path / "assignments.json"
    main(
        [
            "assign",
            "--pairs",
            str(pairs_path),
            "--evaluators",
            "eval-01,eval-02,eval-03,eval-04,eval-05",
            "--seed",
            str(load_preregistration()["seed"]),
            "--coordinator-out",
            str(coordinator_path),
            "--public-dir",
            str(tmp_path / "public"),
        ]
    )
    assignments = load_assignments(coordinator_path)
    raw_path = tmp_path / "raw.jsonl"
    raw_path.write_text(json.dumps(_raw_response(assignments[0], "judge-resp")) + "\n", encoding="utf-8")
    judge_path = tmp_path / "judge.jsonl"
    judge_path.write_text(
        json.dumps({"pairId": assignments[0].pair_id, "preference": "baseline"}) + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "analysis.json"
    main(
        [
            "analyze",
            "--raw",
            str(raw_path),
            "--assignments",
            str(coordinator_path),
            "--judge",
            str(judge_path),
            "--out",
            str(output_path),
        ]
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "llmJudge" in payload
    assert payload["provenance"]["llmJudgeSha256"]
    report = output_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "preregistrationSha256" in report
    assert "pairInputSha256" in report
    assert "confusion matrix" in report.lower()
    assert "recommendation_v2" in report
