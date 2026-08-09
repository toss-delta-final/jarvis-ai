"""#462/#413 — taste_probe 산출물 정규화가 정본 `VOLATILE_MANIFEST_KEYS` 를 수렴해 쓰는지 고정.

`commitSha`/`dirty` 는 `build_run_manifest` 가 실행 시점 라이브 git 상태에서 읽어 최상위에
두므로, 두 실행 사이에 이 값만 달라져도(리포 편집·커밋) 정규화 결과가 갈리면 안 된다(#413).
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.taste_probe.report import VOLATILE_JSON_KEYS, normalized_artifact_bytes


def _write_manifest(path: Path, *, run_id: str, commit_sha: str, dirty: bool) -> None:
    (path / "run_manifest.json").write_text(
        json.dumps(
            {
                "run": {"runId": run_id, "timestamp": "2026-08-08T00:00:00Z", "command": "c"},
                "commitSha": commit_sha,
                "dirty": dirty,
                "seed": 20260808,
            }
        ),
        encoding="utf-8",
    )


def test_normalized_artifact_bytes_ignores_dirty_flip_and_commit_sha_change(tmp_path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_manifest(first, run_id="one", commit_sha="a" * 40, dirty=False)
    _write_manifest(second, run_id="two", commit_sha="b" * 40, dirty=True)

    assert normalized_artifact_bytes(first) == normalized_artifact_bytes(second)


def test_normalized_artifact_bytes_still_catches_seed_change(tmp_path) -> None:
    baseline, changed = tmp_path / "baseline", tmp_path / "changed"
    baseline.mkdir()
    changed.mkdir()
    _write_manifest(baseline, run_id="one", commit_sha="a" * 40, dirty=False)
    (changed / "run_manifest.json").write_text(
        json.dumps(
            {
                "run": {"runId": "two", "timestamp": "2026-08-08T00:00:00Z", "command": "c"},
                "commitSha": "a" * 40,
                "dirty": False,
                "seed": 1,
            }
        ),
        encoding="utf-8",
    )

    assert normalized_artifact_bytes(baseline) != normalized_artifact_bytes(changed)


def test_volatile_json_keys_subsumes_canonical_manifest_keys() -> None:
    from evals.metrics.run_manifest import VOLATILE_MANIFEST_KEYS

    assert VOLATILE_MANIFEST_KEYS <= VOLATILE_JSON_KEYS
