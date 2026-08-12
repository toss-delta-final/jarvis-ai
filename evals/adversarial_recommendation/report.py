"""Adversarial recommendation run 산출물 writer."""

from __future__ import annotations

import hashlib
import importlib
import json
import ctypes
import errno
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.adversarial_recommendation.generator import CASES_PATH, MANIFEST_PATH
from evals.adversarial_recommendation.runner import RunMode
from evals.adversarial_recommendation.schema import EvalCase
from app.agents.buyer.recommendation.rerank_grounding import GroundingArm

_ROOT = Path(__file__).resolve().parents[2]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git_identity() -> dict[str, Any]:
    def _run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    try:
        status = _run("status", "--porcelain=v1")
        return {
            "commit": _run("rev-parse", "HEAD"),
            "branch": _run("branch", "--show-current"),
            "dirty": bool(status),
            "statusSha256": _sha256_bytes(status.encode()),
            "worktreeDiffSha256": _sha256_bytes(
                _run("diff", "--binary", "HEAD", "--", ".").encode()
            ),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unavailable", "branch": "unavailable", "dirty": None}


def _source_hashes() -> dict[str, str]:
    decompose_module = importlib.import_module("app.agents.buyer.recommendation.decompose")
    rerank_module = importlib.import_module("app.agents.buyer.recommendation.rerank")
    return {
        "runner": _file_sha256(Path(__file__).with_name("runner.py")),
        "scoring": _file_sha256(Path(__file__).with_name("scoring.py")),
        "buyerGraph": _file_sha256(_ROOT / "app/agents/buyer/recommendation/graph.py"),
        "decompose": _file_sha256(_ROOT / "app/agents/buyer/recommendation/decompose.py"),
        "rerank": _file_sha256(_ROOT / "app/agents/buyer/recommendation/rerank.py"),
        "config": _file_sha256(_ROOT / "app/core/config.py"),
        "searchService": _file_sha256(_ROOT / "app/services/search_service.py"),
        "springClient": _file_sha256(_ROOT / "app/services/spring_client.py"),
        "springSchemas": _file_sha256(_ROOT / "app/schemas/spring.py"),
        "chatSchemas": _file_sha256(_ROOT / "app/schemas/chat.py"),
        "recordingLlm": _file_sha256(_ROOT / "evals/model_eval/recording.py"),
        "decomposePrompt": _sha256_bytes(decompose_module._SYSTEM.encode()),
        "rerankPrompt": _sha256_bytes(rerank_module._SYSTEM.encode()),
        "rerankStructuredPrompt": _sha256_bytes(
            rerank_module._SYSTEM_STRUCTURED_GROUNDING.encode()
        ),
    }


def _redact_settings(value: Any, key: str = "") -> Any:
    """재현에 필요한 Settings를 보존하되 credential 값은 manifest에 쓰지 않는다."""
    normalized_key = key.casefold().replace("_", "").replace("-", "")
    if normalized_key.endswith(
        ("apikey", "token", "secret", "password", "dburl", "dsn")
    ) or normalized_key in {"databaseurl", "redisurl"}:
        return "<redacted>" if value else value
    if isinstance(value, dict):
        return {
            str(item_key): _redact_settings(item, str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_settings(item) for item in value]
    return value


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Linux renameat2로 directory를 원자 publish하되 기존 경로를 절대 교체하지 않는다."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "atomic no-replace publish requires renameat2")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), destination)


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for result in results:
        by_category[result["category"]][result["verdict"]] += 1
    return {
        "caseCount": len(results),
        "uniqueCaseCount": len({result["caseId"] for result in results}),
        "arms": list(dict.fromkeys(result.get("groundingArm", "current") for result in results)),
        "armVerdictCounts": {
            arm: dict(
                sorted(
                    Counter(
                        result["verdict"]
                        for result in results
                        if result.get("groundingArm", "current") == arm
                    ).items()
                )
            )
            for arm in dict.fromkeys(result.get("groundingArm", "current") for result in results)
        },
        "verdictCounts": dict(sorted(Counter(result["verdict"] for result in results).items())),
        "automaticVerdictCounts": dict(
            sorted(Counter(result["automaticVerdict"] for result in results).items())
        ),
        "categoryVerdictCounts": {
            category: dict(sorted(counts.items()))
            for category, counts in sorted(by_category.items())
        },
        "hardFailureCount": sum(bool(result["execution"].get("hardFailure")) for result in results),
        "reviewCaseIds": [result["caseId"] for result in results if result["verdict"] == "review"],
        "failedCaseIds": [result["caseId"] for result in results if result["verdict"] == "fail"],
        "errorCaseIds": [result["caseId"] for result in results if result["verdict"] == "error"],
    }


def _report(summary: dict[str, Any], mode: RunMode) -> str:
    lines = [
        "# Adversarial recommendation evaluation report",
        "",
        f"- mode: `{mode}`",
        f"- cases: {summary['caseCount']}",
        f"- verdicts: `{json.dumps(summary['verdictCounts'], ensure_ascii=False, sort_keys=True)}`",
        f"- automatic: `{json.dumps(summary['automaticVerdictCounts'], ensure_ascii=False, sort_keys=True)}`",
        f"- hard failures: {summary['hardFailureCount']}",
        "",
    ]
    if mode == "scripted":
        lines.extend(
            [
                "> `scripted`는 production buyer 코드 배선과 결정론 규칙을 검사합니다. ",
                "> 실제 LLM의 prompt-injection/근거 품질 통과를 의미하지 않습니다.",
                "",
            ]
        )
    lines.extend(["## Grounding arms", ""])
    for arm, counts in summary["armVerdictCounts"].items():
        lines.append(f"- `{arm}`: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
    lines.append("")
    lines.extend(["## Category verdicts", ""])
    for category, counts in summary["categoryVerdictCounts"].items():
        lines.append(f"- `{category}`: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
    lines.extend(["", "## Follow-up", ""])
    lines.append(f"- review: {', '.join(summary['reviewCaseIds']) or '(none)'}")
    lines.append(f"- fail: {', '.join(summary['failedCaseIds']) or '(none)'}")
    lines.append(f"- error: {', '.join(summary['errorCaseIds']) or '(none)'}")
    return "\n".join(lines) + "\n"


def write_run_artifacts(
    out_dir: Path,
    *,
    cases: list[EvalCase],
    results: list[dict[str, Any]],
    mode: RunMode,
    model_config: dict[str, Any],
    command: list[str],
    effective_settings: dict[str, Any],
    arms: tuple[GroundingArm, ...] = ("current",),
) -> dict[str, Any]:
    """임시 sibling에서 완성한 네 산출물을 새 output directory로 원자 publish한다."""
    if out_dir.exists():
        raise FileExistsError(f"output directory already exists: {out_dir}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=out_dir.parent))
    summary = build_summary(results)
    dataset_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    redacted_settings = _redact_settings(effective_settings)
    run_manifest = {
        "runAt": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "arms": list(arms),
        "datasetName": dataset_manifest["datasetName"],
        "datasetVersion": dataset_manifest["datasetVersion"],
        "datasetSha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        "caseIds": [case.case_id for case in cases],
        "modelConfig": model_config,
        "command": command,
        "git": _git_identity(),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "uvLockSha256": _file_sha256(_ROOT / "uv.lock"),
        "sourceHashes": _source_hashes(),
        "effectiveSettings": redacted_settings,
        "effectiveSettingsSha256": _sha256_bytes(
            json.dumps(redacted_settings, ensure_ascii=False, sort_keys=True).encode()
        ),
    }
    results_bytes = "".join(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n" for result in results
    )
    try:
        (work_dir / "results.jsonl").write_text(results_bytes, encoding="utf-8")
        (work_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (work_dir / "run_manifest.json").write_text(
            json.dumps(run_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (work_dir / "report.md").write_text(_report(summary, mode), encoding="utf-8")
        _rename_no_replace(work_dir, out_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return summary
