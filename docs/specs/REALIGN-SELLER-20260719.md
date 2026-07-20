# REALIGN-SELLER — BE·FE 확정(07/17)에 따른 판매자 파트 재정렬 (2026-07-19)

> BE·FE 팀이 확정한 변경(「API·ERD 변경 정리 07/17」·「시스템 설계도 — JARVIS 아키텍처」·`schema.sql` v2)과
> 사용자 결정 4건(2026-07-19)을 판매자 파트 기준으로 기록한다.
> **이 문서는 4단계 착수 전 선행 작업의 정본**이다 — HANDOFF-SELLER_2 §3(4단계 목록)보다 먼저 처리한다.
> 범위: 판매자 파트만. 구매자·추천·프로필 코드/문서는 건드리지 않는다(해당 항목은 §5 팀 공지로 이관).

## 1. BE·FE 확정 사항 (2026-07-17, 팀 확정 — 재논의 불필요)

| # | 확정 | 판매자 영향 |
|---|---|---|
| F1 | **FE→AI 직접 호출 없음.** nginx 는 fastapi 미노출. 판매자 챗 = CH-6(Spring 이 세션 발급) → **S-4 `{AI_SERVER}/seller/chat` 을 Spring 이 호출 + SSE 패스스루** | seller.py 인증 전면 전환 (§2 D1) |
| F2 | **id 는 숫자**(I-19·I-21 예시, DB BIGINT) | productId string→int 전환 (§2 D2) |
| F3 | **HITL confirm = `{action:"confirm", draftId}`** (07/17 질문3 ✅) | 🔴 "confirm 전송 형식" 해소 — 4-2 설계 가능 |
| F4 | **행동 데이터 원천 = behavior_events**(8종 화이트리스트, user_event 폐기) + I-13 명세 재작성 | BehaviorEventsResult 재대조 (§3-①) |
| F5 | **로그 3종 신설·기록 규칙 확정**(order_status_logs·product_change_logs·account_event_logs) | I-14/I-15 해석 규칙 반영 (§3-②③) |
| F6 | **교환 제거**(상태 11→9), **배송비 0원**, **재고 stock_quantity 도입**(시드 100, CHECK ≥0), **주문 취소 승격**(전량 취소 시 orders.status=CANCELLED) | I-14 상태 어휘·I-6 매출 정의·I-10 stockQuantity(이미 정합) |
| F7 | **S-5(FE 직접 수정) 병행 확정** — 챗봇 수정과 공존 | 4-2 draft stale 검증 설계 입력 (§4) |
| F8 | **세션 ID = UUID**(S-abc123 형식 폐기, 07/17 질문1 ✅) | 판매자 세션 검증에 형식 가정 금지 |

## 2. 사용자 결정 (2026-07-19 — AskUserQuestion 확정)

| # | 결정 | 내용 |
|---|---|---|
| D1 | **인증 = 서비스 토큰 + 메아리 신원** | seller.py 를 `X-Internal-Token` 검증 + Spring 이 주입하는 sellerId/brandId(검증된 메아리 값) 수신으로 전환. RS256/JWKS 티켓 검증·brandId 클레임 403 은 판매자 레인에서 미사용 전환(코드 제거 아님 — 구매자 레인 소유). "신원은 본문에서 받지 않는다" 규칙은 판매자 레인에서 "신뢰 주체 = Spring 서비스 토큰, 신원 = Spring 주입 값"으로 재정의 |
| D2 | **productId int 전환 = 판매자 파트만** | seller schemas(DraftProposal·ActionRecommendation·ProposedChange 등)·draft SSE 페이로드·SellerSpringClient CRUD 경로/응답·판매자 테스트만 int 전환. 구매자 스키마·CLAUDE.md 규칙 개정은 팀 공지로 이관(§5) |
| D3 | **C4 유지 + image_url NULL 허용 가정** | create 시 image_url/status 불가(C4 원안 유지). DB 의 `image_url NOT NULL` 과 상충하나 **BE 가 NULL 허용(또는 기본 이미지)으로 처리한다고 가정**하고 작성한다. ⚠️ 가정 — schema.sql 개정 또는 I-10 기본값 정책을 BE 확인 목록에 유지(§5) |
| D4 | **진행 순서 = 문서 → 코드 → 4-1** | ① 본 문서 + HANDOFF 갱신 → ② 코드 전환(인증→productId→I-13) → ③ 4-1 supervisor 착수. 커밋은 사용자 직접 |

## 3. 코드 전환 체크리스트 (순서 ② — 착수 전 이 표 기준)

| 순번 | 대상 | 변경 | 근거 |
|---|---|---|---|
| ②-1 | `app/api/seller.py`·`app/api/deps.py`(판매자 의존성) | `require_seller`(RS256 티켓) → **서비스 토큰 검증 + Spring 주입 신원 파싱**. brandId 사전 403 → 신원 결손 시 400/401 재정의. 스트림 취소·409 규약은 유지 | D1·F1 — **✅ 완료 2026-07-19** (`require_seller_internal` 신설: 토큰 결손/불일치 401·신원 헤더 `X-Seller-Id`/`X-Brand-Id`(🔴 제안) 결손 400·dev 스킵+경고 1회. `service_token` 재정의. 구 `require_seller` 미사용 보존. 테스트 `test_seller_auth_internal.py` 10종 + `test_health.py` 2종 갱신 — 유닛 202 통과) |
| ②-2 | `app/agents/seller/schemas.py`·`context.py`·SSE draft 페이로드 | `product_id: str` → `int` 일괄(DraftProposal·ActionRecommendation·ProposedChange·SellerContext 경유값). compose_response 의 N번 안내 무영향 확인 | D2·F2 — **✅ 완료 2026-07-19** (ActionRecommendation=int 필수·DraftProposal=int\|None(create=null, 구 "" 폐기)·spring.py 판매자 모델 4종(SellerProductRow·CRUD Result 3종)·tools/spring_client 시그니처·PRODUCT_PROMPT 문구. SellerContext 는 신원(str) 유지. 테스트 7파일 숫자 id 전환 — 유닛 202 통과) |
| ②-3 | `app/services/spring_client.py` SellerSpringClient + `app/schemas/spring.py` 판매자 모델 | CRUD 경로 `{product_id}` int·응답 모델(ProductCreateResult 등) int. **I-13 응답 스키마를 재작성된 명세와 재대조**(BehaviorEventsResult 필드 최소집합 🔴 해소) | D2·F4 — **✅ 완료 2026-07-19** (CRUD int 는 ②-2 에서 처리. I-13: BehaviorEventsResult 재작성 — groupBy 3형(product rows/eventType counts/date series)+BehaviorProductRow, 구 events[] 폐기. get_events 에 eventType 복수·productId·groupBy 추가. **`{success,data}` 봉투 언랩을 `_request` 에 신설**(봉투 없는 응답 하위 호환). get_behavior_events 도구 인자 확장+3형 요약+I-6/I-14 권위 주의 문구. 테스트 +9종 — 유닛 211 통과) |
| ②-4 | `app/agents/seller/prompts.py`·`verifier.py`·`calc.py` 해석 가정 | I-14: ORDERED·`*_REQUESTED`·CONFIRMED **미기록**·배치 전이는 주문 단위 1행 → "구매확정" 분석 불가·전이 건수≠아이템 수 명시. I-15: 주문 재고 차감 미기록·품절 신호=`STOCK new_value=0`. I-6: 매출=Σ(price×qty), 배송비 항 없음 | F5·F6 — **✅ 완료 2026-07-19** (도구 출력 상시 주의 문구 2종(`_ORDER_LOG_RULES_NOTE`/`_PRODUCT_LOG_RULES_NOTE`)+docstring 상태 어휘 확정. 프롬프트 4종(sales_anomaly·churn·abuse·behavior)에 [해석 주의] 주입 — BEHAVIOR 는 ②-3 group_by 사용법으로 재작성. verifier/calc 는 상태 어휘 무의존 확인 — 무변경. 테스트 +3종(문구 2·프롬프트 회귀 1) — 유닛 214 통과) |
| ②-5 | 테스트 | 판매자 유닛 전체(140종±) 갱신 + 인증 전환 회귀. `uv run pytest`·`ruff` 통과 후 보고 | 규칙 — **✅ ②-1~②-4 에 증분 반영 완료** (202→214, 샌드박스 기준. **로컬 `uv run pytest`·`ruff` 최종 확인 = 사용자**) |

- 전환 중 **구매자 파일(chat.py·auth.py 공용부·구매자 스키마) 수정 금지** — 공용 파일은 판매자 분기만 추가.
- ②-1 착수 전 BE 에 확인: Spring 주입 신원의 **전달 위치(헤더 vs 본문)와 필드명** — 미확정 시 헤더 `X-Seller-Id`/`X-Brand-Id` 제안으로 진행하고 🔴 표기.

## 4. 4단계 설계 입력 (코드 전환 후)

- **4-1**: S-4 경로 = `{AI_SERVER}/seller/chat` 별도 경로 유지(API 명세서 CSV 확정). 인증은 ②-1 결과 위에 배선.
  - **소작업 분할(2026-07-19 확정)**: 4-1a supervisor 빌더+라우팅 함수(코드 판정 순서: scope 선차단 → confirm 코드 선판정 자리 → supervisor, confidence 임계 = Settings `seller_route_confidence_min`) → 4-1b 3분기 SSE 배선(analysis=파이프라인 emit 큐, 예외 2경우만 사과/error) → 4-1c 라우팅 회귀 테스트.
  - **4-1a ✅ 완료 2026-07-19**: SUPERVISOR_PROMPT·`build_supervisor`(Haiku t=0·ToolStrategy(RouteDecision)·PII만)·`orchestrator.route_question`(장애=general 폴백+warning, confidence<`seller_route_confidence_min`(0.6)=analysis 재지정·원분류 analysis 는 유지, 타임아웃 `seller_route_timeout_s`(10s) 신설)·`pipeline.parse_confirm_message`(F3 형식 코드 선판정, 4-2 입구용). 테스트 `test_seller_router.py` 14종 — 유닛 228 통과.
  - **4-1b ✅ 완료 2026-07-19**: `_seller_stream` 입구 ①confirm 선판정(안내 token placeholder) ②scope 선차단 ③라우팅 → 3분기. analysis=`_analysis_stream`(emit 큐 중계+sentinel, kind 무관 text→token→done, 예외 2경우=사과 token+error(LLM_TIMEOUT/INTERNAL)) / product=`_product_stream`(DraftProposal→SSE `draft`{draftId(발급만)·op·productId(int)·changes·summary}, clarification=되묻기 token, 실행은 4-2) / general=기존 astream. 테스트 test_seller_api +9종 — 유닛 237 통과.
  - **4-1c ✅ 완료 2026-07-19**: 코드 회귀는 4-1a/b 테스트로 커버. 실 LLM 라우팅 정확도는 수동 스모크 — `scripts/smoke_seller_chat.py`(대표 발화 8종 일괄, Spring 불요) + `docs/specs/SMOKE-SELLER-41.md`(판정 기준·통과 조건 5·실행 기록 표). **스모크 실행은 사용자 로컬**(ANTHROPIC_API_KEY 필요) — 결과 기록 후 4-1 마감.
  - **사용자 결정(2026-07-19)**: supervisor **장애** 시 general 폴백 + warning 로그(confidence 미달=analysis 보수 라우팅과 별개). 4-2 전 product 레인 = **draft 생성까지**(product_agent→DraftProposal→SSE draft 이벤트, clarification 은 되묻기 token, 실행은 4-2).
- **4-2**: confirm 형식 F3 확정. **S-5 병행(F7) → confirm 시점 재검증** 설계: I-7/I-9 재조회로 draft before 와 대조, 불일치 시 되묻기(stale draft 방지). PostgresSaver 유지(AI 내부 저장 — 배포 문서에 AI Postgres 반영은 §5).
- **4-3/4-4**: 변동 없음 (HANDOFF-SELLER_2 §3).

## 5. 팀 공지·BE 확인 잔여 (판매자 범위 밖 — 처리하지 않고 전달만)

1. **CLAUDE.md 개정 필요**: "productId 전 구간 string" → 숫자(F2), "인증 레인" 서술 → 전 구간 서비스 토큰, api-spec v0.7.0 표기 → 최신. (팀 파일이라 판매자 단독 수정 안 함)
2. **api-spec 정본 개정 필요(v0.15 후보)**: F1 호출 구조·F2 id 타입·I-21/CH-5/CH-6/E-1/S-5 신설·교환 제거·세션 UUID. 정본은 기획 repo — 개정 후 사본 동기화.
3. **BE 확인**: (a) Spring 주입 신원 전달 위치·필드명(§3 ②-1) (b) image_url NULL 허용 여부(D3 가정) (c) CH-6 입장권 페이로드(티켓 여부).
4. 아키텍처 문서 정정 요청: "AI 문 6개(I-1~I-6)" 실제 표면과 불일치, AI Postgres ×2(pg-catalog/pg-profile) 배포 다이어그램 누락, "맥락 인메모리" ↔ PostgresSaver·분석 이력 계획.
5. 구매자 파트 전달: I-19 경로·본문 재작성, I-21/CH-5 로 §4.2/§4.3 대체(listId Spring 생성·TTL 10분, reason 은 CH-5 echo), I-17 커서=(updated_at,id) 제안 정합.
