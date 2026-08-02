"""기본 pytest에서 실행되는 구매자 추천 critical subset gate."""

import pytest

from evals.goldenset.loader import load_cases
from evals.metrics.runner import assert_pr_gate, critical_cases, evaluate


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
