"""Spring → AI internal 위임 엔드포인트 (레인 b, api-spec §1.2·§2.3 b).

`/events/*`(§3.5)가 **통지**라면 여기는 **동기 위임 호출**이다 — 응답 본문에 결과가 실려 나가고
Spring 이 그것을 그대로 쓴다. 따라서 §2.7 의 `/events/*` 멱등 규약이 적용되지 않는다(재시도 =
새 추천 실행). 인증은 같은 서비스 토큰(`X-Internal-Token`)이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agents.seller.analysis.daily_batch import run_manual_analysis
from app.api.deps import verify_service_token
from app.core.config import get_settings
from app.core.errors import get_request_id
from app.core.tracing import bind_request_trace, start_request_trace_safely
from app.schemas.chat import CamelModel
from app.schemas.recommendations import HomeRecommendationRequest, HomeRecommendationResponse
from app.services.home_recommendation import rank_home

router = APIRouter(tags=["internal"])

_log = logging.getLogger(__name__)

# [#469] fire-and-forget finish 태스크 보관 — 참조가 없으면 GC 가 실행 중 태스크를 수거할 수 있다.
_trace_finish_tasks: set[asyncio.Task] = set()
# [#601] 수동 실행 백그라운드 태스크 보관 — 같은 근거(GC 가 실행 중 태스크를 조용히 수거).
_manual_run_tasks: set[asyncio.Task] = set()


def _finish_trace_detached(trace, *, status: str, error_type: str | None, terminal: str) -> None:
    """[#469] 트레이스 finish/export 를 요청 경로 밖에서 완료한다.

    채팅 레인은 스트림 종료 시 await 하지만, I-22 는 P-5 예산(연결 2s/응답 3s) 경로라 export
    (최대 langsmith_export_timeout_s)를 응답 지연에 더할 수 없다 — 응답을 먼저 반환하고 export 는
    백그라운드 태스크로 흘린다. 실패해도 finish 내부가 삼키므로 요청 결과에 영향이 없다.
    """
    task = asyncio.create_task(
        trace.finish(status=status, error_type=error_type, terminal_reason=terminal)
    )
    _trace_finish_tasks.add(task)
    task.add_done_callback(_trace_finish_tasks.discard)


@router.post("/internal/recommendations/home", response_model=HomeRecommendationResponse)
async def home_recommendations(
    request: HomeRecommendationRequest,
    http_request: Request,
    _token: None = Depends(verify_service_token),
) -> HomeRecommendationResponse:
    """홈 "OO님을 위한 추천"(P-5)의 개인화 랭킹 — I-22 (§3.7).

    **왕복 1회로 끝난다** — Spring 이 호출 주체라 응답에 목록이 담겨 나가고 I-21 콜백을 타지 않는다.
    `outcome` 3종(`PERSONALIZED`·`NO_PROFILE`·`INSUFFICIENT_CANDIDATES`)은 **모두 200** 이며
    fallback 판단은 Spring 이 한다 — 프로필 부재·후보 부족으로 4xx/5xx 를 내면 계약 위반이다.

    [#469] 요청 트레이스: memberId 원값은 트레이스에 싣지 않는다 — conversation_id 로 넘긴 값은
    RequestTrace 가 peppered 지문(sessionFp)으로만 남긴다(§3.7 [HARD] 준수). 단계별 span 은
    rank_home 내부의 trace_span 이 만든다.
    """
    # requestId 는 errors.request_context_middleware 가 부여한 값 — §2.4 오류 봉투·응답 헤더
    # (X-Request-Id)와 상관되어 Spring 쪽 실패 로그와 트레이스를 맞댈 수 있다(chat.py 와 동일 관례,
    # PR #470 리뷰).
    # [이슈 #140] provenance `requestId` 와 같은 값을 재사용한다 — 두 번 만들지 않는다.
    request_id = get_request_id(http_request)
    trace = start_request_trace_safely(
        name="home_recommendation",
        request_id=request_id,
        conversation_id=str(request.member_id),
        thread_id="",
        lane="home",
        environment=get_settings().app_environment,
    )
    # [#326] 콘텐츠 모드에서만 시그널이 실린다(off 면 no-op) — strict 키라 전체 카나리아 검증.
    trace.record_request_content(
        extra_inputs={
            "signals": json.dumps(request.signals.model_dump(by_alias=True), ensure_ascii=False),
            "limit": str(request.limit),
        }
    )
    with bind_request_trace(trace):
        try:
            response = await rank_home(request, request_id=request_id)
        except HTTPException as exc:
            code = (
                exc.detail.get("code", "INTERNAL") if isinstance(exc.detail, dict) else "INTERNAL"
            )
            _finish_trace_detached(trace, status="FAILED", error_type=code, terminal=code.lower())
            raise
        except BaseException:
            _finish_trace_detached(trace, status="FAILED", error_type="INTERNAL", terminal="crash")
            raise
    trace.record_request_content(
        output_text=json.dumps(
            {"outcome": response.outcome, "productIds": [i.product_id for i in response.items]},
            ensure_ascii=False,
        )
    )
    _finish_trace_detached(
        trace, status="COMPLETED", error_type=None, terminal=response.outcome.lower()
    )
    return response


class SellerAnalysisRunRequest(CamelModel):
    """수동 실행 요청 본문 — 전부 선택. 생략 시 예약 배치와 같은 기본 기간("전날 1일")."""

    period_from: date | None = None
    period_to: date | None = None


class SellerAnalysisRunResponse(CamelModel):
    accepted: bool
    brand_id: int
    trigger_type: str = "manual"


async def _run_manual_analysis_detached(
    brand_id: int, *, period_from: date | None, period_to: date | None
) -> None:
    try:
        outcome = await run_manual_analysis(brand_id, period_from=period_from, period_to=period_to)
        _log.info(
            "brand_id=%s 수동 실행 완료 report_generated=%s skip_reason=%s",
            brand_id,
            outcome.report_generated,
            outcome.skip_reason,
        )
    except Exception:  # noqa: BLE001 - 응답을 이미 202로 보낸 뒤라 예외를 삼키고 로그만 남긴다
        _log.exception("brand_id=%s 수동 실행 실패", brand_id)


@router.post(
    "/internal/seller/{brand_id}/analysis/run",
    response_model=SellerAnalysisRunResponse,
    status_code=202,
)
async def run_seller_analysis(
    brand_id: int,
    request: SellerAnalysisRunRequest | None = None,
    _token: None = Depends(verify_service_token),
) -> SellerAnalysisRunResponse:
    """무인 판매자 분석 수동 실행 (⑤, 이슈 #601 · `10-TRIGGER.md` §5.1) — 데모·재현 용도.

    스캔 게이트 없이 심층 분석을 즉시 시작한다(`daily_batch.run_manual_analysis`, 예약
    배치와 같은 `mutation_lock` — 그 브랜드의 예약 배치가 마침 도는 중이면 끝날 때까지
    직렬 대기 후 실행된다). 파이프라인 총 소요가 4워커 팬아웃 + 보고서 검증 재작성
    루프로 수 분에 달할 수 있어 **동기 응답을 기다리지 않는다** — 202 Accepted 로 즉시
    받아들이고 실제 실행은 백그라운드 태스크로 흘린다(이 파일의 `_finish_trace_detached`
    와 같은 fire-and-forget 관행). 결과는 이 엔드포인트의 폴링이 아니라 저장된 보고서
    (`GET /seller/reports` 등 기존 조회 경로)로 확인한다.
    """
    period_from = request.period_from if request is not None else None
    period_to = request.period_to if request is not None else None
    task = asyncio.create_task(
        _run_manual_analysis_detached(brand_id, period_from=period_from, period_to=period_to)
    )
    _manual_run_tasks.add(task)
    task.add_done_callback(_manual_run_tasks.discard)
    return SellerAnalysisRunResponse(accepted=True, brand_id=brand_id)
