"""개인화 중지·재개 (#358, §7.2 3행 · REQ-PGRAPH-050~052/080).

본 이슈 범위는 **플래그 테이블과 요약 사용 표식까지**다 — 실제 소비·수집 차단은 #359 다.

여기서 고정하는 두 가지:
  - **락을 두 개 쥐지 않는다.** 플래그는 단일 행 upsert 라 그 자체로 원자적이고, 요약 표식만
    기존 요약 락 아래에서 내린다. 그래프 락과 요약 락을 동시에 잡으면 advisory 풀에서 커넥션을
    둘 점유해 구매자 hot-path 가 3초 타임아웃으로 죽는다(`store.py` 실측 주석).
  - **중지는 삭제가 아니다.** 요약 본문·벡터는 그대로 두고 표식만 내린다 — 지우면 사용자가
    다시 켰을 때 되살릴 것이 없다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.store import get_profile_store, reset_profile_store

USER = "358"
NOW = "2026-08-10T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


async def test_disabling_lowers_the_summary_flag_without_deleting_the_text() -> None:
    """중지하면 표식이 내려가고 본문은 남는다 (REQ-PGRAPH-100)."""
    store = await get_profile_store()
    await store.set_summary(USER, "소니 선호", NOW)

    await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-1", now=NOW
    )

    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is False
    summary = await store.get_summary(USER)
    assert summary is not None
    assert summary.usable is False
    assert summary.markdown == "소니 선호"  # 지우지 않았다


async def test_re_enabling_restores_the_flag_and_the_summary_is_intact() -> None:
    """다시 켜면 되살아난다 — 중지가 데이터 손실이 아니라는 것이 이 왕복으로 드러난다."""
    store = await get_profile_store()
    await store.set_summary(USER, "소니 선호", NOW)
    await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-1", now=NOW
    )

    await graph_journal.set_personalization(
        user_id=int(USER), enabled=True, request_id="req-2", now=NOW
    )

    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is True
    summary = await store.get_summary(USER)
    assert summary is not None and summary.usable is True and summary.markdown == "소니 선호"


async def test_toggling_to_the_same_value_leaves_no_audit_row() -> None:
    """같은 값 재전송은 상태 변경이 아니다 — 감사 행을 남기지 않는다 (REQ-PGRAPH-080)."""
    await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-1", now=NOW
    )

    result = await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-2", now=NOW
    )

    assert result.replayed is True
    rows = await graph_journal.list_audit(user_id=int(USER))
    assert [row.action for row in rows] == ["personalizationToggle"]  # 두 번째는 안 남았다


async def test_the_flag_defaults_to_enabled() -> None:
    """한 번도 끈 적 없으면 켜짐이다 — 기본값이 중지면 개인화가 조용히 안 도는 상태가 된다."""
    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is True


async def test_toggle_works_without_a_profile_summary() -> None:
    """요약이 없어도 중지가 걸린다 — 프로필 유무에 따라 토글이 실패하면 안 된다."""
    await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-1", now=NOW
    )

    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is False


async def test_toggle_honours_if_match_when_given() -> None:
    """`If-Match` 는 선택이지만 주면 존중한다 (api-spec §3.9.5)."""
    with pytest.raises(graph_journal.GraphVersionConflict):
        await graph_journal.set_personalization(
            user_id=int(USER), enabled=False, request_id="req-1", now=NOW, if_match="g99"
        )

    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is True


async def test_re_enabling_is_not_blocked_by_the_disable_ledger_entry() -> None:
    """껐다 켜기가 **같은 ****`If-Match`**** 를 들고 와도** 막히지 않는다 (#360, api-spec §3.9.5).

    중지 경로는 그래프 문서를 건드리지 않아 `graphVersion` 이 변하지 않는다 — 그래서 끄기와
    켜기가 **정상적으로 같은 선행조건**을 지참한다. 파생 키가 대상 상태를 담지 않으면 두 요청이
    같은 키가 되어 **켜기가 끄기 응답을 재생**하고, 원장 TTL(`graph_idempotency_ttl_h`, 기본 24h)
    동안 사용자가 개인화를 다시 켤 수 없다. 정본 I-37 은 정반대를 요구한다 — *"토글은 마지막
    의사가 이기는 게 자연스럽고, 충돌로 막으면 「끄기」가 실패하는 상황이 생긴다."*
    """
    off = await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-1", now=NOW, if_match="g0"
    )
    assert off.replayed is False
    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is False

    on = await graph_journal.set_personalization(
        user_id=int(USER), enabled=True, request_id="req-2", now=NOW, if_match="g0"
    )

    assert on.replayed is False, "켜기가 끄기 응답의 재생으로 처리됐다"
    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is True


async def test_resending_the_same_toggle_with_the_same_if_match_still_replays() -> None:
    """같은 값·같은 `If-Match` 재전송은 **여전히 재생**이다 — 위 수정이 멱등을 없애지 않는다."""
    await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-1", now=NOW, if_match="g0"
    )

    again = await graph_journal.set_personalization(
        user_id=int(USER), enabled=False, request_id="req-2", now=NOW, if_match="g0"
    )

    assert again.replayed is True
    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is False
    rows = await graph_journal.list_audit(user_id=int(USER))
    assert [row.action for row in rows] == ["personalizationToggle"]  # 두 번째는 안 남았다


async def test_the_toggle_never_holds_the_graph_lock() -> None:
    """중지 경로는 그래프 락을 잡지 않는다 — 잡으면 요약 락과 중첩해 advisory 풀을 이중 점유한다.

    `store.py` 가 실측으로 못박은 제약이고, 그 위반은 구매자 턴 경로(`append_session_ctx`)까지
    3초 타임아웃으로 죽인다. 락을 쥔 채로 토글이 도는지를 구조적으로 확인한다.
    """
    from app.agents.profile import store as store_mod

    store = await get_profile_store()
    await store.set_summary(USER, "소니 선호", NOW)

    async with store.graph_lock(USER):  # 다른 작업이 그래프 락을 쥐고 있어도
        assert store_mod._graph_lock(USER).locked()  # noqa: SLF001
        await graph_journal.set_personalization(  # 토글은 막히지 않는다
            user_id=int(USER), enabled=False, request_id="req-1", now=NOW
        )

    assert await graph_journal.get_personalization_flag(user_id=int(USER)) is False
