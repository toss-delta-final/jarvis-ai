"""I-3 인기 상품 후보 조회(`get_popular_products`) 단위 테스트 — api-spec §4.17, 이슈 #162.

실 네트워크 없이 httpx.MockTransport 로 AI→Spring 경계를 태운다(저장소 관례 — respx 미설치).
검증 대상은 계약이 규정한 네 가지다:
  ① 요청이 `/internal/products/popular?size=N` 으로 나가고 서비스 토큰이 실린다
  ② 응답은 I-1 과 동일 DTO 라 `_parse_search_response` 로 그대로 파싱된다
  ③ **0건은 성공이다** — 빈 배열에 예외를 던지면 안 된다(정본: "빈 배열도 정상 결과다")
  ④ **재시도하지 않는다** — §2.9(c) 재시도 1회는 I-1 전용 예외다(#277·#288)
"""

from __future__ import annotations

import httpx
import pytest

import app.services.spring_client as sc
from app.services.spring_client import SpringUnavailableError, get_popular_products

_POPULAR_OK = {
    "success": True,
    "data": [
        {"productId": 101, "name": "P101", "price": 1000},
        {"productId": 102, "name": "P102", "price": 2000},
    ],
}


def _mock_client(monkeypatch: pytest.MonkeyPatch, *responses) -> list[httpx.Request]:
    """호출 순서대로 응답/예외를 내는 MockTransport 를 심고 **받은 요청**을 모아 돌려준다.

    요청을 모으는 이유는 호출 횟수(재시도 여부)와 쿼리 파라미터를 같은 곳에서 보기 위해서다.
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        item = responses[min(len(seen), len(responses) - 1)]
        seen.append(request)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="http://spring.test",
            transport=httpx.MockTransport(_handler),
            headers={"X-Internal-Token": "svc-token-123"},
        ),
    )
    return seen


async def test_requests_popular_path_with_size_and_internal_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """경로·쿼리·인증 레인 — `size` 는 BE 기본값에 맡기지 않고 항상 명시 전송한다(§4.17)."""
    seen = _mock_client(monkeypatch, httpx.Response(200, json=_POPULAR_OK))

    await get_popular_products(size=30)

    assert len(seen) == 1
    assert seen[0].url.path == "/internal/products/popular"
    assert seen[0].url.params["size"] == "30"
    assert seen[0].headers["X-Internal-Token"] == "svc-token-123"


async def test_parses_i1_shaped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """응답이 I-1 과 동일 DTO 라 기존 파서를 그대로 태운다(정본: "같은 DTO 를 쓰는 I-3")."""
    _mock_client(monkeypatch, httpx.Response(200, json=_POPULAR_OK))

    result = await get_popular_products(size=30)

    assert [p.product_id for p in result.products] == [101, 102]
    assert result.total_count == 2


async def test_empty_data_is_success_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """**0건은 성공이다** — 정본 §4.17: "빈 배열도 정상 결과다. 카드 없이 텍스트만 답하면 된다".

    여기서 예외를 던지면 상위가 degrade 로 오인해 무필터 I-1 폴백을 태우고, 그건 이 이슈가
    없애려는 바로 그 13.33MB 호출이다.
    """
    _mock_client(monkeypatch, httpx.Response(200, json={"success": True, "data": []}))

    result = await get_popular_products(size=30)

    assert result.products == []
    assert result.total_count == 0


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"success": False, "error": {"code": "INTERNAL_ERROR"}}),
        httpx.Response(401, json={"success": False, "error": {"code": "INTERNAL_TOKEN_INVALID"}}),
    ],
)
async def test_failure_raises_spring_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: httpx.Response
) -> None:
    """실패 3종(400·401·500) 중 서버·인증 실패는 상위가 degrade 판단할 수 있게 전파한다."""
    _mock_client(monkeypatch, failure)

    with pytest.raises(SpringUnavailableError):
        await get_popular_products(size=30)


async def test_does_not_retry_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """**재시도하지 않는다** — §2.9(c) 의 재시도 1회는 I-1 전용 예외다.

    그 예산은 이미 first-token 상한을 압박하고 있어(#277 실측: 재시도가 이벤트 0건·504 를
    8/8 재현) 새 호출에 예외를 확대하지 않는다. 이 테스트가 없으면 나중에 누가
    `search_products` 의 `attempts` 루프를 "일관성" 명목으로 복사해 와도 드러나지 않는다.
    """
    seen = _mock_client(monkeypatch, httpx.TimeoutException("timeout"))

    with pytest.raises(SpringUnavailableError):
        await get_popular_products(size=30)

    assert len(seen) == 1
