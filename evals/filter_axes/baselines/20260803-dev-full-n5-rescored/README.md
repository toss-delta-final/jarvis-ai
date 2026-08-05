# ablation baseline `20260803-dev-full-n5` 축별 재채점(#334 R3-2)

`evals/ablation/baselines/20260803-dev-full-n5/`(원본, **읽기 전용·개변 금지**)의
`{pipeline,single_call,scoring}/results.json`의 `caseResults[]`(155행 = 31케이스×5반복)에
실린 `extractedFilters`를 골든셋 dev `expectedFilters`와 `caseId`로 조인해
`evals/filter_axes`(축별 P/R/F1)로 재채점한 **역사적 baseline의 재채점**이다 — 원본
파일은 한 바이트도 바꾸지 않았다(`git status`로 확인 가능). 반복(`repeat`)별로 나누지
않고 155행 전체를 **micro** 집계한다.

## 왜 존재하나

이 baseline이 처음 기록됐을 때 `filterAccuracy`(합집합 분모 단일값)는 pipeline 0.0669 ·
scoring 1.0으로 **왜 이렇게 벌어지는지 원인 축을 알 수 없었다**. 이 산출물은 `evals/filter_axes`
축별 지표가 그 격차를 원인 축으로 실제로 분해한다는 것을 커밋된 수치로 증명한다.

## 수치 (수용 기준, `results.json`과 정확히 일치)

| arm | meanFilterAccuracy | keyword valueStrict P/R | keyword presence P/R | category valueStrict R | price_max valueStrict P/R |
|---|---:|---:|---:|---:|---:|
| pipeline | 0.0669 | 0.019 / 0.019 (match 3, valueMismatch 152) | 1.000 / 1.000 | 0.000 (missing 20, precision None) | 0.962 (match 25, spurious 1) / 1.000 |
| single_call | 0.2777 | 0.441 / 0.387 | 1.000 / 0.877 | 0.900 (category valueStrict P 0.157, match 18, spurious 97) | 1.000 / 1.000 |
| scoring | 1.0000 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 | 1.000 / 1.000 |

scoring은 scripted 정답을 그대로 내므로 `support > 0`(dev에서 그 축이 한 번이라도 라벨된)인
축은 전부 1.000이다. `color`·`rating_min`은 이 dev split 에 라벨이 아예 없어(`support=0`)
분모 자체가 없다 — `0.0`이 아니라 `None`(`results.json`에서 확인 가능)이며, 이는 결함이
아니라 `evals/filter_axes/README.md`의 emptyAxisRule·분모 정의(규약8)가 의도한 동작이다.

## 합집합 단일값이 감춘 것

- **keyword**: pipeline은 presence 1.000인데 valueStrict는 0.019다 — 축 존재(무언가 검색어를
  냈다)는 거의 항상 맞지만, `semanticQuery` 폴백 어휘(decompose가 자유 의역한 문장)와
  골든셋 `keyword`(정확한 상품명 LIKE 문구)의 리터럴 불일치가 전체 벌점의 대부분을 차지한다
  — 합집합 단일값은 이 "존재는 맞는데 어휘가 다르다"는 성질을 구분하지 못하고 그냥 감점만 한다.
- **category**: pipeline은 소추출(recall 0.000, missing 20 — 카테고리를 아예 안 뽑음)인데
  single_call은 정반대로 과추출(spurious 97 — 안 물어본 카테고리를 임의로 채움)이다. 둘 다
  "카테고리 축이 나쁘다"로 뭉뚱그려지지만 **실패 방향이 정반대**라 고칠 지점도 다르다.
- **price_max**: 양쪽 다 정상(pipeline 0.962·single_call 1.000) — 가격 상한은 결정론 코드가
  강제하는 축이라 안정적이다(`evals/metrics/README.md`의 PR gate 범위와 같은 이유).

`limit`·`excludeProductIds`는 `axes.json`에서 축 정의 자체가 제외(`excludedFields`·
`evaluated:false`)라, 옛 산출물이 겪었을 수 있는 기계적 필드 문제(limit 기본값 등)와는
무관하다 — 애초에 이 재채점 분모에 들어가지 않는다.

## 재생성

```bash
uv run python -m evals.filter_axes.rescore_ablation --out evals/filter_axes/baselines/20260803-dev-full-n5-rescored
```

`--baseline`로 다른 ablation baseline 디렉터리를 겨눌 수 있다(기본값은 위 20260803 디렉터리).
`results.json`의 `source`에 원본 3개 arm `results.json`의 상대경로·SHA-256·
`datasetVersion`/`datasetHash`(arm 자체에서 읽음)를 동봉해 "무엇을 재채점했는지"를
체크아웃 없이 복원할 수 있다. `filter_axes_spec.json`은 `axes.json` 바이트 사본(기존
writer 관례, F3).
