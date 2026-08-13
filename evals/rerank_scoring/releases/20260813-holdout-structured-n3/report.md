# #631 structured rerank sealed holdout result

- candidate: `a01dae743208367e8fdeb04d01bdc33e0cbda470`
- evaluator: `18aab4e42c790790deac33a018c3da0b79a84036`
- dataset: `2.3.0` / `675520d999dc1fbf0a4b32e13914205bc61c606c9adc2f65833eb67fc133ae50`
- model: `openai:gpt-5.6-luna`
- ranking cases: `19` of 24 holdout MFT cases
- seeds: `[11, 29, 47]`
- decision: **keep `current`**

## Primary current → structured

| paired cases | mean ΔnDCG@10 | bootstrap 95% CI | improved / tied / regressed | verdict |
|---:|---:|---:|---:|---|
| 19 | 0.057509 | [-0.038453, 0.169639] | 8 / 6 / 5 | **inconclusive** |

The dev improvement did not pass the sealed holdout release gate because the interval crosses zero.
Production remains `RERANK_RANKING_ARM=current`; no holdout tuning or second candidate run was performed.

## Integrity and stability

- Ranking cells: 57 current + 57 structured; ranking-case provider failures: 0.
- Hard-constraint violations, foreign IDs, duplicates, partial fallback: 0 in both arms.
- Structured stability: top-1 agreement 0.8596, top-3 Jaccard 0.7614, Spearman 0.8839.
- Current stability: top-1 agreement 0.5965, top-3 Jaccard 0.7550, Spearman 0.5615.
- Five failure-boundary cases have zero rerank candidates. Their 60 bounded attempt failures are expected non-ranking evidence and are excluded from the 19-case paired denominator.

Raw sample files are intentionally not duplicated here because they contain sealed relevance grades. Their SHA-256 values are recorded in `run_manifest.json`; the authorized unseal event is recorded in `evals/goldenset/audit/holdout_runs.jsonl`.
