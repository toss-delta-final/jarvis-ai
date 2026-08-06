"""앵커 로더 — 해시 게이트·셀 전개 (#380)."""

from __future__ import annotations

from evals.underspecified_probe.loader import build_cells, load_anchor_set


def test_build_cells_wraps_every_anchor_and_sorts_by_cell_id() -> None:
    anchors = load_anchor_set()
    cells = build_cells(anchors)
    assert len(cells) == len(anchors.utterances)
    assert [cell.cell_id for cell in cells] == sorted(cell.cell_id for cell in cells)
    assert {cell.anchor.case_id for cell in cells} == {a.case_id for a in anchors.utterances}


def test_cell_id_equals_anchor_case_id() -> None:
    anchors = load_anchor_set()
    cells = build_cells(anchors)
    for cell in cells:
        assert cell.cell_id == cell.anchor.case_id
