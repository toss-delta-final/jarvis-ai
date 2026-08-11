"""features/snapshot.py — `SnapshotRecord` 조립 테스트 (이슈 #593, `OPS-RUNTIME` §1.4).

조립 함수는 **I/O 가 없다** — Spring 조회도 `save_snapshot` 호출도 하지 않는다. 저장
계층(#585)이 이 레코드를 그대로 받으므로, 여기서는 컬럼 매핑·각인·UPSERT 키를 본다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.seller.features import spec
from app.agents.seller.features.snapshot import build_snapshot_record
from app.core.config import Settings
from tests.unit._seller_feature_fixtures import four_type_rows, wire_result, wire_row

_FROM = date(2026, 7, 15)
_TO = date(2026, 8, 11)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _record(rows: list[dict], settings: Settings, **result_overrides):
    return build_snapshot_record(
        wire_result(rows, **result_overrides),
        brand_id=77,
        period_from=_FROM,
        period_to=_TO,
        settings=settings,
    )


def test_record_carries_spec_version_and_reproducibility_stamps(settings: Settings) -> None:
    """`feature_spec_version` 이 다른 스냅샷끼리는 비교를 보류한다 — 그 각인이 여기 있다."""
    record = _record(four_type_rows(40), settings)

    assert record.brand_id == 77
    assert record.source == spec.SNAPSHOT_SOURCE == "i38_v1"
    assert record.feature_spec_version == "fe_v1"
    assert record.random_state == settings.seller_customer_kmeans_random_state
    assert record.silhouette is not None
    assert record.scaler_params["keys"] == list(spec.CLUSTER_INPUT_KEYS)


def test_snapshot_meta_rides_in_scaler_params(settings: Settings) -> None:
    """전용 컬럼이 없는 메타(RFM 구간 수·평활 사전확률)를 여기 함께 각인한다(확정 결정 4)."""
    record = _record(four_type_rows(40), settings)
    params = record.scaler_params

    assert set(params["rfm_bins_actual"]) == {"r", "f", "m"}
    assert set(params["shrinkage_prior"]) == {"cart_rate", "checkout_rate", "order_rate"}
    assert params["shrinkage_alpha"] == settings.seller_feature_shrinkage_alpha
    assert params["period_days"] == (_TO - _FROM).days + 1


def test_response_echo_fields_are_stored_verbatim(settings: Settings) -> None:
    """`totalCustomers`·`rowLimit`·`truncated` 는 BE 가 정본이라 그대로 저장한다."""
    record = _record(four_type_rows(40), settings, totalCustomers=4321, truncated=True)
    assert record.total_customers == 4321
    assert record.row_limit == 1000
    assert record.truncated is True
    assert any("truncated" in hold["reason"] for hold in record.holds)


def test_row_limit_drift_is_held_not_overwritten(settings: Settings) -> None:
    """설정과 응답이 어긋나도 저장값은 응답을 따른다 — 설정은 기대치일 뿐이다."""
    record = _record(four_type_rows(40), settings, rowLimit=500)
    assert record.row_limit == 500
    assert any("row_limit_drift" in hold["reason"] for hold in record.holds)


def test_cluster_id_and_rule_label_are_written_back_to_rows(settings: Settings) -> None:
    """행이 자기 군집을 가리켜야 시점 간 이동(churn)이 행 단위로 조인된다."""
    record = _record(four_type_rows(40), settings)
    label_by_cluster = {c["cluster_id"]: c["rule_label"] for c in record.clusters}

    assert all(row["cluster_id"] in label_by_cluster for row in record.feature_rows)
    assert all(
        row["rule_label"] == label_by_cluster[row["cluster_id"]] for row in record.feature_rows
    )
    assert all(row["is_outlier"] is False for row in record.feature_rows)  # DBSCAN 후속


def test_insufficient_cohort_still_produces_a_row(settings: Settings) -> None:
    """확정 결정 7 — "배치가 안 돌았다"와 "표본이 부족했다"를 구분할 수 있어야 한다."""
    record = _record([], settings, insufficientCohort=True, totalCustomers=12)

    assert record.insufficient_cohort is True
    assert record.total_customers == 12
    assert record.clusters == []
    assert record.feature_rows == []
    assert record.pca_used is False
    assert record.silhouette is None
    assert any("insufficient_cohort" in hold["reason"] for hold in record.holds)


def test_holds_are_serialised_for_jsonb(settings: Settings) -> None:
    """`holds` 는 JSONB 컬럼이라 dict 로 내려야 한다(psycopg Jsonb 가 그대로 감싼다)."""
    record = _record([wire_row(1, amountBucket="GTE_1M")], settings)
    assert all(isinstance(hold, dict) and {"step", "reason"} == set(hold) for hold in record.holds)


def test_upsert_key_is_stable_across_reassembly(settings: Settings) -> None:
    """완료 조건 — 같은 날 두 번 조립하면 UPSERT 키가 같아 행이 1개로 유지된다.

    실제 UPSERT 는 `analysis_store.save_snapshot` 의 `ON CONFLICT
    (brand_id, period_to, feature_spec_version) DO UPDATE` 가 수행한다(#585). 여기서는
    조립부가 그 키를 흔들지 않는지만 본다 — `id`(uuid4)는 매번 달라도 무방하다.
    """
    rows = four_type_rows(40)
    first = _record(rows, settings)
    second = _record(rows, settings)

    def upsert_key(record) -> tuple:
        return (record.brand_id, record.period_to, record.feature_spec_version)

    assert upsert_key(first) == upsert_key(second)
    assert first.id != second.id
    # 내용도 동일해야 갱신이 무의미한 변경을 만들지 않는다(결정론).
    assert first.clusters == second.clusters
    assert first.feature_rows == second.feature_rows
