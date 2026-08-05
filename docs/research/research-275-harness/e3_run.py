#!/usr/bin/env python3
"""E3 — teacher 라벨 안정성·비용 실측 파일럿. 실 LLM 호출 있음. 상한 USD 0.50 하드 가드.

**경고**: 이 스크립트는 실행 시 실제 과금이 발생한다(smart/fast 티어 실 LLM 호출). CI 에서
자동 실행하지 않는 수동 도구다 — README.md 의 경고를 먼저 읽는다.

절차:
  1) fast 티어 LLM으로 합성 구매자 질의 12건 생성(카테고리 seed는 dev 케이스 분포 상위 12개).
  2) 카테고리별 K=20 후보 구성(같은 카테고리 위주 + 타 카테고리 혼입), smart 티어로
     identity/reversed/shuffled 순서 x 2반복 = 질의당 6콜, 총 최대 72콜.
  3) 순서 민감도 vs 반복 민감도 비교, 비용 집계.

판정에 영향을 주는 고정 축: seed=20260803(카테고리 seed·후보 구성·셔플 순서 전부 이 값에서
파생), K_CANDIDATES=20, REPEATS_PER_ORDER=2, ORDERS=identity/reversed/shuffled. 이 값들을
바꾸면 E1-c 회귀식과의 교차 검증(K=20 예측치 대조)이 무효화된다.
"""

import argparse
import asyncio
import json
import random
from collections import Counter
from itertools import combinations
from pathlib import Path

from app.agents.buyer.recommendation.rerank import rerank
from app.agents.buyer.recommendation.state import extract_json
from app.core.config import get_settings
from app.core.llm import LLMError, get_llm, resolve_model_id, resolve_provider_model
from app.schemas.spring import SpringProduct
from evals.goldenset.loader import load_cases
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.model_eval.budget import BudgetLimits, BudgetTracker
from evals.model_eval.pricing import PriceBook
from evals.model_eval.recording import RecordingLLM

SEED = 20260803
# 기본 예산은 0.50 USD다. 2026-08-05 파일럿은 스모크 테스트(N=2, identity만, repeat=1)에
# $0.0019432499999999999 를 먼저 써서 이 스크립트의 실제 상한을 $0.49805675 로 낮춰 실행했다 —
# 이는 그날 실행에만 해당하는 기록이며 다음 실행의 기본값이 아니다. 남은 예산을 넘겨주려면
# --hard-budget-usd 로 명시한다.
DEFAULT_HARD_BUDGET_USD = 0.50
N_QUERIES = 12
K_CANDIDATES = 20
SAME_CATEGORY_COUNT = 16  # K=20 중 같은 카테고리 위주 개수, 나머지는 타 카테고리 혼입
REPEATS_PER_ORDER = 2
ORDERS = ("identity", "reversed", "shuffled")


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("pyproject.toml을 찾지 못해 저장소 루트를 확정할 수 없다")


REPO_ROOT = _repo_root()
CATALOG_PATH = REPO_ROOT / "evals/goldenset/fixtures/catalog_snapshot.json"
SEARCH_RESPONSES_PATH = REPO_ROOT / "evals/goldenset/fixtures/search_responses.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="synth_queries.json/e3_results.json/e3_report.md를 쓸 출력 디렉터리"
    )
    parser.add_argument(
        "--hard-budget-usd",
        type=float,
        default=DEFAULT_HARD_BUDGET_USD,
        help=f"이 실행에서 쓸 수 있는 상한(USD). 기본값 {DEFAULT_HARD_BUDGET_USD}",
    )
    return parser.parse_args()


def load_catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def dev_category_distribution():
    catalog = load_catalog()
    search_responses = json.loads(SEARCH_RESPONSES_PATH.read_text(encoding="utf-8"))
    cases = list(load_cases("dev"))
    pids = set()
    for case in cases:
        fid = case.search_fixture_id
        if fid and fid in search_responses:
            pids.update(search_responses[fid].get("productIds", []))
    counts = Counter(
        catalog[str(pid)].get("categoryName")
        for pid in pids
        if str(pid) in catalog and catalog[str(pid)].get("categoryName")
    )
    return counts


def to_spring_product(product_id, row: dict) -> SpringProduct:
    del product_id  # row["productId"]가 이미 있다 — catalog_snapshot 은 I-1 응답 그대로다.
    return SpringProduct.model_validate(row)


def build_candidates(catalog: dict, category: str, rng: random.Random) -> list[int]:
    """같은 카테고리 위주 + 타 카테고리 혼입으로 K=20 후보 productId 리스트를 만든다."""
    same_category = sorted(
        int(pid) for pid, row in catalog.items() if row.get("categoryName") == category
    )
    other_category = sorted(
        int(pid) for pid, row in catalog.items() if row.get("categoryName") != category
    )
    rng.shuffle(same_category)
    rng.shuffle(other_category)
    picked_same = same_category[:SAME_CATEGORY_COUNT]
    remaining = K_CANDIDATES - len(picked_same)
    picked_other = other_category[:remaining]
    candidates = picked_same + picked_other
    return candidates


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def kendall_tau_b_common(rank_a, rank_b):
    common = [x for x in rank_a if x in rank_b]
    if len(common) < 2:
        return None
    pos_a = {v: i for i, v in enumerate(rank_a)}
    pos_b = {v: i for i, v in enumerate(rank_b)}
    n = len(common)
    concordant = discordant = ties_a = ties_b = 0
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


async def generate_synth_queries(llm, pacer, tracker, categories, hard_budget_usd):
    """fast 티어로 카테고리당 1개씩 합성 구매자 발화를 생성한다. 실패하면 빈 리스트+사유."""
    system = (
        "당신은 커머스 앱의 실제 구매자를 흉내내는 데이터 생성기입니다. "
        "주어진 상품 카테고리를 염두에 두고, 그 카테고리 상품을 찾는 한국어 구매자 채팅 발화를 "
        "1개 만드세요. 실제 사용자가 챗봇에 치는 자연스러운 문장이어야 하고, 카테고리명을 "
        "그대로 복사하지 말고 자연어로 녹여내세요. 반드시 JSON만 출력하세요(설명·코드펜스 금지): "
        '{"query": "한국어 구매자 발화 1문장"}'
    )
    results = []
    for category in categories:
        if tracker.total_cost_usd >= hard_budget_usd:
            results.append({"category": category, "query": None, "error": "budgetGateBeforeCall"})
            continue
        user = f"CATEGORY: {category}"
        await pacer.acquire()
        try:
            raw = await llm.complete(system=system, user=user, tier="fast", max_tokens=300, json_output=True)
            data = extract_json(raw)
            query = data.get("query")
            results.append(
                {
                    "category": category,
                    "prompt": {"system": system, "user": user},
                    "rawResponse": raw,
                    "query": query if isinstance(query, str) and query.strip() else None,
                    "error": None if isinstance(query, str) and query.strip() else "emptyOrInvalidQuery",
                }
            )
        except Exception as exc:  # noqa: BLE001 - 실패를 그대로 관측 대상으로 남긴다
            results.append(
                {
                    "category": category,
                    "prompt": {"system": system, "user": user},
                    "query": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


async def run_teacher_calls(llm, pacer, tracker, queries_with_candidates, expose_max, hard_budget_usd):
    """질의마다 identity/reversed/shuffled x 2반복 rerank 콜. 콜 전 예산 가드."""
    budget_stopped = False
    rng_shuffle = random.Random(SEED)
    for entry in queries_with_candidates:
        query = entry["query"]
        candidates: list[SpringProduct] = entry["candidates"]
        identity_order = list(candidates)
        reversed_order = list(reversed(candidates))
        shuffled_order = list(candidates)
        rng_shuffle.shuffle(shuffled_order)
        order_map = {"identity": identity_order, "reversed": reversed_order, "shuffled": shuffled_order}
        entry["orderProductIds"] = {
            name: [c.product_id for c in order_list] for name, order_list in order_map.items()
        }
        entry["calls"] = []
        for order_name in ORDERS:
            for repeat_idx in range(REPEATS_PER_ORDER):
                if tracker.total_cost_usd >= hard_budget_usd:
                    budget_stopped = True
                    entry["calls"].append(
                        {"order": order_name, "repeat": repeat_idx, "skipped": True, "reason": "budgetGateBeforeCall"}
                    )
                    continue
                await pacer.acquire()
                call_started_len = len(llm.calls)
                try:
                    result = await rerank(
                        llm,
                        query=query,
                        candidates=order_map[order_name],
                        profile_summary=None,
                        tier="smart",
                        expose_max=expose_max,
                    )
                    ranked_ids = [pid for pid, _ in result.ranked]
                    error = None
                except LLMError as exc:
                    ranked_ids = []
                    error = f"{type(exc).__name__}: {exc}"
                recorded = llm.calls[call_started_len:]
                entry["calls"].append(
                    {
                        "order": order_name,
                        "repeat": repeat_idx,
                        "rankedProductIds": ranked_ids,
                        "error": error,
                        "providerCalls": recorded,
                    }
                )
        if budget_stopped:
            break
    return budget_stopped


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    hard_budget_usd = args.hard_budget_usd

    settings = get_settings()
    catalog = load_catalog()

    dist = dev_category_distribution()
    top_categories = [name for name, _ in dist.most_common(N_QUERIES)]

    pricing = PriceBook.load()
    tracker = BudgetTracker(
        limits=BudgetLimits(max_calls=200, max_total_tokens=10_000_000, max_cost_usd=hard_budget_usd)
    )
    pacer = GlobalPacer(limits=PacerLimits(max_rpm=45))

    delegate = get_llm()
    if delegate is None:
        result = {
            "status": "failed",
            "reason": "get_llm() returned None — API key not configured in this worktree's .env",
        }
        (out_dir / "e3_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("E3 FAILED: no LLM configured")
        return

    fast_model = resolve_model_id(settings, "fast")
    smart_model = resolve_model_id(settings, "smart")
    fast_resolved = resolve_provider_model(settings, "fast")
    smart_resolved = resolve_provider_model(settings, "smart", with_tools=False)
    llm = RecordingLLM(
        delegate,
        models={"fast": fast_model, "smart": smart_model},
        reasoning_efforts={"fast": fast_resolved.reasoning_effort, "smart": smart_resolved.reasoning_effort},
        pricing=pricing,
        budget=tracker,
    )

    # ---------------- Step 1: 합성 질의 12건 ----------------
    synth_results = asyncio.run(generate_synth_queries(llm, pacer, tracker, top_categories, hard_budget_usd))
    (out_dir / "synth_queries.json").write_text(
        json.dumps(
            {
                "categorySeedSelection": (
                    f"dev split 31케이스가 참조하는 search fixture productIds의 categoryName 분포에서 "
                    f"빈도 상위 {N_QUERIES}개를 결정론적으로 선택"
                ),
                "categoryDistributionTop": dist.most_common(20),
                "selectedCategories": top_categories,
                "queries": synth_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    valid_entries = [r for r in synth_results if r.get("query")]
    synth_failed = len(valid_entries) < N_QUERIES

    if not valid_entries:
        result = {
            "status": "failed",
            "reason": "합성 질의 생성 전량 실패 — 손으로 12건을 만들지 않음(관측 대상)",
            "step1_synthQueries": synth_results,
            "budgetSnapshot": tracker.snapshot(),
        }
        (out_dir / "e3_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("E3 step1 FAILED: no valid synthetic queries generated")
        return

    fallback_used = False
    if synth_failed:
        # 부족분은 dev 골든 질의로 대체하고 그 사실을 명시한다.
        fallback_used = True
        cases = list(load_cases("dev"))
        needed = N_QUERIES - len(valid_entries)
        for case in cases:
            if needed <= 0:
                break
            valid_entries.append(
                {"category": f"__devFallback__:{case.case_id}", "query": case.query, "error": None, "source": "devGoldenFallback"}
            )
            needed -= 1

    # ---------------- Step 2: 후보 구성 + teacher 실행 ----------------
    rng_cand = random.Random(SEED)
    queries_with_candidates = []
    for entry in valid_entries:
        category = entry["category"]
        base_category = category if not category.startswith("__devFallback__") else None
        if base_category and base_category in {row.get("categoryName") for row in catalog.values()}:
            cand_ids = build_candidates(catalog, base_category, rng_cand)
        else:
            all_ids = sorted(int(pid) for pid in catalog)
            rng_cand.shuffle(all_ids)
            cand_ids = all_ids[:K_CANDIDATES]
        candidates = [to_spring_product(pid, catalog[str(pid)]) for pid in cand_ids]
        queries_with_candidates.append(
            {
                "category": category,
                "query": entry["query"],
                "candidateProductIds": cand_ids,
                "candidateCount": len(candidates),
                "candidates": candidates,
            }
        )

    budget_stopped_before_teacher = tracker.total_cost_usd >= hard_budget_usd
    if not budget_stopped_before_teacher:
        budget_stopped = asyncio.run(
            run_teacher_calls(llm, pacer, tracker, queries_with_candidates, settings.expose_max, hard_budget_usd)
        )
    else:
        budget_stopped = True

    # ---------------- Step 3: 측정 ----------------
    per_query_analysis = []
    for entry in queries_with_candidates:
        calls = entry.get("calls", [])
        by_order = {}
        for call in calls:
            if call.get("skipped"):
                continue
            by_order.setdefault(call["order"], []).append(call["rankedProductIds"])

        order_sensitivity = {}
        for o1, o2 in combinations(ORDERS, 2):
            if o1 in by_order and o2 in by_order and by_order[o1] and by_order[o2]:
                r1, r2 = by_order[o1][0], by_order[o2][0]
                order_sensitivity[f"{o1}_vs_{o2}"] = {
                    "top1Match": bool(r1 and r2 and r1[0] == r2[0]),
                    "top5Jaccard": jaccard(r1[:5], r2[:5]),
                    "kendallTauBCommon": kendall_tau_b_common(r1, r2),
                }

        repeat_sensitivity = {}
        for order_name, ranks in by_order.items():
            if len(ranks) >= 2:
                r1, r2 = ranks[0], ranks[1]
                repeat_sensitivity[order_name] = {
                    "top1Match": bool(r1 and r2 and r1[0] == r2[0]),
                    "top5Jaccard": jaccard(r1[:5], r2[:5]),
                    "kendallTauBCommon": kendall_tau_b_common(r1, r2),
                }

        per_query_analysis.append(
            {
                "category": entry["category"],
                "query": entry["query"],
                "orderSensitivity": order_sensitivity,
                "repeatSensitivity": repeat_sensitivity,
            }
        )

    def _avg(key_path_getter, source):
        vals = []
        for row in source:
            for v in row.values():
                x = key_path_getter(v)
                if x is not None:
                    vals.append(x)
        return sum(vals) / len(vals) if vals else None

    order_rows = [row["orderSensitivity"] for row in per_query_analysis]
    repeat_rows = [row["repeatSensitivity"] for row in per_query_analysis]

    summary = {
        "orderSensitivity": {
            "avgTop1MatchRate": _avg(lambda v: 1.0 if v["top1Match"] else 0.0, order_rows),
            "avgTop5Jaccard": _avg(lambda v: v["top5Jaccard"], order_rows),
            "avgKendallTauBCommon": _avg(lambda v: v["kendallTauBCommon"], order_rows),
        },
        "repeatSensitivity": {
            "avgTop1MatchRate": _avg(lambda v: 1.0 if v["top1Match"] else 0.0, repeat_rows),
            "avgTop5Jaccard": _avg(lambda v: v["top5Jaccard"], repeat_rows),
            "avgKendallTauBCommon": _avg(lambda v: v["kendallTauBCommon"], repeat_rows),
        },
    }
    order_gt_repeat = None
    if (
        summary["orderSensitivity"]["avgKendallTauBCommon"] is not None
        and summary["repeatSensitivity"]["avgKendallTauBCommon"] is not None
    ):
        # tau가 낮을수록 더 흔들림(민감) — "순서 효과 > 반복 효과"는 order의 tau가 더 낮다는 뜻.
        order_gt_repeat = (
            summary["orderSensitivity"]["avgKendallTauBCommon"]
            < summary["repeatSensitivity"]["avgKendallTauBCommon"]
        )
    summary["orderEffectLargerThanRepeatEffect_byKendallTau"] = order_gt_repeat

    all_calls_flat = []
    for entry in queries_with_candidates:
        for call in entry.get("calls", []):
            if call.get("skipped"):
                continue
            for pc in call.get("providerCalls", []):
                all_calls_flat.append(
                    {**pc, "category": entry["category"], "query": entry["query"], "order": call["order"], "repeat": call["repeat"]}
                )

    rerank_calls = list(all_calls_flat)
    costs = [c["costUsd"] for c in rerank_calls if isinstance(c.get("costUsd"), (int, float))]
    input_tokens = [c["inputTokens"] for c in rerank_calls if isinstance(c.get("inputTokens"), int)]
    output_tokens = [c["outputTokens"] for c in rerank_calls if isinstance(c.get("outputTokens"), int)]
    latencies = [c["latencyMs"] for c in rerank_calls if isinstance(c.get("latencyMs"), (int, float))]
    error_calls = [c for c in rerank_calls if c.get("error")]

    cost_summary = {
        "nCalls": len(rerank_calls),
        "totalCostUsd": sum(costs) if costs else 0.0,
        "meanCostUsd": sum(costs) / len(costs) if costs else None,
        "meanInputTokens": sum(input_tokens) / len(input_tokens) if input_tokens else None,
        "meanOutputTokens": sum(output_tokens) / len(output_tokens) if output_tokens else None,
        "meanLatencyMs": sum(latencies) / len(latencies) if latencies else None,
        "errorCallCount": len(error_calls),
    }

    # E1-c 회귀식과 대조 (K=20에서 예측). E1 은 이 harness 의 형제 산출물 e1_results.json 을
    # e1_analyze.py --out 으로 같은 디렉터리에 미리 만들어 뒀을 때만 대조된다(없으면 스킵).
    e1c_path = out_dir / "e1_results.json"
    e1_prediction = None
    if e1c_path.exists():
        e1_data = json.loads(e1c_path.read_text(encoding="utf-8"))
        reg = e1_data.get("e1c_tokensVsK", {}).get("regressionInputTokensOnK")
        if reg:
            e1_prediction = {
                "predictedInputTokensAtK20": reg["intercept"] + reg["slope"] * K_CANDIDATES,
                "regressionUsed": reg,
            }

    results = {
        "status": "completed" if not budget_stopped else "partial_budgetStopped",
        "hardBudgetUsd": hard_budget_usd,
        "budgetSnapshot": tracker.snapshot(),
        "pacerSnapshot": pacer.snapshot(),
        "synthQueryGeneration": {
            "succeededCount": sum(1 for r in synth_results if r.get("query")),
            "requestedCount": N_QUERIES,
            "fallbackToDevGoldenQueriesUsed": fallback_used,
        },
        "categorySelection": {"topCategories": top_categories, "distributionTop20": dist.most_common(20)},
        "candidateConstructionRule": (
            f"K={K_CANDIDATES}: 같은 카테고리에서 최대 {SAME_CATEGORY_COUNT}개(카탈로그 내 셔플 후 "
            f"앞에서부터), 부족분은 타 카테고리에서 채움(seed={SEED}). fallback 질의(카테고리 seed "
            "없음)는 전체 카탈로그 무작위 K개."
        ),
        "perQueryCandidateCounts": [
            {"category": e["category"], "query": e["query"], "candidateCount": e["candidateCount"]}
            for e in queries_with_candidates
        ],
        "measurement": {"perQuery": per_query_analysis, "summary": summary},
        "costSummary": cost_summary,
        "e1RegressionComparisonAtK20": e1_prediction,
        "exposeMax": settings.expose_max,
        "rawCallsFlat": all_calls_flat,
    }
    (out_dir / "e3_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = []
    lines.append("# E3 — teacher 라벨 안정성·비용 실측 파일럿\n")
    lines.append(f"- 상태: {results['status']}")
    lines.append(f"- 실제 지출: ${tracker.total_cost_usd:.6f} / 상한 ${hard_budget_usd}")
    lines.append(
        f"- 합성 질의 생성 성공: {results['synthQueryGeneration']['succeededCount']}/{N_QUERIES}, "
        f"dev 골든 대체 사용: {fallback_used}"
    )
    lines.append(f"- rerank 콜 수: {cost_summary['nCalls']}, 오류 콜: {cost_summary['errorCallCount']}\n")
    lines.append("## 순서 민감도 vs 반복 민감도 (공통항목 Kendall tau-b 평균)\n")
    lines.append(
        f"- 순서 민감도(identity/reversed/shuffled 쌍): tau={summary['orderSensitivity']['avgKendallTauBCommon']}, "
        f"top1일치율={summary['orderSensitivity']['avgTop1MatchRate']}, "
        f"top5 Jaccard={summary['orderSensitivity']['avgTop5Jaccard']}"
    )
    lines.append(
        f"- 반복 민감도(같은 순서 2회): tau={summary['repeatSensitivity']['avgKendallTauBCommon']}, "
        f"top1일치율={summary['repeatSensitivity']['avgTop1MatchRate']}, "
        f"top5 Jaccard={summary['repeatSensitivity']['avgTop5Jaccard']}"
    )
    lines.append(f"- 순서 효과가 반복 효과보다 큰가(=tau가 더 낮은가): {order_gt_repeat}\n")
    lines.append("## 비용\n")
    lines.append(
        f"- 콜당 평균 비용 ${cost_summary['meanCostUsd']}, 평균 inputTokens={cost_summary['meanInputTokens']}, "
        f"평균 outputTokens={cost_summary['meanOutputTokens']}, 평균 latency={cost_summary['meanLatencyMs']}ms"
    )
    if e1_prediction:
        lines.append(
            f"- E1-c 회귀식이 K=20에서 예측한 inputTokens = {e1_prediction['predictedInputTokensAtK20']:.2f} "
            "(실측 평균과 대조)"
        )
    (out_dir / "e3_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("E3 done. status=", results["status"], "spend=$", tracker.total_cost_usd)


if __name__ == "__main__":
    main()
