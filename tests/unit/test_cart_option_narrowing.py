"""옵션 좁히기 순수 함수 단위 테스트 (이슈 #455) — `app/agents/buyer/cart/options.py`.

I/O 없는 순수 함수라 `CartStateStore`·`stream_cart_add` 구동 없이 직접 검증한다. 담기 흐름
회귀·되물음 문구 테스트는 `tests/unit/test_cart.py` 의 "이슈 #455" 절에 있다.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agents.buyer.cart.options import (
    OptionHint,
    condition_terms,
    narrow_options,
)
from app.core.config import get_settings
from app.schemas.spring import CartOption, ProductSearchFilters

_MIN_TERM_LEN = 2
# 프로덕션과 같은 조사·꼬리말 허용목록으로 잰다 — 테스트가 자체 목록을 따로 들면 config 를
# 바꿔도 테스트가 안 알아채는 드리프트가 생긴다(#455 리뷰 F-1).
_SUFFIXES = get_settings().cart_option_match_suffixes


def _opt(option_id: int, name: str) -> CartOption:
    return CartOption(option_id=option_id, name=name)


# ─────────── narrow_options — 세그먼트 분해·정규화·좁힘 판정 ───────────


def test_narrow_options_splits_name_into_segments() -> None:
    """옵션명은 `/`·`,`·`|`·공백으로 세그먼트 분해된다 — 전체 이름이 아니라 세그먼트가 매칭 단위."""
    red_large = _opt(1, "레드/라지")
    blue_small = _opt(2, "블루/스몰")

    narrowing = narrow_options(
        [red_large, blue_small],
        message="라지로 주세요",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == (red_large,)
    assert blue_small not in narrowing.by_message


def test_narrow_options_ignores_segments_shorter_than_min_term_len() -> None:
    """`min_term_len` 미만 세그먼트는 매칭에 쓰지 않는다 — "M" 단독이 아무 문장에나 걸리지 않는다."""
    size_m = _opt(1, "M")
    size_l = _opt(2, "L")

    narrowing = narrow_options(
        [size_m, size_l],
        message="M 사이즈로 담아줘",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    # 메시지에 "M" 이 문자 그대로 있어도 세그먼트 길이가 짧아 매칭되지 않는다 — 좁힌 게
    # 아니므로 by_message 는 빈 튜플이다.
    assert narrowing.by_message == ()


def test_narrow_options_normalizes_case_and_whitespace() -> None:
    """대소문자·공백 정규화 — 옵션명·메시지 모두 casefold + strip 후 비교한다."""
    red = _opt(1, "  RED  ")
    blue = _opt(2, "BLUE")

    narrowing = narrow_options(
        [red, blue],
        message="red 색상으로 담아줘",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == (red,)


def test_narrow_options_zero_matches_returns_empty_tuple() -> None:
    """조건에 하나도 안 걸리면 빈 튜플 — degrade 판정의 기반."""
    options = [_opt(1, "블루"), _opt(2, "그린")]

    narrowing = narrow_options(
        options,
        message="레드로 주세요",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()
    assert narrowing.by_condition == ()


def test_narrow_options_all_matched_is_not_narrowed() -> None:
    """전부 매칭되면 좁힌 게 아니다 — 집합 크기가 옵션 전체와 같으면 빈 튜플로 돌려준다."""
    options = [_opt(1, "화이트/M"), _opt(2, "화이트/L")]

    narrowing = narrow_options(
        options,
        message="화이트로 주세요",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()
    assert narrowing.by_condition == ()


def test_narrow_options_by_condition_includes_terms_not_in_message() -> None:
    """by_condition 은 이번 발화에 없는 누적 조건어도 R2 로 매칭한다(R1 ∪ R2)."""
    red = _opt(1, "레드")
    blue = _opt(2, "블루")

    narrowing = narrow_options(
        [red, blue],
        message="아무거나 담아줘",
        terms=("레드",),
        min_term_len=_MIN_TERM_LEN,
    )

    assert narrowing.by_message == ()  # 이번 발화엔 "레드"가 없다
    assert narrowing.by_condition == (red,)  # 누적 조건으로는 좁혀진다


def test_narrow_options_term_shorter_than_min_term_len_is_ignored() -> None:
    """`min_term_len` 미만인 조건어는 R2 매칭에도 쓰이지 않는다."""
    options = [_opt(1, "M 사이즈"), _opt(2, "L 사이즈")]

    narrowing = narrow_options(
        options, message="아무거나", terms=("M",), min_term_len=_MIN_TERM_LEN
    )

    assert narrowing.by_condition == ()


# ─────────── 발화 매칭은 토큰 경계를 본다 (#455 리뷰 F-1) ───────────
#
# `seg in message` 부분 문자열 포함은 더 긴 낱말 안에 우연히 들어간 세그먼트까지 매칭시킨다.
# 카탈로그 실측(`~/inte-final/_sql/mariadb/40_product_option.sql`) 상위 옵션명 블랙(224)·
# 화이트(82)·그레이(71)·핑크(65)·블루(49)·그린(46) 이 전부 이 함정에 걸린다 — 이 발화들은 이
# 옵션명이 존재하는 카탈로그에서 흔하다. 아래는 그 실제 시드에서 뽑은 회귀다.


def test_narrow_options_segment_inside_longer_word_does_not_match() -> None:
    """ "블루" ⊂ "블루투스"처럼 세그먼트가 더 긴 낱말 안에 우연히 들어가면 매칭되지 않는다."""
    blue = _opt(1, "블루")
    black = _opt(2, "블랙")

    narrowing = narrow_options(
        [blue, black],
        message="블루투스 이어폰 담아줘",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()


def test_narrow_options_segment_inside_brand_name_does_not_match() -> None:
    """브랜드명("블랙야크")에 우연히 들어간 옵션명("블랙")도 매칭되지 않는다."""
    black = _opt(1, "블랙")
    white = _opt(2, "화이트")

    narrowing = narrow_options(
        [black, white],
        message="블랙야크 등산화 담아줘",
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()


def test_narrow_options_message_term_inside_longer_word_does_not_feed_by_message() -> None:
    """`terms_in_message` 판정도 같은 토큰 경계 규칙을 쓴다 — `filters.color="블루"` 인데 발화가
    "블루투스"뿐이면 그 조건어는 "이번 발화에 나타남"으로 카운트되지 않는다."""
    blue = _opt(1, "블루/M")
    black = _opt(2, "블랙/M")
    green = _opt(3, "그린/M")

    narrowing = narrow_options(
        [blue, black, green],
        message="블루투스 이어폰 담아줘",
        terms=("블루",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()
    # R2(이름 매칭)는 이 규칙 밖이라 조건어 자체는 여전히 문구 좁히기에 쓰인다.
    assert narrowing.by_condition == (blue,)


@pytest.mark.parametrize(
    ("message", "expected_name"),
    [
        ("레드로 담아줘", "레드"),  # 조사 "로"
        ("블랙으로 담아줘", "블랙"),  # 받침 뒤 "으로"
        ("화이트 담아줘", "화이트"),  # 조사 없음(빈 꼬리)
    ],
)
def test_narrow_options_particle_suffixed_mentions_still_narrow(
    message: str, expected_name: str
) -> None:
    """정상 지목 경로는 살아 있다 — 조사가 붙거나 없거나 토큰이 세그먼트와 정확히 일치하면 매칭."""
    options = [_opt(1, "레드"), _opt(2, "블랙"), _opt(3, "화이트")]
    expected = next(o for o in options if o.name == expected_name)

    narrowing = narrow_options(
        options,
        message=message,
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == (expected,)


def test_narrow_options_particle_suffixed_mention_without_config_suffix_does_not_match() -> None:
    """허용목록에 없는 꼬리말이 붙으면(=진짜 다른 낱말) 매칭되지 않는다 — 토큰 일치가 핵심이지
    접두 일치가 아니다."""
    navy = _opt(1, "네이비")
    beige = _opt(2, "베이지")

    narrowing = narrow_options(
        [navy, beige],
        message="네이비블루 색상 있나요",  # "블루"는 허용목록 밖 꼬리 — 매칭 안 됨
        terms=(),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()


def test_narrow_options_r3_segment_is_substring_of_condition_term() -> None:
    """R3 세 번째 절(세그먼트가 term 의 부분 문자열) — 이름 매칭(R2/`by_condition`)에서만 적용되고
    발화 매칭(R1/`by_message`)에는 적용되지 않는다. 여기서는 `terms=` 로 실제 R3 경로를 태운다."""
    red = _opt(1, "레드")
    blue = _opt(2, "블루")

    narrowing = narrow_options(
        [red, blue],
        message="아무거나 담아줘",
        terms=("브라이트레드",),  # "레드" 세그먼트를 부분 문자열로 포함하는 조건어
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()  # 자동 선택 근거(R1)엔 안 실린다
    assert narrowing.by_condition == (red,)  # 문구 좁히기(R2)엔 실린다


# ─────────── condition_terms ───────────


def test_condition_terms_pulls_color_and_attr_conditions_only() -> None:
    """color·attr_conditions 값만 뽑는다 — keyword·semantic_query 는 제외."""
    filters = ProductSearchFilters(
        color="레드",
        keyword="니트",
        semantic_query="따뜻한 옷",
        attr_conditions={"소재": "울", "핏": "오버핏"},
    )

    terms = condition_terms(filters)

    assert "레드" in terms
    assert "울" in terms and "오버핏" in terms
    assert "니트" not in terms
    assert "따뜻한 옷" not in terms


def test_condition_terms_dedupes_and_preserves_order_prior_first() -> None:
    """여러 필터를 넘기면 먼저 넘긴 쪽(prior) 순서를 유지하고 중복을 뺀다."""
    prior = ProductSearchFilters(color="레드")
    current = ProductSearchFilters(color="레드", attr_conditions={"소재": "울"})

    terms = condition_terms(prior, current)

    assert terms == ("레드", "울")


def test_condition_terms_skips_none_filters() -> None:
    """`prior` 가 없는(신규 스레드) 경우에도 죽지 않는다."""
    current = ProductSearchFilters(color="블랙")

    assert condition_terms(None, current) == ("블랙",)


def test_condition_terms_empty_when_nothing_explicit() -> None:
    assert condition_terms(ProductSearchFilters()) == ()


# ─────────── OptionHint ───────────


def test_option_hint_is_frozen() -> None:
    hint = OptionHint(names=("레드", "블루"), total=5)
    assert hint.names == ("레드", "블루")
    assert hint.total == 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        hint.total = 6  # type: ignore[misc]
