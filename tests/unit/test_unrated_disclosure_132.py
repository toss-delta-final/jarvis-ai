"""#132 — 사용자가 평점을 명시한 턴에서 무평점 상품이 노출되면 그 사실을 고지한다.

#100 P0 이 rating 사후필터를 '반증된 것만 제거'로 바꾸면서(무평점 `rating=None`·`review_count==0`
보존, #171) "평점 4.5 이상"이라 **명시**한 사용자에게도 리뷰 없는 신상품이 그대로 올라온다.
현행 rerank 는 그 상품에 `ratingLevel: 평가없음` 을 주고 "평점을 근거로 삼지 말라"고만 지시한다 —
**거짓 주장은 막지만 고지는 하지 않는다.** 사용자는 4.5↑라 믿고 무평점 상품을 본다.

자동 완화는 이미 `relaxation_notice` 로 고지되는데(§3.3 #133) 무평점 통과만 조용한 비대칭을 없앤다.
고지는 **코드가 보장**한다 — 프롬프트 지시만으로는 (1) LLM 이 무시할 수 있고 (2) rationale 이 빈
검색순서 보충 카드는 `_reasons()` 에서 통째로 빠져 고지가 아예 못 실린다(PR #212 리뷰).
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.recommendation.graph import (
    _apply_unrated_disclosure,
    _unrated_product_ids,
)
from app.schemas.spring import ProductSearchFilters, SpringProduct

pytestmark = pytest.mark.anyio

_NOTICE = "평점 정보 없음"


def _product(pid: int, *, rating: float | None, review_count: int | None) -> SpringProduct:
    return SpringProduct(
        product_id=pid, name=f"P{pid}", price=1000, rating=rating, review_count=review_count
    )


# ─────────────── 무평점 판정 — rerank `_rating_tier` 와 같은 규약(#171/#100 P0) ───────────────


def test_unrated_ids_cover_none_rating_and_zero_reviews() -> None:
    """`rating is None`(미집계)과 `review_count == 0`(리뷰가 없어 나온 rating=0) 둘 다 무평점이다."""
    candidates = [
        _product(1, rating=None, review_count=None),  # 평점 미집계
        _product(2, rating=0.0, review_count=0),  # 리뷰 0건 → rating=0 은 데이터 부재(#171)
        _product(3, rating=4.8, review_count=120),  # 정상 평점
    ]
    filters = ProductSearchFilters(keyword="q", rating_min=4.5)

    assert _unrated_product_ids(candidates, filters) == {1, 2}


def test_unrated_ids_empty_when_user_did_not_ask_for_rating() -> None:
    """평점을 명시하지 않은 턴은 고지 대상이 아니다 — 기존 동작을 한 글자도 바꾸지 않는다."""
    candidates = [_product(1, rating=None, review_count=None)]

    assert _unrated_product_ids(candidates, ProductSearchFilters(keyword="q")) == set()


# ─────────────────────────── 고지 문구 결합 — 길이 규약 ───────────────────────────


def test_disclosure_appends_to_existing_rationale() -> None:
    """근거가 있으면 뒤에 붙인다 — 근거를 버리지 않는다."""
    out = _apply_unrated_disclosure("가벼워서 데일리로 좋아요", _NOTICE, 100)

    assert "가벼워서 데일리로 좋아요" in out
    assert _NOTICE in out


def test_disclosure_stands_alone_when_rationale_is_empty() -> None:
    """근거가 비어도 고지는 실린다 — 이게 없으면 보충 카드가 조용히 무고지로 나간다."""
    out = _apply_unrated_disclosure("", _NOTICE, 100)

    assert _NOTICE in out


def test_disclosure_survives_truncation_by_trimming_the_rationale() -> None:
    """상한을 넘으면 **근거를 자르고 고지를 남긴다** — 반대로 자르면 고지가 사라진다."""
    out = _apply_unrated_disclosure("아" * 200, _NOTICE, 40)

    assert len(out) <= 40
    assert _NOTICE in out


def test_disclosure_alone_is_truncated_when_longer_than_limit() -> None:
    """고지 자체가 상한보다 길면 상한을 지킨다 — 계약(reason_max_len)이 우선이다."""
    out = _apply_unrated_disclosure("근거", "고" * 50, 10)

    assert len(out) <= 10


def test_no_disclosure_returns_sanitized_reason_unchanged() -> None:
    """고지 문구가 비어 있으면(운영자가 끔) 기존 정제 결과와 같다."""
    assert _apply_unrated_disclosure("가벼워요", "", 100) == "가벼워요"


# ─────────────────────────── rerank 프롬프트 — 조건부 지시 ───────────────────────────


class _CapturingLLM:
    """complete 호출 인자를 기록하는 LLM — 프롬프트 구성을 검증한다(test_recommendation 관례)."""

    def __init__(self) -> None:
        self.user: list[str] = []

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        self.user.append(user)
        return json.dumps(
            {"ranked": [{"productId": 1, "rationale": "좋아요"}], "overallComment": "c"},
            ensure_ascii=False,
        )


async def test_rerank_adds_unrated_instruction_only_when_rating_was_requested() -> None:
    """평점 명시 턴에만 지시가 붙는다 — 그 외 경로의 프롬프트는 불변(#198 희석 전례)."""
    from app.agents.buyer.recommendation.rerank import _UNRATED_DISCLOSURE, rerank

    candidates = [_product(1, rating=None, review_count=None)]

    asked = _CapturingLLM()
    await rerank(
        asked,
        query="평점 4.5 이상 티셔츠",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=5,
        rating_min_requested=True,
    )
    assert _UNRATED_DISCLOSURE in asked.user[0]

    not_asked = _CapturingLLM()
    await rerank(
        not_asked,
        query="티셔츠",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=5,
    )
    assert _UNRATED_DISCLOSURE not in not_asked.user[0]
