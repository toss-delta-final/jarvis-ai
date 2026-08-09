"""사용자 그래프 변경의 순수 변형 로직 (#358, REQ-PGRAPH-030/031/034).

`graph_merge` 와 같은 규율을 따른다 — **인자에 llm·store·현재시각이 없다.** 그래서 "변경 판정에
LLM 을 쓰지 않는다"가 mock 단언이 아니라 구조적으로 보장되고, 잠금·트랜잭션 없이 "무엇을 어떻게
바꾸는가"만 따로 검증할 수 있다. 조립(잠금·저널·CAS)은 `graph_journal.apply_edge_mutation` 몫이다.

반환이 `(document, MutationOutcome)` 튜플인 이유는 outcome 이 **호출부의 두 분기를 결정**하기
때문이다: `changed=False` 면 no-op 이라 claim 을 풀고 감사 행을 남기지 않아야 하고(REQ-PGRAPH-080),
`edge_id_before/after`·`object_label` 은 그대로 감사 컬럼이 된다.
"""

from __future__ import annotations

import inspect

import pytest

from app.agents.profile.graph_merge import empty_document
from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    GraphTombstone,
    make_edge_id,
)
from app.agents.profile.graph_mutations import apply_correction, apply_suppression
from app.core.config import Settings

NOW = "2026-08-09T00:00:00+00:00"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _edge(label: str = "소니", *, predicate: str = "likes", **overrides: object) -> GraphEdge:
    node_id = f"brand:{label}"
    edge_key = f"{predicate}|{node_id}"
    base: dict = {
        "edge_key": edge_key,
        "edge_id": make_edge_id(edge_key),
        "node_id": node_id,
        "predicate": predicate,
        "status": "active",
        "promoted": True,
        "origin": "machine",
        "source_latest": "conversation",
        "confidence": 0.5,
        "evidence_count": 2,
        "evidence_by_source": {"conversation": 2},
        "evidence_refs": ["f1"],
        "first_observed_at": "2026-07-01T00:00:00+00:00",
        "last_observed_at": "2026-07-02T00:00:00+00:00",
        "decay_evaluated_at": "2026-07-02T00:00:00+00:00",
        "valid_from": "2026-07-02T00:00:00+00:00",
        "superseded_by": None,
        "suppressed_at": None,
        "user_intent": None,
        "challenge_count": 0,
        "derived_from_sensitive": False,
        "sensitive_topic": None,
    }
    base.update(overrides)
    return GraphEdge(**base)


def _node(label: str = "소니") -> GraphNode:
    return GraphNode(node_id=f"brand:{label}", type="brand", label=label, verified=False)


def _document(edges: list[GraphEdge], *, tombstones: list[GraphTombstone] | None = None):
    # `node_id` 로 접는다 — 한 노드를 여러 edge 가 가리키는 것이 정상이고(같은 브랜드에 대한
    # likes·avoids), 그때 노드를 중복 생성하면 실제 문서에 없는 상태를 재현하게 된다.
    nodes = {edge.node_id: _node(edge.node_id.split(":", 1)[1]) for edge in edges}
    return GraphDocument(
        revision=42,
        nodes=list(nodes.values()),
        edges=edges,
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=NOW,
        tombstones=list(tombstones or []),
    )


# ─────────── 구조적 보장 ───────────


def test_mutations_have_no_llm_or_io_seam() -> None:
    """변형에 LLM·저장소·현재시각을 몰래 넣을 자리가 없다 (graph_merge 와 같은 규율).

    `now` 는 인자로 받는다 — 내부에서 읽으면 같은 입력이 다른 결과를 낸다.
    """
    for fn in (apply_correction, apply_suppression):
        params = set(inspect.signature(fn).parameters)
        assert "llm" not in params and "store" not in params
        assert "now" in params


# ─────────── 삭제(edgeDelete) ───────────


def test_suppression_removes_the_edge_and_its_orphan_node() -> None:
    """지우면 edge 도 노드도 사라진다 — **라벨은 노드에 있으므로 edge 만 지우면 원문이 남는다**."""
    document = _document([_edge("소니")])

    updated, outcome = apply_suppression(
        document, edge_id=make_edge_id("likes|brand:소니"), now=NOW
    )

    assert updated.edges == []
    assert updated.nodes == []
    assert "소니" not in repr(updated.model_dump(mode="json"))
    assert outcome.changed is True
    assert outcome.edge_id_before == make_edge_id("likes|brand:소니")
    assert outcome.edge_id_after is None


def test_suppression_leaves_a_tombstone_that_blocks_re_derivation() -> None:
    """표식이 남아야 다음 배치가 같은 취향을 되살리지 않는다 (AC-PROF-31)."""
    document = _document([_edge("소니")])

    updated, _ = apply_suppression(document, edge_id=make_edge_id("likes|brand:소니"), now=NOW)

    assert [t.edge_id for t in updated.tombstones] == [make_edge_id("likes|brand:소니")]
    assert updated.tombstones[0].suppressed_at == NOW


def test_suppression_keeps_nodes_that_other_edges_still_use() -> None:
    """같은 노드를 쓰는 다른 edge 가 있으면 노드는 남는다 — 살아 있는 취향을 깨면 안 된다."""
    document = _document([_edge("소니", predicate="likes"), _edge("소니", predicate="avoids")])

    updated, _ = apply_suppression(document, edge_id=make_edge_id("likes|brand:소니"), now=NOW)

    assert [e.edge_key for e in updated.edges] == ["avoids|brand:소니"]
    assert [n.node_id for n in updated.nodes] == ["brand:소니"]


def test_suppressing_an_absent_edge_is_a_no_op() -> None:
    """없는 대상은 변경이 아니다 — 호출부가 `404` 로 옮기고 감사 행을 남기지 않는다."""
    document = _document([_edge("소니")])

    updated, outcome = apply_suppression(document, edge_id="e_nonexistent", now=NOW)

    assert outcome.changed is False
    assert updated.model_dump(mode="json") == document.model_dump(mode="json")


def test_suppressing_twice_is_a_no_op_the_second_time() -> None:
    """이미 지운 대상의 재전송은 상태를 바꾸지 않는다 — 표식이 중복으로 쌓이지도 않는다."""
    document = _document([_edge("소니")])
    once, _ = apply_suppression(document, edge_id=make_edge_id("likes|brand:소니"), now=NOW)

    twice, outcome = apply_suppression(once, edge_id=make_edge_id("likes|brand:소니"), now=NOW)

    assert outcome.changed is False
    assert len(twice.tombstones) == 1


# ─────────── 수정(edgeUpdate) ───────────


def test_correction_is_one_atomic_change(settings: Settings) -> None:
    """[HARD] 구 edge 억제와 새 edge 고정이 **한 번의 반환**으로 끝난다 (REQ-PGRAPH-034).

    두 호출로 나누면 중간 상태와 충돌 창이 둘 생긴다. 그래서 outcome 하나가 before/after 를
    모두 들고 나온다 — 감사도 한 행이다.
    """
    document = _document([_edge("소니", predicate="likes")])

    updated, outcome = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.changed is True
    assert outcome.edge_id_before == make_edge_id("likes|brand:소니")
    assert outcome.edge_id_after == make_edge_id("avoids|brand:소니")
    assert outcome.edge_id_before != outcome.edge_id_after  # edgeId 가 바뀐다(api-spec §3.9.1)
    assert [e.edge_key for e in updated.edges] == ["avoids|brand:소니"]


def test_correction_pins_the_new_edge_as_user_origin(settings: Settings) -> None:
    """사용자 수정은 `origin:"user"`·최상급 확신도·`user_intent` 를 동반한다 (REQ-PGRAPH-030)."""
    document = _document([_edge("소니")])

    updated, _ = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    edge = updated.edges[0]
    assert edge.origin == "user"
    assert edge.source_latest == "user"
    assert edge.confidence == 1.0
    assert edge.status == "active"
    assert edge.user_intent is not None
    assert edge.user_intent.kind == "correct"
    assert edge.user_intent.prior_predicate == "likes"
    assert edge.user_intent.expires_at is None  # [HARD] pin 은 만료되지 않는다(REQ-PGRAPH-032)


def test_correction_leaves_a_tombstone_for_the_old_triple(settings: Settings) -> None:
    """구 edge 는 억제된다 — 표식이 없으면 다음 배치가 원래 취향을 되살린다.

    수정의 절반이 "구 edge 억제"라는 것이 REQ-PGRAPH-034 의 문면이다.
    """
    document = _document([_edge("소니")])

    updated, _ = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert [t.edge_id for t in updated.tombstones] == [make_edge_id("likes|brand:소니")]


def test_correction_merges_into_an_existing_target_edge(settings: Settings) -> None:
    """새 트리플이 기존 edge 와 겹치면 **병합**하고 그 사실을 알린다 (api-spec §3.9.1 `merged`).

    근거는 합산하고 `lastConfirmedAt` 은 최댓값을 쓴다 — 사용자가 이미 있던 취향으로 고쳤다고
    그동안의 관측을 버릴 이유가 없다.
    """
    document = _document(
        [
            _edge(
                "소니",
                predicate="likes",
                evidence_count=2,
                last_observed_at="2026-07-02T00:00:00+00:00",
            ),
            _edge(
                "소니",
                predicate="avoids",
                evidence_count=3,
                last_observed_at="2026-07-05T00:00:00+00:00",
            ),
        ]
    )

    updated, outcome = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.merged is True
    assert len(updated.edges) == 1
    merged = updated.edges[0]
    assert merged.evidence_count == 5  # 2 + 3
    assert merged.last_observed_at == "2026-07-05T00:00:00+00:00"  # 최댓값
    assert merged.user_intent is not None  # 병합돼도 사용자 의도는 걸린다


def test_correcting_an_absent_edge_is_a_no_op(settings: Settings) -> None:
    """없는 대상은 변경이 아니다 — 호출부가 `404` 로 옮긴다."""
    document = _document([_edge("소니")])

    updated, outcome = apply_correction(
        document,
        edge_id="e_nonexistent",
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.changed is False
    assert updated.model_dump(mode="json") == document.model_dump(mode="json")


def test_correcting_to_the_same_triple_is_a_no_op(settings: Settings) -> None:
    """같은 값으로 고치는 것은 상태 변경이 아니다 — 감사 행도 revision 도 남기지 않는다.

    REQ-PGRAPH-080 이 "상태를 바꾸지 않는 요청은 감사 행을 남기지 않는다"이므로, 여기서
    `changed=False` 를 돌려주지 않으면 no-op 재전송이 감사를 오염시킨다.
    """
    document = _document([_edge("소니", predicate="likes")])

    updated, outcome = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="likes",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.changed is False
    assert updated.model_dump(mode="json") == document.model_dump(mode="json")


def test_correction_does_not_resurrect_a_tombstoned_target(settings: Settings) -> None:
    """이미 지운 트리플로 고치면 그 표식을 되살리지 않는다.

    표식이 남은 채 새 edge 가 생기면 다음 배치가 그 edge 를 즉시 지워 사용자 입장에서는 수정이
    조용히 사라진다 — 명시적 사용자 동작이므로 표식을 걷어야 한다(REQ-PGRAPH-026 의 재승격
    금지는 *기계* 재파생에 대한 것이다).
    """
    document = _document(
        [_edge("소니", predicate="likes")],
        tombstones=[GraphTombstone(edge_id=make_edge_id("avoids|brand:소니"), suppressed_at=NOW)],
    )

    updated, outcome = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.changed is True
    assert [e.edge_key for e in updated.edges] == ["avoids|brand:소니"]
    # 새 대상의 표식은 걷히고, 방금 지운 구 트리플의 표식만 남는다.
    assert [t.edge_id for t in updated.tombstones] == [make_edge_id("likes|brand:소니")]


def test_outcome_carries_the_label_for_fingerprinting_not_storage(settings: Settings) -> None:
    """outcome 은 라벨 **원문**을 들고 나온다 — 호출부가 즉시 지문화해 감사에 넣기 위해서다.

    문서에는 안 들어간다(위 테스트들이 단언). 여기서 라벨을 안 주면 호출부가 감사의 `object_fp`
    를 만들 재료가 없어, 지운 대상이 무엇이었는지 사후 대조가 불가능해진다.
    """
    document = _document([_edge("소니")])

    _, outcome = apply_correction(
        document,
        edge_id=make_edge_id("likes|brand:소니"),
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.object_label == "소니"
    assert outcome.predicate == "avoids"


def test_empty_document_correction_is_a_no_op(settings: Settings) -> None:
    """빈 문서에서도 죽지 않는다 — 경계에서 예외가 나면 호출부가 503 으로 오인한다."""
    updated, outcome = apply_correction(
        empty_document(NOW),
        edge_id="e_whatever",
        predicate="avoids",
        node=_node("소니"),
        now=NOW,
        settings=settings,
    )

    assert outcome.changed is False
    assert updated.edges == []
