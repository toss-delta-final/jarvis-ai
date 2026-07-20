# REVIEW-SELLER-STAGE3 — 3단계(분석 파이프라인·미들웨어·SSE 1차 배선) 마감 요약 · opus 적대 리뷰 기록

> 작성: 2026-07-18 (3단계 마감) · 대상: 3-1~3-7 산출물 · 리뷰: opus 적대 리뷰 1회(진행 방식 규칙 준수)
> 다음 단계: 4단계(supervisor 디스패치 + product HITL + 저장소 + RAG + 시맨틱 캐시)
> 이전 기록: [`REVIEW-SELLER-STAGE2.md`](REVIEW-SELLER-STAGE2.md)

## 1. 3단계 산출물 요약

| 파일 | 내용 |
|---|---|
| `app/agents/seller/pipeline.py` (신규) | 파이프라인 순수 계약 — ResolvedPlan·resolve_plan(불성립=ValueError 통일)·워커 입력 포맷·report/judge/recommend 입력 포맷·compose_response(순서=N번 표면)·진행 token 문구(로드 시 자기검증) |
| `app/agents/seller/orchestrator.py` (신규) | LLM 실행 계층 — run_workers(asyncio.gather 팬아웃, degrade 수렴, AllWorkersFailedError)·write_verified_report(D1~D3+judge 합산 feedback ≤3회)·run_recommend(실패=빈 추천)·run_analysis_pipeline(PipelineResult: report/clarification/apology/refused) |
| `app/agents/seller/middleware.py` (신규) | 가드레일 — check_scope/ScopeGuardMiddleware(규칙 상수+end 점프)·PII 3종(email·휴대폰·주민번호, 입력 정제)·mask_output(SSE 쓰기 직전용)·ToolCallLimit(Settings) |
| `app/api/seller.py` (재작성) | general_agent astream→SSE token/done/error 1차 배선 — scope 선차단(LLM 0회)·C1(요청마다 재빌드)·tool_use 블록 미노출·청크 마스킹·brandId 클레임 사전 403 |
| `app/agents/seller/workers.py` | build_analysis_planner 추가 + 빌더별 미들웨어 배선(아래 표) |
| `app/agents/seller/prompts.py` | PLANNER_PROMPT(정규 어휘 4종·날짜 산수 금지·미지원=되묻기) |
| `app/agents/seller/verifier.py` | R1(연도 계열 날짜 마스킹)·R2(D3 구조 판정 `_is_degrade_finding`) 해소 |
| `app/agents/seller/calc.py` | normalize_period 명시 범위("YYYY-MM-DD~YYYY-MM-DD") + N≤0 방어(M3) |
| `app/core/config.py` | `seller_worker_timeout_s: float = 60.0` 추가 |
| 테스트 | test_seller_pipeline(13)·test_seller_orchestrator(16)·test_seller_middleware(8)·test_seller_api(6) 신규 + calc/schemas/workers/verifier 증분 — 판매자 유닛 140종(로컬 `uv run pytest` 기준 사용자 확인) |

### 미들웨어 배정표 (3-6 확정, M1 반영)

| 에이전트 | scope | PII(입력) | ToolCallLimit | 비고 |
|---|---|---|---|---|
| general | 미들웨어(end 점프) + SSE 코드 선차단 | ✓ | ✓ | 유일 자유 텍스트 대면 |
| planner | 코드 경로(run_analysis_pipeline 입구) | ✓ | — | 구조화 출력 — end 점프 금지 |
| 워커 5종 | (입구에서 처리됨) | ✓ (M1) | ✓ | 원문 question 이 직접 들어옴 |
| product | 4단계 배선 시 코드 경로 | ✓ | ✓ | 구조화 출력 |
| recommend | — (내부 입력만) | — | ✓ | |
| report·judge | — | — | — | 도구 0·내부 입력 |

### 사용자 위임 결정 (Q1·Q2 — 2026-07-18, 추후 변경 가능하도록 단일 변경점 유지)

- **Q1** D3 degrade 판정 = `severity=="info" and evidence==[]` 구조 조합 — 오탐(정상 '이상 없음'을 degrade 로) 감수, 미탐(은폐 통과) 방지. 변경점: `verifier._is_degrade_finding` 한 곳.
- **Q2** 검증 루프 중 LLM 장애 = 기존 보고서가 있으면 미달 채택(passed=False)+로그, **1차 작성 실패만 예외 전파**(사과/error 경로). 변경점: `orchestrator.write_verified_report` 의 두 except 분기.

## 2. opus 적대 리뷰 결과 (2026-07-18, 1회)

**하드 제약 위반(critical) 0건** — IDOR 신원 은닉·쓰기 3종 격리·create_agent(오케스트레이션은 순수 파이썬)·계약값=코드·Settings 단일 출처·SSE 4종/stop 단일/camelCase·degrade 규약·모델 배정 전부 준수. langchain v1 API(before_agent jump_to·PIIMiddleware·ToolCallLimitMiddleware·astream messages·structured_response·context=)는 설치 패키지 소스 대조로 확인.

### 즉시 반영 (major 3 + 주석 정정 2 — 2026-07-18 반영 완료, 회귀 테스트 포함)

| # | 내용 | 반영 |
|---|---|---|
| M1 | 분석 워커가 PII 미정제 원문 question 수신(planner 미들웨어는 planner 호출에만 적용) — §10-⑥ 우회 + 주석이 안전을 오도 | `_build_worker` 에 `seller_pii_middlewares()` 추가 + 주석 정정 |
| M2 | `_general_stream` 의 에이전트 빌드가 try 밖 — 빌드 실패 시 error 봉투 없이 스트림 파손 | 빌드·context 생성을 try 안으로 + 회귀 테스트 |
| M3 | normalize_period "최근 0일"(N≤0) 역전 범위 무음 통과 | N≤0 ValueError(되묻기 경로) + 회귀 테스트 |
| — | orchestrator docstring "planner 장애만 전파" 부정확(1차 report 실패도 전파, Q2) | docstring 정정 + 전파 회귀 테스트 |
| — | GENERAL_PROMPT "(normalize_period 와 동일 정의)" 가 "이번 달"에서 거짓 | 문구 정정("이번 달"은 general 레인 전용, 분석 레인은 되묻기) |

### 보류 (S1~S6 — 기록만, 필요 시 착수. R4~R7 방식 승계)

| # | 심각도 | 내용 | 제안됐던 방향 |
|---|---|---|---|
| S1 | minor | verifier D2 — "180,000.0원" `.0` 꼬리·"1,2,3번" 병합 오탐 여전(스테이지2 R4 미해결 승계) | 천단위 그룹 정규식 |
| S2 | minor | D3 공개 검사가 보고서측 "확보 실패"/"데이터 한계" 리터럴 의존(탐지는 구조화됐으나 공개측은 문자열) — REPORT_PROMPT 가 강제해 실무 위험 낮음 | 공개측도 구조화(예: 보고서 섹션 계약) |
| S3 | minor | check_scope "고객 주소" 등 부분 문자열이 정상 집계 질문("고객 주소지 분포")을 과잉 차단할 여지 | 규칙 튜닝(현 MVP 무해) |
| S4 | minor | 누적 타임아웃 예산 — planner+워커+report×3+judge×3+recommend 가 60s 를 공유, 최악 누적이 90s 목표 초과 | report/judge 별도 짧은 예산(§7 은 목표치라 위반 아님) |
| S5 | minor | ScopeGuardMiddleware 가 general SSE 경로에선 사문(코드 선차단이 먼저) — belt-and-suspenders 로 유지 | 인지만 |
| S6 | minor | R3(CONVERSION_PROMPT 두 기간 vs 단일 기간 주입) 드리프트가 파이프라인 가동으로 활성화됨 — 스테이지2 보류 확정 유지 | planner 두 기간 계약(보류) |

### 알려진 한계 (4단계 개선 후보)

- **mask_output 청크 단위 적용** — 시크릿 패턴이 SSE 청크 경계에 걸치면 놓칠 수 있음(경계 버퍼링 후보).
- 워커 PII 미들웨어 부착 여부를 구조적으로 검증하는 테스트 부재(빌더 내부 구성 introspection 불가) — 코드 리뷰 의존.

## 3. 이월 처리 현황 (스테이지2 표 대비)

| # | 상태 | 처리 |
|---|---|---|
| R1 (D2 연도 오탐) | **해소** | 연도 계열 날짜 마스킹(`_DATE_MASK_RES`) — 날짜 아닌 4자리 환각은 여전히 탐지(테스트) |
| R2 (D3 문자열 의존) | **해소(탐지측)** | `_is_degrade_finding` 구조 판정. 공개측 잔여는 S2 |
| C1 (general 재빌드) | **해소** | SSE 배선이 요청마다 `build_general_agent(today=...)` 재빌드 |
| C2 (RecommendationSet 초과 → degrade) | **해소** | `run_recommend` 가 모든 실패(ValidationError 포함)를 빈 추천으로 수렴 |
| R3~R7 | 유지 | 스테이지2 표 그대로(R3 은 S6 로 활성화 기록) |
| C3~C5 | 유지 | 3단계 비대상(C3 judge 방어 유지 확인·C4/C5 는 4단계) |

## 4. SPEC 개정 필요 누적 (스테이지2 §3 + 3단계 추가)

- (기존) §8 에 product 역할 추가 · §3 배정표 product 에 calculate 추가.
- (신규) §2 진행 token — 미들웨어가 아니라 파이프라인 emit 콜백으로 구현됨을 명시.
- (신규) §10-④ 기간 정규화 — 정규 어휘 4종("지난달"/"최근 N일"/"어제"/명시 범위)과 "미지원 표현은 되묻기" 확정 반영. "이번 달"은 general 레인 전용 정의임을 명시.
- (신규) §7 — PipelineResult 4종(kind) 과 예외 전파 2경우(planner·1차 report)의 SSE 매핑.
- 기존 미결(🔴) 유지: C-13 계산 경계표 · C-14 CRUD 스키마 · HITL confirm 전송 형식 · I-8 admin 소유 · 분석 기준서 문서 · I-12 DB 논의.

## 5. 4단계 착수 시 첫 확인 사항

1. `uv run pytest -q` 전체 통과 상태에서 시작(M1~M3 반영분 포함).
2. supervisor 배선 시 분석 레인 SSE 는 `run_analysis_pipeline` 의 **예외 2경우**(planner·1차 report)를 사과/error 로 매핑할 것(본 문서 §2 주석 정정 참조).
3. product 레인 배선 시 **check_scope 코드 경로** 추가(§1 배정표) + C4(op별 허용 필드 검증).
4. 분석 SSE 배선 시 mask_output 적용 지점 = 스트림 쓰기 직전(청크 경계 한계 인지).
