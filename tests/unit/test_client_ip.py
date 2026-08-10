"""클라이언트 IP 판별 + `client_ip_probe` 진단 로그 단위 테스트 (이슈 #134).

`Cf-Connecting-IP` → `X-Forwarded-For` 우측 신뢰 홉 → TCP peer 우선순위(§1)와, 배포된
`FORWARDED_FOR_TRUSTED_HOPS` 가 실제로 맞는지 스스로 증명하는 `client_ip_probe` 로그(§2.3)를
검증한다. 기존 `tests/unit/test_infra.py::test_host_uses_rightmost_forwarded_for_when_trusted`
는 `ratelimit._host` 가 `resolve_client_ip` 로 위임한 뒤에도 그대로 통과해야 한다(변경하지 않음).
"""

from __future__ import annotations

import json
import logging
import types

import pytest
from starlette.datastructures import Headers

from app.core import client_ip
from app.core.client_ip import emit_client_ip_probe, resolve_client_ip
from app.core.config import get_settings


class _IpRequest:
    """resolve_client_ip/emit_client_ip_probe 단위 테스트용 최소 Request 더미.

    Starlette `Headers` 를 raw 튜플 리스트로 만들어 중복 헤더(여러 줄 XFF)를 재현한다.
    """

    def __init__(
        self,
        *,
        xff_lines: list[str] | None = None,
        cf_lines: list[str] | None = None,
        client_host: str | None = "10.0.0.1",
    ) -> None:
        raw: list[tuple[bytes, bytes]] = []
        for line in xff_lines or []:
            raw.append((b"x-forwarded-for", line.encode()))
        for line in cf_lines or []:
            raw.append((b"cf-connecting-ip", line.encode()))
        self.headers = Headers(raw=raw)
        self.client = types.SimpleNamespace(host=client_host) if client_host is not None else None
        self.state = types.SimpleNamespace()


@pytest.fixture(autouse=True)
def _reset_forwarded_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """각 테스트를 코드 기본값에서 시작시킨다 — 이전 테스트의 monkeypatch 잔류 방지."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", False)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
    monkeypatch.setattr(settings, "trusted_client_ip_header", "cf-connecting-ip")
    monkeypatch.setattr(settings, "client_ip_probe_enabled", True)
    monkeypatch.setattr(settings, "pii_hash_pepper", "test-pepper-134")


def _emit_and_capture(
    caplog: pytest.LogCaptureFixture, request: _IpRequest, resolution, path: str = "/chat"
) -> logging.LogRecord:
    caplog.set_level(logging.INFO, logger="app.core.client_ip")
    emit_client_ip_probe(request, resolution, path=path)
    records = [r for r in caplog.records if getattr(r, "event", None) == "client_ip_probe"]
    assert len(records) == 1
    return records[0]


# ─────────── 위조 방어 ───────────


def test_forged_prefix_ignored_when_hops_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """신뢰 홉 수(1)를 넘는 위조 XFF 앞부분이 무시되고 최우측 값만 채택된다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    req = _IpRequest(xff_lines=["9.9.9.9, 1.1.1.1, 203.0.113.7"])

    res = resolve_client_ip(req)

    assert res.ip == "203.0.113.7"
    assert res.source == "forwarded_for"


def test_hop_position_not_shifted_by_extra_forged_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """hops=2 에서 클라이언트가 위조 원소를 하나 더 앞에 붙여도 우측 기준 채택 위치는 그대로다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)

    base = resolve_client_ip(_IpRequest(xff_lines=["1.1.1.1, 203.0.113.7, 198.51.100.9"]))
    extended = resolve_client_ip(
        _IpRequest(xff_lines=["9.9.9.9, 1.1.1.1, 203.0.113.7, 198.51.100.9"])
    )

    assert base.ip == extended.ip == "203.0.113.7"


def test_trust_disabled_ignores_xff_and_cf_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """trust_forwarded_for=False 면 XFF·Cf-Connecting-IP 둘 다 보지 않고 peer 를 쓴다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", False)
    req = _IpRequest(xff_lines=["203.0.113.7"], cf_lines=["198.51.100.1"], client_host="10.0.0.1")

    res = resolve_client_ip(req)

    assert res.ip == "10.0.0.1"
    assert res.source == "peer"


def test_no_headers_falls_back_to_peer_or_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """헤더가 없으면 종전 동작(peer)으로, client 도 없으면 unknown 으로 떨어진다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)

    res = resolve_client_ip(_IpRequest(client_host="10.0.0.5"))
    assert res.ip == "10.0.0.5"
    assert res.source == "peer"

    res_no_client = resolve_client_ip(_IpRequest(client_host=None))
    assert res_no_client.ip == "unknown"
    assert res_no_client.source == "unknown"


# ─────────── 운영이 실제로 타는 설정 조합 (trust=True, hops=2) ───────────


def test_prod_hops2_selects_client_ip_from_two_element_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)

    res = resolve_client_ip(_IpRequest(xff_lines=["203.0.113.9, 172.16.0.1"]))

    assert res.ip == "203.0.113.9"
    assert res.source == "forwarded_for"


def test_prod_hops2_forged_prefix_does_not_shift_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)

    res = resolve_client_ip(_IpRequest(xff_lines=["9.9.9.9, 8.8.8.8, 203.0.113.9, 172.16.0.1"]))

    assert res.ip == "203.0.113.9"


def test_prod_hops2_single_element_falls_back_to_peer_not_forged_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)

    res = resolve_client_ip(_IpRequest(xff_lines=["9.9.9.9"], client_host="10.0.0.9"))

    assert res.ip == "10.0.0.9"
    assert res.source == "peer"


# ─────────── 우선순위·형식 ───────────


def test_cf_header_prioritized_over_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)

    res = resolve_client_ip(
        _IpRequest(xff_lines=["203.0.113.9, 172.16.0.1"], cf_lines=["198.51.100.5"])
    )

    assert res.ip == "198.51.100.5"
    assert res.source == "cf_header"


def test_malformed_cf_header_falls_through_to_xff_then_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)

    to_xff = resolve_client_ip(_IpRequest(xff_lines=["203.0.113.9"], cf_lines=["not-an-ip"]))
    assert to_xff.ip == "203.0.113.9"
    assert to_xff.source == "forwarded_for"

    to_peer = resolve_client_ip(
        _IpRequest(xff_lines=["also-not-an-ip"], cf_lines=["not-an-ip"], client_host="10.0.0.2")
    )
    assert to_peer.ip == "10.0.0.2"
    assert to_peer.source == "peer"


def test_ipv4_and_ipv6_both_accepted_short_and_full_forms_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)

    res_v4 = resolve_client_ip(_IpRequest(cf_lines=["203.0.113.9"]))
    assert res_v4.source == "cf_header"
    assert res_v4.ip == "203.0.113.9"

    res_v6_short = resolve_client_ip(_IpRequest(cf_lines=["2001:db8::1"]))
    res_v6_full = resolve_client_ip(
        _IpRequest(cf_lines=["2001:0db8:0000:0000:0000:0000:0000:0001"])
    )
    assert res_v6_short.source == res_v6_full.source == "cf_header"
    assert res_v6_short.ip == res_v6_full.ip
    assert client_ip._fingerprint_element(res_v6_short.ip) == client_ip._fingerprint_element(
        res_v6_full.ip
    )


def test_xff_count_below_hops_falls_back_to_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 3)

    res = resolve_client_ip(
        _IpRequest(xff_lines=["203.0.113.9, 172.16.0.1"], client_host="10.0.0.3")
    )

    assert res.ip == "10.0.0.3"
    assert res.source == "peer"


def test_empty_trusted_header_name_disables_cf_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "trusted_client_ip_header", "")
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)

    res = resolve_client_ip(_IpRequest(xff_lines=["203.0.113.9"], cf_lines=["198.51.100.5"]))

    assert res.ip == "203.0.113.9"
    assert res.source == "forwarded_for"


# ─────────── 진단 로그 client_ip_probe ───────────


@pytest.mark.parametrize(
    "elements",
    [["1.1.1.1"], ["1.1.1.1", "2.2.2.2"], ["1.1.1.1", "2.2.2.2", "3.3.3.3"]],
)
def test_probe_counts_xff_hops_exactly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, elements: list[str]
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
    req = _IpRequest(xff_lines=[", ".join(elements)])
    resolution = resolve_client_ip(req)

    record = _emit_and_capture(caplog, req, resolution)

    assert record.xffHopCount == len(elements)


def test_probe_cf_match_index_from_right(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)
    req = _IpRequest(xff_lines=["203.0.113.9, 172.16.0.1"], cf_lines=["203.0.113.9"])
    resolution = resolve_client_ip(req)

    record = _emit_and_capture(caplog, req, resolution)

    assert record.cfMatchIndexFromRight == 2
    assert record.hopMismatch is False


def test_probe_hop_mismatch_true_only_when_position_differs_from_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)  # 실제 위치(2)와 다르게 설정
    req = _IpRequest(xff_lines=["203.0.113.9, 172.16.0.1"], cf_lines=["203.0.113.9"])
    resolution = resolve_client_ip(req)

    record = _emit_and_capture(caplog, req, resolution)

    assert record.cfMatchIndexFromRight == 2
    assert record.configuredHops == 1
    assert record.hopMismatch is True


def test_probe_never_leaks_raw_ip_strings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """이 PR 의 안전 계약 — 로그 레코드를 문자열로 직렬화해도 원문 IP 가 등장하지 않는다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)
    raw_ips = ["203.0.113.9", "172.16.0.1", "198.51.100.77", "2001:db8::9"]
    req = _IpRequest(
        xff_lines=[f"{raw_ips[0]}, {raw_ips[1]}"],
        cf_lines=[raw_ips[2]],
        client_host=raw_ips[3],
    )
    resolution = resolve_client_ip(req)

    record = _emit_and_capture(caplog, req, resolution)

    haystack = record.getMessage() + json.dumps(record.__dict__, default=str)
    for raw in raw_ips:
        assert raw not in haystack


def test_probe_disabled_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "client_ip_probe_enabled", False)
    req = _IpRequest(xff_lines=["203.0.113.9"])
    resolution = resolve_client_ip(req)

    caplog.set_level(logging.INFO, logger="app.core.client_ip")
    emit_client_ip_probe(req, resolution, path="/chat")

    assert not any(getattr(r, "event", None) == "client_ip_probe" for r in caplog.records)


def test_probe_assembly_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """진단 로그 조립이 실패해도(safe_fingerprint 예외) 요청 처리에 영향을 주지 않는다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)

    def _boom(_value: str) -> str:
        raise RuntimeError("intentional test failure")

    monkeypatch.setattr(client_ip, "safe_fingerprint", _boom)
    req = _IpRequest(xff_lines=["203.0.113.9"])
    resolution = resolve_client_ip(req)

    emit_client_ip_probe(req, resolution, path="/chat")  # 예외 없이 반환되면 통과


# ─────────── 여러 줄 XFF ───────────


def test_multiline_xff_headers_combined_in_arrival_order(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)

    req_multiline = _IpRequest(xff_lines=["9.9.9.9, 203.0.113.9", "172.16.0.1"])
    resolution = resolve_client_ip(req_multiline)

    assert resolution.xff_header_instances == 2
    assert resolution.ip == "203.0.113.9"

    record = _emit_and_capture(caplog, req_multiline, resolution)
    assert record.xffHopCount == 3
    assert record.xffHeaderInstances == 2

    # 한 줄로 도착하면 종전 동작과 완전히 같아야 한다.
    req_single = _IpRequest(xff_lines=["9.9.9.9, 203.0.113.9, 172.16.0.1"])
    resolution_single = resolve_client_ip(req_single)
    assert resolution_single.ip == resolution.ip
    assert resolution_single.xff_header_instances == 1


# ─────────── 미들웨어 배선 (rate_limit_middleware 가 실제로 emit_client_ip_probe 를 부르는지) ───────────


def test_rate_limit_middleware_emits_probe_for_limited_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`POST /chat` 이 실제 앱 미들웨어를 통과하면 `client_ip_probe` 가 정확히 1건 나온다.

    `ratelimit.py` 의 `emit_client_ip_probe(...)` 호출을 지워도 `resolve_client_ip`/
    `emit_client_ip_probe` 단위 테스트 34건은 전부 초록이었다 — 이 테스트가 배선 자체를 잰다.
    """
    from fastapi.testclient import TestClient

    from app.core.ratelimit import reset_limiter
    from app.main import app

    reset_limiter()
    caplog.set_level(logging.INFO, logger="app.core.client_ip")
    client = TestClient(app)

    client.post(
        "/chat",
        json={"sessionId": "probe-wiring-sess", "threadId": "probe-wiring-thread", "message": "m"},
    )

    records = [r for r in caplog.records if getattr(r, "event", None) == "client_ip_probe"]
    assert len(records) == 1
    assert records[0].path == "/chat"
    reset_limiter()


def test_rate_limit_middleware_skips_probe_for_unlimited_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """레이트 리밋 대상이 아닌 경로(`GET /health`)에는 `client_ip_probe` 가 나오지 않는다."""
    from fastapi.testclient import TestClient

    from app.main import app

    caplog.set_level(logging.INFO, logger="app.core.client_ip")
    client = TestClient(app)

    client.get("/health")

    records = [r for r in caplog.records if getattr(r, "event", None) == "client_ip_probe"]
    assert records == []
