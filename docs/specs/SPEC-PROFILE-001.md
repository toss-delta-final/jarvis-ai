---
id: SPEC-PROFILE-001
version: 0.10.0
status: draft
created: 2026-07-10
updated: 2026-08-10
author: navis
priority: high
issue_number: 79
---

> ✅ **저장소 정본** — 외부 HTTP 계약(엔드포인트·필드·오류 코드)은
> [docs/api-spec.md](../api-spec.md)가 소유하고, 본 SPEC은 프로필 파이프라인의 내부 동작과
> 인수 기준을 소유한다. 두 문서가 어긋나면 계약은 api-spec을 먼저 고친 뒤 본 SPEC을 동기화한다.

# SPEC-PROFILE-001 — 사용자 프로필 파이프라인 (User Profile Pipeline: reader / builder / gate)

> 본 SPEC은 product.md Section 12-A **결정 16**(프로필 파이프라인 상세 설계)를 직접 입력으로 하여, 구매자 그래프(`POST /chat`)에 `profile_summary`를 공급하고 대화·구매 이력에서 프로필을 누적/갱신하는 **프로필 파이프라인**(`app/agents/profile/`의 `reader`/`builder`/`gate`)의 동작을 EARS 요구사항 수준으로 확정한다.
> 결정 16의 `profile_summary` 계약(하이브리드 단일 문자열·구조화 블록의 FilterSet 매핑 한정·최근 맥락 섹션·문자 기반 1,000자 config 상한·생성 측 집행·게이트 통과 미폐기 fact 한정·신규 회원 `None`), 물리 저장소(PostgresStore/LangGraph BaseStore·네임스페이스·카탈로그와 **완전 별도 인스턴스**[v0.2.0: 결정 16-A로 MVP는 **단일 인스턴스 + 별도 데이터베이스**로 개정]·pgvector·BaseStore 내장 semantic 인덱스 + 결정 6 임베딩 모델), 게이트 구현 분담(LLM 태깅 + 코드 계산), transient 3종 MVP, write 소스(대화 델타 + 구매 이력 미러), 저장된 세션 버퍼 기반 트리거(Spring 명시적 종료 + AI 내부 비활동 종료), hot-path "기억해", 마이페이지 GET only는 **구속 제약(binding)** 이며 본 SPEC에서 재논의하지 않는다.
> 결정 16이 상속하는 결정 4(OKF 위키 포맷)·결정 4-A(운영 정책 6항)도 변경 없이 구속 상속한다. 본 SPEC은 그 위에 델타 레코드/게이트 상태/Store item/GET API/`profile_summary` 섹션 레이아웃의 Pydantic 수준 스키마, 오류 처리, 인수 기준을 확정한다.

## HISTORY

- **v0.10.0 (2026-08-10, 이슈 #321)** — **OPEN-P5(대화 보존 기간)를 해소했다** — `conversation_turns`(완료 대화 전사록)의 보존 기간이 신설 `conversation_retention_days`(config 주입, 기본 **90일**)로 확정됐다. 값은 감사 원장 `graph_audit_retention_days`(`SPEC-PROFILE-GRAPH-149` §11, 기본 90일)와 **의도적으로 짝지었다** — 감사 행이 지문만 남기므로(REQ-PGRAPH-081) 원문 대조 상대는 전사록뿐인데, 전사록이 감사 원장보다 먼저 지워지면 그 사이 구간이 조사 불가능해진다. 이 관계는 **기동 시점 fail-fast** 검증기(`conversation_retention_days <= graph_audit_retention_days`)로 강제한다. **삭제 주체는 별도 스케줄러 job**(`conversation_retention_sweep`, 기본 1시간 주기)이며 유계 배치(`ORDER BY created_at LIMIT` + `FOR UPDATE SKIP LOCKED`, 배치당 짧은 트랜잭션 1개)로 지운다 — `run_session_context_sweep`(sole lifecycle authority, 60초 주기)에 얹지 않았다: 그쪽 except 경로의 의미("activity lease 가 다음 sweep 에서 복구")가 실패한 DELETE 의 의미와 다르고 주기도 60배 과하다. **"처리 전 세션 버퍼"는 이 항목의 범위가 아니다** — 그쪽(`ProfileStore.append_session_ctx`)은 이미 별개 lifecycle(`profile_session_idle_timeout_s`/idle sweep, 세션 종료 시 consolidation 소비)로 관리되고 있어 새 시간 기반 삭제 정책이 필요하지 않았다. 요구사항 신설·삭제 없음, 파이프라인 동작·저장소 구성·게이트 규칙 무변경, **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변** — `turns_for()`·`get_turn()` 의 프로덕션 호출부가 없어 전사록은 감사·상관관계 조회 전용이다. 같은 커밋이 하드 PII 저장 게이트(`app/core/pii.py`, `SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-071)도 구현했다 — "기억해" hot-path·세션 델타 승격·요약 재작성 어느 경로로도 하드 PII(전화번호·주민번호·카드번호·계좌번호·이메일·시크릿 토큰)가 저장 전에 전량 폐기되도록 닫았다(그래프 트리플의 `label`/`anchorPhrase` 포함). 동반 개정: api-spec v0.32.9 · `SPEC-PROFILE-GRAPH-149` v0.3.3.
- **v0.9.0 (2026-08-09, 이슈 #499)** — `SPEC-PROFILE-GRAPH-149`·api-spec v0.32.0 의 **undo 폐기**에 동기화했다. **v0.8.0 이 등재한 문장 3건이 되돌아간다.** (1) **REQ-PROF-034 (a) 개별 삭제 = 억제 후 물리 삭제 → 즉시 물리 삭제** — 되돌리기 API(I-35·`M-14`)가 2026-08-07 정본에서 폐기되면서 undo 창이 지킬 대상이 사라졌다. **영구 tombstone(재파생 차단)은 유지**한다 — 없애면 세션 버퍼 flush 가 지운 취향을 되살린다. (2) **좁은 예외 2건 → 1건**(민감 파생 보존기간 만료만). "개별 삭제 undo 창 만료"는 예외가 *해소*된 게 아니라, 유예가 사라져 **애초에 기계 경로가 아니게 된** 것이다 — 분류 정정이다. (3) **AC-PROF-31 재작성** — 인수 기준에서 복구 단계를 제거하고 "즉시 물리 삭제 + 영구 재파생 차단"으로 좁혔다. 요구사항 신설·삭제 없음, 파이프라인 동작 무변경.
- **v0.8.1 (2026-08-06, 이슈 #356)** — OPEN-G0 착수에 맞춘 **보강**(요구사항 신설·삭제 없음, 계약 무개정). (1) **REQ-PROF-086의 단계 분담 명확화** — 트리플의 *식별자 확정*은 1단계(게이트 통과 직후 결정론적 resolver)이고 2단계는 확정분을 병합해 `("graph", user_id)` 문서로 산출한다. 배치마다 재-resolve 하면 거리 임계·통제 어휘가 바뀔 때 같은 fact 가 다른 `node_id` 로 붙어 **tombstone 을 우회**하므로, 결정론을 기능 요구로 못박은 `SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-010과 충돌한다. (2) **§5.3 `graph_triples` 소유 관계 명시** — 이 필드는 *그 fact 가 낳은* 트리플의 증거 측 기록이고, 정본 집계는 `("graph", user_id)/"v1"` 단일 문서다(신규 SPEC §7.1 "fact 항목은 증거 저장소로 유지하고 값에 필드만 더한다"). 필드명·타입은 v0.7.0 선언 그대로 쓴다 — 새 이름을 만들지 않는다. OPEN-P12는 여전히 **해소가 아니라 진행 중**이다. v0.8.0(#322)의 삭제 계약 개정과 충돌하지 않는다 — 본 보강은 *트리플을 만드는 쪽*이고 v0.8.0은 *지우는 쪽*이며, undo 창·원문 물리 삭제는 사용자 변경 경로라 #150/#358 소관이다.
- **v0.8.0 (2026-08-06, 이슈 #322)** — `SPEC-PROFILE-GRAPH-149` v0.2.0·api-spec v0.26.0 의 삭제 계약 개정에 동기화했다. **v0.7.0 이 단정한 문장 2건이 뒤집힌다.** (1) **REQ-PROF-034 (a) 개별 삭제** — "억제(tombstone), 이력 보존, 복구 가능"에서 **"즉시 억제 → undo 창(`graph_undo_window_s`, 기본 5분) → 원문 물리 삭제, 재승격 차단용 tombstone 만 잔존"** 으로 개정. AC-PROF-31 도 같이 고쳤다. (2) **REQ-PROF-034 "좁은 예외 1건·유일"** → **2건** — 민감 파생 보존기간 만료(REQ-PGRAPH-077)에 더해 개별 삭제 undo 창 만료(REQ-PGRAPH-025)가 기계 경로 하드 삭제의 두 번째 지점이다. 성격이 다르다는 점을 명시했다(전자는 기계가 판정해 개시, 후자는 **사용자 요청의 지연 집행**이라 "기계가 조용히 지우는 것"에 애초에 해당하지 않는다). (3) **REQ-PROF-085·AC-PROF-32 전사록 보존 반전** — 전체 초기화가 이제 `conversation_turns`(해당 `user_id` 행 전체)도 물리 삭제하고 **변경 감사 로그만 보존**한다. 근거는 REQ-PROF-034 가 이미 채택한 논거의 연장이며(적용 범위 한정, 예외 신설 아님) 새 원칙을 만들지 않는다. 대화 기록은 `pg-profile` 에만 있고 Spring 에 사본이 없어 **AI 단독 완결**이다(#322 선결 확인). 세션 종료(I-20/D6)는 여전히 전사록을 지우지 않는다 — 로그아웃은 삭제 요청이 아니다. (4) **OPEN-P5 관계 명시(해소 아님)** — 전사록 TTL 은 **시간 경과** 트리거이고 REQ-PROF-085 는 **사용자 명시 요청** 트리거다. #322 는 초기화 트리거만 확정했고 TTL 은 여전히 미정이나, "사용자가 원하면 지울 수 있다"는 최소 보장이 확보돼 긴급도는 내려간다. 파이프라인 동작·저장소 구성·게이트 규칙은 무변경이다.
- **v0.7.0 (2026-08-05, 이슈 #149)** — 승격된 취향을 **사용자가 항목 단위로 고칠 수 있게** 하는 계약(api-spec §3.8·§3.9, 신규 `SPEC-PROFILE-GRAPH-149`)과 정합을 맞췄다. **이는 구현 부채 상환이 아니라 설계 결정의 번복이다** — 결정 16의 "마이페이지 GET only"를 넘어서므로 결정 개정 레코드가 필요하고(api-spec §8 항목 9), 승인 전까지 그 계약은 🔴 초안이다. 근거는 이슈 #147의 커밋된 실측이다: 프로필이 깨끗하면 +0.20, 노이즈가 섞이면 −0.053, 반복으로 부풀려지면 −0.117 nDCG@10 — 즉 **오염된 프로필은 측정 가능한 품질 손실을 만드는데 그것을 되돌릴 수 있는 유일한 주체(사용자)에게 권한이 없었다.** 개정 내용: (1) **EX-P3 배제 범위를 `PUT /profile/me` 마크다운 전문 편집으로 한정** — LLM이 쓴 산문의 부분 수정은 여전히 불가능하고, 항목(edge) 단위 제어가 그 문제를 우회한다. (2) **EX-P6과 충돌하지 않음을 명문화** — 기각된 것은 temporal KG 백엔드·그래프 추론·파라메트릭 편집이며 신규 표면은 저장 모델을 바꾸지 않는 1-hop 읽기 투영이다(graph DB 미도입 불변). (3) **REQ-PROF-034 적용 범위를 기계 경로로 한정** — 사용자 개별 삭제는 tombstone이라 삭제가 아니고, 사용자가 명시적으로 요구한 전체 초기화는 기계 자동 처리가 아니어서 금지 대상이 아니다. 금지되는 것은 *기계가 사용자 데이터를 조용히 지우는 것*이다. AC-PROF-13을 기계 경로로 좁히고 AC-PROF-31/32를 신설했다. (4) **REQ-PROF-012의 confidence 수치 미노출을 `profile_summary` 스코프로 한정** — 그래프 표면은 3버킷 라벨만 노출하므로 수치 불변식은 유지된다(축소된 번복). (5) 신규 REQ-PROF-083~086(개인화 중지 전파·억제 제외·초기화 범위·구조화 트리플 산출). (6) **OPEN-P12는 해소가 아니라 우선순위 상향** — 신규 계약이 결정론적 트리플 산출을 이슈 #150의 **선결 조건**으로 만들었고, 후보 방향(consolidation이 구조화 산출물을 함께 낸다)을 확정 방향으로 승격한다. OPEN-P10은 부분 해소. 파이프라인 동작·저장소 구성·게이트 규칙 자체는 무변경이다.
- **v0.6.0 (2026-07-31, 이슈 #119)** — 프로필 **주입 스코프**를 도입하고 세션 버퍼 적재 규율을 신설했다. 실측 회귀: `profile_summary`를 decompose 프롬프트에 발화와 같은 격으로 주입하면 LLM 이 취향을 `priceMax`/`brand`/`color` 하드 필터로 승격시키고, 그 필터가 스레드 필터 저장소에 영속돼 다음 턴 `PRIOR_FILTERS`로 재주입되며 세션 내내 후보를 좁힌다 — 게스트는 이 입력이 없어 손실이 0이므로 **개인화가 순손실**이 되는 비대칭이 발생했다. 따라서 **이는 구현 부채 상환이 아니라 설계 결정의 번복**이다: §5.1·REQ-PROF-011 이 승인하던 "구조화 블록 → decompose `derived` 필터 파생"을 MVP 에서 유예하고(config `profile_injection_scope` 기본 `rerank_only`), 개인화는 rerank 순서 신호로만 수행한다. REQ-PROF-014 는 입법 의도(소비처별 요약 **생성** 금지)를 유지한 채 "주입하는 경우 동일 문자열"로 자구를 정리했다 — 스코프 선택은 요약을 다르게 만드는 것이 아니다. 신규 REQ-PROF-026(세션 버퍼 intent 배제 + 정규화 동일 발화 **적재 상한**, 결정론 코드)을 추가했다 — 상한은 최소 2 이며, 1로 낮추면 게이트의 반복 승격 경로가 죽는다(반복 신호 제거가 아니라 증폭 차단이 목적). 요구사항 스키마·저장소 구성 무변경. api-spec 무개정(개인화 강도는 와이어 계약에 없음).
- **v0.5.0 (2026-07-30, api-spec v0.16.0 동기화)** — session/thread 축 분리(SPEC-CHAT-SESSION Option B)를 반영해 **Spring I-20 발화 사유를 `logout` 하나로 축소**했다. 구 `newConversation`은 제거 — 축이 갈린 뒤 "새 대화"는 FE가 `threadId`만 새로 생성하고 세션을 유지하므로 CH-1도 I-20도 호출되지 않아 **사유 자체가 발화되지 않는다**. `reason`은 enum 미강제라 수신해도 400은 아니고 관측용으로 기록만 된다. **프로필 파이프라인은 종전대로 session 축**이며(세션버퍼·`profile_session_activity`·멱등키 모두 `(userId, sessionId)`), 한 접속 아래 여러 방의 발화가 **한 세션 버퍼로 모이는** 구조가 이 축 분리의 전제와 정합한다 — 따라서 REQ/AC의 실질 로직 변경은 없고 사유 목록만 좁혔다(REQ-PROF-051, AC-PROF-28, §1.3 표, 결정 12 행).
- **v0.4.0 (2026-07-23, 이슈 #79)** — MVP 세션 종료 트리거 소유권을 확정했다. Spring I-20은 `logout`·`newConversation`만 전달하고 탭 닫기 신호는 제거한다. AI는 수락된 회원 발화 저장 시 pg-profile의 세션 활동 시각을 DB 서버 시각으로 갱신하고, 기본 10분 timeout/60초 sweep의 단일 인스턴스 스케줄러가 인덱스 기반 bounded batch로 비활성 세션을 선점한다. 내부 timeout은 HTTP 자기 호출 없이 I-20과 같은 finalizer·고정키 claim을 사용하되, idle 성공은 영구 종료가 아닌 재개 가능한 checkpoint로 claim을 해제한다. 새 활동은 completed activity를 active로 되돌리고 이전 종료 generation을 같은 transaction에서 무효화하며, terminal finalizer는 처리 중 새 activity를 completed로 덮지 않는다. scheduler는 라이브 스트림 슬롯을 점유하지 않는다. 처리 전 활동/활성 스트림 재확인, token+lease claim, claim별 실패 격리·crash 재시도와 명시적 종료 경합 인수 기준을 추가했다.
- **v0.3.0 (2026-07-20, 이슈 #33; v0.15.17 구현 보강)** — 저장소 이관 구현 완료 반영. (1) **임베딩 모델/차원 갱신**: 결정 6 "셀프호스트 1024차원"은 카탈로그 파이프라인이 이슈 #31 로 Google `gemini-embedding-001`(1536-dim, MRL 절단 수동 L2 정규화)로 전환되며 stale — REQ-PROF-074 자체가 "카탈로그와 모델 공유"를 요구하므로 프로필도 동일 모델/차원을 그대로 따른다(신규 계약 협의 아님, 기존 REQ 의 자연스러운 적용). §5.3 네임스페이스 주석·§1.3·§4 결정 6 행·REQ-PROF-074·§10 비용 문구 갱신(차원 1024→1536, 임베딩 비용 0 문구 삭제 — Google API 호출이라 토큰 비용 발생). (2) **checkpointer→BaseStore 로 구현 확정, OPEN-P9 해소**: session_context(구매자 스레드 상태 전반 — ThreadFilter/Cart/Revert/session_ctx)는 실제 LangGraph StateGraph 가 없는 구매자 실행 모델(단순 함수 호출 체인) 특성상 checkpointer 가 아니라 BaseStore(app/core/pg_store.py 공유 연결, 별도 인스턴스는 아니고 같은 pg-profile 물리 인스턴스 내 별도 store 객체) 로 구현됨 — write 소유는 구매자 그래프(app/agents/buyer/graph.py) 그대로. (3) **fact 저장 단위 확정**: REQ-PROF-070 "위키 파일 1개 = item 1개" 원칙을 그대로 적용해 fact 마다 개별 store item(uuid 키)으로 저장 — semantic 인덱스가 fact 단위로 실제 동작(요약/세션버퍼는 `index=False`). (4) **session-end 멱등 파생키 lifecycle**: 전용 `processed_events` 테이블에서 `session-end:{userId}:{sessionId}`의 PROCESSING(token+lease)과 COMPLETED를 분리한다. 실패·취소는 claim 해제, crash 잔재는 lease 재선점하며 성공 뒤에만 완료 마킹한다(app/agents/profile/processed_events.py, db/profile/init/00_processed_events.sql). (5) OPEN-P11 부분 해소: 서빙 형태는 FastAPI 프로세스 내 동기 SDK 호출(app.pipelines.embedding.embed_texts, google-genai)로 확정. 요구사항·스키마 구조·게이트 규칙은 무변경.
- **v0.2.0 (2026-07-15)** — 결정 16-A(저장소 물리 구성 개정) 반영. "카탈로그와 완전 별도 Postgres 인스턴스"를 MVP 기준 **단일 Postgres 인스턴스 안의 별도 데이터베이스 2개**(catalog/profile)로 개정 — 논리 분리 규율(DB 단위 분리로 cross-DB 조인 구조적 차단 + 계정 분리[search read-only/프로필 워커 profile 한정] + cross-DB 의존 금지)로 부하 격리 목적을 대체하고, 물리 인스턴스 분리는 고도화 승격 경로(연결 문자열 교체 수준)로 유예. REQ-PROF-072/073/074 개정, AC-PROF-21 개정, DoD·불변식 문구 갱신, OPEN-P9에 물리 결합 논점 소멸 주석. 그 외 요구사항·스키마 불변. 근거: 데모 규모에 격리할 부하 없음(2026-07-15 AWS 배포 구성 논의, product.md 결정 16-A).
- **v0.1.0 (2026-07-10)** — 최초 작성. 결정 16(프로필 파이프라인 상세 설계)을 파이프라인 EARS 명세로 구체화. `profile_summary` 섹션 레이아웃·델타 레코드·게이트 상태·Store item·GET API 스키마 초안 확정. reader(동기 read, LLM 0회)·builder(2단계: 세션별 델타 생성 → sleep-time consolidation)·gate(3조건 승격, LLM 태깅 + 코드 계산 분담) 계약 정의. 결정 16이 구속 상속하는 결정 4/4-A 및 관련 결정 5/6/8/9/10-A/12/14-F를 §4 참조 표에 반영. 결정 16 내부의 몇몇 판독 긴장(에피소딕 최근 맥락의 게이트 예외, 구매 신호의 명시성 부재와 3조건 AND 여부, checkpointer 소유 경계, GET 노출 범위)은 해소하지 않고 §9 OPEN 항목으로 명시 등록한다.

---

## 1. 개요 & 범위 (Overview & Scope)

### 1.1 목적

사용자의 대화·구매 이력에서 취향/맥락을 지속 누적·갱신하고(build), 그래프 진입 시 추천·재랭킹이 소비할 압축 취향 요약(`profile_summary`)을 저지연 동기 조회로 공급하며(read), 일시적 요청 오염 없이 검증된 선호만 프로필로 승격시키는(gate) **프로필 파이프라인**의 관찰 가능한 동작을 정의한다. 파이프라인은 요청 경로 안의 동기 `reader`와, 요청 경로 밖의 비동기 `builder`(세션별 델타 생성 → sleep-time consolidation) + `gate`(승격 판단)로 구성된다.

### 1.2 In Scope (본 SPEC이 확정하는 것)

- `reader`: 그래프 진입 시 `profile_summary`의 동기 조회 — store 단일 get, LLM 0회, 지연 크리티컬. 게스트·신규 회원은 `None`.
- `profile_summary` 생성 계약: 하이브리드 단일 문자열의 섹션 레이아웃(구조화 블록 / 산문 / 최근 맥락)과 구조화 블록의 필드 규약(FilterSet 매핑 가능 속성 한정: 가격 성향·선호/회피 브랜드·평점 성향·Layer 2 속성), consolidation 재작성에 의한 크기 집행.
- `builder` 1단계(델타 생성): 미처리 세션당 Sonnet 1회 호출로 명시성/현저성/transient 태그가 붙은 후보 델타 산출 + 세션별 관심 분포 스냅샷.
- `builder` 2단계(sleep-time consolidation): EMA 누적·승격 판정·recency-wins 충돌 해소·supersede 처리·중복 병합의 결정론적 코드(병합된 위키 파일의 텍스트 통합에만 LLM 사용, 신선도/승격 판단에는 LLM 미사용), 크기 상한 내 요약 재생성.
- `gate`: 3조건 승격 로직과 그 LLM 태깅/코드 계산 분담, 엔트로피 신호의 최소 세션 수 가드, 구매 신호 판정 경로(명시성 없음).
- 트리거: 저장된 세션 버퍼(정합성 기반), Spring best-effort 세션 종료 통지(idempotent), AI 내부 10분 비활동 스케줄러, sleep-time 스케줄링 config, 구매 이력 스캔.
- hot-path "기억해": fact 즉시 기록, 턴 중 요약 재생성 없음.
- 저장소 스키마: Store item 구조(valid_from/last_confirmed/superseded_by 포함 frontmatter 메타·confidence·type semantic|episodic), 네임스페이스 레이아웃, 임베딩 인덱스 구성.
- 마이페이지 표시용 `GET /profile/{user_id}` API(마크다운 passthrough).
- **[v0.7.0, 이슈 #149]** consolidation이 요약과 함께 산출하는 **구조화 트리플**(REQ-PROF-086)과, 개인화 중지·사용자 억제 상태를 요약·reader에 반영하는 규율(REQ-PROF-083/084/085). 그래프 투영·제어 API 자체의 모델·규칙은 `SPEC-PROFILE-GRAPH-149` 소관이다.
- 오류 처리(델타/consolidation LLM 실패, store 불가용, activity claim 고착 — 데이터 날조 금지, MoAI 3회 재시도 원칙), 인수 기준, DoD.

### 1.3 의존성 (Dependencies — 본 SPEC 외부, 참조만)

이 파이프라인은 아래 컴포넌트가 제공하는 계약에 의존하나, 그 구현은 본 SPEC의 범위가 아니다.

| 의존 대상 | 제공하는 것 | 소유 |
|---|---|---|
| 구매자 그래프 (`agents/buyer/graph.py`) | 그래프 진입 시 `reader` 호출 지점, 진입 시 주입 대상(`profile_summary`) | 구매자 그래프 SPEC(별도) |
| 추천 서브그래프 (SPEC-RECOMMEND-001) | `profile_summary`를 read-only 문자열로 소비(REQ-REC-005/006, OPEN-9 해소 대상) | SPEC-RECOMMEND-001 |
| session_context / 프로필 세션 버퍼 (BaseStore) | 대화 스레드 상태와 회원 발화 버퍼의 영속 저장. 세션 중 write는 구매자 그래프 소관 | 구매자 그래프 SPEC(별도) — 본 SPEC은 finalizer에서 버퍼를 소비(§9 OPEN-P9) |
| 주문 이벤트 미러 (결정 9 채널 확장 / 14-F) | 사용자별 경량 구매 이력 미러(user_id·product_id·category·purchased_at) — read-only 조회. 추천 dedup(14-F)과 동일 미러 공유 | 카탈로그/주문 이벤트 SPEC(별도) |
| Spring 세션 종료 통지 (I-20) | **`logout`** 명시적 종료의 조기 트리거(best-effort) — 유실 허용. **[v0.5.0]** 구 `newConversation` 제거 | Spring / api-spec §3.5 |
| 임베딩 모델 (결정 6, v0.3.0 갱신) | BaseStore 내장 semantic 인덱스의 1536차원(Google `gemini-embedding-001`) 임베딩 계산(카탈로그 파이프라인과 모델 공유, 인스턴스는 별도) | `app.pipelines.embedding`(이슈 #31/#33) |
| 리뷰 분석 그래프 (결정 10-A) | (고도화) 작성자 취향 신호 공급 — 본 SPEC은 수신 계약 자리만 예약 | 리뷰 분석 SPEC(별도, 고도화) |
| 개인화 그래프 (v0.7.0, 이슈 #149) | 승격된 취향의 node·edge 투영과 사용자 제어(수정·삭제·초기화·중지 — **[v0.9.0]** 복구는 폐기). 본 SPEC이 산출하는 구조화 트리플(REQ-PROF-086)을 소비하고, 억제·중지 상태를 본 SPEC의 요약·reader에 되돌려 반영한다 | `SPEC-PROFILE-GRAPH-149` / api-spec §3.8·§3.9 |

---

## 2. Exclusions (What NOT to Build)

[HARD] 본 파이프라인에서 **구현하지 않는** 항목을 명시한다.

- **EX-P1 추천 서브그래프 동작**: `profile_summary`를 소비하는 재랭킹·개인화 로직은 SPEC-RECOMMEND-001 소관(REQ-REC-005/006). 본 파이프라인은 그 문자열을 **생산·공급**하고, 소비 측은 이를 불투명 read-only 문자열로만 취급한다. 추천이 요약을 어떻게 쓰는지는 본 SPEC의 범위가 아니다.
- **EX-P2 리뷰 신호 수신 구현**: 리뷰 분석 그래프(결정 10-A)가 공급하는 작성자 취향 신호의 실제 수신·게이팅 구현은 **고도화 범위(MVP 비구현)**. 본 SPEC은 수신 계약 슬롯(write 소스 열거의 예약 항목)만 남기고 동작을 구현하지 않는다(REQ-PROF-024).
- **EX-P3 프로필 편집 PUT**: 마이페이지에서 사용자가 프로필을 직접 수정하는 PUT(사용자 수정 = confidence 최상급 병합, 결정 4-A 보강 6)은 **고도화 범위(MVP 비구현)**. MVP는 조회(GET)만 제공한다(REQ-PROF-082).
  - **[개정 v0.7.0, 이슈 #149] 배제 범위는 `PUT /profile/me` 마크다운 전문 편집으로 한정한다.** LLM이 생성한 산문을 사용자가 부분 수정할 수 있는 형태가 아니라는 것이 전문 편집을 계속 배제하는 이유이며, 이 배제는 유지된다. 반면 **항목(edge) 단위 수정·삭제·복구·전체 초기화·개인화 중지는 그래프 표면으로 제안**된다(api-spec §3.8·§3.9, `SPEC-PROFILE-GRAPH-149`) — 결정 4-A 보강 6의 "사용자 수정 = confidence 최상급 병합"은 그 경로에서 이행된다. 🔴 결정 16 개정 승인 전까지 초안(api-spec §8 항목 9).
- **EX-P4 브라우저 탭 종료 신호·Spring 세션 수명 판정**: 탭 닫기 전용 FE→BE→AI 또는 FE→AI API를 만들지 않는다. AI의 10분 비활동 판정은 **프로필 버퍼 flush 시점**만 소유하며 Spring Redis 세션의 유효성·만료 응답을 대신 판정하지 않는다. 사용자가 제한 시간 안에 돌아와 발화하면 활동 시각이 갱신되고, 돌아오지 않으면 timeout이 처리한다.
- **EX-P5 주문 이벤트 미러 적재 계약**: 주문 이벤트 → AI 경량 미러의 **적재(ingestion) 계약·이벤트 채널**은 카탈로그/주문 이벤트 SPEC 소관(결정 9/14-F). 본 SPEC은 이미 적재된 미러를 **read-only로 스캔**만 한다(REQ-PROF-054).
- **EX-P6 완전한 시간 인식 지식 그래프·유저별 LoRA**: temporal KG 백엔드(Zep/Graphiti)와 파라메트릭/유저별 LoRA는 결정 4에서 **v2로 유예·명시 기각**됨. 본 SPEC은 OKF 위키(자연어 마크다운 + frontmatter) 논리 모델과 결정론적 recency-wins만 구현하며, 그래프 추론·파라메트릭 편집은 구현하지 않는다.
  - **[정합 v0.7.0, 이슈 #149] 개인화 관계 그래프(api-spec §3.8)는 이 배제와 충돌하지 않는다.** EX-P6이 기각한 것은 **temporal KG 백엔드**(Zep/Graphiti)·**그래프 추론**·**파라메트릭 편집**이다. 신규 표면은 저장 모델을 바꾸지 않는 **1-hop 읽기 투영**이며 **graph database를 도입하지 않고**(신규 SPEC EX-G3) 다홉 순회·추론도 구현하지 않는다 — 즉 EX-P6의 실질 배제는 그대로 남는다. 사용자 편집은 파라메트릭(모델 가중치) 편집이 아니라 **데이터 항목 편집**이다.
- **EX-P7 턴 중 요약 재생성**: `profile_summary` 재생성은 **sleep-time consolidation 전용**이다. 턴 중(요청 경로) 또는 "기억해" hot-path에서 요약을 재생성하지 **않는다**. 같은 세션 내에서 방금 기록된 fact의 즉시 반영은 추천 서브그래프의 멀티턴 filters 병합(SPEC-RECOMMEND-001 §6.7, 본 SPEC 비범위)이 커버한다(REQ-PROF-061).

---

## 3. 용어 (Glossary)

| 용어 | 정의 |
|---|---|
| `reader` | 그래프 진입 시 `profile_summary`를 store 단일 get으로 동기 조회하는 컴포넌트(`agents/profile/reader.py`). LLM 0회, 지연 크리티컬 |
| `builder` | 요청 경로 밖에서 프로필을 갱신하는 컴포넌트(`agents/profile/builder.py`). 2단계: (1) 세션별 델타 생성, (2) sleep-time consolidation |
| `gate` | 후보 델타의 프로필 승격 여부를 판정하는 로직(`agents/profile/gate.py`). LLM 태깅 + 코드 계산 분담(결정 16) |
| `profile_summary` | 그래프 진입 시 주입되는 압축 취향 요약. **하이브리드 단일 마크다운 문자열**(구조화 블록 + 산문 + 최근 맥락). 게스트·신규 회원 `None`(결정 16) |
| 델타(delta) | 한 세션에서 추출된 후보 프로필 변경 사항. 명시성/현저성/transient 태그(LLM)와 대상 파일·구조화 속성을 담음. 승격 전까지는 미확정 상태 |
| 승격(promotion) | 후보 델타가 3조건 게이트를 통과해 프로필 위키(store)에 반영되는 전이. sleep-time에 코드가 판정 |
| 3조건 게이트 | 반복성(EMA confidence 누적) · 현저성(salience) · 명시성(사용자 직접 진술)의 승격 게이트(결정 4-A). "기억해"는 명시성 hot-path 예외 |
| transient(일시적) | "이번엔 비싸도 돼", "엄마 선물" 같은 일회성/상황적 요청. session_context에만 기록하고 프로필 write 후보에서 배제(결정 4-A/16) |
| session_context | pg-profile BaseStore의 구매자 스레드 상태. transient 요청·세션 관찰이 누적되는 계층이며 세션 종료 시 장기 프로필로 자동 전파되지 않음 |
| consolidation | 여러 세션 델타를 위키에 병합·중복 제거·모순 해소하는 sleep-time 배치 처리. 텍스트 통합은 LLM, 신선도/승격 판단은 코드 |
| recency-wins | 최신성 충돌을 결정론적 코드(타임스탬프 비교)로 해소하는 정책. LLM에 최신성 판단을 위임하지 않음(결정 4-A, STALE arXiv:2605.06527) |
| supersede | fact 폐기 대신 `superseded_by`로 이력을 보존하는 처리. 삭제(delete)하지 않음(결정 4-A) |
| EMA | 지수이동평균. 반복 관측 시 confidence 누적·승격, 재확인 없는 선호는 감쇠(PAMU arXiv:2510.09720) |
| 관심 분포 스냅샷 | 세션별 카테고리 관심 분포의 스냅샷. 엔트로피 급증(선물/탐색 세션 신호) 코드 계산의 입력(결정 16 transient (b)) |
| 세션 활동 행 | `(user_id, session_id)`별 `last_activity_at`·상태·claim lease를 보관하여 bounded timeout sweep의 대상을 결정하는 pg-profile 레코드 |
| 미러(order mirror) | 주문 이벤트에서 파생된 AI 서버 측 경량 구매 이력 사본(user_id·product_id·category·purchased_at). 추천 dedup(14-F)과 공유 |
| Store item | PostgresStore(BaseStore)의 저장 단위. key = 위키 파일 경로, value = frontmatter 필드 + 마크다운 본문 |

---

## 4. 관련 결정 참조 (Related Decisions)

본 SPEC은 아래 확정 결정을 구속 제약으로 상속한다(product.md Section 12-A).

| 결정 | 내용 | 본 SPEC 반영 |
|---|---|---|
| 결정 4 | 프로필 저장 포맷 OKF 스타일 자연어 위키 + 경량 frontmatter, `profiles/{user_id}/` 번들(index.md + 지식 단위 파일), semantic/episodic 분리, consolidation 갱신, 진입 시 index.md + 압축 요약만 read | §5.3/§6.2/§6.8 전반 |
| 결정 4-A | 6항 보강 — (1) transient 격리, (2) 3조건 승격 게이트("기억해" 예외), (3) 2단 비동기 쓰기, (4) 임베딩 검색 인덱스 + frontmatter 구조화 필드, (5) valid_from/last_confirmed/superseded_by + recency-wins + EMA 감쇠 + supersede, (6) 마이페이지 노출/편집 | §6.3/§6.4/§6.5/§6.7/§6.8/§6.9 |
| 결정 16 | 프로필 파이프라인 상세 설계(본 SPEC의 직접 입력) — `profile_summary` 계약, 물리 저장소, 게이트 구현 분담, 트리거/Spring 인터페이스, write 소스. **SPEC-RECOMMEND-001 OPEN-9 해소** | 본 SPEC 전체 |
| 결정 5 | Haiku 4.5(경량) + Sonnet 5(상위). 프로필 델타 생성·consolidation = Sonnet, 캐시된 입력 ITPM 미차감(프롬프트 캐싱 권장) | REQ-PROF-020/031, §비기능 |
| 결정 6 (v0.3.0 갱신) | 임베딩 Google `gemini-embedding-001` 1536차원 — BaseStore 내장 semantic 인덱스가 소비. 카탈로그와 모델 공유, 인스턴스 별도 | REQ-PROF-074, §1.3 |
| 결정 8 | 비회원은 프로필 없음, AI 서버 무상태 — 게스트는 reader `None`, build 스킵 | REQ-PROF-003/041 |
| 결정 9 | 이벤트 기반 준실시간 동기화, 일 1회 보정 배치 패턴 — 주문 미러 채널의 근간 | §1.3, REQ-PROF-054 |
| 결정 10-A | 리뷰 분석 그래프가 (고도화) 작성자 취향 신호 공급 — 본 SPEC은 수신 계약 슬롯만 예약 | EX-P2, REQ-PROF-024 |
| 결정 12 / 이슈 #79 | 종료 트리거 소유권 분리 — **Spring=`logout` 하나만**(v0.5.0, 구 `newConversation` 제거), AI=프로필 버퍼 10분 비활동 flush, 탭 닫기 신호 없음 | EX-P4, REQ-PROF-051/056~059 |
| 결정 14-F | 구매 이력 미러는 추천 dedup과 공유. 미러 이벤트 채널 계약은 카탈로그/주문 이벤트 SPEC 소유, 본 SPEC은 read-only 소비 | EX-P5, REQ-PROF-054 |

---

## 5. 인터페이스 정의 (Interface Definitions)

### 5.1 `profile_summary` 섹션 레이아웃

`reader`가 반환하고 추천·재랭킹이 소비하는 하이브리드 단일 마크다운 문자열의 논리 구조. 실제 타입은 `str | None`이며(SPEC-RECOMMEND-001 State `profile_summary: str | None` 무개정), 아래는 그 문자열의 **내부 섹션 규약**이다. `decompose`와 `rerank`에 **동일한 문자열**이 주입된다(결정 16). 구조화 블록 필드는 decompose의 `source == derived` 필터 유일 원천(SPEC-RECOMMEND-001 REQ-REC-047 연계)이다.
> **[v0.6.0 #119 유예]** 단, MVP 는 `profile_injection_scope` 기본 `rerank_only` 로 `profile_summary` 를 **decompose 에 주입하지 않는다** — 프로필이 하드 필터로 새어 발화 의도를 누르는 실측 회귀(#119) 때문이다. 따라서 구조화 블록은 현재 **rerank 순서 신호 전용**이며, `derived` 필터 연계는 REQ-REC-047/041(0건 완화, 이슈 #113) 착수 시 함께 해제한다. 블록의 **내용 규약(FilterSet 매핑 속성 한정)은 그대로 유효**하다.

```
# (섹션 1) 구조화 블록 — FilterSet 매핑 가능 속성 한정
#   가격 성향(price disposition) / 선호·회피 브랜드 / 평점 성향 / Layer 2 속성
#   confidence 수치는 노출하지 않음(결정 16). 강도는 자연어로만.
# (섹션 2) 산문(prose) — rerank용 취향 서술. 자연어, confidence 미노출
# (섹션 3) 최근 맥락(recent context) — recency 윈도우 내 episodic 하이라이트 2~3개
```

논리 스키마(생성 측 계약을 명세하기 위한 표현이며, 소비 측은 문자열로만 취급):

```python
class ProfileSummarySections(BaseModel):
    """profile_summary 문자열의 논리 섹션 계약 (생성 측 검증용).
    최종 산출물은 이 세 섹션을 결합·압축한 단일 마크다운 str이다."""
    structured_block: StructuredPreferences  # 섹션 1 — FilterSet 매핑 속성만
    prose: str                               # 섹션 2 — 자연어 취향 서술
    recent_context: list[EpisodicHighlight]  # 섹션 3 — 최근 episodic 2~3개 (config)

class StructuredPreferences(BaseModel):
    # FilterSet(SPEC-RECOMMEND-001 §5.1) 매핑 가능 속성 한정 — 그 외 속성 금지
    price_disposition: str | None = None     # 가격 성향 (자연어, 예: "3~5만원대 선호")
    preferred_brands: list[str] = []
    avoided_brands: list[str] = []
    rating_disposition: str | None = None    # 평점 성향 (예: "4.5+ 위주")
    layer2_attributes: dict[str, Any] = {}   # Layer 2 속성 성향 (예: {"무선": true})
    # confidence 수치·내부 메타는 노출하지 않음 (결정 16)

class EpisodicHighlight(BaseModel):
    label: str                               # 최근 상황/구매 요지 (예: "지난주 유럽여행 준비")
    occurred_at: str                         # ISO-8601 (recency 윈도우 판정용)
    # episodic 하이라이트는 recency 윈도우 + salience로 선택 (§9 OPEN-P8 참조)
```

크기 상한: 문자 기반 기본 1,000자(`core/config.py` 주입, `summary.char_cap`). 집행은 **생성 측(consolidation) 압축 재작성**이며 소비 측 절단이 아니다(REQ-PROF-016). 게스트·신규 회원은 문자열 전체가 `None`(REQ-PROF-003).

### 5.2 델타 레코드 / 게이트 상태 스키마

`builder` 1단계가 생성하고 `gate`/2단계가 소비하는 후보 델타, 세션별 관심 분포 스냅샷, 그리고 코드가 관리하는 게이트 누적 상태.

```python
class ProfileDelta(BaseModel):
    """builder 1단계 산출 — 미처리 세션당 Sonnet 1회 호출로 생성된 후보 변경."""
    user_id: str
    thread_id: str                           # 원천 대화 스레드 (워터마크 대상)
    target_path: str                         # 대상 위키 파일 경로 (= Store item key)
    type: Literal["semantic", "episodic"]
    assertion: str                           # 자연어 사실 진술 (consolidation 입력)
    structured_attrs: dict[str, Any] = {}    # frontmatter 구조화 필드 후보
    source: Literal["conversation", "purchase", "review"]  # review는 고도화 예약(EX-P2)
    # LLM 태깅 (델타 생성 1회 호출 내 흡수, 추가 호출 없음 — 결정 16)
    explicitness: float                      # 명시성 (0.0~1.0). 구매 소스는 명시성 없음→낮음(§9 OPEN-P7)
    salience: float                          # 현저성 (0.0~1.0)
    transient: bool                          # 일시적 요청 여부 (true면 session_context 격리, 승격 배제)
    created_at: str                          # ISO-8601

class InterestDistributionSnapshot(BaseModel):
    """builder 1단계가 세션별로 남기는 관심 분포 스냅샷 (transient (b) 엔트로피 신호 입력)."""
    user_id: str
    session_id: str
    distribution: dict[str, float]           # 카테고리 → 관심 비중/빈도
    computed_at: str

class GateState(BaseModel):
    """코드가 sleep-time에 관리하는 승격 누적 상태 (LLM 아님)."""
    user_id: str
    preference_key: str                      # 승격 후보 선호 키 (예: "brand:소니")
    ema_confidence: float                    # 반복성 EMA 누적 (config α)
    observation_count: int
    last_salience: float
    promoted: bool = False
    valid_from: str | None = None            # 승격 시 부여 (결정 4-A 메타)
    last_confirmed: str | None = None        # 최근 재확인 시각
    superseded_by: str | None = None         # 폐기 대신 supersede 참조 (결정 4-A)
    updated_at: str
```

### 5.3 Store item 스키마 (PostgresStore / BaseStore)

위키 "파일" 1개 = Store item 1개. 네임스페이스는 `("profile" | "facts" | "episodes", user_id)`(결정 16). key = 위키 파일 경로, value = frontmatter 필드 + 마크다운 본문.

```python
class StoreItemValue(BaseModel):
    """PostgresStore item의 value 스키마. key(=위키 파일 경로)는 store가 관리."""
    # frontmatter 필드 (결정 4 포맷 + 결정 4-A 메타)
    type: Literal["semantic", "episodic"]    # 결정 4 (필수)
    tags: list[str] = []
    confidence: float | None = None          # 내부 메타 — profile_summary에는 미노출
                                             # (v0.7.0) 개인화 그래프는 3버킷 라벨만 노출 — 수치는 계속 미노출
    suppressed_at: str | None = None         # (v0.7.0, #149) 사용자 개별 삭제 tombstone 시각 — [v0.9.0] 원문은 이 시점에 이미 없다(즉시 물리 삭제). 필드명은 저장 모델 호환을 위해 유지
    graph_triples: list[dict] = []           # (v0.7.0, #149) 투영 원천 — 마크다운 파싱 금지(REQ-PROF-086)
                                             # (v0.7.1, #356) 이 fact 가 낳은 트리플이며, 식별자 확정은
                                             # builder 1단계(게이트 통과 직후 결정론적 resolver)에서 한다.
                                             # 정본 집계는 ("graph", user_id)/"v1" 문서이고 여기는 증거
                                             # 측 기록이다 — SPEC-PROFILE-GRAPH-149 §7.1 "fact 항목은
                                             # 증거 저장소로 유지하고 값에 필드만 더한다"
    derived_from_sensitive: bool = False     # (v0.7.0, #149) 민감 주제 파생 — 보존기간 만료 시 물리 삭제
    valid_from: str | None = None            # 결정 4-A (5)
    last_confirmed: str | None = None
    superseded_by: str | None = None
    structured_attrs: dict[str, Any] = {}    # 수치·구조 속성은 산문 대신 frontmatter (결정 4-A (4))
    # 본문
    body: str                                # 자연어 마크다운 (마이페이지 노출·임베딩 대상)

# 네임스페이스 규약 (결정 16)
#   ("profile",  user_id)  → index.md (압축 요약 진입점)
#   ("facts",    user_id)  → 지식 단위 semantic 파일 (budget.md, taste/fashion.md ...)
#   ("episodes", user_id)  → episodic 파일 (situations/travel-2026.md ...)
#
# 임베딩 인덱스 (결정 16, v0.3.0 갱신): BaseStore 내장 semantic 인덱스.
#   embed 대상 = fact 필드(1 fact = 1 item), 차원 = 1536 (결정 6, Google gemini-embedding-001).
#   embed 함수·차원은 core/config.py 주입 (하드코딩 금지). summary·session_ctx 는 index=False.
#
# 물리 배치 (결정 16/16-A): MVP는 단일 Postgres 인스턴스 내 별도 데이터베이스(catalog/profile).
#   profile DB에도 pgvector 확장 필요. 계정 분리(search read-only / 프로필 워커 profile 한정).
#   session_context(구매자 스레드 상태)는 profile 측 동거 확정(BaseStore, §9 OPEN-P9 해소, v0.3.0).
```

### 5.4 마이페이지 GET API 페이로드 스키마

```python
# GET /profile/{user_id}
# 마이페이지 표시용 — 자연어 마크다운 passthrough (결정 16, MVP는 GET only).
class ProfileViewResponse(BaseModel):
    user_id: str
    exists: bool                             # 프로필 존재 여부 (게스트·신규 회원 false)
    markdown: str | None                     # 사람이 읽는 프로필 마크다운. 미존재 시 None
    generated_at: str | None                 # 요약 생성 시각 (sleep-time consolidation 시각)
    # 주의: reader가 그래프 진입 시 반환하는 압축 profile_summary와 GET 노출 범위는
    #       서로 다를 수 있다(§9 OPEN-P10). GET은 사용자 투명성용(결정 4-A 보강 6),
    #       reader는 지연 크리티컬 압축 요약용.
```

---

## 6. 기능 요구사항 (Functional Requirements — EARS)

> 공통 규약(HARD): 모든 튜너블(요약 문자 상한, EMA α, 승격 임계, 엔트로피 임계, 최소 세션 수, recency 윈도우, 대화 보존 기간, sleep-time 배치 주기, 임베딩 차원 등)은 `core/config.py`에서 config 주입한다 — 하드코딩 금지(결정 16 고민 항목이 전부 config 주입으로 MVP 기본값 동작).

### 6.1 프로필 리더 (reader)

- **REQ-PROF-001** (Event-Driven): **When** 구매자 그래프가 진입(entry)하면, the `reader` **shall** 해당 `user_id`의 `profile_summary`를 PostgresStore에서 **단일 get 1회**로 동기 조회하여 반환한다.
- **REQ-PROF-002** (Ubiquitous): The `reader` **shall** 조회 시 LLM을 **호출하지 않는다**(요약 생성은 sleep-time 전용, REQ-PROF-035) — read 시점 LLM 호출 수는 0이며 지연 크리티컬 경로로 취급한다.
- **REQ-PROF-003** (State-Driven): **While** `user_id`가 부재(게스트)이거나 신규 회원이라 승격된 프로필이 없는 동안, the `reader` **shall** `profile_summary`를 `None`으로 반환한다 — 게스트와 신규 회원은 동일 경로이며(결정 16), 소비 측(SPEC-RECOMMEND-001 REQ-REC-006)이 개인화를 스킵한다.
- **REQ-PROF-004** (Ubiquitous): The `reader`가 반환하는 압축 요약과 마이페이지 `GET`(§6.9)이 반환하는 마크다운은 **노출 범위가 다를 수 있다** — `reader`는 §5.1의 압축 단일 문자열(지연 크리티컬)을, `GET`은 사용자 투명성용 사람이 읽는 마크다운을 반환한다. `reader`는 전체 번들을 로드하지 **않는다**(결정 4 읽기 정책, index.md + 압축 요약만).

### 6.2 `profile_summary` 생성 계약 (summary contract)

- **REQ-PROF-010** (Ubiquitous): The `profile_summary` **shall** 하이브리드 단일 마크다운 문자열이며 §5.1의 세 섹션(구조화 블록 / 산문 / 최근 맥락)을 포함한다.
- **REQ-PROF-011** (Ubiquitous): The 구조화 블록 **shall** FilterSet 매핑 가능 속성(가격 성향·선호/회피 브랜드·평점 성향·Layer 2 속성)만 담고, 그 외 속성을 담지 **않는다** — 이 블록은 decompose의 `source == derived` 필터 유일 원천이기 때문이다(SPEC-RECOMMEND-001 REQ-REC-047 연계). *(v0.6.0 #119: derived 필터 파생은 MVP 유예 — §5.1 유예 주석 참조. 블록의 내용 규약 자체는 rerank 입력 품질에 여전히 유효하다.)*
- **REQ-PROF-012** (Ubiquitous): The 산문 섹션 **shall** rerank용 취향 서술을 자연어로 담되, confidence 수치·내부 메타를 노출하지 **않는다**(강도는 자연어로만). **[스코프 한정 v0.7.0, 이슈 #149]** 이 금지는 **`profile_summary`(프롬프트 입력)** 대상이다. 개인화 그래프 표면(api-spec §3.8)은 **3버킷 라벨만** 노출하고 수치·버킷 경계는 와이어에 싣지 않으므로 수치 미노출 불변식은 유지된다 — 축소된 번복으로 기록한다.
- **REQ-PROF-013** (Ubiquitous): The 최근 맥락 섹션 **shall** recency 윈도우(config `summary.recency_window`) 내 episodic 하이라이트를 config 개수(기본 2~3개)로 담는다.
- **REQ-PROF-014** (Ubiquitous, v0.6.0 개정): The `reader`/생성기 **shall** 소비처별로 다른 요약을 생성하지 **않는다** — 단일 `profile_summary` 문자열만 만들고, **주입하는 소비처에는 그 동일한 문자열을 주입한다**(결정 16). 어느 소비처에 주입할지는 config(`profile_injection_scope`)가 정하며, 이는 요약을 다르게 **만드는** 것이 아니라 소비처를 **선택**하는 것이다(#119). *(금지 대상은 **요약의 분기** — 소비처마다 다른 요약 텍스트를 생성하는 것 — 이지 인코딩이 아니다. 단일 요약을 생성 시점에 한 번 임베딩해 벡터로도 보관하는 것(#148 `ProfileSummary.embedding`, 홈 추천 I-22 소비)은 같은 요약의 다른 인코딩이므로 본 요구에 부합한다.)*
- **REQ-PROF-015** (Ubiquitous): The `profile_summary` **shall** 승격 게이트를 통과했고(§6.5) `superseded_by`가 없는(미폐기) **그리고 사용자에 의해 억제되지 않은**(v0.7.0, 이슈 #149) fact만 반영한다(결정 16, 결정 14-B 항목 6 이행). *(단, 최근 맥락 섹션의 episodic 하이라이트가 반복성 게이트에 종속되는지 여부는 §9 OPEN-P8 참조 — 본 SPEC은 episodic 하이라이트를 recency+salience 선택으로 처리한다고 가정한다.)*
- **REQ-PROF-016** (Ubiquitous): The 요약 크기 상한 **shall** 문자 기반 config 값(`summary.char_cap`, 기본 1,000)으로 하며, 집행은 **생성 측(consolidation) 압축 재작성**으로 수행하고 소비 측 절단으로 처리하지 **않는다**(결정 16).
- **REQ-PROF-017** (State-Driven): **While** 신규 회원이라 승격 fact가 없는 동안, the 생성기 **shall** 요약을 억지로 생성하지 않고 `None`을 유지한다(콜드스타트 = 게스트와 동일, REQ-PROF-003).

### 6.3 빌더 1단계 — 델타 생성 (delta generation)

- **REQ-PROF-020** (Event-Driven): **When** sleep-time 배치가 미처리 세션(스레드)을 스캔하면, the `builder` 1단계 **shall** 세션당 Claude Sonnet 5(결정 5)를 **1회** 호출하여 후보 델타 목록(`ProfileDelta`)을 산출하고, 각 델타에 명시성/현저성/transient를 태깅한다 — 태깅은 이 델타 생성 호출 내에서 함께 수행하며 별도 LLM 호출을 두지 **않는다**(결정 16).
- **REQ-PROF-021** (Event-Driven): **When** 세션 델타를 생성하면, the `builder` 1단계 **shall** 그 세션의 관심 분포 스냅샷(`InterestDistributionSnapshot`)을 함께 저장하여 엔트로피 신호(§6.5)의 코드 계산 입력으로 남긴다.
- **REQ-PROF-022** (Ubiquitous): The `builder` 1단계 **shall** transient 신호 (a)(명시적 한정어·수혜자 전환)와 (c)(intent≠preference 라우팅)를 델타 생성 프롬프트 안에서 판정해 `ProfileDelta.transient`에 반영한다(결정 16 — LLM 흡수 신호).
- **REQ-PROF-023** (Unwanted): The `builder` **shall not** 턴 중(요청 경로)에 프로필 store에 write하지 않는다 — 관찰은 session_context 버퍼에만 누적하고, 델타 생성은 세션 종료 후(sleep-time 배치)에만 수행한다(결정 4-A 3, 2단 비동기).
- **REQ-PROF-024** (Optional): **Where** write 소스가 대화(`conversation`) 또는 구매(`purchase`)인 경우에 한하여, the `builder` **shall** 델타를 생성한다. 리뷰(`review`) 소스는 수신 계약 슬롯만 예약하며 MVP에서 델타 생성을 구현하지 **않는다**(EX-P2, 고도화 — 결정 10-A).
- **REQ-PROF-025** (Ubiquitous): The `builder` 1단계 **shall** 각 델타에 대상 위키 파일 경로(`target_path` = Store item key)와 `type`(semantic|episodic)을 부여한다 — 지속 취향/예산은 semantic, 최근 상황·구매는 episodic로 분류한다(결정 4 메모리 분할).
- **REQ-PROF-026** (Ubiquitous, v0.6.0 신규 #119): The 세션 버퍼 적재 **shall** (a) config 지정 intent(`profile_buffer_excluded_intents`, 기본 주문조회·장바구니 조회)의 발화를 배제하고, (b) 정규화(공백 접기·casefold) 후 동일한 발화를 `profile_buffer_repeat_cap`(기본 2, **최솟값 2**) 개까지만 적재한다. 반복 빈도의 **상한**은 결정론적 코드가 집행하며 델타 생성 LLM 의 자기보고(`repetitionEma`)에 위임하지 않는다 — 버퍼가 `"\n".join` 으로 통째로 델타 프롬프트에 실리므로 **반복 횟수가 그대로 취향 강도**가 되어, 같은 말 3~4회로 취향이 과대 대표된다(#119).
  > **상한을 1(완전 dedup)로 낮추지 않는 이유**: 승격 게이트가 `salience AND (explicit OR repeated)`(§6.5)라 **반복은 명시 표명 없이 승격시키는 독립 경로**다. 버퍼에 1건만 남기면 델타 LLM 이 반복을 관측할 수 없어 그 경로가 통째로 죽고, 세션 간 반복 누적(`GateState.ema_confidence`)은 미구현이라(§9 OPEN-P12) 승격하지 못한 델타는 버려질 뿐 다음 세션이 대신 살려주지도 않는다. 즉 본 요구는 **반복 신호를 없애는 것이 아니라 증폭을 자르는 것**이다.
  *(intent 판정 이후로 적재가 이동하므로 decompose 실패 턴의 발화는 버퍼에 쌓이지 않는다 — 의도를 파악하지 못한 발화는 취향 신호로도 쓰지 않는다.)*

### 6.4 빌더 2단계 — sleep-time consolidation

- **REQ-PROF-030** (Event-Driven): **When** sleep-time 배치가 실행되면, the `builder` 2단계 **shall** 여러 세션 델타를 위키에 병합·중복 제거·모순 해소하며, 맹목적 append·overwrite를 하지 **않는다**(결정 4 갱신 정책).
- **REQ-PROF-031** (Ubiquitous): The `builder` 2단계 **shall** 병합된 위키 파일의 **텍스트 통합(재작성)에만** LLM(Sonnet, 결정 5)을 사용하고, 신선도·승격 판단에는 LLM을 사용하지 **않는다**(결정 16 — "판단은 AI, 계산·검증은 코드").
- **REQ-PROF-032** (Ubiquitous): The `builder` 2단계 **shall** 반복성 EMA 누적, 승격 판정, recency-wins 충돌 해소, supersede 처리, 중복 병합을 **결정론적 코드**로 수행한다(재현 가능).
- **REQ-PROF-033** (Unwanted): The `builder` 2단계 **shall not** 최신성 판단을 LLM에 위임하지 않는다 — 최신성 충돌은 `valid_from`/`last_confirmed` 타임스탬프 비교의 결정론적 recency-wins 코드로 해소한다(결정 4-A 5, STALE arXiv:2605.06527 근거).
- **REQ-PROF-034** (Unwanted): The `builder` 2단계 **shall not** fact를 삭제하지 않는다 — 폐기 대신 `superseded_by`로 supersede하여 이력을 보존한다. 재확인되지 않은 선호는 EMA 감쇠를 적용한다(결정 4-A 5).
  - **[적용 범위 한정 v0.7.0, 이슈 #149] 이 금지는 기계(파이프라인) 경로를 구속한다.** 사용자가 명시적으로 요구한 삭제는 두 등급으로 나뉘며 이 금지의 대상이 아니다: (a) **개별 삭제 = 즉시 물리 삭제** — **[개정 v0.9.0, 이슈 #499]** 투영·요약·랭킹에서 즉시 제외하고 **원문도 그 자리에서 물리 삭제하며, 재승격 차단용 tombstone 만 영구히 남긴다.** v0.8.0(#322)은 그 사이에 undo 창(`graph_undo_window_s`, 기본 5분)을 두어 복구를 허용했는데, **되돌리기 API(I-35·`M-14`)가 2026-08-07 정본에서 폐기되면서 그 창이 지킬 대상이 사라졌다** — 되돌릴 방법이 없는데 원문을 5분 보관하면 사용자가 지웠다고 믿는 문장을 서버가 계속 들고 있는 것뿐이다. (v0.7.0 은 원문을 무기한 보관하는 억제였다.) (b) **전체 초기화 = 물리 삭제** — 사용자 자신의 명시적 요청이며 기계 자동 처리가 아니므로 본 조항의 구속 대상이 아니고, 변경 감사 로그(api-spec §6.3 c)로 추적한다. 즉 **금지되는 것은 기계가 사용자 데이터를 조용히 지우는 것**이고 사용자 자신의 삭제권은 금지 대상이 아니다 — **[v0.9.0] (a)도 이제 유예 없는 즉시 집행이라 "지연 집행" 단서가 필요 없다.** 규격은 `SPEC-PROFILE-GRAPH-149` §6.3·§6.7.
  - **좁은 예외 1건** **[개정 v0.9.0, 이슈 #499 — v0.8.0(#322)이 2건으로 늘렸던 것을 되돌린다]**: 민감 주제에서 파생된 항목은 보존기간 경과 시 **기계가 물리 삭제해야 한다**(신규 SPEC REQ-PGRAPH-077). **이것이 기계 경로 하드 삭제가 의무인 유일한 지점이다.** v0.8.0 은 "사용자가 개별 삭제한 항목의 원문을 undo 창 만료 시 물리 삭제"(REQ-PGRAPH-025)를 둘째 예외로 등재했는데, **undo 창이 폐기되면서 그 삭제가 유예 없이 사용자 요청 시점에 일어나 애초에 기계 경로가 아니게 됐다**(위 (a)) — 예외가 *해소*된 것이 아니라 **분류가 바로잡힌 것**이다. 하드 삭제가 일어나는 지점을 한곳에서 셀 수 있도록 여기에 적는다.
- **REQ-PROF-035** (Event-Driven): **When** consolidation이 완료되면, the `builder` 2단계 **shall** `profile_summary`를 §6.2 계약(문자 상한 내 압축 재작성)에 따라 재생성한다 — 요약 재생성은 sleep-time에만 발생한다(결정 16).
- **REQ-PROF-036** (Ubiquitous): The `builder` 2단계 **shall** 승격 시 각 fact에 `valid_from`을, 재확인 시 `last_confirmed`를 갱신하여 감쇠·모순 해소 메타를 유지한다(결정 4-A 5).
- **REQ-PROF-037** (Ubiquitous): The `builder` 2단계 **shall** Spring I-20 조기 통지 또는 AI inactivity timeout이 공통 session finalizer를 호출할 때 실행한다. 별도 전역 sleep-time 주기나 `run_after_session_end` 이중 경로를 두지 않는다(결정 16, 이슈 #79).

### 6.5 게이트 (gate)

- **REQ-PROF-040** (Ubiquitous): The `gate` **shall** 승격을 3조건(반복성 EMA · 현저성 salience · 명시성)으로 판정하며, 구현을 **LLM 태깅**(명시성·현저성·transient — 델타 생성 1회 호출 내)과 **코드 계산**(EMA 누적·임계 비교·승격 확정·recency-wins — sleep-time)으로 분담한다(결정 16).
- **REQ-PROF-041** (State-Driven): **While** `user_id`가 게스트인 동안, the `gate`/`builder` **shall** 프로필 승격 및 build를 스킵한다(결정 8 — 비회원 프로필 없음, AI 서버 무상태).
- **REQ-PROF-042** (Unwanted): **If** 델타가 transient로 태깅되면, **then** the `gate` **shall** 해당 델타를 session_context에 격리하고 프로필 승격 후보에서 배제한다 — 세션 종료 시 폐기한다(결정 4-A 1).
- **REQ-PROF-043** (Optional): **Where** transient 신호 (b)(관심 분포 엔트로피 급증)를 판정하는 경우, the `gate` **shall** 세션별 분포 스냅샷(REQ-PROF-021)을 입력으로 코드가 엔트로피를 계산하되, 최소 세션 수(config `entropy.min_sessions`) 미달 시 이 신호를 **비활성**한다(이력 부족 시 노이즈 방지 가드, 결정 16).
- **REQ-PROF-044** (Event-Driven): **When** 델타 소스가 구매(`purchase`)이면, the `gate` **shall** 명시성 없이 반복성·현저성 중심으로 승격을 판정한다 — 구매는 행동 신호이므로 명시성 조건을 요구하지 않는다(결정 16). *(3조건이 strict AND인지 가중 앙상블인지의 정확한 의미론은 §9 OPEN-P7 — 본 SPEC은 명시성이 필수가 아닌 가중 신호라고 가정한다.)*
- **REQ-PROF-045** (Event-Driven): **When** 사용자가 명시적 "기억해" 명령을 발화하면, the `gate` **shall** 3조건 게이트를 우회하는 hot-path 예외로 처리한다(즉시 fact 기록은 §6.7) — 명시성 hot-path는 반복성·현저성 누적을 기다리지 않는다(결정 4-A 2).
- **REQ-PROF-046** (Ubiquitous): The `gate`의 모든 임계(EMA α, 승격 임계, 엔트로피 급증 임계, 최소 세션 수, salience 임계) **shall** `core/config.py`에서 config 주입한다 — 하드코딩 금지(결정 16 고민 항목).

### 6.6 트리거 · 스케줄 (triggers & scheduling)

- **REQ-PROF-050** (Ubiquitous): The 파이프라인 **shall** 델타 생성의 정합성 원천을 pg-profile에 영속 저장된 **회원 세션 버퍼**로 두며, Spring 통지 payload의 내용이나 전달 성공에 정합성을 의존하지 **않는다**.
- **REQ-PROF-051** (Event-Driven): **When** Spring이 `logout` 종료를 통지하면, the 파이프라인 **shall** 이를 best-effort 조기 트리거로 사용한다. I-20은 같은 `(userId, sessionId)` 재전송에 idempotent하며 `tabClose`·`inactivityTimeout`을 Spring wire 사유로 요구하지 않는다(api-spec §3.5). **[개정 v0.5.0 — api-spec v0.16.0]** 구 `newConversation`은 **제거** — session/thread 축 분리로 "새 대화"가 `threadId`만 갱신하고 세션을 유지하게 되어 그 사유 자체가 발화되지 않는다. `reason`은 enum 미강제라 수신 시 400은 아니며 관측용으로만 기록한다.
- **REQ-PROF-052** (Ubiquitous): The 파이프라인 **shall** Spring I-20과 AI 내부 timeout을 하나의 session finalizer 및 고정키 `PROCESSING` claim으로 직렬화한다. Spring I-20 성공은 processed event를 영구 `COMPLETED`로 확정하지만, 처리 중 activity generation 또는 claim 소유권이 바뀌면 terminal 완료를 중단한다. AI idle 성공은 같은 claim을 해제하는 checkpoint로 끝내 동일 sessionId의 후속 활동을 다시 처리할 수 있어야 한다. 실패·취소 시 버퍼를 보존하고 activity claim을 `ACTIVE`로 되돌려 재시도를 허용한다.
- **REQ-PROF-053** (Ubiquitous): The 파이프라인 **shall** 비활동 기준(기본 600초), sweep 주기(기본 60초), 한 번의 조회 상한, 최대 동시 finalizer 수와 claim lease를 config 주입값으로 운용한다. activity claim lease는 `ceil(batch size / max concurrency) × 2단계 LLM 최악 예산`보다 길어 모든 batch wave의 대기·처리를 포괄해야 한다.
- **REQ-PROF-054** (Event-Driven): **When** sleep-time 배치가 실행되면, the 파이프라인 **shall** 구매 이력 미러(주문 이벤트 → AI 경량 미러)를 read-only로 스캔하여 구매 소스 델타 후보를 생성한다 — 미러의 적재 계약·이벤트 채널은 본 SPEC 소관이 아니다(EX-P5, 결정 14-F와 미러 공유).
- **REQ-PROF-055** (Unwanted): The 세션 종료 통지 엔드포인트 **shall not** 통지 payload의 `reason`을 프로필 처리 분기나 정합성 원천으로 신뢰하지 않는다. 실제 처리 입력은 저장된 회원 세션 버퍼이며 `reason`은 관측용이다(REQ-PROF-050 연계).
- **REQ-PROF-056** (Event-Driven): **When** 인증·검증을 통과한 회원 사용자 발화를 `conversation_turns`에 저장하면, the 대화 저장소 **shall** 같은 PostgreSQL transaction에서 `(user_id, session_id)`의 `last_activity_at`을 **DB 서버 시각**으로 upsert하고 해당 고정키의 이전 `PROCESSING`/`COMPLETED` processed event를 삭제한다. 프로필 세션 버퍼 저장은 그 뒤 별도 저장소에서 수행한다. 새 활동은 activity를 `ACTIVE`로 재개하고 진행 중/완료된 이전 종료 generation을 무효화한다. 게스트·판매자·거부되거나 대화 저장에 실패한 요청은 활동을 갱신하지 않는다.
- **REQ-PROF-057** (Event-Driven): **When** 비활동 sweep이 실행되면, the scheduler **shall** `last_activity_at <= DB now - timeout`이며 `status='ACTIVE'` 또는 lease가 만료된 `PROCESSING` 후보만 `(status, last_activity_at)` 인덱스로 조회하여 config batch size까지 `PROCESSING`으로 원자 선점한다. `conversation_turns` 전체 집계(`MAX(created_at)`)나 active 전 행 full scan을 하지 않는다. 이 job의 등록·실행은 I-17 및 `GOOGLE_API_KEY` 구성 여부와 독립적이어야 한다.
- **REQ-PROF-058** (State-Driven): **While** timeout 후보를 처리하는 동안, the scheduler **shall** token+lease로 행을 원자 선점하고 finalizer 진입 직전 DB 활동 시각과 in-memory 활성 스트림을 재확인한다. 이미 활성인 스트림·재활동·다른 유효 claim이 있으면 이번 처리를 건너뛴다. scheduler는 실제 stream registry 슬롯을 acquire하지 않아 finalizer 처리 중 새 정상 채팅을 `409 STREAM_IN_PROGRESS`로 거절하지 않아야 한다. activity 완료/소유권 기록이 실패하면 성공이 아니라 `retryable`로 집계하고 claim을 해제한다. timeout coroutine은 FastAPI 메인 event loop에서 실행하여 loop-bound pg-profile store와 활성 스트림 레지스트리를 background thread/별도 loop에서 직접 접근하지 않는다.
- **REQ-PROF-059** (Unwanted): The scheduler **shall not** 자기 `/events/session-end` 엔드포인트를 HTTP 호출하거나 탭 닫기 전용 API를 만들지 않는다. 내부 timeout과 Spring 통지는 공통 finalizer 및 고정키 claim으로 경합을 안전하게 흡수한다. idle 성공은 claim을 해제하고 새 활동이 activity를 재개하게 하며, 실패·프로세스 crash 뒤에는 lease 만료 또는 claim 해제로 재시도할 수 있어야 한다.

### 6.7 hot-path "기억해"

- **REQ-PROF-060** (Event-Driven): **When** 사용자가 명시적 "기억해" 명령을 발화하면, the 파이프라인 **shall** 해당 fact를 `manage_memory_tool` 경로로 store에 **즉시 기록**한다(결정 4-A 2 / 결정 16 hot-path).
- **REQ-PROF-061** (Unwanted): The hot-path "기억해" **shall not** 턴 중에 `profile_summary`를 재생성하지 않는다 — 요약 반영은 sleep-time 원칙을 유지하며, 같은 세션 내 즉시 효과는 추천 서브그래프의 멀티턴 filters 병합(SPEC-RECOMMEND-001 §6.7, 본 SPEC 비범위)이 커버한다(결정 16, EX-P7).
- **REQ-PROF-062** (Ubiquitous): The hot-path 기록 fact **shall** 통상 fact와 동일한 frontmatter 메타(valid_from/last_confirmed/type/confidence)를 부여받아, 이후 sleep-time consolidation에서 recency-wins·supersede 처리에 정상 참여한다.

### 6.8 저장소 스키마 (storage)

- **REQ-PROF-070** (Ubiquitous): The 파이프라인 **shall** 프로필을 PostgresStore(LangGraph BaseStore)에 저장하며, 위키 "파일" 1개 = Store item 1개(key = 위키 파일 경로, value = frontmatter 필드 + 마크다운 본문)로 매핑한다(결정 16, §5.3).
- **REQ-PROF-071** (Ubiquitous): The 파이프라인 **shall** 네임스페이스를 `("profile" | "facts" | "episodes", user_id)`로 구성한다 — `profile`은 index.md 압축 요약, `facts`는 semantic 지식 단위, `episodes`는 episodic 파일(결정 16). **[v0.7.0, 이슈 #149]** 개인화 그래프 투영 문서를 위한 네임스페이스가 추가된다 — 사용자당 문서 1개이며, 다중 항목으로 쪼개지 않는 이유는 현행 인프라에 **다중 항목 원자성이 없기** 때문이다(`SPEC-PROFILE-GRAPH-149` §7.1). `facts`는 그대로 **증거 저장소**로 유지한다.
- **REQ-PROF-072** (Ubiquitous, 결정 16-A 개정): The 프로필 store **shall** 카탈로그 검색 인덱스와 **별도의 데이터베이스**를 사용한다 — MVP는 단일 Postgres 인스턴스 내 DB 분리(catalog/profile) + 계정 분리(`search_service`는 catalog read-only, 프로필 워커는 profile 한정)이며, cross-DB 조인에 의존하지 **않는다**. 물리 인스턴스 분리는 부하 격리가 실제 필요해질 때의 고도화 승격 경로다(연결 문자열 교체 수준).
- **REQ-PROF-073** (Ubiquitous, 결정 16-A 개정): The 프로필 데이터베이스 **shall** pgvector 확장을 갖춘다(BaseStore 내장 semantic 인덱스가 요구 — catalog/profile 두 DB 각각 `CREATE EXTENSION`).
- **REQ-PROF-074** (Ubiquitous, v0.3.0 갱신): The BaseStore 내장 semantic 인덱스 **shall** fact 항목(1 fact = 1 Store item)을 결정 6의 Google `gemini-embedding-001` 1536차원 모델로 임베딩하며, 임베딩 함수·차원은 config 주입한다(하드코딩 금지, `embedding_model_id`·`embedding_dim`) — 카탈로그와 모델은 공유하되 데이터베이스는 별도다(결정 16-A). summary·session_ctx 항목은 semantic 인덱스 대상이 아니다(`index=False`).
- **REQ-PROF-075** (Ubiquitous, v0.3.0 확정): session_context(구매자 스레드 상태 — ThreadFilter/Cart/Revert/session_ctx) **shall** 프로필 인스턴스(pg-profile)에 동거하는 BaseStore 로 구현한다(§9 OPEN-P9 해소) — write 소유는 구매자 그래프(app/agents/buyer/graph.py)가 그대로 가진다.
- **REQ-PROF-076** (Ubiquitous): The 각 Store item **shall** frontmatter에 `type`(필수), `confidence`, `valid_from`/`last_confirmed`/`superseded_by`, `structured_attrs`(수치·구조 속성)를 담는다(결정 4 + 결정 4-A 4/5).
- **REQ-PROF-077** (Ubiquitous): The pg-profile `profile_session_activity` 테이블 **shall** `(user_id BIGINT, session_id TEXT)`를 primary key로 하고 `last_activity_at TIMESTAMPTZ`, `status(ACTIVE|PROCESSING|COMPLETED)`, `claim_token`, `lease_expires_at`을 저장한다. `(status, last_activity_at)` indexed access path를 제공하고 모든 시간 비교는 PostgreSQL 서버 시각을 사용한다.

### 6.9 마이페이지 API (`GET /profile/{user_id}`)

- **REQ-PROF-080** (Event-Driven): **When** `GET /profile/{user_id}` 요청이 도착하면, the API **shall** 해당 사용자의 사람이 읽는 프로필 마크다운을 `ProfileViewResponse`로 반환한다(자연어 마크다운 passthrough — 결정 4-A 보강 6 "노출" 이행, 결정 16 MVP GET only).
- **REQ-PROF-081** (State-Driven): **While** 대상 `user_id`가 게스트이거나 프로필이 없는 동안, the API **shall** `exists = false`, `markdown = null`을 반환하고 오류를 발생시키지 **않는다**.
- **REQ-PROF-082** (Unwanted): The API **shall not** 마크다운 전문 수정(`PUT /profile/me`)을 제공하지 않는다 — 산문 전문 편집은 계속 배제한다(EX-P3, 결정 16). **[한정 v0.7.0, 이슈 #149]** 항목 단위 제어는 api-spec §3.9(별도 표면)로 제안되며 본 조항의 대상이 아니다.
- **REQ-PROF-083** (Event-driven): When 회원이 개인화 중지 상태이면, the `reader` **shall** `profile_summary`를 `None`으로 반환하고 요약 markdown 노출도 중단한다 — 랭킹 소비처(rerank 주입·홈 프로필 벡터)도 동일하게 프로필을 쓰지 않는다(신규 SPEC REQ-PGRAPH-051).
- **REQ-PROF-084** (Event-driven): When 회원이 개인화 중지 상태이면, the `builder` **shall** 세션 버퍼 적재·델타 생성·consolidation·요약 임베딩과 "기억해" hot-path 기록을 **모두 중단**한다. 단 공통 session finalizer는 버퍼 정리와 처리 완료 표시를 계속해 세션 라이프사이클이 멈추지 않게 한다(신규 SPEC REQ-PGRAPH-052/053). 중지 기간의 발화는 **소급 반영하지 않는다**(REQ-PGRAPH-056).
- **REQ-PROF-085** (Event-driven): When 사용자가 전체 초기화를 요청하면, the 파이프라인 **shall** fact·요약(마크다운 및 임베딩)·억제 표식·미처리 세션 버퍼·누적 게이트 상태 **와 대화 전사록(`conversation_turns` 중 해당 `user_id`의 행 전체)** 을 물리 삭제하고, **변경 감사 로그는 보존**한다(신규 SPEC REQ-PGRAPH-061/062). 초기화는 개인화 중지 상태를 변경하지 않는다.
  - **[개정 v0.8.0, 이슈 #322 — v0.7.0 은 전사록도 보존 대상이었다]** 근거는 REQ-PROF-034가 이미 채택한 논거의 연장이다 — 금지 대상은 *기계가 조용히 지우는 것*이고 사용자 자신의 삭제권은 별개다. "전사록을 보존한다"의 대상은 기계·운영이지 사용자의 명시적 초기화 요청까지 붙잡으라는 뜻이 아니다(적용 범위 한정이지 예외 신설이 아니다). 대화 기록은 `pg-profile`에만 있고 Spring에 사본이 없어 **AI 단독으로 완결된다**(#322 선결 확인).
  - **세션 종료(§I-20/D6)는 전사록을 지우지 않는다** — 로그아웃·비활동 종료는 사용자의 삭제 요청이 아니다. 두 경로를 혼동하지 않는다.
- **REQ-PROF-086** (Ubiquitous): The `builder` 2단계 **shall** 요약 마크다운과 함께 **기계 판독용 구조화 트리플**을 산출해 저장한다 — 이것이 개인화 그래프 투영의 유일한 원천이며 마크다운 본문 파싱은 금지된다(신규 SPEC REQ-PGRAPH-001). §9 OPEN-P12의 선결과제가 이 조항으로 확정 방향이 된다. **[보강 v0.8.1, 이슈 #356]** 단계 분담을 명확히 한다: 트리플의 **식별자 확정**(`node_id`·`edge_key`·`edge_id`)은 **1단계**에서 게이트 통과 직후 결정론적 resolver가 수행하고, **2단계**는 확정된 트리플을 병합해 `("graph", user_id)` 문서로 산출한다. 배치마다 재-resolve 하면 거리 임계·통제 어휘가 바뀔 때 **같은 fact 가 다른 `node_id` 로 붙어 tombstone 을 우회**하므로, 결정론을 기능 요구로 규정한 신규 SPEC REQ-PGRAPH-010과 충돌한다.

### 6.10 오류 처리 관련 요구 (see §7)

- **REQ-PROF-090** (Unwanted): **If** `builder` 1단계 델타 생성 LLM 호출이 실패(오류/타임아웃)하면, **then** the 파이프라인 **shall** 해당 세션 버퍼를 삭제하거나 완료 처리하지 **않고**, MoAI 3회 재시도 원칙 하에서 재시도하며 델타를 날조하지 **않는다**.
- **REQ-PROF-091** (Unwanted): **If** `builder` 2단계 consolidation의 텍스트 통합 LLM 호출이 실패하면, **then** the 파이프라인 **shall** 기존 위키·기존 `profile_summary`를 보존(부분 갱신·손상 금지)하고 재시도하며, 실패한 병합을 다음 배치로 이월한다.
- **REQ-PROF-092** (Unwanted): **If** store(PostgresStore)가 불가용하면, **then** the `reader` **shall** `profile_summary`를 `None`으로 반환하여(추천은 게스트 경로로 정상 성립) 요청 경로를 막지 않고, `builder`는 write를 재시도 대상으로 이월한다 — 어느 경우에도 데이터를 날조하지 **않는다**.
- **REQ-PROF-093** (Unwanted): **If** 세션 활동 claim이 crash로 남으면, **then** the 파이프라인 **shall** lease 만료 후 보수적으로 재선점하며, 저장된 세션 버퍼를 조용히 건너뛰거나 삭제하지 **않는다**.
- **REQ-PROF-094** (Ubiquitous): The 모든 오류 처리 **shall** 노드별 재시도를 MoAI constitution의 최대 3회/작업 원칙 하에서 수행하고, 실패 시에도 프로필·요약·세션 버퍼·activity claim을 복구 가능한 상태로 유지한다(fail-safe).

---

## 7. 오류 처리 (Error Handling)

| 실패 지점 | 감지 | 처리 | 안전 불변식 |
|---|---|---|---|
| 델타 생성 실패 (1단계 Sonnet 오류/타임아웃) | LLM 호출 예외 | 최대 3회 재시도, 세션 버퍼 보존(다음 sweep/통지에서 재처리) | 델타 날조 금지, 저장 발화 유실 금지 |
| consolidation 텍스트 통합 실패 (2단계 Sonnet) | LLM 호출 예외 | 기존 위키·기존 요약 보존, 실패 병합 다음 배치 이월 | 부분 갱신·프로필 손상 금지 |
| store read 불가용 (reader) | store get 예외 | `profile_summary = None` 반환(추천 게스트 경로) | 요청 경로 블로킹 금지, `None` 외 값 날조 금지 |
| store write 불가용 (builder) | store put 예외 | write 재시도 이월(다음 배치) | 세션 버퍼·activity claim 복구 가능, 데이터 손실 금지 |
| 비활동 finalizer 실패·crash | 예외 또는 claim lease 만료 | 버퍼 보존, claim 해제 또는 lease 만료 후 재선점 | 실패를 `COMPLETED`로 확정하지 않음, 세션 버퍼 유실 금지 |
| 세션 활동 claim 고착 | lease 만료 감지 | 보수적 재선점(고정키 claim이 동시 실행 직렬화) | 저장 버퍼의 조용한 건너뜀 금지 |
| 임베딩 인덱스 실패 (BaseStore semantic) | embed/index 예외 | 텍스트 검색·링크 그래프로 degrade, 인덱스 재구축 이월 | 위키 본문 데이터 손상 금지 |
| `GET /profile/{user_id}` 미존재 사용자 | store 조회 결과 없음 | `exists = false`, `markdown = null` | 오류(4xx/5xx) 아닌 정상 응답 |

- 재시도 정책은 MoAI constitution의 최대 3회/작업 원칙 하에서 노드별 재시도를 기본으로 한다(구체 백오프 값은 구현 결정).
- 프로필·요약·세션 버퍼·activity claim은 어떤 실패에서도 손상되지 않고 재시도 가능한 상태(fail-safe)로 유지되어야 하며, 데이터 날조(존재하지 않는 fact·요약 생성)는 금지한다.

---

## 8. 인수 기준 (Acceptance Criteria)

모든 기준은 관찰 가능/테스트 가능해야 한다. Given-When-Then 형식.

- **AC-PROF-01 (리더 해피패스)**: **Given** 승격된 프로필이 있는 회원 `user_id`, **When** 그래프가 진입하면, **Then** `reader`는 store 단일 get **1회**로 `profile_summary`(str)를 반환하고, 이 과정에서 LLM 호출 수는 0이다(REQ-PROF-001/002).
- **AC-PROF-02 (게스트·신규 회원 None)**: **Given** `user_id` 부재(게스트) 또는 승격 fact가 없는 신규 회원, **When** 그래프가 진입하면, **Then** `reader`는 `profile_summary == None`을 반환하고 예외가 발생하지 않는다(REQ-PROF-003/017).
- **AC-PROF-03 (reader ≠ my-page 범위)**: **Given** 동일 회원, **When** `reader`가 그래프 진입 시 반환한 압축 요약과 `GET /profile/{user_id}`가 반환한 마크다운을 비교하면, **Then** 전자는 압축 단일 문자열(전체 번들 미로드)이고 후자는 사람이 읽는 마크다운으로, 노출 범위가 다를 수 있음이 관찰 가능하다(REQ-PROF-004).
- **AC-PROF-04 (요약 3섹션 구조)**: **Given** 승격 fact가 충분한 회원, **When** `profile_summary`가 생성되면, **Then** 구조화 블록·산문·최근 맥락 세 섹션이 모두 존재한다(REQ-PROF-010).
- **AC-PROF-05 (구조화 블록 FilterSet 한정)**: **Given** 프로필에 FilterSet 매핑 불가 속성(예: 자유 서술 취향)과 매핑 가능 속성(가격 성향·브랜드)이 섞여 있는 회원, **When** 요약이 생성되면, **Then** 구조화 블록에는 FilterSet 매핑 가능 속성만 나타나고 그 외 속성은 산문 섹션으로만 나타난다(REQ-PROF-011).
- **AC-PROF-06 (동일 문자열 주입)**: **Given** 임의의 추천 턴, **When** `decompose`와 `rerank`에 각각 `profile_summary`가 주입되면, **Then** 두 곳에 주입된 문자열은 **동일**하다(REQ-PROF-014).
- **AC-PROF-07 (문자 상한 생성 측 집행)**: **Given** 승격 fact가 많아 요약이 상한을 초과할 회원과 config `summary.char_cap = 1000`, **When** consolidation이 요약을 재생성하면, **Then** 결과 문자열 길이는 1,000자 이하이고, 초과분은 소비 측 절단이 아니라 생성 측 압축 재작성으로 처리된 것이 관찰 가능하다(REQ-PROF-016).
- **AC-PROF-08 (게이트 통과 미폐기 fact만)**: **Given** 승격되지 않은 후보 fact와 `superseded_by`가 설정된 폐기 fact가 함께 있는 회원, **When** 요약이 생성되면, **Then** 두 fact 모두 `profile_summary`에 나타나지 않는다(REQ-PROF-015).
- **AC-PROF-09 (세션당 델타 1회 호출 + 태깅)**: **Given** 미처리 세션 1개, **When** `builder` 1단계가 실행되면, **Then** 그 세션에 대한 Sonnet 호출은 1회이고, 산출 델타 각각에 explicitness/salience/transient 태그가 붙어 있으며, 태깅을 위한 추가 LLM 호출은 없다(REQ-PROF-020).
- **AC-PROF-10 (세션별 관심 분포 스냅샷)**: **Given** 델타가 생성된 세션, **When** 1단계가 완료되면, **Then** 해당 세션의 `InterestDistributionSnapshot`이 저장되어 엔트로피 신호의 입력으로 사용 가능하다(REQ-PROF-021).
- **AC-PROF-11 (턴 중 write 금지)**: **Given** 진행 중인 대화 턴, **When** 사용자 발화가 처리되면(비 "기억해"), **Then** 프로필 store에 대한 write는 발생하지 않고 관찰은 session_context에만 누적되며, 델타 생성은 세션 종료 후 배치에서만 발생한다(REQ-PROF-023).
- **AC-PROF-12 (EMA·승격·recency-wins 코드 결정론)**: **Given** 동일한 델타 집합, **When** consolidation을 두 번 실행하면, **Then** EMA 누적·승격 판정·recency-wins 결과가 동일하며(재현 가능), 이 판정에 LLM이 호출되지 않는다(REQ-PROF-032/033).
- **AC-PROF-13 (supersede not delete — 기계 경로 한정)**: **Given** 기존 fact와 그를 대체하는 최신 fact, **When** consolidation이 충돌을 해소하면, **Then** 구 fact는 삭제되지 않고 `superseded_by`가 설정되어 이력이 보존된다(REQ-PROF-034). **[v0.7.0]** 범위는 **기계(파이프라인) 경로**이며 사용자 개시 삭제는 AC-PROF-31/32가 다룬다.
- **AC-PROF-31 (사용자 개별 삭제 = 즉시 물리 삭제 + 영구 재파생 차단)** **[개정 v0.9.0, 이슈 #499]**: **Given** 승격된 fact를 가진 회원, **When** 사용자가 그 항목을 삭제하면, **Then** 그 항목은 투영·요약·랭킹에서 즉시 제외되고 **원문도 그 자리에서 물리 삭제되어 복구 경로가 존재하지 않으며**, **어느 시점에도 이후 배치가 같은 취향을 재파생해 다시 나타나게 하지 않는다**(REQ-PROF-015/034, 신규 SPEC REQ-PGRAPH-022/023/025/026/031).
- **AC-PROF-32 (전체 초기화 = 전사록 포함 물리 삭제, 감사만 잔존)** **[개정 v0.8.0, 이슈 #322]**: **Given** 프로필과 대화 전사록을 가진 회원, **When** 사용자가 전체 초기화를 요청하면, **Then** fact·요약·임베딩·억제 표식·미처리 버퍼 **와 해당 회원의 `conversation_turns` 행이 모두 물리 삭제**되고 **변경 감사 로그만 남으며**, 버전 값은 되돌아가지 않고 개인화 중지 상태도 바뀌지 않는다(REQ-PROF-085). **다른 회원의 전사록 행은 영향받지 않는다.**
- **AC-PROF-14 (구매 신호 명시성 없이 승격)**: **Given** 명시성 신호가 없는 구매 소스 델타가 반복성·현저성을 충족, **When** 게이트가 판정하면, **Then** 해당 선호가 승격된다 — 명시성 부재가 승격을 원천 차단하지 않는다(REQ-PROF-044).
- **AC-PROF-15 (엔트로피 최소 세션 가드)**: **Given** config `entropy.min_sessions` 미달의 사용자, **When** 게이트가 transient (b) 엔트로피 신호를 판정하면, **Then** 이 신호는 비활성 처리되어 오탐(노이즈)이 발생하지 않는다(REQ-PROF-043).
- **AC-PROF-16 (transient 격리)**: **Given** "이번엔 비싸도 돼" 같은 일시적 발화, **When** 델타가 transient로 태깅되면, **Then** 해당 델타는 session_context에만 남고 프로필 승격 후보에서 배제되어 장기 프로필로 전파되지 않는다(REQ-PROF-042).
- **AC-PROF-17 (통지 유실 회수)**: **Given** Spring 세션 종료 통지가 유실된 회원 세션, **When** 마지막 저장 발화로부터 10분 뒤 비활동 sweep이 실행되면, **Then** 저장된 세션 버퍼를 회수해 델타를 생성한다 — 통지 유실이 델타 유실로 이어지지 않는다(REQ-PROF-050/056~059).
- **AC-PROF-18 (세션 종료 통지 idempotent)**: **Given** 동일 세션 종료 통지가 2회 이상 도착, **When** 엔드포인트가 이를 처리하면, **Then** 델타·프로필이 중복 처리되지 않는다(REQ-PROF-051).
- **AC-PROF-19 ("기억해" 즉시 기록, 턴 중 요약 재생성 없음)**: **Given** 사용자의 "이거 기억해줘" 발화, **When** 처리되면, **Then** fact가 store에 즉시 기록되지만 `profile_summary`는 턴 중에 재생성되지 않으며, 요약 반영은 다음 세션 finalization에 일어난다(REQ-PROF-060/061).
- **AC-PROF-20 (Store item 구조)**: **Given** 승격된 semantic fact, **When** store에 반영되면, **Then** 해당 item의 key는 위키 파일 경로이고 value는 frontmatter 필드(type/valid_from/last_confirmed/superseded_by/confidence/structured_attrs) + 마크다운 본문이며, 네임스페이스는 `("facts", user_id)`이다(REQ-PROF-070/071/076).
- **AC-PROF-21 (별도 데이터베이스 + 계정 분리, 결정 16-A)**: **Given** docker compose 구성, **When** 서비스를 기동하면, **Then** 단일 Postgres 서비스 안에 catalog/profile **별도 데이터베이스**(각각 pgvector 확장)가 관찰되고, `search_service` 계정은 profile DB에 접근 권한이 없으며 프로필 워커 계정은 catalog DB에 쓰기 권한이 없다(REQ-PROF-072/073).
- **AC-PROF-22 (GET 마크다운 passthrough)**: **Given** 프로필이 있는 회원, **When** `GET /profile/{user_id}`가 호출되면, **Then** `exists == true`, `markdown`은 사람이 읽는 자연어 마크다운이고 PUT 경로는 제공되지 않는다(REQ-PROF-080/082).
- **AC-PROF-23 (GET 미존재 처리)**: **Given** 게스트 또는 프로필 미보유 `user_id`, **When** `GET /profile/{user_id}`가 호출되면, **Then** `exists == false`, `markdown == null`이고 오류가 아닌 정상 응답이다(REQ-PROF-081).
- **AC-PROF-24 (consolidation LLM 실패 안전)**: **Given** 2단계 텍스트 통합 LLM이 강제 실패하도록 주입된 상태, **When** 배치가 실행되면, **Then** 기존 위키·기존 `profile_summary`와 세션 버퍼가 보존되고(부분 손상 없음) 실패 병합이 다음 배치로 이월되며 완료 마킹되지 않는다(REQ-PROF-091, §7).
- **AC-PROF-25 (reader store 불가용 → None 폴백)**: **Given** store read가 강제 실패하도록 주입된 상태, **When** 그래프가 진입하면, **Then** `reader`는 `profile_summary == None`을 반환해 요청 경로를 막지 않고, 추천은 게스트 경로로 정상 성립한다(REQ-PROF-092).
- **AC-PROF-26 (10분 경계와 재활동)**: **Given** timeout=600초인 회원 세션, **When** 마지막 활동이 599초 전이면 대상이 아니고 600초 이상이면 대상이며, 처리 전 새 발화가 저장되면 **Then** 활동 시각이 갱신되어 이번 timeout finalization은 실행되지 않는다(REQ-PROF-056~058).
- **AC-PROF-27 (bounded indexed sweep)**: **Given** 대량의 세션 활동 행과 batch size=N, **When** sweep 1회가 실행되면, **Then** 최대 N개만 claim하고 `(status,last_activity_at)` 인덱스를 사용하는 후보 쿼리가 실행되며 `conversation_turns` 전체 집계는 발생하지 않는다(REQ-PROF-057).
- **AC-PROF-28 (timeout·I-20 경합 멱등)**: **Given** 동일 `(userId,sessionId)`에 AI timeout과 Spring `logout` 통지가 경합, **When** 둘이 동시에 finalizer에 진입하면, **Then** 델타·consolidation·버퍼 삭제는 논리적으로 한 번만 완료되고 한 경로는 `duplicate`로 수렴한다(REQ-PROF-052/059).
- **AC-PROF-29 (timeout 실패 복구)**: **Given** timeout finalizer가 델타/consolidation 중 실패하거나 프로세스가 crash, **When** claim이 해제되거나 lease가 만료된 뒤 다음 sweep이 실행되면, **Then** 보존된 버퍼가 재처리되고 실패 실행은 `COMPLETED`로 남지 않는다(REQ-PROF-052/058/059).
- **AC-PROF-30 (종료/checkpoint 후 같은 세션 재개)**: **Given** idle checkpoint 또는 Spring terminal 처리가 시작·완료된 sessionId, **When** 같은 sessionId의 새 회원 발화가 저장되거나 finalizer 처리 중 새 stream이 시작되면, **Then** 정상 채팅은 scheduler 때문에 409를 받지 않고 activity는 `ACTIVE`로 재개되며 이전 processed-event generation은 무효화된다. 처리 중 terminal finalizer는 새 activity를 `COMPLETED`로 덮지 않고, 새 버퍼는 다음 timeout/I-20에서 다시 처리된다(REQ-PROF-052/056/058/059).

### Definition of Done

- [ ] REQ-PROF-001~004, 010~017, 020~025, 030~037, 040~046, 050~059, 060~062, 070~077, 080~082, 090~094 전 항목이 테스트로 커버됨.
- [ ] AC-PROF-01~30 전 시나리오가 통과(pytest, integration은 docker compose 앱 + 단일 Postgres 서비스[catalog/profile 데이터베이스 2개, 각 pgvector] 구성 — 결정 16-A).
- [ ] `profile_summary` 섹션 레이아웃/델타 레코드/게이트 상태/Store item/GET API 스키마가 Pydantic 모델로 구현되고 스키마 계약 테스트 존재(`ProfileSummarySections`·`StructuredPreferences`·`ProfileDelta`·`GateState`·`StoreItemValue`·`ProfileViewResponse` 포함).
- [ ] 하드 불변식(reader LLM 0회·단일 get, 턴 중 write 금지, EMA/승격/recency-wins 코드 결정론, supersede-not-delete, 요약 문자 상한 생성 측 집행, 게이트 통과 미폐기 fact만 요약 반영, `decompose`·`rerank` 동일 문자열 주입) 회귀 테스트 존재.
- [ ] 게이트 분담(LLM 태깅 + 코드 계산, transient 3종 MVP, 구매 신호 명시성 없음, 엔트로피 최소 세션 가드, REQ-PROF-040~046, AC-PROF-14/15/16) 구현·테스트 존재.
- [ ] 저장된 세션 버퍼 기반 트리거(Spring I-20 best-effort·idempotent + AI 내부 inactivity scheduler, REQ-PROF-050~059, AC-PROF-17/18/26~29) 구현·테스트 존재 — 경계·재활동·경합·실패 복구 회귀 테스트 포함.
- [ ] 저장소(PostgresStore BaseStore·네임스페이스·카탈로그와 별도 데이터베이스 + 계정 분리[결정 16-A]·pgvector·BaseStore semantic 인덱스 + 결정 6 임베딩 모델·`profile_session_activity`, REQ-PROF-070~077, AC-PROF-20/21/27) 구현·테스트 존재.
- [ ] 마이페이지 `GET /profile/{user_id}`(마크다운 passthrough·게스트 처리, PUT 미제공, REQ-PROF-080~082, AC-PROF-22/23) 구현·테스트 존재.
- [ ] 오류 처리(델타/consolidation LLM 실패, store 불가용, claim 고착, 데이터 날조 금지, 3회 재시도, REQ-PROF-090~094, AC-PROF-24/25/29, §7) 회귀 테스트 존재 — 실패 시 프로필·요약·세션 버퍼 fail-safe 유지 검증 포함.
- [ ] 리뷰 신호 수신(EX-P2)·프로필 편집 PUT(EX-P3)은 MVP 비범위(고도화)임을 회귀 테스트에 반영(고도화 미구현 경계 — 수신 계약 슬롯만 예약).
- [ ] 모든 튜너블이 `core/config.py` 주입(하드코딩 금지)임을 검증하는 테스트 존재(요약 문자 상한·EMA α·승격 임계·엔트로피 임계·최소 세션 수·recency 윈도우·비활동 timeout/sweep/batch/concurrency/lease·임베딩 차원).
- [ ] §9의 미해결 항목이 후속 SPEC/이슈로 등록됨.

---

## 9. 미해결 / 후속 항목 (Open Questions & Follow-ups)

> **시점 관례** 🔴 — 아래 OPEN 항목은 MVP를 **막지 않는다**. 해당 기능은 **MVP에서 단순 기본값(config)으로 동작**하며, OPEN은 그 기본값의 **정밀 확정·튜닝(정량 목표·경계 재조정)만 MVP 이후**로 미룬 것이다. 스모크 검증은 SPEC-RECOMMEND-001 §6.12 평가 하니스(골든셋 + 유저 시뮬레이터)로 MVP 중에도 수행한다. 반면 "MVP 비구현" 기능은 §2 Exclusions(EX-*)에 별도로 명시한다(그쪽은 MVP에 동작 자체가 없음).

- **OPEN-P1 (요약 문자 상한 기본값)**: `summary.char_cap` 기본 1,000자가 세 섹션(구조화·산문·최근 맥락)을 담기에 적정한지는 데모 프로필 실측 후 조정(TBD). config 주입이므로 스키마 변경 없이 조정 가능(REQ-PROF-016, 결정 16).
- **OPEN-P2 (EMA α·승격 임계)**: 반복성 EMA α와 승격 confidence 임계의 정밀값은 골든셋/시뮬레이터 실측 후 확정(TBD). MVP는 config 기본값으로 동작(REQ-PROF-040/046, 결정 16).
- **OPEN-P3 (엔트로피 급증 임계·최소 세션 수)**: transient (b) 엔트로피 급증 임계와 `entropy.min_sessions` 가드값은 실측 후 확정(TBD). 이력 부족 시 노이즈 방지를 위해 MVP는 보수적 기본값(REQ-PROF-043, 결정 16).
- **OPEN-P4 (최근 맥락 recency 윈도우)**: 최근 맥락 섹션의 recency 윈도우와 하이라이트 개수(기본 2~3)는 실측 후 조정(TBD). config 주입(REQ-PROF-013, 결정 16).
- **[#321 해소] OPEN-P5 (대화 보존 기간)**: `conversation_turns`(완료 대화 전사록)의 보존 기간이
  `conversation_retention_days`(config 주입, 기본 **90일**)로 확정됐다. 값은 감사 원장
  `graph_audit_retention_days`(SPEC-PROFILE-GRAPH-149 §11, 기본 90일)와 **의도적으로 짝지었다**
  — 전사록이 감사 원장보다 먼저 지워지면 그 사이 구간의 감사 행이 가리키는 원문이 없어져 조사
  불가능해진다(감사 행은 지문만 남긴다, §6.3 c). 기동 시점 fail-fast 로 이 관계를 강제한다
  (`conversation_retention_days <= graph_audit_retention_days`). 삭제 주체는
  `app/pipelines/scheduler.py` 의 별도 job(`conversation_retention_sweep`, 기본 1시간 주기)이며
  유계 배치(`FOR UPDATE SKIP LOCKED`, 배치당 짧은 트랜잭션 1개)로 지운다. **"처리 전 세션 버퍼"는
  이 항목의 범위가 아니다** — 그쪽(`ProfileStore.append_session_ctx`)은 이미 별개 lifecycle
  (`profile_session_idle_timeout_s`/idle sweep, 세션 종료 시 consolidation 소비)로 관리되고
  있어 새 시간 기반 삭제 정책이 필요하지 않았다. 와이어 계약(엔드포인트·SSE 이벤트·필드·오류
  코드)은 불변이다 — `turns_for()`·`get_turn()` 의 프로덕션 호출부가 없어 전사록은 감사·상관관계
  조회 전용이다.
  - **[관계 명시 v0.8.0, 이슈 #322]** REQ-PROF-085(전체 초기화 시 전사록 삭제)와 **트리거가
    다르다**: 본 항목은 **시간 경과**로 지우는 보존 기간 정책이고, REQ-PROF-085는 **사용자의
    명시적 요청**으로 지운다. 서로 다른 사건이 같은 데이터를 지울 수 있을 뿐이며, 어느 한쪽이
    없어도 다른 한쪽은 성립한다. **[#321] TTL 자체도 이제 확정됐다** — 위 90일이 그 값이다.
- **OPEN-P6 (sleep-time consolidation 주기)**: consolidation 배치 주기와 "세션 종료 직후 실행" 옵션의 균형은 데모 차세션 반영 요구 실측 후 조정(TBD). **세션 비활동 판정값은 이 항목과 별개로 이슈 #79에서 기본 timeout 600초/sweep 60초로 확정**하며 config로 조정한다(REQ-PROF-037/053).
- **OPEN-P7 (3조건 게이트 AND vs 가중 앙상블 의미론)**: 결정 16은 구매 신호를 "명시성 없이 반복성·현저성 중심으로 판정"한다고 하나(REQ-PROF-044), 결정 4-A의 "3조건 게이트"가 3조건을 strict AND로 요구하는지 가중 앙상블(명시성은 기여 신호)인지 명시하지 않는다. 두 판독이 상충한다 — strict AND면 구매 신호가 명시성 부재로 **영원히 승격 불가**해 결정 16의 "구매도 write 소스" 의도와 모순되고, 가중 앙상블이면 "기억해" hot-path 예외(REQ-PROF-045)가 자연스럽다. 본 SPEC은 **가중 앙상블(명시성 필수 아님)** 을 가정하고 진행하나, 정확한 게이트 의미론과 가중치는 실측·확정 대상(TBD). 🔴 이는 판독 긴장이므로 상위 결정 계층에서 확인 필요.
- **OPEN-P8 (최근 맥락 episodic의 게이트 예외 경계)**: 결정 16은 요약이 "게이트 통과 미폐기 fact만" 반영한다고 하나(REQ-PROF-015), 동시에 최근 맥락 섹션은 recency 윈도우 내 **episodic 하이라이트 2~3개**를 담는다(REQ-PROF-013). episodic 하이라이트는 최근 단발 이벤트라 반복성(EMA) 조건을 구조적으로 충족하지 못한다 — "게이트 통과"(반복성 포함)와 "최근 episodic 포함"이 상충한다. 본 SPEC은 최근 맥락 섹션의 episodic 하이라이트를 **반복성 게이트가 아닌 recency 윈도우 + salience 선택**으로 처리한다고 가정하나, 이 예외의 정확한 경계(어떤 episodic이 요약에 오를 자격이 있는가)는 확정 대상(TBD). 🔴 판독 긴장, 상위 결정 계층 확인 필요.
- **[v0.3.0 해소] OPEN-P9 (session_context 소유·물리 배치 경계)**: 구매자 실행 모델이 실제 LangGraph StateGraph 가 아니라 단순 함수 호출 체인이라 "checkpointer"라는 메커니즘 자체를 적용할 수 없음이 이슈 #33 구현 중 확인됨 — 대신 BaseStore(app/core/pg_store.py, pg-profile 동거)로 구현했다. write 소유는 그대로 구매자 그래프(app/agents/buyer/graph.py) — 프로필 파이프라인은 read-only 소비만 한다(REQ-PROF-050/075 불변). 결정 16-A(단일 인스턴스)로 물리 결합 우려는 이미 소멸했었고, 이번에 스키마·구현 소유까지 확정됨.
- **OPEN-P10 (GET 마이페이지 노출 범위)**: 결정 16은 GET이 "자연어 마크다운"을 반환한다고만 하고 노출 범위(index.md 압축 요약만인지, 전체 지식 단위 번들을 조립한 마크다운인지)를 명시하지 않는다. reader(그래프 진입)는 압축 요약만 로드하나(결정 4 읽기 정책), 마이페이지는 사용자 투명성용(결정 4-A 6)이라 더 넓은 노출이 자연스럽다. 본 SPEC은 GET을 **사람이 읽는 프로필 마크다운(reader 압축 요약보다 넓을 수 있음)** 으로 가정하나, 정확한 조립 범위는 확정 대상(TBD). 🔴 기획 UX 확인 항목. **[부분 해소 v0.7.0, 이슈 #149]** "더 넓은 투명성 노출"의 요구는 **개인화 그래프 투영**(api-spec §3.8)이 답한다 — 항목 단위로 출처·근거 횟수·확신도 버킷까지 보여주므로 마크다운을 넓힐 필요가 없어졌고, 마크다운 GET은 현행(압축 요약 passthrough) 유지다. 마크다운 자체의 조립 범위는 계속 TBD.
- **[v0.3.0 해소] OPEN-P11 (임베딩 서빙 형태 공유 의존)**: 결정 6 이 이슈 #31 로 확정됨(Google `gemini-embedding-001` API, FastAPI 프로세스 내 동기 SDK 호출, `app.pipelines.embedding.embed_texts`) — 프로필도 동일 함수를 그대로 재사용한다(REQ-PROF-074). 별도 경량 임베딩 서비스 분리는 채택되지 않았다.
- **OPEN-P12 (게이트 누적 상태 미구현 — REQ-PROF-032/033/034/036 gap, v0.6.0 #119 등록)**: §5.2 `GateState`(`preference_key`·`ema_confidence`·`observation_count`·`valid_from`·`last_confirmed`·`superseded_by`)가 **구현되어 있지 않다**. 현재 fact store item 값은 `{"fact": str}` 단일 필드이고, dedup 은 완전 문자열 일치뿐이며, recency-wins 는 consolidation 프롬프트 문구("중복 병합, 최신 우선")로만 존재한다 — 즉 REQ-PROF-032("결정론적 코드")/033("LLM 위임 금지")를 **현행 구현이 위반**한다. #119 는 세션 버퍼 단계의 반복 통제(REQ-PROF-026)로 **부분 완화**했을 뿐, 세션을 넘는 빈도 편향과 supersede 이력 보존은 미해결이다. 선결과제: 자유형 한국어 fact 에서 안정적 `preference_key`("brand:소니")를 결정론적으로 도출하는 방법 — 이것이 없으면 EMA 를 누적할 키가 없다(§5.1 구조화 블록을 `consolidate` 가 JSON 으로도 산출하게 하는 방향이 후보). 명세를 코드에 맞춰 낮추지 않고 gap 으로 남긴다(TBD).
  - **[우선순위 상향 v0.7.0, 이슈 #149 — 해소 아님]** 개인화 그래프 계약(api-spec §3.8·§3.9)이 **결정론적 투영과 중복 불가**를 요구하므로, 자유형 fact 위에서는 그 계약이 **원리적으로 달성 불가**하다. 따라서 이 항목은 이슈 #150의 **선결 조건(blocking)** 으로 승격되고 위 후보 방향은 **확정 방향**이 된다 — REQ-PROF-086이 그것을 조항으로 못 박았다. 키 도출 방식은 `SPEC-PROFILE-GRAPH-149` §6.2가 소유한다(LLM은 타입 붙은 제안만 산출하고 키 확정은 kind별 결정론적 resolver가 수행 — LLM이 키를 직접 만들면 같은 발화에서 값이 흔들려 누적이 성립하지 않는다는 실측이 근거다). **남은 미확정은 거리 임계의 재측정**이며 신규 SPEC OPEN-G1로 이관한다.

---

## 비기능 요구사항 (Non-Functional Requirements)

> 하드 시간 추정을 두지 않는다. 지연은 상대적 예산/우선순위로 표현한다.

### 지연·경로 예산 가이드라인 (상대적)

- `reader`(store 단일 get, LLM 0회)는 **요청 경로에 포함되는 유일한 프로필 컴포넌트**이므로 지연 크리티컬 — 가장 가벼운 지연이어야 한다(최적화 우선순위 High). LLM·전체 번들 로드·다홉 링크 순회는 read 경로에서 금지(REQ-PROF-001/002).
- `builder` 1·2단계와 `gate`는 **요청 경로 밖**(sleep-time 배치)에서 실행되어 챗봇 응답 지연에 영향을 주지 않는다(결정 4-A 3, 2단 비동기).
- consolidation의 LLM 텍스트 통합(Sonnet)이 프로필 파이프라인의 지배적 비용원이나, 배치 처리라 응답 지연과 무관하다.

### 토큰/비용 가드레일

- **read 시점 LLM 0회**: reader는 LLM을 호출하지 않는다 — 요약은 sleep-time에 미리 계산(precompute)되어 store에 저장됨(Sleep-time Compute arXiv:2504.13171 근거, 결정 16).
- **델타 생성 세션당 1회**: builder 1단계는 미처리 세션당 Sonnet 1회 — 명시성·현저성·transient 태깅을 이 호출에 흡수하여 추가 호출을 두지 않는다(REQ-PROF-020, 결정 16).
- **consolidation LLM은 텍스트 통합 전용**: EMA·승격·recency-wins·dedup은 코드이며 LLM이 아니다(REQ-PROF-032, 결정 16).
- **모델 티어**: 델타 생성·consolidation = Sonnet 5(결정 5). 공유 시스템 프롬프트는 프롬프트 캐싱하여 ITPM 한도에서 제외(결정 5, 배치 부하 대비).
- **임베딩 비용**(v0.3.0 갱신): BaseStore semantic 인덱스는 Google `gemini-embedding-001` API(결정 6, 이슈 #31)를 호출하므로 토큰 비용이 발생한다 — fact 승격 시점에만 호출되어 빈도는 낮다(세션당 최대 수 건).
- **config 주입 기본값**: `summary.char_cap = 1000`, `summary.recency_window`·최근 맥락 개수, EMA α·승격 임계, `entropy.min_sessions`·엔트로피 급증 임계, 프로필 비활동 timeout(600초)·sweep(60초)·batch size(10)·concurrency(2)·claim lease(900초), 임베딩 차원(1536, v0.3.0 갱신)·embed 함수 — 전부 `core/config.py` 주입(하드코딩 금지, 결정 16/이슈 #79).

### 안전/일관성 불변식 (must-hold)

- reader는 LLM 0회·단일 get, 게스트·신규 회원 `None`(REQ-PROF-001/002/003, AC-PROF-01/02).
- 턴 중 프로필 write 금지, 델타 생성은 sleep-time 전용(REQ-PROF-023, AC-PROF-11).
- 최신성 충돌은 항상 코드 결정론적 recency-wins, LLM 최신성 판단 위임 금지(REQ-PROF-033, AC-PROF-12).
- **기계 경로의** fact 폐기 대신 supersede(이력 보존). **사용자 요청 삭제는 개별(즉시 물리 삭제 + 영구 tombstone)/전체 초기화(즉시 물리 삭제) 2등급이며 항상 감사된다** — 금지 대상은 기계가 조용히 지우는 것이다(REQ-PROF-034/085, AC-PROF-13/31/32, v0.7.0 이슈 #149, **개별 삭제 개정 v0.8.0 이슈 #322 → v0.9.0 이슈 #499**). 기계 경로 하드 삭제가 의무인 좁은 예외는 **1건**(민감 파생 보존기간 만료)이다 — undo 창 폐기로 개별 삭제가 기계 경로에서 빠졌다.
- 개인화 중지는 **사용과 수집을 동시에** 멈추고 데이터는 보존하며, 중지 중에도 조회·정리 동작은 허용된다(REQ-PROF-083/084).
- 개인화 그래프 투영의 원천은 **구조화 트리플**이며 요약 마크다운 파싱이 아니다(REQ-PROF-086).
- 요약 문자 상한은 생성 측 압축 재작성으로 집행, 소비 측 절단 아님(REQ-PROF-016, AC-PROF-07).
- 요약은 게이트 통과·미폐기 fact만 반영(REQ-PROF-015, AC-PROF-08 — 단 episodic 예외 §9 OPEN-P8).
- `decompose`·`rerank`에 동일 `profile_summary` 문자열 주입(REQ-PROF-014, AC-PROF-06).
- 델타 생성 정합성의 원천은 저장된 회원 세션 버퍼다. Spring 통지가 유실돼도 AI의 비활동 sweep이 회수한다. timeout과 명시적 종료의 경합은 공통 finalizer·고정키 claim으로 직렬화하며, idle은 재개 가능한 checkpoint이고 Spring I-20만 영구 멱등 완료한다(REQ-PROF-050~059, AC-PROF-17/28/30).
- 프로필 store는 카탈로그와 별도 데이터베이스 + 계정 분리, cross-DB 조인 금지 — MVP는 단일 인스턴스(결정 16-A)(REQ-PROF-072, AC-PROF-21).
- 어떤 실패에서도 프로필·요약·세션 버퍼·activity claim은 fail-safe 유지, 데이터 날조 금지(REQ-PROF-090~094, §7, AC-PROF-24/25/29).
