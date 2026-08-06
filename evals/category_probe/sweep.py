"""오프라인 임계 스윕 도구 — hits.csv 원시 top-k 거리로 런 재실행 없이 category_distance_max 등을
재튜닝한다 (#344).

`hits.csv` 는 (cellId, sampleIndex, legIndex, anchorKind, rank, canonical, distance) 행 전체를
남긴다(report.py `_write_csv(out / "hits.csv", ...)`) — 원시 top-k 거리 전량이라 이 도구가 API·pg·
LLM 콜 **0** 으로 dmax·override_margin 조합을 스윕할 수 있다.

**채택 규칙은 `app/agents/buyer/recommendation/category_mapping.py` §4 를 그대로 재현한다** —
아래 순서가 그 파일의 대응 지점이다. 어긋나면 이 스윕이 거짓을 말한다:

  1. winner 앵커 = 해당 (cell,sample,leg) 에 query 히트가 있으면 query, 없으면 raw
     (`category_mapping.py` §4.3.1, "query 우선 — 거리 비교 아님").
  2. d1 = rank1 거리 round(,4), margin = round(rank2−rank1, 4)(히트 1건이면 None)
     — `category_mapping._top1_with_margin` 과 같은 정밀도.
  3. 채택 = `d1 <= dmax` 또는 (`d1 > dmax` and `margin is not None` and `margin >= override_margin`)
     — `category_mapping.map_categories` 의 `category_distance_override`/`category_distance_rejected`
     분기(§4.5)와 같은 부등호 방향.
  4. §4.4 택일(LLM)은 오프라인 재현 불가 — **트리거 수만 센다**
     (`d1 <= dmax and margin is not None and margin <= select_margin`, `map_categories`
     의 `ambiguous` 필터와 동일).

CLI:
  uv run python -m evals.category_probe.sweep --run evals/category_probe/baselines/fast-2026-08-06
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evals.category_probe.loader import load_anchor_set
from evals.category_probe.schema import AnchorSet

# 도구 상수(판정 그리드 기본값) — CLAUDE.md 의 "튜너블 하드코딩 금지"는 런타임 config.py 필드에
# 적용되는 규칙이라 이 그리드 기본값에는 적용되지 않는다(packet-344 §A). 값 자체는
# `app/core/config.py` 의 현재 기본값(재측정 전 override_margin=0.035·select_margin_max=0.02, 이
# 둘은 #344 로 이동하지 않는다)과 일치시켜 "스윕이 실제 배포 설정을 재현한다"는 전제를 지킨다.
DEFAULT_DMAX_GRID = (0.22, 0.24, 0.26, 0.28)
DEFAULT_OVERRIDE_GRID = (0.035,)
DEFAULT_SELECT_MARGIN = 0.02

HitTuple = tuple[int, str, float]  # (rank, canonical, distance)


@dataclass(frozen=True)
class HitRow:
    cell_id: str
    sample_index: int
    leg_index: int
    anchor_kind: str
    rank: int
    canonical: str
    distance: float


def load_hits(run_dir: Path) -> list[HitRow]:
    """런 아티팩트 디렉터리 1개의 `hits.csv` 를 읽는다. 없으면 FileNotFoundError."""
    path = run_dir / "hits.csv"
    if not path.exists():
        raise FileNotFoundError(f"hits.csv 가 없습니다: {path} (런 아티팩트 디렉터리가 맞습니까?)")
    rows: list[HitRow] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for r in csv.DictReader(handle):
            rows.append(
                HitRow(
                    cell_id=r["cellId"],
                    sample_index=int(r["sampleIndex"]),
                    leg_index=int(r["legIndex"]),
                    anchor_kind=r["anchorKind"],
                    rank=int(r["rank"]),
                    canonical=r["canonical"],
                    distance=float(r["distance"]),
                )
            )
    return rows


def _group_by_leg(rows: list[HitRow]) -> dict[tuple[str, int, int], dict[str, list[HitTuple]]]:
    """(cellId, sampleIndex, legIndex) → anchorKind → rank 오름차순 히트 목록."""
    grouped: dict[tuple[str, int, int], dict[str, list[HitTuple]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        key = (row.cell_id, row.sample_index, row.leg_index)
        grouped[key][row.anchor_kind].append((row.rank, row.canonical, row.distance))
    for kinds in grouped.values():
        for hits in kinds.values():
            hits.sort(key=lambda h: h[0])
    return grouped


def top1_with_margin(hits: list[HitTuple]) -> tuple[str, float, float | None] | None:
    """`category_mapping._top1_with_margin` 과 동일 — (canonical, d1, margin) 또는 히트 0건이면 None."""
    if not hits:
        return None
    _rank1, canonical, distance = hits[0]
    d1 = round(distance, 4)
    margin = round(hits[1][2] - distance, 4) if len(hits) > 1 else None
    return canonical, d1, margin


def pick_winner(kinds: dict[str, list[HitTuple]]) -> tuple[str, float, float | None] | None:
    """query 히트가 있으면 query, 없으면 raw (§4.3.1 query 우선 — 거리 비교 아님)."""
    if kinds.get("query"):
        return top1_with_margin(kinds["query"])
    if kinds.get("raw"):
        return top1_with_margin(kinds["raw"])
    return None


def is_adopted(d1: float, margin: float | None, dmax: float, override_margin: float) -> bool:
    """`map_categories` §4.5 의 채택 판정 — `picked[1] > distance_max` 비교 방향 그대로."""
    if d1 <= dmax:
        return True
    return margin is not None and margin >= override_margin


def is_select_triggered(d1: float, margin: float | None, dmax: float, select_margin: float) -> bool:
    """`map_categories` 의 `ambiguous` 필터(§4.4) — 거리컷 통과분 중 마진이 얇은 것만."""
    return d1 <= dmax and margin is not None and margin <= select_margin


@dataclass
class SweepResult:
    dmax: float
    override_margin: float
    select_margin: float

    single_total: int = 0
    single_adopted_correct: int = 0
    single_adopted_wrong: int = 0
    single_dropped: int = 0
    single_no_hit: int = 0
    single_dropped_cells: dict[str, int] = field(default_factory=dict)  # cellId → 드롭 표본 수

    multi_leg_total: int = 0
    multi_leg_covered: int = 0

    none_total: int = 0
    none_forced: int = 0

    nic_total: int = 0
    nic_forced: int = 0

    select_trigger_total: int = 0

    def add(self, other: "SweepResult") -> "SweepResult":
        merged_dropped = dict(self.single_dropped_cells)
        for cell_id, count in other.single_dropped_cells.items():
            merged_dropped[cell_id] = merged_dropped.get(cell_id, 0) + count
        return SweepResult(
            dmax=self.dmax,
            override_margin=self.override_margin,
            select_margin=self.select_margin,
            single_total=self.single_total + other.single_total,
            single_adopted_correct=self.single_adopted_correct + other.single_adopted_correct,
            single_adopted_wrong=self.single_adopted_wrong + other.single_adopted_wrong,
            single_dropped=self.single_dropped + other.single_dropped,
            single_no_hit=self.single_no_hit + other.single_no_hit,
            single_dropped_cells=merged_dropped,
            multi_leg_total=self.multi_leg_total + other.multi_leg_total,
            multi_leg_covered=self.multi_leg_covered + other.multi_leg_covered,
            none_total=self.none_total + other.none_total,
            none_forced=self.none_forced + other.none_forced,
            nic_total=self.nic_total + other.nic_total,
            nic_forced=self.nic_forced + other.nic_forced,
            select_trigger_total=self.select_trigger_total + other.select_trigger_total,
        )


def _run_sample_count(rows: list[HitRow]) -> int:
    """이 런의 셀당 표본 수(N) — 전 셀에 걸친 최대 sampleIndex+1(README §328 4항, 셀당 균일 N).

    특정 (cell,sample) 이 히트를 하나도 못 냈어도(신호 0건) 그 표본은 여전히 시도된 것이라
    분모에 들어가야 한다 — `hits.csv` 는 히트가 있는 행만 남기므로 존재하는 행만으로 표본
    범위를 잡으면 신호 0건 표본이 조용히 분모에서 빠진다(#344 검증 대상 nic 0/40 이 0/37 로
    줄어드는 식).
    """
    if not rows:
        return 0
    return max(row.sample_index for row in rows) + 1


def sweep_one(
    rows: list[HitRow],
    anchors: AnchorSet,
    *,
    dmax: float,
    override_margin: float,
    select_margin: float,
) -> SweepResult:
    """rows(단일 런) 하나에 (dmax, override_margin) 조합 하나를 적용한다."""
    grouped = _group_by_leg(rows)
    result = SweepResult(dmax=dmax, override_margin=override_margin, select_margin=select_margin)
    sample_ids = list(range(_run_sample_count(rows)))

    # legIndex 별 (winner, adopted?) 를 사전 계산해 single/multi/none/notInCatalog 가 재사용한다.
    picks: dict[tuple[str, int, int], tuple[str, float, float | None] | None] = {
        key: pick_winner(kinds) for key, kinds in grouped.items()
    }
    for picked in picks.values():
        if picked is None:
            continue
        _canonical, d1, margin = picked
        if is_select_triggered(d1, margin, dmax, select_margin):
            result.select_trigger_total += 1

    for cell in anchors.cells:
        if cell.slice_id == "single":
            accept = set(cell.expected_legs[0].accept) if cell.expected_legs else set()
            for sample_index in sample_ids:
                result.single_total += 1
                leg_indices = sorted(
                    leg
                    for (cid, sidx, leg) in grouped
                    if cid == cell.cell_id and sidx == sample_index
                )
                adopted_canonicals: list[str] = []
                any_hit = False
                for leg_index in leg_indices:
                    picked = picks.get((cell.cell_id, sample_index, leg_index))
                    if picked is None:
                        continue
                    any_hit = True
                    canonical, d1, margin = picked
                    if is_adopted(d1, margin, dmax, override_margin):
                        adopted_canonicals.append(canonical)
                if any(c in accept for c in adopted_canonicals):
                    result.single_adopted_correct += 1
                elif adopted_canonicals:
                    result.single_adopted_wrong += 1
                elif any_hit:
                    result.single_dropped += 1
                    result.single_dropped_cells[cell.cell_id] = (
                        result.single_dropped_cells.get(cell.cell_id, 0) + 1
                    )
                else:
                    result.single_no_hit += 1
        elif cell.slice_id == "multi":
            for expected_leg in cell.expected_legs:
                accept = set(expected_leg.accept)
                for sample_index in sample_ids:
                    result.multi_leg_total += 1
                    leg_indices = sorted(
                        leg
                        for (cid, sidx, leg) in grouped
                        if cid == cell.cell_id and sidx == sample_index
                    )
                    covered = False
                    for leg_index in leg_indices:
                        picked = picks.get((cell.cell_id, sample_index, leg_index))
                        if picked is None:
                            continue
                        canonical, d1, margin = picked
                        if is_adopted(d1, margin, dmax, override_margin) and canonical in accept:
                            covered = True
                            break
                    if covered:
                        result.multi_leg_covered += 1
        elif cell.slice_id in ("none", "notInCatalog"):
            for sample_index in sample_ids:
                if cell.slice_id == "none":
                    result.none_total += 1
                else:
                    result.nic_total += 1
                forced = False
                leg_indices = sorted(
                    leg
                    for (cid, sidx, leg) in grouped
                    if cid == cell.cell_id and sidx == sample_index
                )
                for leg_index in leg_indices:
                    picked = picks.get((cell.cell_id, sample_index, leg_index))
                    if picked is None:
                        continue
                    _canonical, d1, margin = picked
                    if is_adopted(d1, margin, dmax, override_margin):
                        forced = True
                        break
                if forced:
                    if cell.slice_id == "none":
                        result.none_forced += 1
                    else:
                        result.nic_forced += 1
    return result


def sweep_grid(
    rows_by_run: dict[str, list[HitRow]],
    anchors: AnchorSet,
    *,
    dmax_grid: tuple[float, ...],
    override_grid: tuple[float, ...],
    select_margin: float,
) -> dict[tuple[float, float], dict[str, SweepResult]]:
    """런별 결과 + `"combined"` 합산을 (dmax, override) 조합별로 담는다."""
    out: dict[tuple[float, float], dict[str, SweepResult]] = {}
    for dmax in dmax_grid:
        for override_margin in override_grid:
            per_run: dict[str, SweepResult] = {}
            combined: SweepResult | None = None
            for run_label, rows in rows_by_run.items():
                res = sweep_one(
                    rows,
                    anchors,
                    dmax=dmax,
                    override_margin=override_margin,
                    select_margin=select_margin,
                )
                per_run[run_label] = res
                combined = res if combined is None else combined.add(res)
            if combined is not None:
                per_run["combined"] = combined
            out[(dmax, override_margin)] = per_run
    return out


def _fmt_cells(dropped_cells: dict[str, int]) -> str:
    if not dropped_cells:
        return "-"
    return ", ".join(f"{cid}×{n}" for cid, n in sorted(dropped_cells.items()))


def render_report(grid: dict[tuple[float, float], dict[str, SweepResult]]) -> str:
    lines: list[str] = ["# 카테고리 임계 스윕 (#344)", ""]
    for (dmax, override_margin), per_run in sorted(grid.items()):
        lines.append(f"## dmax={dmax} override={override_margin}")
        for run_label, res in per_run.items():
            lines.append(f"### {run_label}")
            lines.append(
                f"- single: 채택정답 {res.single_adopted_correct}/{res.single_total} · "
                f"오답채택 {res.single_adopted_wrong} · 드롭 {res.single_dropped} · "
                f"신호없음 {res.single_no_hit}"
            )
            lines.append(f"  드롭 잔존 셀: {_fmt_cells(res.single_dropped_cells)}")
            lines.append(f"- multi leg 커버리지: {res.multi_leg_covered}/{res.multi_leg_total}")
            lines.append(f"- none 오강제: {res.none_forced}/{res.none_total}")
            lines.append(f"- notInCatalog 오강제: {res.nic_forced}/{res.nic_total}")
            lines.append(f"- select 트리거 수(진단, LLM 결과 미재현): {res.select_trigger_total}")
            lines.append("")
    return "\n".join(lines)


def _parse_float_grid(text: str) -> tuple[float, ...]:
    return tuple(float(v.strip()) for v in text.split(",") if v.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="오프라인 임계 스윕 — hits.csv 로 category_distance_max 등을 런 재실행 없이 재튜닝 (#344)"
    )
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        type=Path,
        help="런 아티팩트 디렉터리(hits.csv 필요). 여러 번 지정하면 런별+합산 둘 다 표시.",
    )
    parser.add_argument(
        "--fixture", default="default", help="앵커 fixture 이름/경로 (기본 anchors.json)"
    )
    parser.add_argument(
        "--dmax-grid",
        default=",".join(str(v) for v in DEFAULT_DMAX_GRID),
        help=f"쉼표구분 category_distance_max 그리드 (기본 {DEFAULT_DMAX_GRID})",
    )
    parser.add_argument(
        "--override-grid",
        default=",".join(str(v) for v in DEFAULT_OVERRIDE_GRID),
        help=f"쉼표구분 category_distance_override_margin 그리드 (기본 {DEFAULT_OVERRIDE_GRID})",
    )
    parser.add_argument(
        "--select-margin",
        type=float,
        default=DEFAULT_SELECT_MARGIN,
        help=f"category_select_margin_max — 트리거 수만 진단(기본 {DEFAULT_SELECT_MARGIN})",
    )
    parser.add_argument("--out", type=Path, help="결과를 이 md 파일에도 쓴다")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    anchors = load_anchor_set(args.fixture)
    rows_by_run = {str(run_dir): load_hits(run_dir) for run_dir in args.run}
    grid = sweep_grid(
        rows_by_run,
        anchors,
        dmax_grid=_parse_float_grid(args.dmax_grid),
        override_grid=_parse_float_grid(args.override_grid),
        select_margin=args.select_margin,
    )
    report = render_report(grid)
    print(report)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
