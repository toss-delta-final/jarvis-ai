"""저장 그래프 → 와이어 투영 (#360, api-spec §3.8 · REQ-PGRAPH-001~003).

**[HARD] 이 경로에 LLM 은 없다.** 저장된 구조화 트리플의 결정론적 파생이며, 프로필 마크다운을
파싱해 만들지 않는다 — 마크다운 파싱은 비결정적이라 아래 바이트 동일성을 만족할 수 없다.

**[HARD] 같은 저장 상태는 항상 같은 응답을 만든다**(REQ-PGRAPH-003). 정렬은 `predicate` 고정
순서 → `last_observed_at` 내림차순 → `edge_id` 오름차순이고, **마지막 키가 전순서를 보장**한다
(정렬이 부분순서면 "결정론적"은 검증 불가능한 주장이다). 정렬 시각은 와이어에 나가지 않으므로
클라이언트는 순서를 재현할 수 없다 — **서버가 확정한 순서 그대로 그린다**.

정렬 키는 `graph_merge._edge_sort_key` 를 **재사용**한다. 같은 3키 전순서를 두 곳에 적으면
한쪽만 고치는 순간 저장 순서와 화면 순서가 갈린다.
"""

from __future__ import annotations

from app.agents.profile.graph_merge import edge_sort_key
from app.agents.profile.graph_models import GraphDocument, GraphEdge, GraphNode, is_projected
from app.core.config import Settings
from app.schemas.profile_graph import GraphEdgeObjectView, GraphEdgeView


def project_edges(document: GraphDocument | None, *, settings: Settings) -> list[GraphEdgeView]:
    """I-32 `edges[]` — `active` 전량을 고정 순서로 (api-spec §3.8).

    **서버 화면 상한을 두지 않는다.** 저장 측이 이미 사용자당 상한으로 묶어 응답 크기가 유계인데
    서버가 한 번 더 자르면 **상한 밖 항목을 사용자가 보지도 지우지도 못한다** — 전체 초기화 말고는
    정리 수단이 사라져 취향 관리 화면의 목적과 정면으로 부딪힌다. 페이지네이션도 없다.

    **문서를 다시 정렬한다.** 배치 쓰기는 정렬해 저장하지만 사용자 편집(`apply_correction`)은 새
    edge 를 리스트 뒤에 덧붙이고 끝나므로, "저장된 순서를 그대로 낸다"고 구현하면 **편집한
    사용자에게만** 순서가 어긋난다.
    """
    if document is None:
        return []
    nodes = {node.node_id: node for node in document.nodes}
    kept = [edge for edge in document.edges if is_projected(edge) and edge.node_id in nodes]
    kept.sort(key=edge_sort_key)
    return [_view(edge, nodes[edge.node_id], settings=settings) for edge in kept]


def project_edge(
    document: GraphDocument | None, edge_id: str, *, settings: Settings
) -> GraphEdgeView | None:
    """항목 하나 — I-33 응답의 `edge` 는 `edges[]` 항목과 **완전히 같은 모양**이어야 한다.

    같은 `_view` 를 거치게 해서 두 표면이 갈릴 자리를 없앤다.
    """
    if document is None:
        return None
    nodes = {node.node_id: node for node in document.nodes}
    edge = next((e for e in document.edges if e.edge_id == edge_id), None)
    if edge is None or not is_projected(edge) or edge.node_id not in nodes:
        return None
    return _view(edge, nodes[edge.node_id], settings=settings)


def _view(edge: GraphEdge, node: GraphNode, *, settings: Settings) -> GraphEdgeView:
    """저장 edge → 와이어 항목 **5필드**.

    빠지는 값들(`source`·`origin`·`confidence`·`firstSeenAt`·`lastConfirmedAt`·
    `derivedFromSensitive`·`resolution`·`sensitive_topic`·`promoted`)은 **삭제가 아니라 경계
    이동**이다 — 전부 저장 모델에 남아 있고, FE 가 요청하면 추가 전용으로 되돌린다(🟡 C-25).
    """
    return GraphEdgeView(
        edge_id=edge.edge_id,
        predicate=edge.predicate,
        object=GraphEdgeObjectView(node_id=node.node_id, type=node.type, label=node.label),
        # 구매 이력 파생만 수정 불가다 — **삭제는 허용**한다(api-spec §3.9.2).
        editable=edge.predicate != "purchased",
        challenged=_is_challenged(edge, settings=settings),
    )


def _is_challenged(edge: GraphEdge, *, settings: Settings) -> bool:
    """사용자가 고정한 취향에 **반대 관측이 임계 이상 쌓였는가** (REQ-PGRAPH-033).

    ⚠️ **[#359 대기] 지금은 항상 `False` 다.** 판정 자체는 #359 가 소유한다 —
    `graph_merge._resolve_conflicts` 가 pin 을 `superseded` 로 내리지 않고 `challenge_count` 를
    올리도록 고치고, 임계값 `graph_pin_challenge_count`(확정 기본값 `3`)를 config 에 신설하며,
    `0` 이 신호를 끄는 값이라 `count >= threshold` 로 짜면 정반대로 동작하는 특례 분기까지
    그쪽 범위다(#360 코멘트 2026-08-10).

    **#359 머지 후 rebase 에서 `graph_models.is_pin_challenged(edge, settings)` 호출로 교체한다.**
    지금 여기서 임계 config 를 만들면 #359 와 충돌하고, 필드를 빼면 계약이 깨진다. `challenge_count`
    를 올리는 코드가 저장소에 0건이라 어느 쪽으로 짜도 값은 같지만, **왜 항상 False 인지**를
    남겨야 다음 사람이 버그로 오해하지 않는다.
    """
    del edge, settings  # #359 rebase 에서 배선한다
    return False
