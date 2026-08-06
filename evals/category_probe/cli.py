"""카테고리 매핑·선택 프로브 CLI — 수동 실행 도구다. CI 에 넣지 않는다 (#331).

uv run python -m evals.category_probe --out artifacts/category-probe/run1 --tier fast
uv run python -m evals.category_probe --out /tmp/probe --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from evals.category_probe.baseline import run_baseline, score_baseline
from evals.category_probe.client import (
    GlobalPacer,
    PacerLimits,
    build_live_delegate,
    build_probe_llm,
)
from evals.category_probe.fakes import ScriptedCategoryLLM, build_fake_catalog
from evals.category_probe.loader import (
    PreflightError,
    fixture_sha256,
    load_anchor_set,
    preflight_check_catalog,
    resolve_fixture_path,
)
from evals.category_probe.manifest import build_category_probe_manifest, dictionary_fingerprint
from evals.category_probe.metrics import (
    build_confusion,
    diagnostics,
    distance_distribution,
    score_all,
)
from evals.category_probe.report import build_results, write_artifacts
from evals.category_probe.runner import CellResult, run_probe, unfilled_cells
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
    "timeoutS": None,
    "maxRetries": 0,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="카테고리 매핑·선택 프로브 (#331)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", default="default", help="default 또는 앵커 JSON 경로")
    parser.add_argument("--tier", choices=("fast", "smart"), default="fast")
    parser.add_argument("--n", type=int, default=8, help="셀당 성공 표본 수 N")
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument(
        "--estimated-tokens-per-call",
        type=int,
        default=PacerLimits.estimated_tokens_per_call,
        help="decompose max_tokens=800 예약분 포함 토큰 추정치",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--dry-run", action="store_true", help="가짜 LLM·pg — API/pg 콜 0")
    parser.add_argument("--seed", type=int, default=20260806)
    return parser


def _virtual_pacer(limits: PacerLimits) -> GlobalPacer:
    state = {"now": 0.0}

    async def _sleep(seconds: float) -> None:
        state["now"] += seconds

    return GlobalPacer(limits, clock=lambda: state["now"], sleep=_sleep)


async def _no_sleep(seconds: float) -> None:
    return None


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.category_probe " + " ".join(shlex.quote(arg) for arg in argv)


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
    except Exception as exc:  # noqa: BLE001 - 스키마·해시 오류는 네트워크 이전에 거절한다
        print(f"입력 거절: {exc}")
        return EXIT_REJECTED

    settings = get_settings()
    if not args.dry_run:
        try:
            preflight_check_catalog(anchors, settings.catalog_db_url)
        except PreflightError as exc:
            print(f"사전 pre-flight 거절: {exc}")
            return EXIT_REJECTED
        except Exception as exc:  # noqa: BLE001 - pg 연결 실패 등도 사전 거절
            print(f"사전 pre-flight 오류: {exc}")
            return EXIT_REJECTED

    n = args.n
    expected_calls = len(anchors.cells) * n
    max_calls = args.max_calls or expected_calls * args.attempt_multiplier
    if expected_calls > max_calls:
        print(f"예상 호출 {expected_calls} 가 상한 {max_calls} 를 넘습니다")
        return EXIT_REJECTED

    budget = BudgetTracker(
        BudgetLimits(
            max_calls=max_calls,
            max_total_tokens=settings.model_eval_max_total_tokens_per_run,
            max_cost_usd=settings.model_eval_max_cost_usd_per_run,
        )
    )
    limits = PacerLimits(
        max_rpm=args.rpm, max_tpm=args.tpm, estimated_tokens_per_call=args.estimated_tokens_per_call
    )

    if args.dry_run:
        catalog = build_fake_catalog(anchors)
        probe_llm: Any = ScriptedCategoryLLM(anchors)
        model_config = dict(DRY_RUN_MODEL_CONFIG)
        pacer = _virtual_pacer(limits)
        sleep = _no_sleep
        embed_fn = catalog.embed
        search_fn = catalog.search
        exact_fn = catalog.exact
        dsn = "dry-run"
    else:
        try:
            delegate, model_config = build_live_delegate(
                runtime_settings=settings,
                budget=budget,
                pricing=PriceBook.load(DEFAULT_PRICING_PATH),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"LLM 설정 오류: {exc}")
            return EXIT_REJECTED
        pacer = GlobalPacer(limits)
        sleep = asyncio.sleep
        probe_llm = build_probe_llm(delegate, pacer=pacer)
        embed_fn = None
        search_fn = None
        exact_fn = None
        dsn = settings.catalog_db_url

    collected: list[CellResult] = []
    exit_code = EXIT_OK
    try:
        results_cells = asyncio.run(
            run_probe(
                llm=probe_llm,
                anchors=anchors,
                n=n,
                tier=args.tier,
                attempt_multiplier=args.attempt_multiplier,
                concurrency=args.concurrency,
                category_fanout_max=settings.category_fanout_max,
                settings=settings,
                embed=embed_fn,
                search_top_k=search_fn,
                exact_lookup=exact_fn,
                sleep=sleep,
                on_cell_done=collected.append,
            )
        )
    except BudgetExceeded as exc:
        print(f"예산 초과로 중단: {exc} — 부분 산출물을 기록합니다")
        results_cells = sorted(collected, key=lambda result: result.cell_id)
        exit_code = EXIT_BUDGET

    axes = score_all(results_cells, anchors, n=n, category_top_k=settings.category_top_k)
    unfilled = unfilled_cells(results_cells, n=n)
    if unfilled and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    baseline_embed = embed_fn if args.dry_run else _real_embed(settings)
    baseline_search = search_fn if args.dry_run else _real_search()
    baseline_results = run_baseline(
        anchors,
        embed=baseline_embed,
        search_top_k=baseline_search,
        dsn=dsn,
        k=settings.category_top_k,
    )
    baseline_payload = score_baseline(baseline_results, anchors)

    results = build_results(
        cells=results_cells,
        axes=axes,
        baseline=baseline_payload,
        diagnostics_payload=diagnostics(results_cells, anchors),
        confusion=build_confusion(results_cells, anchors),
        distance_distribution=distance_distribution(results_cells, anchors),
        unfilled=unfilled,
        tier=args.tier,
        model_config=model_config,
        fixture={
            "name": anchor_path.name,
            "version": anchors.fixture_version,
            "sha256": fixture_sha256(anchor_path),
        },
        n=n,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        dry_run=args.dry_run,
    )
    manifest = build_category_probe_manifest(
        command=_command(argv),
        seed=args.seed,
        tier=args.tier,
        model_config=model_config,
        n=n,
        attempt_multiplier=args.attempt_multiplier,
        concurrency=args.concurrency,
        anchor_path=anchor_path,
        anchor_sha256=fixture_sha256(anchor_path),
        fixture_version=anchors.fixture_version,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        cell_ids=[cell.cell_id for cell in results_cells],
        axis_definitions={axis_id: axis.as_dict()["definition"] for axis_id, axis in axes.items()},
        dry_run=args.dry_run,
        thresholds={
            "categoryDistanceMax": settings.category_distance_max,
            "categoryDistanceOverrideMargin": settings.category_distance_override_margin,
            "categorySelectMarginMax": settings.category_select_margin_max,
        },
        dictionary=None if args.dry_run else dictionary_fingerprint(settings.catalog_db_url),
    )
    write_artifacts(args.out, results=results, manifest=manifest, cells=results_cells)

    top1 = axes["top1Single"].as_dict()
    print(
        f"top1Single={top1['numerator']}/{top1['denominator']} · 셀 {len(results_cells)} · "
        f"못 채운 셀 {len(unfilled)} · tier={args.tier} → {args.out}"
    )
    return exit_code


def _real_embed(settings):  # noqa: ANN001
    from app.pipelines.embedding import embed_texts

    return lambda texts: embed_texts(texts, task_type=settings.embedding_task_query)


def _real_search():
    from app.pipelines.category_search import search_categories_pg

    return search_categories_pg
