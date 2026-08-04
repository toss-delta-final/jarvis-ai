"""축 정의와 채점.

정의 문장을 **코드 옆 데이터로** 둔다 — `AxisSpec.numerator`/`denominator` 는 산출물
(`results.json`·`report.md`)에 그대로 실린다. 숫자가 정의 없이 돌아다니면 #234·#240 처럼
같은 이름의 지표가 다른 뜻으로 비교되는 사고가 다시 난다(#260 §4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evals.intent_probe.runner import CellResult, Sample
from evals.intent_probe.schema import AnchorSet, Utterance

# #240 코멘트가 쓴 8축 한 줄 요약(237/144/93/27/8/48/32/15)의 순서를 그대로 보존한다.
ISSUE_240_AXIS_ORDER = (
    "mainIntent",
    "cartControl",
    "demonstrative",
    "optionAnswer",
    "switchLegacy2",
    "orderStatus",
    "general",
    "cartAddProductIdLegacy2",
)

Predicate = Callable[[Sample, Utterance, AnchorSet], bool]


def _intent_matches(sample: Sample, utterance: Utterance, _: AnchorSet) -> bool:
    return sample.intent == utterance.expected.intent


def _option_matches(sample: Sample, utterance: Utterance, _: AnchorSet) -> bool:
    return sample.intent == "cart_add" and sample.option_id == utterance.expected.option_id


def _switched_away_from_reask(sample: Sample, _: Utterance, anchors: AnchorSet) -> bool:
    known = {product.product_id for product in anchors.last_recommendations}
    return (
        sample.intent == "cart_add"
        and sample.product_id is not None
        and sample.product_id != anchors.reask_product_id
        and sample.product_id in known
    )


def _product_in_last_recommendations(sample: Sample, _: Utterance, anchors: AnchorSet) -> bool:
    known = {product.product_id for product in anchors.last_recommendations}
    return sample.intent == "cart_add" and sample.product_id in known


@dataclass(frozen=True)
class AxisSpec:
    """축 하나의 정의. `numerator`/`denominator` 는 사람이 읽는 정의 문장이다."""

    axis_id: str
    title: str
    numerator: str
    denominator: str
    predicate: Predicate
    not_comparable_with: tuple[str, ...] = ()
    components: tuple[str, ...] = ()


AXES: tuple[AxisSpec, ...] = (
    AxisSpec(
        axis_id="mainIntent",
        title="본 표 intent",
        numerator="intent 가 기대 intent 와 일치한 표본 수 (장바구니 대조군 + 지시대명사)",
        denominator="장바구니 대조군 6발화 + 지시대명사 4발화 × 컨텍스트 3종 × N",
        predicate=_intent_matches,
        components=("cartControl", "demonstrative"),
    ),
    AxisSpec(
        axis_id="cartControl",
        title="장바구니 대조군",
        numerator="intent 가 기대 intent(cart_view 또는 cart_add)와 일치한 표본 수",
        denominator="장바구니 대조군 6발화 × 컨텍스트 3종 × N",
        predicate=_intent_matches,
    ),
    AxisSpec(
        axis_id="demonstrative",
        title="지시대명사",
        numerator="intent 가 recommend 인 표본 수",
        denominator="지시대명사 4발화 × 컨텍스트 3종 × N",
        predicate=_intent_matches,
    ),
    AxisSpec(
        axis_id="optionAnswer",
        title="옵션 답변",
        numerator="intent 가 cart_add 이고 cart.optionId 까지 기대값과 일치한 표본 수 "
        "(하나만 맞으면 오답 — 옵션을 잘못 담는 것은 실패다)",
        denominator="옵션 답변 4발화 × PENDING_CART 컨텍스트 × N",
        predicate=_option_matches,
    ),
    AxisSpec(
        axis_id="switchLegacy2",
        title="전환(#240 2발화)",
        numerator="intent 가 cart_add 이고 cart.productId 가 **되물음 상품이 아닌** "
        "LAST_RECOMMENDATIONS 안의 상품인 표본 수 — #240 정의",
        denominator="#240 이 쓴 전환 2발화(`이어폰으로 할래`·`다른 거 담아줘`) × PENDING_CART × N",
        predicate=_switched_away_from_reask,
        not_comparable_with=("cartAddProductIdLegacy2", "switchAll7", "#234 productId 표"),
    ),
    AxisSpec(
        axis_id="switchAll7",
        title="전환(#260 7발화)",
        numerator="switchLegacy2 와 같은 술어를 전환 7발화 전부에 적용한 표본 수",
        denominator="전환 7발화 × PENDING_CART 컨텍스트 × N",
        predicate=_switched_away_from_reask,
        not_comparable_with=("switchLegacy2", "#240 전환 축(분모 16)"),
    ),
    AxisSpec(
        axis_id="cartAddProductIdLegacy2",
        title="cart_add productId(#234 정의)",
        numerator="intent 가 cart_add 이고 cart.productId 가 LAST_RECOMMENDATIONS 안에 있는 "
        "표본 수 — **되물음 상품을 그대로 에코해도 정답으로 센다**. #234 정의이며 "
        "switchLegacy2 와 같은 표본을 다른 정의로 다시 센 값이다",
        denominator="switchLegacy2 와 동일 표본(전환 2발화 × PENDING_CART × N)",
        predicate=_product_in_last_recommendations,
        not_comparable_with=("switchLegacy2", "#240 전환 축"),
    ),
    AxisSpec(
        axis_id="orderStatus",
        title="order_status",
        numerator="intent 가 order_status 인 표본 수",
        denominator="order_status 2발화 × 컨텍스트 3종 × N",
        predicate=_intent_matches,
    ),
    AxisSpec(
        axis_id="general",
        title="general",
        numerator="intent 가 general 인 표본 수",
        denominator="general 2발화 × 컨텍스트 3종 × N",
        predicate=_intent_matches,
    ),
)

AXES_BY_ID = {spec.axis_id: spec for spec in AXES}


@dataclass(frozen=True)
class AxisResult:
    """축 하나의 점수. 정의를 함께 들고 다녀 숫자만 떠도는 일이 없게 한다."""

    axis_id: str
    title: str
    numerator: int
    denominator: int
    expected_denominator: int
    unfilled_sample_count: int
    definition_numerator: str
    definition_denominator: str
    not_comparable_with: tuple[str, ...] = ()
    components: tuple[str, ...] = field(default=())

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
                "components": list(self.components),
                "notComparableWith": list(self.not_comparable_with),
            },
        }


def _utterances_by_id(anchors: AnchorSet) -> dict[str, Utterance]:
    return {utterance.utterance_id: utterance for utterance in anchors.utterances}


def score_axis(
    spec: AxisSpec, results: list[CellResult], anchors: AnchorSet, *, n: int
) -> AxisResult:
    """축 하나를 센다.

    `denominator` 는 **실제로 채워진 표본 수**, `expected_denominator` 는 `셀×N` 이다.
    둘이 다르면 못 채운 셀이 있다는 뜻이라 굶은 런이 깨끗한 런으로 위장할 수 없다.
    """
    by_id = _utterances_by_id(anchors)
    numerator = 0
    denominator = 0
    expected = 0
    for result in results:
        utterance = by_id.get(result.utterance_id)
        if utterance is None or spec.axis_id not in utterance.axes:
            continue
        expected += n
        denominator += len(result.samples)
        numerator += sum(
            1 for sample in result.samples if spec.predicate(sample, utterance, anchors)
        )
    return AxisResult(
        axis_id=spec.axis_id,
        title=spec.title,
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator=spec.numerator,
        definition_denominator=spec.denominator,
        not_comparable_with=spec.not_comparable_with,
        components=spec.components,
    )


def score_all(results: list[CellResult], anchors: AnchorSet, *, n: int) -> dict[str, AxisResult]:
    return {spec.axis_id: score_axis(spec, results, anchors, n=n) for spec in AXES}


def diagnostics(results: list[CellResult], anchors: AnchorSet) -> dict[str, Any]:
    """합불이 아닌 진단 카운터 (#240 §5).

    - `reaskProductEchoCount` — 되물음 상품을 그대로 담았다. **위험한 실패**: 사용자가 고르지
      않은 옵션으로 옛 상품이 장바구니에 들어간다.
    - `productIdNullCount` — 못 고르고 null 을 냈다. 되물음이 유지되는 **안전한 퇴화**다.
    """
    echo = 0
    nulls = 0
    for result in results:
        if result.group != "switch":
            continue
        for sample in result.samples:
            if sample.intent != "cart_add":
                continue
            if sample.product_id == anchors.reask_product_id:
                echo += 1
            elif sample.product_id is None:
                nulls += 1
    return {
        "reaskProductEchoCount": echo,
        "productIdNullCount": nulls,
        "definition": {
            "reaskProductEchoCount": "전환 셀에서 cart.productId 가 되물음 상품과 같은 표본 수 "
            "— 사용자가 고르지 않은 옵션으로 옛 상품이 담기는 위험한 실패",
            "productIdNullCount": "전환 셀에서 cart_add 인데 productId 가 null 인 표본 수 "
            "— 되물음이 유지되는 안전한 퇴화",
        },
    }


def issue240_line(axes: dict[str, AxisResult]) -> str:
    """#240 코멘트와 같은 축 순서의 한 줄 요약."""
    return "/".join(str(axes[axis_id].numerator) for axis_id in ISSUE_240_AXIS_ORDER)
