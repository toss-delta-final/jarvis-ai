# Validated Grounding Production Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make validated grounding the production buyer rerank default while preserving current as the evaluation CLI baseline and an environment rollback arm.

**Architecture:** Add one validated Settings field and pass it at the production graph-to-rerank boundary. Keep `rerank()` and evaluation runner defaults unchanged so the rollout is explicit, reversible, and isolated from experiment semantics.

**Tech Stack:** Python 3.12, Pydantic Settings, pytest/pytest-asyncio, Ruff; no new dependencies.

## Global Constraints

- Production Settings default is `validated`.
- `RERANK_GROUNDING_ARM=current` restores A without a code change.
- Evaluation CLI default remains `current`.
- Invalid grounding metadata changes only rationale text, never candidate IDs or rank.

---

### Task 1: Lock production arm selection

**Files:**
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_fanout.py`
- Modify: `app/core/config.py`
- Modify: `app/agents/buyer/recommendation/graph.py`

**Interfaces:**
- Produces: `Settings.rerank_grounding_arm: Literal["current", "prompt_only", "validated"]`
- Consumes: existing `rerank(..., grounding_arm: GroundingArm)`

- [ ] Add a failing Settings test for default C, explicit A, and invalid value rejection.
- [ ] Add a failing graph spy test that observes `grounding_arm="validated"` at the rerank boundary.
- [ ] Run both focused tests and confirm the missing setting/wiring failures.
- [ ] Add the Settings field and pass it from `stream_recommendation()` to `rerank()`.
- [ ] Run both focused tests and the existing grounding unit tests green.

### Task 2: Preserve experiment semantics and document rollout

**Files:**
- Modify: `.env.example`
- Modify: `evals/adversarial_recommendation/README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Documents: production default C, environment rollback A, evaluation CLI default A.

- [ ] Add the rollback environment example and explain the production/evaluation default split.
- [ ] Record the evidence-backed production promotion in the changelog.
- [ ] Run adversarial runner tests to prove explicit arm injection still overrides production Settings.

### Task 3: Verify the rollout

**Files:**
- Verify only; no additional production files.

**Interfaces:**
- Proves the production and evaluation defaults are intentionally different.

- [ ] Run grounding, recommendation graph, adversarial evaluation, and Settings tests.
- [ ] Run Ruff check/format and `git diff --check`.
- [ ] Statically assert production Settings default C, graph wiring C, and CLI parser default A.
- [ ] Commit with Lore trailers and report any untested production-network gap.
