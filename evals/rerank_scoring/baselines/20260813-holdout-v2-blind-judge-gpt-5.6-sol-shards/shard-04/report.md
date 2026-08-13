# Rerank LLM Blind Judge

**Status: exploratory — not confirmatory.**

- Judge: `openai / gpt-5.6-sol` (`smart` tier)
- Source generation model: `gpt-5.6-luna`
- Same judge/source model: `false`
- Source pairs/cases: 98 / 33
- Completed presentations: 196 / 196

## Swap-stable results

- Structured wins: 8
- Current wins: 80
- Stable ties: 3
- Position-swap unstable: 7
- Swap consistency: 0.9286
- Structured decisive win rate: 0.0909 (n=88)
- Case-clustered preference share: 0.1136; 95% bootstrap CI [0.0404, 0.1995]

## Position diagnostics

- Same-side A selections: 3
- Same-side B selections: 3
- Same-display-side rate: 0.0612

## Limitations

The judge saw anonymous A/B rankings, neutral product facts, query, and profile only. It did not receive arm names, component scores, search rank, candidate provenance, heuristic relevance labels, or ideal orders.

This is synthetic LLM-judge evidence. It does not establish population superiority, online conversion lift, or confirmatory ranking quality. Human blind review is still required for confirmatory evidence.
