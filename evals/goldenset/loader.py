"""split별 골든셋 로딩과 holdout 봉인 해제 API."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.pipelines.compare import GoldenCase as CompareGoldenCase
from evals.goldenset.schema import GoldenCase, HoldoutCase, HoldoutLabels, load_jsonl

ROOT = Path(__file__).resolve().parent
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def load_cases(
    split: Literal["dev", "holdout"] = "dev", *, root: Path = ROOT
) -> list[GoldenCase] | list[HoldoutCase]:
    """dev는 라벨 포함, holdout은 봉인된 비라벨 파일만 읽는다."""
    if split == "dev":
        return list(load_jsonl(root / "cases" / "buyer_dev.jsonl", GoldenCase))
    if split == "holdout":
        return list(load_jsonl(root / "cases" / "buyer_holdout.jsonl", HoldoutCase))
    raise ValueError("split은 dev 또는 holdout이어야 합니다")


def unseal_holdout_labels(
    *, reason: str, commit_sha: str, root: Path = ROOT
) -> list[HoldoutLabels]:
    """release 평가 사유와 commit을 감사 로그에 남기고 holdout 라벨을 연다."""
    if not reason.strip():
        raise ValueError("holdout 봉인 해제 reason은 비어 있을 수 없습니다")
    if not COMMIT_SHA_RE.fullmatch(commit_sha):
        raise ValueError("commit_sha는 40자리 hex여야 합니다")
    labels = list(load_jsonl(root / "cases" / "buyer_holdout_labels.jsonl", HoldoutLabels))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    event = {
        "unsealedAt": datetime.now(timezone.utc).isoformat(),
        "commitSha": commit_sha.lower(),
        "reason": reason.strip(),
        "datasetHash": manifest["datasetHash"],
    }
    log_path = root / "audit" / "holdout_runs.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return labels


def _merge_holdout(cores: list[HoldoutCase], labels: list[HoldoutLabels]) -> list[GoldenCase]:
    """holdout 비라벨 케이스와 이미 승인된 라벨을 결합한다."""
    by_id = {item.case_id: item for item in labels}
    merged: list[GoldenCase] = []
    for core in cores:
        label = by_id.get(core.case_id)
        if label is None:
            raise ValueError(f"{core.case_id}: holdout 라벨이 없습니다")
        merged.append(
            GoldenCase.model_validate(
                {
                    **core.model_dump(by_alias=True, exclude={"notes"}),
                    **label.model_dump(by_alias=True),
                }
            )
        )
    return merged


def _load_labeled_holdout_for_audit(
    split: Literal["dev", "holdout"], *, root: Path = ROOT
) -> list[GoldenCase]:
    """감사 모듈만 쓰는 프라이빗 라벨 결합 접근자."""
    if split == "dev":
        return list(load_cases("dev", root=root))
    cores = list(load_cases("holdout", root=root))
    labels = list(load_jsonl(root / "cases" / "buyer_holdout_labels.jsonl", HoldoutLabels))
    return _merge_holdout(cores, labels)


def to_compare_golden_cases(
    split: Literal["dev", "holdout"] = "dev",
    *,
    reason: str | None = None,
    commit_sha: str | None = None,
) -> list[CompareGoldenCase]:
    """#32 비교 하니스가 쓰는 유일한 query→relevant productId 어댑터."""
    if split == "dev":
        cases = list(load_cases("dev"))
    elif split == "holdout":
        if reason is None or commit_sha is None:
            raise ValueError("holdout 비교에는 reason과 commit_sha가 필요합니다")
        labels = unseal_holdout_labels(reason=reason, commit_sha=commit_sha)
        cases = _merge_holdout(list(load_cases("holdout")), labels)
    else:
        raise ValueError("split은 dev 또는 holdout이어야 합니다")
    return [
        CompareGoldenCase(query=case.query, relevant_ids=set(case.relevant_product_ids))
        for case in cases
        if "search" in case.slices
    ]
