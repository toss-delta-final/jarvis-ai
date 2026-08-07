"""union 측정 모드 — decompose 직후 판정과 프로덕션 판정 사이의 카테고리 매핑·`needs_expansion`
보정 단계를 실제로 태워 "전개 후 판정" 을 잰다(#432).

`app.agents.buyer.graph._prepare_recommendation` 을 **그대로** 부른다 — 매핑·전개·합집합·
`filters.category` 확정 순서를 이 파일에 옮겨 적지 않는다(#380 이 decompose 판정식을 복제하지
않은 것과 같은 규약, packet-432.md §2-2). 기본은 off — `--union` 이 켜졌을 때만 쓰인다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.agents.buyer import graph as buyer_graph
from app.agents.buyer.graph import (
    ThreadFilterStore,
    _PrepareRecommendationOut,
    _prepare_recommendation,
)
from app.agents.buyer.recommendation.state import RouteDecision
from app.agents.buyer.recommendation.underspecified import is_underspecified_turn
from app.core.config import Settings
from app.schemas.spring import ProductSearchFilters
from evals.underspecified_probe.runner import (
    JudgmentSettings,
    _clone_decision,
    nonblank_blocking_axes,
    scrub_message,
)

# [§2-4] `_prepare_recommendation` 내부의 `detect_expansion_need` 호출은 사유를 지역 변수로만
# 들고 반환값을 밖으로 노출하지 않는다. 그 사유를 다시 계산하려면 그 앞의 `map_categories` 호출도
# 다시 해야 하는데, 그러면 carry/clear 분기(이 함수가 호출조차 되지 않는 경로)까지 판단해야 해서
# §2-2 "순서를 하네스에 옮겨 적지 마라" 규약을 어긴다. 대신 **호출을 가로채지 않고 그대로
# 위임하는 spy** 로 실제 반환값만 관측한다 — 동작은 원본과 100% 동일하고 관측만 곁다리로 붙는다.
# 전역 함수 하나(모듈 속성)를 패치하는 동안에는 **동시에 하나의 union 호출만** 진행해야 한다 —
# 그래서 이 모듈 전체에서 공유하는 잠금 하나로 순회를 직렬화한다(decompose 단계의 동시성
# `--concurrency` 는 그대로 두고, union 부가 단계만 이 잠금 안에서 순차 실행된다).
_UNION_STAGE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class UnionSampleResult:
    """표본 1건의 union(전개 후) 판정 결과.

    `stage_error` 가 있으면 나머지 필드는 참고용도 아니다 — union 축 **분모에서 이 표본을
    제외**하는 신호로만 쓴다(§2-6 항목 2, 표본 자체는 버리지 않는다).
    """

    verdict: bool | None
    mapped_leg_count: int | None
    category_expanded: bool | None
    filters_category: str | None
    expansion_reason: str | None
    blocking_axes: list[str]
    stage_latency_ms: int
    stage_error: str | None

    @property
    def ok(self) -> bool:
        return self.stage_error is None


def skipped_union_result(reason: str) -> UnionSampleResult:
    """`--dry-run --union` 전용 — 실 pg·임베딩 호출 없이 표본을 "건너뜀"으로 기록한다(§2-6 항목5:
    dry-run 은 pg 접근이 0 이어야 한다)."""
    return UnionSampleResult(
        verdict=None,
        mapped_leg_count=None,
        category_expanded=None,
        filters_category=None,
        expansion_reason=None,
        blocking_axes=[],
        stage_latency_ms=0,
        stage_error=reason,
    )


UNION_TUNABLE_FIELDS = (
    "needs_expansion_enabled",
    "category_expand_enabled",
    "category_fanout_max",
    "category_top_k",
    "category_distance_max",
    "category_distance_override_margin",
    "category_select_max_calls",
    "category_expand_legs",
    "category_scope_classifier_enabled",
    # [F-2, 리뷰 findings-432-r1] 사용처를 호출 사슬로 직접 확인해 추가했다 — 전부 union 단계가
    # 실제로 부르는 함수 안에서 settings 로 읽힌다:
    "category_select_margin_max",  # category_mapping.py::map_categories — §4.4 택일 LLM 발동 마진
    "embedding_task_query",  # category_mapping.py::map_categories — 앵커 임베딩 task_type
    "needs_expansion_tier",  # needs_expansion.py::expand_needs — 전개 모델 티어
    "needs_expansion_min_items",  # needs_expansion.py::expand_needs — 전개 성공/실패 판정 임계
    "embedding_model_id",  # app/pipelines/embedding.py::embed_texts — 임베딩 모델(run_manifest
    # 에는 이미 별도 필드로도 기록되지만, `Settings` 기본값이 있어 튜너블 드리프트 감지에도 넣는다)
)


class UnionPreflightError(RuntimeError):
    """`--union` 사전 거부 사유 — 호출부가 종료 코드 2 로 번역한다(§2-6 항목5)."""


def preflight_catalog(dsn: str) -> dict[str, int]:
    """`categories` 에 `embedding` 이 채워진 행이 1개 이상인지 LLM 콜 이전에 확인한다.

    `evals/category_probe/loader.py::preflight_check_catalog` 는 앵커의 accept 라벨을 검증하는
    함수라 이 하네스에는 재사용할 수 없다(이 하네스는 그런 라벨이 없다, §2-6 항목5) — 필요한
    검사는 훨씬 단순하다.
    """
    import psycopg  # noqa: PLC0415 - LAZY(유닛 테스트가 pg 없이 이 모듈을 임포트할 수 있게)

    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM categories WHERE embedding IS NOT NULL")
            row = cur.fetchone()
            categories_with_embedding = row[0] if row else 0
            cur.execute("SELECT count(*) FROM product_document")
            row = cur.fetchone()
            product_document_count = row[0] if row else 0
    except Exception as exc:  # noqa: BLE001 - 연결 실패도 pre-flight 거절 사유다
        raise UnionPreflightError(f"pg-catalog 연결 실패(dsn={dsn}): {exc}") from exc
    if categories_with_embedding < 1:
        raise UnionPreflightError(
            f"categories 사전에 embedding 이 채워진 행이 없습니다(dsn={dsn}) — pg-catalog 가 "
            "안 붙었거나 시드가 비었습니다"
        )
    return {
        "categoriesWithEmbedding": categories_with_embedding,
        "productDocumentCount": product_document_count,
    }


def union_tunable_report(settings: Settings) -> dict[str, Any]:
    """[§2-6 항목3] union 경로가 읽는 튜너블 전부의 실제 값 + 기본값과 다른 것의 목록.

    `.env` 가 측정 대상을 조용히 바꾸는 것을 막는다 — 다른 것이 있으면 호출부가 stderr 경고를
    찍는다."""
    defaults = {name: Settings.model_fields[name].default for name in UNION_TUNABLE_FIELDS}
    actual = {name: getattr(settings, name) for name in UNION_TUNABLE_FIELDS}
    differs = sorted(name for name in UNION_TUNABLE_FIELDS if actual[name] != defaults[name])
    return {"actual": actual, "defaults": defaults, "differsFromDefault": differs}


async def run_union_stage(
    *,
    decision: RouteDecision,
    prior: ProductSearchFilters | None,
    utterance: str,
    llm: Any,
    settings: Settings,
    judgment_settings: JudgmentSettings,
    thread_key: str,
    map_categories: Any = None,
    expand_needs: Any = None,
) -> UnionSampleResult:
    """`_prepare_recommendation` 을 **사본**에서 그대로 부른다(§2-3).

    원본 `decision` 은 decompose 단계 축·ablation(§D11)·`blockingAxes`·`flagOffInvariant` 의
    근거라 절대 제자리 변경하지 않는다 — `runner._clone_decision`(pydantic 필터·리스트 필드까지
    새로 만드는 깊은 사본)을 그대로 재사용한다.

    실패해도 예외를 밖으로 던지지 않는다 — `stage_error` 에 담아 표본을 살린다(§2-6 항목 2:
    이 표본은 union 축 분모에서만 빠지고 decompose 단계 표본으로는 그대로 산다).

    `map_categories`/`expand_needs` 는 **실 실행에서는 항상 `None`**(=프로덕션 기본, §2-2 인자표)
    이다 — CLI 는 이 값을 절대 넘기지 않는다. 유닛 테스트가 pg·임베딩 없이 §4 (c)/(d)/(f) 를
    결정론으로 고정하기 위해서만 주입한다(`_prepare_recommendation` 자신이 이미 지원하는 주입
    지점을 그대로 노출할 뿐, 순서를 이 파일에 새로 옮겨 적지 않는다).
    """
    clone = _clone_decision(decision)
    request = SimpleNamespace(message=utterance)
    started = perf_counter()
    captured: dict[str, Any] = {"reason": None, "called": False}
    real_detect_expansion_need = buyer_graph.detect_expansion_need

    def _spy_detect_expansion_need(category_queries, *, case, unresolved):
        captured["called"] = True
        reason = real_detect_expansion_need(category_queries, case=case, unresolved=unresolved)
        captured["reason"] = reason
        return reason

    try:
        async with _UNION_STAGE_LOCK:
            with patch.object(buyer_graph, "detect_expansion_need", _spy_detect_expansion_need):
                out = _PrepareRecommendationOut()
                async for _ in _prepare_recommendation(
                    request=request,
                    decision=clone,
                    prior=prior,
                    llm=llm,
                    settings=settings,
                    map_categories=map_categories,
                    expand_needs=expand_needs,
                    observer=None,
                    thread_store=ThreadFilterStore(),
                    thread_key=thread_key,
                    out=out,
                    # [§2-2] 이 하네스의 prior 는 category 를 들고 있지 않아(§D5) 프로덕션에서도
                    # `scope_gate`(`run_buyer_turn`) 가 항상 거짓이다 — 분류기가 안 도는 것이
                    # 정직한 재현이다.
                    scope_free=None,
                ):
                    pass
    except Exception as exc:  # noqa: BLE001 - union 단계 실패는 표본을 죽이지 않는다(§2-6 항목 2)
        return UnionSampleResult(
            verdict=None,
            mapped_leg_count=None,
            category_expanded=None,
            filters_category=None,
            expansion_reason=None,
            blocking_axes=[],
            stage_latency_ms=int(round((perf_counter() - started) * 1000)),
            stage_error=f"{type(exc).__name__}: {scrub_message(str(exc))[:200]}",
        )

    verdict = is_underspecified_turn(clone, prior, judgment_settings)
    return UnionSampleResult(
        verdict=verdict,
        mapped_leg_count=len(clone.category_legs),
        category_expanded=clone.category_expanded,
        filters_category=clone.filters.category,
        expansion_reason=captured["reason"] if captured["called"] else None,
        blocking_axes=nonblank_blocking_axes(clone),
        stage_latency_ms=int(round((perf_counter() - started) * 1000)),
        stage_error=None,
    )
