"""꽉 찬 그래프 픽스처 자체를 검증한다 (#361).

**픽스처에 자체 테스트가 필요한 이유**는 이것이 회귀 방어의 *측정 도구*이기 때문이다. 도구가
무기력해지면 이 도구를 쓰는 모든 테스트가 조용히 초록불로 남는다 — 실패가 아니라 **거짓 통과**로
망가지므로, 망가진 사실이 어디에도 드러나지 않는다.

여기서 고정하는 세 가지:
  1. 어휘를 전부 덮는가 — `NodeType`·`Predicate` 가 늘어나면 픽스처가 따라오게 강제한다.
  2. 그래프와 요약이 같은 취향을 말하는가 — 한쪽만 고치면 커버리지가 사라진다.
  3. **유출되면 실제로 아픈가** — 픽스처 값이 `DEFAULT_PRODUCTS` 와 교차해서, 필터로 새는 순간
     후보가 줄어드는가. 이게 거짓이면 동등성 테스트는 `0 == 0` 을 재는 것이다.
"""

from __future__ import annotations

from typing import get_args

from app.agents.profile.graph_models import NodeType, Predicate, is_projected
from tests._fakes import DEFAULT_PRODUCTS
from tests._graph_fixtures import (
    FULL_GRAPH_MARKDOWN,
    FULL_GRAPH_SPEC,
    GRAPH_LABELS,
    full_graph_document,
)


def test_fixture_covers_every_node_type_and_predicate() -> None:
    """어휘가 늘어나면 픽스처도 늘어나야 한다 — 안 그러면 새 축이 무방비로 열린다."""
    document = full_graph_document()
    assert {node.type for node in document.nodes} == set(get_args(NodeType))
    assert {edge.predicate for edge in document.edges} == set(get_args(Predicate))


def test_every_edge_is_projected() -> None:
    """ "꽉 찬" 은 **와이어에도 전량 실린다**는 뜻이다 — 전부 `active`·비민감이어야 한다.

    억제·민감 파생이 섞이면 투영이 조용히 줄어들어, 투영을 재는 테스트가 의도보다 작은 표본을
    본다(`is_projected` 의 두 조건 — #360).
    """
    assert all(is_projected(edge) for edge in full_graph_document().edges)


def test_every_edge_label_appears_in_the_markdown() -> None:
    """그래프와 요약이 같은 취향을 말해야 "그래프가 꽉 찬 상태"라는 말이 사실이 된다.

    추천 경로가 실제로 읽는 것은 요약이므로, 라벨이 요약에서 빠지면 그 취향은 **동등성 테스트에
    존재하지 않는 것과 같다** — 그래프에만 있고 아무 경로에도 닿지 않는다.
    """
    missing = [label for label in GRAPH_LABELS if label not in FULL_GRAPH_MARKDOWN]
    assert missing == [], f"요약에 빠진 라벨: {missing}"


def test_labels_are_not_substrings_of_the_fake_decompose_output() -> None:
    """라벨이 유출 카나리로 쓰이려면 **정상 필터 값에 우연히 들어 있으면 안 된다**.

    `DEFAULT_DECOMPOSE` 의 `keyword` 는 `"무선 이어폰"` 이라 `"블루투스 이어폰"` 과 겹치지
    않지만, 누군가 fake 를 손보면 카나리가 항상 참이 되어 유출 검사가 죽는다.
    """
    from tests._fakes import DEFAULT_DECOMPOSE

    normal = str(DEFAULT_DECOMPOSE)
    assert [label for label in GRAPH_LABELS if label in normal] == []


def test_fixture_would_shrink_the_fake_catalogue_if_leaked() -> None:
    """**유효성 대조군** — 각 축이 필터로 새면 실제로 후보가 준다.

    선호 가격대를 3~5만원으로 두면 그 값이 새어도 fake 카탈로그 3건이 그대로 남아 유출을 감지할
    수 없다. 아래 단언들이 그 무기력을 막는다. 픽스처 값을 바꿀 때 여기가 같이 깨져야 정상이다.
    """
    labels = dict(
        ((node_type, predicate), label) for node_type, label, predicate in FULL_GRAPH_SPEC
    )

    price_low, price_high = labels[("priceBand", "prefers")].split("-")
    assert all(product.price < int(price_low) for product in DEFAULT_PRODUCTS), (
        "선호 가격대가 카탈로그 위에 있어야 유출 시 후보가 준다"
    )
    assert int(price_high) > int(price_low)

    rating_min = float(labels[("ratingBand", "prefers")].split("-")[0])
    assert any(product.rating < rating_min for product in DEFAULT_PRODUCTS), (
        "선호 평점대가 카탈로그를 갈라야 유출 시 후보가 준다"
    )

    assert labels[("brand", "avoids")] in {product.brand for product in DEFAULT_PRODUCTS}, (
        "회피 브랜드는 카탈로그에 실재해야 배제 승격을 감지할 수 있다"
    )
    assert labels[("brand", "likes")] not in {product.brand for product in DEFAULT_PRODUCTS}

    assert labels[("category", "likes")] not in {
        product.category for product in DEFAULT_PRODUCTS
    }, "선호 카테고리가 카탈로그와 달라야 categoryName 유출이 후보를 0 으로 만든다"

    assert int(labels[("product", "purchased")]) in {
        product.product_id for product in DEFAULT_PRODUCTS
    }, "구매 상품은 카탈로그에 실재해야 exclude_product_ids 유출을 감지할 수 있다"
