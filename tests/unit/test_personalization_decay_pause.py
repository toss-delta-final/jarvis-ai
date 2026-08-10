"""중지 기간 감쇠 정지의 **저장 계층** (REQ-PGRAPH-055, 이슈 #359).

순수 함수 쪽(겹침 차감)은 `test_profile_graph_merge.py` 의 「중지 기간 감쇠 정지」 절이 잰다.
여기서는 그 입력이 어떻게 쌓이고 읽히는지 — 재개 시 구간 append, 상한 병합, 배치 주입 — 를 잰다.

**구간을 그래프 문서가 아니라 플래그 테이블에 두는 이유**(SPEC §7.1): 문서에 두면 중지·재개가
문서를 고치게 되어 §3.9.5 응답의 `graphVersion`·감사 before/after·멱등 원장 payload 가 전부
거짓이 되고(그 경로는 "그래프 문서는 안 바뀐다"를 전제로 같은 값을 싣는다), 그래프 잠금까지
잡아야 해 §7.2 의 "락을 두 개 쥐지 않는다"도 깨진다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.store import reset_profile_store
from app.core.config import Settings

USER = 123


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


async def _disable(now: str) -> None:
    await graph_journal.set_personalization_flag(user_id=USER, enabled=False, now=now)


async def _enable(now: str) -> None:
    await graph_journal.set_personalization_flag(user_id=USER, enabled=True, now=now)


async def test_resuming_appends_the_disabled_span() -> None:
    """재개가 `disabled_at → now` 를 구간으로 남긴다 — 이 값이 감쇠 차감의 유일한 입력이다."""
    await _disable("2026-03-01T00:00:00+00:00")
    await _enable("2026-07-01T00:00:00+00:00")

    state = await graph_journal.get_personalization_state(user_id=USER)

    assert state.enabled is True
    assert state.disabled_spans == [
        {"from": "2026-03-01T00:00:00+00:00", "to": "2026-07-01T00:00:00+00:00"}
    ]


async def test_spans_accumulate_across_toggles() -> None:
    await _disable("2026-01-01T00:00:00+00:00")
    await _enable("2026-02-01T00:00:00+00:00")
    await _disable("2026-05-01T00:00:00+00:00")
    await _enable("2026-06-01T00:00:00+00:00")

    state = await graph_journal.get_personalization_state(user_id=USER)

    assert [span["from"] for span in state.disabled_spans] == [
        "2026-01-01T00:00:00+00:00",
        "2026-05-01T00:00:00+00:00",
    ]


async def test_still_disabled_reports_no_span_yet() -> None:
    """중지 중에는 구간이 안 닫힌다 — 끝나지 않은 구간을 감쇠에서 빼면 미래를 차감하는 셈이다.

    중지 중에는 배치가 아예 안 돌아(C7) 감쇠도 안 걸리므로, 열린 구간을 실을 필요가 없다.
    """
    await _disable("2026-03-01T00:00:00+00:00")

    state = await graph_journal.get_personalization_state(user_id=USER)

    assert state.enabled is False
    assert state.disabled_spans == []


async def test_repeated_enable_is_a_no_op_and_does_not_duplicate_spans() -> None:
    """같은 값으로 다시 켜도 구간이 늘지 않는다 — `set_personalization_flag` 가 no-op 이다."""
    await _disable("2026-03-01T00:00:00+00:00")
    await _enable("2026-07-01T00:00:00+00:00")
    await _enable("2026-08-01T00:00:00+00:00")

    state = await graph_journal.get_personalization_state(user_id=USER)

    assert len(state.disabled_spans) == 1


async def test_spans_over_the_cap_merge_the_two_oldest(monkeypatch) -> None:
    """상한을 넘으면 **가장 오래된 두 구간을 하나로 합친다** — 결정론적이고 보수적이다.

    합치면 그 사이 간격까지 중지로 세므로 감쇠를 **덜 빼는**(=취향을 더 오래 살리는) 쪽으로
    틀린다. 반대로 오래된 것을 버리면 이미 지난 중지가 없던 일이 되어 감쇠가 몰린다.
    """
    settings = Settings(_env_file=None, graph_decay_pause_spans_max=2)
    monkeypatch.setattr(graph_journal, "get_settings", lambda: settings)

    for month in (1, 3, 5):
        await _disable(f"2026-0{month}-01T00:00:00+00:00")
        await _enable(f"2026-0{month}-10T00:00:00+00:00")

    state = await graph_journal.get_personalization_state(user_id=USER)

    assert state.disabled_spans == [
        # 1월·3월이 하나로 — bounding span 이다.
        {"from": "2026-01-01T00:00:00+00:00", "to": "2026-03-10T00:00:00+00:00"},
        {"from": "2026-05-01T00:00:00+00:00", "to": "2026-05-10T00:00:00+00:00"},
    ]


async def test_state_defaults_to_enabled_without_a_row() -> None:
    state = await graph_journal.get_personalization_state(user_id=USER)

    assert state.enabled is True
    assert state.disabled_spans == []


# ─────────── consolidate 배선 ───────────


async def test_consolidate_feeds_the_spans_into_the_merge_engine(monkeypatch) -> None:
    """`consolidate` 가 구간을 읽어 병합 엔진에 넘긴다 — 이 배선이 없으면 저장만 하고 안 쓴다.

    저장 계층과 순수 함수가 각각 옳아도 그 둘을 잇는 한 줄이 빠지면 전부 초록불인 채로
    감쇠 정지가 동작하지 않는다.
    """
    from app.agents.profile import builder
    from app.agents.profile.store import get_profile_store

    await _disable("2026-03-01T00:00:00+00:00")
    await _enable("2026-07-01T00:00:00+00:00")
    store = await get_profile_store()
    await store.add_fact(str(USER), "소니 선호", cap=200, graph_triples=[])

    seen: list = []
    original = builder.build_graph_document

    def spy(facts, **kwargs):  # noqa: ANN001
        seen.append(kwargs.get("decay_pause_spans"))
        return original(facts, **kwargs)

    monkeypatch.setattr(builder, "build_graph_document", spy)

    class _LLM:
        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
            return "# 취향 요약"

        async def stream(self, **kwargs):  # noqa: ANN001
            yield "x"

    await builder.consolidate(str(USER), llm=_LLM(), settings=Settings(_env_file=None))

    assert seen == [[{"from": "2026-03-01T00:00:00+00:00", "to": "2026-07-01T00:00:00+00:00"}]]
