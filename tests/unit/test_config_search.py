"""#101 방식2 검색 백엔드 Settings 신규 필드 테스트.

pgvector 2차 압축(방식2)을 hot path 기본으로 켜는 토글과 압축 상한이 config 주입되는지 확인.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_search_backend_and_rerank_limit_defaults() -> None:
    """방식2 기본 백엔드 + embedding_rerank_limit 기본값 — 하드코딩 금지, config 주입."""
    s = Settings(_env_file=None)
    assert s.search_backend == "embedding_rerank"  # MVP 기본 = Spring 전량 → pgvector 압축(방식2)
    assert (
        s.embedding_rerank_limit == 30
    )  # pgvector 압축 후 Sonnet 입력 상한(옛 "FastAPI 30" 이관처)


def test_search_backend_literal_rejects_unknown() -> None:
    """search_backend 는 Literal — 미정의 값은 로드 시 거부한다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_backend="nonsense")


def test_negative_embedding_rerank_limit_rejected() -> None:
    """embedding_rerank_limit 은 products[:limit] 절단에 쓰이므로 ge=0.

    음수면 Python slice 가 '뒤에서 제외'로 뒤집혀 '<=0 이면 0개' 절단 불변식이 깨진다
    (형제 category_fanout_* 와 동일 규약, PR #73 리뷰).
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_rerank_limit=-1)


def test_tier_thresholds_defaults_ordered() -> None:
    """등급 티어 경계(#171 PR#172) — 하드코딩 금지·config 주입 + 기본값이 내림차순."""
    s = Settings(_env_file=None)
    assert s.rating_tier_excellent >= s.rating_tier_good >= s.rating_tier_fair
    assert s.review_tier_many >= s.review_tier_some >= s.review_tier_few
    assert (
        0
        < s.price_tier_very_cheap_ratio
        < s.price_tier_cheap_ratio
        <= 1.0
        <= s.price_tier_pricey_ratio
        < s.price_tier_very_pricey_ratio
    )
    assert s.price_tier_cheap_ratio < s.price_tier_pricey_ratio


def test_rating_tier_misordered_rejected() -> None:
    """[#171 PR#172 리뷰] rating 티어 경계가 excellent>=good>=fair 를 어기면 기동 fail-fast.

    _rating_tier 가 내림차순 순차 비교라, env 로 순서가 뒤집히면 중간 구간이 조용히 엉뚱한 등급으로
    나간다 — 예외 없이. model_validator 로 기동 시점에 막는다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rating_tier_good=2.0, rating_tier_fair=3.0)  # good < fair


def test_review_tier_misordered_rejected() -> None:
    """[#171 PR#172 리뷰] review 티어 경계가 many>=some>=few 를 어기면 기동 fail-fast."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, review_tier_some=3, review_tier_few=10)  # some < few


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_tier_very_cheap_ratio": 0},
        {"price_tier_very_cheap_ratio": 0.9},
        {"price_tier_cheap_ratio": 1.15},
        {"price_tier_pricey_ratio": 1.6},
        {"price_tier_cheap_ratio": 1.2, "price_tier_pricey_ratio": 1.3},
        {"price_tier_pricey_ratio": 0.9},
        {"price_tier_very_cheap_ratio": 0.85},
        {"price_tier_very_pricey_ratio": 1.15},
    ],
)
def test_price_tier_misordered_or_overlapping_rejected(overrides: dict[str, float]) -> None:
    """[#173] price 티어 경계 순서가 뒤집히거나 cheap/pricey 가 겹치면 기동 fail-fast."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_price_tier_cheap_and_pricey_cannot_both_equal_median() -> None:
    """[#173] cheap=pricey=1.0 은 저렴·비쌈 경계를 없애므로 엄격 부등호로 거부한다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, price_tier_cheap_ratio=1.0, price_tier_pricey_ratio=1.0)


def test_price_group_min_size_default_and_lower_bound() -> None:
    """[#236] 카테고리 그룹 하한은 config 주입이며 0 이하는 기동 fail-fast.

    0 이면 어떤 그룹도 하한에 걸리지 않아 폴백 자체가 죽은 설정이 되고, 음수는 의미가 없다.
    1 은 "싱글턴도 자기 중앙값을 쓴다"(사실상 해제)라 유효 값으로 허용한다.
    """
    assert Settings(_env_file=None).price_group_min_size == 2
    assert Settings(_env_file=None, price_group_min_size=1).price_group_min_size == 1
    with pytest.raises(ValidationError):
        Settings(_env_file=None, price_group_min_size=0)
