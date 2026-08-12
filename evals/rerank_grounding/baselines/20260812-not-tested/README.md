# Rerank grounding live evaluation — not tested

- **Status:** `not tested`
- **Attempted at:** `2026-08-11T22:56:35Z`
- **Git commit:** `eea5c05bcc8c8d59e90a68181b5ac284ac0037e3`
- **Working tree before attempt:** clean
- **Provider/model configuration:** `openai` / `gpt-5.6-luna` smart tier
- **Secret values recorded:** none

## Attempted command

```bash
uv run python -m evals.rerank_grounding \
  --arms all --repeats 3 --tier smart \
  --out evals/rerank_grounding/baselines/20260812-screening-n3
```

## Result

The CLI stopped during preflight before any provider call or output-directory creation:

```text
LLM 설정 오류: openai API key is not configured
exit_code=2
```

The worktree process reported `openai_key_present=false` and `anthropic_key_present=false`. The original
checkout also had no `.env`, `.env.local`, or `.env.test` file to reference. No alternate credential source was
available, so N=3 screening and N=8×2 confirmation were not run.

## Evidence that is available

- deterministic A/B/C dry-run completed all 10 cases and generated all five artifact types;
- the scripted adversarial output produced `current=10/27`, `prompt_only=10/27`, and `validated=0/27`
  unsupported evidence, with candidate-ID errors `0`, duplicates `0`, and coverage `1.0` for every arm;
- 322 relevant tests passed and repository `ruff check` passed;
- the production default remains `grounding_arm="current"`.

The dry-run numbers prove the harness and validator behavior, not real-model quality. They must not be presented
as live improvement evidence. Frozen C1~C4 release claims remain unchanged.
