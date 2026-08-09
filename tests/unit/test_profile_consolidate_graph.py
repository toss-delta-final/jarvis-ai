"""consolidation 그래프 입력 전환 — **이 이슈의 존재 이유** (REQ-PGRAPH-022/023 [HARD]).

억제가 실효하려면 consolidation 이 그래프를 입력으로 읽어야 한다. fact 목록을 그대로 읽는 현행
경로를 유지하면 다음 배치가 삭제된 취향을 요약에 다시 써넣어 **삭제 기능이 겉모습만 남는다.**

여기 테스트는 요약 LLM 에 **실제로 전달된 user 프롬프트를 캡처해** 단언한다. fake 가 돌려주는
고정 요약 문자열을 보고 "요약에 없다"를 재면 아무것도 검증하지 못한다 — fake 출력은 입력과
무관하기 때문이다(docs/lessons.md fake 검증 항목).
"""

import pytest

from app.agents.profile.builder import ConsolidationResult, _summary_input, consolidate
from app.agents.profile.graph_merge import empty_document
from app.agents.profile.graph_models import GraphDocument, GraphEdge, GraphNode, make_edge_id
from app.agents.profile.store import FactRecord, get_profile_store
from app.core.config import get_settings
from app.core.llm import LLMError

NOW = "2026-08-06T00:00:00+00:00"


class _CapturingLLM:
    """요약 LLM 에 들어간 입력을 붙잡는다 — 억제 실효는 **입력**에서 판정해야 한다."""

    def __init__(self, summary: str = "# 취향 요약") -> None:
        self.summary = summary
        self.summary_inputs: list[str] = []

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        if "델타 추출기" in system:
            return '{"deltas": []}'
        self.summary_inputs.append(user)
        return self.summary

    async def stream(self, *, system, user, tier, max_tokens=1024):
        yield "x"


class _FailingLLM(_CapturingLLM):
    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        raise LLMError("upstream down")


def _triple(node_id: str = "brand:소니", predicate: str = "likes", *, label: str = "소니") -> dict:
    edge_key = f"{predicate}|{node_id}"
    return {
        "node": {
            "node_id": node_id,
            "type": "brand",
            "label": label,
            "verified": False,
            "resolution": None,
        },
        "predicate": predicate,
        "edge_key": edge_key,
        "edge_id": make_edge_id(edge_key),
        "salience": 0.9,
        "source": "conversation",
    }


def _edge(status: str = "suppressed", **overrides: object) -> GraphEdge:
    edge_key = "likes|brand:소니"
    base: dict = {
        "edge_key": edge_key,
        "edge_id": make_edge_id(edge_key),
        "node_id": "brand:소니",
        "predicate": "likes",
        "status": status,
        "promoted": True,
        "origin": "machine",
        "source_latest": "conversation",
        "confidence": 0.9,
        "evidence_count": 1,
        "evidence_by_source": {"conversation": 1},
        "evidence_refs": ["seed"],
        "first_observed_at": NOW,
        "last_observed_at": NOW,
        "decay_evaluated_at": NOW,
        "valid_from": NOW,
        "superseded_by": None,
        "suppressed_at": NOW if status == "suppressed" else None,
        "user_intent": None,
        "challenge_count": 0,
        "derived_from_sensitive": False,
        "sensitive_topic": None,
    }
    base.update(overrides)
    return GraphEdge(**base)


def _document(edges: list[GraphEdge]) -> GraphDocument:
    return GraphDocument(
        revision=1,
        nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
        edges=edges,
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=NOW,
    )


async def _run(llm, user: str = "7") -> tuple[ConsolidationResult, object]:
    store = await get_profile_store()
    result = await consolidate(user, llm=llm, settings=get_settings())
    return result, store


# ─────────── [HARD] 억제 실효 ───────────


async def _seed_suppressed_plus_live(status: str = "suppressed", **edge_overrides: object) -> None:
    """억제 대상 하나 + 살아 있는 취향 하나.

    억제분만 두면 요약 입력이 통째로 비어 LLM 이 안 불리고, 그러면 "제외됐다"와 "아무 일도
    안 일어났다"를 구분할 수 없다. 살아 있는 취향을 함께 둬야 제외가 **선택적으로** 동작하는지
    검증된다.
    """
    store = await get_profile_store()
    await store.add_fact("7", "소니 브랜드를 선호한다", graph_triples=[_triple()])
    await store.add_fact(
        "7", "애플 브랜드를 선호한다", graph_triples=[_triple("brand:애플", label="애플")]
    )
    records = await store.get_fact_records("7")
    sony_key = next(r.fact_key for r in records if "소니" in r.fact)
    await store.set_graph(
        "7", _document([_edge(status, evidence_refs=[sony_key], **edge_overrides)])
    )


async def test_suppressed_preference_does_not_return_to_summary() -> None:
    """**이 테스트가 이 이슈의 존재 이유다.**

    사용자가 지운 취향을 재관측해도 요약 입력에 다시 들어가면 안 된다(REQ-PGRAPH-023).
    """
    await _seed_suppressed_plus_live()
    store = await get_profile_store()
    llm = _CapturingLLM()

    result = await consolidate("7", llm=llm, settings=get_settings())

    assert result is ConsolidationResult.UPDATED
    assert llm.summary_inputs, "요약 LLM 이 호출되지 않아 검증 자체가 성립하지 않는다"
    prompt = llm.summary_inputs[0]
    assert "소니" not in prompt
    assert "애플" in prompt  # 제외가 선택적이다 — 전부 지운 게 아니다
    # 새 근거가 지운 취향을 되살리지 않는다. #499 로 삭제가 즉시 물리 삭제가 되면서, 확인할
    # 것이 "상태가 suppressed 인가"에서 **"문서에 아예 없고 라벨 원문도 남지 않았는가"**로
    # 바뀌었다 — 차단은 tombstone 이 맡는다.
    document = await store.get_graph("7")
    assert document is not None
    assert all(edge.node_id != "brand:소니" for edge in document.edges)
    assert all(node.node_id != "brand:소니" for node in document.nodes)
    assert "소니" not in repr(document.model_dump(mode="json"))
    assert make_edge_id("likes|brand:소니") in {t.edge_id for t in document.tombstones}


async def test_superseded_edge_is_excluded_from_summary_input() -> None:
    """충돌에서 진 취향의 근거 fact 는 요약 입력에서 빠진다.

    승자(`avoids`)를 **문서와 fact 양쪽에** 둔다 — 승자가 없으면 병합이 패자를 되살리므로
    (상대 없는 `superseded` 는 유지할 근거가 없다, `_revive_orphan_superseded`) 이 시나리오
    자체가 성립하지 않는다. 승자 fact 원문에 "소니"를 넣지 않아 단언이 패자만 겨냥한다.
    """
    store = await get_profile_store()
    await store.add_fact("7", "소니 브랜드를 선호한다", graph_triples=[_triple()])
    await store.add_fact(
        "7", "애플 브랜드를 선호한다", graph_triples=[_triple("brand:애플", label="애플")]
    )
    await store.add_fact(
        "7", "그 브랜드는 이제 별로다", graph_triples=[_triple("brand:소니", "avoids")]
    )
    records = await store.get_fact_records("7")
    sony_key = next(r.fact_key for r in records if "소니 브랜드" in r.fact)
    winner_key = "avoids|brand:소니"
    await store.set_graph(
        "7",
        _document(
            [
                _edge(
                    "superseded", evidence_refs=[sony_key], superseded_by=make_edge_id(winner_key)
                ),
                _edge(
                    "active",
                    edge_key=winner_key,
                    edge_id=make_edge_id(winner_key),
                    predicate="avoids",
                    evidence_refs=[],
                ),
            ]
        ),
    )
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=get_settings())

    assert "소니" not in llm.summary_inputs[0]
    assert "애플" in llm.summary_inputs[0]


async def test_fact_backing_both_live_and_suppressed_edges_is_excluded() -> None:
    """한 발화가 두 취향을 담고 그중 하나만 지워졌다면 그 fact 는 **통째로** 뺀다.

    fact 원문에는 지운 취향이 그대로 적혀 있다. 살아 있는 쪽을 살리자고 통과시키면 지운 내용이
    요약기 입력으로 들어가 그대로 되살아난다 — 삭제가 이긴다.

    이 케이스가 없으면 `excluded` 규칙이 죽은 코드처럼 보인다(`live` 허용목록만으로 단일 취향
    fact 는 이미 걸러지기 때문). 변이 검증에서 실제로 드러난 구멍이다.
    """
    store = await get_profile_store()
    await store.add_fact(
        "7",
        "소니와 애플을 둘 다 선호한다",
        graph_triples=[_triple(), _triple("brand:애플", label="애플")],
    )
    await store.add_fact(
        "7", "삼성도 좋아한다", graph_triples=[_triple("brand:삼성", label="삼성")]
    )
    records = await store.get_fact_records("7")
    both_key = next(r.fact_key for r in records if "둘 다" in r.fact)
    await store.set_graph("7", _document([_edge("suppressed", evidence_refs=[both_key])]))
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=get_settings())

    prompt = llm.summary_inputs[0]
    assert "소니와 애플을 둘 다 선호한다" not in prompt
    assert "삼성도 좋아한다" in prompt  # 무관한 취향은 남는다


async def test_evidence_refs_cap_does_not_drop_live_facts_from_summary() -> None:
    """요약 입력 판정은 **저장 상한과 무관**해야 한다 (PR #410 리뷰).

    `graph_evidence_refs_max`(기본 20)는 edge 당 보관하는 근거 **참조 개수** 상한이고 목적은
    저장 폭주 방지다. 그 값이 "요약에 넣을 fact" 판정까지 결정하면, 같은 취향을 상한보다 많이
    언급한 순간 **지우지도 않은 활성 취향의 오래된 근거가 조용히 요약에서 빠진다** —
    이 PR 의 목표(삭제 안 한 취향이 요약에서 사라지지 않게)와 정반대다.

    fact 상한(`profile_max_facts`, 기본 200)이 참조 상한(20)보다 훨씬 커서 실제로 도달한다.
    """
    store = await get_profile_store()
    settings = get_settings()
    total = settings.graph_evidence_refs_max + 5
    for i in range(total):
        await store.add_fact("7", f"소니 좋다고 {i}번째로 말함", graph_triples=[_triple()])
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=settings)

    document = await store.get_graph("7")
    assert document is not None
    edge = document.edges[0]
    assert edge.status == "active"
    assert edge.evidence_count == total  # 카운트는 전부 센다
    assert len(edge.evidence_refs) == settings.graph_evidence_refs_max  # 참조만 상한

    prompt = llm.summary_inputs[0]
    for i in range(total):
        assert f"소니 좋다고 {i}번째로 말함" in prompt, f"{i}번째 근거가 상한 때문에 누락됐다"


async def test_active_preference_reaches_summary_input() -> None:
    """제외 규칙이 너무 넓으면 정상 취향까지 사라진다 — 반대 방향도 고정한다."""
    store = await get_profile_store()
    await store.add_fact("7", "소니 브랜드를 선호한다", graph_triples=[_triple()])
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=get_settings())

    assert "소니" in llm.summary_inputs[0]


# ─────────── 잔여 fact 보존 ───────────


async def test_facts_without_triples_still_feed_the_summary() -> None:
    """ "기억해" hot-path·resolver 드롭분이 개인화에서 통째로 사라지면 안 된다.

    트리플 없는 fact 는 그래프에 안 실리지만(unprojected_count) 요약 입력에는 남는다.
    """
    store = await get_profile_store()
    await store.add_fact("7", "매운 음식을 싫어한다")  # 트리플 없음
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=get_settings())

    assert "매운 음식을 싫어한다" in llm.summary_inputs[0]


async def test_fact_backing_a_suppressed_edge_is_excluded_too() -> None:
    """억제된 edge 의 근거 fact 원문이 잔여 fact 로 새어 들어가면 억제가 무의미해진다."""
    await _seed_suppressed_plus_live()
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=get_settings())

    assert "소니 브랜드를 선호한다" not in llm.summary_inputs[0]


# ─────────── 요약 보존 (파괴 금지) ───────────


async def test_empty_summary_input_preserves_existing_summary() -> None:
    """요약 입력이 비어도 기존 요약을 덮지 않는다.

    빈 문자열로 덮으면 마이페이지·rerank 는 취향을 잃는데 홈 랭킹은 캐리오버된 옛 벡터로 계속
    개인화한다 — REQ-PGRAPH-022 의 정반대 상태다.
    """
    store = await get_profile_store()
    await store.set_summary("7", "# 기존 요약", NOW)
    await store.add_fact("7", "소니 브랜드를 선호한다", graph_triples=[_triple()])
    await store.set_graph("7", _document([_edge("suppressed", evidence_refs=[])]))
    records = await store.get_fact_records("7")
    await store.set_graph(
        "7", _document([_edge("suppressed", evidence_refs=[records[0].fact_key])])
    )
    llm = _CapturingLLM()

    result = await consolidate("7", llm=llm, settings=get_settings())

    assert result is ConsolidationResult.NO_WORK
    assert llm.summary_inputs == []  # LLM 을 부르지 않는다
    summary = await store.get_summary("7")
    assert summary is not None
    assert summary.markdown == "# 기존 요약"


async def test_no_facts_is_still_no_work() -> None:
    result, _ = await _run(_CapturingLLM())

    assert result is ConsolidationResult.NO_WORK


# ─────────── 계약 유지 (finalizer 분기) ───────────


async def test_llm_missing_is_retryable_failure() -> None:
    store = await get_profile_store()
    await store.add_fact("7", "소니 선호", graph_triples=[_triple()])

    result = await consolidate("7", llm=None, settings=get_settings())

    assert result is ConsolidationResult.FAILED


async def test_llm_error_is_failure_not_crash() -> None:
    store = await get_profile_store()
    await store.add_fact("7", "소니 선호", graph_triples=[_triple()])

    result = await consolidate("7", llm=_FailingLLM(), settings=get_settings())

    assert result is ConsolidationResult.FAILED


async def test_graph_is_written_even_when_summary_llm_fails() -> None:
    """요약이 실패해도 그래프는 남는다 — 다음 배치가 요약만 다시 만들면 된다."""
    store = await get_profile_store()
    await store.add_fact("7", "소니 선호", graph_triples=[_triple()])

    await consolidate("7", llm=_FailingLLM(), settings=get_settings())

    document = await store.get_graph("7")
    assert document is not None
    assert len(document.edges) == 1


# ─────────── 잠금 규약 ───────────


async def test_summary_is_written_outside_the_graph_lock(monkeypatch) -> None:
    """LLM 왕복을 락 안에서 기다리면 advisory 풀이 말라 구매자 턴까지 3초 타임아웃으로 죽는다.

    `set_summary` 는 스스로 요약 락을 잡으므로(#323) 그래프 락과 중첩하면 커넥션을 동시에 둘
    점유한다. 여기서는 "요약을 쓸 때 그래프 락이 잡혀 있지 않다"를 직접 관측한다.
    """
    from app.agents.profile import store as store_mod

    store = await get_profile_store()
    await store.add_fact("7", "소니 선호", graph_triples=[_triple()])
    held: list[bool] = []
    original = store_mod.ProfileStore.set_summary

    async def _spy(self, user_id, markdown, generated_at, **kwargs):
        held.append(store_mod._graph_lock(user_id).locked())  # noqa: SLF001
        return await original(self, user_id, markdown, generated_at, **kwargs)

    monkeypatch.setattr(store_mod.ProfileStore, "set_summary", _spy)

    await consolidate("7", llm=_CapturingLLM(), settings=get_settings())

    assert held == [False]


async def test_graph_document_is_created_from_facts() -> None:
    store = await get_profile_store()
    await store.add_fact("7", "소니 선호", graph_triples=[_triple()])
    assert await store.get_graph("7") is None

    await consolidate("7", llm=_CapturingLLM(), settings=get_settings())

    document = await store.get_graph("7")
    assert document is not None
    assert document.revision == empty_document(NOW).revision + 1
    assert [e.edge_key for e in document.edges] == ["likes|brand:소니"]


@pytest.mark.parametrize("marker", ["소니", "소니 브랜드를 선호한다"])
async def test_summary_input_is_derived_from_graph_not_raw_fact_list(marker: str) -> None:
    """그래프를 거치지 않고 fact 를 그대로 넘기면 억제가 실효하지 않는다 — 경로를 고정한다."""
    await _seed_suppressed_plus_live()
    llm = _CapturingLLM()

    await consolidate("7", llm=llm, settings=get_settings())

    assert marker not in llm.summary_inputs[0]


# ─────────── 절단 순서의 근거 (graph_merge._truncate) ───────────
#
# `_truncate` 가 `active` 를 `superseded` 보다 **먼저** 버리는 이유가 여기 있다. 순서 자체는
# test_profile_graph_merge.py 가 고정하고, 여기서는 **그 순서를 정당화하는 비대칭**을 잰다 —
# 근거가 테스트로 안 잠기면 다음 사람이 "죽은 edge 를 살아 있는 edge 보다 먼저 지킨다"를
# 버그로 읽고 뒤집는다(PR #410 리뷰에서 실제로 그렇게 읽혔다).


def _record(key: str, text: str, triples: list[dict]) -> FactRecord:
    return FactRecord(fact_key=key, fact=text, created_at=NOW, graph_triples=triples)


def test_truncated_superseded_edge_lets_the_losing_preference_back_into_summary() -> None:
    """`superseded` 가 절단으로 사라지면 진 취향이 요약 입력에 되살아난다 — 그래서 먼저 지킨다.

    문서에 없는 `edge_key` 는 `active` 로 간주된다(저장 한계가 개인화 내용을 줄이지 않게 한 규칙).
    그 규칙이 `superseded` 에는 반대로 작용한다 — 표식이 사라지면 원문이 통과한다.
    """
    facts = [_record("f1", "소니를 좋아한다", [_triple()])]
    with_marker = _document([_edge("superseded", superseded_by="e_x", evidence_refs=["f1"])])
    without_marker = _document([])  # 절단으로 표식이 사라진 상태

    assert _summary_input(with_marker, facts) == []
    assert _summary_input(without_marker, facts) == ["소니를 좋아한다"]  # 진 취향 부활


def test_truncated_active_edge_still_keeps_its_fact_in_summary() -> None:
    """`active` 가 절단돼도 그 fact 는 요약 입력에 남는다 — 잃는 것은 그래프 표현뿐이다.

    두 손실의 크기가 다르다는 것이 절단 순서의 근거다.
    """
    facts = [_record("f1", "소니를 좋아한다", [_triple()])]

    assert _summary_input(_document([_edge("active")]), facts) == ["소니를 좋아한다"]
    assert _summary_input(_document([]), facts) == ["소니를 좋아한다"]  # 절단돼도 동일
