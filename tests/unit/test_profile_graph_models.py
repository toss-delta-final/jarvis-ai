"""개인화 그래프 내부 저장 모델·식별자 도출 (SPEC-PROFILE-GRAPH-149 §5, REQ-PGRAPH-010/017).

와이어 모델이 아니라 ("graph", user_id)/"v1" 문서에 들어가는 snake_case 저장 모델이다.
식별자 결정론이 기능 요구사항이라(REQ-PGRAPH-010) 고정 벡터로 못박는다 — 값이 흔들리면
재파생이 tombstone 을 우회한다.
"""

import pytest
from pydantic import ValidationError

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    NodeResolution,
    make_edge_id,
    make_edge_key,
    make_node_id,
    normalize_label,
)


def _edge(**overrides: object) -> GraphEdge:
    """SPEC §5.2 는 기본값을 두지 않는다 — 테스트 편의 팩토리로만 채운다."""
    base: dict = {
        "edge_key": "likes|brand:소니",
        "edge_id": "e_cbbc9dce30b02793",
        "node_id": "brand:소니",
        "predicate": "likes",
        "status": "active",
        "promoted": True,
        "origin": "machine",
        "source_latest": "conversation",
        "confidence": 0.8,
        "evidence_count": 2,
        "evidence_by_source": {"conversation": 2},
        "evidence_refs": ["fact-1", "fact-2"],
        "first_observed_at": "2026-08-01T00:00:00+00:00",
        "last_observed_at": "2026-08-05T00:00:00+00:00",
        "decay_evaluated_at": "2026-08-06T00:00:00+00:00",
        "valid_from": "2026-08-01T00:00:00+00:00",
        "superseded_by": None,
        "suppressed_at": None,
        "user_intent": None,
        "challenge_count": 0,
        "derived_from_sensitive": False,
        "sensitive_topic": None,
    }
    base.update(overrides)
    return GraphEdge(**base)


# ─────────── 정규화 (REQ-PGRAPH-017) ───────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SONY", "sony"),
        ("ＳＯＮＹ", "sony"),  # NFKC 가 전각을 접는다
        (" 소니 ", "소니"),
        ("음향가전  >  블루투스 이어폰", "음향가전 > 블루투스 이어폰"),  # 공백만 정리
    ],
)
def test_normalize_label_folds_case_width_and_whitespace(raw: str, expected: str) -> None:
    assert normalize_label(raw) == expected


def test_normalize_label_keeps_korean_particles() -> None:
    """조사·어미까지 정규화하면 정당한 취향 신호를 잃는다 — 도입하지 않는다(REQ-PGRAPH-017)."""
    assert normalize_label("노이즈캔슬링이") == "노이즈캔슬링이"
    assert normalize_label("노이즈캔슬링이") != normalize_label("노이즈캔슬링")


def test_normalize_label_does_not_merge_across_scripts() -> None:
    """`소니`/`SONY` 수렴은 정규화가 아니라 통제 어휘 스냅의 몫이다(C-28 / OPEN-G2).

    이 단언이 깨지면 정규화가 어휘의 일을 대신하려 든다는 뜻이라 resolver 설계 전제가 무너진다.
    """
    assert normalize_label("소니") != normalize_label("SONY")


# ─────────── 식별자 도출 (REQ-PGRAPH-010) ───────────


def test_make_node_id_joins_type_and_normalized_label() -> None:
    assert make_node_id("brand", " 소니 ") == "brand:소니"
    assert make_node_id("priceBand", "30000-50000") == "priceBand:30000-50000"
    assert (
        make_node_id("category", "음향가전 > 블루투스 이어폰")
        == "category:음향가전 > 블루투스 이어폰"
    )


def test_make_edge_key_is_predicate_pipe_node_id() -> None:
    assert make_edge_key("likes", "brand:소니") == "likes|brand:소니"


@pytest.mark.parametrize(
    ("edge_key", "expected"),
    [
        ("likes|brand:소니", "e_cbbc9dce30b02793"),
        ("prefers|priceBand:30000-50000", "e_e72d5944f62ce521"),
        ("avoids|category:음향가전 > 블루투스 이어폰", "e_90649c392d73efcd"),
    ],
)
def test_make_edge_id_fixed_vectors(edge_key: str, expected: str) -> None:
    """UTF-8 sha256 앞 16 hex 고정. 값이 바뀌면 기존 tombstone 이 통째로 무효가 된다.

    hashlib 을 쓰는 이유: 내장 hash() 는 PYTHONHASHSEED 랜덤화로 프로세스마다 달라져
    "결정론적 식별자"라는 기능 요구사항 자체가 성립하지 않는다.
    """
    assert make_edge_id(edge_key) == expected


def test_make_edge_id_shape_is_e_plus_16_hex() -> None:
    edge_id = make_edge_id("likes|brand:소니")

    assert edge_id.startswith("e_")
    assert len(edge_id) == 18
    assert all(ch in "0123456789abcdef" for ch in edge_id[2:])


def test_identifier_derivation_is_stable_across_calls() -> None:
    """같은 입력은 항상 같은 식별자 — 재파생이 tombstone 을 우회하지 않는 근거."""
    first = make_edge_id(make_edge_key("likes", make_node_id("brand", "소니")))
    second = make_edge_id(make_edge_key("likes", make_node_id("brand", " 소니 ")))

    assert first == second


# ─────────── 모델 스키마 (SPEC §5.1~5.3) ───────────


def test_graph_node_requires_core_fields() -> None:
    node = GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)

    assert node.resolution is None  # 유일하게 기본값이 있는 필드(SPEC §5.1)

    with pytest.raises(ValidationError):
        GraphNode(node_id="brand:소니", type="brand", label="소니")  # verified 누락


def test_graph_node_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        GraphNode(node_id="color:빨강", type="color", label="빨강", verified=False)


def test_node_resolution_records_distance_and_margin() -> None:
    """임계 재측정(#344/OPEN-G1) 근거라 거리·margin 을 저장한다 — 와이어에는 싣지 않는다."""
    resolution = NodeResolution(
        method="embedding",
        distance=0.12,
        margin=0.04,
        lexicon_version="catalog_categories",
        anchor_phrase="블루투스 이어폰 찾고 있어",
        resolved_at="2026-08-06T00:00:00+00:00",
    )

    assert resolution.method == "embedding"
    assert resolution.distance == 0.12
    assert resolution.margin == 0.04


def test_node_resolution_allows_missing_distance_for_rule_paths() -> None:
    """priceBand·product 는 임베딩을 쓰지 않으므로 거리 자체가 없다(REQ-PGRAPH-014)."""
    resolution = NodeResolution(
        method="rule",
        anchor_phrase="3~5만원",
        resolved_at="2026-08-06T00:00:00+00:00",
    )

    assert resolution.distance is None
    assert resolution.margin is None
    assert resolution.lexicon_version is None


def test_graph_edge_requires_every_field_including_nullables() -> None:
    """SPEC §5.2 는 기본값을 두지 않는다 — 병합 엔진이 필드마다 의식적으로 값을 정하게 강제한다."""
    assert _edge().status == "active"

    with pytest.raises(ValidationError):
        GraphEdge(  # type: ignore[call-arg]
            edge_key="likes|brand:소니",
            edge_id="e_cbbc9dce30b02793",
            node_id="brand:소니",
            predicate="likes",
            status="active",
            promoted=True,
        )


def test_graph_edge_status_and_promoted_are_orthogonal() -> None:
    """suppressed 인데 promoted 인 상태가 표현 가능해야 한다(REQ-PGRAPH-020)."""
    edge = _edge(status="suppressed", promoted=True, suppressed_at="2026-08-06T00:00:00+00:00")

    assert edge.status == "suppressed"
    assert edge.promoted is True


def test_graph_edge_rejects_unknown_predicate_and_status() -> None:
    with pytest.raises(ValidationError):
        _edge(predicate="hates")

    with pytest.raises(ValidationError):
        _edge(status="deleted")


def test_graph_document_round_trips_through_json() -> None:
    """jsonb 저장·재적재 왕복에서 값이 보존돼야 재생 동일성을 말할 수 있다."""
    document = GraphDocument(
        revision=3,
        nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
        edges=[_edge()],
        unprojected_count=2,
        truncated=True,
        purged_at=None,
        updated_at="2026-08-06T00:00:00+00:00",
    )

    restored = GraphDocument.model_validate(document.model_dump(mode="json"))

    assert restored == document


def test_graph_document_requires_unprojected_count() -> None:
    """트리플을 못 만든 fact 는 문서에 싣지 않고 개수만 센다(REQ-PGRAPH-004)."""
    with pytest.raises(ValidationError):
        GraphDocument(  # type: ignore[call-arg]
            revision=1,
            nodes=[],
            edges=[],
            purged_at=None,
            updated_at="2026-08-06T00:00:00+00:00",
        )
