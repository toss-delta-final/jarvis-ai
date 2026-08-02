"""manifest 재현 정보와 secret 차단을 검증한다."""

import pytest

from app.core.config import Settings
from evals.benchmark.manifest import assert_secret_free, collect_manifest, redact_url_userinfo


def test_default_settings_form_consistent_runner_configuration() -> None:
    settings = Settings()
    assert (
        settings.benchmark_cold_requests + settings.benchmark_warmup_requests
        < settings.benchmark_min_measured_requests
    )
    assert settings.benchmark_p99_min_samples >= settings.benchmark_min_measured_requests
    assert settings.benchmark_concurrency_levels == (1, 5, 10)


def test_manifest_has_unknowns_without_guessing(tmp_path) -> None:
    (tmp_path / "uv.lock").write_text("locked", encoding="utf-8")
    manifest = collect_manifest(
        repo=tmp_path,
        settings=Settings(),
        started_at_utc="2026-01-01T00:00:00+00:00",
        ended_at_utc=None,
        command="python -m evals.benchmark.runner --auth-token-env BENCH_AUTH_TOKEN",
        base_url="http://localhost",
        target_label="local",
        auth_mode_observed="dev_guest",
        client_region=None,
        instance_type=None,
        image=None,
        dependency_notes=[],
        sample_size_rationale=None,
        model_ids=None,
    )
    assert manifest["client_region"] == {"value": None, "unknown_reason": "not_provided"}
    assert manifest["model_ids"] == {"value": None, "unknown_reason": "server_join_unavailable"}
    assert manifest["bootstrap"]["seed"] == 20260803


def test_secret_value_is_rejected_from_any_artifact() -> None:
    secret = "fake-super-secret-token-value"
    artifacts = {"manifest": {}, "report": "safe", "raw": [{"accidental": secret}]}
    with pytest.raises(ValueError, match="secret value"):
        assert_secret_free(artifacts, [secret])


@pytest.mark.parametrize(
    "leak",
    [
        "Bearer accidental-value",
        "SERVICE_API_KEY=accidental-value",
        "BENCH_TOKEN=accidental-value",
        "https://user:accidental-value@host/path",
    ],
)
def test_secret_shaped_output_is_rejected_without_known_value(leak: str) -> None:
    with pytest.raises(ValueError, match="secret value"):
        assert_secret_free({"value": leak}, [])


def test_url_userinfo_is_removed_not_partially_masked() -> None:
    redacted = redact_url_userinfo("https://user:password@example.com:8443/path?q=1")
    assert redacted == "https://[REDACTED]@example.com:8443/path?q=1"
    assert "user" not in redacted
    assert "password" not in redacted
