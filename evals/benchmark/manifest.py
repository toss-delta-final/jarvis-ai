"""재현 가능한 벤치마크 실행 환경 manifest를 수집한다."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import Settings

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)=\S+"),
    re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^@\s/]+@"),
)


def unknown(reason: str) -> dict[str, Any]:
    """0과 구분되는 명시적 unknown 값이다."""
    return {"value": None, "unknown_reason": reason}


def _git(repo: Path) -> dict[str, Any]:
    """현재 SHA와 dirty 여부를 수집하고 실패를 unknown으로 보존한다."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return {"sha": sha, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return unknown("git_unavailable")


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def redact_url_userinfo(value: str) -> str:
    """URL userinfo를 산출물에 남기지 않고 호스트·경로만 보존한다."""
    parts = urlsplit(value)
    if parts.username is None and parts.password is None:
        return value
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return urlunsplit(
        (parts.scheme, f"[REDACTED]@{hostname}{port}", parts.path, parts.query, parts.fragment)
    )


def collect_manifest(
    *,
    repo: Path,
    settings: Settings,
    started_at_utc: str,
    ended_at_utc: str | None,
    command: str,
    base_url: str,
    target_label: str,
    auth_mode_observed: str,
    client_region: str | None,
    instance_type: str | None,
    image: str | None,
    dependency_notes: list[str],
    sample_size_rationale: str | None,
    model_ids: set[str] | None,
) -> dict[str, Any]:
    """환경 전체를 덤프하지 않고 허용된 재현 정보만 수집한다."""
    lock = repo / "uv.lock"
    return {
        "client_region": client_region or unknown("not_provided"),
        "client_runtime": {
            "platform": platform.platform(),
            "python": sys.version,
            "httpx": httpx.__version__,
        },
        "instance_type": instance_type or unknown("not_provided"),
        "image": image or unknown("not_provided"),
        "git_sha": _git(repo),
        "lockfile_sha": (
            hashlib.sha256(lock.read_bytes()).hexdigest()
            if lock.exists()
            else unknown("uv_lock_missing")
        ),
        "dependency_ids": {name: _version(name) for name in ("httpx", "fastapi", "pydantic")},
        "model_ids": sorted(model_ids)
        if model_ids is not None
        else unknown("server_join_unavailable"),
        "price_table": {
            "input_per_1k": settings.model_price_in_per_1k,
            "output_per_1k": settings.model_price_out_per_1k,
        },
        "started_at_utc": started_at_utc,
        "ended_at_utc": ended_at_utc,
        "command": command,
        "target": {
            "base_url": redact_url_userinfo(base_url),
            "target_label": target_label,
            "auth_mode_observed": auth_mode_observed,
        },
        "dependency_conditions": dependency_notes,
        "sample_size_rationale": sample_size_rationale,
        "bootstrap": {
            "seed": settings.benchmark_bootstrap_seed,
            "resamples": settings.benchmark_bootstrap_resamples,
            "confidence": settings.benchmark_bootstrap_confidence,
        },
    }


def assert_secret_free(value: object, secret_values: list[str]) -> None:
    """알려진 값과 흔한 시크릿 형태가 산출물에 포함되면 생성을 거부한다."""
    text = json.dumps(value, ensure_ascii=False)
    leaked = [secret for secret in secret_values if secret and secret in text]
    if leaked or any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise ValueError("secret value would be written to benchmark artifacts")
