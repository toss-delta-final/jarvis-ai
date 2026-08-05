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
  마커 나열을 파싱한다(startswith가 아니다).
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
