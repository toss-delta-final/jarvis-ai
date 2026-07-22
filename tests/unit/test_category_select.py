"""카테고리 LLM 택일 테스트 (이슈 #59).

top-k 후보를 LLM 에 주고 최종 1개를 고른다. 핵심 가드:
- 후보에 없는 값(환각)을 LLM 이 내도 그대로 쓰지 않는다(membership → null).
- 후보가 없거나 LLM 이 실패하면 non-blocking 으로 null(categoryName 생략).
"""

from __future__ import annotations

import json

from app.agents.buyer.recommendation.category_select import select_category
from app.core.llm import LLMError


class _FakeLLM:
    """지정 raw 문자열을 돌려주거나 error=True 면 LLMError 를 던지는 최소 LLM."""

    def __init__(self, *, raw: str = "", error: bool = False) -> None:
        self._raw = raw
        self._error = error
        self.called = False

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        self.called = True
        if self._error:
            raise LLMError("boom")
        return self._raw

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "x"


_CANDS = ["PC부품 > CPU", "PC부품 > 그래픽카드", "가전 > TV"]


async def test_picks_candidate_from_list() -> None:
    """LLM 이 후보 중 하나를 고르면 그 canonical 값을 돌려준다."""
    llm = _FakeLLM(raw=json.dumps({"category": "PC부품 > 그래픽카드"}))
    result = await select_category(llm, query="그래픽카드 추천", candidates=_CANDS, tier="fast")
    assert result == "PC부품 > 그래픽카드"


async def test_null_output_returns_none() -> None:
    """맞는 후보가 없다고 LLM 이 null 을 내면 None."""
    llm = _FakeLLM(raw=json.dumps({"category": None}))
    assert await select_category(llm, query="아무거나", candidates=_CANDS, tier="fast") is None


async def test_offlist_output_rejected() -> None:
    """LLM 이 후보에 없는 값(환각)을 내면 membership 가드로 None."""
    llm = _FakeLLM(raw=json.dumps({"category": "식품 > 과자"}))
    assert await select_category(llm, query="cpu", candidates=_CANDS, tier="fast") is None


async def test_empty_candidates_skip_llm() -> None:
    """후보가 없으면 LLM 을 호출하지 않고 None."""
    llm = _FakeLLM(raw=json.dumps({"category": "PC부품 > CPU"}))
    assert await select_category(llm, query="cpu", candidates=[], tier="fast") is None
    assert llm.called is False


async def test_llm_failure_degrades_to_none() -> None:
    """LLM 오류·JSON 파싱 실패는 non-blocking — 스트림을 끊지 않고 None(categoryName 생략)."""
    llm = _FakeLLM(error=True)
    assert await select_category(llm, query="cpu", candidates=_CANDS, tier="fast") is None
