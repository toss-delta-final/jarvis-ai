"""위반 네거티브 채널 오프라인 결정론 주입 스크립트(#370, 패킷 §C).

실행: ``uv run python -m evals.goldenset.inject_violation_negatives``

라이브 I-1/pgvector 호출을 하지 않는다 — 전부 커밋된 ``fixtures/catalog_snapshot.json``·
``cases/buyer_dev.jsonl``·``fixtures/search_responses.json``만 읽고 쓴다(price/attr는 원래
``inject.py``도 DB 조회가 필요 없다고 문서화했다 — #370도 같은 정신).

세 축을 다룬다(§2 사전 등록 quota, §1 오케스트레이터 실측):

- **가격 초과**(``price_violation``): 케이스 relevantProductIds의 categoryName과 같은 카테고리 +
  hardConstraints.priceMax/priceMin 위반 + 기존 fixture candidates 제외. 오프라인 풀이 실제로
  ≥2건 있는 13개 dev 케이스(§1 실측) 전부에 주입한다 — "8개 이상 최소"를 굳이 8개로 깎지 않고
  실제로 채울 수 있는 만큼 채운다(그래야 우연한 축소 리팩터로 quota 미달이 나면 CI가 잡는다).
  ``inject.find_price_violation``(카테고리 1개)를 relevant 카테고리 집합 전부에 대해 호출해
  합친 뒤 productId 오름차순으로 케이스당 최대 4건(주입 상한)만 취한다.
- **카테고리 이탈**(``category_violation``, 신설): 대상 4케이스(buy-cmap-0004/buy-over-0003/
  buy-repu-0001/buy-repu-0003)는 이미 mustExcludeProductIds/forbiddenProductIds에 있는
  상품이 **이미 golden_filter 후보로 fixture에 존재한다** — 새 후보를 추가하는 게 아니라 그
  기존 candidate의 ``rule``만 ``category_violation``으로 재태깅한다(§1 실측이 정확히 이 4건/
  사전 등록 4케이스·케이스당 최소 1건과 일치하는 이유 — 오케스트레이터가 이 사실을 보고
  quota를 그렇게 잡았다). relabel(=mustExclude 추가)이 필요 없으므로 CHANGELOG의 relabel
  섹션이 아니라 add-provenance로만 기록한다.
- **속성 위반**(``attr_violation``): attrConditions 보유 3케이스(buy-fail-0001/buy-mult-0001/
  buy-mult-0002) 전부 케이스의 조건 키(중량/차단지수·사용감/크기)가 catalog attributes의 실제
  키(용량/SPF지수/사이즈 등)와 이름이 달라 ``schema.judge_attr_violation``(정확한 키 일치만
  판정, 동의어 매핑 없음)으로 위반을 확정할 후보가 0건이다. 조작하지 않고 미달로 보고한다
  (패킷 §2 마지막 항 명시적 허용).
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.goldenset.inject import find_price_violation
from evals.goldenset.schema import GoldenCase, load_jsonl

ROOT = Path(__file__).resolve().parent
CASE_ID_LIMIT_PER_CASE = 4

PRICE_VIOLATION_TARGET_CASES = (
    "buy-budg-0002",
    "buy-budg-0003",
    "buy-budg-0005",
    "buy-budg-0006",
    "buy-budg-0008",
    "buy-budg-0009",
    "buy-budg-0010",
    "buy-mult-0001",
    "buy-mult-0003",
    "buy-mult-0004",
    "buy-mult-0006",
    "buy-mult-0007",
    "buy-pers-0001",
)

# 카테고리 이탈 축 — 이미 mustExcludeProductIds/forbiddenProductIds에 있고 fixture에 이미
# golden_filter 후보로 존재하는 productId를 category_violation으로 재태깅할 대상.
CATEGORY_VIOLATION_RETAG_TARGETS: dict[str, list[int]] = {
    "buy-cmap-0004": [1679183612],
    "buy-over-0003": [9205089754, 9406282766],
    "buy-repu-0001": [9205089754],
    "buy-repu-0003": [9205089754],
}

# 속성 위반 축 — attrConditions 보유 3케이스. 오프라인 실측 결과 판정 가능한 후보가 0건이라
# 미달 사유를 그대로 기록한다(inject/manifest/README/GUIDE가 이 딕셔너리를 함께 인용한다).
ATTR_VIOLATION_CANDIDATE_CASES: dict[str, str] = {
    "buy-fail-0001": (
        "attrConditions {'중량': '20kg'} — relevantProductIds가 0건(failure 케이스)이라 비교할 "
        "카테고리 자체가 없고, 동종 카테고리(강아지용품 > 사료 등)의 catalog attributes에도 "
        "'중량' 키가 없다(실제 키는 '용량')."
    ),
    "buy-mult-0001": (
        "attrConditions {'차단지수': 'SPF50', '사용감': '백탁 적음'} — 동종 카테고리(선케어 > "
        "선크림/선블록 등) catalog attributes에 '차단지수'/'사용감' 키가 없다(실제 키는 "
        "'SPF지수'이며 '사용감'에 대응하는 키 자체가 catalog에 없음)."
    ),
    "buy-mult-0002": (
        "attrConditions {'크기': '15인치'} — 동종 카테고리(*여성가방/남성가방 > 노트북가방)"
        " catalog attributes에 '크기' 키가 없다(실제 키는 '사이즈')."
    ),
}


def _load_catalog(root: Path) -> dict[str, dict]:
    return json.loads((root / "fixtures" / "catalog_snapshot.json").read_text(encoding="utf-8"))


def _load_fixtures(root: Path) -> dict[str, dict]:
    return json.loads((root / "fixtures" / "search_responses.json").read_text(encoding="utf-8"))


def _relevant_categories(case: GoldenCase, catalog: dict[str, dict]) -> list[str]:
    categories: list[str] = []
    seen: set[str] = set()
    for product_id in case.relevant_product_ids:
        product = catalog.get(str(product_id))
        category = product.get("categoryName") if product else None
        if category and category not in seen:
            seen.add(category)
            categories.append(category)
    return sorted(categories)


def _price_violation_reason(case: GoldenCase, catalog: dict[str, dict], product_id: int) -> str:
    price = catalog[str(product_id)]["price"]
    hard = case.hard_constraints
    if hard.price_max is not None and price > hard.price_max:
        return "offline_catalog_snapshot:price>priceMax"
    return "offline_catalog_snapshot:price<priceMin"


def find_price_violation_pool(
    case: GoldenCase, catalog: dict[str, dict], existing_ids: frozenset[int]
) -> list[dict]:
    """케이스의 relevant 카테고리 전부에 대해 ``inject.find_price_violation``을 합쳐 부른다.

    ``inject.find_price_violation``은 카테고리 1개만 받으므로(#333 계약, 변경하지 않는다),
    relevant 상품이 여러 카테고리에 걸쳐 있으면 카테고리별로 호출해 병합한다. 이러면
    "productId ∈ ∪categories"로 판정하는 것과 동일한 풀이 되면서도 기존 시그니처를
    바꾸지 않는다.
    """
    hard = case.hard_constraints.model_dump(by_alias=True)
    merged: dict[int, dict] = {}
    for category in _relevant_categories(case, catalog):
        pool = find_price_violation(category, hard, catalog, existing_ids, limit=1000)
        for candidate in pool:
            merged.setdefault(int(candidate["productId"]), candidate)
    return [merged[product_id] for product_id in sorted(merged)]


def inject_price_violations(
    cases_by_id: dict[str, GoldenCase],
    fixtures: dict[str, dict],
    catalog: dict[str, dict],
    *,
    target_case_ids: tuple[str, ...] = PRICE_VIOLATION_TARGET_CASES,
) -> dict[str, list[int]]:
    """§2 가격 축 대상 케이스 fixture에 injected price_violation candidate를 append한다.

    반환값은 caseId별 실제 주입 productId 목록(리포트·CHANGELOG용).
    """
    injected: dict[str, list[int]] = {}
    for case_id in target_case_ids:
        case = cases_by_id[case_id]
        assert case.search_fixture_id is not None
        fixture = fixtures[case.search_fixture_id]
        already_injected = [
            int(c["productId"]) for c in fixture["candidates"] if c.get("rule") == "price_violation"
        ]
        if already_injected:
            # 재실행 멱등성 — 이미 이 축이 주입된 케이스는 건너뛴다. 그렇지 않으면 재실행마다
            # "기존 후보 제외" 풀이 달라져(방금 주입한 것도 제외 대상이 됨) 매번 다른 추가
            # productId가 append되어 재실행 시 동일 결과 규칙(패킷 §C)이 깨진다.
            injected[case_id] = sorted(already_injected)
            continue
        existing_ids = frozenset(fixture["productIds"])
        pool = find_price_violation_pool(case, catalog, existing_ids)
        chosen = pool[:CASE_ID_LIMIT_PER_CASE]
        if not chosen:
            injected[case_id] = []
            continue
        for candidate in chosen:
            product_id = int(candidate["productId"])
            fixture["candidates"].append(
                {
                    "productId": product_id,
                    "source": "injected",
                    "rule": "price_violation",
                    "from": _price_violation_reason(case, catalog, product_id),
                }
            )
        fixture["productIds"] = sorted({int(c["productId"]) for c in fixture["candidates"]})
        injected[case_id] = sorted(int(c["productId"]) for c in chosen)
    return injected


def retag_category_violations(
    cases_by_id: dict[str, GoldenCase],
    fixtures: dict[str, dict],
    *,
    targets: dict[str, list[int]] = CATEGORY_VIOLATION_RETAG_TARGETS,
) -> dict[str, list[int]]:
    """이미 존재하는 golden_filter candidate의 rule만 category_violation으로 재태깅한다.

    새 candidate를 append하지 않는다 — §1 실측이 이미 이 4케이스에 조건을 만족하는 후보가
    fixture 안에 있음을 확인했다(오케스트레이터가 이 실측치로 §2 quota를 4케이스/케이스당
    최소 1건으로 사전 등록했다).

    ``from``(채굴 출처 — ``primary``/``keyword-only`` 등)은 **건드리지 않는다**(#370 리뷰
    라운드2 F-1). ``rule``만 위반 채널 소속 표시로 바꾸면 충분하고, ``from``까지 덮어쓰면
    "이 후보가 어떻게 채굴됐나"라는 유일한 기록이 복구 불가능하게 사라진다 — provenance
    기록 이슈에서 provenance를 지우는 자기모순이다. 대상 5건 중 2건(``buy-over-0003``의
    9205089754/9406282766)은 재태깅 전 ``rule``이 이미 ``broadened_search``였다 — 그 값을
    ``category_violation``으로 덮어쓰는 것은 유지하지만(위반 채널 소속이 더 정확한 분류),
    ``from="keyword-only"``는 그대로 남아 원래 채굴 경로를 복구할 수 있다
    (`evals/goldenset/GUIDE.md`·`CHANGELOG.md`에 이 5건을 caseId·productId 단위로 명시했다).
    """
    retagged: dict[str, list[int]] = {}
    for case_id, product_ids in targets.items():
        case = cases_by_id[case_id]
        assert case.search_fixture_id is not None
        fixture = fixtures[case.search_fixture_id]
        must_exclude = set(case.must_exclude_product_ids)
        forbidden = set(case.hard_constraints.forbidden_product_ids)
        touched: list[int] = []
        for candidate in fixture["candidates"]:
            product_id = int(candidate["productId"])
            if product_id not in product_ids:
                continue
            is_violation_source = product_id in must_exclude or product_id in forbidden
            if not is_violation_source:
                continue
            candidate["rule"] = "category_violation"
            touched.append(product_id)
        retagged[case_id] = sorted(touched)
    return retagged


def _load_dev_cases(root: Path) -> list[GoldenCase]:
    return [
        case
        for case in load_jsonl(root / "cases" / "buyer_dev.jsonl", GoldenCase)
        if isinstance(case, GoldenCase)
    ]


def run(
    *,
    root: Path = ROOT,
    price_target_case_ids: tuple[str, ...] = PRICE_VIOLATION_TARGET_CASES,
    category_targets: dict[str, list[int]] = CATEGORY_VIOLATION_RETAG_TARGETS,
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """전 축을 적용하고 fixtures/search_responses.json을 제자리에서 다시 쓴다.

    반환값은 (가격 축 주입 결과, 카테고리 축 재태깅 결과) — 속성 축은 후보가 0건이라
    ``ATTR_VIOLATION_CANDIDATE_CASES``(모듈 상수)가 그 미달 사유를 대신한다.
    """
    catalog = _load_catalog(root)
    fixtures = _load_fixtures(root)
    cases_by_id = {case.case_id: case for case in _load_dev_cases(root)}

    price_result = inject_price_violations(
        cases_by_id, fixtures, catalog, target_case_ids=price_target_case_ids
    )
    category_result = retag_category_violations(cases_by_id, fixtures, targets=category_targets)

    (root / "fixtures" / "search_responses.json").write_text(
        json.dumps(fixtures, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return price_result, category_result


def main() -> int:
    price_result, category_result = run()
    price_cases = {cid: pids for cid, pids in price_result.items() if pids}
    print(f"price_violation injected into {len(price_cases)} cases: {sorted(price_cases)}")
    print(f"category_violation retagged in {len(category_result)} cases")
    print(
        "attr_violation: 0/"
        f"{len(ATTR_VIOLATION_CANDIDATE_CASES)} cases filled (documented shortfall)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
