"""픽스처 로더 — 해시 게이트 + 인라인 팔 채널(decompose 컨텍스트) 조립."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.schemas.spring import ProductSearchFilters
from evals.priority_probe.schema import Channel, FixtureSet

FIXTURE_DIR = Path(__file__).with_name("fixtures")
DEFAULT_FIXTURE = "priority_fixture.json"


def resolve_fixture_path(name: str, *, fixture_dir: Path | None = None) -> Path:
    directory = fixture_dir or FIXTURE_DIR
    if name == "default":
        return directory / DEFAULT_FIXTURE
    candidate = Path(name)
    return candidate if candidate.exists() else directory / name


def fixture_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixture_set(name: str = "default", *, fixture_dir: Path | None = None) -> FixtureSet:
    """픽스처 파일을 읽는다. 커밋된 픽스처는 `manifest.json` 의 sha256 과 대조한다.

    외부 경로는 대조를 건너뛰되, 호출부가 `fixture_sha256` 으로 산출물에 해시를 남긴다.
    """
    path = resolve_fixture_path(name, fixture_dir=fixture_dir)
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256", {}).get(path.name)
        if expected and expected != fixture_sha256(path):
            raise ValueError(
                f"{path.name} 의 SHA-256 이 manifest 와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    return FixtureSet.model_validate_json(path.read_text(encoding="utf-8"))


def build_decompose_kwargs(channel: Channel) -> dict[str, Any]:
    """인라인 팔이 `decompose()` 에 넘길 세션 상태 kwargs."""
    return {
        "prior_filters": (
            ProductSearchFilters.model_validate(channel.prior_filters)
            if channel.prior_filters is not None
            else None
        ),
        "last_recommendations": (
            [(product.product_id, product.name) for product in channel.last_recommendations] or None
        ),
        "profile_summary": channel.profile_summary,
        "category_fanout_max": channel.category_fanout_max,
    }
