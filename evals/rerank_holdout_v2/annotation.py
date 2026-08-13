"""Blind reviewer packets and strict review imports."""

from __future__ import annotations

import csv
import hashlib
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.rerank_holdout_v2.schema import RankingCaseCore

REVIEW_FIELDS = (
    "caseId",
    "candidateSlot",
    "productId",
    "query",
    "profileSummary",
    "name",
    "brand",
    "category",
    "price",
    "rating",
    "reviewCount",
    "grade",
    "reviewerId",
    "reviewedAt",
    "rationale",
)
_SLOT_SEEDS = {"A": 631201, "B": 631202}
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class ReviewSubmission:
    reviewer_id: str
    grades: Mapping[tuple[str, int], int]
    reviewed_at: Mapping[tuple[str, int], str]
    rationales: Mapping[tuple[str, int], str]


def _case_seed(case_id: str, slot_seed: int) -> int:
    digest = hashlib.sha256(f"{slot_seed}:{case_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _catalog_product(
    catalog: Mapping[str, Mapping[str, object]], product_id: int
) -> Mapping[str, object]:
    product = catalog.get(str(product_id))
    if product is None:
        raise ValueError(f"catalog product missing: {product_id}")
    return product


def write_review_packet(
    cases: Sequence[RankingCaseCore],
    catalog: Mapping[str, Mapping[str, object]],
    *,
    reviewer_slot: str,
    out: Path,
) -> Path:
    """Write a blind 6,000-row packet without draft grade or label provenance."""

    if reviewer_slot not in _SLOT_SEEDS:
        raise ValueError("reviewer_slot must be A or B")
    if out.exists():
        raise ValueError(f"review packet already exists: {out}")
    rows: list[dict[str, object]] = []
    for case in sorted(cases, key=lambda value: value.case_id):
        product_ids = list(case.candidate_product_ids)
        random.Random(_case_seed(case.case_id, _SLOT_SEEDS[reviewer_slot])).shuffle(product_ids)
        for slot, product_id in enumerate(product_ids, 1):
            product = _catalog_product(catalog, product_id)
            rows.append(
                {
                    "caseId": case.case_id,
                    "candidateSlot": slot,
                    "productId": product_id,
                    "query": case.query,
                    "profileSummary": case.profile_summary or "",
                    "name": product.get("name") or "",
                    "brand": product.get("brandName") or "",
                    "category": product.get("categoryName") or "",
                    "price": product.get("price") if product.get("price") is not None else "",
                    "rating": product.get("rating") if product.get("rating") is not None else "",
                    "reviewCount": (
                        product.get("reviewCount") if product.get("reviewCount") is not None else ""
                    ),
                    "grade": "",
                    "reviewerId": "",
                    "reviewedAt": "",
                    "rationale": "",
                }
            )
    expected_rows = sum(len(case.candidate_product_ids) for case in cases)
    if len(rows) != expected_rows:
        raise ValueError("review packet row count does not match candidate count")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return out


def _expected_keys(cases: Sequence[RankingCaseCore]) -> set[tuple[str, int]]:
    return {
        (case.case_id, product_id) for case in cases for product_id in case.candidate_product_ids
    }


def load_review(path: Path, cases: Sequence[RankingCaseCore]) -> ReviewSubmission:
    """Load a complete single-human submission and reject partial or mixed identity packets."""

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REVIEW_FIELDS:
            raise ValueError("review CSV columns do not match the registered packet")
        rows = list(reader)
    grades: dict[tuple[str, int], int] = {}
    reviewed_at: dict[tuple[str, int], str] = {}
    rationales: dict[tuple[str, int], str] = {}
    reviewer_ids: set[str] = set()
    for line_number, row in enumerate(rows, 2):
        try:
            key = (row["caseId"], int(row["productId"]))
            grade = int(row["grade"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: invalid case/product/grade") from exc
        if key in grades:
            raise ValueError(f"{path}:{line_number}: duplicate candidate judgment {key}")
        if grade < 0 or grade > 3:
            raise ValueError(f"{path}:{line_number}: grade must be between 0 and 3")
        reviewer_id = row["reviewerId"].strip()
        timestamp = row["reviewedAt"].strip()
        rationale = row["rationale"].strip()
        if not reviewer_id or not _TIMESTAMP_RE.fullmatch(timestamp) or not rationale:
            raise ValueError(
                f"{path}:{line_number}: reviewerId, ISO reviewedAt, and rationale are required"
            )
        reviewer_ids.add(reviewer_id)
        grades[key] = grade
        reviewed_at[key] = timestamp
        rationales[key] = rationale
    expected = _expected_keys(cases)
    if set(grades) != expected:
        missing = len(expected - set(grades))
        foreign = len(set(grades) - expected)
        raise ValueError(
            f"review must exactly cover every candidate (missing={missing}, foreign={foreign})"
        )
    if len(reviewer_ids) != 1:
        raise ValueError("review submission must contain exactly one reviewer identity")
    return ReviewSubmission(
        reviewer_id=reviewer_ids.pop(),
        grades=grades,
        reviewed_at=reviewed_at,
        rationales=rationales,
    )
