"""벤치마크 통계의 결정적 순수 함수."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from scripts.aggregate_observability import percentile

P99_CONTRACT_MIN_SAMPLES = 100


def bootstrap_percentile_ci(
    values: Sequence[float],
    p: float,
    *,
    resamples: int,
    confidence: float,
    seed: int,
) -> dict[str, Any]:
    """고정 지역 시드로 percentile bootstrap 신뢰구간을 계산한다."""
    if len(values) < 2:
        return {"low": None, "high": None, "unknown_reason": "insufficient_samples(n<2)"}
    rng = random.Random(seed)
    size = len(values)
    estimates = [
        percentile([values[rng.randrange(size)] for _ in range(size)], p) for _ in range(resamples)
    ]
    numeric = [value for value in estimates if value is not None]
    alpha = (1 - confidence) / 2
    return {
        "low": percentile(numeric, alpha * 100),
        "high": percentile(numeric, (1 - alpha) * 100),
        "unknown_reason": None,
    }


def summarize_group(
    records: Sequence[dict[str, Any]],
    *,
    elapsed_s: float,
    p99_min_samples: int,
    resamples: int,
    confidence: float,
    seed: int,
    phase: str = "measured",
) -> dict[str, Any]:
    """신뢰도와 지연 분모를 분리해 한 phase 그룹을 요약한다."""
    phase_records = [record for record in records if record.get("phase") == phase]
    successful = [record for record in phase_records if record.get("success") is True]
    total = len(phase_records)
    latency_n = len(successful)

    def rate(predicate: Callable[[dict[str, Any]], bool]) -> float | None:
        return sum(predicate(record) for record in phase_records) / total if total else None

    def latency_stats(field: str) -> dict[str, Any]:
        values = [
            float(record[field])
            for record in successful
            if isinstance(record.get(field), int | float)
        ]
        p99_threshold = max(p99_min_samples, P99_CONTRACT_MIN_SAMPLES)
        p99 = percentile(values, 99) if len(values) >= p99_threshold else None
        return {
            "n": len(values),
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "p99": p99,
            "p99_omitted_reason": (
                None
                if p99 is not None
                else f"insufficient_samples(n={len(values)} < {p99_threshold})"
            ),
            "max": max(values) if values else None,
            "p50_ci": bootstrap_percentile_ci(
                values,
                50,
                resamples=resamples,
                confidence=confidence,
                seed=seed,
            ),
            "p95_ci": bootstrap_percentile_ci(
                values,
                95,
                resamples=resamples,
                confidence=confidence,
                seed=seed,
            ),
        }

    server_samples = [record for record in phase_records if record.get("server_join") == "joined"]
    costs = [
        float(record["cost_usd"])
        for record in server_samples
        if isinstance(record.get("cost_usd"), int | float)
    ]
    prompt_tokens = [
        int(record["prompt_tokens"])
        for record in server_samples
        if isinstance(record.get("prompt_tokens"), int)
        and not isinstance(record["prompt_tokens"], bool)
    ]
    completion_tokens = [
        int(record["completion_tokens"])
        for record in server_samples
        if isinstance(record.get("completion_tokens"), int)
        and not isinstance(record["completion_tokens"], bool)
    ]
    degrade_known = [record for record in phase_records if isinstance(record.get("degraded"), bool)]
    degrade_count = sum(record["degraded"] is True for record in degrade_known)
    degrade_known_denominator = len(degrade_known)
    outcome_reasons = Counter(
        reason
        for record in phase_records
        for key in ("outcome_mismatch_reasons", "outcome_unknown_reasons")
        for reason in record.get(key, [])
        if isinstance(reason, str)
    )
    return {
        "reliability_denominator": total,
        "latency_denominator": latency_n,
        "latency_excluded": total - latency_n,
        "phase": phase,
        "success_count": len(successful),
        "success_rate": rate(lambda record: record.get("success") is True),
        "error_count": sum(record.get("error") is True for record in phase_records),
        "error_rate": rate(lambda record: record.get("error") is True),
        "timeout_count": sum(record.get("timed_out") is True for record in phase_records),
        "timeout_rate": rate(lambda record: record.get("timed_out") is True),
        "degrade_count": degrade_count,
        "degrade_known_denominator": degrade_known_denominator,
        "degrade_unknown_count": total - degrade_known_denominator,
        "degrade_rate": (
            degrade_count / degrade_known_denominator if degrade_known_denominator else None
        ),
        "outcome_match_count": sum(record.get("outcome_match") is True for record in phase_records),
        "outcome_mismatch_count": sum(
            record.get("outcome_match") is False for record in phase_records
        ),
        "outcome_unknown_count": sum(
            record.get("outcome_match") not in (True, False) for record in phase_records
        ),
        "outcome_reason_counts": dict(sorted(outcome_reasons.items())),
        "latency": {
            field: latency_stats(field)
            for field in (
                "client_ttft_ms",
                "client_first_event_ms",
                "client_total_ms",
                "server_first_text_token_ms",
            )
        },
        "server_metrics": {
            "joined_samples": len(server_samples),
            "cost_sample_count": len(costs),
            "cost_unknown_count": total - len(costs),
            "cost_price_missing_count": sum(
                isinstance(record.get("cost_unknown_reason"), str)
                and record["cost_unknown_reason"].startswith("price_missing(")
                for record in phase_records
            ),
            "cost_usd_total": sum(costs) if costs else None,
            "cost_usd_average": sum(costs) / len(costs) if costs else None,
            "prompt_tokens_sample_count": len(prompt_tokens),
            "prompt_tokens_unknown_count": total - len(prompt_tokens),
            "prompt_tokens_total": sum(prompt_tokens) if prompt_tokens else None,
            "completion_tokens_sample_count": len(completion_tokens),
            "completion_tokens_unknown_count": total - len(completion_tokens),
            "completion_tokens_total": sum(completion_tokens) if completion_tokens else None,
            "model_ids": sorted(
                {
                    model
                    for record in server_samples
                    for model in (record.get("model_ids") or [])
                    if isinstance(model, str)
                }
            )
            or None,
        },
        "throughput": {
            "completed_requests": total,
            "elapsed_s": elapsed_s,
            "requests_per_s": total / elapsed_s if elapsed_s > 0 else None,
        },
    }
