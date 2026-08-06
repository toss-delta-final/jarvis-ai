"""셀별 N 채우기 루프 — 이 프로브의 심장 (#331).

원칙(이 저장소의 `evals/intent_probe` 확립 규약을 승계): **실패는 표본이 아니다.** 429·타임아웃·
파싱 실패는 버리고 다시 호출해 성공 N개를 채운다. decompose 가 intent 를 `recommend` 가 아닌
값으로 낸 시도(라우팅 미끄러짐)도 표본으로 세지 않고 버린다 — 그 실패는 `evals/intent_probe` 가
재는 축이라 여기 섞으면 두 하네스의 실패가 뒤섞인다(packet-331 §1).

배포 파이프라인과 **같은 함수를 같은 순서·같은 인자로** 부른다: `decompose` → `map_categories`.
`map_categories` 의 `search_top_k`/`exact_lookup`/`embed` 는 실물에 **위임하면서 기록만** 하는
래퍼로 주입한다(재구현 금지의 핵심, packet-331 §1).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.agents.buyer.recommendation.category_mapping import map_categories as _map_categories
from app.agents.buyer.recommendation.decompose import decompose
from app.core.llm import LLMClient
from app.pipelines.category_search import exact_lookup as _exact_lookup
from app.pipelines.category_search import search_categories_pg as _search_top_k
from app.pipelines.embedding import embed_texts as _embed_texts
from evals.category_probe.schema import AnchorCell, AnchorSet

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0
EMBED_RETRY_ATTEMPTS = 2  # embed 429/일시 오류 흡수 — LLM 페이서 밖(packet-331 §1)

_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")

CAPTURED_LOGGERS = (
    "app.agents.buyer.recommendation.category_mapping",
    "app.agents.buyer.recommendation.category_select",
)
_STANDARD_LOGRECORD_ATTRS = frozenset(
    logging.LogRecord(
        name="x", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__
)
_log_events_var: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "category_probe_log_events", default=None
)
_log_capture_installed = False

# [F1-1] `map_categories` 는 pg/embed/select 예외를 내부에서 흡수하고 degraded 매핑(흔히 legs=[])을
# **정상 반환**한다 — 예외가 밖으로 전파되지 않으므로 러너가 캡처한 구조화 이벤트로만 인프라 실패를
# 알아챌 수 있다. 이 이벤트가 하나라도 찍힌 시도는 "매핑이 실패했다"가 아니라 "인프라가 순간
# 장애였다"는 뜻이라 표본으로 세면 안 된다(§ CategoryMapping docstring 의 담지 않는다 ③번과 같은
# 원칙 — 내용 기반이 아닌 실패를 내용 기반 지표에 섞지 않는다).
_INFRA_FAILURE_EVENTS = frozenset(
    {
        "category_leg_search_failed",
        "category_embed_failed",
        "category_exact_failed",
        "category_select_stage_failed",
        "category_assembly_failed",
    }
)
# §4.4 택일의 `category_select_unavailable` 은 세 **정책** 사유(예산 상한·LLM 미구성·후보 밖 값)
# 에서도 뜬다 — 그건 배포의 정상 동작(top-1 유지 degrade)이라 표본을 살린다. 그 셋이 아닌 reason
# 은 택일 LLM 호출이 예외로 죽었다는 뜻(`reason=str(exc)`)이라 인프라 실패로 취급한다.
_SELECT_UNAVAILABLE_POLICY_REASONS = frozenset({"max_calls", "llm_unavailable", "off_candidate"})


def _infra_failure_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """캡처된 이벤트 중 인프라 실패를 가리키는 첫 이벤트(없으면 None)."""
    for event in events:
        name = event.get("event")
        if name in _INFRA_FAILURE_EVENTS:
            return event
        if (
            name == "category_select_unavailable"
            and event.get("reason") not in _SELECT_UNAVAILABLE_POLICY_REASONS
        ):
            return event
    return None


def scrub_message(message: str) -> str:
    """실패 메시지에서 계정 식별자를 지운다(intent_probe.runner 와 동일 규약)."""
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


class _CaptureHandler(logging.Handler):
    """샘플 단위로 켜지는 contextvar 표적에만 구조화 이벤트를 적재한다.

    핸들러는 모듈 로드시 **한 번만** 로거에 붙는다 — 매 호출마다 add/remove 하면 동시 실행 중인
    다른 셀의 로그를 붙잡거나 놓치는 경합이 생긴다. 대신 `_log_events_var`(contextvars)로 표적을
    구분한다 — asyncio Task 마다 독립된 컨텍스트 사본을 가지므로 동시성 concurrency>1 에서도
    셀 간 이벤트가 섞이지 않는다(intent_probe.client 의 `_call_site` ContextVar 와 동일 패턴).
    """

    def emit(self, record: logging.LogRecord) -> None:
        target = _log_events_var.get()
        if target is None:
            return
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOGRECORD_ATTRS
        }
        target.append({"event": record.getMessage(), **extra})


def install_log_capture() -> None:
    """`category_mapping`/`category_select` 로거에 캡처 핸들러를 (멱등하게) 붙인다."""
    global _log_capture_installed
    if _log_capture_installed:
        return
    handler = _CaptureHandler()
    handler.setLevel(logging.INFO)
    for name in CAPTURED_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
    _log_capture_installed = True


@contextmanager
def capture_log_events():
    events: list[dict[str, Any]] = []
    token = _log_events_var.set(events)
    try:
        yield events
    finally:
        _log_events_var.reset(token)


def _embed_with_retry(
    real_embed: Callable[[list[str]], list[list[float]]],
    texts: list[str],
    *,
    attempts: int = EMBED_RETRY_ATTEMPTS,
    base_delay: float = 0.5,
) -> list[list[float]]:
    last_exc: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            return real_embed(texts)
        except Exception as exc:  # noqa: BLE001 - 429/일시 오류만 흡수, 그 외도 결국 attempts 소진 후 전파
            last_exc = exc
            if attempt < attempts:
                time.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


@dataclass
class HitRecord:
    """앵커(leg × raw|query) 하나의 top-k 히트 1건 — #344 임계 스윕용 원시 관측."""

    leg_index: int
    anchor_kind: str  # "raw" | "query"
    anchor_text: str
    rank: int
    canonical: str
    distance: float


class _CallRecorder:
    """`map_categories` 가 부르는 embed/search/exact 를 실물에 위임하며 기록만 한다.

    이 인스턴스는 **호출 1건 전용**(run_cell 의 시도마다 새로 만든다) — 동시에 여러 셀이 도는
    concurrency>1 에서도 상태가 섞이지 않는다. `vec_index` 는 `embed()` 가 돌려준 벡터 객체의
    `id()` 로 `search()` 가 받은 벡터를 anchors 순서(§ 아래 `reconstruct_hits`)에 되짚는다 —
    `map_categories` 는 `vecs[j]` 를 그대로(카피 없이) 넘기므로 파이썬 객체 identity 비교가
    유효하다.
    """

    def __init__(
        self,
        *,
        real_embed: Callable[[list[str]], list[list[float]]],
        real_search: Callable[..., list[tuple[str, float]]],
        real_exact: Callable[..., set[str]],
    ) -> None:
        self._real_embed = real_embed
        self._real_search = real_search
        self._real_exact = real_exact
        self.embed_call_count = 0
        self.exact_result: set[str] = set()
        self.search_calls: list[tuple[int | None, list[tuple[str, float]]]] = []
        self._vec_index: dict[int, int] = {}

    def exact(self, values, dsn):  # noqa: ANN001
        result = self._real_exact(values, dsn)
        self.exact_result = set(result)
        return result

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_call_count += 1
        vecs = _embed_with_retry(self._real_embed, texts)
        self._vec_index = {id(vec): index for index, vec in enumerate(vecs)}
        return vecs

    def search(self, vec, dsn, *, k):  # noqa: ANN001
        hits = self._real_search(vec, dsn, k=k)
        anchor_index = self._vec_index.get(id(vec))
        self.search_calls.append((anchor_index, list(hits)))
        return hits


def _reconstruct_anchor_order(
    category_queries, fanout_max: int
) -> tuple[list[str | None], list[str | None]]:
    """`map_categories` 의 anchors 조립 순서를 **인덱스만** 그대로 되밟는다(재구현 아님).

    이 함수가 되밟는 것은 "어떤 leg 의 어떤 앵커(raw|query)를 몇 번째로 임베딩했는가"라는 순수
    배관 순서이지, 그 후보들 중 무엇을 canonical 로 채택하는지의 **판정 규칙**이 아니다(거리컷·
    마진 택일 등은 여전히 프로덕션 `map_categories` 안에서만 일어나고 여기선 다시 만들지 않는다).
    packet-331 §1 이 명시적으로 요구한 대응이다: "runner 가 anchors 순서(코드가 leg 마다 query→raw
    순으로 쌓는다)와 대조해 leg·anchor_kind 별 hit 리스트를 samples 에 남긴다."
    """
    queries = list(category_queries)[:fanout_max]
    raws = [q.raw_category for q in queries]
    qtexts = [q.query for q in queries]
    return raws, qtexts


def reconstruct_hits(recorder: _CallRecorder, category_queries, fanout_max: int) -> list[HitRecord]:
    raws, qtexts = _reconstruct_anchor_order(category_queries, fanout_max)
    exact = recorder.exact_result
    need_idx = [
        i
        for i in range(len(raws))
        if (raws[i] and raws[i] not in exact) or (not raws[i] and qtexts[i])
    ]
    anchors: list[tuple[int, str, str]] = []
    for i in need_idx:
        query_text = qtexts[i]
        if query_text:
            anchors.append((i, "query", query_text))
        raw_text = raws[i]
        if raw_text:
            anchors.append((i, "raw", raw_text))
    records: list[HitRecord] = []
    for anchor_index, hits in recorder.search_calls:
        if anchor_index is None or anchor_index >= len(anchors):
            continue
        leg_index, kind, text = anchors[anchor_index]
        for rank, (canonical, distance) in enumerate(hits, start=1):
            records.append(
                HitRecord(
                    leg_index=leg_index,
                    anchor_kind=kind,
                    anchor_text=text,
                    rank=rank,
                    canonical=canonical,
                    distance=distance,
                )
            )
    return records


@dataclass(frozen=True)
class Sample:
    """성공한 표본 1건 — decompose + map_categories 1회 왕복."""

    cell_id: str
    sample_index: int
    legs: list[tuple[str, str | None]]
    unresolved: list[str]
    expansion_leaves: list[tuple[str, str | None]]
    select_calls: int
    decompose_legs: list[tuple[str | None, str | None]]  # (raw_category, query) — decompose 원산출
    events: list[dict[str, Any]]
    hits: list[HitRecord]
    latency_ms: int


@dataclass(frozen=True)
class FailureRecord:
    cell_id: str
    attempt: int
    error_type: str
    message: str


@dataclass
class CellResult:
    cell_id: str
    utterance_id: str
    slice_id: str
    test_type: str
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    intent_slip_count: int = 0
    filled: bool = False


async def run_cell(
    *,
    llm: LLMClient,
    cell: AnchorCell,
    n: int,
    tier: str,
    attempt_multiplier: int,
    category_fanout_max: int = 5,
    settings: Any = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    search_top_k: Callable[..., list[tuple[str, float]]] | None = None,
    exact_lookup: Callable[..., set[str]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CellResult:
    """성공 표본 N개를 채울 때까지 재시도한다. 예산 초과만 밖으로 던진다."""
    from app.core.config import get_settings

    install_log_capture()
    settings = settings if settings is not None else get_settings()
    real_embed = embed or (
        lambda texts: _embed_texts(texts, task_type=settings.embedding_task_query)
    )
    real_search = search_top_k or _search_top_k
    real_exact = exact_lookup or _exact_lookup

    result = CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.cell_id,
        slice_id=cell.slice_id,
        test_type=cell.test_type,
    )
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        try:
            decision = await decompose(
                llm,
                query=cell.utterance,
                prior_filters=None,
                profile_summary=None,
                tier=tier,
                category_fanout_max=category_fanout_max,
            )
        except Exception as exc:  # noqa: BLE001 - provider·파싱 실패 전부. 표본으로 세지 않는다.
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            await sleep(
                min(BACKOFF_BASE_S * (2 ** max(len(result.failures) - 1, 0)), BACKOFF_MAX_S)
            )
            continue
        if decision.intent != "recommend":
            # 라우팅 미끄러짐 — intent_probe 가 재는 축이다. 버리고 재시도(packet-331 §1).
            result.intent_slip_count += 1
            continue
        recorder = _CallRecorder(
            real_embed=real_embed, real_search=real_search, real_exact=real_exact
        )
        try:
            with capture_log_events() as events:
                mapping = await _map_categories(
                    category_queries=decision.category_queries,
                    utterance=cell.utterance,
                    settings=settings,
                    embed=recorder.embed,
                    search_top_k=recorder.search,
                    exact_lookup=recorder.exact,
                    llm=llm,
                    tier=tier,
                )
        except Exception as exc:  # noqa: BLE001 - map_categories 는 내부적으로 거의 던지지 않으나 방어한다.
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            await sleep(
                min(BACKOFF_BASE_S * (2 ** max(len(result.failures) - 1, 0)), BACKOFF_MAX_S)
            )
            continue
        # [F1-1] map_categories 는 pg/embed/select 예외를 삼키고 degraded 매핑을 정상 반환한다 —
        # 캡처된 이벤트로만 그 흡수를 알아챌 수 있다. 인프라 실패는 표본이 아니다: 버리고 재시도.
        infra_event = _infra_failure_event(events)
        if infra_event is not None:
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=str(infra_event.get("event")),
                    message=scrub_message(str(infra_event.get("reason", "")))[:200],
                )
            )
            await sleep(
                min(BACKOFF_BASE_S * (2 ** max(len(result.failures) - 1, 0)), BACKOFF_MAX_S)
            )
            continue
        hits = reconstruct_hits(recorder, decision.category_queries, category_fanout_max)
        result.samples.append(
            Sample(
                cell_id=cell.cell_id,
                sample_index=len(result.samples),
                legs=list(mapping.legs),
                unresolved=list(mapping.unresolved),
                expansion_leaves=list(mapping.expansion_leaves),
                select_calls=mapping.select_calls,
                decompose_legs=[(q.raw_category, q.query) for q in decision.category_queries],
                events=events,
                hits=hits,
                latency_ms=int(round((perf_counter() - started) * 1000)),
            )
        )
    result.filled = len(result.samples) == n
    return result


async def run_probe(
    *,
    llm: LLMClient,
    anchors: AnchorSet,
    n: int,
    tier: str,
    attempt_multiplier: int,
    concurrency: int = 1,
    category_fanout_max: int = 5,
    settings: Any = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    search_top_k: Callable[..., list[tuple[str, float]]] | None = None,
    exact_lookup: Callable[..., set[str]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_cell_done: Callable[[CellResult], None] | None = None,
) -> list[CellResult]:
    """전 셀을 돌린다. 결과는 항상 cellId 정렬이라 동시성이 순서를 바꾸지 않는다."""
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _one(cell: AnchorCell) -> CellResult:
        async with semaphore:
            result = await run_cell(
                llm=llm,
                cell=cell,
                n=n,
                tier=tier,
                attempt_multiplier=attempt_multiplier,
                category_fanout_max=category_fanout_max,
                settings=settings,
                embed=embed,
                search_top_k=search_top_k,
                exact_lookup=exact_lookup,
                sleep=sleep,
            )
        if on_cell_done is not None:
            on_cell_done(result)
        return result

    results = await asyncio.gather(*(_one(cell) for cell in anchors.cells))
    return sorted(results, key=lambda result: result.cell_id)


def unfilled_cells(results: list[CellResult], *, n: int) -> list[dict[str, Any]]:
    return [
        {
            "cellId": result.cell_id,
            "got": len(result.samples),
            "want": n,
            "attempts": result.attempts,
            "errorTypes": sorted({failure.error_type for failure in result.failures}),
        }
        for result in results
        if not result.filled
    ]
