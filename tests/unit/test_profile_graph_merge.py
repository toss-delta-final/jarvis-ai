"""결정론적 병합 엔진 (SPEC-PROFILE-GRAPH-149 §6.2·§6.3, REQ-PGRAPH-003/004/015~018/020~022).

`build_graph_document` 는 **순수 함수**다 — 인자에 llm·embed·store 가 없어 "병합·최신성·승격
판정에 LLM 을 쓰지 않는다"(REQ-PROF-032/033)가 mock 단언이 아니라 **구조적으로** 보장된다.
그 구조 자체를 회귀 가드로 고정한다.
"""

import inspect
import logging

import pytest

from app.agents.profile.graph_merge import build_graph_document, empty_document
from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    GraphTombstone,
    UserIntent,
    make_edge_id,
)
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


def _pin(kind: str = "assert") -> UserIntent:
    """사용자 편집 고정 표식 — **근거가 사라져도 이월되는 edge** 를 만드는 수단.

    #499 이전에는 `status="suppressed"` 가 그 역할을 겸했지만, 이제 사용자 삭제는 edge 를 떠나
    `tombstones` 로 간다(물리 삭제). 그래서 "이월되는 edge" 시나리오의 매개체는 pin 과
    `superseded` 둘뿐이다.
    """
    return UserIntent(kind=kind, asserted_at=NOW, mutation_id="m-test")


def _tombstone(label: str = "소니", *, predicate: str = "likes") -> GraphTombstone:
    """지운 취향의 차단 표식 — 라벨이 아니라 `edge_id` 만 든다."""
    return GraphTombstone(
        edge_id=make_edge_id(f"{predicate}|brand:{label}"), suppressed_at=NOW, user_intent=None
    )


# ─────────── LLM 0회 — 구조적 보장 ───────────


def test_merge_signature_has_no_llm_or_io_seam() -> None:
    """병합에 LLM·임베딩·저장소를 넘길 자리가 없어야 한다(REQ-PROF-032/033).

    누가 실수로 인자를 더하면 여기서 막힌다 — mock 으로 "안 불렸다"를 재는 것보다 강한 보장이다.

    **[#359] 허용 집합에 `decay_pause_spans` 를 더했다.** REQ-PGRAPH-055(중지 기간 감쇠 정지)는
    배치가 "언제부터 언제까지 시간이 흐르지 않은 것으로 볼지" 를 알아야 집행되는데, 그 값을
    순수 함수 **밖에서** 읽어 인자로 넣는 것이 이 가드의 취지와 어긋나지 않는다 — 불변 스칼라
    입력이라 재생 동일성(REQ-PGRAPH-015)이 유지되고, 함수 안에서 저장소를 만지지 않는다.
    아래 부정 단언이 그 취지를 직접 잰다(허용 집합만 넓히면 가드가 느슨해진다).
    """
    params = set(inspect.signature(build_graph_document).parameters)

    assert params == {"facts", "existing", "settings", "now", "decay_pause_spans"}
    # 넓힌 것은 허용 목록이지 성질이 아니다 — I/O·LLM 이음매는 여전히 없어야 한다.
    assert not {"llm", "embed", "store", "client", "pool"} & params


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
    """감쇠 시계는 배치당 1회 고정 — 관측별 현재시각을 쓰면 재생이 깨진다(REQ-PGRAPH-015).

    **문서에 실리는 모든 edge를 검사한다** — 어떤 경로로 만들어졌든(신규 관측·이월 pin·
    앞으로 생길 무엇이든) 시계가 다르면 여기서 걸린다. 시계가 갈리면 `_truncate` 의 확신도 정렬과
    `_resolve_conflicts` 의 승자 판정이 **서로 다른 자로 잰 값을 비교**하게 된다(PR #410 리뷰).
    이 단언이 그 계열 전체를 막는 구조적 가드다 — 표에 적은 것만 재는 방식이 아니다.
    """
    stale = "2026-01-01T00:00:00+00:00"  # 반감기를 여러 번 넘긴 옛 배치 시각
    existing = _document_of(
        [
            _stored_edge(
                "삼성",
                user_intent=_pin(),
                confidence=0.95,
                decay_evaluated_at=stale,
                last_observed_at=stale,
            )
        ]
    )
    facts = [
        _fact("f1"),
        _fact("f2", fact="다른 취향", triples=[_triple("brand:애플", label="애플")]),
    ]

    document = build_graph_document(facts, existing=existing, settings=settings, now=NOW)

    assert len(document.edges) == 3  # 신규 2 + 이월 pin 1
    assert {e.decay_evaluated_at for e in document.edges} == {NOW}


# `test_carried_pin_confidence_decays_to_the_batch_clock`(#356)은 #359 에서 뒤집혀
# 「pin 불변」절의 `test_carried_pin_confidence_does_not_decay_while_pinned` 가 됐다 —
# 감쇠는 `confidence` 변경이고 REQ-PGRAPH-031 [HARD] 가 그것을 금지한다. 뒤집은 근거는
# 그 테스트 docstring 에 있다.


# ─────────── 중지 기간 감쇠 정지 (REQ-PGRAPH-055) ───────────
#
# C7 이 중지 중 배치를 멈췄지만 그것만으로는 감쇠가 안 멈춘다 — `_confidence` 는
# `decay_evaluated_at` 을 안 보고 **관측 시각부터 `now` 까지** 매 배치 새로 계산하므로,
# 6개월 중지 후 재개하면 첫 배치에서 6개월치가 그대로 걸린다. "데이터는 보존된다"가 거짓이 된다.


def _span(from_: str, to: str) -> dict:
    return {"from": from_, "to": to}


def test_paused_span_is_subtracted_from_the_decay_clock(settings: Settings) -> None:
    """중지 구간만큼 시간이 안 흐른 것으로 본다 — 같은 관측이 덜 감쇠한다."""
    observed = "2026-02-01T00:00:00+00:00"
    facts = [_fact("f1", created_at=observed)]

    without = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)
    with_pause = build_graph_document(
        facts,
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
        # 관측 이후 통째로 중지돼 있었다.
        decay_pause_spans=(_span("2026-03-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),),
    )

    assert with_pause.edges[0].confidence > without.edges[0].confidence


def test_span_outside_the_interval_changes_nothing(settings: Settings) -> None:
    """관측 구간과 안 겹치는 중지는 감쇠에 영향이 없다 — 총량이 아니라 **겹침**으로 잰다.

    누적 스칼라(총 중지 시간)를 최신 구간부터 소진하는 방식이면 여기서 틀린다: 중지 창이
    관측보다 **앞**에 있어도 감쇠를 깎아 준다.
    """
    facts = [_fact("f1", created_at="2026-06-01T00:00:00+00:00")]
    baseline = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    document = build_graph_document(
        facts,
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
        # 관측보다 한참 전에 끝난 중지.
        decay_pause_spans=(_span("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),),
    )

    assert document.edges[0].confidence == baseline.edges[0].confidence


def test_partial_overlap_subtracts_only_the_overlapping_part(settings: Settings) -> None:
    """구간이 걸쳐 있으면 **겹친 만큼만** 뺀다."""
    facts = [_fact("f1", created_at="2026-06-01T00:00:00+00:00")]
    full = build_graph_document(
        facts,
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
        decay_pause_spans=(_span("2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),),
    )
    half = build_graph_document(
        facts,
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
        # 절반이 관측 앞이라 겹치는 것은 6/1~6/16 뿐이다.
        decay_pause_spans=(_span("2026-05-17T00:00:00+00:00", "2026-06-16T00:00:00+00:00"),),
    )
    none = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert none.edges[0].confidence < half.edges[0].confidence < full.edges[0].confidence


def test_carried_edges_also_honour_paused_spans(settings: Settings) -> None:
    """근거가 사라져 이월되는 edge 도 같은 규칙을 쓴다 — 두 감쇠 경로가 갈리면 안 된다."""
    stale = "2026-02-01T00:00:00+00:00"
    existing = _document_of(
        [
            _stored_edge(
                "애플",
                status="superseded",
                superseded_by=make_edge_id("avoids|brand:애플"),
                confidence=0.9,
                decay_evaluated_at=stale,
            )
        ]
    )
    spans = (_span("2026-03-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),)

    without = build_graph_document([], existing=existing, settings=settings, now=NOW)
    with_pause = build_graph_document(
        [], existing=existing, settings=settings, now=NOW, decay_pause_spans=spans
    )

    assert with_pause.edges[0].confidence > without.edges[0].confidence


def test_replay_stays_identical_with_paused_spans(settings: Settings) -> None:
    """같은 입력이면 같은 문서다 (REQ-PGRAPH-015) — 새 인자가 재생 동일성을 깨지 않는다."""
    facts = [_fact("f1", created_at="2026-02-01T00:00:00+00:00")]
    spans = (_span("2026-03-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),)

    first = build_graph_document(
        facts, existing=empty_document(NOW), settings=settings, now=NOW, decay_pause_spans=spans
    )
    second = build_graph_document(
        facts, existing=empty_document(NOW), settings=settings, now=NOW, decay_pause_spans=spans
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_malformed_span_is_ignored_instead_of_killing_the_batch(settings: Settings) -> None:
    """모양이 깨진 구간은 무시한다 — 저장 payload 하나가 배치를 죽이면 안 된다.

    `_elapsed_days` 가 파싱 실패를 0일로 흡수하는 것과 같은 취지다(그 docstring). 감쇠를 덜
    빼는 쪽(=보수적)으로 열화한다.
    """
    facts = [_fact("f1", created_at="2026-02-01T00:00:00+00:00")]
    baseline = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    document = build_graph_document(
        facts,
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
        decay_pause_spans=({"from": "not-a-date", "to": None}, {}, "쓰레기"),  # type: ignore[arg-type]
    )

    assert document.edges[0].confidence == baseline.edges[0].confidence


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("predicate", "hates"),  # Predicate Literal 밖
        ("predicate", None),
        ("edge_key", ""),
        ("edge_id", ""),
    ],
)
def test_corrupt_triple_field_is_dropped_not_raised(
    settings: Settings, field: str, value: object
) -> None:
    """손상된 트리플은 그 fact 만 unprojected 로 빠진다 — 배치를 죽이지 않는다(REQ-PGRAPH-004).

    `_Observation` 은 검증 없는 dataclass 라 `_observation` 이 통과시키면 한참 뒤 `_merge_edge` 의
    `GraphEdge` 생성에서야 `ValidationError` 가 난다. 그 지점엔 잡는 코드가 없어
    `finalizer` 의 최상위 `except Exception` 까지 새고, 손상 fact 가 저장소에 남아 있는 한
    session-end 마다 같은 자리에서 RETRYABLE 만 반복된다(poison record).
    """
    broken = _triple()
    broken[field] = value
    facts = [
        _fact("f1", triples=[broken]),
        _fact("f2", triples=[_triple("brand:애플", label="애플")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert document.unprojected_count == 1
    assert [e.node_id for e in document.edges] == ["brand:애플"]  # 나머지 취향은 정상 반영


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


def test_timezone_mixed_timestamps_skip_decay_instead_of_killing_the_batch(
    settings: Settings,
) -> None:
    """오프셋 없는 관측 시각이 섞여도 배치를 죽이지 않는다 — 감쇠를 포기할 뿐이다.

    `datetime` 뺄셈은 naive-aware 혼합에서 `ValueError` 가 아니라 **`TypeError`** 를 낸다.
    파싱 실패만 막는 가드는 이 경우를 놓쳐 consolidation 이 통째로 죽는다.
    """
    naive = "2026-08-01T00:00:00"  # 오프셋 없음 — `now` 는 aware 다

    document = build_graph_document(
        [_fact("f1", created_at=naive)], existing=empty_document(NOW), settings=settings, now=NOW
    )

    assert document.edges[0].confidence == pytest.approx(0.9)  # 추측 대신 감쇠 미적용


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


def test_valid_from_is_kept_while_promotion_continues(settings: Settings) -> None:
    """**승격이 이어지는 동안은** 갱신하지 않는다 — 언제부터 그 취향이었는지가 흐려진다."""
    existing = _document_with(confidence=0.9, promoted=True, valid_from="2026-07-01T00:00:00+00:00")

    document = build_graph_document([_fact("f1")], existing=existing, settings=settings, now=NOW)

    assert document.edges[0].valid_from == "2026-07-01T00:00:00+00:00"


def test_valid_from_clears_on_demotion(settings: Settings) -> None:
    """강등되면 `valid_from` 은 비운다 — "승격 시각"인데 승격 상태가 아니면 값이 남을 이유가 없다.

    강등은 흔하다. 감쇠 반감기 30일·강등 임계 0.4 에서 **약 40일 침묵이면 내려온다**(실측).
    값이 남으면 `valid_from is not None` 이 "지금 승격됨"을 뜻하지 못하고, 아래 재승격 테스트의
    문제로 이어진다(PR #410 리뷰).
    """
    low = settings.profile_gate_threshold - settings.graph_demote_margin - 0.1
    existing = _document_with(confidence=0.9, promoted=True, valid_from="2026-01-01T00:00:00+00:00")

    document = build_graph_document(
        [_fact("f1", created_at=NOW, triples=[_triple(salience=low)])],
        existing=existing,
        settings=settings,
        now=NOW,
    )

    edge = document.edges[0]
    assert edge.promoted is False
    assert edge.valid_from is None


def test_valid_from_is_the_repromotion_time_not_the_first(settings: Settings) -> None:
    """강등 뒤 재승격하면 **그때** 시각이다 — 최초 승격 시각을 물고 가지 않는다.

    "1월에 좋아함 → 40일 침묵으로 강등 → 8월에 다시 좋아함" 에서 8월이 맞다. 1월을 유지하면
    #150 이 이 값을 노출할 때 "1월부터 관심"이라고 보여주는데, 2~7월엔 프로필에서 빠져 있었다.
    최초 관측 시각은 `first_observed_at` 이 이미 갖고 있어 의미가 겹치지도 않는다.
    """
    demoted = _document_with(
        confidence=0.1,
        promoted=False,
        valid_from=None,  # 위 테스트대로 강등 시 비워진 상태
        first_observed_at="2026-01-01T00:00:00+00:00",
    )
    repromoted_at = "2026-08-07T00:00:00+00:00"

    document = build_graph_document(
        [_fact("f1", created_at=repromoted_at, triples=[_triple(salience=0.95)])],
        existing=demoted,
        settings=settings,
        now=repromoted_at,
    )

    edge = document.edges[0]
    assert edge.promoted is True
    assert edge.valid_from == repromoted_at
    assert edge.first_observed_at == "2026-01-01T00:00:00+00:00"  # 최초 관측은 따로 보존된다


def test_revision_ignores_evidence_fields_which_are_wire_invisible(settings: Settings) -> None:
    """`evidence_*` 는 와이어 미노출이라 revision 을 움직이면 안 된다 (PR #410 리뷰).

    `evidence_count` 는 api-spec v0.26.0 이 명시적으로 와이어에서 뺐고(REQ-PGRAPH-006 —
    `profile_buffer_repeat_cap` 이 관측 횟수를 잘라 정확한 수를 셀 수 없다), `evidence_by_source`·
    `evidence_refs` 도 §5.2 내부 전용이다. fact cap 트리밍으로 오래된 근거가 밀려나면 이 값들만
    바뀌는데(`last_observed_at` 은 최댓값, `first_observed_at` 은 prior 승계라 그대로), 그때
    revision 이 오르면 #150 의 `If-Match` 토큰이 와이어 무변경인데도 무효가 된다.
    """
    old = _fact("f1", created_at="2026-08-01T00:00:00+00:00")
    new = _fact("f2", created_at="2026-08-05T00:00:00+00:00")

    first = build_graph_document(
        [old, new], existing=empty_document(NOW), settings=settings, now=NOW
    )
    second = build_graph_document([new], existing=first, settings=settings, now=NOW)  # f1 트리밍

    assert second.edges[0].evidence_count != first.edges[0].evidence_count  # 실제로 달라졌다
    assert second.edges[0].last_observed_at == first.edges[0].last_observed_at  # 와이어는 그대로
    assert second.revision == first.revision


# ─────────── 상태 보존 — 이 이슈의 존재 이유 ───────────


def test_deleted_preference_is_not_recreated_by_new_evidence(settings: Settings) -> None:
    """[HARD] 지운 취향은 재관측돼도 되살아나지 않는다(REQ-PGRAPH-022/023, AC-PROF-31).

    #499 로 삭제가 즉시 물리 삭제가 되면서 확인할 것이 바뀌었다 — 구 계약은 "edge 가 남되 상태가
    `suppressed` 인가"였지만, 이제는 **edge 가 아예 만들어지지 않고 라벨 원문도 문서에 없는가**다.
    차단은 `tombstones` 가 맡는다.
    """
    existing = _document_of([], tombstones=[_tombstone("소니")])

    document = build_graph_document(
        [_fact("f1"), _fact("f2", fact="소니 또", created_at="2026-08-05T00:00:00+00:00")],
        existing=existing,
        settings=settings,
        now=NOW,
    )

    assert _edge_by_key(document, "likes|brand:소니") is None
    assert document.nodes == []  # 노드도 안 생긴다 — 라벨은 노드에 있다
    assert "소니" not in repr(document.model_dump(mode="json"))
    assert [t.edge_id for t in document.tombstones] == [make_edge_id("likes|brand:소니")]


def test_tombstone_carries_forward_when_evidence_disappears(settings: Settings) -> None:
    """근거 fact 가 cap 트리밍으로 밀려나도 차단 표식은 남는다.

    사라지면 같은 취향이 새 active edge 로 부활해 삭제가 조용히 무력화된다. 이제 표식이 `edges`
    밖 별도 리스트라 **이월 로직이 아니라 자료 구조로** 성립한다.
    """
    existing = _document_of([], tombstones=[_tombstone("소니")])

    document = build_graph_document([], existing=existing, settings=settings, now=NOW)

    assert [t.edge_id for t in document.tombstones] == [make_edge_id("likes|brand:소니")]


def test_legacy_suppressed_edge_is_absorbed_into_a_tombstone(settings: Settings) -> None:
    """구 문서(`status="suppressed"` edge)는 읽는 즉시 표식으로 수렴하고 라벨을 떨군다.

    별도 백필 잡 없이 `GraphDocument` 검증 지점에서 처리한다 — 안 하면 이미 지운 취향의 원문이
    문서에 남은 채로 남고, 그건 "지웠다"는 약속이 거짓인 상태다.
    """
    legacy = _document_with(status="suppressed", suppressed_at=NOW)

    assert legacy.edges == []
    assert legacy.nodes == []
    assert "소니" not in repr(legacy.model_dump(mode="json"))
    assert [t.edge_id for t in legacy.tombstones] == [make_edge_id("likes|brand:소니")]


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


@pytest.mark.parametrize(
    ("positive", "node_id", "node_type", "label"),
    [
        ("prefers", "priceBand:30000-50000", "priceBand", "30000-50000"),  # priceBand·ratingBand
        ("prefers", "attribute:방수", "attribute", "방수"),  # attribute
        ("interestedIn", "situation:캠핑", "situation", "캠핑"),  # situation
    ],
)
def test_any_positive_predicate_conflicts_with_avoids(
    settings: Settings, positive: str, node_id: str, node_type: str, label: str
) -> None:
    """상충은 `likes` vs `avoids` 만이 아니다 — **부정 vs 임의의 긍정**이다(REQ-PGRAPH-018).

    `resolver._POSITIVE_PREDICATE` 는 kind 마다 다른 긍정을 만든다(priceBand·ratingBand·
    attribute → `prefers`, situation → `interestedIn`). 충돌 쌍을 `{likes, avoids}` 로
    하드코딩하면 7개 kind 중 4개가 판정 밖에 남아, 모순된 두 취향이 **둘 다 active** 로 공존하고
    요약 LLM 이 "선호한다 + 싫어한다"를 함께 받는다(PR #410 리뷰).
    """
    facts = [
        _fact(
            "f1",
            created_at="2026-08-01T00:00:00+00:00",
            triples=[_triple(node_id, positive, label=label, node_type=node_type)],
        ),
        _fact(
            "f2",
            created_at="2026-08-05T00:00:00+00:00",
            triples=[_triple(node_id, "avoids", label=label, node_type=node_type)],
        ),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    winner = _edge_by_key(document, f"avoids|{node_id}")  # recency-wins
    loser = _edge_by_key(document, f"{positive}|{node_id}")
    assert winner is not None and loser is not None
    assert winner.status == "active"
    assert loser.status == "superseded"
    assert loser.superseded_by == winner.edge_id


def test_purchased_does_not_conflict_with_avoids(settings: Settings) -> None:
    """구매 사실과 회피는 모순이 아니다 — 사고 나서 싫어질 수 있다.

    `purchased` 의 원천은 질의 시점 구매 이력(I-19)이지 발화가 아니라서, 회피 발언이 구매
    기록을 덮으면 이력이 취향 판정에 지워진다.
    """
    facts = [
        _fact(
            "f1",
            created_at="2026-08-01T00:00:00+00:00",
            triples=[_triple("product:12345", "purchased", label="12345", node_type="product")],
        ),
        _fact(
            "f2",
            created_at="2026-08-05T00:00:00+00:00",
            triples=[_triple("product:12345", "avoids", label="12345", node_type="product")],
        ),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert {e.status for e in document.edges} == {"active"}


def test_superseded_revives_when_its_winner_is_gone(settings: Settings) -> None:
    """상대가 사라진 `superseded` 는 되살아난다 — 아니면 사용자가 취향을 되돌려도 복구 불가다.

    `_carried_tombstones` 는 비대칭이다: `superseded` 는 근거 0건이어도 carry 되지만 승자였던
    `active` edge 는 fact cap 트리밍으로 근거가 밀리면 후보 목록에서 통째로 빠진다. 그러면
    `_resolve_conflicts` 는 "부정 없음"으로 보고 건너뛰고, `_merge_edge` 는 `prior.status` 를
    승계하므로 패자는 새 관측이 아무리 쌓여도 **영구히 `superseded`** 다(PR #410 리뷰).
    """
    # 1) 충돌 → avoids 가 이기고 likes 가 superseded
    first = build_graph_document(
        [
            _fact(
                "f1", created_at="2026-08-01T00:00:00+00:00", triples=[_triple(predicate="likes")]
            ),
            _fact(
                "f2", created_at="2026-08-05T00:00:00+00:00", triples=[_triple(predicate="avoids")]
            ),
        ],
        existing=empty_document(NOW),
        settings=settings,
        now=NOW,
    )
    loser = _edge_by_key(first, "likes|brand:소니")
    assert loser is not None and loser.status == "superseded"

    # 2) 승자(avoids)의 근거 fact 가 cap 트리밍으로 사라지고, 사용자가 취향을 되돌려 다시 말한다
    second = build_graph_document(
        [_fact("f3", created_at="2026-08-09T00:00:00+00:00", triples=[_triple(predicate="likes")])],
        existing=first,
        settings=settings,
        now=NOW,
    )

    revived = _edge_by_key(second, "likes|brand:소니")
    assert revived is not None
    assert revived.status == "active"
    assert revived.superseded_by is None


@pytest.mark.parametrize("bad", [5.0, -1.0, 1.5])
def test_out_of_range_salience_cannot_distort_confidence(settings: Settings, bad: float) -> None:
    """범위 밖 `salience` 가 EMA 를 망가뜨리지 않는다 — 경계에서 `[0,1]` 로 잡는다.

    EMA 식 `confidence + (1-confidence)*salience` 는 양쪽이 `[0,1]` 이라는 전제로 쓰였다.
    `salience=5` 면 관측 2건에서 `5 + (1-5)*5 = -15` 로 진동하고, 마지막 `max/min` 클램프는
    **최종값만** 잡으므로 루프 중간 오염은 못 막는다 — 강하게 반복 언급한 취향이 confidence 0
    으로 떨어져 승격에서 조용히 빠진다(PR #410 리뷰).

    게이트는 `salience >= threshold` 만 보므로 상한 밖 값도 그대로 저장된다. 프롬프트의
    "0.0~1.0" 은 강제되지 않는 소프트 제약이라, 읽는 쪽에서 잡는다(저장된 오염값도 함께 막힌다).
    """
    triples = [_triple(), _triple()]
    for triple in triples:
        triple["salience"] = bad
    facts = [
        _fact("f1", created_at="2026-08-01T00:00:00+00:00", triples=[triples[0]]),
        _fact("f2", created_at="2026-08-02T00:00:00+00:00", triples=[triples[1]]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    edge = document.edges[0]
    assert 0.0 <= edge.confidence <= 1.0
    if bad > 1.0:
        # 상한 밖 강한 신호는 "가장 강한 관측"으로 다뤄야 한다 — 진동해서 0 이 되면 안 된다.
        assert edge.confidence > settings.profile_gate_threshold


def test_corrupt_source_is_dropped_not_raised(settings: Settings) -> None:
    """`source` 도 `GraphEdge.source_latest` Literal 이라 여기서 걸러야 한다 (PR #410 리뷰).

    `predicate`·`edge_key`·`edge_id` 만 검증하고 `source` 를 빠뜨리면 같은 poison record 가
    `source` 값 하나로 재현된다 — `_merge_edge` 의 `GraphEdge(source_latest=...)` 에서 터진다.
    """
    broken = _triple()
    broken["source"] = "telepathy"  # Literal 밖
    facts = [
        _fact("f1", triples=[broken]),
        _fact("f2", triples=[_triple("brand:애플", label="애플")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert document.unprojected_count == 1
    assert [e.node_id for e in document.edges] == ["brand:애플"]


def test_supersede_does_not_touch_unrelated_nodes(settings: Settings) -> None:
    facts = [
        _fact("f1", triples=[_triple(predicate="likes")]),
        _fact("f2", triples=[_triple("brand:애플", "avoids", label="애플")]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=settings, now=NOW)

    assert {e.status for e in document.edges} == {"active"}


# ─────────── pin 불변 (REQ-PGRAPH-031 [HARD]·035) ───────────
#
# §6.4 의 제목이 「기능이 연극이 되지 않게 하는 조항」이다. 사용자가 고친 취향이 다음 배치에
# 되돌아오면 편집 기능은 겉모습만 남는다. 기계가 갱신해도 되는 것은
# `evidence_count`·`evidence_by_source`·`last_observed_at`·`challenge_count` 뿐이고
# `status`·`predicate`·`promoted`·`confidence` 는 손대면 안 된다.


def _pinned_edge(label: str = "소니", **overrides: object) -> GraphEdge:
    """실제 `graph_mutations._pin` 이 새기는 모양 — `origin="user"` 와 `user_intent` 가 함께 온다.

    기존 `_stored_edge(user_intent=_pin())` 은 `origin` 이 `"machine"` 인 채로 남아 **실물과
    다르다.** 그 픽스처로 origin 기반 판정을 재면 새 코드 경로를 하나도 안 밟은 채 초록불이
    된다(거짓 초록불). 여기서는 둘을 함께 세운다 — 그 동반 관계 자체는
    `test_pin_producer_sets_both_origin_user_and_user_intent` 가 잠근다.
    """
    base: dict = {
        "user_intent": _pin(),
        "origin": "user",
        "source_latest": "user",
        "confidence": 1.0,
        "promoted": True,
    }
    base.update(overrides)
    return _stored_edge(label, **base)


def _build_pin_safe(
    facts: list[FactRecord],
    *,
    existing: GraphDocument,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    now: str = NOW,
) -> GraphDocument:
    """배치를 돌리되 **터미널 게이트가 발화하지 않았음**을 함께 단언한다.

    `_reassert_pins` 는 [HARD] 최종 보증이라 앞 단계가 pin 을 망가뜨려도 결과를 고쳐 놓는다.
    그래서 결과만 재면 `_merge_edge`·`_carried_tombstones`·`_resolve_conflicts` 의 pin 분기를
    통째로 지워도 테스트가 초록불이다(실제로 변이 검증에서 61건 전부 통과했다) — **국소 수정에
    테스트가 없는 상태**가 된다.

    그래서 불변식을 하나 더 세운다: **정상 경로에서 게이트는 아무것도 바꾸지 않는다.**
    게이트가 무언가 되돌렸다면 그것은 상류 단계가 [HARD] 를 어겼다는 뜻이고, 여기서 잡힌다.
    """
    with caplog.at_level(logging.WARNING, logger="app.agents.profile.graph_merge"):
        document = build_graph_document(facts, existing=existing, settings=settings, now=now)
    assert "profile_graph_pin_reasserted" not in caplog.text, (
        "터미널 게이트가 발화했다 — 앞 단계(_merge_edge·_carried_tombstones·_resolve_conflicts)"
        " 중 하나가 pin 의 [HARD] 필드를 건드렸다는 뜻이다."
    )
    return document


def test_pinned_edge_wins_conflict_even_when_the_opposing_observation_is_more_recent(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-PGRAPH-035 — 우선순위 비교는 `(origin 클래스, last_observed_at, confidence, edge_id)`.

    **클래스가 먼저이고 최신성은 클래스 안에서 적용된다.** 현행 승자 판정은 `origin` 을 안 봐서
    방금 관측된 기계 `avoids` 가 오래된 사용자 pin 을 이기고 `superseded` 로 강등한다 —
    REQ-PGRAPH-031 이 [HARD] 로 금지한 `status` 변경이다.

    pin 이 recency 에서 불리한 것은 우연이 아니라 구조다: `graph_mutations._pin` 은
    `last_observed_at` 을 갱신하지 않으므로 **만들어진 직후부터** 최신성 비교에서 진다.
    """
    stale = "2026-01-01T00:00:00+00:00"
    existing = _document_of([_pinned_edge("소니", last_observed_at=stale)])
    # 방금 관측된 반대 취향 — 최신성만 보면 이쪽이 이긴다.
    facts = [_fact("f1", triples=[_triple("brand:소니", "avoids", label="소니")])]

    document = _build_pin_safe(facts, existing=existing, settings=settings, caplog=caplog)

    pinned = _edge_by_key(document, "likes|brand:소니")
    assert pinned is not None
    assert pinned.status == "active"  # [HARD] 기계가 사용자 편집을 강등하지 못한다
    assert pinned.superseded_by is None
    opposing = _edge_by_key(document, "avoids|brand:소니")
    assert opposing is not None and opposing.status == "superseded"


def test_pinned_edge_freezes_confidence_and_promoted_when_new_evidence_arrives(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """관측이 새로 들어와도 pin 의 `confidence`·`promoted` 는 승계한다 (REQ-PGRAPH-031 [HARD]).

    **관측 기록 자체는 계속한다** — 사용자 편집이 시스템을 눈멀게 만들면 나중에 "왜 이걸
    추천했나"에 답할 근거가 사라진다. 그래서 `evidence_count`·`last_observed_at` 은 갱신된다.
    """
    existing = _document_of([_pinned_edge("소니")])
    # 확신도를 끌어내릴 만큼 약한 관측 — 기계 재계산이 살아 있으면 1.0 이 이 값 근처로 떨어진다.
    facts = [
        _fact("f1", created_at=NOW, triples=[_triple("brand:소니", label="소니", salience=0.1)])
    ]

    document = _build_pin_safe(facts, existing=existing, settings=settings, caplog=caplog)

    edge = _edge_by_key(document, "likes|brand:소니")
    assert edge is not None
    assert edge.confidence == 1.0  # 동결
    assert edge.promoted is True  # 동결
    assert edge.predicate == "likes"
    assert edge.status == "active"
    assert edge.evidence_count == 1  # 관측은 계속 기록한다
    assert edge.last_observed_at == NOW
    assert edge.decay_evaluated_at == NOW  # 시계는 배치당 1회 고정을 유지한다


def test_user_edit_survives_repeated_consolidation_batches(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-PGRAPH-09 — 같은 취향을 재파생시키는 관측을 여러 배치 주입해도 사용자 수정이 유지된다.

    개별 단계를 재는 위 두 테스트와 달리 **배치를 실제로 여러 번 돌린다.** 리포에 이 성질을
    끝에서 끝까지 재는 테스트가 없었다 — `test_profile_graph_apply.py` 는 사용자 변경 경로만
    검증하고 배치 재실행을 안 돌린다.
    """
    document = _document_of([_pinned_edge("소니")])
    facts = [
        _fact("f1", created_at="2026-08-02T00:00:00+00:00", triples=[_triple(salience=0.2)]),
        _fact(
            "f2",
            created_at="2026-08-03T00:00:00+00:00",
            triples=[_triple("brand:소니", "avoids", label="소니")],
        ),
    ]

    for _ in range(3):
        document = _build_pin_safe(facts, existing=document, settings=settings, caplog=caplog)

    edge = _edge_by_key(document, "likes|brand:소니")
    assert edge is not None
    assert (edge.predicate, edge.status, edge.confidence, edge.promoted) == (
        "likes",
        "active",
        1.0,
        True,
    )
    assert edge.user_intent is not None
    assert edge.last_observed_at == "2026-08-02T00:00:00+00:00"  # 관측은 반영된다


def test_carried_pin_confidence_does_not_decay_while_pinned(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """근거가 사라져 이월되는 pin 도 확신도를 깎지 않는다 (REQ-PGRAPH-031 [HARD]).

    **이 테스트는 `test_carried_pin_confidence_decays_to_the_batch_clock`(#356)을 뒤집은 것이다.**
    그 테스트의 근거는 *"7개월 묵은 0.95 가 방금 관측된 0.5 를 이기고 살아남는다"* — 즉 서로 다른
    시각으로 잰 확신도가 `_truncate` 정렬과 `_resolve_conflicts` 승자 판정에서 같은 자로 비교되는
    것이었다. 그 우려는 #359 가 **두 판정 모두에서 pin 을 확신도 비교 밖으로 빼면서** 사라졌다:
    승자 판정은 pin 을 최우선 키로 올리고(REQ-PGRAPH-035), 절단은 pin 을 아예 자르지 않는다.
    남는 것은 [HARD] 쪽이다 — 감쇠는 `confidence` 변경이고 REQ-PGRAPH-031 이 그것을 금지한다.

    `decay_evaluated_at` 은 계속 `now` 로 찍는다. 얼려 두면 "이 값이 언제 기준인가"가 뜻을 잃고
    `test_decay_clock_is_one_snapshot_per_batch` 의 구조적 가드도 깨진다.
    """
    stale = "2026-01-01T00:00:00+00:00"
    existing = _document_of(
        [_pinned_edge("삼성", confidence=0.95, decay_evaluated_at=stale, last_observed_at=stale)]
    )

    document = _build_pin_safe([], existing=existing, settings=settings, caplog=caplog)

    carried = document.edges[0]
    assert carried.user_intent is not None
    assert carried.confidence == 0.95  # 7개월이 지나도 깎이지 않는다
    assert carried.decay_evaluated_at == NOW
    assert carried.last_observed_at == stale  # 관측 **사실**은 시간이 지나도 안 바뀐다


def test_reassert_pins_rescues_a_pin_even_if_an_earlier_stage_corrupted_it(
    settings: Settings,
) -> None:
    """터미널 게이트를 **직접** 부른다 — 앞 단계가 실수해도 [HARD] 4필드가 복원되는가.

    앞 세 테스트는 각 단계가 스스로 옳음을 재고, 이 테스트는 **그 단계들을 못 믿을 때의 보증**을
    잰다. 미래에 병합 단계가 하나 더 생겨도 자동으로 보호되는 것이 게이트의 존재 이유다.
    lessons 2026-08-10 「방어를 추가하는 커밋에 그 방어가 없으면 깨지는 테스트를 같은 커밋에」.
    """
    from app.agents.profile.graph_merge import _reassert_pins

    prior = _pinned_edge("소니")
    corrupted = prior.model_copy(
        update={
            "status": "superseded",
            "superseded_by": "e_deadbeefdeadbeef",
            "predicate": "avoids",
            "promoted": False,
            "confidence": 0.01,
        }
    )

    restored = _reassert_pins([corrupted], prior={prior.edge_key: prior})

    assert len(restored) == 1
    edge = restored[0]
    assert (edge.status, edge.predicate, edge.promoted, edge.confidence) == (
        "active",
        "likes",
        True,
        1.0,
    )
    assert edge.superseded_by is None  # `active` 로 되돌렸으면 패자 표식도 함께 걷는다


def test_reassert_pins_leaves_unpinned_edges_alone(settings: Settings) -> None:
    """게이트는 pin 에만 손댄다 — 기계 edge 의 정상적인 강등·supersede 를 되돌리면 안 된다."""
    from app.agents.profile.graph_merge import _reassert_pins

    prior = _stored_edge("애플", status="active", confidence=0.9, promoted=True)
    demoted = prior.model_copy(
        update={"status": "superseded", "confidence": 0.1, "promoted": False}
    )

    restored = _reassert_pins([demoted], prior={prior.edge_key: prior})

    assert restored == [demoted]


def test_pin_producer_sets_both_origin_user_and_user_intent(settings: Settings) -> None:
    """`origin == "user"` ⟺ `user_intent is not None` 를 잠근다.

    병합 엔진은 pin 판정에 `_is_pinned`(`user_intent`)를 쓰고 REQ-PGRAPH-035 는 "origin 클래스"라고
    적는다. 두 표현이 같은 것을 가리키는 근거는 **생산자가 하나뿐**이라는 사실이다 — 그 사실을
    여기서 고정한다. 배치는 `origin` 에 `"user"` 를 쓸 수 없다(`_merge_edge` 는 prior 승계 또는
    `"machine"`).
    """
    from app.agents.profile.graph_models import make_edge_key
    from app.agents.profile.graph_mutations import _pin as pin_edge

    node = GraphNode(node_id="brand:소니", type="brand", label="소니", verified=True)
    key = make_edge_key("likes", node.node_id)

    pinned = pin_edge(
        _stored_edge("소니"),
        key=key,
        edge_id=make_edge_id(key),
        node=node,
        intent=_pin(kind="correct"),
        now=NOW,
    )

    assert pinned.origin == "user"
    assert pinned.user_intent is not None
    assert pinned.status == "active"
    assert pinned.promoted is True  # 사용자 명시 취향은 게이트 판정을 기다리지 않는다


# ─────────── challenged 신호 (REQ-PGRAPH-033) ───────────
#
# pin 이후 반대 관측이 임계에 도달하면 표시만 하고 **상태는 바꾸지 않는다.** 취향 변화의 반영은
# 명시적 사용자 동작으로만 일어난다 — `challenged` 는 FE 가 "다시 반영할까요?" 를 물을지 판단하는
# 동작 트리거이지 표시용 값이 아니다(api-spec §3.8).


def _opposing_fact(key: str, *, created_at: str) -> FactRecord:
    """pin 된 `likes|brand:소니` 에 맞서는 반대 관측 한 건."""
    return _fact(
        key, created_at=created_at, triples=[_triple("brand:소니", "avoids", label="소니")]
    )


def test_challenge_count_rises_when_a_fresh_opposing_observation_arrives(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """pin 에 반대 관측이 새로 들어오면 카운터가 오른다 — **상태는 그대로다**(REQ-PGRAPH-033)."""
    existing = _document_of([_pinned_edge("소니", last_observed_at="2026-01-01T00:00:00+00:00")])

    document = _build_pin_safe(
        [_opposing_fact("f1", created_at="2026-08-02T00:00:00+00:00")],
        existing=existing,
        settings=settings,
        caplog=caplog,
    )

    edge = _edge_by_key(document, "likes|brand:소니")
    assert edge is not None
    assert edge.challenge_count == 1
    assert (edge.status, edge.confidence, edge.promoted) == ("active", 1.0, True)  # 불변


def test_challenge_count_does_not_rise_when_only_the_batch_repeats(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """**반대 관측이 아니라 배치 횟수를 세면 안 된다** — 이 커밋에서 가장 조심할 지점이다.

    진 edge 는 근거가 0건이 돼도 `_carried_tombstones` 가 영구 이월하므로(§6.3), 승패만 보고
    카운터를 올리면 **새 관측이 하나도 없어도 매 배치 conflict 가 다시 성립해** 카운터가 오른다.
    idle sweep 이 60초 주기이니 `graph_pin_challenge_count=3` 이면 **3분 침묵만으로**
    `challenged` 가 켜지고, FE 는 시간 경과만으로 "취향이 바뀌셨나요?" 를 띄운다.

    그래서 판정 기준은 **이번 배치에 그 반대 `edge_key` 의 관측이 실제로 있었는가** 다.
    """
    existing = _document_of([_pinned_edge("소니", last_observed_at="2026-01-01T00:00:00+00:00")])
    facts = [_opposing_fact("f1", created_at="2026-08-02T00:00:00+00:00")]

    first = _build_pin_safe(facts, existing=existing, settings=settings, caplog=caplog)
    assert _edge_by_key(first, "likes|brand:소니").challenge_count == 1  # type: ignore[union-attr]

    # 같은 관측이 더는 없는 배치를 두 번 돌린다 — 이월된 avoids 는 그대로 남아 conflict 는
    # 계속 성립하지만, **새 반대 관측은 0건**이다.
    second = _build_pin_safe([], existing=first, settings=settings, caplog=caplog)
    third = _build_pin_safe([], existing=second, settings=settings, caplog=caplog)

    assert _edge_by_key(third, "likes|brand:소니").challenge_count == 1  # type: ignore[union-attr]


def test_challenged_turns_true_at_the_configured_threshold_without_changing_state(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """임계에 도달하면 `challenged` 가 참이 되지만 상태·관계·확신도는 그대로다."""
    from app.agents.profile.graph_models import is_pin_challenged

    tight = Settings(_env_file=None, graph_pin_challenge_count=2)
    document = _document_of([_pinned_edge("소니", last_observed_at="2026-01-01T00:00:00+00:00")])

    for day, key in ((2, "f1"), (3, "f2")):
        document = _build_pin_safe(
            [_opposing_fact(key, created_at=f"2026-08-0{day}T00:00:00+00:00")],
            existing=document,
            settings=tight,
            caplog=caplog,
        )

    edge = _edge_by_key(document, "likes|brand:소니")
    assert edge is not None
    assert edge.challenge_count == 2
    assert is_pin_challenged(edge, settings=tight) is True
    assert (edge.predicate, edge.status, edge.confidence, edge.promoted) == (
        "likes",
        "active",
        1.0,
        True,
    )  # 취향 변화의 반영은 명시적 사용자 동작으로만


def test_zero_threshold_turns_the_signal_off_instead_of_always_on() -> None:
    """설정값 `0` 은 신호를 **끈다**(REQ-PGRAPH-033).

    순진하게 `count >= threshold` 로 쓰면 `0` 에서 **항상 참**이 되어 규약과 정반대로 동작한다.
    `ge=0` 이 그 값을 허용하므로 특례 분기가 없으면 그대로 새어 나간다.
    """
    from app.agents.profile.graph_models import is_pin_challenged

    off = Settings(_env_file=None, graph_pin_challenge_count=0)
    edge = _pinned_edge("소니", challenge_count=99)

    assert is_pin_challenged(edge, settings=off) is False


def test_unpinned_edge_is_never_challenged(settings: Settings) -> None:
    """`challenged` 는 **사용자가 고친 항목**에만 붙는다 — 기계 edge 는 대상이 아니다."""
    from app.agents.profile.graph_models import is_pin_challenged

    assert is_pin_challenged(_stored_edge("소니", challenge_count=99), settings=settings) is False


def test_fingerprint_ignores_challenge_count_but_tracks_challenged(settings: Settings) -> None:
    """지문에는 **파생 `challenged` 만** 들어가고 원시 `challenge_count` 는 안 들어간다.

    카운터를 지문에 두면 반대 관측이 들어올 때마다 revision 이 올라 #150 의 `If-Match` 토큰이
    상시 무효가 된다 — `_fingerprint` docstring 이 `confidence` 에 대해 막으려던 바로 그 실패다.
    반대로 `challenged` 는 api-spec §3.8 의 **와이어 노출 필드**라 지문에 있어야 한다: 값이
    뒤집혔는데 revision 이 그대로면 FE 가 낡은 토큰으로 계속 쓴다.

    배치를 돌려서 재지 않는 이유는 **공허해지기 때문**이다 — 카운터를 올리려면 반대 관측이
    새로 있어야 하고, 그러면 그 edge 의 `last_observed_at` 이 함께 바뀌어 지문이 어차피
    달라진다. 그 시나리오는 "카운터 때문에 올랐다"를 증명하지 못한다.
    """
    from app.agents.profile.graph_merge import _fingerprint

    tight = Settings(_env_file=None, graph_pin_challenge_count=3)
    below = _document_of([_pinned_edge("소니", challenge_count=1)])
    also_below = _document_of([_pinned_edge("소니", challenge_count=2)])
    crossed = _document_of([_pinned_edge("소니", challenge_count=3)])

    # 임계 아래에서 카운터만 움직인 것은 와이어에 안 보인다.
    assert _fingerprint(below, tight) == _fingerprint(also_below, tight)
    # 임계를 넘으면 `challenged` 가 뒤집히므로 실질 변경이다.
    assert _fingerprint(also_below, tight) != _fingerprint(crossed, tight)


def test_revision_is_stable_when_only_challenge_count_moved(settings: Settings) -> None:
    """지문 규칙이 `build_graph_document` 의 revision 판정까지 실제로 이어지는지."""
    tight = Settings(_env_file=None, graph_pin_challenge_count=3)
    existing = _document_of([_pinned_edge("소니", challenge_count=1)])
    # 문서에 이미 실린 pin 을 근거 없이 이월시키되, 카운터만 손으로 올려 둔 상태로 다시 돌린다.
    bumped = existing.model_copy(
        update={"edges": [existing.edges[0].model_copy(update={"challenge_count": 2})]}
    )

    document = build_graph_document([], existing=bumped, settings=tight, now=NOW)

    assert document.edges[0].challenge_count == 2
    assert document.revision == bumped.revision  # 와이어에 안 보이는 변화다


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


def test_superseded_pile_cannot_starve_active_edges(settings: Settings) -> None:
    """**`superseded` 가 아무리 쌓여도 `active` 자리를 먹지 않는다** (REQ-PGRAPH-005, #359).

    단일 상한에서는 보존 우선순위가 높은 쪽이 자리를 독차지했다. `superseded` 는 근거가 0건이어도
    `_carried_tombstones` 가 영구 이월하므로 **단조 누적**되는데 `active` 보다 먼저 보존되어,
    개수가 `상한 − |pin|` 에 이르면 active 가 하나도 안 남았다. 밀려난 active 는 투영에 없어
    사용자가 `edgeId` 를 모르니 지울 수도 없는데 근거 fact 는 살아 있어 요약·추천에는 계속
    반영된다 — **지울수록 못 지우는 게 늘어나는 되먹임**이다(이슈 #150 코멘트 2026-08-09).
    """
    tight = Settings(
        _env_file=None, profile_graph_max_edges=5, profile_graph_max_superseded_edges=5
    )
    # 상한(5)만큼의 superseded 를 쌓아 둔다.
    piled = [
        _stored_edge(
            f"브랜드{i}",
            status="superseded",
            superseded_by=make_edge_id(f"avoids|brand:브랜드{i}"),
            confidence=0.5,
        )
        for i in range(5)
    ]
    existing = _document_of(piled)
    # 각 패자의 승자에게 **이번 배치 근거를 준다** — 승자가 사라지면
    # `_revive_orphan_superseded` 가 더미를 전부 active 로 되살려 시나리오가 성립하지 않는다
    # (그러면 테스트가 엉뚱한 이유로 초록불이 된다).
    facts = [
        _fact(
            f"w{i}",
            triples=[_triple(f"brand:브랜드{i}", "avoids", label=f"브랜드{i}", salience=0.5)],
        )
        for i in range(5)
    ]
    # 관심 대상 — 확신도를 가장 높게 줘서 active 바구니 안 정렬에서도 확실히 살아남게 한다.
    facts.append(_fact("f1", triples=[_triple("brand:소니", label="소니", salience=0.99)]))

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    assert {e.status for e in document.edges if e.edge_key.startswith("likes|brand:브랜드")} == {
        "superseded"
    }  # 더미가 실제로 superseded 로 남아 있어야 이 테스트가 무언가를 잰다
    assert _edge_by_key(document, "likes|brand:소니") is not None


def test_tombstones_are_capped_oldest_first(settings: Settings) -> None:
    """tombstone 목록도 상한을 갖는다 — 넘으면 `suppressed_at` 오래된 순으로 버린다.

    #499/#358 이 tombstone 을 `edges` 밖 별도 목록으로 빼면서 **상한이 아예 없어졌다**. 항목당
    필드 3개라 증가 폭은 작지만 단조 증가라 단일 jsonb 가 무한히 커진다. 버리면 그 취향이
    부활할 수 있다는 잔여 리스크는 낮다 — 개별 삭제가 원문을 물리 삭제하므로 재파생할 fact 가
    대부분 없다(REQ-PGRAPH-005 잔여 리스크 항 참조).
    """
    tight = Settings(_env_file=None, profile_graph_max_tombstones=2)
    existing = _document_of(
        [],
        tombstones=[
            GraphTombstone(
                edge_id=make_edge_id(f"likes|brand:{label}"),
                suppressed_at=stamp,
                user_intent=None,
            )
            for label, stamp in (
                ("오래된", "2026-01-01T00:00:00+00:00"),
                ("중간", "2026-05-01T00:00:00+00:00"),
                ("최근", "2026-08-01T00:00:00+00:00"),
            )
        ],
    )

    document = build_graph_document([], existing=existing, settings=tight, now=NOW)

    assert {t.edge_id for t in document.tombstones} == {
        make_edge_id("likes|brand:중간"),
        make_edge_id("likes|brand:최근"),
    }


def test_truncation_keeps_pins_first(settings: Settings) -> None:
    """pin 은 절단으로 사라지지 않는다 — 사용자 편집에는 복구 경로가 없다.

    **[#359] 이제 pin 은 active 예산 밖이므로 문서에 둘 다 남는다.** 종전에는 상한 1 안에서
    pin 이 자리를 차지해 신규 active 가 밀렸다(그리고 그 밀림이 없애야 할 되먹임이었다).
    이 테스트가 재는 것은 처음부터 "pin 이 남는가" 였으므로 단언을 그쪽으로 좁힌다.
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=1)
    existing = _document_with(user_intent=_pin())
    facts = [_fact("f1", triples=[_triple("brand:애플", label="애플")])]

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    assert any(e.user_intent is not None for e in document.edges)


def test_truncation_never_drops_pins_even_past_the_cap(settings: Settings) -> None:
    """pin 은 개수가 얼마든 하나도 잘리지 않는다 — 상한을 넘겨서라도 지킨다.

    `active`·`superseded` 는 잘려도 재파생으로 자기복구되지만 pin 은 복구 경로가 없다. 잘리는
    순간 "기계 재파생에 덮이지 않는다"(REQ-PGRAPH-031)는 보장이 저장 상한 때문에 깨진다.

    **[#359] pin 에는 이제 상한 자체가 없다** — "상한보다 많아도" 라는 전제가 사라졌다. 그래도
    이 테스트는 남긴다: 바구니를 나눈 뒤에도 pin 이 어느 상한에도 안 걸리는지가 [HARD] 이고,
    나중에 pin 바구니에 상한을 도입하려는 변경이 여기서 걸린다.
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=2)
    existing = _document_of(
        [
            _stored_edge("소니", user_intent=_pin()),
            _stored_edge("애플", user_intent=_pin()),
            _stored_edge("삼성", user_intent=_pin()),
        ]
    )
    facts = [_fact("f1", triples=[_triple("brand:엘지", label="엘지")])]

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    assert {e.edge_key for e in document.edges if e.user_intent is not None} == {
        "likes|brand:소니",
        "likes|brand:애플",
        "likes|brand:삼성",
    }


def test_deleted_preference_does_not_resurrect_after_truncation(settings: Settings) -> None:
    """절단을 거친 다음 배치에서 지운 취향이 다시 언급돼도 `active` 로 부활하지 않는다.

    앞 테스트가 "남는다"를 잰다면 이 테스트는 "남아서 실제로 막는다"를 잰다 — 억제 실효가
    이 이슈의 존재 이유라 절단 경계에서 한 번 더 고정한다.
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=2)
    # 차단 표식은 `edges` 밖 별도 리스트라 절단 대상이 아니다 — 그 **구조적** 성질을 상한이
    # 빡빡한 경계에서 한 번 더 고정한다. 표식을 다시 edges 안으로 넣는 설계로 되돌리면 여기서 깨진다.
    existing = _document_of(
        [_stored_edge("애플"), _stored_edge("삼성")],
        tombstones=[_tombstone("소니")],
    )
    first = build_graph_document(
        [_fact("f1", triples=[_triple("brand:엘지", label="엘지")])],
        existing=existing,
        settings=tight,
        now=NOW,
    )

    second = build_graph_document(
        [
            _fact("f1", triples=[_triple("brand:엘지", label="엘지")]),
            _fact("f2", triples=[_triple("brand:소니", label="소니")]),  # 지운 취향 재언급
        ],
        existing=first,
        settings=tight,
        now=NOW,
    )

    assert _edge_by_key(second, "likes|brand:소니") is None
    assert make_edge_id("likes|brand:소니") in {t.edge_id for t in second.tombstones}


def test_truncation_drops_machine_superseded_before_user_pins(settings: Settings) -> None:
    """`superseded` 는 잘려도 재파생으로 자기복구되지만 pin 은 복구 경로가 없다.

    그래서 둘을 같은 등급으로 두지 않는다 — 밀려나는 쪽은 항상 기계 판정이다.
    문서 등장 순서가 아니라 확신도로 갈리는지도 함께 고정한다(순서 우연으로 통과하지 않게).
    """
    # **[#359] 바구니가 갈려 superseded 전용 상한으로 조인다.** 종전에는 단일 상한 2 안에서
    # pin·superseded·active 가 함께 경쟁했으나, 이제 각 바구니가 자기 예산을 쓴다 — 그래서
    # superseded 등급 **안의** 우선순위를 재려면 그 바구니를 직접 조여야 한다.
    tight = Settings(
        _env_file=None, profile_graph_max_edges=10, profile_graph_max_superseded_edges=1
    )
    # 두 패자에게 **문서·근거 양쪽에 있는 승자**를 준다 — 상대가 없으면 `_revive_orphan_superseded`
    # 가 둘 다 active 로 되살려, 이 테스트가 재려는 "superseded 등급 안의 우선순위"가 성립하지 않는다.
    existing = _document_of(
        [
            _stored_edge(
                "애플",
                status="superseded",
                superseded_by=make_edge_id("avoids|brand:애플"),
                confidence=0.1,
            ),
            _stored_edge(
                "삼성",
                status="superseded",
                superseded_by=make_edge_id("avoids|brand:삼성"),
                confidence=0.9,
            ),
            _pinned_edge("소니"),
        ]
    )
    facts = [
        _fact("f1", triples=[_triple("brand:애플", "avoids", label="애플")]),  # 승자
        _fact("f2", triples=[_triple("brand:삼성", "avoids", label="삼성")]),  # 승자
    ]

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    sony = _edge_by_key(document, "likes|brand:소니")
    assert sony is not None and sony.user_intent is not None  # 사용자 편집은 무조건 남고
    assert _edge_by_key(document, "likes|brand:삼성") is not None  # 확신도 높은 쪽이 남고
    assert _edge_by_key(document, "likes|brand:애플") is None  # 낮은 쪽이 밀린다


def test_superseded_is_not_evicted_by_the_active_cap(settings: Settings) -> None:
    """`active` 상한이 꽉 차도 `superseded` 는 자기 예산에서 산다 (REQ-PGRAPH-005, #359).

    **이 테스트는 `test_truncation_drops_active_before_superseded`(#356)를 대체한다.** 그 테스트가
    지킨 비대칭은 그대로 유효하다 — `active` 가 잘려도 그 fact 는 `_summary_input` 에 남지만
    (문서에 없는 `edge_key` 는 `active` 로 간주된다), `superseded` 가 잘리면 같은 규칙 때문에
    **진 취향이 요약에 되살아난다.** 바뀐 것은 **실현 방식**이다: 단일 상한 안에서 "동률에서
    이긴다" 였던 것이 이제 "자기 예산을 보장받는다" 가 됐다.

    보호는 오히려 세졌다 — 종전 `superseded` 의 실효 예산은 `상한 − |pin|` 이었는데 이제 pin 과
    무관하게 자기 상한 전량이다.
    """
    # active 는 한 칸뿐이고 그 자리를 새 관측이 채운다. superseded 는 그 예산과 무관하게 남는다.
    tight = Settings(
        _env_file=None, profile_graph_max_edges=1, profile_graph_max_superseded_edges=1
    )
    # 승자(avoids|brand:애플)를 문서·근거 양쪽에 둔다 — 상대가 없으면 병합이 패자를 되살려
    # (`_revive_orphan_superseded`) 이 시나리오가 성립하지 않는다.
    winner_key = "avoids|brand:애플"
    existing = _document_of(
        [
            _stored_edge(
                "애플", status="superseded", superseded_by=make_edge_id(winner_key), confidence=0.1
            )
        ]
    )
    facts = [
        _fact("f1", triples=[_triple("brand:애플", "avoids", label="애플")]),
        _fact("f2", triples=[_triple("brand:엘지", label="엘지", salience=0.99)]),
    ]

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    statuses = {e.edge_key: e.status for e in document.edges}
    assert statuses.get("likes|brand:애플") == "superseded"  # 확신도 0.1 이어도 자기 예산에서 산다
    assert "avoids|brand:애플" in statuses or "likes|brand:엘지" in statuses  # active 한 칸


def test_truncated_flag_records_that_edges_were_dropped(settings: Settings) -> None:
    """절단이 실제로 일어났으면 문서에 표식을 남긴다 (REQ-PGRAPH-005, PR #410 리뷰).

    절단을 **쓰기 시점**으로 옮긴 결과, 읽는 쪽(#150 투영)은 저장된 edge 수가 상한 이하라
    "절단 안 됨"으로 볼 수밖에 없다 — `len(edges) == limit` 로 추정하는 것도 "우연히 딱 상한인
    정상 사용자"와 구분되지 않는다. 그래서 자른 쪽이 기록해야 한다.
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=1)
    facts = [
        _fact("f1", triples=[_triple("brand:엘지", label="엘지", salience=0.9)]),
        _fact("f2", triples=[_triple("brand:애플", label="애플", salience=0.2)]),
    ]

    document = build_graph_document(facts, existing=empty_document(NOW), settings=tight, now=NOW)

    assert len(document.edges) == 1
    assert document.truncated is True


def test_truncated_flag_is_false_when_everything_fits(settings: Settings) -> None:
    """상한 안이면 거짓이다 — 매 배치 참이면 표식이 신호 노릇을 못 한다."""
    document = build_graph_document(
        [_fact("f1")], existing=empty_document(NOW), settings=settings, now=NOW
    )

    assert document.truncated is False


def test_truncated_flag_is_false_when_the_cap_is_exceeded_but_nothing_dropped(
    settings: Settings,
) -> None:
    """사용자 삭제만으로 상한을 넘긴 경우는 **자른 게 아니다** — 넘긴 채 전부 보존했다.

    `truncated` 의 뜻은 "상한을 넘었다"가 아니라 "**버린 게 있다**"이다. 여기서 참이면 사용자는
    잘리지도 않은 목록을 보고 "일부만 표시됨"이라는 안내를 받는다.
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=1)
    existing = _document_of(
        [
            _stored_edge("소니", user_intent=_pin()),
            _stored_edge("애플", user_intent=_pin()),
        ]
    )

    document = build_graph_document([], existing=existing, settings=tight, now=NOW)

    assert len(document.edges) == 2  # 상한(1)을 넘겨서라도 삭제는 지킨다
    assert document.truncated is False  # 넘겼을 뿐 버리지 않았다


def test_revision_bumps_when_only_truncated_flips(settings: Settings) -> None:
    """`truncated` 만 달라져도 revision 은 오른다 — 와이어 응답이 바뀌기 때문이다.

    이 필드를 지문에 넣은 것이 **중복이 아니라는** 근거다. 상한을 넘겨 잘린 배치와, 잘린 대상의
    근거 fact 가 트리밍돼 절단이 사라진 다음 배치는 **남은 edge·node 가 완전히 동일**하다 —
    지문에서 빼면 "일부만 표시됨 → 전부 표시됨"이라는 사용자가 보는 변화를 놓치고 #150 의
    `If-Match` 토큰이 낡은 채로 유효해진다(PR #410 리뷰 대응에 딸린 판단).
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=2)
    keep = [
        _fact("f1", triples=[_triple("brand:소니", label="소니", salience=0.95)]),
        _fact("f2", triples=[_triple("brand:애플", label="애플", salience=0.90)]),
    ]
    dropped = _fact("f3", triples=[_triple("brand:삼성", label="삼성", salience=0.10)])

    first = build_graph_document(
        [*keep, dropped], existing=empty_document(NOW), settings=tight, now=NOW
    )
    second = build_graph_document(keep, existing=first, settings=tight, now=NOW)  # f3 트리밍

    assert first.truncated is True and second.truncated is False
    # 남은 것은 완전히 같다 — 그래서 truncated 가 유일한 차이다.
    assert [e.model_dump(mode="json") for e in second.edges] == [
        e.model_dump(mode="json") for e in first.edges
    ]
    assert second.revision == first.revision + 1


def test_pins_do_not_consume_the_active_budget(settings: Settings) -> None:
    """**pin 은 `active` 예산을 먹지 않는다** — 이 이슈가 만들려는 성질을 정면으로 잰다.

    **`test_truncation_keeps_active_edges_within_the_remaining_budget`(#356)을 뒤집은 것이다.**
    종전에는 pin 이 상한 2 중 한 칸을 차지해 새 관측 둘 중 하나만 살아남았다("남은 자리"라는
    이름이 그 구조를 그대로 담고 있다). 그 잠식이 #359 가 없애려는 되먹임의 절반이었다.
    이제 pin 은 자기 바구니(무제한)에 있고 active 두 칸은 온전히 새 관측 몫이다.
    """
    tight = Settings(_env_file=None, profile_graph_max_edges=2)
    existing = _document_of([_pinned_edge("소니")])
    facts = [
        _fact("f1", triples=[_triple("brand:엘지", label="엘지", salience=0.9)]),
        _fact("f2", triples=[_triple("brand:애플", label="애플", salience=0.2)]),
    ]

    document = build_graph_document(facts, existing=existing, settings=tight, now=NOW)

    assert {e.edge_key for e in document.edges} == {
        "likes|brand:소니",  # pin — 예산 밖
        "likes|brand:엘지",
        "likes|brand:애플",  # 종전에는 pin 에 밀려 잘렸다
    }


def test_truncation_logs_when_pins_alone_exceed_the_document_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """pin 이 문서를 부풀리는 것은 **조용히** 넘기지 않는다 — 정리·초기화 신호다.

    **[#359] 경고 조건이 바뀌었다.** 종전 `profile_graph_protected_over_cap` 은 "pin 을 지키느라
    남을 버렸다" 를 알렸는데, 바구니를 나누면서 **버리는 일 자체가 없어져** 발화 조건이 사라졌다.
    남는 관심사는 문서 총량 하나뿐이라 그쪽으로 옮겼다:
    `len(edges) > active 상한 + superseded 상한`. 다른 두 바구니가 상한에 묶여 있으므로
    **초과분은 정의상 pin** 이고, 그래서 새 튜너블 없이 파생 임계로 잰다.
    """
    tight = Settings(
        _env_file=None, profile_graph_max_edges=1, profile_graph_max_superseded_edges=1
    )
    existing = _document_of([_pinned_edge("소니"), _pinned_edge("애플"), _pinned_edge("삼성")])

    with caplog.at_level(logging.WARNING, logger="app.agents.profile.graph_merge"):
        document = build_graph_document([], existing=existing, settings=tight, now=NOW)

    assert len(document.edges) == 3  # pin 은 상한을 넘겨서라도 보존한다
    assert "profile_graph_pins_over_budget" in caplog.text


def test_truncation_stays_quiet_when_pins_fit_the_document_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """총량 안이면 경고하지 않는다 — 매 배치 울리면 신호 노릇을 못 한다."""
    tight = Settings(
        _env_file=None, profile_graph_max_edges=5, profile_graph_max_superseded_edges=5
    )
    existing = _document_of([_pinned_edge("소니"), _pinned_edge("애플")])

    with caplog.at_level(logging.WARNING, logger="app.agents.profile.graph_merge"):
        build_graph_document([], existing=existing, settings=tight, now=NOW)

    assert "profile_graph_pins_over_budget" not in caplog.text


# ─────────── revision ───────────


def test_revision_increases_on_substantive_change(settings: Settings) -> None:
    existing = empty_document(NOW)

    document = build_graph_document([_fact("f1")], existing=existing, settings=settings, now=NOW)

    assert document.revision == existing.revision + 1


def test_revision_ignores_node_resolution_which_is_wire_invisible(settings: Settings) -> None:
    """`NodeResolution` 은 **와이어 미노출** 판정 근거다 — 그것만 달라지면 revision 은 그대로다.

    edge 쪽에서 `confidence`·`decay_evaluated_at` 을 비교에서 뺀 것과 같은 규약이다(§5.1 —
    `resolution` 은 임계 재측정용 내부값이라 FE 에 나가지 않는다). 지금은 `resolution` 이 바뀌는
    경로가 항상 edge 변화를 동반해 이 규칙이 **우연히** 지켜지는데, 어휘 갱신 후 재파생(#150)이나
    임계 재측정 후 재resolve(#344)가 들어오면 그 우연이 깨진다 — 그때 와이어에 안 보이는 변화로
    #150 의 `If-Match` 토큰이 무효가 된다(PR #410 리뷰가 지적한 위험, 인과는 달랐다).
    """
    stale = _triple()
    stale["node"]["resolution"] = {
        "method": "no_vocabulary",
        "anchor_phrase": "소니 좋아",
        "resolved_at": "2026-08-01T00:00:00+00:00",
    }
    fresh = _triple()
    fresh["node"]["resolution"] = {  # 같은 노드, 판정 근거만 새로 찍혔다
        "method": "no_vocabulary",
        "anchor_phrase": "소니 좋아",
        "resolved_at": "2026-08-07T00:00:00+00:00",
    }

    first = build_graph_document(
        [_fact("f1", triples=[stale])], existing=empty_document(NOW), settings=settings, now=NOW
    )
    second = build_graph_document(
        [_fact("f1", triples=[fresh])], existing=first, settings=settings, now=NOW
    )

    assert second.nodes[0].resolution != first.nodes[0].resolution  # 실제로 달라졌다
    assert second.revision == first.revision  # 그래도 와이어 내용은 그대로다


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
    return _document_of([_stored_edge(**overrides)])


def _document_of(
    edges: list[GraphEdge], *, tombstones: list[GraphTombstone] | None = None
) -> GraphDocument:
    """이미 만든 edge 들로 기존 문서를 구성한다 — 노드는 edge 가 가리키는 것만 채운다."""
    nodes = {
        edge.node_id: GraphNode(
            node_id=edge.node_id,
            type="brand",
            label=edge.node_id.split(":", 1)[1],
            verified=False,
        )
        for edge in edges
    }
    return GraphDocument(
        revision=1,
        nodes=list(nodes.values()),
        edges=edges,
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at="2026-07-01T00:00:00+00:00",
        tombstones=list(tombstones or []),
    )


def _stored_edge(
    label: str = "소니", *, predicate: str = "likes", **overrides: object
) -> GraphEdge:
    from app.agents.profile.graph_models import make_edge_id

    node_id = f"brand:{label}"
    edge_key = f"{predicate}|{node_id}"
    base: dict = {
        "edge_key": edge_key,
        "edge_id": make_edge_id(edge_key),
        "node_id": node_id,
        "predicate": predicate,
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
    return GraphEdge(**base)
