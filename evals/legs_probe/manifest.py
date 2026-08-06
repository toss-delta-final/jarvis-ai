"""run manifest — '무엇을 잰 표인지'를 산출물 안에 못 박는다 (intent_probe/manifest.py 와 같은 규약)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from evals.intent_probe.client import PromptIdentity
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_legs_probe_manifest(
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
    category_fanout_max: int,
    repurchase_max: int,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    cell_ids: list[str],
    axis_definitions: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """`build_run_manifest` 위에 이 프로브 고유의 지문을 얹는다.

    `hashes.systemPrompt` 는 **실제로 provider 에 보낸 텍스트**의 해시다 — decompose 모듈 파일
    전체 해시(`hashes.legsProbeModules` 가 아니라 intent_probe 와 마찬가지로 프로덕션 소스 자체는
    건드리지 않으므로 여기서는 프롬프트 문면 해시만 남긴다)와 구분해 사후에 무엇이 바뀌었는지
    가른다.
    """
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["legsProbe"] = {
        "prompt": prompt.as_dict(),
        "tier": tier,
        "modelConfig": model_config,
        "n": n,
        "attemptMultiplier": attempt_multiplier,
        "concurrency": concurrency,
        "categoryFanoutMax": category_fanout_max,
        "repurchaseMax": repurchase_max,
        "dryRun": dry_run,
        "fixtureName": anchor_path.name,
        "fixtureVersion": fixture_version,
        "pacer": pacer,
        "budget": budget,
        "cellIds": cell_ids,
        "axisDefinitions": axis_definitions,
        "seedScope": "셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.",
        "measurementScope": "decompose 단계만 잰다 — needs_expansion(#217)은 부르지 않는다. "
        "단일 턴만 잰다(컨텍스트 행렬 없음).",
        "notGoldenset": "이건 골든셋이 아니다 — 추천 품질(nDCG·MRR)이 아니라 decompose 단계의 "
        "case·legs 산출 분포다. evals/goldenset·evals/intent_probe 와 숫자를 섞지 말 것.",
        "singleRunNotAVerdict": "단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.",
    }
    hashes = manifest["hashes"]
    assert isinstance(hashes, dict)
    hashes["anchorFixture"] = _sha256(anchor_path)
    hashes["systemPrompt"] = prompt.sha256
    hashes["legsProbeModules"] = {
        path.name: _sha256(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest
