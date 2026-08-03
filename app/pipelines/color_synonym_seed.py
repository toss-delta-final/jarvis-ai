"""색상 동의어 검수 큐를 만드는 오프라인 구축 파이프라인 (이슈 #258)."""

from __future__ import annotations

import functools
import json
import logging
import math
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.llm import LLMClient, get_llm
from app.pipelines.embedding import embed_texts as _embed_texts
from app.schemas.spring import ProductChange, ProductChangesPage
from app.services import spring_client

_log = logging.getLogger(__name__)
_pools: dict[str, object] = {}
_pool_lock = threading.Lock()

NON_COLOR_TERMS = frozenset(
    {"혼합색상", "기타", "투명", "멀티컬러", "해당없음", "멀티(리버서블)", "멀티(혼합)"}
)
EmbedFn = Callable[[list[str]], list[list[float]]]
FetchFn = Callable[[str | None, int], Awaitable[ProductChangesPage]]
# Google 서버 상한(100)보다 작게 나눠 기존 3초 API timeout 안에 전체 꼬리를 처리한다.
_EMBED_BATCH_SIZE = 20


@dataclass(frozen=True)
class ClusterMember:
    term: str
    doc_count: int
    embedding: list[float]
    cosine: float
    is_anchor: bool = False
    nearest_anchor: str | None = None
    second_anchor: str | None = None
    second_cosine: float | None = None
    margin: float | None = None


@dataclass(frozen=True)
class Cluster:
    canonical: str
    members: list[ClusterMember]
    llm_status: str = "not_run"
    llm_kept: tuple[ClusterMember, ...] = ()
    llm_removed: tuple[ClusterMember, ...] = ()


@dataclass(frozen=True)
class ColorTermRow:
    term: str
    canonical: str | None
    embedding: list[float] | None
    provenance: str
    doc_count: int


@dataclass(frozen=True)
class BuildResult:
    harvested_terms: int
    cluster_count: int
    llm_adjustments: int
    upserted_rows: int
    review_queue_path: str


def extract_color_terms(attributes: dict | None) -> list[str]:
    """혼재하는 attributes[색상] str/list/null/부재를 정규화해 비어 있지 않은 표기만 반환."""
    if not isinstance(attributes, dict):
        return []
    raw = attributes.get("색상")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def count_terms(changes: Iterable[ProductChange]) -> Counter[str]:
    """각 상품 안의 중복 표기는 한 번만 세어 표기별 상품 수를 집계한다."""
    counts: Counter[str] = Counter()
    for change in changes:
        counts.update(set(extract_color_terms(change.attributes)))
    return counts


async def harvest_terms(
    *, fetch: FetchFn = spring_client.fetch_product_changes, page_size: int = 500
) -> Counter[str]:
    """I-17을 since=0부터 소진하며 enrichment 없이 색상 표기만 수확한다."""
    cursor: str | None = "0"
    counts: Counter[str] = Counter()
    while True:
        page = await fetch(cursor, page_size)
        counts.update(count_terms(page.items))
        if not page.has_more:
            return counts
        if not page.next_cursor:
            _log.warning("색상 수확 중 hasMore=True 이나 nextCursor 없음 — 무한루프 방지 중단")
            return counts
        cursor = page.next_cursor


def load_terms(path: str | Path) -> Counter[str]:
    """오프라인 JSON 빈도 맵 또는 문자열 배열을 엄격히 로드한다."""
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, list):
        if not all(isinstance(term, str) and term.strip() for term in data):
            raise ValueError(f"색상 표기 배열은 비어 있지 않은 문자열이어야 함: {source}")
        return Counter(term.strip() for term in data)
    if isinstance(data, dict):
        valid = all(
            isinstance(term, str)
            and term.strip()
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
            for term, count in data.items()
        )
        if not valid:
            raise ValueError(f"색상 빈도 맵은 비어 있지 않은 문자열→양의 정수여야 함: {source}")
        return Counter({term.strip(): count for term, count in data.items()})
    raise ValueError(f"색상 소스는 빈도 객체 또는 문자열 배열이어야 함: {source}")


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    denom = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / denom if denom else -1.0


def cluster_terms(
    counts: Counter[str], embed: EmbedFn, *, top_n: int, threshold: float
) -> list[Cluster]:
    """빈도 상위 N 앵커는 greedy 병합하고, 나머지 전체 표기를 최근접 앵커에 제안한다."""
    ranked = sorted(
        ((term, count) for term, count in counts.items() if term not in NON_COLOR_TERMS),
        key=lambda item: (-item[1], item[0]),
    )
    anchors = ranked[: max(0, top_n)]
    if not anchors:
        return []
    terms = [term for term, _ in ranked]
    vectors = [
        vector
        for start in range(0, len(terms), _EMBED_BATCH_SIZE)
        for vector in embed(terms[start : start + _EMBED_BATCH_SIZE])
    ]
    vectors_by_term = dict(zip(terms, vectors, strict=True))
    anchor_order = {term: index for index, (term, _) in enumerate(anchors)}

    def ranked_anchors(vector: Sequence[float], *, exclude: str | None = None):
        scored = [
            (_cosine(vector, vectors_by_term[anchor]), anchor)
            for anchor, _ in anchors
            if anchor != exclude
        ]
        scored.sort(key=lambda item: (-item[0], anchor_order[item[1]], item[1]))
        return scored

    owner_by_anchor: dict[str, str] = {}
    members_by_canonical: dict[str, list[ClusterMember]] = {}
    canonical_order: list[str] = []

    # 상위 앵커끼리의 기존 빈도 seed greedy 병합은 유지한다.
    for seed_term, seed_count in anchors:
        if seed_term in owner_by_anchor:
            continue
        owner_by_anchor[seed_term] = seed_term
        canonical_order.append(seed_term)
        second = ranked_anchors(vectors_by_term[seed_term], exclude=seed_term)
        members = [
            ClusterMember(
                seed_term,
                seed_count,
                vectors_by_term[seed_term],
                1.0,
                is_anchor=True,
                nearest_anchor=seed_term,
                second_anchor=second[0][1] if second else None,
                second_cosine=second[0][0] if second else None,
                margin=1.0 - second[0][0] if second else None,
            )
        ]
        members_by_canonical[seed_term] = members
        seed_vector = vectors_by_term[seed_term]
        for anchor_term, anchor_count in anchors:
            if anchor_term in owner_by_anchor:
                continue
            score = _cosine(seed_vector, vectors_by_term[anchor_term])
            if score < threshold:
                continue
            alternatives = ranked_anchors(vectors_by_term[anchor_term], exclude=anchor_term)
            second = next(
                ((sim, term) for sim, term in alternatives if term != seed_term),
                None,
            )
            members.append(
                ClusterMember(
                    anchor_term,
                    anchor_count,
                    vectors_by_term[anchor_term],
                    score,
                    is_anchor=True,
                    nearest_anchor=seed_term,
                    second_anchor=second[1] if second else None,
                    second_cosine=second[0] if second else None,
                    margin=score - second[0] if second else None,
                )
            )
            owner_by_anchor[anchor_term] = seed_term

    # 희귀 꼬리를 버리지 않고 전부 앵커와 비교한다. 임계 미만은 DB에 미배정 행으로 남는다.
    for term, count in ranked[len(anchors) :]:
        nearest = ranked_anchors(vectors_by_term[term])
        if not nearest or nearest[0][0] < threshold:
            continue
        top_score, top_anchor = nearest[0]
        second_score, second_anchor = nearest[1] if len(nearest) > 1 else (None, None)
        canonical = owner_by_anchor[top_anchor]
        members_by_canonical[canonical].append(
            ClusterMember(
                term,
                count,
                vectors_by_term[term],
                top_score,
                nearest_anchor=top_anchor,
                second_anchor=second_anchor,
                second_cosine=second_score,
                margin=top_score - second_score if second_score is not None else None,
            )
        )

    clusters: list[Cluster] = []
    for canonical in canonical_order:
        clusters.append(Cluster(canonical=canonical, members=members_by_canonical[canonical]))
    return clusters


async def _refine_cluster_chunk(
    clusters: list[Cluster],
    llm: LLMClient,
    *,
    max_tokens: int,
) -> list[Cluster]:
    """한 유계 청크를 다듬고 이 청크만 실패 격리한다."""
    if not clusters:
        return []
    payload = [
        {"canonical": cluster.canonical, "terms": [member.term for member in cluster.members]}
        for cluster in clusters
    ]
    try:
        raw = await llm.complete(
            system=(
                "색상 동의어 군집 검수 보조다. 같은 색이 아닌 표기만 제거한다. "
                "표기를 추가하거나 canonical을 바꾸지 말고 JSON clusters[{canonical,keep[]}]만 반환하라."
            ),
            user=json.dumps(payload, ensure_ascii=False),
            tier="fast",
            max_tokens=max_tokens,
            json_output=True,
        )
        data = json.loads(raw)
        decisions = data.get("clusters") if isinstance(data, dict) else None
        if not isinstance(decisions, list):
            raise ValueError("clusters 배열 없음")
        keeps = {
            item["canonical"]: set(item["keep"])
            for item in decisions
            if isinstance(item, dict)
            and isinstance(item.get("canonical"), str)
            and isinstance(item.get("keep"), list)
            and all(isinstance(term, str) for term in item["keep"])
        }
        refined: list[Cluster] = []
        for cluster in clusters:
            allowed = keeps.get(cluster.canonical)
            if allowed is None:
                refined.append(Cluster(cluster.canonical, cluster.members, llm_status="failed"))
                continue
            # canonical 자체는 대표 표기이므로 LLM이 실수로 빼도 보존한다.
            members = [
                member
                for member in cluster.members
                if member.term == cluster.canonical or member.term in allowed
            ]
            removed = [member for member in cluster.members if member not in members]
            refined.append(
                Cluster(
                    cluster.canonical,
                    members,
                    llm_status="completed",
                    llm_kept=tuple(members),
                    llm_removed=tuple(removed),
                )
            )
        return refined
    except Exception:  # LLM/파싱 실패는 1차 군집으로 안전 degrade
        _log.warning("색상 군집 LLM 다듬기 실패 — 1차 군집 그대로 사용", exc_info=True)
        return [
            Cluster(cluster.canonical, cluster.members, llm_status="failed") for cluster in clusters
        ]


async def refine_clusters(
    clusters: list[Cluster],
    llm: LLMClient,
    *,
    clusters_per_call: int,
    max_tokens: int,
) -> list[Cluster]:
    """군집을 유계 청크로 나눠 다듬고, 실패한 청크만 미판정으로 남긴다."""
    if clusters_per_call < 1:
        raise ValueError("clusters_per_call must be >= 1")
    refined: list[Cluster] = []
    for start in range(0, len(clusters), clusters_per_call):
        chunk = clusters[start : start + clusters_per_call]
        refined.extend(await _refine_cluster_chunk(chunk, llm, max_tokens=max_tokens))
    return refined


def write_review_queue(
    clusters: Sequence[Cluster],
    path: str | Path,
    *,
    threshold: float,
    boundary_band_width: float,
) -> None:
    """LLM 판정 흔적과 임계 경계를 숨기지 않는 Markdown 검수 대기 목록을 쓴다."""
    lines = [
        "# 색상 동의어 검수 대기 목록",
        "",
        "> 자동 승인되지 않은 제안입니다.",
        "> 알려진 실패 모드: 임베딩에서 다크/라이트 같은 수식어 토큰이 색상 어근을 지배해",
        "> `다크그린 → 다크그레이`처럼 의미가 다른 앵커가 1위가 될 수 있습니다.",
        "",
    ]
    for cluster in clusters:
        lines.extend([f"## {cluster.canonical}", ""])
        if cluster.llm_status == "completed":
            kept = ", ".join(member.term for member in cluster.llm_kept) or "없음"
            removed = ", ".join(member.term for member in cluster.llm_removed) or "없음"
            lines.extend(["- LLM 실행: 성공", f"- 유지: {kept}", f"- 제거: {removed}"])
            candidates = (*cluster.llm_kept, *cluster.llm_removed)
        elif cluster.llm_status == "failed":
            lines.append("- LLM 실행: 실패 (1차 군집을 미판정 상태로 유지)")
            candidates = tuple(cluster.members)
        else:
            lines.append("- LLM 실행: 미실행")
            candidates = tuple(cluster.members)
        lines.extend(
            [
                "",
                "| 표기 | 상품 수 | 1위 앵커 | 2위 앵커 | 마진 | LLM 결과 | 경계 |",
                "|---|---:|---|---|---:|---|---|",
            ]
        )
        removed_terms = {member.term for member in cluster.llm_removed}
        ordered = sorted(
            candidates,
            key=lambda member: (
                member.term != cluster.canonical,
                member.margin is None,
                member.margin if member.margin is not None else math.inf,
                -member.doc_count,
                member.term,
            ),
        )
        for member in ordered:
            if cluster.llm_status == "failed":
                llm_result = "미판정"
            elif member.term in removed_terms:
                llm_result = "제거 제안"
            else:
                llm_result = "유지 제안"
            boundary = (
                "확인 필요" if threshold <= member.cosine < threshold + boundary_band_width else ""
            )
            top_anchor = member.nearest_anchor or cluster.canonical
            second = (
                f"{member.second_anchor} {member.second_cosine:.4f}"
                if member.second_anchor is not None and member.second_cosine is not None
                else ""
            )
            margin = f"{member.margin:.4f}" if member.margin is not None else ""
            lines.append(
                f"| {member.term} | {member.doc_count} | {top_anchor} {member.cosine:.4f} "
                f"| {second} | {margin} | {llm_result} | {boundary} |"
            )
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


UPSERT_COLOR_TERM_SQL = """
INSERT INTO color_synonyms
    (term, canonical, status, embedding, embedding_model, provenance, doc_count, updated_at)
VALUES (%s, %s, 'pending_review', %s, %s, %s, %s, now())
ON CONFLICT (term) DO UPDATE SET
    canonical = CASE
        WHEN color_synonyms.status <> 'pending_review' THEN color_synonyms.canonical
        ELSE EXCLUDED.canonical
    END,
    status = CASE
        WHEN color_synonyms.status <> 'pending_review' THEN color_synonyms.status
        ELSE EXCLUDED.status
    END,
    -- 임베딩은 사람의 검수 결과가 아니라 term+model의 파생값이라 status로 얼리지 않는다.
    -- 모델 교체 시 승인 행만 구벡터에 남으면 harvest 최근접 비교가 서로 다른 벡터 공간을 섞는다.
    embedding = COALESCE(EXCLUDED.embedding, color_synonyms.embedding),
    embedding_model = COALESCE(EXCLUDED.embedding_model, color_synonyms.embedding_model),
    provenance = CASE
        WHEN color_synonyms.status <> 'pending_review' THEN color_synonyms.provenance
        ELSE EXCLUDED.provenance
    END,
    doc_count = EXCLUDED.doc_count,
    updated_at = now()
"""


def _get_pool(dsn: str):
    """dsn별 vector 등록 ConnectionPool을 프로세스 수명 동안 재사용한다."""
    from pgvector.psycopg import register_vector  # noqa: PLC0415
    from psycopg_pool import ConnectionPool  # noqa: PLC0415

    pool = _pools.get(dsn)
    if pool is None:
        with _pool_lock:
            pool = _pools.get(dsn)
            if pool is None:
                pool = ConnectionPool(dsn, configure=register_vector, open=True)
                _pools[dsn] = pool
    return pool


def upsert_color_terms(dsn: str, rows: Sequence[ColorTermRow], model: str) -> int:
    """검수 결과(status·canonical·provenance)는 보존하고 색상 표기·빈도를 멱등 upsert한다.

    임베딩은 NULL 입력으로 지워지지 않지만, 값이 들어오면 embedding_model과 함께 최신 값으로
    갱신해 한 테이블 안의 비교 벡터 공간을 일치시킨다.
    """
    from pgvector import Vector  # noqa: PLC0415

    with _get_pool(dsn).connection() as conn, conn.transaction():
        for row in rows:
            vector = Vector(row.embedding) if row.embedding is not None else None
            conn.execute(
                UPSERT_COLOR_TERM_SQL,
                (
                    row.term,
                    row.canonical,
                    vector,
                    model if vector is not None else None,
                    row.provenance,
                    row.doc_count,
                ),
            )
    return len(rows)


def harvest_new_terms(
    dsn: str,
    attributes: dict | None,
    embed: EmbedFn,
    model: str,
    threshold: float,
) -> int:
    """한 I-17 상품에서 DB에 없는 색상만 pending_review 제안으로 적재한다.

    승인된 임베딩 중 임계 이상 최근접 canonical은 제안값일 뿐 status는 항상 pending_review다.
    """
    terms = list(dict.fromkeys(extract_color_terms(attributes)))
    if not terms:
        return 0

    from pgvector import Vector  # noqa: PLC0415

    from app.core.config import get_settings  # noqa: PLC0415 - 순환 임포트 회피(모듈 관례)

    timeout_ms = int(get_settings().catalog_store_query_timeout_s * 1000)
    with _get_pool(dsn).connection() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT term FROM color_synonyms WHERE term = ANY(%s)", (terms,)
            ).fetchall()
        }
        new_terms = [term for term in terms if term not in existing]
        if not new_terms:
            return 0
        clusterable = [term for term in new_terms if term not in NON_COLOR_TERMS]
        vectors_by_term = (
            dict(zip(clusterable, embed(clusterable), strict=True)) if clusterable else {}
        )
        rows: list[ColorTermRow] = []
        for term in new_terms:
            vector = vectors_by_term.get(term)
            if vector is None:
                rows.append(
                    ColorTermRow(
                        term=term,
                        canonical=None,
                        embedding=None,
                        provenance="batch_harvest",
                        doc_count=1,
                    )
                )
                continue
            nearest = conn.execute(
                """
                    SELECT canonical, 1 - (embedding <=> %s) AS similarity
                    FROM color_synonyms
                    WHERE status = 'approved'
                      AND canonical IS NOT NULL
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s
                    LIMIT 1
                    """,
                (Vector(vector), Vector(vector)),
            ).fetchone()
            canonical = (
                nearest[0] if nearest is not None and float(nearest[1]) >= threshold else None
            )
            rows.append(
                ColorTermRow(
                    term=term,
                    canonical=canonical,
                    embedding=vector,
                    provenance="batch_harvest",
                    doc_count=1,
                )
            )
    # SQL의 VALUES status가 고정 pending_review라 최근접 제안이 있어도 자동 승인되지 않는다.
    return upsert_color_terms(dsn, rows, model)


def _rows_from_clusters(counts: Counter[str], clusters: Sequence[Cluster]) -> list[ColorTermRow]:
    assignments: dict[str, tuple[str, list[float]]] = {}
    for cluster in clusters:
        for member in cluster.members:
            assignments[member.term] = (cluster.canonical, member.embedding)
    return [
        ColorTermRow(
            term=term,
            canonical=assignments.get(term, (None, None))[0],
            embedding=assignments.get(term, (None, None))[1],
            provenance="seed_pipeline",
            doc_count=count,
        )
        for term, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


async def build(
    dsn: str,
    review_path: str | Path,
    *,
    fetch: FetchFn = spring_client.fetch_product_changes,
    embed: EmbedFn | None = None,
    llm: LLMClient | None = None,
    top_n: int | None = None,
    threshold: float | None = None,
    page_size: int = 500,
) -> BuildResult:
    """수확→군집→LLM 제거 다듬기→검수 큐→pending upsert를 실행한다."""
    settings = get_settings()
    embed = embed or functools.partial(_embed_texts, task_type=settings.embedding_task_document)
    llm = llm or get_llm()
    if llm is None:
        raise RuntimeError("color synonym build: LLM 미구성")
    counts = await harvest_terms(fetch=fetch, page_size=page_size)
    initial = cluster_terms(
        counts,
        embed,
        top_n=settings.color_synonym_top_n if top_n is None else top_n,
        threshold=(settings.color_synonym_cluster_threshold if threshold is None else threshold),
    )
    refined = await refine_clusters(
        initial,
        llm,
        clusters_per_call=settings.color_synonym_llm_clusters_per_call,
        max_tokens=settings.color_synonym_llm_max_tokens,
    )
    before = sum(len(cluster.members) for cluster in initial)
    after = sum(len(cluster.members) for cluster in refined)
    write_review_queue(
        refined,
        review_path,
        threshold=settings.color_synonym_cluster_threshold if threshold is None else threshold,
        boundary_band_width=settings.color_synonym_boundary_band_width,
    )
    rows = _rows_from_clusters(counts, refined)
    upserted = upsert_color_terms(dsn, rows, settings.embedding_model_id)
    return BuildResult(len(counts), len(refined), before - after, upserted, str(review_path))
