"""N 채우기 러너 테스트 — #462. 전부 가짜 LLM·가짜 카탈로그라 API/pg 콜이 0이다."""

from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from evals.model_eval.budget import BudgetExceeded
from evals.taste_probe.fakes import ScriptedDeltaLLM, build_fake_catalog
from evals.taste_probe.loader import load_golden_set
from evals.taste_probe.runner import run_taste_session
from evals.taste_probe.seams import fake_catalog_seam

GOLDEN = load_golden_set("default")
CATALOG = build_fake_catalog(GOLDEN)
SESSIONS_BY_ID = {session.session_id: session for session in GOLDEN.sessions}


async def _no_sleep(seconds: float) -> None:
    return None


def _kwargs(**overrides):
    base = dict(n=3, attempt_multiplier=5, settings=get_settings(), sleep=_no_sleep)
    base.update(overrides)
    return base


async def test_retries_fill_n_and_failures_never_become_samples() -> None:
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = ScriptedDeltaLLM(GOLDEN, fail_first=2)
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(llm=llm, session=session, **_kwargs())
    assert len(result.samples) == 3
    assert result.filled is True
    assert len(result.failures) == 2
    assert result.attempts == 5
    assert all(f.error_type.startswith("transportError") for f in result.failures)


async def test_unfillable_session_is_reported_not_raised() -> None:
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = ScriptedDeltaLLM(GOLDEN, always_fail=True)
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm, session=session, **_kwargs(n=2, attempt_multiplier=3)
        )
    assert result.samples == []
    assert result.filled is False
    assert result.attempts == 6
    assert len(result.failures) == 6


async def test_schema_violations_are_classified_and_never_become_samples() -> None:
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = ScriptedDeltaLLM(GOLDEN, schema_violation_every=1)
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm, session=session, **_kwargs(n=2, attempt_multiplier=3)
        )
    assert result.samples == []
    assert result.filled is False
    assert result.failures
    assert all(f.error_type == "schemaViolation" for f in result.failures)


async def test_transport_error_is_classified_generic_by_default() -> None:
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = ScriptedDeltaLLM(GOLDEN, always_fail=True, fail_kind="generic")
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm, session=session, **_kwargs(n=1, attempt_multiplier=2)
        )
    assert result.filled is False
    assert all(f.error_type == "transportError:other" for f in result.failures)


async def test_transport_error_timeout_is_classified_via_production_discriminator() -> None:
    """`is_timeout_error` 는 원인 체인(`__cause__`)의 `TimeoutError` 를 본다 — 프로덕션 판별기를
    그대로 부르므로(자체 판정 아님) 이 시험은 그 채록·재전파가 원인 체인을 보존하는지를 잰다."""
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = ScriptedDeltaLLM(GOLDEN, always_fail=True, fail_kind="timeout")
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm, session=session, **_kwargs(n=1, attempt_multiplier=2)
        )
    assert result.filled is False
    assert all(f.error_type == "transportError:timeout" for f in result.failures)


async def test_category_session_resolves_through_fake_catalog_seam() -> None:
    """category kind 는 exact_lookup 경로를 타므로, 시임이 없으면 실 pg 를 부르려 한다 — 이 시험은
    시임이 실제로 그 콜을 가로채 결정론 결과를 내는지 확인한다."""
    session = SESSIONS_BY_ID["kc-category-01"]
    llm = ScriptedDeltaLLM(GOLDEN)
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm, session=session, **_kwargs(n=1, attempt_multiplier=2)
        )
    assert result.filled is True
    sample = result.samples[0]
    assert len(sample.produced) == 1
    assert sample.produced[0].node_type == "category"
    assert sample.produced[0].node_label == "음향가전 > 이어폰"
    assert sample.produced[0].verified is True


async def test_samples_do_not_leak_facts_across_attempts() -> None:
    """표본 간 fact 공유가 있으면(user_id 재사용 버그) 시도마다 고유한 조작 오탐 fact 가 누적돼
    산출 트리플 수가 2,4,6 으로 늘어난다 — 격리가 맞으면 매 표본이 2 로 고정된다."""
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = ScriptedDeltaLLM(GOLDEN, extra_every=1)
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm, session=session, **_kwargs(n=3, attempt_multiplier=3)
        )
    assert result.filled is True
    produced_counts = [len(sample.produced) for sample in result.samples]
    assert produced_counts == [2, 2, 2], produced_counts


async def test_default_settings_smoke() -> None:
    """전 테스트가 기본값을 덮어쓰면 기본 조합의 결함이 숨는다 — 실 Settings 기본으로 최소 1회."""
    session = SESSIONS_BY_ID["kc-attribute-01"]
    llm = ScriptedDeltaLLM(GOLDEN)
    with fake_catalog_seam(CATALOG):
        result = await run_taste_session(
            llm=llm,
            session=session,
            n=1,
            attempt_multiplier=3,
            settings=get_settings(),
            sleep=_no_sleep,
        )
    assert result.filled is True


class _BudgetExhaustedLLM:
    """[A-1] 예산 소진을 흉내 내는 가짜 — `RecordingLLM.complete` 가 provider 호출 **전에**
    `budget.reserve()` 로 던지는 것과 같은 성질(전송 실패가 아니라 런 중단 신호)."""

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, **kwargs: object) -> str:
        self.call_count += 1
        raise BudgetExceeded("scripted budget exhausted")

    def stream(self, **kwargs: object) -> None:
        raise NotImplementedError("taste_probe 는 stream 을 쓰지 않는다")


async def test_budget_exceeded_propagates_and_is_not_retried() -> None:
    """[A-1] 실측 재현: 고치기 전에는 `BudgetExceeded` 가 `except Exception` 에 걸려 매 시도
    "transportError:other" 로 위장되고 세션마다 n×attempt_multiplier 회 헛되이 재시도했다
    (attempts=9, failures=9). 지금은 첫 시도에서 즉시 전파되고 재시도가 없어야 한다."""
    session = SESSIONS_BY_ID["kc-brand-01"]
    llm = _BudgetExhaustedLLM()
    with pytest.raises(BudgetExceeded):
        await run_taste_session(
            llm=llm,
            session=session,
            n=3,
            attempt_multiplier=3,
            settings=get_settings(),
            sleep=_no_sleep,
        )
    assert llm.call_count == 1, "재시도 없이 첫 호출에서 즉시 전파돼야 한다"


class _DuplicateFactLLM:
    """[A-2/A-10] 서로 다른 델타 2개가 **같은 fact 문자열**을 낸다 — `add_fact` dedup 이 store
    레코드는 1건으로 합치지만, 프로덕션 `promoted` 반환값은 개별 게이트 통과 건수를 그대로 센다.
    """

    async def complete(self, **kwargs: object) -> str:
        delta = {
            "fact": "중복 취향",
            "kind": "attribute",
            "label": "가벼움",
            "anchorPhrase": "가벼움",
            "polarity": "positive",
            "predicateHint": "prefers",
            "salience": 0.9,
            "explicit": True,
            "repetitionEma": 0.0,
        }
        return json.dumps({"deltas": [delta, dict(delta)]}, ensure_ascii=False)

    def stream(self, **kwargs: object) -> None:
        raise NotImplementedError("taste_probe 는 stream 을 쓰지 않는다")


async def test_promoted_count_reflects_production_value_not_store_record_count() -> None:
    """[A-2] `promotedCount` 는 프로덕션 반환값(개별 게이트 통과 건수, 여기선 2)이지 store 레코드
    수(dedup 후 1)가 아니다 — 파생 뺄셈(`emittedDeltas-gateRejected`)이었어도 우연히 2 가 나오므로
    (2-0=2), 이 시험은 `factDedupCollapsed` 로 dedup 이 실제로 일어났음을 함께 확인해 "레코드 수를
    쓰면 안 된다"는 A-2 의 핵심을 검증한다(§A-10)."""
    session = SESSIONS_BY_ID["kc-attribute-01"]
    result = await run_taste_session(
        llm=_DuplicateFactLLM(),
        session=session,
        n=1,
        attempt_multiplier=2,
        settings=get_settings(),
        sleep=_no_sleep,
    )
    assert result.filled is True
    sample = result.samples[0]
    assert sample.emitted_deltas == 2
    assert sample.gate_rejected == 0
    assert sample.promoted_count == 2  # 프로덕션 반환값 — 개별 게이트 통과 건수
    assert sample.fact_dedup_collapsed == 1  # promotedCount(2) - fact 레코드 수(1)
    assert len(sample.produced) == 1  # store 는 fact 문자열 기준으로 1건만 저장한다


async def test_resolver_dropped_is_attributed_to_the_correct_kind() -> None:
    """[A-3] kind 회전으로 밴드 파싱이 실패하는 델타를 만들어, `resolverDropped` 가 라벨뿐 아니라
    **kind** 도 정확히 싣는지 확인한다 — 종전(카운트만)에는 이 라벨이 "정상 라벨이 거부됐다"처럼
    읽혔지만(실측: `bandLabelRejected: ['30000-50000']`), kind 를 보면 ratingBand 로 회전된
    결과라는 게 드러난다."""
    session = SESSIONS_BY_ID["kc-priceBand-01"]  # canonical "30000-50000"
    llm = ScriptedDeltaLLM(GOLDEN, misclassify_every=1)  # priceBand → ratingBand 회전(고정 표)
    result = await run_taste_session(
        llm=llm,
        session=session,
        n=1,
        attempt_multiplier=2,
        settings=get_settings(),
        sleep=_no_sleep,
    )
    assert result.filled is True
    sample = result.samples[0]
    assert sample.produced == []
    assert sample.resolver_dropped == [{"kind": "ratingBand", "label": "30000-50000"}]
    assert sample.band_label_rejected == [{"kind": "ratingBand", "label": "30000-50000"}]
    assert sample.legacy_schema_no_kind == 0
