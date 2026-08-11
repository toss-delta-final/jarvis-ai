"""프로브 CLI — 전부 `--dry-run`(가짜 LLM)이라 CI 에서 API 콜이 0이다 (#260)."""

from __future__ import annotations

import csv
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
    # [#285, I-25 §4.13 — 4단계] 수량 변경 6셀 추가로 89 → 95(§4-2, `test_intent_probe_fixtures.py`
    # 와 같은 총합. #440 이전엔 85였다).
    # [#440] 찜 해제 4셀 추가로 85 → 89(§4-2, `test_intent_probe_fixtures.py` 와 같은 총합).
    assert results["cellCount"] == 101
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
    assert "intent-probe-anchors-b-v8" in report
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
    # 셀 101 × N=2 (decompose) + 카테고리 15셀 × 2 (범위 해제 분류기) + 저정보량
    # 첫 무맥락 후보 2셀 × 2 (#463 과소지정 전용 분류기) = 236.
    # [#84] 분류기도 **페이서를 지난다** — 레이트 예산에 빠지면 실 런에서 429 가 난다.
    # [#300] screen 6셀은 분류기를 태우지 않는다(직전 카테고리가 없다) — 셀 수만 늘어난다.
    # 실제 판정은 LLM이 하지만, 비용은 저정보량 사전 게이트로 제한한다.
    assert pacer["acquireCount"] == 101 * 2 + 15 * 2 + 2 * 2
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


# ─────────── #84 2차 리뷰 F-5·G-1 ───────────


def test_manifest_records_both_prompt_hashes(tmp_path: Path) -> None:
    """[F-5] 표를 결정하는 프롬프트가 **둘**이므로 해시도 둘이어야 한다.

    분류기 문면만 바꾸고 같은 프로브를 돌리면 manifest 가 똑같아 보이는데 표는 달라진다 —
    manifest 를 둔 이유(#260: 어떤 프롬프트로 잰 표인지 특정)가 그 축에서만 무너진다.
    """
    from app.agents.buyer.recommendation.category_scope import _SYSTEM as SCOPE_SYSTEM
    from evals.intent_probe.manifest import category_scope_prompt_sha256

    out = tmp_path / "run"
    assert _run(out) == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    hashes = manifest["hashes"]
    assert hashes["systemPrompt"] != hashes["categoryScopePrompt"]
    assert hashes["categoryScopePrompt"] == category_scope_prompt_sha256()
    assert len(SCOPE_SYSTEM) > 0 and "categoryScope" in hashes["prompts"]
    # 리포트 헤더만 봐도 두 프롬프트가 구분돼야 한다(표는 복사돼 돌아다닌다).
    report = (out / "report.md").read_text(encoding="utf-8")
    assert f"scope={hashes['categoryScopePrompt'][:12]}" in report


def test_no_classifier_flag_reproduces_the_defect_arm(tmp_path: Path) -> None:
    """[G-1] `--no-classifier` 로 분류기를 끈 런이 재현 가능해야 한다.

    커밋된 기준선 README 가 `before1`(분류기 off) 열을 **#84 결함의 재현**으로 쓰는데, 그 팔을
    끄는 스위치가 없으면 재현 명령을 적을 수 없다. 앱 설정을 읽지 않고 **인자**로 고정한다 —
    환경으로 조건을 정하면 재현되지 않는다.
    """
    on, off = tmp_path / "on", tmp_path / "off"
    assert _run(on) == 0
    assert _run(off, "--no-classifier") == 0

    on_results, off_results = _results(on), _results(off)
    assert on_results["categoryScopeEnabled"] is True
    assert off_results["categoryScopeEnabled"] is False
    # 분류기를 끄면 해제 축이 무너진다 — 그것이 #84 결함의 모양이다.
    assert off_results["axes"]["categoryClear"]["numerator"] == 0
    assert on_results["axes"]["categoryClear"]["numerator"] > 0
    # 표본에도 그 사실이 남는다(전부 판정 없음).
    rows = list(csv.DictReader((off / "samples.csv").read_text(encoding="utf-8").splitlines()))
    assert {row["scopeFree"] for row in rows} == {""}


def test_disabled_classifier_arm_is_marked_in_the_artifacts(tmp_path: Path) -> None:
    """끈 런과 켠 런을 **나중에 구분할 수 있어야** 한다 — 안 남기면 표만 보고는 알 수 없다."""
    out = tmp_path / "off"
    assert _run(out, "--no-classifier") == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["intentProbe"]["categoryScopeClassifier"] == "off"
    # 관여하지 않은 문면의 해시를 남기면 "이 문면으로 쟀다"는 거짓 신호가 된다.
    assert manifest["hashes"]["categoryScopePrompt"] is None
    assert "categoryScope" not in manifest["hashes"]["prompts"]
    assert "scope=off" in (out / "report.md").read_text(encoding="utf-8")


def test_samples_csv_carries_the_raw_legs_for_recounting(tmp_path: Path) -> None:
    """[F-4] leg 원문 열이 있어야 판정 규칙을 바꿔도 **런을 다시 돌리지 않고** 재집계한다."""
    out = tmp_path / "run"
    assert _run(out) == 0
    rows = list(csv.DictReader((out / "samples.csv").read_text(encoding="utf-8").splitlines()))
    assert "categoryLegs" in rows[0]
    category_rows = [row for row in rows if row["group"] == "category_action"]
    assert category_rows and any(row["categoryLegs"] for row in category_rows)


# ─────────── [#300] screen 지시어 해소(#118 이관) ───────────


def test_samples_csv_carries_screen_resolution_columns(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    rows = list(csv.DictReader((out / "samples.csv").read_text(encoding="utf-8").splitlines()))
    assert {"resolvedProductId", "screenResolverFired", "screenResolutionReason"} <= set(rows[0])
    screen_rows = [row for row in rows if row["group"] == "screen"]
    assert screen_rows
    assert any(row["screenResolverFired"] == "True" for row in screen_rows)
    # 이름 매칭 셀(screen-005)은 해소기가 개입하지 않는다.
    named = [row for row in screen_rows if row["utteranceId"] == "screen-005"]
    assert named and all(row["screenResolverFired"] == "False" for row in named)


def test_report_exposes_screen_axes_and_diagnostics(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    results = _results(out)
    for axis_id in (
        "screenExactPick",
        "screenReask",
        "screenNoHallucination",
        "screenResolution",
    ):
        assert axis_id in results["axes"]
        assert f"`{axis_id}`" in report
    assert "screenPromptLayerHitCount" in results["diagnostics"]
    assert "screenResolverOverrideCount" in results["diagnostics"]
    assert "screenOutOfListConfirmCount" in results["diagnostics"]
    assert "화면 지시어" in report


def test_report_exposes_condition_only_axis(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    results = _results(out)
    assert "conditionOnlyNoCategoryQuery" in results["axes"]
    assert "`conditionOnlyNoCategoryQuery`" in report
    assert results["axes"]["conditionOnlyNoCategoryQuery"]["expectedDenominator"] == 10  # N=2


def test_condition_only_cells_do_not_call_the_category_scope_classifier(tmp_path: Path) -> None:
    """조건 전용 첫 턴 중 저정보량 표본만 #463 분류기를 함께 호출한다."""
    out = tmp_path / "run"
    assert _run(out, "--case-ids", "condition-only-001,condition-only-002") == 0
    pacer = _results(out)["pacer"]
    # decompose 2셀 × N=2 + 저정보량 1셀 × N=2 전용 분류기.
    assert pacer["acquireCount"] == 2 * 2 + 2


def test_screen_cells_do_not_call_the_category_scope_classifier(tmp_path: Path) -> None:
    """screen 컨텍스트는 직전 카테고리를 싣지 않는다 — 분류기가 호출되지 않는다(D-6 실측 근거)."""
    out = tmp_path / "run"
    assert _run(out, "--case-ids", "screen-001,screen-002") == 0
    pacer = _results(out)["pacer"]
    # decompose 2셀 × N=2 만 페이서를 지난다 — 분류기 호출이 없다.
    assert pacer["acquireCount"] == 2 * 2


def test_report_exposes_named_category_axis_and_diagnostics(tmp_path: Path) -> None:
    """[#443] 새 축·진단 카운터가 report.md·results.json 배관에 실제로 실리는지 — dry-run 이라
    수치 자체는 의미 없다(가짜 LLM), 배관 확인용이다."""
    out = tmp_path / "run"
    assert _run(out) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    results = _results(out)
    assert "namedCategoryHasLeg" in results["axes"]
    assert "`namedCategoryHasLeg`" in report
    assert results["axes"]["namedCategoryHasLeg"]["expectedDenominator"] == 12  # 6발화 × N=2
    assert "namedCategoryEmptyLegsCount" in results["diagnostics"]
    assert "namedCategoryCase3Count" in results["diagnostics"]


def test_named_category_cells_do_not_call_the_category_scope_classifier(tmp_path: Path) -> None:
    """[#443] 명시 카테고리 첫 턴은 #463 저정보량 호출을 열지 않는다."""
    out = tmp_path / "run"
    assert _run(out, "--case-ids", "named-category-001,named-category-002") == 0
    pacer = _results(out)["pacer"]
    assert pacer["acquireCount"] == 2 * 2
