"""pg-catalog 조회 전용 하드 네거티브 후보 추출(evals.goldenset.inject) 결정론 테스트.

DB는 fake로 대역한다 — Part 1은 라이브 DB 호출을 하지 않는다(패킷 §0).
"""

from __future__ import annotations

from evals.goldenset.inject import (
    build_case_candidates,
    derive_constraint_subset_candidates,
    find_attr_violation,
    find_other_brand,
    find_price_violation,
    find_random_catalog,
    find_semantic_near,
    merge_candidates,
)


def _product(
    product_id: int,
    *,
    category: str = "카테고리A",
    price: int = 10_000,
    brand: str = "브랜드A",
    attributes: dict | None = None,
) -> dict:
    return {
        "productId": product_id,
        "name": f"상품{product_id}",
        "summary": "테스트 상품",
        "attributes": attributes or {},
        "price": price,
        "rating": 4.0,
        "reviewCount": 1,
        "categoryName": category,
        "brandName": brand,
    }


def _fake_embeddings(store: dict[int, list[float]]):
    def _lookup(product_ids):
        return {pid: store[pid] for pid in product_ids if pid in store}

    return _lookup


def _fake_nearest(order: list[tuple[int, float]]):
    """미리 정해둔 (productId, distance) 목록에서 exclude/limit만 적용하는 fake 인덱스."""

    def _nearest(vector, exclude_ids, limit):
        candidates = [(pid, dist) for pid, dist in order if pid not in exclude_ids]
        candidates.sort(key=lambda item: (item[1], item[0]))
        return candidates[:limit]

    return _nearest


def test_find_semantic_near_excludes_golden_and_existing_and_sorts_by_distance() -> None:
    embeddings = _fake_embeddings({1: [1.0, 0.0]})
    nearest = _fake_nearest([(2, 0.5), (3, 0.1), (1, 0.0), (4, 0.5)])
    catalog = {str(pid): _product(pid) for pid in (1, 2, 3, 4)}

    result = find_semantic_near(
        [1],
        frozenset({99}),
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        catalog=catalog,
        limit=10,
    )

    ids = [c["productId"] for c in result]
    assert ids == [3, 2, 4]  # 거리 오름차순, 동률은 productId 오름차순
    assert all(c["source"] == "injected" and c["rule"] == "semantic_near" for c in result)


def test_find_semantic_near_excludes_neighbors_not_in_catalog() -> None:
    embeddings = _fake_embeddings({1: [1.0, 0.0]})
    nearest = _fake_nearest([(2, 0.1), (3, 0.2)])
    catalog = {"2": _product(2)}  # 3은 catalog에 없음(가격/이름 계산 불가)

    result = find_semantic_near(
        [1],
        frozenset(),
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        catalog=catalog,
        limit=10,
    )

    assert [c["productId"] for c in result] == [2]


def test_find_semantic_near_is_deterministic_across_repeated_calls() -> None:
    embeddings = _fake_embeddings({1: [1.0, 0.0], 2: [0.0, 1.0]})
    nearest = _fake_nearest([(10, 0.2), (11, 0.2), (12, 0.1)])
    catalog = {str(pid): _product(pid) for pid in (10, 11, 12)}

    first = find_semantic_near(
        [1, 2],
        frozenset(),
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        catalog=catalog,
        limit=5,
    )
    second = find_semantic_near(
        [1, 2],
        frozenset(),
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        catalog=catalog,
        limit=5,
    )

    assert first == second


def test_find_semantic_near_skips_missing_embeddings() -> None:
    embeddings = _fake_embeddings({})  # 정답 임베딩이 없음
    nearest = _fake_nearest([(2, 0.1)])
    catalog = {"2": _product(2)}

    result = find_semantic_near(
        [1],
        frozenset(),
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        catalog=catalog,
        limit=5,
    )

    assert result == []


def test_find_price_violation_matches_same_category_out_of_range() -> None:
    catalog = {
        "1": _product(1, category="A", price=5_000),
        "2": _product(2, category="A", price=50_000),
        "3": _product(3, category="B", price=50_000),  # 다른 카테고리
    }

    result = find_price_violation("A", {"priceMax": 10_000}, catalog, frozenset(), limit=10)

    assert [c["productId"] for c in result] == [2]
    assert result[0]["rule"] == "price_violation"


def test_find_price_violation_returns_empty_without_constraints() -> None:
    catalog = {"1": _product(1, category="A", price=999_999)}
    result = find_price_violation("A", {}, catalog, frozenset(), limit=10)
    assert result == []


def test_find_attr_violation_matches_mismatched_attribute() -> None:
    catalog = {
        "1": _product(1, category="A", attributes={"소재": "린넨"}),
        "2": _product(2, category="A", attributes={"소재": "면"}),
    }

    result = find_attr_violation("A", {"소재": "린넨"}, catalog, frozenset(), limit=10)

    assert [c["productId"] for c in result] == [2]
    assert result[0]["rule"] == "attr_violation"


def test_find_other_brand_matches_same_category_different_brand() -> None:
    catalog = {
        "1": _product(1, category="A", brand="나이키"),
        "2": _product(2, category="A", brand="아디다스"),
        "3": _product(3, category="B", brand="아디다스"),
    }

    result = find_other_brand("A", ["나이키"], catalog, frozenset(), limit=10)

    assert [c["productId"] for c in result] == [2]
    assert result[0]["rule"] == "other_brand"


def test_merge_candidates_fills_up_to_target_in_rule_priority_order() -> None:
    golden = [{"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}]
    pools = {
        "semantic_near": [
            {"productId": 2, "source": "injected", "rule": "semantic_near", "from": "x"}
        ],
        "price_violation": [
            {"productId": 3, "source": "injected", "rule": "price_violation", "from": "x"}
        ],
    }

    result = merge_candidates(golden, pools, target=3)

    assert [c["productId"] for c in result] == [1, 2, 3]


def test_merge_candidates_stops_at_target_even_with_more_available() -> None:
    golden = [{"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}]
    pools = {
        "semantic_near": [
            {"productId": pid, "source": "injected", "rule": "semantic_near", "from": "x"}
            for pid in (2, 3, 4, 5)
        ],
    }

    result = merge_candidates(golden, pools, target=2)

    assert [c["productId"] for c in result] == [1, 2]


def test_merge_candidates_never_duplicates_a_golden_id_already_present() -> None:
    golden = [{"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}]
    pools = {
        "semantic_near": [
            {"productId": 1, "source": "injected", "rule": "semantic_near", "from": "x"}
        ]
    }

    result = merge_candidates(golden, pools, target=5)

    assert len(result) == 1
    assert result[0]["source"] == "golden_filter"


def test_build_case_candidates_combines_all_rules_deterministically() -> None:
    golden = [{"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}]
    catalog = {
        "1": _product(1, category="A", price=5_000, brand="나이키"),
        "2": _product(2, category="A", price=50_000, brand="나이키"),  # price_violation
        "3": _product(3, category="A", price=5_000, brand="아디다스"),  # other_brand
        "4": _product(4, category="A"),  # semantic_near
    }
    embeddings = _fake_embeddings({1: [1.0, 0.0]})
    nearest = _fake_nearest([(4, 0.1)])

    result = build_case_candidates(
        golden,
        case_id="buy-test-0001",
        category="A",
        hard_constraints={"priceMax": 10_000},
        attr_conditions={},
        target_brands=["나이키"],
        golden_product_ids=[1],
        catalog=catalog,
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        target=4,
    )

    ids = {c["productId"] for c in result}
    assert ids == {1, 2, 3, 4}
    by_id = {c["productId"]: c for c in result}
    assert by_id[2]["rule"] == "price_violation"
    assert by_id[3]["rule"] == "other_brand"
    assert by_id[4]["rule"] == "semantic_near"


def test_build_case_candidates_keeps_non_price_violation_channels_within_price_bounds() -> None:
    # 라이브 실측(#333 Part 2): 가격은 Spring 서버 사이드에서만 걸러지고 AI 앱은 로컬
    # 재검증을 하지 않는다 — semantic_near/other_brand/random_catalog가 우연히 가격 위반
    # 상품을 주입하면 evals.metrics의 hard_filter가 걸러내지 못해 HCV로 그대로 샌다. 오직
    # price_violation 채널만 의도적으로 위반 상품을 찾아야 한다.
    golden = [{"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}]
    catalog = {
        "1": _product(1, category="A", price=5_000, brand="나이키"),
        "2": _product(2, category="A", price=50_000, brand="나이키"),  # 가격 위반, other_brand 아님
        "3": _product(3, category="A", price=50_000, brand="아디다스"),  # 가격도 위반 + 다른 브랜드
        "4": _product(4, category="A", price=5_000, brand="아디다스"),  # 가격은 OK, 다른 브랜드
    }
    embeddings = _fake_embeddings({1: [1.0, 0.0]})
    nearest = _fake_nearest(
        [(2, 0.1), (3, 0.2)]
    )  # 둘 다 가격 위반이라 semantic_near에서 걸러져야 함

    result = build_case_candidates(
        golden,
        case_id="buy-test-0001",
        category="A",
        hard_constraints={"priceMax": 10_000},
        attr_conditions={},
        target_brands=["나이키"],
        golden_product_ids=[1],
        catalog=catalog,
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        target=4,
    )

    by_id = {c["productId"]: c for c in result}
    # 2·3은 semantic_near 이웃이자 가격 위반 상품이다 — semantic_near 채널에서는 걸러지고,
    # price_violation 채널이 의도한 대로 이 둘을 찾아 채운다.
    assert by_id[2]["rule"] == "price_violation"
    assert by_id[3]["rule"] == "price_violation"
    assert 4 in by_id
    assert by_id[4]["rule"] == "other_brand"  # other_brand는 가격 내 상품만 채택


def test_derive_constraint_subset_candidates_is_subset_of_relaxed_by_construction() -> None:
    # #333 라운드2 F-R6 — constraint_subset DIR 쌍은 라이브 검색·독립 주입이 노출 집합을
    # 우연히 어긋나게 할 수 있다(dir-subset-03 실측 실패). 강화 fixture 후보를 완화 fixture의
    # candidates만 걸러 만들면 set(strict) ⊆ set(relaxed)가 항상 성립한다.
    relaxed_candidates = [
        {"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"},
        {"productId": 2, "source": "golden_filter", "rule": None, "from": "primary"},
        {"productId": 3, "source": "injected", "rule": "semantic_near", "from": "semantic_near:1"},
        {"productId": 4, "source": "injected", "rule": "random_catalog", "from": "random_catalog"},
    ]
    catalog = {
        "1": _product(1, price=5_000),
        "2": _product(2, price=50_000),  # priceMax=10_000 위반 — 강화 쪽에서 빠져야 한다
        "3": _product(3, price=8_000),
        "4": {**_product(4), "price": None},  # 가격 미상 — 안전하게 제외
    }

    strict_candidates = derive_constraint_subset_candidates(
        relaxed_candidates,
        catalog=catalog,
        stricter_hard_constraints={"priceMax": 10_000},
    )

    strict_ids = {c["productId"] for c in strict_candidates}
    relaxed_ids = {c["productId"] for c in relaxed_candidates}
    assert strict_ids <= relaxed_ids
    assert strict_ids == {1, 3}
    # provenance(source/rule/from)는 완화 쪽 것을 그대로 물려받는다.
    by_id = {c["productId"]: c for c in strict_candidates}
    assert by_id[3]["rule"] == "semantic_near"
    assert by_id[3]["source"] == "injected"


def test_build_case_candidates_fills_remainder_from_random_catalog() -> None:
    golden = [{"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"}]
    catalog = {str(pid): _product(pid, category="A") for pid in range(1, 11)}
    embeddings = _fake_embeddings({})
    nearest = _fake_nearest([])

    result = build_case_candidates(
        golden,
        case_id="buy-test-0002",
        category="A",
        hard_constraints={},
        attr_conditions={},
        target_brands=[],
        golden_product_ids=[1],
        catalog=catalog,
        embedding_lookup=embeddings,
        nearest_neighbors=nearest,
        target=5,
    )

    assert len(result) == 5
    rules = {c["productId"]: c["rule"] for c in result}
    assert rules[1] is None
    assert all(rules[pid] == "random_catalog" for pid in rules if pid != 1)


def test_find_random_catalog_is_deterministic_and_excludes_given_ids() -> None:
    catalog = {str(pid): _product(pid) for pid in range(1, 21)}

    first = find_random_catalog("buy-test-0001", catalog, frozenset({1, 2}), limit=5)
    second = find_random_catalog("buy-test-0001", catalog, frozenset({1, 2}), limit=5)

    ids = [c["productId"] for c in first]
    assert first == second
    assert 1 not in ids and 2 not in ids
    assert len(ids) == 5
    assert all(c["source"] == "injected" and c["rule"] == "random_catalog" for c in first)


def test_find_random_catalog_differs_by_case_id() -> None:
    catalog = {str(pid): _product(pid) for pid in range(1, 21)}

    first = find_random_catalog("buy-test-0001", catalog, frozenset(), limit=5)
    second = find_random_catalog("buy-test-0002", catalog, frozenset(), limit=5)

    assert [c["productId"] for c in first] != [c["productId"] for c in second]


def test_find_random_catalog_caps_sample_at_available_catalog_size() -> None:
    catalog = {str(pid): _product(pid) for pid in (1, 2)}

    result = find_random_catalog("buy-test-0001", catalog, frozenset(), limit=10)

    assert {c["productId"] for c in result} == {1, 2}


def test_build_label_worksheet_lists_every_candidate_row() -> None:
    from evals.goldenset.inject import build_label_worksheet

    candidates = [
        {"productId": 1, "source": "golden_filter", "rule": None, "from": "primary"},
        {"productId": 2, "source": "injected", "rule": "semantic_near", "from": "semantic_near:1"},
    ]
    catalog = {"1": _product(1), "2": _product(2)}

    worksheet = build_label_worksheet("buy-test-0001", "테스트 질의", candidates, catalog)

    assert "buy-test-0001" in worksheet
    assert "테스트 질의" in worksheet
    assert "상품1" in worksheet
    assert "semantic_near" in worksheet
