"""개인화 Tier D 5-arm ablation과 Tier L scope paired 평가 CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import GoldenCase, Identity
from evals.metrics.report import normalize_artifacts, write_artifacts
from evals.metrics.run_manifest import build_run_manifest, strip_volatile_manifest_keys
from evals.metrics.runner import evaluate, load_evaluation_fixtures
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.budget import BudgetLimits, BudgetTracker
from evals.model_eval.cli import build_live_adapter
from evals.model_eval.pricing import PriceBook
from evals.model_eval.repeats import run_repeats
from evals.model_eval.report import write_artifacts as write_live_artifacts
from evals.personalization.activation import ranking_change
from evals.personalization.adapter import ProfileScoringBuyerAdapter
from evals.personalization.config import CONFIG_PATH, load_eval_config
from evals.personalization.fixtures import (
    FIXTURE_DIR,
    derive_case_preferences,
    load_profile_arms,
)
from evals.personalization.live_adapter import (
    PersonalizationLiveBuyerAdapter,
    estimate_live_matrix,
    filter_axis_leakage,
)
from evals.personalization.overreach import (
    clean_noisy_drop_verdict,
    count_verdict,
    explicit_intent_contradictions,
    new_forbidden_or_recent_inclusions,
)
from evals.personalization.stats import paired_metric_deltas
from evals.scoring.adapter import _recent_ids

DEFAULT_SEED = 20260803
DEFAULT_WEIGHT = 0.15
ROOT = Path(__file__).resolve().parent
PERSONALIZATION_MODULE_PATHS = {
    path.name: path
    for path in sorted(ROOT.glob("*.py"))
    if path.name not in {"__init__.py", "__main__.py"}
}
LIVE_ARMS = ("guest", "clean_rerank_only", "clean_both")
# Tier L paired 비교의 기준선 arm. 상수로 뽑아 두는 이유는 이득(`pairedVsGuest`)과 활성화
# (`rankingChange`)이 **같은 기준선**을 보게 강제하기 위해서다 — 두 지표가 다른 arm 을 기준으로
# 잡히면 같은 표에서 읽을 수 없다. `guest` 는 프로필뿐 아니라 identity 도 달라 프로필 효과가
# 재구매 dedup 효과와 섞이는데(실측: live-v1 헤드라인의 절반 이상), 그 교체는 #483 소관이다.
LIVE_BASELINE_ARM = "guest"
PAIR_DEFINITIONS = {
    "clean_vs_member_no_profile": ("clean", "member_no_profile"),
    "noisy_vs_clean": ("noisy", "clean"),
    "repeated_vs_clean": ("repeated", "clean"),
    "member_no_profile_vs_guest": ("member_no_profile", "guest"),
}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(_json_text(value) + "\n", encoding="utf-8", newline="\n")


def _weight_dir(weight: float) -> str:
    return f"weight-{weight:g}".replace(".", "p")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(*, command: str, seed: int) -> dict[str, Any]:
    manifest = build_run_manifest(command=command, seed=seed)
    hashes = manifest["hashes"]
    hashes["profileFixtures"] = {
        path.name: _sha256(path) for path in sorted(FIXTURE_DIR.iterdir()) if path.is_file()
    }
    hashes["personalizationModules"] = {
        name: _sha256(path) for name, path in sorted(PERSONALIZATION_MODULE_PATHS.items())
    }
    hashes["evalConfig"] = _sha256(CONFIG_PATH)
    return manifest


def _primary_k(config: dict[str, Any]) -> int:
    parts = str(config["primaryMetric"]).split(".")
    if len(parts) != 3 or parts[:2] != ["overall", "ndcgAtK"] or not parts[2].isdigit():
        raise ValueError("personalization primaryMetric은 overall.ndcgAtK.<K>여야 합니다")
    return int(parts[2])


def case_has_repurchase_intent(case: GoldenCase) -> bool:
    """골든셋 판정 정본인 repurchase slice로 최근구매 검사 제외 여부를 정한다."""
    return "repurchase" in case.slices


def _cases_for_identity(cases: list[GoldenCase], identity_kind: str) -> list[GoldenCase]:
    return [
        case.model_copy(
            update={
                "identity": Identity(
                    kind=identity_kind,
                    persona_id=None if identity_kind == "guest" else case.identity.persona_id,
                )
            }
        )
        for case in cases
    ]


def _paired_comparisons(
    arm_reports: dict[str, dict[str, Any]],
    *,
    k_list: tuple[int, ...],
    bootstrap: dict[str, Any],
) -> dict[str, Any]:
    return {
        name: paired_metric_deltas(
            arm_reports[current],
            arm_reports[baseline],
            k_list=k_list,
            resamples=int(bootstrap["resamples"]),
            confidence=float(bootstrap["confidence"]),
            seed=int(bootstrap["seed"]),
        )
        for name, (current, baseline) in PAIR_DEFINITIONS.items()
    }


def _comparison_markdown(comparison: dict[str, Any], *, primary_k: int) -> str:
    lines = [
        "# 개인화 5-arm paired 비교",
        "",
        "- 주 비교: `clean_vs_member_no_profile` (identity·최근구매 조건 동일, profile만 변경)",
        "- `member_no_profile_vs_guest`는 identity·최근구매가 섞인 cold-start 보조 비교",
        f"- hard-filter verdict: **{comparison['hardFilter']['verdict']}**",
        "",
        f"## 기본 weight arm별 nDCG@{primary_k}",
        "",
        f"| arm | nDCG@{primary_k} | diversity |",
        "|---|---:|---:|",
    ]
    for arm, summary in comparison["defaultWeight"].items():
        lines.append(
            f"| {arm} | {summary['ndcgAtK'][str(primary_k)]:.6f} | {summary['diversity']:.6f} |"
        )
    return "\n".join(lines) + "\n"


def _overreach(
    *,
    reports: dict[float, dict[str, dict[str, Any]]],
    cases: list[GoldenCase],
    fixtures,
    settings: EvaluationSettings,
    config: dict[str, Any],
) -> dict[str, Any]:
    primary_k = _primary_k(config)
    default = reports[DEFAULT_WEIGHT]
    case_by_id = {case.case_id: case for case in cases}
    contradictions: list[dict[str, Any]] = []
    inclusions: list[dict[str, Any]] = []
    guest_rows = {row["caseId"]: row for row in default["guest"]["cases"]}
    for arm_name in ("clean", "noisy", "repeated"):
        for row in default[arm_name]["cases"]:
            case = case_by_id[row["caseId"]]
            axes = explicit_intent_contradictions(case.expected_filters, row["extractedFilters"])
            if axes:
                contradictions.append({"arm": arm_name, "caseId": case.case_id, "axes": axes})
            history = fixtures.purchase_history.get(case.identity.persona_id or "")
            recent = set(
                _recent_ids(
                    history,
                    settings.scoring_reference_date,
                    settings.scoring_recent_purchase_window_days,
                )
            )
            forbidden = set(case.hard_constraints.forbidden_product_ids) | set(
                case.must_exclude_product_ids
            )
            result = new_forbidden_or_recent_inclusions(
                guest_ranked=guest_rows[case.case_id]["rankedProductIds"],
                profile_ranked=row["rankedProductIds"],
                forbidden_ids=forbidden,
                recent_ids=recent,
                repurchase_intent=case_has_repurchase_intent(case),
                top_k=primary_k,
            )
            if result["count"]:
                inclusions.append({"arm": arm_name, "caseId": case.case_id, **result})
    bootstrap = config["bootstrap"]
    noisy_vs_clean = paired_metric_deltas(
        default["noisy"],
        default["clean"],
        k_list=(primary_k,),
        resamples=int(bootstrap["resamples"]),
        confidence=float(bootstrap["confidence"]),
        seed=int(bootstrap["seed"]),
    )
    primary = noisy_vs_clean["overall"]["ndcgAtK"][str(primary_k)]
    clean_noisy = clean_noisy_drop_verdict(
        primary["bootstrapCi95"],
        margin=settings.personalization_eval_clean_noisy_drop_margin,
    )
    return {
        "tierDIntentContradictionInterpretation": (
            "scripted decompose가 expectedFilters를 반환하므로 Tier D에서는 구조적 불변식이며, "
            "#119 전후 회귀 판정 정본은 --live clean_both 결과입니다"
        ),
        "intentContradiction": {
            "count": len(contradictions),
            "max": settings.personalization_eval_intent_contradiction_max,
            "verdict": count_verdict(
                len(contradictions), settings.personalization_eval_intent_contradiction_max
            ),
            "cases": contradictions,
        },
        "forbiddenOrRecentInclusion": {
            "count": sum(row["count"] for row in inclusions),
            "max": settings.personalization_eval_forbidden_inclusion_max,
            "verdict": count_verdict(
                sum(row["count"] for row in inclusions),
                settings.personalization_eval_forbidden_inclusion_max,
            ),
            "cases": inclusions,
        },
        "cleanNoisyDrop": {**clean_noisy, "denominator": primary["denominator"]},
    }


def run_tier_d(output_dir: Path, *, seed: int) -> dict[str, Any]:
    """동일 case/fixture/settings에서 case별 profile arm과 weight만 바꿔 실행한다."""
    config = load_eval_config()
    all_arms = load_profile_arms()
    arms = {name: all_arms[name] for name in config["arms"]}
    cases = sorted(load_cases("dev"), key=lambda case: case.case_id)
    fixtures = load_evaluation_fixtures()
    reports: dict[float, dict[str, dict[str, Any]]] = {}
    command = f"uv run python -m evals.personalization --out {output_dir} --seed {seed}"
    output_dir.mkdir(parents=True, exist_ok=True)
    for weight in config["weightSweep"]:
        reports[weight] = {}
        settings = EvaluationSettings(scoring_weight_profile_match=weight)
        for arm_name, arm in arms.items():
            adapter = ProfileScoringBuyerAdapter(
                profile_preferences=arm.preferences,
                preference_resolver=(
                    lambda case, fixture_set, name=arm_name: derive_case_preferences(
                        name, case, fixture_set
                    )
                ),
                arm_name=arm_name,
                settings=settings,
            )
            report = evaluate(
                adapter=adapter,
                cases=_cases_for_identity(cases, arm.identity_kind),
                fixtures=fixtures,
                config=settings,
            )
            reports[weight][arm_name] = report
            write_artifacts(
                output_dir / arm_name / _weight_dir(weight),
                report,
                _manifest(command=command, seed=seed),
            )

    bootstrap = config["bootstrap"]
    k_list = tuple(EvaluationSettings().eval_buyer_k_list)
    weight_pairs = {
        str(weight): _paired_comparisons(arm_reports, k_list=k_list, bootstrap=bootstrap)
        for weight, arm_reports in reports.items()
    }
    cells = []
    violation_count = 0
    for weight, arm_reports in reports.items():
        for arm_name, report in arm_reports.items():
            count = len(report["violations"])
            violation_count += count
            cells.append({"arm": arm_name, "weight": weight, "violationCount": count})
    eval_settings = EvaluationSettings()
    comparison = {
        "primaryComparison": "clean_vs_member_no_profile",
        "defaultWeight": {
            arm_name: report["overall"] for arm_name, report in reports[DEFAULT_WEIGHT].items()
        },
        "armModelConfig": {
            arm_name: report["modelConfig"] for arm_name, report in reports[DEFAULT_WEIGHT].items()
        },
        "pairedComparisons": weight_pairs[str(DEFAULT_WEIGHT)],
        "weightPairedComparisons": weight_pairs,
        "weightAblation": {
            str(weight): {
                arm_name: {
                    "ndcgAtK": report["overall"]["ndcgAtK"],
                    "diversity": report["overall"]["diversity"],
                }
                for arm_name, report in arm_reports.items()
            }
            for weight, arm_reports in reports.items()
        },
        "hardFilter": {
            "violationCount": violation_count,
            "max": eval_settings.personalization_eval_hard_filter_violation_max,
            "verdict": count_verdict(
                violation_count, eval_settings.personalization_eval_hard_filter_violation_max
            ),
            "cells": cells,
        },
    }
    overreach = _overreach(
        reports=reports,
        cases=cases,
        fixtures=fixtures,
        settings=eval_settings,
        config=config,
    )
    _write_json(output_dir / "comparison.json", comparison)
    (output_dir / "comparison.md").write_text(
        _comparison_markdown(comparison, primary_k=_primary_k(config)),
        encoding="utf-8",
        newline="\n",
    )
    _write_json(output_dir / "overreach.json", overreach)
    manifest = _manifest(command=command, seed=seed)
    manifest.update(
        {
            "datasetHash": fixtures.manifest["datasetHash"],
            "configVersion": config["configVersion"],
            "arms": list(arms),
            "weightSweep": config["weightSweep"],
            "primaryMetric": config["primaryMetric"],
            "profilePreferenceDerivation": config["profilePreferenceDerivation"],
            "counterfactualConditions": {
                "caseOrder": "caseId-asc",
                "catalog": "shared",
                "searchFixtures": "shared",
                "referenceDate": eval_settings.scoring_reference_date,
                "seed": seed,
            },
        }
    )
    _write_json(output_dir / "run_manifest.json", manifest)
    return {"comparison": comparison, "overreach": overreach}


def normalize_paired_artifacts(output_dir: Path) -> dict[str, bytes]:
    """Tier D arm/weight artifact에서 실행 인스턴스·워킹트리 상태 축(run·commitSha·dirty)을
    제거한다."""
    normalized: dict[str, bytes] = {}
    for arm_dir in sorted(path for path in output_dir.iterdir() if path.is_dir()):
        for weight_dir in sorted(path for path in arm_dir.iterdir() if path.is_dir()):
            for name, content in normalize_artifacts(weight_dir).items():
                normalized[f"{arm_dir.name}/{weight_dir.name}/{name}"] = content
    for name in ("comparison.json", "comparison.md", "overreach.json"):
        normalized[name] = (output_dir / name).read_bytes()
    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    normalized["run_manifest.json"] = (
        _json_text(strip_volatile_manifest_keys(manifest)) + "\n"
    ).encode()
    return normalized


def _live_metric_report(results: dict[str, Any], *, k_list: tuple[int, ...]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in results["caseResults"]:
        if isinstance(row.get("metrics"), dict):
            grouped.setdefault(row["caseId"], []).append(row["metrics"])
    cases = []
    for case_id, rows in sorted(grouped.items()):
        first = rows[0]
        ndcg: dict[str, float | None] = {}
        for k in k_list:
            values = [
                float(row["metrics"]["ndcgAtK"][str(k)])
                for row in rows
                if row["metrics"]["ndcgAtK"][str(k)] is not None
            ]
            ndcg[str(k)] = statistics.fmean(values) if values else None
        cases.append(
            {
                **first,
                "metrics": {
                    **first["metrics"],
                    "ndcgAtK": ndcg,
                },
                "diversity": statistics.fmean(float(row["diversity"]) for row in rows),
            }
        )
    return {"datasetHash": results["datasetHash"], "kList": list(k_list), "cases": cases}


def _ranking_change_markdown(
    ranking_change_by_arm: dict[str, dict[str, Any]], *, baseline: str
) -> str:
    """[#482] 활성화 지표를 사람이 읽는 표로. 값이 없으면 절을 아예 만들지 않는다."""
    if not ranking_change_by_arm:
        return ""
    lines = [
        "",
        f"## 활성화 (Δranking rate, 기준선 `{baseline}`)",
        "",
        "| arm | paired | 동일 | 순서만 | 집합변경 | 양쪽 빈 노출 | Δranking rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, value in ranking_change_by_arm.items():
        rate = value["changeRate"]
        # 짝이 0이면 비율이 정의되지 않는다 — 0.0% 로 찍으면 "안 바뀌었다"로 오독된다.
        rate_text = "n/a" if rate is None else f"{rate * 100:.1f}%"
        lines.append(
            f"| `{arm}` | {value['pairedCount']} | {value['same']} | {value['orderOnly']} "
            f"| {value['setChanged']} | {value['bothEmpty']} | {rate_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def _live_arm_spec(name: str, clean_markdown: str) -> tuple[str | None, str, str]:
    if name == "guest":
        return None, "guest", "off"
    if name == "clean_rerank_only":
        return clean_markdown, "member", "rerank_only"
    if name == "clean_both":
        return clean_markdown, "member", "both"
    raise ValueError(f"알 수 없는 live arm: {name}")


def run_tier_l(
    output_dir: Path,
    *,
    seed: int,
    repeats: int,
    arm_names: list[str],
    cases: list[GoldenCase],
    runtime_settings: Settings,
) -> dict[str, Any]:
    """예산 gate 뒤 실제 provider를 세 scope arm으로 paired 실행하고 영속한다."""
    config = load_eval_config()
    fixtures = load_evaluation_fixtures()
    limits = BudgetLimits(
        max_calls=runtime_settings.model_eval_max_calls_per_run,
        max_total_tokens=runtime_settings.model_eval_max_total_tokens_per_run,
        max_cost_usd=runtime_settings.model_eval_max_cost_usd_per_run,
    )
    budget = BudgetTracker(limits)
    pricing = PriceBook.load()
    base = build_live_adapter(runtime_settings=runtime_settings, budget=budget, pricing=pricing)
    clean_markdown = load_profile_arms()["clean"].markdown or ""
    results_by_arm: dict[str, dict[str, Any]] = {}
    reports: dict[str, dict[str, Any]] = {}
    k_list = tuple(EvaluationSettings().eval_buyer_k_list)
    command = f"uv run python -m evals.personalization --live --out {output_dir}"
    output_dir.mkdir(parents=True, exist_ok=False)
    for arm_name in arm_names:
        markdown, identity_kind, scope = _live_arm_spec(arm_name, clean_markdown)
        settings = base.settings.model_copy(update={"profile_injection_scope": scope})
        adapter = PersonalizationLiveBuyerAdapter(
            base.llm,
            profile_markdown=markdown,
            identity_kind=identity_kind,
            settings=settings,
            model_config=base.model_config,
        )
        arm_cases = _cases_for_identity(cases, identity_kind)
        result = run_repeats(
            adapter=adapter,
            cases=arm_cases,
            fixtures=fixtures,
            repeats=repeats,
            budget=budget,
            primary_metric=config["primaryMetric"],
            resamples=int(config["bootstrap"]["resamples"]),
            confidence=float(config["bootstrap"]["confidence"]),
            seed=seed,
        )
        result.update(
            {
                "datasetVersion": fixtures.manifest["datasetVersion"],
                "datasetHash": fixtures.manifest["datasetHash"],
                "configVersion": config["configVersion"],
                "coverage": pricing.coverage(
                    [call for row in result["caseResults"] for call in row.get("providerCalls", [])]
                ),
            }
        )
        results_by_arm[arm_name] = result
        reports[arm_name] = _live_metric_report(result, k_list=k_list)

    guest_filters = {
        row["caseId"]: row.get("extractedFilters", {})
        for row in results_by_arm["guest"]["caseResults"]
        if row.get("repeat") == 0
    }
    case_by_id = {case.case_id: case for case in cases}
    for arm_name, result in results_by_arm.items():
        for row in result["caseResults"]:
            expected = case_by_id[row["caseId"]].expected_filters
            row["filterAxisLeakage"] = filter_axis_leakage(
                guest_filters.get(row["caseId"], {}), row.get("extractedFilters", {})
            )
            row["intentContradictionAxes"] = explicit_intent_contradictions(
                expected, row.get("extractedFilters", {})
            )
        manifest = _manifest(command=command, seed=seed)
        manifest["live"] = {
            "arm": arm_name,
            "caseIds": [case.case_id for case in cases],
            "repeats": repeats,
            "budget": budget.snapshot(),
            "coverage": result["coverage"],
            "modelConfig": result["caseResults"][0]["modelConfig"] if result["caseResults"] else {},
        }
        write_live_artifacts(
            output_dir / arm_name,
            results=result,
            manifest=manifest,
            regression=None,
        )

    bootstrap = config["bootstrap"]
    paired = {
        arm_name: paired_metric_deltas(
            reports[arm_name],
            reports[LIVE_BASELINE_ARM],
            k_list=k_list,
            resamples=int(bootstrap["resamples"]),
            confidence=float(bootstrap["confidence"]),
            seed=seed,
        )
        for arm_name in arm_names
        if arm_name != LIVE_BASELINE_ARM
    }
    # [#482] 활성화 지표 — 프로필이 노출을 **실제로 바꿨는가**. 이득 지표(위 `paired`)만으로는
    # "아무것도 안 바꿔서 0" 과 "바꿨는데 좋지 않아서 0" 이 구분되지 않는데, 두 상태의 처방이
    # 정반대다(`activation` 모듈 docstring). 기준선은 paired 와 **같은 arm** 을 쓴다 — 두 지표가
    # 다른 기준을 보면 같은 표에서 읽을 수 없다.
    ranking_change_by_arm = {
        arm_name: ranking_change(
            results_by_arm[LIVE_BASELINE_ARM]["caseResults"],
            results_by_arm[arm_name]["caseResults"],
        )
        for arm_name in arm_names
        if arm_name != LIVE_BASELINE_ARM
    }
    comparison = {
        "primaryMetric": config["primaryMetric"],
        "baselineArm": LIVE_BASELINE_ARM,
        "pairedVsGuest": paired,
        "rankingChange": ranking_change_by_arm,
        "budget": budget.snapshot(),
        "coverage": {arm: result["coverage"] for arm, result in results_by_arm.items()},
        "axisLeakage": {
            arm: [
                {
                    "caseId": row["caseId"],
                    "repeat": row["repeat"],
                    "axes": row["filterAxisLeakage"],
                }
                for row in result["caseResults"]
                if row["filterAxisLeakage"]
            ]
            for arm, result in results_by_arm.items()
        },
        "intentContradictions": {
            arm: [
                {
                    "caseId": row["caseId"],
                    "repeat": row["repeat"],
                    "axes": row["intentContradictionAxes"],
                }
                for row in result["caseResults"]
                if row["intentContradictionAxes"]
            ]
            for arm, result in results_by_arm.items()
        },
    }
    _write_json(output_dir / "comparison.json", comparison)
    (output_dir / "comparison.md").write_text(
        "# Tier L #119 scope paired comparison\n\n"
        "- guest: (decompose=None, rerank=None)\n"
        "- clean_rerank_only: (decompose=None, rerank=clean)\n"
        "- clean_both: (decompose=clean, rerank=clean)\n"
        + _ranking_change_markdown(ranking_change_by_arm, baseline=LIVE_BASELINE_ARM),
        encoding="utf-8",
        newline="\n",
    )
    top_manifest = _manifest(command=command, seed=seed)
    top_manifest["live"] = {
        "arms": arm_names,
        "caseIds": [case.case_id for case in cases],
        "repeats": repeats,
        "budget": budget.snapshot(),
    }
    _write_json(output_dir / "run_manifest.json", top_manifest)
    return comparison


def normalize_live_artifacts(output_dir: Path) -> dict[str, bytes]:
    """Tier L 산출물에서 run/commitSha/dirty(실행 인스턴스·워킹트리 상태 축)와
    latency/correlation ID를 제거한다."""
    normalized: dict[str, bytes] = {}
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(output_dir))
        if path.name.endswith(".json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if path.name == "run_manifest.json":
                payload = strip_volatile_manifest_keys(payload)
            if path.name == "results.json":
                for row in payload.get("caseResults", []):
                    row.pop("latencyMs", None)
                    for call in row.get("providerCalls", []):
                        call.pop("correlationId", None)
                        call.pop("latencyMs", None)
            normalized[relative] = (_json_text(payload) + "\n").encode()
        elif path.suffix != ".csv":
            normalized[relative] = path.read_bytes()
    return normalized


def _parse_arms(value: str | None) -> list[str]:
    arms = (
        list(LIVE_ARMS)
        if value is None
        else [item.strip() for item in value.split(",") if item.strip()]
    )
    if (
        not arms
        or arms[0] != "guest"
        or len(arms) != len(set(arms))
        or any(arm not in LIVE_ARMS for arm in arms)
    ):
        raise ValueError(f"--arms는 guest로 시작하는 고유 목록이어야 합니다: {LIVE_ARMS}")
    return arms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="개인화 효과·과반영 평가")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--arms")
    parser.add_argument("--case-limit", type=int)
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args(argv)
    try:
        config = load_eval_config()
        _primary_k(config)
        arm_names = _parse_arms(args.arms)
    except ValueError as exc:
        print(str(exc))
        return 2
    if args.repeats < 1 or (args.case_limit is not None and args.case_limit < 1):
        print("--repeats와 --case-limit은 1 이상이어야 합니다")
        return 2
    cases = sorted(load_cases("dev"), key=lambda case: case.case_id)
    if args.case_limit is not None:
        cases = cases[: args.case_limit]
    runtime_settings = get_settings()
    if args.dry_run or args.live:
        table = estimate_live_matrix(
            case_count=len(cases),
            arm_count=len(arm_names),
            repeats=args.repeats,
            model=args.model,
            settings=runtime_settings,
        )
        print(_json_text(table))
        if not table["allowed"]:
            return 2
        if args.dry_run:
            return 0
    if args.live:
        if args.out.exists():
            print(f"출력 디렉터리가 이미 존재합니다: {args.out}")
            return 2
        try:
            run_tier_l(
                args.out,
                seed=args.seed,
                repeats=args.repeats,
                arm_names=arm_names,
                cases=cases,
                runtime_settings=runtime_settings,
            )
        except Exception as exc:  # provider 미설정도 실행 전 명시 실패
            print(f"Tier L 실행 오류: {exc}")
            return 2
        return 0
    result = run_tier_d(args.out, seed=args.seed)
    hard = result["comparison"]["hardFilter"]
    print(
        f"arms={len(config['arms'])} weights={len(config['weightSweep'])} "
        f"cells={len(hard['cells'])} violations={hard['violationCount']} out={args.out}"
    )
    return 0
