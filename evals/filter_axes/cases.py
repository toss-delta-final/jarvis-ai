"""probe_cases.jsonl 로더 — manifest 해시 게이트(#334, `evals/README.md` 규약7)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

CASES_DIR = Path(__file__).parent / "cases"
CASES_PATH = CASES_DIR / "probe_cases.jsonl"
MANIFEST_PATH = CASES_DIR / "manifest.json"


class CamelModel(BaseModel):
    """probe 케이스도 앱·evals 공용 camelCase 별칭 규약을 쓴다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class IdentityRef(CamelModel):
    kind: Literal["guest", "member"]
    persona_id: str | None = None


class IdentityPair(CamelModel):
    guest: IdentityRef
    member: IdentityRef


class ProbeCase(CamelModel):
    """라벨 불필요 probe 케이스 1건 — MFT 가 아니라 INV/DIR/pair(#119) 다(규약 6)."""

    case_id: str
    kind: Literal["inv", "dir", "pair"]
    base_case_id: str | None = None
    base_query: str
    variant_query: str
    variant_kind: str | None = None
    expected_new_axes: list[str] | None = None
    identity_pair: IdentityPair | None = None
    notes: str


def load_probe_cases(
    path: Path = CASES_PATH, *, manifest_path: Path = MANIFEST_PATH
) -> list[ProbeCase]:
    """probe 케이스를 읽는다. manifest 가 있으면 datasetHash 와 대조해 손상·변조를 잡는다."""
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("datasetHash")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and expected != actual:
            raise ValueError(
                f"{path.name}의 SHA-256이 manifest와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    cases: list[ProbeCase] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    cases.append(ProbeCase.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f"{path}:{line_number} 검증 실패: {exc}") from exc
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("probe 케이스 caseId가 중복되었습니다")
    return cases


def load_cases_manifest(
    path: Path = CASES_PATH, *, manifest_path: Path = MANIFEST_PATH
) -> dict[str, Any]:
    """manifest.json을 읽고 probe_cases.jsonl 실측 해시 일치 여부를 함께 반환한다.

    `load_probe_cases`가 이미 로드 시점에 해시 불일치를 거절하지만(규약7), 그 검증 결과가
    산출물(results.json·run_manifest.json)에는 남지 않아 "어떤 probe 데이터셋 버전/해시로
    실행했는지" 체크아웃 없이 복원할 수 없었다 — 여기서 계산한 값을 호출부가 산출물에 동봉한다
    (#334 리뷰 F4).
    """
    manifest = dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["datasetHashVerified"] = manifest.get("datasetHash") == actual_hash
    return manifest
