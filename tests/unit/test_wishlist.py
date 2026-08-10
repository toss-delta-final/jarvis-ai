"""찜 어댑터 (이슈 #117, I-26/I-27/I-28 — 확정 2026-08-05, Spring 구현됨) — spring_client 배선
단위 테스트.

[#285] BE `jarvis-backend` main 실측(2026-08-08, BE PR #92·#93) — api-spec §4.12~4.16 v0.31.3.
`_client` 몽키패치로 라이브 Spring 없이 성공/오류 code → typed 예외 매핑과 query 파라미터 배선만
고정한다 — 단위 테스트라 라이브 의존을 두지 않는다(tests/unit/test_cart.py 의
`_CartClient`/`_CartResp` 패턴을 그대로 따른다).
"""

from __future__ import annotations

import pytest

from app.schemas.spring import AddWishlistRequest


class _WishlistResp:
    def __init__(self, status_code, data) -> None:
        self.status_code = status_code
        self._data = data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._data


class _WishlistClient:
    def __init__(self, resp) -> None:
        self._resp = resp
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return self._resp

    async def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return self._resp

    async def delete(self, url, params=None):
        self.calls.append(("DELETE", url, params))
        return self._resp


# ─────────── I-26 찜 추가 ───────────


async def test_add_wishlist_success_parses_product_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    client = _WishlistClient(_WishlistResp(200, {"success": True, "data": {"productId": 42}}))
    monkeypatch.setattr(sc, "_client", lambda: client)
    result = await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))
    assert result.success and result.product_id == 42
    assert client.calls == [("POST", "/internal/wishlist", {"userId": 1, "productId": 42})]


async def test_add_wishlist_product_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 PRODUCT_NOT_FOUND — 없는 상품만(HIDDEN·품절은 찜 가능, 별도 예외)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(404, {"error": {"code": "PRODUCT_NOT_FOUND"}})),
    )
    with pytest.raises(sc.WishlistProductNotFound):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=999))


async def test_add_wishlist_404_with_wrong_code_raises_wishlist_error_not_product_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23] 엔드포인트 미배포로 오는 404(code 가 계약과 다르거나 없음)를 "없는 상품"
    으로 오인하면 안 된다 — code 가 정확히 `PRODUCT_NOT_FOUND` 일 때만 typed 예외다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(404, {"error": {"code": "NOT_FOUND"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=999))


async def test_add_wishlist_duplicate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """409 WISHLIST_DUPLICATE → WishlistDuplicate."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(409, {"error": {"code": "WISHLIST_DUPLICATE"}})),
    )
    with pytest.raises(sc.WishlistDuplicate):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


async def test_add_wishlist_resource_conflict_also_raises_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """409 RESOURCE_CONFLICT(UNIQUE 경합)도 WISHLIST_DUPLICATE 와 동일하게 처리한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(409, {"error": {"code": "RESOURCE_CONFLICT"}})),
    )
    with pytest.raises(sc.WishlistDuplicate):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


async def test_add_wishlist_409_with_wrong_code_raises_wishlist_error_not_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23] 409 여도 code 가 `WISHLIST_DUPLICATE`/`RESOURCE_CONFLICT` 어느 쪽도 아니면
    "이미 찜함"으로 오인해 성공 안내로 종료하지 않는다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(409, {"error": {"code": "CONFLICT"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


async def test_add_wishlist_forbidden_maps_to_wishlist_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 AUTH_FORBIDDEN(SELLER·ADMIN) 은 전용 예외 없이 WishlistError 로 낙성한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(403, {"error": {"code": "AUTH_FORBIDDEN"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


async def test_add_wishlist_500_maps_to_wishlist_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(500, {"error": {"code": "INTERNAL_ERROR"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


async def test_add_wishlist_200_success_false_raises_wishlist_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 이지만 공통 봉투 success:false 면 성공으로 처리하지 않는다(2차 리뷰 지적 5)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _WishlistClient(_WishlistResp(200, {"success": False, "data": None}))
    )
    with pytest.raises(sc.WishlistError):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


async def test_add_wishlist_unparseable_product_id_raises_wishlist_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """라운드 15, head `0b33e06` 리뷰 A — Spring 이 200 + productId 를 dict 등 변환 불가 값으로
    주면 pydantic `ValidationError` 가 그대로 새서 `stream_wishlist_add` 의 `except
    WishlistError` 를 비껴가 우아한 degrade 가 무너졌다(재현 확인). 같은 파일 `get_wishlist` 는
    이미 `ValidationError` 를 낙성하는데 `add_wishlist` 만 빠져 있었다 — 다른 어댑터와 같은
    규약으로 `WishlistError` 로 낙성해야 한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(
            _WishlistResp(200, {"success": True, "data": {"productId": {"nested": "object"}}})
        ),
    )
    with pytest.raises(sc.WishlistError):
        await sc.add_wishlist(AddWishlistRequest(user_id=1, product_id=42))


# ─────────── I-27 찜 해제 ───────────


async def test_remove_wishlist_success_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    client = _WishlistClient(_WishlistResp(200, {"success": True, "data": None}))
    monkeypatch.setattr(sc, "_client", lambda: client)
    result = await sc.remove_wishlist(42, user_id=1)
    assert result is None
    # path 는 productId(wishlistId 아님), query 는 userId 하나뿐.
    assert client.calls == [("DELETE", "/internal/wishlist/42", {"userId": 1})]


async def test_remove_wishlist_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 WISHLIST_NOT_FOUND — 찜 안 한 상품 = 이미 해제 = 없는 상품도 동일 코드(구별 불가).
    비멱등 — 이미 해제된 걸 다시 해제해도(두 번째 호출) 404 그대로다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(404, {"error": {"code": "WISHLIST_NOT_FOUND"}})),
    )
    with pytest.raises(sc.WishlistNotFound):
        await sc.remove_wishlist(42, user_id=1)


async def test_remove_wishlist_404_with_wrong_code_raises_wishlist_error_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23] 엔드포인트 미배포로 오는 404(code 가 계약과 다르거나 없음)를 "이미 해제됨"
    으로 오인하면 안 된다 — code 가 정확히 `WISHLIST_NOT_FOUND` 일 때만 typed 예외다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(404, {"error": {"code": "NOT_FOUND"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.remove_wishlist(42, user_id=1)


async def test_remove_wishlist_404_empty_body_raises_wishlist_error_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23] 라우트 자체가 없어 본문이 아예 비는 404(code 를 못 읽음)도 같은 이유로
    `WishlistError` 여야 한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(sc, "_client", lambda: _WishlistClient(_WishlistResp(404, {})))
    with pytest.raises(sc.WishlistError):
        await sc.remove_wishlist(42, user_id=1)


async def test_remove_wishlist_forbidden_maps_to_wishlist_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(403, {"error": {"code": "AUTH_FORBIDDEN"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.remove_wishlist(42, user_id=1)


async def test_remove_wishlist_500_maps_to_wishlist_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.15 500 INTERNAL_ERROR → WishlistError(add_wishlist·delete_cart_item 의 500 매핑과
    같은 낙성처인데 remove_wishlist 만 빠져 있었다, #285 T4)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(500, {"error": {"code": "INTERNAL_ERROR"}})),
    )
    with pytest.raises(sc.WishlistError):
        await sc.remove_wishlist(42, user_id=1)


async def test_remove_wishlist_200_success_false_raises_wishlist_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 이지만 공통 봉투 success:false 면 성공으로 처리하지 않는다(2차 리뷰 지적 5)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _WishlistClient(_WishlistResp(200, {"success": False, "data": None}))
    )
    with pytest.raises(sc.WishlistError):
        await sc.remove_wishlist(42, user_id=1)


# ─────────── I-28 찜 목록 ───────────


async def test_get_wishlist_parses_items(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {
                    "productId": 42,
                    "name": "파우치",
                    "brandName": "브랜드",
                    "price": 12900,
                    "originalPrice": 15900,
                    "imageUrl": "https://example.com/x.jpg",
                    "rating": 4.5,
                    "reviewCount": 10,
                    "purchaseState": "AVAILABLE",
                }
            ]
        },
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    view = await sc.get_wishlist(1)
    assert len(view.items) == 1
    assert view.items[0].product_id == 42
    assert view.items[0].name == "파우치"
    assert view.items[0].purchase_state == "AVAILABLE"
    assert client.calls == [("GET", "/internal/wishlist", {"userId": 1})]


async def test_get_wishlist_missing_purchase_state_key_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`purchaseState` 키가 아예 없는 응답(BE 미배포 구간)은 예외 없이 파싱되고 `None`(모름)이
    돼야 한다 — **`"AVAILABLE"` 이 아니다**(#310).

    PR #305 시점엔 구 boolean 필드(기본값 참)와 의미를 맞추려 `"AVAILABLE"` 을 뒀지만, 이제
    이 값을 읽어 안내 문구를 가르므로 기본값이 **주장으로 승격**된다 — 키가 없다는 사실을
    "구매 가능이 확인됨"으로 바꿔 읽으면 품절 상품을 살 수 있다고 안내하게 된다. 모름은
    주장이 아니므로 `None` 으로 두고 아무 라벨도 붙이지 않는다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {"items": [{"productId": 42, "name": "파우치"}]},
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    view = await sc.get_wishlist(1)
    assert view.items[0].purchase_state is None


@pytest.mark.parametrize("purchase_state", ["SOLD_OUT", "HIDDEN"])
async def test_get_wishlist_parses_non_default_purchase_states(
    monkeypatch: pytest.MonkeyPatch, purchase_state: str
) -> None:
    """`Literal` 오타 방어 — `SOLD_OUT`/`HIDDEN` 값도 그대로 파싱돼야 한다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {"items": [{"productId": 42, "name": "파우치", "purchaseState": purchase_state}]},
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    view = await sc.get_wishlist(1)
    assert view.items[0].purchase_state == purchase_state


async def test_get_wishlist_unknown_purchase_state_skips_only_that_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """재현·수정 확인 — BE 가 `purchaseState` 에 계약 밖 값을 하나 추가해도 그 항목만 skip
    되고 나머지 정상 항목은 살아남아야 한다(전체 `ValidationError` 로 죽지 않는다).
    `PurchaseState` Literal 은 바뀌지 않는다 — 미지의 값을 관대 수용하는 게 아니라 그 항목만
    걸러내는 파싱 견고성 수정이다.

    **찜과 장바구니는 처방이 다르다**(#310): 장바구니(`CartViewItem`)는 미지 값이 와도 항목을
    skip 하지 않고 필드만 `None` 으로 강등한다 — 항목이 목록에서 사라지면 "전부 빼줘"가 일부만
    지우고 성공을 보고하기 때문이다. 찜은 그런 파괴적 후속 동작이 없어 skip 이 유효하다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {"productId": 1, "name": "이어폰", "purchaseState": "AVAILABLE"},
                {"productId": 2, "name": "케이스", "purchaseState": "DISCONTINUED"},
            ]
        },
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    view = await sc.get_wishlist(1)
    assert len(view.items) == 1
    assert view.items[0].product_id == 1
    assert view.items[0].purchase_state == "AVAILABLE"


@pytest.mark.parametrize("bad", [{}, [], {"kind": "SOLD_OUT"}, ["SOLD_OUT"]])
async def test_get_wishlist_unhashable_purchase_state_skips_item_without_typeerror(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """unhashable 값(dict·list)이 와도 `TypeError` 가 아니라 **항목 skip** 이어야 한다(PR #400 리뷰).

    `WishlistItem` 은 `CartViewItem` 과 달리 `BeforeValidator` 가 없고 **pydantic 내장 Literal
    검증에만 의존**한다. 그 검증기는 pydantic-core(Rust) 구현이라 hashable 여부와 무관하게
    `literal_error` → `ValidationError` 를 내므로 지금은 안전하다 — `_parse_wishlist_items` 의
    `except ValidationError` 에 정상적으로 걸린다.

    이 테스트는 **그 안전성이 구현 세부에 기대고 있다는 사실을 고정한다.** 카트 쪽이 위험했던
    이유는 `Literal` 자체가 아니라 거기 붙인 `BeforeValidator` 가 파이썬 `value in frozenset` 을
    호출했기 때문이다(`hash()` 실패 → `TypeError` → pydantic 이 감싸지 않고 전파). 훗날 여기에도
    같은 형태의 validator 를 붙이면 그 함정에 그대로 빠지므로, 그때 이 테스트가 잡는다.
    """
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {"productId": 1, "name": "정상", "purchaseState": "SOLD_OUT"},
                {"productId": 2, "name": "깨진값", "purchaseState": bad},
            ]
        },
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    view = await sc.get_wishlist(1)
    assert len(view.items) == 1  # 깨진 항목만 빠지고 정상 항목은 살아남는다
    assert view.items[0].product_id == 1
    assert view.items[0].purchase_state == "SOLD_OUT"


async def test_get_wishlist_all_items_unknown_purchase_state_fails_closed_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """전 항목 드리프트는 빈 목록으로 위장하지 않고 degrade 한다 — 원본 `items` 가 비어 있지
    않은데 전부 파싱 실패면, 이건 항목 하나의 이상이 아니라 BE 가 값 체계를 통째로 바꾼
    계약 전면 드리프트다. 이걸 빈 목록으로 돌려주면 찜을 여러 개 해 둔 사용자가 "찜한 상품이
    없어요."라는 확신에 찬 거짓 안내를 듣는다 — 조회 실패를 정직하게 알리는 편이 낫다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {"productId": 1, "name": "이어폰", "purchaseState": "DISCONTINUED"},
                {"productId": 2, "name": "케이스", "purchaseState": "RETIRED"},
            ]
        },
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    with pytest.raises(sc.SpringUnavailableError):
        await sc.get_wishlist(1)


async def test_get_wishlist_non_object_item_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """항목 자체가 object 가 아닌(최상위 타입 붕괴) 경우도 그 항목만 skip한다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {"items": ["not-an-object", {"productId": 1, "name": "이어폰"}]},
    }
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    view = await sc.get_wishlist(1)
    assert len(view.items) == 1
    assert view.items[0].product_id == 1


async def test_get_wishlist_items_not_a_list_still_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`items` 자체가 list 가 아닌 최상위 envelope drift 는 항목 단위 skip 대상이 아니라
    종전대로 fail-closed 다(개별 항목 이상과는 다른 문제)."""
    import app.services.spring_client as sc

    body = {"success": True, "data": {"items": {"not": "a-list"}}}
    client = _WishlistClient(_WishlistResp(200, body))
    monkeypatch.setattr(sc, "_client", lambda: client)
    with pytest.raises(sc.SpringUnavailableError):
        await sc.get_wishlist(1)


async def test_get_wishlist_empty_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """찜 0건도 200 + items:[] 정상(404 아님)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(200, {"success": True, "data": {"items": []}})),
    )
    view = await sc.get_wishlist(1)
    assert view.items == []


async def test_get_wishlist_200_success_false_does_not_masquerade_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 + success:false 를 "찜 0건"으로 위장시키지 않는다(2차 리뷰 지적 5) — 실패는
    SpringUnavailableError 로 낙성해야지, 빈 목록으로 조용히 넘어가면 사용자는 "찜한 상품이
    없어요"라는 잘못된 안내를 듣는다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(200, {"success": False, "data": {"items": []}})),
    )
    with pytest.raises(sc.SpringUnavailableError):
        await sc.get_wishlist(1)


async def test_get_wishlist_unavailable_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """도달 불가/오류/스키마 불일치는 get_cart 와 같은 SpringUnavailableError degrade."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _WishlistClient(_WishlistResp(500, {"error": {"code": "INTERNAL_ERROR"}})),
    )
    with pytest.raises(sc.SpringUnavailableError):
        await sc.get_wishlist(1)
