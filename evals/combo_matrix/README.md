# evals/combo_matrix — 기능 조합 커버리지 매트릭스 (이슈 #335)

> 에픽 [#328](https://github.com/toss-delta-final/jarvis-ai/issues/328) 자식 ⑤. 공통 규약은
> `evals/README.md`(정본) — 이 문서는 그 규약을 이 하네스에 적용한 결과만 적는다.

## 왜

구매자 추천 파이프라인의 축(intent·필터·예산·신원·지면·degrade …)은 서로 **직교하지 않는다** —
`intent != recommend` 면 필터·case·예산 축이 아예 없고, `surface = HOME` 이면 발화 자체가 없다.
이런 제약을 무시하고 각 축을 따로 테스트하면 "축들이 겹치는 자리"의 동작이 코드에도 테스트에도
없이 방치된다. 이 하네스는 **제약 인지 pairwise(2-wise) 커버링 어레이**를 결정론으로 생성하고,
각 케이스에 코드 근거를 인용한 기대 동작(defined/partial/undefined)을 매겨 **근거 없는 셀**을
1급 산출물(`UNDEFINED_CELLS.md`)로 뽑는다. 방법론 앵커는 CheckList(Ribeiro 외, ACL 2020) —
MFT(최소기능, 라벨 필요)/INV(불변)/DIR(방향) 표기.

실제로 이 매트릭스가 찾은 첫 셀이 [#336](https://github.com/toss-delta-final/jarvis-ai/issues/336)
(무지정+예산+세트, 별도 레인에서 진행 중)이다 — §5 "발견한 미정의 셀" 참조.

## 아키텍처

```
axes.json            ★ 앵커 — 축·값(근거 file:line 동봉)·제약·위험 3-wise 튜플·seed·datasetVersion
schema.py            축·케이스·기대동작의 pydantic 스키마 — 규칙은 여기·axes.json 에, 문서엔 없다
generator.py          결정론 leaf 완전열거(DFS, 제약 인지) + greedy 2-wise/3-wise 커버 선택
loader.py             axes.json/케이스/기대동작 로드+검증 (스크립트는 데이터 파일만 읽는다)
runner.py + fakes.py  ScriptedLLM/fake 주입 관측 러너 — ci 케이스만 실행, manual 은 건너뜀
report.py             커버리지 지표(분자/분모) 계산 + UNDEFINED_CELLS.md 생성
pair_runner.py         INV/DIR 쌍(원본↔변형) 실검증 러너(이슈 #371) — pair_checks.jsonl 의
                        mode=ci 쌍만 실행해 PAIR_CHECKS.md 를 생성한다. runner.py/fakes.py 의
                        실행 스택(build_decompose_json·ScriptedLLM·_collect/run_buyer_turn)을
                        재사용한다(§ "INV/DIR 쌍 검증" 참조).
cases/combo_cases.jsonl + manifest.json     ★ 커밋된 케이스 + 재현 지문(sha256·seed)
expected/expected_behavior.jsonl            ★ 케이스별 기대 동작·근거·미정의 좌표
expected/pair_checks.jsonl                  ★ INV/DIR 쌍 검증 앵커(이슈 #371, 손으로 작성) —
                                              쌍 1건당 1행
UNDEFINED_CELLS.md    ★★ 1급 산출물 — expected_behavior.jsonl 에서 report.py 가 **생성**(손으로 안 씀)
PAIR_CHECKS.md         ★★ 1급 산출물(이슈 #371) — pair_checks.jsonl 에서 pair_runner.py 가
                        **생성**(손으로 안 씀)
```

## 축 요약 (17개, 전체 정의는 `axes.json`)

`intent`(8종, 코드 정본 — 이슈 본문 5종은 구버전 드리프트) · `case`(1/2/3, recommend 전용) ·
`buy_all`/`total_budget`(recommend 전용, 서로 독립) · 필터 8축(`category`·`price_min`·`price_max`·
`brand`·`rating_min`·`keyword`·`color`·`attr_conditions` — `decompose._FILTER_AXES` 정본) ·
`constraint_strength`(unspecified/normal/overspecified_zero) · `identity`(guest/member) ·
`context`(none/lastRecommendations/pendingCart/categoryPrior — screen 5종은 **v1 제외**,
`axes.json` `exclusions` 참조) · `surface`(CHAT/HOME) · `degrade`(none/embedding_missing/
rerank_failed/spring_timeout).

**제약**(예: `intent != recommend ⇒ case/필터/예산/degrade 일부 = n/a`, `surface=HOME ⇒
identity=member 고정 + 발화·필터·예산 없음`, `constraint_strength=unspecified ⇒ 필터 전부
absent+context=none`)은 `axes.json` `constraints` 에 기계 판독 형식으로 있고, `generator.py` 의
제네릭 인터프리터(`ConstraintIndex`)가 축 이름을 하드코딩하지 않고 해석한다.

## 재현 명령

```bash
uv run python -m evals.combo_matrix regenerate   # cases/combo_cases.jsonl + manifest.json 재생성
uv run python -m evals.combo_matrix.report        # 커버리지 지표 출력 + UNDEFINED_CELLS.md 재생성
uv run pytest tests/eval/test_combo_matrix_eval.py -v
uv run python -m evals.combo_matrix.pair_runner   # INV/DIR 쌍 실행 + PAIR_CHECKS.md 재생성(#371)
uv run pytest tests/eval/test_combo_matrix_pairs.py -v
```

같은 `axes.json`(`datasetVersion` 1.0.0) + 같은 `seed`(335335) ⇒ **바이트 동일**
`combo_cases.jsonl`(재현성은 `test_regeneration_matches_committed_cases_byte_identical` 이 지킨다).
`expected_behavior.jsonl` 은 재생성 명령이 건드리지 않는다 — 사람이 코드를 읽고 고정한 데이터라
axes.json 이 바뀌면 케이스 구성과 함께 수동으로 갱신해야 한다.

## 커버리지 결과 (2026-08-06 기준, `manifest.json`/`report.py` 실측)

| 지표 | 분자 | 분모 | 비율 |
|---|---|---|---|
| 2-wise(pairwise) | 1061 | 1061 | **100%** |
| 3-wise `budget_buyall_strength`(#336 계열) | 13 | 13 | 100% |
| 3-wise `unspecified_identity_surface` | 9 | 9 | 100% |
| 3-wise `context_intent_case` | 41 | 41 | 100% |
| 1-wise 비교 참고선(11케이스 스위트) | 700 | 1061 | 66.0% |

분모("유효 쌍")는 `axes.json` 제약을 만족하는 모든 완전 할당(leaf, 6,260개)의 축값 쌍 합집합이다
— 사후 필터가 아니라 생성 단계에서 정의된다. **케이스 58건**(pairwise/3-wise 커버용 MFT 55 ·
INV/DIR 파생 3 — DIR 2·INV 1)으로 2-wise 전체와 위험 3-wise 전부를 덮는다 — 1-wise 스위트(11건)의
66.0% 대비 pairwise 가 왜 필요한지의 정량 근거다(규약 1항 정신). 그중 `directedCases` 2건은
greedy 가 안 뽑는 조합을 직접 관측하려고 결정론으로 덧붙인 것이다: `wishlist_add`×`member`×
`spring_timeout`(combo-0057) · `order_status`×`member`×`spring_timeout`(combo-0058, 리뷰 R2 R8
대응 중 추가 — pairwise 가 뽑은 유일한 order_status×spring_timeout 조합도 identity=guest 라
로그인 게이트가 Spring 호출보다 먼저 걸려 그 축을 실측 못 하는 같은 유형의 공백이었다).

**기대 동작 라벨**: defined 51 · partial 4 · undefined 3 (합 58).

## 발견한 미정의 셀 (`UNDEFINED_CELLS.md` 요약, 상세는 그 문서 — 5개 셀·케이스 7건)

1. **`constraint_strength=unspecified, total_budget=present, buy_all=true`** — tracking
   `in_progress(#336)`. 무지정 판정은 `total_budget`/`buy_all` 을 보지 않고, 예산이 취향경로
   차단+인기상품 필터로만 반영된다(정의됨). 되묻기(clarify) 산출물이 코드에 없고 예산 필터
   0건일 때 relaxation 이 totalBudget 을 완화 축으로 다루지 않는다(미정의) — **재발명 금지**,
   #336 레인 소관.
2. **`surface=HOME, degrade∈{embedding_missing,rerank_failed,spring_timeout}`** — `degrade` 축은
   CHAT 추천 파이프라인(검색/rerank/임베딩 재정렬) 실패 어휘라 HOME(I-22, 라이브 임베딩·rerank
   호출 없음)엔 대응 코드 경로가 없다. HOME 의 실제 실패 모드(프로필/카탈로그 저장소 타임아웃)는
   이 축과 별개다 — **축 어휘 자체가 두 지면에 안 맞는다**는 발견.
3. **`intent=wishlist_add, degrade=spring_timeout`** — `stream_wishlist_add`
   (`app/agents/buyer/cart/wishlist.py:175-233`) 는 `SpringUnavailableError` 를 개별 처리하지
   않는다(형제 cart_add/cart_remove/cart_view/wishlist_remove 는 처리한다) — 상위 스트림의 범용
   catch-all(`app/core/stream.py:688-705`)로 새어나가 계약은 지켜지지만(스트림이 안 죽는다)
   코드가 `INTERNAL` 로 뭉뚱그려져 다른 셋과 비일관. **`identity=member` 조합(`directedCases`,
   combo-0057)으로 직접 관측** — `add_wishlist_fn` 을 몽키패치해 재현한 실측에서 실제로
   `SpringUnavailableError` 가 `stream_wishlist_add` 밖으로 그대로 전파됨을 확인했다(unit 경계라
   프로덕션의 `open_stream` catch-all은 안 거친다 — README 관측 한계 참조).

"회원 recall ≥ 게스트 recall"(#119 선례) DIR 파생 케이스는 **스펙 구멍이 아니라 관측 범위
한계**(recall 절대 기준은 `evals/goldenset`(#333) 소관)라 `UNDEFINED_CELLS.md` 에서 뺐다
(defined 로 분류, `aspect` 필드에만 그 한계를 남긴다) — 1급 산출물은 "스펙에 정의가 없는 셀"만
담는다.

## 알려진 관측 한계 (조용한 truncation 아니라 명시적 스코프 — `runner.py` 모듈 docstring 동봉)

- **`embedding_missing` degrade 는 이 하네스가 구별 관측할 수 없다** — `run_buyer_turn` 의
  `search` 주입은 `search_service.py` 임베딩 재정렬보다 상류를 대체해, 재정렬 성공/조용한 degrade
  가 이 경계에서 똑같아 보인다. `degrade=none` 과 동일하게 실행하고 `observed.notes` 에 명시한다.
- **`constraint_strength=overspecified_zero` 는 검색 0건을 직접 주입한다**(`fakes.make_search([])`)
  — 정의(필터 과지정→0건→자동완화·완화칩·`zero_result` 종료)가 실제로 실행되고 관측된다
  (`finishReason=zero_result`·`pushCount=0`). `degrade=spring_timeout` 과 겹치면 검색 실패가
  우선한다(0건 성공보다 상위 실패).
- **`constraint_strength=unspecified` + degrade≠none 조합은 `search`/`rerank` degrade 를 실제로
  타지 않는다** — 무지정 턴의 후보 소스는 `popular_fn`(I-3)이 먼저이고(`graph.py:797-830`),
  이 하네스의 `popular_fn` fake 가 항상 성공해 `_run_search()` 폴백(그리고 그 안의 degrade)까지
  가지 않는다. **코드가 정의한 우선순위**(popular 성공 시 search 시도 안 함)지 하네스 결함이 아니다.
- **wishlist/cart 계열 담기는 사전에 "직전 추천"을 채워야 한다** — `allowed_product_ids`(직전
  추천 ∪ screen.products) 밖 상품은 조용히 되물음으로 돌아 Spring 호출 자체에 닿지 못한다
  (`app/agents/buyer/graph.py:994-1019`). 이 하네스는 cart_add/wishlist_add 관측 전에 같은
  `thread_id` 로 recommend 웜업 턴을 1회 태워 productId 101 을 last_reco 에 올린다
  (`runner.py::_warm_up_last_reco`) — 웜업 없이는 어떤 degrade 를 주입해도 결과가 항상 같은
  되물음이었다(회원 user_id 를 숫자 문자열로 안 쓰면 `cart_identity` 가 익명으로 오판정하는
  별도 함정도 있었다 — 둘 다 실측 중 발견해 고쳤다). 이 전제는 `observed.notes` 에도 남긴다
  (리뷰 R9) — context 축은 `none`(decompose 입력 관점)이지만 세션 스토어엔 웜업의 직전 추천이
  있다는 사실이 관측 데이터만 봐서는 안 드러나기 때문이다.
- **담기 계열 Spring 실패는 개별 몽키패치가 필요하다** — `run_buyer_turn` 은 `add_fn`
  (cart_add)·`add_wishlist_fn`(wishlist_add) 을 주입 파라미터로 노출하지 않아(항상 기본값
  `spring_client.add_to_cart`/`add_wishlist`) `search` 주입만으로는 이 축이 관측되지 않았다
  (리뷰 R2 R7 — combo-0004 가 계속 "성공"으로만 보이던 공회전). 지금은 HOME 러너와 같은 패턴으로
  해당 모듈 함수를 직접 몽키패치한다 — 실측: `CART_ADD_FAILED`(reason=`CART_ERROR`, cart/graph.py:
  453-461) · `order_status`(리뷰 R8, combo-0058 directed): "주문 상태를 불러오지 못했어요"
  (order_status.py:130-136). `make_order_status_ok` fake 도 이 과정에서 실계약(`OrderStatusSummary`)
  대신 무관한 dict 를 돌려주던 결함을 함께 고쳤다 — guest 전용 경로만 exercised 돼 그동안
  안 드러났다.
- **카테고리 leg fan-out(`category_queries`/`map_categories`) 은 범위 밖** — 단, "category 필터축은
  `ProductSearchFilters.category`(하드필터 문자열)만 잰다"는 종전 서술은 **#371 실측으로 반증됐다**:
  `app/agents/buyer/graph.py:520-537` 의 canonical-or-null degrade 는 `decision.category_legs` 가
  비면 `filters.category` 를 무조건 `None` 으로 지운다 — 그리고 `category_legs` 는 오직
  `categoryQueries`→`map_categories` 매핑 결과로만 채워진다. 이 하네스의 `build_decompose_json`
  (`runner.py`)은 `categoryQueries` 를 채우지 않고 `map_categories_noop` 은 항상 빈 legs 를
  돌려주므로, **`category` 축은 지금까지 이 하네스 어디에서도(기존 55건 MFT 케이스 포함) 실제
  검색 경계에 도달한 적이 없다** — 채워도 항상 None 으로 지워졌을 뿐이다. `pair_runner.py`
  (combo-0054, 아래 "INV/DIR 쌍 검증" 절)만 전용 seam(`_pair_decompose_json`+
  `fakes.make_exact_match_category_mapping`)으로 이 축을 실제로 관측한다 — `runner.py`/
  `map_categories_noop` 자체는 고치지 않았으므로(기존 55건 관측 보존), **기존 MFT 케이스들의
  이 맹점은 그대로 남아 있다.** leg 분해·매핑 richness(거리컷·택일·확장) 커버리지는 별도
  이슈(#331) 소관이고, 기존 55건의 잔여 category 맹점 해소는 이 PR 범위 밖 — 후속 이슈로 이관한다.
- **관측 러너의 fixture 는 대표 샘플이다** — 검색 결과 3건 고정 카탈로그, 프로필 always-None(HOME).
  실제 카탈로그 분포·프로필 다양성에 따른 랭킹 품질은 `evals/goldenset`(#333) 소관 — 이 하네스는
  "경로가 죽지 않고 계약 형태를 지키는가"만 잰다.

## 관측 러너가 안 쓰는 것

`observation_mode=manual`(context≠none, 35건)은 실행하지 않는다 — 멀티턴 승계(`categoryPrior`→
`intent_probe:category_action` 등)는 실 LLM 해석이 필요해 `linked` 로 intent_probe 셀만 가리킨다.

## INV/DIR 쌍 검증 (이슈 #371)

`cases/combo_cases.jsonl` 의 `perturbation_of` 케이스(원본 ↔ 변형 쌍) 3건은 MFT/INV/DIR 라벨은
있었지만 그 성질(불변·방향)을 실제로 검증하는 실행 코드가 없었다(`evals/README.md` 규약 6항의
목적 미달). `pair_runner.py` 가 그 실행을 채운다 — 원본·변형 둘 다 `build_decompose_json` 으로
결정론 decompose 를 실현해 `ScriptedLLM` 에 고정 주입하므로, 원본 3건이 모두
`observation_mode=manual`(context≠none)이어도 **쌍 실행 자체는 멀티턴 해석과 무관하게 결정론**
이다(§ `pair_runner.py` 모듈 docstring).

### 앵커: `expected/pair_checks.jsonl`

쌍 1건당 1행, `PairCheckSpec`(`schema.py`) 스키마:

| 필드 | 의미 |
|---|---|
| `case_id` | 변형 케이스 id — 원본은 그 케이스의 `perturbation_of` 로 찾는다 |
| `kind` | `INV`\|`DIR` — 케이스의 `checklist_type` 과 일치해야 한다(테스트로 강제) |
| `mode` | `ci`\|`manual` — 결정론 실행 가능 여부 |
| `invariant_fields` | INV 전용 — 산출 프로젝션 중 동일해야 하는 필드 이름 목록 |
| `metric`/`direction` | DIR 전용(`mode=ci` 면 필수) — v1 은 `metric="push_product_count"` 하나만 |
| `guards` | DIR 공허 통과 방지 조건 이름 목록(아래 표) |
| `reason`/`link` | manual 이면 왜 CI 로 검증 불가한지 + 실검증 소관, ci 면 무엇을 재는지 |

### 산출 프로젝션 (INV "산출 동일"의 정의)

쌍 실행 1턴에서 캡처하는 값 — 비교는 spec 의 `invariant_fields`(INV) 또는 `metric`(DIR) 만 본다.

| 필드 | 포함/제외 | 사유 |
|---|---|---|
| `terminal`/`finishReason`/`errorCode` | 포함 | 기존 `runner.py` observed 와 동일 정의 |
| `searchFilters` | 포함 | search 콜러블이 **실제로 받은** `ProductSearchFilters`(8개 하드필터, camelCase) — 주입 decompose 가 아니라 파이프라인 통과 후 경계 도달값(필터 유실 회귀 감지가 요지) |
| `legs` | 포함(대부분 `[]`) | category 축이 present 인 쌍만 `pair_runner` 전용 seam(아래)으로 1-leg 를 채운다 — 그 외에는 항상 `[]`(leg 분해 커버리지는 #331 소관) |
| `listType`/`listsCount`/`perListProductCount`/`productIdsMultiset` | 포함 | push 계약 형태(구조) |
| `listEntryFieldKeys` | 포함 | 엔트리마다 **값이 실제로 채워진**(`None`/`[]`/`{}`/`""` 가 아닌) 필드 키만 담는다(`_entry_field_keys`, 리뷰 R2 F1) — `model_dump().keys()` 그대로 쓰면 pydantic 이 값과 무관하게 스키마 전 필드를 항상 담아 `['label','listId','productIds','reasons']` 상수가 되고, 그러면 이 필드는 **어떤 회귀에도 깨질 수 없다**(검증하지 않는 검증). `reasons` 는 값이 채워져 있어도 이 집합에서 **명시적으로 뺀다** — 아래 행과 같은 이유(정의된 동작)로, 값 자체를 비교 안 하는 것뿐 아니라 "채워졌는지" 여부도 비교하지 않는다. |
| `pushProductCount` | 포함 | DIR metric `push_product_count` 의 원천 |
| token 텍스트·이벤트 개수/순서 | 제외 | 문구는 계약이 아니고, 스트리밍 분할은 비계약 |
| `reasons` **값**(rationale 문구) | 제외 | rerank 폴백 시 소실이 정의된 동작(단, combo-0056 실측은 productIds 멀티셋 자체는 동일 — 아래 참조). `listEntryFieldKeys` 도 이 필드의 **키 존재 여부조차** 안 본다(위 행) — rerank 성공(2건)·폴백([]) 양쪽 다 이 키를 빼므로 어느 쪽이든 결과가 같다. |
| suggestions 칩 상세 | 제외 | 범위 밖 |

### DIR: metric·guards

`metric="push_product_count"` 는 perturbed/base 각각의 push 총 상품 수. `direction` 은
`non_increase`\|`non_decrease`. **guard 실패는 방향 부등식이 성립해도 FAIL**(공허 통과 방지,
이 이슈의 심사 포인트):

- `perturbed_filters_strict_superset` — perturbed 의 `searchFilters`(present 항목만)가 base 의
  **진상위집합**인지. 아니면 "필터를 늘렸다"는 전제 자체가 거짓이라 방향 부등식이 무의미하다.
- `base_count_positive` — base 의 metric 값 > 0. 0이면 `non_increase` 가 항상 공허하게 성립한다.

### 카테고리 seam (combo-0054 전용, `pair_runner.py` 한정 — 이슈 #371 R1 결정)

combo-0054(DIR — "필터 추가 → 결과 수 비증가")는 `category` 필터축을 검증 대상으로 삼는데, 실측
결과 `category` 축은 이 하네스 전체에서 legs 를 거치지 않으면 검색에 도달한 적이 없었다(위
"알려진 관측 한계" 절 정정 참조). `pair_runner.py` 는 이 축에 한해 `categoryQueries` 를 채우고
(`_pair_decompose_json`) `fakes.make_exact_match_category_mapping()`(raw exact match 만 대역 —
거리컷·택일·확장은 #331 소관, 재구현 아님)으로 legs 를 실제로 채운다. `runner.py`/
`map_categories_noop` 자체는 손대지 않았다 — 기존 55건 MFT 케이스의 `expected_behavior.jsonl`
관측을 보존하기 위해서다.

`PAIR_CATALOG`(`fakes.py`) 는 `CATALOG_PRODUCTS` 3건(`무선이어폰`) + 1건(`여행용품`, product_id
104) — category 필터가 실제로 결과를 줄이도록 카테고리 2종을 보장한다.

**실행 경로 비대칭(리뷰 R2 F3)**: combo-0054 의 base(`legs=[]`)와 perturbed(`legs=[('무선이어폰',
None)]`)는 "필터 한 개 차이"만이 아니라 **코드 경로 자체가 다르다.** base 는
`_run_search`(`recommendation/graph.py`)의 `if not legs:` 분기 — `decision.filters` 를 그대로
써 검색을 1회 부르는 단일 경로다. perturbed 는 legs 가 채워져 있어 `else` 분기(fan-out) —
`_leg(canonical, query)` 가 `base.model_copy(update={"category": canonical, "keyword": ...,
"semantic_query": ..., "limit": leg_limit})` 로 category·keyword·semantic_query·**limit** 을
override 한 뒤 검색을 부른다(`recommendation/graph.py:648-655`). 즉 결과 수 감소가 필터 때문인지,
아니면 leg 경로가 도입하는 `limit` 절단 때문인지 구분해야 근거가 선다.

실측 근거: 두 실행의 `searchFilters` 는 `category` 를 제외한 전 축이 완전히 동일하다
(`priceMax=50000`, 나머지는 전부 `null`) — leg 경로가 `priceMax` 등 다른 필터를 조용히
바꾸지 않는다. 그리고 결과 집합은 base `{101,102,104}` ⊃ perturbed `{101,102}` 로, **정확히
`104`(여행용품, category 가 다른 그 1건)만 빠졌다.** `limit` 절단이 원인이었다면 `leg_limit`
이 후보 수보다 작아 임의의 뒤쪽 상품(순서상 나중 항목)이 잘려나갔을 것이고, 그건 category 값과
무관하게 어떤 상품이든 빠질 수 있었다 — 그런데 실제로 빠진 상품은 정확히 "category 가 다른"
1건이다(`PAIR_CATALOG` 4건 중 leg_limit 이 4 미만으로 작동했다면 무선이어폰 쪽 101/102 중
하나가 빠졌을 수도 있었는데 그러지 않았다). 이건 관측 한계가 아니라 "이렇게 확인했다"는 근거
기록이다 — 감소가 `limit` 절단이 아니라 **category 필터 자체**에 귀속됨을 결과 집합으로 직접
확인했다.

### manual 분리: combo-0055

"회원 recall ≥ 게스트 recall"(#119 선례, DIR)은 `mode=manual` 이다 — recall 은 정답(relevance)
라벨이 있어야 계산되는데, 고정 fixture 검색 대역은 회원/게스트 산출이 항상 동일해 방향 관측이
**원리적으로 불가**하다(라벨이 없으니 CI 로 못 재는 게 아니라, 방향 자체가 안 갈린다). 절대
기준·표본은 `evals/goldenset`(#333) 소관 — `link` 필드가 가리킨다. manual 쌍은 실행 없이
`status="manual"` 로 결과·`PAIR_CHECKS.md` 분모에 그대로 들어간다(조용한 탈락 금지, 규약 8항).

### 필터링 검색 대역의 성격

`fakes.make_recording_filtering_search`(→ `RecordingFilteringSearch`)는 **Spring I-1 검색(외부
시스템)의 WHERE 계약 대역**이지 앱 판정 로직 재구현이 아니다 — `SpringProduct` 로 표현 가능한
하드필터(category 정확 일치·price_min/max 범위·brand 목록 포함·rating_min 이상)만 흉내 낸다.
표현 불가 필터(keyword·color·attr_conditions)가 present 로 들어오면 조용히 무시하지 않고
`ValueError` 로 즉시 실패시킨다 — 미래 쌍이 "필터가 적용된 것처럼" 공허 통과하는 것을 막는다.

### 결과 (2026-08-06 기준, `pair_runner.py` 실측)

INV 통과 1/1 · DIR(ci) 통과 1/1 · manual 분리 1건 · 분모(전체 쌍 행 수) 3. combo-0054 는
base=3(101·102·104) → perturbed=2(101·102) 로 엄격 감소가 실제로 관측됐다. combo-0056 은 실측상
`productIds` 멀티셋 자체가 rerank 성공/폴백 양쪽에서 동일(`[101,102,103,104]`)해
`invariant_fields` 에 `productIdsMultiset` 도 포함했다(§3-a 각주 — 실측이 예측과 다르면 fixture
를 늘리기 전에 원인부터). 상세는 `PAIR_CHECKS.md`.
