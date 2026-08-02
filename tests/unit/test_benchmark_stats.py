"""벤치마크 통계의 분모와 손계산 정의를 검증한다."""

from scripts.aggregate_observability import percentile

from evals.benchmark.stats import bootstrap_percentile_ci, summarize_group


def test_percentile_and_bootstrap_match_hand_calculation() -> None:
    assert percentile([4, 1, 3, 2], 50) == 2
    assert percentile([4, 1, 3, 2], 95) == 4
    # seed=0에서 길이 2의 첫 resample 인덱스는 [1, 1]이므로 통계량은 2다.
    assert bootstrap_percentile_ci([1, 2], 50, resamples=1, confidence=0.95, seed=0) == {
        "low": 2,
        "high": 2,
        "unknown_reason": None,
    }


def test_failed_and_timeout_remain_only_in_reliability_denominator() -> None:
    records = [
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": 10,
            "error": False,
            "timed_out": False,
            "degraded": False,
        },
        {
            "phase": "measured",
            "success": False,
            "client_ttft_ms": None,
            "error": True,
            "timed_out": False,
            "degraded": False,
        },
        {
            "phase": "measured",
            "success": False,
            "client_ttft_ms": None,
            "error": False,
            "timed_out": True,
            "degraded": True,
        },
        {
            "phase": "warmup",
            "success": True,
            "client_ttft_ms": 1,
            "error": False,
            "timed_out": False,
            "degraded": False,
        },
    ]
    result = summarize_group(
        records, elapsed_s=2, p99_min_samples=100, resamples=10, confidence=0.95, seed=3
    )
    assert result["reliability_denominator"] == 3
    assert result["latency_denominator"] == 1
    assert result["latency_excluded"] == 2
    assert result["success_rate"] == 1 / 3
    assert result["error_count"] == 1
    assert result["timeout_count"] == 1
    assert result["degrade_count"] == 1
    assert result["throughput"] == {"completed_requests": 3, "elapsed_s": 2, "requests_per_s": 1.5}
    assert result["latency"]["client_ttft_ms"]["p99"] is None
    assert (
        result["latency"]["client_ttft_ms"]["p99_omitted_reason"]
        == "insufficient_samples(n=1 < 100)"
    )
    assert (
        result["latency"]["client_ttft_ms"]["p50_ci"]["unknown_reason"]
        == "insufficient_samples(n<2)"
    )


def test_unjoined_degrade_and_cost_are_unknown_not_zero() -> None:
    records = [
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": 10,
            "error": False,
            "timed_out": False,
            "degraded": None,
            "server_join": "unavailable",
            "cost_usd": None,
        }
        for _ in range(3)
    ]
    result = summarize_group(
        records, elapsed_s=1, p99_min_samples=100, resamples=10, confidence=0.95, seed=3
    )
    assert result["degrade_known_denominator"] == 0
    assert result["degrade_unknown_count"] == 3
    assert result["degrade_rate"] is None
    assert result["server_metrics"]["cost_unknown_count"] == 3
    assert result["server_metrics"]["prompt_tokens_unknown_count"] == 3
    assert result["server_metrics"]["completion_tokens_unknown_count"] == 3


def test_p99_contract_floor_cannot_be_lowered_by_setting() -> None:
    records = [
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": value,
            "error": False,
            "timed_out": False,
            "degraded": False,
        }
        for value in range(30)
    ]
    result = summarize_group(
        records, elapsed_s=1, p99_min_samples=30, resamples=10, confidence=0.95, seed=3
    )
    latency = result["latency"]["client_ttft_ms"]
    assert latency["p99"] is None
    assert latency["p99_omitted_reason"] == "insufficient_samples(n=30 < 100)"
