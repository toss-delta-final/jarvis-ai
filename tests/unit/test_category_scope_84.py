"""카테고리 범위 해제 분류기 — 파싱·degrade·게이트·병렬 정리 (#84).

인라인 `categoryAction` 이 fast 티어에서 무동작(리셋 기대 32건 중 clear 0~6)이라 전용 호출로
떼어낸 결과물이다. 여기 테스트는 **배관**을 고정한다 — 판정 품질은 실 LLM 프로브
(`evals/intent_probe`)가 재고, 이 파일은 가짜 LLM 만 쓴다(CI API 콜 0).
"""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.category_scope import _SYSTEM, classify_category_scope
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.llm import LLMError
from app.core.session_context import BuyerSessionInput
from app.schemas.chat import BuyerChatRequest
from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM

PRIOR_CATEGORY = "무선이어폰"


# ─────────── 하네스 ───────────


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


class _ScriptedLLM:
    """분류기 호출만 받는 최소 LLM — 응답 문자열을 그대로 돌려주거나 예외를 던진다."""

    def __init__(self, raw: str = '{"scopeFree": true}', *, error: Exception | None = None) -> None:
        self._raw = raw
        self._error = error
        self.calls: list[tuple[str, str, int]] = []  # (system, user, max_tokens)

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        self.calls.append((system, user, max_tokens))
        if self._error is not None:
            raise self._error
        return self._raw

    async def stream(self, *, system, user, tier, max_tokens=1024):  # noqa: ANN001
        yield "x"


class _ScopeAwareLLM(FakeLLM):
    """system 프롬프트로 분기하는 가짜 — 분류기에는 scopeFree, 그 밖에는 decompose 산출.

    두 호출이 같은 `tier="fast"` 라 tier 로는 갈리지 않는다. 배포 경로가 실제로 두 호출을 하므로
    통합 테스트도 그 모양이어야 한다(하나만 흉내 내면 측정이 배포와 갈라진다).
    """

    def __init__(self, *, scope_free, decompose=None) -> None:  # noqa: ANN001
        super().__init__(decompose=decompose)
        self._scope_free = scope_free
        self.scope_calls = 0

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if system == _SYSTEM:
            self.scope_calls += 1
            if self._scope_free is None:
                return "판정 불가"  # JSON 아님 → 분류기가 None 으로 떨어뜨린다
            return json.dumps({"scopeFree": self._scope_free})
        return await super().complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        request_id="category-scope-test",
        context_id=context.context_id,
        record_model_call=lambda *_: None,
    )


async def _run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(request, identity, observer=observer, **kwargs):
        yield frame


class _RecordingSearch:
    def __init__(self) -> None:
        self.filters: list[ProductSearchFilters] = []

    async def __call__(self, filters, exclude_product_ids=None):  # noqa: ANN001
        self.filters.append(filters)
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )


async def _push_ok(push) -> bool:  # noqa: ANN001
    return True


async def _collect(gen) -> list[dict]:  # noqa: ANN001
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


def _request(**updates):
    payload = {"sessionId": "s1", "threadId": "t1", "message": "추천해줘"}
    payload.update(updates)
    return BuyerChatRequest.model_validate(payload)


async def _seed_prior_category(identity, llm=None) -> None:  # noqa: ANN001
    """1턴 — category=무선이어폰 을 스레드에 남긴다."""
    await _collect(
        _run_buyer_turn(
            _request(),
            identity,
            llm=llm or FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )


def _settings():
    """실 Settings 를 그대로 쓴다 — `resolve_model_id` 가 provider 설정까지 읽으므로
    부분 흉내(SimpleNamespace)는 그 자리에서 AttributeError 가 되고, 그 예외를 분류기가
    삼켜 **테스트가 조용히 무의미**해진다(실제로 한 번 밟았다)."""
    return get_settings()


# ─────────── 분류기 단위 ───────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"scopeFree": true}', True),
        ('{"scopeFree": false}', False),
        ('{"scopeFree": "true"}', None),  # 문자열은 해제 신호가 아니다
        ('{"scopeFree": 1}', None),  # 숫자도 마찬가지
        ("{}", None),  # 키 누락
        ('{"scopeFree": null}', None),
        ("판정할 수 없습니다", None),  # JSON 아님
        ('{"scopeFree": true} 라고 봅니다', True),  # 코드펜스·군말은 extract_json 이 흡수
    ],
)
async def test_scope_free_parsing_only_trusts_real_booleans(raw, expected) -> None:
    llm = _ScriptedLLM(raw)
    assert (
        await classify_category_scope(
            llm, message="아무거나", prior_category=PRIOR_CATEGORY, settings=_settings()
        )
        is expected
    )


async def test_llm_failure_returns_none_instead_of_raising() -> None:
    """보조 신호의 실패가 턴을 죽이면 안 된다 — degrade 원칙(`_map_or_empty` 와 같은 규약)."""
    llm = _ScriptedLLM(error=LLMError("boom"))
    assert (
        await classify_category_scope(
            llm, message="아무거나", prior_category=PRIOR_CATEGORY, settings=_settings()
        )
        is None
    )


async def test_unexpected_exception_is_also_swallowed() -> None:
    llm = _ScriptedLLM(error=RuntimeError("네트워크 붕괴"))
    assert (
        await classify_category_scope(
            llm, message="아무거나", prior_category=PRIOR_CATEGORY, settings=_settings()
        )
        is None
    )


async def test_cancellation_is_not_swallowed() -> None:
    """`CancelledError` 는 BaseException 이라 전파돼야 한다 — 그래프가 태스크를 취소할 수 있어야 한다."""
    llm = _ScriptedLLM(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await classify_category_scope(
            llm, message="아무거나", prior_category=PRIOR_CATEGORY, settings=_settings()
        )


async def test_model_call_is_recorded_exactly_once() -> None:
    recorded: list[str] = []
    observer = SimpleNamespace(record_model_call=recorded.append)
    await classify_category_scope(
        _ScriptedLLM(),
        message="아무거나",
        prior_category=PRIOR_CATEGORY,
        settings=_settings(),
        observer=observer,
    )
    assert len(recorded) == 1


async def test_failed_call_is_still_recorded() -> None:
    # 실패한 호출도 비용이 든다 — 형제 경로(needs_expansion·decompose)와 같이 **호출 전** 기록한다.
    recorded: list[str] = []
    observer = SimpleNamespace(record_model_call=recorded.append)
    await classify_category_scope(
        _ScriptedLLM(error=LLMError("boom")),
        message="아무거나",
        prior_category=PRIOR_CATEGORY,
        settings=_settings(),
        observer=observer,
    )
    assert len(recorded) == 1


async def test_user_block_carries_the_prior_category_and_the_message() -> None:
    llm = _ScriptedLLM()
    await classify_category_scope(
        llm, message="5만원 이하 아무거나", prior_category=PRIOR_CATEGORY, settings=_settings()
    )
    system, user, max_tokens = llm.calls[0]
    assert user == f"직전 상품 종류: {PRIOR_CATEGORY}\n사용자 발화: 5만원 이하 아무거나"
    assert max_tokens == get_settings().category_scope_max_tokens


def test_system_prompt_keeps_the_measured_wording() -> None:
    """이 문면이 fast 32/32 를 낸 실측 대상이다 — 동의어로 바꾸면 그 측정이 무효가 된다."""
    anchors = (
        '아래 JSON 만 출력하세요(설명·코드펜스 금지): {"scopeFree": true | false}',
        '- **가격·평점 조건이 함께 있어도** "아무거나"·"상관없이" 가 있으면 true 입니다'
        " — 조건은 종류와 별개입니다.",
        "확신이 없으면 false 입니다.",
    )
    for anchor in anchors:
        assert anchor in _SYSTEM, anchor
    assert _SYSTEM.endswith("확신이 없으면 false 입니다.")


# ─────────── 발동 게이트 ───────────


async def _scope_calls_for(identity, request, **kwargs) -> int:  # noqa: ANN001
    llm = _ScopeAwareLLM(scope_free=True)
    await _collect(
        _run_buyer_turn(
            request,
            identity,
            llm=llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
            **kwargs,
        )
    )
    return llm.scope_calls


async def test_gate_fires_once_on_a_normal_refine_turn() -> None:
    identity = _member()
    await _seed_prior_category(identity)
    assert await _scope_calls_for(identity, _request(message="더 저렴한 걸로")) == 1


async def test_gate_is_closed_without_a_prior_category() -> None:
    # 첫 턴·prior 없는 스레드는 **호출 0회** — 오늘과 완전히 동일하고 비용도 0이다.
    assert await _scope_calls_for(_member(), _request(message="더 저렴한 걸로")) == 0


async def test_gate_is_closed_when_the_classifier_is_disabled(monkeypatch) -> None:
    identity = _member()
    await _seed_prior_category(identity)
    settings = get_settings()
    monkeypatch.setattr(settings, "category_scope_classifier_enabled", False, raising=False)
    assert await _scope_calls_for(identity, _request(message="더 저렴한 걸로")) == 0


async def _set_pending(identity, request) -> None:  # noqa: ANN001
    from app.agents.buyer.cart.state import CartOption, PendingAdd
    from app.agents.buyer.session_state import context_thread_key

    observer = await _committed_observer(request, identity)
    store = await get_cart_store()
    await store.set_pending(
        context_thread_key(observer.context_id, request.thread_id),
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1, name="블랙")]),
    )


async def test_gate_stays_open_during_an_option_reask() -> None:
    """[2차 리뷰 F-1] 되물음 턴에서도 **호출 1회**다 — 초판은 여기를 막아 결함을 남겼다.

    초판 게이트는 "되물음 턴은 카테고리 리셋 턴일 수 없다"고 단정했는데 틀렸다: 사용자는 되물음을
    버릴 수 있고("그건 됐고 종류 상관없이 …"), 그 턴은 decompose 가 recommend 로 보내면서 pending
    도 정리하는데 분류기만 안 돌면 carry 로 떨어져 #84 결함이 그대로 재발한다.

    되물음에서 여는 것이 안전한 이유는 게이트 주석에 있다 — 분류기 프롬프트에는 `PENDING_CART` 가
    실리지 않아 교란 표면이 없고, 산출은 추천 경로에서만 소비된다(옵션 답변 턴은 cart_add 로 가
    아래 intent 분기에서 취소된다).
    """
    identity = _member()
    request = _request(message="2번으로")
    await _seed_prior_category(identity)
    await _set_pending(identity, request)
    assert await _scope_calls_for(identity, request) == 1


async def test_gate_is_closed_on_an_action_only_turn() -> None:
    """`conditionActions` 만 있고 발화가 빈 턴은 판정할 말이 없다."""
    identity = _member()
    await _seed_prior_category(identity)
    request = BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "message": "   ",
            "conditionActions": [{"op": "remove", "field": "priceMax"}],
        }
    )
    assert await _scope_calls_for(identity, request) == 0


# ─────────── 병렬 실행과 태스크 정리 ───────────


class _SlowScopeLLM(_ScopeAwareLLM):
    """분류기 호출이 오래 걸리는 가짜 — 취소되지 않으면 테스트가 그것을 관측한다."""

    def __init__(self, *, decompose_error: bool = False) -> None:
        super().__init__(scope_free=True)
        self.started = 0
        self.finished = 0
        self.cancelled = 0
        self._decompose_error = decompose_error

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if system == _SYSTEM:
            self.started += 1
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            self.finished += 1
            return json.dumps({"scopeFree": True})
        if self._decompose_error:
            # 먼저 이벤트루프에 한 번 양보한다 — 그래야 병렬 분류기 태스크가 **실제로 시작**하고,
            # 아래 취소가 "시작도 안 한 태스크"가 아니라 진행 중인 호출을 끊는지 검증된다.
            await asyncio.sleep(0)
            raise LLMError("decompose boom")
        return await super().complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )


async def test_decompose_failure_cancels_the_classifier_task() -> None:
    """오류 경로에서 태스크를 남기면 "Task exception was never retrieved" 와 유령 호출이 남는다."""
    identity = _member()
    await _seed_prior_category(identity)
    llm = _SlowScopeLLM(decompose_error=True)
    before = {task for task in asyncio.all_tasks()}

    events = await _collect(
        _run_buyer_turn(
            _request(message="더 저렴한 걸로"),
            identity,
            llm=llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )

    # progress_events_enabled 기본 on(#396) — decompose 오류는 progress emit(decompose 직전)
    # 이후에 발생하므로 progress 가 error 앞에 먼저 나간다.
    assert [event["type"] for event in events] == ["progress", "error"]
    for _ in range(5):  # [라운드 4] 동기 취소 — 배달은 이벤트루프가 한다
        await asyncio.sleep(0)
    assert llm.started == 1 and llm.cancelled == 1 and llm.finished == 0
    leftover = {task for task in asyncio.all_tasks() if task not in before and not task.done()}
    assert not leftover, f"정리되지 않은 태스크: {leftover}"


async def test_classifier_failure_still_completes_the_turn_on_carry() -> None:
    """분류기가 죽어도 턴은 정상 완료되고 오늘 동작(carry)으로 폴백한다."""
    identity = _member()
    await _seed_prior_category(identity)

    class _BoomScope(_ScopeAwareLLM):
        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
            if system == _SYSTEM:
                raise RuntimeError("분류기 붕괴")
            return await super().complete(
                system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
            )

    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            _request(message="더 저렴한 걸로"),
            identity,
            llm=_BoomScope(
                scope_free=True,
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 2,
                    "categoryQueries": [],
                    "filters": {"priceMax": 50000},
                },
            ),
            search=search,
            push_fn=_push_ok,
        )
    )

    assert "error" not in {event["type"] for event in events}
    assert search.filters[-1].category == PRIOR_CATEGORY


async def test_gate_is_closed_when_the_prior_exists_but_has_no_category() -> None:
    """prior 는 있는데 **카테고리 축이 비어 있는** 스레드 — 풀 대상이 없으므로 호출하지 않는다.

    `prior is not None` 만으로는 이 경우를 막지 못한다(칩으로 카테고리만 제거한 스레드가 그
    모양이다). 이 입력이 없으면 `bool(prior.category)` 조건은 커버리지 0 인 채로 남는다
    (lessons 「방어를 하나 더 얹을 때는 이 축만 막을 수 있는 입력을 같이 적는다」).
    """
    identity = _member()
    await _seed_prior_category(identity)
    # 카테고리 칩만 제거 — 이후 prior 는 살아 있지만 category 는 None 이다.
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(
                {
                    "sessionId": "s1",
                    "threadId": "t1",
                    "conditionActions": [{"op": "remove", "field": "category"}],
                }
            ),
            identity,
            # 이 턴의 decompose 가 카테고리 leg 를 다시 내면 제거가 무효가 된다 — 빈 leg 로 둔다.
            llm=_ScopeAwareLLM(
                scope_free=True,
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 2,
                    "categoryQueries": [],
                    "filters": {"priceMax": 50000},
                },
            ),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )

    assert await _scope_calls_for(identity, _request(message="더 저렴한 걸로")) == 0


class _SlowScopeIntent(_ScopeAwareLLM):
    """분류기 호출이 오래 걸리고, decompose 는 지정한 intent 를 내는 가짜."""

    def __init__(self, intent: str, *, delay: float = 5.0) -> None:
        decompose = (
            {"intent": "general", "reply": "네", "case": 2, "filters": {}}
            if intent == "general"
            else {
                "intent": intent,
                "reply": "",
                "case": 2,
                "categoryQueries": [],
                "filters": {"priceMax": 50000},
            }
        )
        super().__init__(scope_free=True, decompose=decompose)
        self._delay = delay
        self.started = 0
        self.finished = 0
        self.cancelled = 0

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if system == _SYSTEM:
            self.started += 1
            try:
                await asyncio.sleep(self._delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            self.finished += 1
            return json.dumps({"scopeFree": True})
        # 이벤트루프에 한 번 양보한다 — 그래야 병렬 분류기 태스크가 **실제로 시작**하고,
        # 아래 검증이 "시작도 안 한 태스크"가 아니라 진행 중인 호출을 보는 것이 된다.
        await asyncio.sleep(0)
        return await super().complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )


@pytest.mark.parametrize("intent", ["general", "cart_view"])
async def test_non_recommend_turns_cancel_instead_of_waiting(intent: str) -> None:
    """[2차 리뷰 F-2] 값을 쓰지 않는 턴은 분류기를 **기다리지 않고 취소**한다.

    초판은 정리 누락을 막으려고 모든 분기 앞에서 값을 **회수**했는데, 그 대가로
    order_status·cart_*·general 턴이 쓰지도 않을 값을 기다렸다 — 분류기가 느려지면 그만큼
    무관한 턴의 첫 SSE 이벤트가 밀린다(이 설계가 지키려던 "직렬 지연 0"이 그 경로에서 깨진다).
    분류기를 5초 재우는 가짜로 그것을 잰다: 기다렸다면 이 테스트가 5초 걸린다.

    [라운드 4] 정리가 **동기 취소**가 되면서 취소는 그 자리에서 요청되고 **배달은 이벤트루프가
    한다.** 그래서 스트림이 끝난 뒤 틱을 몇 번 돌려 `cancelled` 를 확인한다 — 이것이 "취소를
    걸었다"가 아니라 "진행 중이던 호출이 실제로 끊겼다"의 증거다.
    """
    identity = _member()
    await _seed_prior_category(identity)
    llm = _SlowScopeIntent(intent)
    before = {task for task in asyncio.all_tasks()}

    started = perf_counter()
    events = await _collect(
        _run_buyer_turn(
            _request(message="안녕"),
            identity,
            llm=llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )
    elapsed = perf_counter() - started

    assert "error" not in {event["type"] for event in events}
    assert elapsed < 1.0, f"분류기 완료를 기다렸다({elapsed:.2f}s)"
    for _ in range(5):  # 취소가 배달될 틱을 준다(동기 취소라 스트림은 이미 끝났다)
        await asyncio.sleep(0)
    assert llm.started == 1 and llm.cancelled == 1 and llm.finished == 0
    leftover = {task for task in asyncio.all_tasks() if task not in before and not task.done()}
    assert not leftover, f"정리되지 않은 태스크: {leftover}"


async def test_recommend_turns_still_wait_for_the_value() -> None:
    """대조군 — 추천 턴은 값을 쓰므로 **회수한다**(취소하지 않는다).

    이 짝이 없으면 위 테스트는 "취소가 아니라 그냥 안 부른다"와 구분되지 않는다.
    """
    identity = _member()
    await _seed_prior_category(identity)
    llm = _SlowScopeIntent("recommend", delay=0.0)
    search = _RecordingSearch()

    await _collect(
        _run_buyer_turn(
            _request(message="종류 상관없이 아무거나"),
            identity,
            llm=llm,
            search=search,
            push_fn=_push_ok,
        )
    )

    assert llm.started == 1 and llm.finished == 1 and llm.cancelled == 0
    assert search.filters[-1].category is None  # scopeFree=True 가 실제로 소비됐다


async def test_abandoning_the_reask_and_resetting_releases_the_category() -> None:
    """[2차 리뷰 F-1] 되물음을 **버리고 리셋하는** 발화에서 #84 가 재발하지 않는다.

    `"그건 됐고 종류 상관없이 아무거나 보여줘"` 는 decompose 가 recommend 로 보내고(프롬프트가
    "담기를 취소·중단하려 하면 … 옛 상품에 갇히지 않게"라고 명시한다) 그래프가 pending 도
    정리하는 턴이다. 초판 게이트(`pending_dict is None`)는 이 턴에서 분류기를 아예 안 돌려
    carry 로 떨어뜨렸다 — 이 이슈가 고치려는 결함이 한 경로에 그대로 남아 있었다.
    """
    identity = _member()
    request = _request(message="그건 됐고 종류 상관없이 아무거나 보여줘")
    await _seed_prior_category(identity)
    await _set_pending(identity, request)

    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            request,
            identity,
            llm=_ScopeAwareLLM(
                scope_free=True,
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 2,
                    # 실측된 배포 모양 — 리셋 발화에도 LLM 이 직전 카테고리를 복사한 leg 를 낸다.
                    "categoryQueries": [{"category": PRIOR_CATEGORY, "query": "무선 이어폰"}],
                    "filters": {"priceMax": 50000},
                },
            ),
            search=search,
            push_fn=_push_ok,
        )
    )

    assert "error" not in {event["type"] for event in events}
    assert search.filters[-1].category is None
    assert search.filters[-1].price_max == 50000


async def test_outside_cancellation_does_not_leave_the_task_running() -> None:
    """[2차 리뷰 F-3] 바깥에서 취소돼도 분류기 태스크가 남지 않는다.

    클라이언트가 첫 이벤트 전에 끊으면 요청 태스크가 취소되는데, `CancelledError` 는
    `except LLMError` 에 걸리지 않아 정상 정리 지점을 **전부 건너뛴다.** 그래서 태스크 생성부터
    회수까지를 `try/finally` 로 감싸고 `finally` 에서 동기 취소한다.

    decompose 를 무한정 지연시켜 그 창(窓) 안에서 취소가 오게 만든다.
    """
    identity = _member()
    await _seed_prior_category(identity)
    started = asyncio.Event()

    class _HangingDecompose(_ScopeAwareLLM):
        def __init__(self) -> None:
            super().__init__(scope_free=True)
            self.scope_started = False

        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
            if system == _SYSTEM:
                self.scope_started = True
                started.set()
                await asyncio.sleep(3600)  # 취소되기 전에는 끝나지 않는다
                return json.dumps({"scopeFree": True})
            await started.wait()  # 분류기가 먼저 뜨도록 보장
            await asyncio.sleep(3600)  # decompose 를 그 창 안에서 붙잡아 둔다
            return "{}"

    llm = _HangingDecompose()
    before = {task for task in asyncio.all_tasks()}

    async def _drive() -> None:
        await _collect(
            _run_buyer_turn(
                _request(message="더 저렴한 걸로"),
                identity,
                llm=llm,
                search=_RecordingSearch(),
                push_fn=_push_ok,
            )
        )

    consumer = asyncio.create_task(_drive())
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    # 취소가 분류기 태스크까지 전파될 이벤트루프 틱을 준다.
    for _ in range(5):
        await asyncio.sleep(0)
    scope_tasks = [task for task in asyncio.all_tasks() if task not in before]
    assert llm.scope_started
    assert all(task.cancelled() or task.done() for task in scope_tasks), (
        f"살아 있는 태스크: {[t for t in scope_tasks if not t.done()]}"
    )


# ─────────── 라운드 4 — 정리 지점이 바깥 취소를 삼키지 않는다 ───────────


class _CancelAtDecomposeLLM(_ScopeAwareLLM):
    """decompose 를 마치기 **직전에 자기 턴을 취소**시키는 가짜.

    그러면 그래프가 정리 지점(`except LLMError` 또는 비추천 분기)에 도달할 때 이미 취소가
    걸려 있다. 정리가 `await` 였다면 그 취소가 **거기서 배달돼 `suppress` 에 삼켜지고**
    턴이 계속 진행됐다(라운드 4 지적). 동기 취소면 삼킬 자리가 없어 다음 체크포인트에서
    그대로 전파된다.
    """

    def __init__(self, *, fail_decompose: bool, decompose: dict | None = None) -> None:
        super().__init__(scope_free=True, decompose=decompose)
        self._fail_decompose = fail_decompose
        self.scope_started = False

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if system == _SYSTEM:
            self.scope_started = True
            await asyncio.sleep(5)  # 취소되기 전에는 끝나지 않는다
            return json.dumps({"scopeFree": True})
        await asyncio.sleep(0)  # 분류기 태스크가 실제로 시작하도록 한 틱 양보
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # 정리 지점에 닿기 전에 바깥 취소가 걸린 상태를 만든다
        if self._fail_decompose:
            raise LLMError("decompose boom")
        return await super().complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )


async def _drive_and_report_cancelled(identity, llm, message: str) -> bool:  # noqa: ANN001
    """스트림을 끝까지 소비하고 **바깥 취소가 살아남았는지**(CancelledError 로 끝났는지) 돌려준다.

    이것이 이 라운드의 판별 축이다 — 정리 지점이 `await` 였을 때는 그 취소가 거기서 삼켜져
    소비 태스크가 **정상 종료**했다(`_must_cancel` 이 세팅되지 않아 재전파도 없다).
    """

    async def _run() -> None:
        async for _ in _run_buyer_turn(
            _request(message=message),
            identity,
            llm=llm,
            search=_RecordingSearch(),
            push_fn=_push_ok,
        ):
            pass

    task = asyncio.create_task(_run())
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


async def test_outer_cancel_survives_the_error_path_cleanup() -> None:
    """[라운드 4 F-1] `except LLMError` 경로의 정리가 **바깥 취소를 삼키지 않는다.**

    정리가 `await` 였을 때는 그 지점에서 취소가 배달돼 `suppress` 가 삼켰고, `_must_cancel` 도
    세팅되지 않아 **재전파되지 않았다** — 이미 끊긴 요청인데 error 이벤트가 그대로 나갔다.
    지금은 동기 취소라 삼킬 자리가 없어 취소가 그대로 전파된다.
    """
    identity = _member()
    await _seed_prior_category(identity)
    llm = _CancelAtDecomposeLLM(fail_decompose=True)

    cancelled = await _drive_and_report_cancelled(identity, llm, "더 저렴한 걸로")

    assert llm.scope_started  # 분류기는 실제로 떠 있었다(정리 대상이 있었다)
    assert cancelled, "바깥 취소가 정리 지점에서 삼켜졌다 — 끊긴 요청이 정상 턴으로 이어진다"


async def test_outer_cancel_survives_the_non_recommend_cleanup() -> None:
    """[라운드 4 F-1] 비추천 분기의 정리도 마찬가지다 — 취소가 살아 있어야 한다.

    이쪽은 정리 뒤에 프로필 버퍼 적재·`stream_fallback` 같은 **후속 단계**가 이어지므로,
    취소가 삼켜지면 끊긴 요청이 그 단계들을 끝까지 실행하고 **정상 종료**한다.

    (가짜 저장소·가짜 LLM 은 실제로 suspend 하지 않아 취소가 배달될 체크포인트가 뒤로 밀린다 —
    그래서 "이벤트가 몇 개 나왔나"가 아니라 **"취소로 끝났나"** 를 본다. 그것이 삼켜졌는지 여부를
    직접 가르는 축이다.)
    """
    identity = _member()
    await _seed_prior_category(identity)
    llm = _CancelAtDecomposeLLM(
        fail_decompose=False,
        decompose={"intent": "general", "reply": "네", "case": 2, "filters": {}},
    )

    cancelled = await _drive_and_report_cancelled(identity, llm, "안녕")

    assert llm.scope_started
    assert cancelled, "바깥 취소가 정리 지점에서 삼켜졌다 — 끊긴 요청이 정상 턴으로 이어진다"


# ─────────── 라운드 5 — intent 사후 재분류와 회수 순서 ───────────


async def test_chip_click_turn_that_becomes_recommend_still_gets_the_classifier_value(
    monkeypatch,
) -> None:
    """[라운드 5 F-1] **완화 칩 정확 일치로 intent 가 사후에 `recommend` 가 되는 턴**에서도
    분류기 판정이 살아 있어야 한다.

    칩 label 은 `"65,000원까지 볼까요?"` 같은 의문문이라 decompose 가 `general` 로 볼 수 있다.
    회수/취소를 decompose 직후에 가르면 그 턴은 `general` 로 판단돼 태스크가 **취소**되고,
    곧이어 칩 분기가 `intent = "recommend"` 로 강제해 추천 경로를 타면서 `scope_free` 는
    `None` 으로 고정된다 — 분류기 판정이 통째로 사라진다. **취소된 태스크의 산출은 되살릴 수
    없으므로** 판단을 intent 확정 뒤로 옮겨야 한다.

    판별력: 분류기 True + prior 카테고리가 있으므로, 값이 살아 있으면 최종 `category is None`
    (해제)이고 죽었으면 carry 로 `무선이어폰` 이 남는다.
    """
    label = "65,000원까지 볼까요?"

    class _Offers:
        async def get_snapshot(self, key):  # noqa: ANN001
            return {label: {"field": "priceMax", "value": 65000}}, None

        async def put(self, key, offers, applied):  # noqa: ANN001
            return None

    async def _factory():
        return _Offers()

    identity = _member()
    await _seed_prior_category(identity)
    monkeypatch.setattr("app.agents.buyer.graph.get_relaxation_offer_store", _factory, raising=True)

    search = _RecordingSearch()
    await _collect(
        _run_buyer_turn(
            _request(message=label),
            identity,
            llm=_ScopeAwareLLM(
                scope_free=True,
                # decompose 는 이 의문문을 general 로 본다 — 칩 분기가 recommend 로 되돌린다.
                decompose={"intent": "general", "reply": "네", "case": 2, "filters": {}},
            ),
            search=search,
            push_fn=_push_ok,
        )
    )

    assert search.filters, "칩 분기가 추천 경로로 되돌리지 못했다 — 테스트 전제가 깨졌다"
    assert search.filters[-1].price_max == 65000  # 칩이 실제로 적용된 턴이다
    assert search.filters[-1].category is None  # 분류기 판정(해제)이 살아 있다
