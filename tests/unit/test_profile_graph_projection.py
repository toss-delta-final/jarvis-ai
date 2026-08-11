"""저장 그래프 → I-32 `edges[]` 투영 (#360, api-spec §3.8 · REQ-PGRAPH-001~003).

여기서 고정하는 축은 넷이다.

  - **결정론** — 같은 저장 상태는 항상 같은 순서·같은 값. 정렬은 3키 전순서이고 마지막 키가
    동점을 끝까지 깬다.
  - **모집단** — `active` 만, 민감 파생 제외. 이 집합이 `purged.edges` 와 같아야 화면 문구
    ("취향 12건")와 초기화 응답이 어긋나지 않는다.
  - **필드 5개** — 그 이상을 실으면 「응답 필드 기준」[HARD] 위반이다.
  - **상한 없음** — 서버가 자르면 상한 밖 항목을 사용자가 보지도 지우지도 못한다.
"""

from __future__ import annotations

import pytest

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    UserIntent,
    make_edge_id,
    make_edge_key,
)
from app.agents.profile.graph_projection import project_edge, project_edges
from app.core.config import Settings

NOW = "2026-08-11T00:00:00+00:00"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _edge(
    label: str,
    *,
    node_type: str = "brand",
    predicate: str = "likes",
    status: str = "active",
    observed_at: str = "2026-07-02T00:00:00+00:00",
    derived_from_sensitive: bool = False,
) -> GraphEdge:
    node_id = f"{node_type}:{label}"
    key = make_edge_key(predicate, node_id)
    return GraphEdge(
        edge_key=key,
        edge_id=make_edge_id(key),
        node_id=node_id,
        predicate=predicate,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        promoted=True,
        origin="machine",
        source_latest="conversation",
        confidence=0.6,
        evidence_count=1,
        evidence_by_source={"conversation": 1},
        evidence_refs=["f1"],
        first_observed_at=observed_at,
        last_observed_at=observed_at,
        decay_evaluated_at=observed_at,
        valid_from=observed_at,
        superseded_by=None,
        suppressed_at=None,
        user_intent=None,
        challenge_count=0,
        derived_from_sensitive=derived_from_sensitive,
        sensitive_topic="health" if derived_from_sensitive else None,
    )


def _document(*edges: GraphEdge) -> GraphDocument:
    # 노드는 edge 의 `node_id` 에서 되짚는다 — `_edge(node_type=...)` 를 쓰면 타입도 따라온다.
    # 라벨은 **저장 canonical 그대로**다(밴드면 `"30000-50000"`) — 문장으로 바꾸는 것은
    # 투영의 일이고, 여기서 미리 바꾸면 렌더러가 실제로 도는지 검증할 수 없다.
    node_ids = {edge.node_id for edge in edges}
    return GraphDocument(
        revision=42,
        nodes=[
            GraphNode(
                node_id=node_id,
                type=node_id.split(":", 1)[0],  # type: ignore[arg-type]
                label=node_id.split(":", 1)[1],
                verified=False,
            )
            for node_id in sorted(node_ids)
        ],
        edges=list(edges),
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=NOW,
        tombstones=[],
    )


# ─────────── 결정론 (REQ-PGRAPH-003) ───────────


def test_predicate_order_is_fixed(settings: Settings) -> None:
    """`prefers` → `likes` → `avoids` → `interestedIn` → `purchased` (api-spec §3.8)."""
    document = _document(
        _edge("A", predicate="purchased"),
        _edge("B", predicate="avoids"),
        _edge("C", predicate="prefers"),
        _edge("D", predicate="interestedIn"),
        _edge("E", predicate="likes"),
    )

    predicates = [edge.predicate for edge in project_edges(document, settings=settings)]

    assert predicates == ["prefers", "likes", "avoids", "interestedIn", "purchased"]


def test_recency_breaks_ties_within_a_predicate(settings: Settings) -> None:
    """같은 관계 안에서는 **최근 확인 시각 내림차순**이다."""
    document = _document(
        _edge("old", observed_at="2026-01-01T00:00:00+00:00"),
        _edge("new", observed_at="2026-08-01T00:00:00+00:00"),
    )

    labels = [edge.object.label for edge in project_edges(document, settings=settings)]

    assert labels == ["new", "old"]


def test_edge_id_gives_a_total_order(settings: Settings) -> None:
    """시각까지 같으면 `edgeId` 오름차순 — **마지막 키가 전순서를 보장**한다.

    부분순서면 "결정론적"은 검증 불가능한 주장이 된다.
    """
    document = _document(_edge("X"), _edge("Y"), _edge("Z"))

    ids = [edge.edge_id for edge in project_edges(document, settings=settings)]

    assert ids == sorted(ids)


def test_a_document_edited_out_of_order_still_projects_in_order(settings: Settings) -> None:
    """**편집 직후 문서는 정렬이 깨져 있다** — 투영이 다시 정렬해야 한다.

    배치 쓰기는 정렬해 저장하지만 `apply_correction` 은 새 edge 를 리스트 뒤에 덧붙이고 끝난다.
    "저장된 순서를 그대로 낸다"고 구현하면 **편집한 사용자에게만** 순서가 어긋난다.
    """
    document = _document(_edge("A", predicate="purchased"), _edge("B", predicate="prefers"))

    predicates = [edge.predicate for edge in project_edges(document, settings=settings)]

    assert predicates == ["prefers", "purchased"]


def test_the_same_document_projects_identically_twice(settings: Settings) -> None:
    """바이트 동일 — 같은 저장 상태를 두 번 투영하면 완전히 같다."""
    document = _document(_edge("A"), _edge("B", predicate="avoids"))

    first = [e.model_dump(by_alias=True) for e in project_edges(document, settings=settings)]
    second = [e.model_dump(by_alias=True) for e in project_edges(document, settings=settings)]

    assert first == second


# ─────────── 모집단 (REQ-PGRAPH-021 · 076) ───────────


def test_superseded_is_not_projected(settings: Settings) -> None:
    """밀려난 옛 취향은 나타나지 않는다 — 요약 생성이 같은 규칙을 쓴다(REQ-PGRAPH-022)."""
    document = _document(_edge("live"), _edge("dead", status="superseded"))

    labels = [edge.object.label for edge in project_edges(document, settings=settings)]

    assert labels == ["live"]


def test_sensitive_derivations_are_absent_and_uncounted(settings: Settings) -> None:
    """**[HARD]** 민감 파생은 `edges` 에 없고 **어떤 카운트에도 세지 않는다**(REQ-PGRAPH-076).

    placeholder 도 두지 않는다 — 자리만 남겨도 "밝히지 않은 무언가가 있다"는 신호가 된다.
    """
    document = _document(_edge("ok"), _edge("secret", derived_from_sensitive=True))

    projected = project_edges(document, settings=settings)

    assert len(projected) == 1
    assert "secret" not in str([e.model_dump(by_alias=True) for e in projected])


def test_an_edge_without_its_node_is_skipped(settings: Settings) -> None:
    """참조가 끊긴 edge 는 건너뛴다 — 라벨 없는 항목을 만들어 내지 않는다."""
    document = _document(_edge("A"))
    orphan = _edge("gone")
    document = document.model_copy(update={"edges": [*document.edges, orphan]})

    labels = [edge.object.label for edge in project_edges(document, settings=settings)]

    assert labels == ["A"]


def test_no_document_projects_to_an_empty_list(settings: Settings) -> None:
    """프로필 미보유는 오류가 아니다 — 빈 배열이다."""
    assert project_edges(None, settings=settings) == []


# ─────────── 필드 (§3.8 「응답 필드 기준」 [HARD]) ───────────


def test_an_item_carries_exactly_five_fields(settings: Settings) -> None:
    """5필드보다 많으면 「FE 가 화면에 그리거나 되돌려 보낼 값만 싣는다」를 어긴 것이다."""
    document = _document(_edge("소니", predicate="avoids"))

    item = project_edges(document, settings=settings)[0].model_dump(by_alias=True)

    assert set(item) == {"edgeId", "predicate", "object", "editable", "challenged"}
    assert set(item["object"]) == {"nodeId", "type", "label"}


def test_internal_judgement_never_reaches_the_wire(settings: Settings) -> None:
    """확신도·근거·판정 상태가 응답에 없다 — 전부 저장 모델에 남는다(경계 이동이지 삭제가 아니다)."""
    document = _document(_edge("소니"))

    rendered = str(project_edges(document, settings=settings)[0].model_dump(by_alias=True))

    for leaked in ("confidence", "source", "origin", "evidence", "resolution", "promoted"):
        assert leaked not in rendered


def test_purchased_is_not_editable_but_everything_else_is(settings: Settings) -> None:
    """`editable` 은 **수정** 가능 여부다 — 구매 이력 파생만 `false` 이고 삭제는 별개다."""
    document = _document(_edge("bought", predicate="purchased"), _edge("liked"))

    editable = {e.object.label: e.editable for e in project_edges(document, settings=settings)}

    assert editable == {"bought": False, "liked": True}


def test_challenged_needs_a_pin(settings: Settings) -> None:
    """고정되지 않은 edge 는 반대 관측이 아무리 쌓여도 `challenged` 가 아니다 (REQ-PGRAPH-033).

    이 신호의 뜻은 *"사용자가 고친 취향에 시스템이 이견이 있다"* 이지 "관측이 흔들린다"가 아니다.
    """
    loud = _edge("소니").model_copy(update={"challenge_count": 99})
    document = _document(loud)

    assert project_edges(document, settings=settings)[0].challenged is False


def test_a_pin_past_the_threshold_is_challenged(settings: Settings) -> None:
    """pin + 임계 이상 반대 관측 → **동작 트리거**가 켜진다. 상태는 그대로다."""
    pinned = _edge("소니").model_copy(
        update={
            "user_intent": UserIntent(kind="correct", asserted_at=NOW, mutation_id="m1"),
            "challenge_count": settings.graph_pin_challenge_count,
        }
    )
    document = _document(pinned)

    item = project_edges(document, settings=settings)[0]

    assert item.challenged is True
    assert item.predicate == "likes"  # 상태는 안 바뀐다 — 반영은 명시적 사용자 동작으로만


def test_the_threshold_zero_switches_the_signal_off(settings: Settings) -> None:
    """`graph_pin_challenge_count == 0` 은 **신호를 끈다** — `count >= 0` 으로 짜면 정반대다.

    판정은 `graph_models.is_pin_challenged` 가 소유하고 투영은 호출만 한다. 그 특례가 여기서
    실제로 지켜지는지 본다.
    """
    off = settings.model_copy(update={"graph_pin_challenge_count": 0})
    pinned = _edge("소니").model_copy(
        update={
            "user_intent": UserIntent(kind="correct", asserted_at=NOW, mutation_id="m1"),
            "challenge_count": 99,
        }
    )
    document = _document(pinned)

    assert project_edges(document, settings=off)[0].challenged is False


# ─────────── 상한 없음 (§3.8) ───────────


def test_projection_never_truncates(settings: Settings) -> None:
    """서버 화면 상한이 없다 — 자르면 상한 밖 항목을 **보지도 지우지도** 못한다.

    저장 상한(`profile_graph_max_edges`)보다 많은 문서를 억지로 만들어도 투영은 전량을 낸다.
    """
    over_cap = settings.profile_graph_max_edges + 5
    document = _document(*[_edge(f"b{index:04d}") for index in range(over_cap)])

    assert len(project_edges(document, settings=settings)) == over_cap


# ─────────── 단건 투영 (I-33 응답) ───────────


def test_single_projection_matches_the_list_item(settings: Settings) -> None:
    """I-33 의 `edge` 는 `edges[]` 항목과 **완전히 같은 모양**이어야 한다 (api-spec §3.9.1)."""
    edge = _edge("소니", predicate="avoids")
    document = _document(edge, _edge("애플"))

    single = project_edge(document, edge.edge_id, settings=settings)
    from_list = next(
        item for item in project_edges(document, settings=settings) if item.edge_id == edge.edge_id
    )

    assert single is not None
    assert single.model_dump(by_alias=True) == from_list.model_dump(by_alias=True)


def test_single_projection_of_a_hidden_edge_is_none(settings: Settings) -> None:
    """목록에 없는 것은 단건으로도 못 꺼낸다 — 두 표면이 같은 모집단을 본다."""
    hidden = _edge("secret", derived_from_sensitive=True)
    document = _document(_edge("ok"), hidden)

    assert project_edge(document, hidden.edge_id, settings=settings) is None


# ─────────── 밴드 라벨 렌더 (#581, api-spec §3.8) ───────────


@pytest.mark.parametrize(
    ("node_type", "canonical", "expected"),
    [
        ("priceBand", "30000-50000", "30,000원 이상, 50,000원 이하"),
        ("priceBand", "-50000", "50,000원 이하"),
        ("priceBand", "100000-", "100,000원 이상"),
        ("ratingBand", "4-5", "4점 이상, 5점 이하"),
        ("ratingBand", "4-", "4점 이상"),
        ("ratingBand", "-5", "5점 이하"),
    ],
)
def test_band_labels_are_rendered_as_sentences(
    settings: Settings, node_type: str, canonical: str, expected: str
) -> None:
    """저장 canonical 은 사람이 읽을 수 없다 — 나갈 때만 문장으로 만든다.

    저장을 문장으로 바꾸지 않는 이유는 `node_id` → `edge_id` 가 라벨 파생이기 때문이다
    (REQ-PGRAPH-010). 표시 규칙을 한 번만 손대도 같은 취향이 다른 `edge_id` 를 얻어
    **사용자가 지운 항목이 tombstone 을 비켜 되살아난다.**
    """
    document = _document(_edge(canonical, node_type=node_type, predicate="prefers"))

    item = project_edges(document, settings=settings)[0]

    assert item.object.label == expected
    assert item.object.node_id == f"{node_type}:{canonical}"  # 식별자는 canonical 그대로다


@pytest.mark.parametrize(
    ("node_type", "stored"),
    [
        # 소수점 — 파서를 안 거친 라벨이 저장 문서에 실재한다(`tests/_graph_fixtures.py` 의
        # ratingBand "4.5-5"). `int("4.5")` 는 ValueError 라 방어가 없으면 조회가 500 이 된다.
        ("ratingBand", "4.5-5"),
        ("priceBand", "3만원-5만원"),
        # 경계가 둘 다 없는 값. resolver 라면 애초에 안 만들지만 우회 경로를 가정한다.
        ("priceBand", "-"),
    ],
)
def test_unrenderable_band_label_falls_back_to_the_stored_string(
    settings: Settings, node_type: str, stored: str
) -> None:
    """렌더 못 하면 **원문 그대로** 낸다 — 못생긴 문자열이 500 보다 항상 낫다.

    저장 문서에는 resolver 를 거치지 않은 라벨이 들어올 수 있다(손으로 조립한 픽스처,
    파서 개정 전에 저장된 값). 렌더러가 그 입력에 터지면 사용자는 취향 화면 자체를
    잃고 지울 수도 없게 된다.
    """
    document = _document(_edge(stored, node_type=node_type, predicate="prefers"))

    assert project_edges(document, settings=settings)[0].object.label == stored


def test_non_band_labels_are_untouched(settings: Settings) -> None:
    """밴드가 아닌 타입은 저장 라벨을 그대로 낸다 — 렌더러가 남의 라벨을 건드리지 않는다."""
    document = _document(
        _edge("소니", node_type="brand"),
        _edge("30000-50000", node_type="attribute"),  # 밴드처럼 생겼어도 타입이 아니면 그대로
    )

    labels = {item.object.label for item in project_edges(document, settings=settings)}

    assert labels == {"소니", "30000-50000"}


def test_the_worst_case_rendered_band_fits_the_label_cap(settings: Settings) -> None:
    """렌더 최악값이 `profile_graph_label_max_chars` 를 넘지 않는다 — **여유가 정확히 0이다**.

    api-spec §3.8 이 "상한은 저장 라벨 기준"이라고 적은 근거가 이 수치다. 상한을 낮추거나
    표시 형식에 글자를 더하면 여기서 먼저 깨진다.
    """
    bigint_max = 9_223_372_036_854_775_807
    document = _document(
        _edge(f"{bigint_max - 1}-{bigint_max}", node_type="priceBand", predicate="prefers")
    )

    label = project_edges(document, settings=settings)[0].object.label

    assert len(label) == settings.profile_graph_label_max_chars == 60


def test_single_projection_renders_the_band_label_too(settings: Settings) -> None:
    """I-33 응답도 목록과 같은 문장을 낸다 — 두 표면이 `_view` 를 공유함을 밴드로도 고정한다."""
    edge = _edge("-50000", node_type="priceBand", predicate="prefers")
    document = _document(edge)

    single = project_edge(document, edge.edge_id, settings=settings)

    assert single is not None and single.object.label == "50,000원 이하"
