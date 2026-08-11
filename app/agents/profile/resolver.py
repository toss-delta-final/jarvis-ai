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
    GraphDocument,
    BAND_RE,
    GraphEdge,
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

_PRODUCT_ID_RE = re.compile(r"^\d+$")
_RATING_SCALE_MAX = 5  # 별점 도메인 상수 — 비즈니스 튜너블이 아니다(HTTP status code 와 동급)
# 숫자 라벨의 **크기** 상한. 형식(`^\d+$`)만 보면 30자리도 통과하고 `int()` 는 파이썬 임의 정밀도라
# 예외도 안 난다 — 존재할 수 없는 productId 노드가 문서 상한 슬롯을 영구히 차지하고, 소비자(#150)가
# Long 으로 파싱하면 오버플로우다(PR #410 리뷰). 상품 id 는 BIGINT 라는 CLAUDE.md 규약에서 오는
# **도메인 상수**지 튜너블이 아니다 — 값을 낮추면 정당한 id 가 드롭되고 높이면 DB 가 못 받는다.
_BIGINT_MAX = 9_223_372_036_854_775_807
# 밴드 kind 별 **도메인 경계**. 경계와 같은 값은 아무것도 걸러내지 않으므로(가격은 늘 0 이상,
# 평점은 늘 5 이하) 경계를 명시한 밴드와 생략한 밴드는 **같은 취향**이다 — `_resolve_band` 가
# 그 값을 접어 하나의 `node_id` 로 수렴시킨다(#581). 접지 않으면 `"0-100000"` 과 `"-100000"` 이
# 별개 노드가 되어, 같은 취향이 두 줄로 보이고 한쪽을 지워도 다른 쪽이 살아남는다
# (REQ-PGRAPH-010 — `"007"`→`"7"` 수렴을 만든 것과 같은 이유).
_BAND_DOMAIN: dict[str, tuple[int, int]] = {
    "priceBand": (0, _BIGINT_MAX),
    "ratingBand": (0, _RATING_SCALE_MAX),
}
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


@dataclass(frozen=True)
class ObjectSpec:
    """I-33 요청의 `object` — **두 형태 중 하나**다 (api-spec §3.9.1).

    `node_id` = 이미 확정된 노드를 가리킨다(FE 자동완성). `node_type`+`label` = 새 대상을 직접
    입력한다. 셋 다 없으면 "대상 유지"다 — `predicate` 만 바꾸는 요청.
    """

    node_id: str | None = None
    node_type: str | None = None
    label: str | None = None


async def resolve_user_object(
    spec: ObjectSpec | None,
    *,
    document: GraphDocument,
    current: GraphEdge,
    settings: Settings,
    now: str,
    category_exact: Callable[..., set[str]] | None = None,
) -> GraphNode | None:
    """I-33 의 `object` 를 노드로 확정한다 (#360, api-spec §3.9.1). 실패는 `None`.

    **배치 경로(`resolve_triple`)와 같은 정규화를 쓴다** — 갈리면 같은 취향이 두 개의 `edgeId` 를
    얻어 하나를 지워도 다른 하나가 살아남고, 재파생 차단 표식을 비켜간다(REQ-PGRAPH-010).
    그래서 라벨 손질도 `clean_label` 규약(공백 정리 + 상한)을 그대로 따른다 — `normalize_label`
    을 여기서 직접 부르지 않는다(그건 `make_node_id` 안에서 일어난다).

    **실패에 예외를 올리지 않는 것**은 이 모듈 규약이다. 다만 뜻은 배치와 다르다 — 배치의
    `None` 은 "드롭하고 계속"이고 여기의 `None` 은 **`400`** 이다(호출부가 옮긴다). 사용자가
    지목한 대상을 서버가 임의로 바꾸면 그것은 수정이 아니라 오염이라 추측하지 않는다.

    **임베딩·LLM 을 타지 않는다** — I-33 은 요청 경로(예산 3s)이고 [HARD] LLM 0회다.
    `category` 는 카탈로그 exact 조회 1회까지만 쓴다(`_resolve_user_category`).
    """
    if spec is None or (spec.node_id is None and spec.node_type is None and spec.label is None):
        # **대상 유지.** 기본값의 출처는 변경 시점에 잠금 아래에서 읽은 문서여야 한다 — 미리
        # 읽어 채우면 그 사이 값이 바뀌었을 때 사용자가 보내지 않은 변경이 적용된다(api-spec §3.9.1).
        return next((node for node in document.nodes if node.node_id == current.node_id), None)

    if spec.node_id is not None:
        if spec.node_type is not None or spec.label is not None:
            return None  # 두 형태 동시 지정 — 스키마가 먼저 막지만 심층 방어로 둔다
        # **재정규화하지 않는다** — 이미 확정된 값이라 다시 돌리면 어휘·임계값 변화에 따라
        # 사용자가 고른 것과 **다른 노드로 튄다**. 그 사용자 그래프 밖이면 새로 만들지 않는다.
        return next((node for node in document.nodes if node.node_id == spec.node_id), None)

    kind = spec.node_type
    if kind is None or kind not in _NODE_TYPES or not spec.label:
        return None
    clean_label = " ".join(spec.label.split())[: settings.profile_graph_label_max_chars]
    if not clean_label:
        return None

    # 사용자 입력에는 **발화 파생 앵커가 없다** — 빈 문자열이 정직한 값이다. 앵커의 용도가
    # 근접 매칭 재측정(OPEN-G1)인데 이 경로는 근접 매칭을 아예 타지 않는다.
    if kind in ("priceBand", "ratingBand"):
        return _resolve_band(kind, clean_label, anchor_phrase="", now=now)
    if kind == "product":
        return _resolve_product(clean_label, anchor_phrase="", now=now)
    if kind == "category":
        return await _resolve_user_category(
            clean_label, settings=settings, now=now, category_exact=category_exact
        )
    return _resolve_from_lexicon(kind, clean_label, anchor_phrase="", now=now, lexicon=None)


async def _resolve_user_category(
    label: str,
    *,
    settings: Settings,
    now: str,
    category_exact: Callable[..., set[str]] | None,
) -> GraphNode | None:
    """사용자 입력 카테고리 — **exact 조회만**. 근접 매칭 경로가 없다 (#360).

    `_resolve_category`(배치)를 재사용하지 않고 따로 쓴 이유는 **구조로 막기 위해서**다. 그쪽은
    exact 가 빗나가면 임베딩 최근접으로 넘어가는데, 앵커가 없으면 거기서 걸려 결과적으로는 같다 —
    하지만 그 가드는 **다른 이슈가 앵커 기본값을 넣는 순간 조용히 열린다.** 요청 경로의 [HARD]
    LLM 0회와 3s 예산을 우연한 조건에 맡기지 않는다.
    """
    if category_exact is None:
        from app.pipelines.category_search import (  # noqa: PLC0415 - LAZY(유닛 pg 의존 회피)
            exact_lookup,
        )

        category_exact = exact_lookup

    try:
        hits = await asyncio.to_thread(category_exact, [label], settings.catalog_db_url)
    except Exception:  # noqa: BLE001 - 어휘 조회 장애는 거절(호출부가 400 으로 옮긴다)
        return None
    if label not in hits:
        return None
    return GraphNode(
        node_id=make_node_id("category", label),
        type="category",
        label=label,
        verified=True,
        resolution=_resolution(
            "exact", anchor_phrase="", now=now, lexicon_version=_CATEGORY_LEXICON
        ),
    )


def _decide_predicate(kind: str, polarity: str, predicate_hint: str) -> Predicate | None:
    """관계는 코드가 정한다 — `predicate_hint` 는 힌트일 뿐 신뢰 대상이 아니다.

    `purchased` 는 **거부한다**. 그 원천은 질의 시점 구매 이력(api-spec §4.7 I-19)이지 발화가
    아니라서, 대화로 구매 사실을 만들면 재구매 dedup(결정 14-F)과 근거 노출 규약(REQ-PGRAPH-078)의
    전제가 함께 깨진다. 구매 언급을 `likes` 로 강등해 살리는 선택지도 있으나, 사용자가 잘못 생긴
    노드를 지울 경로(#150)가 아직 없어 **되돌릴 수 없는 쪽의 오류**가 된다.

    **두 필드 모두 정규화한 뒤 어휘로 판정한다**(PR #410 리뷰). 종전에는 `polarity == "negative"`
    문자열 동등 비교라, `"Negative"`·`" negative "` 처럼 표기만 흔들려도 **긍정으로 떨어졌다** —
    "소니는 별로예요"가 `likes 소니` 로 확정 저장되고 요약·rerank 가 사용자가 싫다고 말한 것을
    밀어 올린다. 이 파일의 다른 필드는 전부 "실패는 드롭"인데 여기만 **의미가 반전**됐다.
    같은 이유로 `purchased` 거부도 대소문자 하나로 뚫렸다(리뷰가 짚지 않은 자매 결함).

    어휘 밖은 긍정으로 흘려보내지 않고 **드롭**한다 — 극성을 모르면 취향의 방향을 모르는 것이고,
    반대 취향을 확정 저장하는 것보다 없는 편이 낫다(REQ-PGRAPH-012b 와 같은 정신).
    """
    if predicate_hint.strip().casefold() == "purchased":
        return None
    normalized = polarity.strip().casefold()
    if normalized == "negative":
        return "avoids"
    if normalized != "positive":
        return None
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

    **[#581] 한쪽 경계만 있는 밴드를 받는다** — `"-50000"`(이하만)·`"100000-"`(이상만).
    양쪽을 강제하던 시절에는 "5만원 이하" 같은 취향을 담을 자리가 없어 LLM 이 없는 쪽을
    지어냈고(실측: 하한 `0`, 상한 `999999999`), 지어낸 값이 형식을 **항상** 만족시켜서
    드롭 지표에도 안 잡혔다. 자연어를 해석하지 않는다는 위 규약은 그대로다 — 바뀐 것은
    LLM 이 제안할 수 있는 정규 형식의 범위지 파서가 추측을 시작한 것이 아니다.
    """
    match = BAND_RE.match(label)
    if match is None:
        return None
    low_text, high_text = match.groups()
    if not low_text and not high_text:
        return None  # `"-"` — 정규식은 통과하지만 경계가 없으면 밴드가 아니다

    low = int(low_text) if low_text else None
    high = int(high_text) if high_text else None

    # 아래 세 검사는 **각 경계를 따로 잰다.** 종전에는 `low >= high` 드롭 덕에 상한 하나만
    # 재도 하한이 함께 걸렸는데, 열린 밴드에는 짝이 되는 경계가 아예 없어 그 보장이 사라진다.
    if low is not None and high is not None and low >= high:
        return None
    if (low is not None and low > _BIGINT_MAX) or (high is not None and high > _BIGINT_MAX):
        return None  # 형식만 보면 30자리도 통과한다 — 크기도 도메인 범위 안이어야 한다
    if kind == "ratingBand" and (
        (low is not None and low > _RATING_SCALE_MAX)
        or (high is not None and high > _RATING_SCALE_MAX)
    ):
        return None  # `"6-"`("6점 이상")은 존재할 수 없는 평점이다

    # **도메인 경계와 같은 값은 접는다** — 아무것도 걸러내지 않는 경계라 명시하든 생략하든 같은
    # 취향이고, 접지 않으면 같은 취향이 두 `node_id` 를 얻는다(`_BAND_DOMAIN` 주석).
    # 검증 **뒤에** 접는 것이 중요하다 — 먼저 접으면 `"0-0"` 의 하한이 사라져 `low >= high` 를
    # 비켜간다(종전에 거부하던 값이 통과하는 퇴행).
    domain_min, domain_max = _BAND_DOMAIN[kind]
    if low == domain_min:
        low = None
    if high == domain_max:
        high = None
    if low is None and high is None:
        return None  # 양쪽이 다 도메인 경계였다 — 아무것도 안 거르는 밴드는 취향이 아니다

    # 빈 쪽은 빈 채로 재조립한다 — `int()` 왕복이 앞자리 0 을 없애는 수렴은 그대로 유지된다
    # (`"030-"` → `"30-"`). 이 문자열이 곧 `node_id` 이므로 두 경로가 같은 규칙을 써야 한다.
    canonical = f"{'' if low is None else low}-{'' if high is None else high}"
    return GraphNode(
        node_id=make_node_id(kind, canonical),
        type=kind,  # type: ignore[arg-type]
        label=canonical,
        verified=True,  # 파싱 성공이 곧 검증 — 외부 어휘가 필요 없다
        resolution=_resolution("rule", anchor_phrase=anchor_phrase, now=now),
    )


def _resolve_product(label: str, *, anchor_phrase: str, now: str) -> GraphNode | None:
    """상품은 숫자 productId 형식 일치만 (REQ-PGRAPH-014). **`verified` 는 False 다.**

    AI 카탈로그는 상품 원본 컬럼(상품명)을 저장하지 않으므로(CLAUDE.md) 이름으로 붙일 어휘가
    아예 없다 — 이름 비슷한 것을 근접 탐색으로 고르면 **다른 상품**을 취향으로 박게 된다.

    `verified=False` 인 이유(PR #410 리뷰) — 미구현 표식이 아니라 **정확한 답**이다:
      - `verified` 의 축은 "통제 어휘에 스냅됐는가"인데, 상품 id 는 어휘의 원소가 아니라
        **외부 엔티티를 가리키는 참조**다. 정규식 통과는 그 축의 답이 될 수 없다.
      - 쓰기 시점 존재 검증은 애초에 답이 아니다 — 있던 상품도 품절·판매종료로 사라진다(#310).
        참조는 **읽을 때** 확인해야 하고, 그건 소비자(#150 → Spring 조회)의 책임이다.
      - 대조할 어휘도 없다: pg-catalog `products` 는 I-17 배치가 처리한 분만 있어 신상품이
        거짓 음성이 되고, 상품 원본은 Spring 이 소유한다.
    REQ-PGRAPH-013 이 이 상태를 정의한다 — "노출은 하되 신뢰하지 않는다". `brand` 도 통제 어휘
    미확보(C-28)로 같은 값이다. **카탈로그 조회를 붙여 True 로 올리는 방향으로 고치지 말 것.**
    """
    if not _PRODUCT_ID_RE.match(label):
        return None
    # 밴드와 같은 규약으로 **정수 왕복 정규화**한다 — 정규식이 "007" 과 "7" 을 둘 다 통과시키는데
    # `normalize_label` 은 NFKC·공백·casefold 만 해서 앞자리 0 을 없애지 않는다. 그대로 두면 같은
    # 상품이 `product:7`·`product:007` 로 갈리고, 사용자가 지운 상품이 다른 표기로 다시 언급될 때
    # 새 active 노드로 부활해 tombstone 을 우회한다 — 식별자 결정론은 이 이슈의 기능 요구사항이다
    # (REQ-PGRAPH-010, PR #410 리뷰). 정상 id 에는 no-op 이다.
    product_id = int(label)
    if product_id > _BIGINT_MAX:  # BIGINT 밖이면 존재할 수 없는 id 다 — 형식만으로는 안 걸린다
        return None
    canonical = str(product_id)
    return GraphNode(
        node_id=make_node_id("product", canonical),
        type="product",
        label=canonical,
        verified=False,  # 형식 일치일 뿐 어휘 스냅이 아니다 — 위 docstring 참조
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
