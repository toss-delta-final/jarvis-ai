# IMPL-PLAN-SELLER-001 — 판매자 멀티에이전트 bottom-to-top 구현 계획

> **버전**: v0.1.0 · **상태**: 계획 (각 단계 상세 설계는 해당 단계의 opus 설계 세션에서 산출)
> **상위 문서**: [SPEC-SELLER-001](specs/SPEC-SELLER-001.md) · api-spec §3.2/§4.4/§4.5 — 어긋나면 api-spec 우선.
> **참고 문서** (2026-07-17 확정 — 정본은 리포 SPEC): 『판매자_AI챗_멀티에이전트_설계서_3』·『상품관리_일반질문_에이전트_상세설계서_v2』(로컬 "판매자 명세서" 폴더). 이들은 별도 플랫폼 계약(API v2/DDL v2: S-3 수렴·interrupt 2종·`/ai/v1/chat/resume`)을 전제하므로 **HITL 범위·쓰기 API·이벤트 세트는 채택하지 않는다**. 다만 계약과 무관한 설계 자산은 각 단계 설계에 채택한다: supervisor 라우팅 경계 few-shot(쓰기 동사 최우선 규칙), `ToolCallLimitMiddleware` 한도(전역 + delete 전용), 동명 상품 되물음, calculator `safe_eval`(ast 화이트리스트), 기준 시점 고지 문구의 도구 반환 강제, 재고 delta→절대값 환산, 오류 문자열 자가수정 규칙.
> **범위**: 판매자(`app/agents/seller/`, `app/api/seller.py`, `spring_client` 판매자 함수군)만. **구매자·추천 코드는 수정하지 않는다.**
> **구현 규칙**: `01_LangGraph` 강의자료 코드 기반 + **`create_agent` 사용, `StateGraph` 수작업 조립 금지** + 코드마다 간단한 한국어 주석.

---

## 0. 전제와 원칙

### 0.1 두 종류의 "모델"을 구분한다

| 구분 | 모델 | 용도 |
|---|---|---|
| **작업 모델** (Claude Code 세션) | 설계 = **opus** / 구현 = **sonnet** / 검증 = **opus** | 각 단계를 만드는 개발 워크플로 |
| **런타임 모델** (자비스 서버 내부) | supervisor·워커·judge = **Haiku 4.5 (t=0)** / report·recommend = **Sonnet 5 (t=0.2)** | SPEC §8의 2-tier 배정, Settings 주입 |

### 0.2 SPEC 위상 → create_agent 재해석 (핵심 결정)

SPEC-SELLER-001 §2는 `StateGraph` + `Send` 팬아웃으로 그려져 있으나, 본 계획은 규칙("그래프 수작업 금지")에 따라 **같은 위상을 create_agent 계층 + 순수 파이썬 오케스트레이션으로 구현**한다. 강의자료 근거:

| SPEC 요소 | 구현 방식 | 강의 근거 |
|---|---|---|
| supervisor 구조화 출력 라우팅 | `create_agent(response_format=ToolStrategy(RouteDecision))` → 코드에서 분기 디스패치 | 05/03 Structured-Output, 09/06 Router |
| Send 팬아웃 워커 5종 | 워커 = 각각 독립 `create_agent`, 팬아웃 = `asyncio.gather`(코드 병렬) | 09/01·02 agent-as-tool, 11/09 부록 coordinator+task |
| 검증 루프(≤3회) | 파이썬 `for` 루프 (report 에이전트 ↔ verifier 에이전트 재호출) | 11/09 Three-Agent retry 라우팅의 코드 이식 |
| 가드레일·진행 token | `create_agent(middleware=[...])` (`@before_model`/`@wrap_model_call`) + `runtime.stream_writer` | 06/01 Middleware-Basics |
| 신원 주입(IDOR 방지) | `context_schema=SellerContext` + `ToolRuntime[SellerContext]` — 도구 시그니처에 신원 인자 없음 | 05/01·02 Runtime-Context/ToolRuntime |
| HITL draft→confirm | `create_agent(checkpointer=PostgresSaver)` + `interrupt` (create_agent가 내부적으로 LangGraph 런타임 공유) | 02/08 HITL, 06/02 HITL-V1 |

> 결정론적 파이프라인(planner→워커→report→verify→recommend→compose)을 LLM 라우팅이 아니라 **일반 async 함수**로 고정하는 것은 SPEC §10 일관성 장치와도 부합한다. "그래프 금지"는 StateGraph 수작업 금지이지, 코드 오케스트레이션 금지가 아니다.

### 0.3 Spring 미확정(🔴) 처리

- C-13/C-14 등 미확정 스키마는 **api-spec 초안 필드대로 구현 + httpx `MockTransport` 테스트**(신규 의존성 불필요). 계약 확정 시 spring_client 어댑터만 수정.
- 착수 전 **api-spec 사본 최신 버전 헤더·§8 개정 이력 대조** (lessons 2026-07-17 — 설계 문서 드리프트 재발 방지). 각 단계 opus 설계 세션의 첫 작업으로 고정.
- `langchain.agents.create_agent`는 **langchain v1 패키지 필요** — 현재 pyproject에 없음(`langchain-core`/`langgraph`만). 1단계에서 `uv add langchain` + context7로 버전 호환 확인.

---

## 1. 단계 분해 (bottom-to-top)

### 1단계 — Tool 계층 `feat/seller-tools`

LLM 없이 완결되는 최하층. **산출물**: `app/services/spring_client.py`(판매자 함수군 11종 신설·구스텁 대체), `app/agents/seller/tools.py`(@tool 래퍼), `app/agents/seller/calc.py`(고도화 계산 순수 함수), `app/core/config.py`(임계값 Settings 필드).

| 작업 | 내용 (SPEC §4·§5) |
|---|---|
| spring_client 함수군 | I-6~I-16 조회 7종 + I-9~I-12 CRUD 4종. `X-Internal-Token`, `{brandId}` path, **3s 타임아웃**. 구스텁 `get_seller_aggregates`·`get_product_detail` 대체·삭제 |
| @tool 래퍼 | `langchain.tools.@tool` + docstring(Args 규약). 응답은 문자열 요약, 오류는 `"Error: ..."` 반환(에이전트 자가수정 유도). **신원은 `ToolRuntime[SellerContext]`에서만** — 시그니처에 sellerId/brandId 금지 |
| 계산 모듈 | 이동평균·편차·이상판정·전환율 비교·`normalize_period`. 임계값 전부 Settings 주입(하드코딩 금지). @tool 노출은 계산기 성격만, 판정 로직은 워커 프롬프트 주입용 순수 함수 |
| RAG 도구 | `search_analysis_guide`는 기준서 문서 미존재(🔴)로 **인터페이스만 정의 + NotImplementedError 스텁**(api-spec § 주석) |

- **opus 설계 포인트**: 도구↔노드 배정표 확정(쓰기 3종 product 전용), 각 tool의 문자열 요약 포맷, 미확정 스키마의 초안 고정.
- **DoD**: `uv run pytest` — MockTransport로 정상/4xx/타임아웃/degrade 문자열, 신원 주입(시그니처 검사), 3s 설정 검증. `ruff` 통과.

### 2단계 — 서브에이전트 계층 `feat/seller-subagents`

전문가 1명씩을 `create_agent`로 만들어 **단독 invoke 테스트**. **산출물**: `app/agents/seller/schemas.py`(구조화 출력 Pydantic), `app/agents/seller/subagents/`(워커 5종 + report·verifier·recommend·general·product), `app/agents/seller/prompts.py`.

| 서브에이전트 | 구성 (SPEC §2·§8) |
|---|---|
| 분석 워커 5종 (sales_anomaly·conversion·behavior·churn·abuse) | Haiku t=0, 배정된 GET 도구만, `response_format=ToolStrategy(AnalysisFinding)`. 프롬프트에 "코드 판정 번복 금지" |
| report_agent / recommend_agent | Sonnet t=0.2. recommend는 `ToolStrategy(RecommendationSet)` + 읽기 도구만 |
| report_verifier | 결정론 검사(순수 함수) + Haiku judge `ToolStrategy(ReportScore)` — 루프는 3단계 소관 |
| general_agent | 읽기 도구 + 계산기. **쓰기 도구 배정 금지** |
| product_agent | 쓰기 3종 + `list_my_products`(before 확보). 이 단계에선 draft 페이로드 생성까지(interrupt 배선은 4단계) |

- 공통: `context_schema=SellerContext(seller_id, brand_id)`, 팩토리 함수(`build_xxx_agent(model, tools) -> agent`)로 테스트 주입 가능하게.
- **opus 설계 포인트**: AnalysisFinding/ReportScore/RecommendationSet/RouteDecision 스키마 확정(Literal·ge/le), 서브에이전트별 시스템 프롬프트 골격, abuse 워커 대체 소스(I-13/I-14) 조합.
- **DoD**: 서브에이전트별 스모크 — mock 도구 주입 → invoke → `structured_response` 타입·제약 검증. LLM 호출 테스트는 FakeChatModel/record 방식으로 CI 무과금.

### 3단계 — 에이전트 계층 `feat/seller-agents`

서브에이전트를 묶어 **혼자서 완결 답변하는 에이전트**를 만들고, 첫 E2E 배선. **산출물**: `app/agents/seller/analysis.py`(분석 파이프라인), `app/agents/seller/middleware.py`(가드레일·진행 token), `app/api/seller.py` 1차 연결.

| 작업 | 내용 |
|---|---|
| 분석 파이프라인 | async 함수: planner(Haiku, 이력 반영·기간 정규화·캐시 조회) → 워커 N종 `asyncio.gather` 팬아웃 → report → verifier `for` 루프(≤3, 미달 시 마지막 채택+로그) → recommend → compose(순수 함수). 워커 1종(sales_anomaly)부터 세로로 뚫고 5종 확장 |
| degrade | 워커 실패 → `severity=info` finding으로 계속 / 전부 실패 → 사과 token+done (SPEC §4·§7) |
| 미들웨어 | `@before_model` scope 가드 / PII 마스킹 / `@wrap_model_call` 재시도. 진행 상황은 `stream_writer` custom 이벤트 → SSE token 변환 |
| E2E 1차 | **general_agent만** `/seller/chat` `_stub_stream` 자리에 배선 — `astream(stream_mode=["messages","custom"])` → SSE `token`/`done` 변환, first-token 10s용 진행 token 선발행 (SPEC §13-2와 정렬: 도구 배선을 가장 얇은 에이전트로 검증) |

- **opus 설계 포인트**: 파이프라인 함수 시그니처·부분 실패 조합 표, astream→SSE 이벤트 변환 규약(멀티 모드 튜플 처리), 시맨틱 캐시 스킵 지점.
- **DoD**: 분석 파이프라인 유닛(워커 mock으로 팬아웃·루프·degrade 분기 전부) + `/seller/chat` 통합 테스트(스트림에서 token→done 순서, 403).

### 4단계 — 멀티에이전트 계층 `feat/seller-multiagent`

supervisor로 전체를 묶고 HITL·이력·캐시까지. **산출물**: `app/agents/seller/supervisor.py`, `app/agents/seller/hitl.py`, `app/agents/seller/history.py`, `/seller/chat` 최종 배선.

| 작업 | 내용 |
|---|---|
| supervisor | `create_agent`(Haiku t=0) + `ToolStrategy(RouteDecision)` 구조화 라우팅 → 코드 디스패치(analysis/product/general). 대안인 agent-as-tool(강의 Supervisor A)과의 선택은 opus 설계에서 확정 — SPEC 명시는 "구조화 출력 라우팅"이므로 기본값은 전자 |
| product HITL | draft{draftId} emit → `interrupt`(PostgresSaver checkpoint) → done / confirm{draftId} resume → I-10/11/12 → token→done. 안전장치 5종(draftId 바인딩·구조화 confirm만·멱등·TTL). **confirm 전송 계층은 🔴 확정 전 스텁** (SPEC §6.5) |
| 추천 적용 §6.3 | 저장된 구조화 recommendations 조회 → draft 변환 (대화 재해석 금지) |
| 분석 이력·캐시 | save_history(pg-profile PostgresStore `sellers/*`) + planner 최근 5건 주입 + `question_cache` 시맨틱 캐시 |
| 마감 | 일관성 회귀 테스트(같은 질문 10회 결론 일치율), CHANGELOG, README 반영 |

- **opus 설계 포인트**: supervisor 라우팅 방식 A/B 결정, 2-스트림 HITL의 checkpoint thread_id 설계, 이력 스키마.
- **DoD**: 라우팅 3분기 테스트(오분류 시에도 쓰기 불가 확인 — 도구 배정 격리), HITL 멱등·TTL 테스트, E2E 시나리오(통계 Q&A / 수정 draft / 일반질문).

---

## 2. 단계별 공통 워크플로 (설계 opus → 구현 sonnet → 검증 opus)

각 단계는 세 세션으로 나눠 진행하고, 산출물이 다음 세션의 입력이 된다.

1. **설계 (opus)** — 입력: 본 계획 + SPEC + api-spec 최신본 + 해당 강의 노트북. 산출: `docs/specs/` 하위 단계 설계 메모(스키마·시그니처·파일 구조·테스트 목록). **api-spec 버전 대조를 첫 작업으로** (lessons). context7로 langchain v1 API 시그니처 검증(추측 금지).
2. **구현 (sonnet)** — 설계 메모대로 TDD: 테스트 먼저 → 구현 → `uv run ruff check --fix && uv run ruff format` → `uv run pytest`. 코드마다 간단 한국어 주석 + api-spec §/SPEC § 참조. 강의 노트북 코드 스니펫을 정본 패턴으로 사용.
3. **검증 (opus)** — diff 리뷰: 계약 정합(§ 대조)·신원 주입 우회 여부·튜너블 하드코딩·구매자 코드 미접촉·테스트 커버리지. 발견 사항은 수정 반영 후 `docs/lessons.md` 기록.
4. **커밋** — topic 브랜치에서 Conventional Commits(`feat(seller): ...`), 이슈 연결(`Closes #N`), 단계 완료 시 PR + CHANGELOG `[Unreleased]`.

## 3. 리스크

| 리스크 | 대응 |
|---|---|
| langchain v1 API가 강의 시점과 다를 수 있음 | 설계 세션마다 context7 문서 조회로 확정 (추측 구현 금지) |
| C-13 계산 경계표 미확정 (I-6 `isAnomaly` 충돌) | AI-측 계산 모듈은 원시 시계열만 입력받게 설계 — Spring 판정 필드는 무시(참고치), 경계표 확정 시 어댑터만 수정 |
| 분석 기준서 부재 (🔴) | 워커는 기준서 없이도 동작하게(검색 실패 = degrade), 기준서·인제스트는 별도 산출물 트랙 |
| HITL confirm 형식 미확정 | interrupt→resume 배선까지 구현, 전송 계층 스텁 + § 주석 |
| SSE 수명주기(§2.9) 공통 인프라 미구현(0번 주제) | 본 트랙 범위 밖 — 스트림 1개 제한·취소는 공통 인프라 완성 시 합류, 판매자 쪽은 인터페이스만 침범 없이 유지 |

---

*개정 시 본 헤더 버전과 CHANGELOG를 함께 갱신한다.*
