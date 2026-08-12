# Adversarial Grounding Arms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect PR #638 adversarial cases to current, prompt-only, and validated rerank grounding arms while replaying B output for C.

**Architecture:** Keep production `stream_recommendation()` unchanged. The evaluation runner injects an arm through its existing sequential patch scope, captures grounding decisions, and derives C from B without another provider call. CLI and artifacts remain backward compatible for the default current-only run.

**Tech Stack:** Python 3.12, argparse, dataclasses, unittest.mock, pytest/pytest-asyncio, existing eval artifact writer; no new dependencies.

## Global Constraints

- Production `rerank(..., grounding_arm="current")` and graph call sites remain unchanged.
- B/C paired comparison uses one structured LLM response.
- Existing #638 artifact fields remain available.
- Live calls stay manual and outside CI.

---

### Task 1: Lock arm injection and validated derivation

**Files:**
- Modify: `tests/eval/test_adversarial_recommendation_runner.py`
- Modify: `evals/adversarial_recommendation/runner.py`

**Interfaces:**
- Produces: `AdversarialBuyerRunner(..., grounding_arm: GroundingArm = "current")`
- Produces: `derive_validated_execution(execution: dict[str, Any]) -> dict[str, Any]`

- [ ] Add tests that structured runner output records `groundingArm` and decisions.
- [ ] Run the focused tests and confirm RED because the constructor has no arm argument.
- [ ] Inject the arm in the evaluation patch scope and serialize decisions.
- [ ] Add a RED test that derived C preserves ranks but templates reasons and clears provider calls.
- [ ] Implement the minimal deep-copy transformation and run focused tests GREEN.

### Task 2: Connect CLI matrix and artifacts

**Files:**
- Modify: `tests/eval/test_adversarial_recommendation_runner.py`
- Modify: `evals/adversarial_recommendation/__main__.py`
- Modify: `evals/adversarial_recommendation/scoring.py`
- Modify: `evals/adversarial_recommendation/report.py`

**Interfaces:**
- Produces: `_parse_arms(raw: str) -> tuple[GroundingArm, ...]`
- Produces: combined arm-labelled `results.jsonl`, `summary.json`, `run_manifest.json`, `report.md`

- [ ] Add parser and `--arms all` CLI artifact tests.
- [ ] Run them and confirm RED because `--arms` is unknown.
- [ ] Implement canonical arm parsing and sequential A/B execution with C derivation.
- [ ] Add arm to scored rows and arm-specific summary/manifest fields.
- [ ] Run focused tests GREEN.

### Task 3: Document and verify the connected experiment

**Files:**
- Modify: `evals/adversarial_recommendation/README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Documents exact scripted/live `--arms all` commands and the B→C replay interpretation.

- [ ] Document commands, compatibility, and limits.
- [ ] Run dataset validator and generator check.
- [ ] Run adversarial and grounding eval/unit tests.
- [ ] Run Ruff and `git diff --check`.
- [ ] Commit with Lore trailers and preserve the worktree for review.
