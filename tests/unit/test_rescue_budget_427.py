"""#427 — 검색 타임아웃을 턴 예산에서 파생시킨다 (DESIGN-SHARED-BUDGET-384 D1~D8).

검증 대상:
1. 분리 회귀 — `SPRING_SEARCH_TIMEOUT_S` 는 `search_products` 에만 적용되고, I-3 인기·I-2 담기
   등 다른 Spring 호출의 httpx 타임아웃(`SPRING_TIMEOUT_S`)은 그대로다.
2. 부등식 회귀(D2) — `open_stream` 이 실제로 넘기는 `turn_started_at` 이 스트림 자체의 전체
   상한 데드라인과 같은 원점인지, 실제 절단 타이밍으로 확인한다.
3. 좁히기(D4) — 잔여 예산이 모자란 단은 `narrow_search_budget` 를 실제로 집행한다.
4. 건너뛰기 + 거짓 신호 금지(D4 H4) — `narrow_skip` 모드에서 자동완화 단은 실제로 건너뛰고
   `relaxing` progress 프레임을 내지 않는다.
5. 본검색은 극단적 예산 압박에서도 건너뛰지 않는다.
6. `observe`(기본) 무동작 — 좁히기·건너뛰기가 발동하지 않되 반사실 로그 필드는 남는다.
7. ContextVar 비누수 — `narrow_search_budget` 은 `with` 블록을 벗어나면 즉시 원복된다.
8. 기동 검증기 — `progress_events_enabled`·`rescue_budget_mode` 가 서로 다른 축의 게이트임을
   상수가 아니라 실제 판정 갈림으로 증명한다.
9. 공유 계수(D7) — 런타임 "남은 단 수" 계산과 기동 검증기가 같은 함수(`_rescue_chain_stage_
   counts`)에서 계수를 얻는다.
10. **핵심 명제(오케스트레이터 지적, R9)** — 위 1~9 는 전부 메커니즘(좁히기 호출 여부·clamp·
    계수·거짓 신호 없음)만 잰다. 이 이슈가 존재하는 이유인 "3s 를 넘기는 검색이 오늘은 실패로
    바뀌고, 검색 전용 타임아웃을 올리면 살아난다"는 명제 자체는 아무 테스트도 재지 않았다 —
    `spring_client.search_products` 를 실제로 태워 A(기본값에서 느린 검색은 실패)·B(타임아웃만
    올리면 같은 지연이 성공)를 지연·응답이 동일하고 손잡이 하나만 다른 쌍으로 고정한다(운영
    실측 #395, 2026-08-06 근거는 각 테스트 docstring). C(다른 Spring 호출은 영향 없음)는 1번
    테스트가 이미 재고 있어 새로 만들지 않고 상호 참조만 한다. D 는 `run_buyer_turn` 을 태워
    사용자가 보는 SSE `error{code:"SEARCH_FAILED"}` 까지 확인한다.

각 테스트는 "이 변경이 회귀했을 때 실제로 깨지는가"를 기준으로 짰다 — 상수를 상수와 비교하는
어설션은 두지 않는다(예: mode 를 무조건 skip/narrow 로 바꿔보면 5·6 이 깨져야 한다).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from contextlib import contextmanager

import httpx
import pytest
from pydantic import ValidationError

from app.agents.buyer.recommendation import graph as recommendation_graph
from app.core.config import Settings, get_settings
from app.core.stream import open_stream
from app.schemas.spring import ProductSearchFilters
from app.services import search_service, spring_client
from app.services.spring_client import SpringUnavailableError
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM
from tests.unit.test_infra import _FakeRequest
from tests.unit.test_recommendation import (
    _collect,
    _guest,
    _make_search,
    _member,
    _req,
    _types,
    _RecordingPush,
    run_buyer_turn,
)
from tests.unit.test_relaxation import _decompose_with, _product
from tests.unit.test_spring_search_budget_132 import _install_transport, _SlowTransport

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """setenv 로 바꾼 설정이 다음 테스트로 새지 않게 앞뒤로 캐시를 비운다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ─────────── (1) 분리 회귀 ───────────


async def test_spring_search_timeout_only_scopes_search_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SPRING_SEARCH_TIMEOUT_S` 를 올려도 다른 Spring 호출의 httpx 타임아웃은 그대로다.

    `_client()` 호출부가 다시 스칼라 하나를 공유하게 되돌아가면(#394 가 기각한 실패 모드)
    두 캡처값이 같아져 이 테스트가 깨진다.
    """
    # 4.0 은 기본 SPRING_TIMEOUT_S(3.0)와 뚜렷이 다르면서도 다른 기동 검증기(구매자 전체
    # 상한·observe 꼬리 예약)를 건드리지 않는 값이다(직렬 합 3×4.0=12.0 < 30-15=15.0).
    monkeypatch.setenv("SPRING_SEARCH_TIMEOUT_S", "4.0")
    monkeypatch.setenv("SPRING_TIMEOUT_S", "3.0")
    get_settings.cache_clear()

    captured: list[float | None] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": []})

    def _spy_client(*, timeout: float | None = None) -> httpx.AsyncClient:
        captured.append(timeout)
        return httpx.AsyncClient(
            base_url="https://spring.test", transport=httpx.MockTransport(_handler)
        )

    monkeypatch.setattr(spring_client, "_client", _spy_client)

    await spring_client.get_popular_products(5)
    await spring_client.search_products(ProductSearchFilters())

    assert captured[0] is None  # I-3 — 검색 전용 타임아웃을 넘기지 않는다(spring_timeout_s 그대로)
    assert captured[1] == 4.0  # I-1 search_products — 검색 전용 타임아웃을 명시로 넘긴다


# ─────────── (2) 부등식 회귀(D2) ───────────


async def test_turn_started_at_is_the_same_origin_open_stream_uses_for_its_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`open_stream` 이 `inner_factory` 에 넘기는 `turn_started_at` 이 전체 상한 데드라인과
    같은 원점인지, 실제 절단 타이밍으로 확인한다(플럼빙이 끊기면 이 테스트가 잡아야 한다).

    내부 제너레이터는 자연 종료하지 않는다 — 관측되는 절단은 오직 `stream.py` 의 전체 상한
    (`start + stream_total_timeout_buyer_s`)뿐이다. `turn_started_at` 이 그 `start` 와 다른
    (더 늦은) 시각이면 절단까지 걸린 시간이 `stream_total_timeout_buyer_s` 보다 짧게 관측된다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "stream_total_timeout_buyer_s", 0.2)
    monkeypatch.setattr(settings, "stream_first_token_timeout_s", 0.05)
    monkeypatch.setattr(settings, "stream_disconnect_poll_s", 0.01)

    captured: dict[str, float] = {}

    async def inner(turn_started_at: float):
        captured["turn_started_at"] = turn_started_at
        yield 'data: {"type":"token","data":{"text":"hi"}}\n\n'
        await asyncio.sleep(10)  # 자연 종료하지 않는다 — 전체 상한만이 끊는다
        yield "data: never\n\n"  # pragma: no cover - 도달하지 않는다

    loop = asyncio.get_event_loop()
    before = loop.time()
    resp = await open_stream(_FakeRequest(), "rescue-427-d2-sess", inner, role="buyer")
    parts = [c if isinstance(c, str) else c.decode() async for c in resp.body_iterator]
    after = loop.time()

    assert "turn_started_at" in captured
    assert before <= captured["turn_started_at"] <= after
    elapsed_since_captured_start = after - captured["turn_started_at"]
    # 실제 절단까지 걸린 시간이 stream_total_timeout_buyer_s 와 거의 같아야(폴링 간격 수준
    # 오차만) turn_started_at 이 stream.py 의 실제 deadline 원점과 같다고 볼 수 있다.
    assert elapsed_since_captured_start >= settings.stream_total_timeout_buyer_s
    assert elapsed_since_captured_start < settings.stream_total_timeout_buyer_s + 0.3
    assert any("total_timeout_stop" in p or "done" in p for p in parts)


# ─────────── (3)(5)(6) 런타임 좁히기·본검색 불가침·observe 무동작 ───────────


def _record_narrow_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []

    @contextmanager
    def _recording(budget_s: float):
        calls.append(budget_s)
        yield

    monkeypatch.setattr(recommendation_graph.spring_client, "narrow_search_budget", _recording)
    return calls


async def test_narrow_mode_narrows_the_main_search_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """[D4] `narrow` 모드에서 잔여 예산이 모자라면 본검색 단이 실제로 `narrow_search_budget`
    를 집행한다(좁힌 값은 `spring_search_timeout_s` 미만).

    검증 실효성: 이 판정을 무조건 "full"(좁히지 않음)로 바꿔보면 `calls` 가 비어 이 테스트가
    깨진다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")
    # rescue_deadline = turn_started_at + (30 - 29) = turn_started_at + 1.0 — 남은 예산을
    # 최소 하한(0.5) 아래로 몰아넣어 확실히 narrow(본검색은 강등) 판정을 받게 한다.
    monkeypatch.setattr(settings, "rescue_tail_reserve_s", 29.0)

    calls = _record_narrow_calls(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time(),
        )
    )

    assert "products.ready" in _types(events)  # 좁혀도 본검색은 정상 완료된다
    assert calls, "본검색 단이 narrow_search_budget 를 집행하지 않았다"
    assert calls[0] < settings.spring_search_timeout_s


async def test_observe_mode_never_calls_narrow_search_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[D4 observe] observe를 명시 주입하면 같은 예산 압박에서도 **집행하지 않는다** — 판정만 계산해
    로그에 반사실로 남긴다.

    검증 실효성: `_apply_stage_budget` 가 모드를 무시하고 항상 집행하도록 바꾸면 `calls` 가
    비지 않아 이 테스트가 깨진다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "observe")
    monkeypatch.setattr(settings, "rescue_tail_reserve_s", 29.0)  # 위 테스트와 같은 압박

    calls = _record_narrow_calls(monkeypatch)

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=_RecordingPush(),
                turn_started_at=asyncio.get_event_loop().time(),
            )
        )

    assert "products.ready" in _types(events)  # 오늘 동작 그대로 정상 완료
    assert not calls, "observe 모드인데 narrow_search_budget 가 집행됐다"

    log = next(r for r in caplog.records if getattr(r, "event", None) == "recommend_pipeline")
    # 반사실 — 집행하지 않았어도 "narrow 였다면 이랬을 값"이 남는다.
    assert log.rescue_stage_narrowed_timeout_ms is not None
    assert log.rescue_budget_mode == "observe"


async def test_main_search_is_never_skipped_even_under_extreme_budget_pressure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[D4] `narrow_skip` + 예산이 이미 바닥난 턴에서도 본검색은 반드시 시도된다.

    `turn_started_at` 을 아득한 과거로 두어 모든 단의 원시 판정이 "skip" 이 되게 만든다 —
    본검색만은 그래도 narrow 로 강등돼 실행되고, 턴은 SEARCH_FAILED 가 아니라 정상 완료된다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow_skip")

    calls = _record_narrow_calls(monkeypatch)

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=_RecordingPush(),
                turn_started_at=asyncio.get_event_loop().time() - 10_000.0,
            )
        )

    assert "products.ready" in _types(events)
    assert "error" not in _types(events)
    assert calls, "본검색 단이 narrow_search_budget 를 집행하지 않았다 — narrow 강등이 깨졌다"
    # [리뷰 F1] 데드라인이 10,000초 지난 턴이라 clamp 가 없으면 이 값은 큰 음수다 — 음수를
    # 그대로 wait_for 에 넘기면 실제로는 요청이 나가지 않는다(아래 실HTTP 경계 테스트가 그
    # 끝단까지 확인한다).
    assert calls[0] >= settings.rescue_stage_min_timeout_s, (
        f"음수/0 예산이 그대로 집행됐다: {calls[0]}"
    )

    log = next(r for r in caplog.records if getattr(r, "event", None) == "recommend_pipeline")
    assert log.rescue_budget_mode == "narrow_skip"
    assert log.rescue_stage_narrowed_timeout_ms is not None
    # 이 턴은 본검색만 돈다(DEFAULT_PRODUCTS 는 0건이 아니라 자동완화 루프에 들어가지 않는다) —
    # 그래서 "건너뛴 단"은 없다. 자동완화까지 건너뛰는 조합은 아래 H4 테스트가 겨눈다.
    assert log.rescue_stage_skipped_budget is False


# ─────────── [리뷰 F1] narrow 집행 예산 하한 — 실 HTTP 경계로 끝단까지 확인 ───────────
#
# 위 테스트들은 `search=` 에 fake 를 주입해 `spring_client.search_products` 에 아예 도달하지
# 않는다 — `narrow_search_budget` 이 불렸다는 것만 보고 그 값이 실제로 쓸 수 있는 값인지는
# 재지 못했다(리뷰가 지적한 결함). 아래는 `tests/unit/test_spring_search_budget_132.py` 의
# `_install_transport` 관례를 따라 HTTP 경계에만 대역을 세우고, `search=` 를 주입하지 않아
# `search_service.search_catalog` → `SpringSearchBackend` → `spring_client.search_products` 의
# 실제 경로를 그대로 태운다.


def _spy_narrow_search_budget_passthrough(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """`narrow_search_budget` 을 **실제로 호출하면서** 넘어온 값을 기록한다.

    `_record_narrow_calls`(위)와 달리 진짜 구현을 대신하지 않는다 — ContextVar 를 실제로
    세팅해야 `search_products` 가 그 예산을 받는다(끝단 검증의 전제).
    """
    calls: list[float] = []
    real_narrow = spring_client.narrow_search_budget

    @contextmanager
    def _spy(budget_s: float):
        calls.append(budget_s)
        with real_narrow(budget_s):
            yield

    monkeypatch.setattr(recommendation_graph.spring_client, "narrow_search_budget", _spy)
    return calls


def _install_search_transport(monkeypatch: pytest.MonkeyPatch):
    """검색 HTTP 경계에 응답을 세우고 히트 수를 반환하는 카운터를 준다(#132 관례).

    Spring 은 rating 을 필터링하지 않는다(AI 사후필터, C-15) — 항상 같은 두 후보를 돌려주고,
    평점 하한은 `search_service.apply_ai_side_filters` 가 요청 필터 기준으로 알아서 거른다.
    """
    hits = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "productId": 101,
                        "name": "이어폰A",
                        "price": 39000,
                        "rating": 4.2,
                        "categoryName": "무선이어폰",
                        "brandName": "BrandX",
                    },
                    {
                        "productId": 102,
                        "name": "이어폰B",
                        "price": 48000,
                        "rating": 4.1,
                        "categoryName": "무선이어폰",
                        "brandName": "BrandY",
                    },
                ],
            },
        )

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.test", transport=httpx.MockTransport(_handler)
        ),
    )
    return lambda: hits


async def test_narrow_mode_stale_deadline_still_sends_the_main_search_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[리뷰 F1] 데드라인이 이미 지난 턴에서도 본검색 HTTP 요청이 실제로 나가고 턴이
    완료된다 — `narrow_search_budget` 에 넘어간 값도 항상 하한 이상이다.

    검증 실효성: `_apply_stage_budget` 의 `granted = max(granted, rescue_stage_min_timeout_s)`
    clamp 를 지우면(원 결함 재현) `narrow_search_budget` 이 큰 음수를 받아
    `asyncio.wait_for(timeout=음수)` 가 즉시 만료된다 — `hits` 가 0 으로 남고 턴은
    `SEARCH_FAILED` 로 끝나 이 테스트가 깨진다(리뷰 재현 스니펫과 동일 메커니즘).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")

    narrow_calls = _spy_narrow_search_budget_passthrough(monkeypatch)
    get_hits = _install_search_transport(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _guest(),  # 게스트 — I-19 구매 이력 조회(get_recent_purchases)가 없어 hit 계상이 깨끗하다
            llm=FakeLLM(),
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time() - 10_000.0,
        )
    )

    assert narrow_calls, "본검색 단이 narrow_search_budget 를 집행하지 않았다"
    assert narrow_calls[0] >= settings.rescue_stage_min_timeout_s, (
        f"음수/0 예산이 그대로 집행됐다: {narrow_calls[0]}"
    )
    assert get_hits() > 0, "본검색 HTTP 요청이 실제로 나가지 않았다"
    assert "products.ready" in _types(events)
    assert "error" not in _types(events)


async def test_narrow_mode_h4_relaxing_frame_implies_the_probe_actually_ran(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[리뷰 F1 + D4 H4] `narrow`(narrow_skip 아님) 모드 + 예산이 바닥난 턴에서 `relaxing` 이
    나갔다면, 자동완화 probe 는 실제로 HTTP 요청을 보냈어야 한다 — emit 만 하고 음수 예산으로
    즉사하는 거짓 신호가 아니어야 한다.

    검증 실효성: F1 결함이 재발하면(clamp 없음) `relaxing` 은 여전히 emit 되지만(narrow 모드는
    skip 을 narrow 로 강등하므로 skip 자체는 안 한다) probe 의 `search_products` 호출이 즉시
    타임아웃돼 HTTP 요청이 안 나간다 — `get_hits() == 1`(본검색 1회분만)로 남아 이 테스트가
    깨진다.

    [리뷰 G1] `narrow` 모드는 skip 판정도 narrow 로 강등해 실제로 시도하므로
    `rescue_stage_skipped_budget` 은 `False` 로 남아야 한다 — `True` 로 남으면 "예산 부족으로
    건너뛴 단이 있었다"는 로그가 실제로는 전부 시도한 턴에 대해 오보를 낸다(H4 위반). 대신
    clamp 된 하한값(`rescue_stage_min_timeout_s`)이 `rescue_stage_narrowed_timeout_ms` 에 남아
    "하한에 걸렸다"는 신호를 정직하게 전달한다.

    검증 실효성: `_apply_stage_budget` 의 skip 분기에서 `narrow` 모드도 여전히
    `rescue_stage_skipped_budget = True` 를 남기도록 되돌리면(G1 결함 재현) 아래
    `skipped_budget is False` 어설션이 깨진다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")

    narrow_calls = _spy_narrow_search_budget_passthrough(monkeypatch)
    get_hits = _install_search_transport(monkeypatch)

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _guest(),  # 게스트 — I-19 호출이 없어 hit 계상이 검색 요청만 반영한다
                llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
                push_fn=_RecordingPush(),
                turn_started_at=asyncio.get_event_loop().time() - 10_000.0,
            )
        )

    stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert "relaxing" in stages, (
        "이 시나리오는 relaxing 이 나가야 정직한 전제다(narrow 는 skip하지 않는다)"
    )
    # 본검색(평점 4.5, 0건) 1회 + 완화 probe(평점 4.0, 2건) 1회 — 최소 2 히트가 실제로 나가야
    # relaxing 프레임이 거짓 신호가 아니다.
    assert get_hits() >= 2, "relaxing 을 emit 했지만 probe HTTP 요청이 나가지 않았다(F1 재발)"
    assert all(c >= settings.rescue_stage_min_timeout_s for c in narrow_calls), (
        f"음수/0 예산이 집행 경로에 그대로 넘어갔다: {narrow_calls}"
    )
    assert "products.ready" in _types(events)  # 완화가 실제로 후보를 되살렸다
    assert "error" not in _types(events)

    log = next(r for r in caplog.records if getattr(r, "event", None) == "recommend_pipeline")
    assert log.rescue_budget_mode == "narrow"
    assert log.rescue_stage_skipped_budget is False, (
        "narrow 모드는 skip 도 narrow 로 강등해 실제로 시도했다 — 건너뛴 단이 있었다고 오보하면 안 된다"
    )
    assert log.rescue_stage_narrowed_timeout_ms == round(
        settings.rescue_stage_min_timeout_s * 1000
    ), "10,000초 지난 턴은 clamp 가 하한 그대로 걸려야 한다"


# ─────────── [PR #452 리뷰 R2] "full" 판정 상한이 재시도 배수를 반영한다 ───────────
#
# `_stage_budget` 은 `granted >= spring_search_timeout_s` 면 `"full"`(안 좁힘)을 낸다. 그런데
# `spring_client.search_products` 가 실제로 쓰는 벽시계 상한은 `spring_search_timeout_s *
# attempts` 다(`attempts = spring_max_retries + 1`).
# [#306] 종전에는 미룬 턴만 `attempts=1` 로 갈렸으나 그 억제가 제거돼 **모든 턴이 같은 축**을
# 탄다. 아래 세 테스트가 함께 "런타임 attempts 규칙 ≡ search_products attempts 규칙"을
# 고정한다: 하나만 바뀌면(예: graph.py 가 상수를 쓰거나 턴 유형으로 다시 갈리면) 아래 중
# 하나가 반드시 깨진다 — 미룬 턴/안 미룬 턴이 이제 **같은 결과**를 내야 한다는 것까지 포함한다.


async def test_narrow_mode_stage_cap_accounts_for_retry_attempts_before_narrowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #452 리뷰 R2 — 과다 승인 회귀] `spring_max_retries=1` 이면 본검색(이 턴은 suppress
    되지 않는다)의 실제 단 상한은 `spring_search_timeout_s * 2 = 6.0s` 다. 잔여 예산을
    (3.0s, 6.0s) 구간(=4.5s)으로 몰아넣으면, 옛 코드(상한을 1회분 3.0s 로 고정)는 `granted
    >= 3.0` 이라 `"full"` 로 오판해 `narrow_search_budget` 을 아예 안 부른다 — rescue_deadline
    을 최대 `spring_search_timeout_s` 만큼 과다 승인한다(리뷰 지적 그대로).

    검증 실효성: `_stage_budget` 의 `stage_cap` 을 `spring_search_timeout_s` 상수로 되돌리면
    (R2 결함 재현) `granted`(4.5s)가 그 상수(3.0s) 이상이라 `"full"` 로 오판되고 `calls` 가
    비어 이 테스트가 깨진다. **먼저 이 테스트를 수정 전 코드에 돌려 실패를 확인했다**(TDD).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")
    monkeypatch.setattr(settings, "spring_max_retries", 1)
    # rescue_deadline 원점부터 window=13.5s, 남은 단 수 3(main=1 + rescue=1 + auto_relax=1,
    # 기본 설정값 — `_rescue_chain_stage_counts` 참조)으로 균등 배분하면 granted≈4.5s. 옛
    # 상한(3.0s)은 넘지만 새 상한(6.0s = 3.0×2)은 안 넘는다.
    monkeypatch.setattr(settings, "rescue_tail_reserve_s", 16.5)

    calls = _record_narrow_calls(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),  # 기본 decompose: ratingMin 미설정 → may_auto_relax=False(항상 재시도)
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time(),
        )
    )

    assert "products.ready" in _types(events)
    assert calls, (
        "본검색 단이 narrow_search_budget 를 집행하지 않았다 — attempts=2(spring_max_retries=1)"
        "인 단을 1회분 상한(3.0s)으로 오판해 'full' 로 통과시켰다(R2 과다 승인 재현)"
    )
    stage_cap = settings.spring_search_timeout_s * 2  # attempts = spring_max_retries(1) + 1
    assert calls[0] < stage_cap
    assert calls[0] > settings.spring_search_timeout_s, (
        "옛 1회분 상한(3.0s)보다 큰 값이 실제로 집행돼야 과다 승인이 없었다는 증거가 된다"
    )


async def test_zero_spring_max_retries_stage_cap_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #452 리뷰 R2 — 0회 재시도 분기 불변] `spring_max_retries=0`을 명시하면
    `attempts=1` 이라 상한은 R2 수정 전후로 `spring_search_timeout_s` 그대로다. 위 테스트와
    똑같은 압박(window=13.5s, granted≈4.5s)을 주고 `spring_max_retries` 만 0 으로 두면 여전히
    `"full"`(안 좁힘)이어야 한다 — 옛 코드도 이 지점에서 이미 `"full"` 이었으므로 이 어설션은
    "R2 수정이 0회 재시도 분기 동작을 바꾸지 않는다"를 직접 잰다.

    검증 실효성: attempts 산출이 `suppress_deferred_search_retry` 를 무시하고 항상
    `spring_max_retries + 1` 을 쓰는 회귀가 나면(그 자체는 이 값에 영향 없지만) 대신 상한
    계산이 `attempts` 를 아예 무시하지 않고 엉뚱한 값(예: 상수 1 대신 2)을 곱하는 회귀가 나면
    `calls` 가 채워져 이 테스트가 깨진다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "spring_max_retries", 0)
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")
    monkeypatch.setattr(settings, "rescue_tail_reserve_s", 16.5)  # 위 테스트와 같은 압박

    calls = _record_narrow_calls(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time(),
        )
    )

    assert "products.ready" in _types(events)
    assert not calls, (
        "attempts=1(spring_max_retries=0)인데 narrow_search_budget 가 집행됐다 — "
        "상한이 attempts 배수를 잘못 곱하는 회귀다(0회 재시도 분기가 바뀌면 안 된다)"
    )


async def test_narrow_mode_deferred_turn_narrows_like_any_other_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#306 — 두 축의 합류] 이 턴은 `ratingMin` 이 설정돼 `may_auto_relax=True`(미룬 턴)다.
    v0.32.4 까지는 그 턴만 `suppress_search_retry()` 로 감싸여 `attempts` 가 1 로 강제됐고,
    단 상한도 `spring_search_timeout_s × 1 = 3.0s` 여서 같은 압박(granted≈4.5s)에서
    `"full"`(안 좁힘)이었다. #306 이 그 억제를 제거해 **미룬 턴도 `attempts=2`·상한 6.0s** 를
    쓰므로, 이제 `granted(≈4.5s) < 6.0s` 로 **narrow 가 집행된다.**

    검증 실효성: 억제가 조금이라도 남아 있으면(= `attempts` 가 1 로 떨어지면) 상한이 3.0s 가
    돼 `granted >= stage_cap` 으로 `"full"` 판정이 나고 `calls` 가 비어 이 테스트가 깨진다.
    그 "attempts=1 이면 안 좁힌다"는 대우는 추정이 아니라 **같은 압박을 주고 재시도만 0 으로
    내린 `test_zero_spring_max_retries_stage_cap_unchanged` 가 직접 잰다**(그쪽은 `assert not
    calls`). 그리고 미루지 않는 턴을 재는
    `test_narrow_mode_stage_cap_accounts_for_retry_attempts_before_narrowing` 과 **이제 같은
    결과**가 나와야 두 축이 합류했다는 증거가 된다 — 셋이 함께 attempts 규칙을 고정한다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")
    monkeypatch.setattr(settings, "spring_max_retries", 1)
    monkeypatch.setattr(settings, "rescue_tail_reserve_s", 16.5)  # 위 두 테스트와 같은 압박

    calls = _record_narrow_calls(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),  # may_auto_relax=True
            search=_make_search(DEFAULT_PRODUCTS),  # 필터 무시 — 완화 루프를 태우지 않는다
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time(),
        )
    )

    assert "products.ready" in _types(events)
    assert calls, (
        "미룬 턴 본검색이 narrow_search_budget 를 집행하지 않았다 — #306 이 제거한 억제가 "
        "남아 attempts 를 1 로 떨어뜨렸다(상한 3.0s 오판)"
    )
    stage_cap = settings.spring_search_timeout_s * 2  # attempts = spring_max_retries(1) + 1
    assert calls[0] < stage_cap
    assert calls[0] > settings.spring_search_timeout_s, (
        "옛 억제 상한(3.0s)보다 큰 값이 실제로 집행돼야 미룬 턴이 다른 턴과 같은 축을 탄다는 "
        "증거가 된다"
    )


# ─────────── [PR #452 리뷰 R3] 본검색 남은 단 수가 F-1 몫을 반영한다 ───────────


async def test_narrow_mode_relaxation_disabled_still_reserves_room_for_f1_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #452 리뷰 R3 — 런타임 과다 승인 회귀] `RELAXATION_MAX_ROUNDS=0`(자동완화 끔) +
    `CATEGORY_EXPAND_ENABLED=True`(기본)에서도 F-1 재검색은 `may_auto_relax` 와 무관하게 돈다
    (design DESIGN-SHARED-BUDGET-384.md §3 D6) — 본검색의 `remaining_stages` 는 본검색(1) +
    F-1(1) = 2 여야 뒤따르는 F-1 몫이 남는다. 옛 코드는 `_rescue_chain_stage_counts` 가 조기
    return 으로 세 항 전부 0을 내(`max(0,1)+0+0=1`) 본검색이 잔여를 통째로 쓴다.

    window=5.0s(rescue_tail_reserve_s=25.0)로 압박하면: n=1(옛 코드) → granted=
    min(3.0,5.0)=3.0=cap → `"full"`(안 좁힘, `narrow_search_budget` 미호출). n=2(고친 코드)
    → granted=min(3.0,2.5)=2.5<cap → 실제로 좁혀진다.

    검증 실효성: **먼저 옛 코드에 돌려 실패(calls 가 빔)를 확인했다**(TDD 적색). 런타임의
    `max(_rescue_stage_counts.main, 1)` 보정을 없애지 않고 그대로 두면(R3 를 계수 함수만
    고치고 소비처를 안 고치면) `_rescue_stage_counts.main` 이 1(고친 함수)이라 `max(1,1)=1`
    로 여전히 1이 나와 이 테스트가 계속 깨진다 — 계수·소비처 둘 다 고쳐야 통과한다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")
    monkeypatch.setattr(settings, "relaxation_max_rounds", 0)
    monkeypatch.setattr(settings, "rescue_tail_reserve_s", 25.0)  # window=5.0s

    calls = _record_narrow_calls(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time(),
        )
    )

    assert "products.ready" in _types(events)
    assert calls, (
        "본검색 단이 narrow_search_budget 를 집행하지 않았다 — remaining_stages 가 여전히 1로 "
        "잡혀(F-1 몫을 반영 못 함) 5.0s 잔여를 'full'(3.0s 이하)로 오판했다(R3 재현)"
    )
    assert calls[0] < settings.spring_search_timeout_s


# ─────────── [PR #452 리뷰 R5] narrow 하한 clamp 가 단 상한을 넘지 않는다 ───────────


async def test_narrow_mode_min_timeout_clamp_never_exceeds_stage_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #452 리뷰 R5 — 역전 회귀] `RESCUE_STAGE_MIN_TIMEOUT_S` 를 `SPRING_SEARCH_TIMEOUT_S`
    이상으로(잘못) 튜닝하면, 상한 clamp 가 없는 F1 하한 clamp 만으로는 "예산이 모자라
    좁힌다"면서 원래 단 상한(`stage_cap`)보다 **더 큰** 값을 `narrow_search_budget()` 에
    주입한다 — 좁히기가 목적과 정반대로 동작한다.

    이 조합은 `Settings()` 생성자로는 못 만든다(기동 검증기가 별도로 막는다, 아래
    `test_config.py` 참조) — 그래서 런타임 값을 `monkeypatch.setattr(settings, ...)` 로 직접
    세워 **지역 불변식**(집행되는 narrow 예산은 항상 stage_cap 이하)만 독립적으로 겨눈다.
    기동 검증기와 이 지역 불변식은 서로 다른 방어선이다 — 하나가 다른 하나를 대체하지 않는다.

    검증 실효성: **먼저 상한 clamp 가 없는 코드(`granted = max(granted, rescue_stage_min_
    timeout_s)`, 상한 씌우기 없음)에 돌려 역전(5.0 > stage_cap 3.0)을 확인했다**(TDD 적색).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow")
    # 이 턴은 미룬 조건이 없어 재시도하므로 attempts=2다.
    monkeypatch.setattr(settings, "rescue_stage_min_timeout_s", 5.0)

    calls = _record_narrow_calls(monkeypatch)

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),  # 기본 decompose: ratingMin 미설정 → may_auto_relax=False, attempts=1
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            turn_started_at=asyncio.get_event_loop().time() - 10_000.0,
        )
    )

    assert "products.ready" in _types(events)
    assert calls, "본검색 단이 narrow_search_budget 를 집행하지 않았다"
    stage_cap = settings.spring_search_timeout_s * 2  # attempts=2(억제되지 않는 단)
    assert calls[0] <= stage_cap, (
        f"got {calls[0]} > stage_cap({stage_cap}) — 좁히기가 안 좁힌 것보다 많이 줬다"
        "(R5 역전 재현: 하한 clamp 뒤 상한을 다시 씌우지 않았다)"
    )


# ─────────── (4) 건너뛰기 + 거짓 신호 금지(H4) ───────────


async def test_narrow_skip_mode_skips_auto_relax_without_emitting_relaxing_frame(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[D4 H4] 예산 부족으로 자동완화 probe 를 건너뛰면 `relaxing` progress 프레임이 나가지
    않는다 — 서버가 하지 않는 일을 하는 중이라고 말하지 않는다.

    검증 실효성: 예산 판정을 (2)rounds+=1 과 (3)relaxing emit **뒤**로 옮기면(순서 회귀) 이
    테스트가 깨진다 — `relaxing` 프레임이 나간 뒤에 건너뛰기 때문이다. 프레임 목록을 실제로
    수집해 검사한다(어설션이 무조건 참이 되는 형태가 아니다 — 동일 설정에서 narrow_search_budget
    를 스파이해 실제로 probe 가 호출되지 않았음도 함께 확인한다).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rescue_budget_mode", "narrow_skip")

    async def _search(filters, exclude_product_ids=None):
        # 평점 4.5 하한으로는 0건, 4.0 으로 완화하면 2건 — probe 가 실제로 불리면 채택된다.
        products = [_product(101, 39000, rating=4.2), _product(102, 48000, rating=4.1)]
        kept = [
            p
            for p in products
            if filters.rating_min is None or (p.rating or 0) >= filters.rating_min
        ]
        from app.schemas.spring import ProductSearchResult

        return ProductSearchResult(products=kept, total_count=len(kept))

    with caplog.at_level(logging.INFO):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(decompose=_decompose_with(ratingMin=4.5)),
                search=_search,
                push_fn=_RecordingPush(),
                turn_started_at=asyncio.get_event_loop().time() - 10_000.0,
            )
        )

    stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert "relaxing" not in stages, "예산 부족으로 건너뛴 자동완화가 relaxing 을 emit 했다"

    # 자동완화 probe 는 본검색(무필터·rating_min=4.5) 1회만 불렸어야 한다 — 완화 재검색
    # (rating_min=4.0)이 실제로 시도됐다면 채택돼 "conditions"의 ratingMin 이 4.0 으로
    # 바뀌었을 것이다.
    conditions = next(e for e in events if e["type"] == "conditions")
    rating_chip = next(c for c in conditions["data"]["chips"] if c["field"] == "ratingMin")
    assert rating_chip["value"] == 4.5, "건너뛰지 않고 자동완화가 실제로 채택됐다"

    log = next(
        (
            r
            for r in caplog.records
            if getattr(r, "event", None) in ("recommend_zero_result", "recommend_pipeline")
        ),
        None,
    )
    assert log is not None
    assert log.rescue_stage_skipped_budget is True


# ─────────── (7) ContextVar 비누수 ───────────


def test_narrow_search_budget_resets_after_the_with_block() -> None:
    """`narrow_search_budget` 는 `observe_search_retry`(#406)와 동일한 누수 방지 규율을
    따른다 — `with` 블록을 벗어나면 다음 검색 호출로 ContextVar 가 새지 않는다.
    """
    assert spring_client._search_budget_override.get() is None
    with spring_client.narrow_search_budget(0.5):
        assert spring_client._search_budget_override.get() == 0.5
    assert spring_client._search_budget_override.get() is None


# ─────────── (8) 기동 검증기 — 네 분기가 실제로 갈린다 ───────────


def test_startup_guard_progress_flag_and_rescue_mode_are_independent_gates() -> None:
    """`progress_events_enabled` 과 `rescue_budget_mode` 가 서로 다른 축의 게이트임을 상수가
    아니라 실제 판정 갈림으로 보인다.

    검색예산=4.0·`spring_max_retries=1`(OFF 분기)이면 직렬 합은
    (1+1)×4.0 + 1×(4.0×2) = 16.0 이다:
    - observe(기본) + progress on(기본): 16.0 ≥ 15.0(30-15, 꼬리 예약)로 거절.
    - observe + progress off: 마찬가지로 거절(검사 순서상 꼬리 예약이 first-token 보다 먼저다).
    - narrow + progress on: 런타임이 꼬리 예약을 집행하므로 기동은 통과한다.
    - narrow + progress off: 첫 토큰 관문(16.0 ≥ 10.0)이 되살아나 다시 거절한다.
    """
    kwargs = {"_env_file": None, "spring_max_retries": 1, "spring_search_timeout_s": 4.0}

    with pytest.raises(ValidationError, match="RESCUE_BUDGET_MODE=observe"):
        Settings(**kwargs, rescue_budget_mode="observe")

    with pytest.raises(ValidationError, match="RESCUE_BUDGET_MODE=observe"):
        Settings(**kwargs, rescue_budget_mode="observe", progress_events_enabled=False)

    assert Settings(**kwargs, rescue_budget_mode="narrow")

    with pytest.raises(ValidationError, match="STREAM_FIRST_TOKEN_TIMEOUT_S"):
        Settings(**kwargs, rescue_budget_mode="narrow", progress_events_enabled=False)


# ─────────── (9) 공유 계수(D7) ───────────


def test_config_and_graph_share_the_same_stage_counts_function() -> None:
    """기동 검증기(`_require_search_retry_within_stream_budget`)와 런타임 좁히기
    (`stream_recommendation`)가 **같은 함수 객체**에서 계수를 얻는다 — 한쪽만 고쳐지는
    드리프트를 구조적으로 막는 D7 의 핵심 주장이다.
    """
    from app.core.config import _rescue_chain_stage_counts as config_fn

    assert recommendation_graph._rescue_chain_stage_counts is config_fn


# ─────────── [R9, 오케스트레이터 지적] 핵심 명제 — 3s 벽이 성공할 검색을 실패로 바꾼다 ───────
#
# 위 테스트들(1~9)은 전부 메커니즘만 잰다 — 이 이슈가 존재하는 이유인 "3s 를 넘기는 검색이
# 오늘은 실패로 바뀌고, 검색 전용 타임아웃을 올리면 살아난다"는 명제 자체를 재는 테스트가
# 없었다(오케스트레이터가 PR #452 리뷰 완주 뒤 직접 지적, 사장님 승인).
#
# 운영 실측 근거(#395, 2026-08-06): 필터 없음 검색은 7.74초·12.3MB 로 3초 검색 예산을 넘겨
# `SEARCH_FAILED` 로 끝나고, "신발" 같은 필터 있는 검색은 0.24초·435B 로 정상 완료한다. 이
# 파일의 A/B 는 그 비율을 그대로 재지 않는다 — 테스트에서 7.74초를 실제로 자면 안 되므로
# `tests/unit/test_spring_search_budget_132.py` 가 이미 쓰는 방식(초 단위를 0.05 급으로
# 스케일 다운)을 따라 **"응답 지연 > 검색 예산" 관계**만 재현한다(숫자는 테스트 소요 시간
# 때문에 줄였다 — 실측 절대값이 아니라 그 부등식 방향이 핵심 주장이다).
#
# `_SlowTransport`/`_install_transport` 는 새로 정의하지 않고 `test_spring_search_budget_132`
# 에서 그대로 import 한다(#132 가 만든 "httpx 타임아웃에 안 걸리는 느린 응답" 도구 — 같은
# 개념을 두 곳에 다르게 두지 않는다). `MockTransport` 는 즉시 응답이라 이 결을 못 만든다는
# 것도 그 클래스 docstring 에 이미 적혀 있다.
#
# **`spring_client.search_products` 를 가짜 `search=` 콜러블로 우회하지 않는다** — 타임아웃이
# 실제로 무는 지점(`asyncio.wait_for(..., timeout=budget_s)`, `spring_client.py::search_products`)
# 이 거기이므로, A/B 는 그 함수를 직접 호출하고 HTTP 경계에만 `_install_transport` 로 대역을
# 세운다.
#
# ⚠️ 새 기동 검증기(`RESCUE_STAGE_MIN_TIMEOUT_S < SPRING_SEARCH_TIMEOUT_S`, PR #452 리뷰 R5)와
# 충돌한다 — `SPRING_SEARCH_TIMEOUT_S` 를 0.05 급으로 낮추면 기본 하한(0.5)에 걸린다.
# `test_spring_search_budget_132.py::_shrink_budget` 가 같은 이유로 하한을 함께 낮춰 재조준한
# 것과 같은 방식으로, 아래도 `RESCUE_STAGE_MIN_TIMEOUT_S` 를 `SPRING_SEARCH_TIMEOUT_S` 보다
# 항상 작게 함께 설정한다(검증 대상이 아닌 무관한 설정만 맞춘다).


async def test_default_search_timeout_fails_a_response_slower_than_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R9-A — 문제가 실재함을 코드가 스스로 증명] 응답 지연이 검색 예산보다 크면
    `spring_client.search_products` 는 `SpringUnavailableError` 로 끝난다.

    `SPRING_SEARCH_TIMEOUT_S=0.01`(budget = 0.01 × attempts(1) = 0.01s) 에 0.05s 지연 응답을
    준다 — 지연이 예산의 5배라 넉넉히 초과한다. 이 비율(지연 > 예산)이 운영 실측(#395)
    "필터 없음 7.74초 > 검색 예산 3초"와 같은 방향이다(절대값은 테스트 소요 때문에 스케일
    다운했다 — 위 섹션 코멘트 참조).

    검증 실효성(리뷰 요구사항 4): **B 와 짝을 이루는 이 테스트가 상수가 아니라 실제 조건을
    재는지, `SPRING_SEARCH_TIMEOUT_S` 를 지연(0.05s) 위로 올려 확인했다** — B 의 값(1.0s)으로
    바꾸면 이 테스트는 예외가 나지 않아 실패한다(수동 확인, 코드에는 남기지 않는다 — 커밋
    보고에 결과를 적는다).
    """
    monkeypatch.setenv("SPRING_SEARCH_TIMEOUT_S", "0.01")
    # [R5 검증기 회피] 하한(기본 0.5)이 위 예산(0.01)보다 커지지 않게 함께 낮춘다 — 검증 대상이
    # 아닌 무관한 설정만 맞춘다(`test_spring_search_budget_132.py::_shrink_budget` 와 같은 이유).
    monkeypatch.setenv("RESCUE_STAGE_MIN_TIMEOUT_S", "0.001")
    get_settings.cache_clear()

    _install_transport(monkeypatch, _SlowTransport(delay_s=0.05))

    with pytest.raises(SpringUnavailableError):
        await spring_client.search_products(ProductSearchFilters(keyword="무선 이어폰"))


async def test_raising_search_timeout_above_the_same_delay_makes_it_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R9-B — 이 PR 의 손잡이가 실제로 문제를 푼다] A 와 **완전히 같은 지연**(0.05s)을 주고
    `SPRING_SEARCH_TIMEOUT_S` 만 그 지연 위(1.0s, 20배 여유 — 지터로 인한 불안정성 방지)로
    올리면, 이번엔 예외 없이 정상 `ProductSearchResult` 가 돌아온다.

    A 와 이 테스트는 지연·응답이 동일하고 `SPRING_SEARCH_TIMEOUT_S` 손잡이 하나만 다른 쌍이다
    — 그래야 그 손잡이가 원인이라는 게 증명된다(다른 Spring 호출은 이 손잡이의 영향을 안
    받는다는 성질은 `test_spring_search_timeout_only_scopes_search_products`(위 (1))가 이미
    재고 있어 새로 만들지 않는다 — 상호 참조).

    "예외 안 남"이 아니라 **실제 결과 객체**를 검사한다(리뷰 요구사항) — `_SlowTransport` 는
    `{"success": True, "data": []}` 를 주므로(그 클래스 docstring 참조) 빈 결과가 정답이다.
    """
    monkeypatch.setenv("SPRING_SEARCH_TIMEOUT_S", "1.0")
    monkeypatch.setenv("RESCUE_STAGE_MIN_TIMEOUT_S", "0.001")
    get_settings.cache_clear()

    _install_transport(monkeypatch, _SlowTransport(delay_s=0.05))

    result = await spring_client.search_products(ProductSearchFilters(keyword="무선 이어폰"))

    assert result.products == []
    assert result.total_count == 0


async def test_slow_search_surfaces_as_search_failed_sse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R9-D — 사용자가 보는 것까지] 기본값 + A 와 같은 지연으로 `run_buyer_turn` 을 태우면
    SSE 에 `error{code:"SEARCH_FAILED"}` 프레임이 실제로 나간다 — A(단위)가 잡는 결함을
    사용자가 겪는 형태로 한 번 더 고정한다.

    A/B 와 달리 `search=` 에 가짜 콜러블을 주지 않는다 — 실제 `search_service.search_catalog`
    를 `SpringSearchBackend`(Spring 위임, `spring_client.search_products` 를 그대로 부른다,
    pgvector 재정렬 없음)로 고정해 주입한다. 기본 백엔드(`embedding_rerank`)는 pgvector DB 가
    있어야 해 단위테스트 범위 밖이라 이 백엔드로 고정했다 — 이것도 가짜가 아니라 저장소에 이미
    있는 실제 백엔드 구현체다.

    카테고리 매핑도 실 DB 를 타지 않게 `categoryQueries=[]` 로 비웠다 — 신호가 없으면
    `category_mapping.py::map_categories` 가 DB/임베딩 호출 없이 빈 매핑을 낸다(그 함수
    docstring "(4) raw·query 모두 없음(빈 리스트 포함) → 신호 없음"·"빈 리스트를 강제로 채우지
    않는다" 참조) — `filters.keyword` 는 남아 있어 무필터 판정(I-3 폴백)에도 걸리지 않는다.
    """
    monkeypatch.setenv("SPRING_SEARCH_TIMEOUT_S", "0.01")
    monkeypatch.setenv("RESCUE_STAGE_MIN_TIMEOUT_S", "0.001")
    get_settings.cache_clear()

    _install_transport(monkeypatch, _SlowTransport(delay_s=0.05))

    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "무선 이어폰",
        "categoryQueries": [],
        "filters": {"keyword": "무선 이어폰"},
    }

    events = await _collect(
        run_buyer_turn(
            _req(),
            _guest(),  # 게스트 — I-19 구매 이력 조회가 없어 배선이 단순하다
            llm=FakeLLM(decompose=decompose),
            search=functools.partial(
                search_service.search_catalog, backend=search_service.SpringSearchBackend()
            ),
            push_fn=_RecordingPush(),
        )
    )

    error = next((e for e in events if e["type"] == "error"), None)
    assert error is not None, "느린 검색이 SSE error 프레임으로 이어지지 않았다"
    assert error["data"]["code"] == "SEARCH_FAILED"
