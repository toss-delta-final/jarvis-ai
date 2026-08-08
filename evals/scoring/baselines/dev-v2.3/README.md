# dev-v2.3 paired baseline

- command: `uv run python -m evals.scoring --out evals/scoring/baselines/dev-v2.3`
- dataset hash: `675520d999dc1fbf0a4b32e13914205bc61c606c9adc2f65833eb67fc133ae50` (v2.3.0)
- ranking cases: 68/109

## dev-v2.2와의 관계

v2.2 hash `ef3a5af8b303041d9f44c156d687e3572feed33d2e85469dce1e0aa49a7ecf37`와
**직접 비교하지 않는다**. 색상 mock 충실도와 신규 6건으로 case/ranking 분모가 103/62에서
109/68로 바뀌었기 때문이다. passthrough nDCG@10은 0.325368→0.315236, scoring nDCG@10은
0.440818→0.422530으로 움직였고, 이는 품질 회귀 판정이 아니라 새 색상 케이스와 mock 필터가
포함된 다른 datasetHash의 기준선이다.

## 임베딩 결측

`scores_scoring.json` 실측에서 semantic `missingEmbedding`은 191 score 항목, 14 case에 있다.
신규 6건은 `embeddings_dev.json`에 질의 임베딩이 없어 semantic 성분이 전부 degrade된 값이며,
따라서 이 baseline의 신규 케이스 점수는 semantic 성분 없이 읽어야 한다. 임베딩 재생성은 DB와
Google API가 필요하므로 이번 오프라인 결정론 이슈 범위에서 실행하지 않았다.
