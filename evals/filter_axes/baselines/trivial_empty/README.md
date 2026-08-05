# trivial baseline — 빈 필터(#334)

`evals/README.md` 공통 규약 1항("아무것도 하지 않는" 기준선을 1급 baseline 으로 사전 등록)의
필터 축 버전이다. 모든 케이스에서 예측 필터를 빈 값(`{}`)으로 고정하고
`evals/goldenset` dev split(v2, #333 — 103건)의 `expectedFilters`에 대해
`aggregate_axis_metrics`를 돌렸다.

## 산출물

- `results.json` — 케이스 수·축별 집계.
- `filter_axes_spec.json` — 이 baseline을 채점할 때 쓴 `axes.json` 바이트 그대로의 사본
  (리뷰 F3: hash만으로는 당시 정규화 규칙을 복원할 수 없어 동봉한다).
  `results.json`의 `filterAxesSpec.sha256`이 이 파일의 SHA-256과 일치한다.

## 해석

- 모든 evaluated 축에서 `presence.precision = None`(예측이 0건이라 분모 자체가 0 —
  `0.0`으로 뭉개지 않는다). `valueStrict`도 동일하게 precision None.
- `presence.recall`·`valueStrict.recall`은 그 축이 dev에서 한 번이라도 라벨됐으면(`support>0`)
  항상 `0.0`(아무것도 못 맞혔으니)이고, dev에 그 축 라벨이 아예 없으면(`support=0`, 이
  데이터셋(v2, 103건)에서는 `rating_min`) recall 분모도 0이라 `None`이다 — support를 먼저
  봐야 recall 값을 올바르게 해석한다.
- `support`(=match+valueMismatch+missing)는 그 축이 dev 케이스에서 실제로 라벨된 횟수다 —
  축마다 라벨 빈도가 달라 support 도 다르다. v2(103건) 실측: keyword 103 · price_max 34 ·
  color 15 · category 4 · attr_conditions 3 · price_min 1 · brand 1 · rating_min 0
  (v1에서는 `color`가 0이었다 — v2에서 색상 라벨이 새로 생겨 `color`가 아니라
  `rating_min`이 "라벨이 아예 없는 축" 예시가 됐다).
- **이 baseline을 못 넘으면 개선이 아니다** — 모델 축별 지표(model_eval/ablation)를 이 값과
  대조해야 "빈 필터보다 나은가"를 판별할 수 있다.
- **다른 `datasetHash`의 값과 직접 비교하지 말 것**(#328 공통 규약8) — 이 파일은 v2
  `datasetVersion 2.1.0`/`datasetHash`(아래) 기준이다. v1 시절 수치(예: 옛 `color` support 0)와
  섞어 읽으면 "라벨이 늘었다/줄었다"를 잘못 해석하게 된다.

## 재생성

```bash
uv run python -m evals.filter_axes.make_trivial_baseline --out evals/filter_axes/baselines/trivial_empty
```

`datasetVersion`·`datasetHash`는 `evals/goldenset/manifest.json`에서 읽는다(하드코딩 아님) —
골든셋이 v2로 바뀌면 이 명령 재실행만으로 baseline이 새 데이터셋 기준으로 갱신된다.
