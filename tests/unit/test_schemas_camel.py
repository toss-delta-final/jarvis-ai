"""SSE·Spring 스키마 camelCase 직렬화 계약 테스트 (api-spec v0.4.0 §2.2/§3.1/§4).

와이어 포맷이 camelCase 인지(별칭 배선)와 입력 시 snake/camel 양쪽 허용을 고정한다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest, DoneData, ErrorData, ProductsReadyData
from app.schemas.spring import (
    ProductCreate,
    ProductSearchFilters,
    RecoReason,
    RecommendationListEntry,
    RecommendationPush,
    SellerProductList,
    SellerProductRow,
)


def test_products_ready_serializes_camel() -> None:
    """products.ready 는 목록이 하나여도 listIds 배열로 직렬화된다."""
    d = ProductsReadyData(session_id="s-1", list_ids=["l-1"]).model_dump(by_alias=True)
    assert d == {"sessionId": "s-1", "listIds": ["l-1"]}


def test_products_ready_preserves_list_order_and_caps_at_ten() -> None:
    """listIds 순서는 I-21 목록 순서를 보존하고 계약 상한 10개를 넘지 못한다."""
    list_ids = [f"list-{index}" for index in range(10)]

    data = ProductsReadyData(session_id="s-1", list_ids=list_ids).model_dump(by_alias=True)

    assert data["listIds"] == list_ids
    with pytest.raises(ValidationError):
        ProductsReadyData(session_id="s-1", list_ids=[*list_ids, "list-10"])


def test_error_serializes_request_id_and_retryable() -> None:
    """in-stream error는 로그 상관키와 재시도 가능 여부를 camelCase로 노출한다."""
    data = ErrorData(
        code="LLM_TIMEOUT",
        message="응답이 지연됐어요.",
        request_id="req-123",
        retryable=True,
    ).model_dump(by_alias=True)

    assert data == {
        "code": "LLM_TIMEOUT",
        "message": "응답이 지연됐어요.",
        "requestId": "req-123",
        "retryable": True,
    }


def test_done_serializes_camel() -> None:
    """done 은 finishReason 으로 직렬화된다."""
    assert DoneData(finish_reason="stop").model_dump(by_alias=True) == {"finishReason": "stop"}


def test_chat_request_accepts_camel_input() -> None:
    """요청 본문(camelCase)이 snake 속성으로 파싱된다 (populate_by_name)."""
    req = ChatRequest.model_validate({"sessionId": "s", "threadId": "t", "message": "m"})
    assert req.session_id == "s"
    assert req.thread_id == "t"


def test_search_filters_serialize_camel() -> None:
    """검색 필터는 excludeProductIds/priceMax 등 camelCase 로 나간다 (§4.2 와이어)."""
    f = ProductSearchFilters(price_max=50000, exclude_product_ids=[1], limit=30)
    d = f.model_dump(by_alias=True)
    assert d["priceMax"] == 50000
    assert d["excludeProductIds"] == [1]
    assert d["limit"] == 30


def _entry(**overrides) -> RecommendationListEntry:
    """§4.2 유효한 목록 1건 — 경계 테스트가 한 필드만 바꿔 쓰도록."""
    return RecommendationListEntry(**{"list_id": "l-1", "product_ids": [101], **overrides})


def test_recommendation_push_i21_serializes_camel() -> None:
    """I-21 추천 push 는 lists[] 최상위로 직렬화된다 (§4.2 v0.17.1 다중 목록).

    목록이 1개여도 길이 1 배열이며, 구 평평 3필드(listId·productIds·reasons)는 폐기다.
    reasons 미지정 시 빈 배열(선택 필드, 이슈 #61).
    """
    push = RecommendationPush(
        session_id="s-1",
        recommendation_request_id="a63be350-ec96-4f44-b3f9-c962b6673a68",
        list_type="PICK_ONE",
        lists=[_entry(product_ids=[101, 205, 552])],
    )
    d = push.model_dump(by_alias=True)
    assert d == {
        "sessionId": "s-1",
        "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
        "listType": "PICK_ONE",
        "totalBudget": None,
        "lists": [
            {"listId": "l-1", "label": None, "productIds": [101, 205, 552], "reasons": []},
        ],
    }
    # 구 평평 형식은 최상위에 남아 있지 않다 — BE 과도기 수용 코드 제거 조건(#209).
    assert "listId" not in d
    assert "productIds" not in d
    assert "reasons" not in d
    # 표시 필드·groups 구조 부재 확인 (경로 B — id 만 전달).
    assert "groups" not in d
    assert "price" not in d


def test_recommendation_push_buy_all_carries_label_and_total_budget() -> None:
    """BUY_ALL 세트 여러 안은 목록별 label + 총액 예산을 싣는다 (§4.2 v0.17.1)."""
    push = RecommendationPush(
        session_id="s-1",
        recommendation_request_id="a63be350-ec96-4f44-b3f9-c962b6673a68",
        list_type="BUY_ALL",
        total_budget=50000,
        lists=[
            _entry(list_id="9f2c1a7e", label="알뜰", product_ids=[101, 205, 552]),
            _entry(list_id="4b8d43f5", label="균형", product_ids=[101, 88]),
        ],
    )
    d = push.model_dump(by_alias=True)
    assert d["listType"] == "BUY_ALL"
    assert d["totalBudget"] == 50000
    assert [item["label"] for item in d["lists"]] == ["알뜰", "균형"]


def test_recommendation_push_rejects_unknown_list_type() -> None:
    """listType 은 PICK_ONE/BUY_ALL 두 값뿐이다 (§4.2)."""
    with pytest.raises(ValidationError):
        RecommendationPush(
            session_id="s-1",
            recommendation_request_id="a63be350",
            list_type="SET",
            lists=[_entry()],
        )


@pytest.mark.parametrize(
    "lists",
    [
        pytest.param([], id="빈 lists"),
        pytest.param(
            [_entry(list_id=f"l-{i}") for i in range(11)],
            id="10개 초과",
        ),
        pytest.param([_entry(), _entry()], id="한 콜백 안 listId 중복"),
    ],
)
def test_recommendation_push_lists_bounds(lists) -> None:
    """lists 는 1~10개이며 한 콜백 안에 같은 listId 가 두 번 오면 400 이다 (§4.2).

    listId 중복을 허용하면 멱등 키 (recommendationRequestId, listId) 가 겹쳐
    뒤 목록이 재전송으로 오해돼 조용히 버려진다.
    """
    with pytest.raises(ValidationError):
        RecommendationPush(
            session_id="s-1",
            recommendation_request_id="a63be350",
            list_type="PICK_ONE",
            lists=lists,
        )


def test_recommendation_push_request_id_capped_at_36() -> None:
    """recommendationRequestId 는 BE CHAR(36) 이라 36자를 넘으면 거절한다 (§4.2)."""
    with pytest.raises(ValidationError):
        RecommendationPush(
            session_id="s-1",
            recommendation_request_id="x" * 37,
            list_type="PICK_ONE",
            lists=[_entry()],
        )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"list_id": "list 1"}, id="listId 허용 문자 위반"),
        pytest.param({"list_id": "a" * 65}, id="listId 64자 초과"),
        pytest.param({"label": "가" * 51}, id="label 50자 초과"),
        pytest.param({"product_ids": []}, id="빈 productIds"),
        pytest.param({"product_ids": list(range(101, 111))}, id="productIds 9개 초과"),
        pytest.param({"product_ids": [101, 101]}, id="한 목록 안 productId 중복"),
    ],
)
def test_recommendation_list_entry_bounds(overrides) -> None:
    """목록 1건의 §4.2 400 조건 — listId 형식·길이, label 50자, productIds 1~9·중복금지."""
    with pytest.raises(ValidationError):
        _entry(**overrides)


def test_recommendation_push_reasons_serializes_camel() -> None:
    """I-21 reasons 는 {productId, reason} camelCase 항목으로 직렬화된다 (§4.2 v0.15.2, 이슈 #61)."""
    push = RecommendationPush(
        session_id="s-1",
        recommendation_request_id="a63be350",
        list_type="PICK_ONE",
        lists=[
            _entry(
                product_ids=[101, 205],
                reasons=[
                    RecoReason(product_id=101, reason="방수 등급이 높아 우천 시에도 안전합니다."),
                    RecoReason(product_id=205, reason="가벼워 휴대가 편합니다."),
                ],
            )
        ],
    )
    d = push.model_dump(by_alias=True)
    assert d["lists"][0]["reasons"] == [
        {"productId": 101, "reason": "방수 등급이 높아 우천 시에도 안전합니다."},
        {"productId": 205, "reason": "가벼워 휴대가 편합니다."},
    ]
    # 순서 권위는 productIds — reasons 는 부분집합/순서무관 허용(계약 §4.2).
    assert d["lists"][0]["productIds"] == [101, 205]


def test_seller_product_row_serializes_camel() -> None:
    """SellerProductRow(I-9)는 originalPrice/stockQuantity/displayedSalesCount 로 직렬화된다."""
    row = SellerProductRow(
        product_id=101,
        name="여행용 파우치",
        price=10000,
        original_price=12000,
        stock_quantity=5,
        status="ON_SALE",
        displayed_sales_count=42,
    )
    d = row.model_dump(by_alias=True)
    assert d["originalPrice"] == 12000
    assert d["stockQuantity"] == 5
    assert d["displayedSalesCount"] == 42


def test_seller_product_list_parses_total_field() -> None:
    """[#622] I-9 응답의 `total`(필터 적용 전체 건수) — 모델에 필드가 없어 pydantic 이
    조용히 버렸던 값이다(`SellerProductInternalListResponse{rows, total}`, BE 는 이미
    내려주고 있었다). `hitl._find_product`가 페이지 순회 종료를 정확히 판단하는 데 쓴다.
    """
    parsed = SellerProductList.model_validate(
        {
            "rows": [
                {
                    "productId": 101,
                    "name": "감귤청",
                    "price": 15000,
                    "originalPrice": None,
                    "stockQuantity": 100,
                    "status": "ON_SALE",
                }
            ],
            "total": 137,
        }
    )
    assert parsed.total == 137
    assert len(parsed.rows) == 1


def test_seller_product_list_total_defaults_to_zero_when_absent() -> None:
    """`total` 이 없는 응답(구 스텁 등)도 캐스팅 실패 없이 기본값 0 으로 채워진다."""
    parsed = SellerProductList.model_validate({"rows": []})
    assert parsed.total == 0


def test_product_create_by_alias() -> None:
    """ProductCreate(I-10) 요청 바디는 camelCase 로 직렬화된다.

    [2026-08-09] `category`(자유 문자열) → `categoryId`(Long). BE 는 categoryId 만
    받으므로 구 키를 보내면 조용히 버려진 뒤 필수 필드 누락으로 등록이 거부된다 —
    이 단언이 그 회귀를 잡는 자리다.
    """
    payload = ProductCreate(
        name="여행용 파우치", price=10000, stock_quantity=5, category_id=1499526220614373
    )
    d = payload.model_dump(by_alias=True)
    assert d == {
        "name": "여행용 파우치",
        "price": 10000,
        "originalPrice": None,
        "stockQuantity": 5,
        "stocks": None,  # [#524] 듀얼모드 — quantity 모드에서는 None(전송 시 exclude_none 으로 탈락)
        "categoryId": 1499526220614373,
        "description": None,
        "imageUrl": None,
    }


def test_stock_entry_by_alias() -> None:
    """[#524] stocks[] 한 줄 — optionId camelCase, null 은 exclude_none 시 키 누락."""
    from app.schemas.spring import StockEntry

    assert StockEntry(option_id=10, quantity=3).model_dump(by_alias=True) == {
        "optionId": 10,
        "quantity": 3,
    }
    assert StockEntry(option_id=None, quantity=3).model_dump(
        by_alias=True, exclude_none=True
    ) == {"quantity": 3}


def test_recommendation_push_rejects_negative_total_budget() -> None:
    """totalBudget 은 예산 상한이라 음수가 될 수 없다 (PR #212 리뷰).

    지금은 그래프가 채우지 않지만(#60·#163 미착수), 채우는 쪽은 "5만원 내로" 를 LLM 이 뽑아낸
    값이다 — 이 파일의 다른 필드처럼 신뢰경계에서 막아 둔다. 상한은 두지 않는다: 과도하게 큰
    상한은 "제한 없음"과 같아 무해하고, 실제 가격은 BIGINT 라 자연 상한이 없다.
    """
    with pytest.raises(ValidationError):
        RecommendationPush(
            session_id="s-1",
            recommendation_request_id="a63be350",
            list_type="BUY_ALL",
            total_budget=-1,
            lists=[_entry()],
        )
