"""vision 이미지 분석 (#506) — 멀티모달 블록 구성·degrade 폴백 계약. 실 LLM 없음."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.seller import vision
from app.agents.seller.vision import ProductImageAnalysis, analyze_product_images
from app.core.llm import LLMNotConfigured

_ANALYSIS = ProductImageAnalysis(
    name="코튼 오버핏 셔츠",
    summary="면 100% 오버핏 셔츠",
    description="부드러운 면 소재.",
    category_hint="셔츠",
    confidence=0.9,
)


class _FakeStructuredModel:
    def __init__(self, result=None, error: Exception | None = None, delay: float = 0.0):
        self._result = result
        self._error = error
        self._delay = delay
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeModel:
    def __init__(self, structured: _FakeStructuredModel):
        self._structured = structured

    def with_structured_output(self, schema):
        assert schema is ProductImageAnalysis
        return self._structured


def _patch_model(monkeypatch, structured: _FakeStructuredModel) -> None:
    monkeypatch.setattr(vision, "init_seller_model", lambda role: _FakeModel(structured))


def test_analyze_builds_multimodal_blocks(monkeypatch) -> None:
    """이미지 URL 은 image_url 블록으로, 발화는 text 블록으로 실린다."""
    structured = _FakeStructuredModel(result=_ANALYSIS)
    _patch_model(monkeypatch, structured)
    result = asyncio.run(
        analyze_product_images(["https://cdn.example.com/a.jpg"], seller_message="이 상품 등록해줘")
    )
    assert result == _ANALYSIS
    human = structured.messages[-1]
    kinds = [block["type"] for block in human.content]
    assert kinds == ["text", "image_url"]
    assert human.content[1]["image_url"]["url"] == "https://cdn.example.com/a.jpg"
    assert "이 상품 등록해줘" in human.content[0]["text"]


def test_analyze_failure_degrades_to_none(monkeypatch) -> None:
    """LLM 예외는 None degrade — 발화만으로 create 초안은 계속 성립 가능해야 한다."""
    _patch_model(monkeypatch, _FakeStructuredModel(error=RuntimeError("boom")))
    assert asyncio.run(analyze_product_images(["https://x/a.jpg"])) is None


def test_analyze_timeout_degrades_to_none(monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeStructuredModel(result=_ANALYSIS, delay=0.2))
    monkeypatch.setattr(
        vision,
        "get_settings",
        lambda: type("S", (), {"seller_vision_timeout_s": 0.01})(),
    )
    assert asyncio.run(analyze_product_images(["https://x/a.jpg"])) is None


def test_analyze_not_configured_propagates(monkeypatch) -> None:
    """미구성은 degrade 가 아니다 — 상위가 LLM_UNAVAILABLE 계약으로 응답해야 한다."""

    def _raise(role):
        raise LLMNotConfigured("no provider")

    monkeypatch.setattr(vision, "init_seller_model", _raise)
    with pytest.raises(LLMNotConfigured):
        asyncio.run(analyze_product_images(["https://x/a.jpg"]))


def test_analyze_non_schema_output_degrades(monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeStructuredModel(result={"name": "raw dict"}))
    assert asyncio.run(analyze_product_images(["https://x/a.jpg"])) is None
