"""Pure loading, blinding, and analysis helpers for rerank LLM judging."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agents.buyer.recommendation.rerank import (
    _price_medians,
    _price_tier,
    _rating_tier,
    _review_tier,
)
from app.core.config import get_settings
from app.schemas.spring import SpringProduct
from evals.rerank_holdout_v2.io import load_dataset
from evals.rerank_scoring.judge_schema import (
    BlindPresentation,
    CandidateFact,
    CoordinatorMapping,
    JudgeArm,
)


@dataclass(frozen=True)
class SourcePair:
    pair_id: str
    case_id: str
    order_seed: int
    repeat: int
    query: str
    profile_summary: str | None
    candidate_product_ids: tuple[int, ...]
    candidates: tuple[SpringProduct, ...]
    rankings: Mapping[JudgeArm, tuple[int, ...]]
    source_response_sha256: Mapping[JudgeArm, str]
    slices: tuple[str, ...]
    identity: str
    stratum: str
    dataset_hash: str


def _json_int_tuple(raw: str, *, field: str) -> tuple[int, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {field} JSON") from exc
    if (
        not isinstance(value, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} must be a list of distinct integers")
    return tuple(value)


def _pair_id(dataset_hash: str, case_id: str, order_seed: int, repeat: int) -> str:
    identity = f"{dataset_hash}\0{case_id}\0{order_seed}\0{repeat}".encode()
    return f"rbj-{hashlib.sha256(identity).hexdigest()[:20]}"


def load_source_pairs(samples_path: Path, *, dataset_root: Path) -> tuple[SourcePair, ...]:
    """Load saved arm outputs while deliberately requesting no ranking labels."""

    if not samples_path.is_file():
        raise ValueError(f"source samples not found: {samples_path}")
    dataset = load_dataset(dataset_root, label_policy="none")
    cases = {case.case_id: case for case in dataset.ranking_cases}
    grouped: dict[tuple[str, int, int], dict[JudgeArm, dict[str, str]]] = {}
    with samples_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "caseId",
            "arm",
            "orderSeed",
            "repeat",
            "candidateOrder",
            "rankedProductIds",
            "rawResponseSha256",
            "datasetHash",
        }
        if reader.fieldnames is None or not required <= set(reader.fieldnames):
            raise ValueError("source samples are missing required columns")
        for row in reader:
            arm = row["arm"]
            if arm not in {"current", "structured"}:
                continue
            case_id = row["caseId"]
            try:
                key = (case_id, int(row["orderSeed"]), int(row["repeat"]))
            except ValueError as exc:
                raise ValueError(f"{case_id}: invalid seed or repeat") from exc
            bucket = grouped.setdefault(key, {})
            typed_arm: JudgeArm = arm  # type: ignore[assignment]
            if typed_arm in bucket:
                raise ValueError(f"duplicate source sample: {key}/{arm}")
            bucket[typed_arm] = row

    source_pairs: list[SourcePair] = []
    for (case_id, order_seed, repeat), rows in sorted(grouped.items()):
        if set(rows) != {"current", "structured"}:
            continue
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"unknown source case: {case_id}")
        dataset_hashes = {row["datasetHash"] for row in rows.values()}
        if dataset_hashes != {dataset.manifest.dataset_hash}:
            raise ValueError(f"{case_id}: source dataset hash mismatch")
        candidate_orders = {
            _json_int_tuple(row["candidateOrder"], field="candidateOrder") for row in rows.values()
        }
        if len(candidate_orders) != 1:
            raise ValueError(f"{case_id}: paired arms used different candidate orders")
        candidate_ids = next(iter(candidate_orders))
        if not candidate_ids or not set(candidate_ids) <= set(case.candidate_product_ids):
            raise ValueError(f"{case_id}: candidate order is outside the dataset case")
        rankings: dict[JudgeArm, tuple[int, ...]] = {}
        response_hashes: dict[JudgeArm, str] = {}
        for arm, row in rows.items():
            ranking = _json_int_tuple(row["rankedProductIds"], field="rankedProductIds")
            if not ranking or not set(ranking) <= set(candidate_ids):
                raise ValueError(f"{case_id}/{arm}: ranking is outside the candidate order")
            response_hash = row["rawResponseSha256"]
            if len(response_hash) != 64 or any(
                ch not in "0123456789abcdef" for ch in response_hash
            ):
                raise ValueError(f"{case_id}/{arm}: invalid raw response hash")
            rankings[arm] = ranking
            response_hashes[arm] = response_hash
        products = tuple(
            SpringProduct.model_validate(dataset.catalog[str(product_id)])
            for product_id in candidate_ids
        )
        source_pairs.append(
            SourcePair(
                pair_id=_pair_id(dataset.manifest.dataset_hash, case_id, order_seed, repeat),
                case_id=case_id,
                order_seed=order_seed,
                repeat=repeat,
                query=case.query,
                profile_summary=case.profile_summary,
                candidate_product_ids=candidate_ids,
                candidates=products,
                rankings=rankings,
                source_response_sha256=response_hashes,
                slices=tuple(case.slices),
                identity=case.identity.kind,
                stratum=case.stratum,
                dataset_hash=dataset.manifest.dataset_hash,
            )
        )
    if not source_pairs:
        raise ValueError("source samples contain no complete current/structured pairs")
    return tuple(source_pairs)


def _candidate_facts(pair: SourcePair) -> tuple[CandidateFact, ...]:
    settings = get_settings()
    medians = _price_medians(list(pair.candidates), None, settings)
    facts = [
        CandidateFact(
            product_id=product.product_id,
            name=product.name,
            brand=product.brand,
            category=product.category,
            price_level=_price_tier(product.price, median_price, settings),
            rating_level=_rating_tier(product, settings),
            review_level=_review_tier(product, settings),
        )
        for product, median_price in zip(pair.candidates, medians, strict=True)
    ]
    return tuple(sorted(facts, key=lambda row: row.product_id))


def _current_is_a(pair_id: str, mapping_seed: int) -> bool:
    digest = hashlib.sha256(f"{mapping_seed}\0{pair_id}".encode()).digest()
    return digest[0] % 2 == 0


def build_presentations(
    pairs: Sequence[SourcePair], *, mapping_seed: int
) -> tuple[tuple[BlindPresentation, ...], tuple[CoordinatorMapping, ...]]:
    presentations: list[BlindPresentation] = []
    mappings: list[CoordinatorMapping] = []
    for pair in pairs:
        first_a: JudgeArm = "current" if _current_is_a(pair.pair_id, mapping_seed) else "structured"
        first_b: JudgeArm = "structured" if first_a == "current" else "current"
        facts = _candidate_facts(pair)
        for orientation, (side_a, side_b) in enumerate(((first_a, first_b), (first_b, first_a))):
            presentation_id = f"{pair.pair_id}-o{orientation}"
            presentations.append(
                BlindPresentation(
                    presentation_id=presentation_id,
                    pair_id=pair.pair_id,
                    query=pair.query,
                    profile_summary=pair.profile_summary or "(없음)",
                    candidates=facts,
                    ranking_a=pair.rankings[side_a],
                    ranking_b=pair.rankings[side_b],
                    orientation=orientation,  # type: ignore[arg-type]
                )
            )
            mappings.append(
                CoordinatorMapping(
                    presentation_id=presentation_id,
                    pair_id=pair.pair_id,
                    orientation=orientation,  # type: ignore[arg-type]
                    case_id=pair.case_id,
                    order_seed=pair.order_seed,
                    repeat=pair.repeat,
                    side_a_arm=side_a,
                    side_b_arm=side_b,
                    slices=pair.slices,
                    identity=pair.identity,  # type: ignore[arg-type]
                    stratum=pair.stratum,
                    source_response_sha256=dict(pair.source_response_sha256),
                )
            )
    return tuple(presentations), tuple(mappings)
