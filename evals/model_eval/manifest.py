"""기존 metric manifest에 실제 모델 평가 재현 필드를 확장한다."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from evals.metrics.run_manifest import build_run_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_EVAL_ROOT = Path(__file__).resolve().parent
GRAPH_PATH = REPO_ROOT / "app" / "agents" / "buyer" / "recommendation" / "graph.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_model_eval_manifest(
    *,
    command: str,
    seed: int,
    model_config: dict[str, Any],
    repeats: int,
    declared_repeats: int,
    split: str,
    case_order: str,
    config_version: str,
    budget: dict[str, Any],
    case_ids: list[str],
    eval_config_path: Path,
    pricing_path: Path,
) -> dict[str, Any]:
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["modelEval"] = {
        "modelConfig": model_config,
        "declaredRepeats": declared_repeats,
        "executedRepeats": repeats,
        "split": split,
        "caseOrder": case_order,
        "configVersion": config_version,
        "budget": budget,
        "caseIds": case_ids,
    }
    hashes = manifest.setdefault("hashes", {})
    hashes["evalConfig"] = _sha256(eval_config_path)
    hashes["pricingManifest"] = _sha256(pricing_path)
    hashes["graph"] = _sha256(GRAPH_PATH)
    hashes["modelEvalModules"] = {
        path.name: _sha256(path) for path in sorted(MODEL_EVAL_ROOT.glob("*.py"))
    }
    return manifest
