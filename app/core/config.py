"""애플리케이션 설정 (pydantic-settings).

프로젝트 원칙 "config 주입": 모든 튜너블(모델 ID, DB URL, 인증(JWKS/iss/aud),
Spring base URL, 검색 파라미터)을 환경변수로 주입하여 코드 변경 없이 교체 가능하게 유지한다.

[2026-07-15] MVP 후보 검색은 Spring 위임(GET /internal/products/search, I-1)이며 상품 원본
컬럼의 AI측 사본(카탈로그 미러)은 두지 않는다.
[2026-07-20 정정] enrichment·임베딩(§4.8 I-17 배치)은 MVP 편입 확정 — 임베딩 검색 방식1·2를
SearchBackend로 구현해 골든셋 비교. [2026-08-03 #32] 방식2를 확정하고 방식1·C-17은 기각 —
방식1은 오프라인 비교 전용으로 존치한다(api-spec §4.8 말미·§4.6).
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Literal, NamedTuple

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# I-21 계약 하드 상한(api-spec §4.2) — 노출 개수 설정이 계약을 넘지 못하게 묶는 기준.
# 계약 값의 단일 출처는 스키마다(app/schemas/spring.py) — 여기서 숫자를 다시 적지 않는다.
from app.schemas.recommendations import LIMIT_MAX as HOME_RECO_LIMIT_MAX
from app.schemas.spring import LIST_MAX_PRODUCTS, MAX_LISTS

LLMProvider = Literal["openai", "anthropic"]
# 검색 백엔드 선택(#101) — spring: Spring 위임만(방식1 이전 MVP, 운영 롤백), embedding_rerank:
# Spring 전량 → pgvector 의미 재정렬(방식2, MVP 기본), vector: 방식1 오프라인 비교 전용
# (운영 사용 금지, #32 미채택, C-17 기각).
SearchBackend = Literal["spring", "embedding_rerank", "vector"]
# 프로필 주입 소비처(#119) — off: 이번 턴 개인화 미적용, rerank_only: 기본(취향은 순서에만),
# both: 구 동작(decompose 하드필터 파생 허용, 롤백 경로).
# ⚠️ off 는 **주입만 차단**하고 프로필 축적(read·"기억해" 기록·세션 버퍼 적재)은 계속된다 —
# "모으되 아직 쓰지 않는" 섀도 모드이지 **게스트 등가가 아니다**(게스트는 그 경로 자체가 없다,
# PR #223 리뷰). A/B 에서 off 를 baseline 으로 쓸 때 그 구간의 추천 자체는 프로필 주입이 없어
# 깨끗하지만, off 기간에도 프로필은 계속 자라므로 "게스트와 동일 조건"으로 읽으면 안 된다.
# 축적까지 멈추는 킬스위치가 필요해지면 별도 스위치를 둔다 — off 의 의미를 좁히지 않는다.
ProfileInjectionScope = Literal["off", "rerank_only", "both"]
# rerank 의 프로필 사용 강도(#119) — tiebreak: 동점 처리 지시 부착, legacy: 지시 없음(현행).
ProfileRerankInfluence = Literal["tiebreak", "legacy"]
# decompose 가 산출하는 intent 집합 — 세션 버퍼 제외 intent 검증의 정의역.
# 정본은 RouteDecision.intent Literal(app/agents/buyer/recommendation/state.py)이며, 런타임
# import 는 순환이라 여기 복제하고 드리프트는 테스트로 고정한다(test_config_profile.py).
ROUTE_INTENTS = frozenset(
    {
        "recommend",
        "cart_add",
        "cart_view",
        "order_status",
        "general",
        "cart_remove",
        "wishlist_add",
        "wishlist_remove",
    }
)


class RescueStageCounts(NamedTuple):
    """첫 `conditions` 앞 직렬 Spring 구간의 이론적 최악 단 수 — 억제 스코프별로 나눈다 (#427 D7).

    DESIGN-SHARED-BUDGET-384 §3 D7 이 요구하는 **단일 계수 원천**이다. 기동 검증기
    (`_require_search_retry_within_stream_budget`)와 런타임 좁히기(D4, "남은 단 수" 계산,
    `app/agents/buyer/recommendation/graph.py::stream_recommendation`)가 이 함수 하나만
    호출해야 한쪽만 고쳐지는 드리프트(#383 이 고친 것과 같은 실패 모드)를 구조적으로 막는다.

    조기 return(전부 0)은 "미룸(`graph.py` 의 `may_auto_relax`)이 성립하지 않으면 첫
    `conditions` 앞 직렬 검증 대상이 아니다"는 #288 의 판단을 그대로 잇는다 — `main` 도 함께
    0 이 되는 것은 "이 계수가 실제로 도는 검색 단 수"가 아니라 "미룬 턴의 첫 이벤트 앞
    직렬 예산 모델"이기 때문이다(본검색 자체는 미룸 여부와 무관하게 항상 실행된다 —
    `_rescue_chain_stage_counts` 의 소비처는 이 사실을 알고 `main` 을 최소 1 로 보정해 쓴다).
    """

    main: int  # 본검색 — 미룸이 성립한 뒤에만 1, 아니면(조기 return) 0
    rescue: int  # F-1/#343 구제 재검색(상호배타, 최대 1) — suppress_search_retry() 밖이라 항상 재시도한다
    auto_relax: int  # 자동완화 probe — min(relaxation_max_rounds, |auto_fields ∩ chip_fields|)


def _rescue_chain_stage_counts(
    *,
    relaxation_max_rounds: int,
    auto_fields: list[str],
    chip_fields: list[str],
    category_expand_enabled: bool,
) -> RescueStageCounts:
    """`RescueStageCounts` 계산 (#288, #383 보정, #427 D7 로 3 항 분해).

    순수 함수 + 모듈 수준으로 둔 이유는 `_require_search_retry_within_stream_budget` 를
    테스트가 실제 config 조합(교집합 ≥ 2)으로 부를 유일한 표면이기 때문이다 — `Settings` 는
    `relaxation_auto_fields` 를 `{"ratingMin"}` 부분집합으로 잠그므로(`_forbid_auto_relaxing_
    explicit_constraints`) 인스턴스 경로만으로는 이 식의 `min`/교집합 분기를 실측할 수 없다.

    `graph.py` 의 `may_auto_relax` 판정·자동 완화 루프와 **같은 식**이어야 어긋나지 않는다:
    후보 생성기(`build_relaxation_candidates`)는 `chip_fields` 를 순회하므로 `auto_fields` 에만
    있고 `chip_fields` 에 없는 필드는 후보 자체가 안 생긴다 → 교집합으로 센다. 루프는
    `rounds >= relaxation_max_rounds` 에서 break 하므로 `min` 으로 상한을 씌운다.

    **`category_expand_enabled` 항(=`rescue`)의 근거(#383, docs/specs/MEASURE-FIRST-TOKEN-363.md
    §5)** — 구제 폴백 한 단이 과거 두 항에 빠져 있어 실측 구제 체인 단 수(3, `test_fanout.py`
    `test_worst_case_rescue_chain_sequential_stages_before_first_sse`)를 과소계상했다:
    - F-1(#222)에는 별도 kill-switch가 없다. `category_expand_enabled`(기본 `True`)가 F-1·#343
      둘의 공통 전제(`decision.category_expanded`)를 잠근다 — #343 자신의 플래그
      (`category_expand_post_suppress_fallback_enabled`)를 꺼도 F-1은 살아 있다.
    - F-1 과 #343 은 `category_expand_notice_suppressed` 로 상호배타라 한 턴에 최대 1회만
      돈다 — 그래서 항이 아니라 **존재 여부**(0 또는 1)만 더한다.
    - `search_filter_guard_enabled`(#393, 기본 `True`)는 이 항을 없애지 않는다:
      `graph.py`의 스킵은 무필터 payload 의 필터 축이 0개일 때만 걸리고, 카테고리 외 축을 준
      턴은 재검색이 그대로 돈다 — 최악 경로에는 이 단이 남으므로 식에
      `search_filter_guard_enabled` 항은 추가하지 않는다.
    `rescue` 항은 **미룸이 성립한 뒤에만**(아래 조기 return 0 을 통과한 뒤에만) 더한다 —
    F-1/#343 재검색은 본 검색이 이미 미뤄진 뒤에만 도는 후속 단계이지, 그 자체로 미룸을
    만들지 않기 때문이다.
    """
    intersection_size = len(set(auto_fields) & set(chip_fields))
    if relaxation_max_rounds <= 0 or intersection_size == 0:
        # may_auto_relax가 False — conditions가 검색 앞에 나가 직렬 검증 대상이 아니다.
        return RescueStageCounts(main=0, rescue=0, auto_relax=0)
    return RescueStageCounts(
        main=1,
        rescue=1 if category_expand_enabled else 0,
        auto_relax=min(relaxation_max_rounds, intersection_size),
    )


def _rescue_chain_serial_budget_s(
    *,
    counts: RescueStageCounts,
    search_timeout_s: float,
    spring_max_retries: int,
    search_retry_on_deferred_conditions: bool,
) -> float:
    """첫 `conditions` 앞 직렬 Spring 구간(본검색 + F-1/#343 재검색 + 자동완화 probe) 직렬
    최악 벽시계 (#427 D7).

    이름은 "rescue_chain"이지만 계산 대상은 §2 가 정의한 좁은 "구제 체인"(F-1/#343+자동완화,
    본검색 제외)이 아니라 본검색을 포함한 넓은 "첫 conditions 앞 직렬 Spring 구간"이다
    (DESIGN-SHARED-BUDGET-384 §2 용어 정의 F2).

    §1(d) 각주①의 억제 스코프 비대칭을 반영한다 — `search_retry_on_deferred_conditions=False`
    (기본값)일 때 F-1/#343 재검색(`counts.rescue`)은 `graph.py::stream_recommendation` 의
    `suppress_search_retry()` 컨텍스트 **밖**에서 돌아 재시도가 억제되지 않는다
    (`spring_client.py::search_products` 의 `attempts` 계산이 그 블록 안에서만 1회로
    강제된다). 나머지(본검색·자동완화 probe)만 억제된다. `True` 면 억제 산출부
    (`suppress_deferred_search_retry`) 자체가 항상 False 라 세 항 모두 재시도 예산을 쓴다.

    기동 검증(`_require_search_retry_within_stream_budget`)과 런타임 좁히기(D4, 그래프의
    "남은 단 수" 계산) **둘 다** 이 함수 하나만 호출한다 — 한쪽만 고쳐지는 드리프트를
    막는다(#383 이 고친 것과 같은 실패 모드, D7).
    """
    retried_budget = search_timeout_s * (spring_max_retries + 1)
    if search_retry_on_deferred_conditions:
        return (counts.main + counts.rescue + counts.auto_relax) * retried_budget
    return (counts.main + counts.auto_relax) * search_timeout_s + counts.rescue * retried_budget


def _deferred_first_event_i1_calls(
    *,
    relaxation_max_rounds: int,
    auto_fields: list[str],
    chip_fields: list[str],
    category_expand_enabled: bool,
) -> int:
    """미룬 턴의 첫 이벤트(`conditions`) 앞에 직렬로 놓이는 I-1 호출 수 (#288, #383 보정).

    [#427 D7] 구현은 `_rescue_chain_stage_counts` 로 위임한다 — 세 항의 정의·근거는 그
    함수 docstring 참조. 이 함수는 총합(`main + rescue + auto_relax`)만 남긴 하위 호환
    래퍼다(기존 두 불변식 `rescue ≤ total`·`total == 0 → rescue == 0` 을 그대로 보존한다).
    """
    counts = _rescue_chain_stage_counts(
        relaxation_max_rounds=relaxation_max_rounds,
        auto_fields=auto_fields,
        chip_fields=chip_fields,
        category_expand_enabled=category_expand_enabled,
    )
    return counts.main + counts.rescue + counts.auto_relax


def _deferred_first_event_rescue_i1_calls(
    *,
    relaxation_max_rounds: int,
    auto_fields: list[str],
    chip_fields: list[str],
    category_expand_enabled: bool,
) -> int:
    """위 총합 중 **구제 폴백 항(0 또는 1)만** 떼어낸다 (#383 R5, PR #414 Claude 리뷰).

    존재 이유는 값 매김이 다르기 때문이다 — `_require_search_retry_within_stream_budget` 의
    가드 OFF 분기(기본)는 `graph.py::stream_recommendation` 의 `suppress_deferred_search_
    retry = may_auto_relax and not search_retry_on_deferred_conditions` 로 재시도를
    끄는데, 그 `with spring_client.suppress_search_retry()` 블록은 저장소 전체에 **딱 두
    곳**뿐이다(같은 함수 안에서 본 검색을 감싼 곳, 자동 완화 probe `_probe(cand)` 를 감싼
    곳 — 둘 다 `await` 직후 블록을 닫는다). F-1(#222) 구제 폴백과 #343 억제-후 재판정
    (둘 다 같은 함수의 `_run_search_unfiltered()` 호출)은 그 블록 **밖**에서 돈다 —
    `spring_client.py::search` 의 `attempts = 1 if _search_retry_suppressed.get() else
    settings.spring_max_retries + 1` 을 그대로 타므로 가드 OFF 여도 **항상**
    `SPRING_MAX_RETRIES` 만큼 재시도한다. 그래서 이 항만은 검색 예산 1 회분이 아니라
    `budget = 검색예산 * (spring_max_retries + 1)` 으로 값을 매겨야 한다 — 총합 함수와
    세 항을 균질하게 검색예산 1 회분으로 매기면 `SPRING_MAX_RETRIES=1`
    (`.env.example` 이 한때 싣던 값)에서 이 항을 과소평가한다(이 이슈가 원래 고치려던
    실패 모드를 항 하나에서 되풀이하는 셈이다).

    구제 경로 자체를 억제하도록 `graph.py` 를 바꾸는 선택지는 **런타임 동작 변경**이라
    범위 밖이다(#384/#288 소관) — 여기서는 가드가 현실을 정확히 재는 것만 고친다.

    [#427 D7] 구현은 `_rescue_chain_stage_counts` 로 위임한다 — 조기 return 0 은 총합 함수와
    **같은 조건**(미룸 불성립)을 쓴다 — 그래야 `rescue ≤ total` 과 `total == 0 → rescue == 0`
    두 불변식이 항상 성립한다.
    """
    counts = _rescue_chain_stage_counts(
        relaxation_max_rounds=relaxation_max_rounds,
        auto_fields=auto_fields,
        chip_fields=chip_fields,
        category_expand_enabled=category_expand_enabled,
    )
    return counts.rescue


class Settings(BaseSettings):
    """환경변수 기반 전역 설정. 접두사 없이 대문자 필드명과 매핑된다."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Runtime environment and explicit request tracing ──
    app_environment: Literal["local", "staging", "production", "test"] = "local"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "jarvis-ai-local"
    langsmith_tracing_sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    langsmith_export_timeout_s: float = Field(default=0.5, gt=0.0, le=5.0)
    # [#326] 콘텐츠 추적 모드 — 켜면 발화·LLM prompt/응답 원문·Spring 페이로드가 트레이스에
    # 실린다(기본 off = #141 비유출 동작 유지). **실사용자 오픈 전 디버깅 구간 전용** —
    # 규약·kill switch 절차는 DEPLOY.md §8. per-value 절단 상한은 max_chars.
    langsmith_trace_content: bool = False
    langsmith_trace_content_max_chars: int = Field(default=20000, gt=0)

    @field_validator("langsmith_trace_content", "langsmith_trace_content_max_chars", mode="before")
    @classmethod
    def _empty_trace_content_settings_use_default(cls, value: object, info) -> object:
        # 배포 워크플로가 미설정 vars 를 빈 문자열로 기록한다 — bool/int 파싱 실패로 기동이
        # 죽지 않게 빈 값은 필드 기본값으로 해석한다(2026-08-05 APP_ENVIRONMENT 빈 값 부팅
        # 실패 교훈). max_chars 는 아직 deploy.yml 에 배선되지 않았지만 나란한 필드라 같은
        # 방식으로 배선되기 쉬워 선제 적용한다(PR #327 리뷰).
        if isinstance(value, str) and value.strip() == "":
            # Field 선언의 기본값을 그대로 참조한다 — 여기 값을 복제하면 선언만 바꿨을 때
            # "미설정 → 빈 문자열" 경로가 조용히 어긋난다(PR #327 리뷰).
            return cls.model_fields[info.field_name].default
        return value

    # ── LLM provider 토글 (이슈 #40) ──
    # "openai"(기본) | "anthropic". 호출부는 tier("fast"|"smart")로 부르고 provider 가 모델을 해석한다.
    # 실행 오버라이드: .env / OS 환경변수 LLM_PROVIDER 가 이 기본값을 덮는다(pydantic-settings).
    llm_provider: LLMProvider = "openai"

    # ── Anthropic 2-tier LLM (fast=haiku / smart=sonnet) ──
    anthropic_api_key: str = ""
    haiku_model_id: str = "claude-haiku-4-5"
    sonnet_model_id: str = "claude-sonnet-5"

    # ── OpenAI 2-tier LLM (fast=nano / smart=luna, 이슈 #40) ──
    # ⚠️ 정확한 API 모델 문자열은 OpenAI 대시보드에서 확인해 주입할 것(값은 config 튜너블).
    openai_api_key: str = ""
    openai_fast_model_id: str = "gpt-5-nano"  # fast: 고빈도 JSON(초저가)
    openai_smart_model_id: str = "gpt-5.6-luna"  # smart: 저빈도·품질(GPT-5.6 계열)
    # GPT-5 nano는 reasoning_effort="none"을 지원하지 않는다. fast JSON 태스크는
    # 최저 지원값인 minimal로 두어 숨은 추론이 출력 예산을 잠식하지 않게 한다.
    openai_fast_reasoning_effort: str = "minimal"
    openai_smart_reasoning_effort: str = "medium"  # smart: 근거문 품질용
    # gpt-5.6-luna 는 /v1/chat/completions 에서 function tools + reasoning_effort 조합을
    # 400(invalid_request_error)으로 거부한다(이슈 #178). tool 을 싣는 호출에서만 effort 를
    # override 값으로 강등한다 — 에러 메시지가 지시하는 경로. 조합을 지원하는 모델로
    # 바꾸면 목록에서 빼는 것으로 원복된다. 매칭은 접두사 — 날짜 스냅샷 ID도 함께 걸린다.
    openai_tool_reasoning_incompatible_models: list[str] = ["gpt-5.6-luna"]
    openai_tool_reasoning_effort_override: str = "none"
    # 요청 단위 비용 관측 단가(USD / 1,000 tokens). 운영 값은 환경변수 JSON으로 주입한다.
    # 빈 기본값은 임의 가격을 코드에 박지 않기 위한 fail-visible 설정이며, 미등록 모델은
    # observability가 비용 0 + 경고로 처리한다.
    model_price_in_per_1k: dict[str, float] = Field(default_factory=dict)
    model_price_out_per_1k: dict[str, float] = Field(default_factory=dict)

    # ── Google 임베딩 API (MVP, §4.8 배치 + 임베딩 검색) ──
    # [2026-07-20 결정 6 개정, v0.15.14] 셀프호스트 torch → Google gemini-embedding-001 API.
    # dim 1536(MRL 절단 — embedding.py 에서 수동 L2 정규화). 방식1(pgvector 벡터검색)·방식2(재정렬) 백엔드가 사용.
    google_api_key: str = ""
    embedding_model_id: str = "gemini-embedding-001"
    embedding_dim: int = 1536
    embedding_task_document: str = "RETRIEVAL_DOCUMENT"  # 저장 문서 임베딩 task(비대칭 검색)
    embedding_task_query: str = "RETRIEVAL_QUERY"  # 질의 임베딩 task(문서와 달라야 함)
    embedding_normalized: bool = True  # MRL 절단 후 수동 L2 정규화 여부(embedding.py)
    # Google 임베딩 API 요청 상한 — 방식2(embedding_rerank)가 hot path 기본이라 매 추천 턴이 이 호출을
    # 탄다. 상한 없으면 느린 응답이 SSE first-token 을 무기한 블로킹한다(CLAUDE.md 'AI→외부 3s' 규약).
    # 초과 시 embed_texts 가 예외 → EmbeddingRerankBackend 가 Spring 순서 degrade(#101 #7, PR#166).
    embedding_timeout_s: float = 3.0
    # embedding_timeout_s 는 청크(HTTP 요청) 1건당 상한인 반면, 이건 embed_texts 호출 1회
    # 전체의 벽시계 상한이다(#391) — 100건 초과 입력은 여러 청크로 나뉘어 순차 호출되므로
    # 요청당 상한만으로는 총 소요가 청크 수만큼 누적될 수 있다. hot path 규약은 "질의 1건
    # (=청크 1개)"이라 CLAUDE.md 'AI→외부 3s' 규약에 맞춰 기본값을 요청당 상한과 같은 3.0s 로
    # 둔다 — 즉 기본 설정에서 100건 초과 입력은 두 번째 청크를 내기 전에 거부된다. 초과 시
    # embed_texts 가 EmbeddingError → EmbeddingRerankBackend 가 Spring 순서로 degrade한다
    # (#101 #7, PR#166). 오프라인 1회 빌드(category_seed.seed_from_file)는
    # embed_texts(..., total_timeout_s=math.inf) 로 이 예산을 명시 제외한다.
    embedding_total_timeout_s: float = Field(default=3.0, gt=0.0)
    catalog_batch_page_size: int = 500  # I-17 배치 페이지 크기(§4.8, config 주입)
    # [#325] 운영 fast tier(gpt-5-nano, reasoning 모델)에서 하드코딩 max_tokens=600 전량이
    # reasoning_tokens 로 소진돼 본문 0자 → openai.LengthFinishReasonError 로 매 5분 주기 정지.
    # JSON 본문(태그 5~12 + 상황태그 3~7 + 속성 dict, 수백 토큰)이 reasoning 몫을 뺀 뒤에도
    # 남도록 여유를 둔다 — color_synonym_llm_max_tokens(2048) 전례와 같은 스케일.
    enrichment_max_tokens: int = Field(default=2048, ge=1)
    # [#325] enrichment(구조화 추출) 전용 effort — 배포 변수 OPENAI_FAST_REASONING_EFFORT 가
    # fast tier 기본 effort 를 무엇으로 덮든 이 값으로 고정된다(#178 tool 동반 호출 effort
    # 강등과 같은 계열: 특정 호출 용도는 tier 기본과 독립적으로 안전값을 강제).
    enrichment_reasoning_effort: str = "minimal"
    # [#325] _drain 항목별 재시도 상한 — 일시 플레이크(파싱 실패·비결정 출력) 구제용. LLM
    # 전송 자체의 재시도는 langchain max_retries 가 이미 담당한다.
    enrichment_item_attempts: int = Field(default=2, ge=1)
    # [#325 R3] 2선 방어(비율 가드) — 1선은 artifacts_batch._drain 의 구조적 판정이다:
    # embed()·store.upsert() 실패와, enrichment 재시도 소진 후 타임아웃 계열로 판정된 실패는
    # 격리하지 않고 그대로 전파해 이미 광역 장애로 처리된다. 이 비율 가드는 그 1선을 통과한
    # 뒤에도 남는 경우 — 인프라는 멀쩡한데 enrichment 결과 자체가 대량으로 깨지는 경우(프롬프트
    # 회귀, 모델 교체 사고 등) — 를 잡는다. 페이지 내 ON_SALE 실패 비율이 이 값 이상이면
    # (그리고 failed>0) 커서를 전진시키지 않고 예외를 던져 자연 복구(동일 커서 재개)로 돌아간다.
    artifacts_batch_failure_ratio_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    # [#325 R3] 위 2선 비율 가드가 유효하려면 최소 표본이 필요하다 — 운영 증분 배치는 5분
    # 주기에 실제 변경분만 담겨 페이지가 대개 1~3건이라(catalog_batch_page_size 는 요청 상한일
    # 뿐), poison 단건 상품 하나만 있어도 ratio=1/1=1.0 로 대량 결과 회귀와 구별이 안 된다.
    # 표본이 이 값 미만이면 비율 가드를 건너뛰고 격리+전진한다 — 소량 표본 판정 불능은 1선
    # (증명된 콘텐츠 실패 화이트리스트 판정)이 이미 광역 장애를 걸러낸 뒤라 안전하다. 남는 소수 항목 격리는
    # dead-letter ERROR 로그와 failed 카운트로 드러나며 run_batch --full(전체 재구축)로
    # 복구 가능한 유계 하방이다.
    artifacts_batch_failure_min_sample: int = Field(default=5, ge=1)
    # [#325 R4] 광역 장애와 항목 고유 결정적 실패는 한 주기 관측으로는 원리적으로 구별할 수
    # 없다 — 비율(R2)·단계(R3)·예외 타입(R3) 모두 각각 구멍이 남았다. 실제로 둘을 가르는
    # 신호는 시간이다: 광역 장애는 언젠가 끝나고, 항목 고유 실패는 몇 번을 다시 해도 같은
    # 자리에서 실패한다. 전파(자연 복구)는 유지하되, 같은 상품이 이 횟수만큼 "주기를
    # 가로질러" 연속 실패하면 항목 고유 실패로 확정하고 격리(dead-letter)한다. 기본
    # 3 × catalog_batch_interval_s(300s) ≈ 15분이 배치가 상품 1건 때문에 막힐 수 있는
    # 상한이다 — 이 안에서 끝나는 장애는 종전대로 자연 복구되고, 그보다 긴 장애는 그
    # 페이지의 항목들이 격리되지만 dead-letter ERROR 로그로 드러나며 run_batch --full
    # (전체 재구축)로 복구 가능하다.
    artifacts_batch_item_dead_letter_cycles: int = Field(default=3, ge=1)
    # [#325 R5] 3선(비율 가드) 방어에도 같은 시간 유계 원리를 적용한다 — 1선(항목별 즉시 격리)이
    # 특정 카테고리 상품들의 프롬프트 회귀로 다건을 매 주기 즉시 격리하면(스트릭을 쌓지 않고
    # pop 하므로 2선 상한이 걸리지 않는다), 페이지 실패율이 매 주기 똑같이 임계를 넘어
    # PageFailureThresholdExceeded 가 반복되고 커서가 전진하지 않아 같은 페이지가 무기한
    # 재조회된다(3선이 스스로 #325 의 무기한 정지를 재현). 같은 커서에서 비율 가드가 이 횟수만큼
    # 연속 발동하면 대량 파손이 자연 회복되지 않는 것으로 보고 그 페이지를 격리(항목들은 이미
    # 1선/2선에서 dead-letter 기록됨)하고 커서를 전진시킨다. 기본
    # 3 × catalog_batch_interval_s(300s) ≈ 15분이 대량 파손으로 배치가 멈춰 있을 수 있는
    # 상한이다 — 이 상한이 없으면 3선이 잡으려던 바로 그 케이스(대량 내용 파손)에서 #325 증상이
    # 그대로 재현된다. 상한 도달은 dead-letter ERROR 로 드러나며 복구는 run_batch --full.
    artifacts_batch_page_failure_max_cycles: int = Field(default=3, ge=1)
    catalog_vector_overfetch: int = 4  # 방식1 hydrate 후 필터·품절 제거 대비 벡터 여유조회 배수
    # 방식2 DB 재정렬 1회 반환 행 가드. 현 카탈로그 7,220건 전량도 p50 49ms라 기본값은
    # 실사용에서 걸리지 않는다. 카탈로그 성장 시 응답 행 수만 제한하며, 실질 지연 상한은
    # catalog_store_query_timeout_s(2.5s)가 맡는다. 상한 밖 후보는 Spring 순서로 보존된다.
    embedding_rerank_vector_k_max: int = Field(default=10000, ge=1)
    catalog_batch_interval_s: float = 300.0  # 주기 증분 pull 배치 스케줄러 간격(이슈 #31)
    # pg-catalog 질의 statement_timeout — get_many·top_k_by_vector 의 DB 쪽 상한(PR #213 리뷰).
    # 앱쪽 벽시계 포기는 스레드 밑의 쿼리를 못 죽이므로, 이게 없으면 지연 쿼리(I-17 replace_all
    # 테이블 락 등)가 풀 커넥션을 계속 붙들어 채팅 rerank 등 다른 경로까지 말려든다.
    # **앱쪽 호출 상한(home_reco_store_timeout_s)보다 커야 한다** — 같거나 작으면 "쿼리가
    # 느리다"는 동일 원인이 어느 타이머가 먼저 발동하느냐에 따라 503/504 로 비결정적으로
    # 갈린다(PR 리뷰: DB 가 먼저 끊으면 psycopg QueryCanceled → except Exception → 503).
    # 관계는 기동 시점에 강제한다(아래 model_validator).
    catalog_store_query_timeout_s: float = Field(default=2.5, gt=0.0)

    # ── 색상 동의어 확장 (이슈 #258) ──
    # 와이어 리스트 전송은 api-spec §4.6 `color: string` → `string[]` 개정과 BE 배포가
    # 모두 끝난 뒤에만 켠다. 기본 off에서는 승인 사전 DB도 조회하지 않아 현행 I-1 요청이 불변이다.
    color_synonym_expansion_enabled: bool = False
    # 운영자가 api-spec §4.6의 `color: string[]` 개정과 이를 파싱하는 BE 배포 완료를 함께
    # 확인했다는 명시적 계약 게이트. 확장 플래그와 이 값을 따로 켜면 기동 시점에 거부한다.
    color_synonym_array_contract_ready: bool = False
    # 새 표기마다 임베딩 API+DB write가 I-17에 추가되고 테이블도 아직 미검수 상태이므로 기본 off.
    # 초기 검수 완료 뒤 운영 비용을 확인하고 켠다.
    color_synonym_batch_harvest_enabled: bool = False
    # 런타임 승인 사전과 배치 수확이 공유하는 dsn별 pg 풀의 단일 총 상한. 배치 수확을 켜면
    # 해당 예산을 먼저 떼고 나머지를 검색 전용으로 쓰며, 끄면 검색이 풀 전체를 사용한다.
    color_synonym_pool_max_size: int = Field(default=4, ge=1)
    # wait_for 뒤에도 남는 to_thread 작업의 프로세스 동시 상한. 슬롯이 차면 해당 change 수확만
    # 즉시 건너뛰어 I-17 생성물 갱신과 cursor 전진을 지연시키지 않는다.
    color_synonym_harvest_max_concurrency: int = Field(default=2, ge=1)
    # 실카탈로그 상품당 색상 개수는 최대·p99 모두 30개였다. 정상 최대에 10개 여유를 둔 40으로
    # 단일 셀러 입력이 DB 배열·외부 임베딩 호출·pending 행을 무제한 증폭하지 못하게 한다.
    color_synonym_harvest_max_terms_per_product: int = Field(default=40, ge=1)
    # dedup 전에 원본 배열을 순회하는 CPU 상한. 실측 p99 30개의 13배인 400까지 스캔해 정상·
    # 중복 입력이 max_terms 밖의 고유 표기를 밀어내지 않게 하면서 비정상 대형 배열은 유계화한다.
    color_synonym_harvest_scan_max_values_per_product: int = Field(default=400, ge=1)
    # 실카탈로그 최장 색상 표기는 28자였다. 복합 표기 여유 12자를 둔 40자까지만 수확한다.
    color_synonym_harvest_max_term_length: int = Field(default=40, ge=1)
    # 실카탈로그 상위 30 표기가 전체 색상 토큰 출현의 82.2%(8,753/10,645)를 덮는다.
    color_synonym_top_n: int = Field(default=30, ge=0)
    # 실측 최저 정탐 남색-네이비=0.854, 최고 오탐 블랙-블루=0.849로 마진이 0.005뿐이다.
    # 0.85는 측정 오탐을 막고 핵심 정탐을 남기는 최소 안전선일 뿐 정밀도 보증이 아니다.
    # 임계만으로 확정하지 않고 LLM 판정 흔적과 경계 표시를 사람이 함께 검수한다.
    color_synonym_cluster_threshold: float = Field(default=0.85, ge=-1.0, le=1.0)
    # LLM 우선 배정은 꼬리 표기를 20개 기본 청크로 나누며, 이 값은 한 호출에 묶는 청크 수다.
    # 기본 1은 출력 규모를 유계화하고 한 호출 실패를 해당 20개 표기에만 격리한다.
    color_synonym_llm_clusters_per_call: int = Field(default=1, ge=1)
    # 앵커 병합 1회 또는 꼬리 배정 청크 JSON에는 충분하면서 무제한 출력을 막는 fast-tier 상한.
    color_synonym_llm_max_tokens: int = Field(default=2048, ge=1)
    color_synonym_cache_ttl_s: float = Field(default=300.0, ge=0.0)
    # I-1 승인 사전 조회·I-17 신규 표기 수확의 앱쪽 벽시계 상한. 동기 쿼리는 to_thread
    # 취소만으로 멈추지 않으므로 catalog_store_query_timeout_s가 이 값보다 늦게 DB에서 잘라
    # 커넥션을 회수한다. 앱 상한이 먼저 발동하면 두 경로 모두 기존 품질 degrade를 유지한다.
    color_synonym_query_timeout_s: float = Field(default=2.0, gt=0.0)

    # ── PostgreSQL / pgvector ×2 ──
    # catalog: AI 생성물(extras/search_doc/임베딩, §4.8 I-17 배치 upsert) 호스트, profile: 프로필 스토어+대화 저장(§6.3).
    catalog_db_url: str = "postgresql://jarvis:jarvis@localhost:5433/catalog"
    profile_db_url: str = "postgresql://jarvis:jarvis@localhost:5434/profile"

    # ── Spring 연동 (역방향 호출, api-spec §4) ──
    spring_base_url: str = "http://localhost:8080"
    # AI→Spring internal 역호출 서비스 토큰 (X-Internal-Token, api-spec §2.3 v0.13.0).
    # 빈 값은 개발용 — Spring 미가동 시 검색/‑push 는 어차피 SpringUnavailable 로 degrade.
    internal_api_token: str = ""

    # ── CORS (FE 직접 호출, api-spec §2.7 / C-11) ──
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # ── 인증 (api-spec §2.2, RS256 + JWKS 확정 2026-07-15) ──
    # dev : 서명 검증 없이 디코드 (헤더 없으면 게스트) — 로컬 개발 전용
    # jwks: Spring GET /.well-known/jwks.json 공개키로 RS256 검증 (kid→키, exp/iss/aud 확인)
    auth_mode: Literal["dev", "jwks"] = "dev"
    jwks_url: str | None = None
    jwt_issuer: str | None = "jarvis-spring-auth"
    jwt_audience: str | None = "jarvis-fastapi-ai"
    # 스트림 티켓 scope 확정값. 다른 값/빈 값/None은 설정 검증에서 기동을 막고,
    # decode 경계도 이 설정과 별개로 exact chat:stream을 항상 강제한다.
    jwt_scope: Literal["chat:stream"] = "chat:stream"
    # JWKS tier-1 캐시 TTL(s) — 만료 전에는 kid miss 시에만 refetch(§2.3), 요청마다 왕복 금지.
    jwks_cache_ttl_s: float = 300.0

    # [통일 2026-07-20 rebase 합류] 서비스 토큰은 팀 규약 `internal_api_token` 단일 키.
    # 인바운드(§3.5 verify_service_token — 프로필 write I-20)와 아웃바운드
    # (spring_client — AI→Spring)가 같은 X-Internal-Token 값을 공유한다(아키텍처 07/17).
    # 구 seller 전용 키 service_token(인바운드)·internal_token(아웃바운드)은 폐기.
    # spring_timeout_s 도 팀 정의(아래 공통 블록)를 재사용한다 — 중복 정의 금지.

    # ── 판매자 분석 임계값 (app/agents/seller 주입, 하드코딩 금지) ──
    # [#290] 구 임계 튜너블(seller_ma_window·seller_ma_min_window·
    # seller_anomaly_deviation_pct·seller_conversion_drop_pct)은 논문 기반 교체로
    # 폐기 — 아래 "분석 계산 층" 블록(S-H-ESD·Wilson/z-검정)이 대체한다.
    seller_churn_inactive_days: int = 30  # 이탈 코호트 무활동 일수(I-16 inactiveDays 기본)
    # [#197 PR 리뷰] I-8 계정/보안 이벤트는 전역 데이터(브랜드 스코프 아님)이고
    # admin 소유 협의가 미완(🔴, api-spec §4.4 v0.19.1)이다. 종전엔 코드 결함(쿼리
    # 400·스키마 미스매치)이 사실상 차단막이었으나 #197 정합으로 실노출이 가능해져,
    # 협의 완료 전까지 판매자 워커 표면 노출을 기본 비활성으로 보류한다.
    seller_account_events_enabled: bool = False
    seller_recent_days_default: int = 7  # normalize_period "최근 N일" 기본 N
    # 기간 상한(일) — 초과는 ValueError(되묻기)로 떨어뜨린다. 상한이 없으면
    # "최근 999999일" 이 date 연산에서 OverflowError 를 내고 호출부의
    # except ValueError 를 빠져나가 되묻기 대신 에러 경로로 샌다(#269). 기본 2년.
    # 상한 자체의 상한(#269 리뷰) — 약 74만일부터 `today - timedelta(days=n)` 이
    # date.min 을 넘어 OverflowError 를 낸다. 이 이슈가 막으려던 바로 그 실패 양상이라,
    # env 오설정을 기동 시점에 끊는다. 10년(3653일)은 판매 데이터 분석에 필요한 범위를
    # 한참 넘고 date 연산 한계보다 훨씬 앞이라, 자릿수 오타(731 → 7310000)를 일찍 잡는다.
    seller_period_max_days: int = Field(default=731, ge=1, le=3653)
    # safe_eval `**` 결과 자릿수 상한(DoS 방어) — 초과 식은 ValueError 로 거부(리뷰 반영).
    seller_calc_max_result_digits: int = 100
    # 도구 반환 상세도 상한(안 1+차등, 2026-07-17 사용자 확정) — 컨텍스트 폭주 방지.
    seller_summary_max_points: int = 60  # 시계열 상세 나열 상한(포인트 수)
    seller_summary_max_events: int = 5  # I-14 이벤트 kv 나열 상한(건)
    # [#197 PR 리뷰] I-16 이탈 회원 나열 상한 — I-14 용 max_events(위)와 분리 신설.
    # 같은 값 공유 시 I-14 요약 상세도 조정이 이탈 회원 노출 건수까지 바꾸는 결합이
    # 생긴다(#196 의 max_products 분리와 같은 취지). 서버 절단 상한은 별도로 50.
    seller_churn_member_max: int = 5  # I-16 members 상세 나열 상한(명)
    # [#196] I-13 상품별 rows 상세 상한 — I-14 용(위)과 분리. 구 공용 상한 5는
    # 시드 브랜드 상품 7종보다 작아 하위 2종이 상시 잘렸다. 상한 초과분은
    # _summarize_behavior 가 꼬리 합계로 남긴다(정보 소실 없음).
    seller_summary_max_products: int = 10  # I-13 상품별 rows 상세 나열 상한(건)
    seller_list_default_limit: int = 20  # I-9 상품 목록 기본 limit(미지정 시)
    # [#297] I-29 주문·I-31 리뷰 나열 상한 — 기존 상한들과 분리 신설(결합 방지, #197 취지).
    # 서버 페이지 상한(limit≤100)과 별개인 "도구 응답 상세도" 상한이다.
    seller_summary_max_orders: int = 10  # I-29 주문 rows 상세 나열 상한(건)
    seller_summary_max_reviews: int = 10  # I-31 리뷰 rows 상세 나열 상한(건)

    # ── 판매자 분석 계산 층 (이슈 #290, app/agents/seller/analysis/ 주입) ──
    # 근거 논문·산식은 docs/worker-papers.md — 아래 기본값은 논문 권장값이다.
    # [timeseries — S-H-ESD (Hochenbaum 2017) + STL (Cleveland 1990)]
    seller_stl_period: int = 7  # STL 계절 주기(일) — 요일 효과. Spring 주간 리듬 전제
    seller_gesd_alpha: float = 0.05  # GESD 검정 유의수준
    # GESD 최대 이상점 수 = ceil(기간 길이 × 이 비율) — S-H-ESD 권장 상한(≤0.49).
    seller_gesd_max_anomalies_ratio: float = 0.2
    # 도구가 요청 기간 앞에 붙여 조회하는 lookback(일) — STL 은 period 의 2주기 이상
    # 이력이 있어야 계절 성분을 추정한다(2×7=14 에 여유 2주기 = 28).
    seller_analysis_lookback_days: int = 28
    # STL 적용 최소 이력(일) — 미만이면 STL 생략, robust z-score 폴백(분해 자체가 불능).
    seller_min_history_for_stl: int = 14
    # [proportions — Wilson CI + two-proportion z-검정 (conversion·churn 공용)]
    seller_rate_test_alpha: float = 0.05  # 기간 비교 z-검정 유의수준
    seller_wilson_confidence: float = 0.95  # Wilson 신뢰구간 수준
    # [outliers — abuse 3-트랙 (Tan&Kumar 2002 피처, Chandola 2009 유형)]
    seller_mad_threshold: float = 3.5  # robust z(MAD) 스파이크 임계 — Iglewicz-Hoaglin 권장
    seller_tukey_k: float = 1.5  # Tukey fence 계수(Q3 + k×IQR)
    # 심야 활동 판정 시간대 [start, end) — I-8 hour-groupBy Collective 트랙.
    seller_night_hours_start: int = 0
    seller_night_hours_end: int = 6
    # [segmentation — k-means (Chen 2012), k 는 실루엣 최대 선택]
    seller_behavior_kmeans_k_min: int = 2
    seller_behavior_kmeans_k_max: int = 5
    seller_kmeans_random_state: int = 42  # 결정론(§10-②) — 같은 입력 = 같은 군집
    # [churn — 신호 순위화] pre_churn_signals 정규화 후 보고할 원인 후보 상위 k.
    seller_churn_signal_top_k: int = 3

    # ── 판매자 후속 단계 대비 선등록 (1단계 미소비, 하드코딩 재발 방지) ──
    seller_report_score_threshold: int = 21  # 보고서 검증 통과 점수(21/30)
    seller_report_max_retries: int = 3  # 검증 루프 상한
    seller_draft_ttl_minutes: int = 10  # HITL 미승인 draft 만료
    # 4-2 HITL 실행(hitl.py): confirm 시점 I-9 재조회(stale 검증)의 페이지 순회 상한 —
    # I-9 에 productId 필터가 없어 목록을 넘겨가며 찾는다(페이지 크기 = seller_list_default_limit).
    seller_draft_lookup_max_pages: int = 10
    # PostgresSaver(pg-profile) 초기 연결 대기 상한 — 초과 시 dev 는 InMemory 폴백.
    seller_checkpoint_connect_timeout_s: float = 5.0
    seller_history_recent_n: int = 5  # planner 최근 분석 이력 주입 건수
    # 4-3 분석 이력(history.py): 판매자당 보관 상한(초과분 오래된 것부터 폐기)과
    # 이력에 남길 보고서 요약 길이(전문 보존은 4-4 캐시 소관 — SPEC §9.1 "report 요약").
    seller_history_max_items: int = 20
    seller_history_report_max_chars: int = 500
    seller_tool_call_limit: int = 8  # ToolCallLimit 전역 한도(선택)
    seller_worker_timeout_s: float = 60.0  # 분석 워커 1종 실행 상한(3-3 팬아웃, §7 90s 목표 내)
    # general 레인(3-7) 전체 벽시계 상한 (#266 P1). 이 레인만 상한이 없어 스트림 전체
    # 90s 에만 의존했고, 그래서 LLM 지연이 계약상 LLM_TIMEOUT 이 아니라 INTERNAL 로 나갔다.
    # **다른 레인처럼 wait_for 로 감쌀 수 없다** — astream 은 중간에 yield 하는 async
    # generator 다. SDK 의 timeout= 도 답이 아니다: 스트리밍에서 그 값은 **청크 간 read
    # 간격**을 재므로 토큰이 상한보다 짧은 간격으로 계속 오면 영원히 발동하지 않는다.
    # 청크 루프를 통째로 덮는 asyncio.timeout 만이 이 레인의 실제 상한이다.
    # 근거: 2026-08-02 로컬 실측(Spring 기동, 동시성 1, n=30) general total max 2.55s ·
    # p95 2.52s — 20s 는 실측 max 의 약 8배이고 30턴 중 초과는 0건이었다.
    seller_general_timeout_s: float = 20.0

    # ── 브랜치 분석 검증 (이슈 #242, DESIGN-ANALYSIS-V31-242 §4·§9) ──────────────
    seller_worker_max_retries: int = 1  # F/judge 미달 시 브랜치 재실행 상한(보수적)
    seller_analysis_score_threshold: int = (
        21  # analysis_judge 통과 임계(21/30, report 와 동일 눈금)
    )
    # judge 를 워커 타임아웃(60s)과 분리 — 브랜치 최악 경로(worker→judge→worker→judge)가
    # 전 구간 60s 를 공유하면 §7 90s 목표가 붕괴한다(설계서 §9-R1).
    seller_analysis_judge_timeout_s: float = 20.0
    # 브랜치 1개의 총 예산(worker + judge + 재실행 1회 + judge) — 예산 초과 시 재실행을
    # 포기하고 원 finding 을 채택한다(강등이 아니다, §9-R1).
    # [PR 리뷰 반영] 기존 120.0 은 기본 타임아웃 조합의 최악 경로(60+20+60+20=160)보다
    # 작아 애초에 재실행을 허용할 여지가 거의 없었다 — orchestrator._run_one_branch 의
    # can_retry 판단(잔여 예산 ≥ worker+judge 타임아웃 합)과 짝을 맞춰 160.0 으로 상향한다.
    seller_branch_deadline_s: float = 160.0

    # ── 판매자 대화 스레드 (thread.py — checkpointer 기반 멀티턴 누적) ──
    # supervisor/planner 입력 주입 상한: 최근 턴(user+assistant 쌍) 수와 메시지당 절단.
    seller_chat_context_turns: int = 6
    seller_chat_context_max_chars: int = 300
    # 비-general 레인 record_turn 절단 — 보고서 전문이 아니라 후속 발화 이해용 맥락
    # (seller_history_report_max_chars 500 과 정합).
    seller_chat_record_max_chars: int = 500

    # ── 판매자 supervisor 라우팅 (4-1a, REALIGN §4 → #180 개정) ──
    # confidence 미달 = general 재지정(#180 저신뢰 폴백 역전 — 구 'analysis 보수
    # 라우팅' 폐기). 장애 = general 폴백 — "불확실하면 general" 단일 원칙.
    seller_route_confidence_min: float = 0.6  # 이 값 미만이면 general 로 재지정
    seller_route_timeout_s: float = 10.0  # 라우팅 LLM 상한 — first-token 10s 목표 내(§2.9)

    # ── 판매자 Anthropic temperature (SPEC-SELLER-001 §8) ──
    # 역할은 fast/smart tier만 선택한다. 이 값은 Anthropic 활성 시에만 적용하며,
    # 기존 환경변수 호환을 위해 haiku/sonnet 이름을 유지한다.
    seller_haiku_temperature: float = 0.0  # fast tier(일관성 장치 ①)
    seller_sonnet_temperature: float = 0.2  # smart tier(서술 품질)

    # ── 검색/추천 튜너블 (SPEC-RECOMMEND-001) ──
    # [#101] hot path 기본 검색 백엔드 = 방식2(Spring 전량 → pgvector 압축). 토글은 provider 처럼 전역.
    search_backend: SearchBackend = "embedding_rerank"
    # [#51] canonical category 가 있는 leg 는 Spring keyword(상품명 LIKE)를 드롭한다 — 상품명 글자
    # 부분일치 AND-필터라 동의어("청바지" vs 상품명 "데님 팬츠")를 retrieval 에서 원천 배제한다.
    # category 가 후보를 확보하고 semanticQuery 임베딩이 rerank 를 담당하므로 keyword 중복은 불필요.
    # False 면 기존 동작(leg query→keyword) 복원(롤백 안전성). category 가 없는 경로는 이 값과 무관하게
    # keyword 를 fallback 으로 유지(무필터 전체 카탈로그 방지).
    # [#51 리뷰] 이 플래그는 search_backend 와 커플링된다 — keyword 부재를 semanticQuery 재정렬이
    # 메우는 embedding_rerank 백엔드에서만 안전하다. spring(재정렬 없음)·vector(filters.keyword 를
    # 쿼리 임베딩 입력으로 씀)에서는 드롭이 품질을 급락시키므로, graph 가 search_backend!=embedding_rerank
    # 이면 이 값과 무관하게 keyword 를 유지한다(가드는 소비 지점 graph.py).
    search_drop_keyword_with_category: bool = True
    # pgvector 의미 재정렬 후 Sonnet 입력 상한(옛 "FastAPI 30" 이관처, §4.6). products[:limit] 절단이라
    # ge=0 — 음수면 slice 가 뒤에서 잘려 "<=0 이면 0개" 불변식이 깨진다(형제 category_fanout_* 규약).
    embedding_rerank_limit: int = Field(default=30, ge=0)
    search_default_limit: int = 30
    top_k: int = 30
    # [#162] 조건 없는 발화의 인기 상품(I-3, §4.17) 요청 개수.
    # **`gt=0` 이 방어다** — BE I-3 에는 범위 검증이 없어(정본 명시) 음수·0 을 보내면 400 이
    # 아니라 `200 + 빈 배열`이 온다. 잘못된 설정이 오류가 아니라 "인기 상품이 없음"으로 위장돼
    # 조용히 카드 없는 답변이 나가므로, 기동 시점에 막는다.
    # 기본값 30 의 근거는 양쪽 경계다 — 하한은 노출(`expose_max` 9) + 최근구매 dedup·소모품
    # 억제로 빠질 몫(BE 기본 12 는 여유가 3뿐이라 `expose_min` 5 까지 떨어질 수 있다), 상한은
    # `embedding_rerank_limit`(30)으로 그보다 많이 받아도 압축 단계에서 버려진다.
    # **dedup 손실은 아직 실측하지 못했다** — `orders` 0행이라 최근구매 제외가 현재 아무것도
    # 걸러내지 않는다. 구매 이력이 쌓이면 그 손실만큼 상향 후보다.
    popular_candidate_size: int = Field(default=30, gt=0)
    # 노출 개수(REQ-REC-021, api-spec §3.3) — **목록 하나 기준**이다. 니즈별 추천처럼 목록이
    # 여럿이면 목록마다 이 상한이 걸린다(REQ-REC-024).
    # 상한이 LIST_MAX_PRODUCTS 로 묶여 있는 이유: 이 값을 넘기면 push 페이로드 생성
    # (RecommendationListEntry)에서 ValidationError 가 나는데, 그 지점은 SpringUnavailableError
    # degrade 블록 **밖**이라 §3.3 의 "목록을 준비하는 데 문제가 있었어요" 대신 일반 INTERNAL 로
    # SSE 스트림이 끊긴다(PR #212 리뷰). 잘못된 설정은 런타임이 아니라 기동 시점에 잡는다.
    expose_min: int = Field(default=5, ge=1, le=LIST_MAX_PRODUCTS)
    expose_max: int = Field(default=LIST_MAX_PRODUCTS, ge=1, le=LIST_MAX_PRODUCTS)
    reason_max_len: int = (
        200  # I-21 reason 안전 상한(§4.2) — 표시 목표는 프롬프트 40자, 이건 방어캡
    )
    # [#60] 세트 모드를 즉시 종전 PICK_ONE 경로로 되돌리는 운영 롤백 스위치.
    budget_set_enabled: bool = True
    # 기본 3안은 선택 부담을 제한하면서 알뜰·균형·강조 조합을 함께 보여주는 최소 top-K다.
    budget_set_max_count: int = Field(default=3, ge=1, le=MAX_LISTS)
    # 노출 수보다 넓은 니즈당 6개 풀로 저가 대안을 보존한다(REQ-REC-077).
    budget_set_alt_pool: int = Field(default=6, ge=1)
    # 기본 5니즈×6대안=7,776 완전 탐색을 수용하고 비정상 폭발만 20,000에서 끊는다.
    budget_set_max_combinations: int = Field(default=20_000, gt=0)
    budget_set_label_cheap: str = "알뜰"
    budget_set_label_balanced: str = "균형"
    budget_set_label_focus: str = "{need} 중심"
    budget_set_label_alt: str = "다른 조합"
    budget_set_dropped_notice: str = "예산에 맞추려고 {items}은(는) 이번 조합에서 뺐어요."
    budget_set_unavailable_notice: str = (
        "{items}은(는) 가격을 확인할 수 있는 상품이 없어 이번 조합에서 뺐어요."
    )
    budget_set_limited_notice: str = (
        "한 조합에 담을 수 있는 상품 수에 맞춰 {items}은(는) 이번 조합에서 뺐어요."
    )
    budget_set_infeasible_notice: str = (
        "말씀하신 예산 안에 드는 조합을 찾지 못해 상품별로 보여드릴게요."
    )
    budget_set_candidate_fallback_notice: str = (
        "가격을 확인할 수 있는 상품 조합을 만들지 못해 상품별로 보여드릴게요."
    )
    # rerank 응답 출력 예산 — **노출 개수에 비례**해야 한다(PR #212 리뷰). 니즈별 분할이면
    # 한 번의 rerank 가 목록 수만큼 항목을 내는데, 고정 예산이면 항목이 27~30개로 늘 때 응답이
    # 중간에 잘리고 extract_json 이 파싱에 실패해 LLMError → 근거 없는 degrade 로 떨어진다.
    # "니즈별 근거 있는 추천"이 정작 니즈가 여러 개일 때 더 자주 깨지는 셈이다.
    # 기본값은 단일 목록 경로(expose_max=9)에서 종전 실효값 1500 과 정확히 같도록 잡았다
    # (960 + 60×9 = 1500) — 흔한 경로의 동작을 바꾸지 않으면서 다중 니즈만 넉넉해진다.
    rerank_max_tokens_base: int = Field(default=960, ge=0)  # overallComment·JSON 골격 몫
    rerank_max_tokens_per_item: int = Field(default=60, ge=1)  # {productId, rationale} 1건 몫
    llm_call_limit: int = 2
    relaxation_max_rounds: int = 3

    # ── 0건/소량 조건 완화 (#113, api-spec §3.1 suggestions.relaxation · 결정 14-D) ──
    # 필드명은 **와이어 표기(camelCase)** 다 — 그대로 `relaxation.field` 로 나가므로(§3.1) 내부
    # snake_case 와의 변환은 relaxation.py 한 곳에서만 한다.
    # [AC④] 카테고리는 어느 목록에도 넣지 않는다 — 카테고리 판단·승계는 #84 소관이고, 여기서 같이
    # 풀면 "무선 이어폰이 없으니 유선 어때요"처럼 **살 물건 자체를 바꾸는** 제안이 된다.
    relaxation_chip_fields: list[str] = Field(
        default_factory=lambda: ["priceMax", "ratingMin", "brand", "color"]
    )
    # 자동 완화(사용자 동의 없이 서버가 먼저 푸는) 허용 목록.
    # **무엇을 자동으로 풀 수 있는지는 튜너블이 아니다**(#133 `_require_degrade_notices_present` 와
    # 같은 원칙) — 목록을 **줄이는** 것만 설정이고, 넓히는 건 기동 시점에 막는다
    # (`_forbid_auto_relaxing_explicit_constraints`). REQ-REC-043·AC-REC-08(가격 제약 불가침)은
    # SPEC 이 하드 불변식으로 규정했는데, 여기 "priceMax" 한 줄을 더하면 "5만원 이하"라고 말한
    # 사용자에게 6만 5천원짜리가 **동의 없이** 노출된다 — 환경변수로 꺼지는 하드 룰은 하드 룰이 아니다.
    # 빈 리스트(=자동 완화 전면 off)는 정상적인 의사표현이라 허용한다.
    # REQ-REC-047 명시/비명시 태깅이 구현되면 이 목록 대신 source 로 판단하고 이 가드도 재검토한다.
    relaxation_auto_fields: list[str] = Field(default_factory=lambda: ["ratingMin"])
    relaxation_price_step_ratio: float = Field(default=0.3, gt=0.0)  # priceMax 상향 비율
    relaxation_price_round_unit: int = Field(default=1000, ge=1)  # 상향값 올림 단위(칩 문구 가독성)
    relaxation_rating_step: float = Field(default=0.5, gt=0.0)  # ratingMin 하향 폭
    # **완화 칩** probe(재검색) 상한 — estCount 는 page-local 로 못 구한다(가격·브랜드·색상은 Spring
    # 쿼리 파라미터라 탈락 상품이 응답에 아예 없다, spring.py ProductSearchResult docstring 참조).
    # 그래서 후보마다 완화 필터로 재검색해 실제 매칭 수를 센다. fan-out 턴은 leg 수만큼 곱해진다.
    # **자동 완화와 예산을 공유하지 않는다**(PR #248 리뷰) — 공유하면 자동 완화가 먼저 돌아 예산을
    # 다 쓴 턴에서 칩이 굶는데, 칩은 정작 **자동 완화가 실패했을 때 쓰라고 있는 폴백**이다.
    # 자동 완화는 `relaxation_max_rounds` 로 따로 제한한다(손잡이 하나가 하나씩만 맡는다).
    #
    # 기본값은 `relaxation_chip_fields` 개수에 맞춘다(PR #248 2차 리뷰). 종전 2 는 위 분리 **이전**
    # 자동 완화와 나눠 쓰던 시절의 값인데, 분리 후 재산정되지 않은 채 남아 "칩 필드 4개를 켜 두고도
    # 앞 2개만 동작"하는 자기모순이 됐다 — 예산이 모자라면 뒤쪽 후보는 estCount 를 못 구하고,
    # estCount 없는 칩은 만들 수 없어(schema 필수) **말없이 사라진다**(실제로 풀면 결과가 있어도).
    # 올려도 흔한 턴은 그대로다: 후보는 **값이 설정된 필드**만 되므로 필터를 1~2개 건 턴은 애초에
    # 예산 이하다. 달라지는 건 3개 이상 건 턴뿐이고, probe 는 `asyncio.gather` 병렬이라 늘어나는
    # 것은 벽시계가 아니라 **동시 호출 수**다.
    # 자동 계산(`len(chip_fields)`)으로 묶지는 않는다 — 손잡이가 사라져 필드를 늘릴 때마다 부하가
    # 말없이 따라 오른다. 조이는 배포는 이 값을 내리면 되고, 그때 잘림은 로그로 드러난다.
    relaxation_max_probes: int = Field(default=4, ge=0)
    # 이 수 **미만**이면 "소량"으로 보고 결과가 있어도 완화 칩을 함께 제안한다(AC①). 0 이면 0건일 때만.
    relaxation_min_results: int = Field(default=3, ge=0)

    # ── degrade 고지 문구 (#133) ──
    # 문안만 튜너블이고 **고지 여부는 튜너블이 아니다**(PR #235 리뷰). 아래 둘은 api-spec 이
    # 안내 발신 자체를 요구하므로 빈 값을 기동 시점에 막는다(_require_degrade_notices_present).
    # 초판은 셋 모두에 "빈 문자열 = 고지 끄기"를 뒀는데, 그건 이슈가 요구한 "문안 config 주입"을
    # 넘어 **정직성 자체를 옵션으로** 만든 것이었다 — 환경변수 한 줄로 #133 이 되돌려진다.
    #
    # 판매자에는 degrade 정직성 게이트(verifier.check_degrade_disclosed)가 있는데 구매자에는
    # 없어, 개인화·근거가 통째로 사라진 폴백이 평상시 문구와 구분되지 않았다.
    # rerank 폴백 — 사라진 것 중 **사용자가 카드로 확인 가능한** 추천 이유를 지목한다.
    # "취향"으로 쓰지 않는 이유: 게스트는 프로필이 없어 평상시에도 취향 반영이 없어 참이 아니다.
    # 실패 단계명·오류 코드는 쓰지 않는다(api-spec §3.3 "단계별 상세는 서버 로그 전용").
    rerank_fallback_notice: str = "추천 이유까지 정리하진 못했어요. 검색 결과 순서로 보여드릴게요."
    # 목록 push(I-21) 실패 안내 — 종전 graph.py 하드코딩을 문구 정책 한 곳으로 모은 것이다.
    # api-spec §3.3 이 "지연 안내가 포함되며"로 발신을 못박는다(§3.1·§4.2 서술도 동일).
    push_skipped_notice: str = "목록을 준비하는 데 문제가 있었어요. 잠시 후 다시 시도해 주세요."
    # 최근 구매 제외(I-19) 실패 안내. **여기만 빈 값이 정상이다** — 계약이 요구하지 않고,
    # 미고지가 문서화된 판단이기 때문이다. 조회 실패는 "중복이 노출됐다"가 아니라 "걸러내지
    # 못했다"라 실제 중복 발생 여부를 알 수 없고, rerank 폴백과 달리 거짓 주장을 하고 있지도
    # 않다(#133 판단). 값을 채우면 켜진다 — 판단을 코드 재배포 없이 되돌리기 위한 여지다.
    dedup_skipped_notice: str = ""
    # [#162] 조건이 하나도 없는 발화의 안내. **문안은 튜너블이지만 발신은 아니다**
    # (rerank_fallback_notice 와 같은 규약) — 없으면 사용자가 인기상품·취향 기반 결과를
    # **자기 조건이 반영된 결과로 오해**한다. 후보 소스가 다르므로 문구를 둘로 나눈다.
    # **무엇을 했는지 말하고 예시로 되묻는다** — "조건을 안 주셨다"고 단정하지 않는다. 예산만
    # 말한 턴("총 5만원 있어 아무거나")처럼 사용자가 무언가는 준 경우가 있어 단정이 거짓이 된다.
    # 예시를 넣는 이유는 "조건을 알려달라"만으로는 무엇을 어떻게 말해야 할지 모르기 때문이다.
    no_condition_notice_popular: str = (
        "지금 인기 있는 상품으로 골라봤어요. "
        '"5만원 이하 무선 이어폰"처럼 알려주시면 더 잘 추천해드릴 수 있어요.'
    )
    no_condition_notice_profile: str = (
        "취향에 맞을 만한 상품으로 골라봤어요. "
        '"5만원 이하 무선 이어폰"처럼 알려주시면 더 잘 추천해드릴 수 있어요.'
    )
    # 총액 예산만 말한 턴 전용 — `{budget}` 은 천단위 구분 금액이 들어간다(예: "50,000원").
    # 세트로 묶지 않고 **예산 안의 대안**을 보여주는 턴이라 문구도 "골라봤어요 + 되묻기"다.
    no_condition_notice_budget: str = (
        "{budget} 안에서 인기 있는 상품으로 골라봤어요. "
        '"무선 이어폰"처럼 어떤 상품을 찾으시는지 알려주시면 더 잘 추천해드릴 수 있어요.'
    )

    # ── 과소지정 발화 되묻기 (#336, `docs/specs/SPEC-UNDERSPECIFIED-336.md`) ──
    # 마스터 스위치 — off 면 `underspecified.is_underspecified_turn` 이 항상 False 다(AC: 한
    # 번에 전체 롤백). 기본 off — 이 기능은 no_condition(#162) 위에 얹는 확장이라, 검증 전
    # 기본 배포에 영향을 주지 않는다.
    underspecified_reask_enabled: bool = False
    # 제약(가격)만 있는 턴의 인기 상품 고지 — no_condition_notice_popular 와 같은 톤이되, 실제로
    # 가격 필터를 통과한 후보라는 사실만 말한다(거짓 주장 금지, #132). no_condition 턴에는 내지
    # 않는다(그 턴은 no_condition_notice_* 가 이미 담당 — 중복 고지 방지).
    underspecified_notice: str = "조건에 맞는 인기 상품으로 골라봤어요."
    # generic 되물음 — 노출 후보에서 예시를 뽑을 수 없을 때(취향 랭킹 경로·0건·예시 cap=0) 쓴다.
    # [리뷰 F3] 기동 검증 없음 — 이 리포의 고지 config 들은 "빈 값 = 그 고지만 끄는 스위치"
    # 관례다(`dedup_skipped_notice` 와 동일 판단). **빈 값(`_strip_unsafe` 정제 후 포함)이면
    # 되물음 token 만 꺼진다** — 후보 소스 스왑(I-3 + 가격 필터)·자동완화 억제·조건 칩 등 다른
    # 동작은 그대로 유지된다. 문구 하나로 기동을 막을 만큼 이 필드가 계약을 진 것은 아니다.
    underspecified_reask_question: str = "어떤 상품을 찾으시는지 조금 더 알려주시겠어요?"
    # 노출 후보 기반 예시 되물음 — `{categories}` 자리표시자 필수(없거나 포맷 실패 시 위 generic
    # 으로 폴백, `underspecified.build_reask_question` 참조).
    underspecified_reask_question_examples: str = (
        "{categories} 중에 찾으시는 게 있을까요? 아니면 다른 상품을 알려주셔도 좋아요."
    )
    # 예시로 뽑을 카테고리 최대 개수 — 0 이면 예시 없이 항상 generic 질문.
    underspecified_reask_examples_max: int = Field(default=3, ge=0)

    # ── 검색 필터 가드 (#393, api-spec §4.17) ──
    # 운영 실측(2026-08-06): I-1 이 SEARCH_FAILED 로 떨어진 요청은 Spring 이 실패한 게 아니라
    # 200 인데 3s 예산을 넘긴 지연이었다 — 무필터 I-1 이 매칭 전량(실측 12.3MB)을 돌려줬기
    # 때문이다. 마스터 스위치 — off 면 `search_guard.is_unfiltered_payload`/
    # `is_category_mapping_dropped` 판정 자체는 그대로 두고 호출부(`recommendation/graph.py`)가
    # 결과를 쓰지 않는다(AC: 한 번에 전체 롤백). **기본 on** — 이 가드가 막는 것은 매칭 전량
    # 응답이라 하방이 유계이고, off 는 운영 롤백 스위치다.
    search_filter_guard_enabled: bool = True
    # [#393 B] 카테고리 매핑이 드롭돼 검색이 0건이라 인기 상품으로 답하는 턴의 고지. 문안은
    # 튜너블이지만 발신은 아니다(no_condition_notice_* 와 같은 규약) — 없으면 사용자가 인기
    # 상품을 자기가 말한 상품군으로 오해한다. 실패 단계명·오류 코드는 싣지 않는다
    # (api-spec §3.3 "단계별 상세는 서버 로그 전용").
    category_unmapped_notice: str = (
        "말씀하신 상품을 정확히 찾지 못해, 지금 인기 있는 상품으로 골라봤어요. "
        "브랜드나 가격대를 함께 알려주시면 더 잘 찾아드릴게요."
    )

    # ── 홈 추천 랭킹 (I-22, api-spec §3.7 · 이슈 #148) ──
    # 질의 벡터 = 시그널 상품 임베딩의 가중 평균. cart 는 "담기까지 갔다"는 강한 신호라 조회보다 높게,
    # 조회는 최신일수록 높게(recency decay 를 인덱스 거듭제곱으로 적용) — §3.7 signals 표.
    home_reco_weight_cart: float = Field(default=1.0, ge=0.0)
    home_reco_weight_viewed: float = Field(default=0.6, ge=0.0)
    # [#148] 장기 취향 항 — 프로필 요약 벡터(sleep-time consolidation 이 미리 만든다).
    # cart(지금 담은 것)보다 낮게 둔다: 오래된 취향이 현재 관심을 덮으면 홈이 안 바뀐 것처럼 보인다.
    # 0 으로 두면 프로필 기여가 **완전히 꺼진다**(롤백 스위치) — reason 의 프로필 문자열 분기는
    # 극성(선호/회피) 문제로 제거됐으므로(83f78a1) 프로필의 유일한 소비처가 이 벡터 항이다.
    home_reco_weight_profile: float = Field(default=0.5, ge=0.0)
    home_reco_viewed_decay: float = Field(default=0.85, gt=0.0, le=1.0)
    # limit 은 최종 노출 목표치 — Spring 의 품절 드롭에 대비해 이 배수만큼 넉넉히 반환한다(§3.7).
    home_reco_overfetch_ratio: float = Field(default=2.0, ge=1.0)
    # overfetch 절대 상한(응답 크기 방어). **요청 `limit` 상한(`LIMIT_MAX`) 이상이어야 한다** —
    # 아래로 내려가면 `_overfetch_size` 가 요청받은 `limit` 보다 적게 반환해 "품절 드롭 대비
    # 넉넉히"(§3.7)가 깨진다. 기동 시점에 잡는다(`expose_max`/LIST_MAX_PRODUCTS 와 같은 방식).
    # 기본값은 LIMIT_MAX 의 2배(= 기본 overfetch 배율) — LIMIT_MAX 와 같게 두면 `limit` 이
    # 상한에 가까울수록 여유분이 0 으로 죽어 "넉넉히" 계약이 최댓값에서 깨진다(PR #213 리뷰).
    home_reco_max_items: int = Field(default=HOME_RECO_LIMIT_MAX * 2, gt=HOME_RECO_LIMIT_MAX)
    # 이 수 미만이면 랭킹이 무의미하다고 보고 INSUFFICIENT_CANDIDATES 로 답한다(200).
    home_reco_min_candidates: int = Field(default=5, gt=0)
    # 카탈로그 스토어 **호출 1회** 상한 — pg-catalog 지연·락이 한 단계를 무한정 붙들지 않게 한다.
    # 초과 시 랭킹 경로는 504 UPSTREAM_TIMEOUT(계약 실패표), reason 경로는 null degrade 다.
    home_reco_store_timeout_s: float = Field(default=2.0, gt=0.0)
    # 요청 **전체** 예산 — 스토어 호출이 3번이라 호출별 상한만으로는 최악 2s×3=6s 로 §3.7 의
    # "응답 3s" 를 넘는다(PR #213 리뷰). 각 호출은 min(호출 상한, 남은 예산)으로 기다리고,
    # 예산이 바닥나면 랭킹 경로는 504·reason 경로는 skip 이다. 3s 계약에서 직렬화·네트워크
    # 여유 0.5s 를 뺀 값이 기본이다.
    home_reco_budget_s: float = Field(default=2.5, gt=0.0)
    # [#148 실측 2026-07-31] reason 을 요청 경로에서 LLM 으로 만드는 방식은 **폐기**했다.
    # gpt-5-nano 배치 1회가 항목 수에 선형으로 늘어(20개 7970ms · 12개 3852ms · 6개 2102ms)
    # I-22 예산(연결 2s/응답 3s)을 5개에서도 넘겼다. 지금은 I-17 배치가 상품당 1회 만들어 둔
    # `extras`(situation_tags·review_pros)에서 **고르기만** 한다(`home_recommendation.build_reasons`).
    # 따라서 reason 관련 timeout·상한 설정이 없다 — 실패할 여지도 예산 소모도 없기 때문이다.
    # rating·reviewCount 등급화 경계(#171 PR#172) — 비표시 정밀값 유출 방지용으로 rerank LLM 에
    # 정확한 숫자 대신 등급만 전달할 때 쓰는 임계. 내림차순(높은 등급부터). 데모 카탈로그 실측 후 조정.
    rating_tier_excellent: float = 4.5  # ≥ → 매우높음
    rating_tier_good: float = 4.0  # ≥ → 높음
    rating_tier_fair: float = 3.0  # ≥ → 보통 (그 미만 낮음)
    # [#132] 사용자가 평점을 **명시**한 턴에서 무평점 상품이 노출될 때 근거문에 덧붙는 고지.
    # #100 P0 의 rating 사후필터는 '반증된 것만' 제거하므로(무평점 보존, #171) "평점 4.5 이상"이라
    # 말한 사용자에게도 리뷰 없는 신상품이 올라온다 — 그 사실을 카드마다 드러내지 않으면 사용자는
    # 4.5↑ 라 믿고 본다. 자동 완화(`relaxation_notice`)는 이미 고지되는데 이쪽만 조용했다.
    # 빈 값은 "고지하지 않는다"는 정상적인 의사표현이다(`dedup_skipped_notice` 와 같은 규약) —
    # 계약이 요구하는 degrade 고지가 아니라 UX 정책이라 `_require_*_notice` 검증 대상이 아니다.
    rating_unrated_disclosure_notice: str = "평점 정보 없음"
    review_tier_many: int = 100  # ≥ → 매우많음
    review_tier_some: int = 20  # ≥ → 많음
    review_tier_few: int = 5  # ≥ → 보통 (그 미만 적음)
    # price 는 절대 기준이 없어 후보 그룹 중앙값 대비 상대 등급으로만 전달한다(#173).
    price_tier_very_cheap_ratio: float = 0.6  # 그룹 중앙값 대비 ≤ → 매우저렴
    price_tier_cheap_ratio: float = 0.85  # ≤ → 저렴
    price_tier_pricey_ratio: float = 1.15  # ≥ → 비쌈
    price_tier_very_pricey_ratio: float = 1.5  # ≥ → 매우비쌈
    # [#236] need_of 가 없는 턴은 후보의 category 로 그룹을 나눈다 — I-1 요청 categoryName 이
    # 대분류여도 응답 [].categoryName 은 leaf 라(§4.6), 대분류 leg 1개·leg 없는 검색에서
    # 상품군이 섞여 전역 중앙값 하나로는 등급이 적극적으로 틀린다. 다만 leaf 가 잘게 쪼개져
    # **유효 price 가 이 수 미만**인 그룹은 중앙값이 사실상 자기 자신이라 전원 '보통'으로 신호가
    # 죽으므로 등급 산출을 포기하고 '정보없음'으로 내린다. 전역 중앙값으로 폴백하지 **않는다** —
    # 전역은 후보가 섞인 값이라 작은 그룹에 이 이슈의 왜곡을 그대로 다시 씌운다.
    # need_of 가 있는 턴에는 적용하지 않는다 — 니즈 경계는 상위 판정이라 #173 그룹이 곧 정답이다.
    price_group_min_size: int = Field(default=2, ge=1)  # 미만 → 정보없음 (1 이면 사실상 해제)

    # ── 카테고리 하이브리드 매핑 (이슈 #59, DESIGN-CATEGORY-HYBRID-59) ──
    # 방식 A: decompose 추측 → 임베딩 보정(exact/최근접). canonical-or-null·멀티 fan-out.
    category_top_k: int = 5  # raw·query 앵커 최근접 조회 top-k
    # 턴당 최대 카테고리 수(프롬프트 상한 + 코드 절단). ge=0 — 음수면 out[:fanout_max] 가
    # 뒤에서 잘려 "fanout_max<=0 이면 정확히 0개" 절단 불변식이 깨진다(PR #73 리뷰).
    # leg(니즈) 수 상한. 계약 목록 상한(§4.2 lists ≤10)을 넘길 수 없다 — case 3 은 니즈 하나가
    # 목록 하나라(REQ-REC-024) 넘기면 초과분이 push 직전에 **조용히 잘린다**. 사용자는 요청한
    # 니즈가 사라진 걸 알 수 없고, rerank 예산도 잘려나갈 니즈까지 세어 부푼다(PR #212 리뷰).
    category_fanout_max: int = Field(default=5, ge=0, le=MAX_LISTS)
    # per_cat_limit·merge_cap 도 fanout_max 와 같은 절단 규약(leg top-K·merged[:cap]). 음수면
    # merged[:cap] 이 "뒤에서 제외"로 뒤집혀 "cap<=0 이면 0개" 불변식이 깨진다(PR #73 리뷰).
    # [#101 PR#166] leg 별 filters.limit 로 실리지만, hot path 방식2(EmbeddingRerankBackend)·
    # SpringSearchBackend 는 filters.limit 을 읽지 않아(절단은 graph dedup 이후 embedding_rerank_limit)
    # 현재 사실상 무효다 — 방식1(VectorSearchBackend, hydrate 미주입이라 hot path 미탑재)만 참조한다.
    # leg 균형은 _merge_fanout_results 의 round-robin + merge_cap 이 담당한다. 값 변경이 방식2
    # 동작에 영향 없음(fan-out 절단 재배치는 별도 과제).
    # [#89] fan-out leg 사전 절단은 더 이상 이 값을 소비하지 않는다 — category_fanout_merge_cap
    # 이 leg 수와 무관하게 담당한다(생존 leg 수는 gather 이후에야 확정돼 요청 시점 값으로는
    # 재조정이 안 됨). 필드 제거는 후속 과제.
    category_fanout_per_cat_limit: int = Field(
        default=10, ge=0
    )  # leg top-K(§4.6 size 아님) — 방식2 hot path 에선 현재 무효(위 주석)
    category_fanout_merge_cap: int = Field(default=30, ge=0)  # 병합 후 rerank 입력 상한
    # pg-catalog 검색 풀 max_size — fan-out 은 한 턴에 최대 category_fanout_max leg 를 gather 로
    # 동시 조회하므로, psycopg_pool 기본값(4)이면 그 이상 leg 가 커넥션을 기다린다. fanout 이상 +
    # 동시 요청 헤드룸으로 명시(암묵 하드코딩 제거, PR #73 리뷰).
    # [#115] 앵커가 leg 당 raw·query **2개**가 되면서(§4.3) 한 턴의 동시 조회가 `2 × fanout_max`
    # 로 늘었다 — 종전 10 은 한 턴이 풀 전체를 소진해 동시 요청 헤드룸이 0 이었다(PR #188 리뷰).
    # 20 = 2 × fanout_max(한 턴) × 동시 턴 2. 하한은 아래 _require_pool_covers_anchor_concurrency
    # 가 기동 시 강제한다.
    category_search_pool_max_size: int = 20
    # [#401] 기동 시 categories 0행/0임베딩 가드(app/pipelines/category_seed.py
    # check_category_dictionary) 를 어떻게 처리할지. "off"=검사 생략, "log"=원인별로 로그만
    # 남기고 계속(ERROR/WARNING, 아래 참조), "fail"=사전이 건강함을 **확인하지 못하면** 기동
    # 거부(app/main.py::_check_category_dictionary_startup).
    # [라운드 7 F8] "fail" 은 원인을 "구성 오류"로 좁혀 잡지 않는다 — 0행/0임베딩·
    # `UndefinedTable` 같은 비연결 DB 오류뿐 아니라 **도달 불가**(`OSError`·
    # `psycopg.OperationalError`)와 가드 코드 자체의 예상 못 한 예외까지 전부 거부 사유다.
    # `psycopg.OperationalError` 하나에 일시적 도달 불가(연결 거부·타임아웃)와 영구적 구성
    # 오류(비밀번호·dbname 오타)가 구조화된 판별자 없이 섞여 나온다(실측 확인, 메시지 문자열은
    # 서버 `lc_messages` 에 따라 지역화돼 매칭에 못 쓴다) — 그래서 "진짜 구성 오류만" 골라
    # 거부하는 분류는 불가능하고, "fail" 을 건 이상 확인 실패 자체를 거부 사유로 삼는다(로그
    # 문구는 원인별로 계속 구분해 남긴다 — 거부 여부만 같아질 뿐 진단 정보는 그대로 유지).
    # 기본이 "fail" 이 아닌 이유: 사전 결측은 map_categories 가 canonical-or-null 로 무필터
    # 퇴화하는 상태다 — 카테고리 매핑만 못 쓸 뿐 서비스는 계속 응답 가능하다. 반면 기동 거부는
    # 서비스 전면 중단이라 하방이 무계다(§4 거리컷과 같은 "미회수는 안전, 오염이 위험" 비대칭의
    # 거울상 — 여기서는 "결함을 못 알아채는 것"이 회수 실패고 "서비스가 안 뜨는 것"이 오염 쪽
    # 위험). 그래서 결함 교정(=시끄럽게 만들기)은 기본 on(`log`) 이고, 기동 거부는 그걸 원하는
    # 환경(예: 배치 세팅 직후 강한 검증)이 옵트인한다.
    category_dictionary_startup_check: Literal["off", "log", "fail"] = "log"
    # [#115] 최근접 채택 상한 — 채택 거리가 이 값을 **초과**하면 그 leg 를 canonical 없이 드롭한다
    # (§4 거리 조건부 채택. 종전 never-null "멀어도 억지로 채택"은 폐기). 거리 초과는 "맞는 칸이
    # taxonomy 에 없다"의 신호다.
    # [#344 재측정] 사전이 leaf 2,056행 → **1,007행**으로 교체돼 0.22(2056행 기준)가 stale 이었다.
    # 기준선 `evals/category_probe/baselines/fast-2026-08-06`(hits.csv, 앵커 38셀×N=8) 오프라인
    # 스윕 결과 **0.26** 으로 올린다 — single 슬라이스 winner top-1 거리가 정답 med 0.2416·q3
    # 0.2579·max 0.3239 인데 notInCatalog(사전에 칸 없음, 오강제 금지 가드레일) 최소 d1 이
    # 0.2621(`수예 재료`, margin 0.0275)이라 **0.26 이 nic 무강제(0/40)를 지키는 최대 컷**이다
    # (0.265 부터 오강제 7건). 0.22→채택 61/176·드롭 107, **0.26→채택 130·오답채택 8·드롭 30·
    # nic 0/40**, 0.28→채택 147 이지만 nic 7/40 로 붕괴 — 그 사이에서 채택을 최대화하는 값.
    # ⚠️ 이 상향은 공짜가 아니다 — 오답채택이 0.22 에서 **0** 이던 것이 0.26 에서 **8** 로 생긴다.
    # 그래도 같은 구간에서 정답 채택이 61→130(+69)이라 손익은 성립한다: 미회수(드롭)는 무필터로
    # 안전하게 퇴화하지만 오분류 유입은 검색을 틀린 칸으로 좁혀 정답 상품을 후보에서 배제한다는
    # §4 비대칭(아래 override_margin 주석)에 비춰도, 협소 발화 69건을 살리는 대가로 8건을 틀린
    # 칸으로 보내는 쪽이 순이득이다.
    # ⚠️ 재튜닝 조건: 이 값은 **임베딩 모델·task_type·사전**에 종속된다(gemini-embedding-001
    # 1536-dim L2 정규화 + 앵커 RETRIEVAL_QUERY / 시드 RETRIEVAL_DOCUMENT). 셋 중 하나라도 바뀌면
    # 재측정 없이는 무효다 — `evals/category_probe/manifest.py` 의 `dictionaryHash`(categories
    # 행 수 + 정렬된 canonical 전체의 sha256)로 과거 런과 사전 상태가 같은지 대조할 수 있다.
    # 재측정은 `uv run python -m evals.category_probe.sweep --run <hits.csv 있는 런 디렉터리>`.
    # [#401] 근거 사전은 이제 repo 정본 `db/catalog/seed/categories.json`(leaf 1,007) — codepoint
    # 정렬 sha256 `db81e849616ec5782f9d1b4ecda1f6eb15f9dbc7a2ec939b40e33fa786d65089`, en_US.utf8
    # 정렬(현행 `dictionaryHash` 가 재는 순서) sha256 `fb9ca975af1ea86ce013caeb018b7adcefc80a96d529aad0dd0555e464f21fe6`.
    # 정본이 밖에 있어 이 임계 근거를 재현할 수 없던 문제를 편입으로 없앤다(`db/catalog/seed/README.md`).
    # 절단 튜너블(ge=0)이 아니라 비교 임계라 코사인 거리 정의역 [0,2] 로 범위 검증한다.
    category_distance_max: float = Field(default=0.26, ge=0.0, le=2.0)
    # [#115 §4.5] 거리컷 마진 예외 — 거리가 임계를 넘어도 마진이 이 값 **이상**이면 채택한다.
    # 근거(76 앵커 실측): 거리는 도메인 어휘 차이에 오염된다. 식품은 상품명과 leaf 이름이 달라
    # 정답 매핑도 멀고(`돼지 등뼈`→`축산 > 돼지고기` 0.2661, `미역`→`수산 > 해조류` 0.2436),
    # 공산품은 상품명이 곧 leaf 이름이라 가깝다(`청바지` 0.1224). 반면 taxonomy 에 칸이 **없으면**
    # 여러 후보가 고만고만하게 멀어 마진이 얇다 — 즉 "맞는 칸이 없다"를 직접 재는 지표는 마진이다.
    # 실측 분리: 회수 대상 상위 7건 0.034~0.085 vs 차단 대상 최대 0.0249(`부모님 환갑 선물`).
    # ⚠️ 경계 여유가 0.008 뿐이고 표본이 76 건이라 보수적으로 잡는다 — 미회수는 무필터(종전 동작,
    # 안전)지만 오분류 유입은 검색을 틀린 칸으로 좁혀 정답 상품을 후보에서 배제한다(§4 비대칭).
    # 관측(`category_distance_override`) 분포가 쌓이면 완화를 검토한다. 0 이면 예외 off 가 아니라
    # **전부 채택**이 되므로(마진 ≥ 0), 끄려면 임계보다 큰 값(예 2.0)을 준다.
    # ⚠️ `category_select_margin_max`(§4.4 애매 판정)보다 **커야** 한다 — 두 구간은 정반대 상태라
    # 겹치면 안 된다(아래 _require_margin_bands_disjoint 가 기동 시 강제).
    # [#344 재검증] leaf 1,007행 기준선 재측정에서도 이 값을 옮길 근거가 없었다 — 거리 초과 채택
    # 예외가 정답 6건을 구제하면서 nic 최대 마진 0.0338, 오답 셀(single-mft-009) 마진 0.0341 을
    # **0.0012 차로** 배제한다(0.26 컷 기준 재확인). 값 유지.
    category_distance_override_margin: float = Field(default=0.035, ge=0.0, le=2.0)
    # [#115] top-k LLM 택일 트리거(§4.4) — 마진(2위−1위 거리차)이 이 값 **이하**면 애매한 판정으로
    # 보고 select_category 로 후보 중 택일한다. 거리컷이 못 잡는 구멍용: 추상 라벨('선물용품')은
    # 거리 0.2074(컷 통과)인데 뜻이 틀리고, 마진은 0.0095 로 얇다. 마진을 드롭 조건으로 쓰면
    # '양말'(1·2위 둘 다 정답, 마진 0.0088)을 오탐하므로 드롭이 아니라 택일 트리거로만 쓴다.
    # [#344 재검증] leaf 1,007행 기준선 재측정에서도 이동 근거 없음 — 값 유지(override_margin 과의
    # 밴드 분리 제약은 `_require_margin_bands_disjoint` 가 그대로 강제).
    category_select_margin_max: float = Field(default=0.02, ge=0.0, le=2.0)
    # 턴당 택일 LLM 호출 상한 — fan-out 5 leg 이 모두 애매하면 턴 LLM 이 2→7회로 뛴다. 초과 leg 는
    # 임베딩 top-1 을 그대로 쓴다(종전 동작). ge=0 — 0 이면 택일 기능 off.
    # ⚠️ **턴당**이 계약이다(#217 PR 리뷰). `map_categories` 는 이 값을 호출 단위로 적용하므로,
    # #217 로 매핑이 턴에 2회 불리게 된 뒤로는 **호출부가 남은 예산을 계산해 넘겨야** 상한이 지켜진다
    # (`graph._map_or_empty(select_max_calls=...)` ← `CategoryMapping.select_calls`).
    # 매핑을 부르는 새 경로를 만들 때 이 배선을 빠뜨리면 상한이 조용히 배수로 깨진다.
    # [#344 관측] 거리컷 상향(0.22→0.26)으로 애매 판정(택일 트리거)이 늘었다 — 같은 기준선
    # (`fast-2026-08-06`, 304 표본)에서 25 → **88** 회로 3.5배 증가(표본당 0.29회). 턴당 상한 2 는
    # 여전히 이 트리거율을 덮지만(fan-out 5 leg 전부가 동시에 애매할 확률이 낮다), 이 상한을 다시
    # 만질 때는 이 관측을 먼저 볼 것 — 거리컷이 더 오르면 트리거율도 함께 오른다. **값은 유지.**
    category_select_max_calls: int = Field(default=2, ge=0)

    # ── 광역 발화 → leaf fan-out (이슈 #222) ──
    # 이슈 원안(top-k 의 공통 조상으로 광역/협소 판정)은 오케스트레이터 실측으로 기각(정확도 0.50,
    # 우연 수준). 채택안은 판정기를 만들지 않는다 — 매핑이 canonical 을 못 낸 leg
    # (`CategoryMapping.unresolved`, #217 이 이미 만든 신호)을 트리거로, 그 앵커의 의미 기반
    # top-N leaf 를 그대로 fan-out leg 으로 쓴다(`CategoryMapping.expansion_leaves`). 협소 발화는
    # canonical 을 내므로 이 경로에 애초에 진입하지 않는다 — 그 자체는 구조적이다.
    # [PR #318 리뷰 R14-2] 단 이 "진입하지 않는다"는 **거리 임계가 정상 튜닝돼 있을 때만**
    # 성립한다 — 재측정 완료(#344, 0.26). 잔존 드롭(정당: 사전에 칸이 없거나 d1>0.26)이 이
    # 확장 폴백으로 가는 것은 여전히 정상 동작이다 — 확장 top-N 은 의미 최근접이라 정답 leaf 가
    # 대체로 상위에 포함되고(실측: "무선 이어폰" top-1 = 음향가전 > 이어폰) leg 마다
    # keyword·semantic_query 가 유지되므로, 무필터 degrade(종전 동작) 대비 악화는 아니다.
    category_expand_enabled: bool = True  # 광역 fan-out 롤백 스위치
    # [PR #318 리뷰 R5-1] **턴 전체 상한**이다 — unresolved leg 당 상한이 아니다. unresolved leg
    # 이 여럿이면 `category_mapping._collect_expansion_leaves` 가 leg 마다 모은 후보를 라운드로빈
    # 인터리브(`recommendation/graph._merge_fanout_results` 와 같은 규약)로 평탄화한 뒤 이 값으로
    # 한 번만 자른다 — leg 별로 이 값을 각각 적용하면 먼저 처리된 leg 이 예산을 통째로 가져가고
    # 뒤 leg 은 0개가 된다(사용자가 명시한 두 번째 니즈가 검색조차 안 되는 조용한 손실).
    # 확장 leg 수 상한. category_fanout_max 와 같은 이유로 le=MAX_LISTS — 확장 턴이 case 3 과
    # 겹치면 leg 마다 목록이 생겨 계약 상한(§4.2 lists ≤ 10)을 넘긴다.
    category_expand_legs: int = Field(default=8, ge=0, le=MAX_LISTS)
    category_expand_notice_enabled: bool = True  # 확장 고지 문구 on/off
    # 확장 leaf 의 중분류(leaf 이름의 " > " 앞부분, 중복 제거) 목록을 끼울 자리 하나({items}).
    # 문구는 LLM 이 짓지 않는다 — DB 값 그대로 조립해 존재하지 않는 카테고리를 말하지 않는다(#59 재발 방지).
    category_expand_notice: str = "{items} 에서 관련 상품을 찾아봤어요."
    # [#343] 확장 턴에서 검색은 히트를 냈는데 `_post_filter`(최근구매 exact 제외 + 소모품 카테고리
    # 억제)가 전량을 지워 candidates 가 0이 되는 갭을 무필터 재검색으로 구제한다. 결함을 고치는
    # 플래그는 기본 on(팀 방침) — 하방이 유계(추가 왕복은 상호배타 가드로 턴당 최대 1회분)라
    # off 로 안전하게 시작할 근거가 없다. "재검색 상한" 수치 config 는 따로 두지 않는다 — 재검색은
    # 결정론적(같은 쿼리 = 같은 결과)이라 2회 이상 시도가 무의미하고, 턴당 무필터 재검색 1회는
    # `category_expand_notice_suppressed` 상호배타 가드로 구조적으로 강제된다.
    category_expand_post_suppress_fallback_enabled: bool = True

    # ── Case 3 니즈별 그룹 출력 (이슈 #168) ──
    # split 턴의 니즈당 rerank 입력 후보 quota. 실측(실 카탈로그 leaf 폭 9~17): merge_cap=30 은
    # 5니즈 턴에서 니즈당 6개로 자연 공급량보다 아래를 절단해 per-need expose_max(9) 도달 불가.
    category_group_per_need_candidates: int = Field(default=10, ge=1)
    group_notice_enabled: bool = True  # 니즈별 그룹 서술 on/off
    # 니즈 그룹 서술 자리 하나({items}) — "라벨1 N개 · 라벨2 M개" 형태로 결정론 조립한다
    # (#222 확장 고지와 같은 패턴, LLM 이 짓지 않는다).
    group_notice: str = "니즈별로 나눠 담았어요 — {items}"

    # ── 목적·상황형 발화의 상품 전개 (이슈 #198·#217, DESIGN-NEEDS-EXPANSION-198) ──
    # "집들이 선물" 처럼 무엇을 살지 사용자가 말하지 않은 발화를 구체 상품 목록으로 전개한다
    # (정본 SPEC-RECOMMEND-001 §5.1 shopping_list 분해, EX-7 v0.10.0 개정으로 전용 호출 허용).
    # 전개 실패는 **코드가 결정적으로 감지**한다(설계 §4) — LLM 자기 보고(case)는 전개와 같은
    # 호출의 산출물이라 실패 회차의 값을 신뢰할 수 없음이 실측으로 확인됐다(§4.1).
    #
    # [#217] 감지 튜너블이 **없다.** 트리거가 목적 marker 열거에서 "매핑 실패"로 바뀌면서
    # `needs_expansion_purpose_markers` 를 폐기했고(§4.0), 판정은 기존 카테고리 임계
    # (`category_distance_max`·`category_distance_override_margin`·`category_select_margin_max`)를
    # 그대로 재사용한다. 전개 전용 거리 임계를 두는 안은 실측으로 기각됐다(§4.5 ④) — 임계를 낮춰
    # 얻는 회수보다 정상 상품명 오탐이 커서 교환비가 맞지 않았다.
    needs_expansion_enabled: bool = True  # 전개 단계 on/off(롤백 스위치)
    # 전개 호출 tier. `fast` 로 시작한다 — §2 의 실패는 "fast 라서"가 아니라 "한 호출에 6가지 작업이
    # 얹혀서"였으므로, **단일 작업 전용 호출**의 fast 성능은 별개 측정 대상이다(설계 OPEN-2).
    # 실측 미달 시 "smart" 로 승격한다.
    # Literal 로 좁힌다 — 이 값은 `resolve_model_id` 에 들어가고 그것은 미지 tier 에 LLMError 를
    # 던진다. 전개는 그 호출을 관측 기록(§6.3)보다 **뒤에** 하므로(needs_expansion.py) 오타 하나가
    # 퇴화가 아니라 턴 예외가 된다. 잘못된 설정은 부팅 시 pydantic 이 막는 게 맞다.
    needs_expansion_tier: Literal["fast", "smart"] = "fast"
    # 이 개수 미만이면 전개 실패로 본다 — 1개면 발화 복사로 되돌아가므로 최소 2개.
    needs_expansion_min_items: int = Field(default=2, ge=1)

    # ── 카테고리 범위 해제 분류기 (이슈 #84) ──
    # "5만원 이하 아무거나" 처럼 **직전 카테고리를 놓겠다**는 발화를 판정하는 전용 호출.
    # decompose 프롬프트 안의 필드(`categoryAction`)로 받는 안은 **실측으로 기각**됐다 — fast
    # 티어에서 리셋 기대 32건 중 clear 산출이 0~6건이었고(문면 후보 6종), 같은 프롬프트를 smart 로
    # 재면 32/32 였다. 짧은 전용 호출은 fast 에서도 32/32 · 오탐 0/56(독립 3회)이다. 즉
    # needs_expansion 과 같은 구조의 문제이고 같은 처방을 쓴다(app/.../category_scope.py 표 참조).
    category_scope_classifier_enabled: bool = True  # 롤백 스위치(끄면 호출 0회 = 오늘 동작)
    # Literal 로 좁힌다 — 위 `needs_expansion_tier` 와 같은 이유다. 이 값은 `resolve_model_id` 에
    # 들어가고 그것은 미지 tier 에 LLMError 를 던지므로, 오타가 퇴화가 아니라 예외가 된다
    # (분류기는 그 예외를 삼켜 None 으로 떨어뜨리지만, 그러면 기능이 조용히 죽는다).
    category_scope_tier: Literal["fast", "smart"] = "fast"
    # 산출이 `{"scopeFree": true|false}` 한 줄이라 32 토큰이면 충분하다.
    category_scope_max_tokens: int = Field(default=32, ge=8)

    # ── 장바구니 (이슈 #3, api-spec §4.1) ──
    # CART_OPTION_INVALID 재질문 상한 — 초과 시 action CART_ERROR(§4.1). 하드코딩 금지.
    cart_option_reask_max: int = 1
    # 옵션 되물음 중 상품 전환 신호(#253). 한국어 조사·활용을 흡수하도록 각 표지가 발화에
    # 포함되는 한 방향만 비교한다. "아니"는 간투사 오탐이 흔하고 에코 보완 이득은 1/8뿐이라
    # 기본값에서 제외한다(운영 설정으로 재추가 가능).
    cart_pending_switch_markers: list[str] = ["다른", "말고", "대신", "바꿔", "바꿀"]
    # [#118] last_reco 누적 상한 — 담기 허용 목록(정본 §3.1 [보안] "누적 추천 목록 ∪
    # screen.products")의 시간 축을 보존하되 무한 증가를 막는다. **상한은 승계분에만 실효적으로
    # 걸린다** — 이번 턴 항목은 잘리지 않는다(CartStateStore.set_last_reco 주석 참조).
    # 값 근거: 한 턴 최대는 I-21 의 MAX_LISTS(10) × LIST_MAX_PRODUCTS(9) = 90 이지만, 통상
    # 추천 턴은 category_fanout_max(5) 이하 × 9 = ≤45, 실측 대다수는 1~3 leg(9~27건)다.
    # 30 이면 통상 한 턴 전체 + 직전 턴 승계분을 담으면서 LAST_RECOMMENDATIONS 길이를 #234/#240
    # 기준선을 잰 규모 근처로 유지한다. 90 으로 잡으면 목록이 3배가 되어 "LLM 오추출 표면을
    # 넓히지 않는다"(2026-07-30 계약 코멘트)와 어긋난다. screen_products_max(20)와도 같은 자릿수다.
    last_reco_max: int = Field(default=30, ge=1)

    # ── 장바구니 삭제 · 찜 (이슈 #116·#117, I-24~I-28 — 확정 2026-08-05, Spring 구현 진행 중) ──
    # [라운드 23] 삭제·찜 흐름의 온/오프를 가리던 두 설정 필드(기본 False)를 삭제했다(사용자
    # 지시 — 플래그를 두지 말고 항상 켜라) — 계약이 확정됐으니 판정이 나오면 항상 해당 흐름으로
    # 위임한다. Spring 이 아직 배포 전이라 실호출은 실패로 degrade하지만(§4.12~4.16), 그 실패는
    # AI 쪽 설정이 아니라 상대 서버 상태의 문제라 AI 코드에 게이트를 둘 이유가 없다.
    # 삭제 발화 표지 — "빼" 같은 짧은 조각은 오탐(빼곡·빼고·뺴빼로)이 흔해 쓰지 않는다. 어미까지
    # 포함한 동작 구만 잡는다("하나 빼고 담아줘"의 "빼고"는 여기 없음 — 삭제 지시가 아니다).
    # [라운드 2 리뷰] 제거해줘·빼 주세요·지워 주세요 추가 — 흔한 변형이면서 전부 어미까지 갖춘
    # 동작 구라 부분 문자열 오탐 위험이 없다.
    cart_remove_markers: list[str] = [
        "빼줘",
        "빼주세요",
        "빼 줘",
        "지워줘",
        "삭제해줘",
        "제거해줘",
        "빼 주세요",
        "지워 주세요",
    ]
    # 담기 표지 — 삭제/찜 표지와 같은 발화에 함께 있으면 담기가 강한 신호로 우선한다(§4.1
    # "찜한 거 담아줘"·"하나 빼고 담아줘" — 강한 신호는 약한 신호로 덮지 않는다, docs/lessons.md).
    cart_add_markers: list[str] = ["담아", "장바구니에 넣"]
    # 담기 표지의 과거 참조 꼬리(2차 리뷰 N-1) — "담아"는 "담아뒀던"·"담아둔"처럼 과거 참조형에도
    # 부분 문자열로 걸린다("전부" ⊂ "전부터"와 같은 사고, docs/lessons.md 재발). `intent_guard.py`
    # 가 이 표지 뒤 짧은 창(부정 표지와 같은 창)에 이 목록 중 하나가 오면 그 출현을 동작 요청이
    # 아닌 것으로 친다(`wishlist_reference_markers` 가 "찜한"을 지시 수식어로 다루는 것과 같은
    # 개념). **한 글자여도 되는 이유**: 이 목록은 발화 전체에서 부분 문자열 검색을 하지 않고
    # 표지 직후의 좁은 창 안에서만 본다 — "전부"⊂"전부터"류처럼 발화 어딘가의 다른 단어에
    # 우연히 묻히는 오탐 구조가 아니다.
    cart_add_reference_markers: list[str] = ["뒀", "둔", "두었", "놨", "놓"]
    # 찜 추가 표지 — "찜해줘"는 "이거 찜해줘" 처럼 앞에 다른 말이 붙어도 부분 문자열로 잡힌다.
    # "찜 해주세요"는 띄어쓰기 변형이라 별도 표지로 둔다. 위시리스트는 동의 표현.
    # [라운드 2 리뷰] 찜해주세요(붙여쓰기)·찜해 줘·찜 목록에 추가·위시리스트에 넣어 추가 — 전부
    # 어미까지 갖춘 동작 구라 오탐 위험이 없다.
    wishlist_add_markers: list[str] = [
        "찜해줘",
        "찜 해주세요",
        "찜해주세요",
        "찜해 줘",
        "찜 목록에 넣어줘",
        "찜 목록에 추가",
        "위시리스트에 추가해줘",
        "위시리스트에 넣어",
    ]
    # 찜 해제 표지. [라운드 2 리뷰] 찜 취소·찜에서 지워 추가.
    # ⚠️ "찜 빼줘"는 cart_remove_markers 의 "빼줘"도 부분 문자열로 동시에 매칭한다 —
    # classify_cart_utterance 의 판정 순서(찜 해제 → 찜 추가 → 삭제 → 담기)가 이 충돌을
    # 해소한다(찜 해제를 삭제보다 먼저 본다). 표지 목록 자체는 겹침을 허용하고 순서로 정리한다.
    wishlist_remove_markers: list[str] = [
        "찜 빼줘",
        "찜 해제해줘",
        "찜에서 빼줘",
        "찜 취소",
        "찜에서 지워",
    ]
    # 찜 지시 표지 — "찜한 거 담아줘"·"찜해둔 이어폰 담아줘"류에서 찜은 지시 대상을 수식할 뿐
    # 동작이 아니다. 이 표지가 있으면 찜 판정에 개입하지 않는다(그 발화의 동사는 담기다).
    wishlist_reference_markers: list[str] = ["찜한", "찜해둔", "찜해 놓은", "찜했던"]
    # 부정·유보 표지(2차 리뷰 지적 1·2·3, `intent_guard.py::_matches_unnegated`) — 표지 출현
    # 바로 뒤 짧은 창 안에 이 표지 중 하나가 오면 그 표지 출현은 없는 것으로 친다("장바구니에
    # 넣지는 마" 의 담기 표지 무효화, "빼줘야 할까"의 삭제 표지 무효화). 이 규칙은 **개입을
    # 줄이는 방향**이라 오탐해도 오늘 동작(담기)으로 되돌아갈 뿐이라 넓게 잡아도 안전하지만,
    # "않"처럼 짧은 조각은 다른 단어에 묻히므로(라운드 3 F-1 과 같은 이유) 어절 단위로 온전한
    # 것만 담는다 — 지 마/지는 마/지마: "~하지 마"류 금지(종결형, "빼지 마"). 지 말: "~하지
    # 말고"·"~하지 말아"·"~하지 말라"·"~하지 말자"류 활용형 전체를 어간("말")에서 잡는다
    # (라운드 12 — "지 마" 만으로는 "지 말고"를 못 잡아, 이름과 부정 사이에 다른 낱말이 끼어
    # "말고"가 창 밖으로 밀리면 "지 말"만 남은 창에서도 놓쳤다: "이어폰은 찜 빼지 말고 케이스
    # 찜 빼줘"에서 "이어폰" 뒤 8자 창 "은 찜 빼지 말"에 "말고"는 안 들어와도 "지 말"은 들어온다).
    # 하지 마: 위 표지가 못 잡는 "동사 없이 하지 마"류 보강. 말고: "A 말고 B" 대조·배제(표지가
    # 이름 바로 뒤에 오는 경우). 야 할/야 될: "~해야 할까?" 류 의문·유보.
    utterance_negation_markers: list[str] = [
        "지 마",
        "지는 마",
        "지마",
        "지 말",
        "하지 마",
        "말고",
        "야 할",
        "야 될",
    ]
    # 부정·유보 표지를 찾는 창 크기(문자 수) — 표지 출현 끝 위치부터 이 길이만큼만 본다.
    # "장바구니에 넣지는 마"(담기 표지 뒤 "지는 마"까지 4자)·"빼줘야 할까"(삭제 표지 뒤 "야
    # 할"까지 3자)를 모두 덮으면서, 창을 과하게 넓히면 표지와 무관한 뒷문장의 부정이 잘못
    # 끌려오므로 6~8자 범위 중 여유를 둔 8로 잡았다.
    utterance_negation_window: int = 8
    # 접두 부정 표지(라운드 9, `intent_guard.py::_has_prefix_negation`) — 한국어 부정은 어미
    # (위 `utterance_negation_markers`, 표지 **뒤**)뿐 아니라 부사 **접두**(안·못)로도 온다
    # ("안 빼줘도 돼"). `utterance_negation_markers` 와 **합치지 않는다** — 검사 방향이
    # 반대(하나는 표지 뒤를 보고, 하나는 표지 앞을 보는)라 한 목록에 두면 코드가 어느 방향으로
    # 검사할지 표지 문자열만으로는 알 수 없다. "안"은 한국어에서 극히 흔한 조각이라("안경"·
    # "안쪽"·"가방 안에") 부분 문자열 검색을 그대로 쓰면 정상 발화를 대량으로 삼킨다 —
    # `_has_prefix_negation` 이 어절 경계(앞이 문자열 시작/공백, 표지와의 사이 공백 0~1개)로만
    # 판정해 "안경 빼줘"·"가방 안에 있는 거 빼줘" 같은 정상 삭제 요청은 죽이지 않는다.
    utterance_prefix_negation_markers: list[str] = ["안", "못"]
    # 상품명 매칭 경계 — 오른쪽 조사 허용(이슈 #116·#117, 라운드 15, head `0b33e06` 리뷰 B).
    # `remove.py`/`wishlist.py` 의 이름 매칭은 부분 문자열이라 "이어폰케이스"의 "이어폰"처럼
    # 다른 낱말에 파묻힌 이름까지 오탐했다. 순진하게 "뒤가 공백/문장부호가 아니면 무효"로만
    # 자르면 "그 세제를 빼줘"(목적격 조사 "를"이 이름 바로 뒤에 붙는 정상 발화)가 깨진다 —
    # 그래서 오른쪽 경계에 조사도 허용한다. 최소 집합만 담는다(주격·목적격·보조사·접속·관형격·
    # 처격·방향격·나열 각 1~2개) — 모든 조사를 망라하려 하지 않는다(그 시도 자체가 새 오탐
    # 표면). "나"/"이나"(나열·선택 접속조사)는 패킷 최소 집합엔 없었지만 기존 회귀 테스트
    # ("파우치 블루나 파우치 레드 찜 빼줘")가 이 조사 뒤 이름까지 매칭돼야 모호 판정(2건)이
    # 유지되므로 추가했다 — 빠지면 "블루"만 boundary 를 통과해 단일 매칭으로 오판된다.
    utterance_name_boundary_particles: list[str] = [
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "도",
        "만",
        "과",
        "와",
        "랑",
        "이랑",
        "나",
        "이나",
        "의",
        "에서",
        "에",
        "으로",
        "로",
    ]
    # 이름 뒤 filler 낱말(이슈 #116·#117, 라운드 17, head `6ab47c9` 리뷰) — "이름 매칭 뒤 검사"
    # (§ `negation.matches_name_unnegated` docstring)에서 조사 소비 후에도 표지가 바로 오지
    # 않고 이 낱말들이 끼어 있으면 건너뛴다("이어폰 좀 빼줘"가 여전히 매칭돼야 한다). 뜻 없이
    # 발화를 채우는 부사·수량사류로만 좁힌다 — 명사(상품명이 될 수 있는 말)는 절대 넣지 않는다
    # (그러면 "이어폰 케이스 빼줘"의 "케이스"가 filler로 삼켜져 라운드 17 이 고치려는 바로 그
    # 버그가 되살아난다).
    utterance_name_trailing_filler_words: list[str] = [
        "좀",
        "지금",
        "다시",
        "그냥",
        "일단",
        "빨리",
        "하나",
    ]
    # 삭제 대상 해소 — 전체 삭제 표지(이슈 #116, 패킷 §5.3). 결과가 이 판별기에서 가장
    # 파괴적인 규칙(장바구니 전체 삭제)이라 표지도 가장 엄격하게 잡는다 — **명사 하나만
    # 있는 표지는 금지**한다. "전부"는 온전한 단어처럼 보여도 "전부터"의 부분 문자열이라
    # ("전부터 쓰던 거 빼줘" 오탐, 라운드 3 리뷰 F-1 재현) 동작 구로만 구성한다. "다" 한
    # 글자도 "다른"·"다시"류와 겹쳐 같은 이유로 쓰지 않는다.
    cart_remove_all_markers: list[str] = [
        "전부 빼",
        "전부 지워",
        "전부 삭제",
        "다 빼",
        "다 지워",
        "모두 빼",
        "모두 지워",
    ]
    # 삭제 대상 해소 — "방금 담은 거" 표지. `CartStateStore.get_last_add` 의 cartItemId 로
    # 이어진다(이슈 #116, 패킷 §5.3). [라운드 3 리뷰 F-1] 위 전체 삭제 표지와 달리 동작 구로
    # 좁히지 않는다 — 이쪽은 결과가 "마지막에 담은 1건"뿐이라 파괴력이 훨씬 낮고, "방금"은
    # 부사라 한국어에서 다른 단어의 앞부분으로 잘 묻히지 않는다(전부→전부터 같은 오탐 형태가
    # 없다).
    cart_remove_recent_markers: list[str] = ["방금", "아까 담은", "마지막에 담은"]

    # ── dedup (#4, api-spec §4.7 결정 14-F) ──
    # 최근 구매 제외 윈도우(일) — 이보다 오래된 구매는 제외 목록에서 뺀다(영구 제외 방지).
    dedup_recent_days: int = 90
    # 소모품 카테고리(결정 14-F 억제 대상) — MVP config 소스. 정본은 catalog 속성사전
    # (SPEC-CATALOG-DATA-001 REQ-CAT-013 소모품 boolean 플래그). 카테고리명은 BE categoryName 과 일치.
    consumable_categories: list[str] = []
    # [#120] repurchaseProducts 파싱 개수 상한 — LLM 의 긴 목록을 유계 입력으로 유지한다.
    # 실제 해제는 graph 가 단일 지목만 신뢰하므로 이 값이 해제 범위를 넓히지는 않는다.
    # category_fanout_max 와 같은 슬라이스 절단 규약(raw[:cap])이라 음수를 거부한다 — 음수면
    # "뒤에서 |cap|개 제외"로 뒤집혀 "cap<=0 이면 정확히 0개" 불변식이 깨진다(PR #230 리뷰).
    dedup_repurchase_max: int = Field(default=5, ge=0)
    # 스레드별 재구매 되돌리기 누적 상한(무한 누적 방어, 이슈 #232). 음수는 상한 의미를
    # 뒤집으므로 형제 튜너블과 같이 거부한다(PR #230 리뷰).
    dedup_repurchase_store_max: int = Field(default=20, ge=0)

    # ── 프로필 (SPEC-PROFILE-001) ──
    profile_recency_highlights: int = 3  # §5.1 최근 맥락 하이라이트 개수
    profile_gate_threshold: float = 0.5  # §6.3 승격 게이트 임계(salience·repetition EMA)
    profile_fact_char_cap: int = 200  # "기억해" hot-path fact 길이 상한(오탐·남용 방어)
    profile_max_facts: int = 200  # 사용자별 fact 개수 상한(무제한 누적 방어) — 최신 우선 유지
    # get_facts/add_fact 의 asearch 조회 상한 여유치 — 트리밍 직후에도 asearch 가 즉시
    # profile_max_facts 이하로 수렴한다는 보장이 없어(동시 add_fact 레이스 등) cap 을
    # 그대로 쓰면 경계에서 최신 fact 가 잘릴 수 있다(PR #47 후속 리뷰).
    profile_facts_query_margin: int = 50
    profile_session_buffer_cap: int = 100  # 세션 transient 버퍼 발화 개수 상한(무제한 누적 방어)

    # ── 프로필 개인화 강도 (이슈 #119, SPEC-PROFILE-001 §5.1 v0.6.0 · REQ-REC-005-A) ──
    # 프로필을 **어느 소비처에** 주입할지. 기본 rerank_only 인 근거: decompose(fast tier, 한 호출에
    # intent 라우팅+필터+장바구니 의도가 얹힌다)의 _SYSTEM 에 프로필 사용 규칙이 없어 LLM 이 취향을
    # priceMax/brand/color 하드필터로 승격시키고, 그 필터가 thread_store 에 영속돼 다음 턴
    # PRIOR_FILTERS 로 재주입되며 세션 내내 후보를 좁힌다(래칫). 게스트는 이 입력이 없어 손실이 0이라
    # 개인화가 순손실이 됐다. 주입을 끊으면 회원 decompose 프롬프트가 게스트와 바이트 동일해진다.
    # 프롬프트 규칙을 얹지 않고 **입력을 제거**하는 이유: 지시 한 줄은 공짜가 아니다(#198/EX-7 —
    # 지시 추가로 기존 성공 케이스가 3/3 → 1/3 로 희석된 실측 전례, rerank.py 주석 참조).
    profile_injection_scope: ProfileInjectionScope = "rerank_only"
    # rerank 의 프로필 사용 강도. 채팅 경로에 연속 가중치(*_weight)를 두지 않는 이유: 전략 A(LLM
    # 재랭킹)는 점수가 아니라 순위 목록(RerankResult.ranked)을 산출해 **가중합할 스칼라 자체가
    # 없고**, "이 상품이 취향에 맞는가"의 ground truth 도 없다(평가 하니스 미구현 — #142/#143).
    # 코사인 임계 하나(category_distance_max)를 정하는 데도 앵커 76개 실측이 필요했다(#115) —
    # 정답셋 없이 정한 계수는 튜너블이 아니라 마술 상수다. 위 home_reco_weight_profile(#148)이
    # 가중치인 것과 모순이 아니다: 홈 추천은 질의 벡터가 임베딩 가중평균이라 스칼라가 실재하고
    # 누를 발화도 없다. **점수가 있는 표면에는 가중치, 없는 표면에는 스코프 스위치**다.
    # 채팅 경로의 연속 가중치는 전략 B(scoring) 도입 시(#145) 함께 정의한다.
    profile_rerank_influence: ProfileRerankInfluence = "tiebreak"
    # 세션 버퍼에 정규화 동일 발화를 몇 번까지 담을지(REQ-PROF-026). 버퍼는 델타 추출 LLM 에
    # "\n".join 으로 통째로 실리고 LLM 이 그 중복을 보고 repetitionEma 를 산출하므로
    # **버퍼 중복이 곧 반복성 점수**다 — 같은 말 3~4회로 취향이 과대 대표된다.
    # **0/1 로 낮추지 말 것**: 게이트가 `salience AND (explicit OR repeated)`(gate.should_promote)
    # 라 반복은 명시 표명 없이 승격시키는 **독립 경로**인데, 1 건만 남기면 LLM 이 반복을 볼 수
    # 없어 그 경로가 통째로 죽는다. 세션 간 누적(GateState)은 미구현이라(SPEC-PROFILE-001
    # OPEN-P12) 승격 못 한 델타는 버려지므로, 다음 세션이 대신 살려주지도 않는다.
    # 기본 2 = "반복했다"는 관측 가능한 최소치. 4회든 10회든 2 로 보여 증폭만 잘린다.
    # 상한을 완전히 끄려면(종전 동작) profile_session_buffer_cap 이상으로 올린다.
    profile_buffer_repeat_cap: int = Field(default=2, ge=2)
    # 취향 신호가 **구조적으로** 없는 intent 만 버퍼에서 뺀다(REQ-PROF-026) — 주문조회
    # ("주문 어디까지 왔어")·장바구니 조회("장바구니 보여줘")는 상태 조회라 원하는 게 뭔지에
    # 대한 정보가 0인데, 매 세션 반복되며 슬라이딩 윈도우를 채워 정작 취향 발화를 밀어낸다.
    #
    # general·cart_add 는 **일부러 남긴다**(PR #223 리뷰 확인):
    #  - general: "나 소니 좋아해" 같은 명시적 취향 표명이 잡담 턴으로 들어온다.
    #  - cart_add: 담기는 채팅 레인에서 **구매에 가장 가까운 행동 신호**다. 명세도 write 소스를
    #    conversation|purchase 로 두고(REQ-PROF-024) 구매 소스는 명시성 없이 반복성·현저성으로
    #    승격한다(REQ-PROF-044) — 제외하면 명세가 인정한 신호원을 코드가 막는다. 발화 자체도
    #    취향을 실어 나른다("M 사이즈로", "검정으로", "소니 거 담아줘").
    #
    # 판단 기준은 **실수의 비대칭**이다: 취향 있는 intent 를 빼면 신호가 영구히 사라져 복구할
    # 방법이 없는 반면, 노이즈("그거 담아줘")를 넣으면 델타 추출 LLM 이 걸러내고(_DELTA_SYSTEM
    # "일회성 잡담·잡음은 제외") 게이트가 한 번 더 막으며 버퍼 상한·반복 상한이 방어한다.
    # 되돌릴 수 없는 실수를 되돌릴 수 있는 실수보다 무겁게 본다(#119 전체와 같은 논리).
    #
    # cart_remove·wishlist_remove(이슈 #116·#117)는 **버퍼에서 제외한다** — 다만
    # order_status·cart_view 와는 제외 이유 자체가 다르다. 저 둘은 취향 신호가 **0(노이즈)**인
    # 상태 조회지만, 삭제·찜 해제는 신호가 0 이 아니라 **부호가 반대인 신호**다. "이어폰 빼줘"에는
    # "이어폰"이라는 상품명이 멀쩡히 들어 있어 델타 추출 LLM 이 "일회성 잡담·잡음"으로 걸러낼
    # 근거가 오히려 약하고, 걸러지지 않으면 **사용자가 방금 치운 상품이 선호로 학습된다.** 위
    # "실수의 비대칭"(애매하면 넣는다)은 "노이즈를 넣는 실수 vs 신호를 놓치는 실수" 구도를
    # 전제하는데, 역신호가 새는 것은 그 저울에 올릴 문제가 아니라서 이 판단을 그대로 적용하지
    # 않는다(방어선 세 겹은 노이즈를 걸러내도록 만들어진 것이지 반대 부호 신호를 걸러내리라는
    # 보장이 없다).
    # `wishlist_add` 는 **버퍼에 남긴다(목록에 넣지 않는다)** — `cart_add` 와 같은 긍정 행동
    # 신호이고 바로 위 문단의 이유(REQ-PROF-024/044, 발화 자체가 취향을 실어 나름)가 그대로
    # 적용된다.
    profile_buffer_excluded_intents: list[str] = [
        "order_status",
        "cart_view",
        "cart_remove",
        "wishlist_remove",
    ]
    # I-20 처리 중 claim lease. delta+consolidation LLM 2단계의 기본 최악시간(약 120s)보다
    # 길게 두되, 프로세스 crash 잔재가 영구 duplicate가 되지 않도록 유한하게 유지한다.
    session_end_claim_ttl_s: float = 180.0
    # 회원 발화 저장 시 touch한 DB activity 기준 프로필 버퍼 inactivity 종료(이슈 #79).
    profile_session_idle_timeout_s: float = 600.0
    profile_idle_sweep_interval_s: float = 60.0
    profile_idle_sweep_batch_size: int = 10
    profile_idle_max_concurrency: int = 2
    # batch=10/concurrency=2에서 2단 LLM 처리가 여러 wave로 이어져도 claim이 만료되지 않게 둔다.
    profile_idle_claim_ttl_s: float = 900.0
    session_lifecycle_legacy_grace_s: float = 86400.0
    session_lifecycle_legacy_quiet_s: float = 90.0
    session_lifecycle_gc_batch_size: int = 100
    session_lifecycle_backfill_max_batches: int = 1000

    profile_summary_max_chars: int = 1000  # §5.1 요약 상한(생성 측 압축 재작성)
    # AsyncPostgresStore(pg-profile) 초기 연결 대기 상한(이슈 #33) — 초과 시 dev 는 InMemory 폴백.
    # seller checkpointer 와 별개 설정(공유 시 두 서브시스템이 값 하나를 두고 경합하는 걸 방지).
    state_store_connect_timeout_s: float = 5.0
    # pg-profile 대화 저장 쿼리(save_user_message/finalize_assistant) 실행 상한 — 연결은 위 상한이
    # 있지만 매 요청 쿼리엔 없어, pg 가 응답 없이 멈추면 commit_user_message 가 영영 안 끝나 동시
    # 스트림 슬롯이 영구히 잠긴다(§2.9 a 슬롯 누수, PR #48 후속 리뷰). CLAUDE.md "타임아웃 전 구간".
    state_store_query_timeout_s: float = 3.0
    # 기존 conversation_turns 스키마 백필은 일반 요청 쿼리보다 오래 걸릴 수 있어 별도 상한을 둔다.
    state_store_migration_timeout_s: float = 30.0
    # lifespan 종료 콜백별 상한. psycopg_pool 자체 close 기본값과 같은 5초를 허용해 정상 worker
    # 종료를 바깥에서 더 일찍 자르지 않는다. 뒤 자원 몫은 아래 floor 예약으로 별도 보호한다.
    lifespan_resource_close_timeout_s: float = Field(default=5.0, gt=0.0, le=60.0)
    # 느린 자원 하나가 앞에서 cap을 써도 남은 각 자원에 최소 0.2초를 예약한다. 9개·8초 기준
    # 첫 자원도 6.4초 allowance를 받아 cap 5초를 온전히 쓴다. 자원 수·예산 변경 시 함께 재검토한다.
    lifespan_resource_close_floor_s: float = Field(default=0.2, ge=0.0, le=60.0)
    # 전체 cleanup 예산. 배포의 SIGTERM→SIGKILL 유예보다 작게 설정해야 한다. 현재 deploy.yml의
    # `docker stop`은 --time이 없어 Docker 기본 유예 10초에 의존한다. 이 관계는 기동 시 강제하지 않는다.
    lifespan_cleanup_budget_s: float = Field(default=8.0, gt=0.0, le=300.0)
    # libpq socket-level 이중 방어(이슈 #50). asyncio.wait_for/statement_timeout과 별개로
    # 네트워크 black-hole에서 커널이 연결을 유한 시간 내 폐기하도록 한다.
    state_store_keepalives_idle_s: int = 10
    state_store_keepalives_interval_s: int = 3
    state_store_keepalives_count: int = 3
    state_store_tcp_user_timeout_ms: int = 3000
    # BaseStore와 RMW advisory lock은 별도 pool을 쓰며 max_size는 각 pool의 동시성 상한이다.
    # last_reco 표시명은 원본 사본 금지라 로컬 bounded LRU만 사용한다.
    state_store_pool_min_size: int = 1
    state_store_pool_max_size: int = 10
    state_store_local_cache_max_entries: int = 10_000
    # PII 로그 지문 pepper (§6.3 b) — 운영(jwks)은 실제 secret 주입 필수(아래 검증). 빈 값은 개발용.
    pii_hash_pepper: str = ""
    # 사용자 message 길이 상한 (api-spec §3.1 · PII·메모리 방어). 튜너블.
    chat_message_max_chars: int = 4000
    # sessionId/threadId 길이 상한 — 불투명 키가 registry·저장소·로그에 쌓이는 남용 방어.
    chat_key_max_chars: int = 200

    # ── 화면 맥락 screen (이슈 #118, api-spec §3.1) ──
    # screen.products 상한(정본 명시 기본값) — 초과분은 화면 순서 앞쪽만 취하고 버린다.
    screen_products_max: int = Field(default=20, ge=1)
    # [13차 리뷰] `screen.products` **원본 배열** 길이 하드 상한 — `screen_products_max` 와는
    # 별개다. 스키마 정규화(`app.schemas.chat._normalize_screen`)가 불량 항목을 걸러 유효
    # `screen_products_max` 건을 채울 때까지 원본 배열을 순회하므로(12차 리뷰 이후 규약), 이
    # 상한이 없으면 무효 항목(빈 dict 등)을 수만~수십만 건 채운 요청이 매번 원본 전체를
    # 스캔한다 — `message` 는 `chat_message_max_chars` 로 길이 상한이 있는데 `products` 원본
    # 크기에는 상한이 없던 비대칭이었고, 요청 바디 크기를 자르는 미들웨어도 없어(레이트리밋은
    # §2.8 요청 "건수"만 제한) 이 경로가 열려 있었다(실제 재현). 기본 500은
    # `screen_products_max`(20)의 25배 — 정상 FE 페이로드(한 응답에 화면이 보여줄 수 있는 상품은
    # 무한 스크롤이어도 수십 건을 넘기 어렵다)는 절대 자르지 않으면서, 악성 페이로드의 스캔량을
    # 유계로 만드는 값이다. 원본 배열 슬라이스도 400 이 아니라 절단이다(관대 유효성 유지).
    screen_products_raw_scan_max: int = Field(default=500, ge=1)
    # screen 문자열(products[].name · filters 값) 항목당 길이 상한 — **FE 가 보낸 문자열이 그대로
    # LLM 프롬프트에 실리는** 신뢰경계라 절단이 필요하다(초과는 400 이 아니라 절단 — 관대 유효성).
    # 값 근거: 같은 카탈로그 상품명의 와이어 상한 선례가 200 자이므로(`OrderStatusOrder.product_name`
    # max_length=200) 그보다 긴 문자열은 실제 상품명일 수 없다. 그런데 200 을 그대로 쓰면 최악
    # 20건 × 200 = 4,000 자로 사용자 발화 상한(chat_message_max_chars=4000) 전체와 맞먹는 분량이
    # 프롬프트에 얹힌다. 120 이면 최악 2,400 자로 묶이면서 실제 한국어 커머스 상품명(브랜드+모델+
    # 옵션 수식, 통상 60~80 자)은 절단 없이 다 들어간다. filters 표시값("배송중"·"최신순")에는
    # 넉넉하다.
    screen_text_max_chars: int = Field(default=120, ge=1)
    # [14차 리뷰, F-17] screen 문자열(products[].name · filters 값) **원문** 길이 하드 상한 —
    # `screen_text_max_chars`(정제 후 절단 상한)와는 별개다. `app.schemas.chat._clean_screen_text`
    # 가 `_strip_unsafe`(제어·zero-width·bidi 검사, 문자 단위 순회)를 원문 **전체**에 먼저 돌린
    # 뒤에야 `screen_text_max_chars` 로 잘랐으므로, 원문 길이 자체에는 사전 상한이 없었다 —
    # `screen_products_raw_scan_max` 가 "원본 배열 길이"는 유계로 만들었지만 그 배열의 각 항목
    # **문자열 길이**는 열려 있던 비대칭이다. 실측: name 200만자 × 50건이 25.02초 걸렸고,
    # `screen_products_raw_scan_max`(500)까지 채우면 더 나쁠 수 있다 — 구매자 스트림 전체 상한
    # 30s(§2.9 c)를 넘겨 사실상 서비스 거부다. `screen_text_max_chars`(120)의 20배로 잡는다 —
    # 실제 상품명·필터 표시값은 정제 전 원문이라도 수백 자를 넘기 어렵고(위 200자 선례의 12배
    # 여유), 20배(2,400)면 정상 페이로드는 자르지 않으면서 악성 원문의 정제 비용을 유계로
    # 만든다. 관대 유효성은 유지한다 — 이 슬라이스도 400 이 아니라 절단이다.
    screen_text_raw_scan_max: int = Field(default=2400, ge=1)
    # 화면을 가리키는 **맨 지시대명사** 표지 — 이것만 있고 이름·순번·좌표가 없으면 정본 §3.1 의
    # "후보가 1건일 때만 확정, 여러 건이면 되물음"을 코드가 강제한다
    # (app/agents/buyer/screen_reference.py). 조사·활용을 흡수하도록 포함 관계로만 비교한다
    # (cart_pending_switch_markers 와 같은 규약). 운영에서 표지를 늘릴 수 있게 config 로 둔다.
    # **근칭만 둔다.** `"그거"`·`"그것"` 은 이 저장소에서 **대화 지시어**로 확립돼 있어(decompose
    # `_SYSTEM` 의 하중 문구가 `"그거 보여줘"`·`"그거 또 사고 싶어"` 를 직전 추천 맥락으로 다루고
    # #234 프로브가 그 경로를 측정했다) 화면 지시로 보면 직전 추천을 가리킨 발화가 화면 상품으로
    # 확정된다 — 리뷰 F-1 에서 실제 오담기로 재현됐다. 정본 §3.1 지시어 해소 표가 든 예도 `"이거"`다.
    # `"저거"` 는 화면에 보이는 것을 가리키는 원칭이고 대화 지시어 선례가 없어 남긴다.
    # [8차 리뷰, F-12] `"얘"` 는 뺐다 — 매칭이 포함 관계(부분 문자열)인데 이 표지만 1글자라
    # `"얘기했던 걸로 담아줘"`·`"얘들아 담아줘"` 처럼 무관한 단어(얘기·얘들아)에 걸린다. 그 발화들은
    # 대화 맥락 지시("얘기했던 것")이지 화면 지시가 아닌데, `context_reference_markers` 에도 안
    # 걸려 화면 후보가 1건이면 되물음 없이 **그대로 확정**됐다(실제 재현, 오담기). 목록의 나머지
    # 표지는 전부 2글자 이상이라 이런 우연 부분일치가 나지 않는다 — 포함 관계 비교 자체는 조사·
    # 활용을 흡수하려는 의도된 설계라 바꾸지 않고(위 주석), 이 표지만 뺐다.
    screen_deictic_markers: list[str] = ["이거", "이것", "요거", "요것", "저거", "저것"]
    # 발화가 **대화 맥락**을 명시적으로 참조하는 표지 — 있으면 화면 해소를 통째로 건너뛰고 LLM 에
    # 맡긴다(`"아까 추천해준 그거 담아줘"` 가 화면 상품으로 확정되던 리뷰 F-1). 좁게 유지한다:
    # 넓히면 정상적인 화면 지시까지 LLM 으로 넘어가 라운드 2가 되찾은 정확도를 잃는다.
    screen_context_reference_markers: list[str] = ["아까", "저번", "지난번", "이전에", "방금 전"]
    # pageType → 한글 표시명 매핑(정본: "AI 가 pageType→표시명 매핑을 config 로 갖는다").
    # decompose 프롬프트의 SCREEN 블록에 이 표시명이 실린다. **매핑에 없는 pageType 은 화면명을
    # 생략**한다 — 원시 pageType 문자열을 프롬프트에 흘리지 않는 것이 정본이 매핑을 AI 에 둔 이유다.
    # 실제 오는 3종만 채운다(나머지 11종은 E-1 page_view 전용).
    screen_page_type_labels: dict[str, str] = Field(
        default_factory=lambda: {
            "chat": "인기 상품",
            "seller_orders": "주문 관리",
            "seller_products": "상품 관리",
        }
    )

    # ── SSE 스트림 수명주기 (api-spec §2.9, 값은 config 기본값·운영 조정 가능) ──
    # first-token: 첫 이벤트까지 상한. 초과 시 스트림 시작 전이면 504, 후면 in-stream error.
    stream_first_token_timeout_s: float = 10.0
    # 스트림 전체 상한. 초과 시 done(finishReason "stop")으로 정상 절단.
    stream_total_timeout_s: float = 90.0
    # 구매자 전체 상한 — 판매자와 분리한다(#138). 판매자는 planner→워커 팬아웃→report→
    # verifier 로 구조적으로 길고 구매자는 decompose+rerank 2회뿐인데, 같은 90s 를 쓰면
    # 구매자 스트림이 목표(slo_total_buyer_ms 30s)의 3배 느슨한 상한으로 돈다.
    # 근거: 2026-08-02 로컬 실측(Spring 기동, 동시성 1, n=30) 구매자 total p95 10.5s ·
    # max 12.8s — 30s 는 실측 max 의 2.3배 여유이고 154턴 중 30s 초과는 0건이었다.
    stream_total_timeout_buyer_s: float = Field(default=30.0, gt=0.0)
    # disconnect 감지 폴링 간격 (취소 = 연결 종료, §2.9 b).
    stream_disconnect_poll_s: float = 0.5
    # AI→Spring 콜백 타임아웃 (§2.9 c, BE I-2 기준 통일). 실제 호출부에서 사용.
    spring_timeout_s: float = 3.0
    # [#427] I-1 검색 전용 타임아웃 — `spring_timeout_s`(전 구간 공용)와 분리한다. 기본값은
    # api-spec §2.9(c) "AI→Spring 전 구간 3s 통일"과 **같은 값**이다 — 이 필드 신설 자체가
    # 기본 배포의 타임아웃을 바꾸지 않는다. **상향은 계약 개정(§2.9(c)) 선행이 필요하다** —
    # 명세를 고치지 않고 이 값만 올리면 와이어 계약과 실제 동작이 어긋난다. 분리하는 이유는
    # #394 가 기각된 사유("스칼라 하나를 공유해 전 구간이 함께 늘어난다", §5.1)를 되풀이하지
    # 않기 위해서다 — 검색 예산을 구제 체인 공유 예산(DESIGN-SHARED-BUDGET-384 §5.1)에 맞춰
    # 조정할 때 I-2 담기·I-18/I-19 조회·I-3 인기·I-21 push·판매자 레인까지 함께 늘어나지
    # 않아야 한다. 소비처는 `spring_client.py::search_products` 뿐이다.
    spring_search_timeout_s: float = Field(default=3.0, gt=0.0)
    # [#133] I-1 검색 재시도 횟수 (SPEC-RECOMMEND-001 §오류처리가 이미 규정한 동작).
    # 타임아웃 3s 는 일시 지연이 재시도로 살아나는 폭인데 LLM 만 재시도를 갖고 검색은 0회였다.
    # **재시도가 의미 있는 실패만** 대상이다 — 타임아웃·연결 오류·응답 중단·5xx·일시 4xx(408·429). 4xx 계약 오류와 응답
    # 파싱 실패는 다시 불러도 같은 결과라 즉시 실패한다. 비멱등 호출(I-2 담기)에는 걸지 않는다.
    # 재시도 사이 sleep 은 두지 않는다 — 타임아웃 실패는 이미 3s 간격이 생기고 1회로는 herd
    # 증폭이 2배를 넘지 않는다.
    # **상한이 1인 이유(PR #235 리뷰)**: backoff 가 구현에 없다. 2·3 을 허용하면 "1 을 넘기려면
    # backoff 가 필요하다"고 적어 둔 위험을 설정 한 줄로 열어 주는 셈이라, **현재 구현이 감당하는
    # 값만** 받는다. 더 올리려면 backoff 를 먼저 만들고 이 상한을 함께 푼다.
    # [#394 한시적 조치] 기본값을 1→0 으로 내린다. 운영 실측(2026-08-06): I-1 이 SEARCH_FAILED 로
    # 떨어진 두 건 모두 Spring 은 200 을 줬다 — 실패가 아니라 3s 예산을 넘긴 지연이었다. 그 상태에서
    # 재시도는 성공했을 쿼리를 backoff 없이 즉시 한 번 더 돌려 Spring 부하만 2배로 만들고, 사용자는
    # 6초 뒤 실패를 받는다. **원복 조건**: BE 검색 쿼리 개선(리뷰 집계 비정규화, BE #395)이 배포되면
    # 재검토한다. 구매자 progress 이벤트(#289)로 first-token 관문이 풀릴 때도 함께 재검토한다.
    # `=1` 로 되돌리면 이 필드가 원래 규정하던 재시도 동작(위 주석)으로 완전히 복귀한다.
    spring_max_retries: int = Field(default=0, ge=0, le=1)
    # [#277] conditions 를 검색 뒤로 미룬 턴은 첫 이벤트 앞에 I-1 이 최대 2회 직렬이라,
    # 재시도까지 얹으면 first-token 상한을 넘어 이벤트 0건·504가 될 수 있다. 한 번의 일시
    # 지연을 살리는 대가가 턴 전체의 침묵이므로 기본값은 그 턴만 재시도를 끈다.
    # 원복 전제(구매자 progress 계약 등재 + 플래그 on)는 #396 으로 충족됐다. 그래도 원복은
    # 하지 않는다 — #394(커밋 2168e9b)가 이미 다른 이유(Spring 부하 실측)로 `spring_max_retries`
    # 기본값을 1→0 으로 내렸고, 이 필드의 원복 여부는 그 조치와 함께 판단해야 하는 별도
    # 결정이다. #396 이슈 본문도 이를 비범위로 못박았다.
    search_retry_on_deferred_conditions: bool = False
    # [#427, DESIGN-SHARED-BUDGET-384 §3 D7] 구제 체인(F-1/#343/자동완화 probe) 예산 집행
    # 강도 — observe: 판정만 계산·로그(반사실), 실제 집행 없음(기본, 오늘 동작 불변).
    # narrow: 잔여 예산이 모자란 단의 타임아웃을 좁혀 시도한다(건너뛰지 않는다). narrow_skip:
    # narrow 로도 부족한(최소 하한 미만) 단은 건너뛴다. §4 Lv0~Lv2 등급의 런타임 스위치이며,
    # #394(spring_max_retries) 원복은 이 값을 narrow 이상으로 함께 올려야 한다(§4 결론).
    rescue_budget_mode: Literal["observe", "narrow", "narrow_skip"] = "observe"
    # 구제 체인 한 단에 줄 수 있는 최소 타임아웃 — 미만이면 시도해도 성공 가망이 없다고 보고
    # narrow_skip 모드에서 그 단을 건너뛴다(본검색 제외, 본검색은 항상 시도한다). 실측(#385)
    # 전 잠정값 — DESIGN-SHARED-BUDGET-384 §3 D7 "예: 0.5"를 그대로 채택한다.
    rescue_stage_min_timeout_s: float = Field(default=0.5, gt=0.0)
    # 구제 체인 잔여 예산 계산의 데드라인에서 미리 떼어 두는 꼬리(rerank·I-21 push) 몫 —
    # `rescue_deadline = turn_started_at + (stream_total_timeout_buyer_s - 이 값)`
    # (DESIGN-SHARED-BUDGET-384 §3 D2). rerank 실 p95 는 #385 실측 전까지 **미확인**이라,
    # 보수적으로 `llm_timeout_s`(30.0)의 절반을 예약해 둔다 — 실측 후 좁힌다(§3 D2 "꼬리 예약").
    rescue_tail_reserve_s: float = Field(default=15.0, ge=0.0)
    # [#132 PR #293 리뷰] I-1 응답 파싱 **전용** 스레드풀 크기. `asyncio.to_thread` 의 앱 전역
    # 기본 executor 를 쓰면, 총시간 가드가 버린(=await 는 취소됐지만 계속 도는) 파싱 스레드가
    # 임베딩·카테고리 매핑·색상 사전과 같은 풀을 놓고 경쟁해 무관한 요청까지 대기시킨다.
    # 기본값은 CPython 기본 executor 와 같은 식(min(32, cpu+4))이라 **격리만 바뀌고 동시 처리량
    # 특성은 그대로**다 — 이 값을 줄이는 것은 파싱을 직렬화해 다른 작업 몫을 늘리는 트레이드다.
    search_parse_max_workers: int = Field(
        default_factory=lambda: min(32, (os.cpu_count() or 1) + 4), ge=1
    )
    # AI→LLM 단일 호출 타임아웃 + 재시도 횟수 (§2.9 c).
    # 현행 30s×(1+1)=60s 최악 예산은 구매자 전체 상한 30s(stream_total_timeout_buyer_s, #138)를 넘는다.
    # timeout 뒤 재시도는 buyer done(stop) 절단 전에 끝날 수 없지만 빠른 오류 재시도는 여전히 유효하다.
    # 구매자 상한은 재시도를 모두 담는 예산이 아니라 대기 백스톱이라 기동 불변식으로 묶지 않는다.
    # 단일 호출 실측 p95는 4.3s다. 이 값을 올릴 때는 구매자 상한과의 관계도 함께 검토한다.
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 1

    # ── 관측 집계 SLO·degrade 알림 (scripts/aggregate_observability.py 주입, EVAL-OBS §3.3·§5) ──
    # 런타임 동작을 바꾸지 않는 **집계 리포트 전용 목표치**다. 위의 스트림 상한은 "언제 끊나"이고
    # 이 값들은 "무엇을 지켰어야 하나"라서 별도로 둔다 — 상한을 SLO 로 재사용하면 상한 조정이
    # 곧 목표 조정이 돼버린다. 역할별 total 목표 분리(판매자 90s·구매자 30s)는 EVAL-OBS §5
    # 제안값이며, 런타임 상한도 역할별로 분리됐지만 SLO 와 상한은 여전히 별개 값이다(#138).
    slo_first_token_ms: int = Field(default=10_000, gt=0)
    slo_total_seller_ms: int = Field(default=90_000, gt=0)
    slo_total_buyer_ms: int = Field(default=30_000, gt=0)
    # degrade 율이 이 비율을 넘으면 집계 스크립트가 non-zero exit 으로 CI·cron 에 표면화한다.
    degrade_rate_alert_threshold: float = Field(default=0.10, ge=0.0, le=1.0)
    # 표본이 적으면 비율이 요동치므로(1/3 = 33%) 이 표본 수 미만이면 알림하지 않는다(오탐 방지).
    degrade_alert_min_samples: int = Field(default=50, ge=0)

    # ── 레이트 리밋 (api-spec §2.8, 토큰 sub 스코프, 인메모리·단일 인스턴스 전제) ──
    rate_limit_per_min: int = 10
    rate_limit_per_hour: int = 100
    # IP 백스톱 배수 — 토큰 sub 스코프를 회전 우회해도 클라이언트 IP 상한으로 남용 차단.
    # NAT 뒤 다수 정상 사용자 오탐을 줄이려 sub 상한보다 관대하게 둔다.
    rate_limit_host_multiplier: int = 5
    # 신뢰 리버스 프록시 뒤 배포 시 True — 클라이언트 IP 를 X-Forwarded-For 에서 읽는다.
    # append 형 프록시($proxy_add_x_forwarded_for)는 자사 프록시가 관측한 IP 를 **최우측**에
    # 붙이므로, 우측에서 신뢰 홉 수만큼 센 위치를 클라이언트 IP 로 쓴다(최좌측은 위조 가능).
    trust_forwarded_for: bool = False
    # 신뢰하는 프록시 홉 수(우측부터). 자사 프록시 1대면 1 = 최우측 값.
    forwarded_for_trusted_hops: int = 1

    # ── 벤치마크 runner (이슈 #151) ──
    # measured 30건·p99 100건의 고정 계약 하한은 evals/benchmark/runner.py(measured)와
    # stats.py(p99)의 max() 클램프에서 강제한다. validator는 값 사이의 상대적 정합만 본다 —
    # 아래 값은 운영에서 더 엄격하게 올릴 수 있어야 하므로 여기서 하한 미만을 거부하지 않는다.
    benchmark_min_measured_requests: int = 30
    benchmark_p99_min_samples: int = 100
    benchmark_warmup_requests: int = 5
    benchmark_cold_requests: int = 3
    benchmark_concurrency_levels: tuple[int, ...] = (1, 5, 10)
    benchmark_bootstrap_resamples: int = 1000
    benchmark_bootstrap_seed: int = 20260803
    benchmark_bootstrap_confidence: float = 0.95
    benchmark_request_timeout_s: float = 120.0

    # ── 구매자 골든셋(#142, evals/goldenset) ──
    # v2(#333)는 서빙 후보 상한(30)까지 후보를 채우는 슬라이스 쿼터 확장을 담아 160으로 올린다.
    goldenset_min_cases: int = 30
    goldenset_max_cases: int = 160
    # 문자 3-gram Jaccard가 이 값을 넘는 split 간 query는 leakage로 본다.
    goldenset_near_dup_jaccard_max: float = 0.6
    # split 간 정답 집합이 절반보다 많이 겹치면 동일 시나리오 누출로 본다.
    goldenset_near_dup_relevant_overlap_max: float = 0.5
    # I-1의 AI 후보 기본 limit과 맞춰 질의별 기록량을 유계로 둔다.
    goldenset_snapshot_per_query_max: int = 30
    # 43건 중 12건을 봉인하는 v1 목표 비중이며 감사 보고에 사용한다.
    goldenset_holdout_ratio: float = 0.3
    # #333: 순위 평가 대상 케이스의 후보 하한. nDCG@10 컷오프가 구조적으로 발동하려면
    # 최소 이 개수는 있어야 한다(narrow-domain 케이스는 notes 접두 문구로 예외).
    goldenset_min_ranking_candidates: int = 20
    # #333: 후보 깊이 목표 — 서빙 상한(30)과 동일하게 맞춘다.
    goldenset_target_candidates: int = 30
    # #333 리뷰 F-5-1(#329 권고 3): 순위 평가 대상 케이스의 등급≥1 후보 비율 상한. v1 평균이
    # 0.389로 이 값을 넘어 하드 네거티브가 사실상 없었다 — 초과 케이스는 notes 접두 문구
    # relevant-ratio-exempt: 로만 예외를 허용한다.
    goldenset_max_relevant_ratio: float = 0.25

    # ── 구매자 추천 평가 지표(#143, evals/metrics) ──
    eval_buyer_k_list: tuple[int, ...] = (5, 10, 20)

    # --- #144 actual-model eval 예산 gate ---
    model_eval_max_calls_per_run: int = Field(default=800, gt=0)
    model_eval_max_total_tokens_per_run: int = Field(default=30_000_000, gt=0)
    model_eval_max_cost_usd_per_run: float = Field(default=20.0, gt=0.0)

    # ── 추천 scoring baseline(#145, evals/scoring) ──
    # 의미 유사도를 주 신호로 두고, profile·인기도·최신성·다양성은 보조 신호로 제한한다.
    # 최근 exact 재구매는 별도 감점이며 모든 값은 ScoringBuyerAdapter가 직접 소비한다.
    scoring_weight_semantic: float = 0.55
    scoring_weight_profile_match: float = 0.15
    scoring_weight_popularity: float = 0.15
    scoring_weight_recency: float = 0.05
    scoring_weight_diversity_bonus: float = 0.10
    scoring_weight_recent_purchase_penalty: float = 0.20
    scoring_reference_date: str = "2026-08-02"
    scoring_recent_purchase_window_days: int = 90

    # ── 개인화 평가 (이슈 #147, evals/personalization) ──
    # 하드필터와 명시 의도는 안전 불변식이라 기본 허용치를 0으로 사전 선언한다.
    personalization_eval_hard_filter_violation_max: int = 0
    personalization_eval_intent_contradiction_max: int = 0
    personalization_eval_forbidden_inclusion_max: int = 0
    # clean→noisy NDCG@10 열화는 paired CI 하한이 -3%를 넘는지를 판정한다.
    personalization_eval_clean_noisy_drop_margin: float = 0.03
    # 현행 0.15를 중심으로 0~4배 범위를 대칭적이지 않은 실용 구간으로 탐색한다.
    personalization_eval_weight_sweep: tuple[float, ...] = (0.0, 0.075, 0.15, 0.30, 0.60)

    # ── 구매자 progress 이벤트 (이슈 #396, 계약 등재 완료 — 기본 on) ──
    # 정본(Notion CH-2)·api-spec §3.1 v0.21.0 등재와 FE 구현 완료(2026-08-06)로 전제가
    # 충족돼 기본 on 으로 해제했다(#289 후속). 되돌리려면 PROGRESS_EVENTS_ENABLED=false.
    progress_events_enabled: bool = True
    # 빈 문자열이면 프레임 `data`에 `message` 키 자체를 싣지 않는다(app/agents/buyer/_frames.py).
    progress_analyzing_message: str = "요청을 확인하고 있어요"
    # 다회 emit·어휘 확장(이슈 #396, api-spec §3.1 v0.27.0) — stage 6종 추가. 규약은 위와 동일.
    progress_mapping_message: str = "카테고리를 찾고 있어요"
    progress_expanding_message: str = "어떤 상품이 필요한지 넓혀 보고 있어요"
    progress_searching_message: str = "상품을 검색하고 있어요"
    progress_relaxing_message: str = "조건을 조금 넓혀 다시 찾고 있어요"
    progress_reranking_message: str = "가장 잘 맞는 걸 고르고 있어요"
    progress_publishing_message: str = "추천 목록을 준비하고 있어요"

    # ── 요청 바디 크기 상한 (이슈 #299, api-spec §2.5·§2.8) ──
    # 레이트 리밋(§2.8)은 요청 **건수**만 세므로 10회로도 임의 크기 바디를 보낼 수 있다.
    # 필드별 상한(chat_message_max_chars·screen_products_raw_scan_max 등)은 흩어져 있고 상한 없는
    # 필드(conditionActions 등)도 계속 생기므로, 그 앞단에 요청 전체를 유계로 만드는 층을 둔다
    # (app/core/body_limit.py, BodySizeLimitMiddleware).
    #
    # 기본값은 현행 필드별 상한이 **절단 없이** 받아들이는 최대 정상 페이로드 크기의 약 4.8배로
    # 잡는다(한국어 UTF-8 3B/자 가정):
    #   message              chat_message_max_chars(4,000자)                      ≈  12,000B
    #   sessionId+threadId   chat_key_max_chars(200자) × 2                         ≈   1,200B
    #   screen.products      screen_products_raw_scan_max(500건) ×
    #                        (screen_text_max_chars(120자) name + productId
    #                         + JSON 구두점 ≈ 400B/건)                             ≈ 200,000B
    #   screen.filters       표시값 10건 × 120자                                    ≈   4,000B
    #   conditionActions + 봉투 키                                                 ≈     500B
    #   합계                                                                      ≈ 218,000B(≈218KB)
    # 1 MiB(1,048,576B)는 그 약 4.8배다 — 실제 FE 페이로드(상품 20건 규모)는 30KB 를 넘기 어려워
    # 정상 요청 회귀는 0이고, 무제한이던 공격 표면은 1MiB 로 유계가 된다.
    # `/internal/recommendations/home`(I-22) 최대 바디도 id 200×3 배열 ≈ 12KB 로 여유가 크다.
    # 추가로 nginx 기본 client_max_body_size 가 1MB 라 같은 자리에 두면 프록시가 먼저 자르는
    # 배포에서도 임계가 어긋나지 않는다 — 프록시가 앞서면 이 층의 목표는 "방어"가 아니라
    # "일관된 §2.5 봉투 응답"이 된다. 운영에서 env 로 낮춰 잡을 수 있다.
    #
    # [리뷰 1차 F-4] **#118 의 raw-scan 상한과 만나는 지점에서 동작이 바뀐다.** #118 은
    # `screen_products_raw_scan_max`(500)·`screen_text_raw_scan_max`(2,400자)를 "정제 비용을
    # 유계로 만드는 사전 절단 상한"으로 설계했고, 그 상한까지 채운 페이로드도 400 이 아니라
    # **절단**해서 받아준다는 것이 §3.1 관대 유효성의 전제였다. 이 층이 생긴 뒤로는 그 전제가
    # 더 이상 전 구간에서 성립하지 않는다 — 두 상한을 **원문 길이까지 가득 채운** 페이로드
    # (products 500건 × name/filters 값 2,400자)는 실측 **≈3,650,100B(기본 상한의 약 348%)**
    # 라 그 요청은 스키마 검증·절단 로직에 도달하기도 전에 이 층에서 400 이 된다. 이것이
    # #118 이 비용을 들여 절단해 주던 바로 그 악성 극단 페이로드다 — 정본을 건드리는 변경이
    # 아니라(§3.1 관대 유효성 자체는 "정상 요청은 절단만 받고 거부되지 않는다"는 뜻이었지,
    # 무제한 극단값까지 보장한 적은 없다) 그 극단값의 처리 계층이 스키마 절단에서 이 미들웨어의
    # 사전 거절로 옮겨 왔을 뿐이다.
    # **현실적인 상한(개수 500건, name/filters 값은 표시 상한인 `screen_text_max_chars`=120자)은
    # 그대로 통과한다** — 실측 **≈209,580B(기본 상한의 약 20.0%)**. 즉 #118 이 "정상 FE
    # 페이로드는 절대 자르지 않는다"고 보장한 구간(건수는 raw_scan_max 까지, 항목 길이는 표시
    # 상한까지)은 이 층도 자르지 않는다 — 어긋나는 것은 항목 길이를 raw-scan 사전절단 상한까지
    # 늘린, 애초에 악성으로 설계된 구간뿐이다.
    request_body_max_bytes: int = Field(default=1_048_576, gt=0)

    # ── 니즈 priority 분류기 (이슈 #281, #60 후속) ──
    # BUY_ALL 총액 예산이 모든 니즈를 못 담을 때 어떤 니즈부터 뺄지의 근거 신호(REQ-REC-076).
    # `budget_sets.build_budget_sets` 는 이 신호가 없거나 신뢰할 수 없으면 "최저가가 비싼 leg
    # 부터"라는 기존 결정론적 순서로 폴백한다(REQ-REC-075) — 즉 이 롤백 스위치를 꺼도 BUY_ALL
    # 예산 제외 자체는 오늘처럼 계속 동작한다.
    need_priority_classifier_enabled: bool = True  # 롤백 스위치(끄면 호출 0회 = 오늘 동작)
    # Literal 로 좁힌다 — `category_scope_tier` 와 같은 이유다. 이 값은 `resolve_model_id` 에
    # 들어가고 그것은 미지 tier 에 LLMError 를 던지므로, 오타가 퇴화가 아니라 예외가 된다
    # (분류기는 그 예외를 삼켜 None 으로 떨어뜨리지만, 그러면 기능이 조용히 죽는다).
    need_priority_tier: Literal["fast", "smart"] = "fast"
    # 산출은 `{"priorities": [1, 2, 3, ...]}` 다 — 니즈 5개(계약 상한 category_fanout_max ≤
    # MAX_LISTS=10 근방)면 `[1,2,3,2,1]` 수준의 소출력이다. `category_scope_max_tokens`(32,
    # `{"scopeFree": true|false}` 한 줄 기준)보다 배열이라 여유를 더 둔다 — 항목당 콤마·공백
    # 포함 약 3토큰이면 10개도 약 30~40토큰이라 64면 넉넉하다.
    need_priority_max_tokens: int = Field(default=64, ge=8)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_llm_provider(cls, value: object) -> object:
        """기존 환경변수 호환을 위해 provider 값의 ASCII 대소문자를 정규화한다."""
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _require_valid_benchmark_settings(self) -> "Settings":
        """벤치마크 기본 표본·동시성·bootstrap 설정의 모순을 기동 시점에 막는다."""
        if self.benchmark_min_measured_requests <= 0:
            raise ValueError("BENCHMARK_MIN_MEASURED_REQUESTS must be positive")
        if self.benchmark_p99_min_samples < self.benchmark_min_measured_requests:
            raise ValueError("BENCHMARK_P99_MIN_SAMPLES must be >= BENCHMARK_MIN_MEASURED_REQUESTS")
        if self.benchmark_cold_requests + self.benchmark_warmup_requests >= (
            self.benchmark_min_measured_requests
        ):
            raise ValueError(
                "BENCHMARK_COLD_REQUESTS + BENCHMARK_WARMUP_REQUESTS must be "
                "< BENCHMARK_MIN_MEASURED_REQUESTS"
            )
        if (
            self.benchmark_cold_requests < 0
            or self.benchmark_warmup_requests < 0
            or not self.benchmark_concurrency_levels
            or any(level <= 0 for level in self.benchmark_concurrency_levels)
            or self.benchmark_bootstrap_resamples <= 0
            or not 0 < self.benchmark_bootstrap_confidence < 1
            or self.benchmark_request_timeout_s <= 0
        ):
            raise ValueError("benchmark runner settings must be positive and non-empty")
        return self

    @model_validator(mode="after")
    def _require_valid_goldenset_settings(self) -> "Settings":
        """구매자 골든셋 크기·누출 임계·기록 상한의 모순을 기동 시점에 막는다."""
        if not 0 < self.goldenset_min_cases <= self.goldenset_max_cases:
            raise ValueError("골든셋 최소 케이스 수는 0보다 크고 최대 케이스 수 이하여야 합니다")
        if not 0 < self.goldenset_near_dup_jaccard_max < 1:
            raise ValueError("골든셋 query Jaccard 임계값은 0과 1 사이여야 합니다")
        if not 0 < self.goldenset_near_dup_relevant_overlap_max < 1:
            raise ValueError("골든셋 정답 겹침 임계값은 0과 1 사이여야 합니다")
        if self.goldenset_snapshot_per_query_max <= 0:
            raise ValueError("골든셋 질의별 스냅샷 상한은 0보다 커야 합니다")
        if not 0 < self.goldenset_holdout_ratio < 1:
            raise ValueError("골든셋 holdout 비율은 0과 1 사이여야 합니다")
        if not 0 < self.goldenset_min_ranking_candidates <= self.goldenset_target_candidates:
            raise ValueError("골든셋 순위 평가 후보 하한은 0보다 크고 목표 후보 수 이하여야 합니다")
        if not 0 < self.goldenset_max_relevant_ratio < 1:
            raise ValueError("골든셋 등급≥1 후보 비율 상한은 0과 1 사이여야 합니다")
        return self

    @model_validator(mode="after")
    def _require_valid_eval_settings(self) -> "Settings":
        """추천 평가 K 목록의 빈 값·비양수를 기동 시점에 막는다."""
        if not self.eval_buyer_k_list or any(k <= 0 for k in self.eval_buyer_k_list):
            raise ValueError("구매자 추천 평가 K 목록은 비어 있지 않고 모두 0보다 커야 합니다")
        return self

    @model_validator(mode="after")
    def _require_valid_scoring_settings(self) -> "Settings":
        """baseline 가중치·골든셋 기준일·최근구매 window를 fail-fast한다."""
        from datetime import date  # noqa: PLC0415 - #145 자기 validator의 ISO 파싱 전용

        # 감점 항만 켜진 baseline은 모든 상품을 0 이하로만 밀어 의미 있는 양의 신호가 없다.
        positive_signal_weights = (
            self.scoring_weight_semantic,
            self.scoring_weight_profile_match,
            self.scoring_weight_popularity,
            self.scoring_weight_recency,
            self.scoring_weight_diversity_bonus,
        )
        weights = (
            *positive_signal_weights,
            self.scoring_weight_recent_purchase_penalty,
        )
        if not all(math.isfinite(weight) for weight in weights):
            raise ValueError("추천 scoring 가중치는 유한한 수여야 합니다")
        if any(weight < 0 for weight in weights):
            raise ValueError("추천 scoring 가중치는 음수일 수 없습니다")
        if not any(positive_signal_weights):
            raise ValueError(
                "추천 scoring 양의 신호 가중치"
                "(semantic·profile·popularity·recency·diversity) 중 하나 이상은 양수여야 합니다"
            )
        if self.scoring_recent_purchase_window_days <= 0:
            raise ValueError("추천 scoring 최근 구매 window는 0보다 커야 합니다")
        try:
            date.fromisoformat(self.scoring_reference_date)
        except ValueError as exc:
            raise ValueError("추천 scoring 기준일은 ISO 날짜 형식이어야 합니다") from exc
        return self

    @model_validator(mode="after")
    def _require_valid_personalization_eval_settings(self) -> "Settings":
        """개인화 평가의 사전 선언 margin과 weight sweep을 fail-fast한다."""
        maxima = (
            self.personalization_eval_hard_filter_violation_max,
            self.personalization_eval_intent_contradiction_max,
            self.personalization_eval_forbidden_inclusion_max,
        )
        if any(value < 0 for value in maxima):
            raise ValueError("개인화 평가 허용 건수는 음수일 수 없습니다")
        if self.personalization_eval_clean_noisy_drop_margin < 0:
            raise ValueError("개인화 평가 clean-noisy margin은 음수일 수 없습니다")
        sweep = self.personalization_eval_weight_sweep
        if (
            not sweep
            or any(not math.isfinite(value) or not 0 <= value <= 1 for value in sweep)
            or tuple(sorted(set(sweep))) != sweep
            or 0.15 not in sweep
        ):
            raise ValueError(
                "개인화 평가 weight sweep은 [0,1]의 오름차순 고유 값이며 0.15를 포함해야 합니다"
            )
        return self

    @model_validator(mode="after")
    def _require_known_buffer_excluded_intents(self) -> "Settings":
        """세션 버퍼 제외 intent 오타를 기동 시 잡는다 (#119).

        `"order-status"` 처럼 한 글자만 어긋나도 비교가 영원히 거짓이 되어 **제외가 조용히
        무효화**되고, 버퍼는 계속 오염된다 — 로그에도 안 남는 종류의 실패라 기동을 막는다.
        """
        unknown = sorted(set(self.profile_buffer_excluded_intents) - ROUTE_INTENTS)
        if unknown:
            raise ValueError(
                f"profile_buffer_excluded_intents has unknown intents {unknown}: "
                f"must be a subset of {sorted(ROUTE_INTENTS)}"
            )
        return self

    @model_validator(mode="after")
    def _require_margin_bands_disjoint(self) -> "Settings":
        """마진 예외(§4.5)와 택일 트리거(§4.4)의 구간이 겹치면 기동 실패 (PR #188 리뷰).

        두 임계는 **정반대 상태**를 가리킨다 — `margin <= category_select_margin_max` 는 "1·2위가
        거의 붙어 애매하다"(LLM 에게 택일을 물어본다), `margin >= category_distance_override_margin`
        은 "1위만 확 가까워 확신한다"(거리가 멀어도 채택한다). 한 leg 이 동시에 애매하면서 확신일
        수는 없으므로 두 구간은 서로소여야 한다.

        겹치면 **#115 가 폐기한 실패 모드가 되살아난다**: 얇은 마진은 "taxonomy 에 맞는 칸이 없다"는
        신호인데(§4.5), 그 상태에서 택일이 고른 먼 후보를 채택하면 그게 곧 "억지 채택"이다
        (`'선물용품'` 마진 0.0095 → `도서/음반 > 독서용품` 0.2292 선택 → 드롭이 정답).

        기본값(0.02 < 0.035)은 서로소지만, 한쪽만 튜닝하면 조용히 겹친다 — 관계를 코드로 고정한다.
        """
        if self.category_distance_override_margin <= self.category_select_margin_max:
            raise ValueError(
                "CATEGORY_DISTANCE_OVERRIDE_MARGIN must be > CATEGORY_SELECT_MARGIN_MAX "
                f"(got {self.category_distance_override_margin} <= "
                f"{self.category_select_margin_max}): "
                "'ambiguous' and 'confident' margin bands must not overlap"
            )
        return self

    @model_validator(mode="after")
    def _require_pool_covers_anchor_concurrency(self) -> "Settings":
        """검색 풀이 한 턴의 동시 조회(`2 × category_fanout_max`)를 못 덮으면 기동 실패 (PR #188 리뷰).

        매핑은 leg 마다 raw·query **두 앵커**를 gather 로 동시 조회한다(§4.3 #115). 두 값은 함께
        움직여야 하는 쌍인데 테스트로만 묶으면 기본값 조합에서만 걸린다 — `category_fanout_max` 를
        올리는 쪽(#168 은 leg 10 계획)이 풀을 잊으면 한 턴이 풀을 소진해 **다른 사용자의 조회가
        대기**한다(증상은 PoolTimeout 이라 원인이 드러나지 않는다). 기동 시점에 막는다.

        `2 ×` 상한의 전제(leg 수 ≤ fanout_max)는 **`map_categories` 가 입력을 방어적으로 절단해**
        스스로 보장한다(PR #188 리뷰) — 호출부(`decompose._parse_category_queries`·`expand_needs`)의
        절단에만 기대면 새 호출부 하나가 풀을 넘기고, 증상이 다른 요청의 PoolTimeout 이라 원인
        추적이 어렵다. 절단이 실제로 발생하면 `category_legs_truncated` 로 관측된다.

        [#222] 확장 leg(`category_expand_legs`)은 이 전제와 무관하다 — 광역 fan-out 후보는
        `expansion_leaves`(이미 조회된 히트를 슬라이스)로 채우고 pg 앵커 조회를 새로 하지 않으므로
        `2 × category_fanout_max` 동시 조회 전제가 그대로 성립한다.
        """
        need = 2 * self.category_fanout_max
        if self.category_search_pool_max_size < need:
            raise ValueError(
                "CATEGORY_SEARCH_POOL_MAX_SIZE must be >= 2 * CATEGORY_FANOUT_MAX "
                f"(need {need}, got {self.category_search_pool_max_size}): "
                "mapping probes two anchors (raw, query) per leg concurrently"
            )
        return self

    @model_validator(mode="after")
    def _require_db_timeout_after_app_timeout(self) -> "Settings":
        """카탈로그 DB 상한이 앱쪽 호출 상한보다 크지 않으면 기동 실패 (PR #213 리뷰).

        앱쪽(_call_store)이 항상 먼저 포기해야 느린 쿼리가 **결정적으로 504**(AI_TIMEOUT)로
        나간다. DB 가 먼저 끊으면 psycopg QueryCanceled(OperationalError 계열)가 except
        Exception 에 잡혀 503(AI_ERROR)이 되고, 같은 원인이 지터에 따라 다른 코드로 기록된다
        (§4.11 fallbackReason 구분 무력화). 포기된 쿼리는 DB 상한이 뒤늦게 잘라 커넥션을 회수한다.
        """
        if self.catalog_store_query_timeout_s <= self.home_reco_store_timeout_s:
            raise ValueError(
                "CATALOG_STORE_QUERY_TIMEOUT_S must be > HOME_RECO_STORE_TIMEOUT_S "
                f"(got {self.catalog_store_query_timeout_s} <= {self.home_reco_store_timeout_s}): "
                "the app-side clock must fire first so slow queries map to 504 deterministically"
            )
        return self

    @model_validator(mode="after")
    def _require_color_synonym_db_timeout_after_app_timeout(self) -> "Settings":
        """색상 동의어 DB 상한은 앱쪽 품질 degrade 상한보다 늦게 발동해야 한다."""
        if self.catalog_store_query_timeout_s <= self.color_synonym_query_timeout_s:
            raise ValueError(
                "CATALOG_STORE_QUERY_TIMEOUT_S must be > COLOR_SYNONYM_QUERY_TIMEOUT_S "
                f"(got {self.catalog_store_query_timeout_s} <= "
                f"{self.color_synonym_query_timeout_s}): "
                "the app-side clock must degrade first and the DB clock must later reclaim "
                "the connection"
            )
        return self

    @model_validator(mode="after")
    def _require_embedding_total_timeout_covers_request_timeout(self) -> "Settings":
        """embed_texts 총 예산이 요청 1건당 상한보다 작으면 기동 실패 (#391 PR #412 Claude 리뷰).

        경계(같은 값)는 허용한다 — 기본값 3.0 == 3.0 이 "hot path 는 청크 1개분"이라는 의도된
        조합이다. 이보다 작게 잡으면 1청크 호출(idx==0, 현재 hot path 전부)은 예산 검사 자체를
        건너뛰므로 설정을 줄여도 아무 효과가 없고, 2청크 이상 호출은 정상 상황에서도 두 번째
        청크에서 거의 항상 거부된다 — 설정이 의미하는 바와 실제 동작이 갈린다.
        """
        if self.embedding_total_timeout_s < self.embedding_timeout_s:
            raise ValueError(
                "EMBEDDING_TOTAL_TIMEOUT_S must be >= EMBEDDING_TIMEOUT_S "
                f"(got {self.embedding_total_timeout_s} < {self.embedding_timeout_s}): "
                "a smaller total budget has no effect on single-chunk (hot path) calls "
                "since idx==0 always skips the budget check, while multi-chunk calls would "
                "be rejected almost every time even under normal conditions"
            )
        return self

    @model_validator(mode="after")
    def _require_color_synonym_array_contract_gate(self) -> "Settings":
        """색상 배열 전송과 외부 계약 준비 선언이 엇갈리면 기동을 막는다."""
        if self.color_synonym_expansion_enabled != self.color_synonym_array_contract_ready:
            raise ValueError(
                "COLOR_SYNONYM_EXPANSION_ENABLED and COLOR_SYNONYM_ARRAY_CONTRACT_READY "
                "must be enabled together only after api-spec §4.6 is revised to "
                "`color: string[]` and the supporting BE is deployed "
                "(api-spec §4.6 개정 + BE 배포 선행 필요)"
            )
        return self

    @model_validator(mode="after")
    def _reserve_color_synonym_runtime_pool_slot(self) -> "Settings":
        """배치 수확을 켰을 때만 공유 풀에 사용자 대면 검색 슬롯을 하나 이상 남긴다."""
        if (
            self.color_synonym_batch_harvest_enabled
            and self.color_synonym_harvest_max_concurrency >= self.color_synonym_pool_max_size
        ):
            raise ValueError(
                "COLOR_SYNONYM_HARVEST_MAX_CONCURRENCY must be less than "
                "COLOR_SYNONYM_POOL_MAX_SIZE "
                f"(got {self.color_synonym_harvest_max_concurrency} >= "
                f"{self.color_synonym_pool_max_size}): reserve at least one "
                "runtime search connection"
            )
        return self

    @model_validator(mode="after")
    def _require_color_synonym_scan_budget_above_result_budget(self) -> "Settings":
        """dedup 전 스캔 예산은 반환 상한보다 커야 중복이 고유 표기를 밀어내지 않는다."""
        if (
            self.color_synonym_harvest_scan_max_values_per_product
            <= self.color_synonym_harvest_max_terms_per_product
        ):
            raise ValueError(
                "COLOR_SYNONYM_HARVEST_SCAN_MAX_VALUES_PER_PRODUCT must be greater than "
                "COLOR_SYNONYM_HARVEST_MAX_TERMS_PER_PRODUCT "
                f"(got {self.color_synonym_harvest_scan_max_values_per_product} <= "
                f"{self.color_synonym_harvest_max_terms_per_product})"
            )
        return self

    @model_validator(mode="after")
    def _require_buyer_cap_within_stream_cap(self) -> "Settings":
        """구매자 상한이 전체 상한을 넘으면 기동 실패 (#138).

        구매자 상한은 전체 상한을 **좁히는** 값이다. 넘어서면 이름과 반대로 판매자보다
        느슨해져 조용히 무의미해지므로 기동 시점에 고정한다.
        반대로 first-token 상한보다 짧으면 첫 이벤트 대기를 허용한 시간보다 전체 스트림을
        먼저 끊는 자기모순이므로 함께 거절한다.
        """
        if self.stream_total_timeout_buyer_s > self.stream_total_timeout_s:
            raise ValueError(
                "STREAM_TOTAL_TIMEOUT_BUYER_S must not exceed STREAM_TOTAL_TIMEOUT_S "
                f"(got {self.stream_total_timeout_buyer_s} > {self.stream_total_timeout_s})"
            )
        if self.stream_total_timeout_buyer_s < self.stream_first_token_timeout_s:
            raise ValueError(
                "STREAM_TOTAL_TIMEOUT_BUYER_S must be at least STREAM_FIRST_TOKEN_TIMEOUT_S "
                f"(got {self.stream_total_timeout_buyer_s} < "
                f"{self.stream_first_token_timeout_s}): "
                "the total stream cap cannot expire before the first-event wait"
            )
        return self

    @model_validator(mode="after")
    def _require_rescue_tail_reserve_within_buyer_cap(self) -> "Settings":
        """꼬리 예약이 구매자 전체 상한 이상이면 기동 실패 (#427, DESIGN-SHARED-BUDGET-384 §3 D2).

        `rescue_deadline = turn_started_at + (stream_total_timeout_buyer_s -
        rescue_tail_reserve_s)`(`app/agents/buyer/recommendation/graph.py::stream_
        recommendation`) — 예약이 전체 예산 이상이면 구제 체인 몫이 음수/0 이 되어 모든 턴이
        예산 판정에서 즉시 `skip`(또는 `narrow` 강등)으로 떨어진다. 그 자체가 설정 오류다.
        """
        if self.rescue_tail_reserve_s >= self.stream_total_timeout_buyer_s:
            raise ValueError(
                "RESCUE_TAIL_RESERVE_S must be < STREAM_TOTAL_TIMEOUT_BUYER_S "
                f"(got {self.rescue_tail_reserve_s} >= {self.stream_total_timeout_buyer_s}): "
                "the rescue chain budget would be zero or negative for every turn"
            )
        return self

    @model_validator(mode="after")
    def _require_general_lane_within_stream_cap(self) -> "Settings":
        """판매자 general 레인 직렬 예산이 스트림 전체 상한을 넘으면 기동 실패 (#266 P1 리뷰).

        이 검증이 없으면 #266 이 고친 것이 **설정 하나로 되돌아간다.** general 레인의
        `asyncio.timeout` 이 발동하기 전에 SSE 계층의 `stream_total_timeout_s` 캡이 먼저
        끊으면(`stream.py` 전체 상한 → `done(stop)`), `_general_stream` 의 예외 분기에 아예
        도달하지 못해 in-stream `error`(`LLM_TIMEOUT`) 대신 **오류 코드 없는 조용한 절단**이
        되고 관측에는 `COMPLETED` 로 남는다. 앱 시계가 항상 먼저 터져야 결정적으로 매핑된다
        (`_require_db_timeout_after_app_timeout` 와 같은 원칙의 LLM 판).

        **라우팅을 더해서 비교하는 이유**(상한이 재는 구간을 emit 순서로 확인):
        general 경로의 첫 SSE 이벤트는 `meta{lane}` 인데 `lane` 은 라우팅 산출물이라
        `route_question` **뒤**에서야 나간다(`app/api/seller.py` `_seller_stream`).
        반면 general 레인의 `asyncio.timeout` 시계는 그 뒤에 시작하므로 두 상한은 **직렬로
        쌓인다.** `seller_general_timeout_s` 만 단독 비교하면 `route=10 + general=85 = 95 > 90`
        같은 조합이 검증을 통과해 검증이 이름만 남는다.

        **체크포인터 초기화도 더한다**(#266 PR 리뷰): pg-profile 최초 연결은 general 상한
        **밖**에서 돈다 — 안에 두면 그 `TimeoutError` 와 LLM 지연의 `TimeoutError` 가 같은
        타입이라 구분이 불가능해지기 때문이다(`app/api/seller.py` `_CheckpointerUnavailable`).
        밖으로 뺀 대가로 이 시간은 general 예산에 잡히지 않고 캡을 향해 **직렬로 더해지므로**
        예산식에도 함께 넣는다. 콜드스타트 1회에만 드는 비용이지만 검증은 최악을 본다.

        **`2 *` 인 이유**(#266 PR 3차 리뷰): `_init_checkpointer` 는 연결(`__aenter__`)과
        `setup()`(DDL)을 **각각** 이 상한으로 감싼다 — 이웃 `pg_store.py` 와 같은 형태다.
        따라서 초기화 1회의 실질 최악은 상수의 2배다. 3차 리뷰 이전에는 `setup()` 이 아예
        상한 밖이라 이 항 자체가 **성립하지 않는 전제** 위에 서 있었다(콜드 DB 에서 MIGRATIONS
        8종이 문장당 `statement_timeout` 3s 씩 누적). 상한을 먼저 채우고 계수를 맞춘다.

        **커버하지 않는 것**: 라우팅 앞의 `load_recent_turns` 조회는 이 식에 없다 —
        `state_store_query_timeout_s` 로 따로 묶이고, 엄격 부등식과 기본값 여유(35 < 90)가
        흡수한다. 이 항을 넣지 않은 것은 누락이 아니라 판단이다.

        `>=` 로 거절하는 이유: 동률이면 어느 시계가 먼저 터지는지가 지터로 갈려
        같은 원인이 `LLM_TIMEOUT`/`done(stop)` 두 갈래로 기록된다 — 이 이슈가 없애려는
        비결정성 그 자체다(`_require_search_retry_within_stream_budget` 와 같은 기준).
        """
        budget = (
            self.seller_route_timeout_s
            + 2 * self.seller_checkpoint_connect_timeout_s
            + self.seller_general_timeout_s
        )
        if budget >= self.stream_total_timeout_s:
            raise ValueError(
                "SELLER_ROUTE_TIMEOUT_S + 2 * SELLER_CHECKPOINT_CONNECT_TIMEOUT_S + "
                "SELLER_GENERAL_TIMEOUT_S must be < STREAM_TOTAL_TIMEOUT_S "
                f"(got {budget} >= {self.stream_total_timeout_s}): "
                "the SSE total cap would cut the general lane before its own timeout fires, "
                "degrading a mapped LLM_TIMEOUT into a silent done(stop)"
            )
        return self

    @model_validator(mode="after")
    def _require_search_retry_within_stream_budget(self) -> "Settings":
        """I-1 검색 재시도 총량이 스트림 전체 상한을 넘으면 기동 실패 (#133, #427 재기준선).

        [#427, DESIGN-SHARED-BUDGET-384 §1(a)] **first-token 비교는 `progress_events_enabled
        is False` 일 때만 한다** — 이 검증기는 원래 "추천 경로의 첫 이벤트가 `conditions`(또는
        미룬 턴은 검색 뒤 `conditions`)이고, first-token 상한(10s)이 그 앞의 검색·재시도를
        가둔다"는 전제로 first-token 과 비교했다. `progress_events_enabled=True`(기본, #396,
        api-spec v0.26.2)면 첫 SSE 는 `conditions` 가 아니라 decompose **앞**의
        `progress`(`app/agents/buyer/graph.py::run_buyer_turn`, 실측 p50 ≈12ms)라, 구제
        체인(F-1/#343/자동완화 probe) 전체가 first-token 관문 **밖**에서 돈다 — 이 검증기의
        옛 docstring 이 "그 플래그를 끄면 다시 실질 가드가 된다 / 연동 여부는 #384·#288 잔여
        후보로 남긴다"고 적어 둔 바로 그 판단을 이 이슈(#384 후속 (i))가 내린다. 플래그를
        끄면(운영 롤백 등) 다시 실질 가드가 필요하므로 그때만 비교한다.

        **전체 상한과는 상시 비교한다**(PR #241/#138 lessons 로 정정, #427 로 유지) — 재시도가
        갉아먹는 것은 턴 전체 시간이다. `llm_timeout_s * (llm_max_retries + 1)` 과 같은 결의
        예산식이며, 한쪽만 튜닝하면 조용히 어긋나는 쌍이라 기동 시점에 고정한다. 비교 대상은
        **구매자 전체 상한**(`stream_total_timeout_buyer_s`, #138)이다 — I-1 검색은 구매자
        추천 경로에서만 돌고, 그 경로를 실제로 끊는 것은 판매자와 공용인 90s 가 아니라 구매자
        전용 30s 다.

        [#427] **I-1 검색 예산은 `spring_search_timeout_s` 로 잰다**(단일 호출 예산·직렬 합
        둘 다) — `spring_timeout_s`(AI→Spring 전 구간 공용)와 분리됐다(#394 가 기각된 "스칼라
        하나를 공유해 전 구간이 함께 늘어난다" 실패 모드를 되풀이하지 않기 위해서다).

        **이 식은 단일 I-1 호출 예산만 본다**(#277). 종전의 배타성 전제는 실측으로 반증됐다:
        본 검색이 1 차 타임아웃 뒤 2 차에 0 건으로 성공하면 재시도를 쓰고도 완화 probe 가 돈다.
        기본 설정은 미룬 턴의 재시도를 건너뛰어 첫 이벤트 앞 직렬 합을
        `3 * spring_search_timeout_s`(9s, #383 보정 후)로 묶는다. `SEARCH_RETRY_ON_DEFERRED_
        CONDITIONS=true`로 종전 동작을 되살리면 세 호출이 각각 재시도해 최대 18s가 되고, #277의
        이벤트 0건·504 조합도 다시 열린다.

        **직렬 합 계산은 `_rescue_chain_stage_counts`/`_rescue_chain_serial_budget_s`
        (#427 D7) 로 위임한다** — 런타임 좁히기(`app/agents/buyer/recommendation/
        graph.py::stream_recommendation`)와 이 기동 검증이 **같은 함수**에서 계수를 얻어야
        한쪽만 고쳐지는 드리프트(#383 이 고친 것과 같은 실패 모드)를 구조적으로 막는다. 계수
        정의(각 항의 출처·§1(d) 각주①의 억제 스코프 비대칭·"오늘 기본값에서 3"인 근거)는 그
        두 함수의 docstring 참조 — 여기서 다시 적지 않는다(드리프트 방지 원칙 그대로).

        게이트는 `deferred_calls == 0`(= `graph.py`의 `may_auto_relax`가 False)일 때만 검증을
        건너뛰어, 실제로 미루지 않는 설정을 일어나지 않는 직렬 호출 때문에 막지 않는다(#277 4차
        원칙을 일반형으로 그대로 유지).

        **커버하지 않는 것**(누락이 아니라 판단): LLM head(#151 baseline p95 ≈3.0s)와 pg 왕복은
        이 식에 없고, `conditions` 뒤에 도는 완화 칩 probe(`relaxation_max_probes`)도 첫 이벤트
        예산 밖이다(그 probe는 이미 첫 이벤트가 나간 뒤라 first-token 상한과 무관하다). head 를
        포함한 타임아웃 재배분은 #288 의 잔여 후보로 남는다. F-1·#343 구제 폴백은 #383 부터 이
        식에 들어왔다(더 이상 커버 밖이 아니다).

        [#427, DESIGN-SHARED-BUDGET-384 §3 D2] **`rescue_budget_mode == "observe"` 일 때만**
        직렬 합을 `stream_total_timeout_buyer_s - rescue_tail_reserve_s` 와 추가로 비교한다.
        근거: `narrow`/`narrow_skip` 에서는 런타임 좁히기(`stream_recommendation`)가 꼬리
        예약을 **실제로 집행**하므로 이론적 직렬 합이 그 값을 넘어도 실제로는 넘지 못한다.
        `observe` 는 아무것도 집행하지 않으므로 설정 자체가 안전해야 한다. **집행이 런타임에
        있거나 기동에 있거나, 둘 중 하나는 항상 있다.**

        **계약 무변경**: 이 검증은 내부 기동 로직이고 AI→Spring 3s 규약과 미룬 턴 재시도
        스킵은 api-spec §2.9(c)(v0.20.2)에 이미 등재돼 있다 — 이 변경으로 와이어·명세를
        건드리지 않는다(`spring_search_timeout_s` 기본값도 3.0 이라 오늘 값은 그대로다).
        """
        budget = self.spring_search_timeout_s * (self.spring_max_retries + 1)
        if budget >= self.stream_total_timeout_buyer_s:
            raise ValueError(
                "SPRING_SEARCH_TIMEOUT_S * (SPRING_MAX_RETRIES + 1) must be < "
                f"STREAM_TOTAL_TIMEOUT_BUYER_S (got {budget} >= "
                f"{self.stream_total_timeout_buyer_s}): "
                "search retries alone would exhaust the buyer turn budget"
            )
        # [#427] first-token 비교는 progress_events_enabled=False 일 때만 — 위 docstring 참조.
        if not self.progress_events_enabled and budget >= self.stream_first_token_timeout_s:
            raise ValueError(
                "SPRING_SEARCH_TIMEOUT_S * (SPRING_MAX_RETRIES + 1) must be < "
                f"STREAM_FIRST_TOKEN_TIMEOUT_S (got {budget} >= "
                f"{self.stream_first_token_timeout_s}) when PROGRESS_EVENTS_ENABLED=false: "
                "conditions is deferred past the search on auto-relaxable turns (#113), "
                "so search retries consume the first-token budget and would 504"
            )
        counts = _rescue_chain_stage_counts(
            relaxation_max_rounds=self.relaxation_max_rounds,
            auto_fields=self.relaxation_auto_fields,
            chip_fields=self.relaxation_chip_fields,
            category_expand_enabled=self.category_expand_enabled,
        )
        deferred_calls = counts.main + counts.rescue + counts.auto_relax
        if deferred_calls == 0:
            return self
        serial_budget = _rescue_chain_serial_budget_s(
            counts=counts,
            search_timeout_s=self.spring_search_timeout_s,
            spring_max_retries=self.spring_max_retries,
            search_retry_on_deferred_conditions=self.search_retry_on_deferred_conditions,
        )
        recovery = (
            (
                "disable SEARCH_RETRY_ON_DEFERRED_CONDITIONS, "
                if self.search_retry_on_deferred_conditions
                else ""
            )
            + "lower SPRING_SEARCH_TIMEOUT_S, disable deferral with RELAXATION_MAX_ROUNDS=0 "
            "or RELAXATION_AUTO_FIELDS=[], or drop the rescue-fallback call with "
            "CATEGORY_EXPAND_ENABLED=false"
        )
        # [#427] 구매자 전체 상한과는 상시 비교한다 — 위 docstring 참조.
        if serial_budget >= self.stream_total_timeout_buyer_s:
            raise ValueError(
                f"the deferred I-1 serial budget ({deferred_calls} calls) must be < "
                f"STREAM_TOTAL_TIMEOUT_BUYER_S (got {serial_budget} >= "
                f"{self.stream_total_timeout_buyer_s}): {recovery}"
            )
        # [#427] observe 모드일 때만 꼬리 예약을 뺀 값과 비교한다 — 위 docstring 참조.
        if self.rescue_budget_mode == "observe":
            tail_budget = self.stream_total_timeout_buyer_s - self.rescue_tail_reserve_s
            if serial_budget >= tail_budget:
                raise ValueError(
                    f"the deferred I-1 serial budget ({deferred_calls} calls) must be < "
                    "STREAM_TOTAL_TIMEOUT_BUYER_S - RESCUE_TAIL_RESERVE_S "
                    f"(got {serial_budget} >= {tail_budget}) when RESCUE_BUDGET_MODE=observe: "
                    f"{recovery}, or set RESCUE_BUDGET_MODE=narrow so the runtime narrowing "
                    "enforces the tail reserve instead"
                )
        if not self.progress_events_enabled and serial_budget >= self.stream_first_token_timeout_s:
            raise ValueError(
                f"the deferred I-1 serial budget ({deferred_calls} calls) must be < "
                f"STREAM_FIRST_TOKEN_TIMEOUT_S (got {serial_budget} >= "
                f"{self.stream_first_token_timeout_s}) when PROGRESS_EVENTS_ENABLED=false: "
                f"deferred conditions put {deferred_calls} serial I-1 calls before the first "
                f"event; {recovery}"
            )
        return self

    @model_validator(mode="after")
    def _require_degrade_notices_present(self) -> "Settings":
        """계약이 요구하는 degrade 고지 문구가 비면 기동 실패 (#133, PR #235 리뷰).

        문안은 튜너블이지만 **안내를 낼지 말지는 튜너블이 아니다.** api-spec §3.3 은 rerank
        폴백에 "품질 저하를 고지한다", push 실패에 "지연 안내가 포함되며"로 **발신 자체**를
        규정한다. 값이 비면 `graph.py` 의 `if comment:` 가 거짓이 되어 안내가 **조용히**
        사라지고, 서버는 멀쩡히 돌면서 계약만 어긴다 — 아무도 모른다.

        정제(`_strip_unsafe`) **후** 값으로 검사한다. zero-width·제어문자만 든 문자열은 길이는
        1 이상이라 `min_length` 를 통과하지만 정제 뒤 비어 같은 구멍이 된다.

        `dedup_skipped_notice` 는 **여기 없다** — 계약이 요구하지 않고 미고지가 문서화된
        판단이라, 빈 값이 정상적인 의사표현이다.
        """
        from app.core.text import _strip_unsafe  # 지연 import — config 는 최하위 모듈이다

        # 값과 함께 **근거 §** 를 든다 — 어느 계약이 이 발신을 요구하는지가 항목마다 다르다.
        required = {
            "RERANK_FALLBACK_NOTICE": (self.rerank_fallback_notice, "§3.3"),
            "PUSH_SKIPPED_NOTICE": (self.push_skipped_notice, "§3.3"),
            # [#162] 조건 없음 안내도 같은 이유로 필수다 — 빠지면 사용자가 인기상품·취향 기반
            # 결과를 자기 조건이 반영된 결과로 오해하고, 서버는 멀쩡히 돌아 드러나지 않는다.
            "NO_CONDITION_NOTICE_POPULAR": (self.no_condition_notice_popular, "§4.17"),
            "NO_CONDITION_NOTICE_PROFILE": (self.no_condition_notice_profile, "§4.17"),
            "NO_CONDITION_NOTICE_BUDGET": (self.no_condition_notice_budget, "§4.17"),
            # [#393] 카테고리 매핑 드롭 + 0건 → 인기 상품 대체 고지도 같은 이유로 필수다.
            "CATEGORY_UNMAPPED_NOTICE": (self.category_unmapped_notice, "§4.17"),
        }
        for name, (value, section) in required.items():
            if not _strip_unsafe(value):
                raise ValueError(
                    f"{name} must not be empty: api-spec {section} requires the disclosure "
                    "to be sent (the wording is tunable, sending it is not)"
                )
        return self

    @model_validator(mode="after")
    def _require_known_relaxation_chip_fields(self) -> "Settings":
        """완화 칩 대상에 모르는 필드명이 있으면 기동 실패 (#113, PR #248 리뷰).

        `build_relaxation_candidates` 는 모르는 이름을 `continue` 로 건너뛴다 — 카테고리가 실수로
        들어와도 후보가 되지 않게 하는 이중 방어인데, **오타에는 그 관대함이 독**이 된다.
        `"pricemax"`(m 소문자) 하나면 기동은 멀쩡히 성공하고 가격 완화 칩만 영구히 안 나오는데
        아무도 이유를 모른다. 형제 설정(`relaxation_auto_fields`)은 바로 아래에서 기동 시점에
        검증하는데 이쪽만 조용히 무해화(silent no-op)되는 비대칭이 있었다.

        빈 목록(= 완화 칩 기능 off)은 정상적인 의사표현이라 막지 않는다.

        허용 집합을 여기 복제하지 않고 `FIELD_TO_ATTR` 를 **지연 import** 하는 이유는 단일 출처를
        지키기 위해서다 — 복제하면 필드가 늘 때 한쪽만 고쳐 검증이 조용히 뒤처진다.
        **전제: `relaxation.py` 는 config 를 import 하지 않는다**(settings 를 인자로 받는 순수
        함수 모듈이다). 그 전제가 깨지면 여기서 순환 import 가 된다 — 다만 Settings 생성 시점
        (=기동)에 ImportError 로 즉시 터지므로 조용히 썩지는 않는다.
        """
        from app.agents.buyer.recommendation.relaxation import (  # 지연 import (위 전제 참조)
            FIELD_TO_ATTR,
        )

        if unknown := sorted(set(self.relaxation_chip_fields) - set(FIELD_TO_ATTR)):
            raise ValueError(
                f"RELAXATION_CHIP_FIELDS contains unknown field(s) {unknown}: "
                f"allowed wire names are {sorted(FIELD_TO_ATTR)} "
                "(category is intentionally excluded from relaxation — see issue #84)"
            )
        # [PR #248 리뷰] 중복도 막는다 — 위 검사는 `set()` 이라 `["priceMax","priceMax"]` 를
        # 통과시키는데, 후보 생성기는 **리스트를 순회**하므로 같은 필드의 후보가 두 개 생긴다.
        # 그러면 같은 조건으로 Spring 을 두 번 재검색하고(예산 낭비) 같은 칩이 화면에 두 번 뜬다.
        # 두 목록 모두 검사한다 — 자동 목록의 중복도 완화 라운드를 헛되이 소모한다.
        for name, values in (
            ("RELAXATION_CHIP_FIELDS", self.relaxation_chip_fields),
            ("RELAXATION_AUTO_FIELDS", self.relaxation_auto_fields),
        ):
            if len(values) != len(set(values)):
                dupes = sorted({v for v in values if values.count(v) > 1})
                raise ValueError(
                    f"{name} contains duplicate field(s) {dupes}: candidates are built by "
                    "iterating the list, so duplicates cause repeated Spring probes and "
                    "duplicate chips on screen."
                )
        return self

    @model_validator(mode="after")
    def _forbid_auto_relaxing_explicit_constraints(self) -> "Settings":
        """사용자 명시 제약을 자동 완화 목록에 넣으면 기동 실패 (#113, PR #248 리뷰).

        REQ-REC-043·AC-REC-08(가격 제약 불가침)은 SPEC 이 **하드 불변식**으로 규정한 것이다.
        그런데 이를 지키는 게 `relaxation_auto_fields` 기본값뿐이라, 운영자가 `"priceMax"` 를
        더하는 순간 "5만원 이하"라고 말한 사용자에게 6만 5천원짜리가 **동의 없이** 노출된다.
        서버는 멀쩡히 돌고 `token` 안내도 나가지만 "동의 전에는 넘지 않는다"는 규칙 자체가
        깨진다 — #133 이 "고지 여부를 튜너블로 두면 정직성이 옵션이 된다"로 막은 것과 같은 종류다.

        **허용 목록 방식**을 쓴다(금지 목록이 아니라) — 나중에 완화 필드가 추가돼도 기본이
        '자동 금지'라 fail-closed 다. 지금 자동 완화가 정당한 건 평점뿐이다: 가격·브랜드·색상은
        사용자가 발화로 명시하는 하드 제약이고, REQ-REC-047 `source` 태깅이 없는 지금은
        "명시인지 파생인지"를 코드가 구분할 수 없어 전부 명시로 보는 게 안전한 쪽이다.
        목록을 **비우는 것**(자동 완화 전면 off)은 정상이라 막지 않는다.
        """
        allowed = {"ratingMin"}
        if forbidden := sorted(set(self.relaxation_auto_fields) - allowed):
            raise ValueError(
                f"RELAXATION_AUTO_FIELDS must not contain {forbidden}: "
                "SPEC REQ-REC-043/AC-REC-08 forbid auto-relaxing user-stated constraints "
                f"without consent (allowed: {sorted(allowed)}; empty list disables auto-relaxation). "
                "Offer them as suggestion chips instead."
            )
        # [PR #248 리뷰] 자동 목록은 칩 목록의 **부분집합**이어야 한다. 완화 후보를 만드는
        # `build_relaxation_candidates` 가 `relaxation_chip_fields` 만 순회하므로, 칩 목록에서 빠진
        # 필드는 자동 목록에 있어도 후보 자체가 안 만들어져 **자동 완화가 조용히 영구 비활성화**된다.
        # 두 값이 개별로는 유효해 기동은 성공하고, 게다가 `may_auto_relax` 는 자동 목록만 보므로
        # 매 턴 conditions 만 헛되이 지연된다 — 설정 **조합**을 여기서 막는다.
        if orphaned := sorted(set(self.relaxation_auto_fields) - set(self.relaxation_chip_fields)):
            raise ValueError(
                f"RELAXATION_AUTO_FIELDS contains field(s) missing from RELAXATION_CHIP_FIELDS: "
                f"{orphaned}. Relaxation candidates are built from the chip list, so those fields "
                "would silently never be auto-relaxed. Add them to RELAXATION_CHIP_FIELDS or "
                "remove them here."
            )
        return self

    @model_validator(mode="after")
    def _require_home_reco_min_within_max(self) -> "Settings":
        """홈 추천(I-22) 후보 하한이 응답 상한을 넘으면 기동 실패 (PR #213 리뷰).

        `rank_home` 은 `k=max(want, home_reco_min_candidates)` 로 top-k 를 조회한다 — 하한이
        `home_reco_max_items`(overfetch 절대 상한, LIMIT_MAX 에 ge 로 묶임)를 넘으면 "응답 크기
        방어"가 조회 단계에서 무력화된다. `expose_max`↔`LIST_MAX_PRODUCTS` 와 같은 방식으로
        관계를 기동 시점에 고정한다 — 한쪽만 튜닝하면 조용히 어긋나는 쌍이다.
        """
        if self.home_reco_min_candidates > self.home_reco_max_items:
            raise ValueError(
                "HOME_RECO_MIN_CANDIDATES must be <= HOME_RECO_MAX_ITEMS "
                f"(got {self.home_reco_min_candidates} > {self.home_reco_max_items}): "
                "candidate floor must not defeat the response-size cap"
            )
        return self

    @model_validator(mode="after")
    def _require_pepper_in_prod(self) -> "Settings":
        """운영(jwks)에서 PII pepper 미주입이면 기동 실패 — 조용히 약한 해시로 도는 것 방지."""
        if self.auth_mode == "jwks" and not self.pii_hash_pepper:
            raise ValueError("PII_HASH_PEPPER must be set when auth_mode=jwks")
        # inbound write 엔드포인트(§3.5) 서비스 토큰 — 운영은 필수(미설정 시 조용히 fail-open 방지).
        if self.auth_mode == "jwks" and not self.internal_api_token:
            raise ValueError("INTERNAL_API_TOKEN must be set when auth_mode=jwks")
        # jwks 모드의 검증 키 소스 — 미설정이면 전 요청 401 폭주라 기동 시점에 fail-fast(#34).
        if self.auth_mode == "jwks" and not self.jwks_url:
            raise ValueError("JWKS_URL must be set when auth_mode=jwks")
        # I-17 배치(§4.8) 임베딩 API 키 — 미설정이면 스케줄러가 5분마다 조용히 실패만
        # 반복한다(PR #42 리뷰, 이슈 #31). 런타임 무한 no-op 대신 기동 시점에 fail-fast.
        if self.auth_mode == "jwks" and not self.google_api_key:
            raise ValueError("GOOGLE_API_KEY must be set when auth_mode=jwks")
        if self.state_store_pool_max_size < 1:
            raise ValueError("STATE_STORE_POOL_MAX_SIZE must be at least 1")
        if self.state_store_migration_timeout_s <= 0:
            raise ValueError("STATE_STORE_MIGRATION_TIMEOUT_S must be positive")
        min_claim_ttl = self.llm_timeout_s * (self.llm_max_retries + 1) * 2
        if self.session_end_claim_ttl_s <= min_claim_ttl:
            raise ValueError("SESSION_END_CLAIM_TTL_S must exceed the two-stage LLM timeout budget")
        if self.profile_session_idle_timeout_s <= 0:
            raise ValueError("PROFILE_SESSION_IDLE_TIMEOUT_S must be positive")
        if self.profile_idle_sweep_interval_s <= 0:
            raise ValueError("PROFILE_IDLE_SWEEP_INTERVAL_S must be positive")
        if self.profile_idle_sweep_batch_size <= 0:
            raise ValueError("PROFILE_IDLE_SWEEP_BATCH_SIZE must be positive")
        if self.profile_idle_max_concurrency <= 0:
            raise ValueError("PROFILE_IDLE_MAX_CONCURRENCY must be positive")
        if self.session_lifecycle_legacy_grace_s < 86400:
            raise ValueError("SESSION_LIFECYCLE_LEGACY_GRACE_S must be at least 86400")
        if self.session_lifecycle_legacy_quiet_s < max(90, self.stream_total_timeout_s):
            raise ValueError(
                "SESSION_LIFECYCLE_LEGACY_QUIET_S must cover STREAM_TOTAL_TIMEOUT_S "
                "and be at least 90"
            )
        if self.session_lifecycle_gc_batch_size <= 0:
            raise ValueError("SESSION_LIFECYCLE_GC_BATCH_SIZE must be positive")
        if self.session_lifecycle_backfill_max_batches <= 0:
            raise ValueError("SESSION_LIFECYCLE_BACKFILL_MAX_BATCHES must be positive")
        idle_batch_waves = (
            self.profile_idle_sweep_batch_size + self.profile_idle_max_concurrency - 1
        ) // self.profile_idle_max_concurrency
        if self.profile_idle_claim_ttl_s <= idle_batch_waves * min_claim_ttl:
            raise ValueError(
                "PROFILE_IDLE_CLAIM_TTL_S must exceed the two-stage LLM timeout budget "
                "for all configured batch waves"
            )
        if self.state_store_pool_min_size < 0:
            raise ValueError("STATE_STORE_POOL_MIN_SIZE must be non-negative")
        if self.state_store_pool_min_size > self.state_store_pool_max_size:
            raise ValueError("STATE_STORE_POOL_MIN_SIZE must not exceed max size")
        if self.state_store_local_cache_max_entries <= 0:
            raise ValueError("STATE_STORE_LOCAL_CACHE_MAX_ENTRIES must be positive")
        # 등급 티어 경계 순서 불변식(#171 PR#172) — _rating_tier/_review_tier 가 내림차순 순차
        # 비교(if r>=excellent .. >=good .. >=fair)라, env 로 순서가 뒤집히면 중간 구간이 조용히
        # 엉뚱한 등급으로 나간다. 기동 시점 fail-fast 로 오설정을 막는다.
        if not (self.rating_tier_excellent >= self.rating_tier_good >= self.rating_tier_fair):
            raise ValueError(
                "RATING_TIER 경계는 excellent >= good >= fair 여야 합니다"
                f" ({self.rating_tier_excellent}/{self.rating_tier_good}/{self.rating_tier_fair})"
            )
        # 기간 기본값·상한 정합(#269 리뷰) — calc.normalize_period 는 기간 미지정("최근")일
        # 때 n=recent_default_days 로 두고 곧바로 n>max_days 상한 검사를 통과시킨다.
        # 상한을 기본값보다 낮게 내리면 가장 흔한 발화("최근 매출 어때?")조차 매번
        # "기간이 너무 깁니다" 되묻기로 빠진다 — 현재 기본값(7 <= 731)에선 안 드러난다.
        if self.seller_recent_days_default > self.seller_period_max_days:
            raise ValueError(
                "SELLER_RECENT_DAYS_DEFAULT 는 SELLER_PERIOD_MAX_DAYS 이하여야 합니다"
                f" (default={self.seller_recent_days_default},"
                f" max={self.seller_period_max_days})"
            )
        if self.seller_recent_days_default < 1:
            raise ValueError(
                "SELLER_RECENT_DAYS_DEFAULT 는 1 이상이어야 합니다"
                f" (got {self.seller_recent_days_default})"
            )
        # ── 분석 계산 층 정합(#290) — env 오설정이면 조회 도구가 매 요청 실패하므로
        # 기동 시점 fail-fast(#194 seller_ma window 검증과 같은 취지). ──
        if self.seller_stl_period < 2:
            # statsmodels STL 은 period>=2 요구 — 1 이면 계절 성분 정의 불가.
            raise ValueError(
                f"SELLER_STL_PERIOD 는 2 이상이어야 합니다 (got {self.seller_stl_period})"
            )
        if self.seller_min_history_for_stl < 2 * self.seller_stl_period:
            # STL 은 최소 2 주기 이력이 필요하다(Cleveland 1990) — 미만 설정이면 폴백
            # 경계(min_history_for_stl)를 통과한 입력이 STL 내부에서 죽는다.
            raise ValueError(
                "SELLER_MIN_HISTORY_FOR_STL 은 SELLER_STL_PERIOD 의 2배 이상이어야 합니다"
                f" (min_history={self.seller_min_history_for_stl}, period={self.seller_stl_period})"
            )
        if self.seller_analysis_lookback_days < self.seller_min_history_for_stl:
            # lookback 이 STL 최소 이력보다 짧으면 확장 조회를 하고도 상시 폴백이라
            # lookback 비용만 내고 STL 은 영영 못 쓴다(무음 무효화 방지).
            raise ValueError(
                "SELLER_ANALYSIS_LOOKBACK_DAYS 는 SELLER_MIN_HISTORY_FOR_STL 이상이어야 합니다"
                f" (lookback={self.seller_analysis_lookback_days},"
                f" min_history={self.seller_min_history_for_stl})"
            )
        for alpha_name, alpha_value in (
            ("SELLER_GESD_ALPHA", self.seller_gesd_alpha),
            ("SELLER_RATE_TEST_ALPHA", self.seller_rate_test_alpha),
        ):
            if not 0.0 < alpha_value < 1.0:
                raise ValueError(f"{alpha_name} 는 (0, 1) 구간이어야 합니다 (got {alpha_value})")
        if not 0.0 < self.seller_wilson_confidence < 1.0:
            raise ValueError(
                f"SELLER_WILSON_CONFIDENCE 는 (0, 1) 구간이어야 합니다"
                f" (got {self.seller_wilson_confidence})"
            )
        if not 0.0 < self.seller_gesd_max_anomalies_ratio <= 0.49:
            # GESD 는 이상점 수 < 표본의 절반 전제 — 0.49 초과는 검정 전제 붕괴(S-H-ESD §3).
            raise ValueError(
                "SELLER_GESD_MAX_ANOMALIES_RATIO 는 (0, 0.49] 구간이어야 합니다"
                f" (got {self.seller_gesd_max_anomalies_ratio})"
            )
        if self.seller_mad_threshold <= 0 or self.seller_tukey_k <= 0:
            raise ValueError(
                "SELLER_MAD_THRESHOLD·SELLER_TUKEY_K 는 양수여야 합니다"
                f" (mad={self.seller_mad_threshold}, tukey={self.seller_tukey_k})"
            )
        if not (0 <= self.seller_night_hours_start < self.seller_night_hours_end <= 24):
            raise ValueError(
                "심야 시간대는 0 <= start < end <= 24 여야 합니다"
                f" (start={self.seller_night_hours_start}, end={self.seller_night_hours_end})"
            )
        if not 2 <= self.seller_behavior_kmeans_k_min <= self.seller_behavior_kmeans_k_max:
            # k<2 는 군집이 아니라 전체 1군집이라 무의미 — sklearn 도 n_clusters>=2 를 요구.
            raise ValueError(
                "SELLER_BEHAVIOR_KMEANS_K 범위는 2 <= k_min <= k_max 여야 합니다"
                f" (k_min={self.seller_behavior_kmeans_k_min},"
                f" k_max={self.seller_behavior_kmeans_k_max})"
            )
        if self.seller_churn_signal_top_k < 1:
            raise ValueError(
                f"SELLER_CHURN_SIGNAL_TOP_K 는 1 이상이어야 합니다"
                f" (got {self.seller_churn_signal_top_k})"
            )
        if not (self.review_tier_many >= self.review_tier_some >= self.review_tier_few):
            raise ValueError(
                "REVIEW_TIER 경계는 many >= some >= few 여야 합니다"
                f" ({self.review_tier_many}/{self.review_tier_some}/{self.review_tier_few})"
            )
        # '저렴/비쌈'은 중앙값(1.0) 기준 아래/위이므로 경계 순서뿐 아니라 방향도 고정한다.
        # 각 등급이 도달 가능해야 한다 — 양끝 경계가 같으면 중간 등급이 죽는다.
        if not (
            0
            < self.price_tier_very_cheap_ratio
            < self.price_tier_cheap_ratio
            <= 1.0
            <= self.price_tier_pricey_ratio
            < self.price_tier_very_pricey_ratio
            and self.price_tier_cheap_ratio < self.price_tier_pricey_ratio
        ):
            raise ValueError(
                "PRICE_TIER 경계는 0 < very_cheap < cheap <= 1.0 <= pricey < very_pricey 이고"
                " cheap < pricey 여야 합니다"
                f" ({self.price_tier_very_cheap_ratio}/{self.price_tier_cheap_ratio}/"
                f"{self.price_tier_pricey_ratio}/{self.price_tier_very_pricey_ratio})"
            )
        # 노출 개수 경계(REQ-REC-021) — expose_min 은 "부족하면 검색순서로 채우는" 하한이라
        # 상한을 넘으면 보충 루프가 곧바로 상한 절단에 되잘리는 모순이 된다. 개별 le 로는
        # 두 값의 관계를 못 잡아 여기서 함께 본다.
        if self.expose_min > self.expose_max:
            raise ValueError(
                "EXPOSE_MIN 은 EXPOSE_MAX 이하여야 합니다"
                f" (min={self.expose_min}, max={self.expose_max})"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴. FastAPI 의존성/모듈에서 재사용한다."""
    return Settings()
