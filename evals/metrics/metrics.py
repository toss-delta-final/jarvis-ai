"""추천 품질 지표 순수 함수.

모든 함수는 입력을 변경하지 않으며 파일·네트워크 I/O를 수행하지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence


def _positive_k(k: int) -> None:
    if k <= 0:
        raise ValueError("K는 0보다 커야 합니다")


def unique_ranked_ids(ranked_ids: Sequence[int]) -> tuple[list[int], int]:
    """첫 등장 순서를 보존하며 중복 상품 id를 제거한다."""
    seen: set[int] = set()
    unique: list[int] = []
    duplicates = 0
    for product_id in ranked_ids:
        if product_id in seen:
            duplicates += 1
            continue
        seen.add(product_id)
        unique.append(product_id)
    return unique, duplicates


def precision_at_k(ranked_ids: Sequence[int], relevant: Iterable[int], k: int) -> float:
    """Precision@K — 결과가 짧아도 분모는 항상 K다."""
    _positive_k(k)
    unique, _ = unique_ranked_ids(ranked_ids)
    return len(set(unique[:k]) & set(relevant)) / k


def recall_at_k(ranked_ids: Sequence[int], relevant: Iterable[int], k: int) -> float:
    """Recall@K — 정답이 없으면 0을 반환하며 runner가 집계 분모에서 제외한다."""
    _positive_k(k)
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    unique, _ = unique_ranked_ids(ranked_ids)
    return len(set(unique[:k]) & relevant_set) / len(relevant_set)


def mean_reciprocal_rank(ranked_ids: Sequence[int], relevant: Iterable[int]) -> float:
    """첫 정답의 1-base 순위 역수."""
    relevant_set = set(relevant)
    unique, _ = unique_ranked_ids(ranked_ids)
    for rank, product_id in enumerate(unique, 1):
        if product_id in relevant_set:
            return 1 / rank
    return 0.0


def ndcg_at_k(
    ranked_ids: Sequence[int], relevance_grades: Mapping[int, int], k: int
) -> float | None:
    """linear gain·log2 할인 nDCG@K; IDCG가 0이면 산출 불가다."""
    _positive_k(k)
    unique, _ = unique_ranked_ids(ranked_ids)
    dcg = sum(
        relevance_grades.get(product_id, 0) / math.log2(rank + 1)
        for rank, product_id in enumerate(unique[:k], 1)
    )
    ideal = sorted(relevance_grades.items(), key=lambda item: (-item[1], item[0]))
    idcg = sum(grade / math.log2(rank + 1) for rank, (_, grade) in enumerate(ideal[:k], 1))
    return dcg / idcg if idcg else None


def filter_accuracy(expected: Mapping[str, object], actual: Mapping[str, object]) -> float:
    """기대·산출 필드 합집합에서 값이 정확히 같은 필드의 비율."""
    fields = set(expected) | set(actual)
    if not fields:
        return 1.0
    return sum(expected.get(field) == actual.get(field) for field in fields) / len(fields)


def _catalog_item(catalog: Mapping[object, Mapping[str, object]], product_id: int):
    return catalog.get(product_id) or catalog.get(str(product_id))


def hard_constraint_violations(
    ranked_ids: Sequence[int],
    hard_constraints: Mapping[str, object],
    must_exclude_product_ids: Iterable[int],
    catalog: Mapping[object, Mapping[str, object]],
) -> list[dict[str, object]]:
    """노출 상품별 하드제약 위반 내역을 결정론적 순서로 반환한다."""
    price_max = hard_constraints.get("priceMax")
    price_min = hard_constraints.get("priceMin")
    forbidden_categories = set(hard_constraints.get("forbiddenCategories") or [])
    forbidden_ids = set(hard_constraints.get("forbiddenProductIds") or [])
    must_exclude = set(must_exclude_product_ids)
    violations: list[dict[str, object]] = []
    for product_id in ranked_ids:
        product = _catalog_item(catalog, product_id)
        if product is not None:
            price = product.get("price")
            category = product.get("categoryName")
            if price_max is not None and price is not None and price > price_max:
                violations.append({"productId": product_id, "constraint": "priceMax"})
            if price_min is not None and price is not None and price < price_min:
                violations.append({"productId": product_id, "constraint": "priceMin"})
            if category in forbidden_categories:
                violations.append({"productId": product_id, "constraint": "forbiddenCategory"})
        if product_id in forbidden_ids:
            violations.append({"productId": product_id, "constraint": "forbiddenProductId"})
        if product_id in must_exclude:
            violations.append({"productId": product_id, "constraint": "mustExclude"})
    return violations


def catalog_coverage(ranked_lists: Iterable[Sequence[int]], eligible_ids: Iterable[int]) -> float:
    """실행 전체 고유 노출 상품 / search fixture 후보 합집합."""
    eligible = set(eligible_ids)
    if not eligible:
        return 0.0
    exposed = {product_id for ranked in ranked_lists for product_id in ranked}
    return len(exposed & eligible) / len(eligible)


def diversity(ranked_ids: Sequence[int], catalog: Mapping[object, Mapping[str, object]]) -> float:
    """고유 categoryName 수 / 노출 수."""
    if not ranked_ids:
        return 0.0
    categories = {
        product.get("categoryName")
        for product_id in ranked_ids
        if (product := _catalog_item(catalog, product_id)) is not None
        and product.get("categoryName")
    }
    return len(categories) / len(ranked_ids)
