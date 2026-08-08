"""CLI 배선 테스트 — #462 (A-6). `--dry-run` 만 쓰므로 API/pg 콜 0.

CLI 는 배선이 가장 많이 꼬이는 곳이다(시임·스토어 리셋·산출물 쓰기·종료 코드) — 라운드 1 에는
`cli.main` 을 실행하는 테스트가 0 개였다. `normalized_artifact_bytes` 도 여기서 처음 실사용된다
(결정론 대조용으로 만들었으나 쓰는 곳이 없었다).

`cli.main` 은 내부에서 `asyncio.run(...)` 을 부르므로 이 파일의 테스트는 **동기 함수**여야 한다 —
`async def test_...` 안에서 부르면 이미 실행 중인 이벤트 루프 위에서 `asyncio.run` 을 또 불러
`RuntimeError` 가 난다.
"""

from __future__ import annotations

from pathlib import Path

from evals.model_eval.budget import BudgetExceeded
from evals.taste_probe import cli
from evals.taste_probe.report import normalized_artifact_bytes

EXPECTED_ARTIFACTS = {
    "results.json",
    "run_manifest.json",
    "report.md",
    "samples.csv",
    "triples.csv",
    "confusion.csv",
    "predicate_confusion.csv",
    "dropped.csv",
}


def test_dry_run_exits_zero_and_writes_all_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "run1"
    exit_code = cli.main(["--out", str(out), "--dry-run", "--n", "1"])
    assert exit_code == cli.EXIT_OK
    assert {path.name for path in out.iterdir()} == EXPECTED_ARTIFACTS


def test_dry_run_is_deterministic_across_identical_invocations(tmp_path: Path) -> None:
    """같은 인자로 두 번 돌린 두 디렉터리의 정규화 산출물이 바이트 단위로 같아야 한다 — 가짜 LLM·
    가짜 카탈로그는 결정론이고, 시각·runId·latency 는 `normalized_artifact_bytes` 가 걷어낸다."""
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    assert cli.main(["--out", str(out1), "--dry-run", "--n", "1"]) == cli.EXIT_OK
    assert cli.main(["--out", str(out2), "--dry-run", "--n", "1"]) == cli.EXIT_OK
    assert normalized_artifact_bytes(out1) == normalized_artifact_bytes(out2)


def test_existing_out_dir_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run1"
    out.mkdir()
    assert cli.main(["--out", str(out), "--dry-run"]) == cli.EXIT_REJECTED


def test_non_positive_n_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run1"
    assert cli.main(["--out", str(out), "--dry-run", "--n", "0"]) == cli.EXIT_REJECTED
    assert not out.exists()


class _BudgetExhaustedLLM:
    """[A-1] `--dry-run` 경로는 원래 budget 을 전혀 안 거치므로(가짜 LLM 이 `reserve()` 를 안
    부른다), CLI 레벨에서 예산 소진을 재현하려면 `ScriptedDeltaLLM` 자체를 갈아끼워야 한다."""

    async def complete(self, **kwargs: object) -> str:
        raise BudgetExceeded("scripted budget exhausted")

    def stream(self, **kwargs: object) -> None:
        raise NotImplementedError("taste_probe 는 stream 을 쓰지 않는다")


def test_budget_exceeded_returns_exit_code_3(tmp_path: Path, monkeypatch) -> None:
    """[A-1] 종전에는 `runner.py` 가 `BudgetExceeded` 를 표본 실패로 삼켜 `cli.main` 의
    `except BudgetExceeded → EXIT_BUDGET` 경로가 도달 불가능한 죽은 코드였다 — 지금은 실제로 3을
    반환하고, 빈 산출물이라도 기록해야 한다."""
    monkeypatch.setattr(cli, "ScriptedDeltaLLM", lambda *args, **kwargs: _BudgetExhaustedLLM())
    out = tmp_path / "run1"
    exit_code = cli.main(["--out", str(out), "--dry-run", "--n", "1"])
    assert exit_code == cli.EXIT_BUDGET
    assert out.exists()
    assert (out / "results.json").exists()
