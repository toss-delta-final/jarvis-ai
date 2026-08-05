# 구매자 골든셋 변경 이력

아직 baseline 실행·공개 전인 v1 초안이므로 아래 정정에도 `datasetVersion`은 `1.0.0`을 유지한다.

| 날짜 | datasetVersion | 변경(add/remove/relabel) | caseId | 사유 |
|---|---|---|---|---|
| 2026-08-02 | 1.0.0 | add | dev: buy-srch-0001~0006, buy-gust-0001~0002, buy-cold-0001~0002, buy-pers-0001~0003, buy-over-0001, buy-repu-0001~0003, buy-cmap-0001~0006, buy-mult-0001~0002, buy-fail-0001~0003; holdout: buy-srch-1001~1006, buy-cmap-1001~1002, buy-fail-1001~1002, buy-repu-1001, buy-pers-1001 | 구매자 추천 골든셋 v1 최초 40건(dev 28 / holdout 12) 구축 |
| 2026-08-03 | 1.0.0 | add | buy-over-0002, buy-over-0003 | 카테고리·브랜드 축의 personalization_overreach 회귀 케이스 보강 |
| 2026-08-03 | 1.0.0 | add | buy-srch-0007 | 야구 categoryName과 실제 장비를 snapshot에서 검증하는 검색 케이스 보강 |
| 2026-08-03 | 1.0.0 | relabel | buy-srch-0005 | 카테고리만 오분류된 실제 축구공 2건을 금지 결과에서 정답(3/1등급)으로 재판정 |
| 2026-08-03 | 1.0.0 | relabel | buy-cmap-0005, buy-cmap-0006 | forbiddenCategories를 DB 계층명이 아닌 I-1 categoryName 표기로 정정 |
| 2026-08-03 | 1.0.0 | relabel | buy-over-0001 | 사용자가 7만원 이상 제약을 직접 말하도록 질의를 재작성해 priceMin 근거를 정정 |
| 2026-08-03 | 1.0.0 | relabel | buy-srch-1001~1006, buy-cmap-1001~1002, buy-fail-1001~1002, buy-repu-1001, buy-pers-1001 | holdout 판정 notes를 봉인 라벨 파일로 이동 |
| 2026-08-03 | 1.0.0 | relabel | guest identity 전 30건 | identity.kind와 guest slice를 양방향 정합 |
| 2026-08-03 | 1.0.0 | relabel | 전체 43건 fixture | request를 raw 발화가 아닌 expectedFilters로 라이브 재기록 |
| 2026-08-03 | 1.0.0 | relabel | buy-srch-1002, buy-cmap-1001, buy-srch-1003 | 오답 후보가 함께 검색되도록 expectedFilters를 상위 keyword로 넓히고 정답을 재판정 |
| 2026-08-03 | 1.0.0 | relabel | buy-srch-1004 | 단일 후보 질의를 학생 운동화 질의로 교체하고 라이브 후보에서 정답을 재판정 |
| 2026-08-03 | 1.0.0 | audit | nonDiscriminativeRanking | 후보가 전부 정답인 케이스를 #143 순위 지표 분모 제외 목록으로 감사 산출물에 추가 |
| 2026-08-05 | 2.0.0 | migrate | 전체 43건(dev 31 / holdout 12) | #333 Part 1 — `migrate_v1_to_v2.py`로 v1(1.0.0)을 v2(2.0.0)로 기계 이관. `testType=MFT` 명시, 니즈 슬라이스(single_need/multi_constraint/budget/repurchase)를 케이스당 정확히 1개 배정, `identity.kind==member` 케이스에 `member` 슬라이스 거울 추가, `fixtures/search_responses.json`에 v2 candidates provenance(`source`/`rule`/`from`) 채움. 니즈 슬라이스 우선순위는 패킷 문면(가격 하드제약 최우선)과 달리 **v1에 이미 명시적으로 curation된 `multi_constraint`/`repurchase` 슬라이스 태그를 가격 신호보다 우선**했다 — 문면 그대로면 `buy-mult-0001/0002`·`buy-fail-0001/0003`가 전부 가격 제약도 있어 `budget`으로 흡수되고 dev split에 `multi_constraint` 케이스가 0건 남아 `audit.run_audit()`의 `missingDevSlice` 위반이 새로 생겼다(상세는 구현 보고 §설계 결정, orchestrator 확인 필요). |
| 2026-08-05 | 2.0.0 | relabel | 순위 평가 대상 중 후보 20건 미만 20건(dev 14 / holdout 6) | `narrow-domain:` notes 접두 문구로 `goldenset_min_ranking_candidates` 하한 예외 처리. Part 1은 구조·규약만 다루므로 실제 후보 패딩(하드 네거티브 주입)은 Part 2에서 한다 — 이 표시는 임시이며 Part 2가 후보를 30까지 채우면 대부분 해제되어야 한다. |
