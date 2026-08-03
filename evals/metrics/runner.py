"""골든 케이스 실행, 케이스·slice·전체 지표 집계."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import GoldenCase
from evals.metrics.metrics import (
    catalog_coverage,
    diversity,
    filter_accuracy,
    hard_constraint_violations,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    unique_ranked_ids,
)
from evals.metrics.settings import EvaluationSettings

Adapter = Callable[[GoldenCase, "EvaluationFixtures"], Mapping[str, object]]
PR_GATE_CONSTRAINTS = frozenset({"priceMax", "priceMin"})


@dataclass(frozen=True)
class EvaluationFixtures:
    """평가 실행이 소비하는 읽기 전용 fixture 묶음."""

    catalog: dict[str, dict]
    search_responses: dict[str, dict]
    purchase_history: dict[str, dict]
    manifest: dict[str, object]
    non_discriminative_case_ids: frozenset[str]


def load_evaluation_fixtures(root: Path = GOLDENSET_ROOT) -> EvaluationFixtures:
    """#142 fixture와 누출 감사 결과를 중복 로더 없이 읽는다."""

    def _json(relative: str) -> dict:
        return json.loads((root / relative).read_text(encoding="utf-8"))

    leakage = _json("audit/leakage_report.json")
    return EvaluationFixtures(
        catalog=_json("fixtures/catalog_snapshot.json"),
        search_responses=_json("fixtures/search_responses.json"),
        purchase_history=_json("fixtures/purchase_history.json"),
        manifest=_json("manifest.json"),
        non_discriminative_case_ids=frozenset(
            row["caseId"] for row in leakage["nonDiscriminativeRankingCases"]
        ),
    )


def critical_cases(cases: Sequence[GoldenCase]) -> list[GoldenCase]:
    """하드제약·must-exclude가 있거나 failure slice인 결정론적 PR 게이트 부분집합."""
    selected: list[GoldenCase] = []
    for case in cases:
        hard = case.hard_constraints
        has_constraint = bool(
            hard.price_max is not None
            or hard.price_min is not None
            or hard.forbidden_categories
            or hard.forbidden_product_ids
            or case.must_exclude_product_ids
        )
        if has_constraint or "failure" in case.slices:
            selected.append(case)
    return selected


def assert_pr_gate(report: Mapping[str, object]) -> None:
    """결정론 코드가 강제하는 가격 제약 위반이 있으면 PR gate를 실패시킨다."""
    violations = report.get("violations", [])
    gated = [
        row
        for row in violations
        if isinstance(row, Mapping) and row.get("constraint") in PR_GATE_CONSTRAINTS
    ]
    if gated:
        raise AssertionError(f"가격 hard constraint 위반: {gated}")


def _catalog_has(catalog: Mapping[object, object], product_id: int) -> bool:
    return product_id in catalog or str(product_id) in catalog


def _case_result(
    case: GoldenCase,
    output: Mapping[str, object],
    fixtures: EvaluationFixtures,
    k_list: tuple[int, ...],
) -> dict[str, Any]:
    raw_ranked = [int(product_id) for product_id in output.get("rankedProductIds", [])]
    ranked, duplicate_count = unique_ranked_ids(raw_ranked)
    actual_filters = dict(output.get("extractedFilters", {}))
    relevant = set(case.relevant_product_ids)
    excluded_reason = None
    if not relevant:
        excluded_reason = "emptyRelevance"
    elif case.case_id in fixtures.non_discriminative_case_ids:
        excluded_reason = "nonDiscriminativeRanking"

    hard = case.hard_constraints.model_dump(by_alias=True)
    violations = hard_constraint_violations(
        ranked,
        hard,
        case.must_exclude_product_ids,
        fixtures.catalog,
    )
    unknown_ids = sorted(
        product_id for product_id in ranked if not _catalog_has(fixtures.catalog, product_id)
    )
    fixture = fixtures.search_responses.get(case.search_fixture_id or "", {})
    ranking_metrics = {
        "precisionAtK": {str(k): precision_at_k(ranked, relevant, k) for k in k_list},
        "recallAtK": {str(k): recall_at_k(ranked, relevant, k) for k in k_list},
        "mrr": mean_reciprocal_rank(ranked, relevant),
        "ndcgAtK": {str(k): ndcg_at_k(ranked, case.relevance_grades, k) for k in k_list},
    }
    return {
        "caseId": case.case_id,
        "slices": list(case.slices),
        "rankedProductIds": ranked,
        "extractedFilters": actual_filters,
        "rankingExcluded": excluded_reason is not None,
        "rankingExclusionReason": excluded_reason,
        "metrics": ranking_metrics,
        "filterAccuracy": filter_accuracy(case.expected_filters, actual_filters),
        "hardConstraintViolated": bool(violations),
        "violations": violations,
        "diversity": diversity(ranked, fixtures.catalog),
        "duplicateCount": duplicate_count,
        "unknownProductIds": unknown_ids,
        "relevantProductIds": sorted(relevant),
        "eligibleProductIds": list(fixture.get("productIds", [])),
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _aggregate(rows: Sequence[dict[str, Any]], k_list: tuple[int, ...]) -> dict[str, Any]:
    ranking_rows = [row for row in rows if not row["rankingExcluded"]]
    excluded_ids = sorted(row["caseId"] for row in rows if row["rankingExcluded"])
    precision: dict[str, float] = {}
    recall: dict[str, float] = {}
    ndcg: dict[str, float | None] = {}
    micro_precision: dict[str, float] = {}
    micro_recall: dict[str, float] = {}
    for k in k_list:
        key = str(k)
        precision[key] = _mean([row["metrics"]["precisionAtK"][key] for row in ranking_rows])
        recall[key] = _mean([row["metrics"]["recallAtK"][key] for row in ranking_rows])
        ndcg_values = [
            row["metrics"]["ndcgAtK"][key]
            for row in ranking_rows
            if row["metrics"]["ndcgAtK"][key] is not None
        ]
        ndcg[key] = _mean(ndcg_values) if ndcg_values else None
        hits = sum(
            len(set(row["rankedProductIds"][:k]) & set(row["relevantProductIds"]))
            for row in ranking_rows
        )
        micro_precision[key] = hits / (len(ranking_rows) * k) if ranking_rows else 0.0
        relevant_total = sum(len(row["relevantProductIds"]) for row in ranking_rows)
        micro_recall[key] = hits / relevant_total if relevant_total else 0.0

    ranked_lists = [row["rankedProductIds"] for row in rows]
    eligible_ids = {product_id for row in rows for product_id in row["eligibleProductIds"]}
    return {
        "caseCount": len(rows),
        "rankingCaseCount": len(ranking_rows),
        "rankingExcludedCount": len(excluded_ids),
        "rankingExcludedCaseIds": excluded_ids,
        "ndcgCaseCount": sum(
            row["metrics"]["ndcgAtK"][str(k_list[0])] is not None for row in ranking_rows
        ),
        "precisionAtK": precision,
        "recallAtK": recall,
        "mrr": _mean([row["metrics"]["mrr"] for row in ranking_rows]),
        "ndcgAtK": ndcg,
        "microPrecisionAtK": micro_precision,
        "microRecallAtK": micro_recall,
        "filterAccuracy": _mean([row["filterAccuracy"] for row in rows]),
        "hardConstraintViolationRate": (
            sum(row["hardConstraintViolated"] for row in rows) / len(rows) if rows else 0.0
        ),
        "coverage": catalog_coverage(ranked_lists, eligible_ids),
        "diversity": _mean([row["diversity"] for row in rows]),
        "duplicateCount": sum(row["duplicateCount"] for row in rows),
        "duplicateCaseIds": sorted(row["caseId"] for row in rows if row["duplicateCount"]),
        "unknownProductIds": sorted(
            {product_id for row in rows for product_id in row["unknownProductIds"]}
        ),
    }


def evaluate(
    *,
    adapter: Adapter | None = None,
    split: str = "dev",
    cases: Sequence[GoldenCase] | None = None,
    fixtures: EvaluationFixtures | None = None,
    k_list: tuple[int, ...] | None = None,
    config: Settings | None = None,
) -> dict[str, Any]:
    """dev split을 실행하고 case·slice·전체 결과를 만든다.

    holdout 라벨 실행은 공개 release 평가 이슈 #144 전까지 봉인한다.
    """
    if split != "dev":
        raise NotImplementedError("sealed holdout 평가는 #144에서 구현합니다")
    settings = config or EvaluationSettings()
    resolved_k = tuple(k_list or settings.eval_buyer_k_list)
    # Settings는 로딩 시 fail-fast하고, 이 검사는 직접 전달된 k_list까지 방어한다.
    if not resolved_k or any(k <= 0 for k in resolved_k):
        raise ValueError("eval_buyer_k_list의 K는 모두 0보다 커야 합니다")
    resolved_cases = list(cases) if cases is not None else list(load_cases("dev"))
    resolved_fixtures = fixtures or load_evaluation_fixtures()
    if adapter is None:
        from evals.metrics.harness import OfflineBuyerAdapter

        adapter = OfflineBuyerAdapter()

    case_rows = [
        _case_result(case, adapter(case, resolved_fixtures), resolved_fixtures, resolved_k)
        for case in resolved_cases
    ]
    case_rows.sort(key=lambda row: row["caseId"])
    slices = sorted({slice_name for row in case_rows for slice_name in row["slices"]})
    slice_reports = {
        slice_name: _aggregate(
            [row for row in case_rows if slice_name in row["slices"]], resolved_k
        )
        for slice_name in slices
    }
    violations = [
        {"caseId": row["caseId"], **violation}
        for row in case_rows
        for violation in row["violations"]
    ]
    return {
        "datasetVersion": resolved_fixtures.manifest["datasetVersion"],
        "datasetHash": resolved_fixtures.manifest["datasetHash"],
        "algorithmVersion": "buyer-metrics-v1",
        "configVersion": "buyer-eval-config-v1",
        "modelConfig": dict(getattr(adapter, "model_config", {"provider": "custom"})),
        "prGateConstraints": sorted(PR_GATE_CONSTRAINTS),
        "kList": list(resolved_k),
        "cases": case_rows,
        "slices": slice_reports,
        "overall": _aggregate(case_rows, resolved_k),
        "violations": violations,
    }
