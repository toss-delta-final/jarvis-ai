"""`apply_edge_mutation` 조립 — 잠금·저널·CAS·감사·롤백이 한 경로에 모이는 지점 (#358).

**이 이슈 완료 조건 대부분이 여기서 처음으로 함께 검증된다.** 앞 단계들이 각각 원장·감사·변형을
따로 세웠다면, 여기서는 그것들이 §7.2 의 순서대로 엮이는지를 잰다.

    잠금 → 완료분이면 최초 응답 재생 → **원장에 흔적이 없고** 대상도 없으면 404
      → 원장 claim(잔재면 takeover) → 재개 판정 → revision CAS
      → 순수 변형(no-op 이면 claim 해제) → 의도 기록 → 문서 쓰기 → 감사 → 원장 완료

순서가 계약인 이유 넷:
  - **재전송 판정이 404 판정보다 앞이다.** 삭제가 성공하면 그 edge 는 문서에서 사라지므로,
    뒤집으면 정상적인 네트워크 재시도가 전부 `404` 를 받는다 — 실제로는 성공했는데 호출자에게는
    실패로 보인다. 이 순서는 `test_replay_...` 가 실제로 잡아낸 것이다.
  - **404 판정은 원장을 본다** (PR #540 리뷰). 문서 쓰기는 됐는데 감사·완료 전에 끊긴 창에서는
    대상이 이미 없는데, 그걸 "없으니 404"로 뭉뚱그리면 api-spec §3.9.2 가 ⚠️ 로 금지한 바로 그
    판정이 된다. 원장에 흔적이 있으면 **재개**이지 404 가 아니다.
  - **404·409 판정이 감사보다 앞이다.** 상태를 바꾸지 않은 요청은 감사 행을 남기지 않는다
    (REQ-PGRAPH-080). 저널을 선행 기록하는 설계라 롤백(`release`)이 그 조항의 집행이다.
  - **감사·원장 완료가 문서 쓰기보다 뒤다.** 문서가 안 바뀌었는데 "바꿨다"는 기록만 남으면
    감사가 거짓이 되고, 원장은 일어나지 않은 일의 응답을 재생한다. 그 사이 창은 **의도 기록**
    (`record_intent`)이 메운다 — 재개가 그 값으로 최초 응답을 재구성한다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.graph_errors import (
    GraphEdgeNotEditable,
    GraphEdgeNotFound,
    GraphObjectUnknown,
    GraphStoreUnavailable,
    GraphVersionConflict,
)
from app.agents.profile.graph_models import GraphDocument, GraphEdge, GraphNode, make_edge_id
from app.agents.profile.resolver import ObjectSpec
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
        action="edgeDelete",
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
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    rows = await graph_journal.list_audit(user_id=int(USER))
    assert len(rows) == 1
    assert rows[0].action == "edgeDelete"
    assert rows[0].graph_version_before == "g42"
    assert rows[0].graph_version_after == "g43"
    assert rows[0].edge_id_before == SONY
    assert "소니" not in " ".join(str(v) for v in vars(rows[0]).values())


async def test_if_match_accepts_the_quoted_form() -> None:
    """`If-Match: "g42"` 와 `g42` 는 동등하다 (api-spec §3.9)."""
    await _seed()

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
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
            action="edgeDelete",
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
            action="edgeDelete",
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
    key = graph_journal.derived_key("edgeDelete", USER, SONY, "g41")

    with pytest.raises(GraphVersionConflict):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeDelete",
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
            action="edgeDelete",
            edge_id="e_nonexistent",
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )

    assert await graph_journal.list_audit(user_id=int(USER)) == []
    key = graph_journal.derived_key("edgeDelete", USER, "e_nonexistent", "g42")
    assert await graph_journal.lookup(key) is None


# ─────────── 재전송 (REQ-PGRAPH-043) ───────────


async def test_replay_returns_the_first_response_without_reapplying() -> None:
    """같은 파생 키의 재전송은 최초 응답을 재생하고 **부작용은 1회**다."""
    await _seed(revision=42)
    first = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    second = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
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
        object_spec=ObjectSpec(node_id="brand:소니"),
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
    same = ObjectSpec(node_id="brand:소니")

    no_op = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="likes",  # 같은 값 — 상태가 안 바뀐다
        object_spec=same,
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
        object_spec=same,
    )

    assert real.graph_version == "g43"
    assert real.edge_id == make_edge_id("avoids|brand:소니")


async def test_same_key_with_a_different_body_conflicts_instead_of_replaying() -> None:
    """같은 파생 키·다른 본문은 재생하지 않고 충돌이다.

    파생 키에 본문이 없어 생기는 구멍이다 — 그대로 재생하면 호출자는 **자기가 보내지도 않은
    변경**의 결과를 성공으로 받는다. `request_fp` 가 그걸 가른다.
    """
    await _seed(revision=42)
    same = ObjectSpec(node_id="brand:소니")
    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="avoids",
        object_spec=same,
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
            object_spec=same,
        )


# ─────────── 원문 물리 삭제 (REQ-PGRAPH-025 [HARD], AC-PGRAPH-13) ───────────


async def test_delete_physically_removes_the_backing_facts() -> None:
    """[HARD] 삭제는 **근거 fact 까지** 그 자리에서 지운다 (REQ-PGRAPH-025, AC-PGRAPH-13).

    edge 만 지우고 fact 를 남기면 **사용자가 "지웠다"고 믿는 문장의 원문이 저장소에 그대로
    남는다.** 응답이 돌아온 시점에 이미 없어야 하고, 유예를 기다리는 스윕은 존재하지 않는다.
    """
    store = await get_profile_store()
    await store.add_fact(
        USER,
        "소니 이어폰을 선호한다",
        graph_triples=[
            {
                "node": {
                    "node_id": "brand:소니",
                    "type": "brand",
                    "label": "소니",
                    "verified": False,
                    "resolution": None,
                },
                "predicate": "likes",
                "edge_key": "likes|brand:소니",
                "edge_id": SONY,
                "salience": 0.9,
                "source": "conversation",
            }
        ],
    )
    await _seed(revision=42)

    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    assert await store.get_facts(USER) == []


async def test_delete_keeps_facts_that_only_back_other_edges() -> None:
    """다른 취향만 담은 fact 는 남는다 — 삭제가 무관한 개인화까지 지우면 안 된다."""
    store = await get_profile_store()
    await store.add_fact(
        USER,
        "애플을 선호한다",
        graph_triples=[
            {
                "node": {
                    "node_id": "brand:애플",
                    "type": "brand",
                    "label": "애플",
                    "verified": False,
                    "resolution": None,
                },
                "predicate": "likes",
                "edge_key": "likes|brand:애플",
                "edge_id": make_edge_id("likes|brand:애플"),
                "salience": 0.9,
                "source": "conversation",
            }
        ],
    )
    await _seed(revision=42)

    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    assert await store.get_facts(USER) == ["애플을 선호한다"]


# ─────────── 크래시 재개 (완료 조건 3) ───────────


async def test_crashed_claim_is_resumed_after_the_lease_expires() -> None:
    """`processing` 잔재가 남은 상태에서 다시 오면 재선점해 이어서 처리한다 (SPEC §8).

    워커가 claim 만 하고 죽은 상황을 lease 만료로 재현한다. 재선점 없이 막아 버리면 그 사용자의
    그 변경은 TTL 이 끝날 때까지 영영 안 된다 — 크래시 한 번이 기능을 잠그는 셈이다.
    """
    await _seed(revision=42)
    key = graph_journal.derived_key("edgeDelete", USER, SONY, "g42")
    # 죽은 워커의 잔재: claim 은 했지만 complete 를 못 했고 lease 가 이미 만료됐다.
    assert await graph_journal.claim(key, user_id=int(USER), scope_id=SONY, lease_s=0)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
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


async def test_retry_after_a_crash_between_write_and_complete_replays_instead_of_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #540 리뷰] **문서 쓰기는 됐는데 완료 표시 전에 끊긴** 뒤의 재시도는 `200 replayed` 다.

    이 창은 SIGKILL 뿐 아니라 **평범한 일시 장애**로도 열린다 — `record_audit` 이 pg blip 으로
    실패하면 호출자는 `503` 을 받는데 그때 문서는 이미 쓰였고 원장은 `processing` 이다. 그 뒤의
    정상적인 재시도가 `404` 를 받으면 api-spec §3.9.2 가 ⚠️ 로 금지한 "edge 존재 여부로 뭉뚱그리기"
    를 그대로 하는 것이다.

    자가 복구도 안 되던 자리다 — 404 판정이 claim 보다 앞이고 원장을 아예 보지 않아 TTL 이 지나도
    같은 답이 나왔다.
    """
    await _seed(revision=42)
    boom = {"n": 0}
    original = graph_journal.record_audit

    async def _fail_once(**kwargs):
        boom["n"] += 1
        if boom["n"] == 1:
            raise TimeoutError("pg-profile timed out")
        return await original(**kwargs)

    monkeypatch.setattr(graph_journal, "record_audit", _fail_once)

    with pytest.raises(GraphStoreUnavailable):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeDelete",
            edge_id=SONY,
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )

    # 문서는 이미 바뀌었고 원장은 미완료다 — 크래시 창이 재현됐다.
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.edges == []
    key = graph_journal.derived_key("edgeDelete", USER, SONY, "g42")
    hit = await graph_journal.lookup(key)
    assert hit is not None and hit.status == "processing"

    retry = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1-retry",
        now=NOW,
    )

    assert retry.replayed is True
    assert retry.graph_version == "g43"
    # 부작용은 1회 — revision 이 두 번 오르지 않았고 감사도 한 행이다.
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1


async def test_a_genuinely_absent_edge_is_still_404() -> None:
    """원장에 흔적이 없으면 여전히 `404` 다 — 재개 경로가 진짜 오류를 삼키면 안 된다."""
    await _seed(revision=42)

    with pytest.raises(GraphEdgeNotFound):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeDelete",
            edge_id="e_nonexistent",
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )


async def test_resume_does_not_write_a_second_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """감사를 쓴 **뒤** 끊긴 경우, 재개가 감사를 두 번 남기면 안 된다 (REQ-PGRAPH-080).

    "한 변경 = 한 감사 행"이므로 재개는 감사 쓰기에 대해 멱등이어야 한다.
    """
    await _seed(revision=42)
    original = graph_journal.complete

    async def _fail_complete(*args, **kwargs):
        raise TimeoutError("pg-profile timed out")

    monkeypatch.setattr(graph_journal, "complete", _fail_complete)
    with pytest.raises(GraphStoreUnavailable):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeDelete",
            edge_id=SONY,
            if_match="g42",
            request_id="req-1",
            now=NOW,
        )
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1  # 감사는 이미 남았다

    monkeypatch.setattr(graph_journal, "complete", original)
    retry = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1-retry",
        now=NOW,
    )

    assert retry.replayed is True
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1  # 두 번 안 남는다


async def test_a_completed_request_is_never_re_executed_even_by_takeover() -> None:
    """**완료분은 어떤 경우에도 재실행하지 않는다** — 그게 "부작용 1회"의 마지막 방어선이다.

    크래시 재개를 위해 `processing` 잔재는 lease 가 살아 있어도 재선점하지만(`claim(takeover=)`),
    `completed` 는 그 대상이 아니다. 재선점하면 같은 변경이 두 번 적용된다.

    **이전 판(`lease 가 살아 있으면 재선점하지 않는다`)은 폐기했다.** 그래프 락이 사용자당 그래프
    변경의 진짜 상호배제라, 그 락을 쥔 채 `processing` 잔재를 봤다면 원래 주인은 지금 돌고 있지
    않다 — lease 만료를 기다리면 재시도가 최대 TTL 동안 `404`/`409` 를 받는다(PR #540 리뷰).
    """
    await _seed(revision=42)
    first = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )
    key = graph_journal.derived_key("edgeDelete", USER, SONY, "g42")

    # 완료분은 takeover 로도 재선점되지 않는다.
    assert (
        await graph_journal.claim(key, user_id=int(USER), scope_id=SONY, lease_s=60, takeover=True)
        is None
    )

    retry = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-2",
        now=NOW,
    )

    assert retry.replayed is True and retry.graph_version == first.graph_version
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43  # 두 번 적용되지 않았다
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1


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
            action="edgeDelete",
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
            action="edgeDelete",
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
                action="edgeDelete",
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


# ─────────── I-33 부분 변경 · 대상 해석 · editable (#360) ───────────


async def _seed_purchased(revision: int = 42) -> str:
    """구매 이력 파생 edge 하나만 있는 문서. 반환값은 그 `edge_id`."""
    store = await get_profile_store()
    edge = _edge("애플", predicate="purchased")
    await store.set_graph(
        USER,
        GraphDocument(
            revision=revision,
            nodes=[GraphNode(node_id="brand:애플", type="brand", label="애플", verified=False)],
            edges=[edge],
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )
    return edge.edge_id


async def test_changing_only_the_predicate_keeps_the_target() -> None:
    """`predicate` 만 보내면 **대상은 기존 edge 에서 가져온다** (api-spec §3.9.1).

    구 구현은 `predicate`·`node` 중 하나라도 없으면 `ValueError` 를 올렸고, 그것이
    `is_state_store_unavailable` 에 안 걸려 그대로 전파돼 **500** 이 나갔다.
    """
    await _seed(revision=42)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="avoids",  # object 는 생략
    )

    assert result.edge_id == make_edge_id("avoids|brand:소니")
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43


async def test_changing_only_the_object_keeps_the_relation() -> None:
    """`object` 만 보내면 **관계가 유지된다** (api-spec §3.9.1)."""
    await _seed(revision=42)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        object_spec=ObjectSpec(node_type="brand", label="애플"),  # predicate 는 생략
    )

    assert result.edge_id == make_edge_id("likes|brand:애플")


async def test_editing_a_purchased_edge_is_refused() -> None:
    """구매 이력 파생은 수정 불가 — `409 PROFILE_EDGE_NOT_EDITABLE` (api-spec §3.9.1).

    구매는 의견이 아니라 사실이라 사용자가 뒤집을 대상이 아니다. 거부는 **문서·감사 무손상**이다.
    """
    edge_id = await _seed_purchased(revision=42)

    with pytest.raises(GraphEdgeNotEditable):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeUpdate",
            edge_id=edge_id,
            if_match="g42",
            request_id="req-1",
            now=NOW,
            predicate="avoids",
        )

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 42
    assert await graph_journal.list_audit(user_id=int(USER)) == []


async def test_not_editable_wins_over_a_stale_if_match() -> None:
    """두 `409` 가 동시에 성립하면 **재조회로 안 바뀌는 쪽**을 먼저 알린다 (api-spec §3.9.1 v0.32.7).

    `PROFILE_VERSION_CONFLICT` 를 먼저 내면 FE 가 규약대로 재조회 후 재시도하고, 그 재시도가
    결국 `PROFILE_EDGE_NOT_EDITABLE` 을 받아 왕복 한 번이 낭비된다.
    """
    edge_id = await _seed_purchased(revision=42)

    with pytest.raises(GraphEdgeNotEditable):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeUpdate",
            edge_id=edge_id,
            if_match="g41",  # 스테일 — VERSION_CONFLICT 도 동시에 성립한다
            request_id="req-1",
            now=NOW,
            predicate="avoids",
        )


async def test_deleting_a_purchased_edge_is_allowed() -> None:
    """삭제는 `editable` 과 무관하다 — `editable` 은 **수정만** 막는다 (api-spec §3.9.2)."""
    edge_id = await _seed_purchased(revision=42)

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=edge_id,
        if_match="g42",
        request_id="req-1",
        now=NOW,
    )

    assert result.suppressed is True
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.edges == []


async def test_an_unresolvable_label_is_refused_without_touching_the_document() -> None:
    """어휘 밖 라벨은 `GraphObjectUnknown`(→ `400`) — **추측해서 붙이지 않는다**.

    가까운 대상으로 스냅하면 배치가 만드는 식별자와 달라져 같은 취향이 두 `edgeId` 를 얻는다.
    """
    await _seed(revision=42)

    with pytest.raises(GraphObjectUnknown):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeUpdate",
            edge_id=SONY,
            if_match="g42",
            request_id="req-1",
            now=NOW,
            object_spec=ObjectSpec(node_type="priceBand", label="가성비"),
        )

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 42
    assert await graph_journal.list_audit(user_id=int(USER)) == []


async def test_a_replayed_patch_returns_the_original_edge_even_after_it_was_deleted() -> None:
    """재전송은 **최초 응답 본문 그대로**다 — 그 사이 그 edge 가 삭제됐어도 (REQ-PGRAPH-043).

    **여기가 원장이 투영된 edge 를 드는 이유다.** "재생 시 현재 문서에서 다시 읽어 조립한다"로
    구현하면 이 창에서 조립할 대상이 없어 `404` 나 `500` 이 나간다:

        PATCH(If-Match g42) → 200, edge Y
        DELETE(If-Match g43) → 200            # Y 소멸
        PATCH 재시도(If-Match g42)             # 네트워크 재시도, 원장 TTL 24h 내
          → 문서에 Y 가 없다

    보관을 빼면 이 테스트가 깨진다.
    """
    await _seed(revision=42)
    first = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="avoids",
    )
    assert first.edge is not None
    await graph_journal.apply_edge_mutation(  # 그 edge 를 지운다
        user_id=int(USER),
        action="edgeDelete",
        edge_id=first.edge_id or "",
        if_match="g43",
        request_id="req-2",
        now=NOW,
    )

    replay = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,
        if_match="g42",  # 최초와 같은 선행조건 → 같은 파생 키
        request_id="req-3",
        now=NOW,
        predicate="avoids",
    )

    assert replay.replayed is True
    assert replay.graph_version == first.graph_version
    # **내용까지** 본다 — `edge is None` 이면 둘이 같아져 동등 비교만으로는 안 걸린다.
    assert replay.edge is not None
    assert replay.edge == first.edge
    assert replay.edge["object"]["label"] == "소니"
    assert replay.edge["predicate"] == "avoids"


async def _seed_hidden(*, status: str = "active", sensitive: bool = False) -> str:
    """GET(I-32)에 **안 나오는** edge 하나만 있는 문서. 반환값은 그 `edge_id`."""
    store = await get_profile_store()
    edge = _edge().model_copy(
        update={
            "status": status,
            "derived_from_sensitive": sensitive,
            "sensitive_topic": "health" if sensitive else None,
        }
    )
    await store.set_graph(
        USER,
        GraphDocument(
            revision=42,
            nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
            edges=[edge],
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )
    return edge.edge_id


@pytest.mark.parametrize(
    ("status", "sensitive"),
    [
        pytest.param("superseded", False, id="superseded"),
        pytest.param("active", True, id="sensitive-derivation"),
    ],
)
@pytest.mark.parametrize("action", ["edgeUpdate", "edgeDelete"])
async def test_an_edge_that_the_read_path_hides_is_not_a_valid_target(
    action: str, status: str, sensitive: bool
) -> None:
    """**변경의 대상 경계는 조회의 노출 경계와 같아야 한다** (PR #562 리뷰).

    `edgeId` 는 `sha256("{predicate}|{node_id}")` 라 **사용자별 salt 가 없는 콘텐츠 해시**다 —
    브랜드명만 알면 계산할 수 있다. 대상 판정이 `edge_id` 일치만 보면 두 가지가 뚫린다:

      - **존재 오라클** — GET 에 안 나오는 edge 에 변경을 쏴서 `200`/`404` 로 *"이 취향이 추론된
        적 있나"* 를 알아낸다. 민감 파생은 **존재 자체를 노출하지 않아야** 한다(REQ-PGRAPH-076 [HARD]).
      - **충돌 해소 우회** — `superseded`(병합 엔진이 상충에서 내린 패자)를 수정하면 `_pin` 이
        무조건 `active` 로 되돌려, 같은 노드에 상충하는 active edge 가 둘 생긴다.
        `_resolve_conflicts` 를 전혀 거치지 않는다.

    그래서 **숨긴 edge 는 "없는 것과 같은 응답"** 이다.
    """
    edge_id = await _seed_hidden(status=status, sensitive=sensitive)

    with pytest.raises(GraphEdgeNotFound):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action=action,
            edge_id=edge_id,
            if_match="g42",
            request_id="req-1",
            now=NOW,
            predicate="avoids" if action == "edgeUpdate" else None,
        )

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 42  # 문서 무손상
    assert await graph_journal.list_audit(user_id=int(USER)) == []


async def test_correcting_onto_a_superseded_target_still_merges() -> None:
    """**반대 방향은 막지 않는다** — 보이는 edge 를 고친 결과가 옛 패자와 겹치면 병합한다.

    `apply_correction` 이 *"대상 트리플에 옛 표식이 남아 있으면 걷는다 … 명시적 사용자 동작"*
    이라고 적어 둔 그 경로다. 리뷰 제안대로 `graph_mutations._find` 를 통째로 필터하면 이
    병합 대상 조회까지 막혀 관측 근거를 잃고 `merged` 가 거짓이 된다.
    """
    store = await get_profile_store()
    loser = _edge("소니", predicate="avoids").model_copy(update={"status": "superseded"})
    await store.set_graph(
        USER,
        GraphDocument(
            revision=42,
            nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
            edges=[_edge(), loser],  # likes(보임) + avoids(숨김)
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )

    result = await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeUpdate",
        edge_id=SONY,  # 보이는 쪽을 고친다
        if_match="g42",
        request_id="req-1",
        now=NOW,
        predicate="avoids",  # 결과가 숨겨진 패자와 같은 키가 된다
    )

    assert result.merged is True
    assert result.edge_id == loser.edge_id


async def test_a_node_id_outside_the_graph_is_refused() -> None:
    """형식은 맞지만 그 사용자 그래프에 없는 `nodeId` 는 새로 만들지 않는다 (api-spec §3.9.1)."""
    await _seed(revision=42)

    with pytest.raises(GraphObjectUnknown):
        await graph_journal.apply_edge_mutation(
            user_id=int(USER),
            action="edgeUpdate",
            edge_id=SONY,
            if_match="g42",
            request_id="req-1",
            now=NOW,
            object_spec=ObjectSpec(node_id="brand:애플"),
        )
