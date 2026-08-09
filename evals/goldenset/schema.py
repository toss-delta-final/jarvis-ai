"""구매자 추천 골든셋 스키마와 교차 fixture 검증."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.core.config import Settings, get_settings
from app.schemas.spring import ProductSearchFilters

SCHEMA_VERSION = "2.1.0"
DATASET_VERSION = "2.3.0"
CASE_ID_RE = re.compile(r"^buy-[a-z][a-z0-9_]*-\d{4}$")
# 니즈 축(신원 아님) — 케이스당 정확히 1개(disjoint, §2.1). guest/member 신원 슬라이스와는 별도 축이다.
NEEDS_SLICES = frozenset({"single_need", "multi_constraint", "budget", "repurchase"})
ALLOWED_SLICES = frozenset(
    {
        "search",
        "guest",
        "member",
        "cold_start",
        "personalization",
        "personalization_overreach",
        "multi_constraint",
        "repurchase",
        "single_need",
        "budget",
        "category_mapping_failure",
        "failure",
    }
)
ALLOWED_ROUTES = frozenset({"recommend", "cart_add", "cart_view", "order_status", "general"})
LABEL_FIELDS = frozenset(
    {
        "relevantProductIds",
        "relevanceGrades",
        "idealOrder",
        "hardConstraints",
        "mustExcludeProductIds",
    }
)
CANDIDATE_SOURCES = frozenset({"golden_filter", "injected"})
CANDIDATE_RULES = frozenset(
    {
        "semantic_near",
        "price_violation",
        "attr_violation",
        "category_violation",
        "other_brand",
        "broadened_search",
        "random_catalog",
    }
)
# 위반 네거티브 채널(#370) — 하드 네거티브(semantic_near 등, "유사하지만 오답")와 별도로,
# 케이스의 하드 제약을 실제로 위반하는 후보만 모은 부분집합이다.
VIOLATION_CANDIDATE_RULES = frozenset({"price_violation", "category_violation", "attr_violation"})
LABEL_SOURCES = frozenset({"human", "model", "heuristic", "unknown"})
NARROW_DOMAIN_NOTE_PREFIX = "narrow-domain:"
INJECTED_RELEVANT_APPROVED_NOTE_PREFIX = "injected-relevant-approved:"
RELEVANT_RATIO_EXEMPT_NOTE_PREFIX = "relevant-ratio-exempt:"
# notes 접두부에 여러 마커를 이어붙일 수 있다(예: "narrow-domain: relevant-ratio-exempt: ...").
# 마커는 소문자·하이픈으로만 구성되고 ":"로 끝나는 토큰이 연속으로 나오는 구간까지만 접두부로 본다.
_NOTE_PREFIX_MARKERS_RE = re.compile(r"^(?:[a-z][a-z-]*:\s*)+")
_NOTE_MARKER_TOKEN_RE = re.compile(r"([a-z][a-z-]*):\s*")


class CamelModel(BaseModel):
    """eval 내부 모델도 앱 와이어와 같은 camelCase 별칭 규약을 쓴다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Identity(CamelModel):
    """케이스 실행 신원."""

    kind: Literal["member", "guest"]
    persona_id: str | None = None

    @model_validator(mode="after")
    def _guest_has_no_history(self) -> "Identity":
        if self.kind == "guest" and self.persona_id is not None:
            raise ValueError("guest identity에는 구매 이력 personaId를 둘 수 없습니다")
        return self


class HardConstraints(CamelModel):
    """노출 결과가 반드시 지켜야 하는 결정론 제약."""

    price_max: int | None = None
    price_min: int | None = None
    forbidden_categories: list[str] = Field(default_factory=list)
    forbidden_product_ids: list[int] = Field(default_factory=list)

    @field_validator("forbidden_categories")
    @classmethod
    def _categories_use_i1_notation(cls, value: list[str]) -> list[str]:
        if any(" > " in category for category in value):
            raise ValueError(
                "forbiddenCategories는 DB 계층 표기가 아니라 I-1 categoryName 표기를 써야 합니다"
            )
        return value

    @model_validator(mode="after")
    def _price_range_is_ordered(self) -> "HardConstraints":
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("priceMin은 priceMax 이하여야 합니다")
        return self


class CaseCore(CamelModel):
    """dev와 봉인 holdout이 공유하는 비라벨 필드."""

    case_id: str
    schema_version: Literal["2.1.0"]
    dataset_version: Literal["2.3.0"]
    split: Literal["dev", "holdout"]
    slices: list[str]
    query: str = Field(min_length=1)
    query_type: Literal["simple", "conditional", "comparison", "multi_constraint"]
    identity: Identity
    expected_route: str
    expected_filters: dict[str, object] = Field(default_factory=dict)
    search_fixture_id: str | None = None
    provenance: Literal["curated", "synthetic", "production-derived"]
    labeler: str = Field(min_length=1)
    adjudicator: str | None = None
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: str = Field(min_length=1)
    # 라벨 provenance(#370) — 이 케이스의 relevantProductIds 등 라벨을 누가/언제/무슨 근거로
    # 붙였는지. dev·holdout core 파일 양쪽에 있어야 하므로(비공개 holdout 라벨과 무관)
    # GoldenCase가 아니라 CaseCore에 둔다.
    label_source: Literal["human", "model", "heuristic", "unknown"]
    labeled_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    label_rationale: str = Field(min_length=1)
    # CheckList 형식(#328 공통 규약): MFT는 라벨 필요, INV/DIR은 라벨 없이 규모를 늘리는 축이다.
    test_type: Literal["MFT", "INV", "DIR"] = "MFT"
    # INV/DIR 케이스를 묶는 검사 단위. 같은 group의 케이스들을 evals.metrics.behavior가 비교한다.
    behavior_group_id: str | None = None
    behavior_kind: (
        Literal["color_synonym", "word_order", "constraint_subset", "member_recall_ge_guest"] | None
    ) = None

    @field_validator("case_id")
    @classmethod
    def _case_id_is_stable(cls, value: str) -> str:
        if not CASE_ID_RE.fullmatch(value):
            raise ValueError("caseId는 buy-<slice>-<4자리> 형식이어야 합니다")
        return value

    @field_validator("slices")
    @classmethod
    def _slices_are_allowlisted(cls, value: list[str]) -> list[str]:
        if not value or not set(value) <= ALLOWED_SLICES:
            raise ValueError("slices는 비어 있지 않은 허용 집합이어야 합니다")
        if len(value) != len(set(value)):
            raise ValueError("slices에 중복을 둘 수 없습니다")
        return value

    @field_validator("expected_route")
    @classmethod
    def _route_is_allowlisted(cls, value: str) -> str:
        if value not in ALLOWED_ROUTES:
            raise ValueError("expectedRoute가 RouteDecision.intent 허용값이 아닙니다")
        return value

    @field_validator("expected_filters")
    @classmethod
    def _filters_match_search_contract(cls, value: dict[str, object]) -> dict[str, object]:
        allowed = {
            field.alias or to_camel(name)
            for name, field in ProductSearchFilters.model_fields.items()
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"expectedFilters에 알 수 없는 키가 있습니다: {sorted(unknown)}")
        ProductSearchFilters.model_validate(value)
        return value

    @model_validator(mode="after")
    def _guest_slice_matches_identity(self) -> "CaseCore":
        is_guest = self.identity.kind == "guest"
        has_guest_slice = "guest" in self.slices
        if is_guest != has_guest_slice:
            raise ValueError("guest identity와 guest slice는 항상 함께 있어야 합니다")
        return self

    @model_validator(mode="after")
    def _member_slice_matches_identity(self) -> "CaseCore":
        is_member = self.identity.kind == "member"
        has_member_slice = "member" in self.slices
        if is_member != has_member_slice:
            raise ValueError("member identity와 member slice는 항상 함께 있어야 합니다")
        return self

    @model_validator(mode="after")
    def _needs_slice_is_exactly_one(self) -> "CaseCore":
        present = NEEDS_SLICES & set(self.slices)
        if len(present) != 1:
            raise ValueError(
                "니즈 슬라이스(single_need/multi_constraint/budget/repurchase)는 "
                f"케이스당 정확히 1개여야 합니다: {sorted(present)}"
            )
        return self


class HoldoutCase(CaseCore):
    """튜닝 중 읽을 수 있는 라벨 없는 holdout 케이스."""

    @model_validator(mode="before")
    @classmethod
    def _labels_are_not_embedded(cls, value: object) -> object:
        if isinstance(value, dict) and LABEL_FIELDS & set(value):
            raise ValueError("holdout 케이스 파일에는 라벨 필드를 넣을 수 없습니다")
        return value

    @model_validator(mode="after")
    def _split_is_holdout(self) -> "HoldoutCase":
        if self.split != "holdout":
            raise ValueError("HoldoutCase split은 holdout이어야 합니다")
        return self


class HoldoutLabels(CamelModel):
    """별도 봉인 파일에 저장하는 holdout 라벨."""

    case_id: str
    relevant_product_ids: list[int] = Field(default_factory=list)
    relevance_grades: dict[int, int] = Field(default_factory=dict)
    ideal_order: list[int] = Field(default_factory=list)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    must_exclude_product_ids: list[int] = Field(default_factory=list)
    notes: str = Field(min_length=1)

    @field_validator("case_id")
    @classmethod
    def _case_id_is_stable(cls, value: str) -> str:
        if not CASE_ID_RE.fullmatch(value):
            raise ValueError("caseId는 buy-<slice>-<4자리> 형식이어야 합니다")
        return value

    @model_validator(mode="after")
    def _labels_are_consistent(self) -> "HoldoutLabels":
        _validate_labels(self)
        return self


class GoldenCase(CaseCore):
    """라벨이 결합된 dev 또는 release 평가용 holdout 케이스."""

    relevant_product_ids: list[int] = Field(default_factory=list)
    relevance_grades: dict[int, int] = Field(default_factory=dict)
    ideal_order: list[int] = Field(default_factory=list)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    must_exclude_product_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _labels_are_consistent(self) -> "GoldenCase":
        _validate_labels(self)
        # INV/DIR은 라벨 없이 규모를 늘리는 축(#328 공통 규약)이라 MFT의 "search면 정답 필수"
        # 규칙에서 제외한다 — evals.metrics.behavior가 라벨과 무관하게 별도로 검사한다.
        if self.test_type == "MFT" and "search" in self.slices and not self.relevant_product_ids:
            raise ValueError("search slice의 relevantProductIds는 비어 있을 수 없습니다")
        return self


def _validate_labels(case: GoldenCase | HoldoutLabels) -> None:
    relevant = set(case.relevant_product_ids)
    excluded = set(case.must_exclude_product_ids)
    if relevant & excluded:
        raise ValueError("relevantProductIds와 mustExcludeProductIds 교집합은 비어야 합니다")
    if len(case.ideal_order) != len(set(case.ideal_order)) or set(case.ideal_order) != relevant:
        raise ValueError("idealOrder는 relevantProductIds와 정확히 같은 집합이어야 합니다")
    grades = case.relevance_grades
    if not relevant <= set(grades):
        raise ValueError("relevanceGrades는 relevantProductIds 전부를 덮어야 합니다")
    for product_id in relevant:
        if grades[product_id] < 1:
            raise ValueError("relevantProductIds의 등급은 1 이상이어야 합니다")
        if grades[product_id] > 3:
            raise ValueError("relevanceGrades 값은 3 이하이어야 합니다")
    if any(grade < 0 or grade > 3 for grade in grades.values()):
        raise ValueError("relevanceGrades 값은 0 이상 3 이하이어야 합니다")


class Candidate(CamelModel):
    """search fixture 후보 한 건의 provenance(§2.1 v2 포맷)."""

    product_id: int
    source: Literal["golden_filter", "injected"]
    rule: (
        Literal[
            "semantic_near",
            "price_violation",
            "attr_violation",
            "category_violation",
            "other_brand",
            "broadened_search",
            "random_catalog",
        ]
        | None
    ) = None
    # 원문 "from" 은 파이썬 예약어라 origin으로 받고 별칭만 "from"으로 고정한다.
    origin: str = Field(alias="from", min_length=1)


class SearchFixture(CamelModel):
    """fixtures/search_responses.json 항목 하나(v2, candidates provenance 필수)."""

    request: dict[str, object]
    product_ids: list[int]
    total_count: int
    recorded_at: str
    source: str
    candidates: list[Candidate]

    @model_validator(mode="after")
    def _product_ids_match_candidate_union(self) -> "SearchFixture":
        expected = sorted({candidate.product_id for candidate in self.candidates})
        if self.product_ids != expected:
            raise ValueError(
                "productIds는 candidates를 productId 오름차순으로 중복 제거한 목록과 같아야 합니다"
                "(no-op 기준선의 정의)"
            )
        return self


def validate_search_fixture(fixture_id: str, payload: dict) -> SearchFixture:
    """fixture 항목 하나를 v2 candidates provenance 포맷으로 검증한다."""
    try:
        return SearchFixture.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"{fixture_id}: 검색 fixture 검증 실패: {exc}") from exc


def all_candidates_are_correct(
    candidate_product_ids: Iterable[int], relevant_product_ids: Iterable[int]
) -> bool:
    """후보 집합이 정답 집합에 완전히 포함될 때만 '전부 정답'(비판별)이다.

    개수 비교(candidateCount<=relevantCount)만으로는 후보에 오답이 섞여 있어도 후보 수가
    우연히 정답 수 이하이면 비판별로 오분류한다 — 반드시 집합 포함 관계로 판정해야 한다
    (#333 리뷰 라운드1 F-1). `schema.validate_cases`·`migrate_v1_to_v2.py`·`audit.py`가 모두
    이 함수 하나를 호출한다.
    """
    return set(candidate_product_ids) <= set(relevant_product_ids)


def has_note_marker(notes: str, marker: str) -> bool:
    """notes 접두부에 marker(콜론 없이)가 있는지 확인한다.

    여러 마커를 이어붙일 수 있다(예: ``"narrow-domain: relevant-ratio-exempt: 근거..."``).
    접두부는 ``"<마커>: "`` 토큰이 문자열 시작부터 연속으로 나오는 구간까지다.
    """
    name = marker.rstrip(":").strip()
    match = _NOTE_PREFIX_MARKERS_RE.match(notes)
    if not match:
        return False
    return name in _NOTE_MARKER_TOKEN_RE.findall(match.group(0))


def load_jsonl(path: Path, model: type[BaseModel]) -> list[BaseModel]:
    """UTF-8 JSONL을 주어진 pydantic 모델 목록으로 검증해 읽는다."""
    rows: list[BaseModel] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(model.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f"{path}:{line_number} 검증 실패: {exc}") from exc
    return rows


def judge_attr_violation(
    attr_conditions: Mapping[str, str], product_attributes: Mapping[str, object]
) -> bool:
    """``attrConditions`` 조건을 catalog ``attributes``로 위반 판정한다(#370, 명시적 함수).

    조건 키가 상품 ``attributes``에 그대로(정확히) 있고 값이 다를 때만 위반으로 판정한다.
    조건 키가 상품에 아예 없으면(예: 케이스의 ``차단지수``가 catalog의 ``SPF지수``처럼 다른
    이름으로만 존재) 위반 여부를 판정할 근거가 없으므로 False다 — 키 이름을 임의로 동의어
    매핑하지 않는다(사후 추정 금지, 패킷 §B 소급 원칙과 같은 정신).
    """
    if not attr_conditions:
        return False
    for key, expected in attr_conditions.items():
        if key in product_attributes and product_attributes[key] != expected:
            return True
    return False


def _validate_violation_candidate(
    case: GoldenCase,
    candidate: Candidate,
    *,
    product: Mapping[str, object],
) -> None:
    """rule이 위반 네거티브 채널(가격/카테고리/속성) 중 하나인 candidate 한 건을 검증한다(#370).

    위반 태그 후보는 정답이 될 수 없다 — 기존 ``injected-relevant-approved`` 마커로도
    우회 불가로 한다(패킷 §A.4). 각 rule은 실제로 그 위반이 성립함을 catalog로 판정한다.
    """
    if candidate.product_id in set(case.relevant_product_ids):
        raise ValueError(
            f"{case.case_id}: 위반 네거티브 후보 {candidate.product_id}가 "
            "relevantProductIds에 있습니다 — 위반 후보는 어떤 마커로도 정답이 될 수 없습니다"
        )
    hard = case.hard_constraints
    if candidate.rule == "price_violation":
        raw_price = product.get("price")
        if not isinstance(raw_price, (int, float)):
            raise ValueError(
                f"{case.case_id}: price_violation 후보 {candidate.product_id}의 catalog 가격이 "
                "없습니다 — 위반 태그 후보는 가격이 필수입니다"
            )
        price = raw_price
        violates = (hard.price_max is not None and price > hard.price_max) or (
            hard.price_min is not None and price < hard.price_min
        )
        if not violates:
            raise ValueError(
                f"{case.case_id}: price_violation 후보 {candidate.product_id}(가격 {price})가 "
                f"priceMax={hard.price_max}/priceMin={hard.price_min}를 위반하지 않습니다"
            )
    elif candidate.rule == "category_violation":
        category_name = product.get("categoryName")
        excluded_ids = set(hard.forbidden_product_ids) | set(case.must_exclude_product_ids)
        violates = category_name in set(hard.forbidden_categories) or (
            candidate.product_id in excluded_ids
        )
        if not violates:
            raise ValueError(
                f"{case.case_id}: category_violation 후보 {candidate.product_id}(카테고리 "
                f"{category_name!r})가 forbiddenCategories에도, forbiddenProductIds ∪ "
                "mustExcludeProductIds에도 속하지 않습니다"
            )
    elif candidate.rule == "attr_violation":
        attr_conditions = case.expected_filters.get("attrConditions")
        if not isinstance(attr_conditions, dict) or not attr_conditions:
            raise ValueError(
                f"{case.case_id}: attr_violation 후보 {candidate.product_id}가 있지만 "
                "expectedFilters.attrConditions가 없습니다"
            )
        product_attributes = product.get("attributes")
        if not isinstance(product_attributes, dict) or not judge_attr_violation(
            attr_conditions, product_attributes
        ):
            raise ValueError(
                f"{case.case_id}: attr_violation 후보 {candidate.product_id}가 "
                f"attrConditions {attr_conditions}를 위반함을 catalog attributes로 판정할 수 "
                "없습니다"
            )


def validate_cases(
    cases: list[GoldenCase],
    *,
    catalog: dict[str, dict],
    search_responses: dict[str, dict],
    purchase_history: dict[str, dict],
    config: Settings | None = None,
) -> None:
    """케이스 간 유일성과 외부 fixture 참조·가격 불변식을 검증한다."""
    settings = config or get_settings()
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("caseId가 전역에서 중복되었습니다")
    if not settings.goldenset_min_cases <= len(cases) <= settings.goldenset_max_cases:
        raise ValueError(
            f"케이스 수 {len(cases)}가 허용 범위 "
            f"{settings.goldenset_min_cases}~{settings.goldenset_max_cases} 밖입니다"
        )
    parsed_fixtures = {
        fixture_id: validate_search_fixture(fixture_id, payload)
        for fixture_id, payload in search_responses.items()
    }
    catalog_ids = {int(key) for key in catalog}
    catalog_categories = {
        product.get("categoryName") for product in catalog.values() if product.get("categoryName")
    }
    # #370 — 위반 태그 후보는 하드 제약을 판정할 단일 소유 케이스가 있어야 한다. 같은 fixture를
    # 2개 이상의 케이스가 공유하면(INV color_synonym/word_order 쌍 등) 어느 케이스 기준으로
    # 위반을 판정해야 할지 모호해진다(GUIDE 관측 절 참조) — 위반 태그 후보는 그런 fixture에
    # 넣을 수 없다.
    cases_by_id = {case.case_id: case for case in cases}
    fixture_owner_case_ids: dict[str, list[str]] = {}
    for case in cases:
        if case.search_fixture_id:
            fixture_owner_case_ids.setdefault(case.search_fixture_id, []).append(case.case_id)
    for fixture_id, fixture in parsed_fixtures.items():
        violation_candidates = [
            candidate
            for candidate in fixture.candidates
            if candidate.rule in VIOLATION_CANDIDATE_RULES
        ]
        if not violation_candidates:
            continue
        owners = fixture_owner_case_ids.get(fixture_id, [])
        if len(owners) != 1:
            raise ValueError(
                f"{fixture_id}: 위반 네거티브 후보가 있는 fixture는 정확히 1개 케이스가 단독 "
                f"사용해야 합니다(현재 {len(owners)}개: {owners})"
            )
        owner = cases_by_id[owners[0]]
        if owner.test_type != "MFT":
            raise ValueError(
                f"{fixture_id}: 위반 네거티브 후보의 단독 소유 케이스({owner.case_id})는 "
                f"MFT여야 합니다(현재 {owner.test_type})"
            )
        for candidate in violation_candidates:
            product = catalog.get(str(candidate.product_id))
            if product is None:
                # catalog 실존 검증은 위 F-2 루프가 이미 전담하므로 여기서는 건너뛴다
                # (이중 오류 메시지 방지).
                continue
            _validate_violation_candidate(owner, candidate, product=product)
    # F-2(#333 리뷰): fixture candidate는 라벨된 id뿐 아니라 전원 catalog_snapshot에 실존해야
    # 한다 — 존재하지 않으면 가격 HCV·diversity·라벨 워크시트 계산이 그 후보에서 전부 불가능해진다.
    for fixture_id, fixture in parsed_fixtures.items():
        missing_candidates = sorted(
            {candidate.product_id for candidate in fixture.candidates} - catalog_ids
        )
        if missing_candidates:
            raise ValueError(
                f"{fixture_id}: catalog에 없는 candidate productId {missing_candidates}"
            )
    for case in cases:
        labeled_ids = set(case.relevant_product_ids) | set(case.must_exclude_product_ids)
        missing = labeled_ids - catalog_ids
        if missing:
            raise ValueError(f"{case.case_id}: 카탈로그에 없는 productId {sorted(missing)}")
        if case.search_fixture_id and case.search_fixture_id not in search_responses:
            raise ValueError(f"{case.case_id}: 검색 fixture가 없습니다")
        fixture = parsed_fixtures.get(case.search_fixture_id) if case.search_fixture_id else None
        if fixture is not None:
            injected_ids = {
                candidate.product_id
                for candidate in fixture.candidates
                if candidate.source == "injected"
            }
            offending = sorted(set(case.relevant_product_ids) & injected_ids)
            if offending and not has_note_marker(case.notes, "injected-relevant-approved"):
                raise ValueError(
                    f"{case.case_id}: injected 후보가 relevantProductIds에 있습니다 {offending} "
                    f"— notes에 '{INJECTED_RELEVANT_APPROVED_NOTE_PREFIX}' 승인 문구가 필요합니다"
                )
            candidate_count = len(fixture.product_ids)
            # F-1(#333 리뷰): 개수 비교가 아니라 집합 포함으로 "전부 정답"을 판정한다 — 오답
            # 후보가 섞여 있는데 후보 수가 우연히 정답 수 이하면 개수 비교는 이를 놓친다.
            all_candidates_correct = all_candidates_are_correct(
                fixture.product_ids, case.relevant_product_ids
            )
            ranking_eligible = (
                case.test_type == "MFT"
                and "search" in case.slices
                and bool(case.relevant_product_ids)
                and not all_candidates_correct
            )
            if (
                ranking_eligible
                and candidate_count < settings.goldenset_min_ranking_candidates
                and not has_note_marker(case.notes, "narrow-domain")
            ):
                raise ValueError(
                    f"{case.case_id}: 순위 평가 후보 수 {candidate_count}가 "
                    f"goldenset_min_ranking_candidates({settings.goldenset_min_ranking_candidates}) "
                    f"미만입니다 — notes에 '{NARROW_DOMAIN_NOTE_PREFIX}' 예외 문구가 필요합니다"
                )
            if ranking_eligible:
                # F-5-1(#333 리뷰, #329 권고 3): 등급≥1(정답) 후보 비율이 너무 높으면 랭커가
                # 실제로 틀릴 여지(하드 네거티브)가 없다 — v1 평균 0.389로 이 상한을 넘었다.
                graded_count = len(set(fixture.product_ids) & set(case.relevant_product_ids))
                relevant_ratio = graded_count / candidate_count if candidate_count else 0.0
                if relevant_ratio > settings.goldenset_max_relevant_ratio and not has_note_marker(
                    case.notes, "relevant-ratio-exempt"
                ):
                    raise ValueError(
                        f"{case.case_id}: 등급≥1 후보 비율 {relevant_ratio:.4f}가 "
                        f"goldenset_max_relevant_ratio({settings.goldenset_max_relevant_ratio}) "
                        f"초과입니다 — notes에 '{RELEVANT_RATIO_EXEMPT_NOTE_PREFIX}' 예외 문구가 "
                        "필요합니다"
                    )
        persona = case.identity.persona_id
        if persona and persona not in purchase_history:
            raise ValueError(f"{case.case_id}: 구매 이력 페르소나가 없습니다")
        unknown_categories = set(case.hard_constraints.forbidden_categories) - catalog_categories
        if unknown_categories:
            raise ValueError(
                f"{case.case_id}: 스냅샷에 없는 금지 카테고리 {sorted(unknown_categories)}"
            )
        for product_id in case.relevant_product_ids:
            price = catalog[str(product_id)].get("price")
            if (
                case.hard_constraints.price_max is not None
                and price is not None
                and price > case.hard_constraints.price_max
            ):
                raise ValueError(f"{case.case_id}: 정답 상품 가격이 priceMax를 위반합니다")
            if (
                case.hard_constraints.price_min is not None
                and price is not None
                and price < case.hard_constraints.price_min
            ):
                raise ValueError(f"{case.case_id}: 정답 상품 가격이 priceMin을 위반합니다")

    # #333 라운드2 F-R6 — constraint_subset DIR 그룹은 강화(stricter) 노출이 완화(relaxed)
    # 노출의 부분집합이어야 한다(evals.metrics.behavior._check_constraint_subset과 같은 판정
    # 로직). 데이터가 다시 어긋나면 평가 스모크가 아니라 로드 시점(validate_cases)에 잡는다.
    constraint_subset_groups: dict[str, list[GoldenCase]] = {}
    for case in cases:
        if case.behavior_kind == "constraint_subset" and case.behavior_group_id:
            constraint_subset_groups.setdefault(case.behavior_group_id, []).append(case)
    for group_id, group_cases in constraint_subset_groups.items():
        if len(group_cases) != 2:
            raise ValueError(
                f"{group_id}: constraint_subset 그룹은 정확히 2케이스(강화/완화)여야 합니다: "
                f"{len(group_cases)}건"
            )
        left, right = group_cases
        left_filters, right_filters = dict(left.expected_filters), dict(right.expected_filters)
        left_is_stricter = left_filters.items() > right_filters.items()
        right_is_stricter = right_filters.items() > left_filters.items()
        if left_is_stricter == right_is_stricter:
            raise ValueError(
                f"{group_id}: expectedFilters의 포함 관계로 강화/완화 케이스를 판별할 수 없습니다"
            )
        stricter, relaxed = (left, right) if left_is_stricter else (right, left)
        stricter_fixture = parsed_fixtures.get(stricter.search_fixture_id or "")
        relaxed_fixture = parsed_fixtures.get(relaxed.search_fixture_id or "")
        if stricter_fixture is None or relaxed_fixture is None:
            raise ValueError(f"{group_id}: constraint_subset 케이스의 검색 fixture가 없습니다")
        offending = sorted(set(stricter_fixture.product_ids) - set(relaxed_fixture.product_ids))
        if offending:
            raise ValueError(
                f"{group_id}: 강화 케이스({stricter.case_id}) 노출이 완화 케이스"
                f"({relaxed.case_id}) 노출의 부분집합이 아닙니다 — 여집합 {offending}"
            )


def dump_jsonl(path: Path, rows: Sequence[BaseModel | dict]) -> None:
    """모델/사전을 결정론적 camelCase JSONL로 기록한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            value = row.model_dump(by_alias=True) if isinstance(row, BaseModel) else row
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
