"""찜 어댑터 (이슈 #117, I-26/I-27/I-28, 🔶 초안 — BE 협의 전) — spring_client 배선 단위 테스트.

Spring 이 아직 이 엔드포인트를 구현하지 않아 실호출 통합 테스트는 하지 않는다(상대가 없다).
`_client` 몽키패치로 라이브 Spring 없이 성공/오류 code → typed 예외 매핑과 query 파라미터 배선만
고정한다(tests/unit/test_cart.py 의 `_CartClient`/`_CartResp` 패턴을 그대로 따른다).
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
                    "purchasable": True,
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
    assert view.items[0].purchasable is True
    assert client.calls == [("GET", "/internal/wishlist", {"userId": 1})]


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


# ─────────── config 플래그 기본값 ───────────


def test_cart_remove_and_wishlist_flags_default_off() -> None:
    """Spring 미구현이라 기본은 off — 켜지 않으면 오늘 동작이 바이트 동일해야 한다."""
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.cart_remove_enabled is False
    assert settings.wishlist_enabled is False
