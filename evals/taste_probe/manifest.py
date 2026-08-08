"""run manifest — '무엇을 쟀는지'를 산출물 안에 못 박는다 (#462, `evals/category_probe/manifest.py` 규약).

`hashes.deltaSystemPrompt` = `sha256(builder._DELTA_SYSTEM)` — 프롬프트가 바뀌면 과거 표와 비교
불가라는 사실이 산출물에서 보여야 한다(#198 전례). `dictionary` 는 라이브 런에서만
`evals.category_probe.manifest.dictionary_fingerprint` 를 **import 해 재사용**한다(복제 금지) —
category kind 의 정확도가 같은 pg-catalog `categories` 사전에 종속되므로 그 지문을 그대로 쓴다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.agents.profile import builder as builder_module
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def delta_system_prompt_sha256() -> str:
    """`builder._DELTA_SYSTEM` 의 sha256 — manifest 와 `report.md` 헤더(A-16)가 같은 값을 쓴다.

    `cli.py` 가 `report.md` 첫 줄에 sha12 를 싣기 위해 이 함수를 재사용한다 — 해시 계산이
    manifest.py 와 report.py 두 곳에서 각자 다시 계산되면 조용히 갈릴 수 있다.
    """
    return _sha256_text(builder_module._DELTA_SYSTEM)


def build_taste_probe_manifest(
    *,
    command: str,
    seed: int,
    model_config: dict[str, Any],
    n: int,
    attempt_multiplier: int,
    concurrency: int,
    fixture_path: Path,
    fixture_sha256: str,
    dataset_version: str,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    session_ids: list[str],
    axis_definitions: dict[str, Any],
    dry_run: bool,
    thresholds: dict[str, float | int],
    dictionary: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["tasteProbe"] = {
        "modelConfig": model_config,
        "n": n,
        "attemptMultiplier": attempt_multiplier,
        "concurrency": concurrency,
        "dryRun": dry_run,
        "fixtureName": fixture_path.name,
        "datasetVersion": dataset_version,
        "pacer": pacer,
        "budget": budget,
        "sessionIds": session_ids,
        "axisDefinitions": axis_definitions,
        "thresholds": thresholds,
        "dictionary": dictionary,
        "notGoldenset": "단발 검색 질의 + 상품 순위 라벨이 아니라 다턴 대화 → 기대 트리플의 "
        "추출·resolver 정확도다. evals/goldenset 과 숫자를 섞지 말 것.",
        "singleRunNotAVerdict": "단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.",
    }
    hashes = manifest["hashes"]
    assert isinstance(hashes, dict)
    hashes["datasetFixture"] = fixture_sha256
    hashes["deltaSystemPrompt"] = delta_system_prompt_sha256()
    hashes["tasteProbeModules"] = {
        path.name: _sha256_file(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest
