from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.rerank_scoring.judge import build_presentations, load_source_pairs
from evals.rerank_scoring.judge_schema import JudgeVerdict

ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = ROOT / "evals/rerank_holdout_v2/dataset"
SOURCE_DIR = ROOT / "evals/rerank_scoring/baselines/20260813-holdout-v2-draft-current-structured-n3"
SOURCE_SAMPLES = SOURCE_DIR / "samples.csv"


def test_source_loader_pairs_saved_outputs_without_loading_draft_labels() -> None:
    pairs = load_source_pairs(SOURCE_SAMPLES, dataset_root=DATASET_ROOT)

    assert len(pairs) == 599
    assert len({pair.case_id for pair in pairs}) == 200
    assert all(set(pair.rankings) == {"current", "structured"} for pair in pairs)
    assert all(pair.candidate_product_ids for pair in pairs)
    assert all(
        set(ranking) <= set(pair.candidate_product_ids)
        for pair in pairs
        for ranking in pair.rankings.values()
    )
    assert not hasattr(pairs[0], "relevance_grades")
    assert not hasattr(pairs[0], "candidate_provenance")


def test_presentations_are_deterministic_blind_and_exactly_position_swapped() -> None:
    pairs = load_source_pairs(SOURCE_SAMPLES, dataset_root=DATASET_ROOT)[:12]

    first_presentations, first_mappings = build_presentations(pairs, mapping_seed=631200)
    second_presentations, second_mappings = build_presentations(pairs, mapping_seed=631200)

    assert [row.model_dump(by_alias=True) for row in first_presentations] == [
        row.model_dump(by_alias=True) for row in second_presentations
    ]
    assert [row.model_dump(by_alias=True) for row in first_mappings] == [
        row.model_dump(by_alias=True) for row in second_mappings
    ]
    assert len(first_presentations) == len(first_mappings) == 24

    by_pair = defaultdict(list)
    mapping_by_pair = defaultdict(list)
    for presentation in first_presentations:
        by_pair[presentation.pair_id].append(presentation)
        public = presentation.model_dump(by_alias=True, mode="json")
        assert set(public) == {
            "schemaVersion",
            "presentationId",
            "pairId",
            "query",
            "profileSummary",
            "candidates",
            "rankingA",
            "rankingB",
        }
        assert [row["productId"] for row in public["candidates"]] == sorted(
            row["productId"] for row in public["candidates"]
        )
        serialized = json.dumps(public, ensure_ascii=False).lower()
        for forbidden in (
            "current",
            "structured",
            "hybrid",
            "relevancegrades",
            "rankingdecisions",
            "candidateprovenance",
            "searchrank",
            "idealorder",
        ):
            assert forbidden not in serialized
    for mapping in first_mappings:
        mapping_by_pair[mapping.pair_id].append(mapping)

    for pair_id, rows in by_pair.items():
        rows.sort(key=lambda row: row.orientation)
        assert [row.orientation for row in rows] == [0, 1]
        assert rows[0].ranking_a == rows[1].ranking_b
        assert rows[0].ranking_b == rows[1].ranking_a
        assert rows[0].candidates == rows[1].candidates
        maps = sorted(mapping_by_pair[pair_id], key=lambda row: row.orientation)
        assert maps[0].side_a_arm == maps[1].side_b_arm
        assert maps[0].side_b_arm == maps[1].side_a_arm
        assert maps[0].side_a_arm != maps[0].side_b_arm


def test_judge_verdict_rejects_arm_disclosure_invalid_bounds_and_unknown_fields() -> None:
    valid = {
        "schemaVersion": "rerank-blind-verdict-v1",
        "winner": "A",
        "confidence": 0.8,
        "reasonCodes": ["QUERY_CONSTRAINT_FIT", "TOP_ORDER_QUALITY"],
        "explanation": "A가 요청 조건을 더 잘 지키고 상위 결과가 유용하다.",
    }
    verdict = JudgeVerdict.model_validate(valid)
    assert verdict.winner == "A"

    invalid_rows = (
        ({**valid, "winner": "structured"}, "winner"),
        ({**valid, "confidence": 1.1}, "confidence"),
        ({**valid, "reasonCodes": ["INTENT_FIT"]}, "reasonCodes"),
        ({**valid, "explanation": "structured arm이 낫다"}, "arm identity"),
        ({**valid, "arm": "current"}, "extra"),
    )
    for row, message in invalid_rows:
        with pytest.raises(ValidationError, match=message):
            JudgeVerdict.model_validate(row)
