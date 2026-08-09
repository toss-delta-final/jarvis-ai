"""사용자 그래프 변경의 도메인 예외 (#358).

**FastAPI 에 의존하지 않는다.** 저장 계층이 HTTP 를 알면 #360 이 오기 전에 계약 모양을 굳히게
되고, 그때 `app/core/errors.py` 를 함께 건드리면 채팅 표면(§2.5 봉투·409 기본 코드)에 회귀
반경이 생긴다. 여기서는 "무슨 일이 일어났는가"만 표현하고, 상태 코드 매핑은 호출자가 생기는
#360 시점에 한다.

api-spec §3.9 의 대응 관계(참고, 매핑은 #360 소관):
  `GraphVersionConflict`   → 409 PROFILE_VERSION_CONFLICT (+ `error.detail.graphVersion`)
  `GraphEdgeNotFound`      → 404 PROFILE_EDGE_NOT_FOUND
  `GraphEdgeNotEditable`   → 409 PROFILE_EDGE_NOT_EDITABLE
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
    """편집할 수 없는 edge 다 — 예: 구매 이력 파생(`purchased`).

    #358 은 이 예외를 정의만 하고 던지지 않는다. 판정 규칙은 편집 가능 여부를 와이어로 내보내는
    #360(`editable` 필드)이 소유한다 — 여기서 규칙을 먼저 박으면 두 곳이 어긋난다.
    """


class GraphStoreUnavailable(GraphMutationError):
    """pg-profile 일시 장애 — 변경이 적용되지 않았고 **문서는 무손상**이다 (SPEC §8).

    `is_state_store_unavailable` 로 판정한 타임아웃·연결 실패만 여기로 옮긴다. 다른 예외를
    묶어 버리면 진짜 버그가 "일시 장애"로 보고되어 재시도만 반복된다.
    """
