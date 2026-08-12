# Buyer rerank grounding probe

`rerank`의 상품별 근거와 추천 전체 `overallComment`가 후보 사실 및 최종 노출 목록에 의해
지지되는지 세 팔로 비교하는 수동 평가 도구다.

- `current`: `origin/dev` 자유문장 근거와 기존 후보 ID 검증;
- `prompt_only`: 구조화 근거를 요구하지만 모델의 `rationale`을 그대로 표시;
- `validated`: 같은 구조화 출력을 코드로 검증하고 검증된 템플릿만 표시.

fixture v2는 기존 item-level 10 cases와 final-view overall 12 cases를 함께 실행한다. overall
oracle은 `finalView.productGroups`와 raw `rating`/`reviewCount`/`price`에서 재계산되므로 JSON의
allowed/forbidden label이 데이터와 모순되면 실행 전에 거절된다.

`detectedOverallClaimViolation`은 A 자유문장을 포함해 등록된 한국어 표현군만 판정한다. 검출되지
않은 문장은 분모에 넣지 않으며 이 값을 전체 자연어 정확도로 해석하지 않는다. B/C는 같은 oracle로
`overallClaims`를 평가하고 C는 최종 view를 통과한 결정론 template만 표시한다.

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

22 cases × 3 arms × N=3 = 198 successful calls를 채운다. 전송 실패는 표본이 아니며
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
validated.detectedOverallClaimViolation.rate == 0
validated.overallInvalidStructuredClaimCount == 0
validated.validRankCoverage >= current.validRankCoverage - 0.05
validated.supportedOverallClaimCoverage.rate is reported
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
- `samples.csv`: raw response·overall comment/claims·final view·validation decision을 함께 보존해
  LLM 재호출 없이 재채점;
- `failures.csv`: 전송·파싱·유효 ID 부재 실패;
- `report.md`: raw artifact에서 생성한 발표용 요약.

`supported`는 세 tier-backed 근거군에 대해서만 말한다. 검색 관련성이나 전체 자연어 진실성을
증명하지 않는다. live 설정이 없으면 `not tested`로 남기고 production 기본 arm `current`를 유지한다.

## #632 item-level decision (2026-08-12)

- **Status:** `supported` — screening과 독립 confirmation 2회가 등록 gate를 통과함;
- **Primary (C):** screening `0/80`, confirmation run 1 `0/212`, run 2 `0/208`;
- **Comparison:** confirmation 합산 A `28/411` (6.81%), B `0/418`, C `0/420`;
- **Guardrails:** 세 run 모두 out-of-candidate ID 0, duplicate 0, post-validation invalid 0,
  unfilled cell 0. C coverage는 `0.988`, `0.981`, `0.963`으로 각 run의 A보다 낮지 않았음;
- **Operational:** C p50/p95 latency는 screening `2663/4407ms`, run 1 `2912/4846ms`,
  run 2 `2820/5438ms`. 전체 571 attempts 중 확인 가능한 사용량은 511,192 tokens,
  $0.2392064. run 2의 A에서 사용량 미확정 length-limit parse 실패 1건이 별도 기록됐고
  재시도로 표본을 충족함;
- **Reproducibility:** git `90b72e6474913649f8a020d2924b3b4ec57c31e0`, dataset
  `aa86ab23c1d5135d5c6f5b9fed66899c2d7f6f85f3b7489c8ab1af7d6648435a`, current prompt
  `8654b0ce8c3c48acd5e2ce296752021f93c58a041ba5bfe0ff345abbcf976eef`, structured prompt
  `a1516c9815f800350983aabbaec59474076e1441a6e306716aa946b3635ed9e0`, validator
  `rerank-grounding-v1`로 동일함;
- **Limit:** 평점·리뷰·후보군 상대가격 및 정확한 숫자 근거만 평가했다. B와 C가 모두 0이므로
  live 결과만으로 validator의 추가 효과까지 분리해 주장하지 않음;
- **Release claims:** C1~C4 unchanged; production default `current` 유지.

초기 credential 부재 기록은 `baselines/20260812-not-tested/README.md`에 이력으로 보존했다.

## #645 overall-comment status

fixture v2 deterministic smoke는 실행기와 artifact 완결성만 검증한다. #645 live N=3과 독립 N=8×2
결과가 같은 model/tier, dataset hash, prompt hash, item validator `rerank-grounding-v1`, overall
validator `overall-comment-grounding-v1`로 기록되기 전에는 overall-comment 품질 채택을 주장하지 않는다.
