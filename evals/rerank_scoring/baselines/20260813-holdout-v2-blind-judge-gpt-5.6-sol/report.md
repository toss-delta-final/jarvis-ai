# Rerank LLM Blind Judge

**Status: exploratory — not confirmatory.**

- Judge: `openai / gpt-5.6-sol` (`smart` tier)
- Source generation model: `gpt-5.6-luna`
- Same judge/source model: `false`
- Source pairs/cases: 599 / 200
- Completed presentations: 1198 / 1198

## Swap-stable results

- Structured wins: 49
- Current wins: 466
- Stable ties: 31
- Position-swap unstable: 53
- Swap consistency: 0.9115
- Structured decisive win rate: 0.0951 (n=515)
- Case-clustered preference share: 0.1344; 95% bootstrap CI [0.0972, 0.1734]

## Position diagnostics

- Same-side A selections: 16
- Same-side B selections: 26
- Same-display-side rate: 0.0701

## Limitations

The judge saw anonymous A/B rankings, neutral product facts, query, and profile only. It did not receive arm names, component scores, search rank, candidate provenance, heuristic relevance labels, or ideal orders.

This is synthetic LLM-judge evidence. It does not establish population superiority, online conversion lift, or confirmatory ranking quality. Human blind review is still required for confirmatory evidence.
