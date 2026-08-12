"""features/clustering.py — 고객 축 군집 파이프라인 테스트 (이슈 #593, `04-CLUSTERING`).

이슈 완료 조건 2건을 여기서 고정한다:
- 같은 fixture + 같은 시드 → 같은 군집(재현성)
- 명확히 분리된 4유형 fixture 에서 **k=4 선택**

그 밖에 소규모 군집 탈락 규칙 · 라벨 중복 번호(조인 키는 원형) · `ratio_to_mean` 0 나눗셈
회피 · 판정 순서를 본다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.seller.features import spec
from app.agents.seller.features.clustering import _rule_label, cluster_customers
from app.agents.seller.features.customer import build_customer_features
from app.core.config import Settings
from tests.unit._seller_feature_fixtures import four_type_rows, wire_result, wire_row

_FROM = date(2026, 7, 15)
_TO = date(2026, 8, 11)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _cluster(rows: list[dict], settings: Settings, **result_overrides):
    feature_set = build_customer_features(
        wire_result(rows, **result_overrides),
        period_from=_FROM,
        period_to=_TO,
        settings=settings,
    )
    return feature_set, cluster_customers(feature_set, settings=settings)


def test_four_distinct_types_yield_k_four(settings: Settings) -> None:
    """완료 조건 — 분리된 4유형은 k=4 로 갈리고, 각 군집이 한 유형과 1:1 로 대응한다."""
    feature_set, clustering = _cluster(four_type_rows(40), settings)

    assert len(clustering.clusters) == 4
    assert clustering.silhouette is not None and clustering.silhouette > 0.5

    # 라벨 L0001~L0040 = 유형1 … L0121~L0160 = 유형4. 군집이 유형을 섞지 않아야 한다.
    for cluster in clustering.clusters:
        groups = {(int(label[1:]) - 1) // 40 for label in cluster["member_labels"]}
        assert len(groups) == 1, f"{cluster['display_label']} 이 여러 유형을 섞었다"
    assert sum(cluster["size"] for cluster in clustering.clusters) == len(feature_set.rows)


def test_four_types_get_four_distinct_rule_labels(settings: Settings) -> None:
    """판정 규칙이 실제 데이터에서 4종을 모두 짚어낸다(순서가 계약)."""
    _, clustering = _cluster(four_type_rows(40), settings)
    labels = {cluster["rule_label"] for cluster in clustering.clusters}
    assert labels == {
        spec.LABEL_LOYAL,
        spec.LABEL_HESITANT,
        spec.LABEL_DORMANT,
        spec.LABEL_AT_RISK,
    }


def test_same_seed_same_clusters(settings: Settings) -> None:
    """완료 조건 — 재현성. 시드를 고정하지 않으면 세그먼트 변화가 진짜인지 알 수 없다."""
    rows = four_type_rows(40)
    _, first = _cluster(rows, settings)
    _, second = _cluster(rows, settings)

    assert first.labels == second.labels
    assert first.silhouette == second.silhouette
    assert first.pca_used == second.pca_used
    assert [c["rule_label"] for c in first.clusters] == [c["rule_label"] for c in second.clusters]


def test_different_seed_is_recorded_in_scaler_params(settings: Settings) -> None:
    """재현 재료(스케일러 평균·표준편차·축군 가중치)를 각인한다."""
    _, clustering = _cluster(four_type_rows(40), settings)
    params = clustering.scaler_params
    assert params["keys"] == list(spec.CLUSTER_INPUT_KEYS)
    assert len(params["mean"]) == 12 and len(params["std"]) == 12
    assert set(params["group_weights"]) == set(spec.CLUSTER_GROUP_KEYS)


def test_pca_is_auto_compared_and_can_be_disabled(settings: Settings) -> None:
    """on/off 실루엣 자동 비교. 끄면 PCA 구성 자체를 후보에서 뺀다."""
    rows = four_type_rows(40)
    off = Settings(_env_file=None, seller_customer_pca_auto_compare=False)
    _, clustering = _cluster(rows, off)
    assert clustering.pca_used is False
    assert clustering.pca_params is None

    _, auto = _cluster(rows, settings)
    if auto.pca_used:
        assert auto.pca_params is not None
        assert auto.pca_params["n_components"] <= 12


def test_small_clusters_are_relabelled_and_held(settings: Settings) -> None:
    """30명 미만 군집은 재식별·소표본 위험이라 "기타"로 내리고 규모를 밝힌다."""
    rows = four_type_rows(40)
    # 한 유형만 5명으로 줄여 소규모 군집을 만든다(다른 유형은 그대로 40명).
    rows = rows[:40] + rows[40:45] + rows[80:]
    tuned = Settings(_env_file=None, seller_customer_kmeans_k_max=4)
    _, clustering = _cluster(rows, tuned)

    small = [cluster for cluster in clustering.clusters if cluster["size"] < 30]
    if small:  # k 선택에 따라 소군집이 흡수될 수 있어 존재할 때만 규약을 본다
        assert all(cluster["rule_label"] == spec.LABEL_SMALL for cluster in small)
        assert any("small_cluster" in hold.reason for hold in clustering.holds)
    # 행은 지우지 않는다 — 삭제가 아니라 라벨 강등이다.
    assert sum(cluster["size"] for cluster in clustering.clusters) == len(rows)


def test_all_k_rejected_skips_clustering_with_hold(settings: Settings) -> None:
    """탈락 규칙에 후보가 전멸하면 군집을 생략한다 — 조용히 넘어가지 않는다(확정 결정 6)."""
    rows = four_type_rows(8)  # 32명: 어떤 k 든 30명 미만 군집이 2개 이상 나온다
    _, clustering = _cluster(rows, settings)

    assert clustering.clusters == []
    assert clustering.labels == [None] * len(rows)
    assert clustering.silhouette is None
    assert any("no_valid_k" in hold.reason for hold in clustering.holds)


def test_identical_customers_are_not_clustered(settings: Settings) -> None:
    """전 고객 피처가 같으면 군집이 정의되지 않는다 — 분리 불능도 사유를 밝힌다."""
    rows = [wire_row(i) for i in range(1, 41)]
    _, clustering = _cluster(rows, settings)
    assert clustering.clusters == []
    assert any("degenerate_features" in hold.reason for hold in clustering.holds)


def test_too_few_rows_for_k_search(settings: Settings) -> None:
    _, clustering = _cluster([wire_row(1), wire_row(2, sessions=9)], settings)
    assert clustering.clusters == []
    assert any("no_valid_k" in hold.reason for hold in clustering.holds)


def test_empty_rows_produce_no_holds(settings: Settings) -> None:
    """표본 부족(rows=[])의 사유는 load 단계가 이미 남겼다 — 여기서 중복 고지하지 않는다."""
    _, clustering = _cluster([], settings, insufficientCohort=True, totalCustomers=7)
    assert clustering.clusters == []
    assert clustering.holds == []


def test_ratio_to_mean_drops_zero_denominator_keys(settings: Settings) -> None:
    """전체 평균이 0 인 축은 비율이 정의되지 않는다 — 1.0 으로 위장하지 않고 뺀다."""
    rows = four_type_rows(40)
    for row in rows:
        row["cancelCount"] = 0  # 브랜드 전체 취소 0건
    _, clustering = _cluster(rows, settings)
    for cluster in clustering.clusters:
        assert "cancelCount" in cluster["centroid_stats"]
        assert "cancelCount" not in cluster["ratio_to_mean"]


def test_flag_ratios_replace_binary_features_in_distance(settings: Settings) -> None:
    """이진을 거리에 넣는 대신 "이 군집의 N% 가 …" 로 쓴다."""
    _, clustering = _cluster(four_type_rows(40), settings)
    hesitant = next(c for c in clustering.clusters if c["rule_label"] == spec.LABEL_HESITANT)
    assert hesitant["flag_ratios"]["is_cart_abandoner"] == pytest.approx(1.0)
    assert set(hesitant["flag_ratios"]) == set(spec.FLAG_KEYS)
    # 이진 키가 군집 입력에 섞이지 않았다.
    assert not set(spec.FLAG_KEYS) & set(spec.CLUSTER_INPUT_KEYS)


def test_llm_fields_are_left_empty_for_the_llm(settings: Settings) -> None:
    """`llm_label`·`llm_desc` 만 LLM 이 채운다 — 크기·통계·라벨은 코드 소유다."""
    _, clustering = _cluster(four_type_rows(40), settings)
    assert all(
        cluster["llm_label"] == "" and cluster["llm_desc"] == "" for cluster in clustering.clusters
    )


def test_duplicate_labels_number_display_only() -> None:
    """조인 키(`rule_label`)는 원형, 표시(`display_label`)만 번호 — 결정 28a.

    번호까지 조인 키에 섞으면 `churn` 워커의 이동 행렬과 시점 간 추적이 1:N 이 된다.
    """
    from app.agents.seller.features.clustering import _apply_display_labels

    clusters = [
        {"rule_label": spec.LABEL_EXPLORER, "display_label": ""},
        {"rule_label": spec.LABEL_EXPLORER, "display_label": ""},
        {"rule_label": spec.LABEL_LOYAL, "display_label": ""},
    ]
    _apply_display_labels(clusters)

    assert [c["display_label"] for c in clusters] == [
        f"{spec.LABEL_EXPLORER}(1)",
        f"{spec.LABEL_EXPLORER}(2)",
        spec.LABEL_LOYAL,
    ]
    assert {c["rule_label"] for c in clusters} == {spec.LABEL_EXPLORER, spec.LABEL_LOYAL}


@pytest.mark.parametrize(
    ("percentiles", "expected"),
    [
        (
            {"recency": 10, "orders": 20, "carts": 90, "order_rate": 10, "amount": 10},
            spec.LABEL_DORMANT,
        ),
        (
            {"recency": 40, "orders": 80, "carts": 20, "order_rate": 90, "amount": 90},
            spec.LABEL_AT_RISK,
        ),
        (
            {"recency": 90, "orders": 40, "carts": 80, "order_rate": 10, "amount": 10},
            spec.LABEL_HESITANT,
        ),
        (
            {"recency": 90, "orders": 90, "carts": 20, "order_rate": 90, "amount": 90},
            spec.LABEL_LOYAL,
        ),
        (
            {"recency": 60, "orders": 40, "carts": 20, "order_rate": 60, "amount": 40},
            spec.LABEL_EXPLORER,
        ),
    ],
)
def test_rule_label_order_is_the_contract(percentiles: dict, expected: str) -> None:
    """휴면 → 이탈위험 → 구매망설임 → 충성 → 탐색. 먼저 걸리는 라벨로 확정한다.

    첫 케이스가 순서를 검증한다 — 담기 백분위 90 이라 구매망설임 조건도 만족하지만,
    휴면형이 먼저 걸리므로 휴면형이어야 한다.
    """
    assert _rule_label(percentiles, spec.DEFAULT_LABEL_THRESHOLDS) == expected
