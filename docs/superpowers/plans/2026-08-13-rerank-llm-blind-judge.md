# Rerank LLM Blind Judge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an auditable, position-swapped LLM blind comparison of the committed `current`
and `structured` rerank outputs for the 200-case prospective dataset.

**Architecture:** A strict schema module owns arm-blind presentation and verdict contracts. A pure
evaluation module loads paired CSV samples without labels, constructs deterministic swapped
presentations, validates verdicts, and aggregates stable outcomes with a case-clustered bootstrap.
A CLI module owns provider configuration, retries, pacing, budgets, concurrency, and immutable
artifact output; a report module renders the same result without recomputing metrics.

**Tech Stack:** Python 3.12, Pydantic 2, asyncio, existing `LLMClient`, evaluation pacing/budget
utilities, pytest, Ruff.

## Global Constraints

- Reuse the committed 200-case source samples; do not generate new rerank outputs.
- Load the prospective dataset with `label_policy="none"`; do not read heuristic labels.
- Every pair is judged in both A/B orientations, and inconsistent swaps are excluded from decisive
  win rates.
- Do not expose arm names, scoring decisions, search ranks, candidate provenance, relevance labels,
  prompt hashes, or ideal orders to the judge.
- Store raw response hashes and parsed bounded verdicts, not exact raw model text.
- The result remains exploratory and must not automate rollout or claim confirmatory superiority.
- Add no dependencies.

---

### Task 1: Strict blind contracts and deterministic presentations

**Files:**
- Create: `evals/rerank_scoring/judge_schema.py`
- Create: `evals/rerank_scoring/judge.py`
- Test: `tests/eval/test_rerank_blind_judge.py`

**Interfaces:**
- Consumes: `RankingCaseCore`, catalog mappings, committed `samples.csv` rows.
- Produces: `CandidateFact`, `BlindPresentation`, `CoordinatorMapping`, `JudgeVerdict`,
  `load_source_pairs(...)`, and `build_presentations(...)`.

- [ ] **Step 1: Write failing contract tests**

  Add tests that request strict Pydantic validation, reject extra/forbidden fields and arm words,
  verify all public serialized keys, verify deterministic opaque IDs, and assert that orientation 1
  swaps orientation 0 exactly.

- [ ] **Step 2: Run tests and verify RED**

  Run:
  `uv run pytest -q tests/eval/test_rerank_blind_judge.py -k 'presentation or verdict'`

  Expected: import failure because the judge modules do not exist.

- [ ] **Step 3: Implement minimal contracts and builders**

  Use `ConfigDict(extra="forbid")`, literal A/B/tie values, confidence bounds, bounded reason codes,
  deterministic SHA-256 IDs, and JSON serialization with camel-case aliases. Build candidate facts
  from the exact post-filter `candidateOrder`, sorted by product ID, using the existing qualitative
  rerank tier helpers. Store slices and arm mappings only in coordinator objects.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Run:
  `uv run pytest -q tests/eval/test_rerank_blind_judge.py -k 'presentation or verdict'`

- [ ] **Step 5: Commit the contract slice**

  Commit the tested schema, loader, builders, and tests with Lore trailers.

### Task 2: Swap-stable aggregation and case-clustered uncertainty

**Files:**
- Modify: `evals/rerank_scoring/judge.py`
- Modify: `tests/eval/test_rerank_blind_judge.py`

**Interfaces:**
- Consumes: validated `CoordinatorMapping` and per-presentation `JudgeResponse` rows.
- Produces: `analyze_judgments(...) -> dict[str, object]`.

- [ ] **Step 1: Write failing aggregation tests**

  Construct stable structured/current/tie pairs, A-side and B-side inconsistent pairs, failed
  presentations, repeated seeds for one case, and guest/member/stratum slices. Assert exact
  denominators, arm remapping, unstable exclusion, position-bias counters, case majority outcomes,
  and deterministic bootstrap output.

- [ ] **Step 2: Run tests and verify RED**

  Run:
  `uv run pytest -q tests/eval/test_rerank_blind_judge.py -k 'analysis or bootstrap'`

  Expected: failure because aggregation is missing.

- [ ] **Step 3: Implement minimal aggregation**

  Map A/B to arms through coordinator records after collection. Count only identical real outcomes
  as stable. Compute pair-level decisive win rates, equal-case preference scores (`structured=1`,
  `tie=0.5`, `current=0`), a fixed-seed 10,000-resample case bootstrap, per-slice summaries, and
  case-majority counts. Preserve incomplete and failed denominators.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Run:
  `uv run pytest -q tests/eval/test_rerank_blind_judge.py -k 'analysis or bootstrap'`

- [ ] **Step 5: Commit the analysis slice**

  Commit aggregation and tests with Lore trailers.

### Task 3: Budgeted live CLI and immutable artifacts

**Files:**
- Create: `evals/rerank_scoring/judge_cli.py`
- Create: `evals/rerank_scoring/judge_report.py`
- Modify: `evals/rerank_scoring/README.md`
- Modify: `tests/eval/test_rerank_blind_judge.py`

**Interfaces:**
- Consumes: source sample CSV, dataset root, `LLMClient`, pricing, pacing, and model-eval budget.
- Produces: CLI `python -m evals.rerank_scoring.judge_cli` and the seven design artifacts.

- [ ] **Step 1: Write failing CLI/artifact tests**

  Test dry-run execution with a scripted judge, refusal to overwrite output, explicit budget
  requirements for live mode, source dataset/hash mismatch rejection, prompt leakage scanning,
  artifact hash provenance, and report caveats.

- [ ] **Step 2: Run tests and verify RED**

  Run:
  `uv run pytest -q tests/eval/test_rerank_blind_judge.py -k 'cli or artifact or report'`

  Expected: import or assertion failure because CLI/report code is missing.

- [ ] **Step 3: Implement the judge prompt and runner**

  Freeze a JSON-only rubric prompt, serialize only `BlindPresentation`, run presentations with a
  bounded semaphore, pass every attempt through `PacedLLM` and `RecordingLLM`, retry invalid/model
  failures up to `--attempt-multiplier`, and propagate `BudgetExceeded`. Permit an explicit judge
  model override while recording the resolved provider/model and source generation model.

- [ ] **Step 4: Implement artifact and report writers**

  Write sorted JSONL/JSON atomically into a previously nonexistent directory. Include exact input,
  prompt, presentation, response, mapping, and git provenance hashes. Render counts, confidence
  interval, slices, judge/source model relationship, failures, and exploratory limitations.

- [ ] **Step 5: Run focused tests and verify GREEN**

  Run: `uv run pytest -q tests/eval/test_rerank_blind_judge.py`

- [ ] **Step 6: Commit the CLI slice**

  Commit CLI, reporting, README, and tests with Lore trailers.

### Task 4: Execute and preserve the 200-case blind comparison

**Files:**
- Create: `evals/rerank_scoring/baselines/20260813-holdout-v2-blind-judge-<model>/...`
- Modify: `evals/rerank_scoring/README.md`

**Interfaces:**
- Consumes: committed source baseline and live provider credentials.
- Produces: immutable exploratory blind-judge evidence.

- [ ] **Step 1: Run a one-case live smoke test**

  Use both orientations, an explicit model/call/cost budget, and a temporary output path. Inspect
  validated responses and verify no forbidden arm/label fields appear in presentations.

- [ ] **Step 2: Run the full 599-pair evaluation**

  Execute 1,198 planned presentations with bounded concurrency and explicit call/cost/rate limits.
  Preserve failures; do not silently substitute heuristic labels or one-sided judgments.

- [ ] **Step 3: Reproduce analysis from artifacts**

  Reload the public responses and coordinator mappings, recompute `results.json`, and require exact
  equality. Recompute all recorded SHA-256 values and scan public presentations for forbidden arm
  and label vocabulary.

- [ ] **Step 4: Update evidence documentation**

  Add the exact judge model, calls, completion rate, stable/unstable outcomes, interval, cost,
  latency if available, and limitations to the rerank README.

- [ ] **Step 5: Commit the evidence**

  Commit only bounded parsed/raw-hash artifacts and documentation, never credentials or raw provider
  text, with Lore trailers describing the exploratory status.

### Task 5: Final branch verification

**Files:**
- Verify all files changed since `308aad53`.

**Interfaces:**
- Consumes: complete branch diff and committed artifacts.
- Produces: fresh verification evidence and a clean worktree.

- [ ] **Step 1: Run focused eval tests**

  Run: `uv run pytest -q tests/eval/test_rerank_blind_judge.py tests/eval/test_rerank_scoring_runner.py`

- [ ] **Step 2: Run targeted pre-commit**

  Run pre-commit only on changed Python/Markdown/JSON files; do not use `--all-files` because the
  repository's pinned formatter would rewrite unrelated legacy files.

- [ ] **Step 3: Run the hermetic full suite**

  Run:
  `PROFILE_DB_URL='postgresql://invalid:invalid@127.0.0.1:1/invalid' STATE_STORE_CONNECT_TIMEOUT_S=0.1 uv run pytest -q`

- [ ] **Step 4: Verify repository and artifact state**

  Require `git diff --check`, a clean `git status --short`, exact artifact reconstruction, and a
  secret-pattern scan before reporting completion.
