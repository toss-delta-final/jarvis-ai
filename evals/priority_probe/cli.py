"""priority 신호 실측 프로브 CLI — 수동 실행 도구다. CI 에 넣지 않는다 (#281 TASK 3).

uv run python -m evals.priority_probe --arm classifier --out artifacts/prio/classifier-1
uv run python -m evals.priority_probe --arm inline     --out artifacts/prio/inline-1
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation import need_priority as need_priority_module
from app.core.config import get_settings
from evals.intent_probe.client import build_live_delegate, prompt_identity, resolve_system_prompt
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.model_eval.budget import BudgetExceeded, BudgetLimits, BudgetTracker
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook
from evals.priority_probe.client import build_classifier_llm, build_inline_llm
from evals.priority_probe.fakes import ScriptedPriorityLLM
from evals.priority_probe.loader import fixture_sha256, load_fixture_set, resolve_fixture_path
from evals.priority_probe.manifest import build_priority_probe_manifest, classifier_prompt_sha256
from evals.priority_probe.metrics import diagnostics, score_all
from evals.priority_probe.report import build_results, write_artifacts
from evals.priority_probe.runner import CellResult, run_probe, unfilled_cells
from evals.priority_probe.schema import PriorityCell

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_BUDGET = 3
EXIT_UNFILLED = 4

DEFAULT_CANDIDATE_PATH = Path(__file__).with_name("candidates") / "inline_priority.txt"

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
    parser = argparse.ArgumentParser(description="니즈 priority 신호 실측 프로브 (#281 TASK 3)")
    parser.add_argument("--arm", choices=("classifier", "inline"), required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", default="default", help="'default' 또는 픽스처 JSON 경로")
    parser.add_argument("--tier", choices=("fast", "smart"), default="fast")
    parser.add_argument(
        "--prompt",
        type=Path,
        default=None,
        help="인라인 팔의 후보 _SYSTEM 파일(기본: candidates/inline_priority.txt). "
        "classifier 팔에는 적용되지 않는다(문면 교체 불가).",
    )
    parser.add_argument("--repeats", type=int, default=8, help="셀당 성공 표본 수 N")
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument("--max-calls", type=int)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case-ids", help="cellId 쉼표 목록")
    selection.add_argument("--case-limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="가짜 LLM — API 를 부르지 않는다")
    parser.add_argument("--seed", type=int, default=20260805)
    return parser


def _select_cells(cells: list[PriorityCell], args: argparse.Namespace) -> list[PriorityCell]:
    if args.case_ids:
        requested = {value.strip() for value in args.case_ids.split(",") if value.strip()}
        known = {cell.cell_id for cell in cells}
        missing = sorted(requested - known)
        if missing:
            raise ValueError(f"알 수 없는 cellId: {missing}")
        return [cell for cell in cells if cell.cell_id in requested]
    if args.case_limit is not None:
        if args.case_limit <= 0:
            raise ValueError("--case-limit 은 0보다 커야 합니다")
        return cells[: args.case_limit]
    return cells


def _virtual_pacer(limits: PacerLimits) -> GlobalPacer:
    state = {"now": 0.0}

    async def _sleep(seconds: float) -> None:
        state["now"] += seconds

    return GlobalPacer(limits, clock=lambda: state["now"], sleep=_sleep)


async def _no_sleep(seconds: float) -> None:
    return None


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.priority_probe " + " ".join(shlex.quote(arg) for arg in argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)

    if args.out is None:
        print("--out 이 필요합니다")
        return EXIT_REJECTED
    if args.out.exists():
        print(f"--out 디렉터리가 이미 있습니다: {args.out}")
        return EXIT_REJECTED
    if args.repeats <= 0 or args.attempt_multiplier <= 0:
        print("--repeats 와 --attempt-multiplier 는 0보다 커야 합니다")
        return EXIT_REJECTED

    prompt_path = args.prompt
    if args.arm == "inline" and prompt_path is None:
        prompt_path = DEFAULT_CANDIDATE_PATH

    try:
        fixture_path = resolve_fixture_path(args.fixture)
        fixture = load_fixture_set(args.fixture)
        cells = _select_cells(fixture.cells, args)
        if args.arm == "inline":
            prompt_text, resolved_prompt_identity = resolve_system_prompt(prompt_path=prompt_path)
        else:
            # 분류기 팔은 프롬프트 교체가 없다 — 배포 문면(need_priority._SYSTEM)의 정체성을
            # 그대로 manifest/results 에 남긴다(decompose 의 프롬프트를 기록하면 이 팔이 실제로
            # 무엇을 쟀는지가 거짓이 된다).
            prompt_text = None
            resolved_prompt_identity = prompt_identity(
                need_priority_module._SYSTEM, source="repo:need_priority._SYSTEM"
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
        delegate: Any = ScriptedPriorityLLM(fixture, arm=args.arm)
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

    if args.arm == "classifier":
        probe_llm, capture = build_classifier_llm(delegate, pacer=pacer)
    else:
        probe_llm, capture = build_inline_llm(delegate, pacer=pacer, system=prompt_text)

    diagnoses: list[dict[str, Any]] = []
    collected: list[CellResult] = []
    exit_code = EXIT_OK
    try:
        results_cells = asyncio.run(
            run_probe(
                arm=args.arm,
                llm=probe_llm,
                capture=capture,
                cells=cells,
                fixture=fixture,
                n=n,
                tier=args.tier,
                attempt_multiplier=args.attempt_multiplier,
                concurrency=args.concurrency,
                settings=settings,
                sleep=sleep,
                on_cell_done=collected.append,
                on_diagnosis=diagnoses.append,
            )
        )
    except BudgetExceeded as exc:
        print(f"예산 초과로 중단: {exc} — 부분 산출물을 기록합니다")
        results_cells = sorted(collected, key=lambda result: result.cell_id)
        exit_code = EXIT_BUDGET

    metrics = score_all(results_cells, fixture)
    unfilled = unfilled_cells(results_cells, n=n)
    if unfilled and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    resolved_prompt = resolved_prompt_identity.as_dict()
    results = build_results(
        arm=args.arm,
        cells=results_cells,
        metrics=metrics,
        diagnostics_payload=diagnostics(results_cells, diagnoses),
        unfilled=unfilled,
        prompt=resolved_prompt,
        classifier_prompt_sha256=classifier_prompt_sha256(),
        tier=args.tier,
        model_config=model_config,
        fixture={
            "name": fixture_path.name,
            "version": fixture.fixture_version,
            "sha256": fixture_sha256(fixture_path),
        },
        n=n,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        dry_run=args.dry_run,
    )
    manifest = build_priority_probe_manifest(
        command=_command(argv),
        seed=args.seed,
        arm=args.arm,
        prompt=resolved_prompt_identity,
        tier=args.tier,
        model_config=model_config,
        n=n,
        attempt_multiplier=args.attempt_multiplier,
        concurrency=args.concurrency,
        fixture_path=fixture_path,
        fixture_version=fixture.fixture_version,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        cell_ids=[cell.cell_id for cell in results_cells],
        metric_definitions={
            metric_id: metric.as_dict()["definition"] for metric_id, metric in metrics.items()
        },
        dry_run=args.dry_run,
    )
    write_artifacts(
        args.out, results=results, manifest=manifest, cells=results_cells, fixture=fixture
    )

    order_pairs = metrics["priorityOrderPairs"].as_dict()
    print(
        f"arm={args.arm} priorityOrderPairs={order_pairs['numerator']}/{order_pairs['denominator']} "
        f"· 셀 {len(results_cells)} · 못 채운 셀 {len(unfilled)} · "
        f"prompt={resolved_prompt['sha12']} tier={args.tier} → {args.out}"
    )
    return exit_code
