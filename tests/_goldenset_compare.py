"""골든셋 케이스와 Spring 검색 fixture를 비교 하니스에 연결하는 테스트 헬퍼."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from evals.goldenset.loader import ROOT, load_cases
from evals.goldenset.schema import GoldenCase


def search_slice_cases(*, root: Path = ROOT) -> list[GoldenCase]:
    """dev split에서 search slice 원본 케이스를 파일 순서대로 반환한다."""
    return [case for case in load_cases("dev", root=root) if "search" in case.slices]


def candidates_provider(
    *,
    cases: Sequence[GoldenCase] | None = None,
    responses: Mapping[str, object] | None = None,
    root: Path = ROOT,
) -> Callable[[str], list[int]]:
    """질의별 Spring fixture 후보 productId를 반환하는 compare_backends용 콜러블을 만든다."""
    source_cases = list(cases) if cases is not None else search_slice_cases(root=root)
    if responses is None:
        responses = json.loads(
            (root / "fixtures" / "search_responses.json").read_text(encoding="utf-8")
        )

    by_query: dict[str, list[int]] = {}
    for case in source_cases:
        if case.query in by_query:
            raise ValueError(f"중복 질의는 후보 매핑을 덮어씁니다: {case.query!r}")
        fixture_id = case.search_fixture_id
        if fixture_id is None or fixture_id not in responses:
            raise ValueError(f"{case.case_id}: 검색 fixture가 없습니다: {fixture_id!r}")
        response = responses[fixture_id]
        if not isinstance(response, dict) or not isinstance(response.get("productIds"), list):
            raise ValueError(f"{case.case_id}: 검색 fixture productIds 형식이 잘못됐습니다")
        by_query[case.query] = [int(product_id) for product_id in response["productIds"]]

    def provide(query: str) -> list[int]:
        return list(by_query[query])

    return provide
