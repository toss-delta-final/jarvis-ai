# dev-v1 paired baseline

- command: `uv run python -m evals.scoring --out evals/scoring/baselines/dev-v1`
- dataset hash: `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- ranking cases: 18/31 (비판별 케이스 등 13건은 #143 계약에 따라 제외)
- embedding coverage: documents 232/232, queries 31/31

| metric | passthrough | scoring | delta |
|---|---:|---:|---:|
| nDCG@10 | 0.738210 | 0.616852 | -0.121358 |
| MRR | 0.794974 | 0.791667 | -0.003307 |
| Precision@10 | 0.266667 | 0.222222 | -0.044444 |
| Recall@10 | 0.855556 | 0.705556 | -0.150000 |
| Diversity | 0.659140 | 0.709319 | +0.050179 |

`latency.json`과 각 manifest의 `run` 섹션은 실행 인스턴스 정보이며 byte-identical 비교에서 제외한다.
나머지 artifact는 다른 출력 경로와 다른 OS 평가 환경변수로 재실행해 정규화 결과가 같은지 검사한다.
