# 📚 Jarvis AI 서버 — 개발 문서

MVP 개발을 위한 실행 문서 모음. 계약 정본(api-spec)은 기획 저장소에 있고, 여기서는 **이 저장소가 무엇을 어떻게 만들지**를 다룬다.

| 문서 | 내용 |
|---|---|
| [mvp-plan.md](mvp-plan.md) | 주제별 구현 계획 — 목표·접근 방식·관련 파일·계약 참조·주의점 |
| [mvp-todo.md](mvp-todo.md) | 주제별 TODO 체크리스트 + 착수 우선순위 + Spring 협의 의존성 |
| [roadmap.md](roadmap.md) | MVP 이후 — 확정된 확장 사항 + 아직 열려 있는(OPEN) 결정들 |
| [lessons.md](lessons.md) | **실수 기록** — 재발 방지 러닝 로그 (작업 전 훑기, 오류 시 추가) |
| [api-spec.md](api-spec.md) | **API 계약 명세 v0.7.0** — 정본(기획 저장소)의 동기화 사본 |
| [specs/](specs/) | **소유 SPEC 사본** — RECOMMEND-001 · PROFILE-001 · CATALOG-DATA-001 (그래프 로직 상세) |

## 원칙

- **계약 우선**: 엔드포인트·SSE 이벤트·필드·오류 코드의 정본은 `api-spec v0.7.0`(기획 저장소 `docs/api-spec.md`). 코드에서 임의 변경 금지, 명세 개정이 먼저.
- **§ 참조 추적성**: 각 스텁 주석에 대응 `api-spec §` 번호를 유지 — 코드↔명세 링크.
- **degrade 우선**: 외부(Spring/LLM) 실패가 사용자 흐름을 막지 않도록 각 주제에 degrade 규칙을 둔다.

> 상태 범례: ✅ 완료 · 🚧 구현 중 · 📋 예정 · 🔴 Spring 협의 대기 · ❓ OPEN(미결)
