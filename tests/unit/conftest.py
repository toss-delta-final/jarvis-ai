"""유닛 테스트 공통 픽스처 — 판매자 영속 백엔드를 InMemory 로 격리한다.

4-2(hitl checkpointer)·4-3(history store)은 미주입 시 pg-profile 접속을 시도한다
(실패 시 dev 폴백). 유닛 테스트는 환경(PG 가동 여부)에 절대 의존하면 안 되므로
전 테스트에 InMemory 백엔드를 자동 주입하고 종료 시 초기화한다 — 로컬에 PG 가
떠 있어도 유닛 테스트가 실 DB 에 쓰는 사고를 구조로 차단한다.
"""

from __future__ import annotations

import socket

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.agents.seller import history, hitl


_REAL_SOCKET = socket.socket


class _TcpRefusingSocket(_REAL_SOCKET):
    """TCP `connect`를 CI의 서비스 미기동 상태와 같은 실패로 바꾼다."""

    def _refuse_tcp(self) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise ConnectionRefusedError("unit tests must not open live TCP connections")

    def connect(self, address):
        self._refuse_tcp()
        return super().connect(address)

    def connect_ex(self, address):
        self._refuse_tcp()
        return super().connect_ex(address)


@pytest.fixture(autouse=True)
def _refuse_live_tcp(monkeypatch: pytest.MonkeyPatch):
    """유닛 테스트의 실제 TCP를 거부해 로컬 서비스 상태를 결과에서 제거한다.

    로컬 Spring BE가 8080에서 실행 중일 때 `.env` 그대로는 재구매 테스트가 실패했지만,
    INTERNAL_API_TOKEN을 비우거나 죽은 포트로 향하게 하면 모두 통과했고 EXPOSE_MAX 변경은
    효과가 없었다. 따라서 httpx/anyio가 생성하는 TCP 소켓만 ConnectionRefusedError로
    거부해, CI의 서비스 미기동 상태와 같은 httpx.ConnectError degrade 경로를 보장한다.
    """
    monkeypatch.setattr(socket, "socket", _TcpRefusingSocket)


@pytest.fixture(autouse=True)
def _isolate_seller_persistence():
    hitl.set_checkpointer(InMemorySaver())
    history.set_store(InMemoryStore())
    yield
    hitl.set_checkpointer(None)
    history.set_store(None)
