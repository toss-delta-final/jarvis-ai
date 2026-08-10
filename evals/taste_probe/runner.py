"""세션당 N 표본 채우기 + 프로덕션 경로 호출 — 이 프로브의 심장 (#462).

원칙(`evals/intent_probe`·`evals/category_probe` 확립 규약 승계): **실패는 표본이 아니다.**
전송 실패·스키마 위반은 버리고 다시 호출해 성공 N개를 채운다.

배포 파이프라인과 **같은 함수를 같은 순서·같은 인자로** 부른다:
`store.append_session_ctx` → `generate_session_delta`(→ `should_promote` → `resolve_triple`)
→ `store.get_fact_records`. 판정(게이트·resolver)은 재구현하지 않고 프로덕션 함수를 그대로 호출한다
(#380 규약).

**스토어 리셋은 표본마다가 아니라 런 시작 시 1회다** — `reset_profile_store()` 는 프로세스
전역 싱글턴(`app.agents.profile.store._store`)을 교체한다. `--concurrency` > 1 로 여러 세션이
동시에 진행 중일 때 표본마다 리셋하면 다른 세션이 마침 쓰고 있는 전역 스토어를 통째로 갈아
치우는 레이스가 생긴다(store.py 자신이 advisory lock 으로 그런 레이스를 막는 것과 정면으로
어긋난다). 격리는 리셋이 아니라 **표본마다 유일한 `user_id`/`thread_key`** 로 이미 충분하다 —
InMemoryStore 는 네임스페이스를 `(root, user_id)`/`(root, thread_key)` 로 가르므로 다른 키는
애초에 겹치지 않는다. 호출부(`cli.py`/테스트)가 런 시작 시 `reset_profile_store()` 를 정확히
1회 불러 pg-profile 접속 0 을 보장한다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

from app.agents.buyer.recommendation.state import extract_json
from app.agents.profile.builder import generate_session_delta
from app.agents.profile.gate import should_promote
from app.agents.profile.resolver import _resolve_band
from app.agents.profile.store import get_profile_store
from app.core.llm import LLMClient, LLMError, is_output_length_error, is_timeout_error
from evals.model_eval.budget import BudgetExceeded
from evals.taste_probe.schema import GoldenSession, GoldenSet

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0
_BAND_KINDS = frozenset({"priceBand", "ratingBand"})
_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    """실패 메시지에서 계정 식별자를 지운다(`intent_probe.runner`/`category_probe.runner` 규약)."""
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


@dataclass(frozen=True)
class ProducedTriple:
    """산출 트리플 1건 — `ResolvedTriple.as_payload()` 의 저장 모양(snake_case, 와이어 아님)을
    그대로 읽는다(§3 — "필드명은 실제 dump 를 찍어 확인" 실측: node_id/type/label/verified/predicate
    모두 snake_case, CamelModel 이 아니다)."""

    node_id: str
    node_type: str
    node_label: str
    verified: bool
    predicate: str
    salience: float
    method: str | None
    distance: float | None
    margin: float | None


def _to_produced_triple(triple: dict[str, Any]) -> ProducedTriple:
    node = triple.get("node") or {}
    resolution = node.get("resolution") or {}
    return ProducedTriple(
        node_id=str(node.get("node_id") or ""),
        node_type=str(node.get("type") or ""),
        node_label=str(node.get("label") or ""),
        verified=bool(node.get("verified")),
        predicate=str(triple.get("predicate") or ""),
        salience=_as_float(triple.get("salience")),
        method=resolution.get("method"),
        distance=resolution.get("distance"),
        margin=resolution.get("margin"),
    )


@dataclass(frozen=True)
class Sample:
    """성공한 표본 1건 — 세션 버퍼 적재 + 델타 생성 1회 왕복.

    `promoted_count` 는 **프로덕션 반환값**(`generate_session_delta` 의 `promoted` 리스트 길이)
    이다 — `emittedDeltas - gateRejected` 로 되계산하지 않는다(A-2). `add_fact` 가 동일 fact
    문자열을 dedup 하므로 그 뺄셈은 프로덕션 동작과 갈릴 수 있다.

    `resolver_dropped`/`legacy_schema_no_kind`/`band_label_rejected` 는 승격된 델타를
    `FactRecord.fact` 문자열로 역매핑해 kind·label 을 얻는다(§A-3) — 같은 fact 문자열을 내는
    델타가 여럿이면(그중 하나만 dedup 되어 store 에 남음) 첫 델타로 귀속한다(근사). 그 근사가
    실제로 발동했는지는 `fact_dedup_collapsed` 로 드러난다(§A-10) — 0 이 아니면 이 표본의
    kind 귀속은 **하한**이다(가려진 드롭이 있을 수 있다는 뜻).
    """

    session_id: str
    sample_index: int
    produced: list[ProducedTriple]
    emitted_deltas: int
    promoted_count: int
    gate_rejected: int
    resolver_dropped: list[dict[str, str]]  # [{"kind": ..., "label": ...}, ...]
    legacy_schema_no_kind: int
    fact_dedup_collapsed: int
    band_label_rejected: list[dict[str, str]]  # [{"kind": ..., "label": ...}, ...]
    latency_ms: int


@dataclass(frozen=True)
class FailureRecord:
    session_id: str
    attempt: int
    error_type: str
    message: str


@dataclass
class SessionResult:
    session_id: str
    slice_id: str
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    filled: bool = False


class _RecordingLLM:
    """`llm.complete()` 예외를 채록해 transportError(전송)와 schemaViolation(파싱)을 가른다.

    `generate_session_delta` 는 `llm.complete()` 예외를 그대로 전파하고, 성공한 응답을
    `extract_json` 으로 다시 파싱하다 실패하면 **다른** `LLMError` 를 낸다 — 둘 다 같은 예외
    타입이라 메시지로 가르면 취약하다. 대신 `complete()` 가 낸 예외 객체 자체를 들고 있다가,
    `generate_session_delta` 가 던진 예외가 **그 객체와 identity 가 같은지** 로 가른다(메시지
    매칭·문자열 파싱 없이 정확하다).
    """

    def __init__(self, delegate: LLMClient) -> None:
        self.delegate = delegate
        self.last_raw: str | None = None
        self.transport_error: BaseException | None = None

    async def complete(self, **kwargs: Any) -> str:
        try:
            raw = await self.delegate.complete(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - 전부 채록 후 그대로 재전파
            self.transport_error = exc
            raise
        self.last_raw = raw
        return raw

    def stream(self, **kwargs: Any) -> Any:
        return self.delegate.stream(**kwargs)


def _classify_error(exc: BaseException, recorder: _RecordingLLM) -> str:
    """진단 카운터 이름 — `probe_delta_prompt_356._error_signature` 와 같은 프로덕션 판별기를
    그대로 쓴다(자체 판정을 만들지 않는다)."""
    if recorder.transport_error is not None and exc is recorder.transport_error:
        if is_output_length_error(exc):
            return "transportError:outputLength"
        if is_timeout_error(exc):
            return "transportError:timeout"
        return "transportError:other"
    return "schemaViolation"


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


async def run_taste_sample(
    *,
    llm: LLMClient,
    session: GoldenSession,
    key_suffix: str,
    settings: Any,
) -> tuple[Sample | None, str | None, str | None]:
    """표본 1회 시도. 성공하면 `(Sample, None, None)`, 실패하면 `(None, errorType, message)`."""
    store = await get_profile_store()
    user_id = f"taste-probe-{session.session_id}-{key_suffix}"
    thread_key = f"taste-probe:{session.session_id}:{key_suffix}"

    for turn in session.turns:
        await store.append_session_ctx(
            thread_key,
            turn,
            cap=settings.profile_session_buffer_cap,
            repeat_cap=settings.profile_buffer_repeat_cap,
        )
    _, watermark = await store.get_session_ctx_snapshot(thread_key)

    recorder = _RecordingLLM(llm)
    started = perf_counter()
    try:
        result = await generate_session_delta(
            user_id,
            thread_key,
            profile_watermark=watermark,
            llm=recorder,
            settings=settings,
        )
    except BudgetExceeded:
        # [A-1] 런 중단 신호 — 표본 실패가 아니다. `RecordingLLM.complete` 가 provider 호출 **전에**
        # `budget.reserve()` 로 던지므로 이 예외를 여기서 삼키면 예산 소진이 매 시도
        # "transportError:other" 로 위장돼 세션마다 n×attempt_multiplier 회를 헛되이 재시도하고,
        # `cli.py` 의 `except BudgetExceeded → EXIT_BUDGET` 경로가 죽은 코드가 된다(실측 재현:
        # attempts=9, failures=9, 전부 transportError:other). `asyncio.CancelledError` 는
        # `BaseException` 이라 애초에 `except Exception` 에 안 걸린다 — 같은 성질의 다른 제어
        # 예외는 여기서만 필요하다.
        raise
    except Exception as exc:  # noqa: BLE001 - 전송·파싱 실패 전부. 표본으로 세지 않는다.
        return None, _classify_error(exc, recorder), scrub_message(str(exc))[:200]
    latency_ms = int(round((perf_counter() - started) * 1000))

    if result is None:
        # degrade(버퍼 없음/LLM 미구성) — 정상 입력이라면 나면 안 된다. 표본으로 세지 않고 재시도.
        return None, "degradedNoResult", "generate_session_delta returned None"

    # [A-2] `promoted_facts` 는 **프로덕션 반환값**이다 — `emittedDeltas - gateRejected` 로
    # 되계산하지 않는다. `add_fact` 의 fact 문자열 dedup 때문에 그 뺄셈은 실제 승격 수와 갈릴 수
    # 있다(이 하네스의 존재 이유가 "판정을 프로덕션에서 그대로 읽자"다).
    promoted_facts, _ = result

    raw = recorder.last_raw or ""
    try:
        data = extract_json(raw)
    except LLMError:
        data = {}
    valid_deltas = [
        delta
        for delta in ((data.get("deltas") if isinstance(data, dict) else None) or [])
        if isinstance(delta, dict) and str(delta.get("fact") or "").strip()
    ]

    gate_rejected = 0
    band_label_rejected: list[dict[str, str]] = []
    # [A-3] 승격된 델타를 fact 문자열로 묶어 둔다 — 아래에서 빈 트리플 레코드를 역매핑할 때 쓴다.
    deltas_by_fact: dict[str, list[dict]] = {}
    for delta in valid_deltas:
        promoted = should_promote(
            salience=_as_float(delta.get("salience")),
            explicit=bool(delta.get("explicit")),
            repetition_ema=_as_float(delta.get("repetitionEma")),
            threshold=settings.profile_gate_threshold,
        )
        if not promoted:
            gate_rejected += 1
            continue
        fact_text = str(delta.get("fact") or "").strip()
        deltas_by_fact.setdefault(fact_text, []).append(delta)
        kind = str(delta.get("kind") or "")
        if kind in _BAND_KINDS:
            label = str(delta.get("label") or "")
            clean = " ".join(label.split())[: settings.profile_graph_label_max_chars]
            if (
                _resolve_band(
                    kind, clean, anchor_phrase="taste-probe", now="1970-01-01T00:00:00+00:00"
                )
                is None
            ):
                band_label_rejected.append({"kind": kind, "label": label})

    records = await store.get_fact_records(user_id)
    # [A-10] 프로덕션 dedup 이 몇 건을 한 레코드로 합쳤는지 — 0 이 아니면 아래 kind 귀속이 근사다.
    fact_dedup_collapsed = max(len(promoted_facts) - len(records), 0)

    resolver_dropped: list[dict[str, str]] = []
    legacy_schema_no_kind = 0
    for record in records:
        if record.graph_triples:
            continue
        group = deltas_by_fact.get(record.fact) or []
        if not group:
            # 방어적 — 이론상 매칭돼야 하지만(승격 fact 는 store 에 그대로 저장됨), 매칭 안
            # 되면 귀속 정보 없이 건너뛴다(카운트에서 빠지므로 하한이 더 낮아질 수 있다).
            continue
        attributed = group[
            0
        ]  # [A-10] dedup 충돌 시 첫 델타로 귀속(근사, fact_dedup_collapsed 참조)
        kind = str(attributed.get("kind") or "")
        if not kind:
            # [A-11] 구스키마(kind 없음) 조기 반환은 `_resolve_delta` 가 "정상 경로"로 문서화한
            # 분기다(`if not delta.get("kind"): return []`) — resolver 실패가 아니다.
            legacy_schema_no_kind += 1
        else:
            label = str(attributed.get("label") or "")
            resolver_dropped.append({"kind": kind, "label": label})

    produced = [
        _to_produced_triple(triple) for record in records for triple in record.graph_triples
    ]

    sample = Sample(
        session_id=session.session_id,
        sample_index=-1,  # 호출부(run_taste_session)가 성공 개수 기준으로 채운다
        produced=produced,
        emitted_deltas=len(valid_deltas),
        promoted_count=len(promoted_facts),
        gate_rejected=gate_rejected,
        resolver_dropped=resolver_dropped,
        legacy_schema_no_kind=legacy_schema_no_kind,
        fact_dedup_collapsed=fact_dedup_collapsed,
        band_label_rejected=band_label_rejected,
        latency_ms=latency_ms,
    )
    return sample, None, None


async def run_taste_session(
    *,
    llm: LLMClient,
    session: GoldenSession,
    n: int,
    attempt_multiplier: int,
    settings: Any,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SessionResult:
    """성공 표본 N개를 채울 때까지 재시도한다(#260 규약: 실패는 표본이 아니다)."""
    result = SessionResult(session_id=session.session_id, slice_id=session.slice_id)
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        sample, error_type, message = await run_taste_sample(
            llm=llm,
            session=session,
            key_suffix=str(result.attempts),
            settings=settings,
        )
        if sample is None:
            result.failures.append(
                FailureRecord(
                    session_id=session.session_id,
                    attempt=result.attempts,
                    error_type=error_type or "unknown",
                    message=message or "",
                )
            )
            await sleep(
                min(BACKOFF_BASE_S * (2 ** max(len(result.failures) - 1, 0)), BACKOFF_MAX_S)
            )
            continue
        result.samples.append(replace(sample, sample_index=len(result.samples)))
    result.filled = len(result.samples) == n
    return result


async def run_probe(
    *,
    llm: LLMClient,
    golden_set: GoldenSet,
    n: int,
    attempt_multiplier: int,
    concurrency: int = 1,
    settings: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_session_done: Callable[[SessionResult], None] | None = None,
) -> list[SessionResult]:
    """전 세션을 돌린다. 결과는 항상 sessionId 정렬이라 동시성이 순서를 바꾸지 않는다."""
    from app.core.config import get_settings

    settings = settings if settings is not None else get_settings()
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _one(session: GoldenSession) -> SessionResult:
        async with semaphore:
            result = await run_taste_session(
                llm=llm,
                session=session,
                n=n,
                attempt_multiplier=attempt_multiplier,
                settings=settings,
                sleep=sleep,
            )
        if on_session_done is not None:
            on_session_done(result)
        return result

    results = await asyncio.gather(*(_one(session) for session in golden_set.sessions))
    return sorted(results, key=lambda result: result.session_id)


def unfilled_sessions(
    results: list[SessionResult], golden_set: GoldenSet, *, n: int
) -> list[dict[str, Any]]:
    """[A-12] 결과가 아예 없는 세션(예산 중단으로 시작도 못 한 세션)도 목록에 낸다.

    `results` 만 보면 시작도 못 한 세션이 이 목록에서도 사라져, 부분 산출이 "채워졌다"처럼
    보인다 — `golden_set` 전체와 대조해야 진짜 커버리지가 드러난다.
    """
    by_id = {result.session_id: result for result in results}
    unfilled: list[dict[str, Any]] = []
    for session in golden_set.sessions:
        result = by_id.get(session.session_id)
        if result is None:
            unfilled.append(
                {
                    "sessionId": session.session_id,
                    "got": 0,
                    "want": n,
                    "attempts": 0,
                    "errorTypes": ["notStarted"],
                }
            )
        elif not result.filled:
            unfilled.append(
                {
                    "sessionId": result.session_id,
                    "got": len(result.samples),
                    "want": n,
                    "attempts": result.attempts,
                    "errorTypes": sorted({failure.error_type for failure in result.failures}),
                }
            )
    return unfilled
