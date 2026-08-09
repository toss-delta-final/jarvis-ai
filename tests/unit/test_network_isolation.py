"""유닛 테스트의 실제 TCP 차단 회귀 테스트."""

from __future__ import annotations

import socket

import pytest


def test_unit_tests_refuse_real_tcp_connections() -> None:
    """로컬 서비스가 살아 있어도 유닛 테스트는 TCP 연결을 만들지 않는다."""
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", 8080), timeout=0.1)
