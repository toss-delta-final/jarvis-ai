"""`cart_add` 로 라우팅된 발화의 삭제·찜 오담기 방어 (이슈 #116·#117, 패킷 §4).

decompose(app/agents/buyer/recommendation/decompose.py)는 다른 이슈(#84) 소유라 이 레인에서
`cart_remove`/`wishlist_add`/`wishlist_remove` intent 를 새로 만들 수 없다. 대신 `cart_add` 로
이미 들어온 발화 중 **명백한** 것만 결정론적으로 갈라낸다 — LLM 을 새로 부르지 않고, 프롬프트도
고치지 않는다. `cart_add` 로 라우팅되지 않는 발화("장바구니에서 빼줘"가 보통 가는 `cart_view`류)
는 이 판별기가 구조적으로 보지 못한다(결함이 아니라 경계, decompose intent 신설은 #84 몫).
"""

from __future__ import annotations


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    """겹치는 경우까지 needle 의 모든 [start, end) 출현 구간을 돌려준다.

    `app/agents/buyer/cart/graph.py::_all_spans` 와 동일한 구현이지만 복제했다 — `graph.py` 가
    이 모듈(`classify_cart_utterance`)을 가져오므로, 여기서 `graph.py` 를 가져오면 순환
    임포트가 된다. 10줄짜리 순수 함수를 위해 신원 도출처럼 별도 공유 모듈을 새로 두는 것도
    과하다고 판단해 그냥 복제를 택했다.
    """
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while (index := text.find(needle, start)) >= 0:
        spans.append((index, index + len(needle)))
        start = index + 1
    return spans


def _has_prefix_negation(message: str, start: int, prefix_markers: list[str]) -> bool:
    """`start` 위치에서 시작하는 표지 바로 **앞**에 접두 부정("안"/"못")이 어절 경계로 오는지
    본다(라운드 9 — 부정 검사가 어미형(뒤쪽, `utterance_negation_markers`)만 보고 접두형(안·못,
    앞쪽)은 놓쳤다. "안 빼줘도 돼"에서 실제로 항목이 삭제되는 사고로 재현됐다).

    한국어 부정은 어미(`-지 마`)뿐 아니라 부사 접두(`안`·`못`)로도 온다. `"안"`은 극히 흔한
    조각이라(`안경`·`안쪽`·`가방 안에`) 부분 문자열 검색을 그대로 쓰면 정상 발화를 대량으로
    삼킨다 — 그래서 **어절 경계**로만 판정한다: 표지 직전에 오는 `"안"`/`"못"` 이 (1) 앞이
    문자열 시작 또는 공백이고 (2) 뒤(표지와의 사이)가 공백 0~1개일 때만 접두 부정으로 친다.
    `"안 빼줘"`·`"안빼줘"`는 잡고 `"안경 빼줘"`는 안 잡는다(그 `"안"`은 `"안경"`의 일부라
    토큰이 아니다) — `"가방 안에 있는 거 빼줘"`처럼 표지에서 멀리 떨어진 `"안"`도 안 잡는다
    (직전 토큰만 본다).
    """
    for gap in (0, 1):
        anchor = start
        if gap == 1:
            if anchor == 0 or message[anchor - 1] != " ":
                continue
            anchor -= 1
        for marker in prefix_markers:
            prefix_start = anchor - len(marker)
            if prefix_start < 0 or message[prefix_start:anchor] != marker:
                continue
            if prefix_start == 0 or message[prefix_start - 1] == " ":
                return True
    return False


def _matches_unnegated(
    message: str,
    markers: list[str],
    negation_markers,
    window: int,
    prefix_negation_markers,
) -> bool:
    """`markers` 중 하나가 발화에 있고, 그 출현 중 **하나라도** 뒤쪽 부정·유보 표지(`window`
    자 안)와 앞쪽 접두 부정(`_has_prefix_negation`) 어느 쪽에도 안 걸리면 True 다(2차 리뷰
    지적 1·2·3 + 라운드 9의 공통 원인 — 표지가 있기만 하면 매칭했지 그 표지가 부정·유보된
    문맥인지 앞뒤 어느 쪽도 보지 않았다).

    같은 표지가 여러 번 나오면 **부정되지 않은 출현이 하나라도** 있어야 매칭이다 — "이건
    찜해줘, 장바구니에 넣지는 마, 진짜 장바구니에 넣어줘"처럼 뒤에서 다시 긍정으로 반복될
    수 있어 첫 출현만 보면 오탐(과소 매칭)한다.
    """
    for marker in markers:
        for start, end in _spans(message, marker):
            following = message[end : end + window]
            if any(neg in following for neg in negation_markers):
                continue
            if _has_prefix_negation(message, start, prefix_negation_markers):
                continue
            return True
    return False


def _matches_cart_add_marker(message: str, settings) -> bool:
    """`cart_add_markers` 매칭 — 부정(뒤쪽 어미·앞쪽 접두, `_matches_unnegated` 와 같은 원리)에
    더해 과거 참조형(2차 리뷰 N-1)도 배제한다.

    "담아"는 "담아뒀던"·"담아둔"처럼 과거 참조형에도 부분 문자열로 걸린다 — 이 PR 에서
    `docs/lessons.md` 에 적은 "전부" ⊂ "전부터" 실수의 재발이다. `wishlist_reference_markers`
    가 "찜한"을 지시 수식어로 다루는 것과 같은 개념으로, 표지 바로 뒤 짧은 창에 과거 참조 꼬리
    (`cart_add_reference_markers`)가 오면 그 출현은 동작 요청이 아니므로 세지 않는다. 부정
    검사와 같은 창 기계(`_spans` + 창 슬라이스)를 재사용하되, 부정·과거 참조는 서로 다른
    개념이라 한 목록으로 합치지 않고 별도 배제로 둔다(표지가 왜 안 걸렸는지 진단하기 쉽다).
    """
    negation_markers = settings.utterance_negation_markers
    prefix_negation_markers = settings.utterance_prefix_negation_markers
    reference_markers = settings.cart_add_reference_markers
    window = settings.utterance_negation_window
    for marker in settings.cart_add_markers:
        for start, end in _spans(message, marker):
            following = message[end : end + window]
            if any(neg in following for neg in negation_markers):
                continue
            if _has_prefix_negation(message, start, prefix_negation_markers):
                continue
            if any(ref in following for ref in reference_markers):
                continue
            return True
    return False


def classify_cart_utterance(message: str, settings) -> str:
    """'cart_add' | 'cart_remove' | 'wishlist_add' | 'wishlist_remove' — 확실할 때만 갈라낸다.

    기본값은 항상 `"cart_add"`(= 오늘 동작). 놓치는 것은 무해하고(오늘처럼 담긴다), 오탐하면
    사용자가 요청하지 않은 동작(담기 취소·찜)이 일어나므로 "확실할 때만 개입"한다
    (docs/lessons.md — 강한 신호는 약한 신호로 덮지 않는다, 양보는 앞단 early return 으로).

    **부정·유보 표지(2차 리뷰 지적 1·2·3)**: 모든 표지 매칭은 `_matches_unnegated` 를 거친다 —
    표지 출현 바로 뒤 짧은 창(`utterance_negation_window`) 안에 `utterance_negation_markers`
    (지 마·지는 마·지마·하지 마·말고·야 할·야 될)가 오면 그 출현은 없는 것으로 친다
    ("이건 찜해줘, 장바구니에 넣지는 마" → 담기 표지가 부정돼 무효화되고 찜 추가로 간다,
    "장바구니에서 빼줘야 할까?" → 삭제 표지가 유보돼 무효화되고 기본값 담기로 남는다).
    개별 케이스를 특례로 막지 않고 이 한 규칙으로 처리한다 — 넓게 잡으면 개입을 **줄이는**
    방향이라 오탐해도 "오늘 동작으로 되돌아갈" 뿐이다.

    **접두 부정(라운드 9)**: 위 부정 검사는 표지 **뒤**(어미형, `-지 마`)만 봤다. 한국어 부정은
    부사 **접두**(`안`·`못`)로도 오는데("안 빼줘도 돼") 그쪽이 빠져 있어 실제로 항목이
    삭제됐다. `_matches_unnegated`(모든 표지 계열 공통)와 `_matches_cart_add_marker` 둘 다
    표지 **앞**도 `_has_prefix_negation` 으로 검사한다 — `utterance_negation_markers` 와
    합치지 않는다(검사 방향이 반대라 한 목록으로 두면 코드가 헷갈린다). `"안"`은 흔한 조각이라
    어절 경계로만 판정해 `"안경 빼줘"`·`"가방 안에 있는 거 빼줘"` 같은 정상 요청은 죽이지
    않는다(표지 앞에 함수 docstring 참조).

    **담기 표지의 과거 참조형(2차 리뷰 N-1)**: `cart_add_markers` 매칭은 `_matches_unnegated`
    대신 `_matches_cart_add_marker` 를 쓴다 — 부정 배제에 더해 과거 참조 꼬리
    (`cart_add_reference_markers`: 뒀·둔·두었·놨·놓)도 같은 창에서 배제한다. "담아"는
    "담아뒀던"·"담아둔"처럼 과거 참조형에도 부분 문자열로 걸려("담아뒀던 거 다 빼줘"가 삭제
    대신 담기로 오담기), 그 출현은 지금 담아 달라는 요청이 아니므로 표지로 세지 않는다.

    판정 순서:
      0-a. `cart_add_markers`(담아·장바구니에 넣)가 있으면 즉시 `"cart_add"`. 담기는 이 판별기가
           다루는 신호 중 가장 강하다 — "찜한 거 장바구니에 담아줘"·"하나 빼고 담아줘"처럼 찜/삭제
           표지처럼 보이는 조각과 같은 발화에 있어도 담기가 이긴다. "장바구니에서 빼줘"는 이
           표지에 걸리지 않는다("장바구니에 넣"과 다른 문자열이라 오탐이 아니다) — 그래서
           삭제 판정(0-a 다음 단계)까지 내려갈 수 있다. "담아뒀던 거 다 빼줘"·"담아둔 이어폰
           찜 취소해줘"류도 과거 참조형 배제로 이 표지에 걸리지 않는다(바로 위 문단 참조).
      1. `wishlist_remove_markers` 매칭 → `"wishlist_remove"`. **`cart_remove_markers` 보다
         먼저 본다** — "찜 빼줘"는 "빼줘"(삭제 표지)도 부분 문자열로 동시에 매칭하는데, 찜
         해제를 삭제보다 먼저 확정해야 "찜 빼줘"가 `cart_remove`로 새지 않는다. **`"장바구니"`
         억제(2번 참조)는 이 단계에 걸리지 않는다** — `wishlist_remove_markers` 는 전부 `"찜"`이
         붙은 명시적 동작 구라 `"장바구니"`가 같이 나와도 혼동 여지가 없다(2차 리뷰 지적 8 —
         이전 코드는 이 단계에도 억제를 걸어 "찜 취소해줘, 장바구니는 그대로 두고"가 명시적
         해제 요청인데도 `cart_add` 로 떨어졌다). **`wishlist_reference_markers`(3번)보다도
         먼저 본다**(라운드 7 — 지적 1 잔여 케이스) — "찜한 거 찜 취소해줘"처럼 지시 수식어
         (`"찜한"`)와 명시적 동작 구(`"찜 취소해줘"`)가 한 발화에 같이 오면, 실제로 사용자가
         요구한 동작(동사)이 대상을 가리키기만 하는 수식어보다 강한 신호다("강한 신호는 약한
         신호로 덮지 않는다", docs/lessons.md) — 이전 순서(수식어 양보가 먼저)는 이 발화를
         `cart_add` 로 떨어뜨려 명시적 찜 해제 요청이 장바구니에 담기는 결과를 냈다.
      2. `wishlist_add_markers` 매칭 → `"wishlist_add"`. 단 발화에 `"장바구니"`가 있으면 찜으로
         가르지 않는다(계약상 찜·장바구니는 다른 자원이라 혼동 방지). 이 억제는 **여기(찜 추가)
         에만** 걸린다 — 1번(찜 해제)·4번(삭제)에는 영향 없다. 이 단계도 3번(`wishlist_reference_markers`)
         보다 먼저 본다 — 같은 "동사가 수식어보다 강하다" 원칙("찜한 거 찜해줘"류에도 동일 적용).
      3. `wishlist_reference_markers`(찜한·찜해둔·찜해 놓은·찜했던)가 있으면 `"cart_add"`.
           이 표지가 있는 발화의 동사는 (1·2번에서 명시적 찜 동작 표지가 안 걸렸다면) 담기다 —
           찜은 지시 대상을 수식할 뿐이다("찜해둔 이어폰 담아줘"는 0-a 가 이미 잡는다. 담기
           표지 없이 지시만 있는 경우("찜해둔 거")를 방어하려고 이 단계가 따로 있다).
           **알려진 거짓음성(라운드 2 리뷰, 의도한 보수성 — 넓히지 말 것)**: "찜한 거 빼줘"·
           "찜해둔 거 지워줘"는 여기서 `"cart_add"` 로 떨어진다 — `"빼줘"`·`"지워줘"` 는
           `cart_remove_markers`(4번)에 속해 1·2번(찜의 명시적 동작 표지)이 못 잡고, 그 발화가
           "장바구니에서 빼라"인지 "찜을 풀어라"인지는 이 표지만으로 결정론적으로 갈릴 수 없어
           애매하면 개입하지 않는다 — 놓치는 결함이 아니라 설계한 보수성이다. (라운드 7 이전엔
           "찜한 거 찜 취소해줘"도 여기서 같은 이유로 거짓음성 처리됐으나, 1번이 그 표지를
           명시적으로 담고 있어 순서를 옮겨 해소했다 — 위 1번 참조.)
      4. `cart_remove_markers` 매칭 → `"cart_remove"`.
      5. 그 외 → `"cart_add"`(기본값).
    """
    negation_markers = settings.utterance_negation_markers
    prefix_negation_markers = settings.utterance_prefix_negation_markers
    window = settings.utterance_negation_window

    if _matches_cart_add_marker(message, settings):
        return "cart_add"

    if _matches_unnegated(
        message,
        settings.wishlist_remove_markers,
        negation_markers,
        window,
        prefix_negation_markers,
    ):
        return "wishlist_remove"

    # "장바구니" 자체도 부정 문맥 검사를 거친다 — "장바구니에 넣지는 마"처럼 그 언급이 부정된
    # 절 안에 있으면(cart_add_markers 가 이미 무효화된 바로 그 절) 억제 근거로 쓰지 않는다.
    # 그러지 않으면 이 억제가 방금 위에서 부정한 담기 표지를 "장바구니 언급이 있다"는 이유로
    # 다시 살려 wishlist_add 를 막아버린다(2차 리뷰 지적 1 재현 케이스).
    suppress_wishlist_add = _matches_unnegated(
        message, ["장바구니"], negation_markers, window, prefix_negation_markers
    )
    if not suppress_wishlist_add and _matches_unnegated(
        message,
        settings.wishlist_add_markers,
        negation_markers,
        window,
        prefix_negation_markers,
    ):
        return "wishlist_add"

    # 명시적 찜 동작 표지(1·2번)가 안 걸렸을 때만 지시 수식어 양보로 내려온다(라운드 7 —
    # 동사가 수식어보다 강한 신호라는 원칙을 순서로 강제한다).
    if _matches_unnegated(
        message,
        settings.wishlist_reference_markers,
        negation_markers,
        window,
        prefix_negation_markers,
    ):
        return "cart_add"

    if _matches_unnegated(
        message, settings.cart_remove_markers, negation_markers, window, prefix_negation_markers
    ):
        return "cart_remove"
    return "cart_add"
