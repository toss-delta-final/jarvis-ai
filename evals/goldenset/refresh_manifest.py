"""골든셋 파일 해시·건수·datasetHash를 결정론적으로 갱신한다."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.goldenset.audit import dataset_hash
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import DATASET_VERSION

ROOT = Path(__file__).resolve().parent
# datasetHash는 재현 가능한 데이터셋 산출물만 덮는다. holdout 봉인 해제 감사 로그는 런타임에
# append되므로 포함하면 데이터가 안 바뀌어도 hash가 바뀐다.
HASH_EXCLUDED_PATHS = frozenset({"audit/holdout_runs.jsonl"})


def run() -> None:
    path = ROOT / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    dev, holdout = load_cases("dev"), load_cases("holdout")
    by_slice: dict[str, dict[str, int]] = {"dev": {}, "holdout": {}}
    for split, cases in (("dev", dev), ("holdout", holdout)):
        for case in cases:
            for name in case.slices:
                by_slice[split][name] = by_slice[split].get(name, 0) + 1
    manifest["datasetVersion"] = DATASET_VERSION
    manifest["counts"].update(
        {
            "dev": len(dev),
            "holdout": len(holdout),
            "total": len(dev) + len(holdout),
            "bySlice": by_slice,
        }
    )
    paths = {
        file.relative_to(ROOT).as_posix()
        for file in ROOT.rglob("*")
        if file.is_file()
        and file.name != "manifest.json"
        and "__pycache__" not in file.parts
        and file.relative_to(ROOT).as_posix() not in HASH_EXCLUDED_PATHS
    }
    files = []
    for rel in sorted(paths):
        payload = (ROOT / rel).read_bytes()
        files.append(
            {
                "path": rel,
                "bytes": len(payload),
                "records": len(payload.splitlines()) if rel.endswith(".jsonl") else 0,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest["files"] = files
    manifest["datasetHash"] = dataset_hash(files)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    run()
