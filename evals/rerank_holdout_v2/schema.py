"""Strict wire contracts for the prospective rerank holdout."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = "1.0.0"
DATASET_VERSION = "1.0.0"
DEFAULT_SEED = 631200
RANKING_CASE_COUNT = 200
SAFETY_CASE_COUNT = 24
CANDIDATE_COUNT = 30

LabelPolicy = Literal["none", "draft", "sealed"]
LabelStatus = Literal["draft", "sealed"]
Stratum = Literal[
    "general",
    "budget_multi",
    "personalization",
    "repurchase",
    "long_tail",
    "adversarial",
]
CandidateSource = Literal[
    "exact_category",
    "near_category",
    "wrong_brand",
    "constraint_violation",
    "random_catalog",
]
SafetyScenario = Literal[
    "catalog_prompt_injection",
    "hard_constraint_integrity",
    "candidate_set_integrity",
]

_CASE_ID_RE = re.compile(r"^rh2-[a-z_]+-\d{4}$")
_FAMILY_ID_RE = re.compile(r"^rh2-fam-[a-z0-9-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_FIELDS = frozenset(
    {
        "relevantProductIds",
        "relevanceGrades",
        "idealOrder",
        "hardConstraints",
        "mustExcludeProductIds",
        "labelRationale",
        "labelStatus",
        "labelSource",
    }
)


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class Identity(CamelModel):
    kind: Literal["guest", "member"]


class CandidateOrigin(CamelModel):
    source: CandidateSource
    detail: str = Field(min_length=1)


class HardConstraints(CamelModel):
    price_max: int | None = Field(default=None, ge=0)
    price_min: int | None = Field(default=None, ge=0)
    forbidden_categories: list[str] = Field(default_factory=list)
    forbidden_product_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_price_range(self) -> HardConstraints:
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("priceMin must not exceed priceMax")
        if len(self.forbidden_categories) != len(set(self.forbidden_categories)):
            raise ValueError("forbiddenCategories must be unique")
        if len(self.forbidden_product_ids) != len(set(self.forbidden_product_ids)):
            raise ValueError("forbiddenProductIds must be unique")
        return self


class RankingCaseCore(CamelModel):
    case_id: str
    family_id: str
    schema_version: Literal["1.0.0"]
    dataset_version: Literal["1.0.0"]
    split: Literal["prospective_holdout"]
    stratum: Stratum
    variant: str = Field(min_length=1)
    slices: list[str] = Field(min_length=1)
    query: str = Field(min_length=1)
    identity: Identity
    profile_summary: str | None = None
    candidate_product_ids: list[int]
    candidate_provenance: dict[int, CandidateOrigin]
    catalog_sha256: str
    provenance: Literal["synthetic-catalog-derived"]

    @model_validator(mode="before")
    @classmethod
    def _reject_embedded_labels(cls, value: object) -> object:
        if isinstance(value, Mapping) and _LABEL_FIELDS & set(value):
            raise ValueError("ranking core contains label fields")
        return value

    @field_validator("case_id")
    @classmethod
    def _stable_case_id(cls, value: str) -> str:
        if not _CASE_ID_RE.fullmatch(value):
            raise ValueError("caseId must match rh2-<stratum>-<4 digits>")
        return value

    @field_validator("family_id")
    @classmethod
    def _stable_family_id(cls, value: str) -> str:
        if not _FAMILY_ID_RE.fullmatch(value):
            raise ValueError("familyId must match rh2-fam-<slug>")
        return value

    @field_validator("catalog_sha256")
    @classmethod
    def _catalog_hash_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("catalogSha256 must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _candidate_and_identity_contract(self) -> RankingCaseCore:
        candidate_ids = self.candidate_product_ids
        if len(candidate_ids) != CANDIDATE_COUNT or len(set(candidate_ids)) != CANDIDATE_COUNT:
            raise ValueError("candidateProductIds must contain exactly 30 distinct IDs")
        if set(self.candidate_provenance) != set(candidate_ids):
            raise ValueError("candidateProvenance must exactly cover candidateProductIds")
        if len(self.slices) != len(set(self.slices)):
            raise ValueError("slices must be unique")
        required_slices = {"ranking", self.stratum, self.identity.kind}
        if not required_slices <= set(self.slices):
            raise ValueError(f"slices must include {sorted(required_slices)}")
        if self.identity.kind == "guest" and self.profile_summary is not None:
            raise ValueError("guest profileSummary must be null")
        if self.identity.kind == "member" and not self.profile_summary:
            raise ValueError("member profileSummary must be non-empty")
        return self


class _LabelFields(CamelModel):
    case_id: str
    relevant_product_ids: list[int]
    relevance_grades: dict[int, int]
    ideal_order: list[int]
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    must_exclude_product_ids: list[int] = Field(default_factory=list)
    label_rationale: str = Field(min_length=1)

    @field_validator("case_id")
    @classmethod
    def _stable_case_id(cls, value: str) -> str:
        if not _CASE_ID_RE.fullmatch(value):
            raise ValueError("caseId must match rh2-<stratum>-<4 digits>")
        return value

    @model_validator(mode="after")
    def _consistent_labels(self) -> _LabelFields:
        relevant = self.relevant_product_ids
        if not relevant or len(relevant) > 6 or len(relevant) != len(set(relevant)):
            raise ValueError("relevantProductIds must contain one to six distinct IDs")
        if set(self.relevance_grades) != set(relevant):
            raise ValueError("relevanceGrades must exactly cover relevantProductIds")
        if any(grade < 1 or grade > 3 for grade in self.relevance_grades.values()):
            raise ValueError("relevanceGrades must be between 1 and 3")
        if 3 not in self.relevance_grades.values():
            raise ValueError("relevanceGrades must include at least one grade 3")
        if len(self.ideal_order) != len(set(self.ideal_order)) or set(self.ideal_order) != set(
            relevant
        ):
            raise ValueError("idealOrder must be a permutation of relevantProductIds")
        excluded = set(self.must_exclude_product_ids)
        if len(excluded) != len(self.must_exclude_product_ids):
            raise ValueError("mustExcludeProductIds must be unique")
        if excluded & set(relevant):
            raise ValueError("positive and must-exclude product IDs must be disjoint")
        return self


class DraftLabels(_LabelFields):
    label_status: Literal["draft"]
    label_source: Literal["heuristic"]


class SealedLabels(_LabelFields):
    label_status: Literal["sealed"]
    label_source: Literal["human-reviewed"]
    reviewer_ids: list[str] = Field(min_length=2, max_length=2)
    adjudicator_id: str | None = None
    sealed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T")

    @field_validator("reviewer_ids")
    @classmethod
    def _reviewers_are_independent(cls, value: list[str]) -> list[str]:
        if len(set(value)) != 2 or any(not reviewer.strip() for reviewer in value):
            raise ValueError("sealed labels require two independent reviewer IDs")
        return value


class CandidateOverride(CamelModel):
    name: str | None = None
    summary: str | None = None

    @model_validator(mode="after")
    def _changes_candidate_text(self) -> CandidateOverride:
        if self.name is None and self.summary is None:
            raise ValueError("candidate override must change name or summary")
        return self


class SafetyCase(CamelModel):
    case_id: str
    schema_version: Literal["1.0.0"]
    dataset_version: Literal["1.0.0"]
    scenario: SafetyScenario
    query: str = Field(min_length=1)
    identity: Identity
    profile_summary: str | None = None
    candidate_product_ids: list[int] = Field(min_length=2)
    candidate_overrides: dict[int, CandidateOverride] = Field(default_factory=dict)
    expected_invariant: str = Field(min_length=1)
    must_exclude_product_ids: list[int] = Field(default_factory=list)
    catalog_sha256: str

    @field_validator("case_id")
    @classmethod
    def _safety_case_id(cls, value: str) -> str:
        if not re.fullmatch(r"^rh2-safe-[a-z_]+-\d{4}$", value):
            raise ValueError("invalid safety caseId")
        return value

    @field_validator("catalog_sha256")
    @classmethod
    def _catalog_hash_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("catalogSha256 must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _candidate_refs_are_consistent(self) -> SafetyCase:
        candidate_ids = set(self.candidate_product_ids)
        if len(candidate_ids) != len(self.candidate_product_ids):
            raise ValueError("safety candidateProductIds must be unique")
        if not set(self.candidate_overrides) <= candidate_ids:
            raise ValueError("candidateOverrides must reference candidateProductIds")
        if not set(self.must_exclude_product_ids) <= candidate_ids:
            raise ValueError("mustExcludeProductIds must reference candidateProductIds")
        if self.identity.kind == "guest" and self.profile_summary is not None:
            raise ValueError("guest profileSummary must be null")
        if self.identity.kind == "member" and not self.profile_summary:
            raise ValueError("member profileSummary must be non-empty")
        return self


class DatasetManifest(CamelModel):
    schema_version: Literal["1.0.0"]
    dataset_version: Literal["1.0.0"]
    seed: int
    catalog_source_path: str = Field(min_length=1)
    catalog_sha256: str
    dataset_hash: str
    ranking_count: int = Field(ge=0)
    safety_count: int = Field(ge=0)
    identity_counts: dict[str, int]
    stratum_counts: dict[str, int]
    label_status: LabelStatus
    confirmatory_eligible: bool
    file_hashes: dict[str, str]

    @field_validator("catalog_sha256", "dataset_hash")
    @classmethod
    def _catalog_hash_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("catalogSha256 must be a lowercase SHA-256")
        return value

    @field_validator("file_hashes")
    @classmethod
    def _file_hashes_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for relative, digest in value.items():
            if not relative or relative.startswith("/") or ".." in relative.split("/"):
                raise ValueError("fileHashes paths must remain inside the dataset root")
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError("fileHashes values must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _eligibility_matches_label_status(self) -> DatasetManifest:
        expected = self.label_status == "sealed"
        if self.confirmatory_eligible != expected:
            raise ValueError("confirmatoryEligible must be true only for sealed labels")
        return self


class LoadedDataset(NamedTuple):
    manifest: DatasetManifest
    ranking_cases: tuple[RankingCaseCore, ...]
    labels_by_case: Mapping[str, DraftLabels | SealedLabels]
    safety_cases: tuple[SafetyCase, ...]
    catalog: Mapping[str, dict[str, object]]
