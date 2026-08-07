"""kind별 결정론적 resolver — LLM 제안을 안정 식별자로 확정한다 (SPEC-PROFILE-GRAPH-149 §6.2).

**LLM 은 키를 만들지 않는다**(REQ-PGRAPH-011). 델타 추출은 타입 붙은 제안(`kind`·`label`·
`anchorPhrase`·`polarity`·`predicateHint`)까지만 내고, `node_id`·`edge_key`·`edge_id` 확정은
여기서 코드가 한다 — LLM 이 라벨을 직접 확정하면 같은 발화에서도 값이 흔들리고 오타가 난 실측이
있고(#115), 흔들리는 키에는 누적이 성립하지 않는다.

`DESIGN-CATEGORY-HYBRID-59` 패턴을 따르되 **두 지점이 다르다**(REQ-PGRAPH-012):
  (a) 앵커는 LLM 라벨이 아니라 **발화 파생 구절**(`anchor_phrase`)이다 — 추상 라벨은 어휘와의
      문자열 겹침으로 가짜 근접을 만든다(#59 §4.3.1: 라벨 앵커 채택 12건 중 11건 오분류).
  (b) **never-null 을 쓰지 않는다 — 멀면 드롭한다.** 틀린 노드는 측정된 품질 손실(-0.053/-0.117)을
      만들고 없는 노드는 손실이 ≈0 이라, 프로필 생성 시점에는 드롭이 항상 더 싸다.

실패는 **전부 드롭이며 예외를 올리지 않는다**. 여기서 예외를 전파하면 임베딩 백엔드 장애 하나가
consolidation 전체를 영구 RETRYABLE 로 만든다(GOOGLE_API_KEY 미구성 시 로컬만 통과하고 키 없는
환경이 폭발하는 전례 — docs/lessons.md).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from app.agents.profile.graph_models import (
    GraphNode,
    NodeResolution,
    Predicate,
    make_edge_id,
    make_edge_key,
    make_node_id,
    normalize_label,
)
from app.core.config import Settings

_NODE_TYPES: frozenset[str] = frozenset(
    {"brand", "category", "attribute", "priceBand", "ratingBand", "product", "situation"}
)
# 어휘 없이 라벨만으로 노드를 세우는 kind. brand 는 어휘가 생기면(C-28) 스냅 경로로 올라간다.
_UNVERIFIED_KINDS: frozenset[str] = frozenset({"brand", "attribute", "situation"})
# polarity 가 positive 일 때 node type 이 정하는 predicate. negative 는 전부 avoids 다.
_POSITIVE_PREDICATE: dict[str, Predicate] = {
    "priceBand": "prefers",
    "ratingBand": "prefers",
    "attribute": "prefers",
    "brand": "likes",
    "category": "likes",
    "product": "likes",
    "situation": "interestedIn",
}

_BAND_RE = re.compile(r"^(\d+)-(\d+)$")
_PRODUCT_ID_RE = re.compile(r"^\d+$")
_RATING_SCALE_MAX = 5  # 별점 도메인 상수 — 비즈니스 튜너블이 아니다(HTTP status code 와 동급)
_CATEGORY_LEXICON = "catalog_categories"


@dataclass(frozen=True)
class ResolvedTriple:
    """확정된 트리플. `generate_session_delta` 가 fact 값에 실어 저장한다."""

    node: GraphNode
    predicate: Predicate
    edge_key: str
    edge_id: str

    def as_payload(self, *, salience: float, source: str) -> dict:
        """fact 값(`graph_triples`)에 저장하는 직렬화 형태 — **이 모양이 병합 엔진의 입력 계약**이다.

        `graph_merge` 가 여기서 읽는 것이 전부이므로 필드를 빼면 병합이 조용히 값을 잃는다.
        `salience` 를 함께 싣는 이유는 confidence 가 관측 salience 의 감쇠 가중 EMA 라서다
        (REQ-PGRAPH-015) — 게이트 판정 직후 버려지던 신호를 여기서 처음으로 영속한다
        (SPEC-PROFILE-001 OPEN-P12 가 지적한 "누적할 상태가 없다"의 해소 지점).
        관측 시각과 fact key 는 store item 이 이미 갖고 있어 중복 저장하지 않는다.
        """
        return {
            "node": self.node.model_dump(mode="json"),
            "predicate": self.predicate,
            "edge_key": self.edge_key,
            "edge_id": self.edge_id,
            "salience": salience,
            "source": source,
        }


async def resolve_triple(
    *,
    kind: str,
    label: str,
    anchor_phrase: str,
    polarity: str,
    predicate_hint: str,
    settings: Settings,
    now: str,
    brand_lexicon: Mapping[str, str] | None = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    category_exact: Callable[..., set[str]] | None = None,
    category_search: Callable[..., list[tuple[str, float]]] | None = None,
) -> ResolvedTriple | None:
    """LLM 제안 하나를 트리플로 확정한다. 확신이 없으면 `None`(드롭).

    I/O(임베딩·pgvector)는 주입 seam 이며 기본값은 lazy 바인딩이다 — 유닛 테스트가 실 API 키나
    pg 없이 돌고, 임베딩을 쓰지 말아야 할 kind 가 실제로 안 쓰는지 호출 여부로 단언할 수 있다.
    """
    if kind not in _NODE_TYPES:
        return None

    clean_label = " ".join(label.split())[: settings.profile_graph_label_max_chars]
    if not clean_label:
        return None

    # 앵커도 상한을 받는다 — 저장(단일 jsonb 문서의 `NodeResolution.anchor_phrase`)과 임베딩
    # 페이로드 양쪽에 실려서, 무제한이면 문서와 API 요청이 통제 없이 커진다(PR #410 리뷰).
    # 상한이 `chat_message_max_chars` 인 근거는 **인용 구절은 인용 대상보다 길 수 없다**는 것이다.
    # 라벨 상한(60자)을 재사용하지 않는다 — 정당한 인용이 잘리면 임베딩 입력이 바뀌어 카테고리
    # 판정이 달라진다. 앵커를 라벨 대신 쓰는 이유 자체가 #59 의 오분류 실측이라(§6.2a) 그쪽을
    # 건드리는 상한은 위험 대비 이득이 없다. 정상 앵커(한국어 구절 10~30자)는 안 잘린다.
    anchor_phrase = " ".join(anchor_phrase.split())[: settings.chat_message_max_chars]

    predicate = _decide_predicate(kind, polarity, predicate_hint)
    if predicate is None:
        return None

    if kind in ("priceBand", "ratingBand"):
        node = _resolve_band(kind, clean_label, anchor_phrase=anchor_phrase, now=now)
    elif kind == "product":
        node = _resolve_product(clean_label, anchor_phrase=anchor_phrase, now=now)
    elif kind == "category":
        node = await _resolve_category(
            clean_label,
            anchor_phrase=anchor_phrase,
            settings=settings,
            now=now,
            embed=embed,
            category_exact=category_exact,
            category_search=category_search,
        )
    else:
        node = _resolve_from_lexicon(
            kind, clean_label, anchor_phrase=anchor_phrase, now=now, lexicon=brand_lexicon
        )

    if node is None:
        return None

    edge_key = make_edge_key(predicate, node.node_id)
    return ResolvedTriple(
        node=node, predicate=predicate, edge_key=edge_key, edge_id=make_edge_id(edge_key)
    )


def _decide_predicate(kind: str, polarity: str, predicate_hint: str) -> Predicate | None:
    """관계는 코드가 정한다 — `predicate_hint` 는 힌트일 뿐 신뢰 대상이 아니다.

    `purchased` 는 **거부한다**. 그 원천은 질의 시점 구매 이력(api-spec §4.7 I-19)이지 발화가
    아니라서, 대화로 구매 사실을 만들면 재구매 dedup(결정 14-F)과 근거 노출 규약(REQ-PGRAPH-078)의
    전제가 함께 깨진다. 구매 언급을 `likes` 로 강등해 살리는 선택지도 있으나, 사용자가 잘못 생긴
    노드를 지울 경로(#150)가 아직 없어 **되돌릴 수 없는 쪽의 오류**가 된다.
    """
    if predicate_hint == "purchased":
        return None
    if polarity == "negative":
        return "avoids"
    return _POSITIVE_PREDICATE.get(kind)


def _resolution(
    method: str,
    *,
    anchor_phrase: str,
    now: str,
    distance: float | None = None,
    margin: float | None = None,
    lexicon_version: str | None = None,
) -> NodeResolution:
    return NodeResolution(
        method=method,  # type: ignore[arg-type]
        distance=distance,
        margin=margin,
        lexicon_version=lexicon_version,
        anchor_phrase=anchor_phrase,
        resolved_at=now,
    )


def _resolve_band(kind: str, label: str, *, anchor_phrase: str, now: str) -> GraphNode | None:
    """`"30000-50000"` 형태만 받는 엄격 파서 (REQ-PGRAPH-014 — 숫자에 근접 탐색을 쓰지 않는다).

    한국어 자연어 가격 표현("가성비"·"5만원대"·"십만 원"·"3~5만원")은 정규식으로 덮을 수 없어
    **시도하지 않는다** — 반쯤 맞는 정규식은 조용히 틀린 밴드를 만든다. 대신 델타 추출 프롬프트가
    `label` 을 정규 형식으로 *제안*하게 하고, 여기서 결정론적으로 검증만 한다. 제안이 형식을
    못 맞추면 드롭이고, 그 빈도는 분포 프로브가 관측한다(OPEN-G8).
    """
    match = _BAND_RE.match(label)
    if match is None:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    if low >= high:
        return None
    if kind == "ratingBand" and high > _RATING_SCALE_MAX:
        return None

    canonical = f"{low}-{high}"
    return GraphNode(
        node_id=make_node_id(kind, canonical),
        type=kind,  # type: ignore[arg-type]
        label=canonical,
        verified=True,  # 파싱 성공이 곧 검증 — 외부 어휘가 필요 없다
        resolution=_resolution("rule", anchor_phrase=anchor_phrase, now=now),
    )


def _resolve_product(label: str, *, anchor_phrase: str, now: str) -> GraphNode | None:
    """상품은 숫자 productId 정확 일치만 (REQ-PGRAPH-014).

    AI 카탈로그는 상품 원본 컬럼(상품명)을 저장하지 않으므로(CLAUDE.md) 이름으로 붙일 어휘가
    아예 없다 — 이름 비슷한 것을 근접 탐색으로 고르면 **다른 상품**을 취향으로 박게 된다.
    """
    if not _PRODUCT_ID_RE.match(label):
        return None
    return GraphNode(
        node_id=make_node_id("product", label),
        type="product",
        label=label,
        verified=True,
        resolution=_resolution("rule", anchor_phrase=anchor_phrase, now=now),
    )


def _resolve_from_lexicon(
    kind: str,
    label: str,
    *,
    anchor_phrase: str,
    now: str,
    lexicon: Mapping[str, str] | None,
) -> GraphNode | None:
    """brand·attribute·situation — 통제 어휘 alias→canonical 스냅, 어휘가 없으면 미검증 통과.

    어휘 유무로 실패 처리가 갈리는 것이 핵심이다:
      - **어휘 없음**(현재 brand: C-28/OPEN-G2, attribute: OPEN-G4) → `verified: false` 로 남긴다.
        이 상태에서도 동작해야 하고, 어휘가 생기면 재파생으로 승격된다. 정규화만으로는 `소니`와
        `SONY` 가 문자 체계가 달라 안 합쳐지고 `쏘니` 같은 오타는 더더욱 못 잡는다 — 그건
        어휘의 일이지 정규화의 일이 아니다.
      - **어휘 있는데 못 붙음** → 드롭(REQ-PGRAPH-012b). 붙일 기준이 있는데 못 붙었다는 것은
        그 표기가 통제 어휘 밖이라는 뜻이다.

    어휘 모양은 `color_synonyms`(#258) 선례를 따른다 — term(실재 표기) → canonical + 사람 검수.
    """
    if kind not in _UNVERIFIED_KINDS:
        return None

    if lexicon:
        canonical = _lookup_canonical(lexicon, label)
        if canonical is None:
            return None
        return GraphNode(
            node_id=make_node_id(kind, canonical),
            type=kind,  # type: ignore[arg-type]
            label=canonical,
            verified=True,
            resolution=_resolution(
                "exact", anchor_phrase=anchor_phrase, now=now, lexicon_version=f"{kind}_lexicon"
            ),
        )

    return GraphNode(
        node_id=make_node_id(kind, label),
        type=kind,  # type: ignore[arg-type]
        label=label,
        verified=False,
        resolution=_resolution("no_vocabulary", anchor_phrase=anchor_phrase, now=now),
    )


def _lookup_canonical(lexicon: Mapping[str, str], label: str) -> str | None:
    """정규화 기준으로 alias 를 찾는다 — 어휘 쪽 표기 흔들림에 매번 걸리지 않게."""
    target = normalize_label(label)
    for alias, canonical in lexicon.items():
        if normalize_label(alias) == target:
            return canonical
    return None


async def _resolve_category(
    label: str,
    *,
    anchor_phrase: str,
    settings: Settings,
    now: str,
    embed: Callable[[list[str]], list[list[float]]] | None,
    category_exact: Callable[..., set[str]] | None,
    category_search: Callable[..., list[tuple[str, float]]] | None,
) -> GraphNode | None:
    """카탈로그 잎 이름이 통제 어휘다 — 유일하게 임베딩을 쓰는 경로.

    exact 를 먼저 보는 이유는 #59 와 같다: DB 검증값은 거리 비교 대상이 아니다. exact 조회와
    임베딩 경로를 각각 감싸는 이유도 같다 — 한쪽 장애가 다른 쪽까지 죽이지 않게 격리한다.
    """
    # 주입받은 settings 를 그대로 쓴다 — 전역 get_settings() 를 다시 부르면 호출자가 넘긴
    # Settings 의 catalog_db_url 이 무시된다(`category_mapping.map_categories` 와 같은 규약).
    dsn = settings.catalog_db_url

    if category_exact is None or category_search is None or embed is None:
        from app.pipelines.category_search import (  # noqa: PLC0415 - LAZY(유닛 pg 의존 회피)
            exact_lookup,
            search_categories_pg,
        )
        from app.pipelines.embedding import embed_texts  # noqa: PLC0415

        category_exact = category_exact or exact_lookup
        category_search = category_search or search_categories_pg
        embed = embed or embed_texts

    try:
        hits = await asyncio.to_thread(category_exact, [label], dsn)
    except Exception:  # noqa: BLE001 - 어휘 조회 장애는 드롭(예외 전파 시 배치 전체가 죽는다)
        hits = set()
    if label in hits:
        return GraphNode(
            node_id=make_node_id("category", label),
            type="category",
            label=label,
            verified=True,
            resolution=_resolution(
                "exact", anchor_phrase=anchor_phrase, now=now, lexicon_version=_CATEGORY_LEXICON
            ),
        )

    # 공백 정리·길이 상한은 `resolve_triple` 이 이미 걸었다 — 여기서 다시 걸면 두 곳이 갈릴 수 있다.
    anchor = anchor_phrase
    if not anchor:
        return None

    try:
        # 앵커는 질의 쪽이라 task_type 비대칭을 지킨다(시드는 RETRIEVAL_DOCUMENT, #65).
        vectors = await asyncio.to_thread(embed, [anchor])
        # top-k 는 기존 `category_top_k` 를 재사용한다 — 같은 연산(앵커 최근접 조회)이고, margin
        # 계산은 top1·top2 만 쓰므로 k>=2 이후로는 값이 결과를 바꾸지 않는다. 거리 임계와 달리
        # 앵커 분포에 종속하지 않아 두 번째 키를 만들 이유가 없다(graph_node_distance_max 주석 참조).
        rows = await asyncio.to_thread(category_search, vectors[0], dsn, k=settings.category_top_k)
    except Exception:  # noqa: BLE001 - 임베딩·검색 장애는 드롭
        return None

    picked = _top1_with_margin(rows)
    if picked is None:
        return None
    canonical, distance, margin = picked

    if distance > settings.graph_node_distance_max:
        # margin None(히트 1건)은 확신을 잴 수 없어 예외 대상이 아니다 — 0.0(동점)과 다르다.
        if margin is None or margin < settings.graph_node_override_margin:
            return None

    return GraphNode(
        node_id=make_node_id("category", canonical),
        type="category",
        label=canonical,
        verified=True,
        resolution=_resolution(
            "embedding",
            anchor_phrase=anchor,
            now=now,
            distance=distance,
            margin=margin,
            lexicon_version=_CATEGORY_LEXICON,
        ),
    )


def _top1_with_margin(
    hits: Sequence[tuple[str, float]],
) -> tuple[str, float, float | None] | None:
    """거리 오름차순 top-k 에서 `(canonical, distance, margin)`.

    margin(2위−1위 거리차)은 "1등이 2등을 얼마나 확실히 이겼나"다. 히트 1건이면 계산이 불가능해
    **0.0 이 아니라 None** — 0.0 은 동점(가장 애매한 상태)으로 오독된다. `category_mapping` 의
    같은 이름 헬퍼와 규약이 동일하다.
    """
    if not hits:
        return None
    canonical, distance = hits[0]
    margin = round(hits[1][1] - distance, 4) if len(hits) > 1 else None
    return canonical, round(distance, 4), margin
