"""멱등 원장 — claim · 재생 · 해제 (#358, REQ-PGRAPH-043).

여기서 고정하는 계약은 넷이다.

1. **claim 은 한 번만 성공한다** — 동시 재전송 중 하나만 실제로 실행한다.
2. **완료된 요청은 재실행하지 않고 최초 응답을 재생한다** — 부작용은 1회다.
3. **해제(release)한 요청은 흔적을 남기지 않는다** — `404`·`409`·no-op 은 상태를 바꾸지 않았으므로
   감사 행도 원장 행도 남으면 안 된다(REQ-PGRAPH-080).
4. **같은 키·다른 본문은 재생하지 않는다** — 파생 키에 본문이 안 들어가 생기는 구멍을
   `request_fp` 로 막는다.

폴백(InMemory)과 실 pg 가 **같은 계약**을 지켜야 한다 — dev 에서 통과하고 운영에서 갈리면
그게 제일 늦게 발견된다. 그래서 여기 유닛은 폴백 경로를, `tests/integration/test_pg_graph_journal.py`
는 같은 시나리오를 실 pg 로 다시 잰다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal

KEY = "profile-graph-edgeUpdate:123:e_abc:g42"


@pytest.fixture(autouse=True)
def _reset_journal():
    graph_journal.reset()
    yield
    graph_journal.reset()


async def test_first_claim_wins_and_second_gets_nothing() -> None:
    """진행 중인 요청은 재선점되지 않는다 — 동시 재전송이 부작용을 두 번 내면 안 된다."""
    token = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)
    assert token

    assert await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60) is None


async def test_completed_request_replays_the_first_response() -> None:
    """완료된 키는 재실행 없이 최초 payload 를 돌려준다 (REQ-PGRAPH-043)."""
    token = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)
    payload = {"graphVersion": "g43", "edgeIdAfter": "e_def", "merged": False}
    assert await graph_journal.complete(KEY, token, payload) is True

    hit = await graph_journal.lookup(KEY)

    assert hit is not None
    assert hit.status == "completed"
    assert hit.response_payload == payload
    # 완료된 행은 재선점 대상이 아니다 — lease 가 만료돼도 다시 실행되면 부작용이 2회다.
    assert await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60) is None


async def test_released_claim_leaves_no_trace_and_can_be_retried() -> None:
    """해제한 요청은 원장에서 사라진다 — 상태를 안 바꾼 요청은 기록도 남기지 않는다.

    저널을 **선행** 기록하는 설계라 `404`·`409`·no-op 판정이 claim 뒤에 온다. 그때 행을
    남겨두면 (a) REQ-PGRAPH-080("상태를 바꾸지 않는 요청은 감사 행을 남기지 않는다")을 어기고
    (b) 조건이 바뀐 뒤의 정당한 재시도가 "진행 중"으로 막힌다.
    """
    token = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)
    assert await graph_journal.release(KEY, token) is True

    assert await graph_journal.lookup(KEY) is None
    assert await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)


async def test_expired_lease_is_reclaimed_but_completed_is_not() -> None:
    """크래시 잔재(`processing` + lease 만료)는 재선점되고 완료분은 안 된다.

    이게 "저널 선행 기록이 크래시 복구의 근거"(SPEC §7.2)의 실제 동작이다.
    """
    first = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=0)
    assert first

    second = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)
    assert second and second != first

    # 재선점 뒤에는 옛 토큰이 무효다 — 늦게 깨어난 원래 주인이 남의 작업을 완료 처리하면 안 된다.
    assert await graph_journal.complete(KEY, first, {"graphVersion": "g43"}) is False
    assert await graph_journal.complete(KEY, second, {"graphVersion": "g43"}) is True


async def test_stale_token_cannot_release_someone_elses_claim() -> None:
    """재선점된 뒤 옛 토큰의 해제는 실패한다 — 남의 진행 중 작업을 지우면 안 된다."""
    first = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=0)
    second = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)

    assert await graph_journal.release(KEY, first) is False
    assert await graph_journal.lookup(KEY) is not None
    assert await graph_journal.release(KEY, second) is True


async def test_same_key_different_body_is_not_replayed() -> None:
    """파생 키가 같아도 본문이 다르면 재생하지 않는다.

    파생 키는 `{action}:{userId}:{scopeId}:{ifMatch}` 라 **본문이 안 들어간다**. 스테일한
    `If-Match` 를 든 다른 요청(예: 같은 edge 에 `predicate` 만 다르게)이 같은 키를 만들 수 있고,
    그때 최초 응답을 재생하면 **호출자가 보내지 않은 변경의 결과**를 성공으로 받는다.
    `request_fp` 가 다르면 재생 대신 충돌로 떨어뜨린다.
    """
    token = await graph_journal.claim(
        KEY, user_id=123, scope_id="e_abc", lease_s=60, request_fp="fp-avoids"
    )
    await graph_journal.complete(KEY, token, {"graphVersion": "g43"})

    assert await graph_journal.lookup(KEY, request_fp="fp-avoids") is not None
    with pytest.raises(graph_journal.LedgerRequestMismatch):
        await graph_journal.lookup(KEY, request_fp="fp-likes")


async def test_lookup_misses_after_ttl_expiry() -> None:
    """TTL 이 지난 원장은 미스다 — 그 뒤 재전송은 CAS 로 판정되고 최악 `409` 다.

    보존을 무한으로 두면 감사보다 오래 사는 원장이 생겨 REQ-PGRAPH-044 가 깨진다.
    """
    token = await graph_journal.claim(KEY, user_id=123, scope_id="e_abc", lease_s=60)
    await graph_journal.complete(KEY, token, {"graphVersion": "g43"})

    assert await graph_journal.lookup(KEY, ttl_h=0) is None
