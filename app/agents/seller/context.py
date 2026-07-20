"""판매자 요청 컨텍스트 스키마 (SPEC-SELLER-001 §3 — 신원 주입, 2026-07-18 ToolRuntime 확정).

`create_agent(context_schema=SellerContext)` 로 에이전트에 연결하고, 실행 시
`agent.invoke(..., context=SellerContext(...))` 로 요청마다 주입한다.
도구는 `ToolRuntime[SellerContext]` 파라미터로 읽는다 — LLM 에게는 보이지 않아
남의 brandId 로 조회/쓰기를 만들 수 없다(IDOR 방지, api-spec §2.6).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SellerContext:
    """요청마다 달라지는 판매자 신원 — 검증된 JWT 클레임에서만 채운다.

    SpringClient 등 앱 수명주기 의존성은 여기 담지 않는다(싱글턴 소유) —
    컨텍스트는 '요청 스코프 값'만 담는다는 원칙(2026-07-18 확정).
    """

    seller_id: str  # JWT sub (role=seller, api-spec §2.6)
    brand_id: str  # JWT brandId 클레임 — 도구 인자로 절대 노출 금지
