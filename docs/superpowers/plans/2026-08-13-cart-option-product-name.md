# 장바구니 옵션 재질문 상품명 표시 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 장바구니가 옵션을 다시 물을 때 실제 대상 상품명을 정제·40자 축약해 표시하고, 이름이 없으면 기존 문구와 동작을 그대로 유지한다.

**Architecture:** 구매자 그래프가 추천 상태와 현재 screen.products를 productId → name 표시용 맵으로 합쳐 stream_cart_add에 넘긴다. 장바구니 그래프는 pending 전환을 포함한 기존 로직으로 최종 product_id를 확정한 다음 그 이름을 찾아, 하나의 공용 머리말 helper로 다섯 옵션 재질문 앞에만 붙인다. 이름은 저장하거나 Spring 요청에 포함하지 않는다.

**Tech Stack:** Python 3.12, Pydantic, pytest/pytest-asyncio, Ruff, pre-commit, SSE token.data.text.

## 전역 제약

- GitHub 이슈 #662와 설계 문서를 정본으로 삼는다.
- 상품명은 _strip_unsafe 후 최대 40자이며 초과할 때만 …를 붙인다.
- 상품 머리말과 빈 줄 뒤에 기존 옵션 재질문을 그대로 둔다.
- 추천 이름보다 같은 ID의 현재 screen.products 이름을 우선한다.
- 최종 product_id로 이름을 고르기 전에는 특정 이름을 확정하지 않는다.
- 이름이 없거나 정제 후 비면 기존 prompt를 그대로 반환한다.
- PendingAdd, Spring/FE/DB 스키마와 새 설정을 변경하지 않는다.
- 품절 degrade, 성공·실패 action, 조회·삭제·수량·찜 출력은 변경하지 않는다.
- 새 의존성과 광범위한 리팩터링을 금지한다.
- 테스트를 먼저 수정하고 기대한 RED를 관찰한 뒤 production code를 편집한다.

---

### Task 1: 상품 머리말 계약을 실패 테스트로 고정

**Files:**
- Modify: tests/unit/test_cart.py

**Interfaces:**
- Consumes: cart_graph 모듈, stream_cart_add, _run_add, _collect, CartOptionRequired, CartOptionInvalid, OptionHint
- Produces: _option_product_heading의 정제·축약 계약과 다섯 재질문 경로의 정확 문자열 회귀

- [ ] **Step 1: 머리말 단위 테스트 추가**

production helper를 모듈 속성으로 호출하는 테스트를 추가한다. 직접 import하지 않아 pytest collection은 유지하고 RED에서 missing attribute를 관찰한다.

검증할 입력:

- 짧은 이름은 그대로 표시
- 40자 문자열은 말줄임표 없음
- 41자 문자열은 앞 40자와 …
- 제어 문자·줄바꿈·zero-width가 섞인 이름은 _strip_unsafe 후 축약
- 정제 후 빈 이름과 None은 빈 문자열

- [ ] **Step 2: 기본 REQUIRED와 이름 없음 폴백 테스트 추가**

기본 CartOptionRequired 테스트에 product_names로 41자 이름을 넘기고 다음 모양의 리터럴을 기대한다.

~~~text
**상품:** <앞 40자>…

옵션을 선택해 주세요:
1. **블랙 / M**
2. **화이트 / M**
어떤 걸로 담을까요?
~~~

별도 테스트에서는 product_names 생략과 다른 ID 이름 모두 기존 리터럴과 완전히 같은지 확인한다.

- [ ] **Step 3: 나머지 네 재질문 경로 테스트 추가**

조건 좁힘, 색상 미충족, I-1 힌트 폴백, pending 뒤 CartOptionInvalid 재질문에 target 상품 머리말을 검증한다. INVALID는 cart.product_id가 비어도 pending product_id로 올바른 이름을 고르는 사례를 포함한다.

- [ ] **Step 4: 집중 테스트 RED 관찰**

~~~bash
/home/uuser/inte-final/jarvis-ai/.venv/bin/pytest \
  tests/unit/test_cart.py -k 'option_product or product_name_reask' -q
~~~

예상: 새 helper가 없어 AttributeError가 발생하고, 나머지는 새 product_names 인자가 없어 TypeError 또는 기존 상품명 없는 문자열 차이로 실패한다. collection 오류나 무관한 실패면 구현으로 넘어가지 않는다.

---

### Task 2: 최소 공용 helper와 장바구니 경로 구현

**Files:**
- Modify: app/agents/buyer/cart/graph.py

**Interfaces:**
- Produces: _option_product_heading(product_name)
- Produces: _prepend_option_product_heading(prompt, product_name)
- Extends: _options_prompt와 _cart_option_required_text의 선택적 product_name
- Extends: stream_cart_add의 선택적 product_names 맵

- [ ] **Step 1: 정제·축약 helper 구현**

_option_label 인접 위치에 최대 길이 40 상수와 helper를 추가한다. _strip_unsafe를 먼저 적용하고 40자를 초과할 때만 앞 40자와 …를 사용한다.

- [ ] **Step 2: 기존 prompt 조립에 선택적 이름 연결**

_options_prompt에 keyword-only product_name을 추가하고 반환 직전에 prepend helper를 적용한다. _cart_option_required_text에도 같은 인자를 추가한다.

I-1 힌트 폴백, 조건 좁힘, 색상 미충족, 기본 REQUIRED에만 이름을 연결한다. 옵션 목록과 힌트가 모두 없는 품절 degrade 문장은 변경하지 않는다.

- [ ] **Step 3: 최종 상품 ID로 표시 이름 선택**

stream_cart_add에 product_names 맵을 추가한다. unresolved 검사를 통과해 product_id가 확정된 뒤 해당 ID의 이름을 고르고 REQUIRED와 INVALID prompt에만 전달한다.

- [ ] **Step 4: 집중 테스트 GREEN**

Task 1과 같은 명령으로 모든 신규 테스트 통과를 확인한다.

---

### Task 3: 추천·screen 이름 배선 TDD

**Files:**
- Modify: tests/unit/test_cart.py
- Modify: tests/unit/test_screen_context.py
- Modify: app/agents/buyer/graph.py

**Interfaces:**
- Consumes: last_reco, screen_products, stream_cart_add
- Produces: 현재 턴 표시 이름 맵과 cart graph keyword argument

- [ ] **Step 1: 추천 이름 배선 실패 테스트**

production run_buyer_turn에 추천 상태와 CartOptionRequired를 구성한다. token이 target 이름을 40자로 축약해 표시하는지 리터럴로 검증한다.

- [ ] **Step 2: screen 전용 이름과 우선순위 실패 테스트**

직전 추천에는 없고 screen.products에만 있는 상품의 이름이 표시되는지 검증한다. 같은 ID에 추천과 화면 이름이 다르면 현재 screen 이름만 출력되는지도 검증한다.

- [ ] **Step 3: 배선 테스트 RED 관찰**

~~~bash
/home/uuser/inte-final/jarvis-ai/.venv/bin/pytest \
  tests/unit/test_cart.py tests/unit/test_screen_context.py \
  -k 'option_product_name_from_recommendation or option_product_name_from_screen' -q
~~~

예상: buyer 호출부가 product_names를 넘기지 않아 머리말이 없어 실패한다.

- [ ] **Step 4: 이름 맵 생성과 전달 구현**

screen_products가 계산된 뒤 추천 이름을 먼저, 현재 screen 이름을 나중에 넣는 맵을 만든다. 이 맵은 cart_add 위임에만 넘기고 상품 해소, allowed 또는 surface 판단에는 사용하지 않는다.

- [ ] **Step 5: 배선 테스트 GREEN**

Task 3 Step 3과 같은 명령을 다시 실행해 통과를 확인한다.

---

### Task 4: 계약 문서와 변경 이력 갱신

**Files:**
- Modify: docs/api-spec.md
- Modify: CHANGELOG.md

- [ ] **Step 1: API 문서에 token 표시 동작 기록**

장바구니 옵션 재질문 설명에, 이름을 확인할 수 있을 때 옵션 목록 앞에 정제·40자 축약한 상품 머리말을 붙이고 확인할 수 없으면 기존 문구로 degrade 한다고 기록한다. SSE 스키마와 Spring 계약이 불변임을 명시한다.

- [ ] **Step 2: CHANGELOG에 이슈 #662 추가**

사용자 문제, 결정론적 ID별 이름 선택, 40자 제한, 이름 없음 호환성, 변경하지 않은 계약을 기록한다.

---

### Task 5: 전체 검증과 구현 커밋

**Files:**
- Verify only: 위 implementation, test, documentation files

- [ ] **Step 1: 집중 회귀 실행**

~~~bash
/home/uuser/inte-final/jarvis-ai/.venv/bin/pytest \
  tests/unit/test_cart.py tests/unit/test_screen_context.py -q
~~~

- [ ] **Step 2: buyer 전체 단위 테스트와 Ruff 실행**

~~~bash
/home/uuser/inte-final/jarvis-ai/.venv/bin/pytest tests/unit -q
/home/uuser/inte-final/jarvis-ai/.venv/bin/ruff check \
  app/agents/buyer/cart/graph.py app/agents/buyer/graph.py \
  tests/unit/test_cart.py tests/unit/test_screen_context.py
/home/uuser/inte-final/jarvis-ai/.venv/bin/ruff format --check \
  app/agents/buyer/cart/graph.py app/agents/buyer/graph.py \
  tests/unit/test_cart.py tests/unit/test_screen_context.py
~~~

- [ ] **Step 3: pre-commit과 diff 위생 검증**

~~~bash
/home/uuser/inte-final/jarvis-ai/.venv/bin/pre-commit run --all-files
git diff --check
git status --short
~~~

pre-commit이 파일을 수정하면 변경을 검토하고 같은 검증을 다시 실행한다.

- [ ] **Step 4: Chrome E2E**

별도 브랜치의 AI 서버를 로컬 FE·BE·DB와 연결한다. 새 추천 턴으로 이름 캐시를 만들고 옵션이 여러 개인 추천 상품 담기를 요청한다. 브라우저에서 상품명 표시, 40자 축약, 실제 target ID와 이름 일치, 기존 옵션 목록 유지, 다음 옵션 답변의 정상 담기를 확인한다.

- [ ] **Step 5: 구현 커밋**

검증된 코드·테스트·API 문서·CHANGELOG만 stage한다. Conventional Commit과 Lore trailer를 함께 만족하는 커밋 메시지를 사용한다.

~~~text
feat(cart): 옵션 재질문에서 대상 상품을 식별하게 한다

Constraint: 상품명은 정제 후 40자로 제한하고 이름이 없으면 기존 문구를 유지
Rejected: PendingAdd에 상품명 저장 | 표시 전용 정보가 상태 권위로 굳어짐
Confidence: high
Scope-risk: narrow
Directive: 상품 이름 맵은 표시 외 상품 선택이나 허용 판단에 사용하지 말 것
Tested: focused cart/screen tests; tests/unit; Ruff; pre-commit --all-files; Chrome E2E
Not-tested: 배포 환경의 다중 인스턴스 캐시 유지성
~~~

## 완료 조건

- GitHub 이슈 #662와 분리 브랜치에서 작업한다.
- 설계·구현 계획 커밋과 구현 커밋이 분리된다.
- 신규 테스트가 구현 전 RED, 구현 후 GREEN을 보인다.
- 다섯 재질문 경로와 추천·screen 배선, 이름 없음 폴백이 테스트로 증명된다.
- 장바구니 전체 단위 테스트, Ruff, pre-commit, diff 검사가 통과한다.
- Chrome E2E에서 상품명 표시와 다음 옵션 담기까지 확인한다.
- stage되지 않은 관련 변경이나 알려진 오류가 없다.
