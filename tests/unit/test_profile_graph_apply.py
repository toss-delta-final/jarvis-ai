"""`apply_edge_mutation` 조립 — 잠금·저널·CAS·감사·롤백이 한 경로에 모이는 지점 (#358).

**이 이슈 완료 조건 대부분이 여기서 처음으로 함께 검증된다.** 앞 단계들이 각각 원장·감사·변형을
따로 세웠다면, 여기서는 그것들이 §7.2 의 순서대로 엮이는지를 잰다.

    잠금 → 재전송이면 최초 응답 재생 → 대상 부재면 404 → 원장 claim → revision CAS
      → 순수 변형(no-op 이면 claim 해제) → 문서 쓰기 → 감사 → 원장 완료

순서가 계약인 이유 셋:
  - **재전송 판정이 404 판정보다 앞이다.** 삭제가 성공하면 그 edge 는 문서에서 사라지므로,
    뒤집으면 정상적인 네트워크 재시도가 전부 `404` 를 받는다 — 실제로는 성공했는데 호출자에게는
    실패로 보인다. 이 순서는 아래 `test_replay_...` 가 실제로 잡아낸 것이다.
  - **404·409 판정이 감사보다 앞이다.** 상태를 바꾸지 않은 요청은 감사 행을 남기지 않는다
    (REQ-PGRAPH-080). 저널을 선행 기록하는 설계라 롤백(`release`)이 그 조항의 집행이다.
  - **감사·원장 완료가 문서 쓰기보다 뒤다.** 문서가 안 바뀌었는데 "바꿨다"는 기록만 남으면
    감사가 거짓이 되고, 원장은 일어나지 않은 일의 응답을 재생한다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.graph_errors import (
    GraphEdgeNotFound,
    GraphStoreUnavailable,
    GraphVersionConflict,
)
from app.agents.profile.graph_models import GraphDocument, GraphEdge, GraphNode, make_edge_id
from app.agents.profile.store import get_profile_store, reset_profile_store

USER = "358"
SONY = make_edge_id("likes|brand:소니")
NOW = "2026-08-10T00:00:00+00:00"


def _edge(label: str = "소니", *, predicate: str = "likes") -> GraphEdge:
    node_id = f"brand:{label}"
    edge_key = f"{predicate}|{node_id}"
    return GraphEdge(
        edge_key=edge_key,
        edge_id=make_edge_id(edge_key),
        node_id=node_id,
        predicate=predicate,
        status="active",
        promoted=True,
        origin="machine",
        source_latest="conversation",
        confidence=0.5,
        evidence_count=2,
        evidence_by_source={"conversation": 2},
        evidence_refs=["f1"],
        first_observed_at="2026-07-01T00:00:00+00:00",
        last_observed_at="2026-07-02T00:00:00+00:00",
        decay_evaluated_at="2026-07-02T00:00:00+00:00",
        valid_from="2026-07-02T00:00:00+00:00",
        superseded_by=None,
        suppressed_at=None,
        user_intent=None,
        challenge_count=0,
        derived_from_sensitive=False,
        sensitive_topic=None,
    )


async def _seed(revision: int = 42) -> GraphDocument:
    store = await get_profile_store()
    document = GraphDocument(
        revision=revision,
        nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
        edges=[_edge()],
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=NOW,
        tombstones=[],
    )
    await store.set_graph(USER, document)
    return document


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


# ─────────── 성공 경로 ───────────


async def test_suppress_applies_and_bumps_the_revision() -> None:
    """삭제가 적용되고 `revision` 이 오른다 — 다음 변경의 `If-Match` 가 바뀐다."""
    await _seed(revision=42)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeSuppress",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    assert result.graph_version == "g43"
    assert result.replayed is False
    store = await get_profile_store()
    document = await store.get_graph(USER)
    assert document is not None and document.revision == 43
    assert document.edges == []
    assert [t.edge_id for t in document.tombstones] == [SONY]


async def test_success_writes_exactly_one_audit_row() -> None:
    """상태를 바꾼 요청은 감사 행을 **하나** 남긴다 (REQ-PGRAPH-080)."""
    await _seed()

    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeSuppress",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    rows = await graph_journal.list_audit(user_id=int(USER))
    assert len(rows) == 1
    assert rows[0].action == "edgeSuppress"
    assert rows[0].graph_version_before == "g42"
    assert rows[0].graph_version_after == "g43"
    assert rows[0].edge_id_before == SONY
    assert "소니" not in " ".join(str(v) for v in vars(rows[0]).values())


async def test_if_match_accepts_the_quoted_form() -> None:
    """`If-Match: "g42"` 와 `g42` 는 동등하다 (api-spec §3.9)."""
    await _seed()

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeSuppress",
        edge_id=SONY,
        if_match='"g42"',
        request_id="req-1",
        now=NOW,
    )

    assert result.graph_version == "g43"


# ─────────── 선행조건 불일치 (REQ-PGRAPH-040) ───────────


async def test_stale_if_match_conflicts_and_carries_the_latest_version() -> None:
    """불일치는 `409` 이고 **최신 버전을 함께** 준다 — 없으면 클라이언트가 재시도할 수 없다."""
    await _seed(revision=42)

    with pytest.raises(GraphVersionConflict) as excinfo:
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id=SONY,
            if_match="g41",  # 낡았다
            request_id="req-1",
            now=NOW,
        )

    assert excinfo.value.latest_graph_version == "g42"


async def test_conflict_leaves_no_audit_row_and_no_partial_write() -> None:
    """실패한 요청은 감사도 문서도 건드리지 않는다 — **부분 적용이 없다**."""
    await _seed(revision=42)

    with pytest.raises(GraphVersionConflict):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id=SONY,
            if_match="g41",
            request_id="req-1",
            now=NOW,
        )

    assert await graph_journal.list_audit(user_id=int(USER)) == []
    store = await get_profile_store()
    document = await store.get_graph(USER)
    assert document is not None and document.revision == 42
    assert len(document.edges) == 1


async def test_conflict_leaves_no_ledger_row_behind() -> None:
    """409 는 원장에 흔적을 남기지 않는다 — claim 을 되돌리기 때문이다.

    **이 단언이 `release` 를 재는 유일한 지점이다.** 올바른 `If-Match` 로 재시도하는 경로로는
    검증되지 않는다 — `If-Match` 가 바뀌면 파생 키도 바뀌어 남은 claim 이 애초에 방해하지 못한다.
    남은 행의 실제 해악은 아래 no-op 테스트가 보여준다(같은 키를 쓰는 후속 요청이 막힌다).
    """
    await _seed(revision=42)
    key = graph_journal.derived_key("edgeSuppress", USER, SONY, "g41")

    with pytest.raises(GraphVersionConflict):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id=SONY,
            if_match="g41",
            request_id="req-1",
            now=NOW,
        )

    assert await graph_journal.lookup(key) is None


# ─────────── 대상 부재 (404) ───────────


async def test_absent_edge_raises_before_touching_the_ledger_or_audit() -> None:
    """없는 대상은 `404` 이고 원장·감사에 흔적을 남기지 않는다.

    판정이 claim 보다 **앞**이라 진행 중 표식조차 만들지 않는다 — 만들면 그 사용자의 다음
    정당한 요청이 "진행 중"으로 막힌다.
    """
    await _seed()

    with pytest.raises(GraphEdgeNotFound):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id="e_nonexistent",
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )

    assert await graph_journal.list_audit(user_id=int(USER)) == []
    key = graph_journal.derived_key("edgeSuppress", USER, "e_nonexistent", "g42")
    assert await graph_journal.lookup(key) is None


# ─────────── 재전송 (REQ-PGRAPH-043) ───────────


async def test_replay_returns_the_first_response_without_reapplying() -> None:
    """같은 파생 키의 재전송은 최초 응답을 재생하고 **부작용은 1회**다."""
    await _seed(revision=42)
    first = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeSuppress",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    second = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeSuppress",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1-retry",
        now=NOW,
    )

    assert first.graph_version == second.graph_version == "g43"
    assert second.replayed is True
    # 부작용 1회 — revision 이 두 번 오르지 않았고 감사도 한 행이다.
    store = await get_profile_store()
    document = await store.get_graph(USER)
    assert document is not None and document.revision == 43
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1


# ─────────── no-op (REQ-PGRAPH-080) ───────────


async def test_no_op_correction_leaves_no_audit_row_and_no_revision_bump() -> None:
    """같은 값으로 고치는 것은 상태 변경이 아니다 — 감사도 revision 도 그대로다."""
    await _seed(revision=42)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="likes",
        node=GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False),
    )

    assert result.graph_version == "g42"  # 안 올랐다
    assert await graph_journal.list_audit(user_id=int(USER)) == []
    store = await get_profile_store()
    document = await store.get_graph(USER)
    assert document is not None and document.revision == 42


async def test_a_no_op_does_not_block_a_real_change_with_the_same_precondition() -> None:
    """no-op 뒤에 같은 `If-Match` 로 온 **진짜 변경**이 막히면 안 된다.

    **여기가 no-op 롤백이 실제로 load-bearing 한 자리다.** 파생 키에는 본문이 없으므로 두 요청의
    키가 **같다** — no-op 이 claim 을 남겨 두면 뒤따르는 정당한 수정이 "진행 중"으로 거부된다.
    """
    await _seed(revision=42)
    same = GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)

    no_op = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="likes",  # 같은 값 — 상태가 안 바뀐다
        node=same,
    )
    assert no_op.graph_version == "g42"

    real = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",  # 같은 선행조건 → **같은 파생 키**
        request_id="req-2",
        now=NOW,
        predicate="avoids",
        node=same,
    )

    assert real.graph_version == "g43"
    assert real.edge_id == make_edge_id("avoids|brand:소니")


async def test_same_key_with_a_different_body_conflicts_instead_of_replaying() -> None:
    """같은 파생 키·다른 본문은 재생하지 않고 충돌이다.

    파생 키에 본문이 없어 생기는 구멍이다 — 그대로 재생하면 호출자는 **자기가 보내지도 않은
    변경**의 결과를 성공으로 받는다. `request_fp` 가 그걸 가른다.
    """
    await _seed(revision=42)
    same = GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)
    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="avoids",
        node=same,
    )

    with pytest.raises(GraphVersionConflict):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeUpdate",
            edge_id=SONY,
            if_match="g42",  # 스테일 — 같은 키인데 본문이 다르다
            request_id="req-2",
            now=NOW,
            predicate="interestedIn",
            node=same,
        )


# ─────────── 크래시 재개 (완료 조건 3) ───────────


async def test_crashed_claim_is_resumed_after_the_lease_expires() -> None:
    """`processing` 잔재가 남은 상태에서 다시 오면 재선점해 이어서 처리한다 (SPEC §8).

    워커가 claim 만 하고 죽은 상황을 lease 만료로 재현한다. 재선점 없이 막아 버리면 그 사용자의
    그 변경은 TTL 이 끝날 때까지 영영 안 된다 — 크래시 한 번이 기능을 잠그는 셈이다.
    """
    await _seed(revision=42)
    key = graph_journal.derived_key("edgeSuppress", USER, SONY, "g42")
    # 죽은 워커의 잔재: claim 은 했지만 complete 를 못 했고 lease 가 이미 만료됐다.
    assert await graph_journal.claim(key, user_id=int(USER), scope_id=SONY, lease_s=0)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeSuppress",
        edge_id=SONY,
        if_match="g42",
        request_id="req-resume",
        now=NOW,
    )

    assert result.graph_version == "g43"
    assert result.replayed is False
    # **중복 부작용이 없다** — 재개했을 뿐 두 번 적용하지 않았다.
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43
    assert len(document.tombstones) == 1
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1


async def test_a_live_claim_is_not_stolen_from_the_worker_holding_it() -> None:
    """lease 가 살아 있으면 재선점하지 않는다 — 진행 중인 워커의 작업을 빼앗으면 부작용이 2회다."""
    await _seed(revision=42)
    key = graph_journal.derived_key("edgeSuppress", USER, SONY, "g42")
    assert await graph_journal.claim(key, user_id=int(USER), scope_id=SONY, lease_s=60)

    with pytest.raises(GraphVersionConflict):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id=SONY,
            if_match="g42",
            request_id="req-2",
            now=NOW,
        )

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 42  # 아무 일도 안 일어났다


# ─────────── 저장소 장애 (완료 조건 7) ───────────


async def test_store_failure_becomes_unavailable_and_leaves_the_document_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pg 일시 장애는 `503` 이고 **문서가 무손상**이다 (SPEC §8).

    무손상은 예외 클래스가 아니라 **쓰기 순서**로 보장된다 — 문서 쓰기가 실패하면 그 뒤의
    감사·원장 완료는 아예 실행되지 않는다. 그래서 "장애 뒤에도 revision 이 그대로"를 함께 잰다.
    """
    await _seed(revision=42)
    store = await get_profile_store()

    async def _boom(*args, **kwargs):
        raise TimeoutError("pg-profile timed out")

    monkeypatch.setattr(type(store), "set_graph", _boom)

    with pytest.raises(GraphStoreUnavailable):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id=SONY,
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )

    monkeypatch.undo()
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 42
    assert len(document.edges) == 1
    assert document.tombstones == []
    assert await graph_journal.list_audit(user_id=int(USER)) == []


async def test_a_plain_bug_is_not_disguised_as_a_transient_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """장애가 아닌 예외를 `503` 으로 묶으면 진짜 버그가 재시도만 반복하며 숨는다."""
    await _seed()
    store = await get_profile_store()

    async def _bug(*args, **kwargs):
        raise ValueError("programmer error")

    monkeypatch.setattr(type(store), "set_graph", _bug)

    with pytest.raises(ValueError, match="programmer error"):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeSuppress",
            edge_id=SONY,
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )


# ─────────── 동시성 (완료 조건 1) ───────────


async def test_concurrent_writers_with_the_same_if_match_elect_one_winner() -> None:
    """같은 `If-Match` 로 동시에 두 변경을 보내면 하나만 성공하고 다른 하나는 `409` 다.

    **부분 적용이 없다** — 최종 문서에 단일 변경만 반영된다. 파생 키가 달라(scopeId 가 다르다)
    멱등 원장이 아니라 **`revision` CAS** 가 갈라 주는 경로다.
    """
    await _seed(revision=42)
    await (await get_profile_store()).set_graph(
        USER,
        (await (await get_profile_store()).get_graph(USER)).model_copy(  # type: ignore[union-attr]
            update={
                "edges": [_edge("소니"), _edge("애플")],
                "nodes": [
                    GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False),
                    GraphNode(node_id="brand:애플", type="brand", label="애플", verified=False),
                ],
            }
        ),
    )

    async def suppress(label: str):
        try:
            return await graph_journal.apply_edge_mutation(
                user_id=int(USER),
                action="edgeSuppress",
                edge_id=make_edge_id(f"likes|brand:{label}"),
                if_match="g42",
                request_id=f"req-{label}",
                now=NOW,
            )
        except GraphVersionConflict as exc:
            return exc

    results = await asyncio.gather(suppress("소니"), suppress("애플"))

    conflicts = [r for r in results if isinstance(r, GraphVersionConflict)]
    assert len(conflicts) == 1, "동시 변경 중 정확히 하나만 충돌해야 한다"
    store = await get_profile_store()
    document = await store.get_graph(USER)
    assert document is not None and document.revision == 43  # 두 번 오르지 않았다
    assert len(document.tombstones) == 1  # 부분 적용 없음 — 한 건만 반영됐다
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1
