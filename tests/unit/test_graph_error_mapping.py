"""그래프 도메인 예외 → HTTP 매핑 (#360, api-spec §2.5·§3.9).

**라우터는 도메인 예외만 던진다.** 상태 코드·`code` 문자열을 라우터가 매번 기억해야 하면 한 곳만
빠뜨려도 계약이 깨지는데, 그 결함은 **정상 경로 테스트로 잡히지 않는다** — api-spec §3.9 구현
노트 1이 경고한 그대로다(`409` 를 코드 지정 없이 던지면 FE 에 "스트림 진행 중"이 표시된다).

그래서 매핑을 `_GRAPH_ERROR_MAP` 한 곳에 모으고, **누락을 테스트가 잡는다.** 앱 기동을 막는
import 시점 단언으로 하지 않는 이유는 이 저장소에 그런 선례가 없고(`config.py` 는 `@model_validator`
로 한다) `python -O` 가 `assert` 를 지워 버리기 때문이다.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.profile.graph_errors import (
    GraphEdgeNotEditable,
    GraphEdgeNotFound,
    GraphMutationError,
    GraphObjectUnknown,
    GraphStoreUnavailable,
    GraphVersionConflict,
)
from app.core.errors import _GRAPH_ERROR_MAP, error_envelope, install_error_handling


def _all_subclasses(cls: type) -> set[type]:
    """**재귀** 수집 — `__subclasses__()` 는 1단계만 보므로 손자 예외를 놓친다."""
    found: set[type] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _all_subclasses(sub)
    return found


def test_every_graph_mutation_error_has_a_status_mapping() -> None:
    """`GraphMutationError` 하위를 새로 만들고 매핑을 안 채우면 여기서 잡는다.

    안 잡으면 그 예외가 처음 발생하는 **운영 시점에 500** 으로 드러난다.
    """
    missing = _all_subclasses(GraphMutationError) - set(_GRAPH_ERROR_MAP)

    assert not missing, f"_GRAPH_ERROR_MAP 미등록: {sorted(c.__name__ for c in missing)}"


# ─────────── 봉투 (§2.5) ───────────


def test_envelope_omits_detail_when_absent() -> None:
    """`detail` 을 안 주면 키 자체가 없다 — 기존 호출부(위치 인자 3개)가 그대로 동작한다."""
    assert error_envelope("BAD_REQUEST", "x", "rid") == {
        "error": {"code": "BAD_REQUEST", "message": "x", "requestId": "rid"}
    }


def test_envelope_nests_detail_inside_error() -> None:
    """**[HARD]** 봉투 확장은 `error.detail` 로만 — `error` 와 나란한 형제 필드를 만들지 않는다."""
    body = error_envelope("PROFILE_VERSION_CONFLICT", "x", "rid", detail={"graphVersion": "g43"})

    assert set(body) == {"error"}  # 최상위에 형제가 생기지 않았다
    assert body["error"]["detail"] == {"graphVersion": "g43"}


# ─────────── 핸들러 (§3.9 실패표) ───────────


def _client(exc: Exception) -> TestClient:
    app = FastAPI()
    install_error_handling(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("exc", "status_code", "code"),
    [
        (GraphObjectUnknown("e_1"), 400, "BAD_REQUEST"),
        (GraphEdgeNotFound("e_1"), 404, "PROFILE_EDGE_NOT_FOUND"),
        (GraphVersionConflict("g43"), 409, "PROFILE_VERSION_CONFLICT"),
        (GraphEdgeNotEditable("e_1"), 409, "PROFILE_EDGE_NOT_EDITABLE"),
        (GraphStoreUnavailable("down"), 503, "UPSTREAM_UNAVAILABLE"),
    ],
)
def test_domain_errors_map_to_the_contract_codes(
    exc: Exception, status_code: int, code: str
) -> None:
    response = _client(exc).get("/boom")

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


def test_version_conflict_carries_the_latest_version_in_detail() -> None:
    """`409 PROFILE_VERSION_CONFLICT` 는 최신 버전을 **`error.detail.graphVersion`** 에 동봉한다.

    이게 없으면 FE 가 재조회 없이는 재시도할 수 없다. Spring 은 이 위치를 그대로 유지해 FE 에
    전달한다(api-spec §3.9, `M-12`).
    """
    response = _client(GraphVersionConflict("g43")).get("/boom")

    assert response.json()["error"]["detail"] == {"graphVersion": "g43"}


def test_other_graph_errors_carry_no_detail() -> None:
    """`detail` 은 실을 것이 있을 때만 붙는다 — 빈 객체를 습관적으로 내보내지 않는다."""
    body = _client(GraphEdgeNotFound("e_1")).get("/boom").json()

    assert "detail" not in body["error"]


def test_the_envelope_never_leaks_the_edge_id() -> None:
    """예외 메시지에 든 `edgeId` 가 응답 본문으로 새지 않는다.

    남의 edge 든 미존재든 **동일 응답**이어야 열거가 막힌다(api-spec §3.9). 예외 문자열을 그대로
    `message` 로 쓰면 그 보장이 깨진다.
    """
    body = _client(GraphEdgeNotFound("e_2f80d1aa63b74c19")).get("/boom").json()

    assert "e_2f80d1aa63b74c19" not in str(body)
