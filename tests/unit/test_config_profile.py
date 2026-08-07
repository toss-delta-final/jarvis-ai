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


# ─────────── #356 개인화 그래프 튜너블 (SPEC-PROFILE-GRAPH-149 §11) ───────────


def test_profile_graph_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.graph_node_distance_max == 0.10
    assert settings.graph_node_override_margin == 0.05
    assert settings.graph_demote_margin == 0.1
    assert settings.graph_decay_half_life_days == 30.0
    assert settings.graph_evidence_refs_max == 20
    assert settings.profile_graph_label_max_chars == 60
    assert settings.profile_graph_max_edges == 200
    assert settings.profile_graph_confidence_buckets == [0.34, 0.67]
    assert settings.profile_graph_delta_enabled is True


def test_graph_distance_threshold_is_not_inherited_from_category_mapping() -> None:
    """거리 임계는 사전에 종속한다 — 앵커 분포가 다른 #59 값을 그대로 옮기지 않는다(OPEN-G1).

    resolver 앵커는 발화 파생 구절이고 #59 는 decompose 가 만든 카테고리 질의라 분포가 다르다.
    #344 재측정 전까지는 더 보수적인(=드롭이 잦은) 쪽에 선다 — 틀린 노드는 측정된 손실
    (-0.053/-0.117)을 만들고 없는 노드는 손실이 0에 가깝다(REQ-PGRAPH-012b).
    """
    settings = Settings(_env_file=None)

    assert settings.graph_node_distance_max < settings.category_distance_max
    assert settings.graph_node_override_margin > settings.category_distance_override_margin


def test_graph_demote_margin_must_be_below_promote_threshold() -> None:
    """강등 임계 < 승격 임계가 히스테리시스의 정의다(REQ-PGRAPH-016).

    같거나 크면 강등 임계가 0 이하로 내려가 승격/강등이 같은 지점에서 갈리고, 배치마다
    깜빡여 사용자에게는 항목이 나타났다 사라지는 것으로 보인다.
    """
    with pytest.raises(ValidationError, match="GRAPH_DEMOTE_MARGIN"):
        Settings(_env_file=None, profile_gate_threshold=0.5, graph_demote_margin=0.5)


def test_graph_demote_margin_accepts_value_below_promote_threshold() -> None:
    settings = Settings(_env_file=None, profile_gate_threshold=0.5, graph_demote_margin=0.2)

    assert settings.graph_demote_margin < settings.profile_gate_threshold


@pytest.mark.parametrize(
    "buckets",
    [
        [0.67, 0.34],  # 내림차순
        [0.34],  # 경계 1개 = 2버킷
        [0.34, 0.5, 0.67],  # 경계 3개 = 4버킷
        [0.0, 0.67],  # 0 은 하위 버킷을 비운다
        [0.34, 1.0],  # 1.0 은 상위 버킷을 비운다
    ],
)
def test_profile_graph_confidence_buckets_must_be_two_ascending_interior_points(
    buckets: list[float],
) -> None:
    """와이어는 3버킷 라벨만 노출한다 — 경계는 정확히 2개이고 (0,1) 안에서 오름차순이어야 한다."""
    with pytest.raises(ValidationError, match="PROFILE_GRAPH_CONFIDENCE_BUCKETS"):
        Settings(_env_file=None, profile_graph_confidence_buckets=buckets)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_decay_half_life_days", 0),
        ("graph_evidence_refs_max", 0),
        ("profile_graph_label_max_chars", 0),
        ("profile_graph_max_edges", 0),
        ("graph_node_distance_max", -0.1),
        ("profile_delta_max_tokens", 0),
        ("profile_summary_max_tokens", 0),
    ],
)
def test_profile_graph_tunables_reject_nonpositive(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_llm_output_budgets_are_injected_not_hardcoded() -> None:
    """델타·요약 LLM 예산은 설정 주입이다 (CLAUDE.md 튜너블 하드코딩 금지).

    실측 근거가 있다: 구조화 프롬프트 전환으로 출력이 길어지자 하드코딩 800 에서 reasoning
    토큰이 예산을 먼저 먹어 분포 프로브 4세션 중 2건이 `LengthFinishReasonError` 로 죽었다
    (#325 가 enrichment 에서 밟은 것과 같은 함정). 값을 바꿔 재현·완화할 수 있어야 한다.
    """
    tuned = Settings(_env_file=None, profile_delta_max_tokens=4096, profile_summary_max_tokens=512)

    assert tuned.profile_delta_max_tokens == 4096
    assert tuned.profile_summary_max_tokens == 512
    # 기본값은 하드코딩이던 800/1000 보다 넉넉해야 한다 — 그게 이 이관의 이유다.
    default = Settings(_env_file=None)
    assert default.profile_delta_max_tokens > 800
    assert default.profile_summary_max_tokens > 1000
