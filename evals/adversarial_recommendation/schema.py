"""구매자 추천 adversarial dataset의 엄격한 스키마."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.schemas.chat import BuyerChatRequest
from app.schemas.spring import SpringProduct

Category = Literal[
    "missing_data",
    "boundary",
    "evidence_conflict",
    "numeric_hallucination",
    "prompt_injection",
    "constraint_conflict",
    "no_evidence",
]
Difficulty = Literal["medium", "hard", "expert"]
Outcome = Literal["eligible", "ineligible", "unknown"]
NumericField = Literal["price", "rating", "reviewCount"]
Operator = Literal["ge", "gt", "le", "lt", "eq"]
MissingPolicy = Literal["exclude", "unknown"]


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


def _wire_keys(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(field.alias or to_camel(name) for name, field in model.model_fields.items())


_REQUEST_KEYS = _wire_keys(BuyerChatRequest)
# SpringProduct에는 방어적으로 받기만 하는 CH-5/재고 필드도 있지만 I-1 검색 응답에는 없다.
# 평가 후보 계약은 실제 추천 입력인 I-1 필드로 더 좁힌다(app/schemas/spring.py SpringProduct 주석).
_I1_CANDIDATE_KEYS = frozenset(
    {
        "productId",
        "name",
        "summary",
        "attributes",
        "price",
        "rating",
        "reviewCount",
        "options",
        "optionCount",
        "categoryName",
        "brandName",
    }
)


def _validate_wire_dict(
    value: dict[str, Any], model: type[BaseModel], keys: frozenset[str]
) -> None:
    unknown = set(value) - keys
    if unknown:
        raise ValueError(f"runtime schema에 없는 wire field: {sorted(unknown)}")
    model.model_validate(value)


class NumericConstraint(CamelModel):
    candidate_field: NumericField
    operator: Operator
    threshold: int | float
    missing_policy: MissingPolicy


class CandidateJudgment(CamelModel):
    product_id: int
    outcome: Outcome
    failed_constraints: list[str] = Field(default_factory=list)
    missing_fields: list[NumericField] = Field(default_factory=list)


class DeterministicOracle(CamelModel):
    constraints: list[NumericConstraint] = Field(default_factory=list)
    candidate_judgments: list[CandidateJudgment]
    eligible_product_ids: list[int]
    ineligible_product_ids: list[int]
    unknown_product_ids: list[int]
    minimum_eligible_candidates: int = Field(default=1, ge=0)
    conflict_detected: bool


class BehavioralOracle(CamelModel):
    required_behavior: list[str] = Field(min_length=1)
    forbidden_claims: list[str] = Field(default_factory=list)
    required_disclosures: list[str] = Field(default_factory=list)
    authoritative_fields: list[str] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)
    judge_mode: Literal["rule", "manual", "hybrid"]
    human_review_focus: list[str] = Field(default_factory=list)


class Oracle(CamelModel):
    deterministic: DeterministicOracle
    behavioral: BehavioralOracle


class MutationChange(CamelModel):
    path: str = Field(min_length=1)
    operation: Literal["replace"] = "replace"
    before: Any
    after: Any


class Mutation(CamelModel):
    role: Literal["seed", "mutation", "contrast"]
    base_case_id: str | None
    target_field: str | None
    target_candidate_id: int | None = None
    changes: list[MutationChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _seed_or_mutation_shape(self) -> "Mutation":
        if self.role == "seed":
            if self.base_case_id is not None or self.changes:
                raise ValueError("seed mutation은 baseCaseId/changes를 가질 수 없습니다")
        elif self.base_case_id is None or not self.changes:
            raise ValueError("mutation/contrast는 baseCaseId와 changes가 필요합니다")
        return self


class EvalCase(CamelModel):
    schema_version: Literal["1.0.0"]
    dataset_version: str = Field(min_length=1)
    case_id: str = Field(pattern=r"^adv-[a-z_]+-[0-9]{2}-[a-z0-9_]+$")
    family_id: str = Field(pattern=r"^fam-[a-z_]+-[0-9]{2}$")
    category: Category
    difficulty: Difficulty
    capability_under_test: str = Field(min_length=1)
    test_type: Literal["MFT", "INV", "DIR"]
    user_request: dict[str, Any]
    candidates: list[dict[str, Any]] = Field(min_length=1)
    mutation: Mutation
    forbidden_behavior: list[str] = Field(min_length=1)
    oracle: Oracle

    @field_validator("user_request")
    @classmethod
    def _request_matches_buyer_contract(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_wire_dict(value, BuyerChatRequest, _REQUEST_KEYS)
        normalized = BuyerChatRequest.model_validate(value)
        if value.get("screen") is not None and normalized.screen is None:
            raise ValueError("buyer request에서 유효하지 않은 screen pageType입니다")
        return value

    @field_validator("candidates")
    @classmethod
    def _candidates_match_i1_contract(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        product_ids: list[int] = []
        for candidate in value:
            unknown = set(candidate) - _I1_CANDIDATE_KEYS
            if unknown:
                raise ValueError(f"I-1 후보 field가 아닌 키: {sorted(unknown)}")
            _validate_wire_dict(candidate, SpringProduct, _I1_CANDIDATE_KEYS)
            product_ids.append(SpringProduct.model_validate(candidate).product_id)
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("한 case의 candidates에 productId 중복을 둘 수 없습니다")
        return value


class OracleInput(CamelModel):
    constraints: list[NumericConstraint] = Field(default_factory=list)
    minimum_eligible_candidates: int = Field(default=1, ge=0)
    behavioral: BehavioralOracle


class SeedCaseSpec(CamelModel):
    case_id: str
    user_request: dict[str, Any]
    candidates: list[dict[str, Any]]
    oracle_input: OracleInput


class VariantSpec(CamelModel):
    case_id: str
    changes: list[MutationChange] = Field(min_length=1)
    oracle_input: OracleInput


class FamilySeed(CamelModel):
    family_id: str
    category: Category
    difficulty: Difficulty
    capability_under_test: str
    test_type: Literal["MFT", "INV", "DIR"]
    target_field: str | None = None
    target_candidate_id: int | None = None
    forbidden_behavior: list[str] = Field(min_length=1)
    base: SeedCaseSpec
    variants: list[VariantSpec] = Field(min_length=1)


class SeedDocument(CamelModel):
    schema_version: Literal["1.0.0"]
    dataset_version: str
    generated_at: str
    families: list[FamilySeed]
