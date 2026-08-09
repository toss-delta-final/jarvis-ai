"""골든셋 로더 — 해시 게이트 + 라이브 사전 pre-flight (#462, `evals/category_probe/loader.py` 규약)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.taste_probe.schema import GoldenSet

FIXTURE_DIR = Path(__file__).with_name("fixtures")
DEFAULT_FIXTURE = "sessions.json"


def resolve_fixture_path(name: str, *, fixture_dir: Path | None = None) -> Path:
    """`default`(별칭) 또는 임의 경로를 골든셋 파일 경로로 푼다.

    `fixture_dir` 가 명시됐고 `name` 이 상대 파일명이면 그 디렉터리 기준으로 푼다(테스트가
    `tmp_path` 격리 디렉터리를 가리킬 때 쓴다) — 절대경로나 `fixture_dir` 미지정 시에는 종전대로
    `name` 을 cwd 기준 경로로 그대로 쓴다(CLI `--fixture <path>` 규약).
    """
    directory = fixture_dir or FIXTURE_DIR
    if name == "default":
        return directory / DEFAULT_FIXTURE
    candidate = Path(name)
    if fixture_dir is not None and not candidate.is_absolute():
        return fixture_dir / candidate
    return candidate


def fixture_sha256(path: Path) -> str:
    """골든셋 파일의 sha256 — 산출물에 그대로 실려 '무엇을 쟀는지'를 남긴다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_golden_set(name: str = "default", *, fixture_dir: Path | None = None) -> GoldenSet:
    """골든셋 파일을 읽는다. 커밋된 픽스처(`default`)는 manifest 해시와 **반드시** 대조한다.

    [F1-6] 규약(`evals/category_probe/loader.py` 승계): `default` 는 manifest.json 부재나 그
    파일명 키 누락을 **거부**한다(ValueError) — 조용히 통과시키면 커밋된 골든셋의 manifest 가
    지워지거나 키가 빠져도 손상 탐지가 통째로 무력화된다. `default` 가 아닌 외부 경로
    (`--fixture <path>`)는 종전대로 대조를 건너뛰되 호출부가 `fixture_sha256` 으로 산출물에
    해시를 남긴다.
    """
    path = resolve_fixture_path(name, fixture_dir=fixture_dir)
    manifest_path = path.parent / "manifest.json"
    is_default = name == "default"
    if not manifest_path.exists():
        if is_default:
            raise ValueError(
                f"기본 픽스처 manifest 가 없습니다: {manifest_path} — 커밋된 골든셋은 sha256 대조가 "
                "필수입니다(재측정 없이 조용히 통과시키면 손상을 못 잡습니다)"
            )
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256", {}).get(path.name)
        if expected is None:
            if is_default:
                raise ValueError(
                    f"기본 픽스처 manifest 에 {path.name!r} 의 sha256 키가 없습니다 — "
                    "커밋된 골든셋은 등재가 필수입니다"
                )
        elif expected != fixture_sha256(path):
            raise ValueError(
                f"{path.name} 의 SHA-256 이 manifest 와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    return GoldenSet.model_validate_json(path.read_text(encoding="utf-8"))


class PreflightError(RuntimeError):
    """라이브 pg-catalog 와 골든셋의 category accept 가 어긋난다 — 종료 코드 2 사유."""


def preflight_check_catalog(golden_set: GoldenSet, dsn: str) -> None:
    """모든 category kind 의 accept 가 라이브 categories 에 실재하는지 확인한다(§11).

    category 라벨은 사전(taxonomy)에 종속된다 — 사전이 재시드돼 골든셋이 낡으면 이 pre-flight 가
    조용한 오탐 대신 즉시 종료 코드 2 로 드러낸다. `--dry-run` 은 pg 접근이 0 이어야 하므로 이
    함수를 호출하지 않는다(cli.py 참조).
    """
    from app.pipelines.category_search import exact_lookup

    accepts: set[str] = set()
    for session in golden_set.sessions:
        for triple in session.expected_triples:
            if triple.kind == "category":
                accepts.update(triple.accept)
    if not accepts:
        return
    found = exact_lookup(sorted(accepts), dsn)
    missing = accepts - found
    if missing:
        raise PreflightError(
            f"category accept 라벨 {sorted(missing)} 이 라이브 categories 사전에 없습니다 — "
            "사전이 재시드됐거나 골든셋이 낡았습니다(재측정 필요)"
        )
