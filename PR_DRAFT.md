docs(api-spec): #472 정본 전수 대조 — 확정·구현 완료된 계약을 사본에 동기화하고 잔여를 갈래별로 분리한다

## 변경 요약

- #472 Notion API 정본을 전수 대조해 46개 명시 범위의 판정, 코드 증거, 후속 작업을 docs/api-spec-canonical-audit.md에 기록했습니다.
- 정본에서 이미 확정돼 문서만 낡은 I-1(리뷰 0건·실패 응답표), I-6(`salesCount`), I-8(브랜드 스코프·집계 필드), I-9~I-12(`DELETED`), I-13(행동 집계), I-14(식별자·스코프), I-16(churn), I-17(`brandId`), I-20(`reason`), I-21, I-24, I-26~I-31, I-32~I-36(즉시 물리 삭제), P-5(`source`·미렌더), CH-2(`budget`)와 Notion SSoT 복귀 정책을 docs/api-spec.md v0.29.0에 동기화했습니다.
- 구현이 필요한 계약 드리프트와 정본의 미해결 질문은 사본을 추측해 바꾸지 않고 후속 이슈 초안으로 분리했습니다.

## 관련

- Closes #472
- Part of #461
- api-spec §3.1, §3.5, §3.7, §3.8~3.9.4, §4.2, §4.4~4.6, §4.8, §4.11~4.12, §4.14~4.20, §6.1~6.2 (v0.29.0)

## 결과와 범위

- 정본 인덱스 98건(범위 안 46건·범위 밖 52건)을 전부 대조했습니다.
- ① 사본 동기화 9건, ② 코드 변경 필요 16건, ③ 정본 미해결 7건, 정합 14건으로 분류했습니다. ③의 I-32~I-36에는 draft 판단과 분리 가능한 확정 wire-field 정정을 병행했습니다.
- 범위 밖 52건 표를 감사 문서에 반영했습니다.

## 후속 이슈 제안 — 코드 변경 필요

### [후속] I-2/E-1 장바구니 분석 이벤트 계약을 정본에 맞춰 동기화

I-2는 chatSessionId와 quantity 응답, Spring after-commit add_to_cart 적재를 요구하고 E-1은 14개 event type 및 producer 규칙을 요구한다. app/schemas/spring.py의 AddToCartRequest/AddToCartResult, app/clients/spring.py, analytics producer를 한 변경으로 검증해 이중 적재를 막는다.

### [후속] 판매자 분석 internal 스키마를 I-6/I-8/I-13/I-14/I-16 정본에 맞춰 갱신

salesCount, brand scope, suspiciousMemberCount, 행동 집계 확장, customerLabel HMAC, churn null 의미를 수용한다. 근거는 app/schemas/spring.py의 SalesSeriesPoint/OrderEventsResult/ChurnMember, app/clients/spring.py get_account_events, app/agents/seller/tools.py get_order_events다.

### [후속] I-9~I-12 삭제 상태를 DELETED로 전환

상품 생성·수정·삭제·상태 전이에서 HIDDEN을 삭제 상태로 쓰는 소비를 DELETED 계약으로 정렬한다. app/schemas/spring.py의 SellerProductRow와 상품 요청 모델, 관련 도구의 상태 분기를 함께 검증한다.

### [후속] I-17/I-20/P-5 계약 변경을 클라이언트와 홈 추천 소비에 반영

I-17 brandId, I-20 inactivityTimeout, P-5 PERSONALIZED/NOT_PERSONALIZED 및 fallback 미렌더 규칙을 구현한다. app/schemas/spring.py ProductChange, app/schemas/profile.py SessionEndEvent, app/services/home_recommendation.py가 증거다.

### [후속] Notion I-3/I-4/I-19/P-5 서술 충돌을 단일 계약으로 정리

I-3의 구 DTO 예시, I-4의 Korean representativeStatus와 I-19 enum, P-5의 구 AI_RECOMMENDED reason 설명을 정본에서 먼저 합의한다. 합의 후 저장소 사본과 코드 적용은 별도 검증한다.

## 후속 이슈 제안 — 정본 미해결

- `CLAUDE.md`의 `docs/api-spec.md` 정본 정책을 Notion SSoT로 되돌리는 후속 이슈가 필요합니다(이번 문서 전용 PR에서는 변경하지 않음).

### [후속] I-1 brandName 부분일치 정책 확정

지정된 브랜드 중 하나라도 일치하는 OR 의미, unknown 무시 및 전부 unknown일 때만 0건이라는 답변 초안을 검토해 Notion 문구를 확정한다. 정본의 미해결은 부분매칭 여부다.

### [후속] I-32~I-37 개인화 그래프의 draft 표시를 정본 확인 후 해제

I-35 폐기와 I-34 즉시 물리 삭제는 사본에 반영했다. 나머지 I-32/I-33/I-34/I-36/I-37의 Spring 프록시 및 구현 상태를 Notion에서 확인한 뒤 draft 표시만 일괄 정정한다.

### [후속] S-4 정본에 ship draft.op와 orderItemId를 반영

I-30은 확정·구현 완료이나 S-4는 ship op를 누락했다. 기존 product 레인과 HITL 승인을 보존한 채 S-4 정본만 갱신한다.

## #461 완료 조건 추적

- [x] canonical audit 기준선 및 범위 밖 표 확정
- [ ] ② 코드 변경 항목을 계약군별 후속 이슈로 분할·구현
- [ ] ③ Notion 질문에 대한 답변과 정본 갱신 완료
- [ ] P-5/I-3/I-4/I-19 정본 충돌 해소
- [ ] 후속 구현 후 사본 재대조 및 회귀 검증

## 재발 방지

- 채택: 정본 대조 최종 시점과 MCP 직접 조회 방법을 감사 문서 상단에 기록하고, 선언된 범위 수 대신 식별자 열거를 검산한다.
- 채택: 코드 변경이 필요한 항목은 사본 동기화와 분리해, 문서가 구현 완료를 오인하게 만들지 않는다.
- 미채택: Notion 덤프 자동 동기화는 이번에 도입하지 않는다. 정본 접근/승인 흐름이 저장소 밖에 있고, 검증 가능한 계약 추출 형식이 아직 고정되지 않았기 때문이다.

## 체크리스트

- [x] uv run pytest — 워크트리 `.env`가 있으면 38건이 실패하지만, 내용을 열지 않고 잠시 치운 뒤 전량 `5035 passed, 156 deselected, 1 warning in 289.65s`를 확인했고 CI는 `.env` 없이 돌므로 이번 문서 변경과 무관함
- [x] uv run ruff check 통과
- [x] 기능/주제 완료 시 CHANGELOG.md [Unreleased] 갱신
- [x] 계약 변경 전 Notion 정본을 직접 대조하고 api-spec 사본을 동기화
- [x] 개발 중 발견한 범위 집계 불일치를 docs/lessons.md에 기록
- [x] 신원은 JWT sub에서만 도출 (요청 본문 신뢰 금지) · productId는 string

## 리뷰 노트

docs/api-spec.md의 변경은 ① 문서 전용 동기화에만 한정했습니다. I-35 restore와 I-32~I-34/I-36의 확정된 suppress/undo 필드는 정정했지만, I-32~I-37의 draft 여부는 정본 확인 전 추측해 바꾸지 않았습니다. 로컬 `.env`를 열지 않고 잠시 치운 상태에서 pytest 전량 `5035 passed, 156 deselected, 1 warning in 289.65s`를 확인했으며 CI도 `.env` 없이 실행되므로 이번 문서 변경과 무관합니다. PR 생성은 이 작업 범위 밖입니다.

## 1라운드 F1~F10 대응

- F1: 정본과 다른 사본은 모두 `① 사본 동기화 + ② 코드 변경 필요`로 기록하고 사본을 정본에 맞췄습니다.
- F2: I-1 §4.6의 rating/reviewCount 규칙, 실패 응답, 실패가 아닌 5개 케이스를 반영했습니다.
- F3: I-1 brandName을 OR 의미로 바로잡고 부분매칭만 미해결로 남겼습니다.
- F4: 감사표 endpoint 경로와 복사 섹션을 정본에 맞췄습니다.
- F5: CH-2 예산을 드리프트로 분류하고 §6.1 구현 목록에서 제외했습니다.
- F6: Notion SSoT 복귀 이력과 CLAUDE.md 후속 이슈를 기록했습니다.
- F7: 커밋 제목·본문을 저장소 형식으로 정리했습니다.
- F8: 인덱스 합계를 98건(46+52)으로 정정했습니다.
- F9: I-26/I-27/I-28의 403 부재를 명시했습니다.
- F10: pytest 실패 원인을 워크트리 `.env`로 정정했습니다.
