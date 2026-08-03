# Actual-model evaluation report

- Dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- Cases × repeats: 31 × 5
- Primary metric: `overall.ndcgAtK.10`
- Primary summary: N=18, mean=0.785560, median=0.832764, SD=0.229230, IQR=0.398845, 95% CI=[0.678745, 0.883255]
- Hard failures: 1
- Hard-constraint violations: 0 (rate=0.000000)
- Budget exceeded: false
- Token coverage: 1.0
- Cost coverage: 0.5344827586206896
- Baseline verdict: **notCompared**

## Warnings

- quality lower bound not calibrated

## Per-case primary metric

| caseId | N | mean | median | SD | IQR |
|---|---:|---:|---:|---:|---:|
| buy-cmap-0001 | 5 | 0.485083 | 0.490903 | 0.051422 | 0.011645 |
| buy-cmap-0003 | 5 | 0.601155 | 0.614745 | 0.065257 | 0.009198 |
| buy-cmap-0004 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cmap-0006 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-cold-0002 | 5 | 0.567447 | 0.530721 | 0.081406 | 0.146595 |
| buy-gust-0002 | 5 | 0.736998 | 0.711246 | 0.057584 | 0.000000 |
| buy-mult-0001 | 5 | 0.670479 | 0.692151 | 0.095233 | 0.112181 |
| buy-over-0001 | 5 | 0.355904 | 0.593173 | 0.324894 | 0.593173 |
| buy-over-0002 | 5 | 0.407277 | 0.509097 | 0.227675 | 0.000000 |
| buy-over-0003 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-pers-0001 | 5 | 0.754111 | 0.754111 | 0.000000 | 0.000000 |
| buy-pers-0002 | 5 | 0.690441 | 0.690441 | 0.000000 | 0.000000 |
| buy-repu-0001 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-repu-0002 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-srch-0002 | 5 | 0.911417 | 0.904577 | 0.056326 | 0.036343 |
| buy-srch-0005 | 5 | 1.000000 | 1.000000 | 0.000000 | 0.000000 |
| buy-srch-0007 | 5 | 0.959772 | 1.000000 | 0.055963 | 0.086598 |

## Hard failures

- buy-fail-0002: nonRecommendIntent:general (repeat=0)

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

- `overall.filterAccuracy`: mean=0.036085
- `overall.hardConstraintViolationRate`: mean=0.000000
- `overall.mrr`: mean=0.862037
- `overall.recallAtK.10`: mean=0.823704

Secondary metrics and slices are exploratory; only the configured primary metric is confirmatory.
