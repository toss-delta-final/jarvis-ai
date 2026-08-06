# 설명 가능한 추천 scoring baseline

이 패키지는 LLM 최종 판단과 독립인 평가·비교용 참조 랭커다. 네트워크나 현재 시각을 읽지 않고,
커밋된 dev 임베딩과 골든셋 fixture, 주입된 설정만으로 동일 입력·snapshot의 동일 순위를 만든다.
실서비스의 rerank LLM 경로와 배선은 바꾸지 않는다.

## 점수

기본 점수는 다음 성분의 가중합이며, 각 상품에 정규화 값·가중치·기여분·degrade 사유를 기록한다.

| 성분 | 기본 가중치 | 정의 |
|---|---:|---|
| semantic | 0.55 | query/document 코사인을 `[0,1]`로 선형 변환 |
| profile match | 0.15 | 구매 카테고리 선호와, 카탈로그에서 복원 가능한 구매 브랜드 선호의 평균 |
| popularity | 0.15 | 후보 내 `log1p(reviewCount)`와 `rating/5`의 평균 |
| recency | 0.05 | 외부 `productId → [0,1]` 입력; 기본 실행은 미주입 |
| diversity bonus | 0.10 | greedy 선택 중 아직 노출하지 않은 `categoryName` |
| recent purchase penalty | 0.20 | 기준일 2026-08-02부터 90일 내 exact `productId` |

semantic을 주 신호로 두고 profile·인기도·최신성·다양성은 보조 신호로 제한했다. recent purchase는
별도 감점이며 카테고리 전체로 확대하지 않는다. 최종 동점은 항상 `productId` 오름차순이다.

임베딩 누락, guest/구매 이력 없음, recency 미주입은 각각 값을 0으로 두고 degrade를 기록한다.
특히 guest를 가짜 중립 프로필로 꾸미지 않는다. 구매 이력에는 브랜드가 없으므로 해당 구매 상품이
카탈로그 snapshot에도 있을 때만 브랜드를 복원한다.

## hard filter

`hard_filter.py`는 스코어러와 별도 시그니처로 가격·금지 카테고리·금지 상품·must-exclude를 컷한다.
profile은 입력받지 않으며, 컷된 상품은 높은 점수로 재진입할 수 없다. headline paired run에는
평가 라벨인 `hardConstraints`/`mustExcludeProductIds`를 주입하지 않고, 양 arm이 공통으로 쓰는
scripted decompose의 `expectedFilters` 중 price 범위만 사용한다. 나머지 축은 단위 테스트가
계약을 고정한다.

## 실행

```bash
uv run python -m evals.scoring --out /tmp/scoring-paired
```

`passthrough/`와 `scoring/`에 #143 artifact를 각각 만들고, `comparison.json/.md`,
`scores_scoring.json`, paired `run_manifest.json`을 기록한다. 벽시계 latency는 결정론 대상과
분리한 `latency.json`에만 둔다. 양 arm의 앱 최근구매 dedup clock은 절대 날짜 fixture와 같은
`scoring_reference_date` 자정(naive UTC)으로 고정한다. `snapshot_embeddings.py`는 fixture
갱신 시에만 DB와 Google API를 사용하며, 일반 평가 경로는
`fixtures/embeddings_dev.json`만 읽는다.

### 최근 구매 penalty의 paired 관측 한계

공정 비교를 위해 paired 실행은 양 arm 모두 실제 앱 경로의 최근구매 dedup을 그대로 탄다. 기본
dedup window와 scoring penalty window가 모두 90일이므로 exact 최근구매 상품은 rerank 전에
제거되고, paired 지표에서 penalty 성분의 효과는 구조적으로 제한된다. 이 가중치는 참조 스코어러
단독 사용과 후속 #146 ablation에서 의미가 있으며, 효과를 만들기 위해 window를 바꾸거나 앱 경로를
우회하지 않는다.

따라서 `scores_scoring.json`은 앱 파이프라인 dedup 이전 후보 전체에 대한 참조 점수다. 최종 앱
ranking은 그 후보의 부분집합일 수 있으며, raw score 파일을 그대로 최종 노출 목록으로 읽으면 안
된다.

현재 비교 기준선은 [`baselines/dev-v2.2/`](baselines/dev-v2.2/)이다(#370, 골든셋
v2.2.0/위반 네거티브 채널·라벨 provenance 반영본). dataset hash는
`ef3a5af8b303041d9f44c156d687e3572feed33d2e85469dce1e0aa49a7ecf37`이며, dev 후보
1519/1526(injected 7건은 live 카탈로그 이탈로 결측 — dev-v2와 동일 목록, 상세는
`baselines/dev-v2.2/README.md`)과 질의 103/103의 `gemini-embedding-001` 1536차원 벡터를
포함한다. [`baselines/dev-v2/`](baselines/dev-v2/)(v2.1.0, #333 Part 3)와
[`baselines/dev-v1/`](baselines/dev-v1/)(v1, 43건)은 이력으로만 남기며 **다른 datasetHash와
직접 비교하지 않는다**.

| metric | passthrough | scoring | delta |
|---|---:|---:|---:|
| nDCG@10 | 0.325368 | 0.440818 | +0.115451 |
| MRR | 0.441615 | 0.681336 | +0.239721 |
| Precision@10 | 0.211290 | 0.225806 | +0.014516 |
| Recall@10 | 0.392275 | 0.460210 | +0.067935 |
| Diversity | 0.678533 | 0.861920 | +0.183387 |

이 결과는 baseline의 우월성 주장이 아니라 후속 #146 ablation과 튜닝이 비교할 고정 출발점이다.
`passthrough`·`scoring` 두 arm 전부 위 5개 랭킹 품질 지표는 dev-v2와 소수점까지 동일하다 —
`scoring` arm은 hard_filter가, `passthrough` arm은 #370 결정 01로 고친
`evals/metrics/harness.py` mock 가격 필터가 각각 신규 위반 네거티브를 노출 전에 컷하기
때문이다. **다만 `coverage`·`candidateDepth.max`(후보 풀 통계, 랭킹 품질 지표 아님)는
후보 풀이 47건 늘어난 만큼 dev-v2와 다르다** — 정확한 수치와 원인은
`baselines/dev-v2.2/README.md`(#370 리뷰 라운드2 F-3 정정) 참조.

**해석 정정(#333)**: 위 `passthrough`는 검색엔진이 매긴 순위 기준선이 **아니다** — 앱이 실제로
노출한 상품 집합을 그대로 쓰되 순서만 임의(productId 오름차순)로 두는 **no-op 기준선**이다
(`evals/goldenset` README·`evals/metrics.NOOP_BASELINE_DEFINITION`의 정의와 동일). v1
dev-v1은 후보 32/32건이 우연히 productId 오름차순으로 기록돼 있어 `passthrough`가 사실상
이 no-op과 동치였을 뿐이고, v2.1(`dev-v2`)부터는 후보 구성이 달라 이 우연이 반복되지 않았다
(위 표에서 `passthrough`가 `scoring`보다 낮게 나온 이유). 두 버전 모두 `passthrough`의 정의
자체는 "no-op 기준선"으로 동일하게 읽는다. `evals/metrics`는 v2부터 이 기준선을
`noopBaseline`으로 모든 실행에 상설 등록한다.
