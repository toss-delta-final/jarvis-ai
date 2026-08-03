"""색상 동의어 오프라인 구축 파이프라인 회귀 테스트 (이슈 #258)."""

from __future__ import annotations

from collections import Counter
import json
import math

import pytest

from app.core.config import Settings
from app.pipelines import color_synonym_seed as seed
from app.schemas.spring import ProductChange, ProductChangesPage


def _change(pid: int, color: object = None, *, attributes: dict | None = None) -> ProductChange:
    attrs = attributes if attributes is not None else {"색상": color}
    return ProductChange(
        product_id=pid,
        status="ON_SALE",
        updated_at="2026-08-04T00:00:00Z",
        attributes=attrs,
    )


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"색상": " 블랙 "}, ["블랙"]),
        ({"색상": [" 핑크 ", "", "  ", "레드", "핑크", None]}, ["핑크", "레드", "핑크"]),
        ({"색상": None}, []),
        ({}, []),
        (None, []),
        ({"색상": "  "}, []),
        ({"색상": 123}, []),
    ],
)
def test_extract_color_terms_handles_catalog_shapes(attributes, expected) -> None:
    assert seed.extract_color_terms(attributes) == expected


def test_count_terms_counts_products_per_term_not_duplicate_tokens() -> None:
    counts = seed.count_terms([_change(1, ["블랙", "블랙"]), _change(2, "블랙")])
    assert counts == Counter({"블랙": 2})


async def test_harvest_terms_drains_i17_without_enrichment() -> None:
    pages = [
        ProductChangesPage(items=[_change(1, "블랙")], next_cursor="c1", has_more=True),
        ProductChangesPage(items=[_change(2, ["검정", "블랙"])], has_more=False),
    ]
    seen: list[tuple[str | None, int]] = []

    async def fetch(cursor: str | None, limit: int) -> ProductChangesPage:
        seen.append((cursor, limit))
        return pages.pop(0)

    assert await seed.harvest_terms(fetch=fetch, page_size=321) == Counter({"블랙": 2, "검정": 1})
    assert seen == [("0", 321), ("c1", 321)]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"블랙": 3, "검정": 1}, Counter({"블랙": 3, "검정": 1})),
        (["블랙", "검정", "블랙"], Counter({"블랙": 2, "검정": 1})),
    ],
)
def test_load_terms_accepts_count_map_or_string_array(tmp_path, payload, expected) -> None:
    path = tmp_path / "colors.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert seed.load_terms(path) == expected


@pytest.mark.parametrize("payload", [{"블랙": 0}, {"블랙": True}, ["블랙", 1], "블랙"])
def test_load_terms_rejects_invalid_input(tmp_path, payload) -> None:
    path = tmp_path / "colors.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        seed.load_terms(path)


def test_cluster_terms_is_frequency_seeded_and_excludes_sentinels() -> None:
    counts = Counter(
        {
            "블랙": 100,
            "혼합색상": 90,
            "검정": 10,
            "빨강": 8,
            "기타": 7,
            "해당없음": 6,
            "투명": 5,
            "멀티컬러": 4,
        }
    )
    vectors = {"블랙": [1.0, 0.0], "검정": [0.99, 0.01], "빨강": [0.0, 1.0]}
    seen: list[str] = []

    def embed(terms: list[str]) -> list[list[float]]:
        seen.extend(terms)
        return [vectors[t] for t in terms]

    clusters = seed.cluster_terms(counts, embed, top_n=30, threshold=0.95)
    assert [(c.canonical, [m.term for m in c.members]) for c in clusters] == [
        ("블랙", ["블랙", "검정"]),
        ("빨강", ["빨강"]),
    ]
    assert seen == ["블랙", "검정", "빨강"]
    assert seed.NON_COLOR_TERMS.isdisjoint(seen)


def test_default_threshold_separates_measured_true_and_false_pairs() -> None:
    """실측 기본값 조합이 출하 시 오탐을 막고 핵심 정탐은 보존한다."""
    threshold = Settings.model_fields["color_synonym_cluster_threshold"].default
    assert threshold == 0.85

    def at_cosine(axis: int, score: float) -> list[float]:
        vector = [0.0] * 9
        vector[axis] = score
        vector[axis + 1] = math.sqrt(1.0 - score**2)
        return vector

    vectors = {
        "블랙": at_cosine(0, 1.0),
        "블루": at_cosine(0, 0.849),
        "검정": at_cosine(0, 0.884),
        "그레이": at_cosine(3, 1.0),
        "그린": at_cosine(3, 0.846),
        "네이비": at_cosine(6, 1.0),
        "남색": at_cosine(6, 0.854),
    }
    counts = Counter(
        {"블랙": 2354, "그레이": 749, "네이비": 631, "블루": 438, "검정": 11, "남색": 8, "그린": 1}
    )
    clusters = seed.cluster_terms(
        counts, lambda terms: [vectors[term] for term in terms], top_n=3, threshold=threshold
    )
    members = {
        cluster.canonical: {member.term for member in cluster.members} for cluster in clusters
    }
    assert "블루" not in members["블랙"]
    assert "그린" not in members["그레이"]
    assert "검정" in members["블랙"]
    assert "남색" in members["네이비"]


def test_low_frequency_tail_is_compared_against_top_n_anchors(tmp_path) -> None:
    """top-N 밖 저빈도 검정도 앵커 블랙에 배정되어 검수 큐 후보가 된다."""
    counts = Counter({"블랙": 100, "화이트": 90, "검정": 2})
    vectors = {
        "블랙": [1.0, 0.0],
        "화이트": [0.0, 1.0],
        "검정": [0.884, math.sqrt(1.0 - 0.884**2)],
    }
    clusters = seed.cluster_terms(
        counts,
        lambda terms: [vectors[term] for term in terms],
        top_n=2,
        threshold=0.85,
    )
    black = next(cluster for cluster in clusters if cluster.canonical == "블랙")
    check = next(member for member in black.members if member.term == "검정")
    assert check.is_anchor is False
    assert check.nearest_anchor == "블랙"
    assert check.second_anchor == "화이트"
    assert check.margin is not None
    path = tmp_path / "review.md"
    seed.write_review_queue(
        clusters,
        path,
        threshold=0.85,
        boundary_band_width=0.01,
    )
    assert "| 검정 | 2 | 블랙 0.8840" in path.read_text(encoding="utf-8")


def test_all_term_embeddings_respect_provider_batch_limit() -> None:
    counts = Counter({f"색상-{index:03d}": 300 - index for index in range(205)})
    batches: list[int] = []

    def embed(terms: list[str]) -> list[list[float]]:
        batches.append(len(terms))
        return [[1.0, float(index + 1)] for index, _ in enumerate(terms)]

    seed.cluster_terms(counts, embed, top_n=2, threshold=2.0)
    assert batches == [20] * 10 + [5]


async def test_refine_clusters_only_removes_members_and_degrades_on_failure(caplog) -> None:
    clusters = [
        seed.Cluster(
            canonical="블랙",
            members=[
                seed.ClusterMember("블랙", 100, [1.0, 0.0], 1.0),
                seed.ClusterMember("검정", 10, [0.99, 0.01], 0.99),
            ],
        )
    ]

    class LLM:
        async def complete(self, **kwargs):
            return '{"clusters":[{"canonical":"블랙","keep":["블랙"]}]}'

    refined = await seed.refine_clusters(
        clusters,
        LLM(),
        clusters_per_call=1,
        max_tokens=2048,
    )
    assert [m.term for m in refined[0].members] == ["블랙"]
    assert refined[0].llm_status == "completed"
    assert [m.term for m in refined[0].llm_removed] == ["검정"]
    assert [m.term for m in refined[0].llm_kept] == ["블랙"]

    class BrokenLLM:
        async def complete(self, **kwargs):
            raise RuntimeError("offline")

    with caplog.at_level("WARNING"):
        degraded = await seed.refine_clusters(
            clusters,
            BrokenLLM(),
            clusters_per_call=1,
            max_tokens=2048,
        )
    assert degraded[0].llm_status == "failed"
    assert [member.term for member in degraded[0].members] == ["블랙", "검정"]
    assert "다듬기" in caplog.text


async def test_refine_clusters_isolates_each_chunk_failure_and_injects_limits() -> None:
    clusters = [
        seed.Cluster(
            canonical,
            [seed.ClusterMember(canonical, 1, [1.0], 1.0)],
        )
        for canonical in ("A", "B", "C")
    ]
    calls: list[tuple[list[str], int]] = []

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            canonicals = [item["canonical"] for item in payload]
            calls.append((canonicals, kwargs["max_tokens"]))
            if canonicals == ["B"]:
                raise RuntimeError("one chunk failed")
            return json.dumps(
                {
                    "clusters": [
                        {"canonical": item["canonical"], "keep": item["terms"]} for item in payload
                    ]
                }
            )

    refined = await seed.refine_clusters(
        clusters,
        LLM(),
        clusters_per_call=1,
        max_tokens=777,
    )
    assert calls == [(["A"], 777), (["B"], 777), (["C"], 777)]
    assert [cluster.llm_status for cluster in refined] == ["completed", "failed", "completed"]


def test_refine_cluster_tunables_are_config_injected() -> None:
    assert Settings.model_fields["color_synonym_llm_clusters_per_call"].default == 1
    assert Settings.model_fields["color_synonym_llm_max_tokens"].default == 2048


def test_review_queue_exposes_llm_audit_and_boundary_band(tmp_path) -> None:
    black = seed.ClusterMember("블랙", 2354, [1.0, 0.0], 1.0)
    navy = seed.ClusterMember("네이비", 631, [1.0, 0.0], 1.0)
    namsaek = seed.ClusterMember("남색", 8, [0.854, 0.52], 0.854)
    removed = seed.ClusterMember("블루", 438, [0.849, 0.52], 0.849)
    clusters = [
        seed.Cluster(
            "블랙",
            [black],
            llm_status="completed",
            llm_kept=(black,),
            llm_removed=(removed,),
        ),
        seed.Cluster(
            "네이비",
            [navy, namsaek],
            llm_status="failed",
        ),
    ]
    path = tmp_path / "review.md"
    seed.write_review_queue(
        clusters,
        path,
        threshold=0.85,
        boundary_band_width=0.01,
    )
    review = path.read_text(encoding="utf-8")
    assert "LLM 실행: 성공" in review
    assert "유지: 블랙" in review
    assert "제거: 블루" in review
    assert "LLM 실행: 실패" in review
    assert "남색" in review and "확인 필요" in review


def test_review_queue_shows_top_two_anchors_margin_and_modifier_warning(tmp_path) -> None:
    blue = seed.ClusterMember("블루", 438, [1.0], 1.0, is_anchor=True)
    namsaek = seed.ClusterMember(
        "남색",
        8,
        [1.0],
        0.873,
        nearest_anchor="블루",
        second_anchor="네이비",
        second_cosine=0.854,
        margin=0.019,
    )
    dark_green = seed.ClusterMember(
        "다크그린",
        22,
        [1.0],
        0.917,
        nearest_anchor="다크그레이",
        second_anchor="그린",
        second_cosine=0.916,
        margin=0.001,
    )
    cluster = seed.Cluster(
        "블루",
        [blue, namsaek, dark_green],
        llm_status="failed",
    )
    path = tmp_path / "review.md"
    seed.write_review_queue(
        [cluster],
        path,
        threshold=0.85,
        boundary_band_width=0.01,
    )
    review = path.read_text(encoding="utf-8")
    assert "수식어 토큰" in review and "색상 어근" in review
    assert "블루 0.8730" in review
    assert "네이비 0.8540" in review
    assert "0.0190" in review
    assert review.index("다크그린") < review.index("남색")


def test_upsert_sql_preserves_human_review_decisions() -> None:
    sql = seed.UPSERT_COLOR_TERM_SQL
    assert "WHEN color_synonyms.status <> 'pending_review'" in sql
    assert "THEN color_synonyms.status" in sql
    assert "THEN color_synonyms.canonical" in sql
    assert "embedding = COALESCE(EXCLUDED.embedding, color_synonyms.embedding)" in sql
    assert (
        "embedding_model = COALESCE(EXCLUDED.embedding_model, color_synonyms.embedding_model)"
        in sql
    )
    assert "doc_count = EXCLUDED.doc_count" in sql


def test_reseed_outside_top_n_keeps_existing_approved_embedding() -> None:
    rows = seed._rows_from_clusters(Counter({"남색": 8}), [])
    assert rows == [seed.ColorTermRow("남색", None, None, "seed_pipeline", 8)]
    assert "COALESCE(EXCLUDED.embedding, color_synonyms.embedding)" in seed.UPSERT_COLOR_TERM_SQL


def test_seed_connection_pool_is_reused_per_dsn(monkeypatch) -> None:
    import psycopg_pool

    created: list[tuple[str, dict]] = []

    class Pool:
        def __init__(self, dsn, **kwargs):
            created.append((dsn, kwargs))

    settings = Settings(_env_file=None, color_synonym_pool_max_size=7)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    monkeypatch.setattr(seed, "_pools", {})
    monkeypatch.setattr(seed, "get_settings", lambda: settings)
    assert seed._get_pool("postgresql://same") is seed._get_pool("postgresql://same")
    assert len(created) == 1
    assert created[0][0] == "postgresql://same"
    assert created[0][1]["max_size"] == 7


def test_batch_harvest_upserts_only_unknown_terms_as_pending_proposals(monkeypatch) -> None:
    executed: list[str] = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class Conn:
        def execute(self, sql, params=None):
            executed.append(sql)
            if "WHERE term = ANY" in sql:
                return Result([("블랙",)])
            return Result([("네이비", 0.91)])

        def transaction(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Pool:
        def __init__(self, *args, **kwargs):
            pass

        def connection(self):
            return Conn()

        def close(self):
            pass

    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    monkeypatch.setattr(seed, "_pools", {})
    captured = []
    monkeypatch.setattr(
        seed,
        "_execute_color_term_upserts",
        lambda conn, rows, model: captured.extend(rows) or len(rows),
    )

    count = seed.harvest_new_terms(
        "dsn",
        {"색상": ["블랙", "남색", "남색", "기타"]},
        lambda terms: [[1.0, 0.0] for _ in terms],
        "model",
        0.84,
    )
    assert count == 2
    assert captured == [
        seed.ColorTermRow("남색", "네이비", [1.0, 0.0], "batch_harvest", 1),
        seed.ColorTermRow("기타", None, None, "batch_harvest", 1),
    ]
    assert executed[0] == "SET LOCAL statement_timeout = 2500"
    assert executed[2] == "SET LOCAL statement_timeout = 2500"
    assert "'approved'" in executed[3]
    assert "'pending_review'" in seed.UPSERT_COLOR_TERM_SQL


def test_batch_harvest_releases_db_connection_before_embedding(monkeypatch) -> None:
    active_connections = 0
    max_active_connections = 0

    class Result:
        def __init__(self, rows=()):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Conn:
        def execute(self, sql, params=None):
            if "WHERE term = ANY" in sql:
                return Result()
            if "SELECT canonical" in sql:
                return Result([("네이비", 0.91)])
            return Result()

        def transaction(self):
            return Transaction()

        def __enter__(self):
            nonlocal active_connections, max_active_connections
            active_connections += 1
            max_active_connections = max(max_active_connections, active_connections)
            return self

        def __exit__(self, *args):
            nonlocal active_connections
            active_connections -= 1
            return False

    class Pool:
        def connection(self):
            return Conn()

    def embed(terms):
        assert active_connections == 0
        return [[1.0, 0.0] for _ in terms]

    monkeypatch.setattr(seed, "_get_pool", lambda dsn: Pool())

    assert seed.harvest_new_terms(
        "dsn",
        {"색상": "남색"},
        embed,
        "model",
        0.84,
    ) == 1
    assert active_connections == 0
    assert max_active_connections == 1


def test_batch_harvest_embeds_many_colors_in_shared_chunks_without_loss(monkeypatch) -> None:
    terms = [f"색상-{index}" for index in range(45)]
    embed_calls: list[list[str]] = []
    captured: list[seed.ColorTermRow] = []

    class Result:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Conn:
        def execute(self, sql, params=None):
            return Result()

        def transaction(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Pool:
        def connection(self):
            return Conn()

    def embed(batch):
        embed_calls.append(batch)
        return [[float(index), 0.0] for index, _ in enumerate(batch)]

    monkeypatch.setattr(seed, "_get_pool", lambda dsn: Pool())
    monkeypatch.setattr(
        seed,
        "_execute_color_term_upserts",
        lambda conn, rows, model: captured.extend(rows) or len(rows),
    )

    assert seed.harvest_new_terms("dsn", {"색상": terms}, embed, "model", 0.84) == 45
    assert [len(batch) for batch in embed_calls] == [20, 20, 5]
    assert [row.term for row in captured] == terms


def test_color_synonym_app_timeout_must_be_below_db_timeout() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(
            _env_file=None,
            catalog_store_query_timeout_s=2.0,
            home_reco_store_timeout_s=1.0,
            color_synonym_query_timeout_s=2.0,
        )


async def test_build_offloads_all_blocking_stages_from_event_loop(monkeypatch, tmp_path) -> None:
    import threading

    loop_thread = threading.get_ident()
    stage_threads: dict[str, int] = {}

    async def harvest(*args, **kwargs):
        return Counter({"블랙": 1})

    def cluster(*args, **kwargs):
        stage_threads["cluster"] = threading.get_ident()
        return []

    async def refine(clusters, *args, **kwargs):
        return clusters

    def write(*args, **kwargs):
        stage_threads["write"] = threading.get_ident()

    def upsert(*args, **kwargs):
        stage_threads["upsert"] = threading.get_ident()
        return 1

    monkeypatch.setattr(seed, "harvest_terms", harvest)
    monkeypatch.setattr(seed, "cluster_terms", cluster)
    monkeypatch.setattr(seed, "refine_clusters", refine)
    monkeypatch.setattr(seed, "write_review_queue", write)
    monkeypatch.setattr(seed, "upsert_color_terms", upsert)

    result = await seed.build(
        "dsn",
        tmp_path / "review.md",
        embed=lambda terms: [],
        llm=object(),
    )

    assert result.upserted_rows == 1
    assert set(stage_threads) == {"cluster", "write", "upsert"}
    assert all(thread_id != loop_thread for thread_id in stage_threads.values())
