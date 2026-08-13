# Jarvis AI 발표 평가·성능 파트 최종 구성

작성일: 2026-08-13  
대상: 이슈 #154 최종 평가 리포트·발표 산출물  
상태: 저장소에 커밋된 결과를 본문으로 사용한다. PR #677(#631)은 `dev`에 병합됐지만
탐색적·불확실 판정과 비용 단가 충돌은 그대로 표시한다.

## 1. 편집 원칙

- **Evidence 요약 슬라이드는 만들지 않는다.** 각 주장 바로 아래에 지표, 표본, 한계, 원본
  artifact를 붙인다.
- 서로 다른 dataset·model·seed·harness의 수치를 더해 하나의 종합 정확도나 평균으로 만들지 않는다.
- 개선되지 않은 결과와 약한 축도 숨기지 않는다. `supported`, `inconclusive`, `regressed`,
  `exploratory`를 원본 판정대로 사용한다.
- 지연은 측정 구간을 구분한다. provider call, AI pipeline proxy, HTTP E2E, staging 값은 서로
  대체하지 않는다.
- 비용은 실행일 단가와 usage가 모두 확인되는 run만 사용한다. dashboard 확인이 필요한 값은
  추정치로 표시한다.

## 2. 최종 슬라이드 구성

평가·성능 파트는 다음 **4페이지**로 구성한다.

1. 평가 테스트셋 설계
2. 추천 품질과 실험 의사결정
3. 안전성·신뢰성
4. LLM 호출 성능과 비용

별도 평가 설계 페이지와 Evidence 요약 페이지는 두지 않는다. 공통 측정 조건은 각 페이지
하단의 출처·한계 영역에 반복한다.

---

## 3. 페이지 1 — 평가 테스트셋 설계

### 헤드라인

> 정상 케이스를 늘어놓는 대신, 실제 실패 경계를 분리한 층화·mutation·prospective 평가셋을
> 만들고 hash와 seed로 재현 가능하게 고정했다.

### 본문 A — 기존 adversarial recommendation 데이터셋

- 210개 test family에서 **450개 minimal-mutation case** 생성
- base와 mutation을 쌍으로 보존해 무엇이 실패를 만들었는지 추적
- 42개 family(category별 20%)를 failure mode·atomicity·evidence sufficiency·non-triviality
  기준으로 직접 재검토
- 대표 축: evidence conflict, no evidence, 후보 집합 무결성, hard constraint, 간접 prompt
  injection

출처:

- `evals/adversarial_recommendation/README.md`
- `evals/adversarial_recommendation/cases/prototype.jsonl`
- `evals/adversarial_recommendation/seeds/families.json`
- `docs/final-report/evidence.md`

### 본문 B — #631 prospective holdout v2

```text
고정 상품 카탈로그 6,585개
        ↓ seed 631200
6개 ranking stratum
        ↓
Ranking 200건 + Safety 24건
        ↓
중복·제약·누수·파일 hash 감사
        ↓
Current / Structured / Code-assisted paired 비교
```

| 구성 | N | 세부 |
|---|---:|---|
| general | 48 | guest 40 / member 8 |
| budget_multi | 40 | guest 28 / member 12 |
| personalization | 48 | member 48 |
| repurchase | 24 | member 24 |
| long_tail | 24 | guest 24 |
| adversarial | 16 | guest 8 / member 8 |
| **Ranking 합계** | **200** | **guest 100 / member 100** |
| **Safety** | **24** | prompt injection·hard constraint·candidate integrity 각 8 |

- ranking case마다 중복 없는 후보 상품 30개
- 파일별 SHA-256, 전체 dataset hash, catalog hash 기록
- 기존 core와 최대 query token-Jaccard `0.40`; 차단 임계치 `0.85`
- 기존 공개 holdout label을 생성 과정에서 읽으면 감사 테스트가 실패

### 반드시 함께 말할 한계

- provenance는 `synthetic-catalog-derived`이며 production query log에서 뽑은 데이터가 아니다.
- 현재 200개 relevance label은 결정론적 heuristic 초안이다.
- `labelStatus=draft`, `confirmatoryEligible=false`다.
- 독립 검수자 2명의 각 6,000개 candidate judgment와 disagreement adjudication은 설계됐지만
  아직 완료된 사람 평가가 아니다.
- 따라서 “200건 사람 라벨 평가” 또는 “confirmatory 성능”이라고 표현하지 않는다.

출처(PR #677, `dev` 병합):

- `evals/rerank_holdout_v2/README.md`
- `evals/rerank_holdout_v2/dataset/manifest.json`
- PR: <https://github.com/toss-delta-final/jarvis-ai/pull/677>

---

## 4. 페이지 2 — 추천 품질과 실험 의사결정

### 헤드라인

> Agent 경로 자체의 효과는 확인했지만, 구조를 복잡하게 만든 새 rerank가 자동으로 더 좋은
> 결과를 만들지는 않았다.

### 좌측 — Agent 경로가 no-op보다 유효한가

| 지표 | 결과 |
|---|---:|
| paired case | 62 |
| mean ΔnDCG@10 | **+0.1084** |
| bootstrap 95% CI | **[+0.0632, +0.1557]** |
| 판정 | **pipelineWins / supported** |

- guest Δ `+0.1477`, member Δ `+0.0690`
- 두 slice 모두 Holm-Bonferroni 보정 후 유의
- no-op은 같은 노출 후보를 productId 오름차순으로 재정렬한 결정론적 비교군이며 추가 LLM 호출은 0

출처:

- `docs/specs/RELEASE-CLAIMS-139.md` C1
- `evals/ablation/baselines/20260805-dev-v2-full-n5/noop_comparison.json`

### 우측 — #631 구조화 rerank를 기본값으로 바꿀 것인가

Sealed holdout의 19개 ranking case × 3 seeds, arm당 57개 sample 결과다.

| 지표 | Current | Structured | 판정 |
|---|---:|---:|---|
| mean ΔnDCG@10 | 기준 | `+0.0575` | 95% CI `[-0.0385, 0.1696]` → **inconclusive** |
| top-1 agreement | 59.6% | **86.0%** | 안정성 개선 |
| Spearman | 0.561 | **0.884** | 안정성 개선 |
| p50 latency | **6.13s** | 14.76s | Structured 지연 증가 |
| p95 latency | **10.74s** | 18.23s | Structured 지연 증가 |
| hard/foreign/duplicate/partial fallback | 0 | 0 | 무결성 유지 |
| release decision |  |  | **keep current** |

보조 탐색 결과:

- 200-case synthetic blind judge: Current 167승, Structured 15승, 동률 17, 불안정 1
- Code-assisted 170 complete pairs: paired ΔnDCG@10 `-0.1471`, CI
  `[-0.1902, -0.1033]`
- Code-assisted blind 결과: Current 78승, Code-assisted 15승, 동률 58, 위치 교환 불안정 19
- 평균 노출 상품 수 `3.63 → 2.20`; 유용한 후보를 지나치게 적게 고른 것이 주요 실패 형태
- p50 `4.55s → 6.15s`, p95 `9.14s → 11.64s`

발표 결론:

> 구조화는 입력 순서 안정성을 높였지만 품질 개선의 신뢰구간이 0을 교차했고 지연도 증가했다.
> Code-assisted는 품질과 coverage가 함께 퇴행했다. 따라서 실험 옵션과 감사 가능성은 남기되 운영
> 기본값은 Current로 유지한다.

출처(PR #677, `dev` merge commit `a5837ede`):

- `evals/rerank_scoring/releases/20260813-holdout-structured-n3/report.md`
- `evals/rerank_scoring/releases/20260813-holdout-structured-n3/summary.json`
- `artifacts/rerank-scoring/current-code-assisted-180-seed11-v2/blind-codex/report.md`
- `evals/rerank_scoring/baselines/20260813-holdout-v2-blind-judge-gpt-5.6-sol/results.json`

슬라이드에는 병합 여부 대신 원본 판정인 `inconclusive`·`exploratory`·`regressed`를 표시한다.
병합은 실험 결과를 confirmatory로 바꾸지 않는다.

---

## 5. 페이지 3 — 안전성·신뢰성

### 헤드라인

> 잘 추천하는 것뿐 아니라 화면 경계, 후보 집합, 근거, 개인화가 사용자 의도를 침범하지 않는지
> 별도 축으로 검증했다.

이 페이지는 3열 카드로 구성하고 카드당 대표 수치 1~2개만 크게 표시한다.

### 카드 A — 컨텍스트·화면 경계

- 출고판 동일 세대 2회 `mainIntent`: **98.33% / 97.92%**
- `screenNoHallucination`: **100% / 100%**
- 화면 밖 상품 ID를 임의 확정하지 않고 재질문하는 경계를 별도 측정

정의 주의:

- `mainIntent`는 전체 intent 정확도가 아니라 cart control과 demonstrative를 합친 등록 축이다.
- 약한 축 `general`은 62.5% / 64.58%였으므로 전체 라우팅 98%라고 확대하지 않는다.

출처:

- `docs/specs/RELEASE-CLAIMS-139.md` C2
- `evals/intent_probe/baselines/fast-2026-08-07-430-v6-adopted-1/`
- `evals/intent_probe/baselines/fast-2026-08-07-430-v6-adopted-2/`

### 카드 B — 추천 근거·후보 무결성

- Validated unsupported evidence: screening **0/80**, confirmation **0/212**, **0/208**
- 두 confirmation 합산: A **28/411(6.81%)**, B **0/418**, C **0/420**
- 세 grounding run에서 후보 밖 ID, 중복 ID, validation 후 invalid evidence, unfilled cell 모두 0

해석 주의:

- B와 C가 모두 0이므로 validator의 추가 효과까지 분리해 주장하지 않는다.
- 등록한 평점·리뷰·후보군 상대가격·정확한 숫자 근거 축에 한정한다.

출처:

- `evals/rerank_grounding/README.md`
- `docs/final-report/evidence.md`

### 카드 C — 개인화 안전성

- 출고 설정 전후 overreach leak: **29/31 → 1/31**
- 개인화는 후보를 제거하거나 hard constraint를 보상하지 않고 순서 조정에만 사용
- 라이브 개인화 품질 lift는 CI가 0을 포함하므로 “개인화가 품질을 높였다”고 주장하지 않는다.

출처:

- `docs/specs/RELEASE-CLAIMS-139.md` C3
- `evals/personalization/`의 결정론·live baseline

---

## 6. 페이지 4 — LLM 호출 성능과 비용

### 헤드라인

> 호출 단계와 사용자 요청 전체를 분리해 지연, token, 비용을 같은 run에서 측정했다.

기준 artifact:
`evals/adversarial_recommendation/baselines/20260812-grounding-arms-live-full/`

### LLM provider 1회 호출

| 호출 단계 | N | 평균 지연 | p50 | p95 | 평균 input / output | 평균 total token | 평균 비용 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 조건 분해 · Fast (`gpt-5-nano`) | 450 | **1.96s** | 1.82s | 2.76s | 4,614 / 193 | **4,807** | **$0.000109** |
| 추천 재정렬 · Smart (`gpt-5.6-luna`) | 350 | **2.74s** | 2.59s | 4.03s | 645 / 187 | **832** | **$0.000354** |

- 조건 분해 input 중 평균 4,420 token은 cache read다. cached token은 input의 부분집합이며 total에
  다시 더하지 않는다.
- 비용은 run manifest에 기록된 실행일 단가를 usage에 적용한 API 비용이다.

### Rerank가 실행된 사용자 요청 1건

| 지표 | 결과 |
|---|---:|
| provider 호출 수 | 2회 |
| 평균 AI pipeline proxy | **4.71s** |
| p50 / p95 | 4.53s / **6.34s** |
| 평균 total token | **5,639** |
| 평균 비용 | **$0.000462 / request** |

측정 범위 주의:

- decompose 직전부터 done까지의 평가 harness pipeline proxy다.
- 실제 HTTP, FE 렌더링, 운영 Spring 네트워크 E2E는 포함하지 않는다.
- 서버·DB·검색 인프라 비용은 포함하지 않는다.

### #631 비용·token 처리

- PR #677의 sample-level `efficiency`는 arm별 provider usage를 `provider usage unavailable`로
  기록한다. 따라서 #631에서 arm별 “평균 token·평균 비용”을 만들지 않는다.
- PR #677은 Luna 단가를 input `$1/M`, output `$6/M`으로 변경했지만, 2026-08-13 현재 직접 확인한
  OpenAI 공식 Luna 모델 페이지는 input `$0.20/M`, cached input `$0.02/M`, output `$1.20/M`을
  표시한다: <https://developers.openai.com/api/docs/models/gpt-5.6-luna>.
- 이 충돌이 해소되기 전에는 PR의 `$1.4933~$2.9438` 비용 감사 범위를 발표 수치로 사용하지 않는다.
- #631에서는 같은 run 안의 p50/p95 비교만 사용한다.

원본:

- `operational_statistics.json`
- `results.jsonl`
- `run_manifest.json`
- `README.md`

---

## 7. 슬라이드 공통 하단 표기

각 페이지 하단에 다음 다섯 항목을 작은 글씨로 고정한다.

1. dataset 이름·version·hash
2. 표본 N과 반복·seed
3. provider·model과 측정 구간
4. 판정 상태와 95% CI 또는 denominator
5. 원본 artifact 경로와 한계

권장 형식:

```text
Source: <artifact path> · N=<n> · model=<model> · seed=<seed>
Scope: AI evaluation harness only · Status: supported|inconclusive|exploratory|regressed
```

## 8. 최종 발표 메시지

> Jarvis는 LLM을 많이 호출한 서비스가 아니라, 의미 판단은 LLM에 맡기고 제약·근거·권한은
> 코드로 검증한 서비스다. 직접 만든 평가셋과 paired 실험으로 Agent 경로의 효과를 확인했으며,
> 더 복잡한 rerank가 퇴행했을 때도 결과를 숨기지 않고 Current 기본값을 유지했다.

이 문장으로 평가·성능 파트를 마치고, 별도의 Evidence 요약은 반복하지 않는다.
