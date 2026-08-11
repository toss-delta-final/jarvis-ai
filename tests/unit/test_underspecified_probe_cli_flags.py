"""CLI 거절 경로 — `--out` 존재·앵커 해시 불일치·`--n<=0` (#380, D14 항목 10).

전부 `--dry-run` 이라 CI 에서 API 콜이 0이다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.config import Settings
from evals.underspecified_probe.cli import _parser, _resolve_attr_axis_suppression, main
from evals.underspecified_probe.loader import FIXTURE_DIR

EXIT_OK = 0
EXIT_REJECTED = 2


def test_attr_axis_suppression_switch_is_independent_and_follows_settings() -> None:
    """#464 스위치는 arm 과 독립이고 생략 시 Settings 값을 따른다."""
    parser = _parser()
    assert parser.parse_args([]).attr_axis_suppression is None
    assert parser.parse_args(["--no-attr-axis-suppression"]).attr_axis_suppression is False
    assert parser.parse_args(["--attr-axis-suppression"]).attr_axis_suppression is True
    assert parser.parse_args(["--arm", "postprocess"]).attr_axis_suppression is None

    settings_off = Settings(_env_file=None).model_copy(
        update={"attr_condition_axis_suppression_enabled": False}
    )
    assert _resolve_attr_axis_suppression(None, settings_off) is False
    assert _resolve_attr_axis_suppression(True, settings_off) is True
    assert _resolve_attr_axis_suppression(False, settings_off) is False


def test_dry_run_manifest_records_attr_axis_suppression_and_constraint_axes(
    tmp_path: Path,
) -> None:
    """#464 측정 조건인 스위치와 제약 축 어휘를 run_manifest 에 남긴다."""
    settings = Settings(_env_file=None)
    runs = {
        "default": [],
        "disabled": ["--no-attr-axis-suppression"],
        "enabled": ["--attr-axis-suppression"],
    }

    manifests: dict[str, dict[str, object]] = {}
    for name, extra in runs.items():
        out = tmp_path / name
        assert main(["--out", str(out), "--dry-run", "--n", "1", *extra]) == EXIT_OK
        manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
        manifests[name] = manifest["underspecifiedProbe"]

    assert manifests["default"]["attrAxisSuppression"] is True
    assert manifests["disabled"]["attrAxisSuppression"] is False
    assert manifests["enabled"]["attrAxisSuppression"] is True
    assert manifests["default"]["attrConditionConstraintAxes"] == (
        settings.attr_condition_constraint_axes
    )


def test_existing_out_dir_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    assert main(["--out", str(out), "--dry-run"]) == EXIT_REJECTED


def test_n_zero_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out), "--dry-run", "--n", "0"]) == EXIT_REJECTED
    assert not out.exists()


def test_negative_n_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out), "--dry-run", "--n", "-1"]) == EXIT_REJECTED
    assert not out.exists()


def test_attempt_multiplier_zero_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out), "--dry-run", "--attempt-multiplier", "0"]) == EXIT_REJECTED
    assert not out.exists()


def test_out_missing_is_rejected() -> None:
    assert main(["--dry-run"]) == EXIT_REJECTED


def test_tampered_external_fixture_is_rejected_when_manifest_present(tmp_path: Path) -> None:
    """committed anchors.json 을 손댄 뒤 옆에 manifest.json 을 그대로 둔 외부 픽스처 — 해시
    불일치는 네트워크 이전에 종료 코드 2로 거절돼야 한다."""
    for name in ("anchors.json", "manifest.json"):
        (tmp_path / name).write_bytes((FIXTURE_DIR / name).read_bytes())
    data = json.loads((tmp_path / "anchors.json").read_text(encoding="utf-8"))
    data["utterances"][0]["utterance"] = "손댄 발화"
    (tmp_path / "anchors.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--fixture", str(tmp_path / "anchors.json")])
    assert exit_code == EXIT_REJECTED
    assert not out.exists()


def test_external_fixture_without_manifest_skips_hash_check_but_records_sha256(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-anchors.json"
    external.write_bytes((FIXTURE_DIR / "anchors.json").read_bytes())
    expected_sha256 = hashlib.sha256(external.read_bytes()).hexdigest()

    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--fixture", str(external)])
    assert exit_code == EXIT_OK
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert results["fixture"]["sha256"] == expected_sha256


def test_schema_invalid_external_fixture_is_rejected(tmp_path: Path) -> None:
    external = tmp_path / "broken-anchors.json"
    external.write_text(
        json.dumps({"schemaVersion": "1.0.0", "fixtureVersion": "broken"}), encoding="utf-8"
    )
    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--fixture", str(external)])
    assert exit_code == EXIT_REJECTED
    assert not out.exists()
