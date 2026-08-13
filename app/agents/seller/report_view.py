"""저장된 분석 보고서 → 화면 조립기 입력 역변환 (이슈 #599 — R-2 보고서 상세).

R-2 응답 본문을 **새로 직렬화하지 않는다.** `api/seller._report_payload()` 가 이미
FE `SellerReport`(jarvis-front `shared/types/chat.ts`) 모양으로 camelCase 조립을 완비하고
있으므로, 저장 레코드를 그 함수의 입력 타입(`PipelineResult`)으로 되돌려주기만 하면 된다.

**직렬화 코드를 두 벌 만들지 않는 것**이 이 모듈의 존재 이유다. 채팅(S-4 `report` 이벤트)과
보고서 페이지(R-2)가 같은 조립기를 공유하므로 두 화면이 어긋날 수 없고, FE
`AnalysisReport.tsx` 를 한 줄도 고치지 않고 새 페이지에서 재사용한다.

⚠️ 여기서 만드는 `PipelineResult` 는 **표시 전용**이다. `changes`(ProposedChange)를 복원하지
않으므로 HITL 적용 경로(§6.3 "N번 적용해줘")에 넘기면 안 된다 — 그 경로는
`analysis_store.get_recommendation()` 으로 레코드를 직접 읽는다.

⚠️ `_report_payload` 는 `summary` 를 저장 컬럼이 아니라 `split_report_summary(report_md)` 로
**다시 뽑는다.** 생성 시에도 같은 함수를 썼다는 전제(`06-REPORT.md` §82 "기존 함수 무수정
재사용")가 유지되어야 목록(R-1, 저장 컬럼)과 상세(R-2, 재추출)의 요약이 일치한다.
다른 함수로 갈아타면 두 화면이 갈린다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from app.agents.seller.analysis_records import RecommendationRecord, ReportRecord
from app.agents.seller.orchestrator import PipelineResult, VerifiedReport
from app.agents.seller.schemas import (
    MAX_RECOMMENDATIONS,
    ActionRecommendation,
    AnalysisFinding,
    RecommendationSet,
)

logger = logging.getLogger(__name__)


def record_to_pipeline_result(
    report: ReportRecord,
    recommendations: Sequence[RecommendationRecord],
) -> PipelineResult:
    """`seller_analysis_reports` 1행 + 추천 N행 → `PipelineResult`(표시 전용).

    차트 관련 필드는 전부 비운다 — 차트는 chart 레인 전용이고 보고서에는 넣지 않는다
    (`01-ARCHITECTURE.md` §2.4). FE 가 `charts?.map`·`chartRequested` 조건부라 빈 값이면
    해당 섹션이 자연히 사라지므로 필드 자체를 없앨 필요가 없다.
    """
    return PipelineResult(
        kind="report",
        # text 는 SSE token 용이라 R-2 경로에서는 쓰이지 않는다. 빈 문자열로 두면
        # 나중에 이 결과를 실수로 스트림에 흘렸을 때 조용히 아무 말도 하지 않는다.
        text="",
        verified=VerifiedReport(
            report=report.report_md,
            passed=report.verified,
            attempts=report.attempts,
            # score_total(정수 합계)만 저장돼 있어 축별 점수를 가진 ReportScore 를 복원할
            # 수 없다. _report_payload 가 last_score 를 읽지 않으므로 무해하다.
            last_score=None,
        ),
        recommendations=_to_recommendation_set(recommendations),
        findings=_to_findings(report.findings),
        period=(report.period_from, report.period_to),
        charts=None,
        chart_requested=False,
        chart_unavailable=(),
        chart_period=None,
        chart_only=False,
    )


def _to_findings(raw: Any) -> list[AnalysisFinding]:
    """`findings` jsonb → `AnalysisFinding` 목록.

    보고서는 **무기한 보존**이라 06 마이그레이션 이전 행은 이 값이 `[]` 이고, 앞으로 스키마가
    바뀌면 과거 행과 모양이 갈릴 수 있다. 한 건이 깨졌다고 보고서 전체를 404 로 만들지 않고
    그 항목만 버린다 — 판매자에게는 헤더가 빠진 화면이 아무것도 못 보는 것보다 낫다.
    """
    if not isinstance(raw, list):
        return []
    findings: list[AnalysisFinding] = []
    for item in raw:
        try:
            findings.append(AnalysisFinding.model_validate(item))
        except ValidationError:
            # 값은 로그에 싣지 않는다 — 보고서 본문에는 판매자 데이터가 들어 있다.
            logger.warning("seller_report_view_finding_invalid")
    return findings


def _to_recommendation_set(recs: Sequence[RecommendationRecord]) -> RecommendationSet:
    """추천 N행 → `RecommendationSet`. **행을 건너뛰지 않는다.**

    `_report_payload` 의 `index` 는 목록 순서(`enumerate(..., start=1)`)라, 한 건이라도
    빠지거나 순서가 흔들리면 화면의 N 과 저장된 `rank` 가 어긋난다. 그 상태에서 판매자가
    "3번 적용해줘" 라고 하면 **다른 추천이 적용된다** — 조용한 오적용이라 사고 중 최악이다.
    그래서 rank 로 명시 정렬하고, 값이 이상해도 버리지 않고 채워 넣는다.
    """
    ordered = sorted(recs, key=lambda r: r.rank)
    if len(ordered) > MAX_RECOMMENDATIONS:
        # 스키마 계약 상한(5)을 넘는 행은 저장 경로가 만들지 않는다. 그래도 넘었다면
        # 앞에서부터 자른다 — 뒤를 자르면 1..5 번호는 보존된다.
        logger.warning("seller_report_view_recs_truncated count=%d", len(ordered))
        ordered = ordered[:MAX_RECOMMENDATIONS]

    items: list[ActionRecommendation] = []
    for rec in ordered:
        items.append(
            ActionRecommendation(
                action_type=rec.action_type,
                # product_ids 는 배열이고 ActionRecommendation.product_id 는 단수 필수다.
                # v1 은 생성 측(ActionRecommendation)이 단수라 원소가 항상 1개다.
                # 비어 있으면 0 을 넣는다 — 표시가 깨질 뿐 번호 정합은 지켜진다.
                product_id=rec.product_ids[0] if rec.product_ids else 0,
                title=rec.title,
                rationale=rec.rationale,
                # 표시 전용이라 복원하지 않는다(모듈 docstring 경고 참조).
                changes=[],
                expected_effect=rec.expected_effect,
            )
        )
    return RecommendationSet(recommendations=items)


def holds_to_limitations(holds: Any) -> list[str]:
    """`holds` jsonb → 화면 "데이터 한계" 문자열 목록.

    `_report_payload` 는 `limitations` 를 **findings 중 evidence 가 빈 것**에서 뽑는다
    (채팅 레인의 degrade finding 규약). 무인 파이프라인의 판정 보류는 finding 이 아니라
    `holds` 에 쌓이므로 R-2 핸들러가 이 목록을 **뒤에 덧붙인다.**

    보류를 화면에서 지우면 "판정 보류 != 이상 없음" 불변 규약이 와이어에서 깨진다
    (`01-ARCHITECTURE.md` §9).
    """
    if not isinstance(holds, list):
        return []
    out: list[str] = []
    for item in holds:
        if not isinstance(item, dict):
            continue
        step = str(item.get("step", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if step and reason:
            out.append(f"{step}: {reason}")
        elif reason:
            out.append(reason)
    return out


def segments_to_wire(segments: Any) -> list[dict[str, Any]]:
    """`segments` jsonb(snake_case) → 와이어(camelCase).

    저장 모델(`analysis_records`)은 **와이어 스키마가 아니라는 규약**(설계서 §3.4)을 지키려고
    DDL 컬럼명 그대로 snake_case 를 쓴다. camelCase 변환은 그래서 저장 계층이 아니라 여기
    (뷰 계층)에 둔다.

    `centroidStats` 안쪽 키까지 변환한다 — 피처 이름이 그대로 화면 표 헤더가 되므로
    바깥만 바꾸면 한 응답에 두 표기가 섞인다.
    """
    if not isinstance(segments, list):
        return []
    return [_camelize(item) for item in segments if isinstance(item, dict)]


def _camelize(value: Any, _depth: int = 0) -> Any:
    if _depth > 4 or not isinstance(value, dict):
        return value
    return {_camel_key(k): _camelize(v, _depth + 1) for k, v in value.items()}


def _camel_key(key: str) -> str:
    head, *rest = str(key).split("_")
    return head + "".join(word[:1].upper() + word[1:] for word in rest)
