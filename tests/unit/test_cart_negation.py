"""부정·유보 판정 공용 모듈(이슈 #116·#117, 라운드 10) 단위 테스트.

`app/agents/buyer/cart/negation.py` — `intent_guard.py`·`remove.py`·`wishlist.py` 가 공유하는
판정을 직접 호출해 고정한다. 소비처별 사용은 `test_cart_intent_guard.py`·`test_cart_remove.py`·
`test_wishlist_flow.py` 에 있다 — 여기는 모듈 자체의 계약(함수 시그니처·경계 규칙)을 지킨다.
"""

from __future__ import annotations

from app.agents.buyer.cart.negation import (
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


def test_has_any_negation_checks_suffix_and_prefix() -> None:
    """`remove.py`·`wishlist.py` 의 문장 전체 가드 용도 — 어미형·접두형 둘 다 본다."""
    assert has_any_negation("찜 취소하지 마", _NEGATION_MARKERS, _PREFIX_MARKERS) is True
    assert has_any_negation("안 전부 빼줘도 되고", _NEGATION_MARKERS, _PREFIX_MARKERS) is True
    assert has_any_negation("못 전부 빼줘", _NEGATION_MARKERS, _PREFIX_MARKERS) is True
    assert has_any_negation("전부 빼줘", _NEGATION_MARKERS, _PREFIX_MARKERS) is False
    # 거짓 억제 방지 — "안경"의 "안"은 독립 어절이 아니다.
    assert has_any_negation("안경 다 빼줘", _NEGATION_MARKERS, _PREFIX_MARKERS) is False
