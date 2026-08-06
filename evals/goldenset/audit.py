"""dev와 sealed holdout 사이의 누출·근접 중복 감사."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from app.core.config import get_settings
from evals.goldenset.loader import ROOT, _load_labeled_holdout_for_audit
from evals.goldenset.schema import ALLOWED_SLICES, GoldenCase, all_candidates_are_correct


def dataset_hash(files: list[dict]) -> str:
    """manifest 자기 자신을 제외한 파일 해시의 결정론적 조합 해시."""
    lines = [f"{item['path']}:{item['sha256']}" for item in sorted(files, key=lambda x: x["path"])]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _normalize(query: str) -> str:
    return re.sub(r"\s+", " ", query.casefold()).strip()


def _trigrams(query: str) -> set[str]:
    normalized = _normalize(query)
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _jaccard(left: set, right: set) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _coverage(cases: list[GoldenCase]) -> dict[str, int]:
    counts = Counter(slice_name for case in cases for slice_name in case.slices)
    return {slice_name: counts.get(slice_name, 0) for slice_name in sorted(ALLOWED_SLICES)}


_VIOLATION_NEGATIVE_RULES = ("price_violation", "category_violation", "attr_violation")


def _violation_negative_fill(
    cases: list[GoldenCase], search_responses: dict[str, dict]
) -> dict[str, dict]:
    """#370 — rule별 위반 네거티브 태그 후보의 dev 실채움(케이스 수·후보 수)을 센다.

    사전 등록 quota(manifest.violationNegatives, §2)는 dev 기준이므로 이 함수도 전달받은
    ``cases``를 그대로 쓴다 — 호출부가 dev만 넘기면 dev 실채움, dev+holdout을 넘기면 합산이다.
    """
    candidate_counts: dict[str, dict[str, int]] = {rule: {} for rule in _VIOLATION_NEGATIVE_RULES}
    for case in cases:
        if not case.search_fixture_id:
            continue
        fixture = search_responses.get(case.search_fixture_id)
        if not fixture:
            continue
        for candidate in fixture.get("candidates", []):
            rule = candidate.get("rule")
            if rule in candidate_counts:
                per_case = candidate_counts[rule]
                per_case[case.case_id] = per_case.get(case.case_id, 0) + 1
    return {
        rule: {
            "actualCases": len(counts),
            "actualCandidates": sum(counts.values()),
            "casesCandidateCounts": dict(sorted(counts.items())),
        }
        for rule, counts in candidate_counts.items()
    }


def run_audit(
    *,
    dev_cases: list[GoldenCase] | None = None,
    holdout_cases: list[GoldenCase] | None = None,
    search_responses: dict[str, dict] | None = None,
    root: Path = ROOT,
    write: bool = True,
) -> dict:
    """중복 ID·query·정답·persona·fixture와 slice 커버리지를 감사한다."""
    config = get_settings()
    dev = dev_cases if dev_cases is not None else _load_labeled_holdout_for_audit("dev", root=root)
    holdout = (
        holdout_cases
        if holdout_cases is not None
        else _load_labeled_holdout_for_audit("holdout", root=root)
    )
    violations: list[dict] = []
    warnings: list[dict] = []
    query_pairs: list[dict] = []
    relevant_pairs: list[dict] = []
    all_cases = [*dev, *holdout]
    responses = (
        search_responses
        if search_responses is not None
        else json.loads((root / "fixtures" / "search_responses.json").read_text(encoding="utf-8"))
    )
    counts = Counter(case.case_id for case in all_cases)
    for case_id, count in counts.items():
        if count > 1:
            violations.append({"kind": "duplicateCaseId", "caseId": case_id, "count": count})
    for index, left in enumerate(all_cases):
        for right in all_cases[index + 1 :]:
            query_score = _jaccard(_trigrams(left.query), _trigrams(right.query))
            if query_score > config.goldenset_near_dup_jaccard_max:
                item = {
                    "kind": "queryNearDuplicate",
                    "left": left.case_id,
                    "right": right.case_id,
                    "score": round(query_score, 6),
                }
                query_pairs.append(item)
                (violations if left.split != right.split else warnings).append(item)
            if left.split != right.split:
                relevant_score = _jaccard(
                    set(left.relevant_product_ids), set(right.relevant_product_ids)
                )
                if relevant_score > config.goldenset_near_dup_relevant_overlap_max:
                    item = {
                        "kind": "relevantSetOverlap",
                        "left": left.case_id,
                        "right": right.case_id,
                        "score": round(relevant_score, 6),
                    }
                    relevant_pairs.append(item)
                    violations.append(item)
    for field, kind in (
        ("persona_id", "personaOverlap"),
        ("search_fixture_id", "catalogScenarioOverlap"),
    ):
        dev_values: dict[str, list[str]] = {}
        holdout_values: dict[str, list[str]] = {}
        for case, target in [
            *((case, dev_values) for case in dev),
            *((case, holdout_values) for case in holdout),
        ]:
            value = case.identity.persona_id if field == "persona_id" else case.search_fixture_id
            if value:
                target.setdefault(value, []).append(case.case_id)
        for value in sorted(set(dev_values) & set(holdout_values)):
            violations.append(
                {
                    "kind": kind,
                    "value": value,
                    "devCases": dev_values[value],
                    "holdoutCases": holdout_values[value],
                }
            )
    coverage = {"dev": _coverage(dev), "holdout": _coverage(holdout)}
    for slice_name, count in coverage["dev"].items():
        if count == 0:
            violations.append({"kind": "missingDevSlice", "slice": slice_name})
    required_holdout = {
        "search",
        "personalization",
        "repurchase",
        "category_mapping_failure",
        "failure",
    }
    for slice_name in sorted(required_holdout):
        if coverage["holdout"][slice_name] == 0:
            violations.append({"kind": "missingHoldoutSlice", "slice": slice_name})
    total_cases = len(all_cases)
    actual_holdout_ratio = len(holdout) / total_cases if total_cases else 0.0
    holdout_ratio_tolerance = max(0.05, 1 / total_cases) if total_cases else 0.05
    if abs(actual_holdout_ratio - config.goldenset_holdout_ratio) > holdout_ratio_tolerance:
        warnings.append(
            {
                "kind": "holdoutRatioDrift",
                "actual": round(actual_holdout_ratio, 6),
                "target": config.goldenset_holdout_ratio,
                "tolerance": round(holdout_ratio_tolerance, 6),
            }
        )
    non_discriminative_ranking_cases = []
    for case in all_cases:
        if "failure" in case.slices:
            continue
        candidate_ids = responses[case.search_fixture_id]["productIds"]
        # F-1(#333 리뷰): 개수 비교가 아니라 집합 포함으로 "전부 정답"(비판별)을 판정한다.
        if all_candidates_are_correct(candidate_ids, case.relevant_product_ids):
            non_discriminative_ranking_cases.append(
                {
                    "caseId": case.case_id,
                    "candidateCount": len(candidate_ids),
                    "relevantCount": len(case.relevant_product_ids),
                }
            )
    if non_discriminative_ranking_cases:
        warnings.append(
            {
                "kind": "nonDiscriminativeRanking",
                "cases": non_discriminative_ranking_cases,
            }
        )
    coverage_limitations = [
        f"holdout {slice_name} slice 표본이 0건입니다"
        for slice_name in (
            "cold_start",
            "multi_constraint",
            "personalization_overreach",
        )
        if coverage["holdout"][slice_name] == 0
    ]
    report = {
        "summary": {
            "violationCount": len(violations),
            "warningCount": len(warnings),
            "devCases": len(dev),
            "holdoutCases": len(holdout),
        },
        "violations": violations,
        "warnings": warnings,
        "queryPairs": query_pairs,
        "relevantPairs": relevant_pairs,
        "holdoutRatio": {
            "actual": round(actual_holdout_ratio, 6),
            "target": config.goldenset_holdout_ratio,
            "tolerance": round(holdout_ratio_tolerance, 6),
        },
        "coverage": coverage,
        "coverageLimitations": coverage_limitations,
        "nonDiscriminativeRankingCases": non_discriminative_ranking_cases,
        "violationNegativeFill": {
            "dev": _violation_negative_fill(dev, responses),
        },
    }
    if write:
        audit_dir = root / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "leakage_report.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        warning_lines = []
        for item in warnings:
            if item["kind"] == "queryNearDuplicate":
                warning_lines.append(
                    f"- `{item['left']}` ↔ `{item['right']}`: query 3-gram Jaccard "
                    f"{item['score']:.3f} (동일 split의 의도적 표현 변형)"
                )
            elif item["kind"] == "holdoutRatioDrift":
                warning_lines.append(
                    f"- holdout 비중 {item['actual']:.3f}: 목표 {item['target']:.3f}에서 "
                    f"허용 오차 {item['tolerance']:.3f}보다 크게 벗어남"
                )
            elif item["kind"] == "nonDiscriminativeRanking":
                cases = ", ".join(
                    f"`{case['caseId']}` ({case['candidateCount']}/{case['relevantCount']})"
                    for case in item["cases"]
                )
                warning_lines.append(f"- 순위 판별 불가 후보/정답: {cases}")
        if not warning_lines:
            warning_lines = ["- 없음"]
        violation_negative_fill = report["violationNegativeFill"]["dev"]
        violation_negative_lines = [
            f"- `{rule}`: 케이스 {data['actualCases']}건 / 후보 {data['actualCandidates']}건"
            for rule, data in sorted(violation_negative_fill.items())
        ]
        markdown = (
            "# 구매자 골든셋 누출 감사\n\n"
            f"- 위반: **{len(violations)}건**\n"
            f"- 경고: **{len(warnings)}건**\n"
            f"- dev/holdout: {len(dev)}/{len(holdout)}\n\n"
            "## 경고와 사유\n\n"
            + "\n".join(warning_lines)
            + "\n\n후보가 전부 정답인 케이스들은 순위 지표"
            "(nDCG·MRR·Precision@k)의 분모에서 제외해야 한다. "
            "노출·필터·하드제약 검증에는 계속 사용한다."
            + "\n\n## 위반 네거티브 채널 실채움(dev, #370)\n\n"
            "manifest.json의 `violationNegatives`가 유형별 최소 케이스·최소 후보 사전 등록치를,"
            " 아래는 실제 채운 값을 담는다(둘의 대조는 CI 유닛 테스트가 강제한다).\n\n"
            + "\n".join(violation_negative_lines)
            + "\n\n## 커버리지\n\n```json\n"
            + json.dumps(coverage, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n```\n\n## v1 커버리지 한계\n\n"
            + "\n".join(f"- {item}" for item in coverage_limitations)
            + "\n"
        )
        (audit_dir / "leakage_report.md").write_text(markdown, encoding="utf-8")
    return report
