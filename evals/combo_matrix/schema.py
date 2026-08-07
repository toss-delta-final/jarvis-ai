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

# `expected/expected_behavior.jsonl` 의 `observed` 는 러너를 실제로 돌려 기록한 관측값이라
# 커밋본과 재실행 결과가 드리프트해도 아무 테스트도 잡지 못한다(이슈 #424) — 다른 레인이 SSE
# 이벤트를 바꾸면 이 필드가 조용히 낡는다(실측 2회, 전부 `eventTypes` 하나만 바뀜). 그렇다고
# `observed` 전체를 byte diff 로 잠그면 SSE 를 건드리는 모든 레인(동시 6~8개)이 이 eval 데이터
# 재생성을 강제당한다 — 그래서 **핵심 계약 필드만** 골라 대조한다.
#
# 포함(핵심 계약 필드 — 바뀌면 파이프라인 동작이 실제로 바뀐 것이다):
#   terminal·finishReason·errorCode·actionType·actionReason — SSE 종료/오류/액션 계약
#   pushCount·pushProductCount·listType — push 결과 형태
#   searchCallCount·searchFilters·unappliedSearchFilters — 검색 경계 도달값(#381)
#   unhandledException — `_observe_chat` 안전망이 dict 로 낙성하는 회귀(§ runner.py 모듈 docstring)
#   outcome·itemCount·exception·statusCode — HOME(I-22) 계약
#   profileHookInvoked·buildReasonsInvoked·reasonsFilledCount·reasonsNull — HOME 계측
# 제외(다른 레인의 정상 작업이라 대조하면 소음이 된다):
#   eventTypes — SSE 이벤트 추가는 다른 레인의 정상 작업이고, 실측상 두 차례 드리프트가
#     전부 이 필드 하나였다(#396 progress emit 확장 등)
#   lastTokenText — 사용자 노출 문구(계약 아님, 문구 톤 조정이 정상 작업)
#   notes·note — 하네스가 붙이는 관측 한계 서술(관측 로직 자체는 계약이 아니다)
OBSERVED_GUARDED_FIELDS: frozenset[str] = frozenset(
    {
        "terminal",
        "finishReason",
        "errorCode",
        "actionType",
        "actionReason",
        "pushCount",
        "pushProductCount",
        "listType",
        "searchCallCount",
        "searchFilters",
        "unappliedSearchFilters",
        # [#426] attr_conditions 는 Spring payload 축이 아니라 AI 사후필터라 `searchFilters` 로는
        # 적용 여부를 알 수 없다 — 사후필터가 실제로 호출됐는지·무엇을 걸렀는지가 그 축의 유일한
        # 계약 신호이므로 가드 대상이다(attr 축 present 행에만 존재하고, 키 존재 여부까지 대조된다).
        "attrConditionsPostFilter",
        "unhandledException",
        "outcome",
        "itemCount",
        "exception",
        "statusCode",
        "profileHookInvoked",
        "buildReasonsInvoked",
        "reasonsFilledCount",
        "reasonsNull",
    }
)

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


PairCheckKind = Literal["INV", "DIR"]
PairCheckMode = Literal["ci", "manual"]
PairCheckDirection = Literal["non_increase", "non_decrease"]
PairCheckMetric = Literal["push_product_count"]
PairCheckGuard = Literal["perturbed_filters_strict_superset", "base_count_positive"]


class PairCheckSpec(BaseModel):
    """INV/DIR 쌍 검증 앵커 1건 (`expected/pair_checks.jsonl`, 이슈 #371 §3-a).

    `case_id` 는 변형(perturbation) 케이스를 가리킨다 — 원본은 그 케이스의
    `ComboCase.perturbation_of` 로 찾는다(둘을 이 스키마에 중복해 두지 않는다).
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^combo-\d{4}$")
    kind: PairCheckKind
    mode: PairCheckMode
    invariant_fields: list[str] | None = None
    metric: PairCheckMetric | None = None
    direction: PairCheckDirection | None = None
    guards: list[PairCheckGuard] = Field(default_factory=list)
    reason: str
    link: str | None = None

    @model_validator(mode="after")
    def _kind_field_shape(self) -> "PairCheckSpec":
        if self.kind == "INV":
            if self.metric is not None or self.direction is not None:
                raise ValueError(f"{self.case_id}: kind=INV must not set metric/direction")
        else:  # DIR
            if self.invariant_fields is not None:
                raise ValueError(f"{self.case_id}: kind=DIR must not set invariant_fields")
        return self

    @model_validator(mode="after")
    def _mode_requirements(self) -> "PairCheckSpec":
        if self.mode == "manual":
            if not self.link:
                raise ValueError(f"{self.case_id}: mode=manual requires `link`")
        else:  # ci
            if self.kind == "INV" and not self.invariant_fields:
                raise ValueError(f"{self.case_id}: mode=ci kind=INV requires invariant_fields")
            if self.kind == "DIR" and (self.metric is None or self.direction is None):
                raise ValueError(f"{self.case_id}: mode=ci kind=DIR requires metric+direction")
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
