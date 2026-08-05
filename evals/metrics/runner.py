"""골든 케이스 실행, 케이스·slice·전체 지표 집계."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from evals.filter_axes.metrics import aggregate_axis_metrics, case_axis_outcomes
from evals.filter_axes.spec import AXES_SPEC_PATH, axes_spec_sha256, load_axes_spec
from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import GoldenCase
from evals.metrics.behavior import evaluate_behavior_checks
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
# primary confirmatory metric은 overall.ndcgAtK.10 하나뿐이지만(README 명문), 후보 깊이가 얕은
# 동안 cutoff 민감도를 같이 보기 위해 3·5도 항상 병기한다(#333 §2.3). precision/recall/micro는
# 설정된 k_list만 쓴다 — ndcg cutoff 확장은 config 튜너블이 아니라 이 모듈의 프로토콜 상수다.
NDCG_ADDITIONAL_CUTOFFS = (3, 5)
PRIMARY_NDCG_K = 10
# manifest.confirmatory.confirmatorySlices에 있고 슬라이스 랭킹 케이스 수가 이 값 이상이면
# confirmatory, 아니면 exploratory로 자동 라벨링한다(#328 다중비교 통제 공통 규약).
MIN_CONFIRMATORY_SLICE_N = 30
# #333 리뷰 F-5-4(#329 권고 2): nDCG@10이 가장 강하게 벌하는 실패 모드("정답이 컷오프 밖으로
# 탈락")가 발동하려면 후보가 이 값을 넘어야 한다 — 이 이하 후보 비율을 산출물에 관측치로 낸다.
CANDIDATE_DEPTH_SHALLOW_THRESHOLD = 10


def _resolve_ndcg_k_list(k_list: tuple[int, ...]) -> tuple[int, ...]:
    """precision/recall용 k_list와 별도로, ndcg는 3·5·10을 항상 포함해 계산한다."""
    return tuple(sorted(set(k_list) | set(NDCG_ADDITIONAL_CUTOFFS) | {PRIMARY_NDCG_K}))


def _ndcg_cutoff_labels(ndcg_k_list: tuple[int, ...]) -> dict[str, str]:
    """cutoff별 confirmatory/exploratory 라벨을 데이터로 표시한다(#333 리뷰 F-5-5, #329 권고 1).

    primary confirmatory metric은 ``overall.ndcgAtK.10`` 1개뿐이다 — 나머지 cutoff는 k_list
    설정으로 몇 개가 늘어나든 전부 exploratory다.
    """
    return {str(k): ("primary" if k == PRIMARY_NDCG_K else "exploratory") for k in ndcg_k_list}


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


NOOP_BASELINE_DEFINITION = (
    "시스템이 실제로 노출한 상품 집합(중복 제거 후)을 productId 오름차순으로 재정렬한 것을 "
    "노출했다고 가정하는 기준선이다(#333 리뷰 F-4b — F-4의 fixture 후보 절단 정의를 대체). "
    "목적은 순수 순서 효과 격리다: 같은 노출 집합 위에서 '시스템이 고른 순서' 대 '임의(오름차순) "
    "순서'만 비교한다(#275의 no-op도 arm 간 동일 eligible 집합 위 순서 비교였다). fixture 후보 "
    "자체가 이미 productId 오름차순으로 기록되므로, 이는 '시스템 노출 집합에 fixture 순서를 "
    "적용한 것'과 동치다. 앱의 hard_filter·dedup을 거친 뒤의 실제 노출 집합을 그대로 쓰므로, "
    "F-4가 남겼던 '노출 집합 자체가 fixture 앞부분과 달라지는' 문제(#333 리뷰 F-4 스모크 실측)가 "
    "생기지 않는다."
)


def _noop_output(system_ranked_ids: Sequence[int]) -> Mapping[str, object]:
    """no-op 기준선(F-4b) — 시스템 노출 집합을 productId 오름차순으로 재정렬한다.

    fixture candidates를 별도로 참조하지 않는다 — 시스템이 이미 실제 앱 경로(hard_filter·
    dedup 포함)를 거쳐 낸 노출 집합 그 자체가 "같은 후보"의 정의이고, 거기에 임의 순서(오름차순)
    를 적용한 것이 no-op이다. 필터 추출은 하지 않는다고 간주해 extractedFilters는 빈 값이다.
    """
    return {"rankedProductIds": sorted(set(system_ranked_ids)), "extractedFilters": {}}


def _case_result(
    case: GoldenCase,
    output: Mapping[str, object],
    fixtures: EvaluationFixtures,
    k_list: tuple[int, ...],
    ndcg_k_list: tuple[int, ...],
    axes_spec: Mapping[str, Any],
) -> dict[str, Any]:
    raw_ranked = [int(product_id) for product_id in output.get("rankedProductIds", [])]
    ranked, duplicate_count = unique_ranked_ids(raw_ranked)
    actual_filters = dict(output.get("extractedFilters", {}))
    relevant = set(case.relevant_product_ids)
    excluded_reason = None
    if case.test_type != "MFT":
        # INV/DIR은 라벨 없이 규모를 늘리는 축(GUIDE §v2)이다 — member_recall_ge_guest처럼
        # 검사 자체에 라벨이 필요한 DIR도 있어(#333 Part 2) relevantProductIds가 비어있지
        # 않을 수 있다. 순위 품질 지표(nDCG 등)는 MFT 케이스로만 계산해야 한다.
        excluded_reason = "notMft"
    elif not relevant:
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
        "ndcgAtK": {str(k): ndcg_at_k(ranked, case.relevance_grades, k) for k in ndcg_k_list},
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
        "filterAxes": case_axis_outcomes(case.expected_filters, actual_filters, axes_spec),
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


def _candidate_depth_stats(depths: Sequence[int]) -> dict[str, Any]:
    """순위 평가 케이스의 후보 수 분포(#333 리뷰 F-5-4, #329 권고 2 관측 판정)."""
    if not depths:
        return {"min": None, "median": None, "max": None, "shallowCount": 0, "shallowRatio": 0.0}
    ordered = sorted(depths)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    shallow_count = sum(1 for depth in depths if depth <= CANDIDATE_DEPTH_SHALLOW_THRESHOLD)
    return {
        "min": ordered[0],
        "median": median,
        "max": ordered[-1],
        "shallowCount": shallow_count,
        "shallowRatio": shallow_count / n,
    }


def _aggregate(
    rows: Sequence[dict[str, Any]], k_list: tuple[int, ...], ndcg_k_list: tuple[int, ...]
) -> dict[str, Any]:
    ranking_rows = [row for row in rows if not row["rankingExcluded"]]
    excluded_ids = sorted(row["caseId"] for row in rows if row["rankingExcluded"])
    precision: dict[str, float] = {}
    recall: dict[str, float] = {}
    micro_precision: dict[str, float] = {}
    micro_recall: dict[str, float] = {}
    for k in k_list:
        key = str(k)
        precision[key] = _mean([row["metrics"]["precisionAtK"][key] for row in ranking_rows])
        recall[key] = _mean([row["metrics"]["recallAtK"][key] for row in ranking_rows])
        hits = sum(
            len(set(row["rankedProductIds"][:k]) & set(row["relevantProductIds"]))
            for row in ranking_rows
        )
        micro_precision[key] = hits / (len(ranking_rows) * k) if ranking_rows else 0.0
        relevant_total = sum(len(row["relevantProductIds"]) for row in ranking_rows)
        micro_recall[key] = hits / relevant_total if relevant_total else 0.0

    ndcg: dict[str, float | None] = {}
    for k in ndcg_k_list:
        key = str(k)
        ndcg_values = [
            row["metrics"]["ndcgAtK"][key]
            for row in ranking_rows
            if row["metrics"]["ndcgAtK"][key] is not None
        ]
        ndcg[key] = _mean(ndcg_values) if ndcg_values else None

    ranked_lists = [row["rankedProductIds"] for row in rows]
    eligible_ids = {product_id for row in rows for product_id in row["eligibleProductIds"]}
    return {
        "caseCount": len(rows),
        "rankingCaseCount": len(ranking_rows),
        "rankingExcludedCount": len(excluded_ids),
        "rankingExcludedCaseIds": excluded_ids,
        "ndcgCaseCount": sum(
            row["metrics"]["ndcgAtK"][str(PRIMARY_NDCG_K)] is not None for row in ranking_rows
        ),
        "precisionAtK": precision,
        "recallAtK": recall,
        "mrr": _mean([row["metrics"]["mrr"] for row in ranking_rows]),
        "ndcgAtK": ndcg,
        "microPrecisionAtK": micro_precision,
        "microRecallAtK": micro_recall,
        "filterAccuracy": _mean([row["filterAccuracy"] for row in rows]),
        "filterAxes": aggregate_axis_metrics([row["filterAxes"] for row in rows]),
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
        "candidateDepth": _candidate_depth_stats(
            [len(row["eligibleProductIds"]) for row in ranking_rows]
        ),
    }


def aggregate_by_slice(
    case_rows: Sequence[dict[str, Any]],
    k_list: tuple[int, ...],
    ndcg_k_list: tuple[int, ...],
    *,
    confirmatory_slices: frozenset[str] = frozenset(),
    min_confirmatory_n: int = MIN_CONFIRMATORY_SLICE_N,
) -> dict[str, dict[str, Any]]:
    """케이스 행 목록만으로 슬라이스별 집계를 만든다 — split과 무관하다(#144 holdout 재사용 대비).

    slice별 N(``rankingCaseCount``)이 ``min_confirmatory_n`` 이상이고 manifest가 사전 등록한
    ``confirmatory_slices``에 있으면 ``confirmatory``, 아니면 ``exploratory``를 자동으로 붙인다
    (#328 다중비교 통제 공통 규약 — 산출물이 스스로 라벨을 표시한다).
    """
    slices = sorted({slice_name for row in case_rows for slice_name in row["slices"]})
    reports: dict[str, dict[str, Any]] = {}
    for slice_name in slices:
        rows = [row for row in case_rows if slice_name in row["slices"]]
        aggregate = _aggregate(rows, k_list, ndcg_k_list)
        aggregate["confirmatoryLabel"] = (
            "confirmatory"
            if slice_name in confirmatory_slices
            and aggregate["rankingCaseCount"] >= min_confirmatory_n
            else "exploratory"
        )
        reports[slice_name] = aggregate
    return reports


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
    resolved_ndcg_k = _resolve_ndcg_k_list(resolved_k)
    resolved_cases = list(cases) if cases is not None else list(load_cases("dev"))
    resolved_fixtures = fixtures or load_evaluation_fixtures()
    confirmatory_slices = frozenset(
        resolved_fixtures.manifest.get("confirmatory", {}).get("confirmatorySlices", [])
    )
    if adapter is None:
        from evals.metrics.harness import OfflineBuyerAdapter

        adapter = OfflineBuyerAdapter()
    axes_spec = load_axes_spec()

    case_rows = [
        _case_result(
            case,
            adapter(case, resolved_fixtures),
            resolved_fixtures,
            resolved_k,
            resolved_ndcg_k,
            axes_spec,
        )
        for case in resolved_cases
    ]
    case_rows.sort(key=lambda row: row["caseId"])
    slice_reports = aggregate_by_slice(
        case_rows, resolved_k, resolved_ndcg_k, confirmatory_slices=confirmatory_slices
    )
    violations = [
        {"caseId": row["caseId"], **violation}
        for row in case_rows
        for violation in row["violations"]
    ]

    # no-op 기준선(#333 §2.2, F-4b 개정) — 시스템이 실제로 낸 노출 집합을 productId 오름차순
    # 으로 재정렬했다고 가정한 순위 지표를 시스템 출력과 나란히 낸다. 같은 k_list/ndcg_k_list
    # 규약을 그대로 쓴다.
    system_rows_by_id = {row["caseId"]: row for row in case_rows}
    noop_case_rows = [
        _case_result(
            case,
            _noop_output(system_rows_by_id[case.case_id]["rankedProductIds"]),
            resolved_fixtures,
            resolved_k,
            resolved_ndcg_k,
            axes_spec,
        )
        for case in resolved_cases
    ]
    noop_case_rows.sort(key=lambda row: row["caseId"])
    noop_slice_reports = aggregate_by_slice(
        noop_case_rows, resolved_k, resolved_ndcg_k, confirmatory_slices=confirmatory_slices
    )

    return {
        "datasetVersion": resolved_fixtures.manifest["datasetVersion"],
        "datasetHash": resolved_fixtures.manifest["datasetHash"],
        "algorithmVersion": "buyer-metrics-v1",
        "configVersion": "buyer-eval-config-v1",
        "modelConfig": dict(getattr(adapter, "model_config", {"provider": "custom"})),
        "prGateConstraints": sorted(PR_GATE_CONSTRAINTS),
        "kList": list(resolved_k),
        "ndcgKList": list(resolved_ndcg_k),
        "ndcgCutoffLabels": _ndcg_cutoff_labels(resolved_ndcg_k),
        "cases": case_rows,
        "slices": slice_reports,
        "overall": _aggregate(case_rows, resolved_k, resolved_ndcg_k),
        "violations": violations,
        "filterAxesSpec": {
            "version": axes_spec["version"],
            "sha256": axes_spec_sha256(AXES_SPEC_PATH),
            "emptyAxisRule": axes_spec["emptyAxisRule"],
        },
        "noopBaseline": {
            "definition": NOOP_BASELINE_DEFINITION,
            "cases": noop_case_rows,
            "slices": noop_slice_reports,
            "overall": _aggregate(noop_case_rows, resolved_k, resolved_ndcg_k),
        },
        # 순위 지표와 분리된 INV/DIR 검사 섹션(#333 §2.4) — 라벨 없이 노출 목록만 비교한다.
        "behaviorChecks": evaluate_behavior_checks(resolved_cases, case_rows),
    }
