"""사용자 그래프 변경의 순수 변형 로직 — 수정·삭제 (SPEC-PROFILE-GRAPH-149 §6.3·§6.4).

**순수 함수다.** `graph_merge` 와 같은 규율을 따른다 — 인자에 `llm`·`store` 가 없고 `now` 를
받으므로, 같은 입력이면 같은 문서가 나오고 "변경 판정에 LLM 을 쓰지 않는다"가 구조적으로
보장된다. 잠금·저널·CAS·감사 같은 부작용은 전부 `graph_journal.apply_edge_mutation` 몫이다.
그 분리 덕에 "무엇을 어떻게 바꾸는가"를 잠금 유무와 무관하게 검증할 수 있다.

반환이 `(document, MutationOutcome)` 인 이유는 outcome 이 **호출부의 분기를 결정**하기 때문이다.

- `changed=False` → no-op. 호출부는 원장 claim 을 풀고 감사 행을 남기지 않는다(REQ-PGRAPH-080).
- `edge_id_before/after`·`predicate`·`object_label` → 그대로 감사 컬럼이 된다. **`object_label`
  만 예외적으로 원문**인데, 호출부가 즉시 지문화하기 위한 재료이지 저장 대상이 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    GraphTombstone,
    UserIntent,
    make_edge_id,
    make_edge_key,
)
from app.core.config import Settings

# 사용자 편집의 확신도. 최상급을 박는 것은 REQ-PGRAPH-030 이고, 이후 기계 감쇠가 이 값을
# 건드리지 못하는 것은 REQ-PGRAPH-031(pin 중 confidence 변경 금지)이 보장한다.
_USER_CONFIDENCE = 1.0


@dataclass(frozen=True)
class MutationOutcome:
    """변형 결과 — 감사 컬럼과 no-op 분기의 단일 출처.

    `changed` 를 별도로 두는 이유: "문서가 달라졌나"를 호출부가 문서 비교로 다시 계산하면
    감쇠·시각 같은 비관측 필드까지 세어 no-op 을 변경으로 오판한다.
    """

    changed: bool
    edge_id_before: str | None = None
    edge_id_after: str | None = None
    predicate: str | None = None
    object_label: str | None = None  # 원문. 호출부가 즉시 지문화한다 — 저장 대상이 아니다
    merged: bool = False


def _unchanged(document: GraphDocument) -> tuple[GraphDocument, MutationOutcome]:
    return document, MutationOutcome(changed=False)


def _find(document: GraphDocument, edge_id: str) -> GraphEdge | None:
    return next((edge for edge in document.edges if edge.edge_id == edge_id), None)


def _label_of(document: GraphDocument, edge: GraphEdge) -> str | None:
    """edge 가 가리키는 노드의 라벨 원문 — **감사 지문의 재료**다.

    문서에서 사라지기 **전에** 뽑아야 한다. 지운 뒤에는 노드도 함께 없어져 "무엇을 지웠는지"를
    사후에 지문화할 방법이 없다. 호출부가 즉시 `identifier_fingerprint` 로 바꾼다.
    """
    node = next((n for n in document.nodes if n.node_id == edge.node_id), None)
    return node.label if node else None


def _prune_orphan_nodes(nodes: list[GraphNode], edges: list[GraphEdge]) -> list[GraphNode]:
    """참조가 끊긴 노드를 버린다 — **라벨은 노드에 있으므로 edge 만 지우면 원문이 남는다**.

    노드는 edge 의 파생물이라(§5.1) 자신을 참조하는 edge 가 없으면 존재할 이유도 없다.
    """
    referenced = {edge.node_id for edge in edges}
    return [node for node in nodes if node.node_id in referenced]


def apply_suppression(
    document: GraphDocument, *, edge_id: str, now: str
) -> tuple[GraphDocument, MutationOutcome]:
    """개별 삭제 — **즉시 물리 삭제 + 차단 표식**(#499, REQ-PGRAPH-025/026).

    서버에 임시 보관 단계를 두지 않는다(undo 창 폐기). 사용자가 "지웠다"고 믿는 문장의 원문을
    들고 있을 이유가 없고, 확인은 FE 확인창에서 이미 받았다.

    그러나 **그냥 지우기만 하면 다음 배치가 같은 fact 에서 되살린다** — 근거 fact 는 살아 있기
    때문이다. 라벨 없는 `GraphTombstone` 을 남겨 재파생을 막는다(`build_graph_document` 입구가
    이 목록을 본다).
    """
    edge = _find(document, edge_id)
    if edge is None:
        return _unchanged(document)

    edges = [candidate for candidate in document.edges if candidate.edge_id != edge_id]
    tombstones = _with_tombstone(document.tombstones, edge=edge, now=now)
    updated = document.model_copy(
        update={
            "edges": edges,
            "nodes": _prune_orphan_nodes(document.nodes, edges),
            "tombstones": tombstones,
        }
    )
    return updated, MutationOutcome(
        changed=True,
        edge_id_before=edge_id,
        edge_id_after=None,
        predicate=edge.predicate,
        object_label=_label_of(document, edge),
    )


def apply_correction(
    document: GraphDocument,
    *,
    edge_id: str,
    predicate: str,
    node: GraphNode,
    now: str,
    settings: Settings,
) -> tuple[GraphDocument, MutationOutcome]:
    """취향 수정 — **원자적 1회 변경**(REQ-PGRAPH-034).

    구 edge 억제와 대상 edge 고정이 한 번에 끝난다. 두 호출로 나누면 중간 상태와 충돌 창이 둘
    생기고, 감사도 두 행이 되어 "한 revision·한 감사 행"이 깨진다.

    `edgeId` 는 `(predicate, node_id)` 파생이라 **관계나 대상을 바꾸면 반드시 새 값**이다
    (api-spec §3.9.1). 그래서 outcome 의 before/after 가 다르고, 클라이언트는 응답의 새 값으로
    키를 교체해야 한다.

    새 트리플이 기존 edge 와 겹치면 **병합**한다 — 사용자가 이미 있던 취향으로 고쳤다고 그동안의
    관측을 버릴 이유가 없다(근거 합산, `last_observed_at` 최댓값).
    """
    edge = _find(document, edge_id)
    if edge is None:
        return _unchanged(document)

    new_key = make_edge_key(predicate, node.node_id)
    new_id = make_edge_id(new_key)
    if new_id == edge_id:
        # 같은 값으로 고치는 것은 상태 변경이 아니다 — 감사 행도 revision 도 남기지 않는다.
        return _unchanged(document)

    target = _find(document, new_id)
    intent = UserIntent(
        kind="correct", asserted_at=now, mutation_id=new_id, prior_predicate=edge.predicate
    )
    pinned = _pin(target or edge, key=new_key, edge_id=new_id, node=node, intent=intent, now=now)
    if target is not None:
        pinned = _merge_evidence(pinned, edge)

    edges = [
        candidate for candidate in document.edges if candidate.edge_id not in (edge_id, new_id)
    ]
    edges.append(pinned)
    nodes = _prune_orphan_nodes([*document.nodes, node], edges)
    # 대상 트리플에 옛 표식이 남아 있으면 걷는다. 안 걷으면 다음 배치가 방금 만든 edge 를 즉시
    # 지워 **사용자 입장에서 수정이 조용히 사라진다** — 재승격 금지(REQ-PGRAPH-026)는 *기계*
    # 재파생에 대한 조항이고, 여기는 명시적 사용자 동작이다.
    tombstones = [t for t in document.tombstones if t.edge_id != new_id]
    tombstones = _with_tombstone(tombstones, edge=edge, now=now)

    return document.model_copy(
        update={
            "edges": edges,
            "nodes": _dedupe_nodes(nodes),
            "tombstones": tombstones,
        }
    ), MutationOutcome(
        changed=True,
        edge_id_before=edge_id,
        edge_id_after=new_id,
        predicate=predicate,
        object_label=node.label,
        merged=target is not None,
    )


def _pin(
    base: GraphEdge,
    *,
    key: str,
    edge_id: str,
    node: GraphNode,
    intent: UserIntent,
    now: str,
) -> GraphEdge:
    """사용자 편집을 edge 에 새긴다 — `origin:"user"` · 최상급 확신도 · `user_intent`.

    REQ-PGRAPH-030. `promoted=True` 로 두는 것은 사용자가 명시한 취향이 게이트 판정을 기다릴
    이유가 없기 때문이다. `decay_evaluated_at` 을 `now` 로 맞춰 두지 않으면 다음 배치의 감쇠가
    옛 기준 시각으로 계산된다.
    """
    return base.model_copy(
        update={
            "edge_key": key,
            "edge_id": edge_id,
            "node_id": node.node_id,
            "predicate": key.split("|", 1)[0],
            "status": "active",
            "promoted": True,
            "origin": "user",
            "source_latest": "user",
            "confidence": _USER_CONFIDENCE,
            "superseded_by": None,
            "suppressed_at": None,
            "user_intent": intent,
            "decay_evaluated_at": now,
            "valid_from": base.valid_from or now,
        }
    )


def _merge_evidence(pinned: GraphEdge, absorbed: GraphEdge) -> GraphEdge:
    """구 edge 의 관측을 대상 edge 에 합친다 (api-spec §3.9.1 `merged`).

    `evidence_refs` 는 상한이 있는 저장용 목록이라 순서만 유지해 이어 붙이고 중복을 제거한다.
    `last_observed_at` 은 최댓값 — 관측 **사실**이라 병합으로 과거로 되돌아가면 안 된다.
    """
    by_source = dict(pinned.evidence_by_source)
    for source, count in absorbed.evidence_by_source.items():
        by_source[source] = by_source.get(source, 0) + count
    refs = list(dict.fromkeys([*pinned.evidence_refs, *absorbed.evidence_refs]))
    return pinned.model_copy(
        update={
            "evidence_count": pinned.evidence_count + absorbed.evidence_count,
            "evidence_by_source": by_source,
            "evidence_refs": refs,
            "first_observed_at": min(pinned.first_observed_at, absorbed.first_observed_at),
            "last_observed_at": max(pinned.last_observed_at, absorbed.last_observed_at),
        }
    )


def _with_tombstone(
    tombstones: list[GraphTombstone], *, edge: GraphEdge, now: str
) -> list[GraphTombstone]:
    """차단 표식을 더한다 — 이미 있으면 그대로 둔다(재전송이 표식을 쌓지 않게)."""
    if any(tombstone.edge_id == edge.edge_id for tombstone in tombstones):
        return list(tombstones)
    return [
        *tombstones,
        GraphTombstone(edge_id=edge.edge_id, suppressed_at=now, user_intent=edge.user_intent),
    ]


def _dedupe_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    """같은 `node_id` 가 둘이면 뒤엣것(이번 변경이 준 것)을 남긴다."""
    merged = {node.node_id: node for node in nodes}
    return sorted(merged.values(), key=lambda node: node.node_id)
