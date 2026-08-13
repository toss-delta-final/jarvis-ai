# Rerank LLM Blind Judge

**Status: exploratory — not confirmatory.**

- Judge: `openai / gpt-5.6-sol` (`smart` tier)
- Source generation model: `gpt-5.6-luna`
- Same judge/source model: `false`
- Source pairs/cases: 99 / 33
- Completed presentations: 198 / 198

## Swap-stable results

- Structured wins: 8
- Current wins: 73
- Stable ties: 6
- Position-swap unstable: 12
- Swap consistency: 0.8788
- Structured decisive win rate: 0.0988 (n=81)
- Case-clustered preference share: 0.1465; 95% bootstrap CI [0.0505, 0.2576]

## Position diagnostics

- Same-side A selections: 5
- Same-side B selections: 5
- Same-display-side rate: 0.1010

## Limitations

The judge saw anonymous A/B rankings, neutral product facts, query, and profile only. It did not receive arm names, component scores, search rank, candidate provenance, heuristic relevance labels, or ideal orders.

This is synthetic LLM-judge evidence. It does not establish population superiority, online conversion lift, or confirmatory ranking quality. Human blind review is still required for confirmatory evidence.
