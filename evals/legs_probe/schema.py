"""니즈 전개(legs) 프로브 앵커(정답지) 스키마 (#332).

검증자는 이 하네스의 설계 확정(§0 — 패킷 `packet-332.md`)을 코드로 못 박는다: decompose 단계
산출만 재는 단일 턴 프로브라 컨텍스트 행렬이 없고, `buy-*` caseId 는 e2e 골든셋
(`evals/goldenset/cases/buyer_dev.jsonl`)과 발화가 글자 그대로 같아야 한다(caseId 척추).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.agents.buyer.recommendation.decompose import normalize_category_token
from app.core.config import Settings

FIXTURE_SCHEMA_VERSION = "1.0.0"
SLICES = ("single", "conditions", "situational", "purpose", "multi")
PAIR_KINDS = ("INV-paraphrase", "DIR-budget")

REPO_ROOT = Path(__file__).resolve().parents[2]
# [§4] `buy-*` caseId 는 e2e 골든셋과 발화가 글자 그대로 같아야 한다(caseId 척추) — #333 이
# 골든셋 v2 를 작업 중이므로 v1(`buyer_dev.jsonl`)만 읽는다. v2 연결은 후속 커밋.
GOLDENSET_PATH = REPO_ROOT / "evals" / "goldenset" / "cases" / "buyer_dev.jsonl"


class CamelModel(BaseModel):
    """eval 내부 모델도 앱 와이어와 같은 camelCase 별칭 규약을 쓴다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


@functools.lru_cache(maxsize=1)
def _load_goldenset_queries() -> dict[str, str]:
    """`buy-*` 발화 대조용 — goldenset 은 **읽기만** 한다(수정 금지, §10)."""
    queries: dict[str, str] = {}
    for line in GOLDENSET_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        queries[record["caseId"]] = record["query"]
    return queries


class CoverageGroup(CamelModel):
    """상황·목적형 발화가 커버해야 할 상품 니즈 1개(예: 김장 재료의 "배추") — synonym 집합."""

    group_id: str = Field(min_length=1)
    synonyms: list[str] = Field(min_length=1)

    @field_validator("synonyms")
    @classmethod
    def _synonyms_are_not_blank_after_normalization(cls, value: list[str]) -> list[str]:
        """[§2, 설계 이탈] 패킷 원문은 "정규화 후 길이 ≥2"를 요구하지만, §4 라벨 데이터 자체가
        단음절 한글 명사 synonym(예: "무"·"꽃"·"잔"·"컵"·"칼"·"숯"·"갓")을 그대로 요구한다 —
        둘 다 만족할 수 없어 데이터(§4 "그대로 옮겨라")를 우선하고 검증자를 "정규화 후 공백만
        아님"으로 완화했다. 진짜 결함(공백-only, 빈 문자열)만 잡는다.
        """
        for synonym in value:
            if not normalize_category_token(synonym):
                raise ValueError(f"synonym '{synonym}' 은 정규화 후 빈 문자열입니다")
        return value


class Expected(CamelModel):
    """앵커 1건의 정답 — 채점은 `metrics.py` 가 이 값과 decompose 산출을 대조한다."""

    case: Literal[1, 2, 3]
    legs_min: int = Field(ge=0)
    legs_max: int = Field(ge=0)
    coverage_target: int | None = None
    coverage_groups: list[CoverageGroup] = Field(default_factory=list)
    acceptable_groups: list[CoverageGroup] = Field(default_factory=list)
    buy_all: bool | None = None
    total_budget: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _legs_range_is_sane(self) -> "Expected":
        # [§0] category_fanout_max 는 env 가 아니라 필드 기본값으로 — 지금은 5.
        max_allowed = Settings.model_fields["category_fanout_max"].default
        if self.legs_min > self.legs_max:
            raise ValueError("legsMin 은 legsMax 를 넘을 수 없습니다")
        if self.legs_max > max_allowed:
            raise ValueError(
                f"legsMax 는 category_fanout_max 기본값({max_allowed}) 을 넘을 수 없습니다"
            )
        return self

    @model_validator(mode="after")
    def _coverage_target_matches_groups(self) -> "Expected":
        if self.coverage_groups:
            valid_range = self.coverage_target is not None and (
                1 <= self.coverage_target <= len(self.coverage_groups)
            )
            if not valid_range:
                raise ValueError(
                    "coverageGroups 가 있으면 1<=coverageTarget<=len(coverageGroups) 여야 합니다"
                )
        elif self.coverage_target is not None:
            raise ValueError("coverageGroups 가 없으면 coverageTarget 은 null 이어야 합니다")
        return self


class Anchor(CamelModel):
    """발화 1건 — 어떤 슬라이스인지, 무엇을 기대하는지, pair 진단 소속까지 포함한다."""

    case_id: str = Field(min_length=1)
    slice: Literal["single", "conditions", "situational", "purpose", "multi"]
    check_type: Literal["MFT"] = "MFT"
    prompt_example: bool = False
    pair_id: str | None = None
    pair_kind: Literal["INV-paraphrase", "DIR-budget"] | None = None
    utterance: str = Field(min_length=1)
    expected: Expected
    # [§2] 저자 라벨 — "발화 원문 하나를 그대로 검색어로 쓰면 커버리지 그룹을 몇 개 짚는가".
    # trivial baseline 계산에만 쓴다(metrics.compute_baseline).
    baseline_groups_hit: Literal[0, 1]
    note: str | None = None

    @model_validator(mode="after")
    def _pair_kind_requires_pair_id(self) -> "Anchor":
        if (self.pair_id is None) != (self.pair_kind is None):
            raise ValueError(
                f"{self.case_id}: pairId 와 pairKind 는 함께 있거나 함께 없어야 합니다"
            )
        return self

    @model_validator(mode="after")
    def _conditions_slice_has_no_groups(self) -> "Anchor":
        """[§2] `slice=="conditions"` → `legsMax==0` · 그룹 없음 · `coverageTarget` null."""
        if self.slice != "conditions":
            return self
        if self.expected.legs_max != 0:
            raise ValueError(f"{self.case_id}: conditions 슬라이스는 legsMax==0 이어야 합니다")
        if self.expected.coverage_groups or self.expected.acceptable_groups:
            raise ValueError(
                f"{self.case_id}: conditions 슬라이스는 커버리지 그룹을 가질 수 없습니다"
            )
        if self.expected.coverage_target is not None:
            raise ValueError(
                f"{self.case_id}: conditions 슬라이스는 coverageTarget 이 null 이어야 합니다"
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
    def _buy_prefixed_anchors_match_goldenset_verbatim(self) -> "AnchorSet":
        """[§2] `buy-*` 는 e2e 골든셋(v1)과 발화가 글자 그대로 같아야 한다."""
        goldenset = _load_goldenset_queries()
        for anchor in self.utterances:
            if not anchor.case_id.startswith("buy-"):
                continue
            expected_query = goldenset.get(anchor.case_id)
            if expected_query is None:
                raise ValueError(
                    f"{anchor.case_id}: goldenset(buyer_dev.jsonl) 에 없는 caseId 입니다"
                )
            if anchor.utterance != expected_query:
                raise ValueError(
                    f"{anchor.case_id}: 발화가 goldenset 과 다릅니다 "
                    f"(anchor={anchor.utterance!r}, goldenset={expected_query!r})"
                )
        return self

    # [§4] pairId 는 항상 2건이 짝을 이루지는 않는다 — `legs-situ-0003`(DIR-budget, camp-budget)
    # 의 비교 대상은 `legs-situ-0001`(camp-paraphrase 소속)이라 note 로만 교차 참조한다.
    # camp-paraphrase·gift-budget·school-budget 은 실제로 2건씩 짝이지만, 그 사실을 스키마가
    # 강제하지는 않는다 — pair 진단(§3)은 합불이 아닌 exploratory 표라 짝이 없어도 표 한 줄로
    # 남을 뿐 채점을 깨지 않는다.
