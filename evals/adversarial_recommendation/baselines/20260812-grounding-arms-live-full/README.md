# 2026-08-12 grounding A/B/C live baseline

PR #638의 buyer adversarial dataset 450 case를 실제 설정의 모델로 A/B/C 각각 실행한 단일
live baseline이다. 총 1,350개 결과를 생성했으며 실행 시점 Git commit은
`0500f48a9bcb4c28aa4441bd119bb12e439c7b24`였다.

## 실험군

- **A — `current`**: 기존 자유 형식 rerank prompt와 모델 reason을 그대로 표시한다.
- **B — `prompt_only`**: 구조화된 `reasonCode`/`evidenceFields`를 요청하지만 모델 reason을 표시한다.
- **C — `validated`**: B의 동일 응답과 순위를 재사용하고 검증된 결정론 템플릿을 표시한다.

A에서 얻은 decompose 결과를 B가 그대로 재사용했다. C는 B에서 파생했으므로 B/C 비교에는 추가
provider 호출이나 모델 샘플링 차이가 없다.

## 결론

구조화 prompt만으로도 이 평가에서 측정 가능한 unsupported reason 비율은
**10.87% → 3.62%**로 낮아졌다. validator가 표시 문구를 제한한 C는 **0%**였다. 반면 추천
집합은 A/B의 비교 가능한 447 case 전부에서 같았고, B/C 순위는 450 case 전부 같았다. 따라서
관측된 개선은 추천 후보 제거가 아니라 사용자에게 표시되는 근거문의 claim surface를 줄인 결과다.

다만 PR #638의 기존 자동 scorer는 주로 eligibility, coverage, injection rank invariance를
판정하며 reason grounding을 직접 점수화하지 않는다. 그래서 기존 verdict는 세 arm 모두
`pass 68 / fail 147 / review 232 / error 3`으로 동일하다. A/B/C는 기존 순위·필터 실패를
개선하는 실험이 아니며, 이 결과를 전체 추천 품질 개선으로 해석하면 안 된다.

## 핵심 결과

### 표시 reason grounding

| arm | unsupported / scorable reason | 비율 | 전체 reason | 숫자 claim signal |
|---|---:|---:|---:|---:|
| A `current` | 55 / 506 | 10.87% | 511 | 49 |
| B `prompt_only` | 20 / 552 | 3.62% | 559 | 17 |
| C `validated` | 0 / 552 | 0.00% | 559 | 0 |

이 표는 `evals.rerank_grounding.metrics.detect_unsupported_rationale`가 등록한 좁은 범위만
측정한다. 후보 fact는 case 원본, 기록된 I-1 query, 추출 filter, 실행 시점 tier 함수와 Settings로
사후 재구성했다. A reason 5개와 B/C reason 각 7개는 fact를 재구성하지 못해 분모에서 제외했다.
재구성 가능한 B decision은 기록된 `groundingDecisions`와 모두 일치했다.

검출기는 정확한 숫자와 rating/review/relative-price 등급 충돌만 본다. 숫자를 부정하거나 불확실성을
고지하는 문장도 숫자가 있으면 signal로 잡으므로, 0%는 임의의 의미적 진실성을 증명하는 값이 아니라
**등록된 claim family를 표시 문구에서 통제했다**는 뜻이다.

B의 구조화 decision 557개는 모두 `supported=true`였고 downgrade는 없었다. reason code 분포는
`RATING_HIGH 353`, `REVIEW_MANY 111`, `NO_VERIFIABLE_EVIDENCE 84`,
`PRICE_RELATIVE_LOW 9`였다. C의 0%는 invalid code를 대량으로 중립화한 결과가 아니라, 유효한
code에도 모델이 덧붙인 숫자·자유 문구를 결정론 템플릿으로 바꾼 결과다.

### 추천 결과 보존

- A/B 비교 가능 case: 447개
- 동일 추천 집합: 447/447 (100%)
- 완전히 동일한 순서: 429/447 (95.97%)
- B/C 동일 순위: 450/450 (100%)
- B/C reason이 달라진 case/item: 307 case / 476 item
- ranked item 중 reason 제공률: A 511/610 (83.77%), B/C 559/610 (91.64%)

### 기존 automatic verdict

각 arm에서 `fail` 147개가 그대로 남았다. A 기준 실패 check 수는 다음과 같다.

- `minimum_eligible_coverage`: 80
- `no_ineligible_recommendation`: 61
- `injection_rank_invariance`: 10

서로 겹치는 case가 있으므로 check 합계와 fail case 수는 다르다. prompt injection 순위 불변성
실패 10개도 세 arm에서 개선되지 않았다.

세 error case는 provider 오류가 아니라 decompose가 추천 외 intent로 분류한 경우다.

- `adv-numeric_hallucination-26-missing`: `general`
- `adv-numeric_hallucination-26-seed`: `order_status`
- `adv-numeric_hallucination-28-missing`: `general`

### 호출량과 지연

| 구간 | provider calls | input tokens | output tokens | p50 latency | p95 latency |
|---|---:|---:|---:|---:|---:|
| A decompose | 450 | 2,076,244 | 87,005 | 1,824 ms | 2,762 ms |
| A rerank | 350 | 225,765 | 65,597 | 2,594 ms | 4,034 ms |
| B rerank | 350 | 205,465 | 89,234 | 3,095 ms | 5,337 ms |
| C derived | 0 | 0 | 0 | N/A | N/A |

B rerank는 A rerank보다 총 token이 약 1.15% 많았고, p50/p95 지연은 각각 약 19.3%/32.3%
높았다. 전체 provider 사용량은 1,150 calls, 2,749,310 tokens였다.

### 비용 비교

실행 결과의 `costUsd`는 runner가 `RecordingLLM`에 price book을 주입하지 않아 `null`이지만,
모델 ID와 input/cached-input/output token usage는 모두 기록됐다. 실행일 기준 OpenAI 공식 단가를
사후 적용하면 usage 기반 API 요금은 다음과 같다.

- [`gpt-5-nano`](https://developers.openai.com/api/docs/models/gpt-5-nano): input $0.05 /
  cached input $0.005 / output $0.40 per 1M tokens
- [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna): input $0.20 /
  cached input $0.02 / output $1.20 per 1M tokens

| 비용 구간 | usage 기반 비용 |
|---|---:|
| 공통 decompose 450회 | $0.0491038 |
| A rerank 350회 | $0.1238694 |
| B rerank 350회 | $0.1481738 |
| C validator 추가 LLM 비용 | $0 |

동일한 450-case traffic을 arm 하나로 독립 운영한다고 가정하면 A는 **$0.1729732**, B와 C는
각각 **$0.1972776**이다. B/C는 A보다 **$0.0243044, 14.05%** 비싸다. decompose를 제외한
rerank만 비교하면 B가 A보다 **19.62%** 비싸다. 이 표본 분포를 1,000 request로 환산하면 A는
약 **$0.3844**, B/C는 약 **$0.4384**다.

실제 A/B/C 전체 실험은 decompose를 한 번만 공유하고 C를 B에서 파생했으므로 합계는
**$0.3211470**이다. 여기서 C의 실험상 한계비용이 $0인 것과 운영 C 비용이 B와 같은 것은
구분해야 한다. 운영 C도 B와 같은 구조화 rerank 호출을 하고 그 뒤 로컬 validator를 적용한다.

이 값은 기록된 usage와 공개 단가를 곱한 API 요금이다. 조직별 할인, 세금, 크레딧, 청구 반올림을
포함한 invoice 실결제액과 대조하려면 OpenAI billing export가 추가로 필요하다.

## 발표용 시간·토큰 분포

모든 표의 percentile은 정렬한 표본에서 `(N-1)×p` 위치를 선형 보간했다. B는 같은 case의 A
decompose latency/usage와 B 실행을 합친 **운영 동등값**이고, C는 운영에서 B와 같은 structured
rerank와 validator를 실행하므로 B와 같은 값으로 표시한다. 이번 실험에서 파생한 C의 실제
`providerCalls=[]`, `latencyMs=null`을 운영 성능으로 오인하지 않는다.

### 전체 450 case pipeline proxy

추천 외 intent와 rerank 미실행 조기 종료를 포함한다. 시간은 평가 harness에서 decompose 직전부터
`done`까지이며 실제 HTTP, 프론트 렌더링, 운영 Spring 네트워크를 포함한 사용자 E2E는 아니다.

| arm | N | 평균 | 최소 | p50 | p95 | 최대 |
|---|---:|---:|---:|---:|---:|---:|
| A `current` | 450 | 4,098.5 ms | 1,391 ms | 4,303.5 ms | 6,183.6 ms | 9,825 ms |
| B `prompt_only` | 450 | 4,558.9 ms | 1,390 ms | 4,702.0 ms | 7,375.3 ms | 10,671 ms |
| C `validated` | 450 | 4,558.9 ms | 1,390 ms | 4,702.0 ms | 7,375.3 ms | 10,671 ms |

### rerank 적용 350 case 완료시간

A/B/C grounding 차이가 실제로 적용된 요청만 남긴 비교다.

| arm | N | 평균 | 최소 | p50 | p95 | 최대 |
|---|---:|---:|---:|---:|---:|---:|
| A `current` | 350 | 4,712.3 ms | 2,962 ms | 4,526.5 ms | 6,343.9 ms | 9,825 ms |
| B `prompt_only` | 350 | 5,304.5 ms | 3,251 ms | 5,049.5 ms | 7,667.7 ms | 10,671 ms |
| C `validated` | 350 | 5,304.5 ms | 3,251 ms | 5,049.5 ms | 7,667.7 ms | 10,671 ms |

### rerank provider call 350회

| arm | N | 평균 | 최소 | p50 | p95 | 최대 |
|---|---:|---:|---:|---:|---:|---:|
| A `current` | 350 | 2,739.6 ms | 1,272 ms | 2,594.0 ms | 4,033.5 ms | 8,057 ms |
| B/C structured | 350 | 3,332.1 ms | 1,467 ms | 3,094.5 ms | 5,336.7 ms | 7,555 ms |

### rerank call당 token

`cached input`은 두 arm 모두 0이었다. `reasoning`은 `output`의 부분집합이므로 total에 다시
더하지 않는다. `total = input + output`이다.

| arm·metric | N | 평균 | 최소 | p50 | p95 | 최대 |
|---|---:|---:|---:|---:|---:|---:|
| A input | 350 | 645.0 | 569 | 636.0 | 697.0 | 705 |
| A output | 350 | 187.4 | 59 | 180.0 | 302.8 | 406 |
| A reasoning | 350 | 103.6 | 0 | 94.0 | 213.6 | 303 |
| A total | 350 | 832.5 | 628 | 823.5 | 976.4 | 1,100 |
| B/C input | 350 | 587.0 | 511 | 578.0 | 639.0 | 647 |
| B/C output | 350 | 255.0 | 60 | 233.0 | 440.7 | 636 |
| B/C reasoning | 350 | 143.7 | 0 | 119.0 | 304.6 | 511 |
| B/C total | 350 | 842.0 | 589 | 822.5 | 1,041.5 | 1,271 |

구조화 arm은 input이 평균 58 token 줄었지만 output이 평균 67.5 token 늘어 rerank 총량은 평균
9.5 token 증가했다. 지연 증가는 단순 총 token 차이보다 output/reasoning token의 증가와 함께
해석해야 한다.

### rerank 적용 요청당 전체 token

공통 decompose와 rerank를 합친 값이다. cached input은 input의 부분집합이고, 표의 total에는
중복 가산하지 않는다.

| arm | N | 평균 | 최소 | p50 | p95 | 최대 |
|---|---:|---:|---:|---:|---:|---:|
| A `current` | 350 | 5,638.9 | 5,422 | 5,625.5 | 5,794.1 | 5,949 |
| B `prompt_only` | 350 | 5,648.5 | 5,383 | 5,631.0 | 5,859.7 | 6,114 |
| C `validated` | 350 | 5,648.5 | 5,383 | 5,631.0 | 5,859.7 | 6,114 |

### rerank 적용 요청당 API 비용

| arm | N | 평균 | 최소 | p50 | p95 | 최대 |
|---|---:|---:|---:|---:|---:|---:|
| A `current` | 350 | $0.0004617 | $0.0002878 | $0.0004491 | $0.0006166 | $0.0007627 |
| B `prompt_only` | 350 | $0.0005311 | $0.0002983 | $0.0005057 | $0.0007546 | $0.0010106 |
| C `validated` | 350 | $0.0005311 | $0.0002983 | $0.0005057 | $0.0007546 | $0.0010106 |

전체 수치와 input/cached-input/output/reasoning/call-count별 분포는
[`operational_statistics.json`](operational_statistics.json)에 machine-readable 형태로 보존한다.

## 해석 한계와 다음 실험

- 단일 live run이므로 모델 변동성에 대한 신뢰구간은 없다.
- 기존 automatic verdict는 grounding 전용 지표가 아니다.
- 사후 grounding 검출기는 제한된 claim family만 다루며 semantic entailment 전반을 평가하지 않는다.
- 발표용 확증 결과에는 고정 모델/설정으로 3회 이상 반복하고, 층화 표본에 blinded human judge를
  추가해 `unsupported`, `helpfulness`, `specificity`를 함께 비교하는 것이 적절하다.

## 파일과 무결성

- `results.jsonl`: 1,350개 case-arm 원본 결과와 provider usage
- `summary.json`: runner의 verdict 집계
- `report.md`: runner가 생성한 사람이 읽는 보고서
- `run_manifest.json`: dataset/model/settings/source/Git provenance
- `operational_statistics.json`: 원본 결과에서 사후 계산한 시간·token·비용 분포와 정의

원본 생성 파일 SHA-256:

```text
report.md        645f0900b36ba88fd6b98038026f64548e43b0c12a2b7f76c617a40df4113941
results.jsonl    1a1efdfba4a56da2da224661acdf51fddb6e3d611b3e2894612db4175ce7a04a
run_manifest.json 64ae4eb1842a95691a4ed6396dc7bbd70a0a0b2ba2f39eaa262e22cd1e2b8b73
summary.json     124fc3ba35bb645c82642012db99bbc6bf1d837b260beb9c77b9370a0c4d473f
```
