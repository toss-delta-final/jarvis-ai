"""evals.goldenset.campaign_v2 결정론·멱등·병합 테스트. 라이브 호출은 fake로 대역한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.spring import ProductChange, ProductChangesPage, ProductSearchResult, SpringProduct
from evals.goldenset.campaign_v2 import fetch_full_catalog_via_i17, run_campaign


def _product(
    product_id: int, *, category: str = "A", price: int = 10_000, brand: str = "브랜드"
) -> SpringProduct:
    return SpringProduct(
        productId=product_id,
        name=f"상품{product_id}",
        summary="테스트 상품",
        attributes={},
        price=price,
        rating=4.0,
        reviewCount=1,
        categoryName=category,
        brandName=brand,
    )


def _fake_search(catalog_by_category: dict[str, list[SpringProduct]]):
    async def _search(filters):
        category = filters.category
        products = catalog_by_category.get(category, [])
        if filters.keyword:
            products = [p for p in products if filters.keyword in p.name]
        if filters.price_max is not None:
            products = [p for p in products if p.price <= filters.price_max]
        if filters.price_min is not None:
            products = [p for p in products if p.price >= filters.price_min]
        return ProductSearchResult(products=products[: filters.limit], totalCount=len(products))

    return _search


def _fake_embeddings(store: dict[int, list[float]]):
    def _lookup(product_ids):
        return {pid: store[pid] for pid in product_ids if pid in store}

    return _lookup


def _fake_nearest(order: list[tuple[int, float]]):
    def _nearest(vector, exclude_ids, limit):
        candidates = [(pid, dist) for pid, dist in order if pid not in exclude_ids]
        candidates.sort(key=lambda item: (item[1], item[0]))
        return candidates[:limit]

    return _nearest


@pytest.mark.asyncio
async def test_fetch_full_catalog_via_i17_pages_until_no_more_and_skips_hidden() -> None:
    pages = [
        ProductChangesPage(
            items=[
                ProductChange(
                    productId=1,
                    status="ON_SALE",
                    updatedAt="2026-08-01T00:00:00Z",
                    name="상품1",
                    category="A",
                    brand="브랜드",
                ),
                ProductChange(
                    productId=2, status="HIDDEN", updatedAt="2026-08-01T00:00:00Z", name="상품2"
                ),
            ],
            nextCursor="cursor-2",
            hasMore=True,
        ),
        ProductChangesPage(
            items=[
                ProductChange(
                    productId=3,
                    status="ON_SALE",
                    updatedAt="2026-08-01T00:00:00Z",
                    name="상품3",
                    category="B",
                    brand="브랜드2",
                ),
            ],
            nextCursor=None,
            hasMore=False,
        ),
    ]
    calls = []

    async def fake_fetch(cursor, limit):
        calls.append(cursor)
        return pages[len(calls) - 1]

    catalog = await fetch_full_catalog_via_i17(fake_fetch, page_limit=500)

    assert set(catalog) == {"1", "3"}  # HIDDEN(2)은 제외
    assert catalog["1"]["price"] is None
    assert catalog["1"]["categoryName"] == "A"
    assert calls == [None, "cursor-2"]


@pytest.mark.asyncio
async def test_run_campaign_enriches_catalog_for_forbidden_categories(tmp_path: Path) -> None:
    # 스냅샷 검증(schema.validate_cases)은 forbiddenCategories도 catalog에 실존해야 한다고
    # 요구한다 — 케이스 자신의 category뿐 아니라 hardConstraints.forbiddenCategories도
    # 카탈로그 확장 검색 대상이어야 한다.
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text("{}", encoding="utf-8")
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    products_a = [_product(1, category="A")]
    products_b = [_product(2, category="B")]
    search = _fake_search({"A": products_a, "B": products_b})

    specs = [
        {
            "caseId": "buy-test-0001",
            "expectedFilters": {"category": "A"},
            "category": "A",
            "hardConstraints": {"forbiddenCategories": ["B"]},
        }
    ]

    await run_campaign(
        specs,
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=_fake_embeddings({}),
        nearest_neighbors=_fake_nearest([]),
        target=3,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    catalog = json.loads((root / "fixtures" / "catalog_snapshot.json").read_text())
    assert "2" in catalog  # category B 상품도 카탈로그에 들어와야 한다


@pytest.mark.asyncio
async def test_run_campaign_excludes_priceless_candidates_for_price_constrained_cases(
    tmp_path: Path,
) -> None:
    # 라이브 실측(#333 Part 2): I-17 폴백 레코드(price=None)가 가격 제약 케이스에 주입되면
    # 앱 hard_filter가 가격을 몰라 걸러내지 못해 HCV로 샌다 — 가격 제약이 있으면 price가 있는
    # catalog 항목으로만 주입 풀을 좁혀야 한다.
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    # 사전에 price=None인 폴백 레코드를 카탈로그에 심어둔다(I-17 스캔을 흉내).
    (root / "fixtures" / "catalog_snapshot.json").write_text(
        json.dumps(
            {"99": {"productId": 99, "name": "가격불명", "categoryName": "A", "price": None}}
        ),
        encoding="utf-8",
    )
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    products = [_product(1, category="A", price=5_000)]
    search = _fake_search({"A": products})

    specs = [
        {
            "caseId": "buy-test-0001",
            "expectedFilters": {"category": "A"},
            "category": "A",
            "hardConstraints": {"priceMax": 10_000},
        }
    ]

    await run_campaign(
        specs,
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=_fake_embeddings({}),
        nearest_neighbors=_fake_nearest([]),
        target=5,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    responses = json.loads((root / "fixtures" / "search_responses.json").read_text())
    ids = responses["buy-test-0001"]["productIds"]
    assert 99 not in ids  # price=None 레코드는 주입되지 않는다


@pytest.mark.asyncio
async def test_run_campaign_writes_fixture_and_worksheet(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text("{}", encoding="utf-8")
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    products = [_product(pid, category="A") for pid in range(1, 4)]
    search = _fake_search({"A": products})
    embeddings = _fake_embeddings({1: [1.0, 0.0]})
    nearest = _fake_nearest([])

    specs = [
        {
            "caseId": "buy-test-0001",
            "query": "테스트 질의",
            "expectedFilters": {"category": "A", "keyword": "상품1"},
            "category": "A",
        }
    ]

    worksheets = await run_campaign(
        specs,
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        target=3,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    assert "buy-test-0001" in worksheets
    assert "buy-test-0001" in worksheets["buy-test-0001"]

    responses = json.loads((root / "fixtures" / "search_responses.json").read_text())
    assert "buy-test-0001" in responses
    fixture = responses["buy-test-0001"]
    assert fixture["productIds"] == sorted(fixture["productIds"])
    catalog = json.loads((root / "fixtures" / "catalog_snapshot.json").read_text())
    for product_id in fixture["productIds"]:
        assert str(product_id) in catalog


@pytest.mark.asyncio
async def test_run_campaign_leaves_zero_result_cases_empty_without_injection(
    tmp_path: Path,
) -> None:
    # 실측 회귀(#333 Part 2): 골든(primary+완화) 검색이 0건인 0-result failure MFT
    # 케이스에 random_catalog 등 하드 네거티브를 주입하면 "검색 결과 없음" 시나리오
    # 자체가 깨진다(buy-fail-*/buy-cmap-0005 등 6건이 오염됐던 실측 사례).
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text("{}", encoding="utf-8")
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    search = _fake_search({})  # 어떤 category/keyword로도 매치되는 상품이 없다

    specs = [
        {
            "caseId": "buy-fail-9999",
            "query": "존재하지 않는 상품",
            "expectedFilters": {"category": "없는카테고리"},
            "category": "없는카테고리",
        }
    ]

    await run_campaign(
        specs,
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=_fake_embeddings({}),
        nearest_neighbors=_fake_nearest([(1, 0.1), (2, 0.2)]),
        target=3,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    responses = json.loads((root / "fixtures" / "search_responses.json").read_text())
    assert responses["buy-fail-9999"]["productIds"] == []
    assert responses["buy-fail-9999"]["candidates"] == []


@pytest.mark.asyncio
async def test_run_campaign_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text("{}", encoding="utf-8")
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    products = [_product(pid, category="A") for pid in range(1, 4)]
    search = _fake_search({"A": products})
    embeddings = _fake_embeddings({})
    nearest = _fake_nearest([])

    specs = [
        {
            "caseId": "buy-test-0001",
            "expectedFilters": {"category": "A"},
        }
    ]

    kwargs = dict(
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        target=3,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    await run_campaign(specs, **kwargs)
    first = (
        (root / "fixtures" / "catalog_snapshot.json").read_bytes(),
        (root / "fixtures" / "search_responses.json").read_bytes(),
    )
    await run_campaign(specs, **kwargs)
    second = (
        (root / "fixtures" / "catalog_snapshot.json").read_bytes(),
        (root / "fixtures" / "search_responses.json").read_bytes(),
    )

    assert first == second


@pytest.mark.asyncio
async def test_run_campaign_preserves_other_cases_fixtures(tmp_path: Path) -> None:
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text(
        json.dumps({"999": _product(999).model_dump(by_alias=True)}), encoding="utf-8"
    )
    (root / "fixtures" / "search_responses.json").write_text(
        json.dumps(
            {
                "buy-existing-0001": {
                    "request": {"keyword": "기존"},
                    "productIds": [999],
                    "totalCount": 1,
                    "recordedAt": "2026-08-01T00:00:00+09:00",
                    "source": "live-spring-i1",
                    "candidates": [
                        {
                            "productId": 999,
                            "source": "golden_filter",
                            "rule": None,
                            "from": "primary",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    products = [_product(pid, category="A") for pid in range(1, 3)]
    search = _fake_search({"A": products})

    specs = [{"caseId": "buy-test-0001", "expectedFilters": {"category": "A"}}]

    await run_campaign(
        specs,
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=_fake_embeddings({}),
        nearest_neighbors=_fake_nearest([]),
        target=2,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    responses = json.loads((root / "fixtures" / "search_responses.json").read_text())
    assert "buy-existing-0001" in responses
    assert responses["buy-existing-0001"]["productIds"] == [999]
    catalog = json.loads((root / "fixtures" / "catalog_snapshot.json").read_text())
    assert "999" in catalog


@pytest.mark.asyncio
async def test_run_campaign_constraint_pair_of_survives_push_truncation(tmp_path: Path) -> None:
    # #333 라운드2 F-R6 — fixture productIds 집합 수준의 부분집합만으로는 평가 시점 push
    # 절단(LIST_MAX_PRODUCTS, ascending 상위 K)을 통과하지 못한다는 것을 실측으로 확인했다
    # (dir-subset-02/03, behaviorChecks 18/18 미달). 강화(stricter)를 먼저 기록하고 완화
    # (relaxed)가 그 위에 병합되도록(constraintPairOf를 완화 스펙에 둔다) 해야 상위 K에서도
    # 강화 쪽이 완화 쪽의 부분집합으로 남는다. 카탈로그를 productId가 듬성듬성하게(스킵 포함)
    # 구성해 "중간 항목 제거로 뒷쪽이 당겨지는" 실패 패턴을 재현한다.
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text("{}", encoding="utf-8")
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    # 강화 필터(priceMax=10_000)를 만족하는 상품(1,2,4)과 위반하는 상품(3,5)을 productId
    # 오름차순으로 섞어, "위반 상품이 중간에 끼면 뒤가 당겨지는" 패턴을 재현한다. category를
    # 쓰지 않는다(실제 constraint_subset 스펙과 동일 — category가 있으면 find_price_violation
    # 채널이 전체 카탈로그에서 별도로 위반 상품을 찾아 주입해 이 테스트의 관심사와 무관한
    # 잡음이 섞인다).
    products = [
        _product(1, price=5_000),
        _product(2, price=5_000),
        _product(3, price=50_000),  # priceMax 위반, 1·2 사이는 아니지만 앞쪽에 낌
        _product(4, price=5_000),
        _product(5, price=50_000),  # priceMax 위반
    ]
    search = _fake_search(
        {None: products}
    )  # category 없는 요청 — 실제 constraint_subset 스펙과 동일
    top_k = 3  # 테스트용 push 절단 크기(실제론 LIST_MAX_PRODUCTS=9)

    specs = [
        {
            "caseId": "buy-dirc-strict",
            "searchFixtureId": "dev-strict",
            "expectedFilters": {"keyword": "상품", "priceMax": 10_000},
            "hardConstraints": {"priceMax": 10_000},
        },
        {
            "caseId": "buy-dirc-relaxed",
            "searchFixtureId": "dev-relaxed",
            "expectedFilters": {"keyword": "상품"},
            "constraintPairOf": "dev-strict",
        },
    ]

    await run_campaign(
        specs,
        recorded_at="2026-08-05T00:00:00+09:00",
        root=root,
        search=search,
        embedding_lookup=_fake_embeddings({}),
        nearest_neighbors=_fake_nearest([]),
        target=5,
        relaxed_limit=10,
        catalog_search_limit=10,
    )

    responses = json.loads((root / "fixtures" / "search_responses.json").read_text())
    relaxed_ids = responses["dev-relaxed"]["productIds"]
    strict_ids = responses["dev-strict"]["productIds"]
    assert set(strict_ids) <= set(relaxed_ids)
    assert set(strict_ids[:top_k]) <= set(relaxed_ids[:top_k])
    # 강화 쪽 골든 검색 자체가 가격 필터를 반영하므로(_fake_search) 3·5는 강화 쪽에 없어야 한다.
    assert 3 not in strict_ids and 5 not in strict_ids


@pytest.mark.asyncio
async def test_run_campaign_constraint_pair_of_requires_stricter_spec_first(
    tmp_path: Path,
) -> None:
    root = tmp_path / "goldenset"
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "catalog_snapshot.json").write_text("{}", encoding="utf-8")
    (root / "fixtures" / "search_responses.json").write_text("{}", encoding="utf-8")

    specs = [
        {
            "caseId": "buy-dirc-relaxed",
            "searchFixtureId": "dev-relaxed",
            "expectedFilters": {"category": "A"},
            "constraintPairOf": "dev-strict-missing",
        }
    ]

    with pytest.raises(ValueError, match="constraintPairOf"):
        await run_campaign(
            specs,
            recorded_at="2026-08-05T00:00:00+09:00",
            root=root,
            search=_fake_search({}),
            embedding_lookup=_fake_embeddings({}),
            nearest_neighbors=_fake_nearest([]),
            target=3,
            relaxed_limit=10,
            catalog_search_limit=10,
        )
