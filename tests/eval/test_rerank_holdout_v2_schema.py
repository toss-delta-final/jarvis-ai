from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.rerank_holdout_v2.io import dataset_hash, load_dataset, sha256_file, write_jsonl
from evals.rerank_holdout_v2.schema import DraftLabels, RankingCaseCore


def _ranking_core_payload(**overrides: object) -> dict[str, object]:
    product_ids = list(range(1, 31))
    payload: dict[str, object] = {
        "caseId": "rh2-general-0001",
        "familyId": "rh2-fam-general-keyboard",
        "schemaVersion": "1.0.0",
        "datasetVersion": "1.0.0",
        "split": "prospective_holdout",
        "stratum": "general",
        "variant": "category",
        "slices": ["ranking", "general", "guest"],
        "query": "업무용 키보드 추천해줘",
        "identity": {"kind": "guest"},
        "profileSummary": None,
        "candidateProductIds": product_ids,
        "candidateProvenance": {
            str(product_id): {"source": "exact_category", "detail": "키보드"}
            for product_id in product_ids
        },
        "catalogSha256": "a" * 64,
        "provenance": "synthetic-catalog-derived",
    }
    payload.update(overrides)
    return payload


def _draft_label_payload(case_id: str = "rh2-general-0001") -> dict[str, object]:
    return {
        "caseId": case_id,
        "labelStatus": "draft",
        "labelSource": "heuristic",
        "relevantProductIds": [1, 2],
        "relevanceGrades": {"1": 3, "2": 2},
        "idealOrder": [1, 2],
        "hardConstraints": {},
        "mustExcludeProductIds": [],
        "labelRationale": "카테고리와 브랜드의 관측 가능한 필드로 만든 초안이다.",
    }


def _write_minimal_dataset(root: Path) -> Path:
    root.mkdir()
    (root / "cases").mkdir()
    (root / "annotations").mkdir()
    catalog_path = root / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                str(product_id): {
                    "productId": product_id,
                    "name": f"상품 {product_id}",
                    "categoryName": "키보드",
                    "brandName": "브랜드",
                    "price": 10_000,
                    "rating": 4.5,
                    "reviewCount": 10,
                    "attributes": {},
                    "summary": None,
                }
                for product_id in range(1, 31)
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    core_path = root / "cases/ranking_core.jsonl"
    labels_path = root / "annotations/draft_labels.jsonl"
    safety_path = root / "cases/safety.jsonl"
    write_jsonl(core_path, [RankingCaseCore.model_validate(_ranking_core_payload())])
    write_jsonl(labels_path, [DraftLabels.model_validate(_draft_label_payload())])
    safety_path.write_text("", encoding="utf-8")
    files = {
        "cases/ranking_core.jsonl": sha256_file(core_path),
        "cases/safety.jsonl": sha256_file(safety_path),
        "annotations/draft_labels.jsonl": sha256_file(labels_path),
    }
    manifest = {
        "schemaVersion": "1.0.0",
        "datasetVersion": "1.0.0",
        "seed": 631200,
        "catalogSourcePath": "catalog.json",
        "catalogSha256": sha256_file(catalog_path),
        "datasetHash": dataset_hash(
            files,
            catalog_sha256=sha256_file(catalog_path),
            seed=631200,
        ),
        "rankingCount": 1,
        "safetyCount": 0,
        "identityCounts": {"guest": 1, "member": 0},
        "stratumCounts": {"general": 1},
        "labelStatus": "draft",
        "confirmatoryEligible": False,
        "fileHashes": files,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def test_manifest_requires_a_dataset_hash(tmp_path: Path) -> None:
    root = _write_minimal_dataset(tmp_path / "dataset")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["datasetHash"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="datasetHash"):
        load_dataset(root, label_policy="draft")


def test_loader_rejects_a_manifest_dataset_hash_mismatch(tmp_path: Path) -> None:
    root = _write_minimal_dataset(tmp_path / "dataset")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["datasetHash"] = "c" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="datasetHash mismatch"):
        load_dataset(root, label_policy="draft")


def test_ranking_core_requires_exactly_thirty_distinct_candidates() -> None:
    with pytest.raises(ValidationError, match="exactly 30"):
        RankingCaseCore.model_validate(
            _ranking_core_payload(candidateProductIds=list(range(1, 30)))
        )


def test_ranking_core_rejects_embedded_labels() -> None:
    with pytest.raises(ValidationError, match="label fields"):
        RankingCaseCore.model_validate(_ranking_core_payload(relevanceGrades={"1": 3}))


def test_ranking_core_requires_profile_only_for_member() -> None:
    with pytest.raises(ValidationError, match="guest profileSummary"):
        RankingCaseCore.model_validate(_ranking_core_payload(profileSummary="선호 브랜드: 테스트"))

    member = _ranking_core_payload(
        caseId="rh2-general-0002",
        familyId="rh2-fam-general-mouse",
        slices=["ranking", "general", "member"],
        identity={"kind": "member"},
        profileSummary="선호 브랜드: 테스트",
    )
    assert RankingCaseCore.model_validate(member).identity.kind == "member"


def test_candidate_provenance_must_cover_candidate_ids() -> None:
    with pytest.raises(ValidationError, match="candidateProvenance"):
        RankingCaseCore.model_validate(
            _ranking_core_payload(
                candidateProvenance={
                    str(product_id): {"source": "exact_category", "detail": "키보드"}
                    for product_id in range(1, 30)
                }
            )
        )


def test_confirmatory_loader_rejects_draft_labels(tmp_path: Path) -> None:
    root = _write_minimal_dataset(tmp_path / "dataset")

    with pytest.raises(ValueError, match="sealed labels required"):
        load_dataset(root, label_policy="sealed")


def test_draft_loader_verifies_hashes_and_loads_rows(tmp_path: Path) -> None:
    root = _write_minimal_dataset(tmp_path / "dataset")

    dataset = load_dataset(root, label_policy="draft")

    assert dataset.manifest.label_status == "draft"
    assert [case.case_id for case in dataset.ranking_cases] == ["rh2-general-0001"]
    assert dataset.labels_by_case["rh2-general-0001"].relevance_grades == {1: 3, 2: 2}
    assert len(dataset.catalog) == 30

    with (root / "cases/ranking_core.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_dataset(root, label_policy="draft")


def test_write_jsonl_is_sorted_and_byte_stable(tmp_path: Path) -> None:
    first = RankingCaseCore.model_validate(
        _ranking_core_payload(
            caseId="rh2-general-0002",
            familyId="rh2-fam-general-mouse",
            query="마우스 추천",
        )
    )
    second = RankingCaseCore.model_validate(_ranking_core_payload())
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"

    write_jsonl(left, [first, second])
    write_jsonl(right, [second, first])

    assert left.read_bytes() == right.read_bytes()
    assert left.read_text(encoding="utf-8").splitlines()[0].startswith('{"candidateProductIds"')
    assert '"caseId":"rh2-general-0001"' in left.read_text(encoding="utf-8").splitlines()[0]
