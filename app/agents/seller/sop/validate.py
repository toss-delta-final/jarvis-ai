"""V1 입력 검증 — `compute` 와 `interpret` 사이의 LLM 0회 관문 (이슈 #596, `06-REPORT` §4.0.2).

**왜 검증을 두 층으로 가르는가.** 기존 검증(`verifier.py` D1~D3)은 *보고서 텍스트*를 본다.
그런데 텍스트가 틀린 이유가 "입력이 애초에 깨져 있어서"라면 **재작성으로 고쳐지지 않는다** —
같은 ctx 로 다시 쓰면 같은 결과가 나와 재작성 3회를 그냥 태운다. 그래서 "고칠 수 있는 실패"
(LLM 잘못)만 재작성 루프에 남기고, "고칠 수 없는 실패"(입력 잘못)는 이 모듈이 앞에서 끊는다.

[경계 — `sop/compute/*` 승계]
**LLM 0회 · Spring 0회 · DB 0회.** 이미 조회된 ctx·레코드를 인자로 받는 순수 함수다. 그래서
스냅샷 신선도가 미달이어도 여기서 **재계산하지 않는다** — `06` §4.0.2 는 "재계산 또는 Hold"
라 적었으나 재계산은 I-38 재조회 + K-Means 재학습이라 `load` 스텝(후속 이슈) 소관이다.
이 모듈은 `Hold` 로만 드러낸다.

[`verifier.py` 무접촉 — 레지스트리를 왜 나누는가]
`verifier` 의 검사 시그니처는 `(report: str, findings: list[AnalysisFinding])` 다. 여기 검사
대상은 텍스트가 아니라 `AnalysisContext` 라 인자 규약이 다르다. 한 레지스트리에 섞으면 두
검사군의 시그니처가 충돌한다 — 그래서 별 모듈이다(이슈 #596 문구 그대로).

[실측 기준 — 문서와 코드가 갈린 지점 3곳]
1. `06` §4.0.2 ③ 의 `_denom`·`_raw` 는 **ctx 에 없다.** 그 키들은 스냅샷 `feature_rows[]
   .derived` 안, 즉 **고객 1명당 한 벌**이고, `context.py` 규약이 개인 단위 데이터의 ctx
   진입을 금지한다(재식별 금지). 그래서 ctx 실측 대응물인 `Verdict.detail` 의 분모 키
   (`_DENOM_DETAIL_KEYS`)를 표본 검사 대상으로 삼는다.
2. 같은 절의 `ProxyValue.basis` 검사는 **대상이 존재하지 않는다.** `analysis/types.ProxyValue`
   는 정의만 있고 코드베이스 전체 인스턴스화 0건이며 ctx 필드도 아니다(실측 2026-08-11).
   대상이 생기면 그때 검사를 붙인다 — 지금 짜면 영영 안 도는 죽은 코드다.
3. `06` §4.0.2 ① 의 *"p_value 가 비었으면 강등"* 을 문자 그대로 구현하면 `sales_anomaly`
   워커가 **전멸한다.** STL+GESD 는 p값을 내는 검정이 아니라 robust z(sigma)를 내므로
   `compute/sales_anomaly.py` 가 `p_value=None` 인 채 `significant_drop` 을 발행한다. 전건
   `undecided` 가 되면 `gate.should_interpret` 이 그 워커의 LLM 호출을 영구 스킵해 매출 이상
   분석이 보고서에서 조용히 사라진다. → p값 필수 여부를 `method` 로 가른다
   (`_P_VALUE_REQUIRED_METHODS`).

[중복 발행 금지]
`behavior.inherit_snapshot_holds` 가 세운 원칙을 그대로 따른다 — 상류가 이미 같은 사유를
남긴 검사(`spec_mismatch`, 세그먼트 30명 미만 제외)는 **실제로 이 모듈이 무언가를 격리했을
때만** Hold 를 단다. 같은 보류가 두 줄로 보고서에 실리면 판매자가 사고가 두 건이라 읽는다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.agents.seller.analysis_records import SnapshotRecord
from app.agents.seller.sop.context import (
    AnalysisContext,
    Comparison,
    Hold,
    Metric,
    Segment,
    Verdict,
)
from app.core.clock import now_kst
from app.core.config import Settings

logger = logging.getLogger(__name__)

_STEP = "validate"

# p값을 내는 검정만 `p_value` 를 요구한다. `stl_gesd` 는 robust z(`detail["sigma"]`)가 근거라
# p값 자체가 없다 — 여기에 넣으면 매출 이상 판정이 전건 강등된다(모듈 docstring 3번).
_P_VALUE_REQUIRED_METHODS = frozenset({"two_proportion_z"})

_RATIO_UNIT = "비율"

# 음수가 정의상 불가능한 단위 접두. **"원" 은 뺀다** — 환불·취소로 순매출이 음수인 날이
# 실재하고, 그걸 격리하면 진짜 이상치를 지우게 된다(NaN/inf 검사는 모든 단위에 건다).
_NON_NEGATIVE_UNIT_PREFIXES = ("명", "일", "건")

# `Verdict.detail` 안의 '분모' 키 — `06` §4.0.2 ③ 의 `_denom` 에 대응하는 ctx 실측 대상.
# 출처: compute/conversion.py(trials) · compute/churn.py(cohort).
_DENOM_DETAIL_KEYS: tuple[str, ...] = (
    "current_trials",
    "baseline_trials",
    "current_cohort",
    "baseline_cohort",
    "cohort",
)

# 경계일 → 그 개정으로 **정의가 바뀐** 지표 키 접두어.
# 2026-08-06: I-13 `counts` 4종 → 5종(`removeFromCart` 편입, `02-DATA-SOURCES` §E4). 그 날을
# 가로지르는 "총 행동 건수"·"장바구니 이탈률" 비교는 정의가 다른 두 수의 비교다.
# ⚠️ **경계와 무관한 지표까지 막지 않는다.** `churn_rate`(I-16)·`conversion:*`(I-7)은 이 개정과
# 아무 관계가 없어서, 전면 보류로 짜면 멀쩡한 비교가 이유 없이 사라진다(`06` "해당 지표만").
# v1 실측상 ctx 의 Comparison 키는 `segment_size:*`·`churn_rate`·`conversion:*` 뿐이라 이
# 매핑은 아직 아무것도 잡지 않는다 — I-13 유래 지표가 들어오는 순간 자동으로 작동한다.
BOUNDARY_AFFECTED_PREFIXES: dict[str, tuple[str, ...]] = {
    "2026-08-06": ("behavior_counts", "cart_abandon", "remove_from_cart"),
}


@dataclass(frozen=True)
class ValidationResult:
    """V1 판정 결과 — ctx 는 제자리에서 격리되고, 이 객체는 ctx 에 담을 자리가 없는 신호를 나른다.

    `AnalysisContext` 에 필드를 더하지 않는 이유: 그 컨테이너는 *"LLM 이 보는 유일한 입력"*
    이라(`context.py`) 검증 메타를 섞으면 프롬프트에 검증 사정이 실린다.

    ⚠️ `engine.StepFn` 은 반환값이 없다(`Awaitable[None]`). 이 함수를 실제 `Step` 으로 등록할
    때는 결과를 받아 두는 얇은 어댑터가 필요하다 — 그 배선은 `load` 스텝 이슈 소관이다.
    """

    blocked: bool
    """`True` = 보고서 미생성. 호출부가 파이프라인을 여기서 끊는다."""

    citable_dates: frozenset[date]
    """V2-d(`period_grounded`, 후속 이슈)의 허용 집합. **격리가 끝난 뒤** 모은 것이라
    빠진 항목의 날짜는 들어 있지 않다."""

    isolated: tuple[str, ...]
    """격리된 항목의 키 — 관측 이벤트·로그용. `seller_validate_strict=False` 면 실제로는
    격리하지 않았으므로 "격리했을 항목"으로 읽는다."""


class _Recorder:
    """검사 결과 수집기 — `strict=False` 면 Hold 만 남기고 ctx 를 고치지 않는다."""

    def __init__(self, *, strict: bool) -> None:
        self.strict = strict
        self.holds: list[Hold] = []
        self.isolated: list[str] = []
        self.blocked = False

    def note(self, code: str, detail: str) -> None:
        """격리 없이 사실만 남긴다(스냅샷 신선도처럼 뺄 대상이 없는 검사)."""
        self.holds.append(Hold(step=_STEP, reason=f"{code}: {detail}"))

    def flag(self, code: str, key: str, detail: str) -> bool:
        """실패를 기록하고 **실제로 격리할지** 돌려준다(`strict` 그대로)."""
        self.note(code, detail)
        self.isolated.append(key)
        return self.strict

    def block(self) -> None:
        """보고서 미생성 판정. 킬스위치가 꺼져 있으면 막지 않는다(경고만 모드의 정의)."""
        if self.strict:
            self.blocked = True


def validate_context(
    ctx: AnalysisContext,
    *,
    settings: Settings,
    current_snapshot: SnapshotRecord | None = None,
    baseline_snapshot: SnapshotRecord | None = None,
    now: datetime | None = None,
) -> ValidationResult:
    """`ctx` 가 보고서를 쓸 자격이 있는지 코드가 판정한다 — LLM 0회 · I/O 0회.

    ctx 를 **제자리에서** 고치고(격리) `ctx.holds` 에 사유를 남긴다. "격리하되 숨기지 않는다"
    가 원칙이다 — 못 쓸 수치는 빼되 뺐다는 사실은 남긴다("판정 보류 ≠ 이상 없음").

    스냅샷 인자는 선택이다. `ctx` 에는 `feature_spec_version` 도 `computed_at` 도 없어서
    (`context.py` 필드 목록 실측) 두 검사는 인자가 있을 때만 돈다. 없으면 조용히 건너뛴다.

    `now` 는 신선도 계산 기준시각이다 — 기본은 `clock.now_kst()`(컨테이너 TZ 무관, #583).
    """
    recorder = _Recorder(strict=settings.seller_validate_strict)
    resolved_now = now if now is not None else now_kst()

    # ② 순서 — 가장 먼저 본다. 기간이 뒤집혔으면 나머지 검사의 결론이 전부 무의미하다.
    if ctx.period_from > ctx.period_to:
        recorder.flag(
            "period_reversed",
            "period",
            f"분석 기간이 역전됐다 ({ctx.period_from.isoformat()} > {ctx.period_to.isoformat()})",
        )
        recorder.block()
        return _finish(ctx, recorder, current_snapshot, baseline_snapshot)

    _check_metrics(ctx, recorder)
    _check_segments(ctx, recorder, settings)
    _check_comparisons(ctx, recorder, settings)
    _check_verdicts(ctx, recorder, settings)
    _check_snapshots(ctx, recorder, settings, current_snapshot, baseline_snapshot, resolved_now)
    _check_material(ctx, recorder)

    return _finish(ctx, recorder, current_snapshot, baseline_snapshot)


def _finish(
    ctx: AnalysisContext,
    recorder: _Recorder,
    current_snapshot: SnapshotRecord | None,
    baseline_snapshot: SnapshotRecord | None,
) -> ValidationResult:
    """보류 반영 + 인용 가능 기간 수집. **수집은 격리 이후다** — 빠진 항목의 날짜는 인용 불가."""
    ctx.holds.extend(recorder.holds)
    return ValidationResult(
        blocked=recorder.blocked,
        citable_dates=_collect_citable_dates(ctx, current_snapshot, baseline_snapshot),
        isolated=tuple(recorder.isolated),
    )


# ── ① 숫자 정합성 ────────────────────────────────────────────────────────────────


def _metric_defect(metric: Metric) -> tuple[str, str] | None:
    """지표 1건의 결함 — `(코드, 설명)` 또는 결함 없음(`None`)."""
    value = metric.value
    if value is None:
        # 결측은 결함이 아니라 정상 표기다 — `Metric` 규약이 0 위장을 금지한 결과물이다.
        return None
    if not math.isfinite(value):
        return ("metric_not_finite", f"{metric.key}={value!r} 은 유한한 수가 아니다")
    if metric.unit == _RATIO_UNIT and not 0.0 <= value <= 1.0:
        return ("metric_out_of_range", f"{metric.key}={value:g} 가 비율 정의역 [0,1] 밖이다")
    if value < 0 and metric.unit.startswith(_NON_NEGATIVE_UNIT_PREFIXES):
        return (
            "metric_negative_count",
            f"{metric.key}={value:g} 는 음수일 수 없는 단위('{metric.unit}')다",
        )
    return None


def _check_metrics(ctx: AnalysisContext, recorder: _Recorder) -> None:
    """범위 밖·비유한 지표를 ctx 에서 뺀다.

    `churn_rate` 가 실제 표적이다 — `compute/churn.py` 는 `_metric(...)` 을 usable 검사보다
    **먼저** 부르므로, I-16 이 `churnRate: 1.7` 을 내려보내면 `verdict` 는 `undecided` 로
    막히지만 **`metrics` 에는 1.7 이 그대로 남는다**. LLM 이 "이탈률 170%" 를 서술할 수 있는
    경로가 여기다.
    """
    kept: list[Metric] = []
    for metric in ctx.metrics:
        defect = _metric_defect(metric)
        if defect is None or not recorder.flag(defect[0], metric.key, defect[1]):
            kept.append(metric)
    ctx.metrics[:] = kept


def _check_segments(ctx: AnalysisContext, recorder: _Recorder, settings: Settings) -> None:
    """세그먼트 방어 검사 — 통상 아무것도 하지 않는다(상류가 이미 걸렀다).

    30명 미만 제외는 `compute/behavior.fill_segments` 가 이미 한다(그 docstring 이 이 이슈를
    직접 인용한다). `ratio_to_mean` 의 분모 0 도 `features/clustering._ratio_to_mean` 이
    `if overall.get(key)` 로 이미 뺀다. 그래서 여기 남는 몫은 **비유한 값 방어**뿐이고,
    Hold 는 실제로 뺀 것이 있을 때만 붙는다.
    """
    min_size = settings.seller_customer_segment_min_size
    kept: list[Segment] = []
    for segment in ctx.segments:
        if segment.size < min_size and recorder.flag(
            "segment_too_small",
            segment.rule_label,
            f"'{segment.display_label or segment.rule_label}' {segment.size}명은"
            f" 최소 군집 {min_size}명 미만이라 제외한다",
        ):
            continue
        broken = sorted(
            key
            for key, value in segment.ratio_to_mean.items()
            if not math.isfinite(value) or value < 0
        )
        if broken and recorder.flag(
            "ratio_not_finite",
            f"{segment.rule_label}.ratio_to_mean",
            f"'{segment.rule_label}' 배수 축 {', '.join(broken)} 이 유한한 양수가 아니다",
        ):
            segment.ratio_to_mean = {
                key: value for key, value in segment.ratio_to_mean.items() if key not in broken
            }
        kept.append(segment)
    ctx.segments[:] = kept


def _significance_defect(verdict: Verdict) -> tuple[str, str] | None:
    """판정의 근거 표기 결함. **`p_value` 필수 여부는 `method` 가 정한다**(모듈 docstring 3번)."""
    if not verdict.method.strip():
        return (
            "verdict_no_method",
            f"{verdict.key} 판정 기법이 비어 있다 — 무엇으로 판정했는지 밝힐 수 없다",
        )
    if verdict.method in _P_VALUE_REQUIRED_METHODS and verdict.p_value is None:
        return (
            "verdict_no_p_value",
            f"{verdict.key} 는 {verdict.method} 판정인데 p_value 가 없다",
        )
    if verdict.p_value is not None and not (
        math.isfinite(verdict.p_value) and 0.0 <= verdict.p_value <= 1.0
    ):
        return (
            "verdict_bad_p_value",
            f"{verdict.key} p_value={verdict.p_value!r} 는 확률이 아니다",
        )
    return None


# ── ③ evidence 충분성 ────────────────────────────────────────────────────────────


def _denom_defect(verdict: Verdict, min_denom: int) -> tuple[str, str] | None:
    """표본 부족 — 분모가 `seller_feature_min_denom` 미만이면 비율을 인용할 수 없다."""
    for key in _DENOM_DETAIL_KEYS:
        value = verdict.detail.get(key)
        if value is None:
            continue
        if not math.isfinite(value) or value < min_denom:
            return (
                "insufficient_denom",
                f"{verdict.key} 의 {key}={value:g} 는 최소 표본 {min_denom} 미만이라"
                " 비율을 인용할 수 없다",
            )
    return None


def _check_verdicts(ctx: AnalysisContext, recorder: _Recorder, settings: Settings) -> None:
    """판정을 `undecided` 로 강등한다 — **제거하지 않는다**(보류도 정보다).

    이미 `undecided` 인 판정은 건너뛴다. `compute/conversion.py` 는 `trials <= 0` 을 이미
    보류로 만들어 두는데, 그걸 다시 강등하면 판정은 그대로인 채 Hold 만 두 줄이 된다.

    표본 부족(`insufficient_denom`)으로 강등된 키는 **같은 키의 `Comparison` 도 뺀다** —
    인용 금지의 실체가 그 비율 수치이기 때문이다. 반면 근거 표기 결함(method/p_value)은
    수치 자체가 거짓이라는 뜻이 아니므로 비교를 남긴다.
    """
    min_denom = settings.seller_feature_min_denom
    denom_blocked: set[str] = set()
    for index, verdict in enumerate(ctx.verdicts):
        if verdict.verdict == "undecided":
            continue
        defect = _significance_defect(verdict) or _denom_defect(verdict, min_denom)
        if defect is None:
            continue
        code, detail = defect
        if not recorder.flag(code, verdict.key, detail):
            continue
        ctx.verdicts[index] = verdict.model_copy(update={"verdict": "undecided"})
        if code == "insufficient_denom":
            denom_blocked.add(verdict.key)
    if denom_blocked:
        ctx.comparisons[:] = [
            comparison for comparison in ctx.comparisons if comparison.key not in denom_blocked
        ]


# ── ② 기간 정합성 ────────────────────────────────────────────────────────────────


def _delta_defect(comparison: Comparison) -> tuple[str, str] | None:
    """증감률이 '정의 불가' 를 수치로 위장하지 않았는지."""
    if comparison.delta_pct is None:
        return None
    if not math.isfinite(comparison.delta_pct):
        return ("delta_not_finite", f"{comparison.key} 증감률이 유한한 수가 아니다")
    if comparison.baseline == 0:
        return (
            "delta_undefined",
            f"{comparison.key} 기준값이 0 인데 증감률 {comparison.delta_pct:g}% 가 유한하다"
            " — 정의 불가를 수치로 위장했다",
        )
    return None


def _overlap_defect(
    comparison: Comparison, ctx: AnalysisContext, settings: Settings
) -> tuple[str, str] | None:
    """비교 기준 기간이 분석 기간과 겹치는가.

    `06` §4.0.2 는 단일 `compared_*` 를 전제로 "비교 전면 보류" 라 적었으나, 실측 ctx 는
    **`Comparison` 마다** `baseline_from/to` 를 갖는다(`context.py`). 그래서 건별로 본다 —
    한 워커 안에서는 기준 기간이 같아 결과가 "전면 보류" 와 일치하고, 워커별로 기준이
    갈리는 경우에만 정밀하게 동작한다.
    """
    if not settings.seller_period_overlap_guard:
        return None
    if comparison.baseline_from > comparison.baseline_to:
        return (
            "comparison_period_reversed",
            f"{comparison.key} 비교 기준 기간이 역전됐다"
            f" ({comparison.baseline_from.isoformat()} > {comparison.baseline_to.isoformat()})",
        )
    if comparison.baseline_from <= ctx.period_to and ctx.period_from <= comparison.baseline_to:
        return (
            "comparison_overlap",
            f"{comparison.key} 비교 기준 기간"
            f" {comparison.baseline_from.isoformat()}~{comparison.baseline_to.isoformat()} 이"
            f" 분석 기간 {ctx.period_from.isoformat()}~{ctx.period_to.isoformat()} 과 겹친다",
        )
    return None


def _boundary_defect(
    comparison: Comparison, ctx: AnalysisContext, boundaries: list[date]
) -> tuple[str, str] | None:
    """비교 금지 경계를 가로지르는가 — **영향 지표만** 본다(`BOUNDARY_AFFECTED_PREFIXES`)."""
    for boundary in boundaries:
        prefixes = BOUNDARY_AFFECTED_PREFIXES.get(boundary.isoformat())
        if not prefixes or not comparison.key.startswith(prefixes):
            continue
        if comparison.baseline_to < boundary <= ctx.period_to:
            return (
                "comparison_boundary",
                f"{comparison.key} 비교가 지표 정의 개정일 {boundary.isoformat()} 을 가로지른다"
                " — 정의가 다른 두 수의 비교라 보류한다",
            )
    return None


def _boundary_dates(settings: Settings) -> list[date]:
    """설정된 경계일 중 **영향 지표 매핑이 있는 것만** 돌려준다.

    매핑에 없는 경계일을 조용히 흘리면 "설정했는데 안 도는" 상태가 눈에 띄지 않는다 —
    로그로 남긴다. 경계 하나가 어느 지표를 흔드는지는 튜너블이 아니라 계약 사실이라
    Settings 가 아니라 코드 상수다.
    """
    known: list[date] = []
    for boundary in settings.seller_comparison_boundary_dates:
        if boundary.isoformat() in BOUNDARY_AFFECTED_PREFIXES:
            known.append(boundary)
        else:
            logger.info(
                "비교 금지 경계 %s 에 영향 지표 매핑이 없어 검사 대상이 없다"
                " (validate.BOUNDARY_AFFECTED_PREFIXES)",
                boundary.isoformat(),
            )
    return known


def _check_comparisons(ctx: AnalysisContext, recorder: _Recorder, settings: Settings) -> None:
    """비교 1건씩 델타·겹침·경계를 보고, 걸리면 그 비교만 뺀다."""
    boundaries = _boundary_dates(settings)
    kept: list[Comparison] = []
    for comparison in ctx.comparisons:
        defect = (
            _delta_defect(comparison)
            or _overlap_defect(comparison, ctx, settings)
            or _boundary_defect(comparison, ctx, boundaries)
        )
        if defect is None or not recorder.flag(defect[0], comparison.key, defect[1]):
            kept.append(comparison)
    ctx.comparisons[:] = kept


def _check_snapshots(
    ctx: AnalysisContext,
    recorder: _Recorder,
    settings: Settings,
    current: SnapshotRecord | None,
    baseline: SnapshotRecord | None,
    now: datetime,
) -> None:
    """스냅샷 축 검사 2종 — 둘 다 뺄 대상이 없어 `Hold` 로만 드러낸다."""
    if (
        current is not None
        and baseline is not None
        and current.feature_spec_version != baseline.feature_spec_version
        # `compute_churn` 이 같은 사유를 이미 남겼으면 두 번 쓰지 않는다.
        and not any(hold.reason.startswith("spec_mismatch:") for hold in ctx.holds)
    ):
        recorder.note(
            "spec_mismatch",
            "피처 스펙 버전이 달라 스냅샷 비교를 보류한다"
            f" (현재={current.feature_spec_version}, 기준={baseline.feature_spec_version})",
        )

    if current is None:
        return
    computed_at = current.computed_at
    if computed_at.tzinfo is None:
        # DB 왕복에서 tz 가 떨어져 나온 경우 — 저장은 UTC 기준이다(`analysis_records`).
        computed_at = computed_at.replace(tzinfo=UTC)
    age_hours = (now - computed_at).total_seconds() / 3600.0
    limit = settings.seller_snapshot_freshness_hours
    if age_hours > limit:
        recorder.note(
            "snapshot_stale",
            f"고객 피처 스냅샷이 {age_hours:.1f}시간 전 것이라 신선도 상한 {limit:g}시간을"
            " 넘었다 (재계산은 load 스텝 소관 — 여기서는 보류만 남긴다)",
        )


def _check_material(ctx: AnalysisContext, recorder: _Recorder) -> None:
    """격리 후 서술할 재료가 하나도 없으면 보고서를 만들지 않는다.

    `holds` 만 남은 ctx 로 보고서를 쓰면 본문이 한계 고지뿐인 문서가 된다 — 그건 보고서가
    아니라 실패 통지다. 무인 실행 실패 규약(`01` §8)의 경로로 보낸다.
    """
    if ctx.metrics or ctx.verdicts or ctx.segments or ctx.product_flags:
        return
    recorder.note("no_material", "격리 후 서술할 재료가 남지 않아 보고서를 생성하지 않는다")
    recorder.block()


def _collect_citable_dates(
    ctx: AnalysisContext,
    current: SnapshotRecord | None,
    baseline: SnapshotRecord | None,
) -> frozenset[date]:
    """보고서가 인용해도 되는 날짜 전부 — V2-d(`period_grounded`)의 허용 집합.

    `06` §4.0.1 이 발견한 구멍을 메우는 재료다: D2 는 날짜를 **마스킹으로 지우고**
    (`verifier._DATE_MASK_RES`) 월·일은 자릿수 미달로 제외하므로, LLM 이 "8월 3일" 을
    "7월 28일" 로 잘못 써도 어떤 검사에도 걸리지 않는다. 그 대조 기준이 이 집합이다.
    """
    dates: set[date] = {ctx.period_from, ctx.period_to}
    for metric in ctx.metrics:
        dates.update((metric.period_from, metric.period_to))
    for comparison in ctx.comparisons:
        dates.update((comparison.baseline_from, comparison.baseline_to))
    for cause in ctx.causes:
        dates.add(cause.event_at)
    for snapshot in (current, baseline):
        if snapshot is None:
            continue
        dates.update((snapshot.period_from, snapshot.period_to, snapshot.computed_at.date()))
    return frozenset(dates)
