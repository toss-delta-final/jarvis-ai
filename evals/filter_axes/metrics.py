"""필터 추출 축별 분해 지표 순수 함수(#334).

전부 파일 I/O를 하지 않는다 — 축 정의는 파라미터로 주입받은 `spec`(``spec.load_axes_spec``의
반환값)만 쓴다. 기존 `evals/metrics/metrics.py::filter_accuracy`(합집합 분모 단일값)를 대체하지
않는다 — 병행 지표다(관계는 `evals/filter_axes/README.md` 참조).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

Outcome = Literal["match", "valueMismatch", "spurious", "missing", "bothEmpty"]
_OUTCOMES: tuple[Outcome, ...] = ("match", "valueMismatch", "spurious", "missing", "bothEmpty")


def _is_set(value: object) -> bool:
    """값이 "설정됨"인가 — 빈 컨테이너/빈 문자열은 미설정, `0`은 설정(`decompose._filter_axes`와 동일 규칙)."""
    return value not in (None, [], {}, "")


def _normalize_string(value: object) -> str | None:
    if not _is_set(value):
        return None
    return unicodedata.normalize("NFC", str(value)).strip().casefold()


def _normalize_string_list_sorted(value: object) -> tuple[str, ...] | None:
    if not _is_set(value):
        return None
    items = [value] if isinstance(value, str) else list(value)  # type: ignore[arg-type]
    normalized = sorted(_normalize_string(item) or "" for item in items)
    return tuple(normalized)


def _normalize_numeric(value: object) -> float | None:
    if not _is_set(value):
        return None
    return float(value)  # type: ignore[arg-type]


def _normalize_boolean(value: object) -> bool | None:
    if not _is_set(value):
        return None
    return bool(value)


def _normalize_attr_conditions(value: object) -> tuple[tuple[str, str], ...] | None:
    if not _is_set(value):
        return None
    items = {
        (_normalize_string(key) or ""): (_normalize_string(val) or "")
        for key, val in value.items()  # type: ignore[union-attr]
    }
    return tuple(sorted(items.items()))


_NORMALIZERS = {
    "string": _normalize_string,
    "stringListSorted": _normalize_string_list_sorted,
    "numeric": _normalize_numeric,
    "boolean": _normalize_boolean,
    "attrConditionsDict": _normalize_attr_conditions,
}


def _normalize_value(value: object, normalization: str) -> object:
    try:
        normalizer = _NORMALIZERS[normalization]
    except KeyError as exc:
        raise ValueError(f"알 수 없는 정규화 타입: {normalization}") from exc
    return normalizer(value)


def _keyword_axis_outcome(
    axis_def: Mapping[str, Any],
    expected: Mapping[str, object],
    actual: Mapping[str, object],
) -> Outcome:
    """keyword 특칙 — keyword↔semanticQuery 는 존재만 서로 흡수하고, 값 일치는 **같은 필드끼리만**
    본다(keyword↔keyword 또는 semanticQuery↔semanticQuery). 교차 필드(정답 keyword vs 예측
    semanticQuery 등)는 문자열이 같아도 valueMismatch다 — 존재 흡수와 값 일치는 별개 질문이다.

    semanticQuery만 비교하는 조건이 없으면 `axis_outcome("keyword", X, X)`가 X에 keyword가
    없고 semanticQuery만 있을 때 자기 자신과도 match가 아닌 비반사성 버그가 된다(#334 리뷰 R3-1)
    — decompose의 semantic_query 폴백(절대 비지 않음, `decompose.py` 참조)이 실제로 이 경로를
    자주 태운다.
    """
    keyword_field, semantic_field = axis_def["fields"]
    expected_present = _is_set(expected.get(keyword_field)) or _is_set(expected.get(semantic_field))
    actual_present = _is_set(actual.get(keyword_field)) or _is_set(actual.get(semantic_field))
    if not expected_present and not actual_present:
        return "bothEmpty"
    if expected_present and not actual_present:
        return "missing"
    if not expected_present and actual_present:
        return "spurious"
    keyword_match = (
        _is_set(expected.get(keyword_field))
        and _is_set(actual.get(keyword_field))
        and _normalize_string(expected[keyword_field]) == _normalize_string(actual[keyword_field])
    )
    semantic_match = (
        _is_set(expected.get(semantic_field))
        and _is_set(actual.get(semantic_field))
        and _normalize_string(expected[semantic_field]) == _normalize_string(actual[semantic_field])
    )
    return "match" if keyword_match or semantic_match else "valueMismatch"


def axis_outcome(
    axis: str,
    expected: Mapping[str, object],
    actual: Mapping[str, object],
    spec: Mapping[str, Any],
) -> Outcome:
    """정답·예측 필터 한 쌍에서 축 하나의 결과를 판정한다.

    spurious = 과추출(정답엔 없는데 예측에 있음), missing = 소추출(정답엔 있는데 예측에 없음).
    """
    axis_def = spec["axes"][axis]
    if axis_def.get("special") == "keywordSemanticQueryAlias":
        return _keyword_axis_outcome(axis_def, expected, actual)
    (field,) = axis_def["fields"]
    normalization = axis_def["normalization"]
    expected_raw = expected.get(field)
    actual_raw = actual.get(field)
    expected_set = _is_set(expected_raw)
    actual_set = _is_set(actual_raw)
    if not expected_set and not actual_set:
        return "bothEmpty"
    if expected_set and not actual_set:
        return "missing"
    if not expected_set and actual_set:
        return "spurious"
    matched = _normalize_value(expected_raw, normalization) == _normalize_value(
        actual_raw, normalization
    )
    return "match" if matched else "valueMismatch"


def case_axis_outcomes(
    expected_filters: Mapping[str, object],
    actual_filters: Mapping[str, object],
    spec: Mapping[str, Any],
) -> dict[str, Outcome]:
    """케이스 1건의 evaluated 축 전부에 대한 판정(bothEmpty 포함 — 집계에서 걸러낸다)."""
    return {
        axis: axis_outcome(axis, expected_filters, actual_filters, spec)
        for axis, axis_def in spec["axes"].items()
        if axis_def.get("evaluated", True)
    }


def _precision_recall_f1(
    match_numerator: int, precision_denominator: int, recall_denominator: int
) -> dict[str, float | None]:
    precision = match_numerator / precision_denominator if precision_denominator else None
    recall = match_numerator / recall_denominator if recall_denominator else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


def aggregate_axis_metrics(
    case_outcomes_list: Sequence[Mapping[str, Outcome]],
) -> dict[str, dict[str, Any]]:
    """케이스별 축 판정을 **micro** 집계한다. 분모 0은 0이 아니라 `None`으로 남긴다."""
    axes = sorted({axis for case_outcomes in case_outcomes_list for axis in case_outcomes})
    result: dict[str, dict[str, Any]] = {}
    for axis in axes:
        counts: dict[str, int] = dict.fromkeys(_OUTCOMES, 0)
        for case_outcomes in case_outcomes_list:
            outcome = case_outcomes.get(axis)
            if outcome is not None:
                counts[outcome] += 1
        match, mismatch, spurious, missing = (
            counts["match"],
            counts["valueMismatch"],
            counts["spurious"],
            counts["missing"],
        )
        support = match + mismatch + missing
        value_strict = _precision_recall_f1(match, match + mismatch + spurious, support)
        presence_numerator = match + mismatch
        presence = _precision_recall_f1(presence_numerator, presence_numerator + spurious, support)
        result[axis] = {
            "counts": counts,
            "support": support,
            "valueStrict": value_strict,
            "presence": presence,
            "definitions": {
                "support": "match + valueMismatch + missing",
                "valueStrict.precision": "match / (match + valueMismatch + spurious)",
                "valueStrict.recall": "match / (match + valueMismatch + missing)",
                "presence.precision": "(match + valueMismatch) / (match + valueMismatch + spurious)",
                "presence.recall": "(match + valueMismatch) / (match + valueMismatch + missing)",
            },
        }
    return result


def axis_presence_set(filters: Mapping[str, object], spec: Mapping[str, Any]) -> frozenset[str]:
    """값이 설정된 evaluated+비evaluated 축 이름 집합(`decompose._filter_axes`의 축 버전)."""
    present = {
        axis
        for axis, axis_def in spec["axes"].items()
        if any(_is_set(filters.get(field)) for field in axis_def["fields"])
    }
    return frozenset(present)


_INVARIANCE_NUMERIC_AXES = ("price_min", "price_max", "rating_min", "total_budget")


def judge_invariance(
    base_filters: Mapping[str, object],
    variant_filters: Mapping[str, object],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """INV 판정 — 축 존재 집합 동등 + 수치 축 값 동등. 깨진 축과 종류를 낱낱이 반환한다."""
    base_set = axis_presence_set(base_filters, spec)
    variant_set = axis_presence_set(variant_filters, spec)
    broken: list[dict[str, str]] = [
        {"axis": axis, "kind": "added"} for axis in sorted(variant_set - base_set)
    ]
    broken.extend({"axis": axis, "kind": "removed"} for axis in sorted(base_set - variant_set))
    for axis in _INVARIANCE_NUMERIC_AXES:
        if axis not in base_set or axis not in variant_set:
            continue
        (field,) = spec["axes"][axis]["fields"]
        normalization = spec["axes"][axis]["normalization"]
        base_value = _normalize_value(base_filters.get(field), normalization)
        variant_value = _normalize_value(variant_filters.get(field), normalization)
        if base_value != variant_value:
            broken.append({"axis": axis, "kind": "valueChanged"})
    broken.sort(key=lambda item: (item["axis"], item["kind"]))
    return {"ok": not broken, "brokenAxes": broken}


def judge_direction(
    base_filters: Mapping[str, object],
    variant_filters: Mapping[str, object],
    expected_new_axes: Iterable[str],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """DIR 판정 — variant 축 집합이 base 를 덮고, expected_new_axes 가 variant 에 전부 있는지."""
    base_set = axis_presence_set(base_filters, spec)
    variant_set = axis_presence_set(variant_filters, spec)
    lost_axes = sorted(base_set - variant_set)
    missing_new_axes = sorted(set(expected_new_axes) - variant_set)
    violated = sorted(set(lost_axes) | set(missing_new_axes))
    return {
        "ok": not violated,
        "violatedAxes": violated,
        "lostAxes": lost_axes,
        "missingNewAxes": missing_new_axes,
    }


def judge_profile_leak(
    guest_filters: Mapping[str, object],
    member_filters: Mapping[str, object],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """회원/게스트 대조(#119) — member 에만 있는 축이 하나라도 있으면 leak.

    진상위집합(guest_set < member_set) 조건은 false negative 를 낸다 — member 가 guest 축
    하나를 잃으면서 다른 축을 새로 얻으면(예: guest={keyword,category}, member={keyword,
    price_max}) 진상위집합이 아니게 돼 leak 을 놓친다. member 에만 존재하는 축은 그 자체가
    프로필 하드필터 승격 신호이므로 `member_set - guest_set` 이 비어 있지 않은지만 본다.
    `guest_set - member_set`(member 가 잃은 축)은 leak 판정에는 참여하지 않고 `lostAxes` 로만
    정보 제공한다(#334 리뷰 F1).
    """
    guest_set = axis_presence_set(guest_filters, spec)
    member_set = axis_presence_set(member_filters, spec)
    leaked_axes = sorted(member_set - guest_set)
    lost_axes = sorted(guest_set - member_set)
    return {"leak": bool(leaked_axes), "leakedAxes": leaked_axes, "lostAxes": lost_axes}


def judge_candidate_subset(base_ids: Iterable[int], variant_ids: Iterable[int]) -> dict[str, Any]:
    """DIR 후보 집합 부분집합 검사 — 제약을 더한 variant 후보는 base 후보의 부분집합이어야 한다."""
    base_set = set(base_ids)
    variant_set = set(variant_ids)
    extra_ids = sorted(variant_set - base_set)
    return {"ok": not extra_ids, "extraIds": extra_ids}
