"""모델 단가표 기본값 — 단일 출처 (이슈 #437).

운영 컨테이너에는 `app/`·`db/` 만 들어가므로(Dockerfile) `evals/` 를 런타임에 import 할 수
없다. 그래서 `evals/model_eval/pricing_manifest.json` 의 값을 여기 복제하고, 드리프트는
`tests/unit/test_model_pricing.py` 가 고정한다 — `app/core/config.py::ROUTE_INTENTS` 가
`app/agents/buyer/recommendation/state.py` 를 복제하고 테스트로 고정하는 것과 같은 관례다.

Anthropic 모델(`claude-haiku-4-5`·`claude-sonnet-5`) 단가는 repo 에 출처 있는 값이 없어 여기
신지 않는다 — `LLM_PROVIDER=anthropic` 로 기동하면 `log_model_price_table_status` 의
`MODEL_PRICE_MISSING_AT_STARTUP` 경고가 미등록을 그대로 드러낸다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    # config.py 가 이 모듈을 최상단에서 import 한다 — 여기서 config 를 최상단 import 하면
    # config → model_pricing → config 순환이 된다. 타입 힌트 전용이라 TYPE_CHECKING 가드로
    # 런타임 import 를 피한다(`from __future__ import annotations` 가 힌트 평가를 지연시킴).
    from app.core.config import Settings

# app.core.logging.get_logger 는 아래 한 줄(logging.getLogger)의 얇은 래퍼일 뿐이지만,
# 그 모듈이 app.core.config 를 최상단 import 하므로 여기서 가져다 쓰면
# config → model_pricing → logging → config 순환이 생긴다. 표준 logging 을 직접 쓴다.
logger = logging.getLogger(__name__)


class ModelPriceEntry(NamedTuple):
    """단가표 한 행 — 출처와 확인 날짜를 코드에 남기는 것이 이슈의 명시 요구사항이다(근거 없는 숫자 금지)."""

    model: str
    in_per_1k: float
    cached_in_per_1k: float
    cache_write_per_1k: float
    out_per_1k: float
    effective_date: str
    source: str


# 값·출처·날짜는 evals/model_eval/pricing_manifest.json 과 글자 그대로 일치해야 한다
# (EVAL-OBS-PLAN-001 §3.4 "비용축과 동일 소스 사용" 규약, test_model_pricing.py 가 고정).
DEFAULT_MODEL_PRICES: tuple[ModelPriceEntry, ...] = (
    ModelPriceEntry(
        model="gpt-5-nano",
        in_per_1k=0.00005,
        cached_in_per_1k=0.000005,
        # GPT-5 nano 문서는 별도 쓰기 단가를 명시하지 않으므로 일반 입력과 같은 보수적 단가.
        cache_write_per_1k=0.00005,
        out_per_1k=0.0004,
        effective_date="2025-08-07",
        source="https://developers.openai.com/api/docs/models/gpt-5-nano",
    ),
    ModelPriceEntry(
        model="gpt-5.6-luna",
        in_per_1k=0.001,
        cached_in_per_1k=0.0001,
        cache_write_per_1k=0.00125,
        out_per_1k=0.006,
        effective_date="2026-08-13",
        source="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    ),
    ModelPriceEntry(
        model="gpt-5.6-sol",
        in_per_1k=0.005,
        cached_in_per_1k=0.0005,
        cache_write_per_1k=0.00625,
        out_per_1k=0.03,
        effective_date="2026-08-13",
        source="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    ),
)

DEFAULT_MODEL_PRICE_IN_PER_1K: dict[str, float] = {
    entry.model: entry.in_per_1k for entry in DEFAULT_MODEL_PRICES
}
DEFAULT_MODEL_PRICE_OUT_PER_1K: dict[str, float] = {
    entry.model: entry.out_per_1k for entry in DEFAULT_MODEL_PRICES
}
DEFAULT_MODEL_PRICE_CACHED_IN_PER_1K: dict[str, float] = {
    entry.model: entry.cached_in_per_1k for entry in DEFAULT_MODEL_PRICES
}
DEFAULT_MODEL_PRICE_CACHE_WRITE_PER_1K: dict[str, float] = {
    entry.model: entry.cache_write_per_1k for entry in DEFAULT_MODEL_PRICES
}

# 기본표 기준일 — 항목 중 가장 최근 effective_date 에서 파생한다(항목 추가 시 자동으로
# 따라간다). ISO 8601(YYYY-MM-DD) 문자열이라 사전식 max 가 곧 날짜순 최신값이다.
MODEL_PRICE_TABLE_AS_OF: str = max(entry.effective_date for entry in DEFAULT_MODEL_PRICES)


def log_model_price_table_status(settings: Settings) -> None:
    """기동 시 1회 — 활성 모델의 단가 상태를 눈에 띄게 알린다(#437).

    지금까지는 미주입/불완전 단가표가 턴마다 조용히 costUsd=0 을 내고(모델별 1회 경고뿐),
    기동 시점에는 아무 신호도 없었다(#401 의 "0행이어도 조용히 degrade" 와 같은 성질의 침묵).
    판정 대상은 **이번 기동이 실제로 쓸 모델**(`resolve_model_id` 의 fast/smart, 중복 제거)뿐이다.

    `app.core.llm` 을 모듈 최상단에서 import 하면 `llm → tracing → observability → config →
    model_pricing` 순환 import 가 생기므로 함수 안에서 지연 import 한다. 어떤 예외가 나도
    기동을 막지 않는다 — 이 신호는 어디까지나 경고 수준이지 기동 거부가 아니다.
    """
    try:
        from app.core.llm import resolve_model_id

        active_models = sorted(
            {resolve_model_id(settings, "fast"), resolve_model_id(settings, "smart")}
        )

        missing = [
            model
            for model in active_models
            if settings.model_price_in_per_1k.get(model) is None
            or settings.model_price_out_per_1k.get(model) is None
        ]
        if missing:
            logger.warning(
                "활성 모델 단가 미등록 — 해당 모델의 턴 비용은 costUsd=0 으로 집계된다 "
                "code=MODEL_PRICE_MISSING_AT_STARTUP models=%s",
                missing,
            )

        defaults_in_use = (
            settings.model_price_in_per_1k == DEFAULT_MODEL_PRICE_IN_PER_1K
            and settings.model_price_cached_in_per_1k == DEFAULT_MODEL_PRICE_CACHED_IN_PER_1K
            and settings.model_price_cache_write_per_1k == DEFAULT_MODEL_PRICE_CACHE_WRITE_PER_1K
            and settings.model_price_out_per_1k == DEFAULT_MODEL_PRICE_OUT_PER_1K
        )
        if defaults_in_use:
            logger.warning(
                "env 주입 없이 코드 내장 기본 단가표를 사용 중이다 — 단가는 바뀌므로 값이 "
                "낡을 수 있다, MODEL_PRICE_IN_PER_1K/MODEL_PRICE_OUT_PER_1K 로 표 전체를 "
                "덮어쓸 수 있다 code=MODEL_PRICE_DEFAULTS_IN_USE as_of=%s",
                MODEL_PRICE_TABLE_AS_OF,
            )

        if not missing and not defaults_in_use:
            logger.info(
                "활성 모델 단가표 준비 완료 code=MODEL_PRICE_TABLE_READY models=%s",
                active_models,
            )
    except Exception:
        logger.error("model price table startup status check failed unexpectedly", exc_info=True)
