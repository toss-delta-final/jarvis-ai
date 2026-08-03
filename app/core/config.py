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
from functools import lru_cache
from typing import Literal

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
ROUTE_INTENTS = frozenset({"recommend", "cart_add", "cart_view", "order_status", "general"})


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
    catalog_batch_page_size: int = 500  # I-17 배치 페이지 크기(§4.8, config 주입)
    catalog_vector_overfetch: int = 4  # 방식1 hydrate 후 필터·품절 제거 대비 벡터 여유조회 배수
    catalog_batch_interval_s: float = 300.0  # 주기 증분 pull 배치 스케줄러 간격(이슈 #31)
    # pg-catalog 질의 statement_timeout — get_many·top_k_by_vector 의 DB 쪽 상한(PR #213 리뷰).
    # 앱쪽 벽시계 포기는 스레드 밑의 쿼리를 못 죽이므로, 이게 없으면 지연 쿼리(I-17 replace_all
    # 테이블 락 등)가 풀 커넥션을 계속 붙들어 채팅 rerank 등 다른 경로까지 말려든다.
    # **앱쪽 호출 상한(home_reco_store_timeout_s)보다 커야 한다** — 같거나 작으면 "쿼리가
    # 느리다"는 동일 원인이 어느 타이머가 먼저 발동하느냐에 따라 503/504 로 비결정적으로
    # 갈린다(PR 리뷰: DB 가 먼저 끊으면 psycopg QueryCanceled → except Exception → 503).
    # 관계는 기동 시점에 강제한다(아래 model_validator).
    catalog_store_query_timeout_s: float = Field(default=2.5, gt=0.0)

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

    # ── 판매자 분석 임계값 (app/agents/seller/calc.py 주입, 하드코딩 금지) ──
    seller_ma_window: int = 7  # 매출 이동평균 window(일) — Spring MOVING_WINDOW 정렬
    # 이상판정 최소 표본 수(직전 포인트 수) — Spring(SellerSalesService) MIN_WINDOW 정렬(#194).
    seller_ma_min_window: int = 3
    seller_anomaly_deviation_pct: float = 30.0  # 매출 이상판정 편차 임계(%)
    seller_conversion_drop_pct: float = 20.0  # 전환율 하락 이상 임계(%)
    seller_churn_inactive_days: int = 30  # 이탈 코호트 무활동 일수(I-16 inactiveDays 기본)
    # [#197 PR 리뷰] I-8 계정/보안 이벤트는 전역 데이터(브랜드 스코프 아님)이고
    # admin 소유 협의가 미완(🔴, api-spec §4.4 v0.19.1)이다. 종전엔 코드 결함(쿼리
    # 400·스키마 미스매치)이 사실상 차단막이었으나 #197 정합으로 실노출이 가능해져,
    # 협의 완료 전까지 판매자 워커 표면 노출을 기본 비활성으로 보류한다.
    seller_account_events_enabled: bool = False
    seller_recent_days_default: int = 7  # normalize_period "최근 N일" 기본 N
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
    # 완화 후보 probe(재검색) 상한 — estCount 는 page-local 로 못 구한다(가격·브랜드·색상은 Spring
    # 쿼리 파라미터라 탈락 상품이 응답에 아예 없다, spring.py ProductSearchResult docstring 참조).
    # 그래서 후보마다 완화 필터로 재검색해 실제 매칭 수를 센다. fan-out 턴은 leg 수만큼 곱해지므로
    # 낮게 잡는다. 자동 완화 시도와 칩 probe 가 **이 예산을 공유**한다.
    relaxation_max_probes: int = Field(default=2, ge=0)
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
    review_tier_many: int = 100  # ≥ → 매우많음
    review_tier_some: int = 20  # ≥ → 많음
    review_tier_few: int = 5  # ≥ → 보통 (그 미만 적음)
    # price 는 절대 기준이 없어 후보 그룹 중앙값 대비 상대 등급으로만 전달한다(#173).
    price_tier_very_cheap_ratio: float = 0.6  # 그룹 중앙값 대비 ≤ → 매우저렴
    price_tier_cheap_ratio: float = 0.85  # ≤ → 저렴
    price_tier_pricey_ratio: float = 1.15  # ≥ → 비쌈
    price_tier_very_pricey_ratio: float = 1.5  # ≥ → 매우비쌈

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
    # [#115] 최근접 채택 상한 — 채택 거리가 이 값을 **초과**하면 그 leg 를 canonical 없이 드롭한다
    # (§4 거리 조건부 채택. 종전 never-null "멀어도 억지로 채택"은 폐기). 거리 0.22 초과는 "맞는 칸이
    # taxonomy 에 없다"의 신호다 — "부모님 환갑 선물"이 출산/돌기념품(0.2971)으로 붕괴하는 식.
    # ⚠️ 재튜닝 조건: 이 값은 **임베딩 모델·task_type·사전에 종속**된다(gemini-embedding-001 1536-dim
    # L2 정규화 + 앵커 RETRIEVAL_QUERY / 시드 RETRIEVAL_DOCUMENT, categories 2056 leaf 기준 실측).
    # 셋 중 하나라도 바뀌면 재측정 없이는 무효다. 실측 경계 여유가 0.005 뿐이므로(정답 최대 0.2168
    # vs 오분류 최소 0.2221) §11 거리 로그로 분포를 관측하며 조정한다.
    # 절단 튜너블(ge=0)이 아니라 비교 임계라 코사인 거리 정의역 [0,2] 로 범위 검증한다.
    category_distance_max: float = Field(default=0.22, ge=0.0, le=2.0)
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
    category_distance_override_margin: float = Field(default=0.035, ge=0.0, le=2.0)
    # [#115] top-k LLM 택일 트리거(§4.4) — 마진(2위−1위 거리차)이 이 값 **이하**면 애매한 판정으로
    # 보고 select_category 로 후보 중 택일한다. 거리컷이 못 잡는 구멍용: 추상 라벨('선물용품')은
    # 거리 0.2074(컷 통과)인데 뜻이 틀리고, 마진은 0.0095 로 얇다. 마진을 드롭 조건으로 쓰면
    # '양말'(1·2위 둘 다 정답, 마진 0.0088)을 오탐하므로 드롭이 아니라 택일 트리거로만 쓴다.
    category_select_margin_max: float = Field(default=0.02, ge=0.0, le=2.0)
    # 턴당 택일 LLM 호출 상한 — fan-out 5 leg 이 모두 애매하면 턴 LLM 이 2→7회로 뛴다. 초과 leg 는
    # 임베딩 top-1 을 그대로 쓴다(종전 동작). ge=0 — 0 이면 택일 기능 off.
    # ⚠️ **턴당**이 계약이다(#217 PR 리뷰). `map_categories` 는 이 값을 호출 단위로 적용하므로,
    # #217 로 매핑이 턴에 2회 불리게 된 뒤로는 **호출부가 남은 예산을 계산해 넘겨야** 상한이 지켜진다
    # (`graph._map_or_empty(select_max_calls=...)` ← `CategoryMapping.select_calls`).
    # 매핑을 부르는 새 경로를 만들 때 이 배선을 빠뜨리면 상한이 조용히 배수로 깨진다.
    category_select_max_calls: int = Field(default=2, ge=0)

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

    # ── 장바구니 (이슈 #3, api-spec §4.1) ──
    # CART_OPTION_INVALID 재질문 상한 — 초과 시 action CART_ERROR(§4.1). 하드코딩 금지.
    cart_option_reask_max: int = 1

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
    profile_buffer_excluded_intents: list[str] = ["order_status", "cart_view"]
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
    # [#133] I-1 검색 재시도 횟수 (SPEC-RECOMMEND-001 §오류처리가 이미 규정한 동작).
    # 타임아웃 3s 는 일시 지연이 재시도로 살아나는 폭인데 LLM 만 재시도를 갖고 검색은 0회였다.
    # **재시도가 의미 있는 실패만** 대상이다 — 타임아웃·연결 오류·응답 중단·5xx·일시 4xx(408·429). 4xx 계약 오류와 응답
    # 파싱 실패는 다시 불러도 같은 결과라 즉시 실패한다. 비멱등 호출(I-2 담기)에는 걸지 않는다.
    # 재시도 사이 sleep 은 두지 않는다 — 타임아웃 실패는 이미 3s 간격이 생기고 1회로는 herd
    # 증폭이 2배를 넘지 않는다.
    # **상한이 1인 이유(PR #235 리뷰)**: backoff 가 구현에 없다. 2·3 을 허용하면 "1 을 넘기려면
    # backoff 가 필요하다"고 적어 둔 위험을 설정 한 줄로 열어 주는 셈이라, **현재 구현이 감당하는
    # 값만** 받는다. 더 올리려면 backoff 를 먼저 만들고 이 상한을 함께 푼다.
    spring_max_retries: int = Field(default=1, ge=0, le=1)
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
    # 초기 데이터셋은 30~50건으로 작게 시작해 사람이 전수 검수할 수 있게 한다.
    goldenset_min_cases: int = 30
    goldenset_max_cases: int = 50
    # 문자 3-gram Jaccard가 이 값을 넘는 split 간 query는 leakage로 본다.
    goldenset_near_dup_jaccard_max: float = 0.6
    # split 간 정답 집합이 절반보다 많이 겹치면 동일 시나리오 누출로 본다.
    goldenset_near_dup_relevant_overlap_max: float = 0.5
    # I-1의 AI 후보 기본 limit과 맞춰 질의별 기록량을 유계로 둔다.
    goldenset_snapshot_per_query_max: int = 30
    # 43건 중 12건을 봉인하는 v1 목표 비중이며 감사 보고에 사용한다.
    goldenset_holdout_ratio: float = 0.3

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
    def _require_search_retry_within_stream_budget(self) -> "Settings":
        """I-1 검색 재시도 총량이 스트림 전체 상한을 넘으면 기동 실패 (#133).

        **first-token 상한이 아니라 전체 상한과 비교하는 이유**(PR #241/#138 lessons 로 정정):
        `stream_first_token_timeout_s` 가 재는 것은 §2.9 c 의 **첫 SSE 이벤트**까지인데, 추천
        경로의 첫 이벤트는 `conditions`(`recommendation/graph.py`)이고 **검색은 그 뒤**에 돈다.
        즉 검색 재시도는 first-token 예산을 한 톨도 쓰지 않는다 — 초판이 파이프라인 그림만 보고
        "검색이 첫 토큰보다 앞"이라 적었던 것은 **emit 순서를 코드로 확인하지 않은 오류**다.

        재시도가 실제로 갉아먹는 것은 턴 전체 시간이므로 전체 상한과 묶는다. `llm_timeout_s *
        (llm_max_retries + 1)` 과 같은 결의 예산식이며, 한쪽만 튜닝하면 조용히 어긋나는 쌍이라
        기동 시점에 고정한다.

        비교 대상은 **구매자 전체 상한**(`stream_total_timeout_buyer_s`, #138)이다 — I-1 검색은
        구매자 추천 경로에서만 돌고, 그 경로를 실제로 끊는 것은 판매자와 공용인 90s 가 아니라
        구매자 전용 30s 다. 느슨한 쪽과 비교하면 검증이 이름만 남는다.
        """
        budget = self.spring_timeout_s * (self.spring_max_retries + 1)
        if budget >= self.stream_total_timeout_buyer_s:
            raise ValueError(
                "SPRING_TIMEOUT_S * (SPRING_MAX_RETRIES + 1) must be < "
                f"STREAM_TOTAL_TIMEOUT_BUYER_S (got {budget} >= "
                f"{self.stream_total_timeout_buyer_s}): "
                "search retries alone would exhaust the buyer turn budget"
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

        required = {
            "RERANK_FALLBACK_NOTICE": self.rerank_fallback_notice,
            "PUSH_SKIPPED_NOTICE": self.push_skipped_notice,
        }
        for name, value in required.items():
            if not _strip_unsafe(value):
                raise ValueError(
                    f"{name} must not be empty: api-spec §3.3 requires the degrade disclosure "
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
        # 이상 감지 window 정합(#194 PR 리뷰) — env 오설정(min_window ≤ 0 또는
        # min_window > window)이면 calc.detect_sales_anomalies 가 daily 매출 조회
        # 매 요청마다 ValueError 로 죽는다. 설정값은 요청마다 변하지 않으므로
        # 런타임 반복 실패 대신 기동 시점에 fail-fast 한다.
        if self.seller_ma_min_window < 1 or self.seller_ma_window < self.seller_ma_min_window:
            raise ValueError(
                "SELLER_MA_MIN_WINDOW 는 1 이상, SELLER_MA_WINDOW 이하여야 합니다"
                f" (min_window={self.seller_ma_min_window}, window={self.seller_ma_window})"
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
