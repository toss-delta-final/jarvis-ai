"""intent 라우팅 프로브 앵커(정답지) 스키마와 픽스처 결함 검증.

이 스키마의 검증자는 전부 **#240에서 실제로 밟은 함정**을 커밋 불가능하게 만드는 장치다.
정답지가 조금만 어긋나도 표가 재현되지 않고, 그 사실을 사람이 눈으로 잡지 못한다는 것이
이 이슈(#260)의 출발점이다 — 그래서 규칙을 문서가 아니라 스키마에 둔다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.schemas.spring import ProductSearchFilters

FIXTURE_SCHEMA_VERSION = "1.3.0"

# [#300] screen 컨텍스트 5종 — screen 픽스처(ScreenFixture) 1건과 1:1 대응한다. 카테고리
# 컨텍스트(`categoryPrior`)처럼 단일 상수가 아니라 5개인 이유는 발화마다 다른 화면 모양
# (1/3/5/9건·이름 매칭)을 재기 때문이다.
SCREEN_CONTEXT_IDS = (
    "screenSingle",
    "screenTriple",
    "screenFive",
    "screenNine",
    "screenNamed",
)
CONTEXT_IDS = (
    "none",
    "lastRecommendations",
    "pendingCart",
    "categoryPrior",
    *SCREEN_CONTEXT_IDS,
)
# [#84] 멀티턴 카테고리 승계 의도(carry/clear/replace)를 재는 그룹.
CATEGORY_ACTION_GROUP = "category_action"
CATEGORY_PRIOR_CONTEXT_ID = "categoryPrior"
# [#300] screen 지시어 해소(#118)를 재는 그룹 — #118 이 만든 두 번째 프로브(PR #292, 이후
# #300 이 이 하네스로 흡수하며 삭제했다)의 screen 셀 6종을 옮긴다.
SCREEN_GROUP = "screen"
# [#344 라운드 2] 조건 전용 발화(카테고리 어휘 없이 조건만 말하는 턴, "평점 좋은 걸로 보여줘")가
# `categoryQueries` 를 비우는 불변식을 재는 그룹 — 지금은 프롬프트의 우연한 동작이고 코드가
# 강제하지 않는다(`category_mapping._collect_expansion_leaves` docstring [#222 R2 F-2] 참조).
CONDITION_ONLY_GROUP = "condition_only"
# [#386] 찜 목록 조회(`wishlist_view`) 라우팅 축 — "보여줘" 3파전(recommend·cart_view·
# wishlist_view)에서 새 의도가 남의 것을 훔치지 않는지를 재는 그룹. 양성 3발화 + 음성 대조
# 3발화로 구성되며 음성 대조의 기대 intent 는 `wishlist_view` 가 **아닌** 값이다.
WISHLIST_VIEW_GROUP = "wishlist_view"
# [#440] 찜 해제 대상 해소(조회 ↔ 해제 인접 결합 판정) 축 — `WISHLIST_VIEW_GROUP`(#386) 과 정확히
# 같은 패턴이다. 프롬프트는 이 이슈에서 안 바뀌므로(범위 밖, #443/#465 소유) 이 셀들은 "프롬프트
# 계층이 이 발화를 어디로 보내는가"의 기록일 뿐이고, 실제 정답 보증은 결정론 계층
# (`intent_guard.has_wishlist_remove_evidence` + `wishlist.py` 해소 근거 게이트)이 맡는다.
WISHLIST_REMOVE_GROUP = "wishlist_remove"
# [#285, I-25 §4.13 — 4단계] 장바구니 수량 변경(치환) 라우팅 축 — `WISHLIST_VIEW_GROUP`(#386) 과
# 같은 패턴이다. combo_matrix 는 build_decompose_json 으로 intent 를 강제 주입해 라우팅을 재지
# 않기로 결정했으므로(evals/combo_matrix/axes.json 의 `cart_quantity_not_generated` excludes 규칙
# 참조), 라우팅 회귀는 여기서만 잰다. 양성(치환 표현 → cart_quantity)과 음성(합산·삭제·조회 —
# cart_quantity 가 남의 발화를 훔치지 않는가)을 갈라서 센다 — 가장 중요한 대조는 "하나 더
# 담아줘"(합산, cart_add) 다(패킷 함정 2 와 같은 축).
CART_QUANTITY_GROUP = "cart_quantity"
# [#443] 상품군을 명시한 첫 턴(무프라이어)에서 decompose 가 `categoryQueries` leg 을 실제로
# 채우는지 재는 그룹 — `CONDITION_ONLY_GROUP`(#344, 조건만 말하고 상품군은 없는 턴이 leg 을
# 비우는지)과 **정반대 방향**이다. 허용 컨텍스트가 `none` 하나뿐인 이유도 같다: 이 축의 정의가
# "무프라이어 컨텍스트에서 상품군을 명시한 첫 턴"이라 다른 컨텍스트(직전 추천·장바구니 등)가
# 섞이면 "첫 턴"이라는 전제가 깨진다.
NAMED_CATEGORY_GROUP = "named_category"
GROUPS = frozenset(
    {
        "cart_control",
        "demonstrative",
        "option_answer",
        "switch",
        "order_status",
        "general",
        CATEGORY_ACTION_GROUP,
        SCREEN_GROUP,
        CONDITION_ONLY_GROUP,
        WISHLIST_VIEW_GROUP,
        WISHLIST_REMOVE_GROUP,
        CART_QUANTITY_GROUP,
        NAMED_CATEGORY_GROUP,
    }
)
# [#313] group → 허용 컨텍스트 매핑. "이 축이 무엇을 재는가"의 선언 그 자체다 — 여기 한 줄이
# 없으면 그 group 은 어떤 컨텍스트도 선언할 수 없다(안전한 기본값). #84 `_category_action_group_is_isolated`
# 와 #300 `_non_screen_utterances_cannot_reference_screen_contexts` 가 각자 자기 그룹만 지키던
# 전용 검증자였고, 그 사이에 `option_answer`·`switch` 처럼 아무도 지키지 않은 그룹이 남아 있었다
# — 이 매핑을 강제하는 일반형 검증자(`Utterance._contexts_are_within_the_group_allowlist`)가 그
# 자리를 메운다.
GROUP_ALLOWED_CONTEXTS: dict[str, frozenset[str]] = {
    "cart_control": frozenset({"none", "lastRecommendations", "pendingCart"}),
    "demonstrative": frozenset({"none", "lastRecommendations", "pendingCart"}),
    "option_answer": frozenset({"pendingCart"}),
    "switch": frozenset({"pendingCart"}),
    "order_status": frozenset({"none", "lastRecommendations", "pendingCart"}),
    "general": frozenset({"none", "lastRecommendations", "pendingCart"}),
    CATEGORY_ACTION_GROUP: frozenset({CATEGORY_PRIOR_CONTEXT_ID}),
    SCREEN_GROUP: frozenset(SCREEN_CONTEXT_IDS),
    # [#344 라운드 3] 이 축의 정의가 "무프라이어(none) 컨텍스트에서 조건만 말하는 턴"이므로
    # 허용 컨텍스트는 `none` 하나뿐이다.
    CONDITION_ONLY_GROUP: frozenset({"none"}),
    # [#386] 찜 목록 조회는 지칭 해소 대상이 없어 컨텍스트가 라우팅을 가르지 않는다 —
    # `none` 하나로 둔다(`condition_only` 와 같은 이유).
    WISHLIST_VIEW_GROUP: frozenset({"none"}),
    # [#440] 찜 해제 대상 해소도 지칭 해소 대상이 없다 — `none` 하나로 둔다
    # (`wishlist_view`/`condition_only` 와 같은 이유). 실제 셀 4개도 전부 `contexts: ["none"]`
    # 이라 [라운드 1 리뷰 F2] `lastRecommendations` 는 아무도 안 쓰는 허용이었다 — 이 매핑은
    # "이 축이 무엇을 재는가"의 선언 그 자체(#313)라, 안 쓰는 값을 열어 두면 다음 사람이 그
    # 축에 다른 컨텍스트 셀을 얹어 분모를 흔들 수 있다.
    WISHLIST_REMOVE_GROUP: frozenset({"none"}),
    # [#285, I-25 §4.13] 수량 변경도 지칭 해소 대상이 없다 — `none` 하나로 둔다(`wishlist_view`/
    # `wishlist_remove` 와 같은 이유). 대상 항목 해소는 결정론 계층(`quantity.py::
    # _resolve_quantity_target`)이 장바구니 조회 결과에서 하지, decompose 컨텍스트가 가르지 않는다.
    CART_QUANTITY_GROUP: frozenset({"none"}),
    # [#443] 이 축의 정의가 "무프라이어(none) 컨텍스트에서 상품군을 명시한 첫 턴"이므로
    # 허용 컨텍스트는 `none` 하나뿐이다(`condition_only` 와 같은 이유 — [#344 라운드 3]).
    NAMED_CATEGORY_GROUP: frozenset({"none"}),
}
INTENTS = (
    "recommend",
    "cart_add",
    "cart_view",
    "order_status",
    "general",
    # [#386] 찜 조회 축을 신설하며 추가.
    "wishlist_view",
    # [#440] 찜 해제 대상 해소 축을 신설하며 추가 — `wishlist_remove`(찜 해제 양성)와
    # `cart_remove`(잠식 대조, "장바구니에서 빼줘")가 이 축의 대조 표현에 필요하다. 나머지
    # `wishlist_add` intent 는 여전히 이 프로브의 정의역 밖이다(#116/#117 별건, 여기 넣으면
    # 재지 않는 값이 정답지에 생긴다).
    "wishlist_remove",
    "cart_remove",
    # [#285, I-25 §4.13 — 4단계] 수량 변경 축을 신설하며 추가. 음성 대조에 `cart_add`·
    # `cart_remove`·`cart_view`(전부 이미 위에 있음)가 필요하다.
    "cart_quantity",
)
CATEGORY_ACTIONS = ("carry", "clear", "replace")
# [#300] screen 셀의 productIdRule 3종. 이슈 본문은 "2종"이라 했지만 실제 셀은 세 모양이다 —
# 확정(screenExact) / 비움·되물음(screenReask) / 특정 id 확정 금지(screenNotHallucinated).
# 둘로 접으면 `301 담아줘` 셀("두 목록 밖 id 를 확정하지 않는다")의 술어가 표현할 곳이 없어진다.
SCREEN_PRODUCT_ID_RULES = ("screenExact", "screenReask", "screenNotHallucinated")
# metrics.AXES 와 같은 집합 — 축을 늘리면 양쪽을 함께 고쳐야 하고, 테스트가 어긋남을 잡는다.
LEGACY_AXIS_IDS = frozenset(
    {
        "mainIntent",
        "cartControl",
        "demonstrative",
        "optionAnswer",
        "switchLegacy2",
        "switchAll7",
        "cartAddProductIdLegacy2",
        "orderStatus",
        "general",
    }
)
# [#84] 신규 축 — 커밋된 기준선(`baselines/fast-2026-08-04/`)에는 **존재하지 않는다.**
# `categoryMixedReplace`(라운드 3)는 **혼합 발화** 전용이라 `categoryReplace` 와 따로 센다 —
# 그 축의 분모(24)를 유지해야 방금 커밋한 v2 기준선과 비교가 된다(실패의 모양을 갈라 세는 규약).
CATEGORY_ACTION_AXIS_IDS = frozenset(
    {
        "categoryAction3Way",
        "categoryCarry",
        "categoryClear",
        "categoryReplace",
        "categoryMixedReplace",
    }
)
# [#300] screen 지시어 해소 축 — 커밋된 기준선(`baselines/fast-2026-08-04/`·
# `baselines/fast-2026-08-05-84/`)에는 **존재하지 않는다.**
SCREEN_AXIS_IDS = frozenset(
    {
        "screenExactPick",
        "screenReask",
        "screenNoHallucination",
        "screenResolution",
    }
)
# [#344 라운드 2] 조건 전용 발화 축 — 커밋된 기준선(`baselines/fast-2026-08-04`·
# `baselines/fast-2026-08-05-84`·`baselines/fast-2026-08-05-300-screen`)에는 **존재하지 않는다.**
CONDITION_ONLY_AXIS_IDS = frozenset({"conditionOnlyNoCategoryQuery"})
# [#386] 찜 목록 조회 축 — 커밋된 기준선 어디에도 **존재하지 않는다**(그 시점엔 intent 자체가
# 없었다). 기존 축의 분모를 늘리지 않으려고 별도 축으로 둔다:
#   · `wishlistViewPositive` — 찜 조회 발화가 실제로 wishlist_view 로 가는가
#   · `wishlistViewNoSteal`  — 새 규칙이 남의 발화를 훔치지 않는가(음성 대조)
#   · `wishlistViewRouting`  — 위 둘의 합계
WISHLIST_VIEW_AXIS_IDS = frozenset(
    {"wishlistViewPositive", "wishlistViewNoSteal", "wishlistViewRouting"}
)
# [#440] 찜 해제 대상 해소 축 — `WISHLIST_VIEW_AXIS_IDS` 와 같은 3분할이다:
#   · `wishlistRemovePositive` — 찜 해제 발화(인접 결합이 필요한 부류 포함)가 실제로
#     `wishlist_remove` 로 가는가
#   · `wishlistRemoveNoSteal`  — 음식명·시설명·다른 intent(cart_remove) 를 훔치지 않는가(음성 대조)
#   · `wishlistRemoveRouting`  — 위 둘의 합계
WISHLIST_REMOVE_AXIS_IDS = frozenset(
    {"wishlistRemovePositive", "wishlistRemoveNoSteal", "wishlistRemoveRouting"}
)
# [#285, I-25 §4.13 — 4단계] 장바구니 수량 변경 축 — `WISHLIST_VIEW_AXIS_IDS` 와 같은 3분할이다.
# 커밋된 기준선 어디에도 **존재하지 않는다**(그 시점엔 intent 자체가 없었다):
#   · `cartQuantityPositive` — 치환 표현("N개로 바꿔줘")이 실제로 cart_quantity 로 가는가
#   · `cartQuantityNoSteal`  — 합산·삭제·조회(cart_add/cart_remove/cart_view) 를 훔치지 않는가
#     (음성 대조 — 가장 중요한 대조는 "하나 더 담아줘" → cart_add, 패킷 함정 2 와 같은 축)
#   · `cartQuantityRouting`  — 위 둘의 합계
CART_QUANTITY_AXIS_IDS = frozenset(
    {"cartQuantityPositive", "cartQuantityNoSteal", "cartQuantityRouting"}
)
# [#443] 상품군 명시 첫 턴 축 — 커밋된 기준선 어디에도 **존재하지 않는다.** 반대 방향 축
# `conditionOnlyNoCategoryQuery`(#344)와 정의가 정확히 거울이다: 한쪽은 "leg 0개가 정답"
# (조건만 말한 턴), 이쪽은 "leg 1개 이상이 정답"(상품군을 명시한 턴).
NAMED_CATEGORY_AXIS_IDS = frozenset({"namedCategoryHasLeg"})
AXIS_IDS = (
    LEGACY_AXIS_IDS
    | CATEGORY_ACTION_AXIS_IDS
    | SCREEN_AXIS_IDS
    | CONDITION_ONLY_AXIS_IDS
    | WISHLIST_VIEW_AXIS_IDS
    | WISHLIST_REMOVE_AXIS_IDS
    | CART_QUANTITY_AXIS_IDS
    | NAMED_CATEGORY_AXIS_IDS
)
# [#300, F-2] productIdRule → 그 규칙이 재는 컴포넌트 축. `screenResolution`(합계 축)은 모든
# screen 발화가 공통으로 선언해야 하므로 이 맵에는 넣지 않는다 —
# `Utterance._screen_axes_match_the_rule` 이 `{매핑값, "screenResolution"}` 을 강제한다.
SCREEN_RULE_TO_AXIS = {
    "screenExact": "screenExactPick",
    "screenReask": "screenReask",
    "screenNotHallucinated": "screenNoHallucination",
}


class CamelModel(BaseModel):
    """eval 내부 모델도 앱 와이어와 같은 camelCase 별칭 규약을 쓴다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Product(CamelModel):
    """LAST_RECOMMENDATIONS 한 줄 (productId + 이름)."""

    product_id: int
    name: str = Field(min_length=1)


class Option(CamelModel):
    """PENDING_CART 되물음 옵션 한 줄."""

    option_id: int
    name: str = Field(min_length=1)


class ScreenFixture(CamelModel):
    """screen 픽스처 1건 — decompose 프롬프트에 실릴 화면 맥락(#118, api-spec §3.1)의 정답지 표현.

    `screen_id` 는 `ProbeContext.screen_ref` 가 가리키는 키다. `page_type` 은 항상 `"chat"` 이고
    (D-4) `filters` 는 그 페이지의 원시 필터값 — 표시명(`인기 상품`)은 여기 쓰지 않는다.
    `build_context_kwargs` 가 `app.schemas.chat.ScreenContext` 로 만든 뒤
    `decompose.build_screen_prompt(labels=settings.screen_page_type_labels)` 를 태워 **프로덕션
    투영 경로 그대로** 표시명이 붙는다 — 라벨 문자열을 픽스처에 직접 박으면 그 매핑이 바뀌어도
    픽스처가 조용히 낡은 문자열을 재게 된다.
    """

    screen_id: str = Field(min_length=1)
    page_type: str = Field(min_length=1)
    filters: dict[str, str] = Field(default_factory=dict)
    columns: int | None = None
    products: list[Product] = Field(min_length=1)


class ProbeContext(CamelModel):
    """세션 상태 한 종류 — decompose 에 무엇을 실을지 결정한다."""

    context_id: Literal[
        "none",
        "lastRecommendations",
        "pendingCart",
        "categoryPrior",
        "screenSingle",
        "screenTriple",
        "screenFive",
        "screenNine",
        "screenNamed",
    ]
    include_prior_filters: bool
    include_last_recommendations: bool
    include_pending_cart: bool
    # [#84] 어느 PRIOR_FILTERS 를 실을지 — `default` 는 `anchors.priorFilters`,
    # `category` 는 `anchors.categoryPriorFilters`(직전 카테고리가 있는 스레드). 기본값이라
    # 기존 3개 컨텍스트 정의는 한 글자도 바뀌지 않는다.
    prior_filters_ref: Literal["default", "category"] = "default"
    # [#300] screen 맥락을 실을지와 그 대상. `screen_ref` 는 `anchors.screens` 의 `screenId` 를
    # 가리킨다 — 기본값이 없는 이유는 (D-3.2) 참조.
    include_screen: bool = False
    screen_ref: str | None = None
    # [#300] LAST_RECOMMENDATIONS 로 실을 목록 — `default` 는 기존 `anchors.last_recommendations`
    # (#84 의 `prior_filters_ref` 와 같은 패턴, 기본값이라 기존 4개 컨텍스트 정의는 불변),
    # `screen` 은 `anchors.screen_last_recommendations`(screen 컨텍스트 전용 목록, D-4 근거).
    last_recommendations_ref: Literal["default", "screen"] = "default"

    @model_validator(mode="after")
    def _pending_cart_needs_recommendations(self) -> "ProbeContext":
        if self.include_pending_cart and not self.include_last_recommendations:
            raise ValueError(
                "PENDING_CART 컨텍스트는 LAST_RECOMMENDATIONS 를 함께 실어야 합니다 "
                "— 되물음 중 상품 전환에 고를 대상이 없으면 전환 축이 성립하지 않습니다"
            )
        return self

    @model_validator(mode="after")
    def _pending_cart_and_screen_are_exclusive(self) -> "ProbeContext":
        """[#300, 이슈 완료조건] pendingCart + screen 은 배포에 없는 조합이다.

        #118 이 확정한 규약("되물음 턴에는 화면 맥락을 프롬프트에 싣지 않는다", PR #292 4차 리뷰)
        이고 `app/agents/buyer/graph.py` 의 `screen_context_active = pending_dict is None` 이 그
        배선이다. 성립하지 않는 컨텍스트를 픽스처가 표현할 수 있으면 배포에 없는 조건을 재게
        된다.
        """
        if self.include_pending_cart and self.include_screen:
            raise ValueError(
                "pendingCart 와 screen 은 함께 실릴 수 없습니다 — 되물음 턴에는 화면 맥락을 "
                "프롬프트에 싣지 않는다는 배포 배선(graph.py screen_context_active)과 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _screen_ref_presence_matches_include_screen(self) -> "ProbeContext":
        if self.include_screen and self.screen_ref is None:
            raise ValueError(
                f"{self.context_id}: includeScreen 이 true 면 screenRef 를 선언해야 합니다"
            )
        if not self.include_screen and self.screen_ref is not None:
            raise ValueError(
                f"{self.context_id}: includeScreen 이 false 면 screenRef 를 선언할 수 없습니다"
            )
        return self

    @model_validator(mode="after")
    def _include_screen_matches_context_id(self) -> "ProbeContext":
        """[#313] `includeScreen` 플래그와 `context_id` 가 screen 컨텍스트인지가 어긋나면 안 된다.

        `GROUP_ALLOWED_CONTEXTS`(`Utterance._contexts_are_within_the_group_allowlist`)는 contextId
        **문자열** 기준으로 group↔컨텍스트를 제약한다. 반면 `_screen_utterances_reference_a_screen_context`
        (AnchorSet)는 `includeScreen` **플래그** 기준이다. 둘이 같은 뜻이라는 전제가 깨지면(예:
        `context_id="none"` 인데 `include_screen=true`) 매핑이 우회된다 — 그래서 두 기준이 항상
        일치하도록 여기서 양방향으로 강제한다.
        """
        is_screen_id = self.context_id in SCREEN_CONTEXT_IDS
        if self.include_screen != is_screen_id:
            raise ValueError(
                f"{self.context_id}: includeScreen({self.include_screen}) 이 contextId 의 screen "
                f"여부({is_screen_id})와 어긋납니다 — 이 둘이 어긋나면 group→컨텍스트 매핑을 "
                "우회할 수 있습니다"
            )
        return self


class Expected(CamelModel):
    """이 발화의 정답. productIdRule 은 cart.productId 채점 규칙이다."""

    intent: Literal[
        "recommend",
        "cart_add",
        "cart_view",
        "order_status",
        "general",
        "wishlist_view",
        # [#440] wishlist_remove 축 신설로 추가 — cart_remove 는 그 축의 잠식 대조("장바구니에서
        # 빼줘")에 필요하다.
        "wishlist_remove",
        "cart_remove",
        # [#285, I-25 §4.13 — 4단계] cart_quantity 축 신설로 추가.
        "cart_quantity",
    ]
    option_id: int | None = None
    product_id_rule: Literal[
        "none",
        "notReaskProduct",
        "inLastRecommendations",
        "screenExact",
        "screenReask",
        "screenNotHallucinated",
    ] = "none"
    # [#84] 직전 카테고리를 어떻게 해야 하는가 — `category_action` 그룹만 선언한다.
    # 채점은 `resolve_category_action` 이 낸 **확정값**과 대조한다(프로브가 규칙을 재구현하지
    # 않는다 — 배포 경로와 측정이 갈라지면 표가 거짓이 된다).
    # ⚠️ 이것은 **가드 확정값의 기대치**이지 LLM 응답 필드가 아니다. 같은 이름의 인라인
    # `categoryAction` 필드는 실측으로 기각돼 `_SYSTEM` 에서 제거됐지만(이득 0 · 전환 축 손해),
    # carry|clear|replace 라는 판정 이름은 그대로 쓴다.
    category_action: Literal["carry", "clear", "replace"] | None = None
    # [#300] screen 셀이 맞춰야 할 확정 대상(screenExact) — 그 발화가 선언한 screen 픽스처의
    # products 안에 있어야 한다(AnchorSet 검증자가 확인).
    product_id: int | None = None
    # [#300] screen 셀이 확정을 금지해야 할 id(screenNotHallucinated) — 해당 screen ∪
    # screenLastRecommendations 어디에도 없어야 한다(AnchorSet 검증자가 확인).
    forbidden_product_id: int | None = None


class Utterance(CamelModel):
    """발화 한 줄 — 어떤 컨텍스트에서 재고, 어떤 축에 세는지까지 포함한다."""

    utterance_id: str = Field(min_length=1)
    group: str
    text: str = Field(min_length=1)
    contexts: list[str] = Field(min_length=1)
    expected: Expected
    axes: list[str] = Field(min_length=1)
    note: str | None = None

    @field_validator("group")
    @classmethod
    def _group_is_allowlisted(cls, value: str) -> str:
        if value not in GROUPS:
            raise ValueError(f"알 수 없는 group: {value}")
        return value

    @field_validator("contexts")
    @classmethod
    def _contexts_are_allowlisted(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(CONTEXT_IDS)
        if unknown:
            raise ValueError(f"알 수 없는 contextId: {sorted(unknown)}")
        return value

    @field_validator("axes")
    @classmethod
    def _axes_are_allowlisted(cls, value: list[str]) -> list[str]:
        unknown = set(value) - AXIS_IDS
        if unknown:
            raise ValueError(f"알 수 없는 축(axis): {sorted(unknown)}")
        return value

    @model_validator(mode="after")
    def _contexts_are_within_the_group_allowlist(self) -> "Utterance":
        """[#313] group 이 선언할 수 있는 컨텍스트를 `GROUP_ALLOWED_CONTEXTS` 로 강제한다.

        왜: 개별 그룹 전용 검증자(#84 `_category_action_group_is_isolated`, #300
        `_non_screen_utterances_cannot_reference_screen_contexts`)는 각자 자기 그룹만 지켰고,
        그 사이에 `option_answer`·`switch` 처럼 아무도 지키지 않은 그룹이 남았다. 이 둘은 분모가
        안 변하는 컨텍스트 치환이라 기존 수치 가드(`test_existing_axis_denominators_are_unaffected_by_screen_cells`)
        를 통과한다 — `option-answer-001` 의 contexts 를 `pendingCart`→`none` 으로 바꾸면
        `optionAnswer` 분모는 32 로 그대로인데 프롬프트에 PENDING_CART(옵션 목록)가 안 실려
        LLM 이 고를 대상 자체가 없어져 조용히 ~0/32 로 떨어진다. `switch-001` 의
        `pendingCart`→`lastRecommendations` 도 같은 모양이다(되물음이 없어 "되물음 상품이 아닌
        목록 내 상품" 술어가 성립 불가). 그 표를 받아 든 사람은 이를 #240 계열의 「픽스처 결함을
        프롬프트 회귀로 오독」한다.

        [#300 → #313] 이 검증자는 `AnchorSet._non_screen_utterances_cannot_reference_screen_contexts`
        를 흡수해 삭제했다. 그 검증자가 잡던 재현도 여전히 여기서 잡힌다: `cart-control-001` 의
        contexts 에 `"screenTriple"` 을 추가하면(비-screen 그룹의 screen 컨텍스트 선언) 그 발화의
        셀이 3 → 4 로 늘어나 `cartControl` 분모(N=8 이면 144)가 조용히 152 가 됐었다 — 이제는
        `cart_control` 의 허용 목록({'lastRecommendations', 'none', 'pendingCart'})에
        `screenTriple` 이 없어 이 매핑 하나가 거부한다. #300 이 남긴 categoryPrior 관련 ⚠️(같은
        구멍이 있는데 범위 밖이라 의도적으로 고치지 않는다)도 이 일반형 매핑이 흡수해 해소됐다 —
        `categoryPrior` 는 `category_action` 만의 허용 컨텍스트라 다른 그룹은 애초에 선언할 수
        없다.
        """
        if self.group not in GROUP_ALLOWED_CONTEXTS:
            raise ValueError(
                f"{self.utterance_id}: group={self.group!r} 은 GROUP_ALLOWED_CONTEXTS 에 없습니다 "
                "— 매핑에 한 줄을 넣지 않으면 이 group 은 어떤 컨텍스트도 선언할 수 없습니다"
            )
        if len(self.contexts) != len(set(self.contexts)):
            raise ValueError(f"{self.utterance_id}: contexts 에 중복이 있습니다: {self.contexts}")
        allowed = GROUP_ALLOWED_CONTEXTS[self.group]
        disallowed = sorted(set(self.contexts) - allowed)
        if disallowed:
            raise ValueError(
                f"{self.utterance_id}: group={self.group!r} 은 컨텍스트 {sorted(allowed)} 만 선언할 "
                f"수 있는데 {disallowed} 를 선언했습니다 — 새 셀이 기존 축의 분모를 늘리거나(분모가"
                " 변하는 경우) 프롬프트에 실리지 않는 컨텍스트를 재는(분모가 안 변하는 경우) 축"
                " 오염 경로입니다"
            )
        return self

    @model_validator(mode="after")
    def _expectations_match_group(self) -> "Utterance":
        if self.group == "option_answer" and self.expected.option_id is None:
            raise ValueError(
                f"{self.utterance_id}: 옵션 답변 발화는 기대 optionId 를 선언해야 합니다 "
                "— intent 만 맞고 optionId 가 틀린 답을 정답으로 세면 축이 무의미해집니다"
            )
        if self.group == "switch" and self.expected.product_id_rule == "none":
            raise ValueError(
                f"{self.utterance_id}: 전환 발화는 productIdRule 을 선언해야 합니다 "
                "— 되물음 상품을 그대로 에코하는 실패를 정답과 갈라야 합니다"
            )
        return self

    @model_validator(mode="after")
    def _screen_expectations_match_their_rule(self) -> "Utterance":
        """[#300, D-3.4·D-3.5] screen 발화의 productIdRule 은 3종 중 하나여야 하고, 각 규칙이
        요구하는 필드 조합(확정 대상/금지 대상의 비어 있음 여부)을 지킨다.

        여기서 확인하는 것은 **필드 조합**뿐이다 — `productId` 가 실제로 그 발화의 screen
        픽스처 안에 있는지, `forbiddenProductId` 가 두 목록 밖인지는 픽스처 전체(screens ·
        screenLastRecommendations)를 봐야 해서 `AnchorSet` 레벨 검증자가 맡는다.
        """
        if self.group != SCREEN_GROUP:
            return self
        rule = self.expected.product_id_rule
        if rule not in SCREEN_PRODUCT_ID_RULES:
            raise ValueError(
                f"{self.utterance_id}: screen 발화는 productIdRule 이 {SCREEN_PRODUCT_ID_RULES} "
                f"중 하나여야 합니다(현재 {rule!r}) — 화면 지시어 해소는 확정/되물음/확정금지 "
                "세 모양뿐입니다"
            )
        if rule == "screenExact" and self.expected.product_id is None:
            raise ValueError(
                f"{self.utterance_id}: screenExact 는 expected.productId 가 필수입니다"
            )
        if rule == "screenReask" and (
            self.expected.product_id is not None or self.expected.forbidden_product_id is not None
        ):
            raise ValueError(
                f"{self.utterance_id}: screenReask 는 expected.productId·forbiddenProductId 를 "
                "둘 다 비워야 합니다 — 되물음이 정답이라 확정 대상이 없습니다"
            )
        if rule == "screenNotHallucinated" and self.expected.forbidden_product_id is None:
            raise ValueError(
                f"{self.utterance_id}: screenNotHallucinated 는 "
                "expected.forbiddenProductId 가 필수입니다"
            )
        return self

    @model_validator(mode="after")
    def _screen_axes_match_the_rule(self) -> "Utterance":
        """[#300, F-2] screen 발화의 축 선언은 `productIdRule` 이 가리키는 축 하나 +
        `screenResolution` 이어야 한다 — 다른 조합은 거부한다.

        `screenExact` 발화가 실수로 `axes` 에 `screenReask` 를 선언해도 스키마가 통과시키면
        축 정의 문장이 말하는 분모(32/8/8/48)와 실제 채점 표본이 조용히 어긋난다 — 표를 봐도
        드러나지 않는 실패 모양이다(리뷰 재현: screen-001 의 축을 바꾸면 `screenExactPick`
        32 → 24, `screenReask` 8 → 16). `screenResolution` 은 합계 축이라 **모든** screen
        발화가 함께 선언해야 한다 — 하나라도 빠지면 그 분모(48)가 조용히 깎인다.
        """
        if self.group != SCREEN_GROUP:
            return self
        axis = SCREEN_RULE_TO_AXIS.get(self.expected.product_id_rule)
        if axis is None:
            return self  # 규칙 자체가 잘못됐으면 위 검증자가 이미 거부했다
        expected_axes = {axis, "screenResolution"}
        declared = set(self.axes)
        if declared != expected_axes:
            raise ValueError(
                f"{self.utterance_id}: screen 발화(productIdRule={self.expected.product_id_rule!r})"
                f" 의 axes 는 {sorted(expected_axes)} 여야 합니다(현재 {sorted(declared)}) — "
                "다른 조합을 선언하면 축 정의가 말하는 분모와 실제 채점 표본이 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _screen_group_is_isolated(self) -> "Utterance":
        """[#300, D-3.4] screen 축은 기존 축과 표본을 섞지 않는다 — #84 의
        `_category_action_group_is_isolated` 와 완전히 같은 이유다: 새 셀이 `mainIntent` 같은
        기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없게 된다.
        """
        declared = set(self.axes)
        if self.group == SCREEN_GROUP:
            if len(self.contexts) != 1:
                raise ValueError(
                    f"{self.utterance_id}: screen 발화의 contexts 는 screen 컨텍스트 1개여야 "
                    "합니다 — screen 컨텍스트는 서로 다른 화면 모양을 가리켜 셀당 하나만 뜻이 있습니다"
                )
            legacy = sorted(declared & (LEGACY_AXIS_IDS | CATEGORY_ACTION_AXIS_IDS))
            if legacy:
                raise ValueError(
                    f"{self.utterance_id}: screen 발화는 기존 축 {legacy} 를 선언할 수 없습니다 "
                    "— 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없게 됩니다"
                )
        elif declared & SCREEN_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & SCREEN_AXIS_IDS)} 는 "
                f"group='{SCREEN_GROUP}' 발화만 선언할 수 있습니다 — 다른 그룹이 섞이면 screen "
                "축의 분모가 정의(6발화)와 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _category_action_group_is_isolated(self) -> "Utterance":
        """[#84] 카테고리 축은 **기존 축과 표본을 섞지 않는다.**

        신규 셀이 `mainIntent` 같은 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수
        없게 된다 — 회귀 0을 증명하려고 만든 축이 스스로 비교 불가가 되는 자기모순이다.
        그래서 (a) 기대 categoryAction 선언 강제, (b) 기존 축 선언 금지, (역) 다른 그룹의 신규 축
        선언 금지를 스키마가 막는다. 컨텍스트는 `categoryPrior` 하나여야 한다는 제약은 [#313]
        `GROUP_ALLOWED_CONTEXTS`(`_contexts_are_within_the_group_allowlist`)로 옮겨졌다.
        """
        declared = set(self.axes)
        if self.group == CATEGORY_ACTION_GROUP:
            if self.expected.category_action is None:
                raise ValueError(
                    f"{self.utterance_id}: 카테고리 발화는 기대 categoryAction 을 선언해야 합니다 "
                    "— intent 만 맞고 승계 판정이 틀린 답을 정답으로 세면 축이 무의미해집니다"
                )
            legacy = sorted(declared & LEGACY_AXIS_IDS)
            if legacy:
                raise ValueError(
                    f"{self.utterance_id}: 카테고리 발화는 기존 축 {legacy} 를 선언할 수 없습니다 "
                    "— 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선 "
                    "baselines/fast-2026-08-04/ 와 그 축을 비교할 수 없게 됩니다"
                )
        elif declared & CATEGORY_ACTION_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & CATEGORY_ACTION_AXIS_IDS)} 는 "
                f"group='{CATEGORY_ACTION_GROUP}' 발화만 선언할 수 있습니다 "
                "— 다른 그룹이 섞이면 신규 축의 분모가 정의(카테고리 11발화)와 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _condition_only_group_is_isolated(self) -> "Utterance":
        """[#344 라운드 2] 조건 전용 축은 기존 축과 표본을 섞지 않는다 — 다른 그룹과 같은 이유
        (`_category_action_group_is_isolated`·`_screen_group_is_isolated` 참조).

        [#344 라운드 3] 컨텍스트가 `none` 하나여야 한다는 제약은 [#313]
        `GROUP_ALLOWED_CONTEXTS`(`_contexts_are_within_the_group_allowlist`)로 옮겨졌다 —
        `condition_only` 의 허용 컨텍스트가 `{"none"}` 하나뿐이라 그 매핑만으로 이미 충분하다
        (`_category_action_group_is_isolated` 가 categoryPrior 단일 제약을 옮긴 것과 같은 정리).
        """
        declared = set(self.axes)
        if self.group == CONDITION_ONLY_GROUP:
            legacy = sorted(
                declared & (LEGACY_AXIS_IDS | CATEGORY_ACTION_AXIS_IDS | SCREEN_AXIS_IDS)
            )
            if legacy:
                raise ValueError(
                    f"{self.utterance_id}: 조건 전용 발화는 기존 축 {legacy} 를 선언할 수 없습니다 "
                    "— 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없게 됩니다"
                )
        elif declared & CONDITION_ONLY_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & CONDITION_ONLY_AXIS_IDS)} 는 "
                f"group='{CONDITION_ONLY_GROUP}' 발화만 선언할 수 있습니다 "
                "— 다른 그룹이 섞이면 신규 축의 분모가 정의(조건 전용 5발화)와 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _wishlist_view_group_is_isolated(self) -> "Utterance":
        """[#386] 찜 조회 축도 기존 축과 표본을 섞지 않는다 — 앞의 세 검증자와 같은 이유.

        이 격리가 **이번 이슈에서 특히 중요한 이유**: 이 프로브의 목적이 "기존 라우팅이 안
        깨졌는가"의 대조인데, 새 발화 6건이 `mainIntent` 같은 legacy 축의 분모에 섞이면 커밋된
        기준선과 그 축을 비교할 수 없게 된다 — 바로 그 비교를 하려고 프로브를 돌리는 것이라
        섞이는 순간 목적이 사라진다.
        """
        declared = set(self.axes)
        if self.group == WISHLIST_VIEW_GROUP:
            others = sorted(
                declared
                & (
                    LEGACY_AXIS_IDS
                    | CATEGORY_ACTION_AXIS_IDS
                    | SCREEN_AXIS_IDS
                    | CONDITION_ONLY_AXIS_IDS
                )
            )
            if others:
                raise ValueError(
                    f"{self.utterance_id}: 찜 조회 발화는 기존 축 {others} 를 선언할 수 없습니다 "
                    "— 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없게 됩니다"
                )
        elif declared & WISHLIST_VIEW_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & WISHLIST_VIEW_AXIS_IDS)} 는 "
                f"group='{WISHLIST_VIEW_GROUP}' 발화만 선언할 수 있습니다 "
                "— 다른 그룹이 섞이면 신규 축의 분모가 정의(찜 조회 6발화)와 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _wishlist_remove_group_is_isolated(self) -> "Utterance":
        """[#440] 찜 해제 대상 해소 축도 기존 축과 표본을 섞지 않는다 — 앞의 네 검증자와 같은
        이유(`_wishlist_view_group_is_isolated` 참조)."""
        declared = set(self.axes)
        if self.group == WISHLIST_REMOVE_GROUP:
            others = sorted(
                declared
                & (
                    LEGACY_AXIS_IDS
                    | CATEGORY_ACTION_AXIS_IDS
                    | SCREEN_AXIS_IDS
                    | CONDITION_ONLY_AXIS_IDS
                    | WISHLIST_VIEW_AXIS_IDS
                    | NAMED_CATEGORY_AXIS_IDS
                )
            )
            if others:
                raise ValueError(
                    f"{self.utterance_id}: 찜 해제 발화는 기존 축 {others} 를 선언할 수 없습니다 "
                    "— 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없게 됩니다"
                )
        elif declared & WISHLIST_REMOVE_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & WISHLIST_REMOVE_AXIS_IDS)} 는 "
                f"group='{WISHLIST_REMOVE_GROUP}' 발화만 선언할 수 있습니다 "
                "— 다른 그룹이 섞이면 신규 축의 분모가 정의(찜 해제 4발화)와 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _cart_quantity_group_is_isolated(self) -> "Utterance":
        """[#285, I-25 §4.13 — 4단계] 장바구니 수량 변경 축도 기존 축과 표본을 섞지 않는다 —
        앞의 다섯 검증자와 같은 이유(`_wishlist_view_group_is_isolated` 참조)."""
        declared = set(self.axes)
        if self.group == CART_QUANTITY_GROUP:
            others = sorted(
                declared
                & (
                    LEGACY_AXIS_IDS
                    | CATEGORY_ACTION_AXIS_IDS
                    | SCREEN_AXIS_IDS
                    | CONDITION_ONLY_AXIS_IDS
                    | WISHLIST_VIEW_AXIS_IDS
                    | WISHLIST_REMOVE_AXIS_IDS
                    | NAMED_CATEGORY_AXIS_IDS
                )
            )
            if others:
                raise ValueError(
                    f"{self.utterance_id}: 수량 변경 발화는 기존 축 {others} 를 선언할 수 없습니다 "
                    "— 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없게 됩니다"
                )
        elif declared & CART_QUANTITY_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & CART_QUANTITY_AXIS_IDS)} 는 "
                f"group='{CART_QUANTITY_GROUP}' 발화만 선언할 수 있습니다 "
                "— 다른 그룹이 섞이면 신규 축의 분모가 정의(수량 변경 6발화)와 어긋납니다"
            )
        return self

    @model_validator(mode="after")
    def _named_category_group_is_isolated(self) -> "Utterance":
        """[#443] 상품군 명시 첫 턴 축도 기존 축과 표본을 섞지 않는다 — 앞의 네 검증자와 같은
        이유(`_condition_only_group_is_isolated` 와 특히 같은 모양 — 반대 방향 축이다).
        """
        declared = set(self.axes)
        if self.group == NAMED_CATEGORY_GROUP:
            others = sorted(
                declared
                & (
                    LEGACY_AXIS_IDS
                    | CATEGORY_ACTION_AXIS_IDS
                    | SCREEN_AXIS_IDS
                    | CONDITION_ONLY_AXIS_IDS
                    | WISHLIST_VIEW_AXIS_IDS
                    | WISHLIST_REMOVE_AXIS_IDS
                    | CART_QUANTITY_AXIS_IDS
                )
            )
            if others:
                raise ValueError(
                    f"{self.utterance_id}: 상품군 명시 발화는 기존 축 {others} 를 선언할 수 "
                    "없습니다 — 새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 "
                    "수 없게 됩니다"
                )
        elif declared & NAMED_CATEGORY_AXIS_IDS:
            raise ValueError(
                f"{self.utterance_id}: {sorted(declared & NAMED_CATEGORY_AXIS_IDS)} 는 "
                f"group='{NAMED_CATEGORY_GROUP}' 발화만 선언할 수 있습니다 "
                "— 다른 그룹이 섞이면 신규 축의 분모가 정의(상품군 명시 6발화)와 어긋납니다"
            )
        return self


class AnchorSet(CamelModel):
    """프로브가 읽는 유일한 입력. 스크립트는 이 파일 밖의 발화를 만들지 않는다."""

    fixture_version: str = Field(min_length=1)
    schema_version: Literal["1.3.0"] = FIXTURE_SCHEMA_VERSION
    reask_product_id: int
    reask_product_list_position: int = Field(ge=1)
    reask_position_rationale: str = Field(min_length=40)
    switch_targets: list[str] = Field(min_length=1)
    prior_filters: dict[str, Any]
    # [#84] `categoryPrior` 컨텍스트가 싣는 PRIOR_FILTERS — 직전 카테고리가 있는 스레드.
    category_prior_filters: dict[str, Any]
    last_recommendations: list[Product] = Field(min_length=2)
    options: list[Option] = Field(min_length=2)
    # [#300] screen 픽스처 5종(#118 이관) — `ProbeContext.screen_ref` 가 `screenId` 로 가리킨다.
    screens: list[ScreenFixture] = Field(min_length=1)
    # [#300] screen 컨텍스트 전용 LAST_RECOMMENDATIONS(원본 `RECO_BASE`). 기본
    # `last_recommendations` 와 분리하는 이유는 D-3.6 검증자 docstring 참조.
    screen_last_recommendations: list[Product] = Field(min_length=1)
    contexts: list[ProbeContext] = Field(min_length=1)
    utterances: list[Utterance] = Field(min_length=1)

    @field_validator("reask_position_rationale")
    @classmethod
    def _rationale_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reaskPositionRationale 은 비워 둘 수 없습니다")
        return value

    @field_validator("prior_filters")
    @classmethod
    def _prior_filters_match_search_contract(cls, value: dict[str, Any]) -> dict[str, Any]:
        ProductSearchFilters.model_validate(value)
        return value

    @field_validator("category_prior_filters")
    @classmethod
    def _category_prior_filters_carry_a_category(cls, value: dict[str, Any]) -> dict[str, Any]:
        """[#84] 검색 계약 정합 + **category 키 필수**.

        이 컨텍스트의 존재 이유가 "직전 카테고리가 있는 스레드"다. category 가 비면 carry 도
        clear 도 가리킬 대상이 없어 축이 통째로 무의미해지는데, 그 사실은 표를 봐도 드러나지
        않는다(전부 그럴듯한 숫자로 채워진다) — 그래서 스키마가 막는다.
        """
        ProductSearchFilters.model_validate(value)
        if not str(value.get("category") or "").strip():
            raise ValueError(
                "categoryPriorFilters 에는 category 가 있어야 합니다 "
                "— 직전 카테고리가 없으면 carry/clear/replace 판정 대상이 없습니다"
            )
        return value

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> "AnchorSet":
        for label, ids in (
            ("utteranceId", [u.utterance_id for u in self.utterances]),
            ("productId", [p.product_id for p in self.last_recommendations]),
            ("optionId", [o.option_id for o in self.options]),
            ("contextId", [c.context_id for c in self.contexts]),
            ("screenId", [s.screen_id for s in self.screens]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} 가 중복되었습니다")
        return self

    @model_validator(mode="after")
    def _screen_refs_point_to_existing_fixtures(self) -> "AnchorSet":
        """[#300, D-3.3] `ProbeContext.screen_ref` 는 `screens` 에 실존하는 `screenId` 여야 한다."""
        known = {screen.screen_id for screen in self.screens}
        for context in self.contexts:
            if context.screen_ref is not None and context.screen_ref not in known:
                raise ValueError(
                    f"{context.context_id}: 존재하지 않는 screenId {context.screen_ref!r} 를 "
                    "가리킵니다"
                )
        return self

    @model_validator(mode="after")
    def _screen_utterances_reference_a_screen_context(self) -> "AnchorSet":
        """[#300, D-3.4] screen 발화가 가리키는 컨텍스트는 실제로 `includeScreen=true` 여야 한다.

        `Utterance._screen_group_is_isolated` 는 개수(1개)만 확인한다 — 그 컨텍스트가 진짜
        screen 컨텍스트인지는 `ProbeContext` 목록을 봐야 해서 여기서 확인한다.
        """
        by_id = {context.context_id: context for context in self.contexts}
        for utterance in self.utterances:
            if utterance.group != SCREEN_GROUP:
                continue
            context = by_id.get(utterance.contexts[0])
            if context is None or not context.include_screen:
                raise ValueError(
                    f"{utterance.utterance_id}: contexts[0]={utterance.contexts[0]!r} 는 screen "
                    "컨텍스트(includeScreen=true)가 아닙니다"
                )
        return self

    @model_validator(mode="after")
    def _screen_expected_product_ids_are_consistent(self) -> "AnchorSet":
        """[#300, D-3.5] screenExact/screenNotHallucinated 의 대상 id 가 실제 픽스처와 맞는지.

        `screenExact` 는 그 발화가 선언한 screen 픽스처의 products 안에 있어야 하고,
        `screenNotHallucinated` 는 그 id 가 **두 목록**(해당 screen ∪ screenLastRecommendations)
        어디에도 없어야 한다 — 목록 안 id 면 "확정 금지" 술어가 무의미해진다.
        """
        context_by_id = {context.context_id: context for context in self.contexts}
        screen_by_id = {screen.screen_id: screen for screen in self.screens}
        reco_ids = {product.product_id for product in self.screen_last_recommendations}
        for utterance in self.utterances:
            if utterance.group != SCREEN_GROUP:
                continue
            context = context_by_id.get(utterance.contexts[0])
            screen = (
                screen_by_id.get(context.screen_ref)
                if context is not None and context.screen_ref is not None
                else None
            )
            if screen is None:
                continue  # 이미 다른 검증자(3·4)가 거부한다
            screen_ids = {product.product_id for product in screen.products}
            expected = utterance.expected
            if expected.product_id_rule == "screenExact" and expected.product_id not in screen_ids:
                raise ValueError(
                    f"{utterance.utterance_id}: expected.productId {expected.product_id} 가 "
                    f"{screen.screen_id} 의 products 안에 없습니다"
                )
            if expected.product_id_rule == "screenNotHallucinated" and (
                expected.forbidden_product_id in screen_ids
                or expected.forbidden_product_id in reco_ids
            ):
                raise ValueError(
                    f"{utterance.utterance_id}: forbiddenProductId "
                    f"{expected.forbidden_product_id} 가 screen 또는 screenLastRecommendations "
                    "목록 안에 있습니다 — '확정 금지' 술어가 무의미해집니다"
                )
        return self

    @model_validator(mode="after")
    def _screen_names_do_not_overlap_screen_last_recommendations(self) -> "AnchorSet":
        """[#300, D-3.6] screen 픽스처 상품명과 screenLastRecommendations 상품명이 겹치면 안 된다.

        공백으로 자른 2글자 이상 토큰이 두 목록 사이에 공유되면 거부한다. 이름 매칭 셀
        (`무선 이어폰 담아줘`)의 정답이 자명하려면 그 이름이 화면에만 있어야 한다 — 실제로 이
        함정은 살아 있다: 기본 `lastRecommendations` 에 `104 무선 블루투스 이어폰` 이 있어서,
        screen 컨텍스트가 그 목록을 실었다면 `resolve_screen_reference` 의 양보 (B)(이름이 직전
        추천에만 있어도 개입하지 않는다)가 발동해 셀이 통째로 무의미해진다 — 그래서 screen
        전용 목록(`screenLastRecommendations`)이 필요하고, 그 전용 목록조차 화면 이름과 겹치면
        같은 함정이 재발한다. #240 「픽스처 문자열이 정답 신호와 겹치면 안 된다」와 같은 계열의
        검증자다.

        ⚠️ [#300, D-3.7] `_option_token_not_in_product_names` 를 `screenLastRecommendations` 로
        확장하지 않는다 — #118 원본 목록(`RECO_BASE`)에 `드럼용 세탁 세제` 가 있어 옵션 `드럼형`
        과 겹치지만, screen 컨텍스트는 `pendingCart` 를 **구조적으로 가질 수 없으므로**
        (`_pending_cart_and_screen_are_exclusive`) 옵션 신호가 샐 표면이 없다. 확장하면 원본
        표본을 바꿔야 해서 §1 이 요구하는 "표본 동일성"(이관 원본과 문자 단위로 동일)이 깨진다.
        """
        reco_tokens: set[str] = set()
        for product in self.screen_last_recommendations:
            reco_tokens.update(token for token in product.name.split() if len(token) >= 2)
        for screen in self.screens:
            for product in screen.products:
                tokens = {token for token in product.name.split() if len(token) >= 2}
                overlap = reco_tokens & tokens
                if overlap:
                    raise ValueError(
                        f"{screen.screen_id}: 상품 '{product.name}' 의 토큰 {sorted(overlap)} 이 "
                        "screenLastRecommendations 상품명과 겹칩니다 — 이름 매칭 정답 신호가 새어 "
                        "resolve_screen_reference 의 양보(B)가 잘못 발동할 수 있습니다"
                    )
        return self

    @model_validator(mode="after")
    def _reask_position_matches_list(self) -> "AnchorSet":
        """되물음 상품의 목록 위치를 못 박는다.

        #240: 이 위치가 1번이냐 2번이냐만으로 `일반형`(1번 옵션) 정답률이 fast 기준
        8/8 ↔ 3/8 로 갈렸다. 선언한 위치와 실제 목록이 어긋나면 리포트의 위치 라벨이
        거짓이 되고, 그 표는 다른 런과 비교할 수 없다.
        """
        if self.reask_product_list_position > len(self.last_recommendations):
            raise ValueError("되물음 상품 위치가 LAST_RECOMMENDATIONS 길이를 넘습니다")
        pinned = self.last_recommendations[self.reask_product_list_position - 1]
        if pinned.product_id != self.reask_product_id:
            raise ValueError(
                f"되물음 상품 위치가 목록과 어긋납니다: {self.reask_product_list_position}번은 "
                f"{pinned.product_id} 인데 reaskProductId 는 {self.reask_product_id} 입니다"
            )
        return self

    @model_validator(mode="after")
    def _option_token_not_in_product_names(self) -> "AnchorSet":
        """옵션 이름의 어간이 상품명에 섞이는 것을 금지한다.

        #240 실제 결함: 되물음 상품명이 `드럼용 세탁 세제` 여서 옵션 `드럼형` 과 문자열이
        겹쳤고, `일반형` 답변이 8/8 오답이었다. **프롬프트 결함이 아니라 픽스처 결함**이다.
        """
        for option in self.options:
            token = option.name[:-1] if option.name.endswith("형") else option.name
            if len(token) < 2:
                continue
            for product in self.last_recommendations:
                if token in product.name:
                    raise ValueError(
                        f"옵션 이름 '{option.name}' 의 어간 '{token}' 이 상품명 "
                        f"'{product.name}' 과 겹칩니다 — 정답 신호가 새어 채점이 무의미해집니다"
                    )
        return self

    @model_validator(mode="after")
    def _switch_targets_are_answerable_and_unambiguous(self) -> "AnchorSet":
        """전환 대상어가 목록에 있고, 되물음 상품과는 겹치지 않아야 한다."""
        reask_name = next(
            product.name
            for product in self.last_recommendations
            if product.product_id == self.reask_product_id
        )
        for target in self.switch_targets:
            if target in reask_name:
                raise ValueError(
                    f"전환 대상어 '{target}' 이 되물음 상품명 '{reask_name}' 에 들어 있습니다 "
                    "— 전환 정답과 에코 실패를 구분할 수 없게 됩니다"
                )
            if not any(target in product.name for product in self.last_recommendations):
                raise ValueError(
                    f"전환 대상어 '{target}' 을 가리킬 상품이 LAST_RECOMMENDATIONS 에 없습니다"
                )
        return self

    @model_validator(mode="after")
    def _expected_option_ids_exist(self) -> "AnchorSet":
        known = {option.option_id for option in self.options}
        for utterance in self.utterances:
            option_id = utterance.expected.option_id
            if option_id is not None and option_id not in known:
                raise ValueError(f"{utterance.utterance_id}: 없는 optionId {option_id}")
        return self

    @model_validator(mode="after")
    def _category_utterances_avoid_the_prior_category_vocabulary(self) -> "AnchorSet":
        """[#84] 카테고리 발화에 **직전 카테고리 어휘**가 들어가는 것을 금지한다.

        "이어폰 말고 더 싼 거" 같은 발화는 carry 로도 replace 로도 읽혀 정답이 자명하지 않다 —
        #240 「픽스처 문자열이 정답 신호와 겹치면 안 된다」와 같은 함정이고, 그런 셀은 프롬프트가
        아니라 정답지 때문에 흔들린다. canonical 은 `대분류 > 잎` 형식이라 **잎 이름**으로 본다.
        """
        category = str(self.category_prior_filters.get("category") or "")
        leaf = category.rsplit(">", 1)[-1].strip()
        if len(leaf) < 2:
            return self
        for utterance in self.utterances:
            if utterance.group != CATEGORY_ACTION_GROUP:
                continue
            if leaf in utterance.text:
                raise ValueError(
                    f"{utterance.utterance_id}: 발화에 직전 카테고리 '{leaf}' 가 들어 있습니다 "
                    "— carry/replace 어느 쪽으로도 읽혀 정답이 자명하지 않습니다"
                )
        return self

    @model_validator(mode="after")
    def _declared_contexts_exist(self) -> "AnchorSet":
        known = {context.context_id for context in self.contexts}
        for utterance in self.utterances:
            missing = set(utterance.contexts) - known
            if missing:
                raise ValueError(
                    f"{utterance.utterance_id}: 선언되지 않은 컨텍스트 {sorted(missing)}"
                )
        return self
