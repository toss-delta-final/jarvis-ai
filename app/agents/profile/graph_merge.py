"""결정론적 병합 엔진 — 확정 트리플을 그래프 문서로 (SPEC-PROFILE-GRAPH-149 §6.2·§6.3).

**순수 함수다.** 인자에 `llm`·`embed`·`store` 가 없어서 "병합·최신성·승격 판정에 LLM 을 쓰지
않는다"(REQ-PROF-032/033)가 mock 단언이 아니라 구조적으로 보장된다. `now` 를 인자로 받는 것도
같은 이유다 — 내부에서 현재시각을 읽으면 같은 관측을 두 번 재생해도 결과가 달라진다
(REQ-PGRAPH-015 재생 동일성).

이 파일이 지키는 불변식 중 조용히 깨지기 쉬운 것 셋:
  - **지운 취향은 재파생하지 않는다.** 사용자 삭제는 edge 를 물리 삭제하지만 근거 fact 는 살아
    있으므로, `existing.tombstones` 로 차단하지 않으면 다음 배치가 같은 트리플에서 같은
    `edge_id` 를 다시 만들어 삭제가 조용히 무력화된다(AC-PROF-31).
  - **tombstone 은 이월된다.** 배치는 tombstone 을 만들지도 지우지도 않지만(그건 #358 의 사용자
    변경 경로와 전체 초기화 몫), 새 문서에 옮겨 싣지 않으면 차단 목록을 잃는다.
  - **절단은 pin 을 먼저 지킨다.** 사용자가 고정한 edge 가 상한 때문에 밀리면 그 편집이 사라진다
    (REQ-PGRAPH-031). tombstone 은 `edges` 밖 별도 리스트라 절단 대상이 아니다.

`logger` 는 있지만 판정 입력이 아니다 — 절단이 상한을 넘겨 보존한 사실만 알린다. 같은 관측을
두 번 재생하면 로그와 무관하게 같은 문서가 나온다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import get_args

from app.agents.profile.graph_models import (
    EdgeSource,
    GraphDocument,
    GraphEdge,
    GraphNode,
    GraphTombstone,
    Predicate,
    is_pin_challenged,
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
# 저장된 payload 의 predicate 검증 어휘. `Predicate` Literal 을 **단일 출처**로 삼는다 —
# `_PREDICATE_ORDER` 를 재사용하면 정렬 순서와 검증 어휘가 한 자료에 묶여, 한쪽만 고칠 때
# 다른 쪽이 조용히 따라 바뀐다.
_PREDICATES: frozenset[str] = frozenset(get_args(Predicate))
_SOURCES: frozenset[str] = frozenset(
    get_args(EdgeSource)
)  # 같은 이유로 `EdgeSource` 가 단일 출처다
# 같은 노드를 두고 서로 못 서는 관계. **쌍 목록이 아니라 의미로 판정한다** — 부정 하나 vs
# 임의의 긍정이다(REQ-PGRAPH-018). `{likes, avoids}` 쌍만 등록했더니 resolver 가 kind 별로 다른
# 긍정을 만드는 탓에(priceBand·ratingBand·attribute → prefers, situation → interestedIn)
# 7개 kind 중 4개가 판정 밖에 남아 모순된 두 취향이 둘 다 active 로 공존했다(PR #410 리뷰).
# 쌍을 늘리는 대신 규칙을 바꾼 이유는, 긍정 predicate 가 하나 더 생길 때 등록을 또 빠뜨리기 때문이다.
_NEGATIVE_PREDICATE = "avoids"
# `purchased` 는 긍정에 넣지 않는다 — 구매 사실과 회피는 모순이 아니고(사고 나서 싫어질 수 있다),
# 원천도 대화가 아니라 질의 시점 구매 이력(I-19)이라 회피 발언이 이력을 덮어서는 안 된다.
_POSITIVE_PREDICATES: frozenset[str] = frozenset({"prefers", "likes", "interestedIn"})
_ROUND = 6  # 고정 소수점 — 부동소수 꼬리가 재생 동일성을 깨지 않게(REQ-PGRAPH-015)
_SECONDS_PER_DAY = 86400.0
# 개인화 중지 구간 `[{"from": iso, "to": iso}, …]` (REQ-PGRAPH-055). 저장 위치는 그래프 문서가
# 아니라 `profile_personalization_state` 다 — 문서에 두면 중지·재개가 문서를 고치게 되어 §3.9.5
# 응답의 `graphVersion`·감사·멱등 원장이 전부 거짓이 된다(SPEC §7.1).
PausedSpans = Sequence[dict]

logger = logging.getLogger(__name__)


def empty_document(now: str) -> GraphDocument:
    """첫 배치 이전 상태. `revision=0` 에서 시작해 실질 변경마다 오른다.

    **`revision=0` 은 "문서가 아예 없던 최초 부트스트랩" 전용 의미다.** 전체 초기화는 이 함수를
    쓰되 revision 을 이어받아 덮어써야 한다 — 되돌리면 같은 `If-Match` 가 서로 다른 상태를
    가리킨다(REQ-PGRAPH-042). 그 집행은 #358 의 `reset_graph` 다.
    """
    return GraphDocument(
        revision=0,
        nodes=[],
        edges=[],
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=now,
        tombstones=[],
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
    decay_pause_spans: PausedSpans = (),
) -> GraphDocument:
    """확정 트리플 + 기존 문서 → 새 문서. LLM·임베딩·저장소 접근 0회.

    `decay_pause_spans` 는 **개인화 중지 구간**이다(REQ-PGRAPH-055) — 그만큼 시간이 흐르지 않은
    것으로 보고 감쇠에서 뺀다. 호출부(`builder.consolidate`)가 상태 테이블에서 읽어 넣는다.
    불변 입력이라 재생 동일성(REQ-PGRAPH-015)과 "저장소 이음매 없음"(REQ-PROF-032/033)이 유지된다.
    """
    observations, unprojected = _collect(facts)
    prior = {edge.edge_key: edge for edge in existing.edges}

    # **지운 취향은 재파생하지 않는다** (AC-PROF-31, REQ-PGRAPH-026). 사용자가 지운 edge 는
    # 문서에서 물리 삭제됐지만 그 근거 fact 는 살아 있으므로, 차단하지 않으면 다음 배치가 같은
    # 트리플에서 같은 `edge_id` 를 다시 만들어 삭제가 조용히 무력화된다. 차단이 `edge_id` 로
    # 성립하는 것은 그것이 `(predicate, node_id)` 파생이라 재파생 시 같은 값이 나오기 때문이다.
    tombstoned = {tombstone.edge_id for tombstone in existing.tombstones}

    grouped: dict[str, list[_Observation]] = {}
    for obs in sorted(observations, key=lambda o: (o.observed_at, o.fact_key)):
        if obs.edge_id in tombstoned:
            continue
        grouped.setdefault(obs.edge_key, []).append(obs)

    edges = [
        _merge_edge(
            obs_list, prior=prior.get(key), settings=settings, now=now, paused=decay_pause_spans
        )
        for key, obs_list in grouped.items()
    ]
    edges.extend(
        _carried_tombstones(
            existing, seen=set(grouped), settings=settings, now=now, paused=decay_pause_spans
        )
    )
    # `grouped` = 이번 배치에 관측이 실제로 있었던 edge_key — `challenge_count` 가 배치 횟수가
    # 아니라 반대 관측 건수를 세게 하는 유일한 입력이다(REQ-PGRAPH-033).
    edges = _resolve_conflicts(edges, observed=set(grouped))
    # 절단 여부는 **개수 차이로** 판정한다 — `_truncate` 의 두 분기(일반 절단·사용자 삭제가 상한을
    # 넘겨 보존)를 한 규칙으로 덮고, "상한을 넘겼다"가 아니라 "버린 게 있다"라는 뜻이 그대로 산다.
    before_truncation = len(edges)
    edges = _truncate(edges, settings=settings)
    tombstones = _truncate_tombstones(list(existing.tombstones), settings=settings)
    # [HARD] 최종 보증 — 앞 단계 중 하나라도 pin 을 건드렸으면 여기서 되돌린다(REQ-PGRAPH-031).
    edges = _reassert_pins(edges, prior=prior)

    nodes = _nodes_for(edges, observations, existing)
    document = GraphDocument(
        revision=existing.revision,
        nodes=sorted(nodes, key=lambda n: n.node_id),
        edges=sorted(edges, key=_edge_sort_key),
        unprojected_count=unprojected,
        truncated=len(edges) < before_truncation,
        purged_at=existing.purged_at,
        updated_at=now,
        # 배치는 tombstone 을 **만들지 않는다** — 만드는 것은 사용자 변경 경로(#358)와 전체
        # 초기화뿐이다. 여기서 이월하지 않으면 다음 배치가 차단 목록을 잃고 지운 취향이 부활한다.
        # **[#359] 다만 상한은 배치가 집행한다** — 목록이 무한히 커지는 것을 막을 다른 자리가
        # 없다(사용자 변경 경로는 한 건씩만 더한다).
        tombstones=tombstones,
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
    """저장된 트리플 payload → 관측. 모양이 깨진 항목은 조용히 버린다(배치를 죽이지 않는다).

    **검증은 `node` 만이 아니라 `predicate`·`edge_key`·`edge_id` 까지다**(PR 리뷰 반영).
    `_Observation` 은 검증 없는 dataclass 라, 여기서 통과시키면 한참 뒤 `_merge_edge` 의
    `GraphEdge` 생성에서야 `ValidationError` 가 난다. 그 지점엔 잡는 코드가 없어 예외가
    `finalizer` 의 최상위 `except Exception` 까지 새고, 손상 fact 는 저장소에서 지워지지 않으므로
    session-end 마다 같은 자리에서 RETRYABLE 만 반복된다(poison record) — REQ-PGRAPH-004 가
    마련한 "못 만든 fact 는 개수만 센다" degrade 를 우회하는 셈이다.

    호출부에서 `build_graph_document` 를 감싸는 방어는 택하지 않았다. 그러면 트리플 하나 때문에
    **그 배치 전체**가 버려진다 — 여기서 거르면 손상분만 빠지고 나머지 취향은 정상 반영된다.
    """
    try:
        node = GraphNode.model_validate(triple["node"])
        predicate = triple["predicate"]
        edge_key = str(triple["edge_key"])
        edge_id = str(triple["edge_id"])
        source = str(triple.get("source") or "conversation")
        # `GraphEdge` 가 나중에 강제할 제약을 여기서 미리 건다 — 늦게 터지면 배치가 죽는다.
        # Literal 필드는 **전부** 본다. 하나라도 빼면 그 필드 하나로 같은 poison record 가 난다.
        if predicate not in _PREDICATES or source not in _SOURCES or not edge_key or not edge_id:
            return None
        return _Observation(
            observed_at=record.created_at,
            fact_key=record.fact_key,
            node=node,
            predicate=predicate,
            edge_key=edge_key,
            edge_id=edge_id,
            # `_confidence` 의 EMA 식은 salience 가 [0,1] 이라는 전제로 쓰였다 — 5 가 들어오면
            # `c + (1-c)*5` 가 진동해(관측 2건이면 -15) 강하게 반복 언급한 취향이 confidence 0
            # 으로 떨어진다. 마지막 클램프는 최종값만 잡으므로 여기서 막아야 한다. 게이트는
            # 하한만 보고 프롬프트의 "0.0~1.0" 은 강제되지 않는 소프트 제약이다(PR #410 리뷰).
            salience=min(1.0, max(0.0, float(triple.get("salience") or 0.0))),
            source=source,
        )
    except Exception:  # noqa: BLE001 - 구 형식·손상 payload 는 unprojected 취급
        return None


def _merge_edge(
    observations: list[_Observation],
    *,
    prior: GraphEdge | None,
    settings: Settings,
    now: str,
    paused: PausedSpans = (),
) -> GraphEdge:
    """같은 `edge_key` 로 수렴한 관측을 하나의 edge 로 (REQ-PGRAPH-015).

    `status`·`suppressed_at`·`user_intent` 는 **기존 값을 그대로 승계한다** — 새 근거가 들어왔다고
    사용자가 지운 취향을 되살리지 않는다(REQ-PGRAPH-022/023). `promoted` 는 `status` 와 직교라
    별도로 판정한다.

    **pin 이 걸려 있으면 `confidence`·`promoted` 도 승계한다**(REQ-PGRAPH-031 [HARD], #359).
    사용자가 고친 취향에 기계 관측이 하나만 들어와도 감쇠 가중 EMA 가 최상급 확신도를 덮어써
    다음 배치에서 강등까지 이어졌다 — 편집이 조용히 되돌려지는 가장 흔한 경로다.
    `predicate` 는 여기서 따로 고정하지 않는다: `edge_key = "{predicate}|{node_id}"` 라
    `prior` 는 정의상 `head` 와 같은 predicate 다(고정 코드를 두면 도달 불가능한 죽은 분기가 된다).
    **관측 기록은 계속한다** — `evidence_*`·`last_observed_at`·`decay_evaluated_at` 은 pin 이어도
    갱신된다. 사용자 편집이 시스템을 눈멀게 만들면 나중에 근거를 제시할 수 없다.
    """
    head = observations[-1]  # 정렬된 마지막 = 최신 관측
    by_source: dict[str, int] = {}
    for obs in observations:
        by_source[obs.source] = by_source.get(obs.source, 0) + 1

    if prior is not None and _is_pinned(prior):
        confidence = prior.confidence
        promoted = prior.promoted
    else:
        confidence = _confidence(observations, settings=settings, now=now, paused=paused)
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


def _confidence(
    observations: list[_Observation], *, settings: Settings, now: str, paused: PausedSpans = ()
) -> float:
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
            confidence *= _decay_factor(previous, obs.observed_at, half_life, paused)
        confidence = round(confidence + (1.0 - confidence) * obs.salience, _ROUND)
        previous = obs.observed_at
    if previous is not None:
        confidence = round(confidence * _decay_factor(previous, now, half_life, paused), _ROUND)
    return max(0.0, min(1.0, confidence))


def _decay_factor(start: str, end: str, half_life_days: float, paused: PausedSpans = ()) -> float:
    days = _elapsed_days(start, end, paused)
    if days <= 0.0:
        return 1.0
    return 0.5 ** (days / half_life_days)


def _elapsed_days(start: str, end: str, paused: PausedSpans = ()) -> float:
    """못 재면 0일로 본다 — 재지 못한 시간을 감쇠로 추측하지 않는다.

    `TypeError` 를 함께 잡는 이유는 `datetime` 뺄셈이 **naive-aware 혼합에서 `ValueError` 가
    아니라 `TypeError`** 를 내기 때문이다. 지금은 양쪽 소스가 모두 aware 지만(`_now_iso` 와
    store 의 `created_at`), 파싱만 막는 가드는 그 전제가 깨지는 순간 배치를 통째로 죽인다.

    **[#359] 개인화 중지 구간은 흐르지 않은 것으로 본다**(REQ-PGRAPH-055). `[start, end]` 와
    실제로 **겹친 만큼만** 뺀다 — 총 중지 시간(스칼라)을 최신 구간부터 소진하는 방식이면
    관측보다 앞에 있던 중지까지 감쇠를 깎아 준다. 수집도 함께 멈추므로 중지 구간 안에는 관측이
    없고, 따라서 모든 구간은 관측 사이 또는 마지막 관측 이후에 있다.
    """
    try:
        begin = datetime.fromisoformat(start)
        finish = datetime.fromisoformat(end)
        seconds = (finish - begin).total_seconds()
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, seconds - _paused_seconds(begin, finish, paused)) / _SECONDS_PER_DAY


def _paused_seconds(begin: datetime, finish: datetime, paused: PausedSpans) -> float:
    """`[begin, finish]` 와 중지 구간들의 겹침(초). 모양이 깨진 항목은 무시한다.

    저장 payload 하나가 배치를 죽이면 안 된다(`_observation` 과 같은 취지). 무시하면 감쇠를
    **덜 빼는** 쪽 — 즉 취향을 더 오래 살리는 보수적 방향으로 열화한다.
    """
    total = 0.0
    for span in paused:
        try:
            start = datetime.fromisoformat(span["from"])
            end = datetime.fromisoformat(span["to"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        overlap = (min(finish, end) - max(begin, start)).total_seconds()
        if overlap > 0:
            total += overlap
    return total


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
    """**지금 승격이 언제 시작됐는가.** 승격이 이어지는 동안은 갱신하지 않고, 강등되면 비운다.

    강등 시 `None` 으로 비우는 이유(PR #410 리뷰):
      - 이름과 정의가 "승격 시각"(§5.2)인데 승격 상태가 아닌 edge 에 값이 남으면 뜻이 없다.
        비워두면 `valid_from is not None` 자체가 "지금 승격됨"을 뜻한다.
      - 강등은 예외가 아니라 **흔한 상태 전이**다 — 반감기 30일·강등 임계 0.4 에서 약 40일
        침묵이면 내려온다(실측). 값을 유지하면 재승격 때 최초 시각을 물고 가, #150 이 이 값을
        노출할 때 "1월부터 관심"이라고 보여주는데 실제로는 8월에 다시 생긴 관심이 된다.
      - "최초 승격 시각"이 필요하면 `first_observed_at` 이 이미 그 자리에 가깝다 — 값을 유지하면
        두 필드가 사실상 같은 것을 가리킨다.

    강등돼도 **edge 자체와 근거는 그대로 남는다**(`status` 와 `promoted` 는 직교) — 여기서 비우는
    것은 "이번 승격 구간"의 시작 시각뿐이다.
    """
    if not promoted:
        return None
    if prior is not None and prior.promoted and prior.valid_from:
        return prior.valid_from
    return now


def _carried_tombstones(
    existing: GraphDocument,
    *,
    seen: set[str],
    settings: Settings,
    now: str,
    paused: PausedSpans = (),
) -> list[GraphEdge]:
    """이번 배치에 근거가 없는 edge 중 **보존 대상만** 살린다.

    `superseded`(진 취향 표식)와 사용자 pin 은 근거가 0이어도 남긴다. `superseded` 가 사라지면
    그 fact 가 다시 `active` 로 간주되어 요약 LLM 이 모순된 두 취향을 함께 받고(`_truncate`
    docstring 참조), pin 이 사라지면 사용자 편집이 조용히 되돌려진다(REQ-PGRAPH-031).
    반대로 그냥 `active` 인 edge 는 근거가 없어지면 함께 사라진다 — 보존은 이 둘 한정이다.

    **사용자 삭제(`suppressed`)는 여기 오지 않는다** — #499 로 즉시 물리 삭제가 되면서 `edges`
    를 떠나 `GraphDocument.tombstones` 로 갔고, 재파생 차단은 `build_graph_document` 입구에서
    한다. 그래서 "근거가 사라져도 삭제가 유지된다"는 보장이 이월이 아니라 **별도 리스트**로
    성립한다 — 이월 로직이 어긋나도 삭제는 안 풀린다.

    **이월할 때 확신도를 이번 배치 시각으로 감쇠시킨다**(PR #410 리뷰). 안 그러면 마지막 관측
    시점의 값이 박제되어, `_truncate` 의 `-confidence` 정렬과 `_resolve_conflicts` 의 승자 판정이
    **서로 다른 시각으로 잰 값을 같은 자로 비교**하게 된다 — 7개월 묵은 0.95 가 방금 관측된 0.5 를
    이기고 살아남는다. `decay_evaluated_at` 이 존재하는 이유가 "이 값이 언제 기준인가"를 남기기
    위해서인데, 갱신하지 않으면 그 필드가 뜻을 잃는다.

    **단 pin 은 감쇠시키지 않는다**(REQ-PGRAPH-031 [HARD], #359). 감쇠는 `confidence` 변경이고
    [HARD] 가 그것을 금지한다. 위 우려는 #359 가 두 판정 모두에서 pin 을 확신도 비교 밖으로
    빼면서 사라졌다 — 승자 판정은 pin 을 최우선 키로 올리고(`_resolve_conflicts`), 절단은 pin 을
    아예 자르지 않는다(`_truncate`). `decay_evaluated_at` 은 pin 이어도 `now` 로 계속 찍는다:
    얼려 두면 그 필드가 뜻을 잃고, pin 이 풀리는 경로가 생기는 순간 누적 감쇠가 한꺼번에 걸린다.

    감쇠는 `decay_evaluated_at → now` 로 **누적** 적용한다(관측이 남아 있는 edge 는 `_confidence`
    가 매 배치 전량 재계산하지만, 여기는 근거가 없어 재계산할 원본이 없다). 반감기 지수는
    `0.5^(Δ1/h) · 0.5^(Δ2/h) = 0.5^((Δ1+Δ2)/h)` 라 나눠 적용해도 값이 같고, 입력이 같으면 결과도
    같아 재생 동일성(REQ-PGRAPH-015)은 유지된다. `last_observed_at` 은 **관측 사실**이라 건드리지
    않는다 — 시간이 흘러도 "마지막으로 언제 말했나"는 변하지 않는다.
    """
    half_life = settings.graph_decay_half_life_days
    carried: list[GraphEdge] = []
    for edge in existing.edges:
        if edge.edge_key in seen:
            continue
        if edge.status == "active" and edge.user_intent is None:
            continue
        decayed = (
            edge.confidence
            if _is_pinned(edge)
            else round(
                edge.confidence * _decay_factor(edge.decay_evaluated_at, now, half_life, paused),
                _ROUND,
            )
        )
        carried.append(
            edge.model_copy(
                update={
                    "evidence_count": 0,
                    "evidence_refs": [],
                    "evidence_by_source": {},
                    "confidence": decayed,
                    "decay_evaluated_at": now,
                }
            )
        )
    return carried


def _resolve_conflicts(edges: list[GraphEdge], *, observed: set[str]) -> list[GraphEdge]:
    """같은 node 를 두고 상충하는 관계는 패자를 `superseded` 로 (REQ-PGRAPH-018) — **삭제하지 않는다**.

    상충은 **부정(`avoids`) vs 임의의 긍정**이다. 쌍을 열거하지 않는 이유는 `_NEGATIVE_PREDICATE`
    주석에 있다 — 열거하면 kind 가 늘 때 등록을 빠뜨리고, 그 결과는 "선호한다 + 싫어한다"가
    **둘 다 active 로 살아남아 요약 LLM 에 함께 들어가는** 것이다.

    승자 선정은 recency-wins 다(REQ-PROF-033): `(origin 클래스, last_observed_at, confidence,
    edge_id)` 최댓값. 마지막 키까지 가야 동률에서도 전순서가 성립한다. 진 **쪽만** 표시한다 —
    승자가 긍정이면 부정들만, 부정이면 긍정들만 `superseded` 다. 같은 편끼리는 모순이 아니라서
    건드리지 않는다.

    **REQ-PGRAPH-035 를 집행하는 것은 이 정렬 키다** — `_reassert_pins` 터미널 게이트가 아니다.
    클래스가 먼저이고 최신성은 클래스 **안에서** 적용된다. 게이트는 다른 요구사항
    (REQ-PGRAPH-031)의 심층 방어이고, 그것이 있다고 이 키를 지우면 pin 이 이번 배치에 실제로
    강등됐다가 게이트가 되돌리는 모양이 되어 `challenge_count` 같은 맥락 판정이 어긋난다.

    origin 클래스의 코드 표현은 `_is_pinned` 다. `origin` 필드(`Literal["machine","user"]`)로도
    같은 판정이 나오지만 — 생산자가 `graph_mutations._pin` 하나뿐이라 두 값이 항상 함께 움직인다
    (`test_pin_producer_sets_both_origin_user_and_user_intent` 가 그 동반 관계를 잠근다) —
    `_truncate` 가 이미 `_is_pinned` 를 쓰므로 한 파일 안에 pin 정의를 둘 두지 않는다.

    **[HARD] 진 쪽에 pin 이 오지 않는다.** pin 이 최우선 키라 pin 은 pin 에게만 질 수 있고,
    같은 노드에 반대 극성 pin 이 둘 생기는 것은 사용자 수정이 구 edge 를 tombstone 으로 보내므로
    정상 경로에서 만들어지지 않는다. 그래도 만들어졌다면 게이트가 둘 다 `active` 로 되돌린다 —
    사용자 의사를 기계가 정리하지 않는 쪽이 REQ-PGRAPH-031 이 정한 방향이다.

    **승자가 pin 이면 `challenge_count` 를 올린다**(REQ-PGRAPH-033) — 단 `observed` 에 든
    edge_key, 즉 **이번 배치에 관측이 실제로 있었던** 패자에 한한다. 승패만 보고 올리면 배치
    횟수를 세게 된다: 진 edge 는 근거가 0건이 돼도 `_carried_tombstones` 가 영구 이월하므로
    새 반대 관측이 하나도 없어도 매 배치 conflict 가 다시 성립한다. idle sweep 이 60초 주기라
    임계 3이면 **3분 침묵만으로** `challenged` 가 켜지고, FE 는 시간 경과만으로 "취향이
    바뀌셨나요?" 를 띄운다. `challenged` 는 상태를 바꾸지 않지만 **사용자를 귀찮게 하는 것은
    상태 변경 못지않은 비용**이다.
    """
    by_node: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        by_node.setdefault(edge.node_id, []).append(edge)

    resolved: dict[str, GraphEdge] = {e.edge_key: e for e in edges}
    for node_edges in by_node.values():
        candidates = [e for e in node_edges if e.status in ("active", "superseded")]
        negatives = [e for e in candidates if e.predicate == _NEGATIVE_PREDICATE]
        positives = [e for e in candidates if e.predicate in _POSITIVE_PREDICATES]
        if not negatives or not positives:
            continue

        winner = max(
            negatives + positives,
            key=lambda e: (_is_pinned(e), e.last_observed_at, e.confidence, e.edge_id),
        )
        losers = negatives if winner.predicate in _POSITIVE_PREDICATES else positives
        winner_update: dict = {"status": "active", "superseded_by": None}
        if _is_pinned(winner):
            fresh = sum(1 for edge in losers if edge.edge_key in observed)
            if fresh:
                winner_update["challenge_count"] = winner.challenge_count + fresh
        resolved[winner.edge_key] = winner.model_copy(update=winner_update)
        for edge in losers:
            resolved[edge.edge_key] = edge.model_copy(
                update={"status": "superseded", "superseded_by": winner.edge_id}
            )
    return _revive_orphan_superseded(resolved)


def _revive_orphan_superseded(resolved: dict[str, GraphEdge]) -> list[GraphEdge]:
    """상대가 사라진 `superseded` 는 `active` 로 되돌린다 (PR #410 리뷰).

    `superseded` 는 "저 edge 에게 졌다"는 **상대적** 상태다. 승자가 없어지면 근거가 사라진
    표식이므로 유지할 이유가 없다. 그런데 `_carried_tombstones` 는 비대칭이라 이 상황이 실제로
    생긴다 — 패자(`superseded`)는 근거 0건이어도 carry 되지만, 승자였던 `active` edge 는 fact cap
    트리밍(`profile_max_facts`)으로 근거가 밀리면 후보 목록에서 통째로 빠진다.

    되돌리지 않으면 `_resolve_conflicts` 는 "부정 없음"으로 보고 건너뛰고 `_merge_edge` 는
    `prior.status` 를 승계하므로, 사용자가 취향을 되돌려 새 관측을 아무리 쌓아도 그 edge 는
    **영구히 `superseded`** 다. 요약 입력에서도 영영 빠진다 — 단방향 상태는 REQ-PGRAPH-015 의
    재생 동일성 취지와도 어긋난다.

    **`suppressed` 는 건드리지 않는다.** 그쪽은 상대가 아니라 사용자 의도가 근거라 상대가 없다.
    """
    present = {edge.edge_id for edge in resolved.values()}
    return [
        edge.model_copy(update={"status": "active", "superseded_by": None})
        if edge.status == "superseded" and edge.superseded_by not in present
        else edge
        for edge in resolved.values()
    ]


def _is_pinned(edge: GraphEdge) -> bool:
    """사용자가 고정한 edge — 기계가 덮지 못하고 절단으로도 버리지 않는다 (REQ-PGRAPH-031).

    **사용자 삭제(`suppressed`)는 더 이상 여기 없다** — #499 로 즉시 물리 삭제가 되면서
    `edges` 를 떠나 `GraphDocument.tombstones` 로 갔다. 그래서 절단이 삭제 표식을 지울 위험
    자체가 사라졌고(별도 리스트라 절단 대상이 아니다), 여기서 지켜야 할 것은 pin 뿐이다.
    """
    return edge.user_intent is not None


def _truncate(edges: list[GraphEdge], *, settings: Settings) -> list[GraphEdge]:
    """상한 초과 시 절단 — **바구니마다 자기 예산을 쓴다** (REQ-PGRAPH-005, 개정 #359).

    ```
    pin        → 무제한 보존              [REQ-PGRAPH-031 HARD]
    active     → profile_graph_max_edges
    superseded → profile_graph_max_superseded_edges
    ```

    **왜 단일 상한을 버렸나.** 종전에는 상한 하나 안에서 밀려나는 *순서*만 정했고
    (`active` → `superseded` → pin), 그러면 보존 우선순위가 높은 쪽이 자리를 독차지한다.
    `superseded` 는 근거가 0건이어도 `_carried_tombstones` 가 영구 이월하므로 **단조 누적**되는데
    `active` 보다 먼저 보존되어, 개수가 `상한 − |pin|` 에 이르면 **active 가 하나도 안 남는다.**
    밀려난 active 는 투영(§3.8)에 없어 사용자가 `edgeId` 를 모르니 지울 수도 없는데 근거 fact 는
    살아 있어 요약·추천에는 계속 반영된다 — **지울수록 못 지우는 게 늘어나는 되먹임**이라
    이 기능의 목적과 정반대다(이슈 #150 코멘트 2026-08-09).

    **바구니 분리 기준은 `status == "active"`** — "사용자가 만든 것이냐"로 가르면 `superseded` 가
    active 바구니에 남아 잠식이 그대로 재현된다.

    **`superseded` 보호는 약해지지 않는다.** 종전 실효 예산이 `상한 − |pin|` 이었으니 자기 상한
    전량을 받는 쪽이 **항상 종전 이상**이다. 그 보호가 필요한 이유(비대칭)는 그대로다:
    - `active` 가 잘려도 **개인화 내용은 안 줄어든다.** `builder._summary_input` 은 문서에 없는
      `edge_key` 를 `active` 로 간주해 통과시키므로 그 fact 는 요약 입력에 남는다.
    - `superseded` 가 잘리면 같은 규칙 때문에 **진 취향이 요약에 되살아난다.** 즉 죽은 데이터가
      아니라 "이 fact 를 요약에 넣지 마라"는 살아 있는 표식이다.
    바뀐 것은 그 비대칭의 **실현 방식**이다 — "동률에서 이긴다" 에서 "자기 예산을 보장받는다" 로.

    **active 절단은 사실상 발동하지 않는다**: `active ≤ 서로 다른 edge_key ≤ fact 수` 이고
    `profile_graph_max_edges` 를 `profile_max_facts` 와 같게 두므로 상한에 닿을 수 없다.
    그래도 분기를 남기는 것은 두 값이 어긋나게 설정될 수 있기 때문이다.

    동률은 `edge_id` 로 갈라 절단 결과까지 결정론적으로 만든다.
    """
    pinned = [e for e in edges if _is_pinned(e)]
    rest = [e for e in edges if not _is_pinned(e)]
    active_cap = settings.profile_graph_max_edges
    superseded_cap = settings.profile_graph_max_superseded_edges

    def _keep(bucket: list[GraphEdge], cap: int) -> list[GraphEdge]:
        if len(bucket) <= cap:
            return bucket
        # 확신도 높은 순으로 남긴다. 동률은 `edge_id` 오름차순.
        return sorted(bucket, key=lambda e: (-e.confidence, e.edge_id))[:cap]

    kept = [
        *pinned,
        *_keep([e for e in rest if e.status == "active"], active_cap),
        *_keep([e for e in rest if e.status != "active"], superseded_cap),
    ]

    # **경고는 문서 총량으로 낸다.** 종전 `profile_graph_protected_over_cap` 은 "pin 을 지키느라
    # 남을 버렸다"를 알렸는데, 바구니를 나누면서 그 사건 자체가 없어져 발화 조건을 잃었다.
    # 남는 관심사는 단일 jsonb 가 커지는 것 하나뿐이고, 다른 두 바구니가 상한에 묶여 있으므로
    # **초과분은 정의상 pin** 이다 — 그래서 새 튜너블 없이 파생 임계로 잰다. 상한을 넘긴 문서는
    # 정리·초기화 신호이지(REQ-PGRAPH-005) 조용히 지울 근거가 아니다.
    if len(kept) > active_cap + superseded_cap:
        logger.warning(
            "profile_graph_pins_over_budget",
            extra={"pins": len(pinned), "edges": len(kept), "budget": active_cap + superseded_cap},
        )
    return kept


def _truncate_tombstones(
    tombstones: list[GraphTombstone], *, settings: Settings
) -> list[GraphTombstone]:
    """tombstone 목록 상한 — 넘으면 `suppressed_at` **오래된 순**으로 버린다 (#359).

    #499/#358 이 tombstone 을 `edges` 밖 별도 목록으로 빼면서 상한이 아예 없어졌다. 항목당 필드
    3개라 증가 폭은 작지만 단조 증가라 단일 jsonb 가 무한히 커진다.

    **오래된 것부터 버리는 이유**: 최근에 지운 취향일수록 그 원문이 fact 저장소에 남아 있을
    가능성이 높아 재파생 위험이 크다. 오래된 표식은 근거가 이미 fact cap 으로 밀려났을 확률이
    높다. 잔여 리스크는 0 이 아니다 — edge 의 근거 목록이 `graph_evidence_refs_max` 로 잘려 있어
    개별 삭제가 원문을 **전부** 지웠다고 보장할 수 없다(REQ-PGRAPH-005 잔여 리스크 항).
    """
    cap = settings.profile_graph_max_tombstones
    if len(tombstones) <= cap:
        return tombstones
    dropped = len(tombstones) - cap
    logger.warning(
        "profile_graph_tombstones_over_cap",
        extra={"tombstones": len(tombstones), "limit": cap, "dropped": dropped},
    )
    # 동률은 `edge_id` 로 갈라 결정론을 유지한다.
    return sorted(tombstones, key=lambda t: (t.suppressed_at, t.edge_id))[dropped:]


def _reassert_pins(edges: list[GraphEdge], *, prior: dict[str, GraphEdge]) -> list[GraphEdge]:
    """터미널 게이트 — pin 의 [HARD] 4필드를 `prior` 값으로 되돌린다 (REQ-PGRAPH-031, #359).

    앞 단계들(`_merge_edge`·`_carried_tombstones`·`_resolve_conflicts`)이 각자 pin 을 존중하므로
    정상 경로에서 이 게이트는 아무것도 바꾸지 않는다. 그런데도 두는 이유는 **[HARD] 불변식을
    한 곳에서 보증하기 위해서**다 — 네 번째 병합 단계가 생기거나 기존 단계가 리팩터로 pin 분기를
    잃어도 여기서 잡힌다. lessons 2026-08-06 「HARD 불변식과 방어용 상한이 만나면 우선순위를
    안 적은 쪽이 조용히 진다」의 반대 방향 적용이다: 불변식 쪽에 최종 발언권을 준다.

    **복원 대상은 REQ-PGRAPH-031 이 열거한 4개 정확히** — `status`·`predicate`·`promoted`·
    `confidence`. 기계가 갱신해도 되는
    `evidence_count`·`evidence_by_source`·`last_observed_at`·`challenge_count` 와 배치 시계
    `decay_evaluated_at` 은 손대지 않는다.

    `status` 는 **`prior` 가 `active` 일 때만** 되돌린다. 이 이슈 이전에 잘못 `superseded` 로 박힌
    pin 까지 복원 대상으로 삼으면 그 상태가 영구 고착되고, 방금 `_revive_orphan_superseded` 가
    한 복구를 게이트가 되돌린다. 되돌릴 때는 `superseded_by` 도 함께 걷는다 — `active` 인데
    패자 표식이 남으면 사라진 승자를 가리키는 dangling 참조가 된다.

    `_truncate` **다음**에 둔다. 이름 그대로 터미널이어야 나중에 단계가 끼어들어도 마지막이
    보장된다. 절단은 pin 을 아예 자르지 않으므로(`_truncate`) 순서가 pin 생존에 영향을 주지는
    않지만, 그 사실에 기대지 않는다.
    """
    restored: list[GraphEdge] = []
    for edge in edges:
        origin = prior.get(edge.edge_key)
        if origin is None or not _is_pinned(origin):
            restored.append(edge)
            continue
        update: dict = {
            "predicate": origin.predicate,
            "promoted": origin.promoted,
            "confidence": origin.confidence,
        }
        if origin.status == "active":
            update["status"] = "active"
            update["superseded_by"] = None
        if all(getattr(edge, field) == value for field, value in update.items()):
            restored.append(edge)  # 정상 경로 — model_copy 로 새 객체를 만들지 않는다
            continue
        logger.warning(
            "profile_graph_pin_reasserted",
            extra={"edge_id": edge.edge_id, "fields": sorted(update)},
        )
        restored.append(edge.model_copy(update=update))
    return restored


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


# [#360] 와이어 투영이 **같은 전순서**를 쓴다 — 두 곳에 3키를 적으면 한쪽만 고치는 순간
# 저장 순서와 화면 순서가 갈린다. 재구현이 아니라 재사용이라는 것을 이름으로 드러낸다.
edge_sort_key = _edge_sort_key


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
    """**와이어에 나가는 내용만** 지문에 넣는다 — 내부 판정값은 revision 을 움직이지 않는다.

    edge 의 `confidence`·`decay_evaluated_at` 과 **같은 이유로 node 의 `resolution` 도 뺀다**
    (§5.1 — 거리·margin·판정 시각은 임계 재측정용 내부값이라 FE 에 나가지 않는다).
    지금은 `resolution` 이 바뀌는 경로가 항상 edge 변화를 동반해 빼지 않아도 결과가 같지만,
    그건 설계가 아니라 우연이다 — 어휘 갱신 후 재파생(#150)·임계 재측정 후 재resolve(#344)가
    들어오면 와이어에 안 보이는 변화로 `If-Match` 토큰이 무효가 된다(PR #410 리뷰).
    """
    bounds = settings.profile_graph_confidence_buckets
    # `evidence_*` 도 와이어 미노출이다 — `evidence_count` 는 api-spec v0.26.0 이 명시적으로 뺐고
    # (REQ-PGRAPH-006: `profile_buffer_repeat_cap` 이 관측 횟수를 잘라 정확한 수를 셀 수 없다),
    # `evidence_by_source`·`evidence_refs` 는 §5.2 내부 전용이다. fact cap 트리밍으로 오래된 근거가
    # 밀려나면 이 값들만 바뀌는데(last_observed_at 은 최댓값, first_observed_at 은 prior 승계),
    # 그때 revision 이 오르면 #150 의 If-Match 토큰이 와이어 무변경인데도 무효가 된다.
    # `challenge_count` 도 같은 이유로 뺀다(#359) — 와이어에 나가는 것은 임계를 넘었는지
    # (`challenged`)뿐이고 원시 카운터가 아니다. 지문에 두면 반대 관측이 들어올 때마다 revision 이
    # 올라 #150 의 `If-Match` 토큰이 상시 무효가 된다. 대신 파생 `challenged` 를 아래에서 넣는다 —
    # 그 값이 뒤집히는 것은 FE 가 보는 변화라 실질 변경이다.
    volatile_edge = {
        "decay_evaluated_at",
        "confidence",
        "challenge_count",
        "evidence_count",
        "evidence_by_source",
        "evidence_refs",
    }
    volatile_node = {"resolution"}
    edges = []
    for edge in document.edges:
        payload = {k: v for k, v in edge.model_dump(mode="json").items() if k not in volatile_edge}
        payload["confidence_bucket"] = _confidence_bucket(edge.confidence, bounds)
        payload["challenged"] = is_pin_challenged(edge, settings=settings)
        edges.append(payload)
    nodes = [
        {k: v for k, v in node.model_dump(mode="json").items() if k not in volatile_node}
        for node in document.nodes
    ]
    # `truncated`·`unprojected_count` 는 **와이어에 나가는 값**이라(api-spec §3.8) 지문에 든다 —
    # 절단 여부가 뒤집히면 사용자가 보는 안내가 바뀌므로 실질 변경이다.
    #
    # tombstone 도 든다. 배치가 이걸 바꾸는 경로는 없지만(사용자 변경·초기화 전용), 지문에서
    # 빼 두면 "지운 취향이 사라졌는데 revision 이 그대로"인 문서를 만들 수 있는 구멍이 남는다 —
    # `edge_id` 목록만 넣어 라벨을 지문 계산에도 끌어들이지 않는다.
    tombstones = sorted(tombstone.edge_id for tombstone in document.tombstones)
    return repr(
        (
            nodes,
            edges,
            tombstones,
            document.unprojected_count,
            document.truncated,
            document.purged_at,
        )
    )
