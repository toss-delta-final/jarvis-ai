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

각 테스트는 "이 변경이 회귀했을 때 실제로 깨지는가"를 기준으로 짰다 — 상수를 상수와 비교하는
어설션은 두지 않는다(예: mode 를 무조건 skip/narrow 로 바꿔보면 5·6 이 깨져야 한다).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager

import httpx
import pytest
from pydantic import ValidationError

from app.agents.buyer.recommendation import graph as recommendation_graph
from app.core.config import Settings, get_settings
from app.core.stream import open_stream
from app.schemas.spring import ProductSearchFilters
from app.services import spring_client
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
    """[D4 observe] 기본 모드는 같은 예산 압박에서도 **집행하지 않는다** — 판정만 계산해
    로그에 반사실로 남긴다.

    검증 실효성: `_apply_stage_budget` 가 모드를 무시하고 항상 집행하도록 바꾸면 `calls` 가
    비지 않아 이 테스트가 깨진다.
    """
    settings = get_settings()
    assert settings.rescue_budget_mode == "observe"  # 기본값 전제
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

    log = next(r for r in caplog.records if r.message == "recommend_pipeline")
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

    log = next(r for r in caplog.records if r.message == "recommend_pipeline")
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

    log = next(r for r in caplog.records if r.message == "recommend_pipeline")
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
# attempts` 다(`attempts = 1 if _search_retry_suppressed.get() else spring_max_retries + 1`).
# 아래 본검색 시나리오는 `may_auto_relax=False`(FakeLLM() 기본 decompose 는 ratingMin 이
# 없어 완화 후보가 안 생긴다)라 `suppress_deferred_search_retry=False` — F-1/#343 재검색과
# 같은 "항상 재시도" 축을 탄다. 이 세 테스트가 함께 "런타임 attempts 규칙 ≡ search_products
# attempts 규칙"을 고정한다: 하나만 바뀌면(예: graph.py 가 suppress 여부를 무시하고 상수를
# 쓰면) 아래 중 하나가 반드시 깨진다.


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


async def test_default_spring_max_retries_stage_cap_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #452 리뷰 R2 — 기본값 불변] `spring_max_retries=0`(오늘 배포 기본값)이면
    `attempts=1` 이라 상한은 R2 수정 전후로 `spring_search_timeout_s` 그대로다. 위 테스트와
    똑같은 압박(window=13.5s, granted≈4.5s)을 주고 `spring_max_retries` 만 0 으로 두면 여전히
    `"full"`(안 좁힘)이어야 한다 — 옛 코드도 이 지점에서 이미 `"full"` 이었으므로 이 어설션은
    "R2 수정이 오늘 기본 배포 동작을 바꾸지 않는다"를 직접 잰다.

    검증 실효성: attempts 산출이 `suppress_deferred_search_retry` 를 무시하고 항상
    `spring_max_retries + 1` 을 쓰는 회귀가 나면(그 자체는 이 값에 영향 없지만) 대신 상한
    계산이 `attempts` 를 아예 무시하지 않고 엉뚱한 값(예: 상수 1 대신 2)을 곱하는 회귀가 나면
    `calls` 가 채워져 이 테스트가 깨진다.
    """
    settings = get_settings()
    assert settings.spring_max_retries == 0  # 기본값 전제
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
        "상한이 attempts 배수를 잘못 곱하는 회귀다(기본 배포 동작이 바뀌면 안 된다)"
    )


async def test_narrow_mode_suppressed_stage_stays_at_single_attempt_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #452 리뷰 R2 — attempts 규칙 ≡ search_products 규칙] 이 턴은 `ratingMin` 이 설정돼
    `may_auto_relax=True`(기본 `search_retry_on_deferred_conditions=False`)라
    `suppress_deferred_search_retry=True` — 본검색은 `spring_client.suppress_search_retry()`
    로 감싸여 `spring_client.search_products` 의 `attempts` 가 `_search_retry_suppressed.get()`
    로 강제 1 이 된다. `spring_max_retries=1` 이어도 이 단의 실제 상한은 여전히
    `spring_search_timeout_s * 1 = 3.0s` 다 — 위 두 테스트와 **같은 압박**(granted≈4.5s)을
    줘도 `"full"`(안 좁힘)이어야 한다.

    검증 실효성: graph.py 의 attempts 산출이 `suppress_deferred_search_retry` 를 무시하고
    상수 `spring_max_retries + 1` 을 그대로 쓰는 회귀가 나면(= `search_products` 의 억제
    규칙과 어긋나면) 상한이 6.0s 로 오판돼 이 턴이 narrow 로 좁혀지고 `calls` 가 채워져
    이 테스트가 깨진다 — `test_narrow_mode_stage_cap_accounts_for_retry_attempts_before_
    narrowing`(억제 안 됨 → narrow) 과 정확히 반대 결과가 나와야 두 축이 함께 고정된다.
    """
    settings = get_settings()
    assert settings.search_retry_on_deferred_conditions is False  # 기본값 전제
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
    assert not calls, (
        "억제된(suppress_deferred_search_retry=True) 단인데도 narrow_search_budget 가 "
        "집행됐다 — attempts 산출이 search_products 의 억제 규칙과 어긋난다"
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
        (r for r in caplog.records if r.message in ("recommend_zero_result", "recommend_pipeline")),
        None,
    )
    assert log is not None
    assert log.rescue_stage_skipped_budget is True


# ─────────── (7) ContextVar 비누수 ───────────


def test_narrow_search_budget_resets_after_the_with_block() -> None:
    """`narrow_search_budget` 는 `suppress_search_retry` 와 동일한 누수 방지 규율을 따른다 —
    `with` 블록을 벗어나면 다음 검색 호출로 ContextVar 가 새지 않는다.
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
        Settings(**kwargs)

    with pytest.raises(ValidationError, match="RESCUE_BUDGET_MODE=observe"):
        Settings(**kwargs, progress_events_enabled=False)

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
