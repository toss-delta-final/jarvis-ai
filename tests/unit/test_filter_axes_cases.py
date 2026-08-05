"""`evals.filter_axes.cases` 테스트(#334 리뷰 F4, r4 R4-3)."""

from __future__ import annotations

import json

from evals.filter_axes.cases import CASES_PATH, MANIFEST_PATH, load_cases_manifest, load_probe_cases
from evals.goldenset.loader import load_cases


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


def test_probe_case_base_case_ids_stay_referentially_intact_against_goldenset_dev() -> None:
    """R4-3 — probe 케이스의 baseCaseId 척추가 골든셋 dev(v2 이후 전부)에서 여전히 존재하고
    그 query가 baseQuery와 일치하는지 CI가 자동으로 확인한다. dev에서 caseId가 사라지거나
    query 문구가 바뀌면(라벨 재작업·caseId 재번호 등) 이 테스트가 즉시 알린다 — 그때만
    probe_cases.jsonl을 교정하고 fax 버전을 올린다(README 정책)."""
    golden_query_by_id = {case.case_id: case.query for case in load_cases("dev")}
    probe_cases = load_probe_cases()

    missing: list[str] = []
    mismatched: list[tuple[str, str]] = []
    for case in probe_cases:
        if case.base_case_id is None:
            continue
        golden_query = golden_query_by_id.get(case.base_case_id)
        if golden_query is None:
            missing.append(case.case_id)
        elif golden_query != case.base_query:
            mismatched.append((case.case_id, case.base_case_id))

    assert missing == [], f"골든셋 dev에서 사라진 baseCaseId를 참조하는 probe 케이스: {missing}"
    assert mismatched == [], f"baseQuery가 골든셋 query와 어긋난 probe 케이스: {mismatched}"
