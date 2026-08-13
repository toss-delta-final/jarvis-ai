"""모델 단가표 기본값 (이슈 #437) — app/core/model_pricing.py + config 배선 검증."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core.auth import Identity
from app.core.config import Settings, get_settings
from app.core.conversation import TurnStatus
from app.core.llm import resolve_model_id
from app.core.model_pricing import (
    DEFAULT_MODEL_PRICE_IN_PER_1K,
    DEFAULT_MODEL_PRICE_OUT_PER_1K,
    DEFAULT_MODEL_PRICES,
    MODEL_PRICE_TABLE_AS_OF,
    log_model_price_table_status,
)
from app.core.observability import start_observation
from evals.model_eval.pricing import PriceBook


def test_default_prices_match_pricing_manifest_exactly() -> None:
    """DEFAULT_MODEL_PRICES 가 evals/model_eval/pricing_manifest.json 과 글자 그대로 일치한다.

    비용축과 동일 소스를 쓰는 규약(EVAL-OBS-PLAN-001 §3.4)이 코드/eval 양쪽에서 드리프트하지
    않게 고정한다.
    """
    manifest = PriceBook.load()
    assert {entry.model for entry in DEFAULT_MODEL_PRICES} == set(manifest.entries)
    for entry in DEFAULT_MODEL_PRICES:
        manifest_entry = manifest.entries[entry.model]
        assert entry.in_per_1k == manifest_entry["inPer1k"]
        assert entry.cached_in_per_1k == manifest_entry["cachedInPer1k"]
        assert entry.cache_write_per_1k == manifest_entry["cacheWritePer1k"]
        assert entry.out_per_1k == manifest_entry["outPer1k"]
        assert entry.effective_date == manifest_entry["effectiveDate"]
        assert entry.source == manifest_entry["source"]


def test_settings_defaults_expose_all_manifest_models() -> None:
    """Settings() 기본값이 모든 모델의 입력·출력 단가를 노출하고 값이 manifest 와 같다."""
    settings = Settings(_env_file=None)
    manifest = PriceBook.load()
    for entry in manifest.entries.values():
        model = entry["model"]
        assert settings.model_price_in_per_1k[model] == entry["inPer1k"]
        assert settings.model_price_cached_in_per_1k[model] == entry["cachedInPer1k"]
        assert settings.model_price_cache_write_per_1k[model] == entry["cacheWritePer1k"]
        assert settings.model_price_out_per_1k[model] == entry["outPer1k"]


def test_gpt_5_6_luna_uses_official_cache_aware_prices() -> None:
    """2026-08-13 공식 모델 문서 단가와 캐시 쓰기 1.25배 규칙을 고정한다."""
    entry = PriceBook.load().entries["gpt-5.6-luna"]

    assert entry["inPer1k"] == 0.001
    assert entry["cachedInPer1k"] == 0.0001
    assert entry["cacheWritePer1k"] == 0.00125
    assert entry["outPer1k"] == 0.006


def test_gpt_5_6_sol_uses_official_cache_aware_prices() -> None:
    """Codex blind judge와 API judge가 같은 공식 Sol 단가를 사용한다."""
    entry = PriceBook.load().entries["gpt-5.6-sol"]

    assert entry["inPer1k"] == 0.005
    assert entry["cachedInPer1k"] == 0.0005
    assert entry["cacheWritePer1k"] == 0.00625
    assert entry["outPer1k"] == 0.03


def test_price_book_cost_separates_cached_reads_and_writes() -> None:
    """캐시 토큰은 전체 입력의 부분집합이며 일반 입력으로 중복 과금하지 않는다."""
    cost = PriceBook.load().cost(
        model="gpt-5.6-luna",
        input_tokens=1_000,
        output_tokens=500,
        cached_input_tokens=400,
        cache_write_tokens=100,
    )

    assert cost == pytest.approx(0.003665)


def test_default_price_tables_are_isolated_between_instances() -> None:
    """한 Settings() 인스턴스의 표를 변형해도 다음 인스턴스가 오염되지 않는다(공유 가변 기본값 방지)."""
    first = Settings(_env_file=None)
    first.model_price_in_per_1k["poison"] = 999.0
    first.model_price_out_per_1k["poison"] = 999.0

    second = Settings(_env_file=None)

    assert "poison" not in second.model_price_in_per_1k
    assert "poison" not in second.model_price_out_per_1k
    assert second.model_price_in_per_1k == DEFAULT_MODEL_PRICE_IN_PER_1K
    assert second.model_price_out_per_1k == DEFAULT_MODEL_PRICE_OUT_PER_1K


def test_env_injection_replaces_default_price_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODEL_PRICE_IN_PER_1K 환경변수 주입이 기본표 전체를 치환한다(병합이 아니다)."""
    monkeypatch.setenv("MODEL_PRICE_IN_PER_1K", '{"m":0.1}')
    get_settings.cache_clear()
    try:
        settings = Settings()
        assert settings.model_price_in_per_1k == {"m": 0.1}
        # 병합이 아니라 치환이므로, 주입하지 않은 out 표는 여전히 코드 기본값이다.
        assert settings.model_price_out_per_1k == DEFAULT_MODEL_PRICE_OUT_PER_1K
    finally:
        get_settings.cache_clear()


def test_deploy_workflow_wires_all_cache_aware_price_tables() -> None:
    """운영·dev 배포 모두 네 단가표를 env 파일에 전달하고 빈 변수는 기본값으로 되돌린다."""
    workflow = (Path(__file__).parents[2] / ".github/workflows/deploy.yml").read_text(
        encoding="utf-8"
    )
    for name in (
        "MODEL_PRICE_IN_PER_1K",
        "MODEL_PRICE_CACHED_IN_PER_1K",
        "MODEL_PRICE_CACHE_WRITE_PER_1K",
        "MODEL_PRICE_OUT_PER_1K",
    ):
        assert workflow.count(f"{name}=${{{{ vars.{name} }}}}") == 2
        assert workflow.count(name) >= 4


@pytest.mark.parametrize("blank_value", ["", "   "])
def test_blank_env_value_falls_back_to_default_price_table(
    monkeypatch: pytest.MonkeyPatch, blank_value: str
) -> None:
    """빈 문자열(공백 포함)은 예외 없이 기본표로 해석된다 — deploy.yml 은 미설정 vars 를
    빈 문자열로 env 파일에 쓰므로, 여기가 깨지면 운영 부팅이 죽는다."""
    monkeypatch.setenv("MODEL_PRICE_IN_PER_1K", blank_value)
    monkeypatch.setenv("MODEL_PRICE_OUT_PER_1K", blank_value)
    get_settings.cache_clear()
    try:
        settings = Settings()
        assert settings.model_price_in_per_1k == DEFAULT_MODEL_PRICE_IN_PER_1K
        assert settings.model_price_out_per_1k == DEFAULT_MODEL_PRICE_OUT_PER_1K
    finally:
        get_settings.cache_clear()


def test_missing_active_model_price_warns_with_model_ids(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """활성 모델(LLM_PROVIDER=anthropic 기본 모델) 단가 누락 시 그 모델 ID 가 경고에 실린다."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    try:
        settings = Settings()
        with caplog.at_level(logging.WARNING, logger="app.core.model_pricing"):
            log_model_price_table_status(settings)
        assert "MODEL_PRICE_MISSING_AT_STARTUP" in caplog.text
        assert settings.haiku_model_id in caplog.text
        assert settings.sonnet_model_id in caplog.text
    finally:
        get_settings.cache_clear()


def test_default_price_table_in_use_warns(caplog: pytest.LogCaptureFixture) -> None:
    """주입 없는 기본 상태에서는 MODEL_PRICE_DEFAULTS_IN_USE 경고가 뜬다."""
    settings = Settings(_env_file=None)

    with caplog.at_level(logging.WARNING, logger="app.core.model_pricing"):
        log_model_price_table_status(settings)

    assert "MODEL_PRICE_DEFAULTS_IN_USE" in caplog.text
    assert MODEL_PRICE_TABLE_AS_OF in caplog.text


def test_fully_injected_active_price_table_logs_ready_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """완전 주입 상태(활성 모델 모두 등록 + 기본값과 다른 값)에서는 두 경고가 모두 없고
    MODEL_PRICE_TABLE_READY 만 남는다."""
    monkeypatch.setenv(
        "MODEL_PRICE_IN_PER_1K",
        '{"gpt-5-nano": 0.00006, "gpt-5.6-luna": 0.0002}',
    )
    monkeypatch.setenv(
        "MODEL_PRICE_OUT_PER_1K",
        '{"gpt-5-nano": 0.0004, "gpt-5.6-luna": 0.0012}',
    )
    get_settings.cache_clear()
    try:
        settings = Settings()
        with caplog.at_level(logging.INFO, logger="app.core.model_pricing"):
            log_model_price_table_status(settings)
        assert "MODEL_PRICE_MISSING_AT_STARTUP" not in caplog.text
        assert "MODEL_PRICE_DEFAULTS_IN_USE" not in caplog.text
        assert "MODEL_PRICE_TABLE_READY" in caplog.text
    finally:
        get_settings.cache_clear()


async def test_shipped_defaults_actually_price_active_models(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """출하 기본값이 실제로 비용을 낸다(공허하지 않은 검증) — 이 이슈의 본질.

    가짜 settings 를 monkeypatch 하지 않고 실제 Settings() 기본값으로, resolve_model_id 가
    돌려주는 fast/smart 모델 ID 에 대해 RequestObservation 이 내는 costUsd 가 0 보다 크고
    MODEL_PRICE_MISSING 경고가 뜨지 않는지 확인한다.
    """
    settings = Settings(_env_file=None)
    fast_model = resolve_model_id(settings, "fast")
    smart_model = resolve_model_id(settings, "smart")

    identity = Identity(user_id=None, is_guest=True, seller_id=None, subject="guest-437")
    observation = start_observation(
        request_id="req-437",
        identity=identity,
        conversation_id="conv-437",
        message="가격표 실측",
        store=object(),  # turn_id 가 None 으로 남아 store I/O 는 전혀 발생하지 않는다.
        now=0.0,
    )
    # 이슈 실측 규모(prompt 363 / completion 27).
    observation.record_model_call(fast_model, prompt_tokens=363, completion_tokens=27)
    observation.record_model_call(smart_model, prompt_tokens=363, completion_tokens=27)

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    assert "MODEL_PRICE_MISSING" not in caplog.text

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["costUsd"] > 0.0
