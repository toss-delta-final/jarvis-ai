# Buyer Chat Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 같은 구매자 채팅방에서 자유대화 맥락을 비용 상한 안에 기억하고, 실제 캐시·압축 토큰 비용을 요청 단위로 측정한다.

**Architecture:** `conversation_turns`에서 같은 방의 최근 원문을 읽고 LangGraph `BaseStore`에는 방별 상황 요약과 압축 커서만 저장한다. 현재 응답에는 제한된 메모리를 조건부 주입하고, 밀려난 고가치 배치가 임계치를 넘을 때만 다음 턴용 요약을 동시 갱신한다. tracing은 명시적 call ID로 압축 usage를 귀속하고 캐시 읽기·쓰기 단가를 계산한다.

**Tech Stack:** Python 3.12, LangGraph BaseStore, LangChain usage metadata, Pydantic Settings, asyncio, pytest, Ruff, pre-commit.

## Global Constraints

- 공개 API·SSE·DB 스키마와 구매자 그래프의 LangGraph 구조를 바꾸지 않는다.
- 신규 dependency를 추가하지 않는다.
- 최근 원문은 최대 3쌍·1,000 추정 토큰, 상황 요약은 400 추정 토큰으로 제한한다.
- 1,200 추정 토큰 미만 또는 저가치 밀려난 배치는 압축하지 않는다.
- 옵션 답변 `pending_cart`와 action-only 턴에는 메모리를 주입하지 않는다.
- 메모리 조회·압축·저장 실패는 사용자 응답에 fail-open한다.
- 비용은 공급자 actual usage로 계산하고 프롬프트 원문은 로그에 기록하지 않는다.
- 모든 프로덕션 변경은 먼저 실패하는 테스트로 고정한다.

---

### Task 1: 설계 계약과 가격 표를 테스트로 고정

**Files:**
- Modify: `tests/unit/test_model_pricing.py`
- Modify: `tests/unit/test_observability.py`
- Modify: `tests/unit/test_llm_provider.py`
- Modify: `evals/model_eval/pricing_manifest.json`
- Modify: `evals/model_eval/pricing.py`
- Modify: `app/core/model_pricing.py`
- Modify: `app/core/config.py`
- Modify: `app/core/observability.py`
- Modify: `app/core/tracing.py`
- Modify: `app/core/llm.py`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Produces: `ModelCall.cached_input_tokens`, `ModelCall.cache_write_tokens`, 캐시 단가 설정, `bind_model_call_usage(call_id)`.
- Consumes: LangChain `AIMessage.usage_metadata`와 기존 `RequestTrace`/`RequestObservation`.

- [x] **Step 1: 가격·비용 RED 테스트 작성**

  Luna 입력/캐시 읽기/캐시 쓰기/출력 단가와 `uncached = input - cache_read - cache_write` 비용식을 기대하는 테스트를 작성한다. manifest와 런타임 기본표의 완전 일치를 함께 고정한다.

- [x] **Step 2: usage 정규화 RED 테스트 작성**

  `input_token_details.cache_read`, `cache_creation`, `cached_tokens`, `cache_write_tokens` 변형이 trace와 요청 로그의 숫자 필드로 전달되는지 검증한다.

- [x] **Step 3: RED 확인**

  Run: `uv run pytest tests/unit/test_model_pricing.py tests/unit/test_observability.py tests/unit/test_llm_provider.py -q`

  Expected: 새 필드·단가·비용식이 없어 assertion이 실패한다.

- [x] **Step 4: 최소 구현 후 GREEN 확인**

  가격 엔트리와 설정 dict를 확장하고 usage 정규화 helper를 추가한다. `RequestTrace`에서 explicit call ID를 전달하고 예약된 호출은 모델명 fallback에서 제외한다. 네 가격표 env를 production/dev 배포에 모두 전달한다.

  Run: `uv run pytest tests/unit/test_model_pricing.py tests/unit/test_observability.py tests/unit/test_llm_provider.py -q`

  Expected: all tests pass.

### Task 2: 메모리 선택·저장·압축 도메인을 TDD로 구현

**Files:**
- Create: `app/agents/buyer/memory.py`
- Create: `tests/unit/test_buyer_memory.py`
- Modify: `app/core/config.py`

**Interfaces:**
- Produces: `SituationMemory`, `BuyerMemoryContext`, `prepare_buyer_memory(...)`, `compact_buyer_memory(...)`, `estimate_tokens(text)`.
- Consumes: `Turn`, `BaseStore`, `LLMClient`, buyer-memory settings, 기존 PII redaction.

- [x] **Step 1: 선택 규칙 RED 테스트 작성**

  같은 thread만 포함, `PENDING` 제외, `FAILED`/`CANCELLED` 포함, 최신 3쌍, 쌍 보존 token cap, 새 thread 빈 결과를 각각 단일 행동 테스트로 작성한다.

- [x] **Step 2: 압축 규칙 RED 테스트 작성**

  저가치 제외, 미처리 토큰 누적 후 임계치에서만 trigger, 손상 저장값 fail-open, 성공 시 PII 제거·400 토큰 cap·cursor 전진, LLM 실패 시 이전 값 보존을 검증한다.

- [x] **Step 3: RED 확인**

  Run: `uv run pytest tests/unit/test_buyer_memory.py -q`

  Expected: `app.agents.buyer.memory`가 없어 collection error가 발생한다. 테스트 import 골격만 임시로 만들고 다시 실행해 기대 assertion 실패도 확인한다.

- [x] **Step 4: 최소 구현 후 GREEN 확인**

  결정론적 추정기와 immutable context를 만들고 BaseStore 레코드를 방별 namespace로 읽고 쓴다. 압축은 JSON 출력만 파싱하고 성공할 때만 mutation lock 안에서 최신 cursor를 재확인해 저장한다.

  Run: `uv run pytest tests/unit/test_buyer_memory.py -q`

  Expected: all tests pass.

### Task 3: decompose 조건부 메모리 주입을 TDD로 구현

**Files:**
- Modify: `app/agents/buyer/recommendation/decompose.py`
- Modify: `tests/unit/test_recommendation.py`

**Interfaces:**
- Consumes: `recent_conversation: list[dict[str, str]] | None`, `situation_memory: dict[str, object] | None`.
- Produces: 메모리가 있을 때만 JSON 데이터 블록과 우선순위 안내가 추가된 decompose 프롬프트.

- [x] **Step 1: 프롬프트 RED 테스트 작성**

  현재 메시지가 마지막이며 과거는 비신뢰 데이터라는 지시, 메모리 JSON 포함, `None`일 때 기존 프롬프트 바이트 동일성을 검증한다.

- [x] **Step 2: RED 확인**

  Run: `uv run pytest tests/unit/test_recommendation.py -k 'conversation_memory or identical_to_guest' -q`

  Expected: `decompose()`가 새 keyword argument를 받지 않아 실패한다.

- [x] **Step 3: 최소 구현 후 GREEN 확인**

  선택 인자를 추가하고 메모리가 있을 때만 system notice와 두 JSON line을 추가한다. 기존 출력 schema와 parsing은 바꾸지 않는다.

  Run: `uv run pytest tests/unit/test_recommendation.py -k 'conversation_memory or identical_to_guest' -q`

  Expected: all selected tests pass.

### Task 4: 구매자 스트림에 메모리 lifecycle을 TDD로 배선

**Files:**
- Modify: `app/agents/buyer/graph.py`
- Modify: `tests/integration/test_buyer_flow_e2e.py`
- Modify: `tests/unit/test_recommendation.py`

**Interfaces:**
- Consumes: observation의 conversation store, `prepare_buyer_memory`, `compact_buyer_memory`, `decompose` 선택 인자.
- Produces: 같은 방의 bounded context 주입과 응답과 동시 실행되는 다음 턴용 compaction task.

- [x] **Step 1: 그래프 RED 테스트 작성**

  두 번째 일반 대화 prompt에 같은 방 최근 원문이 있고 다른 방에는 없으며, `pending_cart`에서는 제외되고 압축 예외에도 DONE이 전송되는지 검증한다. 기존 action-only 테스트가 fast LLM 호출 0회를 유지하는지도 함께 실행한다.

- [x] **Step 2: RED 확인**

  Run: `uv run pytest tests/unit/test_recommendation.py tests/integration/test_buyer_flow_e2e.py -k 'memory or pending_cart or action_only' -q`

  Expected: 두 번째 턴 prompt에 메모리가 없어 새 assertion이 실패한다.

- [x] **Step 3: 최소 구현 후 GREEN 확인**

  `run_buyer_turn` 진입에서 방별 메모리를 준비하고 decompose 직전에 조건부 전달한다. 압축 task는 응답과 함께 시작하되 generator 종료 시 자연 완료를 기다리고 취소 시 취소한다.

  Run: `uv run pytest tests/unit/test_recommendation.py tests/integration/test_buyer_flow_e2e.py -k 'memory or pending_cart or action_only' -q`

  Expected: all selected tests pass.

### Task 5: 관측·문서·전체 회귀 검증

**Files:**
- Modify: `app/core/observability.py`
- Modify: `app/core/tracing.py`
- Modify: `app/agents/buyer/memory.py`
- Modify: `CHANGELOG.md`
- Modify: `docs/api-spec.md` only if internal observability field table is present and version-neutral.

**Interfaces:**
- Consumes: `BuyerMemoryContext` token counts와 explicit compaction call IDs.
- Produces: `recentHistoryTokens`, `situationMemoryTokens`, `evictedHistoryTokens`, compaction token/cost fields.

- [x] **Step 1: 메모리 관측 RED 테스트 작성**

  숫자·boolean 필드만 로그에 나오고 압축 호출 비용이 전체 비용의 부분집합이며 동시 일반 호출 usage와 섞이지 않는 테스트를 작성한다.

- [x] **Step 2: RED 확인 후 최소 구현**

  Run: `uv run pytest tests/unit/test_observability.py -k 'memory or cache' -q`

  Expected before implementation: 새 로그 필드가 없어 실패. 구현 후: all selected tests pass.

- [x] **Step 3: 변경 로그 작성**

  `CHANGELOG.md`의 Unreleased에 같은 방 bounded memory, fail-open compaction, cache-aware cost measurement를 한글로 기록한다.

- [x] **Step 4: 정적 검사와 전체 테스트**

  Run: `uv run ruff check app tests evals`

  Run: `uv run pytest -q -o faulthandler_timeout=60`

  Run: `uv run pre-commit run --from-ref origin/dev --to-ref HEAD`

  Result: ruff 통과, pytest `7163 passed, 229 deselected`(132.80초), 변경 범위 pre-commit 통과.
  저장소 전체 `--all-files`는 이 브랜치 밖의 기존 포맷 드리프트 65개를 변경하므로 기준점 대비
  변경 파일 검사로 범위를 고정한다.

- [x] **Step 5: 커밋과 푸시**

  각 커밋은 한글 Conventional Commit 제목과 Lore trailer를 사용하고 `Refs #653` 또는 마지막
  `Closes #653`를 포함한다. hook을 우회하지 않고 `git push -u origin NyongCho/feat-653-buyer-chat-memory`를 실행한다.
