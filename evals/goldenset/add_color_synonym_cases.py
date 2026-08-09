"""#474 색상 동의어 MFT fixture와 케이스를 오프라인·결정론으로 생성한다.

실행: ``uv run python -m evals.goldenset.add_color_synonym_cases``
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASE_SPECS = (
    ("네이비", "남색", "브랜드 지갑/벨트 > 멜빵/서스펜더", "멜빵", "dev-colr-001"),
    ("블랙", "검정", "브랜드 남성가방 > 크로스백", "크로스백", "dev-colr-002"),
    ("레드", "빨강", "지갑/벨트 > 동전/통장 지갑", "동전지갑", "dev-colr-003"),
)


def _has_color(product: dict) -> bool:
    attributes = product.get("attributes")
    return (
        isinstance(attributes, dict)
        and isinstance(attributes.get("색상"), str)
        and bool(attributes["색상"].strip())
    )


def _case(case_id: str, query: str, color: str, fixture_id: str, relevant: list[int]) -> dict:
    return {
        "caseId": case_id,
        "schemaVersion": "2.1.0",
        "datasetVersion": "2.3.0",
        "split": "dev",
        "slices": ["search", "guest", "single_need"],
        "query": query,
        "queryType": "simple",
        "identity": {"kind": "guest"},
        "expectedRoute": "recommend",
        "expectedFilters": {"keyword": query.split()[1], "color": color},
        "searchFixtureId": fixture_id,
        "provenance": "synthetic",
        "labeler": "labeler-03",
        "adjudicator": None,
        "createdAt": "2026-08-09",
        "notes": (
            "injected-relevant-approved: #474에서는 adjudicator 승인이 아니라 정본 색상 일치로 "
            "설계상 정답으로 선정한 오프라인 후보를 뜻한다(adjudicator 없음)."
        ),
        "labelSource": "model",
        "labeledAt": "2026-08-09",
        "labelRationale": "catalog_snapshot의 정본 색상 표기 상품만 설계상 정답으로 라벨링했으며 독립 adjudicator는 없다.",
        "testType": "MFT",
        "behaviorGroupId": None,
        "behaviorKind": None,
        "relevantProductIds": relevant,
        "relevanceGrades": {str(product_id): 2 for product_id in relevant},
        "idealOrder": relevant,
        "hardConstraints": {},
        "mustExcludeProductIds": [],
    }


def run(root: Path = ROOT) -> None:
    catalog = json.loads((root / "fixtures" / "catalog_snapshot.json").read_text(encoding="utf-8"))
    fixtures_path = root / "fixtures" / "search_responses.json"
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    cases_path = root / "cases" / "buyer_dev.jsonl"
    cases = [
        json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line
    ]
    cases = [case for case in cases if not case["caseId"].startswith("buy-colr-")]
    all_products = [catalog[key] for key in sorted(catalog, key=int)]
    new_cases: list[dict] = []
    used: set[int] = set()
    for index, (canonical, native, category, noun, fixture_id) in enumerate(CASE_SPECS, start=1):
        matching = [
            product
            for product in all_products
            if product.get("categoryName") == category
            and product.get("attributes", {}).get("색상") == canonical
            and native not in product["attributes"]["색상"]
        ]
        relevant = [int(product["productId"]) for product in matching[:3]]
        if len(relevant) < 1:
            raise ValueError(f"{canonical}: 정본 색상 정답 상품이 부족합니다")
        filler = [
            product
            for product in all_products
            if int(product["productId"]) not in used | set(relevant)
        ]
        missing_axis = [product for product in filler if not _has_color(product)][:6]
        other = [
            product
            for product in filler
            if _has_color(product) and product.get("attributes", {}).get("색상") != canonical
        ][: 30 - len(relevant) - len(missing_axis)]
        candidates = sorted(relevant + [int(p["productId"]) for p in missing_axis + other])
        used.update(candidates)
        fixtures[fixture_id] = {
            "candidates": [
                {
                    "productId": product_id,
                    "source": "injected",
                    "rule": None if product_id in relevant else "random_catalog",
                    "from": "offline_catalog_snapshot:#474-color-synonym",
                }
                for product_id in candidates
            ],
            "productIds": candidates,
            "request": {"keyword": noun, "color": native},
            "totalCount": len(candidates),
            "recordedAt": "2026-08-09T00:00:00+09:00",
            "source": "offline_catalog_snapshot",
        }
        canonical_query = f"{canonical} {noun} 추천"
        native_query = f"{native} {noun} 추천"
        new_cases.extend(
            [
                _case(f"buy-colr-{index * 2 - 1:04}", native_query, native, fixture_id, relevant),
                _case(f"buy-colr-{index * 2:04}", canonical_query, canonical, fixture_id, relevant),
            ]
        )
    cases.extend(new_cases)
    cases.sort(key=lambda case: case["caseId"])
    cases_path.write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    fixtures_path.write_text(
        json.dumps(fixtures, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    run()
