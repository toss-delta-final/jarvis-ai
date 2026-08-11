"""SOP 실행 엔진 (이슈 #589, `01-ARCHITECTURE.md` §4.2 — 결정 4 "아주 간단하게").

워커 1종을 **스텝 튜플**로 선언하고 순차 실행한다. 예외를 `Hold` 로 흡수하는 것이
이 엔진의 유일한 지능이다.

만들지 않는 것(필요해질 때까지 — §4.1 표):
- YAML/DSL 로더 · 조건 분기 · 병렬 스텝 · DAG
- 재시도 정책 · 서킷브레이커 · 롤백 · 보상 트랜잭션

스텝은 `AnalysisContext` 를 **제자리에서 채우고 아무것도 반환하지 않는다**(`StepFn`).
반환값 전달 경로를 두지 않는 이유: 스텝 간 데이터가 ctx 하나로 모여야 검증층이 "LLM 이
보는 유일한 입력"(§4.3)을 그대로 직렬화할 수 있다.

타임아웃 값은 하드코딩하지 않는다 — 호출부가 `Settings.seller_sop_*_timeout_s` 를 읽어
`Step.timeout_s` 로 주입한다(`OPS-RUNTIME.md` T-3: load 5 / compare 5 / compute 30 /
feedback 3 / interpret 30).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.agents.seller.schemas import AnalysisType
from app.agents.seller.sop.context import AnalysisContext, Hold

logger = logging.getLogger(__name__)

# 실패 사유 문자열 상한 — 원시 예외에 Spring 응답 본문이 통째로 실려 오는 전례가 있다.
# 이 값은 보고서 본문·R-2 `holds[]` 로 나가므로 무한정 길면 안 된다(튜너블 아님 — 표기 규약).
_REASON_MAX_CHARS = 200

StepFn = Callable[[AnalysisContext], Awaitable[None]]
"""스텝 본문 — ctx 를 채운다. 반환값 없음."""


@dataclass(frozen=True)
class Step:
    """SOP 스텝 1개 선언.

    `required=False` 면 실패해도 다음 스텝으로 진행한다(예: `feedback` — 과거 성과가
    없어도 이번 분석은 성립한다). `required=True` 스텝 실패는 **이 워커의 판정 보류**다.
    """

    name: str
    run: StepFn
    timeout_s: float
    required: bool = True


@dataclass(frozen=True)
class Sop:
    """워커 1종의 스텝 선언 묶음. 공통 프레임은 load/compare/compute/feedback/interpret 이고
    워커별로 다른 것은 `compute` 하나다(§4.4)."""

    worker: AnalysisType
    steps: tuple[Step, ...]


def _reason(step: Step, exc: BaseException) -> str:
    """예외를 `Hold.reason` 문자열로 옮긴다 — 타임아웃과 그 외를 가른다."""
    if isinstance(exc, TimeoutError):
        return f"{step.name} 타임아웃 {step.timeout_s}s 초과"
    detail = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {detail}"[:_REASON_MAX_CHARS]


async def run_sop(sop: Sop, ctx: AnalysisContext) -> AnalysisContext:
    """스텝을 순차 실행하고 **채워진 ctx 를 반환한다**(실패해도 raise 하지 않는다).

    실패해도 ctx 를 돌려주는 이유: 부분 결과 + `holds` 가 무인 실행 실패 규약의 재료다
    (`OPS-RUNTIME` F-3 — 스냅샷 실패 시 고객 축만 보류하고 브랜드 축으로 보고서를 낸다).
    여기서 raise 하면 그 경로가 통째로 죽는다.

    `except Exception` 이지 `BaseException` 이 아니다 — `asyncio.CancelledError` 는
    `BaseException` 이라 여기 걸리지 않고 상위(브랜드당 파이프라인 상한·서버 종료)로
    그대로 전파된다. 취소를 `Hold` 로 삼키면 배치가 멈추지 않는다.
    """
    for step in sop.steps:
        try:
            async with asyncio.timeout(step.timeout_s):
                await step.run(ctx)
        except Exception as exc:  # noqa: BLE001 — 흡수가 이 엔진의 목적이다
            reason = _reason(step, exc)
            logger.warning(
                "SOP 스텝 실패 worker=%s step=%s required=%s reason=%s",
                sop.worker,
                step.name,
                step.required,
                reason,
            )
            ctx.holds.append(Hold(step=step.name, reason=reason))
            if step.required:
                break  # 필수 스텝 실패 = 이 워커는 판정 보류
    return ctx
