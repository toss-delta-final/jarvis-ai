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
from evals.goldenset.snapshot import record_snapshots, record_snapshots_v2
from tests.integration._stubs import SpringStub

ROOT = Path("evals/goldenset")


def test_committed_dev_and_holdout_are_separate() -> None:
    dev = load_cases("dev")
    holdout = load_cases("holdout")
    assert len(dev) == 109
    assert len(holdout) == 24
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
            fixture_request = responses[case["searchFixtureId"]]["request"]
            if case["caseId"].startswith("buy-colr-"):
                twins = [
                    json.loads(row)
                    for row in (ROOT / "cases" / "buyer_dev.jsonl").read_text().splitlines()
                    if json.loads(row)["searchFixtureId"] == case["searchFixtureId"]
                ]
                assert {key: value for key, value in fixture_request.items() if key != "color"} == {
                    key: value for key, value in case["expectedFilters"].items() if key != "color"
                }
                assert fixture_request["color"] in {
                    twin["expectedFilters"]["color"] for twin in twins
                }
            else:
                assert fixture_request == case["expectedFilters"]


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


@pytest.mark.asyncio
async def test_snapshot_v2_records_candidates_provenance_without_broadening(tmp_path: Path) -> None:
    async def fake_search(filters):
        from app.schemas.spring import ProductSearchResult

        return ProductSearchResult(
            products=[
                SpringProduct(productId=pid, name=f"상품{pid}", price=1000) for pid in range(1, 31)
            ],
            totalCount=30,
        )

    _, responses = await record_snapshots_v2(
        {"fixture-z": {"keyword": "테스트", "category": "카테고리"}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
        target_candidates=30,
        per_query_max=30,
    )

    fixture = responses["fixture-z"]
    assert fixture["productIds"] == list(range(1, 31))
    assert len(fixture["candidates"]) == 30
    assert all(c["source"] == "golden_filter" and c["rule"] is None for c in fixture["candidates"])
    assert {c["from"] for c in fixture["candidates"]} == {"primary"}


@pytest.mark.asyncio
async def test_snapshot_v2_broadens_search_when_primary_candidates_are_thin(tmp_path: Path) -> None:
    calls = []

    async def fake_search(filters):
        calls.append(dict(filters.model_dump(exclude_none=True, by_alias=True)))
        from app.schemas.spring import ProductSearchResult

        if filters.category and filters.keyword:
            products = [SpringProduct(productId=1, name="상품1", price=1000)]
        elif filters.keyword and not filters.category:
            products = [
                SpringProduct(productId=1, name="상품1", price=1000),
                SpringProduct(productId=2, name="상품2", price=1000),
            ]
        else:
            products = [
                SpringProduct(productId=1, name="상품1", price=1000),
                SpringProduct(productId=3, name="상품3", price=1000),
            ]
        return ProductSearchResult(products=products, totalCount=len(products))

    _, responses = await record_snapshots_v2(
        {"fixture-z": {"keyword": "테스트", "category": "카테고리"}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
        target_candidates=3,
        per_query_max=30,
    )

    fixture = responses["fixture-z"]
    assert fixture["productIds"] == [1, 2, 3]
    by_id = {c["productId"]: c for c in fixture["candidates"]}
    assert by_id[1] == {"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}
    assert by_id[2]["rule"] == "broadened_search"
    assert by_id[2]["from"] == "keyword-only"
    assert by_id[3]["rule"] == "broadened_search"
    assert by_id[3]["from"] == "category-only"
    assert len(calls) == 3  # primary + keyword-only + category-only


@pytest.mark.asyncio
async def test_snapshot_v2_broadened_search_preserves_price_bounds(tmp_path: Path) -> None:
    # 실측 회귀(#333 Part 2): keyword-only/category-only 완화 요청이 priceMax/priceMin을
    # 빼면 원래 케이스의 가격 범위를 벗어난 실제 상품이 golden_filter 후보로 섞여 들어와
    # HCV로 샌다(buy-mult-0001 등). 완화는 keyword/category만 느슨하게 하고 가격 하드
    # 제약은 유지해야 한다.
    requests = []

    async def fake_search(filters):
        requests.append(dict(filters.model_dump(exclude_none=True, by_alias=True)))
        from app.schemas.spring import ProductSearchResult

        if filters.category:
            products = [SpringProduct(productId=1, name="상품1", price=1000)]
        else:
            products = [
                SpringProduct(productId=1, name="상품1", price=1000),
                SpringProduct(productId=2, name="상품2", price=999999),
            ]
        return ProductSearchResult(products=products, totalCount=len(products))

    await record_snapshots_v2(
        {"fixture-z": {"keyword": "테스트", "category": "카테고리", "priceMax": 20000}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
        target_candidates=3,
        per_query_max=30,
    )

    broadened_requests = [
        request for request in requests if not ("keyword" in request and "category" in request)
    ]
    assert broadened_requests  # keyword-only/category-only 요청이 실제로 발생했다
    for request in broadened_requests:
        assert request.get("priceMax") == 20000


@pytest.mark.asyncio
async def test_snapshot_v2_relaxed_limit_applies_only_to_broadened_requests(
    tmp_path: Path,
) -> None:
    # Part 2 §3-2: 골든 검색은 작은 limit(30)을, 완화 검색은 더 큰 --relaxed-limit을 쓴다.
    limits = []

    async def fake_search(filters):
        limits.append(filters.limit)
        from app.schemas.spring import ProductSearchResult

        products = [SpringProduct(productId=1, name="상품1", price=1000)]
        return ProductSearchResult(products=products, totalCount=1)

    await record_snapshots_v2(
        {"fixture-z": {"keyword": "테스트"}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
        target_candidates=5,
        per_query_max=30,
        relaxed_limit=120,
    )

    assert limits == [30, 120]  # primary(골든) 30, 완화(keyword-only) 120


@pytest.mark.asyncio
async def test_snapshot_v2_relaxed_results_are_capped_at_target(tmp_path: Path) -> None:
    # 실측 회귀(Part 2 라이브 실행): relaxed_limit=120인 완화 요청이 120건을 그대로 돌려주면
    # 후보 수가 target(예: 30)을 훌쩍 넘겨서는 안 된다 — catalog는 전량 넓히되 후보 목록만
    # target까지 채운다.
    async def fake_search(filters):
        from app.schemas.spring import ProductSearchResult

        if filters.category and filters.keyword:
            products = [SpringProduct(productId=1, name="상품1", price=1000)]
        else:
            products = [
                SpringProduct(productId=pid, name=f"상품{pid}", price=1000) for pid in range(1, 91)
            ]
        return ProductSearchResult(products=products, totalCount=len(products))

    catalog, responses = await record_snapshots_v2(
        {"fixture-z": {"keyword": "테스트", "category": "카테고리"}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
        target_candidates=30,
        per_query_max=30,
        relaxed_limit=120,
    )

    fixture = responses["fixture-z"]
    assert len(fixture["productIds"]) == 30
    # catalog는 완화 요청이 실제로 돌려준 응답 전체(최대 90건)를 그대로 반영한다.
    assert len(catalog) == 90


@pytest.mark.asyncio
async def test_snapshot_v2_stops_broadening_once_target_is_reached(tmp_path: Path) -> None:
    calls = []

    async def fake_search(filters):
        calls.append(1)
        from app.schemas.spring import ProductSearchResult

        if filters.category and filters.keyword:
            products = [SpringProduct(productId=1, name="상품1", price=1000)]
        else:
            products = [
                SpringProduct(productId=1, name="상품1", price=1000),
                SpringProduct(productId=2, name="상품2", price=1000),
            ]
        return ProductSearchResult(products=products, totalCount=len(products))

    _, responses = await record_snapshots_v2(
        {"fixture-z": {"keyword": "테스트", "category": "카테고리"}},
        search=fake_search,
        catalog_path=tmp_path / "catalog.json",
        responses_path=tmp_path / "responses.json",
        recorded_at="2026-08-02T00:00:00+09:00",
        target_candidates=2,
        per_query_max=30,
    )

    assert responses["fixture-z"]["productIds"] == [1, 2]
    assert len(calls) == 2  # primary(1건) + keyword-only(목표 도달, category-only는 스킵)
