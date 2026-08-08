"""평가 실행 재현에 필요한 코드·데이터·환경 지문 수집."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]

# 산출물 결정론 비교에서 제외하는 "실행 인스턴스 · 워킹트리 상태" 축.
# run: runId/timestamp/command · commitSha/dirty: 실행 시점의 라이브 git 상태로,
# 평가 입력이 아니라 리포 상태를 찍는 값이라 두 실행 사이 편집·커밋만으로 뒤집힌다(#413).
VOLATILE_MANIFEST_KEYS = frozenset({"run", "commitSha", "dirty"})


def strip_volatile_manifest_keys(manifest: dict[str, Any]) -> dict[str, Any]:
    """매니페스트에서 실행 인스턴스·워킹트리 상태 축을 걷어낸 새 dict를 돌려준다."""
    return {key: value for key, value in manifest.items() if key not in VOLATILE_MANIFEST_KEYS}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_run_manifest(*, command: str, seed: int) -> dict[str, object]:
    """현재 checkout과 평가 입력의 결정론적 manifest를 만든다."""
    fixtures = REPO_ROOT / "evals" / "goldenset" / "fixtures"
    prompts = {
        "decompose": REPO_ROOT / "app" / "agents" / "buyer" / "recommendation" / "decompose.py",
        "rerank": REPO_ROOT / "app" / "agents" / "buyer" / "recommendation" / "rerank.py",
    }
    return {
        "run": {
            "runId": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
        },
        "commitSha": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "pythonVersion": sys.version.split()[0],
        "platform": platform.platform(),
        "image": os.getenv("JARVIS_EVAL_IMAGE"),
        "seed": seed,
        "hashes": {
            "uvLock": _sha256(REPO_ROOT / "uv.lock"),
            "datasetManifest": _sha256(REPO_ROOT / "evals" / "goldenset" / "manifest.json"),
            "fixtures": {
                name: _sha256(fixtures / name)
                for name in (
                    "catalog_snapshot.json",
                    "purchase_history.json",
                    "search_responses.json",
                )
            },
            "prompts": {name: _sha256(path) for name, path in prompts.items()},
            "config": _sha256(REPO_ROOT / "app" / "core" / "config.py"),
        },
    }
