"""옵션 좁히기 순수 함수 단위 테스트 (이슈 #455) — `app/agents/buyer/cart/options.py`.

I/O 없는 순수 함수라 `CartStateStore`·`stream_cart_add` 구동 없이 직접 검증한다. 담기 흐름
회귀·되물음 문구 테스트는 `tests/unit/test_cart.py` 의 "이슈 #455" 절에 있다.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agents.buyer.cart.options import (
    OptionHint,
    color_condition_terms,
    condition_terms,
    narrow_options,
    options_have_color_axis,
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


@pytest.mark.parametrize(
    ("option_names", "message", "terms"),
    [
        (("그레이라이트", "네이비"), "그레이로 담아줘", ("그레이",)),
        (("라이트그레이", "블랙"), "그레이로 담아줘", ("그레이",)),
        (("니트릴 장갑", "면 장갑"), "니트로 담아줘", ("니트",)),
    ],
)
def test_narrow_options_term_substring_of_name_does_not_feed_by_message(
    option_names: tuple[str, str], message: str, terms: tuple[str, ...]
) -> None:
    """(#455 리뷰 F-5) term 이 발화에 토큰으로 나타나도, 옵션 이름의 부분 문자열일 뿐 세그먼트와
    정확히 같지 않으면 R1(자동 선택 근거)엔 실리지 않는다 — R2(by_condition, 되물음 문구 좁히기)엔
    남아 조용히 담기는 대신 되묻는다."""
    matching, other = _opt(1, option_names[0]), _opt(2, option_names[1])

    narrowing = narrow_options(
        [matching, other],
        message=message,
        terms=terms,
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == ()
    assert narrowing.by_condition == (matching,)


def test_narrow_options_term_exact_segment_match_still_feeds_by_message() -> None:
    """(#455 리뷰 F-5 회귀) term 이 옵션 세그먼트와 정확히 같으면 여전히 R1 에 실려 자동 선택
    근거가 된다 — 이번 수정이 막는 것은 부분 문자열 매칭뿐, 정확 일치 경로는 살아 있다."""
    red_m = _opt(1, "레드/M")
    blue_m = _opt(2, "블루/M")

    narrowing = narrow_options(
        [red_m, blue_m],
        message="레드로 담아줘",
        terms=("레드",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_message == (red_m,)


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


# ─────────── 색상 동의어 등가 — R2 전용 (이슈 #454) ───────────
#
# 사전은 `app.pipelines.color_synonyms.get_synonym_map` 산출 그대로다 — 정규화된(strip+
# casefold) 표기 → 승인 묶음 목록. 테스트에서도 그 규약 그대로 손으로 구성한다.

_BLACK_SYNONYMS = {"검정": ["블랙", "검정", "흑색"], "블랙": ["블랙", "검정", "흑색"]}


def test_narrow_options_color_synonyms_expand_by_condition() -> None:
    """(핵심 이득) 조건어 "검정" + 옵션명 "블랙" — 사전이 있으면 R2 가 등가로 매칭한다."""
    black = _opt(1, "블랙")
    white = _opt(2, "화이트")

    narrowing = narrow_options(
        [black, white],
        message="아무거나 담아줘",
        terms=("검정",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
        color_synonyms=_BLACK_SYNONYMS,
    )

    assert narrowing.by_condition == (black,)


def test_narrow_options_color_synonyms_absent_do_not_match() -> None:
    """사전을 안 주면(기본 `None`) "검정"↔"블랙" 등가는 성립하지 않는다 — 오늘 동작 그대로."""
    black = _opt(1, "블랙")
    white = _opt(2, "화이트")

    narrowing = narrow_options(
        [black, white],
        message="아무거나 담아줘",
        terms=("검정",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
    )

    assert narrowing.by_condition == ()


def test_narrow_options_color_synonyms_never_feed_by_message() -> None:
    """(R1 불변, #454) 사전을 줘도 발화 매칭(R1/`by_message`)은 등가를 보지 않는다 — 자동 선택
    근거를 넓히면 사용자가 말한 적 없는 옵션이 담긴다(#455 리뷰 F-1 비대칭 유지)."""
    black = _opt(1, "블랙")
    white = _opt(2, "화이트")

    narrowing = narrow_options(
        [black, white],
        message="검정으로 담아줘",  # 옵션명은 "블랙" — 리터럴로는 발화에 안 나타난다
        terms=("검정",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
        color_synonyms=_BLACK_SYNONYMS,
    )

    assert narrowing.by_message == ()  # R1 은 등가를 몰라 매칭하지 않는다
    assert narrowing.by_condition == (black,)  # R2 는 등가로 좁힌다


def test_narrow_options_color_synonym_lookup_is_exact_key_not_substring() -> None:
    """사전 조회는 조건어 전체의 정확 일치만 본다 — "브라이트레드"는 "레드" 키에 안 걸린다
    (§1-3 SKU 코드류 오탐 방지)."""
    red = _opt(1, "빨강")
    blue = _opt(2, "파랑")
    mapping = {"레드": ["빨강", "레드"]}

    narrowing = narrow_options(
        [red, blue],
        message="아무거나 담아줘",
        terms=("브라이트레드",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
        color_synonyms=mapping,
    )

    assert narrowing.by_condition == ()


def test_narrow_options_condition_matched_all_true_when_every_option_matches() -> None:
    """(#454) 등가 확장으로 전 옵션이 매칭되면 `by_condition` 은 빈 튜플이지만
    `condition_matched_all` 은 참이다 — "0건 좁힘"과 구별하는 신호."""
    black_m = _opt(1, "블랙/M")
    black_l = _opt(2, "블랙/L")

    narrowing = narrow_options(
        [black_m, black_l],
        message="아무거나 담아줘",
        terms=("검정",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
        color_synonyms=_BLACK_SYNONYMS,
    )

    assert narrowing.by_condition == ()
    assert narrowing.condition_matched_all is True


def test_narrow_options_condition_matched_all_false_when_no_option_matches() -> None:
    """(#454) 아무것도 안 매칭되면 `by_condition` 도 빈 튜플, `condition_matched_all` 도 거짓
    — 위 전건 일치 케이스와 구별된다."""
    blue = _opt(1, "블루")
    green = _opt(2, "그린")

    narrowing = narrow_options(
        [blue, green],
        message="아무거나 담아줘",
        terms=("검정",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
        color_synonyms=_BLACK_SYNONYMS,
    )

    assert narrowing.by_condition == ()
    assert narrowing.condition_matched_all is False


def test_narrow_options_condition_matched_all_false_when_partially_narrowed() -> None:
    """일부만 매칭되는(진짜 좁힘) 경우엔 당연히 전건 일치가 아니다."""
    black = _opt(1, "블랙")
    white = _opt(2, "화이트")

    narrowing = narrow_options(
        [black, white],
        message="아무거나 담아줘",
        terms=("검정",),
        min_term_len=_MIN_TERM_LEN,
        match_suffixes=_SUFFIXES,
        color_synonyms=_BLACK_SYNONYMS,
    )

    assert narrowing.by_condition == (black,)
    assert narrowing.condition_matched_all is False


# ─────────── color_condition_terms — 조건어 중 색상어만 (이슈 #454) ───────────


def test_color_condition_terms_keeps_only_exact_dictionary_keys() -> None:
    mapping = {"검정": ["블랙", "검정"], "블랙": ["블랙", "검정"]}
    terms = color_condition_terms(("검정", "오버핏", "SKU-1234"), mapping)
    assert terms == ("검정",)


def test_color_condition_terms_empty_when_no_color_term() -> None:
    mapping = {"검정": ["블랙", "검정"]}
    assert color_condition_terms(("소재", "핏"), mapping) == ()


def test_color_condition_terms_dedupes_and_preserves_order() -> None:
    mapping = {"검정": ["블랙", "검정"], "빨강": ["레드", "빨강"]}
    terms = color_condition_terms(("검정", "빨강", "검정"), mapping)
    assert terms == ("검정", "빨강")


def test_color_condition_terms_substring_of_key_is_not_a_color_term() -> None:
    """ "레드" 는 사전 키가 "레드끈"뿐이면 색상어로 인정하지 않는다 — 정확 일치만."""
    mapping = {"레드끈": ["레드끈"]}
    assert color_condition_terms(("레드",), mapping) == ()


# ─────────── options_have_color_axis — 옵션명에 색상 축이 실재하는가 (이슈 #454) ───────────


def test_options_have_color_axis_true_when_segment_matches_dictionary_key() -> None:
    mapping = {"블랙": ["블랙", "검정"], "화이트": ["화이트", "흰색"]}
    options = [_opt(1, "블랙 / M"), _opt(2, "화이트 / M")]
    assert options_have_color_axis(options, mapping) is True


def test_options_have_color_axis_false_when_no_segment_matches() -> None:
    """사이즈·SKU 코드만 있는 옵션명은 색상 축이 없다고 판정한다(§1-3, 카탈로그 실측 40.1%)."""
    mapping = {"블랙": ["블랙", "검정"]}
    options = [_opt(1, "S"), _opt(2, "M"), _opt(3, "L")]
    assert options_have_color_axis(options, mapping) is False


def test_options_have_color_axis_false_when_only_unapproved_color_spelling() -> None:
    """승인 사전 밖 표기(영문 "Black")는 축이 있어도 못 잡는다 — 이 한계가 §1-3 이 말하는 것."""
    mapping = {"블랙": ["블랙", "검정"]}
    options = [_opt(1, "Black / M"), _opt(2, "White / M")]
    assert options_have_color_axis(options, mapping) is False
