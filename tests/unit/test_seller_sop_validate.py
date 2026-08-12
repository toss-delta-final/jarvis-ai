"""sop/validate.py — V1 입력 검증 (이슈 #596, `06-REPORT` §4.0.2).

이슈 완료 조건 3건(churnRate 1.7 격리 · 기간 겹침 보류 · 표본 3명 인용 금지)을 그대로
못 박고, 문서를 문자 그대로 구현하면 밟는 함정 하나를 회귀 테스트로 막는다 —
**`stl_gesd` 는 p값을 내지 않으므로 p_value 부재로 강등하면 매출 이상 판정이 전멸한다.**
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.agents.seller.analysis_records import SnapshotRecord
from app.agents.seller.sop.context import (
    AnalysisContext,
    CauseCandidate,
    Comparison,
    Hold,
    Metric,
    Segment,
    Verdict,
)
from app.agents.seller.sop.validate import validate_context
from app.core.config import Settings

_FROM = date(2026, 8, 3)
_TO = date(2026, 8, 9)
_NOW = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _ctx(**overrides) -> AnalysisContext:
    return AnalysisContext(
        worker="churn",
        brand_id=7,
        period_from=_FROM,
        period_to=_TO,
        **overrides,
    )


def _metric(key: str, value: float | None, unit: str = "명") -> Metric:
    return Metric(key=key, value=value, unit=unit, source="calc", period_from=_FROM, period_to=_TO)


def _comparison(
    key: str = "churn_rate",
    *,
    current: float = 0.4,
    baseline: float = 0.3,
    delta_pct: float | None = 33.3,
    baseline_from: date = date(2026, 7, 27),
    baseline_to: date = date(2026, 8, 2),
) -> Comparison:
    return Comparison(
        key=key,
        current=current,
        baseline=baseline,
        delta_pct=delta_pct,
        baseline_from=baseline_from,
        baseline_to=baseline_to,
    )


def _snapshot(*, computed_at: datetime, spec_version: str = "fe_v1") -> SnapshotRecord:
    return SnapshotRecord(
        id=uuid4(),
        brand_id=7,
        period_from=_FROM,
        period_to=_TO,
        computed_at=computed_at,
        source="i38_v1",
        feature_spec_version=spec_version,
        total_customers=900,
        row_limit=1000,
        truncated=False,
        insufficient_cohort=False,
        scaler_params={},
        pca_used=False,
        random_state=42,
        clusters=[],
        feature_rows=[],
    )


def _reasons(ctx: AnalysisContext) -> list[str]:
    return [hold.reason for hold in ctx.holds]


def _has(ctx: AnalysisContext, code: str) -> bool:
    return any(reason.startswith(f"{code}:") for reason in _reasons(ctx))


# ── 이슈 완료 조건 3건 ────────────────────────────────────────────────────────────


def test_범위_밖_이탈률은_격리되고_보류가_남는다() -> None:
    """완료조건 ① — I-16 이 churnRate 1.7 을 내려보낸 경우.

    `compute/churn.py` 는 `_metric(...)` 을 usable 검사보다 **먼저** 부르므로 verdict 는
    막혀도 metric 에는 1.7 이 남는다. 그 구멍을 V1 이 메운다.
    """
    ctx = _ctx(metrics=[_metric("churn_rate", 1.7, unit="비율"), _metric("cohort_total", 900.0)])

    result = validate_context(ctx, settings=_settings(), now=_NOW)

    assert [metric.key for metric in ctx.metrics] == ["cohort_total"]
    assert _has(ctx, "metric_out_of_range")
    assert result.isolated == ("churn_rate",)
    assert result.blocked is False


def test_비교_기간이_겹치면_그_비교를_보류한다() -> None:
    """완료조건 ② — 기준 기간이 분석 기간(8/3~8/9)을 파고든 경우."""
    ctx = _ctx(
        metrics=[_metric("cohort_total", 900.0)],
        comparisons=[
            _comparison("churn_rate", baseline_from=date(2026, 8, 1), baseline_to=date(2026, 8, 5)),
            _comparison("conversion:view_to_cart"),
        ],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert [comparison.key for comparison in ctx.comparisons] == ["conversion:view_to_cart"]
    assert _has(ctx, "comparison_overlap")


def test_표본_3명짜리_판정은_undecided로_강등된다() -> None:
    """완료조건 ③ — `seller_feature_min_denom`(5) 미만 표본의 비율은 인용 금지."""
    ctx = _ctx(
        verdicts=[
            Verdict(
                key="conversion:cart_to_checkout",
                verdict="significant_drop",
                method="two_proportion_z",
                p_value=0.01,
                detail={"current_trials": 3.0, "baseline_trials": 120.0},
            )
        ],
        comparisons=[_comparison("conversion:cart_to_checkout")],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.verdicts[0].verdict == "undecided"
    # 인용 금지의 실체는 그 비율 수치다 — 같은 키의 비교도 함께 뺀다.
    assert ctx.comparisons == []
    assert _has(ctx, "insufficient_denom")


# ── 🔴 회귀 방지 — 문서를 문자 그대로 구현하면 밟는 함정 ────────────────────────────


def test_stl_gesd_판정은_p값이_없어도_강등되지_않는다() -> None:
    """`06` §4.0.2 ① 을 문자 그대로 짜면 sales_anomaly 워커가 전멸한다.

    STL+GESD 는 p값을 내는 검정이 아니라 robust z(`detail["sigma"]`)를 낸다. 전건 강등되면
    `gate.should_interpret` 이 그 워커의 LLM 호출을 영구 스킵해 매출 이상 분석이 보고서에서
    조용히 사라진다. p값 필수 여부는 `method` 가 정한다.
    """
    ctx = _ctx(
        verdicts=[
            Verdict(
                key="sales_anomaly:2026-08-05",
                verdict="significant_drop",
                method="stl_gesd",
                detail={"sample_size": 28.0, "sigma": 3.8, "actual": 120000.0},
            )
        ]
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.verdicts[0].verdict == "significant_drop"
    assert _reasons(ctx) == []


def test_p값을_내는_검정인데_p값이_없으면_강등된다() -> None:
    ctx = _ctx(
        verdicts=[Verdict(key="churn_rate", verdict="significant_rise", method="two_proportion_z")]
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.verdicts[0].verdict == "undecided"
    assert _has(ctx, "verdict_no_p_value")


def test_method가_비면_기법과_무관하게_강등된다() -> None:
    """무엇으로 판정했는지 못 밝히는 판정은 근거가 없는 것과 같다."""
    ctx = _ctx(verdicts=[Verdict(key="k", verdict="significant_drop", method="  ")])

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.verdicts[0].verdict == "undecided"
    assert _has(ctx, "verdict_no_method")


def test_이미_보류인_판정은_다시_강등하지_않는다() -> None:
    """`compute/conversion.py` 가 trials<=0 을 이미 보류로 만든다 — 두 번 세면 Hold 만 늘어난다."""
    ctx = _ctx(
        verdicts=[
            Verdict(
                key="conversion:view_to_cart",
                verdict="undecided",
                method="two_proportion_z",
                detail={"current_trials": 0.0, "baseline_trials": 0.0},
            )
        ]
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert _reasons(ctx) == []


# ── ① 숫자 정합성 ────────────────────────────────────────────────────────────────


def test_음수_카운트는_격리하지만_음수_매출은_남긴다() -> None:
    """환불·취소로 순매출이 음수인 날은 실재한다 — 격리하면 진짜 이상치를 지운다."""
    ctx = _ctx(
        metrics=[
            _metric("membership_new", -3.0, unit="명"),
            _metric("sales:2026-08-05", -120000.0, unit="원"),
        ]
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert [metric.key for metric in ctx.metrics] == ["sales:2026-08-05"]
    assert _has(ctx, "metric_negative_count")


def test_결측_지표는_결함이_아니다() -> None:
    """`value=None` 은 "미집계" 의 정상 표기다 — 0 으로 위장하지 않은 상태다."""
    ctx = _ctx(metrics=[_metric("funnel_cart", None, unit="건(담기)")])

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert len(ctx.metrics) == 1
    assert _reasons(ctx) == []


def test_소수점_비율은_그대로_통과한다() -> None:
    ctx = _ctx(metrics=[_metric("churn_rate", 0.0, unit="비율"), _metric("x", 1.0, unit="비율")])

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert len(ctx.metrics) == 2
    assert _reasons(ctx) == []


def test_비유한_지표는_단위와_무관하게_격리된다() -> None:
    ctx = _ctx(metrics=[_metric("sales:2026-08-05", float("inf"), unit="원"), _metric("k", 1.0)])

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert [metric.key for metric in ctx.metrics] == ["k"]
    assert _has(ctx, "metric_not_finite")


def test_기준값_0인데_증감률이_유한하면_그_비교를_뺀다() -> None:
    """정의 불가(None)를 수치로 위장한 상태 — `Comparison` 규약 위반이다."""
    ctx = _ctx(
        metrics=[_metric("k", 1.0)],
        comparisons=[_comparison("segment_size:충성형", baseline=0.0, delta_pct=100.0)],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.comparisons == []
    assert _has(ctx, "delta_undefined")


def test_배수_축이_비유한하면_그_축만_뺀다() -> None:
    """세그먼트 자체는 남긴다 — 축 하나 때문에 군집 전체를 지우면 손실이 크다."""
    ctx = _ctx(
        segments=[
            Segment(
                rule_label="충성형",
                display_label="충성형",
                size=96,
                ratio_to_mean={"orderCount": 2.3, "cartAdds": float("nan")},
            )
        ]
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert len(ctx.segments) == 1
    assert ctx.segments[0].ratio_to_mean == {"orderCount": 2.3}
    assert _has(ctx, "ratio_not_finite")


# ── ② 기간 정합성 ────────────────────────────────────────────────────────────────


def test_기간_역전은_보고서를_막는다() -> None:
    """나머지 검사의 결론이 전부 무의미해지므로 즉시 중단한다."""
    ctx = AnalysisContext(
        worker="churn",
        brand_id=7,
        period_from=date(2026, 8, 9),
        period_to=date(2026, 8, 3),
        metrics=[_metric("k", 1.7, unit="비율")],
    )

    result = validate_context(ctx, settings=_settings(), now=_NOW)

    assert result.blocked is True
    assert _has(ctx, "period_reversed")
    # 즉시 반환이라 뒤 검사는 돌지 않는다 — 깨진 기간 위에서 낸 판정은 신뢰할 수 없다.
    assert not _has(ctx, "metric_out_of_range")


def test_겹침_가드를_끄면_겹쳐도_남긴다() -> None:
    ctx = _ctx(
        metrics=[_metric("k", 1.0)],
        comparisons=[_comparison(baseline_from=date(2026, 8, 1), baseline_to=date(2026, 8, 5))],
    )

    validate_context(ctx, settings=_settings(seller_period_overlap_guard=False), now=_NOW)

    assert len(ctx.comparisons) == 1
    assert _reasons(ctx) == []


def test_경계_영향_지표는_가로지르면_보류된다() -> None:
    """2026-08-06 = I-13 counts 4종 → 5종(removeFromCart 편입). 정의가 다른 두 수의 비교다."""
    ctx = _ctx(
        metrics=[_metric("k", 1.0)],
        comparisons=[
            _comparison(
                "cart_abandon_rate",
                baseline_from=date(2026, 7, 27),
                baseline_to=date(2026, 8, 2),
            )
        ],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.comparisons == []
    assert _has(ctx, "comparison_boundary")


def test_경계_매핑_밖_지표는_가로질러도_남는다() -> None:
    """`churn_rate`(I-16)·`conversion:*`(I-7)은 removeFromCart 개정과 무관하다 — 과차단 금지."""
    ctx = _ctx(
        metrics=[_metric("k", 1.0)],
        comparisons=[
            _comparison(
                "churn_rate",
                baseline_from=date(2026, 7, 27),
                baseline_to=date(2026, 8, 2),
            ),
            _comparison(
                "conversion:view_to_cart",
                baseline_from=date(2026, 7, 27),
                baseline_to=date(2026, 8, 2),
            ),
        ],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert len(ctx.comparisons) == 2
    assert not _has(ctx, "comparison_boundary")


def test_경계_이전에_끝난_비교는_가로지르지_않는다() -> None:
    ctx = AnalysisContext(
        worker="churn",
        brand_id=7,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 4),
        metrics=[_metric("k", 1.0)],
        comparisons=[
            _comparison(
                "cart_abandon_rate",
                baseline_from=date(2026, 7, 25),
                baseline_to=date(2026, 7, 31),
            )
        ],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert len(ctx.comparisons) == 1
    assert not _has(ctx, "comparison_boundary")


def test_스냅샷이_낡으면_보류만_남긴다() -> None:
    """재계산은 load 스텝 소관이다 — 검증 함수는 I/O 경계를 넘지 않는다."""
    ctx = _ctx(metrics=[_metric("k", 1.0)])
    stale = _snapshot(computed_at=_NOW - timedelta(hours=30))

    result = validate_context(ctx, settings=_settings(), now=_NOW, current_snapshot=stale)

    assert _has(ctx, "snapshot_stale")
    assert result.blocked is False
    assert result.isolated == ()


def test_신선한_스냅샷은_보류를_남기지_않는다() -> None:
    ctx = _ctx(metrics=[_metric("k", 1.0)])
    fresh = _snapshot(computed_at=_NOW - timedelta(hours=2))

    validate_context(ctx, settings=_settings(), now=_NOW, current_snapshot=fresh)

    assert _reasons(ctx) == []


def test_이미_기록된_spec_mismatch는_두_번_남기지_않는다() -> None:
    """`compute_churn` 이 같은 사유를 이미 남긴다 — 두 줄이면 판매자가 사고 2건으로 읽는다."""
    ctx = _ctx(
        metrics=[_metric("k", 1.0)],
        holds=[Hold(step="compute", reason="spec_mismatch: 피처 스펙 버전이 달라 ...")],
    )

    validate_context(
        ctx,
        settings=_settings(),
        now=_NOW,
        current_snapshot=_snapshot(computed_at=_NOW, spec_version="fe_v2"),
        baseline_snapshot=_snapshot(computed_at=_NOW, spec_version="fe_v1"),
    )

    assert sum(1 for reason in _reasons(ctx) if reason.startswith("spec_mismatch:")) == 1


def test_spec_불일치가_처음이면_보류를_남긴다() -> None:
    ctx = _ctx(metrics=[_metric("k", 1.0)])

    validate_context(
        ctx,
        settings=_settings(),
        now=_NOW,
        current_snapshot=_snapshot(computed_at=_NOW, spec_version="fe_v2"),
        baseline_snapshot=_snapshot(computed_at=_NOW, spec_version="fe_v1"),
    )

    assert _has(ctx, "spec_mismatch")


def test_스냅샷_인자가_없으면_두_검사를_건너뛴다() -> None:
    ctx = _ctx(metrics=[_metric("k", 1.0)])

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert _reasons(ctx) == []


# ── ③ evidence 충분성 ────────────────────────────────────────────────────────────


def test_소규모_세그먼트는_제외하고_보류를_남긴다() -> None:
    """통상 `compute/behavior.fill_segments` 가 이미 걸러 no-op 이다 — 방어 검사."""
    ctx = _ctx(
        metrics=[_metric("k", 1.0)],
        segments=[
            Segment(rule_label="충성형", size=96),
            Segment(rule_label="탐색형", size=12),
        ],
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert [segment.rule_label for segment in ctx.segments] == ["충성형"]
    assert _has(ctx, "segment_too_small")


def test_정상_세그먼트만_있으면_보류가_없다() -> None:
    ctx = _ctx(segments=[Segment(rule_label="충성형", size=96)])

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert _reasons(ctx) == []


def test_재료가_전부_격리되면_보고서를_막는다() -> None:
    ctx = _ctx(metrics=[_metric("churn_rate", 1.7, unit="비율")])

    result = validate_context(ctx, settings=_settings(), now=_NOW)

    assert result.blocked is True
    assert _has(ctx, "no_material")


def test_보류만_있는_ctx도_보고서를_막는다() -> None:
    """`holds` 만 남은 ctx 로 쓴 보고서는 보고서가 아니라 실패 통지다."""
    ctx = _ctx(holds=[Hold(step="compute", reason="no_series: ...")])

    result = validate_context(ctx, settings=_settings(), now=_NOW)

    assert result.blocked is True


# ── 킬스위치 · 인용 가능 기간 집합 ──────────────────────────────────────────────────


def test_strict가_false면_ctx를_고치지_않는다() -> None:
    """경고만 모드 — 무엇이 걸렸는지는 남기되 파이프라인 동작은 예전 그대로다."""
    ctx = _ctx(
        metrics=[_metric("churn_rate", 1.7, unit="비율")],
        comparisons=[_comparison(baseline_from=date(2026, 8, 1), baseline_to=date(2026, 8, 5))],
    )

    result = validate_context(ctx, settings=_settings(seller_validate_strict=False), now=_NOW)

    assert len(ctx.metrics) == 1
    assert len(ctx.comparisons) == 1
    assert result.blocked is False
    assert _has(ctx, "metric_out_of_range")
    assert _has(ctx, "comparison_overlap")


def test_strict가_false면_기간_역전도_막지_않는다() -> None:
    """예외를 두면 "꺼두면 예전과 같다"는 킬스위치의 보장이 깨진다."""
    ctx = AnalysisContext(
        worker="churn",
        brand_id=7,
        period_from=date(2026, 8, 9),
        period_to=date(2026, 8, 3),
    )

    result = validate_context(ctx, settings=_settings(seller_validate_strict=False), now=_NOW)

    assert result.blocked is False
    assert _has(ctx, "period_reversed")


def test_인용_가능_기간은_격리_이후_기준으로_모인다() -> None:
    """빠진 항목의 날짜가 남아 있으면 V2-d 가 지워진 근거의 날짜를 허용하게 된다."""
    dropped = Metric(
        key="churn_rate",
        value=1.7,
        unit="비율",
        source="I-16",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 2),
    )
    ctx = _ctx(
        metrics=[dropped, _metric("k", 1.0)],
        comparisons=[_comparison()],
        causes=[
            CauseCandidate(
                target_key="churn_rate",
                target_desc="8월 5일 이탈률 상승",
                event_kind="price_change",
                event_at=date(2026, 8, 1),
                event_desc="가격 인상",
                lag_days=4,
                strength="temporal_only",
            )
        ],
    )

    result = validate_context(ctx, settings=_settings(), now=_NOW)

    assert date(2026, 1, 1) not in result.citable_dates
    assert date(2026, 1, 2) not in result.citable_dates
    assert result.citable_dates == frozenset(
        {_FROM, _TO, date(2026, 7, 27), date(2026, 8, 2), date(2026, 8, 1)}
    )


def test_스냅샷_날짜도_인용_집합에_들어간다() -> None:
    ctx = _ctx(metrics=[_metric("k", 1.0)])
    snapshot = _snapshot(computed_at=_NOW)

    result = validate_context(ctx, settings=_settings(), now=_NOW, current_snapshot=snapshot)

    assert _NOW.date() in result.citable_dates


@pytest.mark.parametrize("p_value", [-0.1, 1.5, float("nan")])
def test_확률이_아닌_p값은_강등된다(p_value: float) -> None:
    ctx = _ctx(
        verdicts=[
            Verdict(
                key="churn_rate",
                verdict="significant_drop",
                method="two_proportion_z",
                p_value=p_value,
            )
        ]
    )

    validate_context(ctx, settings=_settings(), now=_NOW)

    assert ctx.verdicts[0].verdict == "undecided"
    assert _has(ctx, "verdict_bad_p_value")
