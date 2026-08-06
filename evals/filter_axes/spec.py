"""축 정의 데이터 정본(axes.json) 로더 — I/O는 이 모듈만 한다(#334)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

AXES_SPEC_PATH = Path(__file__).parent / "axes.json"


def load_axes_spec(path: Path = AXES_SPEC_PATH) -> dict[str, Any]:
    """축 정의·정규화 규칙 정본을 읽는다. `metrics.py`의 순수 함수는 이 반환값만 받는다."""
    return json.loads(path.read_text(encoding="utf-8"))


def axes_spec_sha256(path: Path = AXES_SPEC_PATH) -> str:
    """산출물에 '무엇을 쟀는지' 동봉하기 위한 axes.json 해시."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
