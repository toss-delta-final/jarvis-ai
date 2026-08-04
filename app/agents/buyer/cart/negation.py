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


def _starts_with_particle(message: str, end: int, particles: list[str]) -> bool:
    return any(
        particle and message[end : end + len(particle)] == particle for particle in particles
    )


def matches_name_unnegated(
    message: str,
    name: str,
    negation_markers: list[str],
    window: int,
    prefix_negation_markers: list[str],
    boundary_particles: list[str],
) -> bool:
    """상품명이 발화에 **온전한 어절 경계로**, 그리고 **부정되지 않은 출현으로** 등장하는지 본다
    (라운드 15, head `0b33e06` 리뷰 B — `remove.py`/`wishlist.py` 의 이름 매칭 전용).

    `matches_unnegated` 와 합치지 않고 별도 함수로 둔다 — `matches_unnegated`(그리고 그걸 쓰는
    `intent_guard.py`)의 `markers` 인자는 "빼줘"·"장바구니에 넣어줘" 같은 **동사구**라 경계
    개념이 다르다(동사구는 애초에 앞뒤에 조사가 안 붙는 구조라 경계 검사가 무의미하거나 오히려
    해롭다). 반면 여기 `name` 은 **상품명**(사용자·판매자가 임의로 짓는 문자열)이라 다른
    낱말에 파묻힐 수 있다("이어폰케이스"의 "이어폰"). 두 검사를 하나로 합치면 동사구 매칭에도
    경계 검사가 강제돼 엉뚱한 회귀를 낳는다.

    경계 규칙: 이름 바로 **왼쪽**은 문자열 시작·공백·문장부호여야 하고, 바로 **오른쪽**은
    문자열 끝·공백·문장부호 **또는** `boundary_particles`(한국어 조사) 중 하나여야 유효한
    매칭이다. 순진하게 "오른쪽이 공백/문장부호가 아니면 전부 무효"로 자르면 조사가 이름
    바로 뒤에 붙는 정상 발화("그 세제**를** 빼줘")까지 깨진다 — 조사를 허용해야 "이어폰**케**
    이스"의 "이어폰"(뒤가 "케", 조사도 공백도 아님)만 걸러지고 "세제**를**"은 살아남는다.

    **알려진 한계**(이번 라운드 범위 밖, 의도적으로 손대지 않음): 이름 뒤가 **공백**이면 경계
    규칙을 그대로 통과하므로, "이어폰 케이스 빼줘"에서 장바구니의 "이어폰"이 여전히 매칭된다.
    사용자가 말한 "이어폰 케이스"가 통짜 상품명이라는 것을 알려면 장바구니에 **없는** 그 상품의
    이름을 알아야 하는데 이 함수는 그 정보가 없다. "뒤에 명사가 더 오면 무효" 류 추측성
    휴리스틱은 "이어폰이랑 세제 빼줘" 같은 정상 발화(접속 조사 뒤에 다른 상품명이 옴)를
    깨뜨리므로 넣지 않는다 — 플래그를 켜기 전에 다시 볼 항목이다.
    """
    for start, end in _spans(message, name):
        if start != 0 and not _is_boundary_char(message[start - 1]):
            continue
        if end != len(message) and not (
            _is_boundary_char(message[end])
            or _starts_with_particle(message, end, boundary_particles)
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
