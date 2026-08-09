"""#277 실측 하네스 — may_auto_relax 턴의 첫 SSE 이벤트 지연.

측정 대상: FE→AI 경계에서 요청 전송 ~ 첫 `data:` 프레임 수신까지의 벽시계(ms).
이는 api-spec §2.9(c) first-token 상한(10s)이 실제로 재는 구간이며,
evals/benchmark runner 의 `client_first_event_ms` 와 같은 지점이다(#138).

대역:
  - Spring: httpx.MockTransport (HTTP 경계) — 시나리오별 지연·타임아웃 주입.
    Spring 미기동 환경이라 재시도 경로는 주입으로 재현한다(3s 타임아웃 = spring_timeout_s).
  - LLM: ScriptedLLM (decompose/rerank 즉시 응답, ~0ms) — LLM head 는 별도 보정(보고서 참조).
  - 임베딩 재정렬·pg-catalog·pg-profile: **실물**(라이브 컨테이너·라이브 임베딩 API).

사용: uv run python <this> --out <json>
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# ── env 핀 (Settings 인스턴스화 전에) ──────────────────────────────────────────
# 인증 레인 dev 고정 + 앱 자기 레이트 리밋 분리(lessons 2026-08-02).
os.environ["AUTH_MODE"] = "dev"
os.environ["RATE_LIMIT_PER_MIN"] = "100000"
os.environ["RATE_LIMIT_PER_HOUR"] = "100000"
os.environ["INTERNAL_API_TOKEN"] = "measure-277-internal-token"

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import jwt  # noqa: E402

import app.main as main_mod  # noqa: E402
import app.services.spring_client as sc  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from evals.benchmark.stats import bootstrap_percentile_ci  # noqa: E402
from tests.integration._stubs import ScriptedLLM  # noqa: E402

SETTINGS = get_settings()
# [#306] 주입 지연의 기준은 **검색 전용** 타임아웃이다 — #427 이 I-1 만 `spring_search_timeout_s`
# 로 분리했으므로 공용 `spring_timeout_s` 를 쓰면 둘이 갈리는 순간 시나리오가 실제 컷 지점과
# 어긋난다(오늘은 둘 다 3.0 이라 값은 같다).
SPRING_TIMEOUT_S = SETTINGS.spring_search_timeout_s
# 타임아웃 직전 성공(3s 상한 아래 2.9s) — 재시도가 즉답한다는 낙관 가정을 배제한 최악 성공.
SLOW_OK_S = 2.9
PORT = 8277

# ── 카탈로그 ─────────────────────────────────────────────────────────────────
# rating 4.5 미만만 담은 카탈로그: ratingMin=4.5 사후필터에서 0건 → 자동 완화(4.0) 후 3건.
ZERO_CATALOG = [
    {
        "productId": 201,
        "name": "여행용 방수 파우치 L",
        "price": 19000,
        "categoryName": "여행용품",
        "brandName": "트래블러",
        "rating": 4.4,
        "reviewCount": 320,
    },
    {
        "productId": 202,
        "name": "기내반입 세면도구 파우치",
        "price": 24000,
        "categoryName": "여행용품",
        "brandName": "패커스",
        "rating": 4.3,
        "reviewCount": 158,
    },
    {
        "productId": 203,
        "name": "대용량 캐리어 파우치 3종",
        "price": 28000,
        "categoryName": "여행용품",
        "brandName": "트래블러",
        "rating": 4.1,
        "reviewCount": 87,
    },
]
# rating 4.5 이상 1건 포함 — ratingMin=4.5 에서도 후보가 남아 완화 probe 가 돌지 않는다.
HIT_CATALOG = [{**ZERO_CATALOG[0], "productId": 204, "rating": 4.6}, *ZERO_CATALOG]


def decompose_payload(*, rating_min: float | None) -> dict:
    filters: dict = {"priceMax": 30000, "keyword": "여행 파우치"}
    if rating_min is not None:
        filters["ratingMin"] = rating_min
    return {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": "여행용 파우치",
        "categoryQueries": [{"category": "여행용품", "query": "여행 파우치"}],
        "filters": filters,
    }


class Harness:
    """Spring 대역 + 시나리오 plan. plan 은 검색 HTTP **시도**마다 하나씩 소비된다."""

    def __init__(self) -> None:
        self.plan: list[str] = []
        self.search_attempts = 0
        self.timings: dict[str, list[float]] = {}

    def reset(self, plan: list[str]) -> None:
        self.plan = list(plan)
        self.search_attempts = 0
        self.timings = {}

    def mark(self, name: str, value: float) -> None:
        self.timings.setdefault(name, []).append(value)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/internal/products/search":
            return await self._search()
        if request.method == "GET" and path.startswith("/internal/members/"):
            # I-19 구매 이력 — 회원이라 호출되며 빈 목록으로 즉시 응답(dedup 대상 없음).
            return httpx.Response(200, json={"success": True, "data": {"orders": []}})
        if request.method == "POST" and path == "/internal/recommendations":
            body = json.loads(request.content or b"{}")
            ids = [str(item.get("listId")) for item in (body.get("lists") or [])]
            return httpx.Response(200, json={"success": True, "data": {"listIds": ids}})
        return httpx.Response(404, json={"success": False, "error": {"code": "NOT_FOUND"}})

    async def _search(self) -> httpx.Response:
        import asyncio

        idx = self.search_attempts
        self.search_attempts += 1
        behaviour = self.plan[idx] if idx < len(self.plan) else self.plan[-1]
        if behaviour == "timeout":
            # 실 클라이언트 타임아웃(3s)을 벽시계로 재현한 뒤 httpx 타임아웃 예외를 던진다 —
            # MockTransport 는 timeout 설정을 스스로 강제하지 않기 때문이다.
            await asyncio.sleep(SPRING_TIMEOUT_S)
            raise httpx.ReadTimeout("injected", request=None)
        if behaviour.startswith("slow_"):
            # 타임아웃 직전에 성공하는 응답 — 재시도가 "즉답"으로 성공한다는 가정을 걷어낸다.
            await asyncio.sleep(SLOW_OK_S)
            behaviour = behaviour[len("slow_") :]
        catalog = HIT_CATALOG if behaviour == "hit" else ZERO_CATALOG
        return httpx.Response(200, json={"success": True, "data": catalog})


HARNESS = Harness()


def install() -> ScriptedLLM:
    """HTTP 경계·LLM·라이프사이클 대역을 설치한다(픽스처와 같은 지점)."""
    import app.agents.buyer.graph as buyer_graph
    import app.agents.buyer.recommendation.graph as rec_graph
    from app.core import session_context

    settings = get_settings()

    def _stub_client(*, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=settings.spring_base_url,
            timeout=settings.spring_timeout_s,
            headers={"X-Internal-Token": settings.internal_api_token},
            transport=httpx.MockTransport(HARNESS.handler),
        )

    sc._client = _stub_client

    scripted = ScriptedLLM()
    buyer_graph.get_llm = lambda: scripted

    # 구간 계측 — I-1(재시도 포함) 호출 1회, 재구매 store pg 왕복 1회.
    real_search_products = sc.search_products

    async def timed_search_products(filters):  # noqa: ANN001
        t0 = time.perf_counter()
        try:
            return await real_search_products(filters)
        finally:
            HARNESS.mark("i1_search_products_s", time.perf_counter() - t0)

    sc.search_products = timed_search_products

    real_load = rec_graph._load_persisted_repurchase

    async def timed_load(thread_key, turn_ids, settings_):  # noqa: ANN001
        t0 = time.perf_counter()
        try:
            return await real_load(thread_key, turn_ids, settings_)
        finally:
            HARNESS.mark("repurchase_store_s", time.perf_counter() - t0)

    rec_graph._load_persisted_repurchase = timed_load

    # 세션 컨텍스트·상태 저장소는 **실물 pg-profile** 그대로 둔다(운영 구성과 동일) —
    # #232 재구매 store 왕복이 첫 이벤트 예산에 들어가는지가 이 측정의 관심사다.
    # 배치 스케줄러만 끈다(측정 대상 아님).
    _ = session_context
    main_mod.start_scheduler = lambda: None
    main_mod.stop_scheduler = lambda: None
    return scripted


def member_header(user_id: str = "42") -> dict[str, str]:
    token = jwt.encode(
        {"sub": user_id, "sub_type": "member"},
        "dev-only-not-a-secret-0123456789",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def serve():
    """패치된 앱을 실 uvicorn 으로 띄운다.

    TestClient(ASGI 인프로세스)는 SSE 본문을 **통째로 버퍼링**해 첫 이벤트와 전체 종료 시각이
    같아진다(1차 실행에서 first==total 로 확인). first-token 상한이 재는 것은 네트워크로 나간
    첫 프레임이므로, evals/benchmark runner 와 같이 실 HTTP 경계에서 잰다.
    """
    import threading

    import uvicorn

    config = uvicorn.Config(
        main_mod.app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("uvicorn 기동 실패")


def run_once(client, *, thread: str, plan: list[str]) -> dict:
    """한 턴을 돌려 첫 이벤트 지연(ms)과 이벤트 순서를 기록한다."""
    HARNESS.reset(plan)
    body = {
        "sessionId": "sess-277",
        "threadId": thread,
        "message": "유럽 여행 가는데 기내 반입 되는 파우치 추천해줘",
    }
    first_ms: float | None = None
    events: list[str] = []
    t0 = time.perf_counter()
    with client.stream(
        "POST", f"http://127.0.0.1:{PORT}/chat", json=body, headers=member_header()
    ) as response:
        status = response.status_code
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            if first_ms is None:
                first_ms = (time.perf_counter() - t0) * 1000
            try:
                events.append(json.loads(line[len("data:") :].strip())["type"])
            except Exception:  # noqa: BLE001
                events.append("<unparsed>")
    total_ms = (time.perf_counter() - t0) * 1000
    return {
        "first_event_ms": first_ms,
        "total_ms": total_ms,
        "http_status": status,
        "events": events,
        "first_event_type": events[0] if events else None,
        "search_attempts": HARNESS.search_attempts,
        "i1_search_products_s": HARNESS.timings.get("i1_search_products_s", []),
        "repurchase_store_s": HARNESS.timings.get("repurchase_store_s", []),
    }


SCENARIOS = [
    # (id, ratingMin, plan, n, 설명)
    ("A_nondeferred_fast", None, ["hit"], 20, "미룸 없음(ratingMin 부재) — 검색 전 conditions"),
    ("B_deferred_hit", 4.5, ["hit"], 20, "미룬 턴, 검색 즉답·후보 있음 — probe 없음"),
    ("C_deferred_probe", 4.5, ["zero", "zero"], 20, "미룬 턴, 0건 → 자동 완화 probe 1회"),
    (
        "D_deferred_worst",
        4.5,
        ["timeout", "zero", "timeout", "zero"],
        8,
        "최악: 검색 1차 타임아웃→2차 0건, probe 1차 타임아웃→2차 성공",
    ),
    (
        "E_deferred_search_fail",
        4.5,
        ["timeout", "timeout"],
        8,
        "검색 2회 모두 타임아웃 → conditions + SEARCH_FAILED",
    ),
    (
        "F_deferred_probe_retry",
        4.5,
        ["zero", "timeout", "zero"],
        8,
        "검색 즉답 0건, probe 1차 타임아웃→2차 성공(검증기 docstring 이 상정한 최악)",
    ),
    (
        "G_nondeferred_slow_search",
        None,
        ["timeout", "hit"],
        5,
        "대조군: 미루지 않는 턴은 검색이 느려도 첫 이벤트가 즉시",
    ),
    (
        "H_deferred_search_retry_hit",
        4.5,
        ["timeout", "slow_hit"],
        8,
        "미룬 턴: 검색 1차 타임아웃→2차 타임아웃 직전 성공(후보 있음, probe 없음)",
    ),
    (
        "D2_deferred_worst_slow_ok",
        4.5,
        ["timeout", "slow_zero", "timeout", "slow_hit"],
        8,
        "실최악: 검색·probe 각각 1차 타임아웃→2차 타임아웃 직전 성공(재시도 예산 전량 소진)",
    ),
    (
        "D3_deferred_worst_no_retry",
        4.5,
        ["slow_zero", "slow_hit"],
        8,
        "재시도를 끈 뒤의 최악 — 본 검색·자동 완화 probe가 재시도 없이 각각 상한 직전(2.9s)에 성공하는 조합",
    ),
]


def summarize(values: list[float], *, seed: int) -> dict:
    clean = [v for v in values if v is not None]
    out = {
        "n": len(clean),
        "min_ms": min(clean) if clean else None,
        "p50_ms": statistics.median(clean) if clean else None,
        "max_ms": max(clean) if clean else None,
        "mean_ms": statistics.fmean(clean) if clean else None,
    }
    out["p95_ci"] = bootstrap_percentile_ci(clean, 95, resamples=2000, confidence=0.95, seed=seed)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", default=None, help="시나리오 id 접두어 필터")
    args = parser.parse_args()

    scripted = install()
    results = []
    settings = get_settings()
    server = serve()
    try:
        client = httpx.Client(timeout=60.0)
        # 워밍업 — pg 풀·임베딩 클라이언트 첫 초기화 비용을 표본에서 분리한다.
        scripted._decompose = decompose_payload(rating_min=4.5)
        for i in range(2):
            run_once(client, thread=f"warm-{i}", plan=["zero", "zero"])

        for sid, rating_min, plan, n, note in SCENARIOS:
            if args.only and not sid.startswith(args.only):
                continue
            scripted._decompose = decompose_payload(rating_min=rating_min)
            runs = []
            for i in range(n):
                run = run_once(client, thread=f"{sid}-{i}", plan=plan)
                runs.append(run)
                first = run["first_event_ms"]
                print(
                    f"{sid} #{i}: first={'none' if first is None else f'{first:.0f}ms'} "
                    f"total={run['total_ms']:.0f}ms status={run['http_status']} "
                    f"first_type={run['first_event_type']} events={run['events'][:4]}",
                    flush=True,
                )
            results.append(
                {
                    "scenario": sid,
                    "note": note,
                    "rating_min": rating_min,
                    "plan": plan,
                    "n": n,
                    "runs": runs,
                    "first_event": summarize([r["first_event_ms"] for r in runs], seed=277),
                }
            )
    finally:
        server.should_exit = True
        time.sleep(1.0)

    payload = {
        "metric": "client_first_event_ms (FE→AI 경계, 요청 송신 ~ 첫 data: 프레임)",
        "config": {
            "spring_timeout_s": settings.spring_timeout_s,
            # [#306] 아래 세 키가 없으면 아티팩트가 자기 조건을 설명하지 못한다 — #427 이후
            # 미룬 턴 본검색의 실제 총시간 예산은 `spring_search_timeout_s × attempts` 가 아니라
            # `rescue_budget_mode` 가 좁힌 값(`(30 - rescue_tail_reserve_s - 경과)/남은 단 수`)이다.
            "spring_search_timeout_s": settings.spring_search_timeout_s,
            "rescue_budget_mode": settings.rescue_budget_mode,
            "rescue_tail_reserve_s": settings.rescue_tail_reserve_s,
            "spring_max_retries": settings.spring_max_retries,
            "stream_first_token_timeout_s": settings.stream_first_token_timeout_s,
            "stream_total_timeout_buyer_s": settings.stream_total_timeout_buyer_s,
            "relaxation_max_rounds": settings.relaxation_max_rounds,
            "relaxation_auto_fields": settings.relaxation_auto_fields,
            "state_store_query_timeout_s": settings.state_store_query_timeout_s,
            "search_backend": settings.search_backend,
            "progress_events_enabled": settings.progress_events_enabled,
        },
        "bootstrap": {"resamples": 2000, "confidence": 0.95, "seed": 277},
        "scenarios": results,
    }
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
