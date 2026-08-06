"""`axes.json`/케이스 로드 + 검증 — 스크립트는 데이터 파일만 읽는다(evals/README.md 규약 7항)."""

from __future__ import annotations

import json
from pathlib import Path

from evals.combo_matrix.schema import (
    AxesDocument,
    ComboCase,
    ExpectedBehaviorRow,
    Manifest,
    PairCheckSpec,
)

_HERE = Path(__file__).resolve().parent
AXES_PATH = _HERE / "axes.json"
CASES_PATH = _HERE / "cases" / "combo_cases.jsonl"
MANIFEST_PATH = _HERE / "cases" / "manifest.json"
EXPECTED_PATH = _HERE / "expected" / "expected_behavior.jsonl"
PAIR_CHECKS_PATH = _HERE / "expected" / "pair_checks.jsonl"


def load_axes(path: Path = AXES_PATH) -> AxesDocument:
    return AxesDocument.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_cases(path: Path = CASES_PATH) -> list[ComboCase]:
    return [ComboCase.model_validate(row) for row in _read_jsonl(path)]


def load_expected(path: Path = EXPECTED_PATH) -> list[ExpectedBehaviorRow]:
    return [ExpectedBehaviorRow.model_validate(row) for row in _read_jsonl(path)]


def load_manifest(path: Path = MANIFEST_PATH) -> Manifest:
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_pair_checks(path: Path = PAIR_CHECKS_PATH) -> list[PairCheckSpec]:
    return [PairCheckSpec.model_validate(row) for row in _read_jsonl(path)]


def dump_cases_jsonl(cases: list[ComboCase]) -> str:
    """`combo_cases.jsonl` 바이트 동일 재현을 위한 정본 직렬화 — 키 순서 고정(sort_keys)."""
    lines = [
        json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for case in cases
    ]
    return "\n".join(lines) + "\n"


def dump_expected_jsonl(rows: list[ExpectedBehaviorRow]) -> str:
    """`expected_behavior.jsonl` 정본 직렬화 — 스키마 필드 선언 순서를 그대로 쓴다(커밋본과 동일한
    키 순서, `dump_cases_jsonl` 과 달리 sort_keys 를 쓰지 않는다 — 커밋본이 그렇지 않다)."""
    lines = [
        json.dumps(row.model_dump(mode="json", by_alias=True), ensure_ascii=False) for row in rows
    ]
    return "\n".join(lines) + "\n"
