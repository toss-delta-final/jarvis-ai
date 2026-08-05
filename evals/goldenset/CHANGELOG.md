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
