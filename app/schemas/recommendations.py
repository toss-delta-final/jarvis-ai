"""홈 추천 랭킹 스키마 — I-22 (api-spec §3.7, 이슈 #148).

Spring → AI inbound(레인 b)라 요청은 `StrictEventModel`(extra=forbid, camelCase 전용)을 쓴다 —
`/events/*` 와 같은 신뢰경계이고, 미지 필드를 조용히 흡수하면 계약 불일치가 은폐된다
(lessons 2026-07-30 `extra="allow"` 은폐 사례).

응답은 `CamelModel` — FastAPI 가 `response_model` 로 by_alias 직렬화한다. **알고리즘·모델 버전과
프로필 원문은 이 스키마에 자리가 없다**(§3.7 [HARD] provenance 비노출) — 필드를 추가하면 계약 위반이다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.chat import CamelModel
from app.schemas.events import StrictEventModel

_BIGINT_MAX = 2**63 - 1  # PostgreSQL BIGINT 상한 — 신원 id 범위 방어
_CATALOG_VERSION_MAX_CHARS = 200  # 불투명 키 남용 방어(chat_key_max_chars 와 같은 취지)
# 시그널 배열 길이 상한 — 요청당 조회 비용의 상한이다. Spring 버그·신뢰경계 밖 호출자가 거대한
# 배열을 보내면 매 요청 `get_many`/`exclude` 가 그만큼 커져 I-22 예산을 위협한다.
_SIGNAL_IDS_MAX_LEN = 200

# `limit`(최종 노출 목표) 상한 — **이 상수가 단일 출처**이고 config `home_reco_max_items` 가
# 여기에 묶인다(`core/config.py`, LIST_MAX_PRODUCTS 전례와 같은 방식).
#
# 두 값이 갈리면 `_overfetch_size` 가 어느 쪽으로든 계약을 깬다. `limit > max_items` 를 허용하면
# 응답 크기 상한이 뚫리고, 바깥에서 `max_items` 로 깎으면 **요청받은 `limit` 보다 적게** 반환해
# "품절 드롭 대비 넉넉히"(§3.7)가 무너진다. 상한을 한 곳에 두고 config 가 그 이상이도록 기동
# 시점에 강제하면 두 경우 다 발생할 수 없다.
LIMIT_MAX = 60

# 시그널 상품 id — `member_id` 와 같은 수준으로 방어한다. Python int 는 임의 정밀도라 Pydantic 은
# 통과시키지만, BIGINT 를 넘는 값은 `get_many`/`top_k_by_vector` 의 psycopg 바인딩에서 터진다.
ProductId = Annotated[int, Field(strict=True, gt=0, le=_BIGINT_MAX)]

HomeRecommendationOutcome = Literal["PERSONALIZED", "NO_PROFILE", "INSUFFICIENT_CANDIDATES"]


class HomeRecommendationSignals(StrictEventModel):
    """개인화 입력 신호 (§3.7).

    셋 다 선택이다 — 전부 비면 개인화 근거가 없어 `NO_PROFILE` 로 답한다(오류가 아니다).
    `recent_purchased_product_ids` 는 **가중치가 아니라 제외 필터**다(이미 샀으므로).

    항목 값·배열 길이를 여기서 막는다 — 이 값들은 그대로 `store.get_many` 와 `top_k_by_vector` 의
    `exclude`(`bigint[]` 캐스팅)로 흘러가므로, 범위를 넘으면 DB 경계에서 터지고 길이를 안 막으면
    요청당 조회 비용에 상한이 없다. 스키마에서 400 으로 거절하는 편이 계약상 명확하다.
    """

    recently_viewed_product_ids: list[ProductId] = Field(
        default_factory=list, max_length=_SIGNAL_IDS_MAX_LEN
    )
    cart_product_ids: list[ProductId] = Field(default_factory=list, max_length=_SIGNAL_IDS_MAX_LEN)
    recent_purchased_product_ids: list[ProductId] = Field(
        default_factory=list, max_length=_SIGNAL_IDS_MAX_LEN
    )


class HomeRecommendationRequest(StrictEventModel):
    """I-22 요청 본문 (§3.7).

    `sessionId` 가 **없다** — 홈에는 채팅 세션이 없고 신원은 `memberId` 만으로 충분하다.
    미지 필드는 `extra=forbid` 가 400 으로 거부하므로 `sessionId` 를 실어 보내면 즉시 드러난다.
    게스트는 이 API 를 호출하지 않는다(Spring 이 P-4 로 처리) — 그래서 `memberId` 는 필수다.
    """

    member_id: int = Field(strict=True, gt=0, le=_BIGINT_MAX)
    # 최종 노출 목표 개수. FastAPI 는 Spring 의 품절 드롭에 대비해 이보다 넉넉히 반환한다.
    # 상한은 `LIMIT_MAX` — 홈 레일 한 줄에 그보다 많이 필요한 화면은 없고, 상한이 없으면
    # overfetch 가 응답 크기·조회 비용을 함께 부풀린다.
    limit: int = Field(strict=True, gt=0, le=LIMIT_MAX)
    # [C-18] **폐기 제안 중.** 어느 쪽도 의미 있는 값을 만들 수 없다 — Spring 은 AI 인덱스의 동기화
    # 시점을 모르고, AI 가 지문을 만들어도 **그 시점의 임베딩을 보존하지 않으므로 재현에 쓸 수 없다**
    # (`products` 는 I-17 이 제자리 upsert 한다). 재현이 필요하지도 않다 — 산출물(목록·reason)은
    # Spring 이 `recommendation_generated` 로 이미 저장한다(§3.7). 캐시 무효화도 TTL 10분과 중복이다.
    # Spring 이 계속 보내도 깨지지 않게 선택 필드로 받아만 두고 버린다. 계약에서 제거되면 이 줄도 삭제.
    catalog_version: str | None = Field(default=None, strict=True)
    signals: HomeRecommendationSignals

    @field_validator("catalog_version")
    @classmethod
    def _limit_version_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) > _CATALOG_VERSION_MAX_CHARS:
            raise ValueError("catalogVersion 이 허용 길이를 초과했습니다")
        return value


class HomeRecommendationItem(CamelModel):
    """추천 1건. `position` 을 싣지 않는다 — **배열 순서가 곧 순위**다(§3.7)."""

    product_id: int
    # 카드에 표시할 이유. 근거를 못 만들었으면 null(필드 자체는 유지 — CH-5·P-5 와 같은 규칙).
    reason: str | None = None


class HomeRecommendationResponse(CamelModel):
    """I-22 성공 응답 — `outcome` 3종 모두 **200** 이다 (§3.7).

    cold start 는 오류가 아니다. 프로필이 없어도 200 + `outcome` 으로 답하고 **fallback 판단은
    Spring 이** 한다. 여기에 `algorithmVersion`·`modelVersion` 을 더하면 [HARD] 위반이다.
    """

    outcome: HomeRecommendationOutcome
    # 추천 실행 1회를 가리키는 id. 재시도하면 새 값이 발급된다(멱등 아님).
    recommendation_request_id: str
    # ≥128bit 무작위(I-21 과 동일 규칙) — 순번·타임스탬프 등 추측 가능한 형식 금지.
    list_id: str
    items: list[HomeRecommendationItem] = Field(default_factory=list)
