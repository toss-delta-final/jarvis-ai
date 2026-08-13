# Issue #631 Structured Hybrid Rerank Scoring Design

## 요약

구매자 추천의 현재 rerank는 LLM이 반환한 `ranked` 배열을 사실상 최종 순서로 사용한다. 후보 밖 ID와
중복은 코드가 제거하고 #632/#657의 grounding validator가 사용자 노출 근거를 검증하지만, **왜 그
상품이 그 순위인지**는 여전히 자유로운 listwise LLM 판단에 크게 의존한다. 검색기의 기존 relevance와
개인화 영향도도 최종 순위에서 분리해 관측하거나 제한하기 어렵다.

이 설계는 [#631](https://github.com/toss-delta-final/jarvis-ai/issues/631)의 구조화 scoring과 검색순위
fusion을 기존 경로 옆에 선택 가능한 arm으로 추가한다.

- `current`: 기존 LLM 직접 순위. 현재 동작을 그대로 보존한다.
- `structured`: LLM이 후보별 제한된 component만 평가하고 코드가 순위를 계산한다.
- `hybrid`: `structured` 순위와 기존 검색 순위를 RRF로 결합한다.

초기 구현은 production 기본 ranking arm을 `current`로 유지했다. 이후 dev 68-case와 prospective
draft 200-case의 paired 결과 및 별도 product 승인을 근거로 production graph 기본을
`structured`로 전환했다. 기존 `RERANK_GROUNDING_ARM`과 `RERANK_RANKING_ARM`은 계속 독립 축이며,
`current`는 즉시 롤백 경로로 보존한다.

## 문제 정의

최신 `origin/dev@8d07e8e7` 기준:

1. `app/agents/buyer/recommendation/rerank.py`는 LLM 응답의 `ranked` 순서를 그대로 소비한다.
2. `app/agents/buyer/recommendation/graph.py`는 rerank 전체 실패 시 검색 순서로 degrade하지만, 정상
   응답의 순위 판단을 재구성할 component는 받지 않는다.
3. 현재 프롬프트는 프로필을 동점 처리에만 쓰도록 지시하지만, 그 준수 여부와 실제 영향량을 계량할
   구조화 출력이 없다.
4. PR #646의 grounding C안은 상품별 사실 주장을 검증하지만 순위는 의도적으로 변경하지 않는다.
5. 검색 score와 LLM 판단은 scale과 calibration이 다르므로 원점수를 직접 합산할 안정적 기준이 없다.

따라서 이 변경의 목적은 LLM을 없애는 것이 아니라, LLM 책임을 **제한된 의미 평가**로 축소하고 순위
결정·검색 신호 보존·동점·fallback을 코드 책임으로 옮기는 것이다.

## 목표

1. 현재 발화 적합성, 필요 적합성, 프로필 적합성을 분리해 검증·관측 가능하게 만든다.
2. 프로필은 현재 발화를 만족하는 후보 사이의 tie-break 수준으로 제한한다.
3. LLM이 최종 순서를 독점하지 않도록 기존 검색 순위를 hybrid 결과에 보존한다.
4. 동일 입력과 동일 LLM 응답에서 코드 계산 결과가 결정적이어야 한다.
5. 부분·비정상 LLM 출력에서도 후보 집합과 검색 순서를 이용해 안전하게 복구한다.
6. A/B/C를 같은 dataset, catalog snapshot, model config에서 paired 비교한다.
7. #632/#657 grounding, 기존 wire contract, current ranking 경로를 회귀 없이 유지한다.

## 비목표

- 후보 검색기, pgvector 검색 방식 또는 Spring 검색 계약 변경;
- hard filter나 조건 완화 정책 변경;
- 프로필로 현재 발화의 명시 조건을 덮어쓰기;
- 행동 로그 기반 Learning-to-Rank 학습;
- 새로운 외부 dependency 도입;
- 실험 결과 없이 production ranking 기본값을 변경;
- #632/#657의 reason code, template 또는 overall claim 정책 재설계.

## 핵심 원칙

### 현재 발화 우선

`intentFit`과 `needFit`이 주된 의미 적합도를 구성하고 `profileFit`은 최대 1점만 더한다. 프로필에 잘
맞더라도 현재 요청에 덜 맞는 상품은 위로 올라갈 수 없어야 한다. 이는 현재 `_PROFILE_TIEBREAK`
프롬프트 계약을 코드 계산으로 좁히는 것이다.

### 검색 신호 보존

서로 다른 score scale을 직접 더하지 않는다. 검색 순위와 LLM rubric 순위를 RRF로 결합해 각 시스템의
상대 순위만 사용한다.

### 순위와 설명 검증 분리

scoring metadata가 잘못되면 해당 후보의 LLM 순위를 복구한다. grounding metadata가 잘못되면 PR
#646의 기존 정책대로 상품과 순위는 유지하고 표시 문장만 중립 템플릿으로 낮춘다. 설명 오류가 순위
오류로 전파되거나 그 반대가 되어서는 안 된다.

### 기존 경로 우선 보존

`ranking_arm=current`는 새 score schema나 parser를 통과하지 않는다. 현재 grounding arm에 따라
사용하던 기존 prompt, `ranked` parsing, fallback과 provenance 의미를 유지한다.

## 설정 계약

새 Settings 필드와 환경변수는 다음과 같다.

| Settings | Environment | 기본값 | 유효 범위 |
|---|---|---:|---|
| `rerank_ranking_arm` | `RERANK_RANKING_ARM` | `structured` | `current\|structured\|hybrid` |
| `rerank_rrf_alpha` | `RERANK_RRF_ALPHA` | `0.65` | `0.0 <= value <= 1.0` |
| `rerank_rrf_k` | `RERANK_RRF_K` | `60` | 양의 정수 |
| `rerank_scoring_reasoning_token_reserve` | `RERANK_SCORING_REASONING_TOKEN_RESERVE` | `4096` | 0 이상 정수 |

함수 `rerank()`의 직접 호출 기본값은 호환성을 위해 `current`로 둔다. Production graph는 기본이
`structured`인 Settings 값을 명시적으로 전달한다. 설정 오류는 Pydantic 검증으로 기동 전에 거부한다.

기존 `RERANK_GROUNDING_ARM=current|prompt_only|validated`는 변경하지 않는다. 조합의 의미는 다음과
같다.

| ranking arm | grounding arm | 순위 | 사용자 표시 근거 |
|---|---|---|---|
| `current` | 기존 3개 arm | 기존 동작 그대로 | 기존 동작 그대로 |
| `structured` | `current` | rubric 코드 순위 | 모델 문장 |
| `structured` | `prompt_only` | rubric 코드 순위 | 구조화 prompt의 모델 문장 |
| `structured` | `validated` | rubric 코드 순위 | 검증된 template/중립 문장 |
| `hybrid` | 기존 3개 arm | 검색순위 + rubric RRF | 선택한 grounding arm 정책 |

## LLM 출력 계약

`structured`와 `hybrid`는 동일한 scored prompt와 응답 schema를 사용한다. 두 arm의 차이는 LLM 호출이
아니라 응답 이후 코드 순위 계산뿐이다.

모델은 상위 노출 상품만 고르는 대신 **입력 후보 전부를 한 번씩 평가**해야 한다. 최종 노출 집합은
코드가 `structured` 또는 `hybrid` 계산 후 `expose_max`를 적용해 정한다. scored arm의 출력 token
예산은 따라서 `expose_max`가 아니라 `len(candidates)`에 비례시킨다. `current` arm의 기존 token 예산
계산은 변경하지 않는다.

```json
{
  "evaluations": [
    {
      "productId": 101,
      "intentFit": 4,
      "needFit": 3,
      "profileFit": 1,
      "rationale": "평점 평가가 높은 상품이에요",
      "reasonCode": "RATING_HIGH",
      "evidenceFields": ["ratingLevel"]
    }
  ],
  "overallComment": "...",
  "overallClaims": []
}
```

### Component 의미

| 필드 | 범위 | 의미 |
|---|---:|---|
| `intentFit` | 정수 0~4 | 사용자가 찾는 핵심 상품 의도·카테고리 적합성 |
| `needFit` | 정수 0~3 | 용도, 조건, 명시·암묵 선호 적합성 |
| `profileFit` | 정수 0~1 | 현재 요청을 만족하는 후보 사이의 프로필 tie-break |

Buyer rerank에는 항상 비어 있지 않은 request message가 있으므로 `intentFit`과 `needFit`은 활성 축으로
본다. `needFit`은 멀티니즈의 `need_of` 유무가 아니라 query 안의 용도·조건·선호를 평가한다.

프로필이 없으면 모델은 `profileFit=0`을 반환해야 하며 코드는 0만 허용한다. 프로필이 있는데도 해당
후보와 연결할 근거가 없으면 역시 0이다. 프로필 원문은 사용자 노출 evidence로 사용하지 않으며
`profileFit`은 내부 ranking component다.

기존 grounding의 `reasonCode`, `evidenceFields`, `rationale`, `overallComment`, `overallClaims` 의미는
유지한다. 새 scoring prompt가 grounding enum이나 validator 권한을 확장하지 않는다.
`overallClaims`와 최종 `overallComment`는 #657 경계에서 **코드가 계산한 최종 노출 목록**을 기준으로
다시 검증하므로, 모델이 예상한 순서와 hybrid 결과가 달라도 검증되지 않은 목록 전체 주장은 노출되지
않는다.

## 검증 및 정규화

Scoring validator는 grounding validator와 별도 pure module로 둔다. bool은 Python에서 int의 하위
타입이므로 score나 ID로 허용하지 않는다.

각 evaluation은 다음을 검증한다.

1. 객체 shape인지;
2. `productId`가 bool이 아닌 정수이며 입력 후보 집합에 있는지;
3. 같은 `productId`가 정확히 한 번만 평가됐는지;
4. component가 bool이 아닌 정수이고 각 범위 안인지;
5. 프로필이 없을 때 `profileFit == 0`인지.

후보 밖 ID는 기록 후 무시한다. 중복 평가된 ID는 어느 항목을 신뢰할지 LLM 배열 순서에 맡기지 않고
그 ID의 scoring 평가 전체를 무효로 하며 검색 순위로 복구한다. 점수 범위가 잘못된 후보도 같은 방식으로
복구한다.

Grounding validator는 scoring 유효성과 독립적으로 기존 규칙을 적용한다. 예를 들어 점수는 유효하지만
reason code가 틀린 항목은 순위 계산에 참여하고 표시 문장만 중립화한다.

## 순위 계산

모든 rank는 1부터 시작한다. `searchRank`는 rerank 입력 후보의 원래 검색 순서에서 고정한다. 평가용
후보 permutation이 LLM prompt 순서를 바꾸더라도 `searchRank`는 바꾸지 않는다.

### Rubric score와 LLM rank

```text
rubricScore = intentFit * 4 + needFit * 2 + profileFit
```

유효 평가 후보는 다음 순서로 `llmRank`를 부여한다.

1. `rubricScore` 내림차순;
2. `searchRank` 오름차순;
3. `productId` 오름차순.

LLM이 누락했거나 scoring 평가가 무효인 후보는 모든 유효 평가 후보 다음에 두고, 그 안에서는
`searchRank`, `productId` 순으로 `llmRank`를 부여한다. 따라서 부분 응답이 후보를 제거하지 않으며
hybrid에서는 좋은 검색 순위로 복구될 여지가 남는다.

### Structured arm

`structured`의 최종 순서는 `llmRank`다. 동점과 누락 후보 순서는 위 규칙으로 결정적이다.

### Hybrid arm

```text
finalScore = effectiveAlpha / (k + searchRank)
           + (1 - effectiveAlpha) / (k + llmRank)
```

프로필이 있을 때 `effectiveAlpha = alpha`다. 프로필이 없으면 전체 rubric 최대 23점 중 비활성
`profileFit` 1점의 몫을 검색 신호에 재분배한다.

```text
effectiveAlpha = alpha + (1 - alpha) * (1 / 23)
```

이 재분배는 profile이 없는 요청에서 LLM 영향력을 임의의 0점으로 채우지 않기 위한 것이다. 향후 실제
계약에서 다른 축이 명시적으로 비활성화되면 같은 최대점수 비율 공식을 일반화할 수 있지만 이번 범위에
추가하지 않는다.

최종 정렬은 다음 순서다.

1. `finalScore` 내림차순;
2. `searchRank` 오름차순;
3. `productId` 오름차순.

`alpha=0.65`, `k=60`, component 가중치 `4:2:1`은 구조적 불변식이 아니라 #631의 초기 실험값이다.
config와 artifact에 기록하고 실험 결과 없이 정답으로 주장하지 않는다. scored arm의 출력 예산에는
모든 후보 JSON 몫과 별도로 reasoning reserve를 더한다. 이 reserve는 `current` arm 예산에는 적용하지
않아 기존 경로의 provider 호출 계약을 보존한다.

## 오류 및 fallback

### Current arm

기존 오류 동작을 그대로 유지한다. JSON parsing 실패나 유효한 ranked ID가 하나도 없으면 `LLMError`가
상위 graph로 전파되고 검색 순서 degrade가 실행된다.

### Structured/Hybrid arm

- JSON 자체가 파싱되지 않거나 `evaluations`가 배열이 아니면 전체 검색순서 fallback;
- 유효한 scoring 평가가 하나도 없으면 전체 검색순서 fallback;
- 일부 평가만 잘못됐으면 유효 항목은 사용하고 무효·누락 후보는 검색 순서로 복구;
- 후보 밖 ID와 중복 ID는 절대 노출하지 않음;
- 후보 집합에 들어오기 전 적용된 hard constraint를 score로 우회하거나 새 상품을 추가하지 않음;
- grounding만 실패하면 상품·순위를 유지하고 기존 중립 rationale 정책 적용.

전체 fallback은 기존 graph의 `rerank_degraded`, trace marker, 사용자 고지와 provenance 규칙을 재사용한다.
부분 scoring 복구는 전체 LLM 실패와 구분해 내부 diagnostic에 기록한다.

## 내부 데이터와 관측성

외부 Spring push와 SSE wire contract는 변경하지 않는다. 내부 `RerankResult`에 각 후보의 결정 근거를
담는 구조를 추가한다.

```text
productId
searchRank
intentFit / needFit / profileFit
rubricScore
llmRank
finalScore
finalRank
scoreValid
fallbackReason
```

`current` arm은 기존 결과를 유지하고 scoring decision 목록을 비운다. `structured`/`hybrid`는 위 정보를
평가 runner와 trace에서 소비할 수 있게 한다. 새 scored prompt는 `rerank-scoring-v1`로 구분하고
ranking arm, alpha, k를 run manifest와 trace attribute에 기록한다. 외부 응답에 모델명, score 또는
algorithm version을 노출하지 않는다.

## 구현 경계

책임을 다음처럼 분리한다.

1. `rerank.py`
   - current prompt와 기존 parser 보존;
   - scored prompt 선택 및 LLM 호출;
   - grounding 결과와 ranking 결과 조립.
2. 새 pure scoring module
   - evaluation schema 검증;
   - rubric score, LLM rank, RRF, fallback decision;
   - LLM, Settings singleton, graph에 직접 의존하지 않음.
3. `state.py`
   - 내부 scoring decision 타입을 `RerankResult`에 추가.
4. `graph.py`
   - Settings의 ranking arm과 RRF config를 명시적으로 전달;
   - 기존 전체 fallback과 wire 조립 유지.
5. 평가 harness
   - A/B/C paired 실행, raw response 공유, permutation, report/manifest 생성.

관련 없는 graph refactor나 기존 grounding module 병합은 하지 않는다.

## A/B/C 평가 설계

Ranking 효과를 분리하기 위해 모든 arm에서 grounding은 `validated`로 고정한다.

- **A — Current:** 기존 listwise `ranked` 순서;
- **B — Structured:** 구조화 rubric과 코드 `llmRank`;
- **C — Hybrid:** B의 같은 scored 응답에 RRF 적용.

B와 C는 case/repeat별 동일한 raw LLM 응답 하나를 공유한다. C를 위해 provider를 다시 호출하지 않는다.
A는 기존 prompt 계약이 다르므로 별도 호출하되 같은 dataset, catalog snapshot, model config, case와 repeat
키로 paired한다.

### 데이터

- 순위 품질: buyer goldenset v2.3.0과 고정 catalog/search snapshot;
- grounding safety: 기존 450-case adversarial recommendation dataset;
- dataset version/hash가 달라지면 A/B/C 모두 다시 실행;
- holdout label은 기존 sealed 절차를 따른다.

### Primary

- nDCG@10 case-level paired delta;
- 고정 seed bootstrap 95% CI;
- CI가 0을 교차하면 `inconclusive`로 기록.

### Safety 및 integrity

- hard constraint violation rate;
- out-of-candidate ID와 duplicate 노출 수;
- invalid score/schema rate;
- unsupported evidence rate;
- evaluated coverage, partial fallback rate, full fallback rate.

### Stability

각 case의 동일 후보 집합을 최소 3개의 결정적 seed로 LLM prompt에 순열화한다. 원래 `searchRank`는
고정한다.

- top-3 Jaccard;
- top-1 agreement;
- 공통 후보의 rank correlation;
- seed별 fallback/invalid rate.

### Efficiency

- total latency와 rerank latency p50/p95;
- input/output token;
- cost/task 또는 명시적 unknown 사유;
- 성공·부분 fallback·전체 fallback coverage.

### 판정

결과는 `supported`, `inconclusive`, `regressed`, `not-tested` 중 하나로 기록한다. C가 A 대비 primary를
개선하더라도 hard gate를 깨거나 안정성이 악화되면 production 기본값 승격 근거로 쓰지 않는다.

## 테스트 전략

### Pure unit tests

- score 범위의 최소·최대와 bool 거부;
- 프로필 없음에서 `profileFit=0` 강제;
- 4:2:1 계산;
- searchRank와 productId 동점 처리;
- RRF alpha/k 경계;
- 누락, 후보 밖 ID, 중복, 부분 invalid 복구;
- 동일 입력의 결정성.

### Rerank contract tests

- `ranking_arm=current`의 기존 prompt와 결과가 변하지 않음;
- current ranking과 세 grounding arm 조합 회귀;
- structured/hybrid가 같은 scored prompt/schema 사용;
- scored arm이 모든 입력 후보를 평가하도록 요청하고 출력 예산이 후보 수에 비례함;
- grounding invalid가 순위를 바꾸지 않음;
- scored schema 전체 invalid 시 기존 graph 검색 fallback;
- hard constraint 밖 상품이 score로 복귀하지 않음.

### Graph/config tests

- Settings 기본 `structured`와 env `current` 롤백;
- 세 arm 허용 및 알 수 없는 arm 거부;
- alpha/k 유효 범위;
- production graph의 명시적 arm 전달;
- env 한 줄로 current rollback;
- 기존 SSE/Spring push snapshot 불변.

### Evaluation tests

- B/C raw response 공유 및 provider 추가 호출 없음;
- permutation seed 재현성;
- searchRank가 prompt permutation과 독립적으로 고정;
- case-level paired denominator와 CI 생성;
- raw artifact에서 report 재생성;
- dataset/model/prompt hash가 섞이면 비교 실패.

## Rollout

1. 초기 구현과 deterministic tests 단계에서는 `RERANK_RANKING_ARM=current`를 유지했다.
2. scripted smoke로 current 호환성과 fallback을 확인했다.
3. 고정 dev dataset에서 A/B/C screening과 CI를 생성했다.
4. 별도 200-case prospective draft에서 current/structured 방향성과 안정성을 확인했다.
5. artifact의 exploratory 한계와 latency 증가를 명시한 별도 승인으로 기본을 `structured`로 바꿨다.
6. 문제 발생 시 `RERANK_RANKING_ARM=current`로 즉시 복구한다.
7. 독립 2인 검수·sealed 평가를 후속 검증 gate로 유지한다.

2026-08-13 dev screening 결과는 `evals/rerank_scoring/baselines/20260813-dev-mft68-live-n3/`에
보존한다. 68 case×3 seed에서 structured의 current 대비 평균 ΔnDCG@10은 `+0.1257`, 95% CI는
`[+0.0801,+0.1702]`였고, hybrid `alpha=0.65,k=60`은 `-0.2470`, CI
`[-0.3410,-0.1499]`였다. 이는 structured를 sealed holdout 후보로 올리는 dev 근거이며 production
기본 전환 근거는 아니다. 같은 dev 응답으로 관찰한 낮은 alpha 후보는 holdout 전에 고정해야 한다.

고정 structured 후보를 candidate commit `a01dae74`에서 공식 unseal API로 한 번 평가한 결과,
holdout 순위 19 case의 평균 ΔnDCG@10은 `+0.0575`, 95% CI는 `[-0.0385,+0.1696]`였다. CI가 0을
포함하므로 release 판정은 `inconclusive`이며 production 기본을 변경하지 않는다. 결과 확인 뒤
holdout으로 가중치·prompt·arm을 다시 튜닝하거나 두 번째 후보를 실행하지 않는다.

이후 별도로 생성한 prospective draft 200 case×3 seed에서 structured는 current 대비 평균
ΔnDCG@10 `+0.1219`, 95% CI `[+0.0925,+0.1535]`였고 seed별 방향도 일치했다. member는
`+0.1900`, guest는 `+0.0537`, top-1 agreement는 `0.9250` 대 `0.6388`이었다. 다만 heuristic
draft라는 이유로 결과 status는 `exploratory`이며, structured p50 latency는 `11.845s`로 current
`3.992s`보다 높다. 이 한계를 수용한 product approval로 Settings 기본을 `structured`로 바꿨다.

## 위험과 완화

- **LLM component가 여전히 주관적임**: 제한된 정수 범위, 검색 fusion, paired 평가로 영향력을 제한한다.
- **구조화 prompt가 token/latency를 늘림**: 비용·지연을 관측하고 임계 초과나 장애 시 current로 롤백한다.
- **프로필이 현재 요청을 침범함**: 최대 1점, profile 없음 0 강제, intent/need 우선 회귀 테스트를 둔다.
- **부분 출력이 특정 후보를 제거함**: 무효·누락 후보를 searchRank로 복구한다.
- **RRF 상수가 임의 튜닝됨**: config와 manifest에 기록하고 고정 dataset paired 결과만 근거로 사용한다.
- **두 설정 축의 조합이 복잡함**: compatibility matrix와 조합 회귀 테스트를 둔다.
- **current 경로가 새 parser로 오염됨**: arm 분기를 schema parsing 전에 두고 legacy prompt snapshot을 잠근다.
- **평가 순열이 검색 순위를 바꿈**: prompt order와 searchRank를 별도 값으로 보존한다.

## 수용 기준

1. `RERANK_RANKING_ARM=current|structured|hybrid`를 기동 시 검증하고 production graph에서 선택할 수 있다.
2. Settings 기본값은 `structured`, `rerank()` 직접 호출 기본값은 호환성을 위해 `current`다.
3. current ranking + 기존 grounding의 prompt, 순위, fallback, wire contract 회귀가 통과한다.
4. structured scoring이 component 범위, 후보 ID, 중복, profile availability를 결정적으로 검증한다.
5. hybrid가 config 기반 RRF를 동일 입력에 대해 결정적으로 계산한다.
6. 누락·부분·비정상 score 출력에서 검색순위 복구가 동작한다.
7. grounding failure는 기존처럼 rationale만 강등하고 순위를 변경하지 않는다.
8. A/B/C가 동일 dataset/catalog/model 조건에서 paired 실행되고 B/C는 raw 응답을 공유한다.
9. nDCG@10, safety, stability, latency, token, cost와 95% CI가 raw artifact로 보존된다.
10. 결과와 재현 명령이 #154에서 소비 가능한 형태로 연결된다.
11. 관련 unit/integration/eval tests, Ruff, format, diff check가 통과한다.
12. 기본값 전환 근거·exploratory 한계·latency 비용·current 롤백 경로를 함께 기록한다.

## 결정 근거 요약

- **독립 설정 축**: #632는 설명 안전성, #631은 순위 품질 문제라 결합하면 인과와 롤백이 흐려진다.
- **tie-break 개인화**: 현재 발화 우선이라는 기존 계약을 정량적으로 제한한다.
- **RRF**: 서로 calibration되지 않은 search/LLM 원점수 대신 순위를 결합한다.
- **부분 fallback**: 현재 rerank의 유효 ID 보존 성질을 유지하고 단일 오류의 blast radius를 줄인다.
- **B/C 응답 공유**: 모델 표본 변동과 fusion 효과를 분리한다.
- **structured 기본 + current 롤백**: 채택 결정과 실험의 증거 수준을 구분하면서 복구 경로를 유지한다.

## Self-Review

- 미완성 표식이나 미정 숫자를 남기지 않았다. 초기 가중치와 RRF 값은 명시적으로 실험값으로
  분류했다.
- `current` 경로 보존, 독립 grounding, scored prompt 조합 사이의 책임이 모순되지 않는다.
- scoring 오류와 grounding 오류의 fallback 경계를 각각 명시했다.
- prompt permutation과 searchRank를 분리해 position-bias 측정이 fusion 정의를 오염하지 않게 했다.
- production 기본값 변경을 구현 완료와 분리해 별도 결과·승인·롤백 결정으로 기록했다.
- 새 dependency나 관련 없는 graph refactor를 범위에 넣지 않았다.
