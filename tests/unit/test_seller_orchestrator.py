"""app/agents/seller/orchestrator.py 팬아웃 검증 (3-3) — 실 LLM 없음, 스텁 에이전트 주입.

pytest-asyncio 미의존 — 동기 테스트 안에서 asyncio.run 으로 실행한다(이식성).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import get_args

import pytest
from langchain_core.messages import ToolMessage

from app.agents.seller import orchestrator
from app.agents.seller.context import SellerContext
from app.agents.seller.pipeline import ALL_WORKERS_FAILED_TOKEN, ResolvedPlan
from app.agents.seller.schemas import (
    ActionRecommendation,
    AnalysisFinding,
    AnalysisPlan,
    AnalysisScore,
    AnalysisType,
    ChartAxisPlan,
    ChartPlanSet,
    ChartPoint,
    ChartSeries,
    ChartSet,
    ChartSpec,
    RecommendationSet,
    ReportScore,
)
from app.core.llm import LLMNotConfigured


def _settings(timeout_s: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        seller_worker_timeout_s=timeout_s,
        seller_report_score_threshold=21,
        seller_report_max_retries=3,
        seller_recent_days_default=7,
        seller_period_max_days=731,  # #269 — run_analysis_pipeline 이 resolve_plan 에 넘긴다
        # ── 브랜치 분석 검증 (이슈 #242) ──
        seller_worker_max_retries=1,
        seller_analysis_score_threshold=21,
        seller_analysis_judge_timeout_s=timeout_s,
        seller_branch_deadline_s=160.0,  # config.py 기본값(PR 리뷰 반영)과 정합
        # ── 차트 레인 (이슈 #600) ──
        # graph(축 선언) 전용 타임아웃 — seller_worker_timeout_s 와 분리(09-CHART.md §8).
        seller_chart_agent_timeout_s=timeout_s,
        # 이 파일의 기존 테스트는 #600 이전에 작성돼 해석 에이전트를 스텁하지 않는다 —
        # 비활성으로 두어 run_chart_interpret 이 즉시 None 을 반환하고(고정 문구 폴백),
        # run_graph/차트 조립 계약만 그대로 검증한다. 해석 자체 회귀는
        # tests/unit/test_seller_pipeline.py·test_seller_workers.py 소관.
        seller_chart_interpret_enabled=False,
    )


_CTX = SellerContext(seller_id=7, brand_id=3)  # 계약 타입 = int (context.py, §2.6 숫자 신원)


def _plan(*analyses: str) -> ResolvedPlan:
    return ResolvedPlan(
        analyses=analyses,  # type: ignore[arg-type]
        date_from=dt.date(2026, 6, 1),
        date_to=dt.date(2026, 6, 30),
    )


def _finding(analysis_type: str) -> AnalysisFinding:
    return AnalysisFinding(
        analysis_type=analysis_type,  # type: ignore[arg-type]
        summary=f"{analysis_type} 정상 결과",
        evidence=["x=1"],
        severity="info",
    )


class _StubAgent:
    """create_agent 대역 — ainvoke 만 흉내 낸다(정상/예외/지연/무출력)."""

    def __init__(
        self,
        finding: AnalysisFinding | None = None,
        exc: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._finding = finding
        self._exc = exc
        self._delay_s = delay_s

    async def ainvoke(self, _input: dict, context: object = None) -> dict:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._exc is not None:
            raise self._exc
        # F2(evidence_grounded) 재료 — finding 의 근거를 도구 원출력으로 되돌려줘서
        # (실제로는 역방향이지만) 이 스텁이 만든 finding 이 브랜치 검증(F2)에서
        # "도구 출력에 없는 수치"로 오탐되지 않게 한다(이슈 #242).
        messages: list[object] = []
        if self._finding is not None:
            tool_text = " ".join([self._finding.summary, *self._finding.evidence])
            messages = [ToolMessage(content=tool_text, tool_call_id="stub")]
        return {"structured_response": self._finding, "messages": messages}


def _analysis_score(total_each: int = 8, feedback: str = "") -> AnalysisScore:
    """임계 21/30(각 축 8*3=24) 통과 기본값 — 브랜치 검증 스텁 재료."""
    return AnalysisScore(
        grounding=total_each, sufficiency=total_each, relevance=total_each, feedback=feedback
    )


class _AlwaysPassJudge:
    """analysis_judge 대역 — 팬아웃(워커 예외 3층) 테스트 기본값, 항상 통과 점수."""

    async def ainvoke(self, _input: dict, context: object = None) -> dict:
        return {"structured_response": _analysis_score(8)}


class _SeqJudge:
    """analysis_judge 대역 — 순차 행동(AnalysisScore 또는 예외)을 소비한다(재실행 테스트용)."""

    def __init__(self, behaviors: list[object]) -> None:
        self._behaviors = list(behaviors)
        self.received: list[str] = []

    async def ainvoke(self, agent_input: dict, context: object = None) -> dict:
        self.received.append(agent_input["messages"][0].content)
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return {"structured_response": behavior}


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    stubs: dict[str, _StubAgent],
    timeout_s: float = 5.0,
    judge: object | None = None,
) -> None:
    """WORKER_BUILDERS·analysis_judge·Settings 타임아웃을 스텁으로 교체한다.

    judge 미지정 시 항상 통과(_AlwaysPassJudge) — 팬아웃(3층) 테스트가 브랜치
    검증에 영향받지 않도록 하는 기본값이다.
    """
    for analysis_type, stub in stubs.items():
        monkeypatch.setitem(orchestrator.WORKER_BUILDERS, analysis_type, lambda s=stub: s)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings(timeout_s))
    judge_stub = judge if judge is not None else _AlwaysPassJudge()
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge_stub)


def _collect_emit() -> tuple[list[str], orchestrator.Emit]:
    tokens: list[str] = []

    async def emit(text: str) -> None:
        tokens.append(text)

    return tokens, emit


def test_worker_builders_cover_all_analysis_types() -> None:
    """레지스트리는 AnalysisType 전 값을 커버한다(배정표 실행판 누락 방지)."""
    assert set(orchestrator.WORKER_BUILDERS) == set(get_args(AnalysisType))


def test_run_branches_happy_path_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 2종 — VerifiedFinding 은 계획 순서·passed=True, 진행 token 은 유형별 방출."""
    _patch(
        monkeypatch,
        {
            "sales_anomaly": _StubAgent(finding=_finding("sales_anomaly")),
            "churn": _StubAgent(finding=_finding("churn")),
        },
    )
    tokens, emit = _collect_emit()

    verified = asyncio.run(
        orchestrator.run_branches("질문", _plan("sales_anomaly", "churn"), _CTX, emit=emit)
    )

    assert [vf.finding.analysis_type for vf in verified] == ["sales_anomaly", "churn"]
    assert all(vf.passed for vf in verified)
    assert tokens == ["매출 이상 분석 중…", "고객 이탈 분석 중…"]


def test_run_branches_partial_failure_becomes_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """1종 예외 → degrade finding(확보 실패·info·빈 evidence)으로 수렴, 파이프라인 계속."""
    _patch(
        monkeypatch,
        {
            "sales_anomaly": _StubAgent(finding=_finding("sales_anomaly")),
            "abuse": _StubAgent(exc=RuntimeError("boom")),
        },
    )
    _, emit = _collect_emit()

    verified = asyncio.run(
        orchestrator.run_branches("질문", _plan("sales_anomaly", "abuse"), _CTX, emit=emit)
    )

    degraded = verified[1]
    assert degraded.finding.analysis_type == "abuse"
    assert degraded.finding.severity == "info"
    assert "확보 실패" in degraded.finding.summary  # D3 탐지 문자열 유지
    assert degraded.finding.evidence == []
    assert degraded.passed is False
    assert degraded.degraded is True
    assert verified[0].finding.summary == "sales_anomaly 정상 결과"


def test_run_branches_configuration_error_is_not_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 미구성은 정상 finding이 섞여도 부분 실패로 흡수하지 않는다."""
    _patch(
        monkeypatch,
        {
            "sales_anomaly": _StubAgent(finding=_finding("sales_anomaly")),
            "abuse": _StubAgent(exc=LLMNotConfigured("openai key missing")),
        },
    )
    _, emit = _collect_emit()

    with pytest.raises(LLMNotConfigured):
        asyncio.run(
            orchestrator.run_branches("질문", _plan("sales_anomaly", "abuse"), _CTX, emit=emit)
        )


def test_run_branches_all_failed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """전부 예외 → AllWorkersFailedError(호출부가 사과 token 후 done, §7)."""
    _patch(
        monkeypatch,
        {
            "conversion": _StubAgent(exc=RuntimeError("a")),
            "behavior": _StubAgent(exc=RuntimeError("b")),
        },
    )
    _, emit = _collect_emit()

    with pytest.raises(orchestrator.AllWorkersFailedError):
        asyncio.run(
            orchestrator.run_branches("질문", _plan("conversion", "behavior"), _CTX, emit=emit)
        )


def test_run_branches_timeout_becomes_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """타임아웃 초과 워커는 '응답 시간 초과' degrade — 나머지는 정상 수렴."""
    _patch(
        monkeypatch,
        {
            "sales_anomaly": _StubAgent(finding=_finding("sales_anomaly")),
            "churn": _StubAgent(finding=_finding("churn"), delay_s=0.2),
        },
        timeout_s=0.05,
    )
    _, emit = _collect_emit()

    verified = asyncio.run(
        orchestrator.run_branches("질문", _plan("sales_anomaly", "churn"), _CTX, emit=emit)
    )

    assert "응답 시간 초과" in verified[1].finding.summary
    assert verified[0].finding.summary == "sales_anomaly 정상 결과"


def test_run_branches_missing_structured_response_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """structured_response 누락(None)도 내부 오류 degrade 로 수렴한다(3층 예외 경로)."""
    _patch(
        monkeypatch,
        {
            "sales_anomaly": _StubAgent(finding=None),
            "churn": _StubAgent(finding=_finding("churn")),
        },
    )
    _, emit = _collect_emit()

    verified = asyncio.run(
        orchestrator.run_branches("질문", _plan("sales_anomaly", "churn"), _CTX, emit=emit)
    )

    assert "내부 오류" in verified[0].finding.summary
    assert verified[1].finding.summary == "churn 정상 결과"


# ── 브랜치 분석 검증 (F1~F3 + analysis_judge, 이슈 #242) ───────────────────────


def test_run_one_branch_passes_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """F 통과 + 24/30 → 1회에 passed=True, attempts=1."""
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("sales_anomaly")),
    )
    judge = _SeqJudge([_analysis_score(8)])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is True
    assert verified.attempts == 1
    assert verified.degraded is False
    assert verified.failed_checks == ()


def test_run_one_branch_retries_on_judge_low_score_then_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1회차 judge 미달(F 는 통과) → 재실행 입력에 feedback 주입, 2회차 통과."""

    class _WorkerSeq:
        def __init__(self) -> None:
            self.received: list[str] = []
            self._calls = 0

        async def ainvoke(self, agent_input: dict, context: object = None) -> dict:
            self.received.append(agent_input["messages"][0].content)
            self._calls += 1
            return {"structured_response": _finding("sales_anomaly")}

    worker = _WorkerSeq()
    monkeypatch.setitem(orchestrator.WORKER_BUILDERS, "sales_anomaly", lambda: worker)
    judge = _SeqJudge(
        [_analysis_score(5, feedback="근거 수치를 더 구체화할 것"), _analysis_score(8)]
    )
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is True
    assert verified.attempts == 2
    assert worker._calls == 2
    assert "근거 수치를 더 구체화할 것" in worker.received[1]
    assert "[이전 분석 결과]" in worker.received[1]


def test_run_one_branch_f_failure_triggers_retry_then_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1회차 F3(type_match) 미달 → 재실행, 2회차 올바른 유형으로 통과."""

    class _WrongThenRightWorker:
        def __init__(self) -> None:
            self._calls = 0

        async def ainvoke(self, agent_input: dict, context: object = None) -> dict:
            self._calls += 1
            wrong = self._calls == 1
            return {"structured_response": _finding("conversion" if wrong else "sales_anomaly")}

    worker = _WrongThenRightWorker()
    monkeypatch.setitem(orchestrator.WORKER_BUILDERS, "sales_anomaly", lambda: worker)
    judge = _SeqJudge([_analysis_score(8), _analysis_score(8)])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is True
    assert verified.attempts == 2
    assert verified.finding.analysis_type == "sales_anomaly"


def test_run_one_branch_degrades_after_f_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """재실행(≤1회) 후에도 F 미달 잔존 → degrade finding 으로 강등(passed=False)."""
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("conversion")),  # F3 상시 미달(유형 불일치)
    )
    judge = _SeqJudge([_analysis_score(8), _analysis_score(8)])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is False
    assert verified.degraded is True
    assert verified.finding.severity == "info"
    assert verified.finding.evidence == []
    assert "분석 검증 미달" in verified.finding.summary
    assert any("analysis_type 불일치" in reason for reason in verified.failed_checks)


def test_run_one_branch_adopts_unverified_when_only_judge_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F 는 항상 통과하나 judge 만 계속 미달 → 미달 채택(passed=False, degraded=False)."""
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("sales_anomaly")),
    )
    judge = _SeqJudge([_analysis_score(5, feedback="부족"), _analysis_score(5, feedback="부족")])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is False
    assert verified.degraded is False  # F 는 통과했으므로 강등하지 않는다
    assert verified.finding.analysis_type == "sales_anomaly"  # 원 finding 유지
    assert verified.last_score is not None and verified.last_score.total == 15


def test_run_one_branch_judge_crash_adopts_current_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """judge 장애(예외) → 재실행 없이 현재 finding 미검증 채택(Q2 와 동일 철학)."""
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("sales_anomaly")),
    )
    judge = _SeqJudge([RuntimeError("judge down")])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is False
    assert verified.last_score is None
    assert verified.attempts == 1


def test_run_one_branch_judge_crash_with_f_failure_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """judge 장애 + F 미달(F3 유형 불일치) 동시 발생 → 미검증 채택이 아니라 강등한다.

    judge 만 장애 나고 F 는 통과한 경우(위 테스트)와 달리, F 미달이 남아있는데
    judge 장애로 "미검증 채택"해버리면 이 PR 의 핵심(도구 출력 ⊇ finding 근거
    사슬 검증)이 judge 장애 한 번으로 무력화된다(PR 리뷰 지적 사항 반영).
    """
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("conversion")),  # F3 상시 미달(유형 불일치)
    )
    judge = _SeqJudge([RuntimeError("judge down")])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, _settings()
        )
    )

    assert verified.passed is False
    assert verified.degraded is True
    assert verified.finding.severity == "info"
    assert verified.finding.evidence == []
    assert "분석 검증 미달" in verified.finding.summary
    assert any("analysis_type 불일치" in reason for reason in verified.failed_checks)
    assert verified.last_score is None
    assert verified.attempts == 1  # judge 장애는 재실행하지 않는다


def test_run_one_branch_gives_up_retry_when_deadline_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """브랜치 예산 초과 → 재실행 포기, 직전 결과를 그대로 채택(강등 아님, §9-R1)."""
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("sales_anomaly")),
    )
    judge = _SeqJudge([_analysis_score(5, feedback="부족")])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)
    settings = _settings()
    # 음수로 둬 시각 오차(나노초 단위 경합)와 무관하게 예산이 항상 소진된 상태로 만든다.
    settings.seller_branch_deadline_s = -1.0

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, settings
        )
    )

    assert verified.attempts == 1  # 재실행 없음
    assert verified.passed is False
    assert verified.degraded is False  # F 는 통과 — judge 미달만으로는 강등하지 않는다


def test_run_one_branch_skips_retry_when_remaining_budget_below_retry_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """데드라인이 아직 안 지났어도 재실행 1회(worker+judge)를 완주할 잔여 예산이
    없으면 재실행을 포기한다(PR 리뷰 반영).

    기존엔 "time.monotonic() < deadline" 만 봐서, 데드라인 직전이라도 재실행을
    시작은 하고(도중에 끝내 예산을 넘기는) 경우를 못 막았다. 잔여 예산(=deadline
    까지 남은 시간)이 worker_timeout+judge_timeout 보다 작으면 처음부터 시작하지
    않아야 한다.
    """
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("sales_anomaly")),
    )
    judge = _SeqJudge([_analysis_score(5, feedback="부족")])
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: judge)
    settings = _settings(timeout_s=10.0)  # retry_cycle_cost_s = 10 + 10 = 20
    settings.seller_branch_deadline_s = 5.0  # 잔여 예산(~5s) < retry_cycle_cost_s(20s)

    verified = asyncio.run(
        orchestrator._run_one_branch(
            "sales_anomaly", "질문", _plan("sales_anomaly"), _CTX, settings
        )
    )

    assert verified.attempts == 1  # 재실행 시도 자체가 없었다
    assert verified.passed is False
    assert verified.degraded is False  # F 는 통과 — judge 미달만으로는 강등하지 않는다


def test_run_branches_f_failure_alone_does_not_trigger_all_workers_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """전 브랜치 F 미달(예외 아님) → AllWorkersFailedError 오발동하지 않는다(R3)."""
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_finding("conversion")),  # F3 상시 미달
    )
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: _AlwaysPassJudge())
    _, emit = _collect_emit()

    verified = asyncio.run(
        orchestrator.run_branches("질문", _plan("sales_anomaly"), _CTX, emit=emit)
    )

    assert len(verified) == 1
    assert verified[0].passed is False  # F 미달은 결과에 반영되지만
    # AllWorkersFailedError 는 raise 되지 않는다(위에서 예외 없이 도달했다는 사실 자체가 증거).


# ── 검증 루프 (3-4) — write_verified_report ────────────────────────────────────


class _SeqAgent:
    """호출 순서대로 행동(응답 dict 또는 예외)을 소비하는 스텁 — 입력 메시지를 기록한다."""

    def __init__(self, behaviors: list[object]) -> None:
        self._behaviors = list(behaviors)
        self.received: list[str] = []

    async def ainvoke(self, agent_input: dict, context: object = None) -> dict:
        self.received.append(agent_input["messages"][0].content)
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior  # type: ignore[return-value]


def _report_response(text: str) -> dict:
    return {"messages": [SimpleNamespace(content=text)]}


def _score(total_each: int, feedback: str = "") -> dict:
    return {
        "structured_response": ReportScore(
            accuracy=total_each,
            completeness=total_each,
            clarity=total_each,
            feedback=feedback,
        )
    }


_FINDINGS = [
    AnalysisFinding(
        analysis_type="sales_anomaly",
        summary="6월 12일 매출이 평균 대비 42.1% 급락했다.",
        evidence=["06-12 매출 180,000원 (평균 310,000원)"],
        severity="warning",
    )
]

_GROUNDED = "매출이 180,000원으로 평균 310,000원 대비 42.1% 급락했습니다."


def _patch_loop(
    monkeypatch: pytest.MonkeyPatch, report_agent: _SeqAgent, judge_agent: _SeqAgent
) -> None:
    monkeypatch.setattr(orchestrator, "build_report_agent", lambda: report_agent)
    monkeypatch.setattr(orchestrator, "build_report_judge", lambda: judge_agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())


def test_verified_report_passes_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """결정론 통과 + 24/30 → 1회에 passed=True, report/verify token 이 각 1회."""
    report_agent = _SeqAgent([_report_response(_GROUNDED)])
    judge_agent = _SeqAgent([_score(8)])
    _patch_loop(monkeypatch, report_agent, judge_agent)
    tokens, emit = _collect_emit()

    verified = asyncio.run(orchestrator.write_verified_report(_FINDINGS, _CTX, emit=emit))

    assert verified.passed is True
    assert verified.attempts == 1
    assert verified.report == _GROUNDED
    assert tokens == ["보고서를 작성하고 있습니다…", "보고서를 검증하고 있습니다…"]


def test_verified_report_rewrites_with_combined_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    """1회차 미달(결정론 환각 + 낮은 점수) → 재작성 입력에 결정론 사유와 judge
    feedback 이 합산 주입되고, 2회차에 통과한다(2026-07-18 확정 — 합산 재작성)."""
    hallucinated = "매출이 999,999원으로 급락했습니다."
    report_agent = _SeqAgent([_report_response(hallucinated), _report_response(_GROUNDED)])
    judge_agent = _SeqAgent([_score(5, feedback="근거 수치를 인용할 것"), _score(8)])
    _patch_loop(monkeypatch, report_agent, judge_agent)
    _, emit = _collect_emit()

    verified = asyncio.run(orchestrator.write_verified_report(_FINDINGS, _CTX, emit=emit))

    assert verified.passed is True
    assert verified.attempts == 2
    rewrite_message = report_agent.received[1]
    assert "999999" in rewrite_message  # 결정론(D2) 실패 사유
    assert "근거 수치를 인용할 것" in rewrite_message  # judge feedback
    assert "[이전 보고서]" in rewrite_message


def test_verified_report_adopts_last_after_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """3회 전부 미달 → 마지막 보고서 채택 + passed=False (§7 degrade)."""
    report_agent = _SeqAgent([_report_response(_GROUNDED)] * 3)
    judge_agent = _SeqAgent([_score(5, feedback="부족")] * 3)
    _patch_loop(monkeypatch, report_agent, judge_agent)
    _, emit = _collect_emit()

    verified = asyncio.run(orchestrator.write_verified_report(_FINDINGS, _CTX, emit=emit))

    assert verified.passed is False
    assert verified.attempts == 3
    assert verified.report == _GROUNDED
    assert verified.last_score is not None and verified.last_score.total == 15


def test_verified_report_rewrite_crash_adopts_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    """재작성(2회차) LLM 장애 → 1회차 보고서 미달 채택(Q2 결정, 추후 변경 가능)."""
    report_agent = _SeqAgent([_report_response(_GROUNDED), RuntimeError("llm down")])
    judge_agent = _SeqAgent([_score(5, feedback="부족")])
    _patch_loop(monkeypatch, report_agent, judge_agent)
    _, emit = _collect_emit()

    verified = asyncio.run(orchestrator.write_verified_report(_FINDINGS, _CTX, emit=emit))

    assert verified.passed is False
    assert verified.report == _GROUNDED


def test_verified_report_first_attempt_crash_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """1차 작성부터 실패 → 내보낼 보고서가 없어 예외 전파(호출부 사과 경로, Q2)."""
    report_agent = _SeqAgent([RuntimeError("llm down")])
    judge_agent = _SeqAgent([])
    _patch_loop(monkeypatch, report_agent, judge_agent)
    _, emit = _collect_emit()

    with pytest.raises(RuntimeError):
        asyncio.run(orchestrator.write_verified_report(_FINDINGS, _CTX, emit=emit))


def test_verified_report_judge_crash_adopts_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """judge 장애 → 현재 보고서를 미검증 채택(passed=False, Q2)."""
    report_agent = _SeqAgent([_report_response(_GROUNDED)])
    judge_agent = _SeqAgent([RuntimeError("judge down")])
    _patch_loop(monkeypatch, report_agent, judge_agent)
    _, emit = _collect_emit()

    verified = asyncio.run(orchestrator.write_verified_report(_FINDINGS, _CTX, emit=emit))

    assert verified.passed is False
    assert verified.report == _GROUNDED
    assert verified.last_score is None


# ── recommend + 파이프라인 통합 (3-5) ──────────────────────────────────────────

_REC_SET = RecommendationSet(
    recommendations=[
        ActionRecommendation(
            action_type="price_adjust",
            product_id=101,
            title="감귤청 가격 10% 인하",
            rationale="42.1% 급락",
            expected_effect="전환율 회복",
        )
    ],
    summary="가격 중심 1건",
)


def test_run_recommend_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 — RecommendationSet 반환 + recommend 진행 token 방출."""
    agent = _SeqAgent([{"structured_response": _REC_SET}])
    monkeypatch.setattr(orchestrator, "build_recommend_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    tokens, emit = _collect_emit()

    result = asyncio.run(orchestrator.run_recommend(_FINDINGS, _GROUNDED, _CTX, emit=emit))

    assert result is _REC_SET
    assert tokens == ["개선 방안을 정리하고 있습니다…"]
    assert "[검증된 보고서]" in agent.received[0]


def test_run_recommend_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """추천 실패(예외·C2 ValidationError 포함) → 빈 추천으로 계속(보고서 보호)."""
    agent = _SeqAgent([RuntimeError("boom")])
    monkeypatch.setattr(orchestrator, "build_recommend_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    _, emit = _collect_emit()

    result = asyncio.run(orchestrator.run_recommend(_FINDINGS, _GROUNDED, _CTX, emit=emit))

    assert result.recommendations == []


# ── graph (5단계, 이슈 #242 → #504 재설계) — 축 선언(LLM) + 좌표 조립(charts.py) ──
#
# 좌표 조립 자체(14조합 레지스트리·버킷·절단)는 tests/unit/test_seller_charts.py 소관 —
# 여기서는 오케스트레이션 계약만 본다: LLM 실패 강등, 기간 오류 시 LLM 콜 0회,
# build_charts 위임 인자, 예외 불전파(C2 대칭).


def _axis_plan(x: str = "date", y: str = "sales") -> ChartPlanSet:
    return ChartPlanSet(charts=[ChartAxisPlan(x_axis=x, y_axis=y)])  # type: ignore[arg-type]


def _built_chart(title: str = "일별 매출 추이") -> ChartSpec:
    return ChartSpec(
        title=title,
        chart_type="line",
        unit="KRW",
        aggregate="sum",
        series=[ChartSeries(label="매출", points=[ChartPoint(x="06-12", y=180000)])],
    )


def _fake_build_charts(
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[ChartSet, list[object]] | Exception,
) -> list[dict]:
    """charts.build_charts 대역 — 호출 인자를 수집하고 준비된 결과/예외를 돌려준다."""
    calls: list[dict] = []

    async def _fake(plans, *, brand_id, date_from, date_to):
        calls.append(
            {"plans": list(plans), "brand_id": brand_id, "date_from": date_from, "date_to": date_to}
        )
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(orchestrator.seller_charts, "build_charts", _fake)
    return calls


def test_run_graph_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 — LLM 축 선언 → build_charts 위임(브랜드·기간 전달), graph 진행 token 방출."""
    agent = _SeqAgent([{"structured_response": _axis_plan()}])
    monkeypatch.setattr(orchestrator, "build_graph_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    calls = _fake_build_charts(monkeypatch, (ChartSet(charts=[_built_chart()]), []))
    tokens, emit = _collect_emit()

    charts, unavailable = asyncio.run(
        orchestrator.run_graph(
            _FINDINGS, _GROUNDED, "지난달 매출 추이 보여줘", _plan("sales_anomaly"), _CTX, emit=emit
        )
    )

    assert [c.title for c in charts.charts] == ["일별 매출 추이"]
    assert unavailable == []
    assert tokens == ["차트를 만들고 있습니다…"]
    assert "[판매자 질문]\n지난달 매출 추이 보여줘" in agent.received[0]
    # 좌표 조립에 신원(brand_id)과 분석 기간이 그대로 위임된다.
    assert calls == [
        {
            "plans": _axis_plan().charts,
            "brand_id": _CTX.brand_id,
            "date_from": dt.date(2026, 6, 1),
            "date_to": dt.date(2026, 6, 30),
        }
    ]


def test_run_graph_uses_chart_period_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#504] 차트 전용 기간(chart_from/to)이 있으면 분석 기간 대신 그 기간으로 조립한다."""
    agent = _SeqAgent([{"structured_response": _axis_plan()}])
    monkeypatch.setattr(orchestrator, "build_graph_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    calls = _fake_build_charts(monkeypatch, (ChartSet(charts=[_built_chart()]), []))
    resolved = ResolvedPlan(
        analyses=("sales_anomaly",),
        date_from=dt.date(2026, 6, 1),
        date_to=dt.date(2026, 6, 30),
        wants_chart=True,
        chart_period_expr="최근 7일",
        chart_from=dt.date(2026, 7, 12),
        chart_to=dt.date(2026, 7, 18),
    )
    _, emit = _collect_emit()

    asyncio.run(orchestrator.run_graph(_FINDINGS, _GROUNDED, "질문", resolved, _CTX, emit=emit))

    assert calls[0]["date_from"] == dt.date(2026, 7, 12)
    assert calls[0]["date_to"] == dt.date(2026, 7, 18)


def test_run_graph_chart_period_error_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#504] 차트 기간 해석 실패 → LLM·Spring 콜 0회, chart_period_unclear 사유만 반환
    (보고서는 호출부에서 그대로 살아 나간다)."""

    def _boom_agent() -> object:
        raise AssertionError("chart_period_error 인데 build_graph_agent 가 호출됐다")

    monkeypatch.setattr(orchestrator, "build_graph_agent", _boom_agent)
    resolved = ResolvedPlan(
        analyses=("sales_anomaly",),
        date_from=dt.date(2026, 6, 1),
        date_to=dt.date(2026, 6, 30),
        wants_chart=True,
        chart_period_expr="작년 여름",
        chart_period_error="기간 표현을 해석하지 못했습니다.",
    )
    _, emit = _collect_emit()

    charts, unavailable = asyncio.run(
        orchestrator.run_graph(_FINDINGS, _GROUNDED, "질문", resolved, _CTX, emit=emit)
    )

    assert charts.charts == []
    assert [u.reason for u in unavailable] == ["chart_period_unclear"]
    assert "작년 여름" in unavailable[0].message


def test_run_graph_failure_degrades_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """graph LLM 실패(예외) → 빈 ChartSet + agent_failed 사유(C2 대칭, 보고서 불사)."""
    agent = _SeqAgent([RuntimeError("boom")])
    monkeypatch.setattr(orchestrator, "build_graph_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    _, emit = _collect_emit()

    charts, unavailable = asyncio.run(
        orchestrator.run_graph(
            _FINDINGS, _GROUNDED, "질문", _plan("sales_anomaly"), _CTX, emit=emit
        )
    )

    assert charts.charts == []
    assert [u.reason for u in unavailable] == ["agent_failed"]


def test_run_graph_build_charts_exception_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_charts 자체가 예외를 던져도 run_graph 밖으로 새지 않는다 — source_failed 강등
    (asyncio.gather(run_recommend, run_graph)에 return_exceptions 이 없어, 새면 이미
    성공한 recommend·검증된 보고서까지 사과 응답이 된다)."""
    agent = _SeqAgent([{"structured_response": _axis_plan()}])
    monkeypatch.setattr(orchestrator, "build_graph_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    _fake_build_charts(monkeypatch, ValueError("조립기 결함"))
    _, emit = _collect_emit()

    charts, unavailable = asyncio.run(
        orchestrator.run_graph(
            _FINDINGS, _GROUNDED, "질문", _plan("sales_anomaly"), _CTX, emit=emit
        )
    )

    assert charts.charts == []
    assert [u.reason for u in unavailable] == ["source_failed"]


def test_run_graph_empty_plan_skips_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    """축 선언이 비면(그릴 주제 없음) 조립을 건너뛰고 사유 없이 빈 결과 — 억지 차트 금지."""
    agent = _SeqAgent([{"structured_response": ChartPlanSet(charts=[])}])
    monkeypatch.setattr(orchestrator, "build_graph_agent", lambda: agent)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())
    calls = _fake_build_charts(monkeypatch, (ChartSet(charts=[]), []))
    _, emit = _collect_emit()

    charts, unavailable = asyncio.run(
        orchestrator.run_graph(
            _FINDINGS, _GROUNDED, "질문", _plan("sales_anomaly"), _CTX, emit=emit
        )
    )

    assert charts.charts == [] and unavailable == []
    assert calls == []  # build_charts 미호출


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, plan: AnalysisPlan) -> None:
    """planner·워커·report·judge·recommend 전부 스텁 — 정상 경로 구성."""
    monkeypatch.setattr(
        orchestrator,
        "build_analysis_planner",
        lambda: _SeqAgent([{"structured_response": plan}]),
    )
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(finding=_FINDINGS[0]),
    )
    # 브랜치 분석 검증(이슈 #242) — 항상 통과시켜 후단(report 이하) 회귀 기준을 지킨다.
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: _AlwaysPassJudge())
    monkeypatch.setattr(
        orchestrator,
        "build_report_agent",
        lambda: _SeqAgent([_report_response(_GROUNDED)]),
    )
    monkeypatch.setattr(orchestrator, "build_report_judge", lambda: _SeqAgent([_score(8)]))
    monkeypatch.setattr(
        orchestrator,
        "build_recommend_agent",
        lambda: _SeqAgent([{"structured_response": _REC_SET}]),
    )
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())


def test_pipeline_happy_path_composes_report_and_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """전 구간 통합 — kind=report, 보고서+1번 추천 조립, 진행 token 순서."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    tokens, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 왜 떨어졌어?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "report"
    assert result.text.startswith(_GROUNDED)
    assert "1번. 감귤청 가격 10% 인하" in result.text
    assert result.verified is not None and result.verified.passed is True
    assert result.recommendations is _REC_SET
    assert tokens == [
        "질문을 분석하고 있습니다…",
        "매출 이상 분석 중…",
        "보고서를 작성하고 있습니다…",
        "보고서를 검증하고 있습니다…",
        "개선 방안을 정리하고 있습니다…",
    ]


def test_run_resolved_pipeline_executes_without_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#345] 승인 재개는 저장된 ResolvedPlan 만으로 돈다 — planner 를 부르지 않는다.

    planner 빌더가 호출되면 즉시 실패하므로 "재호출 0회"(#269 완료 조건)가 호출 그래프
    수준에서 고정된다. 진행 token 에 planner 문구가 없는 것도 같은 사실의 다른 표현이다.
    """
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="이번 달", reason="r")
    _patch_pipeline(monkeypatch, plan)

    def _boom():
        raise AssertionError("승인 재개 경로에서 planner 를 빌드하면 안 된다")

    monkeypatch.setattr(orchestrator, "build_analysis_planner", _boom)
    resolved = ResolvedPlan(
        analyses=("sales_anomaly",),
        date_from=dt.date(2026, 8, 1),
        date_to=dt.date(2026, 8, 5),
        period_expr="이번 달",
    )
    tokens, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_resolved_pipeline("이번 달 매출 분석해줘", resolved, _CTX, emit=emit)
    )

    assert result.kind == "report"
    assert result.period == (dt.date(2026, 8, 1), dt.date(2026, 8, 5))
    assert "질문을 분석하고 있습니다…" not in tokens


def test_pipeline_clarification_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """계획 불성립(clarification) → 워커 미실행, 되묻기 문안 반환."""
    plan = AnalysisPlan(analyses=[], reason="r", clarification="어느 기간을 분석할까요?")
    _patch_pipeline(monkeypatch, plan)
    tokens, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "이번 달 어때?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "clarification"
    assert result.text == "어느 기간을 분석할까요?"
    assert result.verified is None
    assert tokens == ["질문을 분석하고 있습니다…"]  # 워커 token 없음


def test_pipeline_scope_refusal_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """scope 위반 질문 → LLM 0회(진행 token 없음) kind=refused 거절(3-6 코드 경로)."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    tokens, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "경쟁사 매출 좀 보여줘", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "refused"
    assert "도와드리기 어려운 영역" in result.text
    assert tokens == []  # planner 진입 전 차단


def test_pipeline_first_report_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """1차 보고서 작성 실패는 파이프라인 밖으로 전파(Q2) — 호출부 사과/error 소관."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    monkeypatch.setattr(
        orchestrator, "build_report_agent", lambda: _SeqAgent([RuntimeError("llm down")])
    )
    _, emit = _collect_emit()

    with pytest.raises(RuntimeError):
        asyncio.run(
            orchestrator.run_analysis_pipeline(
                "지난달 매출?", _CTX, today=dt.date(2026, 7, 18), emit=emit
            )
        )


def test_pipeline_wants_chart_runs_graph_parallel_with_recommend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wants_chart=True → recommend 와 graph 를 병렬 실행하고 charts 를 채워 반환한다(3단계 배선)."""
    plan = AnalysisPlan(
        analyses=["sales_anomaly"], period_expr="지난달", reason="r", wants_chart=True
    )
    _patch_pipeline(monkeypatch, plan)
    monkeypatch.setattr(
        orchestrator,
        "build_graph_agent",
        lambda: _SeqAgent([{"structured_response": _axis_plan()}]),
    )
    _fake_build_charts(monkeypatch, (ChartSet(charts=[_built_chart()]), []))
    tokens, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 추이 그래프로 보여줘", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "report"
    assert result.charts is not None
    assert [c.title for c in result.charts.charts] == ["일별 매출 추이"]
    assert result.chart_unavailable == ()
    assert "차트를 만들고 있습니다…" in tokens
    assert "개선 방안을 정리하고 있습니다…" in tokens


def test_pipeline_wants_chart_false_skips_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """wants_chart=False(기본값) → graph 는 아예 호출되지 않는다(불필요한 LLM 콜 방지)."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)

    def _boom_graph() -> object:
        raise AssertionError("wants_chart=False 인데 build_graph_agent 가 호출됐다")

    monkeypatch.setattr(orchestrator, "build_graph_agent", _boom_graph)
    _, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 왜 떨어졌어?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.charts is None


def test_pipeline_wants_chart_requested_but_unavailable_appends_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#504] wants_chart=True 인데 전건 미생성(no_data 등) → 사유 문장이 본문에 붙고
    chart_unavailable 로 report 이벤트 재료가 채워진다(부분/전건 실패 안내, D-5 승계)."""
    plan = AnalysisPlan(
        analyses=["sales_anomaly"], period_expr="지난달", reason="r", wants_chart=True
    )
    _patch_pipeline(monkeypatch, plan)
    monkeypatch.setattr(
        orchestrator,
        "build_graph_agent",
        lambda: _SeqAgent([{"structured_response": _axis_plan()}]),
    )
    reason = orchestrator.ChartUnavailable(
        reason="no_data", message="'일별 매출 추이': 해당 기간에 표시할 데이터가 없습니다."
    )
    _fake_build_charts(monkeypatch, (ChartSet(charts=[]), [reason]))
    _, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 추이 그래프로 보여줘", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.charts is not None and result.charts.charts == []
    assert result.chart_unavailable == (reason,)
    assert "[차트 안내]" in result.text
    assert "해당 기간에 표시할 데이터가 없습니다" in result.text


def test_pipeline_chart_only_skips_workers_report_recommend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#504] chart_only 턴 — 워커 팬아웃·보고서·추천을 생략하고 차트만 조립한다.
    kind=report + chart_only=True 로 반환돼 SSE 가 제목("판매 분석 그래프")을 가른다."""
    plan = AnalysisPlan(
        analyses=[], period_expr="최근 7일", reason="차트만", chart_only=True, wants_chart=True
    )
    monkeypatch.setattr(
        orchestrator,
        "build_analysis_planner",
        lambda: _SeqAgent([{"structured_response": plan}]),
    )
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings())

    async def _boom_branches(*args: object, **kwargs: object) -> object:
        raise AssertionError("chart_only 인데 run_branches 가 호출됐다")

    monkeypatch.setattr(orchestrator, "run_branches", _boom_branches)
    monkeypatch.setattr(
        orchestrator,
        "build_report_agent",
        lambda: (_ for _ in ()).throw(AssertionError("chart_only 인데 report 가 호출됐다")),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_recommend_agent",
        lambda: (_ for _ in ()).throw(AssertionError("chart_only 인데 recommend 가 호출됐다")),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_graph_agent",
        lambda: _SeqAgent([{"structured_response": _axis_plan()}]),
    )
    _fake_build_charts(monkeypatch, (ChartSet(charts=[_built_chart()]), []))
    tokens, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "최근 7일 매출 그래프만 보여줘", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "report"
    assert result.chart_only is True
    assert result.verified is None and result.findings is None
    assert [c.title for c in result.charts.charts] == ["일별 매출 추이"]
    assert "그래프를 준비했습니다" in result.text
    assert "차트를 만들고 있습니다…" in tokens


def test_pipeline_all_workers_failed_returns_apology(monkeypatch: pytest.MonkeyPatch) -> None:
    """전 워커 실패 → kind=apology + 사과 문안(ALL_WORKERS_FAILED_TOKEN)."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: _StubAgent(exc=RuntimeError("down")),
    )
    _, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "apology"
    assert result.text == ALL_WORKERS_FAILED_TOKEN


# ── 4-3: 분석 이력 — save_history 호출·planner 입력 주입 (conftest 가 InMemory 주입) ──


def test_pipeline_saves_history_after_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """kind=report 완료 시 이력 저장 — §6.3 'N번 적용해줘'·planner 주입의 원천."""
    from app.agents.seller import history

    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    _, emit = _collect_emit()

    asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 왜 떨어졌어?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )
    entries = asyncio.run(history.load_recent(7))

    assert len(entries) == 1
    assert entries[0].question == "지난달 매출 왜 떨어졌어?"
    assert entries[0].analyses == ["sales_anomaly"]
    assert entries[0].date_from == "2026-06-01" and entries[0].date_to == "2026-06-30"
    saved = RecommendationSet.model_validate(entries[0].recommendations)
    assert saved.recommendations[0].title == "감귤청 가격 10% 인하"  # 순서=N번 계약 보존


def test_pipeline_injects_history_into_planner_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """이력이 있으면 planner 입력에 [최근 분석 이력] 블록 — 프롬프트 불변, 메시지 주입."""
    from app.agents.seller import history

    asyncio.run(
        history.save_history(
            7,
            question="6월 매출 분석",
            analyses=["sales_anomaly"],
            date_from="2026-06-01",
            date_to="2026-06-30",
            report="이전 보고서",
            recommendations=RecommendationSet(),
        )
    )
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    planner = _SeqAgent([{"structured_response": plan}])
    monkeypatch.setattr(orchestrator, "build_analysis_planner", lambda: planner)
    _, emit = _collect_emit()

    asyncio.run(
        orchestrator.run_analysis_pipeline(
            "이번엔 7월은?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert planner.received[0].startswith("[최근 분석 이력]")
    assert "6월 매출 분석" in planner.received[0]
    assert planner.received[0].endswith("[이번 질문] 이번엔 7월은?")


def test_pipeline_history_failure_does_not_break_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이력 조회·저장 장애 → 주입/기록 없이 분석은 정상 완료(degrade)."""
    from app.agents.seller import history

    async def _boom(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(history, "load_recent", _boom)
    monkeypatch.setattr(history, "save_history", _boom)
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    _, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "report"


def test_pipeline_injects_recent_turns_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이력이 없어도 대화 맥락이 있으면 [최근 대화] + [이번 질문] 으로 조립한다."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    planner = _SeqAgent([{"structured_response": plan}])
    monkeypatch.setattr(orchestrator, "build_analysis_planner", lambda: planner)
    _, emit = _collect_emit()
    turns = [("user", "어제 매출 알려줘"), ("assistant", "120만원입니다.")]

    asyncio.run(
        orchestrator.run_analysis_pipeline(
            "그럼 지난주는?", _CTX, today=dt.date(2026, 7, 18), emit=emit, recent_turns=turns
        )
    )

    sent = planner.received[0]
    assert sent.startswith("[최근 대화]")
    assert "사용자: 어제 매출 알려줘" in sent
    assert sent.endswith("[이번 질문] 그럼 지난주는?")


def test_pipeline_injects_recent_turns_before_history_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대화 맥락 + 이력이 모두 있으면 [최근 대화] → [최근 분석 이력] → [이번 질문] 순."""
    from app.agents.seller import history

    asyncio.run(
        history.save_history(
            7,
            question="6월 매출 분석",
            analyses=["sales_anomaly"],
            date_from="2026-06-01",
            date_to="2026-06-30",
            report="이전 보고서",
            recommendations=RecommendationSet(),
        )
    )
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    planner = _SeqAgent([{"structured_response": plan}])
    monkeypatch.setattr(orchestrator, "build_analysis_planner", lambda: planner)
    _, emit = _collect_emit()
    turns = [("user", "어제 매출 알려줘"), ("assistant", "120만원입니다.")]

    asyncio.run(
        orchestrator.run_analysis_pipeline(
            "이번엔 7월은?", _CTX, today=dt.date(2026, 7, 18), emit=emit, recent_turns=turns
        )
    )

    sent = planner.received[0]
    assert sent.startswith("[최근 대화]")
    assert sent.index("[최근 대화]") < sent.index("[최근 분석 이력]")
    assert sent.endswith("[이번 질문] 이번엔 7월은?")


# ─────────── S-4 화면 맥락 주입 — planner (이슈 #118) ───────────


def _screen_ctx(**payload):
    from app.schemas.seller import SellerChatRequest

    return SellerChatRequest.model_validate(
        {"sessionId": "s", "threadId": "t", "message": "m", "screen": payload}
    ).screen


def test_pipeline_without_screen_sends_the_same_planner_input_as_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[회귀 0] `screen=None` 이면 planner 입력이 오늘과 **바이트 동일**하다."""
    from app.agents.seller import history

    # 파이프라인을 두 번 돌리므로 첫 회차가 저장한 이력이 둘째 회차 입력에 섞인다 —
    # 비교 대상은 screen 유무 하나뿐이어야 하므로 이력 축을 고정한다.
    async def _no_history(*_args, **_kwargs):
        return []

    async def _no_save(*_args, **_kwargs):
        return None

    monkeypatch.setattr(history, "load_recent", _no_history)
    monkeypatch.setattr(history, "save_history", _no_save)

    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    turns = [("user", "어제 매출 알려줘"), ("assistant", "120만원입니다.")]

    sent: list[str] = []
    for screen_kwargs in ({}, {"screen": None}):
        _patch_pipeline(monkeypatch, plan)
        planner = _SeqAgent([{"structured_response": plan}])
        monkeypatch.setattr(orchestrator, "build_analysis_planner", lambda: planner)
        _, emit = _collect_emit()
        asyncio.run(
            orchestrator.run_analysis_pipeline(
                "그럼 지난주는?",
                _CTX,
                today=dt.date(2026, 7, 18),
                emit=emit,
                recent_turns=turns,
                **screen_kwargs,
            )
        )
        sent.append(planner.received[0])

    assert sent[0] == sent[1]
    assert "[현재 화면]" not in sent[0]


def test_pipeline_injects_screen_context_into_planner_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """화면 맥락은 이력과 **같은 두 곳**(supervisor·planner)에만 주입한다."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    planner = _SeqAgent([{"structured_response": plan}])
    monkeypatch.setattr(orchestrator, "build_analysis_planner", lambda: planner)
    _, emit = _collect_emit()

    asyncio.run(
        orchestrator.run_analysis_pipeline(
            "이 목록 왜 비어?",
            _CTX,
            today=dt.date(2026, 7, 18),
            emit=emit,
            screen=_screen_ctx(pageType="seller_products", filters={"status": "품절"}),
        )
    )

    sent = planner.received[0]
    assert sent.startswith("[현재 화면]")
    assert "상품 관리" in sent and "status=품절" in sent
    assert sent.endswith("[이번 질문] 이 목록 왜 비어?")


def test_pipeline_orders_conversation_then_screen_then_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """순서: [최근 대화] → [현재 화면] → [최근 분석 이력] → [이번 질문]."""
    from app.agents.seller import history

    asyncio.run(
        history.save_history(
            7,
            question="6월 매출 분석",
            analyses=["sales_anomaly"],
            date_from="2026-06-01",
            date_to="2026-06-30",
            report="이전 보고서",
            recommendations=RecommendationSet(),
        )
    )
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    planner = _SeqAgent([{"structured_response": plan}])
    monkeypatch.setattr(orchestrator, "build_analysis_planner", lambda: planner)
    _, emit = _collect_emit()
    turns = [("user", "어제 매출 알려줘"), ("assistant", "120만원입니다.")]

    asyncio.run(
        orchestrator.run_analysis_pipeline(
            "이 목록 왜 비어?",
            _CTX,
            today=dt.date(2026, 7, 18),
            emit=emit,
            recent_turns=turns,
            screen=_screen_ctx(pageType="seller_orders", filters={"status": "신규주문"}),
        )
    )

    sent = planner.received[0]
    assert (
        sent.index("[최근 대화]")
        < sent.index("[현재 화면]")
        < sent.index("[최근 분석 이력]")
        < sent.rindex("[이번 질문]")
    )


def test_planner_prompt_is_untouched_by_screen_injection() -> None:
    """프롬프트 파일은 한 글자도 바꾸지 않는다 — 주입은 **입력 메시지에만**."""
    from app.agents.seller import prompts

    assert "현재 화면" not in prompts.PLANNER_PROMPT


# ── PipelineResult report 재료 필드 (이슈 #296 — report SSE 직렬화 원천) ─────────


def test_pipeline_report_kind_fills_report_event_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kind=report — findings·period·chart_requested 가 report 이벤트 재료로 채워진다."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], period_expr="지난달", reason="r")
    _patch_pipeline(monkeypatch, plan)
    _, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 왜 떨어졌어?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "report"
    assert result.findings == [_FINDINGS[0]]  # 검증 통과 finding 이 그대로 실린다
    assert result.period == (dt.date(2026, 6, 1), dt.date(2026, 6, 30))  # '지난달' 환산값
    assert result.chart_requested is False  # 차트 미요청 질문


def test_pipeline_report_kind_chart_requested_true_when_wanted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wants_chart 질문 — chart_requested=True (charts 실패·드랍과 무관한 요청 신호)."""
    plan = AnalysisPlan(
        analyses=["sales_anomaly"], period_expr="지난달", reason="r", wants_chart=True
    )
    _patch_pipeline(monkeypatch, plan)
    monkeypatch.setattr(
        orchestrator,
        "build_graph_agent",
        lambda: _SeqAgent([{"structured_response": ChartSet(charts=[])}]),
    )
    _, emit = _collect_emit()

    result = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "지난달 매출 차트로 보여줘", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )

    assert result.kind == "report"
    assert result.chart_requested is True
    assert result.charts is not None and result.charts.charts == []


def test_pipeline_non_report_kinds_leave_report_fields_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """되묻기·거절·사과 — report 재료 필드는 기본값(None·False) 그대로다."""
    clar_plan = AnalysisPlan(analyses=[], reason="r", clarification="어느 기간을 분석할까요?")
    _patch_pipeline(monkeypatch, clar_plan)
    _, emit = _collect_emit()
    clarification = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "이번 달 어때?", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )
    assert clarification.kind == "clarification"

    refusal = asyncio.run(
        orchestrator.run_analysis_pipeline(
            "경쟁사 매출 좀 보여줘", _CTX, today=dt.date(2026, 7, 18), emit=emit
        )
    )
    assert refusal.kind == "refused"

    for result in (clarification, refusal):
        assert result.findings is None
        assert result.period is None
        assert result.chart_requested is False
