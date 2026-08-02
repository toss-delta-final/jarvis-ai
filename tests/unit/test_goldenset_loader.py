"""구매자 골든셋 로더·봉인·fixture 호환 회귀 테스트."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evals.goldenset.loader as goldenset_loader
import evals.goldenset.snapshot as goldenset_snapshot
from app.pipelines.compare import recall_at_k
from app.schemas.spring import RecentPurchases, SpringProduct
from evals.goldenset.loader import load_cases, to_compare_golden_cases, unseal_holdout_labels
from evals.goldenset.snapshot import record_snapshots
from tests.integration._stubs import SpringStub

ROOT = Path("evals/goldenset")


def test_committed_dev_and_holdout_are_separate() -> None:
    dev = load_cases("dev")
    holdout = load_cases("holdout")
    assert len(dev) == 31
    assert len(holdout) == 12
    assert {case.split for case in dev} == {"dev"}
    assert {case.split for case in holdout} == {"holdout"}
    assert not hasattr(holdout[0], "relevant_product_ids")


def test_loading_dev_never_opens_holdout_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.open

    def guarded(path: Path, *args, **kwargs):
        if path.name == "buyer_holdout_labels.jsonl":
            raise AssertionError("dev 로더가 봉인 라벨을 열었다")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert load_cases("dev")


@pytest.mark.parametrize(
    ("reason", "sha"), [("", "a" * 40), ("release", "short"), ("release", "g" * 40)]
)
def test_unseal_rejects_empty_reason_or_invalid_sha(reason: str, sha: str) -> None:
    with pytest.raises(ValueError):
        unseal_holdout_labels(reason=reason, commit_sha=sha)


def test_unseal_appends_audit_log(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    (root / "cases").mkdir(parents=True)
    (root / "audit").mkdir()
    (root / "cases" / "buyer_holdout_labels.jsonl").write_text(
        json.dumps(
            {
                "caseId": "buy-srch-9999",
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
                "notes": "release 라벨 판정 근거",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(json.dumps({"datasetHash": "hash-1"}), encoding="utf-8")
    labels = unseal_holdout_labels(reason="release candidate", commit_sha="a" * 40, root=root)
    logged = [
        json.loads(line)
        for line in (root / "audit" / "holdout_runs.jsonl").read_text().splitlines()
    ]
    assert labels[0].case_id == "buy-srch-9999"
    assert logged[0]["commitSha"] == "a" * 40
    assert logged[0]["reason"] == "release candidate"
    assert logged[0]["datasetHash"] == "hash-1"


def test_app_modules_never_reference_sealed_label_filename() -> None:
    offenders = []
    for path in Path("app").rglob("*.py"):
        if "buyer_holdout_labels" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert offenders == []


def test_search_adapter_feeds_existing_recall_harness() -> None:
    source = [case for case in load_cases("dev") if "search" in case.slices]
    adapted = to_compare_golden_cases("dev")
    assert len(adapted) == len(source)
    assert [(case.query, case.relevant_ids) for case in adapted] == [
        (case.query, set(case.relevant_product_ids)) for case in source
    ]
    multi = next(case for case in adapted if len(case.relevant_ids) > 1)
    incomplete = list(multi.relevant_ids)[1:]
    assert recall_at_k(incomplete, multi.relevant_ids, 10) < 1.0


def test_search_adapter_cannot_open_holdout_without_release_gate() -> None:
    assert not hasattr(goldenset_loader, "load_labeled_holdout")
    with pytest.raises(ValueError, match="reason.*commit_sha"):
        to_compare_golden_cases("holdout")


def test_holdout_public_notes_do_not_reveal_labels() -> None:
    holdout = load_cases("holdout")
    labels = {
        row["caseId"]: row
        for row in (
            json.loads(line)
            for line in (ROOT / "cases" / "buyer_holdout_labels.jsonl").read_text().splitlines()
        )
    }
    catalog = json.loads((ROOT / "fixtures" / "catalog_snapshot.json").read_text())
    for case in holdout:
        assert "등급" not in case.notes
        names = [
            catalog[str(product_id)]["name"]
            for product_id in labels[case.case_id]["relevantProductIds"]
        ]
        assert all(name[:8] not in case.notes for name in names)
        assert labels[case.case_id]["notes"]


def test_fixture_requests_equal_case_expected_filters() -> None:
    responses = json.loads((ROOT / "fixtures" / "search_responses.json").read_text())
    for path in (ROOT / "cases" / "buyer_dev.jsonl", ROOT / "cases" / "buyer_holdout.jsonl"):
        for line in path.read_text().splitlines():
            case = json.loads(line)
            assert responses[case["searchFixtureId"]]["request"] == case["expectedFilters"]


def test_holdout_non_failure_cases_have_live_distractors() -> None:
    responses = json.loads((ROOT / "fixtures" / "search_responses.json").read_text())
    labels = {
        row["caseId"]: row
        for row in map(
            json.loads,
            (ROOT / "cases" / "buyer_holdout_labels.jsonl").read_text().splitlines(),
        )
    }
    for case in load_cases("holdout"):
        if "failure" not in case.slices:
            candidates = responses[case.search_fixture_id]["productIds"]
            assert len(candidates) > len(labels[case.case_id]["relevantProductIds"])


def test_private_holdout_accessor_is_only_used_by_audit_module() -> None:
    token = "_load_labeled_holdout_for_audit"
    callers = [
        path.name
        for path in Path("evals/goldenset").glob("*.py")
        if path.name != "loader.py" and token in path.read_text(encoding="utf-8")
    ]
    assert callers == ["audit.py"]


def test_premium_overreach_constraint_is_explicit_in_query() -> None:
    case = next(case for case in load_cases("dev") if case.case_id == "buy-over-0001")
    assert case.hard_constraints.price_min == 70_000
    assert "7만원 이상" in case.query


def test_fixture_contracts_parse_and_spring_stub_accepts_catalog() -> None:
    catalog = json.loads((ROOT / "fixtures" / "catalog_snapshot.json").read_text())
    purchases = json.loads((ROOT / "fixtures" / "purchase_history.json").read_text())
    products = [SpringProduct.model_validate(value) for value in catalog.values()]
    parsed = [RecentPurchases.model_validate(value) for value in purchases.values()]
    stub = SpringStub(catalog=list(catalog.values()))
    assert products and parsed
    assert stub.catalog == list(catalog.values())


@pytest.mark.asyncio
async def test_snapshot_uses_injected_search_and_writes_deterministically(tmp_path: Path) -> None:
    calls = []

    async def fake_search(filters):
        calls.append(filters)
        from app.schemas.spring import ProductSearchResult

        return ProductSearchResult(
            products=[
                SpringProduct(productId=2, name="둘", price=2000),
                SpringProduct(productId=1, name="하나", price=1000),
            ],
            totalCount=2,
        )

    catalog_path = tmp_path / "catalog.json"
    responses_path = tmp_path / "responses.json"
    await record_snapshots(
        {"fixture-z": {"keyword": "테스트"}},
        search=fake_search,
        catalog_path=catalog_path,
        responses_path=responses_path,
        recorded_at="2026-08-02T00:00:00+09:00",
        per_query_max=30,
    )
    first = catalog_path.read_bytes(), responses_path.read_bytes()
    await record_snapshots(
        {"fixture-z": {"keyword": "테스트"}},
        search=fake_search,
        catalog_path=catalog_path,
        responses_path=responses_path,
        recorded_at="2026-08-02T00:00:00+09:00",
        per_query_max=30,
    )
    assert first == (catalog_path.read_bytes(), responses_path.read_bytes())
    assert list(json.loads(catalog_path.read_text())) == ["1", "2"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_snapshot_requires_fixed_recorded_at(tmp_path: Path) -> None:
    async def fake_search(filters):
        raise AssertionError("시각 검증 전에 검색하면 안 됩니다")

    with pytest.raises(TypeError, match="recorded_at"):
        await record_snapshots(
            {"fixture-z": {"keyword": "테스트"}},
            search=fake_search,
            catalog_path=tmp_path / "catalog.json",
            responses_path=tmp_path / "responses.json",
        )


@pytest.mark.asyncio
async def test_snapshot_default_limit_reads_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(filters):
        from app.schemas.spring import ProductSearchResult

        return ProductSearchResult(
            products=[
                SpringProduct(productId=1, name="하나"),
                SpringProduct(productId=2, name="둘"),
            ],
            totalCount=2,
        )

    monkeypatch.setattr(
        goldenset_snapshot,
        "get_settings",
        lambda: type("Config", (), {"goldenset_snapshot_per_query_max": 1})(),
    )
    _, responses = await record_snapshots(
        {"fixture-z": {"keyword": "테스트"}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
    )
    assert responses["fixture-z"]["productIds"] == [1]
