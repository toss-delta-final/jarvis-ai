"""과소지정 판정 축 실 LLM 프로브 CLI — 수동 실행 도구다. CI 에 넣지 않는다 (#380).

uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06 --tier fast
uv run python -m evals.underspecified_probe --out /tmp/probe --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from evals.intent_probe.client import (
    PacedLLM,
    build_live_delegate,
    build_probe_llm,
    resolve_system_prompt,
)
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.model_eval.budget import BudgetLimits, BudgetTracker
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook
from evals.underspecified_probe.fakes import ScriptedDecomposeLLM
from evals.underspecified_probe.loader import (
    build_cells,
    fixture_sha256,
    load_anchor_set,
    resolve_fixture_path,
)
from evals.underspecified_probe.manifest import build_underspecified_probe_manifest
from evals.underspecified_probe.metrics import (
    cause_axis_summary,
    compute_baseline,
    diagnostics,
    sample_rows,
    score_all,
)
from evals.underspecified_probe.report import build_results, write_artifacts
from evals.underspecified_probe.runner import JudgmentSettings, run_probe, unfilled_cells
from evals.underspecified_probe.union import (
    UnionPreflightError,
    preflight_catalog,
    union_tunable_report,
)

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_BUDGET = 3
EXIT_UNFILLED = 4

DRY_RUN_MODEL_CONFIG = {
    "provider": "dry-run",
    "fastModel": "scripted",
    "smartModel": "scripted",
    "fastReasoningEffort": None,
    "smartReasoningEffort": None,
    "timeoutS": None,
    "maxRetries": 0,
}

# [§D6] 판정용 settings 는 `.env` 와 무관한 고정값이다 — 이 프로브의 목적 자체가 "플래그가
# **켜졌다고 가정했을 때** 판정이 실제로 얼마나 정확한가"를 재는 것이므로, 판정은 항상
# `underspecified_reask_enabled=True` 로 고정한다(Settings 기본값 False 를 그대로 쓰면 모든
# 표본이 구조적으로 판정 False 가 되어 하네스가 무의미해진다). D9 `flagOffInvariant` 만 별도로
# False 재판정을 수행해 게이트 자체의 동작을 검증한다.
JUDGMENT_SETTINGS = JudgmentSettings(underspecified_reask_enabled=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="과소지정 판정 축 실 LLM 프로브 (#380)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", help="외부 앵커 JSON 경로 — 생략하면 committed anchors.json")
    parser.add_argument("--tier", choices=("fast", "smart"), default="fast")
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt", type=Path, help="후보 decompose 프롬프트 파일")
    prompt_source.add_argument("--prompt-rev", help="과거 판 _SYSTEM 을 읽을 git 리비전")
    parser.add_argument("--n", type=int, default=8, help="셀당 성공 표본 수")
    parser.add_argument(
        "--arm", choices=("before", "postprocess", "dedicated", "tri"), default="before"
    )
    attr_axis = parser.add_mutually_exclusive_group()
    attr_axis.add_argument(
        "--attr-axis-suppression",
        dest="attr_axis_suppression",
        action="store_true",
        help="#464 attrConditions 제약 축 후처리 켜기(설정 기본값 덮어쓰기)",
    )
    attr_axis.add_argument(
        "--no-attr-axis-suppression",
        dest="attr_axis_suppression",
        action="store_false",
        help="#464 attrConditions 제약 축 후처리 끄기",
    )
    parser.set_defaults(attr_axis_suppression=None)
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument(
        "--budget-usd", type=float, default=5.0, help="model_eval BudgetTracker 상한"
    )
    parser.add_argument("--dry-run", action="store_true", help="가짜 LLM — API 를 부르지 않는다")
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument(
        "--union",
        action="store_true",
        help="[#432] 기본 off — 카테고리 매핑·needs_expansion 보정 후 판정까지 실제로 태워 잰다"
        "(pgvector·추가 LLM 콜 필요, --dry-run 이면 pg 접근 0으로 배관만 확인한다)",
    )
    return parser


def _virtual_pacer(limits: PacerLimits) -> GlobalPacer:
    """dry-run 전용 — 페이싱 로직은 그대로 돌리되 실제로 자지 않는다(산출물도 결정론)."""
    state = {"now": 0.0}

    async def _sleep(seconds: float) -> None:
        state["now"] += seconds

    return GlobalPacer(limits, clock=lambda: state["now"], sleep=_sleep)


async def _no_sleep(seconds: float) -> None:
    return None


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.underspecified_probe " + " ".join(
        shlex.quote(arg) for arg in argv
    )


def _resolve_attr_axis_suppression(requested: bool | None, settings: Settings) -> bool:
    """명시 플래그가 없으면 프로브가 사용할 Settings 값을 그대로 따른다."""
    return settings.attr_condition_axis_suppression_enabled if requested is None else requested


def union_extra_calls_per_sample(category_select_max_calls: int) -> int:
    """[F-3, 리뷰 findings-432-r1] union 단계(카테고리 택일 + 전개)가 표본 1건당 추가로 부를 수
    있는 LLM 콜 상한 — `category_select_max_calls`(원 매핑 + 재매핑의 택일 합, §4.4 — 재매핑
    호출은 `select_max_calls=max(0, category_select_max_calls - mapping.select_calls)` 로
    첫 매핑이 쓴 몫을 빼고 넘겨받으므로 **두 배가 아니라 합쳐서** 이 값이 상한이다) + 전개
    LLM 1회(`expand_needs` 는 트리거당 최대 1콜)."""
    return category_select_max_calls + 1


def max_llm_calls(
    *,
    expected_calls: int,
    attempt_multiplier: int,
    union_enabled: bool,
    category_select_max_calls: int,
) -> int:
    """[F-3] decompose 재시도 상한(`expected_calls * attempt_multiplier`)에 union 단계가 표본당
    추가로 부를 수 있는 콜 수를 더한다. 반영하지 않으면 union 호출이 `BudgetTracker` 를 영구
    exceeded 로 만들어 그 뒤 셀들의 decompose 호출까지 실패시킨다(union 실패 격리 원칙이 예산
    축에서 깨진다, §2-6 항목2)."""
    max_calls = expected_calls * attempt_multiplier
    if union_enabled:
        # union 단계는 채워진 표본(N개)마다 한 번만 돈다(재시도 없음) — 상한은 attempt_multiplier
        # 가 아니라 목표 표본 수(expected_calls, "셀수×N") 기준으로 더한다.
        max_calls += expected_calls * union_extra_calls_per_sample(category_select_max_calls)
    return max_calls


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)

    if args.out is None:
        print("--out 이 필요합니다")
        return EXIT_REJECTED
    if args.out.exists():
        print(f"--out 디렉터리가 이미 있습니다: {args.out}")
        return EXIT_REJECTED
    if args.n <= 0 or args.attempt_multiplier <= 0:
        print("--n 과 --attempt-multiplier 는 0보다 커야 합니다")
        return EXIT_REJECTED

    try:
        anchor_path = resolve_fixture_path(args.fixture)
        anchors = load_anchor_set(args.fixture)
        cells = build_cells(anchors)
        prompt_text, prompt_identity = resolve_system_prompt(
            prompt_path=args.prompt, prompt_rev=args.prompt_rev
        )
    except Exception as exc:  # 스키마·해시·인자 오류는 네트워크 이전에 거절한다
        print(f"입력 거절: {exc}")
        return EXIT_REJECTED

    n = args.n
    # [F-4, §0 승계] 인자로 조건을 고정한다 — env/.env 를 읽으면 앵커와 러너가 다른 기준을 쓸 수
    # 있다. `get_settings()`(.env 반영)는 API 키가 필요한 live 분기 안으로만 옮긴다.
    category_fanout_max = Settings.model_fields["category_fanout_max"].default
    repurchase_max = Settings.model_fields["dedup_repurchase_max"].default
    max_total_tokens = Settings.model_fields["model_eval_max_total_tokens_per_run"].default
    # [F-3] category_select_max_calls 도 같은 이유로 고정값을 쓴다 — union 이 추가로 부를 콜
    # 상한 산정에만 쓰이고, --union 이 아니면 이 값 자체가 결과에 영향을 주지 않는다.
    category_select_max_calls = Settings.model_fields["category_select_max_calls"].default
    expected_calls = len(cells) * n
    max_calls = max_llm_calls(
        expected_calls=expected_calls,
        attempt_multiplier=args.attempt_multiplier,
        union_enabled=args.union,
        category_select_max_calls=category_select_max_calls,
    )
    budget = BudgetTracker(
        BudgetLimits(
            max_calls=max_calls,
            max_total_tokens=max_total_tokens,
            max_cost_usd=args.budget_usd,
        )
    )
    limits = PacerLimits(max_rpm=args.rpm, max_tpm=args.tpm)

    # [#432, §2-6 항목5] --union pre-flight — LLM 콜 이전에, --dry-run 이면 아예 건너뛴다
    # (dry-run 은 pg 접근이 0이어야 한다). live_settings 는 union 튜너블 기록·경고에도 재사용한다.
    live_settings: Settings | None = None
    union_preflight: dict[str, Any] | None = None
    if not args.dry_run:
        live_settings = get_settings()
    probe_settings = live_settings or Settings(_env_file=None)
    attr_axis_suppression = _resolve_attr_axis_suppression(
        args.attr_axis_suppression, probe_settings
    )
    if args.union and not args.dry_run:
        assert live_settings is not None
        try:
            union_preflight = preflight_catalog(live_settings.catalog_db_url)
        except UnionPreflightError as exc:
            print(f"--union pre-flight 거절: {exc}")
            return EXIT_REJECTED

    if args.dry_run:
        delegate: Any = ScriptedDecomposeLLM(anchors)
        model_config = dict(DRY_RUN_MODEL_CONFIG)
        pacer = _virtual_pacer(limits)
        sleep = _no_sleep
    else:
        assert live_settings is not None
        try:
            delegate, model_config = build_live_delegate(
                runtime_settings=live_settings,
                budget=budget,
                pricing=PriceBook.load(DEFAULT_PRICING_PATH),
            )
        except Exception as exc:
            print(f"LLM 설정 오류: {exc}")
            return EXIT_REJECTED
        pacer = GlobalPacer(limits)
        sleep = asyncio.sleep

    probe_llm = build_probe_llm(delegate, pacer=pacer, system=prompt_text)

    # [#432, §2-4] union 단계 전용 LLM — decompose 프롬프트 오버라이드가 없는 PacedLLM(같은
    # delegate·같은 pacer). --dry-run 이면 None 으로 둬 runner 가 표본마다 "건너뜀"을 기록한다.
    union_llm: Any | None = None
    dedicated_llm: Any | None = None
    union_tunables: dict[str, Any] | None = None
    if args.union and not args.dry_run:
        assert live_settings is not None
        union_llm = PacedLLM(delegate, pacer=pacer)
        union_tunables = union_tunable_report(live_settings)
        differs = union_tunables["differsFromDefault"]
        if differs:
            print(
                f"⚠️ --union 튜너블이 Settings 기본값과 다릅니다({', '.join(differs)}) — "
                ".env 가 측정 대상을 조용히 바꿨을 수 있습니다",
                file=sys.stderr,
            )
    if args.arm in ("dedicated", "tri") and not args.dry_run:
        dedicated_llm = PacedLLM(delegate, pacer=pacer)

    exit_code = EXIT_OK
    # [R2-1, legs_probe 승계] `run_cell` 은 BudgetExceeded 를 밖으로 던지지 않는다 — 예산이
    # 소진돼도 이미 모은 표본을 보존한 채 항상 30셀 전부를 정상 반환한다.
    results_cells = asyncio.run(
        run_probe(
            llm=probe_llm,
            cells=cells,
            n=n,
            tier=args.tier,
            attempt_multiplier=args.attempt_multiplier,
            judgment_settings=JUDGMENT_SETTINGS,
            concurrency=args.concurrency,
            category_fanout_max=category_fanout_max,
            repurchase_max=repurchase_max,
            leg_head_suppression=args.arm == "postprocess",
            leg_generic_heads=frozenset(live_settings.category_leg_generic_heads)
            if live_settings
            else frozenset(),
            leg_condition_terms=frozenset(live_settings.category_leg_condition_terms)
            if live_settings
            else frozenset(),
            attr_axis_suppression=attr_axis_suppression,
            attr_constraint_axes=frozenset(probe_settings.attr_condition_constraint_axes),
            dedicated_llm=dedicated_llm,
            dedicated_settings=probe_settings if dedicated_llm is not None else None,
            tri_generic_heads=frozenset(live_settings.category_leg_generic_heads)
            if live_settings
            else frozenset(),
            tri_condition_terms=frozenset(live_settings.category_leg_condition_terms)
            if live_settings
            else frozenset(),
            sleep=sleep,
            union_enabled=args.union,
            union_llm=union_llm,
            union_settings=live_settings if args.union else None,
        )
    )
    if budget.snapshot()["budgetExceeded"]:
        print(f"예산 초과로 중단: {budget.exceeded_reason} — 부분 산출물을 기록합니다")
        exit_code = EXIT_BUDGET

    scored = score_all(results_cells, anchors, union_enabled=args.union)
    diag = diagnostics(results_cells, anchors, union_enabled=args.union)
    baseline = compute_baseline(results_cells, anchors)
    rows = sample_rows(results_cells, anchors, JUDGMENT_SETTINGS)
    cause_summary = cause_axis_summary(rows)
    unfilled = unfilled_cells(results_cells, n=n)
    if unfilled and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    results = build_results(
        cells=results_cells,
        scored=scored,
        diagnostics_payload=diag,
        baseline=baseline,
        cause_summary=cause_summary,
        sample_rows_payload=rows,
        unfilled=unfilled,
        prompt=prompt_identity.as_dict(),
        tier=args.tier,
        model_config=model_config,
        fixture={
            "name": anchor_path.name,
            "version": anchors.fixture_version,
            "sha256": fixture_sha256(anchor_path),
        },
        n=n,
        judgment={"underspecifiedReaskEnabled": JUDGMENT_SETTINGS.underspecified_reask_enabled},
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        dry_run=args.dry_run,
        union_enabled=args.union,
    )
    union_manifest_section: dict[str, Any] | None = None
    if args.union:
        # [F-3] budget 산식 — dry-run·live 공통으로 남긴다(재현·감사용).
        budget_call_formula = {
            "expectedCalls": expected_calls,
            "attemptMultiplier": args.attempt_multiplier,
            "categorySelectMaxCalls": category_select_max_calls,
            "unionExtraCallsPerSample": union_extra_calls_per_sample(category_select_max_calls),
            "maxCalls": max_calls,
            "formula": "maxCalls = expectedCalls*attemptMultiplier + expectedCalls*"
            "(categorySelectMaxCalls+1) — union 은 표본당(재시도 없이) 최대 "
            "categorySelectMaxCalls(원 매핑+재매핑 택일 합)+1(전개) 콜을 추가로 쓴다",
        }
        if args.dry_run:
            union_manifest_section = {
                "enabled": True,
                "dryRunSkipped": True,
                "budgetCallFormula": budget_call_formula,
                "note": "--dry-run --union 은 pg 접근 0 — 배관만 확인했다(§2-6 항목5). "
                "실측이 아니다(union 축은 전부 unionStageError 로 채워진다).",
            }
        else:
            assert live_settings is not None and union_tunables is not None
            assert union_preflight is not None
            union_manifest_section = {
                "enabled": True,
                "dryRunSkipped": False,
                "preflight": union_preflight,
                "embeddingModelId": live_settings.embedding_model_id,
                "tunables": union_tunables["actual"],
                "tunableDefaults": union_tunables["defaults"],
                "tunablesDifferFromDefault": union_tunables["differsFromDefault"],
                "budgetCallFormula": budget_call_formula,
                "embeddingCallsNotCountedInBudget": "union 이 추가로 부르는 임베딩 호출은 "
                "예산·페이서에 안 잡힌다(§2-6 항목4) — 관측 비용은 LLM 콜만의 부분합이다.",
                "revertStoreSideEffect": "_prepare_recommendation 끝부분이 get_revert_store() "
                "를 부른다(pg-profile, 실패 시 InMemory 폴백) — 판정에 영향 없는 부작용이며 "
                "로컬 dev DB 에 되돌리기 키가 쓰일 수 있다(§2-2).",
            }
    manifest = build_underspecified_probe_manifest(
        command=_command(argv),
        seed=args.seed,
        prompt=prompt_identity,
        tier=args.tier,
        model_config=model_config,
        n=n,
        attempt_multiplier=args.attempt_multiplier,
        concurrency=args.concurrency,
        anchor_path=anchor_path,
        fixture_version=anchors.fixture_version,
        category_fanout_max=category_fanout_max,
        repurchase_max=repurchase_max,
        underspecified_reask_enabled=JUDGMENT_SETTINGS.underspecified_reask_enabled,
        attr_axis_suppression=attr_axis_suppression,
        attr_constraint_axes=probe_settings.attr_condition_constraint_axes,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        cell_ids=[cell.cell_id for cell in results_cells],
        axis_definitions={
            axis_id: axis.as_dict()["definition"] for axis_id, axis in scored["axes"].items()
        },
        dry_run=args.dry_run,
        union=union_manifest_section,
    )
    write_artifacts(
        args.out,
        results=results,
        manifest=manifest,
        cells=results_cells,
        sample_rows_payload=rows,
        union_enabled=args.union,
    )

    primary = scored["axes"]["missRate"]
    union_suffix = ""
    if args.union:
        union_after = scored["axes"].get("missRateAfterExpansion")
        error_count = diag["unionStageErrorCount"]
        union_suffix = (
            f" · missRateAfterExpansion={union_after.numerator}/{union_after.denominator} "
            f"· unionStageErrorCount={error_count}"
            if union_after is not None
            else f" · unionStageErrorCount={error_count}"
        )
    print(
        f"missRate={primary.numerator}/{primary.denominator} · "
        f"셀 {len(results_cells)} · 못 채운 셀 {len(unfilled)} · "
        f"prompt={prompt_identity.sha12} tier={args.tier}{union_suffix} → {args.out}"
    )
    return exit_code
