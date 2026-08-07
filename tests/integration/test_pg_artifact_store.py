"""PgCatalogArtifactStore 통합 테스트 (이슈 #31, api-spec §4.8) — 실 pg-catalog 필요.

`docker compose up -d pg-catalog` 로 컨테이너가 떠 있어야 통과한다. 기본 pytest 실행에서는
@pytest.mark.integration 으로 제외된다(pyproject.toml addopts) — 명시적으로
`uv run pytest tests/integration -m integration` 로 실행한다.

CatalogArtifactStore(인메모리)는 유닛 테스트가 계속 쓰므로 여기서 건드리지 않는다
(tests/conftest.py InMemory 격리 컨벤션, 커밋 5066ecf 와 동일 원칙 — 실 인프라 테스트는 분리).
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.pipelines.artifact_store import CatalogArtifact
from app.pipelines.pg_artifact_store import PgCatalogArtifactStore

pytestmark = pytest.mark.integration

_DIM = 1536


def _vec(*nonzero: float) -> list[float]:
    """products.embedding 은 vector(1536) 고정 — 앞자리만 채우고 나머지는 0으로 패딩."""
    out = [0.0] * _DIM
    for i, v in enumerate(nonzero):
        out[i] = v
    return out


@pytest.fixture
def store():
    s = PgCatalogArtifactStore(get_settings().catalog_db_url)
    s.clear()
    s.set_cursor(None)
    yield s
    s.clear()
    s.set_cursor(None)
    s.close()


def test_upsert_and_get_roundtrip(store):
    store.upsert(
        CatalogArtifact(
            product_id=1,
            search_doc="여행 방수 파우치",
            embedding=_vec(0.6, 0.8),
            extras={"tags": ["여행"]},
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    art = store.get(1)
    assert art is not None
    assert art.product_id == 1
    assert art.search_doc == "여행 방수 파우치"
    assert art.embedding == pytest.approx(_vec(0.6, 0.8))
    assert art.extras == {"tags": ["여행"]}


def test_upsert_is_idempotent_update(store):
    store.upsert(
        CatalogArtifact(
            product_id=1,
            search_doc="old",
            embedding=_vec(1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    store.upsert(
        CatalogArtifact(
            product_id=1,
            search_doc="new",
            embedding=_vec(0.0, 1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    assert store.count() == 1
    assert store.get(1).search_doc == "new"


def test_delete_removes_artifact(store):
    store.upsert(
        CatalogArtifact(
            product_id=1,
            search_doc="x",
            embedding=_vec(1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    store.delete(1)
    assert store.get(1) is None


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_all_returns_every_artifact(store):
    store.upsert(
        CatalogArtifact(
            product_id=1,
            search_doc="a",
            embedding=_vec(1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    store.upsert(
        CatalogArtifact(
            product_id=2,
            search_doc="b",
            embedding=_vec(0.0, 1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    ids = {a.product_id for a in store.all()}
    assert ids == {1, 2}


def test_replace_all_atomic_swap_removes_stale(store):
    store.upsert(
        CatalogArtifact(
            product_id=99,
            search_doc="stale",
            embedding=_vec(1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    store.replace_all(
        [
            CatalogArtifact(
                product_id=1,
                search_doc="fresh",
                embedding=_vec(0.0, 1.0),
                embed_model="gemini-embedding-001",
                embed_dim=1536,
            )
        ]
    )
    assert store.get(99) is None
    assert store.get(1) is not None
    assert store.count() == 1


def test_replace_all_and_cursor_commit_together(store):
    store.upsert(
        CatalogArtifact(
            product_id=99,
            search_doc="stale",
            embedding=_vec(1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
        )
    )
    store.set_cursor("old")

    store.replace_all_and_set_cursor(
        [
            CatalogArtifact(
                product_id=1,
                search_doc="fresh",
                embedding=_vec(0.0, 1.0),
                embed_model="gemini-embedding-001",
                embed_dim=1536,
            )
        ],
        "fresh-cursor",
    )

    assert store.get(99) is None
    assert store.get(1) is not None
    assert store.get_cursor() == "fresh-cursor"


def test_cursor_persists_across_store_instances(store):
    store.set_cursor("c42")
    other = PgCatalogArtifactStore(get_settings().catalog_db_url)
    try:
        assert other.get_cursor() == "c42"
    finally:
        other.close()


def test_cursor_defaults_to_none(store):
    assert store.get_cursor() is None


def test_pg_store_satisfies_shared_protocol(store):
    from app.pipelines.artifact_store import ArtifactStore

    assert isinstance(store, ArtifactStore)


def test_provenance_roundtrip(store):
    store.upsert(
        CatalogArtifact(
            product_id=42,
            search_doc="문서",
            embedding=_vec(1.0),
            embed_model="gemini-embedding-001",
            embed_dim=1536,
            embed_task="RETRIEVAL_DOCUMENT",
            normalized=True,
        )
    )
    got = store.get(42)
    assert got.embed_model == "gemini-embedding-001"
    assert got.embed_dim == 1536
    assert got.embed_task == "RETRIEVAL_DOCUMENT"
    assert got.normalized is True


def test_top_k_by_vector_include_contract(store, monkeypatch):
    for product_id, embedding in (
        (1, _vec(1.0, 0.0)),
        (2, _vec(0.8, 0.2)),
        (3, _vec(0.0, 1.0)),
    ):
        store.upsert(
            CatalogArtifact(
                product_id=product_id,
                search_doc=str(product_id),
                embedding=embedding,
                embed_model="gemini-embedding-001",
                embed_dim=1536,
                embed_task="RETRIEVAL_DOCUMENT",
                normalized=True,
            )
        )

    assert store.top_k_by_vector(_vec(1.0, 0.0), k=4, include={2, 3, 999}) == [2, 3]
    assert store.top_k_by_vector(_vec(1.0, 0.0), k=4, include={1, 2}, exclude={1}) == [2]

    original_pool = store._pool

    class QueryForbiddenPool:
        def connection(self):
            pytest.fail("include=set()은 DB 쿼리를 보내면 안 된다")

    monkeypatch.setattr(store, "_pool", QueryForbiddenPool())
    try:
        assert store.top_k_by_vector(_vec(1.0, 0.0), k=4, include=set()) == []
    finally:
        monkeypatch.setattr(store, "_pool", original_pool)


@pytest.mark.integration
def test_check_rejects_missing_provenance():
    import psycopg

    with psycopg.connect(get_settings().catalog_db_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO products (product_id, search_doc, embedding) VALUES (%s, %s, %s)",
                    (999999, "d", "[" + ",".join(["0"] * 1536) + "]"),
                )


# ── 이슈 #416: batch_failure_state 영속화(실 pg-catalog) ──


@pytest.fixture
def failure_streak_store(store):
    """store 픽스처와 같은 dsn 을 쓰되, 스트릭 상태를 테스트 전후로 정리한다."""
    store.clear_failure_streak("item", "int-test-item")
    store.clear_failure_streak("page", "int-test-page")
    yield store
    store.clear_failure_streak("item", "int-test-item")
    store.clear_failure_streak("page", "int-test-page")


def test_batch_failure_state_table_self_creates(failure_streak_store):
    """첫 사용 시 idempotent DDL 로 batch_failure_state 테이블이 자가 생성된다(#416)."""
    assert failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 1


def test_batch_failure_state_bump_accumulates_across_store_instances(failure_streak_store):
    """서로 다른 두 스토어 인스턴스(=두 프로세스 모사)에서 bump 가 누적된다(#416)."""
    assert failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 1

    other = PgCatalogArtifactStore(get_settings().catalog_db_url)
    try:
        assert other.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 2
        assert other.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 3
    finally:
        other.close()


def test_batch_failure_state_bump_absorbs_fallback_progress(failure_streak_store):
    """[T8, PR 리뷰 라운드 6] 간헐적 DB 실패로 인메모리 폴백에 쌓인 진행분을, 다음 DB 성공
    bump 이 실 pg-catalog 행에 흡수 반영한다 — 인메모리 fake 가 아니라 실 UPDATE 문이
    실제로 실행돼 다른 인스턴스에서 읽어도 이어짐을 확인한다."""
    store = failure_streak_store
    # DB 쪽은 아직 0에서 시작 — 폴백에 3을 미리 쌓아 "간헐적 DB 실패로 폴백에 쌓인 진행분"을
    # 재현한다(실제로는 이 몫이 여러 번의 실패한 bump_failure_streak 호출에서 쌓였을 것).
    store._failure_streak_fallback.bump("item", "int-test-item", ttl_s=3600.0)
    store._failure_streak_fallback.bump("item", "int-test-item", ttl_s=3600.0)
    store._failure_streak_fallback.bump("item", "int-test-item", ttl_s=3600.0)
    assert store._failure_streak_fallback.peek("item", "int-test-item", ttl_s=3600.0) == 3

    # DB 는 정상이라 이 bump 자체는 DB 값만으로 1이 되지만, 폴백(3)+1=4 가 더 커 흡수된다
    # (실 UPDATE 문이 batch_failure_state 행을 4로 맞춘다).
    result = store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0)
    assert result == 4
    # 흡수 후 폴백은 비워진다(이중 계수 방지).
    assert store._failure_streak_fallback.peek("item", "int-test-item", ttl_s=3600.0) == 0

    # 다른 인스턴스(=다른 프로세스 모사)에서 읽어도 흡수된 4에서 이어진다 — 실 pg 행 반영 확인.
    other = PgCatalogArtifactStore(get_settings().catalog_db_url)
    try:
        assert other.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 5
    finally:
        other.close()


def test_batch_failure_state_ttl_resets(failure_streak_store):
    """마지막 갱신이 ttl_s 보다 오래되면 다음 bump 는 1 로 리셋된다(#416)."""
    import time

    assert failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 1
    assert failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 2
    time.sleep(1.1)
    # ttl_s=1 보다 오래(1.1s) 지났으니 "연속"이 끊겨 다음 bump 는 1 로 리셋된다.
    assert failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=1.0) == 1


def test_batch_failure_state_clear_and_purge_stale(failure_streak_store):
    """clear·purge_stale_failure_streaks 가 실제로 행을 지운다(#416)."""
    failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0)
    failure_streak_store.clear_failure_streak("item", "int-test-item")
    assert failure_streak_store.bump_failure_streak("item", "int-test-item", ttl_s=3600.0) == 1

    failure_streak_store.bump_failure_streak("page", "int-test-page", ttl_s=3600.0)
    purged = failure_streak_store.purge_stale_failure_streaks(ttl_s=0.000001)
    assert purged >= 1


@pytest.mark.integration
def test_loader_persists_provenance(tmp_path):
    import json

    from app.core.config import get_settings
    from scripts import load_sample_100

    doc = {
        "product_id": 123,
        "search_doc": "문서",
        "embedding": [0.0] * 1536,
        "extras": {"tags": ["여행"]},
        "embed_model": "gemini-embedding-001",
        "embed_dim": 1536,
        "embed_task": "RETRIEVAL_DOCUMENT",
        "normalized": True,
    }
    # L2 norm=1 검증 통과 위해 한 성분을 1.0으로
    doc["embedding"][0] = 1.0
    path = tmp_path / "documents.jsonl"
    path.write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")

    documents = load_sample_100.load_documents(
        path, expected_count=1, expected_dim=1536, expected_model="gemini-embedding-001"
    )
    load_sample_100.upsert_documents(documents)

    store = PgCatalogArtifactStore(get_settings().catalog_db_url)
    got = store.get(123)
    assert got.embed_task == "RETRIEVAL_DOCUMENT" and got.normalized is True
    store.close()
