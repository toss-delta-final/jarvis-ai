"""실 LLM 개인화 arm과 scope gate를 기존 LiveBuyerAdapter에 결합한다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal
from unittest.mock import patch

from app.agents.buyer.recommendation.decompose import _filter_axes
from app.core.config import Settings
from app.schemas.spring import ProductSearchFilters
from evals.goldenset.schema import Identity
from evals.metrics.runner import EvaluationFixtures
from evals.model_eval.adapter import LiveBuyerAdapter
from evals.model_eval.budget import BudgetLimits, estimate_calls, preflight
from evals.model_eval.pricing import PriceBook
from evals.metrics.settings import EvaluationSettings

ProfileScope = Literal["off", "rerank_only", "both"]


def profile_for_scope(
    profile_markdown: str | None, scope: ProfileScope
) -> tuple[str | None, str | None]:
    """graph.py와 같은 decompose/rerank profile 주입 게이트."""
    return (
        profile_markdown if scope == "both" else None,
        None if scope == "off" else profile_markdown,
    )


def filter_axis_leakage(baseline_filters: dict[str, Any], arm_filters: dict[str, Any]) -> list[str]:
    """값을 기록하지 않고 arm에만 새로 생긴 필터 축 이름을 반환한다.

    [#483] 인자 이름이 guest/member 였던 건 기준선이 `guest` 로 하드코딩돼 있던 시절의 흔적이다.
    기준선은 이제 호출부가 정한다(`cli.annotate_axis_metrics`).
    """
    baseline_axes = set(_filter_axes(ProductSearchFilters.model_validate(baseline_filters)))
    arm_axes = set(_filter_axes(ProductSearchFilters.model_validate(arm_filters)))
    return sorted(arm_axes - baseline_axes)


class PersonalizationLiveBuyerAdapter:
    """LiveBuyerAdapter 호출을 감싸 profile과 identity arm만 주입한다."""

    def __init__(
        self,
        llm,
        *,
        profile_markdown: str | None,
        identity_kind: Literal["guest", "member"],
        settings,
        model_config: dict[str, Any] | None = None,
        markdown_resolver: Callable[[object, EvaluationFixtures], str | None] | None = None,
    ) -> None:
        self.profile_markdown = profile_markdown
        # [#484] 케이스별 프로필. 생성자 고정값만 있으면 arm 하나가 dev 전 케이스에 같은
        # 문자열을 먹인다 — Tier D 의 `preference_resolver` 와 같은 콜백 규약을 쓴다.
        self.markdown_resolver = markdown_resolver
        self.identity_kind = identity_kind
        self.settings = settings
        self.adapter = LiveBuyerAdapter(llm, settings=settings, model_config=model_config)
        self.llm = llm
        self.model_config = {
            **self.adapter.model_config,
            "identityKind": identity_kind,
            "profileScope": settings.profile_injection_scope,
        }
        self.last_output: dict[str, Any] = {}

    def __call__(self, case, fixtures):
        from evals.model_eval import adapter as live_module

        markdown = (
            self.markdown_resolver(case, fixtures)
            if self.markdown_resolver is not None
            else self.profile_markdown
        )
        decompose_profile, rerank_profile = profile_for_scope(
            markdown, self.settings.profile_injection_scope
        )
        original_decompose = live_module.decompose
        original_stream = live_module.stream_recommendation

        async def scoped_decompose(*args, **kwargs):
            kwargs["profile_summary"] = decompose_profile
            return await original_decompose(*args, **kwargs)

        async def scoped_stream(*args, **kwargs):
            kwargs["profile"] = rerank_profile
            async for event in original_stream(*args, **kwargs):
                yield event

        identity = Identity(
            kind=self.identity_kind,
            persona_id=None if self.identity_kind == "guest" else case.identity.persona_id,
        )
        scoped_case = case.model_copy(update={"identity": identity})
        with (
            patch.object(live_module, "decompose", scoped_decompose),
            patch.object(live_module, "stream_recommendation", scoped_stream),
        ):
            output = self.adapter(scoped_case, fixtures)
        output["modelConfig"] = dict(self.model_config)
        self.adapter.last_output["modelConfig"] = dict(self.model_config)
        self.last_output = self.adapter.last_output
        return output


def estimate_live_matrix(
    *,
    case_count: int,
    arm_count: int,
    repeats: int,
    model: str,
    input_tokens_per_call: int = 2000,
    output_tokens_per_call: int = 500,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """arm 배수를 반영한 call 수와 PriceBook 기준 보수적 비용 표를 만든다."""
    resolved = settings or EvaluationSettings()
    limits = BudgetLimits(
        max_calls=resolved.model_eval_max_calls_per_run,
        max_total_tokens=resolved.model_eval_max_total_tokens_per_run,
        max_cost_usd=resolved.model_eval_max_cost_usd_per_run,
    )
    base = preflight(
        case_count=case_count * arm_count,
        repeats=repeats,
        limits=limits,
    )
    calls = estimate_calls(case_count=case_count * arm_count, repeats=repeats)
    per_call = PriceBook.load().cost(
        model=model,
        input_tokens=input_tokens_per_call,
        output_tokens=output_tokens_per_call,
    )
    return {
        **base,
        "caseCount": case_count,
        "armCount": arm_count,
        "combinations": case_count * arm_count * repeats,
        "estimatedCostUsd": per_call * calls if per_call is not None else None,
        "priceBookVersion": PriceBook.load().version,
        "model": model,
    }
