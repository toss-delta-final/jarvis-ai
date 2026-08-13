"""`LLM_PROVIDER=scripted` 부하 테스트 스텁 (이슈 #438) — G1/G2/D5/D3 회귀 고정.

이 모듈은 §5 T1~T7을 순서대로 구현한다:
  T1 G1 — 비로컬 환경에서 기동 거부(staging도 포함)
  T2 G2 — 기동 배너가 scripted에서만 WARNING 이상으로 남는다
  T3 D5 — API 키 없이 get_llm()이 LoadTestLLM을 돌려준다(None 아님)
  T4 D3 rerank — 임의의 실 카탈로그 productId에서도 정상 경로를 유지한다(가장 중요)
  T5 D3 분류 커버리지 — 각 호출 지점 실 `_SYSTEM`을 import해 마커 고정 + 파서 통과 + 미상은 LLMError
  T6 결정론 — 같은 입력이면 바이트 단위로 같은 출력
  T7 변이 시험 — scripted 모드에서 실 vendor 클라이언트가 만들어지지 않는다
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import llm as llm_mod
from app.core.config import Settings
from app.core.llm import LLMError, get_llm
from app.core.llm_scripted import (
    _CATEGORY_SCOPE_MARK,
    _CATEGORY_SELECT_MARK,
    _DECOMPOSE_MARK,
    _NEED_PRIORITY_MARK,
    _NEEDS_EXPANSION_MARK,
    _RERANK_MARK,
    _RERANK_SCORING_MARK,
    LoadTestLLM,
)
from app.schemas.spring import SpringProduct


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ─────────── T1 (G1) — 비로컬 환경 기동 거부 ───────────


@pytest.mark.parametrize("env", ["production", "staging"])
def test_scripted_rejected_outside_local_or_test(env: str) -> None:
    with pytest.raises(ValidationError):
        _settings(llm_provider="scripted", app_environment=env)


@pytest.mark.parametrize("env", ["local", "test"])
def test_scripted_allowed_in_local_and_test(env: str) -> None:
    settings = _settings(llm_provider="scripted", app_environment=env)
    assert settings.llm_provider == "scripted"


def test_scripted_delay_settings_default_to_instant_five_seconds() -> None:
    settings = _settings()
    assert settings.scripted_llm_mode == "instant"
    assert settings.scripted_llm_delay_s == 5.0


def test_scripted_delay_settings_accept_delayed_mode() -> None:
    settings = _settings(scripted_llm_mode="delayed", scripted_llm_delay_s=5.0)
    assert settings.scripted_llm_mode == "delayed"
    assert settings.scripted_llm_delay_s == 5.0


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"scripted_llm_mode": "random"}, "scripted_llm_mode"),
        ({"scripted_llm_delay_s": -0.1}, "scripted_llm_delay_s"),
    ],
)
def test_scripted_delay_settings_reject_invalid_values(
    kwargs: dict[str, object], field_name: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(**kwargs)
    assert field_name in str(exc_info.value)


@pytest.mark.parametrize("field_name", ["rate_limit_per_min", "rate_limit_per_hour"])
@pytest.mark.parametrize("value", [0, -1])
def test_loadtest_rate_limits_must_be_positive(field_name: str, value: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(**{field_name: value})
    assert field_name in str(exc_info.value)


def test_deploy_workflow_wires_scripted_profiles_and_loadtest_rate_limits() -> None:
    workflow = (Path(__file__).parents[2] / ".github/workflows/deploy.yml").read_text(
        encoding="utf-8"
    )
    for name in (
        "SCRIPTED_LLM_MODE",
        "SCRIPTED_LLM_DELAY_S",
        "RATE_LIMIT_PER_MIN",
        "RATE_LIMIT_PER_HOUR",
    ):
        assert f"{name}=${{{{ vars.{name} }}}}" in workflow
        assert name in workflow.split("sed -i -E", maxsplit=1)[1]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("env", ["local", "test", "staging", "production"])
def test_non_scripted_providers_are_never_blocked_by_g1(provider: str, env: str) -> None:
    settings = _settings(llm_provider=provider, app_environment=env)
    assert (settings.llm_provider, settings.app_environment) == (provider, env)


# ─────────── T2 (G2) — 기동 배너 ───────────


def test_startup_banner_warns_on_scripted(caplog: pytest.LogCaptureFixture) -> None:
    import app.main as main_mod

    settings = _settings(
        llm_provider="scripted",
        app_environment="test",
        scripted_llm_mode="delayed",
        scripted_llm_delay_s=5.0,
    )
    with caplog.at_level(logging.WARNING, logger="app.main"):
        main_mod._warn_if_scripted_llm(settings)
    assert any("STUB LLM MODE" in record.message for record in caplog.records)
    assert any(
        "SCRIPTED_LLM_MODE=delayed delay_s=5.0" in record.message for record in caplog.records
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_startup_banner_silent_for_real_providers(
    caplog: pytest.LogCaptureFixture, provider: str
) -> None:
    import app.main as main_mod

    settings = _settings(llm_provider=provider)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        main_mod._warn_if_scripted_llm(settings)
    assert caplog.records == []


# ─────────── T3 (D5) — API 키 없이도 get_llm()이 LoadTestLLM을 돌려준다 ───────────


def test_get_llm_scripted_needs_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_mod,
        "get_settings",
        lambda: _settings(
            llm_provider="scripted",
            app_environment="test",
            openai_api_key="",
            anthropic_api_key="",
        ),
    )
    result = get_llm()
    assert isinstance(result, LoadTestLLM)
    assert result is not None


def test_get_llm_wires_scripted_delay_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_mod,
        "get_settings",
        lambda: _settings(
            llm_provider="scripted",
            app_environment="test",
            scripted_llm_mode="delayed",
            scripted_llm_delay_s=5.0,
        ),
    )

    result = get_llm()

    assert isinstance(result, LoadTestLLM)
    assert result.mode == "delayed"
    assert result.delay_s == 5.0


async def test_loadtest_llm_instant_mode_never_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agents.buyer.recommendation.decompose as decompose_mod

    sleeps: list[float] = []

    async def record_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    llm = LoadTestLLM(mode="instant", delay_s=5.0)

    await llm.complete(system=decompose_mod._SYSTEM, user="USER_MESSAGE: 이어폰", tier="fast")

    assert sleeps == []


async def test_loadtest_llm_delayed_mode_sleeps_five_seconds_once_per_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agents.buyer.recommendation.decompose as decompose_mod

    sleeps: list[float] = []

    async def record_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    llm = LoadTestLLM(mode="delayed", delay_s=5.0)

    await llm.complete(system=decompose_mod._SYSTEM, user="USER_MESSAGE: 이어폰", tier="fast")
    await llm.complete(system=decompose_mod._SYSTEM, user="USER_MESSAGE: 헤드폰", tier="fast")

    assert sleeps == [5.0]


async def test_loadtest_llm_delayed_mode_sleeps_once_for_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agents.buyer.recommendation.decompose as decompose_mod

    sleeps: list[float] = []
    release_sleep = asyncio.Event()
    original_sleep = asyncio.sleep

    async def record_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)
        await release_sleep.wait()

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    llm = LoadTestLLM(mode="delayed", delay_s=5.0)

    first = asyncio.create_task(
        llm.complete(system=decompose_mod._SYSTEM, user="USER_MESSAGE: 이어폰", tier="fast")
    )
    while not sleeps:
        await original_sleep(0)
    second = asyncio.create_task(
        llm.complete(system=decompose_mod._SYSTEM, user="USER_MESSAGE: 헤드폰", tier="fast")
    )
    await original_sleep(0)

    assert not first.done()
    assert not second.done()
    release_sleep.set()
    await asyncio.gather(first, second)

    assert sleeps == [5.0]


# ─────────── D5 — 판매자 레인은 무료 모드 밖(#438 R2 F3) ───────────


def test_seller_lane_rejects_scripted_provider_with_clear_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`init_seller_model` 이 scripted 에서 뜻이 분명한 `LLMNotConfigured` 를 던진다.

    라운드 1은 이 동작을 ad-hoc 스크립트로 수동 확인만 하고 회귀 테스트를 남기지 않았다 —
    다음 사람이 이 가드를 지워도 아무도 몰랐을 것이다. `init_chat_model(model_provider=
    "scripted")` 로 흘러들면 알아보기 힘든 예외가 나므로, 먼저 검사해 거부하는지 고정한다.
    """
    import app.agents.seller.models as seller_models_mod
    from app.core.llm import LLMNotConfigured

    monkeypatch.setattr(
        seller_models_mod,
        "get_settings",
        lambda: _settings(llm_provider="scripted", app_environment="test"),
    )

    with pytest.raises(LLMNotConfigured):
        seller_models_mod.init_seller_model("worker")


# ─────────── T4 (D3 rerank — 가장 중요) ───────────


async def test_loadtest_llm_rerank_survives_arbitrary_real_catalog_ids() -> None:
    """`ScriptedLLM`의 고정 rerank(101/102)를 그대로 썼다면 이 테스트는 실패해야 한다."""
    from app.agents.buyer.recommendation.rerank import rerank

    candidates = [
        SpringProduct(product_id=777, name="여행용 크로스백"),
        SpringProduct(product_id=888, name="접이식 캐리어"),
        SpringProduct(product_id=999, name="기내용 파우치"),
    ]

    result = await rerank(
        LoadTestLLM(),
        query="여행 가방 추천해줘",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=3,
    )

    assert result.ranked  # LLMError로 떨어지지 않음 = 정상 경로
    assert result.grounding_decisions == []  # 기본 arm은 기존 자유문장 경로를 유지
    # 부분집합이 아니라 정확한 등장 순서 — 결정론의 실질(#438 R2 F6). 후보 3건·expose_max=3
    # 이면 1건만 나와도 통과하는 느슨한 단언은 "정상 경로를 지킨다"를 실제로 증명하지 못한다.
    assert [pid for pid, _ in result.ranked] == [777, 888, 999]


async def test_loadtest_llm_supports_production_structured_ranking_default() -> None:
    from app.agents.buyer.recommendation.rerank import rerank

    candidates = [
        SpringProduct(product_id=777, name="여행용 크로스백"),
        SpringProduct(product_id=888, name="접이식 캐리어"),
        SpringProduct(product_id=999, name="기내용 파우치"),
    ]

    result = await rerank(
        LoadTestLLM(),
        query="여행 가방 추천해줘",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=3,
        ranking_arm="structured",
    )

    assert [pid for pid, _ in result.ranked] == [777, 888, 999]
    assert len(result.ranking_decisions) == 3


async def test_scripted_llm_baseline_degrades_on_same_real_catalog_ids() -> None:
    """대조군 — `ScriptedLLM`(고정 101/102)은 후보에 없는 id만 내 항상 degrade로 떨어진다.

    LoadTestLLM이 실제로 문제를 해결했다는 것을 이 대조와 함께 증명한다(#438 D3 핵심 제약).
    """
    from app.agents.buyer.recommendation.rerank import rerank
    from app.core.llm_scripted import ScriptedLLM

    candidates = [SpringProduct(product_id=777, name="여행용 크로스백")]

    with pytest.raises(LLMError):
        await rerank(
            ScriptedLLM(),
            query="여행 가방 추천해줘",
            candidates=candidates,
            profile_summary=None,
            tier="smart",
            expose_max=3,
        )


# ─────────── T5 (D3 분류 커버리지) ───────────


def test_markers_are_present_in_real_system_prompts() -> None:
    """실제 `_SYSTEM` 상수를 import해 마커가 여전히 들어 있는지 고정한다."""
    import app.agents.buyer.recommendation.category_scope as category_scope_mod
    import app.agents.buyer.recommendation.category_select as category_select_mod
    import app.agents.buyer.recommendation.decompose as decompose_mod
    import app.agents.buyer.recommendation.need_priority as need_priority_mod
    import app.agents.buyer.recommendation.needs_expansion as needs_expansion_mod
    import app.agents.buyer.recommendation.rerank as rerank_mod

    assert _DECOMPOSE_MARK in decompose_mod._SYSTEM
    assert _DECOMPOSE_MARK in decompose_mod._SYSTEM_WITH_SCREEN
    assert _RERANK_MARK in rerank_mod._SYSTEM
    assert _RERANK_SCORING_MARK in rerank_mod._SYSTEM_STRUCTURED_SCORING
    assert _CATEGORY_SELECT_MARK in category_select_mod._SYSTEM
    assert _CATEGORY_SCOPE_MARK in category_scope_mod._SYSTEM
    assert _NEED_PRIORITY_MARK in need_priority_mod._SYSTEM
    assert _NEEDS_EXPANSION_MARK in needs_expansion_mod._SYSTEM


async def test_loadtest_llm_decompose_passes_real_parser() -> None:
    """#438 R2 F1/F5 — decompose 스텁이 성립하는 진짜 조건: 되물음 레인으로 새지 않는 것.

    `intent == "recommend"` 만 보면 스텁이 `categoryQueries` 를 항상 비워도(F1 결함) 통과해
    버린다. 카테고리 fan-out 레인이 실제로 도는지(`category_queries` 1건), semanticQuery 가
    고정 문자열이 아니라 발화에서 유도됐는지, 그리고 그 결과로 `is_underspecified_turn`/
    `is_no_condition_turn`(추천 대신 되묻는 턴으로 뒤집는 두 판정)이 실제로 False 인지까지
    실 함수를 import 해 고정한다.
    """
    from app.agents.buyer.recommendation.decompose import decompose
    from app.agents.buyer.recommendation.no_condition import is_no_condition_turn
    from app.agents.buyer.recommendation.underspecified import is_underspecified_turn

    utterance = "캠핑용 랜턴 추천해줘"
    decision = await decompose(
        LoadTestLLM(),
        query=utterance,
        prior_filters=None,
        profile_summary=None,
        tier="fast",
    )

    assert decision.intent == "recommend"
    assert decision.case == 1
    # 고정 문자열(DEFAULT_DECOMPOSE 의 "여행용 파우치" 등)이 아니라 발화에서 유도됐다.
    assert decision.filters.semantic_query == utterance
    assert decision.semantic_query_is_fallback is False
    assert len(decision.category_queries) == 1
    assert decision.category_queries[0].raw_category == utterance
    assert decision.category_queries[0].query == utterance

    settings = _settings(underspecified_reask_enabled=True)
    assert is_underspecified_turn(decision, None, settings) is False
    assert is_no_condition_turn(decision, None) is False


async def test_loadtest_llm_category_select_passes_real_parser() -> None:
    """#438 R2 F2 — 첫 후보를 결정론적으로 골라 매핑 **수락** 갈래를 태운다.

    `{"category": null}` 을 고정으로 내면 `select_category` 가 항상 임베딩 top-1 폴백으로
    퇴화한다 — rerank 에서 잡았던 것과 같은 유형의 결함이라, 이 테스트가 그 결함을 "정상"으로
    봉인하지 않는지까지 확인한다.
    """
    from app.agents.buyer.recommendation.category_select import select_category

    result = await select_category(
        LoadTestLLM(), query="아무거나 상관없어", candidates=["여행용품", "전자기기"], tier="fast"
    )
    assert result == "여행용품"  # 후보 목록의 첫 항목 — 폴백이 아니라 수락 경로


async def test_loadtest_llm_category_select_returns_none_without_candidates_marker() -> None:
    """CANDIDATES 목록 자체를 못 찾은 프롬프트에서만 null — 그 외에는 항상 첫 후보를 낸다."""
    import app.agents.buyer.recommendation.category_select as category_select_mod

    raw = await LoadTestLLM().complete(
        system=category_select_mod._SYSTEM, user="USER_MESSAGE: 아무거나", tier="fast"
    )
    assert json.loads(raw) == {"category": None}


async def test_loadtest_llm_category_scope_passes_real_parser() -> None:
    from app.agents.buyer.recommendation.category_scope import classify_category_scope

    result = await classify_category_scope(
        LoadTestLLM(),
        message="더 저렴한 걸로 보여줘",
        prior_category="여행용품",
        settings=_settings(),
    )
    assert result is False


async def test_loadtest_llm_need_priority_passes_real_parser() -> None:
    from app.agents.buyer.recommendation.need_priority import classify_need_priorities

    result = await classify_need_priorities(
        LoadTestLLM(),
        message="집들이 선물 준비물",
        needs=["캔들", "와인", "디퓨저"],
        settings=_settings(),
    )
    assert result == (2, 2, 2)


async def test_loadtest_llm_needs_expansion_passes_real_parser() -> None:
    from app.agents.buyer.recommendation.needs_expansion import expand_needs

    result = await expand_needs("집들이 선물로 뭐 사갈까", llm=LoadTestLLM(), settings=_settings())
    assert len(result) >= 2
    assert all(isinstance(item, str) and item for item in result)


async def test_loadtest_llm_profile_delta_passes_real_parser() -> None:
    import app.agents.profile.builder as builder_mod

    llm = LoadTestLLM()
    for system in (builder_mod._DELTA_SYSTEM, builder_mod._DELTA_SYSTEM_LEGACY):
        raw = await llm.complete(system=system, user="3만원 이하 여행용품을 좋아해요", tier="smart")
        data = json.loads(raw)
        assert isinstance(data.get("deltas"), list)


async def test_loadtest_llm_profile_consolidate_passes_real_parser() -> None:
    import app.agents.profile.builder as builder_mod

    llm = LoadTestLLM()
    raw = await llm.complete(
        system=builder_mod._CONSOLIDATE_SYSTEM,
        user="- 3만원 이하 여행용품 선호",
        tier="smart",
        json_output=False,
    )
    assert raw.strip()  # consolidate()가 빈 문자열이면 FAILED 로 처리 — 비어있지 않아야 한다


async def test_loadtest_llm_unrecognized_prompt_raises_llmerror_loudly() -> None:
    """미상 프롬프트는 조용한 폴백이 아니라 LLMError — #438 D3 핵심 규약."""
    with pytest.raises(LLMError):
        await LoadTestLLM().complete(
            system="이 저장소 어디에도 없는 전혀 새로운 시스템 프롬프트입니다",
            user="x",
            tier="fast",
        )


# ─────────── T6 (결정론) ───────────


async def test_loadtest_llm_decompose_is_byte_deterministic() -> None:
    import app.agents.buyer.recommendation.decompose as decompose_mod

    llm = LoadTestLLM()
    user = "USER_MESSAGE: 여행용 파우치 추천해줘"
    first = await llm.complete(system=decompose_mod._SYSTEM, user=user, tier="fast")
    second = await llm.complete(system=decompose_mod._SYSTEM, user=user, tier="fast")
    assert first == second


async def test_loadtest_llm_rerank_is_byte_deterministic() -> None:
    import app.agents.buyer.recommendation.rerank as rerank_mod

    llm = LoadTestLLM()
    user = 'CANDIDATES: [{"productId": 777}, {"productId": 888}]'
    first = await llm.complete(system=rerank_mod._SYSTEM, user=user, tier="smart")
    second = await llm.complete(system=rerank_mod._SYSTEM, user=user, tier="smart")
    assert first == second


# ─────────── T7 (변이 시험 — 실 LLM 누출 차단) ───────────


async def test_scripted_mode_buyer_turn_never_constructs_vendor_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scripted 모드에서 decompose→rerank 인프로세스 왕복이 실 vendor SDK 를 전혀 만들지 않는다.

    `get_llm` 의 scripted 분기를 지우는 변이를 넣으면 이 테스트는 `AnthropicLLM`/`OpenAILLM`
    생성자 폭발로 실패해야 한다(보고서 §T7 실측 참조 — 검증 단계에서 실제로 지워보고 확인했다).
    """
    from app.agents.buyer.recommendation.decompose import decompose
    from app.agents.buyer.recommendation.rerank import rerank

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("vendor LLM client constructed while LLM_PROVIDER=scripted")

    monkeypatch.setattr(llm_mod, "AnthropicLLM", _boom)
    monkeypatch.setattr(llm_mod, "OpenAILLM", _boom)
    monkeypatch.setattr(
        llm_mod,
        "get_settings",
        lambda: _settings(llm_provider="scripted", app_environment="test"),
    )

    stub = get_llm()
    assert isinstance(stub, LoadTestLLM)

    decision = await decompose(
        stub, query="여행용 파우치 추천해줘", prior_filters=None, profile_summary=None, tier="fast"
    )
    assert decision.intent == "recommend"

    candidates = [SpringProduct(product_id=pid, name=f"상품{pid}") for pid in (777, 888, 999)]
    result = await rerank(
        stub,
        query="여행 가방",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=3,
    )
    assert {pid for pid, _ in result.ranked} <= {777, 888, 999}
