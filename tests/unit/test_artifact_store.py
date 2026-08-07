from app.pipelines.artifact_store import (
    FAILURE_STREAK_KIND_ITEM,
    FAILURE_STREAK_KIND_PAGE,
    CatalogArtifact,
    CatalogArtifactStore,
    FailureStreakTable,
)


def test_get_many_returns_dict_of_found_only():
    """[#101] get_many 는 요청 id 를 한 번에 조회해 productId→artifact dict 로 준다.

    EmbeddingRerankBackend 의 후보별 get() 순차 호출(N+1)을 1회 batch 로 대체하는 계약.
    없는 id 는 dict 에서 생략(재정렬 시 −1.0 규칙으로 맨 뒤 처리).
    """
    store = CatalogArtifactStore()
    store.upsert(CatalogArtifact(product_id=1, search_doc="a", embedding=[1.0]))
    store.upsert(CatalogArtifact(product_id=3, search_doc="c", embedding=[3.0]))

    got = store.get_many([1, 2, 3])  # 2 는 없음
    assert set(got.keys()) == {1, 3}
    assert got[1].search_doc == "a"
    assert got[3].search_doc == "c"
    assert store.get_many([]) == {}  # 빈 입력 → 빈 dict(쿼리 스킵)


def test_artifact_provenance_defaults_none():
    a = CatalogArtifact(product_id=1, search_doc="d", embedding=[0.0])
    assert a.embed_model is None and a.embed_dim is None
    assert a.embed_task is None and a.normalized is None


def test_artifact_carries_provenance():
    a = CatalogArtifact(
        product_id=1,
        search_doc="d",
        embedding=[0.0],
        embed_model="gemini-embedding-001",
        embed_dim=1536,
        embed_task="RETRIEVAL_DOCUMENT",
        normalized=True,
    )
    assert a.embed_model == "gemini-embedding-001"
    assert a.embed_dim == 1536 and a.embed_task == "RETRIEVAL_DOCUMENT"
    assert a.normalized is True


def test_top_k_by_vector_include_contract():
    store = CatalogArtifactStore()
    for product_id, embedding in (
        (1, [1.0, 0.0]),
        (2, [0.8, 0.2]),
        (3, [0.0, 1.0]),
    ):
        store.upsert(
            CatalogArtifact(product_id=product_id, search_doc=str(product_id), embedding=embedding)
        )

    assert store.top_k_by_vector([1.0, 0.0], k=3, include={2, 3}) == [2, 3]
    assert store.top_k_by_vector([1.0, 0.0], k=3, include=set()) == []
    assert store.top_k_by_vector([1.0, 0.0], k=3, include=None) == [1, 2, 3]
    assert store.top_k_by_vector([1.0, 0.0], k=3, include={1, 2}, exclude={1}) == [2]


# ── 이슈 #416: FailureStreakTable — cross-cycle 연속 실패 스트릭 TTL 리셋 ──


def test_failure_streak_table_bumps_and_returns_running_count():
    table = FailureStreakTable(clock=lambda: 0.0)
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 1
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 2
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 3


def test_failure_streak_table_resets_after_ttl_elapses():
    """마지막 갱신이 ttl_s 보다 오래되면 다음 bump 는 1 로 리셋된다 — 무관한 과거 실패가
    "연속"으로 합산되지 않는다(#416)."""
    now = [1000.0]
    table = FailureStreakTable(clock=lambda: now[0])

    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 1
    now[0] += 10
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 2
    now[0] += 61  # ttl_s(60) 초과
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 1


def test_failure_streak_table_kinds_and_keys_are_independent():
    table = FailureStreakTable(clock=lambda: 0.0)
    table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60)
    table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60)
    assert table.bump(FAILURE_STREAK_KIND_PAGE, "1", ttl_s=60) == 1  # 다른 kind, 같은 key
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "2", ttl_s=60) == 1  # 같은 kind, 다른 key


def test_failure_streak_table_clear_removes_entry():
    table = FailureStreakTable(clock=lambda: 0.0)
    table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60)
    table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60)
    table.clear(FAILURE_STREAK_KIND_ITEM, "1")
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 1  # clear 후 다시 1부터
    table.clear(FAILURE_STREAK_KIND_ITEM, "missing")  # 없는 키 clear 는 조용히 무시


def test_failure_streak_table_purge_stale_removes_only_expired():
    now = [0.0]
    table = FailureStreakTable(clock=lambda: now[0])
    table.bump(FAILURE_STREAK_KIND_ITEM, "old", ttl_s=60)
    now[0] = 30.0
    table.bump(FAILURE_STREAK_KIND_ITEM, "fresh", ttl_s=60)
    now[0] = 1000.0

    purged = table.purge_stale(ttl_s=60)

    assert purged == 2  # "old"(1000s 경과)·"fresh"(970s 경과) 둘 다 60s 를 넘음
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "old", ttl_s=60) == 1


def test_failure_streak_table_max_entries_defensively_clears_per_kind_only():
    """[T4, PR 리뷰 라운드 2] 방어적 메모리 상한은 kind 별로 독립 적용된다 — 한 kind 가
    상한에 닿아 방어적으로 비워져도 다른 kind 스트릭은 보존된다.

    수정 전에는 item·page 를 한 dict 에 합쳐 상한(10,000)을 "합산"으로 적용해 구 동작
    (item 10,000 · page 10,000, 총량 20,000)의 절반으로 줄어 있었고, 상한 초과 시
    ``clear()`` 가 item·page 를 한꺼번에 날렸다 — 대량 실패 상황(카탈로그 전량 동시 실패
    등)에서 스트릭이 통째로 리셋되면 #416 이 고치려던 "상한에 영영 도달하지 못한다"가
    그대로 재현됐다."""
    from app.pipelines import artifact_store as _artifact_store

    table = FailureStreakTable(clock=lambda: 0.0)
    table.bump(FAILURE_STREAK_KIND_PAGE, "cursor-x", ttl_s=60)  # 보존 확인 대상

    for i in range(_artifact_store._FAILURE_STREAK_MAX_ENTRIES):
        table.bump(FAILURE_STREAK_KIND_ITEM, str(i), ttl_s=60)
    assert (
        len(table._entries[FAILURE_STREAK_KIND_ITEM]) == _artifact_store._FAILURE_STREAK_MAX_ENTRIES
    )
    # item kind 가 상한에 닿았지만 page kind 엔트리는 그대로 살아 있다(이어서 2가 된다).
    assert table.bump(FAILURE_STREAK_KIND_PAGE, "cursor-x", ttl_s=60) == 2

    table.bump(FAILURE_STREAK_KIND_ITEM, "overflow", ttl_s=60)
    # 상한 도달 → item kind 만 방어적으로 비운 뒤 새 엔트리 1개만 남는다.
    assert len(table._entries[FAILURE_STREAK_KIND_ITEM]) == 1
    assert table.bump(FAILURE_STREAK_KIND_ITEM, "0", ttl_s=60) == 1  # item 은 비워져 1부터
    assert table.bump(FAILURE_STREAK_KIND_PAGE, "cursor-x", ttl_s=60) == 3  # page 는 안 지워짐


# ── 이슈 #416: CatalogArtifactStore(인메모리) 실패 스트릭 위임 ──


def test_catalog_artifact_store_delegates_failure_streak_to_table():
    store = CatalogArtifactStore()
    assert store.bump_failure_streak(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 1
    assert store.bump_failure_streak(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 2
    store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, "1")
    assert store.bump_failure_streak(FAILURE_STREAK_KIND_ITEM, "1", ttl_s=60) == 1


def test_catalog_artifact_store_purge_stale_failure_streaks():
    store = CatalogArtifactStore()
    store._failure_streaks = FailureStreakTable(clock=lambda: 0.0)
    store.bump_failure_streak(FAILURE_STREAK_KIND_PAGE, "c1", ttl_s=60)
    store._failure_streaks._clock = lambda: 1000.0
    assert store.purge_stale_failure_streaks(ttl_s=60) == 1
