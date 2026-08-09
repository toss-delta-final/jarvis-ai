"""E2E 스모크용 Spring stub + 스크립트 LLM 재수출 (이슈 #35, #438).

AI 서버는 단독 실행이 불가하고 Spring 역호출에 의존한다(api-spec §1.2 레인 c). 이 모듈은
라이브 Spring/Anthropic 없이 **결정적으로** 전 흐름을 돌리기 위한 대역을 제공한다.

설계 원칙 — **경계에서만 대역을 넣는다**:
  - Spring 은 `httpx.MockTransport`(HTTP 경계)로 세운다. `spring_client` 함수를 patch 하지
    않으므로 URL 조립·쿼리 파라미터·`X-Internal-Token` 헤더·응답 envelope 파싱·오류 매핑이
    **실코드 그대로** 검증된다(함수 patch 는 이 계층을 통째로 건너뛰어 계약 회귀를 못 잡는다).
  - LLM 은 주입형 `ScriptedLLM`(프롬프트 시그니처로 5종 호출을 분기) — 정의는
    `app/core/llm_scripted.py` 로 이동했고(#438) 이 모듈은 재수출만 한다.

Spring stub 커버 범위 (api-spec §4):
  I-1  GET  /internal/products/search      (§4.6 후보 검색)
  I-4  GET  /internal/members/{id}/orders/status (§4.10 주문 상태)
  I-2  POST /internal/cart/items           (§4.1 담기)
  I-18 GET  /internal/cart                 (§4.9 조회)
  I-19 GET  /internal/members/{id}/orders  (§4.7 구매 이력)
  I-21 POST /internal/recommendations      (§4.2 목록 push, 경로 B)
  I-17 GET  /internal/products/changes     (§4.8 변경분 pull)
  CH-5 GET  /api/chat/lists/{listId}       (§4.3 FE→Spring 목록 조회 — 경로 B 종단 확인용)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

# ScriptedLLM/DEFAULT_*/마커 상수는 app/core/llm_scripted.py 로 이동했다(#438) — app/ 런타임이
# LLM_PROVIDER=scripted 로 같은 정의를 써야 하는데 Dockerfile 은 tests/ 를 이미지에 넣지 않아
# app/ 이 tests.* 를 import 하면 컨테이너에서 깨진다. 여기는 재수출만 해 기존 10개 import 지점을
# 한 줄도 고치지 않는다.
from app.core.llm_scripted import (  # noqa: F401
    DEFAULT_DECOMPOSE,
    DEFAULT_DELTA,
    DEFAULT_ENRICH,
    DEFAULT_PROFILE_MD,
    DEFAULT_RERANK,
    ScriptedLLM,
)

# ── 기본 카탈로그 (숫자 BIGINT id — §2.6) ──
DEFAULT_CATALOG: list[dict] = [
    {
        "productId": 101,
        "name": "여행용 방수 파우치 L",
        "price": 19000,
        "originalPrice": 25000,
        "categoryName": "여행용품",
        "brandName": "트래블러",
        "rating": 4.6,
        "reviewCount": 320,
    },
    {
        "productId": 102,
        "name": "기내반입 세면도구 파우치",
        "price": 24000,
        "categoryName": "여행용품",
        "brandName": "패커스",
        "rating": 4.3,
        "reviewCount": 158,
    },
    {
        "productId": 103,
        "name": "대용량 캐리어 파우치 3종",
        "price": 31000,
        "categoryName": "여행용품",
        "brandName": "트래블러",
        "rating": 4.1,
        "reviewCount": 87,
    },
]


@dataclass
class SpringStub:
    """Spring 백엔드 대역. 상태(장바구니·push 목록·커서)를 들고 요청을 기록한다.

    실패 주입 플래그로 degrade 경로(§7)를 재현한다 — 검색 5xx·push 5xx·이력 5xx 등.
    """

    catalog: list[dict] = field(default_factory=lambda: [dict(p) for p in DEFAULT_CATALOG])
    order_status_orders: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    changes_pages: list[dict] = field(default_factory=list)
    cart_items: list[dict] = field(default_factory=list)
    # I-28 찜 목록(§4.16, 이슈 #386). `name` 은 I-18 의 `productName` 과 필드명이 다르다.
    wishlist_items: list[dict] = field(default_factory=list)
    # listId → productIds (경로 B: push 로 저장 → CH-5 로 조회)
    pushed_lists: dict[str, list[int]] = field(default_factory=dict)
    # 요청 감사 로그 — (method, path, query, headers, body)
    requests: list[dict] = field(default_factory=list)

    # 실패 주입 (degrade 검증용)
    fail_search: bool = False
    fail_order_status: int | None = None
    order_status_exception: Exception | None = None
    order_status_payload: Any | None = None
    fail_purchases: bool = False
    fail_push: bool = False
    fail_cart_add_code: str | None = None  # CART_OPTION_REQUIRED 등
    fail_wishlist: bool = False  # I-28 조회 5xx (#386 degrade 검증)
    cart_option_payload: list[dict] = field(default_factory=list)

    # ── 라우팅 ──

    def handler(self, request: httpx.Request) -> httpx.Response:
        """MockTransport 핸들러 — 메서드+경로로 분기한다."""
        path = request.url.path
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode("utf-8", "replace")
        self.requests.append(
            {
                "method": request.method,
                "path": path,
                "query": dict(request.url.params),
                "headers": dict(request.headers),
                "body": body,
            }
        )

        if request.method == "GET" and path == "/internal/products/search":
            return self._search(request)
        if request.method == "POST" and path == "/internal/cart/items":
            return self._cart_add(body)
        if request.method == "GET" and path == "/internal/cart":
            return self._cart_view()
        if request.method == "GET" and path == "/internal/wishlist":
            return self._wishlist_view()
        # I-4 must precede the generic I-19 member route: both share the same prefix.
        if (
            request.method == "GET"
            and path.startswith("/internal/members/")
            and path.endswith("/orders/status")
        ):
            return self._order_status()
        if request.method == "GET" and path.startswith("/internal/members/"):
            return self._orders()
        if request.method == "POST" and path == "/internal/recommendations":
            return self._push(body)
        if request.method == "GET" and path == "/internal/products/changes":
            return self._changes(request)
        if request.method == "GET" and path.startswith("/api/chat/lists/"):
            return self._list_cards(path.rsplit("/", 1)[-1])
        return httpx.Response(404, json={"success": False, "error": {"code": "NOT_FOUND"}})

    # ── I-1 검색 (§4.6) ──

    def _search(self, request: httpx.Request) -> httpx.Response:
        if self.fail_search:
            return httpx.Response(503, json={"success": False, "error": {"code": "UNAVAILABLE"}})
        params = request.url.params
        items = list(self.catalog)
        # BE I-1 파라미터 의미론 재현 — AI 가 보낸 쿼리가 실제로 반영되는지까지 확인한다.
        if (category := params.get("categoryName")) is not None:
            items = [p for p in items if p.get("categoryName") == category]
        if (brand := params.get("brandName")) is not None:
            items = [p for p in items if p.get("brandName") == brand]
        if (min_price := params.get("minPrice")) is not None:
            items = [p for p in items if p["price"] >= int(min_price)]
        if (max_price := params.get("maxPrice")) is not None:
            items = [p for p in items if p["price"] <= int(max_price)]
        if (size := params.get("size")) is not None:
            items = items[: int(size)]
        return httpx.Response(200, json={"success": True, "data": items})

    # ── I-2 담기 (§4.1) ──

    def _cart_add(self, body: Any) -> httpx.Response:
        # optionId 를 실어 오면 옵션 요구는 해소된 것 — 유일 옵션 자동 선택(#114) 재담기가 성공한다.
        if (
            self.fail_cart_add_code == "CART_OPTION_REQUIRED"
            and (body or {}).get("optionId") is None
        ):
            return httpx.Response(
                400,
                json={
                    "success": False,
                    "error": {
                        "code": "CART_OPTION_REQUIRED",
                        "message": "옵션을 선택해주세요",
                        "detail": {"options": self.cart_option_payload},
                    },
                },
            )
        if self.fail_cart_add_code == "PRODUCT_NOT_FOUND":
            return httpx.Response(
                404, json={"success": False, "error": {"code": "PRODUCT_NOT_FOUND"}}
            )
        item = {
            "cartItemId": 9000 + len(self.cart_items) + 1,
            "productId": (body or {}).get("productId"),
            "optionId": (body or {}).get("optionId"),
            "quantity": (body or {}).get("quantity", 1),
            "productName": self._name_of((body or {}).get("productId")),
        }
        self.cart_items.append(item)
        return httpx.Response(
            200, json={"success": True, "data": {"cartItemId": item["cartItemId"]}}
        )

    def _cart_view(self) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"items": self.cart_items}})

    # ── I-28 찜 목록 조회 (§4.16, 이슈 #386) ──

    def _wishlist_view(self) -> httpx.Response:
        """찜 0건도 200 + `items: []` 다(404 아님) — I-28 정상 응답."""
        if self.fail_wishlist:
            return httpx.Response(500, json={"success": False, "error": {"code": "INTERNAL"}})
        return httpx.Response(200, json={"success": True, "data": {"items": self.wishlist_items}})

    def _name_of(self, product_id: Any) -> str | None:
        for product in self.catalog:
            if product["productId"] == product_id:
                return product["name"]
        return None

    # ── I-19 구매 이력 (§4.7) ──

    def _order_status(self) -> httpx.Response:
        """I-4 status summary, isolated from I-19 purchase-dedup state/failures."""
        if self.order_status_exception is not None:
            raise self.order_status_exception
        if self.fail_order_status is not None:
            return httpx.Response(
                self.fail_order_status,
                json={"success": False, "error": {"code": "UNAVAILABLE"}},
            )
        if self.order_status_payload is not None:
            return httpx.Response(200, json=self.order_status_payload)
        return httpx.Response(
            200,
            json={"success": True, "data": {"orders": self.order_status_orders}},
        )

    def _orders(self) -> httpx.Response:
        if self.fail_purchases:
            return httpx.Response(503, json={"success": False, "error": {"code": "UNAVAILABLE"}})
        return httpx.Response(200, json={"success": True, "data": {"orders": self.orders}})

    # ── I-21 목록 push (§4.2, 경로 B) ──

    def _push(self, body: Any) -> httpx.Response:
        """lists[] 최상위만 받는다 (§4.2 v0.17.1) — 구 평평 3필드는 400 이다.

        BE 는 전환 기간에만 구 형식을 함께 수용하므로(RecommendationCallbackRequest), 이 스텁은
        전환 완료 상태를 재현해 우리가 구 형식으로 되돌아가면 통합 테스트가 깨지게 한다(이슈 #209).
        """
        if self.fail_push:
            return httpx.Response(500, json={"success": False, "error": {"code": "PUSH_FAILED"}})
        payload = body or {}
        lists = payload.get("lists")
        list_ids = [str(item.get("listId")) for item in lists] if isinstance(lists, list) else []
        invalid = (
            not isinstance(lists, list)
            or not 1 <= len(lists) <= 10  # §4.2 상한
            or len(set(list_ids)) != len(list_ids)  # 한 콜백 안 listId 중복
            or payload.get("listType") not in ("PICK_ONE", "BUY_ALL")  # 항상 싣는다
            or not payload.get("recommendationRequestId")
            or "listId" in payload  # 구 평평 형식 잔재
        )
        if invalid:
            return httpx.Response(
                400, json={"success": False, "error": {"code": "VALIDATION_ERROR"}}
            )
        for item in lists:
            self.pushed_lists[str(item.get("listId"))] = list(item.get("productIds") or [])
        return httpx.Response(200, json={"success": True, "data": {"listIds": list_ids}})

    # ── CH-5 목록 조회 (§4.3, FE→Spring) ──

    def _list_cards(self, list_id: str) -> httpx.Response:
        """push 된 id 목록을 Spring 이 표시 필드로 enrich 해 돌려주는 경로(표시 권위=Spring)."""
        product_ids = self.pushed_lists.get(list_id)
        if product_ids is None:
            return httpx.Response(404, json={"success": False, "error": {"code": "LIST_NOT_FOUND"}})
        by_id = {p["productId"]: p for p in self.catalog}
        cards = [by_id[pid] for pid in product_ids if pid in by_id]
        return httpx.Response(200, json={"success": True, "data": {"items": cards}})

    # ── I-17 변경분 pull (§4.8) ──

    def _changes(self, request: httpx.Request) -> httpx.Response:
        since = request.url.params.get("since", "0")
        for page in self.changes_pages:
            if str(page.get("since")) == str(since):
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": page.get("items", []),
                            "nextCursor": page.get("nextCursor"),
                            "hasMore": page.get("hasMore", False),
                        },
                    },
                )
        return httpx.Response(
            200,
            json={"success": True, "data": {"items": [], "nextCursor": since, "hasMore": False}},
        )

    # ── 검증 헬퍼 ──

    def requests_to(self, path_prefix: str) -> list[dict]:
        """경로 접두어로 기록된 요청을 추린다."""
        return [r for r in self.requests if r["path"].startswith(path_prefix)]
