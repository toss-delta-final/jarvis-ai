from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.agents.buyer.recommendation.rerank_grounding import CandidateGroundingFacts
from evals.rerank_grounding.metrics import (
    MetricItem,
    MetricSample,
    detect_unsupported_rationale,
    score_samples,
)
from evals.rerank_grounding.schema import (
    DEFAULT_FIXTURE_PATH,
    fixture_sha256,
    load_fixture,
)


def test_fixture_loads_unique_case_ids_and_declared_test_types() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE_PATH)

    assert fixture.fixture_version == "rerank-grounding-v1"
    assert len(fixture.cases) == 10
    assert len({case.case_id for case in fixture.cases}) == 10
    assert {case.test_type for case in fixture.cases} == {"MFT", "INV", "DIR"}


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
