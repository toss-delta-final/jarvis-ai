"""기존 scoring adapter를 반복 실행 공통 schema에 맞추는 얇은 래퍼."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures
from evals.scoring.adapter import ScoringBuyerAdapter


class _NoCallLLM:
    """결정론 arm의 provider call이 없음을 run_repeats에 명시한다."""

    calls: list[dict[str, Any]] = []


class ScoringAblationAdapter:
    """ScoringBuyerAdapter 결과에 failure·latency·호출 없음 메타데이터를 보탠다."""

    def __init__(self, adapter: ScoringBuyerAdapter | None = None) -> None:
        self.adapter = adapter or ScoringBuyerAdapter()
        self.llm = _NoCallLLM()
        self.model_config = dict(self.adapter.model_config)
        self.last_output: dict[str, Any] = {}

    def __call__(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        started = perf_counter()
        fixture = fixtures.search_responses.get(case.search_fixture_id or "", {})
        expected_zero_candidates = not fixture.get("productIds")
        failure_reason: str | None = None
        ranked: list[int] = []
        filters: dict[str, Any] = {}
        try:
            output = self.adapter(case, fixtures)
            ranked = [int(product_id) for product_id in output.get("rankedProductIds", [])]
            filters = dict(output.get("extractedFilters", {}))
            if not ranked and not expected_zero_candidates:
                failure_reason = "emptyPush"
        except Exception as exc:  # noqa: BLE001 - 케이스 실패를 전체 실험과 격리
            failure_reason = f"{type(exc).__name__}:{exc}"
        self.last_output = {
            "rankedProductIds": ranked if failure_reason is None else [],
            "extractedFilters": filters,
            "modelConfig": dict(self.model_config),
            "providerCalls": [],
            "latencyMs": int(round((perf_counter() - started) * 1000)),
            "hardFailure": failure_reason is not None,
            "failureReason": failure_reason,
            "expectedZeroCandidates": expected_zero_candidates,
        }
        return self.last_output
