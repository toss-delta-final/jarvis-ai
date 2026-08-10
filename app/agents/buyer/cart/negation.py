"""발화 부정·유보 판정 공용 헬퍼 (이슈 #116·#117) — 어미형·접두형을 **한 곳**에서 본다.

이 PR 에서 부정 인지 결함이 세 번 났다: 라운드 5(어미형 도입) → 라운드 9(접두형을
`intent_guard.py` 에만 추가, `remove.py` 는 미적용으로 남음) → 라운드 10(`remove.py::
_resolve_remove_targets` 의 `has_negation` 이 접두형을 못 봐 플래그 on 시 실제 데이터 손실로
재현). 세 번 다 같은 개념이 파일마다 각자 구현돼 한쪽만 고쳐진 게 원인이라, 판정을 이 모듈
하나로 뽑고 `intent_guard.py`·`remove.py`·`wishlist.py` 가 전부 같은 함수를 쓰게 한다 —
다음에 또 한쪽만 고치는 재발을 구조적으로 막는다.

**[#440] `matches_pair_unnegated` — 인접 결합(head→tail) 판정.** 찜 해제 발화("찜한 거 빼줘")를
조회 발화("내 찜 뭐야"·"찜닭 빼줘")와 가르는 문제도 같은 "판정을 한곳에" 원칙을 따른다.
#386 은 두 가지 접근을 시도했다가 둘 다 되돌렸다(`app/core/config.py` `wishlist_reference_markers`
아래 `⚠️ [#440]` 주석 참조) — ① 조회 표지 전수화는 목록 밖 표현에 뚫렸고, ② "찜 명사 ×
해제 동사" 부분 문자열 결합은 `찜닭`·`갈비찜`·`찜질방`의 `"찜"`, `빼고`의 `"빼"`처럼 짧은 표지가
다른 낱말에 묻히는 것을 걸러내지 못했다. `matches_pair_unnegated` 는 어절 경계(왼쪽 시작/경계문자
+ 오른쪽 조사 소비 뒤 경계문자) 통과한 head 출현에서만, **닫힌 어휘 브리지**(head 와 tail 사이에
브리지 낱말·조사·의존명사·filler 만 올 수 있다)로 이어진 tail 출현을 찾는다 — 함수 자체의
docstring 에 실패 사례를 예로 든다.

**[라운드 3 리뷰 F7] 거리(창)를 브리지로 교체했다.** 원래는 `tail_start - head_end <= pair_window`
로 "가까우면 매칭"이었는데, 거리만으로는 "같은 명령"을 보장하지 못한다 — `"찜 보고 이거 빼줘"`
의 head-tail 간격(7자)이 `"찜 목록에서 빼줘"`(7자)와 같아 구분되지 않았다(실측, 파괴적: 서로
다른 절의 "찜"과 "빼줘"가 같은 명령으로 오인돼 규칙 2·3 자동 삭제가 열렸다). 두 사람이 같은
명령을 말했다고 확신하려면 head 와 tail 사이에 **닫힌 어휘만** 올 수 있어야 한다 — 그 밖의
낱말이 하나라도 끼면(`"보고"`·`"나중에"`·`"중에"`처럼) 별개의 절이다. `_has_valid_name_trailing`
(상품명 오른쪽 경계, "이름 + (조사) + (filler)* + 표지")과 같은 모양의 규칙을 head→tail 에도
적용한다.

**[라운드 6 리뷰 F16] tail 오른쪽 판정을 목록에서 구조로 옮겼다.** 라운드 4·5 는 "유보·허가
표현"(`도 될`·`도 돼`·`도 되`, 나중엔 hedge 전용 목록)을 문자열로 나열했는데, 매번 목록 밖
활용("도 괜찮"·"도 상관없")이 다음 라운드에 다시 나왔다 — 한국어 유보·허가 표현은 **열린
집합**이라 나열로 끝나지 않는다. `tail_is_command` 는 그 열린 집합 대신, 앞에 붙는 **닫힌
문법 요소**(연결어미 "도"/"야", 인용 조사 "라는"/"라고" 등)를 본다 — `"도 괜찮을까"`·
`"도 상관없어"`·`"도 무방한가"` 는 무한하지만 그 앞의 연결어미 "도" 하나는 유한하다.
`wishlist_remove_hedge_markers`(라운드 5)는 이 함수가 대체해 삭제됐다.

`app/agents/buyer/cart/graph.py::_all_spans` 를 가져오지 않고 여기서 복제한다 — `graph.py` 가
`intent_guard.py`/`remove.py`/`wishlist.py` 를 가져오므로, 여기서 `graph.py` 를 가져오면 순환
임포트가 된다(순환은 이 모듈 자체에는 없다 — `intent_guard.py` 는 cart 내 다른 모듈을 가져오지
않으므로 문제가 없었지만, 그래도 판정을 한곳에 두는 편이 "다음에 또 한쪽만 고치는" 재발을
막는다).
"""

from __future__ import annotations

import re

_ORDINAL_PREFIX = re.compile(r"\d+\s*번째")
"""화면 순번 지시("3번째"·"99번째") — `_name_left_anchor_reachable`(라운드 9 리뷰 F22, 라운드
13 리뷰 F33 이후 규칙 1·2·3 공유 앵커) 전용 닫힌 **패턴**. 숫자 자체는 무한하지만 "숫자+번째"라는
문법 골격은 닫혀 있다(`app/agents/buyer/screen_reference.py::_ORDINAL` 과 같은 개념 — 그 모듈을 여기서 가져오면 순환
임포트라 패턴만 같은 뜻으로 다시 둔다). "3번째 거 찜에서 빼줘"처럼 화면 순번을 가리키는
발화는 head("찜") 앞에 실질 텍스트가 아니라 **참조 표현**이 온 것이라 "이거"·"그거"(이미
`wishlist_remove_prefix_words`)와 같은 대우를 받아야 한다 — 인용·번역·예시(F22 가 막으려는
대상)와는 다르다."""


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    """겹치는 경우까지 needle 의 모든 [start, end) 출현 구간을 돌려준다."""
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while (index := text.find(needle, start)) >= 0:
        spans.append((index, index + len(needle)))
        start = index + 1
    return spans


def has_prefix_negation(message: str, start: int, prefix_markers: list[str]) -> bool:
    """`start` 위치에서 시작하는 표지 바로 **앞**에 접두 부정("안"/"못")이 어절 경계로 오는지
    본다(위치 앵커형 — 표지 하나의 직전만 본다. 문장 전체를 보는 `has_any_negation` 과 다르다).

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


def is_occurrence_unnegated(
    message: str,
    start: int,
    end: int,
    negation_markers: list[str],
    prefix_negation_markers: list[str],
    window: int,
) -> bool:
    """[start, end) 에 있는 표지 출현 하나가 부정되지 않았는지 — 뒤쪽 어미형(`window` 자 안)과
    앞쪽 접두형(`has_prefix_negation`) 어느 쪽에도 안 걸려야 True."""
    following = message[end : end + window]
    if any(neg in following for neg in negation_markers):
        return False
    return not has_prefix_negation(message, start, prefix_negation_markers)


def matches_unnegated(
    message: str,
    markers: list[str],
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
) -> bool:
    """`markers` 중 하나가 발화에 있고, 그 출현 중 **하나라도** 부정(뒤쪽 어미·앞쪽 접두)되지
    않았으면 True 다(2차 리뷰 지적 1·2·3 + 라운드 9의 공통 원인 — 표지가 있기만 하면 매칭했지
    그 표지가 부정·유보된 문맥인지 앞뒤 어느 쪽도 보지 않았다).

    같은 표지가 여러 번 나오면 **부정되지 않은 출현이 하나라도** 있어야 매칭이다 — "이건
    찜해줘, 장바구니에 넣지는 마, 진짜 장바구니에 넣어줘"처럼 뒤에서 다시 긍정으로 반복될
    수 있어 첫 출현만 보면 오탐(과소 매칭)한다.
    """
    for marker in markers:
        for start, end in _spans(message, marker):
            if is_occurrence_unnegated(
                message, start, end, negation_markers, prefix_negation_markers, window
            ):
                return True
    return False


_SENTENCE_TERMINAL_PUNCTUATION = frozenset(".!?")
"""문장 종결 부호(이슈 #440, 라운드 7 리뷰 F19) — 마침표·느낌표·물음표만. **쉼표는 뺀다**:
쉼표는 한 문장 안의 종속절을 잇는 부호라 `"찜 취소해줘, 장바구니는 그대로 두고"` 같은 양성
회귀 가드(`test_cart_intent_guard.py:149`)를 깨서는 안 된다. `_BOUNDARY_PUNCTUATION` 은
쉼표를 포함하는 범용 경계 집합이라 이 판정에는 재사용할 수 없다 — 그래서 별도로 둔다."""


def tail_is_command(
    message: str,
    tail_end: int,
    hedge_connectives: list[str],
    quotative_markers: list[str],
) -> bool:
    """`tail_end` 뒤가 진짜 **명령**인지 본다(이슈 #440, 라운드 6 리뷰 F16 · 라운드 7 리뷰 F19)
    — 유보·허가 표현·인용·문장 경계를 목록으로 나열하는 대신, 그 앞에 붙는 **닫힌 문법·문장
    부호 요소**로 구조적으로 가른다.

    라운드 4·5 는 `도 될`·`도 돼`·`도 되`(F11) → `wishlist_remove_hedge_markers`(F13) 로
    유보·허가 표현을 나열했는데, 매번 목록 밖 활용("도 괜찮"·"도 상관없"·"도 무방")이 다음
    라운드에 다시 나왔다(실측, 파괴적) — 한국어 유보·허가 표현은 **열린 집합**이라 나열로는
    끝나지 않는다. `도 괜찮을까`·`도 상관없어`·`도 무방한가`는 무한하지만, 그 **앞에 붙는
    연결어미 "도"·"야" 는 유한하다** — 이 함수는 그 연결어미(`hedge_connectives`)와 인용
    조사(`quotative_markers`, "라는"·"라고" 등 — 역시 닫힌 문법 부류)만 본다.

    판정:
      1. `tail_end` 에서 **바로**(경계 건너뛰기 없이) `hedge_connectives` 중 하나로 시작하고
         그 뒤에 텍스트가 더 있으면(문자열이 거기서 끝나지 않으면) **명령이 아니다**. 연결어미는
         한국어에서 어간·어미에 **직접 붙는다**(공백 없음, `"빼줘도"`·`"취소해줘도"`) — 그래서
         경계를 건너뛰지 않고 바로 확인한다. `"빼줘도"` 로 발화가 끝나면(뒤에 정말 아무것도
         없으면) 판정하지 않고 통과시킨다 — 그런 발화는 사실상 없고, 막으면 잃는 게 크다.
      2. `tail_end` 에서 **바로** `_SENTENCE_TERMINAL_PUNCTUATION`(`.`·`!`·`?`) 중 하나로
         시작하면, 그 부호(와 뒤이은 경계문자)를 건너뛴 자리에 텍스트가 더 있는지 본다. 더
         있으면 **명령이 아니다** — `"찜 취소해줘. 무슨 뜻이야?"`(F19 재현)는 인용 조사도
         연결어미도 없어 1·3 번을 다 피해가지만, 마침표는 "그 문장이 여기서 끝났다"는 닫힌
         신호다. 뒤에 또 문장이 있으면 tail 은 발화 전체의 최종 지시가 아니다. **쉼표는 이
         집합에 없다** — `"찜 취소해줘, 장바구니는 그대로 두고"` 는 이 규칙을 타지 않고 그대로
         명령으로 남는다(회귀 가드). 부호 뒤에 아무것도 없으면(`"찜 취소해줘!"`) 판정하지 않고
         통과시킨다. 이 규칙은 `"찜한 거 빼줘. 그리고 이것도 담아줘"` 같은 정상 명령+별개 문장도
         함께 막는 **알려진 축소**다(`wishlist.py` 의 `⚠️ [#440]` 주석 참고) — 잃는 것은
         되물음(비파괴적)이고 막는 것은 파괴적 삭제라 이 비대칭을 택한다.
      3. 경계문자(공백·문장부호, `_is_boundary_char`)를 전부 건너뛴 뒤 남은 텍스트가
         `quotative_markers` 중 하나로 시작하면 **명령이 아니다**. `"취소해줘 라는 말이"`·
         `"취소해줘, 라는 말이"`·`"취소해줘라는"` 이 전부 여기서 죽는다(붙여쓰기·띄어쓰기·쉼표를
         한 규칙으로 덮는다) — 인용 조사는 공백·문장부호를 사이에 두고도 올 수 있어 1·2번과
         달리 경계를 건너뛴다.
      4. 그 외 → 명령이다(`True`).

    **적용 범위는 찜 해제 경로에만 국한한다**(라운드 5 리뷰 F13 의 교훈) — `remove.py`·담기·찜
    추가 판정은 건드리지 않는다. 그쪽 목록이 무효화되면 사다리 기본값(`cart_add`)으로 떨어져
    **다른 자원에 실제 변경**이 나기 때문이다(F13 재현). 찜 해제 판정은 무효화되면 되물음
    (파괴적 동작 없음)으로 끝나 이 구조를 여기에만 넓게 적용할 수 있다.
    """
    remainder = message[tail_end:]
    for connective in hedge_connectives:
        if remainder.startswith(connective) and len(remainder) > len(connective):
            return False
    if remainder and remainder[0] in _SENTENCE_TERMINAL_PUNCTUATION:
        after_terminal = tail_end + 1
        while after_terminal < len(message) and _is_boundary_char(message[after_terminal]):
            after_terminal += 1
        if message[after_terminal:] != "":
            return False
    pos = tail_end
    while pos < len(message) and _is_boundary_char(message[pos]):
        pos += 1
    after_boundary = message[pos:]
    if any(after_boundary.startswith(marker) for marker in quotative_markers):
        return False
    return True


def tail_terminates_utterance(message: str, tail_end: int) -> bool:
    """`tail_end` 뒤에 남은 텍스트가 **경계문자(공백·문장부호)뿐**이면(또는 아예 없으면) `True`
    — 해제 동작구가 발화를 그 자리에서 **끝냈다는 것을 증명**한다(이슈 #440, 라운드 8 리뷰
    F20).

    **`tail_is_command` 와 정반대 극성이다** — 그 함수는 **라우팅용**으로 "비명령 형태(연결어미
    직결·인용 조사·문장 종결 부호+후속 내용)를 나열해 아니면 거부"하는 **기본-허용**이다. 목록에
    없는 새 우회("찜한 거 빼줘 도 될까?"— 연결어미를 띄어 씀, "'찜 해제해줘'를 영어로 번역해줘"
    — 따옴표+조사, "찜 취소해줘, 이 표현이 맞아?"— 쉼표 뒤 메타언어, "찜 취소해줘 이 표현이
    맞아?"— 공백 뒤 메타언어, "찜 취소해줘 . 무슨 뜻이야?"— 종결부호 앞 공백, "찜 취소해줘;
    무슨 뜻이야?"— 세미콜론)가 매번 나온 것도 그래서다 — 뒤에 오는 **비명령 표현 자체**는 열린
    집합이라 나열로는 끝나지 않는다.

    이 함수는 **파괴적 자동 선택(`has_wishlist_remove_evidence`)** 전용이고 **기본-거부**다 —
    "무엇이 비명령인가"를 나열하지 않고, "뒤에 무엇이든 실질 텍스트가 남아 있으면"(그 정체를
    묻지 않고) 무조건 거부한다. 위 여섯 우회가 전부 "tail 뒤에 내용이 남아 있다"는 **하나의
    사실**로 걸린다.

    **라우팅(`classify_cart_utterance`)은 그대로 관대하게 둔다** — `wishlist_remove` 로
    잘못 라우팅돼도 `has_wishlist_remove_evidence` 가 근거 없음으로 막아 되물음으로 끝난다
    (`wishlist.py::_resolve_wishlist_remove_target` 게이트 참조). 라우팅과 근거의 불변식
    (`classify_cart_utterance == "wishlist_remove"` ⟹ `has_wishlist_remove_evidence`)을
    걸었던 라운드 1(D4)의 요구는 **틀렸다** — 그 요구가 이 함수를 걸 수 없게 tail 판정 전체를
    묶어 놨다. 진짜 안전 성질은 반대다: 근거가 없으면(이 함수가 `False`) 라우팅이 어디로 가든
    삭제가 0회여야 한다(`tests/unit/test_wishlist_remove_resolution.py` §4-C 가 이걸로
    교체됐다).

    **[라운드 9 리뷰 F23] "경계문자뿐"은 너무 좁았다** — tail 표지 목록에 `-줘요`류 존댓 활용이
    없어 `"찜한 거 빼줘요"`(이슈 본문과 뜻·위험이 같은 정상 존댓말)가 "요" 한 글자 때문에
    거짓음성이 됐다(실측). `"빼줘요"`·`"해줘요"`·`"지워줘요"`… 를 목록에 나열하는 대신, 존댓
    보조사 **"요"** 는 활용에 상관없이 **하나로 닫힌** 문법 요소라는 사실을 쓴다 — tail 바로
    뒤에 "요" 가 있으면 먼저 소비하고, 남은 텍스트에 **한글 음절도 영숫자도 없으면**(공백·
    문장부호·이모지·기호는 내용이 아니다) 종결로 인정한다. `_is_boundary_char` 처럼 문자를
    나열하는 방식(이모지·특수문자를 목록화)은 여기서도 같은 함정이라 피한다 — 대신 "실질
    내용(한글·영숫자)이 없다"는 닫힌 조건으로 뒤집는다(`str.isalnum()` 은 한글 음절도 포함해
    True 를 준다). `"찜한 거 빼줘 🙏"`·`"찜한 거 빼줘!!"` 도 같은 이유로 종결로 인정된다."""
    remainder = message[tail_end:]
    if remainder.startswith("요"):
        remainder = remainder[1:]
    return not any(ch.isalnum() for ch in remainder)


# 왼쪽 앵커가 건너뛸 수 있는 **절 경계** 부호(#440 라운드 14 리뷰 F34) — 쉼표·세미콜론·
# 가운뎃점처럼 한 문장 안에서 절을 잇는 부호만이다. **인용·삽입 부호(따옴표·괄호·콜론)는
# 절대 넣지 마라** — 그것들은 절 경계가 아니라 "여기부터는 인용된 문구"라는 표시고, 넣으면
# `"'이어폰 찜 빼줘'"` 처럼 인용된 상품명이 규칙 1로 실제 삭제된다(실측, 파괴적).
# `_BOUNDARY_PUNCTUATION`(아래)과 **합치지 마라** — 그쪽은 "어절 경계인가"를 보는 넓은
# 집합이고, 여기는 "절을 잇는가"라는 좁은 의미다(같은 문자 집합이 아니다).
_UTTERANCE_CLAUSE_SEPARATORS = frozenset(",;·")


def _is_left_anchor_skippable(ch: str) -> bool:
    return ch == " " or ch in _UTTERANCE_CLAUSE_SEPARATORS


def _name_left_anchor_reachable(
    message: str,
    target_start: int,
    known_words: list[str],
    other_names: list[str],
) -> bool:
    """`target_start`(어떤 이름 **또는** head 의 시작 위치)가 발화 **시작**부터 이 판정이 아는
    어휘만으로 정확히 도달 가능한지 본다(이슈 #440, 라운드 9 리뷰 F22 → 라운드 11 리뷰 F28 로
    사슬 허용 → 라운드 12 리뷰 F30 으로 일반화 → 라운드 13 리뷰 F31/F33 으로 우회 제거 + 통합)
    — **규칙 1(이름)·규칙 2·3(head) 이 공유하는 단 하나의 왼쪽 앵커**다. 예전엔 규칙 2·3 이
    `_closed_prefix_anchor_end`(별도 함수, 경계 조건도 달랐다)를 썼는데, 라운드 13 리뷰 F33
    ("앵커 판정은 같은 코드여야 한다")이 그 분리 자체를 지적했다 — 이제 세 규칙 모두 이 함수
    하나로 왼쪽을 잰다(호출부가 `other_names=[]` 를 넘기면 규칙 2·3, 등록된 이름을 넘기면
    규칙 1이다).

    **[라운드 13 리뷰 F31] "부정 표지 catch-up" 을 삭제했다 — 그 자체가 우회였다.** 라운드
    12(F30)는 `known_words` 로 아무것도 못 먹는 위치에서도 `negation_markers`(`"지 말"`·
    `"말고"`)가 창 안에 있기만 하면 그 앞의 **모르는 문자를 통째로 건너뛰었다**. 그게
    `"예: 이어폰 말고 케이스 찜 빼줘"`·`"문구: 이어폰 말고 케이스 찜 빼줘"`처럼 인용·메모
    접두를 삼켜 실제로 삭제했다(실측, 파괴적 — 라운드 13 리뷰 F31 재현). 창 점프는 "무엇을
    건너뛰는지" 검증하지 않는다는 점에서 애초에 이 함수의 핵심 계약("아는 어휘로만 발화가
    설명된다")을 스스로 예외 처리하는 것이었다 — 좁게 스코프해도(부정 표지가 뒤에 있어야
    한다는 조건이 있어도) 예외는 예외다.

    catch-up 이 필요했던 진짜 이유는 하나뿐이었다 — `"빼지 말고"` 의 어간 `"빼"` 가 tail
    목록(`wishlist_remove_action_markers` 등, `"빼줘"` 처럼 어미까지 갖춘 형태만 담는다)에
    없어서 소비가 막혔던 것뿐이다. 그래서 창 점프 대신 **앵커 스캔 전용** 어간 목록
    (`wishlist_remove_action_stems`, `config.py` 참조 — 명령의 근거로는 절대 쓰지 않는다)을
    `known_words` 에 더한다(호출부가 합쳐 넘긴다). 어간 자체를 아는 조각으로 등재하면, 뒤에
    실제로 붙는 부정 어미(`utterance_negation_markers`, 이 목록도 이제 `known_words` 에
    그대로 포함된다 — 더 이상 별도 창 검색이 아니라 다른 낱말과 같은 자격의 **평범한 어휘
    토큰**이다)가 `_consume_prefix` 최장 일치로 순서대로 소비된다. `"빼"`(어간) → `"지 말"`
    또는 `"말고"`(부정 표지, 최장 일치가 알아서 고른다)가 이어져 `"빼지 말고"` 전체가 소비된다
    — 창을 넘어가는 게 아니라 **글자 하나하나가 전부 아는 조각**이라는 뜻이다. "무엇을
    건너뛰는지 모른 채 건너뛰는" 예전 catch-up과 달리, 이 경로는 소비되는 모든 글자가
    `known_words` 의 어느 항목과 정확히 일치한다.

    **[라운드 13 리뷰 F31-3] 경계 스킵을 공백 하나에서 `_is_boundary_char` 전체로 넓혔다** —
    쉼표·세미콜론도 이 시스템이 이미 아는 닫힌 경계 문자다(`"이어폰은 찜 빼지 말고, 케이스 찜
    빼줘"` 가 쉼표 하나 때문에 죽던 회귀, 라운드 13 리뷰 F32 — 이 확장이 자동으로 고친다).

    **[라운드 12 리뷰 F30 문단, 그대로 유효]** `known_words`(호출부가 프리픽스·브리지·filler·
    조사·의존명사·head·tail 계열·부정 표지·앵커 전용 어간을 전부 합쳐 넘긴다 — 새 목록을 만들지
    않는다, `intent_guard.wishlist_remove_known_words` 참조)와 `other_names`(등록된 이름)를
    **한 번에** 최장 일치로 소비한다(`_consume_prefix` 한 호출) — 따로 시도하면 `"이"`(닫힌
    접두어)가 `"이어폰"`(등록된 이름)의 첫 음절을 짧게 먼저 삼켜 버린다. `"내가 산 이어폰 찜
    빼줘"`처럼 목표 위치 이전에 도달 불가능한 실질 텍스트("산")가 남으면(알려진 어휘도 등록된
    이름도 아니다) 진행이 멈추고 `target_start` 에 못 미친 채 `False` 를 돌려준다 — 이게
    "받아들이는 축소"(#116/#117 원칙, `wishlist.py` 상단 docstring 참조)가 성립하는 자리다.

    소비마다 경계(공백)를 요구하지 않는다 — 조사는 앞 토큰(닫힌 낱말이든 다른 이름이든)에
    공백 없이 바로 붙는 게 정상 한국어라("이어폰" + "이랑") 그 사이에 경계를 강제하면
    정상 사슬이 깨진다. 대신 **소비가 `target_start` 를 넘어서면**(그 이름 자체를 관통해
    지나쳤다는 뜻) 무효로 본다 — 이 함수는 정확히 `target_start` **에서** 멈추는 경로가
    있는지만 확인한다.

    **[라운드 14 리뷰 F34] 경계 스킵을 `_is_boundary_char` 전체에서 절 경계 전용 집합으로
    좁혔다** — `_is_boundary_char` 는 인용부호·괄호(`'` `"` `(` `)` `[` `]` `{` `}`)도
    "경계"로 친다. 그런데 왼쪽 앵커가 그 집합을 통째로 건너뛰면 발화 첫 글자가 인용부호일 때
    앵커가 그냥 통과해 버려, `"'이어폰 찜 빼줘'"` 처럼 인용된 상품명이 규칙 1로 실제
    삭제됐다(실측, 파괴적). 앵커는 공백과 `_UTTERANCE_CLAUSE_SEPARATORS`(쉼표·세미콜론·
    가운뎃점 — 라운드 13 F31-3/F32 가 필요로 했던 절 연결 부호)만 건너뛴다. 그 밖의 문자는
    `known_words`/`other_names` 로 소비되어야 하고, 안 되면 앵커가 실패한다."""
    pos = 0
    while pos < target_start:
        while pos < len(message) and _is_left_anchor_skippable(message[pos]):
            pos += 1
        if pos == target_start:
            return True
        if pos > target_start:
            return False
        if (ordinal := _ORDINAL_PREFIX.match(message, pos)) and ordinal.end() <= target_start:
            pos = ordinal.end()
            continue
        consumed = _consume_prefix(message, pos, known_words + other_names)
        if consumed != pos and consumed <= target_start:
            pos = consumed
            continue
        return False
    return pos == target_start


def matches_unnegated_left_bounded(
    message: str,
    markers: list[str],
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
    hedge_connectives: list[str],
    quotative_markers: list[str],
    *,
    require_termination: bool = False,
    prefix_words: list[str] | None = None,
) -> bool:
    """`matches_unnegated` 와 같되, 표지 출현의 **왼쪽**만 어절 경계(문자열 시작 또는
    `_is_boundary_char`)로 추가 검사한다(이슈 #440, 라운드 2 리뷰 F6).

    **왜 필요한가**: `matches_unnegated` 는 순수 부분 문자열 검색이라, `wishlist_remove_markers`
    의 `"찜 빼줘"` 가 `"갈비찜 빼줘"`·`"계란찜 빼줘"`·`"김치찜 빼줘"` 안에 그대로 부분 문자열로
    들어 있어 매칭돼 버린다 — `matches_pair_unnegated`(#440 신규 판정)의 head 경계 검사가
    `찜닭`·`갈비찜` 류를 걸러내도, 그 경계 검사를 거치지 않는 옛 표지 목록(`wishlist_remove_
    markers`) 매칭이 같은 함정을 그대로 통과시킨다. 이슈 자체가 "부분 문자열만으로는 조회와
    해제를 가를 수 없다"는 판단에서 출발했는데, 새 경로만 고치고 옛 경로를 안 고치면 그 판단이
    반쪽만 적용된 것이다.

    **[라운드 6 리뷰 F16]** 오른쪽도 이제 본다 — `tail_is_command` 로 유보·허가·인용을 걸러낸다
    (`"찜 해제해줘라고 하면 돼?"` 가 왼쪽 경계만으로는 그대로 통과했다, 실측). 이 함수가 받는
    표지는 전부 어미까지 갖춘 동작 구(`"찜 빼줘"`·`"찜 해제해줘"`)라 조사가 바로 붙는 구조가
    아니므로 `tail_is_command` 의 판정이 그대로 맞는다 — `matches_name_unnegated`(상품명, 조사가
    붙는 구조)와 이 함수를 여전히 합치지 않는 이유는 그대로다(그 함수 docstring 참조).

    **[라운드 8 리뷰 F20]** `require_termination=True` 를 넘기면 `tail_is_command` 통과에
    더해 `tail_terminates_utterance`(뒤가 경계문자뿐)도 요구한다 — 라우팅 호출부(기본값
    `False`, 관대)와 근거 판정 호출부(`True`, 엄격)가 이 한 함수를 공유하면서도 서로 다른
    극성을 쓴다.

    **[라운드 9 리뷰 F22 → 라운드 13 리뷰 F33]** `prefix_words` 를 넘기면(근거 판정 전용)
    왼쪽도 **전체 앵커**한다 — 단순 경계문자 하나가 아니라 `_name_left_anchor_reachable`(규칙
    1과 공유하는 왼쪽 앵커, `other_names=[]`)로 발화 **시작**부터 닫힌 어휘만으로 표지 시작에
    도달해야 한다. 그러지 않으면 `"다음 문구를 영어로 번역해줘: '찜 해제해줘'"` 처럼 해제
    문구를 **인용·번역·예시**의 목적어로 둔 발화가, 그 문구 앞의 실질 텍스트("다음 문구를
    번역해줘:")를 경계문자 하나로만 검사하는 왼쪽 경계 검사를 그대로 통과해 삭제 명령으로
    오인됐다(실측, 파괴적 — 라운드 8 이 오른쪽(tail 종결)만 잠갔더니 왼쪽이 열려 있던 구멍)."""
    for marker in markers:
        for start, end in _spans(message, marker):
            if start != 0 and not _is_boundary_char(message[start - 1]):
                continue
            if prefix_words is not None and not _name_left_anchor_reachable(
                message, start, prefix_words, []
            ):
                continue
            if not tail_is_command(message, end, hedge_connectives, quotative_markers):
                continue
            if require_termination and not tail_terminates_utterance(message, end):
                continue
            if is_occurrence_unnegated(
                message, start, end, negation_markers, prefix_negation_markers, window
            ):
                return True
    return False


_BOUNDARY_PUNCTUATION = frozenset(",.!?;:\"'()[]{}~·…")


def _is_boundary_char(ch: str) -> bool:
    return ch == " " or ch in _BOUNDARY_PUNCTUATION


def _consume_prefix(message: str, pos: int, options: list[str]) -> int:
    """`pos` 에서 `options` 중 리터럴로 일치하는 것이 있으면 **가장 긴 매칭**을 소비한 새 위치를
    돌려준다(못 찾으면 `pos` 그대로) — **옵션 목록의 순서에 의존하지 않는다**(최장 일치).

    **[라운드 20, head `5772021` 리뷰]** 이전에는 `options` 를 리스트 순서대로 훑어 **첫
    매칭**을 소비했다 — `utterance_name_boundary_particles` 에서 `"이"` 가 `"이랑"`·`"이나"`
    보다 앞에 있어, 받침 있는 상품명 뒤의 `"이랑"`에서 `"이"` 만 1글자 소비되고 `"랑"`이 남아
    `_has_valid_name_trailing` 의 오른쪽 경계 검사가 실패했다("이어폰이랑 케이스 빼줘"가
    사용자가 지목한 "이어폰"을 조용히 누락하고 "케이스"만 삭제, 재현 — 받침 없는 "파우치랑
    세제 빼줘"는 우연히 정상 동작해 **받침 유무로 결과가 갈리는** 데이터 의존적 결함이었다).
    `config.py` 목록의 순서를 바꾸는 것은 같은 함정("다음 옵션을 접두 충돌 순서 몰래 추가하면
    재발")을 다음 사람에게 그대로 남기는 미봉책이라, 이 함수 자체를 최장 일치로 고쳤다 —
    호출부(`_skip_trailing_filler`·`_has_valid_name_trailing`)가 넘기는 옵션 목록을 어떤
    순서로 나열해도 항상 같은(가장 긴 매칭) 결과를 낸다.
    """
    best = pos
    for option in options:
        if not option:
            continue
        end = pos + len(option)
        if end > best and message[pos:end] == option:
            best = end
    return best


def _noun_ending_match_end(message: str, end: int, verb_suffixes: list[str]) -> int | None:
    """`end` 뒤가 명사형 tail 의 유효한 종결이면 그 종결이 **끝나는 위치**를 돌려준다(무효면
    `None`) — **발화가 거기서 끝나거나(뒤가 전부 경계문자) 용언 어미로 바로 이어지고 그 어미
    자체가 그 자리에서 끝날 때만** 유효하다(이슈 #440, 라운드 3 리뷰 F8).

    `"찜 취소"`(그 자체로 끝) · `"찜 취소해줘"`(어미 직결)는 유효하고, `"찜 취소는 어떻게 해?"`
    (조사)·`"찜 취소선 그어줘"`(다른 낱말이 바로 붙음)·`"찜 취소 수수료 알려줘"`(공백 뒤에도
    실질 낱말이 남음)는 전부 무효다 — "발화 끝 아니면 반드시 그 자리에서 곧장 용언이 붙어야
    한다"는 것이 핵심이라, 뒤에 공백이 있다는 사실만으로는(그 공백 뒤에 아무것도 없을 때만)
    유효해진다.

    **[라운드 5 리뷰 F14]** 용언 어미 매칭은 접두(`startswith`)만으로는 부족하다 — 어미 자체도
    **종결**(문자열 끝이거나 그 뒤가 `_is_boundary_char`)이어야 한다. 그러지 않으면 `"찜
    취소해줘라는 말이 뭐야?"`(표현을 묻는 질문)가 `"해줘"`로 **시작**한다는 이유만으로 삭제
    명령이 된다(실측, 파괴적 — F10 이 어간을 없애도 "접두 매칭"이라는 구조 자체는 남아 있었다.
    `"찜"` ⊂ `찜닭`, `"빼"` ⊂ `빼고`, 어간이 `"해당"`에 묻히던 것과 같은 함정의 네 번째 재발).

    **[라운드 6 리뷰 F16]** `bool` 대신 위치를 돌려준다 — 호출부가 "종결이 유효한가"뿐 아니라
    "그 종결이 정확히 어디서 끝나는가"도 알아야 그 위치에서 `tail_is_command` 를 이어 걸 수
    있다(어미까지 소비한 뒤의 자리를 몰라서 어미 앞에서 유보·인용을 검사하면 그 어미 자체가
    가려서 못 잡는다) — 라운드 3(F8)에는 `bool` 만 돌려주는 `_noun_ending_ok` 였는데, 위치
    정보가 없어서 이 함수로 바뀌었다(`_noun_ending_ok` 는 삭제 — 유일한 두 호출부가 모두
    위치가 필요해졌다).

    **[라운드 9 리뷰 F23]** 어미 바로 뒤가 존댓 보조사 **"요"** 인 것도 어미가 "그 자리에서
    끝났다"는 뜻이다 — `"찜 취소해줘요"`("취소"+"해줘"+"요")가 "요" 한 글자 때문에 이 함수
    전체가 `None` 을 돌려줘 노미(routing·근거 둘 다) 실패했다(실측 — `"찜 취소해줘"` 는 되고
    존댓말만 안 되는 비대칭). "요" 는 활용이 열린 tail 표지 목록과 달리 **하나로 닫힌** 문법
    요소라 목록에 얹지 않고 여기서 직접 받아준다. 반환 위치는 여전히 어미 끝(요 앞)이다 —
    "요" 자체는 `tail_terminates_utterance` 가 그 위치에서 다시 독립적으로 소비한다(같은
    보조사를 두 함수가 각자 인지하되 정책은 한 곳에서만 판단한다: 여기는 "어미가 끝났는가"만,
    `tail_terminates_utterance` 는 "그 뒤에 진짜 내용이 남았는가"만).
    """
    remainder = message[end:]
    if all(_is_boundary_char(ch) for ch in remainder):
        return end
    for suffix in verb_suffixes:
        if not remainder.startswith(suffix):
            continue
        after = remainder[len(suffix) :]
        if after == "" or after[0] == "요" or _is_boundary_char(after[0]):
            return end + len(suffix)
    return None


def matches_unnegated_left_bounded_with_noun_ending(
    message: str,
    markers: list[str],
    verb_suffixes: list[str],
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
    hedge_connectives: list[str],
    quotative_markers: list[str],
    *,
    require_termination: bool = False,
    prefix_words: list[str] | None = None,
) -> bool:
    """`matches_unnegated_left_bounded` 와 같되, 표지 출현의 **오른쪽**에도 `_noun_ending_
    match_end`(명사형 종결 규칙)를 요구한다(이슈 #440, 라운드 3 리뷰 F8).

    `wishlist_remove_markers`(사다리 1번)의 어미 없는 명사형 표지(`"찜 취소"`·`"찜 해제"`, 별도
    목록 `wishlist_remove_noun_markers`)에 쓴다 — 왼쪽 경계만 요구하면(`matches_unnegated_
    left_bounded`) `"찜 취소는 어떻게 해?"`·`"찜 취소선 그어줘"`·`"찜 취소 수수료 알려줘"` 같은
    **조회·질문**이 삭제 명령으로 읽힌다(실측, 파괴적 — `matches_pair_unnegated` 는 tail
    오른쪽을 안 보고, 왼쪽 경계만 있는 함수도 마찬가지라 이 표지들엔 같은 구멍이 있었다).

    **[라운드 6 리뷰 F16]** 명사형 종결 확인(`_noun_ending_match_end`) 뒤에도 `tail_is_command`
    를 **추가로** 건다 — 종결 확인은 "어미가 그 자리에서 끝나는가"만 보고 그 뒤에 오는 문장이
    유보·인용인지는 모른다. `"찜 취소해줘라고 하면 돼?"`("찜 취소"+어미 "해줘")는 `"해줘"` 로
    어미가 유효하게 끝나지만 바로 그 자리부터 인용 조사 `"라고"` 로 이어진다 —
    `_noun_ending_match_end` 가 돌려준 **어미까지 소비한 뒤의 위치**에서 `tail_is_command` 를
    걸어야 그 인용을 잡는다(어미 앞 위치에서 걸면 어미 자체가 가려 못 잡는다).

    **[라운드 8 리뷰 F20]** `require_termination=True` 는 같은 위치에서 `tail_terminates_
    utterance` 도 추가로 요구한다 — `matches_unnegated_left_bounded` 의 F20 문단 참조.

    **[라운드 9 리뷰 F22]** `prefix_words` 는 같은 방식으로 왼쪽 **전체**를 앵커한다 —
    `matches_unnegated_left_bounded` 의 F22 문단 참조.
    """
    for marker in markers:
        for start, end in _spans(message, marker):
            if start != 0 and not _is_boundary_char(message[start - 1]):
                continue
            if prefix_words is not None and not _name_left_anchor_reachable(
                message, start, prefix_words, []
            ):
                continue
            command_end = _noun_ending_match_end(message, end, verb_suffixes)
            if command_end is None:
                continue
            if not tail_is_command(message, command_end, hedge_connectives, quotative_markers):
                continue
            if require_termination and not tail_terminates_utterance(message, command_end):
                continue
            if is_occurrence_unnegated(
                message, start, end, negation_markers, prefix_negation_markers, window
            ):
                return True
    return False


def _iter_head_bridge_ends(
    message: str,
    head_markers: list[str],
    dependent_nouns: list[str],
    boundary_particles: list[str],
    bridge_words: list[str],
) -> list[tuple[int, int]]:
    """경계를 통과한 head 출현마다 `(head_start, bridge_end)` 를 낸다 — head 뒤 의존명사·조사·
    **닫힌 어휘 브리지**(공백+ 브리지낱말 (조사)?)* 를 전부 소비한 뒤의 위치(`bridge_end`)로,
    tail(동작구 또는 명사형)이 **정확히 거기서** 시작해야 인접 결합으로 친다(이슈 #440, 라운드
    3 리뷰 F7 — `matches_pair_unnegated` 가 이 head 쪽 절반을 재사용한다).

    브리지 문법: `head + (의존명사)? + (조사)? + ( 공백+ 브리지낱말 (조사)? )* + 공백* + tail`.
    head 와 tail 사이에 **닫힌 어휘(브리지 낱말·조사·의존명사)만** 올 수 있다 — 그 밖의 낱말이
    하나라도 끼면(`"찜 보고 이거 빼줘"`의 `"보고"`, `"찜한 상품 중에 이어폰 빼줘"`의 `"중에"`·
    `"이어폰"`) 두 사람은 같은 명령이 아니다. `_has_valid_name_trailing`("이름 + (조사) +
    (filler)* + 표지")과 같은 모양의 규칙을 head→tail 에도 적용한 것이다.

    (head 왼쪽 경계 + 의존명사→조사 소비는 라운드 1 리뷰 F1 이 정한 그대로 — 이 함수가 그 절반과
    브리지 소비를 모아 재사용 지점을 하나로 만든다. **[라운드 9 리뷰 F22]** `head_start` 를
    다시 돌려준다(라운드 3 에 "거리 개념이 사라졌으니 필요 없다"며 뺐던 값) — 근거 판정이
    head **왼쪽 전체**를 닫힌 어휘로 앵커하려면(`matches_pair_unnegated` 의 `prefix_words`)
    호출부가 이 위치를 알아야 한다.)
    """
    results: list[tuple[int, int]] = []
    for head_marker in head_markers:
        for head_start, head_end in _spans(message, head_marker):
            if head_start != 0 and not _is_boundary_char(message[head_start - 1]):
                continue
            head_after_noun = _consume_prefix(message, head_end, dependent_nouns)
            head_consumed_end = _consume_prefix(message, head_after_noun, boundary_particles)
            right_ok = head_consumed_end == len(message) or _is_boundary_char(
                message[head_consumed_end]
            )
            if not right_ok:
                continue
            pos = head_consumed_end
            while True:
                ws_pos = pos
                while ws_pos < len(message) and message[ws_pos] == " ":
                    ws_pos += 1
                consumed = _consume_prefix(message, ws_pos, bridge_words)
                if consumed == ws_pos:
                    break
                pos = _consume_prefix(message, consumed, boundary_particles)
            bridge_end = pos
            while bridge_end < len(message) and message[bridge_end] == " ":
                bridge_end += 1
            results.append((head_start, bridge_end))
    return results


def has_boundary_passing_head(
    message: str,
    head_markers: list[str],
    dependent_nouns: list[str],
    boundary_particles: list[str],
    bridge_words: list[str],
) -> bool:
    """`head_markers` 중 하나라도 어절 경계 검사를 통과해 나타나는지만 본다 — `_iter_head_bridge_
    ends`(위, 라운드 3 리뷰 F7)의 head 스캔을 그대로 재사용한다(새 경계 판정을 만들지 않는다).
    그 함수는 이미 경계를 통과한 head 출현만 걸러 돌려주므로, 결과가 비어 있지 않은지만 보면 된다.

    **[#440 후속]** `wishlist_remove` → `cart_remove` 역방향 정정(`intent_guard.
    has_deceptive_wishlist_marker`)이 쓴다 — LLM 이 `찜닭`·`갈비찜` 처럼 경계를 통과하지 못하는
    부분 문자열에 속아 실제로는 삭제 의도인 발화를 `wishlist_remove` 로 오분류했는지 판정하려면
    "경계를 통과한 head 가 하나도 없다"는 사실이 필요하다."""
    return bool(
        _iter_head_bridge_ends(
            message, head_markers, dependent_nouns, boundary_particles, bridge_words
        )
    )


def matches_pair_unnegated(
    message: str,
    head_markers: list[str],
    tail_markers: list[str],
    action_noun_tails: list[str],
    action_noun_verb_suffixes: list[str],
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
    dependent_nouns: list[str],
    boundary_particles: list[str],
    bridge_words: list[str],
    hedge_connectives: list[str],
    quotative_markers: list[str],
    *,
    require_termination: bool = False,
    prefix_words: list[str] | None = None,
) -> bool:
    """ "지시 명사(head, 어절 경계 통과) → 닫힌 어휘 브리지 → 부정되지 않은 tail"이라는 **인접
    결합**을 판정한다(이슈 #440) — `head_markers`·`tail_markers` 어느 쪽도 부분 문자열 검색만으로
    매칭하지 않는다.

    **왜 부분 문자열 결합(#386 이 시도했다가 되돌린 접근)이 실패했는가**: `["찜","위시리스트"] ×
    ["빼","해제","취소",...]` 처럼 두 목록을 단순히 "발화 안에 둘 다 있으면 매칭"으로 결합하면,
    짧은 표지가 **다른 낱말에 묻히는** 경우를 걸러내지 못한다 — `"찜"` ⊂ `찜닭`·`갈비찜`·
    `찜질방`, `"빼"` ⊂ `빼고`. 그 결과 `"찜닭 빼고 보여줘"`(음식 조회 발화)가 "찜"과 "빼"를
    둘 다 부분 문자열로 포함한다는 이유만으로 찜 해제 근거로 오인됐다. 이 함수는 그 실패를
    두 가지 장치로 막는다:

    1. **어절 경계**: head 출현은 왼쪽이 문자열 시작/경계문자이고, 오른쪽은 **의존명사 →
       조사** 순으로 각각 `_consume_prefix` 로 소비한 뒤가 문자열 끝/경계문자여야 산다.
       `찜닭`의 `"찜"`은 오른쪽에 `"닭"`이 바로 붙어(의존명사도 조사도 경계문자도 아니다)
       죽는다. `갈비찜`의 `"찜"`은 왼쪽에 `"비"`가 바로 붙어 죽는다. `찜질방`의 `"찜"`은
       오른쪽 `"질"`이 붙어 죽는다. 반면 `"찜은 빼줘"`·`"찜에서 빼줘"`는 조사(`"은"`·
       `"에서"` 의 "에"+"서"... `boundary_particles` 최장 일치)를 소비하고 살아난다.
       **[라운드 1 리뷰 F1]** `"찜한거 빼줘"`(공백 없음)가 `"찜한 거 빼줘"`와 같은 말인데도
       죽던 결함 — `"거"`·`"것"`·`"게"`·`"걸"` 은 의존명사라 조사 목록에는 없다. 의존명사를
       먼저 소비하고 그 뒤에서 다시 조사를 소비해 `"찜한거를 빼줘"`(의존명사+조사)도 살린다.
       공백 유무로 기능이 갈리는 것은 데이터 의존적 결함이다(`negation.py` 라운드 20 의
       "받침 유무로 결과가 갈리던" 것과 같은 부류) — `dependent_nouns` 에는 **상품명이 될 수
       있는 실질명사를 절대 넣지 마라**. `찜닭`의 `"닭"`처럼 실질명사가 섞이면 어절 경계
       검사가 무력화돼 이 이슈가 고친 거짓양성이 통째로 되살아난다.
    2. **닫힌 어휘 브리지**(`_iter_head_bridge_ends`, 라운드 3 리뷰 F7) — 경계를 통과한 head
       라도, tail 이 head 뒤 브리지(닫힌 어휘만으로 이어진 구간) 바로 그 자리에서 시작하고
       부정되지 않아야 매칭이다. **[라운드 3 리뷰 이전엔 거리(pair_window)로 판정했는데, 거리는
       "같은 명령"을 보장하지 못했다** — `"찜 보고 이거 빼줘"`의 head-tail 간격(7자)이
       `"찜 목록에서 빼줘"`(7자)와 같아 구분되지 않고 매칭됐다(실측, 파괴적). 브리지는 "그
       사이에 실질적인 낱말이 하나라도 끼면 별개의 절"이라는 판단을 구조로 강제한다.

    **명사형 tail(`action_noun_tails`, 라운드 3 리뷰 F8)** — `"해제"`·`"취소"` 처럼 어미 없는
    명사형은 tail 오른쪽도 명사형 종결 규칙(`_noun_ending_match_end`)으로 검사한다(브리지 끝에서
    발화가 끝나거나 용언 어미로 바로 이어질 때만) — 그러지 않으면 `"찜 해제 방법 보여줘"`·
    `"찜 취소 수수료 알려줘"` 같은 조회·질문이 명령으로 읽힌다.

    **[라운드 6 리뷰 F16]** 두 tail 계열 모두 매칭 뒤에 `tail_is_command` 를 **추가로** 건다 —
    `"찜 빼줘도 될까?"`(어미 동작구 tail)·`"찜 취소해줘라고 하면 돼?"`(명사형 tail)처럼, tail
    자체는 어미까지 온전해도(또는 명사형 종결 규칙을 통과해도) 그 뒤가 유보·허가·인용이면
    아직 명령이 아니다. `tail_markers` 는 브리지가 끝나는 자리(`tail_end`)에서, `action_noun_
    tails` 는 명사형 종결이 실제로 끝나는 자리(`_noun_ending_match_end` 가 돌려준 위치, 어미를
    소비했다면 어미까지 지난 자리)에서 각각 건다.

    **[라운드 8 리뷰 F20]** `require_termination=True` 는 같은 두 위치에서 `tail_terminates_
    utterance` 도 추가로 요구한다 — `matches_unnegated_left_bounded` 의 F20 문단 참조.

    **[라운드 9 리뷰 F22 → 라운드 13 리뷰 F33]** `prefix_words` 는 `head_start`(라운드 9에
    `_iter_head_bridge_ends` 가 다시 돌려주기 시작한 값)에 `_name_left_anchor_reachable`(규칙
    1과 공유하는 왼쪽 앵커, `other_names=[]`)를 걸어 head **왼쪽 전체**를 닫힌 어휘로 앵커한다
    — `matches_unnegated_left_bounded` 의 F22 문단 참조. 어절 경계(위 1번)는 head 바로 앞
    한 글자만 보므로, `"다음 문구를 영어로 번역해줘: '찜 해제해줘'"` 처럼 head 앞에 실질
    텍스트가 있어도 그 텍스트 끝이 경계문자(`':'`·`"'"`)이기만 하면 통과했다(실측, 파괴적) —
    `prefix_words` 는 그 앞이 **전부** 닫힌 어휘(`intent_guard.wishlist_remove_known_words`
    합집합 — 규칙 1과 같은 목록, 라운드 13 리뷰 F33 이전엔 프리픽스·브리지·filler·조사 4개
    뿐이었다)여야 통과시킨다.
    """
    for head_start, bridge_end in _iter_head_bridge_ends(
        message, head_markers, dependent_nouns, boundary_particles, bridge_words
    ):
        if prefix_words is not None and not _name_left_anchor_reachable(
            message, head_start, prefix_words, []
        ):
            continue
        for tail_marker in tail_markers:
            for tail_start, tail_end in _spans(message, tail_marker):
                if tail_start != bridge_end:
                    continue
                if not tail_is_command(message, tail_end, hedge_connectives, quotative_markers):
                    continue
                if require_termination and not tail_terminates_utterance(message, tail_end):
                    continue
                if is_occurrence_unnegated(
                    message, tail_start, tail_end, negation_markers, prefix_negation_markers, window
                ):
                    return True
        for noun in action_noun_tails:
            noun_end = bridge_end + len(noun)
            if message[bridge_end:noun_end] != noun:
                continue
            command_end = _noun_ending_match_end(message, noun_end, action_noun_verb_suffixes)
            if command_end is None:
                continue
            if not tail_is_command(message, command_end, hedge_connectives, quotative_markers):
                continue
            if require_termination and not tail_terminates_utterance(message, command_end):
                continue
            if is_occurrence_unnegated(
                message, bridge_end, noun_end, negation_markers, prefix_negation_markers, window
            ):
                return True
    return False


def _skip_trailing_filler(message: str, pos: int, filler_words: list[str]) -> int:
    """`pos` 부터 (공백 → filler 낱말)* 을 반복 소비한 새 위치를 돌려준다 — "이름 좀 빼줘"처럼
    조사 뒤에 filler 가 끼어도 표지까지 계속 건너뛴다."""
    while True:
        next_pos = pos
        while next_pos < len(message) and message[next_pos] == " ":
            next_pos += 1
        consumed = _consume_prefix(message, next_pos, filler_words)
        if consumed == next_pos:
            return next_pos
        pos = consumed


def _has_valid_name_trailing(
    message: str,
    end: int,
    boundary_particles: list[str],
    filler_words: list[str],
    trailing_markers: list[str],
    other_names: list[str],
) -> bool:
    """이름 매칭 뒤(`end` 위치부터)가 "이름 + (조사) + (filler) + 표지" 형태인지 검사한다
    (라운드 17, head `6ab47c9` 리뷰 — `matches_name_unnegated` 의 오른쪽 경계 판정 본체).

    순서: (1) 조사 하나를 소비한다 (2) 공백을 건너뛴다 (3) filler 낱말이 있으면 소비하고 (2)로
    돌아간다 (4) 남은 텍스트가 **비어 있거나**, `trailing_markers`(그 흐름의 삭제/찜 표지)로
    **시작하거나**, `other_names`(같은 목록의 다른 항목 이름) 중 하나로 **시작하면** 유효하다.
    그 밖의 것이 오면 무효(이 출현은 매칭에서 제외).

    `other_names` 를 허용하는 이유 — "파우치 블루랑 파우치 레드 빼줘"(장바구니에 둘 다 있음)를
    깨뜨리지 않기 위해서다. "파우치 블루" 뒤는 "랑"(조사) 다음 "파우치 레드"(다른 항목 이름)로
    이어지는데, filler 목록에 없는 텍스트라 그것만 보면 무효로 잘못 걸러진다 — 그러면 "파우치
    블루"는 매칭에서 빠지고 "파우치 레드"만 단독 매칭돼 모호 판정(2건 → 되물음)이 아니라
    되레 단일 확정으로 오판된다. 그래서 "다음이 (알려진) 다른 상품명으로 시작한다"도 유효
    종결로 인정한다 — 이건 임의의 명사를 허용하는 추측이 아니라, 지금 이 해소에 실제로 후보로
    올라 있는 **닫힌 목록**(장바구니/찜 목록의 실제 항목명) 검사라 "뒤에 명사가 오면 무효"류
    휴리스틱과 다르다(그 명사가 이 목록에 없으면 여전히 무효).

    `other_names` 종결은 **조사·filler 를 실제로 하나 이상 소비했을 때만** 인정한다 —
    "이어폰케이스 빼줘"(장바구니에 "이어폰"·"케이스"가 각각 있음)에서 "이어폰" 바로 뒤에
    조사도 공백도 없이 "케이스"가 그대로 붙어 있으면, 그건 서로 다른 두 상품명이 조사로 이어진
    것이 아니라 **하나의 합성 낱말**이다. `end` 와 조사·filler 소비 뒤 위치(`pos`)가 같다면(=
    한 글자도 소비되지 않았다면) `other_names` 로 시작해도 무효로 처리한다 — 위 "파우치
    블루랑…" 예시는 "랑"이 실제로 소비되므로(`pos > end`) 이 조건에서 걸러지지 않는다.
    `trailing_markers`(표지)로 시작하는 경우와 "남는 텍스트가 빈 문자열"인 경우는 조사 소비
    여부와 무관하게 그대로 유효다 — "이어폰빼줘"처럼 표지가 곧바로 붙는 것은 정상 발화다.
    """
    pos = _consume_prefix(message, end, boundary_particles)
    pos = _skip_trailing_filler(message, pos, filler_words)
    remainder = message[pos:]
    if remainder == "":
        return True
    if any(marker and remainder.startswith(marker) for marker in trailing_markers):
        return True
    if pos == end:
        return False
    return any(other and remainder.startswith(other) for other in other_names)


def matches_name_unnegated(
    message: str,
    name: str,
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
    boundary_particles: list[str],
    filler_words: list[str],
    trailing_markers: list[str],
    other_names: list[str],
) -> bool:
    """상품명이 발화에 **온전한 어절 경계로**, 그리고 **부정되지 않은 출현으로** 등장하는지 본다
    (라운드 15, head `0b33e06` 리뷰 B — `remove.py`/`wishlist.py` 의 이름 매칭 전용).

    `matches_unnegated` 와 합치지 않고 별도 함수로 둔다 — `matches_unnegated`(그리고 그걸 쓰는
    `intent_guard.py`)의 `markers` 인자는 "빼줘"·"장바구니에 넣어줘" 같은 **동사구**라 경계
    개념이 다르다(동사구는 애초에 앞뒤에 조사가 안 붙는 구조라 경계 검사가 무의미하거나 오히려
    해롭다). 반면 여기 `name` 은 **상품명**(사용자·판매자가 임의로 짓는 문자열)이라 다른
    낱말에 파묻힐 수 있다("이어폰케이스"의 "이어폰"). 두 검사를 하나로 합치면 동사구 매칭에도
    경계 검사가 강제돼 엉뚱한 회귀를 낳는다.

    왼쪽 경계: 이름 바로 **왼쪽**은 문자열 시작·공백·문장부호여야 한다("이어폰케이스"의
    "이어폰"처럼 다른 낱말 뒤에 바로 붙어 시작하는 매칭은 배제).

    오른쪽 경계(**[라운드 17, head `6ab47c9` 리뷰]** 전면 개정): 라운드 15 는 "오른쪽이
    공백/문장부호/조사면 유효"였는데, 이는 "이어폰 케이스 빼줘"(장바구니에 "이어폰"만 있음)
    에서 "이어폰" 뒤가 공백이라는 이유만으로 매칭을 허용해, 사용자가 요청하지 않은 상품
    ("이어폰 케이스")과 다른 실제 보유 상품("이어폰")을 혼동한 채 **삭제까지 실행**했다(라운드
    15 는 이걸 "덜 친절한 문구" 급의 알려진 한계로 미뤘으나, 이 라운드에서 그 판단을 뒤집는다
    — "사용자가 요청하지 않은 파괴적 동작"은 플래그 뒤에 있어도 한계로 미루지 않는다). 그래서
    오른쪽 경계를 `_has_valid_name_trailing` 로 교체한다 — 조사를 소비하고, filler 낱말을
    건너뛴 뒤, 남는 텍스트가 비어 있거나 그 흐름의 삭제/찜 표지로 시작해야만("이름 + (조사) +
    (filler) + 표지" 형태) 유효한 매칭으로 친다. 리뷰어의 원안("매칭 뒤에 다른 토큰이 있으면
    모두 무효")을 그대로 쓰면 "이어폰 **빼줘**"의 "빼줘"도 토큰이라 정상 매칭까지 죽는다 —
    표지로 시작하는 건 허용해야 그 차이가 갈린다. `other_names` 로 다른 항목 이름 나열도
    허용하는 이유는 이 함수 자체의 docstring 을 보라.
    """
    for start, end in _spans(message, name):
        if start != 0 and not _is_boundary_char(message[start - 1]):
            continue
        if not _has_valid_name_trailing(
            message, end, boundary_particles, filler_words, trailing_markers, other_names
        ):
            continue
        if is_occurrence_unnegated(
            message, start, end, negation_markers, prefix_negation_markers, window
        ):
            return True
    return False


def _name_trailing_command_end(
    message: str,
    end: int,
    boundary_particles: list[str],
    filler_words: list[str],
    trailing_markers: list[str],
    other_names: list[str],
) -> tuple[bool, int | None]:
    """`_has_valid_name_trailing` 과 같은 판정이되, 유효하면 **어느 경로로 유효했는지도**
    돌려준다(이슈 #440, 라운드 10 리뷰 F26) — `(유효?, command_end)`.

    `trailing_markers`(표지)가 매칭됐거나 남는 텍스트가 아예 없으면 그 자리가 "명령이 끝나는
    위치"이므로 `command_end` 를 채운다. `other_names`(다른 항목 이름) 로 이어지는 경우는
    "파우치 블루랑 파우치 레드 빼줘"처럼 **이 이름 뒤에 실제 tail 이 없는 사슬 연결**이라
    `command_end=None` 을 돌려준다 — 진짜 tail 은 사슬의 마지막 이름이 표지로 끝나는 자리에서
    별도로 잡힌다. 호출부(`matches_name_unnegated_as_command`)는 `command_end` 가 있을 때만
    종결(`tail_terminates_utterance`)을 검사한다 — 사슬 연결 자리에 종결을 강제하면
    "파우치 블루랑 파우치 레드 빼줘"의 "파우치 블루"가 무효로 걸러져 모호 판정(2건 되물음)이
    깨진다(회귀, `test_wishlist_flow.py::test_resolve_wishlist_remove_target_ambiguous_
    listing_with_particle_still_asks` 참조)."""
    pos = _consume_prefix(message, end, boundary_particles)
    pos = _skip_trailing_filler(message, pos, filler_words)
    remainder = message[pos:]
    if remainder == "":
        return True, pos
    matched_marker = next(
        (marker for marker in trailing_markers if marker and remainder.startswith(marker)), None
    )
    if matched_marker is not None:
        return True, pos + len(matched_marker)
    if pos == end:
        return False, None
    if any(other and remainder.startswith(other) for other in other_names):
        return True, None
    return False, None


def matches_name_unnegated_as_command(
    message: str,
    name: str,
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
    boundary_particles: list[str],
    filler_words: list[str],
    trailing_markers: list[str],
    other_names: list[str],
    known_words: list[str],
) -> bool:
    """`matches_name_unnegated` 과 같되, 상품명이 **명령의 대상으로 지목됐다는** 계약을
    추가로 요구한다(이슈 #440, 라운드 10 리뷰 F26 → 라운드 11 리뷰 F28 로 규칙 2·3 과 같은
    형태로 통일 → 라운드 12 리뷰 F30 으로 앵커 어휘 일반화) — **찜 해제 규칙 1(`wishlist.py::
    _resolve_wishlist_remove_target`) 전용**이다(`remove.py` 의 장바구니 삭제 이름 매칭은
    그대로 `matches_name_unnegated` 를 쓴다 — 이 계약을 넓히지 않는다).

    **[라운드 11 리뷰 F28 → 라운드 12 리뷰 F30] 규칙 1도 이제 규칙 2·3 과 같은 전체 왼쪽
    앵커(`known_words`)를 받는다** — 라운드 9 는 "상품명은 닫힌 어휘가 아니라서 규칙 1엔
    전체 앵커를 못 건다"고 판단해 규칙 1을 라우팅급(발화 전역) 게이트로 따로 뒀는데, 그 분리가
    두 라운드 연속 구멍을 냈다. `"내가 산 이어폰 찜 빼줘"`(정상)와 `"사용자가 말한 건 이어폰
    찜 빼줘"`(인용/간접화법)는 **구조가 같다** — 접두 내용의 **의미**로만 갈리므로, 접두를
    열어 두는 한 어떤 거부 목록(라운드 10 의 `utterance_quote_open_chars`)으로도 못 가른다
    (부호 없는 간접화법이 항상 남는다, 실측: `"사용자가 말한 건 이어폰 찜 빼줘"`·`"예시 →
    이어폰 찜 빼줘"`). 그래서 인용부호 목록은 **삭제**하고, 상품명도 head 와 같은 자리
    (`(아는 어휘)* (이름|head) …`)에 놓아 **같은 왼쪽 앵커**(`_name_left_anchor_reachable`)로
    가른다 — `"이어폰"` 자체가 닫힌 어휘일 필요는 없다(이름 매칭 그 자체가 강한 신호다), 그
    **앞**이 "이 판정이 아는 어휘"여야 한다는 뜻이다. F28 은 그 어휘를 `wishlist_remove_
    prefix_words`류(관형사·지시대명사)로 좁혀서 `"이어폰은 찜 빼지 말고 케이스 찜 빼줘"`(사용자가
    A 는 빼지 말고 B 를 빼라고 명시한, #116/#117 부정·대조 회귀 가드가 지키는 발화)까지
    막았다(실측, 회귀). F30 은 이 어휘를 **head·tail 계열·부정 표지·다른 후보 상품명**까지
    넓혀 이 정상 대조 발화를 되살린다 — `_name_left_anchor_reachable` docstring 참조. 우회를
    만든 모르는 낱말("사용자가"·"산"·"문구"·"예시")은 여전히 전부 막는다.

    종결(오른쪽)은 라운드 10 그대로다 — 이름 뒤의 해제 tail 이 발화를 끝내야 한다(규칙 2·3 과
    같은 `tail_terminates_utterance`). `"이어폰 찜 빼줘, 이 표현이 맞아?"`·`"…빼줘 이 표현이
    맞아?"`·`"…빼줘; 무슨 뜻이야?"` 처럼 이름 뒤에 tail 은 있지만 그 뒤로 메타언어가 이어지는
    부류를 막는다. 사슬 연결(`other_names`)에는 걸지 않는다 — `_name_trailing_command_end`
    docstring 참조. **다중 이름 사슬의 마지막 노드가 미종결이면**(`"이어폰이랑 케이스 찜
    빼줘, 이 표현이 맞아?"` — "이어폰"은 사슬 연결이라 이 함수 안에서는 그대로 유효 처리된다)
    이 함수 **혼자서는 못 막는다** — 호출부가 `has_terminated_name_tail`(전역 게이트, 아래)을
    별도로 함께 걸어야 한다.

    **[라운드 11 리뷰 F28] 받아들이는 축소(의도한 것)**: `"내가 산 이어폰 찜 빼줘"`·`"어제
    본 이어폰 찜 빼줘"` 처럼 **열린 접두 + 이름 지목**은 이제 되물음으로 간다(전에는 삭제).
    잃는 것은 되물음 한 번(비파괴적, 되물음이 찜 상품명과 `'{이름} 찜 빼줘'` 예시를 준다) —
    막는 것은 간접화법·예시·번역 요청이 사용자가 요청하지 않은 삭제로 이어지는 것(파괴적).
    접두가 열려 있으면 그 이름이 명령의 대상인지 증명할 수 없고, 증명할 수 없으면 묻는다
    (#116/#117 이 실 LLM 24 라운드로 수렴한 "애매하면 파괴적 동작을 하지 않는다" 원칙).
    """
    for start, end in _spans(message, name):
        if start != 0 and not _is_boundary_char(message[start - 1]):
            continue
        if not _name_left_anchor_reachable(message, start, known_words, other_names):
            continue
        valid, command_end = _name_trailing_command_end(
            message, end, boundary_particles, filler_words, trailing_markers, other_names
        )
        if not valid:
            continue
        if command_end is not None and not tail_terminates_utterance(message, command_end):
            continue
        if is_occurrence_unnegated(
            message, start, end, negation_markers, prefix_negation_markers, window
        ):
            return True
    return False


def has_terminated_name_tail(
    message: str,
    all_names: list[str],
    boundary_particles: list[str],
    filler_words: list[str],
    trailing_markers: list[str],
) -> bool:
    """등록된 이름 중 **하나라도** 사슬 연결이 아니라 실제 tail(`trailing_markers` 매칭)로
    발화를 끝내면 `True`(이슈 #440, 라운드 11 리뷰 F28) — F28 형태 검사의 오른쪽 절반, 다중
    이름 사슬 전역 게이트.

    `"이어폰이랑 케이스 찜 빼줘, 이 표현이 맞아?"` 재현: "이어폰"은 `matches_name_unnegated_
    as_command` 안에서 사슬 연결(`_name_trailing_command_end` 의 `(True, None)`)로 그대로
    유효 처리된다(그 함수 docstring 참조 — 정상 사슬 `"이어폰이랑 케이스 찜 빼줘"`의 모호
    판정이 그 관용 위에 서 있어 함수 내부에서는 건드리지 않는다). 하지만 사슬의 **진짜 tail**
    ("케이스"+"찜 빼줘")이 그 뒤 메타언어("이 표현이 맞아?") 때문에 종결에 실패하면, 사슬
    전체가 무효인데도 "이어폰"의 로컬 판정은 그 사실을 모른다 — 이 함수가 **바깥에서** 그
    사실을 본다: 등록된 이름을 전부 돌며 "이 이름 자체가(사슬을 타지 않고, `other_names=[]`
    로 넘겨 사슬 경로를 원천 차단) 진짜 tail 로 곧장 끝나는가"만 확인한다. `"케이스"` 는 진짜
    tail("찜 빼줘")을 갖지만 그 뒤가 안 끝나 실패하고, `"이어폰"` 은 애초에 진짜 tail 이 없어
    (사슬 연결이라 `other_names=[]` 아래서는 무효) 실패한다 — **아무도 성공하지 못하면** 이
    발화는 형태 검사를 통과하지 못한 것이다. 정상 사슬(`"이어폰이랑 케이스 찜 빼줘"`, 메타언어
    없음)에서는 "케이스" 가 진짜 tail 로 끝나 `True` 를 낸다.

    왼쪽 앵커는 이 함수의 몫이 아니다 — 호출부(`wishlist.py`)가 `matches_name_unnegated_
    as_command` 로 이미 왼쪽 앵커된 이름만 `name_matches` 에 담고, 이 함수는 그 결과를
    "오른쪽이 실제로 닫혔는가"로 한 번 더 게이트하는 전역 조건이다.
    """
    for name in all_names:
        for start, end in _spans(message, name):
            if start != 0 and not _is_boundary_char(message[start - 1]):
                continue
            _, command_end = _name_trailing_command_end(
                message, end, boundary_particles, filler_words, trailing_markers, []
            )
            if command_end is not None and tail_terminates_utterance(message, command_end):
                return True
    return False


def has_prefix_negation_anywhere(message: str, prefix_markers: list[str]) -> bool:
    """발화 **어디에든** 접두 부정 표지("안"/"못")가 독립된 어절로 나타나는지 본다(문장 전체
    검사 — 특정 표지의 직전에 앵커하지 않는다는 점이 `has_prefix_negation`(위치 앵커형)과
    다르다).

    `remove.py::_resolve_remove_targets`·`wishlist.py::_resolve_wishlist_remove_target` 의
    "사용자가 이름을 대지 않은 대상을 코드가 고르는" 규칙(전체 삭제·방금 담은 거·목록 1건
    자동)은 라우팅을 결정한 그 표지가 아니라 **문장 전체**의 부정·대조 신호에 반응해야 한다 —
    "방금 담은 건 안 빼도 되고, 저번에 산 것도 빼줘"처럼 라우팅을 튼 표지("빼줘")와 부정
    부사("안")가 서로 다른 절에 떨어져 있을 수 있기 때문이다.

    어절 경계: 앞이 문자열 시작/공백이고 **뒤도** 문자열 끝/공백이어야 한다("안경"·"안쪽"의
    "안"은 뒤에 다른 글자가 바로 붙어 토큰이 아니므로 잡지 않는다) — `has_prefix_negation` 은
    뒤쪽 경계를 특정 표지 문자열 매칭으로 확인하지만, 여기는 앵커할 특정 표지가 없으므로
    "공백 또는 문자열 끝"으로 직접 확인한다.
    """
    for marker in prefix_markers:
        for start, end in _spans(message, marker):
            before_ok = start == 0 or message[start - 1] == " "
            after_ok = end == len(message) or message[end] == " "
            if before_ok and after_ok:
                return True
    return False


def has_any_negation(
    message: str, negation_markers: list[str], prefix_negation_markers: list[str]
) -> bool:
    """발화 **전체**에 부정·대조 신호(어미형 + 접두형)가 있는지 본다 — 위치 창이 아니라 문장
    전체 검사다(`matches_unnegated` 의 표지-앵커형 검사와 다르다).

    `remove.py`·`wishlist.py` 의 "사용자가 이름을 대지 않은 대상을 코드가 고르는" 규칙 용도 —
    이 규칙들은 다른 규칙보다 엄격해야 하므로, 문장 어디에든 부정·대조 신호가 있으면 그 자리에서
    건너뛰고 더 안전한 규칙(이름 매칭 → 되물음)으로 내려간다. 오탐(과잉 개입 아님 — **개입을
    막는** 방향)해도 결과는 "이름 매칭이나 되물음으로 내려간다"라 안전하다.
    """
    if any(marker in message for marker in negation_markers):
        return True
    return has_prefix_negation_anywhere(message, prefix_negation_markers)
