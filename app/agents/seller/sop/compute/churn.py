"""`churn` compute — 스냅샷 2개로 세그먼트 이동을 잰다 (이슈 #594, `05-WORKERS` §3).

핵심 규약 셋:

1. **조인 키는 `rule_label` 원형이다.** 표시 라벨(`탐색형(1)`)로 조인하면 군집 번호가
   시점 간에 안정적이지 않아 이동이 **전부 가짜로** 잡힌다(`04` §4.3). 회귀 테스트가
   이 한 줄을 지킨다.
2. **명단에서 사라진 고객은 이동으로 보고하지 않는다.** I-38 이 활동 상위 1,000명만
   주므로 "활동을 끊은 것"과 "남에게 밀린 것"을 구분할 수 없다 — 판정 보류로 규모만
   밝힌다(`05` 결정 3). 밀려난 고객을 이탈로 보고하면 그게 곧 오보다.
3. **순증감·유의성의 모수는 교집합 인원이다.** `05` §3.3 예시의 현재·기준 합계가 둘 다
   교집합 인원(812)으로 일치한다. `Segment.size`(스냅샷 전체 군집 크기)와 모수가 다르므로
   표 머리글이 "양쪽 명단 공통 N명"을 반드시 밝힌다(`render.render_shift_block`).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from app.agents.seller.analysis import proportions
from app.agents.seller.analysis_records import SnapshotRecord
from app.agents.seller.features import spec
from app.agents.seller.sop.compute import behavior
from app.agents.seller.sop.context import AnalysisContext, Comparison, Hold, Metric, Verdict
from app.core.config import Settings
from app.schemas.spring import ChurnResult

# 주요 이동 표시 건수 — 표기 규약이라 Settings 가 아니다(`01` §7.3 의 상수 선례).
# 25칸 행렬을 그대로 주면 LLM 이 산만해진다는 것이 `05` §3.3 의 판단이다.
MOVE_TOP_N = 3

_METHOD = "two_proportion_z"


def _delta_pct(current: float, baseline: float) -> float | None:
    """증감률(%) — 기준값이 0 이하면 정의 불가라 None 이다(0% 위장 금지, #194 계승)."""
    if baseline <= 0:
        return None
    return (current - baseline) / baseline * 100.0


def _label_by_customer(snapshot: SnapshotRecord) -> dict[str, str]:
    """`customerLabel` → `rule_label` 원형. **이 사전은 ctx 로 나가지 않는다**(재식별 금지)."""
    rows = snapshot.feature_rows if isinstance(snapshot.feature_rows, list) else []
    mapping: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("customerLabel")
        if not label:
            continue
        mapping[str(label)] = str(row.get("rule_label") or "")
    return mapping


def _new_by_customer(snapshot: SnapshotRecord) -> dict[str, bool]:
    """`customerLabel` → `flags.is_new`. 오늘만 있는 고객을 신규/복귀로 가르는 근거다."""
    rows = snapshot.feature_rows if isinstance(snapshot.feature_rows, list) else []
    flags: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("customerLabel")
        if not label:
            continue
        block = row.get("flags") if isinstance(row.get("flags"), dict) else {}
        flags[str(label)] = bool(block.get("is_new"))
    return flags


def _metric(
    ctx: AnalysisContext, key: str, value: float | None, unit: str, source: str = "calc"
) -> None:
    ctx.metrics.append(
        Metric(
            key=key,
            value=value,
            unit=unit,
            source=source,
            period_from=ctx.period_from,
            period_to=ctx.period_to,
        )
    )


def _split_membership(
    ctx: AnalysisContext,
    now_labels: dict[str, str],
    base_labels: dict[str, str],
    *,
    current: SnapshotRecord,
    baseline: SnapshotRecord,
) -> set[str]:
    """명단 3분할 (`05` §3.1) — 교집합을 돌려주고 나머지는 지표·보류로 남긴다."""
    both = now_labels.keys() & base_labels.keys()
    gone = base_labels.keys() - now_labels.keys()
    fresh = now_labels.keys() - base_labels.keys()

    new_flags = _new_by_customer(current)
    newly = sum(1 for label in fresh if new_flags.get(label))

    _metric(ctx, "membership_new", float(newly), "명")
    _metric(ctx, "membership_returned", float(len(fresh) - newly), "명")
    _metric(ctx, "membership_dropped_out", float(len(gone)), "명")

    if gone:
        truncated = bool(baseline.truncated or current.truncated)
        note = " — 절단 가능성" if truncated else ""
        ctx.holds.append(
            Hold(
                step="compute",
                reason=(
                    f"membership_pending: 명단 밖으로 나간 {len(gone)}명은 판정 보류{note}"
                    " (I-38 은 활동 상위만 준다 — 이탈과 밀림을 구분할 수 없다)"
                ),
            )
        )
    return set(both)


def _movement_matrix(
    ctx: AnalysisContext,
    both: set[str],
    now_labels: dict[str, str],
    base_labels: dict[str, str],
) -> Counter[tuple[str, str]]:
    """교집합의 (이전 라벨, 현재 라벨) 도수 — `기타`·빈 라벨은 뺀다.

    `기타` 는 서로 다른 소규모 군집이 뭉개진 라벨이라 "기타 → 기타"가 이동인지 아닌지
    말할 수 없다. 빼되 규모는 `shift_unclassified` 로 밝힌다.
    """
    matrix: Counter[tuple[str, str]] = Counter()
    unclassified = 0
    for label in both:
        before = base_labels[label]
        after = now_labels[label]
        if not before or not after or spec.LABEL_SMALL in (before, after):
            unclassified += 1
            continue
        matrix[(before, after)] += 1
    _metric(ctx, "shift_unclassified", float(unclassified), "명")
    return matrix


def _fill_shift_verdicts(
    ctx: AnalysisContext,
    matrix: Counter[tuple[str, str]],
    *,
    baseline: SnapshotRecord,
    settings: Settings,
) -> dict[str, int]:
    """라벨별 순증감 + 2-proportion z. 라벨 → 증감 사전을 돌려준다(`delta_size` 배정용)."""
    before_counts: Counter[str] = Counter()
    after_counts: Counter[str] = Counter()
    for (before, after), count in matrix.items():
        before_counts[before] += count
        after_counts[after] += count
    cohort = sum(matrix.values())
    _metric(ctx, "segment_shift_cohort", float(cohort), "명")

    deltas: dict[str, int] = {}
    if not cohort:
        return deltas

    shift_threshold = cohort * settings.seller_segment_shift_pct
    for label in sorted(set(before_counts) | set(after_counts)):
        size_then = before_counts[label]
        size_now = after_counts[label]
        deltas[label] = size_now - size_then
        key = f"segment_size:{label}"
        detail = {
            "current": float(size_now),
            "baseline": float(size_then),
            "delta": float(size_now - size_then),
            "cohort": float(cohort),
        }
        # 트리거 발동 판정이 아니다 — 서술 우선순위 재료로 각인만 한다(발동은 #595 scan).
        if abs(size_now - size_then) >= shift_threshold:
            detail["exceeds_shift_threshold"] = 1.0
        try:
            comparison = proportions.compare_rates(
                size_now,
                cohort,
                size_then,
                cohort,
                alpha=settings.seller_rate_test_alpha,
                confidence=settings.seller_wilson_confidence,
            )
        except ValueError as exc:
            # clamp 로 정상 검정처럼 위장하지 않는다(`tools.py` 퍼널 선례와 같은 규약).
            ctx.verdicts.append(
                Verdict(key=key, verdict="undecided", method=_METHOD, detail=detail)
            )
            ctx.holds.append(
                Hold(step="compute", reason=f"shift_inconsistent: {label} 검정 불가 — {exc}")
            )
        else:
            ctx.verdicts.append(
                Verdict(
                    key=key,
                    verdict=comparison.verdict,
                    method=_METHOD,
                    p_value=comparison.p_value,
                    detail=detail,
                )
            )
        ctx.comparisons.append(
            Comparison(
                key=key,
                current=float(size_now),
                baseline=float(size_then),
                delta_pct=_delta_pct(float(size_now), float(size_then)),
                baseline_from=baseline.period_from,
                baseline_to=baseline.period_to,
            )
        )
    return deltas


def _fill_top_moves(
    ctx: AnalysisContext, matrix: Counter[tuple[str, str]], *, settings: Settings
) -> None:
    """주요 이동 상위 N건을 지표로 남긴다 — 코호트의 최소 비율 미만은 노이즈라 뺀다."""
    cohort = sum(matrix.values())
    if not cohort:
        return
    floor = cohort * settings.seller_move_report_min_pct
    moves = [
        (before, after, count)
        for (before, after), count in matrix.items()
        if before != after and count >= floor
    ]
    moves.sort(key=lambda move: (-move[2], move[0], move[1]))
    for before, after, count in moves[:MOVE_TOP_N]:
        _metric(ctx, f"segment_move:{before}>{after}", float(count), "명")


def _apply_delta_size(ctx: AnalysisContext, deltas: dict[str, int]) -> None:
    """`Segment.delta_size` 배정 — **같은 원형 라벨 군집이 둘 이상이면 배정하지 않는다.**

    이동 행렬은 원형 라벨 단위라 증감도 원형 단위다. k=6 이면 라벨 5종으로 모자라
    `탐색형(1)`·`탐색형(2)` 처럼 한 원형에 군집이 둘 붙는데, 그 둘에 같은 증감을 넣으면
    합계가 두 배가 되고 어느 한쪽에만 넣으면 근거가 없다. None 으로 두고 보류를 남긴다.
    """
    by_label: dict[str, list[int]] = {}
    for index, segment in enumerate(ctx.segments):
        by_label.setdefault(segment.rule_label, []).append(index)
    ambiguous: list[str] = []
    for label, indices in by_label.items():
        if label not in deltas:
            continue
        if len(indices) > 1:
            ambiguous.append(label)
            continue
        ctx.segments[indices[0]].delta_size = deltas[label]
    for label in sorted(ambiguous):
        ctx.holds.append(
            Hold(
                step="compute",
                reason=(
                    f"delta_size_ambiguous: 원형 라벨 '{label}' 군집이 둘 이상이라"
                    " 세그먼트별 증감을 배정하지 않는다(이동 행렬은 원형 단위)"
                ),
            )
        )


def _valid_fraction(rate: float | None) -> bool:
    """I-16 `churnRate` 는 스키마가 [0,1] 을 강제하지 않는다 — 정의역 밖은 판정 보류다."""
    return rate is not None and 0.0 <= rate <= 1.0


def _compare_churn_rate(
    ctx: AnalysisContext,
    churn_now: ChurnResult | None,
    churn_prev: ChurnResult | None,
    *,
    baseline_from: date,
    baseline_to: date,
    settings: Settings,
) -> None:
    """I-16 이탈률 2기간 비교 (`05` §3.2 절차 6). 인자가 없으면 조용히 생략한다.

    스냅샷과 무관한 축이라 `no_baseline`·`spec_mismatch` 로 스냅샷 비교가 막힌 턴에도
    돌린다 — 두 보류는 "고객 피처 스냅샷을 맞댈 수 없다"는 뜻이지 "I-16 이 없다"가 아니다.
    """
    if churn_now is None or churn_prev is None:
        return
    _metric(ctx, "churn_rate", churn_now.churn_rate, "비율", source="I-16")
    usable = (
        _valid_fraction(churn_now.churn_rate)
        and _valid_fraction(churn_prev.churn_rate)
        and bool(churn_now.cohort_size)
        and bool(churn_prev.cohort_size)
    )
    if not usable:
        ctx.verdicts.append(Verdict(key="churn_rate", verdict="undecided", method=_METHOD))
        ctx.holds.append(
            Hold(
                step="compute",
                reason=(
                    "churn_rate_unusable: I-16 이탈률/코호트가 판정 가능한 값이 아니다"
                    f" (현재={churn_now.churn_rate!r}/{churn_now.cohort_size!r},"
                    f" 기준={churn_prev.churn_rate!r}/{churn_prev.cohort_size!r})"
                ),
            )
        )
        return

    current_cohort = int(churn_now.cohort_size or 0)
    baseline_cohort = int(churn_prev.cohort_size or 0)
    current_rate = float(churn_now.churn_rate or 0.0)
    baseline_rate = float(churn_prev.churn_rate or 0.0)
    # I-16 은 이탈 '수'가 아니라 fraction 을 준다 — 검정에 필요한 성공 수로 환산한다.
    current_success = min(round(current_rate * current_cohort), current_cohort)
    baseline_success = min(round(baseline_rate * baseline_cohort), baseline_cohort)
    try:
        comparison = proportions.compare_rates(
            current_success,
            current_cohort,
            baseline_success,
            baseline_cohort,
            alpha=settings.seller_rate_test_alpha,
            confidence=settings.seller_wilson_confidence,
        )
    except ValueError as exc:
        ctx.verdicts.append(Verdict(key="churn_rate", verdict="undecided", method=_METHOD))
        ctx.holds.append(Hold(step="compute", reason=f"churn_rate_unusable: {exc}"))
        return

    ctx.verdicts.append(
        Verdict(
            key="churn_rate",
            verdict=comparison.verdict,
            method=_METHOD,
            p_value=comparison.p_value,
            detail={
                "current_rate": comparison.current.rate,
                "baseline_rate": comparison.baseline.rate,
                "current_cohort": float(current_cohort),
                "baseline_cohort": float(baseline_cohort),
            },
        )
    )
    ctx.comparisons.append(
        Comparison(
            key="churn_rate",
            current=comparison.current.rate,
            baseline=comparison.baseline.rate,
            delta_pct=_delta_pct(comparison.current.rate, comparison.baseline.rate),
            baseline_from=baseline_from,
            baseline_to=baseline_to,
        )
    )


def compute_churn(
    ctx: AnalysisContext,
    *,
    current: SnapshotRecord,
    baseline: SnapshotRecord | None,
    churn_now: ChurnResult | None = None,
    churn_prev: ChurnResult | None = None,
    settings: Settings,
) -> None:
    """세그먼트 이동 분석의 계산 파트 — LLM 0회 (`05` §3.2).

    비교가 막혀도 **현재 분포는 언제나 채운다** — 기준이 없다고 오늘의 세그먼트까지
    사라지면 보고서가 "고객이 없다"처럼 읽힌다(`05` §3.2 "이동 분석 생략, 현재 분포만").
    """
    behavior.inherit_snapshot_holds(ctx, current)
    classified, excluded = behavior.fill_segments(ctx, current, settings=settings)
    behavior.fill_scale_metrics(ctx, current, classified, excluded)

    offset = timedelta(days=settings.seller_baseline_offset_days)
    baseline_from = baseline.period_from if baseline else ctx.period_from - offset
    baseline_to = baseline.period_to if baseline else ctx.period_to - offset

    if baseline is None:
        ctx.holds.append(
            Hold(
                step="compute",
                reason=(
                    "no_baseline: 비교 기준 스냅샷이 없어 세그먼트 이동 분석을 보류한다"
                    f" (기준일 {baseline_to.isoformat()})"
                ),
            )
        )
    elif current.feature_spec_version != baseline.feature_spec_version:
        ctx.holds.append(
            Hold(
                step="compute",
                reason=(
                    "spec_mismatch: 피처 스펙 버전이 달라 스냅샷 비교를 전면 보류한다"
                    f" (현재={current.feature_spec_version}, 기준={baseline.feature_spec_version})"
                ),
            )
        )
    else:
        now_labels = _label_by_customer(current)
        base_labels = _label_by_customer(baseline)
        both = _split_membership(
            ctx, now_labels, base_labels, current=current, baseline=baseline
        )
        matrix = _movement_matrix(ctx, both, now_labels, base_labels)
        deltas = _fill_shift_verdicts(ctx, matrix, baseline=baseline, settings=settings)
        _fill_top_moves(ctx, matrix, settings=settings)
        _apply_delta_size(ctx, deltas)

    _compare_churn_rate(
        ctx,
        churn_now,
        churn_prev,
        baseline_from=baseline_from,
        baseline_to=baseline_to,
        settings=settings,
    )
