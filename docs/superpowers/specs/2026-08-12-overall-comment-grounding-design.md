# Buyer `overallComment` Grounding Delta Design

**Issue:** [#645](https://github.com/toss-delta-final/jarvis-ai/issues/645)  
**Related:** #632, #638  
**Status:** Design review

## 1. Base design

이 문서는 다음 #632 설계를 반복하지 않고 `overallComment`에 필요한 차이만 정의한다.

- `docs/superpowers/specs/2026-08-12-buyer-rerank-grounding-experiment-design.md`
- `docs/superpowers/specs/2026-08-12-adversarial-grounding-arms-design.md`
- `docs/superpowers/specs/2026-08-12-validated-grounding-rollout-design.md`

위 문서의 다음 계약은 그대로 상속한다.

- A `current` / B `prompt_only` / C `validated` 비교
- 구조화 모델 제안과 결정론 validator/template의 분리
- 검증 실패 시 상품 ID와 순위를 보존하고 문구만 중립화
- A 한 줄 rollback
- 동일 fixture/model/prompt hash 비교
- N=3 screening 후 독립 N=8 confirmation 2회
- 정확도와 함께 coverage, 실패, latency, token, cost 측정
- deterministic dry-run을 live 품질 근거로 사용하지 않음

선행 구현은 `NyongCho/buyer-llm-decision-experiments@0d557c65`다. #645 브랜치는 최신
`origin/dev`를 보존하면서 이 구현을 통합한다. #632가 먼저 병합되면 병합된 `dev`를 기준으로
rebase한다.

## 2. Delta summary

#632는 상품별 `rationale`의 `reasonCode`와 `evidenceFields`를 검증한다. 다음 값은 여전히 모델
자유문장이라 grounding 지표에 포함되지 않는다.

```json
{"overallComment": "평점이 높은 상품들만 골랐어요"}
```

#645는 목록 전체 주장을 위한 `overallClaims`를 추가한다. C에서는 모델의 `overallComment`를
노출하지 않고 최종 추천 목록으로 검증된 claim template만 노출한다.

이번 변경은 popularity score나 value-for-money score를 새로 정의하지 않으며 Spring, CH-5,
SSE wire 계약을 바꾸지 않는다.

## 3. Output contract delta

B와 C의 기존 구조화 rerank 출력에 다음 배열을 추가한다. A 출력은 변경하지 않는다.

```json
{
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

- `overallClaims`는 최대 2개다.
- 모든 claim은 네 필드를 모두 가진다.
- `subjectProductIds`는 중복 없는 정수 배열이다.
- 알 수 없는 code, scope, evidence field, 최종 노출 밖 ID는 invalid다.
- 같은 `claimCode` 중복은 invalid다.
- `NO_VERIFIABLE_OVERALL_CLAIM`은 다른 claim과 공존할 수 없다.
- invalid claim은 ranking이나 상품 ID를 제거하지 않는다.

`RerankResult`에는 raw proposal을 보존한다.

```python
overall_claims: tuple[Mapping[str, object], ...] = ()
```

## 4. New trust boundary

상품별 #632 validator는 rerank 후보 하나의 tier만 보면 된다. 전체 목록 주장은 다음 처리가 끝난
후에만 참을 판정할 수 있다.

1. rerank
2. repurchase pinning
3. `expose_min` 보충과 `expose_max` 절단
4. need별 목록 분할
5. BUY_ALL budget-set 계산

따라서 #645 validator는 모델이 처음 낸 `ranked`가 아니라 I-21에 실릴 최종 계획을 입력으로 받는다.

```python
@dataclass(frozen=True)
class FinalRecommendationView:
    list_type: Literal["PICK_ONE", "BUY_ALL"]
    total_budget: int | None
    product_groups: tuple[tuple[int, ...], ...]
```

`graph.py`는 현재 comment emit 전까지 위 다섯 단계를 이미 완료한다. UUID가 포함된 I-21 객체를
일찍 만들지 않고 `FinalRecommendationView`만 파생한다. 기존 token·notice·products.ready 순서는
바꾸지 않는다.

## 5. New claim taxonomy

| claimCode | scope | evidenceFields | subject 규칙 | truth condition | template |
|---|---|---|---|---|---|
| `TOP_REVIEW_COUNT` | `FINAL_EXPOSED_PRODUCTS` | `reviewCount` | 첫 노출 ID 하나 | 모든 최종 상품의 raw `reviewCount`가 있고 첫 상품 값이 최댓값과 같음; 동률 허용 | `리뷰 수가 가장 많은 상품부터 보여드렸어요.` |
| `ALL_RATING_HIGH` | `FINAL_EXPOSED_PRODUCTS` | `ratingLevel` | 최종 unique ID 전체, 노출 순서 | 모든 대상이 `높음` 또는 `매우높음` | `평점 정보가 높은 상품들만 골랐어요.` |
| `ALL_WITHIN_TOTAL_BUDGET` | `FINAL_RECOMMENDATION_LISTS` | `price`, `totalBudget` | 모든 목록 ID를 순서대로 flatten한 unique ID | BUY_ALL, 예산 존재, 모든 가격 존재, 각 목록 합계가 각각 예산 이하 | `각 추천 조합이 모두 예산 안에 들어와요.` |
| `NO_VERIFIABLE_OVERALL_CLAIM` | `FINAL_EXPOSED_PRODUCTS` | 없음 | 빈 배열 | 다른 claim과 공존하지 않으면 허용 | `요청과의 관련도를 기준으로 추천했어요.` |

추가 규칙:

- `TOP_REVIEW_COUNT`는 `reviewLevel`이 아니라 raw `reviewCount`를 사용한다. tier는 최댓값을
  증명할 수 없다.
- rating이 없거나 `reviewCount == 0`이면 `ratingLevel == 평가없음`이므로 all-high가 아니다.
- 여러 budget 대안의 union 가격을 합하지 않는다. 각 I-21 list가 하나의 구매 조합이다.
- popularity와 value-for-money는 정본 metric이 없으므로 지원 code를 만들지 않는다. “가장 인기”,
  “가성비 최고” 제안은 C에서 중립 template로 downgrade한다.

## 6. New component

`app/agents/buyer/recommendation/overall_comment_grounding.py`가 목록 claim만 책임진다. 상품별
`rerank_grounding.py`에 목록과 예산 지식을 넣지 않는다.

```python
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

처리 순서:

1. shape, 중복, code, scope, evidenceFields, subject를 검사한다.
2. 최종 목록과 raw candidate fact로 truth condition을 검사한다.
3. 지원된 non-neutral claim을 `ALL_WITHIN_TOTAL_BUDGET`, `ALL_RATING_HIGH`,
   `TOP_REVIEW_COUNT` 우선순위로 정렬한다.
4. 최대 두 template를 한 칸으로 연결한다.
5. 지원된 non-neutral claim이 없으면 중립 template를 쓴다.
6. graph의 기존 `_strip_unsafe()`를 마지막에 적용한다.

Failure reason은 모델 문장이 아닌 유계 label이다.

```text
invalid_claim_shape
unknown_claim_code
scope_mismatch
evidence_fields_mismatch
duplicate_claim_code
neutral_claim_conflict
subject_ids_mismatch
subject_outside_final_view
missing_candidate_fact
candidate_fact_not_supported
budget_context_not_supported
too_many_claims
```

## 7. Arm behavior delta

| Arm | 기존 상품 rationale | 새 overall comment 동작 |
|---|---|---|
| A `current` | #632 A | 기존 모델 `overallComment` 표시 |
| B `prompt_only` | #632 B | `overallClaims`를 기록·평가하지만 모델 `overallComment` 표시 |
| C `validated` | #632 C | 모델 자유문장을 버리고 최종 view 기준 template 표시 |

`RERANK_GROUNDING_ARM=current`는 상품별 rationale과 overall comment를 함께 A로 되돌린다. 별도
조합형 feature flag는 추가하지 않는다.

## 8. Fixture and metric delta

`evals/rerank_grounding` fixture schema를 v2로 올리고 각 case에 다음 oracle을 추가한다.

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

필수 case delta:

- review 최댓값 단독·동률·결측
- 최종 상품 전부 high와 한 상품 unrated 반례
- 초기 후보와 최종 노출 subset이 다른 경우
- BUY_ALL 단일·복수 대안 조합의 예산 충족
- 한 조합 예산 초과와 가격 결측
- PICK_ONE에서 budget claim 거부
- popularity/value-for-money claim 거부

Fixture validator는 raw candidate와 `finalView`로 allowed/forbidden claim을 재계산한다.

새 arm metric:

- `detectedOverallClaimViolation.{numerator, denominator, rate}`
- `supportedOverallClaimCoverage`
- `overallValidatorDowngradeCount`
- `overallInvalidStructuredClaimCount`
- failure reason counts

A에는 구조화 claim이 없으므로 등록된 표현만 판정하는 bounded detector를 쓴다.

- top review: `(리뷰|후기)` + `(가장|최다|제일)`
- all rating high: `(모두|전부|만)` + `(평점|평가)` + `(높|좋|우수)`
- all budget: `(모두|전부|각 .*조합)` + `예산` + `(맞|이내|안)`
- unsupported popularity: `(가장|제일)` + `인기`
- unsupported value: `(가장|제일)` + `(가성비|가격 대비)`

검출되지 않은 자유문장은 denominator에 넣지 않는다. 이름도 `overallCommentAccuracy`가 아니라
`detectedOverallClaimViolation`으로 두어 전체 자연어 진실성처럼 과장하지 않는다.

기존 #632 raw artifact는 A detector로 재채점하되, final-view/예산 oracle이 없는 표본은 예산
근거로 쓰지 않고 v2 fixture live run으로 채운다. `samples.csv`에는 raw comment/claims, final view,
detected claims, validation decision, rendered comment를 추가한다. manifest에는 fixture v2 hash와
`overall-comment-grounding-v1` validator hash를 추가한다.

## 9. Test delta

기존 #632 테스트에 다음만 추가한다.

- pure validator: shape, scope, evidence, subject, 중복, neutral conflict, 최대 claim 수
- truth condition: review 동률·결측, rating missing, budget list별 합계·가격 결측
- rendering: 고정 우선순위, 두 문장 상한, 중립 downgrade
- rerank: raw `overallClaims` 보존과 arm별 prompt
- graph: post-pinning, post-split, budget-set 최종 view를 사용
- graph: C에서 모델 자유 comment 미노출, A에서 기존 comment 노출
- graph: rerank degrade의 기존 fallback notice 불변
- eval: fixture v2 oracle, A detector, B violation, C neutral downgrade, metric/report/manifest

검증 범위:

- #632 grounding tests와 recommendation/fanout/tracing/provenance/config tests
- #638 adversarial eval tests, validator, generator check
- Ruff check와 format check
- 문서화된 환경 제외를 뺀 near-full suite

## 10. Rollout delta

#632 production 기본 arm은 이미 `validated`다. 따라서 #645를 병합하면 C의 overall comment도
validated가 된다. 병합 전 같은 설정에서 다음 gate를 통과해야 한다.

```text
validated.detectedOverallClaimViolation.rate == 0
validated.overallInvalidStructuredClaimCount == 0
validated.outOfCandidateIdCount == 0
validated.validRankCoverage >= current.validRankCoverage - 0.05
validated.supportedOverallClaimCoverage is reported
unfilledCells == []
```

N=3 screening을 통과한 뒤 독립 N=8 confirmation 2회를 실행한다. 결과에는 정확도, coverage,
downgrade, latency p50/p95, input/output/reasoning token, cost/case를 기록한다. credential이 없으면
`not tested` artifact를 남기고 deterministic 결과만으로 #645를 병합하지 않는다.

## 11. Acceptance delta

- 지원 claim마다 scope, evidenceFields, subject 규칙, truth condition, template가 하나씩 존재한다.
- C는 pre-rerank 후보가 아니라 최종 I-21 product groups를 검증한다.
- popularity/value-for-money 최상급이 C 사용자 출력에 도달하지 않는다.
- invalid claim은 유효 추천 상품이나 순위를 제거하지 않는다.
- A rollback은 기존 설정 한 줄로 유지된다.
- 새 metric은 bounded detection을 전체 자연어 정확도로 부르지 않는다.
- live screening과 confirmation artifact가 정확도·지연·token·비용을 보존한 뒤에만 병합한다.

