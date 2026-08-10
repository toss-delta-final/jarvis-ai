"""축 정의와 채점.

정의 문장을 **코드 옆 데이터로** 둔다 — `AxisSpec.numerator`/`denominator` 는 산출물
(`results.json`·`report.md`)에 그대로 실린다. 숫자가 정의 없이 돌아다니면 #234·#240 처럼
같은 이름의 지표가 다른 뜻으로 비교되는 사고가 다시 난다(#260 §4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dataclasses import replace

from evals.intent_probe.runner import CellResult, Sample
from evals.intent_probe.schema import (
    AnchorSet,
    CATEGORY_ACTION_GROUP,
    NAMED_CATEGORY_GROUP,
    SCREEN_GROUP,
    Utterance,
)

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


# [#300, §4.4] 세 술어는 #118(PR #292)이 이관 전 별도 프로브에서 쓰던 `_product`/`_no_product`/
# `_not_hallucinated` 를 **원본과 한 글자도 다르지 않게** 옮긴 것이다 — 다르게 세면 #118 이 채택
# 근거로 쓴 48/48 수치와 비교가 끊긴다(그 프로브는 #300 이 이 하네스로 흡수하며 삭제했다).
# `_product` 만 intent 를 함께 본다(`cart_add` 가 아니면 cart 가 채워져 있어도 확정으로 세지
# 않는다); 나머지 둘은 intent 와 무관하게 productId 값만 본다.


def _screen_exact_matches(sample: Sample, utterance: Utterance, _: AnchorSet) -> bool:
    return (
        sample.intent == "cart_add" and sample.resolved_product_id == utterance.expected.product_id
    )


def _screen_reask_matches(sample: Sample, _: Utterance, __: AnchorSet) -> bool:
    return sample.resolved_product_id is None


def _screen_not_hallucinated_matches(sample: Sample, utterance: Utterance, _: AnchorSet) -> bool:
    return sample.resolved_product_id != utterance.expected.forbidden_product_id


_SCREEN_RULE_PREDICATES: dict[str, Predicate] = {
    "screenExact": _screen_exact_matches,
    "screenReask": _screen_reask_matches,
    "screenNotHallucinated": _screen_not_hallucinated_matches,
}


def _screen_resolution_matches(sample: Sample, utterance: Utterance, anchors: AnchorSet) -> bool:
    """[#300] `screenResolution` — 셋 중 **그 셀이 선언한 규칙**을 그 셀에 적용한다.

    `screenExact`/`screenReask`/`screenNotHallucinated` 는 서로 다른 셀 집합을 재는 축이라
    한 표본에 세 술어를 동시에 적용할 수 없다 — 그 표본의 `expected.productIdRule` 이 가리키는
    술어 하나만 쓴다(합계 축이 컴포넌트 축들의 표본을 그대로 이어 붙인 것과 같다).
    """
    predicate = _SCREEN_RULE_PREDICATES.get(utterance.expected.product_id_rule)
    return predicate(sample, utterance, anchors) if predicate is not None else False


def _condition_only_no_category_query(sample: Sample, _: Utterance, __: AnchorSet) -> bool:
    """[#344 라운드 2] 조건 전용 발화에서 decompose 가 `categoryQueries` 를 하나도 못박지
    않았는가(leg 이 0개 — `sample.category_legs` 는 `serialize_category_legs` 가 빈 리스트를
    빈 문자열로 만든다, `runner.serialize_category_legs([]) == ""`). leg 을 하나라도 내면
    불충족이다 — 그 텍스트가 임베딩 앵커로 흘러 #222 확장이 무관 카테고리로 fan-out 하는
    입구가 된다.

    [#443] 이 술어는 `_named_category_has_leg` 와 **정확히 거울**이다 — 이쪽은 "leg 0개가
    정답"(조건만 말한 턴), 저쪽은 "leg 1개 이상이 정답"(상품군을 명시한 턴). 같은 필드
    (`categoryQueries`)의 양쪽 끝을 재는 두 축이라 한쪽만 보고 프롬프트를 고치면 안 된다
    (#465) — 채택 판정은 두 축을 함께 읽는다."""
    return sample.category_legs == ""


def _named_category_has_leg(sample: Sample, _: Utterance, __: AnchorSet) -> bool:
    """[#443] 상품군을 명시한 첫 턴에서 decompose 가 `categoryQueries` leg 을 **1개 이상**
    못박았는가(`sample.category_legs != ""` — leg 이 하나라도 있으면 `serialize_category_legs`
    가 빈 문자열이 아닌 값을 낸다). leg 이 하나도 없으면 불충족이다 — 사용자가 상품군을 말했는데
    파라미터 0개 payload 로 떨어지면 #217 전개 → #222 확장 폴백이라는 불필요한 LLM 호출 1회 +
    fan-out 검색 N건이 붙는다(#443 실측: 지연 14.52s).

    [#465] 이 술어는 `_condition_only_no_category_query` 와 **정확히 거울**이다 — 저쪽은
    "leg 0개가 정답"(조건만 말한 턴), 이쪽은 "leg 1개 이상이 정답"(상품군을 명시한 턴). 같은
    필드의 양쪽 끝을 재는 두 축이라 한쪽만 보고 채택 판정을 내리면 안 된다 — 두 축을 함께
    읽는다."""
    return sample.category_legs != ""


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
    # [#300] screen 지시어 해소(#118 이관) — 기존 커밋 기준선 3종에 이 축이 없다. 채점은
    # **출고 배선의 최종값**(`resolvedProductId` = decompose 다음 `resolve_screen_reference` 를
    # 거친 값)으로 한다 — 사용자가 겪는 동작이 그것이고, #118 이 채택 근거로 쓴 48/48 도 같은
    # 정의(해소기 통과 후 값)로 잰 수치다.
    AxisSpec(
        axis_id="screenExactPick",
        title="화면 지시어 확정",
        numerator="resolvedProductId(해소기 통과 후 최종값) 가 expected.productId 와 일치한 표본 수",
        denominator="확정 4발화(screen-001·003·004·005) × 1 컨텍스트 × N (N=8 이면 32)",
        predicate=_screen_exact_matches,
        components=(),
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",
            "#118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표 — 해소기 통과 후 48/48, 프롬프트 층(SCREEN 블록 채택안·해소기 전) 27/48. screen 을 아예 안 실은 `before` 9/48 은 설계 자체가 달라 어느 쪽과도 비교하지 않는다",
        ),
    ),
    AxisSpec(
        axis_id="screenReask",
        title="화면 지시어 되물음(안전 셀)",
        numerator="resolvedProductId(해소기 통과 후 최종값) 가 None 인 표본 수(임의 확정하지 않고 "
        "되물음으로 흐른다)",
        denominator="되물음 1발화(screen-002) × 1 컨텍스트 × N (N=8 이면 8)",
        predicate=_screen_reask_matches,
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",
            "#118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표 — 해소기 통과 후 48/48, 프롬프트 층(SCREEN 블록 채택안·해소기 전) 27/48. screen 을 아예 안 실은 `before` 9/48 은 설계 자체가 달라 어느 쪽과도 비교하지 않는다",
        ),
    ),
    AxisSpec(
        axis_id="screenNoHallucination",
        title="화면 밖 id 확정 금지",
        numerator="resolvedProductId(해소기 통과 후 최종값) 가 expected.forbiddenProductId 와 다른 표본 수",
        denominator="확정금지 1발화(screen-006) × 1 컨텍스트 × N (N=8 이면 8)",
        predicate=_screen_not_hallucinated_matches,
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",
            "#118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표 — 해소기 통과 후 48/48, 프롬프트 층(SCREEN 블록 채택안·해소기 전) 27/48. screen 을 아예 안 실은 `before` 9/48 은 설계 자체가 달라 어느 쪽과도 비교하지 않는다",
        ),
    ),
    AxisSpec(
        axis_id="screenResolution",
        title="화면 지시어 해소 종합",
        numerator="screenExactPick·screenReask·screenNoHallucination 셋의 합 — 각 표본은 자신의 "
        "productIdRule(screenExact|screenReask|screenNotHallucinated)이 가리키는 술어 하나로만 채점된다",
        denominator="screen 6발화 × 1 컨텍스트 × N (N=8 이면 48) — #118 의 48/48 과 같은 분모",
        predicate=_screen_resolution_matches,
        components=("screenExactPick", "screenReask", "screenNoHallucination"),
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",
            "#118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표 — 해소기 통과 후 48/48, 프롬프트 층(SCREEN 블록 채택안·해소기 전) 27/48. screen 을 아예 안 실은 `before` 9/48 은 설계 자체가 달라 어느 쪽과도 비교하지 않는다",
        ),
    ),
    # [#344 라운드 2] 조건 전용 발화("평점 좋은 걸로 보여줘" 등, 카테고리 어휘 없이 조건만 말하는
    # 턴)가 `categoryQueries` 를 비우는 계약은 지금 프롬프트의 우연한 동작이고 코드가 강제하지
    # 않는다 — 이 축은 그 불변식을 decompose 계약 단계에서 고정한다. category_probe 의 none
    # 슬라이스(임베딩 매핑 단계)와 같은 발화 문구로 같은 현상을 서로 다른 단계에서 잰다.
    AxisSpec(
        axis_id="conditionOnlyNoCategoryQuery",
        title="조건 전용 categoryQueries 비움",
        numerator="categoryLegs(leg 원문 직렬화)가 빈 문자열인 표본 수 — decompose 가 이 발화에서 "
        "categoryQueries 를 하나도 못박지 않은 경우. [#443] 반대 방향 축 namedCategoryHasLeg "
        "(상품군을 명시한 턴은 leg 이 1개 이상이어야 정답)과 정확히 거울이다 — 같은 필드의 "
        "양쪽 끝이라 한쪽만 보고 채택 판정을 내리지 않는다(#465)",
        denominator="조건 전용 5발화 × none 컨텍스트 × N (N=8 이면 40)",
        predicate=_condition_only_no_category_query,
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-300-screen (이 축이 존재하지 않던 런)",
        ),
    ),
    # [#443] 상품군 명시 첫 턴 축 — 이 이슈의 confirmatory-primary(evals/README.md 규약 5,
    # 사전 등록) 지표다. 커밋된 기준선 어디에도 존재하지 않는 신규 축이라 v6 이하 표와 직접
    # 비교하지 않는다.
    AxisSpec(
        axis_id="namedCategoryHasLeg",
        title="상품군 명시 첫 턴 leg 산출 (confirmatory-primary)",
        numerator="categoryLegs(leg 원문 직렬화)가 빈 문자열이 **아닌** 표본 수 — decompose 가 이 "
        "발화에서 categoryQueries leg 을 1개 이상 못박은 경우. [#465] 반대 방향 축 "
        "conditionOnlyNoCategoryQuery(조건 전용 턴은 leg 이 0개여야 정답)와 정확히 거울이다 — "
        "같은 필드의 양쪽 끝이라 한쪽만 보고 채택 판정을 내리지 않는다",
        denominator="상품군 명시 6발화 × none 컨텍스트 × N (N=8 이면 48)",
        predicate=_named_category_has_leg,
        not_comparable_with=(
            "baselines/fast-2026-08-04 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-05-300-screen (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-07-430-* (이 축이 존재하지 않던 런)",
            "baselines/fast-2026-08-07-430-v6-* (이 축이 존재하지 않던 런)",
        ),
    ),
    # [#386] 찜 목록 조회 축. `wishlist_view` intent 를 신설하면서 "보여줘" 계열이
    # recommend·cart_view·wishlist_view 3파전이 됐다 — 새 규칙이 제 몫을 하는지(positive)와
    # 남의 몫을 훔치지 않는지(noSteal)를 **갈라서** 센다. 한 숫자로 합치면 어느 쪽이 무너졌는지
    # 알 수 없고, 이 이슈에서 무서운 것은 후자(기존 라우팅 회귀)다.
    AxisSpec(
        axis_id="wishlistViewPositive",
        title="찜 조회 발화 → wishlist_view",
        numerator='"내가 뭐 찜했지?"류 발화에서 intent == wishlist_view 인 표본 수',
        denominator="찜 조회 양성 3발화 × none 컨텍스트 × N (N=8 이면 24)",
        predicate=_intent_matches,
        not_comparable_with=("커밋된 모든 기준선 (wishlist_view intent 자체가 없던 런)",),
    ),
    AxisSpec(
        axis_id="wishlistViewNoSteal",
        title="찜 조회 규칙이 남의 발화를 훔치지 않음",
        numerator="음성 대조 발화에서 기대 intent(wishlist_view 가 **아닌** 값)와 일치한 표본 수 "
        '— "보여줘" 단독은 recommend, "찜한 거 담아줘"는 cart_add, 부정 발화는 조회가 아니다',
        denominator="찜 조회 음성 대조 3발화 × none 컨텍스트 × N (N=8 이면 24)",
        predicate=_intent_matches,
        not_comparable_with=("커밋된 모든 기준선 (wishlist_view intent 자체가 없던 런)",),
    ),
    AxisSpec(
        axis_id="wishlistViewRouting",
        title="찜 조회 라우팅 종합",
        numerator="wishlistViewPositive·wishlistViewNoSteal 두 축의 합",
        denominator="찜 조회 6발화 × none 컨텍스트 × N (N=8 이면 48)",
        predicate=_intent_matches,
        components=("wishlistViewPositive", "wishlistViewNoSteal"),
        not_comparable_with=("커밋된 모든 기준선 (wishlist_view intent 자체가 없던 런)",),
    ),
    # [#440] 찜 해제 대상 해소 축. 프롬프트는 이 이슈에서 안 바뀐다(#443/#465 소유) — 이 축은
    # "프롬프트 계층이 이 발화를 어디로 보내는가"의 기록이고, 실제 정답 보증은 결정론 계층
    # (`intent_guard.has_wishlist_remove_evidence` + `wishlist.py` 해소 근거 게이트)이 맡는다.
    # `wishlistViewPositive`/`NoSteal`/`Routing` 과 같은 3분할 구조를 그대로 따른다.
    AxisSpec(
        axis_id="wishlistRemovePositive",
        title="찜 해제 발화 → wishlist_remove",
        numerator='"찜한 거 빼줘"류 발화에서 intent == wishlist_remove 인 표본 수',
        denominator="찜 해제 양성 2발화 × none 컨텍스트 × N (N=8 이면 16)",
        predicate=_intent_matches,
        not_comparable_with=("커밋된 모든 기준선 (이 축이 존재하지 않던 런)",),
    ),
    AxisSpec(
        axis_id="wishlistRemoveNoSteal",
        title="찜 해제 규칙이 남의 발화를 훔치지 않음",
        numerator="음성 대조 발화에서 기대 intent(wishlist_remove 가 **아닌** 값)와 일치한 표본 수 "
        '— "찜닭 빼줘"는 음식명이라 cart_remove, "장바구니에서 빼줘"는 장바구니 삭제라 cart_remove',
        denominator="찜 해제 음성 대조 2발화 × none 컨텍스트 × N (N=8 이면 16)",
        predicate=_intent_matches,
        not_comparable_with=("커밋된 모든 기준선 (이 축이 존재하지 않던 런)",),
    ),
    AxisSpec(
        axis_id="wishlistRemoveRouting",
        title="찜 해제 라우팅 종합",
        numerator="wishlistRemovePositive·wishlistRemoveNoSteal 두 축의 합",
        denominator="찜 해제 4발화 × none 컨텍스트 × N (N=8 이면 32)",
        predicate=_intent_matches,
        components=("wishlistRemovePositive", "wishlistRemoveNoSteal"),
        not_comparable_with=("커밋된 모든 기준선 (이 축이 존재하지 않던 런)",),
    ),
    # [#285, I-25 §4.13 — 4단계] 장바구니 수량 변경 라우팅 축. combo_matrix 는 build_decompose_json
    # 으로 intent 를 강제 주입해 라우팅을 재지 않기로 결정했다(evals/combo_matrix/axes.json 의
    # cart_quantity_not_generated excludes 규칙) — 그래서 라우팅 회귀는 여기서만 잰다.
    # `wishlistViewPositive`/`NoSteal`/`Routing` 과 같은 3분할 구조를 그대로 따른다.
    AxisSpec(
        axis_id="cartQuantityPositive",
        title="수량 변경 발화 → cart_quantity",
        numerator='"3개로 바꿔줘"류 발화에서 intent == cart_quantity 인 표본 수',
        denominator="수량 변경 양성 3발화 × none 컨텍스트 × N (N=8 이면 24)",
        predicate=_intent_matches,
        not_comparable_with=("커밋된 모든 기준선 (cart_quantity intent 자체가 없던 런)",),
    ),
    AxisSpec(
        axis_id="cartQuantityNoSteal",
        title="수량 변경 규칙이 남의 발화를 훔치지 않음",
        numerator="음성 대조 발화에서 기대 intent(cart_quantity 가 **아닌** 값)와 일치한 표본 수 "
        '— "하나 더 담아줘"(합산)는 cart_add, "이어폰 빼줘"는 cart_remove, "장바구니 보여줘"는 cart_view',
        denominator="수량 변경 음성 대조 3발화 × none 컨텍스트 × N (N=8 이면 24)",
        predicate=_intent_matches,
        not_comparable_with=("커밋된 모든 기준선 (cart_quantity intent 자체가 없던 런)",),
    ),
    AxisSpec(
        axis_id="cartQuantityRouting",
        title="수량 변경 라우팅 종합",
        numerator="cartQuantityPositive·cartQuantityNoSteal 두 축의 합",
        denominator="수량 변경 6발화 × none 컨텍스트 × N (N=8 이면 48)",
        predicate=_intent_matches,
        components=("cartQuantityPositive", "cartQuantityNoSteal"),
        not_comparable_with=("커밋된 모든 기준선 (cart_quantity intent 자체가 없던 런)",),
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


def _screen_ids_for_context(anchors: AnchorSet, context_id: str) -> tuple[set[int], set[int]]:
    """(그 컨텍스트가 가리키는 screen 픽스처의 productId 집합, screenLastRecommendations 집합).

    `screenOutOfListConfirmCount` 의 "두 목록"이 이것이다 — screen 은 셀마다 다르지만
    screenLastRecommendations 는 D-4 규약상 모든 screen 컨텍스트가 같은 목록을 공유한다.
    """
    context_by_id = {context.context_id: context for context in anchors.contexts}
    screen_by_id = {screen.screen_id: screen for screen in anchors.screens}
    reco_ids = {product.product_id for product in anchors.screen_last_recommendations}
    context = context_by_id.get(context_id)
    screen = (
        screen_by_id.get(context.screen_ref)
        if context is not None and context.screen_ref is not None
        else None
    )
    screen_ids = (
        {product.product_id for product in screen.products} if screen is not None else set()
    )
    return screen_ids, reco_ids


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
    - `namedCategoryEmptyLegsCount` (#443) — 상품군 명시 첫 턴에서 leg 이 0개(`namedCategoryHasLeg`
      의 미충족 표본 수와 같다). 요인 분리의 계측기 — case 분포·상황 선행/후행 등과 교차 집계하는
      출발점이다(#443 「할 일」 1번).
    - `namedCategoryCase3Count` (#443) — 상품군 명시 첫 턴에서 산출 `case == 3`(상황·목적만 있고
      무엇을 살지는 말하지 않음)으로 나온 표본 수. #443 「할 일」 2번이 가르라고 한 요인 중
      "case=3 오분류가 공백을 유발하는가" 축의 계측기다.

    카테고리 카운터 둘(`categoryScopeUnresolvedCount`·`categoryClearOnRefineCount`)은
    `category_action` 그룹 표본만 본다 — `reaskProductEchoCount` 가 전환 셀만 보는 것과 같은
    규약이다. 다른 그룹(장바구니·general)은 승계 가드에 닿지도 않아 섞으면 분모가 뜻을 잃는다.

    [#300] screen 카운터 셋(`screenPromptLayerHitCount`·`screenResolverOverrideCount`·
    `screenOutOfListConfirmCount`)도 같은 규약으로 `screen` 그룹 표본만 본다. [#443]
    `namedCategoryEmptyLegsCount`·`namedCategoryCase3Count` 도 `named_category` 그룹 표본만 본다.
    """
    echo = 0
    nulls = 0
    scope_unresolved = 0
    clear_on_refine = 0
    screen_prompt_layer_hit = 0
    screen_resolver_override = 0
    screen_out_of_list_confirm = 0
    named_category_empty_legs = 0
    named_category_case3 = 0
    by_id = _utterances_by_id(anchors)
    for result in results:
        if result.group == NAMED_CATEGORY_GROUP:
            for sample in result.samples:
                if sample.category_legs == "":
                    named_category_empty_legs += 1
                if sample.case == 3:
                    named_category_case3 += 1
            continue
        if result.group == CATEGORY_ACTION_GROUP:
            utterance = by_id.get(result.utterance_id)
            expected = utterance.expected.category_action if utterance else None
            for sample in result.samples:
                if sample.scope_free is None:
                    scope_unresolved += 1
                if expected == "carry" and sample.resolved_category_action == "clear":
                    clear_on_refine += 1
            continue
        if result.group == SCREEN_GROUP:
            utterance = by_id.get(result.utterance_id)
            predicate = (
                _SCREEN_RULE_PREDICATES.get(utterance.expected.product_id_rule)
                if utterance is not None
                else None
            )
            screen_ids, reco_ids = _screen_ids_for_context(anchors, result.context_id)
            for sample in result.samples:
                if sample.screen_resolver_fired:
                    screen_resolver_override += 1
                # [D-5] 해소기 전 원본 decompose 산출(`sample.product_id`)만으로 같은 규칙을
                # 만족했는가 — #118 이 잰 "코드 해소기 도입 전 9/48" 과 대조하는 값이다.
                if utterance is not None and predicate is not None:
                    prompt_layer_sample = replace(sample, resolved_product_id=sample.product_id)
                    if predicate(prompt_layer_sample, utterance, anchors):
                        screen_prompt_layer_hit += 1
                if (
                    sample.resolved_product_id is not None
                    and sample.resolved_product_id not in screen_ids
                    and sample.resolved_product_id not in reco_ids
                ):
                    screen_out_of_list_confirm += 1
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
        "screenPromptLayerHitCount": screen_prompt_layer_hit,
        "screenResolverOverrideCount": screen_resolver_override,
        "screenOutOfListConfirmCount": screen_out_of_list_confirm,
        "namedCategoryEmptyLegsCount": named_category_empty_legs,
        "namedCategoryCase3Count": named_category_case3,
        "definition": {
            "reaskProductEchoCount": "전환 셀에서 cart.productId 가 되물음 상품과 같은 표본 수 "
            "— 사용자가 고르지 않은 옵션으로 옛 상품이 담기는 위험한 실패",
            "productIdNullCount": "전환 셀에서 cart_add 인데 productId 가 null 인 표본 수 "
            "— 되물음이 유지되는 안전한 퇴화",
            "categoryScopeUnresolvedCount": "전용 분류기가 판정하지 못한(None) 표본 수 "
            "— 호출 실패·비 JSON·비불리언 산출을 합친 침묵률",
            "categoryClearOnRefineCount": "리파인 기대 셀에서 확정값이 clear 로 나온 표본 수 "
            "— 사용자가 좁혀 둔 카테고리가 풀리는 이 변경의 유일한 새 회귀 모양",
            "screenPromptLayerHitCount": "해소기 전 원본 decompose 산출만으로 셀 규칙을 만족한 "
            "표본 수 — #118 이 잰 '코드 해소기 도입 전 9/48' 과 대조하는 값",
            "screenResolverOverrideCount": "해소기(resolve_screen_reference)가 발동해 productId "
            "를 확정한 표본 수(None 강제 되물음도 포함)",
            "screenOutOfListConfirmCount": "최종 productId 가 None 도 아니고 두 목록(screen ∪ "
            "screenLastRecommendations) 안에도 없는 표본 수 — 위험한 실패, 0 이어야 한다",
            "namedCategoryEmptyLegsCount": "상품군 명시 첫 턴에서 categoryLegs 가 빈 문자열인 "
            "표본 수(namedCategoryHasLeg 의 미충족 표본 수와 같다) — #443 이 재는 공백률의 요인 "
            "분리(상황 선행/후행·추상도·수식어)를 samples.csv 재집계로 지탱하는 계측기",
            "namedCategoryCase3Count": "상품군 명시 첫 턴에서 산출 case 가 3(상황·목적만 있고 "
            "무엇을 살지는 말하지 않음)으로 나온 표본 수 — #443 이 가르라고 한 공백 유발 요인 중 "
            "'case 오분류' 축의 계측기",
        },
    }


def issue240_line(axes: dict[str, AxisResult]) -> str:
    """#240 코멘트와 같은 축 순서의 한 줄 요약."""
    return "/".join(str(axes[axis_id].numerator) for axis_id in ISSUE_240_AXIS_ORDER)
