#!/usr/bin/env python3
"""E4 — E2 해석 결함 교정 재측정. 실 LLM 호출 없음, 비용 0.

E2 가 찾은 "오라클 상한 0.738210" 은 축퇴(degenerate) 해였다:
  - ScoringBuyerAdapter 는 recency_by_product=None 으로 구성돼 recency 성분이 항상 0이다
    (무력한 축).
  - Settings 검증자는 "5개 양의 신호 가중치 중 하나 이상 > 0" 만 요구하므로, 탐색기가
    무력한 recency 축에만 가중치를 몰아줘 검증을 통과하면서 실질 신호를 전부 0으로 만들었다.
  - 실질 신호가 전부 0이면 scorer.py 의 동점 처리(key=(-score, product_id))로 productId
    오름차순이 되고, 이는 커밋된 passthrough baseline(dev-v1/comparison.md, 0.738210)과
    같은 no-op 순위다.

E4 는:
  1) recency 를 탐색 공간에서 뺀다(0 고정) + "4개 실질 축 중 최소 하나 0.05 이상" 제약 +
     "no-op 과 최소 1케이스 다른 순위" 제약을 걸고 오라클 상한을 다시 잰다(E4-1).
     — recency 를 뺀 이유: ScoringBuyerAdapter 가 recency_by_product=None 을 주입해
       주입되지 않는 축이라 항상 0이다(app/core/config.py 기본 recency=0.05 도 E2 Step4
       델타 0.000000 으로 실질 무영향임이 이미 관측됐다).
  2) no-op 을 1급 baseline 행으로 명시하고 passthrough(0.738210)와 자릿수 일치를 확인한다(E4-2).
  3) teacher-fit 탐색을 같은 교정 공간에서 재측정한다(E4-3).
  4) #146 사전 등록 규약(paired bootstrap 95% CI, resamples=2000, confidence=0.95)으로
     4개 비교쌍의 케이스 단위 델타 CI를 낸다(E4-4, evals.ablation.stats.paired_comparisons 재사용).
  5) evaluate() 의 랭킹 유효 케이스 수를 기록한다(E4-5).

판정에 영향을 주는 고정 축: seed=20260803(E4-1) / 20260804(E4-3, +1) — E2와 동일해 직접 비교
가능. 탐색 예산(N=2000 랜덤 + top10×2패스 좌표하강, grid step 0.05)도 E2와 동일하게 고정한다.
"""

import argparse
import json
import random
from pathlib import Path

from evals.ablation.stats import paired_comparisons
from evals.goldenset.loader import load_cases
from evals.metrics.metrics import ndcg_at_k
from evals.metrics.runner import evaluate, load_evaluation_fixtures
from evals.metrics.settings import EvaluationSettings
from evals.scoring.adapter import ScoringBuyerAdapter, _preferences, _recent_ids
from evals.scoring.embeddings import load_embeddings
from evals.scoring.scorer import ScoringWeights, score_products


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("pyproject.toml을 찾지 못해 저장소 루트를 확정할 수 없다")


REPO_ROOT = _repo_root()
PIPELINE_RESULTS = REPO_ROOT / "evals/ablation/baselines/20260803-dev-full-n5/pipeline/results.json"
DEV_V1_COMPARISON = REPO_ROOT / "evals/scoring/baselines/dev-v1/comparison.md"
SEARCH_RESPONSES_PATH = REPO_ROOT / "evals/goldenset/fixtures/search_responses.json"
_INTERNAL_TOKEN = "eval-internal-token"

ALL_WEIGHT_NAMES = [
    "semantic",
    "profile_match",
    "popularity",
    "recency",
    "diversity_bonus",
    "recent_purchase_penalty",
]
# E4 자유변수 — recency 는 항상 0으로 고정한다(무력한 축, 모듈 docstring 참조).
FREE_NAMES = ["semantic", "profile_match", "popularity", "diversity_bonus", "recent_purchase_penalty"]
# 축퇴 배제 제약 대상 — "실질 신호" 4축(recent_purchase_penalty 는 패널티라 Settings 검증자도
# 이 넷만 "양의 신호"로 센다).
QUARTET_NAMES = ["semantic", "profile_match", "popularity", "diversity_bonus"]
QUARTET_MIN = 0.05

DEFAULT_WEIGHTS_FULL = {
    "semantic": 0.55,
    "profile_match": 0.15,
    "popularity": 0.15,
    "recency": 0.05,
    "diversity_bonus": 0.10,
    "recent_purchase_penalty": 0.20,
}
NO_OP_VEC_FULL = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # recency=1, 나머지 0 — 검증자 통과 + 무신호

SEED = 20260803
RANDOM_SEARCH_N = 2000
COORD_TOP_K = 10
COORD_PASSES = 2
COORD_GRID_STEP = 0.05
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_CONFIDENCE = 0.95


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="e4_results.json/e4_report.md를 쓸 출력 디렉터리(리포 밖 권장)"
    )
    return parser.parse_args()


def make_settings_full(vec6):
    kwargs = dict(zip(ALL_WEIGHT_NAMES, vec6, strict=True))
    return EvaluationSettings(
        auth_mode="dev",
        internal_api_token=_INTERNAL_TOKEN,
        search_backend="spring",
        scoring_weight_semantic=kwargs["semantic"],
        scoring_weight_profile_match=kwargs["profile_match"],
        scoring_weight_popularity=kwargs["popularity"],
        scoring_weight_recency=kwargs["recency"],
        scoring_weight_diversity_bonus=kwargs["diversity_bonus"],
        scoring_weight_recent_purchase_penalty=kwargs["recent_purchase_penalty"],
    )


def free_vec_to_full(vec5):
    """FREE_NAMES 순서의 5차원 벡터를 recency=0 고정 6차원(ALL_WEIGHT_NAMES 순서)으로 편다."""
    kw = dict(zip(FREE_NAMES, vec5, strict=True))
    kw["recency"] = 0.0
    return [kw[name] for name in ALL_WEIGHT_NAMES]


def quartet_satisfied(vec5):
    kw = dict(zip(FREE_NAMES, vec5, strict=True))
    return max(kw[name] for name in QUARTET_NAMES) >= QUARTET_MIN


def run_evaluate_full(vec6, cases, fixtures):
    settings = make_settings_full(vec6)
    adapter = ScoringBuyerAdapter(settings=settings)
    return evaluate(adapter=adapter, cases=cases, fixtures=fixtures)


def report_case_rankings(report):
    return {row["caseId"]: row.get("rankedProductIds") for row in report["cases"]}


def report_case_ndcg10(report):
    return {
        row["caseId"]: row["metrics"]["ndcgAtK"]["10"]
        for row in report["cases"]
        if not row["rankingExcluded"] and row["metrics"]["ndcgAtK"]["10"] is not None
    }


def random_search_and_coord_descent(objective, n_random, seed, dim):
    """objective: weight_vec(list[dim]) -> float. 반환 (best_score, best_vec, n_evals)."""
    rng = random.Random(seed)
    n_evals = 0
    scored = []
    for _ in range(n_random):
        vec = [rng.random() for _ in range(dim)]
        score = objective(vec)
        n_evals += 1
        scored.append((score, vec))
    scored.sort(key=lambda t: -t[0])
    top = scored[:COORD_TOP_K]

    best_score, best_vec = scored[0]
    grid = [round(i * COORD_GRID_STEP, 10) for i in range(int(1 / COORD_GRID_STEP) + 1)]

    for start_score, start_vec in top:
        cur_vec = list(start_vec)
        cur_score = start_score
        for _pass in range(COORD_PASSES):
            for axis in range(dim):
                best_axis_score = cur_score
                best_axis_val = cur_vec[axis]
                for candidate in grid:
                    trial = list(cur_vec)
                    trial[axis] = candidate
                    if trial == cur_vec:
                        continue
                    score = objective(trial)
                    n_evals += 1
                    if score > best_axis_score:
                        best_axis_score = score
                        best_axis_val = candidate
                cur_vec[axis] = best_axis_val
                cur_score = best_axis_score
        if cur_score > best_score:
            best_score = cur_score
            best_vec = cur_vec

    return best_score, best_vec, n_evals


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = list(load_cases("dev"))
    fixtures = load_evaluation_fixtures()

    # ---------------- E4-5: 랭킹 유효 케이스 수 ----------------
    default_report = run_evaluate_full(
        [DEFAULT_WEIGHTS_FULL[n] for n in ALL_WEIGHT_NAMES], cases, fixtures
    )
    default_ndcg = default_report["overall"]["ndcgAtK"]["10"]
    e4_5 = {
        "rankingCaseCount": default_report["overall"]["rankingCaseCount"],
        "rankingExcludedCount": default_report["overall"]["rankingExcludedCount"],
        "rankingExcludedCaseIds": default_report["overall"]["rankingExcludedCaseIds"],
        "caseCount": default_report["overall"]["caseCount"],
        "note": (
            "evaluate() 의 overall.ndcgAtK 는 rankingExcluded(비판별·relevance 없음) 케이스를 "
            "분모에서 이미 제외한다(evals/metrics/runner.py _aggregate)."
        ),
    }

    # ---------------- E4-2: no-op 1급 baseline ----------------
    no_op_report = run_evaluate_full(NO_OP_VEC_FULL, cases, fixtures)
    no_op_ndcg = no_op_report["overall"]["ndcgAtK"]["10"]
    no_op_rankings = report_case_rankings(no_op_report)

    dev_v1_comparison_text = (
        DEV_V1_COMPARISON.read_text(encoding="utf-8") if DEV_V1_COMPARISON.exists() else None
    )
    committed_passthrough_ndcg = 0.738210

    search_responses = json.loads(SEARCH_RESPONSES_PATH.read_text(encoding="utf-8"))
    multi_item_fixtures = {
        fid: row["productIds"]
        for fid, row in search_responses.items()
        if len(row.get("productIds", [])) >= 2
    }
    ascending_count = sum(1 for ids in multi_item_fixtures.values() if ids == sorted(ids))

    e4_2 = {
        "noOpVector": dict(zip(ALL_WEIGHT_NAMES, NO_OP_VEC_FULL, strict=True)),
        "noOpVectorRationale": (
            "recency_by_product=None 이라 recency 성분은 항상 0(무력한 축). recency=1·나머지 0 은 "
            "Settings 검증자(5개 양의 신호 가중치 중 하나 이상 양수)를 통과시키면서 실질 신호를 "
            "전혀 주입하지 않는 가장 명시적인 no-op 구성이다."
        ),
        "noOpNdcgAt10": no_op_ndcg,
        "committedPassthroughNdcgAt10_devV1": committed_passthrough_ndcg,
        "diffFromCommittedPassthrough": (
            (no_op_ndcg - committed_passthrough_ndcg) if no_op_ndcg is not None else None
        ),
        "matchesCommittedPassthroughToSixDecimals": (
            round(no_op_ndcg, 6) == round(committed_passthrough_ndcg, 6) if no_op_ndcg is not None else False
        ),
        "devV1ComparisonMdRaw": dev_v1_comparison_text,
        "searchFixtureProductIdOrderCheck": {
            "multiItemFixtureCount": len(multi_item_fixtures),
            "ascendingOrderCount": ascending_count,
            "allAscending": ascending_count == len(multi_item_fixtures),
        },
    }

    # ---------------- E4-1: 무력 축 제거 + 축퇴 배제 오라클 상한 ----------------
    invalid_counts = {"quartetBelowMin": 0, "sameAsNoOpRanking": 0, "settingsValidationError": 0}

    def e4_1_objective(vec5):
        if not quartet_satisfied(vec5):
            invalid_counts["quartetBelowMin"] += 1
            return 0.0
        vec6 = free_vec_to_full(vec5)
        try:
            report = run_evaluate_full(vec6, cases, fixtures)
        except Exception:
            invalid_counts["settingsValidationError"] += 1
            return 0.0
        rankings = report_case_rankings(report)
        differs = any(rankings.get(cid) != no_op_rankings.get(cid) for cid in rankings)
        if not differs:
            invalid_counts["sameAsNoOpRanking"] += 1
            return 0.0
        val = report["overall"]["ndcgAtK"]["10"]
        return val if val is not None else 0.0

    e4_1_best_score, e4_1_best_vec5, e4_1_n_evals = random_search_and_coord_descent(
        e4_1_objective, RANDOM_SEARCH_N, SEED, dim=len(FREE_NAMES)
    )
    e4_1_best_vec6 = free_vec_to_full(e4_1_best_vec5)
    e4_1_best_report = run_evaluate_full(e4_1_best_vec6, cases, fixtures)

    e4_1 = {
        "freeAxes": FREE_NAMES,
        "recencyFixedAt": 0.0,
        "recencyExclusionReason": (
            "ScoringBuyerAdapter 가 recency_by_product=None 으로 구성돼 recency 성분이 항상 0이다"
            "(app/core/config.py 기본 recency=0.05 도 실질 무영향 — E2 Step4 델타 0.000000 로 "
            "이미 관측됨)."
        ),
        "degenerateExclusionConstraint": (
            f"{QUARTET_NAMES} 중 최소 하나가 {QUARTET_MIN} 이상이어야 하고, 결과 순위가 no-op "
            "순위와 최소 1케이스 달라야 한다(둘 다 위반 시 무효 시도로 0점)."
        ),
        "method": (
            f"random search N={RANDOM_SEARCH_N} (seed={SEED}) + coordinate descent "
            f"top{COORD_TOP_K} x {COORD_PASSES} passes, grid step {COORD_GRID_STEP}"
        ),
        "nEvals": e4_1_n_evals,
        "invalidTrialCounts": invalid_counts,
        "bestNdcgAt10": e4_1_best_score,
        "bestWeights": dict(zip(ALL_WEIGHT_NAMES, e4_1_best_vec6, strict=True)),
        "deltaVsDefault": e4_1_best_score - default_ndcg,
        "deltaVsNoOp": e4_1_best_score - no_op_ndcg,
        "deltaVsTeacher": e4_1_best_score - 0.782943,
        "note": (
            "in-sample(dev 31건에 직접 맞춘) 낙관적 상한이며 holdout 은 열지 않았다. E2 Step2 값"
            "(0.738210)은 축퇴 해였다 — 이 값이 교정된 상한이다."
        ),
    }

    # ---------------- E4-3: teacher-fit 재측정(같은 교정 공간) ----------------
    pipeline_data = json.loads(PIPELINE_RESULTS.read_text(encoding="utf-8"))
    rep1_rows = {r["caseId"]: r for r in pipeline_data["caseResults"] if r["repeat"] == 1}
    embeddings = load_embeddings()
    catalog = fixtures.catalog

    teacher_fit_cases = []
    excluded_no_rerank = []
    for case in cases:
        row = rep1_rows.get(case.case_id)
        if row is None or not any(c["callSite"] == "rerank" for c in row["providerCalls"]):
            excluded_no_rerank.append(case.case_id)
            continue
        eligible_ids = row["metrics"]["eligibleProductIds"]
        teacher_ranked = row["rankedProductIds"]
        products = [catalog[str(pid)] for pid in eligible_ids if str(pid) in catalog]
        if not products:
            excluded_no_rerank.append(case.case_id)
            continue
        K = len(eligible_ids)
        teacher_grades = {}
        for rank_idx, pid in enumerate(teacher_ranked, start=1):
            teacher_grades[pid] = K - rank_idx + 1
        for pid in eligible_ids:
            teacher_grades.setdefault(pid, 0)
        history = (
            fixtures.purchase_history.get(case.identity.persona_id or "")
            if case.identity.kind != "guest"
            else None
        )
        teacher_fit_cases.append(
            {
                "caseId": case.case_id,
                "products": products,
                "queryEmbedding": embeddings.queries.get(case.case_id),
                "profilePreferences": _preferences(history, catalog),
                "recentProductIds": _recent_ids(
                    history,
                    EvaluationSettings().scoring_reference_date,
                    EvaluationSettings().scoring_recent_purchase_window_days,
                ),
                "teacherGrades": teacher_grades,
                "goldenGrades": case.relevance_grades,
            }
        )

    def score_products_for(vec6, case_entry):
        weights = ScoringWeights(*vec6)
        return score_products(
            case_entry["products"],
            query_embedding=case_entry["queryEmbedding"],
            product_embeddings=embeddings.documents,
            profile_preferences=case_entry["profilePreferences"],
            recency_by_product=None,
            recent_product_ids=case_entry["recentProductIds"],
            weights=weights,
        )

    teacher_cand_no_op_rankings = {
        c["caseId"]: score_products_for([0.0] * 6, c).ranked_product_ids for c in teacher_fit_cases
    }

    e4_3_invalid_counts = {"quartetBelowMin": 0, "sameAsNoOpRanking": 0}

    def e4_3_objective(vec5):
        if not quartet_satisfied(vec5):
            e4_3_invalid_counts["quartetBelowMin"] += 1
            return 0.0
        vec6 = free_vec_to_full(vec5)
        rankings = {}
        vals = []
        for c in teacher_fit_cases:
            result = score_products_for(vec6, c)
            rankings[c["caseId"]] = result.ranked_product_ids
            v = ndcg_at_k(result.ranked_product_ids, c["teacherGrades"], 10)
            if v is not None:
                vals.append(v)
        differs = any(rankings[cid] != teacher_cand_no_op_rankings.get(cid) for cid in rankings)
        if not differs:
            e4_3_invalid_counts["sameAsNoOpRanking"] += 1
            return 0.0
        return sum(vals) / len(vals) if vals else 0.0

    e4_3_best_score, e4_3_best_vec5, e4_3_n_evals = random_search_and_coord_descent(
        e4_3_objective, RANDOM_SEARCH_N, SEED + 1, dim=len(FREE_NAMES)
    )
    e4_3_best_vec6 = free_vec_to_full(e4_3_best_vec5)

    golden_on_teacher_cands_vals = []
    for c in teacher_fit_cases:
        result = score_products_for(e4_3_best_vec6, c)
        v = ndcg_at_k(result.ranked_product_ids, c["goldenGrades"], 10)
        if v is not None:
            golden_on_teacher_cands_vals.append(v)
    e4_3_golden_on_teacher_cands = (
        sum(golden_on_teacher_cands_vals) / len(golden_on_teacher_cands_vals)
        if golden_on_teacher_cands_vals
        else None
    )

    e4_3_settings_invalid_reason = None
    try:
        e4_3_standard_report = run_evaluate_full(e4_3_best_vec6, cases, fixtures)
        e4_3_golden_standard_ndcg = e4_3_standard_report["overall"]["ndcgAtK"]["10"]
    except Exception as exc:  # noqa: BLE001
        e4_3_standard_report = None
        e4_3_golden_standard_ndcg = None
        e4_3_settings_invalid_reason = f"{type(exc).__name__}: {exc}"

    e4_3 = {
        "freeAxes": FREE_NAMES,
        "recencyFixedAt": 0.0,
        "degenerateExclusionConstraint": e4_1["degenerateExclusionConstraint"],
        "teacherRankSource": str(PIPELINE_RESULTS),
        "teacherRepeatUsed": 1,
        "nCasesUsed": len(teacher_fit_cases),
        "nCasesExcluded_noTeacherRerankCall": len(excluded_no_rerank),
        "excludedCaseIds": excluded_no_rerank,
        "method": (
            f"random search N={RANDOM_SEARCH_N} (seed={SEED + 1}) + coordinate descent "
            f"top{COORD_TOP_K} x {COORD_PASSES} passes, grid step {COORD_GRID_STEP}"
        ),
        "nEvals": e4_3_n_evals,
        "invalidTrialCounts": e4_3_invalid_counts,
        "bestTeacherFitObjective_ndcgAt10_vsTeacherGrades": e4_3_best_score,
        "bestWeights": dict(zip(ALL_WEIGHT_NAMES, e4_3_best_vec6, strict=True)),
        "standardEvaluatePath_goldenNdcgAt10": e4_3_golden_standard_ndcg,
        "standardEvaluatePath_invalidReason": e4_3_settings_invalid_reason,
        "isolatedTable_teacherCandidateSet": {
            "note": (
                "후보집합이 teacher eligibleProductIds 로 다른 행들(default/no-op/E4-1/teacher)과 "
                "다르다 — 직접 비교 금지, 별도 표로 격리."
            ),
            "goldenNdcgAt10_onTeacherCandidateSet": e4_3_golden_on_teacher_cands,
            "nCasesUsed": len(golden_on_teacher_cands_vals),
        },
        "vsNoOp_standardPath": (
            (e4_3_golden_standard_ndcg - no_op_ndcg) if e4_3_golden_standard_ndcg is not None else None
        ),
    }

    # ---------------- E4-4: paired bootstrap 95% CI (#146 규약) ----------------
    all_case_ids = [c.case_id for c in cases]

    def arm_from_report(report):
        return {"casePrimaryMetrics": {cid: [v] for cid, v in report_case_ndcg10(report).items()}}

    arm_default = arm_from_report(default_report)
    arm_no_op = arm_from_report(no_op_report)
    arm_e4_1 = arm_from_report(e4_1_best_report)
    arm_e4_3 = (
        arm_from_report(e4_3_standard_report)
        if e4_3_standard_report is not None
        else {"casePrimaryMetrics": {}}
    )

    ranking_excluded_ids = set(default_report["overall"]["rankingExcludedCaseIds"])
    teacher_case_repeats = {}
    for row in pipeline_data["caseResults"]:
        cid = row["caseId"]
        if cid in ranking_excluded_ids:
            continue
        v = row["metrics"]["metrics"]["ndcgAtK"]["10"]
        if v is not None:
            teacher_case_repeats.setdefault(cid, []).append(v)
    arm_teacher = {"casePrimaryMetrics": teacher_case_repeats}

    arms = {
        "noOp": arm_no_op,
        "default": arm_default,
        "e4_1_best": arm_e4_1,
        "e4_3_teacherFit": arm_e4_3,
        "teacher": arm_teacher,
    }
    pairs = [
        ("noOp", "default"),
        ("e4_1_best", "default"),
        ("e4_3_teacherFit", "default"),
        ("teacher", "noOp"),
    ]
    comparisons = paired_comparisons(
        arms,
        all_case_ids=all_case_ids,
        pairs=pairs,
        resamples=BOOTSTRAP_RESAMPLES,
        confidence=BOOTSTRAP_CONFIDENCE,
        seed=SEED,
    )
    e4_4 = {
        "protocol": (
            "#146 사전 등록 — case-level primary delta paired bootstrap 95% CI. resamples=2000, "
            "confidence=0.95(evals/ablation/ablation_config.json 과 동일 값), "
            "seed=20260803(+offset per pair). "
            "구현 재사용: evals/ablation/stats.py::paired_comparisons"
            "(내부적으로 evals/model_eval/stats.py::bootstrap_mean_ci 사용)."
        ),
        "resamples": BOOTSTRAP_RESAMPLES,
        "confidence": BOOTSTRAP_CONFIDENCE,
        "seed": SEED,
        "note_teacherVsNoOp": (
            "teacher(pipeline arm)와 no-op(scoring arm)은 후보 집합·실행 경로가 완전히 같지 않다"
            "(teacher는 pipeline arm 의 LLM decompose 로 만든 검색 후보, no-op은 "
            "ScoringBuyerAdapter 의 expected_filters 기반 후보) — 한계로 기록한다."
        ),
        "comparisons": comparisons,
    }

    results = {
        "e4_1_correctedOracleUpperBound": e4_1,
        "e4_2_noOpBaseline": e4_2,
        "e4_3_teacherFitRemeasured": e4_3,
        "e4_4_pairedBootstrapCi": e4_4,
        "e4_5_rankingValidCaseCount": e4_5,
    }
    (out_dir / "e4_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = []
    lines.append("# E4 — E2 해석 결함 교정 재측정\n")
    lines.append(
        "E2 Step2 의 '오라클 상한 0.738210' 은 축퇴 해(recency 만 값을 갖고 나머지 실질 신호 0 → "
        "productId 오름차순 no-op 순위)였다. 커밋된 passthrough baseline(dev-v1) 과 자릿수까지 "
        "일치한다.\n"
    )

    lines.append("## E4-5 — 랭킹 유효 케이스 수\n")
    lines.append(
        f"- caseCount={e4_5['caseCount']}, rankingCaseCount={e4_5['rankingCaseCount']}, "
        f"rankingExcludedCount={e4_5['rankingExcludedCount']}\n"
    )

    lines.append("## E4-2 — no-op 1급 baseline\n")
    lines.append(f"- no-op 가중치: {e4_2['noOpVector']}")
    lines.append(
        f"- no-op nDCG@10 = {no_op_ndcg:.6f}, 커밋된 passthrough(dev-v1) = "
        f"{committed_passthrough_ndcg:.6f}, 6자리 일치: {e4_2['matchesCommittedPassthroughToSixDecimals']}"
    )
    fc = e4_2["searchFixtureProductIdOrderCheck"]
    lines.append(
        f"- dev search fixture 항목 2개 이상 {fc['multiItemFixtureCount']}건 중 productId 오름차순 "
        f"{fc['ascendingOrderCount']}건, 전량 오름차순: {fc['allAscending']}\n"
    )

    lines.append("## E4-1 — 무력 축 제거 + 축퇴 배제 오라클 상한\n")
    lines.append(f"- 자유축: {FREE_NAMES} (recency=0 고정), 제약: {e4_1['degenerateExclusionConstraint']}")
    lines.append(f"- {e4_1['method']}, 실제 평가 횟수: {e4_1_n_evals}, 무효 시도: {invalid_counts}")
    lines.append(
        f"- 교정된 오라클 상한 nDCG@10 = **{e4_1_best_score:.6f}** (기본 대비 "
        f"{e4_1['deltaVsDefault']:+.6f}, no-op 대비 {e4_1['deltaVsNoOp']:+.6f}, teacher 대비 "
        f"{e4_1['deltaVsTeacher']:+.6f})"
    )
    lines.append(f"- 가중치: {e4_1['bestWeights']}\n")

    lines.append("## E4-3 — teacher-fit 재측정(같은 교정 공간)\n")
    lines.append(
        f"- {e4_3['nCasesUsed']}케이스 사용, {e4_3['nCasesExcluded_noTeacherRerankCall']}케이스 제외: "
        f"{excluded_no_rerank}"
    )
    lines.append(f"- {e4_3['method']}, 실제 평가 횟수: {e4_3_n_evals}, 무효 시도: {e4_3_invalid_counts}")
    lines.append(f"- teacher grades 기준 목적함수 최댓값 = {e4_3_best_score:.6f}")
    standard_str = (
        f"{e4_3_golden_standard_ndcg:.6f}"
        if e4_3_golden_standard_ndcg is not None
        else f"측정 불가 — {e4_3_settings_invalid_reason}"
    )
    lines.append(
        f"- 표준 evaluate() 경로(학생 자체 후보집합) 골든 nDCG@10 = {standard_str}, no-op 대비 "
        f"{e4_3['vsNoOp_standardPath']}"
    )
    lines.append(
        f"- [격리 표] teacher 후보집합 위 골든 nDCG@10 = {e4_3_golden_on_teacher_cands} "
        f"(n={e4_3['isolatedTable_teacherCandidateSet']['nCasesUsed']}) — 후보집합이 달라 위 표준 경로 "
        "값과 직접 비교 금지\n"
    )

    lines.append("### 나란히 비교 (골든 라벨, 표준 evaluate() 경로 기준만)\n")
    lines.append("| 항목 | nDCG@10 |")
    lines.append("|---|---:|")
    lines.append(f"| no-op(축퇴/무신호) | {no_op_ndcg:.6f} |")
    lines.append(f"| 현재 기본 가중치(student) | {default_ndcg:.6f} |")
    lines.append(f"| E4-1 교정된 오라클 상한(in-sample) | {e4_1_best_score:.6f} |")
    lines.append(f"| E4-3 teacher-fit(표준 경로 골든) | {standard_str} |")
    lines.append("| teacher(pipeline arm, 커밋 참고값) | 0.782943 |\n")

    lines.append("## E4-4 — paired bootstrap 95% CI (#146 규약, resamples=2000, confidence=0.95)\n")
    lines.append("| 비교 | paired N | 평균 델타 | 95% CI | 판정 |")
    lines.append("|---|---:|---:|---|---|")
    for key, comp in comparisons.items():
        ci = comp["bootstrapCi95"]
        ci_str = f"[{ci['low']:.6f}, {ci['high']:.6f}]" if ci["low"] is not None else "N/A"
        mean_delta = comp["meanDelta"]
        mean_str = f"{mean_delta:+.6f}" if mean_delta is not None else "N/A"
        lines.append(f"| {key} | {comp['pairedCount']} | {mean_str} | {ci_str} | {comp['verdict']} |")
    lines.append(f"\n- teacher-noOp 한계: {e4_4['note_teacherVsNoOp']}\n")

    (out_dir / "e4_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("E4 done.")
    print("no_op_ndcg", no_op_ndcg, "matches passthrough:", e4_2["matchesCommittedPassthroughToSixDecimals"])
    print("e4_1 corrected oracle upper bound:", e4_1_best_score, "n_evals", e4_1_n_evals, "invalid", invalid_counts)
    print("e4_3 teacher-fit standard path golden ndcg:", e4_3_golden_standard_ndcg)
    for key, comp in comparisons.items():
        print(key, comp["meanDelta"], comp["bootstrapCi95"], comp["verdict"])


if __name__ == "__main__":
    main()
