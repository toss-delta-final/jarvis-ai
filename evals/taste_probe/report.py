"""산출물 — results.json · report.md · samples/triples/confusion CSV (#462).

리포트 첫 줄에 provider·모델·datasetVersion·datasetHash·N·프롬프트 해시를 싣는다
(`evals/README.md` 8항).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.metrics.run_manifest import VOLATILE_MANIFEST_KEYS
from evals.taste_probe.metrics import AxisResult, match_sample
from evals.taste_probe.runner import SessionResult
from evals.taste_probe.schema import GoldenSet

BANNER = "단일 실행으로 채택 판정 금지 — 표본 분산이 커 축당 ±수 포인트가 흔들린다. 독립 2~3회 분포로 판정한다."

TRAPS = (
    "페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.",
    "실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 세션은 아래 목록에 드러난다.",
    "정답 누출 금지 — priceBand/ratingBand canonicalLabel·' > ' 포함 category canonicalLabel 이 "
    "발화 원문에 그대로 들어가면 안 된다(schema.py 가 강제).",
    "repeatCap 이 축자 반복을 먹는다 — repetition 슬라이스는 다른 표현으로 반복해야 한다.",
    BANNER,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def md_cell(value: str) -> str:
    return value.replace("|", "\\|")


def header_line(results: dict[str, Any]) -> str:
    model = results["modelConfig"]
    fixture = results["fixture"]
    return (
        f"provider={model.get('provider')} · model={model.get('smartModel')} · "
        f"promptSha12={results['promptSha12']} · "
        f"datasetVersion={fixture['version']} · datasetHash={fixture['sha256'][:12]} · "
        f"N={results['n']} · sessions={results['sessionCount']}"
    )


def build_results(
    *,
    sessions: list[SessionResult],
    axes: dict[str, AxisResult],
    axes_by_slice: dict[str, AxisResult],
    baseline: dict[str, Any],
    diagnostics_payload: dict[str, Any],
    confusion: list[dict[str, Any]],
    predicate_confusion: list[dict[str, Any]],
    unfilled: list[dict[str, Any]],
    model_config: dict[str, Any],
    fixture: dict[str, Any],
    prompt_sha12: str,
    n: int,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "modelConfig": model_config,
        "fixture": fixture,
        "promptSha12": prompt_sha12,
        "n": n,
        "sessionCount": len(sessions),
        "dryRun": dry_run,
        "axes": {axis_id: axis.as_dict() for axis_id, axis in axes.items()},
        "axesBySlice": {slice_id: axis.as_dict() for slice_id, axis in axes_by_slice.items()},
        "baseline": baseline,
        "diagnostics": diagnostics_payload,
        "confusion": confusion,
        "predicateConfusion": predicate_confusion,
        "unfilledSessions": unfilled,
        "pacer": pacer,
        "budget": budget,
        "sessions": [
            {
                "sessionId": session.session_id,
                "sliceId": session.slice_id,
                "sampleCount": len(session.samples),
                "attempts": session.attempts,
                "failureCount": len(session.failures),
                "filled": session.filled,
            }
            for session in sessions
        ],
    }


def render_report(results: dict[str, Any]) -> str:
    axes = results["axes"]
    lines = [
        "# 취향 추출 골든셋 프로브 리포트 (#462)",
        "",
        header_line(results),
        "",
        f"> ⚠ **{BANNER}**",
        "",
        "> **이건 `evals/goldenset` 이 아니다.** 단발 검색 질의 + 상품 순위 라벨이 아니라 "
        "다턴 대화 → 기대 트리플의 추출·resolver 정확도를 잰 표다. 두 산출물의 숫자를 섞지 말 것.",
        "",
        "## 축 (primary confirmatory · 사전 등록 2차)",
        "",
        "| 축 | 점수 | exploratory | 분자 정의 | 분모 정의 |",
        "|---|---|---|---|---|",
    ]
    for axis_id in ("recall", "noiseFalsePositiveRate", "nodeIdAgreement"):
        axis = axes[axis_id]
        lines.append(_axis_row(axis_id, axis))

    lines += [
        "",
        "## 축 (exploratory)",
        "",
        "| 축 | 점수 | exploratory | 분자 정의 | 분모 정의 |",
        "|---|---|---|---|---|",
    ]
    for axis_id in sorted(axes):
        if axis_id in ("recall", "noiseFalsePositiveRate", "nodeIdAgreement"):
            continue
        lines.append(_axis_row(axis_id, axes[axis_id]))

    lines += ["", "## 슬라이스 분해 — recall (exploratory)", "", "| 슬라이스 | 점수 |", "|---|---|"]
    for slice_id in sorted(results["axesBySlice"]):
        axis = results["axesBySlice"][slice_id]
        ratio = axis["ratio"]
        score = f"{axis['numerator']}/{axis['denominator']}"
        if ratio is not None:
            score += f" ({ratio * 100:.1f}%)"
        lines.append(f"| `{slice_id}` | {score} |")

    baseline = results["baseline"]
    lines += ["", "## trivial baseline 대조", "", "| 축 | 점수 | 정의 |", "|---|---|---|"]
    for axis_id in (
        "baselineRecall",
        "baselineFalsePositiveRate",
        "baselineNoiseFalsePositiveRate",
        "baselineSessionExactSet",
    ):
        axis = baseline[axis_id]
        ratio = axis["ratio"]
        score = f"{axis['numerator']}/{axis['denominator']}"
        if ratio is not None:
            score += f" ({ratio * 100:.1f}%)"
        lines.append(f"| `{axis_id}` | {score} | {md_cell(axis['definition']['numerator'])} |")
    lines += ["", f"> {baseline['note']}"]

    diag = results["diagnostics"]
    lines += [
        "",
        "## 진단 — 단계 귀속 (합불 아님)",
        "",
        f"- emittedDeltas: {diag['emittedDeltas']}",
        f"- promotedCount(프로덕션 반환값): {diag['promotedCount']}",
        f"- gateRejected: {diag['gateRejected']}",
        f"- resolverDroppedByKind: {diag['resolverDroppedByKind']} "
        f"(합계 {diag['resolverDroppedCount']})",
        f"- legacySchemaNoKind(구스키마, 정상 경로): {diag['legacySchemaNoKind']}",
        f"- unprojectedFacts: {diag['unprojectedFacts']}",
        f"- factDedupCollapsed(0 초과면 위 kind 귀속이 근사): {diag['factDedupCollapsed']}",
        f"- verifiedFalseCount: {diag['verifiedFalseCount']}",
        f"- schemaViolation(표본 실패): {diag['schemaViolation']}",
        f"- transportError(표본 실패, 타입별): {diag['transportError']}",
        f"- bandLabelRejected: {diag['bandLabelRejected']}",
        "",
        "## kind 오분류 행렬 (라벨 일치 기준, §A-4)",
        "",
        "| 기대 kind | 산출 kind | 빈도 |",
        "|---|---|---|",
    ]
    if results["confusion"]:
        lines += [
            f"| `{md_cell(row['expectedKind'])}` | `{md_cell(row['producedKind'])}` | {row['count']} |"
            for row in results["confusion"]
        ]
    else:
        lines.append("| (없음) | | |")

    lines += [
        "",
        "## predicate 오분류 행렬 (kind·라벨 일치, predicate 만 다름)",
        "",
        "| 기대 predicate | 산출 predicate | 빈도 |",
        "|---|---|---|",
    ]
    if results["predicateConfusion"]:
        lines += [
            f"| `{md_cell(row['expectedPredicate'])}` | `{md_cell(row['producedPredicate'])}` | "
            f"{row['count']} |"
            for row in results["predicateConfusion"]
        ]
    else:
        lines.append("| (없음) | | |")

    lines += ["", "## 채우지 못한 세션", ""]
    if results["unfilledSessions"]:
        lines += ["| 세션 | 채움/목표 | 시도 | 오류 |", "|---|---|---|---|"]
        lines += [
            f"| `{md_cell(row['sessionId'])}` | {row['got']}/{row['want']} | {row['attempts']} | "
            f"{', '.join(row['errorTypes']) or '-'} |"
            for row in results["unfilledSessions"]
        ]
    else:
        lines.append("(없음)")

    lines += ["", "## 재현 함정", ""]
    lines += [f"{index}. {trap}" for index, trap in enumerate(TRAPS, 1)]
    lines += [
        "",
        f"페이싱 실측: 대기 {results['pacer']['waitCount']}회 / 허용 {results['pacer']['maxRpm']} rpm.",
        "",
    ]
    return "\n".join(lines)


def _axis_row(axis_id: str, axis: dict[str, Any]) -> str:
    ratio = axis["ratio"]
    score = f"{axis['numerator']}/{axis['denominator']}"
    if ratio is not None:
        score += f" ({ratio * 100:.1f}%)"
    if axis["unfilledSampleCount"]:
        score += f" ⚠ 미충족 {axis['unfilledSampleCount']}"
    definition = axis["definition"]
    exploratory = "예" if axis["exploratory"] else "아니오"
    return (
        f"| `{axis_id}` {axis['title']} | {score} | {exploratory} | "
        f"{md_cell(definition['numerator'])} | {md_cell(definition['denominator'])} |"
    )


def _sample_rows(sessions: list[SessionResult], golden_set: GoldenSet) -> list[dict[str, Any]]:
    sessions_by_id = {session.session_id: session for session in golden_set.sessions}
    rows: list[dict[str, Any]] = []
    for session_result in sessions:
        golden_session = sessions_by_id.get(session_result.session_id)
        expected = golden_session.expected_triples if golden_session is not None else []
        for sample in session_result.samples:
            matched, unmatched_expected, unmatched_produced = match_sample(
                expected, sample.produced
            )
            rows.append(
                {
                    "sessionId": session_result.session_id,
                    "sampleIndex": sample.sample_index,
                    "emittedDeltas": sample.emitted_deltas,
                    # [A-2] 프로덕션 반환값(generate_session_delta 의 promoted 길이) — 파생 뺄셈 아님.
                    "promoted": sample.promoted_count,
                    "gateRejected": sample.gate_rejected,
                    "resolverDropped": len(sample.resolver_dropped),
                    "legacySchemaNoKind": sample.legacy_schema_no_kind,
                    "factDedupCollapsed": sample.fact_dedup_collapsed,
                    "matched": len(matched),
                    "missed": len(unmatched_expected),
                    "falsePositive": len(unmatched_produced),
                    "latencyMs": sample.latency_ms,
                }
            )
    return rows


def _dropped_rows(sessions: list[SessionResult]) -> list[dict[str, Any]]:
    """[A-3] `dropped.csv` — resolver 가 드롭한 승격 델타를 kind·label 과 함께 남긴다."""
    return [
        {
            "sessionId": session_result.session_id,
            "sampleIndex": sample.sample_index,
            "kind": entry["kind"],
            "label": entry["label"],
        }
        for session_result in sessions
        for sample in session_result.samples
        for entry in sample.resolver_dropped
    ]


def _triple_rows(sessions: list[SessionResult]) -> list[dict[str, Any]]:
    return [
        {
            "sessionId": session_result.session_id,
            "sampleIndex": sample.sample_index,
            "kind": triple.node_type,
            "label": triple.node_label,
            "nodeId": triple.node_id,
            "predicate": triple.predicate,
            "verified": triple.verified,
            "method": triple.method or "",
            "distance": "" if triple.distance is None else triple.distance,
            "margin": "" if triple.margin is None else triple.margin,
            "salience": triple.salience,
        }
        for session_result in sessions
        for sample in session_result.samples
        for triple in sample.produced
    ]


def write_artifacts(
    out: Path,
    *,
    results: dict[str, Any],
    manifest: dict[str, Any],
    sessions: list[SessionResult],
    golden_set: GoldenSet,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results), encoding="utf-8", newline="\n")

    _write_csv(
        out / "samples.csv",
        [
            "sessionId",
            "sampleIndex",
            "emittedDeltas",
            "promoted",
            "gateRejected",
            "resolverDropped",
            "legacySchemaNoKind",
            "factDedupCollapsed",
            "matched",
            "missed",
            "falsePositive",
            "latencyMs",
        ],
        _sample_rows(sessions, golden_set),
    )
    _write_csv(
        out / "triples.csv",
        [
            "sessionId",
            "sampleIndex",
            "kind",
            "label",
            "nodeId",
            "predicate",
            "verified",
            "method",
            "distance",
            "margin",
            "salience",
        ],
        _triple_rows(sessions),
    )
    _write_csv(
        out / "confusion.csv",
        ["expectedKind", "producedKind", "count"],
        results["confusion"],
    )
    _write_csv(
        out / "predicate_confusion.csv",
        ["expectedPredicate", "producedPredicate", "count"],
        results["predicateConfusion"],
    )
    _write_csv(
        out / "dropped.csv",
        ["sessionId", "sampleIndex", "kind", "label"],
        _dropped_rows(sessions),
    )


def _strip_volatile(payload: Any, volatile_keys: frozenset[str]) -> Any:
    if isinstance(payload, dict):
        return {
            key: _strip_volatile(value, volatile_keys)
            for key, value in payload.items()
            if key not in volatile_keys
        }
    if isinstance(payload, list):
        return [_strip_volatile(item, volatile_keys) for item in payload]
    return payload


# 정본은 evals.metrics.run_manifest.VOLATILE_MANIFEST_KEYS(run/commitSha/dirty) —
# latencyMs 는 우리 하네스 산출물 고유의 행 필드라 그쪽엔 없다(#413 수렴).
VOLATILE_JSON_KEYS = VOLATILE_MANIFEST_KEYS | frozenset({"latencyMs"})
VOLATILE_CSV_COLUMNS = frozenset({"latencyMs"})


def normalized_artifact_bytes(out: Path) -> dict[str, bytes]:
    """실행별 변동값(시각·지연·runId)을 걷어낸 산출물 — 결정론 테스트용."""
    normalized: dict[str, bytes] = {}
    for path in sorted(out.iterdir()):
        if path.suffix == ".json":
            payload = _strip_volatile(
                json.loads(path.read_text(encoding="utf-8")), VOLATILE_JSON_KEYS
            )
            normalized[path.name] = json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
        elif path.suffix == ".csv":
            rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
            for row in rows:
                for column in VOLATILE_CSV_COLUMNS & set(row):
                    row[column] = ""
            normalized[path.name] = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        else:
            normalized[path.name] = path.read_bytes()
    return normalized
