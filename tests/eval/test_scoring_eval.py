from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.agents.buyer.recommendation import graph as recommendation_graph
from app.services import spring_client
from evals.scoring.cli import main, normalize_paired_artifacts

_REAL_GET_RECENT_PURCHASES = spring_client.get_recent_purchases


@pytest.mark.eval
# dev goldenset 전체 × passthrough/scoring 2 arm을 결정론 검증을 위해 2번 도는
# 전체 그래프 재실행 — combo_matrix/personalization과 같은 이유로 CI에서 제외.
@pytest.mark.slow
def test_paired_scoring_run_is_deterministic_and_complete(tmp_path, monkeypatch) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setattr(
        spring_client, "get_recent_purchases", _REAL_GET_RECENT_PURCHASES
    )
    monkeypatch.setenv("EXPOSE_MAX", "1")
    assert main(["--out", str(first)]) == 0
    monkeypatch.setenv("EXPOSE_MAX", "99")
    monkeypatch.setattr(recommendation_graph, "_now", lambda: datetime(2030, 1, 1))
    assert main(["--out", str(second)]) == 0

    assert normalize_paired_artifacts(first) == normalize_paired_artifacts(second)
    assert {
        "passthrough",
        "scoring",
        "comparison.json",
        "comparison.md",
        "scores_scoring.json",
        "run_manifest.json",
        "latency.json",
    } <= {path.name for path in first.iterdir()}
    manifest = json.loads((first / "run_manifest.json").read_text())
    assert manifest["datasetHash"]
    assert manifest["arms"]["scoring"]["rerank"] == "deterministicScoringBaseline"
    assert manifest["rankingExcludedCaseIds"]
    expected_command = (
        f"uv run python -m evals.scoring --out {first} --split dev --seed 20260803"
    )
    for arm in ("passthrough", "scoring"):
        arm_manifest = json.loads((first / arm / "run_manifest.json").read_text())
        assert arm_manifest["run"]["command"] == expected_command
        assert "--arm" not in arm_manifest["run"]["command"]
