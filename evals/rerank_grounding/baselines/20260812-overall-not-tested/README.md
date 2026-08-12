# #645 overall-comment live evaluation — not tested

## Status

Live screening and confirmation were **not run** because the configured `openai` provider has no API
credential in this worktree environment. No secret value was printed or persisted.

Sanitized preflight result:

```json
{"provider":"openai","credentialConfigured":false,"smartModel":"gpt-5.6-luna"}
```

Required live commands remain:

```bash
uv run python -m evals.rerank_grounding \
  --arms all --repeats 3 --tier smart \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out evals/rerank_grounding/baselines/20260812-overall-screening-n3

uv run python -m evals.rerank_grounding \
  --arms all --repeats 8 --tier smart \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out evals/rerank_grounding/baselines/20260812-overall-confirm-n8-run1

uv run python -m evals.rerank_grounding \
  --arms all --repeats 8 --tier smart \
  --fixture evals/rerank_grounding/fixtures/rerank_grounding_v2.json \
  --out evals/rerank_grounding/baselines/20260812-overall-confirm-n8-run2
```

Do not merge #645 on deterministic evidence alone; the design requires N=3 screening and independent N=8×2
confirmation with unchanged fixture/model/prompt/validator hashes.

## Deterministic smoke

The v2 dry-run completed 22 cases × 3 arms × N=1 = 66 scripted calls with no unfilled cells.

- status: `not tested`
- C detected overall violation: `0/22` (`0.0`)
- C supported overall claim coverage: `13/28` (`0.4642857143`)
- C overall invalid after validation: `0`
- token usage and provider cost: unavailable for scripted calls
- latency: synthetic local execution only; not a provider latency measurement

This proves fixture/validator/runner/artifact completeness, not model quality.

## Historical A baseline rescore

`historical_a_rescore.json` deterministically re-scores the archived #632 A rows without LLM calls.
The tightened bounded detector avoids treating phrases such as “후기가 많은 조건에 가장 잘 맞아요” or
“평점은 높지만…” as whole-list superlatives.

Across screening plus two confirmations:

- A samples: `190`
- detected/scored claims: `11`
- violations: `1`
- bounded detected violation rate: `1/11` (`0.0909090909`)
- detected families: `ALL_RATING_HIGH=10`, `VALUE_FOR_MONEY_TOP=1`
- unscored budget claims: `0`

Limit: archived #632 rows did not preserve final budget/list oracle. Their `rankedProductIds` are therefore used as
a one-group `PICK_ONE` proxy, and any budget claim would be excluded from the correctness denominator.
