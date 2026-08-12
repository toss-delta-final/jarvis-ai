"""`conversion` compute — I-7 퍼널 2기간 비교 (이슈 #594, `01` §4.4 표 conversion 행).

`analysis.proportions`(Wilson CI + pooled two-proportion z) 재사용. 단계는 3종으로
`calc.conversion_rates` 선례를 그대로 따른다 — overall(view→purchase)은 BE 가 함께
내려주지만 세 단계의 곱이라 독립 판정이 아니고, 병목 식별에도 기여하지 않는다.

⚠️ **I-7 은 이벤트 카운트다**(`SellerAnalyticsService.funnel` — `countSellerFunnelEvents`).
한 사람이 조회 없이 목록에서 바로 담을 수 있어 `cart > view` 역전이 실데이터에서 나오고,
`wilson_interval` 은 그 입력을 `ValueError` 로 거부한다. `tools.py` 의 `_stage_summary` 가
이미 정한 규약을 그대로 쓴다 — **clamp 로 정상 CI 처럼 위장하지 않고 그 단계만 보류한다.**
"""

from __future__ import annotations

from datetime import date, timedelta

from app.agents.seller.analysis import proportions
from app.agents.seller.sop.context import AnalysisContext, Comparison, Hold, Metric, Verdict
from app.core.config import Settings
from app.schemas.spring import FunnelResult

_METHOD = "two_proportion_z"

# (판정 키, 표시명, 분자 필드, 분모 필드) — `calc.conversion_rates` 와 같은 3단계.
_STAGES: tuple[tuple[str, str, str, str], ...] = (
    ("view_to_cart", "view→cart", "cart", "view"),
    ("cart_to_checkout", "cart→checkout", "checkout", "cart"),
    ("checkout_to_purchase", "checkout→purchase", "purchase", "checkout"),
)

_COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("view", "조회"),
    ("cart", "담기"),
    ("checkout", "결제 시작"),
    ("purchase", "구매"),
)


def _fill_counts(ctx: AnalysisContext, funnel: FunnelResult) -> None:
    """현재 기간의 단계별 카운트 — 미집계 단계는 `value=None` 이다.

    0 으로 채우면 "집계 안 됨"이 "실제 0건"으로 둔갑한다(`Metric` 결측 표기 규약).
    """
    uncomputable = set(funnel.uncomputable_stages)
    for field, unit_label in _COUNT_FIELDS:
        value = None if field in uncomputable else float(getattr(funnel, field))
        ctx.metrics.append(
            Metric(
                key=f"funnel_{field}",
                value=value,
                unit=f"건({unit_label})",
                source="I-7",
                period_from=ctx.period_from,
                period_to=ctx.period_to,
            )
        )


def compute_conversion(
    ctx: AnalysisContext,
    current: FunnelResult,
    baseline: FunnelResult,
    *,
    baseline_from: date | None = None,
    baseline_to: date | None = None,
    settings: Settings,
) -> None:
    """단계별 전환율 유의성 판정 — LLM 0회.

    `baseline_from`/`baseline_to` 는 `Comparison` 표기용 기간이다. 비교 기간을 인자로 받는
    이유는 브랜드 축 비교 기준(직전 7일, `10-TRIGGER` 결정 97)을 호출부가 정하기 때문이다.
    생략하면 ctx 기간과 같은 길이의 **인접 직전 구간**으로 채운다.
    """
    span = ctx.period_to - ctx.period_from
    resolved_to = baseline_to if baseline_to is not None else ctx.period_from - timedelta(days=1)
    resolved_from = baseline_from if baseline_from is not None else resolved_to - span

    _fill_counts(ctx, current)
    uncomputable = set(current.uncomputable_stages) | set(baseline.uncomputable_stages)

    for key, label, numerator_field, denominator_field in _STAGES:
        verdict_key = f"conversion:{key}"
        if numerator_field in uncomputable or denominator_field in uncomputable:
            # count=null·computable=false 는 "0건"이 아니다(`02` §4).
            ctx.verdicts.append(Verdict(key=verdict_key, verdict="undecided", method=_METHOD))
            ctx.holds.append(
                Hold(step="compute", reason=f"uncomputable_stage: {label} 미집계 — 판정 보류")
            )
            continue

        current_successes = int(getattr(current, numerator_field))
        current_trials = int(getattr(current, denominator_field))
        baseline_successes = int(getattr(baseline, numerator_field))
        baseline_trials = int(getattr(baseline, denominator_field))

        if current_trials <= 0 or baseline_trials <= 0:
            # 표본 없음은 실측이다 — 보류로 두되 Hold 까지 달면 정상 저볼륨이 사고로 읽힌다.
            ctx.verdicts.append(
                Verdict(
                    key=verdict_key,
                    verdict="undecided",
                    method=_METHOD,
                    detail={
                        "current_trials": float(current_trials),
                        "baseline_trials": float(baseline_trials),
                    },
                )
            )
            continue

        try:
            comparison = proportions.compare_rates(
                current_successes,
                current_trials,
                baseline_successes,
                baseline_trials,
                alpha=settings.seller_rate_test_alpha,
                confidence=settings.seller_wilson_confidence,
            )
        except ValueError as exc:
            ctx.verdicts.append(Verdict(key=verdict_key, verdict="undecided", method=_METHOD))
            ctx.holds.append(
                Hold(
                    step="compute",
                    reason=(
                        f"funnel_inconsistent: {label} 단계 카운트 정합 이상으로 검정 불가"
                        f" — {exc}"
                    ),
                )
            )
            continue

        ctx.verdicts.append(
            Verdict(
                key=verdict_key,
                verdict=comparison.verdict,
                method=_METHOD,
                p_value=comparison.p_value,
                detail={
                    "current_rate": comparison.current.rate,
                    "baseline_rate": comparison.baseline.rate,
                    "ci_low": comparison.current.ci_low,
                    "ci_high": comparison.current.ci_high,
                    "current_trials": float(current_trials),
                    "baseline_trials": float(baseline_trials),
                },
            )
        )
        ctx.comparisons.append(
            Comparison(
                key=verdict_key,
                current=comparison.current.rate,
                baseline=comparison.baseline.rate,
                delta_pct=(
                    (comparison.current.rate - comparison.baseline.rate)
                    / comparison.baseline.rate
                    * 100.0
                    if comparison.baseline.rate > 0
                    else None
                ),
                baseline_from=resolved_from,
                baseline_to=resolved_to,
            )
        )
