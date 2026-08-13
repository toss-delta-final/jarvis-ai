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

`--case-ids buy-srch-0001,buy-srch-0002`로 범위를 제한할 수 있다. 기존 2.3.0 holdout은 이
CLI에서 다시 열지 않는다. 출력 디렉터리는 새 경로여야 하며 `samples.csv`, `failures.csv`,
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

Sealed holdout은 candidate commit `a01dae74`의 structured를 고정해 한 번만 열었다. 순위 가능한
19개 case×3 seeds에서 current 대비 평균 ΔnDCG@10은 `+0.0575`, 95% CI는
`[-0.0385,+0.1696]`로 `inconclusive`였다. Production 기본은 current로 유지한다. Label을
복제하지 않은 aggregate와 감사 정보는 `releases/20260813-holdout-structured-n3/`에 있다.
이는 19-case sealed holdout을 확인한 당시의 판정 기록이며, 아래 200-case exploratory 결과와
별도의 product 승인 전까지 적용된 상태다.

## Prospective holdout v2 (200 ranking cases)

새 `evals/rerank_holdout_v2/`는 기존 holdout을 늘리거나 재사용하지 않는 별도 dataset이다.
랭킹 200건(guest/member 각 100)과 별도 safety 24건을 가진다. source는 production log가 아니라
고정 local catalog snapshot이다.

현재 committed label은 heuristic `draft`이므로 아래 scripted 실행이 기본 경로다.

```bash
uv run python -m evals.rerank_scoring \
  --dataset rerank-holdout-v2 \
  --arms current,structured \
  --case-ids rh2-general-0001 \
  --order-seeds 11 --dry-run \
  --out /tmp/rerank-holdout-v2-dry
```

Dry-run manifest는 `labelStatus=draft`, `confirmatory=false`이고 품질 근거가 아니다. 기본 live
실행도 sealed manifest가 없으면 provider 생성 전에 실패한다. 사용자가 명시적으로
`--allow-draft-live`를 주면 heuristic label에 대한 exploratory live 비교는 가능하지만,
`status/verdict=exploratory`로 강제되고 raw 통계 판정은 `statisticalVerdict`에만 기록된다.
Confirmatory 실행에는 두 명의 실제 독립 사람 검수, 완전한 adjudication, sealed manifest가
필요하다. 생성·감사·검수·봉인 절차는 `evals/rerank_holdout_v2/README.md`가 정본이다.

### 보존된 200-case exploratory live baseline

`baselines/20260813-holdout-v2-draft-current-structured-n3/`은 clean commit `11f37f70`, ranking
200건과 seeds `11,29,47`의 `current`/`structured` 결과다. 1,200개 목표 cell 중 1,199개가
성공했고, `rh2-adversarial-0016`의 current seed 11 한 cell만 세 번 실패했다. 해당 case에는
나머지 두 seed가 있어 case 평균 paired N은 200을 유지한다.

| 범위 | N | current | structured | 평균 ΔnDCG@10 | 개선/동률/악화 |
|---|---:|---:|---:|---:|---:|
| 전체 | 200 | 0.7139 | 0.8358 | +0.1219 | 106/57/37 |
| guest | 100 | 0.7835 | 0.8372 | +0.0537 | 42/36/22 |
| member | 100 | 0.6443 | 0.8344 | +0.1900 | 64/21/15 |

전체 case bootstrap 95% CI는 `[+0.0925,+0.1535]`이고 raw 통계 판정은 `supported`다. seed별
평균 delta도 `+0.1245`(N=199), `+0.1202`, `+0.1215`로 방향이 일치했다. 특히
personalization은 `+0.3017`, repurchase는 `+0.1098`이었지만 long-tail은 `-0.0096`이었다.
Structured의 seed 간 top-1 agreement는 `0.9250`으로 current `0.6388`보다 높았고,
hard-constraint 위반은 두 arm 모두 0건이었다. 반면 p50 latency는 current `3.992s`, structured
`11.845s`로 약 3배였다. 총 1,209 provider call, 약 516.6만 token, `$2.5761`이 들었다.

이 결과의 artifact `status/verdict`는 의도대로 `exploratory`다. 라벨이 사람이 독립 검수한
정답이 아니라 structured 규칙과 일부 구조적 유사성을 가진 heuristic draft이므로, 양의 delta는
일반화의 확정 근거가 아니다. 이 제한과 약 3배의 p50 latency를 수용하는 별도 product decision으로
production graph의 기본은 `structured`로 전환했다. 이 운영 결정이 artifact를 confirmatory로
재분류하지는 않는다. 다음 검증 gate는 독립 2인 검수와 sealed release이며, 장애·품질 이상 시
`RERANK_RANKING_ARM=current`로 즉시 롤백한다.

`samples.csv`는 후보 permutation, 원래 search rank를 담은 decision, raw response hash,
fallback/무결성 카운트를 보존한다. profile 원문이나 credential은 기록하지 않는다.
`results.json`은 두 CSV에서 다시 계산할 수 있으며, 데이터셋·prompt·model provenance가
섞이면 비교 전에 실패한다.

## LLM blind judge

저장된 200-case output은 heuristic label을 전혀 소비하지 않는 별도 LLM A/B judge로 비교할 수
있다. judge는 query, profile summary, 두 익명 랭킹과 양쪽이 공통으로 받은 중립 상품 사실만 본다.
arm 이름·scoring decision·search rank·candidate provenance·relevance label·ideal order는 받지 않는다.
모든 pair는 A/B와 B/A로 두 번 판정하며, 실제 arm으로 역매핑했을 때 결과가 일치하지 않으면
`unstable`로 남기고 decisive 승률에서 제외한다.

```bash
uv run python -m evals.rerank_scoring.judge_cli \
  --source-dir evals/rerank_scoring/baselines/20260813-holdout-v2-draft-current-structured-n3 \
  --dataset-root evals/rerank_holdout_v2/dataset \
  --judge-tier smart --attempt-multiplier 3 --concurrency 1 \
  --max-calls 3594 --max-cost-usd 20 \
  --out artifacts/rerank-scoring/blind-judge-001
```

Live 실행은 `--max-calls`와 `--max-cost-usd`를 명시해야 한다. `RecordingLLM`의 정확한 usage
귀속을 유지하기 위해 한 process 안의 기본 concurrency는 1이며, 병렬 실행이 필요하면 case를
서로 겹치지 않는 process로 shard한 뒤 coordinator artifact 기준으로 합쳐야 한다. 출력은 공개
`presentations.jsonl`, A/B-only `judge_responses.jsonl`, 비공개 조정용
`coordinator_mapping.jsonl`, `failures.jsonl`, `results.json`, `run_manifest.json`, `report.md`다.

서로 겹치지 않는 shard는 아래처럼 합친다. merge는 dataset, source samples, judge model/prompt,
mapping seed, bootstrap 설정이 모두 같은지와 presentation ID가 disjoint인지 확인한다.

```bash
uv run python -m evals.rerank_scoring.judge_merge_cli \
  --shards artifacts/blind-shard-a,artifacts/blind-shard-b \
  --out artifacts/rerank-scoring/blind-judge-merged
```

이 평가는 arm identity와 위치 편향을 줄이지만 여전히 synthetic judge 기반 exploratory evidence다.
사람 blind review나 production A/B를 대체하지 않고 confirmatory 우월성을 주장하지 않는다.

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
