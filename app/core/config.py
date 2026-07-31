"""애플리케이션 설정 (pydantic-settings).

프로젝트 원칙 "config 주입": 모든 튜너블(모델 ID, DB URL, 인증(JWKS/iss/aud),
Spring base URL, 검색 파라미터)을 환경변수로 주입하여 코드 변경 없이 교체 가능하게 유지한다.

[2026-07-15] MVP 후보 검색은 Spring 위임(GET /internal/products/search, I-1)이며 상품 원본
컬럼의 AI측 사본(카탈로그 미러)은 두지 않는다.
[2026-07-20 정정] enrichment·임베딩(§4.8 I-17 배치)은 MVP 편입 확정 — 임베딩 검색 방식1·2를
SearchBackend로 구현해 골든셋 확정(api-spec §4.8 말미·§4.6, C-17). 구 "post-MVP" 표기 폐기.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# I-21 계약 하드 상한(api-spec §4.2) — 노출 개수 설정이 계약을 넘지 못하게 묶는 기준.
# 계약 값의 단일 출처는 스키마다(app/schemas/spring.py) — 여기서 숫자를 다시 적지 않는다.
from app.schemas.recommendations import LIMIT_MAX as HOME_RECO_LIMIT_MAX
from app.schemas.spring import LIST_MAX_PRODUCTS, MAX_LISTS

LLMProvider = Literal["openai", "anthropic"]
# 검색 백엔드 선택(#101) — spring: Spring 위임만(방식1 이전 MVP), embedding_rerank: Spring 전량 →
# pgvector 의미 재정렬(방식2, MVP 기본), vector: AI 벡터검색 → Spring hydrate(방식1, C-17 미착수).
SearchBackend = Literal["spring", "embedding_rerank", "vector"]


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
    seller_recent_days_default: int = 7  # normalize_period "최근 N일" 기본 N
    # safe_eval `**` 결과 자릿수 상한(DoS 방어) — 초과 식은 ValueError 로 거부(리뷰 반영).
    seller_calc_max_result_digits: int = 100
    # 도구 반환 상세도 상한(안 1+차등, 2026-07-17 사용자 확정) — 컨텍스트 폭주 방지.
    seller_summary_max_points: int = 60  # 시계열 상세 나열 상한(포인트 수)
    seller_summary_max_events: int = 5  # I-14 이벤트 kv 나열 상한(건)
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

    # ── 홈 추천 랭킹 (I-22, api-spec §3.7 · 이슈 #148) ──
    # 질의 벡터 = 시그널 상품 임베딩의 가중 평균. cart 는 "담기까지 갔다"는 강한 신호라 조회보다 높게,
    # 조회는 최신일수록 높게(recency decay 를 인덱스 거듭제곱으로 적용) — §3.7 signals 표.
    home_reco_weight_cart: float = Field(default=1.0, ge=0.0)
    home_reco_weight_viewed: float = Field(default=0.6, ge=0.0)
    # [#148] 장기 취향 항 — 프로필 요약 벡터(sleep-time consolidation 이 미리 만든다).
    # cart(지금 담은 것)보다 낮게 둔다: 오래된 취향이 현재 관심을 덮으면 홈이 안 바뀐 것처럼 보인다.
    # 0 으로 두면 프로필이 랭킹에서 빠지고 reason 근거로만 쓰인다(롤백 스위치).
    home_reco_weight_profile: float = Field(default=0.5, ge=0.0)
    home_reco_viewed_decay: float = Field(default=0.85, gt=0.0, le=1.0)
    # limit 은 최종 노출 목표치 — Spring 의 품절 드롭에 대비해 이 배수만큼 넉넉히 반환한다(§3.7).
    home_reco_overfetch_ratio: float = Field(default=2.0, ge=1.0)
    # overfetch 절대 상한(응답 크기 방어). **요청 `limit` 상한(`LIMIT_MAX`) 이상이어야 한다** —
    # 아래로 내려가면 `_overfetch_size` 가 요청받은 `limit` 보다 적게 반환해 "품절 드롭 대비
    # 넉넉히"(§3.7)가 깨진다. 기동 시점에 잡는다(`expose_max`/LIST_MAX_PRODUCTS 와 같은 방식).
    # 기본값은 LIMIT_MAX 의 2배(= 기본 overfetch 배율) — LIMIT_MAX 와 같게 두면 `limit` 이
    # 상한에 가까울수록 여유분이 0 으로 죽어 "넉넉히" 계약이 최댓값에서 깨진다(PR #213 리뷰).
    home_reco_max_items: int = Field(default=HOME_RECO_LIMIT_MAX * 2, ge=HOME_RECO_LIMIT_MAX)
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
    category_select_max_calls: int = Field(default=2, ge=0)

    # ── 목적·상황형 발화의 상품 전개 (이슈 #198, DESIGN-NEEDS-EXPANSION-198) ──
    # "집들이 선물" 처럼 무엇을 살지 사용자가 말하지 않은 발화를 구체 상품 목록으로 전개한다
    # (정본 SPEC-RECOMMEND-001 §5.1 shopping_list 분해, EX-7 v0.10.0 개정으로 전용 호출 허용).
    # 전개 실패는 **코드가 결정적으로 감지**한다(설계 §4 D1~D3) — LLM 자기 보고(case)는 전개와 같은
    # 호출의 산출물이라 실패 회차의 값을 신뢰할 수 없음이 실측으로 확인됐다(§4.1).
    # 목적 표현 marker — leg query 가 이것으로 **끝나면** 목적 표현으로 본다. `in` 이 아니라
    # `endswith` 인 이유: '한우 선물세트'·'과일 선물세트' 같은 정당한 상품명이 marker '선물' 에 걸려
    # 오탐된다('집들이 선물'.endswith('선물')=True / '한우 선물세트'.endswith('선물')=False).
    # 실측 기반 초기값이며 관측 로그(needs_expansion_triggered.reason) 분포로 조정한다(설계 OPEN-1).
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
    needs_expansion_purpose_markers: list[str] = [
        "선물",
        "답례품",
        "준비물",
        "용품",
        "아이템",
        "키트",
        "물품",
        "추천",
        "것",
        "거",
    ]

    # ── 장바구니 (이슈 #3, api-spec §4.1) ──
    # CART_OPTION_INVALID 재질문 상한 — 초과 시 action CART_ERROR(§4.1). 하드코딩 금지.
    cart_option_reask_max: int = 1

    # ── dedup (#4, api-spec §4.7 결정 14-F) ──
    # 최근 구매 제외 윈도우(일) — 이보다 오래된 구매는 제외 목록에서 뺀다(영구 제외 방지).
    dedup_recent_days: int = 90
    # 소모품 카테고리(결정 14-F 억제 대상) — MVP config 소스. 정본은 catalog 속성사전
    # (SPEC-CATALOG-DATA-001 REQ-CAT-013 소모품 boolean 플래그). 카테고리명은 BE categoryName 과 일치.
    consumable_categories: list[str] = []

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
    # disconnect 감지 폴링 간격 (취소 = 연결 종료, §2.9 b).
    stream_disconnect_poll_s: float = 0.5
    # AI→Spring 콜백 타임아웃 (§2.9 c, BE I-2 기준 통일). 실제 호출부에서 사용.
    spring_timeout_s: float = 3.0
    # AI→LLM 단일 호출 타임아웃 + 재시도 횟수 (§2.9 c).
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 1

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

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_llm_provider(cls, value: object) -> object:
        """기존 환경변수 호환을 위해 provider 값의 ASCII 대소문자를 정규화한다."""
        return value.lower() if isinstance(value, str) else value

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
