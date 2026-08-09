"""그래프 저널 스키마 — 실 pg-profile 필요 (이슈 #358).

`docker compose up -d pg-profile` 로 컨테이너가 떠 있어야 통과한다. 기본 pytest 실행에서는
@pytest.mark.integration 으로 제외된다(pyproject.toml addopts).

여기서 재는 것은 **마이그레이션이 몇 번 돌아도 안전한가**와 **제약이 실제로 DB 에 걸렸는가**다.
앱은 기동할 때마다 `_ensure_schema` 를 부르고 인스턴스가 여럿이면 동시에 부른다 — 두 번째
호출에서 깨지면 배포가 첫 재기동에서 죽는다. 제약은 코드 단언이 아니라 DB 가 거부해야 한다.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents.profile import graph_journal

pytestmark = pytest.mark.integration


@pytest.fixture
def key() -> str:
    """테스트마다 새 파생 키 — 재실행이 이전 실행의 행과 겹치지 않게 한다."""
    return f"profile-graph-edgeUpdate:358:{uuid.uuid4().hex}:g42"


@pytest.fixture
async def pool():
    graph_journal.set_pool(None)
    opened = await graph_journal._get_pool()
    assert opened is not None, "실 pg 풀이 열리지 않았다 — pg-profile 이 떠 있는지 확인"
    yield opened
    await graph_journal.close_pool()


async def test_ensure_schema_is_idempotent(pool) -> None:
    """두 번 더 돌려도 예외가 없다 — 재기동·다중 인스턴스가 매번 지나는 경로다."""
    await graph_journal._ensure_schema(pool)
    await graph_journal._ensure_schema(pool)


async def test_all_three_tables_exist(pool) -> None:
    """SPEC §7.1 은 2개라고 적었지만 라벨 원문 격리 때문에 3개다(모듈 docstring 참조)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (
                [
                    "profile_graph_audit",
                    "profile_graph_idempotency",
                    "profile_personalization_state",
                ],
            ),
        )
        found = {row[0] for row in await cur.fetchall()}

    assert found == {
        "profile_graph_audit",
        "profile_graph_idempotency",
        "profile_personalization_state",
    }


async def test_audit_action_vocabulary_is_enforced_by_the_database(pool) -> None:
    """액션 어휘 4종은 DB 가 거부한다 — 애플리케이션 단언만으로는 우회 경로가 남는다.

    `edgeRestore` 는 #499 로 폐기됐다(복구 없음, 즉시 물리 삭제). 그 값이 다시 새어 들어오면
    여기서 잡힌다.
    """
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            with pytest.raises(Exception):
                await conn.execute(
                    "INSERT INTO profile_graph_audit "
                    "(request_id, actor_fp, action, graph_version_before, graph_version_after) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("req-1", "fp-1", "edgeRestore", "g1", "g2"),
                )


async def test_idempotency_status_vocabulary_is_enforced_by_the_database(pool) -> None:
    """원장 상태는 processing/completed 뿐 — 제3의 상태가 생기면 크래시 재개 판정이 흔들린다."""
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            with pytest.raises(Exception):
                await conn.execute(
                    "INSERT INTO profile_graph_idempotency "
                    "(derived_key, user_id, status) VALUES (%s, %s, %s)",
                    ("profile-graph-edgeUpdate:1:e_x:g1", 1, "pending"),
                )


# ── 멱등 원장 — 폴백 유닛(test_profile_graph_ledger.py)과 같은 계약을 실 pg 로 다시 잰다 ──


async def test_concurrent_claims_elect_exactly_one_owner(pool, key: str) -> None:
    """같은 키로 동시에 몰려도 실행권은 하나다 — 여기가 "부작용 1회"의 뿌리다.

    폴백은 단일 스레드 dict 라 이 성질이 공짜지만, 실 pg 에서는 `ON CONFLICT DO UPDATE ... WHERE`
    가 원자적이어야 성립한다. 유닛으로는 검증되지 않는 지점이다.
    """
    results = await asyncio.gather(
        *(graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60) for _ in range(10))
    )

    assert sum(1 for token in results if token) == 1


async def test_expired_lease_is_reclaimed_on_real_pg(pool, key: str) -> None:
    """크래시 잔재는 lease 가 실제로 만료된 뒤에만 재선점된다 (SPEC §8 크래시 행).

    시각 판정을 DB `now()` 에 맡기므로 여기서만 검증된다 — 폴백은 프로세스 단조 시계를 쓴다.
    """
    first = await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=0.01)
    assert first
    assert await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60) is None

    await asyncio.sleep(0.05)
    second = await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60)

    assert second and second != first
    assert await graph_journal.complete(key, first, {"graphVersion": "g43"}) is False
    assert await graph_journal.complete(key, second, {"graphVersion": "g43"}) is True


async def test_completed_entry_replays_payload_and_is_never_reclaimed(pool, key: str) -> None:
    """완료분은 lease 가 없어도 재선점되지 않고 payload 를 그대로 돌려준다."""
    token = await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=0.01)
    payload = {"graphVersion": "g43", "edgeIdAfter": "e_def", "merged": False}
    assert await graph_journal.complete(key, token, payload) is True

    await asyncio.sleep(0.05)
    assert await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60) is None

    hit = await graph_journal.lookup(key)
    assert hit is not None and hit.status == "completed"
    assert hit.response_payload == payload


async def test_release_removes_the_row_so_a_retry_can_proceed(pool, key: str) -> None:
    """해제한 요청은 행이 사라진다 — 상태를 안 바꾼 요청은 흔적을 남기지 않는다."""
    token = await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60)
    assert await graph_journal.release(key, token) is True

    assert await graph_journal.lookup(key) is None
    assert await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60)


async def test_ttl_expiry_turns_a_completed_entry_into_a_miss(pool, key: str) -> None:
    """TTL 이 지난 원장은 미스다 — 그 뒤 재전송은 CAS 로 판정된다(안전한 방향의 degrade)."""
    token = await graph_journal.claim(key, user_id=358, scope_id="e_abc", lease_s=60)
    await graph_journal.complete(key, token, {"graphVersion": "g43"})

    assert await graph_journal.lookup(key, ttl_h=0) is None
    assert await graph_journal.lookup(key) is not None


async def test_same_key_different_body_is_not_replayed_on_real_pg(pool, key: str) -> None:
    """파생 키에 본문이 안 들어가 생기는 구멍을 `request_fp` 가 막는다."""
    token = await graph_journal.claim(
        key, user_id=358, scope_id="e_abc", lease_s=60, request_fp="fp-avoids"
    )
    await graph_journal.complete(key, token, {"graphVersion": "g43"})

    assert await graph_journal.lookup(key, request_fp="fp-avoids") is not None
    with pytest.raises(graph_journal.LedgerRequestMismatch):
        await graph_journal.lookup(key, request_fp="fp-likes")
