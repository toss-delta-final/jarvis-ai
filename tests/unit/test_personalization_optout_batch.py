"""개인화 중지 — **배치 수집 차단** (REQ-PGRAPH-052/053/056, 이슈 #359).

배치 경로는 hot-path 와 실패 정책이 다르다. 잃는 것이 발화 1건이 아니라 **누적 버퍼 전체**라,
판정 불가(`None`)를 "중지"로 접으면 DB 블립 한 번에 개인화가 켜져 있는 사용자의 세션 버퍼가
영구 삭제된다.

`builder.generate_session_delta` 의 반환 계약이 그 갈림길이다(그 docstring):
  - `tuple` = **처리됨** → finalizer 가 `clear_session_ctx_upto` → `complete_claim` → COMPLETED
  - `None`  = **degrade** → RETRYABLE, 버퍼 보존, 다음 sweep 재시도

그래서 **중지 확인 = 튜플**(REQ-PGRAPH-053: 수집을 멈춰도 버퍼 정리와 처리 완료 표시는
계속해야 세션 라이프사이클이 안 멈춘다), **판정 불가 = `None`** 이다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.builder import ConsolidationResult, consolidate, generate_session_delta
from app.agents.profile.store import get_profile_store, reset_profile_store
from app.core.config import Settings

USER_ID = "123"
THREAD_KEY = "123:s1"


class _ScriptedLLM:
    """델타·요약을 system 프롬프트로 갈라 답하고 **호출을 기록**한다.

    `test_profile_consolidate_graph._CapturingLLM` 과 같은 관례다 — 중지 중에는 이 기록이
    비어 있어야 한다(비용을 쓰면서 결과를 버리지 않는다).
    """

    def __init__(self, *, delta: str = '{"deltas": []}', summary: str = "# 취향 요약") -> None:
        self._delta = delta
        self._summary = summary
        self.calls: list[str] = []

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if "델타 추출기" in system:
            self.calls.append("delta")
            return self._delta
        self.calls.append("summary")
        return self._summary

    async def stream(self, *, system, user, tier, max_tokens=1024):  # noqa: ANN001
        yield "x"


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


async def _disable() -> None:
    await graph_journal.set_personalization_flag(
        user_id=int(USER_ID), enabled=False, now="2026-08-10T00:00:00+00:00"
    )


async def _seed_buffer(*messages: str) -> int:
    store = await get_profile_store()
    for message in messages:
        await store.append_session_ctx(THREAD_KEY, message, cap=100, repeat_cap=2)
    _, watermark = await store.get_session_ctx_snapshot(THREAD_KEY)
    return watermark


def _delta_llm() -> _ScriptedLLM:
    return _ScriptedLLM(
        delta='{"deltas": [{"fact": "소니 선호", "salience": 0.9, "explicit": true}]}'
    )


# ─────────── generate_session_delta ───────────


async def test_delta_extraction_is_skipped_but_reported_as_processed_while_disabled(
    settings: Settings,
) -> None:
    """중지 중에는 델타를 뽑지 않되 **"처리됨"으로 보고한다** (REQ-PGRAPH-053).

    `None`(degrade)을 돌려주면 finalizer 가 RETRYABLE 로 빠지고 버퍼가 남아, **같은 자리에서
    sweep 이 영구 재시도**한다 — 세션 라이프사이클이 멈춘다. 튜플을 돌려줘야 호출자가
    `clear_session_ctx_upto` → `complete_claim` 까지 정상 진행한다.
    """
    watermark = await _seed_buffer("소니 이어폰 좋아해")
    await _disable()

    result = await generate_session_delta(
        USER_ID, THREAD_KEY, profile_watermark=watermark, llm=_delta_llm(), settings=settings
    )

    assert result == ([], watermark)
    store = await get_profile_store()
    assert await store.get_facts(USER_ID) == []  # 승격 0건 — 수집이 실제로 멈췄다


async def test_delta_extraction_does_not_call_the_llm_while_disabled(settings: Settings) -> None:
    """LLM 왕복도 하지 않는다 — 중지 중 비용을 쓰면서 결과를 버리는 것은 무의미하다."""
    watermark = await _seed_buffer("소니 이어폰 좋아해")
    await _disable()
    llm = _delta_llm()

    await generate_session_delta(
        USER_ID, THREAD_KEY, profile_watermark=watermark, llm=llm, settings=settings
    )

    assert llm.calls == []


async def test_delta_extraction_preserves_the_buffer_when_the_flag_lookup_fails(
    settings: Settings, monkeypatch
) -> None:
    """**판정 불가는 `None`** — 버퍼를 보존하고 다음 sweep 에 재시도한다.

    여기서 튜플("처리됨")을 돌려주면 호출자가 `clear_session_ctx_upto` 까지 진행해 **DB 블립
    한 번에 개인화가 켜져 있는 사용자의 누적 버퍼가 영구 삭제**된다. hot-path 는 잃는 것이
    발화 1건이지만 여기는 세션 전체라, 같은 "판정 불가"라도 정책이 갈린다.
    """

    async def _boom(*, user_id: int) -> bool:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(graph_journal, "get_personalization_flag", _boom)
    watermark = await _seed_buffer("소니 이어폰 좋아해")

    result = await generate_session_delta(
        USER_ID, THREAD_KEY, profile_watermark=watermark, llm=_delta_llm(), settings=settings
    )

    assert result is None  # degrade — 버퍼 보존 신호
    store = await get_profile_store()
    assert await store.get_session_ctx_upto(THREAD_KEY, watermark) != []


async def test_delta_extraction_still_promotes_while_enabled(settings: Settings) -> None:
    """반대 방향 — 켜져 있으면 종전대로 승격된다(수집이 통째로 죽지 않았다)."""
    watermark = await _seed_buffer("소니 이어폰 좋아해")

    promoted, returned = await generate_session_delta(
        USER_ID, THREAD_KEY, profile_watermark=watermark, llm=_delta_llm(), settings=settings
    )

    assert promoted == ["소니 선호"] and returned == watermark


# ─────────── consolidate ───────────


async def test_consolidate_is_a_no_op_while_disabled(settings: Settings) -> None:
    """중지 중에는 그래프·요약·임베딩을 만들지 않는다 (REQ-PGRAPH-052).

    `NO_WORK` 는 finalizer 가 이미 "정상 종료" 로 처리하는 값이라 라이프사이클이 멈추지 않는다
    (`FAILED` 만 RETRYABLE 로 간다).
    """
    store = await get_profile_store()
    await store.add_fact(USER_ID, "소니 선호", cap=200, graph_triples=[])
    await _disable()

    llm = _ScriptedLLM()
    assert await consolidate(USER_ID, llm=llm, settings=settings) is ConsolidationResult.NO_WORK
    assert llm.calls == []  # 요약 LLM 왕복도 없다
    assert await store.get_graph(USER_ID) is None  # 그래프 문서를 쓰지 않았다
    assert await store.get_summary(USER_ID) is None


async def test_consolidate_degrades_instead_of_skipping_when_the_flag_lookup_fails(
    settings: Settings, monkeypatch
) -> None:
    """판정 불가면 **중지로 간주하지 않는다** — 켜진 사용자의 배치를 조용히 건너뛰면 안 된다.

    여기서 `NO_WORK` 로 접으면 pg 블립이 지속되는 동안 그 사용자의 프로필이 영영 갱신되지
    않는데, 로그 말고는 드러날 신호가 없다. 종전대로 진행하고 실패는 자기 경로에서 난다.
    """

    async def _boom(*, user_id: int) -> bool:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(graph_journal, "get_personalization_flag", _boom)
    store = await get_profile_store()
    await store.add_fact(USER_ID, "소니 선호", cap=200, graph_triples=[])

    result = await consolidate(
        USER_ID, llm=_ScriptedLLM(summary="# 요약\n- 소니 선호"), settings=settings
    )

    assert result is ConsolidationResult.UPDATED
    assert await store.get_summary(USER_ID) is not None


# ─────────── REQ-PGRAPH-056 소급 금지 ───────────


async def test_re_enabling_does_not_backfill_utterances_from_the_disabled_window(
    settings: Settings,
) -> None:
    """중지 기간의 발화는 재개해도 반영되지 않는다 (REQ-PGRAPH-056).

    **누락이 아니라 규칙이다.** 중지 중 세션이 끝나면 버퍼는 "처리됨" 으로 비워지므로(위
    REQ-PGRAPH-053 경로) 재개 후 소급할 원문 자체가 남지 않는다 — 소급 금지가 별도 방어가
    아니라 그 경로의 귀결이라는 것을 여기서 고정한다.
    """
    watermark = await _seed_buffer("중지 중에 말한 취향")
    await _disable()

    # 세션 종료 — 중지 중이라 델타는 안 뽑히지만 "처리됨" 이라 호출자가 버퍼를 비운다.
    result = await generate_session_delta(
        USER_ID, THREAD_KEY, profile_watermark=watermark, llm=_delta_llm(), settings=settings
    )
    assert result == ([], watermark)
    store = await get_profile_store()
    await store.clear_session_ctx_upto(THREAD_KEY, watermark)

    # 재개.
    await graph_journal.set_personalization_flag(
        user_id=int(USER_ID), enabled=True, now="2026-08-11T00:00:00+00:00"
    )
    _, later = await store.get_session_ctx_snapshot(THREAD_KEY)
    promoted = await generate_session_delta(
        USER_ID, THREAD_KEY, profile_watermark=later, llm=_delta_llm(), settings=settings
    )

    assert promoted is None  # 버퍼가 비어 있다 — 소급할 원문이 없다
    assert await store.get_facts(USER_ID) == []


# ─────────── finalizer 통과 (REQ-PGRAPH-053) ───────────


async def test_finalizer_clears_the_buffer_and_completes_the_claim_while_disabled(
    monkeypatch,
) -> None:
    """**중지 중에도 세션 finalizer 는 버퍼 정리와 처리 완료 표시를 계속한다** (REQ-PGRAPH-053).

    앞의 단위 테스트들은 `generate_session_delta` 의 **반환값**만 재고, 그 반환값이 finalizer 를
    실제로 어디로 보내는지는 재지 못한다 — `builder.py` 는 한 줄도 안 바뀌었는데 `finalizer.py`
    분기가 나중에 바뀌면 조용히 깨진다. 여기서 `process_profile_checkpoint` 를 통째로 통과시켜
    "버퍼가 비었고 claim 이 완료됐고 COMPLETED 다" 를 끝에서 끝까지 확인한다.

    이 보장이 없으면 중지한 사용자의 세션이 `RETRYABLE` 로 남아 **idle sweep 이 같은 자리에서
    영구 재시도**한다.
    """
    from app.agents.profile import finalizer, processed_events

    store = await get_profile_store()
    key = "123:s-optout"
    await store.append_session_ctx(key, "중지 중 발화", cap=100, repeat_cap=2)
    _, watermark = await store.get_session_ctx_snapshot(key)
    await _disable()
    monkeypatch.setattr(finalizer, "get_llm", lambda: _delta_llm())

    result = await finalizer.process_profile_checkpoint(
        int(USER_ID),
        "s-optout",
        event_id="e-optout",
        profile_watermark=watermark,
        settings=Settings(_env_file=None),
    )

    assert result.status is finalizer.ProfilePhaseStatus.COMPLETED
    assert await store.get_session_ctx_upto(key, watermark) == []  # 버퍼 정리
    assert await processed_events.get_status("e-optout") == "completed"  # 처리 완료 표시
    assert await store.get_facts(USER_ID) == []  # 수집은 실제로 멈췄다
