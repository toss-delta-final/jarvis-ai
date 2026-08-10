"""전체 초기화와 `revision` 하한 (#358, §7.2 · REQ-PGRAPH-042/061/062/063).

전체 초기화는 **되돌릴 수 없는 동작**이라 두 가지를 동시에 지켜야 한다.

  - 지울 것은 확실히 지운다 — 그래프·fact·요약·세션버퍼·tombstone·전사록(REQ-PGRAPH-061).
  - 지우면 안 되는 것은 남긴다 — **감사 로그**(REQ-PGRAPH-062)와 **개인화 중지 상태**
    (REQ-PGRAPH-063). 감사가 사라지면 파괴 동작이 추적 불가가 되고, 중지가 풀리면 사용자가
    끈 적 없는 개인화가 조용히 다시 켜진다.

그리고 `revision` 은 **초기화로도 되돌아가지 않는다**(REQ-PGRAPH-042). 되돌아가면 같은
`If-Match` 가 서로 다른 상태를 가리켜 낙관적 동시성 전체가 무너진다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.graph_models import GraphDocument, GraphEdge, GraphNode, make_edge_id
from app.agents.profile.store import get_profile_store, reset_profile_store

USER = "358"
SONY = make_edge_id("likes|brand:소니")
NOW = "2026-08-10T00:00:00+00:00"


def _edge(
    label: str = "소니",
    *,
    status: str = "active",
    derived_from_sensitive: bool = False,
) -> GraphEdge:
    key = f"likes|brand:{label}"
    return GraphEdge(
        edge_key=key,
        edge_id=make_edge_id(key),
        node_id=f"brand:{label}",
        predicate="likes",
        status=status,  # type: ignore[arg-type]
        promoted=True,
        origin="machine",
        source_latest="conversation",
        confidence=0.5,
        evidence_count=1,
        evidence_by_source={"conversation": 1},
        evidence_refs=["f1"],
        first_observed_at="2026-07-01T00:00:00+00:00",
        last_observed_at="2026-07-02T00:00:00+00:00",
        decay_evaluated_at="2026-07-02T00:00:00+00:00",
        valid_from="2026-07-02T00:00:00+00:00",
        superseded_by=None,
        suppressed_at=None,
        user_intent=None,
        challenge_count=0,
        derived_from_sensitive=derived_from_sensitive,
        sensitive_topic="health" if derived_from_sensitive else None,
    )


async def _seed(revision: int = 42) -> None:
    store = await get_profile_store()
    await store.set_graph(
        USER,
        GraphDocument(
            revision=revision,
            nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
            edges=[_edge()],
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )
    await store.add_fact(USER, "소니를 선호한다")
    await store.set_summary(USER, "소니 선호", NOW)
    await store.append_session_ctx(f"{USER}:sess-1", "소니 이어폰 보여줘")


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


# ─────────── 지우는 것 (REQ-PGRAPH-061) ───────────


async def test_reset_clears_graph_facts_summary_and_buffer() -> None:
    """개인화 데이터가 실제로 사라진다 — "초기화했다"가 사실이어야 한다."""
    await _seed()

    await graph_journal.reset_graph(user_id=int(USER), if_match="g42", request_id="req-1", now=NOW)

    store = await get_profile_store()
    document = await store.get_graph(USER)
    assert document is not None
    assert document.edges == [] and document.nodes == [] and document.tombstones == []
    assert await store.get_facts(USER) == []
    assert await store.get_summary(USER) is None
    assert await store.get_session_ctx(f"{USER}:sess-1") == []


async def test_reset_leaves_no_label_anywhere_in_the_document() -> None:
    """지운 취향의 라벨 원문이 문서 어디에도 남지 않는다 — tombstone 까지 걷는다."""
    await _seed()
    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-0",
        now=NOW,
    )

    await graph_journal.reset_graph(user_id=int(USER), if_match="g43", request_id="req-1", now=NOW)

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None
    assert "소니" not in repr(document.model_dump(mode="json"))
    assert document.tombstones == []  # 초기화는 tombstone 도 지운다(REQ-PGRAPH-061)


async def test_reset_records_when_it_happened() -> None:
    """`purged_at` 이 남아야 "언제 초기화됐나"를 문서만 보고도 안다."""
    await _seed()

    await graph_journal.reset_graph(user_id=int(USER), if_match="g42", request_id="req-1", now=NOW)

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.purged_at == NOW


# ─────────── purged 응답 (#360, api-spec §3.9.4) ───────────


async def test_reset_reports_purged_in_the_contract_shape() -> None:
    """`purged` 는 계약이 정한 **정확히 2키**다 — `{edges, transcriptTurns}`.

    구 구현은 `{facts, summary, buffers, conversationTurns}` 를 냈다 — 계약과 **한 키도 겹치지
    않는다.** 와이어로 나가는 값이라 이름이 다르면 FE 가 "취향 N건"을 못 그린다.
    """
    await _seed()

    result = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1", now=NOW
    )

    assert result.purged == {"edges": 1, "transcriptTurns": 0}


async def test_purged_edges_counts_only_what_the_user_could_see() -> None:
    """`purged.edges` 는 **I-32 에 보이던 개수**다 — 화면 문구("취향 N건")와 맞아야 한다.

    `superseded` 는 투영에 안 나가고(REQ-PGRAPH-021), 민감 파생은 **어떤 카운트에도 세지 않는다**
    (REQ-PGRAPH-076 [HARD]). 저장 문서의 edge 를 통째로 세면 사용자가 본 적 없는 수가 나간다.
    """
    store = await get_profile_store()
    await store.set_graph(
        USER,
        GraphDocument(
            revision=42,
            nodes=[
                GraphNode(node_id=f"brand:{label}", type="brand", label=label, verified=False)
                for label in ("소니", "애플", "삼성")
            ],
            edges=[
                _edge("소니"),
                _edge("애플", status="superseded"),
                _edge("삼성", derived_from_sensitive=True),
            ],
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )

    result = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1", now=NOW
    )

    assert result.purged == {"edges": 1, "transcriptTurns": 0}


async def test_reset_replay_returns_the_same_purged_counts() -> None:
    """재전송은 **최초 응답 본문 그대로**다 — 두 번째가 0 이 되면 계약 위반이다.

    두 번째 호출 시점에는 이미 다 지워져 있어 다시 세면 전부 0 이다. 원장이 최초 값을 들고
    있어야 한다(REQ-PGRAPH-043).
    """
    await _seed()
    first = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1", now=NOW
    )

    second = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-2", now=NOW
    )

    assert second.replayed is True
    assert second.purged == first.purged == {"edges": 1, "transcriptTurns": 0}


async def test_reset_without_a_profile_reports_zeros() -> None:
    """프로필이 없어도 오류가 아니다 — `purged` 전부 0 으로 성공한다 (api-spec §3.9.4)."""
    result = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g0", request_id="req-1", now=NOW
    )

    assert result.purged == {"edges": 0, "transcriptTurns": 0}


# ─────────── 남기는 것 (REQ-PGRAPH-062/063) ───────────


async def test_reset_preserves_the_audit_log() -> None:
    """[HARD] 초기화는 감사를 지우지 않는다 — 파괴 동작이 추적 불가가 되면 안 된다."""
    await _seed()
    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-0",
        now=NOW,
    )

    await graph_journal.reset_graph(user_id=int(USER), if_match="g43", request_id="req-1", now=NOW)

    rows = await graph_journal.list_audit(user_id=int(USER))
    assert [row.action for row in rows] == ["edgeDelete", "graphReset"]


async def test_reset_does_not_change_the_personalization_flag() -> None:
    """초기화는 개인화 중지 상태를 건드리지 않는다 (REQ-PGRAPH-063).

    지우면 사용자가 끈 적 없는 개인화가 조용히 다시 켜진다 — 중지 플래그를 요약·그래프가 아니라
    **전용 테이블**에 둔 이유가 정확히 이것이다(REQ-PGRAPH-050).
    """
    await _seed()
    await graph_journal.set_personalization_flag(user_id=int(USER), enabled=False, now=NOW)

    await graph_journal.reset_graph(user_id=int(USER), if_match="g42", request_id="req-1", now=NOW)

    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is False


# ─────────── revision 비되돌림 (REQ-PGRAPH-042) ───────────


async def test_reset_bumps_the_revision_instead_of_resetting_it() -> None:
    """초기화해도 `revision` 은 오른다 — 0 으로 돌아가면 옛 `If-Match` 가 되살아난다."""
    await _seed(revision=42)

    result = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1", now=NOW
    )

    assert result.graph_version == "g43"
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43


async def test_revision_never_falls_below_the_audit_floor() -> None:
    """문서가 손상돼 사라져도 `revision` 이 되돌아가지 않는다.

    `get_graph` 는 스키마가 깨진 문서를 조용히 `None` 으로 돌려준다(배치를 죽이지 않으려고).
    그때 `empty_document()` 로 새로 시작하면 revision 이 0 이 되고, 발급됐던 `If-Match: "g43"`
    이 **다른 상태를 가리키게 된다.** 감사는 초기화로도 보존되므로 거기 남은
    `graph_version_after` 최댓값이 항상 존재하는 하한이다.
    """
    await _seed(revision=42)
    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-0",
        now=NOW,
    )  # 감사에 g43 이 남는다
    store = await get_profile_store()
    await store._store.aput(("graph", USER), "v1", {"revision": "not-an-int"})  # noqa: SLF001
    assert await store.get_graph(USER) is None  # 손상 문서는 없는 것으로 보인다

    floor = await graph_journal.next_revision(user_id=int(USER), existing=None)

    assert floor == 44  # 43(감사 하한) + 1 — 0 으로 되돌아가지 않는다


async def test_next_revision_follows_the_document_when_it_is_ahead() -> None:
    """정상 경로에서는 문서가 기준이다 — 감사 하한은 **바닥**이지 정본이 아니다."""
    await _seed(revision=99)
    document = await (await get_profile_store()).get_graph(USER)

    assert await graph_journal.next_revision(user_id=int(USER), existing=document) == 100


# ─────────── 선행조건·멱등 ───────────


async def test_reset_honours_if_match() -> None:
    """초기화도 선행조건을 요구한다 — 되돌릴 수 없는 동작일수록 더 그렇다."""
    await _seed(revision=42)

    with pytest.raises(graph_journal.GraphVersionConflict):
        await graph_journal.reset_graph(
            user_id=int(USER), if_match="g41", request_id="req-1", now=NOW
        )

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and len(document.edges) == 1  # 아무것도 안 지워졌다


async def test_reset_retry_after_a_crash_between_write_and_complete_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #540 리뷰] 초기화도 같은 크래시 창이 있다 — 재시도가 `409` 가 아니라 재생이어야 한다.

    문서를 교체한 뒤 감사·완료 전에 끊기면 revision 이 이미 올라가 있어, 원래 `If-Match` 로 온
    재시도가 CAS 에 걸린다. §7.2 는 "재전송이면 최초 응답 재생"을 요구한다.
    """
    await _seed(revision=42)
    original = graph_journal.record_audit

    async def _fail_once(**kwargs):
        raise TimeoutError("pg-profile timed out")

    monkeypatch.setattr(graph_journal, "record_audit", _fail_once)
    with pytest.raises(graph_journal.GraphStoreUnavailable):
        await graph_journal.reset_graph(
            user_id=int(USER), if_match="g42", request_id="req-1", now=NOW
        )

    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43  # 교체는 이미 됐다

    monkeypatch.setattr(graph_journal, "record_audit", original)
    retry = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1-retry", now=NOW
    )

    assert retry.replayed is True
    assert retry.graph_version == "g43"
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43  # 두 번 오르지 않았다
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1


async def test_reset_invalidates_the_users_ledger_entries() -> None:
    """초기화는 그 사용자의 원장을 무효화한다 (REQ-PGRAPH-028 잔여, AC-PGRAPH-15).

    원장 TTL(시간 단위)이 초기화 시점보다 길 수 있다. 무효화하지 않으면 **purge 로 사라진 edge 를
    향한 재전송이 원장 히트로 "성공"을 재생한다** — 되돌릴 대상이 없는데 호출자에게는 성공으로
    보인다. 개별 삭제 재전송(`200 replayed`)과는 트리거가 다르므로 한 판정으로 묶지 않는다.
    """
    await _seed(revision=42)
    key = graph_journal.derived_key("edgeDelete", USER, SONY, "g42")
    await graph_journal.apply_edge_mutation(
        user_id=int(USER),
        action="edgeDelete",
        edge_id=SONY,
        if_match="g42",
        request_id="req-0",
        now=NOW,
    )
    assert await graph_journal.lookup(key) is not None  # 삭제 응답이 원장에 있다

    await graph_journal.reset_graph(user_id=int(USER), if_match="g43", request_id="req-1", now=NOW)

    assert await graph_journal.lookup(key) is None


async def test_reset_does_not_touch_another_users_ledger() -> None:
    """무효화는 그 사용자 것만 — 남의 재전송 보호를 걷어내면 안 된다."""
    other_key = graph_journal.derived_key("edgeDelete", "999", SONY, "g1")
    token = await graph_journal.claim(other_key, user_id=999, scope_id=SONY, lease_s=60)
    await graph_journal.complete(other_key, token, {"graphVersion": "g2"})
    await _seed(revision=42)

    await graph_journal.reset_graph(user_id=int(USER), if_match="g42", request_id="req-1", now=NOW)

    assert await graph_journal.lookup(other_key) is not None


async def test_reset_replay_does_not_purge_twice() -> None:
    """재전송은 최초 응답을 재생하고 revision 을 두 번 올리지 않는다."""
    await _seed(revision=42)
    first = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1", now=NOW
    )

    second = await graph_journal.reset_graph(
        user_id=int(USER), if_match="g42", request_id="req-1-retry", now=NOW
    )

    assert first.graph_version == second.graph_version == "g43"
    assert second.replayed is True
    document = await (await get_profile_store()).get_graph(USER)
    assert document is not None and document.revision == 43
    assert len(await graph_journal.list_audit(user_id=int(USER))) == 1
