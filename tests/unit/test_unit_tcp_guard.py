"""유닛 TCP 격리 가드의 경계 — 실 TCP 는 막고 asyncio self-pipe 는 통과시킨다.

`tests/unit/conftest.py` 의 `_refuse_live_tcp` 는 "로컬 Spring BE 가 8080 에 떠 있어도 붙지
마라"를 위해 `socket.socket` 을 전역 패치해 AF_INET/AF_INET6 `connect` 를 거부한다. 그런데
**Windows 에는 AF_UNIX socketpair 가 없어** CPython 이 127.0.0.1 리스닝 소켓 + `connect()` 로
흉내내고, asyncio 이벤트 루프는 생성 시 self-pipe 를 그 socketpair 로 만든다. 그래서 가드가
socketpair 까지 막으면 **이벤트 루프 자체가 안 떠서 async 테스트가 전멸**한다.

리눅스·macOS 는 socketpair 가 AF_UNIX 네이티브라 `connect()` 를 타지 않아 이 경로가 없다 —
CI(ubuntu-latest)는 초록인데 Windows 로컬만 죽는 형태라 CI 로는 영영 안 잡힌다. 그래서
"가드가 여전히 실 TCP 를 막는가"와 "그러면서 이벤트 루프가 뜨는가"를 **둘 다** 고정한다.
한쪽만 재면 되돌림을 못 잡는다.
"""

from __future__ import annotations

import asyncio
import socket

import pytest


def test_socketpair_survives_the_guard() -> None:
    """self-pipe 재료인 socketpair 는 가드를 통과해야 한다 — 실 TCP 가 아니다."""
    left, right = socket.socketpair()
    try:
        left.send(b"ping")
        assert right.recv(4) == b"ping"
    finally:
        left.close()
        right.close()


def test_event_loop_can_be_created_under_the_guard() -> None:
    """이벤트 루프 생성은 self-pipe 를 만든다 — 여기가 Windows 에서 전멸하던 지점이다."""
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0)) is None
    finally:
        loop.close()


async def test_async_tests_can_run_under_the_guard() -> None:
    """async 테스트가 실제로 돈다 — 스위트 4천여 건이 딛고 선 전제다."""
    await asyncio.sleep(0)


def test_live_tcp_is_still_refused() -> None:
    """가드의 본래 목적은 그대로다 — 로컬 서비스에 붙으면 CI 와 같은 실패로 떨어진다."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError, match="must not open live TCP"):
            sock.connect(("127.0.0.1", 8080))
    finally:
        sock.close()


def test_live_tcp_is_refused_for_connect_ex_too() -> None:
    """`connect_ex` 는 예외 대신 errno 를 돌려주는 별도 진입점이라 따로 막아야 한다."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError, match="must not open live TCP"):
            sock.connect_ex(("127.0.0.1", 8080))
    finally:
        sock.close()
