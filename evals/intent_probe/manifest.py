"""run manifest — '무엇을 잰 표인지'를 산출물 안에 못 박는다."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation import category_scope as category_scope_module
from app.agents.buyer.recommendation import underspecified_classifier as underspecified_module
from evals.intent_probe.client import CATEGORY_SCOPE_SYSTEM, PromptIdentity
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent
CATEGORY_SCOPE_MODULE = Path(category_scope_module.__file__)
UNDERSPECIFIED_MODULE = Path(underspecified_module.__file__)


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
    classifier_enabled: bool = True,
    underspecified_classifier_enabled: bool = True,
) -> dict[str, Any]:
    """`build_run_manifest` 위에 이 프로브 고유의 지문을 얹는다.

    `hashes.prompts.decompose` 는 **파일 전체** 해시라 무관한 편집에도 바뀐다.
    `hashes.systemPrompt` 는 **실제로 provider 에 보낸 텍스트**의 해시다 — #260 이 요구하는 쪽은
    후자이며, 둘 다 남겨 사후에 구분할 수 있게 한다.

    [#84·2차 리뷰 F-5] 표를 결정하는 프롬프트가 이제 **둘**이다(decompose `_SYSTEM` + 카테고리
    범위 해제 분류기 `_SYSTEM`). 분류기 문면만 바꾸고 같은 프로브를 돌리면 manifest 가 똑같아
    보이는데 표는 달라지므로, 분류기도 **같은 방식으로** 두 해시를 남긴다:
    `hashes.categoryScopePrompt`(문면) · `hashes.prompts.categoryScope`(모듈 파일 전체).
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
        # [#84·G-1] 분류기 팔의 on/off — 이 값이 없으면 같은 프롬프트 해시의 두 표를 구분할 수 없다.
        "categoryScopeClassifier": "on" if classifier_enabled else "off",
        "underspecifiedClassifier": "on" if underspecified_classifier_enabled else "off",
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
    # 분류기를 끈 런은 그 문면이 표에 관여하지 않았으므로 해시를 남기지 않는다(None) — 남기면
    # "이 문면으로 쟀다"는 거짓 신호가 된다.
    hashes["categoryScopePrompt"] = category_scope_prompt_sha256() if classifier_enabled else None
    hashes["underspecifiedPrompt"] = (
        underspecified_prompt_sha256() if underspecified_classifier_enabled else None
    )
    prompts = hashes.get("prompts")
    if isinstance(prompts, dict) and classifier_enabled:
        prompts["categoryScope"] = _sha256(CATEGORY_SCOPE_MODULE)
    if isinstance(prompts, dict) and underspecified_classifier_enabled:
        prompts["underspecified"] = _sha256(UNDERSPECIFIED_MODULE)
    hashes["intentProbeModules"] = {
        path.name: _sha256(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest


def category_scope_prompt_sha256() -> str:
    """분류기 `_SYSTEM` 문면의 sha256 — decompose 프롬프트와 **같은 방식**(문면 바이트)이다."""
    return hashlib.sha256(CATEGORY_SCOPE_SYSTEM.encode("utf-8")).hexdigest()


def underspecified_prompt_sha256() -> str:
    """#463 전용 분류기 문면의 sha256 — 끈 런에는 manifest가 None을 남긴다."""
    return hashlib.sha256(underspecified_module._SYSTEM.encode("utf-8")).hexdigest()
