"""ablation baseline 20260803-dev-full-n5 축별 재채점 테스트(#334 R3-2) — 결정론·오프라인."""

from __future__ import annotations

import json
from pathlib import Path

from evals.filter_axes.rescore_ablation import (
    DEFAULT_BASELINE,
    build_rescore_results,
    write_rescore_artifacts,
)

COMMITTED_DIR = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "filter_axes"
    / "baselines"
    / "20260803-dev-full-n5-rescored"
)


def test_rescore_regenerates_byte_identical_committed_results_json(tmp_path) -> None:
    """재생성 — 스크립트를 tmp로 돌려 커밋 산출물과 바이트 동일함을 확인한다."""
    results = build_rescore_results(DEFAULT_BASELINE)
    write_rescore_artifacts(tmp_path, results)

    committed = (COMMITTED_DIR / "results.json").read_bytes()
    regenerated = (tmp_path / "results.json").read_bytes()
    assert regenerated == committed

    committed_spec = (COMMITTED_DIR / "filter_axes_spec.json").read_bytes()
    regenerated_spec = (tmp_path / "filter_axes_spec.json").read_bytes()
    assert regenerated_spec == committed_spec


def test_rescore_spot_counts_match_acceptance_criteria() -> None:
    """스팟 수치 — 수용 기준(오케스트레이터 독립 실측)과 커밋 산출물을 대조한다."""
    results = json.loads((COMMITTED_DIR / "results.json").read_text(encoding="utf-8"))
    arms = results["arms"]

    pipeline = arms["pipeline"]
    assert pipeline["rowCount"] == 155
    assert pipeline["uniqueCaseCount"] == 31
    assert pipeline["repeats"] == 5
    assert round(pipeline["meanFilterAccuracy"], 4) == 0.0669
    keyword = pipeline["filterAxes"]["keyword"]
    assert keyword["counts"] == {
        "match": 3,
        "valueMismatch": 152,
        "spurious": 0,
        "missing": 0,
        "bothEmpty": 0,
    }
    assert round(keyword["valueStrict"]["precision"], 3) == 0.019
    assert round(keyword["valueStrict"]["recall"], 3) == 0.019
    assert keyword["presence"]["precision"] == 1.0
    assert keyword["presence"]["recall"] == 1.0
    category = pipeline["filterAxes"]["category"]
    assert category["counts"]["missing"] == 20
    assert category["valueStrict"]["recall"] == 0.0
    assert category["valueStrict"]["precision"] is None
    price_max = pipeline["filterAxes"]["price_max"]
    assert price_max["counts"] == {
        "match": 25,
        "valueMismatch": 0,
        "spurious": 1,
        "missing": 0,
        "bothEmpty": 129,
    }
    assert round(price_max["valueStrict"]["precision"], 3) == 0.962
    assert price_max["valueStrict"]["recall"] == 1.0

    single_call = arms["single_call"]
    assert round(single_call["meanFilterAccuracy"], 4) == 0.2777
    sc_category = single_call["filterAxes"]["category"]
    assert sc_category["counts"] == {
        "match": 18,
        "valueMismatch": 0,
        "spurious": 97,
        "missing": 2,
        "bothEmpty": 38,
    }
    assert round(sc_category["valueStrict"]["precision"], 3) == 0.157
    assert round(sc_category["valueStrict"]["recall"], 3) == 0.900
    sc_keyword = single_call["filterAxes"]["keyword"]
    assert round(sc_keyword["valueStrict"]["precision"], 3) == 0.441
    assert round(sc_keyword["valueStrict"]["recall"], 3) == 0.387
    sc_price_max = single_call["filterAxes"]["price_max"]
    assert sc_price_max["valueStrict"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0}

    scoring = arms["scoring"]
    assert scoring["meanFilterAccuracy"] == 1.0
    for axis, aggregate in scoring["filterAxes"].items():
        # scoring arm은 scripted 정답을 그대로 낸다 — support(=그 축이 dev에 실제 라벨된
        # 횟수)가 0인 축(예: 이 데이터셋에서 color)은 분모 자체가 없어 precision/recall이
        # None이다. support>0인 축만 전부 1.0이어야 한다.
        if aggregate["support"] == 0:
            assert aggregate["valueStrict"]["precision"] is None, axis
            assert aggregate["valueStrict"]["recall"] is None, axis
            continue
        assert aggregate["valueStrict"]["precision"] == 1.0, axis
        assert aggregate["valueStrict"]["recall"] == 1.0, axis


def test_rescore_source_identification_is_embedded() -> None:
    results = json.loads((COMMITTED_DIR / "results.json").read_text(encoding="utf-8"))

    source = results["source"]
    assert source["baselineDir"] == "evals/ablation/baselines/20260803-dev-full-n5"
    assert set(source["arms"]) == {"pipeline", "single_call", "scoring"}
    for arm_info in source["arms"].values():
        assert len(arm_info["sha256"]) == 64
    assert source["datasetVersion"] == "1.0.0"
    assert len(source["datasetHash"]) == 64
    assert results["filterAxesSpec"]["version"] == "filter-axes-v1"
