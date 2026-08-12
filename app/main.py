"""FastAPI 애플리케이션 엔트리포인트.

CORS 미들웨어(오리진은 설정 주입), MVP 라우터(chat/seller), GET /health 를 구성한다.
FE 가 AI 서버를 다른 오리진에서 직접 호출하므로 CORS 가 앞단으로 이동했다 (api-spec §2.7 / C-11).

[변경 2026-07-15] MVP 표면은 /chat, /seller/chat,
  /events/session-end, /health 로 확정. catalog/order 이벤트는 영구 미채택.
  - [완료] §2.9 스트림 수명주기(app/core/stream.py)·§2.8 레이트 리밋(app/core/ratelimit.py)·§2.5 오류 봉투(app/core/errors.py).

[추가 2026-07-20/23, 이슈 #31/#79] lifespan 에서 I-17 증분 pull과 프로필 inactivity
스케줄러(app/pipelines/scheduler.py)를
기동/종료한다 — TestClient(app) 를 `with` 없이 쓰는 기존 테스트들은 lifespan 이 발동하지 않아
영향이 없다(경험적으로 확인).

[추가 2026-08-06, 이슈 #401] lifespan 에서 categories 0행/0임베딩 가드
(app/pipelines/category_seed.py check_category_dictionary)를 검사한다. 기본값(log)·off 는
DB 연결 실패로 기동을 막지 않는다. category_dictionary_startup_check="fail" 로 강한 검증을
opt-in 하면 사전 상태를 확인 못하는 모든 경우(도달 불가 포함)에 기동을 거부한다(라운드 7 F8).

[추가 2026-08-08, 이슈 #437] lifespan 에서 모델 단가표 상태(app/core/model_pricing.py
log_model_price_table_status)를 1회 로그한다 — I/O 가 없어 항상 실행되고, category
dictionary 점검이 실패해도 이 신호는 남도록 그 앞에 둔다.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# [이슈 #33] Windows 기본 ProactorEventLoop 는 psycopg async 연결을 지원하지 않아
# AsyncPostgresStore/AsyncPostgresSaver(pg-profile, seller history.py·hitl.py 포함)가
# 조용히 실패 → dev 폴백(InMemoryStore/Saver)으로 전락한다(docs/lessons.md 2026-07-20).
# uvicorn 이 이벤트 루프를 만들기 전에(모듈 임포트 시점) 정책을 교체해야 한다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.agents.profile.graph_journal import close_pool as close_graph_journal_pool
from app.agents.profile.graph_journal import warm_pool as warm_graph_journal_pool
from app.agents.profile.processed_events import close_pool as close_processed_events_pool
from app.agents.profile.session_activity import close_pool as close_session_activity_pool
from app.agents.profile.store import close_store as close_profile_store
from app.agents.seller.analysis_store import close_pool as close_seller_analysis_pool
from app.agents.seller.analysis_store import ensure_schema as ensure_seller_analysis_schema
from app.agents.seller.analysis_store import warm_pool as warm_seller_analysis_pool
from app.agents.seller.checkpoint import close_checkpointer as close_seller_checkpointer
from app.agents.seller.history import close_store as close_seller_history_store
from app.api import chat, events, internal, profile_graph, seller
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.clock import KST
from app.core.conversation import close_store as close_conversation_store
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handling
from app.core.logging import configure_logging, get_logger
from app.core.model_pricing import log_model_price_table_status
from app.core.pg_store import close_store as close_pg_store
from app.core.pg_resilience import close_advisory_pool
from app.core.session_context import close_session_lifecycle, initialize_session_lifecycle
from app.core.ratelimit import rate_limit_middleware
from app.core.stream import (
    close_stream_registry,
    initialize_stream_registry,
    registry_is_process_local,
)
from app.pipelines.category_seed import (
    CategoryDictionaryError,
    check_category_dictionary,
    unreachable_db_error_types,
)
from app.pipelines.scheduler import start_scheduler, stop_scheduler

logger = get_logger(__name__)


def _warn_process_local_registry_workers() -> None:
    """프로세스 로컬 레지스트리와 uvicorn worker 설정의 위험한 조합을 관측만 한다.

    프로세스 로컬 여부는 이제 `STREAM_REGISTRY_BACKEND` 에서 파생된다 (#476,
    docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md §1).
    """
    if not registry_is_process_local():
        return
    try:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        return
    if workers >= 2:
        logger.warning(
            "WEB_CONCURRENCY=%s with process-local stream registry can bypass the §2.9(a) "
            "concurrency guard; see docs/specs/OPS-SCALEOUT-476.md",
            workers,
        )


async def _close_owned_resources() -> None:
    """소유한 리소스를 의존성 역순으로 닫고 개별 실패를 격리한다."""
    resources = (
        ("stream_registry", close_stream_registry),
        ("session_lifecycle", close_session_lifecycle),
        ("seller_history_store", close_seller_history_store),
        ("seller_checkpointer", close_seller_checkpointer),
        # 전용 풀(D-3, 이슈 #585) — checkpointer 단일 커넥션과 별개, BaseStore 와도 별개다.
        ("seller_analysis_pool", close_seller_analysis_pool),
        # 그래프 저널(#358)은 profile_store 보다 **먼저** 닫는다 — 변경 조립이 store 를 부르므로
        # 의존하는 쪽이 앞이다(이 목록은 의존성 역순).
        ("graph_journal_pool", close_graph_journal_pool),
        ("profile_store", close_profile_store),
        ("session_activity_pool", close_session_activity_pool),
        ("processed_events_pool", close_processed_events_pool),
        ("conversation_store", close_conversation_store),
        ("pg_store", close_pg_store),
        ("advisory_pool", close_advisory_pool),
    )
    settings = get_settings()
    timeout_s = settings.lifespan_resource_close_timeout_s
    floor_s = settings.lifespan_resource_close_floor_s
    loop = asyncio.get_running_loop()
    cleanup_budget_s = settings.lifespan_cleanup_budget_s
    deadline = loop.time() + cleanup_budget_s
    required_floor_s = len(resources) * floor_s
    if cleanup_budget_s < required_floor_s:
        logger.warning(
            "lifespan cleanup budget cannot reserve resource floor "
            "budget_s=%s floor_s=%s resources=%d required_s=%s",
            cleanup_budget_s,
            floor_s,
            len(resources),
            required_floor_s,
        )
    failed = 0
    cancellation: asyncio.CancelledError | None = None
    for index, (name, close) in enumerate(resources):
        remaining_budget_s = max(deadline - loop.time(), 0.0)
        remaining_count = len(resources) - index
        reserved_for_rest_s = (remaining_count - 1) * floor_s
        allowance_s = remaining_budget_s - reserved_for_rest_s
        resource_timeout_s = min(timeout_s, max(allowance_s, 0.0))
        budget_limited = allowance_s < timeout_s
        try:
            async with asyncio.timeout(resource_timeout_s):
                await close()
        except asyncio.CancelledError as exc:
            failed += 1
            cancellation = exc
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            logger.warning("lifespan resource cleanup cancelled resource=%s", name)
        except TimeoutError:
            failed += 1
            if budget_limited:
                logger.warning(
                    "lifespan resource cleanup budget exhausted resource=%s "
                    "timeout_s=%s budget_s=%s",
                    name,
                    resource_timeout_s,
                    cleanup_budget_s,
                )
            else:
                logger.warning(
                    "lifespan resource cleanup timed out resource=%s timeout_s=%s",
                    name,
                    resource_timeout_s,
                )
        except Exception:
            failed += 1
            logger.exception("lifespan resource cleanup failed resource=%s", name)
    logger.info(
        "lifespan resource cleanup complete succeeded=%d failed=%d",
        len(resources) - failed,
        failed,
    )
    if cancellation is not None:
        raise cancellation


async def _check_category_dictionary_startup() -> None:
    """categories 0행/0임베딩 가드(이슈 #401) — 기동 시 1회.

    세 갈래로 원인을 구분해 로그는 정직하게 남기되(진단 정보는 뭉개지 않는다), **거부 여부는
    `fail` 모드에서 하나로 통일**한다(#401 라운드 7 리뷰 F8). 라운드 5(F7)의 "도달 불가는 모든
    모드에서 WARNING"은 리뷰가 실측으로 반박했다 — `psycopg.OperationalError` 는 일시적 도달
    불가(연결 거부·타임아웃)와 **영구적 구성 오류**(비밀번호·dbname 오타)를 **같은 타입**으로
    낸다. libpq 는 연결 수립 실패에 `sqlstate` 등 구조화된 판별자를 주지 않고 차이는 지역화되는
    메시지 문자열뿐이라(실측 확인) 예외 타입으로 "진짜 구성 오류"만 골라내는 건 불가능하다.

    그래서 분류 대신 `fail` 의 **의미**를 바꾼다 — `fail` 은 "강한 검증을 opt-in 한 모드"이므로
    계약은 "**사전이 건강함을 확인하지 못하면 기동하지 않는다**"다(원인이 오타든 일시적 불통이든
    가드 자체의 버그든 상관없이). `log`(기본값)·`off` 의 동작은 이전과 동일하다:

    1. `CategoryDictionaryError`(0행/0임베딩·비연결 DB 오류 — 모드는 `check_category_dictionary`
       안에서 이미 적용됨) — 그대로 전파해 기동을 거부한다.
    2. **도달 불가 계열**(`OSError`·`psycopg.OperationalError` —
       `category_seed.unreachable_db_error_types()` 로 얻는다. `app/main.py` 는 psycopg 를
       import 하지 않는다는 lazy-import 관례를 지키려고 `category_seed.py` 가 타입을 대신
       노출한다) — `log`/`off` 는 WARNING 후 계속(부팅 순간 DB 흔들림으로 서버 전체를 못 뜨게
       하는 건 과하다). **`fail` 은 ERROR 로그 후 전파**한다 — 잘못된 DSN(비밀번호·dbname
       오타)도 이 타입으로 나오므로, `fail` 을 건 운영자가 가장 잡고 싶은 실수를 잡으려면
       도달 불가도 "확인 실패"로 다뤄야 한다.
    3. **그 밖의 예상 못 한 예외**(가드 코드 자체 버그) — ERROR 로그("도달 불가"가 아니라
       "예상 못 한 실패"임을 정직하게 남긴다, `exc_info=True`). `log`/`off` 는 계속하고
       `fail` 은 전파한다 — 같은 "확인 실패면 거부" 원칙의 연장이다.
    """
    settings = get_settings()
    mode = settings.category_dictionary_startup_check
    try:
        await asyncio.to_thread(
            check_category_dictionary,
            settings.catalog_db_url,
            mode=mode,
        )
    except CategoryDictionaryError:
        raise
    except unreachable_db_error_types():
        if mode == "fail":
            logger.error(
                "category dictionary startup check could not reach the database — "
                "failing startup because fail mode requires confirmed dictionary health "
                "(cause may be transient unreachability OR a permanent misconfiguration "
                "such as a wrong password/dbname — psycopg cannot reliably distinguish them)",
                exc_info=True,
            )
            raise
        logger.warning(
            "category dictionary startup check could not reach the database", exc_info=True
        )
    except Exception:
        logger.error(
            "category dictionary startup check failed unexpectedly (guard bug, not a reachability issue)",
            exc_info=True,
        )
        if mode == "fail":
            raise


async def _warm_graph_journal_pool() -> None:
    """`graph_journal` 풀을 미리 열되 **실패해도 기동을 막지 않는다** (#359).

    이 풀은 pg-profile 에 붙는 세 번째 풀(BaseStore·advisory 와 별개)인데 종료 목록에만 있고
    시작 자리가 없었다. #359 가 중지 게이트를 구매자 hot-path 에 붙이면서 첫 호출자가 백그라운드
    sweep 에서 **사용자 턴**으로 바뀌어, 지연 초기화(연결 5s + 마이그레이션 30s, `_init_lock`
    직렬화) 비용을 first-token 10s 관문 안에서 물게 된다.

    워밍은 최적화이지 선결 조건이 아니다 — 실패하면 지연 초기화가 원래 하던 일을 그대로 한다.
    기동 실패로 승격시키면 개인화와 무관한 구매자 턴까지 서버가 안 뜬다.
    """
    try:
        await warm_graph_journal_pool()
    except Exception:  # noqa: BLE001 - 워밍 실패는 기동을 막지 않는다
        logger.warning("graph_journal_pool_warm_failed", exc_info=True)


async def _warm_seller_analysis_pool() -> None:
    """analysis_store 전용 풀을 미리 열되 실패해도 기동을 막지 않는다 (이슈 #585, graph_journal 선례).

    `ensure_seller_analysis_schema()`가 이미 이 풀을 초기화하므로 보통은 즉시 반환하는
    no-op 이다 — 그 호출이 실패해 아직 풀이 없는 경우(dev/test no-op 확정 등)의 안전망이다.
    """
    try:
        await warm_seller_analysis_pool()
    except Exception:  # noqa: BLE001 - 워밍 실패는 기동을 막지 않는다
        logger.warning("seller_analysis_pool_warm_failed", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifecycle migration 뒤 scheduler를 시작하고 owned resources를 역순 종료한다."""
    scheduler_started = False
    try:
        log_model_price_table_status(get_settings())
        _warn_process_local_registry_workers()
        await _check_category_dictionary_startup()
        await initialize_session_lifecycle()
        # 공유 레지스트리 백엔드일 때만 스키마·풀을 준비한다(기본 memory 는 no-op).
        await initialize_stream_registry()
        # 판매자 분석 저장 계층(#585) — 5테이블 idempotent 생성 + 존재 검증(D-2). 운영(jwks)은
        # 누락 시 기동을 거부한다 — ensure_seller_analysis_schema()를 감싸지 않고 그대로 전파한다.
        await ensure_seller_analysis_schema()
        await _warm_graph_journal_pool()
        await _warm_seller_analysis_pool()
        start_scheduler()
        scheduler_started = True
        yield
    finally:
        try:
            if scheduler_started:
                stop_scheduler()
        finally:
            await _close_owned_resources()


def _warn_if_scripted_llm(settings: Settings) -> None:
    """G2(#438) — 스텁 LLM 활성 시 기동 로그에 눈에 띄는 배너를 남긴다.

    G1(config.py `_forbid_scripted_outside_local`)이 local/test 밖 기동 자체를 막지만, 그
    안에서도 "지금 이 서버가 가짜 응답을 낸다"는 사실을 로그로만 보고 켠 사람이 잊을 수 있다.
    운영 var 실수(deploy.yml `LLM_PROVIDER`)는 G1 이 기동 자체를 거부해 이 배너까지 갈 일이
    없다. local/test 전용 배포에서는 이 배너로 scripted 프로파일과 지연값을 확인한다.
    """
    if settings.llm_provider != "scripted":
        return
    logger.warning(
        "=" * 60
        + "\nSTUB LLM MODE — LLM_PROVIDER=scripted\n"
        + f"SCRIPTED_LLM_MODE={settings.scripted_llm_mode} "
        + f"delay_s={settings.scripted_llm_delay_s}\n"
        + "이 서버는 모든 LLM 호출에 결정론 가짜 응답을 낸다 — 실 모델 호출이 아니다.\n"
        + "운영 사용 금지: 사용자에게 정상 200 으로 가짜 추천/응답이 나간다(#438).\n"
        + "부하 테스트 전용(app_environment=local|test 에서만 기동 허용).\n"
        + "=" * 60
    )


def _warn_if_timezone_mismatch() -> None:
    """[이슈 #583] 프로세스 TZ 가 KST 가 아니면 기동 로그에 경고를 남긴다.

    **기동을 막지 않는다.** 기준 시각은 app/core/clock.py 가 명시 오프셋으로 계산하므로
    프로세스 TZ 가 무엇이든 판매자 기간 해석·generatedAt 은 동일하게 KST 다 — 즉 불일치는
    동작 버그가 아니다. 다만 `%(asctime)s` 로 찍히는 로그 타임스탬프는 로컬 TZ 를 따르므로,
    운영 로그를 KST 로 읽는다는 팀 전제와 어긋나면 장애 분석 때 시각 비교가 틀어진다.
    Dockerfile·docker-compose 의 `TZ=Asia/Seoul` 이 빠졌을 때 그 사실을 여기서 드러낸다.

    fail 로 올리지 않는 이유: 로컬(UTC WSL)·CI 컨테이너가 전부 UTC 라 기동이 막힌다.
    """
    local_offset = datetime.now().astimezone().utcoffset()
    if local_offset == KST.utcoffset(None):
        return
    logger.warning(
        "timezone mismatch — 프로세스 TZ 오프셋=%s, 기대=%s(KST). "
        "코드 기준 시각(app/core/clock.py)은 명시 오프셋이라 영향받지 않지만 "
        "로그 타임스탬프가 KST 가 아니다 — 배포 환경에 TZ=Asia/Seoul 을 설정할 것(#583).",
        local_offset,
        KST.utcoffset(None),
    )


def create_app() -> FastAPI:
    """FastAPI 앱을 생성·구성해 반환한다 (앱 팩토리)."""
    configure_logging()
    settings = get_settings()
    _warn_if_scripted_llm(settings)
    _warn_if_timezone_mismatch()

    app = FastAPI(
        title="Jarvis AI Server",
        version="0.1.0",
        description="agentic commerce AI server (FastAPI + LangGraph)",
        lifespan=_lifespan,
    )

    # 오류 봉투(§2.5) 예외 핸들러 + requestId 미들웨어. 스트림 전 거부/검증 실패를
    # {"error":{code,message,requestId}} 봉투로 낸다.
    install_error_handling(app)

    # 레이트 리밋(§2.8, 채팅 전송 경로) — requestId 미들웨어보다 안쪽에 둔다.
    app.middleware("http")(rate_limit_middleware)

    # 요청 바디 크기 상한(이슈 #299) — 레이트 리밋보다 **바깥**에 둔다. rate_limit_middleware 는
    # _verified_sub_scope() 에서 JWT 서명 검증(필요 시 JWKS HTTP)을 돌리는데, 거대 바디를 그
    # 비용 뒤에 거절하면 방어 목적이 반감된다. 또 거대 바디 요청이 레이트 리밋 슬롯을 소모하지
    # 않게 한다. Starlette 은 add_middleware 가 user_middleware.insert(0, ...) 이고 스택을
    # reversed() 로 감싸므로 **나중에 등록한 것이 바깥**이다 — 아래 CORSMiddleware(이미 이
    # 규약으로 최외곽에 등록돼 있던 것) 보다 먼저 등록해 그 안쪽(= rate_limit_middleware 바깥)에
    # 둔다. 실측: app.user_middleware 등록 순서와 TestClient 왕복(순서 증명 테스트,
    # tests/unit/test_body_limit.py) 로 확인 — 바디 초과 요청은 레이트 리밋 상한 횟수를 넘겨
    # 보내도 슬롯을 소모하지 않는다.
    app.add_middleware(BodySizeLimitMiddleware)

    # CORS 는 최외곽(가장 마지막 등록)에 둬 오류·429·바디초과 400 응답에도 헤더가 실리게 한다
    # (api-spec §2.7, 기존 429 와 같은 이유). Authorization 헤더 사용 → 브라우저 preflight(OPTIONS) 발생.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # MVP 라우터: 사용자 대면 chat / seller 만 등록한다.
    app.include_router(chat.router)
    app.include_router(seller.router)
    app.include_router(events.router)
    # Spring → AI 위임(레인 b) — I-22 홈 추천 랭킹(§3.7, #148)
    app.include_router(internal.router)
    # 같은 레인 — 마이페이지 취향 관리 I-32~I-37(§3.8·§3.9, #360). `M-11`~`M-16` 의 internal 판이다.
    app.include_router(profile_graph.router)

    # [#574] GET 과 함께 HEAD 도 연다 — FastAPI 의 `APIRoute` 는 Starlette 의 순수 `Route` 와
    # 달리 GET 등록 시 HEAD 를 자동으로 붙이지 않아, `@app.get` 만 쓰면 HEAD 가 405 로 떨어진다.
    # 외부 업타임 모니터(UptimeRobot 무료 플랜)는 HEAD 만 보낼 수 있어 서비스가 살아 있어도
    # 다운으로 보고됐다. 응답은 상수라 HEAD 를 열어도 노출 정보가 늘지 않는다.
    @app.api_route("/health", methods=["GET", "HEAD"], tags=["ops"])
    async def health() -> dict:
        """헬스 체크. 컨테이너 healthcheck·부팅 확인용."""
        return {"status": "ok"}

    return app


app = create_app()
