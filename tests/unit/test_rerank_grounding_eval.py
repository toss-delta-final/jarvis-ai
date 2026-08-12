from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.agents.buyer.recommendation.rerank_grounding import CandidateGroundingFacts
from app.core.llm import LLMError
from evals.rerank_grounding.cli import main
from evals.rerank_grounding.fakes import ScriptedGroundingLLM
from evals.rerank_grounding.metrics import (
    MetricItem,
    MetricSample,
    detect_overall_claims,
    detect_unsupported_rationale,
    score_samples,
)
from evals.rerank_grounding.schema import (
    DEFAULT_FIXTURE_PATH,
    fixture_sha256,
    load_fixture,
)
from evals.rerank_grounding.report import write_artifacts
from evals.rerank_grounding.runner import run_probe


def test_fixture_loads_unique_case_ids_and_declared_test_types() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE_PATH)

    assert DEFAULT_FIXTURE_PATH.name == "rerank_grounding_v2.json"
    assert fixture.fixture_version == "rerank-grounding-v2"
    assert len(fixture.cases) == 22
    assert len({case.case_id for case in fixture.cases}) == 22
    assert {case.test_type for case in fixture.cases} == {"MFT", "INV", "DIR"}
    assert all(case.final_view.product_groups for case in fixture.cases)
    assert all(
        set(case.overall_oracle.allowed_claim_codes)
        | set(case.overall_oracle.forbidden_claim_codes)
        == {
            "TOP_REVIEW_COUNT",
            "ALL_RATING_HIGH",
            "ALL_WITHIN_TOTAL_BUDGET",
            "NO_VERIFIABLE_OVERALL_CLAIM",
            "POPULARITY_TOP",
            "VALUE_FOR_MONEY_TOP",
        }
        for case in fixture.cases
    )


def test_fixture_hash_is_raw_file_sha256() -> None:
    expected = hashlib.sha256(DEFAULT_FIXTURE_PATH.read_bytes()).hexdigest()

    assert fixture_sha256(DEFAULT_FIXTURE_PATH) == expected


def _write_duplicate_fixture(tmp_path: Path) -> Path:
    source = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))
    source["cases"][1]["caseId"] = source["cases"][0]["caseId"]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    return path


def test_duplicate_case_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate caseId"):
        load_fixture(_write_duplicate_fixture(tmp_path))


def _mutated_fixture(tmp_path: Path, mutate) -> Path:  # noqa: ANN001
    source = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))
    mutate(source["cases"][0])
    path = tmp_path / "mutated.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda case: case["finalView"].__setitem__("listType", "CART"), "unknown listType"),
        (
            lambda case: case["finalView"].__setitem__("totalBudget", True),
            "totalBudget must be an integer or null",
        ),
        (
            lambda case: case["finalView"].__setitem__("productGroups", []),
            "productGroups must be a non-empty list",
        ),
        (
            lambda case: case["finalView"].__setitem__("productGroups", [[999999]]),
            "finalView productId is not a candidate",
        ),
        (
            lambda case: case["finalView"].__setitem__("productGroups", [[101, 101]]),
            "duplicate productId in finalView group",
        ),
        (
            lambda case: case["overallOracle"]["allowedOverallClaims"].append("UNKNOWN"),
            "unknown overall oracle claim",
        ),
        (
            lambda case: case["overallOracle"]["forbiddenOverallClaims"].append(
                case["overallOracle"]["allowedOverallClaims"][0]
            ),
            "overall oracle claim overlap",
        ),
    ],
)
def test_v2_final_view_and_oracle_shape_is_strict(tmp_path: Path, mutate, message: str) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match=message):
        load_fixture(_mutated_fixture(tmp_path, mutate))


def test_v2_oracle_cannot_contradict_raw_final_view(tmp_path: Path) -> None:
    def _contradict(case: dict[str, object]) -> None:
        oracle = case["overallOracle"]
        assert isinstance(oracle, dict)
        allowed = oracle["allowedOverallClaims"]
        forbidden = oracle["forbiddenOverallClaims"]
        assert isinstance(allowed, list) and isinstance(forbidden, list)
        code = forbidden.pop(0)
        allowed.append(code)

    with pytest.raises(ValueError, match="overall oracle contradicts raw facts"):
        load_fixture(_mutated_fixture(tmp_path, _contradict))


def test_v2_contains_all_required_overall_claim_cases() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE_PATH)
    case_ids = {case.case_id for case in fixture.cases}

    assert {
        "overall_top_review_unique",
        "overall_top_review_tie",
        "overall_top_review_missing",
        "overall_all_rating_high",
        "overall_all_rating_unrated",
        "overall_rating_final_subset",
        "overall_budget_single_group",
        "overall_budget_multiple_groups",
        "overall_budget_exceeded",
        "overall_budget_missing_price",
        "overall_budget_pick_one",
        "overall_unsupported_superlatives",
    } <= case_ids


def test_inv_pairs_change_only_the_declared_mutation() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE_PATH)
    pairs: dict[str, list] = {}
    for case in fixture.cases:
        if case.pair_id:
            pairs.setdefault(case.pair_id, []).append(case)

    assert set(pairs) == {"name_injection", "profile_conflict"}
    assert all(len(cases) == 2 for cases in pairs.values())


def _facts(
    *, rating: str = "평가없음", review: str = "정보없음", price: str = "정보없음"
) -> CandidateGroundingFacts:
    return CandidateGroundingFacts(
        product_id=101,
        rating_level=rating,
        review_level=review,
        price_level=price,
    )


@pytest.mark.parametrize(
    ("rationale", "facts"),
    [
        ("평점이 높은 상품이에요", _facts()),
        ("리뷰가 많은 상품이에요", _facts()),
        ("같은 후보군에서 저렴해요", _facts()),
        ("평점 4.8점이고 리뷰 120개예요", _facts(rating="매우높음", review="매우많음")),
    ],
)
def test_detector_flags_unsupported_tier_or_exact_number_claims(
    rationale: str, facts: CandidateGroundingFacts
) -> None:
    assert detect_unsupported_rationale(rationale, facts) is True


@pytest.mark.parametrize(
    ("rationale", "facts"),
    [
        ("평점 평가가 높은 상품이에요", _facts(rating="높음")),
        ("리뷰 정보가 많은 상품이에요", _facts(review="많음")),
        ("같은 후보군에서 비교적 저렴해요", _facts(price="매우저렴")),
        ("요청과의 관련도를 기준으로 추천했어요", _facts()),
    ],
)
def test_detector_accepts_supported_or_neutral_claims(
    rationale: str, facts: CandidateGroundingFacts
) -> None:
    assert detect_unsupported_rationale(rationale, facts) is False


def _metric_item(
    *,
    rationale: str,
    facts: CandidateGroundingFacts,
    grounding_supported: bool | None = None,
    downgraded: bool = False,
) -> MetricItem:
    return MetricItem(
        product_id=facts.product_id,
        displayed_rationale=rationale,
        facts=facts,
        grounding_supported=grounding_supported,
        validator_downgraded=downgraded,
    )


def test_primary_metric_has_explicit_numerator_and_denominator() -> None:
    samples = [
        MetricSample(
            case_id="one",
            test_type="MFT",
            pair_id=None,
            arm="current",
            items=(
                _metric_item(rationale="평점이 높아요", facts=_facts()),
                _metric_item(rationale="중립 추천", facts=_facts()),
                _metric_item(rationale="리뷰가 많아요", facts=_facts(review="많음")),
                _metric_item(rationale="저렴해요", facts=_facts(price="저렴")),
            ),
            candidate_count=4,
        )
    ]

    metrics = score_samples(samples)["current"]

    assert metrics.unsupported_evidence_numerator == 1
    assert metrics.unsupported_evidence_denominator == 4
    assert metrics.unsupported_evidence_rate == 0.25
    assert "표시된" in metrics.metric_definitions["unsupportedEvidence"]["denominator"]


def test_prompt_only_counts_invalid_metadata_even_when_text_is_generic() -> None:
    samples = [
        MetricSample(
            case_id="one",
            test_type="MFT",
            pair_id=None,
            arm="prompt_only",
            items=(
                _metric_item(
                    rationale="추천 상품이에요",
                    facts=_facts(),
                    grounding_supported=False,
                ),
            ),
            candidate_count=1,
        )
    ]

    assert score_samples(samples)["prompt_only"].unsupported_evidence_rate == 1.0


def test_validated_metric_scores_displayed_template_not_model_text() -> None:
    samples = [
        MetricSample(
            case_id="one",
            test_type="MFT",
            pair_id=None,
            arm="validated",
            items=(
                _metric_item(
                    rationale="요청과의 관련도를 기준으로 추천했어요",
                    facts=_facts(),
                    grounding_supported=False,
                    downgraded=True,
                ),
            ),
            candidate_count=1,
        )
    ]

    metrics = score_samples(samples)["validated"]

    assert metrics.unsupported_evidence_rate == 0.0
    assert metrics.validator_downgrade_count == 1
    assert metrics.invalid_structured_evidence_count == 0


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("리뷰가 가장 많은 상품부터 보여드렸어요", ("TOP_REVIEW_COUNT",)),
        ("평점이 높은 상품들만 골랐어요", ("ALL_RATING_HIGH",)),
        ("각 추천 조합이 모두 예산 안에 들어와요", ("ALL_WITHIN_TOTAL_BUDGET",)),
        ("가장 인기 있는 상품이에요", ("POPULARITY_TOP",)),
        ("가성비가 제일 좋아요", ("VALUE_FOR_MONEY_TOP",)),
        ("요청과의 관련도를 기준으로 추천했어요.", ("NO_VERIFIABLE_OVERALL_CLAIM",)),
        ("조건에 맞춰 상품을 정리했어요", ()),
        ("후기가 많은 조건에 가장 잘 맞는 상품이에요", ()),
        ("평점은 높지만 리뷰 정보가 없어요", ()),
        ("평점과 리뷰 수준은 같고 이 상품이 가장 적합해요", ()),
    ],
)
def test_overall_detector_is_bounded_to_registered_korean_families(
    comment: str, expected: tuple[str, ...]
) -> None:
    assert detect_overall_claims(comment) == expected


def _overall_metric_sample(
    *,
    arm: str,
    displayed_comment: str,
    requested: tuple[str, ...] = (),
    supported: tuple[str, ...] = (),
    downgraded: bool = False,
    failures: tuple[str, ...] = (),
) -> MetricSample:
    return MetricSample(
        case_id="overall",
        test_type="MFT",
        pair_id=None,
        arm=arm,  # type: ignore[arg-type]
        items=(),
        candidate_count=0,
        displayed_overall_comment=displayed_comment,
        requested_overall_claim_codes=requested,
        supported_overall_claim_codes=supported,
        overall_validator_downgraded=downgraded,
        overall_failure_reasons=failures,
        allowed_overall_claim_codes=(
            "TOP_REVIEW_COUNT",
            "NO_VERIFIABLE_OVERALL_CLAIM",
        ),
        forbidden_overall_claim_codes=(
            "ALL_RATING_HIGH",
            "ALL_WITHIN_TOTAL_BUDGET",
            "POPULARITY_TOP",
            "VALUE_FOR_MONEY_TOP",
        ),
    )


def test_current_overall_metric_scores_only_detected_claims() -> None:
    metrics = score_samples(
        [
            _overall_metric_sample(
                arm="current",
                displayed_comment="가장 인기 있는 상품이에요",
            )
        ]
    )["current"]

    assert metrics.detected_overall_claim_violation_numerator == 1
    assert metrics.detected_overall_claim_violation_denominator == 1
    assert metrics.detected_overall_claim_violation_rate == 1.0
    assert "등록된 표현" in metrics.metric_definitions["detectedOverallClaimViolation"]["limit"]


def test_prompt_only_counts_forbidden_structured_overall_claim() -> None:
    metrics = score_samples(
        [
            _overall_metric_sample(
                arm="prompt_only",
                displayed_comment="조건에 맞춰 골랐어요",
                requested=("POPULARITY_TOP",),
                downgraded=True,
                failures=("unknown_claim_code",),
            )
        ]
    )["prompt_only"]

    assert metrics.detected_overall_claim_violation_rate == 1.0
    assert metrics.overall_invalid_structured_claim_count == 1
    assert metrics.overall_validator_downgrade_count == 1
    assert metrics.overall_failure_reason_counts == {"unknown_claim_code": 1}


def test_validated_overall_metric_scores_rendered_neutral_not_raw_proposal() -> None:
    metrics = score_samples(
        [
            _overall_metric_sample(
                arm="validated",
                displayed_comment="요청과의 관련도를 기준으로 추천했어요.",
                requested=("POPULARITY_TOP",),
                downgraded=True,
                failures=("unknown_claim_code",),
            )
        ]
    )["validated"]

    assert metrics.detected_overall_claim_violation_rate == 0.0
    assert metrics.overall_invalid_structured_claim_count == 0
    assert metrics.overall_validator_downgrade_count == 1


def test_supported_overall_claim_coverage_uses_oracle_opportunities() -> None:
    metrics = score_samples(
        [
            _overall_metric_sample(
                arm="validated",
                displayed_comment="리뷰 수가 가장 많은 상품부터 보여드렸어요.",
                requested=("TOP_REVIEW_COUNT",),
                supported=("TOP_REVIEW_COUNT",),
            )
        ]
    )["validated"]

    assert metrics.supported_overall_claim_coverage_numerator == 1
    assert metrics.supported_overall_claim_coverage_denominator == 1
    assert metrics.supported_overall_claim_coverage == 1.0


def test_hard_gates_and_valid_rank_coverage_are_aggregated() -> None:
    samples = [
        MetricSample(
            case_id="one",
            test_type="MFT",
            pair_id=None,
            arm="validated",
            items=(_metric_item(rationale="중립", facts=_facts()),),
            candidate_count=2,
            out_of_candidate_id_count=1,
            duplicate_id_count=2,
        )
    ]

    metrics = score_samples(samples)["validated"]

    assert metrics.out_of_candidate_id_count == 1
    assert metrics.duplicate_id_count == 2
    assert metrics.valid_rank_coverage == 0.5


def _one_case_fixture():
    fixture = load_fixture(DEFAULT_FIXTURE_PATH)
    return type(fixture)(
        fixture_version=fixture.fixture_version,
        schema_version=fixture.schema_version,
        cases=(fixture.cases[0],),
    )


def _multi_need_fixture():
    fixture = load_fixture(DEFAULT_FIXTURE_PATH)
    case = next(case for case in fixture.cases if case.case_id == "multi_need_balance")
    return type(fixture)(
        fixture_version=fixture.fixture_version,
        schema_version=fixture.schema_version,
        cases=(case,),
    )


class _FailOnceThenScripted:
    def __init__(self) -> None:
        self.calls = 0
        self.delegate = ScriptedGroundingLLM()

    async def complete(self, **kwargs) -> str:
        self.calls += 1
        if self.calls == 1:
            raise LLMError("temporary")
        return await self.delegate.complete(**kwargs)


async def test_runner_fills_successful_n_and_keeps_failures_separate() -> None:
    run = await run_probe(
        llm=_FailOnceThenScripted(),
        fixture=_one_case_fixture(),
        arms=("current",),
        repeats=2,
        attempt_multiplier=3,
        expose_max=3,
    )

    cell = run.cells[0]
    assert len(cell.samples) == 2
    assert len(cell.failures) == 1
    assert cell.attempts == 3
    assert cell.filled is True


async def test_runner_passes_arm_and_case_need_boundaries_to_rerank() -> None:
    run = await run_probe(
        llm=ScriptedGroundingLLM(),
        fixture=_multi_need_fixture(),
        arms=("validated",),
        repeats=1,
        attempt_multiplier=1,
        expose_max=4,
    )

    sample = run.cells[0].samples[0]
    assert sample.arm == "validated"
    assert sample.ranked_product_ids
    assert len(sample.ranked_product_ids) == 4


async def test_invalid_evidence_is_successful_validated_sample_with_downgrade() -> None:
    run = await run_probe(
        llm=ScriptedGroundingLLM(invalid_evidence=True),
        fixture=_one_case_fixture(),
        arms=("validated",),
        repeats=1,
        attempt_multiplier=1,
        expose_max=3,
    )

    sample = run.cells[0].samples[0]
    assert sample.validator_downgrade_count == 1
    assert sample.failure_type is None
    assert sample.displayed_rationales[0] == "요청과의 관련도를 기준으로 추천했어요"
    assert sample.displayed_overall_comment
    assert sample.final_view.product_groups
    assert sample.overall_grounding_decision is not None


def _manifest() -> dict[str, object]:
    return {
        "gitCommit": "abc123",
        "dirty": False,
        "command": "dry-run",
        "dryRun": True,
        "datasetVersion": "rerank-grounding-v2",
        "datasetHash": "d" * 64,
        "promptHashes": {"current": "a" * 64, "structured": "b" * 64},
        "validatorVersion": "rerank-grounding-v1",
        "overallValidatorVersion": "overall-comment-grounding-v1",
        "modelConfig": {"provider": "dry-run", "model": "scripted", "tier": "smart"},
        "repeats": 1,
        "budget": {"costUsd": 0.0},
    }


async def test_artifacts_are_regenerable_from_raw_samples(tmp_path: Path) -> None:
    run = await run_probe(
        llm=ScriptedGroundingLLM(invalid_evidence=True),
        fixture=_one_case_fixture(),
        arms=("current", "prompt_only", "validated"),
        repeats=1,
        attempt_multiplier=1,
        expose_max=2,
    )

    write_artifacts(tmp_path, run=run, manifest=_manifest())

    assert {path.name for path in tmp_path.iterdir()} == {
        "results.json",
        "run_manifest.json",
        "samples.csv",
        "failures.csv",
        "report.md",
    }
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert results["metrics"]["validated"]["unsupportedEvidence"]["numerator"] == 0
    assert "detectedOverallClaimViolation" in results["metrics"]["validated"]
    samples_header = (tmp_path / "samples.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "rawOverallComment" in samples_header
    assert "rawOverallClaims" in samples_header
    assert "finalView" in samples_header
    assert "renderedOverallComment" in samples_header
    assert "분자" in (tmp_path / "report.md").read_text(encoding="utf-8")


async def test_manifest_records_all_prompt_and_dataset_hashes(tmp_path: Path) -> None:
    run = await run_probe(
        llm=ScriptedGroundingLLM(),
        fixture=_one_case_fixture(),
        arms=("current",),
        repeats=1,
        attempt_multiplier=1,
        expose_max=2,
    )

    write_artifacts(tmp_path, run=run, manifest=_manifest())
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))

    assert set(manifest["promptHashes"]) == {"current", "structured"}
    assert len(manifest["datasetHash"]) == 64
    assert manifest["validatorVersion"] == "rerank-grounding-v1"
    assert manifest["overallValidatorVersion"] == "overall-comment-grounding-v1"


def test_cli_dry_run_writes_all_arms(tmp_path: Path) -> None:
    out = tmp_path / "run"

    code = main(
        [
            "--arms",
            "all",
            "--repeats",
            "1",
            "--dry-run",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert set(results["metrics"]) == {"current", "prompt_only", "validated"}
    assert results["status"] == "not tested"


def test_cli_rejects_existing_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "existing"
    out.mkdir()

    assert (
        main(
            [
                "--arms",
                "all",
                "--repeats",
                "1",
                "--dry-run",
                "--out",
                str(out),
            ]
        )
        == 2
    )


def test_cli_rejects_nonpositive_repeats(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--arms",
                "all",
                "--repeats",
                "0",
                "--dry-run",
                "--out",
                str(tmp_path / "x"),
            ]
        )
        == 2
    )


def test_cli_rejects_unknown_case_id_before_any_call(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--arms",
                "all",
                "--case-ids",
                "missing",
                "--repeats",
                "1",
                "--dry-run",
                "--out",
                str(tmp_path / "x"),
            ]
        )
        == 2
    )
