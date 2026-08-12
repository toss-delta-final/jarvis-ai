"""이슈 #137 관측 로그 집계 — EVAL-OBS-PLAN-001 §3.3.

파일 또는 표준 입력의 ``chat_request`` 로그를 읽어 지연·비용·degrade·error·SLO를 Markdown과
long-format CSV로 요약한다. 로깅 접두사가 붙은 줄과 순수 JSON 줄을 모두 받으며 DB·네트워크는
사용하지 않는다. ``model`` 목록은 fan-in 규칙으로 한 턴의 지연을 사용한 각 모델에 귀속하므로
모델별 표본 수의 합은 전체 표본 수보다 클 수 있다.

사용법::

    uv run python scripts/aggregate_observability.py [LOGFILE ...] [--csv PATH]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from numbers import Real
from pathlib import Path
from typing import Any

from app.core.config import get_settings


def parse_log_line(line: str) -> dict[str, Any] | None:
    """순수 JSON 또는 로깅 접두사 뒤의 ``chat_request`` 레코드를 안전하게 파싱한다."""
    stripped = line.strip()
    if not stripped:
        return None
    record: Any
    try:
        record = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        start = stripped.find("{")
        if start < 0:
            return None
        try:
            record = json.loads(stripped[start:])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if not isinstance(record, dict) or record.get("event") != "chat_request":
        return None
    return record


def percentile(values: Sequence[float], p: float) -> float | None:
    """최근접 순위: ``index = max(ceil(p / 100 * n) - 1, 0)`` 값을 반환한다."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(p / 100 * len(ordered)) - 1, 0)
    return float(ordered[min(index, len(ordered) - 1)])


def _number(value: object) -> float | None:
    """bool·비유한 값을 제외한 숫자만 집계용 float로 좁힌다."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _dimension(value: object, allowed: set[str] | None = None) -> str:
    """문자열 차원을 정규화하고 누락·미지 값은 unknown으로 묶는다."""
    if not isinstance(value, str) or not value or (allowed is not None and value not in allowed):
        return "unknown"
    return value


def _models(value: object) -> list[str]:
    """모델 fan-in 그룹 목록을 중복 없이 만든다."""
    if not isinstance(value, list):
        return ["unknown"]
    models = [item for item in value if isinstance(item, str) and item]
    return list(dict.fromkeys(models)) or ["unknown"]


def _length_bucket(value: object, boundaries: Sequence[int]) -> str:
    """messageLength를 config 경계값 기준 버킷 라벨로 정규화한다."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "unknown"
    ordered = sorted(boundaries)
    lower = 0
    for edge in ordered:
        if value < edge:
            return f"<{edge}" if lower == 0 else f"{lower}-{edge}"
        lower = edge
    return f"{lower}+"


def _latency_stats(values: Sequence[float]) -> dict[str, int | float | None]:
    """지연 표본 수와 최근접 순위 p50/p95/p99를 반환한다."""
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def _executed(record: dict[str, Any]) -> bool:
    """스트림이 실제로 돈 턴인지 판정한다 — ``latencyTotal`` 은 finish 경로에서만 찍힌다.

    스트림 전 거부(``emit_rejection``: 429/409/504)는 LLM 을 한 번도 부르지 않았는데도
    ``costUsd: 0.0`` 을 싣는다. 이걸 비용 분모에 넣으면 "쿼리당 비용"이 0 쪽으로 희석되면서
    커버리지는 100% 로 보여 **완전히 측정된 것처럼 읽힌다**. 그래서 비용만 실행 턴으로 좁힌다.
    """
    return _number(record.get("latencyTotal")) is not None


def _cost_stats(records: Sequence[dict[str, Any]]) -> dict[str, int | float | bool | None]:
    """숫자인 비용만 합산하고 실행 턴 대비 커버리지·최소/최대·partial 상태를 보존한다."""
    values = [
        number for record in records if (number := _number(record.get("costUsd"))) is not None
    ]
    turn_count = len(records)
    sample_count = len(values)
    total = sum(values) if values else None
    return {
        "sample_count": sample_count,
        "turn_count": turn_count,
        "coverage": sample_count / turn_count if turn_count else None,
        "total": total,
        "average": total / sample_count if total is not None else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "partial": sample_count < turn_count
        or any(record.get("costUsdPartial") is True for record in records),
    }


def aggregate_lines(lines: Iterable[str]) -> dict[str, Any]:
    """로그 줄을 읽어 발표용 롤업을 만든다."""
    records: list[dict[str, Any]] = []
    total_lines = 0
    for line in lines:
        total_lines += 1
        record = parse_log_line(line)
        if record is not None:
            records.append(record)

    total_turns = len(records)
    executed_records = [record for record in records if _executed(record)]
    # 개요의 "미실행 턴"과 비용 각주의 "제외한 턴"은 **같은 수**여야 한다. 정의를 두 군데서
    # 따로 쓰면(예: 키 존재 검사 vs 숫자 검사) 같은 라벨의 두 숫자가 한 리포트 안에서 갈린다.
    not_executed_count = total_turns - len(executed_records)
    degraded_count = sum(record.get("degraded") is True for record in records)
    error_count = sum(record.get("errorType") is not None for record in records)
    # 사유 분포는 degrade 율과 **같은 술어**(`degraded is True`)로 세야 한다. 사유만 있고
    # degraded 가 아닌 줄을 세면 사유 비율의 합이 degrade 율을 넘어 표가 자기모순이 된다.
    degrade_reasons = Counter(
        str(record["degradeReason"])
        for record in records
        if record.get("degraded") is True and record.get("degradeReason") is not None
    )
    error_types = Counter(
        str(record["errorType"]) for record in records if record.get("errorType") is not None
    )

    latency_values: dict[str, dict[str, dict[str, list[float]]]] = {
        "role": defaultdict(lambda: {"first_token": [], "total": []}),
        "lane": defaultdict(lambda: {"first_token": [], "total": []}),
        "model": defaultdict(lambda: {"first_token": [], "total": []}),
    }
    overall_latency: dict[str, list[float]] = {"first_token": [], "total": []}
    lane_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    role_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    model_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    length_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    settings = get_settings()

    for record in records:
        role = _dimension(record.get("role"), {"seller", "member", "guest"})
        lane = _dimension(record.get("lane"))
        models = _models(record.get("model"))
        if _executed(record):
            lane_records[lane].append(record)
            role_records[role].append(record)
            length_records[
                _length_bucket(record.get("messageLength"), settings.observability_length_buckets)
            ].append(record)
            for model in models:
                model_records[model].append(record)
        for metric, field in (("first_token", "latencyFirstToken"), ("total", "latencyTotal")):
            value = _number(record.get(field))
            if value is None:
                continue
            overall_latency[metric].append(value)
            latency_values["role"][role][metric].append(value)
            latency_values["lane"][lane][metric].append(value)
            for model in models:
                latency_values["model"][model][metric].append(value)

    latency: dict[str, Any] = {
        "overall": {metric: _latency_stats(values) for metric, values in overall_latency.items()}
    }
    for dimension, groups in latency_values.items():
        latency[dimension] = {
            group: {metric: _latency_stats(values) for metric, values in metrics.items()}
            for group, metrics in sorted(groups.items())
        }

    return {
        "total_lines": total_lines,
        "total_turns": total_turns,
        "skipped_lines": total_lines - total_turns,
        "rejection_turns": not_executed_count,
        "degraded_count": degraded_count,
        "degrade_rate": degraded_count / total_turns if total_turns else None,
        "degrade_reasons": {
            code: {"count": count, "rate": count / total_turns}
            for code, count in sorted(degrade_reasons.items())
        },
        "error_count": error_count,
        "error_rate": error_count / total_turns if total_turns else None,
        "error_types": {
            code: {"count": count, "rate": count / total_turns}
            for code, count in sorted(error_types.items())
        },
        "latency": latency,
        "cost": {
            "overall": _cost_stats(executed_records),
            "lane": {lane: _cost_stats(items) for lane, items in sorted(lane_records.items())},
            "role": {role: _cost_stats(items) for role, items in sorted(role_records.items())},
            "model": {
                model: _cost_stats(items) for model, items in sorted(model_records.items())
            },
            "length": {
                bucket: _cost_stats(items) for bucket, items in sorted(length_records.items())
            },
            # 비용 분모에서 뺀 미실행 턴 수(개요의 값과 동일한 계산). 숨기면 "왜 비용 턴 수가
            # 전체보다 적나"에 답할 수 없고, 조용히 빼면 분모 조작과 구분되지 않는다.
            "excluded_rejection_turns": not_executed_count,
        },
        "records": records,
    }


def _display(value: object) -> str:
    """Markdown 숫자를 간결히 표시하고 결측은 대시로 남긴다."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def _percent(value: float | None) -> str:
    """Markdown 비율을 한 자리 퍼센트로 표시한다."""
    return "-" if value is None else f"{value * 100:.1f}%"


def _slo_rows(result: dict[str, Any], settings: Any) -> list[dict[str, Any]]:
    """전체 TTFT와 역할별 total SLO 행을 계산한다."""
    records = result["records"]
    first_values = [
        value
        for record in records
        if (value := _number(record.get("latencyFirstToken"))) is not None
    ]
    first_p95 = percentile(first_values, 95)
    first_target = settings.slo_first_token_ms
    rows = [
        {
            "group": "첫 토큰 전체",
            "target": first_target,
            "p95": first_p95,
            "status": None if first_p95 is None else ("초과" if first_p95 > first_target else "OK"),
            "exceedance_rate": (
                sum(value > first_target for value in first_values) / len(first_values)
                if first_values
                else None
            ),
            "n": len(first_values),
        }
    ]
    role_targets = {
        "seller": settings.slo_total_seller_ms,
        "member": settings.slo_total_buyer_ms,
        "guest": settings.slo_total_buyer_ms,
        "unknown": None,
    }
    roles_present = {
        _dimension(record.get("role"), {"seller", "member", "guest"}) for record in records
    }
    for role in [
        name for name in ("seller", "member", "guest", "unknown") if name in roles_present
    ]:
        values = [
            value
            for record in records
            if _dimension(record.get("role"), {"seller", "member", "guest"}) == role
            and (value := _number(record.get("latencyTotal"))) is not None
        ]
        target = role_targets[role]
        p95 = percentile(values, 95)
        rows.append(
            {
                "group": f"total {role}",
                "target": target,
                "p95": p95,
                "status": None
                if target is None or p95 is None
                else ("초과" if p95 > target else "OK"),
                "exceedance_rate": (
                    sum(value > target for value in values) / len(values)
                    if target is not None and values
                    else None
                ),
                "n": len(values),
            }
        )
    return rows


def _alert_text(result: dict[str, Any], threshold: float, min_samples: int) -> tuple[str, bool]:
    """표본 하한과 degrade 임계의 AND 조건을 판정한다."""
    total_turns = result["total_turns"]
    degrade_rate = result["degrade_rate"]
    if total_turns < min_samples:
        return f"표본 부족({total_turns}/{min_samples}) — 판정 보류", False
    fired = degrade_rate is not None and degrade_rate > threshold
    if fired:
        return f"알림 발동 — degrade율 {_percent(degrade_rate)} > 임계 {_percent(threshold)}", True
    return f"정상 — degrade율 {_percent(degrade_rate)} ≤ 임계 {_percent(threshold)}", False


def render_markdown(
    result: dict[str, Any],
    settings: Any,
    *,
    threshold: float | None = None,
    min_samples: int | None = None,
) -> str:
    """집계 결과를 고정 순서의 Markdown 요약으로 렌더링한다."""
    threshold = settings.degrade_rate_alert_threshold if threshold is None else threshold
    min_samples = settings.degrade_alert_min_samples if min_samples is None else min_samples
    lines = [
        "# 관측 로그 집계",
        "",
        "## 개요",
        "",
        "| 항목 | 값 |",
        "|---|---:|",
        f"| 읽은 줄 | {result['total_lines']} |",
        f"| 채택한 chat_request 턴 | {result['total_turns']} |",
        f"| 건너뛴 줄 | {result['skipped_lines']} |",
        f"| 미실행 턴(스트림 전 거부 등) | {result['rejection_turns']} |",
        "",
        "## 지연",
        "",
        "| 그룹 | n | p50(ms) | p95(ms) | p99(ms) |",
        "|---|---:|---:|---:|---:|",
    ]
    overall = result["latency"]["overall"]
    for label, metric in (("전체 first_token", "first_token"), ("전체 total", "total")):
        stats = overall[metric]
        lines.append(
            f"| {label} | {stats['n']} | {_display(stats['p50'])} | {_display(stats['p95'])} | {_display(stats['p99'])} |"
        )
    for dimension in ("role", "lane", "model"):
        for group, metrics in result["latency"][dimension].items():
            for metric in ("first_token", "total"):
                stats = metrics[metric]
                lines.append(
                    f"| {dimension}:{group} {metric} | {stats['n']} | {_display(stats['p50'])} | {_display(stats['p95'])} | {_display(stats['p99'])} |"
                )
    lines.extend(
        [
            "",
            "> 모델별 지연은 fan-in 귀속이다. 한 턴이 여러 모델을 쓰면 각 모델 그룹에 같은 턴 지연이 들어간다.",
            "",
            "## 비용",
            "",
            "| 그룹 | 표본/전체 | 커버리지 | 합계(USD) | 평균(USD) | 최소(USD) | 최대(USD) | 상태 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    cost_groups = [
        ("전체", result["cost"]["overall"]),
        *(("lane:" + lane, stats) for lane, stats in result["cost"]["lane"].items()),
        *(("role:" + role, stats) for role, stats in result["cost"]["role"].items()),
        *(("model:" + model, stats) for model, stats in result["cost"]["model"].items()),
        *(("length:" + bucket, stats) for bucket, stats in result["cost"]["length"].items()),
    ]
    for group, stats in cost_groups:
        status = "부분 집계(partial)" if stats["partial"] else "완전 집계"
        lines.append(
            f"| {group} | 비용 표본 {stats['sample_count']} / 전체 {stats['turn_count']} "
            f"({_percent(stats['coverage'])}) | {_percent(stats['coverage'])} | {_display(stats['total'])} | "
            f"{_display(stats['average'])} | {_display(stats['min'])} | {_display(stats['max'])} | {status} |"
        )
    lines.extend(
        [
            "",
            f"> 비용 분모는 **실제로 실행된 턴**이다. 미실행 턴 "
            f"{result['cost']['excluded_rejection_turns']}건(스트림 전 거부 등)은 LLM 을 호출하지 "
            f"않고 costUsd 0 을 싣기 때문에 제외했다 — 포함하면 쿼리당 비용이 0 쪽으로 희석된다. "
            f"degrade·error 율의 분모(전체 {result['total_turns']}턴)와 다르다.",
            "",
            "> 모델별 비용은 fan-in 귀속이다 — 한 턴이 여러 모델을 쓰면 턴 전체 비용이 각 모델 "
            "그룹에 중복으로 들어가 모델별 합계가 전체보다 클 수 있다.",
            "",
            "## degrade",
            "",
            f"- 전체: {result['degraded_count']} / {result['total_turns']} ({_percent(result['degrade_rate'])})",
            "",
            "| 사유 | 건수 | 비율 |",
            "|---|---:|---:|",
        ]
    )
    if result["degrade_reasons"]:
        for code, stats in result["degrade_reasons"].items():
            lines.append(f"| {code} | {stats['count']} | {_percent(stats['rate'])} |")
    else:
        lines.append("| - | 0 | - |")
    lines.extend(
        [
            "",
            "## error",
            "",
            f"- 전체: {result['error_count']} / {result['total_turns']} ({_percent(result['error_rate'])})",
            "",
            "| 오류 | 건수 | 비율 |",
            "|---|---:|---:|",
        ]
    )
    if result["error_types"]:
        for code, stats in result["error_types"].items():
            lines.append(f"| {code} | {stats['count']} | {_percent(stats['rate'])} |")
    else:
        lines.append("| - | 0 | - |")
    lines.extend(
        [
            "",
            "## SLO",
            "",
            "| 그룹 | 목표(ms) | p95 실측(ms) | 판정 | 턴 초과율 | n |",
            "|---|---:|---:|---|---:|---:|",
        ]
    )
    for row in _slo_rows(result, settings):
        lines.append(
            f"| {row['group']} | {_display(row['target'])} | {_display(row['p95'])} | "
            f"{_display(row['status'])} | {_percent(row['exceedance_rate'])} | {row['n']} |"
        )
    alert_text, _ = _alert_text(result, threshold, min_samples)
    lines.extend(
        [
            "",
            "## 알림",
            "",
            f"- {alert_text}",
            "",
        ]
    )
    return "\n".join(lines)


def _csv_rows(
    result: dict[str, Any], settings: Any, threshold: float, min_samples: int
) -> list[tuple[str, str, str, object]]:
    """발표 표에 붙일 long-format CSV 행을 만든다."""
    rows: list[tuple[str, str, str, object]] = []
    for metric in ("total_lines", "total_turns", "skipped_lines", "rejection_turns"):
        rows.append(("overview", "all", metric, result[metric]))
    for dimension in ("overall", "role", "lane", "model"):
        groups = (
            {"overall": result["latency"]["overall"]}
            if dimension == "overall"
            else result["latency"][dimension]
        )
        for group, metrics in groups.items():
            for latency_name, stats in metrics.items():
                csv_group = f"{group}.{latency_name}"
                for metric in ("n", "p50", "p95", "p99"):
                    rows.append(("latency", csv_group, metric, stats[metric]))
    cost_csv_groups = [
        ("overall", result["cost"]["overall"]),
        *(("lane:" + lane, stats) for lane, stats in result["cost"]["lane"].items()),
        *(("role:" + role, stats) for role, stats in result["cost"]["role"].items()),
        *(("model:" + model, stats) for model, stats in result["cost"]["model"].items()),
        *(("length:" + bucket, stats) for bucket, stats in result["cost"]["length"].items()),
    ]
    for group, stats in cost_csv_groups:
        for metric in (
            "sample_count",
            "turn_count",
            "coverage",
            "total",
            "average",
            "min",
            "max",
            "partial",
        ):
            rows.append(("cost", group, metric, stats[metric]))
    rows.append(
        ("cost", "all", "excluded_rejection_turns", result["cost"]["excluded_rejection_turns"])
    )
    rows.extend(
        [
            ("degrade", "all", "count", result["degraded_count"]),
            ("degrade", "all", "rate", result["degrade_rate"]),
            ("error", "all", "count", result["error_count"]),
            ("error", "all", "rate", result["error_rate"]),
        ]
    )
    for section, groups in (
        ("degrade", result["degrade_reasons"]),
        ("error", result["error_types"]),
    ):
        for group, stats in groups.items():
            rows.extend((section, group, metric, stats[metric]) for metric in ("count", "rate"))
    for row in _slo_rows(result, settings):
        for metric in ("target", "p95", "status", "exceedance_rate", "n"):
            rows.append(("slo", row["group"], metric, row[metric]))
    alert_text, fired = _alert_text(result, threshold, min_samples)
    rows.extend(
        [
            ("alert", "degrade", "threshold", threshold),
            ("alert", "degrade", "min_samples", min_samples),
            ("alert", "degrade", "fired", fired),
            ("alert", "degrade", "message", alert_text),
        ]
    )
    return rows


def _write_csv(path: Path, rows: Sequence[tuple[str, str, str, object]]) -> None:
    """CSV 결측을 빈 문자열로 써서 0과 구분한다."""
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("section", "group", "metric", "value"))
        writer.writerows(
            (section, group, metric, "" if value is None else value)
            for section, group, metric, value in rows
        )


def _read_lines(paths: Sequence[str]) -> list[str]:
    """여러 파일 또는 파일 미지정 시 stdin의 줄을 읽는다."""
    if not paths:
        return sys.stdin.readlines()
    lines: list[str] = []
    for name in paths:
        with Path(name).open(encoding="utf-8") as file:
            lines.extend(file.readlines())
    return lines


def _parser() -> argparse.ArgumentParser:
    """명령행 파서를 구성한다."""
    parser = argparse.ArgumentParser(description="chat_request 관측 로그를 집계한다.")
    parser.add_argument("logfiles", metavar="LOGFILE", nargs="*")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--degrade-threshold", type=float)
    parser.add_argument("--min-samples", type=int)
    parser.add_argument("--no-alert", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI를 실행하고 정상 0, degrade 알림 1, 사용·읽기 오류 2를 반환한다."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    settings = get_settings()
    threshold = (
        settings.degrade_rate_alert_threshold
        if args.degrade_threshold is None
        else args.degrade_threshold
    )
    min_samples = (
        settings.degrade_alert_min_samples if args.min_samples is None else args.min_samples
    )
    if not 0 <= threshold <= 1 or min_samples < 0:
        print("오류: degrade 임계는 0..1, 최소 표본은 0 이상이어야 합니다.", file=sys.stderr)
        return 2
    try:
        result = aggregate_lines(_read_lines(args.logfiles))
        markdown = render_markdown(
            result,
            settings,
            threshold=threshold,
            min_samples=min_samples,
        )
        if args.markdown is None:
            print(markdown, end="")
        else:
            args.markdown.write_text(markdown, encoding="utf-8")
        if args.csv is not None:
            _write_csv(args.csv, _csv_rows(result, settings, threshold, min_samples))
    except (OSError, UnicodeError) as exc:
        print(f"오류: 로그 또는 출력 파일을 처리할 수 없습니다: {exc}", file=sys.stderr)
        return 2
    _, fired = _alert_text(result, threshold, min_samples)
    if fired:
        print(
            f"DEGRADE ALERT: {result['degraded_count']}/{result['total_turns']} "
            f"({_percent(result['degrade_rate'])}) > {_percent(threshold)}",
            file=sys.stderr,
        )
    return 0 if args.no_alert or not fired else 1


if __name__ == "__main__":
    raise SystemExit(main())
