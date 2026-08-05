"""셀별 N 채우기 루프 — 두 팔(classifier/inline) 공용.

`evals/intent_probe/runner.py` 와 같은 원칙: **실패는 표본이 아니다.** 429·타임아웃·연결 오류는
버리고 다시 호출해 성공 N개를 채운다.

[TASK-3-CORRECTION] 분류기 팔의 `classify_need_priorities` 는 **전송 실패**(429·타임아웃·
`BudgetExceeded`)와 **모델이 파싱 불가 출력을 냈다**를 구분 없이 `None` 하나로 삼킨다. 초판은
이 둘을 통째로 "표본"으로 셌는데, 그러면 페이서가 조금만 어긋나도 429 가 전부 "분류기 실패"로
집계돼 #240 이 폐기한 실패 양식을 그대로 재현한다(README 「재현 함정」 1·2번) — 게다가 이 오염은
**분류기 팔만** 때려 두 팔 비교가 구조적으로 왜곡된다.

그래서 `client.RawCapture` 를 래퍼 사슬 맨 안쪽에 두고 `complete()` 자체가 예외를 던졌는지를
관측한다: `capture.last_outcome == "error"` 면 **전송 실패**(표본 아님, 재시도), 그 예외가
`BudgetExceeded` 면 예산 상한이라 밖으로 다시 던진다(재시도하면 예산 가드가 무력화된다).
`complete()` 는 성공했는데 `classify_need_priorities` 가 `None` 을 돌려줬으면 **모델 출력
문제**(진짜 표본 — 분류기의 침묵률 데이터).

인라인 팔은 이미 이 규율을 지킨다 — `decompose()` 가 예외를 던지면 `run_cell_inline` 의
`except Exception` 이 곧바로 재시도하고(전송 실패), 예외 없이 돌아온 원시 JSON 을 프로브가
직접 파싱해 leg 불일치·범위 밖 값을 표본으로 센다(모델 출력 문제). 예외/응답이 이미 구조적으로
갈려 있어 별도 관측 래퍼가 필요 없다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal

from app.agents.buyer.recommendation.decompose import decompose, normalize_category_token
from app.agents.buyer.recommendation.need_priority import classify_need_priorities
from app.agents.buyer.recommendation.state import extract_json
from app.core.llm import LLMClient
from evals.model_eval.budget import BudgetExceeded
from evals.priority_probe.client import RawCapture
from evals.priority_probe.loader import build_decompose_kwargs
from evals.priority_probe.schema import FixtureSet, PriorityCell

Arm = Literal["classifier", "inline"]

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0
_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


def backoff_seconds(failure_count: int) -> float:
    return min(BACKOFF_BASE_S * (2 ** max(failure_count - 1, 0)), BACKOFF_MAX_S)


@dataclass(frozen=True)
class Sample:
    """성공한 시도 1건 — 두 팔 공통 모양으로 정규화한 결과.

    [TASK-3-CORRECTION-2] `priorities` 는 채점의 **주 근거**(이름 매칭)다. 인라인 팔은
    `decompose()` 가 픽스처 `needs` 를 입력으로 받지 않고 **자기 leg 를 스스로 만들기 때문에**
    이름이 일치하는 leg 를 찾아 짝짓는다 — 못 찾으면 그 자리는 `None`(=인라인 안의 실제 비용).
    `priorities_by_index` 는 leg 개수가 needs 개수와 **우연히 같을 때만** 채워지는 보조 채점
    (이름이 달라도 순서 신호는 맞았는지)이고, 다르면 `None`(비교 불가, 표에서 제외).
    `raw_legs` 는 모델이 **실제로 낸 것**을 원문 그대로 담아 재집계를 가능하게 한다(#240 규약).
    분류기 팔은 `needs` 를 직접 입력받으므로 이 정합 문제 자체가 없다 — `raw_legs=()`,
    `priorities_by_index=None`, `length_mismatch=False` 고정이다.
    """

    cell_id: str
    sample_index: int
    priorities: tuple[int | None, ...]
    priorities_by_index: tuple[int | None, ...] | None
    raw_legs: tuple[tuple[Any, Any, Any], ...]
    length_mismatch: bool
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
    arm: Arm
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    filled: bool = False


def _validate_priority_value(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2, 3):
        return None
    return value


@dataclass(frozen=True)
class _ClassifierDiagnosis:
    parsed: bool
    length_mismatch: bool
    invalid_value_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed,
            "lengthMismatch": self.length_mismatch,
            "invalidValueCount": self.invalid_value_count,
            "nameUnmatchedCount": 0,  # 분류기는 needs 를 직접 입력받아 이름 매칭 문제가 없다
        }


def diagnose_classifier_raw(raw: str | None, expected_len: int) -> dict[str, Any]:
    """분류기 원시 응답을 진단만을 위해 다시 읽는다(채점에는 쓰지 않는다).

    [TASK-3-CORRECTION-2] `parsed=False` 와 `lengthMismatch=True` 는 **상호 배타적**이다 —
    파싱 자체가 실패했으면 길이를 잴 수 없으므로 lengthMismatch 는 항상 False 로 둔다(같은
    사건을 두 카운터가 동시에 세지 않게 한다).

    `classify_need_priorities` 는 실패 모드를 구분해 노출하지 않고 전부 `None` 하나로
    뭉갠다 — 여기서 원시 텍스트를 따로 파싱해 "길이가 틀렸다" 와 "개별 값이 범위 밖이다" 를
    갈라 센다. 이 진단이 채점(공식 반환값)과 어긋나도 무방하다 — 채점은 항상 공식 함수를 믿는다.
    """
    if raw is None:
        return _ClassifierDiagnosis(
            parsed=False, length_mismatch=False, invalid_value_count=0
        ).as_dict()
    try:
        obj = extract_json(raw)
    except Exception:  # noqa: BLE001 - 진단이라 관대하게 삼킨다
        return _ClassifierDiagnosis(
            parsed=False, length_mismatch=False, invalid_value_count=0
        ).as_dict()
    values = obj.get("priorities")
    if not isinstance(values, list) or len(values) != expected_len:
        return _ClassifierDiagnosis(
            parsed=True, length_mismatch=True, invalid_value_count=0
        ).as_dict()
    invalid = sum(1 for value in values if _validate_priority_value(value) is None)
    return _ClassifierDiagnosis(
        parsed=True, length_mismatch=False, invalid_value_count=invalid
    ).as_dict()


def _parse_inline_legs(raw: str | None) -> tuple[bool, list[tuple[Any, Any, Any]]]:
    """원시 응답에서 leg 목록(`category`, `query`, 원시 `priority`)을 그대로 뽑는다.

    `decompose()` 의 파서는 `priority` 를 읽지 않으므로(#281 TASK 3 지시) 이 함수가 그 파서를
    대신하지 않고 **원시 JSON** 을 직접 읽는다. 값을 정제하지 않는다 — 정제·매칭은 호출부가 한다.
    """
    if raw is None:
        return False, []
    try:
        obj = extract_json(raw)
    except Exception:  # noqa: BLE001 - 진단이라 관대하게 삼킨다
        return False, []
    queries = obj.get("categoryQueries")
    if not isinstance(queries, list):
        return False, []
    legs = [
        (entry.get("category"), entry.get("query"), entry.get("priority"))
        if isinstance(entry, dict)
        else (None, None, None)
        for entry in queries
    ]
    return True, legs


@dataclass(frozen=True)
class _NameMatchResult:
    priorities: tuple[int | None, ...]
    unmatched_count: int  # 매칭되는 leg 자체를 못 찾은 니즈 수(구조적 비용)
    invalid_value_count: int  # 매칭은 됐지만 값이 {1,2,3} 밖이었던 니즈 수(값 비용)


def _match_inline_legs_by_name(
    needs: list[str], legs: list[tuple[Any, Any, Any]]
) -> _NameMatchResult:
    """[TASK-3-CORRECTION-2] 정규화 후 정확 일치로 need ↔ leg 를 짝짓는다(주 채점 근거).

    `decompose()` 는 픽스처 `needs` 를 입력으로 받지 않고 **자기 leg 이름을 스스로 만든다** —
    그래서 개수가 같다고 위치로 짝지으면 부당하다(#281 TASK-3-CORRECTION-2). `query` 또는
    `category` 가 정규화 후(`decompose.normalize_category_token` — 공백 접기 + 소문자) 그
    니즈와 **정확히** 같은 leg 를 찾는다. **부분 문자열 매칭은 쓰지 않는다**(lessons 2026-08-02).
    leg 하나는 하나의 니즈에만 쓴다(먼저 오는 니즈가 우선).

    "매칭되는 leg 를 못 찾았다"(`unmatched_count`, 구조적 비용)와 "매칭은 됐는데 값이
    범위 밖이었다"(`invalid_value_count`, 값 비용)를 갈라 센다 — 최종 `priorities` 값은 둘 다
    `None` 으로 같지만, 원인은 다른 축이라 하나로 뭉개면 정보를 잃는다(TASK-3-CORRECTION-2 §2).
    """
    used: set[int] = set()
    matched: list[int | None] = []
    unmatched_count = 0
    invalid_value_count = 0
    for need in needs:
        need_token = normalize_category_token(need)
        found_index: int | None = None
        for index, (category, query, _raw_priority) in enumerate(legs):
            if index in used:
                continue
            leg_tokens = {normalize_category_token(query), normalize_category_token(category)}
            if need_token and need_token in leg_tokens:
                found_index = index
                break
        if found_index is None:
            matched.append(None)
            unmatched_count += 1
            continue
        used.add(found_index)
        value = _validate_priority_value(legs[found_index][2])
        if value is None:
            invalid_value_count += 1
        matched.append(value)
    return _NameMatchResult(
        priorities=tuple(matched),
        unmatched_count=unmatched_count,
        invalid_value_count=invalid_value_count,
    )


def _match_inline_legs_by_index(
    needs: list[str], legs: list[tuple[Any, Any, Any]]
) -> tuple[int | None, ...] | None:
    """[TASK-3-CORRECTION-2] 보조 채점 — leg 개수가 **우연히 같을 때만** 위치로 짝짓는다.

    "이름은 달라도 순서 신호는 맞았는가" 를 이름 매칭과 별도로 본다(요구사항 §3). 개수가
    다르면 비교 자체가 성립하지 않아 `None`(표에서 제외 — 없는 값을 0 으로 세면 거짓이 된다).
    """
    if len(legs) != len(needs):
        return None
    return tuple(_validate_priority_value(legs[i][2]) for i in range(len(needs)))


def diagnose_inline_raw(raw: str | None, needs: list[str]) -> dict[str, Any]:
    """인라인 원시 응답 진단(채점과 독립) — `unparsed`/`lengthMismatch` 는 상호 배타적이고,
    `nameUnmatchedCount`/`invalidValueCount` 는 니즈 단위로 따로 센다(TASK-3-CORRECTION-2 §2)."""
    parsed, legs = _parse_inline_legs(raw)
    if not parsed:
        return {
            "parsed": False,
            "lengthMismatch": False,
            "nameUnmatchedCount": 0,
            "invalidValueCount": 0,
        }
    length_mismatch = len(legs) != len(needs)
    name_match = _match_inline_legs_by_name(needs, legs)
    return {
        "parsed": True,
        "lengthMismatch": length_mismatch,
        "nameUnmatchedCount": name_match.unmatched_count,
        "invalidValueCount": name_match.invalid_value_count,
    }


async def run_cell_classifier(
    *,
    llm: LLMClient,
    capture: RawCapture,
    cell: PriorityCell,
    n: int,
    settings: Any,
    attempt_multiplier: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_diagnosis: Callable[[dict[str, Any]], None] | None = None,
) -> CellResult:
    """분류기 팔 — 배포와 동일하게 `classify_need_priorities` 를 그대로 부른다.

    [TASK-3-CORRECTION] `classify_need_priorities` **자신은 예외를 던지지 않는다** — 자기
    예외를 전부 삼켜 `None` 으로 떨어뜨리는 것이 그 함수의 정본 계약(need_priority.py)이다.
    그래서 이 호출을 감싼 `try/except` 는 사실상 도달하지 않고, 재시도 여부의 진짜 근거는
    **호출이 끝난 뒤** `capture.last_outcome` 를 보는 것이다:

    - `"error"` → `complete()` 자체가 예외를 던졌다(전송 실패: 429·타임아웃·연결 오류).
      `classify_need_priorities` 가 그것을 삼켜 `None` 을 돌려줬을 뿐이므로 **표본이 아니다**
      — 재시도한다. `BudgetExceeded` 는 예외로, 예산 가드가 무력화되지 않도록 재시도하지 않고
      그대로 다시 던진다.
    - `"ok"` → provider 응답은 받았는데(`capture.last_raw` 가 텍스트) `classify_need_priorities`
      가 `None` 을 돌려줬다 — **모델이 파싱 불가·형식 위반 출력을 냈다.** 이건 분류기의 침묵률
      데이터라 **표본이다.**
    """
    result = CellResult(cell_id=cell.cell_id, arm="classifier")
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        priorities = await classify_need_priorities(
            llm, message=cell.utterance, needs=cell.needs, settings=settings
        )
        if capture.last_outcome == "error":
            error = capture.last_error
            if isinstance(error, BudgetExceeded):
                raise error
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(error).__name__ if error is not None else "Unknown",
                    message=scrub_message(str(error))[:200] if error is not None else "",
                )
            )
            await sleep(backoff_seconds(len(result.failures)))
            continue
        if on_diagnosis is not None:
            on_diagnosis(diagnose_classifier_raw(capture.last_raw, len(cell.needs)))
        values = tuple(priorities) if priorities is not None else (None,) * len(cell.needs)
        result.samples.append(
            Sample(
                cell_id=cell.cell_id,
                sample_index=len(result.samples),
                priorities=values,
                priorities_by_index=None,  # 분류기는 needs 를 직접 입력받아 이 구분이 없다
                raw_legs=(),
                length_mismatch=False,  # 분류기는 구조적으로 needs 와 같은 길이만 유효 신호로 인정한다
                latency_ms=int(round((perf_counter() - started) * 1000)),
            )
        )
    result.filled = len(result.samples) == n
    return result


async def run_cell_inline(
    *,
    llm: LLMClient,
    capture: RawCapture,
    cell: PriorityCell,
    fixture: FixtureSet,
    n: int,
    tier: str,
    attempt_multiplier: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_diagnosis: Callable[[dict[str, Any]], None] | None = None,
) -> CellResult:
    """인라인 팔 — `decompose()` 를 후보 프롬프트로 부르고, 원시 응답에서 priority 를 읽는다.

    [TASK-3-CORRECTION 확인] `decompose()` 는 `classify_need_priorities` 와 달리 **자기 예외를
    삼키지 않는다**(`app/agents/buyer/recommendation/decompose.py` 에 `llm.complete()` 를 감싸는
    try/except 가 없다) — 그래서 전송 실패(429 등)는 여기서 바로 예외로 잡히고(표본 아님, 재시도),
    예외 없이 돌아온 원시 JSON 은 아래에서 직접 파싱해 leg 불일치·범위 밖 값을 표본으로 센다.
    분류기 팔처럼 관측 래퍼로 우회할 필요가 없다 — 예외/응답이 이미 구조적으로 갈려 있다.
    """
    result = CellResult(cell_id=cell.cell_id, arm="inline")
    max_attempts = n * attempt_multiplier
    kwargs = build_decompose_kwargs(fixture.channel)
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        try:
            await decompose(llm, query=cell.utterance, tier=tier, **kwargs)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - provider·파싱 실패 전부 표본이 아니다(전송 실패)
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            await sleep(backoff_seconds(len(result.failures)))
            continue
        if on_diagnosis is not None:
            on_diagnosis(diagnose_inline_raw(capture.last_raw, cell.needs))
        parsed, legs = _parse_inline_legs(capture.last_raw)
        name_match = _match_inline_legs_by_name(cell.needs, legs) if parsed else None
        result.samples.append(
            Sample(
                cell_id=cell.cell_id,
                sample_index=len(result.samples),
                priorities=(
                    name_match.priorities if name_match is not None else (None,) * len(cell.needs)
                ),
                priorities_by_index=(
                    _match_inline_legs_by_index(cell.needs, legs) if parsed else None
                ),
                raw_legs=tuple(legs),
                length_mismatch=parsed and len(legs) != len(cell.needs),
                latency_ms=int(round((perf_counter() - started) * 1000)),
            )
        )
    result.filled = len(result.samples) == n
    return result


async def run_probe(
    *,
    arm: Arm,
    llm: LLMClient,
    capture: RawCapture,
    cells: list[PriorityCell],
    fixture: FixtureSet,
    n: int,
    tier: str,
    attempt_multiplier: int,
    concurrency: int = 1,
    settings: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_cell_done: Callable[[CellResult], None] | None = None,
    on_diagnosis: Callable[[dict[str, Any]], None] | None = None,
) -> list[CellResult]:
    """모든 셀을 돌린다. 결과는 항상 cellId 정렬 — 동시성이 순서를 바꾸지 않는다."""
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _one(cell: PriorityCell) -> CellResult:
        async with semaphore:
            if arm == "classifier":
                result = await run_cell_classifier(
                    llm=llm,
                    capture=capture,
                    cell=cell,
                    n=n,
                    settings=settings,
                    attempt_multiplier=attempt_multiplier,
                    sleep=sleep,
                    on_diagnosis=on_diagnosis,
                )
            else:
                result = await run_cell_inline(
                    llm=llm,
                    capture=capture,
                    cell=cell,
                    fixture=fixture,
                    n=n,
                    tier=tier,
                    attempt_multiplier=attempt_multiplier,
                    sleep=sleep,
                    on_diagnosis=on_diagnosis,
                )
        if on_cell_done is not None:
            on_cell_done(result)
        return result

    results = await asyncio.gather(*(_one(cell) for cell in cells))
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
