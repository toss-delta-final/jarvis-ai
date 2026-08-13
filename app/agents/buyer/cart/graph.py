"""장바구니 서브그래프 스트리밍 (결정 7 / api-spec §4.1 담기·§4.9 조회, 이슈 #3).

담기: (상품·옵션·수량) 의도 확정 → add_to_cart(I-2, 단건) → SSE action.
      옵션 필수(CART_OPTION_REQUIRED)면 실패 action 없이 token 되물음 → pending 저장 →
      다음 턴 사용자 답을 optionId 로 해석해 재담기(§4.1 멀티턴). 단 후보가 1개뿐이면 되묻지
      않고 그 옵션으로 즉시 재담기한다(이슈 #114). 이번 발화 조건으로 후보가 정확히 1개로
      좁혀져도 마찬가지로 되묻지 않고 담는다(이슈 #455, I-1 options·optionCount 소비 —
      담기 권위는 여전히 I-2 이고 optionId 는 그 400 응답에서만 온다). 옵션이 안 좁혀지면
      승인된 색상 동의어 사전(#258/#505)으로 조건어·옵션명 표기 이형("검정"↔"블랙")까지 보고
      다시 좁히고(이슈 #454), 그래도 색상 조건이 안 맞으면 "없다/품절"이라 단정하지 않고
      "찾지 못했어요"로만 안내한다(패킷 §3 — 승인 사전 밖 표기일 수 있어 단정할 근거가 없다).
      담기 전 get_cart(§4.9)로 기존 보유를 확인해 합산 안내(조회 실패 시에도 담기 진행, degrade).
조회: get_cart(I-18) → token 텍스트 답변(별도 이벤트 없음, §3.1).
게스트 담기 허용(userId|guestId, §4.1) — 신원은 JWT sub 유래(요청 본문 불신).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
import logging

from pydantic import ValidationError

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.intent_guard import (
    classify_cart_utterance,
    has_wishlist_remove_evidence,
)
from app.agents.buyer.cart.options import (
    OptionHint,
    color_condition_terms,
    narrow_options,
    options_have_color_axis,
)
from app.agents.buyer.cart.purchase_state import state_advice_lines, state_suffix
from app.agents.buyer.cart.quantity import stream_cart_quantity_change
from app.agents.buyer.cart.remove import stream_cart_remove
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.cart.wishlist import stream_wishlist_add, stream_wishlist_remove
from app.agents.buyer.recommendation.state import CartIntent
from app.core.text import _strip_unsafe
from app.core.tracing import current_request_trace
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import AddToCartRequest, AddToCartResult, CartOption, CartViewItem
from app.services import spring_client
from app.services.spring_client import (
    CartError,
    CartOptionInvalid,
    CartOptionRequired,
    CartProductNotFound,
    CartQuantityExceeded,
    CartStockInsufficient,
    SpringUnavailableError,
)

__all__ = ["cart_identity", "stream_cart_add", "stream_cart_view"]

_log = logging.getLogger(__name__)

_OPTION_PRODUCT_NAME_MAX_CHARS = 40


def _option_label(option: CartOption) -> str:
    """옵션 표시 라벨 — 표시명(판매자 입력이라 정제)에 추가금이 양수면 함께 붙인다. 이름 없으면 "".

    extraPrice 는 api-spec §4.1 상 surcharge(≥0) — 양수만 표시. 0/음수(계약 미정의)는 미표시.
    되물음 문구(_options_text)와 자동 선택 안내(#114)가 같은 규칙을 쓰도록 한 곳에 둔다.
    """
    name = _strip_unsafe(option.name)
    if not name:
        return ""
    return f"{name}(+{option.extra_price:,}원)" if _has_surcharge(option) else name


def _has_surcharge(option: CartOption) -> bool:
    return bool(option.extra_price and option.extra_price > 0)


def _option_product_heading(product_name: str | None) -> str:
    """옵션 재질문의 대상 상품명을 정제하고 최대 40자로 제한한다(이슈 #662)."""
    name = _strip_unsafe(product_name or "")
    if not name:
        return ""
    if len(name) > _OPTION_PRODUCT_NAME_MAX_CHARS:
        name = f"{name[:_OPTION_PRODUCT_NAME_MAX_CHARS]}…"
    return f"**상품:** {name}"


def _prepend_option_product_heading(prompt: str, product_name: str | None) -> str:
    """상품명이 확인될 때만 기존 옵션 문구 앞에 식별 머리말을 붙인다."""
    heading = _option_product_heading(product_name)
    return f"{heading}\n\n{prompt}" if heading else prompt


def _numbered_option_rows(labels: Sequence[str]) -> str:
    """완성된 옵션 표시 라벨을 1-based 번호 목록과 굵은 글씨로 꾸민다(이슈 #582)."""
    return "\n".join(f"{index}. **{label}**" for index, label in enumerate(labels, start=1))


def _options_text(options: list[CartOption]) -> str:
    """옵션 목록을 되물음 문구로 번호 매겨 나열한다 — 추가금(extraPrice)이 있으면 함께 표시.

    이슈 #570 은 옵션 하나를 한 줄에 두도록 정했고, 이슈 #582 는 정제된 완성 라벨을 1-based
    순서 번호와 굵은 글씨로 꾸민다. 구분자는 `"\\n"` — 옵션명 자체에 `/` 가 흔해(로컬 카탈로그 21,373행 실측,
    2026-08-10, 11,480건=53.7%가 `/` 포함) `" / "` 로 이으면 `블랙 / M`, `화이트 / M` 두 개가
    `블랙 / M / 화이트 / M` 으로 붙어 네 개처럼 읽혔다. #118·#455 이후 지켜온 "되물음 문구는
    한 글자도 바꾸지 않는다" 규약을 이 실측 근거로 의도적으로 푼다.
    """
    labels = [label for opt in options if (label := _option_label(opt))]
    return _numbered_option_rows(labels) if labels else "옵션"


def _options_prompt(
    lead: str,
    options: list[CartOption],
    tail: str = "",
    *,
    product_name: str | None = None,
) -> str:
    """'안내 줄 → 번호 매긴 옵션 행들 → 마무리 줄' 로 되물음 문구를 조립한다(이슈 #570, #582).

    이슈 #570 의 한 옵션 한 줄 원칙 위에 #582 의 1-based 번호와 완성 라벨 굵은 글씨를 더한다.
    옵션 하나가 번호 매긴 한 줄을 온전히 차지하게 해 구분자를 없앤다 — 옵션명의 53.7%(로컬
    카탈로그 21,373행 실측, 2026-08-10)가 `/` 를 포함해 한 줄 나열은 개수가 오독됐다.
    옵션 행에는 구두점을 붙이지 않는다(사용자가 옵션명을 그대로 복사해 답한다).
    """
    lines = [lead, _options_text(options)]
    if tail:
        lines.append(tail)
    return _prepend_option_product_heading("\n".join(lines), product_name)


async def _load_cart_color_synonyms(settings) -> Mapping[str, Sequence[str]] | None:
    """옵션 되물음(§2-B, 이슈 #454)에 쓸 색상 동의어 사전을 적재한다 — 실패·설정 off는 오늘 문구로
    degrade(예외를 담기 흐름으로 올리지 않는다).

    `spring_client._load_color_synonym_map` 을 재사용한다(패킷 §2-A-4) — I-1 색상 확장(#258)과
    같은 캐시·동시 실행 상한·negative caching 을 공유해 두 번째 적재 경로를 만들지 않는다.
    `color_synonym_expansion_enabled`(I-1 배선 게이트) 가 아니라 `cart_option_color_synonym_
    enabled` 를 보는 이유는 그 필드의 docstring 참조(§2-A-5) — `color_synonym_expansion_enabled`
    는 BE `color[]` 배열 계약 준비 신호라 전제가 다르다. 이 함수는 그 계약과 무관하게 항상
    켤 수 있다.
    """
    if not settings.cart_option_color_synonym_enabled:
        return None
    try:
        mapping = await spring_client._load_color_synonym_map(settings)
    except Exception:
        _log.warning("장바구니 색상 동의어 적재 실패 — 오늘 되물음 문구로 degrade", exc_info=True)
        mapping = None
    if mapping is None:
        if trace := current_request_trace():
            trace.mark_degraded("cart_option_color_synonym_skipped")
        return None
    return mapping


def _cart_option_required_text(
    options: list[CartOption],
    *,
    message: str,
    condition_terms: Sequence[str],
    hint: OptionHint | None,
    settings,
    color_synonyms: Mapping[str, Sequence[str]] | None = None,
    product_name: str | None = None,
) -> str:
    """CART_OPTION_REQUIRED 되물음 문구(이슈 #455, #454, #508) — 네 갈래.

    (a) 400 목록이 비었을 때 — 두 갈래로 더 나뉜다.
        - I-1 힌트 이름이 있으면 그 이름으로 되묻는다(오늘은 `_options_text([])` 가 "옵션"
          이라 아무 도움이 안 되는 문구가 나갔다).
        - [이슈 #508] 힌트 이름도 없으면 **품절 안내로 degrade** 한다. 신 계약(BE 가
          `error.detail.options` 를 I-1 과 같은 "구매 가능한 것" 기준으로 필터, api-spec §4.1)
          에서는 남은 옵션이 없으면 `CART_OPTION_REQUIRED` 대신 `CART_STOCK_INSUFFICIENT` 로
          와야 하므로 이 경로는 방어(드리프트·계약 위반 대비)다. 여기서는 재고를 단정해도
          된다 — **I-2 가 "옵션이 필수인데 고를 게 하나도 없다"고 말한 사실**에 근거하기
          때문이다((c) 갈래의 색상 단정 금지와는 상황이 다르다 — 그건 옵션명 표기 추론이고
          이건 목록이 비었다는 사실이다).
    (b) 400 목록이 있고 누적 조건(`by_condition`)으로 좁혀지면 좁힌 목록만 실은 문구.
        `color_synonyms`(이슈 #454)를 주면 이 좁히기가 색상 이형 표기(조건어 "검정" ↔
        옵션명 "블랙")까지 등가로 본다(`narrow_options` R2 확장, `_select_auto_option`/R1 은
        건드리지 않는다).
    (c) [이슈 #454] (b)로도 안 좁혀졌는데(0건 — `condition_matched_all` 로 전건 일치와 구별),
        조건어 중 색상어가 있고, 옵션 목록이 색상 축을 실제로 담고 있고, 사전이 있으면 —
        "그 색은 없다/품절이다"라고 **단정하지 않고** 못 찾았다고만 말한다(패킷 §3, 승인 사전
        밖 표기·SKU 코드일 수 있어 없다고 잘라 말할 근거가 없다). 이 갈래는 실측상 색상 조건
        턴의 3.3%에서만 발동한다(docs/specs/MEASURE-OPTION-COLOR-454.md §2) — 나머지는 (d)로
        그대로 떨어진다.
    (d) 그 외 — **오늘 문구를 한 글자도 바꾸지 않는다.**
    """
    if not options:
        if hint is not None and hint.names:
            names = [name for raw in hint.names if (name := _strip_unsafe(raw))]
            if names:
                # 이슈 #570 은 한 옵션 한 줄을 정했고 #582 는 완성 라벨을 번호·굵은 글씨로 꾸민다.
                lines = ["옵션을 선택해 주세요:", _numbered_option_rows(names)]
                if hint.total is not None and hint.total > len(names):
                    lines.append(f"외 {hint.total - len(names)}개")
                lines.append("어떤 걸로 담을까요?")
                return _prepend_option_product_heading("\n".join(lines), product_name)
        return "지금은 고를 수 있는 옵션이 없어요. 품절된 것 같아요. 다른 상품을 보여드릴까요?"

    narrowing = narrow_options(
        options,
        message=message,
        terms=condition_terms,
        min_term_len=settings.cart_option_narrow_min_term_len,
        match_suffixes=settings.cart_option_match_suffixes,
        color_synonyms=color_synonyms,
    )
    if narrowing.by_condition:
        return _options_prompt(
            "말씀하신 조건에 맞는 옵션이에요:",
            list(narrowing.by_condition),
            "이 중에서 고르시거나 다른 옵션을 말씀해 주세요.",
            product_name=product_name,
        )
    if (
        color_synonyms is not None
        and not narrowing.condition_matched_all
        and (color_terms := color_condition_terms(condition_terms, color_synonyms))
        and options_have_color_axis(options, color_synonyms)
    ):
        color_terms_text = " · ".join(_strip_unsafe(term) for term in color_terms)
        return _options_prompt(
            f"'{color_terms_text}' 조건에 맞는 옵션은 찾지 못했어요. 고를 수 있는 옵션은 이거예요:",
            options,
            "이 중에서 고르시거나 다른 상품을 말씀해 주세요.",
            product_name=product_name,
        )
    return _options_prompt(
        "옵션을 선택해 주세요:",
        options,
        "어떤 걸로 담을까요?",
        product_name=product_name,
    )


def _all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """겹치는 경우까지 needle 의 모든 [start, end) 출현 구간을 돌려준다."""
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while (index := text.find(needle, start)) >= 0:
        spans.append((index, index + len(needle)))
        start = index + 1
    return spans


def _pending_switch_signals(
    message: str, pending: PendingAdd | None, markers: list[str]
) -> tuple[bool, bool]:
    """(독립 전환 마커 있음, 독립 pending 옵션명 있음).

    겹친 문자열은 위치로 판정한다. `대` ⊂ `대신`이면 옵션 언급이 아니고,
    `말고` ⊂ `말고기`이면 전환 마커가 아니다.
    """
    if pending is not None and any(not option.name.strip() for option in pending.options):
        # 옵션명을 하나라도 모르면 전환과 옵션 답변을 구별할 근거가 없다. 이 구성에서는
        # 정밀도를 우선해 휴리스틱을 끄므로 #253 의 옛 상품 오담기 보호가 의도적으로 적용되지 않는다.
        return False, False
    option_spans = (
        [
            span
            for option in pending.options
            if (name := option.name.strip())
            for span in _all_spans(message, name)
        ]
        if pending is not None
        else []
    )
    marker_spans = [span for marker in markers if marker for span in _all_spans(message, marker)]
    markers_present = any(
        not any(
            option_start <= marker_start and marker_end <= option_end
            for option_start, option_end in option_spans
        )
        for marker_start, marker_end in marker_spans
    )
    option_mentioned = any(
        not any(
            marker_start <= option_start and option_end <= marker_end
            for marker_start, marker_end in marker_spans
        )
        for option_start, option_end in option_spans
    )
    return markers_present, option_mentioned


def _existing_quantity(items: list[CartViewItem], product_id: int, option_id: int | None) -> int:
    """담기 전 보유 수량(합산 안내용) — 동일 상품·옵션 합계. optionId 미상이면 그 상품 전체를 센다."""
    return sum(
        item.quantity
        for item in items
        if item.product_id == product_id and (option_id is None or item.option_id == option_id)
    )


def _select_auto_option(
    options: list[CartOption],
    *,
    message: str,
    condition_terms: Sequence[str],
    hint: OptionHint | None,
    settings,
    already_sent: int | None,
) -> CartOption | None:
    """자동 선택 후보 판정(이슈 #114·#455) — 순서대로 시도, 아무것도 안 맞으면 `None`.

    1) 후보가 **1개뿐**이면 그 옵션(#114 그대로 — 힌트로 이 규칙을 게이팅하지 않는다).
    2) 아니면 **이번 발화**로 좁힌 후보(`by_message`)가 정확히 1개이고 `optionCount` 정합
       가드를 통과하면 그 옵션. 힌트가 없으면 가드는 통과(부재 ≠ 불일치) — 있는데 400 목록
       개수와 다르면(절단·드리프트) 자동 선택하지 않는다.
    자동 선택은 **이번 발화 근거(by_message)로만** 한다 — 누적 조건(by_condition)은 되물음
    문구를 좁히는 데만 쓴다(옛 턴 조건으로 사용자가 고르지 않은 옵션을 결제 대상에 넣지 않는다).
    """
    if len(options) == 1:
        candidate = options[0]
    else:
        narrowing = narrow_options(
            options,
            message=message,
            terms=condition_terms,
            min_term_len=settings.cart_option_narrow_min_term_len,
            match_suffixes=settings.cart_option_match_suffixes,
        )
        if len(narrowing.by_message) != 1:
            return None
        if not (hint is None or hint.total is None or hint.total == len(options)):
            return None
        candidate = narrowing.by_message[0]
    if candidate.option_id == already_sent:
        return None
    return candidate


async def _add_with_single_option(
    add_fn,
    req: AddToCartRequest,
    *,
    message: str,
    condition_terms: Sequence[str],
    hint: OptionHint | None,
    settings,
) -> tuple[AddToCartResult, CartOption | None]:
    """I-2 담기 — 후보가 자동 선택되면 되묻지 않고 그 optionId 로 즉시 재담기한다(이슈 #114·#455).

    선택지가 하나면(#114), 또는 이번 발화로 후보가 정확히 하나로 좁혀지면(#455) 되물어도 답이
    정해져 있어 왕복만 늘어난다. 계약 변경은 없다 — AI 가 선택한 optionId 로 I-2 를 재호출할
    뿐(api-spec §4.1). 자동 선택 재시도는 **1회**로 고정한다: 자동 선택한 옵션에도 REQUIRED 가
    또 오면 계약 이상이므로 예외를 그대로 올려 기존 되물음 멀티턴으로 degrade 한다(무한 재시도
    금지). 후보를 못 고르면 임의로 고르지 않고, 이미 보낸 optionId 와 같으면 같은 요청을
    되풀이하지 않는다(`_select_auto_option`). 나머지 오류(INVALID·재고·수량 등)는 그대로 상위로
    올려 기존 action 매핑을 탄다.

    반환값 두 번째는 자동 선택한 옵션(없었으면 None) — AI 가 대신 골랐음을 안내 문구에 밝히기 위함.
    """
    try:
        return await add_fn(req), None
    except CartOptionRequired as exc:
        option = _select_auto_option(
            exc.options,
            message=message,
            condition_terms=condition_terms,
            hint=hint,
            settings=settings,
            already_sent=req.option_id,
        )
        if option is None:
            raise
        return await add_fn(req.model_copy(update={"option_id": option.option_id})), option


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


# 담기 대상을 확정하지 못했을 때의 되물음 문구 (#118).
#
# 기본 문구는 **한 글자도 바꾸지 않는다** — 화면 맥락이 없는 경로(FE 가 `screen` 을 보내지 않는
# 현재의 절대다수)는 오늘과 바이트 동일해야 한다. 화면 후보가 있는데 "추천을 먼저 받아보시면"
# 이라고 답하면 사용자는 눈앞의 상품을 두고 엉뚱한 안내를 받는다 — 정본 §3.1 의 "여러 건이면
# 되물음"은 되묻는 것만이 아니라 **무엇을 물어야 할지 알려주는 것**까지다.
_UNRESOLVED_DEFAULT = "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
# [#435] `last_reco`(스레드 누적 추천)가 비어 있지 않은 턴의 문구 — 위 기본 문구는 **이미
# 추천을 받은** 사용자에게 거짓으로 읽힌다. `screen_reason` 기반 문구가 있으면 그쪽이 우선한다
# (화면 맥락이 이름 지목보다 구체적인 신호다) — 이 문구는 `screen_reason` 이 없을 때만 쓴다.
# "방금"처럼 시점을 단정하는 표현은 쓰지 않는다 — `last_reco` 는 누적이라 직전 턴이 아닐 수 있다.
_UNRESOLVED_WITH_RECO = (
    "어떤 상품을 담을까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 담아드릴게요."
)
_UNRESOLVED_AFTER_PUSH_FAILURE = "어떤 상품을 담을까요? 추천 목록 전달에 문제가 있었어요. 다시 추천을 요청해 주시면 도와드릴게요."
# 화면을 가리켰지만 **어느 것인지** 특정되지 않은 경우. 후보 다건(`ambiguous_screen_candidates`)과
# 순번·좌표가 화면 범위를 벗어난 경우(`*_out_of_range`), 좌표를 풀 `columns` 가 없는 경우를 **한
# 문구로 묶는다** — 사유는 다르지만 사용자가 취해야 할 다음 행동이 "위치를 다시 말한다"로 같기
# 때문이다. 범위 밖에 "N개까지만 있어요"처럼 개수를 알려주는 안내는 화면에 이미 보이는 정보를
# 되읊는 것이라 이득이 없다고 판단했다.
_UNRESOLVED_SCREEN_POSITION = (
    "화면에 보이는 상품 중 어떤 걸 담을까요? 왼쪽부터 몇 번째인지 말씀해 주시면 담아드릴게요."
)
# 사용자가 말한 상품 id 를 두 목록 어디에서도 찾지 못한 경우. 위와 **묶지 않는다** — 여기서
# "몇 번째인지 말해 달라"고 하면 못 찾았다는 사실 자체가 전달되지 않아 같은 말을 반복하게 된다.
_UNRESOLVED_SCREEN_NOT_FOUND = (
    "말씀하신 상품을 화면에서 찾지 못했어요. 화면에 보이는 상품 중에서 골라 주시겠어요?"
)
_SCREEN_POSITION_REASONS = frozenset(
    {
        "ambiguous_screen_candidates",
        "ordinal_out_of_range",
        "coordinate_out_of_range",
        "coordinate_without_columns",
        "coordinate_invalid",
    }
)


def _unresolved_notice(
    screen_reason: str | None, has_last_reco: bool, *, has_push_failed: bool = False
) -> str:
    """되물음 문구를 화면 해소 사유 → `last_reco` 유무 순으로 가른다.

    `screen_reason` 이 있으면 화면 문구가 **우선**한다(위 `_UNRESOLVED_WITH_RECO` 정의 참조).
    둘 다 없으면 오늘 문구(`_UNRESOLVED_DEFAULT`) 그대로.
    """
    if screen_reason in _SCREEN_POSITION_REASONS:
        return _UNRESOLVED_SCREEN_POSITION
    if screen_reason == "unknown_product_id_spoken":
        return _UNRESOLVED_SCREEN_NOT_FOUND
    if has_last_reco:
        return _UNRESOLVED_WITH_RECO
    if has_push_failed:
        return _UNRESOLVED_AFTER_PUSH_FAILURE
    return _UNRESOLVED_DEFAULT


async def stream_cart_add(
    *,
    identity,
    cart: CartIntent,
    cart_store: CartStateStore,
    thread_key: str,
    settings,
    message: str = "",
    allowed_product_ids: set[int] | None = None,
    screen_reason: str | None = None,
    screen_reference_attempted: bool = False,
    screen_resolved: bool = False,
    condition_terms: Sequence[str] = (),
    product_names: Mapping[int, str] | None = None,
    has_last_reco: bool = False,
    has_push_failed: bool = False,
    chat_session_id: str | None = None,
    add_fn=None,
    get_cart_fn=None,
    delete_fn=None,
    change_quantity_fn=None,
    add_wishlist_fn=None,
    get_wishlist_fn=None,
    remove_wishlist_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """담기 서브그래프. action(CART_ADDED/CART_ADD_FAILED) 또는 옵션 되물음 token 을 낸다.

    `screen_reason` 은 화면 지시어 해소기(`screen_reference`)가 담기 대상을 **일부러 비운** 사유다
    (#118). 되물음 문구를 상황에 맞게 가르는 데만 쓰고 판정에는 관여하지 않는다 — `None` 이면
    (= FE 가 `screen` 을 안 보냈거나 해소기가 개입하지 않은 절대다수 경로) 문구는 오늘과 같다.

    `has_last_reco`(#435) 는 스레드 누적 추천(`last_reco`)이 비어 있지 않은지만 알리는 신호다 —
    `screen_reason` 이 없을 때 미해소 문구를 가르는 데만 쓴다(`_unresolved_notice` 참조).

    **[라운드 14, head `0a53ffc` 리뷰]** 아래 삭제·찜 세 위임 분기(`stream_cart_remove`/
    `stream_wishlist_add`/`stream_wishlist_remove`)는 이 `screen_reason` 을 넘기지 않는다 —
    누락이 아니라 **의도적 축소**다. 그 세 흐름은 화면 해소 사유별 문구(`_UNRESOLVED_SCREEN_POSITION`·
    `_UNRESOLVED_SCREEN_NOT_FOUND`) 대신 각자의 범용 되물음 문구를 쓴다. 잘못된 동작이 아니라
    "덜 친절한 문구"일 뿐이고(되물음 자체는 정상 발생), #118 의 세 문구는 실 LLM 프로브(N=8)와
    여러 라운드 리뷰로 고른 것이라 이 레인에 측정 없이 같은 급의 찜·삭제용 화면 문구를 지어내
    끼워 넣지 않는다. 화면 맥락(#118)과 삭제·찜(#116·#117)의 통합은 이 레인 범위 밖(핸드오버의
    찜 해소는 "추천 목록·문맥에서 productId 해소"까지)이며, **[라운드 23]** 플래그 제거로 이제
    이 흐름은 항상 사용자에게 도달한다 — 통합은 여전히 후속 항목이다.

    **[#440 라운드 5 리뷰 F15]** 위 "세 분기는 `screen_reason` 을 넘기지 않는다"는 **문구용
    전달**에 대한 결정이다 — `stream_wishlist_remove` 위임에는 화면 위치 신호를 별도로
    넘긴다. 여기서 넘기는 것은 문구가 아니라 **"발화가 화면 위치를 가리키려 했는가·해소기가
    확정했는가"라는 안전 신호**라 위 결정과 충돌하지 않는다(`wishlist.py::_resolve_wishlist_
    remove_target` 게이트 참조). 이 경로는 decompose 가 `cart_add` 로 오분류한 발화가 오는
    **2선 방어**라, 라운드 4(F12)가 `buyer/graph.py` 의 두 직접 경로만 고치고 이 경로를
    빠뜨렸던 것이 실제로 파괴적이었다(화면 순번 해소가 거부됐는데도 찜이 삭제됨, 재현 확인).
    **[라운드 6 리뷰 F18]** 그 신호는 호출부(`buyer/graph.py`)가 계산해 그대로 넘기는 값이다
    — 이 함수는 그 값을 **찜 해제 위임에만** 그대로 전달할 뿐, 담기(`stream_cart_add` 자체의
    동작)에는 아무 영향도 주지 않는다(기본값이 "위치 미언급"이라 화면이 없거나 안 넘긴 호출부는
    오늘과 같다). **[라운드 10 리뷰 F27]** 단일 파생값(`screen_refused`)이 `screen_position_
    mentioned`·`screen_resolved` 두 원자로 갈라졌다 — 규칙 2·3 이 이 신호를 서로 다르게 쓰기
    때문이다(`wishlist.py` docstring "라운드 10 리뷰 F27" 문단 참조). 파생값을 남겨두지 않고
    두 원자를 그대로 전달한다 — 파생값이 남으면 다음 사람이 어느 쪽을 고쳐야 할지 모른다(이
    레인에서 이미 두 번 겪었다).

    **`has_last_reco`(#435)·`has_push_failed`(#468)는 위 세 위임 중 `stream_wishlist_add`로
    위임할 때만 전달한다** — 나머지 둘(`stream_wishlist_remove`·`stream_cart_remove`)은 누락이
    아니라 **불필요**다. 그 두 흐름의 미해소 문구는 `last_reco`·push 실패 마커를 보지 않고
    **실제 찜/장바구니 목록**에서 만들어진다
    (`wishlist.py::_wishlist_unresolved_notice`·`remove.py`) — 이미 목록을 손에 쥐고 있어
    "추천을 받았는지"와 무관하게 구체적인 문구가 나가므로, `last_reco` 유무가 그 문구를 가를
    이유가 없다.
    """
    # 삭제·찜 위임(이슈 #116·#117, 패킷 §4) — 신원 도출·pending 조회보다 앞에서 판별한다. LLM 을
    # 새로 부르지 않는 결정론적 판정이라 순서가 앞이어도 비용이 없다. **[라운드 23]** 이 둘의
    # 온/오프 여부를 가리던 설정 필드를 제거했다(사용자 지시) — 판정이 나오면 항상 해당 흐름으로
    # 위임한다. 이 턴은 담기 대기와 무관한 흐름으로 위임하므로, `graph.py` 665~668행과 같은
    # 취지로 stale pending 을 정리한다(다음 턴이 옛 상품의 옵션 답변으로 오해석되지 않게).
    intent = classify_cart_utterance(message, settings)
    if intent == "wishlist_add":
        await cart_store.clear_pending(thread_key)
        async for frame in stream_wishlist_add(
            identity=identity,
            cart=cart,
            settings=settings,
            allowed_product_ids=allowed_product_ids,
            has_last_reco=has_last_reco,
            has_push_failed=has_push_failed,
            add_wishlist_fn=add_wishlist_fn,
            observer=observer,
        ):
            yield frame
        return
    # [#440 라운드 9 리뷰 F24] 이 위임도 결정론 규칙이 LLM 흐름을 **덮어쓰는** 지점이다 — 관대한
    # 라우팅만으로 위임하면 pending(옵션 되물음) 중 `"찜 취소해줘 이 표현이 맞아?"` 같은 발화가
    # `clear_pending` 을 실행해 **진행 중이던 되물음을 지운다**(실측, 파괴적 — 찜도 안 지워지고
    # 담기 흐름도 잃는다). `has_wishlist_remove_evidence`(F22)가 없으면 위임하지 않고 **오늘
    # 동작(담기 흐름)** 으로 남긴다 — `clear_pending` 도 일어나지 않는다.
    if intent == "wishlist_remove" and has_wishlist_remove_evidence(message, settings):
        await cart_store.clear_pending(thread_key)
        async for frame in stream_wishlist_remove(
            identity=identity,
            cart=cart,
            message=message,
            settings=settings,
            get_wishlist_fn=get_wishlist_fn,
            remove_wishlist_fn=remove_wishlist_fn,
            observer=observer,
            screen_reference_attempted=screen_reference_attempted,
            screen_resolved=screen_resolved,
        ):
            yield frame
        return
    if intent == "cart_remove":
        await cart_store.clear_pending(thread_key)
        async for frame in stream_cart_remove(
            identity=identity,
            message=message,
            cart_store=cart_store,
            thread_key=thread_key,
            settings=settings,
            chat_session_id=chat_session_id,
            get_cart_fn=get_cart_fn,
            delete_fn=delete_fn,
            observer=observer,
        ):
            yield frame
        return
    # [#285, I-25 §4.13] cart_remove 2선 경로와 대칭 — intent_guard 사다리 4-a 가 `"cart_quantity"`
    # 를 돌려주면 여기서도 같은 도착지로 위임한다.
    if intent == "cart_quantity":
        await cart_store.clear_pending(thread_key)
        async for frame in stream_cart_quantity_change(
            identity=identity,
            cart=cart,
            message=message,
            settings=settings,
            get_cart_fn=get_cart_fn,
            change_fn=change_quantity_fn,
            observer=observer,
        ):
            yield frame
        return

    add_fn = add_fn or spring_client.add_to_cart
    get_cart_fn = get_cart_fn or spring_client.get_cart

    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED", message="담기에는 로그인이 필요해요.", reason="CART_ERROR"
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    # 되물음 진행 중이라도 사용자가 **다른 추천 상품**으로 전환하면 pending 을 버리고 새 담기로 처리한다
    # (옛 상품 옵션 되물음에 갇히지 않게 — decompose 가 전환을 새 productId 로 신호).
    pending = await cart_store.get_pending(thread_key)
    if (
        pending is not None
        and cart.product_id is not None
        and cart.product_id != pending.product_id
        and (allowed_product_ids is None or cart.product_id in allowed_product_ids)
    ):
        await cart_store.clear_pending(thread_key)
        pending = None
    markers_present, pending_option_mentioned = _pending_switch_signals(
        message, pending, settings.cart_pending_switch_markers
    )
    # 해소된 전환은 위 분기가 pending 을 지웠다. 여기 남은 pending + 전환 표지는 productId 가
    # 에코/null/미추천 값 중 무엇이든 해소 실패이므로 옛 상품에 적용하지 않는다.
    unresolved_switch = pending is not None and markers_present and not pending_option_mentioned
    if unresolved_switch:
        await cart_store.clear_pending(thread_key)
        pending = None
    if pending is not None:
        product_id: int | None = pending.product_id
        # 옵션 답변과 함께 수량을 다시 말하면("레드로 5개") 새 수량을 우선한다(기본 1이면 pending 유지).
        # 단 이번 턴이 pending 상품을 겨냥할 때만 — 순수 옵션 답변(productId=None)이거나 같은 상품일 때.
        # (다른/미추천 상품을 가리켜 전환이 성립 안 한 경우의 수량을 옛 상품에 잘못 적용하지 않게.)
        same_target = cart.product_id is None or cart.product_id == pending.product_id
        quantity = cart.quantity if (cart.quantity != 1 and same_target) else pending.quantity
        attempts = pending.attempts
    else:
        product_id = None if unresolved_switch else cart.product_id
        quantity = cart.quantity
        attempts = 0
    option_id = None if unresolved_switch else cart.option_id

    # 경로 B — SSE에 카드가 없어 문맥으로 상품을 확정한다. 신규 담기는 직전 추천(last_reco)에 있는
    # productId 만 허용(LLM 이 발화 속 임의 숫자를 오추출해 추천 안 된 상품을 담는 것 차단). 되물음
    # 진행(pending) 중이면 이미 검증된 상품이므로 예외.
    unresolved = product_id is None or (
        pending is None
        and allowed_product_ids is not None
        and product_id not in allowed_product_ids
    )
    if unresolved:
        yield sse(
            "token",
            TokenData(
                text=_unresolved_notice(
                    screen_reason, has_last_reco, has_push_failed=has_push_failed
                )
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    product_name = product_names.get(product_id) if product_names is not None else None

    # 담기 전 기존 보유 확인(안내용, degrade) — 동일 상품·옵션 보유 수량. 조회 결과 자체를 들고
    # 있다가 유일 옵션 자동 선택(#114)으로 옵션이 확정되면 아래에서 그 옵션 기준으로 다시 센다.
    existing_items: list[CartViewItem] = []
    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
        existing_items = list(cart_view.items)
    except SpringUnavailableError:
        if trace := current_request_trace():
            trace.mark_degraded("cart_merge_skipped")
        pass  # 조회 실패해도 담기는 진행(§4.9)
    existing = _existing_quantity(existing_items, product_id, option_id)

    # I-1 옵션 힌트 조회(이슈 #455) — product_id 확정 뒤·I-2 호출 전, 인메모리 1회. 미스는 예외
    # 없이 None(재시작·다중 인스턴스 degrade, 오늘 경로와 동일).
    hint = await cart_store.get_option_hint(thread_key, product_id)
    try:
        recommendation_context = (
            await cart_store.get_last_reco_state(thread_key)
        ).recommendation_contexts.get(product_id)
    except Exception as exc:  # noqa: BLE001 - attribution state failure must not block a cart mutation
        _log.warning("cart_recommendation_context_unavailable", extra={"reason": str(exc)})
        recommendation_context = None

    try:
        req = AddToCartRequest(
            user_id=user_id,
            guest_id=guest_id,
            product_id=product_id,
            option_id=option_id,
            quantity=quantity,
            chat_session_id=chat_session_id,
            recommendation_context=recommendation_context,
        )
        result, auto_option = await _add_with_single_option(
            add_fn,
            req,
            message=message,
            condition_terms=condition_terms,
            hint=hint,
            settings=settings,
        )
    except CartOptionRequired as exc:
        # api-spec §4.1 — REQUIRED 는 **상한 없는 되물음 멀티턴**(사용자가 옵션을 아직 안 준 정상 흐름).
        # 각 되물음은 사용자 입력을 요구하므로 서버 무한 루프가 아니다. INVALID 카운터(attempts)는
        # 리셋하지 않고 보존해 사이에 끼어도 INVALID 상한이 유지되게 한다.
        # PendingAdd.options 에는 **언제나 전체 exc.options** 를 저장한다(좁힌 목록을 저장하면
        # 사용자가 좁힌 목록 밖 옵션을 답했을 때 다음 턴 decompose 가 optionId 를 못 찾는다 —
        # `app/agents/buyer/graph.py` 가 pending 옵션 전체를 PENDING_CART 로 프롬프트에 싣는다).
        await cart_store.set_pending(
            thread_key,
            PendingAdd(
                product_id=product_id, quantity=quantity, options=exc.options, attempts=attempts
            ),
        )
        # 색상 동의어 사전 적재(이슈 #454)는 **여기서만** — 담기 성공 경로에 DB 왕복을 새로 얹지
        # 않는다(패킷 §2-A-4). 목록이 비면(a 갈래) 어차피 안 쓰이므로 로드조차 건너뛴다.
        color_synonyms = await _load_cart_color_synonyms(settings) if exc.options else None
        text = _cart_option_required_text(
            exc.options,
            message=message,
            condition_terms=condition_terms,
            hint=hint,
            settings=settings,
            color_synonyms=color_synonyms,
            product_name=product_name,
        )
        yield sse("token", TokenData(text=text).model_dump(by_alias=True))
        yield _done()
        return
    except CartOptionInvalid as exc:
        # api-spec §4.1 — INVALID 는 재시도 상한(config cart_option_reask_max, 기본 1) 후 CART_ERROR.
        new_attempts = attempts + 1
        if new_attempts > settings.cart_option_reask_max:
            await cart_store.clear_pending(thread_key)
            yield sse(
                "action",
                ActionData(
                    type="CART_ADD_FAILED",
                    message="옵션을 확인하지 못했어요. 다시 시도해 주세요.",
                    reason="CART_ERROR",
                ).model_dump(by_alias=True),
            )
            yield _done()
            return
        await cart_store.set_pending(
            thread_key,
            PendingAdd(
                product_id=product_id, quantity=quantity, options=exc.options, attempts=new_attempts
            ),
        )
        yield sse(
            "token",
            TokenData(
                text=_options_prompt(
                    "그 옵션을 찾지 못했어요. 다시 골라 주세요:",
                    exc.options,
                    product_name=product_name,
                )
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartProductNotFound:
        await cart_store.clear_pending(thread_key)
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message="해당 상품을 찾지 못했어요.",
                reason="PRODUCT_NOT_FOUND",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartStockInsufficient as exc:
        # I-2 재고 부족 — 남은 재고 노출("재고가 N개뿐이에요"). 재고 0(품절, BE ON_SALE+stock 0)은
        # "품절된 상품이에요", availableStock 미상 시 일반 안내(§4.1, 2026-07-22).
        await cart_store.clear_pending(thread_key)
        if exc.available_stock is None:
            message = "재고가 부족해 담지 못했어요."
        elif exc.available_stock == 0:
            message = "품절된 상품이에요."
        else:
            message = f"재고가 {exc.available_stock}개뿐이에요."
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message=message,
                reason="STOCK_INSUFFICIENT",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartQuantityExceeded:
        # I-2 수량 상한 초과(합산 > 99, BE VALIDATION_ERROR) — BE와 동일 문구, reason 은 CART_ERROR 유지.
        # 문구의 99 는 BE CartItem.MAX_QUANTITY 와 결합(변경 시 동기화 필요).
        await cart_store.clear_pending(thread_key)
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message="수량은 최대 99개까지 담을 수 있습니다.",
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except (CartError, SpringUnavailableError, ValidationError):
        await cart_store.clear_pending(thread_key)
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message="장바구니에 담지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    # 성공 — 되물음 상태 정리 + 합산 안내.
    await cart_store.clear_pending(thread_key)
    # "방금 담은 거" 삭제 해소 소스(이슈 #116) — 담기 **성공** 경로에서만 기록한다(실패·되물음은
    # 갱신하지 않음). cart_item_id 가 없으면(계약상 있어야 하지만 방어) 해소에 쓸 수 없어 저장하지
    # 않는다.
    # [라운드 3 리뷰 F-2, 라운드 23] 이 값을 읽는 곳은 삭제 흐름뿐이다. 이전엔 삭제 흐름의 온/오프
    # 설정 필드로 이 쓰기까지 가뒀지만, 그 필드가 삭제돼(사용자 지시) 이제 항상 기록한다 — 삭제
    # 흐름이 늘 활성이라 "쓰지도 않을 값"이 아니다.
    if result.cart_item_id is not None:
        try:
            await cart_store.set_last_add(thread_key, result.cart_item_id, product_id)
        except Exception as exc:  # noqa: BLE001 - Spring 담기가 이미 성공한 뒤라 이 쓰기 실패로
            # CART_ADDED/done 을 죽이면 안 된다(2차 리뷰 지적 6·1번) — 상품은 담겼는데 사용자는
            # 실패를 보는 게 더 나쁘다. "방금 담은 거" 해소만 다음 턴에 모르는 상태로 degrade될
            # 뿐이라 로그만 남기고 성공 흐름은 그대로 진행한다(`ThreadFilterStore.get` 과 같은
            # 취지). CancelledError 는 BaseException 이라 전파된다.
            _log.warning("last_add_write_failed", extra={"reason": str(exc)})
    if auto_option is not None:
        # 담길 옵션이 확정됐으니 그 옵션 기준으로 다시 센다 — 담기 전 계산은 optionId 미상이라
        # 지금 후보에 없는 옛 옵션(단종·품절)의 보유까지 합산할 수 있고, 그러면 Spring 은 새 줄로
        # 담았는데 "수량을 더했어요"라고 말하게 된다(PR #211 리뷰 / REQ-CART-031 합산 권위=Spring).
        existing = _existing_quantity(existing_items, product_id, auto_option.option_id)
    if existing > 0:
        message = "이미 담겨 있던 상품이라 수량을 더했어요."
    else:
        message = "장바구니에 담았어요."
    # 유일 옵션을 AI 가 대신 골랐으면 무엇으로 담았는지 밝힌다(이슈 #114). 라벨은 되물음 문구와
    # 같은 _option_label — 추가금도 함께 알린다(자동 선택은 사용자가 고를 기회가 없었으므로 숨기면
    # 결제 단계에서야 알게 된다, PR #211 리뷰). 이름이 비면 기본 문구를 유지한다.
    if auto_option is not None and (option_label := _option_label(auto_option)):
        message = (
            f"{option_label} 옵션으로 담아 수량을 더했어요."
            if existing > 0
            else f"{option_label} 옵션으로 담았어요."
        )
    yield sse(
        "action",
        ActionData(type="CART_ADDED", message=message, cart_item_id=result.cart_item_id).model_dump(
            by_alias=True
        ),
    )
    yield _done()


async def stream_cart_view(*, identity, get_cart_fn=None, observer=None) -> AsyncIterator[str]:
    """조회 서브그래프. 장바구니 내용을 token 텍스트로 답한다(§4.9, 별도 이벤트 없음)."""
    get_cart_fn = get_cart_fn or spring_client.get_cart
    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "token",
            TokenData(text="장바구니를 보려면 로그인이 필요해요.").model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
    except SpringUnavailableError:
        yield sse(
            "token",
            TokenData(text="장바구니를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.").model_dump(
                by_alias=True
            ),
        )
        yield _done()
        return

    if not cart_view.items:
        yield sse("token", TokenData(text="장바구니가 비어 있어요.").model_dump(by_alias=True))
        yield _done()
        return

    # 못 사는 항목은 사유를 갈라 알린다(#310, REQ-CART-037) — 목록 줄에는 짧은 라벨만 붙이고
    # 행동 안내는 문단 끝에 상태당 한 번만 싣는다. 항목마다 완결 문장을 붙이면 항목이 여럿일 때
    # 같은 문장이 반복돼 목록이 문장 덩어리가 된다.
    lines = []
    hidden_example: str | None = None
    for item in cart_view.items:
        product_name = _strip_unsafe(item.product_name or "상품")
        option_name = _strip_unsafe(item.option_name) if item.option_name else ""
        opt = f" ({option_name})" if option_name else ""
        lines.append(f"{product_name}{opt} · {item.quantity}개{state_suffix(item.purchase_state)}")
        if item.purchase_state == "HIDDEN" and hidden_example is None:
            hidden_example = f"{product_name}{opt}"
    lines.extend(
        state_advice_lines([item.purchase_state for item in cart_view.items], hidden_example)
    )
    text = "장바구니에 담긴 상품이에요:\n" + "\n".join(lines)
    yield sse("token", TokenData(text=text).model_dump(by_alias=True))
    yield _done()
