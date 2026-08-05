# 이슈 #275 측정 하네스

[RESEARCH-TEACHER-275.md](../RESEARCH-TEACHER-275.md)가 인용하는 E1/E2/E3/E4 실측을 재현하는
스크립트다. 2026-08-05 실행 결과(수치·CI·비용)는 문서 본문에 있고, 여기 있는 것은 **그 수치를
낸 코드**다.

## 왜 `evals/` 가 아니라 `docs/research/` 아래에 있는가

`docs/lessons.md`(2026-08-04, `evals/intent_probe` 측정 하네스 유실 사고 — #234/#240 이 각각
정답지를 세션 scratchpad 에만 두고 리포에 커밋하지 않아, 나중에 하나가 유실되자 다른 정답지로
"채택 판정이 뒤집힌" 사고가 있었다)는 **실 LLM 측정으로 결정을 내렸으면 그 PR 에 하네스와
앵커를 함께 커밋한다**는 규칙을 남겼다. 이 조사도 실 LLM 측정(E3)으로 `no-go` 판정을 내렸으므로
같은 규칙이 적용된다.

정석적인 위치는 `evals/`(예: `evals/intent_probe/`)이지만, 이 작업 레인은 `evals/**` 수정이
금지돼 있다. 그래서 허용 경로인 `docs/research/` 아래 전용 디렉터리를 만들어 하네스와 앵커를
같은 PR 에 커밋한다. **후속 이슈에서 이 디렉터리를 `evals/research_275/` 같은 정식 위치로
이관하는 것을 제안한다** — 특히 `e1_analyze.py`/`e2_analyze.py`/`e4_analyze.py`가 임포트하는
`evals.*` 모듈과 나란히 두면 자연스럽다.

## 스크립트가 재는 것

| 스크립트 | 실 LLM 호출 | 재는 것 | 산출물 |
|---|:---:|---|---|
| `e1_analyze.py` | 없음(비용 0) | 커밋된 `pipeline`(teacher) ablation 산출물을 재분석 — teacher 반복 안정성(top-1 최빈값·top-5 Jaccard·Kendall tau-b), teacher nDCG@10 반복 분산, 후보 수 K 대 토큰/비용 선형회귀, 비용·캐시 요약, 단가 재현 | `e1_results.json`, `e1_report.md` |
| `e2_analyze.py` | 없음(비용 0) | student(6성분 선형결합) 용량 상한을 탐색 — **주의: 이 스크립트의 오라클 상한(Step2)은 축퇴 해(no-op)로 수렴하는 결함이 있다.** 원본을 보존한 것이며, 교정판은 `e4_analyze.py`다 | `e2_results.json`, `e2_report.md` |
| `e4_analyze.py` | 없음(비용 0) | `e2_analyze.py`의 축퇴 해 결함을 교정해 재측정 — 무력한 `recency` 축 제외 + 축퇴 배제 제약을 건 오라클 상한, no-op(=passthrough=productId 오름차순) 1급 baseline, teacher-fit 재측정, `#146` 규약 paired bootstrap 95% CI 4쌍 | `e4_results.json`, `e4_report.md` |
| `e3_run.py` | **있음(실제 과금)** | teacher 라벨 안정성·비용 실측 파일럿 — fast 티어로 합성 구매자 질의 12건 생성, smart 티어로 순서(identity/reversed/shuffled)×반복 2회 rerank 호출, 순서 민감도 vs 반복 민감도, 콜당 비용·토큰·latency, `e1_analyze.py`의 K대토큰 회귀식과 K=20 교차 검증 | `synth_queries.json`(신규 실행 시 갱신), `e3_results.json`, `e3_report.md` |

`synth_queries.json`은 2026-08-05 파일럿이 실제로 생성한 합성 질의 12건의 **앵커**다(코드가 아니라
데이터) — teacher 라벨 안정성 수치가 어떤 질의로 측정됐는지 고정해 재현 시비를 없앤다. 재실행하면
`e3_run.py`가 이 파일을 덮어쓴다.

## 실행 명령

리포 루트에서 실행한다(각 스크립트가 `pyproject.toml`을 찾아 저장소 루트를 스스로 확정하므로
`cwd`는 그 아래 어디든 무방하지만, `evals`/`app` 임포트가 `uv` 가상환경에 설치돼 있어야 한다).

```bash
# 비용 0 — 순서대로 실행하면 e3_run.py 의 K=20 교차 검증까지 이어진다
uv run python docs/research/research-275-harness/e1_analyze.py --out /tmp/research-275/e1
uv run python docs/research/research-275-harness/e2_analyze.py --out /tmp/research-275/e2
uv run python docs/research/research-275-harness/e4_analyze.py --out /tmp/research-275/e4

# 실 LLM 호출 — 아래 경고를 먼저 읽는다
uv run python docs/research/research-275-harness/e3_run.py --out /tmp/research-275/e1 \
  --hard-budget-usd 0.50
```

`--out`은 필수 인자다(스크래치 경로 하드코딩을 없애기 위해 의도적으로 기본값을 두지 않았다).
**리포 안(특히 이 디렉터리)을 `--out`으로 쓰지 않는다** — 산출물은 실행마다 달라지는 생성물이라
버전관리 대상이 아니다. `e3_run.py`를 `e1_analyze.py`와 같은 `--out`으로 실행하면
`e1_results.json`을 찾아 K=20 토큰 예측을 자동으로 대조한다(없으면 그 비교만 스킵).

## 경고 — `e3_run.py`는 실 LLM 비용이 드는 수동 도구다

- **CI 에서 돌리지 않는다.** `uv run pytest`가 이 스크립트를 실행하지 않고, 이 하네스 전체에
  단위 테스트가 없다 — 실행하면 매번 실제 과금이 발생하기 때문이다.
- 하드 예산 상한은 `--hard-budget-usd`로 명시한다(기본값 `$0.50`). 콜 전마다 이 상한을 확인해
  넘기면 즉시 중단하고 부분 산출물을 남긴다. 2026-08-05 파일럿은 스모크 테스트에
  $0.0019432499999999999 를 먼저 써서 이 스크립트 실행분의 실제 상한을 $0.49805675 로 낮춰
  돌렸다 — 그날 실행에만 해당하는 예산 배분이며 다음 실행의 기본값이 아니다.
- 실행 전 `.env`에 provider API 키가 설정돼 있어야 한다(`get_llm()`이 `None`이면 즉시
  `status: failed`로 종료하고 손으로 질의를 대체하지 않는다).
- 실행하면 fast 티어 12콜 + smart 티어 최대 72콜(질의 12 × 순서 3 × 반복 2)이 나간다.
  2026-08-05 실행의 실제 지출은 $0.073069(상한 $0.498)였다.

## 산출물 형식

세 스크립트 모두 `<out>/e{N}_results.json`(원시 수치, 프로그램이 읽기 쉬운 형태)과
`<out>/e{N}_report.md`(사람이 읽는 요약, 본문에 인용되는 표와 문장 그대로)를 쌍으로 낸다.
`e3_run.py`는 추가로 `<out>/synth_queries.json`(합성 질의 원문과 카테고리 seed 선택 근거)을 낸다.

## 판정에 영향을 주는 고정 축

수치를 재현하려면 아래 값을 그대로 써야 한다 — 바꾸면 다른 실험이 된다.

- **seed**: `e1_analyze.py`는 입력 파일을 그대로 재분석해 seed가 없다. `e2_analyze.py`/
  `e4_analyze.py`는 골든 오라클 탐색 `seed=20260803`, teacher-fit 탐색 `seed=20260804`(+1)로
  동일해 두 스크립트의 결과가 직접 비교된다. `e3_run.py`는 카테고리 선택·후보 구성·셔플에
  전부 `seed=20260803`을 쓴다.
- **탐색 예산**(`e2_analyze.py`/`e4_analyze.py`): random search N=2000 + coordinate descent
  top10 × 2 passes, grid step 0.05.
- **`e4_analyze.py`가 `recency`를 자유축에서 뺀 이유**: `ScoringBuyerAdapter`가
  `recency_by_product=None`으로 구성돼 이 축은 실행 경로상 항상 0이다(주입되지 않는 무력한
  축). `e2_analyze.py`의 오라클 탐색은 이 축에 검증자 통과에 필요한 값을 몰아주고 나머지
  실질 신호를 전부 0으로 만드는 축퇴 해(=productId 오름차순 no-op)로 수렴했다 — `EvaluationSettings`
  검증자가 "5개 양의 신호 가중치 중 하나 이상 양수"만 요구해 이 코너를 막지 못하기 때문이다.
  `e4_analyze.py`는 `recency=0` 고정 + "4개 실질 축 중 최소 하나 0.05 이상" + "no-op과 최소
  1케이스 다른 순위" 제약으로 이 축퇴를 배제한다.
- **`e3_run.py` K=20**: `e1_analyze.py`의 K대토큰 회귀식과 교차 검증하려면 K를 바꾸지 않아야
  한다(바꾸면 회귀식 예측치와의 대조가 다른 K에서 이뤄져 검증 의미가 없어진다).
