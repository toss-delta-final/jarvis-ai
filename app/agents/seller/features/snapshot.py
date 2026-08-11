"""I-38 응답 → `SnapshotRecord` 조립 (이슈 #593, `OPS-RUNTIME` §1.4).

**I/O 가 없다** — Spring 조회도, `analysis_store.save_snapshot` 호출도 하지 않는다.
무인 배치 진입점(트리거·스케줄러)이 후속 이슈라, 그 경계를 넘지 않으려고 순수 조립
함수로 끊었다. 호출부는 이 레코드를 그대로 `save_snapshot` 에 넘기면 된다
(UPSERT·쓰기 타임아웃·크기 로깅은 저장 계층 #585 소관).

`id` 를 여기서 `uuid4()` 로 채우는 것은 저장 계층 규약이다 — DB 쪽에서 생성하면 쓰기
재시도 시 두 번째 행이 생긴다(`analysis_records` docstring).
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.agents.seller.analysis_records import SnapshotRecord
from app.agents.seller.features import spec
from app.agents.seller.features.clustering import cluster_customers
from app.agents.seller.features.customer import build_customer_features
from app.core.config import Settings
from app.schemas.spring import SellerCustomerFeaturesResult


def build_snapshot_record(
    result: SellerCustomerFeaturesResult,
    *,
    brand_id: int,
    period_from: date,
    period_to: date,
    settings: Settings,
) -> SnapshotRecord:
    """I-38 응답 1건 → 스냅샷 1행. LLM 0회·I/O 0회.

    `insufficientCohort=true`(코호트 30명 미만, `rows=[]`)여도 **행은 만든다** — 컬럼이
    이미 있고, 이렇게 해야 "배치가 안 돌았다"와 "표본이 부족했다"를 나중에 구분할 수
    있다. 보고서를 만들지 말지는 그 값을 읽는 쪽(무인 실행 F-4)이 정한다.

    UPSERT 키는 `(brand_id, period_to, feature_spec_version)` 다 — 같은 날 다시 조립해
    저장하면 새 행이 아니라 갱신이 된다(`analysis_store.save_snapshot`).
    """
    feature_set = build_customer_features(
        result, period_from=period_from, period_to=period_to, settings=settings
    )
    clustering = cluster_customers(feature_set, settings=settings)

    for row, cluster_id in zip(feature_set.rows, clustering.labels, strict=True):
        row["cluster_id"] = cluster_id
    # 라벨은 군집 단위 판정이라 행에는 역참조로 채운다 — 보고서가 행을 직접 인용하지는
    # 않지만, 시점 간 이동(churn 워커)이 행 단위 조인을 쓴다.
    label_by_cluster = {
        cluster["cluster_id"]: cluster["rule_label"] for cluster in clustering.clusters
    }
    for row in feature_set.rows:
        row["rule_label"] = label_by_cluster.get(row["cluster_id"], "")

    # 재현성 각인은 한곳에 모은다 — 전용 컬럼이 없는 메타(`rfm_bins_actual`·shrinkage
    # 사전확률)를 `scaler_params` 에 함께 실어 DDL 변경 없이 스냅샷을 자기설명적으로 만든다.
    scaler_params = dict(clustering.scaler_params)
    scaler_params["shrinkage_prior"] = feature_set.prior_stats
    scaler_params["shrinkage_alpha"] = settings.seller_feature_shrinkage_alpha
    scaler_params["rfm_bins_actual"] = feature_set.rfm_bins_actual
    scaler_params["period_days"] = feature_set.period_days

    holds = [*feature_set.holds, *clustering.holds]

    # rowLimit 은 BE 상수(CUSTOMER_ROW_LIMIT)가 정본이라 응답 에코를 그대로 저장한다.
    # 우리 Settings 는 기대치일 뿐이므로, 어긋나면 저장값이 아니라 Hold 로 드러낸다.
    if result.row_limit != settings.seller_snapshot_row_limit:
        holds.append(
            {
                "step": "load",
                "reason": (
                    "row_limit_drift: I-38 rowLimit 이 설정과 다르다"
                    f" (응답={result.row_limit}, 설정={settings.seller_snapshot_row_limit})"
                ),
            }
        )

    return SnapshotRecord(
        id=uuid4(),
        brand_id=brand_id,
        period_from=period_from,
        period_to=period_to,
        source=spec.SNAPSHOT_SOURCE,
        feature_spec_version=settings.seller_feature_spec_version,
        total_customers=result.total_customers,
        row_limit=result.row_limit,
        truncated=result.truncated,
        insufficient_cohort=result.insufficient_cohort,
        scaler_params=scaler_params,
        pca_used=clustering.pca_used,
        pca_params=clustering.pca_params,
        silhouette=clustering.silhouette,
        random_state=settings.seller_customer_kmeans_random_state,
        clusters=clustering.clusters,
        feature_rows=feature_set.rows,
        holds=[hold if isinstance(hold, dict) else hold.model_dump() for hold in holds],
    )
