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
from types import SimpleNamespace

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


# ── 관측 기록 (api-spec §6.3 — PR #188 리뷰) ─────────────────────────────────


class _ProbeObserver:
    request_id = "req-probe"

    def __init__(self) -> None:
        self.models: list[str] = []

    def record_model_call(
        self, model: str, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        self.models.append(model)


def _settings() -> SimpleNamespace:
    """resolve_model_id 가 읽는 필드만 — tier 를 모델 ID 로 바꾸는 데 필요하다."""
    return SimpleNamespace(
        llm_provider="openai",
        openai_fast_model_id="gpt-5-nano",
        openai_smart_model_id="gpt-5.6-luna",
    )


async def test_select_records_model_call() -> None:
    """[PR #188 리뷰] 택일 LLM 호출을 `chat_request` 모델 호출 집계에 남긴다.

    decompose·전개·rerank 는 모두 기록하는데 택일만 빠져 있었다 — 애매한 leg 이 많은 턴일수록
    실제 호출 수가 관측치보다 많아져 비용·사용량 집계(§6.3)와 요청 단위 트레이싱(#141)이
    조용히 어긋난다.
    """
    observer = _ProbeObserver()
    llm = _FakeLLM(raw=json.dumps({"category": "가전 > 이어폰/헤드폰"}))
    out = await select_category(
        llm,
        query="무선 이어폰",
        candidates=["가전 > 이어폰/헤드폰", "자동차기기 > 카오디오음향기기"],
        tier="fast",
        settings=_settings(),
        observer=observer,
    )
    assert out == "가전 > 이어폰/헤드폰"
    assert observer.models == ["gpt-5-nano"]


async def test_select_records_even_when_llm_fails() -> None:
    """실패한 호출도 비용이 발생하므로 **호출 전** 기록한다 — decompose·전개와 같은 규약."""
    observer = _ProbeObserver()
    with pytest.raises(LLMError):
        await select_category(
            _FakeLLM(error=True),
            query="q",
            candidates=["A", "B"],
            tier="fast",
            settings=_settings(),
            observer=observer,
        )
    assert observer.models == ["gpt-5-nano"]


async def test_select_records_nothing_without_candidates() -> None:
    """후보 0건이면 LLM 을 부르지 않으므로 기록도 없다 — 없는 비용을 만들지 않는다."""
    observer = _ProbeObserver()
    assert (
        await select_category(
            _FakeLLM(),
            query="q",
            candidates=[],
            tier="fast",
            settings=_settings(),
            observer=observer,
        )
        is None
    )
    assert observer.models == []
