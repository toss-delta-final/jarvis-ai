"""결정론적 병합 엔진 — 확정 트리플을 그래프 문서로 (SPEC-PROFILE-GRAPH-149 §6.2·§6.3).

**순수 함수다.** 인자에 `llm`·`embed`·`store` 가 없어서 "병합·최신성·승격 판정에 LLM 을 쓰지
않는다"(REQ-PROF-032/033)가 mock 단언이 아니라 구조적으로 보장된다. `now` 를 인자로 받는 것도
같은 이유다 — 내부에서 현재시각을 읽으면 같은 관측을 두 번 재생해도 결과가 달라진다
(REQ-PGRAPH-015 재생 동일성).

이 파일이 지키는 불변식 중 조용히 깨지기 쉬운 것 둘:
  - **tombstone 은 근거가 사라져도 남는다.** fact cap 트리밍(`profile_max_facts`)으로 증거가
    밀려났다고 `suppressed` edge 를 지우면, 같은 취향이 다음 배치에 새 `active` edge 로 부활해
    삭제가 조용히 무력화된다(AC-PROF-31).
  - **절단도 사용자 삭제를 먼저 지킨다.** 상한을 넘겨 자를 때 tombstone 이 먼저 잘리면 같은 일이
    벌어진다 — 그래서 `suppressed`·pin 은 상한보다 우선한다(`_truncate`).

`logger` 는 있지만 판정 입력이 아니다 — 절단이 상한을 넘겨 보존한 사실만 알린다. 같은 관측을
두 번 재생하면 로그와 무관하게 같은 문서가 나온다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    Predicate,
)
from app.agents.profile.store import FactRecord
from app.core.config import Settings

# REQ-PGRAPH-003 정렬의 predicate 고정 순서. 마지막 키(edge_id)까지 가야 **전순서**가 되고,
# 부분순서면 "결정론적"은 검증 불가능한 주장이 된다.
_PREDICATE_ORDER: dict[str, int] = {
    "prefers": 0,
    "likes": 1,
    "avoids": 2,
    "interestedIn": 3,
    "purchased": 4,
}
# 같은 노드를 두고 서로 못 서는 관계쌍. 승자만 active 로 남고 패자는 superseded 다.
_CONFLICTING: frozenset[frozenset[str]] = frozenset({frozenset({"likes", "avoids"})})
_ROUND = 6  # 고정 소수점 — 부동소수 꼬리가 재생 동일성을 깨지 않게(REQ-PGRAPH-015)
_SECONDS_PER_DAY = 86400.0

logger = logging.getLogger(__name__)


def empty_document(now: str) -> GraphDocument:
    """첫 배치 이전 상태. `revision=0` 에서 시작해 실질 변경마다 오른다."""
    return GraphDocument(
        revision=0, nodes=[], edges=[], unprojected_count=0, purged_at=None, updated_at=now
    )


@dataclass
class _Observation:
    """트리플 하나가 관측된 한 건. 정렬 키가 `(observed_at, fact_key)` 다(REQ-PGRAPH-015)."""

    observed_at: str
    fact_key: str
    node: GraphNode
    predicate: Predicate
    edge_key: str
    edge_id: str
    salience: float
    source: str


def build_graph_document(
    facts: Sequence[FactRecord],
    *,
    existing: GraphDocument,
    settings: Settings,
    now: str,
) -> GraphDocument:
    """확정 트리플 + 기존 문서 → 새 문서. LLM·임베딩·저장소 접근 0회."""
    observations, unprojected = _collect(facts)
    prior = {edge.edge_key: edge for edge in existing.edges}

    grouped: dict[str, list[_Observation]] = {}
    for obs in sorted(observations, key=lambda o: (o.observed_at, o.fact_key)):
        grouped.setdefault(obs.edge_key, []).append(obs)

    edges = [
        _merge_edge(obs_list, prior=prior.get(key), settings=settings, now=now)
        for key, obs_list in grouped.items()
    ]
    edges.extend(_carried_tombstones(existing, seen=set(grouped)))
    edges = _resolve_conflicts(edges)
    edges = _truncate(edges, settings.profile_graph_max_edges)

    nodes = _nodes_for(edges, observations, existing)
    document = GraphDocument(
        revision=existing.revision,
        nodes=sorted(nodes, key=lambda n: n.node_id),
        edges=sorted(edges, key=_edge_sort_key),
        unprojected_count=unprojected,
        purged_at=existing.purged_at,
        updated_at=now,
    )
    return _with_revision(document, existing, settings=settings)


def _collect(facts: Sequence[FactRecord]) -> tuple[list[_Observation], int]:
    """fact 목록에서 관측을 뽑고, 트리플을 못 만든 fact 개수를 센다(REQ-PGRAPH-004).

    `unprojected_count` 는 **매 배치 처음부터 재계산**한다 — 누적하면 같은 fact 를 두 번 세고
    재생 동일성이 깨진다. 내용은 어떤 형태로도 문서에 싣지 않는다(개수만).
    """
    observations: list[_Observation] = []
    unprojected = 0
    for record in facts:
        made = False
        for triple in record.graph_triples:
            obs = _observation(record, triple)
            if obs is not None:
                observations.append(obs)
                made = True
        if not made:
            unprojected += 1
    return observations, unprojected


def _observation(record: FactRecord, triple: dict) -> _Observation | None:
    """저장된 트리플 payload → 관측. 모양이 깨진 항목은 조용히 버린다(배치를 죽이지 않는다)."""
    try:
        node = GraphNode.model_validate(triple["node"])
        return _Observation(
            observed_at=record.created_at,
            fact_key=record.fact_key,
            node=node,
            predicate=triple["predicate"],
            edge_key=triple["edge_key"],
            edge_id=triple["edge_id"],
            salience=float(triple.get("salience") or 0.0),
            source=str(triple.get("source") or "conversation"),
        )
    except Exception:  # noqa: BLE001 - 구 형식·손상 payload 는 unprojected 취급
        return None


def _merge_edge(
    observations: list[_Observation],
    *,
    prior: GraphEdge | None,
    settings: Settings,
    now: str,
) -> GraphEdge:
    """같은 `edge_key` 로 수렴한 관측을 하나의 edge 로 (REQ-PGRAPH-015).

    `status`·`suppressed_at`·`user_intent` 는 **기존 값을 그대로 승계한다** — 새 근거가 들어왔다고
    사용자가 지운 취향을 되살리지 않는다(REQ-PGRAPH-022/023). `promoted` 는 `status` 와 직교라
    별도로 판정한다.
    """
    head = observations[-1]  # 정렬된 마지막 = 최신 관측
    by_source: dict[str, int] = {}
    for obs in observations:
        by_source[obs.source] = by_source.get(obs.source, 0) + 1

    confidence = _confidence(observations, settings=settings, now=now)
    promoted = _promoted(confidence, prior=prior, settings=settings)

    first_observed = observations[0].observed_at
    if prior is not None:
        first_observed = min(first_observed, prior.first_observed_at)

    return GraphEdge(
        edge_key=head.edge_key,
        edge_id=head.edge_id,
        node_id=head.node.node_id,
        predicate=head.predicate,
        status=prior.status if prior else "active",
        promoted=promoted,
        origin=prior.origin if prior else "machine",
        source_latest=head.source,  # type: ignore[arg-type]
        confidence=confidence,
        evidence_count=len(observations),
        evidence_by_source=by_source,
        # 상한은 **참조에만** 걸고 카운트는 전부 센다 — 상한이 evidence_count 를 깎으면
        # 확신도가 설정값에 따라 달라진다. 최신 관측을 남긴다(오래된 근거가 먼저 밀린다).
        evidence_refs=[o.fact_key for o in observations][-settings.graph_evidence_refs_max :],
        first_observed_at=first_observed,
        last_observed_at=max(o.observed_at for o in observations),
        decay_evaluated_at=now,
        valid_from=_valid_from(prior, promoted=promoted, now=now),
        superseded_by=None,  # 아래 _resolve_conflicts 가 다시 채운다
        suppressed_at=prior.suppressed_at if prior else None,
        user_intent=prior.user_intent if prior else None,
        challenge_count=prior.challenge_count if prior else 0,
        derived_from_sensitive=prior.derived_from_sensitive if prior else False,
        sensitive_topic=prior.sensitive_topic if prior else None,
    )


def _confidence(observations: list[_Observation], *, settings: Settings, now: str) -> float:
    """감쇠 가중 EMA — 관측을 시간순으로 처음부터 재계산한다.

    증분 갱신이 아니라 전량 재계산이라 재생 동일성이 자동으로 성립한다. 순서가 실제로
    결과를 바꾸므로 REQ-PGRAPH-015 의 "(observed_at, fact_key) 오름차순 처리"가 장식이 아니다.

    감쇠가 **강등의 전제**다: 게이트가 `salience >= profile_gate_threshold` 인 관측만 저장하므로,
    감쇠가 없으면 confidence 가 승격 임계 아래로 내려갈 수 없어 강등이 도달 불가해진다.
    """
    half_life = settings.graph_decay_half_life_days
    confidence = 0.0
    previous: str | None = None
    for obs in observations:
        if previous is not None:
            confidence *= _decay_factor(previous, obs.observed_at, half_life)
        confidence = round(confidence + (1.0 - confidence) * obs.salience, _ROUND)
        previous = obs.observed_at
    if previous is not None:
        confidence = round(confidence * _decay_factor(previous, now, half_life), _ROUND)
    return max(0.0, min(1.0, confidence))


def _decay_factor(start: str, end: str, half_life_days: float) -> float:
    days = _elapsed_days(start, end)
    if days <= 0.0:
        return 1.0
    return 0.5 ** (days / half_life_days)


def _elapsed_days(start: str, end: str) -> float:
    """못 재면 0일로 본다 — 재지 못한 시간을 감쇠로 추측하지 않는다.

    `TypeError` 를 함께 잡는 이유는 `datetime` 뺄셈이 **naive-aware 혼합에서 `ValueError` 가
    아니라 `TypeError`** 를 내기 때문이다. 지금은 양쪽 소스가 모두 aware 지만(`_now_iso` 와
    store 의 `created_at`), 파싱만 막는 가드는 그 전제가 깨지는 순간 배치를 통째로 죽인다.
    """
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (ValueError, TypeError):
        return 0.0
    return delta.total_seconds() / _SECONDS_PER_DAY


def _promoted(confidence: float, *, prior: GraphEdge | None, settings: Settings) -> bool:
    """승격·강등 히스테리시스 (REQ-PGRAPH-016).

    승격 임계는 **기존 게이트 임계를 재사용한다** — 두 번째 임계 키를 만들지 않는다(§11).
    강등 임계만 `graph_demote_margin` 만큼 낮다. 두 임계가 같으면 경계값에서 배치마다 깜빡여
    사용자에게는 항목이 나타났다 사라지는 것으로 보인다.
    """
    promote = settings.profile_gate_threshold
    if prior is not None and prior.promoted:
        return confidence >= promote - settings.graph_demote_margin
    return confidence >= promote


def _valid_from(prior: GraphEdge | None, *, promoted: bool, now: str) -> str | None:
    """승격 시각. 이미 승격돼 있었으면 **갱신하지 않는다** — 언제부터인지가 흐려진다."""
    if not promoted:
        return prior.valid_from if prior else None
    if prior is not None and prior.valid_from:
        return prior.valid_from
    return now


def _carried_tombstones(existing: GraphDocument, *, seen: set[str]) -> list[GraphEdge]:
    """이번 배치에 근거가 없는 edge 중 **보존 대상만** 살린다.

    `suppressed`·`superseded`·사용자 pin 은 근거가 0이어도 남긴다. 사라지면 같은 취향이 다음
    배치에 새 `active` edge 로 부활해 삭제가 조용히 무력화된다(AC-PROF-31).
    반대로 그냥 `active` 인 edge 는 근거가 없어지면 함께 사라진다 — 보존은 tombstone 한정이다.
    """
    carried: list[GraphEdge] = []
    for edge in existing.edges:
        if edge.edge_key in seen:
            continue
        if edge.status == "active" and edge.user_intent is None:
            continue
        carried.append(
            edge.model_copy(
                update={"evidence_count": 0, "evidence_refs": [], "evidence_by_source": {}}
            )
        )
    return carried


def _resolve_conflicts(edges: list[GraphEdge]) -> list[GraphEdge]:
    """같은 node 를 두고 상충하는 관계는 패자를 `superseded` 로 (REQ-PGRAPH-018) — **삭제하지 않는다**.

    승자 선정은 recency-wins 다(REQ-PROF-033): `(last_observed_at, confidence, edge_id)` 최댓값.
    마지막 키까지 가야 동률에서도 전순서가 성립한다.
    """
    by_node: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        by_node.setdefault(edge.node_id, []).append(edge)

    resolved: dict[str, GraphEdge] = {e.edge_key: e for e in edges}
    for node_edges in by_node.values():
        candidates = [e for e in node_edges if e.status in ("active", "superseded")]
        for pair in _CONFLICTING:
            clashing = [e for e in candidates if e.predicate in pair]
            if len(clashing) < 2:
                continue
            winner = max(clashing, key=lambda e: (e.last_observed_at, e.confidence, e.edge_id))
            for edge in clashing:
                if edge.edge_key == winner.edge_key:
                    resolved[edge.edge_key] = edge.model_copy(
                        update={"status": "active", "superseded_by": None}
                    )
                else:
                    resolved[edge.edge_key] = edge.model_copy(
                        update={"status": "superseded", "superseded_by": winner.edge_id}
                    )
    return list(resolved.values())


def _is_user_tombstone(edge: GraphEdge) -> bool:
    """복구 경로가 없는 tombstone — 사용자 삭제(`suppressed`)와 pin(`user_intent`)."""
    return edge.status == "suppressed" or edge.user_intent is not None


def _truncate(edges: list[GraphEdge], limit: int) -> list[GraphEdge]:
    """상한 초과 시 절단 — **사용자 삭제는 상한보다 우선한다**.

    세 등급이고, 등급이 낮은 쪽부터 밀린다:

    1. `suppressed`·pin — **자르지 않는다.** 잘리면 `_carried_tombstones` 가 보존할 대상을 잃고
       같은 취향이 다음 배치에 새 `active` 로 부활한다(AC-PROF-31). 이쪽만 복구 경로가 없다.
    2. `superseded` — 자를 수 있다. 근거 fact 가 남아 있으면 다음 배치에 다시 파생되고
       `_resolve_conflicts` 가 같은 판정을 반복하므로 **자기복구**된다.
    3. `active` — 남는 자리를 확신도 높은 순으로 채운다.

    1등급만으로 상한을 넘으면 상한을 넘긴 채 보존하고 경고한다 — 저장 폭주 방어보다 삭제 실효가
    앞선다. 그 상태는 정리·초기화 신호이지(REQ-PGRAPH-005) 조용히 지울 근거가 아니다.
    동률은 `edge_id` 로 갈라 절단 결과까지 결정론적으로 만든다.
    """
    if len(edges) <= limit:
        return edges

    kept = [e for e in edges if _is_user_tombstone(e)]
    if len(kept) >= limit:
        if len(kept) > limit:
            logger.warning(
                "profile_graph_protected_over_cap",
                extra={
                    "protected": len(kept),
                    "limit": limit,
                    "dropped": len(edges) - len(kept),
                },
            )
        return kept

    rest = sorted(
        (e for e in edges if not _is_user_tombstone(e)),
        # `status == "active"` 가 False(0) < True(1) 라 superseded 가 앞선다.
        key=lambda e: (e.status == "active", -e.confidence, e.edge_id),
    )
    return kept + rest[: limit - len(kept)]


def _nodes_for(
    edges: list[GraphEdge], observations: list[_Observation], existing: GraphDocument
) -> list[GraphNode]:
    """살아남은 edge 가 참조하는 노드만 (§5.1 — node 는 edge 의 파생물이다).

    이번 배치 관측을 우선 쓰고, 근거가 없는 tombstone 은 기존 문서의 노드로 채운다.
    """
    latest = {obs.node.node_id: obs.node for obs in observations}
    carried = {node.node_id: node for node in existing.nodes}
    wanted = {edge.node_id for edge in edges}
    return [
        latest.get(node_id) or carried[node_id]
        for node_id in wanted
        if node_id in latest or node_id in carried
    ]


def _edge_sort_key(edge: GraphEdge) -> tuple:
    """REQ-PGRAPH-003 전순서: predicate 고정 순서 → last_observed_at 내림차순 → edge_id 오름차순."""
    return (
        _PREDICATE_ORDER.get(edge.predicate, len(_PREDICATE_ORDER)),
        _Descending(edge.last_observed_at),
        edge.edge_id,
    )


@dataclass(frozen=True)
class _Descending:
    """문자열을 내림차순으로 정렬하기 위한 래퍼(reverse=True 는 앞 키까지 뒤집는다)."""

    value: str

    def __lt__(self, other: "_Descending") -> bool:
        return self.value > other.value


def _with_revision(
    document: GraphDocument, existing: GraphDocument, *, settings: Settings
) -> GraphDocument:
    """**관측 가능한** 내용이 바뀔 때만 `revision` 을 올린다.

    비교 단위가 원시 `confidence` 가 아니라 **와이어가 노출하는 3버킷 라벨**인 것이 요점이다
    (§5.2 "내부 수치 — 와이어에는 3버킷만"). 감쇠 때문에 confidence 는 시간이 흐르기만 해도
    소수점 아래가 늘 흔들리는데, 그걸 실질 변경으로 세면 revision 이 **매 배치** 올라 #150 의
    `If-Match` 토큰이 계속 무효가 된다. 같은 이유로 `decay_evaluated_at`·`updated_at` 도 뺀다.

    비교·충돌 응답·초기화 후 비되돌림 집행은 #358 소관이고 여기서는 증가만 한다.
    """
    if _fingerprint(document, settings) == _fingerprint(existing, settings):
        return document.model_copy(update={"revision": existing.revision})
    return document.model_copy(update={"revision": existing.revision + 1})


def _confidence_bucket(confidence: float, bounds: list[float]) -> int:
    """확신도를 와이어 노출 단위(3버킷)로. 경계 값 자체는 계약이 아니다(§6 공통 규약)."""
    return sum(1 for bound in bounds if confidence >= bound)


def _fingerprint(document: GraphDocument, settings: Settings) -> str:
    bounds = settings.profile_graph_confidence_buckets
    volatile = {"decay_evaluated_at", "confidence"}
    edges = []
    for edge in document.edges:
        payload = {k: v for k, v in edge.model_dump(mode="json").items() if k not in volatile}
        payload["confidence_bucket"] = _confidence_bucket(edge.confidence, bounds)
        edges.append(payload)
    nodes = [node.model_dump(mode="json") for node in document.nodes]
    return repr((nodes, edges, document.unprojected_count, document.purged_at))
