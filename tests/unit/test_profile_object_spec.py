"""I-33 요청의 `object` 해석 — 사용자 입력을 안정 식별자로 확정한다 (#360, api-spec §3.9.1).

배치 경로(`resolve_triple`)와 **같은 정규화**를 써야 한다. 다른 식별자가 나오면 같은 취향이 두
개의 `edgeId` 를 얻고, 하나를 지워도 다른 하나가 살아남아 **재파생 차단 표식을 비켜간다**
(REQ-PGRAPH-010 — 식별자 결정론이 기능 요구사항인 이유).

배치와 다른 점은 **실패를 누가 해석하느냐**다. 여기서도 실패는 `None` 이고 예외를 올리지 않는다
(resolver 규약) — 다만 호출부(`graph_journal`)가 그 `None` 을 `400` 으로 옮긴다. 배치는 드롭해도
요약이 계속 돌면 되지만, 사용자가 지목한 대상을 서버가 **임의로 바꾸면 그것은 수정이 아니라
오염**이기 때문이다.

그리고 **임베딩·LLM 을 타지 않는다** — I-33 은 요청 경로(예산 3s)이고 [HARD] LLM 0회다.
`category` 는 카탈로그 exact 조회 1회까지만 쓴다.
"""

from unittest.mock import Mock

import pytest

from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    make_edge_id,
    make_edge_key,
)
from app.agents.profile.resolver import ObjectSpec, resolve_triple, resolve_user_object
from app.core.config import Settings

NOW = "2026-08-11T00:00:00+00:00"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _node(node_id: str, node_type: str, label: str) -> GraphNode:
    return GraphNode(node_id=node_id, type=node_type, label=label, verified=False)  # type: ignore[arg-type]


def _edge(predicate: str = "likes", node_id: str = "brand:소니") -> GraphEdge:
    key = make_edge_key(predicate, node_id)
    return GraphEdge(
        edge_key=key,
        edge_id=make_edge_id(key),
        node_id=node_id,
        predicate=predicate,  # type: ignore[arg-type]
        status="active",
        promoted=True,
        origin="machine",
        source_latest="conversation",
        confidence=0.6,
        evidence_count=1,
        evidence_by_source={"conversation": 1},
        evidence_refs=["f1"],
        first_observed_at=NOW,
        last_observed_at=NOW,
        decay_evaluated_at=NOW,
        valid_from=NOW,
        superseded_by=None,
        suppressed_at=None,
        user_intent=None,
        challenge_count=0,
        derived_from_sensitive=False,
        sensitive_topic=None,
    )


def _document(*nodes: GraphNode, edges: list[GraphEdge] | None = None) -> GraphDocument:
    return GraphDocument(
        revision=42,
        nodes=list(nodes),
        edges=edges if edges is not None else [_edge()],
        unprojected_count=0,
        truncated=False,
        purged_at=None,
        updated_at=NOW,
        tombstones=[],
    )


def _never_called(name: str) -> Mock:
    """호출되면 실패해야 하는 자리 — 호출 여부 자체가 단언 대상이다."""
    return Mock(side_effect=AssertionError(f"{name} must not be called on the I-33 path"))


# ─────────── object 생략 — 대상을 유지한다 ───────────


async def test_omitting_object_keeps_the_current_target(settings: Settings) -> None:
    """`object` 를 안 보내면 관계만 바뀌고 대상은 그대로다 (api-spec §3.9.1).

    기본값의 출처는 **변경 시점에 잠금 아래에서 읽은 edge** 여야 한다 — 그래서 문서를 함께 받는다.
    """
    node = _node("brand:소니", "brand", "소니")
    document = _document(node)

    resolved = await resolve_user_object(
        None, document=document, current=_edge(), settings=settings, now=NOW
    )

    assert resolved is not None and resolved.node_id == "brand:소니"


async def test_omitting_object_fails_when_the_target_node_is_missing(settings: Settings) -> None:
    """참조가 끊긴 문서에서는 기본값을 만들 수 없다 — 라벨을 지어내지 않는다."""
    document = _document()  # 노드 없음

    resolved = await resolve_user_object(
        None, document=document, current=_edge(), settings=settings, now=NOW
    )

    assert resolved is None


# ─────────── nodeId 형태 — 재정규화하지 않는다 ───────────


async def test_node_id_form_returns_the_stored_node_verbatim(settings: Settings) -> None:
    """이미 확정된 노드는 **다시 정규화하지 않는다** (api-spec §3.9.1).

    재정규화하면 어휘·임계값이 바뀌었을 때 **사용자가 고른 것과 다른 노드로 튄다.**
    """
    node = _node("priceBand:30000-50000", "priceBand", "30000-50000")
    document = _document(node)

    resolved = await resolve_user_object(
        ObjectSpec(node_id="priceBand:30000-50000"),
        document=document,
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is node  # 사본이 아니라 그 노드 자체


async def test_node_id_outside_the_users_graph_is_rejected(settings: Settings) -> None:
    """형식은 맞지만 그 사용자 그래프에 없는 `nodeId` 는 **새로 만들지 않는다** (api-spec §3.9.1).

    자동완성 경로에서 나올 수 없는 요청이다 — 만들어 주면 남의 그래프 값을 찔러보는 통로가 된다.
    """
    document = _document(_node("brand:소니", "brand", "소니"))

    resolved = await resolve_user_object(
        ObjectSpec(node_id="brand:애플"),
        document=document,
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is None


async def test_both_forms_together_are_refused(settings: Settings) -> None:
    """`nodeId` 와 `type`+`label` 을 함께 실으면 해석하지 않는다 — 스키마가 먼저 막지만 심층 방어다."""
    document = _document(_node("brand:소니", "brand", "소니"))

    resolved = await resolve_user_object(
        ObjectSpec(node_id="brand:소니", node_type="brand", label="소니"),
        document=document,
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is None


# ─────────── type+label 형태 — 배치와 같은 식별자가 나와야 한다 ───────────


@pytest.mark.parametrize(
    ("label", "expected_node_id"),
    [
        ("30000-50000", "priceBand:30000-50000"),
        ("-50000", "priceBand:-50000"),  # 열린 밴드도 canonical 이다(#581, api-spec §3.9.1)
        ("100000-", "priceBand:100000-"),
        # 도메인 경계(가격 하한 0)는 접힌다 — 사용자가 명시해도 배치 경로와 같은 노드로 간다.
        ("0-10000", "priceBand:-10000"),
    ],
)
async def test_price_band_accepts_the_canonical_form(
    settings: Settings, label: str, expected_node_id: str
) -> None:
    resolved = await resolve_user_object(
        ObjectSpec(node_type="priceBand", label=label),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is not None and resolved.node_id == expected_node_id


@pytest.mark.parametrize(
    "label",
    [
        "가성비",
        "3~5만원대",
        "5만원 이하",
        "50000-30000",
        "",
        # 경계가 둘 다 없으면 밴드가 아니다 — 열린 밴드를 열어도 이건 아니다(#581)
        "-",
        # §3.8 이 내보내는 렌더 문장을 그대로 되보내는 경우. **파싱하지 않는다** —
        # 문장을 숫자로 되돌리는 파서를 두면 그 순간 "반쯤 맞는 해석"이 다시 생긴다.
        # FE 는 `nodeId` 에서 접두어를 뗀 canonical 을 실어야 한다(api-spec §3.9.1).
        "50,000원 이하",
        "30,000원 이상, 50,000원 이하",
    ],
)
async def test_price_band_rejects_natural_language(settings: Settings, label: str) -> None:
    """자연어 가격 표현은 **추측하지 않는다** — 반쯤 맞는 해석은 조용히 틀린 밴드를 만든다."""
    resolved = await resolve_user_object(
        ObjectSpec(node_type="priceBand", label=label),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is None


@pytest.mark.parametrize(
    ("label", "anchor"),
    [
        ("30000-50000", "3만원에서 5만원 사이"),
        # 열린 밴드도 두 경로가 갈리면 안 된다(#581) — 재조립이 한쪽에서만 빈 쪽을
        # 다르게 처리하면 같은 취향이 두 edgeId 를 얻는다.
        ("-50000", "5만원 이하로"),
        ("100000-", "10만원 이상은 돼야"),
    ],
)
async def test_the_identifier_matches_the_batch_path(
    settings: Settings, label: str, anchor: str
) -> None:
    """**같은 취향은 어느 경로로 들어와도 같은 식별자여야 한다** (REQ-PGRAPH-010).

    갈리면 사용자가 지운 취향이 다른 식별자로 부활해 tombstone 을 비켜간다.
    """
    batch = await resolve_triple(
        kind="priceBand",
        label=label,
        anchor_phrase=anchor,
        polarity="positive",
        predicate_hint="",
        settings=settings,
        now=NOW,
        embed=_never_called("embed"),
    )
    user = await resolve_user_object(
        ObjectSpec(node_type="priceBand", label=label),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert batch is not None and user is not None
    assert user.node_id == batch.node.node_id


@pytest.mark.parametrize("label", ["7", "007"])
async def test_product_label_is_normalised_through_int(settings: Settings, label: str) -> None:
    """`"007"` 과 `"7"` 이 같은 노드여야 한다 — 표기가 갈리면 삭제가 반쪽만 먹는다."""
    resolved = await resolve_user_object(
        ObjectSpec(node_type="product", label=label),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is not None and resolved.node_id == "product:7"


async def test_product_label_must_be_numeric(settings: Settings) -> None:
    """상품명으로는 붙일 어휘가 없다 — 이름 비슷한 것을 고르면 **다른 상품**을 취향으로 박는다."""
    resolved = await resolve_user_object(
        ObjectSpec(node_type="product", label="소니 이어폰"),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is None


async def test_brand_without_a_lexicon_is_accepted_but_unverified(settings: Settings) -> None:
    """브랜드 통제 어휘는 아직 없다(C-28) — 그 상태에서도 동작하되 `verified` 는 False 다."""
    resolved = await resolve_user_object(
        ObjectSpec(node_type="brand", label="소니"),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is not None
    assert resolved.node_id == "brand:소니"
    assert resolved.verified is False


async def test_unknown_node_type_is_rejected(settings: Settings) -> None:
    """§3.8 어휘 밖 `type` 은 해석하지 않는다."""
    resolved = await resolve_user_object(
        ObjectSpec(node_type="mood", label="차분한"),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
    )

    assert resolved is None


# ─────────── category — exact 조회만, 임베딩 금지 ───────────


async def test_category_snaps_on_an_exact_catalogue_hit(settings: Settings) -> None:
    """카탈로그에 있는 이름이면 붙고 `verified` 는 True 다 — 인덱스 조회 1회."""
    resolved = await resolve_user_object(
        ObjectSpec(node_type="category", label="블루투스 이어폰"),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
        category_exact=lambda labels, dsn: {"블루투스 이어폰"},
    )

    assert resolved is not None
    assert resolved.node_id == "category:블루투스 이어폰"
    assert resolved.verified is True


async def test_category_miss_is_rejected_and_never_embeds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exact 가 빗나가면 **거기서 끝난다** — 임베딩 근접 매칭으로 넘어가지 않는다.

    I-33 은 요청 경로(예산 3s)이고 [HARD] LLM 0회다. 그리고 근접 매칭은 사용자가 지목하지 않은
    대상에 취향을 붙일 수 있다.

    **임베딩 진입점을 실제로 막아 둔다** — 지금은 `_resolve_user_category` 에 `embed` 인자가
    없어 구조적으로 못 부르지만, 나중에 누가 배치용 `_resolve_category` 로 배선을 바꾸면 그쪽은
    앵커만 있으면 임베딩을 탄다. 그 변경이 조용히 통과하지 않게 여기서 잡는다.
    """
    import app.pipelines.category_search as category_search
    import app.pipelines.embedding as embedding

    monkeypatch.setattr(embedding, "embed_texts", _never_called("embed_texts"))
    monkeypatch.setattr(
        category_search, "search_categories_pg", _never_called("search_categories_pg")
    )

    resolved = await resolve_user_object(
        ObjectSpec(node_type="category", label="이어폰류"),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
        category_exact=lambda labels, dsn: set(),
    )

    assert resolved is None


async def test_category_lookup_failure_is_a_rejection_not_a_crash(settings: Settings) -> None:
    """카탈로그 조회가 죽어도 예외를 올리지 않는다 — 호출부가 `400` 으로 옮긴다."""

    def _boom(labels: object, dsn: object) -> set[str]:
        raise RuntimeError("catalog down")

    resolved = await resolve_user_object(
        ObjectSpec(node_type="category", label="블루투스 이어폰"),
        document=_document(),
        current=_edge(),
        settings=settings,
        now=NOW,
        category_exact=_boom,
    )

    assert resolved is None
