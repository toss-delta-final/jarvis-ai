"""#132 — I-1 전량반환 응답의 총시간 가드와 파싱 스레드 이관.

두 구멍을 막는다(둘 다 개수를 자르지 않는다 — 전량 반환은 BE 합의라 유지한다).

1. **httpx 3s 는 총 수신 시간을 못 막는다.** `spring_timeout_s` 는 스칼라로 주입돼
   connect/read/write/pool 네 시계가 되는데, `read` 는 **청크 사이 간격** 상한이라 바이트가
   끊기지 않고 계속 오면 영원히 물리지 않는다. `config._require_search_budget_*` 가
   "검색은 `spring_timeout_s × (재시도+1)` 안에 끝난다"를 전제로 스트림 예산을 검증하는데,
   그 전제를 강제하는 코드가 없었다.
2. **파싱이 이벤트루프 위에서 돈다.** `resp.json()` + N× `model_validate` 는 `to_thread` 밖이라
   그 워커의 **다른 모든 SSE 스트림**이 파싱 시간만큼 멈춘다. 실 카탈로그 응답(13.33 MB /
   7,245건)으로 잰 루프 정지는 단일 호출 **222ms**, 20 동시 **5.4초**였고 이관 후 각각 56ms /
   2.4초로 줄었다(`docs/specs/MEASURE-I1-RESPONSE-132.md` §4).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx
import pytest

from app.core.config import get_settings
from app.core.tracing import FakeTraceExporter, TraceFactory, bind_request_trace
from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from app.services import spring_client
from app.services.spring_client import SpringUnavailableError

pytestmark = pytest.mark.anyio


class _SlowTransport(httpx.AsyncBaseTransport):
    """지연 후 정상 200 을 주는 전송 — **httpx 타임아웃에 걸리지 않는** 느린 응답의 대역.

    실제 상황의 재현이다: BE 가 바디를 끊김 없이 계속 흘리면 read 타임아웃(청크 간격)은 한 번도
    물리지 않으면서 총 수신 시간만 길어진다. MockTransport 는 즉시 응답이라 이 결을 못 만든다.
    """

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self._delay_s)
        return httpx.Response(200, json={"success": True, "data": []})


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    """`_client` 를 교체해 HTTP 경계에서만 대역을 세운다(tests/integration/conftest.py 규약).

    [#427] `_client` 는 이제 `timeout` 키워드 인자를 받는다(`search_products` 가 검색 전용
    타임아웃을 넘긴다) — fake 도 같은 시그니처를 받아야 몽키패치가 실제 호출과 어긋나지 않는다.
    """
    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.test", transport=transport
        ),
    )


def _shrink_budget(monkeypatch: pytest.MonkeyPatch, timeout_s: str = "0.05") -> None:
    """검색 총예산을 테스트 규모로 줄인다 — 가드는 `spring_search_timeout_s × (재시도+1)` 을 쓴다.

    [#427] `search_products` 전용 타임아웃(`spring_search_timeout_s`)이 공용
    `spring_timeout_s` 와 분리됐다 — 이 헬퍼는 검색 총시간 가드(#132)를 겨누므로 전용
    타임아웃을 줄인다.

    [PR #452 리뷰 R5] 기본 `RESCUE_STAGE_MIN_TIMEOUT_S`(0.5)가 `timeout_s` 를 0.5 미만으로
    줄이면(기본 인자 "0.05" 가 그렇다) 새 기동 검증기(`RESCUE_STAGE_MIN_TIMEOUT_S <
    SPRING_SEARCH_TIMEOUT_S`)를 어긴다 — 이 가드(#132)는 구제 체인 좁히기와 무관한 검색
    HTTP 타임아웃 자체를 겨누는 테스트라, 그 부등식이 깨지지 않게 하한도 `timeout_s` 보다
    작게 함께 낮춘다(검증 대상·어설션은 그대로, 무관한 설정만 맞춘다).
    """
    monkeypatch.setenv("SPRING_SEARCH_TIMEOUT_S", timeout_s)
    monkeypatch.setenv("RESCUE_STAGE_MIN_TIMEOUT_S", str(float(timeout_s) / 10))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """setenv 로 바꾼 설정이 다음 테스트로 새지 않게 앞뒤로 캐시를 비운다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_slow_body_exceeding_budget_raises_spring_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx 가 못 잡는 '느리지만 끊기지 않는' 응답을 총시간 가드가 잘라 §7 degrade 로 보낸다."""
    _shrink_budget(monkeypatch)
    budget = get_settings().spring_search_timeout_s * (get_settings().spring_max_retries + 1)
    _install_transport(monkeypatch, _SlowTransport(delay_s=budget * 20))

    started = time.perf_counter()
    with pytest.raises(SpringUnavailableError):
        await spring_client.search_products(ProductSearchFilters(keyword="q"))
    elapsed = time.perf_counter() - started

    # 예산 근처에서 끊겼는지 — 전송 지연(budget × 20)을 끝까지 기다리지 않았다는 것이 요점이다.
    assert elapsed < budget * 10


async def test_fast_response_within_budget_is_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """예산 안에 끝나는 정상 응답은 가드의 영향을 받지 않는다(기존 동작 불변)."""
    _shrink_budget(monkeypatch, "1.0")
    _install_transport(monkeypatch, _SlowTransport(delay_s=0.0))

    result = await spring_client.search_products(ProductSearchFilters(keyword="q"))

    assert result.products == []
    assert result.total_count == 0


async def test_parsing_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """느린 파싱 동안에도 다른 코루틴이 진행한다 — 파싱이 `to_thread` 로 나가 있다는 뜻.

    파싱을 동기 블로킹으로 바꿔 놓고 하트비트를 함께 돌린다. 파싱이 이벤트루프 위에 있으면
    하트비트는 그 사이 **한 번도** 깨어나지 못한다.
    """
    # 가드가 아니라 블로킹 여부를 보는 테스트라 예산을 넉넉히 준다. 단 `spring_timeout_s ×
    # (재시도+1)` 이 first-token 상한(10s)을 넘으면 설정 검증이 기동을 막으므로 2.0 이 상한이다.
    _shrink_budget(monkeypatch, "2.0")
    _install_transport(monkeypatch, _SlowTransport(delay_s=0.0))

    parse_s = 0.2

    def _slow_parse(_data: object) -> ProductSearchResult:
        time.sleep(parse_s)  # 의도적 동기 블로킹 — 실제 파싱 비용의 대역
        return ProductSearchResult(products=[], total_count=0)

    monkeypatch.setattr(spring_client, "_parse_search_response", _slow_parse)

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(parse_s / 20)
            ticks += 1

    beat = asyncio.create_task(_heartbeat())
    try:
        await spring_client.search_products(ProductSearchFilters(keyword="q"))
    finally:
        beat.cancel()

    # 루프가 파싱에 잡혀 있었다면 tick 이 0 이다. 스케줄러 흔들림을 감안해 넉넉히 잡는다.
    assert ticks >= 5, f"파싱이 이벤트루프를 막고 있다 (tick={ticks})"


def test_guard_timeout_shares_the_transport_timeout_label() -> None:
    """가드 타임아웃의 분류는 `_transport_status_class` 한 곳에서 나온다(PR #235 리뷰 규약).

    로그(`_failure_status_class`)와 trace 가 각자 isinstance 를 구현하면 새 예외 타입을 한쪽에만
    추가했을 때 라벨이 조용히 갈린다 — 그 사고가 실제로 두 번 났다.
    """
    exc = spring_client.SearchBudgetExceeded("budget")
    assert spring_client._transport_status_class(exc) == "timeout"
    assert spring_client._failure_status_class(exc) == "timeout"


def test_bare_timeout_error_is_not_labeled_as_transport_timeout() -> None:
    """가드 밖에서 난 `TimeoutError` 는 전송 계층 실패로 분류하지 않는다 (PR #293 리뷰).

    `_transport_status_class` 는 **모든** Spring 호출의 예외 분류에 쓰인다. 내장 `TimeoutError`
    를 그대로 매칭하면 다른 경로에 `wait_for` 가 하나 생기는 순간부터, 또는 무관한 asyncio
    대기가 끊기는 순간부터 trace 가 그 실패를 "전송 계층 timeout" 이라 조용히 거짓 보고한다.
    """
    assert spring_client._transport_status_class(TimeoutError()) is None
    # 로그 라벨도 같은 판단을 공유한다 — 전송 계층이 아니면 malformed_response 로 떨어진다.
    assert spring_client._failure_status_class(TimeoutError()) != "timeout"


def test_guard_exception_stays_compatible_with_timeout_handling() -> None:
    """가드 전용 타입은 `TimeoutError` 하위라 기존 타임아웃 처리와 호환된다."""
    assert issubclass(spring_client.SearchBudgetExceeded, TimeoutError)


async def test_parsing_uses_a_dedicated_executor_not_the_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파싱은 앱 전역 기본 executor 가 아니라 **전용 풀**에서 돈다 (PR #293 리뷰).

    가드는 await 만 취소하고 스레드는 끝까지 돈다 — 버려진 파싱 스레드가 공유 풀을 점유하면
    임베딩·카테고리 매핑처럼 전혀 무관한 `to_thread` 호출까지 슬롯을 기다린다. 전용 풀로
    격리해 고갈의 파급을 검색 파싱 안에 가둔다.
    """
    _shrink_budget(monkeypatch, "2.0")
    _install_transport(monkeypatch, _SlowTransport(delay_s=0.0))

    seen: list[str] = []

    def _record_thread(_data: object) -> ProductSearchResult:
        seen.append(threading.current_thread().name)
        return ProductSearchResult(products=[], total_count=0)

    monkeypatch.setattr(spring_client, "_parse_search_response", _record_thread)

    await spring_client.search_products(ProductSearchFilters(keyword="q"))

    assert seen, "파싱이 호출되지 않았다"
    assert seen[0].startswith("i1-parse"), f"전용 풀이 아니라 {seen[0]} 에서 돌았다"


async def test_guard_timeout_drops_parse_work_still_waiting_in_the_queue() -> None:
    """가드 취소는 **아직 시작 안 된** 파싱 작업을 큐에서 제거한다 (PR #293 리뷰 검증).

    이게 성립하지 않으면 "버려진 작업이 큐를 채워 신규 요청이 파싱을 시작도 못 하고 예산을
    태우고, 그 요청의 작업도 큐에 남아" 자기증폭하는 연쇄 실패가 된다. 취소가 executor future
    까지 전파되는 덕에 큐가 스스로 빠지는 것이 그 시나리오를 막는 유일한 근거이므로,
    이후 리팩터가 조용히 깨뜨리지 못하게 여기서 고정한다.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    ran: list[str] = []

    def _work(tag: str, seconds: float) -> str:
        ran.append(tag)
        time.sleep(seconds)
        return tag

    async def _submit(tag: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(pool, _work, tag, 0.05)

    try:
        loop = asyncio.get_running_loop()
        occupying = loop.run_in_executor(pool, _work, "occupying", 0.4)
        await asyncio.sleep(0.05)  # 하나뿐인 워커를 확실히 점유시킨다

        # 슬롯이 없어 큐에서 대기하다 예산 초과 — 실제 가드와 같은 모양(wait_for(코루틴)).
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_submit("queued"), timeout=0.05)

        await occupying
        await asyncio.sleep(0.2)  # 점유가 풀린 뒤에도 되살아나지 않는지 본다
    finally:
        pool.shutdown(wait=True)

    assert "queued" not in ran, "취소된 파싱 작업이 큐에 남아 나중에 실행됐다"


async def test_guard_timeout_is_recorded_on_the_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """가드가 끊은 호출도 span 에 `statusClass=timeout` 을 남긴다 — 위 라벨의 **배선**까지 본다.

    분류 함수만 검사하면 `_spring_span` 이 그 함수를 실제로 부르는지, 가드 예외가 span 경계
    **안에서** 터지는지는 검증되지 않는다. 배선이 끊기면 trace 에는 "실패했는데 원인 미분류"로
    남고 로그와 대조가 깨진다.
    """
    _shrink_budget(monkeypatch)
    budget = get_settings().spring_search_timeout_s * (get_settings().spring_max_retries + 1)
    _install_transport(monkeypatch, _SlowTransport(delay_s=budget * 20))

    exporter = FakeTraceExporter()
    trace = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0).start_request(
        name="buyer_chat_turn",
        request_id="req-132",
        conversation_id="session-132",
        thread_id="thread-132",
        lane="buyer",
        environment="test",
    )
    with bind_request_trace(trace):
        with pytest.raises(SpringUnavailableError):
            await spring_client.search_products(ProductSearchFilters(keyword="q"))
    await trace.finish(status="FAILED", error_type="SEARCH_FAILED", terminal_reason="error")

    spans = [n for n in exporter.exported[0] if n.name == "spring.search_products"]
    assert len(spans) == 1, "호출당 span 1개 계약(#133)이 가드 경로에서도 유지돼야 한다"
    assert spans[0].metadata["statusClass"] == "timeout"
