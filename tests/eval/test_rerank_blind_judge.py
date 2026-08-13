from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.rerank_scoring.judge_cli import EXIT_OK, EXIT_REJECTED, main as judge_main
from evals.rerank_scoring.judge_merge_cli import main as judge_merge_main
from evals.rerank_scoring.judge import (
    analyze_judgments,
    build_presentations,
    load_source_pairs,
)
from evals.rerank_scoring.judge_report import reproduce_analysis
from evals.rerank_scoring.judge_schema import (
    CoordinatorMapping,
    JudgeResponse,
    JudgeVerdict,
)

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


def _mapping(
    pair_id: str,
    orientation: int,
    *,
    case_id: str,
    order_seed: int,
    identity: str,
    stratum: str,
) -> CoordinatorMapping:
    side_a = "current" if orientation == 0 else "structured"
    side_b = "structured" if orientation == 0 else "current"
    return CoordinatorMapping(
        presentation_id=f"{pair_id}-o{orientation}",
        pair_id=pair_id,
        orientation=orientation,
        case_id=case_id,
        order_seed=order_seed,
        repeat=0,
        side_a_arm=side_a,
        side_b_arm=side_b,
        slices=("ranking", stratum, identity),
        identity=identity,
        stratum=stratum,
        source_response_sha256={"current": "a" * 64, "structured": "b" * 64},
    )


def _responses_for_outcome(
    mappings: tuple[CoordinatorMapping, CoordinatorMapping], outcome: str
) -> list[JudgeResponse]:
    rows: list[JudgeResponse] = []
    for mapping in mappings:
        if outcome in {"current", "structured"}:
            winner = "A" if mapping.side_a_arm == outcome else "B"
        elif outcome == "tie":
            winner = "tie"
        elif outcome == "same-a":
            winner = "A"
        elif outcome == "same-b":
            winner = "B"
        else:
            raise AssertionError(outcome)
        rows.append(
            JudgeResponse(
                presentation_id=mapping.presentation_id,
                pair_id=mapping.pair_id,
                attempt=1,
                latency_ms=100,
                raw_response_sha256="c" * 64,
                verdict={
                    "winner": winner,
                    "confidence": 0.8,
                    "reasonCodes": ["TOP_ORDER_QUALITY"],
                    "explanation": "상위 상품 순서가 더 유용하다.",
                },
            )
        )
    return rows


def test_analysis_maps_swaps_excludes_unstable_pairs_and_clusters_by_case() -> None:
    mappings: list[CoordinatorMapping] = []
    responses: list[JudgeResponse] = []

    specs = (
        ("pair-a1", "case-a", 1, "guest", "general", "structured"),
        ("pair-a2", "case-a", 2, "guest", "general", "structured"),
        ("pair-a3", "case-a", 3, "guest", "general", "structured"),
        ("pair-b1", "case-b", 1, "member", "personalization", "current"),
        ("pair-c1", "case-c", 1, "guest", "general", "tie"),
        ("pair-d1", "case-d", 1, "member", "personalization", "same-a"),
        ("pair-e1", "case-e", 1, "member", "personalization", "same-b"),
    )
    for pair_id, case_id, seed, identity, stratum, outcome in specs:
        pair_mappings = (
            _mapping(
                pair_id,
                0,
                case_id=case_id,
                order_seed=seed,
                identity=identity,
                stratum=stratum,
            ),
            _mapping(
                pair_id,
                1,
                case_id=case_id,
                order_seed=seed,
                identity=identity,
                stratum=stratum,
            ),
        )
        mappings.extend(pair_mappings)
        responses.extend(_responses_for_outcome(pair_mappings, outcome))

    incomplete = (
        _mapping(
            "pair-f1",
            0,
            case_id="case-f",
            order_seed=1,
            identity="guest",
            stratum="general",
        ),
        _mapping(
            "pair-f1",
            1,
            case_id="case-f",
            order_seed=1,
            identity="guest",
            stratum="general",
        ),
    )
    mappings.extend(incomplete)
    responses.extend(_responses_for_outcome(incomplete, "structured")[:1])

    first = analyze_judgments(
        responses,
        mappings,
        bootstrap_seed=631200,
        bootstrap_samples=2_000,
    )
    second = analyze_judgments(
        responses,
        mappings,
        bootstrap_seed=631200,
        bootstrap_samples=2_000,
    )

    assert first == second
    assert first["coverage"] == {
        "plannedPresentations": 16,
        "completedPresentations": 15,
        "failedPresentations": 1,
        "plannedPairs": 8,
        "completePairs": 7,
        "incompletePairs": 1,
    }
    assert first["pairOutcomes"] == {
        "structured": 3,
        "current": 1,
        "tie": 1,
        "unstable": 2,
    }
    assert first["swapConsistencyRate"] == pytest.approx(5 / 7)
    assert first["decisive"] == {
        "denominator": 4,
        "structuredWins": 3,
        "currentWins": 1,
        "structuredWinRate": 0.75,
    }
    assert first["positionBias"]["sameSideASelections"] == 1
    assert first["positionBias"]["sameSideBSelections"] == 1
    clustered = first["caseClusteredPreferenceShare"]
    assert clustered["eligibleCaseCount"] == 3
    assert clustered["value"] == pytest.approx(0.5)
    assert clustered["ci95"][0] <= clustered["value"] <= clustered["ci95"][1]
    assert first["caseOutcomes"] == {
        "structured": 1,
        "current": 1,
        "tie": 1,
        "unstable": 2,
        "incomplete": 1,
    }
    assert first["slices"]["guest"]["pairOutcomes"]["structured"] == 3
    assert first["slices"]["personalization"]["pairOutcomes"]["unstable"] == 2


def test_dry_run_cli_writes_reproducible_blind_artifacts_and_report(tmp_path: Path) -> None:
    out = tmp_path / "blind-run"
    exit_code = judge_main(
        [
            "--source-dir",
            str(SOURCE_DIR),
            "--dataset-root",
            str(DATASET_ROOT),
            "--case-ids",
            "rh2-adversarial-0001",
            "--dry-run",
            "--out",
            str(out),
        ]
    )

    assert exit_code == EXIT_OK
    assert {path.name for path in out.iterdir()} == {
        "presentations.jsonl",
        "judge_responses.jsonl",
        "coordinator_mapping.jsonl",
        "failures.jsonl",
        "results.json",
        "run_manifest.json",
        "report.md",
    }
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert results["status"] == "exploratory"
    assert results["coverage"] == {
        "plannedPresentations": 6,
        "completedPresentations": 6,
        "failedPresentations": 0,
        "plannedPairs": 3,
        "completePairs": 3,
        "incompletePairs": 0,
    }
    assert results["pairOutcomes"] == {
        "structured": 3,
        "current": 0,
        "tie": 0,
        "unstable": 0,
    }
    assert reproduce_analysis(out) == results

    public_text = (out / "presentations.jsonl").read_text(encoding="utf-8").lower()
    response_text = (out / "judge_responses.jsonl").read_text(encoding="utf-8").lower()
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
        assert forbidden not in public_text
        assert forbidden not in response_text
    mapping_text = (out / "coordinator_mapping.jsonl").read_text(encoding="utf-8")
    assert '"sideAArm"' in mapping_text
    assert '"structured"' in mapping_text
    report = (out / "report.md").read_text(encoding="utf-8").lower()
    assert "exploratory" in report
    assert "not confirmatory" in report
    assert "position" in report


def test_cli_refuses_output_reuse_and_live_execution_without_explicit_budgets(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    common = [
        "--source-dir",
        str(SOURCE_DIR),
        "--dataset-root",
        str(DATASET_ROOT),
        "--case-ids",
        "rh2-adversarial-0001",
    ]

    assert judge_main([*common, "--dry-run", "--out", str(existing)]) == EXIT_REJECTED
    assert judge_main([*common, "--out", str(tmp_path / "live")]) == EXIT_REJECTED


def test_source_loader_rejects_dataset_hash_drift(tmp_path: Path) -> None:
    rows = SOURCE_SAMPLES.read_text(encoding="utf-8").splitlines()
    header = rows[0]
    first_case_rows = [row for row in rows[1:] if row.startswith("rh2-adversarial-0001,")][:2]
    assert len(first_case_rows) == 2
    bad_hash = "f" * 64
    original_hash = "4fa52e596f97c60c2b067c0ca6b30345ed574fcb7ad67acb67009b344a49f87b"
    path = tmp_path / "samples.csv"
    path.write_text(
        "\n".join([header, *(row.replace(original_hash, bad_hash) for row in first_case_rows)])
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dataset hash mismatch"):
        load_source_pairs(path, dataset_root=DATASET_ROOT)


def test_merge_cli_combines_disjoint_shards_and_reproduces_analysis(tmp_path: Path) -> None:
    shards: list[Path] = []
    for index, seed in enumerate((11, 29)):
        shard = tmp_path / f"shard-{index}"
        assert (
            judge_main(
                [
                    "--source-dir",
                    str(SOURCE_DIR),
                    "--dataset-root",
                    str(DATASET_ROOT),
                    "--case-ids",
                    "rh2-adversarial-0001",
                    "--order-seeds",
                    str(seed),
                    "--dry-run",
                    "--out",
                    str(shard),
                ]
            )
            == EXIT_OK
        )
        shards.append(shard)

    out = tmp_path / "merged"
    assert (
        judge_merge_main(
            [
                "--shards",
                ",".join(str(path) for path in shards),
                "--out",
                str(out),
            ]
        )
        == EXIT_OK
    )
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert results["coverage"]["plannedPairs"] == 2
    assert results["coverage"]["plannedPresentations"] == 4
    assert reproduce_analysis(out) == results
    assert manifest["merge"]["shardCount"] == 2
    assert len(manifest["merge"]["shardManifestSha256"]) == 2


def test_merge_cli_rejects_overlapping_presentations(tmp_path: Path) -> None:
    shard = tmp_path / "shard"
    assert (
        judge_main(
            [
                "--source-dir",
                str(SOURCE_DIR),
                "--dataset-root",
                str(DATASET_ROOT),
                "--case-ids",
                "rh2-adversarial-0001",
                "--order-seeds",
                "11",
                "--dry-run",
                "--out",
                str(shard),
            ]
        )
        == EXIT_OK
    )

    assert (
        judge_merge_main(["--shards", f"{shard},{shard}", "--out", str(tmp_path / "merged")])
        == EXIT_REJECTED
    )
