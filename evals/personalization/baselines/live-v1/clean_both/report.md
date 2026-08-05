# Actual-model evaluation report

- Dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- Cases × repeats: 31 × 1
- Primary metric: `overall.ndcgAtK.10`
- Primary summary: N=16, mean=0.554117, median=0.560215, SD=0.377300, IQR=0.712870, 95% CI=[0.368521, 0.735933]
- Hard failures: 2
- Hard-constraint violations: 0 (rate=0.000000)
- Budget exceeded: false
- Token coverage: 1.0
- Cost coverage: 1.0
- Baseline verdict: **notCompared**

## Per-case primary metric

| caseId | N | mean | median | SD | IQR |
|---|---:|---:|---:|---:|---:|
| buy-cmap-0001 | 1 | 0.540460 | 0.540460 | unknown | 0.000000 |
| buy-cmap-0006 | 1 | 0.390380 | 0.390380 | unknown | 0.000000 |
| buy-cold-0001 | 1 | 0.469279 | 0.469279 | unknown | 0.000000 |
| buy-cold-0002 | 1 | 0.000000 | 0.000000 | unknown | 0.000000 |
| buy-gust-0002 | 1 | 0.720449 | 0.720449 | unknown | 0.000000 |
| buy-mult-0001 | 1 | 0.579969 | 0.579969 | unknown | 0.000000 |
| buy-over-0001 | 1 | 0.304467 | 0.304467 | unknown | 0.000000 |
| buy-over-0002 | 1 | 0.000000 | 0.000000 | unknown | 0.000000 |
| buy-over-0003 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-pers-0001 | 1 | 0.754111 | 0.754111 | unknown | 0.000000 |
| buy-pers-0002 | 1 | 0.196946 | 0.196946 | unknown | 0.000000 |
| buy-repu-0001 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-repu-0002 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-srch-0002 | 1 | 0.909816 | 0.909816 | unknown | 0.000000 |
| buy-srch-0005 | 1 | 1.000000 | 1.000000 | unknown | 0.000000 |
| buy-srch-0007 | 1 | 0.000000 | 0.000000 | unknown | 0.000000 |

## Hard failures

- buy-cmap-0003: nonRecommendIntent:general (repeat=0)
- buy-cmap-0004: nonRecommendIntent:general (repeat=0)

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
