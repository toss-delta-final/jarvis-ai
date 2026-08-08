"""#474 색상 동의어 평가 팔과 승인 사전 로더."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.pipelines import color_synonyms
from evals.goldenset.loader import load_cases
from evals.metrics.harness import OfflineBuyerAdapter, classify_color_exposure
from evals.metrics.runner import evaluate, load_evaluation_fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = REPO_ROOT / "db" / "catalog" / "seed" / "color_synonyms.json"


@lru_cache(maxsize=1)
def load_approved_color_synonym_map() -> dict[str, list[str]]:
    """DB 없이 승인된 정본 행만으로 프로덕션 동의어 맵을 만든다."""
    rows = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return color_synonyms.build_synonym_map(
        (row["term"], row["canonical"], row["doc_count"])
        for row in rows
        if row["status"] == "approved" and row["canonical"] is not None
    )


def _exposure_breakdown(
    product_ids: list[int], catalog: dict[str, dict], requested: list[str]
) -> dict[str, int]:
    """노출을 색상축 없음·일치·불일치로 분해한다."""
    result = {"missingColorAxis": 0, "matchedColorAxis": 0, "mismatchedColorAxis": 0}
    for product_id in product_ids:
        product = catalog[str(product_id)]
        result[classify_color_exposure(product, requested)] += 1
    return result


def evaluate_color_expansion() -> dict[str, object]:
    """#474 신규 MFT 6건을 동의어 확장 off/on으로 결정론 실행한다."""
    cases = [case for case in load_cases("dev") if case.case_id.startswith("buy-colr-")]
    fixtures = load_evaluation_fixtures()
    off = evaluate(
        cases=cases, fixtures=fixtures, adapter=OfflineBuyerAdapter(color_expansion=False)
    )
    on_adapter = OfflineBuyerAdapter(color_expansion=True)
    on = evaluate(cases=cases, fixtures=fixtures, adapter=on_adapter)
    off_rows = {row["caseId"]: row for row in off["cases"]}
    on_rows = {row["caseId"]: row for row in on["cases"]}
    rows = []
    for case in cases:
        before, after = off_rows[case.case_id], on_rows[case.case_id]
        requested = [str(case.expected_filters["color"])]
        expanded = color_synonyms.expand_color(requested[0], load_approved_color_synonym_map())
        rows.append(
            {
                "caseId": case.case_id,
                "recallAt10": {
                    "off": before["metrics"]["recallAtK"]["10"],
                    "on": after["metrics"]["recallAtK"]["10"],
                },
                "ndcgAt10": {
                    "off": before["metrics"]["ndcgAtK"]["10"],
                    "on": after["metrics"]["ndcgAtK"]["10"],
                },
                "exposure": {
                    "off": len(before["rankedProductIds"]),
                    "on": len(after["rankedProductIds"]),
                    "delta": len(after["rankedProductIds"]) - len(before["rankedProductIds"]),
                },
                "exposureBreakdown": {
                    "off": _exposure_breakdown(
                        before["rankedProductIds"], fixtures.catalog, requested
                    ),
                    "on": _exposure_breakdown(
                        after["rankedProductIds"], fixtures.catalog, expanded
                    ),
                },
                "offProductIds": before["rankedProductIds"],
                "onProductIds": after["rankedProductIds"],
                "relevantProductIds": case.relevant_product_ids,
                "isCanonical": expanded[0] == requested[0],
            }
        )
    native = [row for row in rows if not row["isCanonical"]]
    canonical = [row for row in rows if row["isCanonical"]]
    return {
        "cases": rows,
        "nativeDeltaSummary": {
            "count": len(native),
            "recallDelta": sum(r["recallAt10"]["on"] - r["recallAt10"]["off"] for r in native),
        },
        "canonicalDeltaSummary": {
            "count": len(canonical),
            "recallDelta": sum(r["recallAt10"]["on"] - r["recallAt10"]["off"] for r in canonical),
        },
    }
