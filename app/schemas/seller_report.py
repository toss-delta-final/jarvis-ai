"""판매자 분석 보고서 조회 API 와이어 스키마 (이슈 #599 — R-1).

R-1(목록)만 여기서 모델로 선언한다. **R-2(상세)는 의도적으로 모델이 없다** —
응답 본문을 `api/seller._report_payload()` 가 조립하는데, 그 모양은 S-4 `report` SSE
이벤트 계약(FE `SellerReport`)이다. 여기서 같은 필드를 다시 선언하면 계약이 두 벌이 되고
한쪽만 고쳐지는 순간 채팅 패널과 보고서 페이지가 갈린다. 조립기를 단일 원천으로 둔다.

`analysis_records` 의 저장 모델과도 섞지 않는다 — 그쪽은 DDL 컬럼명(snake_case)이고
여기는 와이어(camelCase)다(DESIGN-SELLER-ANALYSIS-STORE-585.md §3.4).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from app.schemas.chat import CamelModel

# 목록이 빌 때만 실리는 사유(결정 113). 값이 4종이 아니라 5종인 이유는 `pending_first_run`
# 때문이다 — 등록 직후·배치 미실행은 사고(not_registered)가 아니라 정상 대기 상태이고,
# 모든 판매자가 가입 직후 반드시 한 번 거치므로 빈도가 가장 높다.
NoReportReason = Literal[
    "not_registered",
    "inactive",
    "no_trigger",
    "no_baseline",
    "pending_first_run",
]

TriggerType = Literal["scheduled_daily", "scheduled_weekly", "event", "manual"]


class SellerReportListItem(CamelModel):
    """R-1 `items[]` 1건 — 목록 카드에 필요한 것만. 본문·세그먼트·추천 상세는 R-2 소관."""

    report_id: str = Field(description="UUID 문자열 — 상품·주문 id 가 숫자인 것과 다르다")
    trigger_type: TriggerType
    period_from: date
    period_to: date
    title: str
    summary: str
    recommendation_count: int
    has_holds: bool = Field(description="판정 보류 존재 — 목록 배지용. 상세는 R-2 holds[]")
    created_at: str = Field(description="RFC3339 UTC(Z) — 확정 3")
    read_at: str | None = None


class SellerReportListResponse(CamelModel):
    """R-1 응답.

    `total` 은 `unreadOnly` 필터를 **적용한 뒤**의 건수, `unread_count` 는 필터와 무관하게
    **항상 전량 기준**이다(배지용) — S-2 `tabCounts` 와 같은 원칙이라 탭을 옮겨도 배지가
    흔들리지 않는다.
    """

    total: int
    unread_count: int
    no_report_reason: NoReportReason | None = Field(
        default=None,
        description="목록이 빌 때만 값이 실린다. 판정 불가면 추정하지 않고 null",
    )
    items: list[SellerReportListItem] = Field(default_factory=list)
