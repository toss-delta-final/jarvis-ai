# Actual-model evaluation report

- Dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- Cases × repeats: 31 × 5
- Primary metric: `overall.ndcgAtK.10`
- Primary summary: N=18, mean=0.696084, median=0.669298, SD=0.273801, IQR=0.478028, 95% CI=[0.569732, 0.814249]
- Hard failures: 0
- Hard-constraint violations: 5 (rate=0.032258)
- Budget exceeded: false
- Token coverage: 1.0
- Cost coverage: 1.0
- Baseline verdict: **notCompared**

## Per-case primary metric

| caseId | N | mean | median | SD | IQR |
|---|---:|---:|---:|---:|---:|
| buy-cmap-0001 | 5 | 0.735411 | 0.728353 | 0.015783 | 0.000000 |
| buy-cmap-0003 | 5 | 0.453001 | 0.448309 | 0.102695 | 0.147343 |
| buy-cmap-0004 | 5 | 0.866467 | 0.877215 | 0.014718 | 0.026870 |
| buy-cmap-0006 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0002 | 5 | 0.705556 | 0.671386 | 0.074877 | 0.021798 |
| buy-gust-0002 | 5 | 0.630006 | 0.630006 | 0.000000 | 0.000000 |
| buy-mult-0001 | 5 | 0.521972 | 0.579969 | 0.079416 | 0.144992 |
| buy-over-0001 | 5 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| buy-over-0002 | 5 | 0.402329 | 0.351043 | 0.098223 | 0.187892 |
| buy-over-0003 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-pers-0001 | 5 | 0.497528 | 0.609477 | 0.236080 | 0.360344 |
| buy-pers-0002 | 5 | 0.542452 | 0.540460 | 0.203943 | 0.187892 |
| buy-repu-0001 | 5 | 0.630930 | 0.630930 | 0.000000 | 0.000000 |
| buy-repu-0002 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-srch-0002 | 5 | 0.633041 | 0.665630 | 0.201795 | 0.159033 |
| buy-srch-0005 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-srch-0007 | 5 | 0.910812 | 0.906025 | 0.026683 | 0.053098 |

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

- `overall.filterAccuracy`: mean=0.248333
- `overall.hardConstraintViolationRate`: mean=0.055556
- `overall.mrr`: mean=0.816667
- `overall.precisionAtK.10`: mean=0.207778
- `overall.recallAtK.10`: mean=0.731852

Secondary metrics and slices are exploratory; only the configured primary metric is confirmatory.
