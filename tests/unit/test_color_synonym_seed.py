"""색상 동의어 오프라인 구축 파이프라인 회귀 테스트 (이슈 #258)."""

from __future__ import annotations

from collections import Counter
import json

import pytest

from app.core import config
from app.core.config import Settings
from app.pipelines import color_synonyms
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
        ({"색상": [" 핑크 ", "", "  ", "레드", "핑크", None]}, ["핑크", "레드"]),
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


def test_color_term_count_limit_applies_after_order_preserving_dedup() -> None:
    terms = seed.extract_color_terms(
        {"색상": ["레드"] * 39 + ["블루", "그린"]},
        max_terms=40,
        max_term_length=40,
    )

    assert terms == ["레드", "블루", "그린"]


def test_color_term_scan_budget_stops_before_processing_abusive_tail() -> None:
    class ExplodingStr(str):
        def strip(self, chars=None):
            raise AssertionError("scan budget 밖 값을 정규화하면 안 됨")

    terms = seed.extract_color_terms(
        {"색상": ["레드"] * 99 + ["블루", ExplodingStr("폭발")]},
        max_terms=40,
        max_term_length=40,
        scan_max_values=100,
    )

    assert terms == ["레드", "블루"]


def test_color_term_count_limit_caps_offline_harvest_and_logs(caplog) -> None:
    with caplog.at_level("WARNING"):
        counts = seed.count_terms(
            [_change(1, ["블랙", "화이트", "레드"])],
            max_terms=2,
            max_term_length=40,
        )

    assert counts == Counter({"블랙": 1, "화이트": 1})
    assert "색상 표기 개수 상한 초과" in caplog.text
    assert "1건 제외" in caplog.text
    assert "레드" in caplog.text


def test_color_term_length_limit_rejects_and_logs(caplog) -> None:
    with caplog.at_level("WARNING"):
        terms = seed.extract_color_terms(
            {"색상": ["블랙", "가" * 41]},
            max_terms=40,
            max_term_length=40,
        )

    assert terms == ["블랙"]
    assert "색상 표기 문자열 길이 상한 초과" in caplog.text
    assert "1건 거부" in caplog.text


def test_measured_normal_color_boundaries_pass_unchanged_without_warning(caplog) -> None:
    settings = Settings(_env_file=None)
    terms = [f"{index:02d}" + ("가" * 26) for index in range(30)]

    with caplog.at_level("WARNING"):
        extracted = seed.extract_color_terms(
            {"색상": terms},
            max_terms=settings.color_synonym_harvest_max_terms_per_product,
            max_term_length=settings.color_synonym_harvest_max_term_length,
        )

    assert all(len(term) == 28 for term in terms)
    assert extracted == terms
    assert "색상 표기" not in caplog.text


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


def _group_members(result) -> dict[str, set[str]]:
    return {
        cluster.canonical: {member.term for member in cluster.members}
        for cluster in result.clusters
    }


async def test_llm_assignment_json_mode_prompts_explicitly_request_json() -> None:
    counts = Counter({"블랙": 100, "검정": 10})

    class LLM:
        async def complete(self, **kwargs):
            assert kwargs["json_output"] is True
            assert "JSON" in kwargs["system"]
            payload = json.loads(kwargs["user"])
            if payload["stage"] == "anchors":
                return '{"groups":[{"canonical":"블랙","members":["블랙"]}]}'
            return '{"assignments":[{"term":"검정","canonical":"블랙"}]}'

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=1,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )

    assert _group_members(result) == {"블랙": {"블랙", "검정"}}


async def test_llm_assignment_rejects_hallucinated_terms_exactly() -> None:
    counts = Counter({"블랙": 100, "화이트": 90, "검정": 10})

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["stage"] == "anchors":
                return json.dumps(
                    {
                        "groups": [
                            {"canonical": "블랙", "members": ["블랙"]},
                            {"canonical": "화이트", "members": ["화이트"]},
                        ]
                    }
                )
            return json.dumps(
                {
                    "assignments": [
                        {"term": "검정", "canonical": "블랙"},
                        {"term": "오white", "canonical": "화이트"},
                    ]
                },
                ensure_ascii=False,
            )

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=2,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )
    assert "검정" in _group_members(result)["블랙"]
    assert all("오white" not in members for members in _group_members(result).values())
    assert any(
        "환각" in rejection.reason and "오white" in rejection.terms
        for rejection in result.rejections
    )


async def test_llm_assignment_rejects_duplicate_exclusive_assignments() -> None:
    counts = Counter({"블랙": 100, "화이트": 90, "크림": 10})

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["stage"] == "anchors":
                return '{"groups":[{"canonical":"블랙","members":["블랙"]},{"canonical":"화이트","members":["화이트"]}]}'
            return '{"assignments":[{"term":"크림","canonical":"블랙"},{"term":"크림","canonical":"화이트"}]}'

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=2,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )
    assert all("크림" not in members for members in _group_members(result).values())
    assert any(item.term == "크림" and "배타성" in item.reason for item in result.unassigned)
    assert any("배타성" in rejection.reason for rejection in result.rejections)


@pytest.mark.parametrize(
    "anchor_groups",
    [
        [{"canonical": "없는앵커", "members": ["블랙"]}],
        [{"canonical": "블랙", "members": ["화이트"]}],
    ],
)
async def test_llm_anchor_groups_require_valid_canonical_and_self_membership(anchor_groups) -> None:
    counts = Counter({"블랙": 100, "화이트": 90})

    class LLM:
        async def complete(self, **kwargs):
            return json.dumps({"groups": anchor_groups}, ensure_ascii=False)

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=2,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )
    assert _group_members(result) == {"블랙": {"블랙"}, "화이트": {"화이트"}}
    assert any("canonical" in rejection.reason for rejection in result.rejections)


async def test_llm_anchor_groups_reject_cycles() -> None:
    counts = Counter({"화이트": 100, "아이보리": 90})

    class LLM:
        async def complete(self, **kwargs):
            return (
                '{"groups":[{"canonical":"화이트","members":["화이트","아이보리"]},'
                '{"canonical":"아이보리","members":["아이보리","화이트"]}]}'
            )

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=2,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )
    assert _group_members(result) == {"화이트": {"화이트"}, "아이보리": {"아이보리"}}
    assert any("순환" in rejection.reason for rejection in result.rejections)


async def test_llm_assignment_never_sends_or_groups_sentinels() -> None:
    counts = Counter({"혼합색상": 1000, "블랙": 100, "화이트": 90, "검정": 10})
    payloads = []

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            payloads.append(payload)
            if payload["stage"] == "anchors":
                return '{"groups":[{"canonical":"블랙","members":["블랙"]},{"canonical":"화이트","members":["화이트"]}]}'
            return '{"assignments":[{"term":"검정","canonical":"블랙"}]}'

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=2,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )
    assert "혼합색상" not in json.dumps(payloads, ensure_ascii=False)
    assert all("혼합색상" not in members for members in _group_members(result).values())
    assert any(item.term == "혼합색상" and "sentinel" in item.reason for item in result.unassigned)


async def test_llm_assignment_isolates_failed_term_chunk_as_unassigned() -> None:
    counts = Counter({"블랙": 100, "화이트": 90, "검정": 10, "오프화이트": 9})

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["stage"] == "anchors":
                return '{"groups":[{"canonical":"블랙","members":["블랙"]},{"canonical":"화이트","members":["화이트"]}]}'
            if payload["terms"] == ["검정"]:
                raise RuntimeError("one chunk failed")
            return '{"assignments":[{"term":"오프화이트","canonical":"화이트"}]}'

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=2,
        terms_per_call=1,
        threshold=0.85,
        max_tokens=512,
    )
    assert "오프화이트" in _group_members(result)["화이트"]
    assert any(item.term == "검정" and "LLM 실패" in item.reason for item in result.unassigned)
    assert all("검정" not in members for members in _group_members(result).values())
    failed_row = next(row for row in seed._rows_from_result(counts, result) if row.term == "검정")
    assert failed_row.preserve_existing_canonical is True


async def test_llm_none_clears_previous_pending_canonical_proposal() -> None:
    counts = Counter({"블랙": 100, "스킨": 10})

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["stage"] == "anchors":
                return '{"groups":[{"canonical":"블랙","members":["블랙"]}]}'
            return '{"assignments":[{"term":"스킨","canonical":null}]}'

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [[1.0, 0.0] for _ in terms],
        LLM(),
        top_n=1,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )

    none_row = next(row for row in seed._rows_from_result(counts, result) if row.term == "스킨")
    assert none_row.canonical is None
    assert none_row.preserve_existing_canonical is False
    assert none_row.provenance == "seed_llm_assignment"


async def test_embedding_only_flags_llm_assignment_disagreement_for_review(tmp_path) -> None:
    counts = Counter({"네이비": 100, "블루": 90, "남색": 10})
    vectors = {"네이비": [1.0, 0.0], "블루": [0.0, 1.0], "남색": [0.0, 1.0]}

    class LLM:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["stage"] == "anchors":
                return '{"groups":[{"canonical":"네이비","members":["네이비"]},{"canonical":"블루","members":["블루"]}]}'
            return '{"assignments":[{"term":"남색","canonical":"네이비"}]}'

    result = await seed.assign_color_clusters(
        counts,
        lambda terms: [vectors[term] for term in terms],
        LLM(),
        top_n=2,
        terms_per_call=20,
        threshold=0.85,
        max_tokens=512,
    )
    navy = next(cluster for cluster in result.clusters if cluster.canonical == "네이비")
    namsaek = next(member for member in navy.members if member.term == "남색")
    assert namsaek.nearest_anchor == "블루"
    assert namsaek.cosine == pytest.approx(0.0)
    assert namsaek.review_required is True
    assert result.embedding_mismatch_count == 1

    path = tmp_path / "review.md"
    seed.write_review_queue(
        result,
        path,
        threshold=0.85,
    )
    review = path.read_text(encoding="utf-8")
    assert "LLM=네이비" in review
    assert "임베딩1위=블루 1.0000" in review
    assert "확인 필요" in review


def test_review_queue_sanitizes_seller_control_characters_without_hiding_text(tmp_path) -> None:
    canonical = "블루|확인됨\n가짜"
    nearest = "네이비|위조\n열"
    member = seed.ClusterMember(
        "악성|표기\n확인 필요 (위조)\x01",
        1,
        [1.0, 0.0],
        0.0,
        nearest_anchor=nearest,
        review_required=True,
        review_reasons=("셀러|사유\n위조\x02",),
    )
    result = seed.ClusteringResult(
        (seed.Cluster(canonical, [member]),),
        (),
        (),
        {
            canonical: [1.0, 0.0],
            nearest: [1.0, 0.0],
        },
    )
    path = tmp_path / "review.md"

    seed.write_review_queue(result, path, threshold=0.85)

    review = path.read_text(encoding="utf-8")
    member_row = next(line for line in review.splitlines() if line.startswith("| 악성"))
    assert member_row.count("|") == 8
    assert "악성&#124;표기<br>확인 필요 (위조)\\x01" in member_row
    assert "블루&#124;확인됨<br>가짜" in member_row
    assert "네이비&#124;위조<br>열" in member_row
    assert "셀러&#124;사유<br>위조\\x02" in member_row
    assert "\n가짜" not in review


def test_llm_assignment_tunables_are_config_injected() -> None:
    assert Settings.model_fields["color_synonym_llm_clusters_per_call"].default == 1
    assert Settings.model_fields["color_synonym_llm_max_tokens"].default == 2048


def test_upsert_sql_preserves_human_review_decisions() -> None:
    sql = seed.UPSERT_COLOR_TERM_SQL
    assert "WHEN color_synonyms.status <> 'pending_review'" in sql
    assert "THEN color_synonyms.status" in sql
    assert "THEN color_synonyms.canonical" in sql
    assert "WHEN %s THEN color_synonyms.canonical" in sql
    assert "embedding = COALESCE(EXCLUDED.embedding, color_synonyms.embedding)" in sql
    assert (
        "embedding_model = COALESCE(EXCLUDED.embedding_model, color_synonyms.embedding_model)"
        in sql
    )
    assert "color_synonyms.provenance = 'seed_llm_assignment'" in sql
    assert "EXCLUDED.provenance = 'batch_embedding_unverified'" in sql
    assert "THEN color_synonyms.doc_count" in sql


def test_upsert_transports_failed_evaluation_canonical_preservation_flag() -> None:
    calls = []

    class Conn:
        def execute(self, sql, params):
            calls.append((sql, params))

    row = seed.ColorTermRow(
        "남색",
        None,
        [1.0, 0.0],
        "seed_llm_assignment",
        8,
        preserve_existing_canonical=True,
    )

    assert seed._execute_color_term_upserts(Conn(), [row], "model") == 1
    assert calls[0][1][-1] is True


def test_seed_connection_pool_is_reused_per_dsn(monkeypatch) -> None:
    import psycopg_pool

    from app.core import pg_resilience

    created: list[tuple[str, dict]] = []

    class Pool:
        def __init__(self, dsn, **kwargs):
            created.append((dsn, kwargs))

    settings = Settings(_env_file=None, color_synonym_pool_max_size=7)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    monkeypatch.setattr(color_synonyms, "_pools", {})
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    # 이 테스트는 dsn 별 풀 재사용(캐시 키)을 확인한다 — hardened_pg_conninfo 가 실제 접속
    # 문자열에 무엇을 병합하는지는 app/core/test_pg_resilience.py 소관이라 항등으로 둔다.
    monkeypatch.setattr(pg_resilience, "hardened_pg_conninfo", lambda dsn: dsn)
    assert seed._get_pool("postgresql://same") is seed._get_pool("postgresql://same")
    assert len(created) == 1
    assert created[0][0] == "postgresql://same"
    assert created[0][1]["max_size"] == 7


def test_seed_connection_pool_accepts_configured_boundary_size_two(monkeypatch) -> None:
    import psycopg_pool

    real_pool = psycopg_pool.ConnectionPool

    def closed_pool(dsn, **kwargs):
        return real_pool(dsn, **{**kwargs, "open": False})

    settings = Settings(
        _env_file=None,
        color_synonym_pool_max_size=2,
        color_synonym_harvest_max_concurrency=1,
    )
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", closed_pool)
    monkeypatch.setattr(color_synonyms, "_pools", {})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    pool = seed._get_pool("postgresql://example.invalid/catalog")
    try:
        assert pool.min_size <= pool.max_size == 2
    finally:
        pool.close()


def test_batch_harvest_upserts_only_unknown_terms_as_pending_proposals(monkeypatch) -> None:
    executed: list[tuple[str, object]] = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class Conn:
        def execute(self, sql, params=None):
            executed.append((sql, params))
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

    from app.core import pg_resilience

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    monkeypatch.setattr(color_synonyms, "_pools", {})
    # ConnectionPool 이 모킹돼 있어 실제 접속 문자열 형식은 이 테스트의 관심사가 아니다 —
    # 항등으로 두어 dsn 리터럴 "dsn"이 hardened_pg_conninfo 의 conninfo 파서를 타지 않게 한다.
    monkeypatch.setattr(pg_resilience, "hardened_pg_conninfo", lambda dsn: dsn)
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
        seed.ColorTermRow(
            "남색",
            "네이비",
            [1.0, 0.0],
            "batch_embedding_unverified",
            1,
        ),
        seed.ColorTermRow(
            "기타",
            None,
            None,
            "batch_embedding_unverified",
            1,
        ),
    ]
    assert executed[0][0] == "SET LOCAL statement_timeout = 2500"
    assert executed[2][0] == "SET LOCAL statement_timeout = 2500"
    assert "'approved'" in executed[3][0]
    assert "embedding_model = %s" in executed[3][0]
    assert executed[3][1][1] == "model"
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

    assert (
        seed.harvest_new_terms(
            "dsn",
            {"색상": "남색"},
            embed,
            "model",
            0.84,
        )
        == 1
    )
    assert active_connections == 0
    assert max_active_connections == 1


def test_batch_harvest_embeds_many_colors_within_configured_limit_without_loss(
    monkeypatch,
) -> None:
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

    assert (
        seed.harvest_new_terms(
            "dsn",
            {"색상": terms},
            embed,
            "model",
            0.84,
            max_terms=45,
            max_term_length=40,
        )
        == 45
    )
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

    def embed(terms):
        stage_threads["embed"] = threading.get_ident()
        return [[1.0] for _ in terms]

    class LLM:
        async def complete(self, **kwargs):
            return '{"groups":[{"canonical":"블랙","members":["블랙"]}]}'

    def write(*args, **kwargs):
        stage_threads["write"] = threading.get_ident()

    def upsert(*args, **kwargs):
        stage_threads["upsert"] = threading.get_ident()
        return 1

    monkeypatch.setattr(seed, "harvest_terms", harvest)
    monkeypatch.setattr(seed, "write_review_queue", write)
    monkeypatch.setattr(seed, "upsert_color_terms", upsert)

    result = await seed.build(
        "dsn",
        tmp_path / "review.md",
        embed=embed,
        llm=LLM(),
    )

    assert result.upserted_rows == 1
    assert set(stage_threads) == {"embed", "write", "upsert"}
    assert all(thread_id != loop_thread for thread_id in stage_threads.values())


# --- 정본 시드 적재 (`load_seed_rows`/`seed_from_file`, 이슈 #258 §4.5) --------------------


def _write_seed_json(tmp_path, rows: list[dict]):
    path = tmp_path / "color_synonyms.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_seed_rows_parses_valid_file(tmp_path) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 2358,
            },
            {
                "term": "그린",
                "canonical": "그린",
                "status": "approved",
                "provenance": "human",
                "doc_count": 287,
            },
            {
                "term": "다크그린",
                "canonical": "그린",
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 22,
            },
        ],
    )

    rows = seed.load_seed_rows(path)

    assert rows == [
        seed.SeedColorTermRow("블랙", "블랙", "approved", "human", 2358),
        seed.SeedColorTermRow("그린", "그린", "approved", "human", 287),
        seed.SeedColorTermRow("다크그린", "그린", "pending_review", "seed_llm_assignment", 22),
    ]


def test_load_seed_rows_rejects_non_array_root(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")

    with pytest.raises(ValueError, match="배열"):
        seed.load_seed_rows(path)


@pytest.mark.parametrize(
    "row",
    [
        {"canonical": "블랙", "status": "approved", "provenance": "human", "doc_count": 1},
        {"term": "블랙", "canonical": "블랙", "provenance": "human", "doc_count": 1},
        {"term": "블랙", "canonical": "블랙", "status": "approved", "doc_count": 1},
    ],
)
def test_load_seed_rows_rejects_missing_required_keys(tmp_path, row) -> None:
    """term·status·provenance는 필수(nullable 아님) — 없으면 거부한다."""
    path = _write_seed_json(tmp_path, [row])

    with pytest.raises(ValueError):
        seed.load_seed_rows(path)


def test_load_seed_rows_accepts_missing_nullable_canonical_and_doc_count(tmp_path) -> None:
    """canonical·doc_count는 DB 스키마상 nullable — 키 자체가 없어도 None 으로 로드된다."""
    path = _write_seed_json(
        tmp_path, [{"term": "미상토큰", "status": "pending_review", "provenance": "human"}]
    )

    rows = seed.load_seed_rows(path)

    assert rows == [seed.SeedColorTermRow("미상토큰", None, "pending_review", "human", None)]


def test_load_seed_rows_rejects_status_outside_enum(tmp_path) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "확정",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="status"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_provenance_outside_enum(tmp_path) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "사람이_그냥_넣음",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="provenance"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_approved_row_with_empty_string_canonical(tmp_path) -> None:
    """빈 문자열은 WHERE canonical IS NOT NULL 을 통과해 build_synonym_map 이 ""를 키로
    무관한 승인 행들을 한 묶음으로 잘못 합친다(PR #447 리뷰 R1) — canonical 없음은 null 로만."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "",
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="canonical"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_approved_row_with_null_canonical(tmp_path) -> None:
    """status=approved 인데 canonical 이 없으면 build_synonym_map 필터에 걸려 조용히
    사전에서 빠진다 — 검수자는 승인했다고 믿는데 런타임엔 존재하지 않는 모순 상태다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": None,
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="canonical"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_empty_string_canonical_regardless_of_status(tmp_path) -> None:
    """빈 문자열은 status 와 무관하게 거부한다 — approved 만이 아니라 pending_review 도 대상."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "",
                "status": "pending_review",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="canonical"):
        seed.load_seed_rows(path)


def test_load_seed_rows_accepts_pending_review_row_with_explicit_null_canonical(tmp_path) -> None:
    """정본 789행 중 190행이 이 상태(canonical=None, status=pending_review) — 여기서
    막으면 실제 정본이 로드 불가가 된다(정상 경로 회귀 방지)."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "미상토큰",
                "canonical": None,
                "status": "pending_review",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    rows = seed.load_seed_rows(path)

    assert rows == [seed.SeedColorTermRow("미상토큰", None, "pending_review", "human", 1)]


def test_load_seed_rows_rejects_duplicate_term_with_conflicting_fields(tmp_path) -> None:
    """`_execute_seed_upserts`가 rows 순서대로 ON CONFLICT (term) DO UPDATE 를 실행하므로,
    같은 term 이 두 번(다른 status/canonical) 있으면 배열 순서에 좌우돼 나중 행이 앞 행을
    조용히 덮는다(PR #447 리뷰 R2)."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 100,
            },
            {
                "term": "블랙",
                "canonical": None,
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 1,
            },
        ],
    )

    with pytest.raises(ValueError, match="term 중복"):
        seed.load_seed_rows(path)


def test_load_seed_rows_accepts_distinct_terms(tmp_path) -> None:
    """정상 회귀 — 서로 다른 term 이면 중복 가드가 발화하지 않는다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 100,
            },
            {
                "term": "화이트",
                "canonical": "화이트",
                "status": "approved",
                "provenance": "human",
                "doc_count": 90,
            },
        ],
    )

    rows = seed.load_seed_rows(path)

    assert [row.term for row in rows] == ["블랙", "화이트"]


def test_load_seed_rows_rejects_canonical_not_present_as_a_term(tmp_path) -> None:
    """canonical이 파일 안에 실재하는 term을 가리키지 않으면(앵커 누락·오타) build_synonym_map
    이 그 canonical을 그룹 키로 못 찾아 "승인이 조용히 아무 일도 하지 않는" 상태가 된다
    (PR #447 리뷰 R4)."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "남색",
                "canonical": "네이비",
                "status": "approved",
                "provenance": "human",
                "doc_count": 8,
            }
        ],
    )

    with pytest.raises(ValueError, match="term을 가리키지 않음"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_orphan_approval(tmp_path) -> None:
    """approved 행의 canonical 행이 approved가 아니면(pending_review) 고아 승인이다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "남색",
                "canonical": "네이비",
                "status": "approved",
                "provenance": "human",
                "doc_count": 8,
            },
            {
                "term": "네이비",
                "canonical": "네이비",
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 632,
            },
        ],
    )

    with pytest.raises(ValueError, match="고아 승인"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_two_step_canonical_chain(tmp_path) -> None:
    """X→Y→Z 2단계 체인 — status와 무관하게 X의 확장이 조용히 무력화된다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "X",
                "canonical": "Y",
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 1,
            },
            {
                "term": "Y",
                "canonical": "Z",
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 1,
            },
            {
                "term": "Z",
                "canonical": "Z",
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            },
        ],
    )

    with pytest.raises(ValueError, match="2단계 체인/순환 금지"):
        seed.load_seed_rows(path)


def test_load_seed_rows_accepts_pending_review_row_pointing_at_approved_anchor(tmp_path) -> None:
    """정상 회귀 — 정본 실제 형태(예: 다크그린→그린): pending_review 행이 approved 앵커를
    canonical로 가리키면 통과해야 한다. 여기서 막으면 정본 743행 중 상당수가 로드 불가가 된다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "다크그린",
                "canonical": "그린",
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 22,
            },
            {
                "term": "그린",
                "canonical": "그린",
                "status": "approved",
                "provenance": "human",
                "doc_count": 287,
            },
        ],
    )

    rows = seed.load_seed_rows(path)

    assert {row.term for row in rows} == {"다크그린", "그린"}


def test_load_seed_rows_rejects_term_with_leading_or_trailing_whitespace(tmp_path) -> None:
    """생성물은 바이트 정본이라 로더가 조용히 strip 하면 파일 값과 DB 적재 값이 갈라진다
    (PR #447 리뷰 R5) — 정규화하지 않고 거부한다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": " 블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="term에 앞뒤 공백"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_canonical_with_leading_or_trailing_whitespace(tmp_path) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙 ",
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="canonical에 앞뒤 공백"):
        seed.load_seed_rows(path)


def test_load_seed_rows_rejects_term_with_embedded_newline(tmp_path) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙\n",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="term에 앞뒤 공백"):
        seed.load_seed_rows(path)


def test_load_seed_rows_accepts_term_and_canonical_without_whitespace(tmp_path) -> None:
    """정상 회귀 — 공백 없는 정상 행은 그대로 통과한다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 1,
            }
        ],
    )

    rows = seed.load_seed_rows(path)

    assert rows == [seed.SeedColorTermRow("블랙", "블랙", "approved", "human", 1)]


def test_load_seed_rows_rejects_norm_collision_between_distinct_terms(tmp_path) -> None:
    """casefold 만 다른 두 원문 term(예: Walnut/WALNUT)이 둘 다 self-canonical approved 로
    통과하면 build_synonym_map 이 `mapping[_norm(term)]` 키를 나중 term 으로 조용히 덮어써
    먼저 term 의 동의어 목록이 사라진다(PR #447 리뷰 R6) — 원문 완전일치(seen_terms)와는
    별개 불변식이라 둘 다 검사해야 한다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "Walnut",
                "canonical": "Walnut",
                "status": "approved",
                "provenance": "human",
                "doc_count": 5,
            },
            {
                "term": "WALNUT",
                "canonical": "WALNUT",
                "status": "approved",
                "provenance": "human",
                "doc_count": 3,
            },
        ],
    )

    with pytest.raises(ValueError, match="_norm 기준 term 충돌"):
        seed.load_seed_rows(path)


def test_load_seed_rows_accepts_terms_with_distinct_norm_values(tmp_path) -> None:
    """정상 회귀 — `_norm` 이 서로 다른 term 들은 통과해야 한다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 100,
            },
            {
                "term": "Walnut",
                "canonical": "Walnut",
                "status": "approved",
                "provenance": "human",
                "doc_count": 5,
            },
        ],
    )

    rows = seed.load_seed_rows(path)

    assert {row.term for row in rows} == {"블랙", "Walnut"}


def test_load_seed_rows_exact_duplicate_term_still_raises_seen_terms_message(tmp_path) -> None:
    """원문 완전 중복은 여전히 기존 "term 중복" 메시지로 잡혀야 한다(R2 회귀 방지) —
    _norm 충돌 검사를 추가해도 seen_terms 검사가 먼저 발화한다."""
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 100,
            },
            {
                "term": "블랙",
                "canonical": None,
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 1,
            },
        ],
    )

    with pytest.raises(ValueError, match="term 중복"):
        seed.load_seed_rows(path)


def test_upsert_seed_sql_is_authoritative_not_review_protected() -> None:
    """검수 보호 CASE 가드(UPSERT_COLOR_TERM_SQL)와 달리, 시드 upsert는 파일 값을 그대로 반영한다."""
    sql = seed.UPSERT_SEED_COLOR_TERM_SQL
    assert "canonical = EXCLUDED.canonical" in sql
    assert "status = EXCLUDED.status" in sql
    assert "provenance = EXCLUDED.provenance" in sql
    assert "doc_count = EXCLUDED.doc_count" in sql
    assert "CASE" not in sql
    assert "embedding = COALESCE(EXCLUDED.embedding, color_synonyms.embedding)" in sql


def test_execute_seed_upserts_passes_file_values_straight_through() -> None:
    calls = []

    class Conn:
        def execute(self, sql, params):
            calls.append((sql, params))

    rows = [seed.SeedColorTermRow("곤색", "네이비", "approved", "human", 5)]

    count = seed._execute_seed_upserts(Conn(), rows, {}, "model")

    assert count == 1
    sql, params = calls[0]
    assert sql is seed.UPSERT_SEED_COLOR_TERM_SQL
    assert params == ("곤색", "네이비", "approved", None, None, "human", 5)


def test_seed_from_file_upserts_authoritative_rows_and_skips_non_color_embedding(
    monkeypatch, tmp_path
) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 2358,
            },
            {
                "term": "기타",
                "canonical": None,
                "status": "pending_review",
                "provenance": "seed_llm_assignment",
                "doc_count": 70,
            },
        ],
    )

    embed_calls: list[list[str]] = []

    def embed(terms):
        embed_calls.append(list(terms))
        return [[1.0, 0.0] for _ in terms]

    captured: list[seed.SeedColorTermRow] = []

    def fake_seed_color_terms(dsn, rows, embeddings, model):
        captured.extend(rows)
        assert "기타" not in embeddings
        assert "블랙" in embeddings
        return len(rows)

    monkeypatch.setattr(seed, "seed_color_terms", fake_seed_color_terms)

    count = seed.seed_from_file(path, "dsn", embed=embed, model="test-model")

    assert count == 2
    assert embed_calls == [["블랙"]]
    assert {row.term for row in captured} == {"블랙", "기타"}


def test_seed_from_file_does_not_call_embed_on_empty_seed_file(monkeypatch, tmp_path) -> None:
    path = _write_seed_json(tmp_path, [])
    embed_calls = []

    def embed(terms):
        embed_calls.append(terms)
        return []

    monkeypatch.setattr(seed, "seed_color_terms", lambda *args, **kwargs: 0)

    count = seed.seed_from_file(path, "dsn", embed=embed, model="test-model")

    assert count == 0
    assert embed_calls == []


def test_seed_from_file_is_idempotent_across_repeated_runs(monkeypatch, tmp_path) -> None:
    path = _write_seed_json(
        tmp_path,
        [
            {
                "term": "블랙",
                "canonical": "블랙",
                "status": "approved",
                "provenance": "human",
                "doc_count": 2358,
            }
        ],
    )

    def embed(terms):
        return [[1.0, 0.0] for _ in terms]

    calls: list[tuple] = []
    monkeypatch.setattr(
        seed,
        "seed_color_terms",
        lambda dsn, rows, embeddings, model: calls.append((rows, embeddings, model)) or len(rows),
    )

    seed.seed_from_file(path, "dsn", embed=embed, model="test-model")
    seed.seed_from_file(path, "dsn", embed=embed, model="test-model")

    assert calls[0] == calls[1]
