"""카테고리 LLM 택1 (#506 후속) — "대충 말한 카테고리"를 실제 카테고리 id 로 확정한다.

정상 경로에서 카테고리를 고르는 주체는 product 에이전트다 — 입력의 [카테고리 후보]
블록에서 id 하나를 골라 changes 에 싣는다(prompts.PRODUCT_PROMPT). 이 모듈은 **그
경로가 비었을 때만** 도는 폴백이다:

  ① 에이전트가 category 를 아예 안 실었다(후보가 애매해 포기).
  ② 실었지만 스냅샷에 없는 값이다(경로·이름을 적었거나 지어냈다).

두 경우 모두 구 구현은 그대로 흘러가 confirm 시점에 BE 가 `categoryId` 누락으로
등록을 거부했다("internal 에러"의 실체). 여기서 **더 넓은 후보**(문자열 검색 k×3)를
다시 뽑아 전용 LLM 에게 택1 시키고, 그래도 못 고르면 되묻기로 넘긴다 — 잘못된
카테고리로 등록되는 것보다 한 번 더 묻는 편이 싸다(등록 후 변경 불가 필드).

비용: 폴백 1회 추가 호출뿐이라 happy path 지연은 그대로다. 실패·타임아웃은 None
degrade — 호출부(validate_draft)가 되묻기 문구를 낸다.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.seller import category_catalog
from app.agents.seller.models import init_seller_model
from app.core.config import get_settings
from app.core.llm import LLMNotConfigured
from app.core.tracing import trace_span

logger = logging.getLogger(__name__)


class CategoryPick(BaseModel):
    """택1 결과 — id 는 후보 목록 안의 값이거나 빈 문자열(포기)이다."""

    category_id: str = Field(
        default="",
        description="고른 카테고리 id — 후보 목록의 id 를 그대로. 마땅한 게 없으면 빈 문자열",
    )
    reason: str = Field(default="", description="한 줄 근거 — 로그·추적용, 사용자에게 보이지 않음")


_RESOLVER_PROMPT = """\
당신은 커머스 카테고리 배정 담당이다. 판매자가 등록하려는 상품이 어느 카테고리에
속하는지, **주어진 후보 목록에서 정확히 하나**를 고른다.

[규칙 — 절대 준수]
- category_id 는 후보 목록에 있는 id 를 **그대로** 적는다. 목록에 없는 id·경로·이름을
  적지 않는다.
- 후보 경로는 "대분류 > 중분류 > 소분류" 형식이다. 소분류(마지막 칸)가 상품군과
  맞는 것을 우선하고, 소분류가 비슷하면 대분류가 맞는 것을 고른다.
- 성별·연령 구분이 있는 후보(남성/여성/브랜드/아동)는 판매자 발화에 근거가 있을
  때만 그쪽을 고른다. 근거가 없으면 가장 일반적인 후보를 고른다.
- 상품군이 후보 어디에도 없으면 **억지로 고르지 말고** category_id 를 빈 문자열로
  둔다 — 잘못 배정하면 판매자가 등록 후 되돌릴 수 없다.
"""


def _build_input(*, message: str, hint: str | None, candidates: list) -> str:
    blocks = [f"[판매자 발화]\n{message.strip() or '(없음)'}"]
    if hint:
        blocks.append(f"[사진 분석 상품군]\n{hint}")
    blocks.append(
        "[카테고리 후보] (id | 대분류 > 중분류 > 소분류)\n"
        + category_catalog.candidates_block(candidates)
    )
    return "\n\n".join(blocks)


def wide_candidates(message: str, hint: str | None = None) -> list[category_catalog.CategoryEntry]:
    """폴백용 넓은 후보 — 발화·사진 힌트 각각으로 검색해 합친다(중복 제거).

    좁은 후보(주입용 k)로 이미 실패했으므로 같은 폭으로 다시 물어봐야 의미가 없다.
    """
    settings = get_settings()
    k = max(settings.seller_category_candidates_k, 1) * settings.seller_category_fallback_k_factor
    merged: dict[str, category_catalog.CategoryEntry] = {}
    if hint and hint.strip():
        for entry in category_catalog.search(hint, k):
            merged.setdefault(entry.id, entry)
    for entry in category_catalog.search(message, k):
        merged.setdefault(entry.id, entry)
    return list(merged.values())[:k]


async def resolve_category(
    message: str,
    *,
    hint: str | None = None,
    candidates: list[category_catalog.CategoryEntry] | None = None,
) -> category_catalog.CategoryEntry | None:
    """발화(+사진 힌트) → 카테고리 1건. 확정하지 못하면 None(호출부가 되묻는다).

    후보가 0건이면 LLM 을 부르지 않는다 — 고를 게 없는데 부르면 지연만 늘고, 목록 밖
    값을 지어낼 여지만 준다.
    """
    entries = candidates if candidates is not None else wide_candidates(message, hint)
    if not entries:
        return None
    if len(entries) == 1:
        # 후보가 하나면 LLM 을 부를 이유가 없다(같은 답을 비싸게 받는 셈).
        return entries[0]

    settings = get_settings()
    try:
        model = init_seller_model("category").with_structured_output(CategoryPick)
        with trace_span("llm.seller.category_resolve", "llm"):
            result = await asyncio.wait_for(
                model.ainvoke(
                    [
                        SystemMessage(content=_RESOLVER_PROMPT),
                        HumanMessage(
                            content=_build_input(message=message, hint=hint, candidates=entries)
                        ),
                    ]
                ),
                timeout=settings.seller_category_resolve_timeout_s,
            )
    except LLMNotConfigured:
        raise  # 미구성은 상위 계약(LLM_UNAVAILABLE 안내) 소관 — degrade 로 삼키지 않는다.
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("seller category resolve timeout candidates=%d", len(entries))
        return None
    except Exception:
        logger.warning("seller category resolve failed", exc_info=True)
        return None

    if not isinstance(result, CategoryPick) or not result.category_id.strip():
        return None
    allowed = {entry.id for entry in entries}
    picked = result.category_id.strip()
    if picked not in allowed:
        # 목록 밖 값은 신뢰하지 않는다 — 스냅샷에 있더라도 후보로 보여준 적 없는 값이다.
        logger.warning("seller category resolve returned out-of-list id=%s", picked)
        return None
    return category_catalog.get(picked)
