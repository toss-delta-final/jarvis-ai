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
cases/combo_cases.jsonl + manifest.json     ★ 커밋된 케이스 + 재현 지문(sha256·seed)
expected/expected_behavior.jsonl            ★ 케이스별 기대 동작·근거·미정의 좌표
UNDEFINED_CELLS.md    ★★ 1급 산출물 — expected_behavior.jsonl 에서 report.py 가 **생성**(손으로 안 씀)
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
- **카테고리 leg fan-out(`category_queries`/`map_categories`) 은 범위 밖** — `category` 필터축은
  `ProductSearchFilters.category`(하드필터 문자열)만 잰다. leg 분해·매핑 커버리지는 별도 이슈(#331) 소관.
- **관측 러너의 fixture 는 대표 샘플이다** — 검색 결과 3건 고정 카탈로그, 프로필 always-None(HOME).
  실제 카탈로그 분포·프로필 다양성에 따른 랭킹 품질은 `evals/goldenset`(#333) 소관 — 이 하네스는
  "경로가 죽지 않고 계약 형태를 지키는가"만 잰다.

## 관측 러너가 안 쓰는 것

`observation_mode=manual`(context≠none, 35건)은 실행하지 않는다 — 멀티턴 승계(`categoryPrior`→
`intent_probe:category_action` 등)는 실 LLM 해석이 필요해 `linked` 로 intent_probe 셀만 가리킨다.
