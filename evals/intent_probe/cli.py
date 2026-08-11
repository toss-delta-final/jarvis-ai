"""intent 라우팅 프로브 CLI — 수동 실행 도구다. CI 에 넣지 않는다.

uv run python -m evals.intent_probe --out artifacts/run1 --tier fast
uv run python -m evals.intent_probe --out artifacts/cand1 --prompt cand1.txt
uv run python -m evals.intent_probe --out artifacts/base --prompt-rev 3f1dec7
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from evals.intent_probe.client import (
    build_live_delegate,
    build_probe_llm,
    repo_system_prompt,
    resolve_system_prompt,
)
from evals.intent_probe.fakes import ScriptedDecomposeLLM
from evals.intent_probe.loader import (
    Cell,
    build_cells,
    fixture_sha256,
    load_anchor_set,
    resolve_fixture_path,
)
from evals.intent_probe.manifest import (
    build_intent_probe_manifest,
    category_scope_prompt_sha256,
    underspecified_prompt_sha256,
)
from evals.intent_probe.metrics import diagnostics, score_all
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.intent_probe.report import build_results, write_artifacts
from evals.intent_probe.runner import CellResult, run_probe, unfilled_cells
from evals.model_eval.budget import BudgetExceeded, BudgetLimits, BudgetTracker
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
    parser = argparse.ArgumentParser(description="decompose intent 라우팅 안정성 프로브 (#260)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", default="b", help="a|b 또는 앵커 JSON 경로")
    parser.add_argument("--tier", choices=("fast", "smart"), default="fast")
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt", type=Path, help="후보 프롬프트 파일")
    prompt_source.add_argument("--prompt-rev", help="과거 판 _SYSTEM 을 읽을 git 리비전")
    parser.add_argument("--dump-prompt", type=Path, help="현재 _SYSTEM 을 바이트 그대로 저장")
    parser.add_argument("--repeats", type=int, default=8, help="셀당 성공 표본 수 N")
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument("--max-calls", type=int)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case-ids", help="utteranceId 쉼표 목록")
    selection.add_argument("--case-limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="가짜 LLM — API 를 부르지 않는다")
    parser.add_argument(
        "--no-classifier",
        action="store_true",
        help="카테고리 범위 해제 분류기를 호출하지 않는다(#84 결함 재현용 — scopeFree 는 전부 None)",
    )
    parser.add_argument(
        "--no-underspecified-classifier",
        action="store_true",
        help="#463 과소지정 전용 분류기를 끄고 기존 #430 프롬프트 규칙만 사용한다",
    )
    parser.add_argument("--seed", type=int, default=20260803)
    return parser


def _select_cells(cells: list[Cell], args: argparse.Namespace) -> list[Cell]:
    if args.case_ids:
        requested = {value.strip() for value in args.case_ids.split(",") if value.strip()}
        known = {cell.utterance.utterance_id for cell in cells}
        missing = sorted(requested - known)
        if missing:
            raise ValueError(f"알 수 없는 utteranceId: {missing}")
        return [cell for cell in cells if cell.utterance.utterance_id in requested]
    if args.case_limit is not None:
        if args.case_limit <= 0:
            raise ValueError("--case-limit 은 0보다 커야 합니다")
        return cells[: args.case_limit]
    return cells


def _virtual_pacer(limits: PacerLimits) -> GlobalPacer:
    """dry-run 전용 — 페이싱 로직은 그대로 돌리되 실제로 자지 않는다(산출물도 결정론)."""
    state = {"now": 0.0}

    async def _sleep(seconds: float) -> None:
        state["now"] += seconds

    return GlobalPacer(limits, clock=lambda: state["now"], sleep=_sleep)


async def _no_sleep(seconds: float) -> None:
    return None


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.intent_probe " + " ".join(shlex.quote(arg) for arg in argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)

    if args.dump_prompt is not None:
        args.dump_prompt.parent.mkdir(parents=True, exist_ok=True)
        args.dump_prompt.write_text(repo_system_prompt(), encoding="utf-8", newline="")
        print(f"현재 _SYSTEM 을 {args.dump_prompt} 에 저장했습니다")
        return EXIT_OK

    if args.out is None:
        print("--out 이 필요합니다")
        return EXIT_REJECTED
    if args.out.exists():
        print(f"--out 디렉터리가 이미 있습니다: {args.out}")
        return EXIT_REJECTED
    if args.repeats <= 0 or args.attempt_multiplier <= 0:
        print("--repeats 와 --attempt-multiplier 는 0보다 커야 합니다")
        return EXIT_REJECTED

    try:
        anchor_path = resolve_fixture_path(args.fixture)
        anchors = load_anchor_set(args.fixture)
        cells = _select_cells(build_cells(anchors), args)
        prompt_text, prompt_identity = resolve_system_prompt(
            prompt_path=args.prompt, prompt_rev=args.prompt_rev
        )
    except Exception as exc:  # 스키마·해시·인자 오류는 네트워크 이전에 거절한다
        print(f"입력 거절: {exc}")
        return EXIT_REJECTED

    n = args.repeats
    expected_calls = len(cells) * n
    max_calls = args.max_calls or expected_calls * args.attempt_multiplier
    if expected_calls > max_calls:
        print(f"예상 호출 {expected_calls} 가 상한 {max_calls} 를 넘습니다")
        return EXIT_REJECTED

    settings = get_settings()
    budget = BudgetTracker(
        BudgetLimits(
            max_calls=max_calls,
            max_total_tokens=settings.model_eval_max_total_tokens_per_run,
            max_cost_usd=settings.model_eval_max_cost_usd_per_run,
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
                runtime_settings=settings,
                budget=budget,
                pricing=PriceBook.load(DEFAULT_PRICING_PATH),
            )
        except Exception as exc:
            print(f"LLM 설정 오류: {exc}")
            return EXIT_REJECTED
        pacer = GlobalPacer(limits)
        sleep = asyncio.sleep

    probe_llm = build_probe_llm(delegate, pacer=pacer, system=prompt_text)
    collected: list[CellResult] = []
    exit_code = EXIT_OK
    try:
        results_cells = asyncio.run(
            run_probe(
                llm=probe_llm,
                cells=cells,
                anchors=anchors,
                n=n,
                tier=args.tier,
                attempt_multiplier=args.attempt_multiplier,
                concurrency=args.concurrency,
                category_fanout_max=settings.category_fanout_max,
                repurchase_max=settings.dedup_repurchase_max,
                # [#84] 분류기 tier·max_tokens 는 배포 설정 그대로 쓴다 — `--tier` 는 decompose
                # 축의 스윕이고, 보조 분류기까지 함께 흔들면 두 변수를 한 표에서 재게 된다.
                settings=settings,
                classifier_enabled=not args.no_classifier,
                underspecified_classifier_enabled=not args.no_underspecified_classifier,
                sleep=sleep,
                on_cell_done=collected.append,
            )
        )
    except BudgetExceeded as exc:
        print(f"예산 초과로 중단: {exc} — 부분 산출물을 기록합니다")
        results_cells = sorted(collected, key=lambda result: result.cell_id)
        exit_code = EXIT_BUDGET

    axes = score_all(results_cells, anchors, n=n)
    unfilled = unfilled_cells(results_cells, n=n)
    if unfilled and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    results = build_results(
        cells=results_cells,
        axes=axes,
        diagnostics_payload=diagnostics(results_cells, anchors),
        unfilled=unfilled,
        prompt=prompt_identity.as_dict(),
        tier=args.tier,
        model_config=model_config,
        fixture={
            "name": anchor_path.name,
            "version": anchors.fixture_version,
            "sha256": fixture_sha256(anchor_path),
            "reaskProductId": anchors.reask_product_id,
            "reaskProductListPosition": anchors.reask_product_list_position,
        },
        n=n,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        dry_run=args.dry_run,
        # [#84·G-1] 분류기 팔이 켜졌는지를 산출물에 남긴다 — 안 남기면 두 표를 나중에 구분할 수
        # 없다(같은 프롬프트 해시·같은 픽스처인데 카테고리 축만 무너진 표가 된다).
        category_scope_prompt=(
            None
            if args.no_classifier
            else {
                "sha256": category_scope_prompt_sha256(),
                "sha12": category_scope_prompt_sha256()[:12],
            }
        ),
        underspecified_prompt=(
            None
            if args.no_underspecified_classifier
            else {
                "sha256": underspecified_prompt_sha256(),
                "sha12": underspecified_prompt_sha256()[:12],
            }
        ),
    )
    manifest = build_intent_probe_manifest(
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
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        cell_ids=[cell.cell_id for cell in results_cells],
        axis_definitions={axis_id: axis.as_dict()["definition"] for axis_id, axis in axes.items()},
        dry_run=args.dry_run,
        classifier_enabled=not args.no_classifier,
        underspecified_classifier_enabled=not args.no_underspecified_classifier,
    )
    write_artifacts(args.out, results=results, manifest=manifest, cells=results_cells)

    print(
        f"{results['issue240Line']} · 셀 {len(results_cells)} · 못 채운 셀 {len(unfilled)} · "
        f"prompt={prompt_identity.sha12} tier={args.tier} → {args.out}"
    )
    return exit_code
