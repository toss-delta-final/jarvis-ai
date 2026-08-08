"""#413 — personalization/scoring paired 정규화가 arm 하위 매니페스트까지 dirty·commitSha 축을
걷어내는지 고정한다. `main()` 을 돌리면 느려서(evaluate 파이프라인 전체) 합성 산출물 트리를
tmp_path 에 직접 만들어 정규화 함수만 겨냥한다."""

from __future__ import annotations

import json
from pathlib import Path

from evals.personalization.cli import normalize_live_artifacts
from evals.personalization.cli import normalize_paired_artifacts as normalize_personalization
from evals.scoring.cli import normalize_paired_artifacts as normalize_scoring


def _manifest_text(*, run_id: str, commit_sha: str, dirty: bool, seed: int = 1) -> str:
    return json.dumps(
        {
            "run": {"runId": run_id, "timestamp": "2026-08-03T00:00:00Z", "command": "c"},
            "commitSha": commit_sha,
            "dirty": dirty,
            "seed": seed,
        }
    )


def _build_personalization_tree(
    root: Path, *, run_id: str, commit_sha: str, dirty: bool, results_byte: bytes = b"x"
) -> None:
    """arm/weight 하위 + 최상위 모두 같은 dirty·commitSha 를 찍는 personalization Tier D 산출물
    트리(더미)를 만든다."""
    for arm in ("clean", "member_no_profile"):
        weight_dir = root / arm / "weight-0p15"
        weight_dir.mkdir(parents=True)
        (weight_dir / "run_manifest.json").write_text(
            _manifest_text(run_id=f"{run_id}-{arm}", commit_sha=commit_sha, dirty=dirty),
            encoding="utf-8",
        )
        (weight_dir / "results.json").write_bytes(results_byte)
    (root / "comparison.json").write_text("{}", encoding="utf-8")
    (root / "comparison.md").write_text("# comparison\n", encoding="utf-8")
    (root / "overreach.json").write_text("{}", encoding="utf-8")
    (root / "run_manifest.json").write_text(
        _manifest_text(run_id=run_id, commit_sha=commit_sha, dirty=dirty), encoding="utf-8"
    )


def _build_scoring_tree(
    root: Path, *, run_id: str, commit_sha: str, dirty: bool, results_byte: bytes = b"x"
) -> None:
    """arm 하위 + 최상위 모두 같은 dirty·commitSha 를 찍는 scoring paired 산출물 트리(더미)를
    만든다."""
    for arm in ("passthrough", "scoring"):
        arm_dir = root / arm
        arm_dir.mkdir(parents=True)
        (arm_dir / "run_manifest.json").write_text(
            _manifest_text(run_id=f"{run_id}-{arm}", commit_sha=commit_sha, dirty=dirty),
            encoding="utf-8",
        )
        (arm_dir / "results.json").write_bytes(results_byte)
    (root / "comparison.json").write_text("{}", encoding="utf-8")
    (root / "comparison.md").write_text("# comparison\n", encoding="utf-8")
    (root / "scores_scoring.json").write_text("[]", encoding="utf-8")
    (root / "run_manifest.json").write_text(
        _manifest_text(run_id=run_id, commit_sha=commit_sha, dirty=dirty), encoding="utf-8"
    )


def test_personalization_normalize_paired_artifacts_ignores_dirty_flip_and_commit_sha(
    tmp_path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _build_personalization_tree(first, run_id="one", commit_sha="a" * 40, dirty=False)
    _build_personalization_tree(second, run_id="two", commit_sha="b" * 40, dirty=True)

    assert normalize_personalization(first) == normalize_personalization(second)


def test_personalization_normalize_paired_artifacts_still_catches_arm_byte_changes(
    tmp_path,
) -> None:
    """음성 케이스 — arm 하위 산출물 바이트가 다르면 정규화 결과도 달라야 한다."""
    first, second = tmp_path / "first", tmp_path / "second"
    _build_personalization_tree(
        first, run_id="one", commit_sha="a" * 40, dirty=False, results_byte=b"x"
    )
    _build_personalization_tree(
        second, run_id="one", commit_sha="a" * 40, dirty=False, results_byte=b"y"
    )

    assert normalize_personalization(first) != normalize_personalization(second)


def test_scoring_normalize_paired_artifacts_ignores_dirty_flip_and_commit_sha(tmp_path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _build_scoring_tree(first, run_id="one", commit_sha="a" * 40, dirty=False)
    _build_scoring_tree(second, run_id="two", commit_sha="b" * 40, dirty=True)

    assert normalize_scoring(first) == normalize_scoring(second)


def test_scoring_normalize_paired_artifacts_still_catches_arm_byte_changes(tmp_path) -> None:
    """음성 케이스 — arm 하위 산출물 바이트가 다르면 정규화 결과도 달라야 한다."""
    first, second = tmp_path / "first", tmp_path / "second"
    _build_scoring_tree(first, run_id="one", commit_sha="a" * 40, dirty=False, results_byte=b"x")
    _build_scoring_tree(second, run_id="one", commit_sha="a" * 40, dirty=False, results_byte=b"y")

    assert normalize_scoring(first) != normalize_scoring(second)


def _live_manifest_text(*, run_id: str, commit_sha: str, dirty: bool) -> str:
    return json.dumps(
        {
            "run": {"runId": run_id, "timestamp": "2026-08-03T00:00:00Z", "command": "c"},
            "commitSha": commit_sha,
            "dirty": dirty,
            "seed": 1,
        }
    )


def _live_results_text(
    *, case_id: str, ranked_product_ids: list[int], latency_ms: float, correlation_id: str
) -> str:
    return json.dumps(
        {
            "datasetHash": "abc",
            "caseResults": [
                {
                    "caseId": case_id,
                    "rankedProductIds": ranked_product_ids,
                    "latencyMs": latency_ms,
                    "providerCalls": [
                        {
                            "correlationId": correlation_id,
                            "latencyMs": latency_ms,
                            "inputTokens": 10,
                        }
                    ],
                }
            ],
        }
    )


def _build_live_tree(
    root: Path,
    *,
    run_id: str,
    commit_sha: str,
    dirty: bool,
    case_id: str = "buy-0001",
    ranked_product_ids: tuple[int, ...] = (1, 2),
    latency_ms: float = 1.0,
    correlation_id: str = "corr-a",
) -> None:
    """arm 하위 중첩 디렉터리를 갖는 Tier L(`normalize_live_artifacts`) 산출물 트리(더미)를
    만든다."""
    for arm in ("guest", "clean_both"):
        arm_dir = root / arm
        arm_dir.mkdir(parents=True)
        (arm_dir / "run_manifest.json").write_text(
            _live_manifest_text(run_id=f"{run_id}-{arm}", commit_sha=commit_sha, dirty=dirty),
            encoding="utf-8",
        )
        (arm_dir / "results.json").write_text(
            _live_results_text(
                case_id=case_id,
                ranked_product_ids=list(ranked_product_ids),
                latency_ms=latency_ms,
                correlation_id=correlation_id,
            ),
            encoding="utf-8",
        )


def test_live_normalize_artifacts_ignores_run_instance_and_wall_clock_axes(tmp_path) -> None:
    """양성 — run·commitSha·dirty(실행 인스턴스·워킹트리 상태 축)와 caseResults[].latencyMs·
    providerCalls[].correlationId/latencyMs(실행별 벽시계 축)가 달라도 정규화 결과는 같다."""
    first, second = tmp_path / "first", tmp_path / "second"
    _build_live_tree(
        first,
        run_id="one",
        commit_sha="a" * 40,
        dirty=False,
        latency_ms=1.0,
        correlation_id="corr-a",
    )
    _build_live_tree(
        second,
        run_id="two",
        commit_sha="b" * 40,
        dirty=True,
        latency_ms=99.0,
        correlation_id="corr-b",
    )

    assert normalize_live_artifacts(first) == normalize_live_artifacts(second)


def test_live_normalize_artifacts_still_catches_ranking_changes(tmp_path) -> None:
    """음성 케이스 — caseId·랭킹 결과 같은 실질 값이 다르면 정규화 결과도 달라야 한다."""
    first, second = tmp_path / "first", tmp_path / "second"
    _build_live_tree(first, run_id="one", commit_sha="a" * 40, dirty=False)
    _build_live_tree(
        second, run_id="one", commit_sha="a" * 40, dirty=False, ranked_product_ids=(9, 8)
    )

    assert normalize_live_artifacts(first) != normalize_live_artifacts(second)
