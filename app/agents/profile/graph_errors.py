"""사용자 그래프 변경의 도메인 예외 (#358).

**FastAPI 에 의존하지 않는다.** 저장 계층이 HTTP 를 알면 #360 이 오기 전에 계약 모양을 굳히게
되고, 그때 `app/core/errors.py` 를 함께 건드리면 채팅 표면(§2.5 봉투·409 기본 코드)에 회귀
반경이 생긴다. 여기서는 "무슨 일이 일어났는가"만 표현하고, 상태 코드 매핑은 호출자가 생기는
#360 시점에 한다.

api-spec §3.9 의 대응 관계(매핑은 #360 의 `app/core/errors.py` 가 소유한다):
  `GraphVersionConflict`   → 409 PROFILE_VERSION_CONFLICT (+ `error.detail.graphVersion`)
  `GraphEdgeNotFound`      → 404 PROFILE_EDGE_NOT_FOUND
  `GraphEdgeNotEditable`   → 409 PROFILE_EDGE_NOT_EDITABLE
  `GraphObjectUnknown`     → 400 BAD_REQUEST
  `GraphStoreUnavailable`  → 503 UPSTREAM_UNAVAILABLE
"""

from __future__ import annotations


class GraphMutationError(Exception):
    """사용자 그래프 변경이 적용되지 않았다 — 공통 조상.

    **이 계열이 던져졌다는 것은 문서가 변경 전 상태 그대로라는 뜻이다.** 조립 경로는 문서 쓰기가
    성공한 뒤에만 감사·원장 완료를 기록하므로, 예외로 빠져나간 요청은 부분 적용을 남기지 않는다.
    """


class GraphVersionConflict(GraphMutationError):
    """`If-Match` 가 현재 `revision` 과 다르다 (REQ-PGRAPH-040).

    **최신 버전을 실어 나른다** — 호출부가 `error.detail.graphVersion` 으로 내보내야 클라이언트가
    다시 조회하고 재시도할 수 있다. 이걸 예외에 안 담으면 #360 이 문서를 한 번 더 읽어야 하고,
    그 사이 또 바뀌면 응답에 실린 버전이 이미 낡은 값이 된다.
    """

    def __init__(self, latest_graph_version: str) -> None:
        super().__init__(f"graph version conflict (latest={latest_graph_version})")
        self.latest_graph_version = latest_graph_version


class GraphEdgeNotFound(GraphMutationError):
    """대상 edge 가 없다.

    사용자가 이미 지웠거나(물리 삭제), 수정으로 `edgeId` 가 바뀐 뒤 옛 키로 다시 부른 경우다
    (api-spec §3.9.1 — "응답의 `edge.edgeId` 로 클라이언트 키를 교체해야 하며, 그러지 않으면
    다음 변경이 `404` 다").
    """


class GraphEdgeNotEditable(GraphMutationError):
    """편집할 수 없는 edge 다 — **구매 이력 파생(`purchased`)** (api-spec §3.9.1).

    구매는 의견이 아니라 사실이라 사용자가 뒤집을 대상이 아니다. 판정은 와이어 `editable` 과
    같은 술어여야 한다 — 목록에서 잠긴 것으로 보이는데 수정이 통하면 계약이 거짓이 된다.

    **삭제(§3.9.2)에는 이 예외가 없다** — `editable` 은 수정만 막는다. 구매 파생도 지울 수
    있고, 그것이 재구매 dedup 에 영향을 주지 않는다(dedup 은 프로필이 아니라 질의 시점 I-19 를
    읽는다).

    **[#360] 두 `409` 가 동시에 성립하면 이쪽이 우선한다** — 스테일한 `If-Match` 로 구매 파생을
    고치려 한 경우다. `GraphVersionConflict` 를 먼저 내면 FE 가 규약대로 재조회 후 재시도하고
    그 재시도가 결국 이 예외를 받아 왕복 한 번이 낭비된다. 재조회로 결과가 바뀌지 않는 사실을
    먼저 알린다.
    """


class GraphObjectUnknown(GraphMutationError):
    """요청의 `object` 를 노드로 확정할 수 없다 — `400` (api-spec §3.9.1, #360).

    세 경우다: 통제 어휘 밖 라벨(카탈로그에 없는 카테고리명) · 형식을 못 맞춘 가격대·평점대 ·
    형식은 맞지만 그 사용자 그래프에 없는 `nodeId`.

    **가까운 대상으로 추측해 붙이지 않는다.** 배치가 만드는 식별자와 다른 값이 생기면 같은
    취향이 두 개의 `edgeId` 를 얻어, 하나를 지워도 다른 하나가 살아남고 재파생 차단 표식을
    비켜간다(REQ-PGRAPH-010). 사용자가 지목하지 않은 대상에 취향이 붙는 것은 수정이 아니라
    오염이다.
    """


class GraphStoreUnavailable(GraphMutationError):
    """pg-profile 일시 장애 — 변경이 적용되지 않았고 **문서는 무손상**이다 (SPEC §8).

    `is_state_store_unavailable` 로 판정한 타임아웃·연결 실패만 여기로 옮긴다. 다른 예외를
    묶어 버리면 진짜 버그가 "일시 장애"로 보고되어 재시도만 반복된다.
    """
