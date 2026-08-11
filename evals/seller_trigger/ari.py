"""군집 안정성 — Adjusted Rand Index (이슈 #595, `12-EVAL` 결정 121).

``random_state`` 고정은 **"같은 입력 = 같은 출력"** 만 보장한다. **"입력이 조금 달라지면
어떻게 되나"** 는 보장하지 않는다. 그런데 `churn` 워커의 이동 행렬 전체가
*"어제 충성형이던 사람이 오늘 이탈위험형"* 이라는 비교 위에 서 있다 — 군집이 불안정하면
그 이동이 전부 난수다. 결정 28(`random_state` 고정)의 원래 취지(*"변화가 진짜인지 난수
탓인지 구분"*)를 실제로 측정하는 것이 이 하네스다.

[왜 고객 축이 게이트인가]
`12-EVAL` §6.3 의 처방(*"축군 가중치 조정(`04` §1.2) 또는 k_max 하향"*)과 중요성 근거
(*"`churn` 워커의 이동 행렬"*)가 둘 다 고객 축을 가리킨다 — `seller_customer_cluster_
group_weights`·`seller_customer_kmeans_k_max` 는 고객 축에만 있는 튜너블이다. 상품 축
(`cluster_products`)도 같은 절차로 재되 **exploratory** 로만 기록한다(게이트 아님).

[군집 생략은 0.0 이 아니다]
행이 부족하거나 30명 미만 군집이 2개 이상이면 `cluster_customers` 는 빈 결과를 준다.
그때 ARI 를 0.0 으로 적으면 "불안정하다"로 읽히는데 실제로는 **재지 못한 것**이다 —
`measured=False` 로 갈라 두고 게이트를 통과시키지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.agents.seller.analysis import segmentation
from app.agents.seller.features.clustering import cluster_customers
from app.agents.seller.features.customer import build_customer_features
from app.core.config import Settings
from app.schemas.spring import SellerCustomerFeaturesResult

ARI_SEED = 595002
# 고객 축은 30명 미만 군집이 2개 이상이면 그 k 가 탈락한다(seller_customer_segment_min_size).
# k_max=6 까지 탐색하므로 여유를 두어 60×5=300 명으로 잡는다.
DEFAULT_PER_GROUP = 60
DEFAULT_NOISE_PCT = 0.05
# 유형 **내부** 변주 폭 — 이게 이 하네스의 감도를 정한다.
# ±20% 로 좁히면 5개 유형이 서로 완전히 떨어진 덩어리가 되고, 노이즈를 50% 로 올려도
# ARI 가 1.0 에서 안 내려온다(실측) — 즉 **측정이 헛돈다**. 실제 고객 분포는 유형 경계가
# 겹치므로 2~4배 변주를 준다. 이 값에서 노이즈 5% → ARI 1.000, 80% → 0.396(게이트 실패)로
# 채널이 살아 있음이 확인된다(반대 테스트가 그것을 못박는다).
WITHIN_TYPE_LOW = 0.45
WITHIN_TYPE_HIGH = 1.8

_AMOUNT_BUCKETS = ("ZERO", "LT_10K", "10K_50K", "50K_100K", "100K_300K", "GTE_300K")

# 5개 잠재 유형 — (sessions, productViews, cartAdds, checkoutStarts, orderCount,
# cancelCount, amountBucket, lastActivityDaysAgo, firstSeenDaysAgo).
# `features/spec.RULE_LABELS` 어휘에 대응하도록 축을 서로 반대로 밀어 둔다.
_LATENT_TYPES: tuple[tuple[str, dict[str, float | str]], ...] = (
    (
        "충성",
        dict(
            sessions=22,
            productViews=64,
            cartAdds=19,
            checkoutStarts=16,
            orderCount=13,
            cancelCount=1,
            amountBucket="GTE_300K",
            lastActivityDaysAgo=2,
            firstSeenDaysAgo=320,
        ),
    ),
    (
        "구매망설임",
        dict(
            sessions=13,
            productViews=58,
            cartAdds=23,
            checkoutStarts=9,
            orderCount=0,
            cancelCount=0,
            amountBucket="ZERO",
            lastActivityDaysAgo=3,
            firstSeenDaysAgo=130,
        ),
    ),
    (
        "휴면",
        dict(
            sessions=1,
            productViews=2,
            cartAdds=0,
            checkoutStarts=0,
            orderCount=0,
            cancelCount=0,
            amountBucket="ZERO",
            lastActivityDaysAgo=310,
            firstSeenDaysAgo=620,
        ),
    ),
    (
        "이탈위험",
        dict(
            sessions=7,
            productViews=17,
            cartAdds=5,
            checkoutStarts=5,
            orderCount=5,
            cancelCount=1,
            amountBucket="100K_300K",
            lastActivityDaysAgo=62,
            firstSeenDaysAgo=390,
        ),
    ),
    (
        "탐색",
        dict(
            sessions=9,
            productViews=41,
            cartAdds=3,
            checkoutStarts=1,
            orderCount=1,
            cancelCount=0,
            amountBucket="LT_10K",
            lastActivityDaysAgo=6,
            firstSeenDaysAgo=45,
        ),
    ),
)

_COUNT_KEYS = (
    "sessions",
    "productViews",
    "cartAdds",
    "checkoutStarts",
    "orderCount",
    "cancelCount",
    "lastActivityDaysAgo",
    "firstSeenDaysAgo",
)


@dataclass(frozen=True)
class AriReport:
    """군집 안정성 측정 1건."""

    axis: str
    dataset_version: str
    seed: int
    rows: int
    noise_pct: float
    measured: bool
    ari: float | None
    minimum: float
    passed: bool
    clusters_a: int
    clusters_b: int
    silhouette_a: float | None = None
    silhouette_b: float | None = None
    label_sizes_a: dict[str, int] = field(default_factory=dict)
    label_sizes_b: dict[str, int] = field(default_factory=dict)
    note: str = ""


def build_customer_rows(*, seed: int = ARI_SEED, per_group: int = DEFAULT_PER_GROUP) -> list[dict]:
    """I-38 와이어 행 — 5개 잠재 유형에서 표집. 결정론(numpy default_rng)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    index = 0
    for _, template in _LATENT_TYPES:
        for _ in range(per_group):
            index += 1
            row: dict = {"customerLabel": f"C{index:05d}", "amountBucket": template["amountBucket"]}
            for key in _COUNT_KEYS:
                base = float(template[key])
                # 0 인 축은 0 으로 둔다 — 흔들면 그 유형의 정의(주문 0 건 등)가 흐려진다.
                row[key] = (
                    int(round(base * float(rng.uniform(WITHIN_TYPE_LOW, WITHIN_TYPE_HIGH))))
                    if base
                    else 0
                )
            rows.append(row)
    return rows


def jitter_rows(rows: list[dict], *, seed: int, noise_pct: float) -> list[dict]:
    """카운트 축에 ±noise_pct 곱셈 노이즈. ``amountBucket`` 은 범주라 건드리지 않는다."""
    import numpy as np

    rng = np.random.default_rng(seed)
    noisy: list[dict] = []
    for row in rows:
        new = dict(row)
        for key in _COUNT_KEYS:
            value = float(row[key])
            new[key] = max(0, int(round(value * float(rng.uniform(1 - noise_pct, 1 + noise_pct)))))
        noisy.append(new)
    return noisy


def _wire_result(rows: list[dict]) -> SellerCustomerFeaturesResult:
    return SellerCustomerFeaturesResult.model_validate(
        {
            "totalCustomers": len(rows),
            "rowLimit": 1000,
            "truncated": False,
            "insufficientCohort": False,
            "amountBuckets": list(_AMOUNT_BUCKETS),
            "rows": rows,
        }
    )


def _cluster_once(rows: list[dict], *, settings: Settings):
    feature_set = build_customer_features(
        _wire_result(rows),
        period_from=date(2026, 7, 12),
        period_to=date(2026, 8, 10),
        settings=settings,
    )
    return cluster_customers(feature_set, settings=settings)


def measure_customer_ari(
    *,
    settings: Settings,
    dataset_version: str,
    seed: int = ARI_SEED,
    per_group: int = DEFAULT_PER_GROUP,
    noise_pct: float = DEFAULT_NOISE_PCT,
) -> AriReport:
    """고객 축 ARI — 같은 모집단에 노이즈를 **두 번 독립으로** 섞어 군집을 두 번 만든다.

    한 번만 섞고 원본과 비교하면 "원본이 정답"이라는 전제가 들어간다. 운영에서 비교되는
    것은 어제 스냅샷과 오늘 스냅샷이고 **둘 다 노이즈가 실린 관측**이라, 두 관측을
    서로 비교하는 것이 실제 상황과 같다.
    """
    base = build_customer_rows(seed=seed, per_group=per_group)
    a = _cluster_once(jitter_rows(base, seed=seed + 1, noise_pct=noise_pct), settings=settings)
    b = _cluster_once(jitter_rows(base, seed=seed + 2, noise_pct=noise_pct), settings=settings)
    minimum = settings.seller_cluster_stability_min

    if not a.clusters or not b.clusters or any(x is None for x in (*a.labels, *b.labels)):
        return AriReport(
            axis="customer",
            dataset_version=dataset_version,
            seed=seed,
            rows=len(base),
            noise_pct=noise_pct,
            measured=False,
            ari=None,
            minimum=minimum,
            passed=False,
            clusters_a=len(a.clusters),
            clusters_b=len(b.clusters),
            note="군집 생략 — ARI 가 정의되지 않는다(0.0 으로 적지 않는다). 합성 모집단을 키워야 한다",
        )

    from sklearn.metrics import adjusted_rand_score

    ari = float(adjusted_rand_score(a.labels, b.labels))
    return AriReport(
        axis="customer",
        dataset_version=dataset_version,
        seed=seed,
        rows=len(base),
        noise_pct=noise_pct,
        measured=True,
        ari=ari,
        minimum=minimum,
        passed=ari >= minimum,
        clusters_a=len(a.clusters),
        clusters_b=len(b.clusters),
        silhouette_a=a.silhouette,
        silhouette_b=b.silhouette,
        # display_label 로 키를 잡는다 — rule_label 은 k=6 에서 중복될 수 있어 dict 에서
        # 뭉개지고(04 결정 28a), 그러면 리포트의 군집 수와 항목 수가 어긋나 보인다.
        label_sizes_a={c["display_label"]: c["size"] for c in a.clusters},
        label_sizes_b={c["display_label"]: c["size"] for c in b.clusters},
    )


def measure_product_ari(
    *,
    settings: Settings,
    dataset_version: str,
    rows: list[dict],
    seed: int = ARI_SEED,
    noise_pct: float = DEFAULT_NOISE_PCT,
) -> AriReport:
    """상품 축 ARI — **exploratory**(게이트 아님).

    `12-EVAL` §6.3 의 처방은 고객 축 튜너블만 가리키므로 이 값으로 실패시키지 않는다.
    그래도 재는 이유: 상품 축이 흔들리면 `behavior` 워커의 세그먼트 서술이 매일 바뀐다.
    """
    import numpy as np

    def _jitter(source: list[dict], local_seed: int) -> list[dict]:
        rng = np.random.default_rng(local_seed)
        out = []
        for row in source:
            new = dict(row)
            for key in ("view", "cart", "checkout", "purchase", "visitors"):
                value = row.get(key)
                if value:
                    new[key] = max(
                        0, int(round(value * float(rng.uniform(1 - noise_pct, 1 + noise_pct))))
                    )
            out.append(new)
        return out

    def _labels(source: list[dict]) -> tuple[list[int], list]:
        clusters = segmentation.cluster_products(
            source,
            k_min=settings.seller_behavior_kmeans_k_min,
            k_max=settings.seller_behavior_kmeans_k_max,
            random_state=settings.seller_kmeans_random_state,
        )
        assignment = {
            product_id: index
            for index, cluster in enumerate(clusters)
            for product_id in cluster.product_ids
        }
        return [assignment.get(row["product_id"], -1) for row in source], clusters

    labels_a, clusters_a = _labels(_jitter(rows, seed + 1))
    labels_b, clusters_b = _labels(_jitter(rows, seed + 2))
    minimum = settings.seller_cluster_stability_min
    if not clusters_a or not clusters_b:
        return AriReport(
            axis="product",
            dataset_version=dataset_version,
            seed=seed,
            rows=len(rows),
            noise_pct=noise_pct,
            measured=False,
            ari=None,
            minimum=minimum,
            passed=False,
            clusters_a=len(clusters_a),
            clusters_b=len(clusters_b),
            note="군집 생략 — exploratory 라 게이트에는 영향이 없다",
        )

    from sklearn.metrics import adjusted_rand_score

    ari = float(adjusted_rand_score(labels_a, labels_b))
    return AriReport(
        axis="product",
        dataset_version=dataset_version,
        seed=seed,
        rows=len(rows),
        noise_pct=noise_pct,
        measured=True,
        ari=ari,
        minimum=minimum,
        passed=ari >= minimum,
        clusters_a=len(clusters_a),
        clusters_b=len(clusters_b),
        silhouette_a=clusters_a[0].silhouette,
        silhouette_b=clusters_b[0].silhouette,
        label_sizes_a={c.label: c.size for c in clusters_a},
        label_sizes_b={c.label: c.size for c in clusters_b},
        note="exploratory — 게이트 아님",
    )
