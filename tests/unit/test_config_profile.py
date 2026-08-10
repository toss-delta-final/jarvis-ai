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


# ─────────── #321 대화 전사록 보존 기간 ───────────


def test_conversation_retention_defaults_match_audit_retention() -> None:
    settings = Settings(_env_file=None)

    assert settings.conversation_retention_days == 90.0
    assert settings.conversation_retention_days == settings.graph_audit_retention_days


def test_conversation_retention_days_must_not_exceed_audit_retention() -> None:
    """감사 원장보다 전사록이 먼저 지워지면, 그 사이 구간의 감사 행이 가리키는 원문이 없어져
    조사 불가능해진다(이슈 #321)."""
    with pytest.raises(ValidationError, match="CONVERSATION_RETENTION_DAYS"):
        Settings(_env_file=None, conversation_retention_days=91.0, graph_audit_retention_days=90.0)


def test_conversation_retention_days_equal_to_audit_retention_is_allowed() -> None:
    """경계는 포함 — 같은 값은 허용(초과일 때만 거부)."""
    settings = Settings(
        _env_file=None, conversation_retention_days=90.0, graph_audit_retention_days=90.0
    )
    assert settings.conversation_retention_days == 90.0


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
        "wishlist_view",
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


# ─────────── #358 그래프 저장 안전장치 보존 기간 ───────────


def test_graph_retention_defaults_are_wired() -> None:
    """멱등 원장·감사 보존 기간은 config 주입이다 (SPEC-PROFILE-GRAPH-149 §11).

    두 값 모두 🔴 C-23 잔여(정책·법무 미정)라 **잠정값**이다. 만료 행을 실제로 지우는 스윕은
    #358 범위 밖이므로, 지금 이 값들이 바꾸는 동작은 아래 REQ-PGRAPH-044 기동 검증뿐이다.
    """
    settings = Settings(_env_file=None)

    assert settings.graph_idempotency_ttl_h == 24.0
    assert settings.graph_audit_retention_days == 90.0


def test_idempotency_ledger_must_not_outlive_audit_log() -> None:
    """REQ-PGRAPH-044 — 원장이 감사보다 오래 살면 안 된다.

    원장은 "이 요청 아까 이렇게 답했다"를 들고 있고 감사는 "그 변경이 실제로 있었다"의 근거다.
    원장이 더 오래 살면 재전송이 최초 응답을 재생하는데 그 변경의 감사 근거는 이미 사라진
    구간이 생긴다. §11 이 "두 값의 대소로 정확성을 맞추려는 설정은 금지한다"고 못박았으므로
    런타임 보정이 아니라 **기동 시점 fail-fast** 다.
    """
    with pytest.raises(ValidationError, match="GRAPH_IDEMPOTENCY_TTL_H"):
        Settings(_env_file=None, graph_idempotency_ttl_h=48, graph_audit_retention_days=1)


def test_graph_retention_boundary_is_inclusive() -> None:
    """같은 길이는 허용한다 — REQ-PGRAPH-044 는 "길지 않아야" 이지 "짧아야" 가 아니다.

    경계를 배타로 재면 24h == 1day 인 정상 구성이 기동에서 죽는다
    (docs/lessons.md 2026-08-08 "TTL 만료를 엄격 부등호로 재면 판정이 시계 분해능에 걸린다").
    """
    # conversation_retention_days 도 함께 낮춘다 — 기본 90 이 graph_audit_retention_days=1 을
    # 넘어 이 테스트가 다른 경계(#321)에서 죽지 않게 한다.
    settings = Settings(
        _env_file=None,
        graph_idempotency_ttl_h=24,
        graph_audit_retention_days=1,
        conversation_retention_days=1,
    )

    assert settings.graph_idempotency_ttl_h == 24.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_idempotency_ttl_h", 0),
        ("graph_audit_retention_days", 0),
    ],
)
def test_graph_retention_tunables_reject_nonpositive(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


# ─────────── 그래프 API 응답 예산 (#360, api-spec §3.8·§3.9) ───────────


def test_graph_api_budgets_are_injected_not_hardcoded() -> None:
    """예산이 config 에 있어야 실측 후 코드 수정 없이 재조정된다 (CLAUDE.md — 튜너블 하드코딩 금지).

    기본값은 **제안이며 실측이 아니다** — api-spec §2.9 (c) 기준표에 행이 없는 이유가 그것이고,
    구현 후 실측해 등재하는 것이 #360 완료 조건이다.
    """
    settings = Settings(_env_file=None)

    assert settings.profile_graph_read_budget_s == 2.0
    assert settings.profile_graph_write_budget_s == 3.0


def test_the_read_budget_must_stay_under_the_write_budget() -> None:
    """조회가 변경보다 오래 걸리는 예산은 계약(§3.8 2s / §3.9 3s)을 뒤집는다 — 기동을 막는다."""
    with pytest.raises(ValidationError, match="must stay under the write budget"):
        Settings(_env_file=None, profile_graph_read_budget_s=4.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_graph_read_budget_s", 0),
        ("profile_graph_write_budget_s", 0),
    ],
)
def test_graph_api_budgets_reject_nonpositive(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
