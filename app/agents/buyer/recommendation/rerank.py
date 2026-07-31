"""rerank 노드 — Sonnet 1회로 후보를 프로필 기반 재랭킹 + 근거 생성 (SPEC-RECOMMEND-001 §6.4).

MVP 슬라이스: 후보 외 id 미노출(REQ-REC-081 — 값싼 부분집합 대조만)·노출 상한(config)만 강제한다.
근거 속성 결정적 대조(REQ-REC-082)·순서 무작위화(REQ-REC-080)·출력검증 degrade 상태 기록은 후속.
실패/타임아웃/유효 후보 0건은 LLMError 로 전파 — 상위가 검색순서 degrade 로 처리한다(§7).
"""

from __future__ import annotations

import json

from app.agents.buyer.recommendation.state import RerankResult, extract_json
from app.core.config import get_settings
from app.core.llm import LLMClient, LLMError
from app.schemas.spring import SpringProduct

_SYSTEM = """당신은 커머스 추천 재랭킹기입니다. 후보 상품과 사용자 질의(+프로필)를 받아
가장 적합한 순서로 재랭킹하고 상품마다 한글 40자 이내의 1문장 한국어 근거를 답니다.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{"ranked": [{"productId": int, "rationale": "한글 40자 이내 1문장 근거"}], "overallComment": "전체 1~2문장 코멘트"}
규칙:
- productId 는 반드시 후보 목록(CANDIDATES)에 있는 값만 쓰세요. 없는 id 를 만들지 마세요.
- 후보가 실제로 갖지 않은 속성(브랜드·평점 등)을 근거로 주장하지 마세요.
- ratingLevel(평가없음/낮음/보통/높음/매우높음)·reviewLevel(정보없음/없음/적음/보통/많음/매우많음)은 등급이며, 근거는 정성적으로만 쓰세요(예: "평점이 높아요", "리뷰가 많아요"). '평가없음'·'정보없음'은 데이터가 없다는 뜻이니 평점·리뷰를 근거로 삼지 마세요. 등급을 그대로 인용해도 좋지만 없는 숫자를 지어내지 마세요.
- rationale 은 한글 40자 이내 1문장으로 간결하게 — 개행 없이.
- 가장 적합한 순으로 정렬하고 상위만 남기세요."""


def _rating_tier(product: SpringProduct, settings) -> str:
    """rerank LLM 에 넘길 평점 등급(정확한 숫자 대신) — 비표시 수치 유출 원천 차단(#171 PR#172).

    정확한 rating(4.8)을 주면 LLM 이 근거문에 "4.8 평점"처럼 흘려 CH-5 표시값과 어긋날 수 있어,
    LLM 에는 등급만 준다. 정확한 rating 은 SpringProduct.rating 에 남아 코드 필터(rating_min)·예산이
    계속 정확히 쓴다(질의 "평점 4.5 이상"은 search_catalog 사후필터 소관, 티어화 무영향). review_count
    ==0(리뷰 없음)·rating None 은 데이터 부재로 '평가없음'(#171 rating=0 판별 유지). 경계는 config
    주입(settings) — 호출부가 get_settings()를 1회 조회해 전달한다(핫패스 루프 내 반복 조회 회피).
    """
    if product.rating is None or product.review_count == 0:
        return "평가없음"
    r = product.rating
    if r >= settings.rating_tier_excellent:
        return "매우높음"
    if r >= settings.rating_tier_good:
        return "높음"
    if r >= settings.rating_tier_fair:
        return "보통"
    return "낮음"


def _review_tier(product: SpringProduct, settings) -> str:
    """rerank LLM 에 넘길 리뷰량 등급(정확한 개수 대신) — 유출 원천 차단(#171 PR#172).

    정확한 reviewCount(128)를 주면 LLM 이 "리뷰 128개"처럼 흘릴 수 있어 등급만 준다. None(미전송)은
    '정보없음', 0 은 '없음'(신상). 정확한 개수는 원본에 남아 계산용으로 쓴다. 경계는 config 주입.
    """
    rc = product.review_count
    if rc is None:
        return "정보없음"
    if rc == 0:
        return "없음"
    if rc >= settings.review_tier_many:
        return "매우많음"
    if rc >= settings.review_tier_some:
        return "많음"
    if rc >= settings.review_tier_few:
        return "보통"
    return "적음"


async def rerank(
    llm: LLMClient,
    *,
    query: str,
    candidates: list[SpringProduct],
    profile_summary: str | None,
    tier: str,
    expose_max: int,
    need_of: dict[int, str] | None = None,
    per_need: int | None = None,
) -> RerankResult:
    """Sonnet 1회 호출로 재랭킹 결과를 산출한다(후보 외 id 는 코드로 제거).

    `need_of`(productId → 니즈 이름)와 `per_need` 가 오면 **니즈별 분할 턴**이다(REQ-REC-024).
    이때만 후보에 소속 니즈를 싣고 "니즈마다 상위 N개" 지시를 덧붙인다 — 안 넘기면 LLM 은
    후보를 전역 관련도로만 정렬해 한 니즈가 상위권을 쓸 수 있고, 굶은 니즈는 하류에서
    검색순서로 보충된다. 그 보충분엔 rationale 이 없어 **근거 없는 카드**가 나가는데
    rerank 는 "정상 성공"이라 rerank_degraded 로도 드러나지 않는다(PR #212 리뷰).

    분기를 `_SYSTEM` 이 아니라 user 메시지에 둔 이유: 니즈가 없는 단일 목록 경로의 프롬프트를
    **한 글자도 바꾸지 않기 위해서**다. 이 저장소엔 프롬프트에 지시를 얹었다가 기존 성공
    케이스가 3/3 → 1/3 로 희석된 실측 전례가 있다(#198, SPEC §EX-7).
    """
    settings = get_settings()  # 티어 경계 조회 — 후보 루프 밖에서 1회(캐시 싱글턴, 관례 정합)
    cand = [
        {
            "productId": c.product_id,
            "name": c.name,
            "brand": c.brand,
            "price": c.price,  # 예산 판단용 정밀값(선존재) — 근거문 유출 방어는 별도 #173
            # rating·reviewCount 는 정확한 숫자 대신 등급으로 — LLM 이 "4.8 평점"·"리뷰 128개"처럼
            # 흘려 CH-5 표시값과 어긋나는 걸 원천 차단한다(#171 PR#172). 정확한 값은 원본에 남아
            # 코드 필터·예산이 계속 쓴다. review_count==0 → 평가없음(#171 rating=0 판별 유지).
            "ratingLevel": _rating_tier(c, settings),
            "reviewLevel": _review_tier(c, settings),
            "category": c.category,
        }
        for c in candidates
    ]
    if need_of:
        # 니즈별 분할 턴에서만 실린다 — 상품의 category(분류 경로)와 달리 "이 후보가 어느
        # 니즈를 위해 검색됐는가"라, LLM 이 니즈 경계를 알아야 균형 있게 고를 수 있다.
        for item in cand:
            need = need_of.get(item["productId"])
            if need:
                item["need"] = need
    prof = profile_summary or "(없음)"
    # 니즈 지시는 user 메시지에만 덧붙인다 — 단일 목록 경로의 프롬프트를 그대로 두기 위해서다.
    needs_line = ""
    if need_of and per_need:
        names = list(dict.fromkeys(need_of.values()))  # 등장 순서 유지 + 중복 제거
        needs_line = (
            f"NEEDS: {json.dumps(names, ensure_ascii=False)}\n"
            f"- 후보의 need 필드가 그 상품이 속한 니즈입니다. 니즈마다 상위 {per_need}개까지"
            " 균형 있게 고르세요 — 한 니즈에 몰아주지 마세요.\n"
        )
    user = (
        f"PROFILE_SUMMARY: {prof}\nQUERY: {query}\n"
        f"{needs_line}"
        f"CANDIDATES: {json.dumps(cand, ensure_ascii=False)}"
    )

    # 출력 예산은 **요청한 노출 개수에 비례**한다 — 고정값이면 니즈별 분할로 항목이 늘 때
    # 응답이 잘리고 아래 extract_json 이 파싱에 실패해 LLMError → 근거 없는 degrade 가 된다.
    max_tokens = settings.rerank_max_tokens_base + settings.rerank_max_tokens_per_item * expose_max
    raw = await llm.complete(system=_SYSTEM, user=user, tier=tier, max_tokens=max_tokens)
    data = extract_json(raw)

    valid_ids = {c.product_id for c in candidates}
    ranked: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in data.get("ranked") or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("productId")
        if not isinstance(pid, int) or pid not in valid_ids or pid in seen:
            continue  # 후보 외/중복 id 제거 (REQ-REC-081)
        seen.add(pid)
        ranked.append((pid, str(item.get("rationale") or "")))
        if len(ranked) >= expose_max:
            break

    if not ranked:
        raise LLMError("rerank 가 유효한 후보 id 를 내지 않음")
    return RerankResult(ranked=ranked, overall_comment=str(data.get("overallComment") or ""))
