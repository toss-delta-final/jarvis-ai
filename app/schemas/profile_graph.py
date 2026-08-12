"""개인화 그래프 API 표면 I-32~I-37 의 와이어 스키마 (#360, api-spec §3.8·§3.9).

**요청은 `StrictEventModel`, 응답은 `CamelModel`** 이다(`app/schemas/recommendations.py` 와 같은
이분법). 요청이 strict 인 이유는 미지 필드를 조용히 흡수하면 계약 불일치가 은폐되기 때문이고
(§3.9 — unknown field·snake_case·collision 은 `400`), 응답은 FastAPI 가 `response_model` 로
by_alias camelCase 직렬화한다.

**저장 모델은 여기 없다** — `app/agents/profile/graph_models.py` 가 소유한다. 그쪽은 와이어에
나가지 않는 판정 상태(`confidence`·`resolution`·`sensitive_topic`·`promoted` …)를 들고 있고,
그 경계가 §3.8 「응답 필드 기준」([HARD] FE 가 화면에 그리거나 다음 요청에 되돌려 보낼 값만
싣는다)의 집행 지점이다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.chat import CamelModel
from app.schemas.events import StrictEventModel

# PostgreSQL BIGINT 상한 — 신원 id 범위 방어(`app/schemas/profile.py` 와 같은 상수).
_BIGINT_MAX = 2**63 - 1

# §3.8 `edges[].object.type` 어휘. 저장 모델 `NodeType` 과 같은 값이지만 **계약 어휘라 여기
# 사본을 둔다** — 저장 모델을 넓히는 변경이 와이어를 자동으로 넓히면 안 된다.
NodeType = Literal[
    "brand", "category", "attribute", "priceBand", "ratingBand", "product", "situation"
]
# §3.8 응답 어휘는 5종이지만 **요청(I-33)은 4종**이다 — `purchased` 는 구매 사실이라 사용자가
# 만들 수 없다(지정 시 `400`, api-spec §3.9.1). Literal 이 그 거부를 스키마 단계에서 한다.
EditablePredicate = Literal["prefers", "likes", "avoids", "interestedIn"]


# ─────────── 응답 (§3.8·§3.9) ───────────


class GraphEdgeObjectView(CamelModel):
    """취향의 **대상**. `nodes[]` 배열이 폐지되고 항목 안으로 인라인됐다(v0.32.0).

    요청(I-33)의 `object` 와 모양이 같아 **조회한 `nodeId` 를 그대로 수정 요청에 실을 수 있다**.

    **[v0.33.0, #581] `label` 은 되돌려 보내는 값이 아니다** — `priceBand`·`ratingBand` 는
    저장 canonical(`"30000-50000"`·`"-50000"`)을 사람이 읽는 문장(`"50,000원 이하"`)으로
    렌더해서 싣는다(`graph_projection._render_label`). 그 문장을 `type`+`label` 형태로
    되보내면 `400` 이다(api-spec §3.9.1) — 대상 지목은 **`nodeId` 로 한다**. 화면에 그리는
    용도로는 그대로 쓰면 되고, 파싱하지 않는 것이 규약이다.
    """

    node_id: str
    type: NodeType
    label: str


class GraphEdgeView(CamelModel):
    """취향 항목 — **정확히 5필드**다 (api-spec §3.8).

    `editable` 은 **수정(I-33) 가능 여부**이며 삭제(I-34)는 `false` 여도 허용된다.
    `challenged` 는 표시용이 아니라 **FE 동작 트리거**다 — 사용자가 고친 뒤 반대 관측이 임계
    이상 쌓였다는 힌트이고, 이게 없으면 AI 가 사용자 수정에 이견이 있어도 알릴 방법이 없다.
    """

    edge_id: str
    predicate: Literal["prefers", "likes", "avoids", "interestedIn", "purchased"]
    object: GraphEdgeObjectView
    editable: bool
    challenged: bool


class PersonalizationView(CamelModel):
    """`{enabled}` 뿐이다 — `disabledAt` 은 저장 모델에만 둔다(v0.32.0).

    중지 시각은 소급 미반영 규약 집행에 필요하지만 FE 가 쓸 곳이 없다.
    """

    enabled: bool


class ProfileGraphView(CamelModel):
    """I-32 조회 응답 (§3.8).

    프로필 미보유는 **오류가 아니다** — `exists: false` + 빈 배열로 `200` 이다.
    `markdown` 은 배치(consolidation) 산출물이라 I-33·I-34 직후에는 이전 내용이고, `edges` 는
    즉시 반영되므로 **두 영역이 일시적으로 어긋난다**. `generatedAt` 이 그 기준 시각이다.
    """

    user_id: int
    exists: bool
    markdown: str | None = None
    generated_at: str | None = None
    graph_version: str
    personalization: PersonalizationView
    edges: list[GraphEdgeView]


class PatchGraphEdgeResponse(CamelModel):
    """I-33 수정 응답 (§3.9.1).

    `edge` 는 **§3.8 `edges[]` 항목과 완전히 같은 모양**이다 — FE 가 목록의 해당 항목을 이 값으로
    통째로 교체한다. `edgeId` 는 `(predicate, nodeId)` 파생이라 **바뀐다**.
    """

    user_id: int
    graph_version: str
    edge: GraphEdgeView
    merged: bool
    replayed: bool


class DeleteGraphEdgeResponse(CamelModel):
    """I-34 삭제 응답 (§3.9.2). `edgeId` 는 **요청 대상**을 그대로 돌려준다."""

    user_id: int
    graph_version: str
    edge_id: str
    replayed: bool


class GraphPurgedView(CamelModel):
    """I-36 이 지운 것 — **개수만, 정확히 2키**다 (§3.9.4).

    구 계약의 `nodes`·`facts` 는 사용자가 만든 적 없는 내부 구조라 빠졌고 `suppressed` 는 undo
    폐기로 항상 0 이 됐다. 화면 문구는 "취향 12건 · 대화 기록 143건" 이면 충분하다.
    """

    edges: int
    transcript_turns: int


class GraphResetResponse(CamelModel):
    """I-36 초기화 응답 (§3.9.4). `personalization` 은 **초기화가 바꾸지 않는다**."""

    user_id: int
    graph_version: str
    purged: GraphPurgedView
    personalization: PersonalizationView
    replayed: bool


class PersonalizationResponse(CamelModel):
    """I-37 중지·재개 응답 (§3.9.5)."""

    user_id: int
    graph_version: str
    personalization: PersonalizationView
    replayed: bool


# ─────────── 요청 (§3.9) ───────────


class GraphObjectRequest(StrictEventModel):
    """I-33 의 `object` — **`nodeId` 형태와 `type`+`label` 형태 중 정확히 하나**다 (§3.9.1).

    둘을 함께 실으면 `400` 이다 — 어느 쪽이 우선인지가 계약에 없고, 우선순위를 정하는 순간
    무시된 필드가 **조용한 오작동**이 된다.
    """

    node_id: str | None = Field(default=None, strict=True, min_length=1)
    type: NodeType | None = None
    label: str | None = Field(default=None, strict=True, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> GraphObjectRequest:
        by_node_id = self.node_id is not None
        by_label = self.type is not None or self.label is not None
        if by_node_id and by_label:
            raise ValueError("object accepts either nodeId or type+label, not both")
        if by_label and (self.type is None or self.label is None):
            raise ValueError("object requires both type and label")
        if not by_node_id and not by_label:
            raise ValueError("object requires nodeId or type+label")
        return self

    @model_validator(mode="after")
    def _label_within_cap(self) -> GraphObjectRequest:
        """라벨 상한은 **config 주입**이다 — 저장 모델 상한과 갈리면 안 된다(`schemas/profile.py` 패턴)."""
        if self.label is None:
            return self
        from app.core.config import get_settings

        cap = get_settings().profile_graph_label_max_chars
        if len(self.label) > cap:
            raise ValueError(f"label exceeds {cap} characters")
        return self


class PatchGraphEdgeRequest(StrictEventModel):
    """I-33 수정 요청 (§3.9.1).

    `predicate`·`object` 중 **최소 하나**는 있어야 한다. 생략한 쪽은 대상 edge 의 현재 값을
    쓰는데, 그 출처는 **변경 시점에 잠금 아래에서 읽은 문서**다(서버 규약 — 스키마가 아니다).
    """

    predicate: EditablePredicate | None = None
    object: GraphObjectRequest | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> PatchGraphEdgeRequest:
        if self.predicate is None and self.object is None:
            raise ValueError("predicate or object is required")
        return self


class GraphResetRequest(StrictEventModel):
    """I-36 초기화 요청 (§3.9.4).

    `scope` 는 **파괴 범위를 호출자가 명시적으로 이름 붙이게** 하는 판별자다 — 빈 요청 하나로
    전체 삭제가 일어나지 않게 막는다. 현재 `"ALL"` 하나만 허용한다.
    """

    scope: Literal["ALL"]


class PersonalizationRequest(StrictEventModel):
    """I-37 중지·재개 요청 (§3.9.5). `strict` 라 `"true"`·`1` 같은 coercion 을 거부한다."""

    enabled: bool = Field(strict=True)


def validate_user_id(user_id: int) -> int:
    """경로 `{userId}` 검증 — 양의 BIGINT 만 (api-spec §3.8·§3.9 실패표).

    FastAPI 경로 파라미터는 문자열에서 파싱되므로 pydantic strict 가 그대로 걸리지 않는다.
    `bool` 은 `int` 서브클래스라 별도로 막는다 — 경로에서는 `"true"` 가 파싱 실패로 걸리지만,
    직접 호출·목에서 들어오는 경로까지 덮는 심층 방어다.
    """
    if isinstance(user_id, bool) or not (1 <= user_id <= _BIGINT_MAX):
        raise ValueError("userId must be a positive BIGINT")
    return user_id
