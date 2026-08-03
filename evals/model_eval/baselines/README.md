# Model-eval baselines

## `20260803-dev-full-n5/`

2026-08-03에 dev 31케이스를 각각 5회 실행한 불변 기준 산출물이다.

- dataset hash:
  `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- primary nDCG@10: mean `0.7856`, 95% CI `[0.6787, 0.8833]` (N=18)
- hard failure: 1건 — `buy-fail-0002`의 `nonRecommendIntent` 1/5회
  (실모델 라우팅 분산)
- hard-constraint violation(HCV): 0건
- cost coverage: `0.534` — 실행 당시 `gpt-5.6-luna` 단가가 pricing manifest에 없어
  unknown 규약 적용

이 디렉터리의 파일은 실행 당시 증거이므로 수정하지 않는다. 새 실행과 비교할 때는 다음처럼
디렉터리 자체를 baseline으로 지정한다.

```bash
uv run python -m evals.model_eval \
  --out artifacts/model-eval-candidate \
  --baseline evals/model_eval/baselines/20260803-dev-full-n5
```

`gpt-5.6-luna` 단가는 2026-07-30 OpenAI 발표 기준 USD 0.20/1M input,
USD 1.20/1M output으로 확보돼 이후 실행은 usage가 모두 있으면 cost coverage 100%가
가능하다. 이 baseline은 단가 항목 추가 전 실행이므로 cost coverage `0.534`를 그대로
보존하며, `run_manifest.json`에는 당시 pricing manifest 해시가 기록돼 있다.

이 baseline의 `filterAccuracy`는 `limit`와 `excludeProductIds` 같은 기계적 기본 필드를
제거하기 전 값이므로 이후 실행과 직접 비교하지 않는다. primary nDCG 순위 결과는 이
정규화의 영향을 받지 않는다. 또한 #142 비교 규칙에 따라 `datasetHash`가 다른 결과끼리는
직접 비교하지 않는다.
