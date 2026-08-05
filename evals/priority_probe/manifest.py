"""run manifest — 어떤 프롬프트로 잰 표인지 못 박는다 (#281 TASK 3 §5).

표를 결정하는 프롬프트가 **둘**이다 — 분류기 팔은 `need_priority._SYSTEM`(고정), 인라인 팔은
후보 `_SYSTEM`(파일로 갈아끼운다). 둘 다 해시를 남긴다 — 하나만 남기면 어느 문면으로 쟀는지
manifest 만 보고 구분할 수 없다(`evals/intent_probe/manifest.py` 와 같은 이유).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation import need_priority as need_priority_module
from evals.intent_probe.client import PromptIdentity
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent
NEED_PRIORITY_MODULE = Path(need_priority_module.__file__)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classifier_prompt_sha256() -> str:
    """분류기 `_SYSTEM` 문면의 sha256 — 인라인 후보 프롬프트와 같은 방식(문면 바이트)."""
    return hashlib.sha256(need_priority_module._SYSTEM.encode("utf-8")).hexdigest()


def build_priority_probe_manifest(
    *,
    command: str,
    seed: int,
    arm: str,
    prompt: PromptIdentity,
    tier: str,
    model_config: dict[str, Any],
    n: int,
    attempt_multiplier: int,
    concurrency: int,
    fixture_path: Path,
    fixture_version: str,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    cell_ids: list[str],
    metric_definitions: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["priorityProbe"] = {
        "arm": arm,
        "prompt": prompt.as_dict(),
        "tier": tier,
        "modelConfig": model_config,
        "n": n,
        "attemptMultiplier": attempt_multiplier,
        "concurrency": concurrency,
        "dryRun": dry_run,
        "fixtureName": fixture_path.name,
        "fixtureVersion": fixture_version,
        "pacer": pacer,
        "budget": budget,
        "cellIds": cell_ids,
        "metricDefinitions": metric_definitions,
        "seedScope": "셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.",
        "singleRunNotAVerdict": "단일 실행은 채택 판정이 아니다 — 독립 2회 이상 분포로 판정한다.",
        "specDeviationNotice": (
            "전용 분류기를 채택하면 SPEC-RECOMMEND-001 결정 14-H/REQ-REC-004(LLM 추가 호출 없음)와 "
            "AC-REC-37(별도 분류 LLM 호출 금지)에서 이탈한다 — #84(PR #307)가 category_scope 로 "
            "같은 이탈을 선례로 남겼다. 정본 반영은 후속 과제다."
        ),
    }
    hashes = manifest["hashes"]
    assert isinstance(hashes, dict)
    hashes["priorityFixture"] = _sha256(fixture_path)
    # 인라인 팔이 실제로 provider 에 보낸 텍스트(후보 파일이면 그 파일, 아니면 리포의 _SYSTEM).
    hashes["inlineSystemPrompt"] = prompt.sha256
    # 분류기 팔은 프롬프트 교체가 없다 — 배포 문면 그대로다.
    hashes["classifierSystemPrompt"] = classifier_prompt_sha256()
    prompts = hashes.get("prompts")
    if isinstance(prompts, dict):
        prompts["needPriority"] = _sha256(NEED_PRIORITY_MODULE)
    hashes["priorityProbeModules"] = {
        path.name: _sha256(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest
