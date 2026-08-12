"""테스트 공통 — 인프라 전역 상태(레이트 리밋·활성 스트림 레지스트리)를 테스트마다 격리."""

from __future__ import annotations

import asyncio
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
    os.environ["INTERNAL_API_TOKEN"] = ""

from app.agents.buyer.cart.state import reset_cart_store
from app.agents.buyer.graph import reset_thread_store
from app.agents.buyer.recommendation.state import reset_repurchase_store, reset_revert_store
from app.agents.profile import graph_journal, processed_events, session_activity
from app.agents.profile import store as profile_store_module
from app.agents.profile.store import reset_profile_store
from app.agents.seller import analysis_store
from app.core import conversation as conversation_module
from app.core import pg_store
from app.core.conversation import reset_store
from app.core.pg_resilience import close_advisory_pool, reset_advisory_pool
from app.core.ratelimit import reset_limiter
from app.core.session_context import close_session_lifecycle
from app.core.stream import set_registry
from app.pipelines.artifact_store import reset_catalog_store
from app.pipelines.artifacts_batch import reset_batch_failure_state
from app.services import spring_client as _spring_client


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
    reset_repurchase_store()
    reset_profile_store()
    reset_catalog_store()
    reset_batch_failure_state()
    # 싱글턴 자체를 버린다 — 백엔드 선택이 설정에서 파생되므로(#476) dict 만 비우면
    # 이전 테스트가 고른 백엔드와 lease 토큰이 다음 테스트로 샌다.
    set_registry(None)
    yield
    reset_limiter()
    reset_store()
    reset_thread_store()
    reset_cart_store()
    reset_revert_store()
    reset_repurchase_store()
    reset_profile_store()
    reset_catalog_store()
    reset_batch_failure_state()
    # 싱글턴 자체를 버린다 — 백엔드 선택이 설정에서 파생되므로(#476) dict 만 비우면
    # 이전 테스트가 고른 백엔드와 lease 토큰이 다음 테스트로 샌다.
    set_registry(None)


@pytest.fixture(autouse=True)
async def _reset_advisory_state():
    """advisory pool을 현재 테스트 루프에서 닫고 다음 루프용 lock으로 교체한다."""
    await close_advisory_pool()
    reset_advisory_pool()
    yield
    await close_advisory_pool()
    reset_advisory_pool()


def pg_pool_tasks() -> list[asyncio.Task]:
    """지금 도는 루프에 묶인 psycopg 풀 백그라운드 태스크(워커·스케줄러).

    psycopg_pool 은 이들을 `pool-<n>-worker-<i>` · `pool-<n>-scheduler` 로 이름 붙인다.
    이 목록이 비어 있으면 이 루프의 teardown 은 #208 로 교착할 수 없다 — 닫을 것이 없다.
    """
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("pool-")]


async def close_pg_pools_on_loop() -> None:
    """이 이벤트 루프에서 열린 pg 풀을 전부 닫는다 (이슈 #208).

    pg 모듈들은 sync 리셋터에서 await 할 수 없어 풀 close 를 "다음 async 진입"으로 미룬다.
    그래서 테스트가 끝날 때 살아 있는 `AsyncConnectionPool` 이 곧 파괴될 루프에 남는데,
    psycopg_pool 워커는 유지보수 태스크 실행 중 받은 취소를 삼키므로(근거·재현은
    tests/unit/test_pool_worker_cancellation.py) 루프 teardown 의 `_cancel_all_tasks()` 가
    영원히 반환하지 않는다. 여기서 미리 닫아 그 창을 없앤다.

    모듈을 새로 추가하면 이 목록에도 배선한다 — 누락은 CI 무한 대기로 나타난다
    (tests/integration/test_pg_pool_loop_teardown.py 가 누락을 잡는다).
    """
    for close in (
        analysis_store.close_pool,
        processed_events.close_pool,
        session_activity.close_pool,
        graph_journal.close_pool,
        conversation_module.close_store,
        pg_store.close_store,
        profile_store_module.close_store,
        close_session_lifecycle,
        close_advisory_pool,
    ):
        await close()


@pytest.fixture(autouse=True)
async def _close_pg_pools_on_this_loop():
    """teardown 전용 — 살아 있는 풀을 루프가 죽기 전에 닫는다 (이슈 #208).

    setup 에서는 부르지 않는다. `_reset_infra_state` 가 이미 InMemory 스토어를 꽂아 둔
    상태라, 여기서 다시 닫으면 그 주입을 되돌려 다음 테스트가 실 pg 를 건드리게 된다.
    autouse 정의 순서상 이 fixture 는 `_reset_infra_state` **보다 먼저** teardown 되므로,
    sync 리셋터가 풀을 정리 대기열로 밀어 넣기 전에 살아 있는 풀을 직접 닫는다.

    이 루프에 풀 태스크가 없으면 아무것도 하지 않는다. `TestClient` 는 자체 portal 루프에서
    앱을 돌리므로 거기서 열린 풀은 **이미 죽은 루프**에 묶여 있다 — 여기서 close 해봐야
    실패하고 워커 코루틴만 미회수 상태로 GC 되어 `PytestUnraisableExceptionWarning` 만 남는다.
    그런 풀은 이 루프의 `_cancel_all_tasks()` 대상도 아니라 #208 을 일으키지 못한다(별개 누수).
    """
    yield
    if pg_pool_tasks():
        await close_pg_pools_on_loop()


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
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    # llm·tier 는 [#115 §4.4] 마진 트리거 택일용으로 graph 가 넘긴다. fake 가 이를 안 받으면
    # TypeError 가 graph 의 방어 except(category_map_failed)에 먹혀 **모든 leg 이 조용히 빈
    # legs 로 degrade** 한다 — 카테고리가 사라진 원인을 추적하기 어려우므로 시그니처를 맞춰둔다.
    async def _fake_map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        # #217 — 반환형은 CategoryMapping(legs, unresolved)다. `unresolved` 는 전개 트리거 입력이라
        # 여기서 비워두면 그래프 테스트가 "전개 미발동" 경로만 타는데, 그게 이 fake 의 의도다
        # (전개 트리거 검증은 test_fanout·test_needs_expansion 소관).
        return CategoryMapping(
            legs=[(q.raw_category, q.query) for q in category_queries if q.raw_category],
            unresolved=[],
        )

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


@pytest.fixture(autouse=True)
def _no_live_order_status(monkeypatch, request):
    """Non-client tests must inject I-4 explicitly rather than contact live Spring."""
    # This module's client-contract tests install MockTransport at the HTTP boundary and
    # therefore need the captured real function. Integration fixtures restore it likewise.
    if request.node.path.name == "test_order_status.py":
        return

    async def _blocked(*args, **kwargs):
        raise AssertionError("live get_order_status is disabled in tests")

    monkeypatch.setattr(_spring_client, "get_order_status", _blocked)
