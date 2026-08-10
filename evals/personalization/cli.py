"""개인화 Tier D 5-arm ablation과 Tier L scope paired 평가 CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import GoldenCase, Identity
from evals.metrics.report import normalize_artifacts, write_artifacts
from evals.metrics.run_manifest import build_run_manifest, strip_volatile_manifest_keys
from evals.metrics.runner import EvaluationFixtures, evaluate, load_evaluation_fixtures
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
from evals.personalization.profile_markdown import (
    MARKDOWN_RENDER_VERSION,
    render_profile_markdown,
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
# `clean_fixed` 는 #484 이전의 케이스 무관 고정 프로필을 그대로 돌리는 회귀 대조군이다.
# 허용 목록에만 두고 기본 실행에는 넣지 않는다 — 효과를 잴 때만 --arms 로 명시해 돌린다.
LIVE_ARMS = ("guest", "member_no_profile", "clean_rerank_only", "clean_both", "clean_fixed")
# [#483] 기본 실행 arm. 두 기준선을 **앞에** 둔다 — `run_repeats` 는 예산이 소진되면 뒤 arm 을
# 통째로 남기지 못하므로, 기준선이 먼저 확보돼야 부분 산출물에서도 비교가 성립한다.
DEFAULT_LIVE_ARMS = ("guest", "member_no_profile", "clean_rerank_only", "clean_both")
# 선호가 비지 않은 케이스에만 붙는 슬라이스. dev 109건 중 35건(32%)은 후보에 grade>=2 정답이
# 없어 프로필이 비는데, 그 케이스까지 섞은 평균은 오라클 천장을 0쪽으로 희석한다.
PROFILE_SIGNAL_SLICE = "profile_signal"
_LIVE_ARM_DESCRIPTIONS = {
    "guest": "guest: (decompose=None, rerank=None, identity=guest) — cold-start 보조 기준선",
    "member_no_profile": (
        "member_no_profile: (decompose=None, rerank=None, identity=member) — 주 기준선"
    ),
    "clean_rerank_only": "clean_rerank_only: (decompose=None, rerank=케이스별 clean)",
    "clean_both": "clean_both: (decompose=케이스별 clean, rerank=케이스별 clean)",
    "clean_fixed": "clean_fixed: (decompose=None, rerank=고정 픽스처) — #484 이전 방식 대조군",
}
# [#483] Tier L paired 비교의 **주** 기준선. 이득(`pairedVsMemberNoProfile`)·활성화
# (`rankingChange`)·축 유출(`axisLeakage`)이 **같은 기준선**을 보게 강제하려고 상수로 둔다 —
# 세 지표가 서로 다른 arm 을 기준으로 잡히면 같은 표에서 읽을 수 없다.
# `guest` 였을 때의 문제: 프로필뿐 아니라 identity 까지 달라(persona_id 가 없어 I-19 구매이력
# 조회·재구매 dedup 이 통째로 빠진다) 헤드라인이 "프로필 효과 + identity 효과"의 합이었다.
# 실측으로 갈라 보면 live-v1 헤드라인 하락의 절반 이상이 재구매 3건이 만든 identity 효과였다.
# `member_no_profile` 은 identity 를 비교 대상과 맞추고 **프로필 하나만** 뺀 대조군이다.
LIVE_BASELINE_ARM = "member_no_profile"
# 보조 기준선. `pairedVsGuest` 전용이며 cold-start(비회원 유입) 대비 개선폭을 남겨 두기 위한
# 것이다. identity 효과가 섞이므로 **프로필 효과로 해석하지 않는다**(Tier D dev-v2 README 규약).
LIVE_SECONDARY_BASELINE_ARM = "guest"
# 헤드라인 arm — `primaryComparison` 문자열을 만드는 데만 쓴다(미실행이면 null).
LIVE_PRIMARY_ARM = "clean_rerank_only"
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


def _rows_with_metrics(results: dict[str, Any]) -> list[dict[str, Any]]:
    """지표 계산에 쓸 수 있는 행만 — 이득·활성화가 **같은 행 집합**을 보게 하는 단일 술어.

    `run_repeats` 는 예산이 실행 도중 소진되면 `metrics=None`·`rankedProductIds=[]` 인
    `failureReason="budgetExceeded"` 행을 남긴다. 이 행은 측정 결과가 아니라 **측정이 중단된
    자리**다.

    술어를 한 곳에 두는 이유(PR #485 리뷰): 이득 지표는 이 필터를 거치는데 활성화 지표가
    raw `caseResults` 를 읽으면, 예산이 소진된 그 행이 baseline 의 정상 행과 짝지어져
    `setChanged` 로 잡힌다 — **예산 소진이 프로필 효과로 둔갑한다.** 두 곳에 같은 조건을
    복붙하면 한쪽만 고쳐질 때 같은 어긋남이 조용히 되살아나므로 함수로 고정한다.

    adapter 예외로 생긴 `hardFailure` 행은 **거르지 않는다** — 그쪽은 `evaluate` 가 정상적으로
    metrics 를 산출하므로 두 지표에 똑같이 들어가고, 빈 노출도 실제 산출 결과다.
    """
    return [row for row in results["caseResults"] if isinstance(row.get("metrics"), dict)]


def _live_metric_report(results: dict[str, Any], *, k_list: tuple[int, ...]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _rows_with_metrics(results):
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


def _live_arm_spec(name: str) -> tuple[str | None, str, str]:
    """Tier L arm 이름 → (마크다운 모드, identity_kind, scope).

    모드는 마크다운 문자열이 아니라 **출처**다: 파생 arm 이름("clean")이면 케이스별로 렌더하고,
    "fixed"면 픽스처의 고정 문자열을, None이면 프로필을 주지 않는다.
    """
    if name == "guest":
        return None, "guest", "off"
    if name == "member_no_profile":
        # [#483] guest 와 **identity 하나만** 다르다. 프로필이 없으면 `profile_for_scope` 가 모든
        # scope 에서 (None, None) 을 돌려주므로 scope 값은 실질 no-op 이고, 남는 차이는
        # persona_id 유무 → I-19 구매이력 조회·재구매 dedup 뿐이다.
        return None, "member", "off"
    if name == "clean_rerank_only":
        return "clean", "member", "rerank_only"
    if name == "clean_both":
        return "clean", "member", "both"
    if name == "clean_fixed":
        # live-v1 헤드라인 arm(clean_rerank_only)과 같은 scope 여야 대조가 성립한다.
        return "fixed", "member", "rerank_only"
    raise ValueError(f"알 수 없는 live arm: {name}")


def _case_markdown_resolver(
    derivation_name: str, *, max_chars: int, strength_bands: tuple[float, float]
) -> Callable[[GoldenCase, EvaluationFixtures], str | None]:
    """Tier D 파생 선호를 케이스별 마크다운으로 바꾸는 콜백(순수·LLM 미호출)."""

    def resolve(case: GoldenCase, fixtures: EvaluationFixtures) -> str | None:
        preferences = derive_case_preferences(derivation_name, case, fixtures)
        if preferences is None:
            return None
        return render_profile_markdown(
            preferences, max_chars=max_chars, strength_bands=strength_bands
        )

    return resolve


def _has_profile_signal(
    derivation_name: str, case: GoldenCase, fixtures: EvaluationFixtures
) -> bool:
    preferences = derive_case_preferences(derivation_name, case, fixtures)
    return bool(preferences and (preferences["brands"] or preferences["categories"]))


def _tag_profile_signal_slice(results: dict[str, Any], signal_case_ids: set[str]) -> None:
    """선호가 있는 케이스 행에 슬라이스를 덧붙인다 — 통계는 `paired_metric_deltas` 가 이미 낸다.

    전체 평균 하나만 보면 무신호 32%가 오라클 천장을 0쪽으로 끌어내린다. 별도 집계 코드를
    짜는 대신 기존 slice 축을 쓴다: `pairedVsMemberNoProfile[arm]["slices"]["profile_signal"]`.
    """
    for row in results["caseResults"]:
        metrics = row.get("metrics")
        if not isinstance(metrics, dict) or row["caseId"] not in signal_case_ids:
            continue
        slices = list(metrics.get("slices") or [])
        if PROFILE_SIGNAL_SLICE not in slices:
            metrics["slices"] = [*slices, PROFILE_SIGNAL_SLICE]


def annotate_axis_metrics(
    results_by_arm: dict[str, dict[str, Any]],
    *,
    baseline_arm: str,
    expected_filters_by_case: dict[str, dict[str, Any]],
) -> None:
    """[#483] 각 arm 행에 축 유출·의도 모순을 붙인다(제자리 변경, LLM 미호출 순수 함수).

    **짝짓기 키가 `(caseId, repeat)` 인 이유**: 기준선을 repeat 0 한 벌로 고정하면 `--repeats>1`
    에서 arm 의 repeat 1·2 가 기준선의 *다른 샘플* 과 비교돼 LLM 지터가 프로필 유출로 계상된다.
    같은 함정을 `activation._by_pair_key` 가 이미 명시해 뒀다 — 두 지표가 같은 규약을 쓴다.

    측정되지 않은 행 — 기준선에 짝이 없거나 이 행 자신이 예산 소진 스텁인 경우 — 은 `[]` 가
    아니라 `None` 이다. `[]` 는 "유출 없음"으로 읽혀 **측정 중단이 안전 신호로 둔갑**한다.
    미측정 행은 `comparison.json` 의 `axisLeakageUnmeasured` 로 드러난다. 의도 모순도 같은
    행에서 `None` 이 되므로 그 목록이 두 지표의 공백을 함께 가리킨다.

    **기준선에서 예산 소진 행을 걸러내는 이유**(PR #536 리뷰): `run_repeats` 는 예산이 소진된
    자리에 행을 없애는 게 아니라 `extractedFilters={}` 인 스텁을 남긴다(`repeats.py:108-120`).
    이 행을 그대로 두면 키가 존재하므로 짝이 **있는 것으로 잡히고**, `{}` 가 "필터 없음"으로
    읽혀 비교 arm 이 추출한 축이 전부 유출로 오탐된다. 예산은 arm 전체에 걸쳐 누적 공유되므로
    앞 arm 이 정상 측정을 끝낸 뒤 기준선이 뒤늦게 소진되는 조합은 실제로 도달 가능하다.
    술어는 `_rows_with_metrics` 와 같은 것을 쓴다 — 두 곳이 갈리면 조용히 어긋난다.
    """
    baseline_filters = {
        (str(row["caseId"]), int(row.get("repeat", 0))): row.get("extractedFilters") or {}
        for row in results_by_arm[baseline_arm]["caseResults"]
        if isinstance(row.get("metrics"), dict)
    }
    for result in results_by_arm.values():
        for row in result["caseResults"]:
            # 기준선뿐 아니라 **이 행 자신**도 측정됐는지 본다. 스텁의 `extractedFilters` 는 `{}`
            # 라 `arm_axes - baseline_axes` 가 늘 비고, 그러면 `[]`(유출 없음)로 기록된다. 게다가
            # `[]` 는 `axisLeakage` 에서 falsy 로 빠지고 `None` 이 아니라 `axisLeakageUnmeasured`
            # 에도 안 잡혀 **두 목록 어디에도 남지 않는다**. `DEFAULT_LIVE_ARMS` 가 기준선을 앞에
            # 두므로 예산 소진 시 기준선은 완주하고 뒤 arm 만 잘리는 이 조합이 오히려 기본형이다.
            measured = isinstance(row.get("metrics"), dict)
            extracted = row.get("extractedFilters") or {}
            key = (str(row["caseId"]), int(row.get("repeat", 0)))
            row["filterAxisLeakage"] = (
                filter_axis_leakage(baseline_filters[key], extracted)
                if measured and key in baseline_filters
                else None
            )
            # 빈 `extracted` 는 "모순 없음"으로 계산되므로 같은 둔갑이 일어난다.
            row["intentContradictionAxes"] = (
                explicit_intent_contradictions(expected_filters_by_case[row["caseId"]], extracted)
                if measured
                else None
            )


def build_live_comparison(
    *,
    arm_names: list[str],
    reports: dict[str, dict[str, Any]],
    results_by_arm: dict[str, dict[str, Any]],
    primary_metric: str,
    k_list: tuple[int, ...],
    bootstrap: dict[str, Any],
    seed: int,
    budget_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """[#483] Tier L comparison.json 을 조립한다(순수 함수 — LLM·파일 접근 없음).

    **두 벌의 paired 를 내는 이유.** 주 비교(`pairedVsMemberNoProfile`)는 identity 를 맞추고
    프로필만 뺀 대조군과의 차이라 **프로필 순효과**로 읽을 수 있다. 보조 비교
    (`pairedVsGuest`)는 identity 까지 다르므로 프로필 효과로 해석하면 안 되지만, 비회원 유입
    대비 개선폭이라는 별개의 질문에 답하므로 남긴다(Tier D dev-v2 README 와 같은 규약).

    활성화(`rankingChange`)·축 유출은 **주 기준선 하나만** 쓴다 — 이득과 다른 기준을 보면 같은
    표에서 읽을 수 없다. 행 집합도 `_rows_with_metrics` 로 맞춘다: raw `caseResults` 를 읽으면
    예산 소진 행이 baseline 정상 행과 짝지어져 프로필 효과로 둔갑한다.
    """

    def paired_against(baseline_arm: str) -> dict[str, Any]:
        return {
            arm_name: paired_metric_deltas(
                reports[arm_name],
                reports[baseline_arm],
                k_list=k_list,
                resamples=int(bootstrap["resamples"]),
                confidence=float(bootstrap["confidence"]),
                seed=seed,
            )
            for arm_name in arm_names
            if arm_name != baseline_arm
        }

    def rows_by_arm(key: str) -> dict[str, list[dict[str, Any]]]:
        return {
            arm: [
                {"caseId": row["caseId"], "repeat": row["repeat"], "axes": row[key]}
                for row in result["caseResults"]
                if row[key]
            ]
            for arm, result in results_by_arm.items()
        }

    def unmeasured_rows_by_arm(key: str) -> dict[str, list[dict[str, Any]]]:
        """[PR #536 리뷰] 측정 못 한 행을 **별도 키로** 낸다.

        `None`(측정 못 함)과 `[]`(유출 없음)은 둘 다 falsy 라 위 목록에서 똑같이 빠진다. 그렇다고
        같은 목록에 `axes: null` 로 섞으면 `len(axisLeakage[arm])` 으로 유출 건수를 세던 쪽이
        **측정 실패를 유출로 집계**한다. 목록의 의미를 지키면서 공백을 드러내려면 키를 나눠야 한다.
        """
        return {
            arm: [
                {"caseId": row["caseId"], "repeat": row["repeat"]}
                for row in result["caseResults"]
                if row[key] is None
            ]
            for arm, result in results_by_arm.items()
        }

    return {
        "primaryMetric": primary_metric,
        "baselineArm": LIVE_BASELINE_ARM,
        "secondaryBaselineArm": LIVE_SECONDARY_BASELINE_ARM,
        # 헤드라인 arm 을 안 돌렸으면 이름만 남겨 두지 않는다 — 없는 수치를 가리키게 된다.
        "primaryComparison": (
            f"{LIVE_PRIMARY_ARM}_vs_{LIVE_BASELINE_ARM}" if LIVE_PRIMARY_ARM in arm_names else None
        ),
        "pairedVsMemberNoProfile": paired_against(LIVE_BASELINE_ARM),
        "pairedVsGuest": paired_against(LIVE_SECONDARY_BASELINE_ARM),
        "rankingChange": {
            arm_name: ranking_change(
                _rows_with_metrics(results_by_arm[LIVE_BASELINE_ARM]),
                _rows_with_metrics(results_by_arm[arm_name]),
            )
            for arm_name in arm_names
            if arm_name != LIVE_BASELINE_ARM
        },
        "budget": budget_snapshot,
        "coverage": {arm: result["coverage"] for arm, result in results_by_arm.items()},
        "axisLeakage": rows_by_arm("filterAxisLeakage"),
        # 기준선 짝이 없어 유출을 **재지 못한** 행. `axisLeakage` 가 비어 있다고 "유출이 없었다"로
        # 읽으면 안 된다는 신호다 — 그 판단은 이 목록이 빈 경우에만 성립한다.
        "axisLeakageUnmeasured": unmeasured_rows_by_arm("filterAxisLeakage"),
        "intentContradictions": rows_by_arm("intentContradictionAxes"),
    }


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
    # 렌더 파라미터는 runtime_settings(=env 반영)가 아니라 평가 정본에서 읽는다 — 프로필 문자열이
    # 실행 환경에 따라 달라지면 baseline 간 비교가 무의미해진다(k_list 와 같은 규약).
    eval_settings = EvaluationSettings()
    k_list = tuple(eval_settings.eval_buyer_k_list)
    command = f"uv run python -m evals.personalization --live --out {output_dir}"
    output_dir.mkdir(parents=True, exist_ok=False)
    arm_markdown_mode: dict[str, str | None] = {}
    # 슬라이스는 **케이스 속성**이라 arm 전체에 동일해야 한다 — arm 마다 다르면
    # `stats._validate_pair` 가 paired 비교를 거부한다. arm 이름과 무관하게 한 번만 센다.
    signal_case_ids = {
        case.case_id for case in cases if _has_profile_signal("clean", case, fixtures)
    }
    empty_profile_case_ids = sorted(
        case.case_id for case in cases if case.case_id not in signal_case_ids
    )
    for arm_name in arm_names:
        markdown_mode, identity_kind, scope = _live_arm_spec(arm_name)
        arm_markdown_mode[arm_name] = markdown_mode
        case_derived = markdown_mode not in (None, "fixed")
        settings = base.settings.model_copy(update={"profile_injection_scope": scope})
        adapter = PersonalizationLiveBuyerAdapter(
            base.llm,
            profile_markdown=clean_markdown if markdown_mode == "fixed" else None,
            markdown_resolver=(
                _case_markdown_resolver(
                    markdown_mode,
                    max_chars=eval_settings.profile_summary_max_chars,
                    strength_bands=eval_settings.personalization_eval_profile_strength_bands,
                )
                if case_derived
                else None
            ),
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
        _tag_profile_signal_slice(result, signal_case_ids)
        results_by_arm[arm_name] = result
        reports[arm_name] = _live_metric_report(result, k_list=k_list)

    annotate_axis_metrics(
        results_by_arm,
        baseline_arm=LIVE_BASELINE_ARM,
        expected_filters_by_case={case.case_id: case.expected_filters for case in cases},
    )
    for arm_name, result in results_by_arm.items():
        manifest = _manifest(command=command, seed=seed)
        manifest["live"] = {
            "arm": arm_name,
            "caseIds": [case.case_id for case in cases],
            "repeats": repeats,
            "budget": budget.snapshot(),
            "coverage": result["coverage"],
            "modelConfig": result["caseResults"][0]["modelConfig"] if result["caseResults"] else {},
            "profileMarkdownSource": arm_markdown_mode[arm_name] or "none",
            "profileMarkdownRenderVersion": MARKDOWN_RENDER_VERSION,
            "emptyProfileCaseIds": empty_profile_case_ids,
        }
        write_live_artifacts(
            output_dir / arm_name,
            results=result,
            manifest=manifest,
            regression=None,
        )

    comparison = build_live_comparison(
        arm_names=arm_names,
        reports=reports,
        results_by_arm=results_by_arm,
        primary_metric=config["primaryMetric"],
        k_list=k_list,
        bootstrap=config["bootstrap"],
        seed=seed,
        budget_snapshot=budget.snapshot(),
    )
    _write_json(output_dir / "comparison.json", comparison)
    (output_dir / "comparison.md").write_text(
        "# Tier L #119 scope paired comparison\n\n"
        + "".join(f"- {_LIVE_ARM_DESCRIPTIONS[arm]}\n" for arm in arm_names)
        + f"\n- 주 비교: `{LIVE_PRIMARY_ARM}_vs_{LIVE_BASELINE_ARM}` "
        f"(`pairedVsMemberNoProfile`) — identity·최근구매 조건이 같고 profile만 다르다.\n"
        f"- `pairedVsGuest`는 identity·최근구매가 섞인 cold-start 보조 비교이며 "
        f"**profile 효과로 해석하지 않는다**.\n"
        f"- identity 효과 자체는 `pairedVsGuest['{LIVE_BASELINE_ARM}']`, 그리고 각 비교의 "
        f"`slices.member`·`slices.repurchase`로 갈라 볼 수 있다.\n"
        + f"\n프로필 신호가 있는 케이스 {len(signal_case_ids)}건은 `{PROFILE_SIGNAL_SLICE}` "
        f"슬라이스로 따로 집계된다(무신호 {len(empty_profile_case_ids)}건 제외).\n"
        + _ranking_change_markdown(comparison["rankingChange"], baseline=LIVE_BASELINE_ARM),
        encoding="utf-8",
        newline="\n",
    )
    top_manifest = _manifest(command=command, seed=seed)
    top_manifest["live"] = {
        "arms": arm_names,
        "caseIds": [case.case_id for case in cases],
        "repeats": repeats,
        "budget": budget.snapshot(),
        "profileMarkdownRenderVersion": MARKDOWN_RENDER_VERSION,
        "profileSignalCaseCount": len(signal_case_ids),
        "emptyProfileCaseIds": empty_profile_case_ids,
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
        list(DEFAULT_LIVE_ARMS)
        if value is None
        else [item.strip() for item in value.split(",") if item.strip()]
    )
    # [#483] 위치가 아니라 **포함 여부**를 본다. 옛 `arms[0] == "guest"` 규칙은 기준선을 이름으로
    # 조회하지 않고 `results_by_arm["guest"]` 로 직접 인덱싱하던 코드를 떠받치던 암묵적 결합이라,
    # 기준선을 상수로 뺀 지금은 근거가 없다. 대신 두 기준선이 실행 arm 에 실제로 들어 있는지를
    # 검사한다 — 없으면 paired 비교가 KeyError 로 죽거나 조용히 비어 버린다.
    required = {LIVE_BASELINE_ARM, LIVE_SECONDARY_BASELINE_ARM}
    if (
        not arms
        or required - set(arms)
        or len(arms) != len(set(arms))
        or any(arm not in LIVE_ARMS for arm in arms)
    ):
        raise ValueError(
            f"--arms는 {sorted(required)}를 모두 포함한 고유 목록이어야 합니다: {LIVE_ARMS}"
        )
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
