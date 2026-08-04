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

FIXTURE_SCHEMA_VERSION = "1.0.0"

CONTEXT_IDS = ("none", "lastRecommendations", "pendingCart")
GROUPS = frozenset(
    {"cart_control", "demonstrative", "option_answer", "switch", "order_status", "general"}
)
INTENTS = ("recommend", "cart_add", "cart_view", "order_status", "general")
# metrics.AXES 와 같은 집합 — 축을 늘리면 양쪽을 함께 고쳐야 하고, 테스트가 어긋남을 잡는다.
AXIS_IDS = frozenset(
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


class ProbeContext(CamelModel):
    """세션 상태 한 종류 — decompose 에 무엇을 실을지 결정한다."""

    context_id: Literal["none", "lastRecommendations", "pendingCart"]
    include_prior_filters: bool
    include_last_recommendations: bool
    include_pending_cart: bool

    @model_validator(mode="after")
    def _pending_cart_needs_recommendations(self) -> "ProbeContext":
        if self.include_pending_cart and not self.include_last_recommendations:
            raise ValueError(
                "PENDING_CART 컨텍스트는 LAST_RECOMMENDATIONS 를 함께 실어야 합니다 "
                "— 되물음 중 상품 전환에 고를 대상이 없으면 전환 축이 성립하지 않습니다"
            )
        return self


class Expected(CamelModel):
    """이 발화의 정답. productIdRule 은 cart.productId 채점 규칙이다."""

    intent: Literal["recommend", "cart_add", "cart_view", "order_status", "general"]
    option_id: int | None = None
    product_id_rule: Literal["none", "notReaskProduct", "inLastRecommendations"] = "none"


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


class AnchorSet(CamelModel):
    """프로브가 읽는 유일한 입력. 스크립트는 이 파일 밖의 발화를 만들지 않는다."""

    fixture_version: str = Field(min_length=1)
    schema_version: Literal["1.0.0"] = FIXTURE_SCHEMA_VERSION
    reask_product_id: int
    reask_product_list_position: int = Field(ge=1)
    reask_position_rationale: str = Field(min_length=40)
    switch_targets: list[str] = Field(min_length=1)
    prior_filters: dict[str, Any]
    last_recommendations: list[Product] = Field(min_length=2)
    options: list[Option] = Field(min_length=2)
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

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> "AnchorSet":
        for label, ids in (
            ("utteranceId", [u.utterance_id for u in self.utterances]),
            ("productId", [p.product_id for p in self.last_recommendations]),
            ("optionId", [o.option_id for o in self.options]),
            ("contextId", [c.context_id for c in self.contexts]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} 가 중복되었습니다")
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
    def _declared_contexts_exist(self) -> "AnchorSet":
        known = {context.context_id for context in self.contexts}
        for utterance in self.utterances:
            missing = set(utterance.contexts) - known
            if missing:
                raise ValueError(
                    f"{utterance.utterance_id}: 선언되지 않은 컨텍스트 {sorted(missing)}"
                )
        return self
