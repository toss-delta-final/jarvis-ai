"""Generation, audit, and annotation commands for rerank holdout v2."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.rerank_holdout_v2.annotation import load_review, write_review_packet
from evals.rerank_holdout_v2.generator import GenerationBundle, generate_bundle
from evals.rerank_holdout_v2.io import (
    ROOT,
    dataset_hash,
    load_dataset,
    sha256_file,
    write_json,
    write_jsonl,
)
from evals.rerank_holdout_v2.schema import DatasetManifest
from evals.rerank_holdout_v2.release import Adjudication, build_sealed_labels
from evals.rerank_holdout_v2.validation import AuditReport, validate_bundle

EXIT_OK = 0
EXIT_REJECTED = 2
DEFAULT_CATALOG_PATH = GOLDENSET_ROOT / "fixtures/catalog_snapshot.json"
DEFAULT_CATALOG_SOURCE = "evals/goldenset/fixtures/catalog_snapshot.json"
GENERATED_FILES = (
    "cases/ranking_core.jsonl",
    "cases/safety.jsonl",
    "annotations/draft_labels.jsonl",
    "audit/report.json",
    "manifest.json",
)
SEALED_FILES = (
    "cases/ranking_core.jsonl",
    "cases/safety.jsonl",
    "annotations/sealed_labels.jsonl",
    "audit/report.json",
    "release/review_summary.json",
    "manifest.json",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="prospective rerank holdout v2")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a new immutable draft dataset")
    generate.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
    generate.add_argument("--legacy-root", type=Path, default=GOLDENSET_ROOT)
    generate.add_argument("--seed", type=int, default=631200)
    generate.add_argument("--out", type=Path, default=ROOT)

    audit = subparsers.add_parser("audit", help="revalidate an existing draft dataset")
    audit.add_argument("--root", type=Path, default=ROOT)
    audit.add_argument("--legacy-root", type=Path, default=GOLDENSET_ROOT)
    audit.add_argument("--catalog", type=Path)

    packet = subparsers.add_parser("packet", help="write one blind human review packet")
    packet.add_argument("--root", type=Path, default=ROOT)
    packet.add_argument("--catalog", type=Path)
    packet.add_argument("--reviewer-slot", choices=("A", "B"), required=True)
    packet.add_argument("--out", type=Path, required=True)

    seal = subparsers.add_parser("seal", help="seal two complete independent human reviews")
    seal.add_argument("--root", type=Path, default=ROOT)
    seal.add_argument("--catalog", type=Path)
    seal.add_argument("--legacy-root", type=Path, default=GOLDENSET_ROOT)
    seal.add_argument("--review-a", type=Path, required=True)
    seal.add_argument("--review-b", type=Path, required=True)
    seal.add_argument("--adjudications", type=Path, required=True)
    seal.add_argument("--sealed-at", required=True)
    seal.add_argument("--out", type=Path, required=True)
    return parser


def _load_catalog(path: Path) -> dict[str, dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("catalog snapshot must be a JSON object")
    return value


def _catalog_source(path: Path) -> str:
    if path.resolve() == DEFAULT_CATALOG_PATH.resolve():
        return DEFAULT_CATALOG_SOURCE
    return str(path.resolve())


def _write_draft(
    root: Path,
    *,
    bundle: GenerationBundle,
    report: AuditReport,
    catalog_path: Path,
    catalog_sha256: str,
    seed: int,
) -> DatasetManifest:
    write_jsonl(root / "cases/ranking_core.jsonl", bundle.ranking_cases)
    write_jsonl(root / "cases/safety.jsonl", bundle.safety_cases)
    write_jsonl(root / "annotations/draft_labels.jsonl", bundle.draft_labels)
    write_json(root / "audit/report.json", report)
    artifact_paths = GENERATED_FILES[:-1]
    file_hashes = {relative: sha256_file(root / relative) for relative in artifact_paths}
    manifest = DatasetManifest(
        schema_version="1.0.0",
        dataset_version="1.0.0",
        seed=seed,
        catalog_source_path=_catalog_source(catalog_path),
        catalog_sha256=catalog_sha256,
        dataset_hash=dataset_hash(
            file_hashes,
            catalog_sha256=catalog_sha256,
            seed=seed,
        ),
        ranking_count=len(bundle.ranking_cases),
        safety_count=len(bundle.safety_cases),
        identity_counts=dict(Counter(case.identity.kind for case in bundle.ranking_cases)),
        stratum_counts=dict(Counter(case.stratum for case in bundle.ranking_cases)),
        label_status="draft",
        confirmatory_eligible=False,
        file_hashes=file_hashes,
    )
    write_json(root / "manifest.json", manifest)
    return manifest


def generate_dataset(
    out: Path,
    *,
    catalog_path: Path,
    legacy_root: Path,
    seed: int,
) -> DatasetManifest:
    if out.exists():
        raise ValueError(f"destination already exists: {out}")
    if not catalog_path.is_file():
        raise ValueError(f"catalog snapshot not found: {catalog_path}")
    out.parent.mkdir(parents=True, exist_ok=True)
    catalog_sha256 = sha256_file(catalog_path)
    catalog = _load_catalog(catalog_path)
    bundle = generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=seed)
    report = validate_bundle(
        bundle,
        catalog,
        legacy_root,
        catalog_sha256=catalog_sha256,
        seed=seed,
    )
    temporary = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=str(out.parent.resolve())))
    try:
        manifest = _write_draft(
            temporary,
            bundle=bundle,
            report=report,
            catalog_path=catalog_path,
            catalog_sha256=catalog_sha256,
            seed=seed,
        )
        load_dataset(temporary, label_policy="draft", catalog_path=catalog_path)
        os.replace(temporary, out)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def audit_dataset(
    root: Path,
    *,
    legacy_root: Path,
    catalog_path: Path | None,
) -> AuditReport:
    dataset = load_dataset(root, label_policy="draft", catalog_path=catalog_path)
    bundle = GenerationBundle(
        ranking_cases=dataset.ranking_cases,
        draft_labels=tuple(dataset.labels_by_case.values()),  # type: ignore[arg-type]
        safety_cases=dataset.safety_cases,
    )
    report = validate_bundle(
        bundle,
        dataset.catalog,
        legacy_root,
        catalog_sha256=dataset.manifest.catalog_sha256,
        seed=dataset.manifest.seed,
    )
    recorded = AuditReport.model_validate_json((root / "audit/report.json").read_text())
    if report != recorded:
        raise ValueError("fresh audit does not match committed audit report")
    return report


def _load_adjudications(path: Path) -> dict[tuple[str, int], Adjudication]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("adjudications must be a JSON list")
    rows: dict[tuple[str, int], Adjudication] = {}
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"adjudications[{index}] must be an object")
        try:
            key = (str(raw.pop("caseId")), int(raw.pop("productId")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"adjudications[{index}] needs caseId and productId") from exc
        if key in rows:
            raise ValueError(f"duplicate adjudication: {key}")
        rows[key] = Adjudication.model_validate(raw)
    return rows


def seal_dataset(
    root: Path,
    *,
    review_a_path: Path,
    review_b_path: Path,
    adjudications_path: Path,
    sealed_at: str,
    out: Path,
    legacy_root: Path,
    catalog_path: Path | None,
) -> DatasetManifest:
    if out.exists():
        raise ValueError(f"destination already exists: {out}")
    dataset = load_dataset(root, label_policy="draft", catalog_path=catalog_path)
    review_a = load_review(review_a_path, dataset.ranking_cases)
    review_b = load_review(review_b_path, dataset.ranking_cases)
    release = build_sealed_labels(
        dataset.ranking_cases,
        tuple(dataset.labels_by_case.values()),  # type: ignore[arg-type]
        review_a,
        review_b,
        _load_adjudications(adjudications_path),
        sealed_at=sealed_at,
    )
    released_bundle = GenerationBundle(
        ranking_cases=dataset.ranking_cases,
        draft_labels=release.labels,  # type: ignore[arg-type]
        safety_cases=dataset.safety_cases,
    )
    report = validate_bundle(
        released_bundle,
        dataset.catalog,
        legacy_root,
        catalog_sha256=dataset.manifest.catalog_sha256,
        seed=dataset.manifest.seed,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=str(out.parent.resolve())))
    try:
        write_jsonl(temporary / "cases/ranking_core.jsonl", dataset.ranking_cases)
        write_jsonl(temporary / "cases/safety.jsonl", dataset.safety_cases)
        write_jsonl(temporary / "annotations/sealed_labels.jsonl", release.labels)
        write_json(temporary / "audit/report.json", report)
        write_json(
            temporary / "release/review_summary.json",
            {
                "reviewerIds": [review_a.reviewer_id, review_b.reviewer_id],
                "agreementRate": release.agreement_rate,
                "disagreementCount": release.disagreement_count,
                "adjudicatorId": release.adjudicator_id,
                "sealedAt": sealed_at,
            },
        )
        artifact_paths = SEALED_FILES[:-1]
        file_hashes = {relative: sha256_file(temporary / relative) for relative in artifact_paths}
        manifest = DatasetManifest(
            schema_version=dataset.manifest.schema_version,
            dataset_version=dataset.manifest.dataset_version,
            seed=dataset.manifest.seed,
            catalog_source_path=dataset.manifest.catalog_source_path,
            catalog_sha256=dataset.manifest.catalog_sha256,
            dataset_hash=dataset_hash(
                file_hashes,
                catalog_sha256=dataset.manifest.catalog_sha256,
                seed=dataset.manifest.seed,
            ),
            ranking_count=dataset.manifest.ranking_count,
            safety_count=dataset.manifest.safety_count,
            identity_counts=dataset.manifest.identity_counts,
            stratum_counts=dataset.manifest.stratum_counts,
            label_status="sealed",
            confirmatory_eligible=True,
            file_hashes=file_hashes,
        )
        write_json(temporary / "manifest.json", manifest)
        load_dataset(temporary, label_policy="sealed", catalog_path=catalog_path)
        os.replace(temporary, out)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "generate":
            manifest = generate_dataset(
                args.out,
                catalog_path=args.catalog,
                legacy_root=args.legacy_root,
                seed=args.seed,
            )
            print(
                f"ranking={manifest.ranking_count} draft_labels={manifest.ranking_count} "
                f"safety={manifest.safety_count} confirmatory=false out={args.out}"
            )
        elif args.command == "audit":
            report = audit_dataset(
                args.root,
                legacy_root=args.legacy_root,
                catalog_path=args.catalog,
            )
            print(
                f"ranking={report.ranking_count} guest={report.identity_counts['guest']} "
                f"member={report.identity_counts['member']} safety={report.safety_count} "
                "label_status=draft confirmatory=false"
            )
        elif args.command == "packet":
            dataset = load_dataset(
                args.root,
                label_policy="none",
                catalog_path=args.catalog,
            )
            write_review_packet(
                dataset.ranking_cases,
                dataset.catalog,
                reviewer_slot=args.reviewer_slot,
                out=args.out,
            )
            print(f"reviewer_slot={args.reviewer_slot} rows=6000 out={args.out}")
        else:
            manifest = seal_dataset(
                args.root,
                review_a_path=args.review_a,
                review_b_path=args.review_b,
                adjudications_path=args.adjudications,
                sealed_at=args.sealed_at,
                out=args.out,
                legacy_root=args.legacy_root,
                catalog_path=args.catalog,
            )
            print(
                f"ranking={manifest.ranking_count} label_status=sealed "
                f"confirmatory=true out={args.out}"
            )
    except (OSError, ValueError) as exc:
        print(f"input rejected: {exc}")
        return EXIT_REJECTED
    return EXIT_OK
