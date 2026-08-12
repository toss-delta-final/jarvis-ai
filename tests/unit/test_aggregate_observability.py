"""관측 로그 집계 스크립트의 계산·출력 계약."""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pytest

from scripts import aggregate_observability


def _line(**overrides: object) -> str:
    record: dict[str, object] = {
        "event": "chat_request",
        "requestId": "req-1",
        "role": "member",
        "latencyFirstToken": 100.0,
        "latencyTotal": 500.0,
        "model": ["haiku"],
        "lane": "recommend",
        "degraded": False,
        "degradeReason": None,
        "costUsd": 0.01,
        "errorType": None,
        "streamStatus": "done",
    }
    record.update(overrides)
    return json.dumps(record)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 2, 3, 4, 5], {50: 3, 95: 5, 99: 5}),
        ([1, 2, 3, 4], {50: 2, 95: 4, 99: 4}),
        ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], {50: 50, 90: 90, 95: 100}),
        ([42], {50: 42, 95: 42, 99: 42}),
        ([], {50: None, 95: None, 99: None}),
    ],
)
def test_percentile_matches_nearest_rank_hand_calculation(values, expected):
    for p, value in expected.items():
        assert aggregate_observability.percentile(values, p) == value


def test_percentile_does_not_reorder_input():
    values = [5, 1, 4, 2, 3]

    aggregate_observability.percentile(values, 95)

    assert values == [5, 1, 4, 2, 3]


def test_parse_log_line_accepts_json_and_logging_prefix():
    raw = _line(requestId="plain")
    prefixed = f"2026-08-02 10:00:00,123 INFO observability {_line(requestId='prefixed')}"

    assert aggregate_observability.parse_log_line(raw)["requestId"] == "plain"
    assert aggregate_observability.parse_log_line(prefixed)["requestId"] == "prefixed"


@pytest.mark.parametrize(
    "line",
    ["broken", "", json.dumps([{"event": "chat_request"}]), '{"event":"other"}'],
)
def test_parse_log_line_skips_invalid_or_unrelated_lines(line):
    assert aggregate_observability.parse_log_line(line) is None


def test_synthetic_rollup_matches_hand_calculation():
    lines = [
        _line(requestId="1", role="seller", latencyFirstToken=100, latencyTotal=1000, costUsd=0.01),
        _line(requestId="2", role="seller", latencyFirstToken=200, latencyTotal=2000, costUsd=0.02),
        _line(
            requestId="3",
            role="member",
            latencyFirstToken=300,
            latencyTotal=3000,
            degraded=True,
            degradeReason="rerank_fallback",
            costUsd=0.03,
        ),
        _line(
            requestId="4",
            role="guest",
            latencyFirstToken=400,
            latencyTotal=4000,
            errorType="UPSTREAM_TIMEOUT",
            costUsd=0.04,
        ),
        _line(requestId="5", role="member", latencyFirstToken=500, latencyTotal=5000, costUsd=0.05),
        _line(requestId="6", role="member", latencyFirstToken=600, latencyTotal=6000, costUsd=0.06),
        _line(requestId="7", role="guest", latencyFirstToken=700, latencyTotal=7000, costUsd=0.07),
        _line(requestId="8", role="seller", latencyFirstToken=800, latencyTotal=8000, costUsd=0.08),
        _line(requestId="9", role="member", latencyFirstToken=900, latencyTotal=9000, costUsd=0.09),
        _line(
            requestId="10", role="guest", latencyFirstToken=1000, latencyTotal=10000, costUsd=0.10
        ),
        "not-json",
        '{"event":"healthcheck"}',
    ]

    result = aggregate_observability.aggregate_lines(lines)

    assert result["total_lines"] == 12
    assert result["total_turns"] == 10
    assert result["skipped_lines"] == 2
    assert result["latency"]["overall"]["first_token"] == {
        "n": 10,
        "p50": 500.0,
        "p95": 1000.0,
        "p99": 1000.0,
    }
    assert result["degrade_rate"] == pytest.approx(0.1)
    assert result["error_rate"] == pytest.approx(0.1)
    assert result["cost"]["overall"]["total"] == pytest.approx(0.55)
    assert result["cost"]["overall"]["average"] == pytest.approx(0.055)
    assert result["cost"]["overall"]["min"] == pytest.approx(0.01)
    assert result["cost"]["overall"]["max"] == pytest.approx(0.10)
    assert result["cost"]["role"]["seller"]["min"] == pytest.approx(0.01)
    assert result["cost"]["role"]["seller"]["max"] == pytest.approx(0.08)
    assert result["cost"]["role"]["member"]["min"] == pytest.approx(0.03)
    assert result["cost"]["role"]["member"]["max"] == pytest.approx(0.09)
    assert result["cost"]["role"]["guest"]["min"] == pytest.approx(0.04)
    assert result["cost"]["role"]["guest"]["max"] == pytest.approx(0.10)


def test_rejection_is_included_in_degrade_and_error_denominators():
    rejection = json.dumps(
        {
            "event": "chat_request",
            "requestId": "reject",
            "degraded": False,
            "errorType": "RATE_LIMITED",
            "streamStatus": None,
            "lane": None,
        }
    )
    result = aggregate_observability.aggregate_lines(
        [
            _line(requestId="ok"),
            _line(requestId="degraded", degraded=True, degradeReason="fanout_partial"),
            rejection,
        ]
    )

    assert result["total_turns"] == 3
    assert result["rejection_turns"] == 1
    assert result["degrade_rate"] == pytest.approx(1 / 3)
    assert result["error_rate"] == pytest.approx(1 / 3)
    assert result["degrade_reasons"]["fanout_partial"] == {"count": 1, "rate": pytest.approx(1 / 3)}
    assert result["error_types"]["RATE_LIMITED"] == {"count": 1, "rate": pytest.approx(1 / 3)}


def test_missing_and_null_cost_are_excluded_and_marked_partial():
    without_cost = json.loads(_line(requestId="missing"))
    without_cost.pop("costUsd")
    result = aggregate_observability.aggregate_lines(
        [
            _line(requestId="priced", costUsd=0.30),
            _line(requestId="null", costUsd=None),
            json.dumps(without_cost),
        ]
    )

    overall = result["cost"]["overall"]
    assert overall["total"] == pytest.approx(0.30)
    assert overall["average"] == pytest.approx(0.30)
    assert overall["sample_count"] == 1
    assert overall["turn_count"] == 3
    assert overall["coverage"] == pytest.approx(1 / 3)
    assert overall["partial"] is True
    markdown = aggregate_observability.render_markdown(result, _settings())
    assert "비용 표본 1 / 전체 3 (33.3%)" in markdown
    assert "부분 집계(partial)" in markdown


def test_cost_stats_min_max_are_none_for_zero_samples_and_render_as_dash():
    """표본이 아예 없으면(빈 로그) min/max가 None이고 Markdown엔 '-'로 표시돼야 한다."""
    result = aggregate_observability.aggregate_lines([])

    assert result["cost"]["overall"]["min"] is None
    assert result["cost"]["overall"]["max"] is None
    markdown = aggregate_observability.render_markdown(result, _settings())
    assert "| 전체 | 비용 표본 0 / 전체 0 (-) | - | - | - | - | - | 완전 집계 |" in markdown


def test_cost_stats_min_max_are_none_when_turns_exist_but_no_costusd():
    """턴은 있지만 costUsd가 전부 결측이면(표본 0건) min/max는 None이어야 한다."""
    without_cost = json.loads(_line(requestId="missing"))
    without_cost.pop("costUsd")
    result = aggregate_observability.aggregate_lines([json.dumps(without_cost)])

    overall = result["cost"]["overall"]
    assert overall["sample_count"] == 0
    assert overall["turn_count"] == 1
    assert overall["min"] is None
    assert overall["max"] is None


def test_model_fan_in_duplicates_cost_across_model_groups():
    """한 턴이 모델 2개를 쓰면 costUsd 전액이 두 모델 그룹 모두에 들어간다(fan-in, §4.3)."""
    result = aggregate_observability.aggregate_lines(
        [_line(model=["haiku", "sonnet"], costUsd=0.5)]
    )

    assert result["cost"]["model"]["haiku"]["total"] == pytest.approx(0.5)
    assert result["cost"]["model"]["sonnet"]["total"] == pytest.approx(0.5)
    assert result["cost"]["model"]["haiku"]["sample_count"] == 1
    assert result["cost"]["model"]["sonnet"]["sample_count"] == 1
    # 모델별 합계 합(1.0)이 전체 합계(0.5)보다 크다 — fan-in 중복의 표식.
    assert (
        result["cost"]["model"]["haiku"]["total"] + result["cost"]["model"]["sonnet"]["total"]
        > result["cost"]["overall"]["total"]
    )


@pytest.mark.parametrize(
    ("length", "expected_bucket"),
    [
        (0, "<50"),
        (49, "<50"),
        (50, "50-150"),
        (149, "50-150"),
        (150, "150-400"),
        (399, "150-400"),
        (400, "400+"),
        (10_000, "400+"),
    ],
)
def test_length_bucket_boundaries_match_config_edges(length, expected_bucket):
    assert (
        aggregate_observability._length_bucket(length, (50, 150, 400)) == expected_bucket
    )


@pytest.mark.parametrize("value", [None, -1, "12", 12.5, True])
def test_length_bucket_treats_missing_negative_non_int_as_unknown(value):
    assert aggregate_observability._length_bucket(value, (50, 150, 400)) == "unknown"


def test_cost_usd_partial_marks_complete_coverage_as_partial():
    result = aggregate_observability.aggregate_lines(
        [_line(requestId="1", costUsd=0.1, costUsdPartial=True), _line(requestId="2", costUsd=0.2)]
    )

    assert result["cost"]["overall"]["coverage"] == 1.0
    assert result["cost"]["overall"]["partial"] is True


def test_rejection_zero_cost_does_not_dilute_cost_average():
    """스트림 전 거부는 LLM 을 안 부르고 costUsd 0 을 싣는다 — 비용 분모에 들어가면 안 된다.

    분모에 넣으면 실행 턴 평균 0.02 가 0.01 로 반토막 나는데 커버리지는 100% 로 보여
    "완전히 측정된 값"으로 오독된다. degrade·error 분모(전체 턴)와는 일부러 다르다.
    """
    rejection = json.dumps(
        {
            "event": "chat_request",
            "errorType": "RATE_LIMITED",
            "streamStatus": None,
            "lane": None,
            "degraded": False,
            "costUsd": 0.0,
        }
    )
    result = aggregate_observability.aggregate_lines(
        [_line(requestId=str(index), costUsd=0.02) for index in range(10)] + [rejection] * 10
    )

    overall = result["cost"]["overall"]
    assert overall["average"] == pytest.approx(0.02)
    assert overall["total"] == pytest.approx(0.2)
    assert overall["turn_count"] == 10
    assert result["cost"]["excluded_rejection_turns"] == 10
    # 거부는 lane 이 null 이라 unknown 레인을 만들어내지도 않아야 한다.
    assert set(result["cost"]["lane"]) == {"recommend"}
    # 반면 degrade·error 분모는 그대로 전체 20턴이다.
    assert result["total_turns"] == 20
    assert result["error_rate"] == pytest.approx(0.5)


def test_overview_and_cost_footnote_count_unexecuted_turns_identically():
    """같은 라벨의 두 숫자가 한 리포트 안에서 갈리면 안 된다.

    개요의 미실행 턴과 비용 각주의 제외 턴은 같은 개념이다. 정의를 따로 쓰면(키 존재 검사 vs
    숫자 검사) `latencyTotal` 이 null 인 줄에서 0 과 1 로 갈렸다.
    """
    broken = json.dumps(
        {
            "event": "chat_request",
            "streamStatus": None,
            "latencyTotal": None,
            "degraded": False,
            "errorType": None,
            "costUsd": 0.0,
            "lane": "recommend",
        }
    )
    result = aggregate_observability.aggregate_lines([_line(requestId="ok"), broken])

    assert result["rejection_turns"] == result["cost"]["excluded_rejection_turns"] == 1
    markdown = aggregate_observability.render_markdown(result, _settings())
    assert "| 미실행 턴(스트림 전 거부 등) | 1 |" in markdown
    assert "미실행 턴 1건" in markdown


def test_degrade_reason_distribution_uses_the_same_predicate_as_the_rate():
    """사유 분포와 degrade 율이 다른 술어를 쓰면 사유 비율 합이 degrade 율을 넘는다."""
    result = aggregate_observability.aggregate_lines(
        [
            _line(requestId="real", degraded=True, degradeReason="rerank_fallback"),
            # degraded 가 아닌데 사유만 남은 줄 — 분포에 들어가면 표가 자기모순이 된다.
            _line(requestId="stale", degraded=False, degradeReason="rerank_fallback"),
        ]
    )

    assert result["degrade_rate"] == pytest.approx(0.5)
    assert result["degrade_reasons"]["rerank_fallback"]["count"] == 1
    assert sum(stats["rate"] for stats in result["degrade_reasons"].values()) == pytest.approx(
        result["degrade_rate"]
    )


def test_null_latency_is_excluded_instead_of_counted_as_zero():
    result = aggregate_observability.aggregate_lines(
        [
            _line(requestId="null", latencyFirstToken=None),
            _line(requestId="value", latencyFirstToken=200),
        ]
    )

    assert result["latency"]["overall"]["first_token"] == {
        "n": 1,
        "p50": 200.0,
        "p95": 200.0,
        "p99": 200.0,
    }


def test_model_list_fans_one_turn_into_each_model_group():
    result = aggregate_observability.aggregate_lines(
        [_line(model=["haiku", "sonnet"], latencyFirstToken=123, latencyTotal=456)]
    )

    assert result["latency"]["model"]["haiku"]["first_token"]["n"] == 1
    assert result["latency"]["model"]["haiku"]["total"]["p50"] == 456.0
    assert result["latency"]["model"]["sonnet"]["first_token"]["p50"] == 123.0


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "slo_first_token_ms": 10_000,
        "slo_total_seller_ms": 90_000,
        "slo_total_buyer_ms": 30_000,
        "degrade_rate_alert_threshold": 0.10,
        "degrade_alert_min_samples": 50,
        "observability_length_buckets": (50, 150, 400),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_log(tmp_path, records: list[str]):
    path = tmp_path / "observability.log"
    path.write_text("\n".join(records) + ("\n" if records else ""), encoding="utf-8")
    return path


def test_main_empty_log_uses_config_defaults_and_returns_zero(tmp_path, monkeypatch):
    path = _write_log(tmp_path, [])
    real_get_settings = aggregate_observability.get_settings
    settings = real_get_settings()
    assert settings.degrade_rate_alert_threshold == 0.10
    assert settings.degrade_alert_min_samples == 50

    assert aggregate_observability.main([str(path)]) == 0


def test_main_returns_one_for_twenty_percent_degrade_over_default_minimum(tmp_path, monkeypatch):
    path = _write_log(
        tmp_path,
        [
            _line(
                requestId=str(index),
                degraded=index < 20,
                degradeReason="fallback" if index < 20 else None,
            )
            for index in range(100)
        ],
    )
    monkeypatch.setattr(aggregate_observability, "get_settings", lambda: _settings())

    assert aggregate_observability.main([str(path)]) == 1


def test_main_holds_alert_when_samples_are_below_default_minimum(tmp_path, monkeypatch, capsys):
    path = _write_log(
        tmp_path,
        [
            _line(requestId=str(index), degraded=True, degradeReason="fallback")
            for index in range(10)
        ],
    )
    monkeypatch.setattr(aggregate_observability, "get_settings", lambda: _settings())

    assert aggregate_observability.main([str(path)]) == 0
    assert "표본 부족(10/50) — 판정 보류" in capsys.readouterr().out


def test_no_alert_forces_zero_exit(tmp_path, monkeypatch):
    path = _write_log(tmp_path, [_line(degraded=True, degradeReason="fallback")])
    monkeypatch.setattr(aggregate_observability, "get_settings", lambda: _settings())

    assert (
        aggregate_observability.main(
            [str(path), "--degrade-threshold", "0", "--min-samples", "1", "--no-alert"]
        )
        == 0
    )


def test_csv_and_markdown_outputs_have_long_format_and_missing_markers(tmp_path, monkeypatch):
    log_path = _write_log(tmp_path, ["broken"])
    csv_path = tmp_path / "report.csv"
    markdown_path = tmp_path / "report.md"
    monkeypatch.setattr(aggregate_observability, "get_settings", lambda: _settings())

    assert (
        aggregate_observability.main(
            [str(log_path), "--csv", str(csv_path), "--markdown", str(markdown_path)]
        )
        == 0
    )

    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert rows[0] == ["section", "group", "metric", "value"]
    assert any(
        row[:3] == ["latency", "overall.first_token", "p95"] and row[3] == "" for row in rows
    )
    for heading in ["## 개요", "## 지연", "## 비용", "## degrade", "## error", "## SLO", "## 알림"]:
        assert heading in markdown
    assert "| 전체 first_token | 0 | - | - | - |" in markdown


def test_cost_table_has_min_max_columns_and_dimension_group_labels(tmp_path, monkeypatch):
    """비용 표에 최소/최대 열이 생기고, lane/role/model/length 그룹이 latency 표와 같은
    ``dimension:group`` 라벨 규약으로 나와야 한다."""
    monkeypatch.setattr(aggregate_observability, "get_settings", lambda: _settings())
    log_path = _write_log(
        tmp_path,
        [
            _line(
                requestId="1",
                role="seller",
                lane="analysis",
                model=["haiku"],
                costUsd=0.10,
                messageLength=10,
            ),
            _line(
                requestId="2",
                role="seller",
                lane="analysis",
                model=["haiku"],
                costUsd=0.20,
                messageLength=200,
            ),
        ],
    )
    csv_path = tmp_path / "report.csv"
    markdown_path = tmp_path / "report.md"

    assert (
        aggregate_observability.main(
            [str(log_path), "--csv", str(csv_path), "--markdown", str(markdown_path)]
        )
        == 0
    )

    markdown = markdown_path.read_text(encoding="utf-8")
    assert (
        "| 그룹 | 표본/전체 | 커버리지 | 합계(USD) | 평균(USD) | 최소(USD) | 최대(USD) | 상태 |"
        in markdown
    )
    assert "| lane:analysis |" in markdown
    assert "| role:seller |" in markdown
    assert "| model:haiku |" in markdown
    assert "| length:<50 |" in markdown
    assert "| length:150-400 |" in markdown
    assert "모델별 비용은 fan-in 귀속이다" in markdown

    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    cost_groups = {row[1] for row in rows if row[0] == "cost"}
    assert {
        "overall",
        "lane:analysis",
        "role:seller",
        "model:haiku",
        "length:<50",
        "length:150-400",
    } <= cost_groups
    assert any(row[0] == "cost" and row[2] == "min" for row in rows)
    assert any(row[0] == "cost" and row[2] == "max" for row in rows)


def test_slo_reports_overage_rate_and_leaves_unknown_role_unjudged():
    result = aggregate_observability.aggregate_lines(
        [
            _line(role="seller", latencyFirstToken=11_000, latencyTotal=100_000),
            _line(role="seller", latencyFirstToken=9_000, latencyTotal=80_000),
            _line(role="member", latencyFirstToken=12_000, latencyTotal=40_000),
            _line(role=None, latencyFirstToken=20_000, latencyTotal=200_000),
        ]
    )

    markdown = aggregate_observability.render_markdown(result, _settings())
    assert "| 첫 토큰 전체 | 10000 | 20000 | 초과 | 75.0% | 4 |" in markdown
    assert "| total seller | 90000 | 100000 | 초과 | 50.0% | 2 |" in markdown
    assert "| total member | 30000 | 40000 | 초과 | 100.0% | 1 |" in markdown
    assert "| total unknown | - | 200000 | - | - | 1 |" in markdown
