"""유닛 테스트 공통 픽스처 — 판매자 영속 백엔드를 InMemory 로 격리한다.

4-2(hitl checkpointer)·4-3(history store)·분석 대상 자동 등록은 미주입 시 pg-profile 접속을 시도한다
(실패 시 dev 폴백). 유닛 테스트는 환경(PG 가동 여부)에 절대 의존하면 안 되므로
전 테스트에 InMemory 백엔드를 자동 주입하고 종료 시 초기화한다 — 로컬에 PG 가
떠 있어도 유닛 테스트가 실 DB 에 쓰는 사고를 구조로 차단한다.
"""

from __future__ import annotations

import socket
import threading

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.agents.seller import history, hitl


_REAL_SOCKET = socket.socket
_REAL_SOCKETPAIR = socket.socketpair

# socketpair 내부에서만 참이 되는 스레드 로컬 플래그. **스레드 로컬이어야 한다** — 전역이면
# socketpair 가 도는 찰나에 다른 스레드가 여는 실 TCP 까지 통과해 가드에 구멍이 난다.
_socketpair_scope = threading.local()


def _in_socketpair() -> bool:
    return getattr(_socketpair_scope, "active", False)


class _TcpRefusingSocket(_REAL_SOCKET):
    """TCP `connect`를 CI의 서비스 미기동 상태와 같은 실패로 바꾼다."""

    def _refuse_tcp(self) -> None:
        # socketpair 는 실 TCP 가 아니라 프로세스 내부 파이프다 — 아래 `_guarded_socketpair` 주석 참조.
        if _in_socketpair():
            return
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise ConnectionRefusedError("unit tests must not open live TCP connections")

    def connect(self, address):
        self._refuse_tcp()
        return super().connect(address)

    def connect_ex(self, address):
        self._refuse_tcp()
        return super().connect_ex(address)


def _guarded_socketpair(*args, **kwargs):
    """`socket.socketpair()` 를 가드에서 제외한다 — 이게 없으면 **Windows 에서 스위트가 전멸한다**.

    Windows 에는 AF_UNIX socketpair 가 없어 CPython 이 127.0.0.1 리스닝 소켓 + `connect()` 로
    흉내낸다(리눅스·macOS 는 커널 AF_UNIX 라 `connect()` 자체를 타지 않는다). 그런데 asyncio 는
    이벤트 루프를 만들 때 self-pipe 를 socketpair 로 만들므로, AF_INET `connect` 를 통째로 막으면
    **이벤트 루프 생성이 실패해 async 테스트가 전부 죽는다**(증상: 루프 teardown 에서
    `'ProactorEventLoop' object has no attribute '_ssock'` — self-pipe 를 못 만들어 `_ssock` 이
    설정되지 않은 것). 실측 2026-08-09: 이 예외가 없으면 유닛 556건 실패 / 있으면 정상.

    **CI(ubuntu-latest)로는 영영 안 잡힌다** — 리눅스에는 이 경로가 없어 항상 초록이다.
    그래서 `test_unit_tcp_guard.py` 가 "실 TCP 는 여전히 막히는가"와 "이벤트 루프가 뜨는가"를
    둘 다 고정한다. 한쪽만 재면 이 구멍이나 저 회귀 중 하나를 놓친다.

    예외 범위는 socketpair 호출 구간뿐이다 — httpx/anyio 가 여는 실 TCP 는 그대로 거부된다.
    """
    _socketpair_scope.active = True
    try:
        return _REAL_SOCKETPAIR(*args, **kwargs)
    finally:
        _socketpair_scope.active = False


@pytest.fixture(autouse=True)
def _refuse_live_tcp(monkeypatch: pytest.MonkeyPatch):
    """유닛 테스트의 실제 TCP를 거부해 로컬 서비스 상태를 결과에서 제거한다.

    로컬 Spring BE가 8080에서 실행 중일 때 `.env` 그대로는 재구매 테스트가 실패했지만,
    INTERNAL_API_TOKEN을 비우거나 죽은 포트로 향하게 하면 모두 통과했고 EXPOSE_MAX 변경은
    효과가 없었다. 따라서 httpx/anyio가 생성하는 TCP 소켓만 ConnectionRefusedError로
    거부해, CI의 서비스 미기동 상태와 같은 httpx.ConnectError degrade 경로를 보장한다.

    **`socket.socketpair` 도 함께 패치한다** — Windows 에서 그것이 AF_INET `connect` 로
    구현돼 asyncio self-pipe 가 이 가드에 걸리기 때문이다(`_guarded_socketpair` 참조).
    """
    monkeypatch.setattr(socket, "socket", _TcpRefusingSocket)
    monkeypatch.setattr(socket, "socketpair", _guarded_socketpair)


@pytest.fixture(autouse=True)
def _isolate_seller_persistence(monkeypatch: pytest.MonkeyPatch):
    hitl.set_checkpointer(InMemorySaver())
    history.set_store(InMemoryStore())
    monkeypatch.setattr("app.api.seller.note_seller_seen", lambda _context: None)
    yield
    hitl.set_checkpointer(None)
    history.set_store(None)
