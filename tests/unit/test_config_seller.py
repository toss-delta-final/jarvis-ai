"""판매자 Settings 신규 필드 테스트 (DESIGN-SELLER-TOOLS-STAGE1 §5).

임계값·타임아웃을 코드 하드코딩이 아니라 Settings 로 주입하는지 확인한다.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.agents.seller.features import spec
from app.core.config import Settings


def test_seller_settings_defaults() -> None:
    """§5 표의 판매자 임계값 기본값이 그대로 로드된다 (env 미설정 시)."""
    settings = Settings(_env_file=None)
    # [통일 2026-07-20] 서비스 토큰은 팀 규약 internal_api_token 단일 키(기본 미설정).
    assert settings.internal_api_token == ""
    # [#290] 구 임계 튜너블(seller_ma_*·seller_anomaly_deviation_pct·
    # seller_conversion_drop_pct)은 논문 기반 교체로 폐기 — 대체 튜너블은
    # test_seller_analysis_defaults 가 검증한다.
    assert settings.seller_churn_inactive_days == 30
    assert settings.seller_recent_days_default == 7
    assert settings.seller_calc_max_result_digits == 100
    assert settings.seller_report_score_threshold == 21
    assert settings.seller_report_max_retries == 3
    assert settings.seller_draft_ttl_minutes == 10
    # [이슈 #659] 상품 변경 시 저성과(최근 N일 판매량) 참고 문구 임계값.
    assert settings.seller_low_sales_alert_enabled is True
    assert settings.seller_low_sales_window_days == 7
    assert settings.seller_low_sales_quantity_threshold == 3
    assert settings.seller_history_recent_n == 5
    assert settings.seller_tool_call_limit == 8
    # [#196] I-13 상품별 rows 상한 — I-14 용 max_events(5)와 분리 신설.
    assert settings.seller_summary_max_events == 5
    assert settings.seller_summary_max_products == 10
    # [#481] I-8 브랜드 스코프 전환(2026-08-06)으로 #197 보류 사유 해소 — 기본 활성.
    # 플래그는 운영 킬스위치로만 남는다.
    assert settings.seller_account_events_enabled is True
    # [#197 PR 리뷰] I-16 이탈 회원 나열 상한 — I-14 용 max_events 와 분리(결합 방지).
    assert settings.seller_churn_member_max == 5
    # [#297] I-29 주문·I-31 리뷰 나열 상한 — 기존 상한들과 분리 신설(결합 방지).
    assert settings.seller_summary_max_orders == 10
    assert settings.seller_summary_max_reviews == 10
    # ── 브랜치 분석 검증(이슈 #242, DESIGN-ANALYSIS-V31-242 §9) ──
    assert settings.seller_worker_max_retries == 1
    assert settings.seller_analysis_score_threshold == 21
    assert settings.seller_analysis_judge_timeout_s == 20.0
    assert settings.seller_branch_deadline_s == 160.0


def test_seller_low_sales_alert_bounds_fail_fast() -> None:
    """[이슈 #659] 조회 기간은 1일 이상, 임계는 음수 불가 — 둘 다 기동 시점에 걸린다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_low_sales_window_days=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_low_sales_quantity_threshold=-1)
    # 경계는 유효하다 — 임계 0 은 "판매량 0 일 때만 경고"라는 유효한 설정이다.
    assert (
        Settings(
            _env_file=None, seller_low_sales_quantity_threshold=0
        ).seller_low_sales_quantity_threshold
        == 0
    )


def test_seller_period_max_days_bounds_fail_fast() -> None:
    """[#269 리뷰] 기간 상한 자체가 date 연산 한계를 넘게 설정되면 기동 시점에 실패한다.

    약 74만일부터 `today - timedelta(days=n)` 이 date.min 을 넘어 OverflowError 를 낸다.
    호출부는 except ValueError 만 잡으므로, 그렇게 설정되면 이 이슈가 막으려던
    "되묻기 대신 에러 경로" 가 그대로 재현된다. 상한의 상한을 10년으로 묶는다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_period_max_days=7_310_000)  # 자릿수 오타 상정
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_period_max_days=0)
    # 경계는 유효하다.
    assert Settings(_env_file=None, seller_period_max_days=3653).seller_period_max_days == 3653


def test_seller_recent_default_within_period_max() -> None:
    """[#269 리뷰] 기본 일수가 상한을 넘으면 기동 시점에 실패한다.

    normalize_period 는 기간 미지정("최근")일 때 n=recent_default_days 로 두고 곧바로
    n>max_days 검사를 통과시킨다. 상한을 기본값보다 낮추면 가장 흔한 발화조차 매번
    "기간이 너무 깁니다" 되묻기로 빠지는데, 현재 기본값(7 <= 731)에선 안 드러난다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_recent_days_default=30, seller_period_max_days=7)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_recent_days_default=0)
    # 경계(default == max)는 유효하다.
    ok = Settings(_env_file=None, seller_recent_days_default=7, seller_period_max_days=7)
    assert ok.seller_recent_days_default == 7


def test_seller_analysis_defaults() -> None:
    """[#290] 분석 계산 층 튜너블 기본값 — 근거는 docs/worker-papers.md 논문 권장값."""
    settings = Settings(_env_file=None)
    assert settings.seller_stl_period == 7
    assert settings.seller_gesd_alpha == 0.05
    assert settings.seller_gesd_max_anomalies_ratio == 0.2
    assert settings.seller_analysis_lookback_days == 28
    assert settings.seller_min_history_for_stl == 14
    assert settings.seller_rate_test_alpha == 0.05
    assert settings.seller_wilson_confidence == 0.95
    assert settings.seller_mad_threshold == 3.5
    assert settings.seller_tukey_k == 1.5
    assert settings.seller_night_hours_start == 0
    assert settings.seller_night_hours_end == 6
    assert settings.seller_behavior_kmeans_k_min == 2
    assert settings.seller_behavior_kmeans_k_max == 5
    assert settings.seller_kmeans_random_state == 42
    assert settings.seller_churn_signal_top_k == 3


def test_seller_analysis_stl_relations_fail_fast() -> None:
    """[#290] STL 관계 오설정은 기동 시점에 실패한다.

    min_history < 2×period 면 폴백 경계를 통과한 입력이 STL 내부에서 죽고,
    lookback < min_history 면 확장 조회 비용만 내고 STL 은 영영 폴백이다(무음 무효화).
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_stl_period=1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_min_history_for_stl=13)  # < 2×7
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_analysis_lookback_days=10)  # < min_history 14
    # 경계(2×period == min_history == lookback)는 유효하다.
    ok = Settings(
        _env_file=None,
        seller_stl_period=7,
        seller_min_history_for_stl=14,
        seller_analysis_lookback_days=14,
    )
    assert ok.seller_analysis_lookback_days == 14


def test_seller_analysis_statistical_params_fail_fast() -> None:
    """[#290] 통계 파라미터 구간 오설정은 기동 시점에 실패한다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_gesd_alpha=0.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_rate_test_alpha=1.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_wilson_confidence=1.0)
    # GESD 는 이상점 수 < 표본 절반 전제 — 0.49 초과는 검정 전제 붕괴(S-H-ESD §3).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_gesd_max_anomalies_ratio=0.5)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_mad_threshold=0.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_tukey_k=-1.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_night_hours_start=6, seller_night_hours_end=6)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_behavior_kmeans_k_min=6)  # > k_max 5
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_behavior_kmeans_k_min=1)  # 군집 최소 2
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_churn_signal_top_k=0)
    # 경계 유효값 — ratio 상한 0.49, 심야 [0, 24).
    ok = Settings(
        _env_file=None,
        seller_gesd_max_anomalies_ratio=0.49,
        seller_night_hours_start=22,
        seller_night_hours_end=24,
    )
    assert ok.seller_gesd_max_anomalies_ratio == 0.49


def test_seller_analysis_types_proxy_basis() -> None:
    """[#290] 프록시 근사 규약 — basis 필드가 원천을 지목하고 결과는 불변(frozen)이다."""
    from app.agents.seller.analysis.types import ProxyValue, RateEstimate

    proxy = ProxyValue(name="recency_days", value=12.0, basis="proxy:last_activity_at")
    assert proxy.basis.startswith("proxy:")
    estimate = RateEstimate(
        successes=3, trials=10, rate=0.3, ci_low=0.11, ci_high=0.6, confidence=0.95
    )
    with pytest.raises(AttributeError):  # frozen dataclass — 계산 결과 사후 변조 금지
        estimate.rate = 0.9  # type: ignore[misc]


def test_seller_model_temperatures() -> None:
    """SPEC-SELLER-001 §8 — Haiku t=0 / Sonnet t=0.2 기본값 (2-3 모델 팩토리 재료)."""
    settings = Settings(_env_file=None)
    assert settings.seller_haiku_temperature == 0.0
    assert settings.seller_sonnet_temperature == 0.2


def test_spring_timeout_default_is_3s() -> None:
    """AI→Spring 전 구간 타임아웃 기본값은 3.0s (api-spec §2.9 c)."""
    settings = Settings(_env_file=None)
    assert settings.spring_timeout_s == 3.0


def test_general_lane_budget_must_fit_within_stream_cap() -> None:
    """#266 P1 리뷰 — general 레인 직렬 예산이 SSE 전체 캡을 넘으면 기동 실패.

    캡이 먼저 끊으면 `_general_stream` 의 예외 분기에 도달하지 못해 매핑된
    `LLM_TIMEOUT` 대신 오류 코드 없는 `done(stop)` 절단이 된다 — #266 이 고친 상태로
    되돌아간다.
    """
    # 기본값(10 + 2*5 + 20 = 40 < 90)은 통과한다.
    ok = Settings(_env_file=None)
    serial = (
        ok.seller_route_timeout_s
        + 2 * ok.seller_checkpoint_connect_timeout_s
        + ok.seller_general_timeout_s
    )
    assert serial < ok.stream_total_timeout_s

    # **단독 비교였다면 통과했을 조합**(85 < 90)이지만 직렬 합은 105 >= 90 이다.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_general_timeout_s=85.0)

    # 동률(10 + 10 + 70 == 90)도 거절한다 — 어느 시계가 먼저 터질지 지터로 갈린다.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_general_timeout_s=70.0)

    # 경계 바로 아래는 유효하다.
    edge = Settings(_env_file=None, seller_general_timeout_s=69.0)
    assert edge.seller_general_timeout_s == 69.0


def test_general_lane_budget_tracks_every_serial_term() -> None:
    """general 이 아닌 항만 올려도 같은 검증에 걸린다 — 세 값이 직렬로 쌓이기 때문이다."""
    # general 기본값(20)에 라우팅만 70 → 70 + 10 + 20 = 100 >= 90.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_route_timeout_s=70.0)

    # 체크포인터 상한은 **2배**로 잡힌다(#266 3차 리뷰) — _init_checkpointer 가 연결과
    # setup() 을 각각 이 상한으로 감싸기 때문이다.
    # 31 은 계수를 판별하는 값이다: 1배면 10+31+20 = 61 < 90 으로 통과하지만,
    # 실제 2배면 10+62+20 = 92 >= 90 으로 거절돼야 한다. 계수를 1배로 되돌리면 이 단언이
    # 깨진다 — 상수만 바꾸고 계수 배선을 빠뜨리는 회귀를 잡는다.
    assert 10.0 + 31.0 + 20.0 < 90.0, "이 값이 1배 계산에서는 통과한다는 전제"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_checkpoint_connect_timeout_s=31.0)


# ── 이슈 #621 — management/confirm 레인 직렬 예산 ────────────────────────────────


def test_management_lane_budget_defaults_pass() -> None:
    """기본값(이미지 82 / 텍스트수정 83)은 90s 캡 안에 들어온다."""
    ok = Settings(_env_file=None)
    downstream = 3 * ok.state_store_query_timeout_s
    image_path = (
        ok.state_store_query_timeout_s
        + ok.seller_vision_timeout_s
        + ok.seller_product_agent_timeout_s
        + ok.seller_category_resolve_timeout_s
        + downstream
    )
    text_edit_path = (
        ok.state_store_query_timeout_s
        + ok.seller_pending_gate_timeout_s
        + ok.state_store_query_timeout_s
        + ok.seller_route_timeout_s
        + ok.seller_product_agent_timeout_s
        + ok.seller_category_resolve_timeout_s
        + downstream
    )
    assert max(image_path, text_edit_path) < ok.stream_total_timeout_s


def test_management_lane_budget_rejects_over_cap_image_path() -> None:
    """이미지 경로만 캡을 넘겨도 기동 실패 — vision 상한을 크게 올린다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_vision_timeout_s=60.0)


def test_management_lane_budget_rejects_over_cap_text_edit_path() -> None:
    """텍스트 수정 경로만 캡을 넘겨도 기동 실패 — 라우팅 상한을 크게 올린다(이미지
    경로는 라우팅을 거치지 않아 영향받지 않는다)."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_route_timeout_s=60.0)


def test_management_lane_budget_boundary_is_strict() -> None:
    """동률(>=)도 거절한다 — 어느 시계가 먼저 터질지 지터로 갈린다."""
    ok = Settings(_env_file=None)
    downstream = 3 * ok.state_store_query_timeout_s
    text_edit_path = (
        ok.state_store_query_timeout_s
        + ok.seller_pending_gate_timeout_s
        + ok.state_store_query_timeout_s
        + ok.seller_route_timeout_s
        + ok.seller_product_agent_timeout_s
        + ok.seller_category_resolve_timeout_s
        + downstream
    )
    boundary_route = ok.seller_route_timeout_s + (ok.stream_total_timeout_s - text_edit_path)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_route_timeout_s=boundary_route)


def test_confirm_lane_budget_defaults_pass() -> None:
    """기본값(3+45+3=51)은 90s 캡 안에 들어온다."""
    ok = Settings(_env_file=None)
    budget = 2 * ok.state_store_query_timeout_s + ok.seller_confirm_execute_timeout_s
    assert budget < ok.stream_total_timeout_s


def test_confirm_lane_budget_rejects_over_cap() -> None:
    """seller_confirm_execute_timeout_s 를 크게 올리면 기동 실패."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_confirm_execute_timeout_s=90.0)


def test_confirm_lane_budget_boundary_is_strict() -> None:
    """동률(>=)도 거절한다."""
    ok = Settings(_env_file=None)
    boundary = ok.stream_total_timeout_s - 2 * ok.state_store_query_timeout_s
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_confirm_execute_timeout_s=boundary)


# ── 고객 축 피처·군집 (이슈 #593, 03-FEATURES 2부 / 04-CLUSTERING §7) ──────────


def test_customer_feature_settings_defaults() -> None:
    """기본값의 출처는 features/spec.py 다 — 여기서 숫자를 다시 적지 않는다."""
    settings = Settings(_env_file=None)

    assert settings.seller_feature_spec_version == "fe_v1"
    assert tuple(settings.seller_cluster_input_keys) == spec.CLUSTER_INPUT_KEYS
    assert settings.seller_feature_shrinkage_alpha == 5.0
    assert settings.seller_feature_min_denom == 5
    assert settings.seller_amount_bucket_map == spec.AMOUNT_BUCKET_MAP
    assert settings.seller_customer_kmeans_k_min == 2
    assert settings.seller_customer_kmeans_k_max == 6
    assert settings.seller_customer_kmeans_n_init == 10
    assert settings.seller_customer_pca_variance == 0.95
    assert settings.seller_customer_pca_auto_compare is True
    assert settings.seller_customer_segment_min_size == 30
    assert settings.seller_snapshot_row_limit == 1000
    assert settings.seller_customer_label_thresholds == spec.DEFAULT_LABEL_THRESHOLDS


def test_customer_kmeans_settings_are_separate_from_product_axis() -> None:
    """⚠️ 상품 축 키를 재사용하면 값 하나로 두 파이프라인이 동시에 흔들린다(결정 28b)."""
    settings = Settings(_env_file=None, seller_customer_kmeans_random_state=7)

    assert settings.seller_customer_kmeans_random_state == 7
    assert settings.seller_kmeans_random_state == 42  # 상품 축 — 무접촉
    assert settings.seller_behavior_kmeans_k_max == 5  # 상품 축 k 범위도 별개


def test_cluster_group_weights_default_to_one_over_sqrt_n() -> None:
    """축군의 총 영향력이 1 이 되도록 열별 가중치는 1/√n 이다(04 §1.2)."""
    weights = Settings(_env_file=None).seller_customer_cluster_group_weights

    assert weights["activity"] == pytest.approx(1 / math.sqrt(5))
    assert weights["funnel"] == pytest.approx(1 / math.sqrt(3))
    assert weights["explore"] == pytest.approx(1.0)
    # 각 축군의 기여 합 = n × w² = 1.
    for group, keys in spec.CLUSTER_GROUP_KEYS.items():
        assert len(keys) * weights[group] ** 2 == pytest.approx(1.0)


def test_cluster_input_keys_must_match_spec_order() -> None:
    """스냅샷 각인과 코드가 어긋난 채 기동하면 다른 정의로 만든 숫자를 비교하게 된다."""
    shuffled = list(spec.CLUSTER_INPUT_KEYS)
    shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_cluster_input_keys=shuffled)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_cluster_input_keys=list(spec.CLUSTER_INPUT_KEYS)[:11])


def test_amount_bucket_map_must_match_spec_order() -> None:
    """응답 amountBuckets 와의 대조는 런타임이고, 부팅은 상수끼리만 본다."""
    reordered = {key: spec.AMOUNT_BUCKET_MAP[key] for key in reversed(spec.AMOUNT_BUCKET_ORDER)}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_amount_bucket_map=reordered)


def test_cluster_group_weights_reject_unknown_or_negative() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_customer_cluster_group_weights={"activity": 1.0})
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            seller_customer_cluster_group_weights={
                **spec.DEFAULT_CLUSTER_GROUP_WEIGHTS,
                "explore": 0.0,
            },
        )


def test_label_thresholds_require_every_key_and_percentile_range() -> None:
    partial = dict(spec.DEFAULT_LABEL_THRESHOLDS)
    partial.pop("loyal_recency_min")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_customer_label_thresholds=partial)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            seller_customer_label_thresholds={
                **spec.DEFAULT_LABEL_THRESHOLDS,
                "loyal_orders_min": 120.0,
            },
        )


def test_customer_kmeans_k_range_is_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_customer_kmeans_k_min=1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_customer_kmeans_k_min=6, seller_customer_kmeans_k_max=4)


def test_feature_dict_settings_survive_empty_env_string() -> None:
    """deploy.yml 은 미설정 vars 를 빈 문자열로 쓴다 — model_price_* 와 같은 방어다."""
    settings = Settings(
        _env_file=None,
        seller_amount_bucket_map="",
        seller_customer_cluster_group_weights="",
        seller_customer_label_thresholds="",
    )
    assert settings.seller_amount_bucket_map == spec.AMOUNT_BUCKET_MAP
    assert settings.seller_customer_label_thresholds == spec.DEFAULT_LABEL_THRESHOLDS


def test_snapshot_comparison_settings_defaults() -> None:
    """이슈 #594 신설 4종 — 보관 14일 / 비교 거리 7일 / 순유입 3% / 이동 표시 1%."""
    settings = Settings(_env_file=None)

    assert settings.seller_snapshot_retention_days == 14
    assert settings.seller_baseline_offset_days == 7
    assert settings.seller_segment_shift_pct == 0.03
    assert settings.seller_move_report_min_pct == 0.01
    # 소규모 군집 임계는 #593 이 이미 둔 키다 — 신설하지 않고 재사용한다.
    assert settings.seller_customer_segment_min_size == 30


def test_retention_shorter_than_baseline_offset_fails_fast() -> None:
    """보관이 비교 거리보다 짧으면 churn 이 구조적으로 영원히 no_baseline 이 된다."""
    with pytest.raises(ValidationError, match="SELLER_SNAPSHOT_RETENTION_DAYS"):
        Settings(_env_file=None, seller_snapshot_retention_days=3, seller_baseline_offset_days=7)


def test_seller_trigger_defaults() -> None:
    """무인 스캔 트리거 고정 임계(#595, `10-TRIGGER` §3.2 표).

    ⚠️ 단위가 둘이다 — `*_pct` 는 상대 변화율, `*_pp` 는 퍼센트포인트. 섞으면 임계의
    뜻이 바뀐다(이탈률 2%→3% 를 상대로 재면 +50%).
    """
    settings = Settings(_env_file=None)
    assert settings.seller_trigger_sales_pct == 0.05
    assert settings.seller_trigger_conversion_pct == 0.10
    assert settings.seller_trigger_product_drop_pct == 0.30
    assert settings.seller_trigger_cart_abandon_pp == 0.10
    assert settings.seller_trigger_new_customer_drop_pct == 0.30
    assert settings.seller_trigger_repurchase_drop_pp == 0.10
    assert settings.seller_scan_baseline_days == 7


def test_seller_eval_gate_defaults() -> None:
    """판정 검증 게이트(#595, `12-EVAL` 결정 119·121)."""
    settings = Settings(_env_file=None)
    assert settings.seller_eval_null_days == 1000
    assert settings.seller_eval_trigger_rate_max == 0.01
    assert settings.seller_cluster_stability_min == 0.7


def test_seller_trigger_thresholds_fail_fast() -> None:
    """0 이면 AND 의 한쪽이 사라지고 1 이상이면 도달 불가 — 둘 다 조용한 무력화라 막는다."""
    for field in (
        "seller_trigger_sales_pct",
        "seller_trigger_conversion_pct",
        "seller_trigger_product_drop_pct",
        "seller_trigger_cart_abandon_pp",
        "seller_trigger_new_customer_drop_pct",
        "seller_trigger_repurchase_drop_pp",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: 0.0})
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: 1.0})
    # 경계 안쪽은 유효하다.
    assert Settings(_env_file=None, seller_trigger_sales_pct=0.99).seller_trigger_sales_pct == 0.99


def test_seller_scan_window_relations_fail_fast() -> None:
    """lookback 이 비교 구간 이하면 대상일 자리가 없어 트리거 1 이 상시 보류가 된다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_scan_baseline_days=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_scan_baseline_days=28)  # lookback 과 같다
    assert Settings(_env_file=None, seller_scan_baseline_days=27).seller_scan_baseline_days == 27


def test_seller_eval_gate_fail_fast() -> None:
    """시뮬레이션이 lookback 보다 짧으면 잴 날이 0 일이라 게이트가 조용히 통과한다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_eval_null_days=10)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_eval_trigger_rate_max=0.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_cluster_stability_min=1.5)
    assert Settings(_env_file=None, seller_eval_null_days=28).seller_eval_null_days == 28


# ── chart 해석 에이전트 (이슈 #600, 09-CHART.md §8) ────────────────────────────────


def test_seller_chart_interpret_settings_defaults() -> None:
    """§8 표의 신설 5종 기본값 — 킬스위치 true / 20s / 재작성 1회 / 800자 / 축 선언 25s."""
    settings = Settings(_env_file=None)

    assert settings.seller_chart_interpret_enabled is True
    assert settings.seller_chart_interpret_timeout_s == 20.0
    assert settings.seller_chart_interpret_max_retries == 1
    assert settings.seller_chart_interpret_max_chars == 800
    # [#600] graph(축 선언) 전용 타임아웃 — seller_worker_timeout_s(60s) 재사용을 그친다.
    assert settings.seller_chart_agent_timeout_s == 25.0
    # 재사용(신설 금지, §8 표) — C1이 그대로 쓰는 기존 #598 신설 항목.
    assert settings.seller_report_causal_terms == ["때문에", "원인은", "그래서", "유발", "야기"]


def test_seller_chart_forbidden_terms_default_covers_c4_four_groups() -> None:
    """C4(chart_claims_bounded) 4묶음 — snapshot_trend/daily_bucket/bottom_rank/behavior_all."""
    settings = Settings(_env_file=None)
    terms = settings.seller_chart_forbidden_terms

    assert set(terms) == {"snapshot_trend", "daily_bucket", "bottom_rank", "behavior_all"}
    assert "추세" in terms["snapshot_trend"]
    assert "일별" in terms["daily_bucket"]
    assert "최저" in terms["bottom_rank"]
    assert "전체 행동" in terms["behavior_all"]


def test_seller_chart_forbidden_terms_survives_empty_env_string() -> None:
    """deploy.yml 빈 문자열 함정 — seller_amount_bucket_map 과 같은 방어(NoDecode)."""
    settings = Settings(_env_file=None, seller_chart_forbidden_terms="")
    assert settings.seller_chart_forbidden_terms["snapshot_trend"] == [
        "추세",
        "증가",
        "감소",
        "늘었",
        "줄었",
        "상승",
        "하락",
        "이후",
    ]
