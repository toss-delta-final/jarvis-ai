"""[#60] 총액 예산 안에서 니즈별 상품 조합을 결정론적으로 산출한다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_MIN_DFS_WORK_LIMIT = 64

# priority 신호가 없거나(None) 신뢰할 수 없을 때 전 leg 에 균일하게 주는 값.
# 제외 키의 첫 성분으로만 쓰이므로 전 leg 가 같은 값이면 정렬에 아무 영향이 없다
# (= 기존 "최저가가 비싼 leg 부터" tie-break 로 정확히 되돌아간다). 어떤 정수를 골라도
# 결과가 같으므로 config 로 노출할 튜너블이 아니다.
_UNIFORM_PRIORITY = 0

# [PR #314 리뷰 F-7] "무엇이 유효한 priority 인가"의 **정본**. 이 값을 소비하는 쪽(knapsack)이
# 계약을 정하므로 여기가 정의처이고, `need_priority.py`(LLM 미검증 산출을 거르는 1차 방어)가
# 여기서 import 한다 — 반대 방향으로 두면 이 순수 알고리즘 모듈이 `app.core.llm`/`tracing` 을
# 아는 LLM 호출 모듈에 의존하게 돼 방향이 뒤집힌다. 밑줄 없는 이름으로 공개 export 한다.
# 두 곳에서 각각 검증하는 것은 정의 중복이 아니라 **의도된 방어 심층화**다 — `need_priority.py`
# 쪽은 미검증 LLM 산출을 거르고, 여기(`_normalize_priorities`)는 호출자가 누구든 성립해야 하는
# 공개 API 경계 방어다. 검증 호출은 두 곳, 유효값 정의는 한 곳 — 다음 사람이 "중복이네" 하고
# 한쪽을 지우지 않도록 남긴다.
VALID_PRIORITIES = (1, 2, 3)


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


def _normalize_priorities(priorities: Sequence[int] | None, pool_count: int) -> list[int]:
    """priority 신호를 정규화한다. 신뢰할 수 없으면 전 leg 균일값으로 엄격 폴백한다.

    ``None`` · 길이 불일치 · 원소 중 하나라도 ``int`` 가 아니거나(``bool`` 은 ``int`` 의
    서브클래스라 명시적으로 배제) ``{1, 2, 3}`` 밖이면 전체를 버리고 균일값을 돌려준다
    (REQ-REC-075 — 부분 수용은 어긋난 정렬을 만들 뿐이라 손해가 더 크다).
    """
    if priorities is None or len(priorities) != pool_count:
        return [_UNIFORM_PRIORITY] * pool_count
    for value in priorities:
        if isinstance(value, bool) or not isinstance(value, int) or value not in VALID_PRIORITIES:
            return [_UNIFORM_PRIORITY] * pool_count
    return list(priorities)


def build_budget_sets(
    *,
    pools: Sequence[Sequence[tuple[int, int | None]]],
    total_budget: int | None,
    max_sets: int,
    max_combinations: int,
    max_items: int,
    priorities: Sequence[int] | None = None,
) -> BudgetSetPlan | None:
    """예산을 지키는 top-K 세트를 만든다.

    총액(``total_budget``)·개수(``max_items``) 상한 때문에 니즈(leg)를 제외해야 하면
    ``priorities`` 를 최우선 근거로 삼아 priority 역순(3 선택 → 2 권장, 1 필수는 최후)으로
    뺀다(REQ-REC-076). ``priorities`` 가 없거나(``None``) 길이·값을 신뢰할 수 없으면 전
    leg 를 균일값으로 취급해, 기존처럼 "각 니즈의 최저가가 비싼 순서" 만을 결정 근거로
    쓴다(REQ-REC-075 — 신호가 불확실할 때는 조용히 잘못 정렬하기보다 결정론적 폴백으로
    되돌아간다).
    """
    priority_by_leg = _normalize_priorities(priorities, len(pools))
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
            key=lambda leg: (
                priority_by_leg[leg],
                min(price for _, price in cleaned[leg]),
                -leg,
            ),
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
                key=lambda leg: (
                    priority_by_leg[leg],
                    min(price for _, price in cleaned[leg]),
                    -leg,
                ),
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
    work_limit = max(
        _MIN_DFS_WORK_LIMIT,
        max(1, max_combinations) * (2 * len(active) + 1),
    )
    work_done = 0

    def consume_work() -> bool:
        nonlocal truncated, work_done
        if work_done >= work_limit:
            truncated = True
            return False
        work_done += 1
        return True

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
        if not consume_work():
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
            if not consume_work():
                return
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

    if truncated:
        for combo in ordered:
            add(combo, "extra")
    else:
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
