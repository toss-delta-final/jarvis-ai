"""[#60] 총액 예산 세트 조합 순수 계산 회귀."""

from app.agents.buyer.recommendation.budget_sets import build_budget_sets
from app.core.config import Settings
from app.schemas.spring import LIST_MAX_PRODUCTS


def _price_map(pools):
    prices = {}
    for pool in pools:
        for product_id, price in pool:
            prices.setdefault(product_id, price)
    return prices


def test_budget_sets_obey_budget_are_verified_diverse_and_deterministic() -> None:
    pools = [
        [(11, 20_000), (12, 10_000), (12, 9_000)],
        [(21, 20_000), (22, 15_000)],
        [(31, 20_000), (32, 5_000)],
    ]
    first = build_budget_sets(
        pools=pools, total_budget=50_000, max_sets=3, max_combinations=20_000, max_items=9
    )
    second = build_budget_sets(
        pools=pools, total_budget=50_000, max_sets=3, max_combinations=20_000, max_items=9
    )

    assert first is not None and first == second
    prices = _price_map(pools)
    assert all(
        item.verified_sum == sum(prices[product_id] for product_id in item.product_ids)
        for item in first.sets
    )
    assert all(item.verified_sum <= 50_000 for item in first.sets)
    assert len({frozenset(item.product_ids) for item in first.sets}) == len(first.sets)


def test_budget_sets_drop_unpriced_and_empty_legs() -> None:
    plan = build_budget_sets(
        pools=[[(1, None)], [(2, 1_000), (2, 900)], []],
        total_budget=None,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
    )

    assert plan is not None
    assert plan.dropped_legs == ()
    assert plan.unavailable_legs == (0, 2)
    assert all(item.product_ids == (2,) for item in plan.sets)


def test_budget_sets_drop_most_expensive_minimum_leg_until_feasible() -> None:
    plan = build_budget_sets(
        pools=[[(1, 30_000)], [(2, 20_000)], [(3, 10_000)]],
        total_budget=25_000,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
    )

    assert plan is not None
    assert plan.dropped_legs == (0, 1)
    assert plan.sets[0].legs == (2,)
    assert (
        build_budget_sets(
            pools=[[(1, 30_000)]],
            total_budget=25_000,
            max_sets=3,
            max_combinations=20_000,
            max_items=9,
        )
        is None
    )


def test_budget_sets_marks_truncation_and_rejects_cross_leg_duplicates() -> None:
    plan = build_budget_sets(
        pools=[[(1, 1), (2, 2)], [(1, 1), (3, 3)]],
        total_budget=None,
        max_sets=3,
        max_combinations=1,
        max_items=9,
    )

    assert plan is not None
    assert plan.combinations_truncated is True
    assert all(len(item.product_ids) == len(set(item.product_ids)) for item in plan.sets)


def test_truncated_budget_sets_use_only_neutral_extra_kind() -> None:
    plan = build_budget_sets(
        pools=[[(1, 100), (2, 1)], [(10, 100), (11, 100), (12, 100), (13, 100)]],
        total_budget=None,
        max_sets=3,
        max_combinations=1,
        max_items=9,
    )

    assert plan is not None
    assert plan.combinations_truncated is True
    assert {item.kind for item in plan.sets} == {"extra"}


def test_complete_budget_search_keeps_true_cheapest_set() -> None:
    plan = build_budget_sets(
        pools=[[(1, 100), (2, 1)], [(10, 100), (11, 100), (12, 100), (13, 100)]],
        total_budget=None,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
    )

    assert plan is not None
    assert plan.combinations_truncated is False
    cheap = next(item for item in plan.sets if item.kind == "cheap")
    assert cheap.verified_sum == 101
    assert 2 in cheap.product_ids


def test_truncated_budget_sets_fill_available_slots_without_breaking_invariants() -> None:
    pools = [[(1, 100), (2, 1)], [(10, 100), (11, 100), (12, 100), (13, 100)]]
    first = build_budget_sets(
        pools=pools,
        total_budget=200,
        max_sets=3,
        max_combinations=3,
        max_items=9,
    )
    second = build_budget_sets(
        pools=pools,
        total_budget=200,
        max_sets=3,
        max_combinations=3,
        max_items=9,
    )

    assert first is not None and first == second
    assert first.combinations_truncated is True
    assert len(first.sets) == 3
    assert {item.kind for item in first.sets} == {"extra"}
    assert all(item.verified_sum <= 200 for item in first.sets)
    assert all(len(item.product_ids) == len(set(item.product_ids)) <= 9 for item in first.sets)
    assert len({frozenset(item.product_ids) for item in first.sets}) == len(first.sets)


def test_budget_set_work_cap_truncates_sparse_search_before_combination_cap() -> None:
    pools = [
        [(1, 1), *[(product_id, 100) for product_id in range(100, 1_100)]],
        [(2, 1)],
    ]

    plan = build_budget_sets(
        pools=pools,
        total_budget=2,
        max_sets=3,
        max_combinations=100,
        max_items=9,
    )

    assert plan is not None
    assert plan.combinations_truncated is True
    assert len(plan.sets) == 1 < 100
    assert plan.sets[0].kind == "extra"
    assert plan.sets[0].product_ids == (1, 2)
    assert plan.sets[0].verified_sum == 2


def test_budget_sets_without_budget_and_max_sets_one_returns_baseline() -> None:
    plan = build_budget_sets(
        pools=[[(1, 5_000), (2, 1_000)], [(3, 6_000), (4, 2_000)]],
        total_budget=None,
        max_sets=1,
        max_combinations=20_000,
        max_items=9,
    )

    assert plan is not None
    assert len(plan.sets) == 1
    assert plan.sets[0].kind == "cheap"
    assert plan.total_budget is None


def test_default_config_shape_fits_exact_search_and_contract_limits(monkeypatch) -> None:
    for field in (
        "BUDGET_SET_MAX_COUNT",
        "BUDGET_SET_ALT_POOL",
        "BUDGET_SET_MAX_COMBINATIONS",
    ):
        monkeypatch.delenv(field, raising=False)
    settings = Settings(_env_file=None)
    pools = [
        [
            (leg * settings.budget_set_alt_pool + rank, 1_000 + rank)
            for rank in range(settings.budget_set_alt_pool)
        ]
        for leg in range(5)
    ]
    plan = build_budget_sets(
        pools=pools,
        total_budget=None,
        max_sets=settings.budget_set_max_count,
        max_combinations=settings.budget_set_max_combinations,
        max_items=LIST_MAX_PRODUCTS,
    )

    assert settings.budget_set_alt_pool == 6
    assert settings.budget_set_max_count == 3
    assert settings.budget_set_max_combinations == 20_000
    assert settings.budget_set_alt_pool**5 < settings.budget_set_max_combinations
    assert plan is not None and len(plan.sets) == settings.budget_set_max_count
    assert all(len(item.product_ids) == 5 <= LIST_MAX_PRODUCTS for item in plan.sets)


def test_budget_sets_limit_ten_legs_to_contract_max_in_expensive_first_order() -> None:
    pools = [[(leg + 1, (leg + 1) * 1_000)] for leg in range(10)]

    plan = build_budget_sets(
        pools=pools,
        total_budget=None,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
    )

    assert plan is not None
    assert plan.limited_legs == (9,)
    assert plan.unavailable_legs == ()
    assert all(len(item.product_ids) <= 9 for item in plan.sets)


def test_budget_sets_priority_beats_price_when_dropping() -> None:
    # leg 0(priority 3·선택)은 최저가가 가장 싸다(1,000). 가격만 보면 leg 1(30,000)이
    # 먼저 빠져야 하지만, priority 가 이겨서 가장 싼 leg 0 이 먼저 빠진다.
    pools = [[(1, 1_000)], [(2, 30_000)], [(3, 10_000)]]

    plan = build_budget_sets(
        pools=pools,
        total_budget=40_000,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
        priorities=[3, 1, 2],
    )

    assert plan is not None
    assert plan.dropped_legs == (0,)
    assert plan.sets[0].legs == (1, 2)


def test_budget_sets_priority_one_dropped_last() -> None:
    # leg 2·3 은 둘 다 priority 1(필수)이라 예산이 아주 빡빡하면 결국 하나는 빠져야 하지만,
    # priority 3(leg 0) → priority 2(leg 1) 이 먼저 전부 빠진 뒤에야 그 차례가 온다.
    pools = [[(1, 10_000)], [(2, 10_000)], [(3, 10_000)], [(4, 5_000)]]

    plan = build_budget_sets(
        pools=pools,
        total_budget=12_000,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
        priorities=[3, 2, 1, 1],
    )

    assert plan is not None
    assert plan.dropped_legs == (0, 1, 2)
    assert plan.sets[0].legs == (3,)


def test_budget_sets_priority_tie_preserves_price_tie_break() -> None:
    pools = [[(1, 30_000)], [(2, 20_000)], [(3, 10_000)]]

    with_uniform_priority = build_budget_sets(
        pools=pools,
        total_budget=25_000,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
        priorities=[2, 2, 2],
    )
    without_priority = build_budget_sets(
        pools=pools,
        total_budget=25_000,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
    )

    assert with_uniform_priority is not None and without_priority is not None
    assert with_uniform_priority.dropped_legs == without_priority.dropped_legs
    assert with_uniform_priority == without_priority


def test_budget_sets_max_items_limit_respects_priority_reverse_order() -> None:
    # 전 leg 가격이 같으므로 가격 tie-break 는 무력화된다 — priority 만으로 순서가 갈린다.
    pools = [[(leg + 1, 1_000)] for leg in range(10)]
    priorities = [1] * 9 + [3]

    plan = build_budget_sets(
        pools=pools,
        total_budget=None,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
        priorities=priorities,
    )

    assert plan is not None
    assert plan.limited_legs == (9,)


def test_budget_sets_priority_fallback_on_untrusted_signal() -> None:
    pools = [[(1, 30_000)], [(2, 20_000)], [(3, 10_000)]]
    baseline = build_budget_sets(
        pools=pools,
        total_budget=25_000,
        max_sets=3,
        max_combinations=20_000,
        max_items=9,
    )

    untrusted_inputs = [
        [1, 2],  # 길이 불일치
        [1, None, 2],  # None 원소
        [1, 0, 2],  # 범위 밖(0)
        [1, 4, 2],  # 범위 밖(4)
        [1, True, 2],  # bool 은 int 서브클래스지만 priority 로 인정하지 않는다
    ]
    for priorities in untrusted_inputs:
        plan = build_budget_sets(
            pools=pools,
            total_budget=25_000,
            max_sets=3,
            max_combinations=20_000,
            max_items=9,
            priorities=priorities,
        )
        assert plan == baseline, priorities


def test_budget_sets_priority_does_not_change_single_leg_over_budget_contract() -> None:
    assert (
        build_budget_sets(
            pools=[[(1, 30_000)]],
            total_budget=25_000,
            max_sets=3,
            max_combinations=20_000,
            max_items=9,
            priorities=[1],
        )
        is None
    )
