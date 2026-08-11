"""축 정의·채점 + 원인 축 분해(§D11) + trivial baseline(§D12) (#380).

정의 문장을 **코드 옆 데이터로** 둔다 — `AxisResult` 는 산출물(`results.json`·`report.md`)에
그대로 실린다. 숫자가 정의 없이 돌아다니면 #234·#240 처럼 같은 이름의 지표가 다른 뜻으로
비교되는 사고가 다시 난다(#260 §4, `evals/README.md` 규약 8).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from evals.underspecified_probe.runner import (
    CellResult,
    JudgmentSettings,
    Sample,
    cause_axes_for_miss,
    nonblank_blocking_axes,
    reverdict_with_flag_off,
    reverdict_with_prior_gate,
)
from evals.underspecified_probe.schema import Anchor, AnchorSet, SLICES

GATE_SLICE = "multiturn_gate"
# [§D8] confirmatory 사전 등록 슬라이스 — 다중 비교 통제(evals/README.md 규약 5).
CONFIRMATORY_MISS_SLICES = ("no_condition", "constraint_price")
CONFIRMATORY_FALSE_ALARM_SLICES = ("what_axis", "blocking_rating")
# 슬라이스 표본이 이 미만이면 report 가 스스로 exploratory 라벨을 단다(legs_probe 와 같은 값).
SLICE_SAMPLE_THRESHOLD = 40

Z_95 = 1.959963984540054  # 표준정규분포 97.5th percentile — evals/legs_probe/metrics.py 출처와 동일


def wilson_ci(numerator: float, denominator: int, *, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson 95% 신뢰구간(`evals/legs_probe/metrics.py::wilson_ci` 와 같은 공식, 출처 그대로).

    `denominator<=0` 이면 정의되지 않는다(None).
    """
    if denominator <= 0:
        return None
    p = max(0.0, min(1.0, numerator / denominator))
    n = denominator
    denom = 1 + z**2 / n
    center = p + z**2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    low = (center - margin) / denom
    high = (center + margin) / denom
    return max(0.0, low), min(1.0, high)


@dataclass(frozen=True)
class AxisResult:
    """축 하나의 점수. 정의를 함께 들고 다녀 숫자만 떠도는 일이 없게 한다(legs_probe 와 동형)."""

    axis_id: str
    title: str
    numerator: float
    denominator: int
    definition_numerator: str
    definition_denominator: str
    # "confirmatory-primary" | "confirmatory-secondary" | "exploratory" | "invariant"
    nature: str

    @property
    def ratio(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    @property
    def ci95(self) -> tuple[float, float] | None:
        return wilson_ci(self.numerator, self.denominator)

    @property
    def is_exploratory_by_sample_size(self) -> bool:
        """[§D8] 슬라이스 표본 임계(기본 40) 미만이면 report 가 스스로 exploratory 라벨을 단다."""
        return self.denominator < SLICE_SAMPLE_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        ci = self.ci95
        return {
            "axisId": self.axis_id,
            "title": self.title,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "ratio": self.ratio,
            "ci95": list(ci) if ci is not None else None,
            "nature": self.nature,
            "belowSampleThreshold": self.is_exploratory_by_sample_size,
            "definition": {
                "numerator": self.definition_numerator,
                "denominator": self.definition_denominator,
            },
        }


Pair = tuple[Sample, Anchor]


def _pairs(results: list[CellResult], anchors: AnchorSet) -> list[Pair]:
    by_id = {anchor.case_id: anchor for anchor in anchors.utterances}
    out: list[Pair] = []
    for result in results:
        anchor = by_id[result.case_id]
        for sample in result.samples:
            out.append((sample, anchor))
    return out


def _by_slice(pairs: list[Pair], slice_name: str) -> list[Pair]:
    return [(sample, anchor) for sample, anchor in pairs if anchor.slice == slice_name]


def _recommend_pairs(pairs: list[Pair]) -> list[Pair]:
    """[F-1] confirmatory 분모를 프로덕션이 실제로 판정을 호출하는 표본으로 좁힌다.

    `app/agents/buyer/graph.py::run_buyer_turn` 은 `decision.intent` 가 `general`·`cart_view`·
    `order_status`·`cart_add`·`cart_remove`·`wishlist_add`·`wishlist_remove`·
    `wishlist_view`(#386) 인 분기에서 전부 `is_underspecified_turn` 호출 **이전에 return** 한다
    — 그 표본은 프로덕션에서 판정 함수에 도달조차 하지 않는다. 그 표본을 미탐/오탐으로 세면
    intent 라우팅 축(`evals/intent_probe` 소관)의 실패를 과소지정 판정 축의 실패로 오귀속한다.
    """
    return [(s, a) for s, a in pairs if s.intent == "recommend"]


def axis_miss_rate(pairs: list[Pair]) -> AxisResult:
    """[§D8] PRIMARY confirmatory — 이슈가 지목한 두 위험 중 코드 테스트로 절대 못 잡는 쪽
    ("플래그를 켜도 되물음이 조용히 아무 일도 하지 않는가")를 직접 잰다."""
    rp = [(s, a) for s, a in _recommend_pairs(pairs) if a.expected_reask]
    numerator = sum(1 for s, _ in rp if not s.verdict)
    return AxisResult(
        axis_id="missRate",
        title="미탐율",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="판정 False",
        definition_denominator="expectedReask=true 앵커의 recommend 표본 — 프로덕션은 "
        "intent==recommend 인 턴에서만 is_underspecified_turn 을 호출한다(F-1)",
        nature="confirmatory-primary",
    )


def axis_miss_rate_with_non_recommend_intent(pairs: list[Pair]) -> AxisResult:
    """[F-1] exploratory 포함판 — intent 필터 적용 전 정의. 비교용으로만 남긴다."""
    rp = [(s, a) for s, a in pairs if a.expected_reask]
    numerator = sum(1 for s, _ in rp if not s.verdict)
    return AxisResult(
        axis_id="missRateWithNonRecommendIntent",
        title="미탐율(비-recommend 포함, 참고용)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="판정 False",
        definition_denominator="expectedReask=true 앵커의 표본 전부(intent 무관, 포함판)",
        nature="exploratory",
    )


def axis_false_alarm_rate(pairs: list[Pair], *, include_gate_slice: bool) -> AxisResult:
    """[§D8] confirmatory-secondary(제외판) — `multiturn_gate` 는 판정 두 번째 줄이 LLM 산출과
    무관하게 항상 False 를 내므로, 분모에 넣으면 구조적 성공이 오탐율을 희석한다(legs_probe 가
    `promptExample` 을 제외한 것과 같은 규약)."""
    rp = [
        (s, a)
        for s, a in _recommend_pairs(pairs)
        if not a.expected_reask and (include_gate_slice or a.slice != GATE_SLICE)
    ]
    numerator = sum(1 for s, _ in rp if s.verdict)
    axis_id = "falseAlarmRateWithGateSlice" if include_gate_slice else "falseAlarmRate"
    return AxisResult(
        axis_id=axis_id,
        title="오탐율",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="판정 True",
        definition_denominator="expectedReask=false 앵커의 recommend 표본"
        + ("(게이트 포함)" if include_gate_slice else "(multiturn_gate 슬라이스 제외)")
        + "(F-1)",
        nature="confirmatory-secondary" if not include_gate_slice else "exploratory",
    )


def axis_false_alarm_rate_with_non_recommend_intent(pairs: list[Pair]) -> AxisResult:
    """[F-1] exploratory 포함판 — intent 필터 적용 전 정의(게이트 슬라이스는 여전히 제외).
    비교용으로만 남긴다."""
    rp = [(s, a) for s, a in pairs if not a.expected_reask and a.slice != GATE_SLICE]
    numerator = sum(1 for s, _ in rp if s.verdict)
    return AxisResult(
        axis_id="falseAlarmRateWithNonRecommendIntent",
        title="오탐율(비-recommend 포함, 참고용)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="판정 True",
        definition_denominator="expectedReask=false 표본(intent 무관, multiturn_gate 제외, 포함판)",
        nature="exploratory",
    )


def axis_judgment_accuracy(pairs: list[Pair], *, include_gate_slice: bool) -> AxisResult:
    rp = [(s, a) for s, a in _recommend_pairs(pairs) if include_gate_slice or a.slice != GATE_SLICE]
    numerator = sum(1 for s, a in rp if s.verdict == a.expected_reask)
    axis_id = "judgmentAccuracyWithGateSlice" if include_gate_slice else "judgmentAccuracy"
    return AxisResult(
        axis_id=axis_id,
        title="판정 정확도",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="판정 == expectedReask",
        definition_denominator="recommend 표본"
        + ("(게이트 포함)" if include_gate_slice else "(게이트 제외)")
        + "(F-1)",
        nature="exploratory",
    )


def axis_miss_rate_under_expansion_assumption(pairs: list[Pair]) -> AxisResult:
    """[§D10.3] exploratory — 전개(#217)가 항상 leg 을 낸다고 가정한 **상한**.

    `expectedReask=true` 앵커의 recommend 표본 중, 판정이 이미 False(미탐)이거나 판정이 True
    라도 `detect_expansion_need` 가 전개 사유를 돌려주면(=프로덕션에서 그 True 판정이 전개 후
    뒤집힐 수 있음을 뜻한다) 함께 분자에 넣는다.
    """
    rp = [(s, a) for s, a in _recommend_pairs(pairs) if a.expected_reask]
    numerator = sum(
        1 for s, _ in rp if not s.verdict or (s.verdict and s.expansion_reason is not None)
    )
    return AxisResult(
        axis_id="missRateUnderExpansionAssumption",
        title="미탐율(전개 가정 상한)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="(판정 False) 또는 (판정 True ∧ detect_expansion_need 가 사유를 돌려줌)",
        definition_denominator="expectedReask=true 앵커의 recommend 표본(F-1)",
        nature="exploratory",
    )


def axis_expansion_gate_would_fire_rate(pairs: list[Pair]) -> AxisResult:
    """[§D10.3] 진단 축 — 판정 True(=이 하네스가 되물음 대상으로 판정) recommend 표본 중,
    프로덕션 `needs_expansion` 게이트가 실제로 발동했을 표본의 비율."""
    rp = [(s, a) for s, a in _recommend_pairs(pairs) if s.verdict]
    numerator = sum(1 for s, _ in rp if s.expansion_reason is not None)
    return AxisResult(
        axis_id="expansionGateWouldFireRate",
        title="전개 게이트 발동률(판정 True 표본 중)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="detect_expansion_need(...) 가 사유를 돌려준 표본",
        definition_denominator="판정 True 인 recommend 표본(F-1)",
        nature="exploratory",
    )


def _union_ok_pairs(pairs: list[Pair]) -> list[Pair]:
    """[#432, §2-6 항목2] union 단계가 실패한(`stageError` 있음) 표본은 union 축 **분모에서만**
    제외한다 — decompose 단계 표본으로는 그대로 산다(다른 축의 분모는 이 함수를 거치지 않는다)."""
    return [(s, a) for s, a in pairs if s.union is not None and s.union.ok]


def axis_miss_rate_after_expansion(pairs: list[Pair]) -> AxisResult:
    """[#432] `missRate` 의 union(전개 후) 대응판 — **분모 정의는 `missRate` 와 동일**
    (expectedReask=true 앵커의 recommend 표본), union 단계가 성공한 표본으로만 좁힌다."""
    rp = _union_ok_pairs([(s, a) for s, a in _recommend_pairs(pairs) if a.expected_reask])
    numerator = sum(1 for s, _ in rp if not s.union.verdict)
    return AxisResult(
        axis_id="missRateAfterExpansion",
        title="미탐율(전개 후, union)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="union 판정 False",
        definition_denominator="missRate 와 동일(expectedReask=true 앵커의 recommend 표본) — "
        "union 단계 실패 표본은 제외(§2-6 항목2)",
        nature="exploratory",
    )


def axis_false_alarm_rate_after_expansion(pairs: list[Pair]) -> AxisResult:
    """[#432] `falseAlarmRate` 의 union 대응판 — 분모 정의는 `falseAlarmRate` 와 동일."""
    rp = _union_ok_pairs(
        [
            (s, a)
            for s, a in _recommend_pairs(pairs)
            if not a.expected_reask and a.slice != GATE_SLICE
        ]
    )
    numerator = sum(1 for s, _ in rp if s.union.verdict)
    return AxisResult(
        axis_id="falseAlarmRateAfterExpansion",
        title="오탐율(전개 후, union)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="union 판정 True",
        definition_denominator="falseAlarmRate 와 동일(expectedReask=false 앵커의 recommend 표본, "
        "multiturn_gate 제외) — union 단계 실패 표본은 제외(§2-6 항목2)",
        nature="exploratory",
    )


def axis_expansion_suppression_rate(pairs: list[Pair]) -> AxisResult:
    """[#432] 전개가 되물음을 얼마나 꺼뜨리는가 — decompose 판정 True 였다가 union 판정 False 로
    뒤집힌 비율. #432 이슈가 지목한 핵심 질문의 직접 답."""
    rp = _union_ok_pairs([(s, a) for s, a in _recommend_pairs(pairs) if s.verdict])
    numerator = sum(1 for s, _ in rp if not s.union.verdict)
    return AxisResult(
        axis_id="expansionSuppressionRate",
        title="전개 억제율(decompose True → union False)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="decompose 단계 판정 True ∧ union 판정 False",
        definition_denominator="decompose 단계 판정 True 인 recommend 표본 — union 단계 실패 "
        "표본은 제외(§2-6 항목2)",
        nature="exploratory",
    )


def axis_expansion_gate_fired_rate(pairs: list[Pair]) -> AxisResult:
    """[#432, F-5 리뷰 반영] union 단계에서 `detect_expansion_need` 가 실제 `unresolved` 로
    사유를 돌려준 표본의 비율.

    **`expansionGateWouldFireRate`(가정판)의 "실측 대응물"이 아니다** — 분모가 다르다. 가정판은
    "판정 True 표본 중 몇 %가 게이트에 걸리나"를 묻고, 이 축은 "전체 recommend 표본 중 몇 %에서
    게이트가 실제로 발동하나"를 묻는다. **분모를 가정판과 맞추지 않은 이유**: 판정 True 표본은
    정의상 `category_queries` 가 비어 있고, `detect_expansion_need([], case=…, unresolved=…)`
    는 `unresolved` 값과 무관하게 `no_legs` 를 돌려준다 — 그 좁은 분모 안에서는 가정판과 실측판이
    **구조적으로 항상 같은 값**이 된다(2차 리뷰어가 분모를 맞추라고 제안했으나 반려한 근거).
    분모를 좁히면 이 축은 기존 축의 복제가 되어 새로 재는 정보가 0이 된다 — 넓은 분모라야 비로소
    D2(`mapping_failed`) 가 처음 관측된다."""
    rp = _union_ok_pairs(_recommend_pairs(pairs))
    numerator = sum(1 for s, _ in rp if s.union.expansion_reason is not None)
    return AxisResult(
        axis_id="expansionGateFiredRate",
        title="전개 게이트 실제 발동률(union)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="union 단계에서 detect_expansion_need 가 실제 unresolved 로 사유를 "
        "돌려준 표본",
        definition_denominator="recommend 표본 — union 단계 실패 표본은 제외(§2-6 항목2)",
        nature="exploratory",
    )


UNION_AXIS_BUILDERS: dict[str, Callable[[list[Pair]], AxisResult]] = {
    "missRateAfterExpansion": axis_miss_rate_after_expansion,
    "falseAlarmRateAfterExpansion": axis_false_alarm_rate_after_expansion,
    "expansionSuppressionRate": axis_expansion_suppression_rate,
    "expansionGateFiredRate": axis_expansion_gate_fired_rate,
}


def axis_flag_off_invariant(pairs: list[Pair]) -> AxisResult:
    """[§D9] `flagOffInvariant` — `underspecified_reask_enabled=False` 로 재판정. True 인 표본
    수는 0이어야 한다(`buy-under-0008` 의 실측판). LLM 콜 0(수집된 표본 재사용).

    [F-1] **전 표본 그대로 둔다** — 판정 함수의 게이트 자체(첫 줄 조기 반환)를 보는 불변식이라
    intent 와 무관하다(intent 라우팅으로 판정에 도달하는지 여부와 별개로, 함수가 호출됐다면
    항상 지켜야 하는 성질이다).
    """
    numerator = sum(1 for s, _ in pairs if reverdict_with_flag_off(s.decision, s.prior))
    return AxisResult(
        axis_id="flagOffInvariant",
        title="flag off 불변식",
        numerator=numerator,
        denominator=len(pairs),
        definition_numerator="underspecified_reask_enabled=False 로 재판정했을 때 True 인 표본 수",
        definition_denominator="전 표본(intent 무관 — 판정 함수 게이트 자체를 보는 불변식, F-1)",
        nature="invariant",
    )


def axis_prior_gate_invariant(pairs: list[Pair]) -> AxisResult:
    """[§D9] `priorGateInvariant` — `prior=ProductSearchFilters()` 로 재판정. True 인 표본 수는
    0이어야 한다(`buy-under-0006` 의 일반화). LLM 콜 0.

    [F-1] `flagOffInvariant` 와 같은 이유로 **전 표본 그대로** 둔다(intent 무관).
    """
    numerator = sum(1 for s, _ in pairs if reverdict_with_prior_gate(s.decision))
    return AxisResult(
        axis_id="priorGateInvariant",
        title="prior 게이트 불변식",
        numerator=numerator,
        denominator=len(pairs),
        definition_numerator="prior=ProductSearchFilters() 로 재판정했을 때 True 인 표본 수",
        definition_denominator="전 표본(intent 무관 — 판정 함수 게이트 자체를 보는 불변식, F-1)",
        nature="invariant",
    )


AXIS_BUILDERS: dict[str, Callable[[list[Pair]], AxisResult]] = {
    "missRate": axis_miss_rate,
    "missRateWithNonRecommendIntent": axis_miss_rate_with_non_recommend_intent,
    "falseAlarmRate": partial(axis_false_alarm_rate, include_gate_slice=False),
    "falseAlarmRateWithGateSlice": partial(axis_false_alarm_rate, include_gate_slice=True),
    "falseAlarmRateWithNonRecommendIntent": axis_false_alarm_rate_with_non_recommend_intent,
    "judgmentAccuracy": partial(axis_judgment_accuracy, include_gate_slice=False),
    "judgmentAccuracyWithGateSlice": partial(axis_judgment_accuracy, include_gate_slice=True),
    "missRateUnderExpansionAssumption": axis_miss_rate_under_expansion_assumption,
    "expansionGateWouldFireRate": axis_expansion_gate_would_fire_rate,
    "flagOffInvariant": axis_flag_off_invariant,
    "priorGateInvariant": axis_prior_gate_invariant,
}


def score_all(
    results: list[CellResult], anchors: AnchorSet, *, union_enabled: bool = False
) -> dict[str, Any]:
    """축 전체 + 슬라이스별 병기(§D8: "모든 축은 슬라이스별로 분자·분모·비율·CI95 를 병기").

    [#432] `union_enabled=True` 일 때만 `UNION_AXIS_BUILDERS` 를 함께 채점한다 — 기본(off)
    산출물 형상은 그대로 얼어 있다(#433 이 굳힌 6판과 계속 비교 가능해야 한다)."""
    pairs = _pairs(results, anchors)
    builders = dict(AXIS_BUILDERS)
    if union_enabled:
        builders.update(UNION_AXIS_BUILDERS)
    axes = {axis_id: builder(pairs) for axis_id, builder in builders.items()}
    slices = {
        axis_id: {slice_name: builder(_by_slice(pairs, slice_name)) for slice_name in SLICES}
        for axis_id, builder in builders.items()
    }
    return {"axes": axes, "slices": slices}


def diagnostics(
    results: list[CellResult], anchors: AnchorSet, *, union_enabled: bool = False
) -> dict[str, Any]:
    """[§D10.2, F-1] 합불이 아닌 진단 카운터.

    `categoryEchoWithoutQueriesCount` — `filters.category` 가 비어 있지 않은데 `categoryQueries`
    는 빈 표본 수. 이 하네스는 decompose 만 부르므로(§D2) `filters.category` 는 LLM 의 `filters`
    JSON 스키마에 그 키가 아예 없어 구조적으로 항상 빈다 — 0 이면 "이 데이터셋에서 이 괴리는
    공허하다"고 README 가 말할 수 있다(§D10 항목 2).

    `nonRecommendIntentCount` — `intent != "recommend"` 표본 수(앵커별·intent별). 프로덕션은
    이 표본에서 판정 함수에 도달하지 않으므로 confirmatory 축 분모에서 빠진다(F-1) — 그 노출
    크기를 여기서 수치로 남긴다. 그 실패는 intent 라우팅 축(`evals/intent_probe`)의 소관이다.
    """
    pairs = _pairs(results, anchors)
    category_echo_without_queries = sum(
        1
        for sample, _ in pairs
        if sample.decision.filters.category and not sample.decision.category_queries
    )
    non_recommend_by_case: dict[str, dict[str, int]] = {}
    for sample, anchor in pairs:
        if sample.intent == "recommend":
            continue
        per_intent = non_recommend_by_case.setdefault(anchor.case_id, {})
        per_intent[sample.intent] = per_intent.get(sample.intent, 0) + 1
    payload = {
        "categoryEchoWithoutQueriesCount": category_echo_without_queries,
        "nonRecommendIntentCount": {
            case_id: dict(sorted(counts.items()))
            for case_id, counts in sorted(non_recommend_by_case.items())
        },
        "definition": {
            "categoryEchoWithoutQueriesCount": "filters.category 가 비어 있지 않은데 "
            "categoryQueries 는 빈 표본 수 — §D10 항목 2(프로덕션이 필터를 덮어쓰는 괴리)의 "
            "노출 크기",
            "nonRecommendIntentCount": "intent != 'recommend' 표본 수(앵커별·intent별) — "
            "프로덕션은 이 표본에서 is_underspecified_turn 에 도달하지 않는다(F-1). confirmatory "
            "축 분모에서 제외된 표본의 노출 크기이며, 그 실패는 intent 라우팅 축"
            "(evals/intent_probe)의 소관이다.",
        },
    }
    if union_enabled:
        # [F-4, 리뷰 findings-432-r1] 기본(off) 산출물 형상을 그대로 얼린다(§2-1) — union 단계
        # 실패 건수는 union_enabled 일 때만 넣는다. 표본을 버리지 않고 union 축 분모에서만 뺀
        # 규모를 여기 수치로 남긴다.
        union_stage_error_count = sum(
            1 for sample, _ in pairs if sample.union is not None and not sample.union.ok
        )
        payload["unionStageErrorCount"] = union_stage_error_count
        payload["definition"]["unionStageErrorCount"] = (
            "union 단계(_prepare_recommendation)가 예외를 낸 표본 수 (#432, §2-6 항목2) — 그 "
            "표본은 decompose 단계 표본으로는 살아 있고 union 축 분모에서만 제외된다."
        )
    return payload


def _outcome(*, expected_reask: bool, verdict: bool) -> str:
    if expected_reask and verdict:
        return "hit"
    if expected_reask and not verdict:
        return "miss"
    if not expected_reask and verdict:
        return "falseAlarm"
    return "correctReject"


def sample_rows(
    results: list[CellResult], anchors: AnchorSet, judgment_settings: JudgmentSettings
) -> list[dict[str, Any]]:
    """[§D11] 표본 1건씩의 원인 축 분해 — `samples.csv`·report.md 원인 축 분해 절의 공통 소스.

    런 재실행 없이 재집계 가능해야 한다(legs_probe 규약) — 이 함수는 수집된 `Sample` 만으로
    계산하고 LLM 을 다시 부르지 않는다.
    """
    by_case_id = {anchor.case_id: anchor for anchor in anchors.utterances}
    rows: list[dict[str, Any]] = []
    for result in results:
        anchor = by_case_id[result.case_id]
        for sample in result.samples:
            outcome = _outcome(expected_reask=anchor.expected_reask, verdict=sample.verdict)
            cause_axes: list[str] = []
            blocking_axes: list[str] = []
            if outcome == "miss":
                # [§D11] 미탐 — 단일 축 소거 재판정(ablation). 조건문을 옮겨 적지 않고
                # `is_underspecified_turn` 을 다시 호출한다.
                cause_axes = cause_axes_for_miss(sample.decision, sample.prior, judgment_settings)
                blocking_axes = nonblank_blocking_axes(sample.decision)
            elif outcome == "falseAlarm":
                # [§D11] 오탐 — 앵커가 라벨한 referenceAxes 를 원인으로 싣는다(채워졌어야 할 축).
                cause_axes = list(anchor.reference_axes)
            row = {
                "caseId": sample.case_id,
                "n": sample.sample_index,
                "slice": anchor.slice,
                "intent": sample.intent,
                "case": sample.case,
                "semanticQueryIsFallback": sample.semantic_query_is_fallback,
                "semanticQuery": sample.semantic_query,
                "verdict": sample.verdict,
                "expectedReask": anchor.expected_reask,
                "outcome": outcome,
                "causeAxes": cause_axes,
                "blockingAxes": blocking_axes,
                "attrConditionAxes": sample.attr_condition_axes,
                "attrConditionsSuppressedAxes": sample.attr_conditions_suppressed_axes,
                "expansionReason": sample.expansion_reason,
                "latencyMs": sample.latency_ms,
                "dedicatedCalled": sample.dedicated_called,
                "dedicatedDecisionFlipped": sample.dedicated_disagreed,
                "dedicatedCallFailed": sample.dedicated_failed,
                "dedicatedFallbackAmbiguous": sample.dedicated_ambiguous,
            }
            # [#432] union 모드일 때만 채워진다(samples.csv 컬럼 형상은 report.write_artifacts
            # 호출부가 union_enabled 를 보고 정한다 — 여기서는 값만 싣는다).
            if sample.union is not None:
                union = sample.union
                row["unionVerdict"] = union.verdict
                row["unionOutcome"] = (
                    _outcome(expected_reask=anchor.expected_reask, verdict=union.verdict)
                    if union.ok
                    else None
                )
                row["unionMappedLegCount"] = union.mapped_leg_count
                row["unionCategoryExpanded"] = union.category_expanded
                row["unionFiltersCategory"] = union.filters_category
                row["unionExpansionReason"] = union.expansion_reason
                row["unionBlockingAxes"] = union.blocking_axes
                row["unionStageLatencyMs"] = union.stage_latency_ms
                row["unionStageError"] = union.stage_error
            rows.append(row)
    return rows


def cause_axis_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """[§D11, F-2] report.md 「원인 축 분해」 절의 집계표 소스.

    `missBlockingAxisComboCounts` — 미탐 표본의 `blockingAxes` **조합**별 집계(정렬해 `;` 로
    join 한 문자열을 키로 쓴다). `missCauseAxisCounts`(ablation 이 뒤집은 축, 단일 축 기준
    집계)만으로는 "두 축이 함께 막았다" 는 사실이 `multiple` 로 뭉개져, 실제로 어떤 축들의
    조합이 원인인지 문서 서술이 실측과 어긋나는 사고가 났다(F-2, 2차 리뷰어 발견) — 이 표는
    산출물에서 바로 조합을 읽게 한다.
    """
    miss_axis_counts: Counter[str] = Counter()
    false_alarm_axis_counts: Counter[str] = Counter()
    miss_blocking_combo_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter(row["outcome"] for row in rows)
    for row in rows:
        if row["outcome"] == "miss":
            miss_axis_counts.update(row["causeAxes"])
            combo_key = ";".join(sorted(row["blockingAxes"])) or "(없음)"
            miss_blocking_combo_counts[combo_key] += 1
        elif row["outcome"] == "falseAlarm":
            false_alarm_axis_counts.update(row["causeAxes"])
    return {
        "outcomeCounts": dict(sorted(outcome_counts.items())),
        "missCauseAxisCounts": dict(sorted(miss_axis_counts.items())),
        "missBlockingAxisComboCounts": dict(sorted(miss_blocking_combo_counts.items())),
        "falseAlarmCauseAxisCounts": dict(sorted(false_alarm_axis_counts.items())),
    }


def compute_baseline(results: list[CellResult], anchors: AnchorSet) -> dict[str, Any]:
    """[§D12, F-1] trivial baseline — "항상 reask=false"(플래그 off 동작). LLM 없이 결정론 계산.

    `missRate` 는 baseline 이 항상 False 를 내므로 **구조적으로 1.0**(expectedReask=true 앵커를
    전부 놓친다), `falseAlarmRate` 는 **정의상 0.0**(항상 False 이므로 오탐이 성립하지 않는다).
    `judgmentAccuracy` 는 baseline 과 실측이 **같은 분모**(recommend 표본, F-1)를 쓰도록
    `_recommend_pairs` 로 좁혀서 계산한다 — 분모 정의가 다른 값을 대조하면 #234/#240 사고가
    재발한다(legs_probe README `[R4-2]` 교훈).
    """
    pairs = _recommend_pairs(_pairs(results, anchors))

    def _accuracy(sub_pairs: list[Pair]) -> dict[str, Any]:
        numerator = sum(1 for _, a in sub_pairs if not a.expected_reask)  # baseline 은 항상 False
        denominator = len(sub_pairs)
        return {
            "numerator": numerator,
            "denominator": denominator,
            "ratio": (numerator / denominator) if denominator else None,
        }

    gated_pairs = [(s, a) for s, a in pairs if a.slice != GATE_SLICE]
    return {
        "policy": "항상 reask=false(플래그 off 동작과 동일)",
        "missRate": 1.0,
        "falseAlarmRate": 0.0,
        "judgmentAccuracy": _accuracy(gated_pairs),
        "judgmentAccuracyWithGateSlice": _accuracy(pairs),
        "judgmentAccuracyPerSlice": {
            slice_name: _accuracy(_by_slice(pairs, slice_name)) for slice_name in SLICES
        },
    }
