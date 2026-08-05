"""장바구니 신원 도출 — `cart/graph.py`·`cart/remove.py`·`cart/wishlist.py` 공유(이슈 #116).

`graph.py` 안에 있던 것을 별도 모듈로 뺐다 — `graph.py` 가 `remove.py::stream_cart_remove` 를
가져오고 `remove.py` 는 이 신원 도출을 다시 쓰는 구조라, `remove.py` 가 `graph.py` 에서 직접
가져오면 순환 임포트가 된다. `graph.py` 는 하위 호환을 위해 이 함수를 그대로 재노출한다
(`from app.agents.buyer.cart.graph import cart_identity` 가 계속 동작한다).
"""

from __future__ import annotations


def cart_identity(identity) -> tuple[int | None, str | None]:
    """신원 → (userId, guestId). 회원=userId(숫자), 게스트=guestId(UUID), 익명(dev 무토큰)=둘 다 None.

    user_id 는 JWT sub 원문 문자열이라 숫자 보장이 없다 — 비숫자면 익명 취급((None, None))해
    상위가 CART_ERROR 로 우아하게 처리하게 한다(미처리 ValueError 로 스트림이 죽지 않게).
    """
    if not identity.is_guest and identity.user_id:
        try:
            return int(identity.user_id), None
        except (ValueError, TypeError):
            return None, None
    if identity.is_guest and identity.subject:
        return None, identity.subject
    return None, None
