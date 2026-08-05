"""축 정의와 채점 — 정의 문장을 코드 옆 데이터로 두고 산출물에 그대로 싣는다(#260/#281 규약).

REQ-REC-076 이 요구하는 것은 절대값이 아니라 **제외 순서**다. 그래서 `priorityOrderPairs` 를
본질 축으로 삼는다(§4) — 절대값이 한 칸씩 밀려도 순서가 맞으면 knapsack 은 옳게 동작한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evals.priority_probe.runner import CellResult
from evals.priority_probe.schema import FixtureSet, PriorityCell


@dataclass(frozen=True)
class MetricResult:
    metric_id: str
    title: str
    numerator: int
    denominator: int
    definition_numerator: str
    definition_denominator: str

    @property
    def ratio(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "metricId": self.metric_id,
            "title": self.title,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "ratio": self.ratio,
            "definition": {
                "numerator": self.definition_numerator,
                "denominator": self.definition_denominator,
            },
        }


def _cells_by_id(fixture: FixtureSet) -> dict[str, PriorityCell]:
    return {cell.cell_id: cell for cell in fixture.cells}


def score_signal_present(results: list[CellResult], fixture: FixtureSet) -> MetricResult:
    by_id = _cells_by_id(fixture)
    numerator = 0
    denominator = 0
    for result in results:
        cell = by_id[result.cell_id]
        for sample in result.samples:
            denominator += len(cell.needs)
            numerator += sum(1 for value in sample.priorities if value is not None)
    return MetricResult(
        metric_id="prioritySignalPresent",
        title="신호 유무",
        numerator=numerator,
        denominator=denominator,
        definition_numerator="그 니즈에 유효한 1/2/3 값이 나왔다",
        definition_denominator="니즈 수 × 셀 × N",
    )


def score_exact(results: list[CellResult], fixture: FixtureSet) -> MetricResult:
    by_id = _cells_by_id(fixture)
    numerator = 0
    denominator = 0
    for result in results:
        cell = by_id[result.cell_id]
        for sample in result.samples:
            denominator += len(cell.needs)
            numerator += sum(
                1
                for value, expected in zip(sample.priorities, cell.expected_priorities, strict=True)
                if value == expected
            )
    return MetricResult(
        metric_id="priorityExact",
        title="정확 일치(보조)",
        numerator=numerator,
        denominator=denominator,
        definition_numerator="값이 기대와 정확히 일치",
        definition_denominator="니즈 수 × 셀 × N (prioritySignalPresent 와 같은 분모)",
    )


def _ordering_pairs(cell: PriorityCell) -> list[tuple[int, int]]:
    """기대 priority 가 **다른** 니즈 쌍 — REQ-REC-076 이 요구하는 제외 순서의 단위."""
    return [
        (i, j)
        for i in range(len(cell.needs))
        for j in range(i + 1, len(cell.needs))
        if cell.expected_priorities[i] != cell.expected_priorities[j]
    ]


def score_order_pairs(results: list[CellResult], fixture: FixtureSet) -> MetricResult:
    """**본질 축.** 절대값이 한 칸씩 밀려도 순서(대소 관계)가 맞으면 knapsack 제외 순서는 옳다."""
    by_id = _cells_by_id(fixture)
    numerator = 0
    denominator = 0
    for result in results:
        cell = by_id[result.cell_id]
        pairs = _ordering_pairs(cell)
        for sample in result.samples:
            for i, j in pairs:
                denominator += 1
                value_i, value_j = sample.priorities[i], sample.priorities[j]
                if value_i is None or value_j is None:
                    continue
                expected_sign = cell.expected_priorities[i] - cell.expected_priorities[j]
                actual_sign = value_i - value_j
                if expected_sign * actual_sign > 0:
                    numerator += 1
    return MetricResult(
        metric_id="priorityOrderPairs",
        title="제외 순서 쌍(본질 축)",
        numerator=numerator,
        denominator=denominator,
        definition_numerator="기대 priority 가 다른 니즈 쌍에서 산출이 같은 대소 관계",
        definition_denominator="그런 쌍의 수 × N",
    )


def score_essential_protected(results: list[CellResult], fixture: FixtureSet) -> MetricResult:
    """기대 1(필수) 니즈가 **그 표본 안에서** 최소값 집합에 드는가 — REQ-REC-076 "1 은 최후"."""
    by_id = _cells_by_id(fixture)
    numerator = 0
    denominator = 0
    for result in results:
        cell = by_id[result.cell_id]
        essential_indices = [
            i for i, expected in enumerate(cell.expected_priorities) if expected == 1
        ]
        if not essential_indices:
            continue
        for sample in result.samples:
            produced = [value for value in sample.priorities if value is not None]
            min_value = min(produced) if produced else None
            for index in essential_indices:
                denominator += 1
                value = sample.priorities[index]
                if value is not None and min_value is not None and value == min_value:
                    numerator += 1
    return MetricResult(
        metric_id="essentialProtected",
        title="필수 니즈 보호",
        numerator=numerator,
        denominator=denominator,
        definition_numerator="기대 1 인 니즈가 산출에서 최소값 집합에 든다",
        definition_denominator="기대 1 니즈 수 × N",
    )


def score_order_pairs_by_index(results: list[CellResult], fixture: FixtureSet) -> MetricResult:
    """[TASK-3-CORRECTION-2] 보조 축 — leg **개수가 우연히 같을 때만**, 이름이 달라도 위치로
    짝지은 순서 신호가 맞았는가. 이름 매칭이 실패해도(`priorityOrderPairs` 가 낮아도) 모델이
    "몇 번째로 중요한가"의 상대 순서 자체는 지켰는지를 이름 매칭과 분리해서 본다(요구사항 §3).
    개수가 다른 표본은 분모에서 아예 빠진다(비교 불가를 0 으로 세면 거짓이 된다).
    """
    by_id = _cells_by_id(fixture)
    numerator = 0
    denominator = 0
    comparable_samples = 0
    for result in results:
        cell = by_id[result.cell_id]
        pairs = _ordering_pairs(cell)
        for sample in result.samples:
            if sample.priorities_by_index is None:
                continue
            comparable_samples += 1
            for i, j in pairs:
                denominator += 1
                value_i, value_j = sample.priorities_by_index[i], sample.priorities_by_index[j]
                if value_i is None or value_j is None:
                    continue
                expected_sign = cell.expected_priorities[i] - cell.expected_priorities[j]
                actual_sign = value_i - value_j
                if expected_sign * actual_sign > 0:
                    numerator += 1
    return MetricResult(
        metric_id="priorityOrderPairsByIndex",
        title=f"제외 순서 쌍(보조 · 위치 매칭, 비교 가능 표본 {comparable_samples}건)",
        numerator=numerator,
        denominator=denominator,
        definition_numerator="기대 priority 가 다른 니즈 쌍에서, leg 개수가 같을 때 위치로 짝지은 "
        "산출이 같은 대소 관계(이름 무시)",
        definition_denominator="leg 개수가 needs 개수와 같은 표본에서만 나온 그런 쌍의 수",
    )


def score_all(results: list[CellResult], fixture: FixtureSet) -> dict[str, MetricResult]:
    metrics = (
        score_order_pairs(results, fixture),  # 본질 축을 앞에 둔다(§4)
        score_order_pairs_by_index(results, fixture),
        score_essential_protected(results, fixture),
        score_signal_present(results, fixture),
        score_exact(results, fixture),
    )
    return {metric.metric_id: metric for metric in metrics}


def diagnostics(results: list[CellResult], raw_diagnoses: list[dict[str, Any]]) -> dict[str, Any]:
    """진단 카운터(합불 아님) — §4.

    [TASK-3-CORRECTION-2] `unparsedCount` 와 `lengthMismatchCount` 는 **상호 배타적**이다(한
    표본은 파싱 실패거나 파싱 성공 중 하나이지 둘 다일 수 없다). `nameUnmatchedCount` 는
    `lengthMismatchCount` 와 **겹칠 수 있다**(개수가 달라도 이름 매칭은 별도로 시도하므로) —
    "leg 개수 자체가 다르다"(구조)와 "이름이 안 맞는 니즈가 몇 개다"(값)는 다른 질문이다.
    `emptySignalCount` 는 최종 산출(그 표본의 모든 니즈가 None) 기준이라 위 원인들과 겹칠 수
    있다 — 원인이 아니라 **결과**를 세는 축이라는 점을 분명히 한다.
    """
    unparsed_count = sum(1 for diag in raw_diagnoses if not diag["parsed"])
    length_mismatch_count = sum(1 for diag in raw_diagnoses if diag.get("lengthMismatch"))
    name_unmatched_count = sum(diag.get("nameUnmatchedCount", 0) for diag in raw_diagnoses)
    invalid_value_count = sum(diag.get("invalidValueCount", 0) for diag in raw_diagnoses)
    empty_signal_count = sum(
        1
        for result in results
        for sample in result.samples
        if all(value is None for value in sample.priorities)
    )
    # [TASK-3-CORRECTION] `result.failures` 는 두 팔 모두 **전송 실패만** 담는다(모델 출력 문제는
    # 표본으로 넘어간다) — 그래서 이 합계가 곧 "재시도해야 했던 횟수"다. 이 값이 크면 페이서
    # 설정이 어긋났다는 신호라 표를 신뢰하면 안 된다(README 해석 규칙).
    transport_retries = sum(len(result.failures) for result in results)
    return {
        "unparsedCount": unparsed_count,
        "lengthMismatchCount": length_mismatch_count,
        "nameUnmatchedCount": name_unmatched_count,
        "invalidValueCount": invalid_value_count,
        "emptySignalCount": empty_signal_count,
        "transportRetries": transport_retries,
        "definition": {
            "unparsedCount": "원시 응답을 JSON 으로 파싱하지 못한 시도 수"
            "(lengthMismatchCount 와 상호 배타적)",
            "lengthMismatchCount": "파싱은 됐지만 산출 leg/배열 길이가 니즈 수와 다른 시도 수"
            "(unparsedCount 와 상호 배타적, 구조적 비용)",
            "nameUnmatchedCount": "정규화 후에도 어느 니즈와도 이름이 일치하는 leg 를 찾지 못한 "
            "니즈 수(시도 합산) — 인라인 전용, lengthMismatchCount 와 겹칠 수 있다(값 비용)",
            "invalidValueCount": "이름은 매칭됐지만 그 leg 의 priority 값이 int 가 아니거나 "
            "{1,2,3} 밖인 니즈 수(시도 합산, 값 비용)",
            "emptySignalCount": "그 표본의 모든 니즈가 신호 없음(None)인 표본 수 — 원인이 아니라 "
            "결과라 위 카운터들과 겹칠 수 있다",
            "transportRetries": "전송 실패(429·타임아웃·연결 오류)로 표본에 안 들어가고 재시도된 "
            "시도 수 — 이 값이 크면 페이서 설정이 어긋난 것이라 표를 신뢰하면 안 된다",
        },
    }
