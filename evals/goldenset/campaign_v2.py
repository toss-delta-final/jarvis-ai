"""이슈 #333 Part 2 캠페인 하네스 — 케이스 정의(JSONL)를 읽어 fixture candidates를 채운다.

측정 하네스는 그 PR에 커밋한다는 공통 규약(#328, docs/lessons.md 2026-08-04 유실 사고)에 따라
이 스크립트 자체를 커밋 대상으로 둔다. 앵커는 ``--specs`` JSONL 데이터 파일이고, 이 스크립트는
그것만 읽는다.

실행 예:
    uv run python -m evals.goldenset.campaign_v2 \
        --specs /tmp/specs.jsonl \
        --recorded-at 2026-08-05T00:00:00+09:00 \
        --worksheet-dir /tmp/worksheets

케이스 정의 한 줄(JSON) 스펙(필수: caseId·expectedFilters, 나머지는 선택):
    {
      "caseId": "buy-xxx-0001",
      "searchFixtureId": "dev2-0001",        # 생략 시 caseId 그대로 사용
      "query": "실제 발화(워크시트 표시용)",
      "expectedFilters": {"keyword": "...", "category": "..."},
      "category": "...",                       # 생략 시 expectedFilters.category
      "hardConstraints": {"priceMax": 50000},  # price_violation 채굴 입력
      "attrConditions": {"소재": "린넨"},        # attr_violation 채굴 입력
      "targetBrands": ["나이키"]                # other_brand 채굴 입력(보통 expectedFilters.brand)
    }

절차(케이스별, 결정론 — 같은 입력·같은 카탈로그 상태 → 같은 출력):

0. (``--no-full-catalog-scan`` 없으면 최초 1회) ``fetch_full_catalog_via_i17``로 I-17 배치
   커서를 전량 훑어 name/category/brand/attributes만 있는 부분 레코드로 catalog 기본값을
   깐다 — 라이브 실측(#333 Part 2)에서 category-only 검색만으로는 pgvector 최근접 이웃 대부분이
   F-2(catalog 존재) 필터에 걸려 semantic_near 채굴 수율이 낮아지는 것을 확인했다. 이후 단계의
   실제 I-1 검색 결과(price 포함)가 겹치는 productId를 더 완전한 레코드로 덮어쓴다.
1. ``snapshot.record_snapshots_v2``로 골든 ``expectedFilters`` 검색(limit ``--target``)을 하고
   후보가 모자라면 완화 검색(keyword-only/category-only, limit ``--relaxed-limit``)을 별도
   요청으로 기록한다.
2. ``category``와 ``hardConstraints.forbiddenCategories``가 있으면 그 카테고리들의 "카탈로그
   확장 검색"(category-only, limit ``--catalog-search-limit``)을 카테고리별로 한 번만
   실행해 catalog_snapshot을 더 넓힌다(가격 등 완전한 I-1 레코드로 0단계 값을 덮어쓴다).
3. ``inject.build_case_candidates``로 하드 네거티브(semantic_near·price/attr_violation·
   other_brand·random_catalog)를 채운다. 주입 풀은 catalog ∩ pgvector 이웃이다.
4. 최종 candidates/productIds를 case의 fixture에 병합해 catalog_snapshot.json·
   search_responses.json에 이어쓴다(다른 케이스의 기존 fixture는 보존).
5. ``inject.build_label_worksheet``로 라벨 워크시트를 만들어 ``--worksheet-dir``에
   markdown으로 쓴다(레포에 커밋하지 않는다 — Part 1 §2.5 규약 그대로).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.schemas.spring import ProductChangesPage
from app.services.spring_client import fetch_product_changes, search_products
from evals.goldenset import inject
from evals.goldenset.inject import EmbeddingLookup, NearestNeighborFn
from evals.goldenset.snapshot import SearchFn, record_snapshots_v2

FetchChangesFn = Callable[[str | None, int], Awaitable[ProductChangesPage]]

ROOT = Path(__file__).resolve().parent
DEFAULT_TARGET = 30
DEFAULT_RELAXED_LIMIT = 120
DEFAULT_CATALOG_SEARCH_LIMIT = 200


def _read_specs(path: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                specs.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} JSON 파싱 실패: {exc}") from exc
    return specs


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


async def _record_single(
    fixture_id: str,
    filters: dict[str, Any],
    *,
    search: SearchFn,
    recorded_at: str,
    primary_limit: int,
    target_candidates: int,
    relaxed_limit: int,
) -> tuple[dict[str, dict], dict]:
    """단일 fixture를 임시 파일로 기록하고 (catalog, fixture payload)만 돌려준다."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        catalog, responses = await record_snapshots_v2(
            {fixture_id: filters},
            search=search,
            catalog_path=tmp_path / "catalog.json",
            responses_path=tmp_path / "responses.json",
            recorded_at=recorded_at,
            target_candidates=target_candidates,
            per_query_max=primary_limit,
            relaxed_limit=relaxed_limit,
        )
    return catalog, responses[fixture_id]


async def _enrich_category_catalog(
    category: str,
    *,
    search: SearchFn,
    recorded_at: str,
    catalog_search_limit: int,
) -> dict[str, dict]:
    """카테고리 전체를 category-only로 넓게 조회해 catalog_snapshot 커버리지를 늘린다."""
    fixture_id = f"__catalog_enrich__:{category}"
    catalog, _ = await _record_single(
        fixture_id,
        {"category": category},
        search=search,
        recorded_at=recorded_at,
        primary_limit=catalog_search_limit,
        # target_candidates=1: 완화(별도 category-only 재요청)를 막는다 — 이미 이 요청 자체가
        # category-only라 완화 변형과 중복이다. primary_limit은 별도 인자라 결과 개수와 무관하게
        # catalog_search_limit 그대로 유지된다.
        target_candidates=1,
        relaxed_limit=catalog_search_limit,
    )
    return catalog


async def fetch_full_catalog_via_i17(
    fetch_changes: FetchChangesFn = fetch_product_changes,
    *,
    page_limit: int = 500,
) -> dict[str, dict]:
    """I-17 배치 커서로 카탈로그 전량을 훑어 부분 레코드(name/category/brand/attributes)로 채운다.

    라이브 실행 실측(#333 Part 2)에서 category-only 검색만으로는 catalog 커버리지가 부족해
    semantic_near 채굴 후보 대부분이 F-2(catalog 존재 요구) 필터에 걸려 버려짐을 확인했다 —
    pgvector 최근접 이웃이 같은 category 밖으로도 흔히 나가기 때문이다. I-17은 price·rating·
    reviewCount가 없어(§4.8, ``ProductChange`` 스키마) 완전한 I-1 레코드는 아니지만, catalog
    존재 여부만 필요한 F-2 필터와 price/attr_violation 채굴(가격 없으면 그 채널만 못 쓴다)에는
    이 정도로 충분하다. ``ON_SALE``만 포함하고 ``HIDDEN``은 제외한다.
    """
    catalog: dict[str, dict] = {}
    cursor: str | None = None
    while True:
        page = await fetch_changes(cursor, page_limit)
        for item in page.items:
            if item.status != "ON_SALE":
                continue
            catalog[str(item.product_id)] = {
                "productId": item.product_id,
                "name": item.name,
                "summary": item.description,
                "attributes": item.attributes or {},
                "price": None,
                "rating": None,
                "reviewCount": None,
                "categoryName": item.category,
                "brandName": item.brand,
            }
        if not page.has_more or not page.next_cursor:
            break
        cursor = page.next_cursor
    return catalog


async def run_campaign(
    specs: list[dict[str, Any]],
    *,
    recorded_at: str,
    root: Path = ROOT,
    search: SearchFn = search_products,
    fetch_changes: FetchChangesFn | None = None,
    embedding_lookup: EmbeddingLookup,
    nearest_neighbors: NearestNeighborFn,
    target: int = DEFAULT_TARGET,
    relaxed_limit: int = DEFAULT_RELAXED_LIMIT,
    catalog_search_limit: int = DEFAULT_CATALOG_SEARCH_LIMIT,
) -> dict[str, str]:
    """스펙 목록을 순서대로 처리해 fixture를 채우고, caseId→라벨 워크시트 markdown을 돌려준다."""
    if not recorded_at.strip():
        raise ValueError("recorded_at은 비어 있을 수 없습니다")
    catalog_path = root / "fixtures" / "catalog_snapshot.json"
    responses_path = root / "fixtures" / "search_responses.json"
    master_catalog: dict[str, dict] = (
        json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
    )
    master_responses: dict[str, dict] = (
        json.loads(responses_path.read_text(encoding="utf-8")) if responses_path.exists() else {}
    )
    if fetch_changes is not None:
        # I-17 전량 스캔을 먼저 깔아 F-2 catalog 커버리지의 기본값으로 삼는다 — 이후 케이스별
        # 라이브 I-1 검색이 겹치는 productId를 더 완전한 레코드(price 포함)로 덮어쓴다.
        full_catalog = await fetch_full_catalog_via_i17(fetch_changes)
        master_catalog = {**full_catalog, **master_catalog}
    category_cache: dict[str, dict[str, dict]] = {}
    worksheets: dict[str, str] = {}

    for spec in specs:
        case_id = spec["caseId"]
        fixture_id = spec.get("searchFixtureId", case_id)
        expected_filters = dict(spec["expectedFilters"])
        category = spec.get("category") or expected_filters.get("category")
        constraint_pair_of = spec.get("constraintPairOf")
        if constraint_pair_of is not None:
            # #333 라운드2 F-R6 — constraint_subset DIR 쌍의 완화(relaxed) 쪽. 강화(stricter)
            # 쪽은 스펙 목록에서 반드시 먼저 나와야 한다(이미 master_responses에 기록돼 있어야
            # 함). fixture productIds 집합 수준의 부분집합만으로는 평가 시점 push 절단
            # (LIST_MAX_PRODUCTS, ascending 상위 K)을 통과하지 못한다는 것을 실측으로 확인했다
            # — inject.merge_relaxed_with_stricter_floor의 docstring 참조.
            stricter_fixture_id = constraint_pair_of
            stricter_payload = master_responses.get(stricter_fixture_id)
            if stricter_payload is None:
                raise ValueError(
                    f"{case_id}: constraintPairOf={stricter_fixture_id!r} fixture가 아직 "
                    "없습니다 — 강화(stricter) 스펙이 완화(relaxed) 스펙보다 먼저 나와야 합니다"
                )

        golden_catalog, fixture_payload = await _record_single(
            fixture_id,
            expected_filters,
            search=search,
            recorded_at=recorded_at,
            primary_limit=target,
            target_candidates=target,
            relaxed_limit=relaxed_limit,
        )
        master_catalog.update(golden_catalog)

        forbidden_categories = list(
            (spec.get("hardConstraints") or {}).get("forbiddenCategories") or []
        )
        for enrich_category in filter(None, [category, *forbidden_categories]):
            if enrich_category not in category_cache:
                category_cache[enrich_category] = await _enrich_category_catalog(
                    enrich_category,
                    search=search,
                    recorded_at=recorded_at,
                    catalog_search_limit=catalog_search_limit,
                )
            master_catalog.update(category_cache[enrich_category])

        golden_candidates = fixture_payload["candidates"]
        golden_ids = [candidate["productId"] for candidate in golden_candidates]
        if golden_ids:
            final_candidates = inject.build_case_candidates(
                golden_candidates,
                case_id=case_id,
                category=category,
                hard_constraints=spec.get("hardConstraints") or {},
                attr_conditions=spec.get("attrConditions") or {},
                target_brands=spec.get("targetBrands") or [],
                golden_product_ids=golden_ids,
                catalog=master_catalog,
                embedding_lookup=embedding_lookup,
                nearest_neighbors=nearest_neighbors,
                target=target,
            )
        else:
            # 골든 검색(primary+완화)이 0건이면 0-result failure MFT다 — 하드 네거티브를
            # 주입하면 "검색 결과 없음" 시나리오 자체가 깨진다(#333 Part 2 라이브 실측:
            # buy-fail-*/buy-cmap-0005 등 6건이 random_catalog 주입으로 오염됐었다).
            final_candidates = []

        if constraint_pair_of is not None:
            final_candidates = inject.merge_relaxed_with_stricter_floor(
                final_candidates, stricter_payload["candidates"], target=target
            )

        ordered_candidates = sorted(final_candidates, key=lambda candidate: candidate["productId"])
        fixture_payload["candidates"] = ordered_candidates
        fixture_payload["productIds"] = [c["productId"] for c in ordered_candidates]
        master_responses[fixture_id] = fixture_payload

        worksheets[case_id] = inject.build_label_worksheet(
            case_id, spec.get("query", ""), ordered_candidates, master_catalog
        )

    _write_json(catalog_path, master_catalog)
    _write_json(responses_path, master_responses)
    return worksheets


def _default_embedding_lookup(settings: Settings) -> EmbeddingLookup:
    return inject.pg_catalog_embedding_lookup(settings)


def _default_nearest_neighbors(settings: Settings) -> NearestNeighborFn:
    return inject.pg_catalog_nearest_neighbors(settings)


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    specs = _read_specs(args.specs)
    worksheets = await run_campaign(
        specs,
        recorded_at=args.recorded_at,
        root=args.root,
        fetch_changes=fetch_product_changes if not args.no_full_catalog_scan else None,
        embedding_lookup=_default_embedding_lookup(settings),
        nearest_neighbors=_default_nearest_neighbors(settings),
        target=args.target,
        relaxed_limit=args.relaxed_limit,
        catalog_search_limit=args.catalog_search_limit,
    )
    if args.worksheet_dir:
        args.worksheet_dir.mkdir(parents=True, exist_ok=True)
        for case_id, markdown in worksheets.items():
            (args.worksheet_dir / f"{case_id}.md").write_text(markdown, encoding="utf-8")
    print(f"cases={len(worksheets)} recordedAt={args.recorded_at} root={args.root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="구매자 골든셋 v2 캠페인 — fixture 라이브 기록")
    parser.add_argument("--specs", type=Path, required=True, help="케이스 정의 JSONL 경로")
    parser.add_argument("--recorded-at", required=True, help="ISO-8601 절대 시각 상수")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--relaxed-limit", type=int, default=DEFAULT_RELAXED_LIMIT)
    parser.add_argument("--catalog-search-limit", type=int, default=DEFAULT_CATALOG_SEARCH_LIMIT)
    parser.add_argument("--worksheet-dir", type=Path, default=None)
    parser.add_argument(
        "--no-full-catalog-scan",
        action="store_true",
        help="I-17 전량 스캔(F-2 catalog 커버리지 기본값 채우기)을 건너뛴다",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
