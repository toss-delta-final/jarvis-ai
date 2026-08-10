"""개인화 중지 — **사용 중지** 집행 (SPEC-PROFILE-GRAPH-149 §6.6 REQ-PGRAPH-051, 이슈 #359).

중지하면 세 소비 표면이 프로필을 쓰지 않아야 한다:
  - rerank 프롬프트 주입 (회원 프롬프트가 **게스트와 바이트 동일**해진다 — #119 의 안전 속성)
  - 홈 랭킹(I-22)의 프로필 벡터 항 → `NO_PROFILE`
  - 마이페이지(§3.4) 마크다운 `null`

**이중 방어의 구조를 그대로 잰다**(REQ-PGRAPH-100): 1차는 캐시 없는 플래그 조회,
2차는 요약 항목의 `usable` 표식. 2차만 있으면 세 구멍이 난다 —
`test_summary_usable_alone_is_not_enough_*` 세 건이 그 구멍을 각각 고정한다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.reader import read_profile_summary
from app.agents.profile.store import get_profile_store, reset_profile_store


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


async def _seed_summary(user_id: str = "123") -> None:
    store = await get_profile_store()
    await store.set_summary(user_id, "# 취향\n- 소니 선호", "2026-08-01T00:00:00+00:00")


# ─────────── reader — 2차 방어(usable) ───────────


async def test_reader_returns_none_for_an_unusable_summary() -> None:
    """`usable=False` 요약은 없는 것처럼 읽힌다 — 소비처 세 곳이 이미 `None` 을 정상 처리한다.

    왕복이 0회인 것이 요점이다: `usable` 은 `get_summary` 가 이미 읽어 온 필드다.
    """
    await _seed_summary()
    store = await get_profile_store()
    await store.mark_summary_usable("123", False)

    assert await read_profile_summary("123") is None


async def test_reader_still_reads_a_usable_summary() -> None:
    await _seed_summary()

    summary = await read_profile_summary("123")

    assert summary is not None and summary["markdown"].startswith("# 취향")


# ─────────── set_summary — usable 승계 구멍 ───────────


async def test_set_summary_carries_usable_even_when_the_embedding_succeeds(monkeypatch) -> None:
    """배치가 요약을 다시 써도 중지가 풀리면 안 된다 (REQ-PGRAPH-063 과 같은 취지).

    승계 코드는 있었지만 `existing` 을 **조건부로만** 읽었다 —
    `if embedding is None or expected_seq is not None`. 즉 임베딩이 성공하고 CAS 를 안 쓰는
    호출에서는 `existing` 이 `None` 인 채로 `usable` 이 `True` 로 리셋된다. 지금 프로덕션이
    안전한 유일한 이유는 `builder.consolidate` 가 늘 `expected_seq=` 를 넘기기 때문이고,
    **호출자 한 명의 규율에 [HARD] 보장이 걸려 있는 상태**다.

    임베딩 성공을 강제하지 않으면 이 테스트는 구멍 경로를 **타지도 않고** 초록불이 된다 —
    유닛 환경은 `google_api_key` 가 없어 `_embed_summary` 가 늘 `None` 을 돌려주고, 그러면
    `existing` 을 읽는 분기로 새기 때문이다.
    """
    from app.agents.profile import store as store_mod

    async def _vector(_markdown: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(store_mod, "_embed_summary", _vector)

    await _seed_summary()
    store = await get_profile_store()
    await store.mark_summary_usable("123", False)

    # 임베딩이 성공하고 CAS 를 안 쓰는 경로 — 지금은 여기서 usable 이 True 로 리셋된다.
    await store.set_summary("123", "# 취향\n- 갱신", "2026-08-02T00:00:00+00:00")

    stored = await store.get_summary("123")
    assert stored is not None and stored.usable is False


# ─────────── 1차 방어가 없으면 나는 구멍 ───────────


async def test_summary_usable_alone_is_not_enough_when_no_summary_exists() -> None:
    """요약 행이 없으면 표식을 내릴 자리가 없다 — `mark_summary_usable` 이 조용히 no-op 이다.

    프로필이 아직 없는 회원이 먼저 끄면 2차 방어는 아무 데도 안 남는다. 그 뒤 배치가 요약을
    만들면 `usable=True` 로 태어나므로, **1차(플래그 조회)가 없으면 그 순간 개인화가 되살아난다.**
    """
    store = await get_profile_store()
    await store.mark_summary_usable("123", False)  # 요약 없음 — 조용히 넘어간다

    assert await store.get_summary("123") is None

    await graph_journal.set_personalization_flag(
        user_id=123, enabled=False, now="2026-08-10T00:00:00+00:00"
    )
    await _seed_summary()  # 배치가 처음 요약을 만든다

    stored = await store.get_summary("123")
    assert stored is not None
    assert stored.usable is True  # 2차 방어는 여기서 성립하지 않는다
    # → 그래서 소비 경로가 플래그를 직접 봐야 한다. 그 배선은 buyer/graph.py·home 쪽에서 잰다.
    assert await graph_journal.get_personalization_flag(user_id=123) is False
