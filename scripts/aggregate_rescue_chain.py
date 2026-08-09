"""이슈 #385 구제 체인 실빈도·first-token 지연을 JSONL에서 집계한다.

``recommend_zero_result``와 ``recommend_pipeline``은 한 턴에 상호 배타라 합집합으로
읽는다. 로깅 접두사가 붙은 JSON 줄도 받아, 운영 로그를 파일 또는 표준 입력에서 재실행할 수
있다.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Iterable, Sequence
from numbers import Real
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from scripts.aggregate_observability import percentile


RESCUE_EVENTS = ("recommend_zero_result", "recommend_pipeline")
# #151의 decompose LLM head p95≈3.0s — MEASURE-FIRST-TOKEN-363 §4.1의 비교 기준이다.
DECOMPOSE_LLM_HEAD_P95_MS = 3_000
ZERO_RESULT_FIELDS = frozenset(
    {
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
)
PIPELINE_FIELDS = frozenset(
    {
        "rescue_elapsed_ms",
        "relax_auto_elapsed_ms",
        "relax_chip_elapsed_ms",
        "may_auto_relax",
        "rescue_stage_narrowed_timeout_ms",
        "rescue_stage_skipped_budget",
        "rescue_budget_mode",
    }
)


def parse_log_line(line: str) -> dict[str, Any] | None:
    """순수 JSON 또는 로깅 접두사 뒤의 대상 관측 레코드를 안전하게 파싱한다."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        record: Any = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        start = stripped.find("{")
        if start < 0:
            return None
        try:
            record = json.loads(stripped[start:])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if not isinstance(record, dict) or record.get("event") not in {*RESCUE_EVENTS, "chat_request"}:
        return None
    return record


def _number(value: object) -> float | None:
    """bool·비유한 값을 제외한 숫자만 집계에 쓴다."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _stats(values: Sequence[float]) -> dict[str, int | float | None]:
    """최근접 순위 p50/p95/p99와 표본 수를 반환한다."""
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def _rate(count: int, total: int) -> dict[str, int | float | None]:
    """분자·분모가 드러나는 비율을 만든다."""
    return {"count": count, "rate": count / total if total else None}


def aggregate_lines(lines: Iterable[str]) -> dict[str, Any]:
    """로그 줄을 구제 체인 지연·진입 빈도·504 대조로 롤업한다."""
    records: list[dict[str, Any]] = []
    total_lines = 0
    for line in lines:
        total_lines += 1
        if (record := parse_log_line(line)) is not None:
            records.append(record)

    rescue_records = [record for record in records if record.get("event") in RESCUE_EVENTS]
    zero_records = [
        record for record in rescue_records if record.get("event") == "recommend_zero_result"
    ]
    eligible_values: list[float] = []
    not_delayed_values: list[float] = []
    excluded_invalid = 0
    for record in rescue_records:
        rescue = _number(record.get("rescue_elapsed_ms"))
        auto = _number(record.get("relax_auto_elapsed_ms"))
        may_auto_relax = record.get("may_auto_relax")
        if rescue is None or auto is None or not isinstance(may_auto_relax, bool):
            excluded_invalid += 1
            continue
        value = rescue + auto
        if may_auto_relax:
            eligible_values.append(value)
        else:
            not_delayed_values.append(value)

    contributing_values = [value for value in eligible_values if value > 0]

    chat_records = [record for record in records if record.get("event") == "chat_request"]
    timeout_count = sum(record.get("errorType") == "UPSTREAM_TIMEOUT" for record in chat_records)
    post_suppress_count = sum(
        record.get("post_suppress_fallback_attempted") is True for record in zero_records
    )
    expanded_with_candidates_count = sum(
        record.get("category_expanded") is True and record.get("had_candidates") is True
        for record in zero_records
    )
    return {
        "total_lines": total_lines,
        "accepted_records": len(records),
        "skipped_lines": total_lines - len(records),
        "rescue_records": rescue_records,
        "first_token": {
            "eligible": _stats(eligible_values),
            "contributing": _stats(contributing_values),
            "contributing_values": contributing_values,
            "exposure": _rate(len(contributing_values), len(eligible_values)),
            "max": max(contributing_values) if contributing_values else None,
            "not_delayed": _stats(not_delayed_values),
            "excluded_invalid": excluded_invalid,
            "event_counts": {
                event: sum(record.get("event") == event for record in rescue_records)
                for event in RESCUE_EVENTS
            },
        },
        "chain_entry": {
            "zero_result_turns": len(zero_records),
            "post_suppress_fallback_attempted": _rate(post_suppress_count, len(zero_records)),
            "expanded_with_candidates": _rate(expanded_with_candidates_count, len(zero_records)),
        },
        "upstream_timeout": {
            "count": timeout_count,
            "total": len(chat_records),
            "rate": timeout_count / len(chat_records) if chat_records else None,
        },
    }


def _display(value: object) -> str:
    """결측은 대시로, 숫자는 간결하게 렌더링한다."""
    if value is None:
        return "-"
    return f"{value:.10g}" if isinstance(value, float) else str(value)


def _percent(value: float | None) -> str:
    """비율을 한 자리 퍼센트로 렌더링한다."""
    return "-" if value is None else f"{value * 100:.1f}%"


def _sample_text(n: int, min_samples: int) -> str:
    """표본 하한 전에는 판정을 보류한다."""
    return (
        f"표본 부족({n}/{min_samples}) — 판정 보류" if n < min_samples else "표본 충족 — 분포 참고"
    )


def _rate_display(value: float | None, n: int, min_samples: int) -> str:
    """표본 하한 전에는 비율 수치 대신 보류 문구를 표시한다."""
    return "표본 부족 — 판정 보류" if n < min_samples else _percent(value)


def _threshold_summary(first_token: dict[str, Any], settings: Any) -> dict[str, int]:
    """주입 설정 하나로 조건부 분포의 임계 초과 건수를 계산한다."""
    baseline_ms = settings.spring_search_timeout_s * 3 * 1000
    timeout_ms = settings.stream_first_token_timeout_s * 1000
    values = first_token["contributing_values"]
    return {
        "baseline_or_higher_count": sum(value >= baseline_ms for value in values),
        "timeout_or_higher_count": sum(value >= timeout_ms for value in values),
    }


def _proximity(
    contributing: dict[str, int | float | None],
    baseline_ms: float,
    timeout_ms: float,
    min_samples: int,
    eligible_n: int,
    baseline_or_higher_count: int,
    timeout_or_higher_count: int,
) -> str:
    """조건부 분포와 임계 초과 건수로 근접도를 판단한다."""
    if eligible_n < min_samples:
        return "표본 부족 — 판정 보류"
    if timeout_or_higher_count:
        return "first-token 상한 도달·초과"
    if baseline_or_higher_count:
        return f"{baseline_ms / 1000:.1f}s 기준선 근접·도달"
    p95 = contributing["p95"]
    if contributing["n"] == 0:
        return "구제 기여 없음"
    if not isinstance(p95, float):
        return "조건부 표본 부족 — 판정 보류"
    if p95 >= timeout_ms:
        return "first-token 상한 도달·초과"
    if p95 >= baseline_ms:
        return f"{baseline_ms / 1000:.1f}s 기준선 근접·도달"
    return f"{baseline_ms / 1000:.1f}s 기준선 미만"


def render_markdown(
    result: dict[str, Any], settings: Any, *, min_samples: int | None = None
) -> str:
    """분자·분모·결측 규약을 포함한 Markdown 보고서를 렌더링한다."""
    first_token = result["first_token"]
    eligible = first_token["eligible"]
    not_delayed = first_token["not_delayed"]
    # 별도 구제 체인 설정은 추가하지 않는다. 기본값만 기존 degrade 알림 하한을 빌리고 CLI에서 덮는다.
    min_samples = settings.degrade_alert_min_samples if min_samples is None else min_samples
    timeout_ms = settings.stream_first_token_timeout_s * 1000
    baseline_ms = settings.spring_search_timeout_s * 3 * 1000
    thresholds = _threshold_summary(first_token, settings)
    llm_head_baseline_ms = baseline_ms + DECOMPOSE_LLM_HEAD_P95_MS
    lines = [
        "# 구제 체인 실측 집계",
        "",
        "## 개요",
        "",
        "| 항목 | 값 |",
        "|---|---:|",
        f"| 읽은 줄 | {result['total_lines']} |",
        f"| 채택한 대상 레코드 | {result['accepted_records']} |",
        f"| 건너뛴 줄 | {result['skipped_lines']} |",
        "",
        "## 1급 지표 — first-token 지연 기여분",
        "",
        "분자: `rescue_elapsed_ms + relax_auto_elapsed_ms` (칩 probe는 first SSE 이후라 제외).  ",
        "분모: `recommend_zero_result ∪ recommend_pipeline` 중 `may_auto_relax=True`인 턴.",
        "",
        "| 그룹 | n | p50(ms) | p95(ms) | p99(ms) | 상한 소모율 | 근접도 |",
        "|---|---:|---:|---:|---:|---:|---|",
        f"| may_auto_relax=True (빈도 가중) | {eligible['n']} | {_display(eligible['p50'])} | {_display(eligible['p95'])} | {_display(eligible['p99'])} | {_percent(eligible['p95'] / timeout_ms if eligible['p95'] is not None else None)} | {_proximity(first_token['contributing'], baseline_ms, timeout_ms, min_samples, eligible['n'], thresholds['baseline_or_higher_count'], thresholds['timeout_or_higher_count'])} |",
        f"| 구제 기여 > 0 (조건부) | {first_token['contributing']['n']} | {_display(first_token['contributing']['p50'])} | {_display(first_token['contributing']['p95'])} | {_display(first_token['contributing']['p99'])} | {_percent(first_token['contributing']['p95'] / timeout_ms if first_token['contributing']['p95'] is not None else None)} | 실제 진입 턴 분포 |",
        f"| may_auto_relax=False (별도, first-token 비기여) | {not_delayed['n']} | {_display(not_delayed['p50'])} | {_display(not_delayed['p95'])} | {_display(not_delayed['p99'])} | - | 분모에서 제외 |",
        "",
        f"> 이벤트별 합집합 내역: zero_result {first_token['event_counts']['recommend_zero_result']}건 / pipeline {first_token['event_counts']['recommend_pipeline']}건. 결측·형식 오류 제외 | {first_token['excluded_invalid']}건.",
        f"> 노출 규모: 구제 기여 > 0은 {first_token['exposure']['count']} / {eligible['n']} ({_percent(first_token['exposure']['rate'])}); {baseline_ms / 1000:.1f}s 기준선 이상 {thresholds['baseline_or_higher_count']}건, {timeout_ms / 1000:.1f}s 상한 이상 {thresholds['timeout_or_higher_count']}건, 최댓값 {_display(first_token['max'])}ms.",
        f"> 비교 기준: 구제 3단 기준선 {baseline_ms / 1000:.1f}s, 선행 LLM head 포함 기준선 {llm_head_baseline_ms / 1000:.1f}s, 설정 first-token 상한 {timeout_ms / 1000:.1f}s. {_sample_text(eligible['n'], min_samples)}.",
        "",
        "## 체인 진입 빈도",
        "",
        "분모는 모두 `recommend_zero_result`뿐이다. pipeline에는 필요한 필드가 없어 결과가 0건으로 끝난 진입의 하한이다.",
        "",
        "| 지표 | 분자/분모 | 비율 |",
        "|---|---:|---:|",
        f"| post_suppress_fallback_attempted=True | {result['chain_entry']['post_suppress_fallback_attempted']['count']} / {result['chain_entry']['zero_result_turns']} | {_rate_display(result['chain_entry']['post_suppress_fallback_attempted']['rate'], result['chain_entry']['zero_result_turns'], min_samples)} |",
        f"| category_expanded=True & had_candidates=True | {result['chain_entry']['expanded_with_candidates']['count']} / {result['chain_entry']['zero_result_turns']} | {_rate_display(result['chain_entry']['expanded_with_candidates']['rate'], result['chain_entry']['zero_result_turns'], min_samples)} |",
        "",
        "## 504 대조",
        "",
        f"- `chat_request`의 `errorType=UPSTREAM_TIMEOUT`: {result['upstream_timeout']['count']} / {result['upstream_timeout']['total']} ({_rate_display(result['upstream_timeout']['rate'], result['upstream_timeout']['total'], min_samples)}).",
        "> 이는 first-token 초과만 분리할 수 없는 **상한**이다. 같은 오류가 다른 경로에서도 발생할 수 있어 로그만으로 first-token 초과라고 단정하지 않는다.",
        "",
    ]
    return "\n".join(lines)


def _csv_rows(
    result: dict[str, Any], settings: Any, min_samples: int
) -> list[tuple[str, str, str, object]]:
    """Markdown 수치와 같은 long-format CSV 행을 만든다."""
    rows: list[tuple[str, str, str, object]] = []
    thresholds = _threshold_summary(result["first_token"], settings)
    for metric in ("total_lines", "accepted_records", "skipped_lines"):
        rows.append(("overview", "all", metric, result[metric]))
    for group, stats in (
        ("may_auto_relax_true", result["first_token"]["eligible"]),
        ("contributing", result["first_token"]["contributing"]),
        ("may_auto_relax_false", result["first_token"]["not_delayed"]),
    ):
        for metric in ("n", "p50", "p95", "p99"):
            rows.append(("first_token", group, metric, stats[metric]))
    rows.append(
        ("first_token", "all", "excluded_invalid", result["first_token"]["excluded_invalid"])
    )
    rows.extend(
        ("first_token", "contributing", metric, result["first_token"]["exposure"][metric])
        for metric in ("count", "rate")
    )
    for metric, value in [
        ("baseline_or_higher_count", thresholds["baseline_or_higher_count"]),
        ("timeout_or_higher_count", thresholds["timeout_or_higher_count"]),
        ("max", result["first_token"]["max"]),
    ]:
        rows.append(("first_token", "contributing", metric, value))
    for metric, stats in result["chain_entry"].items():
        if metric == "zero_result_turns":
            rows.append(("chain_entry", "zero_result", metric, stats))
        else:
            rows.extend(("chain_entry", metric, name, stats[name]) for name in ("count", "rate"))
    for metric in ("count", "total", "rate"):
        rows.append(
            ("upstream_timeout", "chat_request", metric, result["upstream_timeout"][metric])
        )
    rows.extend(
        [
            (
                "threshold",
                "first_token",
                "baseline_ms",
                settings.spring_search_timeout_s * 3 * 1000,
            ),
            (
                "threshold",
                "first_token",
                "timeout_ms",
                settings.stream_first_token_timeout_s * 1000,
            ),
            ("threshold", "first_token", "min_samples", min_samples),
        ]
    )
    return rows


def _write_csv(path: Path, rows: Sequence[tuple[str, str, str, object]]) -> None:
    """CSV의 결측은 빈 문자열로 써서 0과 구분한다."""
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("section", "group", "metric", "value"))
        writer.writerows(
            (section, group, metric, "" if value is None else value)
            for section, group, metric, value in rows
        )


def _read_lines(paths: Sequence[str]) -> list[str]:
    """파일 목록 또는 표준 입력에서 줄을 읽는다."""
    if not paths:
        return sys.stdin.readlines()
    lines: list[str] = []
    for name in paths:
        with Path(name).open(encoding="utf-8") as file:
            lines.extend(file.readlines())
    return lines


def _parser() -> argparse.ArgumentParser:
    """명령행 파서를 구성한다."""
    parser = argparse.ArgumentParser(description="구제 체인 관측 로그를 집계한다.")
    parser.add_argument("logfiles", metavar="LOGFILE", nargs="*")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--min-samples", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """집계기를 실행하고 사용·읽기 오류만 2로 반환한다."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        settings = get_settings()
        min_samples = (
            settings.degrade_alert_min_samples if args.min_samples is None else args.min_samples
        )
        if min_samples < 0:
            print("오류: 최소 표본은 0 이상이어야 합니다.", file=sys.stderr)
            return 2
        result = aggregate_lines(_read_lines(args.logfiles))
        markdown = render_markdown(result, settings, min_samples=min_samples)
        if args.markdown is None:
            print(markdown, end="")
        else:
            args.markdown.write_text(markdown, encoding="utf-8")
        if args.csv is not None:
            _write_csv(args.csv, _csv_rows(result, settings, min_samples))
    except (OSError, UnicodeError) as exc:
        print(f"오류: 로그 또는 출력 파일을 처리할 수 없습니다: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
