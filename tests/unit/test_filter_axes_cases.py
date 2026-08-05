"""`evals.filter_axes.cases.load_cases_manifest` 테스트(#334 리뷰 F4)."""

from __future__ import annotations

import json

from evals.filter_axes.cases import CASES_PATH, MANIFEST_PATH, load_cases_manifest


def test_load_cases_manifest_returns_real_dataset_version_hash_and_verified_true() -> None:
    manifest = load_cases_manifest()
    on_disk = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["datasetVersion"] == on_disk["datasetVersion"]
    assert manifest["datasetHash"] == on_disk["datasetHash"]
    assert manifest["datasetHashVerified"] is True


def test_load_cases_manifest_flags_mismatch_without_raising(tmp_path) -> None:
    """`load_probe_cases`(로더)는 해시 불일치에서 예외를 던지지만, `load_cases_manifest`는
    산출물에 실을 값을 만드는 자리라 예외 대신 `datasetHashVerified: False`로 알린다."""
    tampered = tmp_path / "probe_cases.jsonl"
    tampered.write_bytes(CASES_PATH.read_bytes() + b"\n")

    manifest = load_cases_manifest(tampered, manifest_path=MANIFEST_PATH)

    assert manifest["datasetHashVerified"] is False
