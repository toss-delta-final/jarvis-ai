"""이슈 #476 — 프로세스 로컬 상태에서의 워커 증설 방지 장치."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import main as main_mod
from app.core.stream import REGISTRY_IS_PROCESS_LOCAL


def test_process_local_registry_blocks_worker_configuration() -> None:
    if not REGISTRY_IS_PROCESS_LOCAL:
        pytest.skip("공유 레지스트리 전환 후에는 Dockerfile 워커 가드를 완화한다")

    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    command = dockerfile.read_text(encoding="utf-8")
    assert "--workers" not in command and "WEB_CONCURRENCY" not in command, (
        "프로세스 로컬 ActiveStreamRegistry에서는 워커 다중화가 §2.9(a) 409 가드를 "
        "무력화한다. docs/specs/OPS-SCALEOUT-476.md의 선행조건을 충족하고 "
        "REGISTRY_IS_PROCESS_LOCAL을 False로 바꾼 뒤에만 워커 설정을 추가하라."
    )


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
