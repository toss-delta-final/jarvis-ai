# HANDOFF — 판매자 멀티에이전트 작업 인수인계 (2026-07-19 v4, 재정렬 + 4-1 마감)

> 새 세션 시작 시 이 문서를 먼저 읽는다. 다음 작업은 **4-2 (product HITL 실행)** (아래 §5).
> 작업 저장소: `C:\Users\vssea\jarvis-ai` · 강의자료: `C:\Users\vssea\01_LangGraph\01_LangGraph`
> 이전 핸드오프: HANDOFF-SELLER_2(3단계 마감) — 규칙·1~3단계 요약은 그 문서가 정본, 여기선 증분만.
> 재정렬 상세 기록: [`REALIGN-SELLER-20260719.md`](REALIGN-SELLER-20260719.md) — BE·FE 확정 8건(F1~F8)·사용자 결정 4건(D1~D4)·체크리스트 완료 표가 정본.

## 1. 왜 재정렬이 있었나 (2026-07-19)

BE·FE 팀이 07/17 에 API 명세·ERD 를 확정 변경했다(「API·ERD 변경 정리 07/17」·「시스템 설계도 — JARVIS 아키텍처」·`schema.sql` v2·「API 명세서」CSV). 판매자 파트에 직접 영향이 있는 확정:

| # | 확정 | 요지 |
|---|---|---|
| F1 | **FE→AI 직접 호출 폐기 → Spring 패스스루** | nginx 는 fastapi 미노출. 판매자 챗 = FE → Spring(CH-6 세션 발급) → Spring 이 **S-4 `{AI_SERVER}/seller/chat`** 호출 + SSE 를 가공 없이 FE 로 통과. AI행 호출은 CH-2·S-4·I-20 뿐(전부 Spring발) |
| F2 | **productId = 숫자**(DB BIGINT) | 구 "전 구간 string" 규칙 폐기(판매자 파트만 선전환 — D2) |
| F3 | **HITL confirm = `{"action":"confirm","draftId":"..."}`** | 🔴 confirm 전송 형식 해소 |
| F4 | **행동 데이터 원천 = behavior_events**(8종, user_event 폐기) + I-13 명세 재작성 | groupBy 3형(product/eventType/date)·`{success,data}` 봉투·상품 연계 4종만 집계 |
| F5 | **로그 3종 기록 규칙 확정**(order_status_logs·product_change_logs·account_event_logs) | 구매확정·클레임 신청·ORDERED 미기록, 배치 전이=주문 단위 1행, 주문 재고 차감 미기록, 품절 신호=STOCK→0 |
| F6 | 교환 제거(상태 9종)·배송비 0원·재고 stock_quantity 도입(시드 100)·전량 취소 시 주문 CANCELLED 승격 | I-14 상태 어휘·I-6 매출 정의 확정 |
| F7 | **S-5(FE 직접 수정) 병존 확정** | 챗봇 draft 와 동시 수정 경합 → 4-2 stale 검증 설계 입력 |
| F8 | 세션 ID = UUID(S-abc 형식 폐기) | 세션 검증에 형식 가정 금지 |

사용자 결정 4건(D1~D4): D1 인증=서비스 토큰+메아리 신원 / D2 productId int 는 판매자 파트만 / D3 C4 유지+**image_url NULL 허용 가정(⚠️ BE 미확인)** / D4 순서=문서→코드→4-1.

## 2. 재정렬 코드 전환 (②-1~②-5, 전부 완료)

| 소작업 | 내용 | 파일 |
|---|---|---|
| ②-1 인증 전환 | `require_seller_internal` 신설 — `X-Internal-Token`(settings.service_token, 상수시간 비교) 결손/불일치 **401** + Spring 주입 신원 헤더 **`X-Seller-Id`/`X-Brand-Id`(🔴 AI측 제안 — BE 확정 시 이 함수 한 곳만 수정)** 결손 **400**. 미설정=dev 스킵+경고 1회. 구 `require_seller`(RS256 티켓)·JWKS 코드는 미사용 보존. `service_token` 을 "Spring→AI 인바운드 공용"으로 재정의 | `app/api/deps.py`·`app/api/seller.py`·`app/core/config.py` |
| ②-2 productId int | seller schemas(`ActionRecommendation`=int 필수·`DraftProposal`=int\|None, **create=null**(구 "" 폐기))·spring.py 판매자 모델 4종(`SellerProductRow`·CRUD Result 3종)·tools/client 시그니처·PRODUCT_PROMPT. `SellerContext` 신원은 str 유지(헤더 유래) | `schemas.py`·`spring.py`·`tools.py`·`spring_client.py`·`prompts.py` |
| ②-3 I-13 재대조 | `BehaviorEventsResult` 재작성(groupBy 3형: rows/counts/series + `BehaviorProductRow`, 구 events[] 폐기). `get_events` 에 eventType 복수·productId·groupBy. **`_request` 에 `{success,data}` 봉투 언랩 신설**(봉투 없으면 통과 — ⚠️ I-6 등 타 I-* 도 같은 봉투인지 실측 확인 필요). 도구 출력에 "purchaseComplete 는 이벤트 기준, 권위는 I-6/I-14" 상시 문구 | `spring.py`·`spring_client.py`·`tools.py` |
| ②-4 해석 규칙 | 도구 출력 상시 주의 문구 2종(`_ORDER_LOG_RULES_NOTE`/`_PRODUCT_LOG_RULES_NOTE`) + I-14 docstring 상태 어휘 확정. 프롬프트 4종(sales_anomaly·churn·abuse·behavior)에 [해석 주의] 주입 — behavior 는 group_by 사용법으로 절차 재작성. verifier/calc 는 무의존 확인·무변경 | `tools.py`·`prompts.py` |
| ②-5 테스트 | 202→214 통과로 증분 반영(신규: test_seller_auth_internal 10종·I-13 9종·해석 규칙 3종. test_health 는 400/스트림 2종으로 교체) | `tests/unit/` |

## 3. 4-1 supervisor 디스패치 (a·b·c 완료)

- **4-1a**: `SUPERVISOR_PROMPT` + `build_supervisor()`(Haiku t=0·도구 0·ToolStrategy(RouteDecision)·PII만) + `orchestrator.route_question()` — 코드가 최종 판정: **장애(예외·타임아웃·비정형)=general 폴백**+warning(사용자 결정), **confidence<`seller_route_confidence_min`(0.6)=analysis 보수 재지정**(원분류 analysis 는 유지, 원 confidence·사유 보존). 타임아웃 `seller_route_timeout_s`(10s) 신설. `pipeline.parse_confirm_message()` — F3 형식 코드 선판정(발화≠동의를 구조로 보장, 4-2 입구).
- **4-1b**: `app/api/seller.py` `_seller_stream` 재작성 — 입구 ①confirm 선판정(지금은 "준비 중" 안내 token — 실행은 4-2) ②scope 선차단 ③라우팅 → 3분기:
  - **analysis** = `_analysis_stream`: `run_analysis_pipeline` 의 emit 을 asyncio.Queue 로 SSE 중계, `add_done_callback` sentinel 로 행 방지. PipelineResult 는 kind 무관 text→token→done. **예외 2경우(planner 장애·1차 report 실패)만** 사과 token + error(LLM_TIMEOUT/INTERNAL) — REVIEW-STAGE3 §5-2 매핑 이행.
  - **product** = `_product_stream`: draft 생성까지(사용자 결정 — 실행은 4-2). DraftProposal → SSE `draft`{**draftId(uuid 발급만 — checkpoint 바인딩 없음)**·op·productId(int|null)·changes·summary}, clarification=되묻기 token.
  - **general** = 기존 astream 스트림(무변경).
- **4-1c**: 실 LLM 라우팅 스모크 — `scripts/smoke_seller_chat.py`(대표 발화 8종, Spring 불요) + `docs/specs/SMOKE-SELLER-41.md`(기대 분기·통과 조건 5·기록 표). **⚠️ 스모크 실행은 사용자 로컬 미완 — §6 첫 확인 사항.**

테스트: 판매자 유닛 **237종** 통과(샌드박스 `uv run pytest tests/unit`, ruff clean). 신규 테스트 파일: `test_seller_auth_internal.py`(10)·`test_seller_router.py`(14) + test_seller_api(+9)·test_seller_tools(+10)·test_seller_spring_client(+4)·test_health(2 교체).

## 4. 미결(🔴)·가정·잔여 리스크

1. **신원 헤더명** `X-Seller-Id`/`X-Brand-Id` — AI측 제안. BE 확정 시 `require_seller_internal` 한 곳 수정. CH-6 "SELLER 티켓"의 실체(Spring 세션용인지)도 함께 확인.
2. **image_url NULL 가정(D3)** — DB 는 NOT NULL. C4(create 시 image_url 불가)와 상충 — BE 가 기본값/NULL 허용으로 처리한다고 가정하고 작성함. 4-2 create 실행 전 확정 필요.
3. **`{success,data}` 봉투** — I-13 실측으로 도입, SellerSpringClient 전 응답에 적용(하위 호환 언랩). I-6/I-14 등 실측 명세 확인 시 재검증.
4. 팀 공지 잔여: CLAUDE.md(productId string 규칙·인증 레인·v0.7.0 표기 stale), api-spec 정본 v0.15 개정(F1·F2·I-21/CH-5/CH-6/E-1/S-5·교환 제거·세션 UUID), 아키텍처 문서 정정(AI Postgres ×2 누락·"문 6개" 불일치). 구매자 파트 전달분은 REALIGN §5.
5. 보류 승계: R3~R7·C3~C5·S1~S6(REVIEW-STAGE3) + mask_output 청크 경계 버퍼링.

## 5. 남은 작업 (4-2 ~ 4-4)

- **4-2 product HITL 실행**: PostgresSaver(checkpointer) + draftId↔checkpoint 바인딩 → `interrupt` → confirm resume(입구 ① `parse_confirm_message` 가 이미 판정) → 쓰기 3종(I-10/11/12) 실행. **C4**(create 는 image_url/status 불가 — D3 가정) + **S-5 병존 대응: confirm 시점 I-9 재조회로 draft before 재검증, 불일치 시 되묻기(stale draft 차단)** + draft TTL(`seller_draft_ttl_minutes`=10). 강의 참조: `06_Middleware/02-Human-In-The-Loop-V1`.
- **4-3 분석 이력**: save_history(pg-profile, `("sellers", {sellerId}, "analysis_history")`) + "N번 적용해줘" → recommendations[N-1](productId int) → DraftProposal 변환 → 4-2 재사용 + planner 이력 주입(입력 메시지, 프롬프트 불변).
- **4-4**: 시맨틱 캐시(question_cache) · RAG(분석 기준서 🔴 팀 문서 선행 — 그 전까지 search_analysis_guide 스텁 유지) · 같은 질문 10회 일관성 회귀.
- 단계 마감 시 opus 적대 리뷰 1회(규칙 유지 — 4단계 전체 완료 시).

## 6. 새 세션 첫 확인 사항

1. `uv run pytest -q`·`uv run ruff check` 로컬 통과 확인(237 기준) — **재정렬~4-1 커밋이 아직이면 논리 단위로 분리 커밋**: ①인증 전환 ②productId int ③I-13+해석 규칙 ④4-1a 라우터 ⑤4-1b 배선 ⑥문서(REALIGN·SMOKE·HANDOFF).
2. **4-1c 스모크 실행**(`SMOKE-SELLER-41.md` 절차, ANTHROPIC_API_KEY 필요) — 오분류 발화가 있으면 SUPERVISOR_PROMPT 보강 후 4-2 착수.
3. 4-2 착수 전 BE 회신 확인: 신원 헤더명·image_url 처리(§4-1·2). 미회신이어도 가정(🔴 표기)으로 진행 가능.
4. CHANGELOG `[Unreleased]` 에 재정렬·4-1 항목 추가는 PR 병합 시점에(기록 규칙).
