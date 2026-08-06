"""결정론적 병합 엔진 (SPEC-PROFILE-GRAPH-149 §6.2·§6.3, REQ-PGRAPH-003/004/015~018/020~022).

`build_graph_document` 는 **순수 함수**다 — 인자에 llm·embed·store 가 없어 "병합·최신성·승격
판정에 LLM 을 쓰지 않는다"(REQ-PROF-032/033)가 mock 단언이 아니라 **구조적으로** 보장된다.
그 구조 자체를 회귀 가드로 고정한다.
"""

import inspect

import pytest

from app.agents.profile.graph_merge import build_graph_document, empty_document
from app.agents.profile.graph_models import GraphDocument, GraphEdge, GraphNode
from app.agents.profile.store import FactRecord
from app.core.config import Settings

NOW = "2026-08-06T00:00:00+00:00"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _triple(
    node_id: str = "brand:소니",
    predicate: str = "likes",
    *,
    label: str = "소니",
    node_type: str = "brand",
    salience: float = 0.9,
    verified: bool = False,
    source: str = "conversation",
) -> dict:
    edge_key = f"{predicate}|{node_id}"
    from app.agents.profile.graph_models import make_edge_id

    return {
        "node": {
            "node_id": node_id,
            "type": node_type,
            "label": label,
            "verified": verified,
            "resolution": None,
        },
        "predicate": predicate,
        "edge_key": edge_key,
        "edge_id": make_edge_id(edge_key),
        "salience": salience,
        "source": source,
    }


def _fact(
    key: str = "f1",
    *,
    fact: str = "소니 선호",
    created_at: str = "2026-08-01T00:00:00+00:00",
    triples: list[dict] | None = None,
) -> FactRecord:
    return FactRecord(
        fact_key=key,
        fact=fact,
        created_at=created_at,
        graph_triples=[_triple()] if triples is None else triples,
    )


def _edge_by_key(document: GraphDocument, edge_key: str) -> GraphEdge | None:
    return next((e for e in document.edges if e.edge_key == edge_key), None)


# ─────────── LLM 0회 — 구조적 보장 ───────────


def test_merge_signature_has_no_llm_or_io_seam() -> None:
    """병합에 LLM·임베딩·저장소를 넘길 자리가 없어야 한다(REQ-PROF-032/033).

    누가 실수로 인자를 더하면 여기서 막힌다 — mock 으로 "안 불렸다"를 재는 것보다 강한 보장이다.
    """
    params = set(inspect.signature(build_graph_document).parameters)

    assert params == {"facts", "existing", "settings", "now"}


# ─────────── 재생 동일성 (REQ-PGRAPH-015) ───────────


def test_replaying_same_observations_yields_identical_document(settings: Settings) -> None:
    facts = [
        _fact("f1", created_at="2026-08-01T00:00:00+00:00"),
        _fact("f2", fact="이어폰 선호", created_at="2026-08-03T00:00:00+00:00"),
    ]

    first = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)
    second = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_observation_order_does_not_depend_on_input_order(settings: Settings) -> None:
    """관측은 (observed_at, fact_key) 오름차순으로 처리한다 — 입력 순서가 결과를 바꾸지 않는다."""
    a = _fact("f1", created_at="2026-08-01T00:00:00+00:00")
    b = _fact("f2", created_at="2026-08-03T00:00:00+00:00")

    forward = build_graph_document([a, b], existing=empty_document(NOW), settings=settings, now=NOW)
    reverse = build_graph_document([b, a], existing=empty_document(NOW), settings=settings, now=NOW)

    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_decay_clock_is_one_snapshot_per_batch(settings: Settings) -> None:
    """감쇠 시계는 배치당 1회 고정 — 관측별 현재시각을 쓰면 재생이 깨진다(REQ-PGRAPH-015)."""
    facts = [
        _fact("f1"),
        _fact("f2", fact="다른 취향", triples=[_triple("brand:애플", label="애플")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert {e.decay_evaluated_at for e in document.edges} == {NOW}


# ─────────── 병합 (REQ-PGRAPH-015) ───────────


def test_same_edge_key_merges_evidence(settings: Settings) -> None:
    facts = [
        _fact("f1", created_at="2026-08-01T00:00:00+00:00"),
        _fact("f2", fact="소니 또 선호", created_at="2026-08-05T00:00:00+00:00"),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert len(document.edges) == 1
    edge = document.edges[0]
    assert edge.evidence_count == 2
    assert edge.evidence_by_source == {"conversation": 2}
    assert sorted(edge.evidence_refs) == ["f1", "f2"]
    assert edge.first_observed_at == "2026-08-01T00:00:00+00:00"
    assert edge.last_observed_at == "2026-08-05T00:00:00+00:00"  # 최댓값


def test_evidence_refs_respect_configured_cap(settings: Settings) -> None:
    """참조 개수만 상한하고 카운트는 전부 센다 — 상한이 evidence_count 를 깎으면 확신도가 왜곡된다."""
    tight = Settings(_env_file=None, graph_evidence_refs_max=2)
    facts = [
        _fact(f"f{i}", fact=f"소니 선호 {i}", created_at=f"2026-08-0{i + 1}T00:00:00+00:00")
        for i in range(1, 5)
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=tight, now=NOW)

    edge = document.edges[0]
    assert edge.evidence_count == 4
    assert edge.evidence_refs == ["f3", "f4"]  # 최신 관측을 남긴다


def test_nodes_are_emitted_only_for_referenced_edges(settings: Settings) -> None:
    """node 는 edge 의 파생물이다(§5.1) — 참조 없는 노드는 존재하지 않는다."""
    document = build_graph_document(
        [_fact()], existing=empty_document(NOW), settings=settings, now=NOW
    )

    assert [n.node_id for n in document.nodes] == ["brand:소니"]


def test_facts_without_triples_only_count_as_unprojected(settings: Settings) -> None:
    """트리플 없는 fact 는 문서에 싣지 않고 개수만 센다(REQ-PGRAPH-004)."""
    facts = [_fact("f1"), _fact("f2", triples=[]), _fact("f3", triples=[])]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert document.unprojected_count == 2
    assert len(document.edges) == 1


def test_unprojected_count_is_recomputed_not_accumulated(settings: Settings) -> None:
    existing = build_graph_document(
        [_fact("f1", triples=[])], existing=empty_document(NOW), settings=settings, now=NOW
    )
    assert existing.unprojected_count == 1

    document = build_graph_document(
        [_fact("f1", triples=[])], existing=existing, settings=settings, now=NOW
    )

    assert document.unprojected_count == 1  # 누적이 아니라 매 배치 재계산


# ─────────── 감쇠·승격 히스테리시스 (REQ-PGRAPH-016) ───────────


def test_confidence_decays_with_elapsed_time(settings: Settings) -> None:
    """오래 재확인되지 않은 취향은 내려앉는다 — 감쇠가 없으면 강등이 도달 불가하다."""
    fresh = build_graph_document(
        [_fact("f1", created_at=NOW)], existing=empty_document(NOW), settings=settings, now=NOW
    )
    stale = build_graph_document(
        [_fact("f1", created_at="2026-01-01T00:00:00+00:00")],
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
    )

    assert stale.edges[0].confidence < fresh.edges[0].confidence


def _between_thresholds(settings: Settings) -> float:
    """승격 임계와 강등 임계 사이의 salience — 관측이 `now` 면 감쇠가 0이라 곧 confidence 다."""
    promote = settings.profile_gate_threshold
    demote = promote - settings.graph_demote_margin
    assert demote < promote
    return round((promote + demote) / 2, 4)


def test_hysteresis_keeps_promoted_edge_between_thresholds(settings: Settings) -> None:
    """이미 승격된 edge 는 승격 임계 아래로 내려가도 강등 임계 위면 유지한다(REQ-PGRAPH-016)."""
    mid = _between_thresholds(settings)
    existing = _document_with(promoted=True)

    document = build_graph_document(
        [_fact("f1", created_at=NOW, triples=[_triple(salience=mid)])],
        existing=existing,
        settings=settings,
        now=NOW,
    )

    edge = document.edges[0]
    assert edge.confidence == pytest.approx(mid)
    assert edge.promoted is True


def test_hysteresis_does_not_promote_new_edge_between_thresholds(settings: Settings) -> None:
    """같은 confidence 라도 미승격 edge 는 승격 임계를 넘어야 한다 — 두 임계가 실제로 다르다."""
    mid = _between_thresholds(settings)

    document = build_graph_document(
        [_fact("f1", created_at=NOW, triples=[_triple(salience=mid)])],
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
    )

    edge = document.edges[0]
    assert edge.confidence == pytest.approx(mid)
    assert edge.promoted is False


def test_edge_demotes_below_demote_threshold(settings: Settings) -> None:
    low = settings.profile_gate_threshold - settings.graph_demote_margin - 0.05
    existing = _document_with(promoted=True)

    document = build_graph_document(
        [_fact("f1", created_at=NOW, triples=[_triple(salience=low)])],
        existing=existing,
        settings=settings,
        now=NOW,
    )

    assert document.edges[0].promoted is False


def test_decay_alone_can_demote_a_promoted_edge(settings: Settings) -> None:
    """감쇠가 없으면 강등이 구조적으로 도달 불가하다 — 게이트가 임계 이상만 저장하기 때문이다."""
    existing = _document_with(promoted=True)
    old = "2020-01-01T00:00:00+00:00"  # 반감기를 한참 넘긴 관측

    document = build_graph_document(
        [_fact("f1", created_at=old, triples=[_triple(salience=0.95)])],
        existing=existing,
        settings=settings,
        now=NOW,
    )

    edge = document.edges[0]
    assert edge.confidence < settings.profile_gate_threshold - settings.graph_demote_margin
    assert edge.promoted is False


def test_valid_from_is_kept_across_batches(settings: Settings) -> None:
    """승격 시각은 재승격마다 갱신하지 않는다 — 언제부터 그 취향이었는지가 흐려진다."""
    existing = _document_with(confidence=0.9, promoted=True, valid_from="2026-07-01T00:00:00+00:00")

    document = build_graph_document([_fact("f1")], existing=existing, settings=settings, now=NOW)

    assert document.edges[0].valid_from == "2026-07-01T00:00:00+00:00"


# ─────────── 상태 보존 — 이 이슈의 존재 이유 ───────────


def test_suppressed_edge_is_not_reactivated_by_new_evidence(settings: Settings) -> None:
    """[HARD] 지운 취향은 재관측돼도 되살아나지 않는다(REQ-PGRAPH-022/023, AC-PROF-31)."""
    existing = _document_with(status="suppressed", suppressed_at=NOW, promoted=True)

    document = build_graph_document(
        [_fact("f1"), _fact("f2", fact="소니 또", created_at="2026-08-05T00:00:00+00:00")],
        existing=existing,
        settings=settings,
        now=NOW,
    )

    edge = document.edges[0]
    assert edge.status == "suppressed"
    assert edge.suppressed_at == NOW
    assert edge.evidence_count == 2  # 근거는 계속 쌓인다 — 상태만 안 바뀐다


def test_suppressed_edge_survives_when_evidence_disappears(settings: Settings) -> None:
    """근거 fact 가 cap 트리밍으로 밀려나도 tombstone 은 남는다.

    사라지면 같은 취향이 새 active edge 로 부활해 삭제가 조용히 무력화된다.
    """
    existing = _document_with(status="suppressed", suppressed_at=NOW)

    document = build_graph_document([], existing=existing, settings=settings, now=NOW)

    edge = _edge_by_key(document, "likes|brand:소니")
    assert edge is not None
    assert edge.status == "suppressed"
    assert edge.evidence_count == 0


def test_active_edge_without_evidence_is_dropped(settings: Settings) -> None:
    """반대로 그냥 active 인 edge 는 근거가 없어지면 사라진다 — 보존은 tombstone 한정이다."""
    existing = _document_with(status="active")

    document = build_graph_document([], existing=existing, settings=settings, now=NOW)

    assert document.edges == []


def test_conflicting_relations_supersede_loser_without_deleting(settings: Settings) -> None:
    """같은 node 에 likes vs avoids — 패자는 superseded 로 남고 삭제되지 않는다(REQ-PGRAPH-018)."""
    facts = [
        _fact("f1", created_at="2026-08-01T00:00:00+00:00", triples=[_triple(predicate="likes")]),
        _fact("f2", created_at="2026-08-05T00:00:00+00:00", triples=[_triple(predicate="avoids")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    winner = _edge_by_key(document, "avoids|brand:소니")  # recency-wins
    loser = _edge_by_key(document, "likes|brand:소니")
    assert winner is not None and loser is not None
    assert winner.status == "active"
    assert loser.status == "superseded"
    assert loser.superseded_by == winner.edge_id


def test_supersede_does_not_touch_unrelated_nodes(settings: Settings) -> None:
    facts = [
        _fact("f1", triples=[_triple(predicate="likes")]),
        _fact("f2", triples=[_triple("brand:애플", "avoids", label="애플")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert {e.status for e in document.edges} == {"active"}


# ─────────── 정렬·절단 ───────────


def test_edges_are_sorted_by_total_order(settings: Settings) -> None:
    """predicate 고정 순서 → last_observed_at 내림차순 → edge_id 오름차순(REQ-PGRAPH-003)."""
    facts = [
        _fact("f1", triples=[_triple("brand:a", "interestedIn", label="a", node_type="situation")]),
        _fact("f2", triples=[_triple("brand:b", "prefers", label="b", node_type="attribute")]),
        _fact("f3", triples=[_triple("brand:c", "likes", label="c")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert [e.predicate for e in document.edges] == ["prefers", "likes", "interestedIn"]
    assert [n.node_id for n in document.nodes] == sorted(n.node_id for n in document.nodes)


def test_truncation_keeps_tombstones_first(settings: Settings) -> None:
    """상한을 넘기면 자르되 tombstone 을 먼저 지킨다 — 절단으로 삭제가 되살아나면 안 된다."""
    tight = Settings(_env_file=None, profile_graph_max_edges=1)
    existing = _document_with(status="suppressed", suppressed_at=NOW)
    facts = [_fact("f1", triples=[_triple("brand:애플", label="애플")])]

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    assert len(document.edges) == 1
    assert document.edges[0].status == "suppressed"


# ─────────── revision ───────────


def test_revision_increases_on_substantive_change(settings: Settings) -> None:
    existing = empty_document(NOW)

    document = build_graph_document([_fact("f1")], existing=existing, settings=settings, now=NOW)

    assert document.revision == existing.revision + 1


def test_revision_is_stable_when_only_decay_moved_confidence(settings: Settings) -> None:
    """감쇠로 confidence 소수점만 움직이면 올리지 않는다.

    비교 단위는 원시 수치가 아니라 **와이어가 노출하는 3버킷 라벨**이다(§5.2). 시간이 흐르기만
    해도 confidence 는 늘 흔들리므로, 그걸 실질 변경으로 세면 revision 이 매 배치 올라 #150 의
    If-Match 토큰이 계속 무효가 된다.
    """
    first = build_graph_document(
        [_fact("f1")], existing=empty_document(NOW), settings=settings, now=NOW
    )

    second = build_graph_document(
        [_fact("f1")], existing=first, settings=settings, now="2026-08-07T00:00:00+00:00"
    )

    assert second.edges[0].confidence != first.edges[0].confidence  # 실제로 움직였다
    assert second.revision == first.revision  # 그래도 버킷은 그대로다


def test_revision_increases_when_confidence_crosses_a_bucket(settings: Settings) -> None:
    """버킷이 바뀌면 사용자가 볼 라벨이 바뀐 것이라 실질 변경이다."""
    low, high = settings.profile_graph_confidence_buckets
    first = build_graph_document(
        [_fact("f1", created_at=NOW, triples=[_triple(salience=high + 0.1)])],
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
    )

    second = build_graph_document(
        [_fact("f1", created_at=NOW, triples=[_triple(salience=low - 0.1)])],
        existing=first,
        settings=settings,
        now=NOW,
    )

    assert second.revision == first.revision + 1


def _document_with(**overrides: object) -> GraphDocument:
    from app.agents.profile.graph_models import make_edge_id

    edge_key = "likes|brand:소니"
    base: dict = {
        "edge_key": edge_key,
        "edge_id": make_edge_id(edge_key),
        "node_id": "brand:소니",
        "predicate": "likes",
        "status": "active",
        "promoted": False,
        "origin": "machine",
        "source_latest": "conversation",
        "confidence": 0.5,
        "evidence_count": 1,
        "evidence_by_source": {"conversation": 1},
        "evidence_refs": ["seed"],
        "first_observed_at": "2026-07-01T00:00:00+00:00",
        "last_observed_at": "2026-07-01T00:00:00+00:00",
        "decay_evaluated_at": "2026-07-01T00:00:00+00:00",
        "valid_from": None,
        "superseded_by": None,
        "suppressed_at": None,
        "user_intent": None,
        "challenge_count": 0,
        "derived_from_sensitive": False,
        "sensitive_topic": None,
    }
    base.update(overrides)
    return GraphDocument(
        revision=1,
        nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
        edges=[GraphEdge(**base)],
        unprojected_count=0,
        purged_at=None,
        updated_at="2026-07-01T00:00:00+00:00",
    )
