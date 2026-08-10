"""추천 실행 provenance 로그 `recommend_provenance` (이슈 #140, api-spec §6.3 (d)).

추천 실행 1회당 정확히 1건, **추천이 실제로 사용자에게 도달했을 때만**(push 성공 / 홈 응답
반환) 낸다. 이 한 줄에서 추천 목록의 순서와 algorithmVersion 을 복원할 수 있어야 하고,
`recommendationRequestId`·`listId` 로 BE 행동 이벤트(`behavior_events`)와 결정적으로
join 되어야 하며, `requestId` 로 `chat_request` 로그·LangSmith trace 와 이어져야 한다.

`graph.py` 는 이미 2600행이 넘고 홈 경로(`home_recommendation.py`)와도 공유해야 하므로
별도 모듈로 뺐다.

**[HARD] 계약 불변** — 이 모듈은 로그 전용이다. 어떤 필드도 응답(와이어)에 실리지 않는다.
홈 표면(`surface="home"`)에는 모델 식별자(`rankerModel`)를 절대 싣지 않는다(api-spec §3.7
구현 노트 [HARD] — 알고리즘·모델 버전은 AI가 자체 테이블/로그에 보관하며 와이어에 싣지
않는다).

**왜 `deterministic` 이지 `propensity` 가 아닌가** — 이슈 코멘트가 off-policy 평가(IPS/SNIPS)용
propensity(로깅 정책이 그 항목을 선택했을 확률)를 요구한다. 현재 랭킹 정책(rerank 순위 결정
+ 검색순서 폴백 + 고정 규칙)은 같은 snapshot·config 입력에서 **완전히 결정론적**이라 실제
선택 확률은 1.0 이다. 여기에 `"propensity": 1.0` 이라는 **상수 float** 을 실으면 "탐색이
있는 로깅 정책"이라는 거짓 신호가 된다 — 소비자가 그 값으로 IPS 를 돌려도 가중치가 전부
1이라 그냥 naive 평균과 같아지는데, 그 사실이 로그 스키마만 봐서는 드러나지 않는다(상수를
실은 소비자가 "탐색이 있었는데 우연히 전부 1이었다"와 "애초에 탐색이 없었다"를 구분할 수
없다). 대신 `"deterministic": true` 를 싣는다 — 이 값은 (a) 선택 확률 1.0 을 모호함 없이
도출시키고(정책이 결정론이면 실제로 선택된 항목의 확률은 항상 1) (b) **정책에 탐색이 없어
IPS 가 산술 평균으로 퇴화한다는 진짜 메시지**를 명시적으로 전달한다. 나중에 랭킹에 실제
랜덤화(A/B 탐색 등)가 도입되면, 그때 `propensity`(와 정책 식별자 `policyId`)를 실제 계산값과
함께 추가하는 것이 정직한 확장 경로다 — 지금 상수를 미리 심어두는 것보다 안전하다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.core.logging import log_structured
from app.core.tracing import current_request_trace

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

# 닫힌 어휘 — 이슈의 "score component"(수치 score 없음)를 번역한 것: 그 자리를 무엇이
# 결정했는지를 남긴다.
RankSource = Literal[
    "rerank",  # Sonnet rerank 가 고른 순위
    "search_order",  # rerank 실패(degrade)라 검색 순서를 그대로 씀
    "repurchase_pin",  # #120 명시 재구매 지목으로 앞에 고정된 상품
    "expose_min_fill",  # _split_by_need 가 expose_min 미달을 검색순서 재고에서 보충한 상품
    "profile_vector",  # 채팅 프로필 벡터 경로 / 홈 벡터 랭킹
]
Surface = Literal["chat", "home"]
# api-spec §6.3 (d) `algorithmVersion` 표가 이미 닫아 둔 어휘 — 문서가 닫은 걸 코드가 열어
# 두면 오타·임의 값이 조용히 `algorithmVersion` 에 실려 드리프트한다.
Pipeline = Literal["search_rerank", "profile_vector", "home_vector"]


@dataclass(frozen=True, slots=True)
class ProvenanceItem:
    """provenance 목록 안 상품 1건 — 순서는 호출부가 보장한다(배열 순서 = position)."""

    product_id: int
    rank_source: RankSource
    has_reason: bool


@dataclass(frozen=True, slots=True)
class ProvenanceList:
    """provenance 목록 1건 — push 된 `listId`/`label` 을 그대로 담는다."""

    list_id: str
    label: str | None
    items: Sequence[ProvenanceItem]


def _truncate_lists(
    lists: Sequence[ProvenanceList], max_items: int
) -> tuple[list[ProvenanceList], bool]:
    """`reco_provenance_max_items` 방어 상한 — 초과분은 조용히 버리지 않고 표시한다.

    목록 순서대로 예산을 소진한다. 목록 중간에 예산이 바닥나면 그 목록은 앞부분만 남기고,
    이후 목록은(항목이 있었다면) 통째로 빠진다 — 두 경우 모두 `itemsTruncated=true` 로 남는다.
    """
    truncated = False
    remaining = max_items
    kept: list[ProvenanceList] = []
    for lst in lists:
        if remaining <= 0:
            if lst.items:
                truncated = True
            continue
        items = list(lst.items)
        if len(items) > remaining:
            items = items[:remaining]
            truncated = True
        remaining -= len(items)
        kept.append(ProvenanceList(list_id=lst.list_id, label=lst.label, items=items))
    return kept, truncated


def _build_record(
    *,
    settings: "Settings",
    request_id: str,
    recommendation_request_id: str,
    surface: Surface,
    pipeline: Pipeline,
    prompt_version: str | None,
    ranker_model: str | None,
    personalized: bool,
    deterministic: bool,
    list_type: str,
    owner_fp: str | None,
    session_fp: str | None,
    lists: Sequence[ProvenanceList],
) -> dict[str, object]:
    trace = current_request_trace()
    degrade_reason = trace.degrade_reason if trace is not None else None
    kept_lists, items_truncated = _truncate_lists(lists, settings.reco_provenance_max_items)
    return {
        "requestId": request_id,
        "recommendationRequestId": recommendation_request_id,
        "surface": surface,
        "algorithmVersion": f"{pipeline}@{settings.reco_algorithm_version}",
        "promptVersion": prompt_version,
        "rankerModel": ranker_model,
        "personalized": personalized,
        "deterministic": deterministic,
        "degraded": degrade_reason is not None,
        "degradeReason": degrade_reason,
        "listType": list_type,
        "ownerFp": owner_fp,
        "sessionFp": session_fp,
        "itemsTruncated": items_truncated,
        "lists": [
            {
                "listId": lst.list_id,
                "label": lst.label,
                "items": [
                    {
                        "productId": item.product_id,
                        "position": position,
                        "rankSource": item.rank_source,
                        "hasReason": item.has_reason,
                    }
                    for position, item in enumerate(lst.items)
                ],
            }
            for lst in kept_lists
        ],
    }


def emit_recommendation_provenance(
    provenance_logger: logging.Logger,
    *,
    settings: "Settings",
    request_id: str,
    recommendation_request_id: str,
    surface: Surface,
    pipeline: Pipeline,
    prompt_version: str | None,
    ranker_model: str | None,
    personalized: bool,
    deterministic: bool,
    list_type: str,
    owner_fp: str | None,
    session_fp: str | None,
    lists: Sequence[ProvenanceList],
) -> None:
    """`recommend_provenance` 구조화 로그 1건을 낸다.

    **실패 격리** — 관측이 스트림·응답을 죽이면 안 된다(`observability.finish_trace_safely`
    와 같은 원칙). 조립·로깅 중 예외는 전부 삼키고 경고만 남긴다. `asyncio.CancelledError`
    는 `BaseException` 이라 이 `except Exception` 을 자연히 빠져나가 전파된다 — 잡지 않는다.
    """
    try:
        record = _build_record(
            settings=settings,
            request_id=request_id,
            recommendation_request_id=recommendation_request_id,
            surface=surface,
            pipeline=pipeline,
            prompt_version=prompt_version,
            ranker_model=ranker_model,
            personalized=personalized,
            deterministic=deterministic,
            list_type=list_type,
            owner_fp=owner_fp,
            session_fp=session_fp,
            lists=lists,
        )
        log_structured(provenance_logger, "recommend_provenance", **record)
    except Exception:  # noqa: BLE001 - 관측 실패가 추천 스트림·응답을 죽이면 안 된다
        logger.warning("recommend_provenance emit 실패 code=RECO_PROVENANCE_EMIT_FAILED")
