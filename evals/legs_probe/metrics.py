"""leg-그룹 매칭 규칙과 축 정의·채점 (#332).

정의 문장을 **코드 옆 데이터로** 둔다 — `AxisResult` 는 산출물(`results.json`·`report.md`)에
그대로 실린다. 숫자가 정의 없이 돌아다니면 #234·#240 처럼 같은 이름의 지표가 다른 뜻으로
비교되는 사고가 다시 난다(#260 §4, `evals/README.md` 규약 8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.agents.buyer.recommendation.decompose import normalize_category_token
from app.agents.buyer.recommendation.state import CategoryQuery
from evals.legs_probe.runner import CellResult, Sample
from evals.legs_probe.schema import Anchor, AnchorSet, CoverageGroup

SLICES = ("single", "conditions", "situational", "purpose", "multi")
# [§3] confirmatory 사전 등록 — primary 는 전체 + 이 두 슬라이스(α 보정 주석: 다중 비교를
# 통제하려고 슬라이스 2~3개만 confirmatory 로 승격하고 나머지는 exploratory 로 남긴다,
# evals/README.md 규약 5).
CONFIRMATORY_SLICES = ("situational", "purpose")
# 슬라이스 표본이 이 미만이면 report 가 스스로 exploratory 라벨을 단다(§3).
SLICE_SAMPLE_THRESHOLD = 40

Z_95 = 1.959963984540054  # 표준정규분포 97.5th percentile


def wilson_ci(numerator: float, denominator: int, *, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson 95% 신뢰구간. `denominator<=0` 이면 정의되지 않는다(None).

    `legCoverage` 처럼 numerator 가 정수 성공 횟수가 아니라 [0,1] 로 유계인 값들의 합인 축에도
    같은 공식을 적용한다 — 진짜 이항분포는 아니지만 `evals/README.md` 규약(모든 비율에 Wilson CI
    병기)을 따르기 위한 근사다. README 「측정 범위와 한계」에 그 사실을 명시한다.
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


# ─────────── leg-그룹 매칭 (§3) ───────────


def _synonym_matches_query(synonym: str, query: str | None) -> bool:
    """규칙 1·2 — synonym 이 leg.query 에 매칭되는가.

    공백이 있는 synonym 은 부분 문자열로(규칙 1), 공백이 없으면 통짜 일치 또는 head-token
    부분 문자열로(규칙 2) 본다. head-token 규칙의 이유는 한국어 명사구의 head 가 마지막
    토큰이기 때문이다 — 단순 부분 문자열이면 `"이어폰"` synonym 이 `"이어폰 케이스"`(다른 상품)
    leg 를 커버로 오인한다(lessons 2026-08-02 「부분 문자열 매칭은 포함 방향마다 의미가
    다르다」).
    """
    norm_query = normalize_category_token(query)
    if not norm_query:
        return False
    norm_syn = normalize_category_token(synonym)
    if " " in synonym:
        return norm_syn in norm_query
    squeezed_syn = norm_syn.replace(" ", "")
    squeezed_query = norm_query.replace(" ", "")
    if squeezed_syn == squeezed_query:
        return True
    tokens = norm_query.split()
    head_token = tokens[-1] if tokens else ""
    return bool(head_token) and norm_syn in head_token


def _synonym_matches_raw_category(synonym: str, raw_category: str | None) -> bool:
    """규칙 3 — 규칙 1·2 로 안 잡히면 raw_category 에 대해 단순 부분 문자열(정규화 후)."""
    norm_raw = normalize_category_token(raw_category)
    norm_syn = normalize_category_token(synonym)
    if not norm_raw or not norm_syn:
        return False
    return norm_syn in norm_raw


def _leg_matched_group_id(leg: CategoryQuery, groups: list[CoverageGroup]) -> str | None:
    """[R2-2] 문서 §3 의 3단계 우선순위를 그대로 지킨다 — **2-pass**로 전 그룹을 먼저
    query(규칙 1·2)로 탐색하고, 어느 그룹도 못 맞혔을 때만 전 그룹을 raw_category(규칙 3)로
    재탐색한다.

    그룹 단위로(첫 매치에서 즉시 종료) 규칙 1·2·3 을 다 시도하면, 앞 그룹이 raw_category 로
    맞고 뒤 그룹이 query(head-token)로 맞는 leg 가 **앞 그룹**으로 잘못 귀속된다 — 리뷰어
    재현: A=휴대폰, B=케이스, leg `query="휴대폰 케이스"`·`raw_category="휴대폰"` 이면 문서
    의도(query 우선)는 B 인데 그룹 단위 탐색은 A 에서 먼저 걸려 멈춘다. distinct 커버리지
    (매칭된 그룹 수)가 그 오귀속만큼 틀어진다.
    """
    for group in groups:
        if any(_synonym_matches_query(synonym, leg.query) for synonym in group.synonyms):
            return group.group_id
    for group in groups:
        if any(
            _synonym_matches_raw_category(synonym, leg.raw_category) for synonym in group.synonyms
        ):
            return group.group_id
    return None


def is_utterance_echo(leg: CategoryQuery, utterance: str) -> bool:
    """leg.query 가 발화를 그대로 복사했는가(공백 제거 후 부분 문자열) — #198 D3 marker 입력."""
    if not leg.query:
        return False
    squeezed_leg = normalize_category_token(leg.query).replace(" ", "")
    squeezed_utterance = normalize_category_token(utterance).replace(" ", "")
    return bool(squeezed_leg) and squeezed_leg in squeezed_utterance


@dataclass(frozen=True)
class LegDiagnostics:
    """leg 그룹 매칭의 판정 근거 — samples.csv 에 그대로 실려 런 재실행 없이 재집계할 수 있다."""

    matched_coverage_group_ids: tuple[str, ...]
    coverage_ratio: float | None
    over_expanded_leg_count: int
    echo_leg_count: int


def compute_leg_diagnostics(sample: Sample, anchor: Anchor) -> LegDiagnostics:
    """샘플의 leg 들을 앵커의 coverage/acceptable 그룹과 대조한다.

    leg 하나는 coverage 그룹을 먼저 시도하고, 안 맞으면 acceptable 그룹을 시도한다 — 둘 다 아니면
    과전개(over-expanded)다. echo 는 과전개 leg 중에서만 센다(§3).
    """
    expected = anchor.expected
    matched_ids: set[str] = set()
    over_expanded = 0
    echo = 0
    for leg in sample.category_queries:
        coverage_hit = _leg_matched_group_id(leg, expected.coverage_groups)
        if coverage_hit is not None:
            matched_ids.add(coverage_hit)
            continue
        acceptable_hit = _leg_matched_group_id(leg, expected.acceptable_groups)
        if acceptable_hit is not None:
            continue
        over_expanded += 1
        if is_utterance_echo(leg, anchor.utterance):
            echo += 1
    coverage_ratio = None
    if expected.coverage_groups:
        assert expected.coverage_target is not None  # 스키마가 보장
        coverage_ratio = min(1.0, len(matched_ids) / expected.coverage_target)
    return LegDiagnostics(
        matched_coverage_group_ids=tuple(sorted(matched_ids)),
        coverage_ratio=coverage_ratio,
        over_expanded_leg_count=over_expanded,
        echo_leg_count=echo,
    )


# ─────────── 축 정의·채점 (§3) ───────────


@dataclass(frozen=True)
class AxisResult:
    """축 하나의 점수. 정의를 함께 들고 다녀 숫자만 떠도는 일이 없게 한다."""

    axis_id: str
    title: str
    numerator: float
    denominator: int
    definition_numerator: str
    definition_denominator: str
    nature: str  # "confirmatory-primary" | "confirmatory-secondary" | "exploratory"

    @property
    def ratio(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    @property
    def ci95(self) -> tuple[float, float] | None:
        return wilson_ci(self.numerator, self.denominator)

    @property
    def is_exploratory_by_sample_size(self) -> bool:
        """[§3] 슬라이스 표본 임계(기본 40) 미만이면 report 가 스스로 exploratory 라벨을 단다."""
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


def _recommend_pairs(pairs: list[Pair], *, exclude_prompt_example: bool = False) -> list[Pair]:
    """[§3] 표본 필터 — intent!=recommend 는 case/legs 축 분모에서 제외한다."""
    return [
        (sample, anchor)
        for sample, anchor in pairs
        if sample.intent == "recommend"
        and (not exclude_prompt_example or not anchor.prompt_example)
    ]


def _by_slice(pairs: list[Pair], slice_name: str) -> list[Pair]:
    return [(sample, anchor) for sample, anchor in pairs if anchor.slice == slice_name]


def axis_case_accuracy(pairs: list[Pair]) -> AxisResult:
    rp = _recommend_pairs(pairs)
    numerator = sum(1 for sample, anchor in rp if sample.case == anchor.expected.case)
    return AxisResult(
        axis_id="caseAccuracy",
        title="case 판정 정확도",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="산출 case == expected.case",
        definition_denominator="recommend 표본 전부",
        nature="exploratory",
    )


def axis_case3_under_expansion(pairs: list[Pair], *, exclude_prompt_example: bool) -> AxisResult:
    """[§3] PRIMARY confirmatory — #198 `decompose_case` 로그 정의 그대로.

    [R2-3] 제외판·포함판이 **같은 axisId·같은 정의 문장**으로 직렬화되면 소비자가 두 표를
    구분할 수 없다 — #234/#240 이 밟은 "같은 이름, 다른 뜻" 사고와 같은 모양이다. axisId 를
    호출부(`score_all`)가 쓰는 map key 와 일치시키고, 분모 정의 문장에도 어느 쪽인지 명시한다.
    """
    rp = _recommend_pairs(pairs, exclude_prompt_example=exclude_prompt_example)
    denom_pairs = [(sample, anchor) for sample, anchor in rp if sample.case == 3]
    numerator = sum(1 for sample, _ in denom_pairs if len(sample.category_queries) <= 1)
    axis_id = (
        "case3UnderExpansionRate"
        if exclude_prompt_example
        else "case3UnderExpansionRateWithPromptExamples"
    )
    return AxisResult(
        axis_id=axis_id,
        title="case3 과소전개율",
        numerator=numerator,
        denominator=len(denom_pairs),
        definition_numerator="산출 case==3 ∧ len(legs)<=1",
        definition_denominator="산출 case==3 인 recommend 표본"
        + ("(promptExample 제외)" if exclude_prompt_example else "(promptExample 포함)"),
        nature="confirmatory-primary" if exclude_prompt_example else "exploratory",
    )


def axis_expected_case3_under_expansion(pairs: list[Pair]) -> AxisResult:
    rp = [
        (sample, anchor) for sample, anchor in _recommend_pairs(pairs) if anchor.expected.case == 3
    ]
    numerator = sum(1 for sample, _ in rp if len(sample.category_queries) <= 1)
    return AxisResult(
        axis_id="expectedCase3UnderExpansion",
        title="기대 case3 과소전개율(보조 시야)",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="len(legs)<=1",
        definition_denominator="expected.case==3 앵커의 recommend 표본",
        nature="exploratory",
    )


def axis_leg_coverage(pairs: list[Pair], *, exclude_prompt_example: bool) -> AxisResult:
    """[F-1] confirmatory-secondary 축이라 primary 와 같은 규칙으로 promptExample 을 뺀다.

    [R2-3] axisId 도 제외판·포함판을 구분한다 — `axis_case3_under_expansion` 과 같은 이유.
    """
    rp = [
        (s, a)
        for s, a in _recommend_pairs(pairs, exclude_prompt_example=exclude_prompt_example)
        if a.expected.coverage_groups
    ]
    numerator = sum(compute_leg_diagnostics(s, a).coverage_ratio or 0.0 for s, a in rp)
    axis_id = "legCoverage" if exclude_prompt_example else "legCoverageWithPromptExamples"
    return AxisResult(
        axis_id=axis_id,
        title="니즈 커버리지",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="Σ min(1, 커버리지그룹 distinct 매칭 수 / coverageTarget)",
        definition_denominator="coverageGroups 있는 앵커의 recommend 표본 수"
        + ("(promptExample 제외)" if exclude_prompt_example else "(promptExample 포함)"),
        nature="confirmatory-secondary" if exclude_prompt_example else "exploratory",
    )


def axis_legs_in_range(pairs: list[Pair]) -> AxisResult:
    rp = _recommend_pairs(pairs)
    numerator = sum(
        1
        for sample, anchor in rp
        if anchor.expected.legs_min <= len(sample.category_queries) <= anchor.expected.legs_max
    )
    return AxisResult(
        axis_id="legsInRangeRate",
        title="leg 수 범위 준수율",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="legsMin<=len(legs)<=legsMax",
        definition_denominator="recommend 표본 전부",
        nature="exploratory",
    )


def axis_over_expansion(pairs: list[Pair]) -> AxisResult:
    rp = [(s, a) for s, a in _recommend_pairs(pairs) if a.expected.coverage_groups]
    total_legs = 0
    over_expanded = 0
    for sample, anchor in rp:
        diag = compute_leg_diagnostics(sample, anchor)
        total_legs += len(sample.category_queries)
        over_expanded += diag.over_expanded_leg_count
    return AxisResult(
        axis_id="overExpansionRate",
        title="과전개율",
        numerator=over_expanded,
        denominator=total_legs,
        definition_numerator="커버리지∪acceptable 어느 그룹에도 안 맞는 leg 수",
        definition_denominator="산출 leg 총수(그룹 있는 앵커)",
        nature="exploratory",
    )


def axis_buy_all_accuracy(pairs: list[Pair]) -> AxisResult:
    rp = [(s, a) for s, a in _recommend_pairs(pairs) if a.expected.buy_all is not None]
    numerator = sum(1 for sample, anchor in rp if sample.buy_all == anchor.expected.buy_all)
    return AxisResult(
        axis_id="buyAllAccuracy",
        title="buyAll 정확도",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="산출 buy_all == expected.buyAll",
        definition_denominator="expected.buyAll≠null 앵커의 recommend 표본",
        nature="exploratory",
    )


def axis_total_budget_accuracy(pairs: list[Pair]) -> AxisResult:
    rp = _recommend_pairs(pairs)
    numerator = sum(
        1 for sample, anchor in rp if sample.total_budget == anchor.expected.total_budget
    )
    return AxisResult(
        axis_id="totalBudgetAccuracy",
        title="totalBudget 정확도",
        numerator=numerator,
        denominator=len(rp),
        definition_numerator="산출 total_budget == expected.totalBudget(null 포함 정확 일치)",
        definition_denominator="recommend 표본 전부",
        nature="exploratory",
    )


def score_all(results: list[CellResult], anchors: AnchorSet) -> dict[str, Any]:
    """축 전체 + 슬라이스별 병기(§3: caseAccuracy·legCoverage·case3UnderExpansionRate)."""
    pairs = _pairs(results, anchors)
    axes = {
        "caseAccuracy": axis_case_accuracy(pairs),
        "case3UnderExpansionRate": axis_case3_under_expansion(pairs, exclude_prompt_example=True),
        "case3UnderExpansionRateWithPromptExamples": axis_case3_under_expansion(
            pairs, exclude_prompt_example=False
        ),
        "expectedCase3UnderExpansion": axis_expected_case3_under_expansion(pairs),
        "legCoverage": axis_leg_coverage(pairs, exclude_prompt_example=True),
        "legCoverageWithPromptExamples": axis_leg_coverage(pairs, exclude_prompt_example=False),
        "legsInRangeRate": axis_legs_in_range(pairs),
        "overExpansionRate": axis_over_expansion(pairs),
        "buyAllAccuracy": axis_buy_all_accuracy(pairs),
        "totalBudgetAccuracy": axis_total_budget_accuracy(pairs),
    }
    slices = {
        "caseAccuracy": {s: axis_case_accuracy(_by_slice(pairs, s)) for s in SLICES},
        "legCoverage": {
            s: axis_leg_coverage(_by_slice(pairs, s), exclude_prompt_example=True)
            for s in SLICES
            if s != "conditions"
        },
        "case3UnderExpansionRate": {
            s: axis_case3_under_expansion(_by_slice(pairs, s), exclude_prompt_example=True)
            for s in CONFIRMATORY_SLICES
        },
    }
    return {"axes": axes, "slices": slices}


def diagnostics(results: list[CellResult], anchors: AnchorSet) -> dict[str, Any]:
    """합불이 아닌 진단 카운터(§3).

    - `nonRecommendIntentCount` — intent!=recommend 표본 수(**앵커별**). 되도록 0 이어야 한다.
    - `utteranceEchoLegCount` — 과전개 leg 중 발화를 그대로 복사한 것(#198 D3 marker 입력).
    - `emptyLegsOnCase3Count` — 산출 case==3 ∧ legs==0.
    """
    pairs = _pairs(results, anchors)
    non_recommend_by_anchor: dict[str, int] = {}
    echo_total = 0
    empty_legs_on_case3 = 0
    for sample, anchor in pairs:
        if sample.intent != "recommend":
            non_recommend_by_anchor[anchor.case_id] = (
                non_recommend_by_anchor.get(anchor.case_id, 0) + 1
            )
            continue
        diag = compute_leg_diagnostics(sample, anchor)
        echo_total += diag.echo_leg_count
        if sample.case == 3 and len(sample.category_queries) == 0:
            empty_legs_on_case3 += 1
    return {
        "nonRecommendIntentCount": dict(sorted(non_recommend_by_anchor.items())),
        "utteranceEchoLegCount": echo_total,
        "emptyLegsOnCase3Count": empty_legs_on_case3,
        "definition": {
            "nonRecommendIntentCount": "intent!=recommend 표본 수(앵커별) — decompose_case 로그가 "
            "recommend 만 남기는 것과 같은 취지, 되도록 0 이어야 한다",
            "utteranceEchoLegCount": "과전개 leg 중 공백 제거한 leg query 가 공백 제거한 발화의 "
            "부분 문자열인 것 — '발화 복사' 실패, #198 D3 marker 입력",
            "emptyLegsOnCase3Count": "산출 case==3 ∧ legs==0 인 표본 수",
        },
    }


def pair_diagnostics(results: list[CellResult], anchors: AnchorSet) -> list[dict[str, Any]]:
    """[§3] pair 진단(exploratory 표) — 합불 아님, 평균 len(legs)·legCoverage 를 나란히 인쇄."""
    by_case_id = {result.case_id: result for result in results}
    rows: list[dict[str, Any]] = []
    grouped: dict[str, list[Anchor]] = {}
    for anchor in anchors.utterances:
        if anchor.pair_id is not None:
            grouped.setdefault(anchor.pair_id, []).append(anchor)
    for pair_id in sorted(grouped):
        for anchor in grouped[pair_id]:
            result = by_case_id.get(anchor.case_id)
            samples = [s for s in (result.samples if result else []) if s.intent == "recommend"]
            avg_legs = (
                sum(len(s.category_queries) for s in samples) / len(samples) if samples else None
            )
            coverages = [
                diag.coverage_ratio
                for diag in (compute_leg_diagnostics(s, anchor) for s in samples)
                if diag.coverage_ratio is not None
            ]
            avg_coverage = sum(coverages) / len(coverages) if coverages else None
            rows.append(
                {
                    "pairId": pair_id,
                    "pairKind": anchor.pair_kind,
                    "caseId": anchor.case_id,
                    "sampleCount": len(samples),
                    "avgLegs": avg_legs,
                    "avgLegCoverage": avg_coverage,
                }
            )
    return rows


def prompt_example_diagnostics(
    results: list[CellResult], anchors: AnchorSet
) -> list[dict[str, Any]]:
    """[F-2] `promptExample==true` 앵커의 실값 표 — confirmatory 집계에서는 빠지지만

    report 는 여전히 "그 앵커에서 실제로 무슨 일이 있었는지"를 말해야 한다(#260 규약: 산출물이
    스스로 말한다). slice·recommend 표본 수·산출 case==3 ∧ legs<=1 표본 수·평균 legCoverage
    (그룹이 있는 앵커만)를 `compute_leg_diagnostics` 로 계산한다.
    """
    by_case_id = {result.case_id: result for result in results}
    rows: list[dict[str, Any]] = []
    for anchor in anchors.utterances:
        if not anchor.prompt_example:
            continue
        result = by_case_id.get(anchor.case_id)
        samples = [s for s in (result.samples if result else []) if s.intent == "recommend"]
        under_expansion_count = sum(
            1 for s in samples if s.case == 3 and len(s.category_queries) <= 1
        )
        coverages = [
            diag.coverage_ratio
            for diag in (compute_leg_diagnostics(s, anchor) for s in samples)
            if diag.coverage_ratio is not None
        ]
        avg_coverage = sum(coverages) / len(coverages) if coverages else None
        rows.append(
            {
                "caseId": anchor.case_id,
                "slice": anchor.slice,
                "recommendSampleCount": len(samples),
                "case3UnderExpansionCount": under_expansion_count,
                "avgLegCoverage": avg_coverage,
            }
        )
    return rows


def compute_baseline(anchors: AnchorSet) -> dict[str, Any]:
    """[§3] trivial baseline — "항상 leg 1개(전개 없음)" 정책. LLM 없이 결정론 계산.

    `case3UnderExpansionRate` 는 baseline 이 case 를 산출하지 않으므로(LLM 호출 자체가 없다)
    **구조적으로 1.0** 이다 — leg 이 항상 1개(<=1)라서, case==3 인 어떤 표본을 상정해도 이
    술어는 항상 참이다. `overExpansionRate` 도 정의상 0 — baseline 의 유일한 leg 은 [F-5]
    **발화 원문**을 그대로 검색어로 쓰는 것이라(README §「무엇을 재는가」와 같은 정의) 과전개가
    성립하지 않는다.
    """
    per_anchor: dict[str, float | None] = {}
    for anchor in anchors.utterances:
        expected = anchor.expected
        if expected.coverage_groups:
            assert expected.coverage_target is not None
            per_anchor[anchor.case_id] = min(
                1.0, anchor.baseline_groups_hit / expected.coverage_target
            )
        else:
            per_anchor[anchor.case_id] = None

    def _slice_average(*, exclude_prompt_example: bool) -> dict[str, float | None]:
        per_slice: dict[str, float | None] = {}
        for slice_name in SLICES:
            values: list[float] = [
                value
                for anchor in anchors.utterances
                if anchor.slice == slice_name
                and not (exclude_prompt_example and anchor.prompt_example)
                and (value := per_anchor[anchor.case_id]) is not None
            ]
            per_slice[slice_name] = (sum(values) / len(values)) if values else None
        return per_slice

    def _overall_average(*, exclude_prompt_example: bool) -> float | None:
        values: list[float] = [
            value
            for anchor in anchors.utterances
            if not (exclude_prompt_example and anchor.prompt_example)
            and (value := per_anchor[anchor.case_id]) is not None
        ]
        return (sum(values) / len(values)) if values else None

    # [F-1] `legCoverage` 가 confirmatory-secondary 로 promptExample 을 제외하므로, "LLM vs
    # baseline" 대조가 사과-사과가 되려면 baseline 도 같은 규칙을 써야 한다. 포함판은
    # `*WithPromptExamples` 로 남겨 사후 대조를 원하면 볼 수 있게 한다.
    return {
        "policy": "항상 leg 1개(전개 없음)",
        "case3UnderExpansionRate": 1.0,
        "overExpansionRate": 0.0,
        "legCoveragePerAnchor": per_anchor,
        "legCoveragePerSlice": _slice_average(exclude_prompt_example=True),
        "legCoverageOverall": _overall_average(exclude_prompt_example=True),
        "legCoveragePerSliceWithPromptExamples": _slice_average(exclude_prompt_example=False),
        "legCoverageOverallWithPromptExamples": _overall_average(exclude_prompt_example=False),
    }
