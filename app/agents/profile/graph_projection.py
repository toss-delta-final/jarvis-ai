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
from app.agents.profile.graph_models import (
    BAND_RE,
    GraphDocument,
    GraphEdge,
    GraphNode,
    is_pin_challenged,
    is_projected,
)
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


_BAND_UNITS = {"priceBand": "원", "ratingBand": "점"}


def _render_label(node: GraphNode) -> str:
    """저장 canonical → 사람이 읽는 문장 (#581, api-spec §3.8). 밴드가 아니면 원본 그대로.

    **저장을 바꾸지 않고 여기서 만드는 이유**는 `node_id` → `edge_id` 가 라벨 파생이기
    때문이다(REQ-PGRAPH-010). 문장을 저장하면 표시 규칙을 한 글자만 손봐도 같은 취향이
    다른 `edge_id` 를 얻어 **사용자가 지운 항목이 tombstone 을 비켜 되살아나고**,
    `_dedupe_nodes` 가 같은 `node_id` 중 나중 것만 남기므로 한 사용자 그래프에 옛 형식과
    새 형식이 섞인다. 감사 지문·PII 초크포인트도 라벨을 읽는다.

    **파싱 실패는 원문 폴백이다.** 저장 문서에는 파서를 안 거친 라벨이 들어올 수 있고
    (손으로 조립한 픽스처의 `ratingBand "4.5-5"`, 파서 개정 이전에 저장된 값),
    `int("4.5")` 는 ValueError 라 방어가 없으면 **조회 API 가 500 을 낸다** — 그러면
    사용자는 취향 화면을 통째로 잃고 그 항목을 지울 수도 없다. 못생긴 문자열이 낫다.

    **의미 검증은 하지 않는다** — `low >= high` 같은 판정은 쓰기 시점 resolver 의 몫이다.
    여기서 다시 재면 검증 로직이 둘이 되어 언젠가 서로 어긋난다.
    """
    unit = _BAND_UNITS.get(node.type)
    if unit is None:
        return node.label

    match = BAND_RE.match(node.label)
    if match is None:
        return node.label
    low, high = match.groups()
    if not low and not high:
        return node.label

    bounds = []
    if low:
        bounds.append(f"{int(low):,}{unit} 이상")
    if high:
        bounds.append(f"{int(high):,}{unit} 이하")
    return ", ".join(bounds)


def _view(edge: GraphEdge, node: GraphNode, *, settings: Settings) -> GraphEdgeView:
    """저장 edge → 와이어 항목 **5필드**.

    빠지는 값들(`source`·`origin`·`confidence`·`firstSeenAt`·`lastConfirmedAt`·
    `derivedFromSensitive`·`resolution`·`sensitive_topic`·`promoted`)은 **삭제가 아니라 경계
    이동**이다 — 전부 저장 모델에 남아 있고, FE 가 요청하면 추가 전용으로 되돌린다(🟡 C-25).
    """
    return GraphEdgeView(
        edge_id=edge.edge_id,
        predicate=edge.predicate,
        # 라벨만 파생값이다 — `node_id` 는 canonical 그대로여야 FE 가 §3.9.1 에 되실을 수 있다.
        object=GraphEdgeObjectView(node_id=node.node_id, type=node.type, label=_render_label(node)),
        # 구매 이력 파생만 수정 불가다 — **삭제는 허용**한다(api-spec §3.9.2).
        editable=edge.predicate != "purchased",
        # 판정은 `graph_models` 가 소유한다(#359) — 여기서는 호출만 한다. 임계 `0` 이 신호를
        # 끄는 값이라 `count >= threshold` 로 직접 짜면 정반대로 동작하는데, 그 특례가 그쪽에 있다.
        challenged=is_pin_challenged(edge, settings=settings),
    )
