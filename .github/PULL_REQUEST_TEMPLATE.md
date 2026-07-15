## 변경 요약

<!-- 무엇을 왜 바꿨는지 1~3줄 -->

## 관련

<!-- SPEC / api-spec § / mvp-todo 주제 / 이슈 번호 -->

## 체크리스트

- [ ] `uv run pytest` 통과 (CI 자동 확인)
- [ ] `uv run ruff check` 통과 (CI 자동 확인)
- [ ] 기능/주제 완료 시 **CHANGELOG.md** `[Unreleased]` 갱신
- [ ] 계약(엔드포인트·SSE·필드·오류) 변경 시 **api-spec 사본**(`docs/api-spec.md`) 동기화 — 정본 개정이 선행됐는지 확인
- [ ] 개발 중 밟은 실수는 **docs/lessons.md**에 기록
- [ ] 신원은 JWT `sub`에서만 도출 (요청 본문 신뢰 금지) · productId는 string

## 리뷰 노트

<!-- 리뷰어가 집중해서 볼 부분, 스크린샷/로그 등 -->
