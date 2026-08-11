"""sop/compute/churn.py — 세그먼트 이동 (이슈 #594, `05-WORKERS` §3).

이슈 완료 조건을 여기서 고정한다: fixture 스냅샷 2개로 **이동 행렬 · 순증감 · 명단 3분할**,
그리고 **표시 라벨로 조인하면 깨지는 회귀 테스트**.
"""

from __future__ import annotations

import pytest

from app.agents.seller.sop.compute.churn import _label_by_customer, compute_churn
from app.agents.seller.sop.context import AnalysisContext
from app.core.config import Settings
from app.schemas.spring import ChurnResult
from tests.unit import _seller_snapshot_fixtures as fx

_LOYAL = "충성형"
_HESITANT = "구매망설임형"
_EXPLORER = "탐색형"


@pytest.fixture
def settings() -> Settings:
    # 군집 크기 임계를 1 로 낮춘다 — 이동 규칙을 보는 테스트라 30명 fixture 는 잡음이다.
    return Settings(_env_file=None, seller_customer_segment_min_size=1)


def _ctx() -> AnalysisContext:
    return AnalysisContext(
        worker="churn", brand_id=7, period_from=fx.PERIOD_FROM, period_to=fx.PERIOD_TO
    )


def _metric(ctx: AnalysisContext, key: str) -> float | None:
    return next((metric.value for metric in ctx.metrics if metric.key == key), None)


def _verdict(ctx: AnalysisContext, key: str):
    return next((verdict for verdict in ctx.verdicts if verdict.key == key), None)


def _comparison(ctx: AnalysisContext, key: str):
    return next((item for item in ctx.comparisons if item.key == key), None)


def _moved_pair() -> tuple:
    """충성형 10명 중 4명이 구매망설임형으로 이동 · h10 이탈 · 신규 1 · 복귀 1."""
    loyal = [f"c{i}" for i in range(1, 11)]
    hesitant = [f"h{i}" for i in range(1, 11)]
    baseline = fx.snapshot(
        period_from=fx.BASELINE_FROM,
        period_to=fx.BASELINE_TO,
        clusters=[fx.cluster(0, _LOYAL, 10), fx.cluster(1, _HESITANT, 10)],
        feature_rows=fx.cohort({_LOYAL: loyal, _HESITANT: hesitant}),
    )
    current = fx.snapshot(
        clusters=[fx.cluster(0, _LOYAL, 6), fx.cluster(1, _HESITANT, 15)],
        feature_rows=fx.cohort(
            {
                _LOYAL: loyal[:6],
                _HESITANT: hesitant[:9] + loyal[6:] + ["n1", "n2"],
            },
            new_labels={"n1"},
        ),
    )
    return current, baseline


def test_이동_행렬과_순증감은_교집합_모수로_계산된다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    assert _metric(ctx, "segment_shift_cohort") == 19.0  # c1~c10 + h1~h9
    loyal = _comparison(ctx, f"segment_size:{_LOYAL}")
    hesitant = _comparison(ctx, f"segment_size:{_HESITANT}")
    assert (loyal.baseline, loyal.current) == (10.0, 6.0)
    assert (hesitant.baseline, hesitant.current) == (9.0, 13.0)
    assert _metric(ctx, f"segment_move:{_LOYAL}>{_HESITANT}") == 4.0
    # 제자리(A→A)는 이동이 아니다.
    assert _metric(ctx, f"segment_move:{_LOYAL}>{_LOYAL}") is None


def test_명단_3분할_사라진_고객은_이동으로_보고하지_않는다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    assert _metric(ctx, "membership_new") == 1.0
    assert _metric(ctx, "membership_returned") == 1.0
    assert _metric(ctx, "membership_dropped_out") == 1.0
    assert any(hold.reason.startswith("membership_pending") for hold in ctx.holds)


def test_절단_스냅샷은_보류_사유에_절단_가능성을_밝힌다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    baseline.truncated = True
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    pending = next(hold for hold in ctx.holds if hold.reason.startswith("membership_pending"))
    assert "절단 가능성" in pending.reason


def test_delta_size가_세그먼트에_배정된다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    deltas = {segment.rule_label: segment.delta_size for segment in ctx.segments}
    assert deltas == {_LOYAL: -4, _HESITANT: 4}


def test_이동_조인은_원형_라벨_기준이다(settings: Settings) -> None:
    """🔴 회귀 — 표시 라벨(`탐색형(1)`)로 조인하면 실패한다.

    같은 고객이 같은 원형 라벨에 그대로 있는데 군집 번호만 시점 간에 뒤집힌 상황이다.
    표시 라벨을 조인 키로 쓰면 10명 전원이 `탐색형(1) ↔ 탐색형(2)` 로 이동한 것처럼
    보인다 — 군집 id 는 K-Means 실행마다 안정적이지 않다(`04` §4.3).
    """
    front = [f"e{i}" for i in range(1, 7)]
    back = [f"e{i}" for i in range(7, 11)]
    baseline = fx.snapshot(
        period_from=fx.BASELINE_FROM,
        period_to=fx.BASELINE_TO,
        clusters=[
            fx.cluster(0, _EXPLORER, 6, display_label="탐색형(1)"),
            fx.cluster(1, _EXPLORER, 4, display_label="탐색형(2)"),
        ],
        feature_rows=fx.cohort({_EXPLORER: front + back}),
    )
    current = fx.snapshot(
        clusters=[
            fx.cluster(0, _EXPLORER, 4, display_label="탐색형(1)"),
            fx.cluster(1, _EXPLORER, 6, display_label="탐색형(2)"),
        ],
        feature_rows=fx.cohort({_EXPLORER: back + front}),
    )
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    assert all(not label.startswith("탐색형(") for label in _label_by_customer(current).values())
    assert [metric.key for metric in ctx.metrics if metric.key.startswith("segment_move:")] == []
    verdict = _verdict(ctx, f"segment_size:{_EXPLORER}")
    assert verdict.verdict == "no_significant_change"
    assert verdict.detail["delta"] == 0.0


def test_원형_라벨_군집이_둘이면_delta_size를_배정하지_않는다(settings: Settings) -> None:
    front = [f"e{i}" for i in range(1, 7)]
    back = [f"e{i}" for i in range(7, 11)]
    baseline = fx.snapshot(
        period_from=fx.BASELINE_FROM,
        period_to=fx.BASELINE_TO,
        clusters=[fx.cluster(0, _EXPLORER, 10)],
        feature_rows=fx.cohort({_EXPLORER: front + back}),
    )
    current = fx.snapshot(
        clusters=[
            fx.cluster(0, _EXPLORER, 6, display_label="탐색형(1)"),
            fx.cluster(1, _EXPLORER, 4, display_label="탐색형(2)"),
        ],
        feature_rows=fx.cohort({_EXPLORER: front + back}),
    )
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    assert [segment.delta_size for segment in ctx.segments] == [None, None]
    assert any(hold.reason.startswith("delta_size_ambiguous") for hold in ctx.holds)


def test_기타_라벨은_이동_행렬에서_빠지고_규모만_남는다(settings: Settings) -> None:
    baseline = fx.snapshot(
        period_from=fx.BASELINE_FROM,
        period_to=fx.BASELINE_TO,
        clusters=[fx.cluster(0, _LOYAL, 3), fx.cluster(1, "기타", 2)],
        feature_rows=fx.cohort({_LOYAL: ["c1", "c2", "c3"], "기타": ["x1", "x2"]}),
    )
    current = fx.snapshot(
        clusters=[fx.cluster(0, _LOYAL, 3), fx.cluster(1, "기타", 2)],
        feature_rows=fx.cohort({_LOYAL: ["c1", "c2", "c3"], "기타": ["x1", "x2"]}),
    )
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    assert _metric(ctx, "shift_unclassified") == 2.0
    assert _metric(ctx, "segment_shift_cohort") == 3.0
    assert _comparison(ctx, "segment_size:기타") is None


def test_기준_스냅샷이_없으면_현재_분포만_남기고_보류한다(settings: Settings) -> None:
    current, _ = _moved_pair()
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=None, settings=settings)

    assert len(ctx.segments) == 2  # 기준이 없다고 오늘의 세그먼트까지 사라지지 않는다
    assert ctx.comparisons == []
    assert any(hold.reason.startswith("no_baseline") for hold in ctx.holds)


def test_스펙_버전이_다르면_비교를_전면_보류한다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    baseline.feature_spec_version = "fe_v0"
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    assert ctx.comparisons == []
    assert ctx.verdicts == []
    assert _metric(ctx, "segment_shift_cohort") is None
    assert any(hold.reason.startswith("spec_mismatch") for hold in ctx.holds)


def test_이동_하한_미만은_표시하지_않는다() -> None:
    settings = Settings(
        _env_file=None, seller_customer_segment_min_size=1, seller_move_report_min_pct=0.5
    )
    current, baseline = _moved_pair()
    ctx = _ctx()
    compute_churn(ctx, current=current, baseline=baseline, settings=settings)

    # 4명은 교집합 19명의 50% 미만이라 노이즈로 뺀다.
    assert [metric.key for metric in ctx.metrics if metric.key.startswith("segment_move:")] == []


def test_i16_이탈률_비교가_verdict로_들어간다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    ctx = _ctx()
    compute_churn(
        ctx,
        current=current,
        baseline=baseline,
        churn_now=ChurnResult(churnRate=0.40, cohortSize=200),
        churn_prev=ChurnResult(churnRate=0.20, cohortSize=200),
        settings=settings,
    )
    verdict = _verdict(ctx, "churn_rate")
    assert verdict.verdict == "significant_rise"
    assert _comparison(ctx, "churn_rate") is not None


def test_i16_이탈률이_정의역_밖이면_판정_보류다(settings: Settings) -> None:
    current, baseline = _moved_pair()
    ctx = _ctx()
    compute_churn(
        ctx,
        current=current,
        baseline=baseline,
        churn_now=ChurnResult(churnRate=1.4, cohortSize=200),
        churn_prev=ChurnResult(churnRate=0.2, cohortSize=200),
        settings=settings,
    )
    assert _verdict(ctx, "churn_rate").verdict == "undecided"
    assert any(hold.reason.startswith("churn_rate_unusable") for hold in ctx.holds)
