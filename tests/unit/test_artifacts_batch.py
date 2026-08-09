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


@pytest.mark.parametrize(
    ("name", "expected_present"),
    [("여행 방수 파우치", True), (None, False), ("   ", False)],
)
async def test_batch_records_name_presence_without_changing_search_doc(
    name: str | None, expected_present: bool
) -> None:
    """[#468 I-17] 원본 이름 유무는 extras 플래그로만 남기고 임베딩 입력은 그대로 유지한다.

    이 플래그를 빼면 이름 없는 상품의 category 첫 줄이 상품명처럼 추천 문맥에 실리고,
    search_doc 생성을 플래그 때문에 바꾸면 기존 임베딩이 드리프트한다.
    """
    from app.pipelines.artifact_store import EXTRAS_NAME_PRESENT_KEY

    store = CatalogArtifactStore()

    async def fetch(cursor, limit):
        return ProductChangesPage(items=[_change(1, name=name)], next_cursor="c1", has_more=False)

    await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
    )

    artifact = store.get(1)
    assert artifact is not None
    assert artifact.extras[EXTRAS_NAME_PRESENT_KEY] is expected_present
    expected_doc = _embedding.build_search_doc(
        {
            "name": name,
            "description": "설명",
            "category": "여행용품",
            "brand": "트래블",
            "attributes": {"방수": True},
            "extras": {"tags": ["여행", "방수"], "attributes": {"소재": "나일론"}},
        }
    )
    assert artifact.search_doc == expected_doc


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


async def test_color_harvest_count_limit_logs_and_does_not_kill_i17_artifact(monkeypatch, caplog):
    settings = get_settings().model_copy(
        update={
            "color_synonym_batch_harvest_enabled": True,
            "color_synonym_harvest_max_terms_per_product": 2,
            "color_synonym_harvest_scan_max_values_per_product": 100,
            "color_synonym_harvest_max_term_length": 40,
        }
    )
    store = CatalogArtifactStore()
    change = _change(1).model_copy(update={"attributes": {"색상": ["블랙", "화이트", "레드"]}})
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
    """3건 중 1건 실패 → 나머지 2건 처리·upsert, failed=1, 커서 전진, ERROR 로그에 product_id.

    content_retry_cycles=0 — 이 테스트는 1선 즉시 격리 자체(#325)를 검증한다. 기본값(1주기
    재시도, #421)에서의 동작은 test_drain_content_failure_recovers_on_next_cycle 등이 담당한다.
    """
    store = CatalogArtifactStore()
    changes = [
        _change(1, name="상품A"),
        _change(2, name="상품B-실패"),
        _change(3, name="상품C"),
    ]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["상품B-실패"])
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
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

    [T6, PR 리뷰 라운드 4] ``artifacts_batch_content_retry_cycles=0`` 을 명시해 즉시 격리를
    강제한다 — 기본값(1)에서는 콘텐츠 실패가 이 시점엔 재시도 큐에 등재될 뿐 격리가 확정되지
    않아(``failed`` 는 이제 격리 확정 시에만 증가) 이 테스트의 "dead-letter(격리)" 라는 이름과
    어긋난다.
    """
    store = CatalogArtifactStore()
    changes = [_change(1, name="영구실패"), _change(2, name="성공A"), _change(3, name="성공B")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["영구실패"])
    settings = get_settings().model_copy(
        update={"enrichment_item_attempts": 3, "artifacts_batch_content_retry_cycles": 0}
    )
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
    """표본(8) ≥ min_sample(5) 이라 비율 가드 경계(0.5)가 그대로 유효하다.

    [T6, PR 리뷰 라운드 4] ``content_retry_cycles=0`` 을 명시해 1선 실패가 재시도 큐로 새지
    않고 즉시 격리되도록 고정한다 — 기본값(1)이면 이 테스트가 검증하려는 ``failed`` 값이
    "이번 주기에 실제로 격리된 건수"가 아니라 "이번 주기에 재시도 큐로 넘어간 건수(아직
    미확정)"가 돼 3선 경계 검증(비율 가드)과 무관한 잡음이 섞인다.
    """
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
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})

    if expect_advance:
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )
        assert result.failed == len(fail_names)
        assert store.get_cursor() == "c1"
    else:
        with pytest.raises(_batch.PageFailureThresholdExceeded):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"


async def test_drain_single_item_page_isolates_and_advances_default_settings(caplog):
    """[#325] 핵심 회귀 — 운영 증분 페이지는 대개 1~3건, 성공 패딩 없이 항목 1건짜리 페이지를
    기본 Settings() 그대로 재현한다(운영 시나리오 그대로).

    수정 전에는 표본=1, ratio=1/1=1.0 ≥ artifacts_batch_failure_ratio_threshold(0.5) 로
    PageFailureThresholdExceeded 가 던져져 커서가 막혔다 — #325 가 보고한 "문제 상품 1건이
    매 주기 실패" 그 상황이다. 수정 후에는 표본(1) < artifacts_batch_failure_min_sample(5)
    이라 비율 가드를 건너뛰고 전진한다.

    [T6, PR 리뷰 라운드 4] 기본 예산(``artifacts_batch_content_retry_cycles=1``)에서는 이
    시점에 "격리"가 아니라 "재시도 큐 등재"가 확정된다 — ``failed``(격리 확정 카운터)는 0,
    ``retry_pending`` 이 1이 되어 대기 중임을 드러낸다. 예전엔 등재도 ``failed`` 를 올려
    "즉시 격리"처럼 보였을 뿐이다(dead-letter ERROR 로그는 실제로 뜨지 않았다 — WARNING 만).
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

    assert result.failed == 0
    assert result.retry_pending == 1
    assert result.processed == 0
    assert store.get(1) is None
    assert store.get_cursor() == "c1"
    assert "product_id=1" in caplog.text  # 다음 주기 재시도 예약 WARNING 로그
    assert "표본 부족" in caplog.text  # min_sample 미달 WARNING 로그
    assert "failed=1" in caplog.text  # 이 페이지 로그의 page_failed(3선용, 무관 필드 아님)
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

    [T6, PR 리뷰 라운드 4] ``content_retry_cycles=0`` 을 명시해 즉시 격리를 강제한다 —
    기본값(1)이면 이 시점엔 재시도 큐 등재일 뿐이라(``failed`` 는 격리 확정 시에만 증가)
    "격리가 동작함"을 검증하려는 이 테스트의 의도와 어긋난다.
    """
    store = CatalogArtifactStore()
    stale = CatalogArtifact(product_id=99, search_doc="stale", embedding=[0.0, 1.0])
    store.upsert(stale)
    changes = [_change(1, name="A"), _change(2, name="B-실패"), _change(3, name="C")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=["B-실패"])
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    result = await run_artifacts_batch(
        fetch=fetch,
        llm=llm,
        embed=_embed,
        store=store,
        settings=settings,
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

    [T6, PR 리뷰 라운드 4] 기본 예산(``artifacts_batch_content_retry_cycles=1``)에서 1선
    콘텐츠 실패는 이 시점에 격리가 아니라 재시도 큐 등재로 확정된다 — ``failed``==0,
    ``retry_pending``==1 이 기본 조합의 실제 결과다.
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
    assert result.failed == 0
    assert result.retry_pending == 1
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


# ── 라운드 4(#325 R4, PR #399 Claude 리뷰 2차 대응): 상품별 연속 실패 스트릭 → 시간 유계 격리 ──
#
# 리뷰 지적 2건(둘 다 타당): (1) artifacts_batch.py:273 — 특정 상품에서 결정적으로 재현되는
# poison 타임아웃은 재시도를 다 써도 격리되지 않고 매 주기 같은 자리에서 영원히 실패한다.
# (2) artifacts_batch.py:172 — _finish_change(embed·upsert) 실패를 무조건 인프라로 규정했지만
# 실제로는 그 상품 하나의 콘텐츠 문제(embedding_meta_complete CHECK 위반, 콘텐츠 필터 등)일
# 수 있어 같은 커서에서 영구히 막힌다. 광역 장애와 항목 고유 실패는 단일 주기 관측만으로는
# 구별할 수 없으므로, 같은 상품의 "주기를 가로지르는" 연속 실패 횟수로 시간 유계를 건다
# (artifacts_batch_item_dead_letter_cycles, 기본 3).


async def test_drain_embed_poison_propagates_then_isolates_at_cycle_limit(caplog):
    """embed 실패 poison — 상한(기본 3) 전 두 번은 전파, 세 번째 주기에 격리(리뷰 코멘트 2)."""
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()
    assert settings.artifacts_batch_item_dead_letter_cycles == 3  # 기본값 전제 명시

    for _ in range(settings.artifacts_batch_item_dead_letter_cycles - 1):
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch,
                llm=_EnrichLLM(),
                embed=_FailingEmbed(),
                store=store,
                settings=settings,
            )
        assert store.get_cursor() == "checkpoint"
        assert store.get(1) is None

    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
        )

    assert result.failed == 1
    assert result.processed == 0
    assert store.get_cursor() == "c1"
    assert store.get(1) is None
    assert "product_id=1" in caplog.text
    assert "streak=3/3" in caplog.text


async def test_drain_upsert_poison_propagates_then_isolates_at_cycle_limit(caplog):
    """store.upsert 실패 poison — 상한 전 두 번은 전파, 세 번째 주기에 격리(리뷰 코멘트 2)."""
    store = _FailingUpsertStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()

    for _ in range(settings.artifacts_batch_item_dead_letter_cycles - 1):
        with pytest.raises(RuntimeError, match="catalog store down"):
            await run_artifacts_batch(
                fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"

    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
        )

    assert result.failed == 1
    assert store.get_cursor() == "c1"
    assert store.get(1) is None
    assert "product_id=1" in caplog.text
    assert "streak=3/3" in caplog.text


async def test_drain_timeout_poison_propagates_then_isolates_at_cycle_limit(caplog):
    """타임아웃 poison — 특정 상품에서 결정적으로 재현되는 타임아웃도 상한 전엔 전파, 상한에서
    격리된다(리뷰 코멘트 1). 수정 전에는 이 시나리오가 매 주기 같은 자리에서 영원히 실패했다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()
    llm = _TimeoutLLM()

    for _ in range(settings.artifacts_batch_item_dead_letter_cycles - 1):
        with pytest.raises(TimeoutError):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"

    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert result.failed == 1
    assert store.get_cursor() == "c1"
    assert store.get(1) is None
    assert "product_id=1" in caplog.text
    assert "streak=3/3" in caplog.text


async def test_drain_transient_infra_failure_does_not_isolate_and_resets_streak():
    """일시적 장애(1회 실패 후 정상)는 격리되지 않고 커서가 전진하며, 스트릭도 리셋돼 이후 다시
    실패해도 3회차가 아니라 1회차부터 다시 센다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="가끔실패")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()

    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
        )
    assert store.get_cursor() == "checkpoint"

    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
    )
    assert result.processed == 1
    assert result.failed == 0
    assert store.get_cursor() == "c1"
    assert store.get(1) is not None

    # 스트릭이 리셋됐으므로 다시 실패해도 상한 미만(2회)은 전파돼야 한다 — 3회차가 아니라
    # 1회차부터 다시 센다는 것이 이 테스트의 핵심.
    store.set_cursor("checkpoint2")
    for _ in range(settings.artifacts_batch_item_dead_letter_cycles - 1):
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch,
                llm=_EnrichLLM(),
                embed=_FailingEmbed(),
                store=store,
                settings=settings,
            )
        assert store.get_cursor() == "checkpoint2"

    result2 = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
    )
    assert result2.failed == 1
    assert store.get_cursor() == "c1"


async def test_drain_item_failure_streak_is_per_product():
    """스트릭은 상품별로 독립 — A 상품이 2회 실패한 상태에서 B 상품이 처음 실패하면 B 는 전파된다."""
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    change_a = _change(1, name="A")

    async def fetch_a(cursor, limit):
        return ProductChangesPage(items=[change_a], next_cursor="ca", has_more=False)

    settings = get_settings()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch_a,
                llm=_EnrichLLM(),
                embed=_FailingEmbed(),
                store=store,
                settings=settings,
            )
    assert store.get_cursor() == "checkpoint"

    change_b = _change(2, name="B")

    async def fetch_b(cursor, limit):
        return ProductChangesPage(items=[change_b], next_cursor="cb", has_more=False)

    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(
            fetch=fetch_b, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
        )
    # B 는 이번이 첫 실패(streak=1 < 3)라 전파된다 — A 의 스트릭과 섞이지 않는다.
    assert store.get_cursor() == "checkpoint"
    assert store.get(2) is None


# ── 라운드 5(#325 R5, PR #399 Claude 리뷰 3차 대응): 3선(비율 가드)에도 시간 유계를 건다 ──
#
# 리뷰 지적(타당): R4 의 시간 유계는 2선(타임아웃·embed·store 전파)에만 걸려 있고 3선(비율
# 가드)에는 없었다. 특정 카테고리 상품들의 프롬프트 회귀로 enrich 내용 실패가 다건 동시
# 발생하면 1선이 매 주기 즉시 격리해(스트릭을 쌓지 않고 pop) 2선 상한이 영원히 걸리지 않고,
# 페이지 실패율은 매 주기 똑같이 임계를 넘어 PageFailureThresholdExceeded 가 반복돼 커서가
# 전진하지 않는다 — 같은 페이지가 무기한 재조회된다("stuck-batch 를 시간 유계로 닫는다"는
# 목표가 3선에는 적용되지 않아, 3선이 원래 잡으려던 바로 그 케이스에서 #325 증상이 재현).
# 아래는 같은 커서에서 비율 가드가 연속 발동한 횟수로 3선도 시간 유계를 거는 것을 고정한다.


async def test_drain_page_failure_threshold_repeats_then_advances_at_cycle_limit(caplog):
    """비율 가드 반복 발동 → 상한(기본 3)에서 커서 전진(#325 R5 리뷰 시나리오 그대로).

    표본(5) ≥ min_sample(5) 이고 전부 1선(enrich 내용 실패)인 페이지를 반복 실행 → 1·2회차는
    PageFailureThresholdExceeded + 커서 미전진, 3회차는 예외 없이 커서 전진 + ERROR 로그.

    content_retry_cycles=0 — 이 테스트는 3선(페이지 비율 가드) 자체를 검증하며, 1선 항목이
    #421 재시도 큐로 새지 않고 매 주기 결정적으로 페이지 비율에 반영되게 고정한다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    names = [f"실패{i}" for i in range(5)]
    changes = [_change(i + 1, name=name) for i, name in enumerate(names)]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=names)
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    assert settings.artifacts_batch_page_failure_max_cycles == 3  # 기본값 전제 명시

    for _ in range(settings.artifacts_batch_page_failure_max_cycles - 1):
        with pytest.raises(_batch.PageFailureThresholdExceeded):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"

    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert result.failed == 5
    assert store.get_cursor() == "c1"
    assert "cursor=checkpoint" in caplog.text
    assert "streak=3/3" in caplog.text


async def test_drain_page_failure_streak_resets_when_cycle_breaks():
    """연속이 끊기면 카운터가 리셋된다 — 다시 임계 초과가 나도 3회차가 아니라 1회차부터 다시
    센다(#325 R5).
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    fail_names = [f"실패{i}" for i in range(5)]
    fail_changes = [_change(i + 1, name=name) for i, name in enumerate(fail_names)]
    ok_names = [f"성공{i}" for i in range(5)]
    ok_changes = [_change(i + 100, name=name) for i, name in enumerate(ok_names)]

    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # 2회차만 정상 페이지 — next_cursor 를 같은 "checkpoint" 로 되돌려 3회차가 같은
            # 커서에서 다시 임계 초과를 재현하게 한다(카운터가 리셋됐는지가 이 테스트의 핵심).
            return ProductChangesPage(items=ok_changes, next_cursor="checkpoint", has_more=False)
        return ProductChangesPage(items=fail_changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=fail_names)
    settings = get_settings()

    # 1회차: 임계 초과 → 전파, 커서 "checkpoint" 카운터=1.
    with pytest.raises(_batch.PageFailureThresholdExceeded):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )
    assert store.get_cursor() == "checkpoint"

    # 2회차: 정상 페이지 → 전진 + 카운터 리셋(연속만 세므로).
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result.processed == 5
    assert store.get_cursor() == "checkpoint"

    # 3회차: 다시 임계 초과 — 리셋됐으므로 상한(3)이 아니라 1회차부터 다시 세어 여전히 전파된다.
    with pytest.raises(_batch.PageFailureThresholdExceeded):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )
    assert store.get_cursor() == "checkpoint"


async def test_drain_page_failure_streak_is_per_cursor():
    """페이지 실패 연속 발동 카운터는 커서별로 독립적이다 — 커서 A 가 2회 발동한 상태에서
    커서 B 가 처음 발동하면 B 는 예외를 던진다(#325 R5).
    """
    store_a = CatalogArtifactStore()
    store_a.set_cursor("A")
    names_a = [f"A실패{i}" for i in range(5)]
    changes_a = [_change(i + 1, name=name) for i, name in enumerate(names_a)]

    async def fetch_a(cursor, limit):
        return ProductChangesPage(items=changes_a, next_cursor="Ac1", has_more=False)

    llm_a = _FlakyLLM(always_fail=names_a)
    settings = get_settings()

    for _ in range(settings.artifacts_batch_page_failure_max_cycles - 1):
        with pytest.raises(_batch.PageFailureThresholdExceeded):
            await run_artifacts_batch(
                fetch=fetch_a, llm=llm_a, embed=_embed, store=store_a, settings=settings
            )
    assert store_a.get_cursor() == "A"

    store_b = CatalogArtifactStore()
    store_b.set_cursor("B")
    names_b = [f"B실패{i}" for i in range(5)]
    changes_b = [_change(i + 1000, name=name) for i, name in enumerate(names_b)]

    async def fetch_b(cursor, limit):
        return ProductChangesPage(items=changes_b, next_cursor="Bc1", has_more=False)

    llm_b = _FlakyLLM(always_fail=names_b)

    # B 는 이번이 첫 발동(streak=1 < 상한)이라 전파된다 — A 의 카운터와 섞이지 않는다.
    with pytest.raises(_batch.PageFailureThresholdExceeded):
        await run_artifacts_batch(
            fetch=fetch_b, llm=llm_b, embed=_embed, store=store_b, settings=settings
        )
    assert store_b.get_cursor() == "B"


async def test_drain_hidden_delete_failure_still_propagates_unbounded():
    """HIDDEN 삭제 실패는 3선 시간 유계 대상이 아니다 — 상한 횟수보다 더 반복해도 매번
    전파되고 커서는 미전진(api-spec §4.8 fail-closed 규약, #325 R5).
    """

    class _FailingDeleteStore(CatalogArtifactStore):
        def delete(self, product_id):
            raise RuntimeError("delete boom")

    store = _FailingDeleteStore()
    store.set_cursor("checkpoint")

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, status="HIDDEN")], next_cursor="c1", has_more=False
        )

    settings = get_settings()
    for _ in range(settings.artifacts_batch_page_failure_max_cycles + 2):
        with pytest.raises(RuntimeError, match="delete boom"):
            await run_artifacts_batch(
                fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"


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


# ── 라운드 6(#325 R6, PR #399 Claude 리뷰 4차 대응): 콘텐츠 실패 화이트리스트 ──
#
# 리뷰 지적(타당): 예전 판정("타임아웃이면 2선, 아니면 1선")은 블랙리스트라 is_timeout_error
# 가 모르는 예외(openai.RateLimitError 429·APIConnectionError·InternalServerError 5xx 등
# 흔한 일시적 인프라 장애)가 전부 콘텐츠 실패로 오분류돼 첫 주기에 곧바로 영구 격리됐다 —
# R4·R5 의 시간 유계 보호를 통째로 우회하는 경로였다. _is_enrichment_content_failure 로
# 화이트리스트를 도입해 "모르면 2선"을 기본값으로 뒤집는다.


class _RaisingLLM:
    """호출마다 주어진 예외를 내는 fake — 콘텐츠 실패 화이트리스트 판정 테스트 전용(#325 R6)."""

    def __init__(self, exc_factory):
        self._exc_factory = exc_factory
        self.calls = 0

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        self.calls += 1
        raise self._exc_factory()


class _TextLLM:
    """설정된 원문을 그대로 반환하는 fake — extract_json 이 실제로 던지는 경로를 재현한다."""

    def __init__(self, text: str):
        self._text = text
        self.calls = 0

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        self.calls += 1
        return self._text


class _FakeRateLimitError(Exception):
    """openai.RateLimitError(429) 자리를 대신하는 SDK 미의존 더미(#325 R6)."""


class _FakeConnectionError(Exception):
    """openai.APIConnectionError/InternalServerError(5xx) 자리를 대신하는 SDK 미의존 더미."""


def _wrapped(cause_cls):
    """provider SDK 예외를 ``raise LLMError(...) from exc`` 로 감싸는 실제 규약을 재현한다."""

    def factory():
        from app.core.llm import LLMError

        try:
            raise cause_cls("infra hiccup")
        except cause_cls as exc:
            raise LLMError("boom") from exc

    return factory


async def test_drain_rate_limit_propagates_then_isolates_at_cycle_limit(caplog):
    """[#325 R6 핵심 회귀] rate limit 계열(LLMError from 미지 예외)은 화이트리스트 밖이라
    2선이다 — 1회차엔 예외 전파 + 커서 미전진, 상한(기본 3) 주기째에야 격리한다. 예전
    블랙리스트 판정에서는 1회차부터 즉시(영구) 격리됐다(리뷰 시나리오 그대로).
    """
    from app.core.llm import LLMError

    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()
    assert settings.artifacts_batch_item_dead_letter_cycles == 3  # 기본값 전제 명시
    llm = _RaisingLLM(_wrapped(_FakeRateLimitError))

    for _ in range(settings.artifacts_batch_item_dead_letter_cycles - 1):
        with pytest.raises(LLMError):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"
        assert store.get(1) is None

    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert result.failed == 1
    assert store.get_cursor() == "c1"
    assert store.get(1) is None
    assert "product_id=1" in caplog.text
    assert "streak=3/3" in caplog.text


async def test_drain_connection_error_not_isolated_immediately():
    """연결 오류·5xx 계열(LLMError from 미지 예외)도 같은 형태로 화이트리스트 밖 — 1회차엔
    격리 없이 그대로 전파하고 커서는 미전진한다.
    """
    from app.core.llm import LLMError

    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="문제상품")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _RaisingLLM(_wrapped(_FakeConnectionError))
    settings = get_settings()
    with pytest.raises(LLMError):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert llm.calls == settings.enrichment_item_attempts
    assert store.get_cursor() == "checkpoint"
    assert store.get(1) is None


async def test_drain_json_parse_failure_isolates_immediately():
    """extract_json 의 실제 json.loads 실패(원인 ValueError/JSONDecodeError) — 화이트리스트
    1선, 1주기째에 격리+커서 전진(#325 R6).

    [T6, PR 리뷰 라운드 4] ``content_retry_cycles=0`` 을 명시해 즉시 격리를 강제한다 —
    기본값(1)이면 이 시점엔 재시도 큐 등재일 뿐이라 이 테스트 이름의 "즉시"와 어긋난다.
    """
    store = CatalogArtifactStore()
    changes = [_change(1, name="깨진JSON")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _TextLLM("{이건 유효한 JSON 이 아님}")
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result.failed == 1
    assert result.processed == 0
    assert store.get_cursor() == "c1"
    assert store.get(1) is None


async def test_drain_bare_llm_error_isolates_immediately():
    """extract_json 이 "JSON 을 찾지 못함"으로 내는 원인 없는 LLMError — 화이트리스트 1선,
    1주기째에 격리+커서 전진(#325 R6). 가짜 예외가 아니라 실제 extract_json 경로를 그대로
    통과시킨다(LLM 이 JSON 이 아닌 평문을 반환).

    [T6, PR 리뷰 라운드 4] ``content_retry_cycles=0`` 을 명시해 즉시 격리를 강제한다(위와
    같은 이유).
    """
    store = CatalogArtifactStore()
    changes = [_change(1, name="평문응답")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _TextLLM("죄송합니다, JSON 을 만들 수 없어요.")
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result.failed == 1
    assert result.processed == 0
    assert store.get_cursor() == "c1"
    assert store.get(1) is None


async def test_drain_output_length_error_isolates_immediately():
    """[#325 원 사례] openai.LengthFinishReasonError(출력 예산 소진) — 화이트리스트 1선,
    1주기째에 격리+커서 전진. OpenAILLM.complete 이 SDK 예외를
    ``raise LLMError(str(exc)) from exc`` 로 감싸는 실제 규약을 재현한다.

    [T6, PR 리뷰 라운드 4] ``content_retry_cycles=0`` 을 명시해 즉시 격리를 강제한다(위와
    같은 이유).
    """
    openai = pytest.importorskip("openai")
    from types import SimpleNamespace

    from app.core.llm import LLMError

    store = CatalogArtifactStore()
    changes = [_change(1, name="긴응답")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    def factory():
        inner = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
        raise LLMError(str(inner)) from inner

    llm = _RaisingLLM(factory)
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result.failed == 1
    assert result.processed == 0
    assert store.get_cursor() == "c1"
    assert store.get(1) is None


async def test_drain_llm_not_configured_not_isolated_immediately():
    """LLMNotConfigured 는 LLMError 하위타입이지만 화이트리스트에서 명시적으로 제외돼 2선이다
    — 1회차엔 격리 없이 전파, 커서 미전진(#325 R6)."""
    from app.core.llm import LLMNotConfigured

    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    changes = [_change(1, name="구성오류")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _RaisingLLM(lambda: LLMNotConfigured("anthropic API key is not configured"))
    settings = get_settings()
    with pytest.raises(LLMNotConfigured):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert llm.calls == settings.enrichment_item_attempts
    assert store.get_cursor() == "checkpoint"
    assert store.get(1) is None


def test_is_enrichment_content_failure_direct():
    """``_is_enrichment_content_failure`` 자체를 직접 두 극단으로 고정한다 — 원인 없는
    LLMError(True) vs 완전히 무관한 예외(False). 나머지 경계(원인 타입별)는 위 _drain
    통합 테스트가 덮는다."""
    from app.core.llm import LLMError

    assert _batch._is_enrichment_content_failure(LLMError("JSON 못 찾음")) is True
    assert _batch._is_enrichment_content_failure(RuntimeError("모르는 실패")) is False


def test_is_enrichment_content_failure_excludes_llm_not_configured():
    """LLMNotConfigured 는 원인 없는 LLMError 형태여도 명시적으로 제외된다(#325 R6)."""
    from app.core.llm import LLMNotConfigured

    assert _batch._is_enrichment_content_failure(LLMNotConfigured("no key")) is False


# ── 이슈 #421: 1선 콘텐츠 실패 cross-cycle 재시도 예산 ──
#
# JSON 파싱 실패는 LLM 샘플링 노이즈(코드펜스 혼입 등)로도 나므로, 우연히 attempts 회 연속
# 실패한 정상 상품이 2선·3선과 달리 시간 유계 없이 첫 주기에 영구 격리되던 결함(#421)의 수정을
# 고정한다. 아래 테스트는 always_fail/fail_counts 로 만드는 실패가 전부 원인 없는 LLMError —
# 화이트리스트(#325 R6) 1선(콘텐츠 실패) 판정을 그대로 통과한다.


async def test_content_retry_recovers_on_next_cycle(caplog):
    """[#421 이슈 완료 조건] 1주기: 콘텐츠 실패 → 영구 격리 ERROR 없음, 커서 전진, 대기 등재.
    2주기: 같은 상품이 성공 → artifact 가 upsert 되고 recovered==1, 큐가 빈다.

    fetch 는 첫 호출에만 그 상품을 싣는다(F1·F2 PR 리뷰 라운드 1) — 실제로 Spring 은 같은
    변경분을 매 주기 다시 보내지 않으므로, 2주기의 회복은 재시도 패스(``_run_content_retry_pass``)
    가 해낸 것이어야 한다. 매 주기 같은 페이지가 오는 낡은 fetch 를 쓰면 2주기의 ``_drain``
    자체가(재시도 패스와 무관하게) 그 상품을 다시 처리해 회복시켜 버려 이 테스트가 검증하려는
    바(재시도 패스의 회복)를 가린다.
    """
    store = CatalogArtifactStore()
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(
                items=[_change(1, name="노이즈상품")], next_cursor="c1", has_more=False
            )
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings()
    llm = _FlakyLLM(fail_counts={"노이즈상품": settings.enrichment_item_attempts})

    with caplog.at_level("WARNING"):
        result1 = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    # [T6, PR 리뷰 라운드 4] failed==1 이 아니라 0 이 맞다 — 이 주기엔 격리가 "확정"되지
    # 않고 재시도 큐에 등재됐을 뿐이다(retry_pending==1·WARNING 으로 이미 드러난다).
    # 수정 전엔 재시도 큐 등재도 failed 를 올려, 아직 dead-letter ERROR 가 하나도 없는데
    # scheduler.py 가 "부분 실패 — dead-letter 로그 확인" ERROR 알람을 거짓으로 띄웠다.
    assert result1.failed == 0
    assert result1.processed == 0
    assert store.get(1) is None
    assert store.get_cursor() == "c1"
    assert "다음 주기 재시도 예약" in caplog.text
    assert "격리 기록 후 계속" not in caplog.text  # 영구 격리 ERROR 없음
    assert 1 in _batch._content_retry_queue
    assert result1.retry_pending == 1

    result2 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result2.recovered == 1
    assert result2.failed == 0
    assert store.get(1) is not None
    assert 1 not in _batch._content_retry_queue
    assert result2.retry_pending == 0


async def test_content_retry_converges_deterministically_after_budget_exhausted(caplog):
    """[#421] 매번 콘텐츠 실패 → 예산(기본 1) 소진 주기에 ERROR dead-letter 1회 + 큐에서 제거,
    그 다음 주기에는 재시도하지 않는다(LLM 호출 횟수로 고정)."""
    store = CatalogArtifactStore()
    change = _change(1, name="영구노이즈")
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(items=[change], next_cursor="c1", has_more=False)
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings()
    assert settings.artifacts_batch_content_retry_cycles == 1  # 기본값 전제 명시
    llm = _FlakyLLM(always_fail=["영구노이즈"])

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 이 주기엔 재시도 큐 등재만 있고 격리는 아직 확정되지 않았으므로
    # failed==0 이 맞다(예산 소진 격리는 다음 주기 재시도 패스에서 일어난다).
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue
    calls_after_cycle1 = llm.calls_by_name["영구노이즈"]
    assert calls_after_cycle1 == settings.enrichment_item_attempts

    with caplog.at_level("ERROR"):
        result2 = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )
    assert result2.failed == 1  # 재시도 패스가 예산을 소진해 이 주기에 격리가 확정된다
    assert result2.recovered == 0
    assert 1 not in _batch._content_retry_queue
    assert "재시도 예산 소진" in caplog.text
    calls_after_cycle2 = llm.calls_by_name["영구노이즈"]
    assert calls_after_cycle2 == calls_after_cycle1 + settings.enrichment_item_attempts

    result3 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result3.failed == 0
    assert result3.recovered == 0
    assert llm.calls_by_name["영구노이즈"] == calls_after_cycle2  # 더 이상 호출되지 않는다


async def test_content_retry_cycles_zero_preserves_immediate_isolation():
    """[#421 회귀 탈출구] artifacts_batch_content_retry_cycles=0 → 종전대로 첫 주기 즉시 영구
    격리 — 큐에 등재되지 않는다."""
    store = CatalogArtifactStore()
    changes = [_change(1, name="즉시격리")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    llm = _FlakyLLM(always_fail=["즉시격리"])
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )

    assert result.failed == 1
    assert result.recovered == 0
    assert result.retry_pending == 0
    assert 1 not in _batch._content_retry_queue
    assert store.get(1) is None
    assert store.get_cursor() == "c1"


async def test_content_retry_queue_dropped_when_product_goes_hidden():
    """[#421][F1 강화, PR 리뷰 라운드 1] 대기 중인 product 가 HIDDEN 으로 오면 재시도 패스가
    그것을 다시 upsert 하지 않는다(유령 상품 방지). F1(재시도는 _drain 정상 완료 뒤에만)
    적용 후에는 _drain 자신이 HIDDEN 변경분을 처리하며 큐 항목을 먼저 팝하므로, 재시도
    패스가 도는 시점엔 이미 큐에 없다 — 최종 상태만이 아니라 **upsert 호출 자체가 한 번도
    없었음**을 직접 검증해 "일단 살렸다가 삭제로 지워지는" 중간 복원 가능성까지 배제한다
    (기존 테스트는 최종 상태만 봐서 이를 못 잡는다는 리뷰 지적)."""
    upsert_calls: list[int] = []

    class _SpyStore(CatalogArtifactStore):
        def upsert(self, artifact):
            upsert_calls.append(artifact.product_id)
            super().upsert(artifact)

    store = _SpyStore()
    on_sale_change = _change(1, name="사라질상품")
    hidden_change = _change(1, status="HIDDEN")
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(items=[on_sale_change], next_cursor="c1", has_more=False)
        return ProductChangesPage(items=[hidden_change], next_cursor="c2", has_more=False)

    settings = get_settings()
    llm = _FlakyLLM(fail_counts={"사라질상품": settings.enrichment_item_attempts})

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue

    result2 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result2.hidden == 1
    assert result2.recovered == 0
    assert store.get(1) is None
    assert 1 not in _batch._content_retry_queue
    assert 1 not in upsert_calls  # [F1 강화] 유령 상품 생성 자체가 한 번도 없었다(중간 상태 포함)


async def test_content_retry_pass_only_runs_after_drain_completes_successfully(caplog):
    """[F1 blocker 회귀, PR 리뷰 라운드 1] 대기 중 상품이 있는 주기에 fetch 가 실패해 _drain
    이 중단되면 재시도 패스가 아예 돌지 않는다 — 그 사이 HIDDEN 으로 바뀌었을 수 있는 상품을
    낡은 페이로드로 되살리면 §4.8 fail-closed 를 정면으로 뚫는다. LLM 은 이번엔 성공하도록
    설정해 두므로, "재시도 패스가 돌았다면 회복했을 것"이라는 사실로 "안 돌았다"를 증명한다.
    다음 주기에 _drain 이 정상 완료하면 그제서야 재시도 패스가 돌아 회복된다 — 호출 횟수로
    "_drain 정상 완료 주기에만 재시도" 를 고정한다.
    """
    store = CatalogArtifactStore()
    cycle = 0

    async def fetch(cursor, limit):
        nonlocal cycle
        cycle += 1
        if cycle == 1:
            return ProductChangesPage(
                items=[_change(1, name="포이즌")], next_cursor="c1", has_more=False
            )
        if cycle == 2:
            raise RuntimeError("fetch network glitch")
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings()
    llm = _FlakyLLM(fail_counts={"포이즌": settings.enrichment_item_attempts})

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue
    calls_after_cycle1 = llm.calls_by_name["포이즌"]

    with pytest.raises(RuntimeError, match="fetch network glitch"):
        await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    # _drain 이 fetch 실패로 중단됐으므로 재시도 패스가 돌지 않는다 — LLM 은 이제 성공하도록
    # 설정돼 있지만(fail_counts 소진) 그 사실이 반영되면 안 된다.
    assert store.get(1) is None
    assert 1 in _batch._content_retry_queue
    assert llm.calls_by_name["포이즌"] == calls_after_cycle1  # 추가 호출 없음

    result3 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result3.recovered == 1
    assert store.get(1) is not None
    assert 1 not in _batch._content_retry_queue
    assert llm.calls_by_name["포이즌"] > calls_after_cycle1  # 이제서야 재시도 패스가 호출됐다


def test_content_retry_queue_evicts_oldest_at_cap(caplog):
    """[#421] 큐 상한(``_CONTENT_RETRY_MAX_PENDING``) 도달 시 가장 오래된 항목을 축출하고
    ERROR dead-letter 로 남긴다 — 조용한 유실 금지."""
    for i in range(_batch._CONTENT_RETRY_MAX_PENDING):
        _batch._enqueue_content_retry(_change(i, name=f"p{i}"))
    assert len(_batch._content_retry_queue) == _batch._CONTENT_RETRY_MAX_PENDING

    with caplog.at_level("ERROR"):
        _batch._enqueue_content_retry(_change(_batch._CONTENT_RETRY_MAX_PENDING, name="new"))

    assert len(_batch._content_retry_queue) == _batch._CONTENT_RETRY_MAX_PENDING
    assert 0 not in _batch._content_retry_queue  # 가장 오래된(product_id=0) 축출
    assert _batch._CONTENT_RETRY_MAX_PENDING in _batch._content_retry_queue
    assert "product_id=0" in caplog.text
    assert "상한" in caplog.text


async def test_full_rebuild_clears_content_retry_queue():
    """[#421] full_rebuild=True 는 재시도 패스를 돌리지 않고 큐를 비운다 — 전체 재구축이 모든
    항목을 어차피 다시 처리한다."""
    store = CatalogArtifactStore()
    _batch._content_retry_queue[1] = _batch._PendingContentRetry(
        change=_change(1), retry_attempts=0
    )

    async def fetch(cursor, limit):
        return ProductChangesPage(items=[], next_cursor=None, has_more=False)

    await run_artifacts_batch(
        fetch=fetch,
        llm=_EnrichLLM(),
        embed=_embed,
        store=store,
        settings=get_settings(),
        full_rebuild=True,
    )

    assert _batch._content_retry_queue == {}


async def test_content_retry_pass_failure_does_not_kill_batch(monkeypatch, caplog):
    """[#421] 재시도 패스 실패는 절대 전파하지 않는다 — 예외가 나도 본 배치(_drain)가 정상
    진행하고 커서가 전진한다."""
    store = CatalogArtifactStore()
    _batch._content_retry_queue[1] = _batch._PendingContentRetry(
        change=_change(1), retry_attempts=0
    )

    async def boom(**kwargs):
        raise RuntimeError("retry pass exploded")

    monkeypatch.setattr(_batch, "_run_content_retry_pass", boom)

    changes = [_change(2, name="정상")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    with caplog.at_level("ERROR"):
        result = await run_artifacts_batch(
            fetch=fetch, llm=_EnrichLLM(), embed=_embed, store=store, settings=get_settings()
        )

    assert result.processed == 1
    assert store.get(2) is not None
    assert store.get_cursor() == "c1"
    assert "재시도 패스 실패" in caplog.text


# ── 이슈 #416: 2선·3선 연속 실패 스트릭의 cross-cycle 영속화(스토어 위임) ──
#
# 프로세스가 수렴 창(기본 3주기 ≈ 15분)보다 자주 재시작되면 프로세스 메모리 스트릭이 매번
# 0으로 리셋돼 poison 상품이 dead-letter 상한에 영영 도달하지 못하던 결함(#416)의 수정을
# 고정한다. 아래 테스트는 매 주기 CatalogArtifactStore 인스턴스를 새로 만들어(프로세스 재시작
# 모사) 스트릭 저장소(FailureStreakTable)만 공유하는 가짜 영속 스토어로 재현한다.


async def test_item_streak_converges_across_simulated_process_restarts():
    """[#416 이슈 완료 조건] 매 주기 스토어 인스턴스를 새로 만들어도(재시작 모사) 스트릭
    저장소만 영속되면 dead_letter_cycles 주기째에 격리된다."""
    from app.pipelines.artifact_store import FailureStreakTable

    shared_streaks = FailureStreakTable()
    changes = [_change(1, name="포이즌")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()

    def new_store():
        s = CatalogArtifactStore()
        s._failure_streaks = shared_streaks  # "재시작"해도 스트릭 저장소만은 살아남는다(pg 모사)
        return s

    for _ in range(settings.artifacts_batch_item_dead_letter_cycles - 1):
        store = new_store()
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
            )
        assert store.get(1) is None

    store = new_store()
    result = await run_artifacts_batch(
        fetch=fetch, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
    )
    assert result.failed == 1
    assert store.get(1) is None


async def test_item_streak_never_converges_if_reset_every_restart_regression_guard():
    """대조군(#416 회귀 방지) — 스트릭까지 매 주기 새로 만들면(구 프로세스 메모리 동작) 영원히
    격리되지 않는다(연속 배포·크래시 루프 조건에서 재현되던 stuck-batch)."""
    changes = [_change(1, name="포이즌")]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    settings = get_settings()

    for _ in range(settings.artifacts_batch_item_dead_letter_cycles + 5):
        store = CatalogArtifactStore()  # 매 주기 완전히 새 스토어 — 스트릭도 함께 리셋(구 동작)
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch, llm=_EnrichLLM(), embed=_FailingEmbed(), store=store, settings=settings
            )
        assert store.get(1) is None  # dead_letter_cycles 에 영영 도달하지 못해 격리되지 않는다


async def test_page_streak_converges_across_simulated_process_restarts():
    """[#416] 3선(페이지 비율 가드) 연속 발동 카운터도 스토어 인스턴스 교체를 가로질러
    누적된다 — 2선과 같은 시간 유계 원리(#325 R5)가 재시작에도 유지된다."""
    from app.pipelines.artifact_store import FailureStreakTable

    shared_streaks = FailureStreakTable()
    names = [f"실패{i}" for i in range(5)]
    changes = [_change(i + 1, name=name) for i, name in enumerate(names)]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    # content_retry_cycles=0 — 이 테스트는 3선 자체를 검증하며, 1선 항목이 #421 재시도 큐로
    # 새지 않고 매 주기 결정적으로 페이지 비율에 반영되게 고정한다.
    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 0})
    llm = _FlakyLLM(always_fail=names)

    def new_store():
        s = CatalogArtifactStore()
        s._failure_streaks = shared_streaks
        s.set_cursor("checkpoint")
        return s

    for _ in range(settings.artifacts_batch_page_failure_max_cycles - 1):
        store = new_store()
        with pytest.raises(_batch.PageFailureThresholdExceeded):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
            )
        assert store.get_cursor() == "checkpoint"

    store = new_store()
    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result.failed == 5
    assert store.get_cursor() == "c1"


async def test_full_rebuild_records_streaks_on_real_store_not_temp_store():
    """[#416] full_rebuild 에서도 스트릭은 임시 스토어(work)가 아니라 진짜 store 에 기록된다
    — rebuild() 내부 임시 CatalogArtifactStore 는 replace_all 로 버려지므로, 스트릭을 거기
    두면 다음 관측 시 증발한다. 2선(전파) 경로 실패로 검증한다 — 1선(내용 실패)은 성공 시
    스트릭을 clear 하므로 이 구분에 적합하지 않다.
    """
    from app.pipelines.artifact_store import FAILURE_STREAK_KIND_ITEM

    store = CatalogArtifactStore()
    settings = get_settings()

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(9, name="poison")], next_cursor="c1", has_more=False
        )

    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(
            fetch=fetch,
            llm=_EnrichLLM(),
            embed=_FailingEmbed(),
            store=store,
            settings=settings,
            full_rebuild=True,
        )

    # 스트릭이 진짜 store 에 남았다면(임시 work 가 아니라) 직접 bump 시 1이 아니라 2부터
    # 이어서 센다 — work 에 기록됐다면 이 store 는 처음 보는 값이라 1이 나왔을 것이다.
    assert store.bump_failure_streak(FAILURE_STREAK_KIND_ITEM, "9", ttl_s=3600.0) == 2


async def test_pg_store_failure_streak_api_falls_back_on_exception(monkeypatch, caplog):
    """[#416] PgCatalogArtifactStore 의 3개 실패 스트릭 메서드는 DB 오류 시 인메모리 폴백으로
    위임하고 예외를 밖으로 내지 않는다 — 폴백 동작이 곧 종전(프로세스 메모리) 동작이라
    테이블 부재·pg 순단에도 배치가 죽지 않는다. 실 커넥션 풀 없이 스트릭 관련 인스턴스
    필드만 갖춘 최소 구성으로 검증한다(DB 접속 자체는 이 테스트의 관심사가 아니다)."""
    import threading

    from app.pipelines.artifact_store import FailureStreakTable
    from app.pipelines.pg_artifact_store import PgCatalogArtifactStore

    store = object.__new__(PgCatalogArtifactStore)  # __init__(dsn) 의 실 커넥션 없이 구성
    store._failure_streak_schema_ready = False
    store._failure_streak_schema_lock = threading.Lock()
    store._failure_streak_fallback = FailureStreakTable()
    store._failure_streak_fallback_warned = False

    def boom_ensure():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_ensure_failure_streak_schema", boom_ensure)

    with caplog.at_level("WARNING"):
        assert store.bump_failure_streak("item", "1", ttl_s=60) == 1
        assert store.bump_failure_streak("item", "1", ttl_s=60) == 2
        store.clear_failure_streak("item", "1")
        assert store.bump_failure_streak("item", "1", ttl_s=60) == 1
        assert store.purge_stale_failure_streaks(ttl_s=60) == 0

    assert "인메모리 폴백" in caplog.text


def test_default_settings_failure_streak_ttl_exceeds_convergence_window():
    """[공통 #14 기본값 정합] 스트릭 TTL(#416) 기본값이 수렴 창(dead_letter_cycles ×
    catalog_batch_interval_s)보다 길다 — 짧으면 정상 재시작 빈도에서도 "연속" 판정이 끊겨
    dead-letter 상한에 영영 도달하지 못하는 회귀가 조용히 재현된다."""
    settings = get_settings()
    convergence_window_s = (
        settings.artifacts_batch_item_dead_letter_cycles * settings.catalog_batch_interval_s
    )
    assert settings.artifacts_batch_failure_streak_ttl_s > convergence_window_s


# ── PR 리뷰 라운드 1 — F2: 재등재 증폭 방지(F1 이 대부분을 없앤다는 것을 실측으로 고정) ──


async def test_content_retry_default_settings_no_amplification_at_page_failure_limit():
    """[F2] 출하 기본값(content_retry_cycles=1) 그대로 5건 콘텐츠 실패 페이지가 3선 상한까지
    가는 시나리오에서 주기별 LLM 호출 수가 고정된다 — F1(재시도는 _drain 정상 완료 뒤에만 +
    이번 실행 등재분 skip)이 재시도 패스의 같은 주기 중복 처리를 없애 "10 → 30 → 50" 식
    증폭이 사라졌음을 실측한다. 기존 3선 테스트들은 전부 ``content_retry_cycles=0`` 으로
    덮어써 이 기본값 조합을 검증하지 않았다(리뷰 지적) — 이 테스트가 그 기본값 판이다.
    """
    store = CatalogArtifactStore()
    names = [f"실패{i}" for i in range(5)]
    changes = [_change(i + 1, name=name) for i, name in enumerate(names)]

    async def fetch(cursor, limit):
        return ProductChangesPage(items=changes, next_cursor="c1", has_more=False)

    llm = _FlakyLLM(always_fail=names)
    settings = get_settings()
    assert settings.artifacts_batch_content_retry_cycles == 1  # 기본값 전제 명시
    assert settings.artifacts_batch_page_failure_max_cycles == 3  # 기본값 전제 명시

    expected_calls_per_cycle = len(names) * settings.enrichment_item_attempts  # 5 × 2 = 10
    per_cycle_calls: list[int] = []
    prev_total = 0

    for _ in range(settings.artifacts_batch_page_failure_max_cycles - 1):
        with pytest.raises(_batch.PageFailureThresholdExceeded):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
            )
        total = sum(llm.calls_by_name.values())
        per_cycle_calls.append(total - prev_total)
        prev_total = total

    result = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    total = sum(llm.calls_by_name.values())
    per_cycle_calls.append(total - prev_total)

    # 매 주기 정확히 "5건 × attempts" 만큼만 호출된다 — 재시도 패스가 겹쳐 배가되지 않는다.
    assert per_cycle_calls == [expected_calls_per_cycle] * 3
    # [T6, PR 리뷰 라운드 4] 3선이 이 마지막 주기에 페이지를 강제로 격리·전진시키지만, 각
    # 항목은 이번 주기에 처음 콘텐츠 실패를 겪은 것이라(직전 주기의 T1 즉시 격리로 큐에서
    # 빠졌었다) 개별 항목의 격리는 아직 확정되지 않았다 — failed==0, retry_pending==5.
    # (api-spec §4.8 "1선인 경우 재시도 큐로 넘어갔다"가 바로 이 경우다.)
    assert result.failed == 0
    assert result.retry_pending == 5
    assert store.get_cursor() == "c1"


# ── PR 리뷰 라운드 1 — F3: HIDDEN 삭제 성공이 item 스트릭을 clear 하는지 ──


async def test_hidden_delete_success_clears_item_streak():
    """[F3] HIDDEN 삭제 성공은 그 항목의 정상 처리이므로 item 스트릭을 clear 한다 — 아니면
    영속화(TTL 기본 1h) 이후로 "ON_SALE 인프라 실패 2회 → HIDDEN 삭제 → 1시간 안에 재판매 →
    재판매 후 첫 실패가 과거 2와 합쳐져 3/3 으로 즉시 오격리" 가 가능해진다.
    """
    store = CatalogArtifactStore()
    store.set_cursor("checkpoint")
    settings = get_settings()

    async def fetch_fail(cursor, limit):
        return ProductChangesPage(items=[_change(1, name="A")], next_cursor="c1", has_more=False)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch_fail,
                llm=_EnrichLLM(),
                embed=_FailingEmbed(),
                store=store,
                settings=settings,
            )
    assert store.get_cursor() == "checkpoint"  # streak=2(<3) 라 아직 전파만 됨

    async def fetch_hidden(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, status="HIDDEN")], next_cursor="c2", has_more=False
        )

    result = await run_artifacts_batch(
        fetch=fetch_hidden, llm=_EnrichLLM(), embed=_embed, store=store, settings=settings
    )
    assert result.hidden == 1
    assert store.get_cursor() == "c2"

    # 재판매(같은 product_id 의 새 ON_SALE) — 스트릭이 clear 됐다면 이번 1회 실패는
    # streak=1<3 이라 전파(커서 미전진)돼야 한다. F3 이전이었다면 과거 2 와 합쳐져
    # streak=3/3 으로 즉시 격리되어(예외 없이) 커서가 "c3" 로 전진했을 것이다.
    async def fetch_resale(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, name="A-재판매")], next_cursor="c3", has_more=False
        )

    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(
            fetch=fetch_resale,
            llm=_EnrichLLM(),
            embed=_FailingEmbed(),
            store=store,
            settings=settings,
        )
    assert store.get_cursor() == "c2"  # 전파됐으므로 미전진(격리됐다면 "c3" 로 전진했을 것)


# ── PR 리뷰 라운드 1 — F4: 재시도 예산 카운터 불변식(N 회 정확히 재시도 후 격리) ──


async def test_content_retry_cycles_two_retries_exactly_twice():
    """[F4] ``artifacts_batch_content_retry_cycles=2`` 면 cross-cycle 재시도가 정확히 2회
    일어난 뒤에야 영구 격리된다 — 등재 시 ``retry_attempts=0`` 에서 시작해 실패마다 +1 하는
    불변식을 고정한다(수정 전에는 등재 시 1로 시작해 N≥2 에서 재시도가 N-1 회로 한 주기
    모자랐다)."""
    store = CatalogArtifactStore()
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(
                items=[_change(1, name="영구노이즈")], next_cursor="c1", has_more=False
            )
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings().model_copy(update={"artifacts_batch_content_retry_cycles": 2})
    llm = _FlakyLLM(always_fail=["영구노이즈"])

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue
    calls_after_1 = llm.calls_by_name["영구노이즈"]

    # cycle2: cross-cycle 재시도 1회차 — 실패하지만 예산(2) 미만이라 큐에 남는다.
    result2 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result2.failed == 0
    assert result2.recovered == 0
    assert 1 in _batch._content_retry_queue
    calls_after_2 = llm.calls_by_name["영구노이즈"]
    assert calls_after_2 == calls_after_1 + settings.enrichment_item_attempts

    # cycle3: cross-cycle 재시도 2회차 — 예산 소진, 이제서야 격리.
    result3 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result3.failed == 1
    assert 1 not in _batch._content_retry_queue
    calls_after_3 = llm.calls_by_name["영구노이즈"]
    assert calls_after_3 == calls_after_2 + settings.enrichment_item_attempts

    # cycle4: 이미 격리됐으므로 더 이상 호출되지 않는다.
    result4 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result4.failed == 0
    assert llm.calls_by_name["영구노이즈"] == calls_after_3


# ── PR 리뷰 라운드 2 — T1: enrichment 무관 필드만 갱신되는 poison 상품도 예산이 소진된다 ──


async def test_content_retry_budget_carries_over_when_enrichment_inputs_unchanged(caplog):
    """[T1] 가격·재고처럼 enrichment 와 무관한 필드(``updated_at``)만 매 주기 갱신되는 poison
    상품도 유계 주기 안에 dead-letter 된다(기본 예산 1). 수정 전에는 ``_drain`` 이 매 주기 그
    상품의 대기 항목을 pop 하고 ``retry_attempts=0`` 으로 재등재해 예산이 영원히 소진되지
    않았다 — 재시도 패스가 손도 대기 전에 다음 주기 새 변경분이 도착해 항상 리셋됐기 때문
    이다. 이 테스트는 수정 전 코드에서 반드시 실패한다(2주기가 지나도 큐에 남아 있다)."""
    store = CatalogArtifactStore()
    cycle = 0

    async def fetch(cursor, limit):
        nonlocal cycle
        cycle += 1
        # 매 주기 같은 enrichment 입력(name·description·category·brand·attributes)에
        # updated_at 만 갱신된 새 ProductChange — Spring 이 가격·재고 변경으로 이 product 를
        # 다시 보내는 실제 상황을 재현한다.
        change = ProductChange(
            product_id=1,
            status="ON_SALE",
            updated_at=f"2026-07-{20 + cycle:02d}T00:00:00Z",
            name="포이즌",
            description="설명",
            category="여행용품",
            brand="트래블",
            attributes={"방수": True},
        )
        return ProductChangesPage(items=[change], next_cursor=f"c{cycle}", has_more=False)

    settings = get_settings()
    assert settings.artifacts_batch_content_retry_cycles == 1  # 기본값 전제 명시
    llm = _FlakyLLM(always_fail=["포이즌"])

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue
    assert _batch._content_retry_queue[1].retry_attempts == 0

    with caplog.at_level("ERROR"):
        result2 = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
        )

    assert result2.failed == 1
    assert 1 not in _batch._content_retry_queue  # 이전 예산(0)을 이어받아 +1 → 곧바로 dead-letter
    assert "예산 소진" in caplog.text


async def test_content_retry_budget_resets_when_enrichment_inputs_change(caplog):
    """[T1] enrichment 가 실제로 쓰는 필드가 바뀌면 새 내용으로 취급해 예산을 0 부터 다시
    준다 — #421 의 원래 취지(우연히 연속 실패한 *정상* 상품을 첫 주기에 오격리하지 않는다)가
    이번 수정으로 깨지지 않았음을 고정한다."""
    store = CatalogArtifactStore()

    async def fetch_cycle1(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, name="콘텐츠A")], next_cursor="c1", has_more=False
        )

    async def fetch_cycle2(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, name="콘텐츠B")], next_cursor="c2", has_more=False
        )

    settings = get_settings()
    assert settings.artifacts_batch_content_retry_cycles == 1  # 기본값 전제 명시
    llm = _FlakyLLM(always_fail=["콘텐츠A", "콘텐츠B"])

    result1 = await run_artifacts_batch(
        fetch=fetch_cycle1, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다(두 주기 모두).
    assert result1.failed == 0
    assert _batch._content_retry_queue[1].retry_attempts == 0

    result2 = await run_artifacts_batch(
        fetch=fetch_cycle2, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result2.failed == 0
    assert 1 in _batch._content_retry_queue  # 새 내용 — 아직 격리되지 않는다
    assert _batch._content_retry_queue[1].retry_attempts == 0  # 예산이 리셋됐다
    assert _batch._content_retry_queue[1].change.name == "콘텐츠B"


# ── PR 리뷰 라운드 2 — T2: 재시도 시점 인프라 실패는 콘텐츠 예산을 태우지 않는다 ──


async def test_content_retry_pass_finish_failure_does_not_burn_content_budget(caplog):
    """[T2] 재시도 시점에 enrich 는 성공했지만 finish(embed) 가 인프라 실패하면 콘텐츠
    예산(retry_attempts)을 태우지 않고 ``_drain`` 2선과 동일한 시간 유계 스트릭으로 판정한다
    — 스트릭이 상한에 도달해야 비로소 격리된다. 수정 전에는 이 실패도 retry_attempts 를
    소진시켜(기본 예산 1) 첫 재시도 만에 정상 상품을 영구 격리했다 — #421 이 없애려던
    오격리를 재시도 패스 안에서 재현하는 셈이었다."""
    store = CatalogArtifactStore()
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(
                items=[_change(1, name="노이즈상품")], next_cursor="c1", has_more=False
            )
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings().model_copy(
        update={
            "artifacts_batch_content_retry_cycles": 1,
            "artifacts_batch_item_dead_letter_cycles": 2,
        }
    )
    llm = _FlakyLLM(fail_counts={"노이즈상품": settings.enrichment_item_attempts})

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue
    assert _batch._content_retry_queue[1].retry_attempts == 0

    # cycle2: LLM 은 이제 성공(fail_counts 소진)하지만 finish(embed) 는 항상 실패한다.
    with caplog.at_level("WARNING"):
        result2 = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=_FailingEmbed(), store=store, settings=settings
        )
    assert result2.recovered == 0
    assert result2.failed == 0  # 아직 격리 아님 — 스트릭 누적 중
    assert 1 in _batch._content_retry_queue
    assert _batch._content_retry_queue[1].retry_attempts == 0  # 콘텐츠 예산은 그대로
    assert "스트릭 누적" in caplog.text

    # cycle3: 스트릭이 상한(2)에 도달해서야 격리된다.
    result3 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_FailingEmbed(), store=store, settings=settings
    )
    assert result3.failed == 1
    assert 1 not in _batch._content_retry_queue


async def test_content_retry_pass_content_failure_still_burns_budget():
    """[T2] 재시도에서 콘텐츠 실패면 종전대로 예산을 소진한다(기존 동작 유지 — 회귀 없음)."""
    store = CatalogArtifactStore()
    change = _change(1, name="영구노이즈")
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(items=[change], next_cursor="c1", has_more=False)
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings()
    assert settings.artifacts_batch_content_retry_cycles == 1  # 기본값 전제 명시
    llm = _FlakyLLM(always_fail=["영구노이즈"])

    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue

    result2 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result2.failed == 1  # 재시도 패스가 콘텐츠 실패로 예산(1) 소진 — 이 주기에 격리 확정
    assert 1 not in _batch._content_retry_queue


# ── PR 리뷰 라운드 3 — T5: 재시도 패스의 콘텐츠 실패도 2선 스트릭의 "연속"을 끊어야 한다 ──


class _PhasedLLM:
    """공유 ``mode`` dict 의 ``"enrich"`` 값에 따라 콘텐츠 성공/실패를 낸다 — cycle 별로
    시나리오를 조립하는 fake(T5 회귀, PR 리뷰 라운드 3)."""

    def __init__(self, mode):
        self._mode = mode
        self.calls = 0

    async def complete(
        self, *, system, user, tier, max_tokens=1024, json_output=True, reasoning_effort=None
    ):
        self.calls += 1
        if self._mode["enrich"] == "fail":
            from app.core.llm import LLMError

            raise LLMError("boom")  # 원인 없음 — 콘텐츠 실패 화이트리스트(#325 R6) 통과
        return json.dumps({"tags": ["여행"], "attributes": {}}, ensure_ascii=False)


class _PhasedEmbed:
    """공유 ``mode`` dict 의 ``"finish"`` 값에 따라 embed 성공/실패를 낸다(T5 회귀)."""

    def __init__(self, mode):
        self._mode = mode

    def __call__(self, texts):
        if self._mode["finish"] == "fail":
            raise RuntimeError("embed API down")
        return [[float(len(t)), 1.0] for t in texts]


async def test_content_retry_streak_resets_on_content_failure_between_infra_failures(caplog):
    """[T5] 재시도 패스에서 인프라 실패 → 콘텐츠 실패 → 인프라 실패 순서가 나면, 콘텐츠
    실패가 2선 스트릭의 "연속"을 끊어야 한다(``_drain`` 의 대응 분기와 동일 불변식, #325 R6
    — 콘텐츠 실패는 정의상 항목 고유다) — 마지막 인프라 실패의 streak 는 1 이어야 한다.
    수정 전에는 콘텐츠 실패 분기가 스트릭을 clear 하지 않아 끊기지 않고 이어져(1 →
    안 지워짐 → 2) 연속이 아닌 인프라 실패가 연속으로 오판됐다 — 이 테스트는 수정 전
    코드에서 마지막 streak 가 2 로 찍혀 반드시 실패한다."""
    store = CatalogArtifactStore()
    mode = {"enrich": "fail", "finish": "ok"}
    llm = _PhasedLLM(mode)
    embed = _PhasedEmbed(mode)
    cycle = 0

    async def fetch(cursor, limit):
        nonlocal cycle
        cycle += 1
        if cycle == 1:
            return ProductChangesPage(
                items=[_change(1, name="시나리오상품")], next_cursor="c1", has_more=False
            )
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings().model_copy(
        update={
            "artifacts_batch_content_retry_cycles": 2,
            "artifacts_batch_item_dead_letter_cycles": 3,
        }
    )

    # cycle1: _drain 자체에서 콘텐츠 실패 → 큐 등재(retry_attempts=0).
    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
    )
    # [T6, PR 리뷰 라운드 4] 재시도 큐 등재만 있고 격리는 아직 확정되지 않았다.
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue

    # cycle2: 재시도 패스 — enrich 성공, finish(embed) 인프라 실패 → streak=1, 큐 유지.
    mode["enrich"] = "ok"
    mode["finish"] = "fail"
    result2 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
    )
    assert result2.recovered == 0
    assert result2.failed == 0
    assert 1 in _batch._content_retry_queue

    # cycle3: 재시도 패스 — 다시 콘텐츠 실패로 돌아간다. 스트릭이 여기서 끊겨야 한다.
    mode["enrich"] = "fail"
    result3 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
    )
    assert result3.failed == 0  # 예산(2) 미소진 — 아직 큐에 남는다(retry_attempts=1/2)
    assert 1 in _batch._content_retry_queue

    # cycle4: 재시도 패스 — 다시 인프라 실패. 끊겼다면 streak=1(수정 전엔 2로 이어짐).
    mode["enrich"] = "ok"
    mode["finish"] = "fail"
    with caplog.at_level("WARNING"):
        result4 = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
        )
    assert result4.failed == 0
    assert 1 in _batch._content_retry_queue
    assert "streak=1/3" in caplog.text
    assert "streak=2/3" not in caplog.text


# ── PR 리뷰 라운드 4 — T6: BatchResult.failed 는 "격리 확정" 시에만 늘어난다 ──


async def test_batch_result_failed_only_counts_confirmed_isolation_not_retry_enqueue():
    """[T6] 콘텐츠 실패가 재시도 큐로 넘어간 주기에는 ``failed == 0`` 이고, 예산 소진으로
    실제 격리된 주기에만 ``failed == 1`` 이다 — 3선 비율 가드용 ``page_failed``(비공개 지역
    변수)와 달리, 관측 카운터 ``BatchResult.failed`` 는 "이번 주기에 반영되지 않았다"가
    아니라 "격리가 확정됐다"만 센다. 수정 전에는 재시도 큐 등재 시점에도 ``failed`` 를
    올려, ``scheduler.py`` 가 dead-letter ERROR 로그가 하나도 없는 주기에도
    "증분 배치 부분 실패 — dead-letter 로그 확인" ERROR 알람을 거짓으로 띄웠다."""
    store = CatalogArtifactStore()
    change = _change(1, name="영구노이즈")
    call_count = 0

    async def fetch(cursor, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ProductChangesPage(items=[change], next_cursor="c1", has_more=False)
        return ProductChangesPage(items=[], next_cursor="c1", has_more=False)

    settings = get_settings()
    assert settings.artifacts_batch_content_retry_cycles == 1  # 기본값 전제 명시
    llm = _FlakyLLM(always_fail=["영구노이즈"])

    # cycle1: 콘텐츠 실패 → 재시도 큐 등재 — 아직 격리가 아니다.
    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result1.failed == 0
    assert result1.retry_pending == 1
    assert 1 in _batch._content_retry_queue

    # cycle2: 재시도 패스가 예산(1)을 소진 — 이 주기에 격리가 확정된다.
    result2 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=_embed, store=store, settings=settings
    )
    assert result2.failed == 1
    assert result2.retry_pending == 0
    assert 1 not in _batch._content_retry_queue


# ── PR 리뷰 라운드 5 — T7: 2선 중단이 콘텐츠 재시도 예산을 삼키면 안 된다 ──


async def test_content_retry_budget_survives_second_line_interruption(caplog):
    """[T7] 콘텐츠 실패로 쌓인 재시도 예산은, 그 다음 주기가 2선 실패로 ``_drain`` 을
    전파(raise)로 중단시켜도 사라지지 않고 이어진다 — 수정 전에는 항목 루프 맨 앞의
    무조건 pop 이 큐 항목을 즉시 지워버려, 중단된 다음 주기에 같은 상품이 다시 와도
    예산이 0 부터 재시작했다(``artifacts_batch_content_retry_cycles=1`` 이 "정확히 1회
    재시도 후 격리"를 보장하지 못하고 관대한 쪽으로 깨짐). 이 테스트는 수정 전 코드에서
    반드시 실패한다(2선 중단 뒤 큐 항목이 사라져 있다)."""
    store = CatalogArtifactStore()
    mode = {"enrich": "fail", "finish": "ok"}
    llm = _PhasedLLM(mode)
    embed = _PhasedEmbed(mode)

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, name="시나리오상품")], next_cursor="c1", has_more=False
        )

    settings = get_settings().model_copy(
        update={
            "artifacts_batch_content_retry_cycles": 1,
            "artifacts_batch_item_dead_letter_cycles": 3,
        }
    )

    # cycle1: 콘텐츠 실패 → 큐 등재(retry_attempts=0), 아직 격리 아님.
    result1 = await run_artifacts_batch(
        fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
    )
    assert result1.failed == 0
    assert 1 in _batch._content_retry_queue
    assert _batch._content_retry_queue[1].retry_attempts == 0

    # cycle2: enrich 성공·finish(embed) 인프라 실패 → 2선, streak(1) < 상한(3) → 전파(중단).
    mode["enrich"] = "ok"
    mode["finish"] = "fail"
    with pytest.raises(RuntimeError, match="embed API down"):
        await run_artifacts_batch(fetch=fetch, llm=llm, embed=embed, store=store, settings=settings)
    # [T7 핵심] 중단됐어도 콘텐츠 재시도 큐 항목·예산 진행분은 그대로 남아 있어야 한다.
    assert 1 in _batch._content_retry_queue
    assert _batch._content_retry_queue[1].retry_attempts == 0

    # cycle3: 다시 콘텐츠 실패(같은 내용) — 이어받은 예산을 소진(0+1=1 ≥ 1) → 격리 확정.
    mode["enrich"] = "fail"
    with caplog.at_level("ERROR"):
        result3 = await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
        )
    assert result3.failed == 1
    assert 1 not in _batch._content_retry_queue
    assert "예산 소진" in caplog.text


async def test_content_retry_converges_despite_repeated_second_line_interruption():
    """[T7] "콘텐츠 실패 → 2선 중단"이 반복돼도 유계 주기 안에 격리된다(무한 회피 없음) —
    2선 중단이 몇 번 끼어들어도 콘텐츠 예산은 매번 보존되므로, 예산(N)만큼 콘텐츠 실패가
    실제로 누적되면 결국 격리된다. 수정 전에는 2선 중단마다 예산이 0 으로 리셋돼 이
    패턴이 무한히 반복될 수 있었다(콘텐츠 예산도 2선 스트릭도 영영 상한에 도달하지 못함)."""
    store = CatalogArtifactStore()
    mode = {"enrich": "fail", "finish": "ok"}
    llm = _PhasedLLM(mode)
    embed = _PhasedEmbed(mode)

    async def fetch(cursor, limit):
        return ProductChangesPage(
            items=[_change(1, name="반복상품")], next_cursor="c1", has_more=False
        )

    settings = get_settings().model_copy(
        update={
            "artifacts_batch_content_retry_cycles": 2,
            "artifacts_batch_item_dead_letter_cycles": 10,  # 2선 상한은 절대 안 닿게 넉넉히
        }
    )

    async def content_failure_cycle():
        mode["enrich"] = "fail"
        return await run_artifacts_batch(
            fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
        )

    async def second_line_interrupt_cycle():
        mode["enrich"] = "ok"
        mode["finish"] = "fail"
        with pytest.raises(RuntimeError, match="embed API down"):
            await run_artifacts_batch(
                fetch=fetch, llm=llm, embed=embed, store=store, settings=settings
            )

    result1 = await content_failure_cycle()  # retry_attempts=0(신규 등재)
    assert result1.failed == 0
    await second_line_interrupt_cycle()  # 중단 — 예산(0) 보존
    assert _batch._content_retry_queue[1].retry_attempts == 0

    result2 = await content_failure_cycle()  # retry_attempts=0+1=1 < budget(2) → 큐 유지
    assert result2.failed == 0
    await second_line_interrupt_cycle()  # 다시 중단 — 예산(1) 보존
    assert _batch._content_retry_queue[1].retry_attempts == 1

    result3 = await content_failure_cycle()  # retry_attempts=1+1=2 ≥ budget(2) → 격리 확정
    assert result3.failed == 1
    assert 1 not in _batch._content_retry_queue


# ── PR 리뷰 라운드 1 — F5: pg 폴백 clear 는 DB 성공 여부와 무관하게 항상 적용 ──


def test_pg_store_clear_failure_streak_always_clears_fallback_even_when_db_fails():
    """[F5] ``clear_failure_streak`` 는 DB DELETE 가 실패해도 인메모리 폴백 표를 항상
    clear 한다 — "성공 처리 후 DELETE 만 DB 오류" 시나리오에서 인메모리에만 stale 값이
    남는 드리프트 절반을 없앤다(dict pop 이라 비용 0)."""
    from app.pipelines.artifact_store import FailureStreakTable
    from app.pipelines.pg_artifact_store import PgCatalogArtifactStore

    store = object.__new__(PgCatalogArtifactStore)  # __init__(dsn) 의 실 커넥션 없이 구성
    store._failure_streak_schema_ready = True  # ensure_schema 분기를 건드리지 않는다
    store._failure_streak_fallback = FailureStreakTable()
    store._failure_streak_fallback_warned = False
    store._failure_streak_fallback.bump("item", "1", ttl_s=60)  # 이전 폴백 잔재를 미리 심어둔다

    class _BoomConnCtx:
        def __enter__(self):
            raise RuntimeError("db down mid-delete")

        def __exit__(self, *exc):
            return False

    class _BoomPool:
        def connection(self):
            return _BoomConnCtx()

    store._pool = _BoomPool()

    store.clear_failure_streak("item", "1")

    # DB DELETE 는 실패했지만(_BoomPool), 인메모리 폴백은 항상 clear 됐다 — 다음 bump 는 1부터.
    assert store._failure_streak_fallback.bump("item", "1", ttl_s=60) == 1


# ── PR 리뷰 라운드 2 — T3: 폴백 WARNING latch 가 DB 복구 후 재발 시에도 다시 뜨는지 ──


def test_pg_store_fallback_warning_refires_after_recovery(caplog):
    """[T3] ``_failure_streak_fallback_warned`` 는 latch 가 아니다 — DB 경로가 정상 종료하면
    False 로 되돌아가, 이후 다시 장애가 나면 WARNING 이 다시 뜬다. 수정 전에는 한 번 True 가
    되면 인스턴스 수명 동안 리셋되지 않아 pg 순단 1회 → 복구 → 훨씬 심각한 재발 장애에도
    조용히 넘어갔다. 회귀 테스트: 실패 → 성공 → 실패 시 WARNING 이 두 번 남는다."""
    from app.pipelines.artifact_store import FailureStreakTable
    from app.pipelines.pg_artifact_store import PgCatalogArtifactStore

    store = object.__new__(PgCatalogArtifactStore)  # __init__(dsn) 의 실 커넥션 없이 구성
    store._failure_streak_schema_ready = True  # ensure_schema 분기를 건드리지 않는다
    store._failure_streak_fallback = FailureStreakTable()
    store._failure_streak_fallback_warned = False

    class _BoomConnCtx:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *exc):
            return False

    class _BoomPool:
        def connection(self):
            return _BoomConnCtx()

    class _OkConnCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return (1,)

    class _OkPool:
        def connection(self):
            return _OkConnCtx()

    with caplog.at_level("WARNING"):
        store._pool = _BoomPool()
        store.bump_failure_streak("item", "1", ttl_s=60)  # 실패 1회차 — WARNING
        assert store._failure_streak_fallback_warned is True

        store._pool = _OkPool()
        store.bump_failure_streak("item", "1", ttl_s=60)  # DB 복구 — latch 가 풀린다
        assert store._failure_streak_fallback_warned is False

        store._pool = _BoomPool()
        store.bump_failure_streak("item", "1", ttl_s=60)  # 재발 — WARNING 이 다시 떠야 한다
        assert store._failure_streak_fallback_warned is True

    assert caplog.text.count("인메모리 폴백으로 전환") == 2


# ── PR 리뷰 라운드 6 — T8: 간헐적 DB 실패에서도 폴백 진행분을 흡수해 지연을 줄인다 ──


class _BoomConnCtx:
    """DB 커넥션 획득 자체가 실패하는 가짜(T8 전용 — 위 F5/T3 테스트의 로컬 클래스와 동형)."""

    def __enter__(self):
        raise RuntimeError("db down")

    def __exit__(self, *exc):
        return False


class _BoomPool:
    def connection(self):
        return _BoomConnCtx()


class _StatefulFakeDB:
    """(kind, key) → streak 를 흉내 내는 가짜 DB 상태(순수 인메모리, T8 회귀 테스트 전용).

    실제 SQL 문 2종(메인 UPSERT·흡수 UPDATE)을 텍스트로 구분해 반영한다.
    """

    def __init__(self):
        self.streaks: dict[tuple[str, str], int] = {}

    def execute(self, sql, params):
        if "INSERT INTO batch_failure_state" in sql:
            kind, key, _ttl_s = params
            new = self.streaks.get((kind, key), 0) + 1  # 테스트는 즉시 연속 호출 — TTL 안 지남
            self.streaks[(kind, key)] = new
            return _FakeCursor((new,))
        if "UPDATE batch_failure_state" in sql:
            streak, kind, key = params
            self.streaks[(kind, key)] = streak
            return _FakeCursor(None)
        raise AssertionError(f"예상 못 한 SQL: {sql}")


class _FakeCursor:
    def __init__(self, fetchone_result):
        self._fetchone_result = fetchone_result

    def fetchone(self):
        return self._fetchone_result


class _StatefulConnCtx:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return self._db.execute(sql, params)


class _StatefulPool:
    def __init__(self, db):
        self._db = db

    def connection(self):
        return _StatefulConnCtx(self._db)


class _BoomUpdateConnCtx:
    """메인 UPSERT 는 성공하지만 그 다음 흡수 UPDATE 만 실패하는 가짜 커넥션(T8 전용)."""

    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "UPDATE batch_failure_state" in sql:
            raise RuntimeError("db down mid-absorb")
        return self._db.execute(sql, params)


class _BoomUpdatePool:
    def __init__(self, db):
        self._db = db

    def connection(self):
        return _BoomUpdateConnCtx(self._db)


def _new_pg_store_for_t8():
    from app.pipelines.artifact_store import FailureStreakTable
    from app.pipelines.pg_artifact_store import PgCatalogArtifactStore

    store = object.__new__(PgCatalogArtifactStore)  # __init__(dsn) 의 실 커넥션 없이 구성
    store._failure_streak_schema_ready = True  # ensure_schema 분기를 건드리지 않는다
    store._failure_streak_fallback = FailureStreakTable()
    store._failure_streak_fallback_warned = False
    return store


def test_pg_store_bump_absorbs_fallback_progress_across_intermittent_failures():
    """[T8] DB 실패 → 성공 → 실패 → 성공을 번갈아도, 성공 경로가 그 사이 폴백에 쌓인
    진행분을 흡수해 상한 도달을 앞당긴다. 수정 전에는 DB·폴백이 각자 자기 성공분만 세어
    (비교·흡수 없이) 4번째 호출의 반환값이 2([1,1,2,2])에 그쳤다 — DB 는 짝수번째
    호출(2번째·4번째)에서만 자기 스트릭을 1→2 로 올리고, 홀수번째(1번째·3번째)에 폴백에서
    쌓인 값은 그대로 버려졌다. 이 테스트는 수정 전 코드에서 [1, 1, 2, 2]가 나와(4번째
    값이 3이 아니라 2) 반드시 실패한다."""
    db = _StatefulFakeDB()
    store = _new_pg_store_for_t8()
    boom_pool = _BoomPool()
    ok_pool = _StatefulPool(db)

    results = []
    for pool in (boom_pool, ok_pool, boom_pool, ok_pool):
        store._pool = pool
        results.append(store.bump_failure_streak("item", "1", ttl_s=3600))

    # 1번째(fail): 폴백 1. 2번째(ok): db 자체 1, 폴백(1)+1=2 흡수 → 2. 3번째(fail): 폴백
    # 은 흡수 후 비워졌으므로 다시 1부터. 4번째(ok): db 자체(2 에서 이어) 3, 폴백(1)+1=2
    # 이므로 db 쪽 3 이 이긴다 — 흡수 UPDATE 없이도 db 단독 진행이 이미 앞서 있다.
    assert results == [1, 2, 1, 3]


def test_pg_store_bump_clears_fallback_after_absorbing_progress():
    """[T8] 폴백 진행분을 흡수한 뒤에는 폴백 엔트리가 비워져, 다음 성공에서 같은 진행분을
    또 세지 않는다(이중 계수 방지)."""
    db = _StatefulFakeDB()
    store = _new_pg_store_for_t8()

    store._pool = _BoomPool()
    store.bump_failure_streak("item", "1", ttl_s=3600)  # 폴백에 1 쌓임
    assert store._failure_streak_fallback.peek("item", "1", ttl_s=3600) == 1

    store._pool = _StatefulPool(db)
    result = store.bump_failure_streak("item", "1", ttl_s=3600)  # 흡수: 폴백(1)+1=2 > db 1
    assert result == 2
    # [T8 핵심] 흡수 후 폴백 엔트리는 비워진다.
    assert store._failure_streak_fallback.peek("item", "1", ttl_s=3600) == 0

    # 다음 DB 성공은 이미 비워진 폴백을 다시 더하지 않고 db 자체 값(2)에서만 +1 한다.
    result2 = store.bump_failure_streak("item", "1", ttl_s=3600)
    assert result2 == 3


def test_pg_store_bump_falls_back_when_absorb_update_fails():
    """[T8] 메인 UPSERT 는 성공했지만 흡수 UPDATE 만 실패하면, 예외가 밖으로 나오지 않고
    조용히 폴백 경로로 넘어간다 — 이 메서드는 절대 예외를 밖으로 내지 않는다는 현행
    불변식이 흡수 경로에도 그대로 적용됨을 고정한다."""
    db = _StatefulFakeDB()
    store = _new_pg_store_for_t8()

    store._pool = _BoomPool()
    store.bump_failure_streak("item", "1", ttl_s=3600)  # 폴백에 1 쌓임(흡수 대상 진행분)
    assert store._failure_streak_fallback.peek("item", "1", ttl_s=3600) == 1

    store._pool = _BoomUpdatePool(db)
    # 폴백(1)+1=2 > 메인 UPSERT 가 돌려준 값(1) 이라 흡수 UPDATE 를 시도하는데, 이 가짜
    # 커넥션은 그 UPDATE 만 실패시킨다 — 예외가 새면 이 호출 자체가 raise 해야 한다.
    result = store.bump_failure_streak("item", "1", ttl_s=3600)

    assert result == 2  # raise 없이 폴백 경로로 넘어가 폴백 자신의 bump(): 1 → 2
