"""판매자 모델 팩토리 테스트 (SPEC-SELLER-001 §8 — provider-neutral 2-tier)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.agents.seller import models as seller_models
from app.agents.seller.models import _CONTENT_TRACE_CALLBACK, ROLE_TIER, init_seller_model
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
    # 2026-07-29 품질 우선 전환 — 판매자 전 역할 smart(fast 역할 없음).
    # analysis_judge(이슈 #242, DESIGN-ANALYSIS-V31-242 결정 D-1)도 이 정책을 따른다 —
    # 이슈 원안(fast)을 채택하지 않고 판정 품질을 우선했다.
    # graph(5단계, 같은 이슈)도 smart — 이슈 원안 그대로 전 역할 정책과 일치.
    # [#506] vision(이미지 분석)·draft_gate(초안 대기 발화 분류)도 전 역할 smart 정책 —
    # vision 은 초안 전체의 원천 품질, draft_gate 는 오분류가 곧 UX 사고라 강등하지 않는다.
    # [#598] 상주(무인) 분석 파이프라인 역할 3종도 전 역할 smart 정책을 그대로 따른다 —
    # resident_report/resident_recommend 는 채팅 레인 report/recommend 와 무접촉으로
    # 분리한 역할이고, interpret 은 워커 4종 공통 zero-tool interpret 스텝이다. 무인
    # 실행이라 품질 저하를 사람이 즉시 교정할 기회가 없어 강등하지 않는다.
    # [#600] chart_interpret — 차트 레인 해석 스텝도 동일 정책(품질 우선, 강등 없음).
    assert ROLE_TIER == {
        "supervisor": "smart",
        "planner": "smart",
        "worker": "smart",
        "judge": "smart",
        "product": "smart",
        "report": "smart",
        "recommend": "smart",
        "analysis_judge": "smart",
        "graph": "smart",
        "vision": "smart",
        "draft_gate": "smart",
        # [#506 후속] category — 카테고리 오배정은 등록 후 되돌릴 수 없다(BE I-11 에
        # category 필드가 없다). 폴백 1회 호출이라 비용도 작아 강등 이유가 없다.
        "category": "smart",
        "resident_report": "smart",
        "resident_recommend": "smart",
        "interpret": "smart",
        "chart_interpret": "smart",
    }


def test_openai_all_seller_roles_share_smart_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, openai_api_key="openai-key")
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    instances = [init_seller_model(role) for role in ROLE_TIER]

    # 전 역할이 같은 tier → 같은 실효 설정 → lru_cache 로 인스턴스 1개만 생성된다.
    assert all(model is instances[0] for model in instances)
    assert calls == [
        {
            "args": (),
            "model": settings.openai_smart_model_id,
            "model_provider": "openai",
            "api_key": "openai-key",
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
            # [#326] 콘텐츠 추적 콜백 — 무상태 싱글턴이라 캐시 동일성 판정을 깨지 않는다.
            "callbacks": [_CONTENT_TRACE_CALLBACK],
            # 판매자 레인은 with_tools=True — luna 는 tools 와 effort 를 함께 못 받는다(#178).
            "reasoning_effort": settings.openai_tool_reasoning_effort_override,
        }
    ]


def test_openai_seller_roles_downgrade_reasoning_without_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, openai_api_key="openai-key")
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    assert init_seller_model("report") is init_seller_model("recommend")

    assert calls[0]["model"] == settings.openai_smart_model_id
    assert calls[0]["reasoning_effort"] == settings.openai_tool_reasoning_effort_override
    assert "temperature" not in calls[0]


def test_openai_seller_roles_keep_effort_on_compatible_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """조합 지원 모델로 갈아타면 강등 없이 설정한 effort 가 그대로 실린다(#178)."""
    settings = Settings(
        _env_file=None,
        openai_api_key="openai-key",
        openai_smart_model_id="gpt-5-nano",
    )
    calls, _ = _record_factory(monkeypatch, lambda: settings)

    init_seller_model("supervisor")

    assert calls[0]["reasoning_effort"] == settings.openai_smart_reasoning_effort


def test_anthropic_seller_roles_keep_sonnet_temperature(
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

    # 전 역할 smart → sonnet + seller_sonnet_temperature 만 쓰인다.
    # seller_haiku_temperature 는 판매자 경로에서 더 이상 참조되지 않는다.
    assert calls == [
        {
            "args": (),
            "model": settings.sonnet_model_id,
            "model_provider": "anthropic",
            "api_key": "anthropic-key",
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
            # [#326] 콘텐츠 추적 콜백 — 무상태 싱글턴이라 캐시 동일성 판정을 깨지 않는다.
            "callbacks": [_CONTENT_TRACE_CALLBACK],
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
