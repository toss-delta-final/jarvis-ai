# Code-assisted Buyer Rerank Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a selectable `code_assisted` buyer rerank arm that supplies deterministic product signals to one LLM final-selection call and renders detailed reasons only from validated evidence.

**Architecture:** A focused pure module owns code-derived signals, LLM row validation, and safe reason rendering. `rerank.py` keeps prompt orchestration and delegates only the new arm to that module; `graph.py` supplies effective filters and search ranks while preserving all existing arms and fallback behavior. The evaluation harness recognizes the new arm as an independent provider call, but no live evaluation is executed in this change.

**Tech Stack:** Python 3.12, dataclasses, Pydantic settings/models, existing async `LLMClient`, existing rerank evaluation harness.

## Global Constraints

- Preserve `current`, `structured`, and `hybrid` prompts, ranking behavior, defaults, and rollback paths.
- Do not add external dependencies, APIs, or database state.
- Do not parse profile markdown in deterministic code.
- Do not expose internal scores or profile contents on the user wire.
- Do not promote `code_assisted` to the default ranking arm.
- Per explicit user direction, do not add tests and do not run test commands for this implementation.
- Pre-commit hooks may run as part of committing, but their result is not a behavioral test claim.

---

### Task 1: Deterministic code signals and reason rendering

**Files:**
- Create: `app/agents/buyer/recommendation/rerank_code_assisted.py`

**Interfaces:**
- Consumes: `SpringProduct`, `ProductSearchFilters`, precomputed `CandidateGroundingFacts`, candidate search rank, optional need mapping.
- Produces: `CodeScoringContext`, `CandidateCodeSignals`, `CodeAssistedDecision`, `build_candidate_code_signals()`, `parse_code_assisted_ranking()`, `fallback_reason_for_signals()`.

- [ ] **Step 1: Define immutable context and signal records**

```python
@dataclass(frozen=True)
class CodeScoringContext:
    filters: ProductSearchFilters
    search_rank_by_id: Mapping[int, int]
    need_of: Mapping[int, str] | None = None
    total_budget: int | None = None

@dataclass(frozen=True)
class CandidateCodeSignals:
    product_id: int
    search_rank: int
    need: str | None
    facts: CandidateGroundingFacts
    evidence: tuple[CodeEvidence, ...]
    rating_quality: int | None
    review_confidence: int | None
    condition_matched: int
    condition_applicable: int
```

- [ ] **Step 2: Derive only mechanically supported evidence**

Generate evidence identifiers for exact category/brand/attribute/color matches, verified price-range and rating-threshold matches, and existing rating/review/relative-price tiers. Missing source data is not counted as a mismatch and never produces evidence. Keep `searchRank` exact rather than inventing a backend relevance score.

- [ ] **Step 3: Validate LLM-selected rows**

Accept only candidate IDs exactly once with integer `semanticIntentFit 0..4`, `useCaseFit 0..3`, and `profileFit 0..1`; require `profileFit == 0` without a profile. Ignore foreign IDs and duplicates, discard invalid rows, cap at `expose_max`, and raise `CodeAssistedSchemaError` if no valid row remains.

- [ ] **Step 4: Render at most two validated clauses**

Validate every `evidenceRefs` value against the candidate's code evidence and validate `semanticReasonCode` against:

```text
DIRECT_INTENT_MATCH | USE_CASE_MATCH | PROFILE_TIEBREAK | NO_SEMANTIC_REASON
```

Prefer explicit-condition evidence, then one quality/price evidence, then a supported semantic clause. Return the existing neutral rationale if no safe clause remains. Also expose whether unsupported references were removed for observability.

- [ ] **Step 5: Commit the pure module with the integration changes in Task 4**

The new types are not useful independently, so commit them with their `rerank.py` consumer rather than creating a dead intermediate commit.

### Task 2: Extend ranking and result types without changing old arms

**Files:**
- Modify: `app/agents/buyer/recommendation/rerank_scoring.py`
- Modify: `app/agents/buyer/recommendation/state.py`
- Modify: `app/core/config.py`

**Interfaces:**
- Consumes: `CodeAssistedDecision` under `TYPE_CHECKING` in state.
- Produces: `RankingArm = Literal["current", "structured", "hybrid", "code_assisted"]`; `RerankResult.code_assisted_decisions`; validated setting value.

- [ ] **Step 1: Extend only the public arm type**

Keep `ScoredRankingArm = Literal["structured", "hybrid"]` unchanged so `compute_scored_ranking()` cannot accidentally receive `code_assisted`.

- [ ] **Step 2: Add code-assisted decisions to `RerankResult`**

```python
code_assisted_decisions: list[CodeAssistedDecision] = field(default_factory=list)
```

Use `TYPE_CHECKING` to avoid a runtime import cycle.

- [ ] **Step 3: Allow the new configured arm but preserve the default**

```python
rerank_ranking_arm: Literal[
    "current", "structured", "hybrid", "code_assisted"
] = "structured"
```

Update the adjacent comment to state that `code_assisted` is opt-in pending paired evidence.

### Task 3: Pass effective deterministic context from the graph

**Files:**
- Modify: `app/agents/buyer/recommendation/graph.py`

**Interfaces:**
- Consumes: `CodeScoringContext`.
- Produces: `code_scoring_context` passed to `rerank()`.

- [ ] **Step 1: Build first-occurrence search ranks at the final candidate boundary**

```python
search_rank_by_id: dict[int, int] = {}
for rank, candidate in enumerate(candidates, 1):
    search_rank_by_id.setdefault(candidate.product_id, rank)
```

This preserves the existing candidate order while remaining deterministic if duplicate product IDs reach the boundary.

- [ ] **Step 2: Pass the effective request state**

```python
code_scoring_context=CodeScoringContext(
    filters=effective_filters,
    search_rank_by_id=search_rank_by_id,
    need_of=need_of,
    total_budget=decision.total_budget,
)
```

Also pass `search_rank_by_id` through the existing argument so `structured` and `hybrid` use the same production search-rank authority instead of candidate prompt order.

- [ ] **Step 3: Preserve degrade semantics and add factual fallback reasons**

On LLM or schema failure, retain the existing search-order degrade path, degraded trace, and fallback notice. For `code_assisted` only, call `code_assisted_fallback_reasons()` so search-order fallback products can receive one verified code evidence reason. Do not claim a semantic reason for a product the model did not select.

### Task 4: Add the code-assisted prompt and orchestration branch

**Files:**
- Modify: `app/agents/buyer/recommendation/rerank.py`

**Interfaces:**
- Consumes: optional `CodeScoringContext`, code-signal builder, and parser.
- Produces: one-call `code_assisted` `RerankResult` containing selected products, rendered rationales, grounding decisions, and code-assisted decisions; `code_assisted_fallback_reasons()` for graph degrade.

- [ ] **Step 1: Add `_SYSTEM_CODE_ASSISTED`**

Require only a top-`expose_max` `ranked` array, the three semantic fields, a bounded semantic reason enum, and evidence references. State that all `codeSignals` values are authoritative and may not be recalculated or contradicted.

- [ ] **Step 2: Extend the function boundary**

```python
code_scoring_context: CodeScoringContext | None = None
```

For direct callers that select `code_assisted` without a context, derive an empty-filter context and first-occurrence ranks locally. Existing arms ignore the new argument.

- [ ] **Step 3: Augment only code-assisted candidate payloads**

Reuse the existing candidate payload and tier calculations. Add `searchRank`, `codeSignals`, and evidence records only when `ranking_arm == "code_assisted"`; do not modify the serialized payload of the other arms.

- [ ] **Step 4: Give code-assisted current-sized output budget**

Use `expose_max` rather than `len(candidates)` for output item count. Keep the scored reasoning reserve limited to `structured` and `hybrid` so the new arm does not inherit their all-candidate output cost.

- [ ] **Step 5: Parse and render the selected rows**

Delegate validation to `parse_code_assisted_ranking()`. For `grounding_arm == "validated"`, use its evidence renderer; for `current` or `prompt_only`, preserve the model's bounded rationale while still recording validation decisions. Return existing overall comment/claim fields through the current downstream validators.

- [ ] **Step 6: Expose factual fallback rendering to the graph**

Build the same candidate facts and code signals without an LLM call and render the highest-priority verified code evidence for each requested fallback product. Keep this helper synchronous and side-effect free.

- [ ] **Step 7: Commit Tasks 1-4**

Use a Lore commit describing the signal-ownership boundary and explicitly record that tests were not run by user request.

### Task 5: Make the evaluation harness aware of the independent arm

**Files:**
- Modify: `evals/rerank_scoring/runner.py`
- Modify: `evals/rerank_scoring/cli.py`
- Modify: `evals/rerank_scoring/fakes.py`

**Interfaces:**
- Consumes: `_SYSTEM_CODE_ASSISTED`, `RankingArm` with the new literal.
- Produces: CLI parsing, prompt hashes, provider-call budgeting, and deterministic dry-run output for `code_assisted`.

- [ ] **Step 1: Add the arm to CLI and runner validation**

Update `ALL_ARMS`, `_ARMS`, help text, and validation errors to include `code_assisted`.

- [ ] **Step 2: Execute it as an independent provider call**

Continue sharing one response only between `structured` and `hybrid`. Run `code_assisted` separately because its prompt and output schema differ. Count that separate call in `provider_calls_per_cell`.

- [ ] **Step 3: Supply evaluation context**

Construct `ProductSearchFilters` from the case's available hard constraints and pass a `CodeScoringContext` with the case's immutable `search_rank_by_id`. Do not invent unavailable brand, attribute, color, or profile-vector labels.

- [ ] **Step 4: Record provenance**

Add the code-assisted prompt hash and named component ownership to the manifest. Do not compare or merge its raw response with structured/hybrid.

- [ ] **Step 5: Extend the scripted fake by prompt schema**

When the system prompt contains the code-assisted schema, return selected `ranked` rows with valid semantic fields and only evidence references found in each candidate's `codeSignals`.

- [ ] **Step 6: Commit the harness support**

Use a Lore commit stating that the harness was wired but not executed.

### Task 6: Documentation and non-test review

**Files:**
- Modify: `docs/superpowers/specs/2026-08-13-code-assisted-rerank-design.md` only if implementation names differ from the approved contract.

**Interfaces:**
- Consumes: final implementation symbols.
- Produces: implementation-consistent design record and clean worktree.

- [ ] **Step 1: Inspect the final diff for old-arm contamination**

Confirm by code review that old prompt constants and their candidate JSON are unchanged, the default remains `structured`, and `compute_scored_ranking()` still accepts only `structured|hybrid`.

- [ ] **Step 2: Run no behavioral verification commands**

Do not invoke pytest, evaluation CLI, live LLM, typecheck, compile, or manual smoke commands. Record this validation gap in the final response and commit trailers.

- [ ] **Step 3: Commit any documentation synchronization**

Use a Lore commit only if documentation required a correction; otherwise leave the implementation commits as the final branch state.
