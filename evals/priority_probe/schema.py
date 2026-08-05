"""priority 프로브 픽스처 스키마 (#281).

`evals/intent_probe/schema.py` 와 같은 규율 — 함정을 문서가 아니라 스키마로 막는다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.schemas.spring import ProductSearchFilters

FIXTURE_SCHEMA_VERSION = "1.0.0"
VALID_PRIORITIES = (1, 2, 3)
# [§1 규율 3] 픽스처 문자열이 정답 신호와 겹치면 안 된다 — 발화에 이 어휘가 있으면 프롬프트가
# 아니라 픽스처를 재는 셈이 된다(#240 이 밟은 함정과 같은 종류).
_FORBIDDEN_UTTERANCE_TOKENS = ("필수", "꼭 필요한", "선택", "권장")


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Product(CamelModel):
    """LAST_RECOMMENDATIONS 한 줄(채운 컨텍스트, §1 규율 5)."""

    product_id: int
    name: str = Field(min_length=1)


class Channel(CamelModel):
    """인라인 팔이 `decompose()` 에 실제로 싣는 세션 상태(§1 규율 5 — 빈 맥락 금지).

    분류기 팔은 이 값을 쓰지 않는다 — `classify_need_priorities` 는 발화·니즈만 받는다
    (배포와 동일 호출 모양).
    """

    prior_filters: dict[str, Any] | None = None
    last_recommendations: list[Product] = Field(default_factory=list)
    profile_summary: str | None = None
    category_fanout_max: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _prior_filters_match_search_contract(self) -> "Channel":
        if self.prior_filters is not None:
            ProductSearchFilters.model_validate(self.prior_filters)
        return self

    @model_validator(mode="after")
    def _channel_is_actually_filled(self) -> "Channel":
        """[§1 규율 5] 빈 맥락 프로브는 실제 세션의 이웃 규칙 영향을 제거해 거짓 결론을 준다."""
        if self.prior_filters is None and not self.last_recommendations:
            raise ValueError(
                "channel 이 비어 있습니다 — priorFilters 또는 lastRecommendations 중 "
                "하나는 채워야 배포와 같은 모양의 컨텍스트가 됩니다(§1 규율 5)"
            )
        return self


class PriorityCell(CamelModel):
    """측정 단위 — 발화 1개. 니즈 목록·기대 priority·판정 근거를 함께 든다."""

    cell_id: str = Field(min_length=1)
    utterance: str = Field(min_length=1)
    needs: list[str] = Field(min_length=2)
    expected_priorities: list[int] = Field(min_length=2)
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def _priorities_align_with_needs(self) -> "PriorityCell":
        if len(self.expected_priorities) != len(self.needs):
            raise ValueError(
                f"{self.cell_id}: expectedPriorities 길이({len(self.expected_priorities)})가 "
                f"needs 길이({len(self.needs)})와 다릅니다"
            )
        bad = [value for value in self.expected_priorities if value not in VALID_PRIORITIES]
        if bad:
            raise ValueError(f"{self.cell_id}: expectedPriorities 는 1/2/3 만 허용합니다 ({bad})")
        return self

    @model_validator(mode="after")
    def _needs_are_distinct(self) -> "PriorityCell":
        if len(set(self.needs)) != len(self.needs):
            raise ValueError(
                f"{self.cell_id}: needs 에 중복이 있습니다 — leg 정합 채점이 무의미해집니다"
            )
        return self

    @model_validator(mode="after")
    def _utterance_avoids_answer_vocabulary(self) -> "PriorityCell":
        hits = [token for token in _FORBIDDEN_UTTERANCE_TOKENS if token in self.utterance]
        if hits:
            raise ValueError(
                f"{self.cell_id}: 발화에 정답 신호 어휘 {hits} 가 들어 있습니다 "
                "— 프롬프트가 아니라 픽스처를 재는 셈이 됩니다(§1 규율 3)"
            )
        return self

    @model_validator(mode="after")
    def _at_least_one_ordering_pair(self) -> "PriorityCell":
        """priorityOrderPairs 축이 이 셀에서 최소 1쌍은 나와야 vacuous 하지 않다."""
        if len(set(self.expected_priorities)) < 2:
            raise ValueError(
                f"{self.cell_id}: expectedPriorities 가 전부 같습니다 — 이 셀은 순서(제외 순서)를 "
                "구분할 수 없어 priorityOrderPairs 축에 아무 기여도 하지 않습니다"
            )
        return self


class FixtureSet(CamelModel):
    """프로브가 읽는 유일한 입력."""

    fixture_version: str = Field(min_length=1)
    schema_version: str = FIXTURE_SCHEMA_VERSION
    channel: Channel
    cells: list[PriorityCell] = Field(min_length=10)

    @model_validator(mode="after")
    def _cell_ids_are_unique(self) -> "FixtureSet":
        ids = [cell.cell_id for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("cellId 가 중복되었습니다")
        return self

    @model_validator(mode="after")
    def _rationale_is_not_the_spec_example(self) -> "FixtureSet":
        """정본 예시(감자탕: 등뼈/들깨가루/청양고추)를 픽스처로 그대로 옮기지 않는다 — 후보
        프롬프트가 그 예시를 문면에 담고 있어(암기를 재게 된다), 다른 상황으로 만들어야 한다."""
        banned = ("등뼈", "들깨가루", "청양고추", "감자탕")
        for cell in self.cells:
            hits = [token for token in banned if token in cell.utterance or token in cell.needs]
            if hits:
                raise ValueError(
                    f"{cell.cell_id}: 정본 예시 어휘 {hits} 를 픽스처로 재사용했습니다 "
                    "— 후보 프롬프트가 이 예시를 문면에 담고 있어 암기를 재는 셈이 됩니다"
                )
        return self
