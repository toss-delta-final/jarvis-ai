"""Spring → AI internal 위임 엔드포인트 (레인 b, api-spec §1.2·§2.3 b).

`/events/*`(§3.5)가 **통지**라면 여기는 **동기 위임 호출**이다 — 응답 본문에 결과가 실려 나가고
Spring 이 그것을 그대로 쓴다. 따라서 §2.7 의 `/events/*` 멱등 규약이 적용되지 않는다(재시도 =
새 추천 실행). 인증은 같은 서비스 토큰(`X-Internal-Token`)이다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import verify_service_token
from app.schemas.recommendations import HomeRecommendationRequest, HomeRecommendationResponse
from app.services.home_recommendation import rank_home

router = APIRouter(tags=["internal"])


@router.post("/internal/recommendations/home", response_model=HomeRecommendationResponse)
async def home_recommendations(
    request: HomeRecommendationRequest,
    _token: None = Depends(verify_service_token),
) -> HomeRecommendationResponse:
    """홈 "OO님을 위한 추천"(P-5)의 개인화 랭킹 — I-22 (§3.7).

    **왕복 1회로 끝난다** — Spring 이 호출 주체라 응답에 목록이 담겨 나가고 I-21 콜백을 타지 않는다.
    `outcome` 3종(`PERSONALIZED`·`NO_PROFILE`·`INSUFFICIENT_CANDIDATES`)은 **모두 200** 이며
    fallback 판단은 Spring 이 한다 — 프로필 부재·후보 부족으로 4xx/5xx 를 내면 계약 위반이다.
    """
    return await rank_home(request)
