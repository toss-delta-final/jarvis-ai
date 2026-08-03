# Actual-model evaluation report

- Dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- Cases × repeats: 31 × 5
- Primary metric: `overall.ndcgAtK.10`
- Primary summary: N=18, mean=0.782943, median=0.776763, SD=0.213547, IQR=0.395053, 95% CI=[0.684728, 0.878763]
- Hard failures: 0
- Hard-constraint violations: 0 (rate=0.000000)
- Budget exceeded: false
- Token coverage: 1.0
- Cost coverage: 1.0
- Baseline verdict: **notCompared**

## Per-case primary metric

| caseId | N | mean | median | SD | IQR |
|---|---:|---:|---:|---:|---:|
| buy-cmap-0001 | 5 | 0.499214 | 0.540460 | 0.209040 | 0.084849 |
| buy-cmap-0003 | 5 | 0.550664 | 0.491878 | 0.228168 | 0.342696 |
| buy-cmap-0004 | 5 | 0.898933 | 0.831555 | 0.092261 | 0.168445 |
| buy-cmap-0006 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0002 | 5 | 0.692299 | 0.665350 | 0.084910 | 0.042363 |
| buy-gust-0002 | 5 | 0.748343 | 0.840008 | 0.144203 | 0.128762 |
| buy-mult-0001 | 5 | 0.604947 | 0.579969 | 0.055852 | 0.000000 |
| buy-over-0001 | 5 | 0.495385 | 0.593173 | 0.133902 | 0.244471 |
| buy-over-0002 | 5 | 0.371518 | 0.509097 | 0.276371 | 0.339398 |
| buy-over-0003 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-pers-0001 | 5 | 0.754111 | 0.754111 | 0.000000 | 0.000000 |
| buy-pers-0002 | 5 | 0.690441 | 0.690441 | 0.000000 | 0.000000 |
| buy-repu-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-repu-0002 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-srch-0002 | 5 | 0.799414 | 0.848495 | 0.111783 | 0.214136 |
| buy-srch-0005 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-srch-0007 | 5 | 0.987711 | 1.000000 | 0.027478 | 0.000000 |

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

- `overall.filterAccuracy`: mean=0.063519
- `overall.hardConstraintViolationRate`: mean=0.000000
- `overall.mrr`: mean=0.877791
- `overall.precisionAtK.10`: mean=0.240000
- `overall.recallAtK.10`: mean=0.824444

Secondary metrics and slices are exploratory; only the configured primary metric is confirmatory.
