"""판매자 draft 수명주기 단일 입구 (#622 — hitl·draft_session 조정자).

`hitl`(무효화·checkpoint)과 `draft_session`(pending)은 서로 import 하지 않는다(순환
없음) — 이 모듈이 둘을 조합하는 유일한 지점이다. #506 이후 `_product_stream`·
`_apply_stream`(api/seller.py)이 각자 `start_draft`/`invalidate_draft`/`save_pending`을
직접 불러 두 가지 사고가 났다:

① pending 이 죽은 draftId 를 계속 가리킨다 — `invalidate_draft`·`save_pending`이
   `if record.op == "create":` 블록 안에만 있어서, 수정 턴이 `op="update"`(가격만
   변경 등)로 응답하면 이전 create draft 가 무효화도 pending 갱신도 안 된 채 남는다.
   이후 발화가 존재하지 않는 옛 draftId 기준으로 게이트에 걸린다.
③ 게이트 판정 실패(None) 낙하 경로 중 "N번 적용해줘"(`_apply_stream`)만 `pending`을
   안 받아, 대기 중인 create draft 를 무효화하지 않고 두 번째 draft 를 발급한다
   (같은 문제를 supervisor 라우팅 product 분기는 리뷰 M-1b 로 이미 고쳤었다).

`publish_draft`가 이 둘을 하나의 정본 절차로 묶는다 — 호출부는 `start_draft`·
`invalidate_draft`·`save_pending`을 더는 직접 부르지 않는다(아키텍처 테스트로 강제,
tests/unit/test_seller_draft_lifecycle.py).

`lookup_pending`은 ②의 원인(조회 실패와 정상 "없음"이 같은 `None`으로 뭉개져 게이트
①.3 전체가 건너뛰어지던 문제)을 3상태로 분리한다. "unknown"(조회 실패)은 새 draft
발급 경로(product·apply 진입)만 차단하고, search·analysis·general·confirm 은 그대로
진행한다 — confirm 은 draftId 로 독립 검증하므로 pending 상태와 무관하다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.agents.seller import draft_session, hitl
from app.agents.seller.context import SellerContext
from app.agents.seller.preview import diff_notes
from app.agents.seller.vision import ProductImageAnalysis

# [#622] product·apply 진입 차단 문구 — pending 상태가 "unknown"(조회 실패)일 때,
# 그 상태를 모른 채 새 draft 를 발급하면(수정 턴을 신규 등록으로 오인 등) 이전에 진짜
# 대기 중이던 create draft 가 무효화되지 않고 두 번째 draft 와 동시 생존할 수 있다.
# 그래서 draft *발급* 경로만 막는다 — 조회(search)·분석(analysis)·confirm 은 이 상태와
# 무관하게 정상 진행한다(모듈독스트링).
UNKNOWN_STATE_BLOCK_TEXT = (
    "지금 작성 중인 초안 상태를 확인하는 데 문제가 있어서 진행이 어려워요. "
    "잠시 후 다시 시도해 주시겠어요?"
)


@dataclass(frozen=True)
class PendingLookup:
    """등록 초안 대기 조회 결과 — 3상태(모듈독스트링).

    `pending`은 `state == "found"`일 때만 채워진다.
    """

    state: Literal["none", "found", "unknown"]
    pending: draft_session.PendingCreate | None = None


async def lookup_pending(context: SellerContext, thread_id: str) -> PendingLookup:
    """등록 초안 대기 3상태 조회 — `draft_session.load_pending_state`를 감싼다.

    api/seller.py 의 게이트 ①.3(수정/취소/승인/딴주제 판정)은 `state == "found"`일
    때만 돈다 — "unknown"은 판정 근거(실제 pending 내용)가 없어 게이트를 돌릴 수 없고,
    "none"은 원래도 게이트를 건너뛰던 경우다.
    """
    state, pending = await draft_session.load_pending_state(context, thread_id)
    return PendingLookup(state=state, pending=pending)


async def publish_draft(
    context: SellerContext,
    thread_id: str,
    record: hitl.DraftRecord,
    *,
    prev: draft_session.PendingCreate | None,
    image_urls: Sequence[str] = (),
    analysis: ProductImageAnalysis | None = None,
) -> list[str]:
    """draft 발급 단일 입구 — 무효화·checkpoint 저장·pending 갱신을 원자적 순서로 묶는다.

    ① `prev`가 있으면 새 `record.op`와 무관하게 이전 draft 를 무효화한다 — create 로
       이어지든 update/ship 으로 갈라지든, 대기 중이던 draft 는 이번 턴에 대체된다.
    ② `start_draft(record)` — checkpoint 저장 + interrupt 대기(안전장치 ①).
    ③ `record.op == "create"`면 `save_pending`(다음 수정 턴이 이어갈 재료 저장),
       아니면 `clear_pending`(pending 은 항상 "가장 최근 발급된 create 초안"만 가리킨다
       — update/ship 으로 전환된 턴에서 옛 pending 이 남으면 다음 발화가 죽은 draftId
       기준 게이트에 걸린다).

    반환은 `diff_notes(prev.changes, record)` — `prev`가 있고 `record.op == "create"`일
    때만 의미 있다(수정 턴 preview 의 "수정 반영" note). 그 외는 빈 목록.
    """
    if prev is not None:
        await hitl.invalidate_draft(prev.draft_id)
    await hitl.start_draft(record)

    if record.op != "create":
        await draft_session.clear_pending(context, thread_id)
        return []

    notes = diff_notes(prev.changes, record) if prev is not None else []
    await draft_session.save_pending(
        context,
        thread_id,
        draft_session.PendingCreate(
            draft_id=record.draft_id,
            image_urls=tuple(image_urls),
            analysis=analysis.model_dump() if analysis is not None else None,
            changes={c.field: c.after for c in record.changes},
        ),
    )
    return notes


async def cancel_pending(
    context: SellerContext, thread_id: str, pending: draft_session.PendingCreate
) -> None:
    """대기 중인 create 초안을 취소한다 — invalidate + pending 폐기를 한 번에 묶는다.

    `_cancel_pending_stream`(채팅 '취소') 전용 — `publish_draft`와 달리 새 draft 를
    발급하지 않으므로 별도 함수로 둔다.
    """
    await hitl.invalidate_draft(pending.draft_id)
    await draft_session.clear_pending(context, thread_id)
