# 구매자 골든셋 작성·검수·변경 가이드

## 케이스 작성

1. 실제 실패 가설과 slice를 먼저 정하고 안정적인 `caseId`를 발급한다.
2. 원문 발화를 그대로 keyword로 쓰지 않고 decompose 정답인 `expectedFilters`를 먼저 확정한다.
   `snapshot.record_snapshots()`에는 이 필터 사전을 그대로 넘겨
   `app.services.spring_client.search_products`를 통해 라이브 I-1을 호출하게 한다. failure
   slice에서 원문 keyword의 0건을 증명할 때만 예외로 하며 notes에 의도를 적는다. 직접 HTTP
   호출이나 인증 토큰 기록은 금지한다.
3. 응답 순서의 상품명·카테고리·가격·설명과 필요 시 pg-catalog `search_doc`을 읽는다.
4. 관련도는 3=질의의 명확한 정답, 2=허용 가능, 1=주변부, 0=오답으로 판정한다. 근거가
   설명만으로 부족하면 정답에서 제외한다.
5. `notes`에 해당 등급과 금지 결과의 이유를 한국어 1~3줄로 남긴다.
6. `schema.validate_cases()`와 `audit.run_audit()`를 실행한다.

`expectedFilters.category`와 `hardConstraints.forbiddenCategories`는 Spring I-1 응답의
`categoryName` 표기(예: `돼지고기`, `축구`)를 쓴다. pg-catalog
`product_document.category`의 `대분류 > 소분류` 표기(예: `축산 > 돼지고기`)를 옮기지 않는다.
금지 카테고리는 `catalog_snapshot.json`의 실제 `categoryName`에 존재해야 한다.

**관측(2026-08-05, #333 Part 2 라운드2 §7-1)**: 라이브 Spring I-1의 `categoryName`이 일부
카테고리에서 `대분류 > 소분류` 계층 표기로 돌아온다(예: `"당뇨관리용품 > 침/바늘"`,
`"구기/라켓/스포츠 > 축구"`) — 이 절이 문서화한 "평탄 표기만" 전제와 다르다. `schema.py`의
`_categories_use_i1_notation` validator는 여전히 `" > "`를 포함한 `forbiddenCategories`를
거부하므로(이번 PR에서 바꾸지 않는다 — 계약 판단은 후속 이슈), 이런 카테고리는
`forbiddenCategories`로 지정할 수 없다. 영향받는 케이스는 `forbiddenCategories`를 비우고
`mustExcludeProductIds`로 개별 상품을 금지하는 워크어라운드를 쓴다(예:
`buy-cmap-0005`/`buy-cmap-0006`/`buy-over-0002`, `CHANGELOG.md` 2026-08-05 항목 참조).

I-19가 없는 현재 구매 이력은 합성 페르소나다. 다만 상품 필드는
`catalog_snapshot.json`에서 그대로 복사한다. `orderedAt`은 실행 시각으로 만들지 않고 기준일
`2026-08-02`에 대한 절대 ISO-8601 문자열로 고정한다. 그래야 90일 윈도우가 재실행마다
달라지지 않는다.

## 사람 검수

- `labeler`는 응답과 설명을 읽어 최초 등급·제약·notes를 작성한다.
- 별도 `adjudicator`는 상품 실재, 3등급 근거, 0/1/2 경계, 하드제약, 금지 결과, split 누출을
  독립 확인한 뒤 자기 가명 ID를 기록한다.
- 체크리스트: 실제 I-1 응답인가, 애매한 상품이 3등급인가, 모든 라벨 ID가 스냅샷에 있는가,
  가격제약을 정답이 스스로 위반하는가, persona/fixture/query가 split 사이에 재사용됐는가.
- v1의 `adjudicator`는 비어 있다. 현재 라벨은 구현자가 붙인 자동 초안이며 사람 검수 완료
  상태가 아니다.

## 변경

케이스 추가·삭제·재라벨은 반드시 다음 순서를 지킨다.

1. `CHANGELOG.md`에 `caseId`와 사유를 `add`/`remove`/`relabel`로 기록한다.
2. 모든 케이스와 manifest의 `datasetVersion`을 올린다.
3. 전체 대상 파일 SHA-256과 `datasetHash`를 다시 계산한다.
4. 누출 감사와 baseline 평가를 다시 실행한다.

**서로 다른 `datasetHash`에서 나온 점수는 직접 비교하지 않는다.**

## sealed holdout

- 튜닝 중 `buyer_holdout_labels.jsonl`을 열거나 import하지 않는다.
- 공개 release 평가 경로는 후보 commit에서만
  `unseal_holdout_labels(reason="<실행 사유>", commit_sha="<40자리 SHA>")`를 호출한다.
  호출은 dataset hash와 시각을 `audit/holdout_runs.jsonl`에 append한다.
- 누출 감사에는 라벨이 필요한 비공개 `_load_labeled_holdout_for_audit()` 접근자가 하나 있다.
  `evals/goldenset/audit.py`만 이를 호출하며 튜닝·평가 코드에서 직접 호출하지 않는다.
- 이후 라벨을 release 실행 결과와 대조하고 감사 로그를 보관한다.
- `.github/workflows/` 배선은 사람 승인 게이트이므로 이 이슈에서 만들지 않았다. 현재 release
  절차는 위 수동 봉인 해제 API와 기록을 코드 수준 강제로 사용한다.

## #32 검색 비교

#32의 query→relevant productId 원천은 이 데이터셋의 `search` slice뿐이다.
`loader.to_compare_golden_cases()`로 기존 `GoldenCase` 하니스에 연결하며 중복 데이터셋 파일을
추가하지 않는다.

후보가 전부 정답인 케이스는 어떤 순위도 만점이므로 순위 품질을 측정하지 못한다. 이 케이스는
노출·필터·하드제약 검증에만 사용하고, #143 평가기는
`audit/leakage_report.json`의 `nonDiscriminativeRankingCases`를 읽어
nDCG·MRR·Precision@k 분모에서 제외해야 한다.

## v2(#333) — 후보 깊이·하드 네거티브·슬라이스 쿼터·INV/DIR

v1은 순위를 판별하지 못했다(#333 배경 — 판별 유효 18건 중 9건이 후보 ≤10, 하드 네거티브
0건). v2는 구조로 이를 고친다. 데이터 채움(실제 후보 패딩·하드 네거티브 라벨링·adjudicator
검수)은 Part 2가 하고, 이 절은 Part 1이 고정한 규약이다. 설계 근거는 이슈 #329와
`docs/research/RESEARCH-EVAL-329.md`(작성 시점 기준 머지 대기, 사본
`research-eval-329.md`)를 인용한다 — cutoff·후보 깊이·하드 네거티브 채널·슬라이스 표본
산정식·다중비교 통제 항목이 그 문서 §8 권고표에서 왔다.

### 후보 깊이 규약

- 목표(`goldenset_target_candidates`) 30건, 하한(`goldenset_min_ranking_candidates`) 20건.
- 순위 평가 대상(`testType=MFT` + `search` slice + `relevantProductIds` 비어있지 않음 +
  후보 전부정답 아님) 케이스가 20건 미만이면 `schema.validate_cases()`가 오류를 낸다.
  "전부정답"은 개수 비교가 아니라 **후보 집합이 정답 집합에 포함되는지**로 판정한다
  (`schema.all_candidates_are_correct` — #333 리뷰 F-1. 개수만 보면 후보에 오답이 섞여 있어도
  후보 수가 우연히 정답 수 이하이면 비판별로 오분류한다).
- 정말 후보가 그만큼뿐인 좁은 도메인(니치 카테고리 등)은 `notes`에 `narrow-domain:` 마커를
  붙여 예외 처리한다. Part 1 이관은 v1 후보를 그대로 두므로(패딩은 Part 2), 이관된 20건 중
  대부분이 이 표시를 달고 있다 — Part 2가 실제로 후보를 채우면 해제돼야 한다.
- **fixture candidate는 라벨된 id만이 아니라 전원 catalog_snapshot.json에 실존해야 한다**
  (`schema.validate_cases` — #333 리뷰 F-2). catalog에 없는 productId는 가격 HCV·diversity·
  라벨 워크시트 계산이 전부 불가능하다. 채우는 순서는 항상 "완화 검색으로 catalog를 먼저
  넓힌 뒤 주입한다" — 아래 채우는 절차 참조.
- **등급≥1(정답) 후보 비율 상한**(`goldenset_max_relevant_ratio`, 기본 0.25 — #329 권고 3, v1
  평균 0.389)도 순위 평가 대상 케이스에 적용한다. 초과하면 오류이며, `notes`에
  `relevant-ratio-exempt:` 마커로 예외 처리한다. 두 예외 마커는 이어붙일 수 있다(예:
  `"narrow-domain: relevant-ratio-exempt: 근거..."`) — `schema.has_note_marker`가 접두부의
  마커 나열을 파싱한다(startswith가 아니다). **상한은 후보 구성 규약이지 명백한 정답을
  0으로 만드는 허가가 아니다** — 실제 정답인 후보(암묵 0으로 방치된 injected 포함)를 비율을
  맞추려고 0으로 남겨두지 말고, 정직하게 등급을 매긴 뒤 초과분은 exempt 마커로 예외 처리한다
  (#333 adjudication 라운드, adjudicator-omx-01 관찰).
- 채우는 절차: `snapshot.record_snapshots_v2()`로 골든 검색(limit 30) + 완화 검색
  (keyword-only/category-only, 후보<30일 때만) → `inject.build_case_candidates()`로 하드
  네거티브를 채운다(이때 catalog는 완화 검색으로 넓힌 뒤의 catalog_snapshot이다 — 주입 풀은
  실질적으로 catalog ∩ pgvector 이웃). 최종 productId 오름차순 평탄 목록이 no-op 기준선의
  정의다(시스템 노출 길이로 자른 뒤 — 아래 evals/metrics no-op 절 참조).

### 하드 네거티브 주입 규칙

`inject.py`가 판정 규칙 5종을 만든다(전부 SELECT/읽기 전용, pg-catalog 스키마 불변):

| rule | 근거 |
|---|---|
| `semantic_near` | 정답 상품 임베딩의 pgvector 최근접 이웃(정답·기존 후보 제외, catalog_snapshot에 없는 이웃은 제외), 거리·productId 오름차순 |
| `price_violation` | 같은 카테고리에서 `hardConstraints.priceMax`/`priceMin`을 위반 |
| `attr_violation` | 같은 카테고리에서 `expectedFilters.attrConditions`를 위반 |
| `other_brand` | 발화/필터가 브랜드를 지목한 케이스의 같은 카테고리 다른 브랜드 |
| `random_catalog` | catalog 전체에서 caseId 해시로 시드한 결정론 표본(#333 리뷰 F-5-2, #329 권고 3③) — 다른 채널이 전부 같은 카테고리·임베딩 근방에 몰려 인기 편향을 주입하는 것을 상쇄 |

목표 혼합비(주입분 기준, 사전 등록, #329 권고 3): `semantic_near` ≥ 50%, `attr_violation`+
`price_violation` ≈ 25%, `other_brand`+`broadened_search`(§후보 깊이 규약의 완화 검색)+
`random_catalog` ≈ 25%. 케이스 사정으로 못 맞추면 `fixtures/search_responses.json`의
candidates provenance(`source`/`rule`/`from`)에 실제 수가 정본으로 남는다 — 목표는 목표일
뿐이다.

**실측(2026-08-05, #333 Part 2 라운드2 §7-4)**: `semantic_near` 90.07% / `random_catalog`
9.93%, `attr_violation`/`price_violation`/`other_brand`는 0%에 가깝다 — 목표 대역(25%/25%)에
크게 못 미친다. 사유: 이 세 채널은 전부 `category`가 있어야 트리거되는데(`_same_category_products`
전제), 대다수 케이스가 `keyword` 검색만으로 충분해 `category`를 명시하지 않는다. 또한
`fetch_full_catalog_via_i17` 전량 스캔으로 F-2 catalog 커버리지가 크게 늘면서 `semantic_near`
채굴 수율이 다른 채널을 압도한다(v1 43건 재기록·신규 84건 전부 같은 경향). **데이터가
정본이므로 조작하지 않고 그대로 둔다** — 목표 혼합비에 맞추려 억지로 `category`를 넣거나
가짜 attr/brand 제약을 지어내지 않는다. 후속 이슈로 `attrConditions`/`targetBrands`를 쓰는
케이스를 더 늘리면 개선될 수 있다.

**갱신 실측(2026-08-06, #370 리뷰 라운드2 F-2)**: 위 문단은 #333 시점 관측이라 이 PR(#370
`inject_violation_negatives.py`의 `price_violation` 축 신설)이 그 수치를 낡게 만들었다 —
과거 문장은 이력으로 보존하고 여기 갱신치만 덧붙인다. injected 총계 1,435 → **1,482**
(+47), `semantic_near` 89.97% → **87.11%**, `random_catalog` 10.03% → **9.72%**,
`price_violation` 0% → **3.17%**(신설 47건 전부). "`price_violation`은 0%에 가깝다"는 문장은
전체 injected 채널 혼합비 기준으로는 더 이상 참이 아니다 — `manifest.json`의
`violationNegatives.rules.price_violation`(케이스 13/후보 47, §370 사전 등록 quota 대비
실채움)이 정본이다. `attr_violation`/`other_brand`는 이 PR 범위 밖이라 여전히 0%에 가깝다.

**injected 후보는 기본 0등급이다.** `relevantProductIds`에 넣으려면(라벨러가 실제로 관련
있다고 판단한 경우) `notes`에 `injected-relevant-approved:` 마커를 붙여 adjudicator 확인을
남겨야 한다 — 그렇지 않으면 `schema.validate_cases()`가 오류를 낸다.

### 슬라이스 쿼터(사전 등록, 순위 판별 dev 기준 목표 N)

| 슬라이스 | 목표 N | 슬라이스 | 목표 N |
|---|---:|---|---:|
| `guest` | 30 | `repurchase` | 8 |
| `member` | 30 | `category_mapping_failure`(overlay) | 8 |
| `single_need` | 24 | `personalization_overreach`(overlay) | 6 |
| `multi_constraint` | 12 | 비순위(0건 failure MFT) | ≥ 6 |
| `budget` | 12 | INV/DIR 그룹 | ≥ 18케이스 |

신원(guest/member)과 니즈(single_need/multi_constraint/budget/repurchase)는 서로 다른
축이다 — 니즈 슬라이스는 케이스당 정확히 1개(disjoint), `member`는 `identity.kind==member`와
거울(양방향 필수), `guest`도 마찬가지다. holdout은 24건(guest 12/member 12, 필수 슬라이스별
≥6)이 1차 목표다. 수치 근거는 issue-333(sd 0.402, 슬라이스당 30 ≈ ±0.14)이다.

**명확화(#333 라운드2 §7-2)**: 위 "필수 슬라이스별 ≥6"은 아래 confirmatory/exploratory
절의 dev 쪽 `confirmatory.confirmatorySlices`(guest/member/budget, N≥30 라벨링)와 **다른
개념**이다 — `audit.py`의 `required_holdout`(search/personalization/repurchase/
category_mapping_failure/failure) 각 슬라이스가 holdout에 최소 몇 건 있어야 하는지의 목표치
다. `manifest.sliceQuotas.holdout.requiredSlicesMinEach`(옛 이름 `confirmatorySlicesMin` —
혼동을 피하려 개명)가 이 값이다. `failure`는 이 `required_holdout` 집합에는 있지만 dev의
`confirmatorySlices`에는 없다 — 그래서 holdout `failure` 5/6 미달은 dev N≥30 confirmatory
사전 등록 위반이 **아니다**(#333 라운드2 판정 §7-2 승인, 조정 불필요).

### confirmatory/exploratory

primary confirmatory metric은 `overall.ndcgAtK.10` 1개뿐이다(다중비교 통제, #328). 슬라이스는
manifest `confirmatory.confirmatorySlices`(`guest`/`member`/`budget`, α 보정
`holm-bonferroni-3` — #329 권고 4·5, Holm–Bonferroni 절차)에 있고 랭킹 케이스 수(N) ≥ 30이면
`confirmatory`, 아니면 `exploratory`로 `evals/metrics`가 자동 라벨링해 산출물에 싣는다.
미등록 슬라이스나 N<30은 방향 탐색용으로만 읽는다. `ndcgAtK`의 3·5 cutoff는 항상 exploratory,
10만 primary다 — `results.json`의 `ndcgCutoffLabels`가 이를 데이터로도 표시한다(#329 권고 1).
순위 평가 케이스의 후보 수 분포(최소/중앙값/최대, 후보 ≤10 비율)도 `overall`·`report.md`에
함께 인쇄한다(#329 권고 2 관측 판정).

### INV/DIR 작성법(#328 CheckList 규약)

라벨 없이 규모를 늘리는 축이다. `testType`을 `INV`/`DIR`로, 같은 검사 단위를
`behaviorGroupId`로 묶고 `behaviorKind`를 지정한다. 라벨 필드(`relevantProductIds` 등)는
비워도 된다 — `search` slice라도 MFT의 "정답 필수" 규칙에서 제외된다.

- `color_synonym`·`word_order`(INV): 두 발화가 같은 `searchFixtureId`를 공유하고 시스템
  노출 목록이 완전히 같아야 통과. 색상 동의어 6쌍(#258), 어순 변경 6쌍.
- `constraint_subset`(DIR): 제약을 하나 더 강하게 준 케이스와 완화한 케이스를 쌍으로 만든다
  (`expectedFilters`가 진짜 상위집합 관계여야 판별 가능 — 라벨이 아니라 필터 자체의 포함
  관계로 강함/완화를 가른다). 강화 케이스 노출 ⊆ 완화 케이스 노출이면 통과. 3쌍.
- `member_recall_ge_guest`(DIR): 같은 그룹의 member/guest 쌍은 이 검사만 예외로 라벨
  (`relevantProductIds`)이 필요하다(recall 계산 자체가 정답을 요구한다) — #119 선례.

검사기는 `evals/metrics/behavior.py`(`evaluate_behavior_checks`)이며 산출물에
`behaviorChecks` 섹션으로 순위 지표와 분리돼 실린다.

**결정론 실행(기본 `OfflineBuyerAdapter`)에서 `color_synonym`/`word_order` INV는 구조적으로
통과한다(#333 리뷰 F-3).** scripted decompose가 케이스의 `expectedFilters`를 그대로 반환하므로
같은 fixture를 공유하는 INV 쌍은 발화가 달라도 노출이 항상 같다 — 이는 배선 검증이지 모델이
색상 동의어·어순 변화를 실제로 이해하는지의 검증이 아니다. INV 판정의 정본은 실모델 어댑터
실행(#144 계열)이며, 기본 scripted 실행의 통과를 모델 검증 근거로 인용하지 않는다.

### v1→v2 이관 요약

`evals/goldenset/migrate_v1_to_v2.py`가 v1 43건(dev 31/holdout 12)을 기계 이관했다
(`schemaVersion`/`datasetVersion` → 2.0.0, `testType=MFT`, needs 슬라이스 자동 배정, member
거울 슬라이스, fixture candidates 전부 `{"source":"golden_filter","rule":null}`). 니즈 슬라이스
배정 우선순위는 패킷 문면(가격 하드제약 최우선)과 달리 **v1에 이미 curation된
`multi_constraint`/`repurchase` 슬라이스 태그를 가격 신호보다 우선**했다 — 그대로 문면 순서를
따르면 `multi_constraint` dev 슬라이스가 0건이 되어 감사 위반이 새로 생기기 때문이다. 상세는
`CHANGELOG.md` 2026-08-05 항목과 구현 보고를 본다.

## 향후 production-derived 추가

현재 production-derived 케이스는 0건이다. 향후 추가할 때는 비식별 여부를 사람이 수동
검수하고, raw prompt·raw profile·고객 원문·상품 원문을 저장하지 않는다. 필요한 신호는 유계
범주와 가명 식별자로 환원하며 개인을 재식별할 수 있는 조합도 금지한다.

## 알려진 한계(후속 이슈, #333 adjudication 라운드 관찰)

독립 검수자 adjudicator-omx-01의 127건 전수 검수(비차단 관찰)에서 나온, 이번 라운드에서는
데이터를 바꾸지 않고 문서화만 하는 3가지 한계다.

- **`member_recall_ge_guest` 검사의 자명성**: 구매 이력이 비어 있는 합성 페르소나가 많고
  guest/member 거울 케이스의 fixture·라벨이 사실상 동일해, member 재현율이 guest 이상이라는
  검사가 상당수 케이스에서 거의 항상 참이 되는 동어반복에 가깝다. 후속 이슈에서 구매 이력이
  실제로 개인화 신호로 작용하는 페르소나 비중을 늘려야 검사가 실효를 갖는다.
- **신라면/커피믹스 키워드의 cross-split 재사용 패턴**: 카탈로그에 존재하는 실제 신라면·
  커피믹스 상품 수가 제한적이라, dev와 holdout에 걸쳐 유사한 의도(예: "신라면 봉지라면"과
  "카테고리가 이상한 신라면", "커피믹스 추천"과 "카테고리가 이상한 커피믹스")로 케이스를
  구성하면 relevant 집합이 자연히 겹친다. 이번 라운드에서는 `audit.run_audit`의
  `relevantSetOverlap`(상한 0.5)을 넘지 않도록 각 케이스의 실제 취지(일반 추천 vs 카테고리
  오분류 견고성 테스트)에 맞춰 정답 부분집합을 분리했다(예: buy-cmap-1002는 명백히
  오분류된 후보로만 한정). 후속 이슈로 이 카테고리들의 catalog 표본을 늘려 구조적으로
  분리 여지를 넓히는 편이 낫다.
- **`price=null` injected 후보의 가격 게이팅 모호성**: 하드 네거티브 주입 풀 일부 상품은
  catalog_snapshot에 가격이 없다(`price: null`). `priceMax`/`priceMin` 하드제약이 있는
  케이스에서 이런 후보를 relevant로 승격할지 판단할 근거가 없어, 이번 라운드에서는 가격
  하드제약이 없는 케이스에서만 `price: null` injected 후보를 relevant로 승격했다(예:
  buy-cold-0001의 5578895099/8124432652). 후속 이슈로 injected 풀의 가격 결측을 채우거나,
  가격 하드제약이 있는 케이스에서는 `price: null` 후보를 자동으로 grade 판정 보류(수동 확인
  필요)로 표시하는 규약을 검토한다.

## v2.2(#370) — 위반 네거티브 채널·라벨 provenance

#333 adjudication 라운드가 남긴 갭 3건(위반 네거티브 0건·라벨 주체 미기록·슬라이스 쿼터 하향
사유 미문서화) 후속. 새 케이스를 추가하지 않는다(127건 불변) — 기존 fixture candidates
provenance와 케이스 core 필드만 보강한다.

### 위반 네거티브 채널이란

`semantic_near`/`other_brand`/`broadened_search`/`random_catalog`(§하드 네거티브 주입 규칙)는
전부 "**유사하지만 오답**"인 후보다 — 정답과 가깝지만 관련도가 낮다는 이유로 오답이다.
위반 네거티브는 이와 별도 채널로, "**케이스의 하드 제약을 실제로 위반**"하는 후보만 모은다 —
관련도 판단이 아니라 `hardConstraints`/`forbiddenCategories`/`attrConditions`라는 기계적으로
검증 가능한 규칙 위반이다. 랭커가 제약을 지키는지(HCV, hard constraint violation) 재는 축이라
"유사도"가 아니라 "규칙 준수"를 시험한다.

rule 3종:

| rule | 위반 정의 |
|---|---|
| `price_violation` | catalog 가격이 케이스 `hardConstraints.priceMax` 초과 또는 `priceMin` 미만(가격 필수 — null이면 태그 불가) |
| `category_violation` | catalog `categoryName` ∈ `forbiddenCategories` **또는** productId ∈ (`forbiddenProductIds` ∪ `mustExcludeProductIds`) |
| `attr_violation` | 케이스 `expectedFilters.attrConditions`가 존재하고 후보가 그 조건을 위반함을 catalog `attributes`로 판정 가능(`schema.judge_attr_violation` — 조건 키가 catalog attributes에 정확히 있고 값이 다를 때만 위반. 동의어 매핑을 하지 않는다 — 예를 들어 케이스가 `차단지수`를 조건으로 써도 catalog의 실제 키가 `SPF지수`면 판정 불가로 본다) |

### 태그 검증 규칙(`schema.validate_cases`)

- 위반 태그 후보는 **정답이 될 수 없다** — `relevantProductIds`에 있으면 오류다. 하드 네거티브의
  `injected-relevant-approved:` 마커로도 우회할 수 없다(위반 네거티브는 그 마커의 적용 대상이
  아니다 — "관련 있다고 판단해 승격"이라는 그 마커의 취지 자체가 "이 후보는 애초에 위반이다"와
  모순된다).
- 위반 태그 후보를 담은 fixture는 **정확히 1개 MFT 케이스가 단독 사용**해야 한다. 같은
  fixture를 2개 이상의 케이스가 공유하면(INV `color_synonym`/`word_order` 쌍 등) 어느 케이스의
  하드 제약을 기준으로 위반을 판정해야 할지 모호해지기 때문이다 — 공유 fixture에는 위반 태그
  후보를 넣을 수 없다.
- 세 rule 모두 태그된 후보가 실제로 그 위반 정의를 만족하는지 catalog로 재검증한다(위 표) —
  rule 이름만 붙이고 실제로는 위반이 아닌 후보를 허용하지 않는다.

### 사전 등록 쿼터(dev 기준, §370 패킷 §2)

| 유형 | 최소 케이스 수 | 케이스당 최소 후보 | 케이스당 주입 상한 |
|---|---:|---:|---:|
| `price_violation` | 8 | 2 | 4 |
| `category_violation` | 4 | 1 | 4 |
| `attr_violation` | 2 | 1 | 4 |

실채움과 미달 사유는 `manifest.json`의 `violationNegatives` 블록, 기계 검증은
`audit.run_audit()`의 `violationNegativeFill`(산출물 동봉) + CI 유닛 테스트가 강제한다.
`attr_violation`은 오프라인 실측 결과 대상 3케이스 전부 catalog attribute 키 명이 케이스
`attrConditions` 키와 달라(위 표 참조) 판정 가능한 후보가 0건이다 — 조작하지 않고 미달로
남긴다. 가격·카테고리 축은 실측상 채울 수 있어 최소를 넉넉히 초과 달성했다.

**카테고리 축 재태깅이 건드리는 것과 건드리지 않는 것(#370 리뷰 라운드2 F-1)**:
`inject_violation_negatives.retag_category_violations`는 대상 5건(candidate) 중 `rule`
필드만 `category_violation`으로 바꾼다 — `from`(채굴 출처)은 항상 보존한다. 5건 중 3건
(`buy-cmap-0004`의 1679183612, `buy-repu-0001`/`buy-repu-0003`의 9205089754)은 재태깅 전
`rule=None, from="primary"`였다. 나머지 2건(`buy-over-0003`의 9205089754/9406282766)은
재태깅 전 이미 `rule="broadened_search", from="keyword-only"`였다 — 이 둘은 `rule`이
`category_violation`으로 덮어써지지만(위반 채널 소속이 더 정확한 분류다) `from="keyword-only"`
는 그대로 남아 원래 채굴 경로(완화 검색)를 복구할 수 있다.

**holdout은 이번 이슈 범위 밖이다** — sealed 라벨(`buyer_holdout_labels.jsonl`) 접근 없이는
holdout 케이스의 `relevantProductIds`/`hardConstraints`를 알 수 없어 위반 판정 자체가
불가능하다. holdout 위반 네거티브는 sealed 라벨 접근 절차를 설계할 후속 이슈로 남긴다.

### `evals/metrics` 결정론 harness의 Spring mock 가격 필터(#370 결정 01)

`evals/metrics/harness.py`의 `_CaseTransport`(`OfflineBuyerAdapter`가 쓰는 fake Spring)는
원래 `/internal/products/search` 요청의 쿼리 파라미터를 무시하고 fixture 후보 전체를
그대로 돌려줬다 — 실 Spring이 `minPrice`/`maxPrice`를 서버사이드로 거르는 것(정상 경로는
`app/services/spring_client.py`가 필터를 I-1 쿼리에 싣고, `app/agents/buyer/recommendation/
graph.py`의 `within_price_range`는 인기상품 폴백 경로 전용이라 이 경로를 타지 않는다)과 다른
mock 충실도 격차였다. #333의 기존 `price_violation` 채널이 실측 0%에 가까워 이 격차가 한
번도 실제로 후보를 새게 한 적이 없었지만, 이번 이슈가 처음으로 유의미한 수(47건) 후보를
주입해 `tests/eval/test_goldenset_eval.py`의 critical PR 게이트가 새로 걸렸다(앱 결함이
아니라 harness 결함으로 확인).

수정은 **"앱이 실제로 보낸 요청 파라미터"**(`minPrice`/`maxPrice`)만 적용한다 — 케이스의
`hardConstraints`를 직접 읽어 거르지 않는다. 그래야 decompose가 가격 축을 놓치는 상황을 그대로
계측할 수 있다(§아래 채널 비공허성 테스트). 가격이 없는(`None`) 상품은 판정 불가로 보고 항상
통과시킨다(`evals/scoring/hard_filter`의 `priceUnknown` 컷과는 다른 계층 — 통일하지 않는다).
**`keyword`/`categoryName`/`brandName` 충실도는 이번에 올리지 않았다** — 올리면 이 이슈와
무관한 기존 전 케이스의 노출 집합이 흔들려 baseline이 교란된다. `category_violation`/
`attr_violation` 후보는 I-1 쿼리 파라미터가 아니라 라벨 수준 제약(mustExclude·forbidden
category·attrConditions)이라 이 mock이 거르지 않는다 — 랭커가 올리지 않는 것이 정답이며,
그대로 노출되는 것이 의도다.

채널이 공허해지지 않았음을 3가지로 증명한다: (1) mock 필터 자체의 경계·null-보존 유닛
테스트(`tests/unit/test_eval_metric_harness.py`), (2) decompose가 가격 축을 놓치는 상황을
재현해 `assert_pr_gate`가 실제로 실패함을 확인하는 회귀 테스트(`tests/eval/
test_goldenset_eval.py::test_pr_gate_catches_price_violation_when_decompose_drops_price_axis`
— 이 채널의 존재 이유 자체를 증명한다), (3) `evals/scoring`의 `apply_hard_filters`가 주입
47건 중 몇 건을 실제로 컷하는지 `evals/scoring/baselines/dev-v2.2/README.md`에 실측 수치로
남긴다.

### 라벨 provenance 필드

`CaseCore`(dev·holdout core 파일 공통, sealed holdout 라벨과 무관)에 3필드가 있다:

- `labelSource`: `"human"`(사람이 최종 판정) / `"model"`(LLM·에이전트가 초안 또는 검수) /
  `"heuristic"`(규칙 기반 자동 생성) / `"unknown"`(문서 근거를 찾을 수 없어 추정하지 않음).
- `labeledAt`: 라벨이 확정된 날짜(`YYYY-MM-DD`). 케이스별 개별 시점 기록이 없으면 문서화된
  가장 가까운 사건(예: adjudication 반영일)을 쓰고 소급임을 `labelRationale`에 남긴다.
- `labelRationale`: 무슨 근거로 이 라벨을 붙였는지 한 줄.

**소급 원칙**: 문서화된 사실만 쓴다. 개별 케이스의 라벨 시점·주체를 정확히 알 수 없으면
`labelSource="unknown"`으로 정직하게 두고 사후 추정으로 값을 지어내지 않는다. 기존 127건은
labeler-01/02의 자동 초안 + adjudicator-omx-01(모델)의 127건 전수 검수를 거쳤고 사람 검수
완료 상태가 아니라는 사실이 `manifest.adjudicationSummary`와 이 문서(§사람 검수)에 이미
문서화돼 있어 전 127건 `labelSource="model"`이 근거를 갖는다(개별 케이스 단위가 아니라
데이터셋 전체 단위의 문서 근거다). 이 이슈에서 새로 만드는 라벨 변경(향후 relabel 등)은
그 판정을 실제로 내린 근거를 `labelRationale`에 적는다 — "소급"이 아니라 "당시 근거"다.

## #474 색상 동의어 A/B MFT

색상 고유어/정본 쌍은 off 팔에서 의도적으로 노출이 달라 INV `color_synonym` 그룹에 등록하지 않는다.
fixture는 라이브 검색 없이 `catalog_snapshot.json`만으로 결정론 생성한다.
I-1 mock은 api-spec §4.6의 색상 미지정·축 없음 패스스루·부분 일치 3갈래 판정을 모사한다.
`injected-relevant-approved:`는 #474에서 adjudicator 승인이 아니라 정본 색상 일치로 설계한
오프라인 정답 후보를 뜻하며, 신규 케이스의 `adjudicator`는 비어 있다.
