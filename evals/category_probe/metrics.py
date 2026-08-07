"""축 정의와 채점 (#331).

정의 문장을 **코드 옆 데이터로** 둔다 — `AxisSpec` 은 산출물(`results.json`·`report.md`)에 그대로
실린다. 숫자가 정의 없이 돌아다니면 같은 이름의 지표가 다른 뜻으로 비교되는 사고가 난다
(`evals/README.md` 8항 — 지표는 분자·분모 정의 동봉).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import zip_longest
from statistics import median
from typing import Any

from evals.category_probe.runner import CellResult, Sample
from evals.category_probe.schema import AnchorCell, AnchorSet

DEFAULT_CATEGORY_TOP_K = 5


def _cells_by_slice(anchors: AnchorSet, slice_id: str) -> dict[str, AnchorCell]:
    return {cell.cell_id: cell for cell in anchors.cells if cell.slice_id == slice_id}


def _final_canonicals(sample: Sample) -> set[str]:
    return {canonical for canonical, _query in sample.legs}


def _winning_kind_hits(sample: Sample, leg_index: int, top_k: int) -> set[str]:
    """leg 하나의 **이긴 앵커**(query 우선, §4.3) top-k 히트 canonical 집합.

    "이긴 앵커"는 production `map_categories` 의 `query 우선 — 거리 비교가 아니다` 규칙을 그대로
    옮긴 것이다: query 앵커가 히트를 냈으면 query, 아니면 raw. 판정(거리컷·마진 택일)을 다시
    구현하지 않는다 — 여기서 보는 것은 "이 leg 이 어떤 후보 풀을 봤는가"(topKInclusion 계산용)
    뿐이다.
    """
    by_kind: dict[str, list] = {}
    for hit in sample.hits:
        if hit.leg_index == leg_index:
            by_kind.setdefault(hit.anchor_kind, []).append(hit)
    kind = "query" if "query" in by_kind else ("raw" if "raw" in by_kind else None)
    if kind is None:
        return set()
    return {hit.canonical for hit in by_kind[kind] if hit.rank <= top_k}


def _sample_topk_pool(sample: Sample, top_k: int) -> set[str]:
    leg_indices = {hit.leg_index for hit in sample.hits}
    pool: set[str] = set()
    for leg_index in leg_indices:
        pool |= _winning_kind_hits(sample, leg_index, top_k)
    return pool


@dataclass(frozen=True)
class AxisResult:
    axis_id: str
    title: str
    numerator: int
    denominator: int
    expected_denominator: int
    unfilled_sample_count: int
    definition_numerator: str
    definition_denominator: str

    @property
    def ratio(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "axisId": self.axis_id,
            "title": self.title,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "expectedDenominator": self.expected_denominator,
            "unfilledSampleCount": self.unfilled_sample_count,
            "ratio": self.ratio,
            "definition": {
                "numerator": self.definition_numerator,
                "denominator": self.definition_denominator,
            },
        }


def _single_cells(anchors: AnchorSet) -> dict[str, AnchorCell]:
    return _cells_by_slice(anchors, "single")


def score_top1_single(results: list[CellResult], anchors: AnchorSet, *, n: int) -> AxisResult:
    cells = _single_cells(anchors)
    numerator = denominator = expected = 0
    for result in results:
        cell = cells.get(result.cell_id)
        if cell is None:
            continue
        accept = set(cell.expected_legs[0].accept)
        expected += n
        denominator += len(result.samples)
        numerator += sum(1 for sample in result.samples if accept & _final_canonicals(sample))
    return AxisResult(
        axis_id="top1Single",
        title="단일 카테고리 top-1 일치",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="최종 legs 에 기대 leg 의 accept 중 하나가 존재",
        definition_denominator="single(MFT+INV) 30셀 × N",
    )


def score_topk_inclusion(
    results: list[CellResult],
    anchors: AnchorSet,
    *,
    n: int,
    category_top_k: int = DEFAULT_CATEGORY_TOP_K,
) -> AxisResult:
    cells = _single_cells(anchors)
    numerator = denominator = expected = 0
    for result in results:
        cell = cells.get(result.cell_id)
        if cell is None:
            continue
        accept = set(cell.expected_legs[0].accept)
        expected += n
        denominator += len(result.samples)
        for sample in result.samples:
            # [F1-2] exact match 는 exact_lookup 이 즉시 맞춰 임베딩·검색 자체를 생략하므로 hits 가
            # 없는 것이 **정상**이다(runner._CallRecorder 문서 참조). hits 만 보면 파이프라인이
            # 실제로 정답을 낸 표본을 "포함 실패"로 오분류한다 — exact 경로도 "후보 풀에 정답이
            # 있었다"가 참이므로 최종 legs canonical 을 포함 풀에 합친다.
            pool = _sample_topk_pool(sample, category_top_k) | _final_canonicals(sample)
            if accept & pool:
                numerator += 1
    return AxisResult(
        axis_id="topKInclusion",
        title=f"이긴 앵커 top-{category_top_k} 포함",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator=(
            f"기대 accept 중 하나가 이긴 앵커의 top-{category_top_k} 후보(계측 hits 기준) 또는 "
            "최종 legs(exact match 는 hits 없이 바로 canonical 을 내므로 포함) 안에 존재 "
            "— decompose 가 leg 자체를 안 냈으면 분자 불충족"
        ),
        definition_denominator="single(MFT+INV) 30셀 × N",
    )


def score_multi_coverage(results: list[CellResult], anchors: AnchorSet, *, n: int) -> AxisResult:
    cells = _cells_by_slice(anchors, "multi")
    numerator = denominator = expected = 0
    for result in results:
        cell = cells.get(result.cell_id)
        if cell is None:
            continue
        expected += n * len(cell.expected_legs)
        denominator += len(result.samples) * len(cell.expected_legs)
        for sample in result.samples:
            final = _final_canonicals(sample)
            numerator += sum(1 for leg in cell.expected_legs if set(leg.accept) & final)
    return AxisResult(
        axis_id="multiCoverage",
        title="fan-out leg 커버리지",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="기대 leg 별로 accept ∈ legs (leg 단위 집계)",
        definition_denominator="multi 6셀 × 기대 leg 수 × N",
    )


def score_multi_exact_set(results: list[CellResult], anchors: AnchorSet, *, n: int) -> AxisResult:
    cells = _cells_by_slice(anchors, "multi")
    numerator = denominator = expected = 0
    for result in results:
        cell = cells.get(result.cell_id)
        if cell is None:
            continue
        expected += n
        denominator += len(result.samples)
        for sample in result.samples:
            final = _final_canonicals(sample)
            covers_all = all(set(leg.accept) & final for leg in cell.expected_legs)
            no_extras = len(final) == len(cell.expected_legs)
            if covers_all and no_extras:
                numerator += 1
    return AxisResult(
        axis_id="multiExactSet",
        title="fan-out leg 집합 정확 일치",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="legs 집합이 기대 leg 집합과 정확히 일치(여분 leg 없음)",
        definition_denominator="multi 6셀 × N",
    )


def _no_force_axis(
    results: list[CellResult],
    anchors: AnchorSet,
    *,
    n: int,
    slice_id: str,
    axis_id: str,
    title: str,
) -> AxisResult:
    cells = _cells_by_slice(anchors, slice_id)
    numerator = denominator = expected = 0
    for result in results:
        if result.cell_id not in cells:
            continue
        expected += n
        denominator += len(result.samples)
        numerator += sum(1 for sample in result.samples if not sample.legs)
    return AxisResult(
        axis_id=axis_id,
        title=title,
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="legs == [] (오답 canonical 을 강제하지 않음)",
        definition_denominator=f"{slice_id} 5셀 × N",
    )


def score_none_no_force(results: list[CellResult], anchors: AnchorSet, *, n: int) -> AxisResult:
    return _no_force_axis(
        results,
        anchors,
        n=n,
        slice_id="none",
        axis_id="noneNoForce",
        title="카테고리 무지정 → 무필터",
    )


def score_not_in_catalog_no_force(
    results: list[CellResult], anchors: AnchorSet, *, n: int
) -> AxisResult:
    return _no_force_axis(
        results,
        anchors,
        n=n,
        slice_id="notInCatalog",
        axis_id="notInCatalogNoForce",
        title="사전에 없는 카테고리 → 무필터",
    )


def _unique_majority(values: list[str]) -> str | None:
    """최빈값이 **유일한 최대**일 때만 다수결로 인정한다(F1-3).

    `Counter.most_common(1)` 은 동률에서 삽입 순서로 대표값을 뽑아 "무승부"를 조용히 어느 한쪽의
    승리처럼 보이게 한다 — 동률은 그 자체로 "이 변형 그룹이 하나로 수렴하지 못했다"는 뜻이라
    None(다수결 없음)으로 떨어뜨려야 한다.
    """
    counts = Counter(values)
    top_count = max(counts.values())
    tied = [value for value, count in counts.items() if count == top_count]
    return tied[0] if len(tied) == 1 else None


def score_inv_agreement(results: list[CellResult], anchors: AnchorSet) -> AxisResult:
    """[INV] 그룹 내 전 변형의 다수결 top-1 canonical 이 서로 동일하고 null 아닌가."""
    by_cell = {result.cell_id: result for result in results}
    groups: dict[str, list[AnchorCell]] = {}
    for cell in anchors.cells:
        if cell.inv_group_id is not None:
            groups.setdefault(cell.inv_group_id, []).append(cell)

    agreeing = 0
    for cells in groups.values():
        majorities: list[str | None] = []
        for cell in cells:
            result = by_cell.get(cell.cell_id)
            if result is None or not result.samples:
                majorities.append(None)
                continue
            top1s = [sample.legs[0][0] for sample in result.samples if sample.legs]
            majorities.append(_unique_majority(top1s) if top1s else None)
        if len(set(majorities)) == 1 and majorities[0] is not None:
            agreeing += 1
    return AxisResult(
        axis_id="invAgreement",
        title="INV 표기 변형 합의",
        numerator=agreeing,
        denominator=len(groups),
        expected_denominator=len(groups),
        unfilled_sample_count=0,
        definition_numerator="그룹 내 전 변형의 다수결 top-1 canonical 이 서로 동일하고 null 아님 "
        "(동률은 다수결 없음=None 이라 불합의로 센다)",
        definition_denominator="4그룹",
    )


def score_all(
    results: list[CellResult],
    anchors: AnchorSet,
    *,
    n: int,
    category_top_k: int = DEFAULT_CATEGORY_TOP_K,
) -> dict[str, AxisResult]:
    axes = (
        score_top1_single(results, anchors, n=n),
        score_topk_inclusion(results, anchors, n=n, category_top_k=category_top_k),
        score_multi_coverage(results, anchors, n=n),
        score_multi_exact_set(results, anchors, n=n),
        score_none_no_force(results, anchors, n=n),
        score_not_in_catalog_no_force(results, anchors, n=n),
        score_inv_agreement(results, anchors),
    )
    return {axis.axis_id: axis for axis in axes}


def diagnostics(results: list[CellResult], anchors: AnchorSet) -> dict[str, Any]:
    """합불이 아닌 진단 카운터 — 실패 귀속의 핵심(packet-331 §5)."""
    cells_by_id = {cell.cell_id: cell for cell in anchors.cells}
    intent_slip_count = sum(result.intent_slip_count for result in results)
    no_leg_count = 0
    distance_rejected_count = 0
    select_null_count = 0
    select_changed_count = 0
    exact_hit_count = 0
    expansion_taken_count = 0
    for result in results:
        cell = cells_by_id.get(result.cell_id)
        for sample in result.samples:
            if (
                cell is not None
                and cell.slice_id in ("single", "multi")
                and not sample.decompose_legs
            ):
                no_leg_count += 1
            if sample.expansion_leaves:
                expansion_taken_count += 1
            for event in sample.events:
                name = event.get("event")
                if name == "category_distance_rejected":
                    distance_rejected_count += 1
                elif name == "category_select_null":
                    select_null_count += 1
                elif name == "category_selected" and event.get("changed") is True:
                    select_changed_count += 1
                elif name == "category_mapped":
                    exact_hit_count += 1
    return {
        "intentSlipCount": intent_slip_count,
        "noLegCount": no_leg_count,
        "distanceRejectedCount": distance_rejected_count,
        "selectNullCount": select_null_count,
        "selectChangedCount": select_changed_count,
        "exactHitCount": exact_hit_count,
        "expansionTakenCount": expansion_taken_count,
        "definition": {
            "intentSlipCount": "decompose 가 recommend 가 아닌 intent 를 내 버린 시도 수 — "
            "라우팅 미끄러짐은 evals/intent_probe 가 재는 축이다",
            "noLegCount": "single/multi 표본 중 decompose 가 categoryQueries 를 하나도 못 낸 수 "
            "— 매핑 실패와 추출 실패를 가른다",
            "distanceRejectedCount": "category_distance_rejected 이벤트 발생 수",
            "selectNullCount": "category_select_null 이벤트 발생 수",
            "selectChangedCount": "category_selected(changed=true) 이벤트 발생 수",
            "exactHitCount": "category_mapped(exact match) 이벤트 발생 수",
            "expansionTakenCount": "expansion_leaves 가 비어있지 않은 표본 수",
        },
    }


def build_confusion(results: list[CellResult], anchors: AnchorSet) -> list[dict[str, Any]]:
    """single/multi 오답 표본에서 (기대 leg → 실제 leg 또는 ∅) 쌍 빈도 — **leg 단위**로 센다.

    [F1-5] expectedLegs[0] 만 보면 multi 셀의 "첫 leg 는 정답, 둘째 leg 는 누락"이 표본 전체를
    커버로 오판해 그 실패가 통째로 사라진다(multiCoverage 는 1/2 로 정직하게 재는데 혼동 표만
    비어 보이는 모순). 표본마다 **기대 leg 별로** 미커버 여부를 확인해 미커버 기대 leg
    (accept[0] 대표)와 — 최종 legs 중 **어떤 기대 accept 에도 안 맞는** 여분 leg — 를 순서대로
    짝짓는다. 여분이 모자라면 `∅`(그 기대 leg 자체가 아예 안 나왔다는 뜻).
    """
    cells_by_id = {cell.cell_id: cell for cell in anchors.cells}
    counter: Counter[tuple[str, str]] = Counter()
    for result in results:
        cell = cells_by_id.get(result.cell_id)
        if cell is None or cell.slice_id not in ("single", "multi"):
            continue
        any_expected_accept = {value for leg in cell.expected_legs for value in leg.accept}
        for sample in result.samples:
            final_list = [canonical for canonical, _query in sample.legs]
            final_set = set(final_list)
            uncovered = [
                leg.accept[0] for leg in cell.expected_legs if not (set(leg.accept) & final_set)
            ]
            if not uncovered:
                continue  # 모든 기대 leg 가 커버됨 — 혼동 표는 오답만 센다
            extras = [canonical for canonical in final_list if canonical not in any_expected_accept]
            for expected_repr, actual in zip_longest(uncovered, extras, fillvalue="∅"):
                counter[(expected_repr, actual)] += 1
    return [
        {"expected": expected, "actual": actual, "count": count}
        for (expected, actual), count in sorted(counter.items(), key=lambda kv: -kv[1])
    ]


# [F1-4] 이 셋만 **채택** distance 를 싣는다(category_mapping.py 의 assembly 루프) — 택일(§4.4)이
# top-1 을 바꿨으면 이 이벤트가 이미 그 후 값(`nearest[i]` 갱신 후)을 낸다. `category_selected` 는
# 택일 발동 자체의 기록이라 distance 가 같은 값을 반복해 실어도 이중 계산을 막기 위해 여기서는
# 보지 않는다.
ADOPTED_DISTANCE_EVENTS = frozenset(
    {"category_repaired", "category_fallback_top1", "category_distance_override"}
)
# [F1-8] leg 이 canonical 을 못 내고 드롭된 사유 이벤트 — report.py 의 samples.csv `dropReasons`
# 칸이 이 집합을 그대로 재사용한다(정의를 두 곳에 따로 두면 드롭 사유 어휘가 갈라진다).
DROP_REASON_EVENTS = frozenset({"category_distance_rejected", "category_select_null"})


def _adopted_distance(sample: Sample, canonical: str) -> float | None:
    """표본의 **배포 자체 계측**(캡처된 이벤트)에서 채택 distance 를 읽는다 — 재구현하지 않는다.

    같은 이름의 hit 전체에서 min 을 취하면(구판) raw·query 두 앵커 중 채택되지 않은 쪽(예: query
    가 채택됐는데 raw 가 우연히 더 가까움)이 섞여 실제보다 낙관적인 값이 나온다(F1-2 리뷰 재현).
    이벤트가 없는 표본(exact match — 거리 개념 자체가 없다)은 호출부가 분포에서 제외한다.
    """
    for event in sample.events:
        if event.get("event") in ADOPTED_DISTANCE_EVENTS and event.get("canonical") == canonical:
            distance = event.get("distance")
            return float(distance) if isinstance(distance, int | float) else None
    return None


def distance_distribution(results: list[CellResult], anchors: AnchorSet) -> dict[str, Any]:
    """채택 top-1 distance 를 정답/오답으로 갈라 중앙값·사분위를 낸다(#344 계측, report.md 절).

    exact match 표본(canonical 이 DB 검증값 그대로라 거리 개념이 없음)은 채택 이벤트가 없어
    이 분포에서 **제외**한다 — exact 매치 카운트는 `diagnostics().exactHitCount` 로 따로 본다.
    """
    cells_by_id = {cell.cell_id: cell for cell in anchors.cells}
    correct: list[float] = []
    incorrect: list[float] = []
    for result in results:
        cell = cells_by_id.get(result.cell_id)
        if cell is None or cell.slice_id != "single":
            continue
        accept = set(cell.expected_legs[0].accept)
        for sample in result.samples:
            if not sample.legs:
                continue
            top1_canonical = sample.legs[0][0]
            adopted = _adopted_distance(sample, top1_canonical)
            if adopted is None:
                continue
            bucket = correct if top1_canonical in accept else incorrect
            bucket.append(adopted)

    def _summary(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {"count": 0, "median": None, "q1": None, "q3": None}
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "median": median(ordered),
            "q1": ordered[len(ordered) // 4],
            "q3": ordered[(3 * len(ordered)) // 4],
        }

    return {
        "correct": _summary(correct),
        "incorrect": _summary(incorrect),
        "note": "채택(배포 자체 계측) distance 기준 — exact match 표본(거리 개념 없음)은 제외.",
    }
