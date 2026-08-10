"""홈 추천 랭킹 서비스 — I-22 (api-spec §3.7, 이슈 #148).

채팅 경로(CH-2 → I-21 → CH-5)와 **후보 확보 경로가 다르다**. 채팅은 질의 시점 Spring 검색(I-1)에
위임하지만, 홈은 발화가 없어 검색어를 만들 수 없다 — 대신 **자체 카탈로그 인덱스**(I-17 로 동기화된
임베딩, §4.8)에서 시그널 기반 벡터 근접으로 순위를 매긴다. 재고·판매상태 반영은 Spring 이 카드 조립
시점에 한다(CH-5 와 같은 패턴).

**[HARD] provenance 비노출** — 프로필 원문·prompt·모델 식별자는 응답·로그·trace 어디에도 나가지
않는다. 알고리즘·모델 버전은 와이어에 싣지 않는다(§3.7). 이 모듈의 로그는 유계 코드와 개수만 남긴다.

**프로필 벡터는 미리 만들어 둔 것을 읽는다** — §3.7 대로 질의 벡터는 시그널 임베딩과 프로필 벡터의
가중 혼합이다. 프로필은 자연어 markdown 요약이라(`SPEC-PROFILE-001`) 원래 벡터가 없었고, 요청 경로에서
임베딩하면 Google API 왕복이 붙어 P-5 예산(연결 2s/응답 3s)을 위협한다. 그래서 sleep-time
consolidation 이 요약을 만들 때 벡터도 함께 만들어 두고(`profile/store._embed_summary`), 여기서는
읽어서 `build_query_vector` 에 항으로 더하기만 한다. 벡터가 없으면(구 요약·임베딩 실패) 그 항만 빠진다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid

from fastapi import HTTPException

from app.agents.profile.personalization_gate import personalization_enabled
from app.agents.profile.reader import read_profile_summary
from app.core.config import Settings, get_settings
from app.core.logging import safe_fingerprint
from app.core.reco_provenance import ProvenanceItem, ProvenanceList, emit_recommendation_provenance
from app.core.text import _strip_unsafe
from app.core.tracing import trace_span
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


class UpstreamTimeout(HTTPException):
    """504 UPSTREAM_TIMEOUT — 카탈로그 조회가 I-22 예산을 넘겼다 (§3.7 실패 응답표).

    이 코드가 없으면 계약이 정의한 504 가 어떤 경로에서도 발생하지 않는다(PR 리뷰) — pg 지연·락이
    요청을 예산 밖까지 붙들면 hang 또는 미처리 예외로 끝난다. Spring 은 fallbackReason=AI_TIMEOUT
    으로 기록하고 P-4 로 대체한다(§4.11).
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=504,
            detail={"code": "UPSTREAM_TIMEOUT", "message": "추천 의존성 응답 지연"},
        )


def _swallow_abandoned(task: "asyncio.Task") -> None:
    """포기한 스토어 태스크의 늦은 예외를 회수한다 — 안 하면 'exception was never retrieved' 경고."""
    if task.cancelled():
        return
    task.exception()


async def _call_store(fn, /, *, timeout: float, **kwargs):
    """스토어 호출을 별도 스레드에서 돌리되 **벽시계를 실제로 묶는다**.

    `asyncio.wait_for(to_thread(...))` 는 이 목적에 **틀린 도구다** — 동기 psycopg 쿼리가 이미
    스레드에서 도는 중이면 취소가 불가능해, wait_for 는 취소 완료를 기다리느라 **스레드가 끝날
    때까지 블록한 뒤에야** TimeoutError 를 던진다(이 함정을 회귀 테스트가 잡았다 — 예산 0.3s 에
    벽시계 1.15s). `asyncio.wait(timeout=...)` 는 기다림만 포기하고 즉시 돌아온다.

    포기된 스레드는 계속 돈다 — 그 쿼리는 DB 쪽 `statement_timeout`(pg_artifact_store)이 잘라
    커넥션을 회수한다. 두 층의 역할 분담이 이것이다: 여기는 **요청의 벽시계**, DB 상한은
    **커넥션 점유**.
    """
    task = asyncio.create_task(asyncio.to_thread(fn, **kwargs))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()  # 원 예외(코드 버그 포함)는 그대로 전파된다
    task.add_done_callback(_swallow_abandoned)
    raise TimeoutError


# reason 문장 틀. 태그가 뒤에 조사를 요구하지 않도록 **`에` 형태로 통일**한다 — 한국어 조사는
# 받침에 따라 을/를·이/가가 갈리는데 태그는 자유 텍스트라 형태를 미리 알 수 없다.
_CART_FRAME = "장바구니 상품과 함께 {tag}에 쓰기 좋아요"
_VIEWED_FRAME = "최근 보신 상품처럼 {tag}에 맞아요"
_SELF_SIGNAL = "최근 관심 있게 보신 상품이에요"


def _weighted_add(acc: list[float], vec: list[float], weight: float) -> list[float]:
    """acc += vec * weight. 차원이 다르면(모델 교체·마이그레이션) 조용히 섞지 않고 무시한다.

    **가중치 0 은 더하지 않은 것과 같아야 한다** — 빈 `acc` 에 0 을 곱해 넣으면 길이만 있는
    **0 벡터**가 만들어져 `if not query_vec` 판정을 통과해 버린다(`home_reco_weight_profile=0`
    롤백 스위치 + 시그널 없음 조합).
    """
    if weight == 0.0:
        return acc
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
    profile_vec: list[float] | None = None,
) -> list[float]:
    """시그널 상품 임베딩과 **프로필 벡터**의 가중 평균으로 질의 벡터를 만든다 (§3.7 signals 표).

    두 축을 섞는다 — **행동**(지금 담고 본 것)과 **장기 취향**(프로필). cart 는 담기까지 갔다는
    강한 신호라 조회보다 가중치가 높고, 조회는 최신일수록 높다(배열 앞이 최신이라는 §3.7 전제 —
    decay 를 인덱스 거듭제곱으로 적용). 프로필은 그 사이 가중치로, 오래된 취향이 지금의 관심을
    덮지 않게 한다.

    `profile_vec` 은 요약 생성 시점에 미리 만들어 둔 것이다(`profile/store._embed_summary`) —
    여기서 임베딩하면 API 왕복이 붙어 예산을 위협한다. 없으면 그 항만 빠진다.

    **시그널이 비어도 프로필만으로 질의 벡터가 선다** — 홈에 처음 들어온 회원도 대화로 쌓인 취향으로
    개인화된다. 셋 다 없을 때만 빈 리스트이고, 호출부는 이를 `NO_PROFILE` 로 해석한다.
    """
    acc: list[float] = []
    wanted = list(dict.fromkeys([*cart_ids, *viewed_ids]))  # 중복 제거(순서 보존), 조회 1회
    arts = store.get_many(wanted) if wanted else {}

    # 가중 누적도 **dedup 목록**을 돈다(조회용 wanted 와 동일) — 스키마는 중복을 막지 않아,
    # 같은 id 가 여러 번 오면 그 상품이 질의 벡터를 부당하게 지배하고 viewed 는 같은 상품이
    # 서로 다른 rank 로 중복 합산된다(PR 리뷰). 중복은 첫 등장(최신) 위치만 인정한다.
    for pid in dict.fromkeys(cart_ids):
        art = arts.get(pid)
        if art and art.embedding:
            acc = _weighted_add(acc, art.embedding, settings.home_reco_weight_cart)
    for rank, pid in enumerate(dict.fromkeys(viewed_ids)):
        art = arts.get(pid)
        if art and art.embedding:
            weight = settings.home_reco_weight_viewed * (settings.home_reco_viewed_decay**rank)
            acc = _weighted_add(acc, art.embedding, weight)
    if profile_vec:
        acc = _weighted_add(acc, profile_vec, settings.home_reco_weight_profile)
    # **0 벡터는 질의가 아니다.** 가중치가 전부 0 이거나 시그널 임베딩이 모두 0 이면 길이만 있는
    # 벡터가 남는데, 그대로 두면 `if not query_vec` 를 통과해 `PERSONALIZED` 로 응답하면서
    # 실제로는 의미 없는 순서(pg 는 거리 동점, 인메모리는 코사인 -1 → 둘 다 productId 오름차순)를
    # "개인화"로 내보낸다. 개인화 근거가 없다는 뜻이므로 빈 리스트 = `NO_PROFILE` 로 돌린다.
    return acc if any(acc) else []


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
    """limit(최종 노출 목표) 대비 넉넉히 반환할 개수 — Spring 이 품절을 뺀 뒤 자른다(§3.7).

    `ceil` 인 이유(PR 리뷰): `int()` 절단은 배율이 이진 부동소수점으로 정확히 표현되지 않을 때
    (예: 1.3 → 10×1.3 = 12.999…) 의도보다 1 작게 잘라 여유분을 조용히 깎는다. "넉넉히" 계약의
    안전한 방향은 올림이다 — 과잉은 최대 1개이고 max_items 캡이 어차피 막는다.
    """
    target = math.ceil(limit * settings.home_reco_overfetch_ratio)
    return min(max(limit, target), settings.home_reco_max_items)


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
    settings: Settings,
) -> dict[int, str]:
    """미리 만들어 둔 `extras` 재료로 reason 을 **고른다**. LLM 호출 0회.

    요청 경로에서 문장을 생성하지 않는 이유는 실측이다 — `gpt-5-nano` 배치 1회가 후보 20개 7970ms,
    12개 3852ms, 6개 2102ms 로 I-22 예산(연결 2s/응답 3s, §3.7)을 넘겼다. 비싼 LLM 작업은 I-17
    배치가 **상품당 1회** 수행해 `extras` 에 넣어두고(§4.8), 여기서는 사용자 맥락에 맞는 재료를
    고르기만 한다. 따라서 결정적이며(동일 입력 → 동일 reason) 예산과 무관하다.

    우선순위는 신호 강도를 따른다 — 담기(§3.7 "강한 신호") > 조회 > 상품 고유 폴백.
    아무것도 못 고르면 `None` 이고, reason 은 nullable 이라 계약상 정상이다(P-5 는 표시하지 않는다).

    **프로필 markdown 부분 문자열 분기는 제거했다**(PR 리뷰 3건 누적 — 오탐·길이 하한·극성).
    프로필 요약은 LLM 이 쓰는 자유 마크다운이라 선호/**회피** 극성을 파싱할 앵커가 없다 —
    "회피 브랜드: 나이키"의 "나이키"가 태그와 매칭되면 사용자가 피하겠다는 브랜드를 "맞춰
    골랐어요"로 소개한다. 문장 근거는 시그널 태그·상품 리뷰로 한정한다. 장기 취향의 랭킹 반영은
    프로필 **벡터**(build_query_vector)가 담당하므로 개인화가 사라지는 것이 아니다.
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
        elif pro := _first_pro(art):
            text = pro

        cleaned = _sanitize_reason(text, settings.reason_max_len) if text else ""
        if cleaned:
            out[pid] = cleaned
    return out


async def rank_home(
    request: HomeRecommendationRequest, *, request_id: str
) -> HomeRecommendationResponse:
    """I-22 본체 — outcome 3종을 모두 200 으로 답한다 (§3.7).

    프로필 부재·후보 부족으로 예외를 던지지 않는다. fallback 판단은 Spring 이 하며, AI 는 무슨 일이
    있었는지를 `outcome` 으로만 알린다.

    `request_id` 는 [이슈 #140] provenance `recommend_provenance` 로그의 `requestId` 로
    실린다 — 호출부(`internal.py`)가 `errors.get_request_id` 로 이미 계산한 값을 그대로
    받는다(두 번 만들지 않는다).
    """
    started = time.perf_counter()
    settings = get_settings()
    signals = request.signals

    # 예산 시계는 **프로필 조회 전**에 시작한다 — 프로필 스토어(pg-profile)는 자체 타임아웃
    # (state_store_query_timeout_s 3s·connect 5s)이 홈 예산보다 커서, 밖에 두면 예산이 의미를
    # 갖기 전에 §3.7 "응답 3s" 를 이미 넘길 수 있다(PR 리뷰).
    deadline = started + settings.home_reco_budget_s

    def _remaining() -> float:
        return deadline - time.perf_counter()

    # 프로필 저장소 장애·지연은 **degrade** 한다 — 프로필은 랭킹의 한 항(장기 취향)이자 reason
    # 근거지만 유일한 입력이 아니라, 없으면 시그널만으로 추천을 만든다(§4.11 홈 렌더 비차단).
    # 호출별 상한(store_timeout)을 함께 걸어 **프로필이 예산을 독식해 랭킹까지 굶기는 것**을
    # 막는다 — 기본값(상한 2.0 < 예산 2.5)에서 랭킹 몫 0.5s 가 남는다. async 경로라 wait_for
    # 취소가 실제로 듣는다(_call_store 가 필요한 건 동기 스레드 호출뿐).
    # 신원은 서비스 토큰이 인가하고 memberId 는 §3.7 계약상 본문으로 온다(레인 b).
    # [#359] 중지 플래그를 요약과 **병렬**로 읽는다 — 직렬로 붙이면 왕복이 하나 늘어 예산
    # (store 2.0s < 전체 2.5s)을 잠식한다. 같은 pg-profile 이라 병렬이면 벽시계는 `max` 다.
    personalization_on: bool | None = None
    try:
        # [#469] 단계 span — 트레이스 미바인딩(Noop)이면 no-op 이라 예산에 영향이 없다.
        with trace_span("home.profile", "chain"):
            profile, personalization_on = await asyncio.wait_for(
                asyncio.gather(
                    read_profile_summary(str(request.member_id)),
                    # **3상태를 그대로 쓴다**: `None`(판정 불가)이면 아래에서 프로필 항만 빼고
                    # 랭킹을 계속한다 — 플래그도 pg-profile 에 있어 그 실패는 api-spec §3.7
                    # 「HOME 실패 모드」의 `profile_unavailable`(200·프로필 항만 빠짐·남은 근거로
                    # 판정)에 해당한다. `False` 로 접어 `NO_PROFILE` 을 강제하면 그 표와 충돌한다.
                    # 소비 fail-closed 는 여기서 "프로필을 쓰지 않는다"로 실현되며, 시그널은
                    # Spring 이 준 별개 근거라 그것까지 버릴 근거는 중지가 **확인됐을 때**뿐이다.
                    personalization_enabled(request.member_id, on_error=None),
                ),
                timeout=min(settings.home_reco_store_timeout_s, max(_remaining(), 0.001)),
            )
    except TimeoutError:
        logger.warning("home_reco_profile_timeout")
        profile = None
    except Exception:
        # 코드 버그가 degrade 에 묻히지 않게 traceback 을 로컬 로그에 남긴다(카탈로그·reason
        # 경로와 동일 관례). #141 규약이 막는 건 와이어·외부 trace 노출이지 로컬 로그가 아니다.
        logger.exception("home_reco_profile_unavailable")
        profile = None

    # [#359] **중지가 확인되면 시그널이 있어도 `NO_PROFILE`** (api-spec §3.7 v0.32.7·§3.9.5).
    # 중지는 "프로필을 못 읽는다"가 아니라 "개인화하지 않는다"는 사용자 의사이므로, 아래
    # `if not query_vec` 판정 기준의 **결과가 아니라 그보다 앞서는 단락**이다 — 프로필 벡터 항만
    # 빼면 `recentlyViewed`·`cart` 시그널만으로 질의 벡터가 만들어져 `PERSONALIZED` 가 나간다.
    # degrade 표식은 붙이지 않는다(REQ-PGRAPH-054 — 중지는 장애가 아니라 정상 동작이고, 관측
    # 계층에서 중지 여부가 새면 안 된다).
    if personalization_on is False:
        return _respond(
            "NO_PROFILE",
            [],
            {},
            started=started,
            candidates=0,
            settings=settings,
            request_id=request_id,
            member_id=request.member_id,
        )
    if personalization_on is None:
        profile = None  # 판정 불가 — 프로필 항만 빼고 랭킹은 계속한다
    # 미리 만들어 둔 취향 벡터(§3.7 "프로필 벡터와 가중 혼합"). 구 요약·임베딩 실패분은 None.
    # markdown 원문은 여기서 쓰지 않는다 — reason 매칭 분기는 극성(선호/회피) 문제로 제거했다
    # (build_reasons docstring).
    profile_vec = (profile or {}).get("embedding") or None

    # 카탈로그 인덱스는 **랭킹의 유일한 입력**이라 degrade 할 수 없다 — 여기가 죽으면 503 이고,
    # Spring 이 P-4 로 대체하며 fallbackReason=AI_ERROR 로 기록한다(§3.7 실패 응답표·§4.11).
    # 스토어 호출은 전부 `asyncio.to_thread` 로 넘긴다 — `PgCatalogArtifactStore` 는 psycopg
    # **동기** 드라이버(ConnectionPool)라 async 함수 안에서 직접 부르면 쿼리가 끝날 때까지
    # 이벤트 루프가 통째로 막히고, 같은 워커의 채팅 SSE 스트림까지 함께 지연된다. 같은 스토어를
    # 쓰는 `search_service`·`category_mapping` 이 PR #42 리뷰 이후 지키는 컨벤션이다.
    # 유닛 테스트는 인메모리 스토어를 주입해 이 블로킹을 재현하지 못하므로 규약으로 지킨다.
    # 스토어 호출마다 벽시계 상한을 건다(_call_store) — pg 지연·락이 요청을 I-22 예산 밖까지 붙들면
    # 계약의 504(UPSTREAM_TIMEOUT)로 끝나야 한다(§3.7 실패 응답표). to_thread 취소는 밑의 쿼리
    # 스레드를 죽이지 못하므로 DB 쪽도 statement_timeout 으로 이중 방어한다(pg_artifact_store).
    #
    # 상한은 **호출별이 아니라 잔여 예산과의 min** 이다 — 호출이 3번이라 호출별 고정 상한만으로는
    # 최악 2s×3=6s 로 "응답 3s"(§3.7)를 넘는다(PR 리뷰). 예산이 바닥나면 랭킹 경로는 504 다.
    #
    # 예외 처분: TimeoutError=504, 그 외 Exception=503. 503 으로 뭉치면 코드 버그(TypeError 등)가
    # "일시 장애"로 둔갑하므로 **traceback 을 로컬 로그에 남긴다**(logger.exception —
    # errors.py 의 500 처리와 같은 관례). 와이어·trace 로는 코드만 나간다(§3.7 [HARD] 불변).
    try:
        store = get_catalog_store()
        with trace_span("home.query_vector", "tool"):
            query_vec = await _call_store(
                build_query_vector,
                timeout=min(settings.home_reco_store_timeout_s, max(_remaining(), 0.001)),
                cart_ids=signals.cart_product_ids,
                viewed_ids=signals.recently_viewed_product_ids,
                store=store,
                settings=settings,
                profile_vec=profile_vec,
            )
    except TimeoutError as exc:
        logger.warning("home_reco_catalog_timeout")
        raise UpstreamTimeout from exc
    except Exception as exc:
        logger.exception("home_reco_catalog_unavailable")
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
            request_id=request_id,
            member_id=request.member_id,
        )

    exclude = set(signals.recent_purchased_product_ids)  # 가중치가 아니라 제외 필터(§3.7)
    # 필요한 만큼만 가져온다 — overfetch 목표와 부족 판정선 중 큰 쪽. 전량을 끌어오면 예산을 넘긴다.
    want = _overfetch_size(request.limit, settings)
    if _remaining() <= 0:
        logger.warning("home_reco_catalog_timeout")
        raise UpstreamTimeout
    try:
        with trace_span("home.rank", "tool"):
            ranked = await _call_store(
                rank_candidates,
                timeout=min(settings.home_reco_store_timeout_s, max(_remaining(), 0.001)),
                query_vec=query_vec,
                store=store,
                exclude=exclude,
                settings=settings,
                k=max(want, settings.home_reco_min_candidates),
            )
    except TimeoutError as exc:
        logger.warning("home_reco_catalog_timeout")
        raise UpstreamTimeout from exc
    except Exception as exc:
        logger.exception("home_reco_catalog_unavailable")
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
            request_id=request_id,
            member_id=request.member_id,
        )

    top = ranked[:want]

    # reason 은 미리 만들어 둔 `extras` 재료에서 **고른다** — LLM 호출 0회라 상위 N개로 제한할
    # 이유가 없다(전 카드 대상).
    # `build_reasons` 는 내부에서 `store.get_many` 로 카탈로그를 한 번 더 조회하므로 여전히 I/O 이고,
    # 실패를 그냥 두면 처리되지 않은 500 이 나가 "outcome 3종 모두 200" 계약(§3.7)이 깨진다.
    # 다만 **503 으로 올리지는 않는다** — 이 시점엔 `top`(정상 개인화 순위)이 이미 확정돼 있어,
    # reason 조회 순단으로 그것까지 버리면 Spring 이 멀쩡한 결과 대신 인기상품 폴백(AI_ERROR)을
    # 쓰게 된다. `reason` 은 계약상 nullable 이고 위 프로필 조회도 같은 이유로 degrade 하므로,
    # 여기서도 reason 만 비우고 개인화 결과는 그대로 내보내는 쪽이 "홈 렌더 비차단"(§4.11)에 맞다.
    if (reason_budget := _remaining()) <= 0:
        # 랭킹이 예산을 다 썼다 — reason 은 부가 정보라 skip 이 맞고, 확정된 랭킹은 그대로 나간다.
        logger.warning("home_reco_reason_skipped_budget")
        reasons = {}
    else:
        try:
            with trace_span("home.reasons", "tool"):
                reasons = await _call_store(
                    build_reasons,
                    timeout=min(settings.home_reco_store_timeout_s, reason_budget),
                    product_ids=top,
                    store=store,
                    cart_ids=signals.cart_product_ids,
                    viewed_ids=signals.recently_viewed_product_ids,
                    settings=settings,
                )
        except TimeoutError:
            logger.warning("home_reco_reason_timeout")
            reasons = {}
        except Exception:
            # 코드 버그가 degrade 에 묻히지 않게 traceback 을 로컬 로그에 남긴다(랭킹 경로와 동일).
            logger.exception("home_reco_reason_unavailable")
            reasons = {}
    return _respond(
        "PERSONALIZED",
        top,
        reasons,
        started=started,
        candidates=len(ranked),
        settings=settings,
        request_id=request_id,
        member_id=request.member_id,
    )


def _respond(
    outcome: str,
    product_ids: list[int],
    reasons: dict[int, str],
    *,
    started: float,
    candidates: int,
    settings: Settings,
    request_id: str,
    member_id: int,
) -> HomeRecommendationResponse:
    """응답 조립 + 관측 로그 1건 + [이슈 #140] provenance 로그 1건.

    로그 key set 은 고정이며 **memberId·productId·프로필 원문·모델 식별자·토큰을 남기지 않는다**
    (§3.7 [HARD]·§6.3). 남기는 것은 무슨 일이 있었는지 판별할 유계 코드와 개수뿐이다.

    provenance 로그의 `ownerFp` 는 `memberId` 를 `safe_fingerprint` 로 지문화한 값이지 원값이
    아니다 — 위 [HARD] 규약과 같은 이유. 홈은 요청 경로에서 LLM 을 부르지 않으므로
    `rankerModel`/`promptVersion` 은 항상 `null` 이다(§3.7 [HARD], `test_reason_never_calls_an_llm`).
    """
    # [#469] observability 관례대로 JSON 을 메시지에 직접 싣는다 — `extra` 는 기본 포맷터가
    # 출력하지 않아 outcome·개수가 실 로그에서 증발했다(운영 실측 2026-08-08).
    logger.info(
        json.dumps(
            {
                "event": "home_reco_request",
                "outcome": outcome,
                "candidateCount": candidates,
                "returnedCount": len(product_ids),
                "reasonSource": "extras" if reasons else "none",
                "elapsedMs": int((time.perf_counter() - started) * 1000),
            },
            ensure_ascii=False,
        )
    )
    response = HomeRecommendationResponse(
        outcome=outcome,  # type: ignore[arg-type]
        # 재시도 = 새 추천 실행. 멱등이 아니라 호출마다 새로 발급한다(§3.7).
        recommendation_request_id=str(uuid.uuid4()),
        # ≥128bit 무작위 — P-5 를 거쳐 공개되는 귀속 키라 추측 가능한 형식 금지(I-21 §4.2 동일 규칙).
        list_id=uuid.uuid4().hex,
        items=[
            HomeRecommendationItem(product_id=pid, reason=reasons.get(pid)) for pid in product_ids
        ],
    )
    emit_recommendation_provenance(
        logger,
        settings=settings,
        request_id=request_id,
        recommendation_request_id=response.recommendation_request_id,
        surface="home",
        pipeline="home_vector",
        prompt_version=None,
        ranker_model=None,  # §3.7 [HARD] — 홈 표면엔 모델 식별자를 절대 싣지 않는다.
        personalized=outcome == "PERSONALIZED",
        deterministic=True,
        list_type="PICK_ONE",
        owner_fp=safe_fingerprint(str(member_id)),
        session_fp=None,
        lists=[
            ProvenanceList(
                list_id=response.list_id,
                label=None,
                items=[
                    ProvenanceItem(
                        product_id=item.product_id,
                        rank_source="profile_vector",
                        has_reason=item.reason is not None,
                    )
                    for item in response.items
                ],
            )
        ],
    )
    return response
