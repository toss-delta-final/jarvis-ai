"""구매자 추천 그래프 (이슈 #2) — 파이프라인·degrade·fallback·멀티턴·경로 B 회귀.

run_buyer_turn 을 fake LLM/검색/push 로 직접 구동한다(라이브 Anthropic·Spring 불필요).
SSE 는 상품 카드를 싣지 않는다(경로 B) — products.ready 는 {sessionId, listId} 만.
"""

from __future__ import annotations

import asyncio
import gc
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.agents.buyer.graph import get_thread_store, run_buyer_turn
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.schemas.spring import ProductSearchResult, SpringProduct
from app.services.spring_client import SpringUnavailableError
from tests._fakes import DEFAULT_DECOMPOSE, DEFAULT_PRODUCTS, FakeLLM


def _req(message: str = "무선 이어폰 추천해줘", session_id: str = "s1", thread_id: str = "t1"):
    return SimpleNamespace(session_id=session_id, thread_id=thread_id, message=message)


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject=None)


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


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


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
    assert push.pushes[0].product_ids[:2] == [101, 102]
    assert set(push.pushes[0].product_ids) <= {101, 102, 103}

    # reasons — rerank rationale 있는 상품만(101,102). expose_min 보충 103 은 근거 없어 제외(이슈 #61).
    reasons = {r.product_id: r.reason for r in push.pushes[0].reasons}
    assert reasons == {101: "가성비가 좋아요", 102: "음질이 우수해요"}

    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["finishReason"] == "stop"


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
    assert set(ready.keys()) == {"sessionId", "listId"}
    assert ready["listId"]
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
    assert push.pushes[0].product_ids == [101, 102, 103]
    # degrade 경로엔 rerank rationale 이 없으므로 reasons 는 빈 배열(계약상 선택 필드, 이슈 #61).
    assert push.pushes[0].reasons == []


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
    ids = push.pushes[0].product_ids
    assert 999 not in ids  # 후보 외 id 제거(REQ-REC-081)
    assert ids[0] == 101  # rerank 유효 산출이 선두, 나머지는 expose_min 보충


async def test_rerank_sends_rating_review_as_tiers_not_numbers() -> None:
    """[#171 PR#172] rerank LLM 입력의 rating·reviewCount 는 정확한 숫자가 아니라 등급이다.

    정확한 4.2·10 을 LLM 에 주면 근거문에 "4.2 평점"·"리뷰 10개"처럼 흘려 CH-5 표시값과 어긋날 수
    있어, 등급(ratingLevel/reviewLevel)만 준다 → 흘릴 숫자 자체가 없다(유출 원천 차단). review_count
    ==0(리뷰 없음)은 '평가없음'으로 #171 rating=0 판별을 유지한다. 정확한 값은 원본에 남아 코드
    필터·예산이 쓴다(질의 "평점 4.5 이상"은 search_catalog 사후필터 소관, 이 티어화 무영향).
    """
    import json as _json

    from app.agents.buyer.recommendation.rerank import rerank
    from app.schemas.spring import SpringProduct

    llm = FakeLLM(rerank={"ranked": [{"productId": 1, "rationale": "ok"}], "overallComment": "c"})
    candidates = [
        SpringProduct(
            product_id=1, name="무리뷰", rating=0.0, review_count=0, category="c", brand="b"
        ),
        SpringProduct(
            product_id=2, name="리뷰있음", rating=4.2, review_count=10, category="c", brand="b"
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
    assert by_id[2]["ratingLevel"] == "높음"  # 4.2 → 높음
    assert by_id[2]["reviewLevel"] == "보통"  # 10 → 보통
    assert "rating" not in by_id[1] and "reviewCount" not in by_id[1]
    # 정확한 숫자(4.2·10)는 LLM 프롬프트에 등장하지 않는다(흘릴 값 없음).
    assert "4.2" not in user


def test_rerank_system_prompt_forbids_quoting_price() -> None:
    """[#171 PR#172] rerank 프롬프트가 금액(price) 숫자 인용 금지 규칙을 유지한다.

    rating·reviewCount 는 등급으로만 넘겨 흘릴 숫자가 없지만, price 는 예산 판단용으로 정밀값을
    주므로 근거문 금액 인용을 프롬프트로 막는다(코드 backstop 과 이중화). 프롬프트 드리프트 방지.
    """
    from app.agents.buyer.recommendation.rerank import _SYSTEM

    assert "인용하지 마세요" in _SYSTEM
    assert "price" in _SYSTEM and "ratingLevel" in _SYSTEM and "reviewLevel" in _SYSTEM


def test_rerank_prompt_lists_all_tier_return_values() -> None:
    """[#171 PR#172 리뷰⑦] 프롬프트 enum 이 실제 티어 반환값을 모두 포함한다.

    _review_tier 는 review_count is None(BE 미전송)에 '정보없음'을 반환하는데, 이 값이 프롬프트
    reviewLevel 목록에 없으면 LLM 이 예고 못 받은 값을 만나 임의 해석·근거 날조할 수 있다. 실제
    반환값 집합과 프롬프트 enum 을 일치시켜 드리프트를 막는다(rating 은 이미 일치).
    """
    from app.agents.buyer.recommendation.rerank import _SYSTEM, _rating_tier, _review_tier
    from app.schemas.spring import SpringProduct

    def _p(**kw) -> SpringProduct:
        return SpringProduct(product_id=1, name="x", **kw)

    rating_vals = {
        _rating_tier(_p(rating=r, review_count=rc))
        for r, rc in [(None, 5), (0.0, 0), (0.0, 5), (3.5, 5), (4.2, 5), (4.8, 5)]
    }
    review_vals = {_review_tier(_p(review_count=rc)) for rc in [None, 0, 3, 10, 50, 200]}
    for v in rating_vals | review_vals:
        assert v in _SYSTEM, f"티어값 {v!r} 이 프롬프트 enum 에 없음"


def test_redact_leaked_price_only_flags_currency_context() -> None:
    """[#171 PR#172] 코드 가드는 price(정밀값) 유출만 backstop 으로 잡는다.

    rating·reviewCount 는 rerank 입력에서 등급으로 바꿔 LLM 에 숫자를 안 주므로 가드 대상이 아니다
    (유출 원천 차단). price 만 예산용으로 정밀값을 주므로, 값 뒤 '원' 인용(0원 무료 포함)만 통째
    제거하고 스펙 숫자(39000mAh 등 통화 단위 없음)는 보존한다.
    """
    from app.agents.buyer.recommendation.graph import _redact_leaked_number
    from app.schemas.spring import SpringProduct

    prods = [SpringProduct(product_id=1, name="x", price=29900, rating=4.8, review_count=128)]
    # price + '원' 맥락 → 문장 통째 제거(콤마 무시)
    assert _redact_leaked_number("29,900원으로 합리적이에요", prods) == ""
    # 통화 단위 없는 스펙/정상 숫자는 보존 — 티어화라 rating·reviewCount 는 아예 대상 아님
    assert (
        _redact_leaked_number("29900mAh 대용량 배터리예요", prods) == "29900mAh 대용량 배터리예요"
    )
    assert _redact_leaked_number("평점이 높고 리뷰도 많아요", prods) == "평점이 높고 리뷰도 많아요"
    assert (
        _redact_leaked_number("여름에 시원한 린넨 소재예요", prods) == "여름에 시원한 린넨 소재예요"
    )

    # 값 0(무료 이벤트)도 대상 — is not None. '0원' 인용 차단, 숫자 없는 정성 표현은 통과.
    zero = [SpringProduct(product_id=2, name="z", price=0)]
    assert _redact_leaked_number("0원 무료 이벤트예요", zero) == ""
    assert _redact_leaked_number("무료로 받을 수 있어요", zero) == "무료로 받을 수 있어요"

    # price=None 후보로는 아무 것도 안 지운다
    none_price = [SpringProduct(product_id=3, name="w", price=None)]
    assert _redact_leaked_number("29,900원이라 저렴", none_price) == "29,900원이라 저렴"


async def test_push_drops_rationale_that_leaks_nondisplay_number() -> None:
    """[#171 PR#172 리뷰] LLM 이 근거문에 비표시 수치를 인용해도 push 전 코드로 제거된다.

    프롬프트 가드를 어긴 응답 시뮬레이션 — 101 근거문은 자신의 price(39000)를 인용해 통째
    드롭(근거 없이 상품만 노출)되고, 102 의 정상 근거문은 보존된다.
    """
    push = _RecordingPush()
    llm = FakeLLM(
        rerank={
            "ranked": [
                {"productId": 101, "rationale": "39,000원이라 가성비 최고예요"},  # price 유출
                {"productId": 102, "rationale": "음질이 우수해요"},  # 정상
            ],
            "overallComment": "요청 조건에 맞는 추천이에요",
        }
    )
    await _collect(
        run_buyer_turn(
            _req(), _member(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    reasons = {r.product_id: r.reason for r in push.pushes[0].reasons}
    assert 101 not in reasons  # price 유출 근거문 → 드롭
    assert reasons.get(102) == "음질이 우수해요"  # 정상 근거문 보존
    assert 101 in push.pushes[0].product_ids  # 근거만 빠지고 상품 자체 노출은 유지


async def test_overall_comment_that_leaks_price_is_dropped() -> None:
    """[#171 PR#172 리뷰] overallComment 가 price 를 금액 맥락으로 인용하면 token 으로 안 나간다."""
    push = _RecordingPush()
    llm = FakeLLM(
        rerank={
            "ranked": [{"productId": 101, "rationale": "가벼워요"}],
            "overallComment": "39,000원짜리 좋은 상품이에요",  # 101 price=39000 유출
        }
    )
    events = await _collect(
        run_buyer_turn(
            _req(), _member(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    texts = [e["data"].get("text", "") for e in events if e["type"] == "token"]
    assert all("39000" not in t and "39,000" not in t for t in texts)  # 유출 comment 드롭 → 미emit


async def test_push_leak_check_runs_before_truncation() -> None:
    """[#171 PR#172 리뷰④] 유출 검사는 truncate 이전 원문에서 수행한다.

    reason_max_len(200) 경계에서 단위 문자('원')가 잘려나가면 '숫자+단위' 패턴이 안 걸려
    정확한 가격이 새는 우회를 막는다 — 잘리면 단위가 사라져도, 온전한 문장에서 먼저 유출로
    판정해 근거문 전체를 버린다.
    """
    push = _RecordingPush()
    # reason_max_len=200 → _sanitize_reason 은 [:199] 로 자른다. 필러 194자 + "39000" 이 인덱스
    # 194~198 을 채우고 '원'은 199 에 놓여, truncate 를 먼저 하면 정확히 '원'이 잘려나간다.
    leaky = ("가" * 194) + "39000원이라 합리적이에요"  # 101 price=39000 유출, 경계에 단위
    llm = FakeLLM(
        rerank={
            "ranked": [{"productId": 101, "rationale": leaky}],
            "overallComment": "추천이에요",
        }
    )
    await _collect(
        run_buyer_turn(
            _req(), _member(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
        )
    )
    reasons = {r.product_id: r.reason for r in push.pushes[0].reasons}
    # 101 price=39000 유출 → 온전한 문장에서 검사되어 근거문 전체 드롭(잘린 '39000'이 안 남는다).
    assert 101 not in reasons
    assert all("39000" not in (r.reason or "") for r in push.pushes[0].reasons)


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
    reason_by_id = {r.product_id: r.reason for r in push.pushes[0].reasons}
    sent = reason_by_id[101]
    assert "\n" not in sent and "\t" not in sent  # 개행/제어문자 제거
    assert len(sent) <= settings.reason_max_len  # 안전 상한 이내


async def test_overall_comment_sanitized_without_reason_length_cap() -> None:
    """overall_comment 는 SSE 직전 위험 문자만 제거하고 reason 전용 길이 캡은 적용하지 않는다."""
    settings = get_settings()
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


async def test_recommendation_boundaries_apply_unicode_sequence_policy() -> None:
    """Spring reason과 SSE 총평이 같은 등록 보존·비정상 제거 정책을 따른다."""
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

    reason_by_id = {reason.product_id: reason.reason for reason in push.pushes[0].reasons}
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
    assert chips[1]["value"] == ["정상 브랜드"]


async def test_multiturn_filters_persisted_and_fed_back() -> None:
    """1턴 병합 필터가 스레드 스토어(신원 스코프)에 저장되고 2턴 decompose 로 다시 주입된다."""
    llm = FakeLLM()
    ident = _member()
    await _collect(
        run_buyer_turn(
            _req(), ident, llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=_RecordingPush()
        )
    )

    key = conversation_key("u1", "t1")
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


async def test_thread_store_scoped_by_identity() -> None:
    """서로 다른 신원이 같은 threadId 를 써도 필터가 섞이지 않는다(IDOR 방지)."""
    a = Identity(user_id="A", is_guest=False, seller_id=None, subject="A")
    await _collect(
        run_buyer_turn(
            _req(thread_id="shared"),
            a,
            llm=FakeLLM(),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )
    thread_store = await get_thread_store()
    assert await thread_store.get(conversation_key("A", "shared")) is not None
    assert await thread_store.get(conversation_key("B", "shared")) is None


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
    rec = next((r for r in caplog.records if r.msg == "recommend_pipeline"), None)
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
    monkeypatch.setattr(sc, "_client", lambda: _FakeClient(payload))
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
    monkeypatch.setattr(sc, "_client", lambda: _FakeClient(payload))
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
    monkeypatch.setattr(sc, "_client", lambda: _FakeClient(payload))
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
    monkeypatch.setattr(sc, "_client", lambda: _FakeClient(payload))
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
    monkeypatch.setattr(sc, "_client", lambda: _FakeClient(payload))
    res = await sc.search_products(ProductSearchFilters())
    assert [p.product_id for p in res.products] == [7]


async def test_search_products_missing_data_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[PR#127 리뷰] data 키가 없는 envelope drift 는 조용한 0 이 아니라 SEARCH_FAILED 로 degrade(§7)."""
    import app.services.spring_client as sc
    from app.schemas.spring import ProductSearchFilters

    monkeypatch.setattr(sc, "_client", lambda: _FakeClient({"success": True}))
    with caplog.at_level("WARNING"):
        with pytest.raises(SpringUnavailableError):
            await sc.search_products(ProductSearchFilters())
    assert "data 키" in caplog.text


async def test_expose_min_fill_from_search_order() -> None:
    """rerank 가 expose_min 미만을 내면 검색순서로 보충한다(REQ-REC-021 5~8개)."""
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
    ids = push.pushes[0].product_ids
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
    assert 101 not in push.pushes[0].product_ids  # 최근 구매 101 제외
    assert 102 in push.pushes[0].product_ids


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
    assert 101 in push.pushes[0].product_ids  # 제외 안 됨


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
    assert 101 in push.pushes[0].product_ids  # dedup 없이 진행
    assert _types(events)[-1] == "done"


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
    assert 101 in push.pushes[0].product_ids  # dedup 스킵


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
    monkeypatch.setattr(_sc_mod, "_client", lambda: _FakeClient(body))
    res = await _REAL_GET_RECENT(123)
    assert res.purchased_product_ids() == {552, 88}


async def test_get_recent_purchases_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """스키마 불일치(필수 productId 결측)는 SpringUnavailableError 로(호출측 degrade)."""
    body = {
        "success": True,
        "data": {"orders": [{"orderId": 1, "orderedAt": "x", "items": [{"orderItemId": 1}]}]},
    }
    monkeypatch.setattr(_sc_mod, "_client", lambda: _FakeClient(body))
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
    assert 101 in push.pushes[0].product_ids  # dedup 미적용


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
    assert 201 not in push.pushes[0].product_ids  # 조미료 억제
    assert 202 in push.pushes[0].product_ids
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
    assert 201 in push.pushes[0].product_ids
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
    assert 202 not in push.pushes[0].product_ids  # exact 제외(구매한 productId)
    assert 201 in push.pushes[0].product_ids  # 조미료지만 구매 안 함 → 유지
    assert "suggestions" not in _types(events)  # 억제 카테고리 없음 → 칩 없음


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
    assert 201 in push.pushes[0].product_ids
    assert "suggestions" not in _types(events)


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
    assert 201 not in push1.pushes[0].product_ids
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
    assert 201 in push2.pushes[0].product_ids  # 조미료 복원
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
    assert 201 in push.pushes[0].product_ids
    assert "suggestions" not in _types(events)


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
    from app.core.conversation import conversation_key

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
    assert await revert_store.get(conversation_key("123", "tN")) == set()


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
