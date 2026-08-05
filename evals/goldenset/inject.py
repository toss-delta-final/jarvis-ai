"""pg-catalog 조회 전용 하드 네거티브 후보 추출(이슈 #333 §2.5, Part 2 캠페인이 쓴다).

Part 1은 이 도구를 만들고 가짜 검색 함수·가짜 DB로 단위 테스트만 고정한다 — 라이브 DB 호출은
Part 2에서 한다(CLAUDE.md·패킷 §0 실 LLM/라이브 Spring 호출 금지 규약과 같은 정신).

- ``semantic_near``: 정답 상품 임베딩의 pgvector 최근접 이웃(정답·기존 후보 제외). 결정론:
  거리·productId 오름차순 정렬. DB는 SELECT만 하고 스키마를 바꾸지 않는다. catalog_snapshot에
  없는 이웃은 제외한다(#333 리뷰 F-2) — pg-catalog에는 name/price/brand 구조화 필드가 없어
  그 후보는 가격 HCV·diversity·라벨 워크시트 계산이 불가능하다.
- ``price_violation``/``attr_violation``/``other_brand``: 케이스 하드 제약을 위반하는 같은
  카테고리 상품. price/category/attributes/brand는 pg-catalog에 없고(#65로 원본 컬럼 사본을
  이미 뺐다 — CLAUDE.md "AI Postgres에는 AI 생성물만") 이미 기록된 ``catalog_snapshot.json``
  fixture(라이브 I-1 스냅샷)에서만 가져온다 — DB 조회가 필요 없다.
- ``random_catalog``(#333 리뷰 F-5-2, #329 권고 3③): catalog 전체에서 caseId 해시로 시드한
  결정론 표본. 다른 채널이 전부 같은 카테고리·임베딩 근방에 몰려 인기 편향을 주입하는 것을
  상쇄한다.
- 병합 규칙(``merge_candidates``): 골든 결과 전부 + 주입분으로 목표(기본 30)까지 채운다. §4
  목표 혼합비 순서(semantic_near 최우선 → attr/price violation → other_brand/broadened_search/
  random_catalog)로 채우되 실제 케이스 사정으로 못 채우면 있는 만큼만 쓰고 fixture provenance가
  실제 수를 기록한다.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.core.config import Settings

EmbeddingLookup = Callable[[Sequence[int]], Mapping[int, list[float]]]
# (query_vector, exclude_ids, limit) -> [(productId, distance), ...] 거리 오름차순 결정론 정렬.
NearestNeighborFn = Callable[[Sequence[float], frozenset[int], int], list[tuple[int, float]]]

_MERGE_ORDER = (
    "semantic_near",
    "attr_violation",
    "price_violation",
    "other_brand",
    "broadened_search",
    "random_catalog",
)


def _candidate(product_id: int, rule: str, origin: str) -> dict[str, Any]:
    return {"productId": product_id, "source": "injected", "rule": rule, "from": origin}


def find_semantic_near(
    golden_product_ids: Sequence[int],
    excluded_ids: frozenset[int],
    *,
    embedding_lookup: EmbeddingLookup,
    nearest_neighbors: NearestNeighborFn,
    catalog: Mapping[str, Mapping[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """정답 상품 임베딩 각각의 pgvector 최근접 이웃을 거리·productId 오름차순으로 뽑는다.

    ``catalog``(``catalog_snapshot.json``)에 없는 이웃은 제외한다(#333 리뷰 F-2) — pg-catalog
    ``products``에는 구조화된 name/price/brand가 없어(search_doc 텍스트·extras뿐), catalog에
    없는 productId는 가격 HCV·diversity·라벨 워크시트 계산이 전부 불가능하다. Part 2는
    ``snapshot.record_snapshots_v2()``로 catalog를 먼저 넓힌 뒤 이 함수를 부른다 — 주입 풀은
    실질적으로 catalog ∩ pgvector 이웃이다.
    """
    if limit <= 0 or not golden_product_ids:
        return []
    catalog_ids = {int(key) for key in catalog}
    exclude = frozenset(excluded_ids) | frozenset(golden_product_ids)
    found: dict[int, tuple[float, int]] = {}
    for anchor_id in sorted(set(golden_product_ids)):
        vectors = embedding_lookup([anchor_id])
        vector = vectors.get(anchor_id)
        if vector is None:
            continue
        already = exclude | set(found)
        for product_id, distance in nearest_neighbors(vector, already, limit):
            if product_id not in found and product_id in catalog_ids:
                found[product_id] = (distance, anchor_id)
    ordered = sorted(found.items(), key=lambda item: (item[1][0], item[0]))[:limit]
    return [
        _candidate(product_id, "semantic_near", f"semantic_near:{anchor_id}")
        for product_id, (_, anchor_id) in ordered
    ]


def _same_category_products(
    category: str | None, catalog: Mapping[str, Mapping[str, Any]], excluded_ids: frozenset[int]
) -> list[tuple[int, Mapping[str, Any]]]:
    if not category:
        return []
    items = []
    for key, product in catalog.items():
        product_id = int(key)
        if product_id in excluded_ids:
            continue
        if product.get("categoryName") == category:
            items.append((product_id, product))
    items.sort(key=lambda item: item[0])
    return items


def find_price_violation(
    category: str | None,
    hard_constraints: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
    excluded_ids: frozenset[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """같은 카테고리에서 케이스의 priceMax/priceMin을 위반하는 상품(catalog_snapshot 기반)."""
    price_max = hard_constraints.get("priceMax")
    price_min = hard_constraints.get("priceMin")
    if limit <= 0 or (price_max is None and price_min is None):
        return []
    matches = []
    for product_id, product in _same_category_products(category, catalog, excluded_ids):
        price = product.get("price")
        if price is None:
            continue
        violates = (price_max is not None and price > price_max) or (
            price_min is not None and price < price_min
        )
        if violates:
            matches.append(product_id)
    return [_candidate(pid, "price_violation", "price_violation") for pid in matches[:limit]]


def find_attr_violation(
    category: str | None,
    attr_conditions: Mapping[str, str],
    catalog: Mapping[str, Mapping[str, Any]],
    excluded_ids: frozenset[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """같은 카테고리에서 expectedFilters의 attrConditions를 위반하는 상품."""
    if limit <= 0 or not attr_conditions:
        return []
    matches = []
    for product_id, product in _same_category_products(category, catalog, excluded_ids):
        attributes = product.get("attributes") or {}
        if any(attributes.get(attr) != value for attr, value in attr_conditions.items()):
            matches.append(product_id)
    return [_candidate(pid, "attr_violation", "attr_violation") for pid in matches[:limit]]


def find_other_brand(
    category: str | None,
    target_brands: Sequence[str],
    catalog: Mapping[str, Mapping[str, Any]],
    excluded_ids: frozenset[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """발화/필터가 브랜드를 지목한 케이스에서 같은 카테고리의 다른 브랜드 상품."""
    if limit <= 0 or not target_brands:
        return []
    excluded_brands = set(target_brands)
    matches = []
    for product_id, product in _same_category_products(category, catalog, excluded_ids):
        brand = product.get("brandName")
        if brand and brand not in excluded_brands:
            matches.append(product_id)
    return [_candidate(pid, "other_brand", "other_brand") for pid in matches[:limit]]


def find_random_catalog(
    case_id: str,
    catalog: Mapping[str, Mapping[str, Any]],
    excluded_ids: frozenset[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """catalog 전체에서 케이스별 고정 seed(caseId 해시) 기반 결정론 표본을 뽑는다.

    다른 채널(semantic_near·price/attr_violation·other_brand)은 전부 같은 카테고리나 같은
    임베딩 근방에 몰려 인기·카테고리 편향을 주입할 수 있다 — #329 권고 3③이 이를 상쇄할
    무작위 카탈로그 채널을 요구한다. 실행 시각·모듈 전역 난수 상태를 쓰지 않는다
    (``random.Random(seed)`` 로컬 인스턴스만 사용) — 같은 caseId·catalog면 항상 같은 표본이다.
    """
    if limit <= 0:
        return []
    candidate_ids = sorted(int(key) for key in catalog if int(key) not in excluded_ids)
    if not candidate_ids:
        return []
    seed = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16)
    rng = random.Random(seed)
    sample_size = min(limit, len(candidate_ids))
    sampled = sorted(rng.sample(candidate_ids, sample_size))
    return [_candidate(pid, "random_catalog", "random_catalog") for pid in sampled]


def merge_candidates(
    golden_candidates: Sequence[Mapping[str, Any]],
    injected_pools: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    target: int,
) -> list[dict[str, Any]]:
    """골든 결과 전부 + 주입분으로 target까지 채운다. 최종 순서는 productId 오름차순이다."""
    merged: dict[int, dict[str, Any]] = {
        int(candidate["productId"]): dict(candidate) for candidate in golden_candidates
    }
    for rule in _MERGE_ORDER:
        if len(merged) >= target:
            break
        for candidate in injected_pools.get(rule, []):
            if len(merged) >= target:
                break
            product_id = int(candidate["productId"])
            if product_id not in merged:
                merged[product_id] = dict(candidate)
    return [merged[product_id] for product_id in sorted(merged)]


def within_price_bounds(record: Mapping[str, Any], hard_constraints: Mapping[str, Any]) -> bool:
    price_max = hard_constraints.get("priceMax")
    price_min = hard_constraints.get("priceMin")
    if price_max is None and price_min is None:
        return True
    price = record.get("price")
    if price is None:
        return False  # 가격을 모르면 위반 여부도 확인 불가 — 안전하게 제외한다.
    if price_max is not None and price > price_max:
        return False
    return not (price_min is not None and price < price_min)


def derive_constraint_subset_candidates(
    relaxed_candidates: Sequence[Mapping[str, Any]],
    *,
    catalog: Mapping[str, Mapping[str, Any]],
    stricter_hard_constraints: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """완화(relaxed) fixture의 candidates만 걸러 강화(stricter) fixture 후보를 만든다.

    fixture ``productIds`` 집합 수준의 부분집합은 보장하지만, 평가 시점 push 절단
    (``LIST_MAX_PRODUCTS``, ascending productId 상위 K)까지는 보장하지 못한다 — 실측(#333
    라운드2)에서 강화 쪽이 완화 쪽 중간 항목을 걸러내면 뒤 항목들이 앞으로 밀려 강화 쪽 top-K에
    완화 쪽 top-K 밖의 상품이 섞여 들어왔다(behaviorChecks 18/18 미달). 노출(exposure) 수준
    보장에는 ``merge_relaxed_with_stricter_floor``를 쓴다 — 이 함수는 그 전 단계 실험으로
    남겨둔다(단위 테스트로 성질만 고정).
    """
    return [
        dict(candidate)
        for candidate in relaxed_candidates
        if within_price_bounds(
            catalog.get(str(candidate["productId"]), {}), stricter_hard_constraints
        )
    ]


def merge_relaxed_with_stricter_floor(
    relaxed_candidates: Sequence[Mapping[str, Any]],
    stricter_candidates: Sequence[Mapping[str, Any]],
    *,
    target: int,
) -> list[dict[str, Any]]:
    """완화(relaxed) fixture 후보를 강화(stricter) fixture 후보 위에 병합한다.

    #333 라운드2 F-R6 — constraint_subset DIR 쌍은 평가 시점 push가 항상 productId 오름차순
    상위 K(``LIST_MAX_PRODUCTS``)로 절단한다(F-4b no-op 정의와 같은 관례). fixture
    ``productIds`` 집합 수준의 부분집합만으로는 이 절단을 통과하지 못한다 — 강화 쪽에서 중간
    항목이 빠지면 그 뒤 항목들이 한 칸씩 당겨져 강화 쪽 top-K에 완화 쪽 top-K 밖의 상품이 섞여
    들어온다(실측: dir-subset-02/03, behaviorChecks 18/18 미달).

    강화 쪽 candidates 전원을 완화 쪽에 그대로 포함시키고, 나머지(완화 쪽 자체 검색·주입
    결과)는 강화 쪽 최대 productId보다 **큰** 것만 채운다. 이러면 완화 쪽을 오름차순 정렬했을
    때 강화 쪽 항목이 전부 앞쪽에 몰리고, 그 뒤로만 완화 전용 항목이 온다 — push가 어떤 K로
    자르든(상위 9든 30 전부든) 강화 쪽 노출이 항상 완화 쪽 노출의 부분집합이 되도록 구성적으로
    보장한다. 강화 쪽 candidates는 이 함수 호출 전에 이미 확정돼 있어야 한다(라이브 검색 순서와
    무관하게 always-included).
    """
    merged: dict[int, dict[str, Any]] = {
        int(candidate["productId"]): dict(candidate) for candidate in stricter_candidates
    }
    floor = max(merged) if merged else -1
    for candidate in relaxed_candidates:
        if len(merged) >= target:
            break
        product_id = int(candidate["productId"])
        if product_id > floor and product_id not in merged:
            merged[product_id] = dict(candidate)
    return list(merged.values())


def build_case_candidates(
    golden_candidates: Sequence[Mapping[str, Any]],
    *,
    case_id: str,
    category: str | None,
    hard_constraints: Mapping[str, Any],
    attr_conditions: Mapping[str, str],
    target_brands: Sequence[str],
    golden_product_ids: Sequence[int],
    catalog: Mapping[str, Mapping[str, Any]],
    embedding_lookup: EmbeddingLookup,
    nearest_neighbors: NearestNeighborFn,
    target: int,
) -> list[dict[str, Any]]:
    """케이스 하나의 최종 후보 30개(목표)를 만드는 오케스트레이션 — Part 2가 케이스별로 호출한다.

    가격 하드제약이 있으면 ``price_violation`` 채널을 뺀 나머지 채널(semantic_near·
    attr_violation·other_brand·random_catalog)의 주입 풀을 가격 제약 내 상품으로 좁힌다
    (#333 Part 2 라이브 실행 실측 — 가격은 Spring 서버 사이드에서만 걸러지고 AI 앱은 로컬
    재검증을 하지 않아, 가격 위반 의도가 없는 채널이 우연히 가격 위반 상품을 주입하면
    evals.metrics의 hard_filter가 걸러내지 못해 HCV로 그대로 샌다). price_violation은
    의도적으로 위반 상품을 찾아야 하므로 전체 catalog를 그대로 쓴다.
    """
    existing_ids = frozenset(int(candidate["productId"]) for candidate in golden_candidates)
    safe_catalog = catalog
    if hard_constraints.get("priceMax") is not None or hard_constraints.get("priceMin") is not None:
        safe_catalog = {
            product_id: record
            for product_id, record in catalog.items()
            if within_price_bounds(record, hard_constraints)
        }
    pools = {
        "semantic_near": find_semantic_near(
            golden_product_ids,
            existing_ids,
            embedding_lookup=embedding_lookup,
            nearest_neighbors=nearest_neighbors,
            catalog=safe_catalog,
            limit=target,
        ),
        "price_violation": find_price_violation(
            category, hard_constraints, catalog, existing_ids, limit=target
        ),
        "attr_violation": find_attr_violation(
            category, attr_conditions, safe_catalog, existing_ids, limit=target
        ),
        "other_brand": find_other_brand(
            category, target_brands, safe_catalog, existing_ids, limit=target
        ),
        "random_catalog": find_random_catalog(case_id, safe_catalog, existing_ids, limit=target),
    }
    return merge_candidates(golden_candidates, pools, target=target)


def pg_catalog_embedding_lookup(settings: Settings) -> EmbeddingLookup:
    """pg-catalog ``products.embedding``을 SELECT만으로 읽는 실 DB 구현(Part 2 라이브 호출용)."""

    def _lookup(product_ids: Sequence[int]) -> dict[int, list[float]]:
        import psycopg
        from pgvector.psycopg import register_vector

        with psycopg.connect(settings.catalog_db_url) as connection:
            register_vector(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT product_id, embedding FROM products WHERE product_id = ANY(%s)",
                    (list(product_ids),),
                )
                return {
                    int(product_id): [float(value) for value in embedding.to_list()]
                    for product_id, embedding in cursor.fetchall()
                }

    return _lookup


def pg_catalog_nearest_neighbors(settings: Settings) -> NearestNeighborFn:
    """pg-catalog ``products``의 코사인 거리 최근접 이웃을 SELECT만으로 읽는 실 DB 구현."""

    def _nearest(
        vector: Sequence[float], exclude_ids: frozenset[int], limit: int
    ) -> list[tuple[int, float]]:
        import psycopg
        from pgvector.psycopg import register_vector

        with psycopg.connect(settings.catalog_db_url) as connection:
            register_vector(connection)
            with connection.cursor() as cursor:
                excluded = list(exclude_ids) or [-1]
                # psycopg가 파이썬 list를 postgres double precision[]로 직렬화해 <=> 연산자와
                # 타입이 안 맞는다(#333 Part 2 라이브 실행에서 실측) — ::vector로 명시 캐스팅한다.
                cursor.execute(
                    "SELECT product_id, embedding <=> %s::vector AS distance FROM products "
                    "WHERE NOT (product_id = ANY(%s)) ORDER BY distance ASC, product_id ASC LIMIT %s",
                    (list(vector), excluded, limit),
                )
                return [
                    (int(product_id), float(distance)) for product_id, distance in cursor.fetchall()
                ]

    return _nearest


def build_label_worksheet(
    case_id: str,
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    catalog: Mapping[str, Mapping[str, Any]],
) -> str:
    """후보 전부의 name·categoryName·price·summary·provenance를 묶은 라벨링용 markdown 워크시트.

    Part 2 라벨링용 함수만 제공한다 — 파일로 커밋하지 않는다(패킷 §2.5).
    """
    lines = [
        f"# {case_id}",
        "",
        f"질의: {query}",
        "",
        "| productId | name | categoryName | price | source | rule | from | summary |",
        "|---|---|---|---:|---|---|---|---|",
    ]
    for candidate in candidates:
        product = catalog.get(str(candidate["productId"])) or {}
        lines.append(
            f"| {candidate['productId']} | {product.get('name', '')} | "
            f"{product.get('categoryName', '')} | {product.get('price', '')} | "
            f"{candidate['source']} | {candidate.get('rule') or ''} | "
            f"{candidate.get('from', '')} | {product.get('summary', '')} |"
        )
    return "\n".join(lines) + "\n"
