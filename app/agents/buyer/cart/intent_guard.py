"""`cart_add` 로 라우팅된 발화의 삭제·찜 오담기 방어 (이슈 #116·#117, 패킷 §4).

decompose(app/agents/buyer/recommendation/decompose.py)는 다른 이슈(#84) 소유라 이 레인에서
`cart_remove`/`wishlist_add`/`wishlist_remove` intent 를 새로 만들 수 없다. 대신 `cart_add` 로
이미 들어온 발화 중 **명백한** 것만 결정론적으로 갈라낸다 — LLM 을 새로 부르지 않고, 프롬프트도
고치지 않는다. `cart_add` 로 라우팅되지 않는 발화("장바구니에서 빼줘"가 보통 가는 `cart_view`류)
는 이 판별기가 구조적으로 보지 못한다(결함이 아니라 경계, decompose intent 신설은 #84 몫).
"""

from __future__ import annotations


def classify_cart_utterance(message: str, settings) -> str:
    """'cart_add' | 'cart_remove' | 'wishlist_add' | 'wishlist_remove' — 확실할 때만 갈라낸다.

    기본값은 항상 `"cart_add"`(= 오늘 동작). 놓치는 것은 무해하고(오늘처럼 담긴다), 오탐하면
    사용자가 요청하지 않은 동작(담기 취소·찜)이 일어나므로 "확실할 때만 개입"한다
    (docs/lessons.md — 강한 신호는 약한 신호로 덮지 않는다, 양보는 앞단 early return 으로).

    판정 순서:
      0-a. `cart_add_markers`(담아·장바구니에 넣)가 있으면 즉시 `"cart_add"`. 담기는 이 판별기가
           다루는 신호 중 가장 강하다 — "찜한 거 장바구니에 담아줘"·"하나 빼고 담아줘"처럼 찜/삭제
           표지처럼 보이는 조각과 같은 발화에 있어도 담기가 이긴다. "장바구니에서 빼줘"는 이
           표지에 걸리지 않는다("장바구니에 넣"과 다른 문자열이라 오탐이 아니다) — 그래서
           삭제 판정(0-a 다음 단계)까지 내려갈 수 있다.
      0-b. `wishlist_reference_markers`(찜한·찜해둔·찜해 놓은·찜했던)가 있으면 즉시 `"cart_add"`.
           이 표지가 있는 발화의 동사는 항상 담기다 — 찜은 지시 대상을 수식할 뿐이다
           ("찜해둔 이어폰 담아줘"). 0-a 가 이미 대부분 잡지만(담기 표지가 보통 함께 온다),
           담기 표지 없이 지시만 있는 경우("찜해둔 거")까지 방어하려고 별도로 둔다.
           **알려진 거짓음성(라운드 2 리뷰, 의도한 보수성 — 넓히지 말 것)**: "찜한 거 빼줘"·
           "찜해둔 거 지워줘"는 이 규칙 때문에 삭제(`cart_remove`)로 갈라지지 않고 `"cart_add"`
           로 떨어진다. 그 발화가 "장바구니에서 빼라"인지 "찜을 풀어라"인지는 이 표지만으로
           결정론적으로 갈릴 수 없고, 애매하면 개입하지 않는 것이 이 판별기의 규칙이기 때문이다
           — 놓치는 결함이 아니라 설계한 보수성이다.
      1. `wishlist_remove_markers` 매칭 → `"wishlist_remove"`. **`cart_remove_markers` 보다
         먼저 본다** — "찜 빼줘"는 "빼줘"(삭제 표지)도 부분 문자열로 동시에 매칭하는데, 찜
         해제를 삭제보다 먼저 확정해야 "찜 빼줘"가 `cart_remove`로 새지 않는다.
      2. `wishlist_add_markers` 매칭 → `"wishlist_add"`. 단 발화에 `"장바구니"`가 있으면 찜으로
         가르지 않는다(계약상 찜·장바구니는 다른 자원이라 혼동 방지, 삭제 판정에는 영향 없음).
      3. `cart_remove_markers` 매칭 → `"cart_remove"`.
      4. 그 외 → `"cart_add"`(기본값).
    """
    if any(marker in message for marker in settings.cart_add_markers):
        return "cart_add"
    if any(marker in message for marker in settings.wishlist_reference_markers):
        return "cart_add"

    suppress_wishlist = "장바구니" in message
    if not suppress_wishlist and any(
        marker in message for marker in settings.wishlist_remove_markers
    ):
        return "wishlist_remove"
    if not suppress_wishlist and any(marker in message for marker in settings.wishlist_add_markers):
        return "wishlist_add"
    if any(marker in message for marker in settings.cart_remove_markers):
        return "cart_remove"
    return "cart_add"
