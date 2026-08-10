"""브랜드 법인 표기 확장 (#466) — `app.pipelines.brand_aliases`.

정밀도(무엇을 **안** 끌어오는가)가 이 모듈의 핵심 계약이라 배제 케이스를 함께 고정한다.
"""

from __future__ import annotations

import pytest

from app.pipelines.brand_aliases import (
    CORPORATE_SUFFIXES,
    brand_wire_values,
    expand_brands,
    is_admissible_alias,
)


def test_expansion_reaches_the_measured_corporate_forms() -> None:
    """운영 시드 실측에서 실제로 못 닿던 두 쌍이 확장으로 닿는다.

    `삼성` 원문은 78건 중 7건(9.0%), `LG` 는 38건 중 1건(2.6%)에만 닿았다
    (`~/inte-final/_sql/mariadb/20_brand.sql` × `30_product.sql` 조인). 부족분의 정체가
    `삼성전자`(71건)·`LG전자`(37건)라 이 두 표기가 실리는지가 곧 이 모듈의 존재 이유다.
    """
    assert "삼성전자" in expand_brands(["삼성"], cap=12)
    assert "LG전자" in expand_brands(["LG"], cap=12)


@pytest.mark.parametrize("other_line", ["삼성도어", "삼성메디칼"])
def test_expansion_never_reaches_a_different_line_of_business(other_line: str) -> None:
    """업종 명사는 접미사 화이트리스트에 없으므로 **구조적으로** 안 실린다.

    이게 깨지면 "삼성 아무거나"가 문짝·의료기기를 후보로 끌고 온다 — exact IN 이라 조용히
    섞이고, 조건칩엔 여전히 "삼성"만 떠서 표시-실제가 어긋난다.
    """
    assert other_line not in expand_brands(["삼성"], cap=99)
    assert not is_admissible_alias("삼성", other_line)


def test_admissible_alias_requires_exact_suffix_match_not_substring() -> None:
    """부분 문자열이 아니라 `토큰+접미사` 정확 일치다 (lessons 2026-08-02)."""
    assert is_admissible_alias("삼성", "삼성전자")
    assert not is_admissible_alias("삼성", "삼성전자서비스")  # 접미사 뒤에 더 붙은 것
    assert not is_admissible_alias("삼성", "삼성")  # 자기 자신은 별칭이 아니다
    assert not is_admissible_alias("", "전자")
    assert not is_admissible_alias("삼성", "")


def test_user_surface_form_comes_first_and_survives_the_cap() -> None:
    """상한에 걸려도 **사용자가 말한 표기**가 남는다 — 확장이 종전 동작을 밀어내지 않는다."""
    assert expand_brands(["삼성"], cap=1) == ["삼성"]
    out = expand_brands(["삼성", "LG"], cap=2)
    assert out == ["삼성", "LG"]


def test_expansion_is_additive_and_order_stable() -> None:
    """원문 전량이 앞에 오고 그 뒤에 후보가 붙는다(순서 보존·중복 접기)."""
    out = expand_brands(["삼성", "삼성"], cap=12)
    assert out[0] == "삼성"
    assert out.count("삼성") == 1
    assert set(out) == {"삼성"} | {"삼성" + s for s in CORPORATE_SUFFIXES}


def test_blank_only_values_yield_nothing() -> None:
    """공백-only 는 버린다 — `brandName=` 빈값이 나가지 않게(#127 리뷰 규약)."""
    assert expand_brands(["  ", ""], cap=12) == []
    assert brand_wire_values(["  "], enabled=True, cap=12) is None


def test_wire_values_none_when_disabled_or_empty() -> None:
    """None 은 "종전 경로" 신호다 — 확장 실패가 검색을 좁히거나 비우면 안 된다."""
    assert brand_wire_values(["삼성"], enabled=False, cap=12) is None
    assert brand_wire_values(None, enabled=True, cap=12) is None
    assert brand_wire_values([], enabled=True, cap=12) is None
    assert brand_wire_values(["삼성"], enabled=True, cap=0) is None


def test_wire_values_expand_when_enabled() -> None:
    values = brand_wire_values(["삼성"], enabled=True, cap=12)
    assert values is not None
    assert values[0] == "삼성" and "삼성전자" in values


def test_suffix_list_stays_closed() -> None:
    """화이트리스트를 넓히려면 전수 대조를 다시 돌려야 한다 — 무심코 늘어나는 것을 막는다.

    `코리아`·`KOREA`·`그룹` 은 2,368행 전수에서 잇는 쌍이 0건이라 뺐다(모듈 docstring).
    이 테스트가 실패하면 접미사를 늘린 것이므로, 그 근거(새로 잇는 쌍 + 교차 오염 0건)를
    docstring 표에 적었는지 확인하라.
    """
    assert CORPORATE_SUFFIXES == ("전자",)


# ── 거울상 결함: 접미사 양방향 (#466, 리뷰 지적) ────────────────────────────────────────


def test_expansion_is_bidirectional_on_the_corporate_suffix() -> None:
    """"삼성전자"라고 말한 사용자도 `삼성` 행에 닿아야 한다.

    붙이기만 하면 거울상 결함이 남는다 — 그리고 프롬프트의 "원문 표기 그대로" 규칙 때문에
    사용자가 정식 사명을 그대로 말하는 발화가 실제로 자주 들어온다. 시드 실측에서 역방향은
    정방향과 **같은 3쌍**만 잇고 추가 오염이 없다.
    """
    assert "삼성" in expand_brands(["삼성전자"], cap=12)
    assert "LG" in expand_brands(["LG전자"], cap=12)
    assert is_admissible_alias("삼성전자", "삼성")


def test_suffix_strip_never_produces_an_empty_token() -> None:
    """접미사만으로 이뤄진 값("전자")을 벗기면 빈 문자열이 된다 — 실리면 안 된다."""
    assert "" not in expand_brands(["전자"], cap=12)
    assert all(v.strip() for v in expand_brands(["전자"], cap=12))


def test_suffix_token_itself_is_inert_not_special_cased() -> None:
    """"전자" 는 "전자전자" 를 낳는다 — §4.6 이 미존재 이름을 무시하므로 무해하다.

    의도적으로 특수 처리하지 않는다는 사실을 테스트로 남긴다(고려 안 한 것이 아니다).
    """
    assert "전자전자" in expand_brands(["전자"], cap=12)


# ── 음차 쌍 (#466, 리뷰 지적: 애플 1건 vs Apple 7건) ────────────────────────────────────


def test_script_pair_expansion_recovers_the_latin_side() -> None:
    """`애플` 원문만 실으면 시드 8건 중 1건에만 닿는다 — `Apple`(7건)을 함께 실어 회복한다."""
    out = expand_brands(["애플"], cap=12)
    assert out[0] == "애플" and "Apple" in out


def test_script_pair_expansion_is_bidirectional() -> None:
    """라틴 표기로 말해도 한글 행에 닿아야 한다(`나이키` 106건이 그쪽에 있다)."""
    assert "나이키" in expand_brands(["Nike"], cap=12)


def test_script_pair_lookup_is_case_insensitive() -> None:
    assert "애플" in expand_brands(["APPLE"], cap=12)


def test_script_pairs_are_transliterations_not_translations() -> None:
    """등재 기준 회귀 — 의미 번역이 들어오면 브랜드가 아닌 낱말이 브랜드로 나갈 수 있다."""
    from app.pipelines.brand_aliases import SCRIPT_PAIRS

    korean = [k for k, _ in SCRIPT_PAIRS]
    assert len(korean) == len(set(korean))  # 한글 키 중복 없음
    latin = [v for _, v in SCRIPT_PAIRS]
    assert len(latin) == len(set(latin))
    assert ("애플", "Apple") in SCRIPT_PAIRS


def test_unknown_brand_gets_only_suffix_candidates() -> None:
    """음차 쌍에 없는 브랜드는 종전 동작 — 목록이 전수가 아니어도 회귀가 아니다."""
    out = expand_brands(["설화수"], cap=12)
    assert out[0] == "설화수"
    assert set(out) == {"설화수", "설화수전자"}
