"""smart tier 1회로 필터 추출·재랭킹·추천 이유를 함께 산출하는 실험 arm."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from time import perf_counter
from typing import Any

from app.agents.buyer.recommendation.rerank import (
    _price_medians,
    _price_tier,
    _rating_tier,
    _review_tier,
)
from app.agents.buyer.recommendation.state import extract_json
from app.core.config import Settings
from app.core.llm import LLMClient
from app.schemas.spring import ProductSearchFilters, SpringProduct
from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.budget import BudgetExceeded
from evals.model_eval.recording import RecordingLLM

SINGLE_CALL_SYSTEM_PROMPT = """당신은 커머스 추천 실험용 단일 호출 모델입니다.
사용자 질의와 고정 후보 목록을 보고 검색 필터 추출, 후보 재랭킹, 상품별 추천 이유 생성을 한 번에 수행하세요.
반드시 아래 JSON 객체만 출력하세요(설명·코드펜스 금지):
{"extractedFilters": {"검색필터필드": "모델이 실제 결정한 값"}, "ranked": [{"productId": 123, "reason": "한글 40자 이내 한 문장"}]}
규칙:
- extractedFilters 키는 ProductSearchFilters의 camelCase 필드만 사용하세요: category, priceMin, priceMax, brand, ratingMin, keyword, semanticQuery, color, attrConditions.
- category·keyword·semanticQuery·color는 문자열, priceMin·priceMax·ratingMin은 숫자입니다.
- brand는 문자열 배열(예: ["농심"])이고, attrConditions는 문자열 값 객체(예: {"규격": "A4"})입니다.
- 사용자가 실제로 말했거나 질의에서 실제로 추론한 필드만 내고, 기계적 기본값 limit·excludeProductIds, null, 빈 문자열, 빈 목록은 내지 마세요.
- productId는 반드시 CANDIDATES 안의 값만 사용하고, 같은 id를 중복하지 마세요.
- 질의에 가장 적합한 순서로 정렬하고 필요한 상위 후보만 남기세요.
- ratingLevel·reviewLevel·priceLevel은 정성 등급입니다. 원시 평점·리뷰 수·가격 숫자를 지어내거나 이유에 쓰지 마세요.
- 평가없음·정보없음 등급은 데이터가 없다는 뜻이므로 해당 신호를 추천 근거로 삼지 마세요.
- reason은 후보가 실제로 가진 속성과 사용자 질의만 근거로 한 한글 40자 이내 한 문장이어야 합니다."""

_EXTRACTED_FILTER_EXCLUSIONS = frozenset({"limit", "excludeProductIds"})
_ALLOWED_FILTER_ALIASES = frozenset(
    field.alias or name for name, field in ProductSearchFilters.model_fields.items()
)


def _normalize_filter_value(field: str, value: object) -> object:
    """스키마 의미가 같은 단순 표현만 정형화한다."""
    if field == "brand" and isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    return value


def _filter_model_output(
    value: object,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """필드별 독립 검증으로 유효 필터와 드롭 warning을 함께 반환한다."""
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, [{"field": "extractedFilters", "reason": "notObject"}]

    parsed: dict[str, Any] = {}
    warnings: list[dict[str, str]] = []
    for field, raw in value.items():
        if field in _EXTRACTED_FILTER_EXCLUSIONS:
            warnings.append({"field": field, "reason": "mechanicalFieldExcluded"})
            continue
        if field not in _ALLOWED_FILTER_ALIASES:
            warnings.append({"field": str(field), "reason": "unknownField"})
            continue
        normalized = _normalize_filter_value(field, raw)
        try:
            validated = ProductSearchFilters.model_validate(
                {field: normalized}
            ).model_dump(by_alias=True, exclude_none=True)
        except (ValueError, TypeError):
            warnings.append({"field": field, "reason": "invalidTypeOrValue"})
            continue
        parsed_value = validated.get(field)
        if parsed_value is None or (
            isinstance(parsed_value, (str, list, dict)) and not parsed_value
        ):
            warnings.append({"field": field, "reason": "emptyValue"})
            continue
        parsed[field] = parsed_value
    return parsed, sorted(warnings, key=lambda warning: warning["field"])


def _candidate_payload(
    products: list[SpringProduct], settings: Settings
) -> list[dict[str, object]]:
    """원시 rating/reviewCount/price를 정성 tier로 바꾼 LLM 후보 표현을 만든다.

    [#236] priceLevel 기준 중앙값은 production `rerank()` 와 **같은 함수**로 낸다 — pipeline arm 은
    실제 `stream_recommendation` 을 돌리므로(`evals/model_eval/adapter.py`), 여기만 옛 전역
    중앙값을 쓰면 arm 간 가격 표현이 달라져 "단일 호출 vs 분리 파이프라인" 비교에 교란변수가 낀다.
    `need_of` 는 이 arm 에 니즈 개념이 없어 `None` 이며, 그 경로가 곧 category 그룹핑이다.
    """
    medians = _price_medians(products, None, settings)
    return [
        {
            "productId": product.product_id,
            "name": product.name,
            "brand": product.brand,
            "category": product.category,
            "summary": product.summary,
            "priceLevel": _price_tier(product.price, median_price, settings),
            "ratingLevel": _rating_tier(product, settings),
            "reviewLevel": _review_tier(product, settings),
        }
        for product, median_price in zip(products, medians, strict=True)
    ]


def _candidate_is_allowed(
    product_id: object, valid_ids: set[int], seen: set[int]
) -> bool:
    """후보 집합의 중복 없는 정수 id만 노출 대상으로 허용한다."""
    return (
        isinstance(product_id, int)
        and not isinstance(product_id, bool)
        and product_id in valid_ids
        and product_id not in seen
    )


class SingleCallBuyerAdapter:
    """고정 후보를 smart tier LLM 한 번으로 필터 추출과 재랭킹한다."""

    def __init__(
        self,
        llm: RecordingLLM | LLMClient,
        *,
        settings: Settings | None = None,
        model_config: dict[str, Any] | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or EvaluationSettings()
        max_tokens = (
            self.settings.rerank_max_tokens_base
            + self.settings.rerank_max_tokens_per_item * self.settings.expose_max
        )
        self.model_config = model_config or {
            "provider": "custom",
            "tier": "smart",
            "maxTokens": max_tokens,
            "purchaseHistoryIncluded": False,
        }
        self.last_output: dict[str, Any] = {}

    def __call__(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        return asyncio.run(self._run(case, fixtures))

    async def _run(self, case: GoldenCase, fixtures: EvaluationFixtures) -> dict[str, object]:
        started = perf_counter()
        call_start = len(getattr(self.llm, "calls", []))
        fixture = fixtures.search_responses.get(case.search_fixture_id or "", {})
        products = [
            SpringProduct.model_validate(fixtures.catalog[str(product_id)])
            for product_id in fixture.get("productIds", [])
            if str(product_id) in fixtures.catalog
        ]
        expected_zero_candidates = not fixture.get("productIds")
        ranked_ids: list[int] = []
        reasons: dict[str, str] = {}
        extracted_filters: dict[str, Any] = {}
        filter_parse_warnings: list[dict[str, str]] = []
        failure_reason: str | None = None
        try:
            candidates = _candidate_payload(products, self.settings)
            user = (
                f"QUERY: {case.query}\n"
                f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False, sort_keys=True)}"
            )
            max_tokens = (
                self.settings.rerank_max_tokens_base
                + self.settings.rerank_max_tokens_per_item * self.settings.expose_max
            )
            scope = (
                self.llm.scope("single_call")
                if callable(getattr(self.llm, "scope", None))
                else nullcontext()
            )
            with scope:
                raw = await self.llm.complete(
                    system=SINGLE_CALL_SYSTEM_PROMPT,
                    user=user,
                    tier="smart",
                    max_tokens=max_tokens,
                    json_output=True,
                )
            data = extract_json(raw)
            extracted_filters, filter_parse_warnings = _filter_model_output(
                data.get("extractedFilters")
            )
            valid_ids = {product.product_id for product in products}
            seen: set[int] = set()
            for item in data.get("ranked") or []:
                if not isinstance(item, dict):
                    continue
                product_id = item.get("productId")
                if not _candidate_is_allowed(product_id, valid_ids, seen):
                    continue
                seen.add(product_id)
                ranked_ids.append(product_id)
                reasons[str(product_id)] = str(item.get("reason") or "")
                if len(ranked_ids) >= self.settings.expose_max:
                    break
            if not ranked_ids and not expected_zero_candidates:
                failure_reason = "emptyPush"
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - 케이스 실패를 전체 실험과 격리
            failure_reason = f"{type(exc).__name__}:{exc}"

        calls = list(getattr(self.llm, "calls", [])[call_start:])
        if failure_reason is None and any(call.get("error") for call in calls):
            failure_reason = "providerCallFailed"
        self.last_output = {
            "rankedProductIds": ranked_ids if failure_reason is None else [],
            "extractedFilters": extracted_filters,
            "filterParseWarnings": filter_parse_warnings,
            "modelConfig": dict(self.model_config),
            "providerCalls": calls,
            "latencyMs": int(round((perf_counter() - started) * 1000)),
            "hardFailure": failure_reason is not None,
            "failureReason": failure_reason,
            "expectedZeroCandidates": expected_zero_candidates,
            "reasonsByProductId": reasons if failure_reason is None else {},
        }
        return self.last_output
