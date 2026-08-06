"""AI 생성물 갱신 배치 (이슈 #7, api-spec §4.8 / C-4) — 배치 루프·enrich·search_doc·fetch 배선.

LLM·embed·fetch 주입형 fake 로 구동(라이브 Anthropic/torch/Spring 불필요). 스토어는 주입(격리).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.pipelines import artifacts_batch as _batch
from app.pipelines import embedding as _embedding
from app.pipelines.artifact_store import ArtifactStore, CatalogArtifact, CatalogArtifactStore
from app.pipelines.artifacts_batch import run_artifacts_batch
from app.pipelines.enrichment import enrich_product
from app.schemas.spring import ProductChange, ProductChangesPage
from tests.integration._stubs import ScriptedLLM  # 배치 enrichment LLM 대역


def test_catalog_artifact_store_satisfies_shared_protocol():
    """CatalogArtifactStore(인메모리)·PgCatalogArtifactStore(pg-catalog) 공유 계약 정합 (이슈 #31)."""
    assert isinstance(CatalogArtifactStore(), ArtifactStore)


class _EnrichLLM:
    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        return json.dumps(
            {"tags": ["여행", "방수"], "attributes": {"소재": "나일론"}}, ensure_ascii=False
        )


def _embed(texts):
    return [[float(len(t)), 1.0] for t in texts]  # 결정적 2차원(값 자체는 미검증)


def _change(pid, status="ON_SALE", name="여행 방수 파우치"):
    return ProductChange(
        product_id=pid,
        status=status,
        updated_at="2026-07-20T00:00:00Z",
        name=name,
        description="설명",
        category="여행용품",
        brand="트래블",
        attributes={"방수": True},
    )


@pytest.mark.parametrize("status", ["ON_SALE", "HIDDEN"])
def test_product_change_accepts_spring_product_status(status):
    change = ProductChange(product_id=1, status=status, updated_at="2026-07-20T00:00:00Z")

    assert change.status == status


@pytest.mark.parametrize("status", ["ACTIVE", "DELISTED"])
def test_product_change_rejects_legacy_status(status):
    with pytest.raises(ValidationError):
        ProductChange(product_id=1, status=status, updated_at="2026-07-20T00:00:00Z")


# ── HTTP fake (fetch_product_changes 배선 검증) ──
class _Resp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._data


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        return self._resp


async def test_enrich_product_returns_extras():
    extras = await enrich_product(
        {"name": "파우치", "category": "여행용품"}, llm=_EnrichLLM(), settings=get_settings()
    )
    assert extras["tags"] == ["여행", "방수"]
    assert extras["attributes"] == {"소재": "나일론"}
    # [#148] situation_tags 는 홈 추천 reason 의 재료다 — LLM 이 안 주면 빈 목록(구 스키마 호환).
    assert extras["situation_tags"] == []


class _SituationLLM:
    """situation_tags 를 함께 주는 LLM — 확장된 enrichment 스키마(#148)."""

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        return json.dumps(
            {
                "tags": ["여행"],
                "situation_tags": ["해외여행", "  ", "기내반입", ""],
                "attributes": {},
            },
            ensure_ascii=False,
        )


async def test_enrich_product_extracts_situation_tags_for_home_reason():
    """[#148] 신규 상품도 홈 추천 reason 재료를 갖도록 situation_tags 를 뽑는다.

    이게 없으면 I-17 로 새로 들어온 상품만 조용히 reason 이 비어(기존 덤프 상품은 채워져 있어서)
    원인 파악이 어려워진다 — lessons "빈 결과는 계약 불일치를 먼저 의심하라" 와 같은 부류.
    """
    extras = await enrich_product(
        {"name": "캐리어", "category": "여행용품"}, llm=_SituationLLM(), settings=get_settings()
    )
    # 공백/빈 문자열은 reason 문장 틀에 끼면 "에 맞아요"가 되므로 걸러진다
    assert extras["situation_tags"] == ["해외여행", "기내반입"]


def test_build_search_doc_includes_fields_and_tags():
    doc = _embedding.build_search_doc(
        {
            "name": "여행 파우치",
            "category": "여행용품",
            "brand": "트래블",
            "attributes": {"방수": True},
            "extras": {"tags": ["기내반입"], "attributes": {"소재": "나일론"}},
        }
    )
    for token in ("여행 파우치", "여행용품", "트래블", "기내반입", "나일론"):
        assert token in doc


async def test_batch_processes_and_upserts():
    store = CatalogArtifactStore()

    async def fetch(cursor, limit):
        return ProductChangesPage(items=[_change(1), _change(2)], next_cursor="c1", has_more=False)

    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )
    assert result.processed == 2
    assert result.hidden == 0
    assert store.count() == 2
    art = store.get(1)
    assert art is not None and art.embedding and art.search_doc
    assert art.extras["tags"] == ["여행", "방수"]
    assert store.get_cursor() == "c1"


async def test_batch_uses_raw_product_fields_without_retaining_separate_copies():
    store = CatalogArtifactStore()

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, name="저장하면 안 되는 원본명")],
            next_cursor="c1",
            has_more=False,
        )

    await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )

    artifact = store.get(1)
    assert artifact is not None
    assert "저장하면 안 되는 원본명" in artifact.search_doc
    assert set(vars(artifact)) == {
        "product_id",
        "search_doc",
        "embedding",
        "extras",
        "embed_model",
        "embed_dim",
        "embed_task",
        "normalized",
    }


async def test_batch_hidden_removes_artifact():
    store = CatalogArtifactStore()
    store.upsert(CatalogArtifact(product_id=1, search_doc="x", embedding=[1.0, 0.0]))

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, status="HIDDEN")], next_cursor="c1", has_more=False
        )

    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )
    assert result.hidden == 1
    assert result.processed == 0
    assert store.get(1) is None


async def test_batch_hasmore_loops_and_persists_final_cursor():
    store = CatalogArtifactStore()
    pages = [
        ProductChangesPage(items=[_change(1)], next_cursor="c1", has_more=True),
        ProductChangesPage(items=[_change(2)], next_cursor="c2", has_more=False),
    ]
    seen = []

    async def fetch(cursor, limit):
        seen.append(cursor)
        return pages.pop(0)

    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )
    assert result.pages == 2
    assert result.processed == 2
    assert store.count() == 2
    assert store.get_cursor() == "c2"
    assert seen[1] == "c1"  # 2번째 fetch 는 1페이지 nextCursor 로 이어감


async def test_batch_full_rebuild_starts_from_zero():
    store = CatalogArtifactStore()
    store.set_cursor("old-cursor")
    seen = []

    async def fetch(cursor, limit):
        seen.append(cursor)
        return ProductChangesPage(items=[], next_cursor=None, has_more=False)

    await run_artifacts_batch(
        fetch=fetch,
        llm=_EnrichLLM(),
        embed=_embed,
        store=store,
        settings=get_settings(),
        full_rebuild=True,
    )
    assert seen[0] == "0"


async def test_batch_requires_llm(monkeypatch):
    import app.pipelines.artifacts_batch as ab

    monkeypatch.setattr(ab, "get_llm", lambda: None)
    with pytest.raises(RuntimeError):
        await run_artifacts_batch(
            llm=None, embed=_embed, store=CatalogArtifactStore(), settings=get_settings()
        )


async def test_fetch_product_changes_parses_envelope(monkeypatch):
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [{"productId": 7, "status": "ON_SALE", "updatedAt": "t", "name": "n"}],
            "nextCursor": "c9",
            "hasMore": True,
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _Client(_Resp(200, body)))
    page = await sc.fetch_product_changes("0", 500)
    assert page.has_more is True
    assert page.next_cursor == "c9"
    assert page.items[0].product_id == 7


@pytest.mark.parametrize("status", ["ACTIVE", "DELISTED"])
async def test_fetch_product_changes_rejects_legacy_status(monkeypatch, status):
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [{"productId": 7, "status": status, "updatedAt": "t"}],
            "nextCursor": None,
            "hasMore": False,
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _Client(_Resp(200, body)))

    with pytest.raises(sc.SpringUnavailableError, match="fetch_product_changes 실패"):
        await sc.fetch_product_changes("0", 500)


async def test_batch_invalid_status_preserves_entire_page_and_cursor(monkeypatch):
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {"productId": 1, "status": "ON_SALE", "updatedAt": "t", "name": "valid"},
                {"productId": 2, "status": "SOLD_OUT", "updatedAt": "t"},
            ],
            "nextCursor": "c1",
            "hasMore": False,
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _Client(_Resp(200, body)))
    store = CatalogArtifactStore()
    existing = CatalogArtifact(product_id=99, search_doc="existing", embedding=[1.0, 0.0])
    store.upsert(existing)
    store.set_cursor("checkpoint")

    with pytest.raises(sc.SpringUnavailableError, match="fetch_product_changes 실패"):
        await run_artifacts_batch(
            fetch=sc.fetch_product_changes,
            llm=_EnrichLLM(),
            embed=_embed,
            store=store,
            settings=get_settings(),
        )

    assert store.get(1) is None
    assert store.get(99) == existing
    assert store.get_cursor() == "checkpoint"


async def test_fetch_product_changes_error_raises(monkeypatch):
    import app.services.spring_client as sc

    monkeypatch.setattr(sc, "_client", lambda: _Client(_Resp(500, {})))
    with pytest.raises(sc.SpringUnavailableError):
        await sc.fetch_product_changes("0", 500)


async def test_fetch_product_changes_classifies_invalid_cursor(monkeypatch):
    import app.services.spring_client as sc

    body = {
        "success": False,
        "error": {"code": "INVALID_CURSOR", "message": "expired cursor"},
    }
    monkeypatch.setattr(sc, "_client", lambda: _Client(_Resp(400, body)))

    with pytest.raises(sc.InvalidCursorError):
        await sc.fetch_product_changes("expired", 500)


async def test_batch_invalid_cursor_rebuilds_from_zero_atomically():
    import app.services.spring_client as sc

    store = CatalogArtifactStore()
    store.upsert(CatalogArtifact(product_id=99, search_doc="stale", embedding=[0.0, 1.0]))
    store.set_cursor("expired")
    seen = []

    async def fetch(cursor, limit):
        seen.append(cursor)
        if cursor == "expired":
            raise sc.InvalidCursorError("expired cursor")
        return ProductChangesPage(items=[_change(1)], next_cursor="fresh", has_more=False)

    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )

    assert seen == ["expired", "0"]
    assert store.get(99) is None
    assert store.get(1) is not None
    assert store.get_cursor() == "fresh"
    assert result.cursor == "fresh"


async def test_batch_invalid_cursor_rebuild_failure_preserves_rebuild_start_checkpoint():
    import app.services.spring_client as sc

    store = CatalogArtifactStore()
    stale = CatalogArtifact(product_id=99, search_doc="stale", embedding=[0.0, 1.0])
    store.upsert(stale)
    store.set_cursor("expired")

    async def fetch(cursor, limit):
        if cursor == "expired":
            raise sc.InvalidCursorError("expired cursor")
        raise RuntimeError("rebuild failed")

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )

    assert store.get(99) == stale
    assert store.get_cursor() == "expired"


async def test_batch_invalid_cursor_after_committed_page_keeps_that_checkpoint_on_rebuild_failure():
    import app.services.spring_client as sc

    store = CatalogArtifactStore()
    stale = CatalogArtifact(product_id=99, search_doc="stale", embedding=[0.0, 1.0])
    store.upsert(stale)
    store.set_cursor("old")
    seen = []

    async def fetch(cursor, limit):
        seen.append(cursor)
        if cursor == "old":
            return ProductChangesPage(items=[_change(1)], next_cursor="page-1", has_more=True)
        if cursor == "page-1":
            raise sc.InvalidCursorError("expired cursor")
        raise RuntimeError("rebuild failed")

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )

    assert seen == ["old", "page-1", "0"]
    assert store.get(99) == stale
    assert store.get(1) is not None
    assert store.get_cursor() == "page-1"


async def test_batch_invalid_cursor_after_initial_page_rebuilds_from_zero():
    import app.services.spring_client as sc

    store = CatalogArtifactStore()
    seen = []

    async def fetch(cursor, limit):
        seen.append(cursor)
        if seen == ["0"]:
            return ProductChangesPage(items=[_change(1)], next_cursor="page-1", has_more=True)
        if seen == ["0", "page-1"]:
            raise sc.InvalidCursorError("expired cursor")
        return ProductChangesPage(items=[_change(2)], next_cursor="fresh", has_more=False)

    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )

    assert seen == ["0", "page-1", "0"]
    assert store.get(1) is None
    assert store.get(2) is not None
    assert store.get_cursor() == "fresh"
    assert result.cursor == "fresh"


async def test_batch_invalid_cursor_at_zero_raises_without_repeating_same_request():
    import app.services.spring_client as sc

    store = CatalogArtifactStore()
    seen = []

    async def fetch(cursor, limit):
        seen.append(cursor)
        raise sc.InvalidCursorError("zero cursor rejected")

    with pytest.raises(sc.InvalidCursorError, match="zero cursor rejected"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )

    assert seen == ["0"]
    assert store.get_cursor() is None


async def test_batch_invalid_cursor_commit_failure_preserves_rebuild_start_checkpoint():
    import app.services.spring_client as sc

    class FailingCommitStore(CatalogArtifactStore):
        fail_commit = False

        def set_cursor(self, cursor):
            if self.fail_commit:
                raise RuntimeError("cursor commit failed")
            super().set_cursor(cursor)

        def replace_all_and_set_cursor(self, artifacts, cursor):
            if self.fail_commit:
                raise RuntimeError("atomic commit failed")
            super().replace_all_and_set_cursor(artifacts, cursor)

    store = FailingCommitStore()
    stale = CatalogArtifact(product_id=99, search_doc="stale", embedding=[0.0, 1.0])
    store.upsert(stale)
    store.set_cursor("expired")
    store.fail_commit = True

    async def fetch(cursor, limit):
        if cursor == "expired":
            raise sc.InvalidCursorError("expired cursor")
        return ProductChangesPage(items=[_change(1)], next_cursor="fresh", has_more=False)

    with pytest.raises(RuntimeError, match="commit failed"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )

    assert store.get(99) == stale
    assert store.get(1) is None
    assert store.get_cursor() == "expired"


async def test_batch_full_rebuild_replaces_stale():
    store = CatalogArtifactStore()
    store.upsert(CatalogArtifact(product_id=99, search_doc="old", embedding=[0.0, 1.0]))  # stale
    store.set_cursor("old")

    async def fetch(cursor, limit):
        return ProductChangesPage(items=[_change(1)], next_cursor="c1", has_more=False)

    result = await run_artifacts_batch(
        fetch=fetch,
        llm=_EnrichLLM(),
        embed=_embed,
        store=store,
        settings=get_settings(),
        full_rebuild=True,
    )
    assert store.get(99) is None  # stale 원자 교체로 제거(finding 1)
    assert store.get(1) is not None
    assert store.count() == 1
    assert store.get_cursor() == "c1"
    assert result.processed == 1


async def test_batch_full_rebuild_preserves_on_failure():
    store = CatalogArtifactStore()
    store.upsert(CatalogArtifact(product_id=99, search_doc="old", embedding=[0.0, 1.0]))

    async def fetch(cursor, limit):
        raise RuntimeError("rebuild boom")

    with pytest.raises(RuntimeError):
        await run_artifacts_batch(
            fetch=fetch,
            llm=_EnrichLLM(),
            embed=_embed,
            store=store,
            settings=get_settings(),
            full_rebuild=True,
        )
    assert store.get(99) is not None  # 재구축 실패 시 기존 데이터 보존(원자 교체)


async def test_fetch_product_changes_failure_envelope_raises(monkeypatch):
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _Client(_Resp(200, {"success": False, "data": None}))
    )
    with pytest.raises(sc.SpringUnavailableError):
        await sc.fetch_product_changes("0", 500)


async def test_fetch_product_changes_data_null_raises(monkeypatch):
    import app.services.spring_client as sc

    monkeypatch.setattr(sc, "_client", lambda: _Client(_Resp(200, {"success": True, "data": None})))
    with pytest.raises(sc.SpringUnavailableError):
        await sc.fetch_product_changes("0", 500)


# ── 이슈 #65: 비대칭 임베딩 바인딩 — 배치 미주입 기본값이 문서(DOCUMENT) task_type 을 바인딩하는지 ──


async def test_batch_default_embed_binds_document_task(monkeypatch):
    seen = {}

    def spy(texts, *, task_type=None):
        seen["task_type"] = task_type
        return [[float(len(t)), 0.0, 1.0] for t in texts]

    monkeypatch.setattr(_embedding, "embed_texts", spy)

    async def fetch(cursor, size):
        return ProductChangesPage(
            items=[
                ProductChange(
                    productId=1,
                    status="ON_SALE",
                    updatedAt="2026-07-20T00:00:00Z",
                    name="상품-1",
                    description="설명",
                    category="여행용품",  # 주의: ProductChange 의 별칭은 category(SpringProduct 의 categoryName 과 다름)
                    brand="브랜드",  # 주의: ProductChange 의 별칭은 brand(SpringProduct 의 brandName 과 다름)
                )
            ],
            nextCursor=None,
            hasMore=False,
        )

    store = CatalogArtifactStore()
    await _batch.run_artifacts_batch(
        fetch=fetch, llm=ScriptedLLM(), store=store, settings=None, full_rebuild=False
    )
    assert seen["task_type"] == "RETRIEVAL_DOCUMENT"


async def test_batch_records_provenance_from_settings(monkeypatch):
    """이슈 #65: 런타임 배치가 임베딩 프로비넌스를 settings 상수로 채우는지 (embedding_meta_complete CHECK 대응)."""
    monkeypatch.setattr(
        _embedding,
        "embed_texts",
        lambda texts, *, task_type=None: [[1.0] + [0.0] * 1535 for _ in texts],
    )

    async def fetch(cursor, size):
        return ProductChangesPage(
            items=[
                ProductChange(
                    productId=7,
                    status="ON_SALE",
                    updatedAt="2026-07-20T00:00:00Z",
                    name="상품-7",
                    description="설명",
                    category="여행용품",
                    brand="브랜드",
                )
            ],
            nextCursor=None,
            hasMore=False,
        )

    store = CatalogArtifactStore()
    await _batch.run_artifacts_batch(fetch=fetch, llm=ScriptedLLM(), store=store)

    art = store.get(7)
    assert art.embed_model == "gemini-embedding-001"
    assert art.embed_dim == 1536
    assert art.embed_task == "RETRIEVAL_DOCUMENT"
    assert art.normalized is True


@pytest.mark.asyncio
async def test_batch_embed_dim_reflects_actual_vector(monkeypatch):
    """embed_dim 은 settings 상수가 아니라 실제 반환 벡터 길이에서 온다(이슈 #65 PR 리뷰)."""
    monkeypatch.setattr(
        _embedding,
        "embed_texts",
        lambda texts, *, task_type=None: [[0.0] * 8 for _ in texts],  # 8차원(설정 1536과 다름)
    )

    async def fetch(cursor, size):
        return ProductChangesPage(
            items=[
                ProductChange(
                    productId=8,
                    status="ON_SALE",
                    updatedAt="2026-07-20T00:00:00Z",
                    name="상품-8",
                    description="설명",
                    category="여행용품",
                    brand="브랜드",
                )
            ],
            nextCursor=None,
            hasMore=False,
        )

    store = CatalogArtifactStore()
    await _batch.run_artifacts_batch(fetch=fetch, llm=ScriptedLLM(), store=store)

    assert store.get(8).embed_dim == 8


async def test_color_harvest_flag_off_does_not_touch_harvester(monkeypatch):
    settings = get_settings().model_copy(update={"color_synonym_batch_harvest_enabled": False})

    async def forbidden(*args, **kwargs):
        pytest.fail("default-off batch must not harvest colors")

    monkeypatch.setattr(_batch, "_harvest_change_colors", forbidden)
    await _batch._process_change(
        _change(1), llm=_EnrichLLM(), embed=_embed, store=CatalogArtifactStore(), settings=settings
    )


async def test_color_harvest_only_adds_pending_new_terms(monkeypatch):
    settings = get_settings().model_copy(update={"color_synonym_batch_harvest_enabled": True})
    seen = []

    async def harvest(change, *, settings):
        seen.append((change.attributes, settings.color_synonym_batch_harvest_enabled))
        return 1

    monkeypatch.setattr(_batch, "_harvest_change_colors", harvest)
    await _batch._process_change(
        _change(1), llm=_EnrichLLM(), embed=_embed, store=CatalogArtifactStore(), settings=settings
    )
    assert seen == [({"방수": True}, True)]


async def test_color_harvest_count_limit_logs_and_does_not_kill_i17_artifact(
    monkeypatch, caplog
):
    settings = get_settings().model_copy(
        update={
            "color_synonym_batch_harvest_enabled": True,
            "color_synonym_harvest_max_terms_per_product": 2,
            "color_synonym_harvest_scan_max_values_per_product": 100,
            "color_synonym_harvest_max_term_length": 40,
        }
    )
    store = CatalogArtifactStore()
    change = _change(1).model_copy(
        update={"attributes": {"색상": ["블랙", "화이트", "레드"]}}
    )
    harvested: list[str] = []

    def harvest(
        dsn,
        attributes,
        embed,
        model,
        threshold,
        *,
        max_terms,
        max_term_length,
        scan_max_values,
    ):
        assert scan_max_values == 100
        harvested.extend(
            _batch.color_synonym_seed.extract_color_terms(
                attributes,
                max_terms=max_terms,
                max_term_length=max_term_length,
                scan_max_values=scan_max_values,
            )
        )
        return len(harvested)

    monkeypatch.setattr(_batch.color_synonym_seed, "harvest_new_terms", harvest)
    monkeypatch.setattr(_batch, "_harvest_limiters", {})

    with caplog.at_level("WARNING"):
        await _batch._process_change(
            change,
            llm=_EnrichLLM(),
            embed=_embed,
            store=store,
            settings=settings,
        )

    assert harvested == ["블랙", "화이트"]
    assert "색상 표기 개수 상한 초과" in caplog.text
    assert store.get(1) is not None


async def test_color_harvest_failure_does_not_kill_i17_artifact(monkeypatch, caplog):
    settings = get_settings().model_copy(update={"color_synonym_batch_harvest_enabled": True})
    store = CatalogArtifactStore()

    async def fail(*args, **kwargs):
        raise RuntimeError("color DB unavailable")

    monkeypatch.setattr(_batch, "_harvest_change_colors", fail)
    with caplog.at_level("WARNING"):
        await _batch._process_change(
            _change(1), llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
        )
    assert store.get(1) is not None
    assert "색상 표기 수확 실패" in caplog.text


async def test_color_harvest_timeout_does_not_kill_i17_artifact(monkeypatch, caplog):
    import asyncio

    settings = get_settings().model_copy(
        update={
            "color_synonym_batch_harvest_enabled": True,
            "color_synonym_query_timeout_s": 0.001,
        }
    )
    store = CatalogArtifactStore()

    async def never_finishes(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(_batch.asyncio, "to_thread", never_finishes)
    with caplog.at_level("WARNING"):
        await _batch._process_change(
            _change(1), llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
        )
    assert store.get(1) is not None
    assert "색상 표기 수확 실패" in caplog.text


async def test_color_harvest_saturation_skips_without_delaying_batch(monkeypatch):
    import asyncio
    import threading

    settings = get_settings().model_copy(
        update={
            "color_synonym_batch_harvest_enabled": True,
            "color_synonym_harvest_max_concurrency": 1,
            "color_synonym_query_timeout_s": 0.01,
        }
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_harvest(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1.0)
        return 1

    monkeypatch.setattr(_batch.color_synonym_seed, "harvest_new_terms", blocked_harvest)
    monkeypatch.setattr(_batch, "_harvest_limiters", {})
    monkeypatch.setattr(_batch, "_background_harvest_tasks", set(), raising=False)
    try:
        with pytest.raises(TimeoutError):
            await _batch._harvest_change_colors(_change(1), settings=settings)
        assert started.is_set()
        assert len(_batch._background_harvest_tasks) == 1

        store = CatalogArtifactStore()
        started_at = asyncio.get_running_loop().time()
        await _batch._process_change(
            _change(2), llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
        )
        elapsed = asyncio.get_running_loop().time() - started_at

        assert store.get(2) is not None
        assert calls == 1
        assert elapsed < 0.1
    finally:
        release.set()
        for _ in range(100):
            if not _batch._background_harvest_tasks:
                break
            await asyncio.sleep(0.001)
        assert not _batch._background_harvest_tasks


async def test_background_harvest_logs_only_late_failure(caplog):
    import asyncio

    async def fail():
        raise RuntimeError("late harvest failure")

    async def succeed():
        return 1

    failed = asyncio.create_task(fail())
    succeeded = asyncio.create_task(succeed())
    cancelled = asyncio.create_task(asyncio.sleep(1))
    cancelled.cancel()
    await asyncio.sleep(0)

    with caplog.at_level("WARNING"):
        _batch._consume_background_harvest(failed)
        _batch._consume_background_harvest(succeeded)
        _batch._consume_background_harvest(cancelled)

    assert caplog.text.count("백그라운드") == 1
    assert "late harvest failure" in caplog.text


# ── 이슈 #325: enrichment 토큰 예산 + _drain 항목 격리(head-of-line blocking 해소) ──


class _CapturingLLM:
    """enrichment 호출 kwargs(max_tokens·reasoning_effort)를 캡처하는 fake(#325 회귀 방지)."""

    def __init__(self):
        self.calls: list[tuple[int, str | None]] = []

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        self.calls.append((max_tokens, reasoning_effort))
        return json.dumps({"tags": ["여행"], "attributes": {}}, ensure_ascii=False)


async def test_enrich_product_sends_configured_max_tokens_and_effort():
    """#325 회귀 방지 — 600 하드코딩 대신 settings.enrichment_max_tokens/effort 를 싣는다."""
    settings = get_settings().model_copy(
        update={"enrichment_max_tokens": 3333, "enrichment_reasoning_effort": "low"}
    )
    llm = _CapturingLLM()
    await enrich_product({"name": "파우치", "category": "여행용품"}, llm=llm, settings=settings)
    assert llm.calls == [(3333, "low")]


class _FlakyLLM:
    """상품명별 실패 횟수를 지정해 재시도·단건 격리 테스트에 쓴다(#325)."""

    def __init__(self, fail_counts=None, always_fail=None):
        self._fail_counts = dict(fail_counts or {})
        self._always_fail = set(always_fail or [])
        self.calls_by_name: dict[str, int] = {}

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        payload = json.loads(user)
        name = payload.get("name")
        self.calls_by_name[name] = self.calls_by_name.get(name, 0) + 1
        if name in self._always_fail:
            from app.core.llm import LLMError

            raise LLMError(f"boom: {name}")
        remaining = self._fail_counts.get(name, 0)
        if remaining > 0:
            self._fail_counts[name] = remaining - 1
            from app.core.llm import LLMError

            raise LLMError(f"flaky boom: {name}")
        return json.dumps({"tags": ["여행"], "attributes": {}}, ensure_ascii=False)


async def test_drain_isolates_single_item_failure(caplog):
    """3건 중 1건 실패 → 나머지 2건 처리·upsert, failed=1, 커서 전진, ERROR 로그에 product_id."""
    store = CatalogArtifactStore()
    changes = [
        _change(1, name="상품A"),
        _change(2, name="상품B-실패"),
        _change(3, name="상품C"),
    ]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["상품B-실패"])
    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=get_settings()
        )

    assert result.processed == 2
    assert result.failed == 1
    assert store.get(1) is not None
    assert store.get(2) is None
    assert store.get(3) is not None
    assert store.get_cursor() == "c1"
    assert "product_id=2" in caplog.text


async def test_drain_retries_then_succeeds():
    """1차 실패 → 2차 성공이면 processed 에 포함, failed=0."""
    store = CatalogArtifactStore()
    changes = [_change(1, name="플레이키")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(fail_counts={"플레이키": 1})
    settings = get_settings().model_copy(update={"enrichment_item_attempts": 2})
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result.processed == 1
    assert result.failed == 0
    assert llm.calls_by_name["플레이키"] == 2
    assert store.get(1) is not None


async def test_drain_dead_letters_after_attempts_exhausted():
    """attempts 초과 시 dead-letter(격리) — 재시도 횟수만큼만 호출.

    페이지 표본(3)이 artifacts_batch_failure_min_sample(5) 미만이라 비율 가드는 애초에
    평가되지 않는다 — 이 테스트는 단건 격리(retry exhaustion) 만 검증하고, 임계 가드는 별도
    테스트가 담당한다. 성공 항목 2건은 processed 카운트도 함께 확인하기 위한 것이다.
    """
    store = CatalogArtifactStore()
    changes = [_change(1, name="영구실패"), _change(2, name="성공A"), _change(3, name="성공B")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["영구실패"])
    settings = get_settings().model_copy(update={"enrichment_item_attempts": 3})
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result.failed == 1
    assert result.processed == 2
    assert llm.calls_by_name["영구실패"] == 3
    assert store.get(1) is None


async def test_drain_page_failure_threshold_blocks_cursor_advance():
    """표본(5) ≥ min_sample(5) 이고 전부 실패(ratio=1.0 ≥ 0.5) → 예외 전파 + 커서 미전진."""
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    names = [f"실패{i}" for i in range(5)]
    changes = [_change(i + 1, name=name) for i, name in enumerate(names)]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=names)
    with pytest.raises(_batch.PageFailureThresholdExceeded):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=get_settings()
        )

    assert store.get_cursor() == "checkpoint"
    for i in range(1, 6):
        assert store.get(i) is None


@pytest.mark.parametrize(
    ("fail_names", "expect_advance"),
    [
        (["A-실패", "B-실패"], True),  # 2/8 = 0.25 < 0.5 → 전진
        (["A-실패", "B-실패", "C-실패", "D-실패"], False),  # 4/8 = 0.5 ≥ 0.5 → 미전진
    ],
)
async def test_drain_page_failure_threshold_boundary(fail_names, expect_advance):
    """표본(8) ≥ min_sample(5) 이라 비율 가드 경계(0.5)가 그대로 유효하다."""
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [
        _change(1, name="A-실패"),
        _change(2, name="B-실패"),
        _change(3, name="C-실패"),
        _change(4, name="D-실패"),
        _change(5, name="E"),
        _change(6, name="F"),
        _change(7, name="G"),
        _change(8, name="H"),
    ]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=fail_names)

    if expect_advance:
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=get_settings()
        )
        assert result.failed == len(fail_names)
        assert store.get_cursor() == "c1"
    else:
        with pytest.raises(_batch.PageFailureThresholdExceeded):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=get_settings()
            )
        assert store.get_cursor() == "checkpoint"


async def test_drain_single_item_page_isolates_and_advances_default_settings(caplog):
    """[#325] 핵심 회귀 — 운영 증분 페이지는 대개 1~3건, 성공 패딩 없이 항목 1건짜리 페이지를
    기본 Settings() 그대로 재현한다(운영 시나리오 그대로).

    수정 전에는 표본=1, ratio=1/1=1.0 ≥ artifacts_batch_failure_ratio_threshold(0.5) 로
    PageFailureThresholdExceeded 가 던져져 커서가 막혔다 — #325 가 보고한 "문제 상품 1건이
    매 주기 실패" 그 상황이다. 수정 후에는 표본(1) < artifacts_batch_failure_min_sample(5)
    이라 비율 가드를 건너뛰고 격리+전진한다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["문제상품"])
    with caplog.at_level("WARNING"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=get_settings()
        )

    assert result.failed == 1
    assert result.processed == 0
    assert store.get(1) is None
    assert store.get_cursor() == "c1"
    assert "product_id=1" in caplog.text  # dead-letter ERROR 로그
    assert "표본 부족" in caplog.text  # min_sample 미달 WARNING 로그
    assert "failed=1" in caplog.text
    assert "min_sample=5" in caplog.text


async def test_drain_hidden_delete_failure_is_not_isolated():
    """HIDDEN 삭제 실패는 격리 대상이 아니다 — 그대로 전파(api-spec §4.8 fail-closed)."""

    class _FailingDeleteStore(CatalogArtifactStore):
        def delete(self, product_id):
            raise RuntimeError("delete boom")

    store = _FailingDeleteStore()
    store.set_cursor("checkpoint")

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, status="HIDDEN")], next_cursor="c1", has_more=False
        )

    with pytest.raises(RuntimeError, match="delete boom"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )
    assert store.get_cursor() == "checkpoint"


async def test_drain_isolation_applies_under_full_rebuild_with_atomic_replace():
    """full_rebuild 경로에서도 단건 격리가 동작 — replace_all 원자성은 불변(#325).

    페이지 표본(3)이 min_sample(5) 미만이라 비율 가드는 평가되지 않는다 — 이 테스트는 격리가
    full_rebuild 임시 스토어·원자 교체 경로에서도 동작함을 검증한다.
    """
    store = CatalogArtifactStore()
    stale = CatalogArtifact(product_id=99, search_doc="stale", embedding=[0.0, 1.0])
    store.upsert(stale)
    changes = [_change(1, name="A"), _change(2, name="B-실패"), _change(3, name="C")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["B-실패"])
    result = await run_artifacts_batch(
        fetch=fetch,
        llm=llm,
        embed=_embed,
        store=store,
        settings=get_settings(),
        full_rebuild=True,
    )

    assert result.processed == 2
    assert result.failed == 1
    assert store.get(99) is None  # 원자 교체로 stale 제거
    assert store.get(1) is not None
    assert store.get(2) is None
    assert store.get(3) is not None


async def test_default_settings_batch_isolation_smoke():
    """[#325] 새 튜너블을 하나도 override 하지 않은 기본 Settings() 조합 스모크 테스트.

    lessons: 과거 모든 테스트가 기본값을 덮어써 기본 조합 결함이 출하된 전례가 있다. 페이지
    표본(3)이 기본 min_sample(5) 미만이라 비율 가드는 평가되지 않는다 — 1건짜리 페이지 회귀는
    test_drain_single_item_page_isolates_and_advances_default_settings 가 전담한다.
    """
    store = CatalogArtifactStore()
    changes = [
        _change(1, name="기본값A"),
        _change(2, name="기본값B-실패"),
        _change(3, name="기본값C"),
    ]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["기본값B-실패"])
    result = await run_artifacts_batch(fetch=fetch, llm=llm, embed=_embed, store=store)

    assert result.processed == 2
    assert result.failed == 1
    assert store.get(1) is not None
    assert store.get(2) is None
    assert store.get(3) is not None
    assert store.get_cursor() == "c1"


# ── 라운드 3(#325, PR #399 Claude 리뷰 대응): 격리 후보를 실패 "종류"로 가른다 ──
#
# 리뷰 지적: 운영 증분 페이지는 대개 1~3건이라 page_total < min_sample(5) 가 거의 항상 참이고,
# 그러면 비율 가드는 사실상 죽은 코드가 된다 — embed()·store.upsert() 같은 인프라 장애도 매번
# "poison 단건"으로 오분류돼 dead-letter 처리된 채 커서가 전진한다. 아래는 격리 후보를
# enrich_product 단계의 내용 실패로만 구조적으로 한정해 이를 해소했음을 고정한다.


class _FailingEmbed:
    """embed() 가 항상 예외를 내는 fake — 인프라 장애가 격리되지 않음을 검증한다(#325 R3)."""

    def __call__(self, texts):
        raise RuntimeError("embed API down")


class _FailingUpsertStore(CatalogArtifactStore):
    """store.upsert() 가 항상 예외를 내는 스토어 — 인프라 장애가 격리되지 않음을 검증한다(#325 R3)."""

    def upsert(self, artifact):
        raise RuntimeError("catalog store down")


async def test_drain_embed_failure_is_not_isolated_multi_item_page():
    """3건짜리 페이지에서 embed() 가 항상 예외 → 예외가 그대로 전파되고 커서는 미전진(#325 R3).

    수정 전에는 이 실패도 _process_change 전체를 감싼 broad except 에 잡혀 단건 격리 대상이
    됐다 — 임베딩 API 전면 다운 같은 광역 장애가 매번 poison 단건으로 오분류되던 리뷰 지적
    시나리오다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="A"), _change(2, name="B"), _change(3, name="C")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(
            fetch=fetch,
            llm=_EnrichLLM(),
            embed=_FailingEmbed(),
            store=store,
            settings=get_settings(),
        )

    assert store.get_cursor() == "checkpoint"
    assert store.get(1) is None
    assert store.get(2) is None
    assert store.get(3) is None


async def test_drain_upsert_failure_is_not_isolated_multi_item_page():
    """3건짜리 페이지에서 store.upsert() 가 항상 예외 → 예외가 그대로 전파되고 커서는 미전진(#325 R3)."""
    store = _FailingUpsertStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="A"), _change(2, name="B"), _change(3, name="C")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    with pytest.raises(RuntimeError, match="catalog store down"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )

    assert store.get_cursor() == "checkpoint"


async def test_drain_embed_failure_propagates_even_on_single_item_page():
    """[#325 R3 핵심 회귀] 리뷰어가 지적한 시나리오 그대로 — 표본 1건짜리 페이지에서도 embed
    실패는 격리되지 않고 전파된다. 표본 하한(min_sample)은 비율 가드에만 적용되고,
    embed/store 실패는 애초에 격리 후보가 아니므로 비율 가드 자체를 거치지 않는다 — 페이지
    크기와 무관하게 광역 장애가 자연 복구 경로로 간다는 것이 이번 라운드의 핵심 수정이다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(
            fetch=fetch,
            llm=_EnrichLLM(),
            embed=_FailingEmbed(),
            store=store,
            settings=get_settings(),
        )

    assert store.get_cursor() == "checkpoint"
    assert store.get(1) is None


class _TimeoutLLM:
    """enrich_product 호출이 항상 타임아웃 계열 예외를 내는 fake(#325 R3).

    cause_chain=True 면 ``LLMError(...) from TimeoutError()`` 형태로 원인 체인에만 타임아웃을
    심어 ``is_timeout_error`` 의 ``__cause__`` 추적이 실제 _drain 판정에 쓰이는지 고정한다 —
    OpenAILLM.complete 이 SDK 예외를 이 형태로 감싸는 실제 규약을 재현한다.
    """

    def __init__(self, *, cause_chain=False):
        self._cause_chain = cause_chain
        self.calls = 0

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        self.calls += 1
        if self._cause_chain:
            from app.core.llm import LLMError

            try:
                raise TimeoutError("upstream timed out")
            except TimeoutError as exc:
                raise LLMError("timeout") from exc
        raise TimeoutError("upstream timed out")


async def test_drain_enrichment_timeout_not_isolated_direct():
    """enrich_product 가 TimeoutError 자체를 내면 재시도 소진 후 격리 없이 전파 + 커서 미전진
    (#325 R3 규칙 2). LLM API 자체가 응답하지 않는 상황은 항목 내용과 무관한 광역 장애다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _TimeoutLLM()
    settings = get_settings()
    with pytest.raises(TimeoutError):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert llm.calls == settings.enrichment_item_attempts  # 재시도 상한만큼 시도 후 전파
    assert store.get_cursor() == "checkpoint"
    assert store.get(1) is None


async def test_drain_enrichment_timeout_not_isolated_cause_chain():
    """LLMError(...) from TimeoutError() 형태(원인 체인)도 타임아웃으로 판정돼 격리 없이 전파된다
    — OpenAILLM.complete 이 SDK 예외를 ``raise LLMError(str(exc)) from exc`` 로 감싸는 실제
    경로를 재현해 is_timeout_error 의 원인 체인 추적이 _drain 판정에 실제로 쓰이는지 고정한다
    (#325 R3 규칙 2, 문자열 매칭 금지).
    """
    from app.core.llm import LLMError

    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _TimeoutLLM(cause_chain=True)
    with pytest.raises(LLMError):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=get_settings()
        )

    assert store.get_cursor() == "checkpoint"
    assert store.get(1) is None


async def test_color_harvest_cancellation_propagates(monkeypatch):
    import asyncio

    settings = get_settings().model_copy(update={"color_synonym_batch_harvest_enabled": True})

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(_batch, "_harvest_change_colors", cancel)
    with pytest.raises(asyncio.CancelledError):
        await _batch._process_change(
            _change(1),
            llm=_EnrichLLM(),
            embed=_embed,
            store=CatalogArtifactStore(),
            settings=settings,
        )
