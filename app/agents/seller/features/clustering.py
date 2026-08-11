"""고객 축 군집 파이프라인 (이슈 #593, `04-CLUSTERING` 전체).

```
표준화(StandardScaler) → 축군 가중치 → (PCA 자동 판정) → K-Means → rule_label
```

`analysis/segmentation.cluster_products`(상품 축)의 구조를 복제한다. 그 파일 docstring 이
*"고객별 원시 데이터가 생기는 Phase B 에서 고객 축 정식 도입"* 이라고 예고해 뒀고, I-38 이
그 Phase B 다. 다른 점은 셋이다 — ① 입력이 5차원이 아니라 12차원 ② 축군 가중치와 PCA 가
붙는다 ③ 라벨이 백분위 판정이다(상품 축은 전체 평균 대비 비교).

결정론: `random_state`·`n_init` 주입 + `scaler_params`·`pca_params` 각인. 시드를 고정하지
않으면 어제와 오늘의 세그먼트 변화가 진짜 변화인지 난수 탓인지 구분할 수 없다(`04` §3.2).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from app.agents.seller.features import spec, transforms
from app.agents.seller.features.customer import CustomerFeatureSet
from app.agents.seller.sop.context import Hold
from app.core.config import Settings


@dataclass(frozen=True)
class CustomerClustering:
    """군집 산출 — 스냅샷 컬럼과 1:1 로 대응한다.

    `labels` 는 `feature_set.rows` 와 같은 순서의 행별 `cluster_id` 이고, 군집을 생략한
    경우 전부 None 이다(빈 `clusters` 가 "군집 생략" 신호 — `cluster_products` 선례).
    """

    clusters: list[dict]
    labels: list[int | None]
    scaler_params: dict
    pca_used: bool
    pca_params: dict | None = None
    silhouette: float | None = None
    holds: list[Hold] = field(default_factory=list)


def _column_weights(settings: Settings) -> list[float]:
    """열별 가중치 — 축군마다 `1/√n`(기본). `CLUSTER_INPUT_KEYS` 순서로 편다."""
    group_weights = settings.seller_customer_cluster_group_weights
    per_key: dict[str, float] = {}
    for group, keys in spec.CLUSTER_GROUP_KEYS.items():
        weight = float(group_weights[group])
        for key in keys:
            per_key[key] = weight
    return [per_key[key] for key in spec.CLUSTER_INPUT_KEYS]


def _rule_label(percentiles: dict[str, float], thresholds: dict[str, float]) -> str:
    """군집 centroid 의 백분위로 라벨을 확정한다 — **위에서부터 먼저 걸리는 것**(`04` §4.2).

    절대 기준("주문 3회 이상 = 충성형")은 브랜드마다 적정값이 달라 튜닝이 필요하다.
    상대 기준은 규모와 무관하게 작동하는 대신 "장사가 안 되는 기간에도 상위 20%는
    무조건 충성형"이 되는데, 보고서가 절대 수치를 병기해 완화한다.

    ⚠️ 구매망설임형에 "군집 내 `is_cart_abandoner` 비율" 조건은 넣지 않는다 —
    `03` §5 초안에 있었으나 `04` §4.2(정본)가 백분위 2개로 확정했다.
    """
    if (
        percentiles["recency"] <= thresholds["dormant_recency_max"]
        and percentiles["orders"] <= thresholds["dormant_orders_max"]
    ):
        return spec.LABEL_DORMANT
    if (
        percentiles["recency"] <= thresholds["at_risk_recency_max"]
        and percentiles["orders"] > thresholds["at_risk_orders_min"]
    ):
        return spec.LABEL_AT_RISK
    if (
        percentiles["carts"] > thresholds["hesitant_carts_min"]
        and percentiles["order_rate"] <= thresholds["hesitant_order_rate_max"]
    ):
        return spec.LABEL_HESITANT
    if (
        percentiles["orders"] > thresholds["loyal_orders_min"]
        and percentiles["amount"] > thresholds["loyal_amount_min"]
        and percentiles["recency"] > thresholds["loyal_recency_min"]
    ):
        return spec.LABEL_LOYAL
    return spec.LABEL_EXPLORER


def _label_axes(rows: list[dict]) -> dict[str, list[float]]:
    """라벨 판정 5축의 고객별 값 — 백분위 분모가 되는 분포다.

    `order_rate` 는 평활값을 쓴다(원값은 결제 시작이 0 인 고객에서 None 이라 분포가
    성립하지 않는다). 나머지는 원값이다.
    """
    ordinal_index = {bucket: i for i, bucket in enumerate(spec.AMOUNT_BUCKET_ORDER)}
    fallback = len(spec.AMOUNT_BUCKET_ORDER) // 2
    return {
        "recency": [row["vector"]["recency_score"] for row in rows],
        "orders": [float(row["raw"]["orderCount"]) for row in rows],
        "carts": [float(row["raw"]["cartAdds"]) for row in rows],
        "order_rate": [row["vector"]["order_rate"] for row in rows],
        "amount": [float(ordinal_index.get(row["raw"]["amountBucket"], fallback)) for row in rows],
    }


def _centroid_stats(rows: list[dict], members: list[int]) -> dict[str, float]:
    """군집 centroid — **PCA·스케일링 이전 원 피처 평균**(`04` §6.1).

    `_raw` 계열은 None(관측 없음)을 제외하고 평균한다 — 0 으로 채워 평균을 내리면
    "관측이 없었다"가 "전환율이 낮았다"로 둔갑한다. 전원이 결측이면 키를 뺀다.
    """
    ordinal_index = {bucket: i for i, bucket in enumerate(spec.AMOUNT_BUCKET_ORDER)}
    stats: dict[str, float] = {}
    for key in spec.CENTROID_RAW_KEYS:
        stats[key] = sum(float(rows[i]["raw"][key]) for i in members) / len(members)
    ordinals = [
        ordinal_index[rows[i]["raw"]["amountBucket"]]
        for i in members
        if rows[i]["raw"]["amountBucket"] in ordinal_index
    ]
    if ordinals:
        stats[spec.CENTROID_AMOUNT_KEY] = sum(ordinals) / len(ordinals)
    for key in spec.CENTROID_DERIVED_KEYS:
        observed = [
            float(rows[i]["derived"][key]) for i in members if rows[i]["derived"][key] is not None
        ]
        if observed:
            stats[key] = sum(observed) / len(observed)
    return stats


def _ratio_to_mean(stats: dict[str, float], overall: dict[str, float]) -> dict[str, float]:
    """군집 평균 / 전체 평균. **분모 0 인 키는 뺀다** — 0 나눗셈을 1.0 으로 위장하지 않는다.

    워커가 아니라 여기서 미리 계산한다 — LLM 에게 나눗셈을 시키지 않기 위해서다(`04` §6.1).
    """
    return {key: value / overall[key] for key, value in stats.items() if overall.get(key)}


def _flag_ratios(rows: list[dict], members: list[int]) -> dict[str, float]:
    """군집 내 플래그 True 비율 — 이진을 거리에 섞는 대신 프로파일로 쓴다(`03` §3.2)."""
    return {
        key: sum(1 for i in members if rows[i]["flags"][key]) / len(members)
        for key in spec.FLAG_KEYS
    }


def _apply_display_labels(clusters: list[dict]) -> None:
    """같은 `rule_label` 이 여러 군집에 붙으면 **표시만 번호**를 붙인다(`04` 결정 28a).

    k=6 이면 라벨 5종으로는 모자라 최소 두 군집이 같은 라벨을 받는다. 조인 키까지
    번호를 붙이면 `churn` 워커의 이동 행렬과 시점 간 추적이 1:N 이 되어 무너지므로,
    `rule_label` 은 **원형**을 유지하고 `display_label` 만 번호를 받는다.
    `cluster_products` 가 쓰는 규약을 그대로 가져왔다.
    """
    counts: dict[str, int] = {}
    for cluster in clusters:
        counts[cluster["rule_label"]] = counts.get(cluster["rule_label"], 0) + 1
    seen: dict[str, int] = {}
    for cluster in clusters:
        label = cluster["rule_label"]
        if counts[label] > 1:
            seen[label] = seen.get(label, 0) + 1
            cluster["display_label"] = f"{label}({seen[label]})"
        else:
            cluster["display_label"] = label


def cluster_customers(feature_set: CustomerFeatureSet, *, settings: Settings) -> CustomerClustering:
    """고객 군집화 — PCA on/off × k 후보를 돌려 실루엣 최대 구성을 채택한다.

    빈 `clusters` 는 "군집 생략" 신호다: 행이 부족하거나(k 후보 없음), 전 고객 피처가
    동일하거나(분리 불능), **30명 미만 군집이 2개 이상 나오는 k 뿐이어서** 후보가 전멸한
    경우다. 생략해도 `feature_rows` 는 그대로 저장한다 — 조용히 빼면 합계가 안 맞아
    보이므로 사유는 `Hold` 로 반드시 밝힌다.
    """
    rows = feature_set.rows
    holds: list[Hold] = []
    empty_scaler = {"keys": list(spec.CLUSTER_INPUT_KEYS), "mean": [], "std": [], "imputed": {}}

    n = len(rows)
    k_min = settings.seller_customer_kmeans_k_min
    k_max = settings.seller_customer_kmeans_k_max
    if n == 0 or min(k_max, n - 1) < k_min:
        if n:
            holds.append(
                Hold(step="compute", reason=f"no_valid_k: 고객 {n}명은 k={k_min} 탐색에 부족하다")
            )
        return CustomerClustering(
            clusters=[], labels=[None] * n, scaler_params=empty_scaler, pca_used=False, holds=holds
        )

    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.metrics import silhouette_score

    matrix = np.array(feature_set.matrix, dtype=float)
    # 결측(NaN — 매핑 없는 amountBucket) 대체: 관측치 평균. 표준화 후 0(중립)이 되어
    # 그 고객의 군집 판정에 이 축이 기여하지 않는다. 저장되는 vector 블록은 None 그대로다.
    imputed: dict[str, float] = {}
    for column in range(matrix.shape[1]):
        missing = np.isnan(matrix[:, column])
        if not missing.any():
            continue
        observed = matrix[~missing, column]
        replacement = float(observed.mean()) if observed.size else 0.0
        matrix[missing, column] = replacement
        imputed[spec.CLUSTER_INPUT_KEYS[column]] = replacement

    means = matrix.mean(axis=0)
    variances = matrix.var(axis=0)
    # ⚠️ 무변동 피처를 `std == 0.0` 로만 거르면 안 된다 — 값이 전부 같아도 평균 계산의
    # 반올림 때문에 분산이 정확히 0 이 아니라 1e-32 쯤으로 남을 수 있고, 그걸로 나누면
    # 부동소수 오차가 O(1) 로 증폭돼 **없는 구조가 만들어진다**(전 고객이 동일한
    # 입력에서 군집이 나오는 것을 실측). sklearn StandardScaler 의 상수열 판정과 같은
    # 기준(분산 <= eps × n × mean²)을 쓴다.
    epsilon = float(np.finfo(np.float64).eps)
    constant = variances <= (epsilon * n) * np.maximum(means**2, 1.0)
    centered = matrix - means
    centered[:, constant] = 0.0  # 무변동 피처는 표준화에서 0 기여
    stds = np.sqrt(variances)
    stds[constant] = 1.0
    stds[stds == 0.0] = 1.0  # 0 나눗셈 방지(방어)
    weights = np.array(_column_weights(settings), dtype=float)
    # 순서 엄수: 표준화 → 가중치 → PCA. 가중치를 준 뒤 회전해야 의도가 반영된 채로
    # 축이 합쳐진다(`04` §2.3).
    weighted = (centered / stds) * weights
    scaler_params = {
        "keys": list(spec.CLUSTER_INPUT_KEYS),
        "mean": [float(v) for v in means],
        "std": [float(v) for v in stds],
        "group_weights": dict(settings.seller_customer_cluster_group_weights),
        "imputed": imputed,
    }

    if not np.any(weighted):
        holds.append(
            Hold(
                step="compute",
                reason="degenerate_features: 전 고객 피처가 동일해 군집이 정의되지 않는다",
            )
        )
        return CustomerClustering(
            clusters=[], labels=[None] * n, scaler_params=scaler_params, pca_used=False, holds=holds
        )

    min_size = settings.seller_customer_segment_min_size
    random_state = settings.seller_customer_kmeans_random_state
    n_init = settings.seller_customer_kmeans_n_init
    upper = min(k_max, n - 1)  # 실루엣은 군집 수 < 표본 수 를 요구한다

    candidates: list[tuple[bool, object, dict | None]] = [(False, weighted, None)]
    if settings.seller_customer_pca_auto_compare:
        # n_components 를 실수로 주려면 svd_solver="full" 이어야 한다(sklearn 계약).
        pca = PCA(n_components=settings.seller_customer_pca_variance, svd_solver="full")
        reduced = pca.fit_transform(weighted)
        candidates.append(
            (
                True,
                reduced,
                {
                    "n_components": int(pca.n_components_),
                    "variance_target": settings.seller_customer_pca_variance,
                    "explained_variance_ratio": [float(v) for v in pca.explained_variance_ratio_],
                },
            )
        )

    best: tuple[float, bool, int, object, dict | None] | None = None
    rejected_k = 0
    for pca_used, space, pca_params in candidates:
        for k in range(k_min, upper + 1):
            with warnings.catch_warnings():
                # 중복 피처 점이 많으면 "distinct clusters < n_clusters" 경고가 난다 —
                # 후보 탐색에선 예상되는 퇴화라(아래에서 라벨 수로 거른다) 삼킨다.
                warnings.simplefilter("ignore", ConvergenceWarning)
                fit = KMeans(n_clusters=k, random_state=random_state, n_init=n_init).fit(space)
            labels = [int(label) for label in fit.labels_]
            if len(set(labels)) < 2:
                continue  # 퇴화(전부 한 군집) — 실루엣 정의 불가
            sizes = [labels.count(cluster_id) for cluster_id in set(labels)]
            # 실루엣은 잘게 쪼갤수록 올라가는 경향이 있어, 이 규칙이 없으면
            # "3명짜리 군집 3개 + 나머지" 같은 답이 점수만 높게 나온다(`04` §3.1).
            if sum(1 for size in sizes if size < min_size) >= 2:
                rejected_k += 1
                continue
            score = float(silhouette_score(space, fit.labels_))
            # 동률이면 먼저 본 구성(PCA off · 작은 k) 우선 — 결정론.
            if best is None or score > best[0]:
                best = (score, pca_used, k, labels, pca_params)

    if best is None:
        holds.append(
            Hold(
                step="compute",
                reason=(
                    "no_valid_k: 30명 미만 군집이 2개 이상 나오는 k 뿐이라 군집을 생략한다"
                    f" (k={k_min}~{upper}, 탈락 {rejected_k}건)"
                ),
            )
        )
        return CustomerClustering(
            clusters=[], labels=[None] * n, scaler_params=scaler_params, pca_used=False, holds=holds
        )

    silhouette, pca_used, _k, labels, pca_params = best

    axes = _label_axes(rows)
    sorted_axes = {name: sorted(values) for name, values in axes.items()}
    # 전체 평균도 같은 함수로 낸다 — 군집 평균과 정의가 어긋나면 `ratio_to_mean` 이
    # 조용히 틀린다(결측 제외 규칙이 분자·분모에 동일하게 적용돼야 한다).
    overall = _centroid_stats(rows, list(range(n)))
    thresholds = settings.seller_customer_label_thresholds

    clusters: list[dict] = []
    for cluster_id in sorted(set(labels)):
        members = [i for i, label in enumerate(labels) if label == cluster_id]
        stats = _centroid_stats(rows, members)
        percentiles = {
            name: transforms.percentile_of(
                sorted_axes[name], sum(axes[name][i] for i in members) / len(members)
            )
            for name in axes
        }
        clusters.append(
            {
                "cluster_id": cluster_id,
                "rule_label": _rule_label(percentiles, thresholds),
                "display_label": "",
                # LLM 이 채우는 유일한 두 필드 — 코드는 빈 문자열로 둔다(불변 규약).
                "llm_label": "",
                "llm_desc": "",
                "size": len(members),
                "centroid_stats": stats,
                "ratio_to_mean": _ratio_to_mean(stats, overall),
                "flag_ratios": _flag_ratios(rows, members),
                "member_labels": [rows[i]["customerLabel"] for i in members],
            }
        )

    # 소규모 군집: 재식별 위험(4명짜리 집단은 누구인지 특정될 수 있다) + 소표본 평균의
    # 불안정을 동시에 막는다. 행은 지우지 않고 라벨만 "기타"로 바꾼다(`04` §3.3).
    small = [cluster for cluster in clusters if cluster["size"] < min_size]
    for cluster in small:
        cluster["rule_label"] = spec.LABEL_SMALL
    if small:
        holds.append(
            Hold(
                step="compute",
                reason=(
                    f"small_cluster: {', '.join(str(c['size']) for c in small)}명 규모"
                    f" {len(small)}개 군집 분류 보류"
                ),
            )
        )

    clusters.sort(key=lambda c: (-c["size"], c["cluster_id"]))
    _apply_display_labels(clusters)

    return CustomerClustering(
        clusters=clusters,
        labels=list(labels),
        scaler_params=scaler_params,
        pca_used=pca_used,
        pca_params=pca_params,
        silhouette=silhouette,
        holds=holds,
    )
