"""구매자 추천 골든셋 스키마와 교차 fixture 검증."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.core.config import Settings, get_settings
from app.schemas.spring import ProductSearchFilters

SCHEMA_VERSION = "1.0.0"
DATASET_VERSION = "1.0.0"
CASE_ID_RE = re.compile(r"^buy-[a-z][a-z0-9_]*-\d{4}$")
ALLOWED_SLICES = frozenset(
    {
        "search",
        "guest",
        "cold_start",
        "personalization",
        "personalization_overreach",
        "multi_constraint",
        "repurchase",
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
    schema_version: Literal["1.0.0"]
    dataset_version: Literal["1.0.0"]
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
        if "search" in self.slices and not self.relevant_product_ids:
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
    catalog_categories = {
        product.get("categoryName") for product in catalog.values() if product.get("categoryName")
    }
    for case in cases:
        labeled_ids = set(case.relevant_product_ids) | set(case.must_exclude_product_ids)
        missing = labeled_ids - {int(key) for key in catalog}
        if missing:
            raise ValueError(f"{case.case_id}: 카탈로그에 없는 productId {sorted(missing)}")
        if case.search_fixture_id and case.search_fixture_id not in search_responses:
            raise ValueError(f"{case.case_id}: 검색 fixture가 없습니다")
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


def dump_jsonl(path: Path, rows: list[BaseModel | dict]) -> None:
    """모델/사전을 결정론적 camelCase JSONL로 기록한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            value = row.model_dump(by_alias=True) if isinstance(row, BaseModel) else row
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
