"""헬스 체크 + /chat SSE 스텁 스모크 테스트 (dev 인증 모드).

스캐폴드가 부팅하고 CH-2 명명 계약대로 스트리밍하는지 검증한다:
  - GET /health == 200
  - POST /chat 가 text/event-stream 을 스트리밍하고 done 이벤트로 종료
  - SSE 이벤트명·필드가 api-spec v0.4.0 §3.1 과 일치 (camelCase, 6-event 세트)
  - [HARD] SSE 는 상품 카드를 싣지 않는다 (경로 B): products.ready 는 {sessionId, listIds} 상관키만
  - 구 프로필 HTTP 표면은 등록되지 않는다
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok() -> None:
    """GET /health 는 200 과 status=ok 를 반환한다."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_head_ok() -> None:
    """[#574] HEAD /health 는 200 이다.

    FastAPI 의 `APIRoute` 는 Starlette 의 순수 `Route` 와 달리 GET 등록 시 HEAD 를 자동으로
    붙이지 않는다 — `@app.get` 만 쓰면 HEAD 가 **405** 로 떨어진다. 외부 업타임 모니터가
    HEAD 만 보낼 수 있어 서비스가 살아 있어도 다운으로 보고됐다. 405 회귀를 여기서 막는다.
    """
    resp = client.head("/health")
    assert resp.status_code == 200


def test_health_head_has_no_body() -> None:
    """[#574] HEAD 응답은 HTTP 규약대로 본문을 싣지 않는다."""
    resp = client.head("/health")
    assert resp.content == b""


def _parse_sse(body: str) -> list[dict]:
    """SSE 본문에서 `data:` 라인의 JSON 이벤트를 순서대로 파싱한다."""
    events: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def test_chat_streams_sse_ending_with_done(buyer_fakes) -> None:
    """POST /chat 는 실 buyer 그래프를 SSE 로 스트리밍하고 done 으로 끝난다 (fake LLM/검색/push)."""
    resp = client.post(
        "/chat",
        json={"sessionId": "sess-1", "threadId": "thread-1", "message": "무선 이어폰 추천해줘"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]

    # 순서·유일성 계약 (api-spec §3.1, 경로 B): conditions·products.ready 각 1회, done 마지막.
    assert types.count("conditions") == 1
    assert types.count("products.ready") == 1
    assert types.count("done") == 1
    assert types[-1] == "done"
    assert types.index("conditions") < types.index("products.ready") < types.index("done")

    # [HARD] 스트림 어디에도 상품 카드가 없다 (경로 B) — 카드 필드 부재.
    assert "products" not in types  # 구 카드 이벤트명 폐기
    for ev in events:
        data = ev["data"]
        assert "price" not in data
        assert "rationale" not in data
        assert "items" not in data  # 카드 목록 없음

    # products.ready 는 상관관계 키만 (camelCase): sessionId + 비어있지 않은 listIds.
    ready = next(e for e in events if e["type"] == "products.ready")["data"]
    assert set(ready.keys()) == {"sessionId", "listIds"}
    assert ready["sessionId"] == "sess-1"
    assert len(ready["listIds"]) == 1

    # conditions 는 chips 배열, 카테고리 칩이 먼저.
    conditions = next(e for e in events if e["type"] == "conditions")["data"]
    assert isinstance(conditions["chips"], list)
    assert conditions["chips"][0]["field"] == "category"

    # done.finishReason == "stop" (camelCase).
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "stop"


def test_seller_chat_requires_seller_scope() -> None:
    """판매자 스코프 없는 토큰(dev 게스트)의 /seller/chat 은 403 이다 (api-spec §3.2)."""
    resp = client.post(
        "/seller/chat",
        json={"sessionId": "sess-1", "threadId": "thread-1", "message": "이번 주 매출 어때?"},
    )
    assert resp.status_code == 403


def test_events_catalog_is_post_mvp_404() -> None:
    """POST /events/catalog 는 고도화(post-MVP)로 미등록 → 404 (MVP 표면 축소)."""
    resp = client.post(
        "/events/catalog",
        json={"eventId": "evt-1", "changeType": "priceStock", "productId": "P-1"},
    )
    assert resp.status_code == 404


def test_openapi_surface_is_exactly_mvp() -> None:
    """OpenAPI 표면이 정확히 MVP 엔드포인트 집합인지 확인."""
    paths = set(app.openapi()["paths"].keys())
    assert paths == {
        "/chat",
        "/seller/chat",
        # [#599] R-1/R-2 판매자 분석 보고서 조회 — GET 목록(reports)·상세(reports/{report_id}).
        # SSE 레인(`/seller/chat`)과 별개로, 배치가 저장해 둔 보고서를 조회 전용으로 읽는다.
        "/seller/reports",
        "/seller/reports/{report_id}",
        "/health",
        "/events/session-end",
        "/events/session-claim",
        # [#148] I-22 홈 추천 랭킹 — Spring → AI 위임(레인 b, api-spec §3.7).
        # `/events/*` 와 같은 서비스 토큰 레인이지만 통지가 아니라 동기 요청/응답이다.
        "/internal/recommendations/home",
        # [#601] 무인 판매자 분석 수동 실행 — 스캔 게이트 우회, 202 + 백그라운드 실행
        # (`daily_batch.run_manual_analysis`). 데모·재현 용도, 결과는 /seller/reports 로 조회.
        "/internal/seller/{brand_id}/analysis/run",
        # [#360] 마이페이지 취향 관리 I-32~I-37 — 같은 레인, `M-11`~`M-16` 의 internal 판
        # (api-spec §3.8·§3.9). 조회 1 + 변경 4 이고 I-35(되돌리기)는 2026-08-07 폐기됐다.
        # 자리표시자 이름이 정본의 `{userId}`·`{edgeId}` 와 다른 것은 **드리프트가 아니다** —
        # 실제 URL 은 숫자·식별자라 와이어가 같고, 이름은 파이썬 인자명 규약을 따른다.
        "/internal/profile/{user_id}/graph",
        "/internal/profile/{user_id}/graph/edges/{edge_id}",
        "/internal/profile/{user_id}/graph/reset",
        "/internal/profile/{user_id}/personalization",
    }
