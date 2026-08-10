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

`intent`(9종, 코드 정본 — 이슈 본문 5종은 구버전 드리프트) · `case`(1/2/3, recommend 전용) ·
`buy_all`/`total_budget`(recommend 전용, 서로 독립) · 필터 8축(`category`·`price_min`·`price_max`·
`brand`·`rating_min`·`keyword`·`color`·`attr_conditions` — `decompose._FILTER_AXES` 정본) ·
`constraint_strength`(unspecified/normal/overspecified_zero) · `identity`(guest/member) ·
`context`(none/lastRecommendations/pendingCart/categoryPrior — screen 5종은 **v1 제외**,
`axes.json` `exclusions` 참조) · `surface`(CHAT/HOME) · `degrade`(**지면별 어휘, #367** —
CHAT 3종 `embedding_missing`/`rerank_failed`/`spring_timeout` + HOME 4종
`profile_unavailable`/`catalog_unavailable`/`catalog_timeout`/`reason_degraded` + 공통 `none`,
아래 「degrade 축은 지면별 어휘다」 참조).

**제약**(예: `intent != recommend ⇒ case/필터/예산/degrade 일부 = n/a`, `surface=HOME ⇒
identity=member 고정 + 발화·필터·예산 없음`, `constraint_strength=unspecified ⇒ 필터 전부
absent+context=none`, **`surface=CHAT ⇒ HOME 전용 degrade 4종 금지` / `surface=HOME ⇒ CHAT
전용 degrade 3종 금지`(#367)**)은 `axes.json` `constraints` 에 기계 판독 형식으로 있고,
`generator.py` 의 제네릭 인터프리터(`ConstraintIndex`)가 축 이름을 하드코딩하지 않고 해석한다.

### degrade 축은 지면별 어휘다 (#367)

`degrade` 축은 원래 CHAT 추천 파이프라인(검색/rerank/임베딩 재정렬) 실패 어휘만 있었는데,
#335 매트릭스가 `surface=HOME × degrade∈{embedding_missing,rerank_failed,spring_timeout}` 를
미정의 셀로 찾아냈다 — HOME(I-22)은 라이브 임베딩·rerank·Spring 검색(I-1)을 호출하지 않아
그 어휘에 대응하는 코드 경로 자체가 없기 때문이다. 승인된 결정(현행 추인)은 HOME 의 실제
실패 모드 4종을 `docs/api-spec.md` §3.7(v0.26.1)에 명문화하고 이 축을 지면별로 갈랐다.

| 값 | 지면 | 계약 | 코드 근거 |
|---|---|---|---|
| `none` | 공통 | 정상 | — |
| `embedding_missing` | CHAT 전용 | 재정렬만 격리 실패 — 조용히 degrade | `app/services/search_service.py:126-137` |
| `rerank_failed` | CHAT 전용 | LLMError → 검색순서 top-N + 고지 | `app/agents/buyer/recommendation/graph.py:1431-1440` |
| `spring_timeout` | CHAT 전용 | 검색 실패 → SEARCH_FAILED(턴 종료) | `app/agents/buyer/recommendation/graph.py:592-596,839-858` |
| `profile_unavailable` | HOME 전용 | 200 degrade — 프로필 항만 빠짐 | `app/services/home_recommendation.py:316-338` |
| `catalog_unavailable` | HOME 전용 | 503 `UPSTREAM_UNAVAILABLE` | `app/services/home_recommendation.py:340-373,406-408` |
| `catalog_timeout` | HOME 전용 | 504 `UPSTREAM_TIMEOUT` | `app/services/home_recommendation.py:368-370,390-392,403-405` |
| `reason_degraded` | HOME 전용 | 200 PERSONALIZED + reason=null | `app/services/home_recommendation.py:431-452` |

지면 밖 조합은 `axes.json` excludes 제약 2건(`surface_chat_forbids_home_degrade`·
`surface_home_forbids_chat_degrade`)이 생성 단계에서 막는다 — 사후 필터가 아니라 애초에
케이스가 나오지 않는다. `runner.py::_observe_home`이 HOME 4종의 실제 관측 주입(프로필/스토어
몽키패치)을 담당한다.

## 재현 명령

```bash
uv run python -m evals.combo_matrix regenerate         # cases/combo_cases.jsonl + manifest.json 재생성
uv run python -m evals.combo_matrix refresh-observed    # expected_behavior.jsonl 의 ci 행 observed 만 재실행 갱신(#381 D6)
uv run python -m evals.combo_matrix.report               # 커버리지 지표 출력 + UNDEFINED_CELLS.md 재생성
uv run pytest tests/eval/test_combo_matrix_eval.py -v
uv run python -m evals.combo_matrix.pair_runner          # INV/DIR 쌍 실행 + PAIR_CHECKS.md 재생성(#371)
uv run pytest tests/eval/test_combo_matrix_pairs.py -v
```

같은 `axes.json`(`datasetVersion` 3.0.0 — 1.0.0→2.0.0 은 #367(degrade 축 어휘 갱신),
2.0.0→3.0.0 은 #386(intent 축에 `wishlist_view` 신설). 둘 다 케이스 우주가 바뀌는 파괴적 변경이라
major 를 올린다) + 같은 `seed`(335335) ⇒ **바이트 동일**
`combo_cases.jsonl`(재현성은 `test_regeneration_matches_committed_cases_byte_identical` 이 지킨다).
`expected_behavior.jsonl` 의 `expected`·`evidence`·`status`·`undefined_tuple`·`aspect`·`linked`·
`tracking` 필드는 재생성 명령이 건드리지 않는다 — 사람이 코드를 읽고 고정한 데이터라 axes.json 이
바뀌면 케이스 구성과 함께 수동으로 갱신해야 한다. **`observed` 필드만은 예외**다 — 이건 러너를
그대로 재실행한 관측값이라 `refresh-observed`(§ 위)로 기계적으로 덮어쓸 수 있다. 단 이것도
**덮어쓰기 도구이지 판정 도구가 아니다** — 바뀐 값이 실측 개선인지 회귀인지는 사람이 코드 근거로
판단해 아래 "관측 재생성 이력" 절에 남겨야 한다.

## 커버리지 결과 (2026-08-07 기준, #386 재생성 후 `manifest.json`/`report.py` 실측)

| 지표 | 분자 | 분모 | 비율 |
|---|---|---|---|
| 2-wise(pairwise) | 1110 | 1110 | **100%** |
| 3-wise `budget_buyall_strength`(#336 계열) | 13 | 13 | 100% |
| 3-wise `unspecified_identity_surface` | 9 | 9 | 100% |
| 3-wise `context_intent_case` | 42 | 42 | 100% |
| 1-wise 비교 참고선(14케이스 스위트) | 759 | 1110 | 68.4% |

> 1-wise 참고선의 **분자 759 는 #367 시점 실측을 그대로 쓴다** — 그 14케이스 스위트에는
> `wishlist_view` 발화가 없어 #386 이 늘린 쌍을 하나도 덮지 않으므로 분자는 그대로이고 분모만
> 늘었다. 재실측 없이 비율만 갱신한 값임을 밝힌다.

분모("유효 쌍")는 `axes.json` 제약을 만족하는 모든 완전 할당(leaf, 6,265개)의 축값 쌍 합집합이다
— 사후 필터가 아니라 생성 단계에서 정의된다. `degrade` 축이 지면별 어휘로 7종(CHAT 3 + HOME 4)
+ `none` 이 되면서(#367) leaf/유효 쌍 분모가 늘었고(6,260→6,261, 1061→1092), intent 축에
`wishlist_view` 가 더해지며(#386) 다시 늘었다(6,261→6,265, 1092→1110). **케이스 65건**
(pairwise/3-wise 커버용 MFT 55 · INV/DIR 파생 3 — DIR 2·INV 1 · directed 7)으로 2-wise 전체와
위험 3-wise 전부를 덮는다 — 1-wise 스위트(14건)의 68.4% 대비 pairwise 가 왜 필요한지의 정량
근거다(규약 1항 정신). **커버리지 비율은 directed 케이스 추가로 바뀌지 않는다** — directed 는 이미
100% 인 쌍 커버를 늘리려는 게 아니라 **greedy 가 뽑는 조합으로는 특정 축을 실측할 수 없는 공백**을
메우려는 것이다(#426 로 3건 추가, 62→65).

`directedCases` 7건은 **greedy 가 안 뽑는(또는 재생성으로 잃은) 조합을 직접 관측**하려고
결정론으로 덧붙인 것이다 — 전부 "그 조합이 데이터에 없으면 그 축을 실측 자체를 못 한다"는 같은
유형이다:

| id | 왜 |
|---|---|
| `wishlist_add_member_spring_timeout` · `order_status_member_spring_timeout` · `wishlist_view_member_spring_timeout` | pairwise 가 뽑는 그 조합은 `identity=guest` 라 로그인 게이트가 Spring 호출보다 먼저 걸려 해당 degrade 축을 실측 못 한다(셋 다 같은 유형) |
| `overspecified_zero_member_none_price_min` | **[#386]** #425 판정(overspecified_zero 에서 자동완화가 안 도는 것은 완화 축이 없어서 생기는 정의된 동작)이 관측되던 자리를 재생성이 지웠다 — 그 자리가 `spring_timeout` × 필터 8축 present 로 바뀌면서 (a) 완화 축이 생겨 판정의 전제가 깨지고 (b) `finishReason=zero_result` 를 관측하는 ci 케이스가 하나도 남지 않았다(실측). 필터축을 `price_min` 하나로 못박아 #425 의 코드 근거를 그대로 되살린다 |
| `keyword_only_no_category`(combo-0063) | **[#426]** keyword=present 인 케이스는 전부 category 도 present 라 #51 `drop_keyword` 로 경계 도달값이 항상 `null` — 대역에 매칭을 넣어도 실행되지 않는다 |
| `color_only_no_other_filters`(combo-0064) | **[#426]** 기존 케이스는 category+price+brand 가 이미 후보를 1건으로 좁힌 뒤라 color 가 결정타가 못 된다(빼도 관측이 안 변함 = 변이 시험 불성립) |
| `attr_conditions_only_unfiltered_payload`(combo-0065) | **[#426]** attr 사후필터가 실제로 좁히는 모양(#393 A `unfiltered_bypass` → 인기 폴백)이 ci 에 없었다 — 같은 모양의 기존 케이스는 `context` 가 none 이 아니라 manual 이다 |

**기대 동작 라벨**: defined 63 · partial 2 · undefined 0 (합 65) — #367 로 HOME 미정의 셀 3건이
해소되고, #368(94f0fb2)로 이미 고쳐진 `wishlist_add` SpringUnavailableError 갭도 재관측에서
defined 로 전환됐다. #426 directed 3건도 defined 로 추가됐다. 잔존 미정의는 #336(무지정+예산+세트)
계열뿐이며, #386 재생성으로 그 **좌표를 차지한 케이스가 1건에서 2건으로** 늘어 partial 이 2건이
됐다(새 갭이 아니라 같은 좌표의 표본 증가).

## 발견한 미정의 셀 (`UNDEFINED_CELLS.md` 요약, 상세는 그 문서 — 1개 셀·케이스 1건)

1. **`constraint_strength=unspecified, total_budget=present, buy_all=true`** — tracking
   `in_progress(#336)`. 무지정 판정은 `total_budget`/`buy_all` 을 보지 않고, 예산이 취향경로
   차단+인기상품 필터로만 반영된다(정의됨). 되묻기(clarify) 산출물이 코드에 없고 예산 필터
   0건일 때 relaxation 이 totalBudget 을 완화 축으로 다루지 않는다(미정의) — **재발명 금지**,
   #336 레인 소관.

과거 이 목록에 있던 나머지 두 발견은 후속 이슈로 해소됐다 — 기록만 남긴다:

- **`surface=HOME, degrade∈{embedding_missing,rerank_failed,spring_timeout}`(#367 로 해소)** —
  `degrade` 축은 원래 CHAT 추천 파이프라인(검색/rerank/임베딩 재정렬) 실패 어휘였는데 HOME(I-22,
  라이브 임베딩·rerank 호출 없음)엔 대응 코드 경로가 없어 미정의였다. **축 어휘 자체가 두 지면에
  안 맞는다**는 이 발견을 승인된 A안(현행 추인)으로 반영해 `degrade` 축을 지면별 어휘로 갈랐다
  (위 「degrade 축은 지면별 어휘다」 절). HOME 의 실제 실패 모드 4종은 이제 defined 다.
- **`intent=wishlist_add, degrade=spring_timeout`(#368 로 해소)** — `stream_wishlist_add`
  (`app/agents/buyer/cart/wishlist.py:175-233`) 가 `SpringUnavailableError` 를 개별 처리하지
  않아(형제 cart_add/cart_remove/cart_view/wishlist_remove 는 처리) 상위 스트림의 범용
  catch-all(`app/core/stream.py:688-705`)로 새어나갔다. #368(94f0fb2)이
  `except (WishlistError, SpringUnavailableError)` 를 추가해 형제들과 같은 처리 형태(action
  `WISHLIST_ADD_FAILED`/`WISHLIST_ERROR`)로 수렴시켰다 — 재관측(`identity=member` 조합)에서
  `unhandledException` 대신 정상 action 이 나옴을 확인했다.

"회원 recall ≥ 게스트 recall"(#119 선례) DIR 파생 케이스는 **스펙 구멍이 아니라 관측 범위
한계**(recall 절대 기준은 `evals/goldenset`(#333) 소관)라 `UNDEFINED_CELLS.md` 에서 뺐다
(defined 로 분류, `aspect` 필드에만 그 한계를 남긴다) — 1급 산출물은 "스펙에 정의가 없는 셀"만
담는다.

## 알려진 관측 한계 (조용한 truncation 아니라 명시적 스코프 — `runner.py` 모듈 docstring 동봉)

- **`embedding_missing` degrade 는 이 하네스가 구별 관측할 수 없다** — `run_buyer_turn` 의
  `search` 주입은 `search_service.py` 임베딩 재정렬보다 상류를 대체해, 재정렬 성공/조용한 degrade
  가 이 경계에서 똑같아 보인다. `degrade=none` 과 동일하게 실행하고 `observed.notes` 에 명시한다.
- **필터 축의 검색 경계 관측(#381 → #426 로 해소)** — `_observe_chat` 은
  `fakes.make_recording_filtering_search()`(대역 카탈로그 `PAIR_CATALOG` 5건 — `무선이어폰` 4건·
  `여행용품` 1건)를 써서 search 콜러블이 실제로 받은 `ProductSearchFilters` 를
  `observed.searchFilters` 에 담는다(camelCase 8축, `pair_runner._SEARCH_FILTER_KEYS` 와 정의 공유
  — `runner.search_filters_projection`). **#426 이후 하드필터 8축이 전부 실제로 걸러진다** —
  대역이 `search_catalog` 를 통째로 대체하지 않고 그 아래 `SearchBackend` 자리
  (`fakes.SpringWhereCatalogBackend`, 실제 네트워크 경계)에 서기 때문이다:
  - **Spring 와이어 6축**(`keyword`·`categoryName`·`minPrice`·`maxPrice`·`brandName`·`color`,
    `spring_client._search_query_params` 기준)은 대역이 **WHERE 계약**으로 흉내 낸다 — 외부 시스템
    동작이라 대역이 흉내 내는 것이 정당한 범위다. LIKE 대상은 api-spec §4.6 그대로(keyword 는
    상품명+summary+attributes, color 는 attributes). `summary`·`attributes` 가 비어 있는 상품
    (105)은 그 축의 LIKE 대상이 없어 **제외**된다 — 데이터 부재로 빠지는 것이 Spring LIKE 의 실제
    동작이고, 같은 "데이터 부재"를 AI 사후필터는 **보존**으로 처리한다(#100 P0, 아래).
  - **AI 사후필터 2축**(`rating_min`·`attr_conditions`)은 **배포 코드가 그대로 돈다**
    (`search_service.apply_ai_side_filters`). 이 축들은 Spring payload 축이 아니고
    (`spring_client.search_filter_axes`), `attr_conditions` 는 축별 완화 재시도까지 있는 앱 판정
    로직이라 대역이 흉내 내면 같은 판정을 두 벌 갖는 드리프트가 된다(`fakes.py` 모듈 docstring).
    `attr_conditions` 는 `searchFilters` 에 값이 실려 있어도 적용 여부를 알 수 없으므로, 사후필터
    호출 자체를 `observed.attrConditionsPostFilter`(`{invoked, inputCount, outputCount}`)로
    계측한다 — attr 축이 present 인 행에만 싣는다(전 행에 늘리면 `refresh-observed` diff 가 번져
    케이스별 사람 판정이 불가능해진다). 이 필드는 `OBSERVED_GUARDED_FIELDS`(#424) 에도 등록돼
    키 존재 여부까지 드리프트 가드가 대조한다.
  - `_UNREPRESENTABLE_FILTER_CAMEL`(표현 불가 축 기록, D1)은 그래서 **비어 있다**. 메커니즘은
    남겨 둔다 — 미래에 대역이 표현 못 하는 축이 생기면 조용히 무시하지 않고 데이터로 드러내는
    자리다(성질 테스트가 잠근다). D1 자체의 근거는 그대로다: 예전엔 표현 불가 축이 present 면
    `ValueError` 로 즉시 실패시켰는데, 그 예외가 앱의 검색 실패 처리에 삼켜져 "공허 통과 방지"가
    아니라 **공허 통과를 만들고 있었다**(INV 쌍 실측 — 아래 "관측 재생성 이력" 참조).

  `search` 가 아예 안 불린 케이스는 `observed.searchCallCount == 0`·`searchFilters == null` 로
  구별된다(예: `constraint_strength=unspecified` — popular_fn 우선). 검색이 여러 번 불릴 수
  있으면(완화 재검색 — **자동완화 루프** 또는 **완화 칩 estCount probe**, 이 매트릭스 ci 케이스는
  실측상 전부 후자다. 아래 `overspecified_zero` 항목·「관측 재생성 이력」참조) **첫 호출(주 검색)**
  만 쓴다 — 재검색이 축을 하나씩 완화하며 값이 바뀌므로 마지막 호출을 쓰면 "주입에 가장 가까운
  값"이라는 의도가 깨진다(`pair_runner.py` 와 같은 규약). `observed.pushProductCount`(push 된 상품
  수 합, 기존 `pushCount`=이벤트 수와 혼동 금지)가 필터가 실제로 결과를 줄였다는 가시적 결과다.
- **`keyword` 는 category leg 가 있으면 검색 경계에 도달하지 않는다 — 대역의 한계가 아니라 앱의
  정의된 동작이다(#51)**. `recommendation/graph.py` 의 `drop_keyword`(config
  `search_drop_keyword_with_category` ∧ `search_backend=="embedding_rerank"` ∧ `category_legs`
  존재)가 leg 검색어에서 keyword 를 비운다 — Spring keyword 가 상품명 글자 부분일치 AND-필터라
  동의어('청바지' vs 상품명 '데님 팬츠')를 retrieval 에서 원천 배제하기 때문이다. 그래서
  `keyword=present ∧ category=present` 인 케이스의 `searchFilters.keyword` 는 `null` 이 **정상**
  이고, 이 값을 대역 결함으로 읽으면 안 된다. 이 축을 실제로 재는 것은
  `category=absent ∧ keyword=present` 인 directed 케이스(combo-0063)가 담당한다.
- **세 축(keyword·color·attr_conditions)이 결과를 실제로 가르는지는 directed 케이스가 잰다(#426)** —
  기존 케이스로는 원리적으로 불가능하다. keyword 는 위 #51 로 경계에 도달하지 않고, color 는
  필터 8축이 전부 present 인 케이스에서 category+price+brand 가 이미 후보를 1건으로 좁힌 뒤라
  결정타가 되지 못하며(대역에서 color 매칭을 빼도 관측이 안 변한다 = 변이 시험 불성립),
  `attr_conditions` 사후필터가 후보를 좁히는 모양(#393 A `unfiltered_bypass` → 인기 폴백)은 ci
  케이스에 없었다(같은 모양의 기존 케이스는 `context` 가 none 이 아니라 manual). 그래서
  `axes.json` `directedCases` 에 combo-0063(keyword only)·0064(color only)·0065(attr_conditions
  only)을 못박고, 각각 대역/사후필터를 무력화하면 관측이 변하는 것을 변이 시험으로 상주 검증한다
  (`tests/eval/test_combo_matrix_eval.py` D8-5~D8-7).
- **`constraint_strength=overspecified_zero` 는 검색 0건을 직접 주입한다**(D4, #381 재검토 — 결론:
  유지) — `RecordingFilteringSearch(products=[])`(빈 카탈로그, `degrade=spring_timeout` 이 아닐 때만)
  로 표현 가능한 필터를 적용해도 항상 0건이 되게 하면서 경계 도달 `searchFilters` 도 함께 기록한다.
  0건을 **자연 발생**시키지 않고 주입을 유지하는 근거를 실측으로 확인했다: combo-0031 의 표본
  필터값은 `price_min=20000` 뿐인데, `PAIR_CATALOG` 의 가격이 전부 20000 이상이라 **필터를 실제로
  적용해도 전건이 그대로 통과한다**(0건이 자연 발생하지 않는다). 이 실측 당시 카탈로그는 4건
  (39000·48000·89000·30000)이었고, [#386] 이 `price_min` DIR 쌍의 공허 통과를 막으려고 19000원짜리
  1건(product 105)을 더해 지금은 5건이다 — 그 1건도 `price_min=20000` 미만이라 필터에 걸리지만
  나머지 4건이 그대로 통과하므로 **결론은 같다**(0건이 자연 발생하지 않는다).
  0건을 자연 발생시키려면 비현실적인 과지정 표본값이 따로 필요한데, 그래도 이 케이스에서
  자동완화·완화칩이 도는 건 아니다(아래 참조) — present 인 필터축(`price_min`) 자체가 완화 축이
  아니라서다.

  **#425 판정(정의된 동작, 갭 아님) — 실제로 관측되는 건 `zero_result` 종료뿐이다.** 실측:
  `finishReason=zero_result`·`pushCount=0`·`pushProductCount=0`·`searchCallCount=1`(주검색 1회뿐,
  재검색 없음). 자동완화·완화칩이 안 도는 이유는 `price_min` 이 완화 축이 아니라서다 —
  `app.agents.buyer.recommendation.relaxation.FIELD_TO_ATTR` 는 `priceMax`·`ratingMin`·`brand`·
  `color` 뿐이고(모듈 docstring "비카테고리 조건(가격 상한·평점 하한·브랜드·색상)만 한 단계 푼다"),
  `build_relaxation_candidates` 는 `FIELD_TO_ATTR` 밖 필드를 조용히 `continue` 로 건너뛴다. config
  로도 `priceMin` 을 넣을 수 없다 — `app.core.config.Settings._require_known_relaxation_chip_fields`
  가 기동 시점에 `FIELD_TO_ATTR` 밖 이름을 거부한다. 그래서 combo-0031 은
  `build_relaxation_candidates(filters, settings) == []` 이고, `stream_recommendation`
  (`app/agents/buyer/recommendation/graph.py`)의 `may_auto_relax` 게이트가 False, 자동완화 루프
  (`if not candidates and not underspecified:`)는 진입해도 후보가 비어 0회 반복, 완화 칩 블록
  (`if not underspecified and (not candidates or len(candidates) < settings.relaxation_min_results):`)
  도 진입해도 probe 후보가 비어 칩 0개다. **갭이 아니라 정의된 동작이므로 `UNDEFINED_CELLS.md` 에
  등재하지 않는다.** 0건 주입 유지 결정(위 문단)도 이 판정 위에 서 있다 — 표본값(`runner.
  _FILTER_SAMPLE`)을 과지정 값으로 바꿔 자연 0건을 노려도, 이 케이스에 present 인 축은 여전히
  `price_min` 하나라 완화 축이 새로 생기지 않는다. present 축 구성 자체를 바꾸려면
  `axes.json`/케이스 재생성(바이트 동일 재현 가드)을 흔들어야 해서 별개 작업이다. **자동완화
  전용 축을 이 매트릭스에 새로 뽑지도 않는다** — 자동완화의 실검증은 이미
  `tests/unit/test_relaxation.py`(`test_auto_relaxation_emits_notice_and_recovers_products` 등)
  소관이고, 이 하네스의 고정 대역 카탈로그 + 0건 주입으로는 "완화가 결과를 **살린다**"를 표현할 수
  없다(주입이 항상 0건이라 probe 도 0건 → 채택 자체가 불가능). 이 판정은
  `test_overspecified_zero_has_no_relaxable_axis_so_no_relaxation_search`
  (`tests/eval/test_combo_matrix_eval.py`)가 잠근다.
  `degrade=spring_timeout` 과 겹치면 검색 실패가 우선한다(0건 성공보다 상위 실패, `failing_search`).
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
  454-462) · `order_status`(리뷰 R8, combo-0057 directed): "주문 상태를 불러오지 못했어요"
  (order_status.py:130-136). `make_order_status_ok` fake 도 이 과정에서 실계약(`OrderStatusSummary`)
  대신 무관한 dict 를 돌려주던 결함을 함께 고쳤다 — guest 전용 경로만 exercised 돼 그동안
  안 드러났다.
- **주입 예외 타입은 실 어댑터 규약을 따른다(#376)** — `spring_timeout` 이 담기 계열에 주입하는
  예외는 `SpringUnavailableError` 가 아니라 `WishlistError`(add_wishlist)·`CartError`
  (add_to_cart)다. 실 어댑터(`app/services/spring_client.py::add_wishlist`/`add_to_cart`)는
  `except httpx.HTTPError`(타임아웃 `httpx.TimeoutException` 포함, `HTTPError` 하위 클래스)를
  각각 그 타입으로 낙성한다 — `SpringUnavailableError` 는 그 두 어댑터의 실패 규약이 아니다.
  두 흐름 다 `except (CartError, SpringUnavailableError, ValidationError)`
  (cart/graph.py:454)·`except (WishlistError, SpringUnavailableError)`(cart/wishlist.py:245)로
  실제 타입을 개별 처리하므로, 이전에 `SpringUnavailableError` 를 주입해도 겉보기 결과
  (action `*_FAILED`)는 같았지만 "실제 어댑터가 이 예외를 낸다"는 증거로는 부정확했다
  (`tests/eval/test_combo_matrix_eval.py::test_add_wishlist_and_add_to_cart_injections_match_adapter_exception_convention`
  이 이 타입을 잠근다).
- **카테고리 leg fan-out — #381 D5 로 `category` 축이 이제 실제 검색 경계에 도달한다.**
  `app/agents/buyer/graph.py:520-537` 의 canonical-or-null degrade 는 `decision.category_legs` 가
  비면 `filters.category` 를 무조건 `None` 으로 지운다 — 그리고 `category_legs` 는 오직
  `categoryQueries`→`map_categories` 매핑 결과로만 채워진다. #371 R1 당시엔 `runner.py::
  build_decompose_json` 이 `categoryQueries` 를 채우지 않고 `map_categories_noop` 이 항상 빈
  legs 를 돌려줘, `category` 축이 이 하네스 어디에서도(기존 54건 MFT 케이스 포함) 실제 검색
  경계에 도달한 적이 없었다 — `pair_runner.py` 전용 seam(`_pair_decompose_json`)만 별도로 이 축을
  관측했다. **#381 D5 로 이 seam 이 `build_decompose_json` 본체에 흡수됐다** — `category ==
  "present"` 면 `categoryQueries` 를 채우고(`_FILTER_SAMPLE["category"]` 공유), `_observe_chat` 도
  `map_categories_noop` 대신 `fakes.make_exact_match_category_mapping()`(raw exact match 만 대역 —
  거리컷·택일·확장은 `#331` 소관, 재구현하지 않는다)을 쓴다. `pair_runner.py` 의 구
  `_pair_decompose_json()` 래퍼는 제거하고 `build_decompose_json()` 을 직접 쓴다 — 두 러너가 같은
  seam 을 공유해 드리프트 여지가 없다. `map_categories_noop` 자체는 삭제하지 않았다 — 웜업 턴
  (`_warm_up_last_reco`)이 여전히 쓴다(그 턴의 decompose 는 categoryQueries 를 절대 채우지 않아
  거동 차이가 없다). 실측: category 축이 있는 4개 ci 케이스(combo-0026·0053·0054·0055) 모두 leg
  이 1개 생겨 검색에 실제로 도달한다 — 단 BUY_ALL(세트)은 "니즈 2개 이상"이 조건이라(state.py:
  128-131) leg 1개로는 여전히 트리거되지 않는다(실측으로 확인, `test_ci_cases_execute_and_
  defined_cases_match_contract`). leg 분해·매핑 richness(거리컷·택일·확장) 자체의 커버리지는
  별도 이슈(#331) 소관이다.
- **HOME 픽스처는 `home_reco_min_candidates` 이상의 상품을 채워야 관측이 성립한다**(리뷰 F1,
  #367) — `_observe_home` 의 건강한 스토어가 이 값(기본 5) 미만이면 `rank_home` 이
  `INSUFFICIENT_CANDIDATES` 로 조기 반환해 `combo-0050`(none)·`combo-0051`
  (profile_unavailable)·`combo-0052`(reason_degraded) 모두 reason 관측 경로에 닿지 못하고
  `reasonsNull: True` 는 빈 리스트에 대한 vacuous truth 로 둔갑한다(예전 관측이 이 함정에
  빠져 있었다). 러너는 후보 수를 코드에 하드코딩하지 않고 `get_settings().home_reco_min_candidates`
  에서 유도해, 설정값이 바뀌어도 조용히 다시 공허해지지 않고
  `test_home_healthy_fixture_meets_min_candidates`(`tests/eval/test_combo_matrix_eval.py`)가
  시끄럽게 깨진다. 픽스처 상품엔 `extras.situation_tags` 를 넣어 cart 시그널(101)과 태그가
  겹치게 구성했다 — `build_reasons` 가 실제로 문장을 고를 재료가 있어야 "주입 있음(reason_degraded)
  → 전부 null / 주입 없음(none) → 일부 non-null"의 대비가 성립한다
  (`test_home_reason_degraded_injection_actually_runs`). `profile_unavailable` 은 outcome 만으로
  `none` 과 구별되지 않아(계약상 와이어 구별 신호 없음, api-spec §3.7 v0.26.1) 러너 계측
  (`profileHookInvoked`/`buildReasonsInvoked`)으로 주입이 실제로 실행됐음을 관측 dict 에 남긴다.
- **`#393` 최소 필터 가드는 `runner.py` 에서 끄지 않는다(`pair_runner.py` 만 끈다)** — 이 관측
  러너(`_observe_chat`)는 **배포 기본값 그대로** 관측해야 그 값이 "실제로 배포되는 동작"을 대표한다
  (`expected_behavior.jsonl` 의 존재 이유). `pair_runner.py` 는 성격이 다르다 — "Spring I-1 WHERE
  계약 대역"으로 **필터 배관**(하드필터가 search 콜러블에 실제로 도달하는가)만 재는 게 목적이라,
  가드를 켜 두면 전 축 absent 인 base arm 이 아예 search 에 못 닿아 그 질문 자체가 성립하지 않는다
  (§ 아래 "INV/DIR 쌍 검증 > #393 최소 필터 가드와의 축 분리" 절 참조). 두 러너의 목적이 다르므로
  이 차이는 드리프트가 아니다.
- **관측 러너의 fixture 는 대표 샘플이다** — 검색 결과는 `PAIR_CATALOG`(5건, category 2종) 고정
  카탈로그(#381 이전엔 `CATALOG_PRODUCTS` 3건 고정, 필터 미적용), 프로필 always-None(HOME). 실제
  카탈로그 분포·프로필 다양성에 따른 랭킹 품질은 `evals/goldenset`(#333) 소관 — 이 하네스는
  "경로가 죽지 않고 계약 형태를 지키며 표현 가능한 필터가 실제로 걸러지는가"만 잰다.
- **`intent_cart_quantity_not_generated`(#285, I-25 §4.13)** — `cart_quantity` 는 이 매트릭스가
  재지 않는다(`axes.json` excludes 로 어떤 leaf 에도 선택되지 않게 막음): 이 하네스는
  `build_decompose_json` 으로 intent 를 강제 주입해 라우팅을 재지 않고, 수량 변경용 Spring
  대역도 없다 — 값을 넣으면 65→73 케이스로 밀려 63/65건의 `expected_behavior.jsonl` 사람 판단이
  다른 시나리오에 조용히 붙는다. 라우팅 회귀는 `evals/intent_probe` 의 `cart_quantity` 그룹이
  잰다(`wishlist_view_context_none`/#386 과 같은 교환).

## 관측 재생성 이력 (#426, 2026-08-07)

`refresh-observed` 로 ci 31행(기존 28 + directed 3)을 재실행했다. **회귀는 0건** — 값이 바뀐 행은
2건, 신규 3건, 나머지 60행은 바이트 불변이다.

| 케이스 | 값 변화 | 판정 |
|---|---|---|
| combo-0058 (필터 8축 전부 present, INV 쌍 원본) | `unappliedSearchFilters: ["color","attrConditions"] → []` + `attrConditionsPostFilter: {invoked: true, inputCount: 1, outputCount: 1}` 추가 | **실측 개선** — 두 축이 이제 실제로 적용된다(color 는 대역이 WHERE 로, attr_conditions 는 배포 사후필터가). 사후필터가 `invoked: true` 로 잡힌 것은 **그 코드가 하네스에서 처음 실행됐다**는 뜻이다(이전엔 호출 0회). `input==output` 은 이 케이스에선 다른 축이 이미 후보를 101 한 건으로 좁힌 뒤라 attr 조건이 결정타가 아니라는 뜻 — 좁히는 모양은 combo-0065 가 따로 잰다 |
| combo-0035 (attr present, `degrade=spring_timeout`) | `attrConditionsPostFilter: {invoked: false, 0, 0}` 추가 | **필드 추가 + 실측 개선** — 검색이 죽어 사후필터에 아예 도달하지 못한 턴이다. `invoked: false` 가 **"축이 평가되지 않았다"를 "평가됐지만 안 걸렀다"(`invoked: true, input==output`)와 데이터에서 구별**한다 — 이 구별이 없으면 두 상태가 똑같이 "아무 일도 안 일어난 것"으로 보인다 |
| 나머지 60행 | 변화 없음 | 필터가 실제로 적용돼도 `pushProductCount`·`eventTypes`·`terminal` 이 그대로라는 확인 — **대역 자리를 `SearchBackend` 로 내린 것이 관측 계약을 흔들지 않았다** |
| combo-0063·0064·0065 | 신규 행 | directed 케이스 3건(#426). 실측: 0063 `keyword="가벼운"` 경계 도달·`pushProductCount 3` / 0064 `color="블랙"`·`3` / 0065 `searchCallCount 0`(인기 폴백)·`attrConditionsPostFilter {invoked: true, 3, 2}`·`pushProductCount 2` |

directed 3건의 `pushProductCount` 3·3·2 는 각각 이렇게 나온다.

- **0063(keyword)·0064(color)** — `PAIR_CATALOG` 5건 중 **102 와 105 가 탈락**해 3건. 102 는 조건
  불일치(summary 에 '가벼운' 없음 / 색상 화이트)이고, 105 는 `summary`·`attributes` 가 비어 LIKE
  대상이 `name` 뿐이라 **데이터 부재로** 빠진다 — 둘 다 Spring LIKE 의 실제 동작이다.
- **0065(attr_conditions)** — 이 케이스는 인기 폴백이라 후보가 `CATALOG_PRODUCTS` 3건이고, 그중
  `방수=False` 인 102 만 탈락해 2건. AI 사후필터는 **축이 없는 상품을 보존**하므로(#100 P0) 위
  두 축과 데이터 부재 처리 규약이 반대다 — 픽스처가 그 대비를 그대로 담고 있다.

대역/사후필터를 무력화하면 이 숫자가 5·5·3 으로 바뀌는 것을 변이 시험이 상주 검증한다
(`tests/eval/test_combo_matrix_eval.py` D8-5~D8-7).

## 관측 재생성 이력 (#381)

`refresh-observed` 로 ci 25행 전부를 재실행해 `observed` 를 갱신했다(2026-08-06). 값이 바뀐 행은
20건 — 케이스별 판정(실측 개선/회귀/필드 추가)을 아래 표에 남긴다. **회귀는 0건.**

공통 배경: **`eventTypes` 가 이 작업 중 두 차례 조용히 드리프트했다** — 둘 다 #381 이 만든 변경이
아니라 다른 레인이 SSE 에 이벤트를 추가한 뒤 `expected_behavior.jsonl` 이 재생성되지 않아 뒤늦게
드러난 사전 드리프트다. `test_ci_cases_execute_and_defined_cases_match_contract` 는 `eventTypes`
를 통째로 대조하지 않아(개별 필드만 봄) 둘 다 테스트를 깨지 않고 조용히 통과했다.

1. **1차(이 브랜치 분기 시점, base 798f0a9)** — 모든 ci 20건(아래 표)의 `eventTypes` 맨 앞에
   `progress` 이벤트가 하나 새로 등장했다(`git stash` 로 재실행해 base 커밋에서 이미 나옴을 확인).
2. **2차(리뷰 라운드 2, `dev` 병합 커밋 `adb9db0` — #396 2단계 "구매자 progress 다회 emit +
   stage 어휘 7종 확장")** — recommend 파이프라인을 타는 **11건**(`combo-0023·0026·0031·0035·
   0036·0037·0038·0039·0053·0054·0055`)에서 `eventTypes` 안 `progress` 이벤트가 단일 emit에서
   **다단계 emit**(호출당 여러 번, stage 어휘도 늘어남)으로 다시 바뀌었다(예: combo-0031
   `[progress,conditions,token,done]` → `[progress,conditions,progress,token,done]`). 나머지
   9건(담기/조회/찜/주문조회, 아래 표 첫 행)은 recommend 파이프라인을 안 타 영향이 없었다.

두 차례 다 `refresh_observed(write=False)` 실측으로 **`eventTypes` 하나만** 바뀌고 나머지 필드
(`terminal`·`finishReason`·`errorCode`·`actionType`·`pushCount`·`listType`·`searchFilters` 등
핵심 계약 필드)는 전부 불변임을 확인했다 — 판정은 둘 다 **"필드 추가/이벤트 추가 — 회귀 아님"**.
이 표에서는 두 드리프트를 "필드 추가" 판정에 흡수하고 케이스별로 반복 서술하지 않는다.

| case_id | 무엇이 바뀌었나 | 판정 | 근거 |
|---|---|---|---|
| combo-0004·0005·0010·0016·0019·0042·0047·0056·0057 (담기/조회/찜/주문조회 9건) | `progress` 이벤트 + 신규 필드(`pushProductCount:0`·`searchCallCount:0`·`searchFilters:null`·`unappliedSearchFilters:[]`) | **필드 추가** | intent 가 recommend 가 아니라 search 콜러블이 애초에 안 불린다 — 핵심 계약 필드(`terminal`·`actionType`·`actionReason`·`lastTokenText`)는 전부 불변. `searchCallCount:0`/`searchFilters:null` 은 "안 불렸다"를 처음으로 데이터에 명시한 것(D3). |
| combo-0035·0036·0037·0038·0039 (constraint_strength=unspecified 5건) | `progress` 이벤트 + `pushProductCount`(실측값)·`searchCallCount:0`·`searchFilters:null`·`unappliedSearchFilters:[]` 추가 | **필드 추가** | popular_fn(I-3) 우선이라 search 자체가 안 불린다(코드 정의 우선순위, 갭 아님 — 기존 note 그대로). `pushCount`(이벤트 수)는 불변, `pushProductCount` 는 처음 관측된 값. |
| combo-0023 (price_min, embedding_missing) | `progress` 이벤트 + `pushProductCount:4`·`searchCallCount:1`·`searchFilters`(`priceMin:20000`, 나머지 null)·`unappliedSearchFilters:[]` 추가 | **필드 추가 + 실측 개선(경계값 최초 기록)** | `PAIR_CATALOG` 4건이 `price_min=20000` 을 전부 만족(D4 근거 숫자와 동일 계산) — 필터가 있어도 이 표본에선 안 줄어드는 사례를 데이터로 처음 확인. `terminal`/`pushCount`/`listType` 은 불변. |
| combo-0031 (overspecified_zero) | `progress` 이벤트 + `pushProductCount:0`·`searchCallCount:1`·`searchFilters`(`priceMin:20000`)·`unappliedSearchFilters:[]` 추가 | **필드 추가 + 실측 개선** | 0건 주입(`RecordingFilteringSearch(products=[])`)이 경계 도달 filters 도 함께 기록하게 됐다(D2) — `finishReason=zero_result`·`pushCount=0` 은 불변. |
| combo-0026·0054·0055 (필터 8축 전부 present, degrade rerank_failed/embedding_missing) | `progress`+`suggestions` 이벤트 추가, `searchCallCount:1→5`(0055 는 신규), `searchFilters`(`keyword:null` — #51 규칙으로 leg 검색어에서 drop, `color`/`attrConditions` 는 present 유지), `unappliedSearchFilters:["color","attrConditions"]`, `pushProductCount`(실측값 1) 추가 | **실측 개선 — #371 잔여 맹점(category 축 미도달) 해소(D5)** | 예전엔 `category` 축이 항상 canonical-or-null degrade 로 `None` 지워져 leg 가 안 생겼다 — D5 로 leg 가 1개 생기면서 **완화 칩 estCount probe**(주검색 1 + 칩 probe 4 = 5회, `relaxation_max_probes` 기본값)가 처음으로 실제 실행됐다 — **자동완화 루프가 아니다**(#425 재판정, 아래 참조): 결과가 1건(`not candidates` False)이라 자동완화 루프 자체는 안 돈다. `terminal=done`·`pushCount=1`·`listType=PICK_ONE` 은 불변(핵심 계약 유지) — combo-0055 는 특히 `pair_runner` 쪽 INV 검증과 짝을 이룬다(아래 참조, D1 로 양쪽 arm 이 `error`→`done` 으로 바뀜). |
| combo-0053 (category+rating_min, embedding_missing) | `progress`+`suggestions` 이벤트 추가, `searchCallCount:0→2`, `searchFilters`(`category:무선이어폰`·`ratingMin:4.0`) 신규, `pushProductCount:2` | **실측 개선 — #371 잔여 맹점 해소(D5), 처음으로 search 를 탄다** | 배경(패킷 §"이미 실측한 사실") 대로 category 축을 실현하니 비로소 search 경계에 도달했다 — 예전엔 `category` 지워짐 → 하드필터가 `rating_min` 뿐 → `#393` 최소 필터 가드가 이 턴을 I-3(인기)로 돌려 search 자체가 안 불렸다. `searchCallCount:2` 는 **완화 칩 estCount probe**(주검색 1 + 칩 probe 1, 완화 후보가 `ratingMin` 1개뿐) 다 — 결과 2건 < `relaxation_min_results`(3)라 칩 probe 블록이 돌지만, 자동완화 루프는 `not candidates`(0건)가 아니라서 안 돈다(#425 재판정). `terminal=done`·`listType=PICK_ONE` 은 불변. |

위 표의 "progress"/"progress+suggestions 이벤트" 서술은 1차 드리프트(단일 emit) 기준이다 —
combo-0023·0026·0031·0035~0039·0053~0055(11건)는 리뷰 라운드 2 에서 2차 드리프트(다단계 emit)로
`eventTypes` 가 한 번 더 갱신됐다(위 공통 배경 참조) — 다른 필드·판정은 그대로다.

3. **3차(#425 판정, 2026-08-07)** — combo-0031 의 `notes` 에 "0건은 주입이며, 이 케이스에 present
   인 필터축(price_min)이 완화 축이 아니라 자동완화·완화칩은 돌지 않는다(#425 판정: 정의된 동작)"
   1건을 추가했다(`refresh-observed` 재실행, 「알려진 관측 한계」`overspecified_zero` 항목 참조).
   `notes` 는 `OBSERVED_GUARDED_FIELDS`(#424 드리프트 가드, 아래 절) 제외 필드라 이 재생성으로도
   가드는 계속 통과한다 — 실측으로 경계 설계가 맞음을 확인했다. 다른 행·필드는 바뀌지 않았다.
   같은 날 리뷰 라운드 2 로 combo-0031 행의 `expected` 서술도 실측에 맞게 정정했다(evidence 에
   `relaxation.py::FIELD_TO_ATTR` 1건 추가) — **실측(`observed`)이 있고 그 실측과 어긋나는 행만**
   정정 대상이다. 나머지 `overspecified_zero` 행(combo-0030·0032·0033·0034)은 전부
   `observation_mode=manual`(`observed` null)이라 실측 근거가 없고, present 필터축도 완화 축
   (brand·color 등)이라 그 서술이 틀렸다고 말할 근거가 없어 건드리지 않았다.

**드리프트 가드(#424)**: 위에서 두 차례 확인했듯 `observed` 는 다른 레인이 SSE 이벤트를 바꿀 때마다
조용히 낡는데, 그동안은 커밋된 값과 재실행 값을 대조하는 가드가 없어서 아무도 몰랐다 —
`refresh-observed` 로 손으로 재생성해야만 드러났다. `test_observed_guarded_fields_match_recomputed_
values_for_all_ci_rows`(`tests/eval/test_combo_matrix_eval.py`)가 이 가드다: PR 에서 매번
`refresh_observed(write=False)` 를 재실행해 커밋본과 딕셔너리째(키 존재 여부 포함) 대조한다.

전량 byte diff 가 아니라 **핵심 계약 필드만** 고른 이유는 SSE 를 건드리는 모든 레인(동시 6~8개)이
이 eval 데이터 재생성을 강제당하는 레인 결합 비용 때문이다 — 실측상 두 차례 드리프트가 전부
`eventTypes` 하나였고, 이벤트 추가는 다른 레인의 정상 작업이다. 필드 경계(`OBSERVED_GUARDED_FIELDS`,
`evals/combo_matrix/schema.py`):

- **포함**(바뀌면 파이프라인 동작이 실제로 바뀐 것): `terminal`·`finishReason`·`errorCode`·
  `actionType`·`actionReason`(SSE 종료/오류/액션 계약), `pushCount`·`pushProductCount`·`listType`
  (push 결과 형태), `searchCallCount`·`searchFilters`·`unappliedSearchFilters`(검색 경계 도달값,
  #381), `unhandledException`(안전망 dict 낙성 회귀), `outcome`·`itemCount`·`exception`·
  `statusCode`(HOME 계약), `profileHookInvoked`·`buildReasonsInvoked`·`reasonsFilledCount`·
  `reasonsNull`(HOME 계측).
- **제외**(다른 레인의 정상 작업이라 대조하면 소음): `eventTypes`(SSE 이벤트 추가, 실측 2회 드리프트
  전부 이 필드), `lastTokenText`(문구, 계약 아님), `notes`/`note`(관측 한계 서술).

**행 범위**: `status` 와 무관하게 `observed` 가 있는 모든 ci 행(partial 인 combo-0038 포함) — 이건
기록 신선도 검사이지 미정의 동작의 스펙화가 아니다. `expected`·`status`·`undefined_tuple` 은 이
가드가 보지 않는다.

**깨졌을 때 조치**: `uv run python -m evals.combo_matrix refresh-observed` 로 갱신 → 위 표에 행을
추가해 무엇이 왜 바뀌었는지(실측 개선/회귀/필드 추가) 판정을 남긴다.

변이 시험으로 경계가 실제로 작동함을 확인했다(2026-08-07): combo-0031 의 `finishReason` 을
`zero_result`→`stop` 로 바꾸면 가드가 깨지고(핵심 계약 필드), `eventTypes` 맨 앞 이벤트를 지우면
가드는 그대로 통과한다(제외 필드) — 둘 다 원복 후 확인.

## 재생성 이력 (#386 — intent 축에 `wishlist_view` 추가)

`RouteDecision.intent` Literal 에 값이 하나 늘면
`test_intent_axis_matches_route_decision_literal` 이 즉시 깨진다(그러라고 있는 가드다) — 그래서
이 이슈는 매트릭스를 함께 재생성했다. **케이스 번호가 대거 밀렸다**: 57 → 61건, 축 조합이 그대로
유지된 것은 18건뿐이고 나머지는 재배치됐다(greedy pairwise 가 pair 우주 전체를 다시 보기 때문에,
조합을 아무리 좁혀도 이 재배치는 피할 수 없다 — 실측으로 확인). 위 "#381" 절의 case id 들은 **그
시점 기준**이며 이 재생성 이후와 대응하지 않는다.

| 옮긴 것 | 구 → 신 | 근거 |
|---|---|---|
| 필터 8축 전부 present 인 ci 케이스(D8 테스트 3종) | combo-0026 → **combo-0058** | 축 조합 동일(recommend·guest·rerank_failed·case=3·normal), 관측값도 동일 |
| 표현 가능 축만 present 인 ci 케이스 | combo-0023 → **combo-0057** | `unappliedSearchFilters == []` 를 재는 자리 |
| DIR 쌍(하드필터 추가 → 결과 비증가) | combo-0053 → **combo-0056** | 흔드는 축이 `category` → `price_min` 으로 바뀜 |
| DIR 쌍(identity, manual) | combo-0054 → **combo-0057** | recall 은 goldenset 소관이라는 분리 그대로 |
| INV 쌍(degrade none→rerank_failed) | combo-0055 → **combo-0058** | 불변식 그대로. `invariant_fields` 는 #426 로 한 항목 교체(`unappliedSearchFilters` → `attrConditionsPostFilter`, 아래 프로젝션 표) |

번호에 의존하던 테스트 2개(`test_combo_0053_*`·`test_combo_0054_*`)는 **spec 의 성격으로 찾도록**
고쳤다(`kind`·`metric`·`mode` 기준) — 재생성마다 번호를 따라다니며 고치는 일을 여기서 끝낸다.

**대역 카탈로그에 1건(product 105, 19000원 `무선이어폰`)을 더했다.** DIR 쌍이 흔드는 축이
`price_min` 으로 바뀌었는데 기존 4건이 전부 3만원 이상이라 필터를 태워도 `base=3 · perturbed=3` 로
**쌍이 공허해졌다** — `test_hard_filter_pair_fixture_actually_narrows` 가 그걸 잡았고, #371 이
`category` 대조군(104)을 넣은 것과 같은 이유·같은 해법으로 해소했다.

**`get_wishlist`(I-28) 몽키패치를 추가했다** — 조회 계열은 실패 주입 때만이 아니라 **늘** 패치한다.
담기 계열과 달리 정상 케이스에서도 호출되는데, 패치가 없으면 로컬에 Spring 이 없을 때 실 네트워크
호출이 실패해 `degrade=none` 케이스가 degrade 를 관측한다(결과가 환경에 따라 뒤집힌다). 이번
재생성으로 `wishlist_remove` 가 ci·`degrade=none` 조합을 갖게 되면서 이 공백이 드러났다.
예외 타입은 실 어댑터 규약대로 `SpringUnavailableError` 다(담기 계열의 `WishlistError` 가 아니다 —
#376 이 고친 바로 그 실수).

## 관측 러너가 안 쓰는 것

`observation_mode=manual`(context≠none, 34건)은 실행하지 않는다 — 멀티턴 승계(`categoryPrior`→
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
| `unappliedSearchFilters` | 프로젝션엔 포함, **combo-0058 `invariant_fields` 에선 제외**(#426) | F3(리뷰 라운드 1)가 이 필드를 INV 로 잠근 취지는 "두 arm 에서 **실제로 측정된 축**이 갈리면 안 된다"였고, 당시엔 양쪽 다 `['color','attrConditions']` 라 비공허했다. #426 로 8축이 전부 적용되면서 양쪽 다 `[]` 로 수렴 — 빈 리스트끼리 비교하는 **공허한 불변식**이 된다(F3 가 막으려던 바로 그 상태, `docs/lessons.md`). 그래서 그 자리를 아래 `attrConditionsPostFilter` 로 **교체**했다. 프로젝션 필드 자체는 남긴다(미래에 표현 불가 축이 생기면 다시 채워지는 자리) |
| `attrConditionsPostFilter` | 포함(#426, `unappliedSearchFilters` 대체) | `attr_conditions` 는 Spring payload 축이 아니라 AI 사후필터(`search_service.apply_ai_side_filters`)라, `searchFilters` 에 값이 실려 있어도 **적용 여부를 알 수 없다** — 사후필터 호출 자체를 계측해(`{invoked, inputCount, outputCount}`) 두 arm 에서 그 축의 평가가 갈리지 않음을 잠근다. 실측 양쪽 다 `{invoked: True, inputCount: 1, outputCount: 1}` 로 **비공허**하다. `runner.py::_observe_chat` 와 같은 스파이·같은 규약(첫 호출 기준) |
| `legs` | 포함(대부분 `[]`) | category 축이 present 인 쌍만 `runner.py`·`pair_runner.py` 공유 seam(아래, #381 D5 로 흡수)으로 1-leg 를 채운다 — 그 외에는 항상 `[]`(leg 분해 커버리지는 #331 소관) |
| `listType`/`listsCount`/`perListProductCount`/`productIdsMultiset` | 포함 | push 계약 형태(구조) |
| `listEntryFieldKeys` | 포함 | 엔트리마다 **값이 실제로 채워진**(`None`/`[]`/`{}`/`""` 가 아닌) 필드 키만 담는다(`_entry_field_keys`, 리뷰 R2 F1) — `model_dump().keys()` 그대로 쓰면 pydantic 이 값과 무관하게 스키마 전 필드를 항상 담아 `['label','listId','productIds','reasons']` 상수가 되고, 그러면 이 필드는 **어떤 회귀에도 깨질 수 없다**(검증하지 않는 검증). `reasons` 는 값이 채워져 있어도 이 집합에서 **명시적으로 뺀다** — 아래 행과 같은 이유(정의된 동작)로, 값 자체를 비교 안 하는 것뿐 아니라 "채워졌는지" 여부도 비교하지 않는다. |
| `pushProductCount` | 포함 | DIR metric `push_product_count` 의 원천 |
| token 텍스트·이벤트 개수/순서 | 제외 | 문구는 계약이 아니고, 스트리밍 분할은 비계약 |
| `reasons` **값**(rationale 문구) | 제외 | rerank 폴백 시 소실이 정의된 동작(단, combo-0055 실측은 productIds 멀티셋 자체는 동일 — 아래 참조). `listEntryFieldKeys` 도 이 필드의 **키 존재 여부조차** 안 본다(위 행) — rerank 성공(2건)·폴백([]) 양쪽 다 이 키를 빼므로 어느 쪽이든 결과가 같다. |
| suggestions 칩 상세 | 제외 | 범위 밖 |

### DIR: metric·guards

`metric="push_product_count"` 는 perturbed/base 각각의 push 총 상품 수. `direction` 은
`non_increase`\|`non_decrease`. **guard 실패는 방향 부등식이 성립해도 FAIL**(공허 통과 방지,
이 이슈의 심사 포인트):

- `perturbed_filters_strict_superset` — perturbed 의 `searchFilters`(present 항목만)가 base 의
  **진상위집합**인지. 아니면 "필터를 늘렸다"는 전제 자체가 거짓이라 방향 부등식이 무의미하다.
- `base_count_positive` — base 의 metric 값 > 0. 0이면 `non_increase` 가 항상 공허하게 성립한다.

### 카테고리 seam (combo-0053, 이슈 #371 R1 결정 → #381 D5 로 `runner.py` 본체에 흡수)

combo-0053(DIR — "필터 추가 → 결과 수 비증가")는 `category` 필터축을 검증 대상으로 삼는데, #371
R1 실측 당시엔 `category` 축이 이 하네스 전체에서 legs 를 거치지 않으면 검색에 도달한 적이 없었다
(위 "알려진 관측 한계" 절 참조). 그때는 `pair_runner.py` 전용 `_pair_decompose_json()` 래퍼로
`categoryQueries` 를 채우고 `fakes.make_exact_match_category_mapping()`(raw exact match 만 대역 —
거리컷·택일·확장은 #331 소관, 재구현 아님)으로 legs 를 채웠다. **#381 D5 로 이 seam 이
`runner.py::build_decompose_json` 본체에 흡수됐다** — `pair_runner.py` 는 이제 `_pair_decompose_
json()` 없이 `build_decompose_json()` 을 직접 쓰고, `_observe_chat` 도 같은
`make_exact_match_category_mapping()` 을 쓴다(§ "관측 재생성 이력" 절). 두 러너가 같은 seam 을
공유하므로 이 절의 실측(아래)은 `runner.py` 쪽 관측(combo-0053, combo-0026·0054·0055)과도
일관된다.

`PAIR_CATALOG`(`fakes.py`) 는 `CATALOG_PRODUCTS` 3건(`무선이어폰`) + 1건(`여행용품`, product_id
104) + 1건(`무선이어폰`, 3만원 미만, product_id 105) — **하드필터가 실제로 결과를 줄이도록**
대조군을 둔다. 104 는 `category` 축(#371), 105 는 `price_min` 축(#386) 담당이다: #386 재생성으로
DIR 쌍이 흔드는 축이 `category` 에서 `price_min` 으로 바뀌었는데 기존 4건 가격이 전부 3만원
이상이라 `price_min=30000` 을 태워도 4/4 가 통과해 쌍이 공허해졌다(base=3·perturbed=3 실측 →
`test_hard_filter_pair_fixture_actually_narrows` 가 잡았다). 105 의 `brand`·`rating` 을 일부러
다르게 둔 것은 "필터 8축 전부 present 면 product 101 하나만 남는다"는 기존 전제를 지키기 위해서다.

**실행 경로 비대칭(리뷰 R2 F3)**: combo-0053 의 base(`legs=[]`)와 perturbed(`legs=[('무선이어폰',
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

### manual 분리: combo-0054

"회원 recall ≥ 게스트 recall"(#119 선례, DIR)은 `mode=manual` 이다 — recall 은 정답(relevance)
라벨이 있어야 계산되는데, 고정 fixture 검색 대역은 회원/게스트 산출이 항상 동일해 방향 관측이
**원리적으로 불가**하다(라벨이 없으니 CI 로 못 재는 게 아니라, 방향 자체가 안 갈린다). 절대
기준·표본은 `evals/goldenset`(#333) 소관 — `link` 필드가 가리킨다. manual 쌍은 실행 없이
`status="manual"` 로 결과·`PAIR_CHECKS.md` 분모에 그대로 들어간다(조용한 탈락 금지, 규약 8항).

### 필터링 검색 대역의 성격

`fakes.make_recording_filtering_search`(→ `RecordingFilteringSearch`)는 **Spring I-1 검색(외부
시스템)의 WHERE 계약 대역**이지 앱 판정 로직 재구현이 아니다.

**#426 로 대역이 서는 자리를 한 층 내렸다** — `run_buyer_turn(search=...)`(= `search_catalog` 를
통째로 대체)에서 `search_catalog(backend=...)`(= `SearchBackend`, 실제 네트워크 경계)로. 이유:

- 예전 자리에서는 `search_catalog` 안의 dedup·`rating_min`·`attr_conditions` 단계가 **하네스에서
  한 번도 실행되지 않았다.** 그 판정이 망가져도 관측이 1비트도 안 변하는 검증 사각지대였고,
  이슈가 지시한 "사후필터 호출 계측"도 그 상태로는 항상 `false` 상수가 된다.
- 없어진 단계를 대역이 손으로 메꾸다 보니 `rating_min` 을 앱과 **다른 의미로** 재구현해 뒀다
  (대역 `rating is not None and rating >= min` vs 앱 `rating is None or review_count == 0 or
  rating >= threshold` — "반증된 것만 제거", 무평점 신상품 처리가 반대다). 게다가 `rating_min` 은
  애초에 Spring payload 축이 아니라 대역이 Spring 인 척 적용하던 축이었다. 자리를 내리면서 이
  코드는 고쳐진 게 아니라 **삭제**됐다.
- 이 패턴은 새로 만든 게 아니다 — `evals/filter_axes/probe.py::LocalCatalogSearchBackend`
  ("app 코드 무수정")가 같은 자리에 서서 `search_catalog(backend=...)` 를 그대로 호출한다.
  두 하네스가 이제 같은 규약을 쓴다.
- `fakes.py` 모듈 docstring 의 자기 서술("네트워크로 나가는 콜러블의 얇은 대역뿐")과도 이제
  실제가 일치한다 — `search_catalog` 는 네트워크로 나가는 콜러블이 아니라 AI 내부 함수다.

표현 불가 필터(keyword·color·attr_conditions)가 present 로 들어오면 **#381 D1 이전엔** 조용히
무시하지 않고 `ValueError` 로 즉시 실패시켰다 — 그런데 그 예외가 앱의 검색 실패 처리에 삼켜져
`terminal=error`/`errorCode=SEARCH_FAILED` 로 낙성했고, combo-0055 INV 쌍은 **base·perturbed
양쪽 다 이 상태로 우연히 "동일"해 pass 했다**(공허 통과 — "필터가 적용된 것처럼"이 아니라 "둘 다
실패한 것처럼" 통과하는, 예상 못 한 반대 방향의 공허였다). D1 은 이걸 던지지 않고
**미적용으로 기록**(`unapplied_calls`)한 뒤 표현 가능한 축만 적용해 계속하도록 고쳤다 — 그 결과
combo-0055 의 두 arm 은 이제 `terminal=done` 으로 정상 종료해 INV 불변식이 실제 의미로 성립한다
(§ 결과 절, `pair_checks.jsonl` combo-0055 `reason` 갱신 참조).

### #393 최소 필터 가드와의 축 분리 (`pair_runner.py` 실행 한정)

이 러너는 *"Spring I-1 WHERE 계약 대역"* 으로 **필터 배관**(하드필터가 실제로 `search` 콜러블에
도달하는가)을 잰다. #393 의 최소 필터 가드(`search_filter_guard_enabled`, `search_guard.
is_unfiltered_payload`)는 아예 다른 축이다 — "이번 턴이 Spring 파라미터 0개로 나가는가"를 보고
그렇다면 **후보 소스 자체를 I-3(인기 상품)로 바꾼다**(운영 실측 7.74초·12.3MB 무필터 응답 방지).
가드를 켜 두면 category/keyword/brand/color/price 가 전부 absent 인 base arm(예: combo-0022,
`rating_min`·`total_budget` 만 present)이 `search` 에 아예 도달하지 못해 `_execute` 의 "search
콜러블이 호출되지 않았다" 로 `UnsupportedPairAxes` 가 나 그 축의 필터 배관을 잴 수 없게 된다 —
그 turn 은 오늘 실제로 무필터 I-1 을 부르는 turn 이라(#393 이 고치는 바로 그 경우) 이 v1
하네스가 재려는 "필터가 배관을 타는가"라는 질문 자체가 성립하지 않는다.

그래서 `pair_runner._execute` **실행 한정으로만** `search_filter_guard_enabled=False` 로 둔다 —
케이스 정의(`combo_cases.jsonl`)·축 할당·기대값(`expected/*.jsonl`)은 건드리지 않았고,
`runner.py`(기존 55건 MFT 경로)도 무관하다. **가드 자체의 회귀는 이 하네스가 지키지 않는다** —
#393 전용 단위/통합 테스트(`tests/unit/test_search_guard_393.py`·`tests/unit/test_recommendation.py`
의 `test_unfiltered_bypass_*`·`test_category_mapping_dropped_*` 계열)가 그 몫을 진다.

### 결과 (2026-08-07 기준, #426 반영 후 `pair_runner.py` 재실측)

INV 통과 1/1 · DIR(ci) 통과 1/1 · manual 분리 1건 · 분모(전체 쌍 행 수) 3(verdict 는 #381 D1/D5 ·
#386 재생성 · #426 전후로 모두 유지됨). DIR 쌍 combo-0056 은 `price_min` 을 더해
base=4(101·102·103·105) → perturbed=3(101·102·103) 로 엄격 감소가 실제로 관측됐다 — #426 로
대역이 `SearchBackend` 자리로 내려가고 픽스처에 `summary`·`attributes` 가 채워진 뒤에도 이 숫자는
그대로다(category·price_min 만 present 라 새로 재지는 축이 관여하지 않는다).

INV 쌍 combo-0058 은 `invariant_fields` 에서 `unappliedSearchFilters` 를
`attrConditionsPostFilter` 로 **교체**했다 — 전자는 #426 이후 양쪽 arm 다 `[]` 로 수렴해 빈
리스트끼리 비교하는 공허한 불변식이 되고(F3 가 막으려던 바로 그 상태), 후자는 양쪽 실측이
`{invoked: True, inputCount: 1, outputCount: 1}` 로 비공허하다(§ 위 프로젝션 필드 표).

combo-0055 은 D1 이전엔 base·perturbed 둘 다 `terminal=error`(`errorCode=SEARCH_FAILED`, 표현
불가 필터 present 로 대역이 `ValueError` 를 던지고 그게 앱의 검색 실패 처리에 삼켜짐)로 **우연히
동일해 pass 하는 공허 통과**였다(실측, `git stash` 로 D1 이전 상태를 재실행해 확인 — 두 arm 모두
`productIdsMultiset: []`·`pushProductCount: 0`). 그 상태에서 커밋돼 있던 `pair_checks.jsonl` 의
`reason` 문구는 `productIds` 멀티셋이 "둘 다 `[101,102,103,104]`"라고 적고 있었는데, 이는 그
당시 실측과도 어긋나는 드리프트였다(원인은 추적하지 않았다 — 문구 자체가 실측 근거 없이 남아
있었다는 사실이 이 이슈가 잡으려던 문제의 정확한 예시). D1(표현 불가 필터를 미적용으로 기록하고
계속) + D5(category leg 실현) 적용 후 재실측하면 base·perturbed 둘 다 `terminal=done`·
`productIdsMultiset=[101]` 로 **정상 종료 상태에서 진짜로 동일**하다 — INV 불변식이 이제 의미
있게 성립한다(§ "필터링 검색 대역의 성격" 절). 상세는 `PAIR_CHECKS.md`, `pair_checks.jsonl` 의
combo-0055 `reason` 문구도 이 실측에 맞게 정정했다.
