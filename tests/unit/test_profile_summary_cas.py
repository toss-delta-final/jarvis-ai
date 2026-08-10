"""요약 쓰기 compare-and-set — #323 의 잔여 (#358 작업 범위 5).

**잠금은 이미 있다**(PR #387). 여기서 닫는 것은 잠금으로 닫히지 않는 갭이다.

`consolidate()` 는 그래프 락을 놓은 뒤 LLM 왕복(수 초)을 하고 `set_summary` 를 부른다. 그 창에서
사용자가 요약을 고치면, 배치가 돌아와 **낡은 fact 스냅샷으로 만든 요약**으로 사용자 편집을 덮는다.
두 쓰기가 시간상 겹치지 않으므로 잠금은 이 상황을 막지 못한다 — 필요한 것은 "내가 읽은 뒤로
바뀌었나"를 묻는 CAS 다(SPEC §7.4 "남은 부분", §12-6).

락 키를 합치는 대안은 채택하지 않았다. 그러려면 `consolidate` 가 LLM 왕복 내내 요약 락을 쥐어야
하고, 그동안 `record_remember` hot-path 의 요약 경로가 초 단위로 막힌다(#387 코멘트 1안).
"""

from __future__ import annotations

import pytest

from app.agents.profile.store import get_profile_store, reset_profile_store

USER = "358"
NOW = "2026-08-10T00:00:00+00:00"
LATER = "2026-08-10T00:01:00+00:00"


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    yield
    reset_profile_store()


async def test_summary_carries_a_monotonic_sequence() -> None:
    """쓸 때마다 `seq` 가 오른다 — CAS 가 비교할 대상이다."""
    store = await get_profile_store()

    await store.set_summary(USER, "첫 요약", NOW)
    first = await store.get_summary(USER)
    await store.set_summary(USER, "둘째 요약", LATER)
    second = await store.get_summary(USER)

    assert first is not None and second is not None
    assert second.seq == first.seq + 1


async def test_legacy_summary_without_seq_is_absorbed_as_zero() -> None:
    """`seq` 가 없던 구 요약도 읽힌다 — 필드 추가가 기존 사용자의 프로필을 지우면 안 된다."""
    store = await get_profile_store()
    await store._store.aput(  # noqa: SLF001
        ("profile", USER), "summary", {"markdown": "구 요약", "generated_at": NOW}
    )

    summary = await store.get_summary(USER)

    assert summary is not None
    assert summary.markdown == "구 요약"
    assert summary.seq == 0
    assert summary.usable is True  # 표식이 없던 요약은 사용 가능으로 본다


async def test_stale_expected_seq_is_rejected_without_writing() -> None:
    """내가 읽은 뒤에 바뀌었으면 쓰지 않는다 — **이 이슈가 닫으려는 갭이다**."""
    store = await get_profile_store()
    await store.set_summary(USER, "배치가 읽은 시점", NOW)
    observed = await store.get_summary(USER)
    assert observed is not None
    await store.set_summary(USER, "사용자 편집", LATER)  # 그 사이 사용자가 고쳤다

    applied = await store.set_summary(
        USER, "배치가 만든 낡은 요약", LATER, expected_seq=observed.seq
    )

    assert applied is False
    current = await store.get_summary(USER)
    assert current is not None and current.markdown == "사용자 편집"  # 안 덮였다


async def test_matching_expected_seq_writes() -> None:
    """그 사이 아무도 안 고쳤으면 정상적으로 쓴다 — CAS 가 정상 경로를 막으면 안 된다."""
    store = await get_profile_store()
    await store.set_summary(USER, "이전", NOW)
    observed = await store.get_summary(USER)
    assert observed is not None

    applied = await store.set_summary(USER, "이후", LATER, expected_seq=observed.seq)

    assert applied is True
    current = await store.get_summary(USER)
    assert current is not None and current.markdown == "이후"


async def test_expected_seq_zero_guards_the_first_write() -> None:
    """ "아직 요약이 없다"를 본 쓰기도 보호된다 — 0 을 "검사 안 함"으로 취급하면 안 된다.

    `expected_seq=0` 과 `expected_seq=None`(무조건 쓰기)은 다른 뜻이다. 0 을 falsy 로 흘려보내면
    첫 요약 경합에서만 CAS 가 조용히 꺼진다.
    """
    store = await get_profile_store()
    await store.set_summary(USER, "누군가 먼저 썼다", NOW)  # seq 1

    applied = await store.set_summary(USER, "없는 줄 알았다", LATER, expected_seq=0)

    assert applied is False
    current = await store.get_summary(USER)
    assert current is not None and current.markdown == "누군가 먼저 썼다"


async def test_unconditional_write_still_works() -> None:
    """`expected_seq` 를 안 주면 종전대로 무조건 쓴다 — 기존 호출부가 안 깨진다."""
    store = await get_profile_store()
    await store.set_summary(USER, "이전", NOW)

    applied = await store.set_summary(USER, "이후", LATER)

    assert applied is True
    current = await store.get_summary(USER)
    assert current is not None and current.markdown == "이후"


async def test_usable_flag_can_be_lowered_without_touching_the_text() -> None:
    """개인화 중지가 내리는 사용 표식 — 요약 본문은 건드리지 않는다 (REQ-PGRAPH-100).

    중지는 삭제가 아니다. 본문을 지우면 사용자가 개인화를 다시 켰을 때 되살릴 것이 없다.
    """
    store = await get_profile_store()
    await store.set_summary(USER, "소니 선호", NOW)

    await store.mark_summary_usable(USER, False)

    summary = await store.get_summary(USER)
    assert summary is not None
    assert summary.usable is False
    assert summary.markdown == "소니 선호"


async def test_lowering_usable_on_a_missing_summary_is_a_no_op() -> None:
    """요약이 없으면 조용히 넘어간다 — 중지 토글이 프로필 유무에 따라 실패하면 안 된다."""
    store = await get_profile_store()

    await store.mark_summary_usable(USER, False)

    assert await store.get_summary(USER) is None


# ─────────── consolidate 배선 — 실제 배치 경로에서 갭이 닫히는가 ───────────


class _EditingLLM:
    """LLM 왕복 **도중에** 사용자가 요약을 고치는 상황을 재현한다.

    `consolidate` 는 그래프 락을 놓은 뒤 이 호출을 하고, 돌아와서 `set_summary` 를 부른다.
    그 창이 정확히 #323 이 못 닫은 갭이다.
    """

    def __init__(self, user_edit: str) -> None:
        self._user_edit = user_edit
        self.calls = 0

    async def complete(self, **kwargs) -> str:
        self.calls += 1
        store = await get_profile_store()
        await store.set_summary(USER, self._user_edit, LATER)  # 사용자 편집이 끼어든다
        return "배치가 만든 요약"


async def test_consolidate_does_not_overwrite_an_edit_made_during_the_llm_call() -> None:
    """**#323 잔여가 닫혔는지를 배치 경로에서 직접 잰다.**

    되돌리면(=`expected_seq` 를 빼면) 배치 요약이 사용자 편집을 덮어써서 이 단언이 깨진다.
    """
    from app.agents.profile.builder import ConsolidationResult, consolidate
    from app.core.config import get_settings

    store = await get_profile_store()
    await store.add_fact(USER, "소니를 선호한다")
    await store.set_summary(USER, "배치가 읽을 시점의 요약", NOW)
    llm = _EditingLLM("사용자가 직접 고친 요약")

    result = await consolidate(USER, llm=llm, settings=get_settings())

    assert llm.calls == 1, "LLM 이 안 불렸으면 이 테스트는 아무것도 검증하지 않는다"
    assert result is ConsolidationResult.NO_WORK  # 덮지 않고 물러났다
    current = await store.get_summary(USER)
    assert current is not None
    assert current.markdown == "사용자가 직접 고친 요약"


async def test_consolidate_writes_normally_when_nothing_intervenes() -> None:
    """아무도 안 끼어들면 배치가 정상적으로 요약을 갱신한다 — CAS 가 정상 경로를 막으면 안 된다."""
    from app.agents.profile.builder import ConsolidationResult, consolidate
    from app.core.config import get_settings

    class _QuietLLM:
        async def complete(self, **kwargs) -> str:
            return "배치가 만든 요약"

    store = await get_profile_store()
    await store.add_fact(USER, "소니를 선호한다")
    await store.set_summary(USER, "이전 요약", NOW)

    result = await consolidate(USER, llm=_QuietLLM(), settings=get_settings())

    assert result is ConsolidationResult.UPDATED
    current = await store.get_summary(USER)
    assert current is not None and current.markdown == "배치가 만든 요약"
