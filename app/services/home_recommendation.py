"""홈 추천 랭킹 서비스 — I-22 (api-spec §3.7, 이슈 #148).

채팅 경로(CH-2 → I-21 → CH-5)와 **후보 확보 경로가 다르다**. 채팅은 질의 시점 Spring 검색(I-1)에
위임하지만, 홈은 발화가 없어 검색어를 만들 수 없다 — 대신 **자체 카탈로그 인덱스**(I-17 로 동기화된
임베딩, §4.8)에서 시그널 기반 벡터 근접으로 순위를 매긴다. 재고·판매상태 반영은 Spring 이 카드 조립
시점에 한다(CH-5 와 같은 패턴).

**[HARD] provenance 비노출** — 프로필 원문·prompt·모델 식별자는 응답·로그·trace 어디에도 나가지
않는다. 알고리즘·모델 버전은 와이어에 싣지 않는다(§3.7). 이 모듈의 로그는 유계 코드와 개수만 남긴다.

**정본과의 차이 1건(의도)** — §3.7 은 시그널 임베딩을 "프로필 벡터와 가중 혼합"한다고 쓰지만, 현재
프로필은 자연어 markdown 요약이라 벡터가 없다(`SPEC-PROFILE-001`). 프로필을 임베딩하려면 요청 경로에
Google 임베딩 API 왕복이 하나 더 붙어 P-5 예산(연결 2s/응답 3s)을 위협한다. 그래서 **질의 벡터는
시그널만으로 만들고**, 프로필은 reason 문장의 취향 근거로만 쓴다. 프로필 벡터가 생기면
`_build_query_vector` 에 항을 더하는 것이 전부다(주입형 seam).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import HTTPException

from app.agents.profile.reader import read_profile_summary
from app.core.config import Settings, get_settings
from app.core.llm import get_llm
from app.core.text import _strip_unsafe
from app.pipelines.artifact_store import ArtifactStore, get_catalog_store
from app.schemas.recommendations import (
    HomeRecommendationItem,
    HomeRecommendationRequest,
    HomeRecommendationResponse,
)
from app.services.search_service import cosine_similarity

logger = logging.getLogger(__name__)


class UpstreamUnavailable(HTTPException):
    """503 UPSTREAM_UNAVAILABLE — 랭킹에 필수인 내부 의존성(카탈로그 인덱스) 일시 장애 (§3.7).

    **cold start 와 혼동하면 안 된다.** 프로필 부재·후보 부족은 정상 200(`NO_PROFILE`·
    `INSUFFICIENT_CANDIDATES`)이고, 이 예외는 "랭킹을 시도조차 못 했다"는 뜻이다. Spring 은
    양쪽 다 P-4 로 대체하지만 `fallbackReason` 을 각각 다르게 기록한다(§4.11).
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail={"code": "UPSTREAM_UNAVAILABLE", "message": "추천 의존성 일시 장애"},
        )


_REASON_SYSTEM = (
    "너는 커머스 홈 화면의 추천 이유를 쓰는 카피라이터다. "
    "각 상품마다 '왜 이 사용자에게 맞는지'를 한국어 한 문장(40자 이내)으로 쓴다. "
    "가격·재고·할인율은 알 수 없으니 언급하지 않는다. "
    'JSON 만 출력한다: {"reasons":[{"productId":숫자,"reason":"문장"}]}'
)


def _weighted_add(acc: list[float], vec: list[float], weight: float) -> list[float]:
    """acc += vec * weight. 차원이 다르면(모델 교체·마이그레이션) 조용히 섞지 않고 무시한다."""
    if not acc:
        return [v * weight for v in vec]
    if len(acc) != len(vec):
        return acc
    return [a + v * weight for a, v in zip(acc, vec, strict=True)]


def build_query_vector(
    *,
    cart_ids: list[int],
    viewed_ids: list[int],
    store: ArtifactStore,
    settings: Settings,
) -> list[float]:
    """시그널 상품 임베딩의 가중 평균으로 질의 벡터를 만든다 (§3.7 signals 표).

    cart 는 담기까지 갔다는 강한 신호라 조회보다 가중치가 높고, 조회는 최신일수록 높다(배열 앞이 최신
    이라는 §3.7 전제 — decay 를 인덱스 거듭제곱으로 적용). 인덱스에 임베딩이 없는 시그널은 건너뛴다.
    하나도 못 찾으면 빈 리스트를 반환하며, 호출부는 이를 `NO_PROFILE` 로 해석한다.
    """
    wanted = list(dict.fromkeys([*cart_ids, *viewed_ids]))  # 중복 제거(순서 보존), 조회 1회
    if not wanted:
        return []
    arts = store.get_many(wanted)

    acc: list[float] = []
    for pid in cart_ids:
        art = arts.get(pid)
        if art and art.embedding:
            acc = _weighted_add(acc, art.embedding, settings.home_reco_weight_cart)
    for rank, pid in enumerate(viewed_ids):
        art = arts.get(pid)
        if art and art.embedding:
            weight = settings.home_reco_weight_viewed * (settings.home_reco_viewed_decay**rank)
            acc = _weighted_add(acc, art.embedding, weight)
    return acc


def rank_candidates(
    *,
    query_vec: list[float],
    store: ArtifactStore,
    exclude: set[int],
    settings: Settings,
) -> list[int]:
    """질의 벡터에 가까운 순으로 productId 를 반환한다. 구매 이력은 여기서 제외된다.

    **결정적이어야 한다**(완료조건: 동일 snapshot·config → 동일 ranking). 코사인 동점 시 저장소
    순회 순서에 기대면 pg 행 순서에 따라 순위가 흔들리므로 `productId` 오름차순으로 tiebreak 한다.
    """
    scored: list[tuple[float, int]] = []
    for art in store.all():
        if art.product_id in exclude or not art.embedding:
            continue
        scored.append((cosine_similarity(query_vec, art.embedding), art.product_id))
    # (-score, productId) 정렬 = 점수 내림차순 + 동점은 id 오름차순(결정적 tiebreak)
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [pid for _, pid in scored]


def _overfetch_size(limit: int, settings: Settings) -> int:
    """limit(최종 노출 목표) 대비 넉넉히 반환할 개수 — Spring 이 품절을 뺀 뒤 자른다(§3.7)."""
    target = int(limit * settings.home_reco_overfetch_ratio)
    return max(limit, min(target, settings.home_reco_max_items))


def _sanitize_reason(text: str, max_len: int) -> str:
    """reason 방어 정제 — I-21(§4.2)과 동일 규약.

    LLM 자유 텍스트가 신뢰경계(→Spring→P-5→FE)를 넘기 전에 제어·포맷 문자를 제거하고 공백을 접은 뒤
    안전 상한으로 자른다. 표시 목표(40자)는 프롬프트로 유도하고 max_len 은 비정상 초장문 방어캡이다.
    """
    collapsed = _strip_unsafe(text)
    if max_len <= 0:
        return ""
    if len(collapsed) > max_len:
        collapsed = collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def _reason_prompt(
    *, product_ids: list[int], store: ArtifactStore, profile_markdown: str, signal_docs: list[str]
) -> str:
    """reason 배치 프롬프트. 프로필 요약은 여기(입력)에만 쓰고 응답·로그로는 절대 내보내지 않는다."""
    arts = store.get_many(product_ids)
    lines = [f"- productId={pid}: {arts[pid].search_doc}" for pid in product_ids if pid in arts]
    parts = []
    if profile_markdown:
        parts.append(f"[사용자 취향 요약]\n{profile_markdown}")
    if signal_docs:
        parts.append("[최근 관심 상품]\n" + "\n".join(f"- {d}" for d in signal_docs))
    parts.append("[추천 후보]\n" + "\n".join(lines))
    return "\n\n".join(parts)


async def _generate_reasons(
    *,
    product_ids: list[int],
    store: ArtifactStore,
    profile_markdown: str,
    signal_docs: list[str],
    settings: Settings,
) -> dict[int, str]:
    """상위 N개 reason 을 **fast tier 배치 1회**로 만든다. 어떤 실패든 빈 맵으로 degrade한다.

    P-5 예산이 3s 라 상품별 호출은 불가능하다. 타임아웃·예외·JSON 파싱 실패는 모두 삼키고 빈 맵을
    돌려준다 — reason 은 nullable 이고, 이유가 없다고 홈 렌더가 막히면 안 된다(§3.7·§4.11).
    """
    llm = get_llm()
    if llm is None or not product_ids:
        return {}

    prompt = _reason_prompt(
        product_ids=product_ids,
        store=store,
        profile_markdown=profile_markdown,
        signal_docs=signal_docs,
    )
    try:
        raw = await asyncio.wait_for(
            llm.complete(system=_REASON_SYSTEM, user=prompt, tier="fast", json_output=True),
            timeout=settings.home_reco_reason_timeout_s,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("home_reco_reason_timeout")
        return {}
    except Exception:
        # 예외 문자열은 업스트림 상태를 유출할 수 있어 클래스명조차 남기지 않는다(#141 규약).
        logger.warning("home_reco_reason_failed")
        return {}

    try:
        payload = json.loads(raw)
        rows = payload["reasons"]
    except (json.JSONDecodeError, TypeError, KeyError):
        logger.warning("home_reco_reason_malformed")
        return {}

    allowed = set(product_ids)
    out: dict[int, str] = {}
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid, reason = row.get("productId"), row.get("reason")
        # bool 은 int 의 서브클래스라 명시 배제. 후보 밖 id 는 무시(LLM 환각 방어).
        if not isinstance(pid, int) or isinstance(pid, bool) or pid not in allowed:
            continue
        if not isinstance(reason, str):
            continue
        cleaned = _sanitize_reason(reason, settings.reason_max_len)
        if cleaned:
            out[pid] = cleaned
    return out


async def rank_home(request: HomeRecommendationRequest) -> HomeRecommendationResponse:
    """I-22 본체 — outcome 3종을 모두 200 으로 답한다 (§3.7).

    프로필 부재·후보 부족으로 예외를 던지지 않는다. fallback 판단은 Spring 이 하며, AI 는 무슨 일이
    있었는지를 `outcome` 으로만 알린다.
    """
    started = time.perf_counter()
    settings = get_settings()
    signals = request.signals

    # 프로필 저장소 장애는 **degrade** 한다 — 이 구현에서 프로필은 reason 문장의 취향 근거일 뿐
    # 랭킹 입력이 아니라, 없다고 추천을 못 만들지 않는다. 홈 렌더를 막지 않는 쪽이 §4.11 취지에 맞다.
    # 신원은 서비스 토큰이 인가하고 memberId 는 §3.7 계약상 본문으로 온다(레인 b).
    try:
        profile = await read_profile_summary(str(request.member_id))
    except Exception:
        # 예외 문자열은 업스트림 상태를 유출할 수 있어 클래스명도 남기지 않는다(#141 규약).
        logger.warning("home_reco_profile_unavailable")
        profile = None
    profile_markdown = (profile or {}).get("markdown", "") or ""

    # 카탈로그 인덱스는 **랭킹의 유일한 입력**이라 degrade 할 수 없다 — 여기가 죽으면 503 이고,
    # Spring 이 P-4 로 대체하며 fallbackReason=AI_ERROR 로 기록한다(§3.7 실패 응답표·§4.11).
    try:
        store = get_catalog_store()
        query_vec = build_query_vector(
            cart_ids=signals.cart_product_ids,
            viewed_ids=signals.recently_viewed_product_ids,
            store=store,
            settings=settings,
        )
    except Exception as exc:
        logger.warning("home_reco_catalog_unavailable")
        raise UpstreamUnavailable from exc

    # 1) 개인화 근거가 없으면 NO_PROFILE — 신규 회원이거나 시그널이 비었거나, 시그널 상품이 아직
    #    인덱스에 없는 경우다. 셋 다 Spring 입장에선 "P-4 인기상품으로 대체"라 구분할 필요가 없다.
    if not query_vec:
        return _respond("NO_PROFILE", [], {}, started=started, candidates=0, settings=settings)

    exclude = set(signals.recent_purchased_product_ids)  # 가중치가 아니라 제외 필터(§3.7)
    try:
        ranked = rank_candidates(
            query_vec=query_vec, store=store, exclude=exclude, settings=settings
        )
    except Exception as exc:
        logger.warning("home_reco_catalog_unavailable")
        raise UpstreamUnavailable from exc

    # 2) 후보가 부족하면 랭킹이 무의미하다 — INSUFFICIENT_CANDIDATES (역시 200)
    if len(ranked) < settings.home_reco_min_candidates:
        return _respond(
            "INSUFFICIENT_CANDIDATES",
            [],
            {},
            started=started,
            candidates=len(ranked),
            settings=settings,
        )

    top = ranked[: _overfetch_size(request.limit, settings)]

    signal_arts = store.get_many(
        list(dict.fromkeys([*signals.cart_product_ids, *signals.recently_viewed_product_ids]))
    )
    reasons = await _generate_reasons(
        product_ids=top[: settings.home_reco_reason_max_items],
        store=store,
        profile_markdown=profile_markdown,
        signal_docs=[a.search_doc for a in signal_arts.values() if a.search_doc],
        settings=settings,
    )
    return _respond(
        "PERSONALIZED", top, reasons, started=started, candidates=len(ranked), settings=settings
    )


def _respond(
    outcome: str,
    product_ids: list[int],
    reasons: dict[int, str],
    *,
    started: float,
    candidates: int,
    settings: Settings,
) -> HomeRecommendationResponse:
    """응답 조립 + 관측 로그 1건.

    로그 key set 은 고정이며 **memberId·productId·프로필 원문·모델 식별자·토큰을 남기지 않는다**
    (§3.7 [HARD]·§6.3). 남기는 것은 무슨 일이 있었는지 판별할 유계 코드와 개수뿐이다.
    """
    logger.info(
        "home_reco_request",
        extra={
            "event": "home_reco_request",
            "outcome": outcome,
            "candidateCount": candidates,
            "returnedCount": len(product_ids),
            "reasonSource": "llm" if reasons else "none",
            "elapsedMs": int((time.perf_counter() - started) * 1000),
        },
    )
    return HomeRecommendationResponse(
        outcome=outcome,  # type: ignore[arg-type]
        # 재시도 = 새 추천 실행. 멱등이 아니라 호출마다 새로 발급한다(§3.7).
        recommendation_request_id=str(uuid.uuid4()),
        # ≥128bit 무작위 — P-5 를 거쳐 공개되는 귀속 키라 추측 가능한 형식 금지(I-21 §4.2 동일 규칙).
        list_id=uuid.uuid4().hex,
        items=[
            HomeRecommendationItem(product_id=pid, reason=reasons.get(pid)) for pid in product_ids
        ],
    )
