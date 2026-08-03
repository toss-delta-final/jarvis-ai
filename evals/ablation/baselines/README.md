# Ablation baselines

## `20260803-dev-full-n5`

- Date: 2026-08-03
- Base commit: `4d12348b679b515c57e83b81b1a0eabca0f1bc7c` + dirty ablation working tree
- Dataset: buyer goldenset `1.0.0`, dev 31 cases, N=5, seed `20260803`
- Dataset hash:
  `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- 한 줄 결론: `pipeline`은 `single_call`보다 nDCG@10이 +0.087
  (paired N=18, bootstrap 95% CI [0.022, 0.160]) 높았고 비용 이점도 없어 현행
  pipeline을 유지한다.

이 디렉터리는 승인된 실제 모델 전량 실행의 **불변 baseline**이다. 기존 디렉터리의 파일을
수정하거나 덮어쓰지 않는다. 새 모델·프롬프트·설정·데이터셋으로 다시 실행할 때는 날짜와
조건을 드러내는 새 sibling 디렉터리를 만들고, 그 실행의 manifest와 산출물을 함께 보존한다.

## 산출물

- `run_manifest.json`: 실행 좌표, 모델, 예산, 데이터·설정·소스·프롬프트 SHA-256
- `comparison.json`, `report.md`: 3-arm 비교와 paired bootstrap 판정
- `{pipeline,scoring,single_call}/results.json`: arm별 구조화 결과
- `{pipeline,scoring,single_call}/cases.csv`: case/repeat별 품질·실패·자원 측정
- `{pipeline,scoring,single_call}/calls.csv`: 실제 호출별 모델·usage·비용·latency
- `{pipeline,scoring,single_call}/report.md`: arm별 사람이 읽는 요약
- `{pipeline,scoring,single_call}/regression.csv`: 회귀 판정용 long-format 지표
- `{pipeline,scoring,single_call}/run_manifest.json`: arm별 model-eval 실행 manifest

## 재현 확인

먼저 최상위 `run_manifest.json`의 `commitSha`, `dirty`, `run`, `ablation`,
`modelEval.budget`을 확인한다. 이어서 아래처럼 manifest에 기록된 파일 hash와 현재 재현
입력의 SHA-256을 비교한다. 모든 줄이 `OK`여야 같은 입력 좌표다.

```bash
uv run python - <<'PY'
from hashlib import sha256
import json
from pathlib import Path

root = Path(".")
manifest = json.loads(
    (root / "evals/ablation/baselines/20260803-dev-full-n5/run_manifest.json").read_text()
)
hashes = manifest["hashes"]
checks = {
    "config": root / "app/core/config.py",
    "evalConfig": root / "evals/ablation/ablation_config.json",
    "datasetManifest": root / "evals/goldenset/manifest.json",
    "pricingManifest": root / "evals/model_eval/pricing_manifest.json",
    "uvLock": root / "uv.lock",
}
checks.update(
    (f"ablationModules.{name}", root / "evals/ablation" / name)
    for name in hashes["ablationModules"]
)
expected = {
    **{name: hashes[name] for name in checks if "." not in name},
    **{
        name: hashes["ablationModules"][name.removeprefix("ablationModules.")]
        for name in checks
        if name.startswith("ablationModules.")
    },
}
for name, path in checks.items():
    actual = sha256(path.read_bytes()).hexdigest()
    print(f"{name}: {'OK' if actual == expected[name] else 'MISMATCH'}")
PY
```

`hashes.fixtures`, `hashes.modelEvalModules`, `hashes.prompts`, `hashes.singleCallPrompt`도
같은 방식으로 검증할 수 있다. 특히 `dirty=true` 실행이므로 커밋 SHA만 같다고 같은 실행
좌표로 간주하지 말고 manifest의 소스·프롬프트 hash까지 함께 확인한다.
