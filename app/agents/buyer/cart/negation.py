"""발화 부정·유보 판정 공용 헬퍼 (이슈 #116·#117) — 어미형·접두형을 **한 곳**에서 본다.

이 PR 에서 부정 인지 결함이 세 번 났다: 라운드 5(어미형 도입) → 라운드 9(접두형을
`intent_guard.py` 에만 추가, `remove.py` 는 미적용으로 남음) → 라운드 10(`remove.py::
_resolve_remove_targets` 의 `has_negation` 이 접두형을 못 봐 플래그 on 시 실제 데이터 손실로
재현). 세 번 다 같은 개념이 파일마다 각자 구현돼 한쪽만 고쳐진 게 원인이라, 판정을 이 모듈
하나로 뽑고 `intent_guard.py`·`remove.py`·`wishlist.py` 가 전부 같은 함수를 쓰게 한다 —
다음에 또 한쪽만 고치는 재발을 구조적으로 막는다.

`app/agents/buyer/cart/graph.py::_all_spans` 를 가져오지 않고 여기서 복제한다 — `graph.py` 가
`intent_guard.py`/`remove.py`/`wishlist.py` 를 가져오므로, 여기서 `graph.py` 를 가져오면 순환
임포트가 된다(순환은 이 모듈 자체에는 없다 — `intent_guard.py` 는 cart 내 다른 모듈을 가져오지
않으므로 문제가 없었지만, 그래도 판정을 한곳에 두는 편이 "다음에 또 한쪽만 고치는" 재발을
막는다).
"""

from __future__ import annotations


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


_BOUNDARY_PUNCTUATION = frozenset(",.!?;:\"'()[]{}~·…")


def _is_boundary_char(ch: str) -> bool:
    return ch == " " or ch in _BOUNDARY_PUNCTUATION


def _consume_prefix(message: str, pos: int, options: list[str]) -> int:
    """`pos` 에서 `options` 중 하나가 리터럴로 시작하면 그만큼 소비한 새 위치를 돌려준다(못
    찾으면 `pos` 그대로)."""
    for option in options:
        if option and message[pos : pos + len(option)] == option:
            return pos + len(option)
    return pos


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
    """
    pos = _consume_prefix(message, end, boundary_particles)
    pos = _skip_trailing_filler(message, pos, filler_words)
    remainder = message[pos:]
    if remainder == "":
        return True
    if any(marker and remainder.startswith(marker) for marker in trailing_markers):
        return True
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
