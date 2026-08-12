from __future__ import annotations

from dataclasses import replace

import pytest

from app.schemas.spring import SpringProduct
from evals.goldenset.schema import DATASET_VERSION, SCHEMA_VERSION, GoldenCase
from evals.metrics.runner import EvaluationFixtures
from evals.rerank_scoring.fakes import ScriptedScoringLLM
from evals.rerank_scoring.runner import build_case_input, run_case_arms, run_probe
from evals.rerank_scoring.schema import RankingCaseInput


def _case(
    *,
    case_id: str = "buy-srch-9001",
    identity_kind: str = "guest",
    hard_constraints: dict[str, object] | None = None,
) -> GoldenCase:
    return GoldenCase.model_validate(
        {
            "caseId": case_id,
            "schemaVersion": SCHEMA_VERSION,
            "datasetVersion": DATASET_VERSION,
            "split": "dev",
            "slices": ["search", identity_kind, "single_need"],
            "query": "업무용 테스트 상품 추천",
            "queryType": "simple",
            "identity": {
                "kind": identity_kind,
                "personaId": "persona-1" if identity_kind == "member" else None,
            },
            "expectedRoute": "recommend",
            "expectedFilters": {"keyword": "테스트"},
            "searchFixtureId": f"fixture-{case_id}",
            "provenance": "synthetic",
            "labeler": "labeler-01",
            "createdAt": "2026-08-13",
            "labelSource": "model",
            "labeledAt": "2026-08-13",
            "labelRationale": "평가 runner 계약 테스트용 라벨.",
            "notes": "평가 runner 계약 테스트",
            "relevantProductIds": [3, 1],
            "relevanceGrades": {"3": 3, "1": 2, "2": 0, "4": 0},
            "idealOrder": [3, 1],
            "hardConstraints": hard_constraints or {},
            "mustExcludeProductIds": [],
        }
    )


def _fixtures(case: GoldenCase) -> EvaluationFixtures:
    catalog = {
        "1": {
            "productId": 1,
            "name": "Acme 키보드",
            "price": 10_000,
            "rating": 4.8,
            "reviewCount": 100,
            "categoryName": "키보드",
            "brandName": "Acme",
        },
        "2": {
            "productId": 2,
            "name": "고가 마우스",
            "price": 99_000,
            "rating": 4.7,
            "reviewCount": 80,
            "categoryName": "마우스",
            "brandName": "Other",
        },
        "3": {
            "productId": 3,
            "name": "Acme 마우스",
            "price": 20_000,
            "rating": 4.6,
            "reviewCount": 60,
            "categoryName": "마우스",
            "brandName": "Acme",
        },
        "4": {
            "productId": 4,
            "name": "차단 카테고리",
            "price": 25_000,
            "rating": 4.5,
            "reviewCount": 40,
            "categoryName": "차단",
            "brandName": "Other",
        },
    }
    return EvaluationFixtures(
        catalog=catalog,
        search_responses={
            case.search_fixture_id: {
                "productIds": [3, 1, 2, 4],
                "request": case.expected_filters,
            }
        },
        purchase_history={"persona-1": {"orders": []}},
        manifest={"datasetVersion": DATASET_VERSION, "datasetHash": "dataset-sha"},
        non_discriminative_case_ids=frozenset(),
    )


def _case_input() -> RankingCaseInput:
    products = tuple(
        SpringProduct(
            product_id=product_id,
            name=f"상품 {product_id}",
            price=10_000 + product_id,
            rating=4.8,
            review_count=100,
            category="테스트",
        )
        for product_id in (101, 102, 103, 104)
    )
    return RankingCaseInput(
        case_id="buy-srch-9001",
        query="테스트 추천",
        candidates=products,
        search_rank_by_id={product.product_id: rank for rank, product in enumerate(products, 1)},
        profile_summary=None,
        relevance_grades={101: 3, 102: 2, 103: 1, 104: 0},
        hard_constraints={},
        must_exclude_product_ids=(),
        slices=("search", "guest", "single_need"),
    )


def test_build_case_input_applies_hard_filters_before_recording_search_ranks() -> None:
    case = _case(
        hard_constraints={
            "priceMax": 30_000,
            "forbiddenCategories": ["차단"],
        }
    )

    result = build_case_input(case, _fixtures(case))

    assert [product.product_id for product in result.candidates] == [3, 1]
    assert all(isinstance(product, SpringProduct) for product in result.candidates)
    assert result.search_rank_by_id == {3: 1, 1: 2}
    assert result.profile_summary is None
    assert result.hard_constraints["priceMax"] == 30_000


def test_build_case_input_renders_case_specific_profile_for_member() -> None:
    case = _case(case_id="buy-srch-9002", identity_kind="member")

    result = build_case_input(case, _fixtures(case))

    assert result.profile_summary is not None
    assert "Acme" in result.profile_summary
    assert "마우스" in result.profile_summary
    assert result.profile_summary != "persona-1"


async def test_structured_and_hybrid_share_one_scored_provider_response() -> None:
    provider = ScriptedScoringLLM()

    result = await run_case_arms(
        _case_input(),
        provider,
        arms=("structured", "hybrid"),
        grounding_arm="validated",
        expose_max=4,
        order_seed=7,
    )

    assert provider.scored_calls == 1
    assert result["structured"].raw_response_sha256 == result["hybrid"].raw_response_sha256
    assert result["structured"].provider_called is True
    assert result["hybrid"].provider_called is False
    assert result["structured"].failure is None
    assert result["hybrid"].failure is None


async def test_scripted_provider_covers_current_structured_and_hybrid() -> None:
    result = await run_case_arms(
        _case_input(),
        ScriptedScoringLLM(),
        arms=("current", "structured", "hybrid"),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )

    assert set(result) == {"current", "structured", "hybrid"}
    for arm_result in result.values():
        assert arm_result.sample is not None
        assert arm_result.sample.ranked_product_ids
        assert arm_result.sample.top3_product_ids == arm_result.sample.ranked_product_ids[:3]
        assert arm_result.sample.top1_product_id == arm_result.sample.ranked_product_ids[0]
        assert arm_result.sample.raw_response_sha256
        assert all(decision.supported for decision in arm_result.sample.grounding_decisions)


async def test_prompt_permutation_is_seeded_but_search_ranks_stay_original() -> None:
    input_value = _case_input()

    first = await run_case_arms(
        input_value,
        ScriptedScoringLLM(),
        arms=("hybrid",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )
    repeated = await run_case_arms(
        input_value,
        ScriptedScoringLLM(),
        arms=("hybrid",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )
    changed = await run_case_arms(
        input_value,
        ScriptedScoringLLM(),
        arms=("hybrid",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=29,
    )

    first_sample = first["hybrid"].sample
    repeated_sample = repeated["hybrid"].sample
    changed_sample = changed["hybrid"].sample
    assert first_sample is not None and repeated_sample is not None and changed_sample is not None
    assert first_sample.candidate_order == repeated_sample.candidate_order
    assert first_sample.candidate_order != changed_sample.candidate_order
    assert {row.product_id: row.search_rank for row in first_sample.ranking_decisions} == dict(
        input_value.search_rank_by_id
    )


@pytest.mark.parametrize(
    ("mode", "expected_field", "expected_value"),
    [
        ("duplicate", "duplicate_evaluation_count", 1),
        ("missing", "partial_fallback", True),
        ("out_of_range", "invalid_score_count", 1),
        ("out_of_candidate", "foreign_evaluation_count", 1),
    ],
)
async def test_scripted_fault_modes_are_recorded(
    mode: str, expected_field: str, expected_value: object
) -> None:
    result = await run_case_arms(
        _case_input(),
        ScriptedScoringLLM(mode=mode),
        arms=("structured",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )

    sample = result["structured"].sample
    assert sample is not None
    assert getattr(sample, expected_field) == expected_value


async def test_all_invalid_scores_are_failures_not_zero_quality_samples() -> None:
    one_candidate = replace(
        _case_input(),
        candidates=_case_input().candidates[:1],
        search_rank_by_id={101: 1},
    )

    result = await run_case_arms(
        one_candidate,
        ScriptedScoringLLM(mode="out_of_range"),
        arms=("structured",),
        grounding_arm="validated",
        expose_max=1,
        order_seed=11,
    )

    assert result["structured"].sample is None
    assert result["structured"].failure is not None
    assert result["structured"].failure.error_type == "LLMError"


class _FailOnceScoredLLM(ScriptedScoringLLM):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def complete(self, **kwargs) -> str:
        if "evaluations" in kwargs["system"] and not self.failed:
            self.failed = True
            raise RuntimeError("org-secret-123456 should be scrubbed")
        return await super().complete(**kwargs)


async def test_probe_retries_bounded_failures_and_keeps_them_separate() -> None:
    case = _case(hard_constraints={"priceMax": 30_000})

    run = await run_probe(
        _FailOnceScoredLLM(),
        cases=(case,),
        fixtures=_fixtures(case),
        arms=("structured",),
        repeats=1,
        attempt_multiplier=2,
        order_seeds=(11,),
    )

    assert len(run.samples) == 1
    assert run.samples[0].attempt == 2
    assert len(run.failures) == 1
    assert run.failures[0].attempt == 1
    assert run.failures[0].message == "org-*** should be scrubbed"


async def test_probe_stops_after_attempt_budget_is_exhausted() -> None:
    class _AlwaysFail:
        async def complete(self, **kwargs) -> str:
            raise RuntimeError("provider down")

    case = _case(hard_constraints={"priceMax": 30_000})
    run = await run_probe(
        _AlwaysFail(),
        cases=(case,),
        fixtures=_fixtures(case),
        arms=("structured", "hybrid"),
        repeats=1,
        attempt_multiplier=2,
        order_seeds=(11,),
    )

    assert run.samples == ()
    assert len(run.failures) == 4
    assert {failure.attempt for failure in run.failures} == {1, 2}
