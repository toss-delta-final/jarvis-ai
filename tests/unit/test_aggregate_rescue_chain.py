"""구제 체인 실빈도·지연 집계의 계산·출력 계약."""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pytest

from scripts import aggregate_rescue_chain
from tests.unit import test_fanout


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
        record for record in caplog.records if record.msg == "recommend_pipeline"
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
    zero_record = next(record for record in caplog.records if record.msg == "recommend_zero_result")
    assert expected_zero_fields <= set(zero_record.__dict__)
