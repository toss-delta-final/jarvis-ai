from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.rerank_holdout_v2.annotation import load_review, write_review_packet
from evals.rerank_holdout_v2.cli import main
from evals.rerank_holdout_v2.generator import GenerationBundle, generate_bundle
from evals.rerank_holdout_v2.io import ROOT, load_dataset, sha256_file
from evals.rerank_holdout_v2.release import build_sealed_labels

CATALOG_PATH = GOLDENSET_ROOT / "fixtures/catalog_snapshot.json"


@pytest.fixture(scope="module")
def catalog() -> dict[str, dict[str, object]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bundle(catalog) -> GenerationBundle:
    return generate_bundle(
        catalog,
        catalog_sha256=sha256_file(CATALOG_PATH),
        seed=631200,
    )


def _complete_packet(
    packet: Path,
    output: Path,
    *,
    reviewer_id: str,
    grades_by_key: dict[tuple[str, int], int],
) -> Path:
    with packet.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        key = (row["caseId"], int(row["productId"]))
        row["grade"] = str(grades_by_key[key])
        row["reviewerId"] = reviewer_id
        row["reviewedAt"] = "2026-08-13T12:00:00+09:00"
        row["rationale"] = "카탈로그 사실과 요청 적합성을 독립 검수했다."
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return output


def _draft_grades(bundle: GenerationBundle) -> dict[tuple[str, int], int]:
    labels_by_case = {labels.case_id: labels for labels in bundle.draft_labels}
    return {
        (case.case_id, product_id): labels_by_case[case.case_id].relevance_grades.get(product_id, 0)
        for case in bundle.ranking_cases
        for product_id in case.candidate_product_ids
    }


def _reviews(bundle, catalog, tmp_path: Path):
    grades = _draft_grades(bundle)
    packet_a = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="A",
        out=tmp_path / "packet-a.csv",
    )
    packet_b = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="B",
        out=tmp_path / "packet-b.csv",
    )
    review_a = load_review(
        _complete_packet(
            packet_a,
            tmp_path / "review-a.csv",
            reviewer_id="human-reviewer-a",
            grades_by_key=grades,
        ),
        bundle.ranking_cases,
    )
    review_b = load_review(
        _complete_packet(
            packet_b,
            tmp_path / "review-b.csv",
            reviewer_id="human-reviewer-b",
            grades_by_key=grades,
        ),
        bundle.ranking_cases,
    )
    return review_a, review_b


def test_review_packets_hide_heuristic_labels(bundle, catalog, tmp_path: Path) -> None:
    packet = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="A",
        out=tmp_path / "review-a.csv",
    )
    text = packet.read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))

    assert "heuristic" not in text
    assert "suggestedGrade" not in text
    assert len(rows) == 200 * 30
    assert {row["grade"] for row in rows} == {""}
    assert {row["reviewerId"] for row in rows} == {""}


def test_reviewer_packets_use_independent_candidate_orders(bundle, catalog, tmp_path: Path) -> None:
    packet_a = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="A",
        out=tmp_path / "review-a.csv",
    )
    packet_b = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="B",
        out=tmp_path / "review-b.csv",
    )
    with packet_a.open(newline="", encoding="utf-8") as handle:
        rows_a = list(csv.DictReader(handle))
    with packet_b.open(newline="", encoding="utf-8") as handle:
        rows_b = list(csv.DictReader(handle))

    first_case = bundle.ranking_cases[0].case_id
    order_a = [row["productId"] for row in rows_a if row["caseId"] == first_case]
    order_b = [row["productId"] for row in rows_b if row["caseId"] == first_case]
    assert set(order_a) == set(order_b)
    assert order_a != order_b


def test_review_import_rejects_missing_candidate(bundle, catalog, tmp_path: Path) -> None:
    packet = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="A",
        out=tmp_path / "packet.csv",
    )
    completed = _complete_packet(
        packet,
        tmp_path / "review.csv",
        reviewer_id="human-reviewer-a",
        grades_by_key=_draft_grades(bundle),
    )
    lines = completed.read_text(encoding="utf-8").splitlines()
    completed.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly cover every candidate"):
        load_review(completed, bundle.ranking_cases)


def test_release_rejects_same_reviewer_identity(bundle, catalog, tmp_path: Path) -> None:
    review_a, review_b = _reviews(bundle, catalog, tmp_path)
    same_reviewer = replace(review_b, reviewer_id=review_a.reviewer_id)

    with pytest.raises(ValueError, match="independent reviewer"):
        build_sealed_labels(
            bundle.ranking_cases,
            bundle.draft_labels,
            review_a,
            same_reviewer,
            {},
        )


def test_release_rejects_unadjudicated_disagreement(bundle, catalog, tmp_path: Path) -> None:
    review_a, review_b = _reviews(bundle, catalog, tmp_path)
    key = next(iter(review_b.grades))
    changed = dict(review_b.grades)
    changed[key] = 0 if changed[key] != 0 else 1
    review_b = replace(review_b, grades=changed)

    with pytest.raises(ValueError, match="unadjudicated disagreement"):
        build_sealed_labels(
            bundle.ranking_cases,
            bundle.draft_labels,
            review_a,
            review_b,
            {},
        )


def test_release_seals_only_after_complete_adjudication(bundle, catalog, tmp_path: Path) -> None:
    review_a, review_b = _reviews(bundle, catalog, tmp_path)
    key = next(key for key, grade in review_b.grades.items() if grade == 2)
    changed = dict(review_b.grades)
    changed[key] = 1
    review_b = replace(review_b, grades=changed)
    adjudications = {
        key: {
            "grade": 2,
            "adjudicatorId": "human-adjudicator",
            "adjudicatedAt": "2026-08-13T13:00:00+09:00",
            "rationale": "두 검수 근거와 상품 필드를 비교해 grade 2로 판정했다.",
        }
    }

    release = build_sealed_labels(
        bundle.ranking_cases,
        bundle.draft_labels,
        review_a,
        review_b,
        adjudications,
        sealed_at="2026-08-13T14:00:00+09:00",
    )

    assert len(release.labels) == 200
    assert release.disagreement_count == 1
    assert release.agreement_rate == pytest.approx(5999 / 6000)
    assert release.adjudicator_id == "human-adjudicator"
    assert all(label.label_status == "sealed" for label in release.labels)
    assert all(label.label_source == "human-reviewed" for label in release.labels)
    assert all(
        label.reviewer_ids == ["human-reviewer-a", "human-reviewer-b"] for label in release.labels
    )


def test_seal_command_writes_a_confirmatory_eligible_copy(bundle, catalog, tmp_path: Path) -> None:
    grades = _draft_grades(bundle)
    packet_a = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="A",
        out=tmp_path / "seal-packet-a.csv",
    )
    packet_b = write_review_packet(
        bundle.ranking_cases,
        catalog,
        reviewer_slot="B",
        out=tmp_path / "seal-packet-b.csv",
    )
    review_a_path = _complete_packet(
        packet_a,
        tmp_path / "seal-review-a.csv",
        reviewer_id="human-reviewer-a",
        grades_by_key=grades,
    )
    review_b_path = _complete_packet(
        packet_b,
        tmp_path / "seal-review-b.csv",
        reviewer_id="human-reviewer-b",
        grades_by_key=grades,
    )
    adjudications = tmp_path / "adjudications.json"
    adjudications.write_text("[]\n", encoding="utf-8")
    out = tmp_path / "sealed"

    assert (
        main(
            [
                "seal",
                "--root",
                str(ROOT),
                "--review-a",
                str(review_a_path),
                "--review-b",
                str(review_b_path),
                "--adjudications",
                str(adjudications),
                "--sealed-at",
                "2026-08-13T14:00:00+09:00",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    sealed = load_dataset(out, label_policy="sealed")
    assert sealed.manifest.confirmatory_eligible is True
    assert sealed.manifest.label_status == "sealed"
    assert len(sealed.labels_by_case) == 200
