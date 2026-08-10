"""클라이언트 IP 판별 + 진단 로그 (이슈 #134).

Cloudflare 엣지 → ALB → AI EC2 :8000 경로(2026-08-10 cloudflared 터널 제거 후 유일한
경로)에서 실제 클라이언트 IP 를 판별한다. `Cf-Connecting-IP` 는 Cloudflare 엣지가 항상
덮어쓰는 단일 값(홉 수 개념이 없다)이라 `X-Forwarded-For` 우측 홉 판독보다 우선한다.
`trust_forwarded_for=False` 면 어떤 헤더도 보지 않고 TCP peer 를 쓴다(설계 근거는 PR 본문).

이 모듈이 내는 `client_ip_probe` 진단 로그가 이슈 #134 의 핵심 산출물이다. `X-Forwarded-For`
에 실제 몇 개 원소가 쌓이는지 지금까지 검증된 적이 없었다(AI 가 XFF 를 로그로 남긴 적이
없어 `FORWARDED_FOR_TRUSTED_HOPS=2` 는 근거 없이 신뢰돼 왔다 — 2026-08-06 커밋 메시지의
"실측"은 관측 경로가 없던 시점의 추정이었다). `cfMatchIndexFromRight` 를 보면 배포 후 실제
운영 로그 한 줄만으로 그 값이 맞는지 확인할 수 있다.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import get_request_id
from app.core.logging import get_logger, log_structured, safe_fingerprint

logger = get_logger(__name__)

ClientIpSource = Literal["cf_header", "forwarded_for", "peer", "unknown"]


def _header_lines(headers: Any, name: str) -> list[str]:
    """헤더 값을 줄 단위로 얻는다.

    프록시는 기존 헤더에 append 하는 대신 같은 이름의 헤더를 **한 줄 더 추가**할 수 있다
    (Starlette `Headers.get` 은 이 경우 첫 줄만 돌려줘 홉 수를 잘못 센다) — 그래서
    `getlist` 로 전부 받는다. 단순 dict 등 `getlist` 가 없는 더미는 `get` 단일값으로 폴백한다.
    """
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        lines = cast("list[str]", getlist(name))
        return [v for v in lines if v]
    value = headers.get(name) if hasattr(headers, "get") else None
    return [value] if value else []


def _strip_bracket_port(raw: str) -> str:
    """`[2001:db8::1]:443` 처럼 오염된 원소의 대괄호·포트를 방어적으로 벗긴다.

    ALB 가 XFF 에 포트를 붙이지는 않지만, 원소가 오염됐을 때 조용히 잘못된 키를
    만들지 않도록 방어한다. IPv6 축약형은 콜론이 여러 개라 `host:port` 오판을 피하려
    콜론이 정확히 1개일 때만 포트로 취급한다.
    """
    value = raw.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host
    return value


def _normalize_ip(raw: str) -> str | None:
    """유효한 IP(v4/v6) 면 `ipaddress` 정규화 문자열을, 아니면 None."""
    try:
        return str(ipaddress.ip_address(_strip_bracket_port(raw)))
    except ValueError:
        return None


def _fingerprint_element(raw: str | None) -> str | None:
    """정규화 가능하면 정규화된 문자열로 지문을 낸다 — 같은 IP 는 표기가 달라도 같은 지문."""
    if raw is None:
        return None
    normalized = _normalize_ip(raw)
    return safe_fingerprint(normalized if normalized is not None else raw)


@dataclass(frozen=True)
class ClientIpResolution:
    """클라이언트 IP 판별 결과 + `client_ip_probe` 조립에 필요한 부수 정보."""

    ip: str
    source: ClientIpSource
    xff_present: bool
    xff_header_instances: int
    xff_elements: tuple[str, ...]  # 좌→우, 원문(비정규화)
    xff_valid: tuple[bool, ...]  # xff_elements 와 평행 — 각 원소가 유효 IP 인지
    cf_present: bool
    cf_raw: str | None  # 채택된(마지막 줄) 원문, 헤더 없으면 None
    cf_normalized: str | None
    peer_raw: str | None


def resolve_client_ip(request: Request) -> ClientIpResolution:
    """§1 우선순위(Cf-Connecting-IP → XFF 우측 신뢰 홉 → TCP peer)로 클라이언트 IP 를 판별한다.

    malformed 원소는 예외 없이 그 원소만 무효로 보고 다음 우선순위로 내려간다. 진단
    필드(xff_*/cf_*)는 `trust_forwarded_for` 와 무관하게 항상 채운다 — 신뢰를 켜기 전에도
    실제로 몇 홉이 도착하는지 관측할 수 있어야 하기 때문이다. 실제 채택(`ip`/`source`)만
    신뢰 플래그로 게이팅한다: 꺼져 있으면 어떤 헤더도 보지 않고 곧장 peer 를 쓴다.
    """
    settings = get_settings()
    headers = request.headers

    xff_lines = _header_lines(headers, "x-forwarded-for")
    xff_present = bool(xff_lines)
    combined = ",".join(xff_lines)
    xff_elements = tuple(p.strip() for p in combined.split(",") if p.strip())
    xff_valid = tuple(_normalize_ip(p) is not None for p in xff_elements)

    cf_header_name = settings.trusted_client_ip_header.strip().lower()
    # 빈 문자열이면 "CF 헤더 사용 안 함" — 우선순위 1단계를 건너뛴다(§2.4).
    cf_lines = _header_lines(headers, cf_header_name) if cf_header_name else []
    cf_present = bool(cf_lines)
    cf_raw = cf_lines[-1] if cf_lines else None  # 마지막 줄 = 엣지가 최종적으로 쓴 값
    cf_normalized = _normalize_ip(cf_raw) if cf_raw is not None else None

    peer_raw = request.client.host if request.client else None

    ip: str
    source: ClientIpSource
    if not settings.trust_forwarded_for:
        ip, source = (peer_raw, "peer") if peer_raw is not None else ("unknown", "unknown")
    elif cf_normalized is not None:
        ip, source = cf_normalized, "cf_header"
    else:
        hops = max(1, settings.forwarded_for_trusted_hops)
        candidate_ip = None
        if len(xff_elements) >= hops:
            candidate_ip = _normalize_ip(xff_elements[-hops])
        if candidate_ip is not None:
            ip, source = candidate_ip, "forwarded_for"
        elif peer_raw is not None:
            ip, source = peer_raw, "peer"
        else:
            ip, source = "unknown", "unknown"

    return ClientIpResolution(
        ip=ip,
        source=source,
        xff_present=xff_present,
        xff_header_instances=len(xff_lines),
        xff_elements=xff_elements,
        xff_valid=xff_valid,
        cf_present=cf_present,
        cf_raw=cf_raw,
        cf_normalized=cf_normalized,
        peer_raw=peer_raw,
    )


def emit_client_ip_probe(request: Request, resolution: ClientIpResolution, *, path: str) -> None:
    """`client_ip_probe` 진단 로그 1건을 낸다(§2.3). 원문 IP 는 절대 남기지 않는다.

    `client_ip_probe_enabled` 로 끌 수 있다. **실패 격리** — 조립·기록 중 예외가 요청을
    죽이면 안 된다는 원칙은 `reco_provenance.emit_recommendation_provenance` 관례를 그대로
    따른다(`except Exception` 삼키고 경고만, `CancelledError` 는 `BaseException` 이라 자연히
    전파된다).
    """
    settings = get_settings()
    if not settings.client_ip_probe_enabled:
        return
    try:
        cf_match_index_from_right: int | None = None
        if resolution.cf_normalized is not None:
            for offset, raw in enumerate(reversed(resolution.xff_elements), start=1):
                if _normalize_ip(raw) == resolution.cf_normalized:
                    cf_match_index_from_right = offset
                    break
        configured_hops = settings.forwarded_for_trusted_hops
        hop_mismatch = (
            cf_match_index_from_right is not None and cf_match_index_from_right != configured_hops
        )
        log_structured(
            logger,
            "client_ip_probe",
            requestId=get_request_id(request),
            path=path,
            xffPresent=resolution.xff_present,
            xffHeaderInstances=resolution.xff_header_instances,
            xffHopCount=len(resolution.xff_elements),
            xffHopFps=[_fingerprint_element(v) for v in resolution.xff_elements],
            xffHopValid=list(resolution.xff_valid),
            cfHeaderPresent=resolution.cf_present,
            cfIpFp=_fingerprint_element(resolution.cf_raw),
            peerFp=_fingerprint_element(resolution.peer_raw),
            cfMatchIndexFromRight=cf_match_index_from_right,
            configuredHops=configured_hops,
            trustEnabled=settings.trust_forwarded_for,
            selectedSource=resolution.source,
            selectedFp=_fingerprint_element(resolution.ip),
            hopMismatch=hop_mismatch,
        )
    except Exception:  # noqa: BLE001 - 진단 로그 실패가 요청을 죽이면 안 된다
        logger.warning("client_ip_probe emit 실패 code=CLIENT_IP_PROBE_EMIT_FAILED")
