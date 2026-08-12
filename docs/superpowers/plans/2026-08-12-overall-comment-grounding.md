# Buyer `overallComment` Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make buyer recommendation-level comments mechanically verifiable against the final exposed products, and extend the existing A/B/C rerank experiment so accuracy, latency, token usage, and cost can be compared before rollout.

**Architecture:** Integrate the unmerged #632 grounding branch, retain its item-level `reasonCode` validator, and add an independent `overall_comment_grounding.py` boundary. The reranker only proposes structured `overallClaims`; after pinning, exposure shaping, need splitting, and budget-set construction, the graph validates those proposals against a `FinalRecommendationView` and renders deterministic templates for arm C. The existing rerank-grounding fixture, runner, metrics, and report advance to schema v2 so arm A uses a bounded lexical detector while B/C use structured decisions.

**Tech Stack:** Python 3.12, dataclasses, Pydantic Settings, LangGraph async streaming, pytest/pytest-asyncio, Ruff; no new dependencies.

## Global Constraints

- `NyongCho/buyer-llm-decision-experiments@0d557c65` is the normative #632 dependency.
- A remains `current`, B remains `prompt_only`, and C remains `validated`.
- `RERANK_GROUNDING_ARM=current` restores both legacy item rationales and the legacy model `overallComment`.
- Invalid overall metadata changes only the comment; it never removes or reorders product IDs.
- C renders at most two fixed templates and otherwise renders `요청과의 관련도를 기준으로 추천했어요.`
- Popularity and value-for-money superlatives remain unsupported because no canonical metric exists.
- Validation uses final I-21 product groups, not the pre-pinning rerank order.
- A measurement is named `detectedOverallClaimViolation`; it is not presented as universal natural-language accuracy.
- Live evidence requires N=3 screening followed by two independent N=8 confirmations on the same model/tier; deterministic runs are not substitutes.
- No new package dependency and no Spring, CH-5, or SSE wire-contract change.

---

### Task 1: Integrate the #632 grounding baseline

**Files:**
- Merge from: `NyongCho/buyer-llm-decision-experiments@0d557c65`
- Verify: `app/agents/buyer/recommendation/rerank_grounding.py`
- Verify: `evals/rerank_grounding/`
- Verify: `tests/unit/test_rerank_grounding.py`
- Verify: `tests/unit/test_rerank_grounding_eval.py`

**Interfaces:**
- Consumes: `GroundingArm = Literal["current", "prompt_only", "validated"]`
- Consumes: `rerank(..., grounding_arm: GroundingArm = "current") -> RerankResult`
- Produces: a clean branch containing the full #632 experiment and production rollout surface.

- [ ] **Step 1: Merge the dependency without rewriting its commits**

Run:

```bash
git merge --no-ff NyongCho/buyer-llm-decision-experiments
```

Expected: the #632 implementation, fixtures, reports, and tests are present; the #645 spec and plan remain present.

- [ ] **Step 2: Verify the imported baseline**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_rerank_grounding.py \
  tests/unit/test_rerank_grounding_eval.py \
  tests/unit/test_recommendation.py \
  tests/unit/test_config.py
```

Expected: PASS with no #645 implementation changes.

- [ ] **Step 3: Record dependency integration**

If the merge command requires an explicit message, use a Conventional Commit subject plus Lore trailers explaining that #645 is stacked on #632 and that rebasing onto merged `dev` is preferred once #632 lands.

---

### Task 2: Add the pure overall-claim validator and renderer

**Files:**
- Create: `app/agents/buyer/recommendation/overall_comment_grounding.py`
- Create: `tests/unit/test_overall_comment_grounding.py`

**Interfaces:**
- Produces: `OverallClaimCode = Literal["TOP_REVIEW_COUNT", "ALL_RATING_HIGH", "ALL_WITHIN_TOTAL_BUDGET", "NO_VERIFIABLE_OVERALL_CLAIM"]`
- Produces: `FinalRecommendationView(list_type: Literal["PICK_ONE", "BUY_ALL"], total_budget: int | None, product_groups: tuple[tuple[int, ...], ...])`
- Produces: `OverallGroundingDecision(requested_claim_codes: tuple[str, ...], supported_claim_codes: tuple[OverallClaimCode, ...], rendered_comment: str, downgraded: bool, failure_reasons: tuple[str, ...])`
- Produces: `validate_and_render_overall_comment(proposals: Sequence[Mapping[str, object]], *, final_view: FinalRecommendationView, products_by_id: Mapping[int, SpringProduct], settings: Settings) -> OverallGroundingDecision`

- [ ] **Step 1: Write RED contract-shape tests**

Add parameterized tests that assert these exact failure labels:

```python
(
    "invalid_claim_shape",
    "unknown_claim_code",
    "scope_mismatch",
    "evidence_fields_mismatch",
    "duplicate_claim_code",
    "neutral_claim_conflict",
    "subject_ids_mismatch",
    "subject_outside_final_view",
    "too_many_claims",
)
```

Also assert that every invalid case returns the neutral template and preserves an empty `supported_claim_codes` tuple.

- [ ] **Step 2: Run shape tests and confirm RED**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q tests/unit/test_overall_comment_grounding.py -k 'shape or scope or duplicate or neutral or subject or too_many'
```

Expected: collection/import failure because the new module does not exist.

- [ ] **Step 3: Implement minimal shape validation**

Define immutable dataclasses, exact scope/evidence/subject specifications, the fixed priority
`ALL_WITHIN_TOTAL_BUDGET -> ALL_RATING_HIGH -> TOP_REVIEW_COUNT`, the two-template cap, and finite failure labels. Never include model text in `failure_reasons`.

- [ ] **Step 4: Write RED truth-condition tests**

Cover these exact cases:

```text
TOP_REVIEW_COUNT: unique maximum passes; tied maximum passes; missing reviewCount fails; non-first subject fails
ALL_RATING_HIGH: all high/very-high pass; reviewCount=0 fails; rating=None fails; excluded low-rated candidate is ignored
ALL_WITHIN_TOTAL_BUDGET: one BUY_ALL group passes; two groups are checked independently; one over-budget group fails; missing price fails; PICK_ONE fails
unsupported: POPULARITY_TOP and VALUE_FOR_MONEY_TOP downgrade to neutral
rendering: supported claims use fixed priority and at most two templates
```

- [ ] **Step 5: Run truth tests and confirm RED**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q tests/unit/test_overall_comment_grounding.py
```

Expected: failures identify the unimplemented truth conditions.

- [ ] **Step 6: Implement truth checks and deterministic rendering**

Use raw `SpringProduct.review_count` for `TOP_REVIEW_COUNT`, settings thresholds plus the existing `review_count == 0` missing-rating rule for `ALL_RATING_HIGH`, and raw `SpringProduct.price` plus `FinalRecommendationView.total_budget` for each budget group. Deduplicate final product IDs in first-exposure order.

- [ ] **Step 7: Run the pure unit suite GREEN**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q tests/unit/test_overall_comment_grounding.py
```

Expected: PASS.

- [ ] **Step 8: Commit the pure boundary**

Commit only the module and its tests with a Lore-compliant Conventional Commit message.

---

### Task 3: Preserve structured overall proposals at the rerank boundary

**Files:**
- Modify: `app/agents/buyer/recommendation/state.py`
- Modify: `app/agents/buyer/recommendation/rerank.py`
- Modify: `tests/unit/test_rerank_grounding.py`
- Modify: `tests/unit/test_recommendation.py`

**Interfaces:**
- Produces: `RerankResult.overall_claims: tuple[Mapping[str, object], ...] = ()`
- Consumes: the existing `_SYSTEM` unchanged for arm A.
- Produces: B/C structured schema with `overallClaims`, while retaining model `overallComment` for experiment comparison.

- [ ] **Step 1: Write RED parsing and prompt tests**

Assert:

```text
A prompt has no overallClaims schema and returns overall_claims == ()
B/C prompt names all four supported claim codes, scope, subjectProductIds, and evidenceFields
B/C preserve well-formed raw proposal mappings in response order
non-list overallClaims and non-object members become validator-visible invalid mappings rather than disappearing
rank filtering, duplicate removal, and item grounding behavior remain unchanged
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_rerank_grounding.py \
  tests/unit/test_recommendation.py -k 'overall_claim or grounding_prompt or grounding_arm'
```

Expected: failures for the missing field, prompt schema, and parser behavior.

- [ ] **Step 3: Implement the minimal state and rerank changes**

Extend only `_SYSTEM_STRUCTURED_GROUNDING` and `RerankResult`. Keep `_SYSTEM` byte-for-byte unchanged. Parse raw overall proposals after `extract_json`; encode malformed array members as mappings carrying a private invalid-shape sentinel so Task 2 records `invalid_claim_shape` instead of silently accepting them.

- [ ] **Step 4: Run rerank regression tests GREEN**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_rerank_grounding.py \
  tests/unit/test_recommendation.py
```

Expected: PASS.

- [ ] **Step 5: Commit the proposal boundary**

Commit the state, prompt/parser, and focused tests with Lore trailers stating that exact review counts and prices remain outside the LLM prompt.

---

### Task 4: Validate and render against the final graph view

**Files:**
- Modify: `app/agents/buyer/recommendation/graph.py`
- Modify: `tests/unit/test_fanout.py`
- Modify: `tests/unit/test_recommendation.py`
- Modify: `tests/unit/test_buyer_tracing.py`
- Modify: `tests/unit/test_reco_provenance_140.py`

**Interfaces:**
- Consumes: `RerankResult.overall_claims`
- Consumes: `validate_and_render_overall_comment(...)`
- Produces: `FinalRecommendationView` derived from `plan.sets` when a budget plan exists, otherwise from `exposed_groups` with `list_type="PICK_ONE"` and `total_budget=None`.

- [ ] **Step 1: Write RED graph boundary tests**

Add async stream tests that prove:

```text
validated uses products after repurchase pinning and exposure truncation
validated uses post-split product groups
validated BUY_ALL checks each plan.sets group rather than the flattened union
validated never emits the model free overallComment
validated invalid metadata emits the neutral template without changing pushed IDs/order
prompt_only emits the model free overallComment
current emits the legacy model free overallComment
rerank LLMError still emits rerank_fallback_notice and does not invoke the overall validator
```

- [ ] **Step 2: Run the graph tests and confirm RED**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_fanout.py \
  tests/unit/test_recommendation.py \
  tests/unit/test_buyer_tracing.py \
  tests/unit/test_reco_provenance_140.py \
  -k 'overall or grounding or rerank_fallback or budget_set'
```

Expected: failures show that `rr.overall_comment` is still selected before final-view validation.

- [ ] **Step 3: Move comment selection behind final shaping**

Keep `raw_overall_comment`, `raw_overall_claims`, and `rerank_degraded` after the LLM call. Once `exposed_groups` and `plan` are final, construct the view using the same branch that later constructs `RecommendationPush.lists`. For C call the validator and use `rendered_comment`; for A/B use the model comment. Apply `_strip_unsafe()` once at the existing SSE boundary.

- [ ] **Step 4: Add bounded observability without model text**

Extend the existing `recommend_pipeline` log with claim codes, downgrade boolean, and finite failure labels only. Do not log raw model comments, product names, prices, budgets, or user text.

- [ ] **Step 5: Run graph and provenance regressions GREEN**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_fanout.py \
  tests/unit/test_recommendation.py \
  tests/unit/test_buyer_tracing.py \
  tests/unit/test_reco_provenance_140.py
```

Expected: PASS with unchanged SSE ordering and push payload contracts.

- [ ] **Step 6: Commit the final-view trust boundary**

Commit graph wiring and regression tests with Lore trailers rejecting pre-pinning validation.

---

### Task 5: Advance the evaluation fixture and oracle to v2

**Files:**
- Create: `evals/rerank_grounding/fixtures/rerank_grounding_v2.json`
- Modify: `evals/rerank_grounding/schema.py`
- Modify: `tests/unit/test_rerank_grounding_eval.py`

**Interfaces:**
- Produces: `FinalViewFixture(list_type, total_budget, product_groups)`
- Produces: `OverallOracle(allowed_claim_codes, forbidden_claim_codes)`
- Extends: `GroundingCase.final_view` and `GroundingCase.overall_oracle`
- Changes: `DEFAULT_FIXTURE_PATH` to `rerank_grounding_v2.json`.

- [ ] **Step 1: Write RED schema and oracle-consistency tests**

Assert rejection of unknown list type, boolean/non-integer budget, empty/non-list product groups, product IDs outside candidates, duplicate IDs within a group, unknown claim codes, overlap between allowed and forbidden sets, and oracle declarations that contradict recomputed raw facts.

- [ ] **Step 2: Run schema tests and confirm RED**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q tests/unit/test_rerank_grounding_eval.py -k 'fixture or oracle or final_view'
```

Expected: failures for missing v2 fields and validation.

- [ ] **Step 3: Implement v2 schema parsing and independent oracle recomputation**

Reuse `FinalRecommendationView` and the Task 2 validator to compute support from fixture candidates. The loader must reject a committed allowed claim that the raw candidates/final view do not support, and reject a committed forbidden claim that is supported.

- [ ] **Step 4: Add the twelve required deterministic cases**

Create cases for unique/tied/missing review maximum; all-high/unrated/final-subset rating; single/multiple/over-budget/missing-price BUY_ALL groups; PICK_ONE budget rejection; and unsupported popularity/value-for-money. Each case includes explicit `finalView` and disjoint `allowedOverallClaims`/`forbiddenOverallClaims`.

- [ ] **Step 5: Run fixture tests GREEN**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q tests/unit/test_rerank_grounding_eval.py -k 'fixture or oracle or final_view'
```

Expected: PASS and the checked-in v2 fixture loads without contradiction.

- [ ] **Step 6: Commit the fixture truth source**

Commit schema, fixture, and tests with Lore trailers stating that oracle truth is recomputed from raw data.

---

### Task 6: Measure overall claims across A/B/C

**Files:**
- Modify: `evals/rerank_grounding/metrics.py`
- Modify: `evals/rerank_grounding/runner.py`
- Modify: `evals/rerank_grounding/report.py`
- Modify: `evals/rerank_grounding/cli.py`
- Modify: `evals/rerank_grounding/README.md`
- Modify: `tests/unit/test_rerank_grounding_eval.py`

**Interfaces:**
- Produces: `detect_overall_claims(comment: str) -> tuple[OverallClaimCode | str, ...]` using only registered lexical families.
- Extends: `MetricSample` with displayed comment, detected/requested/supported claim codes, downgrade state, failure labels, and final view.
- Extends: `ArmMetrics.as_dict()` with `detectedOverallClaimViolation`, `supportedOverallClaimCoverage`, `overallValidatorDowngradeCount`, `overallInvalidStructuredClaimCount`, and failure-reason counts.

- [ ] **Step 1: Write RED A-detector tests**

Cover positive and negative Korean expressions for top review, all-high rating, all-within-budget, unsupported popularity, and unsupported value-for-money. Assert unrelated prose creates no denominator and that the metric key is exactly `detectedOverallClaimViolation`.

- [ ] **Step 2: Write RED B/C scoring tests**

Assert B counts unsupported structured proposals while displaying raw model comment, C counts downgrade and displays templates, coverage counts supported non-neutral claims over proposed non-neutral claims, and finite failure reasons aggregate deterministically.

- [ ] **Step 3: Run metrics tests and confirm RED**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q tests/unit/test_rerank_grounding_eval.py -k 'overall or detector or metrics'
```

Expected: failures for missing detector/sample fields/metric keys.

- [ ] **Step 4: Implement metrics and runner capture**

Pass fixture `final_view` to Task 2 after every rerank call. Preserve raw comment, raw proposal mappings, final view, detector output, decision, rendered comment, and exact failure labels in `Sample`. A uses detector plus raw oracle; B uses structured proposals plus oracle while preserving model text; C uses the validator decision and deterministic rendered comment.

- [ ] **Step 5: Extend CSV, JSON, report, and manifest output**

Add columns/sections for raw overall comment, raw claims JSON, final view JSON, detected claim codes, requested/supported codes, downgrade, rendered comment, and failure reasons. Add fixture v2 SHA-256 and `overall-comment-grounding-v1` validator version to the manifest. Keep existing latency, token, cost, unknown-usage, failure, and unfilled-cell fields.

- [ ] **Step 6: Run the complete eval unit suite GREEN**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_rerank_grounding_eval.py \
  tests/eval/test_adversarial_recommendation_eval.py \
  tests/eval/test_adversarial_recommendation_runner.py
```

Expected: PASS.

- [ ] **Step 7: Commit measurement support**

Commit runner, metrics, reports, README, and tests with Lore trailers warning that A detection is bounded.

---

### Task 7: Re-measure, document, and verify the rollout gate

**Files:**
- Modify: `CHANGELOG.md`
- Create when credentials exist: `evals/rerank_grounding/baselines/20260812-overall-screening-n3/`
- Create after screening passes: `evals/rerank_grounding/baselines/20260812-overall-confirm-n8-run1/`
- Create after screening passes: `evals/rerank_grounding/baselines/20260812-overall-confirm-n8-run2/`
- Create when credentials do not exist: `evals/rerank_grounding/baselines/20260812-overall-not-tested/README.md`

**Interfaces:**
- Proves: C has zero detected overall violations and zero invalid structured claims without unacceptable rank-coverage regression.
- Preserves: accuracy, coverage, downgrade, p50/p95 latency, input/output/reasoning tokens, confirmed cost, unknown usage, failures, and unfilled cells.

- [ ] **Step 1: Run deterministic smoke measurement**

Run:

```bash
out="/tmp/overall-grounding-dry-${RANDOM}-${RANDOM}/run"
uv run python -m evals.rerank_grounding \
  --arms all --repeats 1 --dry-run \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out "$out"
```

Expected: exit 0 and complete `results.json`, `run_manifest.json`, `samples.csv`, `failures.csv`, and `report.md`. Mark these artifacts as deterministic smoke evidence, not live quality evidence.

- [ ] **Step 2: Run live screening when provider credentials are available**

Run N=3 for A/B/C with the same model and tier:

```bash
uv run python -m evals.rerank_grounding \
  --arms all --repeats 3 --tier smart \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out evals/rerank_grounding/baselines/20260812-overall-screening-n3
```

Continue only if:

```text
validated.detectedOverallClaimViolation.rate == 0
validated.overallInvalidStructuredClaimCount == 0
validated.outOfCandidateIdCount == 0
validated.validRankCoverage >= current.validRankCoverage - 0.05
validated.supportedOverallClaimCoverage is present
unfilledCells == []
```

- [ ] **Step 3: Run two independent live confirmations**

Run N=8 twice with distinct run directories and no fixture/model/prompt/validator changes between runs:

```bash
uv run python -m evals.rerank_grounding \
  --arms all --repeats 8 --tier smart \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out evals/rerank_grounding/baselines/20260812-overall-confirm-n8-run1

uv run python -m evals.rerank_grounding \
  --arms all --repeats 8 --tier smart \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out evals/rerank_grounding/baselines/20260812-overall-confirm-n8-run2
```

If credentials are unavailable, create `evals/rerank_grounding/baselines/20260812-overall-not-tested/README.md` with the failed preflight command and sanitized error, and do not claim live quality validation.

- [ ] **Step 4: Run targeted and regression verification**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q \
  tests/unit/test_overall_comment_grounding.py \
  tests/unit/test_rerank_grounding.py \
  tests/unit/test_rerank_grounding_eval.py \
  tests/unit/test_recommendation.py \
  tests/unit/test_fanout.py \
  tests/unit/test_buyer_tracing.py \
  tests/unit/test_reco_provenance_140.py \
  tests/unit/test_config.py \
  tests/eval/test_adversarial_recommendation_eval.py \
  tests/eval/test_adversarial_recommendation_runner.py
uv run ruff check app evals tests
uv run ruff format --check app evals tests
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 5: Run the near-full suite**

Run:

```bash
PROFILE_DB_URL='' uv run pytest -q
```

Expected: PASS, or only repository-documented environment exclusions recorded verbatim in the final report.

- [ ] **Step 6: Update release documentation and commit evidence**

Document the new validated overall-comment boundary, A rollback, live/not-tested status, and exact artifact paths in `CHANGELOG.md` and `evals/rerank_grounding/README.md`. Commit code, documentation, and evidence with Lore trailers listing every verification command and any provider-network gap.
