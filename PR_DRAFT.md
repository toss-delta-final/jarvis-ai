# 변경 요약

#474 색상 고유어/정본 표기 MFT 6건과 I-1 색상 배열 mock 충실도, 승인 사전 기반 on/off A/B 측정을 추가한다.

## 관련

Closes #474

## 체크리스트

- [x] `uv run pytest` 통과
- [x] `uv run ruff check` 통과
- [x] CHANGELOG 갱신
- [x] 계약 문서 무변경

## 리뷰 노트

- labelSource는 `model`, 신규 6건 adjudicator는 독립 검수 부재로 비어 있다.
- dev-v2.2와 2.3.0 점수는 직접 비교하지 않는다. 실 LLM/임베딩 baseline은 외부 의존성 때문에 재실행하지 않았다.
- 신규 쌍은 off 팔에서 의도적으로 달라져 INV 그룹에 등록하지 않았다.
