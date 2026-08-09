"""이슈 #476 — 프로세스 로컬 상태에서의 워커 증설 방지 장치."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import main as main_mod
from app.core.config import Settings
from app.core.stream import registry_is_process_local

_WORKER_SETTINGS = re.compile(r"--workers|WEB_CONCURRENCY")


def test_dockerfile_worker_configuration_requires_shared_registry() -> None:
    """워커 다중화를 켜려면 같은 Dockerfile 이 공유 백엔드를 함께 설정해야 한다.

    "마커 상수가 True 면 금지" 로는 부족하다 — 이제 프로세스 로컬 여부가
    `STREAM_REGISTRY_BACKEND` 에서 파생되므로, Dockerfile 이 워커를 켜면서 백엔드를 안 켜는
    조합이 정확히 금지 대상이다. 백엔드가 무엇이든 이 테스트는 스킵되지 않는다.
    """
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    command = dockerfile.read_text(encoding="utf-8")
    if not _WORKER_SETTINGS.search(command):
        return
    assert "STREAM_REGISTRY_BACKEND=shared" in command, (
        "프로세스 로컬 ActiveStreamRegistry에서는 워커 다중화가 §2.9(a) 409 가드를 "
        "무력화한다. Dockerfile에서 워커를 켜려면 STREAM_REGISTRY_BACKEND=shared를 함께 "
        "설정하고, docs/specs/OPS-SCALEOUT-476.md의 나머지 선행조건도 충족하라."
    )


def test_registry_locality_is_derived_from_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`registry_is_process_local()` 은 하드코딩이 아니라 설정에서 파생된다."""
    from app.core import stream_registry as registry_mod

    def _use(backend: str) -> None:
        settings = Settings(stream_registry_backend=backend)
        monkeypatch.setattr(registry_mod, "get_settings", lambda: settings)

    _use("memory")
    assert registry_is_process_local() is True
    _use("shared")
    assert registry_is_process_local() is False


def test_shipping_default_backend_is_the_process_local_registry() -> None:
    """출하 기본값은 기존 인메모리 — 워커 1개 배포에서 동작이 바뀌지 않는다(D4)."""
    assert Settings().stream_registry_backend == "memory"


@pytest.mark.parametrize("value", ["2", "3"])
def test_lifespan_warns_for_multiple_web_workers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", value)

    with caplog.at_level("WARNING"):
        main_mod._warn_process_local_registry_workers()

    assert "WEB_CONCURRENCY" in caplog.text
    assert "docs/specs/OPS-SCALEOUT-476.md" in caplog.text


def test_lifespan_does_not_warn_when_the_registry_is_shared(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """공유 백엔드에서는 워커 다중화 경고가 자동으로 사라진다."""
    from app.core import stream_registry as registry_mod

    settings = Settings(stream_registry_backend="shared")
    monkeypatch.setattr(registry_mod, "get_settings", lambda: settings)
    monkeypatch.setenv("WEB_CONCURRENCY", "3")

    with caplog.at_level("WARNING"):
        main_mod._warn_process_local_registry_workers()

    assert "WEB_CONCURRENCY" not in caplog.text


@pytest.mark.parametrize("value", [None, "0", "1", "not-an-int"])
def test_lifespan_does_not_warn_for_single_or_invalid_web_worker_value(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", value)

    with caplog.at_level("WARNING"):
        main_mod._warn_process_local_registry_workers()

    assert "WEB_CONCURRENCY" not in caplog.text
