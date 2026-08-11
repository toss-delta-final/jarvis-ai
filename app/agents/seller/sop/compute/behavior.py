"""`behavior` compute — 스냅샷 군집을 `ctx.segments` 로 옮긴다 (이슈 #594, `05-WORKERS` §2).

⚠️ **여기서 군집 통계를 다시 계산하지 않는다.** `size`·`centroid_stats`·`ratio_to_mean`·
`flag_ratios`·`display_label` 은 #593 `features/clustering.cluster_customers` 가 스냅샷을
만들 때 이미 채워 넣었다(실측). 이 스텝이 하는 일은 **전사(轉寫)와 제외 판정**이고, 새로
계산하는 것은 금액 구간 분포 하나뿐이다.

소규모 군집(`rule_label="기타"`)도 여기서 다시 만들지 않는다 — #593 이 라벨 치환과
`Hold("small_cluster")` 발행까지 끝냈다. 두 번 발행하면 판매자가 같은 보류를 두 번 본다.

[재식별 금지] `clusters[].member_labels` 와 `feature_rows[].customerLabel` 은 **ctx 에
올리지 않는다**. 금액 분포를 만들 때 모듈 내부에서 조인에만 쓰고 버린다 — LLM 이 보는
유일한 입력에 개인 단위 식별자가 실리면 `01` §9 불변 규약이 깨진다.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from app.agents.seller.analysis_records import SnapshotRecord
from app.agents.seller.features import spec
from app.agents.seller.sop.context import AnalysisContext, Hold, Metric, Segment
from app.core.config import Settings

# 매핑에 없는 금액 구간이 온 고객의 몫 — 0 원(ZERO)으로 뭉개면 "안 산 사람"과 구분이
# 사라진다. `features/customer._check_amount_buckets` 가 이미 Hold 로 알린 상태다.
UNKNOWN_BUCKET = "UNKNOWN"

_BUCKET_ORDER: tuple[str, ...] = (*spec.AMOUNT_BUCKET_ORDER, UNKNOWN_BUCKET)


def inherit_snapshot_holds(ctx: AnalysisContext, snapshot: SnapshotRecord) -> None:
    """스냅샷이 이미 기록한 보류를 ctx 로 승계한다 — **재판정하지 않는다**.

    `small_cluster`·`insufficient_cohort`·`truncated`·`amount_bucket_drift`·
    `row_limit_drift` 는 전부 #593 이 만든 것이다. 여기서 같은 조건을 다시 평가하면
    같은 사유가 두 줄로 보고서에 실린다.
    """
    holds = snapshot.holds if isinstance(snapshot.holds, list) else []
    for hold in holds:
        if isinstance(hold, Hold):
            ctx.holds.append(hold)
            continue
        if not isinstance(hold, dict):
            continue
        step = str(hold.get("step") or "load")
        reason = str(hold.get("reason") or "")
        if reason:
            ctx.holds.append(Hold(step=step, reason=reason))


def _amount_distributions(snapshot: SnapshotRecord) -> dict[int, dict[str, float]]:
    """군집별 금액 구간 분포 — `feature_rows` 를 `cluster_id` 로 묶어 센다.

    비율이 0 인 구간은 키 자체를 빼서 표가 길어지지 않게 한다(값 0 을 나열해도 정보가
    늘지 않는다). 반환 dict 의 키 순서는 `AMOUNT_BUCKET_ORDER` + UNKNOWN 고정이다.
    """
    rows = snapshot.feature_rows if isinstance(snapshot.feature_rows, list) else []
    counts: dict[int, Counter[str]] = defaultdict(Counter)
    for row in rows:
        if not isinstance(row, dict):
            continue
        cluster_id = row.get("cluster_id")
        if cluster_id is None:
            continue
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        bucket = raw.get("amountBucket")
        key = bucket if bucket in spec.AMOUNT_BUCKET_ORDER else UNKNOWN_BUCKET
        counts[int(cluster_id)][key] += 1

    distributions: dict[int, dict[str, float]] = {}
    for cluster_id, counter in counts.items():
        total = sum(counter.values())
        if not total:
            continue
        distributions[cluster_id] = {
            bucket: counter[bucket] / total for bucket in _BUCKET_ORDER if counter[bucket]
        }
    return distributions


def _floats(values: object) -> dict[str, float]:
    """JSONB 에서 돌아온 dict 를 float 사전으로 좁힌다(문자열 수치·None 방어)."""
    if not isinstance(values, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in values.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def fill_segments(
    ctx: AnalysisContext, snapshot: SnapshotRecord, *, settings: Settings
) -> tuple[int, int]:
    """스냅샷 군집 → `ctx.segments`. `(분류 인원, 제외 인원)` 을 돌려준다.

    제외 대상은 `rule_label == "기타"` 이거나 `size < seller_customer_segment_min_size`
    인 군집이다. 제외분을 ctx 에서 아예 빼는 이유(`05` §2.2 · #596 "세그먼트 30명 미만
    → 제외"): 소표본 평균은 불안정하고, 4명짜리 집단은 재식별될 수 있다. 대신 **인원을
    숨기지는 않는다** — 규모는 `segment_excluded_customers` 지표로 남고 사유는 승계된
    `Hold("small_cluster")` 에 있다.

    churn 워커도 "현재 분포"를 같은 방식으로 채우므로 이 함수를 공유한다.
    """
    clusters = snapshot.clusters if isinstance(snapshot.clusters, list) else []
    distributions = _amount_distributions(snapshot)
    min_size = settings.seller_customer_segment_min_size

    classified = 0
    excluded = 0
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        try:
            size = int(cluster.get("size", 0))
        except (TypeError, ValueError):
            continue
        rule_label = str(cluster.get("rule_label") or "")
        if not rule_label or rule_label == spec.LABEL_SMALL or size < min_size:
            excluded += size
            continue
        classified += size
        try:
            cluster_id = int(cluster.get("cluster_id", -1))
        except (TypeError, ValueError):
            cluster_id = -1
        ctx.segments.append(
            Segment(
                rule_label=rule_label,
                display_label=str(cluster.get("display_label") or rule_label),
                size=size,
                centroid_stats=_floats(cluster.get("centroid_stats")),
                ratio_to_mean=_floats(cluster.get("ratio_to_mean")),
                flag_ratios=_floats(cluster.get("flag_ratios")),
                amount_distribution=distributions.get(cluster_id, {}),
            )
        )
    return classified, excluded


def fill_scale_metrics(
    ctx: AnalysisContext, snapshot: SnapshotRecord, classified: int, excluded: int
) -> None:
    """표 머리글이 인용할 규모 3건 — "1,000명 중 942명 분류"의 재료다(`05` §2.2)."""
    for key, value, source in (
        ("cohort_total_customers", float(snapshot.total_customers), "snapshot"),
        ("segment_classified_customers", float(classified), "calc"),
        ("segment_excluded_customers", float(excluded), "calc"),
    ):
        ctx.metrics.append(
            Metric(
                key=key,
                value=value,
                unit="명",
                source=source,
                period_from=snapshot.period_from,
                period_to=snapshot.period_to,
            )
        )


def compute_behavior(ctx: AnalysisContext, snapshot: SnapshotRecord, *, settings: Settings) -> None:
    """고객 군집 해석의 계산 파트 — LLM 0회 (`05` §2.1).

    `verdicts` 를 만들지 않는 유일한 워커다. 군집 분포 자체는 검정 대상이 아니고(시점
    비교는 churn 소관), 그래서 `gate.should_interpret` 이 verdicts 가 빈 경우를 따로
    다룬다 — 그 함수의 마지막 분기가 이 워커를 위한 것이다.
    """
    inherit_snapshot_holds(ctx, snapshot)
    classified, excluded = fill_segments(ctx, snapshot, settings=settings)
    fill_scale_metrics(ctx, snapshot, classified, excluded)
