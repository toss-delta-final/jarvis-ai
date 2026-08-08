"""취향 추출 골든셋 프로브 CLI — 수동 실행 도구다. CI 에 넣지 않는다 (#462).

uv run python -m evals.taste_probe --out artifacts/taste-probe/run1
uv run python -m evals.taste_probe --out /tmp/tp --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from app.agents.profile.store import reset_profile_store
from app.core.config import get_settings
from evals.intent_probe.client import PacedLLM, build_live_delegate
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.model_eval.budget import BudgetExceeded, BudgetLimits, BudgetTracker
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook
from evals.taste_probe.baseline import score_baseline
from evals.taste_probe.fakes import ScriptedDeltaLLM, build_fake_catalog
from evals.taste_probe.loader import (
    PreflightError,
    fixture_sha256,
    load_golden_set,
    preflight_check_catalog,
    resolve_fixture_path,
)
from evals.taste_probe.manifest import build_taste_probe_manifest, delta_system_prompt_sha256
from evals.taste_probe.metrics import (
    build_confusion,
    build_predicate_confusion,
    diagnostics,
    score_all,
    score_recall_by_slice,
)
from evals.taste_probe.report import build_results, write_artifacts
from evals.taste_probe.runner import SessionResult, run_probe, unfilled_sessions
from evals.taste_probe.seams import fake_catalog_seam

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
    parser = argparse.ArgumentParser(description="취향 추출 골든셋 프로브 (#462)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", default="default", help="default 또는 골든셋 JSON 경로")
    parser.add_argument("--n", type=int, default=5, help="세션당 성공 표본 수 N")
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument(
        "--estimated-tokens-per-call",
        type=int,
        # 세션 버퍼는 짧다(3~5턴) — 프롬프트 실측치(_DELTA_SYSTEM ≈ 700자 ≈ 250tok + 버퍼 ≈
        # 5턴×40자 ≈ 200자 ≈ 70tok) + profile_delta_max_tokens(2048) 예약분을 더해 잡는다.
        # category_probe 의 3,900(decompose, max_tokens=800)보다 큰 이유는 델타 응답 상한이
        # 2048 로 더 크기 때문이다 — README 에 근거를 남긴다.
        default=2_400,
        help="델타 프롬프트 max_tokens=2048 예약분 포함 토큰 추정치",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        # [A-9] 공유 `RecordingLLM`(evals/model_eval/recording.py) 은 호출마다
        # unittest.mock.patch("app.core.llm._record_usage", ...) 로 모듈 전역을 패치한다 — 동시
        # 실행은 usage·예산 집계를 오염시키고 조기 원복된 패치를 되살릴 수 있다(이 하네스가 만든
        # 결함이 아니라 evals/model_eval 공유 인프라의 성질, 별건으로 보고됨). 150콜 규모는
        # 순차로도 충분하다 — 1보다 큰 값은 그 위험을 감수하고 켜는 것이다.
        help="기본 1(순차) — 공유 RecordingLLM 의 전역 patch 가 동시 실행에서 usage·예산 집계를 "
        "오염시킬 수 있다(별건, evals/model_eval 소관). 가짜 LLM 경로에는 영향 없다.",
    )
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument(
        "--dry-run", action="store_true", help="가짜 LLM·가짜 카탈로그 — API/pg 콜 0"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260808,
        # [A-17] evals/category_probe 와 동일 규약 — 실행 동작에 영향 없다.
        help="manifest 기록 전용 — 실행 동작(dry-run 결함 주기·라이브 호출 순서 등)에 영향 없다",
    )
    return parser


def _virtual_pacer(limits: PacerLimits) -> GlobalPacer:
    state = {"now": 0.0}

    async def _sleep(seconds: float) -> None:
        state["now"] += seconds

    return GlobalPacer(limits, clock=lambda: state["now"], sleep=_sleep)


async def _no_sleep(seconds: float) -> None:
    return None


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.taste_probe " + " ".join(shlex.quote(arg) for arg in argv)


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
        fixture_path = resolve_fixture_path(args.fixture)
        golden_set = load_golden_set(args.fixture)
    except Exception as exc:  # noqa: BLE001 - 스키마·해시 오류는 네트워크 이전에 거절한다
        print(f"입력 거절: {exc}")
        return EXIT_REJECTED

    settings = get_settings()
    if not settings.profile_graph_delta_enabled:
        print("profile_graph_delta_enabled=False — 이 설정에서는 트리플이 생기지 않습니다")
        return EXIT_REJECTED
    if not args.dry_run:
        try:
            preflight_check_catalog(golden_set, settings.catalog_db_url)
        except PreflightError as exc:
            print(f"사전 pre-flight 거절: {exc}")
            return EXIT_REJECTED
        except Exception as exc:  # noqa: BLE001 - pg 연결 실패 등도 사전 거절
            print(f"사전 pre-flight 오류: {exc}")
            return EXIT_REJECTED

    n = args.n
    expected_calls = len(golden_set.sessions) * n
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

    # [설계 판단] `reset_profile_store()` 를 런 시작 시 **정확히 1회** 부른다 — 표본마다 부르면
    # `--concurrency` > 1 에서 동시 진행 중인 다른 세션의 전역 스토어를 갈아치우는 레이스가 된다
    # (runner.py 모듈 docstring 참조). 격리는 표본별 유일 user_id/thread_key 로 충분하다.
    reset_profile_store()

    seam_cm: Any
    if args.dry_run:
        catalog = build_fake_catalog(golden_set)
        probe_llm: Any = ScriptedDeltaLLM(
            golden_set,
            fail_first=1,
            omit_every=5,
            extra_every=7,
            misclassify_every=11,
            schema_violation_every=13,
        )
        model_config = dict(DRY_RUN_MODEL_CONFIG)
        pacer = _virtual_pacer(limits)
        sleep = _no_sleep
        dictionary = None
        seam_cm = fake_catalog_seam(catalog)
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
        probe_llm = PacedLLM(delegate, pacer=pacer)
        from evals.category_probe.manifest import dictionary_fingerprint

        dictionary = dictionary_fingerprint(settings.catalog_db_url)
        seam_cm = nullcontext()

    collected: list[SessionResult] = []
    exit_code = EXIT_OK
    try:
        with seam_cm:
            result_sessions = asyncio.run(
                run_probe(
                    llm=probe_llm,
                    golden_set=golden_set,
                    n=n,
                    attempt_multiplier=args.attempt_multiplier,
                    concurrency=args.concurrency,
                    settings=settings,
                    sleep=sleep,
                    on_session_done=collected.append,
                )
            )
    except BudgetExceeded as exc:
        print(f"예산 초과로 중단: {exc} — 부분 산출물을 기록합니다")
        result_sessions = sorted(collected, key=lambda result: result.session_id)
        exit_code = EXIT_BUDGET

    axes = score_all(result_sessions, golden_set, n=n)
    axes_by_slice = score_recall_by_slice(result_sessions, golden_set, n=n)
    # [A-12] golden_set 전체와 대조 — 예산 중단으로 시작도 못 한 세션도 목록에 낸다.
    unfilled = unfilled_sessions(result_sessions, golden_set, n=n)
    if unfilled and exit_code == EXIT_OK:
        exit_code = EXIT_UNFILLED

    # [A-5] noiseFalsePositiveRate(분모=noise 세션×N)와 같은 분모로 대조하려면 n 이 필요하다.
    baseline_payload = score_baseline(golden_set, n=n)

    results = build_results(
        sessions=result_sessions,
        axes=axes,
        axes_by_slice=axes_by_slice,
        baseline=baseline_payload,
        diagnostics_payload=diagnostics(result_sessions),
        confusion=build_confusion(result_sessions, golden_set),
        predicate_confusion=build_predicate_confusion(result_sessions, golden_set),
        unfilled=unfilled,
        model_config=model_config,
        fixture={
            "name": fixture_path.name,
            "version": golden_set.dataset_version,
            "sha256": fixture_sha256(fixture_path),
        },
        # [A-16] report.md 헤더가 "첫 줄에 프롬프트 해시" 문서와 실제로 일치하게 한다.
        prompt_sha12=delta_system_prompt_sha256()[:12],
        n=n,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        dry_run=args.dry_run,
    )
    manifest = build_taste_probe_manifest(
        command=_command(argv),
        seed=args.seed,
        model_config=model_config,
        n=n,
        attempt_multiplier=args.attempt_multiplier,
        concurrency=args.concurrency,
        fixture_path=fixture_path,
        fixture_sha256=fixture_sha256(fixture_path),
        dataset_version=golden_set.dataset_version,
        pacer=pacer.snapshot(),
        budget=budget.snapshot(),
        session_ids=[session.session_id for session in result_sessions],
        axis_definitions={axis_id: axis.as_dict()["definition"] for axis_id, axis in axes.items()},
        dry_run=args.dry_run,
        thresholds={
            "profileGateThreshold": settings.profile_gate_threshold,
            "graphNodeDistanceMax": settings.graph_node_distance_max,
            "graphNodeOverrideMargin": settings.graph_node_override_margin,
            "profileDeltaMaxTokens": settings.profile_delta_max_tokens,
            "profileSessionBufferCap": settings.profile_session_buffer_cap,
            "profileBufferRepeatCap": settings.profile_buffer_repeat_cap,
            "categoryTopK": settings.category_top_k,
        },
        dictionary=dictionary,
    )
    write_artifacts(
        args.out,
        results=results,
        manifest=manifest,
        sessions=result_sessions,
        golden_set=golden_set,
    )

    recall = axes["recall"].as_dict()
    print(
        f"recall={recall['numerator']}/{recall['denominator']} · 세션 {len(result_sessions)} · "
        f"못 채운 세션 {len(unfilled)} → {args.out}"
    )
    return exit_code
