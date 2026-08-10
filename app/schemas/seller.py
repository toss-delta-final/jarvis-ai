"""판매자 챗봇 전용 요청 스키마 (S-4, api-spec §3.2).

구매자 `ChatRequest`(app/schemas/chat.py)를 확장해 길이 검증을 재사용하고,
판매자 HITL 승인(confirm)의 **구조화 신호**를 최상위 선택 필드(action/draftId)로
받는다. 구매자 계약(ChatRequest)은 건드리지 않는다 — 판매자 스트림만 소비한다.

[변경 2026-07-22, FE 계약 A-2] confirm 전송을 구 "message 문자열에 JSON 을 실어
코드가 파싱" 방식에서 **최상위 action/draftId 필드**로 전환한다. FE 가 message 를
이스케이프하지 않고 `{sessionId, threadId, action:"confirm", draftId}` 로 보낸다.
"발화 ≠ 동의" [HARD] 원칙은 유지된다 — 승인은 `action == "confirm"` 구조화 신호로만
성립하고, 자유 텍스트 message 는 승인이 아니다(HITL 안전장치 ②).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.core.config import get_settings
from app.schemas.chat import ChatRequest


class SellerChatRequest(ChatRequest):
    """POST /seller/chat 요청 본문 — ChatRequest + HITL 승인 필드(선택).

    - 일반 발화: `{sessionId, threadId, message}` (action 미지정).
    - 승인(confirm): `{sessionId, threadId, action:"confirm", draftId}` — message 는
      비워도 된다(승인은 발화가 아니므로). action=="confirm" 이면 draftId 필수.
    """

    action: Literal["confirm"] | None = Field(
        default=None,
        description="HITL 승인 신호. 'confirm' 이면 draftId 초안을 실행(§3.2). 미지정=일반 발화",
    )
    # alias 는 CamelModel(alias_generator=to_camel)이 draft_id → draftId 로 생성한다.
    draft_id: str | None = Field(
        default=None,
        description="action=='confirm' 일 때 실행할 draft 식별자(스트림1 draft.draftId)",
    )
    # [2026-07-24] confirm 은 '발화 ≠ 동의'라 message 가 없다 — FE 계약 A-2 의
    # confirm 페이로드({sessionId, threadId, action:"confirm", draftId})는 message 키를
    # 싣지 않는다. 부모 ChatRequest 의 message 는 필수(...)라, 문서대로 보낸 confirm 이
    # "message field required" 400 으로 거절됐다. 여기서 message 를 선택(기본 "")으로
    # 낮춰 계약과 스키마를 일치시킨다 — 일반 발화의 message 필수성은 아래 model_validator
    # 가 action 별로 조건부 강제하므로 빈 발화 400 방어는 유지된다.
    message: str = Field(
        default="",
        description="현재 턴 사용자 원문 질의. confirm 승인 시엔 비운다(발화≠동의), 그 외 필수.",
    )
    # [#506, api-spec §3.2 v0.31.0] 이미지 기반 상품 등록 초안 — 이미지를 **새로 첨부한
    # 턴에만** 실린다(후속 턴 미전송은 FE 계약). alias 는 CamelModel 이 imageUrls 로 생성.
    image_urls: list[str] | None = Field(
        default=None,
        description="새로 첨부한 상품 이미지의 canonical S3 URL 목록(MVP 1장). 후속 턴엔 싣지 않는다",
    )

    @model_validator(mode="after")
    def _validate_message_and_confirm(self) -> "SellerChatRequest":
        """action 별 요청 형식을 강제한다 — 위반은 스트림 시작 전 400(BAD_REQUEST, §3.2).

        - action=='confirm': draftId 필수. 빈 draftId 를 일반 발화로 흘리면 승인이
          조용히 무시되어 FE 가 원인을 알 수 없다. message 는 요구하지 않는다(발화 아님).
          imageUrls 는 금지 — confirm 은 draftId 만 되보내는 계약이다(§3.2).
        - 그 외(일반 발화): message 필수. 빈 발화를 라우팅·파이프라인에 흘리지 않는다
          (message 를 선택 필드로 낮춘 뒤에도 일반 발화의 필수성을 여기서 지킨다).
        """
        if self.action == "confirm":
            if not (self.draft_id and self.draft_id.strip()):
                raise ValueError("action=='confirm' 이면 draftId 가 필요합니다")
            if self.image_urls:
                raise ValueError("confirm 요청에는 imageUrls 를 실을 수 없습니다")
        elif not (self.message and self.message.strip()):
            raise ValueError("message 가 필요합니다")
        return self

    @model_validator(mode="after")
    def _validate_image_urls(self) -> "SellerChatRequest":
        """imageUrls 2차 방어(#506) — 1차 방어는 FE 서버 라우트(업로드 검증)다.

        - 개수: `seller_image_max_count`(MVP 1장 — 2장째는 FE 가 교체로 처리).
        - 길이: `seller_image_url_max_len`(DB VARCHAR(500)) — presigned URL(서명
          쿼리스트링 포함, 만료됨)은 보통 1,000자를 넘어 여기서 함께 걸린다.
        - 형식: http(s) canonical URL 만. `X-Amz-Signature` 는 길이와 무관하게 명시
          거부한다 — 통과해 저장되면 만료 시점에 상품 이미지가 조용히 죽는다(§2.3).
        """
        if not self.image_urls:
            return self
        settings = get_settings()
        if len(self.image_urls) > settings.seller_image_max_count:
            raise ValueError(
                f"imageUrls 는 최대 {settings.seller_image_max_count}장까지 지원합니다"
            )
        for url in self.image_urls:
            stripped = url.strip()
            if not stripped:
                raise ValueError("imageUrls 항목이 비어 있습니다")
            if not stripped.startswith(("https://", "http://")):
                raise ValueError("imageUrls 는 http(s) URL 이어야 합니다")
            if len(stripped) > settings.seller_image_url_max_len:
                raise ValueError(
                    f"imageUrls 항목이 {settings.seller_image_url_max_len}자를 초과합니다 — "
                    "canonical URL 만 허용됩니다(presigned URL 금지)"
                )
            if "X-Amz-Signature" in stripped:
                raise ValueError("presigned URL 은 허용되지 않습니다 — canonical URL 을 보내주세요")
        return self
