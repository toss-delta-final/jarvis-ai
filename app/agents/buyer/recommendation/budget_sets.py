"""[#60] 총액 예산 안에서 니즈별 상품 조합을 결정론적으로 산출한다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetSet:
    kind: str
    focus_leg: int | None
    legs: tuple[int, ...]
    product_ids: tuple[int, ...]
    verified_sum: int


@dataclass(frozen=True)
class BudgetSetPlan:
    sets: tuple[BudgetSet, ...]
    dropped_legs: tuple[int, ...]  # 총액 예산 때문에 제외
    unavailable_legs: tuple[int, ...]  # 가격 있는 후보가 없어 제외
    limited_legs: tuple[int, ...]  # 목록당 상품 계약 상한 때문에 제외
    total_budget: int | None
    combinations_truncated: bool


@dataclass(frozen=True)
class _Combination:
    legs: tuple[int, ...]
    product_ids: tuple[int, ...]
    prices: tuple[int, ...]
    ranks: tuple[int, ...]
    score: int

    @property
    def verified_sum(self) -> int:
        return sum(self.prices)


def build_budget_sets(
    *,
    pools: Sequence[Sequence[tuple[int, int | None]]],
    total_budget: int | None,
    max_sets: int,
    max_combinations: int,
    max_items: int,
) -> BudgetSetPlan | None:
    """예산을 지키는 top-K 세트를 만든다.

    SPEC 의 ``priority`` 신호는 현재 구현돼 있지 않으므로, 예산이 부족하면 각 니즈의 최저가가
    비싼 순서만을 결정 근거로 사용한다.
    """
    cleaned: dict[int, tuple[tuple[int, int], ...]] = {}
    unavailable: list[int] = []
    for leg, pool in enumerate(pools):
        seen: set[int] = set()
        items: list[tuple[int, int]] = []
        for product_id, price in pool:
            if price is None or product_id in seen:
                continue
            seen.add(product_id)
            items.append((product_id, price))
        if items:
            cleaned[leg] = tuple(items)
        else:
            unavailable.append(leg)
    if not cleaned:
        return None

    active = list(cleaned)
    limited: list[int] = []
    while len(active) > max_items:
        remove_leg = max(
            active,
            key=lambda leg: (min(price for _, price in cleaned[leg]), -leg),
        )
        active.remove(remove_leg)
        limited.append(remove_leg)
    if not active:
        return None

    dropped: list[int] = []
    if total_budget is not None:
        while sum(min(price for _, price in cleaned[leg]) for leg in active) > total_budget:
            if len(active) == 1:
                return None
            remove_leg = max(
                active,
                key=lambda leg: (min(price for _, price in cleaned[leg]), -leg),
            )
            active.remove(remove_leg)
            dropped.append(remove_leg)

    min_suffix = [0] * (len(active) + 1)
    if total_budget is not None:
        for index in range(len(active) - 1, -1, -1):
            min_suffix[index] = min_suffix[index + 1] + min(
                price for _, price in cleaned[active[index]]
            )

    combinations: list[_Combination] = []
    truncated = False

    def visit(
        index: int,
        product_ids: tuple[int, ...],
        prices: tuple[int, ...],
        ranks: tuple[int, ...],
        partial_sum: int,
    ) -> None:
        nonlocal truncated
        if truncated:
            return
        if index == len(active):
            combinations.append(
                _Combination(
                    legs=tuple(active),
                    product_ids=product_ids,
                    prices=prices,
                    ranks=ranks,
                    score=sum(
                        len(cleaned[leg]) - rank for leg, rank in zip(active, ranks, strict=True)
                    ),
                )
            )
            if len(combinations) >= max_combinations:
                truncated = True
            return
        if total_budget is not None and partial_sum + min_suffix[index] > total_budget:
            return
        leg = active[index]
        for rank, (product_id, price) in enumerate(cleaned[leg]):
            if product_id in product_ids:
                continue
            next_sum = partial_sum + price
            if total_budget is not None and next_sum + min_suffix[index + 1] > total_budget:
                continue
            visit(
                index + 1,
                product_ids + (product_id,),
                prices + (price,),
                ranks + (rank,),
                next_sum,
            )
            if truncated:
                return

    visit(0, (), (), (), 0)
    if not combinations:
        return None

    def order_key(combo: _Combination) -> tuple[int, int, tuple[int, ...]]:
        return (-combo.score, combo.verified_sum, combo.product_ids)

    ordered = sorted(combinations, key=order_key)
    selected: list[BudgetSet] = []
    seen_sets: set[frozenset[int]] = set()

    def add(combo: _Combination, kind: str, focus_leg: int | None = None) -> None:
        identity = frozenset(combo.product_ids)
        if identity in seen_sets or len(selected) >= max_sets:
            return
        seen_sets.add(identity)
        selected.append(
            BudgetSet(
                kind=kind,
                focus_leg=focus_leg,
                legs=combo.legs,
                product_ids=combo.product_ids,
                verified_sum=combo.verified_sum,
            )
        )

    if not truncated:
        cheap = min(combinations, key=lambda combo: (combo.verified_sum, order_key(combo)))
        add(cheap, "cheap")
    add(ordered[0], "balanced")
    for position, leg in enumerate(active):
        focused = [combo for combo in ordered if combo.ranks[position] == 0]
        if focused:
            add(focused[0], "focus", leg)
    for combo in ordered:
        add(combo, "extra")

    return BudgetSetPlan(
        sets=tuple(selected),
        dropped_legs=tuple(dropped),
        unavailable_legs=tuple(unavailable),
        limited_legs=tuple(limited),
        total_budget=total_budget,
        combinations_truncated=truncated,
    )
