# 화면 좌표 구조화 JSON 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한글 수사로 말한 추천 그리드 좌표를 decompose JSON의 `screenReference`로 추출하고, 검증된 서버 추천 순서와 `screen.columns`로 최종 상품 ID를 결정적으로 계산한다.

**Architecture:** screen이 주입된 decompose 시스템 프롬프트에만 nullable `screenReference={kind,row,column}` 계약을 추가한다. 파싱 결과는 `RouteDecision`에 보관하고 기존 화면 해소기에 전달하되, LLM은 좌표 의미만 제공하며 원문 숫자 파서·양보 가드·순서 검증·범위 검사 이후 서버가 index와 productId를 계산한다. 외부 FE·SSE·Spring·DB 계약은 바꾸지 않는다.

**Tech Stack:** Python 3.12, dataclasses, pytest/pytest-asyncio, Ruff, pre-commit, 기존 decompose JSON·buyer graph·screen reference 모듈.

## Global Constraints

- GitHub 이슈 #664와 `docs/superpowers/specs/2026-08-13-screen-reference-json-design.md`를 정본으로 삼는다.
- LLM은 `row`·`column`만 구조화하고 배열 index와 `productId`를 계산하지 않는다.
- `screenReference`는 screen 변형 프롬프트에만 추가하고 screen 없는 `_SYSTEM` 문자열은 바이트 동일하게 유지한다.
- 기존 숫자형 완전 좌표가 JSON과 충돌하면 원문 코드 파서가 우선한다.
- 상품명 직접 지목, 대화 맥락, 열 우선, 행 단독, 옵션 pending 안전 가드를 보존한다.
- 순서 미검증, columns 누락, malformed·범위 밖 좌표는 임의 상품 선택 없이 재질문한다.
- 추천 상품 ID를 FE `screen.products`로 되돌려 보내지 않는다.
- 외부 FE·SSE·Spring·DB 스키마, 새 설정과 새 의존성을 변경하지 않는다.
- 각 production 변경 전에 실패 테스트를 작성하고 기대한 RED를 확인한다.

---

### Task 1: decompose 내부 JSON 계약과 파싱

**Files:**
- Modify: `app/agents/buyer/recommendation/state.py`
- Modify: `app/agents/buyer/recommendation/decompose.py`
- Test: `tests/unit/test_decompose.py`

**Interfaces:**
- Produces: `ScreenReference(kind: Literal["grid"], row: int | None, column: int | None)`
- Produces: `RouteDecision.screen_reference: ScreenReference | None`
- Produces: `_parse_screen_reference(raw: object) -> ScreenReference | None`
- Consumes: 기존 `_as_int` 관대 정수 파싱 규약

- [ ] **Step 1: 프롬프트 계약 실패 테스트 작성**

screen 변형 프롬프트 JSON 예시에 `screenReference`가 있고, 행·열을 모두 말한 경우만 `grid`로 추출하며 `두 번째 옵션`·수량·순번 단독은 null이라는 규칙이 있는지 검증한다. 기존 `test_screen_absent_keeps_the_prompt_byte_identical`은 그대로 통과해야 한다.

- [ ] **Step 2: 파싱 실패 테스트 작성**

`screenReference={"kind":"grid","row":2,"column":2}`가 RouteDecision의 구조화 값으로 들어오는지 검증한다. 정수형 float와 숫자 문자열은 정수로, bool·0·음수·누락은 해당 축 `None`으로 남기고, dict가 아니거나 kind가 `grid`가 아니면 전체 `None`인지 검증한다.

- [ ] **Step 3: RED 확인**

```bash
uv run pytest tests/unit/test_decompose.py -k 'screen_reference_json' -q
```

예상: `RouteDecision.screen_reference`와 screen 변형 JSON 계약이 없어 실패한다.

- [ ] **Step 4: 최소 상태·파서·프롬프트 구현**

frozen slots dataclass를 추가하고 `_parse_screen_reference`가 좌표 주장을 보존하도록 구현한다. `_SYSTEM`은 변경하지 않고 `_SYSTEM_WITH_SCREEN`과 dedicated 변형을 만들 때 JSON 필드와 screen 규칙만 삽입한다.

- [ ] **Step 5: GREEN 확인**

Task 1 Step 3 명령을 다시 실행해 신규 테스트와 기존 screen prompt 테스트가 통과하는지 확인한다.

### Task 2: 화면 해소기에 JSON 좌표 연결

**Files:**
- Modify: `app/agents/buyer/screen_reference.py`
- Modify: `app/agents/buyer/graph.py`
- Test: `tests/unit/test_screen_context.py`

**Interfaces:**
- Consumes: `RouteDecision.screen_reference`
- Extends: `resolve_screen_reference(..., structured_reference: ScreenReference | None = None)`
- Preserves: `ScreenResolution(product_id, reason)`와 기존 숫자·이름·모호성 계약

- [ ] **Step 1: 한글 좌표 RED 테스트 작성**

추천 5건, `columns=3`, `ordinal_span=5`, 발화 `두번째 줄 두번째 상품 담아줘`, LLM `cart.productId`는 일부러 전체 2번째로 두고 `screenReference={kind:grid,row:2,column:2}`를 준다. Spring fake가 전체 5번째 상품 ID를 받는지 기대한다.

- [ ] **Step 2: 안전 실패 RED 테스트 작성**

다음 사례를 각각 검증한다.

- malformed 좌표는 담기 없이 위치 재질문
- 순서 미검증 추천 표면은 담기 없이 재질문
- columns가 없으면 담기 없이 재질문
- 범위 밖 좌표는 담기 없이 재질문
- 숫자 원문 좌표와 JSON이 충돌하면 숫자 원문이 우선
- 행 단독·열 우선·상품명 직접 지목은 JSON이 채우거나 덮어쓰지 않음

- [ ] **Step 3: RED 확인**

```bash
uv run pytest tests/unit/test_screen_context.py -k 'structured_grid_reference' -q
```

예상: `screenReference`가 화면 해소기에 전달되지 않아 LLM의 잘못된 `cart.productId`가 남거나 재질문 신호가 누락돼 실패한다.

- [ ] **Step 4: 최소 resolver·graph 배선 구현**

기존 양보 가드와 숫자 좌표·행 단독 검사 뒤, 숫자 순번 전에 구조화 좌표를 처리한다. `row`·`column`이 없거나 1 미만이면 `coordinate_invalid`, columns가 없으면 `coordinate_without_columns`, index가 범위 밖이면 `coordinate_out_of_range`를 반환한다. `screen_reference_attempted`는 기존 원문 탐지 또는 구조화 좌표 존재로 계산한다.

- [ ] **Step 5: GREEN 확인**

Task 2 Step 3 명령을 다시 실행하고 기존 screen context 전체를 실행한다.

### Task 3: 통합 경로와 문서 정합화

**Files:**
- Modify: `tests/integration/test_buyer_flow_e2e.py`
- Modify: `docs/api-spec.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: FakeLLM decompose `screenReference`
- Produces: 추천 ID 재전송 없이 한글 좌표가 Spring I-2 productId로 이어지는 회귀 증거

- [ ] **Step 1: 통합 RED 테스트 작성**

기존 추천 좌표 E2E 발화를 `두번째 줄 두번째 상품 담아줘`로 구성하고 FakeLLM이 `screenReference={kind:grid,row:2,column:2}`와 잘못된 `cart.productId`를 동시에 내게 한다. 최종 Spring 요청은 서버 추천 배열의 좌표 상품이어야 한다.

- [ ] **Step 2: RED 확인**

```bash
uv run pytest tests/integration/test_buyer_flow_e2e.py -k 'structured_grid_reference' -q
```

- [ ] **Step 3: API 사본과 CHANGELOG 갱신**

`docs/api-spec.md`를 v0.33.4로 올리고 내부 LLM JSON `screenReference`는 외부 와이어 필드가 아니며 row·column 추출 뒤 서버가 ID를 계산한다는 AI 동작을 기록한다. CHANGELOG에는 한글 수사 좌표가 최종 productId 직접 선택으로 빠지던 원인과 안전 경계를 기록한다.

- [ ] **Step 4: GREEN 확인**

통합 집중 테스트와 관련 unit 테스트를 함께 실행한다.

### Task 4: 검증, 커밋, Draft PR 갱신

**Files:**
- Verify: 변경된 production·test·documentation 파일

**Interfaces:**
- Produces: 재현 가능한 테스트 증거, Lore 커밋, 원격 브랜치와 Draft PR 갱신

- [ ] **Step 1: 집중 테스트**

```bash
uv run pytest tests/unit/test_decompose.py tests/unit/test_screen_context.py tests/integration/test_buyer_flow_e2e.py -q
```

- [ ] **Step 2: 전체 unit·Ruff**

```bash
uv run pytest tests/unit -q
uv run ruff check
uv run ruff format --check app/agents/buyer/recommendation/state.py app/agents/buyer/recommendation/decompose.py app/agents/buyer/screen_reference.py app/agents/buyer/graph.py tests/unit/test_decompose.py tests/unit/test_screen_context.py tests/integration/test_buyer_flow_e2e.py
```

- [ ] **Step 3: pre-commit과 diff 위생**

```bash
uv run pre-commit run --files app/agents/buyer/recommendation/state.py app/agents/buyer/recommendation/decompose.py app/agents/buyer/screen_reference.py app/agents/buyer/graph.py tests/unit/test_decompose.py tests/unit/test_screen_context.py tests/integration/test_buyer_flow_e2e.py docs/api-spec.md CHANGELOG.md docs/superpowers/specs/2026-08-13-screen-reference-json-design.md docs/superpowers/plans/2026-08-13-screen-reference-json.md
git diff --check
git status --short
```

- [ ] **Step 4: Chrome E2E**

branch AI 서버를 재시작하고 추천 5건·3열 화면에서 `두번째 줄 두번째 상품 담아줘`를 입력한다. 옵션 재질문 상품명이 실제 2행 2열 카드와 일치하고 옵션 선택 후 그 productId가 장바구니에 들어가는지 확인한다.

- [ ] **Step 5: Lore 커밋과 푸시**

```text
fix(cart): 한글 화면 좌표를 구조화해 오담기를 막는다

Constraint: LLM은 행·열만 추출하고 상품 ID는 검증된 서버 추천 순서에서 계산
Rejected: 발화 전체 한글 수사 정규화 | 옵션·수량·상품명 표현까지 변형할 위험
Confidence: high
Scope-risk: moderate
Directive: screenReference를 외부 와이어 필드나 productId 권위로 승격하지 말 것
Tested: focused decompose/screen/integration; tests/unit; Ruff; changed-file pre-commit; Chrome E2E
Not-tested: 배포 모델의 장기 분포 변화
```

푸시 후 Draft PR #666 본문에 `screenReference` 구조, 한글 수사 재현, 검증 결과를 추가하고 base/head/Draft 상태를 다시 확인한다.

## 완료 조건

- 격리 worktree `feat/662-cart-option-product-name`에서만 변경한다.
- 설계·계획 커밋과 구현 커밋을 분리한다.
- 신규 테스트가 구현 전 RED, 구현 후 GREEN을 보인다.
- 한글 좌표 JSON이 전체 5번째 상품으로 해소되고 잘못된 `cart.productId`를 덮어쓴다.
- malformed·미검증·columns 없음·범위 밖·충돌 사례가 임의 담기 없이 안전하게 처리된다.
- screen 없는 프롬프트와 외부 FE·SSE·Spring·DB 계약은 변하지 않는다.
- 전체 unit, Ruff, 변경 파일 pre-commit, Chrome E2E가 통과한다.
- Lore 커밋이 원격 브랜치와 Draft PR #666에 반영된다.
