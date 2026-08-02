"""시나리오×동시성 벤치마크 오케스트레이션 CLI."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from evals.benchmark.client import measure_request
from evals.benchmark.manifest import assert_secret_free, collect_manifest, redact_url_userinfo
from evals.benchmark.report import render_markdown, write_artifacts
from evals.benchmark.scenarios import Scenario, evaluate_outcome, load_scenarios
from evals.benchmark.server_join import apply_price_evidence, join_records, load_server_records
from evals.benchmark.stats import summarize_group

MIN_MEASURED_CONTRACT_REQUESTS = 30


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    """Settings 기본값을 주입한 CLI parser를 만든다."""
    parser = argparse.ArgumentParser(description="HTTP/SSE benchmark runner")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--target-label", required=True)
    parser.add_argument("--scenarios")
    parser.add_argument(
        "--concurrency",
        default=",".join(str(value) for value in settings.benchmark_concurrency_levels),
    )
    parser.add_argument(
        "--measured-requests", type=int, default=settings.benchmark_min_measured_requests
    )
    parser.add_argument("--sample-size-rationale")
    parser.add_argument("--server-log", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("evals/benchmark/baselines"))
    parser.add_argument("--auth-token-env")
    parser.add_argument("--client-region", default=os.getenv("BENCH_CLIENT_REGION"))
    parser.add_argument("--instance-type", default=os.getenv("BENCH_INSTANCE_TYPE"))
    parser.add_argument("--image")
    parser.add_argument("--dependency-note", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _compact(timestamp: str) -> str:
    return timestamp.replace("+00:00", "Z").replace("-", "").replace(":", "").replace(".", "")


def _parse_concurrency(value: str) -> list[int]:
    try:
        levels = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise ValueError("concurrency must be comma-separated integers") from exc
    if not levels or any(level <= 0 for level in levels):
        raise ValueError("concurrency levels must be positive")
    # 같은 수준을 합치고 첫 batch elapsed만 쓰면 throughput이 부풀어 순서대로 중복 제거한다.
    return list(dict.fromkeys(levels))


def _validate_args(
    args: argparse.Namespace, settings: Settings
) -> tuple[list[Scenario], list[int], str | None]:
    if args.measured_requests <= 0:
        raise ValueError("measured requests must be positive")
    # 설정은 정직성 계약 하한 30을 올릴 수만 있고 낮출 수는 없다.
    measured_minimum = max(settings.benchmark_min_measured_requests, MIN_MEASURED_CONTRACT_REQUESTS)
    if args.measured_requests < measured_minimum and not (
        args.sample_size_rationale and args.sample_size_rationale.strip()
    ):
        raise ValueError("measured requests below minimum require --sample-size-rationale")
    selected = set(args.scenarios.split(",")) if args.scenarios else None
    scenarios = load_scenarios(selected)
    levels = _parse_concurrency(args.concurrency)
    token = None
    if args.auth_token_env:
        token = os.getenv(args.auth_token_env)
        if not token:
            raise ValueError(f"auth token environment variable is unset: {args.auth_token_env}")
    if args.server_log and not args.server_log.is_file():
        raise ValueError(f"server log does not exist: {args.server_log}")
    return scenarios, levels, token


def _payload(scenario: Scenario) -> dict[str, Any]:
    """스트림 락 충돌을 막도록 요청마다 고유 session/thread를 덮어쓴다."""
    payload = dict(scenario.payload)
    payload["sessionId"] = f"bench-{uuid.uuid4()}"
    payload["threadId"] = f"bench-{uuid.uuid4()}"
    return payload


async def _run_batch(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    phase: str,
    count: int,
    concurrency: int,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(index: int) -> dict[str, Any]:
        async with semaphore:
            result = await measure_request(
                client, endpoint=scenario.endpoint, payload=_payload(scenario), headers=headers
            )
            result.update(
                scenario_id=scenario.id,
                role=scenario.role,
                phase=phase,
                concurrency=concurrency,
                request_index=index,
            )
            return result

    started = time.perf_counter()
    records = await asyncio.gather(*(one(index) for index in range(count)))
    elapsed = time.perf_counter() - started
    for record in records:
        record["group_elapsed_s"] = elapsed
    return records


async def run_network(
    args: argparse.Namespace,
    scenarios: list[Scenario],
    levels: list[int],
    token: str | None,
    settings: Settings,
) -> list[dict[str, Any]]:
    """cold/warmup을 분리한 뒤 scenario×concurrency measured 요청을 실행한다."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx.Timeout(settings.benchmark_request_timeout_s)
    records: list[dict[str, Any]] = []
    for scenario in scenarios:
        # 시나리오마다 새 풀로 cold를 수집한 뒤 같은 풀에서 각 동시성 warm-up→measured를 잇는다.
        async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
            records.extend(
                await _run_batch(
                    client,
                    scenario,
                    phase="cold",
                    count=settings.benchmark_cold_requests,
                    concurrency=1,
                    headers=headers,
                )
            )
            for concurrency in levels:
                records.extend(
                    await _run_batch(
                        client,
                        scenario,
                        phase="warmup",
                        count=max(settings.benchmark_warmup_requests, concurrency),
                        concurrency=concurrency,
                        headers=headers,
                    )
                )
                records.extend(
                    await _run_batch(
                        client,
                        scenario,
                        phase="measured",
                        count=args.measured_requests,
                        concurrency=concurrency,
                        headers=headers,
                    )
                )
    return records


def _summaries(records: list[dict[str, Any]], settings: Settings) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        phase = record.get("phase")
        if phase not in {"cold", "measured"}:
            continue
        key = f"{phase}:{record['scenario_id']}@{record['concurrency']}"
        grouped.setdefault(key, []).append(record)
    return {
        key: summarize_group(
            values,
            elapsed_s=float(values[0]["group_elapsed_s"]),
            p99_min_samples=settings.benchmark_p99_min_samples,
            resamples=settings.benchmark_bootstrap_resamples,
            confidence=settings.benchmark_bootstrap_confidence,
            seed=settings.benchmark_bootstrap_seed,
            phase=str(values[0]["phase"]),
        )
        for key, values in grouped.items()
    }


def _command(argv: list[str]) -> str:
    """셸 재인용이 가능한 정확한 argv를 토큰 값 없이 보존한다."""
    safe_argv = list(argv)
    for index, value in enumerate(safe_argv[:-1]):
        if value == "--base-url":
            safe_argv[index + 1] = redact_url_userinfo(safe_argv[index + 1])
    return shlex.join([sys.executable, "-m", "evals.benchmark.runner", *safe_argv])


def main(argv: list[str] | None = None) -> int:
    """사용 오류는 2, 측정 품질과 무관한 정상 실행은 0으로 끝낸다."""
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    parser = build_parser(settings)
    args = parser.parse_args(actual_argv)
    started_at = datetime.now(timezone.utc).isoformat()
    destination = args.out_dir / f"{_compact(started_at)}-{args.target_label}"
    try:
        scenarios, levels, token = _validate_args(args, settings)
        if destination.exists():
            raise ValueError(f"output directory already exists: {destination}")
        induced = sorted({scenario.induced_by for scenario in scenarios if scenario.induced_by})
        dependency_notes = list(args.dependency_note) + [f"induced_by={item}" for item in induced]
        if args.dry_run:
            print(f"dry-run: target={redact_url_userinfo(args.base_url)} label={args.target_label}")
            print(f"scenarios={','.join(scenario.id for scenario in scenarios)}")
            print(
                f"concurrency={','.join(str(level) for level in levels)} measured={args.measured_requests}"
            )
            print(f"output={destination}")
            print("network_calls=0 status=valid")
            return 0
        raw = asyncio.run(run_network(args, scenarios, levels, token, settings))
        server = load_server_records(args.server_log) if args.server_log else None
        joined, join_stats = join_records(raw, server)
        apply_price_evidence(
            joined,
            input_prices=settings.model_price_in_per_1k,
            output_prices=settings.model_price_out_per_1k,
        )
        scenarios_by_id = {scenario.id: scenario for scenario in scenarios}
        for record in joined:
            record.update(evaluate_outcome(record, scenarios_by_id[record["scenario_id"]]))
        summaries = _summaries(joined, settings)
        model_ids = {
            model
            for record in joined
            for model in (record.get("model_ids") or [])
            if isinstance(model, str)
        }
        ended_at = datetime.now(timezone.utc).isoformat()
        manifest = collect_manifest(
            repo=Path.cwd(),
            settings=settings,
            started_at_utc=started_at,
            ended_at_utc=ended_at,
            command=_command(actual_argv),
            base_url=args.base_url,
            target_label=args.target_label,
            auth_mode_observed="bearer_token" if token else "dev_guest",
            client_region=args.client_region,
            instance_type=args.instance_type,
            image=args.image,
            dependency_notes=dependency_notes,
            sample_size_rationale=args.sample_size_rationale,
            model_ids=model_ids if server is not None else None,
        )
        report = render_markdown(
            summaries,
            join_stats=join_stats,
            server_log_provided=args.server_log is not None,
            sample_size_rationale=args.sample_size_rationale,
        )
        artifacts = {"manifest": manifest, "raw": joined, "report": report}
        assert_secret_free(artifacts, [token] if token else [])
        write_artifacts(
            destination,
            report=report,
            summaries=summaries,
            join_stats=join_stats,
            raw_records=joined,
            manifest=manifest,
        )
        print(destination)
        return 0
    except (ValueError, OSError) as exc:
        print(f"benchmark runner error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
