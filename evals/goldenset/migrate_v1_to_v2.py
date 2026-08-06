"""v1(schemaVersion/datasetVersion 1.0.0) 골든셋을 v2(2.0.0)로 기계 이관하는 일회성 스크립트.

실행: ``uv run python -m evals.goldenset.migrate_v1_to_v2``

무엇을 하는가(이슈 #333 Part 1, §2.1):

- dev/holdout/holdout-labels JSONL의 ``schemaVersion``·``datasetVersion``을 2.0.0으로 올리고
  ``testType=MFT``를 명시한다.
- ``identity.kind == "member"`` 케이스에 ``member`` 슬라이스를 거울로 채운다(``guest``와 대칭).
- 니즈 슬라이스(단일/복수제약/예산/재구매)를 케이스당 정확히 1개로 배정한다. 우선순위는
  가격 하드제약이 있으면 ``budget``, ``repurchase`` 슬라이스가 있으면 ``repurchase``,
  ``queryType == "multi_constraint"``면 ``multi_constraint``, 나머지는 ``single_need``다.
  이미 붙어 있던 다른 니즈류 슬라이스 태그(예: v1의 ``multi_constraint`` 시나리오 태그)는
  disjoint 불변식을 지키기 위해 배정된 것으로 교체한다 — 구현 보고의 설계 결정 항목 참조.
- ``fixtures/search_responses.json``에 v2 candidates provenance를 채운다. v1은 전부 단일 골든
  검색 결과이므로 ``source="golden_filter"``, ``rule=null``, ``from="primary"``다.
- 순위 평가 대상(MFT + search slice + relevant 비어있지 않음 + 후보 전부정답 아님, "전부정답"은
  개수가 아니라 집합 포함으로 판정한다 — ``schema.all_candidates_are_correct``)인데 후보 수가
  ``goldenset_min_ranking_candidates`` 미만인 케이스의 notes에 ``narrow-domain:`` 접두 문구를,
  등급≥1 후보 비율이 ``goldenset_max_relevant_ratio``를 초과하는 케이스의 notes에
  ``relevant-ratio-exempt:`` 접두 문구를 붙인다(두 문구는 이어붙일 수 있다 — ``schema.has_note_marker``
  참조). dev는 케이스 파일 notes에, holdout은 라벨 파일 notes에 붙인다 — 병합된 GoldenCase의
  notes는 holdout core가 아니라 라벨 쪽 값이기 때문이다(``loader._merge_holdout`` 참조).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from evals.goldenset.schema import (
    NEEDS_SLICES,
    GoldenCase,
    HoldoutCase,
    HoldoutLabels,
    all_candidates_are_correct,
    dump_jsonl,
    has_note_marker,
)

ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "cases"
FIXTURES_PATH = ROOT / "fixtures" / "search_responses.json"
DEFAULT_MIN_RANKING_CANDIDATES = 20
DEFAULT_MAX_RELEVANT_RATIO = 0.25


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _needs_slice(raw: dict) -> str:
    """케이스 하나의 니즈 슬라이스를 정한다.

    패킷 §2.1의 문면 우선순위는 "가격 하드제약 → budget"이 최우선이지만, 그대로 적용하면
    v1에서 이미 사람이 ``multi_constraint`` 슬라이스로 큐레이션해 둔 케이스
    (``buy-mult-0001/0002``, ``buy-fail-0001/0003``)가 전부 가격 제약도 함께 갖고 있어
    ``budget``으로 흡수되고, dev split에 ``multi_constraint`` 케이스가 0건 남아
    ``audit.run_audit()``의 ``missingDevSlice`` 위반을 새로 만든다. 라벨을 손대지 않고
    이를 피하는 유일한 길은 "이미 명시적으로 curation된 슬라이스"를 기계적 가격 신호보다
    우선하는 것이다 — v1 라벨러의 명시적 판단이 필드 존재 여부보다 강한 신호라고 본다.
    이 재정렬은 패킷 문면과 다른 설계 결정이라 구현 보고에 명시했다(§5, orchestrator 확인 필요).
    """
    if "repurchase" in raw["slices"]:
        return "repurchase"
    if "multi_constraint" in raw["slices"]:
        return "multi_constraint"
    hard = raw.get("hardConstraints") or {}
    if hard.get("priceMax") is not None or hard.get("priceMin") is not None:
        return "budget"
    if raw["queryType"] == "multi_constraint":
        return "multi_constraint"
    return "single_need"


def _migrate_slices(raw: dict) -> list[str]:
    kept = [name for name in raw["slices"] if name not in NEEDS_SLICES]
    if raw["identity"]["kind"] == "member" and "member" not in kept:
        kept.append("member")
    kept.append(_needs_slice(raw))
    return kept


def _migrate_core(raw: dict) -> dict:
    """dev 케이스 또는 holdout core 케이스가 공유하는 필드를 이관한다."""
    case = dict(raw)
    case["schemaVersion"] = "2.0.0"
    case["datasetVersion"] = "2.0.0"
    case["testType"] = "MFT"
    case["slices"] = _migrate_slices(raw)
    return case


def _migrate_candidates(fixture: dict) -> dict:
    product_ids = sorted(fixture["productIds"])
    return {
        **fixture,
        "productIds": product_ids,
        "candidates": [
            {"productId": product_id, "source": "golden_filter", "rule": None, "from": "primary"}
            for product_id in product_ids
        ],
    }


def migrate_fixtures(fixtures: dict[str, dict]) -> dict[str, dict]:
    """search_responses.json의 모든 fixture 항목에 v2 candidates provenance를 채운다."""
    return {fixture_id: _migrate_candidates(payload) for fixture_id, payload in fixtures.items()}


def _is_ranking_eligible(
    slices: list[str], relevant_product_ids: list[int], candidate_ids: list[int]
) -> bool:
    if "search" not in slices or not relevant_product_ids:
        return False
    return not all_candidates_are_correct(candidate_ids, relevant_product_ids)


def _apply_note_marker(notes: str, marker: str) -> str:
    """notes 접두부에 marker(콜론 포함)를 붙인다 — 이미 있으면 그대로 두고, 다른 마커가 이미
    붙어 있으면 그 앞에 이어붙여 접두부 전체가 마커들의 연속으로 남게 한다."""
    name = marker.rstrip(":").strip()
    if has_note_marker(notes, name):
        return notes
    return f"{marker} {notes}"


def _apply_ranking_exemption_notes(
    notes: str,
    *,
    slices: list[str],
    relevant_product_ids: list[int],
    candidate_ids: list[int],
    min_ranking_candidates: int,
    max_relevant_ratio: float,
) -> str:
    """순위 평가 대상 케이스에 필요한 예외 마커(narrow-domain/relevant-ratio-exempt)를 붙인다."""
    if not _is_ranking_eligible(slices, relevant_product_ids, candidate_ids):
        return notes
    candidate_count = len(candidate_ids)
    updated = notes
    if candidate_count < min_ranking_candidates:
        updated = _apply_note_marker(updated, "narrow-domain:")
    graded_count = len(set(candidate_ids) & set(relevant_product_ids))
    relevant_ratio = graded_count / candidate_count if candidate_count else 0.0
    if relevant_ratio > max_relevant_ratio:
        updated = _apply_note_marker(updated, "relevant-ratio-exempt:")
    return updated


def migrate(
    *,
    root: Path = ROOT,
    min_ranking_candidates: int = DEFAULT_MIN_RANKING_CANDIDATES,
    max_relevant_ratio: float = DEFAULT_MAX_RELEVANT_RATIO,
) -> None:
    """v1 dev/holdout/labels/fixtures 파일을 제자리에서 v2로 이관해 다시 쓴다."""
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    migrated_fixtures = migrate_fixtures(fixtures)

    dev_raw = _read_jsonl(root / "cases" / "buyer_dev.jsonl")
    migrated_dev: list[BaseModel] = []
    for raw in dev_raw:
        case = _migrate_core(raw)
        fixture_id = raw.get("searchFixtureId")
        if fixture_id and fixture_id in migrated_fixtures:
            candidate_ids = migrated_fixtures[fixture_id]["productIds"]
            case["notes"] = _apply_ranking_exemption_notes(
                case["notes"],
                slices=case["slices"],
                relevant_product_ids=case.get("relevantProductIds") or [],
                candidate_ids=candidate_ids,
                min_ranking_candidates=min_ranking_candidates,
                max_relevant_ratio=max_relevant_ratio,
            )
        migrated_dev.append(GoldenCase.model_validate(case))

    holdout_core_raw = _read_jsonl(root / "cases" / "buyer_holdout.jsonl")
    migrated_holdout_core: list[BaseModel] = [
        HoldoutCase.model_validate(_migrate_core(raw)) for raw in holdout_core_raw
    ]

    holdout_core_by_id = {raw["caseId"]: raw for raw in holdout_core_raw}
    labels_raw = _read_jsonl(root / "cases" / "buyer_holdout_labels.jsonl")
    migrated_labels: list[BaseModel] = []
    for label in labels_raw:
        core = holdout_core_by_id[label["caseId"]]
        merged_slices = _migrate_slices(core)
        fixture_id = core.get("searchFixtureId")
        updated_label = dict(label)
        if fixture_id and fixture_id in migrated_fixtures:
            candidate_ids = migrated_fixtures[fixture_id]["productIds"]
            updated_label["notes"] = _apply_ranking_exemption_notes(
                label["notes"],
                slices=merged_slices,
                relevant_product_ids=label.get("relevantProductIds") or [],
                candidate_ids=candidate_ids,
                min_ranking_candidates=min_ranking_candidates,
                max_relevant_ratio=max_relevant_ratio,
            )
        migrated_labels.append(HoldoutLabels.model_validate(updated_label))

    dump_jsonl(root / "cases" / "buyer_dev.jsonl", migrated_dev)
    dump_jsonl(root / "cases" / "buyer_holdout.jsonl", migrated_holdout_core)
    dump_jsonl(root / "cases" / "buyer_holdout_labels.jsonl", migrated_labels)
    FIXTURES_PATH.write_text(
        json.dumps(migrated_fixtures, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    migrate()
    print("migrated evals/goldenset cases and fixtures to schemaVersion/datasetVersion 2.0.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
