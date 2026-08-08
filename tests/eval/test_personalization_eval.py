from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.agents.buyer.recommendation import graph as recommendation_graph
from evals.metrics.settings import EvaluationSettings
from evals.personalization import cli as personalization_cli
from evals.personalization.cli import main, normalize_paired_artifacts

BASELINE = Path("evals/personalization/baselines/dev-v2")


@pytest.mark.eval
def test_personalization_run_is_deterministic_across_environment_and_clock(
    tmp_path, monkeypatch
) -> None:
    """이 테스트가 고정하는 축: 환경변수(`EXPOSE_MAX`)·시계가 달라도 산출물이 재현된다.

    정규화(`normalize_paired_artifacts`)가 걷어내는 축: 실행 인스턴스(`run`)와 워킹트리 상태
    (`commitSha`·`dirty`) — 이 실행 도중 리포를 편집하거나 커밋이 끼어도 이 테스트는 깨지지
    않는다(#413).

    남겨 둔 계약: `hashes`(uv.lock·goldenset manifest/fixtures·decompose.py·rerank.py·
    config.py 의 sha256, `evals.metrics.run_manifest.build_run_manifest` 참조)는 여전히 비교
    대상이다 — 이 실행 도중 그 파일들을 편집하면 실패하는 것이 의도다.
    """
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("EXPOSE_MAX", "1")
    assert main(["--out", str(first)]) == 0
    monkeypatch.setenv("EXPOSE_MAX", "99")
    monkeypatch.setattr(recommendation_graph, "_now", lambda: datetime(2030, 1, 1))
    assert main(["--out", str(second)]) == 0
    assert normalize_paired_artifacts(first) == normalize_paired_artifacts(second)


@pytest.mark.eval
def test_all_arms_and_weights_preserve_hard_filters(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    comparison = json.loads((out / "comparison.json").read_text())
    assert comparison["hardFilter"]["violationCount"] == 0
    assert comparison["hardFilter"]["verdict"] == "pass"
    assert len(comparison["hardFilter"]["cells"]) == 25


@pytest.mark.eval
def test_case_derived_noisy_and_repeated_arms_are_discriminative(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    comparison = json.loads((out / "comparison.json").read_text())
    pairs = comparison["pairedComparisons"]
    assert any(
        delta != 0 for delta in pairs["noisy_vs_clean"]["overall"]["ndcgAtK"]["10"]["deltas"]
    )
    assert any(
        delta != 0 for delta in pairs["repeated_vs_clean"]["overall"]["ndcgAtK"]["10"]["deltas"]
    )
    curves = comparison["weightAblation"]
    assert any(
        cell["noisy"]["ndcgAtK"]["10"] != cell["clean"]["ndcgAtK"]["10"] for cell in curves.values()
    )


@pytest.mark.eval
def test_comparison_separates_profile_identity_and_noise_counterfactuals(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    comparison = json.loads((out / "comparison.json").read_text())
    assert comparison["primaryComparison"] == "clean_vs_member_no_profile"
    assert set(comparison["pairedComparisons"]) == {
        "clean_vs_member_no_profile",
        "noisy_vs_clean",
        "repeated_vs_clean",
        "member_no_profile_vs_guest",
    }
    configs = comparison["armModelConfig"]
    clean = {key: value for key, value in configs["clean"].items() if key != "profileArm"}
    no_profile = {
        key: value for key, value in configs["member_no_profile"].items() if key != "profileArm"
    }
    assert clean == no_profile


@pytest.mark.eval
def test_overreach_verdicts_match_committed_baseline(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    fresh = json.loads((out / "overreach.json").read_text())
    committed = json.loads((BASELINE / "overreach.json").read_text())
    for name in ("intentContradiction", "forbiddenOrRecentInclusion", "cleanNoisyDrop"):
        assert fresh[name]["verdict"] == committed[name]["verdict"]


@pytest.mark.eval
def test_live_preflight_rejection_does_not_execute(tmp_path, monkeypatch) -> None:
    called = False

    def forbidden_execution(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        personalization_cli,
        "get_settings",
        lambda: EvaluationSettings(model_eval_max_calls_per_run=1),
    )
    monkeypatch.setattr(personalization_cli, "run_tier_l", forbidden_execution)
    out = tmp_path / "live"
    assert main(["--live", "--case-limit", "1", "--out", str(out)]) == 2
    assert not called
    assert not out.exists()


@pytest.mark.eval
def test_live_mode_dispatches_three_scope_arms_after_preflight(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_run(output_dir, **kwargs):
        captured["output"] = output_dir
        captured.update(kwargs)

    monkeypatch.setattr(personalization_cli, "run_tier_l", fake_run)
    out = tmp_path / "live"
    assert main(["--live", "--case-limit", "1", "--out", str(out)]) == 0
    assert captured["output"] == out
    assert captured["arm_names"] == ["guest", "clean_rerank_only", "clean_both"]
    assert len(captured["cases"]) == 1
