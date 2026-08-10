"""개인화 중지 게이트 (SPEC-PROFILE-GRAPH-149 §6.6, REQ-PGRAPH-051/052/054).

#358 이 전용 저장 위치(`profile_personalization_state`)와 읽기 함수를 깔았지만 **프로덕션
호출자가 0건**이었다 — 스위치를 꺼도 아무 일도 일어나지 않았다. 이 모듈이 그 플래그를 소비
지점(rerank·홈 벡터·마이페이지)과 수집 지점("기억해"·세션 버퍼·배치)이 쓸 수 있는 모양으로
감싼다.

**캐시를 두지 않는다.** REQ-PGRAPH-100 의 즉시성 약속이고, `get_personalization_flag` 가
매번 단일 행 SELECT 를 치는 것도 같은 이유다. 이중 방어의 2차(요약 항목 `usable` 표식)는
`reader.read_profile_summary` 가 본다 — 여기는 **1차**다.
"""

from __future__ import annotations

import logging

from app.agents.profile import graph_journal

logger = logging.getLogger(__name__)


async def personalization_enabled(
    user_id: str | int | None, *, on_error: bool | None
) -> bool | None:
    """이 사용자가 개인화를 켜 두었는가. 플래그 행이 없으면 켜짐(기본값).

    **`on_error` 를 호출부가 정하는 것이 이 함수의 요점이다.** 조회가 실패했을 때 잃는 것이
    경로마다 다르기 때문이다:

    - **hot-path 쓰기·소비 → `False`(fail-closed).** 사용자가 명시적으로 껐는데 저장소 장애
      동안 시스템이 몰래 개인화를 재개하는 것이, 이 기능이 막으려는 바로 그 상황이다. 반대
      방향의 대가는 그 턴에 한해 개인화를 잃는 것뿐이고 **게스트와 동등하게 안전 열화**한다.
    - **배치 → `None`(fail-unknown).** 여기서 `False` 를 돌려주면 `generate_session_delta` 가
      "처리됨, 승격 0건" 을 반환하고 `finalizer` 가 그 뒤 `clear_session_ctx_upto` 까지 진행해
      **DB 블립 한 번에 개인화가 켜져 있는 사용자의 세션 버퍼가 영구 삭제**된다. 기존 계약상
      `None` 이 "degrade, 버퍼 보존, RETRYABLE" 이므로 그 어휘를 그대로 쓴다.

    숫자가 아닌 신원은 **조회하지 않고 켜짐으로 본다.** 플래그 테이블 PK 가 `bigint` 라 게스트
    (UUID 문자열)는 행을 가질 수 없고, JWT `sub` 는 값 형식을 검증하지 않으므로
    (`app/core/auth.py`) 회원 id 도 숫자 문자열이 아닐 수 있다 — `int()` 가 그대로 터지면 500 이
    된다. `builder._bootstrap_document` 의 3분기 예외 패턴과 같다.

    **중지 자체는 로그를 남기지 않는다.** 정상 동작이고, degrade 어휘를 붙이면 REQ-PGRAPH-054
    ("중지 여부가 어떤 요청의 실패로도 추론되지 않는다")가 와이어가 아닌 관측 계층에서 깨진다.
    남기는 것은 **조회 실패**뿐이다 — 안 남기면 fail-closed 로 인한 개인화 손실이 영원히 안 보인다.
    """
    try:
        numeric = int(user_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True

    try:
        return await graph_journal.get_personalization_flag(user_id=numeric)
    except Exception:  # noqa: BLE001 - 저장소 실패는 호출부 정책으로 열화한다
        logger.warning("personalization_flag_unavailable", exc_info=True)
        return on_error
