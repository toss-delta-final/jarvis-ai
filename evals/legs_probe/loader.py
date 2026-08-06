"""앵커 정답지 로더 — 해시 게이트, 셀 전개.

[§0] 이 프로브는 컨텍스트 행렬이 없다(intent_probe 와 다른 점) — 셀 = 앵커 1개이므로
`build_cells` 는 단순히 앵커를 셀로 감싸는 일만 한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.legs_probe.schema import Anchor, AnchorSet

FIXTURE_DIR = Path(__file__).with_name("fixtures")
DEFAULT_FIXTURE_NAME = "anchors.json"


@dataclass(frozen=True)
class Cell:
    """측정 단위 = 앵커 1개(컨텍스트 행렬 없음, §0)."""

    cell_id: str
    anchor: Anchor


def resolve_fixture_path(fixture: str | None, *, fixture_dir: Path | None = None) -> Path:
    """`--fixture` 미지정이면 committed anchors.json, 지정하면 그 경로 그대로."""
    if fixture is None:
        return (fixture_dir or FIXTURE_DIR) / DEFAULT_FIXTURE_NAME
    return Path(fixture)


def fixture_sha256(path: Path) -> str:
    """앵커 파일의 sha256 — 산출물에 그대로 실려 '무엇을 쟀는지'를 남긴다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_anchor_set(fixture: str | None = None, *, fixture_dir: Path | None = None) -> AnchorSet:
    """앵커 파일을 읽는다. committed 픽스처는 manifest 해시와 대조한다(불일치 → 종료 코드 2).

    외부 경로(`--fixture <path>`)는 옆에 `manifest.json` 이 없으면 해시 대조를 자연히 건너뛰되,
    호출부가 `fixture_sha256` 으로 산출물에 해시를 남겨 어떤 정답지로 쟀는지 항상 특정된다.
    """
    path = resolve_fixture_path(fixture, fixture_dir=fixture_dir)
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256", {}).get(path.name)
        if expected and expected != fixture_sha256(path):
            raise ValueError(
                f"{path.name} 의 SHA-256 이 manifest 와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    return AnchorSet.model_validate_json(path.read_text(encoding="utf-8"))


def build_cells(anchors: AnchorSet) -> list[Cell]:
    """앵커를 셀로 전개한다. cellId 정렬이라 동시 실행에도 순서가 결정론이다."""
    cells = [Cell(cell_id=anchor.case_id, anchor=anchor) for anchor in anchors.utterances]
    return sorted(cells, key=lambda cell: cell.cell_id)
