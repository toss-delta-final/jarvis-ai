"""프로필/I-20 설정 검증."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_session_end_claim_ttl_default_covers_two_llm_stages() -> None:
    settings = Settings()

    assert settings.session_end_claim_ttl_s == 180.0
    assert settings.session_end_claim_ttl_s > (
        settings.llm_timeout_s * (settings.llm_max_retries + 1) * 2
    )


def test_session_end_claim_ttl_must_exceed_processing_budget() -> None:
    with pytest.raises(ValidationError, match="must exceed the two-stage LLM timeout budget"):
        Settings(session_end_claim_ttl_s=0)


def test_profile_idle_scheduler_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.profile_session_idle_timeout_s == 600.0
    assert settings.profile_idle_sweep_interval_s == 60.0
    assert settings.profile_idle_sweep_batch_size == 10
    assert settings.profile_idle_max_concurrency == 2
    assert settings.profile_idle_claim_ttl_s == 900.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_session_idle_timeout_s", 0),
        ("profile_idle_sweep_interval_s", 0),
        ("profile_idle_sweep_batch_size", 0),
        ("profile_idle_max_concurrency", 0),
        ("profile_idle_claim_ttl_s", 0),
    ],
)
def test_profile_idle_scheduler_settings_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_profile_idle_claim_ttl_must_cover_every_configured_batch_wave() -> None:
    with pytest.raises(ValidationError, match="all configured batch waves"):
        Settings(
            _env_file=None,
            profile_idle_sweep_batch_size=11,
            profile_idle_max_concurrency=2,
            profile_idle_claim_ttl_s=700,
        )


# ─────────── #119 개인화 강도 튜너블 ───────────


def test_profile_injection_defaults() -> None:
    """기본값 — 하드필터는 발화에서만(rerank_only), 취향은 rerank 동점 처리로만."""
    settings = Settings(_env_file=None)

    assert settings.profile_injection_scope == "rerank_only"
    assert settings.profile_rerank_influence == "tiebreak"
    assert settings.profile_buffer_repeat_cap == 2
    assert settings.profile_buffer_excluded_intents == [
        "order_status",
        "cart_view",
        "cart_remove",
        "wishlist_remove",
    ]


@pytest.mark.parametrize("value", [0, 1])
def test_profile_buffer_repeat_cap_rejects_below_two(value: int) -> None:
    """1 이하로 낮추면 게이트의 반복 승격 경로(explicit OR repeated)가 죽는다 — 기동을 막는다.

    버퍼에 1 건만 남으면 델타 추출 LLM 이 반복을 볼 수 없고, 세션 간 누적(GateState)은
    미구현이라(SPEC-PROFILE-001 OPEN-P12) 다음 세션이 대신 살려주지도 않는다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, profile_buffer_repeat_cap=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_injection_scope", "decompose_only"),
        ("profile_injection_scope", ""),
        ("profile_rerank_influence", "strong"),
    ],
)
def test_profile_injection_rejects_unknown_literal(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_profile_buffer_excluded_intents_rejects_unknown_intent() -> None:
    """오타("order-status")가 조용히 무효가 되면 버퍼가 계속 오염된다 — 기동 시 막는다."""
    with pytest.raises(ValidationError, match="profile_buffer_excluded_intents"):
        Settings(_env_file=None, profile_buffer_excluded_intents=["order-status"])


def test_profile_buffer_excluded_intents_match_route_decision_literal() -> None:
    """config 의 intent 집합이 decompose 산출 Literal 과 드리프트하지 않게 고정한다."""
    from typing import get_args, get_type_hints

    from app.agents.buyer.recommendation.state import RouteDecision
    from app.core.config import ROUTE_INTENTS

    intent_type = get_type_hints(RouteDecision)["intent"]
    assert ROUTE_INTENTS == set(get_args(intent_type))
