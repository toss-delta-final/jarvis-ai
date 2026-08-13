"""Stable serialization and hash-checked loading for rerank holdout v2."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TypeVar, cast

from pydantic import BaseModel

from evals.rerank_holdout_v2.schema import (
    DatasetManifest,
    DraftLabels,
    LabelPolicy,
    LoadedDataset,
    RankingCaseCore,
    SafetyCase,
    SealedLabels,
)

ROOT = Path(__file__).resolve().parent / "dataset"
ModelT = TypeVar("ModelT", bound=BaseModel)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_hash(file_hashes: Mapping[str, str], *, catalog_sha256: str, seed: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"catalog\0{catalog_sha256}\nseed\0{seed}\n".encode())
    for relative, file_hash in sorted(file_hashes.items()):
        digest.update(f"{relative}\0{file_hash}\n".encode())
    return digest.hexdigest()


def _wire_row(row: BaseModel | Mapping[str, object]) -> dict[str, object]:
    if isinstance(row, BaseModel):
        return cast(dict[str, object], row.model_dump(by_alias=True, mode="json"))
    return dict(row)


def _stable_json(value: object, *, indent: int | None = None) -> str:
    separators = None if indent is not None else (",", ":")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=separators,
    )


def write_jsonl(path: Path, rows: Iterable[BaseModel | Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wire_rows = [_wire_row(row) for row in rows]
    wire_rows.sort(key=lambda row: str(row.get("caseId", "")))
    content = "".join(f"{_stable_json(row)}\n" for row in wire_rows)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, value: BaseModel | Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_stable_json(_wire_row(value), indent=2)}\n", encoding="utf-8")


def load_jsonl(path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    rows: list[ModelT] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(model.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid row: {exc}") from exc
    return tuple(rows)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return cast(dict[str, object], value)


def _verify_files(root: Path, manifest: DatasetManifest) -> None:
    for relative, expected in manifest.file_hashes.items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"dataset file missing: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"dataset file hash mismatch: {relative}")
    expected_dataset_hash = dataset_hash(
        manifest.file_hashes,
        catalog_sha256=manifest.catalog_sha256,
        seed=manifest.seed,
    )
    if manifest.dataset_hash != expected_dataset_hash:
        raise ValueError("manifest datasetHash mismatch")


def _resolve_catalog(root: Path, source: str, override: Path | None) -> Path:
    if override is not None:
        return override
    candidates = (root / source, Path.cwd() / source)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"catalog snapshot not found: {source}")


def load_dataset(
    root: Path = ROOT,
    *,
    label_policy: LabelPolicy = "none",
    catalog_path: Path | None = None,
) -> LoadedDataset:
    manifest = DatasetManifest.model_validate(_load_json(root / "manifest.json"))
    if label_policy == "sealed" and (
        manifest.label_status != "sealed" or not manifest.confirmatory_eligible
    ):
        raise ValueError("sealed labels required for confirmatory evaluation")
    if label_policy == "draft" and manifest.label_status != "draft":
        raise ValueError("draft label policy requires a draft dataset")

    _verify_files(root, manifest)
    resolved_catalog = _resolve_catalog(root, manifest.catalog_source_path, catalog_path)
    if sha256_file(resolved_catalog) != manifest.catalog_sha256:
        raise ValueError("catalog snapshot hash mismatch")
    catalog = _load_json(resolved_catalog)

    ranking_cases = load_jsonl(root / "cases/ranking_core.jsonl", RankingCaseCore)
    safety_cases = load_jsonl(root / "cases/safety.jsonl", SafetyCase)
    labels_by_case: dict[str, DraftLabels | SealedLabels] = {}
    if label_policy == "draft":
        label_rows: Iterable[DraftLabels | SealedLabels] = load_jsonl(
            root / "annotations/draft_labels.jsonl", DraftLabels
        )
    elif label_policy == "sealed":
        label_rows = load_jsonl(root / "annotations/sealed_labels.jsonl", SealedLabels)
    else:
        label_rows = ()
    for label in label_rows:
        if label.case_id in labels_by_case:
            raise ValueError(f"duplicate labels for {label.case_id}")
        labels_by_case[label.case_id] = label

    if len(ranking_cases) != manifest.ranking_count:
        raise ValueError("rankingCount does not match ranking core rows")
    if len(safety_cases) != manifest.safety_count:
        raise ValueError("safetyCount does not match safety rows")
    if label_policy != "none" and set(labels_by_case) != {case.case_id for case in ranking_cases}:
        raise ValueError("labels must exactly cover ranking cases")

    return LoadedDataset(
        manifest=manifest,
        ranking_cases=ranking_cases,
        labels_by_case=labels_by_case,
        safety_cases=safety_cases,
        catalog=cast(Mapping[str, dict[str, object]], catalog),
    )
