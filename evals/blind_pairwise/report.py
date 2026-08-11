"""사람 평가 분석 결과의 짧은 Markdown 보고서."""

from __future__ import annotations

from typing import Any


def render_report(result: dict[str, Any]) -> str:
    """분석 JSON을 분모·불일치·한계가 보이는 문서로 렌더링한다."""
    counts = result["preferenceCounts"]
    denominators = result["denominators"]
    intervals = result["preferenceIntervals95"]
    coverage = result["coverage"]
    lines = [
        "# Blind pairwise buyer evaluation (#153)",
        "",
        "## Preference summary",
        "",
        "| preference | count | denominator | 95% interval |",
        "|---|---:|---:|---|",
        _row("baseline", counts, intervals),
        _row("recommendation_v2", counts, intervals),
        _row("tie", counts, intervals),
        _row("abstain", counts, intervals),
        "",
        (
            f"Response denominator={denominators['responses']}; non-abstain denominator="
            f"{denominators['nonAbstain']}; decisive preference denominator="
            f"{denominators['decisive']}."
        ),
        "",
        (
            "95% intervals are descriptive conditional response-level Wilson intervals; "
            "crossed pair/evaluator dependence is not accounted for and they have no "
            "population coverage or superiority claim."
        ),
        "",
        "## Ordinal rubric distributions",
        "",
    ]
    for dimension, variants in result["ordinalDistributions"].items():
        lines.append(f"### {dimension}")
        lines.append("")
        lines.append("| variant | counts (1..5) | denominator | mean | median |")
        lines.append("|---|---|---:|---:|---:|")
        for variant, distribution in variants.items():
            counts_text = ", ".join(
                f"{score}:{distribution['counts'][str(score)]}" for score in range(1, 6)
            )
            lines.append(
                f"| {variant} | {counts_text} | {distribution['denominator']} | "
                f"{_number(distribution['mean'])} | {_number(distribution['median'])} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Agreement",
            "",
            "Method: Krippendorff alpha (nominal for preference; abstain as missing "
            "for preference alpha while its count is preserved; ordinal for 1–5 rubric "
            "scores using pooled-marginal-cumulative distance).",
            "",
            f"- Preference alpha: {_alpha(result['agreement']['dimensions']['preference'])}",
        ]
    )
    for dimension, variants in result["agreement"]["ordinal"].items():
        for variant, payload in variants.items():
            lines.append(f"- {dimension}/{variant} alpha: {_alpha(payload)}")

    lines.extend(["", "## Disagreement examples", ""])
    examples = result.get("disagreementExamples", [])
    if not examples:
        lines.append("No disagreement examples were observed in the supplied human rows.")
    else:
        lines.append("| pair | response count | preferences | dimension ranges |")
        lines.append("|---|---:|---|---|")
        for example in examples:
            lines.append(
                f"| {example['pairId']} | {example['responseCount']} | "
                f"{example['preferences']} | {example['dimensionRanges']} |"
            )

    lines.extend(
        [
            "",
            "## Collection coverage",
            "",
            f"- Status: **{result.get('status', 'HUMAN_INPUT_REQUIRED')}**",
            f"- Planned pairs: {coverage['plannedPairs']}; planned assignments: {coverage['plannedAssignments']}",
            f"- Eligible evaluator aliases: {coverage['eligibleEvaluators']}; observed responses: {coverage['observedResponses']}",
            f"- Observed human evaluator aliases: {coverage['observedHumanEvaluators']}",
            f"- Complete pairs: {coverage['completePairs']}; minimum plan satisfied: {coverage['minimumPlanSatisfied']}",
            "",
            "| pair | expected responses | observed responses | expected evaluators | observed distinct evaluators | complete |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for pair_id, pair in coverage["pairCompleteness"].items():
        lines.append(
            f"| {pair_id} | {pair['expected']} | {pair['observed']} | "
            f"{pair['expectedEvaluators']} | {pair['observedDistinctEvaluators']} | "
            f"{pair['complete']} |"
        )
    lines.extend(
        [
            "",
            f"**Caveat:** {result['caveat']}",
            "",
            "Human rows are preserved as submitted; ties, abstentions, and disagreements are not imputed.",
        ]
    )
    if result.get("provenance"):
        lines.extend(["", "## Provenance", ""])
        for key, value in sorted(result["provenance"].items()):
            lines.append(f"- `{key}`: `{value}`")
    if "llmJudge" in result:
        judge = result["llmJudge"]
        lines.extend(
            [
                "",
                "## Optional LLM judge comparison",
                "",
                f"Compared denominator={judge['denominator']}; agreement={_number(judge['agreement'])}; "
                f"95% interval={_interval_text(judge['agreementCi95'])}.",
                "",
                "### Confusion matrix (human rows × LLM-judge columns)",
                "",
                "| human \\ judge | baseline | recommendation_v2 | tie | abstain |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for human in ("baseline", "recommendation_v2", "tie", "abstain"):
            matrix_row = judge["confusionMatrix"][human]
            lines.append(
                f"| {human} | {matrix_row['baseline']} | {matrix_row['recommendation_v2']} | "
                f"{matrix_row['tie']} | {matrix_row['abstain']} |"
            )
        lines.extend(
            [
                "",
                "The judge section is present only because judge rows were supplied.",
            ]
        )
    return "\n".join(lines) + "\n"


def _row(name: str, counts: dict[str, int], intervals: dict[str, Any]) -> str:
    interval = intervals[name]
    return (
        f"| {name} | {counts[name]} | {interval['denominator']} | "
        f"{_interval_text(interval)} |"
    )


def _interval_text(interval: dict[str, Any]) -> str:
    if interval["low"] is None:
        return "N/A (no observations)"
    return f"[{interval['low']:.3f}, {interval['high']:.3f}]"


def _alpha(payload: dict[str, Any]) -> str:
    return "N/A" if payload["alpha"] is None else f"{payload['alpha']:.3f}"


def _number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


__all__ = ["render_report"]
