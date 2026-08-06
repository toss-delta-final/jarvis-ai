"""run manifest — '무엇을 잰 표인지'를 산출물 안에 못 박는다 (#331).

거리 임계는 사전(taxonomy)에 종속된다(docs/lessons.md 2026-08-05) — 그래서 `dictionaryHash`
(categories 행 수 + 정렬된 category 문자열 전체의 sha256)를 임계 3종과 함께 남긴다.

[이슈 #401] 정본이 repo 편입(`db/catalog/seed/categories.json`)되면서 이 지문을 정본과 대조할
수 있게 됐다. 기존 `rowCount`/`sha256`(DB collation `ORDER BY category` 순서)은 과거 런과의
비교 가능성 때문에 손대지 않는다 — 대신 collation 무관한 codepoint 지문(`canonicalSha256`)과
정본 상태(`seed`)를 추가해 대조한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation import category_select as category_select_module
from app.agents.buyer.recommendation import decompose as decompose_module
from evals.metrics.run_manifest import build_run_manifest

MODULE_ROOT = Path(__file__).parent
DECOMPOSE_MODULE = Path(decompose_module.__file__)
CATEGORY_SELECT_MODULE = Path(category_select_module.__file__)
REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SEED_PATH = REPO_ROOT / "db" / "catalog" / "seed" / "categories.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint_rows(rows: Sequence[str]) -> dict[str, Any]:
    """행 수 + codepoint 정렬(파이썬 `sorted()`) `"\\n".join` sha256 — DB 없이 유닛테스트 가능.

    DB collation(en_US.utf8 등)에 종속된 기존 `dictionary_fingerprint` 의 `sha256` 과 달리,
    정렬 기준이 파이썬 표준 라이브러리라 어디서 계산해도 같은 값이 나온다.
    """
    digest = hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()
    return {"rowCount": len(rows), "sha256": digest}


def canonical_seed_fingerprint(path: Path = CANONICAL_SEED_PATH) -> dict[str, Any]:
    """repo 정본(`db/catalog/seed/categories.json`)의 codepoint 지문 + 경로."""
    leaves = json.loads(path.read_bytes().decode("utf-8"))
    fingerprint = fingerprint_rows(leaves)
    return {"path": str(path.relative_to(REPO_ROOT)), **fingerprint}


def dictionary_fingerprint(dsn: str) -> dict[str, Any]:
    """사전(taxonomy) 상태 지문 — 행 수 + 정렬된 category 문자열 전체의 sha256.

    거리 임계(`category_distance_max` 등)는 이 지문에 종속된다(lessons 2026-08-05) — 사전이
    재시드되면 이 값이 바뀌어 과거 런과 비교할 수 없다는 사실이 드러난다.

    `rowCount`/`sha256` 은 DB collation 순서(`ORDER BY category`) 그대로 둔다 — 여기를 바꾸면
    과거 런과 비교할 수 없어진다(이슈 #401). `canonicalSha256`/`seed`/`matchesSeed` 는 추가
    필드 — 같은 DB 행을 codepoint 정렬해 repo 정본과 대조한다.
    """
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT category FROM categories ORDER BY category")
        rows = [row[0] for row in cur.fetchall()]
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    canonical = fingerprint_rows(rows)
    seed = canonical_seed_fingerprint()
    return {
        "rowCount": len(rows),
        "sha256": digest,
        "canonicalSha256": canonical["sha256"],
        "seed": seed,
        "matchesSeed": canonical["sha256"] == seed["sha256"],
    }


def build_category_probe_manifest(
    *,
    command: str,
    seed: int,
    tier: str,
    model_config: dict[str, Any],
    n: int,
    attempt_multiplier: int,
    concurrency: int,
    anchor_path: Path,
    anchor_sha256: str,
    fixture_version: str,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    cell_ids: list[str],
    axis_definitions: dict[str, Any],
    dry_run: bool,
    thresholds: dict[str, float | int],
    dictionary: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest = build_run_manifest(command=command, seed=seed)
    manifest["categoryProbe"] = {
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
        "thresholds": thresholds,
        "dictionary": dictionary,
        "notGoldenset": "추천 품질이 아니라 발화→카테고리 매핑·선택 정확도다. evals/goldenset 과 "
        "숫자를 섞지 말 것.",
        "singleRunNotAVerdict": "단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.",
    }
    hashes = manifest["hashes"]
    assert isinstance(hashes, dict)
    hashes["anchorFixture"] = anchor_sha256
    hashes["decomposeSystemPrompt"] = _sha256_text(decompose_module._SYSTEM)
    hashes["categorySelectSystemPrompt"] = _sha256_text(category_select_module._SYSTEM)
    prompts = hashes.get("prompts")
    if isinstance(prompts, dict):
        prompts["categorySelect"] = _sha256_file(CATEGORY_SELECT_MODULE)
    hashes["categoryProbeModules"] = {
        path.name: _sha256_file(path) for path in sorted(MODULE_ROOT.glob("*.py"))
    }
    return manifest
