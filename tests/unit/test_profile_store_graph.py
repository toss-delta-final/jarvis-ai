"""그래프 문서 저장·fact 증거 확장 (SPEC-PROFILE-GRAPH-149 §7.1, REQ-PGRAPH-004/010).

그래프는 사용자당 jsonb 문서 **1개**다 — per-user advisory 잠금이 별도 연결 풀에서 잡혀 store
트랜잭션과 결합되지 않아 다중 항목 원자성이 없고, N개로 쪼개면 전부 찢어진 쓰기 상태를 만든다.

fact 쪽은 **비파괴 확장**이다: 기존 `get_facts()`(list[str])는 그대로 두고 fact key·생성 시각·
트리플을 함께 주는 `get_fact_records()`를 더한다. `evidence_refs` 가 fact key 참조라(§5.2)
문자열 목록으로는 채울 수 없다.
"""

import asyncio

import pytest

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    GraphTombstone,
    make_edge_id,
)
from app.agents.profile.store import get_profile_store

NOW = "2026-08-06T00:00:00+00:00"


def _node() -> GraphNode:
    return GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)


def _edge(**overrides: object) -> GraphEdge:
    base: dict = {
        "edge_key": "likes|brand:소니",
        "edge_id": "e_cbbc9dce30b02793",
        "node_id": "brand:소니",
        "predicate": "likes",
        "status": "active",
        "promoted": True,
        "origin": "machine",
        "source_latest": "conversation",
        "confidence": 0.8,
        "evidence_count": 1,
        "evidence_by_source": {"conversation": 1},
        "evidence_refs": ["fact-1"],
        "first_observed_at": NOW,
        "last_observed_at": NOW,
        "decay_evaluated_at": NOW,
        "valid_from": NOW,
        "superseded_by": None,
        "suppressed_at": None,
        "user_intent": None,
        "challenge_count": 0,
        "derived_from_sensitive": False,
        "sensitive_topic": None,
    }
    base.update(overrides)
    return GraphEdge(**base)


def _document(**overrides: object) -> GraphDocument:
    base: dict = {
        "revision": 1,
        "nodes": [_node()],
        "edges": [_edge()],
        "unprojected_count": 0,
        "truncated": False,
        "purged_at": None,
        "updated_at": NOW,
    }
    base.update(overrides)
    return GraphDocument(**base)


# ─────────── fact 증거 확장 (비파괴) ───────────


async def test_get_fact_records_exposes_key_created_at_and_triples() -> None:
    store = await get_profile_store()
    triple = {"node_id": "brand:소니", "predicate": "likes", "edge_key": "likes|brand:소니"}

    await store.add_fact("u1", "소니 선호", graph_triples=[triple])

    records = await store.get_fact_records("u1")

    assert len(records) == 1
    assert records[0].fact == "소니 선호"
    assert records[0].fact_key  # evidence_refs 가 참조할 키(§5.2)
    assert records[0].created_at  # 관측 정렬 키(REQ-PGRAPH-015)
    assert records[0].graph_triples == [triple]


async def test_get_facts_still_returns_plain_strings() -> None:
    """기존 시그니처를 깨지 않는다 — 호출부를 강제로 고치게 만들 이유가 없다."""
    store = await get_profile_store()

    await store.add_fact("u1", "소니 선호")

    assert await store.get_facts("u1") == ["소니 선호"]


async def test_add_fact_without_triples_records_empty_list() -> None:
    """트리플을 못 만든 fact 도 증거로는 남는다 — 문서에 안 실릴 뿐이다(REQ-PGRAPH-004)."""
    store = await get_profile_store()

    await store.add_fact("u1", "기억해 준 발화")

    records = await store.get_fact_records("u1")

    assert records[0].graph_triples == []


async def test_fact_records_are_sorted_by_created_at() -> None:
    """병합은 (observed_at, fact_key) 오름차순 처리다 — 저장소가 그 순서를 준다(REQ-PGRAPH-015)."""
    store = await get_profile_store()

    for i in range(3):
        await store.add_fact("u1", f"취향 {i}")

    records = await store.get_fact_records("u1")

    assert [r.fact for r in records] == ["취향 0", "취향 1", "취향 2"]
    assert [r.created_at for r in records] == sorted(r.created_at for r in records)


async def test_add_fact_dedup_stays_exact_string_match() -> None:
    """fact 수준 dedup 은 문자열 완전 일치를 유지한다 — 의미 병합은 전부 edge 수준(REQ-PGRAPH-017)."""
    store = await get_profile_store()

    await store.add_fact("u1", "소니 선호", graph_triples=[{"node_id": "brand:소니"}])
    await store.add_fact("u1", "소니 선호", graph_triples=[{"node_id": "brand:다른값"}])

    records = await store.get_fact_records("u1")

    assert len(records) == 1
    assert records[0].graph_triples == [{"node_id": "brand:소니"}]  # 재승격은 스킵(멱등)


async def test_add_fact_backfills_triples_onto_a_fact_that_had_none() -> None:
    """트리플 없이 저장된 fact 는 나중에 resolve 가 성공하면 **채워진다** (PR #410 리뷰).

    resolver 는 임베딩 백엔드 장애를 예외 전파 대신 **드롭**으로 처리한다(그래야 배치 전체가
    영구 RETRYABLE 이 되지 않는다). 그래서 장애 중에는 트리플 없는 fact 가 저장되는데, dedup 이
    `graph_triples` 를 안 보고 무조건 return 하면 백엔드가 복구되어 같은 취향이 다시 승격돼도
    새 트리플이 버려진다 — **일시적 장애가 영구 손실**이 되고 그 취향은 계속 unprojected 로만
    잡혀 그래프에 영영 안 실린다.

    중복 fact 는 여전히 안 쌓인다(항목 수는 1). 채우는 것은 값뿐이다.
    """
    store = await get_profile_store()

    await store.add_fact("u1", "소니 선호")  # 1차: resolve 실패 → 트리플 없음
    await store.add_fact("u1", "소니 선호", graph_triples=[{"node_id": "brand:소니"}])  # 2차: 성공

    records = await store.get_fact_records("u1")

    assert len(records) == 1  # 중복은 그대로 막는다
    assert records[0].graph_triples == [{"node_id": "brand:소니"}]


# ─────────── 그래프 문서 read/write ───────────


async def test_get_graph_returns_none_before_first_write() -> None:
    store = await get_profile_store()

    assert await store.get_graph("u1") is None


async def test_set_graph_round_trips_document() -> None:
    store = await get_profile_store()
    document = _document(revision=7, unprojected_count=3)

    await store.set_graph("u1", document)
    restored = await store.get_graph("u1")

    assert restored == document


async def test_set_graph_overwrites_previous_revision() -> None:
    """쓰기 측은 문서 전체를 재작성한다 — per-edge 병합이 아니다(REQ-PGRAPH-041)."""
    store = await get_profile_store()

    await store.set_graph("u1", _document(revision=1))
    await store.set_graph("u1", _document(revision=2, edges=[], nodes=[]))

    restored = await store.get_graph("u1")

    assert restored is not None
    assert restored.revision == 2
    assert restored.edges == []


async def test_graph_is_scoped_per_user() -> None:
    store = await get_profile_store()

    await store.set_graph("u1", _document())

    assert await store.get_graph("u2") is None


async def test_tombstone_survives_round_trip() -> None:
    """차단 표식이 저장·재적재에서 사라지면 삭제가 무력화된다(REQ-PGRAPH-022)."""
    store = await get_profile_store()
    document = _document(edges=[])
    document.tombstones.append(
        GraphTombstone(edge_id=make_edge_id("likes|brand:소니"), suppressed_at=NOW)
    )

    await store.set_graph("u1", document)
    restored = await store.get_graph("u1")

    assert restored is not None
    assert [t.edge_id for t in restored.tombstones] == [make_edge_id("likes|brand:소니")]
    assert restored.tombstones[0].suppressed_at == NOW


async def test_legacy_suppressed_edge_loses_its_label_on_read() -> None:
    """구 문서를 읽으면 라벨 원문이 떨어진다 — 백필 잡 없이 읽는 즉시 수렴한다.

    #499 이전에 저장된 문서에는 `status="suppressed"` edge 가 남아 있고, 그 `node_id` 는
    `"brand:소니"` 라 **사용자가 지웠다고 믿는 문장의 원문이 그대로 있다.** 저장소에서 올라오는
    이 지점이 그걸 떨굴 마지막 기회다.
    """
    store = await get_profile_store()
    # `active` 로 만든 뒤 직렬화 결과를 손으로 뒤집는다 — `GraphDocument` 를 거쳐 만들면
    # 검증 단계가 이미 흡수해 버려서 "구 표현"을 재현할 수 없다.
    legacy = _document(edges=[_edge(evidence_count=0, evidence_refs=[])])
    raw = legacy.model_dump(mode="json")
    raw["edges"][0]["status"] = "suppressed"
    raw["edges"][0]["suppressed_at"] = NOW
    raw.pop("tombstones", None)  # 필드 자체가 없던 구 문서다
    await store._store.aput(("graph", "u1"), "v1", raw)  # noqa: SLF001

    restored = await store.get_graph("u1")

    assert restored is not None
    assert restored.edges == []
    assert restored.nodes == []
    assert "소니" not in repr(restored.model_dump(mode="json"))
    assert [t.edge_id for t in restored.tombstones] == [make_edge_id("likes|brand:소니")]


async def test_get_graph_returns_none_on_corrupt_document() -> None:
    """스키마가 안 맞는 문서는 배치를 죽이지 않고 없는 것으로 본다 — 다음 배치가 다시 만든다."""
    store = await get_profile_store()
    await store._store.aput(("graph", "u1"), "v1", {"revision": "not-an-int"})  # noqa: SLF001

    assert await store.get_graph("u1") is None


# ─────────── 잠금 ───────────


async def test_graph_lock_serializes_concurrent_writers() -> None:
    """문서는 read-modify-write 라 잠금 없이는 나중 쓰기가 앞 쓰기를 덮는다."""
    store = await get_profile_store()
    await store.set_graph("u1", _document(revision=0))
    order: list[str] = []

    async def bump(tag: str, hold: float) -> None:
        async with store.graph_lock("u1"):
            order.append(f"{tag}:enter")
            current = await store.get_graph("u1")
            assert current is not None
            await asyncio.sleep(hold)
            await store.set_graph("u1", _document(revision=current.revision + 1))
            order.append(f"{tag}:exit")

    await asyncio.gather(bump("a", 0.02), bump("b", 0.0))

    # 임계 구역이 겹치지 않는다 — enter/exit 가 교차하지 않는다.
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    )
    final = await store.get_graph("u1")
    assert final is not None
    assert final.revision == 2  # 두 증가가 모두 살아남았다(lost update 없음)


async def test_graph_lock_does_not_block_summary_writes() -> None:
    """그래프 락과 요약 락(#323)은 키가 다르다 — 겹치면 consolidation 이 서로를 막는다.

    락을 중첩해 잡으면 advisory 풀(전역 max_size)에서 커넥션을 동시에 둘 점유하게 되므로,
    consolidate() 는 그래프 락을 놓은 뒤에 set_summary 를 불러야 한다.
    """
    store = await get_profile_store()

    async with store.graph_lock("u1"):
        await asyncio.wait_for(store.set_summary("u1", "요약", NOW), timeout=1.0)

    summary = await store.get_summary("u1")
    assert summary is not None
    assert summary.markdown == "요약"


async def test_reset_clears_graph_locks() -> None:
    """pytest-asyncio 는 테스트마다 새 루프를 쓴다 — 전역 Lock 이 루프를 넘나들면 hang 한다(#50).

    레지스트리가 WeakValueDictionary 라 사용 중이 아닌 락은 GC 가 알아서 회수한다. 여기서
    검증할 것은 **강한 참조가 살아 있어도** reset 이 비운다는 것이다 — 그래야 이전 루프에 묶인
    락이 다음 테스트로 새지 않는다.
    """
    from app.agents.profile import store as store_mod

    held = store_mod._graph_lock("u1")  # noqa: SLF001 - 강한 참조를 쥐어 GC 회수를 막는다
    assert "u1" in store_mod._graph_locks  # noqa: SLF001

    store_mod.reset_profile_store()

    assert "u1" not in store_mod._graph_locks  # noqa: SLF001
    assert held is not store_mod._graph_lock("u1")  # noqa: SLF001 - 새 락으로 교체됐다


@pytest.mark.parametrize("user_id", ["", None])
async def test_graph_accessors_ignore_blank_user(user_id: object) -> None:
    store = await get_profile_store()

    assert await store.get_graph(user_id) is None  # type: ignore[arg-type]
