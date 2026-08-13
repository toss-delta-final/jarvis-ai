"""구매자 추천 그래프 (이슈 #2) — 파이프라인·degrade·fallback·멀티턴·경로 B 회귀.

run_buyer_turn 을 fake LLM/검색/push 로 직접 구동한다(라이브 Anthropic·Spring 불필요).
SSE 는 상품 카드를 싣지 않는다(경로 B) — products.ready 는 {sessionId, listIds} 만.
"""

from __future__ import annotations

import asyncio
import gc
import json
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from langgraph.store.memory import InMemoryStore

from app.agents.buyer.graph import get_thread_store, run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation import graph as recommendation_graph
from app.agents.buyer.recommendation.overall_comment_grounding import NEUTRAL_OVERALL_COMMENT
from app.agents.buyer.recommendation.rerank_grounding import NEUTRAL_RATIONALE
from app.agents.buyer.recommendation.state import RepurchaseStore
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import ProductSearchResult, SpringProduct
from app.services.spring_client import SpringUnavailableError
from tests._fakes import DEFAULT_DECOMPOSE, DEFAULT_PRODUCTS, FakeLLM


def _req(message: str = "무선 이어폰 추천해줘", session_id: str = "s1", thread_id: str = "t1"):
    return SimpleNamespace(session_id=session_id, thread_id=thread_id, message=message)


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject=None)


def _event(record: object, event: str) -> bool:
    """JSON message 구조화 이벤트를 caplog에서 찾는다."""
    return getattr(record, "event", None) == event


async def _committed_observer(request, identity, observer=None):  # noqa: ANN001
    owner_id = buyer_owner_id(identity, get_settings())
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            owner_id,
        )
    )
    if observer is None:
        observer = SimpleNamespace(
            request_id="unit-request",
            record_model_call=lambda *_: None,
        )
    observer.context_id = context.context_id
    return observer


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity, kwargs.pop("observer", None))
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
        yield frame


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


def _make_search(products):
    async def _search(filters, exclude_product_ids=None):
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _search


async def _failing_search(filters, exclude_product_ids=None):
    raise SpringUnavailableError("spring down")


class _RecordingPush:
    def __init__(self) -> None:
        self.pushes: list = []

    async def __call__(self, push) -> bool:
        self.pushes.append(push)
        return True


async def _failing_push(push) -> bool:
    raise SpringUnavailableError("push down")


def _only_list(push):
    """일반 추천은 목록 1건 — lists 길이 1 배열에서 그 항목을 꺼낸다 (§4.2 v0.17.1)."""
    assert len(push.lists) == 1
    return push.lists[0]


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


def _revert_chips(events) -> list[dict]:
    """되돌리기 칩만 추린다 — [#113] 소량 결과의 완화 칩이 같은 suggestions 이벤트에 함께 실린다."""
    return [
        chip
        for e in events
        if e["type"] == "suggestions"
        for chip in e["data"]["chips"]
        if chip.get("revert")
    ]


# ─────────── 해피패스 파이프라인 ───────────


async def test_happy_path_pipeline() -> None:
    """decompose→search→rerank→push→products.ready→done, rerank 순서 id 를 push 한다."""
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    types = _types(events)
    assert types.count("conditions") == 1
    assert types.count("products.ready") == 1
    assert types.count("done") == 1
    assert types[-1] == "done"
    assert types.index("conditions") < types.index("products.ready") < types.index("done")

    # push 된 productIds — rerank 순서(101,102)가 앞, expose_min 보충으로 검색순서 103 추가.
    assert len(push.pushes) == 1
    entry = _only_list(push.pushes[0])
    assert entry.product_ids[:2] == [101, 102]
    assert set(entry.product_ids) <= {101, 102, 103}

    # Production C는 legacy fake에 구조화 metadata가 없으면 후보·순위는 보존하고 reason만
    # 중립 템플릿으로 강등한다. expose_min 보충 103은 rerank 항목이 아니라 reason에서 제외된다.
    reasons = {r.product_id: r.reason for r in entry.reasons}
    assert reasons == {101: NEUTRAL_RATIONALE, 102: NEUTRAL_RATIONALE}

    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "stop"


async def test_push_sends_single_list_as_length_one_array() -> None:
    """일반 추천은 목록 1개지만 lists 는 길이 1 배열이고 listType 은 항상 실린다 (§4.2 v0.17.1).

    후보들이 서로 대안이라 PICK_ONE 이며, 세트(BUY_ALL)·총액 예산은 이 그래프가 내지 않는다(#60).
    """
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )

    sent = push.pushes[0]
    assert len(sent.lists) == 1
    assert sent.list_type == "PICK_ONE"
    assert sent.total_budget is None
    assert sent.lists[0].label is None
    # products.ready 의 listIds 는 lists 와 순서·개수가 같다(§4.2 규약, §3.1).
    ready = next(e for e in events if e["type"] == "products.ready")["data"]
    assert ready["listIds"] == [entry.list_id for entry in sent.lists]


async def test_recommendation_request_id_is_per_turn_and_distinct_from_list_id() -> None:
    """추천 실행 상관키는 턴마다 새로 발급되며 listId 와 역할이 달라 값도 다르다 (§4.2, #140)."""
    push = _RecordingPush()
    for _ in range(2):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )

    request_ids = [item.recommendation_request_id for item in push.pushes]
    assert len(set(request_ids)) == 2, "추천 실행 1회당 새 상관키여야 한다"
    for item in push.pushes:
        assert len(item.recommendation_request_id) <= 36  # BE CHAR(36)
        assert item.recommendation_request_id != _only_list(item).list_id


async def test_list_id_uses_uuid4_hex_and_matches_products_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-21 listId는 추측 불가능한 UUID4 hex이며 push와 SSE가 같은 값을 사용한다.

    한 턴이 uuid4 를 2회 쓴다 — recommendationRequestId(정규 36자) → listId(hex 32자) 순.
    """
    generated = [
        UUID("a63be350-ec96-4f44-b3f9-c962b6673a68"),
        UUID("9f2c1a7e-4b8d-43f5-a0c6-e1d97b3f8a24"),
        UUID("c1e97b3f-8a24-4f5a-b0c6-d1e97b3f8a24"),
        UUID("4b8d43f5-a0c6-41d9-b3f8-a249f2c1a7e4"),
    ]
    generated_iter = iter(generated)
    monkeypatch.setattr(recommendation_graph, "uuid4", lambda: next(generated_iter))
    push = _RecordingPush()

    first_events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    second_events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )

    pushed_ids = [_only_list(item).list_id for item in push.pushes]
    ready_ids = [
        next(event for event in events if event["type"] == "products.ready")["data"]["listIds"][0]
        for events in (first_events, second_events)
    ]

    expected_ids = [generated[1].hex, generated[3].hex]
    assert pushed_ids == expected_ids
    assert ready_ids == pushed_ids
    # 상관키는 정규 UUID 문자열(36자) — listId(hex 32자)와 형식부터 구분된다.
    assert [item.recommendation_request_id for item in push.pushes] == [
        str(generated[0]),
        str(generated[2]),
    ]


async def test_products_ready_carries_no_cards() -> None:
    """[HARD] 경로 B — products.ready 는 상관키만, 어떤 이벤트에도 카드 필드 없음."""
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    ready = next(e for e in events if e["type"] == "products.ready")["data"]
    assert set(ready.keys()) == {"sessionId", "listIds"}
    assert len(ready["listIds"]) == 1
    for ev in events:
        for banned in ("price", "rationale", "items", "productId", "name"):
            assert banned not in ev["data"]


# ─────────── degrade 3종 ───────────


async def test_search_failed_emits_error() -> None:
    """검색 실패 → error SEARCH_FAILED 로 종결(products.ready·done 없음)."""
    events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=FakeLLM(), search=_failing_search, push_fn=_RecordingPush()
        )
    )
    types = _types(events)
    assert types[-1] == "error"
    assert "products.ready" not in types
    assert "done" not in types
    err = events[-1]["data"]
    assert err["code"] == "SEARCH_FAILED"


async def test_rerank_failure_degrades_to_search_order() -> None:
    """rerank 실패 시 검색순서 상위 N 으로 degrade — products.ready 유지, done stop."""
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(rerank_error=True),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=push,
        )
    )
    types = _types(events)
    assert "error" not in types
    assert "products.ready" in types
    assert types[-1] == "done"
    # 검색 순서(101,102,103) 상위 노출 — rerank 없이도 하드 제약(검색 반영) 유지.
    assert _only_list(push.pushes[0]).product_ids == [101, 102, 103]
    # degrade 경로엔 rerank rationale 이 없으므로 reasons 는 빈 배열(계약상 선택 필드, 이슈 #61).
    assert _only_list(push.pushes[0]).reasons == []


# ─────────── degrade 고지 (#133) ───────────


async def test_rerank_fallback_discloses_quality_drop() -> None:
    """rerank 폴백은 품질 저하를 **고지한다** — 평상시 문구와 구분되어야 한다(#133).

    개인화와 상품별 근거가 통째로 사라지는데 종전 문구("요청하신 조건으로 찾은 상품들이에요")는
    정상 경로와 구분되지 않아, 오히려 조건에 맞게 골라준 것처럼 읽혔다. 판매자에는
    degrade 정직성 게이트(verifier.check_degrade_disclosed)가 있는데 구매자에만 없던 비대칭.
    """
    settings = get_settings()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(rerank_error=True),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert settings.rerank_fallback_notice in texts
    # 회귀 가드 — 평상시와 구분 불가하던 종전 문구가 다시 새면 안 된다.
    assert "요청하신 조건으로 찾은" not in texts
    assert _types(events)[-1] == "done"  # degrade 는 error 가 아니라 done(§3.3)


async def test_scored_schema_error_uses_existing_search_order_degrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "structured")
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "evaluations": [
                        {
                            "productId": 101,
                            "intentFit": 5,
                            "needFit": 3,
                            "profileFit": 0,
                        }
                    ]
                }
            ),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=push,
        )
    )

    assert _only_list(push.pushes[0]).product_ids == [101, 102, 103]
    assert _only_list(push.pushes[0]).reasons == []
    texts = [event["data"].get("text", "") for event in events if event["type"] == "token"]
    assert settings.rerank_fallback_notice in texts
    assert _types(events)[-1] == "done"


async def test_partial_scored_recovery_keeps_turn_healthy_and_candidate_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "structured")
    monkeypatch.setattr(settings, "rerank_grounding_arm", "current")
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "evaluations": [
                        {
                            "productId": 999,
                            "intentFit": 4,
                            "needFit": 3,
                            "profileFit": 0,
                            "rationale": "외부 상품",
                        },
                        {
                            "productId": 102,
                            "intentFit": 4,
                            "needFit": 3,
                            "profileFit": 0,
                            "rationale": "유효 상품",
                        },
                    ],
                    "overallComment": "정상 scored 결과",
                }
            ),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=push,
        )
    )

    pushed_ids = _only_list(push.pushes[0]).product_ids
    assert pushed_ids == [102, 101, 103]
    assert set(pushed_ids) <= {101, 102, 103}
    texts = [event["data"].get("text", "") for event in events if event["type"] == "token"]
    assert settings.rerank_fallback_notice not in texts
    assert "정상 scored 결과" in texts


async def test_scored_invalid_grounding_does_not_change_pushed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agents.buyer.recommendation.rerank_grounding import NEUTRAL_RATIONALE

    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "structured")
    monkeypatch.setattr(settings, "rerank_grounding_arm", "validated")
    products = [product.model_copy(update={"review_count": 20}) for product in DEFAULT_PRODUCTS]
    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "evaluations": [
                        {
                            "productId": 102,
                            "intentFit": 4,
                            "needFit": 3,
                            "profileFit": 0,
                            "rationale": "리뷰가 많아요",
                            "reasonCode": "REVIEW_MANY",
                            "evidenceFields": ["ratingLevel"],
                        },
                        {
                            "productId": 101,
                            "intentFit": 3,
                            "needFit": 3,
                            "profileFit": 0,
                            "rationale": "평점이 높아요",
                            "reasonCode": "RATING_HIGH",
                            "evidenceFields": ["ratingLevel"],
                        },
                        {
                            "productId": 103,
                            "intentFit": 2,
                            "needFit": 3,
                            "profileFit": 0,
                            "rationale": "중립",
                            "reasonCode": "NO_VERIFIABLE_EVIDENCE",
                            "evidenceFields": [],
                        },
                    ],
                    "overallComment": "추천이에요",
                    "overallClaims": [],
                }
            ),
            search=_make_search(products),
            push_fn=push,
        )
    )

    entry = _only_list(push.pushes[0])
    assert entry.product_ids == [102, 101, 103]
    assert {reason.product_id: reason.reason for reason in entry.reasons}[102] == NEUTRAL_RATIONALE


async def test_rerank_fallback_discloses_for_guest_too() -> None:
    """게스트 턴에도 **같은** 고지가 나간다 — 문안이 프로필 유무에 의존하지 않는다(#133).

    문안을 "취향"으로 쓰지 않은 이유가 이것이다. 게스트는 프로필이 없어 평상시에도 취향
    반영이 없으므로 "취향까지 반영하지 못했다"가 참이 되지 않는다. 반면 추천 이유는
    프로필과 무관하게 폴백에서 항상 사라지므로 두 신원 모두에게 참이다.
    """
    settings = get_settings()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _guest(),
            llm=FakeLLM(rerank_error=True),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert settings.rerank_fallback_notice in texts


async def test_degrade_notice_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    """config 주입 문구도 _strip_unsafe 를 통과한다(#67 규약).

    운영자 주입 값이라 소스 리터럴이 아니다 — 정상 경로의 overall_comment 와 같은 정제를 받는다.
    """
    settings = get_settings()
    # zero-width space·RTL override 는 소스에서 눈에 보이지 않아 편집 중 조용히 사라질 수 있다.
    # 이름을 붙여 두면 주입부와 단언부가 같은 문자를 가리킴이 드러나고, 하나가 지워지면
    # NameError 로 즉시 깨진다(리터럴을 양쪽에 흩어 두면 조용히 통과한다).
    zwsp, rlo = "​", "‮"
    monkeypatch.setattr(settings, "rerank_fallback_notice", f"추천 이유\n정리 실패{zwsp}{rlo} 안내")
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(rerank_error=True),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "추천 이유 정리 실패 안내" in texts  # 개행은 단일 공백으로 접힘
    for banned in ("\n", zwsp, rlo):
        assert banned not in texts


def test_degrade_notice_cannot_be_disabled_by_empty_value() -> None:
    """고지 문구를 비우면 **기동 실패**한다 (#133, PR #235 리뷰).

    초판은 "빈 문자열 = 고지 끄기"를 운영 롤백 수단으로 뒀는데, 그건 이슈가 요구한 "문안 config
    주입"을 넘어 **정직성 자체를 옵션으로** 만든 것이었다 — api-spec §3.3 이 발신을 규정하는데
    환경변수 한 줄로 #133 이 조용히 되돌려진다. 문안은 튜너블이고 발신 여부는 아니다.
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    for field in ("rerank_fallback_notice", "push_skipped_notice"):
        with pytest.raises(ValidationError, match="must not be empty"):
            Settings(_env_file=None, **{field: ""})

    # 정제 후 비는 값도 같은 구멍이다 — zero-width 만 든 문자열은 min_length 를 통과한다.
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings(_env_file=None, rerank_fallback_notice="​‮")

    # dedup 고지는 계약이 요구하지 않는다 — 빈 값이 정상적인 의사표현이다.
    assert Settings(_env_file=None, dedup_skipped_notice="").dedup_skipped_notice == ""


async def test_overall_comment_markdown_collapses_to_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[이슈 #570] LLM overallComment 가 `## 제목\\n| a | b |` 처럼 마크다운·개행을 실어도
    실제 token.text 는 개행 0개인 한 줄로 나간다 — `_strip_unsafe` 가 **공백류(개행 포함)만**
    단일 공백으로 접을 뿐, `#`·`|`·`-` 같은 문법 문자 자체는 지우지 않는다는 것도 함께 못 박는다
    (아래 단언의 "## 제목 …"이 그 증거 — 마크다운이 "접히는" 게 아니라 개행만 접혀 표·코드펜스
    처럼 여러 줄이 필요한 구성만 구조적으로 성립하지 못하게 된다). api-spec §3.1 `token` 렌더링
    방식 두 번째 불릿(LLM 자유 문장은 프롬프트로만 금지하고 결정론적으로 보장하지 않는다)의
    근거가 되는 회귀다.
    """
    monkeypatch.setattr(get_settings(), "rerank_grounding_arm", "current")
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "ranked": [
                        {"productId": 101, "rationale": "가성비가 좋아요"},
                        {"productId": 102, "rationale": "음질이 우수해요"},
                    ],
                    "overallComment": "## 제목\n| a | b |\n- 목록1\n- 목록2",
                }
            ),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "\n" not in texts
    assert "## 제목 | a | b | - 목록1 - 목록2" in texts


async def test_push_skipped_notice_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """push 실패 안내도 config 주입이다 — 문구 정책을 한 곳에 모은다(#133)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "push_skipped_notice", "목록 준비 지연 안내")
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_failing_push,
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "목록 준비 지연 안내" in texts
    assert _types(events)[-1] == "done"


async def test_push_failure_skips_products_ready() -> None:
    """push 실패 시 products.ready 를 emit 하지 않고 done 으로 종료(§3.3)."""
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_failing_push,
        )
    )
    types = _types(events)
    assert "products.ready" not in types
    assert types[-1] == "done"
    assert "error" not in types


async def test_push_failure_does_not_persist_last_reco_for_search_path() -> None:
    """[#435 W5] push 실패 턴은 `last_reco` 를 저장하지 않는다 — 미노출 상품이 담기는 것을 막는
    **의도된 동작**이다(패킷 §4 W5). 바꾸지 않고 테스트로 고정만 한다."""
    from app.agents.buyer.cart.state import get_cart_store

    request = _req(thread_id="push-fail-search-435")
    events = await _collect(
        run_buyer_turn(
            request,
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_failing_push,
        )
    )
    assert "products.ready" not in _types(events)
    key = await _thread_key(request, _member())
    cart_store = await get_cart_store()
    assert await cart_store.get_last_reco(key) == []
    assert await cart_store.get_push_failed(key) is True


async def test_push_failed_marker_write_failure_does_not_break_search_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#468 I-21] 실패 사실 기록이 실패해도 목록 전달 실패 턴은 기존 degrade로 끝난다."""
    from app.agents.buyer.cart.state import CartStateStore

    async def fail_set_push_failed(self, key):  # noqa: ANN001
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(CartStateStore, "set_push_failed", fail_set_push_failed)
    events = await _collect(
        run_buyer_turn(
            _req(thread_id="push-fail-marker-write"),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_failing_push,
        )
    )

    assert "products.ready" not in _types(events)
    assert _types(events)[-1] == "done"


async def test_profile_name_falls_back_only_for_legacy_or_malformed_name_presence_flag() -> None:
    """[#468 I-17] 명시적 False 만 category 폴백을 버리고 구 행·이상값은 #435 동작을 유지한다.

    이 함수가 플래그를 무시하면 False 행이 "생활용품"을 이름으로 되돌려준다. 반대로
    truthiness 판정으로 바꾸면 0·문자열 같은 손상 값을 가진 기존 행의 이름 공급이 죽는다.
    """
    from app.agents.buyer.recommendation.no_condition import name_from_artifact
    from app.pipelines.artifact_store import CatalogArtifact, EXTRAS_NAME_PRESENT_KEY

    def artifact(extras: dict) -> CatalogArtifact:
        return CatalogArtifact(
            product_id=101, search_doc="생활용품\n설명", embedding=[], extras=extras
        )

    assert name_from_artifact(artifact({EXTRAS_NAME_PRESENT_KEY: False})) == ""
    assert name_from_artifact(artifact({EXTRAS_NAME_PRESENT_KEY: True})) == "생활용품"
    assert name_from_artifact(artifact({})) == "생활용품"
    assert name_from_artifact(artifact({EXTRAS_NAME_PRESENT_KEY: 0})) == "생활용품"
    assert name_from_artifact(artifact({EXTRAS_NAME_PRESENT_KEY: "false"})) == "생활용품"


# ─────────── zero-result / fallback ───────────


async def test_zero_result_done() -> None:
    """검색 0건 → zero_result done(오류 아님), products.ready 없음."""
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(_req(), _member(), llm=FakeLLM(), search=_make_search([]), push_fn=push)
    )
    types = _types(events)
    assert "products.ready" not in types
    assert "error" not in types
    assert types[-1] == "done"
    done = events[-1]["data"]
    assert done["finishReason"] == "zero_result"
    assert push.pushes == []  # push 미호출


async def test_general_intent_uses_fallback() -> None:
    """intent=general → fallback token + done, conditions/products.ready 없음."""
    llm = FakeLLM(decompose={"intent": "general", "reply": "안녕하세요! 무엇을 도와드릴까요?"})
    events = await _collect(
        run_buyer_turn(
            _req(message="오늘 날씨 어때?"),
            _member(),
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    types = _types(events)
    assert "conditions" not in types
    assert "products.ready" not in types
    assert types[-1] == "done"
    token = next(e for e in events if e["type"] == "token")["data"]
    assert "안녕하세요" in token["text"]


# ─────────── LLM 미구성 / decompose 실패 ───────────


async def test_llm_unavailable_when_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 미구성(키 없음)이면 네트워크 없이 즉시 LLM_UNAVAILABLE error."""
    import app.agents.buyer.graph as bg

    monkeypatch.setattr(bg, "get_llm", lambda: None)
    events = await _collect(run_buyer_turn(_req(), _member()))
    assert _types(events) == ["error"]
    assert events[0]["data"]["code"] == "LLM_UNAVAILABLE"


async def test_decompose_error_maps_to_llm_code() -> None:
    """decompose 실패는 LLM_UNAVAILABLE, 타임아웃 메시지는 LLM_TIMEOUT 로 매핑."""
    ev1 = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose_error=True),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    assert ev1[-1]["type"] == "error" and ev1[-1]["data"]["code"] == "LLM_UNAVAILABLE"

    ev2 = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose_error=True, timeout=True),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    assert ev2[-1]["data"]["code"] == "LLM_TIMEOUT"


def test_rerank_prompt_forbids_markdown_in_overall_comment() -> None:
    """[이슈 #570] `overallComment` 는 4종 밖 마크다운을 실을 수 있어 시스템 프롬프트가 금지
    문장을 담고 있어야 한다 — 누가 조용히 지우면 이 트립와이어가 깨진다."""
    from app.agents.buyer.recommendation.rerank import _SYSTEM

    assert "마크다운을 쓰지 마세요" in _SYSTEM
    assert "overallComment" in _SYSTEM.split("마크다운을 쓰지 마세요", 1)[0].rsplit("\n", 1)[-1]


# ─────────── rerank 후보 부분집합 / 멀티턴 ───────────


async def test_rerank_ids_subset_of_candidates() -> None:
    """rerank 가 후보 외 id 를 내면 코드가 제거하고 유효 id 만 push (REQ-REC-081)."""
    push = _RecordingPush()
    llm = FakeLLM(
        rerank={
            "ranked": [
                {"productId": 999, "rationale": "환각"},
                {"productId": 101, "rationale": "ok"},
            ],
            "overallComment": "c",
        }
    )
    await _collect(
        run_buyer_turn(
            _req(), _member(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    ids = _only_list(push.pushes[0]).product_ids
    assert 999 not in ids  # 후보 외 id 제거(REQ-REC-081)
    assert ids[0] == 101  # rerank 유효 산출이 선두, 나머지는 expose_min 보충


async def test_rerank_sends_nondisplay_numbers_as_tiers() -> None:
    """[#171 PR#172, #173] rerank LLM 입력의 비표시 수치는 정확한 숫자가 아니라 등급이다.

    정확한 price·rating·reviewCount 를 LLM 에 주면 근거문에 숫자를 흘려 CH-5 표시값과 어긋날 수
    있어 등급만 준다 → 흘릴 숫자 자체가 없다(유출 원천 차단). 정확한 값은 원본에 남는다.
    """
    import json as _json

    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    candidates = [
        SpringProduct(
            product_id=1,
            name="무리뷰",
            price=39000,
            rating=0.0,
            review_count=0,
            category="c",
            brand="b",
        ),
        SpringProduct(
            product_id=2,
            name="리뷰있음",
            price=41000,
            rating=4.2,
            review_count=10,
            category="c",
            brand="b",
        ),
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=8
    )
    _, user = llm.calls[-1]
    payload = _json.loads(user.split("CANDIDATES: ", 1)[1])
    by_id = {c["productId"]: c for c in payload}
    # 등급으로 전달 — 정확한 숫자 키(rating/reviewCount)는 없다.
    assert by_id[1]["ratingLevel"] == "평가없음"  # review_count==0 → 데이터 부재(#171)
    assert by_id[1]["reviewLevel"] == "없음"
    assert by_id[1]["priceLevel"] == "보통"
    assert by_id[2]["ratingLevel"] == "높음"  # 4.2 → 높음
    assert by_id[2]["reviewLevel"] == "보통"  # 10 → 보통
    assert by_id[2]["priceLevel"] == "보통"
    assert "price" not in by_id[1]
    assert "rating" not in by_id[1] and "reviewCount" not in by_id[1]
    # 정확한 숫자(price 39000·41000, rating 4.2)는 프롬프트에 등장하지 않는다(흘릴 값 없음).
    assert "39000" not in user and "41000" not in user and "4.2" not in user


async def test_rerank_price_level_uses_group_median_ratios() -> None:
    """[#173] priceLevel 은 전체 후보 중앙값 대비 상대 등급이다."""
    import json as _json

    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    candidates = [
        SpringProduct(product_id=i, name=f"p{i}", price=price)
        for i, price in enumerate([10000, 50000, 100000, 1000000], start=1)
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=8
    )
    _, user = llm.calls[-1]
    payload = _json.loads(user.split("CANDIDATES: ", 1)[1])
    assert {c["productId"]: c["priceLevel"] for c in payload} == {
        1: "매우저렴",
        2: "저렴",
        3: "비쌈",
        4: "매우비쌈",
    }


async def test_rerank_price_level_keeps_tightly_clustered_prices_normal() -> None:
    """[#173] 차이가 미미하면 등급을 만들지 않는다 — 분위수의 거짓 우열을 피하는 의도된 붕괴."""
    import json as _json

    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    candidates = [
        SpringProduct(product_id=i, name=f"p{i}", price=price)
        for i, price in enumerate([29000, 30000, 31000, 32000], start=1)
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=8
    )
    _, user = llm.calls[-1]
    payload = _json.loads(user.split("CANDIDATES: ", 1)[1])
    assert {c["priceLevel"] for c in payload} == {"보통"}


async def test_rerank_price_level_uses_separate_need_medians() -> None:
    """[#173] 혼합 상품 후보는 need 별 중앙값으로 비교해 카테고리 가격대를 섞지 않는다."""
    import json as _json

    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    prices = [1200000, 1500000, 1800000, 10000, 20000, 30000, 40000, 50000]
    candidates = [
        SpringProduct(product_id=i, name=f"p{i}", price=price)
        for i, price in enumerate(prices, start=1)
    ]
    need_of = {1: "노트북", 2: "노트북", 3: "노트북", 4: "마우스", 5: "마우스", 6: "마우스"}
    await rerank(
        llm,
        query="q",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=8,
        need_of=need_of,
        per_need=3,
    )
    _, user = llm.calls[-1]
    payload = _json.loads(user.split("CANDIDATES: ", 1)[1])
    assert {c["productId"]: c["priceLevel"] for c in payload} == {
        1: "저렴",
        2: "보통",
        3: "비쌈",
        4: "매우저렴",
        5: "보통",
        6: "매우비쌈",
        7: "보통",
        8: "보통",
    }


async def test_rerank_price_level_handles_missing_and_uninformative_groups() -> None:
    """[#173] 가격 부재·비양수 중앙값은 정보없음, 단일 유효 가격은 보통이다."""
    import json as _json

    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    candidates = [
        SpringProduct(product_id=1, name="none-a", price=None),
        SpringProduct(product_id=2, name="none-b", price=None),
        SpringProduct(product_id=3, name="single", price=50000),
        SpringProduct(product_id=4, name="zero", price=0),
    ]
    need_of = {1: "missing", 2: "missing", 3: "single", 4: "zero"}
    await rerank(
        llm,
        query="q",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=8,
        need_of=need_of,
        per_need=2,
    )
    _, user = llm.calls[-1]
    payload = _json.loads(user.split("CANDIDATES: ", 1)[1])
    assert {c["productId"]: c["priceLevel"] for c in payload} == {
        1: "정보없음",
        2: "정보없음",
        3: "보통",
        4: "정보없음",
    }


def _levels(llm) -> dict[int, str]:
    """마지막 rerank 호출의 CANDIDATES 에서 productId → priceLevel 을 뽑는다(#236)."""
    import json as _json

    _, user = llm.calls[-1]
    return {c["productId"]: c["priceLevel"] for c in _json.loads(user.split("CANDIDATES: ", 1)[1])}


async def test_rerank_price_level_groups_by_category_without_needs() -> None:
    """[#236] need_of 가 없어도 후보의 category 별 중앙값으로 등급을 매긴다.

    대분류 leg 1개로 검색해도 I-1 응답 `[].categoryName` 은 leaf 라(api-spec §4.6) 가격 스케일이
    다른 상품군이 한 후보군에 섞인다. 수정 전에는 전 후보가 한 그룹(전역 median 90,000)이라
    브랜드PC 3건이 전부 '매우비쌈', 노트북가방이 '매우저렴' 으로 쏠렸다.
    """
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    rows = [
        (1, 1_200_000, "브랜드PC"),
        (2, 1_500_000, "브랜드PC"),
        (3, 1_800_000, "브랜드PC"),
        (4, 70_000, "SSD"),
        (5, 90_000, "SSD"),
        (6, 110_000, "SSD"),
        (7, 30_000, "노트북가방"),
        (8, 45_000, "노트북가방"),
        (9, 60_000, "노트북가방"),
    ]
    candidates = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=price, categoryName=category)
        for pid, price, category in rows
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=9
    )
    # leaf 마다 저렴/보통/비쌈 이 독립적으로 갈린다 — 상품군 간 스케일이 섞이지 않았다는 뜻.
    assert _levels(llm) == {
        1: "저렴",
        2: "보통",
        3: "비쌈",
        4: "저렴",
        5: "보통",
        6: "비쌈",
        7: "저렴",
        8: "보통",
        9: "비쌈",
    }


async def test_rerank_price_level_treats_empty_need_of_as_no_needs() -> None:
    """[#236 PR#274 리뷰] 빈 `need_of` dict 는 `None` 과 똑같이 "니즈 없음"으로 본다.

    `is not None` 으로 판정하면 빈 dict 가 "니즈 있음"으로 새어 전 후보가 `need_of.get()` → `None`
    단일 그룹으로 묶이고, 이 이슈가 고치려는 버그가 그대로 재발한다. 반면 `rerank()` 본문의
    `need` 필드·`NEEDS` 지시는 truthy 판정이라 프롬프트만 "니즈 없음"처럼 나가 둘이 갈린다.
    기대값은 `test_rerank_price_level_groups_by_category_without_needs`(need_of 미전달)와 같다.
    """
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    rows = [
        (1, 1_200_000, "브랜드PC"),
        (2, 1_500_000, "브랜드PC"),
        (3, 1_800_000, "브랜드PC"),
        (4, 70_000, "SSD"),
        (5, 90_000, "SSD"),
        (6, 110_000, "SSD"),
    ]
    candidates = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=price, categoryName=category)
        for pid, price, category in rows
    ]
    await rerank(
        llm,
        query="q",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=6,
        need_of={},
        per_need=3,
    )
    assert _levels(llm) == {1: "저렴", 2: "보통", 3: "비쌈", 4: "저렴", 5: "보통", 6: "비쌈"}
    # 프롬프트도 "니즈 없음" 경로 그대로여야 한다 — 그룹핑과 프롬프트가 갈리지 않는다.
    _, user = llm.calls[-1]
    assert "NEEDS" not in user and '"need"' not in user


async def test_rerank_price_level_marks_lone_category_as_unknown() -> None:
    """[#236] 비교 대상이 없는 그룹은 '정보없음' — 전역 중앙값으로 폴백하지 않는다.

    전역 중앙값은 후보 전체가 섞인 값이라(여기선 645,000) 거기로 폴백하면 SSD 90,000 이
    '매우저렴' 이 되어 이 이슈가 고치려는 왜곡을 그 그룹에만 다시 씌운다.
    """
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    rows = [
        (1, 1_200_000, "브랜드PC"),
        (2, 1_500_000, "브랜드PC"),
        (3, 1_800_000, "브랜드PC"),
        (4, 20_000, "마우스"),
        (5, 30_000, "마우스"),
        (6, 90_000, "SSD"),  # 자기 카테고리에 혼자 — 비교 대상 없음
    ]
    candidates = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=price, categoryName=category)
        for pid, price, category in rows
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=6
    )
    levels = _levels(llm)
    assert levels[6] == "정보없음"
    # 나머지 그룹은 자기 중앙값을 그대로 쓴다 — 싱글턴 처리가 다른 그룹을 오염시키지 않는다.
    assert levels == {
        1: "저렴",
        2: "보통",
        3: "비쌈",
        4: "저렴",
        5: "비쌈",
        6: "정보없음",
    }


async def test_rerank_price_level_group_min_size_is_configurable(monkeypatch) -> None:
    """[#236] 그룹 하한은 config 주입이다 — 1 로 낮추면 싱글턴도 자기 중앙값(항상 '보통')을 쓴다."""
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    monkeypatch.setattr(get_settings(), "price_group_min_size", 1)
    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    rows = [(1, 1_200_000, "브랜드PC"), (2, 1_800_000, "브랜드PC"), (3, 90_000, "SSD")]
    candidates = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=price, categoryName=category)
        for pid, price, category in rows
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=3
    )
    # 하한이 해제돼 SSD 가 '정보없음' 대신 자기 자신 대비 등급('보통')을 받는다.
    assert _levels(llm)[3] == "보통"


async def test_rerank_price_level_treats_blank_category_as_one_unknown_group() -> None:
    """[#236] category 가 None·빈문자열·공백이면 하나의 '미상' 버킷으로 합친다.

    BE 자유 문자열이라 `""` 가 실제로 도달하는데, 갈라지면 각 조각이 하한 미달로 떨어져
    멀쩡한 비교 표본이 통째로 '정보없음' 이 된다(아래 2·3 번이 그 증거).
    """
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    rows = [
        (1, 10_000, None),
        (2, 50_000, ""),
        (3, 100_000, "   "),
        (4, 1_000_000, None),
        (5, 1_500_000, "브랜드PC"),
        (6, 1_600_000, "브랜드PC"),
        (7, 1_700_000, "브랜드PC"),
    ]
    candidates = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=price, categoryName=category)
        for pid, price, category in rows
    ]
    await rerank(
        llm, query="q", candidates=candidates, profile_summary=None, tier="smart", expose_max=7
    )
    # 미상 4건이 한 버킷(median 75,000)이라 네 등급이 모두 갈린다. 쪼개졌다면 2·3 이 '정보없음'.
    assert _levels(llm) == {
        1: "매우저렴",
        2: "저렴",
        3: "비쌈",
        4: "매우비쌈",
        5: "보통",
        6: "보통",
        7: "보통",
    }


async def test_rerank_price_level_ignores_category_when_needs_present() -> None:
    """[#236] need_of 가 있으면 category 는 무시한다 — 니즈 경계는 상위 판정이라 권위가 높다.

    기대값은 `test_rerank_price_level_uses_separate_need_medians`(#173)와 완전히 동일하다.
    판별 장치는 **7·8 의 category 를 니즈 이름과 같은 `"노트북"` 으로 둔 것**이다 — `_need_label`
    이 leg 의 canonical category 를 라벨로 쓰기도 해(graph.py) 실제로 일어나는 충돌이다.
    `need_of.get(...) or category` 같은 `or` 배선이면 니즈 미매핑인 7·8 이 category 를 타고
    '노트북' 그룹에 합류해 그 중앙값이 1,200,000 으로 끌려 내려가고 id 1 이 '보통' 이 된다.
    하한도 need 경로엔 적용되지 않아 7·8(유효 price 2건)이 자기 중앙값으로 '보통' 을 지킨다.
    """
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    rows = [
        (1, 1_200_000, "노트북"),
        (2, 1_500_000, "노트북"),
        (3, 1_800_000, "노트북"),
        (4, 10_000, "마우스"),
        (5, 20_000, "마우스"),
        (6, 30_000, "마우스"),
        (7, 40_000, "노트북"),  # need_of 미매핑 + 니즈 이름과 같은 category
        (8, 50_000, "노트북"),
    ]
    candidates = [
        SpringProduct(product_id=pid, name=f"p{pid}", price=price, categoryName=category)
        for pid, price, category in rows
    ]
    need_of = {1: "노트북", 2: "노트북", 3: "노트북", 4: "마우스", 5: "마우스", 6: "마우스"}
    await rerank(
        llm,
        query="q",
        candidates=candidates,
        profile_summary=None,
        tier="smart",
        expose_max=8,
        need_of=need_of,
        per_need=3,
    )
    assert _levels(llm) == {
        1: "저렴",
        2: "보통",
        3: "비쌈",
        4: "매우저렴",
        5: "보통",
        6: "매우비쌈",
        7: "보통",
        8: "보통",
    }


def test_rerank_prompt_lists_all_tier_return_values() -> None:
    """[#171 PR#172 리뷰⑦] 프롬프트 enum 이 실제 티어 반환값을 모두 포함한다.

    _review_tier 는 review_count is None(BE 미전송)에 '정보없음'을 반환하는데, 이 값이 프롬프트
    reviewLevel 목록에 없으면 LLM 이 예고 못 받은 값을 만나 임의 해석·근거 날조할 수 있다. 실제
    반환값 집합과 프롬프트 enum 을 일치시켜 드리프트를 막는다(rating 은 이미 일치).
    """
    from app.agents.buyer.recommendation.rerank import (
        _SYSTEM,
        _price_tier,
        _rating_tier,
        _review_tier,
    )
    from app.core.config import get_settings
    from app.schemas.spring import SpringProduct

    s = get_settings()

    def _p(**kw) -> SpringProduct:
        return SpringProduct(product_id=1, name="x", **kw)

    rating_vals = {
        _rating_tier(_p(rating=r, review_count=rc), s)
        for r, rc in [(None, 5), (0.0, 0), (0.0, 5), (3.5, 5), (4.2, 5), (4.8, 5)]
    }
    review_vals = {_review_tier(_p(review_count=rc), s) for rc in [None, 0, 3, 10, 50, 200]}
    price_vals = {
        _price_tier(price, median, s)
        for price, median in [
            (None, 100.0),
            (10, None),
            (0, 0.0),
            (50, 100.0),
            (80, 100.0),
            (100, 100.0),
            (120, 100.0),
            (160, 100.0),
        ]
    }
    for v in rating_vals | review_vals | price_vals:
        assert v in _SYSTEM, f"티어값 {v!r} 이 프롬프트 enum 에 없음"


async def test_price_tiering_does_not_mutate_product_or_filter_values() -> None:
    """[#173] 티어화는 LLM 입력 전용이며 원본 price·Spring maxPrice 정확값은 유지한다."""
    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.spring_client import _search_query_params

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    products = [
        SpringProduct(product_id=1, name="a", price=39000),
        SpringProduct(product_id=2, name="b", price=41000),
    ]
    await rerank(
        llm, query="q", candidates=products, profile_summary=None, tier="smart", expose_max=8
    )
    assert [product.price for product in products] == [39000, 41000]
    assert _search_query_params(ProductSearchFilters(price_max=39000))["maxPrice"] == 39000


def test_sanitize_reason_strips_control_and_format_chars() -> None:
    """_sanitize_reason 은 비-whitespace 제어문자(NUL/ESC/DEL)·zero-width·bidi 포맷 문자를 제거한다.

    `\\s` 로는 안 걸리는 표시 조작/주입 문자를 신뢰경계 전에 실제로 벗긴다(§4.2 이슈 #61 보안).
    """
    from app.agents.buyer.recommendation.graph import _sanitize_reason

    dirty = "방수\x1b[31m등급\x00이\x7f 높아요​‮"
    clean = _sanitize_reason(dirty, 200)
    for ch in ("\x1b", "\x00", "\x7f", "​", "‮"):
        assert ch not in clean
    # 제어/포맷 문자만 타깃 — 정상 한글·기호 텍스트는 보존.
    assert "방수" in clean and "등급" in clean and "높아요" in clean


def test_strip_unsafe_removes_controls_and_preserves_normal_text() -> None:
    """공용 정제는 위험 문자·공백류만 정리하고 정상 한글·기호는 보존한다(이슈 #67)."""
    from app.agents.buyer.recommendation.graph import _strip_unsafe

    assert _strip_unsafe("  정상\n문장\t(1~2문장)\u200b\u202e  ") == "정상 문장 (1~2문장)"


def test_strip_unsafe_multiline_preserves_structural_newlines() -> None:
    """장문용 조합은 같은 위험 문자를 제거하면서 마크다운 구조 개행은 보존한다."""
    from app.core.text import _strip_unsafe_multiline

    dirty = "# 제목\x1b[31m\n\n- 첫째\t항목\u200b\u202e\r\n   기대 효과: 유지\n- 둘째"
    assert _strip_unsafe_multiline(dirty) == (
        "# 제목[31m\n\n- 첫째 항목\n   기대 효과: 유지\n- 둘째"
    )


def test_sanitize_reason_nonpositive_cap_blocks() -> None:
    """max_len<=0(오설정)이면 방어캡이 원문을 차단한다 — 경계값에서 무력화되지 않음(PR #66 리뷰)."""
    from app.agents.buyer.recommendation.graph import _sanitize_reason

    text = "가나다라마바사"  # 7자
    assert _sanitize_reason(text, 0) == ""  # 0 = 사실상 차단(빈 문자열 → reasons 에서 생략)
    assert _sanitize_reason(text, -5) == ""  # 음수도 통과 안 함
    assert len(_sanitize_reason(text, 3)) <= 3  # 작은 양수 상한은 지켜짐


async def test_reason_sanitized_and_capped_before_push() -> None:
    """reason 은 push 전 정제된다 — 개행/제어문자 제거 + 안전 상한 truncate (이슈 #61 보안).

    rerank rationale 은 판매자 입력(상품명·브랜드)에 영향받는 자유 텍스트라 신뢰경계를 넘기 전에
    방어한다. 정상 40자 reason 은 무영향, 비정상 초장문/개행만 차단.
    """
    settings = get_settings()
    long_reason = "방수\n등급이\t높아요 " + ("가" * (settings.reason_max_len + 50))
    push = _RecordingPush()
    llm = FakeLLM(
        rerank={
            "ranked": [{"productId": 101, "rationale": long_reason}],
            "overallComment": "c",
        }
    )
    await _collect(
        run_buyer_turn(
            _req(), _member(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    reason_by_id = {r.product_id: r.reason for r in _only_list(push.pushes[0]).reasons}
    sent = reason_by_id[101]
    assert "\n" not in sent and "\t" not in sent  # 개행/제어문자 제거
    assert len(sent) <= settings.reason_max_len  # 안전 상한 이내


async def test_overall_comment_sanitized_without_reason_length_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overall_comment 는 SSE 직전 위험 문자만 제거하고 reason 전용 길이 캡은 적용하지 않는다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_grounding_arm", "current")
    comment = "추천\n총평\u200b\u202e " + ("가" * (settings.reason_max_len + 20))
    llm = FakeLLM(
        rerank={
            "ranked": [{"productId": 101, "rationale": "정상 근거"}],
            "overallComment": comment,
        }
    )

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token.startswith("추천 총평 ")
    assert "\n" not in token and "\u200b" not in token and "\u202e" not in token
    assert len(token) > settings.reason_max_len  # overall_comment 에 reason 캡을 재사용하지 않음


async def test_validated_overall_comment_uses_final_view_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_grounding_arm", "validated")
    monkeypatch.setattr(settings, "expose_min", 2)
    monkeypatch.setattr(settings, "expose_max", 2)
    products = [
        SpringProduct(product_id=101, name="A", price=10_000, rating=4.8, review_count=20),
        SpringProduct(product_id=102, name="B", price=12_000, rating=4.5, review_count=10),
    ]
    model_comment = "모델 자유문장은 노출되면 안 돼요"
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "ranked": [
                        {
                            "productId": 101,
                            "rationale": "평점이 높아요",
                            "reasonCode": "RATING_HIGH",
                            "evidenceFields": ["ratingLevel"],
                        },
                        {
                            "productId": 102,
                            "rationale": "평점이 높아요",
                            "reasonCode": "RATING_HIGH",
                            "evidenceFields": ["ratingLevel"],
                        },
                    ],
                    "overallComment": model_comment,
                    "overallClaims": [
                        {
                            "claimCode": "ALL_RATING_HIGH",
                            "scope": "FINAL_EXPOSED_PRODUCTS",
                            "subjectProductIds": [101, 102],
                            "evidenceFields": ["ratingLevel"],
                        }
                    ],
                }
            ),
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )

    texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert "평점 정보가 높은 상품들만 골랐어요." in texts
    assert all(model_comment not in text for text in texts)


async def test_validated_overall_comment_checks_post_fill_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_grounding_arm", "validated")
    monkeypatch.setattr(settings, "expose_min", 3)
    monkeypatch.setattr(settings, "expose_max", 3)
    push = _RecordingPush()
    products = [
        SpringProduct(product_id=101, name="A", rating=4.8, review_count=20),
        SpringProduct(product_id=102, name="B", rating=4.5, review_count=10),
        SpringProduct(product_id=103, name="C", rating=3.0, review_count=5),
    ]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "ranked": [
                        {
                            "productId": 101,
                            "rationale": "평점이 높아요",
                            "reasonCode": "RATING_HIGH",
                            "evidenceFields": ["ratingLevel"],
                        },
                        {
                            "productId": 102,
                            "rationale": "평점이 높아요",
                            "reasonCode": "RATING_HIGH",
                            "evidenceFields": ["ratingLevel"],
                        },
                    ],
                    "overallComment": "모두 평점이 높아요",
                    "overallClaims": [
                        {
                            "claimCode": "ALL_RATING_HIGH",
                            "scope": "FINAL_EXPOSED_PRODUCTS",
                            "subjectProductIds": [101, 102],
                            "evidenceFields": ["ratingLevel"],
                        }
                    ],
                }
            ),
            search=_make_search(products),
            push_fn=push,
        )
    )

    assert _only_list(push.pushes[0]).product_ids == [101, 102, 103]
    texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert NEUTRAL_OVERALL_COMMENT in texts
    assert all("모두 평점이 높아요" not in text for text in texts)


@pytest.mark.parametrize("arm", ["current", "prompt_only"])
async def test_non_validated_arms_keep_model_overall_comment(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    monkeypatch.setattr(get_settings(), "rerank_grounding_arm", arm)
    model_comment = f"{arm} 모델 코멘트"
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(
                rerank={
                    "ranked": [
                        {
                            "productId": 101,
                            "rationale": "근거",
                            "reasonCode": "NO_VERIFIABLE_EVIDENCE",
                            "evidenceFields": [],
                        }
                    ],
                    "overallComment": model_comment,
                    "overallClaims": [],
                }
            ),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )

    texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert model_comment in texts


async def test_recommendation_boundaries_apply_unicode_sequence_policy(monkeypatch) -> None:
    """Spring reason과 SSE 총평이 같은 등록 보존·비정상 제거 정책을 따른다."""
    # 모델 자유 reason 자체의 Unicode 경계 테스트라 C 템플릿으로 덮지 않고 A rollback을 쓴다.
    monkeypatch.setattr(get_settings(), "rerank_grounding_arm", "current")
    push = _RecordingPush()
    llm = FakeLLM(
        rerank={
            "ranked": [
                {
                    "productId": 101,
                    "rationale": "추천 ❤️ A\ufe0fB\U000e0061",
                }
            ],
            "overallComment": "총평 ❤️ X\ufe0fY\U000e0061 㐂\U000e0100",
        }
    )

    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=push,
        )
    )

    reason_by_id = {
        reason.product_id: reason.reason for reason in _only_list(push.pushes[0]).reasons
    }
    assert reason_by_id[101] == "추천 ❤️ AB"
    token = next(event for event in events if event["type"] == "token")["data"]["text"]
    assert token == "총평 ❤️ XY 㐂\U000e0100"


async def test_general_reply_and_condition_chips_strip_unsafe_text() -> None:
    """LLM 일반답변과 조건 칩의 노출 문자열은 SSE 경계에서 정제된다."""
    general = FakeLLM(decompose={"intent": "general", "reply": "안녕\n하세요\u200b\u202e!"})
    general_events = await _collect(
        run_buyer_turn(
            _req(message="인사"),
            _member(),
            llm=general,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    general_text = next(e for e in general_events if e["type"] == "token")["data"]["text"]
    assert general_text == "안녕 하세요!"

    recommend = FakeLLM(
        decompose={
            "intent": "recommend",
            "categoryQueries": [{"category": "여행\n용품\u200b\u202e", "query": "여행"}],
            "filters": {"brand": ["정상\t브랜드"]},
            "case": 2,
        }
    )
    recommend_events = await _collect(
        run_buyer_turn(
            _req(thread_id="unsafe-condition"),
            _member(),
            llm=recommend,
            search=_make_search([]),
            push_fn=_RecordingPush(),
        )
    )
    chips = next(e for e in recommend_events if e["type"] == "conditions")["data"]["chips"]
    assert chips[0]["label"] == "카테고리 · 여행 용품"
    assert chips[0]["value"] == "여행 용품"
    assert chips[1]["label"] == "정상 브랜드"
    # [이슈 #434, §3.1 v0.32.14 정정] brand 칩 value 는 스칼라다(리스트 아님) — 단일 값이어도.
    assert chips[1]["value"] == "정상 브랜드"


async def test_multiturn_filters_persisted_and_fed_back() -> None:
    """1턴 병합 필터가 스레드 스토어(신원 스코프)에 저장되고 2턴 decompose 로 다시 주입된다."""
    llm = FakeLLM()
    ident = _member()
    await _collect(
        run_buyer_turn(
            _req(), ident, llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=_RecordingPush()
        )
    )

    key = await _thread_key(_req(), ident)
    thread_store = await get_thread_store()
    stored = await thread_store.get(key)
    assert stored is not None and stored.category == "무선이어폰"

    # 2턴 — decompose user 프롬프트에 직전 필터(PRIOR_FILTERS)가 실렸는지 확인.
    llm.calls.clear()
    await _collect(
        run_buyer_turn(
            _req(message="그중에 5만원 이하"),
            ident,
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    decompose_calls = [u for (m, u) in llm.calls if m == "fast"]
    assert decompose_calls and "무선이어폰" in decompose_calls[0]


async def test_thread_store_scoped_by_session_context() -> None:
    """서로 다른 세션 context가 같은 threadId를 써도 필터가 섞이지 않는다."""
    a = Identity(user_id="A", is_guest=False, seller_id=None, subject="A")
    request_a = _req(session_id="session-a", thread_id="shared")
    await _collect(
        run_buyer_turn(
            request_a,
            a,
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    thread_store = await get_thread_store()
    key_a = await _thread_key(request_a, a)
    b = Identity(user_id="B", is_guest=False, seller_id=None, subject="B")
    key_b = await _thread_key(_req(session_id="session-b", thread_id="shared"), b)
    assert await thread_store.get(key_a) is not None
    assert await thread_store.get(key_b) is None


# ─────────── 검색 사후필터 (search_service) ───────────


async def test_search_catalog_post_filters_exclude_and_rating() -> None:
    """BE I-1 엔 dedup·평점 파라미터 없음 → search_catalog 가 사후 제외한다(C-15)."""
    from app.schemas.spring import ProductSearchFilters
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    # 101(4.5)·102(4.2)·103(3.9) 중 exclude 101 + rating_min 4.0 → 102 만.
    res = await search_catalog(
        ProductSearchFilters(rating_min=4.0), exclude_product_ids=[101], backend=FakeBackend()
    )
    assert [p.product_id for p in res.products] == [102]


async def test_search_catalog_rating_filter_preserves_unrated() -> None:
    """[#100 P0] 평점 하한 사후필터는 '반증된 것만' 제거한다.

    rating 이 있고 미달인 상품(3.9)은 버리되, rating=None 신상품(리뷰 없음)은
    데이터 부재일 뿐 미달이 반증된 게 아니므로 후보에 보존해 rerank 가 판단하게 한다.
    """
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=201, name="신상품", rating=None, category="c", brand="b"),
        SpringProduct(product_id=202, name="저평점", rating=3.9, category="c", brand="b"),
        SpringProduct(product_id=203, name="고평점", rating=4.5, category="c", brand="b"),
    ]
    res = await search_catalog(ProductSearchFilters(rating_min=4.0), backend=FakeBackend(products))
    # 무평점(201) 보존, 저평점(202) 탈락, 고평점(203) 통과.
    assert [p.product_id for p in res.products] == [201, 203]


async def test_search_catalog_rating_filter_distinguishes_no_review() -> None:
    """[#171] reviewCount 로 '리뷰 없어 rating=0'과 '리뷰 있고 하한 미달'을 구분한다.

    - reviewCount=0(리뷰 없음)의 rating=0 은 데이터 부재 → 보존(rerank 판단에 위임).
    - reviewCount>0·rating<하한 은 반증된 낮은 평점 → 탈락.
    - reviewCount=None(BE 미전송) 은 기존 동작(rating 이 지배) 으로 폴백.
    - rating=None 무평점은 여전히 보존.
    """
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(
            product_id=301, name="무리뷰0점", rating=0.0, review_count=0, category="c", brand="b"
        ),
        SpringProduct(
            product_id=302, name="리뷰저평점", rating=3.9, review_count=12, category="c", brand="b"
        ),
        SpringProduct(
            product_id=303, name="리뷰고평점", rating=4.5, review_count=30, category="c", brand="b"
        ),
        SpringProduct(
            product_id=304,
            name="rc미전송저평점",
            rating=3.9,
            review_count=None,
            category="c",
            brand="b",
        ),
        SpringProduct(
            product_id=305, name="무평점", rating=None, review_count=0, category="c", brand="b"
        ),
    ]
    res = await search_catalog(ProductSearchFilters(rating_min=4.0), backend=FakeBackend(products))
    # 무리뷰 0점(301) 보존, 리뷰 저평점(302) 탈락, 고평점(303) 통과,
    # reviewCount 미전송 저평점(304) 폴백 탈락, 무평점(305) 보존.
    assert [p.product_id for p in res.products] == [301, 303, 305]


def test_i1_envelope_preserves_rerank_fields() -> None:
    """[#100 P0/P1] BE I-1 실제 envelope({success, data:[...]}) 파싱 계약 테스트.

    rerank·예산검증 입력 필드(price·rating·summary·attributes)와 별칭 필드
    (categoryName·brandName)가 파싱 후 보존됨을 고정한다. SpringProduct 에 필드가 없으면
    Pydantic 이 조용히 버리는 사고(summary·attributes 유실 P0)의 재발 방지 가드다.
    """
    from app.services.spring_client import _parse_search_response

    raw = {
        "success": True,
        "data": [
            {
                "productId": 1,
                "name": "린넨 셔츠",
                "price": 29900,
                "rating": 4.8,
                "summary": "시원한 여름 린넨 셔츠",
                "attributes": {"소재": "린넨", "핏": "오버핏"},
                "categoryName": "여성의류",
                "brandName": "더센트",
            }
        ],
    }
    result = _parse_search_response(raw)
    assert len(result.products) == 1
    p = result.products[0]
    assert p.price == 29900  # 예산검증(verifiedSum §3.1)·maxPrice 판정
    assert p.rating == 4.8  # 평점 사후필터·rerank 신호
    assert p.summary == "시원한 여름 린넨 셔츠"
    assert p.attributes == {"소재": "린넨", "핏": "오버핏"}
    assert p.category == "여성의류"  # categoryName 별칭
    assert p.brand == "더센트"  # brandName 별칭


def test_i1_envelope_parses_review_count() -> None:
    """[#171] I-1 응답의 reviewCount 가 SpringProduct.review_count 로 파싱된다.

    reviewCount 는 rating 과 짝지어 '리뷰 없어 0'과 '리뷰 있고 저평점'을 가르는 판별자다.
    """
    from app.services.spring_client import _parse_search_response

    raw = {
        "success": True,
        "data": [
            {"productId": 1, "name": "무리뷰", "rating": 0.0, "reviewCount": 0},
            {"productId": 2, "name": "리뷰있음", "rating": 4.2, "reviewCount": 37},
        ],
    }
    products = _parse_search_response(raw).products
    assert products[0].review_count == 0
    assert products[1].review_count == 37


def test_i1_envelope_parses_option_names_and_total_count() -> None:
    """[#278] I-1 options/optionCount 를 이름 배열과 개수(int)로 수신한다(의미는 #508 개정 —
    api-spec §4.6, 여기서는 파싱만 고정한다)."""
    from app.services.spring_client import _parse_search_response

    product = _parse_search_response(
        {
            "success": True,
            "data": [
                {
                    "productId": 1,
                    "name": "린넨 셔츠",
                    "options": ["화이트/M", "화이트/L", "블랙/M"],
                    "optionCount": 5,
                }
            ],
        }
    ).products[0]
    assert product.options == ["화이트/M", "화이트/L", "블랙/M"]
    assert product.option_count == 5


def test_i1_envelope_allows_missing_option_fields() -> None:
    """[#278] 두 선택 필드가 없는 기존 I-1 응답도 그대로 파싱한다."""
    from app.services.spring_client import _parse_search_response

    product = _parse_search_response(
        {"success": True, "data": [{"productId": 1, "name": "린넨 셔츠"}]}
    ).products[0]
    assert product.options is None
    assert product.option_count is None


def test_i1_options_over_20_preserve_product_and_unconsumed_metadata() -> None:
    """[#278] 송신 상한 drift가 미소비 options 때문에 I-1 상품 전체를 제거하지 않는다."""
    from app.services.spring_client import _parse_search_response

    option_names = [f"옵션-{i}" for i in range(21)]
    product = _parse_search_response(
        {
            "success": True,
            "data": [{"productId": 1, "name": "상품", "options": option_names}],
        }
    ).products[0]
    assert product.product_id == 1
    assert product.options == option_names


def test_i1_option_count_rejects_negative_value() -> None:
    """[#278] optionCount 는 음수가 될 수 없다."""
    import pytest
    from pydantic import ValidationError

    from app.services.spring_client import _parse_search_response

    with pytest.raises(ValidationError):
        _parse_search_response(
            {"success": True, "data": [{"productId": 1, "name": "상품", "optionCount": -1}]}
        )


def test_i1_attributes_accepts_non_string_values() -> None:
    """[PR#127 리뷰] attributes 값이 문자열이 아니어도(bool·숫자) 파싱이 실패하지 않는다.

    dict[str, str] 로 엄격하면 {"방수": true} 같은 값 1건이 SpringProduct.model_validate 를
    ValidationError 로 터뜨려 검색 전체(수십 건)가 SEARCH_FAILED 로 낙성한다 — attributes 소비는
    #101 이라 지금 값 타입을 강제할 이유가 없고, 오히려 전체 검색을 무너뜨릴 리스크가 크다.
    """
    from app.services.spring_client import _parse_search_response

    raw = {
        "success": True,
        "data": [{"productId": 1, "name": "우산", "attributes": {"방수": True, "소재": "나일론"}}],
    }
    result = _parse_search_response(raw)
    assert len(result.products) == 1  # 값 타입 때문에 드롭되지 않음
    assert result.products[0].attributes == {"방수": True, "소재": "나일론"}


def test_i1_parse_skips_malformed_item_keeps_valid() -> None:
    """[PR#127 리뷰] 후보 1건이 스키마 위반이어도 나머지 정상 후보는 반환한다.

    단일 list comprehension 이면 1건 ValidationError 가 리스트 전체 생성을 실패시켜
    SEARCH_FAILED 로 이어진다 — 멀쩡한 수십 건까지 통째로 버려진다. 항목별 검증으로
    실패분만 skip(로그)하고 나머지를 보존한다.
    """
    from app.services.spring_client import _parse_search_response

    raw = {
        "success": True,
        "data": [
            {"productId": 1, "name": "우산"},  # 정상
            {"productId": 2},  # name 누락 → ValidationError
            {"productId": 3, "name": "장화"},  # 정상
        ],
    }
    result = _parse_search_response(raw)
    assert [p.product_id for p in result.products] == [1, 3]  # 2번만 skip
    assert result.total_count == 2


def test_i1_parse_all_non_object_items_fail_closed() -> None:
    """[PR#127 리뷰] data 는 배열인데 원소가 전부 비-object 면 §7 fail-closed(예외)여야 한다.

    필드 결측보다 심각한 최상위 타입 붕괴가 조용한 zero-result 로 새면 안 된다 — non-dict
    항목도 invalid 로 세어, 정상 0건이면 예외를 내 SEARCH_FAILED 로 degrade 한다.
    """
    import pytest
    from pydantic import ValidationError

    from app.services.spring_client import _parse_search_response

    with pytest.raises((ValidationError, ValueError)):
        _parse_search_response({"success": True, "data": ["oops", "oops2"]})


def test_i1_parse_empty_data_is_valid_zero_result() -> None:
    """[PR#127 리뷰] data 가 빈 배열이면 진짜 0건 — fail-closed 아님(예외 없이 빈 결과)."""
    from app.services.spring_client import _parse_search_response

    result = _parse_search_response({"success": True, "data": []})
    assert result.products == []
    assert result.total_count == 0


def test_i1_parse_success_false_fails_closed() -> None:
    """[PR#127 리뷰] 200 이어도 success:false 는 실패 envelope — 정상 0건으로 삼키지 않고 fail-closed(§7).

    fetch_product_changes 가 이미 `data.get("success") is not True` 로 막는 같은 클래스의 실패다.
    """
    import pytest

    from app.services.spring_client import _parse_search_response

    with pytest.raises(ValueError):
        _parse_search_response({"success": False, "data": None, "error": {"code": "X"}})
    # data 에 값이 있어도 success:false 면 실패
    with pytest.raises(ValueError):
        _parse_search_response({"success": False, "data": [{"productId": 1, "name": "x"}]})


def test_i1_parse_top_level_items_without_data_fails_closed() -> None:
    """[PR#127 리뷰] data 키 없이 top-level items 만 오는 레거시 형태도 fail-closed(§7).

    실 BE envelope 는 {success, data:[...]} — data 키 부재는 의심스러운 drift 라 예외로 degrade.
    """
    import pytest

    from app.services.spring_client import _parse_search_response

    with pytest.raises(ValueError):
        _parse_search_response({"success": True, "items": [{"productId": 1, "name": "x"}]})


def test_search_query_params_drops_blank_brands() -> None:
    """[PR#127 리뷰] 빈/공백 브랜드 요소는 걸러낸다(LLM 이 [''] 등을 낼 수 있음)."""
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    params = _search_query_params(ProductSearchFilters(brand=["삼성", "", "  ", "애플"]))
    assert params.get("brandName") == ["삼성", "애플"]  # 빈/공백 제거, 나머지 유지
    # 전부 빈 값이면 brandName 미전송
    params2 = _search_query_params(ProductSearchFilters(brand=["", "  "]))
    assert "brandName" not in params2


def test_search_query_params_omits_semantic_query() -> None:
    """[#101] semantic_query 는 AI 내부(임베딩 재정렬용) 필드 — Spring I-1 로 전송하지 않는다."""
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    params = _search_query_params(
        ProductSearchFilters(keyword="셔츠", semantic_query="시원한 여름 셔츠")
    )
    assert "semanticQuery" not in params
    assert "semantic_query" not in params
    assert params.get("keyword") == "셔츠"  # keyword(상품명 LIKE)는 그대로 전송


def test_attr_conditions_is_ai_internal_not_sent_to_spring() -> None:
    """[PR② #101] attr_conditions(명시 속성조건)는 AI 사후 속성매칭용 내부 필드 — Spring I-1 에 안 나간다.

    semantic_query 처럼 _search_query_params 가 추출하지 않는 와이어 제외 필드라 계약 변경이 없다.
    """
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    f = ProductSearchFilters(keyword="셔츠", attr_conditions={"소재": "린넨", "핏": "오버핏"})
    assert f.attr_conditions == {"소재": "린넨", "핏": "오버핏"}  # 필드가 값을 보관
    params = _search_query_params(f)
    assert "attrConditions" not in params
    assert "attr_conditions" not in params
    assert params.get("keyword") == "셔츠"  # 와이어 필드는 그대로 전송


def test_search_query_params_drops_blank_text_filters() -> None:
    """[PR#127 리뷰] LLM 산출 텍스트 필터(keyword·category·color)의 공백-only 값은 미전송.

    `if filters.X:` 는 빈 문자열('')만 막고 공백(' ')은 truthy 라 통과했다 — brand 와 동일
    근거로 .strip() 가드를 맞춘다. 정상 값은 그대로 전송한다.
    """
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    blank = _search_query_params(ProductSearchFilters(keyword="  ", category="\t", color=" "))
    assert "keyword" not in blank
    assert "categoryName" not in blank
    assert "color" not in blank

    ok = _search_query_params(
        ProductSearchFilters(keyword="셔츠", category="여성의류", color="빨강")
    )
    assert ok.get("keyword") == "셔츠"
    assert ok.get("categoryName") == "여성의류"
    assert ok.get("color") == "빨강"


def test_search_query_params_omits_size() -> None:
    """[2026-07-23, BE 합의] I-1 요청에서 size 제거 — 라운드1 전량 반환, top-K 는 AI 쪽(api-spec §4.6)."""
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    params = _search_query_params(ProductSearchFilters(keyword="무선 이어폰", limit=30))
    assert "size" not in params


def test_search_query_params_sends_color() -> None:
    """[#100 P1] color 조건이 있으면 Spring 요청에 실린다 (BE I-1 attributes LIKE)."""
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    params = _search_query_params(ProductSearchFilters(keyword="원피스", color="빨강"))
    assert params.get("color") == "빨강"


def test_search_query_params_sends_all_brands() -> None:
    """[#100 P1] 다중 브랜드는 전부 brandName 배열로 실린다(반복 파라미터 → BE IN 필터, 방법 D).

    구 brand[0] 만 전송(2번째 이후 유실 + 칩 거짓표시) 폐기. httpx 는 리스트 값을
    brandName=A&brandName=B 반복 파라미터로 직렬화한다.
    """
    from app.schemas.spring import ProductSearchFilters
    from app.services.spring_client import _search_query_params

    params = _search_query_params(ProductSearchFilters(brand=["삼성", "애플"]))
    assert params.get("brandName") == ["삼성", "애플"]


async def test_fanout_legs_rerank_with_leg_specific_semantic_query() -> None:
    """[#101 PR#166 리뷰] fan-out 각 leg 는 자기 leg 검색어를 재정렬 앵커(semantic_query)로 쓴다.

    leg 별 keyword 만 override 하고 semantic_query 는 전역 값 하나로 두면, 모든 leg 가 동일 벡터로
    pgvector 재정렬돼 leg 관련성이 깨진다("유럽여행 준비물"로 여행용품·전자기기·의류를 똑같이 정렬).
    _leg 가 semantic_query 도 leg 값으로 override 하는지 주입 search 가 받은 filters 로 확인한다.
    """
    seen_sq: list[str | None] = []

    async def _spy_search(filters, exclude_product_ids=None):
        seen_sq.append(filters.semantic_query)
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )

    decompose = {
        "intent": "recommend",
        "reply": "",
        "semanticQuery": "유럽여행 준비물",
        "categoryQueries": [
            {"category": "여행용품", "query": "여행 자물쇠"},
            {"category": "전자기기", "query": "여행용 어댑터"},
        ],
        "filters": {},
    }
    await _collect(
        run_buyer_turn(
            _req(),
            _guest(),
            llm=FakeLLM(decompose=decompose),
            search=_spy_search,
            push_fn=_RecordingPush(),
        )
    )
    # 각 leg 가 자기 검색어를 앵커로 — 전역 "유럽여행 준비물" 아님.
    assert set(seen_sq) == {"여행 자물쇠", "여행용 어댑터"}


async def test_fanout_query_null_leg_falls_to_global_not_breadcrumb_canonical() -> None:
    """[#101 PR#166 리뷰] 멀티 fan-out 에서 query=null 인 leg 는 canonical(분류 경로 breadcrumb)이
    아니라 전역 semantic_query(자연어)로 폴백한다.

    canonical 은 "가전 > 이어폰/헤드폰" 같은 분류 경로라 임베딩 앵커로 부적합하다(decompose 의
    cat_signal 이 raw_category 를 배제하는 것과 동일 원칙). query 있는 leg 는 leg 검색어를, query=null
    leg 는 broad 해도 자연어인 전역값을 앵커로 쓴다.
    """
    seen: dict[str, str | None] = {}

    async def _spy_search(filters, exclude_product_ids=None):
        seen[filters.category] = filters.semantic_query
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )

    decompose = {
        "intent": "recommend",
        "reply": "",
        "semanticQuery": "유럽여행 준비물",
        "categoryQueries": [
            {"category": "여행용품", "query": "여행 자물쇠"},
            {"category": "가전 > 이어폰/헤드폰", "query": None},
        ],
        "filters": {},
    }
    await _collect(
        run_buyer_turn(
            _req(),
            _guest(),
            llm=FakeLLM(decompose=decompose),
            search=_spy_search,
            push_fn=_RecordingPush(),
        )
    )
    assert seen["여행용품"] == "여행 자물쇠"  # query 있는 leg → leg 검색어
    # query=null leg → breadcrumb canonical 아니라 전역 자연어.
    assert seen["가전 > 이어폰/헤드폰"] == "유럽여행 준비물"


async def test_single_category_leg_keeps_global_semantic_query() -> None:
    """[#101 PR#166 리뷰] 단일 카테고리(leg 1개)는 전역 semantic_query(가장 풍부한 전체 의도)를
    재정렬 앵커로 유지한다 — leg 검색 키워드로 다운그레이드하지 않는다.

    leg 별 override 는 멀티 카테고리에서 leg 관련성을 살리기 위한 것이라 단일 leg 엔 적용하지
    않는다. 예: global "가성비 좋은 무선 이어폰"(리치)를 leg query "무선 이어폰"으로 낮추지 않는다.
    """
    seen_sq: list[str | None] = []

    async def _spy_search(filters, exclude_product_ids=None):
        seen_sq.append(filters.semantic_query)
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )

    decompose = {
        "intent": "recommend",
        "reply": "",
        "semanticQuery": "가성비 좋은 무선 이어폰",
        "categoryQueries": [{"category": "무선이어폰", "query": "무선 이어폰"}],
        "filters": {},
    }
    await _collect(
        run_buyer_turn(
            _req(),
            _guest(),
            llm=FakeLLM(decompose=decompose),
            search=_spy_search,
            push_fn=_RecordingPush(),
        )
    )
    # 단일 leg — 전역 리치 앵커 유지, leg query("무선 이어폰")로 다운그레이드 안 함.
    assert seen_sq == ["가성비 좋은 무선 이어폰"]


async def test_search_catalog_returns_all_after_postfilter() -> None:
    """[#101] search_catalog 는 더 이상 top-K 절단하지 않는다 — 절단은 graph dedup 이후로 이동했다.

    이전엔 filters.limit 로 dedup **이전**에 절단해, 최근구매 dedup·소모품 억제가 상위 후보에
    몰리면 rerank 입력이 상한 미만이 되는 recall 손실이 있었다(#101). 이제 search_catalog 는
    재정렬·사후필터(dedup 제외·평점 하한)만 하고 전량을 반환하며, 최종 절단(embedding_rerank_limit)은
    graph 가 dedup 이후에 적용한다. total_count 는 사후필터 통과 매칭 수 그대로.
    """
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [SpringProduct(product_id=i, name=f"p{i}", price=1000) for i in range(1, 6)]  # 5개
    res = await search_catalog(
        ProductSearchFilters(limit=2), backend=FakeBackend(products=products)
    )
    # limit=2 여도 절단하지 않는다 — 전량 반환(절단은 graph 몫).
    assert [p.product_id for p in res.products] == [1, 2, 3, 4, 5]
    assert res.total_count == 5


async def test_attr_conditions_hard_filter_excludes_disproven() -> None:
    """[PR②] 명시 속성조건에 반하는 상품(축 있고 값 불일치)은 하드 제외한다."""
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=1, name="a", price=1, attributes={"소재": "린넨"}),
        SpringProduct(product_id=2, name="b", price=1, attributes={"소재": "면"}),  # 반증
    ]
    res = await search_catalog(
        ProductSearchFilters(attr_conditions={"소재": "린넨"}),
        backend=FakeBackend(products=products),
    )
    assert [p.product_id for p in res.products] == [1]


async def test_attr_conditions_preserve_axis_absent() -> None:
    """[PR② — #100 P0 정합] 조건 축이 없는 상품은 '반증 아님'이라 보존한다(rerank 가 판단)."""
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=1, name="a", price=1, attributes={"소재": "린넨"}),
        SpringProduct(product_id=2, name="b", price=1, attributes={"색상": "빨강"}),  # 소재 축 없음
    ]
    res = await search_catalog(
        ProductSearchFilters(attr_conditions={"소재": "린넨"}),
        backend=FakeBackend(products=products),
    )
    assert {p.product_id for p in res.products} == {1, 2}  # 축 부재 2 보존


async def test_color_attr_conditions_preserve_axis_absent_and_exclude_mismatch() -> None:
    """[#461 §4.6 ②] 색상 축 부재는 보존하고, 명시 색상 불일치는 사후필터에서 제외한다."""
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=1, name="무색상 속성 상품", price=1, attributes={"소재": "린넨"}),
        SpringProduct(product_id=2, name="그레이 상품", price=1, attributes={"색상": "그레이"}),
        SpringProduct(product_id=3, name="빨강 상품", price=1, attributes={"색상": "빨강"}),
    ]
    res = await search_catalog(
        ProductSearchFilters(attr_conditions={"색상": "그레이"}),
        backend=FakeBackend(products=products),
    )
    assert {product.product_id for product in res.products} == {1, 2}


async def test_attr_conditions_lenient_match() -> None:
    """[PR②] 관대 매칭 — 부분·대소문자 무시. bool/숫자 값(dict[str,object])도 문자열화 비교."""
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=1, name="a", price=1, attributes={"소재": "린넨 혼방"}),
        SpringProduct(product_id=2, name="b", price=1, attributes={"방수": True}),
    ]
    r1 = await search_catalog(
        ProductSearchFilters(attr_conditions={"소재": "린넨"}),
        backend=FakeBackend(products=products),
    )
    assert 1 in {p.product_id for p in r1.products}  # "린넨" ⊂ "린넨 혼방"
    r2 = await search_catalog(
        ProductSearchFilters(attr_conditions={"방수": "true"}),
        backend=FakeBackend(products=products),
    )
    assert 2 in {p.product_id for p in r2.products}  # bool True ~ "true"


async def test_attr_conditions_numeric_exact_match() -> None:
    """[PR② PR#169 리뷰] 숫자값 조건은 완전 일치 — "1" 이 "100"·"21" 을 부분매칭으로 통과시키지 않는다.

    부분매칭이면 `"1" in "100"` 이 True 라 하드필터 취지가 깨진다(사이즈·용량 등 숫자 축). 문자열
    값은 기존대로 관대 부분매칭.
    """
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=1, name="a", price=1, attributes={"사이즈": "1"}),
        SpringProduct(product_id=2, name="b", price=1, attributes={"사이즈": "100"}),
        SpringProduct(product_id=3, name="c", price=1, attributes={"사이즈": "21"}),
    ]
    res = await search_catalog(
        ProductSearchFilters(attr_conditions={"사이즈": "1"}),
        backend=FakeBackend(products=products),
    )
    assert [p.product_id for p in res.products] == [1]  # 100·21 은 부분포함이어도 제외


async def test_attr_conditions_relax_per_axis_on_zero() -> None:
    """[PR②] 하드 필터가 0건이면 축을 완화해 과다제외를 막는다(완화칩 emit 은 #113 소관).

    축별 완화 — 두 조건 모두 반증(0건)이면 한 축을 빼고 재시도해, 남은 축이라도 만족하는 상품을 살린다.
    """
    from app.schemas.spring import ProductSearchFilters, SpringProduct
    from app.services.search_service import search_catalog
    from tests._fakes import FakeBackend

    products = [
        SpringProduct(product_id=1, name="a", price=1, attributes={"소재": "린넨", "핏": "슬림"}),
        SpringProduct(product_id=2, name="b", price=1, attributes={"소재": "면", "핏": "슬림"}),
    ]
    # {소재:린넨, 핏:오버핏} → 둘 다 0건 → 핏 완화 → 소재=린넨 인 1만(2는 소재 반증 유지)
    res = await search_catalog(
        ProductSearchFilters(attr_conditions={"소재": "린넨", "핏": "오버핏"}),
        backend=FakeBackend(products=products),
    )
    assert [p.product_id for p in res.products] == [1]


async def test_graph_caps_rerank_input_to_embedding_rerank_limit() -> None:
    """[#101] 후보 절단은 search_catalog(사전) 이 아니라 graph 의 dedup 이후에 embedding_rerank_limit
    으로 적용된다 — 절단 위치 이동. dedup 이 상위 후보를 지워도 rerank 입력이 상한까지 채워진다.

    비-fanout 경로(categoryQueries 비움 → merge_cap 미개입)로 격리하고, guest 로 최근구매 dedup 을
    회피해 'graph 가 embedding_rerank_limit 로 절단하는지'만 본다. rerank(smart) 프롬프트의
    CANDIDATES 개수로 rerank 입력 후보 수를 관측한다.
    """
    from app.schemas.spring import SpringProduct

    settings = get_settings()
    cap = settings.embedding_rerank_limit
    # cap 초과 후보 — 서로 다른 category 로 소모품 억제 회피.
    products = [
        SpringProduct(product_id=i, name=f"p{i}", price=1000, category=f"c{i}")
        for i in range(1, cap + 6)
    ]
    llm = FakeLLM(
        decompose={**DEFAULT_DECOMPOSE, "categoryQueries": []},
        rerank={"ranked": [{"productId": 1, "rationale": "좋아요"}], "overallComment": ""},
    )
    await _collect(
        run_buyer_turn(
            _req(), _guest(), llm=llm, search=_make_search(products), push_fn=_RecordingPush()
        )
    )
    smart = [u for t, u in llm.calls if t == "smart"]
    assert smart, "rerank(smart) 호출이 있어야 한다"
    cands = json.loads(smart[0].split("CANDIDATES: ", 1)[1])
    assert len(cands) == cap  # graph 가 embedding_rerank_limit 로 절단
    assert [c["productId"] for c in cands] == list(range(1, cap + 1))  # 검색순서 상위 cap 보존


async def test_pipeline_logs_stage_candidate_counts(caplog) -> None:
    """[#101 #8] 관측성 — 단계별 후보 수(received→after_dedup→compressed→final)를 구조화 로그로 남긴다.

    recall 손실 추적·자원 진단을 위해 파이프라인 깔때기를 한 줄 구조화 로그로 남긴다. 비-fanout·guest
    (dedup 없음)로 received==compressed 를 확인한다.
    """
    import logging

    from app.schemas.spring import SpringProduct

    products = [
        SpringProduct(product_id=i, name=f"p{i}", price=1000, category=f"c{i}") for i in range(1, 6)
    ]
    llm = FakeLLM(
        decompose={**DEFAULT_DECOMPOSE, "categoryQueries": []},
        rerank={"ranked": [{"productId": 1, "rationale": "좋아요"}], "overallComment": ""},
    )
    with caplog.at_level(logging.INFO, logger="app.agents.buyer.recommendation.graph"):
        await _collect(
            run_buyer_turn(
                _req(), _guest(), llm=llm, search=_make_search(products), push_fn=_RecordingPush()
            )
        )
    rec = next((r for r in caplog.records if _event(r, "recommend_pipeline")), None)
    assert rec is not None, "단계별 후보 수 구조화 로그가 있어야 한다"
    assert rec.received == 5  # Spring/merge 수신
    assert rec.after_dedup == 5  # guest → 최근구매 dedup 없음
    assert rec.compressed == 5  # 5 < embedding_rerank_limit → 절단 없음
    assert rec.rerank_degraded is False


def test_search_filters_limit_rejects_negative() -> None:
    """[PR#127 리뷰] limit 은 slice 절단(방식1 VectorSearchBackend 의 over_fetch k)에 쓰이므로 ge=0.

    #101 로 hot path(방식2) 사전 절단은 제거됐지만, 방식1 VectorSearchBackend 는 여전히
    filters.limit*over_fetch 로 top-k slice 를 하므로 음수면 '뒤에서 N개 제외'로 뒤집혀 '≤0 → 0개'
    절단 불변식이 깨진다(형제 category_fanout_* 필드가 PR#73 에서 같은 이유로 ge=0 을 건 것과 정합).
    """
    import pytest
    from pydantic import ValidationError

    from app.schemas.spring import ProductSearchFilters

    with pytest.raises(ValidationError):
        ProductSearchFilters(limit=-1)


# ─────────── 리뷰 수정 회귀 (Fix A~E) ───────────


class _FakeResp:
    def __init__(self, data) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data) -> None:
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, params=None):
        return _FakeResp(self._data)


def test_spring_product_maps_i1_wire_fields() -> None:
    """SpringProduct 가 BE I-1 응답 필드명(categoryName/brandName/originalPrice/imageUrl)을 매핑한다."""
    from app.schemas.spring import SpringProduct

    p = SpringProduct.model_validate(
        {
            "productId": 1,
            "name": "린넨 셔츠",
            "price": 29900,
            "originalPrice": 39900,
            "categoryName": "여성의류",
            "brandName": "더센트",
            "imageUrl": "https://x/1.jpg",
            "rating": 4.8,
        }
    )
    assert p.product_id == 1
    assert p.category == "여성의류"  # categoryName → category (None 유실 방지)
    assert p.brand == "더센트"
    assert p.list_price == 39900
    assert p.main_image == "https://x/1.jpg"


def test_spring_product_preserves_summary_and_attributes() -> None:
    """[#100 P0] BE I-1이 주는 summary·attributes 를 SpringProduct 가 유실하지 않고 보존한다.

    Spring 은 세부조건 후처리·리랭킹(#101 2차 압축)용으로 summary·attributes 를 반환하는데,
    스키마에 필드가 없으면 Pydantic 파싱에서 조용히 제거된다.
    """
    from app.schemas.spring import SpringProduct

    p = SpringProduct.model_validate(
        {
            "productId": 1,
            "name": "린넨 셔츠",
            "summary": "시원한 여름 린넨 셔츠",
            "attributes": {"소재": "린넨", "핏": "오버핏"},
            "categoryName": "여성의류",
            "brandName": "더센트",
        }
    )
    assert p.summary == "시원한 여름 린넨 셔츠"
    assert p.attributes == {"소재": "린넨", "핏": "오버핏"}


async def test_search_products_parses_i1_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_products 가 {success,data:{items}} 응답을 SpringProduct 로 파싱한다(§4.6)."""
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    payload = {
        "success": True,
        "data": {
            "items": [
                {
                    "productId": 1,
                    "name": "셔츠",
                    "price": 29900,
                    "categoryName": "의류",
                    "brandName": "B",
                    "rating": 4.8,
                }
            ]
        },
    }
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _FakeClient(payload))
    res = await sc.search_products(ProductSearchFilters())
    assert len(res.products) == 1
    assert res.products[0].category == "의류" and res.products[0].brand == "B"


async def test_search_products_parses_i1_array_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_products 가 Spring ApiResponse<List> 인 {success,data:[...]} 배열도 파싱한다(§2.3 정합)."""
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    payload = {
        "success": True,
        "data": [
            {
                "productId": 1,
                "name": "셔츠",
                "price": 29900,
                "categoryName": "의류",
                "brandName": "B",
                "rating": 4.8,
            }
        ],
    }
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _FakeClient(payload))
    res = await sc.search_products(ProductSearchFilters())
    assert len(res.products) == 1
    assert res.products[0].category == "의류" and res.products[0].brand == "B"


async def test_search_products_malformed_maps_to_search_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 이지만 스키마 불일치(필수 productId 결측) 응답은 SpringUnavailableError 로 degrade(§7)."""
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    payload = {"success": True, "data": {"items": [{"name": "x"}]}}  # productId 없음
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _FakeClient(payload))
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())


async def test_search_products_unknown_envelope_fails_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[PR#127 리뷰] 미인식 envelope drift 는 조용한 0 건이 아니라 SEARCH_FAILED 로 degrade한다.

    항목 단위 fail-closed 와 동일 원칙(§7) — envelope 자체가 어긋나면(예: data 가 미인식
    형태) 정상 0건과 구분되지 않는 빈 결과 대신 예외를 내 상위가 SEARCH_FAILED 로 낸다.
    경고 로그도 남긴다.
    """
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    payload = {"success": True, "data": {"products": [{"productId": 1}]}}  # 미인식 형태
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _FakeClient(payload))
    with caplog.at_level("WARNING"):
        with pytest.raises(SpringUnavailableError):
            await sc.search_products(ProductSearchFilters())
    assert "미인식" in caplog.text


async def test_search_products_parses_bare_list_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """래퍼 없는 최상위 배열 응답도 후보로 수용한다(envelope 방어)."""
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    payload = [
        {"productId": 7, "name": "모자", "price": 9900, "categoryName": "잡화", "brandName": "B"}
    ]
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _FakeClient(payload))
    res = await sc.search_products(ProductSearchFilters())
    assert [p.product_id for p in res.products] == [7]


async def test_search_products_missing_data_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[PR#127 리뷰] data 키가 없는 envelope drift 는 조용한 0 이 아니라 SEARCH_FAILED 로 degrade(§7)."""
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _FakeClient({"success": True}))
    with caplog.at_level("WARNING"):
        with pytest.raises(SpringUnavailableError):
            await sc.search_products(ProductSearchFilters())
    assert "data 키" in caplog.text


# ─────────── I-1 검색 재시도 (#133) ───────────

_I1_OK = {
    "success": True,
    "data": {"items": [{"productId": 101, "name": "P101", "price": 1000}], "totalCount": 1},
}


def _counting_client(monkeypatch: pytest.MonkeyPatch, *responses):
    """호출 순서대로 응답/예외를 내는 MockTransport 클라이언트 — 호출 횟수를 센다.

    `_FakeClient` 에는 카운터가 없어 재시도 검증에 쓸 수 없다. httpx 실경로를 그대로 태우려고
    MockTransport 를 쓴다(저장소 관례 — respx 미설치, tests/unit/test_buyer_tracing.py 전례).
    """
    import httpx

    import app.services.spring_client as sc

    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        item = responses[min(len(calls), len(responses) - 1)]
        calls.append(1)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(
        sc,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="http://spring.test", transport=httpx.MockTransport(_handler)
        ),
    )
    return calls


async def test_search_retries_once_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """1차 타임아웃 → 2차 성공이면 정상 결과를 낸다(#133).

    Spring 타임아웃은 3s 로 짧아 일시 지연이 재시도로 살아난다. LLM 만 30s+1회 재시도를 갖고
    검색은 0회였던 비대칭을 해소한다 — SPEC-RECOMMEND-001 §오류처리가 이미 규정한 동작이다.
    """
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    calls = _counting_client(
        monkeypatch, httpx.TimeoutException("slow"), httpx.Response(200, json=_I1_OK)
    )
    result = await sc.search_products(ProductSearchFilters())
    assert [p.product_id for p in result.products] == [101]
    assert len(calls) == 2


async def test_search_gives_up_after_configured_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """재시도까지 실패하면 SpringUnavailableError — 상위가 SEARCH_FAILED 로 낸다."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    calls = _counting_client(monkeypatch, httpx.TimeoutException("slow"))
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 2  # 1차 + 재시도 1회, 무한 재시도 아님


async def test_search_retries_on_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """연결 오류도 재시도 대상이다(일시 장애)."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    calls = _counting_client(
        monkeypatch, httpx.ConnectError("refused"), httpx.Response(200, json=_I1_OK)
    )
    assert len((await sc.search_products(ProductSearchFilters())).products) == 1
    assert len(calls) == 2


async def test_search_retries_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx 는 업스트림 일시 장애라 재시도한다."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    calls = _counting_client(monkeypatch, httpx.Response(503), httpx.Response(200, json=_I1_OK))
    assert len((await sc.search_products(ProductSearchFilters())).products) == 1
    assert len(calls) == 2


async def test_search_retries_on_remote_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """서버가 응답 도중 끊는 경우도 재시도한다 (#133 자체 점검).

    httpx 계층에서 `RemoteProtocolError` 는 `NetworkError` 의 **하위가 아니라 형제**다
    (둘 다 `TransportError` 직계). "연결 오류"로만 묶으면 이 흔한 일시 장애가 재시도에서
    빠지므로 판정에 따로 적었다 — 초판이 실제로 이걸 빠뜨렸다.
    """
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    assert not isinstance(httpx.RemoteProtocolError("x"), httpx.NetworkError)  # 전제 고정
    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    calls = _counting_client(
        monkeypatch,
        httpx.RemoteProtocolError("server disconnected"),
        httpx.Response(200, json=_I1_OK),
    )
    assert len((await sc.search_products(ProductSearchFilters())).products) == 1
    assert len(calls) == 2


async def test_retry_log_labels_disconnect_as_connection_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """응답 중단을 `malformed_response` 로 오분류하지 않는다 (PR #235 리뷰).

    `RemoteProtocolError` 는 `NetworkError` 의 형제라 분류 함수에서 빠지면 마지막 return 으로
    떨어져 "스키마 불일치"로 찍힌다. 재시도는 제대로 되는데 로그만 거짓말하는 상태라,
    운영자가 Spring 응답 계약을 의심하며 없는 문제를 찾게 된다.
    """
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    _counting_client(
        monkeypatch, httpx.RemoteProtocolError("disconnected"), httpx.Response(200, json=_I1_OK)
    )
    with caplog.at_level("WARNING", logger="app.services.spring_client"):
        await sc.search_products(ProductSearchFilters())

    retries = [r for r in caplog.records if r.msg == "spring_search_retry"]
    assert len(retries) == 1
    assert retries[0].statusClass == "connection_error"
    assert "disconnected" not in caplog.text  # 예외 원문은 싣지 않는다(#141)


@pytest.mark.parametrize("status", [408, 429])
async def test_search_retries_transient_4xx(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """408·429 는 4xx 지만 재시도한다 (PR #235 리뷰).

    "4xx 는 다시 보내도 같은 거절"이라는 일반 규칙의 예외다 — 요청 자체는 유효하고 서버·인프라의
    일시 상태일 뿐이라 5xx 와 성격이 같다. 특히 429 는 타임아웃과 달리 **즉시 응답**이라 재시도
    비용이 밀리초여서, 순간적인 레이트 리밋 하나로 턴이 죽는 손실이 훨씬 크다.
    """
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    calls = _counting_client(monkeypatch, httpx.Response(status), httpx.Response(200, json=_I1_OK))
    assert len((await sc.search_products(ProductSearchFilters())).products) == 1
    assert len(calls) == 2


def test_transport_classification_is_shared_between_log_and_trace() -> None:
    """로그와 trace 가 **같은 분류 함수**를 쓴다 (PR #235 리뷰).

    두 곳이 isinstance 를 따로 구현하면 새 예외 타입을 한쪽에만 추가했을 때 라벨이 조용히
    갈린다 — 이 PR 이 RemoteProtocolError 로 그 사고를 실제로 두 번 냈다. 주석 약속 대신
    구조로 고정한다: 전송 계층 실패는 `_transport_status_class` 가 유일한 출처다.
    """
    import httpx

    import app.services.spring_client as sc

    for exc, expected in [
        (httpx.TimeoutException("t"), "timeout"),
        (httpx.ConnectError("c"), "connection_error"),
        (httpx.RemoteProtocolError("d"), "connection_error"),
    ]:
        assert sc._transport_status_class(exc) == expected
        assert sc._failure_status_class(exc) == expected  # 로그가 같은 출처를 쓴다
        assert sc._is_retryable(exc) is True  # 재시도 판정도 같은 출처를 쓴다

    # 전송 계층이 아니면 None — span 은 손대지 않고, 로그는 자체 분기로 내려간다.
    assert sc._transport_status_class(httpx.LocalProtocolError("l")) is None
    assert sc._transport_status_class(ValueError("bad json")) is None


async def test_search_does_not_retry_local_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """우리 요청이 잘못된 경우(LocalProtocolError)는 재시도하지 않는다 — 다시 보내도 같다."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    calls = _counting_client(
        monkeypatch, httpx.LocalProtocolError("bad request"), httpx.Response(200, json=_I1_OK)
    )
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 1


async def test_search_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx 계약 오류는 재시도해도 같은 결과다 — 즉시 실패해 예산을 태우지 않는다(#133)."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    calls = _counting_client(monkeypatch, httpx.Response(400), httpx.Response(200, json=_I1_OK))
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 1


async def test_search_does_not_retry_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 이지만 파싱 불가한 응답도 재시도 대상이 아니다 — 같은 응답이 또 온다."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    calls = _counting_client(
        monkeypatch,
        httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
        httpx.Response(200, json=_I1_OK),
    )
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 1


async def test_search_retry_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """spring_max_retries=0 이면 종전과 같이 1회만 호출한다(롤백 안전성)."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    monkeypatch.setattr(get_settings(), "spring_max_retries", 0)
    calls = _counting_client(monkeypatch, httpx.TimeoutException("slow"))
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 1


async def test_search_retry_default_config_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#394 원복] 기본 설정은 실패 검색을 한 번 재시도해 2회 호출한다."""
    import httpx

    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    calls = _counting_client(monkeypatch, httpx.TimeoutException("slow"))
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 2


async def test_search_retry_zero_retries_config_calls_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """`spring_max_retries=0` 롤백 경로는 검색을 1회만 호출한다."""
    import httpx
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    monkeypatch.setattr(get_settings(), "spring_max_retries", 0)
    calls = _counting_client(monkeypatch, httpx.TimeoutException("slow"))
    with pytest.raises(SpringUnavailableError):
        await sc.search_products(ProductSearchFilters())
    assert len(calls) == 1


async def test_recommendation_deferred_conditions_keeps_search_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#306] 미룬 턴도 다른 턴과 **같은** I-1 재시도를 쓴다 — #277 의 스킵을 원복했다.

    #277 이 그 스킵을 넣은 이유는 미룬 턴의 첫 SSE 가 검색 뒤라 재시도가 first-token 상한을
    넘겼기 때문인데, #396 이 `progress` 를 검색 **앞**으로 보내면서 그 전제가 사라졌다.
    기본 설정(`spring_max_retries=1`, #406 이 #394 를 원복)에서 이 턴은 이제 2회 호출한다 —
    억제가 남아 있으면 1회에 그쳐 이 어설션이 깨진다(회귀 가드).

    같은 이유로 `retrying` progress 가 이 턴에서도 나간다(api-spec §3.1 v0.32.5) —
    v0.32.4 까지는 미룬 턴이 재시도 자체를 안 해 그 프레임이 없었다.
    """
    # [#443] 이 픽스처는 `categoryQueries: []` 인데 발화(`무선 이어폰 추천해줘`)에는 카탈로그
    # 카테고리(`이어폰`)가 있어, 사전 기반 보강이 leg 을 채우면 흐름이 달라진다. 실제 decompose
    # 라면 이 발화에 leg 을 내므로 그 조합은 프로덕션에 없는 픽스처 인공물이다 — 이 테스트의
    # 주제는 I-1 재시도지 leg 산출이 아니므로 보강을 끈다(위 가드와 같은 규약).
    monkeypatch.setattr(get_settings(), "category_leg_injection_enabled", False)
    import httpx

    # [#393] `ratingMin` 만 있는 턴은 payload 기준으로 무필터라 새 가드(A)가 인기 상품으로
    # 돌린다 — 이 테스트의 주제는 I-1 재시도지 후보 소스 선택이 아니므로 새 가드를 끈다.
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls = _counting_client(monkeypatch, httpx.Response(503))
    events = await _collect(
        run_buyer_turn(
            _req(thread_id="deferred-retry-kept"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "filters": {"ratingMin": 4.5},
                    "case": 2,
                }
            ),
        )
    )

    assert len(calls) == 2
    # progress 다회 emit(#396) — 이 decompose 는 categoryQueries 가 없어 카테고리 신호가
    # 전혀 없다(mapping honesty 회귀, #396 라운드 1). mapping 은 안 나가고 analyzing·searching·
    # retrying 이 conditions 앞에 온다(검색이 하드 실패라 relaxing 루프는 안 돈다 — 0건이
    # 아니라 예외로 끝난다).
    assert _types(events) == ["progress", "progress", "progress", "conditions", "error"]
    assert [e["data"]["stage"] for e in events if e["type"] == "progress"] == [
        "analyzing",
        "searching",
        "retrying",
    ]
    assert events[-1]["data"]["code"] == "SEARCH_FAILED"


async def test_recommendation_nondeferred_conditions_keeps_search_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conditions를 먼저 내는 턴은 첫 이벤트 예산 밖이므로 I-1 재시도 1회를 유지한다(#277).

    [#162] `semanticQuery` 를 준다 — 종전에는 `filters: {}` 만으로 이 턴을 만들었는데, 그건 이제
    **조건 없는 발화**로 판정돼 I-1 이 아니라 I-3(인기 상품) 경로를 탄다. 이 테스트의 주제는
    I-1 재시도지 후보 소스 선택이 아니므로, 의미 신호를 줘서 종전 경로를 유지시킨다.
    [#393] `semanticQuery` 는 Spring payload 축이 아니라 여전히 payload 기준으로는 무필터다 —
    새 가드(A)도 함께 끈다.
    """
    # [#443] 이 픽스처는 `categoryQueries: []` 인데 발화(`무선 이어폰 추천해줘`)에는 카탈로그
    # 카테고리(`이어폰`)가 있어, 사전 기반 보강이 leg 을 채우면 흐름이 달라진다. 실제 decompose
    # 라면 이 발화에 leg 을 내므로 그 조합은 프로덕션에 없는 픽스처 인공물이다 — 이 테스트의
    # 주제는 I-1 재시도지 leg 산출이 아니므로 보강을 끈다(위 가드와 같은 규약).
    monkeypatch.setattr(get_settings(), "category_leg_injection_enabled", False)
    import httpx

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls = _counting_client(monkeypatch, httpx.Response(503))
    events = await _collect(
        run_buyer_turn(
            _req(thread_id="nondeferred-retry-kept"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "semanticQuery": "무선 이어폰",
                    "filters": {},
                    "case": 2,
                }
            ),
        )
    )

    assert len(calls) == 2
    # progress 다회 emit(#396) — 이 decompose 는 categoryQueries 가 없어 카테고리 신호가
    # 전혀 없다(mapping honesty 회귀, #396 라운드 1). mapping 은 안 나간다. analyzing 은
    # conditions **앞**(이 턴은 non-deferred라 conditions 가 검색 전에 나간다), searching 은
    # conditions **뒤**(검색 직전 emit 지점). #406 retrying은 실제 재시도 진입 뒤 추가 전용이다.
    assert _types(events) == ["progress", "conditions", "progress", "progress", "error"]
    assert [e["data"]["stage"] for e in events if e["type"] == "progress"] == [
        "analyzing",
        "searching",
        "retrying",
    ]
    assert events[-1]["data"]["code"] == "SEARCH_FAILED"


# [#306] `test_recommendation_deferred_conditions_retry_can_be_restored_by_guard` 는 삭제했다 —
# 그 테스트는 `SEARCH_RETRY_ON_DEFERRED_CONDITIONS=true` 로 억제를 끈 미룬 턴을 쟀는데, 억제
# 기구 자체가 사라져 그 조건이 곧 기본 동작이 됐다. 같은 명제는 위
# `test_recommendation_deferred_conditions_keeps_search_retry` 가 그대로 고정한다.


async def test_recommendation_relaxation_chip_probe_keeps_search_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conditions 뒤 완화 칩 probe는 첫 이벤트 예산 밖이라 I-1 재시도를 유지한다(#277)."""
    # [#443] 이 픽스처는 `categoryQueries: []` 인데 발화(`무선 이어폰 추천해줘`)에는 카탈로그
    # 카테고리(`이어폰`)가 있어, 사전 기반 보강이 leg 을 채우면 흐름이 달라진다. 실제 decompose
    # 라면 이 발화에 leg 을 내므로 그 조합은 프로덕션에 없는 픽스처 인공물이다 — 이 테스트의
    # 주제는 I-1 재시도지 leg 산출이 아니므로 보강을 끈다(위 가드와 같은 규약).
    monkeypatch.setattr(get_settings(), "category_leg_injection_enabled", False)
    import httpx

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    empty = httpx.Response(200, json={"success": True, "data": []})
    calls = _counting_client(
        monkeypatch,
        empty,  # 본 검색
        empty,  # ratingMin 자동 완화 probe
        httpx.Response(503),  # conditions 뒤 priceMax 완화 칩 probe 1차
        httpx.Response(200, json=_I1_OK),  # 완화 칩 probe 재시도
    )
    events = await _collect(
        run_buyer_turn(
            _req(thread_id="relaxation-chip-retry-kept"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "filters": {"ratingMin": 4.5, "priceMax": 50000},
                    "case": 2,
                }
            ),
        )
    )

    assert len(calls) == 4
    # progress 다회 emit(#396) — 이 decompose 는 categoryQueries 가 없어 카테고리 신호가
    # 전혀 없다(mapping honesty 회귀, #396 라운드 1). mapping 은 안 나간다. analyzing·searching·
    # relaxing 3개가 conditions 앞에 온다(본 검색·ratingMin 자동 완화 probe 모두 0건이라
    # relaxing 이 실제로 probe 했다).
    assert _types(events)[:3] == ["progress", "progress", "progress"]
    assert [e["data"]["stage"] for e in events[:3]] == [
        "analyzing",
        "searching",
        "relaxing",
    ]
    assert _types(events)[3] == "conditions"
    assert "suggestions" in _types(events)
    assert _types(events)[-1] == "done"


async def test_expose_min_fill_from_search_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank 가 expose_min 미만을 내면 검색순서로 보충한다(REQ-REC-021 5~8개)."""
    monkeypatch.setattr(get_settings(), "rerank_ranking_arm", "current")
    products = [
        SpringProduct(
            product_id=pid, name=f"P{pid}", price=1000 * pid, rating=4.0, category="c", brand="b"
        )
        for pid in range(201, 207)  # 6개 후보
    ]
    push = _RecordingPush()
    llm = FakeLLM(
        rerank={"ranked": [{"productId": 201, "rationale": "top"}], "overallComment": "c"}
    )
    await _collect(
        run_buyer_turn(_req(), _member(), llm=llm, search=_make_search(products), push_fn=push)
    )
    ids = _only_list(push.pushes[0]).product_ids
    assert ids[0] == 201  # rerank 선두 유지
    assert len(ids) == 5  # expose_min 까지 검색순서로 보충


async def test_push_failure_emits_notice_token() -> None:
    """push 실패 시 목록 지연 안내 token 을 낸다(경로 B 실패 계약, error 아님)."""
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_failing_push,
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "잠시 후" in texts or "문제" in texts
    assert _types(events)[-1] == "done"


# ─────────── 구매 이력 dedup (#4, §4.7 결정 14-F) ───────────

import app.services.spring_client as _sc_mod  # noqa: E402
from app.schemas.spring import OrderHistory, OrderHistoryItem, RecentPurchases  # noqa: E402

_REAL_GET_RECENT = _sc_mod.get_recent_purchases  # autouse 패치 전에 캡처(배선 테스트용)


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject="guest-1")


def _member_num() -> Identity:
    """숫자 sub 회원(실제 JWT sub 는 숫자 BIGINT, §2.6) — dedup 경로 검증용."""
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


def _recording_search(products, sink):
    async def _s(filters, exclude_product_ids=None):
        sink["exclude"] = exclude_product_ids
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _s


def _purchases(*product_ids):
    async def _fn(user_id, status=None):
        return RecentPurchases(
            orders=[
                OrderHistory(
                    order_id=1,
                    ordered_at="2026-07-10T00:00:00",
                    items=[
                        OrderHistoryItem(order_item_id=i, product_id=pid)
                        for i, pid in enumerate(product_ids, 1)
                    ],
                )
            ]
        )

    return _fn


def _fix_now(monkeypatch, when=datetime(2026, 7, 19)):
    monkeypatch.setattr("app.agents.buyer.recommendation.graph._now", lambda: when)


async def test_recommendation_dedups_recent_purchases(monkeypatch: pytest.MonkeyPatch) -> None:
    """회원 최근 구매 productId 는 그래프 사후필터로 후보에서 제외된다(exact 제외, 결정 14-F).

    병렬화로 검색엔 exclude 를 넘기지 않고(그래프에서 제외), 최종 push 에 101 이 빠진다.
    """
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases(101))
    push = _RecordingPush()
    sink: dict = {}
    await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_recording_search(DEFAULT_PRODUCTS, sink),
            push_fn=push,
        )
    )
    assert sink["exclude"] is None  # 검색엔 exclude 미전달(병렬 — 제외는 그래프 사후필터)
    assert 101 not in _only_list(push.pushes[0]).product_ids  # 최근 구매 101 제외
    assert 102 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_skips_dedup_for_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    """게스트는 이력 조회를 스킵하고 제외 없이 추천한다(결정 8)."""
    called = {"n": 0}

    async def _spy(user_id, status=None):
        called["n"] += 1
        return RecentPurchases()

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _spy)
    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(), _guest(), llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    assert called["n"] == 0  # 조회 스킵
    assert 101 in _only_list(push.pushes[0]).product_ids  # 제외 안 됨


async def test_recommendation_degrades_when_purchases_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """이력 조회 실패 시 dedup 없이 추천을 정상 진행한다(degrade, §4.7)."""

    async def _boom(user_id, status=None):
        raise SpringUnavailableError("orders down")

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _boom)
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=push,
        )
    )
    assert 101 in _only_list(push.pushes[0]).product_ids  # dedup 없이 진행
    assert _types(events)[-1] == "done"


async def test_dedup_skip_is_not_disclosed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """이력 조회 실패는 **기본 미고지**다(#133 판단).

    조회 실패는 "중복이 노출됐다"가 아니라 "걸러내지 못했다"라 실제 중복 발생 여부를 알 수 없고,
    rerank 폴백과 달리 거짓 주장을 하고 있지도 않다. 매 턴 붙는 안내는 노이즈가 된다.
    """

    async def _boom(user_id, status=None):
        raise SpringUnavailableError("orders down")

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _boom)
    assert get_settings().dedup_skipped_notice == ""  # 기본값이 곧 미고지 수단
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "최근 구매" not in texts
    assert _types(events)[-1] == "done"


async def test_dedup_skip_discloses_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """문구를 채우면 이력 조회 실패도 고지된다 — 판단을 재배포 없이 되돌리기 위한 여지(#133)."""

    async def _boom(user_id, status=None):
        raise SpringUnavailableError("orders down")

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _boom)
    monkeypatch.setattr(
        get_settings(), "dedup_skipped_notice", "최근 구매 제외를 적용하지 못했어요."
    )
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "최근 구매 제외를 적용하지 못했어요." in texts


async def test_dedup_notice_not_emitted_for_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    """게스트는 **이력이 없는 것**이지 조회에 실패한 게 아니다 — 고지하지 않는다(#133).

    `_fetch_purchases` 는 두 경우 모두 None 을 돌려주므로 호출부에서 구분할 수 없었다.
    degrade 플래그로 갈라야 "없는 기능이 고장났다"는 거짓 고지를 막는다.
    """
    monkeypatch.setattr(
        get_settings(), "dedup_skipped_notice", "최근 구매 제외를 적용하지 못했어요."
    )
    events = await _collect(
        run_buyer_turn(
            _req(),
            _guest(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    texts = " ".join(e["data"].get("text", "") for e in events if e["type"] == "token")
    assert "최근 구매 제외를 적용하지 못했어요." not in texts


async def test_recommendation_degrades_on_non_numeric_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """비숫자 sub 회원은 dedup 없이 진행(int 변환 실패로 죽지 않음)."""
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases(101))
    bad = Identity(user_id="abc", is_guest=False, seller_id=None, subject="abc")
    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(), bad, llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    assert 101 in _only_list(push.pushes[0]).product_ids  # dedup 스킵


async def test_recommendation_search_and_purchases_run_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검색과 이력조회를 병렬 실행한다 — 검색 호출에 exclude 를 넘기지 않는다(§4.7 지연 가드)."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases(101))
    sink: dict = {}
    await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_recording_search(DEFAULT_PRODUCTS, sink),
            push_fn=_RecordingPush(),
        )
    )
    assert sink["exclude"] is None


def test_purchased_product_ids_excludes_canceled_returned() -> None:
    """취소/반품 아이템은 보유분이 아니라 제외 대상에서 뺀다(Claude #19)."""
    rp = RecentPurchases(
        orders=[
            OrderHistory(
                order_id=1,
                ordered_at="2026-07-10T00:00:00",
                items=[
                    OrderHistoryItem(order_item_id=1, product_id=101, status="DELIVERED"),
                    OrderHistoryItem(order_item_id=2, product_id=102, status="CANCELED"),
                    OrderHistoryItem(order_item_id=3, product_id=103, status="CANCELLED"),
                    OrderHistoryItem(order_item_id=4, product_id=104, status="RETURNED"),
                ],
            )
        ]
    )
    # 철자 양쪽(CANCELED/CANCELLED) 모두 제외
    assert rp.purchased_product_ids(exclude_statuses={"CANCELED", "CANCELLED", "RETURNED"}) == {101}


def test_purchased_product_ids_window_excludes_old() -> None:
    """윈도우(since)보다 오래된 구매는 제외 목록에서 뺀다 — 영구 제외 방지(Codex #19)."""
    rp = RecentPurchases(
        orders=[
            OrderHistory(
                order_id=1,
                ordered_at="2026-07-15T00:00:00",
                items=[OrderHistoryItem(order_item_id=1, product_id=101)],
            ),
            OrderHistory(
                order_id=2,
                ordered_at="2025-01-01T00:00:00",
                items=[OrderHistoryItem(order_item_id=2, product_id=102)],
            ),
            OrderHistory(
                order_id=3,
                ordered_at="bad-date",
                items=[OrderHistoryItem(order_item_id=3, product_id=103)],
            ),
        ]
    )
    assert rp.purchased_product_ids(since=datetime(2026, 7, 1)) == {101}  # 오래된 102·불명 103 제외
    assert rp.purchased_product_ids() == {101, 102, 103}  # since 없으면 전체(불명 포함)


async def test_get_recent_purchases_parses_and_collects_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-19 응답을 파싱하고 productId 집합을 모은다(§4.7)."""
    body = {
        "success": True,
        "data": {
            "orders": [
                {
                    "orderId": 1023,
                    "orderedAt": "2026-07-10T14:23:00",
                    "status": "DELIVERED",
                    "items": [
                        {
                            "orderItemId": 2001,
                            "productId": 552,
                            "productName": "무선 키보드",
                            "quantity": 1,
                            "price": 29000,
                            "status": "DELIVERED",
                        }
                    ],
                },
                {
                    "orderId": 1024,
                    "orderedAt": "2026-07-11T09:00:00",
                    "status": "SHIPPING",
                    "items": [
                        {"orderItemId": 2002, "productId": 88},
                        {"orderItemId": 2003, "productId": 552},
                    ],
                },
            ]
        },
    }
    monkeypatch.setattr(_sc_mod, "_client", lambda *, timeout=None: _FakeClient(body))
    res = await _REAL_GET_RECENT(123)
    assert res.purchased_product_ids() == {552, 88}


async def test_get_recent_purchases_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """스키마 불일치(필수 productId 결측)는 SpringUnavailableError 로(호출측 degrade)."""
    body = {
        "success": True,
        "data": {"orders": [{"orderId": 1, "orderedAt": "x", "items": [{"orderItemId": 1}]}]},
    }
    monkeypatch.setattr(_sc_mod, "_client", lambda *, timeout=None: _FakeClient(body))
    with pytest.raises(SpringUnavailableError):
        await _REAL_GET_RECENT(1)


# ─────────── #19 리뷰 2차 회귀 ───────────


def test_parse_ordered_at_normalizes_tz() -> None:
    """aware ordered_at 은 UTC 로 변환 후 naive 화(offset 만 버리지 않음, Claude #19)."""
    from app.schemas.spring import _parse_ordered_at

    # 09:00+09:00 == 00:00 UTC
    assert _parse_ordered_at("2026-07-10T09:00:00+09:00") == datetime(2026, 7, 10, 0, 0, 0)
    assert _parse_ordered_at("2026-07-10T00:00:00") == datetime(
        2026, 7, 10, 0, 0, 0
    )  # naive 그대로
    assert _parse_ordered_at("bad") is None


async def test_recommendation_dedup_empty_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """dedup 로 후보가 전부 제외되면 '조건 바꿔라'가 아니라 원인을 바르게 안내한다(Claude #19)."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases(101, 102, 103)
    )  # DEFAULT 전부 제외
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "최근에 구매" in token
    assert "products.ready" not in _types(events)
    assert events[-1]["data"]["finishReason"] == "zero_result"


async def test_recommendation_skips_dedup_for_seller(monkeypatch: pytest.MonkeyPatch) -> None:
    """판매자 토큰(user_id=sub·seller_id=sub)은 sub 를 memberId 로 쓰지 않는다(IDOR 방지, Claude #19)."""
    called = {"n": 0}

    async def _spy(user_id, status=None):
        called["n"] += 1
        return RecentPurchases()

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _spy)
    seller = Identity(user_id="500", is_guest=False, seller_id="500", subject="500")
    push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(), seller, llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    assert called["n"] == 0  # 판매자 sub 로 I-19 조회 안 함
    assert 101 in _only_list(push.pushes[0]).product_ids  # dedup 미적용


# ─────────── 소모품 카테고리 억제 + 되돌리기 (#4, 결정 14-F) ───────────


def _purchases_cat(*items):
    """items = (productId, category, name) — 카테고리 포함 최근 구매."""

    async def _fn(user_id, status=None):
        return RecentPurchases(
            orders=[
                OrderHistory(
                    order_id=1,
                    ordered_at="2026-07-15T00:00:00",
                    items=[
                        OrderHistoryItem(
                            order_item_id=idx, product_id=pid, category=cat, product_name=name
                        )
                        for idx, (pid, cat, name) in enumerate(items, 1)
                    ],
                )
            ]
        )

    return _fn


def _prod(pid, cat, name="상품"):
    return SpringProduct(
        product_id=pid, name=name, price=10000, rating=4.0, category=cat, brand="b"
    )


def _filtered_repurchase_search(products, calls):
    """재구매·완화 결합 테스트용 — 가격·평점 필터를 실제 적용하고 호출 필터를 기록한다."""

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        kept = [
            product
            for product in products
            if (filters.price_max is None or (product.price or 0) <= filters.price_max)
            and (filters.rating_min is None or (product.rating or 0) >= filters.rating_min)
        ]
        return ProductSearchResult(products=kept, total_count=len(kept))

    return _search


async def test_recommendation_repurchase_restores_exact_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명시 재구매 상품은 최근 구매 exact 제외를 되돌려 다시 추천됨을 보장한다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(
            _req(), _member_num(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    assert 101 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_restores_only_named_consumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명시한 소모품만 exact·카테고리 억제를 면제하고 같은 카테고리의 다른 상품은 억제한다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(900, "조미료", "소금"), _prod(201, "조미료", "후추")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["소금"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 900 in _only_list(push.pushes[0]).product_ids
    assert 201 not in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_persists_across_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """턴1 재구매 지목은 턴2 조건 다듬기에도 exact 제외 면제로 남는다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    request = _req(thread_id="repurchase-persist")
    products = [_prod(101, "무선이어폰", "무선 이어폰 프로"), _prod(102, "무선이어폰")]
    first_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search(products),
            push_fn=first_push,
        )
    )
    second_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={"intent": "recommend", "filters": {"priceMax": 50000}, "case": 2}
            ),
            search=_make_search(products),
            push_fn=second_push,
        )
    )

    assert 101 in _only_list(first_push.pushes[0]).product_ids
    assert 101 in _only_list(second_push.pushes[0]).product_ids


async def test_recommendation_repurchase_persists_when_scoped_refine_carries_relaxation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """완화 승계 턴에서도 직전 재구매 면제와 새 가격 조건을 함께 유지한다."""
    _fix_now(monkeypatch)
    # [#393] 1턴째는 ratingMin 만 있어 payload 기준 무필터다 — 이 테스트의 주제는 완화 승계지
    # 후보 소스 선택이 아니므로 새 가드를 끈다.
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    request = _req(thread_id="repurchase-scoped-relax")
    products = [
        SpringProduct(
            product_id=101,
            name="무선 이어폰 프로",
            price=40000,
            rating=4.2,
            category="무선이어폰",
            brand="b",
        )
    ]
    calls = []
    first_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {"ratingMin": 4.5},
                    "case": 1,
                }
            ),
            search=_filtered_repurchase_search(products, calls),
            push_fn=first_push,
        )
    )

    turn2 = len(calls)
    second_push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(message="그 중에 5만원 이하", thread_id=request.thread_id),
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "scopedToPrevious": True,
                    "filters": {"ratingMin": 4.5, "priceMax": 50000},
                    "case": 2,
                }
            ),
            search=_filtered_repurchase_search(products, calls),
            push_fn=second_push,
        )
    )

    assert 101 in _only_list(first_push.pushes[0]).product_ids
    assert calls[turn2].rating_min == 4.0
    assert calls[turn2].price_max == 50000
    assert second_push.pushes
    assert 101 in _only_list(second_push.pushes[0]).product_ids
    rating_chip = next(
        chip
        for event in events
        if event["type"] == "conditions"
        for chip in event["data"]["chips"]
        if chip["field"] == "ratingMin"
    )
    assert rating_chip["value"] == 4.0


async def test_recommendation_relaxation_probe_applies_persisted_repurchase(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """자동 완화 probe도 지속 재구매 면제를 적용해 count와 실제 노출을 일치시킨다."""
    _fix_now(monkeypatch)
    # [#393] 2턴째는 ratingMin 만 있어 payload 기준 무필터다 — 이 테스트의 주제는 완화 probe의
    # 재구매 면제 적용이지 후보 소스 선택이 아니므로 새 가드를 끈다.
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    request = _req(thread_id="repurchase-relax-probe")
    products = [
        SpringProduct(
            product_id=101,
            name="무선 이어폰 프로",
            price=40000,
            rating=4.2,
            category="무선이어폰",
            brand="b",
        )
    ]
    calls = []
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_filtered_repurchase_search(products, calls),
            push_fn=_RecordingPush(),
        )
    )

    push = _RecordingPush()
    with caplog.at_level("INFO"):
        events = await _collect(
            run_buyer_turn(
                request,
                _member_num(),
                llm=FakeLLM(
                    decompose={
                        "intent": "recommend",
                        "filters": {"ratingMin": 4.5},
                        "case": 2,
                    }
                ),
                search=_filtered_repurchase_search(products, calls),
                push_fn=push,
            )
        )

    assert "products.ready" in _types(events)
    exposed = _only_list(push.pushes[0]).product_ids
    assert exposed == [101]
    pipeline = next(record for record in caplog.records if _event(record, "recommend_pipeline"))
    assert pipeline.after_dedup == len(exposed) == 1


async def test_recommendation_delayed_conditions_survive_repurchase_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """자동 완화 가능 턴의 저장소 실패도 conditions를 막지 않고 이번 턴 지목으로 degrade한다."""

    class BrokenStore:
        async def add(self, key, product_ids, *, cap):
            return await self.get(key)

        async def get(self, key):
            raise RuntimeError("store get failed")

    async def broken_store():
        return BrokenStore()

    _fix_now(monkeypatch)
    monkeypatch.setattr(recommendation_graph, "get_repurchase_store", broken_store)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    product = SpringProduct(
        product_id=101,
        name="무선 이어폰 프로",
        price=40000,
        rating=4.6,
        category="무선이어폰",
        brand="b",
    )
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(thread_id="repurchase-delayed-conditions"),
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {"ratingMin": 4.5},
                    "case": 1,
                }
            ),
            search=_filtered_repurchase_search([product], []),
            push_fn=push,
        )
    )

    assert _types(events).count("conditions") == 1
    assert 101 in _only_list(push.pushes[0]).product_ids
    assert _types(events)[-1] == "done"


async def test_recommendation_delays_conditions_until_search_and_auto_relax_probe_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """자동 완화 가능 턴은 본 검색과 probe 두 I-1 호출이 끝난 뒤 conditions를 낸다(#277)."""
    # [#393] ratingMin 만 있는 턴은 payload 기준으로 무필터다 — 이 테스트의 주제는 본 검색·
    # probe 순서지 후보 소스 선택이 아니므로 새 가드를 끈다.
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    calls = []
    product = SpringProduct(
        product_id=101,
        name="무선 이어폰 프로",
        price=40000,
        rating=4.2,
        category="무선이어폰",
        brand="b",
    )
    conditions_call_counts = []

    async for frame in run_buyer_turn(
        _req(thread_id="first-event-two-searches"),
        _member_num(),
        llm=FakeLLM(
            decompose={
                "intent": "recommend",
                "filters": {"ratingMin": 4.5},
                "case": 2,
            }
        ),
        search=_filtered_repurchase_search([product], calls),
        push_fn=_RecordingPush(),
    ):
        line = frame.strip()
        if line.startswith("data:"):
            event = json.loads(line[len("data:") :].strip())
            if event["type"] == "conditions":
                conditions_call_counts.append(len(calls))

    assert conditions_call_counts == [2]  # 본 검색 0건 + ratingMin 자동 완화 probe


async def test_recommendation_emits_conditions_before_search_when_auto_relax_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relaxation_max_rounds=0이면 ratingMin 턴도 검색 전에 conditions를 낸다(#277)."""
    monkeypatch.setattr(get_settings(), "relaxation_max_rounds", 0)
    calls = []
    conditions_call_counts = []

    async for frame in run_buyer_turn(
        _req(thread_id="first-event-auto-relax-disabled"),
        _member_num(),
        llm=FakeLLM(
            decompose={
                "intent": "recommend",
                "filters": {"ratingMin": 4.5},
                "case": 2,
            }
        ),
        search=_filtered_repurchase_search([], calls),
        push_fn=_RecordingPush(),
    ):
        line = frame.strip()
        if line.startswith("data:"):
            event = json.loads(line[len("data:") :].strip())
            if event["type"] == "conditions":
                conditions_call_counts.append(len(calls))

    assert conditions_call_counts == [0]


async def test_recommendation_persisted_repurchase_is_revalidated_against_recent_purchases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장된 id가 다음 턴 최근 구매 이력에 없으면 면제되지 않는다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    responses = iter(
        [
            await _purchases_cat((101, "조미료", "소금"))("123"),
            await _purchases_cat((102, "조미료", "후추"))("123"),
        ]
    )

    async def changing_purchases(user_id, status=None):
        return next(responses)

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", changing_purchases)
    request = _req(thread_id="repurchase-revalidate")
    products = [
        _prod(101, "조미료", "소금"),
        _prod(102, "조미료", "후추"),
        _prod(103, "주방가전", "믹서"),
    ]
    first_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["소금"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search(products),
            push_fn=first_push,
        )
    )
    second_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(decompose={"intent": "recommend", "filters": {}, "case": 2}),
            search=_make_search(products),
            push_fn=second_push,
        )
    )

    assert 101 in _only_list(first_push.pushes[0]).product_ids
    assert 101 not in _only_list(second_push.pushes[0]).product_ids
    assert 102 not in _only_list(second_push.pushes[0]).product_ids
    assert 103 in _only_list(second_push.pushes[0]).product_ids


async def test_recommendation_repurchase_store_cap_evicts_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """설정 상한 1은 두 번째 지목 시 오래된 첫 id를 다시 exact 제외시킨다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "dedup_repurchase_store_max", 1)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat(
            (101, "무선이어폰", "무선 이어폰 프로"),
            (102, "무선이어폰", "다른 이어폰"),
        ),
    )
    request = _req(thread_id="repurchase-cap")
    products = [
        _prod(101, "무선이어폰", "무선 이어폰 프로"),
        _prod(102, "무선이어폰", "다른 이어폰"),
    ]
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )
    second_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["다른 이어폰"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search(products),
            push_fn=second_push,
        )
    )

    ids = _only_list(second_push.pushes[0]).product_ids
    assert 101 not in ids
    assert 102 in ids


async def test_recommendation_repurchase_store_cap_zero_applies_to_persisted_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """운영 상한을 0으로 낮추면 새 지목 없는 다음 턴부터 지속 면제가 꺼진다."""
    _fix_now(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "dedup_repurchase_store_max", 20)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    request = _req(thread_id="repurchase-cap-zero")
    products = [_prod(101, "무선이어폰", "무선 이어폰 프로"), _prod(102, "무선이어폰")]
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )

    monkeypatch.setattr(settings, "dedup_repurchase_store_max", 0)
    second_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(decompose={"intent": "recommend", "filters": {}, "case": 2}),
            search=_make_search(products),
            push_fn=second_push,
        )
    )

    ids = _only_list(second_push.pushes[0]).product_ids
    assert 101 not in ids
    assert 102 in ids


async def test_recommendation_repurchase_store_reduced_cap_keeps_latest_persisted_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """운영 상한을 20에서 1로 낮추면 새 지목 없이도 최신 id 하나만 면제한다."""
    _fix_now(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "dedup_repurchase_store_max", 20)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat(
            (101, "무선이어폰", "무선 이어폰 프로"),
            (102, "무선이어폰", "다른 이어폰"),
        ),
    )
    request = _req(thread_id="repurchase-cap-reduced")
    products = [
        _prod(101, "무선이어폰", "무선 이어폰 프로"),
        _prod(102, "무선이어폰", "다른 이어폰"),
    ]
    for reference in ("무선 이어폰 프로", "다른 이어폰"):
        await _collect(
            run_buyer_turn(
                request,
                _member_num(),
                llm=FakeLLM(
                    decompose={
                        "intent": "recommend",
                        "repurchaseProducts": [reference],
                        "filters": {},
                        "case": 1,
                    }
                ),
                search=_make_search(products),
                push_fn=_RecordingPush(),
            )
        )

    monkeypatch.setattr(settings, "dedup_repurchase_store_max", 1)
    third_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(decompose={"intent": "recommend", "filters": {}, "case": 2}),
            search=_make_search(products),
            push_fn=third_push,
        )
    )

    ids = _only_list(third_push.pushes[0]).product_ids
    assert 101 not in ids
    assert 102 in ids


@pytest.mark.parametrize("failure_point", ["add", "get"])
async def test_recommendation_repurchase_store_failure_degrades_to_turn_signal(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    """저장소 add/get 실패는 스트림을 끊지 않고 이번 턴 지목을 유지한다."""

    class BrokenStore:
        async def add(self, key, product_ids, *, cap):
            if failure_point == "add":
                raise RuntimeError("store add failed")
            return await self.get(key)

        async def get(self, key):
            if failure_point == "get":
                raise RuntimeError("store get failed")
            return []

    async def broken_store():
        return BrokenStore()

    _fix_now(monkeypatch)
    monkeypatch.setattr(recommendation_graph, "get_repurchase_store", broken_store)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰 프로"))
    )
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(thread_id="repurchase-degrade"),
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["무선 이어폰 프로"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search([_prod(101, "무선이어폰", "무선 이어폰 프로")]),
            push_fn=push,
        )
    )

    assert 101 in _only_list(push.pushes[0]).product_ids
    assert _types(events)[-1] == "done"


async def test_recommendation_persisted_repurchase_stays_product_scoped_for_consumables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """지속 면제는 지목한 소금만 복원하고 같은 조미료의 후추로 번지지 않는다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    request = _req(thread_id="repurchase-consumable-persist")
    products = [_prod(900, "조미료", "소금"), _prod(201, "조미료", "후추")]
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "repurchaseProducts": ["소금"],
                    "filters": {},
                    "case": 1,
                }
            ),
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )
    second_push = _RecordingPush()
    await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(decompose={"intent": "recommend", "filters": {}, "case": 2}),
            search=_make_search(products),
            push_fn=second_push,
        )
    )

    ids = _only_list(second_push.pushes[0]).product_ids
    assert 900 in ids
    assert 201 not in ids


async def test_recommendation_repurchase_prefers_exact_name_over_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """지목과 완전 일치하는 구매가 있으면 접두어 관계인 형제 상품까지 풀지 않음을 보장한다(PR #230 리뷰).

    "무선 이어폰" 지목이 "무선 이어폰 케이스"의 부분문자열이기도 해, 좁은 해석을 고르지 않으면
    사용자가 지목하지 않은 상품까지 exact 제외가 풀린다.
    """
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat((101, "음향가전", "무선 이어폰"), (102, "음향가전", "무선 이어폰 케이스")),
    )
    products = [_prod(101, "음향가전", "무선 이어폰"), _prod(102, "음향가전", "무선 이어폰 케이스")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 101 in _only_list(push.pushes[0]).product_ids
    assert 102 not in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_falls_back_to_partial_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """완전 일치가 없으면 부분비교로 넓혀 표기 차이("무선이어폰" vs "무선 이어폰 프로")를 잡는다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "음향가전", "무선 이어폰 프로"))
    )
    products = [_prod(101, "음향가전", "무선 이어폰 프로")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 101 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_rejects_reverse_only_partial_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """긴 지목 안에 짧은 구매명이 든 역방향 부분일치는 다른 상품 오해제로 번지지 않는다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "음향가전", "이어폰"))
    )
    products = [_prod(101, "음향가전", "이어폰"), _prod(202, "음향가전", "헤드폰")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰 케이스"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 101 not in _only_list(push.pushes[0]).product_ids
    assert 202 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_survives_rerank_limit_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """되살린 상품이 검색 순서상 rerank 상한 밖이어도 절단에 잘리지 않음을 보장한다(PR #230 리뷰).

    절단(`kept[:embedding_rerank_limit]`)은 원본 검색 순서 기준이라, 지목 상품이 상한 밖이면
    exact 제외를 면제해 놓고도 rerank 후보에조차 못 들어가 "지목하면 다시 추천된다"가 깨진다.
    """
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "embedding_rerank_limit", 2)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "음향가전", "무선 이어폰"))
    )
    # 지목 상품(101)을 검색 순서 **맨 뒤**(상한 2 밖)에 둔다.
    products = [
        _prod(201, "음향가전", "유선 이어폰"),
        _prod(202, "음향가전", "헤드폰"),
        _prod(101, "음향가전", "무선 이어폰"),
    ]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 101 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_survives_rerank_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rerank 가 지목 상품을 안 골라도 노출 목록에 포함됨을 보장한다(PR #230 리뷰).

    rerank 는 relevance 로 expose_max 개만 고르고 "이 상품은 반드시" 라는 고정 수단이 없다.
    절단만 막고 여기를 두면 exact 제외·상한 절단을 다 통과하고도 최종 노출에서 조용히 빠진다.
    """
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "rerank_grounding_arm", "validated")
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 2)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "음향가전", "무선 이어폰"))
    )
    products = [
        _prod(101, "음향가전", "무선 이어폰").model_copy(update={"review_count": 10}),
        _prod(201, "음향가전", "유선 이어폰").model_copy(update={"review_count": 10}),
        _prod(202, "음향가전", "헤드폰").model_copy(update={"review_count": 10}),
    ]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰"],
            "filters": {},
            "case": 1,
        },
        # rerank 가 지목 상품(101)을 빼고 고른 상황.
        rerank={
            "ranked": [
                {"productId": 201, "rationale": "가성비가 좋아요"},
                {"productId": 202, "rationale": "음질이 우수해요"},
            ],
            "overallComment": "추천이에요",
            "overallClaims": [
                {
                    "claimCode": "ALL_RATING_HIGH",
                    "scope": "FINAL_EXPOSED_PRODUCTS",
                    "subjectProductIds": [201, 202],
                    "evidenceFields": ["ratingLevel"],
                }
            ],
        },
    )
    events = await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 101 in _only_list(push.pushes[0]).product_ids
    texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert NEUTRAL_OVERALL_COMMENT in texts
    assert "추천이에요" not in texts


async def test_recommendation_repurchase_pin_stays_in_its_fanout_need(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fan-out에서도 절단 전 우선순위와 rerank pin이 지목 상품을 자기 니즈 목록에만 보존한다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "rerank_ranking_arm", "current")
    monkeypatch.setattr(get_settings(), "embedding_rerank_limit", 3)
    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 2)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "전자기기", "여행용 어댑터"))
    )

    async def _map(**kwargs):
        return CategoryMapping(legs=[("여행용품", "여행용 파우치"), ("전자기기", "여행용 어댑터")])

    async def _search(filters, exclude_product_ids=None):
        products = (
            [_prod(102, "여행용품", "여행용 파우치"), _prod(103, "여행용품", "압축 파우치")]
            if filters.category == "여행용품"
            else [
                _prod(201, "전자기기", "멀티 어댑터"),
                _prod(101, "전자기기", "여행용 어댑터"),
            ]
        )
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["여행용 어댑터"],
            "categoryQueries": [
                {"category": "여행용품", "query": "여행용 파우치"},
                {"category": "전자기기", "query": "여행용 어댑터"},
            ],
            "filters": {},
            "case": 3,
        },
        rerank={
            "ranked": [
                {"productId": 102, "rationale": "수납하기 좋아요"},
                {"productId": 201, "rationale": "호환성이 좋아요"},
            ],
            "overallComment": "니즈별 추천이에요",
        },
    )
    await _collect(
        run_buyer_turn(
            _req(message="여행용 파우치랑 전에 산 어댑터 추천해줘"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    lists = push.pushes[0].lists
    assert len(lists) == 2
    assert lists[0].product_ids == [102]
    assert lists[1].product_ids == [101, 201]
    assert all(len(item.product_ids) <= 2 for item in lists)
    assert 101 not in {reason.product_id for reason in lists[1].reasons}


async def test_buy_all_budget_builds_top_k_sets_from_wider_candidate_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#60] 노출 후보보다 넓은 leg 풀로 예산 준수 BUY_ALL 세트와 ready id를 만든다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(settings, "budget_set_alt_pool", 3)
    monkeypatch.setattr(settings, "budget_set_max_count", 3)
    monkeypatch.setattr(settings, "budget_set_label_focus", "형식 누락")

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "파우치"), ("B", "어댑터")])

    pools = {
        "A": [
            _prod(11, "A", "A1").model_copy(update={"price": 30_000}),
            _prod(12, "A", "A2").model_copy(update={"price": 20_000}),
            _prod(13, "A", "A3").model_copy(update={"price": 10_000}),
        ],
        "B": [
            _prod(21, "B", "B1").model_copy(update={"price": 30_000}),
            _prod(22, "B", "B2").model_copy(update={"price": 20_000}),
            _prod(23, "B", "B3").model_copy(update={"price": 10_000}),
        ],
    }

    async def _search(filters, exclude_product_ids=None):
        products = pools[filters.category]
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "totalBudget": 50_000,
            "categoryQueries": [
                {"category": "A", "query": "파우치"},
                {"category": "B", "query": "어댑터"},
            ],
            "filters": {"priceMax": 50_000},
        },
        rerank={
            "ranked": [
                {"productId": 11, "rationale": "튼튼해요"},
                {"productId": 21, "rationale": "호환돼요"},
            ],
            "overallComment": "세트 추천이에요",
            "overallClaims": [
                {
                    "claimCode": "ALL_WITHIN_TOTAL_BUDGET",
                    "scope": "FINAL_RECOMMENDATION_LISTS",
                    "subjectProductIds": [13, 23, 11, 21],
                    "evidenceFields": ["price", "totalBudget"],
                }
            ],
        },
    )
    events = await _collect(
        run_buyer_turn(
            _req("5만원으로 파우치와 어댑터 전부"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    sent = push.pushes[0]
    assert sent.list_type == "BUY_ALL"
    assert sent.total_budget == 50_000
    assert len(sent.lists) == settings.budget_set_max_count
    assert [item.label for item in sent.lists] == ["알뜰", "균형", "균형"]
    prices = {p.product_id: p.price for products in pools.values() for p in products}
    assert all(sum(prices[pid] for pid in item.product_ids) <= 50_000 for item in sent.lists)
    assert len({item.list_id for item in sent.lists}) == len(sent.lists)
    ready = next(event for event in events if event["type"] == "products.ready")
    assert ready["data"]["listIds"] == [item.list_id for item in sent.lists]
    texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert "각 추천 조합이 모두 예산 안에 들어와요." in texts
    assert "세트 추천이에요" not in texts


async def test_buy_all_pool_keeps_cheap_candidate_below_relevance_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relevance 7위 저가 후보도 유계 풀에 남아 실제 예산 가능 BUY_ALL 조합을 만든다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(settings, "budget_set_alt_pool", 6)
    observed_pools = []
    build = recommendation_graph.build_budget_sets

    def _record_bounded_pools(**kwargs):
        observed_pools.append(kwargs["pools"])
        return build(**kwargs)

    monkeypatch.setattr(recommendation_graph, "build_budget_sets", _record_bounded_pools)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "파우치"), ("B", "어댑터")])

    pools = {
        category: [
            _prod(base + rank, category, f"{category}{rank}").model_copy(
                update={"price": 10_000 if rank == 7 else 30_000}
            )
            for rank in range(1, 8)
        ]
        for category, base in (("A", 10), ("B", 20))
    }

    async def _search(filters, exclude_product_ids=None):
        products = pools[filters.category]
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "totalBudget": 50_000,
            "categoryQueries": [
                {"category": "A", "query": "파우치"},
                {"category": "B", "query": "어댑터"},
            ],
            "filters": {"priceMax": 50_000},
        },
        rerank={
            "ranked": [
                {"productId": 11, "rationale": "관련도가 높아요"},
                {"productId": 21, "rationale": "관련도가 높아요"},
            ],
            "overallComment": "세트 추천이에요",
        },
    )

    await _collect(
        run_buyer_turn(
            _req("5만원으로 파우치와 어댑터 전부"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    sent = push.pushes[0]
    assert sent.list_type == "BUY_ALL"
    assert observed_pools
    assert all(len(pool) <= settings.budget_set_alt_pool for pool in observed_pools[0])
    assert {11, 17} <= {product_id for product_id, _ in observed_pools[0][0]}
    assert {21, 27} <= {product_id for product_id, _ in observed_pools[0][1]}
    assert any({17, 27} <= set(item.product_ids) for item in sent.lists)
    assert all(len(item.product_ids) == 2 for item in sent.lists)


async def test_buy_all_pool_finds_priced_candidate_below_unpriced_relevance_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """상위 relevance 후보가 모두 무가격이어도 뒤의 priced 후보를 보존해 거짓 고지를 막는다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(settings, "budget_set_alt_pool", 6)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파")])

    pools = {
        category: [
            _prod(base + rank, category, f"{category}{rank}").model_copy(
                update={"price": 10_000 if rank == 7 else None}
            )
            for rank in range(1, 8)
        ]
        for category, base in (("A", 10), ("B", 20))
    }

    async def _search(filters, exclude_product_ids=None):
        products = pools[filters.category]
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
            ],
            "filters": {},
        },
        rerank={
            "ranked": [
                {"productId": 11, "rationale": "관련도가 높아요"},
                {"productId": 21, "rationale": "관련도가 높아요"},
            ],
            "overallComment": "세트 추천이에요",
        },
    )

    events = await _collect(
        run_buyer_turn(
            _req("감자탕 재료 전부"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    sent = push.pushes[0]
    assert sent.list_type == "BUY_ALL"
    assert all({17, 27} <= set(item.product_ids) for item in sent.lists)
    token_texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert settings.budget_set_candidate_fallback_notice not in token_texts
    assert not any("가격 후보가 없어" in text for text in token_texts)


async def test_buy_all_without_budget_still_builds_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#60] BUY_ALL 판정은 예산 유무와 독립이며 totalBudget은 선택 필드다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파")])

    async def _search(filters, exclude_product_ids=None):
        base = 10 if filters.category == "A" else 20
        products = [
            _prod(base + 1, filters.category, "상품1"),
            _prod(base + 2, filters.category, "상품2").model_copy(update={"price": 20_000}),
        ]
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
            ],
            "filters": {},
        }
    )
    await _collect(
        run_buyer_turn(
            _req("감자탕 재료"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "BUY_ALL"
    assert push.pushes[0].total_budget is None


async def test_infeasible_budget_falls_back_to_pick_one_and_notices_before_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#60] 어떤 세트도 불가능하면 종전 목록으로 폴백하고 ready 전에 투명 고지한다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)
    build = recommendation_graph.build_budget_sets
    build_contexts = []

    def _record_build_context(**kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_event_loop = False
        else:
            on_event_loop = True
        build_contexts.append((kwargs["total_budget"], on_event_loop))
        return build(**kwargs)

    monkeypatch.setattr(recommendation_graph, "build_budget_sets", _record_build_context)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파")])

    async def _search(filters, exclude_product_ids=None):
        product_id = 11 if filters.category == "A" else 21
        products = [
            _prod(product_id, filters.category, "상품").model_copy(update={"price": 20_000})
        ]
        return ProductSearchResult(products=products, total_count=1)

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "totalBudget": 10_000,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
            ],
            "filters": {"priceMax": 10_000},
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req("1만원으로 전부"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "PICK_ONE"
    assert build_contexts == [(10_000, False), (None, False)]
    notice_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "token" and "예산 안에 드는 조합" in event["data"]["text"]
    )
    ready_index = next(
        index for index, event in enumerate(events) if event["type"] == "products.ready"
    )
    assert notice_index < ready_index


async def test_partial_budget_set_names_dropped_need_before_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#60] 일부 니즈를 제외한 세트는 제외 이름을 정제한 token으로 투명하게 고지한다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파"), ("C", "들깨가루")])

    prices = {"A": 40_000, "B": 20_000, "C": 10_000}

    async def _search(filters, exclude_product_ids=None):
        product_id = {"A": 11, "B": 21, "C": 31}[filters.category]
        products = [
            _prod(product_id, filters.category, "상품").model_copy(
                update={"price": prices[filters.category]}
            )
        ]
        return ProductSearchResult(products=products, total_count=1)

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "totalBudget": 35_000,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
                {"category": "C", "query": "들깨가루"},
            ],
            "filters": {"priceMax": 35_000},
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req("3만5천원으로 감자탕 재료 전부"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "BUY_ALL"
    notice_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "token" and "등뼈" in event["data"]["text"]
    )
    ready_index = next(
        index for index, event in enumerate(events) if event["type"] == "products.ready"
    )
    assert notice_index < ready_index


async def test_recommendation_ambiguous_repurchase_reverts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """짧고 흔한 지목("세트")이 최근 구매 여러 건에 걸리면 아무것도 되돌리지 않는다(PR #230 리뷰).

    완전 일치 없이 부분비교만으로 여러 건이 걸리는 것은 지목이 모호하다는 신호다. 그대로 풀면
    사용자가 지목하지 않은 상품까지 dedup 이 통째로 무력화된다 — 미해제 방향으로 degrade 한다.
    """
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat((301, "식품", "한우 선물세트"), (302, "뷰티", "화장품 세트")),
    )
    products = [_prod(301, "식품", "한우 선물세트"), _prod(302, "뷰티", "화장품 세트")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["세트"],
            "filters": {},
            "case": 1,
        }
    )
    events = await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    # 둘 다 최근 구매라 제외 유지 → 노출할 상품이 없다(push 자체가 없음).
    assert push.pushes == []
    assert "error" not in _types(events)


async def test_recommendation_multiple_repurchase_references_revert_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """맥락 목록이 복수 지목으로 에코되면 각 이름이 정확해도 아무 상품도 되돌리지 않는다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat(
            (301, "세탁용품", "리필 세탁 세제 2L"),
            (302, "세탁용품", "드럼용 세탁 세제"),
        ),
    )
    products = [
        _prod(301, "세탁용품", "리필 세탁 세제 2L"),
        _prod(302, "세탁용품", "드럼용 세탁 세제"),
    ]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["리필 세탁 세제 2L", "드럼용 세탁 세제"],
            "filters": {},
            "case": 1,
        }
    )
    events = await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert push.pushes == []
    assert events[-1]["data"]["finishReason"] == "zero_result"


async def test_recommendation_duplicate_repurchase_references_restore_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 상품명의 반복 지목은 단일 고유 지목으로 보고 정상적으로 되돌린다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat((301, "음향가전", "무선 이어폰")),
    )
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰", "무선 이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=llm,
            search=_make_search([_prod(301, "음향가전", "무선 이어폰")]),
            push_fn=push,
        )
    )
    assert 301 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_restores_all_identically_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """완전일치 후보의 정규화 상품명이 하나면 여러 productId를 **전부** 되돌린다.

    모호성은 매칭 productId 개수가 아니라 구분되는 정규화 상품명 개수로 판정한다. 재등록·옵션
    분리로 같은 이름이 여러 productId로 존재해도 이름 그룹은 하나이므로 모두 해제한다.
    """
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat((401, "음향가전", "무선 이어폰"), (402, "음향가전", "무선 이어폰")),
    )
    products = [_prod(401, "음향가전", "무선 이어폰"), _prod(402, "음향가전", "무선 이어폰")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    ids = _only_list(push.pushes[0]).product_ids
    assert 401 in ids and 402 in ids


async def test_recommendation_repurchase_partial_restores_all_identically_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부분일치 후보가 같은 정규화 상품명뿐이면 해당 productId를 전부 되돌린다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat(
            (411, "음향가전", "무선 이어폰 프로"),
            (412, "음향가전", "무선 이어폰 프로"),
        ),
    )
    products = [
        _prod(411, "음향가전", "무선 이어폰 프로"),
        _prod(412, "음향가전", "무선 이어폰 프로"),
        _prod(499, "음향가전", "블루투스 스피커"),
    ]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert {411, 412} <= set(_only_list(push.pushes[0]).product_ids)


@pytest.mark.parametrize("decompose", [{}, {"repurchaseProducts": []}])
async def test_recommendation_without_repurchase_keeps_exact_exclusion(
    monkeypatch: pytest.MonkeyPatch,
    decompose: dict,
) -> None:
    """재구매 신호가 없거나 빈 목록이면 기존 최근 구매 exact 제외가 유지됨을 보장한다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰"))
    )
    push = _RecordingPush()
    llm = FakeLLM(decompose={"intent": "recommend", "filters": {}, "case": 1, **decompose})
    await _collect(
        run_buyer_turn(
            _req(), _member_num(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    assert 101 not in _only_list(push.pushes[0]).product_ids
    assert 102 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_unrelated_repurchase_keeps_exact_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구매 이력과 무관한 상품명 지목은 최근 구매 상품의 exact 제외를 풀지 않음을 보장한다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "무선이어폰", "무선 이어폰"))
    )
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["세탁 세제"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(
            _req(), _member_num(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    assert 101 not in _only_list(push.pushes[0]).product_ids
    assert 102 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_repurchase_resolves_only_against_recent_purchases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """후보에만 있는 지목 상품은 특별 취급하지 않아 해제 집합이 최근 구매 안에 머묾을 보장한다."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(201, "조미료", "후추"), _prod(202, "무선이어폰", "이어폰")]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["후추"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(_req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push)
    )
    assert 201 not in _only_list(push.pushes[0]).product_ids
    assert 202 in _only_list(push.pushes[0]).product_ids


async def test_recommendation_guest_ignores_repurchase_without_purchases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구매 이력이 없는 게스트도 재구매 지목 때문에 크래시하지 않고 정상 추천됨을 보장한다."""
    accessed = {"n": 0}

    async def unexpected_store():
        accessed["n"] += 1
        raise AssertionError("guest must not access repurchase store")

    monkeypatch.setattr(recommendation_graph, "get_repurchase_store", unexpected_store)
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["후추"],
            "filters": {},
            "case": 1,
        }
    )
    await _collect(
        run_buyer_turn(
            _req(),
            _guest(),
            llm=llm,
            search=_make_search([_prod(201, "조미료", "후추")]),
            push_fn=push,
        )
    )
    assert 201 in _only_list(push.pushes[0]).product_ids
    assert accessed["n"] == 0


@pytest.mark.parametrize("cap", [0, -1])
async def test_repurchase_store_nonpositive_cap_persists_empty_list(cap: int) -> None:
    """cap <= 0은 음수 슬라이스 역전을 피하고 정확히 빈 리스트를 저장한다."""
    store = RepurchaseStore()

    await store.add("cap-boundary", [101, 102], cap=cap)

    assert await store.get("cap-boundary") == []


async def test_repurchase_store_get_discards_polluted_values() -> None:
    """오염된 저장값은 int 리스트만 방어적으로 복원하고 스트림 입력으로 넘기지 않는다."""
    backend = InMemoryStore()
    store = RepurchaseStore(backend)
    await backend.aput(
        ("buyer_repurchase_v1", "polluted"),
        "product_ids",
        {"product_ids": [101, "102", None, True, 103]},
    )

    assert await store.get("polluted") == [101, 103]


async def test_load_persisted_repurchase_avoids_redundant_read_after_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """지목 턴은 read+write 2회, 지목 없는 턴은 순수 read 1회만 저장소를 왕복한다."""

    class CountingStore(InMemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.operations: list[str] = []

        async def aget(self, *args, **kwargs):
            self.operations.append("get")
            return await super().aget(*args, **kwargs)

        async def aput(self, *args, **kwargs):
            self.operations.append("put")
            return await super().aput(*args, **kwargs)

    backend = CountingStore()
    store = RepurchaseStore(backend)

    async def get_store():
        return store

    monkeypatch.setattr(recommendation_graph, "get_repurchase_store", get_store)
    settings = SimpleNamespace(dedup_repurchase_store_max=20)

    assert await recommendation_graph._load_persisted_repurchase("thread", {101}, settings) == {101}
    assert backend.operations == ["get", "put"]

    backend.operations.clear()
    assert await recommendation_graph._load_persisted_repurchase("thread", set(), settings) == {101}
    assert backend.operations == ["get"]


async def test_recommendation_suppresses_consumable_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """최근 구매한 소모품 카테고리는 후보에서 억제되고 되돌리기 칩이 나온다(결정 14-F)."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(201, "조미료", "후추"), _prod(202, "무선이어폰", "이어폰")]
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(), _member_num(), llm=FakeLLM(), search=_make_search(products), push_fn=push
        )
    )
    assert 201 not in _only_list(push.pushes[0]).product_ids  # 조미료 억제
    assert 202 in _only_list(push.pushes[0]).product_ids
    sug = next(e for e in events if e["type"] == "suggestions")["data"]
    assert sug["chips"][0]["revert"]["category"] == "조미료"
    assert sug["chips"][0]["estCount"] == 1
    assert "소금" in sug["chips"][0]["label"]


async def test_recommendation_revert_chip_strips_seller_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """판매자 입력 영향 상품명·카테고리는 suggestions 칩 노출 전에 정제된다."""
    _fix_now(monkeypatch)
    dirty_category = "조미\n료\u200b\u202e"
    dirty_name = "소\x1b[31m금\u200b\u202e"
    monkeypatch.setattr(get_settings(), "consumable_categories", [dirty_category])
    monkeypatch.setattr(
        _sc_mod,
        "get_recent_purchases",
        _purchases_cat((900, dirty_category, dirty_name)),
    )
    products = [_prod(201, dirty_category, "후추"), _prod(202, "무선이어폰", "이어폰")]

    events = await _collect(
        run_buyer_turn(
            _req(thread_id="unsafe-revert"),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )

    chip = next(e for e in events if e["type"] == "suggestions")["data"]["chips"][0]
    assert chip["label"] == "소[31m금은 최근 구매 — 다시 추천받기"
    assert chip["revert"]["category"] == "조미 료"

    # FE가 정제된 machine value를 다음 턴에 돌려줘도 내부 원본 카테고리와 다시 매핑돼야 한다.
    push = _RecordingPush()
    revert = FakeLLM(
        decompose={
            "intent": "recommend",
            "revertCategories": [chip["revert"]["category"]],
            "filters": {},
            "case": 2,
        }
    )
    reverted_events = await _collect(
        run_buyer_turn(
            _req(thread_id="unsafe-revert"),
            _member_num(),
            llm=revert,
            search=_make_search(products),
            push_fn=push,
        )
    )
    assert 201 in _only_list(push.pushes[0]).product_ids
    assert "suggestions" not in _types(reverted_events)


async def test_recommendation_nonconsumable_not_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """비소모품 카테고리는 억제하지 않는다(exact 제외만) — 되돌리기 칩 없음."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((202, "무선이어폰", "이어폰"))
    )
    products = [_prod(201, "조미료", "후추"), _prod(202, "무선이어폰", "이어폰")]
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(), _member_num(), llm=FakeLLM(), search=_make_search(products), push_fn=push
        )
    )
    assert 202 not in _only_list(push.pushes[0]).product_ids  # exact 제외(구매한 productId)
    assert 201 in _only_list(push.pushes[0]).product_ids  # 조미료지만 구매 안 함 → 유지
    assert _revert_chips(events) == []  # 억제 카테고리 없음 → 되돌리기 칩 없음


async def test_recommendation_no_consumable_config_no_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """consumable_categories 미설정(기본 [])이면 카테고리 억제·칩 없음."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(201, "조미료", "후추")]
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(), _member_num(), llm=FakeLLM(), search=_make_search(products), push_fn=push
        )
    )
    assert 201 in _only_list(push.pushes[0]).product_ids
    assert _revert_chips(events) == []


async def test_recommendation_revert_unsuppresses_category(monkeypatch: pytest.MonkeyPatch) -> None:
    """되돌리기(revertCategories)하면 다음 턴부터 그 카테고리를 억제하지 않는다(멀티턴 지속)."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(201, "조미료", "후추"), _prod(202, "무선이어폰", "이어폰")]
    # 턴 1: 조미료 억제
    push1 = _RecordingPush()
    await _collect(
        run_buyer_turn(
            _req(thread_id="tR"),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(products),
            push_fn=push1,
        )
    )
    assert 201 not in _only_list(push1.pushes[0]).product_ids
    # 턴 2: 사용자 되돌리기
    push2 = _RecordingPush()
    llm2 = FakeLLM(
        decompose={"intent": "recommend", "revertCategories": ["조미료"], "filters": {}, "case": 2}
    )
    events2 = await _collect(
        run_buyer_turn(
            _req(thread_id="tR"),
            _member_num(),
            llm=llm2,
            search=_make_search(products),
            push_fn=push2,
        )
    )
    assert 201 in _only_list(push2.pushes[0]).product_ids  # 조미료 복원
    assert "suggestions" not in _types(events2)  # 더는 억제 안 함 → 칩 없음


async def test_recommendation_all_suppressed_offers_revert(monkeypatch: pytest.MonkeyPatch) -> None:
    """후보가 전부 소모품 억제로 비어도 되돌리기 칩은 제공한다(복원 가능)."""
    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(201, "조미료", "후추")]
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member_num(),
            llm=FakeLLM(),
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )
    assert "products.ready" not in _types(events)
    assert events[-1]["data"]["finishReason"] == "zero_result"
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "가렸" in token and "구매하신 것들" not in token  # 카테고리 억제 문구(exact 문구 아님)
    sug = next(e for e in events if e["type"] == "suggestions")["data"]
    assert sug["chips"][0]["revert"]["category"] == "조미료"


async def test_recommendation_guest_no_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    """게스트는 이력 조회 스킵 → 카테고리 억제·칩 없음."""
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    products = [_prod(201, "조미료", "후추")]
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(_req(), _guest(), llm=FakeLLM(), search=_make_search(products), push_fn=push)
    )
    assert 201 in _only_list(push.pushes[0]).product_ids
    assert _revert_chips(events) == []


def test_order_item_category_and_recent_items() -> None:
    """I-19 categoryName 파싱 + recent_items 윈도우/상태 필터."""
    from app.schemas.spring import RecentPurchases

    rp = RecentPurchases.model_validate(
        {
            "orders": [
                {
                    "orderId": 1,
                    "orderedAt": "2026-07-15T00:00:00",
                    "items": [
                        {
                            "orderItemId": 1,
                            "productId": 5,
                            "categoryName": "조미료",
                            "status": "DELIVERED",
                        },
                        {
                            "orderItemId": 2,
                            "productId": 6,
                            "categoryName": "조미료",
                            "status": "CANCELED",
                        },
                    ],
                },
            ]
        }
    )
    items = rp.recent_items(exclude_statuses={"CANCELED"})
    assert [i.product_id for i in items] == [5]
    assert items[0].category == "조미료"


async def test_recommendation_revert_ignores_non_consumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """소모품 화이트리스트 밖 revert 문자열은 무시(무한 누적·임의 문자열 방지, Claude)."""
    from app.agents.buyer.recommendation.state import get_revert_store

    _fix_now(monkeypatch)
    monkeypatch.setattr(get_settings(), "consumable_categories", ["조미료"])
    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _purchases_cat((900, "조미료", "소금")))
    products = [_prod(201, "조미료", "후추")]
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "revertCategories": ["해킹", "무선이어폰"],
            "filters": {},
            "case": 2,
        }
    )
    await _collect(
        run_buyer_turn(
            _req(thread_id="tN"),
            _member_num(),
            llm=llm,
            search=_make_search(products),
            push_fn=_RecordingPush(),
        )
    )
    # 화이트리스트 밖이라 저장 안 됨 → 조미료 억제 유지(되돌려지지 않음)
    revert_store = await get_revert_store()
    assert await revert_store.get(await _thread_key(_req(thread_id="tN"), _member_num())) == set()


def test_suggestion_chip_requires_exactly_one_kind() -> None:
    """SuggestionChip 은 revert/relaxation 중 정확히 하나여야 한다(§3.1)."""
    import pytest as _pytest
    from app.schemas.chat import RelaxationRef, RevertRef, SuggestionChip

    SuggestionChip(label="ok", revert=RevertRef(category="조미료"), est_count=1)  # 유효
    SuggestionChip(
        label="ok", relaxation=RelaxationRef(field="priceMax", value=1), est_count=1
    )  # 유효
    with _pytest.raises(ValueError):
        SuggestionChip(label="none", est_count=1)  # 둘 다 없음
    with _pytest.raises(ValueError):
        SuggestionChip(
            label="both",
            revert=RevertRef(category="x"),
            relaxation=RelaxationRef(field="f", value=1),
            est_count=1,
        )


async def test_thread_filter_and_revert_stores_have_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """buyer filter/revert BaseStore I/O도 cart/profile과 같은 deadline을 사용한다."""
    from app.agents.buyer.graph import ThreadFilterStore
    from app.agents.buyer.recommendation.state import RevertStore
    from app.schemas.spring import ProductSearchFilters

    class _HangStore:
        async def aget(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def aput(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.01)
    thread = ThreadFilterStore(_HangStore())
    revert = RevertStore(_HangStore())
    operations = [
        lambda: thread.get("k"),
        lambda: thread.put("k", ProductSearchFilters(category="x")),
        lambda: revert.get("k"),
        lambda: revert.add("k", ["x"]),
    ]
    for operation in operations:
        with pytest.raises(TimeoutError):
            await operation()


def test_revert_lock_registry_releases_idle_keys() -> None:
    from app.agents.buyer.recommendation import state

    lock = state._lock_for("k")
    assert len(state._add_locks) == 1
    del lock
    gc.collect()
    assert len(state._add_locks) == 0


# ─────────── #51 동의어 확장 — retrieval keyword 완화 (A) ───────────


def _filter_recording_search(products, *, honor_keyword: bool = False):
    """leg 에 전달된 filters 를 리스트로 기록하는 검색 fake.

    기존 _recording_search(products, sink) 는 exclude 만 기록하므로 filters 확인용으로 별도로 둔다.
    honor_keyword=True 면 Spring keyword(상품명 LIKE 부분일치)를 흉내내 필터링한다 —
    발화≠상품명일 때 keyword 가 살아있으면 탈락함을 재현(동의어 통합 검증용).
    """
    seen: list = []

    async def _search(filters, exclude_product_ids=None):
        seen.append(filters)
        items = list(products)
        if honor_keyword and filters.keyword:
            items = [p for p in items if filters.keyword in (p.name or "")]
        return ProductSearchResult(products=items, total_count=len(items))

    return _search, seen


async def test_leg_drops_keyword_when_category_present() -> None:
    """[#51 A] canonical category 가 있으면 Spring keyword(상품명 LIKE)를 드롭한다.

    leg 는 항상 canonical 이므로(map_categories 는 canonical-or-drop) keyword=None 이어야 —
    동의어(글자 다름)가 retrieval 후보를 원천 배제하지 못하게 한다. category 는 그대로 실린다.
    """
    search, seen = _filter_recording_search(DEFAULT_PRODUCTS)
    await _collect(
        run_buyer_turn(_req(), _member(), llm=FakeLLM(), search=search, push_fn=_RecordingPush())
    )
    assert seen, "검색이 호출되지 않았다"
    leg = seen[0]
    assert leg.category  # category 는 retrieval 앵커로 실린다
    assert leg.keyword is None  # keyword(글자 필터)는 드롭


async def test_leg_keeps_keyword_when_config_disabled(monkeypatch) -> None:
    """[#51 A] search_drop_keyword_with_category=False 면 기존 동작(leg query→keyword) 복원 — 롤백 안전성.

    leg query 와 filters.keyword 를 서로 다르게 둬 `query or filters.keyword` 의 leg query 우선을
    명확히 핀한다(둘이 같으면 어느 분기가 값을 줬는지 구분 못 함).
    """
    monkeypatch.setattr(get_settings(), "search_drop_keyword_with_category", False)
    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "무선 이어폰",
        "categoryQueries": [{"category": "무선이어폰", "query": "레그검색어"}],
        "filters": {"keyword": "베이스키워드"},
    }
    search, seen = _filter_recording_search(DEFAULT_PRODUCTS)
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(decompose=decompose),
            search=search,
            push_fn=_RecordingPush(),
        )
    )
    # config off → leg query 가 keyword 로(우선), base keyword 는 아님
    assert seen[0].keyword == "레그검색어"


async def test_conditions_omit_keyword_chip_when_dropped() -> None:
    """[#51 A] keyword 를 retrieval 에서 드롭하면 conditions 칩에서도 keyword 를 빼 표시-실제를 맞춘다.

    적용되지 않는 필터를 "제거 가능 조건"으로 광고하는 dead chip 을 막는다. category 칩은 남는다.
    """
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    cond = next(e for e in events if e["type"] == "conditions")["data"]
    fields = {c["field"] for c in cond["chips"]}
    assert "keyword" not in fields  # 드롭된 keyword 는 칩으로 노출하지 않는다
    assert "category" in fields  # 실제 적용되는 category 는 칩으로 노출


async def test_conditions_keep_keyword_chip_when_config_disabled(monkeypatch) -> None:
    """[#51 A] config off 면 keyword 를 retrieval 에 쓰므로 conditions 칩에도 그대로 노출(정합)."""
    monkeypatch.setattr(get_settings(), "search_drop_keyword_with_category", False)
    events = await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    cond = next(e for e in events if e["type"] == "conditions")["data"]
    fields = {c["field"] for c in cond["chips"]}
    assert "keyword" in fields  # 적용되는 keyword 는 칩으로 노출


async def test_leg_keeps_keyword_when_backend_not_embedding_rerank(monkeypatch) -> None:
    """[#51 리뷰] keyword 드롭은 embedding_rerank 백엔드에서만 안전 — spring/vector 면 플래그가
    True 여도 유지한다.

    spring 은 재정렬이 없어 keyword 가 유일한 텍스트 신호이고, vector 는 filters.keyword 를 쿼리
    임베딩 입력으로 쓴다(드롭 시 빈 문자열 임베딩). 둘 다 드롭하면 품질이 급락하므로 유지해야 한다.
    """
    monkeypatch.setattr(get_settings(), "search_drop_keyword_with_category", True)
    monkeypatch.setattr(get_settings(), "search_backend", "spring")
    search, seen = _filter_recording_search(DEFAULT_PRODUCTS)
    await _collect(
        run_buyer_turn(_req(), _member(), llm=FakeLLM(), search=search, push_fn=_RecordingPush())
    )
    assert seen[0].keyword == "무선 이어폰"  # embedding_rerank 아님 → keyword 유지


async def test_synonym_product_survives_keyword_drop() -> None:
    """[#51 A] 발화("청바지")와 상품명("데님 팬츠")이 달라도 keyword 드롭 덕에 후보로 살아남는다.

    honor_keyword=True 로 Spring keyword LIKE 를 흉내 — keyword 가 살아있으면 '데님 팬츠'는
    '청바지' 부분일치 실패로 탈락하지만, category 로 드롭돼 후보에 남아 products.ready 가 뜬다.
    """
    denim = SpringProduct(
        product_id=201, name="데님 팬츠", price=39000, rating=4.4, category="청바지", brand="B"
    )
    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "청바지 데님 팬츠",
        "categoryQueries": [{"category": "청바지", "query": "청바지"}],
        "filters": {"keyword": "청바지"},
    }
    rerank = {"ranked": [{"productId": 201, "rationale": "핏이 좋아요"}], "overallComment": "c"}
    search, _seen = _filter_recording_search([denim], honor_keyword=True)
    push = _RecordingPush()
    events = await _collect(
        run_buyer_turn(
            _req(message="청바지 추천해줘"),
            _member(),
            llm=FakeLLM(decompose=decompose, rerank=rerank),
            search=search,
            push_fn=push,
        )
    )
    assert "products.ready" in _types(events)  # 동의어 상품이 살아 노출된다
    assert push.pushes and 201 in _only_list(push.pushes[0]).product_ids


# ─────────── #164 I-4 주문 상태 early route ───────────


def _order_member() -> Identity:
    return Identity(user_id="42", is_guest=False, seller_id=None, subject="42")


async def test_order_status_branch_is_early_and_passes_request_id() -> None:
    calls: list[int] = []

    async def fetch(user_id: int):
        calls.append(user_id)
        return SimpleNamespace(orders=[])

    async def forbidden(*args, **kwargs):
        raise AssertionError("order_status must bypass recommendation dependencies")

    llm = FakeLLM(decompose={"intent": "order_status", "filters": {}})
    events = await _collect(
        run_buyer_turn(
            _req(message="배송 상태 알려줘"),
            _order_member(),
            llm=llm,
            search=forbidden,
            push_fn=forbidden,
            map_categories=forbidden,
            order_status_fn=fetch,
            request_id="req-i4-unit",
        )
    )

    assert calls == [42]
    # progress_events_enabled 기본 on(#396) — 스트림 맨 앞에 progress 프레임이 추가된다.
    assert _types(events) == ["progress", "token", "done"]
    assert events[-1]["data"]["finishReason"] == "stop"
    assert [tier for tier, _ in llm.calls] == ["fast"]


async def test_order_status_default_dependency_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    calls: list[int] = []

    async def late_fetch(user_id: int):
        calls.append(user_id)
        return SimpleNamespace(orders=[])

    monkeypatch.setattr(sc, "get_order_status", late_fetch, raising=False)
    events = await _collect(
        run_buyer_turn(
            _req(message="내 주문 어디까지 왔어?"),
            _order_member(),
            llm=FakeLLM(decompose={"intent": "order_status", "filters": {}}),
            request_id="req-late-bound",
        )
    )
    assert calls == [42]
    # progress_events_enabled 기본 on(#396) — 스트림 맨 앞에 progress 프레임이 추가된다.
    assert _types(events) == ["progress", "token", "done"]


async def test_non_callable_order_status_dependency_only_errors_on_selected_route() -> None:
    with pytest.raises(TypeError, match="order_status_fn"):
        await _collect(
            run_buyer_turn(
                _req(),
                _order_member(),
                llm=FakeLLM(decompose={"intent": "order_status", "filters": {}}),
                order_status_fn=object(),
            )
        )

    events = await _collect(
        run_buyer_turn(
            _req(message="인사"),
            _order_member(),
            llm=FakeLLM(decompose={"intent": "general", "reply": "안녕하세요", "filters": {}}),
            order_status_fn=object(),
        )
    )
    # progress_events_enabled 기본 on(#396) — 스트림 맨 앞에 progress 프레임이 추가된다.
    assert _types(events) == ["progress", "token", "done"]


async def test_order_status_clears_pending_without_copying_response_into_buyer_state() -> None:
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import OrderStatusSummary, ProductSearchFilters

    identity = _order_member()
    request = _req(message="배송 상태 알려줘", thread_id="i4-state")
    key = await _thread_key(request, identity)
    cart_store = await get_cart_store()
    await cart_store.set_pending(key, PendingAdd(product_id=101, quantity=1))
    await cart_store.set_last_reco(key, [(777, "기존 추천 상품")])
    thread_store = await get_thread_store()
    original = ProductSearchFilters(category="기존 카테고리", semantic_query="기존 검색")
    await thread_store.put(key, original)

    async def fetch(user_id: int):
        return OrderStatusSummary.model_validate(
            {
                "orders": [
                    {
                        "orderId": 87654321,
                        "orderedAt": "2026-07-30T09:00:00+09:00",
                        "representativeStatus": "배송중",
                        "items": [
                            {
                                "productName": "I4 전용 응답 상품",
                                "status": "SHIPPING",
                                "statusText": "배송중",
                            }
                        ],
                    }
                ]
            }
        )

    events = await _collect(
        run_buyer_turn(
            request,
            identity,
            llm=FakeLLM(decompose={"intent": "order_status", "filters": {}}),
            order_status_fn=fetch,
        )
    )

    assistant_text = next(event["data"]["text"] for event in events if event["type"] == "token")
    assert "87654321" in assistant_text
    assert "I4 전용 응답 상품" in assistant_text
    assert "배송중" in assistant_text

    # The route may clear stale pending-cart state, but it must not copy I-4 facts into
    # recommendation filters or the cart's existing recommendation context.
    assert await cart_store.get_pending(key) is None
    assert await thread_store.get(key) == original
    assert await cart_store.get_last_reco(key) == [(777, "기존 추천 상품")]
    persisted_state = repr(
        (
            (await thread_store.get(key)).model_dump(),
            await cart_store.get_last_reco(key),
            await cart_store.get_pending(key),
        )
    )
    for response_only_value in ("87654321", "I4 전용 응답 상품", "배송중"):
        assert response_only_value not in persisted_state


# ─────────── rerank 출력 예산 (PR #212 리뷰 — 니즈별 분할이 max_tokens 를 넘기지 않게) ───────────


class _CapturingLLM:
    """complete 호출 인자를 그대로 기록하는 LLM — max_tokens·프롬프트 구성을 검증한다."""

    def __init__(self, ranked: list[dict]) -> None:
        self._ranked = ranked
        self.max_tokens: list[int] = []
        self.user: list[str] = []

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        self.max_tokens.append(max_tokens)
        self.user.append(user)
        return json.dumps(
            {"ranked": self._ranked, "overallComment": "골라봤어요"}, ensure_ascii=False
        )


class _DecomposePromptLLM:
    """decompose의 system/user 프롬프트만 기록하고 유효한 general JSON을 반환한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system, user, **kwargs):
        del kwargs
        self.calls.append((system, user))
        return json.dumps({"intent": "general", "reply": "답변", "filters": {}})


async def test_decompose_conversation_memory_is_untrusted_json_before_current_message() -> None:
    from app.agents.buyer.recommendation.decompose import decompose

    llm = _DecomposePromptLLM()
    await decompose(
        llm,
        query="지금 질문이 최우선이야",
        prior_filters=None,
        profile_summary=None,
        tier="fast",
        recent_conversation=[{"user": "이전 질문", "assistant": "이전 답변"}],
        situation_memory={"topic": "제주 여행", "openQuestions": ["숙소는?"]},
    )

    [(system, user)] = llm.calls
    assert "비신뢰 데이터" in system
    assert "현재 USER_MESSAGE" in system
    assert 'RECENT_CONVERSATION: [{"user": "이전 질문"' in user
    assert 'SITUATION_MEMORY: {"topic": "제주 여행"' in user
    assert user.endswith("USER_MESSAGE: 지금 질문이 최우선이야")


async def test_decompose_conversation_memory_treats_current_message_as_context_correction() -> None:
    from app.agents.buyer.recommendation.decompose import decompose
    from app.schemas.spring import ProductSearchFilters

    llm = _DecomposePromptLLM()
    await decompose(
        llm,
        query="나 여자야",
        prior_filters=ProductSearchFilters(
            category="남성의류 > 정장/슈트",
            semantic_query="캐주얼 정장",
        ),
        profile_summary=None,
        tier="fast",
        recent_conversation=[
            {
                "user": "캐주얼 정장 추천해줘",
                "assistant": "남성 캐주얼 정장을 추천할게요.",
            }
        ],
    )

    [(system, user)] = llm.calls
    assert "정정·추가 조건" in system
    assert "현재 사용자의 정정은 PRIOR_FILTERS와 이전 추천보다 우선" in system
    assert "현재 발화에 상품명이 없어도" in system
    assert "충돌하는 이전 조건은 유지하지 않는다" in system
    assert "여성 캐주얼 정장" in system
    assert '"category":"여성의류 > 정장세트"' in system
    assert 'RECENT_CONVERSATION: [{"user": "캐주얼 정장 추천해줘"' in user
    assert user.endswith("USER_MESSAGE: 나 여자야")


async def test_decompose_none_memory_keeps_existing_prompt_byte_identical() -> None:
    from app.agents.buyer.recommendation.decompose import decompose

    baseline = _DecomposePromptLLM()
    explicit_none = _DecomposePromptLLM()
    kwargs = {
        "query": "같은 질문",
        "prior_filters": None,
        "profile_summary": None,
        "tier": "fast",
    }

    await decompose(baseline, **kwargs)
    await decompose(
        explicit_none,
        **kwargs,
        recent_conversation=None,
        situation_memory=None,
    )

    assert baseline.calls == explicit_none.calls


def _cands(n: int) -> list:
    from app.schemas.spring import SpringProduct

    return [
        SpringProduct(product_id=100 + i, name=f"P{i}", price=1000, rating=4.0, category="c")
        for i in range(n)
    ]


async def test_rerank_output_budget_scales_with_expose_max() -> None:
    """rerank 의 max_tokens 는 요청한 노출 개수에 비례한다 (PR #212 리뷰).

    고정 1500 이면 니즈가 여럿일 때 항목이 27~30개로 늘어 출력이 중간에 잘리고,
    extract_json 이 파싱에 실패해 LLMError → rerank_degraded 로 떨어진다. 즉 "니즈별 근거 있는
    추천"이 정작 니즈가 여러 개일 때 더 자주 깨진다.
    """
    from app.agents.buyer.recommendation.rerank import rerank

    llm = _CapturingLLM([{"productId": 100, "rationale": "좋아요"}])
    await rerank(
        llm, query="q", candidates=_cands(30), profile_summary=None, tier="smart", expose_max=9
    )
    await rerank(
        llm, query="q", candidates=_cands(30), profile_summary=None, tier="smart", expose_max=30
    )

    small, large = llm.max_tokens
    assert large > small, "노출 개수가 늘면 출력 예산도 늘어야 한다"
    # 종전 단일 목록 경로(expose_max=9)의 실효 예산은 그대로 유지한다 — 회귀 방지.
    assert small == 1500


async def test_rerank_without_needs_keeps_prompt_unchanged() -> None:
    """니즈 정보가 없으면(단일 목록 경로) 프롬프트는 종전과 한 글자도 다르지 않다.

    다중 니즈 대응이 트래픽 대부분인 단일 목록 경로를 건드리면 안 된다 — 이 저장소는
    프롬프트에 지시를 얹었다가 기존 성공 케이스가 3/3→1/3 으로 희석된 실측 전례가 있다(#198).
    """
    from app.agents.buyer.recommendation.rerank import rerank

    llm = _CapturingLLM([{"productId": 100, "rationale": "좋아요"}])
    await rerank(
        llm, query="q", candidates=_cands(3), profile_summary=None, tier="smart", expose_max=9
    )

    assert "NEEDS" not in llm.user[0]
    assert '"need"' not in llm.user[0]


async def test_rerank_carries_need_boundaries_when_split() -> None:
    """니즈별 분할이면 후보마다 소속 니즈와 "니즈당 상위 N개" 지시를 함께 넘긴다 (PR #212 리뷰).

    안 넘기면 LLM 은 후보를 전역 관련도로만 정렬한다 — 한 니즈가 상위권을 쓸면 굶은 니즈는
    검색순서 보충으로 채워지고, 그 보충분에는 rationale 이 없어 **근거 없는 카드**가 나간다.
    rerank 는 "정상 성공"이라 rerank_degraded 로도 드러나지 않는다.
    """
    from app.agents.buyer.recommendation.rerank import rerank

    llm = _CapturingLLM([{"productId": 100, "rationale": "좋아요"}])
    await rerank(
        llm,
        query="q",
        candidates=_cands(3),
        profile_summary=None,
        tier="smart",
        expose_max=6,
        need_of={100: "파우치", 101: "파우치", 102: "어댑터"},
        per_need=3,
    )

    sent = llm.user[0]
    assert "파우치" in sent and "어댑터" in sent
    assert '"need"' in sent, "후보마다 소속 니즈를 실어야 LLM 이 경계를 안다"
    assert "3" in sent  # 니즈당 상위 N


# ─────────── #119 프로필 개인화 강도 (주입 스코프 · rerank 타이브레이커 · 세션 버퍼) ───────────


_PROFILE_MD = "# 취향\n- 3~5만원대 선호\n- 소니 선호, 샤오미 회피\n- 평점 4.5 이상 선호"


async def _seed_profile(user_id: str = "u1", markdown: str = _PROFILE_MD) -> None:
    """회원에게 취향 요약을 심는다 — reader 가 이 값을 그래프로 흘린다."""
    from app.agents.profile.store import get_profile_store

    store = await get_profile_store()
    await store.set_summary(user_id, markdown, "2026-07-31T00:00:00Z")


def _fast_prompt(llm: FakeLLM) -> str:
    """FakeLLM 이 기록한 decompose(fast tier) user 프롬프트."""
    fast = [user for tier, user in llm.calls if tier == "fast"]
    assert fast, "decompose 가 호출되지 않았다"
    return fast[0]


def _smart_prompt(llm: FakeLLM) -> str:
    smart = [user for tier, user in llm.calls if tier != "fast"]
    assert smart, "rerank 가 호출되지 않았다"
    return smart[0]


async def test_member_decompose_prompt_is_identical_to_guest() -> None:
    """[#119 핵심 회귀] 회원과 게스트의 decompose 프롬프트는 **바이트 동일**해야 한다.

    입력이 같으면 filters·category_legs 가 같은 분포로 나오고, 따라서 회원의 검색 후보가
    게스트보다 좁아질 수 **없다**. 프로필이 하드필터로 새는 경로(#119 래칫)를 LLM 품질
    측정 없이 결정론적으로 봉인하는 불변식이다.
    """
    await _seed_profile()

    member_llm = FakeLLM()
    await _collect(
        run_buyer_turn(_req(session_id="s-m", thread_id="t-m"), _member(), llm=member_llm)
    )
    guest_llm = FakeLLM()
    await _collect(run_buyer_turn(_req(session_id="s-g", thread_id="t-g"), _guest(), llm=guest_llm))

    assert _fast_prompt(member_llm) == _fast_prompt(guest_llm)
    assert "소니" not in _fast_prompt(member_llm)


async def test_profile_reaches_decompose_when_scope_both(monkeypatch) -> None:
    """롤백 경로 — scope=both 면 종전대로 프로필이 decompose 프롬프트에 실린다."""
    monkeypatch.setattr(get_settings(), "profile_injection_scope", "both")
    await _seed_profile()

    llm = FakeLLM()
    await _collect(run_buyer_turn(_req(), _member(), llm=llm))

    assert "소니" in _fast_prompt(llm)


async def test_rerank_still_receives_profile_when_decompose_does_not() -> None:
    """decompose 주입만 끈다 — rerank 개인화까지 같이 꺼지면 기능이 통째로 사라진다.

    (#119 구현에서 가장 그럴듯한 실수라 전용으로 고정한다.)
    """
    await _seed_profile()

    llm = FakeLLM()
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )

    assert "소니" not in _fast_prompt(llm)
    assert "소니" in _smart_prompt(llm)


async def test_profile_injection_scope_off_skips_both(monkeypatch) -> None:
    """scope=off 는 이번 턴 개인화를 끈다 — 라이브 A/B 의 baseline arm."""
    monkeypatch.setattr(get_settings(), "profile_injection_scope", "off")
    await _seed_profile()

    llm = FakeLLM()
    await _collect(
        run_buyer_turn(
            _req(),
            _member(),
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )

    assert "소니" not in _fast_prompt(llm)
    assert "소니" not in _smart_prompt(llm)


async def test_rerank_adds_tiebreak_line_with_profile() -> None:
    """프로필이 있으면 동점 처리 지시가 user 메시지에 붙는다(문구는 모듈 상수 참조)."""
    from app.agents.buyer.recommendation.rerank import _PROFILE_TIEBREAK, rerank

    llm = _CapturingLLM([{"productId": 100, "rationale": "좋아요"}])
    await rerank(
        llm,
        query="q",
        candidates=_cands(3),
        profile_summary=_PROFILE_MD,
        tier="smart",
        expose_max=6,
    )

    assert _PROFILE_TIEBREAK in llm.user[0]


async def test_rerank_prompt_unchanged_without_profile() -> None:
    """게스트(프로필 None) 프롬프트는 지시가 붙지 않는다 — 잘 도는 경로를 건드리지 않는다."""
    from app.agents.buyer.recommendation.rerank import _PROFILE_TIEBREAK, rerank

    llm = _CapturingLLM([{"productId": 100, "rationale": "좋아요"}])
    await rerank(
        llm, query="q", candidates=_cands(3), profile_summary=None, tier="smart", expose_max=6
    )

    assert _PROFILE_TIEBREAK not in llm.user[0]
    assert llm.user[0].startswith("PROFILE_SUMMARY: (없음)\nQUERY: q\n")


async def test_rerank_legacy_influence_omits_tiebreak_line(monkeypatch) -> None:
    """legacy 모드면 프로필이 있어도 지시가 붙지 않는다(롤백 경로)."""
    from app.agents.buyer.recommendation.rerank import _PROFILE_TIEBREAK, rerank

    monkeypatch.setattr(get_settings(), "profile_rerank_influence", "legacy")
    llm = _CapturingLLM([{"productId": 100, "rationale": "좋아요"}])
    await rerank(
        llm,
        query="q",
        candidates=_cands(3),
        profile_summary=_PROFILE_MD,
        tier="smart",
        expose_max=6,
    )

    assert _PROFILE_TIEBREAK not in llm.user[0]


async def test_rerank_system_prompt_has_no_profile_rule() -> None:
    """분기는 user 메시지에만 둔다 — _SYSTEM 을 건드리면 프로필 없는 경로까지 바뀐다."""
    from app.agents.buyer.recommendation.rerank import _SYSTEM

    assert "PROFILE" not in _SYSTEM


async def _buffer(user_id: str, session_id: str) -> list[str]:
    from app.agents.profile.store import get_profile_store
    from app.core.conversation import conversation_key

    store = await get_profile_store()
    return await store.get_session_ctx(conversation_key(user_id, session_id))


async def test_session_buffer_records_recommend_intent() -> None:
    """추천 턴 발화는 종전대로 취향 소스로 쌓인다(회귀)."""
    await _collect(run_buyer_turn(_req(session_id="s1"), _member(), llm=FakeLLM()))

    assert await _buffer("u1", "s1") == ["무선 이어폰 추천해줘"]


async def test_session_buffer_skips_excluded_intent() -> None:
    """[#119] 주문조회 발화는 취향 신호가 아니라 버퍼를 오염시킨다 — 적재하지 않는다."""
    llm = FakeLLM(decompose={"intent": "order_status", "reply": "", "filters": {}})

    async def _orders(user_id, **_):
        from app.schemas.spring import OrderStatusSummary

        return OrderStatusSummary(orders=[])

    await _collect(
        run_buyer_turn(
            _req("주문 어디쯤 왔어?", session_id="s1"),
            _member(),
            llm=llm,
            order_status_fn=_orders,
        )
    )

    assert await _buffer("u1", "s1") == []


async def test_session_buffer_skipped_when_decompose_fails() -> None:
    """[#119 행동 변화] 의도를 파악 못 한 턴은 취향 신호로도 쓰지 않는다."""
    llm = FakeLLM(decompose_error=True)
    await _collect(run_buyer_turn(_req(session_id="s1"), _member(), llm=llm))

    assert await _buffer("u1", "s1") == []


async def test_remember_command_recorded_even_when_decompose_fails() -> None:
    """ "기억해"는 intent 무관한 명시 명령 — 라우팅 앞 hot-path 로 남아야 한다."""
    from app.agents.profile.store import get_profile_store

    llm = FakeLLM(decompose_error=True)
    await _collect(
        run_buyer_turn(_req("소니 좋아해 기억해줘", session_id="s1"), _member(), llm=llm)
    )

    store = await get_profile_store()
    assert await store.get_facts("u1")


async def test_profile_injection_scope_off_still_accumulates_profile(monkeypatch) -> None:
    """[PR #223 리뷰] off 는 **주입만** 끊는다 — 프로필 축적은 계속된다(섀도 모드).

    "게스트 등가"로 읽으면 안 된다: 게스트는 프로필 경로 자체가 없지만(profile_eligible=False),
    off 인 회원은 세션 버퍼가 쌓이고 "기억해"도 기록돼 세션 종료 후 프로필이 계속 자란다.
    축적까지 멈추는 킬스위치가 필요해지면 off 의 의미를 좁히지 말고 별도 스위치를 둔다.
    """
    from app.agents.profile.store import get_profile_store

    monkeypatch.setattr(get_settings(), "profile_injection_scope", "off")
    message = "소니 이어폰 좋아해 기억해줘"
    await _collect(run_buyer_turn(_req(message, session_id="s-off"), _member(), llm=FakeLLM()))

    # 세션 버퍼(세션 종료 델타 소스)는 계속 쌓인다
    assert await _buffer("u1", "s-off") == [message]

    store = await get_profile_store()
    # "기억해" hot-path 도 계속 돌아 장기 fact 가 기록된다 — 게스트에겐 없는 경로다
    assert await store.get_facts("u1")
    # 다만 요약 재작성은 sleep-time 소관이라 턴 중에는 일어나지 않는다(REQ-PROF-023)
    assert await store.get_summary("u1") is None


async def test_session_buffer_records_cart_add_intent() -> None:
    """[PR #223 리뷰] 담기 발화는 **일부러** 버퍼에 남긴다 — 제외 목록에 없는 게 의도다.

    담기는 채팅 레인에서 구매에 가장 가까운 행동 신호다. 명세도 write 소스를
    conversation|purchase 로 두고(REQ-PROF-024) 구매 소스는 명시성 없이 반복성·현저성으로
    승격한다(REQ-PROF-044) — 제외하면 명세가 인정한 신호원을 코드가 막는다. 발화 자체도
    취향을 실어 나른다("검정으로 담아줘"). 노이즈("그거 담아줘")는 델타 추출 LLM·게이트·
    버퍼 상한이 걸러내므로, 되돌릴 수 없는 실수(신호 소실)를 피하는 쪽을 택한다.
    """
    message = "검정으로 담아줘"
    llm = FakeLLM(
        decompose={
            "intent": "cart_add",
            "reply": "",
            "filters": {},
            "cart": {"productId": 101, "quantity": 1},
        }
    )
    await _collect(run_buyer_turn(_req(message, session_id="s-cart"), _member(), llm=llm))

    assert await _buffer("u1", "s-cart") == [message]


async def test_buy_all_ten_legs_respects_list_product_contract_and_discloses_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R1 F1] fanout 10도 목록당 9상품을 넘지 않고 빠진 니즈를 투명하게 알린다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "category_fanout_max", 10)
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    legs = [(f"C{leg}", f"니즈{leg}") for leg in range(10)]

    async def _map(**kwargs):
        return CategoryMapping(legs=legs)

    async def _search(filters, exclude_product_ids=None):
        leg = int(filters.category[1:])
        products = [
            _prod(100 + leg, filters.category, f"상품{leg}").model_copy(
                update={"price": (leg + 1) * 1_000}
            )
        ]
        return ProductSearchResult(products=products, total_count=1)

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "categoryQueries": [{"category": category, "query": query} for category, query in legs],
            "filters": {},
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req("열 가지를 전부 추천해줘"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "BUY_ALL"
    assert all(len(item.product_ids) <= 9 for item in push.pushes[0].lists)
    assert any(event["type"] == "token" and "니즈9" in event["data"]["text"] for event in events)


async def test_budget_set_exception_degrades_to_pick_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R1 F2] 조합기 예외도 후보 폴백을 고지하고 PICK_ONE 스트림은 완료된다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(
        settings,
        "budget_set_candidate_fallback_notice",
        "후보\u200b 조합 오류로\n상품별로 보여드릴게요.",
    )

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파")])

    async def _search(filters, exclude_product_ids=None):
        product_id = 11 if filters.category == "A" else 21
        return ProductSearchResult(products=[_prod(product_id, filters.category)], total_count=1)

    def _boom(**kwargs):
        raise AssertionError("불변식 실패 흉내")

    monkeypatch.setattr(recommendation_graph, "build_budget_sets", _boom)
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
            ],
            "filters": {},
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req("감자탕 재료"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "PICK_ONE"
    notice_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "token"
        and event["data"]["text"] == "후보 조합 오류로 상품별로 보여드릴게요."
    )
    ready_index = next(
        index for index, event in enumerate(events) if event["type"] == "products.ready"
    )
    assert notice_index < ready_index
    assert _types(events)[-2:] == ["products.ready", "done"]


async def test_buy_all_without_budget_does_not_blame_unavailable_need_on_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """가격 후보가 없는 일부 니즈를 이름으로 고지하되 예산 제외와 섞지 않는다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(
        settings,
        "budget_set_unavailable_notice",
        "{items}은(는) 가격 후보가 없어 조합에서 뺐어요.",
    )

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파")])

    async def _search(filters, exclude_product_ids=None):
        products = [] if filters.category == "B" else [_prod(11, "A", "등뼈")]
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
            ],
            "filters": {},
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req("감자탕 재료 추천해줘"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "BUY_ALL"
    unavailable_indices = [
        index
        for index, event in enumerate(events)
        if event["type"] == "token" and "대파" in event["data"]["text"]
    ]
    assert unavailable_indices, "빠진 니즈 이름을 담은 token 고지가 없다"
    ready_index = next(
        index for index, event in enumerate(events) if event["type"] == "products.ready"
    )
    assert unavailable_indices[0] < ready_index
    assert all(
        settings.budget_set_dropped_notice.split("{items}", 1)[0] not in event["data"]["text"]
        for event in events
        if event["type"] == "token"
    )


async def test_unpriced_budget_set_uses_candidate_fallback_notice_not_budget_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """가격 없는 후보뿐이면 PICK_ONE으로 생존하고 예산이 아닌 후보 원인을 고지한다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(
        settings,
        "budget_set_candidate_fallback_notice",
        "가격 후보가 부족해 상품별로 보여드릴게요.",
    )

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "등뼈"), ("B", "대파")])

    async def _search(filters, exclude_product_ids=None):
        product_id = 11 if filters.category == "A" else 21
        product = _prod(product_id, filters.category).model_copy(update={"price": None})
        return ProductSearchResult(products=[product], total_count=1)

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "totalBudget": 10_000,
            "categoryQueries": [
                {"category": "A", "query": "등뼈"},
                {"category": "B", "query": "대파"},
            ],
            "filters": {"priceMax": 10_000},
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req("1만원으로 감자탕 재료 전부"),
            _member_num(),
            llm=llm,
            search=_search,
            push_fn=push,
            map_categories=_map,
        )
    )

    assert push.pushes[0].list_type == "PICK_ONE"
    token_texts = [event["data"]["text"] for event in events if event["type"] == "token"]
    assert settings.budget_set_candidate_fallback_notice in token_texts
    assert settings.budget_set_infeasible_notice not in token_texts
    assert _types(events)[-2:] == ["products.ready", "done"]


async def test_total_budget_output_controls_push_independently_of_price_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 상품별 상한에서도 별도 totalBudget 출력만 push 총액을 제어한다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 1)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("A", "파우치"), ("B", "어댑터")])

    async def _search(filters, exclude_product_ids=None):
        product_id = 11 if filters.category == "A" else 21
        return ProductSearchResult(products=[_prod(product_id, filters.category)], total_count=1)

    totals = []
    for index, total_budget in enumerate((50_000, None)):
        push = _RecordingPush()
        llm = FakeLLM(
            decompose={
                "intent": "recommend",
                "case": 3,
                "buyAll": True,
                "totalBudget": total_budget,
                "categoryQueries": [
                    {"category": "A", "query": "파우치"},
                    {"category": "B", "query": "어댑터"},
                ],
                "filters": {"priceMax": 50_000},
            }
        )
        await _collect(
            run_buyer_turn(
                _req(
                    "5만원으로 파우치와 어댑터",
                    session_id=f"budget-s{index}",
                    thread_id=f"budget-t{index}",
                ),
                _member_num(),
                llm=llm,
                search=_search,
                push_fn=push,
                map_categories=_map,
            )
        )
        totals.append(push.pushes[0].total_budget)

    assert totals == [50_000, None]


# ─────────── [#162] 조건 없는 발화 → 인기 상품(I-3) ───────────

_NO_CONDITION_DECOMPOSE = {"intent": "recommend", "filters": {}, "case": 2}


def _counting_search_calls(products=DEFAULT_PRODUCTS):
    """I-1 호출을 기록하는 검색 — "호출되지 않았다"를 검증하려면 셀 수 있어야 한다."""
    calls: list = []

    async def _search(filters, exclude_product_ids=None):
        calls.append(filters)
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _search, calls


def _recording_popular(products=DEFAULT_PRODUCTS, *, error: Exception | None = None):
    """I-3 호출을 기록하는 fake. `error` 를 주면 그 예외를 던진다(장애 재현)."""
    calls: list = []

    async def _popular(size):
        calls.append(size)
        if error is not None:
            raise error
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _popular, calls


async def test_no_condition_turn_uses_popular_instead_of_unfiltered_search() -> None:
    """조건 0개 발화는 I-1 을 부르지 않고 I-3 후보로 답한다 — 이 이슈의 본체.

    종전에는 파라미터 0개의 I-1 이 나가 매칭 전량(실측 7,245건·13.33MB)을 받았다.
    """
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    push = _RecordingPush()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-popular"),
            _guest(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert search_calls == []  # 무필터 I-1 이 나가지 않는다
    assert popular_calls == [get_settings().popular_candidate_size]  # config 값을 명시 전송
    assert "products.ready" in _types(events)
    assert _types(events)[-1] == "done"


async def test_no_condition_turn_discloses_popular_source() -> None:
    """안내를 함께 낸다 — 없으면 사용자가 인기 상품을 자기 조건이 반영된 결과로 오해한다."""
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-notice"),
            _guest(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any(get_settings().no_condition_notice_popular in t for t in texts)


async def test_condition_turn_never_calls_popular() -> None:
    """조건이 있는 턴은 종전 경로 그대로 — I-3 로 새면 사용자가 말한 조건이 버려진다."""
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(thread_id="nc-conditioned"),
            _guest(),
            llm=FakeLLM(),  # DEFAULT_DECOMPOSE — priceMax·keyword·categoryQueries 있음
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls == []
    assert search_calls  # I-1 은 정상 호출된다


async def test_popular_failure_degrades_to_search_without_false_claim() -> None:
    """I-3 장애면 종전 검색으로 degrade 하고 스트림은 살아 있다.

    이때 "인기 상품으로 보여드릴게요" 는 **내지 않는다** — 그 결과는 인기 상품이 아니라
    무필터 검색이라 거짓 주장이 된다(#133 정직성 규약).
    """
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(error=SpringUnavailableError("popular down"))

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-degrade"),
            _guest(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls  # 시도는 했다
    assert search_calls  # 종전 검색으로 떨어졌다
    types = _types(events)
    assert types[-1] == "done"  # 스트림이 죽지 않는다
    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert not any(get_settings().no_condition_notice_popular in t for t in texts)


async def test_popular_zero_results_is_not_a_degrade() -> None:
    """I-3 0건은 성공이다(§4.17) — 카드 없이 텍스트로 답하고 무필터 I-1 로 떨어지지 않는다.

    여기서 degrade 로 처리하면 이 이슈가 없애려는 13.33MB 호출을 도로 부른다.
    """
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=[])

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-zero"),
            _guest(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls
    assert search_calls == []  # 폴백하지 않는다
    types = _types(events)
    assert "products.ready" not in types  # 실을 카드가 없다
    assert types[-1] == "done"


# ─────────── [#162] 조건 없는 발화 + 프로필 → 취향 벡터 랭킹 ───────────


def _catalog_store(pids: list[int]):
    """3차원 임베딩 카탈로그 — 앞 번호일수록 [1,0,0] 축에 가깝다(순서를 눈으로 검증)."""
    from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore

    store = CatalogArtifactStore()
    for i, pid in enumerate(pids):
        store.upsert(
            CatalogArtifact(
                product_id=pid,
                search_doc=f"상품 {pid}",
                embedding=[1.0 - (i + 1) * 0.05, (i + 1) * 0.05, 0.0],
                extras={"review_pros": [f"{pid} 리뷰 장점"]},
            )
        )
    return store


def _inject_profile(monkeypatch: pytest.MonkeyPatch, *, vector, store) -> None:
    """프로필 요약(취향 벡터 포함)과 카탈로그 인덱스를 인메모리로 대체한다."""
    import app.agents.buyer.graph as buyer_graph
    import app.pipelines.artifact_store as artifact_store

    async def _summary(user_id):  # noqa: ANN001
        return {"markdown": "취향 요약", "generatedAt": "2026-08-05", "embedding": vector}

    monkeypatch.setattr(buyer_graph, "read_profile_summary", _summary)
    monkeypatch.setattr(artifact_store, "get_catalog_store", lambda: store)


async def test_profile_member_ranks_by_taste_vector_without_search_or_popular(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """취향 벡터가 있는 회원은 I-1·I-3 **둘 다 부르지 않고** 자체 인덱스에서 뽑는다.

    이 이슈가 노린 개인화의 본체다 — 무작위 후보를 받아 rerank 로 고르는 게 아니라, 후보
    단계부터 취향에 가까운 상품이 온다(홈 화면과 같은 엔진·같은 인덱스).
    """
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([201, 202, 203]))
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()
    push = _RecordingPush()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-profile"),
            _member(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert search_calls == []
    assert popular_calls == []
    assert _types(events)[-1] == "done"
    assert "products.ready" in _types(events)
    entry = _only_list(push.pushes[0])
    assert entry.product_ids[0] == 201  # 취향 벡터에 가장 가까운 상품이 앞


async def test_profile_member_discloses_taste_based_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회원 경로는 인기 상품이 아니라 취향 기반이므로 안내 문구도 다르다."""
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([201, 202]))
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-profile-notice"),
            _member(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    settings = get_settings()
    assert any(settings.no_condition_notice_profile in t for t in texts)
    assert not any(settings.no_condition_notice_popular in t for t in texts)


async def test_profile_push_failure_does_not_persist_last_reco(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#435 W5] 프로필 벡터 경로도 push 실패 턴엔 `last_reco` 를 저장하지 않는다 — 정상(검색)
    경로와 같은 규약(의도된 동작, 바꾸지 않고 고정만 한다)."""
    from app.agents.buyer.cart.state import get_cart_store

    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([601, 602]))
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()
    request = _req(message="아무거나 추천해줘", thread_id="push-fail-profile-435")

    events = await _collect(
        run_buyer_turn(
            request,
            _member_num(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_failing_push,
            popular_fn=popular,
        )
    )
    assert "products.ready" not in _types(events)
    key = await _thread_key(request, _member_num())
    cart_store = await get_cart_store()
    assert await cart_store.get_last_reco(key) == []
    assert await cart_store.get_push_failed(key) is True


# ─────────── [#435] 이음매 회귀 — "추천 → 이름으로 지목해 찜/담기" ───────────


class _NameMatchDecomposeLLM:
    """LAST_RECOMMENDATIONS 이름 매칭 실측(#118, N=8 프로브 8/8)의 대역.

    실 LLM 을 흉내내는 게 아니라, decompose 프롬프트에 **이미 실린** `LAST_RECOMMENDATIONS`
    (productId+name)에서 발화(`USER_MESSAGE`)에 등장하는 이름의 productId 를 고른다 — 이름이
    실려 있지 않거나 발화에 없으면 `productId=null` 을 낸다(#435 판정 근거 1, 패킷 §1).
    """

    def __init__(self, *, intent: str) -> None:
        self._intent = intent

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        assert tier == "fast"  # 이 시나리오는 decompose 만 탄다(rerank 는 검색/프로필 경로 전용)
        reco_line = next(
            line for line in user.splitlines() if line.startswith("LAST_RECOMMENDATIONS: ")
        )
        reco = json.loads(reco_line[len("LAST_RECOMMENDATIONS: ") :])
        message_line = next(line for line in user.splitlines() if line.startswith("USER_MESSAGE: "))
        message = message_line[len("USER_MESSAGE: ") :]
        product_id = next(
            (
                entry["productId"]
                for entry in reco
                if entry.get("name") and entry["name"] in message
            ),
            None,
        )
        return json.dumps(
            {
                "intent": self._intent,
                "reply": "",
                "case": 2,
                "filters": {},
                "cart": {"productId": product_id, "optionId": None, "quantity": 1},
            },
            ensure_ascii=False,
        )

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "x"


async def _recommend_via_profile_and_get_named_reco(
    monkeypatch: pytest.MonkeyPatch, *, thread_id: str, pids: list[int]
) -> str:
    """프로필 벡터 경로로 1턴 추천하고, 그 턴이 저장한 누적 추천 중 **이름이 실린** 항목 하나를
    돌려준다(W2 회귀의 턴 1 공용부)."""
    from app.agents.buyer.cart.state import get_cart_store

    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store(pids))
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()

    turn1_events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id=thread_id),
            _member_num(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    assert "products.ready" in _types(turn1_events)

    key = await _thread_key(_req(thread_id=thread_id), _member_num())
    cart_store = await get_cart_store()
    reco = await cart_store.get_last_reco(key)
    named = [name for _, name in reco if name]
    assert named, "이 회귀는 프로필 경로가 이름을 실제로 실었다는 전제 위에 있다(#435 W1)"
    return named[0]


async def test_profile_recommendation_name_target_wishlist_add_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#435] 프로필 벡터 경로로 추천된 상품을 **이름으로** 지목한 찜이 성공한다.

    #435 가 고치는 결함 그 자체 — 과거 이 경로는 빈 이름(`(pid, "")`)만 저장해, 사용자가 화면에서
    본 상품명을 그대로 말해도 decompose 가 LAST_RECOMMENDATIONS 에서 매칭할 이름이 없어
    `productId=null` → 미해소로 실패했다.
    """
    import app.services.spring_client as spring_client

    thread_id = "nc-435-wishlist"
    target_name = await _recommend_via_profile_and_get_named_reco(
        monkeypatch, thread_id=thread_id, pids=[401, 402]
    )

    async def fake_add_wishlist(req):  # noqa: ANN001
        return None

    monkeypatch.setattr(spring_client, "add_wishlist", fake_add_wishlist)

    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()
    turn2_events = await _collect(
        run_buyer_turn(
            _req(message=f"{target_name} 찜해줘", thread_id=thread_id),
            _member_num(),
            llm=_NameMatchDecomposeLLM(intent="wishlist_add"),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    actions = [e for e in turn2_events if e["type"] == "action"]
    assert actions and actions[0]["data"]["type"] == "WISHLIST_ADDED"


async def test_profile_recommendation_name_target_cart_add_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#435 W2] 담기(cart_add)도 같은 이음매를 갖는다 — 최소 1건으로 함께 고정한다."""
    import app.services.spring_client as spring_client
    from app.schemas.spring import AddToCartResult, CartView

    thread_id = "nc-435-cartadd"
    target_name = await _recommend_via_profile_and_get_named_reco(
        monkeypatch, thread_id=thread_id, pids=[501, 502]
    )

    async def fake_get_cart(*, user_id=None, guest_id=None):  # noqa: ANN001
        return CartView(items=[])

    async def fake_add_to_cart(req):  # noqa: ANN001
        return AddToCartResult(success=True, cart_item_id=9001)

    monkeypatch.setattr(spring_client, "get_cart", fake_get_cart)
    monkeypatch.setattr(spring_client, "add_to_cart", fake_add_to_cart)

    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()
    turn2_events = await _collect(
        run_buyer_turn(
            _req(message=f"{target_name} 담아줘", thread_id=thread_id),
            _member_num(),
            llm=_NameMatchDecomposeLLM(intent="cart_add"),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )
    actions = [e for e in turn2_events if e["type"] == "action"]
    assert actions and actions[0]["data"]["type"] == "CART_ADDED"


def _no_name_category_store(pid: int, category: str = "생활용품", extras: dict | None = None):
    """이름 없는 상품 1건 — `search_doc` 첫 줄이 `category` 로 밀리는 [#435 리뷰 C1] 재현용."""
    from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore

    store = CatalogArtifactStore()
    store.upsert(
        CatalogArtifact(
            product_id=pid,
            search_doc=category,
            embedding=[1.0, 0.0, 0.0],
            extras=extras or {},
        )
    )
    return store


async def test_profile_recommendation_cross_turn_category_fallback_names_deduped_against_accumulated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#435 리뷰 C1] 이름 없는 상품의 카테고리 폴백 이름이 **스레드 누적**에서 다른 productId
    와 겹치면 두 번째 노출에서 버려진다.

    재현 시나리오(턴1 [101]→"생활용품" 누적, 턴3 [202]도 "생활용품")를 실제 `stream_recommendation`
    을 **두 번 직접 호출**해 고정한다 — `run_buyer_turn` 전체를 거치면 첫 턴이 성공하는 순간
    `_prepare_recommendation` 이 `thread_store.put` 으로 필터를 영속시켜(추천 intent 턴은 항상
    저장) 그 다음 턴부터 `is_no_condition_turn` 의 ①(`prior is None`)이 막혀 프로필 경로가
    다시는 열리지 않는다(이 경로가 "첫 턴 한정"으로 설계된 이유, `no_condition.py` docstring
    참조) — 즉 **같은 스레드에서 프로필 경로가 두 번 열리는 것 자체가 `thread_store` 와 무관한
    상위 계층 문제라, 이 테스트는 그 상위 계층을 우회해 `no_condition=True` 를 직접 두 번 준다.**
    누적은 `cart_store`(진짜 인스턴스, `run_buyer_turn` 과 같은 store 타입)를 공유해 실제
    `set_last_reco`/`dedup_exposed_names` 왕복을 그대로 태운다.
    """
    from app.agents.buyer.cart.state import CartStateStore
    from app.agents.buyer.recommendation.graph import stream_recommendation
    from app.agents.buyer.recommendation.state import RouteDecision
    from app.schemas.spring import ProductSearchFilters

    cart_store = CartStateStore()
    thread_key = "t-435-c1-cross-turn"
    decision = RouteDecision(
        intent="recommend", filters=ProductSearchFilters(), semantic_query_is_fallback=True
    )

    async def _run_turn(pid: int, request_id: str):
        monkeypatch.setattr(
            "app.pipelines.artifact_store.get_catalog_store",
            lambda: _no_name_category_store(pid),
        )
        return await _collect(
            stream_recommendation(
                request=_req(thread_id="nc-435-c1-cross-turn"),
                decision=decision,
                llm=FakeLLM(),
                search=_make_search([]),
                push_fn=_RecordingPush(),
                identity=None,
                profile=None,
                settings=get_settings(),
                cart_store=cart_store,
                thread_key=thread_key,
                request_id=request_id,
                no_condition=True,
                popular_fn=_recording_popular()[0],
                profile_vec=[1.0, 0.0, 0.0],
            )
        )

    turn1 = await _run_turn(101, "req-1")
    assert "products.ready" in _types(turn1)
    assert dict(await cart_store.get_last_reco(thread_key)) == {101: "생활용품"}

    turn2 = await _run_turn(202, "req-2")
    assert "products.ready" in _types(turn2)

    accumulated = dict(await cart_store.get_last_reco(thread_key))
    # 오확정을 막는 핵심 단언 — 같은 이름이 두 productId 에 동시에 남으면 안 된다.
    assert accumulated.get(202, "") == "", accumulated
    assert accumulated.get(101) == "생활용품", accumulated


async def test_profile_recommendation_discards_flagged_category_fallback_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#468 I-17] 명시적 이름 없음 상품 하나도 추천은 유지하되 이름은 누적하지 않는다.

    `rank_by_profile`가 `name_from_artifact` 대신 첫 줄 추출기를 직접 쓰면 이 테스트는
    "생활용품"이 last_reco에 남아 실패한다. 플래그 없는 대조군은 바로 위 #435 cross-turn
    테스트의 첫 턴(``{101: "생활용품"}``)이 기존 동작으로 이미 고정한다.
    """
    from app.agents.buyer.cart.state import CartStateStore
    from app.agents.buyer.recommendation.graph import stream_recommendation
    from app.agents.buyer.recommendation.state import RouteDecision
    from app.pipelines.artifact_store import EXTRAS_NAME_PRESENT_KEY
    from app.schemas.spring import ProductSearchFilters

    cart_store = CartStateStore()
    thread_key = "t-468-flagged-category-fallback"
    monkeypatch.setattr(
        "app.pipelines.artifact_store.get_catalog_store",
        lambda: _no_name_category_store(101, extras={EXTRAS_NAME_PRESENT_KEY: False}),
    )
    events = await _collect(
        stream_recommendation(
            request=_req(thread_id="nc-468-flagged-category-fallback"),
            decision=RouteDecision(
                intent="recommend", filters=ProductSearchFilters(), semantic_query_is_fallback=True
            ),
            llm=FakeLLM(),
            search=_make_search([]),
            push_fn=_RecordingPush(),
            identity=None,
            profile=None,
            settings=get_settings(),
            cart_store=cart_store,
            thread_key=thread_key,
            request_id="req-468-flagged-category-fallback",
            no_condition=True,
            popular_fn=_recording_popular()[0],
            profile_vec=[1.0, 0.0, 0.0],
        )
    )

    assert "products.ready" in _types(events)
    assert dict(await cart_store.get_last_reco(thread_key)) == {101: ""}


async def test_member_without_taste_vector_falls_back_to_popular(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프로필은 있는데 **벡터가 없는** 회원(구 요약·임베딩 실패)은 인기 상품으로 간다.

    None 벡터를 그대로 랭킹에 넣으면 빈 결과가 나오고 그게 "추천할 게 없다"로 오독된다.
    """
    _inject_profile(monkeypatch, vector=None, store=_catalog_store([201, 202]))
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-no-vector"),
            _member(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls  # I-3 로 떨어진다
    assert search_calls == []  # 무필터 I-1 은 여전히 부르지 않는다


async def test_empty_catalog_index_falls_back_to_popular(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카탈로그 인덱스가 비었으면(동기화 전·장애) 인기 상품으로 폴백하고 스트림은 산다."""
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([]))
    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-empty-index"),
            _member(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls
    assert _types(events)[-1] == "done"


async def test_profile_fallback_does_not_refetch_purchases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """취향 랭킹이 폴백해도 I-19 는 **한 번만** 부른다.

    취향 경로가 dedup 재료로 I-19 를 먼저 부르는데, 랭킹이 실패해 인기 상품 경로로 떨어지면
    그쪽 gather 가 또 부른다 — 같은 턴에 3s 짜리 Spring 호출이 두 번 나가는 셈이다.
    """
    _fix_now(monkeypatch)
    calls: list[int] = []
    real = _purchases(101)

    async def _spy(user_id, status=None):  # noqa: ANN001
        calls.append(user_id)
        return await real(user_id, status)

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _spy)
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([]))  # 랭킹 0건
    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-purchases-once"),
            _member_num(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls  # 인기 상품으로 폴백했다
    assert len(calls) == 1  # 그런데 I-19 는 한 번뿐


# ─────────── [#162 / PR #311 리뷰] 총액 예산만 말한 턴 ───────────

_BUDGET_ONLY_DECOMPOSE = {
    "intent": "recommend",
    "filters": {},
    "case": 2,
    "buyAll": True,
    "totalBudget": 50000,
}

_PRICED_POPULAR = [
    SpringProduct(product_id=101, name="싼 것", price=30000, category="c", brand="b"),
    SpringProduct(product_id=102, name="비싼 것", price=80000, category="c", brand="b"),
    SpringProduct(product_id=103, name="딱 맞는 것", price=50000, category="c", brand="b"),
]


async def test_budget_only_turn_filters_popular_by_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "총 5만원 있어 아무거나" — 인기 상품 중 **예산 이하만** 후보로 남는다.

    세트로 묶지 않는다: 무엇을 몇 개 살지 사용자가 말하지 않아 조합 기준이 없다. 대신 예산 안의
    대안을 보여주고 대화로 되묻는다.
    """
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([201, 202]))
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(_PRICED_POPULAR)
    push = _RecordingPush()

    await _collect(
        run_buyer_turn(
            _req(message="총 5만원 있어 아무거나 추천해줘", thread_id="nc-budget"),
            _member(),
            llm=FakeLLM(decompose=_BUDGET_ONLY_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert popular_calls  # 인기 상품 경로를 탄다
    assert search_calls == []  # 무필터 I-1 로 되돌아가지 않는다
    entry = _only_list(push.pushes[0])
    assert 102 not in entry.product_ids  # 8만원짜리는 빠진다
    assert set(entry.product_ids) <= {101, 103}
    assert push.pushes[0].list_type == "PICK_ONE"  # 세트가 아니라 대안이다
    assert push.pushes[0].total_budget is None


async def test_budget_only_turn_skips_taste_vector_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """취향 벡터가 있어도 예산 턴은 그 경로를 타지 않는다.

    AI 카탈로그 인덱스(`CatalogArtifact`)에 **가격이 없어** 예산을 확인할 방법이 없다.
    그 경로로 보내면 5만원이라 말한 사용자에게 8만원짜리가 나갈 수 있다.
    """
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([201, 202]))
    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular(_PRICED_POPULAR)
    push = _RecordingPush()

    await _collect(
        run_buyer_turn(
            _req(message="총 5만원 있어 아무거나 추천해줘", thread_id="nc-budget-taste"),
            _member(),
            llm=FakeLLM(decompose=_BUDGET_ONLY_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert popular_calls  # 취향 랭킹(201·202)이 아니라 인기 상품으로 갔다
    assert not set(_only_list(push.pushes[0]).product_ids) & {201, 202}


async def test_budget_only_turn_discloses_amount_and_asks_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """안내에 **금액을 되짚고 예시로 되묻는다** — "조건을 안 주셨다"고 단정하지 않는다."""
    _inject_profile(monkeypatch, vector=None, store=_catalog_store([]))
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular(_PRICED_POPULAR)

    events = await _collect(
        run_buyer_turn(
            _req(message="총 5만원 있어 아무거나 추천해줘", thread_id="nc-budget-notice"),
            _guest(),
            llm=FakeLLM(decompose=_BUDGET_ONLY_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    settings = get_settings()
    assert any("50,000원" in t for t in texts)  # 금액을 되짚는다
    assert not any(settings.no_condition_notice_popular in t for t in texts)  # 일반 문구 아님


async def test_profile_path_discloses_dedup_failure_like_the_other_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """취향 경로도 I-19 실패 고지를 낸다 — 이 경로만 건너뛰면 config 스위치가 반쪽이 된다.

    `dedup_skipped_notice` 는 기본이 빈 값(미고지)이지만, **판단을 코드 재배포 없이 되돌리기
    위한 여지**로 남겨 둔 스위치다(#133). 취향 경로는 `done` 을 내고 곧바로 return 해서 하류의
    고지 지점에 도달하지 못했다 — 운영자가 값을 채우는 순간 "인기 경로에서는 고지되는데 취향
    경로에서만 조용히 묻히는" 비대칭이 드러난다(PR #311 리뷰).
    """
    _fix_now(monkeypatch)

    async def _boom(user_id, status=None):  # noqa: ANN001
        raise SpringUnavailableError("orders down")

    monkeypatch.setattr(_sc_mod, "get_recent_purchases", _boom)
    monkeypatch.setattr(
        get_settings(), "dedup_skipped_notice", "최근 구매 내역을 확인하지 못했어요."
    )
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([201, 202]))
    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-profile-dedup"),
            _member_num(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls == []  # 취향 경로를 탄 턴이다
    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any("최근 구매 내역을 확인하지 못했어요." in t for t in texts)


async def _failed_mapping(*args, **kwargs):
    """canonical 매핑이 아무것도 못 찾은 상태 — legs 가 빈 CategoryMapping."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    return CategoryMapping()


async def test_multi_item_utterance_with_failed_mapping_falls_back_to_popular() -> None:
    """ "이어폰이랑 노트북 추천해줘" — 매핑이 전부 실패하면 무필터 I-1 대신 인기 상품으로 답한다.

    상품 2개 지목은 `cat_signal` 승격 조건(`decompose.py:585`, leg 1개)에 안 걸려 `semantic_query`
    로도 `filters.keyword` 로도 안 실린다. 매핑까지 실패하면 `category_legs` 도 비어, 이 턴의 실제
    Spring payload 는 **파라미터 0개**다.

    **PR #311 이 지키려던 것과 #393 이 그 경계를 옮긴 이유**: PR #311(리뷰)은 "매핑이 드롭돼도
    `category_queries` 원시 신호가 있으면 인기 상품으로 새면 안 된다"고 판단해 종전 무필터 검색
    경로를 지켰다 — 그 판단은 **그 무필터 검색이 실제로 결과를 준다는 전제** 위에 있었다. 상품
    100→6,559건으로 카탈로그가 커진 지금 그 전제가 깨졌다(운영 실측: 무필터 I-1 은 7.74초·
    12.3MB → 3초 예산 초과 → **결과가 아니라 에러**). 사용자가 말한 상품군을 지키려다 빈손 대신
    `SEARCH_FAILED` 를 주는 것은 원래 목적에 반한다 — #393 A(payload 사실 판정)는 매핑 드롭
    여부와 무관하게 이 턴을 인기 상품 + 정직한 고지로 돌린다("인기 상품 + 정직한 고지 >
    SEARCH_FAILED"). `search_filter_guard_enabled=False` 로 종전 동작(무필터 I-1)을 되돌릴 수
    있다 — 아래 롤백 테스트가 그 회귀를 지킨다.
    """
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="이어폰이랑 노트북 추천해줘", thread_id="nc-multi-item"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [
                        {"category": None, "query": "무선 이어폰"},
                        {"category": None, "query": "노트북"},
                    ],
                }
            ),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
            map_categories=_failed_mapping,  # 매핑 실패 재현
        )
    )

    assert search_calls == []  # 무필터 I-1 이 나가지 않는다 — #393 A 의 핵심
    assert popular_calls == [get_settings().popular_candidate_size]  # I-3 로 갔다
    types = _types(events)
    assert "error" not in types
    assert types[-1] == "done"
    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any(get_settings().no_condition_notice_popular in t for t in texts)


async def test_multi_item_utterance_with_failed_mapping_keeps_normal_search_when_guard_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#393 롤백] `search_filter_guard_enabled=False` 면 PR #311 이 지키던 종전 동작(무필터
    검색 경로 유지, 인기 상품으로 새지 않음)이 그대로 재현된다."""
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="이어폰이랑 노트북 추천해줘", thread_id="nc-multi-item-guard-off"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "reply": "",
                    "case": 3,
                    "filters": {},
                    "categoryQueries": [
                        {"category": None, "query": "무선 이어폰"},
                        {"category": None, "query": "노트북"},
                    ],
                }
            ),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
            map_categories=_failed_mapping,  # 매핑 실패 재현
        )
    )

    assert popular_calls == []  # 인기 상품으로 새지 않는다
    assert search_calls  # 종전 검색 경로를 그대로 탄다


async def test_budget_notice_without_placeholder_falls_back_to_popular_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`{budget}` 자리표시자가 빠진 오설정은 **금액 없는 문구를 조용히 내보내지 않는다**.

    `str.format` 은 쓰지 않는 키워드를 예외 없이 무시하므로 except 로는 못 잡는다(PR #311 리뷰).
    금액을 되짚지 못할 바에는 금액을 주장하지 않는 인기 상품 문구로 떨어뜨린다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "no_condition_notice_budget", "금액 없이 골라봤어요.")
    _inject_profile(monkeypatch, vector=None, store=_catalog_store([]))
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular(_PRICED_POPULAR)

    events = await _collect(
        run_buyer_turn(
            _req(message="총 5만원 있어 아무거나 추천해줘", thread_id="nc-budget-badcfg"),
            _guest(),
            llm=FakeLLM(decompose=_BUDGET_ONLY_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert not any("금액 없이 골라봤어요." in t for t in texts)  # 오설정 문구는 안 나간다
    assert any(settings.no_condition_notice_popular in t for t in texts)  # 안전한 문구로 폴백


async def test_catalog_store_failure_keeps_stream_alive_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카탈로그 스토어 확보가 **실패해도** 스트림이 죽지 않고 인기 상품으로 폴백한다.

    `get_catalog_store()` 는 pg 커넥션 풀을 만드는 지점이라 실패하기 쉬운데, 종전에는
    `_call_store(rank_candidates)` 만 try 로 감싸 그 앞 두 줄이 무방비였다. 거기서 예외가 나면
    `stream_recommendation` 제너레이터가 그대로 죽어 **`done` 도 `error` 도 없이 SSE 가
    끊긴다**(PR #311 리뷰, 재현 확인). §7 "실패해도 턴을 죽이지 않는다"에 어긋난다.
    """
    import app.pipelines.artifact_store as artifact_store

    def _boom():
        raise RuntimeError("catalog store init failed (pg down)")

    async def _summary(user_id):  # noqa: ANN001
        return {"markdown": "취향 요약", "embedding": [1.0, 0.0, 0.0]}

    import app.agents.buyer.graph as buyer_graph

    monkeypatch.setattr(buyer_graph, "read_profile_summary", _summary)
    monkeypatch.setattr(artifact_store, "get_catalog_store", _boom)
    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="아무거나 추천해줘", thread_id="nc-store-down"),
            _member(),
            llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    types = _types(events)
    assert types[-1] == "done"  # 스트림이 정상 종료된다
    assert "error" not in types
    assert popular_calls  # 인기 상품 폴백을 실제로 탄다


# ─────────── #393 검색 필터 가드 (A: 최소 필터 가드, B: 매핑 드롭 0건 폴백, C: 인기 후보 사후필터) ───────────

_RATING_ONLY_DECOMPOSE = {"intent": "recommend", "filters": {"ratingMin": 4.5}, "case": 2}


async def test_unfiltered_bypass_turn_skips_search_and_uses_popular() -> None:
    """[A 회귀] rating_min 만 있는 턴은 payload 기준 무필터다 — I-1 이 나가지 않고 I-3 로 답하며
    no_condition 과 같은 고지가 나간다(그 턴도 payload 기준으로는 조건이 하나도 안 나갔다)."""
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="평점 4 이상 아무거나 추천해줘", thread_id="unfiltered-bypass"),
            _guest(),
            llm=FakeLLM(decompose=_RATING_ONLY_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert search_calls == []  # 무필터 I-1 이 나가지 않는다
    assert popular_calls == [get_settings().popular_candidate_size]
    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any(get_settings().no_condition_notice_popular in t for t in texts)


async def test_unfiltered_bypass_can_be_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """[A 롤백] `search_filter_guard_enabled=False` 면 종전대로 무필터 I-1 이 나간다."""
    monkeypatch.setattr(get_settings(), "search_filter_guard_enabled", False)
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="평점 4 이상 아무거나 추천해줘", thread_id="unfiltered-bypass-off"),
            _guest(),
            llm=FakeLLM(decompose=_RATING_ONLY_DECOMPOSE),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert search_calls  # 종전대로 무필터 I-1 이 나간다
    assert popular_calls == []


def _shoe_decompose(*, extra_filters: dict | None = None) -> dict:
    filters = {"keyword": "신발"}
    filters.update(extra_filters or {})
    return {
        "intent": "recommend",
        "case": 2,
        "categoryQueries": [{"category": "신발", "query": None}],
        "filters": filters,
    }


async def test_category_mapping_dropped_zero_result_falls_back_to_popular() -> None:
    """[B 회귀 — "신발" 시나리오] 매핑이 드롭돼도 keyword 검색을 **먼저** 시도하고, 그게 0건일
    때만 인기 상품으로 대체한다 — 사전 우회가 아니라 사후 폴백이다."""
    search, search_calls = _counting_search_calls(products=[])
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="신발 추천해줘", thread_id="shoe-mapping-dropped"),
            _guest(),
            llm=FakeLLM(decompose=_shoe_decompose()),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
            map_categories=_failed_mapping,
        )
    )

    assert len(search_calls) == 1  # keyword 검색을 먼저 시도했다 — 사전 우회가 아니다
    assert popular_calls == [get_settings().popular_candidate_size]
    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any(get_settings().category_unmapped_notice in t for t in texts)


async def test_category_mapping_dropped_with_results_does_not_fall_back() -> None:
    """[B] keyword 검색이 실제로 결과를 내면 인기 상품으로 대체하지 않는다 — 관련 결과가
    인기 상품보다 낫다."""
    search, search_calls = _counting_search_calls()  # DEFAULT_PRODUCTS(비어있지 않음)
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="신발 추천해줘", thread_id="shoe-mapping-dropped-hit"),
            _guest(),
            llm=FakeLLM(decompose=_shoe_decompose()),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
            map_categories=_failed_mapping,
        )
    )

    assert len(search_calls) == 1
    assert popular_calls == []  # 대체하지 않는다


async def test_category_mapping_dropped_deferred_turn_does_not_fall_back() -> None:
    """[B 지연 가드] `may_auto_relax` 턴(ratingMin 도 함께 걸림)은 첫 이벤트 앞 직렬 호출이
    늘어나지 않게 B 를 발동하지 않는다 — 0건이어도 인기 상품으로 대체하지 않는다(#277)."""
    search, search_calls = _counting_search_calls(products=[])
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="신발 평점 4점 이상 추천해줘", thread_id="shoe-mapping-dropped-deferred"),
            _guest(),
            llm=FakeLLM(decompose=_shoe_decompose(extra_filters={"ratingMin": 4.5})),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
            map_categories=_failed_mapping,
        )
    )

    assert (
        search_calls
    )  # 본 검색은 그대로 나간다(+ ratingMin 자동완화 probe 재검색이 더 갈 수 있다)
    assert popular_calls == []  # 그러나 0건이어도 인기 상품으로 새지 않는다


async def test_category_mapping_dropped_with_brand_does_not_fall_back() -> None:
    """[PR #411 Claude 리뷰 — "나이키 신발" 회귀] payload 에 `brand` 가 남아 있으면 매핑 드롭
    0건이어도 B 를 발동하지 않는다 — 인기 후보는 브랜드를 걸러주지 않는데 `conditions` 칩엔
    "나이키"가 그대로 떠 표시-실제가 어긋난다. 종전 동작(0건 응답)을 그대로 유지한다."""
    search, search_calls = _counting_search_calls(products=[])
    popular, popular_calls = _recording_popular()

    events = await _collect(
        run_buyer_turn(
            _req(message="나이키 신발 추천해줘", thread_id="shoe-mapping-dropped-brand"),
            _guest(),
            llm=FakeLLM(decompose=_shoe_decompose(extra_filters={"brand": ["나이키"]})),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
            map_categories=_failed_mapping,
        )
    )

    # 브랜드 검색은 그대로 나갔다(+ 0건이라 브랜드 완화 칩 probe 재검색이 더 갈 수 있다, #113).
    assert search_calls
    assert search_calls[0].brand == ["나이키"]
    assert popular_calls == []  # B 미발동 — 인기 상품으로 새지 않는다
    types = _types(events)
    assert "error" not in types
    assert types[-1] == "done"
    conditions_event = next(e for e in events if e["type"] == "conditions")
    chips = conditions_event["data"]["chips"]
    # 브랜드 칩이 conditions 에 그대로 있다 — 실제로 브랜드 검색이 나갔고 결과가 0건이었으므로
    # 표시(칩)와 실제(요청)가 일치한다.
    assert any(c["field"] == "brand" for c in chips)


async def test_category_mapping_dropped_with_price_still_falls_back_to_popular() -> None:
    """[PR #411 Claude 리뷰] `keyword`+가격만 남은 매핑 드롭 0건 턴은 여전히 B 가 발동한다 —
    가격은 `within_price_range` 로 인기 후보에 실제로 적용되는 안전한 축이다."""
    over_budget = SpringProduct(
        product_id=901, name="비싼 신발", price=90000, category="신발", brand="b"
    )
    under_budget = SpringProduct(
        product_id=902, name="싼 신발", price=30000, category="신발", brand="b"
    )
    search, search_calls = _counting_search_calls(products=[])
    popular, popular_calls = _recording_popular(products=[over_budget, under_budget])
    push = _RecordingPush()

    events = await _collect(
        run_buyer_turn(
            _req(message="5만원 이하 신발 추천해줘", thread_id="shoe-mapping-dropped-price"),
            _guest(),
            llm=FakeLLM(decompose=_shoe_decompose(extra_filters={"priceMax": 50000})),
            search=search,
            push_fn=push,
            popular_fn=popular,
            map_categories=_failed_mapping,
        )
    )

    # 본 검색은 그대로 나갔다(+ 0건이라 가격 완화 칩 probe 재검색이 더 갈 수 있다, #113).
    assert search_calls
    assert popular_calls == [get_settings().popular_candidate_size]  # B 발동
    exposed = set(_only_list(push.pushes[0]).product_ids)
    assert 901 not in exposed  # priceMax 초과 — within_price_range 로 걸러짐
    assert 902 in exposed
    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert any(get_settings().category_unmapped_notice in t for t in texts)


async def test_unfiltered_bypass_popular_candidates_drop_rating_below_threshold() -> None:
    """[C] rating_min 만 있는 턴이 A 로 인기 후보를 받을 때 평점 미달 후보는 제외된다 —
    조건 칩엔 "평점 4.5 이상"이 떠 있는데 후보가 그 조건을 안 지키는 표시-실제 불일치를 막는다.
    데이터 부재(`rating=None`·`review_count==0`)는 반증이 아니므로 보존한다.

    [F2-1] `conditions` 칩과 C 의 사후필터는 **같은 `decision.filters` 객체**를 읽는다(칩은
    `_condition_chips`→`build_condition_chips(decision.filters, ...)`, 사후필터는
    `apply_ai_side_filters(products, decision.filters)`) — 구조적으로 어긋날 수 없지만, 그 사실
    자체를 테스트로 고정해 둔다.
    """
    products = [
        SpringProduct(product_id=201, name="A", price=10000, rating=4.8, review_count=10),
        SpringProduct(product_id=202, name="B", price=10000, rating=3.0, review_count=5),  # 미달
        SpringProduct(product_id=203, name="C", price=10000, rating=None, review_count=None),
        SpringProduct(product_id=204, name="D", price=10000, rating=0.0, review_count=0),
    ]
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=products)
    push = _RecordingPush()

    events = await _collect(
        run_buyer_turn(
            _req(
                message="평점 4.5 이상 아무거나 추천해줘", thread_id="unfiltered-rating-postfilter"
            ),
            _guest(),
            llm=FakeLLM(decompose=_RATING_ONLY_DECOMPOSE),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert search_calls == []
    assert popular_calls
    exposed = set(_only_list(push.pushes[0]).product_ids)
    assert 202 not in exposed  # 반증됨(리뷰 있음·미달) — 제외
    assert {201, 203, 204} <= exposed  # 통과 + 데이터 부재 보존

    conditions_event = next(e for e in events if e["type"] == "conditions")
    chips = conditions_event["data"]["chips"]
    assert any(
        c["field"] == "ratingMin" and c["value"] == 4.5 for c in chips
    )  # 표시(칩)와 실제(후보)가 일치 — 202 는 위에서 이미 제외됨을 확인했다


async def test_unfiltered_bypass_popular_candidates_apply_attr_conditions() -> None:
    """[C] attr_conditions 만 있는 턴도 인기 후보에 같은 사후필터가 걸린다."""
    products = [
        SpringProduct(
            product_id=301,
            name="A",
            price=10000,
            attributes={"방수": "true"},
        ),
        SpringProduct(
            product_id=302,
            name="B",
            price=10000,
            attributes={"방수": "false"},
        ),
    ]
    search, _ = _counting_search_calls()
    popular, popular_calls = _recording_popular(products=products)
    push = _RecordingPush()

    await _collect(
        run_buyer_turn(
            _req(message="방수되는 거 아무거나 추천해줘", thread_id="unfiltered-attr-postfilter"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "case": 2,
                    "attrConditions": {"방수": "true"},
                    "filters": {},
                }
            ),
            search=search,
            push_fn=push,
            popular_fn=popular,
        )
    )

    assert popular_calls
    exposed = set(_only_list(push.pushes[0]).product_ids)
    assert exposed == {301}


async def test_category_legs_mapped_turn_never_calls_popular() -> None:
    """[매핑 성공 턴 무영향] `category_legs` 가 있는 턴은 A/B 어느 쪽에도 걸리지 않는다 —
    `popular_fn` 이 한 번도 불리지 않는다."""
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="신발 추천해줘", thread_id="shoe-mapping-succeeded"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "case": 2,
                    "categoryQueries": [{"category": "신발", "query": None}],
                    "filters": {},
                }
            ),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert popular_calls == []
    assert search_calls  # canonical 매핑 성공 → 종전 fan-out 검색


async def test_relaxation_probe_with_empty_filters_skips_spring_call() -> None:
    """[완화 probe 가드] brand 하나만 걸린 턴에서 완화 칩이 그 축을 제거하면 payload 가
    비므로, probe 는 Spring 을 부르지 않고 빈 결과로 처리한다(본 검색은 정상적으로 나간다).
    """
    main_products = [SpringProduct(product_id=401, name="A", price=10000, brand="나이키")]
    search, search_calls = _counting_search_calls(products=main_products)
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="나이키 아무거나 추천해줘", thread_id="brand-only-probe"),
            _guest(),
            llm=FakeLLM(
                decompose={
                    "intent": "recommend",
                    "case": 2,
                    "filters": {"brand": ["나이키"]},
                }
            ),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    # 본 검색 1회만 나간다 — brand 완화 probe 는 payload 가 비어 Spring 을 부르지 않는다.
    assert len(search_calls) == 1
    assert popular_calls == []


async def test_underspecified_flag_off_default_unaffected_by_393() -> None:
    """[기존 판정 불변] `underspecified_reask_enabled` 기본 off 에서, 가격 제약만 있는 턴은
    (payload 축이라 A 에도 안 걸리고) 종전처럼 무필터 I-1 이 아니라 필터 검색으로 나간다 —
    #393 이 이 플래그의 dormant 경로를 건드리지 않았음을 고정한다."""
    assert get_settings().underspecified_reask_enabled is False
    search, search_calls = _counting_search_calls()
    popular, popular_calls = _recording_popular()

    await _collect(
        run_buyer_turn(
            _req(message="5만원 이하로 아무거나 추천해줘", thread_id="underspecified-flag-off"),
            _guest(),
            llm=FakeLLM(
                decompose={"intent": "recommend", "case": 2, "filters": {"priceMax": 50000}}
            ),
            search=search,
            push_fn=_RecordingPush(),
            popular_fn=popular,
        )
    )

    assert search_calls  # priceMax 는 payload 축이라 무필터가 아니다 — 필터 검색이 나간다
    assert popular_calls == []


# ─────────── I-1 options·optionCount 적재 — 장바구니 옵션 힌트 (이슈 #455) ───────────


def _products_with_option_hints():
    """DEFAULT_RERANK 가 참조하는 101·102 에 options/optionCount 를 실은 후보 — 103 은 미수신."""
    return [
        SpringProduct(
            product_id=101,
            name="이어폰A",
            price=39000,
            rating=4.5,
            category="무선이어폰",
            brand="BrandX",
            options=["레드", "블루"],
            option_count=5,
        ),
        SpringProduct(
            product_id=102,
            name="이어폰B",
            price=48000,
            rating=4.2,
            category="무선이어폰",
            brand="BrandY",
            option_count=0,  # options 는 없지만 optionCount 만 온 케이스도 적재 대상
        ),
        SpringProduct(
            product_id=103,
            name="이어폰C",
            price=29000,
            rating=3.9,
            category="무선이어폰",
            brand="BrandZ",
        ),
    ]


async def test_push_success_loads_option_hints_for_cart() -> None:
    """(적재) 추천 push **성공** 턴에 candidates 의 옵션 힌트가 장바구니 상태에 실린다."""
    from app.agents.buyer.cart.options import OptionHint
    from app.agents.buyer.cart.state import get_cart_store

    request = _req(thread_id="option-hint-push-success")
    identity = _member()
    key = await _thread_key(request, identity)
    cart_store = await get_cart_store()

    await _collect(
        run_buyer_turn(
            request,
            identity,
            llm=FakeLLM(),
            search=_make_search(_products_with_option_hints()),
            push_fn=_RecordingPush(),
        )
    )

    assert await cart_store.get_option_hint(key, 101) == OptionHint(names=("레드", "블루"), total=5)
    assert await cart_store.get_option_hint(key, 102) == OptionHint(names=(), total=0)
    # 103 은 I-1 이 options/optionCount 를 안 실어 보냈으므로 힌트가 없다(오늘 경로로 degrade).
    assert await cart_store.get_option_hint(key, 103) is None


async def test_push_failure_does_not_load_option_hints() -> None:
    """(적재) push **실패** 턴에는 카드가 노출되지 않은 것과 대칭으로 옵션 힌트도 싣지 않는다."""
    from app.agents.buyer.cart.state import get_cart_store

    request = _req(thread_id="option-hint-push-failure")
    identity = _member()
    key = await _thread_key(request, identity)
    cart_store = await get_cart_store()

    await _collect(
        run_buyer_turn(
            request,
            identity,
            llm=FakeLLM(),
            search=_make_search(_products_with_option_hints()),
            push_fn=_failing_push,
        )
    )

    assert await cart_store.get_option_hint(key, 101) is None
    assert await cart_store.get_option_hint(key, 102) is None


# ─────── #571 ordinal_span ───────


async def test_ordinal_span_matches_list_length_for_a_single_list_push() -> None:
    """[#571-19] 목록 1개 push → `ordinal_span` == 그 목록 길이(표시 순서 = 저장 순서 증명)."""
    from app.agents.buyer.cart.state import get_cart_store

    push = _RecordingPush()
    request = _req(thread_id="t-571-single-list")
    await _collect(
        run_buyer_turn(
            request, _member(), llm=FakeLLM(), search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    entry = _only_list(push.pushes[0])
    key = await _thread_key(request, _member())
    state = await (await get_cart_store()).get_last_reco_state(key)
    assert state.ordinal_span == len(entry.product_ids)
    assert state.ordinal_span > 0


async def test_ordinal_span_is_zero_for_a_multi_list_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-19] 목록 2개 이상 push → `ordinal_span` == 0(전역 순번이 정의되지 않는다,
    §2 결정 2 — 다목록 PICK_ONE 은 화면이 섹션으로 쪼개져 "3번째"가 전역 순번인지 섹션 내
    순번인지 정의되지 않는다)."""
    from app.agents.buyer.cart.state import get_cart_store
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    monkeypatch.setattr(get_settings(), "expose_min", 1)
    monkeypatch.setattr(get_settings(), "expose_max", 2)

    async def _map(**kwargs):
        return CategoryMapping(legs=[("여행용품", "여행용 파우치"), ("전자기기", "여행용 어댑터")])

    async def _search(filters, exclude_product_ids=None):
        products = (
            [_prod(102, "여행용품", "여행용 파우치"), _prod(103, "여행용품", "압축 파우치")]
            if filters.category == "여행용품"
            else [_prod(201, "전자기기", "멀티 어댑터"), _prod(101, "전자기기", "여행용 어댑터")]
        )
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    request = _req(message="여행용 파우치랑 어댑터 추천해줘", thread_id="t-571-multi-list")
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "categoryQueries": [
                {"category": "여행용품", "query": "여행용 파우치"},
                {"category": "전자기기", "query": "여행용 어댑터"},
            ],
            "filters": {},
            "case": 3,
        }
    )
    await _collect(
        run_buyer_turn(
            request, _member_num(), llm=llm, search=_search, push_fn=push, map_categories=_map
        )
    )
    assert len(push.pushes[0].lists) >= 2
    key = await _thread_key(request, _member_num())
    state = await (await get_cart_store()).get_last_reco_state(key)
    assert state.ordinal_span == 0


async def test_ordinal_span_matches_exposed_count_for_profile_vector_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-19] 프로필 벡터 경로도 목록 1개(PICK_ONE)라 `ordinal_span` == exposed 개수."""
    from app.agents.buyer.cart.state import get_cart_store

    thread_id = "t-571-profile-vector"
    await _recommend_via_profile_and_get_named_reco(
        monkeypatch, thread_id=thread_id, pids=[301, 302, 303]
    )
    request = _req(thread_id=thread_id)
    key = await _thread_key(request, _member_num())
    state = await (await get_cart_store()).get_last_reco_state(key)
    assert state.ordinal_span == len(state.items)
    assert state.ordinal_span > 0
