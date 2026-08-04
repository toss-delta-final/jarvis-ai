"""run manifest — '무엇을 잰 표인지'를 산출물 안에 못 박는다."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from evals.intent_probe.client import PromptIdentity
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_intent_probe_manifest(
    *,
    command: str,
    seed: int,
    prompt: PromptIdentity,
    tier: str,
    model_config: dict[str, Any],
    n: int,
    attempt_multiplier: int,
    concurrency: int,
    anchor_path: Path,
    fixture_version: str,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    cell_ids: list[str],
    axis_definitions: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """`build_run_manifest` 위에 이 프로브 고유의 지문을 얹는다.

    `hashes.prompts.decompose` 는 **파일 전체** 해시라 무관한 편집에도 바뀐다.
    `hashes.systemPrompt` 는 **실제로 provider 에 보낸 텍스트**의 해시다 — #260 이 요구하는 쪽은
    후자이며, 둘 다 남겨 사후에 구분할 수 있게 한다.
    """
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["intentProbe"] = {
        "prompt": prompt.as_dict(),
        "tier": tier,
        "modelConfig": model_config,
        "n": n,
        "attemptMultiplier": attempt_multiplier,
        "concurrency": concurrency,
        "dryRun": dry_run,
        "fixtureName": anchor_path.name,
        "fixtureVersion": fixture_version,
        "pacer": pacer,
        "budget": budget,
        "cellIds": cell_ids,
        "axisDefinitions": axis_definitions,
        "seedScope": "셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.",
        "notGoldenset": "추천 품질(nDCG·MRR)이 아니라 intent 라우팅 분포다. evals/goldenset 과 "
        "숫자를 섞지 말 것.",
        "singleRunNotAVerdict": "단일 실행은 채택 판정이 아니다 — 축당 ±2 흔들린다. "
        "독립 2~3회 분포로 판정한다 (#240 §6).",
    }
    hashes = manifest["hashes"]
    assert isinstance(hashes, dict)
    hashes["anchorFixture"] = _sha256(anchor_path)
    hashes["systemPrompt"] = prompt.sha256
    hashes["intentProbeModules"] = {
        path.name: _sha256(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest
