"""부정·유보 판정 공용 모듈(이슈 #116·#117, 라운드 10) 단위 테스트.

`app/agents/buyer/cart/negation.py` — `intent_guard.py`·`remove.py`·`wishlist.py` 가 공유하는
판정을 직접 호출해 고정한다. 소비처별 사용은 `test_cart_intent_guard.py`·`test_cart_remove.py`·
`test_wishlist_flow.py` 에 있다 — 여기는 모듈 자체의 계약(함수 시그니처·경계 규칙)을 지킨다.
"""

from __future__ import annotations

from app.agents.buyer.cart.negation import (
    _consume_prefix,
    _spans,
    has_any_negation,
    has_prefix_negation,
    has_prefix_negation_anywhere,
    is_occurrence_unnegated,
    matches_unnegated,
)

_NEGATION_MARKERS = ["지 마", "지는 마", "지마", "하지 마", "말고", "야 할", "야 될"]
_PREFIX_MARKERS = ["안", "못"]


def test_spans_finds_all_overlapping_occurrences() -> None:
    assert _spans("aaa", "aa") == [(0, 2), (1, 3)]
    assert _spans("빼줘 빼줘", "빼줘") == [(0, 2), (3, 5)]
    assert _spans("아무거도 없음", "빼줘") == []
    assert _spans("아무거나", "") == []


def test_has_prefix_negation_word_boundary() -> None:
    assert has_prefix_negation("안 빼줘", 1, _PREFIX_MARKERS) is True
    assert has_prefix_negation("안빼줘", 1, _PREFIX_MARKERS) is True
    assert has_prefix_negation("안경 빼줘", 3, _PREFIX_MARKERS) is False
    message = "가방 안에 있는 거 빼줘"
    assert has_prefix_negation(message, message.index("빼줘"), _PREFIX_MARKERS) is False


def test_has_prefix_negation_anywhere_finds_standalone_token() -> None:
    """`has_prefix_negation_anywhere` 는 특정 표지에 앵커하지 않고 문장 전체에서 독립 어절
    "안"/"못"을 찾는다 — `remove.py`·`wishlist.py` 의 문장 전체 부정 가드 용도."""
    assert has_prefix_negation_anywhere("방금 담은 건 안 빼도 되고", _PREFIX_MARKERS) is True
    assert has_prefix_negation_anywhere("안 전부 빼줘도 되고", _PREFIX_MARKERS) is True
    assert has_prefix_negation_anywhere("못 전부 빼줘", _PREFIX_MARKERS) is True
    # 거짓 억제 방지 — "안경"의 "안"은 뒤에 "경"이 바로 붙어 독립 어절이 아니다.
    assert has_prefix_negation_anywhere("안경 빼줘", _PREFIX_MARKERS) is False
    # "안"이 아예 없으면 당연히 False.
    assert has_prefix_negation_anywhere("전부 빼줘", _PREFIX_MARKERS) is False


def test_is_occurrence_unnegated_checks_both_directions() -> None:
    # "빼줘"가 뒤쪽 어미형 부정에 걸리는 경우.
    msg = "장바구니에서 빼줘야 할까?"
    start = msg.index("빼줘")
    end = start + len("빼줘")
    assert is_occurrence_unnegated(msg, start, end, _NEGATION_MARKERS, _PREFIX_MARKERS, 8) is False
    # "빼줘"가 앞쪽 접두형 부정에 걸리는 경우.
    msg2 = "안 빼줘도 돼"
    start2 = msg2.index("빼줘")
    end2 = start2 + len("빼줘")
    assert (
        is_occurrence_unnegated(msg2, start2, end2, _NEGATION_MARKERS, _PREFIX_MARKERS, 8) is False
    )
    # 부정 신호가 전혀 없는 정상 출현.
    msg3 = "장바구니에서 빼줘"
    start3 = msg3.index("빼줘")
    end3 = start3 + len("빼줘")
    assert (
        is_occurrence_unnegated(msg3, start3, end3, _NEGATION_MARKERS, _PREFIX_MARKERS, 8) is True
    )


def test_matches_unnegated_requires_at_least_one_unnegated_occurrence() -> None:
    assert (
        matches_unnegated("장바구니에서 빼줘", ["빼줘"], _NEGATION_MARKERS, 8, _PREFIX_MARKERS)
        is True
    )
    assert (
        matches_unnegated("안 빼줘도 돼", ["빼줘"], _NEGATION_MARKERS, 8, _PREFIX_MARKERS) is False
    )
    # 같은 표지가 부정된 출현과 안 된 출현으로 둘 다 있으면 매칭이다(과소 매칭 방지).
    assert (
        matches_unnegated(
            "장바구니에 넣지는 마, 그래도 장바구니에 넣어줘",
            ["장바구니에 넣"],
            _NEGATION_MARKERS,
            8,
            _PREFIX_MARKERS,
        )
        is True
    )


# ─── 라운드 20(head `5772021` 리뷰): `_consume_prefix` 는 옵션 순서가 아니라 최장 일치를 쓴다 ───


def test_consume_prefix_picks_longest_match_regardless_of_option_order() -> None:
    """재현·고정(라운드 20 패킷) — "이"가 "이랑"의 접두사인 목록에서, 이전에는 리스트 순서상
    먼저 나온 옵션(첫 매칭)만 소비해 받침 있는 이름 뒤 "이랑"에서 "이"만 소비되고 "랑"이
    남았다(`_has_valid_name_trailing` 의 오른쪽 경계 검사를 깨뜨린 원인). 옵션을 어떤 순서로
    나열해도(짧은 것이 먼저든 긴 것이 먼저든) 항상 가장 긴 매칭이 선택돼야 한다."""
    message = "이어폰이랑 케이스"
    pos = message.index("이랑")
    short_first = ["이", "이랑", "이나"]
    long_first = ["이랑", "이나", "이"]
    assert _consume_prefix(message, pos, short_first) == pos + len("이랑")
    assert _consume_prefix(message, pos, long_first) == pos + len("이랑")

    # "이나"(선택 접속조사)도 "이"와 같은 접두 관계.
    message2 = "이어폰이나 케이스"
    pos2 = message2.index("이나")
    assert _consume_prefix(message2, pos2, short_first) == pos2 + len("이나")
    assert _consume_prefix(message2, pos2, long_first) == pos2 + len("이나")


def test_consume_prefix_no_match_returns_pos_unchanged() -> None:
    assert _consume_prefix("빼줘", 0, ["이", "이랑", "이나"]) == 0


def test_consume_prefix_single_char_option_still_matches_when_no_longer_option_fits() -> None:
    """접두 관계가 있는 옵션 목록이어도, 실제로 긴 쪽이 이어지지 않는 위치에서는 짧은 쪽이
    그대로 매칭돼야 한다(회귀 방지 — 최장 일치가 "무조건 실패"로 오작동하지 않는다)."""
    message = "세제이 빼줘"
    pos = message.index("이")
    assert _consume_prefix(message, pos, ["이", "이랑", "이나"]) == pos + len("이")


def test_has_any_negation_checks_suffix_and_prefix() -> None:
    """`remove.py`·`wishlist.py` 의 문장 전체 가드 용도 — 어미형·접두형 둘 다 본다."""
    assert has_any_negation("찜 취소하지 마", _NEGATION_MARKERS, _PREFIX_MARKERS) is True
    assert has_any_negation("안 전부 빼줘도 되고", _NEGATION_MARKERS, _PREFIX_MARKERS) is True
    assert has_any_negation("못 전부 빼줘", _NEGATION_MARKERS, _PREFIX_MARKERS) is True
    assert has_any_negation("전부 빼줘", _NEGATION_MARKERS, _PREFIX_MARKERS) is False
    # 거짓 억제 방지 — "안경"의 "안"은 독립 어절이 아니다.
    assert has_any_negation("안경 다 빼줘", _NEGATION_MARKERS, _PREFIX_MARKERS) is False


def _matches_name(message: str, name: str, other_names: list[str]) -> bool:
    from app.agents.buyer.cart.negation import matches_name_unnegated
    from app.core.config import get_settings

    settings = get_settings()
    return matches_name_unnegated(
        message,
        name,
        settings.utterance_negation_markers,
        settings.utterance_negation_window,
        settings.utterance_prefix_negation_markers,
        settings.utterance_name_boundary_particles,
        settings.utterance_name_trailing_filler_words,
        settings.cart_remove_markers,
        other_names,
    )


def test_matches_name_unnegated_glued_other_name_is_not_a_valid_trailing() -> None:
    """재현 — "이어폰케이스"는 "이어폰" 바로 뒤에 조사도 공백도 없이 "케이스"가 그대로 붙은
    합성 낱말이다. `other_names` 종결은 다른 상품명이 조사·filler 로 실제로 **이어질 때만**
    유효해야 하는데, 소비된 글자가 하나도 없어도 통과시키면 이런 합성어까지 "다른 이름으로
    이어진다"로 오인해 매칭시킨다 — 장바구니에 우연히 "케이스"가 같이 있으면 "이어폰"이
    사용자가 말하지 않은 상품인데도 삭제 대상으로 잡히는 데이터 의존적 결함이 된다."""
    assert _matches_name("이어폰케이스 빼줘", "이어폰", ["이어폰", "케이스"]) is False


def test_matches_name_unnegated_particle_separated_other_name_still_valid() -> None:
    """회귀 방지 — 조사로 실제로 이어지는 다른 상품명("파우치 블루랑 파우치 레드")은 조사가
    소비되므로 여전히 유효 종결이어야 한다. 이게 무효가 되면 "파우치 블루"가 매칭에서 빠지고
    "파우치 레드"만 단독 매칭돼, 둘 다 있어야 할 모호 판정(되물음)이 단일 확정으로 오판된다."""
    assert (
        _matches_name(
            "파우치 블루랑 파우치 레드 빼줘", "파우치 블루", ["파우치 블루", "파우치 레드"]
        )
        is True
    )
