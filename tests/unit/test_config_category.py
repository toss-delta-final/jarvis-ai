"""카테고리 하이브리드 매핑 Settings 신규 필드 테스트 (이슈 #59).

방식 A(추측→임베딩 보정)·canonical-or-null·멀티 fan-out 튜너블이 기본값으로 로드되는지 확인.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_category_mapping_settings_defaults() -> None:
    """카테고리 매핑·fan-out 튜너블 기본값 — 하드코딩 금지, config 주입."""
    settings = Settings(_env_file=None)
    assert settings.category_top_k == 5
    assert settings.category_fanout_max == 5
    assert settings.category_fanout_per_cat_limit == 10
    assert settings.category_fanout_merge_cap == 30


def test_pool_max_size_covers_fanout_concurrency() -> None:
    """검색 풀 max_size 는 fan-out 동시 조회(최대 category_fanout_max leg)를 커버해야 한다(PR #73 리뷰).

    psycopg_pool 기본 max_size(4) < fanout(5) 이면 5번째 leg 가 커넥션을 기다려 gather 병렬화가
    부분 무력화된다 — 암묵 하드코딩(psycopg 기본값)을 config 로 빼고 fanout 이상으로 명시한다.
    """
    settings = Settings(_env_file=None)
    assert settings.category_search_pool_max_size >= settings.category_fanout_max


def test_negative_fanout_max_rejected() -> None:
    """음수 fanout_max 는 로드 시 거부한다 — out[:fanout_max] 가 음수면 '뒤에서' 잘려 앞 항목이

    남아 "fanout_max<=0 이면 정확히 0개"라는 절단 불변식이 조용히 깨진다(category_mapping·
    decompose 두 슬라이스 공통). 소스에서 ge=0 으로 막는다(PR #73 리뷰).
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, category_fanout_max=-1)


def test_negative_slice_tunables_rejected() -> None:
    """merge_cap·per_cat_limit 도 같은 절단 규약(merged[:cap]·Spring size)이라 음수를 거부한다.

    _merge_fanout_results 의 merged[:cap] 은 cap 이 음수면 "뒤에서 제외"가 되어 "cap<=0 이면 0개"
    주석이 깨지고, per_cat_limit 은 음수 그대로 Spring 검색 limit 으로 나간다(PR #73 리뷰).
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, category_fanout_merge_cap=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, category_fanout_per_cat_limit=-1)


def test_needs_expansion_settings_defaults() -> None:
    """[#198] 상품 전개 튜너블 기본값 — 하드코딩 금지, config 주입(설계 §9)."""
    settings = Settings(_env_file=None)
    assert settings.needs_expansion_enabled is True
    assert settings.needs_expansion_tier == "fast"
    assert settings.needs_expansion_min_items == 2
    assert "선물" in settings.needs_expansion_purpose_markers


def test_needs_expansion_tier_rejects_unknown_value() -> None:
    """[PR #203 리뷰] tier 오타는 **부팅 시** 막는다 — 턴 예외로 번지지 않게.

    이 값은 `resolve_model_id` 에 들어가고 그것은 미지 tier 에 `LLMError` 를 던진다. 전개는 관측
    기록(api-spec §6.3)을 LLM 호출 `try` 보다 **먼저** 하므로, 오타 하나가 "전개 실패 → 원본 leg
    유지"(설계 §7 후퇴 없음)가 아니라 recommend 턴 전체의 예외가 된다. `LLMProvider`·`SearchBackend`
    와 같이 Literal 로 좁혀 pydantic 이 걸러낸다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, needs_expansion_tier="fasttt")
