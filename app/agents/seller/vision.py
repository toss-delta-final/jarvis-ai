"""상품 이미지 vision 분석 (#506, api-spec §3.2 v0.31.0).

이미지 기반 등록 초안의 1단계 — **이미지를 새로 첨부한 턴에만 정확히 1회** 실행된다.
FE 가 후속 턴에 imageUrls 를 싣지 않는 것과 대칭으로, 서버도 imageUrls 없는 턴에는
vision 을 절대 태우지 않는다(매 턴 재분석 시 상품명이 흔들리고 이미지 토큰이 낭비된다
— FE 계약 문서 §4). 분석 결과는 draft_session 에 보관돼 수정 턴에서 재사용된다.

모델 경로는 판매자 레인 공통(`init_seller_model`) — LangChain 표준 멀티모달 블록
(`{"type": "image_url", ...}`)을 쓰므로 별도 provider 분기가 없다. 구조화 출력은
`with_structured_output` — 워커들의 ToolStrategy 와 달리 단발 호출이라 에이전트
루프가 필요 없다.

실패는 None degrade — 이미지 분석이 죽어도 판매자 발화만으로 create 초안은 성립할
수 있다(가격·재고는 어차피 발화에서만 온다). 호출부가 preview sections.warning 에
분석 실패를 고지한다.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.seller.models import init_seller_model
from app.core.config import get_settings
from app.core.llm import LLMNotConfigured
from app.core.tracing import trace_span

logger = logging.getLogger(__name__)


class ProductImageAnalysis(BaseModel):
    """vision 1회 분석 결과 — 등록 초안의 이름·설명 원천(내부 스키마, 와이어 아님).

    가격·재고는 여기 없다 — 사진에서 알 수 없는 값이라 발명 금지(판매자 발화 전용).
    """

    name: str = Field(
        description=(
            "상품명 제안 — 검색 노출을 고려해 [핵심 소재/특징] + [상품 유형] + [용도/핏] "
            "순으로 조합한다(예: '면 100% 오버핏 반팔 셔츠'). 40자 이내. 특수문자·이모지·"
            "과장 수식어('최고의', '완벽한')는 쓰지 않는다. 사진에서 못 읽은 정보(브랜드명 등)는 "
            "지어내지 않고 비운다."
        )
    )
    summary: str = Field(
        description=(
            "한 줄 요약 — name 을 그대로 반복하지 않고, 소재·핏·용도 중 name 에 못 담은 "
            "특징 하나를 보탠다(예: name='면 오버핏 셔츠' → summary='캐주얼하게 걸치기 좋은 "
            "박시 실루엣')."
        )
    )
    description: str = Field(
        description=(
            "상세 설명 2~4문장 — 사진에서 확인 가능한 사실만 쓰되, 나열형('소재: 면. 색상: "
            "네이비.')이 아니라 자연스러운 문장으로 연결한다. 첫 문장은 가장 눈에 띄는 특징, "
            "이어서 소재감·디테일(포켓·마감 등 사진에서 보이는 것), 마지막은 어떤 상황에 "
            "어울리는지로 마무리한다. '고급스러운', '완벽한' 같은 근거 없는 미사여구는 "
            "쓰지 않는다 — 사진에서 보이는 구체적 사실로 매력을 전달한다."
        )
    )
    category_hint: str = Field(
        description="상품군 표준 명사 1개 — 예: '셔츠', '운동화'. 수식어 없이 명사만"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="분석 확신도 0~1")


_VISION_PROMPT = (
    "당신은 커머스 상품 등록 도우미다. 판매자가 올린 상품 사진을 보고 등록 초안에 쓸 "
    "상품 정보를 추출한다.\n"
    "[규칙 — 절대 준수]\n"
    "- 사진에서 확인 가능한 사실만 적는다 — 브랜드·가격·재고·사이즈는 추측하지 않는다.\n"
    "- name 은 검색 친화적으로(소재·형태·용도), 과장 표현 없이.\n"
    "- name·summary·description 은 밋밋한 사실 나열이 아니라, 그 상품을 처음 보는 손님에게 "
    "설명하듯 자연스러운 문장으로 쓴다. 다만 사진에서 확인 못 한 소재·원산지·인증·효능은 "
    "절대 지어내지 않는다 — 매력적으로 쓰는 것과 사실을 부풀리는 것은 다르다.\n"
    "- category_hint 는 상품군 표준 명사 1개만 — 카테고리 사전 검색의 질의어로 쓰인다.\n"
    "- 사진이 상품 사진이 아니면(풍경·문서 등) confidence 를 0.2 이하로 낮춘다."
)


async def analyze_product_images(
    image_urls: list[str], *, seller_message: str = ""
) -> ProductImageAnalysis | None:
    """이미지 1회 분석 → ProductImageAnalysis. 실패는 None degrade(모듈 docstring).

    seller_message 를 함께 주는 이유: "이 셔츠 등록해줘" 같은 발화가 카테고리 힌트를
    이미 담고 있을 수 있다 — 사진과 발화가 어긋나면 사진을 우선하되 참고는 한다.
    """
    settings = get_settings()
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"판매자 발화(참고): {seller_message}\n"
                "아래 상품 사진을 분석해 구조화 출력으로 반환하라."
            ),
        }
    ]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)
    try:
        model = init_seller_model("vision").with_structured_output(ProductImageAnalysis)
        with trace_span("llm.seller.vision", "llm"):
            result = await asyncio.wait_for(
                model.ainvoke(
                    [SystemMessage(content=_VISION_PROMPT), HumanMessage(content=content)]
                ),
                timeout=settings.seller_vision_timeout_s,
            )
    except LLMNotConfigured:
        raise  # 미구성은 상위 계약(LLM_UNAVAILABLE 안내) 소관 — degrade 로 삼키지 않는다.
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("seller vision timeout urls=%d", len(image_urls))
        return None
    except Exception:
        logger.warning("seller vision failed urls=%d", len(image_urls), exc_info=True)
        return None
    if not isinstance(result, ProductImageAnalysis):
        logger.warning("seller vision returned non-schema output type=%s", type(result).__name__)
        return None
    return result
