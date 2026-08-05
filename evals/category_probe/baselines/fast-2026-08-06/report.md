# 카테고리 매핑·선택 프로브 리포트 (#331)

tier=fast · model=gpt-5-nano · fixture=category-probe-anchors-v1 · N=8 · cells=38

> ⚠ **단일 실행으로 채택 판정 금지 — 표본 분산이 커 축당 ±수 포인트가 흔들린다. 독립 2~3회 분포로 판정한다.**

> **이건 골든셋이 아니다.** 추천 품질이 아니라 발화→카테고리 매핑·선택 정확도를 잰 표다.

## 축 (파이프라인)

| 축 | 점수 | 분자 정의 | 분모 정의 |
|---|---|---|---|
| `invAgreement` INV 표기 변형 합의 | 1/4 (25.0%) | 그룹 내 전 변형의 다수결 top-1 canonical 이 서로 동일하고 null 아님 (동률은 다수결 없음=None 이라 불합의로 센다) | 4그룹 |
| `multiCoverage` fan-out leg 커버리지 | 54/104 (51.9%) | 기대 leg 별로 accept ∈ legs (leg 단위 집계) | multi 6셀 × 기대 leg 수 × N |
| `multiExactSet` fan-out leg 집합 정확 일치 | 16/48 (33.3%) | legs 집합이 기대 leg 집합과 정확히 일치(여분 leg 없음) | multi 6셀 × N |
| `noneNoForce` 카테고리 무지정 → 무필터 | 39/40 (97.5%) | legs == [] (오답 canonical 을 강제하지 않음) | none 5셀 × N |
| `notInCatalogNoForce` 사전에 없는 카테고리 → 무필터 | 40/40 (100.0%) | legs == [] (오답 canonical 을 강제하지 않음) | notInCatalog 5셀 × N |
| `top1Single` 단일 카테고리 top-1 일치 | 60/176 (34.1%) | 최종 legs 에 기대 leg 의 accept 중 하나가 존재 | single(MFT+INV) 22셀 × N |
| `topKInclusion` 이긴 앵커 top-5 포함 | 163/176 (92.6%) | 기대 accept 중 하나가 이긴 앵커의 top-5 후보(계측 hits 기준) 또는 최종 legs(exact match 는 hits 없이 바로 canonical 을 내므로 포함) 안에 존재 — decompose 가 leg 자체를 안 냈으면 분자 불충족 | single(MFT+INV) 22셀 × N |

## trivial baseline 대조

| 축 | 점수 | 정의 |
|---|---|---|
| `baselineTop1Single` | 20/22 (90.9%) | 임베딩 최근접 top-1 ∈ accept |
| `baselineTopK` | 20/22 (90.9%) | accept ∈ baseline top-5 |

> none/notInCatalog 슬라이스에서 baseline 은 항상 top-1 을 강제하므로 정의상 0% 다 — baseline 은 기권할 수 없다는 사실 자체가 정보다(§6).

## 진단 (합불 아님)

- intent 슬립(버려짐): 1
- leg 추출 실패(noLegCount): 8
- 거리컷 드롭: 201
- 택일 null: 3
- 택일 교체(changed): 8
- exact 매치: 0
- fan-out 확장 발동: 187

## 혼동 표 (single/multi 오답)

| 기대 | 실제 | 빈도 |
|---|---|---|
| `남성가방 > 백팩` | `∅` | 40 |
| `음향가전 > 이어폰` | `∅` | 24 |
| `여성의류 > 원피스` | `∅` | 24 |
| `노트북 > 삼성전자` | `∅` | 16 |
| `남성의류 > 청바지` | `∅` | 10 |
| `남성신발 > 운동화` | `∅` | 8 |
| `선케어 > 선크림/선블록` | `∅` | 8 |
| `기저귀 > 일회용기저귀` | `∅` | 8 |
| `문구/사무용품 > 문구용품` | `∅` | 8 |
| `세제/방향/살충 > 세탁세제` | `∅` | 8 |
| `축산 > 돼지고기` | `∅` | 8 |
| `건강식품 > 인삼/한방재료` | `∅` | 2 |
| `우유/유제품 > 우유` | `∅` | 2 |

## top-1 distance 분포 (#344 계측)

| | 표본 수 | 중앙값 | Q1 | Q3 |
|---|---|---|---|---|
| 정답 | 60 | 0.2194 | 0.2104 | 0.2378 |
| 오답 | 0 | None | None | None |

> 채택(배포 자체 계측) distance 기준 — exact match 표본(거리 개념 없음)은 제외.

## 채우지 못한 셀

(없음)

## 재현 함정

1. 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 아래 목록에 드러난다.
3. 앵커-정답 누출 금지 — accept(canonical) 문자열이 발화 원문에 그대로 들어가면 안 된다(schema.py 가 ' > ' 포함을 거부해 강제한다).
4. 단일 실행으로 채택 판정 금지 — 표본 분산이 커 축당 ±수 포인트가 흔들린다. 독립 2~3회 분포로 판정한다.

페이싱 실측: 대기 159회 / 허용 45 rpm.
