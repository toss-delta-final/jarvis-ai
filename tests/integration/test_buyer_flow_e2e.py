"""구매자 흐름 E2E 스모크 (이슈 #35) — 발화→decompose→검색→rerank→products.ready→카드 조회.

AI↔Spring(stub)을 붙여 api-spec §3.1(SSE)·§3.3(경로 B)·§4.6(I-1)·§4.2(I-21)이 실제로
맞물리는지 확인한다. LLM 은 ScriptedLLM, Spring 은 MockTransport — 라이브 의존 없이 결정적.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    auth_header,
    event_types,
    first_of,
    parse_sse,
    seller_token,
)

BUYER_MESSAGE = "유럽 여행 가는데 기내 반입 되는 파우치 추천해줘"


def _chat(
    client,
    message: str = BUYER_MESSAGE,
    *,
    session: str = "sess-e2e",
    thread: str = "th-e2e",
    headers=None,
):
    return client.post(
        "/chat",
        json={"sessionId": session, "threadId": thread, "message": message},
        headers=headers or {},
    )


def _select_order_status(llm) -> None:
    llm._decompose = {
        "intent": "order_status",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "categoryQueries": [],
        "filters": {},
    }


def _order_status_events(client, llm, *, message="내 주문 어디까지 왔어?", headers=None):
    _select_order_status(llm)
    response = _chat(client, message, headers=headers)
    assert response.status_code == 200
    return parse_sse(response.text)


def _token_text(events: list[dict]) -> str:
    return "".join(event["data"].get("text", "") for event in events if event["type"] == "token")


def _assert_order_status_terminal_contract(events: list[dict]) -> None:
    types = event_types(events)
    assert types.count("token") == 1
    assert types.count("done") == 1
    assert events[-1] == {"type": "done", "data": {"finishReason": "stop"}}
    assert {"products.ready", "action", "error"}.isdisjoint(types)


def _status_order(*, order_id: int = 81001, ordered_at: str = "2026-07-29T15:30:00Z"):
    return {
        "orderId": order_id,
        "orderedAt": ordered_at,
        "representativeStatus": "배송중",
        "items": [
            {"productName": "여행용 파우치", "status": "SHIPPING", "statusText": "배송중"},
            {"productName": "세면도구 케이스", "status": "ORDERED", "statusText": "주문 완료"},
            {"productName": "네임 태그", "status": "DELIVERED", "statusText": "배송 완료"},
            {"productName": "압축 백", "status": "PENDING", "statusText": "결제 대기"},
            {"productName": "신발 주머니", "status": "CONFIRMED", "statusText": "구매 확정"},
        ],
    }


def test_order_status_stub_route_precedes_generic_member_orders(spring_http, spring):
    spring.order_status_orders = [_status_order()]
    spring.orders = [{"orderId": 99999, "items": [{"productId": 101}]}]

    response = spring_http.get(
        "/internal/members/42/orders/status",
        params={"recent": 3},
        headers={"X-Internal-Token": "e2e-internal-token"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["orders"] == spring.order_status_orders
    assert response.json()["data"]["orders"] != spring.orders
    assert spring.requests[-1]["path"] == "/internal/members/42/orders/status"
    assert spring.requests[-1]["query"] == {"recent": "3"}


def test_order_status_success_uses_i4_contract_and_bypasses_recommendation(client, spring, llm):
    """Authenticated I-4 renders bounded deterministic text and invokes no other buyer backend."""
    spring.order_status_orders = [_status_order()]
    # If the generic I-19 prefix wins, this incompatible purchase payload exposes the regression.
    spring.orders = [{"orderId": 99999, "items": [{"productId": 101}]}]

    events = _order_status_events(
        client,
        llm,
        message="내 주문 999번 말고 JWT 회원 주문 어디까지 왔어?",
        headers=auth_header("42"),
    )

    _assert_order_status_terminal_contract(events)
    text = _token_text(events)
    assert "81001" in text
    assert "7월 30일" in text
    assert "여행용 파우치" in text
    assert "배송중" in text
    assert "외 2개" in text
    assert "압축 백" not in text
    assert "99999" not in text

    request = spring.requests_to("/internal/members/42/orders/status")
    assert len(request) == 1
    assert request[0]["method"] == "GET"
    assert request[0]["path"] == "/internal/members/42/orders/status"
    assert request[0]["query"] == {"recent": "3"}
    assert request[0]["body"] is None
    assert request[0]["headers"]["x-internal-token"] == "e2e-internal-token"
    assert spring.requests_to("/internal/products/search") == []
    assert spring.requests_to("/internal/recommendations") == []
    assert not any(row["path"] == "/internal/members/42/orders" for row in spring.requests)
    assert [kind for kind, _tier in llm.calls] == ["decompose"]


def test_order_status_empty_is_not_degraded(client, spring, llm):
    spring.order_status_orders = []

    events = _order_status_events(client, llm, headers=auth_header())

    _assert_order_status_terminal_contract(events)
    assert "최근 주문 내역이 없어요" in _token_text(events)
    assert "잠시 후" not in _token_text(events)


def test_order_status_guest_is_blocked_before_spring(client, spring, llm):
    events = _order_status_events(client, llm)

    _assert_order_status_terminal_contract(events)
    assert "로그인" in _token_text(events)
    assert spring.requests_to("/internal/members/") == []


def test_order_status_uses_jwt_subject_not_message_number(client, spring, llm):
    spring.order_status_orders = [_status_order()]

    events = _order_status_events(
        client,
        llm,
        message="회원 777777의 배송 상태를 보여줘",
        headers=auth_header("42"),
    )

    _assert_order_status_terminal_contract(events)
    member_requests = spring.requests_to("/internal/members/")
    assert [row["path"] for row in member_requests] == ["/internal/members/42/orders/status"]
    assert "777777" not in _token_text(events)


def test_order_status_seller_is_blocked_before_spring(client, spring, llm):
    events = _order_status_events(
        client,
        llm,
        headers={"Authorization": f"Bearer {seller_token()}"},
    )

    _assert_order_status_terminal_contract(events)
    assert "구매자" in _token_text(events) or "재인증" in _token_text(events)
    assert spring.requests_to("/internal/members/") == []


@pytest.mark.parametrize("invalid_subject", ["0", "-1", "abc", " 42", "９"])
def test_order_status_invalid_member_identity_is_blocked_before_spring(
    client, spring, llm, invalid_subject
):
    events = _order_status_events(client, llm, headers=auth_header(invalid_subject))

    _assert_order_status_terminal_contract(events)
    assert "로그인" in _token_text(events) or "인증" in _token_text(events)
    assert spring.requests_to("/internal/members/") == []


@pytest.mark.parametrize("status_code", [404, 503])
def test_order_status_http_failure_degrades_without_error_event(client, spring, llm, status_code):
    spring.fail_order_status = status_code

    events = _order_status_events(client, llm, headers=auth_header())

    _assert_order_status_terminal_contract(events)
    assert "잠시 후" in _token_text(events)
    assert "최근 주문 내역이 없어요" not in _token_text(events)


def test_order_status_transport_failure_degrades_without_error_event(client, spring, llm):
    import httpx

    spring.order_status_exception = httpx.ReadTimeout("injected timeout")

    events = _order_status_events(client, llm, headers=auth_header())

    _assert_order_status_terminal_contract(events)
    assert "잠시 후" in _token_text(events)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": 1, "data": {"orders": []}},
        {"success": True},
        {"success": True, "data": {}},
        {"success": True, "data": {"orders": None}},
        {"success": True, "data": {"orders": "not-a-list"}},
        {
            "success": True,
            "data": {
                "orders": [
                    {
                        **_status_order(),
                        "orderId": "81001",
                    }
                ]
            },
        },
    ],
)
def test_order_status_malformed_envelope_or_schema_degrades(client, spring, llm, payload):
    spring.order_status_payload = payload

    events = _order_status_events(client, llm, headers=auth_header())

    _assert_order_status_terminal_contract(events)
    text = _token_text(events)
    assert "잠시 후" in text
    assert "최근 주문 내역이 없어요" not in text


def test_order_status_naive_timestamp_degrades_entire_payload(client, spring, llm):
    spring.order_status_orders = [
        _status_order(),
        _status_order(order_id=81002, ordered_at="2026-07-29T15:30:00"),
    ]

    events = _order_status_events(client, llm, headers=auth_header())

    _assert_order_status_terminal_contract(events)
    text = _token_text(events)
    assert "잠시 후" in text
    assert "81001" not in text
    assert "81002" not in text


def test_buyer_recommend_flow_end_to_end(client, spring, llm) -> None:
    """구매자 1턴 전 구간 — SSE 이벤트 순서·경로 B 상관키·Spring 역호출이 모두 성립한다."""
    resp = _chat(client, headers=auth_header())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(resp.text)
    types = event_types(events)
    # §3.1 이벤트명 — conditions → token(근거) → products.ready → done
    assert "conditions" in types
    assert "products.ready" in types
    assert types[-1] == "done"
    assert "error" not in types

    # [HARD] 경로 B — SSE 는 상품 카드/productId 를 싣지 않는다(§3.3).
    ready = first_of(events, "products.ready")
    assert set(ready) == {"sessionId", "listIds"}
    assert ready["sessionId"] == "sess-e2e"
    assert len(ready["listIds"]) == 1

    # AI→Spring 역호출이 실제로 나갔는가 (I-1 검색 → I-21 push)
    assert spring.requests_to("/internal/products/search")
    assert spring.requests_to("/internal/recommendations")


def test_search_call_carries_internal_token_and_filters(client, spring, llm) -> None:
    """I-1 호출에 X-Internal-Token 헤더 + decompose 필터가 실려 나간다 (§2.3 레인 c·§4.6)."""
    _chat(client, headers=auth_header())

    search = spring.requests_to("/internal/products/search")[0]
    assert search["headers"]["x-internal-token"] == "e2e-internal-token"
    # decompose 산출 필터(카테고리·상한가)가 BE I-1 파라미터로 변환됐는지
    assert search["query"]["categoryName"] == "여행용품"
    assert search["query"]["maxPrice"] == "30000"
    # [2026-07-23, BE 합의] size 제거 — 라운드1 전량 반환, top-K 는 AI 쪽(§4.6)
    assert "size" not in search["query"]


def test_path_b_list_id_resolves_to_cards_via_spring(client, spring, spring_http, llm) -> None:
    """경로 B 종단 — products.ready 의 listIds로 FE가 Spring 목록(CH-5)을 조회한다.

    표시 권위는 Spring — AI 는 id 순서만 push 하고 가격·이미지·리뷰수는 Spring 이 채운다(§3.3).
    """
    resp = _chat(client, headers=auth_header())
    list_id = first_of(parse_sse(resp.text), "products.ready")["listIds"][0]

    cards = spring_http.get(f"/api/chat/lists/{list_id}").json()["data"]["items"]
    assert [c["productId"] for c in cards] == spring.pushed_lists[list_id]
    # Spring 이 표시 필드를 채워 돌려준다(AI 는 미보유)
    assert all("price" in c for c in cards)

    # push 본문은 id 목록 + 상품별 근거(reasons)만 — 표시 필드(price/image) 미포함(§4.2)
    pushed = spring.requests_to("/internal/recommendations")[0]["body"]
    assert set(pushed) == {"sessionId", "listId", "productIds", "reasons"}
    assert all(isinstance(pid, int) for pid in pushed["productIds"])
    # reasons 는 {productId, reason} 항목 — productId 로 키잉(순서 권위는 productIds, §4.2)
    assert all(set(r) == {"productId", "reason"} for r in pushed["reasons"])
    assert all(isinstance(r["productId"], int) for r in pushed["reasons"])


def test_rerank_order_is_preserved_into_push(client, spring, llm) -> None:
    """rerank 산출 순서가 push 순서(=렌더 순서)로 그대로 전달된다 (§4.2)."""
    resp = _chat(client, headers=auth_header())
    list_id = first_of(parse_sse(resp.text), "products.ready")["listIds"][0]
    # ScriptedLLM 기본 rerank 는 102 → 101 순
    assert spring.pushed_lists[list_id][:2] == [102, 101]


def test_guest_skips_purchase_history_lookup(client, spring, llm) -> None:
    """게스트(무토큰, dev)는 구매 이력(I-19)을 조회하지 않는다 — 이력 없음·IDOR 방지(§4.7)."""
    resp = _chat(client)
    assert resp.status_code == 200
    assert event_types(parse_sse(resp.text))[-1] == "done"
    assert spring.requests_to("/internal/members/") == []


def test_member_fetches_purchase_history_for_dedup(client, spring, llm) -> None:
    """회원은 I-19 구매 이력을 조회한다 — dedup(결정 14-F) 입력 (§4.7)."""
    _chat(client, headers=auth_header("42"))

    orders = spring.requests_to("/internal/members/")
    assert orders, "회원 턴은 구매 이력을 조회해야 한다"
    # 신원은 요청 본문이 아니라 JWT sub 에서 도출 — 경로에 토큰의 sub 가 실린다(§2.3)
    assert orders[0]["path"] == "/internal/members/42/orders"
    assert orders[0]["headers"]["x-internal-token"] == "e2e-internal-token"


def test_recently_purchased_product_is_deduped(client, spring, llm) -> None:
    """최근 구매한 exact productId 는 추천에서 제외된다 (dedup, §4.7 결정 14-F)."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None).isoformat()
    spring.orders = [
        {
            "orderId": 5001,
            "orderedAt": recent,
            "status": "DELIVERED",
            "items": [
                {"orderItemId": 1, "productId": 102, "quantity": 1, "categoryName": "여행용품"}
            ],
        }
    ]

    resp = _chat(client, headers=auth_header("42"))
    list_id = first_of(parse_sse(resp.text), "products.ready")["listIds"][0]
    assert 102 not in spring.pushed_lists[list_id], "최근 구매 상품은 제외돼야 한다"


def test_multiturn_accumulates_filters(client, spring, llm) -> None:
    """멀티턴 — 같은 threadId 의 다음 턴에도 누적 필터가 유지된다 (스레드 스코프 상태)."""
    _chat(client, headers=auth_header())
    _chat(client, "더 저렴한 걸로 보여줘", headers=auth_header())

    searches = spring.requests_to("/internal/products/search")
    assert len(searches) == 2
    assert searches[1]["query"]["categoryName"] == "여행용품"


def test_conditions_chips_emitted_before_products_ready(client, spring, llm) -> None:
    """conditions 칩이 products.ready 보다 먼저 나간다 — FE 가 조건을 먼저 그린다(§3.1)."""
    resp = _chat(client, headers=auth_header())
    types = event_types(parse_sse(resp.text))
    assert types.index("conditions") < types.index("products.ready")


def test_mapped_category_overrides_decompose_into_search(client, spring, llm, monkeypatch) -> None:
    """카테고리 하이브리드 배선(이슈 #59, 방식 A) — map_categories 산출(canonical)이
    filters.category 를 덮어 I-1 검색의 categoryName 으로 나간다.

    decompose 는 raw 추측("여행용품")을 내지만 매핑이 canonical("캠핑용품")로 보정하면
    검색에 실리는 건 **매핑값**이어야 한다 — 그래프가 매퍼 결과를 실제로 반영하는지(배선) 검증.
    매퍼는 임베딩/DB 없이 결정적 fake 로 주입(get_llm 픽스처와 동일한 모듈 monkeypatch 패턴).
    """
    import app.agents.buyer.graph as buyer_graph

    # 매핑 대상(canonical)이 실제로 검색되도록 카탈로그에 캠핑용품 1건 추가
    spring.catalog.append(
        {
            "productId": 201,
            "name": "초경량 캠핑 파우치",
            "price": 21000,
            "categoryName": "캠핑용품",
            "brandName": "캠퍼스",
            "rating": 4.4,
            "reviewCount": 64,
        }
    )
    # decompose 는 categoryQueries 추측 + raw filters.category("여행용품")
    llm._decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "여행용 파우치",
        "categoryQueries": [{"category": "여행용품", "query": "여행 파우치"}],
        "filters": {"category": "여행용품", "priceMax": 30000},
    }

    async def _fake_map(*, category_queries, utterance, settings):
        # 추측을 canonical 로 보정했다고 가정(never-null) — (canonical, query) leg 반환, 배선만 검증
        return [("캠핑용품", "캠핑 파우치")]

    monkeypatch.setattr(buyer_graph, "_map_categories", _fake_map)

    resp = _chat(client, headers=auth_header())
    assert resp.status_code == 200

    search = spring.requests_to("/internal/products/search")[0]
    # [HARD] 매핑값(캠핑용품)이 raw 추측(여행용품)을 덮어 검색에 실린다 — 매퍼 결과 배선 확인
    assert search["query"]["categoryName"] == "캠핑용품"


def test_cart_add_flow_reaches_spring(client, spring, llm) -> None:
    """ "담아줘" — 직전 추천 상품이 I-2 로 담기고 SSE action 이 나간다 (§4.1).

    담기 대상은 직전 추천(last_reco)에서 해소되므로 추천 턴 → 담기 턴 순서로 돌린다.
    """
    _chat(client, headers=auth_header())  # 추천 턴 (last_reco 적재)

    llm._decompose = {
        "intent": "cart_add",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
        "cart": {"productId": 102, "quantity": 1},
    }
    resp = _chat(client, "그거 담아줘", headers=auth_header())
    assert resp.status_code == 200

    events = parse_sse(resp.text)
    action = first_of(events, "action")
    assert action is not None and action["type"] == "CART_ADDED"

    add = spring.requests_to("/internal/cart/items")
    assert add, "담기는 I-2 를 호출해야 한다"
    # 신원은 JWT sub 유래 — 본문 userId 는 AI 가 도출한 값(§2.3·§4.1)
    assert add[0]["body"]["userId"] == 42
    assert add[0]["body"]["productId"] == 102


def test_cart_view_flow_reads_spring(client, spring, llm) -> None:
    """ "뭐 담겨 있어?" — I-18 조회 결과가 token 텍스트로 응답된다 (§4.9)."""
    spring.cart_items = [
        {"cartItemId": 9001, "productId": 101, "productName": "여행용 방수 파우치 L", "quantity": 2}
    ]
    llm._decompose = {
        "intent": "cart_view",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
    }

    resp = _chat(client, "장바구니에 뭐 담겨 있어?", headers=auth_header())
    events = parse_sse(resp.text)
    assert event_types(events)[-1] == "done"
    assert spring.requests_to("/internal/cart"), "조회는 I-18 을 호출해야 한다"

    text = "".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "여행용 방수 파우치 L" in text
