"""ctx → LLM 이 읽는 표 (이슈 #594, `05-WORKERS` §2.2·§3.3).

`pipeline.format_findings_block` 이 JSON 덤프 대신 사람이 읽는 형태를 쓰는 선례를 따른다.
JSON 을 그대로 주면 LLM 이 필드명을 문장에 흘리고, 무엇이 특이한지 스스로 판단하려 든다.

**이 모듈의 목적은 나눗셈을 대신 해 주는 것이다.** "담기 8.4회"만 주면 그게 많은지 적은지
알 수 없어 LLM 이 지어낸다 — "담기 8.4회 (평균 3.6회의 2.3배)"로 문장째 넘기고, LLM 은
그것을 옮겨 쓸 뿐 나눗셈을 하지 않는다(`05` §1).
"""

from __future__ import annotations

from app.agents.seller.features import spec
from app.agents.seller.sop.compute.behavior import UNKNOWN_BUCKET
from app.agents.seller.sop.context import AnalysisContext, Segment

# 배수가 이만큼 벌어진 축에 `←` 를 붙인다 — 양방향(상회·하회 대칭)이다. 표기 규약이라
# Settings 가 아니다(`01` §7.3 상수 선례). `05` §1 의 "무엇이 특이한가"를 놓치지 않게
# 하는 장치이고, 임계를 낮추면 전 축에 화살표가 붙어 표시가 무의미해진다.
RATIO_MARK_FACTOR = 1.5

# 표에 실을 원 피처 축 — (centroid_stats 키, 표시명, 단위).
_AXES: tuple[tuple[str, str, str], ...] = (
    ("sessions", "방문", "회"),
    ("productViews", "상품 조회", "회"),
    ("cartAdds", "장바구니", "회"),
    ("checkoutStarts", "결제 시작", "회"),
    ("orderCount", "구매", "회"),
    ("cancelCount", "취소", "회"),
    ("lastActivityDaysAgo", "마지막 활동", "일 전"),
    ("firstSeenDaysAgo", "첫 방문", "일 전"),
)

_FLAG_LABELS: dict[str, str] = {
    "is_cart_abandoner": "장바구니 이탈자",
    "is_checkout_dropper": "결제 중단자",
    "is_viewer_only": "조회만",
    "is_new": "신규",
    "is_returning": "재방문",
    "has_cancelled": "취소 경험",
}

# 구간 서수 대신 사람이 읽는 금액 문구 — `spec.AMOUNT_BUCKET_REPRESENTATIVE` 의 경계와 정합.
_AMOUNT_LABELS: dict[str, str] = {
    "ZERO": "0원",
    "LT_10K": "1만원 미만",
    "10K_50K": "1~5만원",
    "50K_100K": "5~10만원",
    "100K_300K": "10~30만원",
    "GTE_300K": "30만원 이상",
    UNKNOWN_BUCKET: "구간 미상",
}

_VERDICT_TEXT: dict[str, str] = {
    "significant_drop": "통계적으로 유의한 감소",
    "significant_rise": "통계적으로 유의한 증가",
    "no_significant_change": "유의하지 않음",
    "undecided": "판정 보류",
}

_AXIS_WIDTH = 11


def _metric(ctx: AnalysisContext, key: str) -> float | None:
    for metric in ctx.metrics:
        if metric.key == key:
            return metric.value
    return None


def _int(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _mark(ratio: float) -> str:
    """배수가 1.0 에서 크게 벗어난 축 표시 — 상회·하회 대칭."""
    if ratio >= RATIO_MARK_FACTOR or ratio <= 1.0 / RATIO_MARK_FACTOR:
        return "   ←"
    return ""


def _axis_line(segment: Segment, key: str, label: str, unit: str) -> str | None:
    """축 1줄 — "장바구니     8.4회  (평균 3.6회의 2.3배)"."""
    if key not in segment.centroid_stats:
        return None
    value = segment.centroid_stats[key]
    ratio = segment.ratio_to_mean.get(key)
    head = f"  {label.ljust(_AXIS_WIDTH)}{value:>6.1f}{unit}"
    if ratio is None or ratio <= 0:
        # 전체 평균이 0 이라 배수가 정의되지 않는 축(#593 이 키를 뺀 경우) — 값만 싣는다.
        return f"{head}  (전체 평균 0 — 배수 정의 불가)"
    overall = value / ratio
    return f"{head}  (평균 {overall:.1f}{unit}의 {ratio:.1f}배){_mark(ratio)}"


def _composition_line(segment: Segment) -> str | None:
    parts = [
        f"{_FLAG_LABELS[key]} {segment.flag_ratios[key]:.0%}"
        for key in spec.FLAG_KEYS
        if key in segment.flag_ratios
    ]
    return f"  구성: {' · '.join(parts)}" if parts else None


def _amount_line(segment: Segment) -> str | None:
    """금액은 구간 이름이 아니라 분포로 — 비중 큰 순 상위 3구간(`05` §2.2)."""
    if not segment.amount_distribution:
        return None
    ordered = sorted(segment.amount_distribution.items(), key=lambda item: -item[1])[:3]
    parts = [f"{_AMOUNT_LABELS.get(bucket, bucket)} {share:.0%}" for bucket, share in ordered]
    return f"  금액: {' · '.join(parts)}"


def render_segment_block(ctx: AnalysisContext) -> str:
    """고객 세그먼트 표 (`05` §2.2 형태). 세그먼트가 없으면 그 사실을 문장으로 남긴다."""
    total = _int(_metric(ctx, "cohort_total_customers"))
    classified = _int(_metric(ctx, "segment_classified_customers"))
    excluded = _int(_metric(ctx, "segment_excluded_customers"))

    scope = f"{ctx.period_from.isoformat()}~{ctx.period_to.isoformat()}"
    if total is not None and classified is not None:
        header = f"[고객 세그먼트 — {scope} ({total:,}명 중 {classified:,}명 분류)]"
    else:
        header = f"[고객 세그먼트 — {scope}]"

    lines = [header, ""]
    if not ctx.segments:
        lines.append("분류된 세그먼트가 없습니다 — 사유는 판정 보류 목록을 참조하십시오.")
        return "\n".join(lines)

    denominator = classified or sum(segment.size for segment in ctx.segments)
    for segment in ctx.segments:
        share = f" ({segment.size / denominator:.1%})" if denominator else ""
        lines.append(f"■ {segment.display_label or segment.rule_label} · {segment.size:,}명{share}")
        for key, label, unit in _AXES:
            line = _axis_line(segment, key, label, unit)
            if line:
                lines.append(line)
        for extra in (_composition_line(segment), _amount_line(segment)):
            if extra:
                lines.append(extra)
        if segment.delta_size is not None:
            lines.append(f"  변화: {segment.delta_size:+d}명 (양쪽 명단 공통 기준)")
        lines.append("")

    if excluded:
        lines.append(f"[분류 보류] {excluded:,}명 — 소규모 군집 제외분")
    return "\n".join(lines).rstrip()


def _shift_rows(ctx: AnalysisContext) -> list[str]:
    verdicts = {verdict.key: verdict for verdict in ctx.verdicts}
    rows: list[str] = []
    for comparison in ctx.comparisons:
        if not comparison.key.startswith("segment_size:"):
            continue
        label = comparison.key.split(":", 1)[1]
        current = int(comparison.current)
        baseline = int(comparison.baseline)
        delta = current - baseline
        delta_text = f"{delta:+d}"
        if comparison.delta_pct is not None:
            delta_text += f" ({comparison.delta_pct:+.1f}%)"
        verdict = verdicts.get(comparison.key)
        note = _VERDICT_TEXT.get(verdict.verdict, verdict.verdict) if verdict else ""
        if verdict is not None and verdict.p_value is not None:
            note += f" (p={verdict.p_value:.3f})"
        rows.append(f"  {label.ljust(10)}{baseline:>5,} → {current:>5,}명   {delta_text:<16}{note}")
    return rows


def render_shift_block(ctx: AnalysisContext) -> str:
    """세그먼트 변화 표 (`05` §3.3 형태).

    머리글이 **"양쪽 명단 공통 N명"** 을 반드시 밝힌다 — 순증감의 모수는 교집합이고
    `Segment.size`(스냅샷 전체)와 다르기 때문이다. 이 문구가 빠지면 두 숫자가 같은
    모수처럼 읽힌다.
    """
    cohort = _int(_metric(ctx, "segment_shift_cohort"))
    baseline_range = next(
        (
            f"{item.baseline_from.isoformat()}~{item.baseline_to.isoformat()}"
            for item in ctx.comparisons
            if item.key.startswith("segment_size:")
        ),
        None,
    )
    scope = f"{ctx.period_to.isoformat()}"
    if baseline_range:
        scope += f" vs {baseline_range}"
    header = f"[세그먼트 변화 — {scope}"
    header += f" (양쪽 명단 공통 {cohort:,}명)]" if cohort else "]"

    lines = [header, ""]
    rows = _shift_rows(ctx)
    if rows:
        lines.extend(rows)
    else:
        lines.append("  비교 가능한 세그먼트가 없습니다 — 사유는 판정 보류 목록을 참조하십시오.")

    moves = [
        (metric.key.split(":", 1)[1], metric.value)
        for metric in ctx.metrics
        if metric.key.startswith("segment_move:") and metric.value is not None
    ]
    if moves:
        lines.append("")
        lines.append("[주요 이동]")
        for path, count in moves:
            before, _, after = path.partition(">")
            lines.append(f"  {before} → {after}   {int(count):,}명")

    newly = _int(_metric(ctx, "membership_new"))
    returned = _int(_metric(ctx, "membership_returned"))
    dropped = _int(_metric(ctx, "membership_dropped_out"))
    if newly is not None or returned is not None or dropped is not None:
        lines.append("")
        lines.append("[명단 변동]")
        parts = []
        if newly is not None:
            parts.append(f"신규 유입 {newly:,}명")
        if returned is not None:
            parts.append(f"재활동 복귀 {returned:,}명")
        if dropped is not None:
            parts.append(f"명단 밖 {dropped:,}명(판정 보류)")
        lines.append("  " + " · ".join(parts))
    return "\n".join(lines).rstrip()
