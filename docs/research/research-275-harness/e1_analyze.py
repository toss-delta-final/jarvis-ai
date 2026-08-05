#!/usr/bin/env python3
"""E1 — 커밋된 ablation 산출물(pipeline/scoring/single_call results.json) 재분석.
실 LLM 호출 없음. 입력은 evals/ablation/baselines/20260803-dev-full-n5/**(읽기 전용)뿐이다.
출력: <out>/e1_results.json, <out>/e1_report.md.
"""

import argparse
import json
import statistics as st
from collections import defaultdict
from itertools import combinations
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("pyproject.toml을 찾지 못해 저장소 루트를 확정할 수 없다")


REPO_ROOT = _repo_root()
BASE = REPO_ROOT / "evals/ablation/baselines/20260803-dev-full-n5"
PRICING = REPO_ROOT / "evals/model_eval/pricing_manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="e1_results.json/e1_report.md를 쓸 출력 디렉터리(리포 밖 권장)"
    )
    return parser.parse_args()


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def kendall_tau_b_common(rank_a, rank_b):
    """공통 항목만으로 Kendall tau-b 계산 (순수 파이썬, scipy 미사용).
    rank_a, rank_b: 순서 리스트(1위가 index 0)."""
    common = [x for x in rank_a if x in rank_b]
    if len(common) < 2:
        return None
    pos_a = {v: i for i, v in enumerate(rank_a)}
    pos_b = {v: i for i, v in enumerate(rank_b)}
    n = len(common)
    concordant = 0
    discordant = 0
    ties_a = 0
    ties_b = 0
    for i, j in combinations(range(n), 2):
        a1, a2 = pos_a[common[i]], pos_a[common[j]]
        b1, b2 = pos_b[common[i]], pos_b[common[j]]
        sign_a = (a1 > a2) - (a1 < a2)
        sign_b = (b1 > b2) - (b1 < b2)
        if sign_a == 0:
            ties_a += 1
        if sign_b == 0:
            ties_b += 1
        if sign_a == 0 or sign_b == 0:
            continue
        if sign_a == sign_b:
            concordant += 1
        else:
            discordant += 1
    n0 = n * (n - 1) / 2
    denom = ((n0 - ties_a) * (n0 - ties_b)) ** 0.5
    if denom == 0:
        return None
    return (concordant - discordant) / denom


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def ols_1d(xs, ys):
    """순수 파이썬 최소제곱 1차 회귀. y = a + b*x. 반환 (a, b, r2, max_abs_resid, n)."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    if sxx == 0:
        return None
    b = sxy / sxx
    a = mean_y - b * mean_x
    preds = [a + b * x for x in xs]
    resids = [y - p for y, p in zip(ys, preds, strict=True)]
    ss_res = sum(r**2 for r in resids)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot != 0 else None
    max_abs_resid = max(abs(r) for r in resids)
    return {"intercept": a, "slope": b, "r2": r2, "max_abs_resid": max_abs_resid, "n": n}


def pct(xs, p):
    """p in [0,100], 선형보간 percentile (순수 파이썬)."""
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pipeline = load(BASE / "pipeline/results.json")
    scoring = load(BASE / "scoring/results.json")
    single_call = load(BASE / "single_call/results.json")
    pricing = load(PRICING)

    smart_price = next(e for e in pricing["entries"] if e["model"] == "gpt-5.6-luna")
    in_per1k = smart_price["inPer1k"]
    out_per1k = smart_price["outPer1k"]

    case_results = pipeline["caseResults"]

    # 케이스별 5반복 그룹핑
    by_case = defaultdict(list)
    for r in case_results:
        by_case[r["caseId"]].append(r)
    for cid in by_case:
        by_case[cid].sort(key=lambda r: r["repeat"])

    ranking_excluded_case_ids = {e["caseId"] for e in pipeline["rankingExcludedCases"]}

    # ---------------- E1-a: teacher 반복 안정성 ----------------
    e1a_per_case = {}
    for cid, reps in by_case.items():
        ranks = [r["rankedProductIds"] for r in reps]
        top1s = [r[0] if r else None for r in ranks]
        top1_counts = defaultdict(int)
        for t in top1s:
            top1_counts[t] += 1
        top1_mode_ratio = max(top1_counts.values()) / len(top1s) if top1s else None

        top5_sets = [r[:5] for r in ranks]
        pair_jaccards = []
        for i, j in combinations(range(len(top5_sets)), 2):
            pair_jaccards.append(jaccard(top5_sets[i], top5_sets[j]))
        avg_top5_jaccard = sum(pair_jaccards) / len(pair_jaccards) if pair_jaccards else None

        taus = []
        missing_pairs = 0
        for i, j in combinations(range(len(ranks)), 2):
            tau = kendall_tau_b_common(ranks[i], ranks[j])
            if tau is None:
                missing_pairs += 1
            else:
                taus.append(tau)
        avg_tau = sum(taus) / len(taus) if taus else None

        all_identical = len({tuple(r) for r in ranks}) == 1

        e1a_per_case[cid] = {
            "rankingExcluded": cid in ranking_excluded_case_ids,
            "topOneModeRatio": top1_mode_ratio,
            "avgTop5Jaccard": avg_top5_jaccard,
            "avgKendallTauBCommon": avg_tau,
            "kendallMissingPairs": missing_pairs,
            "kendallPairsTotal": len(list(combinations(range(len(ranks)), 2))),
            "allFiveIdentical": all_identical,
            "nRepeats": len(ranks),
        }

    def summarize(values):
        vals = [v for v in values if v is not None]
        if not vals:
            return {"mean": None, "median": None, "min": None, "n": 0}
        return {"mean": sum(vals) / len(vals), "median": st.median(vals), "min": min(vals), "n": len(vals)}

    def e1a_summary(include_excluded):
        cases = {
            cid: v for cid, v in e1a_per_case.items() if include_excluded or not v["rankingExcluded"]
        }
        n_identical = sum(1 for v in cases.values() if v["allFiveIdentical"])
        return {
            "nCases": len(cases),
            "topOneModeRatio": summarize([v["topOneModeRatio"] for v in cases.values()]),
            "avgTop5Jaccard": summarize([v["avgTop5Jaccard"] for v in cases.values()]),
            "avgKendallTauBCommon": summarize([v["avgKendallTauBCommon"] for v in cases.values()]),
            "allFiveIdenticalCount": n_identical,
            "allFiveIdenticalRatio": n_identical / len(cases) if cases else None,
        }

    e1a = {
        "perCase": e1a_per_case,
        "summaryIncludingExcluded": e1a_summary(True),
        "summaryExcludingExcluded": e1a_summary(False),
    }

    # ---------------- E1-b: teacher 품질 반복 분산 ----------------
    e1b_per_case = {}
    for cid, reps in by_case.items():
        ndcgs_raw = [r["metrics"]["metrics"]["ndcgAtK"]["10"] for r in reps]
        ndcgs = [v for v in ndcgs_raw if v is not None]
        n_null = len(ndcgs_raw) - len(ndcgs)
        e1b_per_case[cid] = {
            "rankingExcluded": cid in ranking_excluded_case_ids,
            "ndcgAtK10ByRepeat": ndcgs_raw,
            "nNullRepeats": n_null,
            "sd": st.pstdev(ndcgs) if len(ndcgs) > 1 else (0.0 if ndcgs else None),
            "range": (max(ndcgs) - min(ndcgs)) if ndcgs else None,
        }

    included_cids = [cid for cid in by_case if cid not in ranking_excluded_case_ids]
    per_repeat_means_included = {}
    for rep_idx in range(5):
        vals = [
            [r for r in by_case[cid] if r["repeat"] == rep_idx][0]["metrics"]["metrics"]["ndcgAtK"]["10"]
            for cid in included_cids
        ]
        per_repeat_means_included[rep_idx] = sum(vals) / len(vals) if vals else None

    overall_mean_included = sum(per_repeat_means_included.values()) / len(per_repeat_means_included)
    committed_ndcg = pipeline["primarySummary"]["mean"]

    e1b = {
        "perCase": e1b_per_case,
        "perRepeatMeanNdcgAt10_includedCases": per_repeat_means_included,
        "overallMeanOfPerRepeatMeans_includedCases": overall_mean_included,
        "committedPrimarySummaryMean": committed_ndcg,
        "reproducedWithinTolerance1e6": abs(overall_mean_included - committed_ndcg) < 1e-6,
        "diffFromCommitted": overall_mean_included - committed_ndcg,
        "note": (
            "커밋된 primarySummary.mean 은 case-level 평균 5개 nDCG(반복 평균 후 케이스 평균)일 "
            "가능성이 있어 두 집계 순서를 모두 확인함(아래 caseLevelThenOverall 참조)."
        ),
    }

    case_level_means = []
    for cid in included_cids:
        ndcgs = [r["metrics"]["metrics"]["ndcgAtK"]["10"] for r in by_case[cid]]
        case_level_means.append(sum(ndcgs) / len(ndcgs))
    case_level_then_overall = sum(case_level_means) / len(case_level_means)
    e1b["caseLevelThenOverall_includedCases"] = case_level_then_overall
    e1b["caseLevelThenOverall_reproducedWithinTolerance1e6"] = (
        abs(case_level_then_overall - committed_ndcg) < 1e-6
    )
    e1b["caseLevelThenOverall_diffFromCommitted"] = case_level_then_overall - committed_ndcg
    e1b["caseLevelMeans_summary"] = {
        "mean": case_level_then_overall,
        "median": st.median(case_level_means),
        "sd": st.pstdev(case_level_means),
        "n": len(case_level_means),
    }

    # ---------------- E1-c: 후보 수 K 대 토큰/비용 ----------------
    rerank_rows = []
    no_rerank_count = 0
    for r in case_results:
        K = len(r["metrics"]["eligibleProductIds"])
        rerank_calls = [c for c in r["providerCalls"] if c["callSite"] == "rerank"]
        if not rerank_calls:
            no_rerank_count += 1
            continue
        for c in rerank_calls:
            rerank_rows.append(
                {
                    "caseId": r["caseId"],
                    "repeat": r["repeat"],
                    "K": K,
                    "inputTokens": c["inputTokens"],
                    "outputTokens": c["outputTokens"],
                    "costUsd": c["costUsd"],
                    "cacheTokens": c.get("cacheTokens"),
                    "latencyMs": c["latencyMs"],
                }
            )

    Ks = [row["K"] for row in rerank_rows]
    reg_input = ols_1d(Ks, [row["inputTokens"] for row in rerank_rows])
    reg_output = ols_1d(Ks, [row["outputTokens"] for row in rerank_rows])
    reg_cost = ols_1d(Ks, [row["costUsd"] for row in rerank_rows])

    e1c = {
        "nRerankCalls": len(rerank_rows),
        "nCaseRepeatWithoutRerankCall": no_rerank_count,
        "regressionInputTokensOnK": reg_input,
        "regressionOutputTokensOnK": reg_output,
        "regressionCostUsdOnK": reg_cost,
    }

    # ---------------- E1-d: 비용 요약 ----------------
    rerank_costs = [row["costUsd"] for row in rerank_rows]
    decompose_calls = [c for r in case_results for c in r["providerCalls"] if c["callSite"] == "decompose"]
    decompose_costs = [c["costUsd"] for c in decompose_calls]
    rerank_cache_tokens = [row["cacheTokens"] for row in rerank_rows]
    decompose_cache_tokens = [c.get("cacheTokens") for c in decompose_calls]

    def cost_stats(costs):
        return {
            "n": len(costs),
            "total": sum(costs),
            "mean": sum(costs) / len(costs) if costs else None,
            "median": st.median(costs) if costs else None,
            "p95": pct(costs, 95) if costs else None,
        }

    e1d = {
        "rerank": cost_stats(rerank_costs),
        "decompose": cost_stats(decompose_costs),
        "rerankCacheTokens": {
            "zeroCount": sum(1 for t in rerank_cache_tokens if t == 0),
            "nonZeroCount": sum(1 for t in rerank_cache_tokens if t and t > 0),
            "mean": (
                sum(t for t in rerank_cache_tokens if t is not None) / len(rerank_cache_tokens)
                if rerank_cache_tokens
                else None
            ),
            "distinctValues_sample": sorted(set(rerank_cache_tokens))[:20],
        },
        "decomposeCacheTokens": {
            "zeroCount": sum(1 for t in decompose_cache_tokens if t == 0),
            "nonZeroCount": sum(1 for t in decompose_cache_tokens if t and t > 0),
            "mean": (
                sum(t for t in decompose_cache_tokens if t is not None) / len(decompose_cache_tokens)
                if decompose_cache_tokens
                else None
            ),
        },
    }

    # ---------------- E1-e: 단가 재현 ----------------
    mismatches = []
    for row in rerank_rows:
        expected = row["inputTokens"] * in_per1k / 1000 + row["outputTokens"] * out_per1k / 1000
        diff = row["costUsd"] - expected
        if abs(diff) > 1e-9:
            mismatches.append(
                {
                    "caseId": row["caseId"],
                    "repeat": row["repeat"],
                    "costUsd": row["costUsd"],
                    "expected": expected,
                    "diff": diff,
                }
            )
    e1e = {
        "pricingUsed": {"model": smart_price["model"], "inPer1k": in_per1k, "outPer1k": out_per1k},
        "nChecked": len(rerank_rows),
        "nMismatches": len(mismatches),
        "mismatches": mismatches[:50],
        "allMatchWithinTolerance": len(mismatches) == 0,
    }

    reference_check = {
        "rerankCallCount": {"computed": len(rerank_rows), "reference": 135},
        "rerankTotalCostUsd": {"computed": sum(rerank_costs), "reference": 0.10520060},
        "rerankMeanCostUsd": {
            "computed": sum(rerank_costs) / len(rerank_costs) if rerank_costs else None,
            "reference": 0.00077926,
        },
        "rerankInputTokensMedian": {
            "computed": st.median([row["inputTokens"] for row in rerank_rows]) if rerank_rows else None,
            "reference": 926,
        },
        "rerankInputTokensP95": {
            "computed": pct([row["inputTokens"] for row in rerank_rows], 95) if rerank_rows else None,
            "reference": 3171,
        },
        "rerankOutputTokensMean": {
            "computed": (
                sum(row["outputTokens"] for row in rerank_rows) / len(rerank_rows) if rerank_rows else None
            ),
            "reference": 419.274,
        },
        "rerankLatencyMsMean": {
            "computed": (
                sum(row["latencyMs"] for row in rerank_rows) / len(rerank_rows) if rerank_rows else None
            ),
            "reference": 4262.874,
        },
        "rerankLatencyMsP95": {
            "computed": pct([row["latencyMs"] for row in rerank_rows], 95) if rerank_rows else None,
            "reference": 7590,
        },
        "decomposeCallCount": {"computed": len(decompose_calls), "reference": 155},
        "decomposeCacheTokensMedian": {
            "computed": (
                st.median([t for t in decompose_cache_tokens if t is not None])
                if decompose_cache_tokens
                else None
            ),
            "reference": 2816,
        },
        "errorCount": {
            "computed": sum(1 for r in case_results for c in r["providerCalls"] if c.get("error")),
            "reference": 0,
        },
    }

    arm_primary_summaries = {
        "pipeline": pipeline["primarySummary"],
        "scoring": scoring["primarySummary"],
        "single_call": single_call["primarySummary"],
    }

    results = {
        "armPrimarySummaries": arm_primary_summaries,
        "inputFiles": {
            "pipeline": str(BASE / "pipeline/results.json"),
            "scoring": str(BASE / "scoring/results.json"),
            "single_call": str(BASE / "single_call/results.json"),
            "pricingManifest": str(PRICING),
        },
        "e1a_teacherRepeatStability": e1a,
        "e1b_teacherQualityRepeatVariance": e1b,
        "e1c_tokensVsK": e1c,
        "e1c_rerankRowsRaw": rerank_rows,
        "e1d_costSummary": e1d,
        "e1e_pricingReproduction": e1e,
        "referenceCheck": reference_check,
    }

    (out_dir / "e1_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = []
    lines.append("# E1 — 커밋된 ablation 산출물 재분석\n")
    lines.append(f"입력: `{BASE}`\n")

    lines.append("## E1-a teacher 반복 안정성\n")
    lines.append("### 포함(rankingExcluded 제외, 18케이스)\n")
    s = e1a["summaryExcludingExcluded"]
    lines.append(
        f"- top-1 최빈값 비율: mean={s['topOneModeRatio']['mean']:.4f}, "
        f"median={s['topOneModeRatio']['median']:.4f}, min={s['topOneModeRatio']['min']:.4f}"
    )
    lines.append(
        f"- top-5 평균 pairwise Jaccard: mean={s['avgTop5Jaccard']['mean']:.4f}, "
        f"median={s['avgTop5Jaccard']['median']:.4f}, min={s['avgTop5Jaccard']['min']:.4f}"
    )
    lines.append(
        f"- 공통항목 Kendall tau-b 평균: mean={s['avgKendallTauBCommon']['mean']:.4f}, "
        f"median={s['avgKendallTauBCommon']['median']:.4f}, min={s['avgKendallTauBCommon']['min']:.4f}"
    )
    lines.append(
        f"- 5회 완전 동일 순위 케이스: {s['allFiveIdenticalCount']}/{s['nCases']} "
        f"({s['allFiveIdenticalRatio']:.4f})\n"
    )
    lines.append("### 전체(rankingExcluded 포함, 31케이스)\n")
    s2 = e1a["summaryIncludingExcluded"]
    lines.append(
        f"- top-1 최빈값 비율: mean={s2['topOneModeRatio']['mean']:.4f}, "
        f"median={s2['topOneModeRatio']['median']:.4f}, min={s2['topOneModeRatio']['min']:.4f}"
    )
    lines.append(f"- top-5 평균 pairwise Jaccard: mean={s2['avgTop5Jaccard']['mean']:.4f}")
    lines.append(f"- 공통항목 Kendall tau-b 평균: mean={s2['avgKendallTauBCommon']['mean']:.4f}")
    lines.append(f"- 5회 완전 동일 순위 케이스: {s2['allFiveIdenticalCount']}/{s2['nCases']}\n")

    lines.append("## E1-b teacher 품질 반복 분산\n")
    lines.append(f"- 커밋된 primarySummary.mean = {committed_ndcg:.6f}")
    lines.append(
        f"- 재현(반복별 평균들의 평균, included 18케이스) = {overall_mean_included:.6f}, "
        f"diff={e1b['diffFromCommitted']:.2e}, 재현 여부: {e1b['reproducedWithinTolerance1e6']}"
    )
    lines.append(
        f"- 재현(케이스 평균 먼저→전체 평균) = {case_level_then_overall:.6f}, "
        f"diff={e1b['caseLevelThenOverall_diffFromCommitted']:.2e}, "
        f"재현 여부: {e1b['caseLevelThenOverall_reproducedWithinTolerance1e6']}"
    )
    lines.append(f"- 반복별(0~4) 전체 평균: {per_repeat_means_included}")
    case_sds = [v["sd"] for v in e1b_per_case.values() if not v["rankingExcluded"]]
    lines.append(f"- 케이스별 nDCG@10 표준편차(포함 케이스만): mean={sum(case_sds) / len(case_sds):.4f}, max={max(case_sds):.4f}\n")

    lines.append("## E1-c 후보 수(K) 대 토큰/비용 회귀\n")
    lines.append(f"- rerank 콜 수: {e1c['nRerankCalls']}, rerank 콜 없는 case-repeat 수: {e1c['nCaseRepeatWithoutRerankCall']}")
    if reg_input:
        lines.append(
            f"- inputTokens ~ K: intercept={reg_input['intercept']:.3f}, "
            f"slope={reg_input['slope']:.4f} tok/candidate, R²={reg_input['r2']:.4f}, "
            f"maxAbsResid={reg_input['max_abs_resid']:.2f}"
        )
    if reg_output:
        lines.append(
            f"- outputTokens ~ K: intercept={reg_output['intercept']:.3f}, "
            f"slope={reg_output['slope']:.4f} tok/candidate, R²={reg_output['r2']:.4f}, "
            f"maxAbsResid={reg_output['max_abs_resid']:.2f}"
        )
    if reg_cost:
        lines.append(
            f"- costUsd ~ K: intercept={reg_cost['intercept']:.6f}, "
            f"slope={reg_cost['slope']:.8f} USD/candidate, R²={reg_cost['r2']:.4f}, "
            f"maxAbsResid={reg_cost['max_abs_resid']:.6f}\n"
        )

    lines.append("## E1-d 비용 요약\n")
    lines.append(
        f"- rerank: n={e1d['rerank']['n']}, total=${e1d['rerank']['total']:.8f}, "
        f"mean=${e1d['rerank']['mean']:.8f}, median=${e1d['rerank']['median']:.8f}, "
        f"p95=${e1d['rerank']['p95']:.8f}"
    )
    lines.append(
        f"- decompose: n={e1d['decompose']['n']}, total=${e1d['decompose']['total']:.8f}, "
        f"mean=${e1d['decompose']['mean']:.8f}, median=${e1d['decompose']['median']:.8f}, "
        f"p95=${e1d['decompose']['p95']:.8f}"
    )
    lines.append(
        f"- rerank cacheTokens: zero={e1d['rerankCacheTokens']['zeroCount']}, "
        f"nonzero={e1d['rerankCacheTokens']['nonZeroCount']} (가설: rerank 는 캐시 거의 안 먹음)"
    )
    lines.append(
        f"- decompose cacheTokens: zero={e1d['decomposeCacheTokens']['zeroCount']}, "
        f"nonzero={e1d['decomposeCacheTokens']['nonZeroCount']}\n"
    )

    lines.append("## E1-e 단가 재현\n")
    lines.append(
        f"- 검증 대상 rerank 콜: {e1e['nChecked']}건, 불일치: {e1e['nMismatches']}건, "
        f"전량 일치: {e1e['allMatchWithinTolerance']}\n"
    )

    lines.append("## 직교 검증(팩킷에 명시된 참고값과 대비)\n")
    lines.append("| 지표 | 계산값 | 참고값 | 일치 |")
    lines.append("|---|---|---|---|")
    for k, v in reference_check.items():
        c, r = v["computed"], v["reference"]
        match = "✓" if (c is not None and abs(c - r) < max(1e-6, abs(r) * 1e-4)) else "✗"
        lines.append(f"| {k} | {c} | {r} | {match} |")

    (out_dir / "e1_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("E1 done. Wrote e1_results.json and e1_report.md to", out_dir)
    print(
        "committed_ndcg",
        committed_ndcg,
        "reproduced(repeat-mean-of-means)",
        overall_mean_included,
        "reproduced(case-mean-then-overall)",
        case_level_then_overall,
    )


if __name__ == "__main__":
    main()
