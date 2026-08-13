# Rerank scoring paired evaluation

Issue #631의 `current`, `structured`, `hybrid` ranking arm을 같은 dev goldenset 후보에서
비교한다. `structured`와 `hybrid`는 한 번 받은 scored provider 응답을 공유하며, hybrid만
production `rerank()` API에 replay해 RRF 효과와 모델 샘플링 분산을 분리한다.

## Dry-run

```bash
uv run python -m evals.rerank_scoring \
  --arms all --split dev --repeats 1 --order-seeds 11,29,47 \
  --dry-run --out /tmp/rerank-scoring-dry
```

Dry-run은 runner와 artifact 재현성만 검증하며 `status=not-tested`다. 품질 개선 근거로
사용하면 안 된다.

## Live run

```bash
uv run python -m evals.rerank_scoring \
  --arms all --split dev --repeats 3 --attempt-multiplier 3 \
  --order-seeds 11,29,47 --alpha 0.65 --k 60 \
  --max-calls 2500 --max-cost-usd 20 \
  --out artifacts/rerank-scoring/live-001
```

`--case-ids buy-srch-0001,buy-srch-0002`로 범위를 제한할 수 있다. holdout은 이 CLI에서
열지 않는다. 출력 디렉터리는 새 경로여야 하며 `samples.csv`, `failures.csv`,
`results.json`, `run_manifest.json`, `report.md` 다섯 파일만 생성한다.

30개 후보를 모두 평가하는 scored prompt는 JSON 생성 전 reasoning token도 소비한다.
`RERANK_SCORING_REASONING_TOKEN_RESERVE`(기본 4096)는 structured/hybrid에만 추가되며,
기존 current arm의 출력 예산은 바꾸지 않는다.

## 보존된 live baseline

`baselines/20260813-dev-mft68-live-n3/`은 clean commit `aa8f85b0`, dataset `2.3.0`, eligible
MFT 68개와 seeds `11,29,47`의 A/B/C 결과다. case별 seed 평균을 paired bootstrap했다.

| 비교 | 평균 ΔnDCG@10 | 95% CI | 판정 |
|---|---:|---:|---|
| current → structured | +0.1257 | [+0.0801, +0.1702] | supported |
| current → hybrid(alpha=0.65, k=60) | -0.2470 | [-0.3410, -0.1499] | regressed |
| structured → hybrid | -0.3727 | [-0.4513, -0.2898] | regressed |

Structured는 47 case 개선·8 동률·13 악화였고 hard-constraint 위반은 세 arm 모두 0건이다.
Structured/hybrid는 204/204 표본이 생성됐으며 동일 raw response hash를 공유한다. current는
출력 길이 오류가 두 cell에서 재시도 후에도 남아 202/204 표본이다. 이는 dev screening 결과이며
sealed holdout이나 production 승격 결과가 아니다. 초기 RRF 0.65는 기각 대상이고, dev에서 찾은
낮은 alpha 후보를 같은 dev 결과로 확정하면 안 된다.

`samples.csv`는 후보 permutation, 원래 search rank를 담은 decision, raw response hash,
fallback/무결성 카운트를 보존한다. profile 원문이나 credential은 기록하지 않는다.
`results.json`은 두 CSV에서 다시 계산할 수 있으며, 데이터셋·prompt·model provenance가
섞이면 비교 전에 실패한다.

## 결과 재계산

아래 명령은 두 raw CSV와 manifest만으로 저장된 `results.json`을 재계산한다. dry-run이면
비교 수치가 있더라도 verdict는 동일하게 `not-tested`로 억제된다.

```bash
OUT=artifacts/rerank-scoring/live-001 uv run python - <<'PY'
import json
import os
from pathlib import Path

from evals.rerank_scoring.report import load_run_from_artifacts, score_artifacts

out = Path(os.environ["OUT"])
manifest = json.loads((out / "run_manifest.json").read_text())
run = load_run_from_artifacts(out / "samples.csv", out / "failures.csv", manifest)
print(json.dumps(score_artifacts(run, manifest), ensure_ascii=False, indent=2, sort_keys=True))
PY
```
