"""LLM provider 토글 + tier 추상화 (이슈 #40).

get_llm() 이 settings.llm_provider 로 Anthropic/OpenAI 를 분기하고, 각 provider 가
tier(fast/smart) → 자기 모델 id 로 매핑하는지 검증한다. 네트워크·SDK 없이 도는
순수 단위 테스트 — ChatAnthropic/ChatOpenAI 는 _chat 에서 지연 import 되므로
resolve/분기 로직만 확인한다.
"""

from __future__ import annotations

import pytest

from app.core import llm as llm_mod
from app.core.config import Settings
from app.core.llm import AnthropicLLM, LLMError, OpenAILLM, get_llm


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ─────────── tier 매핑 ───────────


def test_anthropic_resolves_tiers() -> None:
    c = AnthropicLLM(
        "k", fast_model="claude-haiku-4-5", smart_model="claude-sonnet-5", timeout=1.0, max_retries=0
    )
    assert c._resolve("fast") == "claude-haiku-4-5"
    assert c._resolve("smart") == "claude-sonnet-5"


def test_anthropic_unknown_tier_raises() -> None:
    c = AnthropicLLM("k", fast_model="h", smart_model="s", timeout=1.0, max_retries=0)
    with pytest.raises(LLMError):
        c._resolve("turbo")


def test_openai_resolves_tiers() -> None:
    c = OpenAILLM(
        "k",
        fast_model="gpt-5-nano",
        smart_model="gpt-5.6-luna",
        timeout=1.0,
        max_retries=0,
        fast_reasoning_effort="low",
        smart_reasoning_effort="medium",
    )
    assert c._resolve("fast") == ("gpt-5-nano", "low")
    assert c._resolve("smart") == ("gpt-5.6-luna", "medium")


def test_openai_unknown_tier_raises() -> None:
    c = OpenAILLM("k", fast_model="a", smart_model="b", timeout=1.0, max_retries=0)
    with pytest.raises(LLMError):
        c._resolve("turbo")


# ─────────── settings.model_for_tier (관측용 telemetry) ───────────


def test_settings_model_for_tier_anthropic() -> None:
    s = _settings(llm_provider="anthropic")
    assert s.model_for_tier("fast") == s.haiku_model_id
    assert s.model_for_tier("smart") == s.sonnet_model_id


def test_settings_model_for_tier_openai() -> None:
    s = _settings(llm_provider="openai")
    assert s.model_for_tier("fast") == s.openai_fast_model_id
    assert s.model_for_tier("smart") == s.openai_smart_model_id


def test_settings_model_for_tier_unknown_raises() -> None:
    with pytest.raises(ValueError):
        _settings().model_for_tier("turbo")


# ─────────── get_llm 분기 ───────────


def test_get_llm_anthropic(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_mod, "get_settings", lambda: _settings(llm_provider="anthropic", anthropic_api_key="k")
    )
    assert isinstance(get_llm(), AnthropicLLM)


def test_get_llm_anthropic_no_key(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_mod, "get_settings", lambda: _settings(llm_provider="anthropic", anthropic_api_key="")
    )
    assert get_llm() is None


def test_get_llm_openai(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_mod, "get_settings", lambda: _settings(llm_provider="openai", openai_api_key="k")
    )
    assert isinstance(get_llm(), OpenAILLM)


def test_get_llm_openai_no_key(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_mod, "get_settings", lambda: _settings(llm_provider="openai", openai_api_key="")
    )
    assert get_llm() is None


def test_get_llm_defaults_to_openai(monkeypatch) -> None:
    # llm_provider 미지정 → config.py 기본값(openai)
    monkeypatch.setattr(llm_mod, "get_settings", lambda: _settings(openai_api_key="k"))
    assert isinstance(get_llm(), OpenAILLM)
