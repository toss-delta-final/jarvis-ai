"""compute 스텝 테스트용 스냅샷 fixture 빌더 (이슈 #594).

`_` 접두어라 pytest 가 테스트로 수집하지 않는다(`_seller_feature_fixtures` 관행).

#593 `features/snapshot.build_snapshot_record` 를 통째로 돌리면 K-Means 가 끼어들어
"군집이 이렇게 나왔을 때 compute 가 무엇을 하는가"를 고정할 수 없다. compute 는 스냅샷을
**읽기만** 하므로 여기서는 그 산출물 모양(`clusters`·`feature_rows`)을 직접 만든다.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.agents.seller.analysis_records import SnapshotRecord

PERIOD_FROM = date(2026, 8, 3)
PERIOD_TO = date(2026, 9, 1)
BASELINE_FROM = date(2026, 7, 27)
BASELINE_TO = date(2026, 8, 25)

_DEFAULT_CENTROID = {
    "sessions": 11.2,
    "productViews": 42.1,
    "cartAdds": 8.4,
    "checkoutStarts": 2.1,
    "orderCount": 0.3,
    "cancelCount": 0.1,
    "lastActivityDaysAgo": 6.0,
    "firstSeenDaysAgo": 180.0,
    "amountOrdinal": 1.4,
}
_DEFAULT_RATIO = {
    "sessions": 1.6,
    "productViews": 2.3,
    "cartAdds": 2.3,
    "checkoutStarts": 1.1,
    "orderCount": 0.2,
    "lastActivityDaysAgo": 0.3,
}
_DEFAULT_FLAGS = {
    "is_cart_abandoner": 0.78,
    "is_checkout_dropper": 0.41,
    "is_viewer_only": 0.02,
    "is_new": 0.08,
    "is_returning": 0.92,
    "has_cancelled": 0.05,
}


def cluster(
    cluster_id: int,
    rule_label: str,
    size: int,
    *,
    display_label: str | None = None,
    centroid_stats: dict | None = None,
    ratio_to_mean: dict | None = None,
    flag_ratios: dict | None = None,
    member_labels: list[str] | None = None,
) -> dict:
    """`clusters[]` 1건 — #593 `cluster_customers` 산출 모양 그대로."""
    return {
        "cluster_id": cluster_id,
        "rule_label": rule_label,
        "display_label": display_label if display_label is not None else rule_label,
        "llm_label": "",
        "llm_desc": "",
        "size": size,
        "centroid_stats": dict(centroid_stats or _DEFAULT_CENTROID),
        "ratio_to_mean": dict(ratio_to_mean or _DEFAULT_RATIO),
        "flag_ratios": dict(flag_ratios or _DEFAULT_FLAGS),
        "member_labels": list(member_labels or []),
    }


def feature_row(
    customer_label: str,
    *,
    cluster_id: int,
    rule_label: str,
    amount_bucket: str = "ZERO",
    is_new: bool = False,
) -> dict:
    """`feature_rows[]` 1건 — compute 가 실제로 읽는 필드만 채운다."""
    return {
        "customerLabel": customer_label,
        "raw": {
            "sessions": 3,
            "productViews": 9,
            "cartAdds": 2,
            "checkoutStarts": 1,
            "orderCount": 0,
            "cancelCount": 0,
            "amountBucket": amount_bucket,
            "lastActivityDaysAgo": 5,
            "firstSeenDaysAgo": 90,
        },
        "flags": {
            "is_cart_abandoner": True,
            "is_checkout_dropper": False,
            "is_viewer_only": False,
            "is_new": is_new,
            "is_returning": True,
            "has_cancelled": False,
        },
        "cluster_id": cluster_id,
        "rule_label": rule_label,
    }


def snapshot(
    *,
    clusters: list[dict] | None = None,
    feature_rows: list[dict] | None = None,
    holds: list[dict] | None = None,
    brand_id: int = 7,
    period_from: date = PERIOD_FROM,
    period_to: date = PERIOD_TO,
    feature_spec_version: str = "fe_v1",
    total_customers: int | None = None,
    truncated: bool = False,
    insufficient_cohort: bool = False,
) -> SnapshotRecord:
    rows = list(feature_rows or [])
    return SnapshotRecord(
        id=uuid4(),
        brand_id=brand_id,
        period_from=period_from,
        period_to=period_to,
        source="i38_v1",
        feature_spec_version=feature_spec_version,
        total_customers=total_customers if total_customers is not None else len(rows),
        row_limit=1000,
        truncated=truncated,
        insufficient_cohort=insufficient_cohort,
        scaler_params={},
        pca_used=False,
        random_state=42,
        clusters=list(clusters or []),
        feature_rows=rows,
        holds=list(holds or []),
    )


def cohort(
    assignments: dict[str, list[str]],
    *,
    cluster_ids: dict[str, int] | None = None,
    new_labels: set[str] | None = None,
) -> list[dict]:
    """`{원형 라벨: [customerLabel, ...]}` → `feature_rows`.

    이동 시나리오를 "누가 어느 라벨에 있었나"로 바로 적을 수 있게 하는 축약이다.
    """
    ids = cluster_ids or {}
    fresh = new_labels or set()
    rows: list[dict] = []
    for index, (rule_label, members) in enumerate(assignments.items()):
        cluster_id = ids.get(rule_label, index)
        for member in members:
            rows.append(
                feature_row(
                    member,
                    cluster_id=cluster_id,
                    rule_label=rule_label,
                    is_new=member in fresh,
                )
            )
    return rows
