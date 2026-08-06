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
    """검색 풀 max_size 는 한 턴의 동시 조회 **× 앵커 수**를 커버해야 한다(PR #73·#188 리뷰).

    psycopg_pool 기본 max_size(4) < fanout(5) 이면 5번째 leg 가 커넥션을 기다려 gather 병렬화가
    부분 무력화된다 — 암묵 하드코딩(psycopg 기본값)을 config 로 뺐다(PR #73).

    [#115] 앵커가 leg 당 raw·query **2개**가 되면서(§4.3) 한 턴의 동시 조회가 `2 × fanout_max`
    로 늘었다. 종전 기준(`>= fanout_max`)은 10 >= 5 로 통과하지만 **한 턴이 풀 전체를 소진**해
    동시 요청 헤드룸이 사라진다 — 기준을 실제 필요량으로 올린다(PR #188 리뷰).
    """
    settings = Settings(_env_file=None)
    assert settings.category_search_pool_max_size >= 2 * settings.category_fanout_max


def test_pool_max_size_below_anchor_concurrency_rejected() -> None:
    """풀이 `2 × fanout_max` 미만이면 **기동 시** 거부한다 (PR #188 리뷰).

    테스트만으로 막으면 `category_fanout_max` 를 올리는 쪽(#168 은 leg 10 을 계획)이 풀을 잊어도
    기본값 조합에서만 걸린다. 두 값은 함께 움직여야 하는 쌍이므로 config 불변식으로 고정해,
    fanout 을 올리는 순간 부팅이 막혀 이 결정을 반드시 다시 마주하게 한다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, category_fanout_max=10, category_search_pool_max_size=10)
    # 함께 올리면 통과한다 — 금지가 아니라 "짝을 맞춰라"는 제약임을 고정
    ok = Settings(_env_file=None, category_fanout_max=10, category_search_pool_max_size=24)
    assert ok.category_fanout_max == 10


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


def test_needs_expansion_has_no_detection_tunable() -> None:
    """[#217] 감지 튜너블이 없다 — 목적 marker 열거를 폐기하고 새 임계도 두지 않았다(설계 §9).

    트리거가 "매핑 실패"로 바뀌면서 판정은 기존 카테고리 임계를 그대로 재사용한다. 전개 전용
    거리 임계를 두는 안은 실측으로 기각됐고(§4.5 ④), 여기에 새 키가 다시 생기면 임계가 두 벌이 되어
    한쪽만 튜닝했을 때 조용히 어긋난다(`category_distance_override_margin` 과
    `category_select_margin_max` 의 서로소 불변식을 config 검증기로 고정한 것과 같은 이유).
    """
    assert "needs_expansion_purpose_markers" not in Settings.model_fields
    assert not [k for k in Settings.model_fields if k.startswith("needs_expansion_distance")]


def test_needs_expansion_tier_rejects_unknown_value() -> None:
    """[PR #203 리뷰] tier 오타는 **부팅 시** 막는다 — 턴 예외로 번지지 않게.

    이 값은 `resolve_model_id` 에 들어가고 그것은 미지 tier 에 `LLMError` 를 던진다. 전개는 관측
    기록(api-spec §6.3)을 LLM 호출 `try` 보다 **먼저** 하므로, 오타 하나가 "전개 실패 → 원본 leg
    유지"(설계 §7 후퇴 없음)가 아니라 recommend 턴 전체의 예외가 된다. `LLMProvider`·`SearchBackend`
    와 같이 Literal 로 좁혀 pydantic 이 걸러낸다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, needs_expansion_tier="fasttt")


def test_category_dictionary_startup_check_defaults_to_log() -> None:
    """[#401] 기본은 `log`(구성 오류를 시끄럽게 만들되 기동은 막지 않는다) — `fail` 이 아니다.

    사전 결측은 map_categories 가 canonical-or-null 로 무필터 퇴화하는 상태라 서비스는 계속
    응답 가능하다. 기동 거부(`fail`)는 서비스 전면 중단이라 하방이 무겁다 — 그래서 결함 교정은
    기본 on 이고 거부는 옵트인이다(app/core/config.py 필드 주석 참조).
    """
    settings = Settings(_env_file=None)
    assert settings.category_dictionary_startup_check == "log"


def test_category_dictionary_startup_check_rejects_unknown_value() -> None:
    """오타는 부팅 시 막는다 — Literal 로 좁혀 pydantic 이 걸러낸다(다른 tier 필드들과 같은 관례)."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, category_dictionary_startup_check="warn")


def test_category_dictionary_startup_check_accepts_off_and_fail() -> None:
    assert (
        Settings(
            _env_file=None, category_dictionary_startup_check="off"
        ).category_dictionary_startup_check
        == "off"
    )
    assert (
        Settings(
            _env_file=None, category_dictionary_startup_check="fail"
        ).category_dictionary_startup_check
        == "fail"
    )


def test_override_margin_must_exceed_select_trigger() -> None:
    """[PR #188 리뷰] 마진 예외(§4.5)와 택일 트리거(§4.4)의 구간은 겹치면 안 된다.

    두 임계는 **정반대 상태**를 가리킨다 — `margin <= select_margin_max` 는 "1·2위가 거의 붙어
    애매하다"(LLM 에게 물어본다), `margin >= override_margin` 은 "1위만 확 가까워 확신한다"
    (거리가 멀어도 채택한다). 한 leg 이 동시에 애매하면서 확신일 수는 없다.

    겹치면 **#115 가 폐기한 실패 모드가 되살아난다**: 마진이 얇다는 건 taxonomy 에 맞는 칸이
    없다는 신호인데(§4.5), 그 상태에서 LLM 이 고른 먼 후보를 채택하면 그게 바로 "억지 채택"이다
    (`'선물용품'` 마진 0.0095 → LLM 이 `도서/음반 > 독서용품` 0.2292 선택 → 드롭이 정답).

    관계가 주석·테스트에만 있으면 한쪽 임계를 튜닝할 때 조용히 겹칠 수 있어 config 로 고정한다.
    """
    settings = Settings(_env_file=None)
    assert settings.category_distance_override_margin > settings.category_select_margin_max

    with pytest.raises(ValidationError):  # 겹치는 조합은 기동 시 거부
        Settings(
            _env_file=None,
            category_select_margin_max=0.05,
            category_distance_override_margin=0.035,
        )
    # 함께 올리면 통과한다 — 금지가 아니라 "구간을 겹치지 마라"는 제약임을 고정
    ok = Settings(
        _env_file=None,
        category_select_margin_max=0.05,
        category_distance_override_margin=0.08,
    )
    assert ok.category_select_margin_max == 0.05
