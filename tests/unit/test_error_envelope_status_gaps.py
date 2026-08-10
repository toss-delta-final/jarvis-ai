"""§2.5 오류 봉투의 **현재** 상태→코드 매핑 공백을 고정한다 (이슈 #149).

api-spec v0.21.0 §3.9 구현 노트가 코드에 대해 세 가지 사실을 주장한다. 그 주장이 문서 안의
단언으로만 남지 않도록, 여기서는 **지금 동작을 그대로** 못 박는다 (희망 동작을 쓰면 즉시 실패한다).

1. `409` 의 기본 코드는 `STREAM_IN_PROGRESS` 다 — 프로필 그래프 변경(§3.9)이 코드를 지정하지 않고
   409 를 내면 FE 에 "스트림 진행 중"이 표시된다. **여전히 유효하다** — #360 은 전용 예외
   핸들러로 덮지, 이 기본값을 바꾸지 않는다(채팅 표면 `SESSION_ACTIVE` 3형제가 같은 409 를 쓴다).
2. ~~`404` 는 매핑에 아예 없다~~ — **[해소 #360] 맵에 `NOT_FOUND` 를 등재했다.** 구 서술은
   *"§2.5 에 `404` 를 등재했으므로 맵에 기본 항목을 추가해야 한다"* 는 지시였고 그것을 이행했다.
   edge 케이스는 `_GRAPH_ERROR_MAP` 이 `PROFILE_EDGE_NOT_FOUND` 로 덮는다.
3. `412` 는 여전히 매핑에 없다 — 이것이 충돌 응답으로 `412` 대신 `409` 를 택한 근거(§2.5 말미)이며
   채택하지 않았으므로 채우지 않는다.

맵이 더 채워지면 이 테스트가 깨진다. 그때 api-spec §3.9 구현 노트를 함께 갱신한다.
"""

from __future__ import annotations

import pytest

from app.core.errors import _resolve


def test_409_default_code_is_stream_in_progress() -> None:
    """코드 미지정 409 는 스트림 관련 코드로 나간다 — §3.9 는 반드시 detail 로 덮어야 한다."""
    code, _message = _resolve(409, None)
    assert code == "STREAM_IN_PROGRESS"


def test_404_default_code_is_not_found() -> None:
    """**[#360]** 코드 미지정 `404` 는 §2.5 가 등재한 `NOT_FOUND` 로 나간다.

    edge 케이스(`PROFILE_EDGE_NOT_FOUND`)는 전용 예외 핸들러가 덮는다 — 이 기본값은 그 핸들러를
    안 타는 다른 `404` 의 안전망이다.
    """
    code, _message = _resolve(404, None)
    assert code == "NOT_FOUND"


def test_412_stays_unmapped() -> None:
    """`412` 는 채택하지 않았으므로 매핑을 채우지 않는다 (§2.5 말미 — 충돌은 `409` 다)."""
    code, message = _resolve(412, None)
    assert code == "ERROR"
    assert message == "오류가 발생했습니다"


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (404, "PROFILE_EDGE_NOT_FOUND"),
        (409, "PROFILE_VERSION_CONFLICT"),
        (409, "PROFILE_EDGE_NOT_EDITABLE"),
    ],
)
def test_detail_code_overrides_mapping_below_500(status_code: int, expected_code: str) -> None:
    """5xx 미만은 detail 의 code 가 매핑을 덮는다 — §3.9 가 의존하는 경로다."""
    code, _message = _resolve(status_code, {"code": expected_code, "message": "x"})
    assert code == expected_code
