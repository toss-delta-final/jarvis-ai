"""보고서의 정직성 문구와 결정성을 검증한다."""

import pytest

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


# ─────────── #438 R2 F4 — STUB LLM MODE 배너 3갈래 ───────────


def _measured_record(**overrides: object) -> dict:
    record = {
        "phase": "measured",
        "success": True,
        "client_ttft_ms": 8,
        "error": False,
        "timed_out": False,
        "degraded": False,
    }
    record.update(overrides)
    return record


def test_report_shows_stub_banner_when_scripted_model_ids_observed() -> None:
    records = [
        _measured_record(
            server_join="joined", model_ids=["scripted-stub-fast", "scripted-stub-smart"]
        )
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
    assert "STUB LLM MODE" in report
    assert "scripted-stub-fast" in report
    assert "scripted-stub-smart" in report


def test_report_omits_stub_banner_for_real_model_ids() -> None:
    records = [_measured_record(server_join="joined", model_ids=["gpt-5-nano", "claude-sonnet-5"])]
    summary = summarize_group(
        records, elapsed_s=1, p99_min_samples=100, resamples=2, confidence=0.95, seed=1
    )
    report = render_markdown(
        {"measured:buyer@1": summary},
        join_stats={"joined": 1, "measured": 1, "rate": 1.0},
        server_log_provided=True,
        sample_size_rationale=None,
    )
    assert "STUB LLM MODE" not in report


def test_report_shows_llm_mode_unknown_without_server_log() -> None:
    """`--server-log` 미지정이면 스텁 여부를 추정하지 않고 "LLM 모드 미확인" 만 남긴다."""
    records = [_measured_record()]
    summary = summarize_group(
        records, elapsed_s=1, p99_min_samples=100, resamples=2, confidence=0.95, seed=1
    )
    report = render_markdown(
        {"measured:buyer@1": summary},
        join_stats={"joined": 0, "measured": 1, "rate": None},
        server_log_provided=False,
        sample_size_rationale=None,
    )
    assert "LLM 모드 미확인" in report
    assert "STUB LLM MODE" not in report


def test_report_raises_keyerror_if_server_metrics_key_missing() -> None:
    """`server_metrics` 자체가 없는 summary 는 `KeyError` 로 즉시 드러난다(방어 불필요, 결정 기록).

    `_observed_stub_model_ids` 는 `model_ids` **하위 키**만 `.get()` 으로 방어한다(값이 없을 수
    있는 정상 케이스 — `stats.py` 가 관측 0건이면 `None` 을 남긴다). `server_metrics` 딕셔너리
    자체는 render_markdown 의 다른 모든 필드(`server['joined_samples']` 등, 이 배너 코드보다
    먼저 있던 기존 코드)도 이미 존재를 무조건 전제하므로, 배너 코드에서만 방어하면 일관성 없이
    "단일 생산자 stats.summarize_group() 이 항상 채운다"는 계약 위반을 조용히 숨기게 된다 —
    그래서 `.get()` 을 추가하지 않기로 판단했다(#438 R2 F4).
    """
    summary_without_server_metrics: dict = {}
    with pytest.raises(KeyError):
        render_markdown(
            {"g": summary_without_server_metrics},
            join_stats={"joined": 0, "measured": 0, "rate": None},
            server_log_provided=True,
            sample_size_rationale=None,
        )
