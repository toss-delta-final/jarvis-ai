"""기본 eval adapter가 실제 앱 HTTP 경계를 통과하는지 검증한다."""

import pytest

from app.core.config import get_settings
from app.schemas.spring import LIST_MAX_PRODUCTS
from evals.goldenset.loader import load_cases
from evals.metrics.harness import OfflineBuyerAdapter
from evals.metrics.runner import evaluate, load_evaluation_fixtures


def test_offline_adapter_runs_real_search_and_push_boundaries() -> None:
    case = load_cases("dev")[0]
    fixtures = load_evaluation_fixtures()
    adapter = OfflineBuyerAdapter()

    output = adapter(case, fixtures)

    # v2 후보 depth(30)는 실제 push 경계(LIST_MAX_PRODUCTS=9, I-21)보다 깊다 — 스크립트
    # rerank가 검색 순서를 보존하므로 push된 목록은 fixture 순서의 접두(prefix)여야 한다.
    fixture = fixtures.search_responses[case.search_fixture_id]
    assert len(output["rankedProductIds"]) <= LIST_MAX_PRODUCTS
    assert output["rankedProductIds"] == fixture["productIds"][: len(output["rankedProductIds"])]
    assert output["extractedFilters"] == case.expected_filters
    assert [request["path"] for request in adapter.last_requests] == [
        "/internal/products/search",
        "/internal/recommendations",
    ]
    assert all(
        request["headers"]["x-internal-token"] == "eval-internal-token"
        for request in adapter.last_requests
    )


def test_offline_adapter_names_case_when_search_fixture_is_missing() -> None:
    case = load_cases("dev")[0].model_copy(
        update={"case_id": "buy-fail-9999", "search_fixture_id": None}
    )

    with pytest.raises(ValueError, match="buy-fail-9999"):
        OfflineBuyerAdapter()(case, load_evaluation_fixtures())


def test_offline_adapter_ignores_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(case for case in load_cases("dev") if case.case_id == "buy-srch-0002")
    fixtures = load_evaluation_fixtures()

    monkeypatch.setenv("EXPOSE_MAX", "5")
    monkeypatch.setenv("CATEGORY_FANOUT_MAX", "0")
    monkeypatch.setenv("EVAL_BUYER_K_LIST", "[1]")
    get_settings.cache_clear()
    constrained = OfflineBuyerAdapter()
    constrained_output = constrained(case, fixtures)
    constrained_report = evaluate(
        cases=[case],
        fixtures=fixtures,
        adapter=constrained,
    )

    monkeypatch.setenv("EXPOSE_MAX", "9")
    monkeypatch.setenv("CATEGORY_FANOUT_MAX", "5")
    monkeypatch.setenv("EVAL_BUYER_K_LIST", "[5]")
    get_settings.cache_clear()
    permissive = OfflineBuyerAdapter()
    permissive_output = permissive(case, fixtures)
    permissive_report = evaluate(
        cases=[case],
        fixtures=fixtures,
        adapter=permissive,
    )
    get_settings.cache_clear()

    assert constrained.settings.expose_max == permissive.settings.expose_max == 9
    assert constrained.settings.category_fanout_max == permissive.settings.category_fanout_max == 5
    assert constrained_report["kList"] == permissive_report["kList"] == [5, 10, 20]
    assert constrained_output == permissive_output
    assert constrained_report == permissive_report
