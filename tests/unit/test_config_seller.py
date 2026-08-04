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
    # 기본값(10 + 5 + 20 = 35 < 90)은 통과한다.
    ok = Settings(_env_file=None)
    serial = (
        ok.seller_route_timeout_s
        + ok.seller_checkpoint_connect_timeout_s
        + ok.seller_general_timeout_s
    )
    assert serial < ok.stream_total_timeout_s

    # **단독 비교였다면 통과했을 조합**(85 < 90)이지만 직렬 합은 100 >= 90 이다.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_general_timeout_s=85.0)

    # 동률(10 + 5 + 75 == 90)도 거절한다 — 어느 시계가 먼저 터질지 지터로 갈린다.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_general_timeout_s=75.0)

    # 경계 바로 아래는 유효하다.
    edge = Settings(_env_file=None, seller_general_timeout_s=74.0)
    assert edge.seller_general_timeout_s == 74.0


def test_general_lane_budget_tracks_every_serial_term() -> None:
    """general 이 아닌 항만 올려도 같은 검증에 걸린다 — 세 값이 직렬로 쌓이기 때문이다."""
    # general 기본값(20)에 라우팅만 70 → 10 이 아니라 70+5+20 = 95 >= 90.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_route_timeout_s=70.0)

    # 체크포인터 연결 상한도 마찬가지다(#266 PR 리뷰로 예산식에 편입).
    # 10 + 65 + 20 = 95 >= 90.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seller_checkpoint_connect_timeout_s=65.0)
