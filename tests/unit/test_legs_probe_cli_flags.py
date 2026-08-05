"""CLI 플래그 경로 — `--fixture`·`--prompt`·`--prompt-rev`·`--n`·`--attempt-multiplier` (#332, R3-2).

전부 `--dry-run`(가짜 LLM)이라 CI 에서 API 콜이 0이다(intent_probe/test_intent_probe_cli.py 와
같은 규약). intent_probe 경로는 `test_intent_probe_cli.py` 가 이미 재므로, 여기서는 legs_probe
가 **처음** 밟는 조합(외부 앵커·후보 프롬프트 파일·인자 가드)만 다룬다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.intent_probe.client import repo_system_prompt
from evals.legs_probe.cli import main
from evals.legs_probe.loader import FIXTURE_DIR

EXIT_OK = 0
EXIT_REJECTED = 2


def _results(out: Path) -> dict:
    return json.loads((out / "results.json").read_text(encoding="utf-8"))


def _manifest(out: Path) -> dict:
    return json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))


# ─────────── R3-2.1 — --fixture (외부 앵커) ───────────


def test_external_fixture_hash_is_recorded_without_manifest_check(tmp_path: Path) -> None:
    """옆에 manifest.json 이 없는 외부 경로는 해시 대조를 생략하되, 그 파일의 sha256 을
    results.json·run_manifest.json 에 그대로 남긴다(무엇으로 쟀는지 특정)."""
    external = tmp_path / "external-anchors.json"
    external.write_bytes((FIXTURE_DIR / "anchors.json").read_bytes())
    expected_sha256 = hashlib.sha256(external.read_bytes()).hexdigest()

    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--fixture", str(external)])
    assert exit_code == EXIT_OK

    results = _results(out)
    assert results["fixture"]["sha256"] == expected_sha256
    assert results["fixture"]["name"] == "external-anchors.json"
    manifest = _manifest(out)
    assert manifest["hashes"]["anchorFixture"] == expected_sha256


def test_external_fixture_with_tampered_content_still_loads_since_hash_check_is_skipped(
    tmp_path: Path,
) -> None:
    """[대조] committed 픽스처는 옆의 manifest.json 과 어긋나면 거부되지만(loader 테스트가
    이미 검증), 외부 경로는 manifest.json 자체가 없으므로 내용이 달라도(스키마만 통과하면)
    그대로 로드된다 — '해시 대조 생략'의 의미를 명시적으로 고정한다."""
    data = json.loads((FIXTURE_DIR / "anchors.json").read_text(encoding="utf-8"))
    data["fixtureVersion"] = "legs-anchors-external-test"
    external = tmp_path / "external-anchors.json"
    external.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--fixture", str(external)])
    assert exit_code == EXIT_OK
    assert _results(out)["fixture"]["version"] == "legs-anchors-external-test"


def test_schema_invalid_external_fixture_is_rejected(tmp_path: Path) -> None:
    """스키마 자체가 안 맞는 외부 앵커(예: utterances 누락)는 네트워크 이전에 종료 코드 2."""
    external = tmp_path / "broken-anchors.json"
    external.write_text(
        json.dumps({"schemaVersion": "1.0.0", "fixtureVersion": "broken"}), encoding="utf-8"
    )
    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--fixture", str(external)])
    assert exit_code == EXIT_REJECTED
    assert not out.exists()  # 거절 시 산출물 디렉터리를 만들지 않는다


# ─────────── R3-2.2 — --prompt (후보 프롬프트 파일) ───────────


def test_prompt_file_hash_reaches_the_manifest_not_the_repo_prompt(tmp_path: Path) -> None:
    """`--prompt <file>` 이면 run_manifest 의 `hashes.systemPrompt` 가 리포 `_SYSTEM` 이 아니라
    **그 파일 텍스트**의 해시로 남는다 — 무엇을 쟀는지 특정하는 intent_probe 규약을 legs_probe
    도 그대로 재사용한다(`resolve_system_prompt`)."""
    candidate = tmp_path / "candidate-system.txt"
    candidate.write_text("당신은 테스트용 후보 프롬프트입니다.", encoding="utf-8")
    expected_sha256 = hashlib.sha256(
        candidate.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    repo_prompt_sha256 = hashlib.sha256(repo_system_prompt().encode("utf-8")).hexdigest()
    assert expected_sha256 != repo_prompt_sha256  # 전제 확인: 후보와 리포 프롬프트는 다르다

    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--prompt", str(candidate)])
    assert exit_code == EXIT_OK

    manifest = _manifest(out)
    results = _results(out)
    assert manifest["hashes"]["systemPrompt"] == expected_sha256
    assert manifest["hashes"]["systemPrompt"] != repo_prompt_sha256
    assert results["prompt"]["sha256"] == expected_sha256
    assert results["prompt"]["source"] == f"file:{candidate.as_posix()}"


def test_without_prompt_flag_the_repo_system_prompt_hash_is_used(tmp_path: Path) -> None:
    """대조군 — `--prompt` 를 안 주면 여전히 리포 `_SYSTEM` 해시가 그대로 남는다."""
    repo_prompt_sha256 = hashlib.sha256(repo_system_prompt().encode("utf-8")).hexdigest()
    out = tmp_path / "run"
    assert main(["--out", str(out), "--dry-run"]) == EXIT_OK
    assert _manifest(out)["hashes"]["systemPrompt"] == repo_prompt_sha256


# ─────────── R3-2.3 — --prompt 와 --prompt-rev 동시 지정 ───────────


def test_prompt_and_prompt_rev_together_is_rejected(tmp_path: Path) -> None:
    """argparse mutually exclusive group — 동시 지정은 `SystemExit(2)` 로 즉시 거절된다."""
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("아무 내용", encoding="utf-8")
    out = tmp_path / "run"
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--out",
                str(out),
                "--dry-run",
                "--prompt",
                str(candidate),
                "--prompt-rev",
                "3f1dec7",
            ]
        )
    assert excinfo.value.code == EXIT_REJECTED
    assert not out.exists()


# ─────────── R3-2.4 — --n 0 / --attempt-multiplier 0 ───────────


def test_n_zero_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--n", "0"])
    assert exit_code == EXIT_REJECTED
    assert not out.exists()


def test_negative_n_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--n", "-1"])
    assert exit_code == EXIT_REJECTED
    assert not out.exists()


def test_attempt_multiplier_zero_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "run"
    exit_code = main(["--out", str(out), "--dry-run", "--attempt-multiplier", "0"])
    assert exit_code == EXIT_REJECTED
    assert not out.exists()
