"""상품 행동 피처화 + k-means 군집화 — behavior 워커 (이슈 #290).

근거 논문(docs/worker-papers.md):
- Chen, Sain & Guo 2012: RFM + k-means 세그멘테이션 레시피. 단, 원 논문의 고객
  축은 계약(I-13)에 없어 **상품 축으로 재정의**한다 — 고객별 원시 데이터가 생기는
  Phase B 에서 고객 축 정식 도입.
- Moe 2003: 방문 의도 유형론(buying/searching/browsing) — 군집 규칙 라벨의 어휘
  (전환직결형/카트이탈형/구경형)를 여기서 가져온다.

피처 벡터(상품당 5차원): [log(1+view), cart/view, checkout/cart, purchase/checkout,
visitors/view] — 카운트 절대량은 로그로 눌러(멱법칙 분포) 비율 피처와 스케일을
맞추고, 표준화(z-score) 후 k-means. k 는 k_min..k_max 중 실루엣 최대 선택.

순수 함수·결정론(§10-②): random_state·n_init 고정 주입 — 같은 입력 = 같은 군집.
[#196 주의] purchaseComplete 는 상품 미귀속으로 0 일 수 있다 — purchase 파생
피처가 0 인 것은 '구매 전무'가 아니라 수집 경로 문제일 수 있음(도구 노트가 통제).
"""

from __future__ import annotations

import math
from typing import TypedDict

from app.agents.seller.analysis.types import ClusterSummary

# 군집 생략 하한 — 상품 수 < k_min×3 이면 군집당 기대 표본이 3개 미만이라 실루엣이
# 불안정하다(handoff §4.3 폴백 규칙). 호출부는 빈 결과를 "군집 생략"으로 표기한다.
_MIN_PRODUCTS_PER_CLUSTER = 3

_FEATURE_NAMES = (
    "log_view",
    "cart_per_view",
    "checkout_per_cart",
    "purchase_per_checkout",
    "visitors_per_view",
)


class ProductActivity(TypedDict, total=False):
    """cluster_products 입력 1행 — 도구 층이 I-13 row 에서 뽑아 전달한다."""

    product_id: int
    view: int
    cart: int
    checkout: int
    purchase: int
    visitors: int | None


def _ratio(numerator: float, denominator: float) -> float:
    """분모 0 이면 0.0 — 비율 피처 전용(단계 미발생 상품의 자연스러운 기본값)."""
    return numerator / denominator if denominator else 0.0


def _features(row: ProductActivity) -> list[float]:
    view = row.get("view", 0)
    cart = row.get("cart", 0)
    checkout = row.get("checkout", 0)
    purchase = row.get("purchase", 0)
    visitors = row.get("visitors")
    return [
        math.log1p(max(0, view)),
        _ratio(cart, view),
        _ratio(checkout, cart),
        _ratio(purchase, checkout),
        _ratio(visitors if visitors is not None else 0, view),
    ]


def _label_cluster(centroid: dict[str, float], overall: dict[str, float]) -> str:
    """군집 중심을 전체 평균과 비교해 규칙 라벨을 붙인다(Moe 2003 유형론 어휘).

    규칙은 순서가 계약이다 — 카트이탈(개입 여지 최대)을 먼저 잡고, 전 단계 우량을
    전환직결로, 트래픽만 높은 군집을 구경형으로, 나머지는 활동량으로 가른다.
    """
    if (
        centroid["cart_per_view"] >= overall["cart_per_view"]
        and centroid["checkout_per_cart"] < overall["checkout_per_cart"]
    ):
        return "카트이탈형"
    if (
        centroid["cart_per_view"] >= overall["cart_per_view"]
        and centroid["checkout_per_cart"] >= overall["checkout_per_cart"]
        and centroid["purchase_per_checkout"] >= overall["purchase_per_checkout"]
    ):
        return "전환직결형"
    if (
        centroid["log_view"] >= overall["log_view"]
        and centroid["cart_per_view"] < overall["cart_per_view"]
    ):
        return "구경형"
    return "저활동형" if centroid["log_view"] < overall["log_view"] else "혼합형"


def cluster_products(
    rows: list[ProductActivity], *, k_min: int, k_max: int, random_state: int
) -> list[ClusterSummary]:
    """상품 행동 군집화 — k 는 실루엣 최대 선택, 규칙 라벨 부착.

    빈 결과([])는 "군집 생략" 신호다: 상품 수 < k_min×3(불안정), 또는 전 상품
    피처 동일(분리 불능 — 실루엣 정의 불가). 호출부가 생략 사유를 표기한다.
    반환 군집은 크기 내림차순(동률은 첫 상품 id 오름차순) — 결정론 정렬.
    """
    if k_min < 2 or k_max < k_min:
        raise ValueError(f"k 범위가 유효하지 않다(k_min={k_min}, k_max={k_max})")
    n = len(rows)
    if n < k_min * _MIN_PRODUCTS_PER_CLUSTER:
        return []

    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    matrix = np.array([_features(row) for row in rows], dtype=float)
    means = matrix.mean(axis=0)
    stds = matrix.std(axis=0)
    stds[stds == 0.0] = 1.0  # 무변동 피처는 표준화에서 0 기여(0 나눗셈 방지)
    standardized = (matrix - means) / stds
    if not np.any(standardized):
        return []  # 전 상품 피처 동일 — 군집이 정의될 수 없다

    best: tuple[float, int, object] | None = None  # (silhouette, k, labels)
    upper = min(k_max, n - 1)  # 실루엣은 군집 수 < 표본 수 를 요구한다
    for k in range(k_min, upper + 1):
        import warnings

        with warnings.catch_warnings():
            # 중복 피처 점이 많으면 KMeans 가 "distinct clusters < n_clusters"
            # ConvergenceWarning 을 낸다 — k 후보 탐색에선 예상되는 퇴화라(아래에서
            # 라벨 수로 걸러 skip) 경고를 삼켜 테스트/로그 노이즈를 막는다.
            from sklearn.exceptions import ConvergenceWarning

            warnings.simplefilter("ignore", ConvergenceWarning)
            fit = KMeans(n_clusters=k, random_state=random_state, n_init=10).fit(standardized)
        if len(set(fit.labels_)) < 2:
            continue  # 퇴화(전부 한 군집) — 실루엣 정의 불가
        score = float(silhouette_score(standardized, fit.labels_))
        # 동률이면 작은 k(단순한 설명) 우선 — range 오름차순이라 '>' 비교로 충분(결정론).
        if best is None or score > best[0]:
            best = (score, k, fit.labels_)
    if best is None:
        return []
    silhouette, _, labels = best

    overall = dict(zip(_FEATURE_NAMES, (float(v) for v in means), strict=True))
    clusters: list[ClusterSummary] = []
    for cluster_id in sorted(set(int(label) for label in labels)):
        member_indices = [i for i, label in enumerate(labels) if int(label) == cluster_id]
        centroid_values = matrix[member_indices].mean(axis=0)  # 원 피처 공간(해석 가능 단위)
        centroid = dict(zip(_FEATURE_NAMES, (float(v) for v in centroid_values), strict=True))
        clusters.append(
            ClusterSummary(
                label=_label_cluster(centroid, overall),
                product_ids=[rows[i].get("product_id", -1) for i in member_indices],
                size=len(member_indices),
                centroid=centroid,
                silhouette=silhouette,
            )
        )
    clusters.sort(key=lambda c: (-c.size, c.product_ids[0] if c.product_ids else -1))
    # 같은 라벨이 여러 군집에 붙으면 번호로 구분한다(예: 구경형(1)·구경형(2)) — LLM 이
    # 군집을 혼동하지 않게. 단일 라벨은 번호 없이 그대로.
    label_counts: dict[str, int] = {}
    for cluster in clusters:
        label_counts[cluster.label] = label_counts.get(cluster.label, 0) + 1
    seen: dict[str, int] = {}
    renamed: list[ClusterSummary] = []
    for cluster in clusters:
        if label_counts[cluster.label] > 1:
            seen[cluster.label] = seen.get(cluster.label, 0) + 1
            renamed.append(
                ClusterSummary(
                    label=f"{cluster.label}({seen[cluster.label]})",
                    product_ids=cluster.product_ids,
                    size=cluster.size,
                    centroid=cluster.centroid,
                    silhouette=cluster.silhouette,
                )
            )
        else:
            renamed.append(cluster)
    return renamed
