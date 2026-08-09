"""구매자 골든셋 스키마와 설정 불변식 회귀 테스트."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from evals.goldenset.loader import ROOT, load_cases
from evals.goldenset.schema import DATASET_VERSION, SCHEMA_VERSION, GoldenCase, validate_cases


def _config(
    min_cases: int = 1,
    max_cases: int = 50,
    min_ranking_candidates: int = 1,
    target_candidates: int = 30,
    max_relevant_ratio: float = 1.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        goldenset_min_cases=min_cases,
        goldenset_max_cases=max_cases,
        goldenset_min_ranking_candidates=min_ranking_candidates,
        goldenset_target_candidates=target_candidates,
        goldenset_max_relevant_ratio=max_relevant_ratio,
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


def _fixture(product_ids: list[int], *, injected_ids: list[int] | None = None) -> dict:
    """v2 candidates provenance를 갖춘 search fixture 페이로드(#333 §2.1)."""
    injected = set(injected_ids or [])
    return {
        "request": {"keyword": "테스트"},
        "productIds": sorted(set(product_ids)),
        "totalCount": len(set(product_ids)),
        "recordedAt": "2026-08-02T00:00:00+09:00",
        "source": "live-spring-i1",
        "candidates": [
            {
                "productId": product_id,
                "source": "injected" if product_id in injected else "golden_filter",
                "rule": "semantic_near" if product_id in injected else None,
                "from": "primary",
            }
            for product_id in sorted(set(product_ids))
        ],
    }


def _raw(case_id: str = "buy-srch-0001") -> dict:
    return {
        "caseId": case_id,
        "schemaVersion": SCHEMA_VERSION,
        "datasetVersion": DATASET_VERSION,
        "split": "dev",
        "slices": ["search", "guest", "single_need"],
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
        "testType": "MFT",
        "labelSource": "model",
        "labeledAt": "2026-08-06",
        "labelRationale": "테스트 라벨 근거.",
    }


def _validate(raws: list[dict]) -> list[GoldenCase]:
    cases = [GoldenCase.model_validate(raw) for raw in raws]
    validate_cases(
        cases,
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": _fixture([1, 2])},
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
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": _fixture([1, 2])},
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
    raw["slices"] = ["search", "member", "single_need"]
    case = GoldenCase.model_validate(raw)
    with pytest.raises(ValueError, match="페르소나"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": _fixture([1, 2])},
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
            search_responses={"fixture-1": _fixture([1, 2])},
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
            search_responses={"fixture-1": _fixture([1, 2])},
            purchase_history={},
            config=_config(),
        )


def test_case_count_changes_when_configured_range_changes() -> None:
    case = GoldenCase.model_validate(_raw())
    with pytest.raises(ValueError, match="케이스 수"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": _fixture([1, 2])},
            purchase_history={},
            config=_config(min_cases=2),
        )
    validate_cases(
        [case],
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": _fixture([1, 2])},
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
        {"goldenset_min_ranking_candidates": 0},
        {"goldenset_min_ranking_candidates": 31, "goldenset_target_candidates": 30},
    ],
)
def test_goldenset_config_rejects_invalid_tunables(overrides: dict) -> None:
    with pytest.raises(ValueError, match="골든셋"):
        Settings(**overrides)


def test_needs_slice_must_be_exactly_one() -> None:
    for slices in (
        ["search", "guest"],  # 니즈 슬라이스 0개
        ["search", "guest", "single_need", "budget"],  # 니즈 슬라이스 2개
    ):
        raw = _raw()
        raw["slices"] = slices
        with pytest.raises(ValidationError, match="니즈 슬라이스"):
            GoldenCase.model_validate(raw)


def test_member_slice_must_match_member_identity() -> None:
    raw = _raw()
    raw["identity"] = {"kind": "member", "personaId": "persona-1"}
    raw["slices"] = ["search", "single_need"]  # member 슬라이스 누락
    with pytest.raises(ValidationError, match="member"):
        GoldenCase.model_validate(raw)

    raw = _raw()
    raw["slices"] = ["search", "guest", "single_need", "member"]  # guest인데 member도 있음
    with pytest.raises(ValidationError, match="member"):
        GoldenCase.model_validate(raw)


def test_inv_dir_cases_are_exempt_from_search_relevant_not_empty() -> None:
    raw = _raw()
    raw["testType"] = "INV"
    raw["behaviorGroupId"] = "color-synonym-0001"
    raw["behaviorKind"] = "color_synonym"
    raw["relevantProductIds"] = []
    raw["relevanceGrades"] = {}
    raw["idealOrder"] = []
    raw["mustExcludeProductIds"] = []
    case = GoldenCase.model_validate(raw)
    assert case.test_type == "INV"
    assert case.behavior_kind == "color_synonym"


def test_mft_search_case_still_requires_relevant_ids() -> None:
    raw = _raw()
    raw["relevantProductIds"] = []
    raw["relevanceGrades"] = {}
    raw["idealOrder"] = []
    with pytest.raises(ValidationError, match="relevantProductIds"):
        GoldenCase.model_validate(raw)


def test_injected_candidate_in_relevant_ids_requires_approval_note() -> None:
    raw = _raw()
    raw["relevantProductIds"] = [1, 2]
    raw["relevanceGrades"] = {"1": 3, "2": 2}
    raw["idealOrder"] = [1, 2]
    raw["mustExcludeProductIds"] = []
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2], injected_ids=[2])
    with pytest.raises(ValueError, match="injected-relevant-approved"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": fixture},
            purchase_history={},
            config=_config(),
        )


def test_injected_candidate_in_relevant_ids_is_allowed_with_approval_note() -> None:
    raw = _raw()
    raw["relevantProductIds"] = [1, 2]
    raw["relevanceGrades"] = {"1": 3, "2": 2}
    raw["idealOrder"] = [1, 2]
    raw["mustExcludeProductIds"] = []
    raw["notes"] = "injected-relevant-approved: adjudicator-02가 관련 있다고 판단."
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2], injected_ids=[2])
    validate_cases(
        [case],
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": fixture},
        purchase_history={},
        config=_config(),
    )


def test_ranking_eligible_case_below_min_candidates_requires_narrow_domain_note() -> None:
    raw = _raw()
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2, 3])  # relevant={1}, candidates=3 < min_ranking_candidates=20
    with pytest.raises(ValueError, match="narrow-domain"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2), "3": _product(3)},
            search_responses={"fixture-1": fixture},
            purchase_history={},
            config=_config(min_ranking_candidates=20),
        )


def test_ranking_eligible_case_below_min_candidates_is_allowed_with_narrow_domain_note() -> None:
    raw = _raw()
    raw["notes"] = "narrow-domain: 실제로 후보가 이만큼뿐이다."
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2, 3])
    validate_cases(
        [case],
        catalog={"1": _product(1), "2": _product(2), "3": _product(3)},
        search_responses={"fixture-1": fixture},
        purchase_history={},
        config=_config(min_ranking_candidates=20),
    )


def test_all_correct_candidates_are_exempt_from_min_ranking_candidates() -> None:
    # candidateCount(1) <= relevantCount(1)이면 순위 판별력이 없어 하한 검사 대상이 아니다.
    raw = _raw()
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1])
    validate_cases(
        [case],
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": fixture},
        purchase_history={},
        config=_config(min_ranking_candidates=20),
    )


def test_search_fixture_product_ids_must_match_candidate_union() -> None:
    fixture = _fixture([1, 2])
    fixture["productIds"] = [1]  # candidates는 여전히 {1,2}
    raw = _raw()
    case = GoldenCase.model_validate(raw)
    with pytest.raises(ValueError, match="검색 fixture 검증 실패"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2)},
            search_responses={"fixture-1": fixture},
            purchase_history={},
            config=_config(),
        )


def test_fewer_candidates_than_relevant_but_with_a_wrong_candidate_is_not_all_correct() -> None:
    # F-1(#333 리뷰): 후보 수(2) < 정답 수(3)라도 후보에 오답(product 2)이 섞여 있으면
    # "전부 정답"이 아니다 — 개수 비교였다면 이 케이스를 비판별로 잘못 넘겼을 것이다.
    raw = _raw()
    raw["relevantProductIds"] = [1, 3, 4]
    raw["relevanceGrades"] = {"1": 3, "3": 2, "4": 1}
    raw["idealOrder"] = [1, 3, 4]
    raw["mustExcludeProductIds"] = []
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2])  # 2는 relevant가 아닌 오답 후보
    with pytest.raises(ValueError, match="narrow-domain"):
        validate_cases(
            [case],
            catalog={"1": _product(1), "2": _product(2), "3": _product(3), "4": _product(4)},
            search_responses={"fixture-1": fixture},
            purchase_history={},
            config=_config(min_ranking_candidates=20),
        )


def test_fixture_candidate_not_in_catalog_is_rejected() -> None:
    # F-2(#333 리뷰): 라벨된 id만이 아니라 fixture candidate 전원이 catalog에 있어야 한다.
    raw = _raw()
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2])
    with pytest.raises(ValueError, match="catalog에 없는 candidate productId"):
        validate_cases(
            [case],
            catalog={"1": _product(1)},  # 2가 없음(라벨되지 않은 후보라도 걸려야 한다)
            search_responses={"fixture-1": fixture},
            purchase_history={},
            config=_config(),
        )


def test_relevant_ratio_above_cap_requires_exempt_note() -> None:
    raw = _raw()
    raw["relevantProductIds"] = [1]
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2, 3, 4])  # 등급≥1 비율 1/4 = 0.25, 상한 0.2보다 큼
    with pytest.raises(ValueError, match="relevant-ratio-exempt"):
        validate_cases(
            [case],
            catalog={str(i): _product(i) for i in range(1, 5)},
            search_responses={"fixture-1": fixture},
            purchase_history={},
            config=_config(min_ranking_candidates=1, max_relevant_ratio=0.2),
        )


def test_relevant_ratio_above_cap_is_allowed_with_exempt_note() -> None:
    raw = _raw()
    raw["relevantProductIds"] = [1]
    raw["notes"] = "relevant-ratio-exempt: 도메인이 좁아 비율을 낮출 수 없다."
    case = GoldenCase.model_validate(raw)
    fixture = _fixture([1, 2, 3, 4])
    validate_cases(
        [case],
        catalog={str(i): _product(i) for i in range(1, 5)},
        search_responses={"fixture-1": fixture},
        purchase_history={},
        config=_config(min_ranking_candidates=1, max_relevant_ratio=0.2),
    )


def test_narrow_domain_and_relevant_ratio_exempt_notes_can_be_combined() -> None:
    raw = _raw()
    raw["relevantProductIds"] = [1]
    raw["notes"] = "narrow-domain: relevant-ratio-exempt: 도메인이 좁고 후보도 적다."
    case = GoldenCase.model_validate(raw)
    fixture = _fixture(
        [1, 2]
    )  # 후보 2개(<20, narrow-domain 필요) + 비율 0.5(>0.2, ratio-exempt 필요)
    validate_cases(
        [case],
        catalog={"1": _product(1), "2": _product(2)},
        search_responses={"fixture-1": fixture},
        purchase_history={},
        config=_config(min_ranking_candidates=20, max_relevant_ratio=0.2),
    )


def _dir_raw(case_id: str, *, fixture_id: str, expected_filters: dict, group_id: str) -> dict:
    """DIR constraint_subset 케이스 — 라벨 필드는 비워도 된다(GUIDE v2)."""
    return {
        "caseId": case_id,
        "schemaVersion": SCHEMA_VERSION,
        "datasetVersion": DATASET_VERSION,
        "split": "dev",
        "slices": ["guest", "single_need"],
        "query": "테스트 발화",
        "queryType": "simple",
        "identity": {"kind": "guest", "personaId": None},
        "expectedRoute": "recommend",
        "expectedFilters": expected_filters,
        "searchFixtureId": fixture_id,
        "relevantProductIds": [],
        "relevanceGrades": {},
        "idealOrder": [],
        "hardConstraints": {
            "priceMax": None,
            "priceMin": None,
            "forbiddenCategories": [],
            "forbiddenProductIds": [],
        },
        "mustExcludeProductIds": [],
        "provenance": "synthetic",
        "labeler": "labeler-02",
        "adjudicator": None,
        "createdAt": "2026-08-05",
        "notes": "DIR constraint_subset 테스트.",
        "testType": "DIR",
        "behaviorGroupId": group_id,
        "behaviorKind": "constraint_subset",
        "labelSource": "model",
        "labeledAt": "2026-08-06",
        "labelRationale": "테스트 라벨 근거.",
    }


def test_constraint_subset_group_passes_when_stricter_is_subset_of_relaxed() -> None:
    # #333 라운드2 F-R6 — validate_cases가 로드 시점에 부분집합 관계를 검증한다.
    relaxed = GoldenCase.model_validate(
        _dir_raw(
            "buy-dirc-0001",
            fixture_id="fixture-relaxed",
            expected_filters={"keyword": "테스트"},
            group_id="dir-subset-01",
        )
    )
    stricter = GoldenCase.model_validate(
        _dir_raw(
            "buy-dirc-0002",
            fixture_id="fixture-stricter",
            expected_filters={"keyword": "테스트", "priceMax": 10_000},
            group_id="dir-subset-01",
        )
    )
    validate_cases(
        [relaxed, stricter],
        catalog={str(i): _product(i) for i in range(1, 4)},
        search_responses={
            "fixture-relaxed": _fixture([1, 2, 3]),
            "fixture-stricter": _fixture([1, 2]),
        },
        purchase_history={},
        config=_config(),
    )


def test_constraint_subset_group_fails_when_stricter_exposure_is_not_subset() -> None:
    relaxed = GoldenCase.model_validate(
        _dir_raw(
            "buy-dirc-0001",
            fixture_id="fixture-relaxed",
            expected_filters={"keyword": "테스트"},
            group_id="dir-subset-01",
        )
    )
    stricter = GoldenCase.model_validate(
        _dir_raw(
            "buy-dirc-0002",
            fixture_id="fixture-stricter",
            expected_filters={"keyword": "테스트", "priceMax": 10_000},
            group_id="dir-subset-01",
        )
    )
    with pytest.raises(ValueError, match="부분집합이 아닙니다"):
        validate_cases(
            [relaxed, stricter],
            catalog={str(i): _product(i) for i in range(1, 5)},
            search_responses={
                "fixture-relaxed": _fixture([1, 2]),
                "fixture-stricter": _fixture([1, 4]),  # 4는 완화 쪽에 없다
            },
            purchase_history={},
            config=_config(),
        )


def test_committed_dev_dataset_passes_validate_cases() -> None:
    """#370 리뷰 라운드2 F-4 — 위반 태그 검증이 실제로 커밋된 데이터를 태우는지 고정한다.

    기존 단위 테스트는 전부 합성 fixture만 썼다 — "태그된 후보가 실제로 위반한다"는 이번
    이슈의 핵심 보증이 정작 실제로 싣는 dev 103건 + 실제 catalog/fixture에 대해서는 한 번도
    실행되지 않았다. 실제 `get_settings()` 기본값으로(합성 완화 config 아님) 커밋된 dev
    전체를 검증한다 — 이 테스트가 없으면 위반 태그 검증 로직이 리팩터로 깨져도 잡히지 않는다.
    """
    dev_cases = load_cases("dev")
    assert len(dev_cases) == 109
    catalog = json.loads((ROOT / "fixtures" / "catalog_snapshot.json").read_text())
    search_responses = json.loads((ROOT / "fixtures" / "search_responses.json").read_text())
    purchase_history = json.loads((ROOT / "fixtures" / "purchase_history.json").read_text())

    validate_cases(
        dev_cases,
        catalog=catalog,
        search_responses=search_responses,
        purchase_history=purchase_history,
    )  # config 미지정 — 실 get_settings() 기본값, 합성 완화 config로 갈아끼우지 않는다
