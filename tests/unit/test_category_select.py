"""카테고리 LLM 택일 테스트 (이슈 #59).

top-k 후보를 LLM 에 주고 최종 1개를 고른다. 핵심 가드:
- 후보에 없는 값(환각)을 LLM 이 내도 그대로 쓰지 않는다(membership → null).
- null 은 **"맞는 후보 없음"이라는 판정 결과**다(후보 0건·LLM 이 null·환각 포함) — 호출부(§4.4)는
  그 leg 를 드롭한다.
- **LLM 실패(LLMError)는 null 로 뭉개지 않고 전파**한다(#115) — "판정 실패"는 후속 조치가 반대
  (임베딩 top-1 유지)라서 구분해야 한다. non-blocking 보장은 호출부가 담당한다.
"""

from __future__ import annotations

import json

import pytest

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


async def test_llm_failure_propagates_so_caller_can_distinguish() -> None:
    """[#115 §4.4] LLM 오류는 삼키지 않고 전파한다 — 호출부가 None 과 구분해야 하기 때문이다.

    None 은 "후보 중 맞는 것이 없음" = 그 leg 를 드롭하라는 **판정 결과**이고, 예외는 "판정을 못
    했음"이다. 둘의 후속 조치가 반대다(드롭 vs 임베딩 top-1 유지). 종전처럼 LLMError 를 None 으로
    뭉개면 LLM 타임아웃이 "맞는 카테고리 없음"으로 오해돼 **인프라 실패가 카테고리를 삭제**한다 —
    거리컷 도입 이전보다 후퇴다. non-blocking 보장은 호출부(map_categories)가 gather
    return_exceptions + top-1 유지로 담당한다.
    """
    llm = _FakeLLM(error=True)
    with pytest.raises(LLMError):
        await select_category(llm, query="cpu", candidates=_CANDS, tier="fast")
