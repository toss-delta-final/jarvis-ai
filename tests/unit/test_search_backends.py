"""임베딩 검색 백엔드 (이슈 #7, §4.8 결정 2026-07-20) — 방식1/방식2 + 골든셋 비교.

embed·store 주입형 fake(어휘 기반 결정적 임베딩). #32에서 방식1·C-17 기각 후 오프라인 전용
미채택 신호(에러)를 검증한다.
"""

from __future__ import annotations

import pytest

from app.pipelines import embedding as _embedding
from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
from app.pipelines.compare import GoldenCase, compare_backends, recall_at_k
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services import search_service, spring_client
from app.services.search_service import (
    EmbeddingRerankBackend,
    VectorSearchBackend,
    _cosine,
    cosine_similarity,
    vector_rank,
)

_VOCAB = ["여행", "방수", "이어폰", "무선", "린넨", "셔츠"]


def _embed(texts):
    return [[1.0 if w in t else 0.0 for w in _VOCAB] for t in texts]


def _seed_store():
    store = CatalogArtifactStore()
    store.upsert(
        CatalogArtifact(
            product_id=1,
            search_doc="여행 방수 파우치",
            embedding=_embed(["여행 방수"])[0],
        )
    )
    store.upsert(
        CatalogArtifact(
            product_id=2,
            search_doc="무선 이어폰",
            embedding=_embed(["무선 이어폰"])[0],
        )
    )
    store.upsert(
        CatalogArtifact(
            product_id=3,
            search_doc="린넨 셔츠",
            embedding=_embed(["린넨 셔츠"])[0],
        )
    )
    return store


def test_make_default_backend_from_config(monkeypatch):
    """[#101] hot path 기본 백엔드는 config search_backend 로 결정된다(전역 토글).

    embedding_rerank 는 pgvector store 를 쓰므로 get_catalog_store 를 인메모리로 우회해 pg 연결을
    막고 클래스만 확인한다.
    """
    from app.core.config import Settings
    from app.services.search_service import (
        EmbeddingRerankBackend,
        SpringSearchBackend,
        _make_default_backend,
    )

    monkeypatch.setattr(search_service, "get_catalog_store", CatalogArtifactStore)

    monkeypatch.setattr(
        search_service, "get_settings", lambda: Settings(_env_file=None, search_backend="spring")
    )
    assert isinstance(_make_default_backend(), SpringSearchBackend)

    monkeypatch.setattr(
        search_service,
        "get_settings",
        lambda: Settings(_env_file=None, search_backend="embedding_rerank"),
    )
    assert isinstance(_make_default_backend(), EmbeddingRerankBackend)


def test_cosine_basic():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([], [1.0]) == -1.0


def test_vector_rank_orders_by_similarity():
    store = _seed_store()
    ranked = vector_rank(_embed(["여행 방수"])[0], store, k=3)
    assert ranked[0] == 1


async def test_embedding_rerank_backend_reorders(monkeypatch):
    store = _seed_store()

    async def fake_search(filters):
        return ProductSearchResult(
            products=[
                SpringProduct(product_id=3, name="셔츠", price=10),
                SpringProduct(product_id=2, name="이어폰", price=20),
                SpringProduct(product_id=1, name="파우치", price=30),
            ],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=_embed)
    result = await backend.search(ProductSearchFilters(keyword="여행 방수", limit=10))
    expected = sorted(
        result.products,
        key=lambda product: cosine_similarity(
            _embed(["여행 방수"])[0], store.get(product.product_id).embedding
        ),
        reverse=True,
    )
    assert [p.product_id for p in result.products] == [p.product_id for p in expected]


async def test_embedding_rerank_embeds_semantic_query_not_keyword(monkeypatch):
    """[#101] 백엔드는 filters.semantic_query 를 임베딩한다(상품명 LIKE keyword 아님).

    keyword 가 없어도 semantic_query 가 있으면 재정렬을 수행한다(의미검색과 LIKE 분리).
    """
    store = _seed_store()
    embedded: list[str] = []

    def spy_embed(texts):
        embedded.append(texts[0])
        return _embed(texts)

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=spy_embed)
    result = await backend.search(
        ProductSearchFilters(keyword=None, semantic_query="여행 방수", limit=10)
    )
    assert embedded == ["여행 방수"]  # keyword 가 아니라 semantic_query 를 임베딩
    assert [p.product_id for p in result.products][0] == 1  # keyword 없어도 재정렬됨


async def test_embedding_rerank_receives_product_anchor_with_structured_filters(monkeypatch):
    """[#603] 임베딩 경계는 구조화 필터와 함께 보정된 상품 앵커를 사용한다."""
    store = _seed_store()
    embedded: list[str] = []

    def spy_embed(texts):
        embedded.append(texts[0])
        return _embed(texts)

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=spy_embed)
    await backend.search(
        ProductSearchFilters(semantic_query="바지", price_max=30000, color="파란색", limit=10)
    )

    assert embedded == ["바지"]


async def test_embedding_rerank_degrades_to_spring_order_on_embed_failure(monkeypatch):
    """[#101 #7] 임베딩/pgvector 실패 시 추천 전체를 죽이지 않고 Spring 순서로 degrade한다.

    Spring I-1 자체 실패만 SEARCH_FAILED — 재정렬 단계(embed/store) 실패는 Spring 순서를 보존한다.
    """
    store = _seed_store()

    def boom_embed(texts):
        raise RuntimeError("google embedding down")

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=boom_embed)
    result = await backend.search(ProductSearchFilters(semantic_query="여행 방수", limit=10))
    assert [p.product_id for p in result.products] == [3, 2, 1]  # Spring 순서 그대로(재정렬 skip)
    assert result.total_count == 3


async def test_embedding_rerank_degrades_to_spring_order_on_total_budget_exceeded(monkeypatch):
    """[#391] embed_texts 총 시간 예산 초과(EmbeddingError)도 동일하게 Spring 순서로 degrade한다."""
    store = _seed_store()

    def boom_embed(texts):
        raise _embedding.EmbeddingError(
            "embed_texts: 총 시간 예산 초과 — 입력 250건/3청크 중 1청크 완료, "
            "경과 4.10s + 요청당 3.00s > 예산 3.00s (embedding_total_timeout_s)"
        )

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=boom_embed)
    result = await backend.search(ProductSearchFilters(semantic_query="여행 방수", limit=10))
    assert [p.product_id for p in result.products] == [3, 2, 1]  # Spring 순서 그대로(재정렬 skip)
    assert result.total_count == 3


async def test_embedding_rerank_backend_uses_single_vector_query(monkeypatch):
    """[#254] 재정렬은 top_k_by_vector 1회로 후보 집합만 순위화한다(N+1 아님)."""
    store = _seed_store()
    calls = {"get": 0, "top_k_by_vector": []}
    orig_top_k_by_vector = store.top_k_by_vector

    def spy_get(pid):
        calls["get"] += 1
        return CatalogArtifactStore.get(store, pid)

    def spy_top_k_by_vector(query_vec, *, k, exclude=None, include=None):
        calls["top_k_by_vector"].append(
            {"query_vec": query_vec, "k": k, "exclude": exclude, "include": include}
        )
        return orig_top_k_by_vector(query_vec, k=k, exclude=exclude, include=include)

    monkeypatch.setattr(store, "get", spy_get)
    monkeypatch.setattr(store, "top_k_by_vector", spy_top_k_by_vector)

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=_embed)
    await backend.search(ProductSearchFilters(keyword="여행 방수", limit=10))
    assert calls["top_k_by_vector"] == [
        {
            "query_vec": _embed(["여행 방수"])[0],
            "k": 3,
            "exclude": None,
            "include": {1, 2, 3},
        }
    ]
    assert calls["get"] == 0


async def test_embedding_rerank_preserves_missing_empty_and_duplicate_candidates(monkeypatch):
    """[#254] DB 미존재·빈 임베딩·중복 후보도 유실 없이 Spring 상대순서로 꼬리에 둔다."""
    store = CatalogArtifactStore()
    store.upsert(CatalogArtifact(product_id=1, search_doc="best", embedding=[1.0, 0.0]))
    store.upsert(CatalogArtifact(product_id=2, search_doc="empty", embedding=[]))
    spring_order = [
        SpringProduct(product_id=9, name="missing-a", price=10),
        SpringProduct(product_id=1, name="ranked-a", price=10),
        SpringProduct(product_id=2, name="empty", price=10),
        SpringProduct(product_id=1, name="ranked-b", price=10),
        SpringProduct(product_id=8, name="missing-b", price=10),
    ]

    async def fake_search(_filters):
        return ProductSearchResult(products=list(spring_order), total_count=len(spring_order))

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=lambda _texts: [[1.0, 0.0]])
    result = await backend.search(ProductSearchFilters(semantic_query="best"))

    assert len(result.products) == len(spring_order)
    assert sorted(p.product_id for p in result.products) == sorted(
        p.product_id for p in spring_order
    )
    assert [p.name for p in result.products] == [
        "ranked-a",
        "ranked-b",
        "missing-a",
        "empty",
        "missing-b",
    ]


async def test_embedding_rerank_vector_k_max_leaves_overflow_in_spring_order(monkeypatch):
    """[#254] DB 순위 상한 밖 후보는 자르지 않고 원래 Spring 순서로 꼬리에 보존한다."""
    from types import SimpleNamespace

    store = CatalogArtifactStore()
    embeddings = {
        1: [1.0, 0.0],
        2: [0.8, 0.2],
        3: [0.2, 0.8],
        4: [0.0, 1.0],
    }
    for product_id, embedding in embeddings.items():
        store.upsert(
            CatalogArtifact(product_id=product_id, search_doc=str(product_id), embedding=embedding)
        )
    spring_order = [
        SpringProduct(product_id=4, name="p4", price=10),
        SpringProduct(product_id=3, name="p3", price=10),
        SpringProduct(product_id=2, name="p2", price=10),
        SpringProduct(product_id=1, name="p1", price=10),
    ]

    async def fake_search(_filters):
        return ProductSearchResult(products=list(spring_order), total_count=4)

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    monkeypatch.setattr(
        search_service,
        "get_settings",
        lambda: SimpleNamespace(embedding_rerank_vector_k_max=2),
    )
    backend = EmbeddingRerankBackend(store=store, embed=lambda _texts: [[1.0, 0.0]])
    result = await backend.search(ProductSearchFilters(semantic_query="query"))

    assert [p.product_id for p in result.products] == [1, 2, 4, 3]
    assert len(result.products) == len(spring_order)


async def test_embedding_rerank_degrades_to_spring_order_on_store_failure(monkeypatch):
    """[#254] pgvector 장애·statement_timeout도 Spring 원순서 degrade 경계가 흡수한다."""
    store = _seed_store()
    spring_order = [SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)]

    def boom_top_k(*_args, **_kwargs):
        raise TimeoutError("pgvector timeout")

    async def fake_search(_filters):
        return ProductSearchResult(products=list(spring_order), total_count=3)

    monkeypatch.setattr(store, "top_k_by_vector", boom_top_k)
    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=_embed)
    result = await backend.search(ProductSearchFilters(semantic_query="여행 방수"))

    assert [p.product_id for p in result.products] == [3, 2, 1]


async def test_embedding_rerank_offloads_scoring_to_thread(monkeypatch):
    store = _seed_store()
    calls = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(search_service.asyncio, "to_thread", fake_to_thread)

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=1, name="파우치", price=30)], total_count=1
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=_embed)
    result = await backend.search(ProductSearchFilters(keyword="여행 방수", limit=10))

    # 임베딩 호출(_embed)·store 조회+정렬(_rerank) 둘 다 스레드로 오프로드됨(PR #42 리뷰)
    assert calls == [_embed, backend._rerank]
    assert result.products[0].product_id == 1


async def test_embedding_rerank_blank_semantic_query_passthrough(monkeypatch):
    """[#101 PR#166 리뷰] semantic_query 가 공백-only 면 재정렬을 건너뛰고 Spring 순서를 유지한다.

    공백 문자열은 truthy 라 `if not query_text` 가드를 통과해 무의미한 텍스트로 임베딩 API 를
    호출·정렬하게 된다 — 최종 소비 지점에서도 blank-only 를 걸러 불필요한 외부 호출을 막는다.
    """
    store = _seed_store()
    embed_calls: list = []

    def spy_embed(texts):
        embed_calls.append(texts)
        return _embed(texts)

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name=f"p{i}", price=10) for i in (3, 2, 1)],
            total_count=3,
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=spy_embed)
    result = await backend.search(ProductSearchFilters(semantic_query="   ", keyword=None))
    assert [p.product_id for p in result.products] == [3, 2, 1]  # Spring 순서 그대로(재정렬 skip)
    assert embed_calls == []  # 공백은 임베딩 호출조차 안 함


async def test_embedding_rerank_passthrough_without_keyword(monkeypatch):
    store = _seed_store()

    async def fake_search(filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=9, name="x", price=1)], total_count=1
        )

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=_embed)
    result = await backend.search(ProductSearchFilters(limit=10))  # keyword 없음
    assert [p.product_id for p in result.products] == [9]


async def test_vector_backend_without_hydrate_signals_c17():
    backend = VectorSearchBackend(store=_seed_store(), embed=_embed)  # hydrate 미주입
    with pytest.raises(spring_client.SpringUnavailableError):
        await backend.search(ProductSearchFilters(keyword="여행", limit=5))


async def test_vector_backend_with_hydrate_returns_ranked_and_receives_filters():
    store = _seed_store()
    seen = {}

    async def hydrate(ids, filters):
        seen["ids"] = ids
        seen["filters"] = filters
        # Spring 이 필터·가용성 적용했다고 가정, 벡터 순서 보존해 상위 limit 반환
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name="n", price=1) for i in ids[: filters.limit]],
            total_count=len(ids),
        )

    backend = VectorSearchBackend(store=store, embed=_embed, hydrate=hydrate, over_fetch=4)
    result = await backend.search(
        ProductSearchFilters(keyword="무선 이어폰", limit=3, category="이어폰")
    )
    assert result.products[0].product_id == 2
    assert seen["filters"].category == "이어폰"  # 필터가 hydrate 로 전달됨(리뷰 반영, finding 2)


async def test_vector_backend_offloads_ranking_to_thread(monkeypatch):
    store = _seed_store()
    calls = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(search_service.asyncio, "to_thread", fake_to_thread)

    async def hydrate(ids, filters):
        return ProductSearchResult(
            products=[SpringProduct(product_id=i, name="n", price=1) for i in ids[: filters.limit]],
            total_count=len(ids),
        )

    backend = VectorSearchBackend(store=store, embed=_embed, hydrate=hydrate)
    result = await backend.search(ProductSearchFilters(keyword="무선 이어폰", limit=3))

    # 임베딩 호출(_embed)·store.all() 스캔(vector_rank) 둘 다 스레드로 오프로드됨(PR #42 리뷰)
    assert calls == [_embed, vector_rank]
    assert result.products


def test_cosine_dim_mismatch_excluded():
    assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0]) == -1.0  # 차원 불일치 → 제외(finding 3)


def test_recall_at_k():
    assert recall_at_k([1, 2, 3], {1, 4}, 3) == pytest.approx(0.5)
    assert recall_at_k([], {1}, 3) == 0.0
    assert recall_at_k([1], set(), 3) == 0.0


def test_compare_backends_reports_both_methods():
    store = _seed_store()
    cases = [
        GoldenCase(query="여행 방수", relevant_ids={1}),
        GoldenCase(query="무선 이어폰", relevant_ids={2}),
    ]

    def candidates(_q):
        return [1, 2, 3]

    report = compare_backends(cases, store=store, embed=_embed, candidates=candidates, k=3)
    assert report.method1.mean_recall_at_k == pytest.approx(1.0)
    assert report.method2.mean_recall_at_k == pytest.approx(1.0)
    assert 0.0 <= report.mean_overlap <= 1.0


async def test_golden_spring_rank_beyond_limit_enters_after_compression(monkeypatch):
    """[#101 인수조건] Spring 정렬 상한(embedding_rerank_limit) 밖이지만 semanticQuery 와 유사도 높은
    상품이 pgvector 재정렬+압축 후 후보에 진입한다 — recall@limit 로 고정(compare.recall_at_k 재사용).

    타깃을 Spring 순서 맨 끝(= 상한 밖)에 두고 query 와 동일 임베딩을 준다. Spring 순서 상위-limit
    엔 없지만(recall 0), 방식2 재정렬 후 상위-limit 에 진입한다(recall 1). 이게 #101 의 핵심 인수조건.
    """
    from app.core.config import get_settings
    from app.pipelines.artifact_store import CatalogArtifact

    cap = get_settings().embedding_rerank_limit
    n = cap + 5
    target = n  # Spring 순서 맨 끝 = 상한(cap) 밖
    qvec = _embed(["여행 방수"])[0]

    store = CatalogArtifactStore()
    for pid in range(1, n + 1):
        # 타깃만 query 와 동일 임베딩(코사인 1.0), 나머지는 무관(어휘 겹침 없음 → 코사인 -1.0, 맨 뒤)
        emb = qvec if pid == target else _embed(["무관"])[0]
        store.upsert(CatalogArtifact(product_id=pid, search_doc=f"d{pid}", embedding=emb))

    spring_order = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=10) for pid in range(1, n + 1)
    ]

    async def fake_search(_filters):
        return ProductSearchResult(products=list(spring_order), total_count=n)

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    backend = EmbeddingRerankBackend(store=store, embed=_embed)
    result = await backend.search(ProductSearchFilters(semantic_query="여행 방수"))

    reranked_ids = [p.product_id for p in result.products]
    spring_ids = [p.product_id for p in spring_order]
    # recall@cap = 압축(상위-cap 절단) 후 진입 여부. Spring 순서엔 없고(0), 재정렬 후엔 있다(1).
    assert recall_at_k(spring_ids, {target}, cap) == 0.0
    assert recall_at_k(reranked_ids, {target}, cap) == 1.0


# ── 이슈 #65: 비대칭 임베딩 바인딩 — 미주입 기본값이 질의(QUERY) task_type 을 바인딩하는지 ──


def test_rerank_backend_default_embed_binds_query_task(monkeypatch):
    seen = {}

    def spy(texts, *, task_type=None):
        seen["task_type"] = task_type
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(_embedding, "embed_texts", spy)
    backend = EmbeddingRerankBackend()
    backend._embed(["질의"])
    assert seen["task_type"] == "RETRIEVAL_QUERY"


def test_vector_backend_default_embed_binds_query_task(monkeypatch):
    seen = {}

    def spy(texts, *, task_type=None):
        seen["task_type"] = task_type
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(_embedding, "embed_texts", spy)
    backend = VectorSearchBackend()
    backend._embed(["질의"])
    assert seen["task_type"] == "RETRIEVAL_QUERY"
