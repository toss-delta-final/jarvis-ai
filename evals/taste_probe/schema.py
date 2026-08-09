"""취향 추출 골든셋(대화 세션 → 기대 트리플) 스키마 (#462).

`evals/category_probe/schema.py` 의 검증-in-스키마 규약을 그대로 따른다 — 규칙은 문서가 아니라
validator 에 둔다. **판정 로직은 복제하지 않는다** — node_id 산출(`make_node_id`)·predicate↔kind
정합(`resolver._POSITIVE_PREDICATE`)·밴드/상품 라벨 형식(`resolver._resolve_band`/
`_resolve_product`)은 전부 프로덕션 함수를 그대로 import 해 대조한다(#380 규약,
`scripts/probe_delta_prompt_356.py` 가 세운 전례). 규칙을 정규식으로 새로 쓰면 그 순간 드리프트가
측정에 안 잡힌다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.agents.profile.graph_models import make_node_id, normalize_label
from app.agents.profile.resolver import _POSITIVE_PREDICATE, _resolve_band, _resolve_product

FIXTURE_SCHEMA_VERSION = "1.0.0"
SLICE_IDS = ("kindCoverage", "polarity", "repetition", "conflict", "noise")
KINDS = ("brand", "category", "attribute", "priceBand", "ratingBand", "product", "situation")
_BAND_KINDS = frozenset({"priceBand", "ratingBand"})
# 스키마 검증은 시각에 무관하다 — `_resolve_band`/`_resolve_product` 가 요구하는 `now` 인자용 placeholder.
_VALIDATION_NOW = "1970-01-01T00:00:00+00:00"


class CamelModel(BaseModel):
    """eval 내부 모델도 앱 와이어와 같은 camelCase 별칭 규약을 쓴다(CLAUDE.md)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ExpectedTriple(CamelModel):
    """기대 트리플 1개 — 세션에서 추출돼야 할 취향.

    `purchased` predicate 는 스키마 레벨에서 아예 표현할 수 없다(Literal 밖) — resolver 가 거부
    하는 것과 같은 이유(대화가 구매 사실의 원천이 아니다, `resolver._decide_predicate` 참조)다.
    """

    kind: Literal[
        "brand", "category", "attribute", "priceBand", "ratingBand", "product", "situation"
    ]
    predicate: Literal["prefers", "likes", "avoids", "interestedIn"]
    accept: list[str] = Field(min_length=1)
    canonical_label: str = Field(min_length=1)
    node_id: str = Field(min_length=1)

    @field_validator("accept")
    @classmethod
    def _accept_no_duplicates(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for item in value:
            key = normalize_label(item)
            if key in seen:
                raise ValueError(
                    f"accept 원소 '{item}' 이 normalize_label 기준 다른 원소와 중복입니다 — "
                    "표기 흔들림은 accept 항목 하나로 족합니다"
                )
            seen.add(key)
        return value

    @model_validator(mode="after")
    def _canonical_label_is_accepted(self) -> "ExpectedTriple":
        if self.canonical_label not in self.accept:
            raise ValueError(
                f"canonicalLabel '{self.canonical_label}' 이 accept {self.accept} 안에 없습니다 "
                "— canonicalLabel 은 accept 중 확정 표기 1개여야 합니다"
            )
        return self

    @model_validator(mode="after")
    def _node_id_matches_production(self) -> "ExpectedTriple":
        expected_node_id = make_node_id(self.kind, self.canonical_label)
        if self.node_id != expected_node_id:
            raise ValueError(
                f"nodeId '{self.node_id}' 가 make_node_id({self.kind!r}, {self.canonical_label!r}) "
                f"= '{expected_node_id}' 와 다릅니다 — make_node_id 로 다시 계산해 채우세요"
            )
        return self

    @model_validator(mode="after")
    def _predicate_matches_kind_polarity(self) -> "ExpectedTriple":
        if self.predicate == "avoids":
            return self  # negative polarity — 모든 kind 에서 avoids 가 허용된다(resolver 규칙)
        expected = _POSITIVE_PREDICATE.get(self.kind)
        if self.predicate != expected:
            raise ValueError(
                f"kind={self.kind!r} 의 positive predicate 는 resolver._POSITIVE_PREDICATE 기준 "
                f"{expected!r} 이어야 하는데 {self.predicate!r} 입니다(avoids 가 아니면 이 표와 "
                "일치해야 합니다 — resolver._decide_predicate 규칙)"
            )
        return self

    @model_validator(mode="after")
    def _band_label_is_production_valid(self) -> "ExpectedTriple":
        if self.kind not in _BAND_KINDS:
            return self
        node = _resolve_band(
            self.kind, self.canonical_label, anchor_phrase="schema-validation", now=_VALIDATION_NOW
        )
        if node is None or node.label != self.canonical_label:
            raise ValueError(
                f"canonicalLabel '{self.canonical_label}' 을 resolver._resolve_band 가 받지 "
                f"않습니다({self.kind} 는 '최소-최대' 숫자 정규형이어야 합니다, low<high"
                + (", ratingBand 는 상한 5" if self.kind == "ratingBand" else "")
                + ")"
            )
        return self

    @model_validator(mode="after")
    def _product_label_is_production_valid(self) -> "ExpectedTriple":
        if self.kind != "product":
            return self
        node = _resolve_product(
            self.canonical_label, anchor_phrase="schema-validation", now=_VALIDATION_NOW
        )
        if node is None or node.label != self.canonical_label:
            raise ValueError(
                f"canonicalLabel '{self.canonical_label}' 을 resolver._resolve_product 가 받지 "
                "않습니다(product 라벨은 숫자 productId 만 허용, 앞자리 0 없는 정규형)"
            )
        return self


class GoldenSession(CamelModel):
    """세션 1개 — 여러 턴의 사용자 발화 + 기대 트리플. `sessionId` 가 산출물 전부의 귀속 키다."""

    session_id: str = Field(min_length=1)
    slice_id: Literal["kindCoverage", "polarity", "repetition", "conflict", "noise"]
    test_type: Literal["MFT"]
    turns: list[str] = Field(min_length=2)
    expected_triples: list[ExpectedTriple] = Field(default_factory=list)
    boundary_note: str = Field(min_length=10)

    @field_validator("turns")
    @classmethod
    def _turns_are_not_blank(cls, value: list[str]) -> list[str]:
        for turn in value:
            if not turn.strip():
                raise ValueError(
                    "turns 원소는 공백만일 수 없습니다 — append_session_ctx 의 빈 발화 가드가 "
                    "이 항목을 버려 세션 버퍼가 의도한 턴 수보다 짧아집니다"
                )
        return value

    @field_validator("boundary_note")
    @classmethod
    def _boundary_note_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("boundaryNote 는 비워 둘 수 없습니다(이슈 요구 — 오답 경계 사유)")
        return value

    @model_validator(mode="after")
    def _expected_triples_count_matches_slice(self) -> "GoldenSession":
        if self.slice_id == "noise" and self.expected_triples:
            raise ValueError(
                f"{self.session_id}: slice=noise 는 expectedTriples 가 비어 있어야 합니다 "
                "(취향 신호 0인 잡담 슬라이스)"
            )
        if self.slice_id != "noise" and not self.expected_triples:
            raise ValueError(
                f"{self.session_id}: slice={self.slice_id} 는 expectedTriples 가 1개 이상이어야 "
                "합니다"
            )
        return self

    @model_validator(mode="after")
    def _no_answer_leakage_in_turns(self) -> "GoldenSession":
        joined = "\n".join(self.turns)
        for triple in self.expected_triples:
            if triple.kind in _BAND_KINDS and triple.canonical_label in joined:
                raise ValueError(
                    f"{self.session_id}: canonicalLabel '{triple.canonical_label}' "
                    "(priceBand/ratingBand 정규형)이 발화 원문에 그대로 있습니다 — 정규형을 발화에 "
                    "심으면 LLM 이 베껴 쓰는 것을 추출 능력으로 오독합니다(category_probe 함정 3 승계)"
                )
            if triple.kind == "category" and " > " in triple.canonical_label:
                if triple.canonical_label in joined:
                    raise ValueError(
                        f"{self.session_id}: canonicalLabel '{triple.canonical_label}' "
                        "(카테고리 canonical 표기)가 발화 원문에 그대로 있습니다 — 정답 신호 누출"
                    )
        return self


class GoldenSet(CamelModel):
    """이 하네스가 읽는 유일한 입력. 스크립트는 이 파일 밖의 세션을 만들지 않는다."""

    schema_version: Literal["1.0.0"] = FIXTURE_SCHEMA_VERSION
    dataset_version: str = Field(min_length=1)
    sessions: list[GoldenSession] = Field(min_length=1)

    @model_validator(mode="after")
    def _session_ids_are_unique(self) -> "GoldenSet":
        ids = [session.session_id for session in self.sessions]
        if len(ids) != len(set(ids)):
            raise ValueError("sessionId 가 중복되었습니다")
        return self

    @model_validator(mode="after")
    def _first_turns_are_unique(self) -> "GoldenSet":
        """`fakes.ScriptedDeltaLLM` 이 세션을 첫 턴 텍스트로 식별한다(§ fakes.py 참조) — 중복되면
        가짜 LLM 이 엉뚱한 세션의 기대 트리플을 돌려줘 CI 표본이 조용히 오염된다."""
        first_turns = [session.turns[0] for session in self.sessions]
        if len(first_turns) != len(set(first_turns)):
            raise ValueError(
                "세션들의 첫 turn 이 중복됩니다 — ScriptedDeltaLLM 이 첫 turn 으로 세션을 식별하므로"
                " 골든셋 전체에서 유일해야 합니다"
            )
        return self
