---
name: implement-topic
description: Implement one MVP topic end-to-end in this repo (jarvis-ai AI server), contract-first. Use when starting or continuing an mvp-todo topic — recommendation graph, cart (I-2/I-9), seller (I-6/I-7), profile, AI-artifacts batch (I-8), SSE lifecycle. Reads api-spec § + SPEC, wires the stub with tests, runs ruff+pytest, commits.
argument-hint: "[topic-or-issue]"
---

MVP 주제 하나를 계약 우선으로 구현한다. 대상: $ARGUMENTS

이 저장소는 계약 우선이다. 계약 단계를 건너뛰지 마라.

## 1. 범위 확인
- `docs/mvp-todo.md`에서 해당 주제와 체크리스트, `docs/mvp-plan.md`에서 구현 방식·관련 파일을 찾는다.
- 이슈 번호가 주어졌으면 이슈를 읽는다. 바꿀 스텁/파일을 확정한다.

## 2. 계약 읽기 (코드 작성 전)
- `docs/api-spec.md` — 스텁 docstring이 참조하는 § (엔드포인트·SSE 이벤트·필드·오류 코드).
- `docs/specs/SPEC-*.md` — 노드 내부 로직(분기·계산·게이트).
- 주제가 🔴 Spring 계약(C-15/C-6/C-3/C-13/C-14/C-4 등) 미확정에 의존하면 **멈추고 보고** — 계약을 지어내지 말고 스텁을 § 참조와 함께 유지.
- `docs/lessons.md`를 훑는다.

## 3. 구현 (TDD, 계약 형태 준수)
- 계약에서 테스트를 먼저 쓴다 — 와이어는 camelCase(`CamelModel`), `productId`는 string, 신원은 JWT `sub`에서만(요청 본문 금지).
- 스텁을 채운다. 계획의 **degrade 규칙**을 지킨다(Spring/LLM 실패가 사용자 흐름을 막지 않게).
- 라이브러리 API(LangGraph·FastAPI·Pydantic·httpx·PyJWT 등)가 불확실하면 추측하지 말고 **context7 MCP**로 최신 공식 문서를 확인한다(버전·시그니처·패턴). 학습 데이터 시점 이후 변경 반영.
- 튜너블은 `app/core/config.py` 주입. 주석 한국어, 식별자 영어.
- 계약(스키마·엔드포인트·SSE·오류)은 **명세 개정 없이 바꾸지 않는다**.

## 4. 검증
- `uv run ruff check --fix && uv run ruff format`
- `uv run pytest` — 통과해야 함. 초록 결과 없이 "완료" 보고 금지.

## 5. 커밋 · 기록
- `git diff` 검토(시크릿·디버그·잔여 스텁 없나).
- diff 근거로 Conventional Commit `<type>(<scope>): <subject>`. 관련 파일만 스테이징(한 커밋 = 한 논리 단위).
- `CHANGELOG.md` `[Unreleased]` 갱신. 계약 변경 시 `docs/api-spec.md` 사본 동기화를 선행/동반.
- 밟은 실수는 `docs/lessons.md`에 기록. PR 본문에 `Closes #이슈번호`.

계약이 모호하거나 Spring API가 미확정이면 추측하지 말고 드러내서 물어라.
