"""보고서의 정직성 문구와 결정성을 검증한다."""

from evals.benchmark.report import render_markdown
from evals.benchmark.stats import summarize_group


def test_report_is_deterministic_and_prints_p99_reason_and_denominators() -> None:
    records = [
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": 8,
            "error": False,
            "timed_out": False,
            "degraded": False,
        }
    ]
    summary = summarize_group(
        records, elapsed_s=1, p99_min_samples=100, resamples=10, confidence=0.95, seed=1
    )
    kwargs = {
        "summaries": {"buyer_recommend@1": summary},
        "join_stats": {"joined": 0, "measured": 1, "rate": 0.0},
        "server_log_provided": False,
        "sample_size_rationale": None,
    }
    first = render_markdown(**kwargs)
    second = render_markdown(**kwargs)
    assert first == second
    assert "server-side 지표 미수집" in first
    assert "insufficient_samples(n=1 < 100)" in first
    assert "latency 제외 규칙" in first
    assert "reliability denominator" in first
    assert "#137" in first


def test_report_prints_unknown_degrade_and_cold_limitation() -> None:
    records = [
        {
            "phase": "cold",
            "success": True,
            "client_ttft_ms": 8,
            "error": False,
            "timed_out": False,
            "degraded": None,
            "outcome_match": "unknown",
            "outcome_unknown_reasons": ["lane_not_observed", "degrade_not_observed"],
        }
    ]
    summary = summarize_group(
        records,
        elapsed_s=1,
        p99_min_samples=100,
        resamples=10,
        confidence=0.95,
        seed=1,
        phase="cold",
    )
    report = render_markdown(
        {"cold:buyer_recommend@1": summary},
        join_stats={"joined": 0, "measured": 0, "rate": None},
        server_log_provided=False,
        sample_size_rationale=None,
    )
    assert "| unknown | 1 |" in report
    assert "outcome mismatch | outcome unknown" in report
    assert "lane_not_observed=1" in report
    assert "cold 표본" in report


def test_report_exposes_outcome_mismatch_and_unknown_counts() -> None:
    records = [
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": 8,
            "error": False,
            "timed_out": False,
            "degraded": False,
            "outcome_match": False,
            "outcome_mismatch_reasons": ["unexpected_degrade"],
        },
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": 9,
            "error": False,
            "timed_out": False,
            "degraded": None,
            "outcome_match": "unknown",
            "outcome_unknown_reasons": ["degrade_not_observed"],
        },
    ]
    summary = summarize_group(
        records, elapsed_s=1, p99_min_samples=100, resamples=10, confidence=0.95, seed=1
    )
    report = render_markdown(
        {"measured:buyer@1": summary},
        join_stats={"joined": 1, "measured": 2, "rate": 0.5},
        server_log_provided=True,
        sample_size_rationale=None,
    )
    assert summary["outcome_match_count"] == 0
    assert summary["outcome_mismatch_count"] == 1
    assert summary["outcome_unknown_count"] == 1
    assert "| 0 | 1 | 1 |" in report


def test_report_exposes_price_missing_unknown_count() -> None:
    records = [
        {
            "phase": "measured",
            "success": True,
            "client_ttft_ms": 8,
            "error": False,
            "timed_out": False,
            "degraded": False,
            "server_join": "joined",
            "model_ids": ["gpt-5-nano"],
            "cost_usd": None,
            "cost_unknown_reason": "price_missing(model=gpt-5-nano)",
        }
    ]
    summary = summarize_group(
        records, elapsed_s=1, p99_min_samples=100, resamples=2, confidence=0.95, seed=1
    )
    report = render_markdown(
        {"measured:buyer@1": summary},
        join_stats={"joined": 1, "measured": 1, "rate": 1.0},
        server_log_provided=True,
        sample_size_rationale=None,
    )
    assert "price_missing=1" in report
    assert "cost=$unknown" in report
