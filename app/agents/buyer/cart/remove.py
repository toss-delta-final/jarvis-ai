"""장바구니 삭제 서브그래프 (이슈 #116, I-24 — 확정 2026-08-05, Spring 구현됨).

**[#285]** BE `jarvis-backend` main 실측(2026-08-08, BE PR #92·#93) — api-spec §4.12~4.16
v0.31.3. 이 파일에 있던 "Spring 구현 진행 중" 표기는 그래서 지웠다. 같은 문구가 `config.py`·
`spring_client.py`·`schemas/spring.py` 등에도 남아 있었는데, 이 배치가 함께 정정한다
(`wishlist.py` 와 구조·어조를 맞춘 형제 모듈이다).

`stream_cart_add` 가 `classify_cart_utterance` 로 "cart_remove" 로 판정하면 항상 위임받는다
(패킷 §5.3·§5.4, 라운드 23 — 온/오프를 가리던 설정 필드 제거). 대상 해소는 결정론적이다 —
LLM 을 새로 부르지 않는다. 복수 삭제는 항목별 반복 호출이다(I-24 에 bulk 가 없다).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import inspect
import logging

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.negation import has_any_negation, matches_name_unnegated
from app.agents.buyer.cart.purchase_state import state_suffix
from app.agents.buyer.cart.state import CartStateStore, LastAdd
from app.core.text import _strip_unsafe
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import CartViewItem
from app.services import spring_client
from app.services.spring_client import CartError, CartItemNotFound, SpringUnavailableError

_log = logging.getLogger(__name__)


def _accepts_chat_session_id(delete_fn: object) -> bool:
    """I-24 확장 전 시그니처의 주입 fake도 계속 쓸 수 있게 한다.

    프로덕션 Spring client는 ``chat_session_id``를 받는다. 다만 오래된 단위 테스트/호출자
    double은 해당 keyword가 없을 수 있다. 시그니처를 확인할 수 없으면 실제 클라이언트를
    막지 않도록 전달을 택한다.
    """
    try:
        parameters = inspect.signature(delete_fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "chat_session_id" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


def _display_name(item: CartViewItem) -> str:
    """안내 문구용 상품명 — 반드시 `_strip_unsafe` 를 거친다(판매자 입력, §5.3 항목 6)."""
    return _strip_unsafe(item.product_name or "") or "상품"


def _display_name_with_option(item: CartViewItem) -> str:
    """되물음 문구용 상품명 — 옵션이 있으면 구분자를 붙인다.

    `product_name` 이 같고 `option_name` 만 다른 항목이 섞이면 `_display_name` 만으로는 되물음
    문구가 "이어폰, 이어폰"처럼 똑같이 나와 사용자가 어느 쪽을 가리켜도 구분할 방법이 없다.
    `app/agents/buyer/cart/graph.py::stream_cart_view` 가 이미 쓰는 `f"{product_name}
    ({option_name})"` 표기를 그대로 따른다 — 새 형식을 발명하지 않는다. `_resolve_remove_targets`
    가 이름 매칭 후보에 더하는 구분자 형태(공백·괄호 유무 변형)와 짝을 이뤄, 이 문구가 예시로
    든 대로 사용자가 답하면 실제로 좁혀진다.
    """
    name = _display_name(item)
    option = _strip_unsafe(item.option_name) if item.option_name else ""
    return f"{name} ({option})" if option else name


def _resolve_remove_targets(
    message: str,
    items: list[CartViewItem],
    settings,
    last_add: LastAdd | None,
) -> list[CartViewItem] | None:
    """삭제 대상을 결정론적으로 해소한다 — 강한 신호부터 순서대로 시도하고, 결과가 나오면 그
    자리에서 확정한다(다음 규칙으로 넘어가지 않는다). 어느 규칙도 못 잡으면 `None`(호출부가
    현재 담긴 상품을 나열하며 되묻는다).

    순서(패킷 §5.3, 2차 리뷰 지적 2·3 + 라운드 10 이후):
      1. 발화에 부정·대조 표지(`negation.has_any_negation` — 어미형(`utterance_negation_markers`)
         과 접두형(`utterance_prefix_negation_markers`, "안"·"못") 둘 다 본다, **문장 어디든** —
         이 층은 위치 창이 아니라 전체 검사다, `intent_guard.py` 의 근처 창 검사와 다르다)가
         **없을 때만** 전체 표지(`cart_remove_all_markers`) → 전 항목. "전체 삭제"·"방금 담은 거"는 사용자가
         이름을 대지 않은 대상을 **코드가 고르는** 규칙이라 다른 규칙보다 엄격해야 한다 — "전부
         빼지는 말고 이어폰만 빼줘"에서 "전부 빼"만 보고 확정하면 사용자가 명시적으로 배제한
         전체 삭제가 실행된다. 대조어("말고")가 표지에서 여러 글자 떨어져 나오므로 위치 창이
         아니라 문장 전체를 본다.
      2. 상품명이 발화에 **부분 문자열로**, 그리고 **부정되지 않은 출현으로** 등장 → 정확히
         1건이면 그 항목. 2건 이상이면 어느 쪽인지 결정할 수 없어 **그 자리에서 되물음**(3·4번
         으로 넘어가지 않는다 — 이미 "이름을 지목했다"는 강한 신호가 확보됐는데 모호하면, 더
         약한 신호로 임의로 골라잡는 것이 더 위험하다). 이름은 `_strip_unsafe` 로 정제한 값으로
         매칭한다 — product_name 은 판매자 입력이라 제어·zero-width·bidi 문자가 섞일 수 있고,
         원문 그대로 매칭하면 (a) 육안상 똑같아 보이는 이름이 안 보이는 문자 때문에 조용히
         매칭 실패하거나 (b) 반대로 그 안 보이는 문자를 이용해 매칭을 조작하는 표면이 생긴다.
         발화(`message`) 쪽은 사용자 입력이고 이미 API 경계에서 정제된다는 전제라 여기서 다시
         정제하지 않는다(`_pending_switch_signals` 와 같은 전제). **[라운드 11]** 부정 판정은
         1·3번의 문장 전체 가드(`has_any_negation`)가 아니라 `negation.matches_unnegated` 의
         **출현 단위** 판정을 쓴다 — "이어폰은 빼지 말고 케이스 빼줘"에서 문장 전체 가드를
         쓰면 정상 삭제 대상인 "케이스"까지 함께 죽어 되물음이 된다. 이름별로 발화에 나온 각
         출현의 앞뒤(접두·어미 부정)를 보고, **부정되지 않은 출현이 하나라도 있는 이름만**
         매칭으로 친다 — "이어폰"은 뒤에 "말고"가 붙어 그 출현이 무효화되지만 "케이스"는
         부정 없이 살아남는다.
      3. 부정·대조 표지가 **없고 이름을 대려는 시도도 없을 때만**(1번·4번과 같은
         `name_mentioned` 가드) "방금 담은 거" 표지(`cart_remove_recent_markers`) →
         `last_add.cart_item_id`. "방금 담은 거 말고 다른 거 빼줘"처럼 사용자가 **제외한** 바로
         그 상품을 코드가 고르면 안 된다 — 이름이 따로 없으므로 안전한 동작은 되물음이다(1번과
         같은 이유로 문장 전체 검사). "이어폰케이스 방금 담은 거 빼줘"처럼 이름과 표지가 같은
         발화에 있어도 마찬가지다 — 사용자가 댄 이름과 무관한 최근 담긴 항목을 대신 지우면
         안 된다(1번 "이어폰 전부 빼줘"가 이름 있는 발화에서 전체 삭제로 새지 않는 것과 같은
         이유). 표지가 있고 부정·이름 언급도 없는데 그 항목이 지금 장바구니에 없으면(이미
         빠졌거나 다른 세션에서 지워짐) **그 자리에서 되물음**(4번 "단건 자동"으로 넘어가지
         않는다 — 사용자가 명시적으로 "방금 그거"를 지목했는데 실패하면, 장바구니에 다른 게
         1건 있다고 그걸 대신 지우는 것은 사용자 의도와 다를 수 있다).
      4. 부정·대조 표지가 **없고**, 발화에 어느 항목의 이름도 (경계와 무관하게) **아예 등장하지
         않을 때만** 장바구니에 항목이 정확히 1건이면 그 1건(표지도 이름도 없는 발화에서만 적용
         — 표지가 있었는데 해소에 실패한 경우는 2·3번이 이미 되물음으로 종결했다).
      5. 그 외 → `None`.

    **[라운드 10]** 이 함수의 부정 판정은 원래 어미형(`utterance_negation_markers`)만 봤다 —
    `intent_guard.py` 가 라운드 9 에서 접두형("안"·"못")을 배울 때 이 함수는 그대로 남아,
    "방금 담은 건 안 빼도 되고, 저번에 산 것도 빼줘"류가 `cart_remove` 로 라우팅된 뒤 여기서
    사용자가 명시적으로 "빼지 말라"고 한 항목이 실제로 삭제됐다(플래그 on 시 데이터 손실).
    같은 부정 개념이 두 파일에 각자 구현돼 한쪽만 고쳐진 것이 원인이라, 판정을
    `negation.has_any_negation` 공용 함수로 옮겨 `intent_guard.py` 와 같은 함수를 쓰게 했다.

    **[라운드 11]** 라운드 10 은 1·3·4번(전체 삭제·방금 담은 거·단건 자동)에 문장 전체 가드를
    붙였지만 **가장 강한 신호인 2번(이름 매칭)은 그대로 남겨** "이어폰은 빼지 말고 케이스
    빼줘"에서 이름이 지목된 이어폰이 그대로 매칭돼 삭제됐다. 2번은 (라운드 15 이전엔)
    `negation.matches_unnegated`(출현 단위 — `negation.py` 에 이미 있던 함수, 새로 만들지
    않았다)를 썼다.

    **[라운드 15, head `0b33e06` 리뷰 B]** 2번의 이름 매칭에 **경계 검사**가 없어 "이어폰케이스
    빼줘"(장바구니에 이어폰만 있음)에서 다른 낱말 안에 파묻힌 "이어폰"까지 매칭됐다. 순진한
    "앞뒤가 공백/문장부호가 아니면 무효" 규칙은 "그 세제**를** 빼줘"(목적격 조사가 이름 바로
    뒤에 붙는 정상 발화)를 깨뜨리므로, 오른쪽 경계에 한국어 조사를 허용하는
    `negation.matches_name_unnegated` 를 새로 만들어 쓴다(기존 `matches_unnegated` 에 합치지
    않은 이유는 그 함수 정의를 보라 — 동사구 매칭과 이름 매칭은 경계 개념이 다르다).

    **[라운드 17, head `6ab47c9` 리뷰]** 라운드 15 는 "이름 뒤가 공백이면(문장부호도 조사도
    아니어도) 여전히 매칭"되는 문제를 "덜 친절한 문구"급 알려진 한계로 미뤘는데, 그 판단이
    틀렸다 — "이어폰 케이스 빼줘"(장바구니에 이어폰만 있음)는 사용자가 요청하지 않은
    "이어폰"이 확인 없이 삭제되는 **파괴적 동작**이라, 이 판별기 전체가 막으려던 바로 그
    클래스다. `matches_name_unnegated` 의 오른쪽 경계를 "이름 + (조사) + (filler) + 표지"
    형태만 인정하도록 전면 개정했다(구체적인 조사·filler 소비 순서와 `other_names` 로 다른
    항목 이름 나열까지 인정하는 이유는 그 함수 정의를 보라 — 이름 매칭 로직 자체는 여기서
    새로 만들지 않고 그 공용 함수를 그대로 쓴다). 이제 남는 진짜 한계는: filler 목록이나
    표지 목록에 없는 낯선 수식어가 이름과 표지 사이에 오면("이어폰 저거 빼줘"처럼) 여전히
    매칭에서 제외될 수 있다는 정도이고, 이건 "친절함"의 문제이지 "파괴적 오삭제"가 아니다.

    경계 검사가 2번을 정확하게 만들어도(=이제 "이어폰케이스"의 "이어폰"을 매칭에서 뺀다),
    장바구니가 1건뿐이면 4번이 표지 없이도 그 1건을 자동으로 고르므로 **결과가 그대로 같아질
    수 있다**(이름을 댔는데 아무것도 안 맞았어도 "1건뿐이니 그거겠지"로 밀어붙이는 셈) —
    boundary 수정만으로는 안 끝난다는 뜻이라 4번에도 손을 댔다: 어느 항목 이름이든 (경계·부정과
    무관하게) 발화에 **원문 부분 문자열로라도** 나타나면 "이름을 대려는 시도가 있었다"로 보고
    4번을 건너뛴다(추측성 휴리스틱이 아니다 — 새 신호를 만들지 않고, 이미 계산해 둔 부분
    문자열 겹침을 그대로 재사용한다). "이어폰케이스 빼줘"(장바구니 `[이어폰]`)는 "이어폰"이
    원문에 나타나므로 4번을 건너뛰어 되물음이 된다. "그거 지워줘"처럼 어느 이름도 원문에 없으면
    그대로 4번이 동작한다(회귀 없음).

    **[라운드 18, head `f87eb5b` 리뷰 F1]** 라운드 17 은 `name_mentioned` 가드를 4번(단건 자동)
    에만 달았는데, 정작 이 함수에서 가장 파괴적인 규칙은 1번(전체 삭제)이다 — "이어폰 전부
    빼줘"(장바구니 `[이어폰, 파우치]`)에서 "전부 빼"만 보고 1번이 확정하면, 사용자가 이름을
    댔는데도 이름을 대지 않은 "파우치"까지 함께 지워진다. 새 판정을 만들지 않고 `name_mentioned`
    계산을 함수 앞부분(1번보다 먼저)으로 끌어올려 1번에도 재사용한다: 발화에 어느 항목 이름이든
    (경계·부정과 무관하게) 원문 부분 문자열로 나타나면 1번을 건너뛰고 2번(이름 매칭)으로
    내려간다. "이어폰 전부 빼줘"는 2번에서 "이어폰" 뒤가 "전부"라 라운드 17 경계 규칙상 유효
    매칭이 아니므로 모호로 떨어져 되물음이 된다(전체 삭제가 아니면 충분 — 되물음 문구 자체를
    개선하는 것은 이 라운드의 범위가 아니다). "전부 빼줘"·"다 빼줘"·"모두 빼줘"처럼 어느 이름도
    원문에 없으면 `name_mentioned` 가 거짓이라 1번이 그대로 동작한다(회귀 없음).
    """
    has_negation = has_any_negation(
        message, settings.utterance_negation_markers, settings.utterance_prefix_negation_markers
    )

    # 라운드 18 — 4번(단건 자동)만 쓰던 계산을 앞으로 끌어올려 1번(전체 삭제)에도 재사용한다
    # (새 신호 아님, 라운드 17 그대로).
    name_mentioned = any(
        (name := _strip_unsafe(item.product_name or "")) and name in message for item in items
    )

    if (
        not has_negation
        and not name_mentioned
        and any(marker in message for marker in settings.cart_remove_all_markers)
    ):
        return list(items)

    all_names = [name for item in items if (name := _strip_unsafe(item.product_name or ""))]

    def _matches_any(candidates: list[str]) -> bool:
        return any(
            matches_name_unnegated(
                message,
                candidate,
                settings.utterance_negation_markers,
                settings.utterance_negation_window,
                settings.utterance_prefix_negation_markers,
                settings.utterance_name_boundary_particles,
                settings.utterance_name_trailing_filler_words,
                settings.cart_remove_markers,
                all_names,
            )
            for candidate in candidates
        )

    # product_name 이 같고 option_name 만 다른 항목(예: 이어폰(블랙)·이어폰(화이트))은
    # 맨이름만으로 영원히 모호하다 — "이어폰 빼줘" → 되물음 → 되물음 문구가 예시로 든 맨이름을
    # 사용자가 그대로 반복해도 다시 2건이라 되물음이 끝나지 않는다(되물음이 상태를 저장하지
    # 않으므로 "다시 말해 달라"가 유일한 탈출구인데, 맨이름으로는 그 탈출구 자체가 막다른
    # 길이 된다). `option_name` 을 담은 구분자 형태(`_unresolved_notice` 가 예시로 드는 표기와
    # 같다)를 이름 매칭 후보에 추가한다 — 경계·부정 판정은 기존 `matches_name_unnegated` 를
    # 그대로 재사용하고 후보 문자열만 늘린다(새 판정 로직을 만들지 않는다).
    # 구분자 형태로 **유일하게** 좁혀지면 그 강한 신호를 맨이름의 모호한 매칭보다 우선한다 —
    # 그러지 않으면 "이어폰(블랙) 빼줘"도 여전히 "이어폰"이 두 항목 모두에 걸려 모호 판정으로
    # 되돌아가 루프가 안 끊긴다.
    candidates_by_item: list[tuple[CartViewItem, list[str], list[str]]] = []
    for item in items:
        base = _strip_unsafe(item.product_name or "")
        if not base:
            continue
        option = _strip_unsafe(item.option_name) if item.option_name else ""
        qualified = (
            [f"{base} ({option})", f"{base}({option})", f"{base} {option}"] if option else []
        )
        candidates_by_item.append((item, [base, *qualified], qualified))

    qualified_matches = [
        item for item, _, qualified in candidates_by_item if qualified and _matches_any(qualified)
    ]
    if len(qualified_matches) == 1:
        return qualified_matches

    name_matches = [item for item, candidates, _ in candidates_by_item if _matches_any(candidates)]
    if name_matches:
        return name_matches if len(name_matches) == 1 else None

    # 이름을 대려는 시도가 있었으면(경계·부정과 무관하게 원문에 어느 항목 이름이든 등장) 이
    # 규칙을 건너뛴다 — "방금 담은 거"는 1번(전체 삭제)·4번(단건 자동)과 같은 성격의 규칙이다:
    # 사용자가 이름을 대지 않은 대상을 코드가 고른다. "이어폰케이스 방금 담은 거 빼줘"에서
    # "방금"이 매칭돼도 이름을 대려는 시도가 있었다면, last_add 가 가리키는 무관한 항목을
    # 대신 지우면 안 된다 — 다른 두 규칙과 같은 가드(`name_mentioned`, 함수 앞부분에서 계산).
    if (
        not has_negation
        and not name_mentioned
        and any(marker in message for marker in settings.cart_remove_recent_markers)
    ):
        if last_add is not None:
            recent_match = [item for item in items if item.cart_item_id == last_add.cart_item_id]
            if recent_match:
                return recent_match
        return None

    # 라운드 15 — 2번의 경계 검사가 이름 매칭을 정확히 만들어도, 장바구니 1건뿐이면 아래 단건
    # 자동이 표지 없이도 그 1건을 자동 선택해 같은 결과가 나올 수 있다("이어폰케이스 빼줘"가
    # "이어폰"에 boundary 로 안 걸려도 단건 자동으로 새 버린다). `name_mentioned` 는 함수 앞부분
    # (라운드 18, 1번과 공용)에서 이미 계산됐다 — 여기서 다시 계산하지 않는다.
    if not has_negation and not name_mentioned and len(items) == 1:
        return items
    return None


def _unresolved_notice(items: list[CartViewItem]) -> str:
    """되물음 문구 — 지금 담긴 상품명을 나열해 무엇을 물어야 할지 알려준다
    (`_UNRESOLVED_SCREEN_POSITION` 의 철학과 같음, graph.py 참조).

    **[라운드 19, head `26f5596` 리뷰]** 이 되물음은 **상태를 저장하지 않는다** — 옵션 되물음
    (`PendingAdd`, `CartStateStore`)과 달리 "삭제 대상을 되묻는 중"이라는 사실을 어디에도 남기지
    않는다. 사용자가 다음 턴에 상품명만("이어폰") 답하면 `classify_cart_utterance` 는 삭제
    표지가 없는 발화를 보고 기본값 `"cart_add"` 로 떨어져 이 흐름으로 돌아오지 못한다(직전
    추천에 그 상품이 있으면 오히려 담기로 새는 막다른 길이 된다). 다중 턴 pending-remove 상태를
    새로 도입하는 것은 이 레인의 범위 밖이다(패킷은 모호하면 "token 되물음"까지로 규정, 새
    저장소 키·`PendingAdd` 확장·`buyer/graph.py` 수정은 `PendingAdd` pending 정리 규칙과
    얽혀 새 결함 표면을 낳는 후속 이슈감이다) — 대신 문구가 사용자의 다음 답을 **판별기가 다시
    잡을 수 있는 형태**(동작 표지 포함)로 유도한다: "OO 빼줘"처럼 답하면 다음 턴이 삭제로
    정확히 라우팅되지만, 상품명만 답하면 여전히 담기로 샌다(알려진 한계 — 문구로 유도할 뿐 강제
    하지 않는다).

    상품명을 그대로 예시로 주는 것도 `product_name` 이 같고 `option_name` 만 다른 항목이 섞이면
    막다른 길이 된다 — "이어폰 빼줘" → 이 문구가 "이어폰"을 예시로 줌 → 사용자가 그대로 반복
    → 다시 2건 모호. `_display_name_with_option` 으로 옵션 구분자를 붙여(예: "이어폰 (블랙)")
    목록·예시 둘 다에 실으면, `_resolve_remove_targets` 가 그 구분자 형태를 이름 매칭 후보로
    인식하므로 예시대로 답한 다음 턴이 실제로 1건까지 좁혀진다.

    **[#310]** 목록에 구매 가능 상태 라벨을 함께 싣는다 — 안 붙이면 같은 장바구니가 질문 방식에
    따라 다르게 보인다("뭐 있어?"에는 (판매 종료)가 보이는데 "빼줘"에는 이름만). 삭제 문맥은
    판매 종료 항목을 빼도록 돕는 자리라 라벨이 가장 쓸모 있는 곳이기도 하다.
    """
    names = ", ".join(
        f"{_display_name_with_option(item)}{state_suffix(item.purchase_state)}" for item in items
    )
    # 예시는 "이렇게 답하세요"라고 권하는 자리라 **살 수 있는 항목**을 우선한다 — 첫 항목이
    # 품절이면 못 사는 상품을 예시로 들게 된다. 다만 못 사는 항목도 삭제 대상으로는 유효하므로
    # 전부 못 사는 경우엔 그대로 첫 항목을 쓴다(예시가 사라지는 쪽이 더 나쁘다).
    example_item = next(
        (item for item in items if item.purchase_state in (None, "AVAILABLE")), items[0]
    )
    example = _display_name_with_option(example_item)
    return (
        f"지금 장바구니에 있는 상품: {names}. 예) '{example} 빼줘'처럼 상품명과 함께 말씀해 주세요."
    )


async def stream_cart_remove(
    *,
    identity,
    message: str,
    cart_store: CartStateStore,
    thread_key: str,
    settings,
    get_cart_fn=None,
    delete_fn=None,
    observer=None,
    chat_session_id: str | None = None,
) -> AsyncIterator[str]:
    """삭제 서브그래프. 항목마다 `action`(CART_REMOVED/CART_REMOVE_FAILED)을 내고 `done` 1회로 끝난다."""
    get_cart_fn = get_cart_fn or spring_client.get_cart
    delete_fn = delete_fn or spring_client.delete_cart_item

    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "token",
            TokenData(text="장바구니 삭제에는 로그인이 필요해요.").model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
    except SpringUnavailableError:
        yield sse(
            "action",
            ActionData(
                type="CART_REMOVE_FAILED",
                message="장바구니를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    items = list(cart_view.items)
    if not items:
        yield sse("token", TokenData(text="장바구니가 비어 있어요.").model_dump(by_alias=True))
        yield _done()
        return

    try:
        last_add = await cart_store.get_last_add(thread_key)
    except Exception as exc:  # noqa: BLE001 - "방금 담은 거" 표지 없는 평범한 삭제도 이 읽기를
        # 항상 먼저 거친다 — 실패가 새면 첫 SSE 프레임도 나가기 전에 삭제 기능이 통째로
        # 죽는다(2차 리뷰 지적 6·2번). None 으로 degrade하면 _resolve_remove_targets 의
        # "방금 담은 거" 분기가 표지는 있어도 대상을 못 찾은 것과 같은 경로(되물음)로 자연히
        # 떨어진다 — 임의로 다른 상품을 고르지 않는다. CancelledError 는 BaseException 이라
        # 전파된다.
        _log.warning("last_add_read_failed", extra={"reason": str(exc)})
        last_add = None
    targets = _resolve_remove_targets(message, items, settings, last_add)
    if targets is None:
        yield sse("token", TokenData(text=_unresolved_notice(items)).model_dump(by_alias=True))
        yield _done()
        return

    for item in targets:
        name = _display_name(item)
        try:
            delete_kwargs = {"user_id": user_id, "guest_id": guest_id}
            if chat_session_id is not None and _accepts_chat_session_id(delete_fn):
                delete_kwargs["chat_session_id"] = chat_session_id
            await delete_fn(item.cart_item_id, **delete_kwargs)
        except CartItemNotFound:
            # [확정 2026-08-05] 404 를 성공 안내로 종료하는 것은 정본 권고안 — 사용자가 보기엔
            # "빼려던 게 이미 빠져 있다"는 실패가 아니라 원하는 상태에 도달한 것이다.
            yield sse(
                "action",
                ActionData(
                    type="CART_REMOVED",
                    message=f"이미 빠져 있어요: {name}",
                    cart_item_id=item.cart_item_id,
                ).model_dump(by_alias=True),
            )
        except CartError:
            # 한 항목이 실패해도 다른 항목은 계속 진행한다(실패 격리) — 여기서 return 하지 않는다.
            # [확정 2026-08-05] CART_REMOVE_FAILED 에 cartItemId 를 함께 싣는 확장안은 이미
            # 채택됐다(api-spec §3.1 예시). 복수 삭제에서 어느 항목이 실패했는지 가르려고 싣는다.
            yield sse(
                "action",
                ActionData(
                    type="CART_REMOVE_FAILED",
                    message=f"빼지 못했어요: {name}. 잠시 후 다시 시도해 주세요.",
                    cart_item_id=item.cart_item_id,
                    reason="CART_ERROR",
                ).model_dump(by_alias=True),
            )
        else:
            # 조사(을/를) 대신 어순으로 자연스럽게 만든다(라운드 3 리뷰 F-3) — 받침 유무에 따른
            # 조사 선택 로직을 새로 만들지 않기 위해 "{동작}: {이름}" 형태로 통일한다.
            yield sse(
                "action",
                ActionData(
                    type="CART_REMOVED",
                    message=f"장바구니에서 뺐어요: {name}",
                    cart_item_id=item.cart_item_id,
                ).model_dump(by_alias=True),
            )

    yield _done()
