"""판매자 Settings 신규 필드 테스트 (DESIGN-SELLER-TOOLS-STAGE1 §5).

임계값·타임아웃을 코드 하드코딩이 아니라 Settings 로 주입하는지 확인한다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_seller_settings_defaults() -> None:
    """§5 표의 판매자 임계값 기본값이 그대로 로드된다 (env 미설정 시)."""
    settings = Settings(_env_file=None)
    # [통일 2026-07-20] 서비스 토큰은 팀 규약 internal_api_token 단일 키(기본 미설정).
    assert settings.internal_api_token == ""
    assert settings.seller_ma_window == 7
    assert settings.seller_ma_min_window == 3  # Spring MIN_WINDOW 정렬(#194)
    assert settings.seller_anomaly_deviation_pct == 30.0
    assert settings.seller_conversion_drop_pct == 20.0
    assert settings.seller_churn_inactive_days == 30
    assert settings.seller_recent_days_default == 7
    assert settings.seller_calc_max_result_digits == 100
    assert settings.seller_report_score_threshold == 21
    assert settings.seller_report_max_retries == 3
    assert settings.seller_draft_ttl_minutes == 10
    assert settings.seller_history_recent_n == 5
    assert settings.seller_tool_call_limit == 8
    # [#196] I-13 상품별 rows 상한 — I-14 용 max_events(5)와 분리 신설.
    assert settings.seller_summary_max_events == 5
    assert settings.seller_summary_max_products == 10
    # [#197 PR 리뷰] I-8 은 admin 소유 협의(🔴) 전까지 판매자 노출 보류 — 기본 비활성.
    assert settings.seller_account_events_enabled is False
    # [#197 PR 리뷰] I-16 이탈 회원 나열 상한 — I-14 용 max_events 와 분리(결합 방지).
    assert settings.seller_churn_member_max == 5
    # ── 브랜치 분석 검증(이슈 #242, DESIGN-ANALYSIS-V31-242 §9) ──
    assert settings.seller_worker_max_retries == 1
    assert settings.seller_analysis_score_threshold == 21
    assert settings.seller_analysis_judge_timeout_s == 20.0
    assert settings.seller_branch_deadline_s == 160.0


def test_seller_ma_window_invalid_config_fails_fast() -> None:
    """[#194 PR 리뷰] 이상 감지 window 오설정은 기동 시점에 실패한다 — 런타임에
    daily 매출 조회가 매 요청 ValueError 로 죽는 것 방지(설정값은 요청마다 안 변함)."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_ma_min_window=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_ma_window=2, seller_ma_min_window=3)
    # 경계(min_window == window)는 유효하다.
    ok = Settings(_env_file=None, seller_ma_window=3, seller_ma_min_window=3)
    assert ok.seller_ma_min_window == 3


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
