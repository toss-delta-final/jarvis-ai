"""§2.5 오류 봉투의 **현재** 상태→코드 매핑 공백을 고정한다 (이슈 #149).

api-spec v0.21.0 §3.9 구현 노트가 코드에 대해 세 가지 사실을 주장한다. 그 주장이 문서 안의
단언으로만 남지 않도록, 여기서는 **지금 동작을 그대로** 못 박는다 (희망 동작을 쓰면 즉시 실패한다).

1. `409` 의 기본 코드는 `STREAM_IN_PROGRESS` 다 — 프로필 그래프 변경(§3.9)이 코드를 지정하지 않고
   409 를 내면 FE 에 "스트림 진행 중"이 표시된다.
2. `404` 는 매핑에 아예 없어 일반 코드로 나간다 — §2.5 에 `404` 를 등재했으므로 #150 이 맵에
   기본 항목을 추가해야 한다.
3. `412` 도 매핑에 없다 — 이것이 충돌 응답으로 `412` 대신 `409` 를 택한 근거(§2.5 말미)다.

맵이 채워지면 이 테스트가 깨지고, 그때 api-spec §3.9 구현 노트를 함께 갱신해야 한다.
"""

from __future__ import annotations

import pytest

from app.core.errors import _resolve


def test_409_default_code_is_stream_in_progress() -> None:
    """코드 미지정 409 는 스트림 관련 코드로 나간다 — §3.9 는 반드시 detail 로 덮어야 한다."""
    code, _message = _resolve(409, None)
    assert code == "STREAM_IN_PROGRESS"


@pytest.mark.parametrize("status_code", [404, 412])
def test_unmapped_statuses_fall_back_to_generic_code(status_code: int) -> None:
    """404·412 는 매핑에 없어 기계 판독용 코드가 아닌 일반값으로 나간다."""
    code, message = _resolve(status_code, None)
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
