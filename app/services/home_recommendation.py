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

import logging
import time
import uuid

from fastapi import HTTPException

from app.agents.profile.reader import read_profile_summary
from app.core.config import Settings, get_settings
from app.core.text import _strip_unsafe
from app.pipelines.artifact_store import ArtifactStore, get_catalog_store
from app.schemas.recommendations import (
    HomeRecommendationItem,
    HomeRecommendationRequest,
    HomeRecommendationResponse,
)

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


# reason 문장 틀. 태그가 뒤에 조사를 요구하지 않도록 **`에` 형태로 통일**한다 — 한국어 조사는
# 받침에 따라 을/를·이/가가 갈리는데 태그는 자유 텍스트라 형태를 미리 알 수 없다.
_CART_FRAME = "장바구니 상품과 함께 {tag}에 쓰기 좋아요"
_VIEWED_FRAME = "최근 보신 상품처럼 {tag}에 맞아요"
_PROFILE_FRAME = "{tag}에 맞춰 골랐어요"
_SELF_SIGNAL = "최근 관심 있게 보신 상품이에요"


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
    k: int | None = None,
) -> list[int]:
    """질의 벡터에 가까운 상위 k productId. 구매 이력은 여기서 제외된다.

    **전량을 끌어와 정렬하지 않는다** — 7,220건 실측에서 그 방식이 p50 3.3초로 I-22 예산
    (연결 2s/응답 3s)을 그 자체로 넘겼다. 스토어의 `top_k_by_vector` 에 위임해 pg 경로는
    HNSW 인덱스로 DB 에서 자르고, 인메모리 경로는 정확한 코사인을 쓴다.

    **결정적이어야 한다**(완료조건: 동일 snapshot·config → 동일 ranking) — 동점은 양쪽 구현
    모두 `product_id` 오름차순으로 tiebreak 한다.
    """
    limit = k if k is not None else settings.home_reco_max_items
    return store.top_k_by_vector(query_vec, k=limit, exclude=exclude)


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


def _tags(art) -> list[str]:
    """상품의 상황 태그 — I-17 배치가 미리 만들어 `extras` 에 넣어둔 재료(§4.8).

    키가 없으면 빈 목록이다. 구 `tags`(enrichment 초기 스키마)도 함께 본다 — 덤프로 들어온 7,220건은
    `situation_tags`, 배치가 새로 만든 상품은 양쪽을 다 가질 수 있어 어느 쪽이든 재료로 쓴다.
    """
    if art is None or not isinstance(art.extras, dict):
        return []
    out: list[str] = []
    for key in ("situation_tags", "tags"):
        v = art.extras.get(key)
        if isinstance(v, list):
            out.extend(str(t) for t in v if isinstance(t, str) and t.strip())
    return out


def _first_pro(art) -> str:
    """리뷰 장점 첫 항목 — 매칭이 하나도 없을 때 쓰는 상품 고유 폴백."""
    if art is None or not isinstance(art.extras, dict):
        return ""
    v = art.extras.get("review_pros")
    if isinstance(v, list) and v and isinstance(v[0], str):
        return v[0]
    return ""


def _pick(candidate_tags: list[str], context_tags: set[str]) -> str | None:
    """후보와 맥락의 공통 태그 1개. 여러 개면 사전순 첫 항목 — **결정적이어야 한다**."""
    common = sorted(set(candidate_tags) & context_tags)
    return common[0] if common else None


def build_reasons(
    *,
    product_ids: list[int],
    store: ArtifactStore,
    cart_ids: list[int],
    viewed_ids: list[int],
    profile_markdown: str,
    settings: Settings,
) -> dict[int, str]:
    """미리 만들어 둔 `extras` 재료로 reason 을 **고른다**. LLM 호출 0회.

    요청 경로에서 문장을 생성하지 않는 이유는 실측이다 — `gpt-5-nano` 배치 1회가 후보 20개 7970ms,
    12개 3852ms, 6개 2102ms 로 I-22 예산(연결 2s/응답 3s, §3.7)을 넘겼다. 비싼 LLM 작업은 I-17
    배치가 **상품당 1회** 수행해 `extras` 에 넣어두고(§4.8), 여기서는 사용자 맥락에 맞는 재료를
    고르기만 한다. 따라서 결정적이며(동일 입력 → 동일 reason) 예산과 무관하다.

    우선순위는 신호 강도를 따른다 — 담기(§3.7 "강한 신호") > 조회 > 프로필 > 상품 고유 폴백.
    아무것도 못 고르면 `None` 이고, reason 은 nullable 이라 계약상 정상이다(P-5 는 표시하지 않는다).
    """
    wanted = list(dict.fromkeys([*product_ids, *cart_ids, *viewed_ids]))
    arts = store.get_many(wanted)

    cart_tags = {t for pid in cart_ids for t in _tags(arts.get(pid))}
    viewed_tags = {t for pid in viewed_ids for t in _tags(arts.get(pid))}
    signal_ids = set(cart_ids) | set(viewed_ids)

    out: dict[int, str] = {}
    for pid in product_ids:
        art = arts.get(pid)
        tags = _tags(art)
        text: str | None = None

        if pid in signal_ids:
            # 시그널 상품 자신이 후보로 올라온 경우 — "비슷한 상품" 서술이 어색하다.
            text = _SELF_SIGNAL
        elif (tag := _pick(tags, cart_tags)) is not None:
            text = _CART_FRAME.format(tag=tag)
        elif (tag := _pick(tags, viewed_tags)) is not None:
            text = _VIEWED_FRAME.format(tag=tag)
        elif profile_markdown and (
            tag := next((t for t in sorted(set(tags)) if t in profile_markdown), None)
        ):
            # 프로필 원문은 **여기서 매칭에만** 쓰고 응답·로그로는 내보내지 않는다(§3.7 [HARD]).
            text = _PROFILE_FRAME.format(tag=tag)
        elif pro := _first_pro(art):
            text = pro

        cleaned = _sanitize_reason(text, settings.reason_max_len) if text else ""
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
        return _respond(
            "NO_PROFILE",
            [],
            {},
            started=started,
            candidates=0,
            settings=settings,
        )

    exclude = set(signals.recent_purchased_product_ids)  # 가중치가 아니라 제외 필터(§3.7)
    # 필요한 만큼만 가져온다 — overfetch 목표와 부족 판정선 중 큰 쪽. 전량을 끌어오면 예산을 넘긴다.
    want = _overfetch_size(request.limit, settings)
    try:
        ranked = rank_candidates(
            query_vec=query_vec,
            store=store,
            exclude=exclude,
            settings=settings,
            k=max(want, settings.home_reco_min_candidates),
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

    top = ranked[:want]

    # reason 은 미리 만들어 둔 `extras` 재료에서 **고른다** — LLM 호출 0회라 상위 N개로 제한할
    # 이유가 없다(전 카드 대상). 실패할 여지도 없어 degrade 경로가 필요 없다.
    reasons = build_reasons(
        product_ids=top,
        store=store,
        cart_ids=signals.cart_product_ids,
        viewed_ids=signals.recently_viewed_product_ids,
        profile_markdown=profile_markdown,
        settings=settings,
    )
    return _respond(
        "PERSONALIZED",
        top,
        reasons,
        started=started,
        candidates=len(ranked),
        settings=settings,
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
            "reasonSource": "extras" if reasons else "none",
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
