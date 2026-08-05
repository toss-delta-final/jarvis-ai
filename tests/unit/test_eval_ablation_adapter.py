from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from evals.ablation.single_call import SINGLE_CALL_SYSTEM_PROMPT, SingleCallBuyerAdapter
from evals.goldenset.loader import load_cases
from evals.metrics.runner import load_evaluation_fixtures
from evals.metrics.settings import EvaluationSettings


class _FakeLLM:
    def __init__(self, payload: dict[str, object] | None = None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return json.dumps(self.payload, ensure_ascii=False) if self.payload is not None else "not-json"


def _nonempty_case():
    fixtures = load_evaluation_fixtures()
    case = next(
        case
        for case in load_cases("dev")
        if len(fixtures.search_responses[case.search_fixture_id]["productIds"]) >= 2
    )
    return case, fixtures


def test_single_call_parses_filters_removes_outside_ids_caps_and_preserves_reasons() -> None:
    case, fixtures = _nonempty_case()
    eligible = fixtures.search_responses[case.search_fixture_id]["productIds"]
    fake = _FakeLLM(
        {
            "extractedFilters": {
                "priceMax": 50000,
                "semanticQuery": "축구공",
                "limit": 30,
                "excludeProductIds": [],
                "keyword": "",
            },
            "ranked": [
                {"productId": 999999999999, "reason": "후보 밖"},
                {"productId": eligible[0], "reason": "첫 번째"},
                {"productId": eligible[1], "reason": "두 번째"},
            ],
        }
    )
    adapter = SingleCallBuyerAdapter(
        fake,
        settings=EvaluationSettings(expose_min=1, expose_max=1),
        model_config={"provider": "fake"},
    )

    output = adapter(case, fixtures)

    assert output["rankedProductIds"] == [eligible[0]]
    assert output["extractedFilters"] == {"priceMax": 50000, "semanticQuery": "축구공"}
    assert output["reasonsByProductId"] == {str(eligible[0]): "첫 번째"}
    assert output["hardFailure"] is False
    assert len(fake.calls) == 1
    assert fake.calls[0]["tier"] == "smart"
    assert fake.calls[0]["system"] == SINGLE_CALL_SYSTEM_PROMPT
    assert 'brand는 문자열 배열(예: ["농심"])' in SINGLE_CALL_SYSTEM_PROMPT
    assert 'attrConditions는 문자열 값 객체(예: {"규격": "A4"})' in SINGLE_CALL_SYSTEM_PROMPT
    assert "priceMin·priceMax·ratingMin은 숫자" in SINGLE_CALL_SYSTEM_PROMPT
    prompt = str(fake.calls[0]["user"])
    assert "ratingLevel" in prompt and "reviewLevel" in prompt and "priceLevel" in prompt
    assert '"rating"' not in prompt and '"reviewCount"' not in prompt


def test_single_call_parse_failure_and_empty_ranking_are_hard_failures() -> None:
    case, fixtures = _nonempty_case()
    parse_failure = SingleCallBuyerAdapter(_FakeLLM())(case, fixtures)
    assert parse_failure["hardFailure"] is True
    assert parse_failure["rankedProductIds"] == []
    assert str(parse_failure["failureReason"]).startswith("LLMError:")

    empty = SingleCallBuyerAdapter(
        _FakeLLM({"extractedFilters": {}, "ranked": []})
    )(case, fixtures)
    assert empty["hardFailure"] is True
    assert empty["failureReason"] == "emptyPush"


def test_single_call_expected_zero_candidates_allows_empty_ranking() -> None:
    fixtures = load_evaluation_fixtures()
    case = next(
        case
        for case in load_cases("dev")
        if not fixtures.search_responses[case.search_fixture_id]["productIds"]
    )
    output = SingleCallBuyerAdapter(
        _FakeLLM({"extractedFilters": {}, "ranked": []})
    )(case, fixtures)
    assert output["expectedZeroCandidates"] is True
    assert output["hardFailure"] is False
    assert output["failureReason"] is None


def test_single_call_uses_fixture_ids_for_expected_zero_semantics() -> None:
    case, fixtures = _nonempty_case()
    product_ids = fixtures.search_responses[case.search_fixture_id]["productIds"]
    missing_catalog = {
        key: value for key, value in fixtures.catalog.items() if int(key) not in product_ids
    }
    output = SingleCallBuyerAdapter(
        _FakeLLM({"extractedFilters": {}, "ranked": []})
    )(case, replace(fixtures, catalog=missing_catalog))

    assert output["expectedZeroCandidates"] is False
    assert output["hardFailure"] is True
    assert output["failureReason"] == "emptyPush"


def test_single_call_lenient_filters_promote_brand_and_warn_for_bad_attr_conditions() -> None:
    case, fixtures = _nonempty_case()
    eligible = fixtures.search_responses[case.search_fixture_id]["productIds"]
    output = SingleCallBuyerAdapter(
        _FakeLLM(
            {
                "extractedFilters": {
                    "brand": "농심",
                    "attrConditions": "A4 규격, 총 2500매",
                    "priceMax": 50000,
                },
                "ranked": [{"productId": eligible[0], "reason": "후보 안"}],
            }
        )
    )(case, fixtures)

    assert output["extractedFilters"] == {"brand": ["농심"], "priceMax": 50000}
    assert output["filterParseWarnings"] == [
        {"field": "attrConditions", "reason": "invalidTypeOrValue"}
    ]
    assert output["hardFailure"] is False


def test_lenient_filter_regression_probe_fails_when_brand_promotion_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.ablation import single_call

    case, fixtures = _nonempty_case()
    eligible = fixtures.search_responses[case.search_fixture_id]["productIds"]
    monkeypatch.setattr(
        single_call,
        "_normalize_filter_value",
        lambda field, value: value,
    )
    output = SingleCallBuyerAdapter(
        _FakeLLM(
            {
                "extractedFilters": {"brand": "농심"},
                "ranked": [{"productId": eligible[0], "reason": "후보 안"}],
            }
        )
    )(case, fixtures)

    with pytest.raises(AssertionError):
        assert output["extractedFilters"].get("brand") == ["농심"]


def test_single_call_uses_recording_scope_when_supported() -> None:
    class ScopedFake(_FakeLLM):
        def __init__(self) -> None:
            super().__init__({"extractedFilters": {}, "ranked": []})
            self.scopes: list[str] = []

        @contextmanager
        def scope(self, label: str):
            self.scopes.append(label)
            yield

    fixtures = load_evaluation_fixtures()
    case = next(
        case
        for case in load_cases("dev")
        if not fixtures.search_responses[case.search_fixture_id]["productIds"]
    )
    fake = ScopedFake()
    SingleCallBuyerAdapter(fake)(case, fixtures)
    assert fake.scopes == ["single_call"]


def test_outside_candidate_regression_probe_makes_invariant_assertion_fail(
    monkeypatch,
) -> None:
    from evals.ablation import single_call

    case, fixtures = _nonempty_case()
    eligible = fixtures.search_responses[case.search_fixture_id]["productIds"]
    outside = 999999999999
    fake = _FakeLLM(
        {
            "extractedFilters": {},
            "ranked": [
                {"productId": outside, "reason": "후보 밖"},
                {"productId": eligible[0], "reason": "후보 안"},
            ],
        }
    )
    monkeypatch.setattr(single_call, "_candidate_is_allowed", lambda *args: True)
    output = SingleCallBuyerAdapter(fake)(case, fixtures)

    with pytest.raises(AssertionError):
        assert output["rankedProductIds"] == [eligible[0]]
