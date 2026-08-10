"""run manifest — '무엇을 잰 표인지'를 산출물 안에 못 박는다 (legs_probe/manifest.py 와 같은 규약)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from evals.intent_probe.client import PromptIdentity
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_underspecified_probe_manifest(
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
    underspecified_reask_enabled: bool,
    attr_axis_suppression: bool,
    attr_constraint_axes: list[str],
    pacer: dict[str, Any],
    budget: dict[str, Any],
    cell_ids: list[str],
    axis_definitions: dict[str, Any],
    dry_run: bool,
    union: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`build_run_manifest` 위에 이 프로브 고유의 지문을 얹는다.

    `hashes.systemPrompt` 는 **실제로 provider 에 보낸 텍스트**의 해시다 — 프로덕션 소스 자체는
    건드리지 않으므로 여기서는 프롬프트 문면 해시만 남긴다(intent_probe·legs_probe 와 동형).
    """
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["underspecifiedProbe"] = {
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
        "judgment": {
            "underspecifiedReaskEnabled": underspecified_reask_enabled,
        },
        "attrAxisSuppression": attr_axis_suppression,
        "attrConditionConstraintAxes": list(attr_constraint_axes),
        "seedScope": "셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.",
        # [§D10, F-3] 측정 범위와 한계 — README §측정 범위와 한계의 요약. `detect_expansion_need`
        # (게이트 판정 함수)는 진단 목적으로 매 표본 부른다 — "needs_expansion 은 부르지 않는다"는
        # 표현은 이 호출과 모순됐다(F-3, 2차 리뷰어 발견). 부르지 않는 것은 카테고리 매핑과
        # 전개 LLM 생성(needs_expansion 의 전개 호출)뿐이다.
        "measurementScope": "decompose 직후·판정(is_underspecified_turn) 직전의 RouteDecision 만 "
        "잰다 — 카테고리 매핑(#331)과 needs_expansion(#217) 의 전개 LLM 생성은 부르지 않는다. "
        "다만 게이트 판정 함수 detect_expansion_need 자체는 진단 목적으로 매 표본 부른다"
        "(expansionGateWouldFireRate·missRateUnderExpansionAssumption 의 근거). category_legs 는 "
        "항상 비고, filters.category 는 decompose JSON 스키마에 키가 없어 구조적으로 항상 빈다. "
        "프로덕션은 intent==recommend 인 턴에서만 판정을 호출하므로 confirmatory 축은 recommend "
        "표본으로 좁힌다(F-1, nonRecommendIntentCount 참조). 단일 턴만 잰다(컨텍스트 행렬 없음).",
        "notGoldenset": "이건 골든셋이 아니다 — 추천 품질이 아니라 첫 턴 과소지정 판정의 실 LLM "
        "반복 분포다. #372 의 되물음 답변 턴 결정론 fixture 실측과 숫자를 섞지 말 것.",
        "singleRunNotAVerdict": "단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다. "
        "이 실측은 underspecified_reask_enabled 기본값을 전환하지 않는다.",
    }
    if union is not None:
        manifest["underspecifiedProbe"]["union"] = union
    hashes = manifest["hashes"]
    assert isinstance(hashes, dict)
    hashes["anchorFixture"] = _sha256(anchor_path)
    hashes["systemPrompt"] = prompt.sha256
    hashes["underspecifiedProbeModules"] = {
        path.name: _sha256(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest
