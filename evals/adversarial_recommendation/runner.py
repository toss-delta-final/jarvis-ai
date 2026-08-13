"""고정 후보를 실제 구매자 추천 경로에 주입하는 adversarial 평가 runner."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import asdict
from time import perf_counter
from typing import Any, Literal
from unittest.mock import patch

import httpx

from app.agents.buyer.recommendation.decompose import decompose
import app.agents.buyer.recommendation.graph as recommendation_graph
from app.agents.buyer.recommendation.graph import stream_recommendation
from app.agents.buyer.recommendation.rerank_grounding import GroundingArm
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.auth import Identity
from app.core.config import Settings, get_settings
from app.core.llm import LLMClient, get_llm, resolve_provider_model
from app.schemas.chat import BuyerChatRequest
from app.services import search_service, spring_client
from evals.adversarial_recommendation.schema import EvalCase, NumericConstraint
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.recording import RecordingLLM

RunMode = Literal["scripted", "live"]
_INTERNAL_TOKEN = "adversarial-eval-internal-token"


def _replace_rendered_reasons(body: object, rendered: dict[int, str]) -> None:
    if not isinstance(body, dict):
        return
    lists = body.get("lists")
    if not isinstance(lists, list):
        return
    for entry in lists:
        if not isinstance(entry, dict) or not isinstance(entry.get("reasons"), list):
            continue
        for reason in entry["reasons"]:
            if not isinstance(reason, dict):
                continue
            product_id = reason.get("productId")
            if isinstance(product_id, int) and product_id in rendered:
                reason["reason"] = rendered[product_id]


def derive_validated_execution(prompt_only: dict[str, Any]) -> dict[str, Any]:
    """Derive C from B so validator lift is not confounded by another LLM call."""
    if prompt_only.get("groundingArm") != "prompt_only":
        raise ValueError("validated derivation requires a prompt_only execution")
    validated = deepcopy(prompt_only)
    rendered = {
        int(decision["productId"]): str(decision["renderedRationale"])
        for decision in validated.get("groundingDecisions", [])
        if isinstance(decision, dict)
        and isinstance(decision.get("productId"), int)
        and isinstance(decision.get("renderedRationale"), str)
    }
    reasons = validated.get("reasons")
    if isinstance(reasons, dict):
        for product_id, rationale in rendered.items():
            key = str(product_id)
            if key in reasons:
                reasons[key] = rationale
    _replace_rendered_reasons(validated.get("pushBody"), rendered)
    for request in validated.get("requests", []):
        if isinstance(request, dict) and request.get("path") == "/internal/recommendations":
            _replace_rendered_reasons(request.get("body"), rendered)
    validated["groundingArm"] = "validated"
    validated["derivedFromArm"] = "prompt_only"
    validated["providerCalls"] = []
    validated["latencyMs"] = None
    return validated


def _normalized(value: object) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _filter_candidates(
    candidates: list[dict[str, Any]], params: httpx.QueryParams
) -> list[dict[str, Any]]:
    """Spring I-1이 실제로 받는 쿼리 축을 fixture 위에서 엄격하게 모사한다."""
    minimum = params.get("minPrice")
    maximum = params.get("maxPrice")
    category = _normalized(params.get("categoryName"))
    keyword = _normalized(params.get("keyword"))
    brands = {_normalized(value) for value in params.get_list("brandName") if _normalized(value)}
    colors = [_normalized(value) for value in params.get_list("color") if _normalized(value)]
    filtered: list[dict[str, Any]] = []
    for candidate in candidates:
        price = candidate.get("price")
        if minimum is not None and (not isinstance(price, int | float) or price < int(minimum)):
            continue
        if maximum is not None and (not isinstance(price, int | float) or price > int(maximum)):
            continue
        if category and _normalized(candidate.get("categoryName")) != category:
            continue
        if brands and _normalized(candidate.get("brandName")) not in brands:
            continue
        if keyword and keyword not in _normalized(candidate.get("name")):
            continue
        if colors:
            attributes = candidate.get("attributes")
            color = _normalized(attributes.get("색상")) if isinstance(attributes, dict) else ""
            if not color or not any(requested in color for requested in colors):
                continue
        filtered.append(candidate)
    return filtered


class CaseTransport:
    """한 case의 I-1/I-19/I-21을 제공하고 모든 경계 요청을 채록한다."""

    def __init__(self, case: EvalCase, *, internal_token: str) -> None:
        self.case = case
        self.internal_token = internal_token
        self.requests: list[dict[str, Any]] = []
        self.push_body: dict[str, Any] | None = None

    @property
    def ranked_product_ids(self) -> list[int]:
        return [
            int(product_id)
            for entry in (self.push_body or {}).get("lists", [])
            for product_id in entry.get("productIds", [])
        ]

    @property
    def reasons(self) -> dict[str, str]:
        return {
            str(reason["productId"]): str(reason["reason"])
            for entry in (self.push_body or {}).get("lists", [])
            for reason in entry.get("reasons", [])
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": {
                    key: request.url.params.get_list(key) for key in request.url.params.keys()
                },
                "body": body,
            }
        )
        if request.headers.get("X-Internal-Token") != self.internal_token:
            return httpx.Response(401, json={"success": False})
        if request.method == "GET" and request.url.path == "/internal/products/search":
            products = _filter_candidates(self.case.candidates, request.url.params)
            return httpx.Response(200, json={"success": True, "data": products})
        if request.method == "GET" and request.url.path.startswith("/internal/members/"):
            return httpx.Response(200, json={"success": True, "data": {"orders": []}})
        if request.method == "POST" and request.url.path == "/internal/recommendations":
            self.push_body = body or {}
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(404, json={"success": False})


def _inclusive_bound(constraint: NumericConstraint) -> int | float:
    threshold = constraint.threshold
    if constraint.operator == "gt":
        return threshold + 1 if isinstance(threshold, int) else math.nextafter(threshold, math.inf)
    if constraint.operator == "lt":
        return threshold - 1 if isinstance(threshold, int) else math.nextafter(threshold, -math.inf)
    return threshold


def _scripted_filters(case: EvalCase) -> tuple[dict[str, Any], list[str]]:
    filters: dict[str, Any] = {}
    unapplied: list[str] = []
    for constraint in case.oracle.deterministic.constraints:
        field = constraint.candidate_field
        value = _inclusive_bound(constraint)
        if field == "price":
            if constraint.operator in {"ge", "gt", "eq"}:
                filters["priceMin"] = value
            if constraint.operator in {"le", "lt", "eq"}:
                filters["priceMax"] = value
        elif field == "rating" and constraint.operator in {"ge", "gt", "eq"}:
            filters["ratingMin"] = value
        else:
            unapplied.append(f"{field}:{constraint.operator}:{constraint.threshold}")
    return filters, unapplied


class ScriptedCaseLLM:
    """Oracle 숫자조건만 구조화하고 검색 결과 순서를 유지하는 결정론 LLM."""

    def __init__(self, case: EvalCase) -> None:
        filters, unapplied = _scripted_filters(case)
        self.filters = filters
        self.unapplied_constraints = unapplied
        self.calls: list[dict[str, Any]] = []
        self._decompose = {
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "semanticQuery": case.user_request["message"],
            "categoryQueries": [],
            "filters": filters,
            "cart": {"productId": None, "optionId": None, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
        }

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        del max_tokens, json_output
        if tier == "fast":
            payload = self._decompose
        else:
            marker = "CANDIDATES: "
            raw_candidates = user.split(marker, 1)[1] if marker in user else "[]"
            candidates = json.loads(raw_candidates)
            scored = "커머스 추천 평가기" in system
            structured = "reasonCode" in system and "evidenceFields" in system
            ranked = []
            for index, candidate in enumerate(candidates):
                item = {
                    "productId": candidate["productId"],
                    "rationale": "후보 데이터 기반 추천",
                }
                if structured:
                    item.update(
                        {
                            "reasonCode": "NO_VERIFIABLE_EVIDENCE",
                            "evidenceFields": [],
                        }
                    )
                if scored:
                    target_score = max(0, 22 - index * 2)
                    intent_fit = min(4, target_score // 4)
                    item.update(
                        {
                            "intentFit": intent_fit,
                            "needFit": min(3, (target_score - intent_fit * 4) // 2),
                            "profileFit": 0,
                        }
                    )
                ranked.append(item)
            payload = {
                "evaluations" if scored else "ranked": ranked,
                "overallComment": "후보 데이터 기반 결과입니다.",
            }
        self.calls.append({"tier": tier, "error": None})
        return json.dumps(payload, ensure_ascii=False)

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        del system, user, tier, max_tokens
        yield "후보 데이터 기반 결과입니다."


def _parse_frame(frame: str) -> tuple[str | None, dict[str, Any]]:
    line = next((line for line in frame.splitlines() if line.startswith("data: ")), "")
    if not line:
        return None, {}
    payload = json.loads(line.removeprefix("data: "))
    return payload.get("type"), payload.get("data") or {}


class AdversarialBuyerRunner:
    """실제 decompose→search→rerank→push 경로를 case 단위로 실행한다."""

    def __init__(
        self,
        *,
        mode: RunMode,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
        model_config: dict[str, Any] | None = None,
        grounding_arm: GroundingArm = "current",
    ) -> None:
        if mode == "live" and llm is None:
            raise ValueError("live mode에는 LLM client가 필요합니다")
        self.mode = mode
        self.llm = llm
        self.settings = settings or EvaluationSettings(
            auth_mode="dev",
            app_environment="test",
            internal_api_token=_INTERNAL_TOKEN,
            search_backend="spring",
        )
        self.model_config = model_config or {"provider": mode, "searchBackend": "spring"}
        self.grounding_arm = grounding_arm
        self.decompose_decisions: dict[str, RouteDecision] = {}

    async def run(
        self,
        case: EvalCase,
        *,
        decision_override: RouteDecision | None = None,
        decompose_source_arm: GroundingArm | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        llm = ScriptedCaseLLM(case) if self.mode == "scripted" else self.llm
        assert llm is not None
        call_start = len(getattr(llm, "calls", []))
        transport = CaseTransport(case, internal_token=self.settings.internal_api_token)
        events: list[str] = []
        sse_frames: list[dict[str, Any]] = []
        token_text: list[str] = []
        errors: list[dict[str, Any]] = []
        extracted_filters: dict[str, Any] = {}
        grounding_decisions: list[dict[str, Any]] = []
        failure_reason: str | None = None

        original_rerank = recommendation_graph.rerank

        async def _rerank_with_arm(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            kwargs["grounding_arm"] = self.grounding_arm
            result = await original_rerank(*args, **kwargs)
            grounding_decisions.extend(asdict(decision) for decision in result.grounding_decisions)
            return result

        def _client(*, timeout: float | None = None) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=self.settings.spring_base_url,
                timeout=timeout or self.settings.spring_timeout_s,
                headers={"X-Internal-Token": self.settings.internal_api_token},
                transport=httpx.MockTransport(transport.handler),
            )

        async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
            return await search_service.search_catalog(
                filters,
                exclude_product_ids,
                backend=search_service.SpringSearchBackend(),
            )

        async def _popular(_size: int):  # noqa: ANN202
            # production graph가 I-3 후보원을 고르는 턴도 평가에서는 같은 고정 후보 universe를
            # 써야 한다. direct list 반환 대신 실제 search_catalog→Spring I-1 adapter를 통과시켜
            # 모든 case가 동일한 schema parsing/AI-side filtering 경계를 실행하게 한다.
            return await _search(decision.filters)

        try:
            request = BuyerChatRequest.model_validate(case.user_request)
            decision = (
                deepcopy(decision_override)
                if decision_override is not None
                else await decompose(
                    llm,
                    query=request.message,
                    prior_filters=None,
                    profile_summary=None,
                    tier="fast",
                    category_fanout_max=self.settings.category_fanout_max,
                    repurchase_max=self.settings.dedup_repurchase_max,
                    screen=request.screen,
                )
            )
            self.decompose_decisions[case.case_id] = deepcopy(decision)
            extracted_filters = decision.filters.model_dump(by_alias=True, exclude_none=True)
            if decision.intent != "recommend":
                failure_reason = f"nonRecommendIntent:{decision.intent}"
            else:
                patches = (
                    patch.object(spring_client, "_client", _client),
                    patch.object(spring_client, "get_settings", return_value=self.settings),
                    patch(
                        "app.agents.buyer.recommendation.rerank.get_settings",
                        return_value=self.settings,
                    ),
                    patch.object(recommendation_graph, "rerank", _rerank_with_arm),
                )
                with ExitStack() as stack:
                    for active_patch in patches:
                        stack.enter_context(active_patch)
                    async for frame in stream_recommendation(
                        request=request,
                        decision=decision,
                        llm=llm,
                        search=_search,
                        push_fn=spring_client.push_recommendations,
                        popular_fn=_popular,
                        identity=Identity(user_id=None, is_guest=True, seller_id=None),
                        profile=None,
                        settings=self.settings,
                        request_id=f"adv-eval-{case.case_id}",
                    ):
                        event_type, data = _parse_frame(frame)
                        if event_type:
                            events.append(event_type)
                            sse_frames.append({"type": event_type, "data": data})
                        if event_type == "token" and isinstance(data.get("text"), str):
                            token_text.append(data["text"])
                        if event_type == "error":
                            errors.append(data)
        except Exception as exc:  # noqa: BLE001 - case 실패는 전체 run과 격리한다.
            failure_reason = f"{type(exc).__name__}:{exc}"

        if failure_reason is None and errors:
            failure_reason = f"pipelineError:{errors[0]}"
        calls = list(getattr(llm, "calls", [])[call_start:])
        return {
            "caseId": case.case_id,
            "mode": self.mode,
            "groundingArm": self.grounding_arm,
            "decomposeSourceArm": decompose_source_arm,
            "groundingDecisions": [
                {
                    "productId": decision["product_id"],
                    "requestedReasonCode": decision["requested_reason_code"],
                    "evidenceFields": decision["evidence_fields"],
                    "modelRationale": decision["model_rationale"],
                    "renderedRationale": decision["rendered_rationale"],
                    "supported": decision["supported"],
                    "downgraded": decision["downgraded"],
                    "failureReason": decision["failure_reason"],
                }
                for decision in grounding_decisions
            ],
            "rankedProductIds": transport.ranked_product_ids,
            "reasons": transport.reasons,
            "pushBody": transport.push_body,
            "extractedFilters": extracted_filters,
            "unappliedConstraints": list(getattr(llm, "unapplied_constraints", [])),
            "events": events,
            "sseFrames": sse_frames,
            "tokenText": "".join(token_text),
            "requests": transport.requests,
            "providerCalls": calls,
            "modelConfig": dict(self.model_config),
            "latencyMs": int(round((perf_counter() - started) * 1000)),
            "hardFailure": failure_reason is not None,
            "failureReason": failure_reason,
        }


def build_live_runner(*, grounding_arm: GroundingArm = "current") -> AdversarialBuyerRunner:
    """현재 배포 설정의 provider/model로 고정-candidate live runner를 만든다."""
    runtime = get_settings()
    delegate = get_llm()
    if delegate is None:
        raise ValueError("live mode LLM이 구성되지 않았습니다. provider API key를 확인하세요")
    fast = resolve_provider_model(runtime, "fast")
    smart = resolve_provider_model(runtime, "smart")
    llm = RecordingLLM(
        delegate,
        models={"fast": fast.model_id, "smart": smart.model_id},
        reasoning_efforts={
            "fast": fast.reasoning_effort,
            "smart": smart.reasoning_effort,
        },
    )
    settings = runtime.model_copy(
        update={
            "auth_mode": "dev",
            "internal_api_token": _INTERNAL_TOKEN,
            "search_backend": "spring",
        }
    )
    model_config = {
        "provider": fast.provider,
        "searchBackend": "spring",
        "tiers": {
            "fast": {"model": fast.model_id, "reasoningEffort": fast.reasoning_effort},
            "smart": {"model": smart.model_id, "reasoningEffort": smart.reasoning_effort},
        },
    }
    return AdversarialBuyerRunner(
        mode="live",
        llm=llm,
        settings=settings,
        model_config=model_config,
        grounding_arm=grounding_arm,
    )
