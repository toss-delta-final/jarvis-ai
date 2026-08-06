"""라벨 provenance(labelSource/labeledAt/labelRationale) 소급 기입 일회성 스크립트(#370).

실행: ``uv run python -m evals.goldenset.backfill_label_provenance``

무엇을 하는가:

- dev(``buyer_dev.jsonl``)와 holdout core(``buyer_holdout.jsonl``) 전 127건에 새 provenance
  필드 3종을 채운다. ``buyer_holdout_labels.jsonl``(sealed)은 절대 열지도 수정하지도 않는다 —
  labelSource 등은 케이스 core 필드(``schema.CaseCore``)이지 라벨 값 자체가 아니므로 core
  파일만으로 충분하다.
- ``schemaVersion``/``datasetVersion``을 2.1.0/2.2.0으로 함께 올린다(GUIDE.md 변경 절차 2번).

소급 원칙(#370 패킷 §B, orchestrator 지시): 문서화된 사실만 쓰고 불명은 추정하지 않는다.
GUIDE.md("v1의 adjudicator는 비어 있다... 현재 라벨은 구현자가 붙인 자동 초안이며 사람 검수
완료 상태가 아니다")와 manifest.json의 ``adjudicationSummary``(adjudicator-omx-01의 127건
전수 검수)가 기존 127건 전부에 ``labelSource="model"``을 정당화하는 문서 근거다. 개별 케이스별
라벨 시점 기록은 없으므로(패킷 §B) ``labeledAt``은 adjudication이 실제로 반영된 날짜를 쓴다 —
CHANGELOG.md의 adjudicator-omx-01 dispute 반영 항목은 **2026-08-06**(패킷 문면의 2026-08-05는
CHANGELOG 실제 일자와 어긋나 이 스크립트는 CHANGELOG 원문을 따른다, 최종 보고에 명시)에
날짜가 찍혀 있고, manifest에 전 127건 adjudicator 기록이 반영된 것도 같은 날짜다.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.goldenset.schema import (
    DATASET_VERSION,
    SCHEMA_VERSION,
    GoldenCase,
    HoldoutCase,
    dump_jsonl,
)

ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "cases"

LABEL_SOURCE = "model"
LABELED_AT = "2026-08-06"
LABEL_RATIONALE = (
    "GUIDE.md 검수 절·manifest.adjudicationSummary 근거 — labeler-01/02 자동 초안 + "
    "adjudicator-omx-01(모델) 127건 전수 검수(CHANGELOG 2026-08-06 항목), 사람 검수 완료 "
    "아님. #370 소급 기입."
)


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _backfill_core(raw: dict) -> dict:
    case = dict(raw)
    case["schemaVersion"] = SCHEMA_VERSION
    case["datasetVersion"] = DATASET_VERSION
    case["labelSource"] = LABEL_SOURCE
    case["labeledAt"] = LABELED_AT
    case["labelRationale"] = LABEL_RATIONALE
    return case


def backfill(*, root: Path = ROOT) -> None:
    """dev/holdout core 파일을 제자리에서 라벨 provenance 포함 버전으로 다시 쓴다."""
    dev_raw = _read_jsonl(root / "cases" / "buyer_dev.jsonl")
    backfilled_dev = [GoldenCase.model_validate(_backfill_core(raw)) for raw in dev_raw]

    holdout_raw = _read_jsonl(root / "cases" / "buyer_holdout.jsonl")
    backfilled_holdout = [HoldoutCase.model_validate(_backfill_core(raw)) for raw in holdout_raw]

    dump_jsonl(root / "cases" / "buyer_dev.jsonl", backfilled_dev)
    dump_jsonl(root / "cases" / "buyer_holdout.jsonl", backfilled_holdout)


def main() -> int:
    backfill()
    print(
        f"backfilled evals/goldenset label provenance to schemaVersion={SCHEMA_VERSION} "
        f"datasetVersion={DATASET_VERSION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
