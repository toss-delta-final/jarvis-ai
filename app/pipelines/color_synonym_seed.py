"""색상 동의어 검수 큐를 만드는 오프라인 구축 파이프라인 (이슈 #258)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import unicodedata
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
SEED_LLM_PROVENANCE = "seed_llm_assignment"
BATCH_EMBEDDING_PROVENANCE = "batch_embedding_unverified"
# 사람 검수 결과(오버레이 approved/rejected)의 provenance. DB CHECK(03_color_synonyms.sql)와
# scripts/derive_color_synonym_seed.py 양쪽이 이미 "human" 리터럴을 쓰고 있어 이 상수는 이
# 모듈 안(정본 시드 로더 검증)에서만 쓴다 — 그 스크립트는 app 모듈을 lazy import 로만 참조하는
# 관례라 거기로 끌고 가지 않는다(PR #447 리뷰 R3).
HUMAN_PROVENANCE = "human"
_EXCLUDED_TERM_LOG_PREVIEW = 5


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
    scan_max_values: int | None,
) -> tuple[int, int, int]:
    if max_terms is None or max_term_length is None or scan_max_values is None:
        settings = get_settings()
        max_terms = (
            settings.color_synonym_harvest_max_terms_per_product if max_terms is None else max_terms
        )
        max_term_length = (
            settings.color_synonym_harvest_max_term_length
            if max_term_length is None
            else max_term_length
        )
        scan_max_values = (
            settings.color_synonym_harvest_scan_max_values_per_product
            if scan_max_values is None
            else scan_max_values
        )
    if max_terms < 1 or max_term_length < 1 or scan_max_values < 1:
        raise ValueError("색상 표기 수확 상한은 1 이상이어야 함")
    if scan_max_values <= max_terms:
        raise ValueError("색상 표기 스캔 상한은 반환 상한보다 커야 함")
    return max_terms, max_term_length, scan_max_values


def extract_color_terms(
    attributes: dict | None,
    *,
    max_terms: int | None = None,
    max_term_length: int | None = None,
    scan_max_values: int | None = None,
) -> list[str]:
    """혼재 색상 값을 정규화하고 config 기반 개수·길이 상한 안의 표기만 반환한다."""
    if not isinstance(attributes, dict):
        return []
    max_terms, max_term_length, scan_max_values = _color_term_limits(
        max_terms,
        max_term_length,
        scan_max_values,
    )
    raw = attributes.get("색상")
    values = (
        [raw] if isinstance(raw, str) else raw[:scan_max_values] if isinstance(raw, list) else []
    )
    if isinstance(raw, list) and len(raw) > scan_max_values:
        _log.warning(
            "색상 원본 배열 스캔 상한 초과 — %d건 미처리 (raw=%d, scan_max_values=%d)",
            len(raw) - scan_max_values,
            len(raw),
            scan_max_values,
        )
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped:
            normalized.append(stripped)
    unique = list(dict.fromkeys(normalized))
    overlong_count = sum(len(value) > max_term_length for value in unique)
    if overlong_count:
        _log.warning(
            "색상 표기 문자열 길이 상한 초과 — %d건 거부 (max_length=%d)",
            overlong_count,
            max_term_length,
        )
    accepted = [value for value in unique if len(value) <= max_term_length]
    excluded = accepted[max_terms:]
    if excluded:
        preview = ", ".join(repr(value) for value in excluded[:_EXCLUDED_TERM_LOG_PREVIEW])
        remaining = len(excluded) - _EXCLUDED_TERM_LOG_PREVIEW
        suffix = f", 그 외 {remaining}건" if remaining > 0 else ""
        _log.warning(
            "색상 표기 개수 상한 초과 — %d건 제외: [%s%s] (unique_accepted=%d, max_terms=%d)",
            len(excluded),
            preview,
            suffix,
            len(accepted),
            max_terms,
        )
    return accepted[:max_terms]


def count_terms(
    changes: Iterable[ProductChange],
    *,
    max_terms: int | None = None,
    max_term_length: int | None = None,
    scan_max_values: int | None = None,
) -> Counter[str]:
    """각 상품 안의 중복 표기는 한 번만 세어 표기별 상품 수를 집계한다."""
    max_terms, max_term_length, scan_max_values = _color_term_limits(
        max_terms,
        max_term_length,
        scan_max_values,
    )
    counts: Counter[str] = Counter()
    for change in changes:
        counts.update(
            set(
                extract_color_terms(
                    change.attributes,
                    max_terms=max_terms,
                    max_term_length=max_term_length,
                    scan_max_values=scan_max_values,
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
    scan_max_values: int | None = None,
) -> Counter[str]:
    """I-17을 since=0부터 소진하며 enrichment 없이 색상 표기만 수확한다."""
    max_terms, max_term_length, scan_max_values = _color_term_limits(
        max_terms,
        max_term_length,
        scan_max_values,
    )
    cursor: str | None = "0"
    counts: Counter[str] = Counter()
    while True:
        page = await fetch(cursor, page_size)
        counts.update(
            count_terms(
                page.items,
                max_terms=max_terms,
                max_term_length=max_term_length,
                scan_max_values=scan_max_values,
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
        member: canonical for canonical, members in anchor_groups.items() for member in members
    }
    assignment.update(tail_assignments)
    anchor_order = {anchor: index for index, anchor in enumerate(canonicals)}
    members_by_canonical: dict[str, list[ClusterMember]] = {
        canonical: [] for canonical in canonicals
    }
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
                margin=(nearest_score - second_score if second_score is not None else None),
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
        )
        for canonical in canonicals
    )
    return ClusteringResult(
        clusters,
        tuple(sentinel_unassigned + unassigned),
        tuple(rejections),
        vectors_by_term,
    )


def _review_text(value: str) -> str:
    """셀러 원문을 알아볼 수 있게 남기면서 Markdown 구조 제어 문자만 가시화한다."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    rendered: list[str] = []
    for character in value:
        if character == "|":
            rendered.append("&#124;")
        elif unicodedata.category(character) == "Cc":
            codepoint = ord(character)
            rendered.append(f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


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
            terms = ", ".join(_review_text(term) for term in rejection.terms) or "표기 없음"
            canonical = (
                f" / canonical={_review_text(rejection.canonical)}" if rejection.canonical else ""
            )
            lines.append(
                f"- [{_review_text(rejection.stage)}] {_review_text(rejection.reason)}: "
                f"{terms}{canonical}"
            )
    else:
        lines.append("- 없음")

    lines.extend(["", "## 미배정", ""])
    if result.unassigned:
        lines.extend(
            [
                "| 표기 | 상품 수 | 사유 |",
                "|---|---:|---|",
                *[
                    f"| {_review_text(item.term)} | {item.doc_count} "
                    f"| {_review_text(item.reason)} |"
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
                f"## {_review_text(cluster.canonical)}",
                "",
                "| 표기 | 상품 수 | LLM 배정 | LLM 코사인 | 임베딩 1위 | 1위 점수 | 판정 |",
                "|---|---:|---|---:|---|---:|---|",
            ]
        )
        for member in cluster.members:
            canonical = _review_text(cluster.canonical)
            term = _review_text(member.term)
            nearest_anchor = _review_text(member.nearest_anchor or cluster.canonical)
            nearest_score = _cosine(
                member.embedding,
                result.embeddings[member.nearest_anchor or cluster.canonical],
            )
            evidence = f"LLM={canonical} / 임베딩1위={nearest_anchor} {nearest_score:.4f}"
            reasons = "; ".join(_review_text(reason) for reason in member.review_reasons)
            verdict = f"확인 필요 ({evidence}; {reasons})" if member.review_required else ""
            lines.append(
                f"| {term} | {member.doc_count} | {canonical} "
                f"| {member.cosine:.4f} | {nearest_anchor} "
                f"| {nearest_score:.4f} | {verdict} |"
            )
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_review_queue(
    result: ClusteringResult,
    path: str | Path,
    *,
    threshold: float,
) -> None:
    """LLM 배정과 임베딩 교차검증 근거를 Markdown 검수 대기 목록에 쓴다."""
    _write_assignment_review_queue(result, path, threshold=threshold)


UPSERT_COLOR_TERM_SQL = """
INSERT INTO color_synonyms
    (term, canonical, status, embedding, embedding_model, provenance, doc_count, updated_at)
VALUES (%s, %s, 'pending_review', %s, %s, %s, %s, now())
ON CONFLICT (term) DO UPDATE SET
    canonical = CASE
        WHEN color_synonyms.status <> 'pending_review' THEN color_synonyms.canonical
        -- 늦게 커밋된 배치 임베딩 추정은 먼저 저장된 LLM 의미 배정을 강등하지 않는다.
        WHEN color_synonyms.provenance = 'seed_llm_assignment'
         AND EXCLUDED.provenance = 'batch_embedding_unverified'
        THEN color_synonyms.canonical
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
        WHEN color_synonyms.provenance = 'seed_llm_assignment'
         AND EXCLUDED.provenance = 'batch_embedding_unverified'
        THEN color_synonyms.provenance
        ELSE EXCLUDED.provenance
    END,
    doc_count = CASE
        WHEN color_synonyms.provenance = 'seed_llm_assignment'
         AND EXCLUDED.provenance = 'batch_embedding_unverified'
        THEN color_synonyms.doc_count
        ELSE EXCLUDED.doc_count
    END,
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
    scan_max_values: int | None = None,
) -> int:
    """한 I-17 상품에서 DB에 없는 색상만 pending_review 제안으로 적재한다.

    승인된 임베딩 중 임계 이상 최근접 canonical은 제안값일 뿐 status는 항상 pending_review다.
    """
    settings = get_settings()
    max_terms, max_term_length, scan_max_values = _color_term_limits(
        (settings.color_synonym_harvest_max_terms_per_product if max_terms is None else max_terms),
        (
            settings.color_synonym_harvest_max_term_length
            if max_term_length is None
            else max_term_length
        ),
        (
            settings.color_synonym_harvest_scan_max_values_per_product
            if scan_max_values is None
            else scan_max_values
        ),
    )
    terms = list(
        dict.fromkeys(
            extract_color_terms(
                attributes,
                max_terms=max_terms,
                max_term_length=max_term_length,
                scan_max_values=scan_max_values,
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
                        provenance=BATCH_EMBEDDING_PROVENANCE,
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
                      AND embedding_model = %s
                    ORDER BY embedding <=> %s
                    LIMIT 1
                    """,
                (Vector(vector), model, Vector(vector)),
            ).fetchone()
            canonical = (
                nearest[0] if nearest is not None and float(nearest[1]) >= threshold else None
            )
            rows.append(
                ColorTermRow(
                    term=term,
                    canonical=canonical,
                    embedding=vector,
                    provenance=BATCH_EMBEDDING_PROVENANCE,
                    doc_count=1,
                )
            )
        # 첫 SELECT와 임베딩 사이에 다른 배치가 같은 term을 넣어도 ON CONFLICT가 멱등 처리한다.
        # 승인 행의 status·canonical·provenance는 CASE 가드로 보존돼 사람 검수 결과를 덮지 않는다.
        # VALUES status도 고정 pending_review라 최근접 제안이 있어도 자동 승인되지 않는다.
        return _execute_color_term_upserts(conn, rows, model)


def _rows_from_result(
    counts: Counter[str],
    result: ClusteringResult,
) -> list[ColorTermRow]:
    assignments = {
        member.term: cluster.canonical for cluster in result.clusters for member in cluster.members
    }
    preserve_existing = {item.term: item.preserve_existing_canonical for item in result.unassigned}
    return [
        ColorTermRow(
            term=term,
            canonical=assignments.get(term),
            embedding=result.embeddings.get(term),
            provenance=SEED_LLM_PROVENANCE,
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
        terms_per_call=(_EMBED_BATCH_SIZE * settings.color_synonym_llm_clusters_per_call),
        threshold=settings.color_synonym_cluster_threshold if threshold is None else threshold,
        max_tokens=settings.color_synonym_llm_max_tokens,
    )
    await asyncio.to_thread(
        write_review_queue,
        result,
        review_path,
        threshold=settings.color_synonym_cluster_threshold if threshold is None else threshold,
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


_SEED_STATUSES = frozenset({"pending_review", "approved", "rejected"})
_SEED_PROVENANCES = frozenset({SEED_LLM_PROVENANCE, BATCH_EMBEDDING_PROVENANCE, HUMAN_PROVENANCE})


@dataclass(frozen=True)
class SeedColorTermRow:
    """정본 시드 파일(`db/catalog/seed/color_synonyms.json`) 한 행.

    `ColorTermRow`(배치 수확 제안, 검수 보호형 upsert 대상)와 달리 이 행은 **이미 검수를 거친
    정본** — 적재 시 `status`/`canonical`/`provenance`를 그대로 권위 있게(authoritative) 반영한다.
    """

    term: str
    canonical: str | None
    status: str
    provenance: str
    doc_count: int | None


def load_seed_rows(path: str | Path) -> list[SeedColorTermRow]:
    """정본 시드 JSON(`scripts/derive_color_synonym_seed.py` 생성물)을 엄격 로드한다.

    형식 위반(배열 아님·필수 키 결측·enum 밖 값·타입 불일치)은 즉시 `ValueError`.
    """
    data = json.loads(Path(path).read_bytes().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"색상 동의어 정본 시드는 배열이어야 함: {path}")
    rows: list[SeedColorTermRow] = []
    seen_terms: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"색상 동의어 정본 시드 항목은 객체여야 함: {item!r}")
        term = item.get("term")
        canonical = item.get("canonical")
        status = item.get("status")
        provenance = item.get("provenance")
        doc_count = item.get("doc_count")
        if not isinstance(term, str) or not term.strip():
            raise ValueError(
                f"색상 동의어 정본 시드 term은 비어 있지 않은 문자열이어야 함: {item!r}"
            )
        # 이 파일은 생성물이라 바이트가 derive --check·커밋된 sha256 으로 고정돼 있다 — 로더가
        # 조용히 strip 하면 파일 내용과 DB 에 들어가는 값이 갈라져 "repo 파일이 바이트 단위
        # 정본"이라는 이 PR 의 핵심 성질이 깨진다. extract_color_terms 가 셀러 자유 텍스트를
        # strip 하는 것과는 신뢰 경계가 다르다(PR #447 리뷰 R5) — 여기는 고치지 않고 거부한다.
        if term != term.strip():
            raise ValueError(
                f"색상 동의어 정본 시드 term에 앞뒤 공백/개행이 있음(허용 안 함) — "
                f"scripts/derive_color_synonym_seed.py 로 재생성하라: {item!r}"
            )
        # _execute_seed_upserts 가 rows 순서대로 ON CONFLICT (term) DO UPDATE 를 실행하므로,
        # 같은 term 이 두 번 있으면(다른 status/canonical 이어도) 배열 순서에 좌우돼 나중 행이
        # 앞 행을 조용히 덮는다(PR #447 리뷰 R2). DB UNIQUE(term) 과 같은 기준으로 원문 term
        # 중복만 판정한다 — 표기 정규화(`_norm`) 충돌 판정은 파생 스크립트(apply_overlay)의
        # 책임이라 여기서 끌어오지 않는다.
        if term in seen_terms:
            raise ValueError(f"색상 동의어 정본 시드 term 중복: {term!r} ({item!r})")
        seen_terms.add(term)
        if canonical is not None and not isinstance(canonical, str):
            raise ValueError(f"색상 동의어 정본 시드 canonical은 문자열/null이어야 함: {item!r}")
        # 빈 문자열은 "canonical 없음"의 유효한 표현이 아니다 — WHERE canonical IS NOT NULL 을
        # 통과해 build_synonym_map 의 groups.setdefault(canonical, ...) 가 ""를 키로 삼아
        # 서로 무관한 승인 행을 한 묶음으로 잘못 합친다(PR #447 리뷰 R1). "없음"은 None 으로만
        # 표현한다.
        if canonical is not None and not canonical.strip():
            raise ValueError(
                f"색상 동의어 정본 시드 canonical은 빈 문자열일 수 없음(없으면 null): {item!r}"
            )
        # term 과 같은 이유(생성물 바이트 정본, PR #447 리뷰 R5) — canonical 도 조용히 strip 하지
        # 않고 거부한다.
        if canonical is not None and canonical != canonical.strip():
            raise ValueError(
                f"색상 동의어 정본 시드 canonical에 앞뒤 공백/개행이 있음(허용 안 함) — "
                f"scripts/derive_color_synonym_seed.py 로 재생성하라: {item!r}"
            )
        if status not in _SEED_STATUSES:
            raise ValueError(f"색상 동의어 정본 시드 status 위반({status!r}): {item!r}")
        if provenance not in _SEED_PROVENANCES:
            raise ValueError(f"색상 동의어 정본 시드 provenance 위반({provenance!r}): {item!r}")
        if doc_count is not None and (
            not isinstance(doc_count, int) or isinstance(doc_count, bool)
        ):
            raise ValueError(f"색상 동의어 정본 시드 doc_count는 정수/null이어야 함: {item!r}")
        # status=approved 인데 canonical 이 없으면(None) build_synonym_map 필터
        # (canonical IS NOT NULL)에 걸려 조용히 사전에서 빠진다 — 검수자는 승인했다고 믿는데
        # 런타임에는 존재하지 않는 모순 상태다.
        if status == "approved" and canonical is None:
            raise ValueError(
                f"색상 동의어 정본 시드 status=approved 인데 canonical 이 없음(null): {item!r}"
            )
        rows.append(SeedColorTermRow(term, canonical, status, provenance, doc_count))

    # 2차 패스 — 행 단위 루프에서는 뒤에 올 행을 알 수 없으므로, 파일 전체를 다 읽은 뒤
    # canonical 의 참조 무결성을 검증한다. scripts/derive_color_synonym_seed.py::apply_overlay
    # 가 수확 집합(DB) 기준으로 이미 강제하는 것과 같은 세 규칙을 시드 파일(정본 JSON) 기준으로
    # 지킨다 — 원천이 다를 뿐 불변식은 같다(#258 리뷰 R4). 위반해도 조용히 통과하면
    # build_synonym_map 이 그 canonical 을 그룹 키로 못 찾아 "승인이 조용히 아무 일도 하지
    # 않는" 상태가 된다.
    by_term = {row.term: row for row in rows}
    for row in rows:
        if row.canonical is None:
            continue
        target = by_term.get(row.canonical)
        if target is None:
            raise ValueError(
                f"색상 동의어 정본 시드 canonical이 파일 안 term을 가리키지 않음: "
                f"{row.term!r} → {row.canonical!r}"
            )
        if row.status == "approved" and target.status != "approved":
            raise ValueError(
                f"고아 승인 — {row.term!r}(approved)의 canonical {row.canonical!r}이 "
                f"approved가 아님(status={target.status!r})"
            )
        if target.canonical != row.canonical:
            raise ValueError(
                f"2단계 체인/순환 금지 위반: {row.term!r} → {row.canonical!r} → "
                f"{target.canonical!r}"
            )

    # DB UNIQUE(term)(위 seen_terms)은 원문 기준이고, 이건 다른 불변식이다 —
    # app.pipelines.color_synonyms.build_synonym_map 이 `mapping[_norm(term)] = ordered.copy()`
    # 로 키를 만들므로, 서로 다른 원문 term 두 개가 같은 _norm 이면 나중 그룹이 먼저 그룹을
    # 조용히 덮어써 먼저 term 의 동의어 목록이 사라진다(예외·로그 없음, PR #447 리뷰 R6).
    # 두 검사는 대체가 아니라 공존해야 한다. scripts/derive_color_synonym_seed.py::apply_overlay
    # 가 수확 집합 기준으로 같은 _norm 충돌 검사를 한다 — 여기는 시드 파일 기준의 대응물이다.
    # 정규화는 런타임 build_synonym_map 의 키와 절대 갈라지면 안 되므로 복사하지 않고
    # color_synonyms._norm 을 그대로 가져다 쓴다.
    norm_seen: dict[str, str] = {}
    for row in rows:
        key = color_synonyms._norm(row.term)
        if key in norm_seen and norm_seen[key] != row.term:
            raise ValueError(
                f"색상 동의어 정본 시드 _norm 기준 term 충돌: {norm_seen[key]!r} vs {row.term!r}"
            )
        norm_seen[key] = row.term

    return rows


# 정본은 repo 파일이다 — 위 UPSERT_COLOR_TERM_SQL은 배치 수확이 사람 검수 결과(status·canonical·
# provenance)를 덮지 않도록 보호하는 CASE 가드를 쓴다. 이 상수는 반대로 시드 파일 값을 항상
# 권위 있게(authoritative) 반영한다 — DB에서 직접 고친 검수 결과(수동 UPDATE)는 재파생·재커밋
# 하지 않으면 다음 seed_from_file 실행에서 되돌아간다(db/catalog/seed/README.md 검수 워크플로 참고).
UPSERT_SEED_COLOR_TERM_SQL = """
INSERT INTO color_synonyms
    (term, canonical, status, embedding, embedding_model, provenance, doc_count, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (term) DO UPDATE SET
    canonical = EXCLUDED.canonical,
    status = EXCLUDED.status,
    -- 임베딩은 term+model의 파생값이라 재실행마다 다시 계산하지만, NULL(예: NON_COLOR_TERMS)로
    -- 들어오면 기존 벡터를 지우지 않는다(UPSERT_COLOR_TERM_SQL과 같은 COALESCE 관례).
    embedding = COALESCE(EXCLUDED.embedding, color_synonyms.embedding),
    embedding_model = COALESCE(EXCLUDED.embedding_model, color_synonyms.embedding_model),
    provenance = EXCLUDED.provenance,
    doc_count = EXCLUDED.doc_count,
    updated_at = now()
"""


def _execute_seed_upserts(
    conn,
    rows: Sequence[SeedColorTermRow],
    embeddings: dict[str, list[float]],
    model: str,
) -> int:
    from pgvector import Vector  # noqa: PLC0415 - LAZY import(pg 미설치 환경 유닛테스트 회피)

    for row in rows:
        vector = embeddings.get(row.term)
        conn.execute(
            UPSERT_SEED_COLOR_TERM_SQL,
            (
                row.term,
                row.canonical,
                row.status,
                Vector(vector) if vector is not None else None,
                model if vector is not None else None,
                row.provenance,
                row.doc_count,
            ),
        )
    return len(rows)


def seed_color_terms(
    dsn: str,
    rows: Sequence[SeedColorTermRow],
    embeddings: dict[str, list[float]],
    model: str,
) -> int:
    """정본 시드 행을 권위 있게(authoritative) upsert한다(단일 트랜잭션)."""
    with _get_pool(dsn).connection() as conn, conn.transaction():
        return _execute_seed_upserts(conn, rows, embeddings, model)


def seed_from_file(
    source_path: str | Path,
    dsn: str,
    embed: EmbedFn | None = None,
    model: str | None = None,
) -> int:
    """정본 시드 파일 → 임베딩 → `color_synonyms` 권위 있게 upsert (오프라인 1회 빌드, 이슈 #258).

    `app.pipelines.category_seed.seed_from_file`과 같은 관례 — 미주입 기본값은 문서(document)
    임베딩이고, 오프라인 1회 빌드(SSE hot path 아님)이므로 hot path 총 예산
    (embedding_total_timeout_s, #391)을 `total_timeout_s=math.inf`로 우회한다. `NON_COLOR_TERMS`는
    임베딩하지 않는다(배치 파이프라인과 같은 sentinel 제외 관례).
    """
    settings = get_settings()
    embed = embed or functools.partial(
        _embed_texts, task_type=settings.embedding_task_document, total_timeout_s=math.inf
    )
    model = model or settings.embedding_model_id
    rows = load_seed_rows(source_path)
    terms_to_embed = list(
        dict.fromkeys(row.term for row in rows if row.term not in NON_COLOR_TERMS)
    )
    embeddings = (
        dict(zip(terms_to_embed, embed(terms_to_embed), strict=True)) if terms_to_embed else {}
    )
    return seed_color_terms(dsn, rows, embeddings, model)
