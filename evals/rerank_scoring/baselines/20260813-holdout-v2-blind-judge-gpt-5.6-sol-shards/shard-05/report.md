# Rerank LLM Blind Judge

**Status: exploratory — not confirmatory.**

- Judge: `openai / gpt-5.6-sol` (`smart` tier)
- Source generation model: `gpt-5.6-luna`
- Same judge/source model: `false`
- Source pairs/cases: 99 / 33
- Completed presentations: 198 / 198

## Swap-stable results

- Structured wins: 11
- Current wins: 74
- Stable ties: 3
- Position-swap unstable: 11
- Swap consistency: 0.8889
- Structured decisive win rate: 0.1294 (n=85)
- Case-clustered preference share: 0.1515; 95% bootstrap CI [0.0556, 0.2677]

## Position diagnostics

- Same-side A selections: 3
- Same-side B selections: 4
- Same-display-side rate: 0.0707

## Limitations

The judge saw anonymous A/B rankings, neutral product facts, query, and profile only. It did not receive arm names, component scores, search rank, candidate provenance, heuristic relevance labels, or ideal orders.

This is synthetic LLM-judge evidence. It does not establish population superiority, online conversion lift, or confirmatory ranking quality. Human blind review is still required for confirmatory evidence.
