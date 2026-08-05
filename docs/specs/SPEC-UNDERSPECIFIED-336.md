# SPEC — 과소지정 발화 처리 (이슈 #336)

## 1. 배경

"5만원 이내로 아무거나 세트로"·"5만원 이하로 아무거나"·"아무거나 추천해줘" 처럼 **무엇을**
찾는지는 말하지 않고 가격 같은 **제약**만(혹은 그마저도 없이) 준 발화다. `no_condition.py`
(#162, api-spec §4.17)가 "축이 전부 빈" 부분집합을 이미 처리한다 — 이 이슈는 그 위에 얹어
"제약만 있는 턴"까지 넓히고, 카드와 함께 카테고리를 되묻는다.

이 문서는 이슈에서 논의가 필요했던 6건과 그 결정을 기록한다.

| # | 논의 필요 | 결정 | 근거 절 |
|---|---|---|---|
| 1 | 과소지정을 어떤 축으로 판정하는가 — no_condition 과의 관계는? | no_condition 의 **상위 집합**. what-축(category/brand/keyword/color/attr_conditions)·rating_min 이 비면 트리거, price_min/max 는 있어도 트리거 | §2 |
| 2 | 제약만 있는 턴의 후보를 어디서 가져오는가 — 무필터 I-1 그대로 두는가? | I-3(인기 상품) + 가격 클라이언트 필터. 무필터 I-1 은 이슈가 없애려는 바로 그 문제(#162 재발)라 배제 | §3 |
| 3 | 되물음을 칩으로 내는가, 산문으로 내는가? | **산문(`token`)만.** `SuggestionChip` 은 relaxation/revert 중 정확히 하나를 강제하는 계약이라 카테고리 되물음 칩은 계약 의미 확장 — 명세 개정 없이는 칩을 새로 만들지 않는다 | §4 |
| 4 | 멀티턴에서 되물음 상태를 어떻게 승계하는가 — 새 저장소가 필요한가? | **새 상태 없음.** 답변은 다음 턴 decompose 가 일반 발화로 처리하고, prior 가 이미 반복을 막는다 | §5 |
| 5 | 예산 세트(#60)와의 접점 — 제약만 있는 턴에서 세트를 만드는가? | **카테고리 확정 후로 미룬다.** 이 턴은 세트 조합을 만들지 않는다(`has_total_budget` 와 같은 논리 — 무엇을 몇 개 살지 모르는 턴에 조합을 지어내면 근거 없는 묶음이 된다) | §5.1 |
| 6 | 평점 조건(rating_min)을 과소지정에 포함하는가? | **차단-축으로 제외**(보수적). I-3 인기 후보가 평점 사후필터를 타지 않아 이 모듈에서 복제하면 "같은 판정 두 곳" lessons 위반. 미탐 비용 0 | §2.2, §6 |

## 2. 판정 정의 — `is_underspecified_turn`

`app/agents/buyer/recommendation/underspecified.py`. `no_condition.is_no_condition_turn`
(#162)과 나란히 두고 비교하면 다음과 같다.

### 2.1 축 분류표 (`ProductSearchFilters`, `decompose._FILTER_AXES` 전체와 정확히 대조)

| 그룹 | 필드 | 있으면? |
|---|---|---|
| what-축 (`_WHAT_FILTER_AXES`) | `category`·`brand`·`keyword`·`color`·`attr_conditions` | **지목** — 트리거 안 함 |
| 제약-축 (`_CONSTRAINT_FILTER_AXES`) | `price_min`·`price_max` | 트리거 **유지** — 후보를 거르는 데만 씀(§3) |
| 차단-축 (`_BLOCKING_FILTER_AXES`) | `rating_min` | **지목 취급** — 트리거 안 함(보수적, §6) |

세 그룹의 합집합은 `decompose._FILTER_AXES` 와 정확히 일치하고(교집합 없음) —
`test_underspecified_axes_partition_filter_axes` 가 드리프트를 잡는다. 새 하드필터가
생기면 반드시 세 그룹 중 하나에 배정해야 한다(모듈 상단 `assert`).

### 2.2 `RouteDecision` 축 분류표 (필터 밖 축, 전체 14 필드와 대조)

| 그룹 | 필드 | 비고 |
|---|---|---|
| 차단 | `filters`(§2.1 로 세분) | |
| 차단 | `category_legs` | 매핑된 카테고리 — 있으면 지목 |
| 차단 | `category_queries` | 매핑 **전** 원시 신호 — 매핑 실패해도 지목(no_condition ⑤와 같은 근거, PR #311 리뷰) |
| 차단 | `semantic_query_is_fallback` | False(실신호 있음)면 지목 |
| 차단 | `repurchase_products` | 재구매 지목 |
| 차단 | `revert_categories` | 되돌리기 지목 |
| 제약(비차단) | `total_budget`·`buy_all` | 트리거 유지, 후보 소스 선택에만 영향(no_condition 의 `has_total_budget` 과 동일 논리) |
| 무관 | `intent`·`case`·`reply`·`cart`·`category_expanded`·`scoped_to_previous` | 판정에 영향 없음 |

**[리뷰 F4 반영] `scoped_to_previous` 는 차단이 아니라 무관이다.** 초판은 이 축을 차단으로
분류했는데, 이 판정은 첫 턴(`prior is None`) 한정이라 "직전 결과"(#113 의 지시 대상)가
존재하지 않는 스코프에서만 발동한다 — 즉 이 축이 True 라도 가리킬 대상이 없어 공허하다.
`is_no_condition_turn`(#162)도 같은 이유로 이 축을 안 본다. 초판대로 차단에 두면
`scoped_to_previous=True` + 나머지 전 축 빈값인 첫 턴에서 `is_no_condition_turn`=True ∧
`is_underspecified_turn`=False 인 반례가 생겨 §2.3 불변식이 깨졌다(재현: `test_scoped_to_
previous_does_not_block_trigger`).

`test_route_decision_axes_are_all_classified` 가 새 필드 누락을 잡는다(no_condition.py 의
동명 테스트와 동형).

### 2.3 불변식 — `no_condition ⊂ underspecified`

`is_no_condition_turn(d, p)` 이 True 인 모든 턴은 flag on 에서 `is_underspecified_turn(d, p, s)`
도 True 다 — no_condition 이 요구하는 조건(`_FILTER_AXES` 전체 빈값, 즉 price_min/max 도 포함)
이 underspecified 의 요구(what-축·rating_min 만 빈값)보다 **엄격**하기 때문이다.
`test_no_condition_implies_underspecified_when_flag_on` 이 고정한다.

### 2.4 마스터 스위치

`settings.underspecified_reask_enabled`(기본 False)가 꺼지면 판정 자체가 항상 False —
결함 발견 시 코드 재배포 없이 한 번에 전체 롤백할 수 있다(AC).

## 3. 후보 소스 — I-3 + 가격 클라이언트 필터

`recommendation/graph.py::_run_candidate_source`. 기존 `if not no_condition: return
await _run_search()` 를 `if not (no_condition or underspecified): ...` 로 확장한다.

- 인기 상품(I-3, §4.17) 경로에서 **가격 제약이 있으면** `underspecified.within_price_range`
  로 클라이언트 필터한다 — `no_condition.within_budget`(총액 예산)과 나란히 적용된다(둘은
  서로 다른 축이라 동시에 걸릴 수 있다).
- `within_price_range` 는 **입증 필요** 규약이다(`within_budget` 과 동일 사상) — 가격을
  모르는 상품(`price is None`)은 가격 조건이 하나라도 걸린 턴에서 제외한다. 순서는
  **보존**(stable) — BE 인기 순위·productId tiebreak 를 유지한다.
- **[리뷰 F1] `price_min`/`price_max` 는 `no_condition._is_blank` 와 같은 falsy 규약으로
  "미지정"을 판정한다** — `0` 도 미지정이다. 초판은 0 을 유효 경계로 취급해, `price_max=0`
  턴(decompose 산출값, `_is_blank` 가 이미 "조건 없음"으로 본다)이 **flag off** 에서도
  `is_no_condition_turn`=True 로 popular 경로를 타면서 `within_price_range(products, None,
  0)` 이 양수 가격 전부를 걸러 zero-result 로 만드는 회귀가 있었다(판정-필터 비대칭). 이제
  `within_price_range` 진입부에서 `_is_blank` 로 0/None 을 먼저 정규화한다.
- I-3 실패 시 기존 `popular_degraded` 폴백(무필터 → `_run_search`) 을 그대로 재사용한다.
  0건도 성공이다(§4.17).
- **profile 취향 경로 게이트는 건드리지 않는다** — 제약만 있는 턴은 `no_condition=False`
  라 애초에 그 경로를 타지 않는다(AI 카탈로그 인덱스에 가격이 없어 타면 안 된다). 이 사실은
  `test_multiturn_never_reasks`·`test_fully_specified_turn_never_reasks` 류의 회귀
  테스트가 간접 고정한다.
- **`may_auto_relax` 게이트**: `underspecified` 턴은 자동완화 대상에서 제외한다
  (`and not underspecified` 한 줄 추가). 다만 `may_auto_relax` 는 실제로는 conditions
  이벤트 **발신 시점**만 가르는 변수이고, 실제 자동완화·완화칩 probe 는 별개 조건
  (`if not candidates:`, `if not candidates or len(candidates) < relaxation_min_results:`)
  으로 독립 실행된다는 것을 구현 중 확인했다(§7.1 참조) — 그 두 지점에도 `not underspecified`
  를 추가해 "카테고리 없는 I-1 재검색"이 실제로 나가지 않게 막았다.

## 4. 되물음 — `token` 산문, 와이어 불변

### 4.1 왜 칩이 아닌가

`SuggestionChip`(`app/schemas/chat.py`)은 모델 검증자로 `revert`/`relaxation` 중
**정확히 하나**를 강제한다. §3.1 은 `suggestions` 이벤트의 용도를 "완화 제안(0건/소량) +
되돌리기(소모품 억제)"로 계약했다 — 카테고리 되물음 칩은 이 계약의 의미를 확장하는 것이라
**명세 개정(사람 게이트) 없이는 만들지 않는다.** 대신 `token` 산문으로만 되묻는다 — 새
이벤트·새 필드·기존 페이로드 변경이 전혀 없다.

### 4.2 문구 생성 — `build_reask_question`

노출 후보(이번 턴에 실제로 push 될 상품)에서 `category` 를 **순서 보존 dedup** 으로 최대
`underspecified_reask_examples_max`(기본 3, `ge=0`)개까지 추출하고 `_strip_unsafe` 로
정제한다(Spring 응답은 신뢰 경계 밖). 예시가 있으면 `{categories}` 자리표시자 템플릿
(`underspecified_reask_question_examples`), 없으면 generic(`underspecified_reask_question`).
자리표시자 부재·포맷 예외는 generic 으로 폴백한다(#162 budget notice 와 같은 방어 패턴 —
`str.format` 은 안 쓰는 키워드를 조용히 무시하므로 존재를 먼저 검사한다).

문구에는 **가격·평점 값을 넣지 않는다**(#171/#173 경계 — 예산 복창은 #162 고지 소관이라
중복하지 않는다). 질문은 어떤 조건이 적용됐다고 주장하지 않는다(#132 거짓 주장 금지).

**[리뷰 F3] 되물음 문구는 기동 검증하지 않는다** — 이 리포의 고지 config 들은 "빈 값 = 그
고지만 끄는 스위치" 관례다(`dedup_skipped_notice` 와 동일 판단). `underspecified_reask_
question` 이 빈 값(정제 후 포함)이면 되물음 token 만 조용히 꺼지고, 후보 소스 스왑(I-3 +
가격 필터)·자동완화 억제 등 다른 동작은 그대로 유지된다. 핀 테스트로 고정한다
(`test_empty_reask_question_only_disables_the_question`).

### 4.3 emit 지점 (flag on ∧ underspecified, 성공 종료 경로마다 정확히 1회, error 경로엔 없음)

**[리뷰 F2] 셋 다 "push 가 성공한 턴의 노출 후보" 기준으로 emit 시점을 잡는다** — push 전에
예시 질문을 내면 그 상품이 화면에 뜨지 않을 수 있는데도 "이 중에"라고 가리키는 표시=실제
(#51) 위반이 생긴다. 초판은 지점 1·2 를 push **이전**(profile 경로는 push 호출 앞, 메인
경로는 push 호출보다 한참 앞)에 뒀다가 이번 라운드에서 뒤로 옮겼다.

1. **profile 취향 경로** — push 성공/실패가 정해진 **뒤**(`products.ready` 성공 시 그 뒤,
   실패 시 `push_skipped_notice` 뒤). 이 경로는 AI 카탈로그 인덱스 랭킹이라 `categoryName`
   이 없어 성공·실패 어느 쪽이든 **generic** 질문만(예시를 뽑을 후보 자체가 없다).
2. **메인 경로** — push 결과가 정해진 뒤(`done` 직전). **push 성공**이면 노출 후보(실제로
   push 된 `ranked_ids`) 기반 예시 질문(`build_reask_question`). **push 실패**면 보여준 게
   없으니 예시 없이 **generic** 질문만(`underspecified_reask_question` 그대로) — 되묻기
   자체는 유지한다(다음 턴을 위한 질문이라 카드 유무와 무관하게 유효). `no_condition ∧
   underspecified` 이므로 두 턴 유형 모두 여기서 나간다.
3. **zero-result 경로** — 카드가 없어 노출 후보가 없다 → **generic** 질문. "카드 없는 답 +
   되물음"으로 다음 턴에 지목할 실마리를 준다(push 자체가 없는 경로라 원래부터 안전했다).

### 4.4 인기 상품 고지 (`underspecified_notice`)

`underspecified ∧ ¬no_condition ∧ ¬popular_degraded` 턴에만 신규 고지를 낸다("조건에 맞는
인기 상품" 취지 — 실제로 가격 필터를 통과한 인기 상품이라 참이다). `no_condition` 턴은 기존
#162 고지(`no_condition_notice_*`)가 이미 담당하므로 중복하지 않는다(상호 배타). `popular_degraded` 면 인기 주장 고지는 스킵하지만(#162 와 동일 정직성 규약), 되물음 질문은
그대로 낸다 — "인기 상품"이라는 주장만 거짓이 될 뿐 되묻는 행위 자체는 참이다.

## 5. 멀티턴 — 신규 상태 없음

되물음에 대한 답("이어폰")은 **일반 발화**로 처리된다 — 다음 턴 decompose 가 카테고리로
잡고, 가격 필터는 기존 `ThreadFilterStore` 승계로 이어진다. 새 저장소를 만들지 않는다
(#118/#119 래칫 재발 방지 — "판정을 저장하는 새 상태"는 항상 몇 턴 뒤 정합성 문제로
돌아온다는 lessons).

`total_budget`·`buy_all` 은 기존 설계상 **턴 로컬**(#60)이라 승계되지 않는다 — 알려진
한계로 §6 에 남긴다.

### 5.1 예산 세트(#60) 접점

**카테고리 확정 후로 미룬다** — 이 턴에서는 세트 조합을 만들지 않는다.
`no_condition.has_total_budget` docstring 의 논리를 그대로 따른다: 무엇을 몇 개 살지
사용자가 말하지 않은 턴에 조합을 지어내면 "이어폰+샴푸+등산화 합쳐 5만원" 같은 근거 없는
묶음이 나온다 — 고를 기준(니즈 leg)이 없기 때문이다. `budget_sets.py` 는 이 이슈에서
불가침이다(허용 파일 목록 밖).

### 5.2 D6 실측 — 되물음이 반복되는가

**질문**: 전 축이 빈 턴(순수 무조건)에서 되물음 다음, 사용자가 다시 같은 무조건 발화를
하면 되물음이 반복되는가?

**실측 결과**: **반복되지 않는다.** `ThreadFilterStore.put`(`app/agents/buyer/graph.py`)은
`_prepare_recommendation` 안에서 **매 추천 턴마다 무조건 호출**되며, `decision.filters` 가
전부 빈(default) `ProductSearchFilters` 여도 그대로 저장한다(`filters.model_dump()`, 필드
값 검사 없음). 따라서 무조건 턴 다음 턴의 `ThreadFilterStore.get` 은 `None` 이 아니라
**빈 필드로 채워진 `ProductSearchFilters` 인스턴스**를 돌려주고, `is_underspecified_turn`의
`prior is not None` 가드가 즉시 False 로 떨어뜨린다. 게다가 decompose 의 semantic_query
폴백 체인(`llm_sq or cat_signal or prior_sq or query`)이 1턴째 저장된 원문
(`prior.semantic_query`)을 `prior_sq` 로 읽어 2턴째 `semantic_query_is_fallback` 도
False 로 만든다 — **두 개의 독립적인 이유**로 반복이 막힌다. `test_second_bare_turn_does_
not_repeat_reask`(`tests/unit/test_underspecified_graph.py`)가 이 실측을 테스트로
고정한다. 동작은 바꾸지 않았다(관찰만).

## 6. config

| 키 | 기본값 | 용도 |
|---|---|---|
| `underspecified_reask_enabled` | `False` | 마스터 스위치 |
| `underspecified_notice` | "조건에 맞는 인기 상품으로 골라봤어요." | 제약만 있는 턴의 인기 고지 |
| `underspecified_reask_question` | "어떤 상품을 찾으시는지 조금 더 알려주시겠어요?" | generic 되물음 |
| `underspecified_reask_question_examples` | "{categories} 중에 찾으시는 게 있을까요? 아니면 다른 상품을 알려주셔도 좋아요." | 예시 되물음(`{categories}` 필수) |
| `underspecified_reask_examples_max` | `3`(`ge=0`) | 예시 카테고리 최대 개수 — 0 이면 예시 없이 항상 generic |

## 7. 알려진 한계

1. **`rating_min` 은 과소지정에서 제외**(§2.2 참조) — "평점 4 이상 아무거나"는 되묻지
   않고 종전 동작(무필터 검색)으로 처리된다. 드문 발화라 미탐 비용은 낮다고 판단했다.
   I-3 인기 후보 경로에 평점 사후필터를 태우는 재작업이 후속 이슈 후보다.
2. **`total_budget`은 멀티턴에 승계되지 않는다**(§5) — "5만원 안에서" → "이어폰" 답변
   뒤에는 예산이 사라진다. `total_budget` 자체가 #60 설계상 턴 로컬이라, 이 이슈가 그
   경계를 넓히지 않는다.
3. **무조건 턴의 되물음 반복 여부**(§5.2) — 실측 결과 반복되지 않음을 확인·테스트로
   고정했다. 이 결론은 `ThreadFilterStore.put` 의 "무조건 저장" 동작에 의존하므로, 그
   동작이 바뀌면(예: 빈 필터를 저장 생략하는 최적화) 이 문서를 재검증해야 한다.
4. **완화 칩(relaxation chips)이 과소지정 턴에서 완전히 꺼진다** — §3 의 자동완화·완화칩
   probe 차단은 "카테고리 없는 I-1 재검색"을 막는 부작용으로, 이 턴에서는 사용자가 완화
   칩을 눌러 조건을 넓히는 기존 UX 도 함께 사라진다. 되물음이 그 자리를 대신한다는 게
   이 설계의 전제다.

### 7.1 구현 중 발견한 결함과 조치 (자기 판단 지점)

D2 는 "`may_auto_relax` 게이트에 `and not underspecified` 한 줄을 추가하면 된다"고
명시했다. 실제로 통합 테스트(`test_price_constraint_only_turn_uses_popular_and_price_
filters`)를 돌려보니 **`may_auto_relax` 는 conditions 이벤트 발신 시점만 가르는
변수이고, 자동완화 루프·완화칩 probe 는 별도 조건(`if not candidates:` · `if not
candidates or len(candidates) < relaxation_min_results:`)으로 독립 실행되어 `may_auto_
relax` 를 전혀 참조하지 않는다**는 것을 확인했다(가격 제약만 있는 턴에서 완화칩 probe 가
30% 확대된 `price_max` 로 실제 I-1 을 호출하는 것을 재현). D2 의 진짜 목적("카테고리 없는
I-1 재검색을 막는다")을 달성하려면 이 두 지점에도 `not underspecified` 가드가 필요해
추가했다 — 설계 자체를 재설계한 것이 아니라, 명시된 한 줄로는 도달하지 못하는 지점을
같은 원칙(진입 분기 최소, 조건식 한 줄 추가)으로 마저 채운 것이다.

## 8. caseId 표

`evals/underspecified_cases/cases.json`(#328 공통 규약, `evals/underspecified_cases/
README.md`).

| caseId | 발화(참고) | testType | 기대 |
|---|---|---|---|
| buy-under-0001 | "5만원 이내로 아무거나 세트로 추천해줘" | MFT | reask=true |
| buy-under-0002 | "아무거나 추천해줘" | MFT | reask=true |
| buy-under-0003 | "5만원 이하로 아무거나 추천해줘" | MFT | reask=true |
| buy-under-0004 | "이어폰 추천해줘" | INV | reask=false |
| buy-under-0005 | "삼성 제품 아무거나" | INV | reask=false |
| buy-under-0006 | "그중에 5만원 이하"(멀티턴) | INV | reask=false |
| buy-under-0007 | "평점 4 이상 아무거나" | INV | reask=false |
| buy-under-0008 | "5만원 이하로 아무거나 추천해줘"(flag off) | INV | reask=false |

앵커 로더: `tests/unit/test_underspecified.py::test_cases_json_anchor`.
