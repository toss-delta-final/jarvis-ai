# dev-v2.2 paired baseline

- command: `uv run python -m evals.scoring --out evals/scoring/baselines/dev-v2.2`
- dataset hash: `ef3a5af8b303041d9f44c156d687e3572feed33d2e85469dce1e0aa49a7ecf37`
  (v2.2.0, 위반 네거티브 채널·라벨 provenance 반영본 — #370)
- ranking cases: 62/103 (nonDiscriminativeRanking·emptyRelevance·notMft 제외, #143 계약,
  dev-v2와 동일 — 이 이슈는 라벨을 바꾸지 않았다)
- embedding coverage: documents **1519/1526**, queries 103/103

## dev-v2 대비 변경분(#370) — 랭킹 품질 지표 5종은 동일, coverage·candidateDepth는 다르다

이 baseline은 v2.1.0(`dev-v2`) 위에 위반 네거티브 채널(가격 초과 13케이스·47후보 injected,
카테고리 이탈 4케이스·5후보 retagged — `evals/goldenset/CHANGELOG.md` 2026-08-06 항목)과
라벨 provenance 필드(점수에 영향 없음)만 얹은 datasetHash다. `relevantProductIds` 등 라벨
값은 바뀌지 않았다.

**정정(#370 리뷰 라운드2 F-3)**: 아래 "결과" 표의 랭킹 품질 지표 5종(nDCG@10·MRR·
Precision@10·Recall@10·Diversity)은 `passthrough`·`scoring` 두 arm 전부 dev-v2와 소수점까지
동일하다. 다만 `comparison.json`을 전수 대조하면 이 5종 **밖의** 필드는 dev-v2와 다르다 —
"결과는 소수점까지 완전히 동일하다"는 표현은 부정확했다:

| 필드 | dev-v2 | dev-v2.2 |
|---|---:|---:|
| `scoring.coverage` | 0.3348714568226763 | **0.3328964613368283** |
| `passthrough.coverage` | 0.3091628213579433 | **0.3073394495412844** |
| `delta.coverage` | 0.02570863546473301 | **0.025557011795543927** |
| `scoring.candidateDepth.max` | 30 | **34** |
| `passthrough.candidateDepth.max` | 30 | **34** |

원인은 `evals.metrics.metrics.catalog_coverage`의 분모가 "search fixture 후보 합집합"이기
때문이다 — 이번 이슈가 후보를 47건(고유 41개) 늘리면 노출(분자)이 그대로여도 coverage는
떨어진다. `candidateDepth.max`도 후보 풀 크기지 노출 품질이 아니다(가격 축 주입으로 일부
케이스의 fixture 후보가 30건에서 최대 34건까지 늘었다). 둘 다 **랭킹 품질 지표가 아니라
후보 풀 통계**이므로 "품질이 바뀌었다"는 뜻이 아니다 — 데이터를 조작해 이 차이를 없애지
않았다. 별개로 `comparison.json`에 `filterAxes` 블록이 새로 생겼는데, 이는 #370이 아니라
#334(필터 추출 축별 분해 지표) 배선이 그사이 dev 브랜치에 머지된 결과다.

**랭킹 품질 지표가 동일한 이유**: 두 arm 모두 위반 네거티브 후보가 실제 노출 집합에 도달하기
**전에** 걸러지도록 구성적으로 보장된다:

- **`scoring` arm**: `evals/scoring/hard_filter.py`가 케이스의 `priceMax`/`priceMin`을 그대로
  적용해 신규 주입 `price_violation` 후보를 스코어링 전에 전부 컷한다.
- **`passthrough` arm**: `passthrough`는 `evals/metrics/harness.py`의 `OfflineBuyerAdapter`/
  `_CaseTransport`(실 app 파이프라인 + fake Spring)를 그대로 쓴다(`evals/scoring/adapter.py`
  참조) — no-op 정의(`evals/goldenset/README.md`)가 "시스템이 실제로 노출한 상품 집합을
  productId 오름차순으로 재정렬"이므로, `_CaseTransport`가 요청의 `minPrice`/`maxPrice`로
  가격을 거르도록 고친 #370 결정 01(`evals/goldenset/GUIDE.md` "결정 01" 절)이 이 arm에도
  똑같이 적용돼 injected 후보가 애초에 "시스템이 노출한 집합"에 들어가지 않는다.
- `category_violation`은 새 candidate를 추가하지 않고 기존 candidate의 `rule` 필드만
  재태깅했으므로 애초에 후보 집합 자체가 안 바뀌었다(재태깅이 `from`까지 건드리던 결함은
  #370 리뷰 라운드2 F-1로 수정 — `evals/goldenset/GUIDE.md`·`CHANGELOG.md` 참조. 이 필드는
  점수 계산에 쓰이지 않아 F-1 수정 전후로 이 baseline의 수치는 무관하다).

**채널 실측(#370 결정 01 요건 3)**: `scores_scoring.json`의 `cuts`를 집계하면 주입한
price_violation 후보 47건(고유 productId 41개, 일부는 여러 케이스에 등장) 전부가
`reason="priceMax"`로 컷됐다(47/47, 컷 누락 0건) — 이 채널이 결정론 CI 실행(`evals.scoring`)
에서도 실제로 발화함을 실측으로 확인한다. `evals.metrics`의 critical PR 게이트(harness mock
가격 필터)와는 서로 다른 계층의 독립 확인이다 — 둘 다 같은 harness 코드를 공유하지만
(`evals.scoring.adapter`가 `evals.metrics.harness`를 그대로 import), 전자는 `hard_filter.py`
(scoring arm 전용 후처리)로, 후자는 mock 자체(모든 harness 소비자가 공유)로 각각 독립적으로
막는다.

## 임베딩 결측 7건 (dev-v2와 동일, 데이터 변경 없음)

`snapshot_embeddings.py` 재실행 결과 dev fixture가 참조하는 productId 1526개(dev-v2의 1517
대비 +9 — 가격 축 주입이 늘린 신규 고유 productId) 중 여전히 같은 7개가
live pg-catalog에 없다(dev-v2 README와 동일 목록·동일 사유 — 카탈로그 이탈로 추정, 이번
주입분 중 이 7건에 해당하는 productId는 없다):

| productId | 등장 caseId | source |
|---:|---|---|
| 2137780125 | buy-invw-0003, buy-invw-0004 | injected |
| 2803258637 | buy-pers-0001 | injected |
| 2848444180 | buy-budg-0001, buy-budg-0003 | injected |
| 3294263680 | buy-mult-0005 | injected |
| 3674644033 | buy-pers-0001 | injected |
| 3709020096 | buy-budg-0003, buy-mult-0009, buy-pers-0001 | injected |
| 8864611198 | buy-over-0001 | injected |

영향 판단은 dev-v2와 동일(순위 품질 지표에 준 영향 무시 가능 — 비정답 후보의 semantic 성분만
0으로 강등, scoring arm에 불리하지 않은 방향).

## 결과

| metric | passthrough | scoring | delta |
|---|---:|---:|---:|
| nDCG@10 | 0.325368 | 0.440818 | +0.115451 |
| MRR | 0.441615 | 0.681336 | +0.239721 |
| Precision@10 | 0.211290 | 0.225806 | +0.014516 |
| Recall@10 | 0.392275 | 0.460210 | +0.067935 |
| Diversity | 0.678533 | 0.861920 | +0.183387 |

**다른 datasetHash라 dev-v2와 직접 비교하지 않는다** — 위 5개 지표는 실측 결과 dev-v2와
동일하지만, `coverage`·`candidateDepth.max` 등 후보 풀 통계는 다르다(위 "dev-v2 대비
변경분" 절 참조) — 이 규약을 우회하는 근거로 쓰지 않는다.
`evals/ablation/baselines/20260805-dev-v2-full-n5`(실 LLM n5)는 v2.1.0 해시 고정 참조로
남기고 이번 이슈에서 재실행하지 않는다(오케스트레이터 결정 대기, goldenset README/CHANGELOG
참조).

`latency.json`과 각 manifest의 `run` 섹션은 실행 인스턴스 정보이며 byte-identical 비교에서
제외한다.
