# Buyer rerank grounding probe

`rerank`의 추천 근거가 후보의 `ratingLevel`, `reviewLevel`, `priceLevel`에 의해 지지되는지
세 팔로 비교하는 수동 평가 도구다.

- `current`: `origin/dev` 자유문장 근거와 기존 후보 ID 검증;
- `prompt_only`: 구조화 근거를 요구하지만 모델의 `rationale`을 그대로 표시;
- `validated`: 같은 구조화 출력을 코드로 검증하고 검증된 템플릿만 표시.

사람 blind pairwise는 사용하지 않는다. 이 결과는 frozen C1~C4를 바꾸지 않는 exploratory appendix
근거다. 자동 판정 범위도 평점·리뷰·후보군 상대가격 및 정확한 숫자 주장으로 제한된다.

## 결정론 smoke

```bash
out=$(mktemp -d /tmp/rerank-grounding-dry.XXXXXX)/run
uv run python -m evals.rerank_grounding \
  --arms all --repeats 1 --dry-run \
  --out "$out"
```

dry-run은 실행기·validator·artifact 재생성만 검증한다. 품질 채택 근거가 아니다.

## Live screening

10 cases × 3 arms × N=3 = 90 successful calls를 채운다. 전송 실패는 표본이 아니며
`failures.csv`에 별도 기록된다.

```bash
uv run python -m evals.rerank_grounding \
  --arms all --repeats 3 --tier smart \
  --out evals/rerank_grounding/baselines/20260812-screening-n3
```

다음 조건을 모두 만족해야 confirmation으로 간다.

```text
validated.unsupportedEvidence.rate == 0
validated.outOfCandidateIdCount == 0
validated.duplicateIdCount == 0
validated.invalidStructuredEvidenceCount == 0
validated.validRankCoverage >= current.validRankCoverage - 0.05
unfilledCells == []
```

## Live confirmation

screening을 통과한 경우에만 독립 N=8 run 두 번을 실행한다.

```bash
uv run python -m evals.rerank_grounding \
  --arms all --repeats 8 --tier smart \
  --out evals/rerank_grounding/baselines/20260812-confirm-n8-run1

uv run python -m evals.rerank_grounding \
  --arms all --repeats 8 --tier smart \
  --out evals/rerank_grounding/baselines/20260812-confirm-n8-run2
```

N=3은 screening, N=8×2도 exploratory confirmation이다. 두 run의 dataset/prompt hash가
같지 않으면 비교하지 않는다.

## 산출물

- `results.json`: primary, hard gates, latency, INV mismatch, status;
- `run_manifest.json`: git/dirty, dataset/prompt hash, validator, model/tier, budget/pacer, command;
- `samples.csv`: raw response와 표시 근거를 함께 보존해 LLM 재호출 없이 재채점;
- `failures.csv`: 전송·파싱·유효 ID 부재 실패;
- `report.md`: raw artifact에서 생성한 발표용 요약.

`supported`는 세 tier-backed 근거군에 대해서만 말한다. 검색 관련성이나 전체 자연어 진실성을
증명하지 않는다. live 설정이 없으면 `not tested`로 남기고 production 기본 arm `current`를 유지한다.

## Current decision (2026-08-12)

- **Status:** `not tested`
- **Primary:** live numerator/denominator 없음; API key preflight에서 provider 호출 전 중단;
- **Guardrails:** deterministic dry-run에서 candidate ID 0, duplicate 0, validated 0/27, coverage 1.0;
- **Operational:** live latency/token/cost 미측정;
- **Limit:** dry-run은 validator 동작만 증명하며 실제 모델 개선을 증명하지 않음;
- **Release claims:** C1~C4 unchanged; production default `current` 유지.

실패 명령과 scrubbed 사유는 `baselines/20260812-not-tested/README.md`에 보존했다.
