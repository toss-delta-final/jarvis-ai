"""벤치마크 Markdown·long-format CSV·raw 산출물을 생성한다."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.core.llm import LOADTEST_MODEL_IDS

# #438 D6/R3 G2 — 접두어 리터럴을 따로 적지 않는다(정본 중복 금지, CLAUDE.md "계약 값의 단일
# 출처는 스키마다"). app/core/llm.py::LOADTEST_MODEL_IDS(정본) 의 값 집합과 **정확히 일치**하는
# id 만 스텁으로 본다 — evals 는 이미 app.core.config 를 import 하므로(manifest.py·runner.py)
# app.core.llm 을 import 해도 새 결합이 생기지 않는다. 접두어 매칭 대신 exact 매칭인 이유: 접두어는
# 그 자체로 또 하나의 파생 리터럴이라, 정본 값이 바뀌면(예: "scripted-stub-fast" → 다른 형식)
# 접두어도 같이 손으로 맞춰야 하는 드리프트 지점이 남는다. exact 매칭은 정본 딕셔너리 값만 보므로
# 그 지점 자체가 없다.
_STUB_MODEL_IDS = frozenset(LOADTEST_MODEL_IDS.values())


def _observed_stub_model_ids(summaries: dict[str, dict[str, Any]]) -> list[str]:
    """모든 group 의 server_metrics.model_ids 를 모아 스텁 id 만 정렬해 돌려준다."""
    ids: set[str] = set()
    for summary in summaries.values():
        model_ids = summary["server_metrics"].get("model_ids")
        if isinstance(model_ids, list):
            ids.update(m for m in model_ids if isinstance(m, str) and m in _STUB_MODEL_IDS)
    return sorted(ids)


def render_markdown(
    summaries: dict[str, dict[str, Any]],
    *,
    join_stats: dict[str, Any],
    server_log_provided: bool,
    sample_size_rationale: str | None,
) -> str:
    """분모와 unknown을 숨기지 않는 결정적 Markdown 보고서를 만든다.

    #438 D6 — 관측된 model id 중 정본 스텁 id(`app.core.llm.LOADTEST_MODEL_IDS`)가 하나라도
    있으면(=부하 테스트 스텁 모드로 돌았음이 서버 로그로 증언됨) 보고서 최상단에 경고 배너를 낸다: 이 수치는
    벤더 지연을 포함하지 않으므로 실 LLM p95 로 인용하면 안 된다. `--server-log` 가 없어
    model id 자체를 확인할 수 없으면(server_log_provided=False) 스텁 여부를 **추정하지
    않고** "LLM 모드 미확인" 이라고만 적는다.
    """
    lines = ["# Benchmark report", ""]
    if not server_log_provided:
        lines.extend(
            [
                "> **server-side 지표 미수집 — `--server-log` 미지정**",
                "> **LLM 모드 미확인** — 스텁(`LLM_PROVIDER=scripted`)인지 실 LLM인지 이 실행만으로는"
                " 판정할 수 없다. 아래 수치를 실 LLM p95 로 인용하지 말 것.",
                "",
            ]
        )
    else:
        stub_model_ids = _observed_stub_model_ids(summaries)
        if stub_model_ids:
            lines.extend(
                [
                    "> ⚠️ **STUB LLM MODE — 이 수치는 벤더 지연을 포함하지 않는다."
                    " 실 LLM p95 로 인용 금지.**",
                    f"> 관측된 스텁 모델 id: {', '.join(stub_model_ids)}",
                    "",
                ]
            )
    lines.extend(
        [
            f"- server join: {join_stats['joined']} / {join_stats['measured']} "
            f"({join_stats['rate'] if join_stats['rate'] is not None else 'unknown'})",
            f"- sample size rationale: {sample_size_rationale or '기본 하한 사용'}",
            "",
            "| group | reliability denominator | latency denominator | success | error | timeout | degrade (known denominator) | degrade unknown | outcome match | outcome mismatch | outcome unknown | p50 ms | p95 ms | p99 ms | max ms | throughput req/s |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group, summary in sorted(summaries.items()):
        latency = summary["latency"]["client_ttft_ms"]
        throughput = summary["throughput"]
        degrade_display = (
            f"{summary['degrade_count']}/{summary['degrade_known_denominator']}"
            if summary["degrade_rate"] is not None
            else "unknown"
        )
        lines.append(
            f"| {group} | {summary['reliability_denominator']} | {summary['latency_denominator']} | "
            f"{summary['success_count']}/{summary['reliability_denominator']} | "
            f"{summary['error_count']}/{summary['reliability_denominator']} | "
            f"{summary['timeout_count']}/{summary['reliability_denominator']} | "
            f"{degrade_display}"
        )
        lines[-1] += (
            f" | {summary['degrade_unknown_count']} | "
            f"{summary['outcome_match_count']} | "
            f"{summary['outcome_mismatch_count']} | "
            f"{summary['outcome_unknown_count']} | "
            f"{latency['p50'] if latency['p50'] is not None else 'unknown'} | "
            f"{latency['p95'] if latency['p95'] is not None else 'unknown'} | "
            f"{latency['p99'] if latency['p99'] is not None else 'unknown'} | "
            f"{latency['max'] if latency['max'] is not None else 'unknown'} | "
            f"{throughput['requests_per_s'] if throughput['requests_per_s'] is not None else 'unknown'} |"
        )
        if latency["p99_omitted_reason"]:
            lines.append(f"\n- `{group}` p99 omitted: `{latency['p99_omitted_reason']}`")
        lines.append(
            f"- `{group}` throughput: {throughput['completed_requests']} requests / "
            f"{throughput['elapsed_s']} seconds"
        )
        lines.append(
            f"- `{group}` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) "
            f"요청만 포함. 제외 {summary['latency_excluded']}건"
        )
        if summary["outcome_reason_counts"]:
            reason_text = ", ".join(
                f"{reason}={count}" for reason, count in summary["outcome_reason_counts"].items()
            )
            lines.append(f"- `{group}` outcome reasons: {reason_text}")
        server = summary["server_metrics"]
        lines.append(
            f"- `{group}` server metrics: joined={server['joined_samples']}, "
            f"cost=${server['cost_usd_total'] if server['cost_usd_total'] is not None else 'unknown'} "
            f"(samples={server['cost_sample_count']}, unknown={server['cost_unknown_count']}, "
            f"price_missing={server['cost_price_missing_count']}), "
            f"promptTokens={server['prompt_tokens_total'] if server['prompt_tokens_total'] is not None else 'unknown'}, "
            f"completionTokens={server['completion_tokens_total'] if server['completion_tokens_total'] is not None else 'unknown'}, "
            f"models={server['model_ids'] if server['model_ids'] is not None else 'unknown'}"
        )
        if summary["phase"] == "cold":
            lines.append(
                f"- `{group}` cold 표본은 n={summary['reliability_denominator']}로 작아 "
                "CI·p95를 강한 성능 주장에 사용하지 않는다."
            )
    lines.extend(
        [
            "",
            "> p50/p95는 #137 `scripts/aggregate_observability.py`와 동일한 최근접 순위 정의다.",
            "> provider TTFT는 chat_request 로그에 없어 `unknown` "
            "(`not_in_chat_request_log`)으로 기록한다.",
            "> reasoning/cache token은 서버가 내보내지 않아 `unknown` "
            "(`not_emitted_by_server`)으로 기록한다.",
            "> client TTFT는 커넥션 재사용 시 request-byte send 기준에 수렴한다. 풀에 유휴 연결이 "
            "없으면 httpx가 정확한 byte-send hook을 제공하지 않아 DNS·TCP·TLS 연결 수립 및 "
            "풀 대기 시간이 포함될 수 있다.",
            "",
        ]
    )
    return "\n".join(lines)


def csv_rows(
    summaries: dict[str, dict[str, Any]], join_stats: dict[str, Any]
) -> list[tuple[str, str, str, Any]]:
    """#137과 같은 section,group,metric,value long-format 행을 만든다."""
    rows: list[tuple[str, str, str, Any]] = []
    for group, summary in sorted(summaries.items()):
        for metric in (
            "reliability_denominator",
            "latency_denominator",
            "latency_excluded",
            "success_count",
            "success_rate",
            "error_count",
            "error_rate",
            "timeout_count",
            "timeout_rate",
            "degrade_count",
            "degrade_known_denominator",
            "degrade_unknown_count",
            "degrade_rate",
            "outcome_match_count",
            "outcome_mismatch_count",
            "outcome_unknown_count",
        ):
            rows.append(("reliability", group, metric, summary[metric]))
        for reason, count in summary["outcome_reason_counts"].items():
            rows.append(("outcome_reason", group, reason, count))
        for latency_name, latency_stats in summary["latency"].items():
            for metric, value in latency_stats.items():
                if isinstance(value, dict):
                    for child, child_value in value.items():
                        rows.append(
                            ("latency", group, f"{latency_name}.{metric}.{child}", child_value)
                        )
                else:
                    rows.append(("latency", group, f"{latency_name}.{metric}", value))
        for metric, value in summary["server_metrics"].items():
            rows.append(
                (
                    "server_metrics",
                    group,
                    metric,
                    ",".join(value) if isinstance(value, list) else value,
                )
            )
        for metric, value in summary["throughput"].items():
            rows.append(("throughput", group, metric, value))
    for metric, value in join_stats.items():
        rows.append(("server_join", "all", metric, value))
    return rows


def write_artifacts(
    directory: Path,
    *,
    report: str,
    summaries: dict[str, dict[str, Any]],
    join_stats: dict[str, Any],
    raw_records: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    """새 디렉터리에 네 파일을 한 번만 기록한다."""
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "report.md").write_text(report, encoding="utf-8")
    with (directory / "metrics.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("section", "group", "metric", "value"))
        writer.writerows(
            (section, group, metric, "" if value is None else value)
            for section, group, metric, value in csv_rows(summaries, join_stats)
        )
    with (directory / "raw.jsonl").open("w", encoding="utf-8") as file:
        for record in raw_records:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
