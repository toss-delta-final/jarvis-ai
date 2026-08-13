"""Deterministic facts, validation, and reason rendering for code-assisted rerank."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from app.agents.buyer.recommendation.rerank_grounding import (
    NEUTRAL_RATIONALE,
    CandidateGroundingFacts,
    GroundingDecision,
)
from app.schemas.spring import ProductSearchFilters, SpringProduct

EvidenceCode: TypeAlias = Literal[
    "ATTRIBUTE_MATCH",
    "BRAND_MATCH",
    "CATEGORY_MATCH",
    "COLOR_MATCH",
    "PRICE_RANGE_MATCH",
    "RATING_THRESHOLD_MATCH",
    "RATING_HIGH",
    "REVIEW_MANY",
    "PRICE_RELATIVE_LOW",
]
SemanticReasonCode: TypeAlias = Literal[
    "DIRECT_INTENT_MATCH",
    "USE_CASE_MATCH",
    "PROFILE_TIEBREAK",
    "NO_SEMANTIC_REASON",
]

_SEMANTIC_REASON_CODES = frozenset(
    {
        "DIRECT_INTENT_MATCH",
        "USE_CASE_MATCH",
        "PROFILE_TIEBREAK",
        "NO_SEMANTIC_REASON",
    }
)


class CodeAssistedSchemaError(ValueError):
    """The code-assisted response cannot produce a trustworthy selection."""


@dataclass(frozen=True)
class CodeScoringContext:
    filters: ProductSearchFilters
    search_rank_by_id: Mapping[int, int]
    need_of: Mapping[int, str] | None = None
    total_budget: int | None = None


@dataclass(frozen=True)
class CodeEvidence:
    ref: str
    code: EvidenceCode
    field: str | None = None
    value: str | None = None

    def prompt_dict(self) -> dict[str, object]:
        item: dict[str, object] = {"ref": self.ref, "code": self.code}
        if self.field is not None:
            item["field"] = self.field
        if self.value is not None:
            item["value"] = self.value
        return item


@dataclass(frozen=True)
class CandidateCodeSignals:
    product_id: int
    search_rank: int
    need: str | None
    facts: CandidateGroundingFacts
    evidence: tuple[CodeEvidence, ...]
    rating_quality: int | None
    review_confidence: int | None
    condition_matched: int
    condition_applicable: int

    def prompt_dict(self) -> dict[str, object]:
        return {
            "objectiveComponents": {
                "ratingQuality": self.rating_quality,
                "reviewConfidence": self.review_confidence,
                "explicitConditionCoverage": {
                    "matched": self.condition_matched,
                    "applicable": self.condition_applicable,
                },
            },
            "evidence": [item.prompt_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class CodeAssistedDecision:
    product_id: int
    search_rank: int
    llm_rank: int
    semantic_intent_fit: int | None
    use_case_fit: int | None
    profile_fit: int | None
    semantic_reason_code: str
    evidence_refs: tuple[str, ...]
    used_evidence_refs: tuple[str, ...]
    score_valid: bool
    fallback_reason: str | None


@dataclass(frozen=True)
class CodeAssistedComputation:
    ordered_product_ids: tuple[int, ...]
    decisions: tuple[CodeAssistedDecision, ...]
    model_items_by_id: Mapping[int, Mapping[str, object]]
    grounding_decisions: tuple[GroundingDecision, ...]


def _norm(value: object) -> str:
    return str(value).strip().casefold()


def _value_matches(want: object, have: object) -> bool:
    if isinstance(have, (list, tuple, set, frozenset)):
        return any(_value_matches(want, item) for item in have)
    normalized_want = _norm(want)
    normalized_have = _norm(have)
    if not normalized_want or not normalized_have:
        return False
    if normalized_want.isdigit():
        return normalized_want == normalized_have
    return normalized_want in normalized_have


def _attribute_value(product: SpringProduct, axis: str) -> object | None:
    attributes = product.attributes or {}
    if axis in attributes:
        return attributes[axis]
    normalized_axis = _norm(axis)
    for key, value in attributes.items():
        if _norm(key) == normalized_axis:
            return value
    return None


def _rating_quality(level: str) -> int | None:
    if level in {"높음", "매우높음"}:
        return 2
    if level == "보통":
        return 1
    if level == "낮음":
        return 0
    return None


def _review_confidence(level: str) -> int | None:
    if level in {"많음", "매우많음"}:
        return 2
    if level == "보통":
        return 1
    if level == "적음":
        return 0
    return None


def _condition_evidence(
    product: SpringProduct, filters: ProductSearchFilters
) -> tuple[list[CodeEvidence], int, int]:
    evidence: list[CodeEvidence] = []
    matched = 0
    applicable = 0

    if filters.category and product.category:
        applicable += 1
        if _norm(filters.category) == _norm(product.category):
            matched += 1
            evidence.append(CodeEvidence(ref="CATEGORY_MATCH", code="CATEGORY_MATCH"))

    requested_brands = [value for value in filters.brand or [] if value.strip()]
    if requested_brands and product.brand:
        applicable += 1
        if any(_norm(value) == _norm(product.brand) for value in requested_brands):
            matched += 1
            evidence.append(CodeEvidence(ref="BRAND_MATCH", code="BRAND_MATCH"))

    if (
        filters.price_min is not None or filters.price_max is not None
    ) and product.price is not None:
        applicable += 1
        in_range = (filters.price_min is None or product.price >= filters.price_min) and (
            filters.price_max is None or product.price <= filters.price_max
        )
        if in_range:
            matched += 1
            evidence.append(CodeEvidence(ref="PRICE_RANGE_MATCH", code="PRICE_RANGE_MATCH"))

    if filters.rating_min is not None and product.rating is not None and product.review_count != 0:
        applicable += 1
        if product.rating >= filters.rating_min:
            matched += 1
            evidence.append(
                CodeEvidence(ref="RATING_THRESHOLD_MATCH", code="RATING_THRESHOLD_MATCH")
            )

    if filters.color:
        color_value = _attribute_value(product, "색상")
        if color_value is None:
            color_value = _attribute_value(product, "color")
        if color_value is not None:
            applicable += 1
            if _value_matches(filters.color, color_value):
                matched += 1
                evidence.append(
                    CodeEvidence(
                        ref="COLOR_MATCH",
                        code="COLOR_MATCH",
                        field="색상",
                        value=filters.color.strip(),
                    )
                )

    for axis, wanted in (filters.attr_conditions or {}).items():
        actual = _attribute_value(product, axis)
        if actual is None:
            continue
        applicable += 1
        if _value_matches(wanted, actual):
            matched += 1
            clean_axis = axis.strip()
            evidence.append(
                CodeEvidence(
                    ref=f"ATTRIBUTE_MATCH:{clean_axis}",
                    code="ATTRIBUTE_MATCH",
                    field=clean_axis,
                    value=wanted.strip(),
                )
            )

    return evidence, matched, applicable


def build_candidate_code_signals(
    candidates: Sequence[SpringProduct],
    facts_by_id: Mapping[int, CandidateGroundingFacts],
    context: CodeScoringContext,
) -> dict[int, CandidateCodeSignals]:
    """Build named code-owned signals without inventing a composite relevance score."""

    signals: dict[int, CandidateCodeSignals] = {}
    for fallback_rank, product in enumerate(candidates, 1):
        if product.product_id in signals:
            continue
        facts = facts_by_id[product.product_id]
        evidence, matched, applicable = _condition_evidence(product, context.filters)
        if facts.rating_level in {"높음", "매우높음"}:
            evidence.append(CodeEvidence(ref="RATING_HIGH", code="RATING_HIGH"))
        if facts.review_level in {"많음", "매우많음"}:
            evidence.append(CodeEvidence(ref="REVIEW_MANY", code="REVIEW_MANY"))
        if facts.price_level in {"저렴", "매우저렴"}:
            evidence.append(CodeEvidence(ref="PRICE_RELATIVE_LOW", code="PRICE_RELATIVE_LOW"))
        signals[product.product_id] = CandidateCodeSignals(
            product_id=product.product_id,
            search_rank=context.search_rank_by_id.get(product.product_id, fallback_rank),
            need=(context.need_of or {}).get(product.product_id),
            facts=facts,
            evidence=tuple(evidence),
            rating_quality=_rating_quality(facts.rating_level),
            review_confidence=_review_confidence(facts.review_level),
            condition_matched=matched,
            condition_applicable=applicable,
        )
    return signals


def _bounded_int(item: Mapping[str, object], key: str, maximum: int) -> int | None:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def _evidence_phrases(evidence: CodeEvidence) -> tuple[str, str]:
    if evidence.code == "ATTRIBUTE_MATCH":
        axis = (evidence.field or "속성").strip() or "속성"
        return (
            f"요청한 {axis} 조건과 일치하고",
            f"요청한 {axis} 조건과 일치하는 상품이에요",
        )
    return {
        "BRAND_MATCH": ("요청한 브랜드와 일치하고", "요청한 브랜드와 일치하는 상품이에요"),
        "CATEGORY_MATCH": (
            "요청한 카테고리와 일치하고",
            "요청한 카테고리와 일치하는 상품이에요",
        ),
        "COLOR_MATCH": ("요청한 색상 조건과 일치하고", "요청한 색상 조건과 일치하는 상품이에요"),
        "PRICE_RANGE_MATCH": (
            "요청한 가격 범위 안에 있고",
            "요청한 가격 범위 안에 있는 상품이에요",
        ),
        "RATING_THRESHOLD_MATCH": (
            "요청한 평점 조건을 만족하고",
            "요청한 평점 조건을 만족하는 상품이에요",
        ),
        "RATING_HIGH": ("평점 평가가 높고", "평점 평가가 높은 상품이에요"),
        "REVIEW_MANY": ("리뷰 정보가 많고", "리뷰 정보가 많은 상품이에요"),
        "PRICE_RELATIVE_LOW": (
            "같은 후보군에서 비교적 저렴하고",
            "같은 후보군에서 비교적 저렴한 상품이에요",
        ),
    }[evidence.code]


def _semantic_phrases(reason_code: str) -> tuple[str, str] | None:
    return {
        "DIRECT_INTENT_MATCH": (
            "요청하신 핵심 의도에 잘 맞고",
            "요청하신 핵심 의도에 잘 맞는 상품이에요",
        ),
        "USE_CASE_MATCH": (
            "말씀하신 사용 상황에 활용하기 좋고",
            "말씀하신 사용 상황에 활용하기 좋은 상품이에요",
        ),
        "PROFILE_TIEBREAK": (
            "요청을 만족하면서 평소 취향과도 가깝고",
            "요청을 만족하면서 평소 취향과도 가까운 상품이에요",
        ),
    }.get(reason_code)


def _semantic_reason_supported(
    reason_code: str,
    *,
    intent_fit: int,
    use_case_fit: int,
    profile_fit: int,
    profile_available: bool,
) -> bool:
    if reason_code == "NO_SEMANTIC_REASON":
        return True
    if reason_code == "DIRECT_INTENT_MATCH":
        return intent_fit >= 3
    if reason_code == "USE_CASE_MATCH":
        return use_case_fit >= 2
    if reason_code == "PROFILE_TIEBREAK":
        return profile_available and profile_fit == 1 and intent_fit >= 1
    return False


_EVIDENCE_PRIORITY: dict[EvidenceCode, int] = {
    "ATTRIBUTE_MATCH": 0,
    "COLOR_MATCH": 1,
    "BRAND_MATCH": 2,
    "PRICE_RANGE_MATCH": 3,
    "RATING_THRESHOLD_MATCH": 4,
    "CATEGORY_MATCH": 5,
    "RATING_HIGH": 6,
    "REVIEW_MANY": 7,
    "PRICE_RELATIVE_LOW": 8,
}


def _render_reason(
    signals: CandidateCodeSignals,
    evidence_refs: Sequence[str],
    semantic_reason_code: str,
    *,
    semantic_supported: bool,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    evidence_by_ref = {item.ref: item for item in signals.evidence}
    unknown: list[str] = []
    used: list[CodeEvidence] = []
    seen: set[str] = set()
    for ref in evidence_refs:
        if ref in seen:
            continue
        seen.add(ref)
        evidence = evidence_by_ref.get(ref)
        if evidence is None:
            unknown.append(ref)
        else:
            used.append(evidence)
    used.sort(key=lambda item: (_EVIDENCE_PRIORITY[item.code], item.ref))
    selected = used[:2]
    semantic = _semantic_phrases(semantic_reason_code) if semantic_supported else None

    phrases = [_evidence_phrases(item) for item in selected]
    if len(phrases) < 2 and semantic is not None:
        phrases.append(semantic)
    if not phrases:
        return NEUTRAL_RATIONALE, (), tuple(unknown)
    if len(phrases) == 1:
        return phrases[0][1], tuple(item.ref for item in selected), tuple(unknown)
    return (
        f"{phrases[0][0]} {phrases[1][1]}",
        tuple(item.ref for item in selected),
        tuple(unknown),
    )


def fallback_reason_for_signals(signals: CandidateCodeSignals) -> str:
    """Render one factual reason for a search-order fallback candidate."""

    reason, _, _ = _render_reason(
        signals,
        [item.ref for item in signals.evidence],
        "NO_SEMANTIC_REASON",
        semantic_supported=True,
    )
    return reason


def parse_code_assisted_ranking(
    raw_ranked: object,
    signals_by_id: Mapping[int, CandidateCodeSignals],
    *,
    profile_available: bool,
    expose_max: int,
) -> CodeAssistedComputation:
    """Validate an LLM-selected subset and render reasons from code-owned evidence."""

    if not isinstance(raw_ranked, list):
        raise CodeAssistedSchemaError("code-assisted ranked must be a list")
    if expose_max <= 0:
        raise CodeAssistedSchemaError("code-assisted expose_max must be positive")

    rejection_counts: Counter[str] = Counter()
    rows_by_id: dict[int, list[tuple[int, Mapping[str, object]]]] = {}
    for llm_rank, raw_item in enumerate(raw_ranked, 1):
        if not isinstance(raw_item, Mapping):
            rejection_counts["non_object"] += 1
            continue
        product_id = raw_item.get("productId")
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            rejection_counts["invalid_product_id"] += 1
            continue
        if product_id not in signals_by_id:
            rejection_counts["foreign_product_id"] += 1
            continue
        rows_by_id.setdefault(product_id, []).append((llm_rank, raw_item))

    valid_rows: list[tuple[int, Mapping[str, object], CandidateCodeSignals]] = []
    decisions: list[CodeAssistedDecision] = []
    for product_id, rows in rows_by_id.items():
        signals = signals_by_id[product_id]
        if len(rows) != 1:
            rejection_counts["duplicate_selection"] += len(rows)
            decisions.append(
                CodeAssistedDecision(
                    product_id=product_id,
                    search_rank=signals.search_rank,
                    llm_rank=rows[0][0],
                    semantic_intent_fit=None,
                    use_case_fit=None,
                    profile_fit=None,
                    semantic_reason_code="",
                    evidence_refs=(),
                    used_evidence_refs=(),
                    score_valid=False,
                    fallback_reason="duplicate_selection",
                )
            )
            continue
        llm_rank, item = rows[0]
        intent_fit = _bounded_int(item, "semanticIntentFit", 4)
        use_case_fit = _bounded_int(item, "useCaseFit", 3)
        profile_fit = _bounded_int(item, "profileFit", 1)
        failure_reason = None
        if intent_fit is None:
            failure_reason = "invalid_semantic_intent_fit"
        elif use_case_fit is None:
            failure_reason = "invalid_use_case_fit"
        elif profile_fit is None:
            failure_reason = "invalid_profile_fit"
        elif not profile_available and profile_fit != 0:
            failure_reason = "profile_fit_without_profile"
        if failure_reason is not None:
            rejection_counts[failure_reason] += 1
            decisions.append(
                CodeAssistedDecision(
                    product_id=product_id,
                    search_rank=signals.search_rank,
                    llm_rank=llm_rank,
                    semantic_intent_fit=intent_fit,
                    use_case_fit=use_case_fit,
                    profile_fit=profile_fit,
                    semantic_reason_code="",
                    evidence_refs=(),
                    used_evidence_refs=(),
                    score_valid=False,
                    fallback_reason=failure_reason,
                )
            )
            continue
        valid_rows.append((llm_rank, item, signals))

    if not valid_rows:
        diagnostics = {
            "rows": len(raw_ranked),
            "non_object": rejection_counts["non_object"],
            "invalid_product_id": rejection_counts["invalid_product_id"],
            "foreign_product_id": rejection_counts["foreign_product_id"],
            "duplicate_selection": rejection_counts["duplicate_selection"],
            "invalid_semantic_intent_fit": rejection_counts["invalid_semantic_intent_fit"],
            "invalid_use_case_fit": rejection_counts["invalid_use_case_fit"],
            "invalid_profile_fit": rejection_counts["invalid_profile_fit"],
            "profile_fit_without_profile": rejection_counts["profile_fit_without_profile"],
        }
        summary = ", ".join(f"{key}={value}" for key, value in diagnostics.items())
        raise CodeAssistedSchemaError(f"code-assisted rerank has no valid selections ({summary})")

    valid_rows.sort(key=lambda row: row[0])
    model_items_by_id: dict[int, Mapping[str, object]] = {}
    grounding_decisions: list[GroundingDecision] = []
    ordered_ids: list[int] = []
    for llm_rank, item, signals in valid_rows[:expose_max]:
        intent_fit = int(item["semanticIntentFit"])
        use_case_fit = int(item["useCaseFit"])
        profile_fit = int(item["profileFit"])
        raw_reason_code = item.get("semanticReasonCode")
        reason_code = raw_reason_code if isinstance(raw_reason_code, str) else ""
        raw_refs = item.get("evidenceRefs")
        refs = (
            tuple(raw_refs)
            if isinstance(raw_refs, list) and all(isinstance(value, str) for value in raw_refs)
            else ()
        )
        semantic_supported = _semantic_reason_supported(
            reason_code,
            intent_fit=intent_fit,
            use_case_fit=use_case_fit,
            profile_fit=profile_fit,
            profile_available=profile_available,
        )
        rendered, used_refs, unknown_refs = _render_reason(
            signals,
            refs,
            reason_code,
            semantic_supported=semantic_supported,
        )
        failures: list[str] = []
        if reason_code not in _SEMANTIC_REASON_CODES:
            failures.append("unknown_semantic_reason_code")
        elif not semantic_supported:
            failures.append("unsupported_semantic_reason")
        if not isinstance(raw_refs, list) or not all(isinstance(value, str) for value in raw_refs):
            failures.append("invalid_evidence_refs")
        if unknown_refs:
            failures.append("unsupported_evidence_refs")
        model_rationale = item.get("rationale")
        grounding_decisions.append(
            GroundingDecision(
                product_id=signals.product_id,
                requested_reason_code=reason_code,
                evidence_fields=refs,
                model_rationale=model_rationale if isinstance(model_rationale, str) else "",
                rendered_rationale=rendered,
                supported=not failures,
                downgraded=bool(failures),
                failure_reason=";".join(failures) if failures else None,
            )
        )
        decisions.append(
            CodeAssistedDecision(
                product_id=signals.product_id,
                search_rank=signals.search_rank,
                llm_rank=llm_rank,
                semantic_intent_fit=intent_fit,
                use_case_fit=use_case_fit,
                profile_fit=profile_fit,
                semantic_reason_code=reason_code,
                evidence_refs=refs,
                used_evidence_refs=used_refs,
                score_valid=True,
                fallback_reason=None,
            )
        )
        ordered_ids.append(signals.product_id)
        model_items_by_id[signals.product_id] = item

    decisions.sort(key=lambda item: (item.llm_rank, item.product_id))
    return CodeAssistedComputation(
        ordered_product_ids=tuple(ordered_ids),
        decisions=tuple(decisions),
        model_items_by_id=model_items_by_id,
        grounding_decisions=tuple(grounding_decisions),
    )
