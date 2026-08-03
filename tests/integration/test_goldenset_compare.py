"""라이브 pg-catalog·Google 임베딩으로 골든셋 방식1/2 결정을 재현한다."""

from __future__ import annotations

import time
from functools import partial

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from app.core.config import get_settings
from app.pipelines.artifact_store import CatalogArtifactStore
from app.pipelines.compare import compare_backends
from app.pipelines.embedding import embed_texts
from app.pipelines.pg_artifact_store import PgCatalogArtifactStore
from evals.goldenset.loader import to_compare_golden_cases
from tests._goldenset_compare import candidates_provider

pytestmark = pytest.mark.integration


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch):
    """공통 테스트 격리가 비운 외부 API 키를 라이브 비교에서만 기존 Settings 경로로 복원한다."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


def test_embedding_rerank_remains_default_winner_on_live_goldenset(live_settings) -> None:
    settings = live_settings
    if not settings.google_api_key:
        pytest.skip("google_api_key 미구성 — 라이브 임베딩 비교를 건너뜁니다")

    pg_store: PgCatalogArtifactStore | None = None
    try:
        pg_store = PgCatalogArtifactStore(settings.catalog_db_url)
        artifacts = pg_store.all()
    except (psycopg.OperationalError, PoolTimeout) as exc:
        pytest.skip(f"pg-catalog 미기동 — 라이브 비교를 건너뜁니다: {exc}")
    finally:
        if pg_store is not None:
            pg_store.close()

    if not artifacts:
        pytest.skip("pg-catalog products 테이블이 비어 있어 라이브 비교를 건너뜁니다")

    store = CatalogArtifactStore()
    store.replace_all(artifacts)
    embed_query = partial(embed_texts, task_type=settings.embedding_task_query)

    started = time.perf_counter()
    report = compare_backends(
        to_compare_golden_cases("dev"),
        store=store,
        embed=embed_query,
        candidates=candidates_provider(),
        k=10,
    )
    elapsed = time.perf_counter() - started
    print(
        "goldenset compare:"
        f" method1 mean recall@10={report.method1.mean_recall_at_k:.4f},"
        f" method2 mean recall@10={report.method2.mean_recall_at_k:.4f},"
        f" elapsed={elapsed:.1f}s"
    )

    assert report.method2.mean_recall_at_k >= report.method1.mean_recall_at_k
