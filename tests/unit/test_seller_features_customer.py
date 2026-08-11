"""features/customer.py — I-38 rows → 피처 27개 테스트 (이슈 #593, `03-FEATURES` 2부).

6블록 구성 · 결측 규약(`_raw=null` + `_denom`) · 플래그 6종 · RFM 오분위 각인 ·
계약 드리프트(미등록 구간)·표본 부족·절단을 본다.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from app.agents.seller.features import spec
from app.agents.seller.features.customer import build_customer_features
from app.core.config import Settings
from tests.unit._seller_feature_fixtures import wire_result, wire_row

_FROM = date(2026, 7, 15)
_TO = date(2026, 8, 11)  # 28일 구간


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _build(rows: list[dict], settings: Settings, **result_overrides):
    return build_customer_features(
        wire_result(rows, **result_overrides),
        period_from=_FROM,
        period_to=_TO,
        settings=settings,
    )


def test_row_has_six_blocks_and_vector_matches_spec_order(settings: Settings) -> None:
    """저장 스키마는 6블록이고, `vector` 키 순서가 곧 행렬 차원 순서다."""
    result = _build([wire_row(1, productViews=10, cartAdds=4)], settings)
    row = result.rows[0]

    assert {"raw", "vector", "derived", "flags", "rfm", "join"} <= set(row)
    assert tuple(row["vector"]) == spec.CLUSTER_INPUT_KEYS
    assert tuple(row["raw"]) == spec.RAW_KEYS
    assert tuple(row["flags"]) == spec.FLAG_KEYS
    assert tuple(row["rfm"]) == spec.RFM_KEYS
    # C30~C32(I-14 조인)는 abuse 워커와 함께 후속 — v1 은 자리만 있다.
    assert row["join"] == {}
    assert result.matrix[0] == [row["vector"][key] for key in spec.CLUSTER_INPUT_KEYS]


def test_feature_count_is_23_plus_rfm_4(settings: Settings) -> None:
    """채택 23개(C01~C26 − C13·C17·C24) + RFM 등급 4종 = 27, 군집 입력은 그중 12개."""
    row = _build([wire_row(1)], settings).rows[0]
    stored = (
        set(row["raw"])  # C01~C09
        | {"cart_rate", "checkout_rate", "order_rate", "views_per_session"}  # C10~C12·C14
        | {"carts_per_session", "orders_per_session", "visit_frequency"}  # C15·C16·C18
        | set(row["flags"])  # C19~C23·C25
        | {"recency_score"}  # C26
    )
    assert len(stored) == 23
    assert len(row["rfm"]) == 4
    assert len(spec.CLUSTER_INPUT_KEYS) == 12


def test_zero_denominator_ratio_is_null_not_zero(settings: Settings) -> None:
    """분모 0 은 결측이다 — 0.0 으로 위장하면 "관측 없음"이 "전환율 0%"가 된다."""
    rows = [wire_row(1, productViews=0, cartAdds=0, checkoutStarts=0, sessions=0)]
    row = _build(rows, settings).rows[0]

    assert row["derived"]["cart_rate_raw"] is None
    assert row["derived"]["cart_rate_denom"] == 0
    assert row["derived"]["views_per_session_raw"] is None
    # 평활값(군집 입력)은 별개로 항상 존재한다 — 원값과 분리 저장하는 이유.
    assert row["vector"]["cart_rate"] is not None
    assert row["vector"]["views_per_session"] == 0.0


def test_denominator_is_recorded_for_report_guard(settings: Settings) -> None:
    """`_denom` 은 보고서 인용 가드(`seller_feature_min_denom`)의 근거다."""
    row = _build(
        [wire_row(1, productViews=35, cartAdds=8, checkoutStarts=3, orderCount=2)], settings
    ).rows[0]
    assert row["derived"]["cart_rate_denom"] == 35
    assert row["derived"]["checkout_rate_denom"] == 8
    assert row["derived"]["order_rate_denom"] == 3
    assert row["derived"]["cart_rate_raw"] == pytest.approx(8 / 35)


def test_flags_capture_funnel_dropouts(settings: Settings) -> None:
    """이진 플래그 6종 — 군집 입력에서 빼는 대신 세그먼트 프로파일로 쓴다."""
    abandoner = _build(
        [wire_row(1, productViews=20, cartAdds=5, checkoutStarts=0, orderCount=0, sessions=3)],
        settings,
    ).rows[0]["flags"]
    assert abandoner["is_cart_abandoner"] is True
    assert abandoner["is_checkout_dropper"] is False
    assert abandoner["is_viewer_only"] is False
    assert abandoner["is_returning"] is True

    dropper = _build(
        [wire_row(1, productViews=20, cartAdds=5, checkoutStarts=2, orderCount=0)], settings
    ).rows[0]["flags"]
    assert dropper["is_checkout_dropper"] is True

    viewer = _build([wire_row(1, productViews=9, cartAdds=0, cancelCount=2)], settings).rows[0][
        "flags"
    ]
    assert viewer["is_viewer_only"] is True
    assert viewer["has_cancelled"] is True


def test_is_new_uses_inclusive_period_length(settings: Settings) -> None:
    """기간 내 첫 접촉이면 신규 — 경계(구간 길이와 같은 날)는 포함이다."""
    period_days = (_TO - _FROM).days + 1
    inside = _build([wire_row(1, firstSeenDaysAgo=period_days)], settings).rows[0]
    outside = _build([wire_row(1, firstSeenDaysAgo=period_days + 1)], settings).rows[0]
    assert inside["flags"]["is_new"] is True
    assert outside["flags"]["is_new"] is False


def test_rfm_grades_and_bins_actual_are_recorded(settings: Settings) -> None:
    """치우친 분포에서는 5구간이 안 나온다 — 나온 만큼만 쓰고 실제 구간 수를 각인한다."""
    rows = [wire_row(i, orderCount=0) for i in range(1, 61)]
    rows += [
        wire_row(i, orderCount=7, amountBucket="GTE_300K", lastActivityDaysAgo=1)
        for i in range(61, 81)
    ]
    result = _build(rows, settings)

    assert set(result.rfm_bins_actual) == {"r", "f", "m"}
    assert result.rfm_bins_actual["f"] < 5  # 주문 0회가 75% — 5등분 불가
    grades = result.rows[0]["rfm"]
    assert grades["rfm_score"] == f"{grades['rfm_r']}{grades['rfm_f']}{grades['rfm_m']}"
    # 최근·다구매·고액 고객이 더 높은 등급을 받는다.
    assert result.rows[-1]["rfm"]["rfm_f"] > result.rows[0]["rfm"]["rfm_f"]


def test_unknown_amount_bucket_holds_instead_of_failing(settings: Settings) -> None:
    """BE 가 구간을 늘리면 배치를 죽이지 않고 금액 축만 보류한다(확정 결정 5)."""
    result = _build([wire_row(1, amountBucket="GTE_1M")], settings)

    assert result.rows[0]["vector"]["amount_log"] is None
    # 행렬에서는 NaN 이다 — 대체(관측치 평균)는 군집 층이 하고 저장값은 None 그대로다.
    assert math.isnan(result.matrix[0][spec.CLUSTER_INPUT_KEYS.index("amount_log")])
    reasons = " ".join(hold.reason for hold in result.holds)
    assert "amount_bucket_drift" in reasons


def test_echoed_amount_buckets_are_compared_at_runtime(settings: Settings) -> None:
    """응답 배열과의 대조는 런타임이다 — 부팅 시점에는 응답이 존재하지 않는다."""
    result = _build([wire_row(1)], settings, amountBuckets=["ZERO", "GTE_300K"])
    assert any("amount_bucket_drift" in hold.reason for hold in result.holds)


def test_insufficient_cohort_yields_empty_rows_with_hold(settings: Settings) -> None:
    """표본 부족은 "고객 없음"이 아니다 — 빈 결과 + Hold 로 남긴다(노션 규약)."""
    result = _build([], settings, insufficientCohort=True, totalCustomers=12)
    assert result.rows == []
    assert result.matrix == []
    assert any("insufficient_cohort" in hold.reason for hold in result.holds)


def test_truncated_flag_raises_hold_for_selection_bias(settings: Settings) -> None:
    """활동량 상위 절단은 저활동 고객을 통째로 빼므로 세그먼트 크기 인용에 한계가 붙는다."""
    result = _build([wire_row(1)], settings, truncated=True, totalCustomers=4321)
    assert any("truncated" in hold.reason for hold in result.holds)


def test_priors_come_from_the_same_snapshot(settings: Settings) -> None:
    """평활 사전확률은 브랜드 전체 비율 — 같은 스냅샷 안에서 계산해 외부 의존이 없다."""
    rows = [wire_row(1, productViews=100, cartAdds=10), wire_row(2, productViews=100, cartAdds=30)]
    result = _build(rows, settings)
    assert result.prior_stats["cart_rate"] == pytest.approx(40 / 200)
    # 분모가 전무하면 0.0 — 값을 못 만든 게 아니라 그 단계가 한 번도 없었다는 실측이다.
    empty = _build([wire_row(1, productViews=0, cartAdds=0)], settings)
    assert empty.prior_stats["cart_rate"] == 0.0


def test_row_order_follows_response_order(settings: Settings) -> None:
    """I-38 응답 순서(활동량 내림차순)를 보존한다 — 절단 편향을 되짚을 수 있어야 한다."""
    rows = [wire_row(3), wire_row(1), wire_row(2)]
    result = _build(rows, settings)
    assert [row["customerLabel"] for row in result.rows] == ["L0003", "L0001", "L0002"]
