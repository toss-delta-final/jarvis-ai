"""축·값·제약·케이스·기대동작의 pydantic 스키마 (이슈 #335).

규칙은 문서가 아니라 여기 스키마에 둔다 — `axes.json`·`cases/combo_cases.jsonl`·
`expected/expected_behavior.jsonl` 모두 이 모듈을 거쳐야 커밋된다(§3 "제약은 axes.json 에
기계 판독 형식으로 고정하고 생성기·검증 양쪽이 같은 데이터를 읽는다").
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 값 축 전부가 공유하는 "해당 없음" 센티널 — 제약이 어떤 축을 강제로 비울 때 쓴다
# (제약 인지 covering array 의 표준 처리, packet §2).
NA = "n/a"

ChecklistType = Literal["MFT", "INV", "DIR"]
ObservationMode = Literal["ci", "manual"]
ExpectedStatus = Literal["defined", "partial", "undefined"]
EvidenceKind = Literal["code", "spec", "test"]


class AxisValue(BaseModel):
    """축의 값 1개 — 근거 앵커(`source`)를 동봉한다."""

    model_config = ConfigDict(extra="forbid")

    value: str
    source: str = Field(description="file:line 또는 api-spec §x.y")
    note: str | None = None


class Axis(BaseModel):
    """직교 축 1개."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    values: list[AxisValue]

    @model_validator(mode="after")
    def _values_nonempty_and_unique(self) -> "Axis":
        if not self.values:
            raise ValueError(f"axis {self.id!r} has no values")
        seen = [v.value for v in self.values]
        if len(seen) != len(set(seen)):
            raise ValueError(f"axis {self.id!r} has duplicate values: {seen}")
        return self

    def value_ids(self) -> list[str]:
        return [v.value for v in self.values]


class ForcesConstraint(BaseModel):
    """`if` 조건이 참이면 `then` 의 축들이 정확히 그 값으로 강제된다 (보통 `NA`)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["forces"] = "forces"
    id: str
    description: str
    if_: dict[str, list[str]] = Field(alias="if")
    then: dict[str, str]


class RequiresAnyPresentConstraint(BaseModel):
    """`if` 조건이 참이면 `axes` 중 최소 1개는 `present` 여야 한다."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["requires_any_present"] = "requires_any_present"
    id: str
    description: str
    if_: dict[str, list[str]] = Field(alias="if")
    axes: list[str]


class ExcludesConstraint(BaseModel):
    """`if` 조건과 `forbid` 조건이 동시에 참인 조합은 유효하지 않다."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["excludes"] = "excludes"
    id: str
    description: str
    if_: dict[str, list[str]] = Field(alias="if")
    forbid: dict[str, list[str]]


Constraint = ForcesConstraint | RequiresAnyPresentConstraint | ExcludesConstraint


class RiskTriple(BaseModel):
    """3-wise 로 승격할 위험 축쌍(§2 "위험 축쌍의 3-wise 승격")."""

    model_config = ConfigDict(extra="forbid")

    id: str
    axes: list[str] = Field(min_length=3, max_length=3)
    note: str | None = None


class Exclusion(BaseModel):
    """v1 에서 의도적으로 뺀 값/축과 그 사유(§2 "screen 5종은 v1 제외")."""

    model_config = ConfigDict(extra="forbid")

    id: str
    reason: str


class DirectedCase(BaseModel):
    """greedy pairwise 가 안 뽑았지만 실측이 필요한 특정 축 조합을 데이터로 못박는다(리뷰 R3).

    2-wise 목표상 불필요해 greedy 가 고르지 않는 조합(예: `wishlist_add`×`member`×`spring_timeout`)
    도 "미정의 셀의 현행 동작을 직접 관측"해야 할 때가 있다 — 그 필요를 코드가 아니라 데이터로
    표현해, 생성기가 제약 검증을 거쳐 결정론으로 덧붙이게 한다. `axes` 는 **부분 할당**이어도
    된다 — 나머지 축은 `axes.json` 제약(forces)이 함의하는 값으로 결정론 완성된다.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    axes: dict[str, str]
    reason: str


class AxesDocument(BaseModel):
    """`axes.json` 전체 — 앵커 데이터 파일 (§2)."""

    model_config = ConfigDict(extra="forbid")

    dataset_version: str = Field(alias="datasetVersion")
    seed: int
    axes: list[Axis]
    constraints: list[
        Annotated[
            ForcesConstraint | RequiresAnyPresentConstraint | ExcludesConstraint,
            Field(discriminator="type"),
        ]
    ]
    risk_triples: list[RiskTriple] = Field(alias="riskTriples")
    exclusions: list[Exclusion] = Field(default_factory=list)
    directed_cases: list[DirectedCase] = Field(default_factory=list, alias="directedCases")

    @model_validator(mode="after")
    def _axis_ids_unique(self) -> "AxesDocument":
        ids = [a.id for a in self.axes]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate axis ids: {ids}")
        return self

    def axis_by_id(self) -> dict[str, Axis]:
        return {a.id: a for a in self.axes}


class ComboCase(BaseModel):
    """생성된 케이스 1건 (`cases/combo_cases.jsonl`, §3)."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^combo-\d{4}$")
    axes: dict[str, str]
    utterance: str
    checklist_type: ChecklistType
    label_required: bool
    observation_mode: ObservationMode
    linked: str | None = None
    perturbation_of: str | None = None

    @model_validator(mode="after")
    def _manual_has_linked(self) -> "ComboCase":
        if self.observation_mode == "manual" and not self.linked:
            raise ValueError(f"{self.case_id}: manual case must set `linked`")
        return self

    @model_validator(mode="after")
    def _perturbation_needs_non_mft(self) -> "ComboCase":
        if self.perturbation_of and self.checklist_type == "MFT":
            raise ValueError(f"{self.case_id}: perturbation_of is for INV/DIR pairs, not MFT")
        return self


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    ref: str


class ExpectedBehaviorRow(BaseModel):
    """`expected/expected_behavior.jsonl` 1행 (§4)."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^combo-\d{4}$")
    status: ExpectedStatus
    expected: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    # 셀 좌표(축값 조합) — 키는 axes.json 축 id **만** 허용한다. 좌표가 아닌 세부 구분은
    # `aspect` 로 뺀다(리뷰 R2) — 섞으면 UNDEFINED_CELLS.md 의 좌표 체계가 무너진다
    # (예: `aspect=zero_result_relaxation_and_clarify` 가 축인 것처럼 렌더되던 결함).
    # 축 id 자체와의 대조는 axes.json 을 아는 쪽(테스트)이 드리프트 가드로 강제한다.
    undefined_tuple: dict[str, str] | None = None
    # undefined_tuple 만으로 구분이 안 되는 세부 특성(예: "이 셀의 어느 부분이 갭인가") — 좌표가
    # 아니라 주석이다. UNDEFINED_CELLS.md 렌더는 이 값을 좌표 옆에 별도 표기한다.
    aspect: str | None = None
    observed: dict | None = None
    linked: str | None = None
    tracking: str | None = None

    @model_validator(mode="after")
    def _defined_requires_evidence_and_text(self) -> "ExpectedBehaviorRow":
        if self.status == "defined":
            if not self.evidence:
                raise ValueError(f"{self.case_id}: status=defined requires evidence >= 1")
            if not self.expected:
                raise ValueError(f"{self.case_id}: status=defined requires `expected` text")
        return self

    @model_validator(mode="after")
    def _undefined_or_partial_requires_tuple(self) -> "ExpectedBehaviorRow":
        if self.status in ("undefined", "partial") and self.undefined_tuple is None:
            raise ValueError(f"{self.case_id}: status={self.status} requires `undefined_tuple`")
        return self

    @model_validator(mode="after")
    def _manual_observed_is_null(self) -> "ExpectedBehaviorRow":
        if self.linked and self.observed is not None:
            raise ValueError(f"{self.case_id}: manual(linked) rows must leave `observed` null")
        return self


class Manifest(BaseModel):
    """`cases/manifest.json` — 재현 지문 (§1)."""

    model_config = ConfigDict(extra="forbid")

    dataset_version: str = Field(alias="datasetVersion")
    seed: int
    axes_sha256: str = Field(alias="axesSha256")
    cases_sha256: str = Field(alias="casesSha256")
    case_count: int = Field(alias="caseCount")
    generator_params: dict = Field(alias="generatorParams")
