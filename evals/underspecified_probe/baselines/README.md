# 기준선 색인 (#433)

`evals/underspecified_probe/baselines/` 아래 여러 판이 쌓인다. **인용 대상은 하나여야 한다**
(#433 체크리스트 4항) — 이 문서가 그 하나를 가리킨다.

## 정본 선언

| family | 디렉터리 | prompt sha12 | 정본 여부 |
|---|---|---|---|
| **A — 현행 dev 프롬프트** | `fast-2026-08-08-run1`·`run2`·`run3` | `e62fd0f6e03d` | ✅ **`#430` before · `#431` 전환 판단의 정본** |
| B — pre-#386 프롬프트(역사 기록) | `fast-2026-08-06`(run1, 커밋된 기준선)·`run2`·`run3` | `11c6fe3bfa0c` | ❌ 역사 기록. **`#430` after 와 대조하지 마라** |
| C — 티어 대조(현행 dev 프롬프트, smart) | `smart-2026-08-08-run1` | `e62fd0f6e03d` | 참고(원인 축 분해의 티어 대조 전용, 채택 판정 대상 아님) |

**정본은 A family 3판의 분포다 — 단일 판이 아니다.** 대표값 하나를 인용해야 하면
`fast-2026-08-08-run3`(missRate 112/112, 100.0%, `nonRecommendIntentCount={}` — 세 판 중
가장 "깨끗한" 판)를 쓰되, 분산이 있다는 사실(아래 §런 간 편차)을 함께 인용해라.

## 프롬프트 세대 분기

#433 착수 전 실측(sub-orchestrator)에서 이슈가 가정한 전제("세 판이 같은 `hashes.systemPrompt`
를 공유한다")가 깨져 있었다:

| 대조 | 커밋된 기준선(`fast-2026-08-06`) manifest | 현재 HEAD | 판정 |
|---|---|---|---|
| `hashes.anchorFixture` | `cdadb1ca…384b8c` | 동일 | ✅ |
| `hashes.underspecifiedProbeModules.metrics.py` | `ce1784fe…` | `f4ef7a8f…` | ⚠️ 다르지만 **주석 전용** |
| 나머지 9개 하네스 모듈 해시 | — | 전부 동일 | ✅ |
| `hashes.systemPrompt` | `11c6fe3bfa0c…` | `e62fd0f6e03d…` | ❌ **다르다** |

- **`metrics.py` 차이의 정체**: 커밋 `bb65ee3`("docs(eval): #380 하네스의 graph.py 인용을 줄
  번호에서 심볼로")가 `_recommend_pairs` 의 **docstring 만** 고쳤다
  (`git diff b1413dc bb65ee3 -- evals/underspecified_probe/metrics.py` 로 확인 — 코드 라인
  변경 0). 계측 동작은 동일하다 — #328 규약 8("계측기가 바뀌면 전 baseline 재실행")의 트리거가
  아니다.
- **`systemPrompt` 차이의 정체**: 커밋 `3547e43`(#386 `wishlist_view` intent 신설)이
  `app/agents/buyer/recommendation/decompose.py::_SYSTEM` 을 바꿨다 → **커밋된 기준선은
  pre-#386 프롬프트 세대다.**
- **옛 프롬프트는 재현 가능하다** — CLI 의 `--prompt-rev <git rev>` 로
  `798f0a965385bfdedbe20646c3e8a07ba73ea08b` 를 지정하면 산출물 `prompt.sha12` 가 정확히
  `11c6fe3bfa0c` 로 재현된다(아래 6판 전부 실측 확인, run_manifest.json 대조).

두 프롬프트 세대를 각각 굳혔다 — 다운스트림(#430 after 대조, #431 전환 판단)이 쓰는 before 는
현행 dev 프롬프트여야 하지만, #433 이 문자 그대로 요구한 "커밋된 기준선(옛 프롬프트)의 n=3"도
채웠다.

## 실행 6판

```bash
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-08-run1 --tier fast
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-08-run2 --tier fast
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-08-run3 --tier fast

uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06-run2 --tier fast --prompt-rev 798f0a965385bfdedbe20646c3e8a07ba73ea08b
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06-run3 --tier fast --prompt-rev 798f0a965385bfdedbe20646c3e8a07ba73ea08b

uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/smart-2026-08-08-run1 --tier smart
```

6판 전부 종료 코드 0 · `unfilledCells` 빈 배열 · `hashes.anchorFixture` ==
`cdadb1cac1d88c7d6fa2904ca2f5c29065ad4c6663e47d46cd7b8be2ea384b8c` · `prompt.sha12` 이
family 대로(A·C=`e62fd0f6e03d`, B=`11c6fe3bfa0c`) · 6판의 `hashes.underspecifiedProbeModules`
전부 동일(같은 계측기, sha256 축약 `8aa8fcb49940252d`) — 게이트 6항목 전부 통과, 폐기한 판 없음.

## 런 간 편차 — A family (`fast-2026-08-08-run1~3`, prompt `e62fd0f6e03d`, tier `fast`)

| 런 | `missRate` (분자/분모, 비율, CI95) | `falseAlarmRate` | `judgmentAccuracy` | 종료코드 | 관측 비용(부분합) |
|---|---|---|---|---|---|
| run1 | 110/111, 99.1%, [95.1%, 99.8%] | 0/104, 0.0%, [0.0%, 3.6%] | 105/215, 48.8%, [42.2%, 55.5%] | 0 | $0.0536(`unknownCostCallCount`=39) |
| run2 | 111/112, 99.1%, [95.1%, 99.8%] | 0/104, 0.0%, [0.0%, 3.6%] | 105/216, 48.6%, [42.0%, 55.2%] | 0 | $0.0533(`unknownCostCallCount`=40) |
| run3 | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0544(`unknownCostCallCount`=36) |

run1·run3 의 `missRate` 분모가 다른 것(111 vs 112)은 intent 라우팅의 확률적 산출 때문이다 —
run1 은 `under-nc-0002` 앵커 표본 1건이 `general` 로 라우팅돼(`nonRecommendIntentCount=
{"under-nc-0002": {"general": 1}}`) F-1 필터에서 confirmatory 분모 밖으로 빠졌다. run2·run3 은
`nonRecommendIntentCount={}`(전 표본 recommend 라우팅). 이것이 바로 이슈 본문이 지목한 "정의가
같아도 판마다 흔들릴 수 있는" 확률적 요소다 — 계측기 정의는 세 판에서 동일하다(§실행 6판의
모듈 해시 대조).

`falseAlarmRate` 는 세 판 모두 0/104 로 완전히 일치.

## 런 간 편차 — B family (`fast-2026-08-06` + `run2`/`run3`, prompt `11c6fe3bfa0c`, tier `fast`)

| 런 | `missRate` (분자/분모, 비율, CI95) | `falseAlarmRate` | `judgmentAccuracy` | 종료코드 | 관측 비용(부분합) |
|---|---|---|---|---|---|
| run1(커밋된 기준선) | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0510(`unknownCostCallCount`=38) |
| run2 | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0521(`unknownCostCallCount`=34) |
| run3 | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0541(`unknownCostCallCount`=26) |

세 판 모두 `missRate`·`falseAlarmRate`·`judgmentAccuracy` 가 소수점까지 완전히 일치한다
(`nonRecommendIntentCount={}` 도 세 판 동일) — pre-#386 프롬프트 세대는 이 데이터셋에서
관측 가능한 편차가 없었다.

## 티어 대조 — C (`smart-2026-08-08-run1`, prompt `e62fd0f6e03d`, tier `smart`)

| 런 | `missRate` (분자/분모, 비율, CI95) | `falseAlarmRate` | `judgmentAccuracy` | 종료코드 | 관측 비용(부분합) |
|---|---|---|---|---|---|
| smart-run1 | 56/104, 53.8%, [44.3%, 63.1%] | 0/104, 0.0%, [0.0%, 3.6%] | 152/208, 73.1%, [66.7%, 78.6%] | 0 | $0.2165(`unknownCostCallCount`=37) |

`missRate` 분모가 fast(111~112) 대신 104 인 것은 `under-cbs-0003` 앵커 표본 8건이
`cart_add` 로 라우팅돼(`nonRecommendIntentCount={"under-cbs-0003": {"cart_add": 8}}`) F-1
필터에서 제외됐기 때문이다.

## `blockingAxes` 조합 분포 표

### A family (fast, 신 프롬프트)

| 조합 | run1 | run2 | run3 |
|---|---|---|---|
| `semanticQueryIsFallback`(단독) | 97 | 100 | 102 |
| `categoryQueries;semanticQueryIsFallback` | 9 | 7 | 4 |
| `filters.attrConditions;semanticQueryIsFallback` | 4 | 4 | 6 |

### B family (fast, 구 프롬프트)

| 조합 | run1(커밋) | run2 | run3 |
|---|---|---|---|
| `semanticQueryIsFallback`(단독) | 92 | 94 | 94 |
| `categoryQueries;semanticQueryIsFallback` | 9 | 7 | 7 |
| `filters.attrConditions;semanticQueryIsFallback` | 11 | 10 | 9 |
| `filters.keyword;semanticQueryIsFallback` | 0 | 1 | 2 |

### C (smart, 신 프롬프트)

| 조합 | smart-run1 |
|---|---|
| `semanticQueryIsFallback`(단독) | 52 |
| `categoryQueries;semanticQueryIsFallback` | 4 |

조합이 판마다(그리고 프롬프트 세대 사이에) 흔들린다는 것을 위 표가 그대로 보인다 —
`filters.attrConditions` 조합은 A family 에서 4~6건, B family 에서 9~11건으로, 프롬프트
세대에 따라 크기가 뚜렷이 다르다(#380 README 의 F-2 규약대로 조합은 여기서 표로만 인용하고
산문으로 풀지 않는다).

## `semanticQueryIsFallback` 단독 비율의 티어 대조

미탐 표본 중 원인이 `semanticQueryIsFallback` **단독**인 비율(다른 축과 조합이 아닌 경우):

| family/런 | 단독/전체 미탐 | 비율 |
|---|---|---|
| A run1 | 97/110 | 88.2% |
| A run2 | 100/111 | 90.1% |
| A run3 | 102/112 | 91.1% |
| C smart-run1 | 52/56 | 92.9% |

**단독 비율 자체는 티어에 따라 크게 달라지지 않는다**(fast 88.2~91.1% vs smart 92.9%, 모두
88~93% 구간) — 미탐이 발생했을 때 그 원인이 `semanticQueryIsFallback` 하나로 좁혀지는 경향은
티어와 무관하다.

그러나 **`missRate` 자체(미탐이 발생하는 빈도)는 티어에 따라 극적으로 다르다** — fast 는
99.1~100.0%(사실상 항상 미탐)인 반면 smart 는 53.8%(CI95 [44.3%, 63.1%])로 거의 절반 수준
낮다. 즉 smart 티어는 fast 보다 훨씬 자주 `semanticQuery` 를 올바르게 채우지만(미탐 자체가
적다), **일단 미탐이 나면 그 원인 축 분해는 fast 와 비슷한 모양**(대부분
`semanticQueryIsFallback` 단독)이라는 뜻이다. `judgmentAccuracy` 도 smart(73.1%)가
fast(48.1~48.8%)보다 높다 — smart 티어가 되물음 판정 자체의 품질을 끌어올린다.

## 편차 판정 (#433 체크리스트 5항)

A family(정본) 3판의 `missRate` 최대-최소 차 = 100.0% − 99.1% = **0.9%p** —
**10%p 미만**이므로 `--n` 상향·앵커 증설 검토는 이 PR 의 범위 밖으로 남긴다(#328 규약 4,
검토만 여기 기록하고 실행하지 않는다). `judgmentAccuracy` 편차도 0.7%p(48.8%→48.1%)로 작다.
B family 는 편차 0%p(세 판 완전 일치)로 A family 보다도 안정적이다. 현재 N=8(셀당) 로도
confirmatory 축의 결론(미탐율 fast ≈100%, smart ≈54%)은 판마다 뒤집히지 않는다.

## 폐기한 판

없음 — 6판 전부 §게이트 6항목을 첫 시도에 통과했다.

## 재현 명령

§실행 6판 참조.
