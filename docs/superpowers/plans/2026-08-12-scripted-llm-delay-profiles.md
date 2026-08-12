# Scripted LLM Delay Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add instant and 5-second delayed load-test profiles without changing scripted response content or calling a vendor LLM.

**Architecture:** Pydantic settings select `instant | delayed` and carry a bounded delay value. `get_llm()` injects those values into one request-scoped `LoadTestLLM`, which performs a non-blocking delay before its first recognized completion only.

**Tech Stack:** Python 3.12, FastAPI settings via pydantic-settings, asyncio, pytest, Ruff.

## Global Constraints

- Preserve `instant` as the default and therefore preserve all existing behavior.
- Use `asyncio.sleep`, never blocking `time.sleep`.
- Apply delayed mode once per `LoadTestLLM` instance, not once per LLM call.
- Default delayed duration is exactly `5.0` seconds.
- Do not add dependencies or enable scripted mode outside local/test.
- Never run I-17 catalog enrichment while scripted responses are active.
- Require positive deployed rate-limit values and document explicit restoration of every temporary variable.

---

### Task 1: Lock configuration and runtime behavior with failing tests

**Files:**
- Modify: `tests/unit/test_llm_scripted.py`

**Interfaces:**
- Consumes: `Settings`, `get_llm()`, `LoadTestLLM.complete()`.
- Produces: regression expectations for `scripted_llm_mode`, `scripted_llm_delay_s`, and one-shot non-blocking delay.

- [x] **Step 1: Write the failing configuration tests**

Add assertions that `Settings(_env_file=None)` defaults to `instant` and `5.0`, accepts `delayed`, and rejects an unknown mode or a negative delay.

- [x] **Step 2: Write the failing runtime tests**

Monkeypatch `app.core.llm_scripted.asyncio.sleep` with an async recorder. Assert `LoadTestLLM(mode="instant", delay_s=5.0)` records no sleeps and `LoadTestLLM(mode="delayed", delay_s=5.0)` records exactly `[5.0]` after two recognized completions.

- [x] **Step 3: Write the failing wiring test**

Patch `app.core.llm.get_settings` to return scripted delayed settings, call `get_llm()`, and assert the returned `LoadTestLLM` exposes `mode == "delayed"` and `delay_s == 5.0`.

- [x] **Step 4: Run tests and confirm RED**

Run: `uv run pytest tests/unit/test_llm_scripted.py -q`

Expected: failures for missing settings and unsupported `LoadTestLLM` constructor arguments.

### Task 2: Implement the minimal profiles and configuration wiring

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/core/llm.py`
- Modify: `app/core/llm_scripted.py`

**Interfaces:**
- Consumes: `Settings.scripted_llm_mode`, `Settings.scripted_llm_delay_s`.
- Produces: `LoadTestLLM(mode: Literal["instant", "delayed"], delay_s: float)` with read-only properties and one-shot delay behavior.

- [x] **Step 1: Add bounded settings**

Declare `ScriptedLLMMode = Literal["instant", "delayed"]`, then add `scripted_llm_mode: ScriptedLLMMode = "instant"` and `scripted_llm_delay_s: float = Field(default=5.0, ge=0.0, le=60.0)` beside `llm_provider`.

- [x] **Step 2: Inject settings from `get_llm()`**

Construct `LoadTestLLM(mode=settings.scripted_llm_mode, delay_s=settings.scripted_llm_delay_s)` in the scripted branch.

- [x] **Step 3: Add one-shot non-blocking delay**

Import `asyncio`. Give `LoadTestLLM` an initializer that calls `super().__init__()`, stores mode and delay, and initializes `_delay_applied = False`. Before producing a recognized response, set the flag and await `asyncio.sleep(delay_s)` only when mode is delayed and delay is positive.

- [x] **Step 4: Run tests and confirm GREEN**

Run: `uv run pytest tests/unit/test_llm_scripted.py -q`

Expected: all tests pass.

### Task 3: Document deployment switches and verify the focused change

**Files:**
- Modify: `.env.example`
- Modify: `evals/benchmark/README.md`

**Interfaces:**
- Consumes: `SCRIPTED_LLM_MODE`, `SCRIPTED_LLM_DELAY_S`.
- Produces: copyable instant and delayed EC2 configuration examples with honest measurement boundaries.

- [x] **Step 1: Document both profiles**

Add the two variables to `.env.example` and add instant/delayed command examples to the benchmark README. State that delayed mode adds one 5-second wait per request-scoped stub and does not simulate vendor networking or 429s.

- [x] **Step 2: Run focused verification**

Run: `uv run pytest tests/unit/test_llm_scripted.py tests/unit/test_config.py -q`

Expected: all tests pass.

- [x] **Step 3: Run lint**

Run: `uv run ruff check app/core/config.py app/core/llm.py app/core/llm_scripted.py tests/unit/test_llm_scripted.py`

Expected: no lint errors.

- [x] **Step 4: Review the diff**

Run: `git diff --check && git diff -- app/core/config.py app/core/llm.py app/core/llm_scripted.py tests/unit/test_llm_scripted.py .env.example evals/benchmark/README.md`

Expected: no whitespace errors and no unrelated changes.

### Task 4: Close production-data and restoration safety gaps

**Files:**
- Modify: `app/pipelines/scheduler.py`
- Modify: `app/core/config.py`
- Modify: `tests/unit/test_scheduler.py`
- Modify: `tests/unit/test_llm_scripted.py`
- Modify: `DEPLOY.md`
- Modify: `evals/benchmark/README.md`

- [x] Add a regression test proving scripted mode skips only I-17 while retaining lifecycle jobs.
- [x] Skip I-17 registration when `LLM_PROVIDER=scripted` and emit an explicit warning.
- [x] Reject zero and negative `RATE_LIMIT_PER_MIN`/`RATE_LIMIT_PER_HOUR` values.
- [x] Cover concurrent delayed completions with one shared sleep task.
- [x] Document traffic isolation, all-variable restoration, redeployment, and real-model smoke verification.
