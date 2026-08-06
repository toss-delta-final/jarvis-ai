"""trivial(빈 필터) baseline 스크립트 테스트(#334) — 형태·값(recall 0, precision None) 검증."""

from __future__ import annotations

import json

from evals.filter_axes.make_trivial_baseline import build_trivial_baseline_results, write_baseline


def test_trivial_baseline_has_zero_recall_and_none_precision_on_every_evaluated_axis() -> None:
    results = build_trivial_baseline_results()

    assert results["caseCount"] > 0
    assert results["filterAxes"]
    for axis, aggregate in results["filterAxes"].items():
        # support(=match+valueMismatch+missing)가 0인 축(dev에 라벨이 아예 없는 축)은
        # recall 분모도 0이라 None이 맞다 — 예측이 전부 빈 필터라 match/valueMismatch/spurious는
        # 항상 0이므로, support>0인 축만 recall이 결정론적으로 0.0이어야 한다.
        expected_recall = 0.0 if aggregate["support"] > 0 else None
        assert aggregate["presence"]["recall"] == expected_recall, axis
        assert aggregate["presence"]["precision"] is None, axis
        assert aggregate["valueStrict"]["recall"] == expected_recall, axis
        assert aggregate["valueStrict"]["precision"] is None, axis
        # bothEmpty + missing 만 있고 match/valueMismatch/spurious 는 전부 0 이어야 한다
        # (예측이 항상 빈 필터라 과추출·불일치가 있을 수 없다).
        assert aggregate["counts"]["match"] == 0, axis
        assert aggregate["counts"]["valueMismatch"] == 0, axis
        assert aggregate["counts"]["spurious"] == 0, axis


def test_trivial_baseline_dataset_version_and_hash_come_from_goldenset_manifest() -> None:
    from evals.goldenset.loader import ROOT as GOLDENSET_ROOT

    manifest = json.loads((GOLDENSET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    results = build_trivial_baseline_results()

    assert results["datasetVersion"] == manifest["datasetVersion"]
    assert results["datasetHash"] == manifest["datasetHash"]


def test_write_baseline_creates_results_json_in_temp_directory(tmp_path) -> None:
    results = build_trivial_baseline_results()
    output_dir = tmp_path / "trivial_empty"

    write_baseline(output_dir, results)

    written = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    assert written["caseCount"] == results["caseCount"]
    assert written["filterAxesSpec"]["version"] == "filter-axes-v1"


def test_write_baseline_embeds_axes_spec_json_with_matching_hash(tmp_path) -> None:
    """리뷰 F3 — hash만으로는 당시 정규화 규칙을 복원할 수 없다. 동봉 파일의 SHA-256이
    results.json의 filterAxesSpec.sha256과 실제로 일치하는지 확인한다."""
    import hashlib

    results = build_trivial_baseline_results()
    output_dir = tmp_path / "trivial_empty"

    write_baseline(output_dir, results)

    spec_bytes = (output_dir / "filter_axes_spec.json").read_bytes()
    assert hashlib.sha256(spec_bytes).hexdigest() == results["filterAxesSpec"]["sha256"]
    spec = json.loads(spec_bytes)
    assert spec["version"] == results["filterAxesSpec"]["version"]
    assert "axes" in spec
