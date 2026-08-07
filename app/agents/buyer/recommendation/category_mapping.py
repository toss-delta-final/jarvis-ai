"""카테고리 매핑 오케스트레이션 (이슈 #59, 방식 A — LLM 추측 → 임베딩 보정).

decompose 가 추측한 카테고리(raw)들을 실제 DB 카테고리(canonical)로 보정한다. LLM 재호출
없이 exact match → 임베딩 최근접(raw·query 둘 다 조회해 **query 우선**, §4.3 #115)으로 처리한다.
카테고리 신호가 있는 leg 는 성공 시 canonical 을 내고, 신호 없는 leg(raw·query 모두 없음)는
카테고리를 강제하지 않는다 → **canonical-or-null**(Spring 엔 canonical 또는 null 만, 미검증 raw 는
안 나간다, #22·#20).
embed·search·exact 는 주입형 seam 이라 유닛테스트가 pg·API 없이 돈다.

블로킹 호출(embed_texts·pgvector 조회·exact SELECT)은 asyncio.to_thread 로 감싸 이벤트 루프를
막지 않는다(search_service 와 동일 패턴).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.agents.buyer.recommendation.category_select import select_category as _select_category
from app.agents.buyer.recommendation.state import CategoryQuery
from app.pipelines.category_search import exact_lookup as _exact_lookup
from app.pipelines.category_search import search_categories_pg as _search_top_k
from app.pipelines.embedding import embed_texts as _embed_texts

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], list[list[float]]]
SearchFn = Callable[..., list[tuple[str, float]]]  # (category, distance) 오름차순 (#115)
ExactFn = Callable[[Sequence[str], str], set[str]]
SelectFn = Callable[..., Awaitable[str | None]]  # top-k 택일(§4.4 #115) — 후보 밖이면 None

# #428 대분류 합의 필터의 최소 지지 leg 수 — 튜너블이 아니라 **합의의 정의상 최소값**이다(1개
# leg 만의 지목은 "형제간 합의"가 아니라 그 leg 단독의 의견이라 노이즈와 구분할 신호가 없다).
# `CLAUDE.md` "튜너블 하드코딩 금지"는 실측으로 캘리브레이션하는 값(거리컷 등)에 대한 규율이고,
# 이 값은 실측과 무관한 개념 정의라 config 로 뺄 대상이 아니다.
_CONSENSUS_MIN_SUPPORT_LEGS = 2

# 채택 후보 = (canonical, 채택 거리, 마진) — 마진은 히트 1건이면 None(§11 #115)
_Picked = tuple[str, float, float | None]
# leg 최종 승자 = 후보 + 이긴 앵커 종류("raw"|"query", §4.3 #115)
_Winner = tuple[str, float, float | None, str]


@dataclass
class CategoryMapping:
    """매핑 결과 — leg 목록 + **canonical 을 못 낸 leg**(이슈 #217, DESIGN-198 §6.2).

    `unresolved` 는 전개 트리거의 입력이다. 종전 반환형(`legs` 리스트 단독)으로는 호출부가
    "드롭됐다"와 "애초에 신호가 없었다"를 구분할 수 없어, 목적 marker 열거로 미리 맞히는 수밖에
    없었다(#198 초판 D2·D3). 결과를 보고 판정하면 열거가 필요 없다.

    **무엇을 담고 무엇을 담지 않는지가 이 필드의 전부다**(§4):

    - 담는다 — ① 거리컷 드롭(`category_distance_rejected`) · ② 택일 null(`category_select_null`).
      둘 다 "이 표현에 맞는 칸이 taxonomy 에 없다"는 같은 뜻이다.
    - 담지 않는다 — ③ 조회 예외(pg 경합·embed 실패) · ④ 히트 0건(`categories` 미시드).
      **실패 원인이 발화 내용이 아니라서** 내용 기반 처방(LLM 전개)을 붙일 근거가 없다. 섞으면
      pg 순간 장애가 전개 호출을 부르고, 전개해봐야 같은 인프라·같은 빈 사전을 다시 두드린다.
      PR #188 이 `error_type` 으로 "인프라 장애 vs 코드 버그"를 가른 것과 같은 원칙이다.
    - 신호 없는 leg(raw·query 모두 없음)도 담지 않는다 — 매핑을 시도조차 하지 않았고, 그 상황은
      호출부가 D1(`no_legs`)로 따로 판정한다. 여기서 새면 사유가 이중 기록돼 관측이 오염된다.

    값은 **이긴 앵커 텍스트**(query 우선, §4.3.1)다. 전개 자체는 발화 원문으로 하므로 이 값이
    트리거 판정을 바꾸지는 않지만, 어떤 앵커가 왜 실패했는지가 로그에 남아야 하류
    `category_distance_rejected` 의 거리·마진과 조인해 임계를 재튜닝할 수 있다(§10).
    """

    legs: list[tuple[str, str | None]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    # 이 호출이 **실제로 쓴** §4.4 택일 LLM 호출 수(#217 PR 리뷰). `category_select_max_calls` 는
    # **턴당** 상한인데 #217 로 매핑이 턴에 2회 불리므로, 호출부가 남은 예산을 계산해 두 번째 호출에
    # 넘겨야 상한이 지켜진다(§6.1). 실패한 호출도 비용이 발생하므로 **시도 수**를 센다 —
    # `observer.record_model_call` 도 같은 이유로 호출 전에 기록한다.
    select_calls: int = 0
    # 매핑이 canonical 을 못 낸 leg 의 **의미 기반 top-N leaf** — 광역 발화 fan-out 후보(#222).
    # `unresolved` 와 같은 leg 에서 나오지만 용도가 다르다: unresolved 는 "왜 실패했나"(전개 트리거),
    # 이 값은 "그래도 어디를 볼 것인가"(검색 leg). 조회 예외·히트 0건 leg 은 담지 않는다 —
    # 후보 자체가 없어서 담을 것이 없다.
    expansion_leaves: list[tuple[str, str | None]] = field(default_factory=list)


def _top1_with_margin(hits: list[tuple[str, float]]) -> _Picked | None:
    """거리 오름차순 top-k 에서 `(canonical, distance, margin)` 을 뽑는다 (히트 0건이면 None).

    마진(2위−1위 거리차)은 "1등이 2등을 얼마나 확실히 이겼나" = 판정 확신도다. 히트가 1건이면
    계산 불가라 **0.0 이 아니라 None** — 0.0 은 "동점"(가장 애매한 상태)으로 오독된다.
    소수 4자리로 반올림해 로그 가독성을 맞춘다(임계 비교는 원값 기준이 아니라 이 값 기준).
    """
    if not hits:
        return None
    canonical, distance = hits[0]
    margin = round(hits[1][1] - distance, 4) if len(hits) > 1 else None
    return canonical, round(distance, 4), margin


def dedup_truncate(
    legs: list[tuple[str, str | None]], fanout_max: int
) -> list[tuple[str, str | None]]:
    """canonical 기준 순서보존 dedup(첫 query 유지) 후 fanout_max 로 절단.

    공개 함수인 이유(#217): 합집합 배선(§6)에서 **호출부가** 원 매핑 legs 와 전개 매핑 legs 를
    이어붙인 뒤 같은 규칙으로 정리해야 한다. 순서보존이 계약의 일부다 — 원 leg 을 앞에 두면
    `fanout_max` 절단에서 사용자가 명시한 카테고리가 먼저 살아남는다.
    """
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for cat, query in legs:
        if cat not in seen:
            seen.add(cat)
            out.append((cat, query))
    return out[:fanout_max]


def _interleave_by_leg(
    by_leg: dict[int, list[tuple[str, str | None]]],
) -> list[tuple[str, str | None]]:
    """[#222 PR #318 리뷰 R5-1] leg 마다 모은 후보를 leg 순서대로 한 개씩 번갈아 뽑아 평탄화한다.

    unresolved leg 이 여럿인데 `expansion_leaves` 를 leg 순서대로 이어 붙이기만 하면, 그 뒤
    `dedup_truncate(..., category_expand_legs)` 가 **턴 전체 상한**을 앞쪽부터 잘라 먼저 처리된
    leg 이 예산을 통째로 가져가고 뒤 leg 은 0개가 된다("캠핑용품이랑 낚시용품" 이 둘 다 매핑
    실패하면 캠핑 8개만 검색되고 낚시는 조용히 사라지는 식). `recommendation/graph.py` 의
    `_merge_fanout_results` 가 "한 카테고리가 rerank 입력을 독점하지 않게" 쓰는 것과 같은
    round-robin 규약을 여기서도 쓴다 — leg 별 상한이 아니라 **인터리브 뒤 한 번만** 전체
    상한을 적용해야 leg 간 형평이 산다. 후보가 적은 leg 은 그 라운드에서 건너뛰고 후보가
    남은 leg 이 계속 순서를 채운다(예산 누수 없음).
    """
    ordered = sorted(by_leg)  # leg 인덱스 순서 보존
    lists = [by_leg[i] for i in ordered]
    depth = max((len(pl) for pl in lists), default=0)
    out: list[tuple[str, str | None]] = []
    for d in range(depth):
        for pl in lists:
            if d < len(pl):
                out.append(pl[d])
    return out


def _consensus_filter(
    expansion_by_leg: dict[int, list[tuple[str, str | None]]],
) -> tuple[dict[int, list[tuple[str, str | None]]], dict[str, object]]:
    """[#428] 전개 형제(sibling) leg 사이 **최근접(top-1) 대분류 합의**로 동음이의어 노이즈를
    걸러낸다.

    전개 아이템(#217)은 하나의 니즈에서 나온 형제들이라 서로의 카테고리를 검증할 수 있다 —
    **두 형제 이상의 최근접 대분류가 서로 일치하면** 그 대분류(동률이면 전부)가 사용자가 말한
    상품군이고, 후보 전체를 거기로 좁힌다. 최근접이 갈리면(= 최다 지지가 1개 leg 뿐) 형제간
    합의가 없다는 뜻이라 아무것도 하지 않는다.

    [#428 리뷰 1차 F-1] 지지 집계는 **각 leg 의 top-1 하나만** 본다 — leg 의 top-k 전체(최대
    `category_expand_legs`개)에서 셌던 초판은 "어느 발화에나 조금씩 가까운 잡동사니 대분류"가
    꼬리 순위에서 여러 leg 에 걸쳐 우연히 겹쳐 승자가 되는 결함이 있었다. 실측: "집들이 선물"
    전개(디퓨저·캔들·와인잔·식기 세트)에서 향수·조명·주방잡화라는 정답급 후보를 전부 버리고
    `주얼리`(전 leg 의 5~7위에 조금씩 걸침)만 남겼다. 형제가 서로를 검증한다는 신호는 **각자가
    자기 최선의 답으로 같은 곳을 가리킬 때** 성립하지, 꼬리에서 우연히 겹칠 때 성립하지 않는다
    — 이건 "몇 개까지 셀까"라는 튜너블이 아니라 합의라는 개념 자체의 정의다(집계 창을 2~5 로
    넓혀가며 실측했고 4 이상부터 잡동사니 대분류가 다시 승자가 된다. 1~3 은 실측 시나리오
    전부에서 결과가 동일해 임의 상수가 필요 없는 top-1 을 정본으로 한다). **필터링(무엇을
    남길지)은 여전히 leg 의 후보 전체를 대상으로 한다** — 좁아지는 건 "누가 승자 대분류인가"를
    세는 창일 뿐, 승자 대분류에 속한 leaf 는 순위와 무관하게 전부 살아남는다.

    [#428 리뷰 3차 R3-1] **필터는 leg 을 없애지 않고 후보만 좁힌다.** 승자 대분류(들)로 각 leg 을
    걸러 본 결과 **후보가 0개가 되는 형제가 하나라도 있으면 필터를 전혀 적용하지 않고**(부분 적용
    금지) 원본을 그대로 반환한다 — 그 형제의 후보 목록 어디에도 승자 대분류가 없다는 것은 그
    형제가 정말로 다른 상품군이라는 뜻이지, 합의에서 벗어난 노이즈가 아니기 때문이다. 반대로
    `#428` 과일 케이스에서 동음이의어 `"배"`는 최근접이 `여성가방 > 백팩`이어도 6위에
    `과일 > 국산과일`을 갖고 있다 — 같은 상품군에 속한다는 증거가 후보 안에 남아 있어 가드를
    통과한다. (리뷰어가 제안한 "형제 수 대비 과반" 임계는 `#428` 본체를 깬다 — 과일 A 회차는
    지지 2/4로 과반이 아니다.) 초판 서술("형제 사이 형평을 깨고 소수 의견을 탈락시킨다",
    `_interleave_by_leg` R5-1 형평 규약과 별개)은 이 가드로 더는 사실이 아니다 — leg 자체가
    탈락하는 경로가 구조적으로 없어졌다.

    합의가 성립하지 않으면(기여 leg 2개 미만, 또는 최다 지지가 1개 leg 뿐) 아무것도 하지 않고
    원본을 그대로 반환한다 — "이사 갈 때 필요한 것들" → 행거·커튼·이불처럼 형제들이 애초에
    이질적인 전개는 대분류가 갈리는 것이 정상이라 합의 신호 자체가 없다.

    [#428 리뷰 6차 R6-1] **미적용(None) 반환은 없다** — 적용이든 미적용이든 항상 stats dict 를
    낸다. 미적용 사유는 `category_select_unavailable` 과 같은 규약으로 `reason` 필드 하나로
    구분한다(`single_leg`·`no_consensus`·`leg_without_winning_mid`) — 호출부가 이 `reason` 을
    그대로 로그에 싣는다.
    """
    if len(expansion_by_leg) < _CONSENSUS_MIN_SUPPORT_LEGS:
        return expansion_by_leg, {
            "skipped": True,
            "reason": "single_leg",
            "legs": len(expansion_by_leg),
        }

    support: dict[str, set[int]] = {}
    for leg_i, leaves in expansion_by_leg.items():
        # [#428 리뷰 2차 R2-2] 빈 leg 은 건너뛴다 — R2-1 이 유일한 유입 경로(`_collect_
        # expansion_leaves`)를 막았지만, 이 함수는 주입형 seam 을 가진 모듈의 private 헬퍼라
        # 미래의 호출부가 빈 리스트를 다시 넘길 수 있다. `leaves[0]`(top-1) 인덱싱은 빈 리스트에
        # IndexError 를 내므로(Claude PR Review, PR #444) 방어가 아니라 **의미상 옳은 처리**로
        # 건너뛴다 — 지지할 후보가 없는 leg 은 애초에 셀 것이 없다.
        if not leaves:
            continue
        # [F-1] top-1 만 — leg 의 최선의 답만 지지로 센다(위 docstring 근거).
        top1_mid = leaves[0][0].split(" > ", 1)[0]
        support.setdefault(top1_mid, set()).add(leg_i)

    max_support = max((len(legs) for legs in support.values()), default=0)
    if max_support < _CONSENSUS_MIN_SUPPORT_LEGS:
        return expansion_by_leg, {
            "skipped": True,
            "reason": "no_consensus",
            "legs": len(expansion_by_leg),
            "max_support": max_support,
        }

    winning_mids = sorted(mid for mid, legs in support.items() if len(legs) == max_support)
    winning = set(winning_mids)

    filtered: dict[int, list[tuple[str, str | None]]] = {}
    dropped_leaves = 0
    for leg_i, leaves in expansion_by_leg.items():
        # [#428 리뷰 2차 R2-2] 원래 비어 있던 leg 은 필터가 "0개로 걸러낸" 것이 아니므로 건너뛴다.
        if not leaves:
            continue
        kept = [
            (canonical, query)
            for canonical, query in leaves
            if canonical.split(" > ", 1)[0] in winning
        ]
        if not kept:
            # [#428 리뷰 3차 R3-1] 가드 — 이 leg 은 후보 목록 어디에도 승자 대분류가 없다(=
            # 정당하게 다른 상품군). 부분 적용 금지: 지금까지 좁힌 다른 leg 도 버리고 원본
            # expansion_by_leg 전체를 그대로 반환한다.
            return expansion_by_leg, {
                "skipped": True,
                "reason": "leg_without_winning_mid",
                "legs": len(expansion_by_leg),
                "max_support": max_support,
                "winning_mids": winning_mids,
            }
        dropped_leaves += len(leaves) - len(kept)
        filtered[leg_i] = kept
    stats = {
        "skipped": False,
        "legs": len(expansion_by_leg),
        "max_support": max_support,
        "winning_mids": winning_mids,
        "dropped_leaves": dropped_leaves,
    }
    return filtered, stats


async def map_categories(
    *,
    category_queries: Sequence[CategoryQuery],
    utterance: str,
    settings,
    embed: EmbedFn | None = None,
    search_top_k: SearchFn = _search_top_k,
    exact_lookup: ExactFn = _exact_lookup,
    llm=None,
    tier: str = "fast",
    select_category: SelectFn = _select_category,
    select_max_calls: int | None = None,
    observer=None,
    sibling_expansion: bool = False,
) -> CategoryMapping:
    """decompose 추측들을 canonical (category, query) leg 리스트로 보정한다.

    `sibling_expansion` — 입력 leg 들이 **하나의 니즈에서 전개된 형제 아이템**임을 호출부가
    보증한다(#217 전개 후 재매핑). 참이면 `expansion_leaves` 조립에 대분류 합의 필터
    (`_consensus_filter`, #428) — **두 형제 이상의 최근접(top-1) 대분류가 일치**하면 그
    대분류로 후보를 좁히는 필터 — 를 건다. 원 매핑(서로 다른 니즈)에는 적용하지 않는다 — 그쪽은
    대분류가 갈리는 것이 정상이다.

    각 leg 의 query 는 그 카테고리 전용 검색 키워드(fan-out leg keyword, §6·§9) — 매핑 전
    추측(raw)이 어디로 보정되든 원 추측의 query 를 그대로 이어 붙인다.
    per-leg 규칙(우선순위): (1) raw 가 exact match → raw (DB 검증값이라 거리 비교 대상 아님).
    (2) 그 외 신호가 있으면 **raw·query 를 각각 임베딩·조회해 query 쪽 최근접을 우선 채택**하고,
    raw 최근접은 query 히트 0건·조회 실패 시 폴백으로만 쓴다(§4.3·§4.3.1 #115 — 종전엔 raw 가
    있으면 query 를 버렸고, 거리 비교도 추상 라벨의 문자열 겹침 때문에 성립하지 않는다).
    (3) 채택 거리가 `category_distance_max` 를 **초과하면 그 leg 를 드롭**한다 — 최근접이 멀다는 건
    "맞는 칸이 taxonomy 에 없다"는 신호이고, 틀린 카테고리로 좁히면 정답 상품이 후보에서 제외되기
    때문이다(§4 #115, 종전 never-null 폐기). exact 매치는 거리 개념이 없어 컷 대상이 아니다.
    (4) raw·query 모두 없음(빈 리스트 포함) → 신호 없음으로 보고 leg 를 만들지
    않는다 → 무필터 검색(카테고리 강제 금지, PR #73 #22). (5) 실패는 **앵커 단위로 격리** —
    exact 매치는 임베딩 경로 실패와 무관하게 보존하고, 한 leg 의 앵커 하나가 실패해도 다른 앵커가
    canonical 을 냈으면 그 leg 를 살린다. 앵커 전부 실패·embed 전면 실패는 그 leg 만 드롭한다
    (canonical-or-null, #20·PR #73 리뷰). 모든 leg 가 드롭되면 빈 리스트를 낸다 — Spring 엔 canonical
    또는 null(생략)만 나간다.

    입력 leg 수는 `category_fanout_max` 로 **방어적으로 절단**한다(PR #188 리뷰) — leg 당 앵커가
    2개(raw·query)라 한 턴의 pg 커넥션 점유가 `2 × leg 수`이고, 그 상한을 config 검증기가 기동 시
    강제하기 때문이다. 호출부 절단에만 기대면 새 호출부 하나가 풀을 넘긴다.

    반환은 `CategoryMapping(legs, unresolved)` 다(#217, §6.2) — `unresolved` 는 (3)·§4.4 택일 null 로
    **canonical 을 못 낸 leg** 의 앵커 텍스트이고, 호출부(graph)가 이를 전개 트리거로 쓴다. 조회
    예외((5))·히트 0건은 담지 않는다: 실패 원인이 발화 내용이 아니라 인프라·시드 상태라서
    LLM 전개로 풀 수 없다(`CategoryMapping` docstring 참조).

    (utterance 는 매퍼 인터페이스 파라미터로 유지하되, 현재 앵커는 leg 별 raw·query 만 쓴다.)
    """
    dsn = settings.catalog_db_url
    # [#222] 조회 k 는 택일 후보(category_top_k)와 광역 fan-out 후보(category_expand_legs) 중 큰
    # 쪽으로 늘린다 — 같은 쿼리의 LIMIT 만 커질 뿐 pg 왕복 횟수는 그대로다. 늘어난 히트는
    # `candidates_by_leg`(§4.4 택일)·`_top1_with_margin`(top1/margin)에 **그대로 넘기지 않는다** —
    # 아래에서 앞 category_top_k 개만 슬라이스해 종전 #115 동작(택일 후보 5개)을 고정한다.
    # [PR #318 리뷰 R6-2] `category_expand_enabled` 를 여기서 직접 읽는다 — 소비 지점(`buyer/graph.py`)
    # 검사만으로는 이 플래그가 "롤백 스위치"로 동작하지 않는다. 꺼도 ① k 확대로 pgvector LIMIT 이
    # 그대로 늘어난 채 나가고 ② `_collect_expansion_leaves` 가 계속 돌아
    # `category_expansion_leaves` 로그가 계속 쌓인다 — 부하·로그 노이즈 때문에 롤백하는
    # 인시던트에서 롤백이 안 되는 롤백 스위치가 된다. 꺼져 있으면 k 를 category_top_k 로 고정해
    # 조회 폭 자체를 늘리지 않는다(소비 지점의 기존 검사는 이중 방어로 그대로 둔다).
    k = (
        max(settings.category_top_k, settings.category_expand_legs)
        if settings.category_expand_enabled
        else settings.category_top_k
    )
    fanout_max = settings.category_fanout_max
    distance_max = settings.category_distance_max  # 초과 시 leg 드롭(§4 #115)
    # 거리컷 마진 예외(§4.5 #115) — 튜너블은 **여기서 한 번에** 읽는다. 결과 루프 안에서 읽으면
    # 설정 하나가 어긋났을 때 이미 확정된 leg 까지 함께 날아간다(PR #188 리뷰).
    override_margin = settings.category_distance_override_margin
    # 미주입 기본값은 질의(query) 임베딩 — 앵커(raw 추측·leg query)는 질의 쪽이므로 비대칭 검색
    # 관례에 맞춰 RETRIEVAL_QUERY 로 바인딩한다(문서 쪽 category_seed=document, 이슈 #65·PR #73 리뷰).
    embed = embed or functools.partial(_embed_texts, task_type=settings.embedding_task_query)
    queries = list(category_queries)  # 빈 리스트를 강제로 채우지 않는다 — 신호 없으면 빈 결과(#22)
    # 입력 leg 수를 fanout_max 로 **방어적으로** 절단한다(PR #188 리뷰). 매핑은 leg 당 raw·query
    # 두 앵커를 gather 로 동시 조회하므로 한 턴의 pg 커넥션 점유가 `2 × leg 수`이고,
    # config._require_pool_covers_anchor_concurrency 는 `pool >= 2 × fanout_max` 를 기동 시
    # 강제한다 — 그 전제("leg 수 ≤ fanout_max")가 호출부 절단에만 의존하면 새 호출부 하나가
    # 풀을 넘기고, 증상은 **다른 사용자 요청의 PoolTimeout** 이라 원인 추적이 어렵다.
    # 출력은 어차피 dedup_truncate 로 같은 상한을 받으므로 초과분은 낭비였다(dedup 이 겹칠 때만
    # leg 다양성이 줄지만, 그건 호출부 계약 위반 상황이고 풀 고갈보다 가벼운 손해다).
    if len(queries) > fanout_max:
        logger.warning(
            "category_legs_truncated",
            extra={"given": len(queries), "fanout_max": fanout_max},
        )
        queries = queries[:fanout_max]
    raws = [q.raw_category for q in queries]
    qtexts = [q.query for q in queries]  # leg keyword 로 이어 붙일 원 추측 query

    # exact match(DB 직접 조회)는 그 자체로 canonical 검증 — 임베딩 경로(embed/search) 실패와
    # 독립적으로 보존한다. exact 조회 실패는 미검증 raw 를 canonical 처럼 내보내지 않으려 exact 를
    # 비우고 임베딩 경로로 넘긴다(canonical-or-null, PR #73 #20).
    non_null = [r for r in raws if r]
    try:
        exact = await asyncio.to_thread(exact_lookup, non_null, dsn) if non_null else set()
    except Exception as exc:  # noqa: BLE001 - exact 조회 실패: exact 없음으로 두고 임베딩 경로 시도
        logger.warning("category_exact_failed", extra={"reason": str(exc)})
        exact = set()

    # 보정(임베딩) 필요한 leg = 신호가 있는 것만: exact 아닌 raw, 또는 raw 없지만 query 있음.
    # raw·query 모두 없는 leg(및 빈 리스트)은 카테고리를 강제하지 않는다 — 발화 전체를 앵커로
    # 쓰지 않아 category-agnostic 질의("5만원 이하 아무거나")를 엉뚱한 카테고리로 안 좁힌다(#22).
    need_idx = [
        i
        for i in range(len(raws))
        if (raws[i] and raws[i] not in exact) or (not raws[i] and qtexts[i])
    ]
    nearest: dict[int, _Winner] = {}
    candidates_by_leg: dict[
        int, list[tuple[str, float]]
    ] = {}  # 택일 후보(§4.4) — category_top_k 로 슬라이스
    # [#222] 슬라이스 전 원본 히트(최대 k=category_expand_legs) — 광역 fan-out leaf 후보용.
    # candidates_by_leg 를 그대로 쓰면 택일 후보 수가 늘어 #115 가 튜닝한 동작이 조용히 바뀐다.
    full_hits_by_leg: dict[int, list[tuple[str, float]]] = {}
    anchor_by_leg: dict[int, str] = {}  # 이긴 앵커 텍스트(택일 질의 조립용, §4.4)
    failed_idx: set[int] = set()  # 조회가 예외로 실패한 leg — category_unmapped(품질) 오염 방지
    try:
        # 앵커: leg 마다 raw(LLM 추측)·query(발화 유래) **둘 다** 조회하되 **query 우선** 채택하고,
        # raw 최근접은 query 히트 0건·조회 실패 시의 폴백으로만 쓴다(§4.3·§4.3.1 #115). 종전
        # `raws[i] or qtexts[i]` 는 raw 가 있으면 query 를 조회조차 하지 않았고, 그게 정답을 능동적으로
        # 버리는 경로였다 — "층간소음 방지 용품"(query) 0.0850 vs '가전/생활용폼'(창작 raw, 오타)
        # 0.2399 채택. 거리 비교("가까운 쪽")로 고르지 않는 이유는 **추상 라벨의 가짜 근접** 때문이다:
        # '주방용품'→`주방용품 > 칼` 0.1387 은 의미가 아니라 카테고리명과의 문자열 겹침이고, 의미가
        # 맞는 '냄비 세트'(0.1941)보다 가깝게 나온다. 라이브 실측에서 raw 채택 12건 중 11건이 오분류.
        # null-raw leg 이 여럿이어도 각자 query 로 임베딩해 fan-out 폭을 지킨다(PR #73 #17).
        anchors: list[tuple[int, str, str]] = []  # (leg 인덱스, 앵커 종류, 텍스트)
        for i in need_idx:
            if qtexts[i]:
                anchors.append((i, "query", qtexts[i]))
            if raws[i]:
                anchors.append((i, "raw", raws[i]))
        vecs = await asyncio.to_thread(embed, [text for _, _, text in anchors]) if anchors else []
        # 앵커별 최근접 조회를 병렬 실행 — 카테고리 여러 개(상황형 질의)일수록 직렬 지연이
        # 앵커 수만큼 쌓이므로 gather 로 동시 실행한다(순서 보존 → anchors 매핑 유지, §6).
        # return_exceptions=True — 앵커 하나의 순간 실패(pg 경합·타임아웃)가 gather 전체를 던져
        # 정상 leg 까지 무필터로 날리지 않게 격리한다. recommendation/graph 의 leg 별
        # SpringUnavailable 격리(§6)와 일관 — 부분 성공 보존(PR #73 리뷰).
        hit_lists = await asyncio.gather(
            *(asyncio.to_thread(search_top_k, vecs[j], dsn, k=k) for j in range(len(anchors))),
            return_exceptions=True,
        )
        errored: set[int] = set()  # 앵커 하나 이상이 예외로 실패한 leg
        by_leg: dict[int, dict[str, _Picked]] = {}  # leg → {앵커 종류: 후보}
        for j, hits in enumerate(hit_lists):
            leg_i, kind, _text = anchors[j]
            if isinstance(hits, Exception):
                # 이 앵커 조회만 실패 — 사유를 남긴다. 같은 leg 의 다른 앵커가 성공하면 살린다(아래).
                logger.warning(
                    "category_leg_search_failed", extra={"reason": str(hits), "anchor_kind": kind}
                )
                errored.add(leg_i)
                continue
            picked = _top1_with_margin(hits)
            if picked is None:
                continue  # 히트 0건 — 이 앵커는 후보 없음(leg 판정은 아래 unmapped 로)
            by_leg.setdefault(leg_i, {})[kind] = (picked, list(hits), _text)
        # query 우선 — 거리 비교가 아니다(§4.3.1). raw 는 query 가 후보를 못 낸 leg 의 폴백.
        for leg_i, by_kind in by_leg.items():
            kind = "query" if "query" in by_kind else "raw"
            picked, hit_list, anchor_text = by_kind[kind]
            nearest[leg_i] = (*picked, kind)
            # 택일(§4.4)에 넘길 후보·앵커 — 이긴 앵커의 top-k **중 category_top_k 개**만 쓴다(#222).
            # hit_list 는 k=max(category_top_k, category_expand_legs) 로 조회돼 늘어나 있을 수 있으므로,
            # 여기서 슬라이스하지 않으면 택일 후보 수가 조용히 늘어 #115 튜닝이 깨진다.
            candidates_by_leg[leg_i] = hit_list[: settings.category_top_k]
            full_hits_by_leg[leg_i] = hit_list  # [#222] 광역 fan-out leaf 후보용 — 슬라이스 안 함
            anchor_by_leg[leg_i] = anchor_text
        # 실패 격리 단위가 leg→앵커로 내려갔다: 앵커 하나가 죽어도 다른 앵커가 canonical 을 냈으면
        # 그 leg 는 드롭하지 않는다(부분 성공 보존, §5). 전부 실패한 leg 만 failed 로 표시한다.
        failed_idx.update(i for i in errored if i not in nearest)
    except Exception as exc:  # noqa: BLE001 - embed 전면 실패 등: 임베딩 경로 leg 만 드롭
        # 임베딩 경로(embed 배치·전면 조회)가 통째로 죽어도, 이미 DB 검증된 exact 매치는 아래
        # result 에서 보존한다(canonical-or-null 은 exact·search 히트 둘 다 canonical 이라 성립).
        # need_idx(임베딩 필요) leg 는 전부 실패로 표시 → canonical 없이 드롭된다(PR #73 리뷰).
        # error_type 을 함께 싣는다(PR #188 리뷰) — 앵커별 조회 실패는 이미
        # gather(return_exceptions=True) → category_leg_search_failed 로 격리되므로 여기 도달하는
        # 것은 embed 배치 실패(I/O) 아니면 **순수 로직 버그**다. 이벤트 이름만으로는 둘이 구분되지
        # 않아 triage 가 어렵다(§11). try 를 I/O 로 좁히지 않는 이유: 로직 버그가 전파되면
        # map_categories 가 통째로 던져 호출부가 exact 매치까지 버린다(앞선 리뷰가 고친 문제).
        logger.warning(
            "category_embed_failed",
            extra={"reason": str(exc), "error_type": type(exc).__name__},
        )
        failed_idx.update(need_idx)

    # ── §4.4 마진 트리거 top-k 택일 (#115) ────────────────────────────────────────────
    # 거리컷이 못 막는 구멍: 추상 라벨은 거리는 가까운데(0.2074 < 컷) 뜻이 틀리다. 마진으로는
    # 잡히지만 마진 드롭은 '양말'(1·2위 둘 다 정답)을 오탐하므로 드롭이 아니라 **택일**한다.
    select_dropped: set[int] = set()  # 택일이 "맞는 후보 없음"을 낸 leg → canonical 없이 드롭
    # 택일이 top-1 을 **교체한** leg (PR #188 리뷰). 교체되면 하류 로그의 distance 는 새 후보
    # 기준인데 margin 은 택일 이전 top1 vs top2 값이라 **두 숫자가 서로 다른 후보를 가리킨다**.
    # margin 을 pick 기준으로 재계산하지 않는 이유: margin 의 용도가 "택일을 발동시킨 애매함의
    # 세기"(트리거 임계 튜닝, §11)라 발동 시점 값이어야 하고, distance 는 실제 채택값이어야
    # 거리컷 튜닝에 쓸 수 있다 — 둘 다 각자의 목적에는 옳으므로 필요한 건 **구분 표시**다.
    select_changed: set[int] = set()
    # 이 호출이 실제로 쓴 택일 예산(#217 PR 리뷰) — 호출부가 두 번째 매핑에 남은 몫을 넘긴다.
    # 택일 단계가 예외로 죽어도 0 이 아니라 **그때까지 시도한 수**여야 예산이 새지 않는다.
    select_calls = 0
    # 이 단계 전체를 격리한다(PR #188 리뷰) — LLM 호출은 gather(return_exceptions=True) 로 이미
    # 막혀 있지만 그 앞뒤의 순수 파이썬 로직(설정 접근·마진 필터·정렬·상한)은 어떤 try 에도 안
    # 감싸여 있었다. 여기서 예외가 나면 map_categories 가 통째로 던지고 호출부(graph)가
    # `category_legs = []` 로 만들어 **exact 매치와 이미 성공한 임베딩 top-1 까지 전부 버린다** —
    # 이 함수가 표방한 "실패는 leg 단위로 격리" 원칙(docstring (5))이 §4.4 추가로 깨져 있었다.
    # 실패 시 택일만 건너뛰고 전 leg 이 임베딩 top-1 을 유지한다(llm 미구성 경로와 동일 degrade).
    try:
        ambiguous = [
            i
            for i in sorted(nearest)
            if nearest[i][1] <= distance_max  # 거리컷 통과분만(멀면 이미 버릴 leg)
            and nearest[i][2] is not None  # 마진 None(히트 1건) = 비교 대상 없음 → 트리거 아님
            and nearest[i][2] <= settings.category_select_margin_max
        ]
        # 사유는 leg 당 하나여야 한다(PR #188 리뷰) — llm 미구성을 먼저 처리하지 않으면 상한 초과분이
        # "max_calls"(사실 아님) + "llm_unavailable" 로 이중 기록돼 관측 목적을 스스로 훼손한다.
        targets: list[int] = []
        if llm is None:  # LLM 미구성 — 매핑을 LLM 에 종속시키지 않는다(전 leg top-1 유지)
            for i in ambiguous:
                # margin 을 함께 남긴다 — 이 이벤트의 용도가 마진 분포 기반 임계 재튜닝(§11)이라
                # 사유별로 payload 가 다르면 그 사유의 표본이 분석에서 빠진다.
                logger.info(
                    "category_select_unavailable",
                    extra={
                        "reason": "llm_unavailable",
                        "canonical": nearest[i][0],
                        "margin": nearest[i][2],
                    },
                )
        else:
            # 턴당 상한 — fan-out 전 leg 이 애매하면 LLM 이 폭증한다. 초과분은 top-1 유지(종전 동작).
            # 예산은 **마진이 작은(가장 애매한) leg 부터** 쓴다(PR #188 리뷰) — 애매함의 판정 기준이
            # 마진인데 배분을 leg 인덱스로 하면 코드 안에서 기준이 어긋나, 마진 0.002 가 검증 없이
            # top-1 로 남고 컷 턱걸이 0.019 가 예산을 먹는 역전이 생긴다. `legs[0]` 이 대표 카테고리
            # (칩·멀티턴 승계)라 "눈에 띄는 것 먼저"라는 명분도 있으나, 애매함이 큰 leg 을 방치하면
            # 틀린 카테고리로 검색이 좁혀지는 손해가 대표 여부와 무관하게 발생한다. 대표 leg 이 정말
            # 애매하면 마진도 작아 우선순위를 자연히 얻는다. 동률은 leg 인덱스 순(정렬 안정성).
            # `select_max_calls` 주입이 있으면 그 값을 쓴다(#217 PR 리뷰) — 상한이 **턴당**이라
            # 매핑이 턴에 2회 불릴 때(원 legs·전개 legs) 각 호출이 예산을 따로 먹으면 상한이 2배로
            # 깨진다. 호출부가 남은 예산을 계산해 넘긴다(§6.1).
            max_calls = (
                settings.category_select_max_calls if select_max_calls is None else select_max_calls
            )
            ambiguous.sort(key=lambda i: nearest[i][2])
            for i in ambiguous[max_calls:]:
                logger.info(
                    "category_select_unavailable",
                    extra={
                        "reason": "max_calls",
                        "canonical": nearest[i][0],
                        "margin": nearest[i][2],
                    },
                )
            targets = ambiguous[:max_calls]
        # 예산 소비는 **gather 앞에서** 센다 — 실패한 호출도 비용이 발생하므로(관측 기록도 호출 전에
        # 한다) 결과를 보고 세면 실패분이 예산에서 빠져 다음 매핑이 더 쓰게 된다.
        select_calls = len(targets)
        if targets:
            picks = await asyncio.gather(
                *(
                    select_category(
                        llm,
                        # 발화 + 앵커를 둘 다 싣는다 — 발화만 주면 멀티 leg 이 같은 질문을 받아 leg
                        # 정체성이 사라지고, 앵커만 주면 '선물용품' 같은 라벨은 판단 근거가 없다(§4.4).
                        query=f"{utterance} / 찾는 상품: {anchor_by_leg[i]}",
                        candidates=[c for c, _ in candidates_by_leg[i]],
                        tier=tier,
                        # 관측(§6.3)은 모델을 실제로 부르는 select 안에서 한다 — 여기서 기록하면
                        # LLM 을 쓰지 않는 대체 구현에도 유령 호출이 남는다(PR #188 리뷰).
                        settings=settings,
                        observer=observer,
                    )
                    for i in targets
                ),
                return_exceptions=True,
            )
            for i, pick in zip(targets, picks, strict=True):
                top1, _dist, margin, kind = nearest[i]
                if isinstance(pick, Exception):
                    # 판정 실패는 "맞는 후보 없음"과 다르다 — 인프라 실패로 카테고리를 잃으면 후퇴이므로
                    # 임베딩 top-1 을 유지한다(§4.4 실패 degrade).
                    logger.info(
                        "category_select_unavailable",
                        extra={"reason": str(pick), "canonical": top1, "margin": margin},
                    )
                    continue
                if pick is None:
                    logger.info(
                        "category_select_null",
                        extra={"raw": raws[i], "query": qtexts[i], "top1": top1, "margin": margin},
                    )
                    select_dropped.add(i)
                    continue
                raw_chosen = {c: d for c, d in candidates_by_leg[i]}.get(pick)
                # `_top1_with_margin` 과 **같은 정밀도**로 맞춘다(PR #188 리뷰) — 원시값을 그대로
                # 두면 같은 `distance` 필드에 leg 마다 정밀도가 달라져 §11 임계 재튜닝 분포에
                # 노이즈가 섞이고, 거리컷이 `picked[1] > distance_max` 비교라 임계 근처에서
                # 채택/드롭까지 갈린다(0.220001 은 드롭, 0.22 는 채택).
                chosen = None if raw_chosen is None else round(raw_chosen, 4)
                if chosen is None:
                    # 후보 밖 값(주입 fake·환각) — select_category 의 membership 가드가 이미 막지만
                    # 주입형 seam 이라 방어한다. 미검증 값을 canonical 로 내보내지 않고 top-1 유지.
                    logger.warning(
                        "category_select_unavailable",
                        extra={"reason": "off_candidate", "pick": pick},
                    )
                    continue
                changed = pick != top1
                if changed:
                    select_changed.add(i)  # 하류 로그의 distance·margin 기준 불일치 표시(§11)
                # 교체가 없어도(top-1 확정) 남긴다 — "택일이 돌아 확정했다"와 "트리거가 안 됐다"를
                # 사후에 구분할 수 없으면 트리거 임계를 재튜닝할 근거가 사라진다(§11).
                logger.info(
                    "category_selected",
                    extra={
                        "top1": top1,
                        "canonical": pick,
                        "changed": changed,
                        "distance": chosen,
                        "margin": margin,
                        "anchor_kind": kind,
                    },
                )
                # 택일 결과에도 거리컷을 다시 적용한다(아래 result 루프) — top-k 안에서 골랐어도 top-1
                # 보다 먼 후보일 수 있고("선물용품" → 독서용품 0.2292), 그건 "가까운 칸이 없다"는 뜻이다.
                nearest[i] = (pick, chosen, margin, kind)
    except Exception as exc:  # noqa: BLE001 - 택일 단계 실패: 전 leg 이 임베딩 top-1 을 유지한다
        # LLM 호출 실패는 gather(return_exceptions=True) → category_select_unavailable 로 이미
        # 격리되므로 **여기 도달하는 것은 LLM 실패가 아니다** — 설정 접근·정렬·배분 등 순수 로직
        # 오류다. 그 사실이 이벤트 이름에 드러나 있지만 타입까지 실어 triage 를 돕는다(PR #188 리뷰).
        logger.warning(
            "category_select_stage_failed",
            extra={"reason": str(exc), "error_type": type(exc).__name__},
        )

    result: list[tuple[str, str | None]] = []
    # 전개 트리거 입력(#217 §4) — canonical 을 못 낸 leg 만. 조회 예외·히트 0건은 담지 않는다.
    unresolved: list[str] = []
    # [#222] 광역 발화 fan-out 후보 — unresolved 와 같은 조건(거리컷 드롭·택일 null)에서만 채운다.
    # [PR #318 리뷰 R5-1] leg **별로** 모은다(합쳐서 하나의 flat list 에 바로 넣지 않는다) —
    # unresolved leg 이 여럿이면 아래에서 leg 순서대로 라운드로빈 인터리브한 뒤에야
    # `category_expand_legs`(턴 전체 상한)를 적용해야, 먼저 처리된 leg 이 예산을 통째로
    # 가져가고 뒤 leg 이 0개가 되는 예산 독식을 막는다.
    expansion_by_leg: dict[int, list[tuple[str, str | None]]] = {}

    def _anchor_text(i: int) -> str:
        """실패 leg 을 식별할 텍스트 — 이긴 앵커(query 우선 §4.3.1), 없으면 원 신호로 폴백."""
        return anchor_by_leg.get(i) or qtexts[i] or raws[i] or ""

    def _collect_expansion_leaves(i: int, anchor_kind: str) -> None:
        """canonical 을 못 낸 leg 의 이긴 앵커 top-k 앞 category_expand_legs 개를 fan-out 후보로 담는다.

        `full_hits_by_leg` 는 need_idx 조회가 성공한 leg 에만 있다 — 조회 예외·히트 0건 leg 은
        여기 도달하지 않으므로(호출부가 `unresolved` 를 채우는 두 분기에서만 부른다) 자연히
        빈 채로 남는다(#222 계약: 후보 자체가 없어서 담을 것이 없다).

        [#222 R2 F-2] **잔여 위험 — 조건 전용 앵커가 새면 노이즈 8개가 나온다.** 이 앵커는
        leg 의 raw/query 텍스트를 그대로 임베딩한 것이라, "평점 높은 거"·"가성비 좋은 거"처럼
        카테고리 신호가 아니라 조건·수식어뿐인 텍스트가 여기까지 들어오면 top-k 가 서로 무관한
        카테고리 8개(게임/도서/화장품/향수 …)로 흩어진다(오케스트레이터 라이브 pg 실측 6/6).
        지금은 라이브 decompose 가 순수 조건 발화에서 `categoryQueries` 를 비워 내
        (`raws[i]`·`qtexts[i]` 모두 없음, §4 "신호 없음" 스킵)이 코드 경로 자체에 도달하지 않아
        **재현되지 않는다** — 다만 이는 decompose 프롬프트가 우연히 지켜주는 전제이지 이 함수가
        스스로 강제하는 불변식이 아니다. `case`(§3 이슈 원안이 쓰려던 신호)로 새 게이트를 걸지
        않는다 — 실측상 `case` 는 이 축에서 신뢰할 수 없다(`청바지 보여줘`=case 2, `가전
        추천해줘`=case 3/2/2 로 발화마다 흔들림) — case!=N 게이트는 협소 발화까지 함께 막는다.
        `graph.py` 의 무필터 폴백(§4.6 F-1, `category_expand_zero_fallback`)이 이 피해의
        **하한**을 잡는다 — 노이즈 leg 8개가 전부 0건이면 무필터로 되돌아가 최소한 "엉뚱한
        카테고리로 좁혀진 0건"은 막는다. 그러나 노이즈 leg 이 0건이 아니라 *그 엉뚱한 카테고리의
        진짜 상품*을 돌려주는 경우는 폴백이 못 막는다(검색 결과가 비어 있지 않아 트리거되지
        않음) — 사후 감시는 이 함수가 매 호출 남기는 `category_expansion_leaves` 로그의 `mids`
        필드로 한다(조건 전용 발화가 새면 그 발화의 mids 가 서로 무관한 대분류로 흩어져 보인다).
        """
        # [PR #318 리뷰 R6-2] 킬스위치가 꺼져 있으면 아예 돌지 않는다 — 계속 돌면 로그
        # (`category_expansion_leaves`)가 계속 쌓여 "롤백이 안 되는 롤백 스위치"가 된다.
        if not settings.category_expand_enabled:
            return
        hits = full_hits_by_leg.get(i)
        if not hits:
            return
        leaves = hits[: settings.category_expand_legs]
        # [#428 리뷰 2차 R2-1] `category_expand_legs=0`(ge=0 필드, 합법값)이면 슬라이스가 빈
        # 리스트가 된다 — 빈 leg 을 담으면 `expansion_by_leg[i] = []`가 그대로 들어가 하류
        # `_consensus_filter` 가 `leaves[0]`(top-1)을 인덱싱하다 IndexError 를 낸다(Claude PR
        # Review, PR #444). 여기서 거른다: 후보 0개 leg 은 "그래도 어디를 볼 것인가"라는
        # `expansion_by_leg` 계약상 담을 것이 없고, 로그도 `count: 0`으로 무의미하게 쌓인다.
        # **동작 변화는 없다** — expand_legs=0 이면 최종 `dedup_truncate(..., 0)` 가 어차피 전부
        # 잘라내므로, 이 가드는 그 결과를 앞당길 뿐 무의미한 저장·로그만 없앤다.
        if not leaves:
            return
        mids: list[str] = []
        seen_mid: set[str] = set()
        for canonical, _distance in leaves:
            mid = canonical.split(" > ", 1)[0]
            if mid not in seen_mid:
                seen_mid.add(mid)
                mids.append(mid)
        # 이 leg 의 후보만 담는다 — 다른 leg 과 섞지 않는다(R5-1, 아래 인터리브가 leg 경계를 쓴다).
        expansion_by_leg[i] = [(canonical, qtexts[i]) for canonical, _distance in leaves]
        logger.info(
            "category_expansion_leaves",
            extra={"anchor_kind": anchor_kind, "count": len(leaves), "mids": mids},
        )

    # 조립 루프도 격리한다(PR 리뷰) — 여기서 예외가 나면 `map_categories` 가 통째로 던지고 호출부가
    # 빈 legs 로 degrade 해 **이미 DB 검증된 exact 매치와 채택 canonical 까지 버린다.** PR #188 이
    # 택일 단계를 감싼 것과 같은 이유이며(§5 부분 성공 보존), 덤으로 `select_calls` 회계도 살아남아
    # 두 번째 매핑이 예산을 다시 받는 일이 없다(#217 §6.1).
    # 여기 도달하는 것은 I/O 실패가 아니다 — 앵커 조회·택일은 각자 격리돼 있으므로 **순수 로직**
    # 오류다. `error_type` 을 실어 triage 를 돕는다(PR #188 과 동일 규약).
    try:
        for i, r in enumerate(raws):
            if r and r in exact:
                logger.info("category_mapped", extra={"raw": r, "canonical": r})
                result.append((r, qtexts[i]))
                continue
            if i not in need_idx:
                continue  # 신호 없는 leg(raw·query 모두 없음) → 카테고리 강제 없이 스킵(#22)
            picked = nearest.get(i)
            # §4.5 거리컷 마진 예외(#115) — 거리가 멀어도 1위만 확 가까우면(마진 두꺼움) "맞는 칸이
            # 분명히 하나 있다"는 뜻이라 채택한다. 거리는 도메인 어휘 차이(식품은 상품명≠leaf명)에
            # 오염되지만 마진은 상쇄된다. 마진 None(히트 1건)은 확신을 잴 수 없으므로 예외 대상 아님.
            if (
                picked
                and picked[1] > distance_max
                and picked[2] is not None
                and picked[2] >= override_margin
            ):
                logger.info(
                    "category_distance_override",
                    extra={
                        "raw": r,
                        "query": qtexts[i],
                        "canonical": picked[0],
                        "distance": picked[1],
                        "margin": picked[2],
                        "anchor_kind": picked[3],
                        "threshold": distance_max,
                        "select_changed": i in select_changed,
                    },
                )
            elif picked and picked[1] > distance_max:
                # 거리컷(§4 #115) — 최근접이 너무 멀다 = "맞는 칸이 taxonomy 에 없다". 틀린 카테고리로
                # 좁히면 정답 상품이 후보에서 아예 제외되므로 canonical 없이 드롭하고 semanticQuery 로
                # 넓게 찾게 둔다. 전용 이벤트로 남긴다 — category_unmapped(히트 0건=시드 결측 품질 신호)
                # 와 섞으면 정책적 드롭이 품질 메트릭을 오염시킨다(§5 격리 규약과 동일 취지).
                logger.info(
                    "category_distance_rejected",
                    extra={
                        "raw": r,
                        "query": qtexts[i],
                        "canonical": picked[0],
                        "distance": picked[1],
                        "margin": picked[2],
                        "anchor_kind": picked[3],
                        "threshold": distance_max,
                        # True 면 distance 는 택일이 고른 후보, margin 은 택일 이전 top1 기준이다
                        "select_changed": i in select_changed,
                    },
                )
                # #217 §4 ① — "맞는 칸이 taxonomy 에 없다"는 판정이라 전개 대상이다.
                unresolved.append(_anchor_text(i))
                _collect_expansion_leaves(i, picked[3])  # [#222] 광역 fan-out 후보
                continue
            if i in select_dropped:
                # §4.4 택일이 "맞는 후보 없음" → 이미 category_select_null 로 관측됨. 이 leg 은 위
                # 거리 분기에 닿지 않는다 — 택일 트리거(ambiguous)가 **거리컷 통과분만** 대상으로
                # 하므로 nearest[i] 의 거리는 임계 이하다(테스트로 고정).
                # #217 §4 ② — ① 과 같은 뜻("후보 중 맞는 것이 없다")이라 함께 전개 대상이다.
                unresolved.append(_anchor_text(i))
                if picked:  # nearest[i] 는 택일 null 이어도 원 top-1 을 그대로 들고 있다
                    _collect_expansion_leaves(i, picked[3])  # [#222] 광역 fan-out 후보
                continue
            if picked:
                canonical, distance, margin, anchor_kind = picked
                # 이벤트는 종전 정의(raw 유무)를 유지해 메트릭 연속성을 지키고, 실제로 canonical 을 낸
                # 앵커는 anchor_kind 로 구분한다(§11 #115) — raw 가 있어도 query 앵커가 이길 수 있으므로
                # (§4.3) `category_repaired` + anchor_kind=query 조합이 정상이다. 거리컷 임계 재튜닝과
                # 후속 top-k 택일 트리거가 distance·margin 분포에 의존한다.
                event = "category_repaired" if r else "category_fallback_top1"
                logger.info(
                    event,
                    extra={
                        "raw": r,
                        "canonical": canonical,
                        "distance": distance,
                        "margin": margin,
                        "anchor_kind": anchor_kind,
                        # True 면 distance 는 택일이 고른 후보, margin 은 택일 이전 top1 기준이라
                        # 두 값을 함께 쓰는 분포 분석에서 제외하거나 따로 봐야 한다(§11).
                        "select_changed": i in select_changed,
                    },
                )
                result.append((canonical, qtexts[i]))
            elif i in failed_idx:
                # 조회 예외로 실패 — 이미 실패 로그로 관측됨. 품질 메트릭 오염 방지로 드롭만.
                # **`unresolved` 에 담지 않는다**(#217 §4 ③): 실패 원인이 발화 내용이 아니라 인프라라
                # LLM 전개로 풀리지 않고, 전개해봐야 같은 pg·임베딩을 다시 두드린다.
                continue
            else:
                # 신호(raw/query)는 있었고 조회도 정상이나 히트 0건 → canonical 없이 드롭(top-k 미스율
                # 품질 신호, §11). categories 미시드·임베딩 결측 등 드문 상태라 관측 가능하게 남긴다(#4).
                # 여기도 `unresolved` 대상이 아니다(#217 §4 ④) — 전개된 상품명도 같은 빈 사전을 본다.
                logger.warning("category_unmapped", extra={"raw": r})
    except Exception as exc:  # noqa: BLE001 - 조립 로직 오류: 그때까지 확정된 leg 은 보존한다
        logger.warning(
            "category_assembly_failed",
            extra={"reason": str(exc), "error_type": type(exc).__name__},
        )

    # [#428] 전개 후 재매핑(sibling_expansion=True)에만 대분류 합의 필터를 건다 — 원 매핑(서로
    # 다른 니즈)에는 적용하지 않는다.
    # [#428 리뷰 6차 R6-1/R6-2] 라운드 1엔 여기서 "필터가 실제로 돌아 무언가 걸렀는지와 애초에
    # 돌지 않았는지를 로그로 구분한다"고 썼지만 사실이 아니었다(#444 Claude 리뷰 5차 지적) —
    # `_consensus_filter` 가 미적용을 `None` 으로 냈던 시절엔 `consensus_stats is not None` 게이트
    # 때문에 "합의가 성립하지 않아 미적용"과 "sibling_expansion=False 라 애초에 안 돌았다"가 둘 다
    # 무기록이라 로그만으로 구분되지 않았다. 이제 `_consensus_filter` 는 미적용 사유
    # (`single_leg`·`no_consensus`·`leg_without_winning_mid`)도 전부 stats 로 반환하므로, 적용·
    # 미적용(사유 3종) 전부가 로그를 남기고 **`sibling_expansion=False` 만 유일한 무기록 상태**가
    # 된다 — 로그 유무 자체가 "이 턴에 합의 필터가 실제로 돌았는가"의 신뢰 가능한 신호다. 이
    # 구분이 있어야 아래 `source_legs`(R5-2, "발동한 턴을 운영에서 식별")가 실제로 쓸모 있다 —
    # 무기록 상태가 여러 원인을 덮으면 "이 로그가 남았다 = 특정 상황"이라는 R5-2 의 전제가
    # 깨진다.
    # [#428 리뷰 2차 R2-3] 이 호출도 조립 루프(위 `category_assembly_failed`)·택일 단계
    # (`category_select_stage_failed`)와 같은 이유로 자체 격리한다 — 여기서 예외가 나면
    # `map_categories` 가 통째로 던지고 `_map_or_empty`(graph.py)가 **빈 CategoryMapping**으로
    # degrade 해 이미 DB 검증된 exact 매치와 채택 canonical 까지 전부 버린다(Claude PR Review,
    # PR #444 — 조립 루프의 try/except **밖**에 있던 것이 진짜 위험이었다). 합의는 개선 시도일
    # 뿐이므로 실패 시 필터를 건너뛰고 `expansion_by_leg` 원본을 그대로 쓴다(#217 "§7 후퇴 없음"과
    # 같은 원칙). 여기 도달하는 것은 I/O 실패가 아니다 — 조회·택일은 이미 각자 격리돼 있으므로
    # **순수 로직 오류**다(형제 except 절들과 동일 서술, `error_type` 도 동일 규약으로 싣는다).
    if sibling_expansion:
        try:
            expansion_by_leg, consensus_stats = _consensus_filter(expansion_by_leg)
            # [#428 리뷰 6차 R6-1] `_consensus_filter` 는 이제 미적용도 항상 stats 를 낸다 —
            # `reason`(`single_leg`·`no_consensus`·`leg_without_winning_mid`)으로 사유를 가른다.
            # 별개 이벤트를 사유마다 만들지 않고 `category_select_unavailable` 과 같은 규약으로
            # `category_expansion_consensus_skipped` 하나에 `reason` 필드로 싣는다.
            skipped = consensus_stats.pop("skipped")
            event = (
                "category_expansion_consensus_skipped"
                if skipped
                else "category_expansion_consensus"
            )
            # [#428 리뷰 5차 R5-2] "원 발화의 leg 수"를 그대로 실으려면 map_categories 에
            # 관측 전용 파라미터를 새로 뚫어야 하는데, 그건 매퍼 인터페이스를 관측 목적으로
            # 오염시킨다. 대신 R5-1 이 게이트를 상류(graph.py)로 옮겼으므로 "이 로그가
            # 남았다 = 원 발화 신호 leg 이 0~1개였다"가 이미 함의된다. `source_legs`(이번
            # 매핑의 입력 leg 수 = 전개 재매핑에서는 곧 전개 아이템 수)는 그 위에 "형제 몇
            # 개가 합의에 참여했나"를 더해, 이 상호작용이 실제로 발동한 턴을 사후 분석할 수
            # 있게 한다.
            logger.info(event, extra={**consensus_stats, "source_legs": len(queries)})
        except Exception as exc:  # noqa: BLE001 - 합의 필터 실패: 원본 expansion_by_leg 를 보존한다
            logger.warning(
                "category_expansion_consensus_failed",
                extra={"reason": str(exc), "error_type": type(exc).__name__},
            )

    return CategoryMapping(
        legs=dedup_truncate(result, fanout_max),
        unresolved=unresolved,
        select_calls=select_calls,
        # R5-1: 인터리브가 **먼저**, 절단(dedup_truncate)이 **나중**이어야 leg 간 형평이 산다 —
        # 순서를 바꾸면 leg 0 이 이미 상한을 다 채운 뒤라 인터리브가 의미 없어진다.
        expansion_leaves=dedup_truncate(
            _interleave_by_leg(expansion_by_leg), settings.category_expand_legs
        ),
    )
