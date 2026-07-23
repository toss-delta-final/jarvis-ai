"""판매자 모델 팩토리 테스트 (SPEC-SELLER-001 §8 — provider-neutral 2-tier)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.agents.seller import models as seller_models
from app.agents.seller.models import ROLE_TIER, init_seller_model
from app.core import llm as llm_mod
from app.core.config import Settings


@pytest.fixture(autouse=True)
def _fresh_model_cache() -> None:
    """테스트마다 모델 인스턴스 캐시를 비운다."""
    seller_models._cached_model.cache_clear()


def _record_factory(
    monkeypatch: pytest.MonkeyPatch, settings_factory: Callable[[], Settings]
) -> tuple[list[dict[str, Any]], list[object]]:
    """Settings와 init_chat_model을 대역으로 바꾸고 실제 생성 인자를 수집한다."""
    calls: list[dict[str, Any]] = []
    models: list[object] = []

    def fake_init_chat_model(*args: Any, **kwargs: Any) -> object:
        calls.append({"args": args, **kwargs})
        model = object()
        models.append(model)
        return model

    monkeypatch.setattr(seller_models, "get_settings", settings_factory)
    monkeypatch.setattr(seller_models, "init_chat_model", fake_init_chat_model)
    return calls, models


def test_role_tier_matches_provider_neutral_spec() -> None:
    assert ROLE_TIER == {
        "supervisor": "fast",
        "planner": "fast",
        "worker": "fast",
        "judge": "fast",
        "product": "fast",
        "report": "smart",
        "recommend": "smart",
    }


def test_openai_fast_roles_use_reasoning_without_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, openai_api_key="openai-key")
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    instances = [
        init_seller_model(role) for role in ("supervisor", "planner", "worker", "judge", "product")
    ]

    assert all(model is instances[0] for model in instances)
    assert calls == [
        {
            "args": (),
            "model": settings.openai_fast_model_id,
            "model_provider": "openai",
            "api_key": "openai-key",
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
            "reasoning_effort": settings.openai_fast_reasoning_effort,
        }
    ]


def test_openai_smart_roles_use_smart_reasoning_without_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, openai_api_key="openai-key")
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    assert init_seller_model("report") is init_seller_model("recommend")

    assert calls[0]["model"] == settings.openai_smart_model_id
    assert calls[0]["reasoning_effort"] == settings.openai_smart_reasoning_effort
    assert "temperature" not in calls[0]


def test_anthropic_tiers_keep_seller_temperatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key="anthropic-key",
        seller_haiku_temperature=0.1,
        seller_sonnet_temperature=0.3,
    )
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    init_seller_model("worker")
    init_seller_model("report")

    assert calls == [
        {
            "args": (),
            "model": settings.haiku_model_id,
            "model_provider": "anthropic",
            "api_key": "anthropic-key",
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
            "temperature": 0.1,
        },
        {
            "args": (),
            "model": settings.sonnet_model_id,
            "model_provider": "anthropic",
            "api_key": "anthropic-key",
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
            "temperature": 0.3,
        },
    ]
    assert all("reasoning_effort" not in call for call in calls)


def test_provider_switch_does_not_reuse_cached_model(monkeypatch: pytest.MonkeyPatch) -> None:
    active = [Settings(_env_file=None, openai_api_key="same-key")]
    calls, _ = _record_factory(monkeypatch, lambda: active[0])

    openai_model = init_seller_model("worker")
    active[0] = Settings(
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key="same-key",
    )
    anthropic_model = init_seller_model("worker")

    assert openai_model is not anthropic_model
    assert [call["model_provider"] for call in calls] == ["openai", "anthropic"]


def test_missing_provider_key_fails_before_sdk_call(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, openai_api_key="")
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    with pytest.raises(llm_mod.LLMNotConfigured):
        init_seller_model("worker")

    assert calls == []


def test_unknown_role_raises() -> None:
    with pytest.raises(KeyError):
        init_seller_model("chart")  # type: ignore[arg-type]
