"""priority 프로브 CLI — 전부 `--dry-run`(가짜 LLM)이라 CI 에서 API 콜이 0이다 (#281 TASK 3 §6)."""

from __future__ import annotations

import json
from pathlib import Path

from evals.priority_probe.cli import main

ARTIFACT_NAMES = {
    "results.json",
    "run_manifest.json",
    "report.md",
    "samples.csv",
    "cells.csv",
    "failures.csv",
}


def _run(out: Path, arm: str, *extra: str) -> int:
    return main(["--arm", arm, "--out", str(out), "--dry-run", "--repeats", "2", *extra])


def _results(out: Path) -> dict:
    return json.loads((out / "results.json").read_text(encoding="utf-8"))


def test_classifier_dry_run_writes_every_artifact(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, "classifier") == 0
    assert {path.name for path in out.iterdir()} == ARTIFACT_NAMES
    results = _results(out)
    assert results["arm"] == "classifier"
    assert results["cellCount"] == 12
    assert results["unfilledCells"] == []
    assert results["dryRun"] is True


def test_inline_dry_run_writes_every_artifact(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, "inline") == 0
    results = _results(out)
    assert results["arm"] == "inline"
    assert results["dryRun"] is True


def test_dry_run_never_touches_the_network(tmp_path: Path, monkeypatch) -> None:
    """`--dry-run` 은 `build_live_delegate` 를 호출하면 안 된다 — API 콜 0 을 배관으로 강제한다."""
    import evals.priority_probe.cli as cli_module

    def _forbidden(**_kwargs):
        raise AssertionError("dry-run 인데 실 provider 클라이언트를 만들려고 했다")

    monkeypatch.setattr(cli_module, "build_live_delegate", _forbidden)
    out = tmp_path / "run"
    assert _run(out, "classifier") == 0


def test_existing_out_dir_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    assert _run(out, "classifier") == 2


def test_unknown_case_id_is_refused(tmp_path: Path) -> None:
    assert _run(tmp_path / "run", "classifier", "--case-ids", "nope") == 2


def test_case_limit_narrows_the_run(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, "classifier", "--case-limit", "3") == 0
    assert _results(out)["cellCount"] == 3


def test_corrupt_fixture_path_is_rejected_before_network(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    out = tmp_path / "run"
    assert main(["--arm", "classifier", "--out", str(out), "--dry-run", "--fixture", str(bad)]) == 2
    assert not out.exists()


def test_unfillable_run_exits_4_and_lists_the_cells(tmp_path: Path, monkeypatch) -> None:
    import evals.priority_probe.cli as cli_module
    from evals.priority_probe.fakes import ScriptedPriorityLLM

    monkeypatch.setattr(
        cli_module,
        "ScriptedPriorityLLM",
        lambda fixture, arm: ScriptedPriorityLLM(fixture, arm=arm, always_fail=True),
    )
    out = tmp_path / "run"
    assert _run(out, "classifier", "--case-limit", "2") == 4
    results = _results(out)
    assert len(results["unfilledCells"]) == 2
    assert results["unfilledCells"][0]["got"] == 0
    assert (out / "failures.csv").read_text(encoding="utf-8").count("LLMError") > 0


def test_budget_exceeded_exits_3_with_partial_artifacts(tmp_path: Path, monkeypatch) -> None:
    import evals.priority_probe.cli as cli_module
    from evals.model_eval.budget import BudgetExceeded
    from evals.priority_probe.fakes import ScriptedPriorityLLM

    class _BudgetBurstLLM(ScriptedPriorityLLM):
        async def complete(self, **kwargs: object) -> str:
            if self._attempts >= 4:
                raise BudgetExceeded("maxCallsExceeded")
            return await super().complete(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cli_module, "ScriptedPriorityLLM", lambda fixture, arm: _BudgetBurstLLM(fixture, arm=arm)
    )
    out = tmp_path / "run"
    assert (
        main(
            [
                "--arm",
                "classifier",
                "--out",
                str(out),
                "--dry-run",
                "--repeats",
                "2",
                "--concurrency",
                "1",
            ]
        )
        == 3
    )
    assert {path.name for path in out.iterdir()} == ARTIFACT_NAMES
    assert _results(out)["cellCount"] < 12


def test_probe_is_not_wired_into_ci() -> None:
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    for path in workflows.glob("*.y*ml"):
        assert "priority_probe" not in path.read_text(encoding="utf-8")


def test_manifest_records_both_prompt_hashes(tmp_path: Path) -> None:
    from evals.priority_probe.manifest import classifier_prompt_sha256

    out = tmp_path / "run"
    assert _run(out, "inline") == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    hashes = manifest["hashes"]
    assert hashes["inlineSystemPrompt"] != hashes["classifierSystemPrompt"]
    assert hashes["classifierSystemPrompt"] == classifier_prompt_sha256()
    report = (out / "report.md").read_text(encoding="utf-8")
    assert hashes["inlineSystemPrompt"][:12] in report
    assert hashes["classifierSystemPrompt"][:12] in report


def test_classifier_arm_records_its_own_fixed_prompt_identity(tmp_path: Path) -> None:
    """분류기 팔은 프롬프트 교체가 없다 — results.prompt 가 decompose 문면이 아니라
    `need_priority._SYSTEM` 을 가리켜야 한다(다른 팔의 프롬프트를 기록하면 표가 거짓이 된다)."""
    from evals.priority_probe.manifest import classifier_prompt_sha256

    out = tmp_path / "run"
    assert _run(out, "classifier") == 0
    results = _results(out)
    assert results["prompt"]["sha256"] == classifier_prompt_sha256()
    assert "need_priority" in results["prompt"]["source"]


def test_priority_order_pairs_axis_is_the_headline_metric(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, "classifier") == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    lines = [line for line in report.splitlines() if line.startswith("| `")]
    assert lines[0].startswith("| `priorityOrderPairs`")  # 본질 축이 표 맨 앞에 온다
