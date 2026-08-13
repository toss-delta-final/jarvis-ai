from __future__ import annotations

import pytest

from app.agents.buyer.recommendation.rerank_scoring import (
    ScoringSchemaError,
    compute_scored_ranking,
)


def _evaluation(
    product_id: object,
    *,
    intent_fit: object = 4,
    need_fit: object = 3,
    profile_fit: object = 0,
) -> dict[str, object]:
    return {
        "productId": product_id,
        "intentFit": intent_fit,
        "needFit": need_fit,
        "profileFit": profile_fit,
        "rationale": f"상품 {product_id} 근거",
    }


def _decisions_by_id(result):
    return {row.product_id: row for row in result.decisions}


def test_structured_uses_421_rubric_and_profile_only_breaks_equal_query_fit() -> None:
    result = compute_scored_ranking(
        [101, 102, 103],
        [
            _evaluation(101, intent_fit=4, need_fit=3, profile_fit=0),
            _evaluation(102, intent_fit=4, need_fit=3, profile_fit=1),
            _evaluation(103, intent_fit=3, need_fit=3, profile_fit=1),
        ],
        arm="structured",
        profile_available=True,
        alpha=0.65,
        k=60,
    )

    assert result.ordered_product_ids == (102, 101, 103)
    assert [row.rubric_score for row in result.decisions] == [22, 23, 19]
    assert [row.llm_rank for row in result.decisions] == [2, 1, 3]
    assert [row.final_rank for row in result.decisions] == [2, 1, 3]


@pytest.mark.parametrize("value", [True, False, -1, 5, 1.5, "4", None])
def test_invalid_intent_fit_recovers_that_candidate_by_search_rank(value: object) -> None:
    result = compute_scored_ranking(
        [101, 102],
        [
            _evaluation(101, intent_fit=value),
            _evaluation(102),
        ],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    by_id = _decisions_by_id(result)
    assert result.ordered_product_ids == (102, 101)
    assert by_id[101].score_valid is False
    assert by_id[101].fallback_reason == "invalid_intent_fit"
    assert by_id[101].rubric_score is None
    assert result.invalid_evaluation_count == 1
    assert result.model_items_by_id[101]["rationale"] == "상품 101 근거"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("needFit", -1, "invalid_need_fit"),
        ("needFit", 4, "invalid_need_fit"),
        ("needFit", True, "invalid_need_fit"),
        ("profileFit", -1, "invalid_profile_fit"),
        ("profileFit", 2, "invalid_profile_fit"),
        ("profileFit", False, "invalid_profile_fit"),
    ],
)
def test_other_score_ranges_are_validated(field: str, value: object, reason: str) -> None:
    bad = _evaluation(101)
    bad[field] = value

    result = compute_scored_ranking(
        [101, 102],
        [bad, _evaluation(102)],
        arm="structured",
        profile_available=True,
        alpha=0.65,
        k=60,
    )

    assert _decisions_by_id(result)[101].fallback_reason == reason


def test_duplicate_evaluation_invalidates_that_product_instead_of_trusting_first() -> None:
    result = compute_scored_ranking(
        [101, 102],
        [
            _evaluation(101, intent_fit=4),
            _evaluation(101, intent_fit=0),
            _evaluation(102, intent_fit=3),
        ],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    by_id = _decisions_by_id(result)
    assert result.ordered_product_ids == (102, 101)
    assert by_id[101].fallback_reason == "duplicate_evaluation"
    assert 101 not in result.model_items_by_id
    assert result.duplicate_evaluation_count == 1


def test_missing_candidates_are_appended_in_search_order() -> None:
    result = compute_scored_ranking(
        [101, 102, 103],
        [_evaluation(102)],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    assert result.ordered_product_ids == (102, 101, 103)
    assert [row.fallback_reason for row in result.decisions] == [
        "missing_evaluation",
        None,
        "missing_evaluation",
    ]


def test_out_of_candidate_id_is_audited_but_never_returned() -> None:
    result = compute_scored_ranking(
        [101],
        [_evaluation(999), _evaluation(101)],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    assert result.ordered_product_ids == (101,)
    assert set(result.model_items_by_id) == {101}
    assert result.foreign_evaluation_count == 1


def test_malformed_rows_are_counted_without_hiding_valid_candidates() -> None:
    result = compute_scored_ranking(
        [101],
        ["not-an-object", {"productId": True}, _evaluation(101)],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    assert result.ordered_product_ids == (101,)
    assert result.invalid_evaluation_count == 2


def test_all_invalid_evaluations_raise_schema_error() -> None:
    with pytest.raises(ScoringSchemaError, match="no valid evaluations"):
        compute_scored_ranking(
            [101],
            [_evaluation(101, intent_fit=5)],
            arm="structured",
            profile_available=False,
            alpha=0.65,
            k=60,
        )


def test_profile_absence_requires_zero_profile_fit() -> None:
    with pytest.raises(ScoringSchemaError, match="no valid evaluations"):
        compute_scored_ranking(
            [101],
            [_evaluation(101, profile_fit=1)],
            arm="structured",
            profile_available=False,
            alpha=0.65,
            k=60,
        )


def test_partial_profile_mismatch_has_distinct_fallback_reason() -> None:
    result = compute_scored_ranking(
        [101, 102],
        [_evaluation(101, profile_fit=1), _evaluation(102)],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    assert _decisions_by_id(result)[101].fallback_reason == "profile_fit_without_profile"


def test_hybrid_combines_one_based_search_and_llm_ranks() -> None:
    result = compute_scored_ranking(
        [101, 102],
        [
            _evaluation(101, intent_fit=0, need_fit=0),
            _evaluation(102, intent_fit=4, need_fit=3),
        ],
        arm="hybrid",
        profile_available=False,
        alpha=0.65,
        k=60,
    )

    effective_alpha = 0.65 + 0.35 / 23
    by_id = _decisions_by_id(result)
    assert by_id[101].final_score == pytest.approx(
        effective_alpha / 61 + (1 - effective_alpha) / 62
    )
    assert by_id[102].final_score == pytest.approx(
        effective_alpha / 62 + (1 - effective_alpha) / 61
    )
    assert result.ordered_product_ids == (101, 102)


def test_prompt_permutation_does_not_change_explicit_search_ranks() -> None:
    result = compute_scored_ranking(
        [102, 101],
        [_evaluation(102), _evaluation(101)],
        arm="hybrid",
        profile_available=False,
        alpha=0.65,
        k=60,
        search_rank_by_id={101: 1, 102: 2},
    )

    assert {row.product_id: row.search_rank for row in result.decisions} == {101: 1, 102: 2}
    assert result.ordered_product_ids == (101, 102)
    assert [row.product_id for row in result.decisions] == [101, 102]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"arm": "current"},
        {"arm": "unknown"},
        {"alpha": True},
        {"alpha": -0.1},
        {"alpha": 1.1},
        {"k": True},
        {"k": 0},
    ],
)
def test_invalid_ranker_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    config = {
        "arm": "structured",
        "profile_available": False,
        "alpha": 0.65,
        "k": 60,
    }
    config.update(kwargs)

    with pytest.raises(ScoringSchemaError):
        compute_scored_ranking([101], [_evaluation(101)], **config)


@pytest.mark.parametrize(
    "candidate_ids",
    [
        [True],
        [101, 101],
        [101, "102"],
        [],
    ],
)
def test_invalid_candidate_contract_is_rejected(candidate_ids: list[object]) -> None:
    with pytest.raises(ScoringSchemaError):
        compute_scored_ranking(
            candidate_ids,
            [_evaluation(101)],
            arm="structured",
            profile_available=False,
            alpha=0.65,
            k=60,
        )


@pytest.mark.parametrize(
    "search_rank_by_id",
    [
        {101: 0, 102: 1},
        {101: 1, 102: True},
        {101: 1, 102: 1},
        {101: 1},
        {101: 1, 102: 3},
    ],
)
def test_invalid_explicit_search_rank_contract_is_rejected(
    search_rank_by_id: dict[int, object],
) -> None:
    with pytest.raises(ScoringSchemaError):
        compute_scored_ranking(
            [101, 102],
            [_evaluation(101), _evaluation(102)],
            arm="hybrid",
            profile_available=False,
            alpha=0.65,
            k=60,
            search_rank_by_id=search_rank_by_id,
        )


def test_boolean_search_rank_key_cannot_alias_product_id_one() -> None:
    with pytest.raises(ScoringSchemaError):
        compute_scored_ranking(
            [1, 2],
            [_evaluation(1), _evaluation(2)],
            arm="hybrid",
            profile_available=False,
            alpha=0.65,
            k=60,
            search_rank_by_id={True: 1, 2: 2},
        )


def test_non_list_evaluations_are_rejected() -> None:
    with pytest.raises(ScoringSchemaError, match="evaluations must be a list"):
        compute_scored_ranking(
            [101],
            {"productId": 101},
            arm="structured",
            profile_available=False,
            alpha=0.65,
            k=60,
        )


def test_rerank_result_defaults_to_no_ranking_diagnostics() -> None:
    from app.agents.buyer.recommendation.state import RerankResult

    result = RerankResult()

    assert result.ranked == []
    assert result.grounding_decisions == []
    assert result.ranking_decisions == []
