"""위반 네거티브 채널·라벨 provenance(#370) 결정론·검증 테스트.

schema.judge_attr_violation·validate_cases 위반 태그 검증, audit.violationNegativeFill,
backfill_label_provenance/inject_violation_negatives 스크립트의 멱등성·정합성을 다룬다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.goldenset.audit import run_audit
from evals.goldenset.backfill_label_provenance import backfill
from evals.goldenset.inject_violation_negatives import (
    ATTR_VIOLATION_CANDIDATE_CASES,
    find_price_violation_pool,
    retag_category_violations,
    run as run_injection,
)
from evals.goldenset.schema import (
    DATASET_VERSION,
    SCHEMA_VERSION,
    GoldenCase,
    judge_attr_violation,
    validate_cases,
)

# ---------------------------------------------------------------------------
# schema.judge_attr_violation
# ---------------------------------------------------------------------------


def test_judge_attr_violation_true_when_key_present_and_value_differs() -> None:
    assert judge_attr_violation({"소재": "린넨"}, {"소재": "면"}) is True


def test_judge_attr_violation_false_when_key_missing_from_product() -> None:
    # 케이스 조건 키가 catalog attributes에 아예 없으면(동의어 등) 판정 불가 — False다.
    assert judge_attr_violation({"차단지수": "SPF50"}, {"SPF지수": "SPF50+"}) is False


def test_judge_attr_violation_false_when_values_match() -> None:
    assert judge_attr_violation({"소재": "린넨"}, {"소재": "린넨"}) is False


def test_judge_attr_violation_false_when_conditions_empty() -> None:
    assert judge_attr_violation({}, {"소재": "면"}) is False


# ---------------------------------------------------------------------------
# schema.validate_cases 위반 태그 검증
# ---------------------------------------------------------------------------


def _config():
    from types import SimpleNamespace

    return SimpleNamespace(
        goldenset_min_cases=1,
        goldenset_max_cases=50,
        goldenset_min_ranking_candidates=1,
        goldenset_target_candidates=30,
        goldenset_max_relevant_ratio=1.0,
    )


def _product(
    product_id: int, *, price: int | None = 10_000, category: str = "카테고리A", attributes=None
) -> dict:
    return {
        "productId": product_id,
        "name": f"상품{product_id}",
        "summary": "테스트 상품",
        "attributes": attributes or {},
        "price": price,
        "rating": 4.5,
        "reviewCount": 10,
        "categoryName": category,
        "brandName": "브랜드",
    }


def _candidate(product_id: int, *, rule: str | None = None, source: str = "golden_filter") -> dict:
    return {
        "productId": product_id,
        "source": source,
        "rule": rule,
        "from": "primary" if rule is None else rule,
    }


def _fixture(candidates: list[dict]) -> dict:
    ids = sorted({c["productId"] for c in candidates})
    return {
        "request": {"keyword": "테스트"},
        "productIds": ids,
        "totalCount": len(ids),
        "recordedAt": "2026-08-06T00:00:00+09:00",
        "source": "live-spring-i1",
        "candidates": candidates,
    }


def _raw(
    case_id: str = "buy-srch-0001",
    *,
    fixture_id: str = "fixture-1",
    relevant: list[int] | None = None,
    must_exclude: list[int] | None = None,
    forbidden_product_ids: list[int] | None = None,
    forbidden_categories: list[str] | None = None,
    price_max: int | None = None,
    price_min: int | None = None,
    expected_filters: dict | None = None,
    test_type: str = "MFT",
) -> dict:
    relevant = relevant if relevant is not None else [1]
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
        "expectedFilters": expected_filters or {"keyword": "테스트"},
        "searchFixtureId": fixture_id,
        "relevantProductIds": relevant,
        "relevanceGrades": {str(pid): 3 for pid in relevant},
        "idealOrder": relevant,
        "hardConstraints": {
            "priceMax": price_max,
            "priceMin": price_min,
            "forbiddenCategories": forbidden_categories or [],
            "forbiddenProductIds": forbidden_product_ids or [],
        },
        "mustExcludeProductIds": must_exclude or [],
        "provenance": "curated",
        "labeler": "labeler-01",
        "adjudicator": None,
        "createdAt": "2026-08-06",
        "notes": "테스트 근거.",
        "testType": test_type,
        "labelSource": "model",
        "labeledAt": "2026-08-06",
        "labelRationale": "테스트 라벨 근거.",
    }


def _validate(raw: dict, *, catalog: dict, fixture: dict) -> None:
    case = GoldenCase.model_validate(raw)
    validate_cases(
        [case],
        catalog=catalog,
        search_responses={raw["searchFixtureId"]: fixture},
        purchase_history={},
        config=_config(),
    )


def test_price_violation_candidate_must_actually_violate_price_bounds() -> None:
    raw = _raw(price_max=10_000)
    fixture = _fixture([_candidate(1), _candidate(2, rule="price_violation", source="injected")])
    catalog = {"1": _product(1, price=5_000), "2": _product(2, price=5_000)}  # 2는 위반 아님
    with pytest.raises(ValueError, match="위반하지 않습니다"):
        _validate(raw, catalog=catalog, fixture=fixture)


def test_price_violation_candidate_requires_a_price() -> None:
    raw = _raw(price_max=10_000)
    fixture = _fixture([_candidate(1), _candidate(2, rule="price_violation", source="injected")])
    catalog = {"1": _product(1, price=5_000), "2": _product(2, price=None)}
    with pytest.raises(ValueError, match="가격이"):
        _validate(raw, catalog=catalog, fixture=fixture)


def test_price_violation_candidate_passes_when_actually_over_price_max() -> None:
    raw = _raw(price_max=10_000)
    fixture = _fixture([_candidate(1), _candidate(2, rule="price_violation", source="injected")])
    catalog = {"1": _product(1, price=5_000), "2": _product(2, price=50_000)}
    _validate(raw, catalog=catalog, fixture=fixture)  # no raise


def test_category_violation_candidate_must_be_forbidden_or_excluded() -> None:
    raw = _raw(must_exclude=[])
    fixture = _fixture(
        [_candidate(1), _candidate(2, rule="category_violation", source="golden_filter")]
    )
    catalog = {"1": _product(1), "2": _product(2)}
    with pytest.raises(ValueError, match="forbiddenCategories에도"):
        _validate(raw, catalog=catalog, fixture=fixture)


def test_category_violation_candidate_passes_via_must_exclude() -> None:
    raw = _raw(must_exclude=[2])
    fixture = _fixture(
        [_candidate(1), _candidate(2, rule="category_violation", source="golden_filter")]
    )
    catalog = {"1": _product(1), "2": _product(2)}
    _validate(raw, catalog=catalog, fixture=fixture)  # no raise


def test_category_violation_candidate_passes_via_forbidden_category() -> None:
    raw = _raw(forbidden_categories=["금지카테고리"])
    fixture = _fixture([_candidate(1), _candidate(2, rule="category_violation", source="injected")])
    catalog = {"1": _product(1), "2": _product(2, category="금지카테고리")}
    _validate(raw, catalog=catalog, fixture=fixture)  # no raise


def test_attr_violation_candidate_requires_attr_conditions_in_expected_filters() -> None:
    raw = _raw(expected_filters={"keyword": "테스트"})
    fixture = _fixture([_candidate(1), _candidate(2, rule="attr_violation", source="injected")])
    catalog = {"1": _product(1), "2": _product(2, attributes={"소재": "면"})}
    with pytest.raises(ValueError, match="attrConditions가 없습니다"):
        _validate(raw, catalog=catalog, fixture=fixture)


def test_attr_violation_candidate_must_actually_violate() -> None:
    raw = _raw(expected_filters={"keyword": "테스트", "attrConditions": {"소재": "린넨"}})
    fixture = _fixture([_candidate(1), _candidate(2, rule="attr_violation", source="injected")])
    catalog = {"1": _product(1), "2": _product(2, attributes={"소재": "린넨"})}  # 일치, 위반 아님
    with pytest.raises(ValueError, match="판정할 수"):
        _validate(raw, catalog=catalog, fixture=fixture)


def test_attr_violation_candidate_passes_when_attribute_actually_differs() -> None:
    raw = _raw(expected_filters={"keyword": "테스트", "attrConditions": {"소재": "린넨"}})
    fixture = _fixture([_candidate(1), _candidate(2, rule="attr_violation", source="injected")])
    catalog = {"1": _product(1), "2": _product(2, attributes={"소재": "면"})}
    _validate(raw, catalog=catalog, fixture=fixture)  # no raise


def test_violation_candidate_cannot_be_relevant_even_with_approval_marker() -> None:
    raw = _raw(price_max=10_000, relevant=[1, 2])
    raw["notes"] = "injected-relevant-approved: 승인."
    fixture = _fixture([_candidate(1), _candidate(2, rule="price_violation", source="injected")])
    catalog = {"1": _product(1, price=5_000), "2": _product(2, price=50_000)}
    with pytest.raises(ValueError, match="정답이 될 수 없습니다"):
        _validate(raw, catalog=catalog, fixture=fixture)


def test_violation_candidate_fixture_must_be_owned_by_exactly_one_case() -> None:
    shared_fixture_id = "fixture-shared"
    raw_a = _raw("buy-srch-0001", fixture_id=shared_fixture_id, price_max=10_000)
    raw_b = _raw("buy-srch-0002", fixture_id=shared_fixture_id, price_max=10_000, relevant=[3])
    fixture = _fixture(
        [_candidate(1), _candidate(3), _candidate(2, rule="price_violation", source="injected")]
    )
    catalog = {
        "1": _product(1, price=5_000),
        "2": _product(2, price=50_000),
        "3": _product(3, price=5_000),
    }
    case_a = GoldenCase.model_validate(raw_a)
    case_b = GoldenCase.model_validate(raw_b)
    with pytest.raises(ValueError, match="정확히 1개 케이스가 단독"):
        validate_cases(
            [case_a, case_b],
            catalog=catalog,
            search_responses={shared_fixture_id: fixture},
            purchase_history={},
            config=_config(),
        )


def test_violation_candidate_owner_must_be_mft() -> None:
    raw = _raw(price_max=10_000, test_type="DIR", relevant=[])
    raw["relevanceGrades"] = {}
    raw["idealOrder"] = []
    fixture = _fixture([_candidate(1), _candidate(2, rule="price_violation", source="injected")])
    catalog = {"1": _product(1, price=5_000), "2": _product(2, price=50_000)}
    with pytest.raises(ValueError, match="MFT여야"):
        _validate(raw, catalog=catalog, fixture=fixture)


# ---------------------------------------------------------------------------
# audit.violationNegativeFill
# ---------------------------------------------------------------------------


def test_run_audit_reports_violation_negative_fill_for_committed_dataset() -> None:
    report = run_audit(write=False)
    fill = report["violationNegativeFill"]["dev"]
    assert fill["price_violation"]["actualCases"] >= 8
    assert all(count >= 2 for count in fill["price_violation"]["casesCandidateCounts"].values())
    assert fill["category_violation"]["actualCases"] >= 4
    assert all(count >= 1 for count in fill["category_violation"]["casesCandidateCounts"].values())


def test_committed_dataset_meets_violation_negative_quota_from_manifest() -> None:
    manifest = json.loads(Path("evals/goldenset/manifest.json").read_text(encoding="utf-8"))
    quotas = manifest["violationNegatives"]["rules"]
    report = run_audit(write=False)
    fill = report["violationNegativeFill"]["dev"]
    for rule, quota in quotas.items():
        actual_cases = fill[rule]["actualCases"]
        actual_candidates = fill[rule]["actualCandidates"]
        # 회귀 고정 — manifest에 기록된 실채움과 실제 감사 결과가 어긋나면 실패한다.
        assert actual_cases == quota["actualCases"], rule
        assert actual_candidates == quota["actualCandidates"], rule
        if quota["shortfallReason"] is None:
            assert actual_cases >= quota["minCases"], rule
            for count in fill[rule]["casesCandidateCounts"].values():
                assert count >= quota["minPerCase"], rule


# ---------------------------------------------------------------------------
# backfill_label_provenance
# ---------------------------------------------------------------------------


def _legacy_case(case_id: str, *, split: str = "dev") -> dict:
    raw = _raw(case_id)
    raw["schemaVersion"] = "2.0.0"
    raw["datasetVersion"] = "2.1.0"
    raw["split"] = split
    del raw["labelSource"]
    del raw["labeledAt"]
    del raw["labelRationale"]
    return raw


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_backfill_adds_label_provenance_and_bumps_versions(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    dev_raw = _legacy_case("buy-srch-0001")
    holdout_raw = _legacy_case("buy-srch-1001", split="holdout")
    for key in (
        "relevantProductIds",
        "relevanceGrades",
        "idealOrder",
        "hardConstraints",
        "mustExcludeProductIds",
    ):
        holdout_raw.pop(key, None)
    _write_jsonl(root / "cases" / "buyer_dev.jsonl", [dev_raw])
    _write_jsonl(root / "cases" / "buyer_holdout.jsonl", [holdout_raw])

    backfill(root=root)

    dev_after = json.loads((root / "cases" / "buyer_dev.jsonl").read_text().splitlines()[0])
    holdout_after = json.loads((root / "cases" / "buyer_holdout.jsonl").read_text().splitlines()[0])
    for case in (dev_after, holdout_after):
        assert case["labelSource"] == "model"
        assert case["labeledAt"] == "2026-08-06"
        assert case["labelRationale"]
        assert case["schemaVersion"] == SCHEMA_VERSION
        assert case["datasetVersion"] == DATASET_VERSION


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    _write_jsonl(root / "cases" / "buyer_dev.jsonl", [_legacy_case("buy-srch-0001")])
    holdout_raw = _legacy_case("buy-srch-1001", split="holdout")
    for key in (
        "relevantProductIds",
        "relevanceGrades",
        "idealOrder",
        "hardConstraints",
        "mustExcludeProductIds",
    ):
        holdout_raw.pop(key, None)
    _write_jsonl(root / "cases" / "buyer_holdout.jsonl", [holdout_raw])

    backfill(root=root)
    first = (root / "cases" / "buyer_dev.jsonl").read_bytes()
    backfill(root=root)
    second = (root / "cases" / "buyer_dev.jsonl").read_bytes()

    assert first == second


# ---------------------------------------------------------------------------
# inject_violation_negatives
# ---------------------------------------------------------------------------


def _min_dev_case(case_id: str = "buy-srch-0001", **overrides) -> dict:
    return _raw(case_id, **overrides)


def test_find_price_violation_pool_merges_multiple_relevant_categories() -> None:
    raw = _min_dev_case(price_max=10_000, relevant=[1, 2])
    case = GoldenCase.model_validate(raw)
    catalog = {
        "1": _product(1, price=5_000, category="A"),
        "2": _product(2, price=5_000, category="B"),
        "3": _product(3, price=50_000, category="A"),  # 위반, 카테고리 A
        "4": _product(4, price=50_000, category="B"),  # 위반, 카테고리 B
        "5": _product(5, price=50_000, category="C"),  # 위반이지만 무관 카테고리
    }
    pool = find_price_violation_pool(case, catalog, frozenset({1, 2}))
    assert [c["productId"] for c in pool] == [3, 4]


def test_inject_price_violations_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    raw = _min_dev_case("buy-budg-0002", price_max=10_000, relevant=[1])
    _write_jsonl(root / "cases" / "buyer_dev.jsonl", [raw])
    catalog = {
        "1": _product(1, price=5_000, category="A"),
        **{str(pid): _product(pid, price=50_000, category="A") for pid in range(2, 10)},
    }
    (root / "fixtures").mkdir(parents=True, exist_ok=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text(json.dumps(catalog), encoding="utf-8")
    fixture = _fixture([_candidate(1)])
    (root / "fixtures" / "search_responses.json").write_text(
        json.dumps({"fixture-1": fixture}), encoding="utf-8"
    )

    price_result_1, _ = run_injection(
        root=root, price_target_case_ids=("buy-budg-0002",), category_targets={}
    )
    first_bytes = (root / "fixtures" / "search_responses.json").read_bytes()
    price_result_2, _ = run_injection(
        root=root, price_target_case_ids=("buy-budg-0002",), category_targets={}
    )
    second_bytes = (root / "fixtures" / "search_responses.json").read_bytes()

    assert first_bytes == second_bytes
    assert price_result_1 == price_result_2
    assert len(price_result_1["buy-budg-0002"]) == 4  # 케이스당 주입 상한


def test_retag_category_violations_does_not_add_new_candidates() -> None:
    raw = _min_dev_case("buy-cmap-0004", must_exclude=[2])
    case = GoldenCase.model_validate(raw)
    fixture = {"candidates": [_candidate(1), _candidate(2)]}
    fixtures = {"fixture-1": fixture}
    result = retag_category_violations(
        {"buy-cmap-0004": case}, fixtures, targets={"buy-cmap-0004": [2]}
    )
    assert result == {"buy-cmap-0004": [2]}
    assert len(fixture["candidates"]) == 2  # append 없음, 재태깅만
    retagged = next(c for c in fixture["candidates"] if c["productId"] == 2)
    assert retagged["rule"] == "category_violation"
    assert retagged["source"] == "golden_filter"  # 원 provenance 보존
    assert retagged["from"] == "primary"  # #370 리뷰 라운드2 F-1 — from은 재태깅 대상이 아니다


def test_retag_category_violations_preserves_from_even_when_rule_was_already_set() -> None:
    # #370 리뷰 라운드2 F-1 실측 재현 — buy-over-0003의 두 후보는 재태깅 전 이미
    # rule="broadened_search"였다. rule을 category_violation으로 덮어쓰는 것은 유지하되
    # from(채굴 출처)은 절대 건드리지 않아야 한다.
    raw = _min_dev_case("buy-over-0003", forbidden_product_ids=[2])
    case = GoldenCase.model_validate(raw)
    fixture = {
        "candidates": [
            _candidate(1),
            {
                "productId": 2,
                "source": "golden_filter",
                "rule": "broadened_search",
                "from": "keyword-only",
            },
        ]
    }
    fixtures = {"fixture-1": fixture}

    retag_category_violations({"buy-over-0003": case}, fixtures, targets={"buy-over-0003": [2]})

    retagged = next(c for c in fixture["candidates"] if c["productId"] == 2)
    assert retagged["rule"] == "category_violation"  # broadened_search → category_violation
    assert retagged["from"] == "keyword-only"  # 채굴 출처는 그대로 복구 가능


def test_attr_violation_shortfall_is_documented_for_all_three_cases() -> None:
    assert set(ATTR_VIOLATION_CANDIDATE_CASES) == {
        "buy-fail-0001",
        "buy-mult-0001",
        "buy-mult-0002",
    }
    assert all(reason.strip() for reason in ATTR_VIOLATION_CANDIDATE_CASES.values())
