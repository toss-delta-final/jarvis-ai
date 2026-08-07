"""과소지정 판정(underspecified) 실 LLM 프로브 앵커 스키마 (#380).

검증자는 이 하네스의 설계 확정(패킷 `packet-380.md` §D3)을 코드로 못 박는다: 셀 = 앵커 1개
(컨텍스트 행렬 없음, `evals/legs_probe` 와 같은 구조) — `is_underspecified_turn` 이 실제로 참조하는
축(§D8) 이름 어휘를 `referenceAxes`/`constraintAxes` 가 공유하고, `sourceCaseId` 가 있으면
`evals/underspecified_cases/cases.json`(척추)과 발화가 글자 그대로 같아야 한다.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

FIXTURE_SCHEMA_VERSION = "1.0.0"
SLICES = (
    "no_condition",
    "constraint_price",
    "constraint_budget_set",
    "what_axis",
    "blocking_rating",
    "multiturn_gate",
)
TEST_TYPES = ("MFT", "INV")

REPO_ROOT = Path(__file__).resolve().parents[2]
# [§D3] 척추 정본 — 읽기만 한다(수정 금지, 패킷 §3 비범위).
CASES_JSON_PATH = REPO_ROOT / "evals" / "underspecified_cases" / "cases.json"

# [§D8] `is_underspecified_turn` 이 실제로 차단 후보로 보는 축의 정규 이름 — referenceAxes 는
# 이 집합만 쓴다. price_min/price_max/totalBudget/buyAll 은 제약-축이라 여기 없다(차단 후보 아님).
BLOCKING_AXES = frozenset(
    {
        "categoryLegs",
        "categoryQueries",
        "semanticQueryIsFallback",
        "repurchaseProducts",
        "revertCategories",
        "filters.category",
        "filters.brand",
        "filters.keyword",
        "filters.color",
        "filters.attrConditions",
        "filters.rating_min",
    }
)
# [§D8] 제약-축 — 채워져도 판정을 뒤집지 않는다. constraintAxes 라벨 전용(진단용 참고값).
CONSTRAINT_AXES = frozenset({"filters.price_min", "filters.price_max", "totalBudget", "buyAll"})


class CamelModel(BaseModel):
    """eval 내부 모델도 앱 와이어와 같은 camelCase 별칭 규약을 쓴다(legs_probe 승계)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


@functools.lru_cache(maxsize=1)
def _load_cases_json_utterances() -> dict[str, str]:
    """`sourceCaseId` 대조용 — cases.json 은 **읽기만** 한다(수정 금지, 패킷 §3)."""
    records = json.loads(CASES_JSON_PATH.read_text(encoding="utf-8"))
    return {record["caseId"]: record["utterance"] for record in records}


class Anchor(CamelModel):
    """발화 1건 — 어떤 슬라이스인지, 무엇을 기대하는지, 원인 축 라벨까지 포함한다."""

    case_id: str = Field(min_length=1)
    slice: Literal[SLICES]  # type: ignore[valid-type]
    test_type: Literal["MFT", "INV"]
    utterance: str = Field(min_length=1)
    prior_exists: bool = False
    expected_reask: bool
    # [§D3] 이 발화가 채워야 한다고 저자가 라벨한 축 — 오탐(§D11) 원인 분해에 쓴다.
    reference_axes: list[str] = Field(default_factory=list)
    # [§D3] 채워져도 판정을 뒤집지 않는 제약-축 라벨 — 진단용 참고값.
    constraint_axes: list[str] = Field(default_factory=list)
    source_case_id: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _reference_axes_are_canonical(self) -> "Anchor":
        unknown = [axis for axis in self.reference_axes if axis not in BLOCKING_AXES]
        if unknown:
            raise ValueError(
                f"{self.case_id}: referenceAxes 에 정규 축이 아닌 이름이 있습니다: {unknown}"
            )
        return self

    @model_validator(mode="after")
    def _constraint_axes_are_canonical(self) -> "Anchor":
        unknown = [axis for axis in self.constraint_axes if axis not in CONSTRAINT_AXES]
        if unknown:
            raise ValueError(
                f"{self.case_id}: constraintAxes 에 정규 축이 아닌 이름이 있습니다: {unknown}"
            )
        return self

    @model_validator(mode="after")
    def _expected_reask_true_requires_empty_reference_axes_and_mft(self) -> "Anchor":
        """[§D3] `expectedReask=true` ⟹ `referenceAxes==[]` ∧ `testType=='MFT'`."""
        if self.expected_reask:
            if self.reference_axes:
                raise ValueError(
                    f"{self.case_id}: expectedReask=true 인 앵커는 referenceAxes 가 비어야 합니다"
                )
            if self.test_type != "MFT":
                raise ValueError(
                    f"{self.case_id}: expectedReask=true 인 앵커는 testType=MFT 여야 합니다"
                )
        return self

    @model_validator(mode="after")
    def _expected_reask_false_requires_reference_axes_or_gate_and_inv(self) -> "Anchor":
        """[§D3] `expectedReask=false` ⟹ (`referenceAxes` 비어있지 않음 ∨ `priorExists=true`) ∧
        `testType=='INV'`."""
        if not self.expected_reask:
            if not self.reference_axes and not self.prior_exists:
                raise ValueError(
                    f"{self.case_id}: expectedReask=false 인 앵커는 referenceAxes 가 있거나 "
                    "priorExists=true(게이트 앵커) 여야 합니다"
                )
            if self.test_type != "INV":
                raise ValueError(
                    f"{self.case_id}: expectedReask=false 인 앵커는 testType=INV 여야 합니다"
                )
        return self

    @model_validator(mode="after")
    def _source_case_id_matches_cases_json_verbatim(self) -> "Anchor":
        """[§D3 척추 검증자] `sourceCaseId` 가 있으면 cases.json 의 그 caseId 발화와 글자 그대로
        같아야 한다(#328 규약 2, legs_probe 의 goldenset 대조 검증자와 동형)."""
        if self.source_case_id is None:
            return self
        utterances = _load_cases_json_utterances()
        expected = utterances.get(self.source_case_id)
        if expected is None:
            raise ValueError(
                f"{self.case_id}: sourceCaseId {self.source_case_id!r} 가 cases.json 에 없습니다"
            )
        if self.utterance != expected:
            raise ValueError(
                f"{self.case_id}: 발화가 cases.json({self.source_case_id}) 과 다릅니다 "
                f"(anchor={self.utterance!r}, cases.json={expected!r})"
            )
        return self


class AnchorSet(CamelModel):
    """프로브가 읽는 유일한 입력. 스크립트는 이 파일 밖의 발화를 만들지 않는다."""

    schema_version: Literal["1.0.0"] = FIXTURE_SCHEMA_VERSION
    fixture_version: str = Field(min_length=1)
    utterances: list[Anchor] = Field(min_length=1)

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> "AnchorSet":
        ids = [anchor.case_id for anchor in self.utterances]
        if len(ids) != len(set(ids)):
            raise ValueError("caseId 가 중복되었습니다")
        return self

    @model_validator(mode="after")
    def _utterances_are_unique(self) -> "AnchorSet":
        """[§D3] 발화는 앵커 집합 안에서 고유해야 한다 — 결정론 fake 의 발화→산출 매핑과 셀
        분리의 전제(§D7)다."""
        texts = [anchor.utterance for anchor in self.utterances]
        if len(texts) != len(set(texts)):
            raise ValueError("utterance 가 중복되었습니다")
        return self
