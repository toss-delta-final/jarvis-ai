# 소유 SPEC (동기화 사본)

그래프 노드 내부 로직의 상세 규칙. **정본은 기획 저장소 `.moai/specs/`**, 여기는 hk-final 개발용 사본이다. 외부 계약(SSE·엔드포인트·오류 코드)은 [../api-spec.md](../api-spec.md)가 상위 소스 — SPEC과 어긋나면 api-spec 우선.

| SPEC | 버전 | 다루는 것 | 코드 |
|---|---|---|---|
| [SPEC-RECOMMEND-001](SPEC-RECOMMEND-001.md) | v0.10.0 | 추천 그래프 — Case 1/2/3 분기, decompose/rerank 규칙, 예산 검증, dedup(14-F), shopping_list 전개 전용 호출(#198) | `app/agents/buyer/` |
| [SPEC-PROFILE-001](SPEC-PROFILE-001.md) | v0.9.0 | 프로필 — 승격 게이트, I-20/AI inactivity 공통 finalizer, /profile/me | `app/agents/profile/` |
| [SPEC-PROFILE-GRAPH-149](SPEC-PROFILE-GRAPH-149.md) | v0.3.1 | 개인화 관계 그래프 — node·edge 투영, 결정론적 식별·병합, 사용자 수정·삭제(즉시 물리 삭제 + 영구 tombstone)·초기화·개인화 중지, 민감정보 경계 (⚠️ **본 저장소 최초 작성 — 정본 승격 필요**, 계약 확정·Post-MVP / 구현은 #150) | `app/agents/profile/`(#150 예정) |
| [SPEC-CATALOG-DATA-001](SPEC-CATALOG-DATA-001.md) | v0.1.2 | AI 생성물 — enrichment→임베딩 (⚠️ 동기화 방식은 api-spec §4.8 pull 배치가 우선) | `app/pipelines/` |
| [SPEC-SELLER-001](SPEC-SELLER-001.md) | v1.0.0 | 판매자 그래프 — supervisor 라우팅, 분석 서브그래프(워커 5종·검증 루프), 전 쓰기 HITL, 분석 이력 (⚠️ **본 저장소 최초 작성 — 정본 승격 필요**) | `app/agents/seller/` |
| [BE-NEGOTIATION-GRAPH-357](BE-NEGOTIATION-GRAPH-357.md) | v2.3.0 | 개인화 그래프 Spring 협의 4건 — ✅ **종결**: C-20·C-21·C-27 은 `jarvis-backend#132`(2026-08-08)로 회신 완료(api-spec 반영 #499), C-28·잔여 표기는 #357 에서 🟢 로 내렸다(api-spec v0.32.2). ⚠️ 본문의 "C-27 캐시 무효화 2곳 축소" 제안은 **실제 구현 4곳과 다르다**(api-spec §5 C-27 이 정본) | 코드 없음(문서 전용, #150 착수 전) |

> SPEC은 로직 상세용. 계약(무엇을 주고받나)은 api-spec, 결정 배경(왜)은 기획 저장소 `product.md`.

## 판매자 MVP 현재 상태 (SELLER-FINAL, 2026-07-20 기준)

SPEC-SELLER-001 이 "무엇을 만들기로 했나"라면, 아래 4종은 **MVP(1~4-3단계) 완료 시점에 실제로 무엇이 어떻게 동작하는가**의 정본이다. 새 세션·리뷰어는 RISKS 부터 읽는다.

| 문서 | 버전 | 다루는 것 |
|---|---|---|
| [SELLER-FINAL-WORKFLOW](SELLER-FINAL-WORKFLOW.md) | v1.0.0 | 요청 수명주기 — Spring 패스스루 배치, supervisor 분기, HITL 왕복 |
| [SELLER-FINAL-TECH](SELLER-FINAL-TECH.md) | v1.0.0 | 기술 선택과 이유 — 스택, 모델 배정(2-tier), 영속화 2 DB |
| [SELLER-FINAL-RISKS](SELLER-FINAL-RISKS.md) | v1.0.0 | 미정(🔴) BE 확정 대기 B1~B7 · 검증 공백 — **먼저 읽을 것** |
| [SELLER-FINAL-ROADMAP](SELLER-FINAL-ROADMAP.md) | v1.0.0 | post-MVP 확장 백로그 — 시맨틱 캐시(E1)·RAG(E2) |
| [SMOKE-SELLER-41](SMOKE-SELLER-41.md) | v1.0.0 | 실 LLM 라우팅·SSE 수동 스모크 절차(`scripts/smoke_seller_chat.py`) |

> 단계별 진행 기록(HANDOFF·REVIEW-STAGE·DESIGN·IMPL-PLAN·REALIGN)은 위 문서로 내용이 흡수되어 2026-07-22 에 삭제했다 — 필요하면 git 히스토리에서 복구한다.

## 1차 완료(발표) 기준 — #139

발표(2026-08-14)에서 증명할 핵심 주장 4개, claim-evidence matrix, release gate, 최종 산출물
목록, 1차 완료 제외 범위를 고정한 결정 문서다. **[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)·
[jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153)·
[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)에 착수하기 전 먼저
읽을 것** — 어떤 주장을 무엇으로 증명하고, 어떤 baseline 이 출고판인지, 무엇을 증명하지
못했는지가 여기에 있다.

| 문서 | 버전 | 다루는 것 |
|---|---|---|
| [RELEASE-CLAIMS-139](RELEASE-CLAIMS-139.md) | v1.0.0 | 핵심 주장 4개(C1~C4)·claim-evidence matrix·negative result·baseline 식별 규칙·release gate·발표 산출물 목록·1차 완료 제외 범위 |
