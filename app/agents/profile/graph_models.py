"""개인화 그래프 내부 저장 모델 (SPEC-PROFILE-GRAPH-149 §5.1~5.4).

`("graph", user_id)` / `"v1"` 단일 jsonb 문서에 들어가는 **저장 모델**이고 와이어 모델이 아니다 —
그래서 `app/schemas/`(camelCase CamelModel 규약 디렉터리)가 아니라 여기 둔다. 와이어 계약은
api-spec §3.8·§3.9가 소유하며 그 표면 구현은 이슈 #150이다.

사용자당 항목 1개로 두는 이유는 §7.1이다: per-user advisory 잠금이 별도 연결 풀에서 잡혀 store
트랜잭션과 결합되지 않아 **다중 항목 원자성이 없다** — N개로 쪼개면 전부 찢어진 쓰기 상태를 만든다.

SPEC §5.2는 필드에 기본값을 두지 않는다. 여기서도 두지 않는다(`GraphNode.resolution` 제외) —
병합 엔진이 edge 를 만들 때 `suppressed_at`·`superseded_by` 같은 상태 필드를 **의식적으로**
정하게 강제하는 것이 목적이다. 기본값을 주면 tombstone 관련 필드를 빠뜨려도 조용히 통과한다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:  # 런타임 임포트를 만들지 않는다 — 저장 모델이 설정 모듈을 끌고 오지 않게.
    from app.core.config import Settings

NodeType = Literal[
    "brand", "category", "attribute", "priceBand", "ratingBand", "product", "situation"
]
Predicate = Literal["prefers", "likes", "avoids", "interestedIn", "purchased"]
EdgeStatus = Literal["active", "suppressed", "superseded"]
# 관측 출처. **이름을 붙여 두는 이유**는 `graph_merge._observation` 이 저장 payload 를 검증할 때
# 이 어휘를 단일 출처로 삼기 때문이다 — 인라인 Literal 로 두면 검증 쪽이 값을 복제하게 되고,
# 한쪽만 늘렸을 때 다른 쪽이 조용히 어긋난다(`Predicate`·`EdgeStatus` 와 같은 규약).
EdgeSource = Literal["conversation", "purchase", "user"]

_EDGE_ID_PREFIX = "e_"
_EDGE_ID_HEX_LEN = 16  # api-spec §3.8 `edgeId` = "e_" + 16자 hex

BAND_RE = re.compile(r"^(\d*)-(\d*)$")
"""밴드형 노드(`priceBand`·`ratingBand`)의 canonical 라벨 (#581).

**한쪽 경계 생략을 허용한다** — `"30000-50000"`(양쪽) · `"-50000"`(이하만) · `"100000-"`(이상만).
양쪽을 강제하던 시절에는 "5만원 이하" 같은 취향을 담을 자리가 없어 추출 LLM 이 없는 경계를
지어냈다(실측: 하한 `0`, 상한 `999999999`). 경계가 둘 다 없는 `"-"` 는 매치되지만 밴드가
아니므로 **호출부가 걸러야 한다** — 정규식으로 표현하면 읽을 수 없어진다.

**이 모듈에 두는 이유**는 파서(`resolver._resolve_band`)와 렌더러(`graph_projection`)가 반드시
같은 것을 봐야 하기 때문이다. 갈리면 저장은 되는데 화면에서는 폴백되는 라벨이 생긴다.
`normalize_label`·`make_node_id` 와 같은 자리다 — 여기가 라벨 규약의 주인이다.
"""


def normalize_label(text: str) -> str:
    """`node_id` 라벨 부분 정규화 — NFKC + 공백 정리 + casefold.

    **조사·어미는 건드리지 않는다**(REQ-PGRAPH-017) — 한국어 형태소까지 접으면 "노이즈캔슬링이"와
    "노이즈캔슬링"이 합쳐지는 정도를 넘어 정당한 취향 신호를 잃는다. fact 수준 dedup 이 문자열
    완전 일치를 유지하는 것과 같은 경계다.

    이 함수가 합쳐 주는 것은 **표기 변형뿐**이다(`SONY`/`sony`/`ＳＯＮＹ`). `소니`↔`SONY` 같은
    문자 체계 차이나 `쏘니` 같은 오타 수렴은 정규화가 아니라 **통제 어휘 스냅**의 몫이며
    (§6.2 resolver), 브랜드 어휘는 아직 없다(OPEN-G2 / api-spec C-28).
    """
    return unicodedata.normalize("NFKC", " ".join(text.split())).casefold()


def make_node_id(node_type: str, label: str) -> str:
    """`"{type}:{정규화 라벨}"` (REQ-PGRAPH-010)."""
    return f"{node_type}:{normalize_label(label)}"


def make_edge_key(predicate: str, node_id: str) -> str:
    """`"{predicate}|{node_id}"` — `SPEC-PROFILE-001` §5.2 `GateState.preference_key`와 동일 개념."""
    return f"{predicate}|{node_id}"


def make_edge_id(edge_key: str) -> str:
    """`"e_" + sha256(edge_key)[:16]` (REQ-PGRAPH-010).

    **랜덤 식별자를 쓰면 재파생이 tombstone 을 우회하므로 이 결정론성은 기능 요구사항이다.**
    내장 `hash()` 는 PYTHONHASHSEED 랜덤화로 프로세스마다 값이 달라져 여기 쓸 수 없다 —
    `hashlib` 고정(docs/lessons.md 2026-08-06 항목).
    """
    digest = hashlib.sha256(edge_key.encode("utf-8")).hexdigest()
    return _EDGE_ID_PREFIX + digest[:_EDGE_ID_HEX_LEN]


class NodeResolution(BaseModel):
    """노드 식별에 쓴 판정 근거 — **와이어 미노출**(SPEC §5.1).

    거리·margin 을 저장하는 목적은 임계 재측정이다(OPEN-G1 / #344): resolver 기본값은 다른 앵커
    분포에서 측정된 값이라 이 저장소에서 다시 재야 하는데, 판정 시점의 수치를 남기지 않으면
    "표본 0"이 근거가 아니라 질문이 된다(docs/lessons.md 2026-08-05 실측 프로브 항목).

    와이어에 싣지 않는 이유는 임베딩 내부값에 FE 로직이 결합되면 모델·어휘 교체가 곧 계약 변경이
    되기 때문이다(`DESIGN-CATEGORY-HYBRID-59` §10).
    """

    # exact=어휘 정확 일치 / embedding=최근접 스냅 / rule=결정론적 파서(밴드) / no_vocabulary=어휘 부재
    method: Literal["exact", "embedding", "rule", "no_vocabulary"]
    distance: float | None = None  # 임베딩 경로만 — 밴드·정확 일치는 거리 개념이 없다
    margin: float | None = None  # top1-top2 차. 히트 1건이면 확신을 잴 수 없어 None
    lexicon_version: str | None = None  # 스냅 대상 어휘 식별자. 어휘가 없으면 None
    anchor_phrase: str  # 스냅에 실제 쓴 발화 파생 구절 — LLM 라벨이 아니다(REQ-PGRAPH-012a)
    resolved_at: str  # ISO-8601


class GraphNode(BaseModel):
    """edge 가 참조하는 대상. 자신을 참조하는 non-purged edge 가 없으면 존재하지 않는다(SPEC §5.1).

    **믿음·출처를 노드에 두지 않는다** — 같은 브랜드를 좋아하면서 특정 카테고리에서만 회피할 수
    있어, 확신도를 노드에 두면 그 상태를 표현할 수 없다. 그래서 confidence·evidence 는 전부 edge 다.
    """

    node_id: str  # "{type}:{정규화 라벨}"
    type: NodeType
    label: str  # 사람이 읽는 이름(상한: profile_graph_label_max_chars)
    verified: bool  # 통제 어휘에 스냅됐는가 — 실패해도 노출은 하되 신뢰하지 않는다(REQ-PGRAPH-013)
    resolution: NodeResolution | None = None


class UserIntent(BaseModel):
    """사용자 편집 고정(pin) 표식 (SPEC §5.4, §6.4).

    본 이슈(#356)는 사용자 변경 경로가 없어 항상 `None` 이다 — 스키마 자리만 만든다.
    쓰기 경로는 #150(API)·#358(저널·CAS)이 만든다.
    """

    kind: Literal["assert", "correct", "suppress"]
    asserted_at: str
    expires_at: None = None  # [HARD] 항상 None — 사용자 의도는 만료되지 않는다(§6.4)
    mutation_id: str
    prior_predicate: str | None = None


class GraphEdge(BaseModel):
    """믿음과 출처의 소재 (SPEC §5.2).

    `status` 와 `promoted` 는 **직교**한다 — 사용자가 지운(suppressed) 취향도 게이트 통과분일 수
    있고, 그 사실을 지웠다는 이유로 잊으면 복구가 성립하지 않는다.
    """

    edge_key: str  # "{predicate}|{node_id}"
    edge_id: str  # "e_" + sha256(edge_key)[:16] — 재파생에 안정
    node_id: str  # 대상. from 은 항상 사용자 자신이라 저장하지 않는다
    predicate: Predicate
    status: EdgeStatus
    promoted: bool  # 게이트 통과 여부(status 와 직교)
    origin: Literal["machine", "user"]
    source_latest: EdgeSource
    confidence: float  # 내부 수치 — 와이어에는 3버킷 라벨만 나간다
    evidence_count: int
    evidence_by_source: dict[str, int]
    evidence_refs: list[str]  # fact key 참조(상한: graph_evidence_refs_max)
    first_observed_at: str
    last_observed_at: str
    decay_evaluated_at: str  # 감쇠 시계 — 배치당 1회 고정(§6.2, 관측별 현재시각 금지)
    valid_from: str | None  # 승격 시각
    superseded_by: str | None  # 충돌 패자 → 승자 edge_id
    suppressed_at: str | None
    user_intent: UserIntent | None  # pin(§6.4). 생산자는 `graph_mutations._pin` 하나뿐이다
    # pin 이후 쌓인 **반대 관측 건수**(§6.4). 배치 횟수가 아니다 — `graph_merge._resolve_conflicts`
    # 가 이번 배치에 실제 반대 관측이 있었을 때만 올린다. 노출 판정은 `is_pin_challenged`.
    challenge_count: int
    derived_from_sensitive: bool
    sensitive_topic: str | None  # 보존기간 판정용 — **와이어 미노출**(§6.8)


def is_projected(edge: GraphEdge) -> bool:
    """이 edge 가 **와이어 투영(I-32 `edges[]`)에 실리는가** (#360).

    정본 `I-32` 는 *"`edges` 는 `active` 만 싣는다"* 이고, 민감 파생은 **어떤 카운트에도 세지
    않는다**(REQ-PGRAPH-076 [HARD]). 두 조건이 전부다 — `promoted` 는 병합 엔진 내부의 확신도
    히스테리시스 값이지 노출을 가르는 스위치가 아니다(SPEC v0.3.3 에서 정정).

    **술어를 여기 한 곳에 두는 이유**: 투영과 I-36 의 `purged.edges` 가 **같은 모집단**을 세야
    화면 문구("취향 12건")와 초기화 응답이 어긋나지 않는다. 두 곳에 손으로 같은 조건을 적으면
    한쪽만 고치는 순간 조용히 갈라진다.
    """
    return edge.status == "active" and not edge.derived_from_sensitive


def is_pin_challenged(edge: GraphEdge, *, settings: Settings) -> bool:
    """pin 에 반대 관측이 임계 이상 쌓였는가 (REQ-PGRAPH-033).

    **파생값이라 저장하지 않는다** — 와이어에서 뺀 `confidence` 를 3버킷 라벨로 파생하는
    `graph_merge._confidence_bucket` 과 같은 패턴이다. 임계는 설정이라 같은 문서라도 값이 바뀌면
    판정이 달라져야 하는데, 저장해 두면 다음 배치까지 옛 임계로 굳는다.

    **상태를 바꾸는 신호가 아니다.** 참이어도 `status`·`predicate`·`confidence` 는 그대로이며,
    FE 가 "다시 반영할까요?" 를 물을지 판단하는 **동작 트리거**다(api-spec §3.8). 취향 변화의
    반영은 명시적 사용자 동작으로만 일어난다.

    **`graph_pin_challenge_count == 0` 은 신호를 끈다.** `count >= threshold` 로만 쓰면 0 에서
    항상 참이 되어 규약과 정반대로 동작한다 — 설정이 `ge=0` 으로 그 값을 허용하므로 여기서 가른다.

    이 모듈에 두는 이유는 **투영 계층(#360)이 호출하기 때문**이다. `graph_merge` 에 두면 요청
    경로가 배치 모듈을 통해 `store` → 연결 풀까지 끌고 온다.
    """
    if edge.user_intent is None:
        return False
    threshold = settings.graph_pin_challenge_count
    if threshold <= 0:
        return False
    return edge.challenge_count >= threshold


class GraphTombstone(BaseModel):
    """지운 취향의 **재파생 차단 표식** — 라벨을 담지 않는다 (§6.3, #499).

    개별 삭제는 **즉시 물리 삭제**다(undo 창 없음 — #499가 I-35를 폐기했다). 그래서 지운 edge 는
    `edges` 에서 통째로 사라지고 그 노드도 참조가 끊기면 함께 사라진다 — 사용자가 "지웠다"고
    믿는 문장의 원문이 문서에 남지 않는다.

    그런데 **그냥 지우기만 하면 다음 배치가 같은 fact 에서 같은 취향을 다시 만든다.** 삭제가
    조용히 무력화되는 것이다(AC-PROF-31). 그래서 "이건 사용자가 지웠다"는 표식만 남긴다.

    표식이 `edge_id` 하나로 성립하는 이유는 그것이 **내용 파생**이기 때문이다 —
    `"e_" + sha256("{predicate}|{node_id}")`. 같은 취향이 재파생되면 같은 `edge_id` 가 나오므로
    라벨 원문 없이도 차단할 수 있다(`make_edge_id` 의 결정론성이 기능 요구사항인 이유).

    시간 만료가 없다 — 만료시키면 `profile_idle_sweep_interval_s` 주기 배치가 같은 발화를
    재승격시켜 방금 지운 취향이 부활한다. 다만 "영구"는 *자동 만료 없음*이지 *사용자도 못 지움*이
    아니다(전체 초기화는 tombstone 도 지운다 — REQ-PGRAPH-061).
    """

    edge_id: str  # "e_" + sha256(edge_key)[:16] — 라벨을 담지 않는 유일한 식별자
    suppressed_at: str
    user_intent: UserIntent | None = None  # 지울 당시 pin 이 걸려 있었나(감사·재승격 판정용)


class GraphDocument(BaseModel):
    """`("graph", user_id)` / `"v1"` 단일 jsonb 문서 (SPEC §5.3, §7.1의 원자성 근거)."""

    revision: int  # 단조 증가. 초기화로도 되돌리지 않는다(REQ-PGRAPH-042 — 집행은 #358)
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    unprojected_count: int  # 트리플을 못 만든 fact 개수만. 내용은 어떤 형태로도 싣지 않는다
    # 이번 배치 절단에서 **버린 edge 가 있었나**(REQ-PGRAPH-005). SPEC §5.3 의 저장 모델에는 원래
    # 없던 필드다 — 명세는 절단을 §6.1 **투영 시점**으로 뒀는데 구현이 저장 폭주 방어를 위해
    # 쓰기 시점으로 옮겼기 때문에(`profile_graph_max_edges` 주석), 표식도 같이 옮겨야 한다.
    # 안 남기면 #150 투영은 저장분이 늘 상한 이하라 "절단 안 됨"만 내보내고, `len(edges) == 상한`
    # 추정은 "우연히 딱 상한인 정상 사용자"와 구분되지 않는다.
    # 뜻은 "상한을 넘겼나"가 아니라 **"버린 게 있나"** 다 — 사용자 삭제만으로 상한을 넘겨 전부
    # 보존한 경우는 거짓이다(자르지 않았으므로).
    truncated: bool
    purged_at: str | None
    updated_at: str
    # 사용자가 지운 취향의 재파생 차단 표식(#499·#358). **이 모듈에서 유일하게 기본값을 두는
    # 상태 필드다** — 모듈 docstring 의 "기본값을 두지 않는다" 규약은 병합 엔진이 tombstone
    # 필드를 의식적으로 정하게 하려는 것인데, 여기는 반대로 **필드가 없던 구 문서를 읽어야**
    # 한다. 기본값이 없으면 기존 사용자 문서가 전부 `ValidationError` 로 떨어져
    # `store.get_graph` 가 None 을 돌려주고, 그 순간 revision 이 0 으로 되돌아간다.
    #
    # `profile_graph_max_edges` 절단 대상이 아니다 — edge 가 아니라 별도 리스트이고,
    # 잘리면 지운 취향이 부활한다. 항목당 필드 3개라 증가 폭도 edge 와 비교가 안 된다.
    tombstones: list[GraphTombstone] = Field(default_factory=list)

    @model_validator(mode="after")
    def _absorb_legacy_suppressed_edges(self) -> "GraphDocument":
        """구 표현(`status="suppressed"` edge)을 tombstone 으로 흡수한다 — 마이그레이션 없이.

        #499 이전에는 지운 취향을 `suppressed` edge 로 **문서 안에 그대로** 뒀다. 그 edge 의
        `node_id` 는 `"brand:소니"` 라 **라벨 원문이 남아 있다**. 저장된 문서를 읽는 이 지점에서
        표식만 남기고 원문을 떨어뜨려, 별도 백필 잡 없이 읽는 즉시 새 표현으로 수렴시킨다.

        참조가 끊긴 노드도 함께 버린다 — 라벨은 노드에 있으므로 edge 만 지우면 원문이 남는다.
        """
        legacy = [edge for edge in self.edges if edge.status == "suppressed"]
        if not legacy:
            return self

        known = {tombstone.edge_id for tombstone in self.tombstones}
        for edge in legacy:
            if edge.edge_id in known:
                continue
            self.tombstones.append(
                GraphTombstone(
                    edge_id=edge.edge_id,
                    # 구 문서에 `suppressed_at` 이 비어 있으면 문서 시각으로 대신한다 — 시각을
                    # 날조하지 않으면서(문서가 마지막으로 쓰인 때는 사실이다) 필드를 채운다.
                    suppressed_at=edge.suppressed_at or self.updated_at,
                    user_intent=edge.user_intent,
                )
            )
        self.edges = [edge for edge in self.edges if edge.status != "suppressed"]
        referenced = {edge.node_id for edge in self.edges}
        self.nodes = [node for node in self.nodes if node.node_id in referenced]
        return self
