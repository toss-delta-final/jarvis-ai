"""프로브 CLI — 전부 `--dry-run`(가짜 LLM)이라 CI 에서 API 콜이 0이다 (#260)."""

from __future__ import annotations

import json
from pathlib import Path

from evals.intent_probe.cli import main
from evals.intent_probe.client import repo_system_prompt
from evals.intent_probe.report import normalized_artifact_bytes

ARTIFACT_NAMES = {
    "results.json",
    "run_manifest.json",
    "report.md",
    "samples.csv",
    "cells.csv",
    "failures.csv",
}


def _run(out: Path, *extra: str) -> int:
    return main(["--out", str(out), "--dry-run", "--repeats", "2", *extra])


def _results(out: Path) -> dict:
    return json.loads((out / "results.json").read_text(encoding="utf-8"))


def test_dry_run_writes_every_artifact(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    assert {path.name for path in out.iterdir()} == ARTIFACT_NAMES
    results = _results(out)
    assert results["cellCount"] == 53
    assert results["unfilledCells"] == []
    assert results["dryRun"] is True


def test_dry_run_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert _run(first) == 0
    assert _run(second) == 0
    assert normalized_artifact_bytes(first) == normalized_artifact_bytes(second)


def test_dry_run_is_independent_of_concurrency(tmp_path: Path) -> None:
    # 동시성이 바뀌어도 같은 표가 나와야 한다 — 가짜 LLM 의 오답 주기가 셀 밖으로 새면
    # 스케줄링에 따라 숫자가 달라지고, 결정론이 '운'이 된다.
    serial, parallel = tmp_path / "c1", tmp_path / "c8"
    assert _run(serial, "--concurrency", "1") == 0
    assert _run(parallel, "--concurrency", "8") == 0
    # run_manifest 는 제외한다 — 거기에는 `concurrency: 1|8` 이 설정으로 기록되며, 그건 달라야 한다.
    measured = {
        name: value
        for name, value in normalized_artifact_bytes(serial).items()
        if name != "run_manifest.json"
    }
    assert measured == {
        name: value
        for name, value in normalized_artifact_bytes(parallel).items()
        if name != "run_manifest.json"
    }


def test_existing_out_dir_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    assert _run(out) == 2


def test_unknown_case_id_is_refused(tmp_path: Path) -> None:
    assert _run(tmp_path / "run", "--case-ids", "nope-001") == 2


def test_case_limit_narrows_the_run(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, "--case-limit", "3") == 0
    assert _results(out)["cellCount"] == 3


def test_report_header_carries_prompt_tier_fixture(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    results = _results(out)
    assert results["prompt"]["sha12"] in report
    assert "tier=fast" in report
    assert "intent-probe-anchors-b-v1" in report
    assert "이건 골든셋이 아니다" in report


def test_report_tables_escape_pipes_in_cell_ids(tmp_path: Path) -> None:
    # cellId 는 `발화|컨텍스트` 라 이스케이프하지 않으면 마크다운 표가 통째로 어긋난다.
    out = tmp_path / "run"
    assert _run(out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    table_rows = [line for line in report.splitlines() if line.startswith("| `")]
    assert table_rows
    for row in table_rows:
        assert row.count("|") - row.count("\\|") == 5  # 표 구분자 5개(4열)만 남아야 한다
    assert "cart-control-001\\|none" in report


def test_axis_definitions_travel_with_the_numbers(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    results = _results(out)
    assert results["axes"]["switchLegacy2"]["definition"]["notComparableWith"]
    assert "직접 비교 금지" in report


def test_candidate_prompt_changes_the_recorded_hash(tmp_path: Path) -> None:
    baseline, candidate = tmp_path / "base", tmp_path / "cand"
    prompt = tmp_path / "cand.txt"
    prompt.write_text("후보 프롬프트", encoding="utf-8")
    assert _run(baseline, "--case-limit", "2") == 0
    assert _run(candidate, "--case-limit", "2", "--prompt", str(prompt)) == 0
    assert _results(baseline)["prompt"]["sha12"] != _results(candidate)["prompt"]["sha12"]
    manifest = json.loads((candidate / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["hashes"]["systemPrompt"] == _results(candidate)["prompt"]["sha256"]


def test_dump_prompt_round_trips_byte_for_byte(tmp_path: Path) -> None:
    dumped = tmp_path / "system.txt"
    assert main(["--dump-prompt", str(dumped)]) == 0
    assert dumped.read_text(encoding="utf-8") == repo_system_prompt()
    baseline, round_trip = tmp_path / "base", tmp_path / "rt"
    assert _run(baseline, "--case-limit", "2") == 0
    assert _run(round_trip, "--case-limit", "2", "--prompt", str(dumped)) == 0
    assert _results(baseline)["prompt"]["sha12"] == _results(round_trip)["prompt"]["sha12"]


def test_changing_anchors_changes_the_result(tmp_path: Path) -> None:
    # 수용 기준의 실행 가능한 형태 — 되물음 상품 위치만 다른 두 정답지가 다른 표를 낸다.
    fixture_a, fixture_b = tmp_path / "a", tmp_path / "b"
    assert _run(fixture_a, "--fixture", "a") == 0
    assert _run(fixture_b, "--fixture", "b") == 0
    results_a, results_b = _results(fixture_a), _results(fixture_b)
    assert results_a["fixture"]["sha256"] != results_b["fixture"]["sha256"]
    assert results_a["fixture"]["reaskProductListPosition"] == 1
    assert results_b["fixture"]["reaskProductListPosition"] == 2
    assert (
        results_a["axes"]["optionAnswer"]["numerator"]
        != results_b["axes"]["optionAnswer"]["numerator"]
    )


def test_pacer_snapshot_is_recorded(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, "--rpm", "5") == 0
    pacer = _results(out)["pacer"]
    assert pacer["maxRpm"] == 5
    assert pacer["acquireCount"] == 53 * 2
    assert pacer["waitCount"] > 0


def test_unfillable_run_exits_4_and_lists_the_cells(tmp_path: Path, monkeypatch) -> None:
    from evals.intent_probe import cli as cli_module
    from evals.intent_probe.fakes import ScriptedDecomposeLLM

    monkeypatch.setattr(
        cli_module,
        "ScriptedDecomposeLLM",
        lambda anchors: ScriptedDecomposeLLM(anchors, always_fail=True),
    )
    out = tmp_path / "run"
    assert _run(out, "--case-limit", "2") == 4
    results = _results(out)
    assert len(results["unfilledCells"]) == 2
    assert results["unfilledCells"][0]["got"] == 0
    assert results["axes"]["mainIntent"]["unfilledSampleCount"] > 0
    assert (out / "failures.csv").read_text(encoding="utf-8").count("LLMError") == 12


def test_budget_exceeded_exits_3_with_partial_artifacts(tmp_path: Path, monkeypatch) -> None:
    # 예산 게이트는 dry-run 가짜가 예산을 쓰지 않아 평소 실행되지 않는 경로다 — 여기서만 밟힌다.
    from evals.intent_probe import cli as cli_module
    from evals.intent_probe.fakes import ScriptedDecomposeLLM
    from evals.model_eval.budget import BudgetExceeded

    class _BudgetBurstLLM(ScriptedDecomposeLLM):
        async def complete(self, **kwargs: object) -> str:
            if self._attempts >= 4:
                raise BudgetExceeded("maxCallsExceeded")
            return await super().complete(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "ScriptedDecomposeLLM", _BudgetBurstLLM)
    out = tmp_path / "run"
    assert main(["--out", str(out), "--dry-run", "--repeats", "2", "--concurrency", "1"]) == 3
    assert {path.name for path in out.iterdir()} == ARTIFACT_NAMES
    results = _results(out)
    assert results["cellCount"] < 53  # 중단 시점까지의 부분 결과만 기록된다


def test_probe_is_not_wired_into_ci(tmp_path: Path) -> None:
    # #260 §5: 실 LLM 비용·비결정론 때문에 CI 에서 돌리지 않는다.
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    for path in workflows.glob("*.y*ml"):
        assert "intent_probe" not in path.read_text(encoding="utf-8")
