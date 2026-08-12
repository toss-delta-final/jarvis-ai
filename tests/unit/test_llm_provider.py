"""LLM provider 토글 + tier 추상화 (이슈 #40).

get_llm() 이 settings.llm_provider 로 Anthropic/OpenAI 를 분기하고, 각 provider 가
tier(fast/smart) → 자기 모델 id 로 매핑하는지 검증한다. 네트워크·SDK 없이 도는
순수 단위 테스트 — ChatAnthropic/ChatOpenAI 는 _chat 에서 지연 import 되므로
resolve/분기 로직만 확인한다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core import llm as llm_mod
from app.core.config import Settings
from app.core.llm import (
    AnthropicLLM,
    LLMError,
    OpenAILLM,
    get_llm,
    is_output_length_error,
    is_timeout_error,
)
from app.core.tracing import FakeTraceExporter, TraceFactory, bind_request_trace, trace_span


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ─────────── is_timeout_error — 타입 기반 타임아웃 판정 (#266 P1) ───────────


def test_sdk_timeout_is_not_a_builtin_timeout_error() -> None:
    """#266 P1 의 **전제**를 코드로 고정한다.

    `except (TimeoutError, asyncio.TimeoutError)` 만으로 SDK 타임아웃이 잡힌다면
    is_timeout_error 는 존재할 이유가 없다. 라이브러리 업그레이드로 이 전제가 바뀌면
    이 테스트가 먼저 깨져서 알려준다.
    """
    import httpx

    assert not issubclass(httpx.TimeoutException, TimeoutError)


def test_is_timeout_error_detects_sdk_timeout_with_empty_message() -> None:
    """문자열 매칭이 못 잡는 케이스 — `str(exc)` 가 비어도 타입으로 잡아야 한다."""
    import httpx

    exc = httpx.ReadTimeout("")
    assert "timeout" not in str(exc).lower(), "문자열 매칭이 통했다면 이 테스트의 전제가 무너진다"
    assert is_timeout_error(exc) is True


def test_is_timeout_error_follows_cause_chain() -> None:
    """LLMError(str(exc)) from exc 로 감싸도 원인 체인에서 원본 타입을 찾아야 한다."""
    import httpx

    try:
        try:
            raise httpx.ConnectTimeout("")
        except httpx.TimeoutException as inner:
            raise LLMError("wrapped") from inner
    except LLMError as wrapped:
        assert is_timeout_error(wrapped) is True


def test_is_timeout_error_covers_builtin_and_asyncio_timeout() -> None:
    """asyncio.timeout 이 던지는 TimeoutError 도 같은 판정기로 덮인다(3.11+ 동일 객체)."""
    import asyncio

    assert asyncio.TimeoutError is TimeoutError
    assert is_timeout_error(TimeoutError()) is True


def test_is_timeout_error_rejects_non_timeout() -> None:
    """일반 실패를 타임아웃으로 오분류하지 않는다 — None 도 안전하게 False."""
    assert is_timeout_error(RuntimeError("boom")) is False
    assert is_timeout_error(LLMError("모델 응답 파싱 실패")) is False
    assert is_timeout_error(None) is False


def test_is_timeout_error_survives_self_referencing_chain() -> None:
    """원인 체인이 순환해도 무한 루프에 빠지지 않는다."""
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert is_timeout_error(first) is False


# ─────────── is_output_length_error — 출력 토큰 예산 소진 판정 (#325 R6) ───────────


def test_is_output_length_error_detects_direct_type() -> None:
    """openai.LengthFinishReasonError 를 직접 타입으로 잡는다(#325 원 사례)."""
    from types import SimpleNamespace

    openai = pytest.importorskip("openai")
    exc = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
    assert is_output_length_error(exc) is True


def test_is_output_length_error_follows_cause_chain() -> None:
    """OpenAILLM.complete 이 ``raise LLMError(str(exc)) from exc`` 로 감싸는 실제 경로처럼
    원인 체인에만 있어도 잡아야 한다."""
    from types import SimpleNamespace

    openai = pytest.importorskip("openai")
    inner = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
    try:
        try:
            raise inner
        except openai.LengthFinishReasonError:
            raise LLMError("wrapped") from inner
    except LLMError as wrapped:
        assert is_output_length_error(wrapped) is True


def test_is_output_length_error_rejects_unrelated() -> None:
    """무관한 예외·None 은 False — 타임아웃도 출력 예산 소진이 아니다."""
    assert is_output_length_error(RuntimeError("boom")) is False
    assert is_output_length_error(LLMError("모델 응답 파싱 실패")) is False
    assert is_output_length_error(TimeoutError()) is False
    assert is_output_length_error(None) is False


def test_is_output_length_error_survives_self_referencing_chain() -> None:
    """원인 체인이 순환해도 무한 루프에 빠지지 않는다(is_timeout_error 와 같은 안전장치)."""
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert is_output_length_error(first) is False


# ─────────── tier 매핑 ───────────


def test_anthropic_resolves_tiers() -> None:
    c = AnthropicLLM(
        "k",
        fast_model="claude-haiku-4-5",
        smart_model="claude-sonnet-5",
        timeout=1.0,
        max_retries=0,
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


# ─────────── 공용 provider/tier resolver ───────────


def test_resolve_provider_model_anthropic_tiers() -> None:
    settings = _settings(llm_provider="anthropic", anthropic_api_key="anthropic-key")

    fast = llm_mod.resolve_provider_model(settings, "fast")
    smart = llm_mod.resolve_provider_model(settings, "smart")

    assert (fast.provider, fast.model_id, fast.api_key, fast.reasoning_effort) == (
        "anthropic",
        settings.haiku_model_id,
        "anthropic-key",
        None,
    )
    assert (smart.provider, smart.model_id, smart.api_key, smart.reasoning_effort) == (
        "anthropic",
        settings.sonnet_model_id,
        "anthropic-key",
        None,
    )


def test_resolve_provider_model_openai_tiers() -> None:
    settings = _settings(llm_provider="openai", openai_api_key="openai-key")

    fast = llm_mod.resolve_provider_model(settings, "fast")
    smart = llm_mod.resolve_provider_model(settings, "smart")

    assert (fast.provider, fast.model_id, fast.api_key, fast.reasoning_effort) == (
        "openai",
        settings.openai_fast_model_id,
        "openai-key",
        settings.openai_fast_reasoning_effort,
    )
    assert (smart.provider, smart.model_id, smart.api_key, smart.reasoning_effort) == (
        "openai",
        settings.openai_smart_model_id,
        "openai-key",
        settings.openai_smart_reasoning_effort,
    )
    assert "openai-key" not in repr(fast)


@pytest.mark.parametrize(
    ("provider", "tier", "expected_field"),
    [
        ("openai", "fast", "openai_fast_model_id"),
        ("openai", "smart", "openai_smart_model_id"),
        ("anthropic", "fast", "haiku_model_id"),
        ("anthropic", "smart", "sonnet_model_id"),
    ],
)
def test_resolve_model_id_does_not_require_api_key(
    provider: str, tier: str, expected_field: str
) -> None:
    settings = _settings(llm_provider=provider, openai_api_key="", anthropic_api_key="")

    assert llm_mod.resolve_model_id(settings, tier) == getattr(settings, expected_field)


@pytest.mark.parametrize(
    ("provider", "key_field"),
    [("openai", "openai_api_key"), ("anthropic", "anthropic_api_key")],
)
def test_resolve_provider_model_missing_active_key_raises(provider: str, key_field: str) -> None:
    settings = _settings(llm_provider=provider, **{key_field: ""})

    with pytest.raises(llm_mod.LLMNotConfigured):
        llm_mod.resolve_provider_model(settings, "fast")


def test_resolve_provider_model_unknown_tier_raises() -> None:
    settings = _settings(openai_api_key="openai-key")

    with pytest.raises(LLMError):
        llm_mod.resolve_provider_model(settings, "turbo")


@pytest.mark.parametrize(
    ("raw_provider", "expected"),
    [("OpenAI", "openai"), ("OPENAI", "openai"), ("Anthropic", "anthropic")],
)
def test_settings_normalizes_provider_value_case(raw_provider: str, expected: str) -> None:
    settings = _settings(llm_provider=raw_provider)

    assert settings.llm_provider == expected


def test_settings_normalizes_provider_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "Anthropic")

    assert Settings(_env_file=None).llm_provider == "anthropic"


def test_settings_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError):
        _settings(llm_provider="other")


# ─────────── tool + reasoning_effort capability 게이팅 (이슈 #178) ───────────


def test_tool_lane_downgrades_effort_on_incompatible_model() -> None:
    """gpt-5.6-luna 는 tools + reasoning_effort 를 400 으로 거부 — override 로 강등한다."""
    settings = _settings(openai_api_key="k", openai_smart_model_id="gpt-5.6-luna")

    smart = llm_mod.resolve_provider_model(settings, "smart", with_tools=True)

    assert smart.reasoning_effort == settings.openai_tool_reasoning_effort_override
    assert smart.reasoning_effort == "none"


def test_non_tool_lane_keeps_effort_on_incompatible_model() -> None:
    """구매자 레인(OpenAILLM.complete/stream)은 tool 을 싣지 않아 강등 대상이 아니다."""
    settings = _settings(openai_api_key="k", openai_smart_model_id="gpt-5.6-luna")

    smart = llm_mod.resolve_provider_model(settings, "smart")

    assert smart.reasoning_effort == settings.openai_smart_reasoning_effort


def test_tool_lane_keeps_effort_on_compatible_model() -> None:
    """gpt-5-nano 는 tools + minimal 조합이 통과한다(aa0a2b6 이전 판매자 레인 실측)."""
    settings = _settings(openai_api_key="k")

    fast = llm_mod.resolve_provider_model(settings, "fast", with_tools=True)

    assert fast.model_id == "gpt-5-nano"
    assert fast.reasoning_effort == settings.openai_fast_reasoning_effort


def test_incompatible_match_is_prefix_based() -> None:
    """날짜 스냅샷 ID 도 같은 제약을 받는다 — 접두사 매칭."""
    settings = _settings(openai_api_key="k", openai_smart_model_id="gpt-5.6-luna-2026-07-01")

    smart = llm_mod.resolve_provider_model(settings, "smart", with_tools=True)

    assert smart.reasoning_effort == "none"


def test_empty_incompatible_entry_does_not_match_everything() -> None:
    """빈 문자열 항목이 전체 모델을 강등시키지 않는다."""
    settings = _settings(openai_api_key="k", openai_tool_reasoning_incompatible_models=[""])

    assert llm_mod.supports_tool_reasoning(settings, "gpt-5.6-luna")


def test_gating_disabled_by_empty_model_list() -> None:
    """조합을 지원하는 모델로 갈아타면 목록을 비우는 것으로 원복된다."""
    settings = _settings(openai_api_key="k", openai_tool_reasoning_incompatible_models=[])

    smart = llm_mod.resolve_provider_model(settings, "smart", with_tools=True)

    assert smart.reasoning_effort == settings.openai_smart_reasoning_effort


def test_anthropic_tool_lane_has_no_reasoning_effort() -> None:
    """anthropic 은 reasoning_effort 자체가 없어 with_tools 와 무관하다."""
    settings = _settings(llm_provider="anthropic", anthropic_api_key="k")

    smart = llm_mod.resolve_provider_model(settings, "smart", with_tools=True)

    assert smart.reasoning_effort is None


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


# ─────────── OpenAI json_output 토글 + 캐시 키 (리뷰 #44 회귀) ───────────


def _openai(**kw) -> OpenAILLM:
    base = dict(
        fast_model="gpt-x",
        smart_model="gpt-x",
        timeout=5.0,
        max_retries=0,
        fast_reasoning_effort="low",
        smart_reasoning_effort="medium",
    )
    base.update(kw)
    return OpenAILLM("sk-test", **base)


def _provider(provider: str):
    if provider == "anthropic":
        return AnthropicLLM(
            "key", fast_model="fast-model", smart_model="smart-model", timeout=1, max_retries=0
        )
    return OpenAILLM(
        "key", fast_model="fast-model", smart_model="smart-model", timeout=1, max_retries=0
    )


def _trace():
    exporter = FakeTraceExporter()
    trace = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0).start_request(
        name="buyer_chat_turn",
        request_id="req-provider",
        conversation_id="conversation",
        thread_id="thread",
        lane="buyer",
        environment="test",
    )
    return trace, exporter


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_provider_stream_records_first_nonempty_ttft_and_disables_implicit_tracing(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from langsmith.run_helpers import get_tracing_context

    contexts: list[bool | str | None] = []

    class Chat:
        async def astream(self, messages):
            del messages
            contexts.append(get_tracing_context()["enabled"])
            yield SimpleNamespace(content="")
            yield SimpleNamespace(
                content="첫 토큰",
                usage_metadata={"input_tokens": 11, "output_tokens": 3},
            )

    llm = _provider(provider)
    monkeypatch.setattr(llm, "_chat", lambda *args, **kwargs: Chat())
    times = iter([10.0, 10.025])
    monkeypatch.setattr(llm_mod, "perf_counter", lambda: next(times), raising=False)
    trace, exporter = _trace()

    with bind_request_trace(trace):
        with trace_span("llm.fallback", "llm", {"model": "fast-model"}):
            chunks = [
                chunk
                async for chunk in llm.stream(
                    system="private system", user="private user", tier="fast"
                )
            ]
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert chunks == ["첫 토큰"]
    root = next(node for node in exporter.exported[0] if node.parent_id is None)
    span = next(node for node in exporter.exported[0] if node.name == "llm.fallback")
    assert root.metadata["provider_ttft_ms"] == 25
    assert span.metadata == {
        "model": "fast-model",
        "promptTokens": 11,
        "completionTokens": 3,
    }
    assert contexts == [False]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_provider_complete_records_usage_without_provider_ttft(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from langsmith.run_helpers import get_tracing_context

    contexts: list[bool | str | None] = []

    class Chat:
        async def ainvoke(self, messages):
            del messages
            contexts.append(get_tracing_context()["enabled"])
            return SimpleNamespace(
                content='{"ok": true}',
                usage_metadata={
                    "input_tokens": 7,
                    "output_tokens": 2,
                    "input_token_details": {"cache_read": 3, "cache_creation": 1},
                },
            )

    llm = _provider(provider)
    monkeypatch.setattr(llm, "_chat", lambda *args, **kwargs: Chat())
    trace, exporter = _trace()

    with bind_request_trace(trace):
        with trace_span("llm.decompose", "llm", {"model": "fast-model"}):
            result = await llm.complete(system="private system", user="private user", tier="fast")
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert result == '{"ok": true}'
    root = next(node for node in exporter.exported[0] if node.parent_id is None)
    span = next(node for node in exporter.exported[0] if node.name == "llm.decompose")
    assert "provider_ttft_ms" not in root.metadata
    assert span.metadata == {
        "model": "fast-model",
        "promptTokens": 7,
        "completionTokens": 2,
        "cachedInputTokens": 3,
        "cacheWriteTokens": 1,
    }
    assert contexts == [False]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_request_provider_ttft_is_first_write_wins_across_streams_and_complete(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    calls = 0

    class Chat:
        async def astream(self, messages):
            del messages
            yield SimpleNamespace(content="")
            yield SimpleNamespace(content="")
            yield SimpleNamespace(content=f"stream-{calls}")

        async def ainvoke(self, messages):
            del messages
            return SimpleNamespace(
                content='{"ok": true}',
                usage_metadata={"input_tokens": 1, "output_tokens": 1},
            )

    llm = _provider(provider)

    def chat(*args, **kwargs):
        del args, kwargs
        nonlocal calls
        calls += 1
        return Chat()

    monkeypatch.setattr(llm, "_chat", chat)
    times = iter([10.0, 10.010, 20.0, 20.250])
    monkeypatch.setattr(llm_mod, "perf_counter", lambda: next(times))
    trace, exporter = _trace()

    with bind_request_trace(trace):
        with trace_span("llm.fallback", "llm", {"model": "fast-model"}):
            first = [
                chunk
                async for chunk in llm.stream(
                    system="private system", user="private user", tier="fast"
                )
            ]
        with trace_span("llm.fallback", "llm", {"model": "fast-model"}):
            second = [
                chunk
                async for chunk in llm.stream(
                    system="private system", user="private user", tier="fast"
                )
            ]
        with trace_span("llm.decompose", "llm", {"model": "fast-model"}):
            completed = await llm.complete(
                system="private system", user="private user", tier="fast"
            )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert first == ["stream-1"]
    assert second == ["stream-2"]
    assert completed == '{"ok": true}'
    root = next(node for node in exporter.exported[0] if node.parent_id is None)
    assert root.metadata["provider_ttft_ms"] == 10


def test_openai_json_output_toggles_response_format() -> None:
    """complete(json_output=False) 는 response_format 을 붙이지 않는다 — 마크다운 태스크(consolidate)용."""
    llm = _openai()
    c_json = llm._chat("fast", 100, json_mode=True)
    c_plain = llm._chat("fast", 100, json_mode=False)
    assert c_json.model_kwargs.get("response_format") == {"type": "json_object"}
    assert "response_format" not in c_plain.model_kwargs
    assert c_json is not c_plain  # json 여부가 캐시 키에 반영


def test_openai_cache_key_includes_tier_effort() -> None:
    """fast/smart 가 같은 모델이어도 reasoning_effort 가 tier 별로 유지된다(캐시 키에 tier 포함)."""
    llm = _openai()  # fast/smart 모두 gpt-x, effort 만 low/medium
    c_fast = llm._chat("fast", 100, json_mode=True)
    c_smart = llm._chat("smart", 100, json_mode=True)
    assert c_fast is not c_smart
    assert c_fast.reasoning_effort == "low"
    assert c_smart.reasoning_effort == "medium"


# ─────────── #325: per-call reasoning_effort override + 캐시 오염 회귀 ───────────


async def test_openai_complete_reasoning_effort_override_reaches_chat_kwargs() -> None:
    """reasoning_effort override 가 tier 기본 대신 실제 ChatOpenAI kwargs 에 실린다."""
    llm = _openai(fast_reasoning_effort="minimal")
    chat = llm._chat("fast", 100, json_mode=True, effort_override="low")
    assert chat.reasoning_effort == "low"


async def test_openai_cache_key_distinguishes_effort_override_from_tier_default() -> None:
    """같은 (tier, max_tokens, json) 이라도 override 유/무는 다른 클라이언트로 캐시된다(캐시 오염 회귀).

    이 테스트가 없으면 enrichment(override="minimal")가 다른 fast 호출(override=None,
    tier 기본도 "minimal")과 같은 캐시 키를 공유해도 우연히 통과해 보이므로, 서로 다른
    tier 기본/override 값 조합으로 실제 분리를 확인한다.
    """
    llm = _openai(fast_reasoning_effort="minimal")
    without_override = llm._chat("fast", 100, json_mode=True)
    with_override = llm._chat("fast", 100, json_mode=True, effort_override="low")
    assert without_override is not with_override
    assert without_override.reasoning_effort == "minimal"
    assert with_override.reasoning_effort == "low"
    # 재호출 시 각자의 캐시 항목이 재사용된다(오염되지 않음).
    assert llm._chat("fast", 100, json_mode=True) is without_override
    assert llm._chat("fast", 100, json_mode=True, effort_override="low") is with_override


async def test_openai_complete_passes_reasoning_effort_through(monkeypatch) -> None:
    llm = _openai()
    captured = {}

    class Chat:
        reasoning_effort = None

        async def ainvoke(self, messages):
            return SimpleNamespace(content="ok", usage_metadata=None)

    def fake_chat(tier, max_tokens, *, json_mode, effort_override=None):
        captured["args"] = (tier, max_tokens, json_mode, effort_override)
        return Chat()

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = await llm.complete(
        system="s", user="u", tier="fast", max_tokens=2048, reasoning_effort="minimal"
    )
    assert result == "ok"
    assert captured["args"] == ("fast", 2048, True, "minimal")


async def test_anthropic_complete_ignores_reasoning_effort(monkeypatch) -> None:
    """Anthropic 은 effort 개념이 없어 kwarg 를 받아도 예외 없이 동작한다(#325)."""
    llm = _provider("anthropic")

    class Chat:
        async def ainvoke(self, messages):
            return SimpleNamespace(content="ok", usage_metadata=None)

    monkeypatch.setattr(llm, "_chat", lambda *a, **kw: Chat())
    result = await llm.complete(system="s", user="u", tier="fast", reasoning_effort="minimal")
    assert result == "ok"
