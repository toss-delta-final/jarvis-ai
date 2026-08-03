# Actual-model evaluation report

- Dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- Cases × repeats: 31 × 1
- Primary metric: `overall.ndcgAtK.10`
- Primary summary: N=18, mean=0.724036, median=0.792833, SD=0.321331, IQR=0.420031, 95% CI=[0.572864, 0.861062]
- Hard failures: 0
- Hard-constraint violations: 0 (rate=0.000000)
- Budget exceeded: false
- Token coverage: 1.0
- Cost coverage: 1.0
- Baseline verdict: **notCompared**

## Per-case primary metric

| caseId | N | mean | median | SD | IQR |
|---|---:|---:|---:|---:|---:|
| buy-cmap-0001 | 1 | 0.321204 | 0.321204 | unknown | 0.000000 |
| buy-cmap-0003 | 1 | 0.979707 | 0.979707 | unknown | 0.000000 |
| buy-cmap-0004 | 1 | 0.831555 | 0.831555 | unknown | 0.000000 |
| buy-cmap-0006 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-cold-0001 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-cold-0002 | 1 | 0.697882 | 0.697882 | unknown | 0.000000 |
| buy-gust-0002 | 1 | 0.711246 | 0.711246 | unknown | 0.000000 |
| buy-mult-0001 | 1 | 0.579969 | 0.579969 | unknown | 0.000000 |
| buy-over-0001 | 1 | 0.323587 | 0.323587 | unknown | 0.000000 |
| buy-over-0002 | 1 | 0.181344 | 0.181344 | unknown | 0.000000 |
| buy-over-0003 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-pers-0001 | 1 | 0.754111 | 0.754111 | unknown | 0.000000 |
| buy-pers-0002 | 1 | 0.690441 | 0.690441 | unknown | 0.000000 |
| buy-repu-0001 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-repu-0002 | 1 | 0.000000 | 0.000000 | unknown | 0.000000 |
| buy-srch-0002 | 1 | 0.961598 | 0.961598 | unknown | 0.000000 |
| buy-srch-0005 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-srch-0007 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |

## Hard failures

- None

## Ranking denominator exclusions

- buy-cmap-0002: nonDiscriminativeRanking
- buy-cmap-0005: emptyRelevance
- buy-fail-0001: emptyRelevance
- buy-fail-0002: emptyRelevance
- buy-fail-0003: emptyRelevance
- buy-gust-0001: nonDiscriminativeRanking
- buy-mult-0002: nonDiscriminativeRanking
- buy-pers-0003: nonDiscriminativeRanking
- buy-repu-0003: emptyRelevance
- buy-srch-0001: nonDiscriminativeRanking
- buy-srch-0003: nonDiscriminativeRanking
- buy-srch-0004: nonDiscriminativeRanking
- buy-srch-0006: nonDiscriminativeRanking

## Secondary metrics (exploratory)

- None

Secondary metrics and slices are exploratory; only the configured primary metric is confirmatory.
