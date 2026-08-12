"""I-38 rows → 고객 피처 27개 (이슈 #593, `03-FEATURES` 2부 §3~§4).

저장 27개 / 군집 입력 12개를 **분리**한다. "최대한 많은 피처"와 "군집이 활동량 축
하나로 갈리지 않게"는 충돌하는 요구인데, 저장은 전량 / 입력은 선별로 두면 둘 다
만족한다(`03` §1).

27 = 23(C01~C26 중 C13·C17·C24 제외) + RFM 등급 4종(C50~C53, 결정 44).
그중 12개만 `spec.CLUSTER_INPUT_KEYS` 순서로 벡터가 된다.

LLM 0회 · I/O 0회 — 순수 계산이라 테스트는 fixture rows 를 넣고 결과를 비교하면 끝난다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from app.agents.seller.features import spec, transforms
from app.agents.seller.sop.context import Hold
from app.core.config import Settings
from app.schemas.spring import SellerCustomerFeatureRow, SellerCustomerFeaturesResult


@dataclass(frozen=True)
class CustomerFeatureSet:
    """`build_customer_features` 산출 — 저장용 행 + 군집용 행렬 + 각인 재료.

    `matrix` 는 `spec.CLUSTER_INPUT_KEYS` 순서의 N×12 실수 행렬이다. 매핑에 없는
    `amountBucket` 때문에 `nan` 이 들어갈 수 있고, 그 대체(관측치 평균)는 군집 층이
    맡는다 — 저장되는 `rows` 의 `vector` 블록에는 **None 그대로** 남는다(0 위장 금지).
    """

    rows: list[dict]
    matrix: list[list[float]]
    prior_stats: dict[str, float]
    rfm_bins_actual: dict[str, int]
    period_days: int
    holds: list[Hold] = field(default_factory=list)


def _prior(numerators: list[int], denominators: list[int]) -> float:
    """shrinkage 사전확률 — 같은 스냅샷 내 브랜드 전체 비율(Σnumer / Σdenom).

    외부 의존이 없다(`03` §7). 브랜드 전체 분모가 0 이면 0.0 — "값을 못 만든 것"이
    아니라 그 단계가 브랜드 전체에서 한 번도 일어나지 않았다는 실측이다.
    """
    total_denominator = sum(denominators)
    if not total_denominator:
        return 0.0
    return sum(numerators) / total_denominator


def _check_amount_buckets(
    result: SellerCustomerFeaturesResult, mapping: dict[str, float]
) -> list[Hold]:
    """응답이 에코한 `amountBuckets` 배열과 매핑표를 **런타임에** 대조한다.

    부팅 검증은 상수끼리(`Settings` ↔ `spec`)만 볼 수 있다 — 응답 배열은 런타임에만
    존재하기 때문이다. BE 가 구간을 늘리면 여기서 드러난다. 배치를 죽이지 않고 `Hold`
    로 남기는 이유: 한 브랜드의 스냅샷이 통째로 사라지는 것보다, 금액 축만 보류한
    스냅샷이 남고 판매자에게 한계가 드러나는 편이 낫다.
    """
    holds: list[Hold] = []
    echoed = tuple(result.amount_buckets)
    if echoed and echoed != tuple(mapping):
        holds.append(
            Hold(
                step="load",
                reason=(
                    "amount_bucket_drift: 응답 amountBuckets 가 매핑표와 다르다"
                    f" (응답={list(echoed)}, 매핑={list(mapping)})"
                ),
            )
        )
    unknown = sorted({row.amount_bucket for row in result.rows} - set(mapping))
    if unknown:
        holds.append(
            Hold(
                step="compute",
                reason=f"amount_bucket_drift: 매핑 없는 구간 {unknown} — 해당 고객 금액 축 보류",
            )
        )
    return holds


def _build_row(
    row: SellerCustomerFeatureRow,
    *,
    priors: dict[str, float],
    alpha: float,
    amount_map: dict[str, float],
    period_days: int,
) -> dict:
    """고객 1명 → 6블록 JSON. 블록 경계가 곧 용도 경계다(`03` §4)."""
    amount_log = transforms.amount_bucket_to_log(row.amount_bucket, amount_map)

    cart_rate_raw = transforms.safe_ratio(row.cart_adds, row.product_views)
    checkout_rate_raw = transforms.safe_ratio(row.checkout_starts, row.cart_adds)
    order_rate_raw = transforms.safe_ratio(row.order_count, row.checkout_starts)
    views_per_session_raw = transforms.safe_ratio(row.product_views, row.sessions)

    return {
        "customerLabel": row.customer_label,
        # 보고서가 인용하는 유일한 소스 — 변환값·주성분은 인용 금지(`04` §2.4).
        "raw": {
            "sessions": row.sessions,
            "productViews": row.product_views,
            "cartAdds": row.cart_adds,
            "checkoutStarts": row.checkout_starts,
            "orderCount": row.order_count,
            "cancelCount": row.cancel_count,
            "amountBucket": row.amount_bucket,
            "lastActivityDaysAgo": row.last_activity_days_ago,
            "firstSeenDaysAgo": row.first_seen_days_ago,
        },
        # 군집 입력. 키 순서가 곧 벡터 차원 순서이며 Settings 목록과 일치해야 한다.
        "vector": {
            "log_sessions": transforms.log1p(row.sessions),
            "log_product_views": transforms.log1p(row.product_views),
            "log_cart_adds": transforms.log1p(row.cart_adds),
            "log_checkout_starts": transforms.log1p(row.checkout_starts),
            "log_order_count": transforms.log1p(row.order_count),
            "amount_log": amount_log,
            "recency_score": transforms.recency_score(row.last_activity_days_ago),
            "log_tenure": transforms.log1p(row.first_seen_days_ago),
            "cart_rate": transforms.shrinkage(
                row.cart_adds, row.product_views, prior=priors["cart_rate"], alpha=alpha
            ),
            "checkout_rate": transforms.shrinkage(
                row.checkout_starts, row.cart_adds, prior=priors["checkout_rate"], alpha=alpha
            ),
            "order_rate": transforms.shrinkage(
                row.order_count, row.checkout_starts, prior=priors["order_rate"], alpha=alpha
            ),
            # 비율의 log1p — 세션당 탐색량은 카운트처럼 롱테일이라 눌러야 한다.
            "views_per_session": transforms.log1p(views_per_session_raw or 0.0),
        },
        # 원값 + 표본 크기. 보고서 인용 가드(`seller_feature_min_denom`)의 근거다.
        "derived": {
            "cart_rate_raw": cart_rate_raw,
            "cart_rate_denom": row.product_views,
            "checkout_rate_raw": checkout_rate_raw,
            "checkout_rate_denom": row.cart_adds,
            "order_rate_raw": order_rate_raw,
            "order_rate_denom": row.checkout_starts,
            "views_per_session_raw": views_per_session_raw,
            "carts_per_session": transforms.safe_ratio(row.cart_adds, row.sessions),
            "orders_per_session": transforms.safe_ratio(row.order_count, row.sessions),
            "visit_frequency": row.sessions / max(row.first_seen_days_ago, 1),
        },
        # 이진은 유클리드 거리에 넣으면 군집 경계를 흐린다 — 대신 `flag_ratios` 로
        # "이 군집의 78% 가 장바구니 이탈자"라고 쓴다(`03` §3.2).
        "flags": {
            "is_cart_abandoner": row.cart_adds > 0 and row.order_count == 0,
            "is_checkout_dropper": row.checkout_starts > 0 and row.order_count == 0,
            "is_viewer_only": row.product_views > 0 and row.cart_adds == 0,
            "is_new": row.first_seen_days_ago <= period_days,
            "is_returning": row.sessions > 1,
            "has_cancelled": row.cancel_count > 0,
        },
        # 오분위는 전 고객이 모여야 정해진다 — 아래에서 한 번에 채운다.
        "rfm": {},
        # C30~C32(I-14 조인)는 abuse 워커와 함께 후속이다 — 자리만 둔다(`03` §3.3).
        "join": {},
        "cluster_id": None,
        "rule_label": "",
        # DBSCAN 은 후속으로 연기됐다(`04` §5) — 마이그레이션 없이 자리만 둔다.
        "is_outlier": False,
    }


def _fill_rfm(rows: list[dict], amount_ordinal: list[int]) -> dict[str, int]:
    """RFM 등급 4종을 각 행에 채우고 **실제 구간 수**를 돌려준다(`03` §2.6).

    R 은 `recency_score`(클수록 최근), F 는 `orderCount`, M 은 `amountBucket` 서수다.
    등급은 세그먼트 프로파일과 보고서 인용에만 쓰고 군집 입력에는 넣지 않는다.
    """
    recency = [row["vector"]["recency_score"] for row in rows]
    orders = [float(row["raw"]["orderCount"]) for row in rows]
    amounts = [float(value) for value in amount_ordinal]

    r_ranks, r_bins = transforms.quintile_ranks(recency, bins=spec.RFM_BINS)
    f_ranks, f_bins = transforms.quintile_ranks(orders, bins=spec.RFM_BINS)
    m_ranks, m_bins = transforms.quintile_ranks(amounts, bins=spec.RFM_BINS)

    for row, r_rank, f_rank, m_rank in zip(rows, r_ranks, f_ranks, m_ranks, strict=True):
        row["rfm"] = {
            "rfm_r": r_rank,
            "rfm_f": f_rank,
            "rfm_m": m_rank,
            "rfm_score": f"{r_rank}{f_rank}{m_rank}",
        }
    return {"r": r_bins, "f": f_bins, "m": m_bins}


def build_customer_features(
    result: SellerCustomerFeaturesResult,
    *,
    period_from: date,
    period_to: date,
    settings: Settings,
) -> CustomerFeatureSet:
    """I-38 응답 → (`feature_rows`, 벡터 행렬, prior, RFM 구간 수). LLM 0회, 순수 계산.

    `insufficientCohort=true` 면 BE 가 `rows=[]` 를 준다 — "고객 없음"이 아니라 표본
    부족이므로 빈 결과 + `Hold` 로 남기고, 스냅샷 자체는 만든다(조립부 소관).
    행 순서는 I-38 응답 순서(활동량 내림차순)를 그대로 보존한다 — 절단 편향을 보고서가
    되짚을 수 있어야 한다.
    """
    amount_map = dict(settings.seller_amount_bucket_map)
    holds = _check_amount_buckets(result, amount_map)

    if result.insufficient_cohort:
        holds.append(
            Hold(
                step="load",
                reason=(
                    "insufficient_cohort: 코호트 표본 부족으로 고객 축 판정 보류"
                    f" (totalCustomers={result.total_customers})"
                ),
            )
        )
    if result.truncated:
        holds.append(
            Hold(
                step="load",
                reason=(
                    "truncated: 활동량 상위 절단으로 저활동 고객이 빠져 있다"
                    f" (totalCustomers={result.total_customers}, rowLimit={result.row_limit})"
                ),
            )
        )

    source_rows = list(result.rows)
    period_days = (period_to - period_from).days + 1
    if not source_rows:
        return CustomerFeatureSet(
            rows=[],
            matrix=[],
            prior_stats={},
            rfm_bins_actual={},
            period_days=period_days,
            holds=holds,
        )

    priors = {
        "cart_rate": _prior(
            [row.cart_adds for row in source_rows], [row.product_views for row in source_rows]
        ),
        "checkout_rate": _prior(
            [row.checkout_starts for row in source_rows], [row.cart_adds for row in source_rows]
        ),
        "order_rate": _prior(
            [row.order_count for row in source_rows], [row.checkout_starts for row in source_rows]
        ),
    }
    alpha = settings.seller_feature_shrinkage_alpha

    rows = [
        _build_row(row, priors=priors, alpha=alpha, amount_map=amount_map, period_days=period_days)
        for row in source_rows
    ]

    # 매핑에 없는 구간은 서수도 정의되지 않는다 — 최하위(0)로 뭉개면 "0원 구매"와
    # 구분이 사라지므로 M 등급 계산에서는 중앙값 자리(len//2)를 쓰고 Hold 로 알린다.
    fallback_ordinal = len(spec.AMOUNT_BUCKET_ORDER) // 2
    ordinal_index = {bucket: i for i, bucket in enumerate(spec.AMOUNT_BUCKET_ORDER)}
    amount_ordinal = [ordinal_index.get(row.amount_bucket, fallback_ordinal) for row in source_rows]
    rfm_bins_actual = _fill_rfm(rows, amount_ordinal)

    matrix = [
        [
            math.nan if (value := row["vector"][key]) is None else float(value)
            for key in spec.CLUSTER_INPUT_KEYS
        ]
        for row in rows
    ]

    return CustomerFeatureSet(
        rows=rows,
        matrix=matrix,
        prior_stats=priors,
        rfm_bins_actual=rfm_bins_actual,
        period_days=period_days,
        holds=holds,
    )
