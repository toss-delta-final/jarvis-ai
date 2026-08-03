"""골든셋 방식1/2 비교 어댑터의 오프라인 계약 테스트."""

from __future__ import annotations

import pytest

from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
from app.pipelines.compare import compare_backends
from evals.goldenset.loader import to_compare_golden_cases
from tests._goldenset_compare import candidates_provider, search_slice_cases


def test_committed_search_slice_matches_compare_adapter() -> None:
    cases = search_slice_cases()
    adapted = to_compare_golden_cases("dev")

    assert len(cases) == 26
    assert len({case.query for case in cases}) == len(cases)
    assert [case.query for case in adapted] == [case.query for case in cases]
    assert [case.relevant_ids for case in adapted] == [
        set(case.relevant_product_ids) for case in cases
    ]

    candidates = candidates_provider()
    assert all(candidates(case.query) for case in cases)


def test_candidates_provider_rejects_duplicate_queries() -> None:
    cases = search_slice_cases()
    duplicate = cases[1].model_copy(update={"query": cases[0].query})

    with pytest.raises(ValueError, match="중복 질의"):
        candidates_provider(cases=[cases[0], duplicate])


def test_candidates_provider_rejects_missing_fixture() -> None:
    case = search_slice_cases()[0].model_copy(update={"search_fixture_id": "missing-fixture"})

    with pytest.raises(ValueError, match="검색 fixture"):
        candidates_provider(cases=[case])


def test_provider_wires_real_cases_into_compare_backends() -> None:
    cases = search_slice_cases()[:2]
    candidates = candidates_provider()
    target = cases[0]
    target_candidates = candidates(target.query)
    assert target.case_id == "buy-srch-0001"
    assert len(target_candidates) <= 10
    assert set(target.relevant_product_ids) <= set(target_candidates)

    product_ids = {product_id for case in cases for product_id in candidates(case.query)}
    store = CatalogArtifactStore()
    store.replace_all(
        [
            CatalogArtifact(
                product_id=product_id,
                search_doc=f"product-{product_id}",
                embedding=[float(product_id % 7), 1.0],
            )
            for product_id in product_ids
        ]
    )

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[float(len(text) % 7), 1.0] for text in texts]

    report = compare_backends(
        to_compare_golden_cases("dev")[:2],
        store=store,
        embed=fake_embed,
        candidates=candidates,
        k=10,
    )

    assert report.k == 10
    assert len(report.method1.per_case) == len(cases)
    assert len(report.method2.per_case) == len(cases)
    assert report.method2.per_case[0] == 1.0
