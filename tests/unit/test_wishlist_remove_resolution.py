"""찜 해제 대상 해소 — 조회 ↔ 해제 인접 결합 판정 (이슈 #440).

이슈가 명시적으로 요구한 "거짓양성 4종 + 거짓음성을 한 테이블로 고정"을 이 파일 하나에 담는다
(`test_cart_intent_guard.py`·`test_wishlist_flow.py` 에 흩뿌리지 않는다). §4 표를 그대로 옮긴다:
  - 4-A 거짓음성: 찜 지시 명사 + **닫힌 어휘 브리지**(라운드 3 리뷰 F7 — 거리(창)가 아니라
    head-tail 사이에 닫힌 어휘만 올 수 있다는 규칙) 뒤의 해제 tail(동작구 또는 명사형, 라운드 3
    리뷰 F8) 은 `wishlist_remove` 로 라우팅돼야 한다.
  - 4-B 거짓양성: 조회·음식명·시설명·서로 다른 절(브리지 밖 낱말로 이어진 head/tail)·조회성
    명사형 질문은 `wishlist_remove` 도, 해제 근거도 아니어야 한다 — 근거가 없으면
    `_resolve_wishlist_remove_target` 규칙 2·3(문맥 id·목록 1건 자동)이 자동 선택하지 않는다
    (파괴적 동작을 막았다는 직접 증거). [라운드 9 리뷰 F22] `ROUTES_BUT_NO_EVIDENCE` 는
    라우팅은 `wishlist_remove` 로 갈 수 있지만(인용·번역·예시의 목적어) 근거는 없어야 하는
    별도 목록 — FALSE_POSITIVES 와 구분한다(그 목록은 라우팅 자체가 안 돼야 한다).
    `RULE_23_EVIDENCE_TRUE` 는 반대로 근거가 True 여야 하는 목록(왼쪽 닫힌 접두·존댓말·이모지).
  - 4-C: [라운드 8 리뷰 F20] 근거 없으면 라우팅이 무엇이든 삭제 0회(옛 불변식 "라우팅이
    wishlist_remove 면 근거도 True" 는 라운드 1 요구가 틀렸다고 정정됐다 — 아래 테스트 참조) +
    `cart_control` 그룹 6종 무회귀.
  - 4-D: 기존 판정이 그대로인지(장바구니 억제·부정·유보 등) + 알려진 한계(브리지가 못 잇는
    "찜한 상품 중에 이어폰 빼줘" — 파괴적이지 않은 이유는 그 자리 테스트 참조).
  - 4-E: `stream_wishlist_remove`/`stream_cart_add`/`buyer/graph.py` 세 경로의 종단 테스트.
    화면 순번 해소가 `cart_remove`→`wishlist_remove` 정정 경로에도 연결됐는지(라운드 3 리뷰 F9)
    는 `tests/unit/test_screen_context.py` 가 잰다(화면 픽스처·헬퍼가 이미 그쪽에 있다).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.cart.intent_guard import (
    classify_cart_utterance,
    has_deceptive_wishlist_marker,
    has_wishlist_remove_evidence,
)
from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target, stream_wishlist_remove
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.state import CartIntent
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import CartView, CartViewItem, WishlistItem, WishlistView

SETTINGS = get_settings()


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject="guest-uuid-1")


def _req(message: str, thread_id: str = "t1"):
    return SimpleNamespace(session_id="s1", thread_id=thread_id, message=message)


def _item(product_id: int, name: str) -> WishlistItem:
    return WishlistItem(product_id=product_id, name=name, purchase_state="AVAILABLE")


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        context_id=context.context_id,
        request_id="unit-request",
        record_model_call=lambda *_: None,
    )


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(request, identity, observer=observer, **kwargs):
        yield frame


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


def _actions(events) -> list[dict]:
    return [e["data"] for e in events if e["type"] == "action"]


def _wishlist(*items: WishlistItem):
    async def _get(user_id):
        return WishlistView(items=list(items))

    return _get


# ─────────── §4-A 거짓음성 — wishlist_remove 로 라우팅돼야 한다 ───────────

FALSE_NEGATIVES = [
    "찜한 거 빼줘",
    "찜해둔 거 지워줘",
    "찜했던 거 삭제해줘",
    "찜해 놓은 거 빼주세요",
    "찜 빼줘",
    "찜 해제해줘",
    "위시리스트에서 빼줘",
    # [라운드 1 리뷰 F1] 붙여쓰기 — "찜한거"·"찜한 거"는 같은 말인데 공백 유무로 갈리면 안
    # 된다(의존명사 소비, config.utterance_dependent_nouns).
    "찜한거 빼줘",
    "찜해둔거 지워줘",
    # [라운드 1 리뷰 F1-(b)] 보강된 tail 표지("지워주세요"·"없애줘").
    "찜한 거 지워주세요",
    "찜한 거 없애줘",
    "찜 목록에서 없애줘",
    "찜 목록에서 빼줘",
    # [라운드 1 리뷰 F1-(c)] "찜목록"(붙여쓰기) head 표지.
    "찜목록에서 빼줘",
    # [라운드 2 리뷰 F6-(c)] 왼쪽 경계가 정상 발화(찜 앞에 공백/문장 시작)를 죽이지 않는지 —
    # 사다리 1번(`wishlist_remove_markers`)이 `matches_unnegated_left_bounded` 로 바뀐 뒤에도
    # 이 발화들은 여전히 매칭돼야 한다.
    "이어폰 찜 빼줘",
    "그거 찜에서 빼줘",
    "이 상품 찜 취소",
    "내 찜 빼줘",
    # [라운드 3 리뷰 F7] 닫힌 어휘 브리지 검증표 — "찜한 것 빼줘"(의존명사 "것"의 공백형)·
    # "찜한거를 빼줘"(의존명사+조사).
    "찜한 것 빼줘",
    "찜한거를 빼줘",
    # [라운드 3 리뷰 F8] 명사형 tail 이 발화 끝/용언 어미로 바로 이어질 때는 살아야 한다.
    "찜 취소해줘",
    "찜 취소해 주세요",
    "찜 해제",
    # [라운드 5 리뷰 F14] "찜 취소해줘, 장바구니는 그대로 두고" — 1번(명시적 동작 구)은
    # 장바구니 억제를 안 받는 규약(2차 리뷰 지적 8)이 종결 검사 도입 뒤에도 유지되는지.
    "찜 취소해줘, 장바구니는 그대로 두고",
    # [라운드 7 리뷰 F19] 문장 종결 부호 뒤에 내용이 없으면(부호가 발화의 끝이면) 여전히
    # 명령이어야 한다 — "찜 취소해줘. 무슨 뜻이야?"(§4-B, F19)와 구분하는 대조군.
    "찜 취소해줘!",
]


@pytest.mark.parametrize("message", FALSE_NEGATIVES)
def test_false_negatives_route_to_wishlist_remove(message: str) -> None:
    assert classify_cart_utterance(message, SETTINGS) == "wishlist_remove"


async def test_false_negative_issue_body_case_actually_removes_the_single_item() -> None:
    """수용 기준 — 찜 목록이 1건이면 그 1건이 실제로 해제돼 WISHLIST_REMOVED 가 나온다."""
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="찜한 거 빼줘",
            settings=SETTINGS,
            get_wishlist_fn=_wishlist(_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [10]
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


# ─────────── §4-B 거짓양성 — wishlist_remove 도 근거도 아니어야 한다 ───────────

FALSE_POSITIVES = [
    "내 찜 뭐야",
    "찜 리스트 좀",
    "찜해둔거 뭐뭐 있지",
    "찜한 거 보여줘",
    "찜닭 냄비 빼고 다른 것도 보여줘",
    "찜닭 빼줘",
    "갈비찜 지워줘",
    "갈비찜 빼고 보여줘",
    "김치찜 재료 제거하고 추천해줘",
    "찜질방 용품 삭제해줘",
    # [라운드 1 리뷰 F1-(d)] F1 이 넓힌 판정(의존명사 소비 + tail 보강)이 새 거짓양성을 만들지
    # 않는지 — 붙여쓰기 조회와, 보강된 tail("없애줘")이 음식명 head 를 되살리지 않는지.
    "찜한거 보여줘",
    "찜닭 없애줘",
    "갈비찜 없애줘",
    # [라운드 2 리뷰 F6] 사다리 1번(`wishlist_remove_markers`)이 왼쪽 경계 없이 순수 부분
    # 문자열이던 결함 — `"찜 빼줘"`가 `"갈비찜 빼줘"`·`"계란찜 빼줘"`·`"김치찜 빼줘"` 안에
    # 그대로 들어 있어 매칭되고(파괴적 — 규칙 2·3 자동 선택까지 열렸다), `"찜 취소"`도
    # `"계란찜 취소"` 안에서 같은 문제를 냈다.
    "갈비찜 빼줘",
    "계란찜 빼줘",
    "김치찜 빼줘",
    "계란찜 취소",
    # [라운드 3 리뷰 F7] 거리(창)로는 "같은 명령"을 보장하지 못했다 — 서로 다른 절의 "찜"과
    # 해제 동작 구가 우연히 가까우면 결합됐다(파괴적, 규칙 2·3 자동 삭제까지 열렸다). 닫힌 어휘
    # 브리지로 교체한 뒤에는 head-tail 사이에 실질 낱말("보고"·"나중에")이 끼면 결합되지 않아야
    # 한다.
    "찜은 나중에 보고 이어폰 빼줘",
    "찜 목록 보여주고 이거 빼줘",
    "찜 보고 이거 빼줘",
    # [라운드 3 리뷰 F8] 어미 없는 명사형("해제"·"취소") 뒤에 조사·다른 낱말이 오면(조회·질문)
    # 명령으로 읽으면 안 된다.
    "찜 해제 방법 보여줘",
    "찜 해제 조건 알려줘",
    "찜 취소 수수료 알려줘",
    "찜 취소선 그어줘",
    "찜 취소는 어떻게 해?",
    # [라운드 4 리뷰 F10] `utterance_action_verb_suffixes` 의 맨 어간("해"·"할"·"했"·"시켜")이
    # 질문·조회를 명령으로 인정하던 결함 — "할"이 있으면 "할 방법"·"할 수 있는지"·"할까"가,
    # "해"가 있으면 "해도 돼"·"해당"(어간으로 시작하는 다른 낱말)이, "했"이 있으면 "했는지"가
    # 전부 통과했다(실측, 파괴적).
    "찜 해제할 방법 알려줘",
    "찜 취소할 수 있는지 알려줘",
    "찜 취소할까?",
    "찜 해제해도 돼?",
    "찜 취소했는지 확인해줘",
    "찜 해제해당 여부 알려줘",
    # [라운드 4 리뷰 F11] tail 뒤 유보·허가 질문("빼줘야 하나?"·"빼줘도 될까?")이 그대로 삭제
    # 명령으로 읽히던 결함 — 기존 무회귀 표의 "찜 빼줘야 할까?"는 "야 할"을 문자 그대로 암기해
    # 통과했을 뿐, 같은 뜻의 다른 활용은 뚫려 있었다(실측, 파괴적).
    "찜한 거 빼줘야 하나?",
    "찜한 거 빼줘도 될까?",
    "찜한 거 지워줘도 돼?",
    "찜한 거 없애줘야 하나?",
    "찜한 거 삭제해줘야 하나?",
    # [라운드 5 리뷰 F14] `_noun_ending_match_end` 가 접두(startswith)만 보고 종결을 확인하지 않던
    # 결함 — 어미로 "시작"하기만 하면 통과해 표현·의미를 묻는 질문이 명령으로 읽혔다(실측,
    # 파괴적). F10 이 어간을 없애도 접두 매칭 구조 자체는 남아 있었다.
    "찜 취소해줘라는 말이 뭐야?",
    "찜 취소해줘가 맞는 표현이야?",
    "찜 취소할래가 무슨 뜻이야?",
    "찜 취소할게 맞지?",
    # [라운드 6 리뷰 F16] 목록(hedge_markers) 대신 구조(tail_is_command — 연결어미 "도"/"야",
    # 인용 조사)로 유보·허가·인용을 가른다.
    "찜한 거 빼줘도 괜찮을까?",
    "찜한 거 지워줘도 괜찮아?",
    "찜한 거 없애줘도 상관없을까?",
    "이어폰 찜 빼줘도 될까?",
    "이어폰 찜에서 빼줘도 돼?",
    "찜 취소해줘 라는 말이 뭐야?",
    "찜 취소해줘, 라는 말이 뭐야?",
    "찜 해제해줘라고 하면 돼?",
    # [라운드 7 리뷰 F19] 연결어미도 인용 조사도 없는 두 독립 문장 — 문장 종결 부호(".") 뒤에
    # 내용이 더 있으면 tail 은 발화 전체의 최종 지시가 아니라고 본다(tail_is_command 규칙 2).
    # "찜 취소해줘. 무슨 뜻이야?"는 라운드 6(F16)에서 이 구조 밖의 알려진 공백으로 남겨뒀던
    # 사례인데, 이 라운드에서 닫힌 문장부호 집합으로 해소했다(목록에 표현을 추가하지 않았다).
    "찜 취소해줘. 무슨 뜻이야?",
    "찜한 거 빼줘. 배송도 돼?",
]

# [라운드 8 리뷰 F20] `tail_is_command`(라우팅용, 비명령 형태를 나열해 거부)가 못 잡는 새
# 우회 — 라우팅(`classify_cart_utterance`)은 여전히 관대해서 이 발화들도 `wishlist_remove`
# 로 갈 수 있다(그래서 FALSE_POSITIVES 에는 안 넣는다 — 그 목록은 "라우팅도 안 됨"까지
# 요구한다). 하지만 `has_wishlist_remove_evidence` 는 종결(`tail_terminates_utterance`)을
# 요구하게 되면서 근거 축에서 전부 막힌다 — 라우팅이 느슨해도 무해하다는 게 F20 의 핵심이라,
# 이 목록은 "근거만" 검증하고 `test_no_evidence_means_zero_deletions_regardless_of_routing`
# 로 실제 삭제 0회까지 확인한다.
ROUTES_BUT_NO_EVIDENCE = [
    "찜한 거 빼줘 도 될까?",
    "'찜 해제해줘'를 영어로 번역해줘",
    "찜 취소해줘, 이 표현이 맞아?",
    "찜 취소해줘 이 표현이 맞아?",
    "찜 취소해줘 . 무슨 뜻이야?",
    "찜 취소해줘; 무슨 뜻이야?",
    # [라운드 9 리뷰 F22] 오른쪽(종결)만 잠그면 앞 문맥이 열려 있다 — 해제 문구를 인용·번역·
    # 예시의 목적어로 두고 그 문구로 발화를 끝내면 F20 만으로는 통과했다. 왼쪽 전체 앵커
    # (`prefix_words`)가 이 부류를 막는다. 라우팅은 여전히 wishlist_remove 로 갈 수 있다
    # (`is_wishlist_remove_command_context` 는 True — 이름이 실제로 매칭되지 않는 한 규칙 1도
    # 아무것도 지우지 않는다, 아래 안전 성질 테스트 참조).
    "다음 문구를 영어로 번역해줘: '찜 해제해줘'",
    "사용자가 말한 건 '찜 취소해줘'",
    "문구 예시는 (찜 취소해줘)",
]

# [라운드 9 리뷰 F22·F23] 규칙 2·3(코드가 대상을 고르는 자동 선택)의 근거가 True 여야 하는
# 발화 — 화면에 걸리지 않는 정상 명령(왼쪽이 닫힌 어휘뿐이거나 아예 없음, 오른쪽이 존댓
# 보조사·이모지·감탄부호만으로 끝남)은 전부 근거를 받아야 한다.
RULE_23_EVIDENCE_TRUE = [
    "찜한 거 빼줘",
    "찜한거 빼줘",
    "찜 빼줘",
    "찜 해제해줘",
    "찜 취소해줘",
    # F22 — 닫힌 접두(prefix_words·bridge_words)만 head 앞에 있으면 근거가 산다.
    "내 찜 빼줘",
    "그거 찜에서 빼줘",
    "이 상품 찜 취소",
    "찜 목록에서 빼줘",
    # F23 — 존댓 보조사 "요"·이모지·감탄부호는 "내용"이 아니라 종결로 인정된다(목록 나열 금지,
    # `negation.tail_terminates_utterance`·`_noun_ending_match_end` 의 "요" 처리 참조).
    "찜한 거 빼줘요",
    "찜 해제해줘요",
    "찜 취소해줘요",
    "찜한 거 빼줘 🙏",
    "찜한 거 빼줘!!",
]


@pytest.mark.parametrize("message", RULE_23_EVIDENCE_TRUE)
def test_rule_23_evidence_true_cases_have_remove_evidence(message: str) -> None:
    assert has_wishlist_remove_evidence(message, SETTINGS) is True


@pytest.mark.parametrize("message", ROUTES_BUT_NO_EVIDENCE)
def test_routes_but_no_evidence_cases_have_no_remove_evidence(message: str) -> None:
    assert has_wishlist_remove_evidence(message, SETTINGS) is False


@pytest.mark.parametrize("message", FALSE_POSITIVES)
def test_false_positives_do_not_route_to_wishlist_remove(message: str) -> None:
    assert classify_cart_utterance(message, SETTINGS) != "wishlist_remove"


@pytest.mark.parametrize("message", FALSE_POSITIVES)
def test_false_positives_have_no_remove_evidence(message: str) -> None:
    assert has_wishlist_remove_evidence(message, SETTINGS) is False


@pytest.mark.parametrize("message", FALSE_POSITIVES)
def test_false_positives_do_not_auto_select_via_context_id_or_single_item(message: str) -> None:
    """찜 1건 목록 + `cart.product_id` 가 그 항목을 가리키는 상태에서도 규칙 2·3 이 자동 선택
    하지 않는다 — 파괴적 동작을 막았다는 직접 증거(§4-B)."""
    items = [_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(CartIntent(product_id=10), message, items, SETTINGS)
    assert result is None


# ─────────── §4-C 근거 없으면 삭제 0회(라운드 8 리뷰 F20 정정) + cart_control 무회귀 ───────────

CART_CONTROL_TEXTS = [
    "장바구니 보여줘",
    "장바구니에 뭐 있어?",
    "내 장바구니 확인해줘",
    "그거 담아줘",
    "장바구니에 넣어줘",
    "2번 담아줘",
]
# 이 변경(#440) 이전에 실측한 판정 — cart_control 6종은 모두 담기 계열 발화라 찜 표지가
# 전혀 없다("찜" head 가 없으니 1-b 가 손댈 여지가 구조적으로 없다). 그래도 이슈가 "cart_remove
# ('장바구니에서 빼줘')를 잠식하지 않는지 직접 재라" 고 요구하므로 스냅샷으로 고정한다.
CART_CONTROL_EXPECTED_BEFORE = {
    "장바구니 보여줘": "cart_add",
    "장바구니에 뭐 있어?": "cart_add",
    "내 장바구니 확인해줘": "cart_add",
    "그거 담아줘": "cart_add",
    "장바구니에 넣어줘": "cart_add",
    "2번 담아줘": "cart_add",
}


@pytest.mark.parametrize("message", CART_CONTROL_TEXTS)
def test_cart_control_group_utterances_are_unaffected_by_pair_matching(message: str) -> None:
    """evals/intent_probe/fixtures/anchors_b.json 의 cart_control 그룹 발화 전부(6종) —
    #440 변경 전후로 classify_cart_utterance 판정이 같은지 결정론 계층에서 직접 잰다."""
    assert classify_cart_utterance(message, SETTINGS) == CART_CONTROL_EXPECTED_BEFORE[message]


@pytest.mark.parametrize("message", [*FALSE_POSITIVES, *ROUTES_BUT_NO_EVIDENCE])
async def test_no_evidence_means_zero_deletions_regardless_of_routing(message: str) -> None:
    """[라운드 8 리뷰 F20, D4 불변식 정정] 진짜 안전 성질 — **근거가 없으면 라우팅이 어디로
    가든 삭제는 0회다.** 라운드 1(D4)이 걸었던 옛 불변식("라우팅이 `wishlist_remove` 면 근거도
    반드시 `True`")은 **틀렸다** — 그 요구가 `has_wishlist_remove_evidence` 의 tail 판정을
    라우팅과 같게 묶어 놔서 "해제 동작구가 발화를 끝냈는가"(F20 종결 요구)를 걸 수 없게 만들었다.
    라우팅이 느슨한 것은 무해하다 — `wishlist_remove` 로 (오)라우팅돼도 근거가 없으면 규칙
    1·2·3 이 전부 막혀 되물음으로 끝난다. 그래서 이 테스트는 `classify_cart_utterance` 의
    결과를 **묻지 않고** `stream_wishlist_remove` 를 직접 강제로 태운다 — 라우팅이 실제로 무엇을
    내든(제품 이름으로 결정되는 게 아니라) 근거가 없는 §4-B 발화가 삭제로 끝나지 않는지를 규칙
    2(문맥 id)·3(목록 1건 자동) 둘 다 열릴 수 있는 최악의 상태에서 잰다."""
    calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        calls.append(product_id)

    # 찜 1건 — 규칙 3(목록 1건 자동)이 열릴 수 있는 최악의 상태.
    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message=message,
            settings=SETTINGS,
            get_wishlist_fn=_wishlist(_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert calls == []
    assert _types(events) == ["token", "done"]

    # 찜 2건 + cart.product_id 가 그중 하나를 가리킴 — 규칙 2(문맥 id)가 열릴 수 있는 최악의 상태.
    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=10),
            message=message,
            settings=SETTINGS,
            get_wishlist_fn=_wishlist(_item(10, "이어폰"), _item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert calls == []
    assert _types(events) == ["token", "done"]


# ─────────── §4-D 무회귀 ───────────


@pytest.mark.parametrize(
    "message,expected_not_wishlist_remove",
    [
        ("찜한 거 담아줘", True),
        ("찜해둔 이어폰 담아줘", True),
        ("찜한 거 장바구니에서 빼줘", True),
        ("찜한 거 빼지 마", True),
        ("찜 안 빼줘도 돼", True),
        ("찜 빼줘야 할까?", True),
    ],
)
def test_regressions_still_avoid_wishlist_remove(
    message: str, expected_not_wishlist_remove: bool
) -> None:
    got = classify_cart_utterance(message, SETTINGS)
    assert (got != "wishlist_remove") == expected_not_wishlist_remove


def test_regression_cart_remove_still_wins_for_cart_only_phrase() -> None:
    assert classify_cart_utterance("장바구니에서 빼줘", SETTINGS) == "cart_remove"


def test_regression_explicit_wishlist_remove_marker_ignores_cart_suppression() -> None:
    """ "찜 취소해줘, 장바구니는 그대로 두고" — 1번(명시적 동작 구)은 장바구니 억제를 받지
    않는다(2차 리뷰 지적 8 규약, 이 이슈에서도 유지)."""
    assert (
        classify_cart_utterance("찜 취소해줘, 장바구니는 그대로 두고", SETTINGS)
        == "wishlist_remove"
    )


def test_regression_wishlist_add_still_routes() -> None:
    assert classify_cart_utterance("이건 찜해줘", SETTINGS) == "wishlist_add"


@pytest.mark.parametrize(
    "message",
    ["이거 찜해줘. 배송도 돼?", "이거 찜해줘 포장도 되는 걸로"],
)
def test_regression_f13_wishlist_add_survives_trailing_question(message: str) -> None:
    """[라운드 5 리뷰 F13 회귀 복구] 라운드 4 의 F11 이 `utterance_negation_markers` 를 넓혀
    "도 돼"/"도 되"가 담기 표지 뒤 창에서 걸리는 바람에 이 발화들이 `wishlist_add` 대신
    `cart_add`(사다리 기본값)로 떨어졌다 — 찜하려던 상품이 장바구니에 담기는 파괴적 회귀였다.
    hedge 목록을 찜 해제 판정 전용으로 분리해 복구했다."""
    assert classify_cart_utterance(message, SETTINGS) == "wishlist_add"


def test_regression_f13_cart_remove_all_marker_survives_trailing_question() -> None:
    """[라운드 5 리뷰 F13 회귀 복구] 같은 원인으로 `remove.py::_resolve_remove_targets` 의
    전체 삭제 규칙도 "전부 빼줘, 환불도 되는지 알려줘"에서 정상 2건 전체 삭제 대신 되물음으로
    바뀌었다 — hedge 분리로 복구됐는지 `remove.py` 를 직접 재는 것이 이 축의 존재 이유다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets
    from app.schemas.spring import CartViewItem

    items = [
        CartViewItem(cart_item_id=1, product_id=1, product_name="이어폰", quantity=1),
        CartViewItem(cart_item_id=2, product_id=2, product_name="케이스", quantity=1),
    ]
    result = _resolve_remove_targets("전부 빼줘, 환불도 되는지 알려줘", items, SETTINGS, None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1, 2]


# ─────────── 알려진 한계 (라운드 3 리뷰 F7 · 라운드 5 리뷰 F14) ───────────


def test_known_limitation_name_and_reference_marker_across_a_bridge_gap_is_a_false_negative() -> (
    None
):
    """[라운드 3 리뷰 F7] "찜한 상품 중에 이어폰 빼줘"는 §4-A(거짓음성 표)에서 여기로 옮겼다 —
    "중에"·"이어폰"이 `wishlist_remove_bridge_words` 밖이라 head→tail 브리지가 끊겨 이제
    `wishlist_remove` 로 라우팅되지 않는다.

    **이 방향이 옳은 이유**: 사용자가 상품명("이어폰")을 직접 댔으므로, 이 발화가 만약
    `wishlist_remove` 로 왔다면 규칙 1(이름 매칭, `wishlist.py::_resolve_wishlist_remove_target`)
    이 정확히 해소했을 것이다 — 라우팅을 놓쳐도 결과는 `cart_add`(되물음 없는 장바구니 흐름)일
    뿐이라 **파괴적이지 않다**. 반대로 브리지를 열어 이 발화를 살리면 `"찜 보고 이거 빼줘"`류
    (§4-B) 도 같이 살아나 사용자가 요청하지 않은 항목이 삭제된다 — 넓혀서 얻는 것보다 잃는
    것이 크다."""
    assert classify_cart_utterance("찜한 상품 중에 이어폰 빼줘", SETTINGS) != "wishlist_remove"


def test_known_limitation_first_person_intent_suffix_is_no_longer_positive() -> None:
    """[라운드 5 리뷰 F14] "찜 취소할래"는 라운드 4(F10)에서 §4-A 양성이었으나 여기로 옮겼다 —
    "할래"·"할게"(1인칭 의지형)를 `utterance_action_verb_suffixes` 에서 뺐다. 경계 검사를
    통과해도 그 자체가 질문의 주제가 될 수 있어("찜 취소할게 맞지?") 요청형만 남기는 편이
    안전하다. **파괴적이지 않은 이유**: 놓쳐도 결과는 되물음(`cart_add` 기본값)일 뿐이다."""
    assert classify_cart_utterance("찜 취소할래", SETTINGS) != "wishlist_remove"


def test_known_limitation_valid_compound_command_after_terminal_punctuation_is_blocked() -> None:
    """[라운드 7 리뷰 F19] "찜한 거 빼줘. 그리고 이것도 담아줘" 처럼 **정상 명령 + 별개 문장**도
    F19 의 문장 종결 부호 규칙(`tail_is_command`)에 걸려 `wishlist_remove` 로 못 간다 — 의도한
    축소다. 문장 종결 부호는 닫힌 신호이고, 그 앞뒤를 "명령+무관한 후속 문장"과 "명령+별개
    명령"으로 나누는 판단은 이 함수의 두 닫힌 문법 부류(연결어미·인용 조사) 밖이라 열지 않는다.
    **파괴적이지 않은 이유**: 놓쳐도 결과는 되물음(`cart_add` 기본값)일 뿐이고, 반대로 열면
    F19 가 막으려던 "찜 취소해줘. 무슨 뜻이야?"류(§4-B)가 다시 뚫린다."""
    assert (
        classify_cart_utterance("찜한 거 빼줘. 그리고 이것도 담아줘", SETTINGS) != "wishlist_remove"
    )


def test_known_limitation_comma_continuation_still_routes_but_loses_evidence() -> None:
    """[라운드 8 리뷰 F20] "찜 취소해줘, 장바구니는 그대로 두고"(2차 리뷰 지적 8 원래 가드,
    `test_regression_explicit_wishlist_remove_marker_ignores_cart_suppression` 참조)는
    **라우팅은 그대로 `wishlist_remove`** 다(`tail_is_command` 는 안 바꿨다). 하지만
    `has_wishlist_remove_evidence` 는 이제 종결(F20)을 요구해서 쉼표 뒤에 "장바구니는..."
    이라는 실질 내용이 남는 이 발화는 **근거가 `False`** 로 바뀌었다 — 의도한 축소다(위
    `wishlist.py` 의 `⚠️ [#440]` "라운드 8 리뷰 F20" 문단 참조). 자동 삭제 대신 되물음으로
    끝나 파괴적이지 않다."""
    message = "찜 취소해줘, 장바구니는 그대로 두고"
    assert classify_cart_utterance(message, SETTINGS) == "wishlist_remove"
    assert has_wishlist_remove_evidence(message, SETTINGS) is False


def test_wishlist_reference_markers_is_a_subset_of_wishlist_target_markers() -> None:
    """[라운드 1 리뷰 F4] `wishlist_reference_markers`(지시 수식어)는 `wishlist_target_markers`
    (#440 인접 결합 head 축)의 부분집합이어야 한다 — 지시 수식어는 전부 인접 결합의 head 도
    될 수 있어야 한다. 두 목록이 같은 개념을 각자 들고 있다가 한쪽만 고쳐지는 재발
    (`negation.py` 상단 docstring 이 이미 세 번 지적한 실패 모양)을 테스트로 못 박는다."""
    assert set(SETTINGS.wishlist_reference_markers) <= set(SETTINGS.wishlist_target_markers)


# ─────────── §4-F 규칙 1(이름 매칭) — "이름을 댔다" ≠ "그 이름이 명령의 대상이다"
# (라운드 10 리뷰 F26 → 라운드 11 리뷰 F28 로 등급 통합) ───────────

RULE_1_BLOCKED = [
    # F26 원 재현 — 인용부호 목록은 F28 에서 삭제됐지만, 전체 왼쪽 앵커가 같은 발화를 더
    # 강하게 막는다(부호 유무와 무관하게 "닫힌 접두가 아니면 무효").
    "다음 문구를 번역해줘: '이어폰 찜 빼줘'",
    "사용자가 말한 건 '이어폰 찜 빼줘'",
    "문구 예시는 (이어폰 찜 빼줘)",
    "이어폰 찜 빼줘, 이 표현이 맞아?",
    "이어폰 찜 빼줘 이 표현이 맞아?",
    "이어폰 찜 빼줘; 무슨 뜻이야?",
    # F28 재현 — 부호 없는 간접화법·화살표·대괄호(F26 의 인용부호 목록 밖).
    "사용자가 말한 건 이어폰 찜 빼줘",
    "예시 → 이어폰 찜 빼줘",
    "문구 예시는 [이어폰 찜 빼줘]",
    # F28 재현 — 다중 이름 사슬의 마지막 노드가 미종결이면 사슬 전체가 무효
    # (`has_terminated_name_tail` 전역 게이트, 찜 [이어폰, 케이스] 필요).
    "이어폰이랑 케이스 찜 빼줘, 이 표현이 맞아?",
    # F28 받아들이는 축소 — 열린 접두("내가 산")는 그 이름이 지금 지목한 대상인지
    # 증명할 수 없다. 잃는 것은 되물음 한 번(비파괴적).
    "내가 산 이어폰 찜 빼줘",
    # [라운드 13 리뷰 F31] 라운드 12(F30)의 "부정 표지 창 점프"가 이 셋을 실제로 삭제했다 —
    # 아는 어휘를 하나도 소비 못 한 위치("예: "·"문구: "·"예시: ")에서 부정 표지("말고")가
    # 창 안에 있다는 이유만으로 그 앞을 통째로 건너뛰었다(실측, 파괴적 — 케이스(20)가 삭제됨).
    # 창 점프를 없애고 앵커 스캔 전용 어간(`wishlist_remove_action_stems`)으로 대체한 뒤에는
    # 이 셋 모두 "예"·"문구"·"예시" 자체가 아는 어휘가 아니라서 다시 막힌다.
    "예: 이어폰 말고 케이스 찜 빼줘",
    "문구: 이어폰 말고 케이스 찜 빼줘",
    "예시: 이어폰 말고 케이스 찜 빼줘",
    # [라운드 14 리뷰 F34] 인용·삽입 부호가 발화 **첫 글자**인 경로 — 위 인용부호·괄호 항목은
    # 전부 그 앞에 미지 어휘("문구 예시는" 등)가 있어 그 어휘 때문에 우연히 막혔을 뿐, 왼쪽
    # 앵커가 인용부호 자체를 건너뛰는 경로를 실제로 타지 않았다. 첫 글자부터 시작하면 옛
    # 구현(`_is_boundary_char` 전체를 스킵)은 이 전부를 통과시켜 인용된 상품명을 실제로
    # 삭제했다(실측, 파괴적) — `_UTTERANCE_CLAUSE_SEPARATORS`(쉼표·세미콜론·가운뎃점만)로
    # 좁힌 뒤에는 전부 다시 막힌다.
    "'이어폰 찜 빼줘'",
    '"이어폰 찜 빼줘"',
    "(이어폰 찜 빼줘)",
    "[이어폰 찜 빼줘]",
    "{이어폰 찜 빼줘}",
    ": 이어폰 찜 빼줘",
]

RULE_1_NORMAL_UTTERANCES_RESOLVE = [
    "이어폰 찜 빼줘",
    "이어폰 찜 빼줘요",
    "그 이어폰 찜 빼줘",
    "내 이어폰 찜 빼줘",
]


@pytest.mark.parametrize("message", RULE_1_BLOCKED)
def test_rule_1_blocked_utterances_do_not_resolve(message: str) -> None:
    """[라운드 10 리뷰 F26 → 라운드 11 리뷰 F28] 규칙 1(이름 매칭)도 규칙 2·3 과 같은 전체
    왼쪽 앵커를 받는다 — 인용부호가 있든 없든("내가 산"처럼 정상적인 수식어처럼 보여도),
    닫힌 접두가 아니면 그 이름이 지금 지목한 대상인지 증명할 수 없다. 다중 이름 사슬은 마지막
    노드가 종결에 실패하면 전역 게이트(`has_terminated_name_tail`)가 사슬 전체를 막는다."""
    items = [_item(10, "이어폰"), _item(20, "케이스")]
    result = _resolve_wishlist_remove_target(CartIntent(product_id=None), message, items, SETTINGS)
    assert result is None


@pytest.mark.parametrize("message", RULE_1_NORMAL_UTTERANCES_RESOLVE)
def test_rule_1_normal_utterances_still_resolve(message: str) -> None:
    """[라운드 11 리뷰 F28] 정상 발화는 전체 왼쪽 앵커를 그대로 통과해야 한다 — 이름 앞이
    비어 있거나 닫힌 접두(`wishlist_remove_prefix_words`)뿐이면 된다. 열린 접두("내가 산")는
    이제 막힌다(위 `RULE_1_BLOCKED` 참조, 받아들이는 축소)."""
    items = [_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(CartIntent(product_id=None), message, items, SETTINGS)
    assert result is not None
    assert result.product_id == 10


@pytest.mark.parametrize("message", ["이어폰 찜 빼줘도 될까?", "이어폰 찜 빼줘야 하나?"])
def test_rule_1_hedge_gate_still_blocks(message: str) -> None:
    """[라운드 10 리뷰 F26 검증표] 기존 hedge 게이트(종결 판정)는 F28 이후에도 그대로
    유지된다 — 회귀 방지."""
    items = [_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(CartIntent(product_id=None), message, items, SETTINGS)
    assert result is None


def test_rule_1_ambiguous_chain_without_trailing_junk_still_asks() -> None:
    """[라운드 11 리뷰 F28] "이어폰이랑 케이스 찜 빼줘"(메타언어 없음)는 사슬의 진짜
    tail("케이스"+"찜 빼줘")이 정상 종결되므로 전역 게이트를 통과하고, 두 이름 모두
    유효해 모호(되물음)로 남아야 한다 — `test_wishlist_flow.py::test_resolve_wishlist_
    remove_target_ambiguous_listing_with_particle_still_asks` 와 같은 사실을 이 파일의
    §4 표에도 고정한다."""
    items = [_item(10, "이어폰"), _item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰이랑 케이스 찜 빼줘", items, SETTINGS
    )
    assert result is None


RULE_1_NEGATION_CONTRAST_UTTERANCES = [
    "이어폰 찜 빼지 말고 케이스 찜 빼줘",
    "이어폰은 찜 빼지 말고 케이스 찜 빼줘",
    "이어폰은 찜 빼지 말고 케이스는 찜 빼줘",
    # [라운드 13 리뷰 F31/F32] 쉼표·세미콜론이 낀 변형 — "말고" 뒤 브리지에 문장부호가 있어도
    # 그 자체는 이미 아는 경계문자다(`negation._is_boundary_char`). F30(창 점프)은 이 변형을
    # 우연히 살렸지만, 창 점프를 없앤 뒤엔 앵커 스캔이 문장부호도 건너뛰게 하는 게 정식 수정이다
    # (F31-3) — 쉼표 하나로 정상 대조 발화가 죽던 회귀(F32)를 이렇게 고친다.
    "이어폰은 찜 빼지 말고, 케이스 찜 빼줘",
    "이어폰은 찜 빼지 말고; 케이스 찜 빼줘",
]


@pytest.mark.parametrize("message", RULE_1_NEGATION_CONTRAST_UTTERANCES)
def test_rule_1_negation_across_two_items_still_picks_the_unnegated_one(message: str) -> None:
    """[라운드 12 리뷰 F30, F28 회귀 복구 → 라운드 13 리뷰 F31/F32 로 우회 없이 재확인]
    "이어폰(은) 찜 빼지 말고 케이스(는) 찜 빼줘"는 라운드 11(F28)의 좁은 앵커(닫힌 접두
    목록만)에 걸려 되물음으로 잘못 축소됐었다(`test_wishlist_flow.py` 의 #116/#117 부정·대조
    회귀 가드 5건이 실제로 깨졌다 — 임의로 고치지 않고 그대로 보고했다). "이어폰은 찜 빼지
    말고"는 임의의 텍스트가 아니라 이 판정이 이미 아는 어휘(다른 후보 상품명·head·tail
    어간+부정 표지)라서, F30 은 앵커 어휘를 그 전부로 넓혀 "케이스"만 정확히 해소되게
    되살렸다. F31 은 그 구현(부정 표지 "창 점프")이 우회였다는 걸 확인하고 앵커 스캔 전용
    어간(`wishlist_remove_action_stems`)으로 대체했다 — 이 파라미터화가 그 대체 뒤에도 같은
    결과가 나오는지 재확인한다. 쉼표·세미콜론 변형(F32)도 여기서 함께 고정한다."""
    items = [_item(10, "이어폰"), _item(20, "케이스")]
    result = _resolve_wishlist_remove_target(CartIntent(product_id=None), message, items, SETTINGS)
    assert result is not None
    assert result.product_id == 20


@pytest.mark.parametrize("message", RULE_1_NEGATION_CONTRAST_UTTERANCES)
def test_rule_1_negation_across_two_items_asks_when_the_other_item_is_not_in_the_wishlist(
    message: str,
) -> None:
    """[라운드 12 리뷰 F30 검증표] 위 발화라도 찜 목록에 "케이스"가 없으면(기존 가드) 여전히
    되물음이어야 한다 — 앵커 어휘 확장이 "이어폰"의 부정 자체를 무력화하지 않는다."""
    items = [_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(CartIntent(product_id=None), message, items, SETTINGS)
    assert result is None


def test_known_limitation_open_connective_after_negation_still_blocks() -> None:
    """[라운드 13 리뷰 F32] "이어폰은 찜 빼지 말고 **대신** 케이스 찜 빼줘" — 열린 연결어미
    "대신"이 낀 변형은 여전히 되물음으로 막힌다(의도한 축소, 앵커 어휘에 "대신"을 넣지 않았다).
    "대신"류 열린 연결어미를 나열로 덮으려 하면 F16 이 이미 겪은 실패(유보·허가 표현의 열린
    집합)가 그대로 반복된다 — **파괴적이지 않은 이유**: 놓쳐도 결과는 되물음일 뿐이다."""
    items = [_item(10, "이어폰"), _item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None),
        "이어폰은 찜 빼지 말고 대신 케이스 찜 빼줘",
        items,
        SETTINGS,
    )
    assert result is None


# ─────────── §4-G 규칙 1·2·3 이 같은 근거를 공유한다 (라운드 13 리뷰 F33) ───────────

# [라운드 13 리뷰 F33] 지금까지 §4-B FALSE_POSITIVES 는 찜 상품명을 실제로 담은 발화가 하나도
# 없었다 — 그래서 "ev=False ⟹ 규칙 1도 None"이라는 불변식에서 규칙 1(이름 매칭) 경로가 한 번도
# 실제로 검사되지 않았다. 아래 목록은 발화 안에 **실제 찜 상품명("이어폰")이 들어간** 거짓양성만
# 모은다 — F31 재현 3종(인용·메모 접두) + 기존 §4-B/§4-F 패턴에 이름을 끼워 넣은 변형.
RULE_123_FALSE_POSITIVES_WITH_REAL_NAME = [
    "예: 이어폰 말고 케이스 찜 빼줘",
    "문구: 이어폰 말고 케이스 찜 빼줘",
    "예시: 이어폰 말고 케이스 찜 빼줘",
    "다음 문구를 번역해줘: '이어폰 찜 빼줘'",
    "이어폰 찜 빼줘도 될까?",
    "이어폰 찜 취소해줘라는 말이 뭐야?",
]


@pytest.mark.parametrize("message", RULE_123_FALSE_POSITIVES_WITH_REAL_NAME)
def test_false_positives_with_real_product_name_have_no_evidence(message: str) -> None:
    """[라운드 13 리뷰 F33] "근거 하나로 통일"이 사실이려면 `has_wishlist_remove_evidence`가
    `False` 인 발화는 규칙 1(이름 매칭)에도 그대로 막혀야 한다 — 이 목록으로 그 전제(근거
    자체가 False)부터 고정한다."""
    assert has_wishlist_remove_evidence(message, SETTINGS) is False


@pytest.mark.parametrize("message", RULE_123_FALSE_POSITIVES_WITH_REAL_NAME)
def test_false_positives_with_real_product_name_resolve_to_none_via_any_rule(
    message: str,
) -> None:
    """[라운드 13 리뷰 F33] `has_wishlist_remove_evidence(m) is False` ⟹
    `_resolve_wishlist_remove_target(...)` 는 규칙 1·2·3 **어느 경로로도** `None` 이다 — 찜
    1건·2건, `cart.product_id` 가 실제 항목을 가리키는 최악 상태(규칙 2 가 열릴 수 있는 조건)
    를 모두 태운다. 발화 안에 실제 상품명이 들어 있어 규칙 1(이름 매칭)도 이 발화들에서
    처음으로 검사된다."""
    one_item = [_item(10, "이어폰")]
    two_items = [_item(10, "이어폰"), _item(20, "케이스")]
    for items in (one_item, two_items):
        for product_id in (None, items[0].product_id):
            result = _resolve_wishlist_remove_target(
                CartIntent(product_id=product_id), message, items, SETTINGS
            )
            assert result is None


# ─────────── §4-E 종단(end-to-end) ───────────


async def test_e2e_stream_wishlist_remove_removes_the_resolved_item() -> None:
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="찜한 거 빼줘",
            settings=SETTINGS,
            get_wishlist_fn=_wishlist(_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [10]
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_e2e_stream_wishlist_remove_view_phrase_asks_and_calls_nothing() -> None:
    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("조회 발화인데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="내 찜 뭐야",
            settings=SETTINGS,
            get_wishlist_fn=_wishlist(_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_e2e_route_cart_add_delegates_when_decompose_misroutes_to_cart_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decompose 가 `cart_add` 로 오분류한 경우 — `stream_cart_add` 안의 2선 방어가 찜 해제로
    위임한다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[_item(10, "이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        assert product_id == 10
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="찜한 거 빼줘"), _member(), llm=llm))
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_e2e_route_cart_remove_corrects_to_wishlist_remove_when_decompose_misroutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5 — decompose 가 `cart_remove` 를 산출해도 "찜한 거 빼줘" 는 찜 해제로 정정된다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[_item(10, "이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        assert product_id == 10
        return None

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("정정됐어야 하는데 stream_cart_remove 가 호출됐다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="찜한 거 빼줘"), _member(), llm=llm))
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_e2e_route_cart_remove_stays_cart_remove_for_ordinary_cart_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5 회귀 — decompose 가 `cart_remove` 를 산출한 평범한 장바구니 삭제 발화는 정정 없이
    그대로 `stream_cart_remove` 로 간다(정정은 wishlist_remove 판정일 때만)."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc
    from app.schemas.spring import CartView, CartViewItem

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="키보드", quantity=1)]
        )

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        assert cart_item_id == 1
        return None

    async def fake_get_wishlist(user_id):
        raise AssertionError("찜 해제 판정이 아닌데 get_wishlist 가 호출됐다")

    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="키보드 빼줘"), _member(), llm=llm))
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_e2e_f24_quoted_wishlist_phrase_does_not_swallow_a_cart_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 9 리뷰 F24] decompose 가 `cart_remove` 를 낸 발화("문구 '찜 해제해줘' 대신
    키보드 빼줘")에 인용된 찜 문구가 있으면, `has_wishlist_remove_evidence` 가 없는 한(F22 —
    발화 전체가 해제 명령이 아니다) `corrected_to_wishlist_remove` 게이트가 열리지 않는다 —
    정정 없이 그대로 `stream_cart_remove` 로 가서 사용자가 실제로 요청한 장바구니 삭제가
    일어나야 한다. 게이트가 없으면 정정이 이 요청을 삼켜 키보드가 안 지워진다(실측, 파괴적)."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    deleted: list[int] = []

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="키보드", quantity=1)]
        )

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)
        return None

    async def fake_get_wishlist(user_id):
        raise AssertionError("정정 없이 stream_cart_remove 로 가야 하는데 get_wishlist 가 호출됐다")

    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {}})
    events = await _collect(
        run_buyer_turn(_req(message="문구 '찜 해제해줘' 대신 키보드 빼줘"), _member(), llm=llm)
    )
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_e2e_f24_no_evidence_delegation_preserves_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 9 리뷰 F24] pending(옵션 되물음) 중 `"찜 취소해줘 이 표현이 맞아?"`(evidence
    없음, F22 재현)는 decompose 가 `cart_add` 로 내도 `stream_cart_add` 의 2선 방어가 찜
    해제로 위임하면 안 된다 — 위임하면 `clear_pending` 이 실행돼 진행 중이던 옵션 되물음이
    사라진다(실측, 파괴적). evidence 가 없으면 위임하지 않고 오늘 동작(담기 흐름, pending
    유지)으로 남는다."""
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption
    from app.services.spring_client import CartOptionRequired
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        raise AssertionError("근거 없는 발화인데 위임돼 get_wishlist 가 호출됐다")

    async def fake_remove_wishlist(product_id, *, user_id=None):
        raise AssertionError("근거 없는 발화인데 위임돼 remove_wishlist 가 호출됐다")

    async def fake_add(req):  # noqa: ANN001
        # 옵션 답변으로 해석되지 않으면 오늘 동작대로 옵션 되물음이 다시 난다(pending 유지).
        raise CartOptionRequired(
            [
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ]
        )

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = _req(message="찜 취소해줘 이 표현이 맞아?", thread_id="t-f24-pending")
    identity = _member()
    key = await _thread_key(request, identity)
    store = await get_cart_store()
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )

    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {}})
    events = await _collect(run_buyer_turn(request, identity, llm=llm))
    assert "WISHLIST_REMOVED" not in [a.get("type") for a in _actions(events)]
    pending = await store.get_pending(key)
    assert pending is not None
    assert pending.product_id == 9001


async def test_e2e_route_wishlist_remove_direct_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decompose 가 이미 `wishlist_remove` 를 직접 산출한 경우(세 번째 경우) — 그대로 동작한다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[_item(10, "이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        assert product_id == 10
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="찜한 거 빼줘"), _member(), llm=llm))
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


# ─── §4-E 후속 정정(#440 followup) — `wishlist_remove` → `cart_remove` 역방향 ───


async def test_e2e_followup_wishlist_remove_corrects_to_cart_remove_for_food_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decompose 가 `"찜닭 빼줘"`(음식명 + 장바구니 삭제 의도)를 `wishlist_remove` 로 오분류해도,
    결정론 계층은 `cart_remove` 로 본다("찜닭"의 "찜"이 어절 경계를 통과 못한다) — 정정돼 장바구니
    항목이 실제로 삭제되고 찜(이어폰)은 건드리지 않는다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    deleted: list[int] = []

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="찜닭", quantity=1)]
        )

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)
        return None

    async def fake_get_wishlist(user_id):
        raise AssertionError("정정 후에는 장바구니 삭제로 가야 하는데 get_wishlist 가 호출됐다")

    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="찜닭 빼줘"), _member(), llm=llm))
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_e2e_followup_wishlist_remove_corrects_to_cart_remove_for_ribs_stew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 정정 — `"갈비찜 빼줘"`(`"갈비찜"`의 `"찜"`은 왼쪽에 `"비"`가 바로 붙어 경계를
    통과 못한다)."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    deleted: list[int] = []

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="갈비찜", quantity=1)]
        )

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)
        return None

    async def fake_get_wishlist(user_id):
        raise AssertionError("정정 후에는 장바구니 삭제로 가야 하는데 get_wishlist 가 호출됐다")

    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="갈비찜 빼줘"), _member(), llm=llm))
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_e2e_followup_wishlist_remove_stays_wishlist_remove_without_marker_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대조군(정정 조건 3번) — `"이어폰 빼줘"`는 발화에 `wishlist_target_markers` 부분 문자열이
    전혀 없다(`"찜"` 자체가 없다) — 정정되면 **안 된다**. 정정되면 규칙 1(이름 매칭)이 처리해야
    할 정상 찜 해제 경로가 장바구니로 새 버린다(이 조건이 빠지면 재현되는 결함, findings 문서
    §B ⚠️ 참조). `"찜"` 이 전혀 없는 이 발화는 `has_wishlist_remove_evidence` 도 애초에 `False`
    라 규칙 2·3(문맥 id·목록 1건 자동)이 열리지 않고, 규칙 1(이름 매칭)도 트레일링 표지가
    `wishlist_remove_markers`(`"찜 빼줘"`류)뿐이라 bare `"빼줘"` 는 못 잡는다 — 그래서 실제
    결과는 삭제가 아니라 **되물음**이다(직접 실측 확인). 이 테스트가 지키는 성질은 "삭제가
    일어난다"가 아니라 "장바구니 쪽으로 잘못 정정돼 엉뚱한 항목(키보드)이 지워지지 않는다"다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[_item(10, "이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        raise AssertionError("근거 없는 발화인데 remove_wishlist 가 호출됐다")

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("정정되지 않아야 하는데 stream_cart_remove 가 호출됐다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="이어폰 빼줘"), _member(), llm=llm))
    assert not _actions(events)


async def test_e2e_followup_wishlist_remove_explicit_marker_stays_wishlist_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """무회귀 — `"찜 빼줘"`(사다리 1번 명시 매칭)는 `classify_cart_utterance` 도 `wishlist_remove`
    로 보므로 정정 조건 2번(`== "cart_remove"`)이 거짓이라 정정되지 않는다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[_item(10, "이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        assert product_id == 10
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="찜 빼줘"), _member(), llm=llm))
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_e2e_followup_wishlist_remove_view_phrase_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """무회귀 — `"내 찜 뭐야"`(조회 발화)는 근거가 없어(§4-B) 삭제 0회·되물음으로 남아야 한다
    (`classify_cart_utterance` 기본값이 `cart_add` 라 정정 조건 2번도 애초에 거짓이다)."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[_item(10, "이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        raise AssertionError("조회 발화인데 remove_wishlist 가 호출됐다")

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("조회 발화인데 delete_cart_item 이 호출됐다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="내 찜 뭐야"), _member(), llm=llm))
    assert not _actions(events)


def test_has_deceptive_wishlist_marker_true_for_food_name_false_positives() -> None:
    assert has_deceptive_wishlist_marker("찜닭 빼줘", SETTINGS) is True
    assert has_deceptive_wishlist_marker("갈비찜 빼줘", SETTINGS) is True


def test_has_deceptive_wishlist_marker_false_when_no_marker_substring() -> None:
    assert has_deceptive_wishlist_marker("이어폰 빼줘", SETTINGS) is False


def test_has_deceptive_wishlist_marker_false_for_boundary_passing_head() -> None:
    """`"찜 빼줘"`는 `"찜"` 이 어절 경계를 통과하는 정상 head 라 거짓양성 서명이 아니다."""
    assert has_deceptive_wishlist_marker("찜 빼줘", SETTINGS) is False


async def test_e2e_guest_gets_login_notice_regardless_of_phrasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """게스트/비회원 — "찜한 거 빼줘" 로도 로그인 안내 후 종료(찜 게이트 무변화)."""

    async def get_wishlist_fn(user_id):
        raise AssertionError("게스트인데 get_wishlist_fn 이 호출됐다")

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("게스트인데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_guest(),
            cart=CartIntent(product_id=None),
            message="찜한 거 빼줘",
            settings=SETTINGS,
            get_wishlist_fn=get_wishlist_fn,
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)
