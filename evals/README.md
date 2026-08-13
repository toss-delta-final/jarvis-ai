# evals/ — 평가 커버리지 맵과 공통 규약

> 정본: 이 문서 (이슈 [#328](https://github.com/toss-delta-final/jarvis-ai/issues/328) 에픽에서 확정).
> 모든 `evals/` 하위 하네스와 그 자식 이슈(#329~#336)는 이 규약을 따른다. 규약과 어긋나는
> 하네스를 발견하면 이 문서를 고치지 말고 해당 하네스에 이슈를 걸어라 — 규약 개정은 에픽
> 계보(#328 후속)로만 한다.

## 구매자 adversarial recommendation dataset

`evals/adversarial_recommendation/`은 구매자 `POST /chat` 추천 경로의 seven-category
behavioral dataset이다. 판매자 경로는 포함하지 않는다. 실제 `BuyerChatRequest`와
`SpringProduct` wire schema를 Pydantic으로 교차검증하며, 210 family를 450 minimal-mutation
case로 결정론 확장한다. category별 20%인 42 family의 직접 재검토 기록도 함께 검증한다.

- 정본: `evals/adversarial_recommendation/seeds/families.json`
- 생성물: `evals/adversarial_recommendation/cases/prototype.jsonl`
- 검증: `uv run python scripts/validate_dataset.py`
- 재현성: `uv run python -m evals.adversarial_recommendation.generator --check`
- 오프라인 실제 코드 경로: `uv run python -m evals.adversarial_recommendation --mode scripted --out <new-dir>`
- 실 LLM 행동 평가: `uv run python -m evals.adversarial_recommendation --mode live --out <new-dir>`
- 상세 설계와 확장 전략: `evals/adversarial_recommendation/README.md`

정량 threshold/missingness/family mutation은 CI gold다. 추천 이유의 의미적 충실성,
prompt-injection 불복종, unsupported claim 여부는 exact 문장 gold가 아니라 behavioral invariant로
분리한다.

## 왜 이 문서가 생겼나

#275 조사(`docs/research/RESEARCH-TEACHER-275.md`)가 랭킹 개선을 재려다 **계측기 자체의
한계**를 드러냈다: 튜닝된 결정론 스코어러(nDCG@10 0.617)가 "아무것도 하지 않는" 순서(no-op,
0.738)보다 유의하게 나빴는데(paired bootstrap 95% CI [0.040, 0.207]), 임의 순서 기준선을
등록한 적이 없어 아무도 몰랐다. teacher 가 no-op 보다 낫다는 것조차 현 골든셋으로는
`inconclusive` 다. 원인은 규모가 아니라 구조다(판별 유효 18건 중 9건이 후보 ≤10 — nDCG@10
컷오프가 작동 불능, 하드 네거티브 0). 같은 실패가 다른 축에도 숨어 있을 수 있다는 전제가
이 규약의 출발점이다.

## 공통 규약 (8항)

1. **trivial baseline 의무** — 축마다 "아무것도 하지 않는" 기준선을 1급 baseline 으로 사전
   등록한다. 랭킹=임의 순서(no-op) · 카테고리=임베딩 최근접 top-1 · 니즈 전개=항상 1개 ·
   필터=빈 필터. **그걸 못 넘으면 개선이 아니다.**
2. **`caseId` 척추 공유** — 단계별 데이터셋은 파일·버전·해시를 각자 갖되 `caseId` 를 공유해
   e2e 실패를 단계로 귀속한다. 하나의 거대 골든셋으로 합치지 않는다(`evals/intent_probe/
   README.md` 의 "숫자를 섞지 말 것" 원칙 승계).
3. **결정론은 CI, 확률은 수동** — ScriptedLLM/fixture 축은 CI, 실 LLM 반복 분포 축은 수동
   도구(`evals/intent_probe` 형태). 실 LLM 하네스는 CI 에 넣지 않는다.
4. **슬라이스 쿼터·표본 사전 산정** — 슬라이스별 목표 N 을 관측 분산에서 역산해 사전 등록.
   관측 sd 0.402 기준: CI 반폭 ±0.10 ≈ 63건 · ±0.05 ≈ 249건 · 현 효과(+0.045)를 95%/80%
   power 검출 ≈ 634건. **케이스 수를 늘리기 전에 분산을 줄이는 설계(후보 깊이·라벨 안정화)가
   먼저다.**
5. **다중 비교 통제** — primary confirmatory metric 1개(#146 규약), 사전 등록 슬라이스 2~3개만
   알파 보정해 confirmatory, 나머지는 산출물이 스스로 `exploratory` 라벨을 단다.
6. **테스트 유형 명시(CheckList)** — MFT(최소 기능, 라벨 필요) / INV(불변, 라벨 불필요) /
   DIR(방향, 라벨 불필요)를 데이터에 표기한다. INV·DIR 은 라벨 공수 없이 규모를 늘리는
   수단이다. 선례: #119 "회원 recall ≥ 게스트" 회귀 테스트(DIR).
7. **하네스는 그 PR 에 커밋** — 앵커는 데이터 파일, 스크립트는 그것만 읽는다
   (`docs/lessons.md` 2026-08-04 프로브 유실 사고 규칙).
8. **지표는 분자·분모 정의 동봉**(#260 규약) + **datasetVersion·datasetHash 변경 시 모든
   baseline 재실행** — 같은 이름 다른 정의, 다른 해시 점수의 직접 비교를 금지한다
   (`evals/goldenset/GUIDE.md`).

## 커버리지 지도 (2026-08-05 기준)

| 단계 | 자산 | 상태 | 담당 |
|---|---|---|---|
| intent 라우팅 | `evals/intent_probe` (53셀 × N=8, 런당 ≈$0.09) | 있음 | #260 고정 · #84/#300 확장 |
| 개인화 주입·과반영 | `evals/personalization` (5-arm × weight 5점) | 있음 | #147 |
| 지연·예산 | `evals/first_event_budget` · `evals/benchmark` | 있음 | #277/#289 · #151 |
| e2e 추천 품질(결정론) | `evals/metrics` + `evals/goldenset` | 있음 — **순위 판별력 부족** | **#333 (P0)** |
| e2e 실모델 | `evals/model_eval` | 있음 | #144 |
| rerank 표시 근거 grounding | `evals/rerank_grounding` (A/B/C, MFT/INV/DIR, 사람 평가 없음) | **탐색적 수동 프로브 — production 기본 current 유지** | 이번 실험 branch |
| rerank scoring prospective holdout | `evals/rerank_holdout_v2` (ranking 200 + 별도 safety 24) | **catalog-derived draft — 실제 2인 사람 검수·봉인 전 confirmatory 아님** | #631 후속 평가 |
| 경로 비교(ablation) | `evals/ablation` | 있음 | #146 |
| 카테고리 매핑·선택 | 없음(골든셋 슬라이스 9건에 얹힘) | **공백** | **#331** |
| 니즈 전개(legs) | 없음(#198 지표가 로그 관측뿐) | **공백** | **#332** |
| 필터 추출 단독 | `evals/filter_axes`(축별 P/R·trivial baseline·INV/DIR/pair probe·ablation 재채점) | 있음 | #334 |
| 기능 조합 커버리지 | 없음 | **공백** | **#335** |
| 과소지정 판정 축 | `evals/underspecified_probe`(30셀 × N=8, 런당 ≈$0.03~0.10, `--union` 은 더 큼) | 있음 — 기준선 n=3(#433) + union(전개 후 판정) 모드(#432, `baselines/README.md` 색인) | #380 · #433 · #432 |
| 완화(relaxation)·칩 | 유닛 테스트만 | 미착수 | #328 체크리스트 보존 |
| 예산 세트(#60)·priority(#281) | 유닛 테스트만 | 미착수 | #328 체크리스트 보존 |
| degrade 경로(임베딩 누락·rerank 실패·타임아웃) | 산발 | 미착수 | #328 체크리스트 보존 |
| 홈 추천(I-22) `rank_candidates` 품질 | 유닛 테스트만 — **#275 형 실패가 숨어 있을 수 있는 자리** | 미착수 | #328 체크리스트 보존 |
| buyer 추천 blind pairwise 사람 평가 | `evals/blind_pairwise` (수집 전 설계·배정·분석) | **사람 입력 전 준비 완료 — 실제 응답 없음** | **#153** |

## 착수 순서 (에픽 확정)

**#333**(P0 — 랭킹 주장 전부의 선행 조건) → **#331**(라벨이 싸고 실패가 잦음) → **#332**(#198
지표 정의 기존) → **#335**(산출물이 미정의 셀을 낳음) → **#334**. 병행: **#329·#330**(연구,
#333 설계 근거). 독립: **#336**(#335 가 찾은 첫 미정의 셀 — feat).

## 기존 산출물 주의 사항

- `evals/scoring` 의 `passthrough` 는 "검색 순위" 기준선이 아니라 **임의 순서** 기준선이다 —
  dev fixture 의 다항목 32/32건이 productId 오름차순이라서다. 해석 정정은 #333 이 v2 산출물에
  반영한다. #145 의 기존 산출물 원본은 이력으로 보존한다(개변 금지, v2 를 새 버전으로).
- 서로 다른 `datasetHash` 의 점수를 비교하는 문서·보고는 규약 위반이다.
