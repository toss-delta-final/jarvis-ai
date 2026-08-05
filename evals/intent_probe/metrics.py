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
from evals.intent_probe.schema import AnchorSet, CATEGORY_ACTION_GROUP, Utterance

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


def _category_action_matches(sample: Sample, utterance: Utterance, _: AnchorSet) -> bool:
    """[#84] **확정값**(`resolve_category_action` 산출)이 기대와 같은가.

    **확정값**을 보는 이유: 사용자가 겪는 동작은 가드가 고른 쪽이다. 기대치
    (`expected.categoryAction`)는 LLM 필드 이름이 아니라 **가드 확정값의 기대치**다 — 인라인
    `categoryAction` 필드는 실측으로 기각돼 프롬프트에서 제거됐지만(이득 0 · 전환 축 손해),
    carry|clear|replace 라는 **판정 이름 자체는 그대로 유효**하다.

    **carry 기대에는 예외가 하나 있다**(2차 리뷰 P2): `_SYSTEM` 의 categoryQueries 불릿이
    리파인 턴에 직전 카테고리를 leg 로 복사하라고 지시하므로 확정값이 `replace` 로 나오는데,
    그 leg 는 prior 에코라 **결과적으로 카테고리가 유지된다.** 사용자가 겪는 동작이 carry 와
    같으므로 정답으로 센다 — 그러지 않으면 축이 정상 동작을 실패로 읽는다(실측 1/32).
    """
    expected = utterance.expected.category_action
    if expected is None:
        return False
    if expected == "carry":
        return sample.resolved_category_action == "carry" or (
            sample.resolved_category_action == "replace" and sample.category_legs_echo_prior
        )
    return sample.resolved_category_action == expected


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
    # [#84] 카테고리 승계 3분기 — 기준선에 없는 신규 축이다. 기존 축과 표본을 섞지 않는다
    # (스키마가 강제): 새 셀이 기존 축의 분모를 늘리면 회귀 0 을 증명할 대조가 사라진다.
    AxisSpec(
        axis_id="categoryAction3Way",
        title="카테고리 승계 3분기",
        numerator="확정값(resolvedCategoryAction)이 기대 carry·clear·replace 와 일치한 표본 수 "
        "(carry 기대는 확정값이 replace 라도 그 leg 이 **전부** 직전 카테고리 에코면 정답으로 센다 "
        "— 프롬프트가 리파인 턴에 PRIOR_FILTERS.category 를 categoryQueries 로 복사하라고 지시해서 "
        "나오는 모양이고, 결과적으로 카테고리가 유지되므로 사용자가 겪는 동작이 carry 와 같다. "
        "에코 판정은 앵커 categoryPriorFilters(카테고리 전체·각 조각·semanticQuery)와 "
        "**정규화 후 정확 일치**다 — 부분 문자열이면 `이어폰 케이스` 같은 새 상품도 에코로 세어 "
        "카테고리가 바뀐 턴을 '유지됐다'로 읽는다)",
        denominator="카테고리 15발화 × categoryPrior 컨텍스트 × N (N=8 이면 120) "
        "— 라운드 3 이 혼합 4발화를 더해 88 → 120 이 됐다",
        predicate=_category_action_matches,
        components=("categoryCarry", "categoryClear", "categoryReplace", "categoryMixedReplace"),
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (분모 88 — 혼합 4발화 추가 전)",
        ),
    ),
    AxisSpec(
        axis_id="categoryCarry",
        title="리파인(직전 카테고리 유지)",
        numerator="resolvedCategoryAction 이 carry 인 표본 수 "
        "(**또는** replace 이면서 leg 이 **전부** 직전 카테고리 에코인 표본 — 프롬프트가 리파인 "
        "턴에 직전 카테고리를 leg 로 복사하라고 지시해서 나오는 모양이고, 카테고리는 유지된다. "
        "에코는 앵커 categoryPriorFilters 와 정규화 후 **정확 일치**일 때만 인정한다)",
        denominator="리파인 4발화 × categoryPrior 컨텍스트 × N (N=8 이면 32)",
        predicate=_category_action_matches,
        not_comparable_with=("baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",),
    ),
    AxisSpec(
        axis_id="categoryClear",
        title="카테고리-무관 리셋",
        numerator="resolvedCategoryAction 이 clear 인 표본 수",
        denominator="리셋 4발화 × categoryPrior 컨텍스트 × N (N=8 이면 32)",
        predicate=_category_action_matches,
        not_comparable_with=("baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",),
    ),
    AxisSpec(
        axis_id="categoryMixedReplace",
        title="혼합 발화(새 카테고리 + 아무거나)",
        numerator="resolvedCategoryAction 이 replace 인 표본 수 — 새 카테고리를 지목하면서 동시에 "
        "'아무거나'류 표현을 쓴 발화다. 초판(scopeFree 우선)에서는 사용자가 말한 카테고리가 통째로 "
        "버려져 무필터가 됐다(실 LLM 실측 32건 중 19건 clear)",
        denominator="혼합 4발화 × categoryPrior 컨텍스트 × N (N=8 이면 32)",
        predicate=_category_action_matches,
        not_comparable_with=("baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",),
    ),
    AxisSpec(
        axis_id="categoryReplace",
        title="카테고리 교체",
        numerator="resolvedCategoryAction 이 replace 인 표본 수",
        denominator="교체 3발화 × categoryPrior 컨텍스트 × N (N=8 이면 24)",
        predicate=_category_action_matches,
        not_comparable_with=("baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",),
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
    - `categoryScopeUnresolvedCount` (#84) — 전용 분류기가 판정하지 못한(None) 표본 수.
      분류기의 침묵률이며, 이 프로브가 3분기 축을 신뢰할 수 있는지의 근거다.
    - `categoryClearOnRefineCount` (#84) — 리파인 발화가 `clear` 로 확정됐다. 이 변경이 만들 수
      있는 **유일한 새 회귀 모양**(리파인 턴의 카테고리가 풀림)이라 정확도와 따로 센다
      (lessons 「정확도 지표만 보지 말고 실패의 모양을 갈라 센다」).

    카테고리 카운터 둘(`categoryScopeUnresolvedCount`·`categoryClearOnRefineCount`)은
    `category_action` 그룹 표본만 본다 — `reaskProductEchoCount` 가 전환 셀만 보는 것과 같은
    규약이다. 다른 그룹(장바구니·general)은 승계 가드에 닿지도 않아 섞으면 분모가 뜻을 잃는다.
    """
    echo = 0
    nulls = 0
    scope_unresolved = 0
    clear_on_refine = 0
    by_id = _utterances_by_id(anchors)
    for result in results:
        if result.group == CATEGORY_ACTION_GROUP:
            utterance = by_id.get(result.utterance_id)
            expected = utterance.expected.category_action if utterance else None
            for sample in result.samples:
                if sample.scope_free is None:
                    scope_unresolved += 1
                if expected == "carry" and sample.resolved_category_action == "clear":
                    clear_on_refine += 1
            continue
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
        "categoryScopeUnresolvedCount": scope_unresolved,
        "categoryClearOnRefineCount": clear_on_refine,
        "definition": {
            "reaskProductEchoCount": "전환 셀에서 cart.productId 가 되물음 상품과 같은 표본 수 "
            "— 사용자가 고르지 않은 옵션으로 옛 상품이 담기는 위험한 실패",
            "productIdNullCount": "전환 셀에서 cart_add 인데 productId 가 null 인 표본 수 "
            "— 되물음이 유지되는 안전한 퇴화",
            "categoryScopeUnresolvedCount": "전용 분류기가 판정하지 못한(None) 표본 수 "
            "— 호출 실패·비 JSON·비불리언 산출을 합친 침묵률",
            "categoryClearOnRefineCount": "리파인 기대 셀에서 확정값이 clear 로 나온 표본 수 "
            "— 사용자가 좁혀 둔 카테고리가 풀리는 이 변경의 유일한 새 회귀 모양",
        },
    }


def issue240_line(axes: dict[str, AxisResult]) -> str:
    """#240 코멘트와 같은 축 순서의 한 줄 요약."""
    return "/".join(str(axes[axis_id].numerator) for axis_id in ISSUE_240_AXIS_ORDER)
