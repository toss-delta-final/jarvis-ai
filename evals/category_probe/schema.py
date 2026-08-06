"""카테고리 매핑·선택 프로브 앵커(정답지) 스키마 (#331).

`evals/intent_probe/schema.py` 의 검증-in-스키마 규약을 그대로 따른다 — 정답지가 조금만
어긋나도 표가 재현되지 않고, 그 사실을 사람이 눈으로 잡지 못한다는 것이 그 하네스의 출발점이고
이 하네스도 같은 이유로 규칙을 문서가 아니라 스키마에 둔다.

앵커 표기(§2, packet-331 §2)는 이미 결정됐다 — canonical 은 라이브 pg-catalog `categories`
사전(leaf 1,007행)의 **잎 이름 그대로**(`대분류 > 중분류`, 예: `음향가전 > 이어폰`)다. leaf 단독
표기(`이어폰`)는 도메인 간 중복이 커 라벨로 성립하지 않는다(실측 81건 중복 — `노트북`이 6개
서로 다른 canonical 에 걸치는 것이 그 예다). goldenset GUIDE 의 "leaf 단독 표기" 규칙은 구
taxonomy 기준이라 이 하네스에는 적용하지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

FIXTURE_SCHEMA_VERSION = "1.0.0"
SLICE_IDS = ("single", "multi", "none", "notInCatalog")
TEST_TYPES = ("MFT", "INV")


class CamelModel(BaseModel):
    """eval 내부 모델도 앱 와이어와 같은 camelCase 별칭 규약을 쓴다(CLAUDE.md)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ExpectedLeg(CamelModel):
    """기대 leg 1개 — `accept` 안 아무 문자열이나 맞으면 그 leg 은 정답이다(복수 정답 허용).

    분류상 모호한 발화(예: "노트북 사고 싶어" — 브랜드별 leaf 3종에 흩어짐, "백팩 사고 싶어" —
    성별 leaf 2종에 흩어짐)는 taxonomy 자체의 구조이지 매핑 실패가 아니므로, 그 leaf 전부를
    accept 로 열거해 정당한 모호성과 오분류를 가른다.
    """

    accept: list[str] = Field(min_length=1)

    @field_validator("accept")
    @classmethod
    def _accept_is_canonical_leaf_form(cls, value: list[str]) -> list[str]:
        for item in value:
            if item.count(" > ") != 1:
                raise ValueError(
                    f"accept 값 '{item}' 은 ' > ' 를 정확히 1회 포함하는 canonical 잎 표기여야 "
                    "합니다(§2) — 대분류 단독·중분류 단독 표기는 라벨로 성립하지 않습니다"
                )
        return value


class AnchorCell(CamelModel):
    """측정 단위 1개 — 발화 하나 + 그 발화의 정답 leg 목록."""

    cell_id: str = Field(min_length=1)
    utterance: str = Field(min_length=1)
    slice_id: Literal["single", "multi", "none", "notInCatalog"]
    test_type: Literal["MFT", "INV"]
    inv_group_id: str | None = None
    case_id: str | None = None
    expected_legs: list[ExpectedLeg] = Field(default_factory=list)
    # notInCatalog 셀 전용 — pre-flight(수동 도구)가 이 키워드로 라이브 categories 를 ILIKE 조회해
    # 정말 부재하는지 확인한다(§2b). 다른 슬라이스는 이 필드를 선언할 수 없다.
    absent_keyword: str | None = None
    boundary_note: str = Field(min_length=10)

    @field_validator("utterance")
    @classmethod
    def _utterance_does_not_leak_the_answer(cls, value: str) -> str:
        if " > " in value:
            raise ValueError(
                f"발화 '{value}' 에 ' > ' 가 들어 있습니다 — 정답 신호(canonical 표기)가 발화에 "
                "직접 새면 채점이 무의미해집니다(§2b, intent_probe 함정 3 승계)"
            )
        return value

    @field_validator("boundary_note")
    @classmethod
    def _boundary_note_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("boundaryNote 는 비워 둘 수 없습니다(이슈 요구 — 오답 경계 사유)")
        return value

    @model_validator(mode="after")
    def _inv_group_matches_test_type(self) -> "AnchorCell":
        if self.test_type == "INV" and self.inv_group_id is None:
            raise ValueError(f"{self.cell_id}: INV 셀은 invGroupId 를 선언해야 합니다")
        if self.test_type == "MFT" and self.inv_group_id is not None:
            raise ValueError(f"{self.cell_id}: MFT 셀은 invGroupId 를 선언할 수 없습니다")
        return self

    @model_validator(mode="after")
    def _expected_legs_count_matches_slice(self) -> "AnchorCell":
        count = len(self.expected_legs)
        if self.slice_id == "single" and count != 1:
            raise ValueError(
                f"{self.cell_id}: slice=single 은 expectedLegs 가 정확히 1개여야 합니다"
            )
        if self.slice_id == "multi" and count < 2:
            raise ValueError(
                f"{self.cell_id}: slice=multi 는 expectedLegs 가 2개 이상이어야 합니다"
            )
        if self.slice_id in ("none", "notInCatalog") and count != 0:
            raise ValueError(
                f"{self.cell_id}: slice={self.slice_id} 는 expectedLegs 가 비어 있어야 합니다"
                " — 오답 canonical 을 강제하지 않습니다"
            )
        return self

    @model_validator(mode="after")
    def _absent_keyword_matches_slice(self) -> "AnchorCell":
        if self.slice_id == "notInCatalog" and not (self.absent_keyword or "").strip():
            raise ValueError(
                f"{self.cell_id}: slice=notInCatalog 는 absentKeyword 를 선언해야 합니다 "
                "— pre-flight 가 이 키워드로 라이브 사전 부재를 확인합니다"
            )
        if self.slice_id != "notInCatalog" and self.absent_keyword is not None:
            raise ValueError(
                f"{self.cell_id}: slice={self.slice_id} 는 absentKeyword 를 선언할 수 없습니다"
            )
        return self


class AnchorSet(CamelModel):
    """프로브가 읽는 유일한 입력. 스크립트는 이 파일 밖의 발화를 만들지 않는다."""

    schema_version: Literal["1.0.0"] = FIXTURE_SCHEMA_VERSION
    fixture_version: str = Field(min_length=1)
    cells: list[AnchorCell] = Field(min_length=1)

    @model_validator(mode="after")
    def _cell_ids_are_unique(self) -> "AnchorSet":
        ids = [cell.cell_id for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("cellId 가 중복되었습니다")
        return self

    @model_validator(mode="after")
    def _inv_groups_have_at_least_two_cells(self) -> "AnchorSet":
        counts: dict[str, int] = {}
        for cell in self.cells:
            if cell.inv_group_id is not None:
                counts[cell.inv_group_id] = counts.get(cell.inv_group_id, 0) + 1
        short = {group: n for group, n in counts.items() if n < 2}
        if short:
            raise ValueError(f"invGroupId 는 셀이 2개 이상이어야 합니다: {short}")
        return self
