"""구매자 골든셋 스키마와 설정 불변식 회귀 테스트."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from evals.goldenset.schema import GoldenCase, validate_cases


def _config(min_cases: int = 1, max_cases: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        goldenset_min_cases=min_cases,
        goldenset_max_cases=max_cases,
    )


def _product(product_id: int, price: int = 10_000) -> dict:
    return {
        "productId": product_id,
        "name": f"상품 {product_id}",
        "summary": "근거가 충분한 실제 상품 스냅샷",
        "attributes": {},
        "price": price,
        "rating": 4.5,
        "reviewCount": 10,
        "categoryName": "테스트",
        "brandName": "브랜드",
    }


def _raw(case_id: str = "buy-srch-0001") -> dict:
    return {
        "caseId": case_id,
        "schemaVersion": "1.0.0",
        "datasetVersion": "1.0.0",
        "split": "dev",
        "slices": ["search", "guest"],
        "query": "테스트 상품",
        "queryType": "simple",
        "identity": {"kind": "guest", "personaId": None},
        "expectedRoute": "recommend",
        "expectedFilters": {"keyword": "테스트"},
        "searchFixtureId": "fixture-1",
        "relevantProductIds": [1],
        "relevanceGrades": {"1": 3},
        "idealOrder": [1],
        "hardConstraints": {
            "priceMax": None,
            "priceMin": None,
            "forbiddenCategories": [],
            "forbiddenProductIds": [],
        },
        "mustExcludeProductIds": [2],
        "provenance": "curated",
        "labeler": "labeler-01",
        "adjudicator": None,
        "createdAt": "2026-08-02",
        "notes": "상품명과 설명을 읽고 명확한 정답으로 판정했다.",
    }


def _validate(raws: list[dict]) -> list[GoldenCase]:
    cases = [GoldenCase.model_validate(raw) for raw in raws]
    validate_cases(
        cases,
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": {}},
        purchase_history={},
        config=_config(),
    )
    return cases


def test_valid_case_uses_camel_case_wire_format() -> None:
    case = _validate([_raw()])[0]
    dumped = case.model_dump(by_alias=True)
    assert dumped["caseId"] == "buy-srch-0001"
    assert dumped["relevantProductIds"] == [1]


@pytest.mark.parametrize("case_id", ["buy-bad", "srch-0001", "buy-SRCH-0001"])
def test_case_id_must_match_stable_pattern(case_id: str) -> None:
    with pytest.raises(ValidationError):
        GoldenCase.model_validate(_raw(case_id))


def test_case_ids_are_globally_unique() -> None:
    with pytest.raises(ValueError, match="중복"):
        _validate([_raw(), _raw()])


def test_relevant_and_excluded_ids_must_be_disjoint() -> None:
    raw = _raw()
    raw["mustExcludeProductIds"] = [1]
    with pytest.raises(ValidationError, match="교집합"):
        GoldenCase.model_validate(raw)


def test_all_labeled_product_ids_must_exist_in_catalog() -> None:
    raw = _raw()
    raw["mustExcludeProductIds"] = [999]
    case = GoldenCase.model_validate(raw)
    with pytest.raises(ValueError, match="카탈로그"):
        validate_cases(
            [case],
            catalog={"1": _product(1)},
            search_responses={"fixture-1": {}},
            purchase_history={},
            config=_config(),
        )


def test_ideal_order_must_be_exact_permutation() -> None:
    raw = _raw()
    raw["relevantProductIds"] = [1, 2]
    raw["mustExcludeProductIds"] = []
    raw["idealOrder"] = [1]
    raw["relevanceGrades"] = {"1": 3, "2": 2}
    with pytest.raises(ValidationError, match="idealOrder"):
        GoldenCase.model_validate(raw)


@pytest.mark.parametrize(
    ("grades", "message"),
    [({"1": 0}, "1 이상"), ({"2": 3}, "전부"), ({"1": 4}, "3 이하")],
)
def test_relevance_grades_cover_relevant_ids_with_positive_grade(
    grades: dict[str, int], message: str
) -> None:
    raw = _raw()
    raw["relevanceGrades"] = grades
    with pytest.raises(ValidationError, match=message):
        GoldenCase.model_validate(raw)


def test_unknown_search_fixture_is_rejected() -> None:
    case = GoldenCase.model_validate(_raw())
    with pytest.raises(ValueError, match="검색 fixture"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={},
            purchase_history={},
            config=_config(),
        )


def test_unknown_persona_is_rejected() -> None:
    raw = _raw()
    raw["identity"] = {"kind": "member", "personaId": "missing"}
    raw["slices"].remove("guest")
    case = GoldenCase.model_validate(raw)
    with pytest.raises(ValueError, match="페르소나"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": {}},
            purchase_history={},
            config=_config(),
        )


def test_slices_must_be_non_empty_and_allowlisted() -> None:
    for slices in ([], ["unknown"]):
        raw = _raw()
        raw["slices"] = slices
        with pytest.raises(ValidationError):
            GoldenCase.model_validate(raw)


@pytest.mark.parametrize(
    ("identity", "slices"),
    [
        ({"kind": "guest", "personaId": None}, ["search"]),
        ({"kind": "member", "personaId": "persona-1"}, ["search", "guest"]),
    ],
)
def test_guest_slice_must_match_guest_identity(identity: dict, slices: list[str]) -> None:
    raw = _raw()
    raw["identity"] = identity
    raw["slices"] = slices
    with pytest.raises(ValidationError, match="guest"):
        GoldenCase.model_validate(raw)


def test_forbidden_category_rejects_catalog_db_hierarchy_notation() -> None:
    raw = _raw()
    raw["hardConstraints"]["forbiddenCategories"] = ["구기/라켓/스포츠 > 축구"]
    with pytest.raises(ValidationError, match="I-1 categoryName"):
        GoldenCase.model_validate(raw)


def test_forbidden_category_must_exist_in_catalog_snapshot() -> None:
    raw = _raw()
    raw["hardConstraints"]["forbiddenCategories"] = ["실재하지않는카테고리"]
    case = GoldenCase.model_validate(raw)
    with pytest.raises(ValueError, match="금지 카테고리"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": {}},
            purchase_history={},
            config=_config(),
        )


@pytest.mark.parametrize(
    ("field", "value", "price"), [("priceMax", 9_999, 10_000), ("priceMin", 10_001, 10_000)]
)
def test_relevant_products_must_obey_price_constraints(field: str, value: int, price: int) -> None:
    raw = _raw()
    raw["hardConstraints"][field] = value
    case = GoldenCase.model_validate(raw)
    with pytest.raises(ValueError, match="가격"):
        validate_cases(
            [case],
            catalog={"1": _product(1, price), "2": _product(2)},
            search_responses={"fixture-1": {}},
            purchase_history={},
            config=_config(),
        )


def test_case_count_changes_when_configured_range_changes() -> None:
    case = GoldenCase.model_validate(_raw())
    with pytest.raises(ValueError, match="케이스 수"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": {}},
            purchase_history={},
            config=_config(min_cases=2),
        )
    validate_cases(
        [case],
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": {}},
        purchase_history={},
        config=_config(min_cases=1),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"goldenset_min_cases": 0},
        {"goldenset_min_cases": 51, "goldenset_max_cases": 50},
        {"goldenset_near_dup_jaccard_max": 1.0},
        {"goldenset_near_dup_relevant_overlap_max": 0.0},
        {"goldenset_snapshot_per_query_max": 0},
        {"goldenset_holdout_ratio": 1.0},
    ],
)
def test_goldenset_config_rejects_invalid_tunables(overrides: dict) -> None:
    with pytest.raises(ValueError, match="골든셋"):
        Settings(**overrides)
