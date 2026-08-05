"""분류기 팔 — 전송 실패 vs 모델 출력 실패 (#281 TASK-3-CORRECTION).

이 정정이 고치는 결함: `classify_need_priorities` 는 429·타임아웃 같은 **전송 실패**와
**모델이 파싱 불가 출력을 냈다**를 구분 없이 `None` 하나로 삼킨다. 초판은 둘 다 "표본"으로 세서
페이서가 조금만 어긋나도 429 가 전부 "분류기 실패"로 집계되는 #240 의 실패 양식을 재현했다.
여기 테스트는 두 경우가 **서로 다른 카운터**로 가는지를 기계적으로 증명한다. 전부 가짜 LLM이라
CI 에서 API 콜이 0이다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.core.llm import LLMError
from evals.priority_probe.client import RawCapture
from evals.priority_probe.runner import run_cell_classifier
from evals.priority_probe.schema import PriorityCell


def _cell() -> PriorityCell:
    return PriorityCell(
        cell_id="c1",
        utterance="테스트 발화",
        needs=["a", "b", "c"],
        expected_priorities=[1, 2, 3],
        rationale="분류기 재시도 테스트용 근거 문장입니다 스무 자 이상.",
    )


class _ScriptedDelegate:
    """`complete()` 를 스크립트대로 굴리는 최소 delegate. `RawCapture` 가 안쪽에서 감싼다."""

    def __init__(self, script: list[Exception | str]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        self.calls += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _settings():
    return get_settings()


async def _run(delegate: _ScriptedDelegate) -> tuple:
    capture = RawCapture(delegate)

    class _Paced:
        async def complete(self, **kwargs):  # noqa: ANN003
            return await capture.complete(**kwargs)

    result = await run_cell_classifier(
        llm=_Paced(),
        capture=capture,
        cell=_cell(),
        n=1,
        settings=_settings(),
        attempt_multiplier=10,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    return result, capture


async def test_transport_exception_is_retried_not_counted_as_a_sample() -> None:
    """`complete()` 가 예외를 던지면 **표본에 안 들어가고** 재시도된다."""
    delegate = _ScriptedDelegate([LLMError("429 rate limited"), '{"priorities": [1, 2, 3]}'])
    result, _capture = await _run(delegate)

    assert len(result.samples) == 1  # 재시도 끝에 결국 1개를 채웠다
    assert result.samples[0].priorities == (1, 2, 3)
    assert len(result.failures) == 1  # 전송 실패 1건이 재시도 사유로 남는다
    assert result.failures[0].error_type == "LLMError"
    assert delegate.calls == 2  # 실패 1회 + 성공 1회 = 실제 provider 호출 2회


async def test_malformed_model_output_is_counted_as_a_sample_not_retried() -> None:
    """`complete()` 는 성공했는데 파싱 불가 출력이면 — **표본**이다(재시도하지 않는다)."""
    delegate = _ScriptedDelegate(["이건 JSON 이 아닙니다"])
    result, capture = await _run(delegate)

    assert len(result.samples) == 1  # 재시도 없이 바로 표본으로 잡힌다
    assert result.samples[0].priorities == (None, None, None)  # 분류기가 None 으로 떨어뜨렸다
    assert result.failures == []  # 전송 실패가 아니므로 failures 에는 안 남는다
    assert delegate.calls == 1  # 재시도가 없었다는 직접 증거
    assert capture.last_outcome == "ok"  # complete() 자체는 성공했다


async def test_both_failure_modes_go_to_different_counters() -> None:
    """전송 실패와 모델 출력 실패가 **같은 셀 안에서** 섞여도 서로 다른 카운터로 간다."""
    delegate = _ScriptedDelegate(
        [
            LLMError("timeout"),  # 전송 실패 — failures 로
            "형식이 깨진 응답",  # 모델 출력 실패 — samples 로(None)
            '{"priorities": [1, 2, 3]}',  # 정상 — samples 로
        ]
    )
    capture = RawCapture(delegate)

    class _Paced:
        async def complete(self, **kwargs):  # noqa: ANN003
            return await capture.complete(**kwargs)

    result = await run_cell_classifier(
        llm=_Paced(),
        capture=capture,
        cell=_cell(),
        n=2,  # 표본 2개(모델 출력 실패 1 + 정상 1)를 채운다
        settings=_settings(),
        attempt_multiplier=10,
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    assert len(result.failures) == 1  # 전송 실패는 딱 1건
    assert len(result.samples) == 2  # 표본은 모델 출력 실패 포함 2건
    assert result.samples[0].priorities == (None, None, None)
    assert result.samples[1].priorities == (1, 2, 3)


async def test_budget_exceeded_propagates_instead_of_retrying_forever() -> None:
    """`BudgetExceeded` 는 전송 실패로 재시도되면 예산 가드가 무력화된다 — 그대로 다시 던진다."""
    from evals.model_eval.budget import BudgetExceeded

    delegate = _ScriptedDelegate([BudgetExceeded("maxCallsExceeded")])
    with pytest.raises(BudgetExceeded):
        await _run(delegate)
