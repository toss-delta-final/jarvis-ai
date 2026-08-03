"""색상 동의어 검수 큐를 만드는 오프라인 구축 파이프라인 (이슈 #258)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.llm import LLMClient, get_llm
from app.pipelines import color_synonyms
from app.pipelines.embedding import embed_texts as _embed_texts
from app.schemas.spring import ProductChange, ProductChangesPage
from app.services import spring_client

_log = logging.getLogger(__name__)

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
    review_required: bool = False
    review_reasons: tuple[str, ...] = ()


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
    preserve_existing_canonical: bool = False


@dataclass(frozen=True)
class ValidationRejection:
    stage: str
    reason: str
    terms: tuple[str, ...] = ()
    canonical: str | None = None


@dataclass(frozen=True)
class UnassignedTerm:
    term: str
    doc_count: int
    reason: str
    preserve_existing_canonical: bool = False


@dataclass(frozen=True)
class ClusteringResult:
    clusters: tuple[Cluster, ...]
    unassigned: tuple[UnassignedTerm, ...]
    rejections: tuple[ValidationRejection, ...]
    embeddings: dict[str, list[float]]

    @property
    def embedding_mismatch_count(self) -> int:
        return sum(
            member.review_required
            for cluster in self.clusters
            for member in cluster.members
            if member.term != cluster.canonical
        )


@dataclass(frozen=True)
class BuildResult:
    harvested_terms: int
    cluster_count: int
    llm_adjustments: int
    upserted_rows: int
    review_queue_path: str
    rejected_proposals: int = 0
    hallucination_rejections: int = 0
    exclusivity_rejections: int = 0
    embedding_mismatches: int = 0
    unassigned_terms: int = 0


def _color_term_limits(
    max_terms: int | None,
    max_term_length: int | None,
) -> tuple[int, int]:
    if max_terms is None or max_term_length is None:
        settings = get_settings()
        max_terms = (
            settings.color_synonym_harvest_max_terms_per_product
            if max_terms is None
            else max_terms
        )
        max_term_length = (
            settings.color_synonym_harvest_max_term_length
            if max_term_length is None
            else max_term_length
        )
    if max_terms < 1 or max_term_length < 1:
        raise ValueError("색상 표기 수확 상한은 1 이상이어야 함")
    return max_terms, max_term_length


def extract_color_terms(
    attributes: dict | None,
    *,
    max_terms: int | None = None,
    max_term_length: int | None = None,
) -> list[str]:
    """혼재 색상 값을 정규화하고 config 기반 개수·길이 상한 안의 표기만 반환한다."""
    if not isinstance(attributes, dict):
        return []
    max_terms, max_term_length = _color_term_limits(max_terms, max_term_length)
    raw = attributes.get("색상")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    normalized = [
        value.strip() for value in values if isinstance(value, str) and value.strip()
    ]
    overlong_count = sum(len(value) > max_term_length for value in normalized)
    if overlong_count:
        _log.warning(
            "색상 표기 문자열 길이 상한 초과 — %d건 거부 (max_length=%d)",
            overlong_count,
            max_term_length,
        )
    accepted = [value for value in normalized if len(value) <= max_term_length]
    overflow_count = max(0, len(accepted) - max_terms)
    if overflow_count:
        _log.warning(
            "색상 표기 개수 상한 초과 — %d건 제외 (accepted=%d, max_terms=%d)",
            overflow_count,
            len(accepted),
            max_terms,
        )
    return accepted[:max_terms]


def count_terms(
    changes: Iterable[ProductChange],
    *,
    max_terms: int | None = None,
    max_term_length: int | None = None,
) -> Counter[str]:
    """각 상품 안의 중복 표기는 한 번만 세어 표기별 상품 수를 집계한다."""
    max_terms, max_term_length = _color_term_limits(max_terms, max_term_length)
    counts: Counter[str] = Counter()
    for change in changes:
        counts.update(
            set(
                extract_color_terms(
                    change.attributes,
                    max_terms=max_terms,
                    max_term_length=max_term_length,
                )
            )
        )
    return counts


async def harvest_terms(
    *,
    fetch: FetchFn = spring_client.fetch_product_changes,
    page_size: int = 500,
    max_terms: int | None = None,
    max_term_length: int | None = None,
) -> Counter[str]:
    """I-17을 since=0부터 소진하며 enrichment 없이 색상 표기만 수확한다."""
    max_terms, max_term_length = _color_term_limits(max_terms, max_term_length)
    cursor: str | None = "0"
    counts: Counter[str] = Counter()
    while True:
        page = await fetch(cursor, page_size)
        counts.update(
            count_terms(
                page.items,
                max_terms=max_terms,
                max_term_length=max_term_length,
            )
        )
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


def _embed_in_batches(terms: Sequence[str], embed: EmbedFn) -> list[list[float]]:
    """임베딩 API 상한 안에서 색상 표기를 공통 고정 크기 청크로 처리한다."""
    return [
        vector
        for start in range(0, len(terms), _EMBED_BATCH_SIZE)
        for vector in embed(list(terms[start : start + _EMBED_BATCH_SIZE]))
    ]


def _anchor_groups_from_response(
    raw: str,
    anchors: Sequence[str],
) -> tuple[dict[str, tuple[str, ...]], list[ValidationRejection]]:
    """앵커 병합 응답을 전역 검증하고 위반 그룹 대신 독립 앵커를 보존한다."""
    anchor_set = set(anchors)
    rejections: list[ValidationRejection] = []
    try:
        data = json.loads(raw)
        raw_groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(raw_groups, list):
            raise ValueError("groups 배열 없음")
    except Exception as exc:
        return (
            {anchor: (anchor,) for anchor in anchors},
            [ValidationRejection("anchors", f"LLM 응답 파싱 실패: {type(exc).__name__}")],
        )

    candidates: list[tuple[str, tuple[str, ...]] | None] = []
    mentioned_anchors: set[str] = set()
    invalid: dict[int, list[str]] = {}
    for index, item in enumerate(raw_groups):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("canonical"), str)
            or not isinstance(item.get("members"), list)
            or not all(isinstance(term, str) for term in item["members"])
        ):
            candidates.append(None)
            invalid.setdefault(index, []).append("그룹 스키마 위반")
            continue
        canonical = item["canonical"]
        members = tuple(item["members"])
        candidates.append((canonical, members))
        mentioned_anchors.update(term for term in members if term in anchor_set)
        hallucinated = tuple(term for term in members if term not in anchor_set)
        if hallucinated:
            invalid.setdefault(index, []).append("환각 표기")
            rejections.append(
                ValidationRejection("anchors", "환각 표기 거부", hallucinated, canonical)
            )
        if canonical not in anchor_set:
            invalid.setdefault(index, []).append("canonical이 앵커 집합 밖")
        if canonical not in members:
            invalid.setdefault(index, []).append("canonical이 자기 그룹 멤버가 아님")
        if len(set(members)) != len(members):
            invalid.setdefault(index, []).append("배타성 위반: 그룹 안 중복")

    # A←B와 B←A는 그룹 단위로 모두 거부한다. 배타성보다 구체적인 원인을 먼저 남긴다.
    for left, left_candidate in enumerate(candidates):
        if left_candidate is None:
            continue
        left_canonical, left_members = left_candidate
        for right in range(left + 1, len(candidates)):
            right_candidate = candidates[right]
            if right_candidate is None:
                continue
            right_canonical, right_members = right_candidate
            if right_canonical in left_members and left_canonical in right_members:
                invalid.setdefault(left, []).append("순환 위반")
                invalid.setdefault(right, []).append("순환 위반")

    member_owners: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        if candidate is None:
            continue
        for term in candidate[1]:
            if term in anchor_set:
                member_owners.setdefault(term, []).append(index)
    for term, owners in member_owners.items():
        if len(owners) > 1:
            for index in owners:
                invalid.setdefault(index, []).append(f"배타성 위반: {term} 중복 배정")

    accepted: dict[str, tuple[str, ...]] = {}
    assigned: set[str] = set()
    for index, candidate in enumerate(candidates):
        if candidate is None:
            reasons = invalid.get(index, ["그룹 스키마 위반"])
            rejections.append(ValidationRejection("anchors", "; ".join(dict.fromkeys(reasons))))
            continue
        canonical, members = candidate
        reasons = invalid.get(index)
        if reasons:
            rejections.append(
                ValidationRejection(
                    "anchors",
                    "; ".join(dict.fromkeys(reasons)),
                    tuple(term for term in members if term in anchor_set),
                    canonical,
                )
            )
            continue
        accepted[canonical] = members
        assigned.update(members)

    for anchor in anchors:
        if anchor not in assigned:
            accepted[anchor] = (anchor,)
            if anchor not in mentioned_anchors:
                rejections.append(
                    ValidationRejection(
                        "anchors",
                        "LLM 응답 누락으로 독립 앵커 유지",
                        (anchor,),
                        anchor,
                    )
                )
    return (
        {anchor: accepted[anchor] for anchor in anchors if anchor in accepted},
        rejections,
    )


async def _llm_anchor_groups(
    anchors: Sequence[str],
    llm: LLMClient,
    *,
    max_tokens: int,
) -> tuple[dict[str, tuple[str, ...]], list[ValidationRejection]]:
    if not anchors:
        return {}, []
    payload = {"stage": "anchors", "anchors": list(anchors)}
    try:
        raw = await llm.complete(
            system=(
                "JSON 색상 동의어 앵커 병합기다. 입력 anchors의 원문만 사용한다. 같은 색인 "
                "앵커만 groups[{canonical,members[]}]로 묶고, 모든 canonical은 입력 앵커이며 "
                "자기 members에 포함되어야 한다. 각 앵커는 정확히 한 그룹에만 나오고 새 "
                "표기를 만들지 않는다. 병합하지 않는 앵커도 자기 단독 그룹으로 반환한다."
            ),
            user=json.dumps(payload, ensure_ascii=False),
            tier="fast",
            max_tokens=max_tokens,
            json_output=True,
        )
    except Exception as exc:
        _log.warning("색상 앵커 LLM 병합 실패 — 독립 앵커로 유지", exc_info=True)
        return (
            {anchor: (anchor,) for anchor in anchors},
            [ValidationRejection("anchors", f"LLM 실패: {type(exc).__name__}")],
        )
    return _anchor_groups_from_response(raw, anchors)


async def _llm_tail_assignments(
    terms: Sequence[str],
    canonicals: Sequence[str],
    llm: LLMClient,
    *,
    terms_per_call: int,
    max_tokens: int,
) -> tuple[dict[str, str], list[UnassignedTerm], list[ValidationRejection]]:
    if terms_per_call < 1:
        raise ValueError("terms_per_call must be >= 1")
    canonical_set = set(canonicals)
    assignments: dict[str, str] = {}
    unassigned: list[UnassignedTerm] = []
    rejections: list[ValidationRejection] = []
    for start in range(0, len(terms), terms_per_call):
        chunk = list(terms[start : start + terms_per_call])
        payload = {"stage": "terms", "anchors": list(canonicals), "terms": chunk}
        try:
            raw = await llm.complete(
                system=(
                    "JSON 색상 동의어 배정기다. 각 입력 terms 원문을 정확히 한 번씩 "
                    "assignments[{term,canonical}]로 반환한다. canonical은 anchors 중 하나 "
                    "또는 null이다. 입력에 없는 표기를 만들거나 정규화·번역하지 않는다. "
                    "수식어의 모양보다 실제 색조와 색상 어근을 우선한다. 예를 들어 해당 "
                    "앵커가 있으면 남색은 네이비, 다크그린·라이트그린은 그린에 배정한다."
                ),
                user=json.dumps(payload, ensure_ascii=False),
                tier="fast",
                max_tokens=max_tokens,
                json_output=True,
            )
            data = json.loads(raw)
            raw_assignments = data.get("assignments") if isinstance(data, dict) else None
            if not isinstance(raw_assignments, list):
                raise ValueError("assignments 배열 없음")
        except Exception as exc:
            _log.warning("색상 표기 LLM 배정 청크 실패 — 청크만 미배정", exc_info=True)
            unassigned.extend(
                UnassignedTerm(
                    term,
                    0,
                    f"LLM 실패로 미배정: {type(exc).__name__}",
                    preserve_existing_canonical=True,
                )
                for term in chunk
            )
            continue

        by_term: dict[str, list[object]] = {}
        for item in raw_assignments:
            if not isinstance(item, dict) or not isinstance(item.get("term"), str):
                rejections.append(ValidationRejection("terms", "배정 스키마 위반"))
                continue
            term = item["term"]
            if term not in chunk:
                rejections.append(
                    ValidationRejection("terms", "환각 표기 거부", (term,), item.get("canonical"))
                )
                continue
            by_term.setdefault(term, []).append(item.get("canonical"))

        for term in chunk:
            choices = by_term.get(term, [])
            if not choices:
                unassigned.append(
                    UnassignedTerm(
                        term,
                        0,
                        "LLM 미응답으로 미배정",
                        preserve_existing_canonical=True,
                    )
                )
                continue
            if len(choices) > 1:
                unassigned.append(
                    UnassignedTerm(
                        term,
                        0,
                        "배타성 위반으로 미배정",
                        preserve_existing_canonical=True,
                    )
                )
                rejections.append(
                    ValidationRejection("terms", "배타성 위반: 중복 배정 거부", (term,))
                )
                continue
            canonical = choices[0]
            if canonical is None or canonical == "none":
                unassigned.append(UnassignedTerm(term, 0, "LLM none으로 미배정"))
                continue
            if not isinstance(canonical, str) or canonical not in canonical_set:
                unassigned.append(
                    UnassignedTerm(
                        term,
                        0,
                        "canonical 유효성 위반으로 미배정",
                        preserve_existing_canonical=True,
                    )
                )
                rejections.append(
                    ValidationRejection(
                        "terms",
                        "canonical 유효성 위반",
                        (term,),
                        canonical if isinstance(canonical, str) else None,
                    )
                )
                continue
            assignments[term] = canonical
    return assignments, unassigned, rejections


async def assign_color_clusters(
    counts: Counter[str],
    embed: EmbedFn,
    llm: LLMClient,
    *,
    top_n: int,
    terms_per_call: int,
    threshold: float,
    max_tokens: int,
) -> ClusteringResult:
    """LLM이 의미 군집을 만들고 임베딩은 불일치 근거만 기록하는 검수 후보를 만든다."""
    ranked = sorted(
        ((term, count) for term, count in counts.items() if term not in NON_COLOR_TERMS),
        key=lambda item: (-item[1], item[0]),
    )
    terms = [term for term, _ in ranked]
    vectors = await asyncio.to_thread(_embed_in_batches, terms, embed)
    vectors_by_term = dict(zip(terms, vectors, strict=True))
    anchors = terms[: max(0, top_n)]
    tail = terms[len(anchors) :]
    anchor_groups, rejections = await _llm_anchor_groups(anchors, llm, max_tokens=max_tokens)
    canonicals = list(anchor_groups)
    tail_assignments, tail_unassigned, tail_rejections = await _llm_tail_assignments(
        tail,
        canonicals,
        llm,
        terms_per_call=terms_per_call,
        max_tokens=max_tokens,
    )
    rejections.extend(tail_rejections)

    assignment: dict[str, str] = {
        member: canonical
        for canonical, members in anchor_groups.items()
        for member in members
    }
    assignment.update(tail_assignments)
    anchor_order = {anchor: index for index, anchor in enumerate(canonicals)}
    members_by_canonical: dict[str, list[ClusterMember]] = {canonical: [] for canonical in canonicals}
    for term, _ in ranked:
        canonical = assignment.get(term)
        if canonical is None:
            continue
        scored = [
            (_cosine(vectors_by_term[term], vectors_by_term[anchor]), anchor)
            for anchor in canonicals
        ]
        scored.sort(key=lambda item: (-item[0], anchor_order[item[1]], item[1]))
        nearest_score, nearest_anchor = scored[0]
        second_score, second_anchor = scored[1] if len(scored) > 1 else (None, None)
        chosen_score = _cosine(vectors_by_term[term], vectors_by_term[canonical])
        reasons: list[str] = []
        if term != canonical and chosen_score < threshold:
            reasons.append(f"LLM 앵커 코사인 {chosen_score:.4f} < {threshold:.4f}")
        if term != canonical and nearest_anchor != canonical:
            reasons.append(f"임베딩 1위 {nearest_anchor} != LLM {canonical}")
        members_by_canonical[canonical].append(
            ClusterMember(
                term,
                counts[term],
                vectors_by_term[term],
                chosen_score,
                is_anchor=term in anchors,
                nearest_anchor=nearest_anchor,
                second_anchor=second_anchor,
                second_cosine=second_score,
                margin=(
                    nearest_score - second_score if second_score is not None else None
                ),
                review_required=bool(reasons),
                review_reasons=tuple(reasons),
            )
        )

    sentinel_unassigned = [
        UnassignedTerm(term, count, "sentinel 보호로 미배정")
        for term, count in counts.items()
        if term in NON_COLOR_TERMS
    ]
    unassigned = [
        UnassignedTerm(
            item.term,
            counts[item.term],
            item.reason,
            item.preserve_existing_canonical,
        )
        for item in tail_unassigned
    ]
    clusters = tuple(
        Cluster(
            canonical,
            sorted(
                members_by_canonical[canonical],
                key=lambda member: (
                    member.term != canonical,
                    -member.doc_count,
                    member.term,
                ),
            ),
            llm_status="completed",
        )
        for canonical in canonicals
    )
    return ClusteringResult(
        clusters,
        tuple(sentinel_unassigned + unassigned),
        tuple(rejections),
        vectors_by_term,
    )


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
    vectors = _embed_in_batches(terms, embed)
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


def _write_assignment_review_queue(
    result: ClusteringResult,
    path: str | Path,
    *,
    threshold: float,
) -> None:
    """LLM 배정·엄격 검증·임베딩 교차검증 근거를 한 검수 문서에 쓴다."""
    lines = [
        "# 색상 동의어 검수 대기 목록",
        "",
        "> 자동 승인되지 않은 `pending_review` 제안입니다.",
        "> LLM이 의미 배정을 만들고, 임베딩은 자동 판정이 아니라 불일치 교차검증에만 사용합니다.",
        "",
        f"- 검증 거부: {len(result.rejections)}건",
        f"- 미배정: {len(result.unassigned)}건",
        f"- LLM·임베딩 불일치: {result.embedding_mismatch_count}건",
        "",
        "## 검증 거부",
        "",
    ]
    if result.rejections:
        for rejection in result.rejections:
            terms = ", ".join(rejection.terms) or "표기 없음"
            canonical = f" / canonical={rejection.canonical}" if rejection.canonical else ""
            lines.append(f"- [{rejection.stage}] {rejection.reason}: {terms}{canonical}")
    else:
        lines.append("- 없음")

    lines.extend(["", "## 미배정", ""])
    if result.unassigned:
        lines.extend(
            [
                "| 표기 | 상품 수 | 사유 |",
                "|---|---:|---|",
                *[
                    f"| {item.term} | {item.doc_count} | {item.reason} |"
                    for item in sorted(
                        result.unassigned,
                        key=lambda item: (-item.doc_count, item.term),
                    )
                ],
            ]
        )
    else:
        lines.append("- 없음")

    for cluster in result.clusters:
        lines.extend(
            [
                "",
                f"## {cluster.canonical}",
                "",
                "| 표기 | 상품 수 | LLM 배정 | LLM 코사인 | 임베딩 1위 | 1위 점수 | 판정 |",
                "|---|---:|---|---:|---|---:|---|",
            ]
        )
        for member in cluster.members:
            nearest_score = _cosine(
                member.embedding,
                result.embeddings[member.nearest_anchor or cluster.canonical],
            )
            evidence = (
                f"LLM={cluster.canonical} / 임베딩1위="
                f"{member.nearest_anchor or cluster.canonical} {nearest_score:.4f}"
            )
            reasons = "; ".join(member.review_reasons)
            verdict = f"확인 필요 ({evidence}; {reasons})" if member.review_required else ""
            lines.append(
                f"| {member.term} | {member.doc_count} | {cluster.canonical} "
                f"| {member.cosine:.4f} | {member.nearest_anchor or cluster.canonical} "
                f"| {nearest_score:.4f} | {verdict} |"
            )
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_review_queue(
    clusters: Sequence[Cluster] | ClusteringResult,
    path: str | Path,
    *,
    threshold: float,
    boundary_band_width: float,
) -> None:
    """LLM 판정 흔적과 임계 경계를 숨기지 않는 Markdown 검수 대기 목록을 쓴다."""
    if isinstance(clusters, ClusteringResult):
        _write_assignment_review_queue(clusters, path, threshold=threshold)
        return
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
        -- 일시 실패·검증 불가처럼 이번 실행이 유효한 결론을 못 낸 경우만 이전 제안을 보존한다.
        -- LLM이 명시적으로 none을 선택한 경우에는 false가 전달돼 NULL 철회가 반영된다.
        WHEN %s THEN color_synonyms.canonical
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
    """런타임 승인 조회와 같은 dsn별 vector 등록 풀을 공유한다."""
    return color_synonyms._get_pool(dsn)


def _execute_color_term_upserts(conn, rows: Sequence[ColorTermRow], model: str) -> int:
    """열린 트랜잭션에서 검수 보호형 색상 표기 upsert를 실행한다."""
    from pgvector import Vector  # noqa: PLC0415

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
                row.preserve_existing_canonical,
            ),
        )
    return len(rows)


def upsert_color_terms(dsn: str, rows: Sequence[ColorTermRow], model: str) -> int:
    """검수 결과(status·canonical·provenance)는 보존하고 색상 표기·빈도를 멱등 upsert한다.

    임베딩은 NULL 입력으로 지워지지 않지만, 값이 들어오면 embedding_model과 함께 최신 값으로
    갱신해 한 테이블 안의 비교 벡터 공간을 일치시킨다.
    """
    with _get_pool(dsn).connection() as conn, conn.transaction():
        return _execute_color_term_upserts(conn, rows, model)


def harvest_new_terms(
    dsn: str,
    attributes: dict | None,
    embed: EmbedFn,
    model: str,
    threshold: float,
    *,
    max_terms: int | None = None,
    max_term_length: int | None = None,
) -> int:
    """한 I-17 상품에서 DB에 없는 색상만 pending_review 제안으로 적재한다.

    승인된 임베딩 중 임계 이상 최근접 canonical은 제안값일 뿐 status는 항상 pending_review다.
    """
    settings = get_settings()
    max_terms, max_term_length = _color_term_limits(
        (
            settings.color_synonym_harvest_max_terms_per_product
            if max_terms is None
            else max_terms
        ),
        (
            settings.color_synonym_harvest_max_term_length
            if max_term_length is None
            else max_term_length
        ),
    )
    terms = list(
        dict.fromkeys(
            extract_color_terms(
                attributes,
                max_terms=max_terms,
                max_term_length=max_term_length,
            )
        )
    )
    if not terms:
        return 0

    from pgvector import Vector  # noqa: PLC0415

    timeout_ms = int(settings.catalog_store_query_timeout_s * 1000)
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
    # asyncio.wait_for가 to_thread 아래 동기 호출을 취소해도 스레드는 계속 돈다. 따라서 외부
    # 임베딩 API 자체 상한을 기다리는 동안 DB 연결·트랜잭션은 반드시 놓아 풀 고갈을 완화한다.
    vectors_by_term = (
        dict(zip(clusterable, _embed_in_batches(clusterable, embed), strict=True))
        if clusterable
        else {}
    )

    with _get_pool(dsn).connection() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
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
        # 첫 SELECT와 임베딩 사이에 다른 배치가 같은 term을 넣어도 ON CONFLICT가 멱등 처리한다.
        # 승인 행의 status·canonical·provenance는 CASE 가드로 보존돼 사람 검수 결과를 덮지 않는다.
        # VALUES status도 고정 pending_review라 최근접 제안이 있어도 자동 승인되지 않는다.
        return _execute_color_term_upserts(conn, rows, model)


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


def _rows_from_result(
    counts: Counter[str],
    result: ClusteringResult,
) -> list[ColorTermRow]:
    assignments = {
        member.term: cluster.canonical
        for cluster in result.clusters
        for member in cluster.members
    }
    preserve_existing = {
        item.term: item.preserve_existing_canonical for item in result.unassigned
    }
    return [
        ColorTermRow(
            term=term,
            canonical=assignments.get(term),
            embedding=result.embeddings.get(term),
            provenance="seed_pipeline",
            doc_count=count,
            preserve_existing_canonical=preserve_existing.get(term, False),
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
    """오프라인 1회 빌드로 수확→LLM 배정→엄격 검증→교차검증→pending upsert를 실행한다.

    임베딩 API·파일·DB 동기 작업은 오래 걸려도 정상인 배치이므로 시간 제한 없이 스레드로
    넘겨, async 진입점이 실행 중인 이벤트 루프만 막지 않는다.
    """
    settings = get_settings()
    embed = embed or functools.partial(_embed_texts, task_type=settings.embedding_task_document)
    llm = llm or get_llm()
    if llm is None:
        raise RuntimeError("color synonym build: LLM 미구성")
    counts = await harvest_terms(fetch=fetch, page_size=page_size)
    result = await assign_color_clusters(
        counts,
        embed,
        llm,
        top_n=settings.color_synonym_top_n if top_n is None else top_n,
        terms_per_call=(
            _EMBED_BATCH_SIZE * settings.color_synonym_llm_clusters_per_call
        ),
        threshold=settings.color_synonym_cluster_threshold if threshold is None else threshold,
        max_tokens=settings.color_synonym_llm_max_tokens,
    )
    await asyncio.to_thread(
        write_review_queue,
        result,
        review_path,
        threshold=settings.color_synonym_cluster_threshold if threshold is None else threshold,
        boundary_band_width=settings.color_synonym_boundary_band_width,
    )
    rows = _rows_from_result(counts, result)
    upserted = await asyncio.to_thread(
        upsert_color_terms,
        dsn,
        rows,
        settings.embedding_model_id,
    )
    assigned_noncanonical = sum(
        member.term != cluster.canonical
        for cluster in result.clusters
        for member in cluster.members
    )
    return BuildResult(
        len(counts),
        len(result.clusters),
        assigned_noncanonical,
        upserted,
        str(review_path),
        len(result.rejections),
        sum("환각" in rejection.reason for rejection in result.rejections),
        sum("배타성" in rejection.reason for rejection in result.rejections),
        result.embedding_mismatch_count,
        len(result.unassigned),
    )
