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

## v2 재실행 사전 등록 (2026-08-05, dataset 2.1.0)

이슈 #333 Part 3 — 골든셋 v2.1.0(adjudication 반영본, 127건) 위에서 ablation baseline을
전면 재실행한다. 위 2026-08-03 결과는 v1(43건, dataset hash
`764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`) 기준이라 다른
datasetHash와 비교하지 않는다(§ 아래 "다른 datasetHash 비교 금지" 참조) — 이번 절이 v2
기준 새 사전 등록이다.

- **Arms**: `pipeline`·`scoring`·`single_call` 동일 3-arm(정의는 위 "Arm 정의" 절 그대로,
  confound도 동일하게 유효) + **파생 참조 `noop`**. `noop`은 실행 arm이 아니다 — 각 실행
  arm(주로 `pipeline`)이 그 케이스·반복에서 실제로 노출한 상품 집합(중복 제거 후)을
  productId 오름차순으로 재정렬한 것을 노출했다고 가정하는 결정론 파생값이다
  (`evals.metrics.runner._noop_output`/F-4b 정의 재사용, 추가 LLM 호출 0).
- **Dataset**: dev split, **MFT 케이스만**(67건) — `testType=INV`/`DIR`(36건)는
  `evals.metrics.runner._case_result`가 순위 지표에서 `notMft`로 이미 제외하므로, 비용·
  잡음 절감을 위해 arm 실행 자체의 케이스 로드 단계에서도 제외한다(측정 공간은 기존
  `nonDiscriminativeRanking`/`emptyRelevance`/`notMft` 제외 규약과 동일 — 새 제외 사유
  아님). MFT-only 필터는 `evals/ablation/ablation_config.json`의 신설 필드
  `caseTestTypeFilter: "MFT"`로 선언하고 `cli.py`가 케이스 로드 시 이를 읽어 적용한다
  (case-id 하드코딩 금지).
- **Config version**: `ablation-config-v3` — seed·MFT 필터 변경을 반영한 사전 등록 개정.
  기존 `ablation-config-v2`(v1 데이터셋, seed `20260803`)는 그대로 두고 새 버전만 쓴다.
- **반복 N=5**, **seed 20260805**, case order `caseId-asc`.
- **Primary confirmatory metric**: `overall.ndcgAtK.10` 1개(#328 다중비교 통제, 기존 규약
  유지).
- **confirmatory 판정 대상 비교는 1개**: `pipeline(teacher) − noop` paired bootstrap 95%
  CI(resamples 2000, `evals.model_eval.stats.bootstrap_mean_ci`와 동일 구현 재사용).
  **CI가 0을 배제하면 v2 성공(#275 재평가 조건 1)으로 판정하고, 0을 포함하면
  `inconclusive`로 정직하게 기록한다(성공 조작 금지)**. `scoring − noop`·
  `pipeline − scoring`은 같은 방식으로 병기하되 exploratory다(기존 3-arm 상호 비교와
  같은 라벨 규약).
- **사전 등록 슬라이스**: `guest`·`member`(각 ranking N=31 실측, confirmatory,
  Holm–Bonferroni m=2). `budget`은 N=12로 `MIN_CONFIRMATORY_SLICE_N`(30) 미달이라
  exploratory 자동 라벨(기존 `evals.metrics.runner` 문턱 규약 그대로 적용 — 이번 Part에서
  문턱을 바꾸지 않는다). 나머지 슬라이스는 전부 exploratory.
- **순위 제외 규약**: `nonDiscriminativeRanking`·`emptyRelevance`·`notMft`(#143) 그대로.
  **다른 datasetHash와 비교 금지** — 이번 v2 결과는 2026-08-03 v1 결과와 절대 나란히
  "개선/퇴보"로 해석하지 않는다(데이터셋 자체가 바뀌었으므로 델타가 시스템 품질 변화인지
  라벨링 변화인지 분리할 수 없다).
- **예산**: 이 실행 한정 상한 $5(이슈 본문 실측 기준 여유 2배). `MODEL_EVAL_MAX_COST_USD_PER_RUN`
  환경변수로 이 실행에만 주입하고 `app/core/config.py`의 전역 기본값(20.0)은 바꾸지 않는다.
  초과 예상 시 즉시 중단·보고.

### 결과 (2026-08-06 dev-v2 MFT-only full N=5)

#### 실행 좌표

- 기준 커밋: `e9be8ab055ddf5c29f0c4824aa2106e659daa24f`(#333 adjudication 반영 커밋)
- 작업 트리: `dirty=true` — 이번 Part 3 하네스 변경(`ablation_config.json`
  `ablation-config-v3`·`cli.py` MFT 필터·`app/pipelines/embedding.py` 배치 청크 수정 등)이
  아직 커밋되지 않은 상태. 정확한 소스·설정·프롬프트는 `run_manifest.json`의 `hashes`(
  `ablationModules`·`config`·`singleCallPrompt` 등) SHA-256으로 고정했다.
- 데이터셋: `2.1.0`, hash
  `904f90e93a1dbff797c7e8bc48f2a795f006d1e6b5405e753207c76adb8de273`(adjudication 반영본),
  dev **MFT-only 67건**, `caseId-asc`, 각 arm N=5, seed `20260805`, configVersion
  `ablation-config-v3`.
  - `datasetManifest` hash: `323962f3aac9dd87ae8f7dd1dd549f80afb89aa0484b60155ab45f25324dc531`
  - `singleCallPrompt` hash: `6cd96436f7a4fc22ab1f5e0d4948c033e3b2748fd2675045bbd96546aa4336a9`
- 모델: fast=`gpt-5-nano`/`minimal`, smart=`gpt-5.6-luna`/`medium`(pipeline·single_call
  공통).
- 예산: preflight 1340/1500 calls·$5.0로 승인됐고, 실제 **985 calls · 4,180,955 tokens ·
  $1.0113362**였다(상한 $5의 약 20.2%). calls/token/cost gate 전부 통과, budgetExceeded
  없음, hard failure 0건.
- scoring 임베딩 fixture: documents 1510/1517(결측 7건은 §2-1/`evals/scoring/baselines/dev-v2/README.md`
  참조 — 전부 injected 비정답이며 semantic degrade=0으로 순위가 낮아지는 방향이라 영향은
  무시 가능한 수준, 아래 "해석 한계" 참조), queries 103/103.

#### Arm별 품질

Primary는 `nDCG@10`이며 confirmatory다. 나머지 품질 지표는 exploratory다. 순위 판별력이
있는 62/67건이 순위 지표에 포함됐고(제외 5건은 `nonDiscriminativeRanking`/`emptyRelevance`
등 #143 계약), hard failure는 3-arm 전체에서 0건이다.

| Arm | nDCG@10 | P@10 | R@10 | MRR | Filter Accuracy | HCV | Hard failures |
|---|---:|---:|---:|---:|---:|---:|---:|
| `pipeline` | 0.738029 | 0.347742 | 0.725256 | 0.897778 | 0.113871 | 0.000000 | 0 |
| `scoring` | 0.440818 | 0.225806 | 0.460210 | 0.681336 | 1.000000 | 0.029851 | 0 |
| `single_call` | 0.706531 | 0.317742 | 0.679378 | 0.879785 | 0.279140 | 0.014925 | 0 |

#### Arm별 자원

| Arm | Model calls | Input tokens/case | Output tokens/case | Cost/case | Latency/case | Token/cost coverage |
|---|---:|---:|---:|---:|---:|---:|
| `pipeline` | 650 | 6,572.81 | 678.19 | `$0.00143353` | 8,435.03 ms | 1.0 / 1.0 |
| `scoring` | 0 | N/A | N/A | N/A | 6.43 ms | N/A |
| `single_call` | 335 | 4,689.97 | 539.49 | `$0.00158539` | 6,255.20 ms | 1.0 / 1.0 |

v1(2026-08-03) 대비 `single_call`의 token/latency 절감폭이 줄었다(당시 pipeline 대비
−48.5%/−23.7%, 이번엔 개별 arm 수치만 비교 가능 — **다른 datasetHash라 직접 delta 비교는
하지 않는다**).

#### Slice 하이라이트 (exploratory, budget 제외)

| Slice (ranking N) | `pipeline` | `scoring` | `single_call` |
|---|---:|---:|---:|
| guest (31) | 0.792078 | 0.463703 | 0.774359 |
| member (31) | 0.683981 | 0.417934 | 0.638703 |
| personalization (11) | 0.791400 | 0.393690 | 0.705588 |
| repurchase (7) | 0.550209 | 0.176159 | 0.651110 |
| budget (12, exploratory) | 0.697629 | 0.360635 | 0.664724 |

#### 3-arm 상호 비교 (exploratory)

| Pair | nDCG@10 delta | Bootstrap 95% CI | Verdict |
|---|---:|---:|---|
| `pipeline - scoring` | +0.297211 | [0.231781, 0.363905] | `pipelineWins` |
| `pipeline - single_call` | +0.031498 | [-0.004701, 0.066304] | `inconclusive` |
| `single_call - scoring` | +0.265713 | [0.193565, 0.339099] | `single_callWins` |

`pipeline - single_call`가 v1(2026-08-03, `pipelineWins`)과 부호는 같지만 이번엔
`inconclusive`다 — **다른 datasetHash 비교이므로 "품질 격차가 좁혀졌다"고 해석하지 않는다**
(데이터셋 자체가 다시 만들어졌다).

#### teacher−no-op CI 분석과 v2 성공 판정 (confirmatory)

`noop` 파생은 각 arm이 그 케이스·반복에서 실제로 노출한 `rankedProductIds`(중복 제거)를
productId 오름차순으로 재정렬한 결정론 값이다(F-4b, `evals.metrics.runner._noop_output`
재사용, 추가 LLM 호출 0). 산출물: `baselines/20260805-dev-v2-full-n5/noop_comparison.json`.

| Pair | paired N | mean delta | Bootstrap 95% CI | Verdict | Label |
|---|---:|---:|---|---|---|
| **`pipeline − noop`** | 62 | **+0.108369** | **[0.063244, 0.155651]** | **`pipelineWins`** | **confirmatory** |
| `scoring − noop` | 62 | +0.058414 | [0.024206, 0.094306] | `scoringWins` | exploratory |
| `pipeline − scoring`(noop 분석 경로 재계산) | 62 | +0.297211 | [0.232357, 0.363906] | `pipelineWins` | exploratory |

**판정: CI `[0.063244, 0.155651]`가 0을 배제한다 → v2 성공(#275 재평가 조건 1 충족)으로
판정한다.** teacher(pipeline)는 같은 노출 집합 위에서 임의 순서(no-op)보다 유의하게 높은
nDCG@10을 낸다 — 순수 순서 효과(rerank)가 통계적으로 유의하다.

**사전 등록 슬라이스 확인(guest·member, Holm–Bonferroni m=2 step-down)**: 관측
`|meanDelta|`가 큰 순으로 1단계(guest, alpha/2=0.025 → 97.5% CI)를 먼저 검정하고, 유의하면
2단계(member, alpha=0.05 → 95% CI)를 검정했다.

| 단계 | Slice | N | mean delta | CI(보정 신뢰수준) | 유의 |
|---|---|---:|---:|---|---|
| 1 (97.5% CI) | `guest` | 31 | +0.147731 | [0.059003, 0.242780] | 예 |
| 2 (95% CI) | `member` | 31 | +0.069007 | [0.018483, 0.124319] | 예 |

두 슬라이스 모두 Holm-Bonferroni 보정 후에도 유의해 전체 판정을 뒷받침한다.

#### 필요 N 재산정 (#328 규약)

관측 case-level delta 표준편차(sd)로, 같은 크기의 평균 delta를 alpha=0.05(양측)·검정력
80%로 검출하는 데 필요한 paired 표본수 `N = (z_{α/2}+z_β)² · sd² / meanDelta²`
(z_{α/2}=1.959964, z_β=0.841621)다.

| Pair | 관측 sd | 관측 meanDelta | 필요 N(80% power) | 현재 N | 여유 |
|---|---:|---:|---:|---:|---|
| `pipeline - noop` | 0.195854 | 0.108369 | 26 | 62 | 충분 |
| `pipeline - scoring` | 0.264161 | 0.297211 | 7 | 62 | 충분 |
| `scoring - noop` | 0.141634 | 0.058414 | 47 | 62 | 근소 |

`scoring - noop`(exploratory)만 현재 N=62가 필요 N=47에 근접해 여유가 크지 않다 — 향후 이
비교를 confirmatory로 승격하려면 반복 수를 늘리는 편이 안전하다. primary confirmatory인
`pipeline - noop`은 필요 N(26) 대비 현재 N(62)이 2.4배로 여유 있다.

#### 사전 등록 대비 일탈

없음. §1에서 등록한 arms·dataset·N·seed·caseOrder·primary metric·confirmatory 비교·슬라이스
라벨·순위 제외 규약을 그대로 실행했다. 예산 상한만 이 실행 한정으로 환경변수 주입했다(전역
기본값 불변, §1에 사전 고지).

#### 해석 한계 (v1과 공유)

- 위 "해석 한계" 절(2026-08-03)의 5개 항목이 이번 실행에도 그대로 유효하다(Arm B confound,
  Filter Accuracy 정의, TTFT unknown, HCV 분모 희석, dev-only).
- `pipeline - noop` 유의성은 **rerank(순서) 효과**가 유의하다는 뜻이지, `pipeline` arm 전체
  (decompose+검색+rerank)가 최선이라는 뜻은 아니다 — `scoring - noop`도 유의해(exploratory)
  결정론 scoring 경로 역시 no-op보다 순서를 개선한다.
- scoring 임베딩 문서 결측 7건이 `scoring`/`scoring-noop` 델타에 준 영향은 무시 가능한
  수준이다(비정답 7/1,517의 semantic 성분만 0으로 강등되며, 이는 해당 비정답의 순위를
  낮추는 방향이라 scoring arm에 불리하지 않다) — "영향 없음"이 아니라 방향이 결과를
  왜곡하지 않는다는 뜻이다.
