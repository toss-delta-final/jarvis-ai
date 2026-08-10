"""찜 추가·해제·조회 서브그래프 (이슈 #117 · #386, I-26/I-27/I-28 — 확정 2026-08-05).

**[#436] Spring 은 이미 구현했다** — BE main `InternalWishlistController` 실측(2026-08-07).
이 파일에 있던 "Spring 구현 진행 중" 표기는 그래서 지웠다. 같은 문구가 `remove.py`·
`config.py`·`spring_client.py`·`schemas/spring.py` 등에도 남아 있는데, 그 일괄 점검과
api-spec §4.14~4.16 헤더 갱신은 **#436 소유**라 여기서 건드리지 않는다.

`stream_cart_add` 가 `classify_cart_utterance` 로 "wishlist_add"/"wishlist_remove" 로 판정하면
항상 위임받는다(패킷 §5.4, 라운드 23 — 온/오프를 가리던 설정 필드 제거). 게스트 찜은 없다(I-26)
— 회원이 아니면 internal 호출 없이 degrade한다. 이벤트에 productId 를 싣지 않는다(확정, 경로 B)
— `remove.py` 와 구조·어조를 맞춘 형제 모듈이다.

**[#386] `stream_wishlist_view` 는 위 두 흐름과 위임 경로가 다르다** — `classify_cart_utterance`
2선 방어를 거치지 않고 `buyer/graph.py` 의 intent 분기에서 곧장 온다. 그 판별기는 `cart_add` 로
라우팅된 발화만 보는데 조회 발화는 거기 도달하지 않기 때문이다(반환 어휘에 `cart_view` 가 없는
것과 같은 이유, `intent_guard.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.intent_guard import (
    has_wishlist_remove_evidence,
    wishlist_remove_known_words,
)
from app.agents.buyer.cart.negation import (
    has_any_negation,
    has_terminated_name_tail,
    matches_name_unnegated_as_command,
)
from app.agents.buyer.cart.purchase_state import state_suffix
from app.agents.buyer.recommendation.state import CartIntent
from app.core.text import _strip_unsafe
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import AddWishlistRequest, WishlistItem
from app.services import spring_client
from app.services.spring_client import (
    SpringUnavailableError,
    WishlistDuplicate,
    WishlistError,
    WishlistNotFound,
    WishlistProductNotFound,
)


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


# 찜 대상을 확정하지 못했을 때의 되물음 문구 (#435).
#
# 기본 문구는 **한 글자도 바꾸지 않는다** — `last_reco`(스레드 누적 추천)가 빈 절대다수 경로가
# 오늘과 바이트 동일해야 한다(`cart/graph.py::_UNRESOLVED_DEFAULT` 와 같은 규약). "추천을 먼저
# 받아보시면"은 **이미 추천을 받은** 사용자에게 거짓으로 읽힌다 — `last_reco` 가 비어 있지 않으면
# "이름을 말씀해 주시면"으로 갈아 무엇을 말해야 할지 알려준다. "방금"처럼 시점을 단정하는 표현은
# 쓰지 않는다 — `last_reco` 는 누적이라 직전 턴이 아닐 수 있다.
_WISHLIST_UNRESOLVED_DEFAULT = "어떤 상품을 찜할까요? 추천을 먼저 받아보시면 찜해 드릴게요."
_WISHLIST_UNRESOLVED_WITH_RECO = (
    "어떤 상품을 찜할까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 찜해 드릴게요."
)
_WISHLIST_UNRESOLVED_AFTER_PUSH_FAILURE = "어떤 상품을 찜할까요? 추천 목록 전달에 문제가 있었어요. 다시 추천을 요청해 주시면 도와드릴게요."


def _wishlist_add_unresolved_notice(has_last_reco: bool, *, has_push_failed: bool = False) -> str:
    """찜 담기 미해소 문구 — 추천 목록이 있으면 그 문구가 실패 마커보다 우선한다."""
    if has_last_reco:
        return _WISHLIST_UNRESOLVED_WITH_RECO
    if has_push_failed:
        return _WISHLIST_UNRESOLVED_AFTER_PUSH_FAILURE
    return _WISHLIST_UNRESOLVED_DEFAULT


def _display_wishlist_name(item: WishlistItem) -> str:
    """안내 문구용 상품명 — 반드시 `_strip_unsafe` 를 거친다(판매자 입력, `remove.py` 와 같은 규약)."""
    return _strip_unsafe(item.name or "") or "상품"


def _wishlist_unresolved_notice(items: list[WishlistItem]) -> str:
    """되물음 문구 — 지금 찜한 상품명을 나열해 무엇을 물어야 할지 알려준다(`remove.py` 와 같은 철학).

    **[라운드 19, head `26f5596` 리뷰]** `remove.py::_unresolved_notice` 와 같은 사안·같은
    해법 — 이 되물음도 **상태를 저장하지 않는다**(옵션 되물음 `PendingAdd` 와 다르다). 사용자가
    다음 턴에 상품명만 답하면 `classify_cart_utterance` 가 찜 해제 표지 없는 발화를 기본값
    `"cart_add"` 로 떨어뜨려 이 흐름으로 돌아오지 못한다. 다중 턴 pending 상태 신설은 이 레인의
    범위 밖(후속 이슈)이라, 대신 문구가 다음 답을 판별기가 다시 잡을 수 있는 형태(찜 해제 동작
    표지 포함)로 유도한다 — 상품명만 답하면 여전히 담기로 새는 것은 문구로 유도할 뿐 강제하지
    않는 알려진 한계다(`remove.py` 참조).

    **[#310]** 구매 가능 상태 라벨을 함께 싣는다(장바구니 조회·삭제 되물음과 같은 규칙). 찜은
    "나중에 사려고 담아둔" 목록이라 시간이 지나며 상태가 바뀌기 쉬워 이 안내의 값어치가 크다.
    다만 안내 **문장**은 더하지 않는다 — 이 문구의 목적은 "어느 걸 뺄지 묻기"라 문장을 얹으면
    초점이 흐려진다(장바구니 조회는 목적이 달라 문단 끝 안내를 싣는다).
    """
    names = ", ".join(
        f"{_display_wishlist_name(item)}{state_suffix(item.purchase_state)}" for item in items
    )
    # 예시는 살 수 있는 항목을 우선한다 — 전부 못 사면 그대로 첫 항목(`remove.py` 와 같은 규칙).
    example_item = next(
        (item for item in items if item.purchase_state in (None, "AVAILABLE")), items[0]
    )
    example = _display_wishlist_name(example_item)
    return f"찜한 상품: {names}. 예) '{example} 찜 빼줘'처럼 상품명과 함께 말씀해 주세요."


def _resolve_wishlist_remove_target(
    cart: CartIntent,
    message: str,
    items: list[WishlistItem],
    settings,
    *,
    screen_reference_attempted: bool = False,
    screen_resolved: bool = False,
) -> WishlistItem | None:
    """찜 해제 대상을 결정론적으로 해소한다(패킷 §5.4, 2차 리뷰 지적 4 + 라운드 10·11 이후) —
    강한 신호부터 순서대로 확정한다:
      1. 이름이 발화에 **부분 문자열로**, 그리고 **부정되지 않은 출현으로** 등장 → 정확히
         1건이면 그것, 2건 이상이면 **그 자리에서 미해소**(2번 문맥 id 로 내려가지 않는다 —
         `remove.py::_resolve_remove_targets` 와 같은 "강한 신호가 모호하면 더 약한 신호로
         임의로 고르지 않는다" 원칙). **[라운드 11]** 부정 판정은 3번의 문장 전체 가드
         (`has_any_negation`)가 아니라 `negation.matches_unnegated` 의 **출현 단위** 판정을
         쓴다 — 문장 전체 가드를 이름 매칭에 쓰면 "이어폰은 찜 빼지 말고 케이스 찜 빼줘"에서
         정상 해제 대상인 "케이스"까지 함께 죽어 되물음이 된다.
      2. 발화에 부정·대조 신호(`negation.has_any_negation`, 문장 전체 검사)가 **없고 이름을
         대려는 시도도 없을 때만**(3번과 같은 `name_mentioned` 가드) `cart.product_id`
         (decompose 가 문맥에서 이미 골라 온 값)가 목록 안에 있으면 그것. **[라운드 16]** 문맥
         id 는 이름이 없어도 쓰이는 신호라 "출현 단위"로 볼 대상 자체가 없다 — 그래서 1번(이름
         매칭)처럼 출현 단위가 아니라 3번과 같은 문장 전체 판정을 쓴다. 이름을 대려는 시도가
         있었는데 1번(부분 문자열·경계·부정 판정)이 못 잡았다면(예: 발화의 이름이 목록 어느
         항목과도 정확히 일치하지 않는 변형), 문맥 id 로 대신 확정하지 않고 되물음으로 내려간다
         — 문맥 id 는 사용자가 입으로 말한 이름보다 약한 신호이므로, 이름이 있었는데 못 맞춘
         경우까지 문맥 id 가 대신 나서면 안 된다("이어폰케이스 찜 빼줘"에서 이름 매칭이 실패해도
         `cart.product_id` 가 가리키는 무관한 항목이 해제되던 결함, 재현·수정 확인).
      3. 발화에 부정·대조 신호가 **없고**, 목록 어느 항목의 이름도 (경계와 무관하게) 발화에
         아예 등장하지 않을 때만 위 두 규칙이 모두 안 잡히고 목록이 정확히 1건이면 그 1건.
      4. 그 외 → `None`.

    **문맥 id 보다 발화의 이름을 먼저 본다**(2차 리뷰 지적 4 — 이전 순서는 반대였다). 재현:
    찜 목록 `10=이어폰 / 20=케이스`, 발화 `"이어폰 찜 빼줘"`, `cart.product_id=20`(LLM 오추출·
    stale). 문맥 id 를 먼저 보면 사용자가 입으로 말한 상품이 아니라 케이스가 해제된다 —
    `docs/lessons.md` 의 "강한 신호(이름 지목)가 있으면 약한 신호로 덮지 않는다"와 정반대다.
    사용자가 입으로 말한 이름은 LLM 이 문맥에서 고른 id 보다 강한 신호다.

    **[라운드 10]** "목록 1건 자동"(3번)도 `remove.py` 의 "전체 삭제"·"방금 담은 거"와 같은
    성격 — 사용자가 이름을 대지 않은 대상을 코드가 고른다. 직접 호출해 확인한 재현: 찜이
    1건(`이어폰`)뿐인 상태에서 `"방금 찜한 건 안 빼도 되고 저건 찜 빼줘"` 처럼 다른 대상("저건")
    을 가리키며 지금 있는 항목은 빼지 말라고 부정한 발화가 `wishlist_remove` 로 라우팅되면(뒤쪽
    "찜 빼줘"가 그 자체로는 안 부정됐으므로), 이름도 문맥 id 도 안 잡혀 3번이 실행돼 사용자가
    "빼지 말라"고 명시한 바로 그 항목이 삭제됐다. `negation.has_any_negation` 으로 같은 가드를
    건다 — `remove.py` 와 같은 공용 함수를 써서 "한쪽만 고치는" 재발을 막는다(부정 인지 결함
    3연발의 세 번째가 바로 이 함수였다).

    `remove.py::_resolve_remove_targets` 와 규칙 모양(강한 신호 우선·부분 문자열 이름 매칭·단건
    자동·부정 가드)이 겹치지만 **공용 헬퍼로 묶지 않았다**. 자료형이 다르고(`CartViewItem` vs
    `WishlistItem`, 필드명도 `product_name` vs `name`), 이쪽에는 "전체 삭제"·"방금 담은 거"
    같은 규칙이 아예 없다 — 억지로 하나의 함수로 묶으면 두 흐름이 서로 쓰지 않는 매개변수(상대
    쪽의 `cart_remove_all_markers`·`last_add` 같은)까지 시그니처에 끌고 다니게 되고, 한쪽 규칙을
    바꿀 때 다른 쪽 테스트까지 다시 봐야 한다. 겹치는 부분(이름 매칭·부정 판정)은 각각
    `_strip_unsafe`·`negation.py` 공용 함수로 이미 공유돼 있어, 남은 겹침은 "이 조각들을 어떤
    순서로 조합하느냐"뿐이라 분리해서 각자 읽기 쉽게 두는 편이 더 싸다.

    **[#440] 아래 해제 근거 게이트(`intent_guard.has_wishlist_remove_evidence`)도 `remove.py`
    와 공유하지 않는다** — 같은 이유의 연장이다. 이 근거 판정의 head 축은 `"찜"` 이라는 **찜
    전용 지시 명사**다. 장바구니 삭제에는 대응하는 head 가 없고(`"장바구니"` 는 이미 억제 축으로
    쓰인다), `찜닭`/`갈비찜` 같은 다른 낱말에 묻히는 충돌도 없다(그 발화들엔 애초에 삭제 대상이
    될 "장바구니" 항목 이름이 겹칠 일이 없다). 겹치는 **기계**(`negation.matches_pair_unnegated`)
    는 이미 공용이고, 남는 것은 "어떤 목록으로 인스턴스화하느냐"뿐이라 `remove.py::
    _resolve_remove_targets` 는 한 줄도 고치지 않는다.

    **[라운드 15, head `0b33e06` 리뷰 B]** 1번의 이름 매칭에 경계 검사가 없어 "이어폰케이스
    찜 빼줘"류(찜 목록에 이어폰만 있음)에서 다른 낱말에 파묻힌 이름까지 매칭됐다. `remove.py`
    와 같은 이유로 `negation.matches_name_unnegated`(오른쪽 경계에 한국어 조사를 허용하는
    전용 헬퍼, `matches_unnegated` 와 별개 — 그 함수 정의의 근거 참조)를 쓴다.

    **[라운드 17, head `6ab47c9` 리뷰]** 라운드 15 는 이름 뒤가 공백이면(예 "이어폰 케이스
    찜 빼줘") 여전히 매칭되는 문제를 알려진 한계로 미뤘는데, 그 판단이 틀렸다 — 찜 목록에
    "이어폰"만 있을 때 이 발화는 사용자가 요청하지 않은 "이어폰"을 확인 없이 해제하는
    **파괴적 동작**이다(`remove.py` 와 같은 이유, 그 파일 docstring 참조). `matches_name_
    unnegated` 오른쪽 경계를 "이름 + (조사) + (filler) + 표지" 형태만 인정하도록 고쳐서
    해결했다 — 판정은 `remove.py` 와 완전히 같은 공용 함수(`negation.matches_name_unnegated`)
    를 그대로 쓴다(새로 구현하지 않는다).

    **[라운드 10 리뷰 F26 → 라운드 11 리뷰 F28 로 강화]** "이름을 댔다"(어절 경계)와 "그
    이름이 명령의 대상이다"는 다르다 — 라운드 10 은 이름→tail 구간에 종결과 "앞에 인용부호가
    없어야 한다"는 거부 목록(`utterance_quote_open_chars`)을 걸었는데, `"사용자가 말한 건
    이어폰 찜 빼줘"`(부호 없는 간접화법)가 그 목록을 그대로 통과해 실제로 이어폰을 지웠다
    (실측, 재현). `"내가 산 이어폰 찜 빼줘"`(정상)와 구조가 같아서 — 접두 내용의 **의미**로만
    갈리는 이상 어떤 거부 목록으로도 못 가른다. 그래서 라운드 11 은 방향을 뒤집는다 — 인용부호
    목록을 삭제하고, `negation.matches_name_unnegated_as_command` 가 규칙 2·3 과 **같은 전체
    왼쪽 앵커**(`prefix_words`, 발화 시작부터 닫힌 접두만으로 이름 시작에 도달해야 한다)를
    받게 한다. `remove.py` 는 이 계약을 받지 않는다(그대로 `matches_name_unnegated`).
    **받아들이는 축소**: `"내가 산 이어폰 찜 빼줘"`도 이제 되물음으로 간다(전에는 삭제) —
    "내가 산"이 닫힌 접두가 아니라서 그 이름이 지금 지목한 대상인지 증명할 수 없기 때문이다.
    잃는 것은 되물음 한 번(비파괴적, 상품명과 `'{이름} 찜 빼줘'` 예시를 준다)이고 막는 것은
    간접화법·예시·번역 요청이 사용자가 요청하지 않은 삭제로 이어지는 것(파괴적)이라 이 방향을
    택했다(#116/#117 이 실 LLM 24 라운드로 수렴한 "애매하면 파괴적 동작을 하지 않는다" 원칙).

    **[라운드 11 리뷰 F28] 다중 이름 사슬의 마지막 노드가 미종결이면 첫 이름만 남는 구멍도
    닫는다** — `"이어폰이랑 케이스 찜 빼줘, 이 표현이 맞아?"`는 "이어폰"이 사슬 연결
    (`negation._name_trailing_command_end` 의 `(True, None)`, 정상 사슬의 모호 판정이 그
    관용 위에 서 있어 그 자체는 그대로 둔다)이라 지역 판정만으로는 그대로 유효 처리되는데,
    사슬의 진짜 tail("케이스"+"찜 빼줘")이 뒤 메타언어 때문에 종결에 실패해도 "이어폰"의
    지역 판정은 그 사실을 모른다(재현, 파괴적 — 이어폰이 삭제됐다). `negation.has_terminated_
    name_tail`(전역 게이트, 아래 `has_terminated_tail`)을 이름 매칭과 **별도로** 함께 걸어
    "등록된 이름 중 하나라도 진짜 tail 로 발화를 끝내는가"를 바깥에서 확인한다 — 아무도
    끝내지 못하면(이 발화처럼) 사슬 전체가 무효다.

    경계 검사가 1번을 정확하게 만들어도, 찜 목록이 1건뿐이면 3번(목록 1건 자동)이 표지 없이도
    그 1건을 자동 선택해 같은 결과가 나올 수 있다 — `remove.py` 와 같은 이유로 3번에도 이름을
    대려는 시도(경계·부정과 무관한 원문 부분 문자열 겹침)가 있으면 자동 선택을 건너뛴다(새
    신호 아님, 이미 계산해 둔 겹침 재사용).

    **[라운드 16, head `3ca0f40` 리뷰]** docstring 은 줄곧 문맥 id(2번)를 이름(1번)보다 **약한**
    신호로 규정해 왔는데(2차 리뷰 지적 4), 정작 세 규칙 중 2번에만 부정 가드가 없었다 — 1번은
    출현 단위(라운드 11·15), 3번은 문장 전체(라운드 10) 가드를 진작 받았다. 그래서 이름을
    대지 않고 부정만 있는 발화("그건 빼지 말고 찜 빼줘", 찜 `[10=이어폰, 20=케이스]`,
    `cart.product_id=20`)에서 이름 매칭은 애초에 안 잡히니 곧장 2번으로 내려가 부정을 무시하고
    20 을 반환했다(재현 확인 — 사용자가 "빼지 말라"고 한 항목이 해제됨). 문맥 id 는 발화에
    이름이 없어도 쓰이는 신호라 부정 판정을 앵커할 특정 출현이 없다 — 그래서 1번처럼 출현 단위
    (`negation.matches_name_unnegated`)가 아니라 3번과 같은 문장 전체 판정(`negation.
    has_any_negation`, 라운드 10 공용 모듈)을 쓴다. `has_negation` 계산을 2번 앞으로 올려
    2·3번이 같은 값을 공유한다(중복 계산도, 판정 재구현도 아니다). `remove.py::
    _resolve_remove_targets` 는 네 규칙 모두 이미 가드를 받고 있어(라운드 10·11·15) 손대지
    않는다.

    **[#440 라운드 4 리뷰 F12] `screen_refused`** — 화면 지시어 해소기(`resolve_screen_reference`,
    `buyer/graph.py`)가 순번·좌표를 **확정하지 못하고 거부**했으면(`ordinal_out_of_range`·
    `ambiguous_screen_candidates`·`coordinate_out_of_range`, 그 함수 주석의 "되물음 강제") 2·3번
    자동 선택을 추가로 막는다. 해소기가 거부한 화면 발화가 `wishlist_remove` 로 왔을 때, 이 게이트
    없이는 2·3번이 그 거부를 모른 채 문맥 id/목록 1건으로 여전히 확정해 사용자가 가리키지 못한
    항목을 지웠다(재현: 화면 3건, 찜 1건, `"99번째 거 찜에서 빼줘"` → 3번이 되살려 그 1건을
    삭제, 화면 순번이 애초에 범위 밖이었는데도). 기본값 `False`(거부 없음)라 화면이 없거나 해소가
    필요 없는 호출부는 오늘과 동일하다. **[라운드 5 리뷰 F15]** `cart/graph.py` 의 2선 방어
    위임(`stream_cart_add` 가 부르는 경로)도 이 값을 넘긴다 — 처음엔 "그 경로는 `screen_reason`
    을 안 계산하니 기본값 그대로"라고 잘못 판단했는데, 실제로는 `decision.intent == "cart_add"`
    에도 화면 해소가 돌고 `screen_reason` 이 `stream_cart_add` 에 전달돼 있었다(하필 이 이슈의
    원래 거짓음성 경로). **[라운드 6 리뷰 F18]** 값 자체의 계산도 넓어졌다 — `buyer/graph.py`
    docstring 참조(해소기가 거부한 경우뿐 아니라 pending 턴처럼 애초에 안 돈 경우도 포함한다).

    **[라운드 10 리뷰 F27] `screen_refused` 파생값을 폐기하고 원자 둘로 쪼갰다** — 화면 3번째가
    503 으로 정확히 해소됐는데 찜 목록엔 77 하나뿐이면, 파생값(`screen_refused=False`, 위치를
    가리켰고 해소도 성공했으니 "거부 아님")을 규칙 3(목록 1건 자동)에 그대로 쓰면 3번이 그
    무관한 77 을 지웠다(재현, 파괴적 — `screen_resolved=True` 를 "fallback 허용"으로 잘못
    읽은 것). 사용자가 **특정 위치를 지목**했는데 그 지목이 대상 목록의 항목으로 이어지지
    않으면 되물어야지, 목록에 하나 있다는 이유로 **다른 것**을 지우면 안 된다 — 이 이슈를
    관통하는 원칙("코드가 고르는 규칙은 더 엄격해야 한다")의 이 경로 버전이다. 그래서 규칙 2·3
    이 서로 다른 계약을 받는다: 규칙 2(문맥 id)는 `screen_reference_attempted and not screen_
    resolved` 면 건너뛴다(= 옛 `screen_refused` 와 값이 같다 — 문맥 id 는 화면과 무관하게도
    쓰이는 신호라 "위치를 가리켰는데 확정 못 했을 때만" 막으면 된다). 규칙 3(목록 1건 자동)은
    `screen_reference_attempted` 이면 **해소 성공 여부와 무관하게** 건너뛴다 — 위치를 지목한
    이상 그 지목이 실패해도 "마침 목록에 하나 있으니 그걸로" 대체하면 안 된다.

    **[라운드 6 리뷰 F17] 규칙 1(이름 매칭)은 `screen_refused`(라운드 10부터는 `screen_
    position_mentioned`·`screen_resolved` 두 원자, F27 참조)는 안 받지만 근거 판정은
    받는다 — 이 둘은 다른 축이다.** 사용자가 이름을 직접 댔으면 화면 순번 해소와는 무관하게
    강한 신호라 화면 신호는 규칙 1을 막지 않는다(F12 문단대로). 하지만 `"이어폰 찜
    빼줘도 될까?"` 처럼 **발화 자체가 명령이 아니면**(유보·인용, `tail_is_command`) 얘기가
    다르다 — decompose 가 이 발화를 `wishlist_remove` 로 보내면 규칙 1은 그 거부를 모른 채
    이름만 보고 "이어폰"을 그대로 지웠다(재현 — 두 계층이 정반대 결론을 낸 것과 같다, 이
    이슈가 존재하는 이유 그 자체). **"이름이 약해서" 막는 게 아니라 "발화 자체가 명령이
    아니어서" 막는다.**

    **[라운드 9 리뷰 F22 → 라운드 11 리뷰 F28 로 되돌림] 규칙 1도 이제 규칙 2·3 과 같은 판정
    형태를 쓴다.** 라운드 9 는 규칙 1을 별도의 라우팅급 게이트(`is_wishlist_remove_command_
    context`, 왼쪽 전체 앵커 없음)로 갈랐었다 — 상품명은 닫힌 어휘가 아니라서(`"이어폰"`)
    전체 앵커를 걸면 `"이어폰 찜 빼줘"` 자체가 막힌다고 판단했기 때문이다. 그 분리가 라운드
    10·11 에서 연속으로 뚫렸다(위 F26/F28 문단) — 그래서 라운드 11 이 되돌렸다: 등급을
    나누는 대신, 왼쪽 앵커를 "상품명 **자체**가 닫힌 어휘여야 한다"가 아니라 "상품명 **앞**이
    닫힌 접두여야 한다"로 정의해 규칙 1에도 그대로 적용한다(`negation.matches_name_
    unnegated_as_command` 의 `prefix_words`). `"이어폰 찜 빼줘"`(접두 없음, 앵커 자명하게
    성립)는 여전히 그대로 규칙 1로 해소된다 — 무회귀. `is_wishlist_remove_command_context`
    함수는 삭제했다(같은 개념이 두 이름으로 남지 않게).
    """
    # ⚠️ [#440] **해결됨 — 해제 근거 게이트.** 아래 2·3번은 사용자가 이름을 대지 않은 대상을
    # **코드가 고른다** — 조회성 발화가 `wishlist_remove` 로 오분류돼 들어오면 요청하지 않은
    # 항목이 해제된다(찜이 1건뿐이면 3번이, 문맥 id 가 남아 있으면 2번이). #386 은 이 자리에
    # "해제 근거가 있을 때만 허용" 가드를 넣어 봤다가 **되돌렸다** — 짧은 표지(`"찜"` ⊂ `찜닭`,
    # `"빼"` ⊂ `빼고`)로는 조회와 해제를 가를 수 없어 오히려 `"찜닭 빼고 보여줘"` 를 해제 근거로
    # 오인했다("찜 명사 + 해제 동사" 단순 결합, 접근 ②). 그 전에 시도한 "조회 표지 목록"(접근
    # ①)도 한국어 조회 표현을 전수로 알아야만 성립해 `"내 찜 뭐야"` 류 목록 밖 표현에서 뚫렸다.
    # #440 은 `intent_guard.has_wishlist_remove_evidence` 로 이 자리를 다시 게이트한다 — 그 함수가
    # 쓰는 `negation.matches_pair_unnegated`(어절 경계 + 인접 창)가 접근 ②의 실패(다른 낱말에
    # 묻히는 짧은 표지)를 구조적으로 막고, "조회 표지 전수화" 대신 "해제 근거 존재"를 양성 조건
    # 으로 뒤집어 접근 ①의 실패(목록 밖 표현)도 피한다.
    #
    # 이 위험은 #386 이 만든 것이 아니다 — `wishlist_remove` 로 온 **어떤** 발화든 이름이 없으면
    # 원래부터 1건을 자동 선택했다. `evals/intent_probe` 실측에서 찜 조회 발화 3종이 8/8 로
    # 정확히 `wishlist_view` 에 가는 것도 확인했다(오분류가 관측되지 않았다).
    #
    # **[라운드 6 리뷰 F17] 규칙 1(이름 매칭)도 이제 이 게이트를 받는다** — 사용자가 이름을
    # 직접 대고 경계·부정 검사를 통과한 매칭은 여전히 가장 강한 신호이지만, `has_remove_
    # evidence` 가 `False` 라는 것은 "발화 자체가 명령이 아니다"(유보·인용)라는 뜻이라 이름이
    # 아무리 정확해도 파괴적 동작을 정당화하지 못한다("이어폰 찜 빼줘도 될까?" 재현, 위 함수
    # docstring "라운드 6 리뷰 F17" 문단 참조). 이 게이트가 없던 라운드 5 까지는 근거 판정(1·
    # 1-a·1-b)이 막았는데 규칙 1은 이름만 보고 그대로 지워 "두 계층이 정반대 결론"이라는, 이
    # 이슈가 존재하는 이유 그 자체인 결함이 남아 있었다.
    #
    # `remove.py::_resolve_remove_targets`(장바구니 삭제)와 이 게이트를 공유하지 않는다 — 이
    # 근거 판정의 head 축은 `"찜"` 이라는 **찜 전용 지시 명사**다. 장바구니 삭제에는 대응하는
    # head 가 없고(`"장바구니"` 는 이미 억제 축으로 쓰인다), `찜닭`/`갈비찜` 같은 다른 낱말에
    # 묻히는 충돌도 없다. 겹치는 **기계**(`negation.matches_pair_unnegated`)는 이미 공용이고,
    # 남는 것은 "어떤 목록으로 인스턴스화하느냐"뿐이라 `remove.py` 는 손대지 않는다.
    #
    # **[라운드 1 리뷰 F5] 알려진 축소(동작 변경 없음, 기록만).** 이 게이트 때문에
    # `wishlist_remove_action_markers`(tail) 목록 밖 표현은 규칙 2·3 자동 선택을 못 받고
    # 되물음으로 간다(예: `"찜 목록 비워줘"` — "비워줘"가 tail 목록에 없다). 전에는 decompose
    # 가 `wishlist_remove` 로 보내고 찜이 1건이면 그대로 자동 해제됐다. 이건 이슈가 요구한
    # 방향(애매하면 파괴적 동작 금지)이고 되물음이 상품명을 나열해 다음 답을 유도하지만, 이건
    # 가설이 아니라 실측이다 — `evals/intent_probe` 에서 decompose 가 `"찜닭 빼줘"`(찜 목록에
    # 없는 음식명)를 `wishlist_remove` 로 **8/8** 보냈다(`wishlist-remove-003` 셀). 이 게이트가
    # 없었다면 그 오분류가 자동 해제로 이어졌을 것이다. 되물음이 아니라 자동 선택을 넓히려면
    # `wishlist_remove_action_markers`(tail) 목록에 표현을 추가하면 된다.
    #
    # **[라운드 7 리뷰 F19] 알려진 축소 추가.** `negation.tail_is_command` 가 문장 종결 부호
    # (`.`·`!`·`?`) 뒤에 내용이 더 있으면 명령이 아니라고 보는 규칙을 얻으면서, `"찜한 거
    # 빼줘. 그리고 이것도 담아줘"` 처럼 **정상 명령 + 별개 문장**도 함께 되물음으로 간다 —
    # 의도한 것이다. 잃는 것은 되물음(비파괴적)이고, 이 규칙이 없으면 `"찜 취소해줘. 무슨
    # 뜻이야?"`(조회 의도) 같은 발화가 그대로 자동 해제로 새 나간다(F19 재현, 파괴적) — 비대칭
    # 이라 이 방향을 택했다.
    #
    # **[라운드 8 리뷰 F20] 이 게이트가 요구하는 근거의 극성이 뒤집혔다 — "발화 자체가 명령인가"
    # (`tail_is_command`, 기본-허용)가 아니라 "해제 동작구가 발화를 끝냈는가"(`tail_terminates_
    # utterance`, 기본-거부)를 요구한다(`intent_guard.has_wishlist_remove_evidence` docstring
    # 참조). tail 뒤 비명령 형태를 나열해 거부하는 방식은 "찜한 거 빼줘 도 될까?"(연결어미를
    # 띄어 씀)·"'찜 해제해줘'를 영어로 번역해줘"(따옴표+조사)처럼 다음 우회가 계속 나왔다 —
    # 열린 집합(후속 구문)을 목록으로 못 덮는다는 같은 실패가 tail 층에서 두 번째로 반복된
    # 것이다. **`classify_cart_utterance`(라우팅)는 여전히 관대한 `tail_is_command` 를 쓴다** —
    # 라우팅이 느슨해 `wishlist_remove` 로 잘못 와도 이 게이트가 근거 없음으로 막아 되물음으로
    # 끝나 무해하기 때문이다. 이 극성 뒤집기로 `"찜 취소해줘, 장바구니는 그대로 두고"`(2차
    # 리뷰 지적 8, 라우팅은 `wishlist_remove` 로 유지)도 **근거는 이제 `False`** 가 됐다 —
    # 쉼표 뒤에도 "장바구니는..." 이라는 실질 내용이 남아 "동작구가 발화를 끝냈다"는 조건을
    # 못 채우기 때문이다. 라우팅은 무회귀(계약대로 `wishlist_remove`)이고 자동 삭제 대신
    # 되물음으로 끝나는 축소라 안전하다 — 새로운 알려진 축소로 기록한다.
    #
    # **[라운드 11 리뷰 F28] 알려진 축소 추가.** 규칙 1(이름 매칭)도 규칙 2·3 과 같은 전체
    # 왼쪽 앵커를 받으면서, `"내가 산 이어폰 찜 빼줘"`·`"어제 본 이어폰 찜 빼줘"` 처럼 **열린
    # 접두 + 이름 지목**도 이제 되물음으로 간다(전에는 그대로 삭제됐다). 잃는 것은 되물음
    # 한 번(비파괴적, 되물음이 찜 상품명과 `'{이름} 찜 빼줘'` 예시를 준다) — 막는 것은
    # 간접화법·예시·번역 요청이 사용자가 요청하지 않은 삭제로 이어지는 것(파괴적)이다. 접두가
    # 열려 있으면 그 이름이 명령의 대상인지 증명할 수 없고, 증명할 수 없으면 묻는다(#116/#117
    # 이 실 LLM 24 라운드로 수렴한 "애매하면 파괴적 동작을 하지 않는다" 원칙).
    #
    # **[라운드 12 리뷰 F30] 라운드 11(F28)의 앵커가 너무 좁아 #116/#117 부정·대조 회귀
    # 가드 5건을 깨뜨렸었다 — `"이어폰(은) 찜 빼지 말고 케이스 찜 빼줘"`(사용자가 **A 는
    # 빼지 말고 B 를 빼라**고 명시한, 흔하고 중요한 발화) 계열. "케이스" 앞의 "이어폰은 찜
    # 빼지 말고"는 임의의 텍스트가 아니라 **이 판정이 이미 다루는 개념**(다른 후보 상품명·
    # head·tail 어간+부정 표지)인데, F28 은 앵커 어휘를 `wishlist_remove_prefix_words`류로만
    # 좁혀서 이걸 "모르는 낱말"로 오인했다(실측, 회귀 — 임의로 고치지 않고 그대로 보고했다).
    # F30 은 앵커 어휘를 "닫힌 접두"에서 **"이 판정이 아는 어휘 전부"**로 넓혀(head·tail
    # 계열·부정 표지·다른 후보 상품명, 전부 이미 있는 목록) 이 정상 대조 발화를 되살렸다 —
    # `negation._name_left_anchor_reachable` docstring 참조. F28 이 "받아들이는 축소"로
    # 선언한 `"내가 산 이어폰 찜 빼줘"`(간접화법·예시가 아니라 진짜 열린 수식어)는 이 어휘
    # 확장 뒤에도 여전히 막힌다 — "산"은 이 판정이 아는 어휘가 아니다.
    #
    # **[라운드 13 리뷰 F31] F30 의 구현(부정 표지 "창 점프")이 그 자체로 우회였다 — 삭제하고
    # 되돌렸다.** F30 은 `known_words` 로 아무것도 못 먹는 위치에서도 부정 표지가 8자 창 안에
    # 있으면 그 앞 모르는 문자를 통째로 건너뛰었다 — `"예: 이어폰 말고 케이스 찜 빼줘"`가
    # 그 인용·메모 접두("예: ")를 삼켜 실제로 케이스(20)를 삭제했다(실측, 파괴적). 창 점프
    # 대신 앵커 스캔 **전용** 어간 목록(`wishlist_remove_action_stems`, 명령 근거로는 쓰지
    # 않는다)을 `known_words` 에 더해 "빼지 말고"의 "빼"가 독립 토큰으로 소비되게 했다 —
    # `negation._name_left_anchor_reachable` docstring 참조.
    #
    # **[라운드 13 리뷰 F32] 알려진 축소.** `"이어폰은 찜 빼지 말고 대신 케이스 찜 빼줘"`(열린
    # 연결어미 "대신"이 낀 변형)는 여전히 되물음으로 막힌다 — "대신"을 앵커 어휘에 넣지 않기로
    # 했다(열면 다음 라운드에 또 다른 열린 연결어미가 뚫린다, F16 의 교훈과 같은 이유). 쉼표·
    # 세미콜론이 낀 변형(`"…말고, 케이스…"`)은 F31 의 경계문자 스킵 확장으로 이미 정상 해소된다
    # — 이건 축소가 아니라 회귀 수정이다.
    # [라운드 9 리뷰 F22 → 라운드 11 리뷰 F28 → 라운드 12 리뷰 F30 → 라운드 13 리뷰 F31/F33]
    # 규칙 1(이름 매칭)도 이제 규칙 2·3(`has_remove_evidence`, 아래)과 **같은 개념**(전체 왼쪽
    # 앵커)을 받는다 — 등급을 나눴던 게 두 라운드 연속 구멍이었다(`wishlist.py` 상단 docstring
    # "라운드 11 리뷰 F28" 문단 참조). F30 은 "닫힌 접두"를 **"이 판정이 아는 어휘 전부"**로
    # 넓혔고, F31/F33 은 그 어휘를 `intent_guard.wishlist_remove_known_words` 하나로 뽑아
    # 규칙 2·3(`has_wishlist_remove_evidence`)과 **글자 그대로 같은 목록**을 쓰게 한다(새 목록
    # 없음, 라운드 13 리뷰 F33 — "앵커 판정은 같은 코드여야 한다").
    known_words = wishlist_remove_known_words(settings)
    has_remove_evidence = has_wishlist_remove_evidence(message, settings)

    all_names = [name for item in items if (name := _strip_unsafe(item.name or ""))]
    name_matches = [
        item
        for item in items
        if (name := _strip_unsafe(item.name or ""))
        and matches_name_unnegated_as_command(
            message,
            name,
            settings.utterance_negation_markers,
            settings.utterance_negation_window,
            settings.utterance_prefix_negation_markers,
            settings.utterance_name_boundary_particles,
            settings.utterance_name_trailing_filler_words,
            settings.wishlist_remove_markers,
            all_names,
            known_words,
        )
    ]
    if name_matches:
        # [라운드 11 리뷰 F28] 다중 이름 사슬의 마지막 노드가 미종결이면(`"이어폰이랑 케이스
        # 찜 빼줘, 이 표현이 맞아?"`) 사슬 앞쪽 이름은 지역 판정(사슬 연결 관용)만으로 계속
        # 유효 처리된다 — 전역 게이트로 "등록된 이름 중 하나라도 진짜 tail 로 끝나는가"를
        # 바깥에서 확인한다. `negation.has_terminated_name_tail` docstring 참조.
        if not has_terminated_name_tail(
            message,
            all_names,
            settings.utterance_name_boundary_particles,
            settings.utterance_name_trailing_filler_words,
            settings.wishlist_remove_markers,
        ):
            return None
        return name_matches[0] if len(name_matches) == 1 else None

    has_negation = has_any_negation(
        message, settings.utterance_negation_markers, settings.utterance_prefix_negation_markers
    )
    # 2·3번이 공유하는 계산이라 여기서 한 번만 한다(라운드 10 교훈 — 같은 판정을 두 곳에
    # 구현하면 한쪽만 고쳐지는 재발이 난다).
    name_mentioned = any(
        (name := _strip_unsafe(item.name or "")) and name in message for item in items
    )

    if (
        cart.product_id is not None
        and not has_negation
        and not name_mentioned
        and has_remove_evidence
        and not (screen_reference_attempted and not screen_resolved)
    ):
        direct = [item for item in items if item.product_id == cart.product_id]
        if direct:
            return direct[0]

    if (
        not has_negation
        and not name_mentioned
        and len(items) == 1
        and has_remove_evidence
        and not screen_reference_attempted
    ):
        return items[0]
    return None


async def stream_wishlist_view(
    *,
    identity,
    get_wishlist_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """찜 목록 조회 서브그래프 (이슈 #386, I-28 §4.16).

    **`cart/graph.py::stream_cart_view` 와 같은 성격**이다 — 목록을 `token` 텍스트로만 답하고
    상품 카드도 `action` 도 내지 않는다(경로 B). 다만 신원 게이트는 **형제 찜 핸들러 쪽**을
    따른다: 장바구니는 게스트도 갖지만 찜은 회원 전용(I-26/I-27/I-28, M-4)이라
    `user_id is None` 하나로 게스트·익명을 함께 막는다. `stream_cart_view` 의
    `user_id is None and guest_id is None` 을 그대로 베끼면 게스트에게 계약 밖 경로가 열린다.

    **실패 처분이 `stream_wishlist_remove` 와 갈린다**(형제라고 베끼면 안 되는 지점) — 저쪽은
    조회를 *해제 대상 해소 수단*으로 부르므로 그 턴의 실패는 변경 실패이고
    `action(WISHLIST_REMOVE_FAILED)` 로 답한다. 이 턴은 상태를 바꾸지 않으므로 `token` 안내 후
    정상 `done` 으로 끝낸다 — `ActionData.type` 유니온(api-spec §3.1)에 조회 실패 어휘가 애초에
    없다. 개별 처리 자체는 형제 4개와 같은 규약이다(#368 — 빠뜨리면 상위 스트림의 범용
    catch-all 이 `error(INTERNAL)` 로 내보낸다).

    잡는 예외는 `SpringUnavailableError` **하나뿐**이다. `get_wishlist`(조회 계열)는
    `WishlistError` 를 내지 않는다 — `stream_wishlist_add` 가 두 타입을 함께 잡는 것은 주입
    가능한 인자를 방어하는 것이지 어댑터 규약이 아니다(`docs/lessons.md` #368 항목).

    `observer` 는 형제 핸들러(`stream_cart_view`·`stream_wishlist_add`/`_remove`)와 시그니처를
    맞추려고 받을 뿐 **이 함수는 쓰지 않는다**.
    """
    get_wishlist_fn = get_wishlist_fn or spring_client.get_wishlist

    user_id, _guest_id = cart_identity(identity)
    if user_id is None:
        yield sse(
            "token",
            TokenData(text="찜 목록 조회에는 로그인이 필요해요.").model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        wishlist_view = await get_wishlist_fn(user_id)
    except SpringUnavailableError:
        yield sse(
            "token",
            TokenData(text="찜 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.").model_dump(
                by_alias=True
            ),
        )
        yield _done()
        return

    items = list(wishlist_view.items)
    if not items:
        yield sse("token", TokenData(text="찜한 상품이 없어요.").model_dump(by_alias=True))
        yield _done()
        return

    # 못 사는 항목은 짧은 라벨로만 가른다(#310, 장바구니 조회와 같은 규칙). 문단 끝
    # `state_advice_lines` 안내는 **싣지 않는다** — 그 문구는 장바구니 어휘("다시 담을 수
    # 있어요")이고, 예시로 드는 `'{품목} 빼줘'` 는 `intent_guard` 가 의도적으로 `cart_add` 로
    # 떨어뜨리는 발화라(알려진 거짓음성, intent_guard.py) 답할 수 없는 안내가 된다.
    lines = [f"{_display_wishlist_name(item)}{state_suffix(item.purchase_state)}" for item in items]
    yield sse(
        "token",
        TokenData(text="찜한 상품이에요:\n" + "\n".join(lines)).model_dump(by_alias=True),
    )
    yield _done()


async def stream_wishlist_add(
    *,
    identity,
    cart: CartIntent,
    settings,
    allowed_product_ids: set[int] | None = None,
    has_last_reco: bool = False,
    has_push_failed: bool = False,
    add_wishlist_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """찜 추가 서브그래프(I-26, 확정 2026-08-05). `action`(WISHLIST_ADDED/WISHLIST_ADD_FAILED)
    또는 되물음 token 을 내고 `done` 으로 끝난다.

    `has_last_reco`(#435) 는 스레드 누적 추천(`last_reco`)이 비어 있지 않은지만 알리는 신호다 —
    미해소 문구를 가르는 데만 쓰고 판정에는 관여하지 않는다. 기본값 `False` 는
    `screen_reference.py` 의 "기본값 금지"(F-5) 와 다르다 — 여기서 빠뜨렸을 때의 실패 모드는
    **오담기가 아니라 문구 퇴화**(오늘 문구로 남는 것)뿐이라 안전한 쪽으로 기본값을 둘 수 있다.
    """
    add_wishlist_fn = add_wishlist_fn or spring_client.add_wishlist

    user_id, _guest_id = cart_identity(identity)
    if user_id is None:
        # 게스트·익명 모두 여기 걸린다(cart_identity 는 게스트를 guest_id 로, 익명을 (None, None)
        # 으로 돌려주므로 둘 다 user_id 가 None) — 게스트 찜은 계약에 없다(I-26). internal 호출
        # 없이 degrade한다. 폐기된 GUEST_NOT_ALLOWED action 을 되살리지 않는다(계약에서 폐기된 값).
        yield sse("token", TokenData(text="찜에는 로그인이 필요해요.").model_dump(by_alias=True))
        yield _done()
        return

    # 경로 B 가드 — stream_cart_add 의 unresolved 판정과 같은 규칙이다. 담기 쪽은 옵션 되물음
    # 진행 중(pending)이면 이미 검증된 상품이라는 예외 분기가 있지만, 찜에는 pending 개념이
    # 없으므로 그 분기가 없다.
    product_id = cart.product_id
    if product_id is None or (
        allowed_product_ids is not None and product_id not in allowed_product_ids
    ):
        yield sse(
            "token",
            TokenData(
                text=_wishlist_add_unresolved_notice(has_last_reco, has_push_failed=has_push_failed)
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        await add_wishlist_fn(AddWishlistRequest(user_id=user_id, product_id=product_id))
    except WishlistDuplicate:
        # [확정 2026-08-05] 409 를 성공 안내로 종료하는 것은 정본 권고안 — 사용자가 보기엔
        # "찜하려던 게 이미 찜해 있다"는 실패가 아니라 원하는 상태에 도달한 것이다.
        yield sse(
            "action",
            ActionData(type="WISHLIST_ADDED", message="이미 찜해 두셨어요.").model_dump(
                by_alias=True
            ),
        )
    except WishlistProductNotFound:
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_ADD_FAILED",
                message="해당 상품을 찾지 못했어요.",
                reason="PRODUCT_NOT_FOUND",
            ).model_dump(by_alias=True),
        )
    except (WishlistError, SpringUnavailableError):
        # 기본 어댑터 add_wishlist(I-26)는 실패를 전부 WishlistError 로 낸다 — SpringUnavailableError 는
        # 같은 파일 stream_wishlist_remove 의 get_wishlist(I-28 조회) 처리 규약이지 이 경로의 규약은
        # 아니다. 그래도 함께 잡는 이유는 add_wishlist_fn 이 주입 가능한 인자라서다 — 주입 구현이 그
        # 예외를 내면(평가 하네스 degrade 주입이 그렇다) 이 except 없이는 상위 스트림 pump 의 범용
        # catch-all(INTERNAL)로 샌다. 형제 cart_add(graph.py::stream_cart_add)도 어댑터가 내지 않는 이 예외를 같은
        # 이유로 튜플에 방어해 둔다.
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_ADD_FAILED",
                message="찜하지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="WISHLIST_ERROR",
            ).model_dump(by_alias=True),
        )
    else:
        # 경로 B — 이벤트에 productId 를 싣지 않는다. `ActionData.cart_item_id` 는 cart_item.id
        # 전용 필드다 — 여기 productId 를 넣으면 다른 자원의 id 를 그 필드로 흘리는 것이라
        # 절대 하지 않는다.
        yield sse(
            "action",
            ActionData(type="WISHLIST_ADDED", message="찜해 뒀어요.").model_dump(by_alias=True),
        )
    yield _done()


async def stream_wishlist_remove(
    *,
    identity,
    cart: CartIntent,
    message: str,
    settings,
    get_wishlist_fn=None,
    remove_wishlist_fn=None,
    observer=None,
    screen_reference_attempted: bool = False,
    screen_resolved: bool = False,
) -> AsyncIterator[str]:
    """찜 해제 서브그래프(I-27, 확정 2026-08-05). 회원 아니면 `stream_wishlist_add` 와 같은 degrade.

    `screen_reference_attempted`·`screen_resolved`(#440 라운드 4 리뷰 F12, 라운드 10 리뷰 F27
    로 원자 두 개로 분리) — 화면 위치·순번을 발화가 가리키려 시도했는지, 해소기가 실제로
    상품을 확정했는지를 각각 넘긴다. `_resolve_wishlist_remove_target` 로 그대로 전달해 규칙
    2·3 자동 선택을 추가로 막는다(규칙 1 이름 매칭은 무관 — 그 함수 docstring 참조). 기본값
    `False`(위치 미언급)라 화면이 없는 호출부(예: `cart/graph.py` 2선 방어 위임)는 오늘과
    동일하게 동작한다."""
    get_wishlist_fn = get_wishlist_fn or spring_client.get_wishlist
    remove_wishlist_fn = remove_wishlist_fn or spring_client.remove_wishlist

    user_id, _guest_id = cart_identity(identity)
    if user_id is None:
        yield sse(
            "token", TokenData(text="찜 해제에는 로그인이 필요해요.").model_dump(by_alias=True)
        )
        yield _done()
        return

    try:
        wishlist_view = await get_wishlist_fn(user_id)
    except SpringUnavailableError:
        # I-28 어댑터는 4xx/5xx·도달 불가·스키마 불일치를 전부 SpringUnavailableError 로 낸다
        # (get_cart 와 같은 degrade 규약) — WishlistError 만 잡으면 이 실패를 하나도 못 잡는다.
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_REMOVE_FAILED",
                message="찜 목록을 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="WISHLIST_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    items = list(wishlist_view.items)
    if not items:
        yield sse("token", TokenData(text="찜한 상품이 없어요.").model_dump(by_alias=True))
        yield _done()
        return

    target = _resolve_wishlist_remove_target(
        cart,
        message,
        items,
        settings,
        screen_reference_attempted=screen_reference_attempted,
        screen_resolved=screen_resolved,
    )
    if target is None:
        yield sse(
            "token",
            TokenData(text=_wishlist_unresolved_notice(items)).model_dump(by_alias=True),
        )
        yield _done()
        return

    name = _display_wishlist_name(target)
    try:
        await remove_wishlist_fn(target.product_id, user_id=user_id)
    except WishlistNotFound:
        # [확정 2026-08-05] 404 를 성공 안내로 종료하는 것은 정본 권고안.
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_REMOVED", message=f"이미 찜 목록에 없어요: {name}"
            ).model_dump(by_alias=True),
        )
    except WishlistError:
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_REMOVE_FAILED",
                message=f"빼지 못했어요: {name}. 잠시 후 다시 시도해 주세요.",
                reason="WISHLIST_ERROR",
            ).model_dump(by_alias=True),
        )
    else:
        # 어순은 라운드 3(F-3)에서 정한 "{동작}: {이름}" 규약과 통일한다(조사 없이 자연스럽게).
        yield sse(
            "action",
            ActionData(type="WISHLIST_REMOVED", message=f"찜 목록에서 뺐어요: {name}").model_dump(
                by_alias=True
            ),
        )
    yield _done()
