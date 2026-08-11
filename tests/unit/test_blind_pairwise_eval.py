from __future__ import annotations

import json
from collections import Counter

import pytest

from evals.blind_pairwise.analysis import analyze_responses, krippendorff_alpha, reproduce_analysis
from evals.blind_pairwise.design import (
    PairInput,
    generate_assignments,
    load_assignments,
    write_public_assignments_by_evaluator,
    write_assignments,
)
from evals.blind_pairwise.preregistration import load_preregistration, validate_preregistration
from evals.blind_pairwise.preregistration import CONFIG_PATH, sha256_file
from evals.blind_pairwise.report import render_report
from evals.blind_pairwise.schema import (
    DIMENSIONS,
    RawResponse,
    ValidationError,
    validate_raw_response,
)

PAIR_INPUT_SHA256 = "0" * 64


def _pairs(count: int = 20) -> list[PairInput]:
    return [
        PairInput(
            pair_id=f"pair-{index:02d}",
            prompt=f"buyer query {index}",
            baseline_text=f"option one recommendation {index}",
            recommendation_v2_text=f"option two recommendation {index}",
        )
        for index in range(count)
    ]


def _assignments() -> list:
    return generate_assignments(
        _pairs(),
        evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
        ratings_per_pair=3,
        seed=20260811,
    )


def _response(
    assignment,
    *,
    response_id: str,
    preference: str = "A",
    evaluator_id: str | None = None,
    scores: dict[str, tuple[int, int]] | None = None,
) -> dict[str, object]:
    scores = scores or {dimension: (4, 3) for dimension in DIMENSIONS}
    return {
        "schemaVersion": "blind-pairwise-response-v1",
        "responseId": response_id,
        "assignmentId": assignment.assignment_id,
        "pairId": assignment.pair_id,
        "evaluatorId": evaluator_id or assignment.evaluator_id,
        "responseOrigin": "human",
        "consent": True,
        "preference": preference,
        "dimensionScores": {
            dimension: {"A": values[0], "B": values[1]}
            for dimension, values in scores.items()
        },
        "disagreementTags": [],
        "submittedAt": "2026-08-11T00:00:00+00:00",
    }


def test_assignments_are_seeded_balanced_and_public_presentation_is_blind() -> None:
    first = _assignments()
    second = _assignments()

    assert [item.to_public_dict() for item in first] == [item.to_public_dict() for item in second]
    assert len(first) == 60
    assert {item.pair_id for item in first} == {f"pair-{index:02d}" for index in range(20)}
    for pair_id in {item.pair_id for item in first}:
        rows = [item for item in first if item.pair_id == pair_id]
        assert len({item.evaluator_id for item in rows}) == 3
        assert all(item.public_presentation["leftLabel"] == "A" for item in rows)
        assert all(item.public_presentation["rightLabel"] == "B" for item in rows)
        assert all("baseline" not in json.dumps(item.to_public_dict()) for item in rows)
        assert all("recommendation_v2" not in json.dumps(item.to_public_dict()) for item in rows)
    assert {item.assignment_id for item in first} == {
        item.assignment_id for item in second
    }
    assert {item.evaluator_id for item in first} == {
        "eval-01",
        "eval-02",
        "eval-03",
        "eval-04",
        "eval-05",
    }
    loads = Counter(item.evaluator_id for item in first)
    assert max(loads.values()) - min(loads.values()) <= 1


def test_assignment_generation_rejects_underpowered_designs() -> None:
    with pytest.raises(ValueError, match="20"):
        generate_assignments(_pairs(19), evaluator_ids=("eval-01",) * 5, seed=1)
    with pytest.raises(ValueError, match="5 eligible"):
        generate_assignments(_pairs(), evaluator_ids=("eval-01", "eval-02", "eval-03"), seed=1)
    with pytest.raises(ValueError, match="independent"):
        generate_assignments(
            _pairs(),
            evaluator_ids=("eval-01", "eval-02", "eval-03", "eval-04", "eval-05"),
            ratings_per_pair=6,
            seed=1,
        )


def test_assignment_artifact_round_trips_without_losing_hidden_mapping(tmp_path) -> None:
    assignments = _assignments()
    path = tmp_path / "assignments.json"
    write_assignments(
        path,
        assignments,
        seed=20260811,
        pair_input_sha256=PAIR_INPUT_SHA256,
        preregistration_sha256=sha256_file(CONFIG_PATH),
    )
    loaded = load_assignments(path)
    assert [item.to_public_dict() for item in loaded] == [
        item.to_public_dict() for item in assignments
    ]
    assert loaded[0].left_variant in {"baseline", "recommendation_v2"}


def test_public_assignment_artifact_contains_only_blind_a_b_payload(tmp_path) -> None:
    assignments = _assignments()
    path = write_public_assignments_by_evaluator(tmp_path, assignments)["eval-01"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "blind-pairwise-presentation-v1"
    assert "randomizationAlgorithm" not in payload
    assert "seed" not in payload
    assert all(set(row) == {"assignmentId", "pairId", "presentation"} for row in payload["assignments"])
    serialized = json.dumps(payload)
    assert "leftVariant" not in serialized
    assert "evaluatorId" not in serialized
    assert "baseline" not in serialized
    assert "recommendation_v2" not in serialized


def test_preregistration_freezes_fixed_before_collection_plan() -> None:
    config = load_preregistration()
    validate_preregistration(config)
    assert config["pairCount"] >= 20
    assert config["ratingsPerPair"] == 3
    assert config["minimumEligibleEvaluators"] >= 5
    assert config["agreement"]["ordinalDistance"] == "pooled-marginal-cumulative"
    assert config["fixedBeforeCollection"] is True
    with pytest.raises(ValueError):
        validate_preregistration({**config, "ratingsPerPair": 2})


def test_raw_response_validation_rejects_pii_and_nonhuman_or_missing_scores() -> None:
    assignment = _assignments()[0]
    valid = _response(assignment, response_id="resp-01")
    assert validate_raw_response(valid).response_id == "resp-01"

    for invalid, message in (
        ({**valid, "evaluatorId": "person@example.com"}, "pseudonymous"),
        ({**valid, "responseOrigin": "synthetic"}, "human"),
        ({**valid, "consent": False}, "consent"),
        ({**valid, "dimensionScores": {}}, "dimension"),
        ({**valid, "freeText": "my phone is 010-1234-5678"}, "unknown field"),
    ):
        with pytest.raises(ValidationError, match=message):
            validate_raw_response(invalid)

    abstention = _response(assignment, response_id="resp-abstain", preference="abstain")
    abstention["dimensionScores"] = {
        dimension: {"A": None, "B": None} for dimension in DIMENSIONS
    }
    assert RawResponse.from_dict(abstention).preference == "abstain"


def test_analysis_preserves_preference_denominators_ties_abstains_and_ordinal_scores() -> None:
    assignments = _assignments()
    first_pair = sorted({item.pair_id for item in assignments})[0]
    pair_assignments = [item for item in assignments if item.pair_id == first_pair]
    rows = [
        _response(
            pair_assignments[0],
            response_id="resp-01",
            preference=("A" if pair_assignments[0].left_variant == "baseline" else "B"),
        ),
        _response(
            pair_assignments[1],
            response_id="resp-02",
            preference=("A" if pair_assignments[1].left_variant == "recommendation_v2" else "B"),
        ),
        _response(pair_assignments[2], response_id="resp-03", preference="tie"),
        _response(
            next(item for item in assignments if item.pair_id != first_pair),
            response_id="resp-04",
            preference="abstain",
        ),
    ]
    rows[-1]["dimensionScores"] = {
        dimension: {"A": None, "B": None} for dimension in DIMENSIONS
    }
    result = analyze_responses(rows, assignments)

    assert result["preferenceCounts"] == {
        "baseline": 1,
        "recommendation_v2": 1,
        "tie": 1,
        "abstain": 1,
    }
    assert result["denominators"] == {
        "responses": 4,
        "nonAbstain": 3,
        "decisive": 2,
    }
    assert result["preferenceIntervals95"]["recommendation_v2"]["denominator"] == 2
    assert result["ordinalDistributions"]["relevance_fit"]["baseline"]["denominator"] == 3
    assert (
        sum(result["ordinalDistributions"]["relevance_fit"]["recommendation_v2"]["counts"].values())
        == 3
    )
    assert "exploratory" in result["caveat"].lower()
    assert "population" in result["caveat"].lower()


def test_analysis_reports_krippendorff_agreement_and_disagreement_examples() -> None:
    assignments = _assignments()
    pair_assignments = [item for item in assignments if item.pair_id == "pair-00"]
    rows = [
        _response(item, response_id=f"resp-{index:02d}", preference=preference)
        for index, (item, preference) in enumerate(
            zip(pair_assignments, ("A", "B", "tie"), strict=True)
        )
    ]
    result = analyze_responses(rows, assignments)

    agreement = result["agreement"]
    assert agreement["method"] == "krippendorff-alpha"
    assert agreement["ordinalDistance"] == "pooled-marginal-cumulative"
    assert "preference" in agreement["dimensions"]
    assert set(agreement["ordinal"].keys()) == set(DIMENSIONS)
    assert result["disagreementExamples"]
    assert result["disagreementExamples"][0]["pairId"] == "pair-00"
    assert "evaluatorId" not in result["disagreementExamples"][0]


def test_krippendorff_alpha_handles_perfect_and_discordant_nominal_units() -> None:
    assert krippendorff_alpha({"pair-01": ["baseline", "baseline"]}) == pytest.approx(1.0)
    alpha = krippendorff_alpha(
        {
            "pair-01": ["baseline", "recommendation_v2"],
            "pair-02": ["baseline", "recommendation_v2"],
        }
    )
    assert alpha is not None and alpha < 0


def test_llm_judge_outputs_are_optional_and_include_confusion_matrix_and_ci() -> None:
    assignments = _assignments()
    pair_assignments = [item for item in assignments if item.pair_id in {"pair-00", "pair-01"}]
    rows = [
        _response(item, response_id=f"resp-{index:02d}", preference="A")
        for index, item in enumerate(pair_assignments[:2])
    ]
    without_judge = analyze_responses(rows, assignments)
    assert "llmJudge" not in without_judge

    with_judge = analyze_responses(
        rows,
        assignments,
        llm_judgments=[
            {"pairId": pair_assignments[0].pair_id, "preference": pair_assignments[0].left_variant},
            {"pairId": pair_assignments[1].pair_id, "preference": "tie"},
        ],
    )
    judge = with_judge["llmJudge"]
    assert judge["denominator"] == 2
    assert judge["confusionMatrix"][pair_assignments[0].left_variant][pair_assignments[0].left_variant] == 1
    assert judge["agreementCi95"]["confidence"] == 0.95


def test_analysis_is_reproducible_from_raw_and_assignment_artifacts(tmp_path) -> None:
    assignments = _assignments()
    assignment_path = tmp_path / "assignments.json"
    raw_path = tmp_path / "raw.jsonl"
    write_assignments(
        assignment_path,
        assignments,
        seed=20260811,
        pair_input_sha256=PAIR_INPUT_SHA256,
        preregistration_sha256=sha256_file(CONFIG_PATH),
    )
    raw_path.write_text(
        "\n".join(
            json.dumps(_response(item, response_id=f"resp-{index:02d}"), sort_keys=True)
            for index, item in enumerate(assignments[:4])
        )
        + "\n",
        encoding="utf-8",
    )
    first = reproduce_analysis(raw_path, assignment_path)
    second = reproduce_analysis(raw_path, assignment_path)
    assert first == second
    assert first["artifacts"]["rawSha256"]
    assert first["artifacts"]["assignmentsSha256"]


def test_report_mentions_denominators_intervals_agreement_and_exploratory_limit() -> None:
    assignments = _assignments()
    result = analyze_responses(
        [_response(assignments[0], response_id="resp-01", preference="A")], assignments
    )
    report = render_report(result)
    assert "denominator" in report
    assert "95%" in report
    assert "Krippendorff" in report
    assert "exploratory" in report.lower()
    assert "population" in report.lower()
