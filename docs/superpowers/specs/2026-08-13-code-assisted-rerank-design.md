# Code-assisted buyer rerank 설계

## 배경

이슈 #631에서 `current`, `structured`, `hybrid` ranking arm을 분리했다. 현재 `structured`는
LLM이 모든 후보에 `intentFit`, `needFit`, `profileFit`을 부여하고 코드가 `4:2:1`로 합산한다.
이 구조는 점수 검증과 결정적 정렬이 가능하지만 다음 한계가 확인됐다.

- 객관적 상품 사실까지 LLM이 해석해 점수로 바꾼다.
- 모든 후보 평가를 출력하므로 `current`보다 지연과 출력 토큰이 크다.
- 200-case draft nDCG에서는 우세했지만 별도 blind judge에서는 `current`가 우세했다.
- 사용자에게 보이는 추천 이유는 ranking arm과 독립된 grounding 계약을 사용하므로,
  `structured`의 세부 점수가 더 나은 설명으로 직접 이어지지 않는다.
- `4:2:1` 주변의 사후 가중치 분석에서는 유의미한 추가 개선이 없었다. 문제의 중심은 합산
  가중치보다 신호 소유권과 후보 선택 방식에 있다.

따라서 기존 arm을 보존하면서, 코드가 확인 가능한 사실과 정량 신호를 만들고 LLM은 의미 판단과
최종 선택만 담당하는 `code_assisted` arm을 추가한다.

## 목표

1. 객관적 사실의 계산과 검증을 코드 소유로 옮긴다.
2. 자연어 문맥, 용도, 취향처럼 규칙만으로 판단하기 어려운 부분은 LLM에 남긴다.
3. LLM은 모든 후보 점수표가 아니라 최종 노출 후보만 반환해 `structured`의 출력 비용을 줄인다.
4. 추천 이유를 검증된 코드 증거와 제한된 의미 판단을 조합해 더 구체적으로 만든다.
5. 기존 `current`, `structured`, `hybrid`의 동작과 롤백 경로를 유지한다.
6. 각 판단의 소유자와 출처를 기록해 오프라인 분석과 장애 진단이 가능하게 한다.

## 비목표

- 이번 변경에서 학습 기반 LTR 모델을 도입하지 않는다.
- 새로운 외부 API, DB 테이블, 패키지를 추가하지 않는다.
- 프로필 markdown을 코드에서 파싱해 개인화 점수를 만들지 않는다.
- 검색 결과에 없는 상품을 생성하거나 검색 하드 필터를 완화하지 않는다.
- `code_assisted`를 평가 없이 production 기본값으로 전환하지 않는다.
- 점수 숫자나 내부 프로필 정보를 사용자에게 노출하지 않는다.

## 설계 원칙

### 필드마다 판단 주체는 하나다

가격, 평점, 리뷰, 명시 속성처럼 코드가 판정한 항목을 LLM이 다시 채점하지 않는다. 같은 신호를
양쪽에서 평가하면 이중 반영되고 실패 원인을 구분할 수 없다.

### 하드 조건은 점수가 아니다

명시적 가격 상한, 브랜드, 평점 하한, 색상, 속성 조건은 기존 검색·사후 필터에서 계속 후보를
제외한다. 조건 위반 후보를 낮은 점수로 살려두지 않는다. 통과 사실은 추천 이유를 위한 증거로만
전달할 수 있다.

### `0`과 `unknown`을 구분한다

`0`은 확인했으나 불일치 또는 최저 등급임을 뜻한다. 원본 값이 없거나 적용할 조건이 없는 경우는
`null`/`not_applicable`로 둔다. 정보 부재를 부적합으로 오인하지 않는다.

### 단일 총점보다 이름 있는 신호를 우선한다

주 경로에는 모든 객관 신호를 임의 가중치로 합친 `codeScore` 하나를 만들지 않는다. 코드가 만든
각 component와 evidence를 LLM에 독립적으로 제공한다. 단일 총점은 가중치 선택 문제를 다시 만들고,
LLM이 어떤 사실을 사용했는지 숨긴다.

## arm과 롤아웃 경계

`RankingArm`을 다음처럼 확장한다.

```text
current | structured | hybrid | code_assisted
```

- `current`: 기존 LLM 직접 선택 경로를 그대로 유지한다.
- `structured`: 기존 LLM component 전수 평가와 코드 합산 경로를 그대로 유지한다.
- `hybrid`: 기존 structured 순위와 search rank의 RRF 결합을 그대로 유지한다.
- `code_assisted`: 새 코드 신호를 입력으로 받고 LLM이 최종 노출 목록을 선택한다.

초기 기본값은 바꾸지 않는다. `code_assisted`는 명시 설정과 평가 harness에서만 선택할 수 있게 하고,
품질·지연·안전성 gate를 통과한 뒤 별도 결정으로 기본값 변경을 검토한다.

## 데이터 경계

### `CodeScoringContext`

`recommendation/graph.py`는 현재 rerank 경계에서 유실되는 구조화 조건을 모아 별도 context로
전달한다.

```python
CodeScoringContext(
    filters=effective_filters,
    search_rank_by_id=search_rank_by_id,
    need_of=need_of,
    total_budget=decision.total_budget,
)
```

초기 버전에는 현재 요청에서 이미 확정된 값만 포함한다.

- `ProductSearchFilters`: category, price range, brand, rating minimum, color, attributes
- 후보의 원래 search rank
- 멀티 니즈 턴의 product-to-need 매핑
- 전체 예산이 있는 경우 그 값

프로필 벡터는 upstream에 존재하지만 현재 rerank 후보별 유사도 점수가 없다. 이번 버전에서는
`profile_summary`를 LLM의 의미 판단 입력으로 유지하며, 코드 개인화 component를 만들지 않는다.
향후 검색 backend가 후보별 profile similarity를 제공하면 별도 versioned component로 추가한다.

### `CandidateCodeSignals`

각 후보마다 코드가 다음 자료를 만든다.

```json
{
  "productId": 123,
  "searchRank": 4,
  "need": "여름 출근복",
  "facts": {
    "category": "상의",
    "brand": "Example",
    "priceLevel": "저렴",
    "ratingLevel": "높음",
    "reviewLevel": "많음"
  },
  "codeSignals": {
    "evidence": [
      {"ref": "CATEGORY_MATCH", "code": "CATEGORY_MATCH"},
      {"ref": "ATTRIBUTE_MATCH:소재", "code": "ATTRIBUTE_MATCH", "field": "소재", "value": "린넨"},
      {"ref": "PRICE_RANGE_MATCH", "code": "PRICE_RANGE_MATCH"}
    ],
    "objectiveComponents": {
      "ratingQuality": 2,
      "reviewConfidence": 2,
      "explicitConditionCoverage": {"matched": 3, "applicable": 3}
    }
  }
}
```

component는 내부 정렬용 만능 총점이 아니라 입력을 압축한 정규화 값이다.

| component | 소유자 | 의미 |
|---|---|---|
| `ratingQuality` | 코드 | 기존 `_rating_tier`와 일치하는 정규화 품질 |
| `reviewConfidence` | 코드 | 기존 `_review_tier`와 일치하는 근거량 |
| `explicitConditionCoverage` | 코드 | 판정 가능한 명시 조건 중 일치한 개수와 전체 개수 |

`ratingQuality`는 `매우높음=2`, `높음=2`, `보통=1`, `낮음=0`, `평가없음=null`이다.
`reviewConfidence`는 `매우많음=2`, `많음=2`, `보통=1`, `적음=0`, `없음/정보없음=null`이다.
두 값은 기존 config 기반 tier 함수의 결과만 변환하므로 임계값을 중복 정의하지 않는다.
`explicitConditionCoverage`는 category, brand, price range, rating minimum, color, attribute 중 실제로
적용되고 코드가 확인할 수 있는 항목만 `applicable`에 포함한다. 하드 필터를 통과한 정상 후보에서는
대부분 전부 일치하며, 이는 주로 검증·설명 신호이지 순위 차이를 억지로 만드는 신호가 아니다.

가격은 낮을수록 항상 좋은 것이 아니므로 `priceLevel`과 예산 통과 evidence로 제공하되 기본 objective
component에 가산하지 않는다. 사용자가 가성비·최저가를 원하는지에 대한 판단은 LLM 소유다.

검색 순위는 실제 backend relevance score가 현재 `SpringProduct`에 없으므로 정확한 `searchRank`만
전달한다. 향후 raw relevance가 제공되기 전까지 유사도 점수처럼 표현하지 않는다.

## LLM 계약

LLM은 코드가 만든 사실과 신호를 변경하거나 재계산하지 않는다. 모든 후보를 입력으로 보되
최대 `expose_max`개만 최종 순서대로 반환한다.

```json
{
  "ranked": [
    {
      "productId": 123,
      "semanticIntentFit": 4,
      "useCaseFit": 3,
      "profileFit": 1,
      "semanticReasonCode": "DIRECT_INTENT_MATCH",
      "evidenceRefs": ["ATTRIBUTE_MATCH:소재", "PRICE_RANGE_MATCH", "RATING_HIGH"]
    }
  ],
  "overallComment": "요청 조건과 활용 목적을 함께 고려해 골랐어요.",
  "overallClaims": []
}
```

### LLM 소유 component

| component | 범위 | 의미 |
|---|---:|---|
| `semanticIntentFit` | 0..4 | 원문이 뜻하는 핵심 상품·스타일·문맥과의 적합성 |
| `useCaseFit` | 0..3 | 출근, 여행, 선물 등 사용 목적과 trade-off 적합성 |
| `profileFit` | 0..1 | 현재 요청을 만족하는 후보 사이의 개인화 tie-break |

이 component는 관측과 검증을 위해 남기지만 코드가 다시 고정 가중치로 정렬하지 않는다. 최종 순서는
LLM의 `ranked` 배열이며, 코드 신호와 semantic component를 함께 본 판단 결과다. 이렇게 해야 코드
점수를 만든 뒤 LLM이 최종 판단한다는 arm의 목적을 보존한다.

LLM은 다음을 할 수 없다.

- 코드 facts나 objective component 값을 다시 출력해 덮어쓰기
- 후보 밖 ID 사용
- 동일 상품 중복 선택
- `evidenceRefs`에 입력되지 않은 증거 생성
- 프로필이 없는데 `profileFit > 0` 사용
- 프로필 취향만으로 명시 query에 덜 맞는 후보 승격

## 추천 이유

추천 이유는 점수 숫자를 문장으로 바꾸지 않는다. 코드가 검증한 evidence와 LLM의 제한된 semantic
reason을 각각 검증한 뒤 최대 두 절로 조합한다.

### 코드 evidence 문구

초기 whitelist는 다음을 사용한다.

| evidence | 문구 예시 |
|---|---|
| `ATTRIBUTE_MATCH` | `요청한 린넨 소재와 일치하고` |
| `BRAND_MATCH` | `요청한 브랜드의 상품이며` |
| `PRICE_RANGE_MATCH` | `요청한 가격 범위 안에 있고` |
| `RATING_HIGH` | `평점 평가가 높은 상품이에요` |
| `REVIEW_MANY` | `리뷰 정보가 많은 상품이에요` |
| `PRICE_RELATIVE_LOW` | `같은 후보군에서 비교적 저렴해요` |

속성명과 값은 `effective_filters`와 `SpringProduct.attributes`의 검증된 교집합만 사용하고 기존
`_sanitize_reason` 상한과 문자 정제를 적용한다.

### semantic reason 문구

LLM은 자유로운 사실 주장이 아니라 제한된 enum을 반환한다.

| reason code | 문구 예시 |
|---|---|
| `DIRECT_INTENT_MATCH` | `요청하신 용도에 잘 맞는 상품이에요` |
| `USE_CASE_MATCH` | `말씀하신 사용 상황에 활용하기 좋아요` |
| `PROFILE_TIEBREAK` | `요청을 만족하면서 평소 취향과도 가까워요` |
| `NO_SEMANTIC_REASON` | semantic 절 없음 |

`PROFILE_TIEBREAK`는 프로필이 있고 `profileFit=1`이며 다른 query component가 유효할 때만 허용한다.
프로필 원문이나 민감한 취향 내용을 이유에 복사하지 않는다.

### 조합 규칙

1. 사용자가 명시한 조건의 code evidence를 가장 먼저 선택한다.
2. 그다음 품질·가격 evidence 하나를 선택한다.
3. code evidence가 하나뿐이면 semantic reason 하나를 덧붙일 수 있다.
4. 전체 문장은 최대 두 절, 기존 `reason_max_len` 이하로 제한한다.
5. 증거 검증이 실패하면 해당 절만 제거한다.
6. 모든 근거가 실패하면 기존 중립 문구를 사용한다.

예:

```text
요청한 린넨 소재와 일치하고 예산 범위 안에 있어요.
```

```text
평점 평가가 높고 말씀하신 사용 상황에 활용하기 좋아요.
```

점수 자체는 사용자에게 노출하지 않는다.

## 순위와 fallback

정상 경로에서는 LLM이 최종 `ranked` 목록을 결정한다. 코드는 다음 순서로 출력 계약만 검증한다.

1. 후보 ID인지 확인한다.
2. 중복을 제거한다.
3. 최대 노출 수와 니즈별 상한을 적용한다.
4. 프로필 없는 요청의 `profileFit`을 검증한다.
5. evidence reference를 후보별 code evidence와 대조한다.

LLM 호출 실패나 유효 후보 0건이면 기존 검색 순위로 fallback한다. 부분 응답으로 최소 노출 수를
채우지 못하면 미선택 후보를 다음 결정적 순서로 보충한다.

1. 원래 search rank
2. product ID

보충 후보에는 검증된 code evidence만 추천 이유로 사용한다. LLM이 선택하지 않은 상품에 semantic
reason을 붙이지 않는다.

멀티 니즈에서는 기존 `need_of`, `per_need` 경계를 유지한다. 한 니즈가 전체 노출을 독점하지 않도록
LLM 출력 검증 뒤 니즈별 상한을 적용하고, 부족한 니즈는 해당 니즈의 검색 순위로 보충한다.

## 지연과 토큰

`structured`는 모든 후보의 evaluation을 출력하지만 `code_assisted`는 최종 노출 상품만 출력한다.
입력에는 code signals가 추가되므로 current보다 입력 토큰이 늘 수 있으나, 출력은 current와 비슷한
크기로 제한한다.

초기 구현은 별도 후보 shortlist를 추가하지 않는다. 현재 검색 top-K에서 다시 코드로 자르면 의미
적합성을 판단하기 전에 좋은 후보를 제거할 수 있기 때문이다. 실측 후 입력 토큰이 문제일 때만
search rank 기반의 별도 실험 arm으로 검토한다.

## 관측성

상품별 내부 decision에는 다음을 남긴다.

- code component와 evidence code
- LLM semantic component
- 최종 LLM rank
- evidence 검증 성공·강등 사유
- fallback 여부와 출처

원문 profile, 사용자 query 전문, 상품 속성 원문은 구조화 운영 로그에 새로 추가하지 않는다. 기존
평가 artifact에는 dataset 계약이 허용하는 범위에서만 저장한다.

## 테스트

### 단위 테스트

- 동일 입력에서 code signals가 결정적이다.
- 명시 조건 통과와 evidence 생성이 일치한다.
- 정보 부재는 0이 아니라 `not_applicable`로 남는다.
- 가격이 명시되지 않은 요청에서 낮은 가격을 자동 가산하지 않는다.
- 후보 밖·중복 ID와 잘못된 component 범위를 거부한다.
- 존재하지 않는 evidence reference를 강등한다.
- 프로필 없는 요청에서 `profileFit > 0`을 거부한다.
- 추천 이유가 최대 두 절과 길이·문자 정제 규칙을 지킨다.
- 부분 응답과 전체 실패에서 검색 순위 fallback이 결정적이다.
- 기존 세 arm의 prompt snapshot과 결과 계약이 바뀌지 않는다.

### 통합 테스트

- graph가 `effective_filters`, search rank, need map, total budget을 정확히 전달한다.
- 단일 목록과 멀티 니즈 목록 모두 최대 노출 수를 지킨다.
- 최종 `RecommendationListEntry.reasons`에 검증된 목록 내 상품 이유만 실린다.
- rerank 실패가 기존 degrade 경로와 SSE wire contract를 보존한다.

### 평가

1. 저장된 200-case dataset을 그대로 사용해 dataset hash와 라벨을 고정한다.
2. `current`, `structured`, `code_assisted`를 같은 모델·후보·seed에서 paired 비교한다.
3. primary metric은 nDCG@10, 보조 지표는 blind preference, top-1 agreement, hard-constraint
   violation, reason coverage, grounding downgrade, latency, 출력 토큰이다.
4. 기존 blind judge 데이터를 새 arm의 품질 근거로 재사용하지 않는다. 새 결과가 없으면 품질 우세를
   주장하지 않는다.
5. 유료 전체 평가는 별도 승인 전 실행하지 않는다. 구현 검증은 deterministic test와 작은 scripted
   fixture로 완료할 수 있지만 production 기본 변경은 paired live evidence 없이는 하지 않는다.

## 수용 기준

1. `code_assisted`가 기존 arm과 독립적으로 선택 가능하다.
2. 기존 세 arm의 동작과 기본값, 롤백 경로가 보존된다.
3. 코드와 LLM이 같은 component를 중복 소유하지 않는다.
4. 하드 조건 위반 후보는 점수로 복구되지 않는다.
5. 사용자 이유에 쓰인 객관 주장은 모두 code evidence로 검증된다.
6. 의미 이유는 제한된 enum이며 검증 실패 시 안전하게 제거된다.
7. LLM 실패 시 검색 순위 fallback과 검증된 code reason이 제공된다.
8. `code_assisted` 출력은 전 후보 evaluation을 요구하지 않는다.
9. 내부 점수와 프로필 원문은 사용자 wire에 노출되지 않는다.
10. 유료 평가 전에는 `code_assisted`를 더 우수하다고 주장하거나 기본값으로 전환하지 않는다.

## 기각한 대안

### 기존 structured의 component만 늘리기

LLM이 객관 사실을 계속 채점하고 전 후보 출력을 요구하므로 신호 소유권과 지연 문제가 남는다.
가중치 축만 늘어나 튜닝 공간과 설명 복잡도만 커질 가능성이 높다.

### 코드가 최종 순위까지 전부 결정하기

현재 backend relevance score와 후보별 profile similarity가 없고, 용도·스타일 같은 의미 적합성을
규칙으로 대체하기 어렵다. 검색순위와 인기 신호 위주의 current 유사 결과로 수렴할 위험이 있다.

### LLM 자유문장을 그대로 상세화하기

표현은 풍부해지지만 사실성 검증이 약해지고 상품 데이터에 없는 장점을 생성할 수 있다. 상세 설명은
검증된 evidence 조합으로 확장하고 자유 생성 범위는 제한한다.
