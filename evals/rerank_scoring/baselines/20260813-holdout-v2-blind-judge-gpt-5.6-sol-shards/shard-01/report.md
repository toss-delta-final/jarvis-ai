# Rerank LLM Blind Judge

**Status: exploratory — not confirmatory.**

- Judge: `openai / gpt-5.6-sol` (`smart` tier)
- Source generation model: `gpt-5.6-luna`
- Same judge/source model: `false`
- Source pairs/cases: 102 / 34
- Completed presentations: 204 / 204

## Swap-stable results

- Structured wins: 7
- Current wins: 81
- Stable ties: 9
- Position-swap unstable: 5
- Swap consistency: 0.9510
- Structured decisive win rate: 0.0795 (n=88)
- Case-clustered preference share: 0.1373; 95% bootstrap CI [0.0539, 0.2353]

## Position diagnostics

- Same-side A selections: 2
- Same-side B selections: 2
- Same-display-side rate: 0.0392

## Limitations

The judge saw anonymous A/B rankings, neutral product facts, query, and profile only. It did not receive arm names, component scores, search rank, candidate provenance, heuristic relevance labels, or ideal orders.

This is synthetic LLM-judge evidence. It does not establish population superiority, online conversion lift, or confirmatory ranking quality. Human blind review is still required for confirmatory evidence.
