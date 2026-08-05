"""인라인 팔의 leg 이름 매칭 — 정규화 후 정확 일치, 위치 매칭과의 구분 (#281 TASK-3-CORRECTION-2).

이 정정이 고치는 결함: 초판은 leg **개수**가 needs 개수와 다르면 이름이 일부 맞아도 전부
버렸다(inline-1 런이 legMismatch=lengthMismatch=emptySignal 전부 96/96 으로 나온 원인). 여기
테스트는 정규화 후 정확 일치 매칭이 개수 불일치와 무관하게 동작하는지 고정한다. 전부 합성
데이터라 CI 에서 API 콜이 0이다.
"""

from __future__ import annotations

from evals.priority_probe.runner import (
    _match_inline_legs_by_index,
    _match_inline_legs_by_name,
    _parse_inline_legs,
    diagnose_inline_raw,
)


def test_name_matching_succeeds_even_when_leg_count_differs_from_need_count() -> None:
    """[핵심 회귀] leg 4개·needs 3개 여도, 이름이 맞는 leg 는 정상적으로 매칭된다.

    초판은 개수가 다르면 전부 None 으로 버렸다(inline-1 의 원인). 개수 불일치는
    `lengthMismatch` 로 별도로 세지만, 이름 매칭 자체는 개수와 무관하게 시도해야 한다.
    """
    needs = ["텐트", "침낭", "버너"]
    legs = [
        ("캠핑", "텐트", 1),
        ("캠핑", "침낭", 1),
        ("캠핑", "버너", 2),
        ("캠핑", "랜턴", 3),  # needs 에 없는 여분의 leg
    ]

    result = _match_inline_legs_by_name(needs, legs)

    assert result.priorities == (1, 1, 2)
    assert result.unmatched_count == 0
    assert result.invalid_value_count == 0


def test_name_matching_uses_normalized_exact_match_not_substring() -> None:
    """부분 문자열 매칭은 쓰지 않는다(lessons 2026-08-02) — "이어폰 케이스" 는 "이어폰" 과 다르다."""
    needs = ["이어폰"]
    legs = [("음향가전", "이어폰 케이스", 2)]  # 부분 문자열은 포함하지만 다른 상품이다

    result = _match_inline_legs_by_name(needs, legs)

    assert result.priorities == (None,)
    assert result.unmatched_count == 1


def test_name_matching_normalizes_whitespace_and_case() -> None:
    needs = ["무선 이어폰"]
    legs = [("음향가전", "무선   이어폰", 1)]  # 공백이 여러 칸이어도 정규화 후 일치

    result = _match_inline_legs_by_name(needs, legs)

    assert result.priorities == (1,)
    assert result.unmatched_count == 0


def test_name_matching_falls_back_to_category_field() -> None:
    """query 가 비어도 category 가 니즈와 일치하면 매칭된다."""
    needs = ["등뼈"]
    legs = [("등뼈", None, 1)]

    result = _match_inline_legs_by_name(needs, legs)

    assert result.priorities == (1,)


def test_name_matching_does_not_reuse_the_same_leg_for_two_needs() -> None:
    """leg 하나는 한 니즈에만 쓴다 — 같은 이름의 leg 가 여럿이면 순서대로 소비한다."""
    needs = ["김", "김"]  # 스키마는 니즈 중복을 막지만, 매칭 함수 자체의 불변식을 검증한다
    legs = [("식품", "김", 1), ("식품", "김", 2)]

    result = _match_inline_legs_by_name(needs, legs)

    assert result.priorities == (1, 2)  # 각 니즈가 서로 다른 leg 를 가져간다
    assert result.unmatched_count == 0


def test_name_matching_reports_invalid_value_separately_from_unmatched() -> None:
    """매칭은 됐는데 값이 범위 밖이면 `invalid_value_count` 로 간다 — `unmatched_count` 가 아니다."""
    needs = ["텐트"]
    legs = [("캠핑", "텐트", 5)]  # 매칭은 성공, 값만 범위 밖

    result = _match_inline_legs_by_name(needs, legs)

    assert result.priorities == (None,)
    assert result.unmatched_count == 0
    assert result.invalid_value_count == 1


def test_index_matching_requires_equal_leg_count() -> None:
    needs = ["a", "b", "c"]
    legs = [("x", "다른이름1", 1), ("x", "다른이름2", 2)]  # 개수가 다르다(2 vs 3)

    assert _match_inline_legs_by_index(needs, legs) is None


def test_index_matching_ignores_names_and_uses_position_only() -> None:
    """이름이 전혀 달라도 개수만 같으면 위치로 짝짓는다 — "순서 신호"만 보는 보조 채점."""
    needs = ["텐트", "침낭", "버너"]
    legs = [("x", "완전히 다른 이름", 1), ("x", "또 다른 이름", 1), ("x", "세번째", 2)]

    assert _match_inline_legs_by_index(needs, legs) == (1, 1, 2)


def test_parse_inline_legs_returns_raw_priority_unvalidated() -> None:
    """원시 파싱은 값을 검증하지 않는다 — 검증은 매칭 단계의 책임이다(재집계 가능성 보존)."""
    raw = '{"categoryQueries": [{"category": "a", "query": "b", "priority": "이상한값"}]}'

    parsed, legs = _parse_inline_legs(raw)

    assert parsed is True
    assert legs == [("a", "b", "이상한값")]


def test_parse_inline_legs_handles_unparseable_json() -> None:
    parsed, legs = _parse_inline_legs("이건 JSON 이 아닙니다")
    assert parsed is False
    assert legs == []


def test_diagnose_inline_raw_separates_unparsed_from_length_mismatch() -> None:
    """[TASK-3-CORRECTION-2 §2] unparsed 와 lengthMismatch 는 상호 배타적이다."""
    needs = ["a", "b"]

    unparsed = diagnose_inline_raw("이건 JSON 이 아닙니다", needs)
    assert unparsed["parsed"] is False
    assert unparsed["lengthMismatch"] is False  # 파싱 실패면 길이를 잴 수 없다

    mismatched = diagnose_inline_raw(
        '{"categoryQueries": [{"category": "a", "query": "a", "priority": 1}]}', needs
    )
    assert mismatched["parsed"] is True
    assert mismatched["lengthMismatch"] is True  # needs=2, legs=1


def test_diagnose_inline_raw_name_unmatched_can_coexist_with_length_match() -> None:
    """개수는 같아도(lengthMismatch=False) 이름이 안 맞으면 nameUnmatchedCount 가 오른다."""
    needs = ["텐트", "침낭"]
    raw = (
        '{"categoryQueries": ['
        '{"category": null, "query": "전혀 다른 상품1", "priority": 1}, '
        '{"category": null, "query": "전혀 다른 상품2", "priority": 2}]}'
    )

    diag = diagnose_inline_raw(raw, needs)

    assert diag["lengthMismatch"] is False
    assert diag["nameUnmatchedCount"] == 2
