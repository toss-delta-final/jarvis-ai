"""Manual CLI for the buyer rerank-grounding A/B/C experiment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shlex
import sys
from pathlib import Path
from typing import cast

from app.agents.buyer.recommendation.rerank import _SYSTEM, _SYSTEM_STRUCTURED_GROUNDING
from app.agents.buyer.recommendation.rerank_grounding import GroundingArm
from app.core.config import get_settings
from evals.intent_probe.client import PacedLLM, build_live_delegate
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.metrics.run_manifest import build_run_manifest
from evals.model_eval.budget import BudgetExceeded, BudgetLimits, BudgetTracker
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook
from evals.rerank_grounding.fakes import ScriptedGroundingLLM
from evals.rerank_grounding.report import write_artifacts
from evals.rerank_grounding.runner import CellResult, ProbeRun, run_probe
from evals.rerank_grounding.schema import (
    DEFAULT_FIXTURE_PATH,
    FixtureSet,
    fixture_sha256,
    load_fixture,
)

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_BUDGET = 3
EXIT_UNFILLED = 4
VALIDATOR_VERSION = "rerank-grounding-v1"
OVERALL_VALIDATOR_VERSION = "overall-comment-grounding-v1"
ALL_ARMS: tuple[GroundingArm, ...] = ("current", "prompt_only", "validated")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="buyer rerank 근거 A/B/C 수동 평가")
    parser.add_argument("--arms", required=True, help="all 또는 current,prompt_only,validated")
    parser.add_argument("--fixture", default="default", help="default 또는 fixture JSON 경로")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tier", choices=("smart",), default="smart")
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--case-ids", help="caseId 쉼표 목록")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=20260812)
    return parser


def _parse_arms(raw: str) -> tuple[GroundingArm, ...]:
    if raw == "all":
        return ALL_ARMS
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if (
        not values
        or len(values) != len(set(values))
        or any(value not in ALL_ARMS for value in values)
    ):
        raise ValueError(
            "--arms 는 all 또는 current,prompt_only,validated의 중복 없는 목록이어야 합니다"
        )
    return cast(tuple[GroundingArm, ...], values)


def _fixture_path(raw: str) -> Path:
    return DEFAULT_FIXTURE_PATH if raw == "default" else Path(raw)


def _select_cases(fixture: FixtureSet, raw_case_ids: str | None) -> FixtureSet:
    if raw_case_ids is None:
        return fixture
    requested = {value.strip() for value in raw_case_ids.split(",") if value.strip()}
    known = {case.case_id for case in fixture.cases}
    missing = sorted(requested - known)
    if not requested or missing:
        raise ValueError(f"알 수 없는 caseId: {missing or sorted(requested)}")
    return FixtureSet(
        fixture_version=fixture.fixture_version,
        schema_version=fixture.schema_version,
        cases=tuple(case for case in fixture.cases if case.case_id in requested),
    )


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.rerank_grounding " + " ".join(
        shlex.quote(value) for value in argv
    )


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest(
    *,
    command: str,
    seed: int,
    dry_run: bool,
    fixture_path: Path,
    fixture: FixtureSet,
    arms: tuple[GroundingArm, ...],
    repeats: int,
    attempt_multiplier: int,
    model_config: dict[str, object],
    budget: dict[str, object],
    pacer: dict[str, object],
) -> dict[str, object]:
    base = build_run_manifest(command=command, seed=seed)
    budget_unknown = bool(budget.get("unknownCostCallCount"))
    return {
        "run": base["run"],
        "gitCommit": base["commitSha"],
        "dirty": base["dirty"],
        "pythonVersion": base["pythonVersion"],
        "platform": base["platform"],
        "command": command,
        "seed": seed,
        "dryRun": dry_run,
        "arms": list(arms),
        "datasetName": fixture_path.name,
        "datasetVersion": fixture.fixture_version,
        "datasetHash": fixture_sha256(fixture_path),
        "caseIds": [case.case_id for case in fixture.cases],
        "promptHashes": {
            "current": _prompt_hash(_SYSTEM),
            "structured": _prompt_hash(_SYSTEM_STRUCTURED_GROUNDING),
        },
        "validatorVersion": VALIDATOR_VERSION,
        "overallValidatorVersion": OVERALL_VALIDATOR_VERSION,
        "modelConfig": model_config,
        "repeats": repeats,
        "attemptMultiplier": attempt_multiplier,
        "budget": budget,
        "costUnknownReason": "provider_usage_or_price_missing" if budget_unknown else None,
        "pacer": pacer,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.out.exists():
        print(f"--out 디렉터리가 이미 있습니다: {args.out}")
        return EXIT_REJECTED
    if (
        args.repeats <= 0
        or args.attempt_multiplier <= 0
        or args.rpm <= 0
        or args.tpm <= 0
        or args.concurrency != 1
    ):
        print("repeats/attempt-multiplier/rpm/tpm은 양수이고 concurrency는 1이어야 합니다")
        return EXIT_REJECTED
    try:
        arms = _parse_arms(args.arms)
        fixture_path = _fixture_path(args.fixture)
        fixture = _select_cases(load_fixture(fixture_path), args.case_ids)
    except ValueError as exc:
        print(f"입력 거절: {exc}")
        return EXIT_REJECTED

    expected_calls = len(fixture.cases) * len(arms) * args.repeats
    max_calls = args.max_calls or expected_calls * args.attempt_multiplier
    if expected_calls > max_calls:
        print(f"예상 성공 호출 {expected_calls}가 상한 {max_calls}를 넘습니다")
        return EXIT_REJECTED

    settings = get_settings()
    max_cost_usd = (
        args.max_cost_usd
        if args.max_cost_usd is not None
        else settings.model_eval_max_cost_usd_per_run
    )
    if max_cost_usd <= 0:
        print("--max-cost-usd 는 0보다 커야 합니다")
        return EXIT_REJECTED
    budget = BudgetTracker(
        BudgetLimits(
            max_calls=max_calls,
            max_total_tokens=settings.model_eval_max_total_tokens_per_run,
            max_cost_usd=max_cost_usd,
        )
    )
    pacer = GlobalPacer(PacerLimits(max_rpm=args.rpm, max_tpm=args.tpm))
    if args.dry_run:
        llm = ScriptedGroundingLLM(invalid_evidence=True)
        model_config: dict[str, object] = {
            "provider": "dry-run",
            "fastModel": "scripted",
            "smartModel": "scripted",
            "fastReasoningEffort": None,
            "smartReasoningEffort": None,
            "timeoutS": None,
            "maxRetries": 0,
        }
    else:
        try:
            delegate, live_model_config = build_live_delegate(
                runtime_settings=settings,
                budget=budget,
                pricing=PriceBook.load(DEFAULT_PRICING_PATH),
            )
        except Exception as exc:  # noqa: BLE001 - CLI preflight error
            print(f"LLM 설정 오류: {exc}")
            return EXIT_REJECTED
        llm = PacedLLM(delegate, pacer=pacer)
        model_config = dict(live_model_config)

    completed_cells: list[CellResult] = []
    exit_code = EXIT_OK
    try:
        run = asyncio.run(
            run_probe(
                llm=llm,
                fixture=fixture,
                arms=arms,
                repeats=args.repeats,
                attempt_multiplier=args.attempt_multiplier,
                expose_max=9,
                on_cell_done=completed_cells.append,
            )
        )
    except BudgetExceeded as exc:
        print(f"예산 초과로 중단: {exc} — 완료 셀의 부분 산출물을 기록합니다")
        run = ProbeRun(
            fixture_version=fixture.fixture_version,
            arms=arms,
            repeats=args.repeats,
            cells=tuple(completed_cells),
        )
        exit_code = EXIT_BUDGET
    if run.unfilled_cells() and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    manifest = _manifest(
        command=_command(argv),
        seed=args.seed,
        dry_run=args.dry_run,
        fixture_path=fixture_path,
        fixture=fixture,
        arms=arms,
        repeats=args.repeats,
        attempt_multiplier=args.attempt_multiplier,
        model_config=model_config,
        budget=budget.snapshot(),
        pacer=pacer.snapshot(),
    )
    write_artifacts(args.out, run=run, manifest=manifest)
    print(
        f"arms={','.join(arms)} cases={len(fixture.cases)} N={args.repeats} "
        f"unfilled={len(run.unfilled_cells())} → {args.out}"
    )
    return exit_code
