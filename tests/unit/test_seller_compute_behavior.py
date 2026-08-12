"""sop/compute/behavior.py — 스냅샷 군집 → ctx.segments 전사 (이슈 #594, `05-WORKERS` §2).

여기서 고정하는 것 넷:
- 군집 통계를 **다시 계산하지 않는다**(#593 값 그대로 전사)
- 소규모/기타 군집은 segments 에서 빠지되 인원은 숨기지 않는다
- 스냅샷 Hold 는 승계이지 재발행이 아니다
- `customerLabel`·`member_labels` 가 ctx 로 새지 않는다(재식별 금지)
"""

from __future__ import annotations

import pytest

from app.agents.seller.sop.compute.behavior import compute_behavior
from app.agents.seller.sop.context import AnalysisContext
from app.core.config import Settings
from tests.unit import _seller_snapshot_fixtures as fx


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _ctx() -> AnalysisContext:
    return AnalysisContext(
        worker="behavior", brand_id=7, period_from=fx.PERIOD_FROM, period_to=fx.PERIOD_TO
    )


def _metric(ctx: AnalysisContext, key: str) -> float | None:
    return next(metric.value for metric in ctx.metrics if metric.key == key)


def test_전사는_스냅샷_통계를_그대로_옮긴다(settings: Settings) -> None:
    snapshot = fx.snapshot(
        clusters=[fx.cluster(0, "구매망설임형", 128)],
        feature_rows=fx.cohort({"구매망설임형": [f"L{i:04d}" for i in range(128)]}),
        total_customers=1000,
    )
    ctx = _ctx()
    compute_behavior(ctx, snapshot, settings=settings)

    assert len(ctx.segments) == 1
    segment = ctx.segments[0]
    assert segment.rule_label == "구매망설임형"
    assert segment.size == 128
    assert segment.centroid_stats == snapshot.clusters[0]["centroid_stats"]
    assert segment.ratio_to_mean == snapshot.clusters[0]["ratio_to_mean"]
    assert segment.flag_ratios == snapshot.clusters[0]["flag_ratios"]
    # verdict 를 만들지 않는 유일한 워커다 — 시점 비교는 churn 소관이다.
    assert ctx.verdicts == []


def test_표시_라벨_중복은_display_label로만_구분된다(settings: Settings) -> None:
    snapshot = fx.snapshot(
        clusters=[
            fx.cluster(0, "탐색형", 60, display_label="탐색형(1)"),
            fx.cluster(1, "탐색형", 40, display_label="탐색형(2)"),
        ],
        feature_rows=fx.cohort({"탐색형": [f"L{i:04d}" for i in range(100)]}),
    )
    ctx = _ctx()
    compute_behavior(ctx, snapshot, settings=settings)

    assert [s.rule_label for s in ctx.segments] == ["탐색형", "탐색형"]
    assert [s.display_label for s in ctx.segments] == ["탐색형(1)", "탐색형(2)"]


def test_소규모와_기타는_제외하되_인원은_지표로_남는다(settings: Settings) -> None:
    snapshot = fx.snapshot(
        clusters=[
            fx.cluster(0, "충성형", 96),
            fx.cluster(1, "기타", 40),  # 라벨이 기타면 크기가 임계 이상이어도 제외
            fx.cluster(2, "휴면형", 18),  # 30명 미만
        ],
        feature_rows=fx.cohort({"충성형": [f"L{i:04d}" for i in range(96)]}),
        total_customers=1000,
    )
    ctx = _ctx()
    compute_behavior(ctx, snapshot, settings=settings)

    assert [s.rule_label for s in ctx.segments] == ["충성형"]
    assert _metric(ctx, "segment_classified_customers") == 96.0
    assert _metric(ctx, "segment_excluded_customers") == 58.0
    assert _metric(ctx, "cohort_total_customers") == 1000.0


def test_스냅샷_hold는_승계하고_재발행하지_않는다(settings: Settings) -> None:
    snapshot = fx.snapshot(
        clusters=[fx.cluster(0, "충성형", 96), fx.cluster(1, "기타", 12)],
        feature_rows=fx.cohort({"충성형": [f"L{i:04d}" for i in range(96)]}),
        holds=[{"step": "compute", "reason": "small_cluster: 12명 규모 1개 군집 분류 보류"}],
    )
    ctx = _ctx()
    compute_behavior(ctx, snapshot, settings=settings)

    small = [hold for hold in ctx.holds if hold.reason.startswith("small_cluster")]
    assert len(small) == 1  # 제외 판정이 같은 사유를 두 번 만들지 않는다


def test_금액은_분포로_넘어가고_합이_1이다(settings: Settings) -> None:
    rows = [
        fx.feature_row("A", cluster_id=0, rule_label="충성형", amount_bucket="GTE_300K"),
        fx.feature_row("B", cluster_id=0, rule_label="충성형", amount_bucket="GTE_300K"),
        fx.feature_row("C", cluster_id=0, rule_label="충성형", amount_bucket="100K_300K"),
        fx.feature_row("D", cluster_id=0, rule_label="충성형", amount_bucket="WEIRD"),
    ]
    snapshot = fx.snapshot(clusters=[fx.cluster(0, "충성형", 4)], feature_rows=rows)
    ctx = _ctx()
    loose = Settings(_env_file=None, seller_customer_segment_min_size=1)
    compute_behavior(ctx, snapshot, settings=loose)

    distribution = ctx.segments[0].amount_distribution
    assert distribution["GTE_300K"] == pytest.approx(0.5)
    assert distribution["100K_300K"] == pytest.approx(0.25)
    # 매핑 밖 구간을 ZERO 로 뭉개면 "안 산 사람"과 구분이 사라진다.
    assert distribution["UNKNOWN"] == pytest.approx(0.25)
    assert sum(distribution.values()) == pytest.approx(1.0)


def test_개인_식별자는_ctx로_새지_않는다(settings: Settings) -> None:
    snapshot = fx.snapshot(
        clusters=[fx.cluster(0, "충성형", 40, member_labels=["SECRET-1", "SECRET-2"])],
        feature_rows=fx.cohort({"충성형": ["SECRET-1", "SECRET-2"] + [f"L{i}" for i in range(38)]}),
    )
    ctx = _ctx()
    compute_behavior(ctx, snapshot, settings=settings)

    dumped = ctx.model_dump_json()
    assert "SECRET-1" not in dumped
    assert "member_labels" not in dumped


def test_군집_생략_스냅샷은_빈_세그먼트를_남긴다(settings: Settings) -> None:
    snapshot = fx.snapshot(
        clusters=[],
        feature_rows=fx.cohort({"": [f"L{i}" for i in range(10)]}),
        holds=[{"step": "compute", "reason": "degenerate_features: 전 고객 피처가 동일하다"}],
    )
    ctx = _ctx()
    compute_behavior(ctx, snapshot, settings=settings)

    assert ctx.segments == []
    assert any(hold.reason.startswith("degenerate_features") for hold in ctx.holds)
