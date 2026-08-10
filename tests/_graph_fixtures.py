"""**그래프가 꽉 찬** 회원 픽스처 — INV-PGRAPH-ORDER 회귀 방어의 전제 (#361).

`SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-113 은 회원·게스트 동등성 회귀 테스트가 **그래프가 꽉 찬
상태에서도** 성립할 것을 요구한다. 기존 #119 스위트는 `store.set_summary` 로 요약 한 줄만 심어
검증하는데, 그것만으로는 "프로필이 비어 있어 통과하는 것"과 구분되지 않는다.

## 두 가지를 함께 심는다

`seed_full_graph` 는 `set_graph`(저장 그래프)와 `set_summary`(그 그래프를 말로 옮긴 요약)를
**둘 다** 한다. 그래프만 심으면 추천 경로에서 `0 == 0` 을 재게 되기 때문이다 — 오늘 추천 경로의
그래프 소비는 `builder._summary_input` → 요약 마크다운 → `reader.read_profile_summary` 경유
한 갈래뿐이고, 저장 문서를 직접 읽는 소비자는 투영(`graph_projection`, #360)밖에 없다. 요약이
없으면 회원과 게스트의 입력이 애초에 같아서 무엇을 바꿔도 테스트가 초록불로 남는다.

두 표현이 같은 취향을 말한다는 것은 `test_graph_fixtures.py` 가 강제한다(모든 edge 라벨이
마크다운에 등장). 강제하지 않으면 한쪽만 고쳤을 때 커버리지가 조용히 사라진다.

## 값이 `tests/_fakes.DEFAULT_PRODUCTS` 와 **실제로 교차해야 한다**

이 픽스처 설계에서 가장 중요한 결정이다. 예를 들어 선호 가격대를 3~5만원으로 두면 그 값이 필터로
새어도 fake 카탈로그(39000·48000·29000원) 3건이 그대로 남아 **유출을 감지하지 못한다.** 그래서
가격대는 5~10만원이고 회피 브랜드는 카탈로그에 실재하는 `BrandY` 다 — 새는 순간 후보가 준다.
그 성질 자체를 `test_graph_fixtures.py` 가 단언한다.

## 손으로 조립한다

`resolver.resolve_triple` 경유로 만들지 않는다 — 카테고리 통제 어휘 스냅이 pg-catalog 와 임베딩
API 를 타서 유닛 테스트에 맞지 않는다. 다만 식별자는 리터럴로 박지 않고 `make_node_id` ·
`make_edge_key` · `make_edge_id` 로 파생시킨다(식별 규칙이 바뀌면 픽스처도 따라 움직여야 한다).
시나리오는 `scripts/seed_profile_graph_356.py` 의 `_SCENARIO` 를 거울처럼 따라간다.
"""

from __future__ import annotations

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    make_edge_id,
    make_edge_key,
    make_node_id,
)
from app.agents.profile.store import get_profile_store

# 픽스처 시각 — 고정값이다. `datetime.now()` 를 쓰면 감쇠·정렬이 실행 시각에 따라 흔들린다.
GRAPH_NOW = "2026-08-10T00:00:00+00:00"
GRAPH_GENERATED_AT = "2026-08-10T00:00:00Z"
GRAPH_REVISION = 42

# (type, label, predicate) — 모든 `NodeType` 을 덮고, 필터 축과 겹치는 것은 **카탈로그와 교차하는
# 값**으로 고른다. 오른쪽 주석은 "이 값이 필터로 새면 어느 축이 어떻게 아픈가"다.
FULL_GRAPH_SPEC: tuple[tuple[str, str, str], ...] = (
    ("brand", "소니", "likes"),  # brandName — 카탈로그에 없어 새면 후보가 0 이 된다
    ("brand", "BrandY", "avoids"),  # brandName 배제 승격 유혹 — 실재 브랜드라 새면 102 가 빠진다
    ("category", "블루투스 이어폰", "likes"),  # categoryName — 카탈로그는 "무선이어폰" 이라 0 건
    ("category", "키보드", "avoids"),  # categoryName 배제
    ("priceBand", "50000-100000", "prefers"),  # minPrice — 새면 39000·48000·29000 전부 탈락
    ("ratingBand", "4.5-5", "prefers"),  # rating_min 사후필터 — 새면 4.2·3.9 가 탈락
    ("attribute", "노이즈캔슬링", "prefers"),  # keyword
    ("attribute", "검정", "prefers"),  # color
    ("product", "101", "purchased"),  # exclude_product_ids — 새면 101 이 빠진다
    ("situation", "캠핑", "interestedIn"),  # 필터 축 없음 — 어휘 커버리지용
)

# 위 라벨 전량. 스레드 필터 저장소·검색 페이로드에 **하나라도 나타나면 유출**이다.
GRAPH_LABELS: tuple[str, ...] = tuple(label for _, label, _ in FULL_GRAPH_SPEC)

# 그래프를 그대로 말로 옮긴 요약. 추천 경로가 실제로 읽는 것은 이쪽이다(모듈 docstring 참조).
FULL_GRAPH_MARKDOWN = """# 취향
- 소니 선호, BrandY 회피
- 블루투스 이어폰 선호, 키보드 회피
- 5~10만원대(50000-100000) 선호
- 평점 4.5 이상(4.5-5) 선호
- 노이즈캔슬링 중요, 검정 선호
- 캠핑에 관심
- 상품 101 구매 이력"""


def _edge(node_id: str, predicate: str) -> GraphEdge:
    """관측이 충분히 쌓여 승격까지 끝난 edge — "꽉 찬" 상태를 뜻한다."""
    edge_key = make_edge_key(predicate, node_id)
    return GraphEdge(
        edge_key=edge_key,
        edge_id=make_edge_id(edge_key),
        node_id=node_id,
        predicate=predicate,
        status="active",
        promoted=True,
        origin="machine",
        # 구매 사실만 출처가 다르다 — `purchased` 는 대화에서 만들어질 수 없다(api-spec §3.8).
        source_latest="purchase" if predicate == "purchased" else "conversation",
        confidence=0.9,
        evidence_count=3,
        evidence_by_source={"purchase" if predicate == "purchased" else "conversation": 3},
        evidence_refs=["f1", "f2", "f3"],
        first_observed_at="2026-07-01T00:00:00+00:00",
        last_observed_at=GRAPH_NOW,
        decay_evaluated_at=GRAPH_NOW,
        valid_from="2026-07-02T00:00:00+00:00",
        superseded_by=None,
        suppressed_at=None,
        user_intent=None,
        challenge_count=0,
        derived_from_sensitive=False,
        sensitive_topic=None,
    )


def full_graph_document() -> GraphDocument:
    """모든 `NodeType` 이 채워진 문서 — 전 항목 `active` 라 투영에도 전량 실린다."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for node_type, label, predicate in FULL_GRAPH_SPEC:
        node_id = make_node_id(node_type, label)
        nodes.append(GraphNode(node_id=node_id, type=node_type, label=label, verified=True))
        edges.append(_edge(node_id, predicate))
    return GraphDocument(
        revision=GRAPH_REVISION,
        nodes=nodes,
        edges=edges,
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=GRAPH_NOW,
        tombstones=[],
    )


async def seed_full_graph(user_id: str = "u1") -> GraphDocument:
    """회원에게 꽉 찬 그래프 **와** 그 그래프에서 파생된 요약을 함께 심는다.

    요약을 빼면 추천 경로에서 회원과 게스트의 입력이 같아져 동등성 테스트가 공허해진다
    (모듈 docstring 「두 가지를 함께 심는다」).
    """
    store = await get_profile_store()
    document = full_graph_document()
    await store.set_graph(user_id, document)
    await store.set_summary(user_id, FULL_GRAPH_MARKDOWN, GRAPH_GENERATED_AT)
    return document
