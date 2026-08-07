"""Degrade 경로 E2E 스모크 (이슈 #35) — 상류 장애가 사용자 흐름을 어떻게 끊/잇는지.

SPEC-RECOMMEND-001 §7 + api-spec §3.3 의 degrade 규약을 실 HTTP 경계로 재현한다:
  - 검색 실패        → in-stream `error` SEARCH_FAILED 후 종료
  - LLM 미구성/오류  → `error` LLM_UNAVAILABLE / LLM_TIMEOUT
  - rerank 실패      → 검색 순서 폴백(스트림은 정상 완료)
  - push 실패        → `products.ready` 미emit + `done` 종료 (error 아님)
  - 이력(I-19) 실패  → dedup 없이 추천 진행
  - 담기 옵션 필요   → 되물음(멀티턴), 상품 없음 → action 실패 사유
"""

from __future__ import annotations

import json
import logging

from tests.integration.conftest import auth_header, event_types, first_of, parse_sse

MESSAGE = "여행용 파우치 추천해줘"


def _chat(client, message: str = MESSAGE, *, thread: str = "th-deg", headers=None):
    return client.post(
        "/chat",
        json={"sessionId": "sess-deg", "threadId": thread, "message": message},
        headers=headers if headers is not None else auth_header(),
    )


def test_search_failure_emits_correlated_retryable_error(client, spring, llm, caplog) -> None:
    """I-1 검색 실패는 응답 헤더·로그와 같은 requestId의 재시도 가능 오류로 종료한다."""
    spring.fail_search = True

    with caplog.at_level(logging.INFO, logger="observability"):
        response = _chat(client)
    events = parse_sse(response.text)
    error = first_of(events, "error")
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "observability" and record.getMessage().startswith("{")
    ]

    assert error == {
        "code": "SEARCH_FAILED",
        "message": "상품 검색에 실패했어요.",
        "requestId": response.headers["X-Request-Id"],
        "retryable": True,
    }
    assert records[-1]["requestId"] == error["requestId"]
    # 후보가 없으므로 목록 push 는 하지 않는다
    assert spring.requests_to("/internal/recommendations") == []


def test_llm_unavailable_emits_error(client, spring, monkeypatch) -> None:
    """LLM 미구성(키 없음) → 네트워크 호출 없이 즉시 LLM_UNAVAILABLE (개발·CI 안전판)."""
    import app.agents.buyer.graph as buyer_graph

    monkeypatch.setattr(buyer_graph, "get_llm", lambda: None)

    response = _chat(client)
    error = first_of(parse_sse(response.text), "error")
    assert error is not None and error["code"] == "LLM_UNAVAILABLE"
    assert error["requestId"] == response.headers["X-Request-Id"]
    assert error["retryable"] is False
    assert spring.requests_to("/internal/products/search") == []


def test_decompose_timeout_maps_to_llm_timeout(client, spring, monkeypatch) -> None:
    """decompose 타임아웃 → LLM_TIMEOUT (일반 실패와 구분, §2.9 c)."""
    import app.agents.buyer.graph as buyer_graph

    from tests.integration._stubs import ScriptedLLM

    monkeypatch.setattr(
        buyer_graph, "get_llm", lambda: ScriptedLLM(decompose_error=True, timeout=True)
    )

    response = _chat(client)
    error = first_of(parse_sse(response.text), "error")
    assert error is not None and error["code"] == "LLM_TIMEOUT"
    assert error["requestId"] == response.headers["X-Request-Id"]
    assert error["retryable"] is True


def test_rerank_failure_falls_back_to_search_order(client, spring, monkeypatch) -> None:
    """rerank 실패해도 스트림은 완료된다 — 검색 순서 폴백(사용자 흐름 유지, §7)."""
    import app.agents.buyer.graph as buyer_graph

    from tests.integration._stubs import ScriptedLLM

    monkeypatch.setattr(buyer_graph, "get_llm", lambda: ScriptedLLM(rerank_error=True))

    events = parse_sse(_chat(client).text)
    types = event_types(events)
    assert types[-1] == "done"
    assert "error" not in types
    # 폴백이어도 경로 B 는 성립 — 검색 순서대로 push 된다
    ready = first_of(events, "products.ready")
    assert ready is not None
    assert spring.pushed_lists[ready["listIds"][0]], "폴백 순서로라도 목록은 push 된다"


def test_push_failure_skips_products_ready_but_completes(client, spring, llm) -> None:
    """I-21 push 실패 → products.ready 미emit, 스트림은 error 가 아니라 done 으로 종료(§3.3)."""
    spring.fail_push = True

    events = parse_sse(_chat(client).text)
    types = event_types(events)
    assert "products.ready" not in types, "push 실패 시 상관키를 내보내면 FE 가 빈 목록을 조회한다"
    assert types[-1] == "done"
    assert "error" not in types


def test_purchase_history_failure_still_recommends(client, spring, llm) -> None:
    """I-19 이력 조회 실패 → dedup 없이 추천을 계속한다 (degrade, §4.7)."""
    spring.fail_purchases = True

    events = parse_sse(_chat(client, headers=auth_header("42")).text)
    types = event_types(events)
    assert types[-1] == "done"
    assert "error" not in types
    assert first_of(events, "products.ready") is not None


def test_cart_option_required_triggers_reask(client, spring, llm) -> None:
    """담기 CART_OPTION_REQUIRED → 옵션 되물음(멀티턴) — 실패 종료가 아니다 (§4.1)."""
    _chat(client)  # 추천 턴 (last_reco 적재)

    spring.fail_cart_add_code = "CART_OPTION_REQUIRED"
    spring.cart_option_payload = [
        {"optionId": 5001, "name": "L 사이즈", "extraPrice": 0},
        {"optionId": 5002, "name": "XL 사이즈", "extraPrice": 2000},
    ]
    llm._decompose = {
        "intent": "cart_add",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
        "cart": {"productId": 102, "quantity": 1},
    }

    events = parse_sse(_chat(client, "그거 담아줘").text)
    text = "".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "사이즈" in text, "되물음 문구에 옵션이 제시돼야 한다"
    assert event_types(events)[-1] == "done"


def test_cart_single_option_autoselected_without_reask(client, spring, llm) -> None:
    """옵션 후보가 1개뿐이면 되묻지 않고 그 optionId 로 재담기해 CART_ADDED 로 끝낸다 (#114)."""
    _chat(client)  # 추천 턴 (last_reco 적재)

    spring.fail_cart_add_code = "CART_OPTION_REQUIRED"
    spring.cart_option_payload = [{"optionId": 5001, "name": "프리 사이즈", "extraPrice": 0}]
    llm._decompose = {
        "intent": "cart_add",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
        "cart": {"productId": 102, "quantity": 1},
    }

    events = parse_sse(_chat(client, "그거 담아줘").text)
    action = first_of(events, "action")
    option_ids = [r["body"].get("optionId") for r in spring.requests_to("/internal/cart/items")]

    assert "token" not in event_types(events), "되묻지 않는다"
    assert action["type"] == "CART_ADDED"
    assert "프리 사이즈 옵션으로" in action["message"]
    assert option_ids == [None, 5001]  # 유일 옵션으로 1회 재담기


def test_cart_product_not_found_reports_action_failure(client, spring, llm) -> None:
    """담기 404 PRODUCT_NOT_FOUND → action 으로 실패 사유를 알린다 (§4.1)."""
    _chat(client)

    spring.fail_cart_add_code = "PRODUCT_NOT_FOUND"
    llm._decompose = {
        "intent": "cart_add",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
        "cart": {"productId": 102, "quantity": 1},
    }

    events = parse_sse(_chat(client, "그거 담아줘").text)
    action = first_of(events, "action")
    assert action is not None and action["type"] != "CART_ADDED"
    assert event_types(events)[-1] == "done"


def test_cart_view_degrades_when_spring_unreachable(client, spring, llm, monkeypatch) -> None:
    """장바구니 조회 실패 → 스트림은 안내 후 정상 종료(조회는 안내용, §4.9)."""
    import app.services.spring_client as sc

    from app.services.spring_client import SpringUnavailableError

    async def failing_get_cart(user_id=None, guest_id=None):
        raise SpringUnavailableError("cart down")

    monkeypatch.setattr(sc, "get_cart", failing_get_cart)
    llm._decompose = {
        "intent": "cart_view",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
    }

    events = parse_sse(_chat(client, "장바구니 보여줘").text)
    assert event_types(events)[-1] == "done"


def _wishlist_view_turn(client, llm, message: str = "내가 뭐 찜했지?"):
    llm._decompose = {
        "intent": "wishlist_view",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "filters": {},
    }
    return parse_sse(_chat(client, message, thread="th-wv").text)


def test_wishlist_view_degrades_when_spring_unreachable(client, spring, llm) -> None:
    """[#386] 찜 목록 조회 실패 → `token` 안내 후 정상 종료. `action` 은 내지 않는다.

    조회는 상태를 바꾸지 않으므로 `ActionData.type` 유니온에 실패 어휘가 없다(§3.1) —
    `stream_wishlist_remove` 가 `action(WISHLIST_REMOVE_FAILED)` 로 답하는 것과 갈리는 지점이다.
    개별 처리를 빠뜨리면 상위 스트림 catch-all 이 `error(INTERNAL)` 로 내보낸다(#368).
    """
    spring.fail_wishlist = True

    events = _wishlist_view_turn(client, llm)
    assert event_types(events)[-1] == "done"
    assert first_of(events, "error") is None
    assert first_of(events, "action") is None
    token = first_of(events, "token")
    assert token is not None and "찜 목록을 불러오지 못했어요" in token["text"]


def test_wishlist_view_lists_items_over_real_http_boundary(client, spring, llm) -> None:
    """[#386] 완료조건 ① — 회원이 물으면 찜 상품명이 `token` 텍스트로 나온다(실 HTTP 경계)."""
    spring.wishlist_items = [
        {"productId": 101, "name": "무선 이어폰", "purchaseState": "AVAILABLE"},
        {"productId": 102, "name": "가죽 크로스백", "purchaseState": "SOLD_OUT"},
    ]

    events = _wishlist_view_turn(client, llm)
    assert event_types(events)[-1] == "done"
    assert first_of(events, "action") is None
    assert first_of(events, "products.ready") is None  # 경로 B — 상품 카드 없음
    token = first_of(events, "token")
    assert token is not None
    assert "무선 이어폰" in token["text"]
    assert "가죽 크로스백 (품절)" in token["text"]
    assert spring.requests_to("/internal/wishlist")


def test_wishlist_view_empty_is_guidance_not_error(client, spring, llm) -> None:
    """[#386] 완료조건 ④ — 찜 0건은 200 + items:[] 이고 정상 안내로 끝난다(오류 아님)."""
    spring.wishlist_items = []

    events = _wishlist_view_turn(client, llm)
    assert event_types(events)[-1] == "done"
    assert first_of(events, "error") is None
    token = first_of(events, "token")
    assert token is not None and token["text"] == "찜한 상품이 없어요."
