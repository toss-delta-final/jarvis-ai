# dev-v2 paired baseline

- command: `uv run python -m evals.scoring --out evals/scoring/baselines/dev-v2`
- dataset hash: `904f90e93a1dbff797c7e8bc48f2a795f006d1e6b5405e753207c76adb8de273`
  (v2.1.0, adjudication 반영본 — #333 Part 3)
- ranking cases: 62/103 (nonDiscriminativeRanking·emptyRelevance·notMft 제외, #143 계약)
- embedding coverage: documents **1510/1517**, queries 103/103

## 임베딩 결측 7건 (실측, 데이터 변경 없음)

`snapshot_embeddings.py` 실행 시 dev fixture가 참조하는 injected 후보 1517개 중 7개가
live pg-catalog의 `products`/`product_document` 양쪽에 모두 없었다(정적
`catalog_snapshot.json` 스냅샷엔 있으나 이후 카탈로그 이탈로 live DB에서 빠진 것으로
추정). 오케스트레이터 확인 후 결측 7건은 재구성 없이 진행했다(재구성은 재검수 연쇄를
부른다):

| productId | 등장 caseId | source |
|---:|---|---|
| 2137780125 | buy-invw-0003, buy-invw-0004 | injected |
| 2803258637 | buy-pers-0001 | injected |
| 2848444180 | buy-budg-0001, buy-budg-0003 | injected |
| 3294263680 | buy-mult-0005 | injected |
| 3674644033 | buy-pers-0001 | injected |
| 3709020096 | buy-budg-0003, buy-mult-0009, buy-pers-0001 | injected |
| 8864611198 | buy-over-0001 | injected |

전부 `source=injected` 하드 네거티브이며 어떤 케이스에서도 `relevant=True`가 아니다(정답
아님). `scores_scoring.json` 실측 결과 11회 등장 전부 `semantic.degraded=true,
reason=missingEmbedding, value=0.0`으로 기록됐고, 그 결과 해당 후보는 각 케이스에서
후보 30건 중 28~30위(최하위권)로 밀려났다. **순위 품질 지표(nDCG/Recall/Precision)에 준
영향은 무시 가능한 수준이다** — 비정답 7/1,517의 semantic 성분만 0으로 강등되며, 이는
해당 비정답의 순위를 낮추는 방향이라 scoring arm에 불리하지 않다("영향 없음"이 아니라
방향이 결과를 왜곡하지 않는다는 뜻이다). `embed_texts`가 Google 배치 상한(100건/요청)을
넘는 호출에서 에러를 내는 결함은 `app/pipelines/embedding.py`(app 소관, 이 이슈 범위
밖)에서 발견·보고했고, 이 eval 경로는 `evals/scoring/snapshot_embeddings.py` 호출부
청크 분할로 대응해 나머지 1510건을 정상 재생성했다.

## 결과

| metric | passthrough | scoring | delta |
|---|---:|---:|---:|
| nDCG@10 | 0.325368 | 0.440818 | +0.115451 |
| MRR | 0.441615 | 0.681336 | +0.239721 |
| Precision@10 | 0.211290 | 0.225806 | +0.014516 |
| Recall@10 | 0.392275 | 0.460210 | +0.067935 |
| Diversity | 0.678533 | 0.861920 | +0.183387 |

dev-v1(위 delta 전부 음수, `passthrough` 우위)과 부호가 반대다 — **다른 datasetHash라
직접 비교하지 않는다**. v1은 후보 32건이 우연히 productId 오름차순으로 기록돼 `passthrough`
가 사실상 no-op에 가까웠고(README 상단 "해석 정정(#333)" 참조), v2.1은 후보 구성·주입
하드 네거티브·라벨이 모두 다시 만들어져 같은 부호 비교의 근거가 없다. 이 delta 반전은
`scoring` baseline의 우열 주장이 아니라 서로 다른 데이터셋의 독립 실측치다.

`latency.json`과 각 manifest의 `run` 섹션은 실행 인스턴스 정보이며 byte-identical 비교에서
제외한다.
