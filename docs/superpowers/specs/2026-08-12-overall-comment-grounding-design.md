# Buyer `overallComment` Grounding Design

**Issue:** [#645](https://github.com/toss-delta-final/jarvis-ai/issues/645)  
**Related:** #632, #638  
**Status:** Approved design, implementation pending

## 1. Problem

#632 구조화 grounding은 상품별 `rationale`만 보호한다. 모델 출력의 `overallComment`는
`_strip_unsafe()` 처리 후 SSE `token`으로 그대로 노출되고, A/B/C 평가의 grounding violation
분모에도 들어가지 않는다.

따라서 다음과 같은 목록 수준 주장의 사실성을 현재 코드가 증명할 수 없다.

- “리뷰 수가 가장 많은 상품부터 보여드렸어요.”
- “각 추천 조합이 모두 예산 안에 들어와요.”
- “평점 정보가 높은 상품들만 골랐어요.”
- “가장 인기 있는 상품이에요.”
- “가성비가 가장 좋아요.”

사용자 노출 문장을 정규식으로만 판정하면 표현 이형을 놓치고, 자유문장을 그대로 두면 validator가
무엇을 검증했는지 알 수 없다. `overallComment`도 모델의 구조화 claim 제안과 코드의 결정론 검증·
렌더링 경계로 분리해야 한다.

## 2. Goals

1. 전체 추천 코멘트의 지원 가능한 주장 유형과 truth condition을 코드로 정의한다.
2. 모델이 제안한 claim을 pinning, 노출 보정, 목록 분할, budget-set 계산이 끝난 최종 결과에 대해
   검증한다.
3. validated arm에서는 모델 자유문장을 노출하지 않고 검증된 코드 template만 노출한다.
4. 검증 실패는 추천 목록 자체를 실패시키지 않고 중립 코멘트로 downgrade한다.
5. 기존 A/B/C harness에 overall claim 정확도, coverage, downgrade, 지연, token, 비용을 추가한다.
6. 동일 dataset, prompt, validator hash로 A/B/C 반복 실행 결과를 보존한다.

## 3. Non-goals

- popularity score나 value-for-money score를 새로 정의하지 않는다.
- Spring I-21, CH-5, SSE wire 계약을 바꾸지 않는다.
- 판매자 추천 경로를 바꾸지 않는다.
- 상품별 rationale의 #632 reason taxonomy를 확장하지 않는다.
- 자유문장 전체의 보편적 자연어 진실성을 증명한다고 주장하지 않는다.

## 4. Dependency and Integration

이 작업은 아직 `dev`에 병합되지 않은 #632 구현 브랜치
`NyongCho/buyer-llm-decision-experiments@0d557c65`의 다음 표면을 선행 의존성으로 사용한다.

- `app/agents/buyer/recommendation/rerank_grounding.py`
- `GroundingArm = current | prompt_only | validated`
- structured rerank prompt와 상품별 grounding decision
- `evals/rerank_grounding/` fixture, runner, metrics, report, live artifacts
- production graph의 `Settings.rerank_grounding_arm`

#645 브랜치는 최신 `origin/dev`를 보존하면서 #632 브랜치를 통합한 뒤 구현한다. #632가 먼저
병합되면 그 병합 커밋을 기준으로 rebase하고, 그렇지 않으면 stacked dependency를 PR 본문에 명시한다.

## 5. Trust Boundary

검증 기준은 rerank가 처음 반환한 `ranked`가 아니다. 다음 단계가 모두 끝난 뒤의 사용자 노출 계획이다.

1. rerank 결과
2. repurchase pinning
3. `expose_min` 보충과 `expose_max` 절단
4. need별 목록 분할
5. BUY_ALL budget-set 생성
6. I-21에 실릴 최종 `listType`, `totalBudget`, `lists[].productIds`

`graph.py`는 현재 `overallComment`를 emit하기 전에 1~5를 이미 완료한다. 목록 객체와 UUID를 일찍
만들 필요는 없다. 아래 값만 먼저 결정해 validator에 전달한다.

```python
FinalRecommendationView(
    list_type="PICK_ONE" | "BUY_ALL",
    total_budget=int | None,
    product_groups=tuple[tuple[int, ...], ...],
)
```

검증과 렌더링이 끝난 comment는 기존 위치에서 SSE `token`으로 emit한다. 다른 결정론 고지와
`products.ready` 순서는 바꾸지 않는다.

## 6. Structured Output Contract

`current` arm은 기존 prompt와 출력 계약을 그대로 쓴다. `prompt_only`와 `validated` arm은 기존
상품별 구조화 필드에 `overallClaims`를 추가한다.

```json
{
  "ranked": [{
    "productId": 101,
    "rationale": "모델 자유문장",
    "reasonCode": "RATING_HIGH",
    "evidenceFields": ["ratingLevel"]
  }],
  "overallComment": "모델 자유문장",
  "overallClaims": [{
    "claimCode": "ALL_RATING_HIGH",
    "scope": "FINAL_EXPOSED_PRODUCTS",
    "subjectProductIds": [101, 102],
    "evidenceFields": ["ratingLevel"]
  }]
}
```

Rules:

- `overallClaims`는 배열이며 최대 2개다.
- 각 claim은 네 필드를 모두 가져야 한다.
- `subjectProductIds`는 중복 없는 정수 배열이다.
- 알 수 없는 code, scope, evidence field, final-view 밖 ID는 invalid다.
- 같은 `claimCode` 중복은 invalid다.
- `NO_VERIFIABLE_OVERALL_CLAIM`은 다른 claim과 공존할 수 없다.
- invalid claim 하나가 있어도 추천 ranking은 보존한다.

`RerankResult`는 모델 자유문장과 raw claim proposal을 모두 보존한다.

```python
@dataclass
class RerankResult:
    ranked: list[tuple[int, str]]
    overall_comment: str
    overall_claims: tuple[Mapping[str, object], ...] = ()
```

## 7. Claim Taxonomy

### 7.1 `TOP_REVIEW_COUNT`

- **Scope:** `FINAL_EXPOSED_PRODUCTS`
- **Evidence fields:** `['reviewCount']`
- **Subjects:** 최종 노출 순서의 첫 product ID 하나
- **Truth condition:**
  - 최종 노출 상품이 하나 이상이다.
  - 모든 최종 노출 상품의 raw `reviewCount`가 존재한다.
  - 첫 상품의 `reviewCount`가 최댓값과 같다. 동률은 허용한다.
- **Template:** `리뷰 수가 가장 많은 상품부터 보여드렸어요.`

`reviewLevel`이 아니라 raw `reviewCount`를 사용한다. tier는 서로 다른 실제 개수를 같은 등급으로
접어 “가장 많다”를 증명할 수 없기 때문이다.

### 7.2 `ALL_RATING_HIGH`

- **Scope:** `FINAL_EXPOSED_PRODUCTS`
- **Evidence fields:** `['ratingLevel']`
- **Subjects:** 최종 노출 unique product ID 전체를 노출 순서대로 나열
- **Truth condition:** 모든 대상의 `ratingLevel`이 `높음` 또는 `매우높음`이다.
- **Template:** `평점 정보가 높은 상품들만 골랐어요.`

rating이 없거나 `reviewCount == 0`이면 `ratingLevel == 평가없음`이므로 실패한다.

### 7.3 `ALL_WITHIN_TOTAL_BUDGET`

- **Scope:** `FINAL_RECOMMENDATION_LISTS`
- **Evidence fields:** `['price', 'totalBudget']`
- **Subjects:** 모든 최종 목록의 product ID를 목록 순서대로 flatten하고 처음 등장한 ID만 보존
- **Truth condition:**
  - `listType == BUY_ALL`이다.
  - `totalBudget`이 존재한다.
  - 모든 목록의 모든 상품에 raw `price`가 존재한다.
  - 각 목록의 가격 합이 각각 `totalBudget` 이하이다.
- **Template:** `각 추천 조합이 모두 예산 안에 들어와요.`

여러 대안 목록의 union 가격을 한 번에 합하지 않는다. 각 I-21 list가 하나의 구매 조합이므로 각
조합을 독립적으로 검사한다.

### 7.4 `NO_VERIFIABLE_OVERALL_CLAIM`

- **Scope:** `FINAL_EXPOSED_PRODUCTS`
- **Evidence fields:** `[]`
- **Subjects:** `[]`
- **Truth condition:** 항상 허용하되 다른 claim과 공존 불가
- **Template:** `요청과의 관련도를 기준으로 추천했어요.`

### 7.5 Unsupported semantic families

다음 표현은 초기 taxonomy에 code를 만들지 않는다.

- `POPULARITY_TOP`: popularity의 정본 score와 비교 집합이 없다.
- `VALUE_FOR_MONEY_TOP`: value-for-money 산식과 가중치가 없다.

모델이 알 수 없는 code로 이를 제안하거나 자유문장에만 쓰면 validated arm은 중립 template로
downgrade한다. 향후 정본 metric이 생기면 별도 이슈에서 code와 truth condition을 추가한다.

## 8. Validation and Rendering

새 모듈 `app/agents/buyer/recommendation/overall_comment_grounding.py`가 전체 코멘트만 책임진다.
상품별 `rerank_grounding.py`에 목록·예산 지식을 넣지 않는다.

Public interfaces:

```python
OverallClaimCode = Literal[
    "TOP_REVIEW_COUNT",
    "ALL_RATING_HIGH",
    "ALL_WITHIN_TOTAL_BUDGET",
    "NO_VERIFIABLE_OVERALL_CLAIM",
]

@dataclass(frozen=True)
class FinalRecommendationView:
    list_type: Literal["PICK_ONE", "BUY_ALL"]
    total_budget: int | None
    product_groups: tuple[tuple[int, ...], ...]

@dataclass(frozen=True)
class OverallGroundingDecision:
    requested_claim_codes: tuple[str, ...]
    supported_claim_codes: tuple[OverallClaimCode, ...]
    rendered_comment: str
    downgraded: bool
    failure_reasons: tuple[str, ...]

def validate_and_render_overall_comment(
    proposals: Sequence[Mapping[str, object]],
    *,
    final_view: FinalRecommendationView,
    products_by_id: Mapping[int, SpringProduct],
    settings: Settings,
) -> OverallGroundingDecision: ...
```

Rendering rules:

1. Validate each proposal independently.
2. If any proposal is malformed, references an invalid subject, or fails its truth condition, record a bounded
   failure reason and mark the decision downgraded.
3. Render only supported non-neutral claims in fixed priority order:
   `ALL_WITHIN_TOTAL_BUDGET`, `ALL_RATING_HIGH`, `TOP_REVIEW_COUNT`.
4. Emit at most two templates, joined with one space.
5. If no supported non-neutral claim remains, emit the neutral template.
6. Pass the final template through `_strip_unsafe()` at the graph boundary as today.

Failure reasons are finite labels, not model text:

- `invalid_claim_shape`
- `unknown_claim_code`
- `scope_mismatch`
- `evidence_fields_mismatch`
- `duplicate_claim_code`
- `neutral_claim_conflict`
- `subject_ids_mismatch`
- `subject_outside_final_view`
- `missing_candidate_fact`
- `candidate_fact_not_supported`
- `budget_context_not_supported`
- `too_many_claims`

## 9. Arm Semantics

| Arm | Prompt | Product rationale | Overall comment |
|---|---|---|---|
| A `current` | legacy | model text | model `overallComment` |
| B `prompt_only` | structured | model text | model `overallComment`; metadata는 평가에만 사용 |
| C `validated` | structured | validated template | 최종 view에 대해 validated claim template |

`rerank_grounding_arm=current`는 상품별 rationale과 overall comment를 함께 기존 A 동작으로
되돌리는 한 줄 rollback이어야 한다. 별도 조합형 flag를 추가하지 않는다.

## 10. Evaluation Fixture and Oracle

`evals/rerank_grounding/fixtures/rerank_grounding_v2.json`으로 schema를 올린다. 기존 product rationale
oracle에 다음 필드를 추가한다.

```json
{
  "finalView": {
    "listType": "PICK_ONE",
    "totalBudget": null,
    "productGroups": [[101, 102]]
  },
  "overallOracle": {
    "allowedClaimCodes": ["ALL_RATING_HIGH"],
    "forbiddenClaimCodes": ["TOP_REVIEW_COUNT", "ALL_WITHIN_TOTAL_BUDGET"],
    "requiredNeutralFallback": false
  }
}
```

Required cases:

1. unique top review count
2. tied top review count
3. missing review count blocks top claim
4. all final ratings high
5. one unrated final product blocks all-high claim
6. high-rated candidate excluded from final view does not affect all-high truth
7. BUY_ALL one list within budget
8. BUY_ALL multiple alternative lists each within budget
9. one list exceeds budget
10. one final product has missing price
11. PICK_ONE with a budget-like user utterance rejects budget claim
12. popularity/value-for-money proposal is forbidden and downgraded

The fixture validator recomputes allowed/forbidden truth from raw candidates and `finalView`; the committed
oracle cannot contradict raw data.

## 11. A-arm Detection

A has no structured claims, so measurement needs an intentionally bounded lexical detector. It does not claim
general semantic completeness.

Registered lexical families:

- top review: `(리뷰|후기)` + `(가장|최다|제일)`
- all rating high: `(모두|전부|만)` + `(평점|평가)` + `(높|좋|우수)`
- all within budget: `(모두|전부|각 .*조합)` + `예산` + `(맞|이내|안)`
- unsupported popularity: `(가장|제일)` + `인기`
- unsupported value: `(가장|제일)` + `(가성비|가격 대비)`

검출된 claim은 fixture의 raw facts와 final view로 판정한다. 검출되지 않은 문장은 denominator에 넣지
않는다. 따라서 metric 이름은 `detectedOverallClaimViolation`, not `overallCommentAccuracy`다.

기존 #632 artifact의 `rawResponse.overallComment`는 재호출 없이 A detector로 재채점한다. 하지만
예산/final-view oracle이 없는 표본은 예산 metric의 근거로 사용하지 않고 v2 fixture live run으로
채운다.

## 12. Metrics and Artifacts

Each arm reports:

- `detectedOverallClaimViolation.{numerator, denominator, rate}`
- `supportedOverallClaimCoverage`
- `overallValidatorDowngradeCount`
- `overallInvalidStructuredClaimCount`
- failure reason counts
- valid rank coverage and existing product-rationale metrics
- latency p50/p95
- input/output/reasoning tokens
- confirmed cost and unknown-usage attempts
- successful sample count, failure count, unfilled cells

`samples.csv` preserves:

- raw `overallComment`
- raw `overallClaims`
- final view
- detected A claims
- validation decision
- rendered comment

`run_manifest.json` adds fixture v2 hash, structured prompt hash, overall validator version
`overall-comment-grounding-v1`, source commit, dirty status, model/tier, and command.

## 13. Rollout Decision

Implementation does not change `Settings.rerank_grounding_arm` semantics. Since #632 production default is
`validated`, merging #645 would make overall comment validation part of C. The branch must not be merged until
these gates pass on the same configuration:

```text
validated.detectedOverallClaimViolation.rate == 0
validated.overallInvalidStructuredClaimCount == 0
validated.outOfCandidateIdCount == 0
validated.validRankCoverage >= current.validRankCoverage - 0.05
validated.supportedOverallClaimCoverage is reported, not hidden
unfilledCells == []
```

Run live N=3 screening first. If gates pass, run two independent N=8 confirmations. If provider credentials are
unavailable, preserve a not-tested artifact and keep #645 unmerged rather than presenting deterministic dry-run as
live quality evidence.

## 14. Testing Strategy

All behavior changes follow red-green-refactor.

### Unit tests

- exact claim shape, scope, evidence field, subjects, duplicate, neutral conflict, and max-count validation
- each truth condition with success, missing data, boundary, and wrong final-view cases
- deterministic rendering order, two-sentence cap, and neutral downgrade
- `RerankResult` preserves raw proposals
- arm-specific prompt and comment behavior

### Graph regression tests

- validated comment uses post-pinning final products
- validated comment uses post-split final groups
- BUY_ALL validates each final budget set rather than flattened union
- model free comment never appears in validated SSE token
- current rollback still emits legacy model comment
- rerank degrade keeps existing configured fallback notice

### Evaluation tests

- fixture v2 schema and raw-data oracle consistency
- A lexical detector true/false cases
- B invalid metadata counts as violation but model comment remains displayed
- C invalid metadata downgrades to neutral template
- report and manifest contain new metrics/hashes
- historical raw artifact re-scoring is deterministic

### Verification

- targeted overall grounding tests
- all `tests/eval/test_rerank_grounding*.py`
- `tests/unit/test_recommendation.py`, fanout, tracing, provenance, config
- #638 adversarial eval tests and dataset validator
- Ruff check and format check
- near-full test suite with documented environment exclusions

## 15. File Responsibilities

- `app/agents/buyer/recommendation/overall_comment_grounding.py`: claim contract, validator, templates
- `app/agents/buyer/recommendation/rerank.py`: structured prompt and raw proposal parsing only
- `app/agents/buyer/recommendation/state.py`: `RerankResult.overall_claims`
- `app/agents/buyer/recommendation/graph.py`: final-view construction, arm routing, SSE emission
- `evals/rerank_grounding/schema.py`: fixture v2 and final-view/oracle validation
- `evals/rerank_grounding/metrics.py`: A detection and overall metrics
- `evals/rerank_grounding/runner.py`: capture raw claims, decisions, final view
- `evals/rerank_grounding/report.py`: aggregate and artifact rendering
- `tests/unit/test_overall_comment_grounding.py`: pure validator tests
- existing recommendation/eval tests: integration and regression coverage

## 16. Acceptance Criteria

- Every supported claim has one exact scope, evidence field set, truth condition, and deterministic template.
- Unsupported popularity/value-for-money superlatives cannot reach validated user output.
- C evaluates facts against final I-21 product groups, not pre-pinning rerank results.
- Invalid overall claims never remove valid recommendations.
- A rollback remains one setting change.
- New metrics do not relabel bounded detection as universal natural-language accuracy.
- Live screening and confirmation artifacts record accuracy, latency, token, and cost before merge.

