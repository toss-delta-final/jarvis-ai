#!/usr/bin/env python3
"""E2 — student(6성분 선형결합) 용량 상한 프로브. 실 LLM 호출 없음, 비용 0.

**주의(2026-08-05, E4 참조)**: 이 스크립트의 Step2 "오라클 상한"은 축퇴(recency-only,
사실상 no-op) 해로 수렴할 수 있다. `ScoringBuyerAdapter`는 `recency_by_product=None`으로
구성돼 `recency` 성분이 항상 0이고(무력한 축), `EvaluationSettings` 검증자는 "5개 양의 신호
가중치 중 하나 이상 양수"만 요구해 무력한 recency 축에 값을 몰아주고 나머지를 전부 0으로
만드는 시도를 막지 못한다. 교정된 탐색(recency 제외 + 축퇴 배제 제약)은 `e4_analyze.py`를 본다.
이 스크립트는 그 결함이 재현되는 경로를 그대로 보존한 원본이며, 교정 로직을 덮어쓰지 않는다.

전략:
  1) 기본 가중치로 evaluate()+ScoringBuyerAdapter(전체 그래프 경로)를 돌려
     커밋된 baseline scoring nDCG@10 0.616852 재현을 확인한다.
  2) 6개 가중치를 자유변수로 둔 랜덤서치+좌표하강으로 골든 라벨 기준 nDCG@10 in-sample 상한을 잰다
     (전체 그래프 경로 그대로 — evaluate() 호출을 목적함수로 사용).
  3) 같은 탐색을, 목적함수만 "teacher 순위에서 변환한 graded relevance"로 바꿔서 수행한다.
     후보 집합은 teacher pipeline(repeat=1)의 eligibleProductIds로 맞추고
     (score_products를 직접 호출 — 전체 그래프를 타지 않음, hard_filter 재적용 없음),
     teacher rerank 콜이 없는 케이스는 제외하고 건수를 센다.
     찾은 가중치로 다시 evaluate()를 돌려 골든 라벨 기준 nDCG@10을 잰다.
  4) 기본 가중치에서 성분 하나씩 0으로 만든 6행 표(evaluate() 기준).
"""

import argparse
import json
from pathlib import Path

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
_INTERNAL_TOKEN = "eval-internal-token"

WEIGHT_NAMES = [
    "semantic",
    "profile_match",
    "popularity",
    "recency",
    "diversity_bonus",
    "recent_purchase_penalty",
]

# 판정에 영향을 주는 고정 축: 기본 가중치는 app/core/config.py 기본값과 동일하다(2026-08-05 기준).
DEFAULT_WEIGHTS = {
    "semantic": 0.55,
    "profile_match": 0.15,
    "popularity": 0.15,
    "recency": 0.05,
    "diversity_bonus": 0.10,
    "recent_purchase_penalty": 0.20,
}

# 판정에 영향을 주는 고정 축: seed 20260803(골든 오라클 탐색) / 20260804(teacher-fit 탐색, +1).
# 탐색 예산(N=2000 랜덤 + top10×2패스 좌표하강, grid step 0.05)도 이 리포의 재현 대상이라 고정한다.
SEED = 20260803
RANDOM_SEARCH_N = 2000
COORD_TOP_K = 10
COORD_PASSES = 2
COORD_GRID_STEP = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="e2_results.json/e2_report.md를 쓸 출력 디렉터리(리포 밖 권장)"
    )
    return parser.parse_args()


def clamp01(x):
    return min(1.0, max(0.0, x))


INVALID_WEIGHT_TRIAL_COUNT = {"n": 0}


def make_settings(weight_vec):
    """가중치 벡터로 EvaluationSettings 를 만든다.

    Settings 는 "5개 양의 신호 가중치(semantic/profile/popularity/recency/diversity) 중
    하나 이상은 양수" 를 강제한다(app/core/config.py) — 전부 0인 코너는 프로덕션에서도
    허용되지 않는 설정이라 탐색 목적함수가 이 코너를 만나면 무효 시도로 세고 최저점(0.0)을
    돌려보내 탐색이 그 방향을 피하게 한다(가중치를 몰래 보정해서 통과시키지 않는다).
    이 검증자는 recency 만으로도 통과하므로(무력한 축), 이 스크립트의 오라클 상한은
    축퇴 해로 수렴할 수 있다 — 교정판은 e4_analyze.py 를 본다.
    """
    kwargs = {name: weight_vec[i] for i, name in enumerate(WEIGHT_NAMES)}
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


def random_search_and_coord_descent(objective, n_random, seed, n_used_holder):
    """objective: weight_vec(list[6]) -> float. 반환 (best_score, best_vec, n_evals)."""
    import random

    rng = random.Random(seed)
    n_evals = 0
    scored = []
    for _ in range(n_random):
        vec = [rng.random() for _ in range(6)]
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
            for axis in range(6):
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

    n_used_holder["n"] = n_evals
    return best_score, best_vec, n_evals


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = list(load_cases("dev"))
    fixtures = load_evaluation_fixtures()

    # ---------------- Step 1: 경로 검증 ----------------
    default_settings = make_settings([DEFAULT_WEIGHTS[n] for n in WEIGHT_NAMES])
    default_adapter = ScoringBuyerAdapter(settings=default_settings)
    default_report = evaluate(adapter=default_adapter, cases=cases, fixtures=fixtures)
    default_ndcg = default_report["overall"]["ndcgAtK"]["10"]
    committed_scoring_ndcg = 0.616852
    reproduced_step1 = abs(default_ndcg - committed_scoring_ndcg) < 1e-5

    step1 = {
        "defaultWeights": DEFAULT_WEIGHTS,
        "computedNdcgAt10": default_ndcg,
        "committedBaselineNdcgAt10": committed_scoring_ndcg,
        "diff": default_ndcg - committed_scoring_ndcg,
        "reproduced": reproduced_step1,
        "rankingCaseCount": default_report["overall"]["rankingCaseCount"],
    }

    # ---------------- Step 2: 골든 라벨 기준 오라클 상한 ----------------
    def golden_objective(vec):
        try:
            settings = make_settings(vec)
        except Exception:
            INVALID_WEIGHT_TRIAL_COUNT["n"] += 1
            return 0.0
        adapter = ScoringBuyerAdapter(settings=settings)
        report = evaluate(adapter=adapter, cases=cases, fixtures=fixtures)
        val = report["overall"]["ndcgAtK"]["10"]
        return val if val is not None else 0.0

    n_holder_golden = {}
    golden_best_score, golden_best_vec, golden_n_evals = random_search_and_coord_descent(
        golden_objective, RANDOM_SEARCH_N, SEED, n_holder_golden
    )

    step2 = {
        "method": (
            f"random search N={RANDOM_SEARCH_N} (seed={SEED}) + coordinate descent "
            f"top{COORD_TOP_K} x {COORD_PASSES} passes, grid step {COORD_GRID_STEP}"
        ),
        "nEvals": golden_n_evals,
        "nInvalidWeightTrials_allFivePositiveWeightsZero": INVALID_WEIGHT_TRIAL_COUNT["n"],
        "bestNdcgAt10": golden_best_score,
        "bestWeights": dict(zip(WEIGHT_NAMES, golden_best_vec, strict=True)),
        "deltaVsDefault": golden_best_score - default_ndcg,
        "note": "in-sample(dev 31건에 직접 맞춘) 낙관적 상한. holdout 미사용. 축퇴 해 위험은 모듈 docstring 참조.",
    }

    # ---------------- Step 3: teacher-fit 상한 ----------------
    pipeline_data = json.loads(PIPELINE_RESULTS.read_text(encoding="utf-8"))
    rep1_rows = {r["caseId"]: r for r in pipeline_data["caseResults"] if r["repeat"] == 1}

    embeddings = load_embeddings()
    catalog = fixtures.catalog

    teacher_fit_cases = []
    excluded_no_rerank = []
    for case in cases:
        row = rep1_rows.get(case.case_id)
        if row is None:
            excluded_no_rerank.append(case.case_id)
            continue
        rerank_calls = [c for c in row["providerCalls"] if c["callSite"] == "rerank"]
        if not rerank_calls:
            excluded_no_rerank.append(case.case_id)
            continue
        eligible_ids = row["metrics"]["eligibleProductIds"]
        teacher_ranked = row["rankedProductIds"]
        products = [catalog[str(pid)] for pid in eligible_ids if str(pid) in catalog]
        if not products:
            excluded_no_rerank.append(case.case_id)
            continue
        K = len(eligible_ids)
        # teacher 순위 -> graded relevance: top-1 = K, 2위 = K-1, ... 미노출 후보 = 0
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
                    default_settings.scoring_reference_date,
                    default_settings.scoring_recent_purchase_window_days,
                ),
                "teacherGrades": teacher_grades,
                "goldenGrades": case.relevance_grades,
                "K": K,
            }
        )

    def teacher_fit_objective(vec):
        weights = ScoringWeights(*vec)
        vals = []
        for c in teacher_fit_cases:
            result = score_products(
                c["products"],
                query_embedding=c["queryEmbedding"],
                product_embeddings=embeddings.documents,
                profile_preferences=c["profilePreferences"],
                recency_by_product=None,
                recent_product_ids=c["recentProductIds"],
                weights=weights,
            )
            v = ndcg_at_k(result.ranked_product_ids, c["teacherGrades"], 10)
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if vals else 0.0

    n_holder_teacher = {}
    teacher_best_score, teacher_best_vec, teacher_n_evals = random_search_and_coord_descent(
        teacher_fit_objective, RANDOM_SEARCH_N, SEED + 1, n_holder_teacher
    )

    def golden_on_teacher_candidates(vec):
        weights = ScoringWeights(*vec)
        vals = []
        for c in teacher_fit_cases:
            result = score_products(
                c["products"],
                query_embedding=c["queryEmbedding"],
                product_embeddings=embeddings.documents,
                profile_preferences=c["profilePreferences"],
                recency_by_product=None,
                recent_product_ids=c["recentProductIds"],
                weights=weights,
            )
            v = ndcg_at_k(result.ranked_product_ids, c["goldenGrades"], 10)
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if vals else 0.0, len(vals)

    teacher_fit_golden_on_teacher_cands, teacher_fit_golden_case_count = golden_on_teacher_candidates(
        teacher_best_vec
    )

    # teacher_fit_objective 는 ScoringWeights(검증 없는 NamedTuple)로 탐색해 golden_objective
    # 와 달리 "5개 양의 가중치 전부 0" 코너를 만날 수 있다 — 그러면 evaluate() 경로 자체를
    # 못 도니 None 으로 남기고 그 사실을 보고서에 남긴다.
    teacher_fit_settings_invalid_reason = None
    try:
        teacher_fit_settings = make_settings(teacher_best_vec)
        teacher_fit_adapter = ScoringBuyerAdapter(settings=teacher_fit_settings)
        teacher_fit_report = evaluate(adapter=teacher_fit_adapter, cases=cases, fixtures=fixtures)
        teacher_fit_golden_standard_ndcg = teacher_fit_report["overall"]["ndcgAtK"]["10"]
    except Exception as exc:  # noqa: BLE001 - 코너 케이스를 그대로 관측 대상으로 남긴다
        teacher_fit_golden_standard_ndcg = None
        teacher_fit_settings_invalid_reason = f"{type(exc).__name__}: {exc}"

    step3 = {
        "teacherRankSource": str(PIPELINE_RESULTS),
        "teacherRepeatUsed": 1,
        "gradedRelevanceFormula": (
            "teacher 1위=K, 2위=K-1, ..., 마지막 순위=1; teacher가 아예 노출하지 않은 eligible "
            "후보=0 (K=해당 케이스 eligibleProductIds 개수)"
        ),
        "nCasesUsed": len(teacher_fit_cases),
        "nCasesExcluded_noTeacherRerankCall": len(excluded_no_rerank),
        "excludedCaseIds": excluded_no_rerank,
        "candidateSetPolicy": (
            "student 후보를 teacher eligibleProductIds로 강제 일치(hard_filter 재적용 없이 "
            "score_products 직접 호출)"
        ),
        "method": (
            f"random search N={RANDOM_SEARCH_N} (seed={SEED + 1}) + coordinate descent "
            f"top{COORD_TOP_K} x {COORD_PASSES} passes, grid step {COORD_GRID_STEP}"
        ),
        "nEvals": teacher_n_evals,
        "bestTeacherFitObjective_ndcgAt10_vsTeacherGrades": teacher_best_score,
        "bestWeights": dict(zip(WEIGHT_NAMES, teacher_best_vec, strict=True)),
        "sameWeights_ndcgAt10_vsGoldenGrades_onTeacherCandidateSet": teacher_fit_golden_on_teacher_cands,
        "sameWeights_goldenCaseCount_onTeacherCandidateSet": teacher_fit_golden_case_count,
        "sameWeights_ndcgAt10_vsGoldenGrades_standardEvaluatePath": teacher_fit_golden_standard_ndcg,
        "sameWeights_standardEvaluatePath_invalidReason": teacher_fit_settings_invalid_reason,
        "comparisonTable": {
            "currentDefaultWeights_golden_standardPath": default_ndcg,
            "teacherReportedNdcgAt10_reference": 0.782943,
            "step2_oracleUpperBound_golden_standardPath": golden_best_score,
            "step3_teacherFitWeights_golden_standardPath": teacher_fit_golden_standard_ndcg,
            "step3_teacherFitWeights_golden_onTeacherCandidateSet": teacher_fit_golden_on_teacher_cands,
        },
    }

    # ---------------- Step 4: 성분 기여 분해 ----------------
    ablation_rows = []
    for name in WEIGHT_NAMES:
        vec = [DEFAULT_WEIGHTS[n] for n in WEIGHT_NAMES]
        idx = WEIGHT_NAMES.index(name)
        vec[idx] = 0.0
        settings = make_settings(vec)
        adapter = ScoringBuyerAdapter(settings=settings)
        report = evaluate(adapter=adapter, cases=cases, fixtures=fixtures)
        ndcg = report["overall"]["ndcgAtK"]["10"]
        ablation_rows.append(
            {"zeroedComponent": name, "ndcgAt10": ndcg, "deltaVsDefault": ndcg - default_ndcg}
        )

    step4 = {"defaultNdcgAt10": default_ndcg, "rows": ablation_rows}

    results = {
        "step1_pathValidation": step1,
        "step2_goldenOracleUpperBound": step2,
        "step3_teacherFitUpperBound": step3,
        "step4_componentAblation": step4,
    }
    (out_dir / "e2_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = []
    lines.append("# E2 — student 용량 상한 프로브\n")
    lines.append("## Step 1 — 경로 검증\n")
    lines.append(f"- 기본 가중치 nDCG@10 (재계산) = {default_ndcg:.6f}")
    lines.append(
        f"- 커밋된 baseline = {committed_scoring_ndcg:.6f}, diff = {step1['diff']:.2e}, "
        f"재현 여부: {reproduced_step1}\n"
    )

    lines.append("## Step 2 — 골든 라벨 기준 in-sample 오라클 상한\n")
    lines.append(f"- {step2['method']}")
    lines.append(
        f"- 실제 사용 평가 횟수: {golden_n_evals} (그중 무효 시도 — 5개 양의 가중치 전부 0인 코너, "
        f"Settings 검증 거부: {INVALID_WEIGHT_TRIAL_COUNT['n']}건, 점수 0.0으로 처리)"
    )
    lines.append(f"- 최댓값 nDCG@10 = {golden_best_score:.6f} (기본 대비 +{step2['deltaVsDefault']:.6f})")
    lines.append(f"- 가중치: {step2['bestWeights']}")
    lines.append("- **주의: in-sample(dev 31건에 직접 맞춘) 낙관적 상한이며 holdout 은 열지 않았다. 축퇴 해 위험은 모듈 docstring 참조.**\n")

    lines.append("## Step 3 — teacher 모방 상한\n")
    lines.append(
        f"- teacher 순위 출처: repeat=1, {step3['nCasesUsed']}케이스 사용, "
        f"{step3['nCasesExcluded_noTeacherRerankCall']}케이스 제외(teacher rerank 콜 없음): "
        f"{excluded_no_rerank}"
    )
    lines.append(f"- graded relevance 변환식: {step3['gradedRelevanceFormula']}")
    lines.append(f"- {step3['method']}, 실제 평가 횟수: {teacher_n_evals}")
    lines.append(f"- teacher-fit 목적함수 최댓값(teacher grades 기준, teacher 후보집합) = {teacher_best_score:.6f}")
    lines.append(
        f"- 같은 가중치를 golden grades에 적용(teacher 후보집합 그대로) = "
        f"{teacher_fit_golden_on_teacher_cands:.6f} (n={teacher_fit_golden_case_count})"
    )
    standard_path_str = (
        f"{teacher_fit_golden_standard_ndcg:.6f}"
        if teacher_fit_golden_standard_ndcg is not None
        else f"측정 불가 — {teacher_fit_settings_invalid_reason}"
    )
    lines.append(f"- 같은 가중치를 표준 evaluate() 경로(학생 자체 후보집합)로 재평가 = {standard_path_str}")
    lines.append(f"- 가중치: {step3['bestWeights']}\n")

    lines.append("### 나란히 비교 (골든 라벨, 표준 경로 기준)\n")
    lines.append("| 항목 | nDCG@10 |")
    lines.append("|---|---:|")
    lines.append(f"| 현재 기본 가중치(student) | {default_ndcg:.6f} |")
    lines.append("| teacher(pipeline arm, 커밋 참고값) | 0.782943 |")
    lines.append(f"| Step2 골든 오라클 상한(in-sample) | {golden_best_score:.6f} |")
    lines.append(f"| Step3 teacher-fit 가중치(표준 경로 골든 평가) | {standard_path_str} |")
    lines.append(f"| Step3 teacher-fit 가중치(teacher 후보집합 골든 평가) | {teacher_fit_golden_on_teacher_cands:.6f} |\n")

    lines.append("## Step 4 — 성분 기여 분해(기본 가중치에서 하나씩 0으로)\n")
    lines.append("| 성분 | nDCG@10 | 델타 |")
    lines.append("|---|---:|---:|")
    for row in ablation_rows:
        lines.append(f"| {row['zeroedComponent']} | {row['ndcgAt10']:.6f} | {row['deltaVsDefault']:+.6f} |")

    (out_dir / "e2_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("E2 done.")
    print("step1 reproduced:", reproduced_step1, "default_ndcg", default_ndcg)
    print("step2 best", golden_best_score, "n_evals", golden_n_evals)
    print(
        "step3 teacher-fit best (teacher grades)",
        teacher_best_score,
        "-> golden standard path",
        teacher_fit_golden_standard_ndcg,
    )


if __name__ == "__main__":
    main()
