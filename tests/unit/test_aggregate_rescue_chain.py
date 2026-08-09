"""구제 체인 실빈도·지연 집계의 계산·출력 계약."""

from __future__ import annotations

import csv
import json
import logging
from types import SimpleNamespace

import pytest

from app.core.logging import log_structured
from scripts import aggregate_rescue_chain
from scripts import aggregate_observability
from tests.unit import test_fanout


_PLAIN_FORMATTER = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")


def _line(event: str, **overrides: object) -> str:
    record: dict[str, object] = {
        "event": event,
        "rescue_elapsed_ms": 0,
        "relax_auto_elapsed_ms": 0,
        "relax_chip_elapsed_ms": 0,
        "may_auto_relax": True,
    }
    record.update(overrides)
    return json.dumps(record)


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "stream_first_token_timeout_s": 10.0,
        "spring_search_timeout_s": 3.0,
        "degrade_alert_min_samples": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_union_includes_pipeline_and_zero_result_with_nearest_rank_percentiles():
    result = aggregate_rescue_chain.aggregate_lines(
        [
            _line("recommend_pipeline", rescue_elapsed_ms=100),
            _line("recommend_zero_result", rescue_elapsed_ms=200),
            _line("recommend_pipeline", rescue_elapsed_ms=300),
        ]
    )

    first_token = result["first_token"]
    assert first_token["eligible"] == {"n": 3, "p50": 200.0, "p95": 300.0, "p99": 300.0}
    assert first_token["event_counts"] == {"recommend_zero_result": 1, "recommend_pipeline": 2}


def test_union_accepts_each_mutually_exclusive_event_on_its_own():
    pipeline_only = aggregate_rescue_chain.aggregate_lines(
        [_line("recommend_pipeline", rescue_elapsed_ms=111)]
    )
    zero_only = aggregate_rescue_chain.aggregate_lines(
        [_line("recommend_zero_result", rescue_elapsed_ms=222)]
    )

    assert pipeline_only["first_token"]["eligible"]["n"] == 1
    assert pipeline_only["first_token"]["eligible"]["p95"] == 111.0
    assert zero_only["first_token"]["eligible"]["n"] == 1
    assert zero_only["first_token"]["eligible"]["p95"] == 222.0


def test_parse_log_line_accepts_json_and_logging_prefix_for_each_target_event():
    plain = _line("recommend_zero_result")
    prefixed = f"2026-08-09 17:27:40,044 INFO graph {_line('recommend_pipeline')}"

    assert aggregate_rescue_chain.parse_log_line(plain)["event"] == "recommend_zero_result"
    assert aggregate_rescue_chain.parse_log_line(prefixed)["event"] == "recommend_pipeline"


def test_may_auto_relax_false_is_reported_separately_not_mixed_into_first_token_distribution():
    result = aggregate_rescue_chain.aggregate_lines(
        [
            _line("recommend_pipeline", rescue_elapsed_ms=100),
            _line("recommend_zero_result", rescue_elapsed_ms=200),
            _line("recommend_pipeline", rescue_elapsed_ms=1, may_auto_relax=False),
            _line("recommend_zero_result", rescue_elapsed_ms=2, may_auto_relax=False),
        ]
    )

    assert result["first_token"]["eligible"]["p50"] == 100.0
    assert result["first_token"]["not_delayed"] == {"n": 2, "p50": 1.0, "p95": 2.0, "p99": 2.0}


def test_first_token_contribution_excludes_post_sse_chip_probe_time():
    result = aggregate_rescue_chain.aggregate_lines(
        [
            _line(
                "recommend_zero_result",
                rescue_elapsed_ms=100,
                relax_auto_elapsed_ms=20,
                relax_chip_elapsed_ms=9_999,
            )
        ]
    )

    assert result["first_token"]["eligible"]["p50"] == 120.0


def test_rare_rescue_contributions_report_conditional_tail_and_exposure_not_only_diluted_percentiles():
    result = aggregate_rescue_chain.aggregate_lines(
        [_line("recommend_pipeline") for _ in range(995)]
        + [
            _line("recommend_pipeline", rescue_elapsed_ms=value)
            for value in (8_500, 8_800, 9_000, 9_300, 9_600)
        ]
    )

    first_token = result["first_token"]
    assert first_token["eligible"]["p95"] == 0.0
    assert first_token["contributing"] == {"n": 5, "p50": 9_000.0, "p95": 9_600.0, "p99": 9_600.0}
    assert first_token["exposure"] == {"count": 5, "rate": 0.005}
    assert first_token["max"] == 9_600.0

    markdown = aggregate_rescue_chain.render_markdown(result, _settings())
    assert "| 구제 기여 > 0 (조건부) | 5 | 9000 | 9600 | 9600 |" in markdown
    assert "9.0s 기준선 이상 3건" in markdown
    assert "9.0s 기준선 근접·도달" in markdown
    csv_rows = aggregate_rescue_chain._csv_rows(result, _settings(), min_samples=3)
    assert ("first_token", "contributing", "timeout_or_higher_count", 0) in csv_rows


def test_injected_thresholds_control_both_labels_and_exceedance_counts():
    result = aggregate_rescue_chain.aggregate_lines(
        [_line("recommend_pipeline") for _ in range(995)]
        + [
            _line("recommend_pipeline", rescue_elapsed_ms=value)
            for value in (8_500, 8_800, 9_000, 9_300, 9_600)
        ]
    )
    settings = _settings(spring_search_timeout_s=10.0, stream_first_token_timeout_s=40.0)

    markdown = aggregate_rescue_chain.render_markdown(result, settings)

    assert "30.0s 기준선 이상 0건, 40.0s 상한 이상 0건, 최댓값 9600ms" in markdown
    assert "30.0s 기준선 미만" in markdown


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([8_999, 8_999, 8_999], "9.0s 기준선 미만"),
        ([9_000, 9_000, 9_000], "9.0s 기준선 근접·도달"),
        ([10_000, 10_000, 10_000], "first-token 상한 도달·초과"),
        ([10_001, 10_001, 10_001], "first-token 상한 도달·초과"),
    ],
)
def test_proximity_covers_baseline_and_timeout_boundaries(values, expected):
    result = aggregate_rescue_chain.aggregate_lines(
        [_line("recommend_pipeline", rescue_elapsed_ms=value) for value in values]
    )

    assert expected in aggregate_rescue_chain.render_markdown(result, _settings())


def test_missing_null_and_invalid_metrics_are_excluded_and_counted():
    result = aggregate_rescue_chain.aggregate_lines(
        [
            _line("recommend_pipeline", rescue_elapsed_ms=10),
            _line("recommend_pipeline", rescue_elapsed_ms=None),
            _line("recommend_pipeline", rescue_elapsed_ms="10"),
            _line("recommend_pipeline", rescue_elapsed_ms=True),
            _line("recommend_pipeline", relax_auto_elapsed_ms=None),
        ]
    )

    assert result["first_token"]["eligible"] == {"n": 1, "p50": 10.0, "p95": 10.0, "p99": 10.0}
    assert result["first_token"]["excluded_invalid"] == 4
    assert "결측·형식 오류 제외 | 4" in aggregate_rescue_chain.render_markdown(result, _settings())


def test_chain_entry_and_timeout_rates_keep_their_distinct_denominators():
    result = aggregate_rescue_chain.aggregate_lines(
        [
            _line(
                "recommend_zero_result",
                post_suppress_fallback_attempted=True,
                category_expanded=True,
                had_candidates=True,
            ),
            _line("recommend_zero_result", post_suppress_fallback_attempted=False),
            _line("recommend_pipeline"),
            _line("chat_request", errorType="UPSTREAM_TIMEOUT"),
            _line("chat_request", errorType=None),
        ]
    )

    assert result["chain_entry"]["zero_result_turns"] == 2
    assert result["chain_entry"]["post_suppress_fallback_attempted"] == {"count": 1, "rate": 0.5}
    assert result["chain_entry"]["expanded_with_candidates"] == {"count": 1, "rate": 0.5}
    assert result["upstream_timeout"] == {"count": 1, "total": 2, "rate": 0.5}
    markdown = aggregate_rescue_chain.render_markdown(result, _settings())
    assert "1 / 2 | 표본 부족 — 판정 보류" in markdown


def test_cli_writes_markdown_and_csv_with_empty_missing_values(tmp_path, capsys):
    log_path = tmp_path / "rescue.log"
    csv_path = tmp_path / "report.csv"
    markdown_path = tmp_path / "report.md"
    log_path.write_text(
        _line("recommend_pipeline", rescue_elapsed_ms=None) + "\n", encoding="utf-8"
    )

    assert (
        aggregate_rescue_chain.main(
            [str(log_path), "--csv", str(csv_path), "--markdown", str(markdown_path)]
        )
        == 0
    )

    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert rows[0] == ["section", "group", "metric", "value"]
    assert ["first_token", "may_auto_relax_true", "p95", ""] in rows
    assert "표본 부족(0/" in markdown
    assert capsys.readouterr().out == ""


def test_cli_min_samples_override_controls_rescue_report_hold(tmp_path):
    log_path = tmp_path / "rescue.log"
    markdown_path = tmp_path / "report.md"
    log_path.write_text(
        _line("recommend_pipeline", rescue_elapsed_ms=9_000) + "\n", encoding="utf-8"
    )

    assert (
        aggregate_rescue_chain.main(
            [str(log_path), "--markdown", str(markdown_path), "--min-samples", "1"]
        )
        == 0
    )

    assert "9.0s 기준선 근접·도달" in markdown_path.read_text(encoding="utf-8")


def test_llm_head_baseline_is_the_documented_section_four_point_one_reference():
    result = aggregate_rescue_chain.aggregate_lines(
        [_line("recommend_pipeline", rescue_elapsed_ms=9_000) for _ in range(3)]
    )

    assert aggregate_rescue_chain.DECOMPOSE_LLM_HEAD_P95_MS == 3_000
    assert "선행 LLM head 포함 기준선 12.0s" in aggregate_rescue_chain.render_markdown(
        result, _settings()
    )


@pytest.mark.asyncio
async def test_expected_field_names_match_actual_zero_and_pipeline_log_records(monkeypatch, caplog):
    """실제 턴의 LogRecord로 집계 입력 키 드리프트를 막는다.

    `test_fanout`의 0건·구제 성공 fixture를 그대로 실행한다. caplog 속성은 formatter가 버리는
    extra도 보존하므로, 로그 렌더링 검증과 별개로 생산 코드가 내보내는 필드 이름 계약을 고정한다.
    """
    expected_zero_fields = {
        "had_candidates",
        "suppressed_categories",
        "category_expanded",
        "post_suppress_fallback_attempted",
        "rescue_elapsed_ms",
        "relax_probes",
        "relax_auto_elapsed_ms",
        "relax_chip_elapsed_ms",
        "may_auto_relax",
        "rescue_stage_narrowed_timeout_ms",
        "rescue_stage_skipped_budget",
        "rescue_budget_mode",
    }
    expected_pipeline_fields = {
        "rescue_elapsed_ms",
        "relax_auto_elapsed_ms",
        "relax_chip_elapsed_ms",
        "may_auto_relax",
        "rescue_stage_narrowed_timeout_ms",
        "rescue_stage_skipped_budget",
        "rescue_budget_mode",
    }
    assert aggregate_rescue_chain.ZERO_RESULT_FIELDS == expected_zero_fields
    assert aggregate_rescue_chain.PIPELINE_FIELDS == expected_pipeline_fields
    await test_fanout.test_recommend_pipeline_logs_rescue_elapsed_when_fallback_succeeds_may_auto_relax_false(
        monkeypatch, caplog
    )
    pipeline_record = next(
        record for record in caplog.records if record.event == "recommend_pipeline"
    )
    assert expected_pipeline_fields <= set(pipeline_record.__dict__)

    caplog.clear()

    async def empty_search(filters, exclude_product_ids=None):
        return test_fanout._res()

    await test_fanout._collect(
        test_fanout.run_buyer_turn(
            test_fanout._req(session_id="field-drift-zero"),
            test_fanout._member_num(),
            llm=test_fanout.FakeLLM(),
            search=empty_search,
            push_fn=test_fanout._RecordingPush(),
            map_categories=test_fanout._two_leg_mapper(),
        )
    )
    zero_record = next(
        record for record in caplog.records if record.event == "recommend_zero_result"
    )
    assert expected_zero_fields <= set(zero_record.__dict__)


def test_structured_log_renders_fields_with_plain_formatter_and_preserves_extra(caplog) -> None:
    """렌더된 sink 문자열과 LogRecord extra를 함께 검증한다."""
    logger = logging.getLogger("tests.rescue-render")
    caplog.set_level(logging.INFO, logger=logger.name)

    log_structured(logger, "recommend_zero_result", rescue_elapsed_ms=123, may_auto_relax=True)

    record = caplog.records[-1]
    assert _PLAIN_FORMATTER.format(record).endswith(
        '{"event": "recommend_zero_result", "rescue_elapsed_ms": 123, "may_auto_relax": true}'
    )
    assert record.rescue_elapsed_ms == 123
    assert record.may_auto_relax is True
    assert record.event == "recommend_zero_result"


@pytest.mark.asyncio
async def test_actual_rescue_turns_round_trip_from_rendered_logs_into_aggregator(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """구제 이벤트는 평문 sink에서도 JSON으로 남고 기존 LogRecord 필드를 보존한다."""
    await test_fanout.test_recommend_pipeline_logs_rescue_elapsed_when_fallback_succeeds_may_auto_relax_false(
        monkeypatch, caplog
    )
    pipeline_record = next(
        record for record in caplog.records if record.event == "recommend_pipeline"
    )
    pipeline_line = _PLAIN_FORMATTER.format(pipeline_record)
    parsed_pipeline = aggregate_rescue_chain.parse_log_line(pipeline_line)
    assert parsed_pipeline is not None
    assert parsed_pipeline["rescue_elapsed_ms"] == pipeline_record.rescue_elapsed_ms
    assert parsed_pipeline["may_auto_relax"] is pipeline_record.may_auto_relax
    assert pipeline_record.rescue_elapsed_ms > 0

    caplog.clear()

    async def empty_search(filters, exclude_product_ids=None):
        return test_fanout._res()

    await test_fanout._collect(
        test_fanout.run_buyer_turn(
            test_fanout._req(session_id="rendered-rescue-zero"),
            test_fanout._member_num(),
            llm=test_fanout.FakeLLM(),
            search=empty_search,
            push_fn=test_fanout._RecordingPush(),
            map_categories=test_fanout._two_leg_mapper(),
        )
    )
    zero_record = next(
        record for record in caplog.records if record.event == "recommend_zero_result"
    )
    zero_line = _PLAIN_FORMATTER.format(zero_record)
    parsed_zero = aggregate_rescue_chain.parse_log_line(zero_line)
    assert parsed_zero is not None
    assert parsed_zero["event"] == "recommend_zero_result"
    assert zero_record.rescue_elapsed_ms == parsed_zero["rescue_elapsed_ms"]

    aggregate = aggregate_rescue_chain.aggregate_lines([pipeline_line, zero_line])
    assert aggregate["first_token"]["event_counts"] == {
        "recommend_zero_result": 1,
        "recommend_pipeline": 1,
    }


def test_chat_request_json_message_stays_parseable_by_existing_observability_aggregator() -> None:
    """전역 JSON formatter를 도입하지 않아 chat_request의 기존 JSON message를 보존한다."""
    logger = logging.getLogger("tests.chat-request-render")
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        0,
        json.dumps({"event": "chat_request", "latencyFirstToken": 12}),
        (),
        None,
    )

    line = _PLAIN_FORMATTER.format(record)

    assert aggregate_observability.parse_log_line(line) == {
        "event": "chat_request",
        "latencyFirstToken": 12,
    }
