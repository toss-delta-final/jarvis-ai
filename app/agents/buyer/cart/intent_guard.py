"""`cart_add` 로 라우팅된 발화의 삭제·찜 오담기 방어 (이슈 #116·#117, 패킷 §4).

이 판별기가 생긴 당시에는 decompose(app/agents/buyer/recommendation/decompose.py)가 다른
이슈(#84) 소유라 이 레인에서 `cart_remove`/`wishlist_add`/`wishlist_remove` intent 를 새로
만들 수 없었다. 대신 `cart_add` 로 이미 들어온 발화 중 **명백한** 것만 결정론적으로 갈라낸다
— LLM 을 새로 부르지 않고, 프롬프트도 고치지 않는다.

**[#116·#117 이후]** 그 세 intent 는 decompose 가 직접 산출하게 됐고(`RouteDecision.intent`),
이 판별기는 **2선 방어**로 남았다 — decompose 가 여전히 `cart_add` 로 오분류한 발화를 여기서
다시 갈라낸다(`buyer/graph.py` 라우팅 docstring 참조).

`cart_add` 로 라우팅되지 않는 발화는 이 판별기가 **구조적으로 보지 못한다**(결함이 아니라
경계). 반환 어휘에 조회 계열(`cart_view`·`wishlist_view`)이 없는 것도 같은 이유다 — 조회
발화는 애초에 여기 도달하지 않는다.

**[#386 → #440] 찜 조회 오분류 방어의 소재지가 바뀌었다.** #386 당시엔 "이 파일이 아니라
`wishlist.py::_resolve_wishlist_remove_target` 의 조회 표지 가드가 맡는다"였는데, 그 가드
(표지 목록·부분 문자열 결합)가 둘 다 실패해 #440 으로 옮겨졌다(아래 `has_wishlist_remove_evidence`
docstring 참조). 지금 구조는: **판정은 여기(`has_wishlist_remove_evidence`), 적용은 양쪽**
— `classify_cart_utterance` 의 사다리 1-b(아래)와 `wishlist.py::_resolve_wishlist_remove_target`
의 규칙 2·3 게이트가 **같은 함수**를 부른다. 판정을 한곳에 두는 이유는 이 모듈 상단에 이미
적은 원칙과 같다 — 다음에 또 한쪽만 고치는 재발을 구조적으로 막는다.
"""

from __future__ import annotations

from app.agents.buyer.cart.negation import (
    _spans,
    has_boundary_passing_head,
    has_prefix_negation,
    matches_pair_unnegated,
    matches_unnegated,
    matches_unnegated_left_bounded,
    matches_unnegated_left_bounded_with_noun_ending,
)

# 부정·유보 판정은 `negation.py` 에서 가져온다(라운드 10) — 이 판정을 이 파일과 `remove.py` 가
# 각자 구현했다가 라운드 9 에서 이 파일만 접두 부정을 배웠고 `remove.py` 는 못 배웠다(플래그
# on 시 실제 데이터 손실로 재현). `_matches_unnegated` 라는 이름을 이 모듈 안에서 계속 쓰기
# 위해 별칭을 둔다 — 아래 함수 전체를 다시 쓰지 않아도 되고, 이 모듈의 다른 곳에서 이 이름을
# 참조하는 코드(테스트 포함)와의 diff 도 작아진다.
_matches_unnegated = matches_unnegated


def _matches_marker_excluding_reference(
    message: str, markers: list[str], reference_markers: list[str], settings
) -> bool:
    """`markers` 매칭 — 부정(뒤쪽 어미·앞쪽 접두, `_matches_unnegated` 와 같은 원리)에 더해 과거
    참조형(2차 리뷰 N-1)도 배제하는 공용 창 기계.

    표지 하나가 "담아"·"위시리스트에 넣어"처럼 어간만 있으면 "담아뒀던"·"넣어놓은"류 과거
    참조형에도 부분 문자열로 걸린다 — 이 PR 에서 `docs/lessons.md` 에 적은 "전부" ⊂ "전부터"
    실수의 재발이다. `wishlist_reference_markers` 가 "찜한"을 지시 수식어로 다루는 것과 같은
    개념으로, 표지 바로 뒤 짧은 창에 과거 참조 꼬리(`reference_markers`)가 오면 그 출현은 동작
    요청이 아니므로 세지 않는다. 부정 검사와 같은 창 기계(`negation._spans` + 창 슬라이스)를
    재사용하되, 부정·과거 참조는 서로 다른 개념이라 한 목록으로 합치지 않고 별도 배제로 둔다
    (표지가 왜 안 걸렸는지 진단하기 쉽다).

    **[라운드 18, F2]** 원래 `cart_add_markers` 전용(`_matches_cart_add_marker`, 라운드 8)이던
    이 창 기계를 `markers`/`reference_markers` 를 인자로 받도록 일반화했다 — `wishlist_add_markers`
    의 "위시리스트에 넣어"도 같은 어간형이라 "위시리스트에 넣어놓은 거 있어요?"(질문·과거 참조)가
    찜 추가로 오분류됐다. 새 판정을 만들지 않고 이 창 기계를 그대로 재사용한다(호출부 참조).
    """
    negation_markers = settings.utterance_negation_markers
    prefix_negation_markers = settings.utterance_prefix_negation_markers
    window = settings.utterance_negation_window
    for marker in markers:
        for start, end in _spans(message, marker):
            following = message[end : end + window]
            if any(neg in following for neg in negation_markers):
                continue
            if has_prefix_negation(message, start, prefix_negation_markers):
                continue
            if any(ref in following for ref in reference_markers):
                continue
            return True
    return False


def _matches_cart_add_marker(message: str, settings) -> bool:
    """`cart_add_markers` 매칭 — `_matches_marker_excluding_reference` 를 `cart_add_markers`/
    `cart_add_reference_markers` 로 인스턴스화한다(라운드 8 원안, 라운드 18 에서 창 기계를
    공용화하며 이 함수는 얇은 래퍼로 남는다 — 테스트를 포함해 기존 호출부와의 이름 호환 유지)."""
    return _matches_marker_excluding_reference(
        message, settings.cart_add_markers, settings.cart_add_reference_markers, settings
    )


def _matches_wishlist_remove_pair(
    message: str,
    settings,
    *,
    suppress_wishlist: bool | None = None,
    require_termination: bool = False,
    prefix_words: list[str] | None = None,
) -> bool:
    """찜 해제 인접 결합(#440, 사다리 1-b 의 근거) — `negation.matches_pair_unnegated` 를
    `wishlist_target_markers`(head)·`wishlist_remove_action_markers`(어미 동작구 tail)·
    `wishlist_remove_action_nouns`(어미 없는 명사형 tail, 라운드 3 리뷰 F8)·
    `wishlist_remove_bridge_words`(head-tail 사이 닫힌 어휘, 라운드 3 리뷰 F7)로
    인스턴스화하고, **"장바구니" 억제**를 더한다.

    브리지 낱말은 `wishlist_remove_bridge_words` 와 `utterance_name_trailing_filler_words`
    (상품명 오른쪽 경계가 쓰는, 뜻 없는 부사·수량사 목록)를 **여기서 합친다** — 새 목록으로
    베끼지 않는다(`config.py` `wishlist_remove_bridge_words` 주석 참조).

    `suppress_wishlist` 를 넘기지 않으면(`None`) 이 함수가 직접 계산한다 — 이 함수의 유일한
    다른 호출부(`has_wishlist_remove_evidence`)는 그 값을 미리 계산해 둘 이유가 없어서다.
    `classify_cart_utterance` 는 같은 값을 2번(`wishlist_add`)과도 공유해야 해서(중복 계산
    금지) 미리 계산한 값을 넘긴다.

    억제 근거: `"찜한 거 장바구니에서 빼줘"` 는 장바구니 삭제 요청이지 찜 해제가 아니다. 계산은
    `wishlist_add`(사다리 2번)가 쓰는 `suppress_wishlist_add` 와 **완전히 같다**(`"장바구니"`
    가 부정되지 않은 출현으로 있는지) — 새로 구현하지 않는다. 이 억제는 **1-b 에만** 걸린다.
    사다리 1번(`wishlist_remove_markers`·`wishlist_remove_noun_markers` 명시 매칭)에는 걸지
    않는다 — "찜 취소해줘, 장바구니는 그대로 두고"가 명시적 해제 요청인데도 죽던 2차 리뷰
    지적 8 과 같은 함정이라, 그 규약을 1-b 까지 넓히지 않는다(`classify_cart_utterance`
    사다리 1번 문단 참조).

    **[라운드 5 리뷰 F13 → 라운드 6 리뷰 F16 대체]** tail 부정 검사는 `settings.utterance_
    negation_markers` 그대로 쓴다 — F13 이 넣었던 `wishlist_remove_hedge_markers`(유보·의문
    표지를 나열한 목록)는 F16 에서 삭제됐다. 유보·허가는 이제 `matches_pair_unnegated` 내부의
    `tail_is_command`(연결어미 "도"/"야", 인용 조사 — 닫힌 문법 요소) 가 구조로 가른다(`config.py`
    `utterance_hedge_connectives`·`utterance_quotative_markers` 주석 참조). `"장바구니"` 억제
    계산은 원래 목록만 쓴다(변경 없음).
    """
    negation_markers = settings.utterance_negation_markers
    prefix_negation_markers = settings.utterance_prefix_negation_markers
    window = settings.utterance_negation_window
    if suppress_wishlist is None:
        suppress_wishlist = _matches_unnegated(
            message, ["장바구니"], negation_markers, window, prefix_negation_markers
        )
    if suppress_wishlist:
        return False
    return matches_pair_unnegated(
        message,
        settings.wishlist_target_markers,
        settings.wishlist_remove_action_markers,
        settings.wishlist_remove_action_nouns,
        settings.utterance_action_verb_suffixes,
        negation_markers,
        window,
        prefix_negation_markers,
        settings.utterance_dependent_nouns,
        settings.utterance_name_boundary_particles,
        settings.wishlist_remove_bridge_words + settings.utterance_name_trailing_filler_words,
        settings.utterance_hedge_connectives,
        settings.utterance_quotative_markers,
        require_termination=require_termination,
        prefix_words=prefix_words,
    )


def _wishlist_remove_command_matches(
    message: str,
    settings,
    *,
    require_termination: bool,
    prefix_words: list[str] | None,
) -> bool:
    """명시적 해제 동작 구(`wishlist_remove_markers`, 사다리 1번과 같은 매칭) **또는** 명사형
    해제 표지(`wishlist_remove_noun_markers`, 사다리 1-a 와 같은 매칭, 라운드 3 리뷰 F8)
    **또는** 인접 결합(`_matches_wishlist_remove_pair`, 사다리 1-b 와 같은 매칭)의 합집합이다
    (이슈 #440) — `require_termination`·`prefix_words` 를 세 매칭 모두에 똑같이 건다.

    **[라운드 9 리뷰 F22 → 라운드 11 리뷰 F28 로 정정]** F22 는 이 판정을 규칙 1(라우팅급,
    `require_termination=False`·`prefix_words=None`)과 규칙 2·3(전체 앵커,
    `require_termination=True`·`prefix_words=closed_prefix`) 두 등급으로 나눴었다 — 상품명은
    닫힌 어휘가 아니라서 규칙 1엔 전체 앵커를 못 건다고 판단했기 때문이다. **그 분리가 라운드
    10·11 에서 연속으로 구멍을 냈다**(`"내가 산 이어폰 찜 빼줘"`와 `"사용자가 말한 건 이어폰
    찜 빼줘"`는 구조가 같아서 열린 접두를 어떤 거부 목록으로도 못 가른다). F28 이 규칙 1의
    이름 매칭도 같은 전체 왼쪽 앵커를 받게 하면서(`negation.matches_name_unnegated_as_command`
    의 `prefix_words`), 이 함수는 이제 **오직 규칙 2·3(head 매칭)에만** 쓰인다 — 규칙 1은
    이름 매칭 전용 함수가 자기 몫의 앵커를 직접 받는다(같은 개념을 두 이름으로 남기지 않는다).
    """
    negation_markers = settings.utterance_negation_markers
    prefix_negation_markers = settings.utterance_prefix_negation_markers
    window = settings.utterance_negation_window
    if matches_unnegated_left_bounded(
        message,
        settings.wishlist_remove_markers,
        negation_markers,
        window,
        prefix_negation_markers,
        settings.utterance_hedge_connectives,
        settings.utterance_quotative_markers,
        require_termination=require_termination,
        prefix_words=prefix_words,
    ):
        return True
    if matches_unnegated_left_bounded_with_noun_ending(
        message,
        settings.wishlist_remove_noun_markers,
        settings.utterance_action_verb_suffixes,
        negation_markers,
        window,
        prefix_negation_markers,
        settings.utterance_hedge_connectives,
        settings.utterance_quotative_markers,
        require_termination=require_termination,
        prefix_words=prefix_words,
    ):
        return True
    return _matches_wishlist_remove_pair(
        message,
        settings,
        require_termination=require_termination,
        prefix_words=prefix_words,
    )


def wishlist_remove_known_words(settings) -> list[str]:
    """찜 해제 규칙 1(이름 앵커, `negation.matches_name_unnegated_as_command`)과 규칙 2·3
    (head 앵커, 아래 `has_wishlist_remove_evidence`)이 **공유하는** 왼쪽 앵커 어휘(#440 라운드
    13 리뷰 F33) — "앵커 판정은 같은 코드여야 한다"를 어휘 구성에서도 지킨다. 라운드 13 이전엔
    `has_wishlist_remove_evidence` 가 `wishlist_remove_prefix_words`·`wishlist_remove_bridge_
    words`·`utterance_name_trailing_filler_words`·`utterance_name_boundary_particles` 4개만
    묶어 규칙 1(`wishlist.py::_resolve_wishlist_remove_target`)의 더 넓은 목록과 **서로 다른
    앵커**를 썼다 — 그래서 `has_wishlist_remove_evidence(m) is False` 인데도 규칙 1은 통과하는
    발화가 있었다("예: 이어폰 말고 케이스 찜 빼줘" 류, 라운드 13 리뷰 F31 재현). 이 함수 하나로
    두 규칙이 같은 목록을 쓰게 한다 — 새 목록을 만들지 않고 기존 목록만 합친다.

    `wishlist_remove_action_stems`(라운드 13 리뷰 F31, `config.py` 참조 — 앵커 스캔 전용, 명령
    근거로는 쓰지 않는다)는 어간뿐 아니라 어간 + `"지"`(부정 연결어미 "-지 말다"가 붙기 직전의
    활용형, 예: "빼"+"지") 형태도 함께 등재한다 — 그러지 않으면 `"빼지 말고"`의 `"지"` 한 글자가
    (독립 토큰이 아니라서) 소비되지 않아 뒤에 이어지는 `"말고"`(부정 표지)에 도달하지 못한다.
    `"-지"`는 한국어 동사 활용의 닫힌 문법 요소(연결어미)라 나열이 아니다 — `wishlist_remove_
    action_stems` 목록이 유한한 한 이 파생도 유한하다.
    """
    stems = settings.wishlist_remove_action_stems
    return (
        settings.wishlist_remove_prefix_words
        + settings.wishlist_remove_bridge_words
        + settings.utterance_name_trailing_filler_words
        + settings.utterance_name_boundary_particles
        + settings.utterance_dependent_nouns
        + settings.wishlist_target_markers
        + settings.wishlist_remove_action_markers
        + settings.wishlist_remove_action_nouns
        + settings.wishlist_remove_markers
        + settings.wishlist_remove_noun_markers
        + settings.utterance_action_verb_suffixes
        + settings.utterance_prefix_negation_markers
        + settings.utterance_negation_markers
        + stems
        + [stem + "지" for stem in stems]
    )


def has_wishlist_remove_evidence(message: str, settings) -> bool:
    """규칙 2·3(문맥 id·목록 1건 자동)용 — **발화 전체가 해제 명령**일 때만 근거로 친다
    (이슈 #440).

    **왜 이 함수가 필요한가**: `wishlist.py::_resolve_wishlist_remove_target` 의 규칙 2(문맥
    id)·3(목록 1건 자동)은 사용자가 이름을 대지 않은 대상을 **코드가 고른다** — `wishlist_remove`
    로 오분류된 조회 발화("내 찜 뭐야")가 여기 도달하면 요청하지 않은 항목이 해제된다(파괴적).
    #386 은 이 지점에 "조회 표지가 없으면 허용" 가드를 시도했다가 되돌렸다 — 조회 표지를
    전수로 나열해야만 성립해 목록 밖 표현에서 뚫렸다. #440 은 반대 극성으로 접근한다 — "조회가
    아님을 표지로 확인"이 아니라 **"해제 근거가 있음을 확인"**한다. 이 근거가 없으면
    `wishlist_remove` 로 온 어떤 발화든(2선 방어의 오분류 포함) 규칙 2·3 을 건너뛰고 되물음으로
    내려간다 — `wishlist.py::_resolve_wishlist_remove_target` 의 게이트 참조.

    **종결 요구(`require_termination=True`, 라운드 8 리뷰 F20)** — tail 매칭 뒤(명사형은 용언
    어미까지 소비한 뒤) 남은 텍스트에 **한글·영숫자가 없어야**(라운드 9 리뷰 F23, 존댓 보조사
    "요"는 선택적으로 먼저 소비) 근거로 친다(`negation.tail_terminates_utterance`). 해제
    동작구가 **발화를 끝내야** 파괴적 자동 선택의 근거가 된다는 뜻이다 — "찜한 거 빼줘 도
    될까?"(연결어미를 띄어 씀)·"'찜 해제해줘'를 영어로 번역해줘"(따옴표+조사)·"찜 취소해줘,
    이 표현이 맞아?"(쉼표 뒤 메타언어)·"찜 취소해줘 이 표현이 맞아?"(공백 뒤 메타언어)·"찜
    취소해줘 . 무슨 뜻이야?"(종결부호 앞 공백)·"찜 취소해줘; 무슨 뜻이야?"(세미콜론) 여섯
    우회가 전부 "tail 뒤에 내용이 남아 있다"는 하나의 사실로 걸린다.

    **왼쪽 전체 앵커(`prefix_words`, 라운드 9 리뷰 F22 → 라운드 13 리뷰 F33)** — 오른쪽(종결)만
    잠그면 앞 문맥이 열려 있다. `"다음 문구를 영어로 번역해줘: '찜 해제해줘'"`·`"사용자가 말한
    건 '찜 취소해줘'"`·`"문구 예시는 (찜 취소해줘)"` 처럼 해제 문구를 **인용·번역·예시**의
    목적어로 두고 그 문구로 발화를 끝내면 종결 검사만으로는 통과한다(실측, 파괴적) — 부분
    문자열로 head/tail 을 발화 아무 데서나 찾은 뒤 한쪽만 앵커하는 한 이 부류는 계속 나온다.
    그래서 head **왼쪽**도 발화 시작부터 닫힌 어휘로 앵커한다 — `wishlist_remove_known_words`
    (규칙 1의 `known_words` 와 **글자 그대로 같은 목록**, `negation._name_left_anchor_reachable`
    도 규칙 1과 공유하는 그 함수를 그대로 쓴다). **[라운드 13 리뷰 F33]** 라운드 9~12 는 이
    목록이 프리픽스·브리지·filler·조사 4개뿐이었다 — 규칙 1(`known_words`)보다 좁아서
    `has_wishlist_remove_evidence(m) is False` 인데도 규칙 1은 통과하는 발화가 있었다(`"예:
    이어폰 말고 케이스 찜 빼줘"` 류, 실측 — F31 재현). 두 규칙이 **같은 어휘·같은 함수**로
    왼쪽을 재면서 이 간극이 없어졌다.

    **이 함수와 `classify_cart_utterance` 사이에 더 이상 불변식을 걸지 않는다(라운드 8 리뷰
    F20, 라운드 1(D4)의 정정)**. 예전엔 `classify_cart_utterance(m, s) == "wishlist_remove"`
    이면 이 함수도 반드시 `True` 여야 한다고 요구했는데, 그 요구가 이 함수의 tail 판정을 라우팅
    판정과 같게 묶어 놔서 **종결·전체 앵커를 요구할 수 없었다**. 그 불변식은 틀렸다 — 라우팅이
    느슨해도 무해하다: `wishlist_remove` 로 왔는데 이 함수가 `False` 면 규칙 1·2·3 이 전부
    막혀 **되물음**(`_wishlist_unresolved_notice`, 찜 목록을 나열)으로 끝난다. 진짜 지켜야 할
    성질은 반대 방향이다 — **이 함수가 `False` 면, 라우팅이 어디로 가든 삭제는 0회다**
    (`tests/unit/test_wishlist_remove_resolution.py` §4-C 가 이 성질을 고정한다). 라우팅은
    관대하게, 파괴적 실행 게이트는 엄격하게 — 라우팅과 근거를 같은 잣대로 묶지 않는 것이 이
    이슈 내내 옳았던 방향이다.

    **[라운드 13 리뷰 F33] "근거 하나로 통일"은 이 두 성질이 함께 성립할 때만 사실이다**: (1)
    이 함수가 `True` 를 낼 수 있는 경로는 전부 규칙 1과 같은 앵커·종결·부정 판정을 쓴다(위
    문단), (2) 이 함수가 `False` 면 규칙 1도 (규칙 1만의 추가 신호 — 상품명 자체가 발화에
    등장한다는 사실 — 이 없는 한) 통과하지 못한다. `tests/unit/test_wishlist_remove_
    resolution.py` 가 발화 안에 **실제 찜 상품명이 들어간** 거짓양성으로 이 불변식을 고정한다
    (라운드 13 이전엔 거짓양성 목록에 상품명이 없어 규칙 1 경로가 한 번도 실제로 검사되지
    않았다).
    """
    return _wishlist_remove_command_matches(
        message,
        settings,
        require_termination=True,
        prefix_words=wishlist_remove_known_words(settings),
    )


def has_deceptive_wishlist_marker(message: str, settings) -> bool:
    """[#440 후속 정정] `wishlist_target_markers` 가 발화에 **부분 문자열로는** 있지만, 그중
    어느 것도 어절 경계 검사(`negation.has_boundary_passing_head` — `matches_pair_unnegated`
    가 쓰는 head 스캔과 **같은 코드 경로**)를 통과하지 못했는지 본다. LLM 이 `찜닭`·`갈비찜`의
    `"찜"`처럼 경계를 통과하지 못하는 부분 문자열에 속아, 실제로는 장바구니 삭제 의도인 발화를
    `wishlist_remove` 로 오분류했다는 서명이다(오케스트레이터 실측 8/8,
    `evals/intent_probe/fixtures/anchors_a.json` `wishlist-remove-003` 참조).

    `buyer/graph.py::corrected_to_cart_remove`(`wishlist_remove` → `cart_remove` 역방향
    정정)의 세 번째 조건 — 이 조건이 없으면 `"찜"` 자체가 발화에 없는 경우("이어폰 빼줘")까지
    부분 문자열 부재를 "경계 통과 실패"와 같은 것으로 오인해 장바구니로 보내, 규칙 1(이름
    매칭)이 처리해야 할 정상 찜 해제 경로를 죽인다 — 그래서 부분 문자열 **존재**를 먼저
    요구한다(`tests/unit/test_wishlist_remove_resolution.py` §4-E 대조군)."""
    markers = settings.wishlist_target_markers
    if not any(marker in message for marker in markers):
        return False
    return not has_boundary_passing_head(
        message,
        markers,
        settings.utterance_dependent_nouns,
        settings.utterance_name_boundary_particles,
        settings.wishlist_remove_bridge_words + settings.utterance_name_trailing_filler_words,
    )


def classify_cart_utterance(message: str, settings) -> str:
    """'cart_add' | 'cart_remove' | 'wishlist_add' | 'wishlist_remove' | 'cart_quantity' — 확실할
    때만 갈라낸다.

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
    표지 **앞**도 `negation.has_prefix_negation` 으로 검사한다 — `utterance_negation_markers`
    와 합치지 않는다(검사 방향이 반대라 한 목록으로 두면 코드가 헷갈린다). `"안"`은 흔한
    조각이라 어절 경계로만 판정해 `"안경 빼줘"`·`"가방 안에 있는 거 빼줘"` 같은 정상 요청은
    죽이지 않는다(`negation.py` 의 함수 docstring 참조).

    **부정 판정은 `app/agents/buyer/cart/negation.py` 공용 모듈에 있다(라운드 10)**. 원래 이
    파일에 직접 구현돼 있었는데, `remove.py::_resolve_remove_targets` 가 **같은 개념을 따로
    구현**하다가 이 파일이 라운드 9 에서 접두 부정을 배울 때 `remove.py` 는 못 배워 플래그 on
    시 실제 데이터 손실로 재현됐다(부정 인지 결함 3연발의 세 번째). 그래서 판정을
    `negation.py` 로 뽑아 이 파일과 `remove.py`·`wishlist.py` 가 **같은 함수**를 쓰게 했다 —
    다음에 또 한쪽만 고치는 재발을 구조적으로 막는다.

    **담기 표지의 과거 참조형(2차 리뷰 N-1)**: `cart_add_markers` 매칭은 `_matches_unnegated`
    대신 `_matches_cart_add_marker` 를 쓴다 — 부정 배제에 더해 과거 참조 꼬리
    (`cart_add_reference_markers`: 뒀·둔·두었·놨·놓)도 같은 창에서 배제한다. "담아"는
    "담아뒀던"·"담아둔"처럼 과거 참조형에도 부분 문자열로 걸려("담아뒀던 거 다 빼줘"가 삭제
    대신 담기로 오담기), 그 출현은 지금 담아 달라는 요청이 아니므로 표지로 세지 않는다.

    **찜 추가 표지의 과거 참조형(라운드 18, F2)**: `wishlist_add_markers` 중 "위시리스트에 넣어"도
    "담아"와 같은 어간형이라 "위시리스트에 넣어놓은 거 있어요?"(질문·과거 참조)에 부분 문자열로
    걸려 찜 추가로 오분류됐다("담아" ⊂ "담아뒀던"과 같은 사고, `docs/lessons.md` 재발). 새 목록을
    만들지 않고 `cart_add_reference_markers` 를 그대로 재사용한다 — 꼬리 형태(뒀·둔·두었·놨·놓)는
    "담다"·"넣다" 어느 어간에도 똑같이 붙는 활용 어미라 표지별로 다시 정의할 이유가 없다. 2번
    (`wishlist_add_markers`) 도 `_matches_unnegated` 대신 이 창 기계(`_matches_marker_excluding_reference`)
    를 쓴다 — "찜해줘"류 나머지 표지는 전부 어미까지 갖춘 동작 구라 참조 꼬리와 겹칠 부분
    문자열이 없으므로(예: "찜해줘"는 "찜해뒀던"과 "줘"/"뒀" 지점에서 이미 갈린다) 회귀가 없다.

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
         **[라운드 2 리뷰 F6]** 매칭은 `matches_unnegated` 대신 `matches_unnegated_left_bounded`
         를 쓴다 — 왼쪽 경계 없이 순수 부분 문자열로 보면 `"찜 빼줘"` 가 `"갈비찜 빼줘"`·
         `"계란찜 빼줘"`·`"김치찜 빼줘"` 안에 그대로 들어 있어 매칭되고, `matches_pair_unnegated`
         (1-b)의 head 경계 검사를 우회해 파괴적 자동 삭제로 샜다(실측 확인). 오른쪽 경계는
         요구하지 않는다(그 함수 docstring 참조 — `"찜 취소해줘"` 안의 `"찜 취소"` 가 죽는다).
         **[라운드 5 리뷰 F13 → 라운드 6 리뷰 F16 대체]** 라운드 4(F11)는 `utterance_negation_
         markers` 자체를 넓혔다가 2번(찜 추가)·`remove.py` 도 같이 넓어져 "이거 찜해줘. 배송도
         돼?" 가 찜 추가 대신 장바구니에 담기는 회귀를 냈다(공유 목록의 fallback 이 서로 달라
         "넓혀도 안전하다"는 전제가 깨졌다). 라운드 5(F13)는 `wishlist_remove_hedge_markers`
         라는 찜 해제 전용 **목록**으로 분리해 되돌렸는데, 그 목록도 목록인 이상 "도 될"·"도
         돼"만 알고 "도 괜찮"·"도 상관없"엔 다시 뚫렸다(실측). 라운드 6(F16)은 **목록을 구조로
         바꿨다** — `tail_is_command`(연결어미 "도"/"야", 인용 조사 — 전부 닫힌 문법 요소)가
         이 단계와 1-a·1-b 에서 tail 오른쪽을 본다. 적용 범위(찜 해제 경로에만)는 F13 이 세운
         원칙 그대로다(`config.py` `utterance_hedge_connectives`·`utterance_quotative_markers`
         주석 참조).
      1-a. **[라운드 3 리뷰 F8]** `wishlist_remove_noun_markers`(`"찜 취소"`·`"찜 해제"`, 어미
           없는 명사형) 매칭 → `"wishlist_remove"`. `matches_unnegated_left_bounded_with_noun_
           ending` 을 쓴다 — 왼쪽 경계에 더해 **명사형 종결 규칙**(발화 끝이거나
           `utterance_action_verb_suffixes` 로 바로 이어질 때만)을 요구한다. 이 표지들은 뒤에
           조사·다른 낱말이 자유롭게 붙을 수 있어("취소는"·"취소선"·"취소 수수료") 1번의 왼쪽
           경계만으로는 부족하다 — "찜 취소는 어떻게 해?"·"찜 취소선 그어줘"·"찜 취소 수수료
           알려줘" 같은 조회·질문이 삭제 명령으로 읽혔다(실측, 파괴적). 원래 `wishlist_remove_
           markers`(1번)에 있던 `"찜 취소"` 를 이 목록으로 옮긴 것이라, **장바구니 억제를 받지
           않는다는 1번의 규약을 그대로 물려받는다**(`"찜 취소해줘, 장바구니는 그대로 두고"` →
           `wishlist_remove`, 기존 테스트가 고정).
      1-b. **[#440]** `_matches_wishlist_remove_pair` 매칭(인접 결합, "장바구니" 억제 포함) →
           `"wishlist_remove"`. 1·1-a(명시적 동작 구·명사형)가 못 잡는 "찜 지시 명사 + 닫힌
           어휘 브리지 + 해제 tail"("찜한 거 빼줘")를 여기서 잡는다. **반드시 3번보다 먼저**
           본다(아래 3번의 "알려진 거짓음성" 문단이 이 단계가 왜 그 판단을 뒤집는지 설명한다).
           0-a(담기)는 그대로 이 단계보다 앞이다 — "찜한 거 담아줘"는 0-a 가 이미 잡아 여기
           도달하지 않는다(#386 `wishlist-view-006`, 8/8 고정 무회귀 지점). **[라운드 3 리뷰
           F7]** tail 은 head 뒤 **닫힌 어휘 브리지** 바로 그 자리에서만 인정한다 — 거리(예전
           `pair_window`)로는 "같은 명령"을 보장하지 못했다(`negation.matches_pair_unnegated`
           docstring 참조). tail 이 어미 없는 명사형(`wishlist_remove_action_nouns`)이면
           1-a 와 같은 명사형 종결 규칙도 함께 받는다(**[라운드 3 리뷰 F8]**). **[라운드 6
           리뷰 F16]** 두 tail 계열 모두 `tail_is_command` 로 유보·인용도 추가로 걸러진다 —
           "찜한 거 빼줘도 될까?" 가 브리지·명사형 종결을 다 통과해도 여기서 죽는다.
      2. `wishlist_add_markers` 매칭(과거 참조 꼬리 배제 포함, 라운드 18) → `"wishlist_add"`. 단
         발화에 `"장바구니"`가 있으면 찜으로 가르지 않는다(계약상 찜·장바구니는 다른 자원이라
         혼동 방지). 이 억제는 **여기(찜 추가)와 1-b 에만** 걸린다(#440) — 1번(찜 해제 명시
         매칭)·4번(삭제)에는 영향 없다. `"장바구니"` 억제 계산은 1-b 앞에서 한 번만 하고 이
         단계와 공유한다(중복 계산 금지). 이 단계도 3번(`wishlist_reference_markers`) 보다
         먼저 본다 — 같은 "동사가 수식어보다 강하다" 원칙("찜한 거 찜해줘"류에도 동일 적용).
      3. `wishlist_reference_markers`(찜한·찜해둔·찜해 놓은·찜했던)가 있으면 `"cart_add"`.
           이 표지가 있는 발화의 동사는 (1·1-b·2번에서 명시적/인접 찜 동작 표지가 안 걸렸다면)
           담기다 — 찜은 지시 대상을 수식할 뿐이다("찜해둔 이어폰 담아줘"는 0-a 가 이미 잡는다.
           담기 표지 없이 지시만 있는 경우("찜해둔 거")를 방어하려고 이 단계가 따로 있다).
           **[#440] 뒤집힌 판단(과거엔 "알려진 거짓음성 — 의도한 보수성", 라운드 2 리뷰)**:
           "찜한 거 빼줘"·"찜해둔 거 지워줘"는 예전엔 여기서 `"cart_add"` 로 떨어졌다 —
           `"빼줘"`·`"지워줘"` 는 `cart_remove_markers`(4번)에 속해 1·2번(찜의 명시적 동작
           표지)이 못 잡았고, 그 발화가 "장바구니에서 빼라"인지 "찜을 풀어라"인지 부분 문자열
           표지만으로는 결정론적으로 갈릴 수 없어 애매하면 개입하지 않았다(#440 이슈 본문이
           지적한 바로 그 결함). 그때는 좁혔던 이유가 "표지 목록만으로는 조회·해제·다른 낱말
           묻힘을 가를 수 없다"는 것이었는데, 지금은 **더 정교한 판정**(어절 경계 + 닫힌 어휘
           브리지, 1-b)이 생겨 그 세 가지를 실제로 가른다 — 그래서 넓힐 수 있다. 지금 이 3번에
           그대로 남는 거짓음성은 1-b 도 못 잡는 경우(예: head·tail 사이에 브리지 밖 낱말이 낀
           경우)뿐이고, 그건 여전히 의도한 보수성이다(무한정 브리지를 넓히지 않는다, `config.py`
           `wishlist_remove_bridge_words` 주석 참조). (라운드 7 이전엔 "찜한 거 찜 취소해줘"도
           여기서 같은 이유로 거짓음성 처리됐으나, 1번이 그 표지를 명시적으로 담고 있어 순서를
           옮겨 해소했다 — 위 1번 참조.)

           **[라운드 3 리뷰 F7] 알려진 축소 — "찜한 상품 중에 이어폰 빼줘"는 이제 거짓음성
           이다.** "중에"·"이어폰"이 `wishlist_remove_bridge_words` 밖이라 1-b 의 브리지가
           끊긴다(라운드 1 §4-A 표에 있던 예시였으나 라운드 3 이후엔 §4-D "알려진 한계"로
           옮겼다 — `tests/unit/test_wishlist_remove_resolution.py` 참조). 이 방향이 맞는
           이유: 이 발화는 사용자가 상품명("이어폰")을 직접 댔으므로 `wishlist_remove` 로만
           오면 규칙 1(`wishlist.py::_resolve_wishlist_remove_target` 의 이름 매칭)이 정확히
           해소한다 — 여기서 라우팅을 놓쳐도 결과는 `cart_add`(되물음 없는 장바구니 흐름)일
           뿐이라 **파괴적이지 않다**. 반대로 브리지를 열어 이 발화를 살리면 `"찜 보고 이거
           빼줘"`·`"찜은 나중에 보고 이어폰 빼줘"`·`"찜 목록 보여주고 이거 빼줘"` 같은 서로
           다른 절의 "찜"과 "빼줘"도 같이 살아나 사용자가 요청하지 않은 항목이 삭제된다
           (실측, 파괴적) — 넓혀서 얻는 것보다 잃는 것이 크다.
      4. `cart_remove_markers` 매칭 → `"cart_remove"`.
      4-a. **[#285, I-25 §4.13]** `cart_quantity_markers`(치환 동사, "개로 바꿔"·"수량 변경" 등)
           매칭 → `"cart_quantity"`. **0-a(담기)가 여전히 이 단계보다 앞이다** — "3개로 바꿔서
           담아줘"는 "담아"가 `cart_add_markers` 에 걸려 0-a 에서 이미 `"cart_add"` 로 확정되고
           이 단계에 도달하지 않는다. 이 판별기는 decompose 가 **이미 `cart_add` 로 보낸** 발화만
           보는 2선 방어라(패킷 §5.3), "담기가 가장 강한 신호"라는 0-a 의 기존 원칙(docstring 위
           문단)을 이 새 클래스에도 그대로 적용한다 — decompose(1차)가 `cart_add`/`cart_quantity`
           를 가르는 판단은 이미 끝났고, 여기서 다시 뒤집을 근거가 없다. **함정 2**(치환 vs
           합산): `cart_quantity_increment_markers`(더 담아·하나 더·추가로 담아)가 부정되지 않은
           채 매칭되면 이 단계를 건너뛴다 — "하나 더 담아줘"류는 합산이라 `cart_quantity` 가
           아니다(대개 "담아"를 포함해 0-a 에서 이미 걸리지만, "하나 더 줘"처럼 "담아"가 없는
           변형도 방어한다). `cart_remove_markers`(4번)와 어휘가 겹치지 않아(치환 동사엔 "빼"·
           "지워" 계열이 없다) 순서가 4번의 앞이든 뒤든 결과는 같지만, decompose 사다리(1-4)와
           같은 상대 위치(삭제 확인 다음)를 유지해 두 파일을 나란히 읽기 쉽게 한다.
      5. 그 외 → `"cart_add"`(기본값).
    """
    negation_markers = settings.utterance_negation_markers
    prefix_negation_markers = settings.utterance_prefix_negation_markers
    window = settings.utterance_negation_window

    if _matches_cart_add_marker(message, settings):
        return "cart_add"

    # [라운드 6 리뷰 F16] 찜 해제 경로(1번·1-a·1-b)만 `tail_is_command`(연결어미·인용 조사)로
    # 유보·인용을 구조적으로 가른다 — 0-a(위)·2·4(아래)는 그대로 `negation_markers` 만 쓴다.
    # `config.py` `utterance_hedge_connectives`·`utterance_quotative_markers` 주석 참조.
    if matches_unnegated_left_bounded(
        message,
        settings.wishlist_remove_markers,
        negation_markers,
        window,
        prefix_negation_markers,
        settings.utterance_hedge_connectives,
        settings.utterance_quotative_markers,
    ):
        return "wishlist_remove"

    if matches_unnegated_left_bounded_with_noun_ending(
        message,
        settings.wishlist_remove_noun_markers,
        settings.utterance_action_verb_suffixes,
        negation_markers,
        window,
        prefix_negation_markers,
        settings.utterance_hedge_connectives,
        settings.utterance_quotative_markers,
    ):
        return "wishlist_remove"

    # "장바구니" 자체도 부정 문맥 검사를 거친다 — "장바구니에 넣지는 마"처럼 그 언급이 부정된
    # 절 안에 있으면(cart_add_markers 가 이미 무효화된 바로 그 절) 억제 근거로 쓰지 않는다.
    # 그러지 않으면 이 억제가 방금 위에서 부정한 담기 표지를 "장바구니 언급이 있다"는 이유로
    # 다시 살려 wishlist_add 를 막아버린다(2차 리뷰 지적 1 재현 케이스). [#440] 이 값은 1-b(찜
    # 해제 인접 결합)와 2번(찜 추가)이 공유한다 — 여기서 한 번만 계산한다(중복 계산 금지).
    suppress_wishlist = _matches_unnegated(
        message, ["장바구니"], negation_markers, window, prefix_negation_markers
    )

    # 1-b. [#440] 인접 결합(`_matches_wishlist_remove_pair`, docstring 참조) — 명시적 동작 구
    # (1번)가 못 잡는 "찜 지시 명사 + 짧은 창 안의 해제 동작 구"("찜한 거 빼줘")를 여기서 잡는다.
    # 반드시 3번(`wishlist_reference_markers`)보다 앞이어야 한다 — 뒤로 가면 "찜한"이 3번에서
    # 먼저 `cart_add` 로 떨어뜨려 이 이슈가 안 고쳐진다. 0-a(담기)는 이미 이 단계보다 앞이라
    # "찜한 거 담아줘"는 여전히 담기다(#386 `wishlist-view-006` 무회귀 지점).
    if _matches_wishlist_remove_pair(message, settings, suppress_wishlist=suppress_wishlist):
        return "wishlist_remove"

    if not suppress_wishlist and _matches_marker_excluding_reference(
        message, settings.wishlist_add_markers, settings.cart_add_reference_markers, settings
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

    # 4-a. [#285, I-25 §4.13] 함정 2 방어 — 합산 표지(더 담아·하나 더·추가로 담아)가 있으면
    # cart_quantity 로 가지 않는다(대개 "담아"를 포함해 0-a 에서 이미 걸리지만 방어적으로 둔다).
    if not _matches_unnegated(
        message,
        settings.cart_quantity_increment_markers,
        negation_markers,
        window,
        prefix_negation_markers,
    ) and _matches_unnegated(
        message,
        settings.cart_quantity_markers,
        negation_markers,
        window,
        prefix_negation_markers,
    ):
        return "cart_quantity"
    return "cart_add"
