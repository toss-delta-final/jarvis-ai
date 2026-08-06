"""니즈 전개(legs) 평가 하네스 CLI — 수동 실행 도구다. CI 에 넣지 않는다.

uv run python -m evals.legs_probe --out evals/legs_probe/baselines/fast-2026-08-06 --tier fast
uv run python -m evals.legs_probe --out /tmp/probe --dry-run
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
    build_live_delegate,
    build_probe_llm,
    resolve_system_prompt,
)
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.legs_probe.fakes import ScriptedDecomposeLLM
from evals.legs_probe.loader import (
    build_cells,
    fixture_sha256,
    load_anchor_set,
    resolve_fixture_path,
)
from evals.legs_probe.manifest import build_legs_probe_manifest
from evals.legs_probe.metrics import (
    compute_baseline,
    diagnostics,
    pair_diagnostics,
    prompt_example_diagnostics,
    score_all,
)
from evals.legs_probe.report import build_results, write_artifacts
from evals.legs_probe.runner import run_probe, unfilled_cells
from evals.model_eval.budget import BudgetLimits, BudgetTracker
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="니즈 전개(legs) 평가 하네스 (#332)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", help="외부 앵커 JSON 경로 — 생략하면 committed anchors.json")
    parser.add_argument("--tier", choices=("fast", "smart"), default="fast")
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt", type=Path, help="후보 decompose 프롬프트 파일")
    prompt_source.add_argument("--prompt-rev", help="과거 판 _SYSTEM 을 읽을 git 리비전")
    parser.add_argument("--n", type=int, default=8, help="셀당 성공 표본 수")
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument(
        "--budget-usd", type=float, default=5.0, help="model_eval BudgetTracker 상한"
    )
    parser.add_argument("--dry-run", action="store_true", help="가짜 LLM — API 를 부르지 않는다")
    parser.add_argument("--seed", type=int, default=20260806)
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
    return "uv run python -m evals.legs_probe " + " ".join(shlex.quote(arg) for arg in argv)


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
    # [F-4, §0] 인자로 조건을 고정한다 — env/.env 를 읽으면 앵커(schema.py 가
    # Settings.model_fields[...].default 로 legsMax 상한을 검증한다)와 러너가 다른 기준을
    # 쓸 수 있다(.env 에 CATEGORY_FANOUT_MAX=3 이 있으면 앵커는 5까지 허용되는데 산출은 3에서
    # 잘려 legsInRange 가 조용히 왜곡된다). `get_settings()`(.env 반영)는 API 키가 필요한
    # live 분기 안으로만 옮긴다 — dry-run 은 .env 존재 여부와 무관하게 같은 산출을 낸다.
    category_fanout_max = Settings.model_fields["category_fanout_max"].default
    repurchase_max = Settings.model_fields["dedup_repurchase_max"].default
    max_total_tokens = Settings.model_fields["model_eval_max_total_tokens_per_run"].default
    expected_calls = len(cells) * n
    max_calls = expected_calls * args.attempt_multiplier
    budget = BudgetTracker(
        BudgetLimits(
            max_calls=max_calls,
            max_total_tokens=max_total_tokens,
            max_cost_usd=args.budget_usd,
        )
    )
    limits = PacerLimits(max_rpm=args.rpm, max_tpm=args.tpm)

    if args.dry_run:
        delegate: Any = ScriptedDecomposeLLM(anchors)
        model_config = dict(DRY_RUN_MODEL_CONFIG)
        pacer = _virtual_pacer(limits)
        sleep = _no_sleep
    else:
        try:
            delegate, model_config = build_live_delegate(
                runtime_settings=get_settings(),
                budget=budget,
                pricing=PriceBook.load(DEFAULT_PRICING_PATH),
            )
        except Exception as exc:
            print(f"LLM 설정 오류: {exc}")
            return EXIT_REJECTED
        pacer = GlobalPacer(limits)
        sleep = asyncio.sleep

    probe_llm = build_probe_llm(delegate, pacer=pacer, system=prompt_text)
    exit_code = EXIT_OK
    # [R2-1] `run_cell` 은 더 이상 BudgetExceeded 를 밖으로 던지지 않는다 — 예산이 소진돼도
    # 이미 모은 표본을 보존한 채 **항상 39셀 전부**를 정상 반환한다(시작 못 한 셀은 표본
    # 0짜리 CellResult). 예산 소진 여부는 `budget.snapshot()` 으로 사후 판정한다.
    results_cells = asyncio.run(
        run_probe(
            llm=probe_llm,
            cells=cells,
            n=n,
            tier=args.tier,
            attempt_multiplier=args.attempt_multiplier,
            concurrency=args.concurrency,
            category_fanout_max=category_fanout_max,
            repurchase_max=repurchase_max,
            sleep=sleep,
        )
    )
    if budget.snapshot()["budgetExceeded"]:
        print(f"예산 초과로 중단: {budget.exceeded_reason} — 부분 산출물을 기록합니다")
        exit_code = EXIT_BUDGET

    scored = score_all(results_cells, anchors)
    diag = diagnostics(results_cells, anchors)
    baseline = compute_baseline(anchors)
    pairs = pair_diagnostics(results_cells, anchors)
    prompt_examples = prompt_example_diagnostics(results_cells, anchors)
    unfilled = unfilled_cells(results_cells, n=n)
    if unfilled and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    results = build_results(
        cells=results_cells,
        scored=scored,
        diagnostics_payload=diag,
        baseline=baseline,
        pair_rows=pairs,
        prompt_example_rows=prompt_examples,
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
        category_fanout_max=category_fanout_max,
        repurchase_max=repurchase_max,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        dry_run=args.dry_run,
    )
    manifest = build_legs_probe_manifest(
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
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        cell_ids=[cell.cell_id for cell in results_cells],
        axis_definitions={
            axis_id: axis.as_dict()["definition"] for axis_id, axis in scored["axes"].items()
        },
        dry_run=args.dry_run,
    )
    write_artifacts(
        args.out, results=results, manifest=manifest, cells=results_cells, anchors=anchors
    )

    primary = scored["axes"]["case3UnderExpansionRate"]
    print(
        f"case3UnderExpansionRate={primary.numerator}/{primary.denominator} · "
        f"셀 {len(results_cells)} · 못 채운 셀 {len(unfilled)} · "
        f"prompt={prompt_identity.sha12} tier={args.tier} → {args.out}"
    )
    return exit_code
