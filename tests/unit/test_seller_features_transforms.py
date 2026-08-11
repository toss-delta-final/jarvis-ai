"""features/transforms.py — 고객 피처 변환 순수 함수 테스트 (이슈 #593).

경계(0·분모 0·음수)와 결측 규약(None ≠ 0.0), 오분위의 동점·치우침 처리를 본다.
"""

from __future__ import annotations

import math

import pytest

from app.agents.seller.features import spec, transforms


def test_log1p_preserves_zero_and_clamps_negative() -> None:
    """x=0 → 0 (결측 위장 아님). 음수는 0 으로 막아 NaN 전파를 차단한다."""
    assert transforms.log1p(0) == 0.0
    assert transforms.log1p(1) == pytest.approx(math.log(2))
    assert transforms.log1p(-5) == 0.0


def test_recency_score_is_monotone_decreasing() -> None:
    """오늘 활동이 1.0 에 가장 가깝고 경과일이 늘수록 0 으로 수렴한다."""
    assert transforms.recency_score(0) == 1.0
    assert transforms.recency_score(1) == pytest.approx(0.5)
    assert transforms.recency_score(9) > transforms.recency_score(99)


def test_amount_bucket_unknown_is_none_not_zero() -> None:
    """미등록 구간은 None — 0.0 은 ZERO 구간의 정당한 값이라 구분이 불가능해진다."""
    mapping = spec.AMOUNT_BUCKET_MAP
    assert transforms.amount_bucket_to_log("ZERO", mapping) == 0.0
    assert transforms.amount_bucket_to_log("GTE_1M", mapping) is None


def test_amount_bucket_map_matches_backend_order() -> None:
    """jarvis-back `SellerCustomerFeaturesResponse.AMOUNT_BUCKETS` 실측과 순서까지 같다."""
    assert spec.AMOUNT_BUCKET_ORDER == (
        "ZERO",
        "LT_10K",
        "10K_50K",
        "50K_100K",
        "100K_300K",
        "GTE_300K",
    )
    # 단조 증가여야 서수 대신 쓰는 의미가 있다(등간격 가정을 깨는 것이 목적).
    values = list(spec.AMOUNT_BUCKET_MAP.values())
    assert values == sorted(values)


def test_safe_ratio_distinguishes_missing_from_zero() -> None:
    assert transforms.safe_ratio(0, 10) == 0.0
    assert transforms.safe_ratio(3, 0) is None


def test_shrinkage_pulls_small_samples_toward_prior() -> None:
    """조회 1번·담기 1번(원값 100%)이 브랜드 평균 쪽으로 끌려온다."""
    smoothed = transforms.shrinkage(1, 1, prior=0.2, alpha=5.0)
    assert smoothed == pytest.approx((1 + 5 * 0.2) / (1 + 5))
    assert smoothed < 1.0

    # 표본이 크면 원값에 수렴한다 — 평활이 큰 고객의 실측을 덮지 않는다.
    large = transforms.shrinkage(500, 1000, prior=0.2, alpha=5.0)
    assert large == pytest.approx(0.5, abs=0.01)

    # 분모 0 이면 정확히 prior — 결측 표현은 `_raw`/`_denom` 이 따로 맡는다.
    assert transforms.shrinkage(0, 0, prior=0.3, alpha=5.0) == pytest.approx(0.3)


def test_shrinkage_rejects_non_positive_alpha() -> None:
    with pytest.raises(ValueError):
        transforms.shrinkage(1, 2, prior=0.5, alpha=0.0)


def test_percentile_of_is_weak_rank() -> None:
    ordered = [1.0, 2.0, 3.0, 4.0]
    assert transforms.percentile_of(ordered, 0.5) == 0.0
    assert transforms.percentile_of(ordered, 2.0) == 50.0
    assert transforms.percentile_of(ordered, 4.0) == 100.0
    # 빈 분포는 판정 불가 — 중립값(방어 경로).
    assert transforms.percentile_of([], 1.0) == 50.0


def test_quintile_ranks_spread_and_bins_actual() -> None:
    """고르게 퍼진 분포는 5구간 전부 나온다."""
    ranks, bins_actual = transforms.quintile_ranks([float(v) for v in range(100)])
    assert bins_actual == 5
    assert min(ranks) == 1 and max(ranks) == 5
    # 단조 — 큰 값이 낮은 등급을 받으면 안 된다.
    assert ranks == sorted(ranks)


def test_quintile_ranks_collapse_on_skewed_distribution() -> None:
    """주문 0회가 60% 인 브랜드는 F 를 5등분할 수 없다 — 나오는 만큼만 쓰고 각인한다.

    조용히 뭉개면 "F=3 인데 주문 0회" 같은 값이 나온다(`03` §2.6).
    """
    values = [0.0] * 60 + [1.0] * 20 + [5.0] * 20
    ranks, bins_actual = transforms.quintile_ranks(values)
    assert bins_actual < 5
    # 동점은 낮은 등급 쪽 — 0 인 60명은 전원 1 등급이어야 한다.
    assert set(ranks[:60]) == {1}
    assert ranks[-1] > ranks[0]


def test_quintile_ranks_handles_empty_and_rejects_small_bins() -> None:
    assert transforms.quintile_ranks([]) == ([], 0)
    with pytest.raises(ValueError):
        transforms.quintile_ranks([1.0, 2.0], bins=1)
