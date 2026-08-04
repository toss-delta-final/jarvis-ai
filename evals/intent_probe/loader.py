"""앵커 정답지 로더 — 해시 게이트, 셀 전개, decompose 컨텍스트 조립."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.schemas.spring import ProductSearchFilters
from evals.intent_probe.schema import AnchorSet, ProbeContext, Utterance

FIXTURE_DIR = Path(__file__).with_name("fixtures")
BUILTIN_FIXTURES = {"a": "anchors_a.json", "b": "anchors_b.json"}


@dataclass(frozen=True)
class Cell:
    """측정 단위 = 발화 1개 × 컨텍스트 1개. 이 셀을 N회 반복해 분포를 만든다."""

    cell_id: str
    utterance: Utterance
    context: ProbeContext


def resolve_fixture_path(name: str, *, fixture_dir: Path | None = None) -> Path:
    """`a`/`b` 별칭 또는 임의 경로를 앵커 파일 경로로 푼다."""
    directory = fixture_dir or FIXTURE_DIR
    if name in BUILTIN_FIXTURES:
        return directory / BUILTIN_FIXTURES[name]
    return Path(name)


def fixture_sha256(path: Path) -> str:
    """앵커 파일의 sha256 — 산출물에 그대로 실려 '무엇을 쟀는지'를 남긴다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_anchor_set(name: str = "b", *, fixture_dir: Path | None = None) -> AnchorSet:
    """앵커 파일을 읽는다. 커밋된 픽스처는 manifest 해시와 대조한다.

    manifest 에 없는 외부 경로(후보 정답지 실험)는 해시 대조를 건너뛰되, 호출부가
    `fixture_sha256` 으로 산출물에 해시를 남긴다 — 어떤 정답지로 잰 표인지 항상 특정된다.
    """
    path = resolve_fixture_path(name, fixture_dir=fixture_dir)
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256", {}).get(path.name)
        if expected and expected != fixture_sha256(path):
            raise ValueError(
                f"{path.name} 의 SHA-256 이 manifest 와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    return AnchorSet.model_validate_json(path.read_text(encoding="utf-8"))


def build_cells(anchors: AnchorSet) -> list[Cell]:
    """발화 × 컨텍스트를 전개한다. cellId 정렬이라 동시 실행에도 순서가 결정론이다."""
    by_id = {context.context_id: context for context in anchors.contexts}
    cells = [
        Cell(
            cell_id=f"{utterance.utterance_id}|{context_id}",
            utterance=utterance,
            context=by_id[context_id],
        )
        for utterance in anchors.utterances
        for context_id in utterance.contexts
    ]
    return sorted(cells, key=lambda cell: cell.cell_id)


def build_context_kwargs(anchors: AnchorSet, context: ProbeContext) -> dict[str, Any]:
    """decompose 가 받는 세션 상태 인자를 만든다.

    PENDING_CART 모양은 app/agents/buyer/graph.py 가 실제로 넘기는 dict 와 같아야 한다
    (quantity·attempts 는 프롬프트에 싣지 않는다).
    """
    pending = None
    if context.include_pending_cart:
        pending = {
            "productId": anchors.reask_product_id,
            "options": [
                {"optionId": option.option_id, "name": option.name} for option in anchors.options
            ],
        }
    return {
        "prior_filters": (
            ProductSearchFilters.model_validate(anchors.prior_filters)
            if context.include_prior_filters
            else None
        ),
        "last_recommendations": (
            [(product.product_id, product.name) for product in anchors.last_recommendations]
            if context.include_last_recommendations
            else None
        ),
        "pending_cart": pending,
    }
