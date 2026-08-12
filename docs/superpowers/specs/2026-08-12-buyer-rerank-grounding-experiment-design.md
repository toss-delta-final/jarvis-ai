# Buyer Rerank Grounding Experiment Design

## 요약

구매자 그래프의 첫 개선 대상으로 `rerank`가 생성하는 추천 근거를 선택한다. 현재 경로는 후보 밖
`productId`를 코드로 제거하지만, 자연어 `rationale`이 실제 후보 데이터에 근거하는지는 프롬프트에만
맡긴다. 이 문서의 목적은 문구를 바로 교체하는 것이 아니라 다음 세 팔을 같은 케이스에서 비교해
**프롬프트 개선 효과와 코드 검증 효과를 분리**하는 것이다.

- **A — current**: `origin/dev`의 자유문장 근거와 현재 ID 검증;
- **B — prompt-only**: 허용된 근거 유형과 근거 필드를 구조화해 출력하도록 프롬프트만 변경;
- **C — prompt + validator**: B 출력에 결정적 검증기를 적용하고 검증된 근거만 코드 템플릿으로 표시.

사람 blind pairwise 평가는 제외한다. 결정론 회귀 테스트와 실 LLM 반복 결과만 사용하며, 새 결과는
[#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)의 고정된 C1~C4를 바꾸지 않는
**탐색적 보조 실험**으로 보고한다.

## 왜 이 개선부터 하는가

최신 기준선 `origin/dev@8a4259eeaeef9d0a2a7279a56841de0c238f392c`을 다시 확인한 결과:

- `profile_injection_scope` 기본값은 이미 `rerank_only`라 프로필이 decompose 하드 필터로 새는 과거
  결함은 코드 불변식과 회귀 테스트로 보호된다.
- 과소지정 판정은 전용 `underspecified_classifier`로 분리되어 단일책임 개선의 before/after 근거가
  이미 있다.
- 반면 `app/agents/buyer/recommendation/rerank.py`는 후보 ID 부분집합만 코드로 강제하고, 파일
  docstring에 근거 속성의 결정적 대조가 후속임을 명시한다. 이는
  `/home/uuser/프롬프트-작성-및-피드백.md`의 “추천 이유가 실제 상품 데이터와 일치하는지 코드로
  검증” 원칙과 정확히 맞닿은 미해결 경계다.

따라서 이 실험은 기존에 해결된 문제를 다시 튜닝하지 않고, 현재 남아 있는 명시적 책임 공백을
검증한다.

## 목표

1. 프롬프트만 강화했을 때와 구조화 출력에 코드 검증을 결합했을 때의 효과를 분리한다.
2. 후보에 없는 숫자·브랜드·평점·리뷰·가격 근거가 사용자에게 노출되지 않게 한다.
3. 근거 검증 때문에 추천 ID 유효성, 추천 커버리지, 순위 품질, 지연, 비용이 악화되는지 함께 측정한다.
4. 결과가 좋아도 frozen release claim을 조용히 교체하지 않고 재현 가능한 탐색적 산출물로 남긴다.

## 비목표

- 검색·카테고리 매핑·후보 생성 품질 개선;
- 하드 필터, 조건 완화, 원래 조건 보존 정책 변경;
- 개인화가 추천 품질을 높인다는 주장;
- 전체 파이프라인이 단일 LLM보다 낫다는 주장;
- 사람 평가 또는 발표자 주관 점수;
- #152 staging 지연·비용 측정 대체;
- 기존 C1~C4 수치나 baseline artifact 수정.

## 출력 계약

### A — current

현행 출력 계약을 그대로 사용한다.

```json
{
  "ranked": [
    {"productId": 101, "rationale": "평점이 높고 리뷰가 많아요"}
  ],
  "overallComment": "..."
}
```

### B/C — structured evidence

각 순위 항목은 자유문장 대신 제한된 근거 코드와 그 근거가 참조하는 후보 필드를 낸다.

```json
{
  "ranked": [
    {
      "productId": 101,
      "rationale": "평점 평가가 높은 상품이에요",
      "reasonCode": "RATING_HIGH",
      "evidenceFields": ["ratingLevel"]
    }
  ],
  "overallComment": "..."
}
```

B는 `rationale`을 모델이 작성한 그대로 사용자에게 표시하고 구조화 근거는 평가용으로만 남긴다.
C는 모델의 `rationale`을 사용자에게 표시하지 않고, 검증을 통과한 `reasonCode`를 아래 코드 템플릿으로
바꾼다. 따라서 B와 C의 차이는 프롬프트가 아니라 **결정적 검증·표시 정책의 유무** 하나다.

초기 허용 코드는 결정적으로 대조 가능한 다음 세 개로 제한한다.

| reasonCode | 필요한 evidenceFields | 후보 데이터 조건 | 표시 템플릿 예시 |
|---|---|---|---|
| `RATING_HIGH` | `ratingLevel` | `높음` 또는 `매우높음` | `평점 평가가 높은 상품이에요` |
| `REVIEW_MANY` | `reviewLevel` | `많음` 또는 `매우많음` | `리뷰 정보가 많은 상품이에요` |
| `PRICE_RELATIVE_LOW` | `priceLevel` | `저렴` 또는 `매우저렴` | `같은 후보군에서 비교적 저렴해요` |

세 조건 중 어느 것도 성립하지 않으면 `reasonCode`는 `NO_VERIFIABLE_EVIDENCE`,
`evidenceFields`는 빈 배열로 출력할 수 있다. C는 이 값을 허용하되 사실 주장을 만들지 않는 중립
템플릿(`요청과의 관련도를 기준으로 추천했어요`)을 사용한다. 이 문구는 검색 관련성이 검증됐다는
주장이 아니라 상류가 제공한 후보를 재정렬했다는 동작 설명이다. B도 이 코드에서는 같은 중립
문구를 쓰도록 프롬프트로 지시하지만, 코드가 강제하지는 않는다.

`brand`, 정확한 가격, 정확한 평점, 정확한 리뷰 수는 LLM 입력에 없거나 표시값과 다를 수 있으므로
근거 코드로 추가하지 않는다. 새 근거 코드는 데이터 조건과 템플릿을 함께 검증하는 별도 변경 없이는
허용하지 않는다.

## 검증기 책임

C의 검증기는 각 항목에 대해 다음 순서로 처리한다.

1. `productId`가 후보 집합에 있고 중복이 아닌지 검사한다.
2. `reasonCode`가 enum에 있는지 검사한다.
3. `evidenceFields`가 해당 코드의 정확한 필드 집합인지 검사한다.
4. 후보의 tier 값이 코드별 조건을 만족하는지 검사한다.
5. 통과하면 코드 템플릿으로 `rationale`을 생성한다.
6. 근거만 실패하면 그 상품 ID를 버리지 않고 중립 템플릿으로 낮춘다.
7. 유효한 상품 ID가 하나도 없을 때만 현행처럼 `LLMError`를 발생시켜 상위 검색순서 degrade를 쓴다.

검증기는 순서를 새로 계산하거나 프로필을 해석하지 않는다. 순위 판단은 LLM 책임으로 남기고,
사용자에게 표시되는 사실 주장만 결정적으로 제한한다.

## 실험 데이터

별도 데이터 파일을 커밋하고 모든 팔이 동일한 `caseId`를 공유한다. 각 케이스는 `MFT`, `INV`,
`DIR` 중 하나를 명시한다.

### 필수 슬라이스

1. **missing facts (MFT)**: 평점·리뷰·가격 정보가 없거나 `정보없음`인 후보;
2. **boundary tiers (MFT)**: 허용 tier와 불허 tier가 함께 있는 후보;
3. **adversarial text (INV)**: 상품명·브랜드·query에 숫자나 지시문처럼 보이는 문자열을 넣되 구조화
   후보 사실은 동일한 쌍;
4. **profile conflict (INV)**: 프로필 문구만 바꿔도 명시 query에 관한 근거 사실은 바뀌면 안 되는 쌍;
5. **multi-need (DIR)**: 니즈별 노출 균형을 유지하면서 근거 검증이 적용되는 케이스.

데이터는 최소한 `datasetVersion`, `datasetHash`, `caseId`, `testType`, 입력 후보, query, profile,
기대 가능한 근거 코드를 포함한다. 데이터 해시가 달라지면 A/B/C를 모두 다시 실행한다.

## 지표

### Confirmatory primary

`unsupportedEvidenceRate`

- **분자**: 표시된 근거의 `reasonCode`/필드/후보 tier 조합 중 위 계약으로 지지되지 않는 항목 수;
- **분모**: 사용자에게 표시 가능한 유효 상품 ID를 가진 전체 랭크 항목 수;
- **목표**: C에서 `0`;
- **비교**: 같은 `caseId`의 A/B/C paired delta. A의 자유문장은 고정된 탐지 규칙으로 해당 세 속성에
  관한 명시 주장만 판정하고, 탐지하지 못하는 의미 오류는 `not measured`로 남긴다.

이 지표는 모든 자연어 진실성을 증명하지 않는다. 후보의 `ratingLevel`, `reviewLevel`, `priceLevel`에
대한 지원 여부만 증명한다.

### Hard gates

- `outOfCandidateIdCount = 0`;
- `duplicateIdCount = 0`;
- `invalidStructuredEvidenceCount = 0` for C after validation;
- `hardConstraintViolationRate`가 같은 후보 입력에서 증가하지 않음;
- `validRankCoverage`가 A보다 5%p 넘게 감소하지 않음;
- LLM/파서/타임아웃 실패를 모델 판단 오류와 분리해 기록.

### Exploratory secondary

- nDCG@10, MRR, expose count, neutral-template rate;
- rationale coverage;
- p50/p95 latency, input/output tokens, cost 또는 `cost_unknown_reason`;
- 슬라이스별 unsupported rate;
- B 대비 C의 validator downgrade 횟수.

작은 라이브 표본의 nDCG 차이는 confirmatory claim으로 승격하지 않는다.

## 실행 단계와 중단 조건

1. **결정론 단계**: ScriptedLLM으로 A/B/C parser·validator·fallback·manifest를 검증한다.
2. **screening**: 같은 소규모 개발 케이스에서 각 팔 N=3을 실행한다.
3. B 또는 C가 hard gate를 깨면 그 후보는 중단하고 실패 산출물을 보존한다.
4. screening을 통과한 후보만 고정된 개발셋에서 N=8, 독립 run 2회로 확인한다.
5. C가 primary 0을 달성하고 hard gate를 지키면 production 후보로 채택한다.
6. 효과 방향이 run마다 바뀌거나 커버리지/순위 회귀가 gate를 넘으면 `inconclusive` 또는 `rejected`로
   보고하고 production 기본값을 바꾸지 않는다.

라이브 실행은 CI에 넣지 않는다. API 자격 증명이 없거나 공급자 오류로 실행하지 못하면 deterministic
결과만으로 production 채택을 주장하지 않는다.

## 재현성과 PromptOps

각 run manifest는 다음을 포함한다.

- git commit과 dirty 여부;
- arm, prompt ID/version/hash;
- validator version;
- model provider/model ID/tier와 가능한 경우 model snapshot;
- temperature/reasoning 설정, 반복 수, seed 지원 여부;
- datasetVersion/datasetHash와 case count;
- 시작·종료 시각, 성공/실패/degrade count;
- latency/token/cost 또는 unknown 사유;
- 실행 명령과 raw artifact 경로.

원본 응답과 판정 결과를 분리해 저장하고, report와 차트는 raw artifact에서 재생성한다. baseline과
candidate의 prompt/model/dataset hash가 섞이면 비교기는 실패해야 한다.

## 구현 경계

승인 후 첫 구현 계획은 다음 범위만 다룬다.

- `rerank`의 실험용 A/B/C 출력·검증 seam;
- 구조화 근거 타입과 결정적 validator/template;
- 커밋된 fixture, deterministic runner, unit tests;
- 실 LLM 수동 runner와 manifest/report 생성;
- 결과를 기본 production 동작과 분리하는 명시적 arm 설정.

기본 arm은 실험 결과가 채택 기준을 통과하기 전까지 A로 유지한다. B/C를 구현했다는 이유만으로
production prompt를 바꾸지 않는다.

## 발표 산출물

1. A/B/C 구조도: `free text` → `structured prompt` → `structured + validator`;
2. `unsupportedEvidenceRate`와 guardrail을 함께 보이는 막대/표;
3. validator가 근거만 중립화하고 상품 순서는 보존한 실제 사례;
4. run manifest에서 자동 생성한 재현 명령·비용·지연 표;
5. 결론 상태 `supported / inconclusive / rejected / not tested`;
6. “전체 자연어 진실성”이 아니라 “세 구조화 상품 속성의 표시 근거”만 검증했다는 한계.

## 위험과 완화

- **구조화 출력이 랭킹 자체를 흔들 수 있음**: A/B/C paired 실행과 nDCG·coverage gate를 둔다.
- **중립 템플릿이 과다해 설명력이 떨어질 수 있음**: neutral-template rate를 별도 보고하고 높으면
  채택하지 않는다.
- **A 자유문장 탐지기가 의미 오류를 놓칠 수 있음**: 측정 가능한 세 속성만 primary 범위로 선언하고
  탐지 불가능한 오류를 0으로 간주하지 않는다.
- **작은 표본을 과장할 수 있음**: N=3은 screening, N=8×2도 탐색적 확인으로만 사용한다.
- **#154 frozen claim과 섞일 수 있음**: 별도 artifact namespace와 exploratory 라벨을 강제한다.
- **validator가 유효 상품까지 제거할 수 있음**: 근거 실패는 ID 제거 대신 중립 템플릿으로 낮춘다.

## 수용 기준

1. 동일 `caseId`·datasetHash에서 A/B/C를 재현 가능하게 실행할 수 있다.
2. C의 표시 근거는 validator 이후 `unsupportedEvidenceRate = 0`이다.
3. 후보 외 ID·중복 ID gate는 0을 유지한다.
4. C의 valid rank coverage 감소가 A 대비 5%p 이내다.
5. deterministic tests가 parser, 코드별 tier 경계, 잘못된 evidence, 중립 fallback, 빈 유효 ID degrade를
   모두 잠근다.
6. live 결과에는 두 독립 run, 원본 응답, manifest, 비용/지연 또는 unknown 사유가 남는다.
7. 결과 보고서는 primary, guardrail, negative result, 한계를 함께 표시한다.
8. frozen release claim artifact는 수정하지 않는다.

## 구현 완료 조건

구현은 단순히 프롬프트가 바뀌었을 때가 아니라, deterministic 회귀가 통과하고 screening과 가능한
confirmatory run의 raw artifact가 생성되며, 수용 또는 기각 판정이 보고서에서 자동 재생성될 때
완료된다. 실 LLM 실행이 불가능하면 production 기본 arm은 A로 남고 상태는 `not tested`다.

## Self-Review

- **기준선**: 최신 `origin/dev` commit과 현재 미해결 trust boundary를 명시했다.
- **인과 분리**: A/B/C가 현행, prompt-only, prompt+validator 효과를 각각 분리한다.
- **정직한 지표**: primary의 분자·분모와 검증 가능한 세 속성의 한계를 적었다.
- **회귀 방지**: ID, coverage, 순위, latency, cost를 guardrail/secondary로 보존했다.
- **평가 규약**: trivial current baseline, `caseId`, MFT/INV/DIR, dataset hash, 결정론/라이브 분리,
  raw artifact 재생성을 반영했다.
- **발표 범위**: #154 C1~C4를 바꾸지 않고 exploratory appendix로만 사용한다.
- **단계 경계**: 이 변경은 설계 문서만 추가한다. production 코드와 테스트는 승인 후 별도 계획에서
  변경한다.
