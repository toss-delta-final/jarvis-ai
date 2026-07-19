# HANDOFF — 판매자 멀티에이전트 작업 인수인계 (2026-07-18 v2, 2단계 마감)

> 새 세션 시작 시 이 문서를 먼저 읽는다. 다음 작업은 **3단계 첫 소작업(3-1)** (아래 §4).
> 작업 저장소: `C:\Users\vssea\jarvis-ai` · 강의자료: `C:\Users\vssea\01_LangGraph\01_LangGraph`
> 2단계 상세·리뷰 기록: `docs/specs/REVIEW-SELLER-STAGE2.md` (보류 R1~R7 · 이월 C1~C5 표 포함)

## 1. 범위·정본·핵심 규칙 (전부 사용자 확정 사항)

- **판매자 파트만** 구현한다 — 구매자·추천 코드는 절대 수정 금지.
- bottom-to-top 4단계: **tool(1단계 완료) → 서브에이전트(2단계 완료) → 에이전트/파이프라인(3단계 ← 다음) → 멀티에이전트(4단계)**.
- **`create_agent` 사용, StateGraph 수작업 조립 금지** — SPEC의 Send 팬아웃·검증 루프는 순수 파이썬(asyncio.gather·for 루프)으로 구현.
- 계약 정본: 리포 `docs/api-spec.md` **v0.14.0 사본** + `docs/specs/SPEC-SELLER-001.md`. CSV 대조 완료(I-6~I-16 일치). 대조 기록: `docs/specs/DESIGN-SELLER-TOOLS-STAGE1.md` 부록 A.
- "판매자 명세서" 폴더 문서(API v2/S-3 체계)는 **참고만**.
- 전체 계획: `docs/specs/IMPL-PLAN-SELLER-001.md` (ToolRuntime 관련 서술은 확정으로 대체됨).
- **강의자료 정합 확인 완료**(2026-07-18): create_agent·ToolStrategy·ToolRuntime·init_chat_model 사용법이 `05_Agent_Development` 노트북 패턴과 일치. 3단계는 `06_Middleware`, 4단계는 `09_Multi_Agent`(02-Supervisor·06-Router-Pattern)·`07_Memory`·`08_RAG` 참조.

### 진행 방식 (사용자 강력 요구 — 반드시 유지)

- **한 턴 = 한 소작업, 10분 이내.** 절대 한 번에 코드 전부 생성 금지.
- 각 소작업: **설계 요지(필드 표 등) 먼저 제시 → 사용자 확정 → 구현 → 변경 요약 보고.** 모호한 부분·수정 사항은 전부 보고하고 상호작용.
- **git 커밋 금지** (사용자가 직접). 파일은 device_commit_files 로 로컬 반영만.
- pytest·ruff는 **사용자 로컬 실행** → 결과를 붙여받아 확인. (클라우드는 PyPI 403 — py_compile·pydantic 자체검증까지만 가능.)
- 무거운 에이전트 파이프라인 금지 — 직접 수정 위주, **opus 적대 리뷰는 단계 마감 시 1회만**(2단계분 완료, 3단계 마감 시 1회).

### 기술 확정 사항 (1~2단계 누적)

- **ToolRuntime 방식**: 도구는 모듈 레벨 1회 정의, 신원은 `ToolRuntime[SellerContext]` 주입. 신원은 어떤 @tool 시그니처에도 없음(IDOR) — 테스트가 강제.
- `SellerContext`(frozen dataclass): 신원만(seller_id, brand_id). SpringClient는 `get_spring_client()` 싱글턴.
- 도구 반환 상세도 = 안 1+차등(Settings 상한 3종). 이상 판정은 도구 내장(daily만, Spring isAnomaly 무시). degrade: raise 금지, `"Error: ..."` 한국어 문자열.
- 런타임 모델(SPEC §8): Haiku 4.5 t=0(supervisor·planner·워커·judge·**product**) / Sonnet 5 t=0.2(report·recommend) — `models.py` ROLE_TIER + Settings(seller_haiku_temperature=0.0/seller_sonnet_temperature=0.2). **product 역할은 §8 표에 없어서 추가 배정함 — SPEC 개정 필요.**
- **계약값=코드 원칙**: ReportScore.total(property 합산)·21/30 판정·draftId 발급·clarification 판정은 전부 코드 소관, LLM 필드 금지.
- **A안(쓰기 구조적 차단)**: product draft 에이전트는 `list_my_products`+`calculate`만 바인딩. 쓰기 3종은 4단계 confirm-resume 코드 경로 전용.
- RecommendationSet **목록 순서 = "N번"** (§6.3 조회 계약, max 5건). ProposedChange.after는 str 통일(before는 실행 시점 I-9 확보).
- get_account_events(I-8 🔴)는 churn·abuse의 **보조 소스** — Error 나도 주 소스로 계속(프롬프트 명시).
- general 레인 기간 환산: `build_general_agent(today)` 빌드 시점 주입(잠정) — **요청마다 재빌드 필수**(이월 C1, 3단계에서 개선).
- `langchain` v1 + `langchain-anthropic` 설치됨. 임포트 경로: `langchain.agents.create_agent` / `langchain.agents.structured_output.ToolStrategy` / `langgraph.graph.state.CompiledStateGraph`.

## 2. 완료된 작업

### 1단계 — Tool 계층 (완료, opus 리뷰 완료)

- `app/services/spring_client.py`(조회 8종+쓰기 3종, 3s, MockTransport 주입, 싱글턴) · `app/agents/seller/tools.py`(도구 14종, READ_TOOLS/PRODUCT_TOOLS) · `calc.py`(순수 계산·normalize_period·safe_eval) · `context.py` · `app/core/auth.py`(brand_id) · `app/core/config.py` · `app/schemas/spring.py`(CamelModel 12종). 상세는 v1 핸드오프·DESIGN-SELLER-TOOLS-STAGE1 참조.

### 2단계 — 서브에이전트 계층 (완료, 2026-07-18 하루에 2-2b~2-9 전부)

- **2-2b** `schemas.py`: ReportScore(3축 0~10+feedback, SCORE_AXES 확장 지점, total=property) · ProductField(8종 Literal) · ProposedChange · ActionRecommendation(action_type 5종, product_id 필수) · RecommendationSet(순서=N번, ≤5).
- **2-3** `models.py`: SellerRole 7종·ROLE_TIER·init_seller_model(+lru_cache) + config temperature 2필드.
- **2-4~2-5** 분석 워커 5종(`prompts.py`+`workers.py`): WORKER_COMMON_RULES(코드 판정 번복 금지·degrade finding·기간=planner 주입) + 워커별 head. 전부 `_build_worker()` 경유, ToolStrategy(AnalysisFinding), 배정표 §3 코드화(테스트 parametrize).
- **2-6** general_agent: GENERAL_TOOLS 5종(쓰기 0), 자유 텍스트(3단계 SSE 대상), 3원칙(해석 금지·calculate 강제·미지원 안내), today 주입.
- **2-7** product_agent: DraftChange·DraftProposal(draftId 없음·clarification=불성립 신호, 잠정 확정) + PRODUCT_PROMPT(before는 조회값만·N번 발화 격리·delete=HIDDEN 가시화) + A안 바인딩.
- **2-8** `verifier.py`(DETERMINISTIC_CHECKS 레지스트리: D1 빈보고서/D2 수치정합/D3 degrade 정직성 — 순수 함수) + report(Sonnet, 도구 0)·judge(ToolStrategy(ReportScore))·recommend(ToolStrategy(RecommendationSet), 읽기 2종) 빌더 + 프롬프트 3종.
- **2-9** opus 적대 리뷰 1회: **critical 0**. calculate를 product에 추가 바인딩(유일 반영). 보류 R1~R7·이월 C1~C5는 `REVIEW-SELLER-STAGE2.md` §2 표 참조.
- 테스트 현황: test_seller_schemas(17)·test_seller_models(7)·test_seller_workers·test_seller_verifier(6)·test_config_seller. ⚠️ **2-8·2-9 반영 후 전체 스위트(uv run pytest -q) 로컬 결과 미확인 — 새 세션 첫 확인 사항.**

## 3. 에이전트별 도구 배정표 (2-9 개정 반영)

- sales_anomaly: get_sales_timeseries·get_order_events·get_product_change_logs·search_analysis_guide
- conversion: get_funnel·search_analysis_guide / behavior: get_behavior_events·get_funnel·search_analysis_guide
- churn: get_churn_cohort·get_order_events·get_product_change_logs·get_account_events(보조 🔴)·search_analysis_guide
- abuse: get_behavior_events·get_order_events·get_account_events(보조 🔴)·search_analysis_guide
- recommend: list_my_products·get_product_change_logs (읽기 전용)
- general: get_sales_timeseries·get_order_events·list_my_products·calculate·search_analysis_guide (쓰기 0)
- product(draft 생성): **list_my_products·calculate** (A안 — 쓰기 3종은 4단계 실행 레인 전용)
- supervisor·report·verifier(judge): 도구 없음

## 4. 남은 작업 (순서대로)

### 3단계 — 분석 파이프라인 + SSE 1차 배선

- **[다음] 3-1**: 파이프라인 입출력 계약 설계 — planner 프롬프트/스키마(AnalysisPlan: 워커 선택 + 정규화 기간 from/to, `calc.normalize_period` 사용), 워커 입력 메시지 포맷(기간 주입 규약), 진행 token 문구. 설계 표 제시 → 확정 → 구현.
- 3-2: analysis_planner 구현(캐시·이력 반영은 4단계 스텁).
- 3-3: gather 팬아웃 — asyncio.gather 로 워커 N종 병렬 실행 + degrade finding 수렴(전 워커 실패 시 사과 token 경로).
- 3-4: 검증 루프 — run_deterministic_checks → judge → 21/30(Settings) → ≤3회 재작성(feedback 주입), 미달 시 마지막 보고서 채택+로그. **이때 R1(D2 연도 오탐)·R2(D3 문자열 의존) 보류분 함께 처리 권장.**
- 3-5: recommend 호출 → compose_response(순수 함수) 조립.
- 3-6: 미들웨어(가드레일 scope→PII→출력 검사, 진행 token, ToolCallLimit) — 강의 06_Middleware 패턴.
- 3-7: general_agent를 `/seller/chat` SSE 1차 배선(astream→token/done, first-token 10s) — **C1: build_general_agent는 요청마다 재빌드**.
- 3단계 마감: opus 적대 리뷰 1회 + 요약본.

### 4단계 — 멀티에이전트·HITL·저장소

- supervisor 디스패치(RouteDecision), product HITL(draftId 발급→interrupt→confirm resume→쓰기 도구 호출, PostgresSaver, confirm 전송계층 🔴 스텁, **C4: op별 허용 필드 검증 — create는 image_url/status 불가**), 분석 이력(pg-profile)+§6.3 추천 적용 흐름, 시맨틱 캐시, RAG 활성화(seller_kb 인제스트+search_analysis_guide 교체), 일관성 회귀 테스트.

## 5. 미결(🔴)·보류·이월

- 팀 협의 대기: C-13(계산 경계표) · C-14(CRUD 스키마) · HITL confirm 전송 형식 · I-8 admin 소유 · 분석 기준서 문서 부재 · I-12 DB 논의.
- SPEC 개정 필요 누적: §8에 product 역할 추가 · §3 배정표 product에 calculate 추가 (+DESIGN 문서 "SPEC 개정 필요" 표 A~G).
- 리뷰 보류 R1~R7(사용자 결정: 기록만) · 이월 C1~C5(3·4단계에서 반영): **`docs/specs/REVIEW-SELLER-STAGE2.md` §2가 정본.**
