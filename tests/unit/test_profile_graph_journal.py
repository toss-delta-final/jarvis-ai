"""그래프 저널 모듈의 연결 풀 수명주기 (#358).

여기서 고정하는 것은 **도메인 로직이 아니라 수명주기 규약**이다 — 운영에서 폴백을 금지하고,
dev 에서는 경고 1회 뒤 InMemory 로 내려가며, 테스트 격리(`reset()`)가 실 pg 를 건드리지 않는다.
`processed_events` 와 같은 규약이라 어긋나면 두 모듈이 서로 다른 실패 모드를 갖게 된다.

**운영 폴백 금지가 이 모듈에서 특히 중요하다** — 멱등 원장이 InMemory 로 내려가면 프로세스
재시작마다 재전송 판정이 증발해 "부작용 1회" 보장이 조용히 깨진다(감사 행도 같이 사라진다).
`processed_events` 가 같은 이유로 `auth_mode == "jwks"` 에서 raise 한다.
"""

from __future__ import annotations

import logging

import pytest

from app.agents.profile import graph_journal
from app.core.config import Settings


def _settings(*, auth_mode: str = "dev", dsn: str | None = None) -> Settings:
    """실 pg 가 없는 유닛 환경에서 폴백 분기를 재현할 설정.

    `_env_file=None` 으로 앰비언트 `.env` 를 끊는다 — 안 그러면 로컬에 실 DSN 이 있는 사람과
    없는 사람의 결과가 갈린다. 포트는 닫힌 것을 쓴다(연결 실패를 결정적으로 만든다).
    """
    overrides: dict[str, object] = {
        "profile_db_url": dsn or "postgresql://x:x@127.0.0.1:1/none",
        "state_store_connect_timeout_s": 0.05,
        "auth_mode": auth_mode,
    }
    if auth_mode == "jwks":
        # 운영 모드 validator 가 요구하는 최소 조합 — 여기 값들은 본 테스트의 관심사가 아니다.
        overrides |= {
            "pii_hash_pepper": "test-pepper",
            "internal_api_token": "test-token",
            "jwks_url": "https://example.invalid/jwks.json",
            "google_api_key": "test-key",
        }
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def _reset_journal():
    graph_journal.reset()
    yield
    graph_journal.reset()


async def test_reset_installs_inmemory_fallback_without_touching_pg() -> None:
    """`reset()` 직후에는 연결 시도 없이 즉시 폴백이다 — 테스트가 실 pg 상태에 얽히면 안 된다."""
    assert await graph_journal._get_pool() is None


async def test_production_refuses_the_inmemory_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """운영(jwks)에서 연결이 실패하면 **폴백하지 않고 죽는다**.

    조용히 InMemory 로 내려가면 재시작마다 멱등 원장이 증발해 재전송이 부작용을 두 번 낸다 —
    실패를 감추는 것보다 기동에서 드러나는 편이 낫다(`processed_events` 와 같은 규약).
    """
    graph_journal.set_pool(None)
    monkeypatch.setattr(graph_journal, "get_settings", lambda: _settings(auth_mode="jwks"))

    with pytest.raises(Exception) as excinfo:
        await graph_journal._get_pool()

    assert not isinstance(excinfo.value, AssertionError)


async def test_dev_falls_back_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """dev 는 폴백하되 **경고는 한 번만** — 매 호출 경고하면 로그가 그걸로 덮인다."""
    graph_journal.set_pool(None)
    monkeypatch.setattr(graph_journal, "get_settings", lambda: _settings())

    with caplog.at_level(logging.WARNING, logger=graph_journal.__name__):
        assert await graph_journal._get_pool() is None
        graph_journal.set_pool(None)
        assert await graph_journal._get_pool() is None

    warnings = [r for r in caplog.records if "graph_journal" in r.getMessage()]
    assert len(warnings) == 1


async def test_close_pool_is_safe_when_nothing_was_opened() -> None:
    """teardown 훅은 풀이 없어도 조용히 통과해야 한다 — 매 테스트가 부르는 경로다."""
    await graph_journal.close_pool()
