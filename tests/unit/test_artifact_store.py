from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore


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
