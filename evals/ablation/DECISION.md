# Recommendation pipeline ablation decision record

## 사전 등록

- Arms: `pipeline`, `scoring`, `single_call` 고정
- Config version: `ablation-config-v2` — 실 LLM 실행 전 마지막 사전 등록 개정
- Dataset split: dev만 사용
- 반복: 기본 N=5, 모든 arm에 같은 N 적용
- Seed: 20260803
- Case order: `caseId-asc`
- Primary confirmatory metric: `overall.ndcgAtK.10`
- Secondary quality: Filter Accuracy, hard-constraint violation rate, Recall@10,
  Precision@10, MRR. latency·token·cost와 함께 exploratory
- 순위 제외: #143의 `nonDiscriminativeRanking` 케이스 제외
- Missing run: pair에서 제외하고 전체 실행 대상 caseId 기준 `missingLeft`/`missingRight`/
  `missingBoth`, 그리고 양쪽 반복 표본 수를 보고
- 판정: case-level primary delta의 bootstrap 95% CI가 0을 포함하면 `inconclusive`
- hard-constraint violation rate는 hardFailure 행도 분모에 포함되어 희석될 수 있으므로
  hardFailureCount를 반드시 병기
- Arm C 필터 파싱은 field-lenient 규약을 쓴다. `brand` 문자열은 동일 의미의 문자열 배열로
  승격하고, 알 수 없거나 검증 실패한 필드는 그 필드만 드롭해 `filterParseWarnings`에 기록한다.
  드롭 필드는 Filter Accuracy에서 모델이 내지 않은 것으로 벌점 처리한다.
- 케이스 5×N=2 live smoke에서 `brand`/`attrConditions` 타입 오류를 실측해 프롬프트 타입
  규칙을 개정했으며, 이 문구는 전량 실행 전 확정한다. 측정 공간은 같아 config v2를 유지하고
  프롬프트 변경은 run manifest의 `singleCallPrompt` 해시로 식별한다.

## Arm 정의

- A `pipeline`: `LiveBuyerAdapter`를 재사용한 decompose(fast)→고정 fixture 검색→rerank(smart)→I-21 경로.
- B `scoring`: `ScoringBuyerAdapter`를 재사용한 scripted expectedFilters decompose + 결정론 scoring 경로.
- C `single_call`: smart tier LLM 한 번으로 필터 추출·후보 재랭킹·추천 이유 생성을 통합한 실험 경로.

## 알려진 confound

Arm B는 rerank만 바꾼 것이 아니라 decompose도 골든 라벨 `expectedFilters`를 쓰는 scripted 경로다. 기존 모듈을 소비만 해야 하므로 이 confound를 수정하지 않고 manifest와 결과 해석에 함께 기록한다.

## 비범위

`scoring + LLM rerank` 하이브리드 arm은 이번 3-arm 사전 등록 밖이며 후속 실험 후보로만 남긴다.

## 결과 (2026-08-03 dev full N=5)

### 실행 좌표

- 기준 커밋: `4d12348b679b515c57e83b81b1a0eabca0f1bc7c`
- 작업 트리: `dirty=true`. 위 커밋에 아직 커밋하지 않은 ablation 하네스가 더해진
  상태이며, 정확한 소스·설정·프롬프트는 baseline `run_manifest.json`의 SHA-256으로 고정했다.
- 데이터셋: `1.0.0`, hash
  `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`,
  dev 31건, `caseId-asc`, 각 arm N=5, seed `20260803`.
- 모델: fast=`gpt-5-nano`/`minimal`, smart=`gpt-5.6-luna`/`medium`.
- 예산: preflight 620/800 calls로 승인됐고, 실제 445 calls·1,106,237 tokens·
  `$0.28132815`였다. calls/token/cost gate를 모두 통과했으며 token·cost coverage는 1.0이다.

### Arm별 품질

Primary는 `nDCG@10`이며 confirmatory다. 나머지 품질 지표는 exploratory다.
순위 판별력이 있는 18건만 순위 지표에 포함했고, hard failure는 전체 155회 실행에서 집계했다.

| Arm | nDCG@10 | P@10 | R@10 | MRR | Filter Accuracy | HCV | Hard failures |
|---|---:|---:|---:|---:|---:|---:|---:|
| `pipeline` | 0.782943 | 0.240000 | 0.824444 | 0.877791 | 0.063519 | 0.000000 | 0 |
| `scoring` | 0.616852 | 0.222222 | 0.705556 | 0.791667 | 1.000000 | 0.032258 | 0 |
| `single_call` | 0.696084 | 0.207778 | 0.731852 | 0.816667 | 0.248333 | 0.032258 | 0 |

HCV는 전체 155 evaluated row 분모(top-level)이며, 순위 유효 18케이스 case-mean
(secondary summary, `scoring`·`single_call` 0.055556)과 정의가 다르다. 두 값 모두
위반 소스는 arm별 동일 1케이스로, `scoring`은 `buy-cmap-0004`, `single_call`은
`buy-repu-0001`이다.

### Arm별 자원

| Arm | Model calls | Input tokens/case | Output tokens/case | Cost/case | Latency/case | Token/cost coverage |
|---|---:|---:|---:|---:|---:|---:|
| `pipeline` | 290 | 4,170.103 | 540.529 | `$0.000897` | 6,362.729 ms | 1.0 / 1.0 |
| `scoring` | 0 | N/A | N/A | N/A | 3.781 ms | N/A |
| `single_call` | 155 | 1,993.871 | 432.510 | `$0.000918` | 4,851.826 ms | 1.0 / 1.0 |

`single_call`은 `pipeline`보다 총 token/case가 약 48.5% 적고 latency/case가 약 23.7%
낮았지만, 모든 호출이 smart tier라 case당 비용은 오히려 사실상 같은 수준
(`$0.000918` 대 `$0.000897`)이었다.

### Slice 하이라이트

아래 slice 수치는 표본이 작으므로 방향 탐색용이며 confirmatory 결론으로 쓰지 않는다.

| Slice (ranking N) | `pipeline` | `scoring` | `single_call` |
|---|---:|---:|---:|
| guest (9) | 0.787690 | 0.585590 | 0.750080 |
| personalization (7) | 0.758780 | 0.611740 | 0.581890 |
| personalization overreach (3) | 0.622300 | 0.657700 | 0.467440 |
| repurchase (2) | 1.000000 | 0.500000 | 0.815460 |

### 사전 등록 비교와 판정

모든 비교의 paired N은 18이다.

| Pair | nDCG@10 delta | Bootstrap 95% CI | Verdict |
|---|---:|---:|---|
| `pipeline - scoring` | +0.166 | [0.035, 0.320] | `pipelineWins` |
| `pipeline - single_call` | +0.087 | [0.022, 0.160] | `pipelineWins` |
| `single_call - scoring` | +0.079 | [-0.086, 0.265] | `inconclusive` |

`pipeline`은 `scoring`과 `single_call` 모두보다 primary quality가 유의하게 높았다.
`single_call`과 `scoring`의 차이는 CI가 0을 포함해 결론을 내릴 수 없다.

**결정: production의 현행 pipeline을 유지하고 single-call 전환은 기각한다.**
single-call은 latency와 token을 줄였지만 품질이 유의하게 낮고 실제 비용 이점도 없었다.
smart tier 가격이 충분히 낮아지거나, 후보 압축으로 smart 입력 비용을 더 줄이거나,
single-call 품질 개선이 별도 사전 등록 실험에서 확인되면 전환을 다시 검토한다.

### 해석 한계

- Arm B는 scripted `expectedFilters` decompose까지 포함하므로 reranker만의 인과 효과가 아니다.
- Filter Accuracy는 예측·정답 필드 합집합을 분모로 삼는다. `pipeline` 0.064와
  `single_call` 0.248의 차이는 빈번한 추가 필드도 벌점으로 세는 이 정의와 함께 읽어야 한다.
- 오프라인 하네스는 server first text token을 관측하지 않아 TTFT는 `unknown`이다.
- HCV는 hard failure 행도 분모에 포함해 위반율을 희석할 수 있으므로 반드시
  `hardFailureCount=0`과 함께 해석한다.
- 이번 결과는 dev-only이며 sealed holdout 확인 전에는 일반화된 release 근거가 아니다.
