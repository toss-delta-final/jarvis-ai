"""기본 pytest에서 실행되는 구매자 추천 critical subset gate."""

import pytest

from evals.goldenset.loader import load_cases
from evals.metrics.runner import assert_pr_gate, critical_cases, evaluate
from evals.metrics.color_expansion import evaluate_color_expansion


@pytest.mark.eval
def test_critical_dev_subset_has_no_deterministic_price_violation() -> None:
    cases = critical_cases(load_cases("dev"))
    assert cases
    assert "buy-cmap-0004" in {case.case_id for case in cases}
    report = evaluate(cases=cases)

    assert_pr_gate(report)
    assert not [
        row for row in report["violations"] if row["constraint"] in report["prGateConstraints"]
    ]


@pytest.mark.eval
def test_pr_gate_catches_price_violation_when_decompose_drops_price_axis() -> None:
    """#370 결정 01 — 위반 네거티브 채널의 존재 이유를 직접 증명한다.

    정상 경로에서는 decompose가 뽑은 priceMax가 Spring 요청에 실려(`spring_client.py`)
    `_CaseTransport` mock(#370 harness 가격 필터)이 주입된 price_violation 후보를 걸러낸다
    (위 테스트가 이를 확인한다). 이 테스트는 그 안전망이 없는 상황 — 실모델이 가격 축을
    decompose에서 놓치는 경우 — 를 그대로 시뮬레이션한다. `expectedFilters`에서 가격 축만
    빼면(케이스의 `hardConstraints.priceMax`는 그대로 두어 정답 판정은 안 바뀐다) 요청에
    `maxPrice`가 안 실리므로 mock이 걸러주지 않고, 주입된 위반 후보가 노출까지 살아남아
    `assert_pr_gate`가 실패해야 한다 — 실패하지 않으면 이 채널이 아무것도 재지 못하는
    공허한 채널이라는 뜻이다.
    """
    case = next(case for case in load_cases("dev") if case.case_id == "buy-budg-0002")
    assert case.hard_constraints.price_max is not None
    filters_without_price = dict(case.expected_filters)
    filters_without_price.pop("priceMax", None)
    filters_without_price.pop("priceMin", None)
    decompose_dropped_price = case.model_copy(update={"expected_filters": filters_without_price})

    report = evaluate(cases=[decompose_dropped_price])

    with pytest.raises(AssertionError, match="가격 hard constraint 위반"):
        assert_pr_gate(report)
    gated = [
        row for row in report["violations"] if row["constraint"] in report["prGateConstraints"]
    ]
    assert gated  # 주입된 price_violation 후보가 실제로 노출·검출됐다


@pytest.mark.eval
def test_color_synonym_expansion_ab_channel_is_not_vacuous() -> None:
    """#474: 색상 mock·확장 와이어가 꺼지면 고유어 정답이 실제로 사라져야 한다."""
    report = evaluate_color_expansion()
    rows = report["cases"]
    native = [row for row in rows if not row["isCanonical"]]
    canonical = [row for row in rows if row["isCanonical"]]

    assert all(row["recallAt10"]["on"] > row["recallAt10"]["off"] for row in native)
    assert all(set(row["offProductIds"]).isdisjoint(row["relevantProductIds"]) for row in native)
    assert all(row["onProductIds"] == row["offProductIds"] for row in canonical)


@pytest.mark.eval
def test_color_synonym_on_arm_sends_expanded_repeated_color_params() -> None:
    """#474: recall 회복은 color가 사라진 우연이 아니라 배열 확장의 결과여야 한다."""
    from evals.metrics.harness import OfflineBuyerAdapter
    from evals.metrics.runner import load_evaluation_fixtures

    case = next(case for case in load_cases("dev") if case.case_id == "buy-colr-0001")
    adapter = OfflineBuyerAdapter(color_expansion=True)
    adapter(case, load_evaluation_fixtures())
    request = next(
        item for item in adapter.last_requests if item["path"] == "/internal/products/search"
    )
    assert len(request["query"]["color"]) >= 2
    assert "네이비" in request["query"]["color"]
