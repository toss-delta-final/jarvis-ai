"""고객 피처 스펙 상수 (이슈 #593, `03-FEATURES` 2부).

여기 값들은 **Settings 기본값의 출처**다. 런타임은 항상 `Settings` 쪽을 읽고(튜너블
하드코딩 금지), 이 모듈은 "코드가 아는 정본"으로 남아 부팅 검증이 둘을 대조한다 —
어긋나면 기동이 실패한다(`config.Settings._validate_cross_field`).

[키 표기 규약 — 블록마다 다르다]
`raw`·`centroid_stats` 는 **camelCase**, 그 외 계산 결과는 **snake_case** 다. 와이어
규약(CamelModel)과 무관한 DB JSONB 인데도 이렇게 가르는 이유: `raw` 는 I-38 응답 필드를
가공 없이 옮긴 것이라 이름이 같아야 원천을 되짚을 수 있고(`03` §4 예시·`04` §6.1 예시가
그렇게 적혀 있다), 나머지는 우리가 만든 값이라 파이썬 관행을 따른다.
"""

from __future__ import annotations

import math

# 스냅샷 각인용 버전 문자열 — 정의가 바뀌면 올린다. 다른 버전끼리는 비교를 보류한다
# (`04` §6.2 — 다른 정의로 만든 숫자를 비교하면 조용히 틀린 결과가 나온다).
FEATURE_SPEC_VERSION = "fe_v1"
# `seller_analysis_snapshots.source` — 어느 원천으로 만든 스냅샷인지.
SNAPSHOT_SOURCE = "i38_v1"

# ── raw 블록 (C01~C09) — I-38 응답 필드명 그대로 ────────────────────────────────
RAW_KEYS: tuple[str, ...] = (
    "sessions",
    "productViews",
    "cartAdds",
    "checkoutStarts",
    "orderCount",
    "cancelCount",
    "amountBucket",
    "lastActivityDaysAgo",
    "firstSeenDaysAgo",
)

# ── vector 블록 — K-Means 입력 12개. **이 순서가 곧 벡터 차원 순서다** ──────────
# 카운트 5축은 활동량의 크기를, 비율 3축은 깔때기의 형태를, RFM 3축은 가치를,
# views_per_session 은 탐색 스타일을 담는다(`03` §3.1).
CLUSTER_INPUT_KEYS: tuple[str, ...] = (
    "log_sessions",
    "log_product_views",
    "log_cart_adds",
    "log_checkout_starts",
    "log_order_count",
    "amount_log",
    "recency_score",
    "log_tenure",
    "cart_rate",
    "checkout_rate",
    "order_rate",
    "views_per_session",
)

# ── 축군 (`04` §1.2) — 각 군의 총 영향력을 1 로 맞춘다 ──────────────────────────
# 유클리드 거리는 제곱합이라 열마다 w 를 곱하면 기여가 w² 가 된다. w = 1/√n 이면
# n 개 열의 기여 합이 n × (1/n) = 1 이 되어 축군이 정확히 1인분씩 작용한다.
CLUSTER_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "activity": (
        "log_sessions",
        "log_product_views",
        "log_cart_adds",
        "log_checkout_starts",
        "log_order_count",
    ),
    "funnel": ("cart_rate", "checkout_rate", "order_rate"),
    "value": ("amount_log", "recency_score", "log_tenure"),
    "explore": ("views_per_session",),
}

DEFAULT_CLUSTER_GROUP_WEIGHTS: dict[str, float] = {
    name: 1.0 / math.sqrt(len(keys)) for name, keys in CLUSTER_GROUP_KEYS.items()
}

# ── flags 블록 (C19~C23·C25) ──────────────────────────────────────────────────
FLAG_KEYS: tuple[str, ...] = (
    "is_cart_abandoner",
    "is_checkout_dropper",
    "is_viewer_only",
    "is_new",
    "is_returning",
    "has_cancelled",
)

# ── rfm 블록 (C50~C53, 결정 44) — 저장 전용. 군집 입력에 넣지 않는다 ───────────
# 같은 정보가 이미 연속 3축(recency_score·log_order_count·amount_log)으로 들어가 있어
# 중복이고, 등급은 계단 함수라 유클리드 거리에서 왜곡을 만든다.
RFM_KEYS: tuple[str, ...] = ("rfm_r", "rfm_f", "rfm_m", "rfm_score")
RFM_BINS = 5

# ── 금액 구간 → 로그 중앙값 (`03` §2.3) ────────────────────────────────────────
# 서수(0~5)를 그대로 쓰면 등간격 가정이 깨진다 — 1→2 와 4→5 의 실제 금액 차이가
# 자릿수로 다르다. 구간 대표값의 ln(1+x) 로 옮겨 거리 계산이 금액 규모를 반영하게 한다.
# 대표값(원)은 jarvis-back `SellerAnalyticsService.amountBucket` 의 경계와 정합한다:
#   <=0 ZERO / <10,000 / <50,000 / <100,000 / <300,000 / else
AMOUNT_BUCKET_REPRESENTATIVE: dict[str, int] = {
    "ZERO": 0,
    "LT_10K": 5_000,
    "10K_50K": 30_000,
    "50K_100K": 75_000,
    "100K_300K": 200_000,
    "GTE_300K": 500_000,
}
AMOUNT_BUCKET_MAP: dict[str, float] = {
    bucket: math.log1p(amount) for bucket, amount in AMOUNT_BUCKET_REPRESENTATIVE.items()
}
# 서수 순서 — jarvis-back `SellerCustomerFeaturesResponse.AMOUNT_BUCKETS` 와 **순서까지**
# 같아야 한다. 응답이 이 배열을 그대로 에코하므로 런타임에 대조한다(`customer.py`).
AMOUNT_BUCKET_ORDER: tuple[str, ...] = tuple(AMOUNT_BUCKET_REPRESENTATIVE)

# ── rule_label 어휘 (`04` §4.2) — 판정 순서가 계약이다 ─────────────────────────
LABEL_DORMANT = "휴면형"
LABEL_AT_RISK = "이탈위험형"
LABEL_HESITANT = "구매망설임형"
LABEL_LOYAL = "충성형"
LABEL_EXPLORER = "탐색형"
# 소규모 군집(재식별 위험 + 소표본 평균 불안정)에 붙는 라벨 — 보고서 세그먼트에서 빠진다.
LABEL_SMALL = "기타"

RULE_LABELS: tuple[str, ...] = (
    LABEL_DORMANT,
    LABEL_AT_RISK,
    LABEL_HESITANT,
    LABEL_LOYAL,
    LABEL_EXPLORER,
    LABEL_SMALL,
)

# 라벨 판정 임계(백분위) — 전부 Settings 로 주입되고 이 표는 기본값이다.
DEFAULT_LABEL_THRESHOLDS: dict[str, float] = {
    "dormant_recency_max": 25.0,
    "dormant_orders_max": 50.0,
    "at_risk_recency_max": 50.0,
    "at_risk_orders_min": 50.0,
    "hesitant_carts_min": 50.0,
    "hesitant_order_rate_max": 25.0,
    "loyal_orders_min": 75.0,
    "loyal_amount_min": 75.0,
    "loyal_recency_min": 50.0,
}

# ── centroid_stats 축 (`04` §6.1) ─────────────────────────────────────────────
# **PCA·스케일링 이전 원 피처 평균**이다. 키는 원천 블록의 이름을 그대로 써서, 보고서가
# 인용할 때 `feature_rows` 의 어느 값의 평균인지 되짚을 수 있게 한다.
CENTROID_RAW_KEYS: tuple[str, ...] = (
    "sessions",
    "productViews",
    "cartAdds",
    "checkoutStarts",
    "orderCount",
    "cancelCount",
    "lastActivityDaysAgo",
    "firstSeenDaysAgo",
)
CENTROID_DERIVED_KEYS: tuple[str, ...] = (
    "cart_rate_raw",
    "checkout_rate_raw",
    "order_rate_raw",
    "views_per_session_raw",
)
# amountBucket 은 문자열이라 평균이 정의되지 않는다 — 서수로 옮긴 값을 따로 싣는다.
CENTROID_AMOUNT_KEY = "amountOrdinal"
