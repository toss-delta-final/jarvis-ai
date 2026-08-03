# Actual-model evaluation report

- Dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- Cases × repeats: 31 × 5
- Primary metric: `overall.ndcgAtK.10`
- Primary summary: N=18, mean=0.616852, median=0.570173, SD=0.289322, IQR=0.322458, 95% CI=[0.489836, 0.740183]
- Hard failures: 0
- Hard-constraint violations: 5 (rate=0.032258)
- Budget exceeded: false
- Token coverage: None
- Cost coverage: None
- Baseline verdict: **notCompared**

## Per-case primary metric

| caseId | N | mean | median | SD | IQR |
|---|---:|---:|---:|---:|---:|
| buy-cmap-0001 | 5 | 0.520742 | 0.520742 | 0.000000 | 0.000000 |
| buy-cmap-0003 | 5 | 0.589536 | 0.589536 | 0.000000 | 0.000000 |
| buy-cmap-0004 | 5 | 0.831555 | 0.831555 | 0.000000 | 0.000000 |
| buy-cmap-0006 | 5 | 0.982892 | 0.982892 | 0.000000 | 0.000000 |
| buy-cold-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0002 | 5 | 0.550810 | 0.550810 | 0.000000 | 0.000000 |
| buy-gust-0002 | 5 | 0.210002 | 0.210002 | 0.000000 | 0.000000 |
| buy-mult-0001 | 5 | 0.472944 | 0.472944 | 0.000000 | 0.000000 |
| buy-over-0001 | 5 | 0.703918 | 0.703918 | 0.000000 | 0.000000 |
| buy-over-0002 | 5 | 0.536343 | 0.536343 | 0.000000 | 0.000000 |
| buy-over-0003 | 5 | 0.732829 | 0.732829 | 0.000000 | 0.000000 |
| buy-pers-0001 | 5 | 0.800017 | 0.800017 | 0.000000 | 0.000000 |
| buy-pers-0002 | 5 | 0.509097 | 0.509097 | 0.000000 | 0.000000 |
| buy-repu-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-repu-0002 | 5 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| buy-srch-0002 | 5 | 0.509097 | 0.509097 | 0.000000 | 0.000000 |
| buy-srch-0005 | 5 | 0.951443 | 0.951443 | 0.000000 | 0.000000 |
| buy-srch-0007 | 5 | 0.202107 | 0.202107 | 0.000000 | 0.000000 |

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

- `overall.filterAccuracy`: mean=1.000000
- `overall.hardConstraintViolationRate`: mean=0.055556
- `overall.mrr`: mean=0.791667
- `overall.precisionAtK.10`: mean=0.222222
- `overall.recallAtK.10`: mean=0.705556

Secondary metrics and slices are exploratory; only the configured primary metric is confirmatory.
