# 변경 요약

#474 색상 고유어/정본 표기 MFT 6건과 I-1 색상 배열 mock 충실도, 승인 사전 기반 on/off A/B 측정을 추가한다.

| 케이스 | recall@10 off→on | nDCG@10 off→on | 노출(축없음/일치/불일치) |
|---|---:|---:|---|
| buy-colr-0001/3/5 | 0→1 | 0→0.445734 | 6/0/0 → 6/3/0 |
| buy-colr-0002/4/6 | 1→1 | 0.445734→0.445734 | 6/3/0 → 6/3/0 |

| baseline | 처리 | 사유 |
|---|---|---|
| audit leakage report | 재실행 | dataset 2.3.0 감사 |
| filter_axes trivial_empty | 재실행 | 지정된 결정론 baseline |
| scoring dev-v2.3 | 신규 sibling | v2.2와 직접 비교 금지; 신규 케이스 임베딩 결측은 degrade |
| ablation/personalization/model_eval | 기존 hash 고정 | 실 LLM baseline, 이번 범위 밖 |

STEP-1: 기존 color 15건 노출 9→9, 정답 탈락 0건, behaviorChecks 18/18→18/18, overall nDCG@10 0.766540→0.766540.

## 관련

Closes #474

## 체크리스트

- [ ] `uv run pytest` 통과 (커밋 전 집계 원문 첨부 필요)
- [x] `uv run ruff check` 통과
- [x] CHANGELOG 갱신
- [x] 계약 문서 무변경

## 리뷰 노트

- labelSource는 `model`, 신규 6건 adjudicator는 독립 검수 부재로 비어 있다.
- dev-v2.2와 2.3.0 점수는 직접 비교하지 않는다. 실 LLM/임베딩 baseline은 외부 의존성 때문에 재실행하지 않았다.
- 신규 쌍은 off 팔에서 의도적으로 달라져 INV 그룹에 등록하지 않았다.
