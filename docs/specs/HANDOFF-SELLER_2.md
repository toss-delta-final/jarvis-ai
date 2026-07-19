# HANDOFF — 판매자 멀티에이전트 작업 인수인계 (2026-07-18 v3, 3단계 마감)

> ⚠️ **[대체됨 2026-07-19]** 이 문서는 3단계 마감 시점 기록이다 — 새 세션은
> [`HANDOFF-SELLER_3.md`](HANDOFF-SELLER_3.md)(재정렬 + 4-1 마감)를 먼저 읽는다.
> 재정렬 상세는 [`REALIGN-SELLER-20260719.md`](REALIGN-SELLER-20260719.md).
> 작업 저장소: `C:\Users\vssea\jarvis-ai` · 강의자료: `C:\Users\vssea\01_LangGraph\01_LangGraph`
> 3단계 상세·리뷰 기록: `docs/specs/REVIEW-SELLER-STAGE3.md` (M1~M3 반영·보류 S1~S6 표 포함)
> 이전 핸드오프: HANDOFF-SELLER_1(2단계 마감) — 규칙·1~2단계 요약은 그 문서가 정본, 여기선 증분만.

## 1. 범위·정본·핵심 규칙 (전부 사용자 확정 — v2 에서 유지)

- **판매자 파트만** 구현 — 구매자·추천 코드 절대 수정 금지.
- bottom-to-top: **tool(1단계✓) → 서브에이전트(2단계✓) → 파이프라인(3단계✓) → 멀티에이전트(4단계 ← 다음)**.
- **`create_agent` 사용, StateGraph 수작업 조립 금지** — 오케스트레이션은 순수 파이썬(asyncio.gather·for 루프). 3단계가 이 방식으로 완성됨(`orchestrator.py`).
- 계약 정본: `docs/api-spec.md` v0.14.0 사본 + `docs/specs/SPEC-SELLER-001.md`. SPEC 개정 필요 누적은 REVIEW-STAGE3 §4.
- 강의자료 참조: 4단계는 `09_Multi_Agent`(02-Supervisor·06-Router)·`07_Memory`·`08_RAG` + HITL 은 `06_Middleware/02-Human-In-The-Loop-V1`(HumanInTheLoopMiddleware·Command·checkpointer 패턴 — 3-6 에서 임포트 경로 실검증 완료).
- 전체 워크플로우 다이어그램: `docs/specs/WORKFLOW-SELLER-STAGE3.png`(생성 스크립트는 세션 임시 — 갱신 시 재생성).

### 진행 방식 (사용자 강력 요구 — 반드시 유지)

- **한 턴 = 한 소작업.** 설계 요지 제시 → 사용자 확정 → 구현 → 상세 변경 보고(파일·판단 근거 포함, 사용자가 "더 상세하게" 요구했음).
- **결정 필요/애매한 부분은 소크라테스식 질문**으로 사용자에게 묻는다(2026-07-18 지시). 위임받으면 판단하되 근거·변경점을 기록.
- **git 커밋 금지**(사용자 직접). pytest·ruff 는 **사용자 로컬 실행** 결과로 확인 — 단, 이번 세션 환경은 샌드박스 pip 가 가능해 langchain v1 포함 사전 검증이 됐다(정식 판정은 로컬).
- opus 적대 리뷰는 **단계 마감 시 1회만**(3단계분 완료). 각 단계 완료 시 요약 보고 → 사용자 검증 → 다음 단계 여부 질문.

## 2. 3단계 완료 내역 (2026-07-18~19, 3-1~3-7 + 마감)

- **3-1** `pipeline.py` 신규 — AnalysisPlan(스키마, 중복 dedupe 관용)·resolve_plan(불성립=ValueError 통일→되묻기)·ResolvedPlan·워커 입력 포맷(`[분석 기간] from= to=` + 질문)·진행 token 문구(AnalysisType 커버 자기검증). calc.normalize_period 에 명시 범위 "YYYY-MM-DD~YYYY-MM-DD" 추가. **"이번 달" 등 미지원 표현 = 되묻기(사용자 확정)**.
- **3-2** PLANNER_PROMPT + build_analysis_planner(Haiku t=0·도구 0·ToolStrategy(AnalysisPlan)). 연도 없는 날짜도 되묻기. 이력 주입(§9.1)은 4단계에 **입력 메시지로**(프롬프트 불변).
- **3-3** orchestrator.py 신규 — WORKER_BUILDERS 레지스트리·Emit 콜백(`Callable[[str], Awaitable[None]]`)·run_workers(gather, 요청마다 빌드, degrade 수렴 2종 문구 "응답 시간 초과"/"내부 오류", 전부 예외→AllWorkersFailedError). Settings `seller_worker_timeout_s=60.0` 신설.
- **3-4** write_verified_report — report(Sonnet)→D1~D3(코드)→judge(항상 실행)→코드 판정(결정론 0건 AND ≥21/30)→**사유+feedback 합산** 재작성 ≤3회(사용자 확정). VerifiedReport(report·passed·attempts·last_score). report/judge/재작성 입력 포맷은 pipeline.py 순수 함수(format_findings_block·format_report_input·format_rewrite_input·format_judge_input). verifier R1(연도 마스킹 `_DATE_MASK_RES`)·R2(`_is_degrade_finding` 구조 판정) 해소.
- **3-5** run_recommend(실패=빈 추천 degrade, C2 처리)·compose_response(번호=목록 순서=§6.3 N번, `_APPLY_GUIDE` 안내)·run_analysis_pipeline → **PipelineResult(kind: report/clarification/apology/refused, text, verified, recommendations)**.
- **3-6** middleware.py 신규 — check_scope(SCOPE_BLOCK_RULES 3규칙군, 규칙 상수=모듈 단일 출처)·ScopeGuardMiddleware(before_agent end 점프, **마지막 human 기준**)·PII 3종(email 내장 + 휴대폰/주민번호 커스텀 detector, apply_to_input)·mask_output(SSE 쓰기 직전용)·ToolCallLimit(Settings 8). **구조화 출력 레인은 end 점프 금지 → 코드 경로(check_scope)** 가 핵심 판단.
- **3-7** app/api/seller.py 재작성 — general_agent astream(stream_mode="messages")→token/done/error. scope 코드 선차단(LLM 0회)·**C1 이행(요청마다 재빌드)**·tool_use 블록 미노출·청크 마스킹·brandId 클레임 누락 시 사전 403(판단: §2.3 a [확정] 클레임이라 결손=403).
- **마감** opus 적대 리뷰 1회: **critical 0**, major 3 즉시 반영 — M1(워커에 PII 미들웨어 추가), M2(SSE 빌드를 try 안으로), M3(최근 N≤0 ValueError). 주석 정정 2건. 상세·보류 S1~S6: REVIEW-STAGE3 §2. CHANGELOG `[Unreleased]` 에 판매자 3단계 항목 추가. 워크플로우 PNG 배지 3단계 완료로 갱신.
- 테스트: 판매자 유닛 **139종** — pipeline 13 · orchestrator 19 · middleware 8 · api 6 (전부 신규) + calc 15 · schemas 20 · workers 42 · verifier 10 · models 6 (+config_seller 3 별도). ⚠️ 마감 반영분(M1~M3+회귀 5종) 이후 **로컬 `uv run pytest -q`·`ruff` 미확인 — 새 세션 첫 확인 사항.**

### 기술 확정 사항 (3단계 증분 — 1~2단계 누적은 HANDOFF v2 §1 유지)

- **Q1(사용자 위임 결정, 변경 가능)**: D3 degrade 판정 = `severity=="info" and evidence==[]` 구조 조합. 오탐(정상 '이상 없음' finding 이 evidence 를 비우면 보고서에 한계 문구 요구) 감수, 미탐(은폐) 방지 우선. **변경점: `verifier._is_degrade_finding` 한 곳.**
- **Q2(사용자 위임 결정, 변경 가능)**: 검증 루프 중 LLM 장애 — 기존 보고서 있으면 미달 채택(passed=False)+warning 로그, **1차 작성 실패만 예외 전파**. judge 장애 = 현재 보고서 미검증 채택(last_score=None). **변경점: `write_verified_report` 의 except 2분기.**
- run_analysis_pipeline **예외 전파는 2경우**: planner 장애 + 1차 report 실패. SSE 호출부는 둘 다 사과/error 매핑 필수.
- 되묻기·사과·거절은 예외가 아니라 PipelineResult 반환값 — 호출부는 kind 무관 text→token→done 단일 계약.
- 진행 token = 미들웨어가 아니라 **파이프라인 emit 콜백**(SSE 가 큐 put 주입, 테스트는 리스트 수집).
- general 레인 "이번 달"=당월 1일~오늘(레인 전용 정의) vs 분석 레인=되묻기 — GENERAL_PROMPT 에 명시(의도된 비대칭).
- 워커 degrade 문구는 원인 2종만("응답 시간 초과"/"내부 오류") — 예외 원문 미노출(정보 노출·D2 오염 방지).
- 미들웨어 배정표(scope/PII/ToolCallLimit × 에이전트)는 REVIEW-STAGE3 §1 표가 정본.

## 3. 남은 작업 — 4단계 (멀티에이전트·HITL·저장소)

HANDOFF v2 §4 의 4단계 목록 유지 + 3단계에서 생긴 접속 조건 반영:

- **4-1(제안)**: supervisor 디스패치 — RouteDecision(스키마 완성돼 있음) 라우팅 + `/seller/chat` 에 3분기 배선(analysis→run_analysis_pipeline, general→기존 스트림, product→4-2). first-token 진행 token, PipelineResult kind→SSE 매핑, 예외 2경우 사과/error. 강의 09_Multi_Agent/02-Supervisor·06-Router 참조.
- 4-2: product HITL — draftId 발급(uuid)→interrupt→confirm resume→쓰기 3종. PostgresSaver, confirm 전송계층 🔴 스텁. **C4: op별 허용 필드 검증(create 는 image_url/status 불가)** + product 레인 check_scope 코드 경로 + DraftProposal.clarification→되묻기 token.
- 4-3: 분석 이력 save_history(pg-profile, `("sellers", {sellerId}, "analysis_history")`) + §6.3 추천 적용 흐름(저장된 recommendations[N-1]→draft) + planner 이력 주입(입력 메시지).
- 4-4: 시맨틱 캐시(question_cache) · RAG 활성화(분석 기준서 작성 🔴 선행 → seller_kb 인제스트 → search_analysis_guide 교체) · 일관성 회귀 테스트(같은 질문 10회).
- 개선 후보(선택): mask_output 청크 경계 버퍼링 · S4 타임아웃 예산 분리 · S1(D2 `.0` 꼬리) — REVIEW-STAGE3 §2 보류 표.

## 4. 미결(🔴)·보류·이월

- 팀 협의 대기(변동 없음): C-13 계산 경계표 · C-14 CRUD 스키마 · HITL confirm 전송 형식 · I-8 admin 소유 · 분석 기준서 문서 부재 · I-12 DB 논의.
- SPEC 개정 필요 누적: REVIEW-STAGE3 §4 (스테이지2 2건 + 3단계 신규 3건).
- 보류·이월 현황: **R1·R2·C1·C2 해소 완료** / R3~R7·C3~C5 유지 / 신규 보류 S1~S6 — **`REVIEW-SELLER-STAGE3.md` §2~§3 이 정본.**
