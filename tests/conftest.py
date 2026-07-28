"""테스트 공통 — 인프라 전역 상태(레이트 리밋·활성 스트림 레지스트리)를 테스트마다 격리."""

from __future__ import annotations

import os
import sys

import pytest

# 로컬 통합용 .env가 단위/통합 테스트의 인증·외부 provider를 오염시키지 않게 한다.
# 단, 실 키가 필요한 smoke(마커 명시 선택)는 그대로 둔다 — 여기서 지우면 실행 불가(리뷰 반영).
if not ("smoke" in " ".join(sys.argv) and "not smoke" not in " ".join(sys.argv)):
    os.environ["AUTH_MODE"] = "dev"
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["GOOGLE_API_KEY"] = ""

from app.agents.buyer.cart.state import reset_cart_store
from app.agents.buyer.graph import reset_thread_store
from app.agents.buyer.recommendation.state import reset_revert_store
from app.agents.profile.store import reset_profile_store
from app.core.conversation import reset_store
from app.core.pg_resilience import close_advisory_pool, reset_advisory_pool
from app.core.ratelimit import reset_limiter
from app.core.stream import get_registry
from app.pipelines.artifact_store import reset_catalog_store


@pytest.fixture(autouse=True)
def _reset_infra_state():
    """각 테스트 전후로 인메모리 카운터·레지스트리를 비워 테스트 간 누수를 막는다.

    reset_catalog_store(): get_catalog_store() 싱글턴은 이제 pg-catalog 연결 풀을 여는데,
    유닛 테스트는 항상 store 를 직접 주입해 이 경로를 타지 않는다 — 혹시 실수로 호출됐을
    커넥션 풀이 다음 테스트로 새지 않게 방어적으로 리셋한다(이슈 #31).
    """
    reset_limiter()
    reset_store()
    reset_thread_store()
    reset_cart_store()
    reset_revert_store()
    reset_profile_store()
    reset_catalog_store()
    get_registry()._active.clear()
    yield
    reset_limiter()
    reset_store()
    reset_thread_store()
    reset_cart_store()
    reset_revert_store()
    reset_profile_store()
    reset_catalog_store()
    get_registry()._active.clear()


@pytest.fixture(autouse=True)
async def _reset_advisory_state():
    """advisory pool을 현재 테스트 루프에서 닫고 다음 루프용 lock으로 교체한다."""
    await close_advisory_pool()
    reset_advisory_pool()
    yield
    await close_advisory_pool()
    reset_advisory_pool()


@pytest.fixture(autouse=True)
def _pg_free_default_backend(monkeypatch):
    """[#101] hot path 기본 백엔드를 pg-free 인 Spring 위임으로 고정한다(테스트 전역).

    prod 기본은 config search_backend=embedding_rerank 라 default_backend 를 지연 생성하면
    EmbeddingRerankBackend.__init__ 이 get_catalog_store()로 pg-catalog 풀을 즉시 연다 — pg 미기동
    테스트 환경에선 연결 재시도로 hang 한다. #101 이전 기본(SpringSearchBackend)과 동일하게
    되돌려, backend 를 명시 주입하지 않는 테스트가 실 pg/임베딩을 건드리지 않게 한다.

    autouse 라 buyer_fakes(FakeBackend override)보다 먼저 세팅돼 그쪽이 이긴다. embedding 재정렬
    자체를 검증하는 테스트는 EmbeddingRerankBackend(store=...) 를 직접 주입한다.
    """
    import app.services.search_service as ss
    from app.services.search_service import SpringSearchBackend

    monkeypatch.setattr(ss, "default_backend", SpringSearchBackend())


@pytest.fixture(autouse=True)
def _fake_category_mapping(monkeypatch):
    """buyer 그래프 테스트가 라이브 map_categories(Google 임베딩·pg-catalog)를 안 타게 결정적
    fake 를 주입한다 — 추측 category(raw)를 그대로 canonical 로 echo 한다(매핑 정확도 검증은
    test_category_mapping.py 소관). map_categories 를 명시 주입하는 테스트는 이 기본값을 덮는다.
    """
    import app.agents.buyer.graph as bg

    async def _fake_map(*, category_queries, utterance, settings):
        return [(q.raw_category, q.query) for q in category_queries if q.raw_category]

    monkeypatch.setattr(bg, "_map_categories", _fake_map)


@pytest.fixture
def buyer_fakes(monkeypatch):
    """/chat 을 실 buyer 그래프 + fake LLM/검색/push 로 구동한다(라이브 의존 없이 해피패스)."""
    import app.agents.buyer.graph as bg
    import app.services.search_service as ss
    import app.services.spring_client as sc
    from tests._fakes import FakeBackend, FakeLLM, fake_push

    llm = FakeLLM()
    monkeypatch.setattr(bg, "get_llm", lambda: llm)
    monkeypatch.setattr(ss, "default_backend", FakeBackend())
    monkeypatch.setattr(sc, "push_recommendations", fake_push)
    return llm


@pytest.fixture(autouse=True)
def _no_live_recent_purchases(monkeypatch):
    """구매이력 조회 기본값을 빈 응답으로 — 단위테스트가 라이브 Spring 을 건드리지 않게.
    dedup 동작 검증 테스트는 get_recent_purchases 를 명시적으로 재패치한다.
    """
    from app.schemas.spring import RecentPurchases

    async def _empty(user_id, status=None):
        return RecentPurchases()

    monkeypatch.setattr("app.services.spring_client.get_recent_purchases", _empty)
