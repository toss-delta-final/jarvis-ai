# 취향 추출 골든셋 프로브 리포트 (#462)

provider=openai · model=gpt-5.6-luna · promptSha12=f1087ac09a78 · datasetVersion=2026-08-08.2 · datasetHash=e31dd78646bc · N=5 · sessions=30

> ⚠ **단일 실행으로 채택 판정 금지 — 표본 분산이 커 축당 ±수 포인트가 흔들린다. 독립 2~3회 분포로 판정한다.**

> **이건 `evals/goldenset` 이 아니다.** 단발 검색 질의 + 상품 순위 라벨이 아니라 다턴 대화 → 기대 트리플의 추출·resolver 정확도를 잰 표다. 두 산출물의 숫자를 섞지 말 것.

## 축 (primary confirmatory · 사전 등록 2차)

| 축 | 점수 | exploratory | 분자 정의 | 분모 정의 |
|---|---|---|---|---|
| `recall` recall(미탐율의 반대) | 84/115 (73.0%) | 아니오 | 매칭된 기대 트리플 수(최대 이분 매칭, §7 매칭 규칙, A-13) | 노이즈 제외 세션의 기대 트리플 수 × N |
| `noiseFalsePositiveRate` 오탐 슬라이스 산출 트리플 수 | 0/50 (0.0%) | 아니오 | noise 세션 표본에서 산출된 트리플 총수(표본당 0개 이상) | noise 세션 수 × N(표본 수 기준) |
| `nodeIdAgreement` nodeId 일치율(매칭 트리플 중) | 74/84 (88.1%) | 아니오 | 매칭 트리플 중 produced.node.node_id == expected.node_id | 매칭 트리플 수 |

## 축 (exploratory)

| 축 | 점수 | exploratory | 분자 정의 | 분모 정의 |
|---|---|---|---|---|
| `falsePositiveRate` 오탐율(전체) | 59/143 (41.3%) | 예 | 어떤 기대 트리플과도 매칭 안 된 산출 트리플 수 | 산출 트리플 총수(전체 슬라이스) |
| `missRate` 미탐율 | 31/115 (27.0%) | 예 | 1 - recall 의 분자(매칭 안 된 기대 트리플 수) | 노이즈 제외 세션의 기대 트리플 수 × N |
| `sessionExactSet` 세션 집합 정확 일치 | 92/150 (61.3%) | 예 | 산출 트리플 집합이 기대 집합과 정확히 일치(여분 0·누락 0) | 세션 수 × N |

## 슬라이스 분해 — recall (exploratory)

| 슬라이스 | 점수 |
|---|---|
| `conflict` | 10/15 (66.7%) |
| `kindCoverage` | 39/55 (70.9%) |
| `noise` | 0/0 |
| `polarity` | 27/30 (90.0%) |
| `repetition` | 8/15 (53.3%) |

## trivial baseline 대조

| 축 | 점수 | 정의 |
|---|---|---|
| `baselineRecall` | 0/23 (0.0%) | 0(아무것도 안 뽑는다) |
| `baselineFalsePositiveRate` | 0/0 | 0(산출 트리플 자체가 없다) |
| `baselineNoiseFalsePositiveRate` | 0/50 (0.0%) | 0(아무것도 안 뽑는다) |
| `baselineSessionExactSet` | 10/30 (33.3%) | 기대 집합이 빈 세션(noise) 수 — baseline 출력([])과 우연히 일치 |

> baseline 은 오탐 축에서 정의상 완벽하고 미탐 축에서 정의상 최악이다 — 따라서 추출이 개선인지는 'recall 이 0보다 얼마나 큰가'를 '오탐을 얼마나 치렀나'와 함께 봐야 판정된다. 한 축만 보면 baseline 을 이기는 것이 자명하거나 불가능해 보인다.

## 진단 — 단계 귀속 (합불 아님)

- emittedDeltas: 171
- promotedCount(프로덕션 반환값): 169
- gateRejected: 2
- resolverDroppedByKind: {'category': 26} (합계 26)
- legacySchemaNoKind(구스키마, 정상 경로): 0
- unprojectedFacts: 26
- factDedupCollapsed(0 초과면 위 kind 귀속이 근사): 0
- verifiedFalseCount: 122
- schemaViolation(표본 실패): 0
- transportError(표본 실패, 타입별): {}
- bandLabelRejected: []

## kind 오분류 행렬 (라벨 일치 기준, §A-4)

| 기대 kind | 산출 kind | 빈도 |
|---|---|---|
| `∅` | `attribute` | 52 |
| `category` | `∅` | 16 |
| `attribute` | `∅` | 10 |
| `∅` | `situation` | 5 |
| `situation` | `∅` | 5 |
| `∅` | `priceBand` | 2 |

## predicate 오분류 행렬 (kind·라벨 일치, predicate 만 다름)

| 기대 predicate | 산출 predicate | 빈도 |
|---|---|---|
| (없음) | | |

## 채우지 못한 세션

(없음)

## 재현 함정

1. 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 세션은 아래 목록에 드러난다.
3. 정답 누출 금지 — priceBand/ratingBand canonicalLabel·' > ' 포함 category canonicalLabel 이 발화 원문에 그대로 들어가면 안 된다(schema.py 가 강제).
4. repeatCap 이 축자 반복을 먹는다 — repetition 슬라이스는 다른 표현으로 반복해야 한다.
5. 단일 실행으로 채택 판정 금지 — 표본 분산이 커 축당 ±수 포인트가 흔들린다. 독립 2~3회 분포로 판정한다.

페이싱 실측: 대기 19회 / 허용 45 rpm.
