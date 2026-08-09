"""그래프 저널 스키마 — 실 pg-profile 필요 (이슈 #358).

`docker compose up -d pg-profile` 로 컨테이너가 떠 있어야 통과한다. 기본 pytest 실행에서는
@pytest.mark.integration 으로 제외된다(pyproject.toml addopts).

여기서 재는 것은 **마이그레이션이 몇 번 돌아도 안전한가**와 **제약이 실제로 DB 에 걸렸는가**다.
앱은 기동할 때마다 `_ensure_schema` 를 부르고 인스턴스가 여럿이면 동시에 부른다 — 두 번째
호출에서 깨지면 배포가 첫 재기동에서 죽는다. 제약은 코드 단언이 아니라 DB 가 거부해야 한다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal

pytestmark = pytest.mark.integration


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
