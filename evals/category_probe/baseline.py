"""trivial baseline (#331 §6) — `evals/README.md` 1항 "trivial baseline 의무"의 이 하네스 몫.

정의: 발화 원문을 그대로 `embedding_task_query` 로 임베딩 → `search_categories_pg` top-5 → top-1.
**LLM 0콜, 거리컷·택일 없음.** 셀당 결정론 1회다. 파이프라인(decompose+map_categories) 수치와
같은 report.md 에 나란히 실어 "아무것도 안 하는 것보다 나은가"를 보인다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from evals.category_probe.schema import AnchorCell, AnchorSet

EmbedFn = Callable[[list[str]], list[list[float]]]
SearchFn = Callable[..., list[tuple[str, float]]]


@dataclass(frozen=True)
class BaselineResult:
    cell_id: str
    top1: str | None
    top5: list[str]


def run_baseline(
    anchors: AnchorSet, *, embed: EmbedFn, search_top_k: SearchFn, dsn: str, k: int = 5
) -> list[BaselineResult]:
    """single 슬라이스(30셀)에 대해 결정론 1회 top-5 를 낸다. 다른 슬라이스는 정의상 채점 대상이 아니다."""
    single_cells = [cell for cell in anchors.cells if cell.slice_id == "single"]
    texts = [cell.utterance for cell in single_cells]
    if not texts:
        return []
    vecs = embed(texts)
    results: list[BaselineResult] = []
    for cell, vec in zip(single_cells, vecs, strict=True):
        hits = search_top_k(vec, dsn, k=k)
        top5 = [canonical for canonical, _distance in hits]
        results.append(
            BaselineResult(cell_id=cell.cell_id, top1=top5[0] if top5 else None, top5=top5)
        )
    return results


def score_baseline(results: list[BaselineResult], anchors: AnchorSet) -> dict[str, Any]:
    cells_by_id: dict[str, AnchorCell] = {cell.cell_id: cell for cell in anchors.cells}
    top1_num = topk_num = 0
    denom = len(results)
    for result in results:
        cell = cells_by_id[result.cell_id]
        accept = set(cell.expected_legs[0].accept)
        if result.top1 is not None and result.top1 in accept:
            top1_num += 1
        if accept & set(result.top5):
            topk_num += 1
    return {
        "baselineTop1Single": {
            "axisId": "baselineTop1Single",
            "title": "trivial baseline top-1",
            "numerator": top1_num,
            "denominator": denom,
            "ratio": (top1_num / denom) if denom else None,
            "definition": {
                "numerator": "임베딩 최근접 top-1 ∈ accept",
                "denominator": "single 30셀(결정론 1회)",
            },
        },
        "baselineTopK": {
            "axisId": "baselineTopK",
            "title": "trivial baseline top-5 포함",
            "numerator": topk_num,
            "denominator": denom,
            "ratio": (topk_num / denom) if denom else None,
            "definition": {
                "numerator": "accept ∈ baseline top-5",
                "denominator": "single 30셀(결정론 1회)",
            },
        },
        "note": (
            "none/notInCatalog 슬라이스에서 baseline 은 항상 top-1 을 강제하므로 정의상 0% 다 — "
            "baseline 은 기권할 수 없다는 사실 자체가 정보다(§6)."
        ),
    }
