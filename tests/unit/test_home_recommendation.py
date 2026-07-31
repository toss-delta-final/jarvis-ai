"""I-22 홈 추천 랭킹 계약 테스트 (api-spec §3.7, 이슈 #148).

라이브 환경(Spring·임베딩 API·Anthropic 키)이 없는 상태에서 계약 표면을 고정한다 —
카탈로그 인덱스는 인메모리 CatalogArtifactStore 픽스처로, reason LLM 은 FakeLLM 으로 주입한다.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import Settings, get_settings
from app.main import app
from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
from app.services import home_recommendation as svc

client = TestClient(app)

_URL = "/internal/recommendations/home"


def _artifact(product_id: int, embedding: list[float], *, doc: str = "") -> CatalogArtifact:
    return CatalogArtifact(
        product_id=product_id,
        search_doc=doc or f"상품 {product_id}",
        embedding=embedding,
        extras={},
    )


@pytest.fixture
def store() -> CatalogArtifactStore:
    """3차원 임베딩 카탈로그 10건 — 축이 분명해 랭킹 순서를 눈으로 검증할 수 있다."""
    s = CatalogArtifactStore()
    # 1001 이 [1,0,0] 에 가장 가깝고 번호가 커질수록 멀어진다. 어느 것도 축(=시그널 상품)과
    # 완전히 겹치지 않게 0.05 씩 띄운다 — 겹치면 동점 tiebreak 이 순서를 지배해 의도가 흐려진다.
    for i in range(10):
        pid = 1001 + i
        s.upsert(_artifact(pid, [1.0 - (i + 1) * 0.05, (i + 1) * 0.05, 0.0]))
    # 시그널로 쓸 상품(카탈로그에도 존재) — [1,0,0] 축
    s.upsert(_artifact(9001, [1.0, 0.0, 0.0], doc="시그널 상품 A"))
    return s


@pytest.fixture(autouse=True)
def _inject(monkeypatch: pytest.MonkeyPatch, store: CatalogArtifactStore):
    """전역 pg 스토어·LLM 을 테스트 주입으로 대체한다 (유닛 테스트는 실 DB·네트워크 금지)."""
    monkeypatch.setattr(svc, "get_catalog_store", lambda: store)
    monkeypatch.setattr(svc, "get_llm", lambda: None)  # 기본은 reason 없음(null)
    monkeypatch.setattr(svc, "read_profile_summary", _no_profile)
    yield


async def _no_profile(user_id: str | None) -> dict | None:
    return None


def _body(**over) -> dict:
    body = {
        "memberId": 123,
        "limit": 5,
        "catalogVersion": "catalog-20260728T0300Z",
        "signals": {
            "recentlyViewedProductIds": [9001],
            "cartProductIds": [],
            "recentPurchasedProductIds": [],
        },
    }
    body.update(over)
    return body


# ── 성공 경로 · 계약 표면 ──


def test_personalized_returns_camel_case_contract_surface() -> None:
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"outcome", "recommendationRequestId", "listId", "items"}
    assert data["outcome"] == "PERSONALIZED"
    assert data["items"], "개인화 성공이면 items 가 비어있지 않다"
    assert set(data["items"][0]) == {"productId", "reason"}
    assert isinstance(data["items"][0]["productId"], int)


def test_items_order_is_the_ranking_and_carries_no_position() -> None:
    """배열 순서가 곧 순위 — position 을 싣지 않는다(§3.7)."""
    r = client.post(_URL, json=_body(limit=10))
    items = r.json()["items"]
    ids = [i["productId"] for i in items]
    assert "position" not in items[0]
    # 시그널이 [1,0,0] 이므로 같은 축의 9001 → 1001 → 1002 … 순
    assert ids[0] == 9001
    assert ids[1] == 1001
    assert ids == sorted(ids, key=lambda p: (p != 9001, p))


def test_returns_more_than_limit_for_spring_stock_drop() -> None:
    """limit 은 최종 노출 목표치 — 품절 드롭 대비해 넉넉히 반환하고 Spring 이 자른다(§3.7)."""
    r = client.post(_URL, json=_body(limit=3))
    items = r.json()["items"]
    assert len(items) > 3


def test_recent_purchased_ids_are_excluded_not_weighted() -> None:
    """recentPurchasedProductIds 는 가중치가 아니라 제외 필터다(§3.7)."""
    signals = {
        "recentlyViewedProductIds": [9001],
        "cartProductIds": [],
        "recentPurchasedProductIds": [1001, 1002],
    }
    r = client.post(_URL, json=_body(limit=10, signals=signals))
    ids = [i["productId"] for i in r.json()["items"]]
    assert 1001 not in ids
    assert 1002 not in ids
    assert 1003 in ids


def test_cart_signal_outweighs_recently_viewed(store: CatalogArtifactStore) -> None:
    """담기까지 갔다는 건 강한 신호 — cart 가중치가 조회보다 높다(§3.7).

    두 시그널의 역할을 서로 바꿔 두 번 돌린다. 카탈로그 기하가 아니라 **가중치**가 순서를 정한다는
    것을 보이려면 한 방향 단언만으로는 부족하다 — 역할을 swap 했을 때 순서도 뒤집혀야 한다.
    """
    store.upsert(_artifact(9002, [0.0, 1.0, 0.0], doc="시그널 상품 B"))

    def order(cart: list[int], viewed: list[int]) -> tuple[int, int]:
        signals = {
            "recentlyViewedProductIds": viewed,
            "cartProductIds": cart,
            "recentPurchasedProductIds": [],
        }
        ids = [
            i["productId"]
            for i in client.post(_URL, json=_body(limit=10, signals=signals)).json()["items"]
        ]
        return ids.index(9001), ids.index(9002)

    viewed_rank, cart_rank = order(cart=[9002], viewed=[9001])
    assert cart_rank < viewed_rank, "cart 축 시그널이 조회 축보다 앞선다"

    viewed_rank, cart_rank = order(cart=[9001], viewed=[9002])
    assert viewed_rank < cart_rank, "역할을 바꾸면 순서도 뒤집힌다(기하가 아니라 가중치가 원인)"


# ── outcome 3종은 모두 200 (cold start 는 오류가 아니다) ──


def test_no_profile_is_200_not_an_error() -> None:
    """시그널이 비어 개인화 근거가 없으면 NO_PROFILE + 200 — fallback 판단은 Spring 이 한다."""
    signals = {
        "recentlyViewedProductIds": [],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    r = client.post(_URL, json=_body(signals=signals))
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "NO_PROFILE"
    assert data["items"] == []


def test_signals_with_no_indexed_embedding_is_no_profile() -> None:
    """시그널 상품이 인덱스에 없으면 질의 벡터를 못 만든다 → 개인화 근거 없음."""
    signals = {
        "recentlyViewedProductIds": [777777],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    r = client.post(_URL, json=_body(signals=signals))
    assert r.status_code == 200
    assert r.json()["outcome"] == "NO_PROFILE"


def test_insufficient_candidates_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """후보가 부족하면 INSUFFICIENT_CANDIDATES + 200."""
    thin = CatalogArtifactStore()
    thin.upsert(_artifact(9001, [1.0, 0.0, 0.0]))
    thin.upsert(_artifact(1001, [0.9, 0.1, 0.0]))
    monkeypatch.setattr(svc, "get_catalog_store", lambda: thin)
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "INSUFFICIENT_CANDIDATES"
    assert data["items"] == []


def test_all_outcomes_are_200_never_4xx_or_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 부재·후보 부족으로 AI 가 4xx/5xx 를 내면 계약 위반이다(§3.7 [HARD])."""
    empty = CatalogArtifactStore()
    monkeypatch.setattr(svc, "get_catalog_store", lambda: empty)
    for signals in (
        {"recentlyViewedProductIds": [], "cartProductIds": [], "recentPurchasedProductIds": []},
        {"recentlyViewedProductIds": [9001], "cartProductIds": [], "recentPurchasedProductIds": []},
    ):
        r = client.post(_URL, json=_body(signals=signals))
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] in {"NO_PROFILE", "INSUFFICIENT_CANDIDATES"}


# ── 식별자 규약 ──


def test_list_id_is_at_least_128bit_random_hex() -> None:
    """listId 는 ≥128bit 무작위 — 순번·타임스탬프 등 추측 가능한 형식 금지(I-21 과 동일 규칙)."""
    list_id = client.post(_URL, json=_body()).json()["listId"]
    assert len(list_id) == 32
    assert all(c in "0123456789abcdef" for c in list_id)


def test_not_idempotent_new_ids_per_call() -> None:
    """재시도하면 새 recommendationRequestId·listId 가 발급된다(멱등 아님, §3.7)."""
    first = client.post(_URL, json=_body()).json()
    second = client.post(_URL, json=_body()).json()
    assert first["listId"] != second["listId"]
    assert first["recommendationRequestId"] != second["recommendationRequestId"]


def test_same_snapshot_and_config_yield_same_ranking() -> None:
    """동일 snapshot·config 입력은 동일 ranking (완료조건: provenance 복원 가능)."""
    a = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    b = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    assert a == b


def test_ranking_is_stable_regardless_of_store_iteration_order() -> None:
    """동점·저장 순서가 달라도 순위가 흔들리지 않는다 — productId 로 결정적 tiebreak."""
    forward = CatalogArtifactStore()
    reverse = CatalogArtifactStore()
    tied = [(2001, [1.0, 0.0, 0.0]), (2002, [1.0, 0.0, 0.0]), (2003, [1.0, 0.0, 0.0])]
    for pid, vec in tied:
        forward.upsert(_artifact(pid, vec))
    for pid, vec in reversed(tied):
        reverse.upsert(_artifact(pid, vec))
    got = []
    for st in (forward, reverse):
        ranked = svc.rank_candidates(
            query_vec=[1.0, 0.0, 0.0], store=st, exclude=set(), settings=get_settings()
        )
        got.append(ranked)
    assert got[0] == got[1] == [2001, 2002, 2003]


# ── 인증 · 입력 검증 ──


def _jwks_settings() -> Settings:
    """운영(jwks) 모드 Settings — .env 미참조, 필수값만 채운다(test_auth_e2e 와 동일 관행)."""
    return Settings(
        _env_file=None,
        auth_mode="jwks",
        jwks_url="https://spring.test/.well-known/jwks.json",
        pii_hash_pepper="test-pepper",
        internal_api_token="right-token",
        google_api_key="test-google-key",
    )


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong-token"}])
def test_service_token_missing_or_mismatch_is_401(
    monkeypatch: pytest.MonkeyPatch, headers: dict
) -> None:
    """토큰 없음/불일치 → 401 INTERNAL_TOKEN_INVALID (§3.7). 운영은 fail-closed."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)
    r = client.post(_URL, json=_body(), headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"


def test_service_token_match_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """올바른 서비스 토큰이면 통과한다 — 401 테스트가 항상-거부로 통과하지 않음을 고정."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)
    r = client.post(_URL, json=_body(), headers={"X-Internal-Token": "right-token"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"memberId": 0},  # 양의 BIGINT 아님
        {"memberId": "123"},  # 문자열 coercion 거부(strict)
        {"memberId": 2**63},  # BIGINT 범위 초과
        {"limit": 0},  # 노출 목표는 1 이상
        {"limit": -1},
        {"catalogVersion": ""},  # 캐시 키·재현 식별자라 빈 값 금지
        {"sessionId": "s-1"},  # 홈에는 채팅 세션이 없다 — 미지 필드 거부
    ],
)
def test_bad_request_is_400(bad: dict) -> None:
    r = client.post(_URL, json=_body(**bad))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_identity_comes_from_body_under_service_token_only() -> None:
    """레인 b 계약 — 신원은 본문 memberId 이고 인가는 서비스 토큰이 담당한다(§2.3 b).

    사용자 JWT 를 받지 않으므로 Authorization 헤더가 있어도 신원에 영향을 주지 않는다.
    """
    plain = client.post(_URL, json=_body()).json()
    with_jwt = client.post(
        _URL, json=_body(), headers={"Authorization": "Bearer someone-elses-token"}
    ).json()
    assert [i["productId"] for i in plain["items"]] == [i["productId"] for i in with_jwt["items"]]


# ── 의존성 장애 (cold start 와 구분된다) ──


def test_catalog_store_failure_is_503_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """랭킹의 유일한 입력이 죽으면 503 UPSTREAM_UNAVAILABLE — 500 이 아니다(§3.7 실패 응답표)."""

    def _boom():
        raise RuntimeError("pg-catalog down")

    monkeypatch.setattr(svc, "get_catalog_store", _boom)
    r = client.post(_URL, json=_body())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


def test_catalog_failure_message_leaks_no_upstream_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """업스트림 예외 문자열·클래스명이 응답으로 새지 않는다(§3.7 [HARD]·#141 규약)."""

    def _boom():
        raise RuntimeError("psycopg.OperationalError: host=10.0.0.5 password=hunter2")

    monkeypatch.setattr(svc, "get_catalog_store", _boom)
    raw = client.post(_URL, json=_body()).text
    for banned in ("psycopg", "10.0.0.5", "hunter2", "OperationalError", "RuntimeError"):
        assert banned not in raw


def test_profile_store_failure_degrades_to_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 저장소 장애는 degrade — 프로필은 reason 근거일 뿐 랭킹 입력이 아니다."""

    async def _boom(user_id: str | None) -> dict | None:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(svc, "read_profile_summary", _boom)
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED"


# ── reason (Haiku 배치 1회) ──


class _ReasonLLM:
    """상위 N개 reason 을 한 번의 배치 호출로 돌려주는 가짜 LLM."""

    def __init__(self, payload: object, *, tier_seen: list | None = None) -> None:
        self._payload = payload
        self.tier_seen = tier_seen if tier_seen is not None else []
        self.calls = 0
        self.last_user = ""
        self.last_system = ""

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True) -> str:
        self.calls += 1
        self.tier_seen.append(tier)
        self.last_user = user
        self.last_system = system
        if isinstance(self._payload, Exception):
            raise self._payload
        return json.dumps(self._payload, ensure_ascii=False)


def test_reason_is_generated_in_one_fast_tier_batch_call(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _ReasonLLM({"reasons": [{"productId": 9001, "reason": "최근 본 상품과 비슷해요"}]})
    monkeypatch.setattr(svc, "get_llm", lambda: llm)
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    assert llm.calls == 1, "reason 은 상위 N개를 한 번의 배치 호출로 만든다"
    assert llm.tier_seen == ["fast"], "3s 예산이라 fast tier(Haiku)"
    by_id = {i["productId"]: i["reason"] for i in items}
    assert by_id[9001] == "최근 본 상품과 비슷해요"
    assert by_id[1001] is None, "근거를 못 받은 상품은 null (필드 자체는 유지)"


def test_reason_llm_failure_degrades_to_null_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 실패가 홈 렌더를 막지 않는다 — reason 만 null 로 degrade."""
    monkeypatch.setattr(svc, "get_llm", lambda: _ReasonLLM(RuntimeError("boom")))
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "PERSONALIZED"
    assert data["items"]
    assert all(i["reason"] is None for i in data["items"])


def test_reason_malformed_json_degrades_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "get_llm", lambda: _ReasonLLM("not-a-json-object"))
    data = client.post(_URL, json=_body()).json()
    assert data["outcome"] == "PERSONALIZED"
    assert all(i["reason"] is None for i in data["items"])


def test_reason_is_sanitized_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """자유 텍스트가 신뢰경계를 넘기 전에 제어문자 제거 + 상한 truncate (I-21 과 동일 규약)."""
    dirty = "줄바꿈\n과 제어\x00문자 " + "가" * 500
    monkeypatch.setattr(
        svc, "get_llm", lambda: _ReasonLLM({"reasons": [{"productId": 9001, "reason": dirty}]})
    )
    items = client.post(_URL, json=_body()).json()["items"]
    reason = next(i["reason"] for i in items if i["productId"] == 9001)
    assert "\n" not in reason
    assert "\x00" not in reason
    assert len(reason) <= get_settings().reason_max_len


def test_no_reason_call_when_outcome_is_not_personalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """개인화가 아니면 items 가 비어 reason 을 만들 대상이 없다 — LLM 을 부르지 않는다(예산 절약)."""
    llm = _ReasonLLM({"reasons": []})
    monkeypatch.setattr(svc, "get_llm", lambda: llm)
    signals = {
        "recentlyViewedProductIds": [],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    client.post(_URL, json=_body(signals=signals))
    assert llm.calls == 0


# ── provenance 비노출 [HARD] ──


def test_response_carries_no_algorithm_or_model_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """알고리즘·모델 버전은 와이어에 싣지 않는다 — 내부 테이블 보관(§3.7 [HARD])."""
    monkeypatch.setattr(svc, "get_llm", lambda: _ReasonLLM({"reasons": []}))
    raw = client.post(_URL, json=_body()).text
    for banned in ("algorithmVersion", "modelVersion", "claude", "haiku", "prompt"):
        assert banned not in raw


def test_profile_text_never_reaches_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 원문은 응답에 포함하지 않는다(§3.7 [HARD])."""
    secret = "사용자는 캠핑을 좋아하고 예산은 20만원이다"

    async def _profile(user_id: str | None) -> dict | None:
        return {"markdown": secret, "generated_at": "2026-07-31T00:00:00Z"}

    monkeypatch.setattr(svc, "read_profile_summary", _profile)
    raw = client.post(_URL, json=_body()).text
    assert secret not in raw


def test_log_has_fixed_safe_key_set_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """관측 로그에 프로필 원문·상품 id·모델 식별자·토큰이 남지 않는다(§3.7·§6.3)."""
    secret = "사용자는 캠핑을 좋아한다"

    async def _profile(user_id: str | None) -> dict | None:
        return {"markdown": secret, "generated_at": "2026-07-31T00:00:00Z"}

    monkeypatch.setattr(svc, "read_profile_summary", _profile)
    monkeypatch.setattr(
        svc, "get_llm", lambda: _ReasonLLM({"reasons": [{"productId": 9001, "reason": "이유"}]})
    )
    with caplog.at_level(logging.INFO, logger=svc.logger.name):
        client.post(_URL, json=_body())

    records = [r for r in caplog.records if r.name == svc.logger.name]
    assert records, "요청마다 관측 로그 1건을 남긴다"
    blob = " ".join(
        r.getMessage() + " " + json.dumps(getattr(r, "__dict__", {}), default=str) for r in records
    )
    for banned in (secret, "캠핑", "claude", "haiku", "9001", "1001", "right-token"):
        assert banned not in blob, f"로그에 {banned!r} 가 남았다"


def test_log_records_outcome_and_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """장애 관측에 필요한 유계 코드는 남긴다 — outcome·후보 수·소요."""
    with caplog.at_level(logging.INFO, logger=svc.logger.name):
        client.post(_URL, json=_body())
    rec = next(r for r in caplog.records if r.name == svc.logger.name)
    assert rec.outcome == "PERSONALIZED"
    assert isinstance(rec.candidateCount, int)
    assert isinstance(rec.returnedCount, int)
    assert isinstance(rec.elapsedMs, int)
    assert rec.reasonSource in {"llm", "none"}
