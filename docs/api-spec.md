# 자비스 AI 에이전트 서버 — API 명세서

> ✅ **정본(single source of truth)** — 이 파일이 계약의 정본이다(2026-07-22 정책 변경: 외부
> 사본 의존 폐기). 계약(엔드포인트·SSE 이벤트·필드·오류 코드)을 바꾸려면 **이 파일을 먼저
> 개정**한 뒤 코드를 고친다.

| 항목 | 값 |
|---|---|
| 문서 버전 | v0.28.3 |
| 작성일 | 2026-07-14 (v0.28.3 개정 2026-08-08 — **[#258] §4.6 `color` 사본 동기화 — string → string[](반복 파라미터, BE 부분 일치 OR 매칭).** BE 는 2026-08-03 LLM팀 실측 합의로 개정하고 2026-08-04 배포(`InternalProductController.search`, 머지 커밋 `1e0ce150`)를 마쳤는데 본 사본만 단수로 남아 있던 drift 정정이다(**신설 협의 아님** — v0.23.1·v0.20.4·v0.15.27 과 같은 유형). 3갈래 판정(미지정→무조건 통과, 색상축 없음→통과, 있으면 좁혀 비교)·부분 일치(`그레이`가 `다크그레이` 매칭)·정규화 주체(BE, trim+소문자화)·정규식 메타문자 이스케이프(BE)를 함께 등재. 동의어 확장은 여전히 **AI 소관**(#258)이며 BE 스키마 변경 없음. **와이어 계약은 이미 운영에 배포돼 있었고(2026-08-08 확인), 이 개정은 사본을 그 실측에 맞추는 것**이다. (v0.28.2 개정 2026-08-07 — **[#421·#416] §4.8 I-17 배치 복구 규약 후속 개정 — (1) 1선(콘텐츠 실패) 즉시 영구 격리를 `artifacts_batch_content_retry_cycles`(기본 1주기) cross-cycle 재시도 뒤 격리로 완화(JSON 파싱 실패는 LLM 샘플링 노이즈로도 나 정상 상품이 첫 주기에 오격리되는 것을 방지, 재시도 대기 큐는 상품 원본 필드를 담아 AI DB 미저장·프로세스 메모리 한정), (2) 2선·3선 연속 실패 스트릭을 프로세스 메모리에서 pg-catalog `batch_failure_state` 영속으로 전환(`artifacts_batch_failure_streak_ttl_s`, 기본 1시간, 안의 연속만 산정) — 스케줄러가 수렴 창(기본 3주기 ≈ 15분)보다 자주 재시작되면(연속 배포·크래시 루프) 스트릭이 매번 리셋돼 poison 상품이 dead-letter 상한에 영영 도달하지 못하던 결함 수정. AI측 소비 동작 서술 개정이며 **와이어 계약(요청·응답·오류 코드) 불변**) (v0.28.1 개정 2026-08-07 — **[#435] §3.1 지시어 해소 적용 범위(추천 카드는 `screen` 미포함) 서술 추가, 와이어 계약 불변**) (v0.28.0 개정 2026-08-07 — **[#439] §2.3 신원 discriminator XOR 폐지 — `sub_type` 을 모든 티켓의 필수 클레임으로, `role` 을 선택적 권한 클레임으로 재정의.** 종전 XOR 규약(`role`·`sub_type` 공존 시 `401 TOKEN_INVALID`, `exactly one identity discriminator is required`)은 **BE 가 실제로 발급하는 판매자 티켓을 전부 거부**하고 있었다 — `StreamTicketProvider` 실측과 **CH-6 정본(2026-07-18)** 상 발급 형식은 "`sub_type` 은 모든 티켓 공통, 판매자만 `role="seller"`·`brandId` 추가"이며 판매자 티켓은 `sub_type="member"` 를 항상 동반한다. 이것이 운영 `/seller/chat 401` 의 원인이다(#408 이 사유 로깅을 넣은 그 401 이며, 구매자 레인은 영향이 없었다). 개정 후 `role="seller"` + `sub_type="member"` 를 판매자로 수용하고, `sub_type` 이 없거나 `guest` 인 판매자 티켓과 `seller` 아닌 role 값은 `401` 로 남긴다. **`sub_type` 없는 판매자 티켓은 종전 허용 → `401` 로 강화되지만 CH-6 정본상 실존하지 않는 형식이라 와이어 영향이 0**이며, 허용을 유지하는 것보다 단순하고 더 fail-closed 다(미지 role 관용 부활을 구조적으로 차단). 🔴 구매자 티켓에는 `role` 을 싣지 않는다는 BE 확답(2026-08-07)에 따라 buyer role 값은 계약에 두지 않는다. 401 사유 문자열은 진단을 위해 `invalid sub_type claim` / `invalid seller role claim` 2종으로 정리했다(#408 로그 경로에 그대로 실린다). **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변** — 바뀌는 것은 티켓 클레임 수용 범위다.) (v0.27.1 개정 2026-08-06 — **[#325] §4.8 I-17 배치 복구 규약 개정 — ON_SALE 단건 실패 중 enrichment 내용 실패는 재시도 상한 후 dead-letter 기록·즉시 격리하고 커서 전진, 임베딩·스토어 실패·타임아웃 계열은 상품별 연속 실패 스트릭(주기 간 유지)이 `artifacts_batch_item_dead_letter_cycles`(기본 3주기 ≈ 15분) 도달 전엔 전파(자연 복구)하고 도달하면 격리(시간 유계). 표본·실패율 임계 초과(3선)는 같은 커서에서 연속 발동 횟수가 `artifacts_batch_page_failure_max_cycles`(기본 3주기) 도달 전엔 페이지 실패(커서 미전진), 도달하면 격리 후 전진(시간 유계). HIDDEN 삭제 실패·계약 위반만 종전대로 무기한 페이지 실패(커서 미전진). enrichment 출력 상한·reasoning effort 를 config 튜너블로 이관. 와이어 계약(요청·응답·오류 코드) 불변**) (v0.27.0 개정 2026-08-06 — **[#396] 구매자 `progress` 다회 emit + `stage` 어휘 확장(1종 → 7종, 개방형)** — `0~1회` → `0회 이상`, `analyzing`에 `mapping`·`expanding`·`searching`·`relaxing`·`reranking`·`publishing` 추가. FE가 모르는 `stage`를 무시하는 개방형 규약을 명문화해 이후 어휘 추가를 AI 단독 배포로 만든다. **기존 6종의 이름·페이로드·상대 순서 불변**(추가 전용) — `conditions`는 여전히 검색·자동 완화 뒤다. `progress`는 `token` 이후(`publishing`)에도 올 수 있다. **정본(Notion CH-2)은 2026-08-06 개정 완료**(0회 이상·개방형 7종 stage 표·덮어쓰기 표시 규약) — 본 사본은 그 동기화다.) (v0.26.3 개정 2026-08-06 — **[#310] I-18 응답에 `purchaseState` 등재(§4.9) — BE jarvis-backend#91 머지로 이미 와이어에 실려 오던 필드를 사본에 등재한 것이라 신설 협의가 아니다. §4.9·§4.16 에 "상태별로 갈라 안내한다"는 AI 동작 서술을 대칭으로 추가. 추가 전용 — 기존 엔드포인트·필드·오류 코드 계약 불변**) (v0.26.2 개정 2026-08-06 — **[#396] 구매자 `progress` 플래그 기본 on + 운영 기동 가드 제거 — 계약 등재(v0.21.0)·FE 구현 완료(2026-08-06)로 전제 충족. 와이어 계약(이벤트 이름·페이로드·필드·횟수·상대 순서) 불변, 실제 발신 개시**) (v0.26.1 개정 2026-08-06 — **[#367] §3.7 HOME 실패 모드 어휘 4종 규범화(현행 추인) + 실패 응답표 503/504 조건 드리프트 정정 — 와이어 계약(필드·`outcome`·오류 코드) 불변**) (v0.26.0 개정 2026-08-06 — **[#322] #149 계약 개정 — (1) 개별 삭제(§3.9.2)를 "즉시 억제 → undo 창 `graph_undo_window_s`(기본 5분) → 원문 물리 삭제, tombstone 만 잔존"으로 재정의(REQ-PGRAPH-032 pin 만료 없음과의 구분 명시), (2) 전체 초기화(§3.9.4) 범위에 **대화 전사록 `conversation_turns` 포함**(감사 로그만 보존, OPEN-G6 해소·C-23 (1)항 결정), (3) **§3.8 조회를 FE 직접 → Spring 프록시로 전환** — 마이페이지에서 `chat:stream` 티켓을 발급받을 수 없어(CH-1b 는 `sessionId` 필수) v0.22.0 의 레인 비대칭 [HARD] 전제가 무너졌다(§1.2·§2.3·§3 앵커·§8 항목9 동기화), (4) **I-번호 재채번 I-29~I-33 → I-32~I-37**(조회 I-32 합류로 6종 — I-29~I-31 은 판매자 주문·리뷰 #297 이 선점, C-26 경고가 실증), (5) `evidenceCount` **와이어 제거**(`profile_buffer_repeat_cap`=2 가 관측 횟수를 자르므로 정확한 수를 셀 수 없다 — 내부 필드는 유지), (6) §3.8 응답 `userId` string → **number(BIGINT)**·「타입 비대칭」 항목 삭제, (7) §3.9.1 `object`에 **`nodeId` 직접 지정** 허용(동시 지정 400), (8) **`error.detail` 공식화**(§2.5)·§3.9 `409` 의 `graphVersion` 을 봉투 밖 → `error.detail.graphVersion`(§4.1 관례로 통일). §3.8·§3.9 는 전 구간 🔴 제안(초안)·Post-MVP 로 구현이 없어 깨질 소비자가 없다. 그 외 엔드포인트·SSE 이벤트·필드 계약 불변**) (v0.25.0 개정 2026-08-05 — **[#297] 판매자 주문·리뷰 internal 3종 계약 등재(🔶 초안 — BE 협의 전): I-29 자사 주문 조회 §4.18 · I-30 발송 처리(HITL) §4.19 · I-31 리뷰 조회 §4.20 + S-4 `draft.op`에 `ship` 추가·`orderItemId` 필드(§3.2, 추가 전용 — 기존 op 3종 와이어 불변, 기존 `product` 레인 재사용·레인 신설 없음) + I-30 409 코드 `ALREADY_SHIPPED`→`ORDER_ALREADY_SHIPPED` 개명(공통 규약 형식) + ⚠️ §3.9(개인화 그래프)와의 I-29~I-31 번호 충돌 명시·§3.9 재채번(I-34~38) 제안(🔴 C-26)**) (v0.24.0 개정 2026-08-05 — **[#296] 판매자 분석 보고서 구조화 `report` SSE 이벤트 신설(§2.2·§3.2) — 기간·요약·findings·데이터 한계·차트·추천을 한 이벤트에 내장해 우측 패널 재료로 제공, `token` 산문(좌측 채팅·스레드 기록 원천)은 불변. 구 `chart` 이벤트(v0.20.0, #242)는 legacy 폐기(부활 없음) — FE 미구현 실증(useChat.ts 소비 케이스 부재)으로 소비자 없는 계약이라 dual-emit 없이 안전 대체. 이벤트 7종 유지(chart→report 교체), 그 외 이벤트 이름·페이로드 계약 불변**) (v0.23.1 개정 2026-08-05 — **[#162] I-3 `GET /internal/products/popular` 사본 등재(§4.17 신설) — 정본에는 있고 본 사본에만 누락돼 있던 것으로 신설 협의가 아니다. 레인 (c) 17→18건(§1.2). 와이어 계약 불변**) (v0.23.0 개정 2026-08-05 — **[#116·#117] I-24~I-28 계약 등재(정본 확정 2026-08-05) + CH-2 `action` 2종 → 10종 + §3.1 `cartItemId` 표기 정정(사본 드리프트) — 기존 엔드포인트·SSE 이벤트 이름·필드 계약 불변, 추가 전용**) (v0.22.0 개정 2026-08-05 — **[#149] 개인화 관계 Graph 계약 초안 등재 — 조회 §3.8(FE 직접, 기존 `chat:stream` 티켓 재사용) + 제어 5종 §3.9(Spring→AI internal, I-29~I-33), 🔴 제안(초안)·Post-MVP. 오류 코드 4종·감사 로그(§6.3 c) 신설, 기존 엔드포인트·SSE 이벤트·필드 계약 불변**) (v0.21.0 개정 2026-08-05 — **[#289] 구매자 `progress` SSE 이벤트 신설(정본 Notion CH-2 2026-08-05 합의·등재 반영) — §2.2·§3.1(본 사본 §3.1 번호는 6→7종, `progress`가 (1)번 — 정본은 `suggestions`를 목록에 포함해 8종으로 세지만 본 사본은 「MVP 추가 페이로드」 절에서 별도로 다뤄 번호가 하나 적다, 기존 6종 이름·페이로드·상대 순서 불변)·§2.9(c) I-1 원복 전제 갱신** — **AI 구현은 완료됐으나 `progress_events_enabled` 기본 false + 운영·스테이징 기동 가드로 잠겨 있어 실 와이어는 아직 종전 7종뿐**) (v0.20.4 개정 2026-08-05 — **[#118] 되물음(`PENDING_CART`) 예외 정본 반영 — 사본의 🔴 미반영 표시 해제(§3.1 [보안], 계약 동작 불변, 표시만 갱신)**) (v0.20.3 개정 2026-08-04 — **[#118] §3.1 v0.15.26 등재 계약인 `screen`(화면 맥락) 수신 구현 — 관대 유효성(400 없음)·담기 허용 목록 합집합·지시어 해소, §3.2 판매자 레인은 `pageType`·`filters`를 입력 메시지에만 주입** — **와이어 계약(엔드포인트·요청 필드·SSE 이벤트·오류 코드) 불변, 수신 구현**) (v0.20.2 개정 2026-08-04 — **[#277] §2.9(c) 미룬 턴 I-1 재시도 스킵 + 복구 가드 — 와이어 계약 불변**) (v0.20.1 개정 2026-08-04 — **[#278] §4.6 `categoryName` 정본 동기화 + §3.1 `conditionActions` 수신 구현 + I-1 `options`/`optionCount` 추가 전용 응답 계약**) (v0.20.0 개정 2026-08-03 — **[#242] 판매자 분석 파이프라인 v3.1: 브랜치 분석 검증(F1~F3+analysis_judge) 신설 + `chart` SSE 이벤트 신설(§2.2·§3.2, 이벤트 6→7종, 추가 전용)** — 기존 이벤트 5종 계약 불변) (v0.19.5 개정 2026-08-04 — **[#232] §4.7 결정 14-F 재구매 지목을 "현재 턴 한정"에서 "스레드 범위(유계 누적·매 턴 재검증)"로 정정 — 와이어 계약 불변**) (v0.19.4 개정 2026-08-03 — **[#32] 골든셋 실측으로 방식2 확정·방식1 및 C-17 기각 — 와이어 계약 불변**) (v0.19.3 개정 2026-08-03 — **[#138 후속] §2.9(c) 구매자 스트림 전체 상한 30s·판매자 90s 역할별 분리 — 와이어 계약 불변**) (v0.19.2 개정 2026-08-03 — **[#133] 구매자 degrade 고지(§3.3)·I-1 검색 재시도(§2.9 c) 명문화** — 와이어 계약 불변, BE 관측 포인트만 추가) (v0.19.1 개정 2026-08-02 — **[#197] I-16 이탈 코호트 응답 실측 확정(from/to 필수·cohortSize·members·churnRate=fraction) + I-8 실측 명문화(from/to 필수·groupBy 화이트리스트·rows), I-8 admin 소유 🔴 유지 — 협의 전 판매자 노출은 설정 플래그(기본 false)로 보류**) (v0.19.0 개정 2026-07-31 — **[#148] C-18: I-22 `catalogVersion`을 선택으로 완화하고 계약 폐기 제안** — 재현·캐시 어느 명분도 성립하지 않음) (v0.18.0 개정 2026-07-31 — **[#148] 홈 추천 계약 등재: I-22 §3.7(Spring→AI 위임, `outcome` 3종 전부 200, provenance 비노출) · P-5 §4.11(FE↔Spring 전제), 🔴 C-18 `catalogVersion` 주체 미해결**) (v0.17.4 개정 2026-07-31 — **[#196] I-13 계약 명문화 3건** — `eventType` CSV 직렬화 확정·rows 활동량 내림차순 정렬 명문화·purchaseComplete 미귀속(0 집계 가능) 경고, 근본 수정 jarvis-backend#62 연계) (v0.17.3 개정 2026-07-31 — **[#209 후속] 노출 상한 목록당 8→9(§3.3), 니즈별 추천(`PICK_ONE`×N)이 실제 발신 경로로 — §4.2 계약 자체는 불변**) (v0.17.2 개정 2026-07-31 — **[#114] 옵션 후보가 1개면 되묻지 않고 자동 선택해 담기** — §4.1 AI 동작·§3.1 되물음 서술 명확화, **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변**) (v0.17.1 개정 2026-07-31 — **[#209] I-21 다중 목록(`lists[]`) 정본 정합** — `recommendationRequestId`·`listType`·`totalBudget`·`label` 신설, 목록당 상품 9개, 멱등 키·400 조건 등재) (v0.17.0 개정 2026-07-31 — **[#187] signed `sessionId` 기반 stable `context_id`, guest→member claim, D6/I-20 lifecycle 계약 반영**) (v0.16.3 개정 2026-07-30 — **[#164] I-4 주문 상태 요약 계약·구매자 `order_status` 라우트 구현 정합**, §4.10 신설) (v0.16.2 개정 2026-07-30 — **[#194] I-14/I-15 응답 스키마 BE 실측 확정 + I-6 이상 감지 규칙 명문화**) (v0.16.1 개정 2026-07-30 — **I-21 `listId`를 UUID급 무작위(≥128bit)로 확정**, 순번·타임스탬프 등 추측 가능한 형식 금지) (v0.16.0 개정 2026-07-30 — **`sessionId`(접속)·`threadId`(방) 축 분리**: 동시 스트림 락을 방 단위로, I-20 사유 `logout` 1종, CH-1 멱등(D5), 맥락 TTL 접속 단위(D6)) (v0.15.27 개정 2026-07-30 — 사본 drift 정정: 담기 이벤트 적재 주체(BE→FE)·`budget` 이벤트 제외·`search.query` PII 기준) (v0.15.26 개정 2026-07-28 — 사본 동기화: §3.1 `conditionActions`(칩 제거, #84)·`screen`(화면 맥락, #118) 신설, `conditions` 칩 `field` 6종 확정, in-stream `error`에 `requestId`·`retryable` 추가) (v0.15.25 개정 2026-07-28 — #171: I-1 응답에 reviewCount 추가(AI 계산용·비표시), rating=0 의미 판별(리뷰 부재 vs 저평점). #100 "reviewCount 표시전용·미반환" 부분 개정. / v0.15.24 개정 2026-07-27 — 사본 동기화: S-5 폐기 반영, 상품 수정은 챗봇 HITL(I-11) 유일 경로) |
| 상태 | draft |
| 대상 독자 | Spring 백엔드 팀, React 프론트엔드(FE) 팀 |
| 소유 | AI 에이전트 서버 팀 |

> 본 문서는 **인터페이스 계약(interface contract)** 이다. 사용자 대면 엔드포인트의 동작·불변식은 소유 SPEC(`SPEC-RECOMMEND-001`, `SPEC-PROFILE-001`)에서 확정되며, 본 문서는 그 계약을 외부(Spring/FE) 관점에서 정리한다. 이벤트 채널(`/events/*`)의 HTTP 계약은 **본 문서가 단일 소스(single source of truth)** 로 소유한다(product.md 결정 21).
>
> **[v0.5.0 개정 — 2026-07-15 사용자 최종 확정]** 본 개정은 v0.4.0 Batch 2(카탈로그 미러 + 배치 동기화)를 **되돌려**, **후보 검색 = 질의 시점 Spring 위임(`POST /products/search`)** 을 **프로젝트 전 범위의 유일·영구 후보 확보 경로**로 확정한다. **[v0.5.1 정정 — 용어 확정]** 채택하지 않는 것은 **상품 원본 컬럼의 AI측 사본**(가격·재고·상품명 등 필터 컬럼 복제)이다. **AI 생성물 — extras(추론 태그)·search_doc·임베딩 벡터 — 은 AI Postgres에 저장·유지**하며(결정 3 Layer 2/3·결정 6 존속), 상품 변경 반영은 **AI가 요청하는 pull 배치**(§4.8)로 갱신한다. 이는 v0.4.0의 provenance 노트가 폐기했던 검색 위임 노선을 **최종 채택**하는 것이며, 이미 boot-verified 구현 스캐폴드(`~/projet/hk-final`, jarvis-ai, FastAPI+LangGraph)가 이 노선 위에 존재하고 사용자가 이를 구현 기준으로 비준했다.
> - **핵심 변경**: 후보 확보가 "AI 자기 검색 인덱스(미러)"에서 "질의 시점 Spring `POST /products/search` 위임"(신규 §4.6)으로 **영구 전환**된다. 상품 원본 컬럼의 사본(미러)은 두지 않는다. **[v0.5.1 정정]** AI 생성물(extras·search_doc·임베딩)은 유지하며 bulk export pull 배치(§4.8, C-4 부활)로 갱신한다. 질의 시점 후보 흐름에서 AI 임베딩과 Spring 검색의 결합 방식은 **OPEN**(§4.8 말미 — 두 방식 병행 검토).
> - **[이벤트 최종 — #187 개정]** `POST /events/session-end`(세션 종료)와 `POST /events/session-claim`(로그인 승격)을 **MVP에 유지**한다. **주문 알림(구 `POST /events/order`)·주문 미러는 채택하지 않는다** — 검색이 질의 시점 위임으로 확정되면서 구매 이력도 **추천 직전 질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 확보한다(결정 14-F 동작 요구 — exact 기본 제외와 명시적 재구매 지목 시 해제 포함 — 는 불변, 데이터 획득 방식만 교체). 재구매 지목은 자연어에서 추출해 AI 내부에서 처리하며 와이어 필드를 추가하지 않는다. **병행 PRD 초안 라인은 모든 이벤트를 고도화로 옮겼으나, 본 계약은 session-end 유지 한 지점에서 PRD와 갈라진다** — PRD의 events-scope를 **바로잡아야 하며**(§8 항목 6), 본 문서는 PRD를 조용히 따르지 않는다.
> - **Batch 1(판매자 확장)은 v0.4.0 그대로 유지**: `POST /seller/chat` = 통계 Q&A(원천 = Spring 집계 I-6 질의 시점 콜백, C-7 해소) + 상세 수정 draft 흐름(I-7 읽기 → LLM 개정안 → SSE `draft` → FE diff 카드 → FE가 Spring `S-3` PATCH로 반영, FE↔Spring 전제).
> - **[v0.6.0 개정 — 2026-07-15 사용자 확정, BE "챗봇 장바구니 담기(I-2)" 문서 채택]** 장바구니 계약을 BE 팀 I-2 문서 기준으로 재작성한다(§4.1) — **게스트 담기 허용**(02 D30, 결정 8 개정 필요 §8 항목 7), **`POST /internal/cart/items` + `X-Internal-Token` 서비스 토큰 + 본문 신원(userId/guestId, AI-검증 JWT `sub` 유래)**, **`optionId` 필수 옵션 되물음 멀티턴**(400 `CART_OPTION_REQUIRED` + options 목록 → LLM 재질문), 동일 상품·옵션 기존 존재 시 **Spring이 quantity 합산**. **장바구니 조회(§4.9, C-16 신설)** 추가 — "장바구니에 뭐 있어?" 질의 응답 + 담기 시 기존 보유 안내.
> - **[v0.7.0 개정 — 2026-07-15 사용자 확정, 스트림 운영 규약]** SSE 스트림 수명주기 규약 신설(§2.9) — **동시 스트림 제한(세션당 1개, `409 STREAM_IN_PROGRESS`)**, **취소 = 클라이언트 연결 종료**(FE `AbortController` → AI가 disconnect 감지 시 LLM 스트림 즉시 중단), **타임아웃 기준표**(first-token 10s / 스트림 상한 90s / AI→Spring 3s / LLM 30s+1재시도), **레이트 리밋 값·소유 확정**(FastAPI 미들웨어 + in-memory, 분당·시간당 상한 config). 대화 저장(COMPLETED/FAILED/CANCELLED)·로그/모니터링 필드는 운영 요구로 부록 §6.3에 등재.
>
> **[v0.3.0 명명 기준 — 유지]** FE/BE 팀의 챗 API 문서("추천 챗봇 CH-2")를 **명명 기준(naming baseline)** 으로 채택한다. 구매자 SSE 이벤트명은 `token`/`conditions`/`action`/`products.ready`/`done`/`error`를 쓰고(구 `text.delta`/`products` 폐기), 모든 페이로드 필드는 **camelCase**로 표기한다. 이 변경으로 `SPEC-RECOMMEND-001` §5.3과의 정렬이 깨지므로 해당 SPEC의 **동기화 개정(sync amendment)** 이 후속으로 필요하다(§7). 본 문서는 SPEC을 편집하지 않고 후속 항목으로만 등록한다.
>
> **[provenance — 노선 확정]** v0.4.0은 검색 위임 노선을 "미비준 병렬 초안"으로 폐기하고 미러+배치를 채택했으나, v0.5.0은 사용자 최종 확정으로 **검색 위임 노선을 유일·영구 비준 노선으로 채택**한다(구현 기준 = `~/projet/hk-final` 스캐폴드). v0.4.0 Batch 2(미러+배치·카탈로그 벡터 검색)는 **채택하지 않기로 확정**되어 문서에서 제거된다. 병행 PRD 라인 대비 유일하게 다른 점은 session-end가 MVP에 남는다는 것이다(주문 알림은 §4.7 질의 시점 조회로 대체). 상세는 §6.2 변경 이력 참고.
>
> **표기 규약**
> - 🔴 **협의 필요**: Spring/FE 팀과 계약 확정이 필요한 미해결 항목. 이 표시가 붙은 스키마는 본 문서에서 **제안(초안)** 으로만 제시한다.
> - **제안(초안)**: 어느 계약에서도 아직 확정하지 않은 형태(상관관계 키·목록 push 스키마·bulk export API·I-6/I-7 계약 등)를 본 문서가 초안으로 제안하는 것. 최종 확정 전까지 변경될 수 있다.
> - **확정안 반영**: 소유 SPEC 또는 팀 세션에서 확정된 결정을 본 문서에 반영한 것. Spring/FE 수용 전까지 🔴가 병기될 수 있다.

---

## 1. 개요 (Overview)

### 1.1 목적

자비스 AI 에이전트 서버가 제공/요구하는 HTTP API 표면을 Spring 백엔드 팀·React FE 팀과 공유하기 위한 계약 문서다. AI 서버는 자연어 상품 추천·프로필·판매자 통계/상세 수정 보조 응답을 담당하고, 커머스 트랜잭션(장바구니 저장·결제·회원)과 **상품 표시 UI(우측 상품 패널)·상품 원본 데이터**는 Spring이 소유한다.

### 1.2 호출 방향 원칙 (Call Direction)

FE가 사용자 대면 API에 대해 **AI 서버를 직접 호출**하고(결정 19), AI 서버는 후보 검색·구매 이력·주문 상태·장바구니·추천 목록·판매자 집계/이력·판매자 상품 CRUD를 위해 Spring을 역호출하며, AI 생성물 갱신은 Spring 변경분을 pull한다. Spring → AI 레인은 **`/events/session-end`와 로그인 승격용 `/events/session-claim`**(§3.5)에 더해 **홈 추천 랭킹 위임 I-22 `POST /internal/recommendations/home`**(§3.7)을 갖는다 — 주문 알림은 채택하지 않는다(§3.6·§4.7). 상품 원본 컬럼의 AI측 사본은 두지 않으며 후보 확보는 **질의 시점 I-1 `GET /internal/products/search`**(§4.6), AI 생성물 갱신은 I-17 pull 배치(§4.8)로 분리한다.

| 레인 | 방향 | 호출 | 인증 | 근거 |
|---|---|---|---|---|
| (a) 사용자 대면 | **FE → AI (직접)** | `POST /chat`, `POST /seller/chat`, `GET /profile/me` | 사용자 JWT (§2.3 a) | 결정 19 |
| (b) Spring → AI | **Spring → AI** | 이벤트 `POST /events/session-end`·`POST /events/session-claim`(§3.5) + **위임 호출 I-22 `POST /internal/recommendations/home`**(§3.7) + **개인화 그래프 조회·제어 위임 6종 I-32~I-37**(§3.8 조회 + §3.9 수정·삭제·복구·초기화·중지, 🔴 초안·Post-MVP) | 서비스 간 토큰 (§2.3 b) | 결정 12/16/21, #187, I-22(2026-07-28), #149·#322 |
| (c) 역방향 | **AI → Spring** | **18건** `{I-1,I-3,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}` — 후보 검색(§4.6), **인기 상품 후보(§4.17)**, 구매 이력(§4.7), 주문 상태 요약(§4.10), 장바구니 담기/조회(§4.1/§4.9), 추천 목록 push(§4.2), 판매자 집계·이력(§4.4), 판매자 상품 CRUD(§4.5), AI 생성물 변경분 pull(§4.8) | **전부 서비스 토큰(internal, `X-Internal-Token`)**. 사용자/판매자 스코프 신원은 AI가 검증 JWT 클레임에서만 도출 | 결정 7 / 경로 B / BE DB 정합 |
| (d) 전제 계약 | **FE → Spring** | 세션+스트림 티켓 발급(CH-1)·티켓 재발급(CH-1b)·판매자 세션(CH-6), 채팅 추천 목록 GET(CH-5, §4.3), **홈 추천 목록 GET(P-5, §4.11)**, (판매자 FE 직접 상품편집 — AI 표면 밖) | Spring 소관 | 결정 19 / 경로 B / v0.15.20 / P-5(2026-07-28) |

- 레인 (a): 사용자(회원·게스트·판매자)의 요청. 신원은 **토큰 클레임**에서 추출한다(§2.3, §2.6). AI는 사용자 요청 본문의 식별자를 신뢰하지 않는다.
- 레인 (b): Spring → AI는 **(1) 이벤트 2건** — 세션 종료 통지(`/events/session-end`)와 로그인 소유권 승격(`/events/session-claim`) — **과 (2) 위임 호출 1건 I-22**(홈 추천 랭킹, §3.7), **그리고 (3) 개인화 그래프 조회·제어 위임 6건 I-32~I-37**(§3.8·§3.9, 🔴 초안·Post-MVP)이다. (3)은 FE가 Spring의 마이페이지 엔드포인트(`M-11`~`M-16`, Spring 소유)를 호출하고 Spring이 자기 로그인 세션에서 도출한 `{userId}`로 AI에 위임하는 구조다 — **[개정 v0.26.0] 조회·변경 모두 이 레인이다.** v0.22.0은 조회만 레인 (a)에 두고 그 비대칭을 의도된 것으로 [HARD] 선언했는데, 마이페이지에는 채팅 세션이 없어 재사용할 `chat:stream` 티켓을 발급받을 수 없다는 것이 드러나 **전제가 무너졌다**(#322, §3.8·§3.9 preamble). §2.7의 `/events/*` 멱등 규약은 (2)·(3) 어디에도 적용되지 않는다. 주문 알림은 채택하지 않는다 — 구매 이력은 질의 시점 조회(§4.7)로 확보하며, 카탈로그 변경 이벤트도 존재하지 않는다(사본 없음). **[v0.18.0] 이 레인은 더 이상 "이벤트 채널"만이 아니다** — I-22는 통지가 아니라 응답 본문에 결과가 실려 오는 **동기 요청/응답**이고, 따라서 §2.7의 `/events/*` 멱등 규약이 적용되지 않는다(재시도 = 새 추천 실행).
- 레인 (c): AI → Spring 역방향은 **정확히 18건**이다 — `{I-1,I-3,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}`. 이름과 순서는 **후보 검색**, **인기 상품 후보 조회(I-3, §4.17)**, **구매 이력 조회**, **주문 상태 요약(I-4, §4.10)**, **장바구니 담기**, **장바구니 조회**, **추천 목록 push**, **매출 시계열**, **구매전환 퍼널**, **행동 이벤트 집계**, **주문 상태 전이/조회**, **상품 변경 이력**, **이탈 코호트**, **자사 상품 목록 조회**, **상품 등록**, **상품 수정**, **상품 삭제**, **AI 생성물 변경분 pull**이다. I-1/I-3/I-19/I-4/I-2/I-18/I-21과 판매자 API는 요청 시점 호출이고, I-17은 배치 pull이다. **[v0.22.1] I-3 는 신설이 아니라 정본에 있던 것의 사본 등재다**(#162).
- 레인 (d): FE ↔ Spring 전제 계약(Spring 소유). **[v0.18.0] P-5 `GET /api/products/recommended`(§4.11) 등재** — 홈 "OO님을 위한 추천". Spring이 이를 서빙하려고 내부에서 I-22(§3.7)를 호출하므로 **P-5 ↔ I-22가 서브 관계**다. 게스트는 P-5 대신 P-4(인기 상품)를 직접 호출한다. **[v0.15.20] BE 구현 실측으로 경로·응답 확정.** (1) **세션+스트림 티켓 발급(CH-1, `POST /api/chat/sessions`)** — 응답 `{sessionId, ttlSeconds, streamTicket, ticketTtlSeconds, llmSseUrl}`. 세션 TTL 10분 sliding, 티켓 TTL 60s(RS256). `llmSseUrl`은 FE가 AI 서버에 직결할 SSE 주소로, Spring이 내려준다. (2) **스트림 티켓 재발급(CH-1b, `POST /api/chat/tickets`)** — 요청 `{sessionId}`, 응답은 CH-1과 동일 DTO. 세션 유지한 채 새 티켓만 발급(2번째 메시지·`401` 시)하며 세션 TTL도 함께 갱신한다. **CH-1 재호출은 새 세션(맥락 단절)이라 티켓 재발급에 쓸 수 없다.** (3) **판매자 세션 발급(CH-6, `POST /api/chat/seller/sessions`)** — 판매자 챗 입구. `brandId`는 **BE가 JWT 검증 후 DB에서 도출해** 티켓 클레임에 박는다(클라이언트·LLM 주장 무시). (4) 추천 목록 GET(§4.3). (5) 판매자가 FE에서 직접 상품을 편집하는 경로(AI 표면 밖). ※ 구 "draft 적용 = FE가 S-3 PATCH"는 **폐기** — 채팅 경로 쓰기는 AI 직접(§3.2), `S-3`은 자사 상품 목록 조회(=I-9)다.

> **[HARD] 후보 확보 = 질의 시점 Spring 검색(v0.5.0, 유일·영구)**: 구매자 추천 후보는 **질의 시점에 Spring `POST /products/search`(§4.6)를 위임 호출**하여 확보한다. 상품 원본 컬럼의 AI측 사본은 두지 않는다. **[v0.5.1]** AI 생성물(extras·search_doc·임베딩)은 AI Postgres에 저장하며(§4.8), 질의 시점에 AI 임베딩과 Spring 검색을 어떻게 결합할지는 OPEN(§4.8 말미)이다. rerank(profile_summary 반영)는 여전히 AI 경계에서 수행한다.
>
> **[HARD] 표시 경로 = 경로 B(불변)**: 상품 목록은 SSE에 싣지 않는다. AI가 최종 랭크 목록을 Spring에 push(§4.2)하면 Spring이 표시 필드를 enrich하여 저장하고(§4.3), FE가 이를 GET한다. **표시 권위 = Spring**(결정 9-B, AI는 표시 필드 미보유). §4.6 검색 응답의 price는 rerank·예산 검증(AI-side)용이며 우측 패널 표시가는 여전히 경로 B로 채운다. 단방향 원칙의 AI→Spring 역방향 예외 증가는 product.md 신규 결정 레코드가 명문화한다(§8 항목 3).

### 1.3 MVP 범위 요약

MVP(개발 가동 목표 2026-07-19)에 포함되는 API 표면:

- **아키텍처**: 사용자 대면 API(`/chat`·`/seller/chat`·`GET /profile/me`)는 **FE → AI 직접 호출**(사용자 JWT), **후보 확보는 질의 시점 Spring 검색 위임(`POST /products/search`, §4.6)**, **상품 목록 표시는 경로 B**(AI → Spring push → FE ← Spring GET)로 분리된다(§1.2). 상품 원본 컬럼 사본은 없음, AI 생성물(extras·search_doc·임베딩)은 pull 배치로 유지(§4.8, v0.5.1 정정).
- **추천 agent** — `POST /chat`(SSE 스트리밍, 상품 추천 서브그래프 포함). 소유: `SPEC-RECOMMEND-001`. 후보는 §4.6 Spring 검색으로 확보, rerank는 AI-side. SSE 스트림은 상품 카드를 싣지 않고 `products.ready` 상관관계 키만 emit한다.
- **후보 검색 위임** — `POST /products/search`(§4.6, AI → Spring 질의 시점). decompose 산출 구조화 필터로 Spring 카탈로그를 검색하고, rerank·예산 검증에 필요한 후보 필드(price 포함)를 돌려받는다. **가장 중요한 신규 Spring 계약**.
- **장바구니 서브그래프** — `POST /chat` 내부 흐름. 실제 담기는 AI → Spring 장바구니 API 호출(I-2, §4.1, 단건 — 묶음은 반복 호출). **게스트도 담기 가능**(v0.6.0). 옵션 필수 상품은 `CART_OPTION_REQUIRED` 응답의 options 목록으로 **되물음 멀티턴**을 수행하고, 담기 전/질의 시 장바구니 **조회**(§4.9)로 기존 보유·수량 합산을 안내한다. 결과는 SSE `action` 이벤트로 반영.
- **프로필 조회** — `GET /profile/me`(마이페이지, 토큰 소유자 본인). 소유: `SPEC-PROFILE-001`.
- **판매자 agent** — `POST /seller/chat`. (a) **매출/판매 통계 Q&A**(원천 = Spring 집계 I-6 콜백, C-7 해소) **+ (b) 상세 수정 draft 흐름**(I-7 읽기 → `draft` 이벤트 → FE 반영). 리뷰 인사이트는 **비범위(MVP 제외)**.
- **이벤트 채널** — `POST /events/session-end`(세션 종료)와 `POST /events/session-claim`(guest→member 승격)을 유지. 주문 알림은 채택하지 않음 — 구매 이력은 **질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 대체(사용자 명시 결정 — 병행 PRD 라인과는 session-end 유지 지점에서 갈라짐, §8 항목 6).

> **[v0.22.0] MVP 범위 아님 — 개인화 그래프(§3.8·§3.9)**: 취향 그래프 조회와 사용자 제어 5종은 **Post-MVP**(milestone `Post-MVP Buyer Quality`)이며 위 목록에 포함되지 않는다. 계약은 이슈 #149, 구현은 #150이다. MVP 프로필 표면은 `GET /profile/me` 조회 하나뿐이다.

> **판매자 agent 범위(Batch 1)**: 판매자 agent는 원래 고도화(~7/31) 범위였으나 2026-07-14 세션에서 최소 범위(통계 Q&A)로 MVP에 편입되었고(product.md 결정 20), 2026-07-15 세션에서 **상세 수정 draft 흐름까지 MVP로 확대**되었다(§8 결정 20 개정 항목). 리뷰 인사이트(측면별 감성)는 계속 고도화.
>
> **[v0.5.0] 시맨틱 검색 caveat(정직 명시)**: Case 3(상황 기반) 추천 품질은 **Spring 검색(`POST /products/search`) + LLM decompose(쇼핑리스트 분해)** 로 달성한다. **AI 측 시맨틱(임베딩) 인덱스는 도입하지 않는다** — 따라서 상황 태그·의미 유사도 기반 검색은 Spring 카탈로그의 키워드/필터 검색 능력 한도 안에서만 동작한다. 이 caveat로 `SPEC-RECOMMEND-001`의 검색 도구(search-tool) 절이 개정 대상이 된다(§7).

---

## 2. 공통 규약 (Common Conventions)

### 2.1 Base URL

```
{AI_SERVER_BASE_URL}
```

- 배포 환경별 실제 값은 인프라 설정으로 주입한다(플레이스홀더). 예: `https://ai.jarvis.internal`.
- 모든 §3 경로는 이 base URL에 상대적이다.

### 2.2 명명 규약 (Naming Convention)

**[HARD] 본 문서의 모든 JSON 필드명·SSE 이벤트명은 FE/BE 팀 챗 API 문서("추천 챗봇 CH-2")를 명명 기준으로 채택한다.**

- **구매자 SSE 이벤트명**: `progress` / `token` / `conditions` / `action` / `products.ready` / `done` / `error`(§3.1, `progress`는 [확정 v0.21.0, 이슈 #289 / 다회 emit v0.27.0, 이슈 #396] **0회 이상**이며, 나가면 **첫 `progress`가 스트림 첫 프레임**). 구 v0.2.0/`SPEC-RECOMMEND-001` §5.3의 `text.delta`·`products`는 **폐기**한다.
- **판매자 SSE 이벤트명**: `meta` / `progress` / `token` / `draft` / `report` / `done` / `error`(§3.2, `report`는 [확정 v0.24.0, 이슈 #296] 분석 레인 전용·리포트 결과일 때 정확히 1회 — 차트 데이터 내장). 구 `chart`(v0.20.0, 이슈 #242)는 **legacy 폐기**(v0.24.0) — FE 미구현 실증으로 소비자 없는 계약이었다. `products.ready`·`conditions`·`suggestions`·`budget`·`action`은 판매자 스트림에서 **emit하지 않는다**.
- **필드명**: 모든 페이로드는 **camelCase**(`sessionId`·`threadId`·`productId`·`finishReason`·`relaxationNotice`·`verifiedSum`·`withinBudget`·`droppedItems`·`feasibilityNotice`·`cartItemId`…). 구 snake_case는 전 계약에서 폐기한다.
- **일관성**: `/events/*`(§3.5~3.6)와 `GET /profile/me`(§3.4)도 **camelCase로 통일**할 것을 제안한다(제안(초안)). `SPEC-PROFILE-001` §5.4의 `ProfileViewResponse` 필드 역시 camelCase 정렬 대상이다(§7 후속 개정).

> **SPEC 정렬 깨짐 명시 🔴**: 이 명명 채택으로 `SPEC-RECOMMEND-001` §5.3(snake_case + `text.delta`/`products` 이벤트)과 정렬이 깨진다. 의미론은 보존하되 이름만 바뀌므로, **SPEC §5.3의 동기화 개정**이 후속으로 필요하다(§7).

### 2.3 인증 (Authentication) — 확정안 반영(RS256/JWKS·401 규약), Spring 수용 전 🔴

인증은 **호출자 유형에 따라 2종**으로 나뉜다.

#### (a) 사용자 대면 API — 사용자 JWT (레인 a)

`POST /chat`, `POST /seller/chat`, `GET /profile/me` 에 적용한다.

```
Authorization: Bearer {STREAM_TICKET}   ← Spring이 스트림 단위로 발급한 단명 JWT (로그인 AT가 아님)
```

> **[개정 v0.26.0, #322] 개인화 그래프는 조회·변경 모두 이 레인이 아니라 레인 (b)다.** v0.22.0은 `GET /profile/me/graph`(§3.8)를 이 레인에 두고 기존 티켓(`scope == "chat:stream"`)을 재사용하기로 했으나, **마이페이지에는 채팅 세션이 없어 티켓을 발급받을 수 없다**(CH-1b는 `sessionId` 필수). 재사용할 자산이 없으므로 §3.8을 §3.9와 같은 레인으로 옮겼다 — 근거와 이력은 §3.9 preamble. 이 검증 경로(`scope` exact `chat:stream`)는 **여전히 바뀌지 않으며**, 전용 `profile:*` scope 신설안도 계속 기각이다(`/chat`·`/seller/chat`이 함께 지나가는 경로에 회귀 위험을 만든다). 🔴 C-20.
>
> **`GET /profile/me`(§3.4, 마크다운 조회)는 이 레인에 남는다** — 같은 티켓을 쓰지만 MVP로 이미 배포·동작 중이고, 그 경로의 인증 전환은 본 개정 범위 밖이다(별도 판단).

- **[개정 v0.10.0] SSE에 쓰는 토큰 = 스트림 단명 티켓** — 로그인 AT(전권 토큰)를 SSE에 직접 싣지 않는다. Spring이 **채팅 진입 시 신원을 확인하고 스트림 단위로 단명 JWT(RS256, TTL 30~60초)를 발급**하며, FE는 이 티켓으로 AI 서버에 SSE 연결한다. ("JWKS 검토 후 제안" 최종안 채택.)
  - **발급 흐름**: `FE → Spring`(회원=AT / 게스트=`guest_id` 쿠키) → `Spring`(신원 확인 후 스트림 티켓 발급, RS256) → `FE → AI`(티켓으로 SSE) → `AI`(JWKS 검증 후 스트리밍). **첫 티켓**은 **CH-1**(세션 발급, `POST /api/chat/sessions`) 응답에 얹어 추가 왕복이 없다(응답에 `sessionId` + `streamTicket`).
  - **[확정 v0.15.20] 티켓 재발급 경로 = CH-1b `POST /api/chat/tickets`** — 스트림 티켓 TTL(60초)이 세션 TTL(10분 sliding)보다 **훨씬 짧아**, CH-1 1회로는 첫 스트림만 커버된다. 2번째 메시지부터는 **세션을 유지한 채 티켓만 재발급**한다. BE 구현 실측: 요청 `{sessionId}`, 응답은 CH-1과 동일 DTO.
  - **[개정 v0.16.0 — D5] CH-1은 멱등이다. 구 "CH-1 재호출 = 새 세션(맥락 단절)" 경고를 폐기한다.** Spring이 세션 생성 시 Redis `SETNX`로 **"이 사용자의 세션이 이미 있으면 그것을 그대로 반환"** 하므로, CH-1을 몇 번 부르든 한 사용자에게는 세션이 하나다. 이 멱등이 **정확성의 책임자**다 — FE의 Web Locks(D1)는 한 브라우저 안에서만 통하므로 폰과 PC 동시 접속은 막지 못하고, 축출을 없앤 뒤에는 기록에서 밀린 세션이 CH-1b로 TTL을 계속 연장하며 **유령 세션으로 남아 I-20도 나가지 않는다**. Web Locks는 쓸데없는 CH-1 중복 호출을 줄이는 **최적화**이며 유지 여부는 FE 판단이다.
    - 다만 **CH-1b가 여전히 정규 경로**다 — 티켓만 갱신하는 편이 싸고, 세션 소유자 검증이 붙는다. CH-1 재호출은 이제 "틀린 것"이 아니라 "불필요한 것"이다.
    - **예외 — 게스트 첫 방문 멀티탭**: 쿠키가 아직 없는 상태에서 탭을 여러 개 동시에 열면 Spring이 게스트를 **두 명** 만든다. 쿠키는 하나만 남으므로 밀린 탭은 자기 세션의 주인과 쿠키가 달라져 **CH-1b에서 `403`** 을 맞는다(CH-1을 다시 부르면 복구). **신원 자체가 갈라지는 것이라 `SETNX`로 막을 수 없다** — Web Locks가 실질적으로 방어하는 유일한 케이스다.
    - 🔴 **BE 확인 필요**: `SETNX` 멱등 키의 스코프(회원 `sub` 단위인지 `sub_type`+`sub` 단위인지)와, 멱등 반환 시 세션 TTL을 sliding 갱신하는지 여부. 정본 SPEC-CHAT-SESSION §5 D5는 스코프를 "이 사용자"로만 적었다. **호출자 신원이 세션 소유자와 다르면 거부**하며(BE가 세션에 보관한 `sub_type`+`sub` 대조), 재발급과 함께 세션 TTL도 sliding 갱신한다. 판매자 세션은 보관된 `brandId`를 복원해 SELLER 스코프 티켓으로 재발급한다. Spring 소유 전제 계약(§1.2 레인 d).
  - **채택 이유**: (1) **게스트 커버** — 게스트는 로그인 AT가 없으므로 Spring이 `guest_id` 쿠키를 확인해 동일 경로로 티켓 발급(`sub_type: guest`). (2) **전권 AT 비노출** — SSE 쿼리스트링/헤더에는 30~60초짜리 읽기 전용 티켓만 나가 유출 시 피해가 "스트림 1회 연결"로 한정. (3) **aud 규율** — 로그인 AT는 Spring 전용, FastAPI용 `aud`는 티켓에만. (4) **발급 = 인증 관문** — 모든 SSE 연결이 스트림마다 Spring 신원 검증을 1회 통과.
- **[확정] 서명·검증 = RS256 + JWKS** — Spring이 **JWKS 엔드포인트**(`GET /.well-known/jwks.json`)를 노출하고, AI 서버가 JWKS 공개키를 **fetch·캐시하여 로컬 검증**한다(RS256, `kid`로 키 선택). **`kid` miss 시에만 refetch**하며, 요청마다 Spring에 왕복하지 않는다(FastAPI 기동 시 Spring이 잠깐 죽어 있어도 캐시로 동작).
- **[확정] 스트림 티켓 필수 클레임**:
  - `sub` — 사용자/판매자/게스트 식별자(숫자 id를 문자열로, §2.5·§2.6).
  - `sub_type` — `member` | `guest`. **[개정 v0.28.0, #439] 판매자 티켓을 포함한 모든 스트림
    티켓의 필수 클레임**이며 구매자 신원 유형(회원/게스트)의 유일한 정본이다
    (BE `StreamTicketProvider` 실측 · **CH-6 정본 2026-07-18** — "`sub_type` 은 모든 티켓 공통,
    판매자만 `role`·`brandId` 추가"). JWKS 모드에서 누락·그 외 값·legacy `role=GUEST|USER`·
    미지 role 대체는 모두 `401 TOKEN_INVALID`로 fail-closed 한다. dev 모드만 로컬 호환을 유지한다.
  - `iss` — 발급자 **`"jarvis-spring-auth"` [확정 v0.15.20]** (BE `StreamTicketProvider.ISSUER` 실측).
  - `aud` — 대상 **`"jarvis-fastapi-ai"` [확정 v0.15.20]** (BE `StreamTicketProvider.AUDIENCE` 실측). **AI는 `aud`를 검증**한다(토큰 혼용 방지 — 로그인 AT는 이 aud가 없어 SSE에 못 씀).
  - `scope` — **`"chat:stream"` [확정 v0.15.20]** (BE `StreamTicketProvider.SCOPE_CHAT_STREAM` 실측). AI는 이 **단일 문자열 exact 값**을 항상 검증한다. 누락·빈 값·다른 값·공백 구분 복합 문자열·배열·비문자 값은 모두 `401 TOKEN_INVALID`이며 설정 누락으로 검증을 끌 수 없다.
  - `exp` — 발급 후 **60초 [확정 v0.15.20]** (BE `app.stream-ticket.ttl-seconds: 60`. 구 "30~60초" 범위의 상단값). 완전 1회용은 아니며 짧은 TTL로 근사 — Redis는 Spring 전용 결정 유지, stateless 검증. CH-1/CH-1b 응답이 `ticketTtlSeconds`로 실값을 함께 반환한다.
  - **구매자(`/chat`)**: 위 공통 클레임에 서명된 **`sessionId`**를 추가한다. AI는 이 값을 요청 body의 `sessionId`와 대조하고, 누락·불일치하면 `403 SESSION_FORBIDDEN`으로 거부한다. **`threadId`는 body-only**다 — 한 접속 티켓으로 여러 탭/방을 동시에 열 수 있어야 하므로 티켓에 바인딩하지 않는다.
  - **판매자(`/seller/chat`)**: **`role == "seller"`(소문자) + `brandId`(JSON 정수) — [확정 v0.15.20]** (BE `StreamTicketProvider.buildTicket` 실측). seller `sub`는 양의 BIGINT 숫자 문자열, `brandId`는 bool을 제외한 JSON 정수 `1..2^63-1`만 허용한다. null/string/float/bool/list/object/범위 밖 값은 seller route나 Spring backend에 닿기 전에 `401 TOKEN_INVALID`다. 집계·CRUD 역호출(§4.4·§4.5)의 `{brandId}` path에 이 값을 쓴다. AI는 `brandId`를 **요청 본문에서 받지 않고 검증된 티켓 클레임에서만** 얻는다(userId와 동일 원칙 — IDOR 방지, 판매자가 남의 brandId로 조회 불가, §2.6). **판매자 티켓에는 구매자용 `sessionId` claim을 요구하지 않는다.**

> **[개정 v0.28.0, #439] 신원 클레임 규약 — XOR 폐지, `sub_type` 필수 + `role` 교차 검증.**
> 종전에는 `role`과 `sub_type`이 함께 실리면 값과 무관하게 `401`이었다(XOR). 그러나 BE
> `StreamTicketProvider` 실측과 **CH-6 정본(2026-07-18 확정)** 상 실제 발급 형식은
> "**`sub_type` 은 모든 티켓 공통, 판매자만 `role="seller"`·`brandId` 추가**"이며,
> 판매자 티켓은 **`sub_type="member"` 를 항상 동반**한다. 즉 XOR 규약은 실제로 발급되는 판매자
> 티켓을 전부 거부하고 있었다(운영 `/seller/chat` `401` 의 원인). 이제 **`sub_type` 은 모든 티켓의
> 필수 클레임**이고 **`role` 은 선택적 권한 클레임**이다 — 실려 있으면 정확히 `"seller"`여야 하고
> 그 티켓의 `sub_type` 은 반드시 `member`여야 한다.
>
> | `role` | `sub_type` | 판정 |
> |---|---|---|
> | 없음 | `member` | 구매자 회원 |
> | 없음 | `guest` | 게스트 |
> | `seller` | `member` | **판매자** — v0.28.0 신규 허용 |
> | `seller` | 없음 | `401 TOKEN_INVALID` — **v0.28.0 강화** (실존하지 않는 형식) |
> | `seller` | `guest` | `401 TOKEN_INVALID` — 판매자는 회원이어야 한다 |
> | `seller` 외 값 | (무관) | `401 TOKEN_INVALID` |
> | (무관) | 없음·그 외 값 | `401 TOKEN_INVALID` |
>
> - 값 판정은 **exact 문자열 일치**다. 키가 존재하는데 값이 비문자열(JSON `null`·수·bool·배열·객체)
>   이거나 빈/공백 문자열이면 "없음"이 아니라 **이상값으로 보아 `401`** 이다(빈 문자열 role 로
>   fail-closed 가드를 우회한 PR #39 리뷰 3R 회귀 방지).
> - **`role` 이 판정의 우선 축이다** — `role="seller"` + `sub_type="member"` 는 판매자다.
> - 🔴 **구매자 티켓에는 `role` 을 싣지 않는다** (BE 확답 2026-08-07 — 신설 계획 없음).
>   따라서 buyer role 값은 계약에 존재하지 않으며 `seller` 아닌 role 값은 전부 fail-closed 다.
> - **하위호환**: 실제 발급되는 티켓 3형태(회원·게스트·판매자 both-claims)는 모두 수용되고, 구매자
>   단일 discriminator 티켓의 동작은 전 케이스 불변이다. `sub_type` 없는 판매자 티켓만 허용 →
>   거부로 강화되는데 **그 형식은 CH-6 정본상 실존하지 않아 와이어 영향이 없다**.
> - `sub_type` 미지 값·seller `sub`/`brandId` BIGINT 검증·`sessionId` 규약·`403` 규약은 종전 유지.
> - dev 모드(`AUTH_MODE=dev`) 레거시 role 관용은 이 매트릭스의 대상이 아니며 무변경이다.

올바른 판매자 티켓도 구매자 `/chat`에서는 buyer state를 만들기 전에 `403 FORBIDDEN`으로 거부한다. AI는 신원을 **오직 토큰 클레임에서만** 추출한다(요청 본문 금지, §2.5·§3.1·§3.2).
  - 검증 항목: **signature / exp / iss / aud / scope**.
- **[확정] 401 통일 규약**: 토큰이 **없음/무효/만료**이면 AI 서버는 항상 **`401`** 을 반환한다.
  - `code == "TOKEN_EXPIRED"` — `exp` 경과.
  - `code == "TOKEN_INVALID"` — 서명 불일치·형식 오류·누락.
  - **FE 반응**: `401` 수신 → Spring **티켓 재발급 경로(CH-1b, §1.2 레인 d)** 에서 새 스트림 티켓 발급 → 새 티켓으로 원 요청을 **1회 재시도**(§2.5·§6.1). (티켓 TTL이 짧지만, 스트림 시작 전 만료 시의 재발급 흐름이며 — 이미 열린 스트림은 티켓 만료로 끊지 않는다, §2.5.)
- **[확정] `403` 규약**: `/seller/chat`는 `role == "seller"`를 요구한다. 판매자 스코프가 없는 토큰의 호출은 **`403 FORBIDDEN`**.
- **[폐기] `CHAT_SESSION_EXPIRED`(FE/BE 문서의 `400`) 폐기**: `sessionId`에는 만료 의미가 **없다**(§2.6). 인증 실패는 모두 `401`(TOKEN_*)로 통일한다.

#### (b) Spring → AI — 서비스 간 토큰 (레인 b)

`POST /events/session-end`와 `POST /events/session-claim`(Spring → AI, §3.5), **I-22 `POST /internal/recommendations/home`(§3.7)**, **그리고 개인화 그래프 조회·제어 6종 I-32~I-37(§3.8 조회 + §3.9 제어 5종, 🔴 초안·Post-MVP)** 에 적용한다(**[v0.26.0]** 조회가 레인 (a)에서 이 레인으로 합류해 5종 → 6종). (v0.5.0에서 주문 알림·카탈로그 배치는 채택하지 않으므로 해당 인증 항목은 없다.) I-22와 §3.8·§3.9의 토큰 불일치 오류 코드는 `INTERNAL_TOKEN_INVALID`(401)다.

```
X-Internal-Token: {SERVICE_TOKEN}
```

- **[v0.15.17 확정]** Spring PR #24와 AI 수신 구현이 사용하는 서비스 토큰 헤더는 `X-Internal-Token`이다. 발급·회전 주체, 만료 정책, mTLS 병용 여부는 🔴 협의(§5 C-1).
- 사용자 JWT와 **별개의 자격 증명**이다 — 이벤트 채널은 사용자 신원이 아니라 서비스 신원을 검증한다.
- **[개정 v0.13.0] AI → Spring 역호출은 전 구간 동일 레인** — BE DB 실측(`internal` 그룹 전부 `서비스 토큰`)에 맞춰 **`X-Internal-Token` 서비스 토큰 + 본문/쿼리에 신원**(AI가 검증한 JWT `sub`에서 도출)으로 통일한다. 구 "사용자/판매자 JWT 포워딩" 제안(후보 검색·판매자·구매 이력)은 **폐기** — 장바구니 I-2 패턴이 표준. **IDOR 안전**: 본문 신원은 사용자 입력이 아니라 AI가 검증 토큰에서 도출한 값이다(§2.6).
- **[v0.22.0 · 범위 확대 v0.26.0] inbound 방향의 대칭 규칙 — §3.8 조회 + §3.9 제어**: Spring → AI inbound에서 경로·본문에 실리는 `{userId}`는 **Spring이 자기 로그인 세션에서 도출한 값**이어야 하며 FE 입력을 그대로 중계해서는 안 된다. AI는 이 경로들에서 `Authorization` 헤더를 인증 수단으로 인정하지 않는다 — `X-Internal-Token`이 유일한 자격 증명이고, 신원 도출 책임은 Spring에 있다(§3.5.1 session-claim과 동일 규칙). 🔴 C-20.

### 2.4 Content-Type

| 방향 | Content-Type |
|---|---|
| 요청 본문(JSON) | `application/json; charset=utf-8` |
| 일반 JSON 응답 | `application/json; charset=utf-8` |
| SSE 스트리밍 응답(`/chat`, `/seller/chat`) | `text/event-stream; charset=utf-8` |

- SSE 응답 시 **FastAPI 앞단 리버스 프록시**는 **응답 버퍼링을 비활성화**해야 토큰 단위 스트리밍이 유지된다. FE가 AI 서버를 직접 호출하므로 chat 스트림에 대한 **Spring 중계 버퍼링 이슈는 해당하지 않는다**(§1.2).

### 2.5 스트림 전(前) 오류 봉투 (Pre-stream Error Envelope) — 확정안 반영, Spring 수용 전 🔴

비스트리밍 응답 및 **SSE 스트림이 시작되기 전** 거부(인증·요청 검증 등)의 오류 봉투다. (스트림 **내부** 오류는 §3.1/§3.2의 `error` 이벤트로 별도 전달되며 아래 봉투와 다르다.)

```json
{
  "error": {
    "code": "string",
    "message": "string",
    "requestId": "string",
    "detail": { }
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `error.code` | string | 기계 판독용 오류 코드(아래 상태 매핑) |
| `error.message` | string | 사람이 읽는 안전한 메시지(내부 스택/PII 미포함) |
| `error.requestId` | string | 추적용 요청 식별자(로그 상관관계) |
| `error.detail` | object \| 없음 | **[v0.26.0 공식화]** (선택) 오류 코드별 **기계 판독용 부가 데이터**. 형태는 각 절이 코드별로 정의한다. 현행 3종: `CART_STOCK_INSUFFICIENT`의 `availableStock`(§4.1) · `CART_OPTION_REQUIRED`의 `options`(§4.1) · `PROFILE_VERSION_CONFLICT`의 `graphVersion`(§3.9). **`message`에 기계 판독 값을 섞지 않기 위한 자리다** — `message`는 "PII 미포함 사람이 읽는 문자열"로 고정돼 있다 |

**[확정] 스트림 전 상태 코드 매핑**:

| HTTP | `code` | 의미 |
|---|---|---|
| `400` | `BAD_REQUEST` | 요청 본문/파라미터 오류 |
| `401` | `TOKEN_EXPIRED` / `TOKEN_INVALID` | 인증 실패(§2.3 a) |
| `403` | `SESSION_FORBIDDEN` | 구매자 티켓의 서명된 `sessionId` 누락/불일치, 또는 claim 뒤 옛 owner 접근 |
| `409` | `SESSION_ACTIVE` | owner claim 대상 guest session에 활성 스트림이 있음 |
| `409` | `SESSION_FINALIZING` | D6/I-20 정리 중이라 touch/claim을 받을 수 없음 |
| `409` | `SESSION_CLAIM_CONFLICT` | 이미 다른 소유권 이력이 있거나 terminal/owner가 충돌 |
| `503` | `STATE_UNAVAILABLE` | lifecycle 정본 저장소를 사용할 수 없어 fail-closed |
| `403` | `FORBIDDEN` | 권한 없음(예: 판매자 스코프 없이 `/seller/chat`) |
| `409` | `STREAM_IN_PROGRESS` | **[v0.7.0 · 개정 v0.16.0]** 동일 **`threadId`** 에 활성 스트림 존재(§2.9 a) — FE는 진행 중 스트림 종료 후 재시도. 같은 `sessionId`의 **다른 방은 막지 않는다** |
| `429` | `RATE_LIMITED` | 레이트 리밋 초과(§2.8) |
| `504` | `UPSTREAM_TIMEOUT` | **[v0.7.0]** 스트림 시작 전 상류(LLM/Spring) 타임아웃(§2.9 기준표) |
| `401` | `INTERNAL_TOKEN_INVALID` | **[v0.22.0 drift 정정]** 레인 (b) 서비스 토큰 누락/불일치(§3.5.1·§3.7·§3.9). 이 표가 "통합 목록"이라고 선언(아래)한 시점부터 이미 쓰이고 있었는데 등재가 빠져 있었다 |
| `503` | `UPSTREAM_UNAVAILABLE` | **[v0.22.0 drift 정정]** 내부 의존성(프로필 저장소·인덱스) 일시 장애(§3.7·§3.8·§3.9) — 조건은 각 절 소유, **§3.7은 카탈로그 인덱스 한정**(프로필 저장소 장애는 200 degrade). 위와 같은 누락 |
| `404` | `NOT_FOUND` | **[v0.22.0]** 대상 리소스 부재 일반 코드 |
| `404` | `PROFILE_EDGE_NOT_FOUND` | **[v0.22.0]** §3.9 대상 edge가 해당 회원 그래프에 없음. **남의 edge와 미존재를 구분하지 않는다**(열거 방지 — §3.9 공통 실패) |
| `409` | `PROFILE_VERSION_CONFLICT` | **[v0.22.0 · 위치 개정 v0.26.0]** §3.9 `If-Match` 불일치. 최신 `graphVersion`을 **`error.detail.graphVersion`** 에 병기한다(구 계약은 `error` 봉투 **밖** 최상위였다 — 아래 확장 규칙) |
| `409` | `PROFILE_EDGE_NOT_EDITABLE` | **[v0.22.0]** `editable: false`(구매 파생) edge에 대한 §3.9.1 수정 시도 |

- `404`/`503`/`500` 등 그 외 상태는 필요 시 동일 봉투로 확장한다(제안). 정확한 코드 목록은 Spring 협의로 조정될 수 있다. 🔴
- **[HARD, v0.26.0] 봉투 확장은 `error.detail`(중첩 object)로만 한다 — `error`와 나란한 최상위 형제 필드를 추가하지 않는다.** 확장 자리를 하나로 고정해야 프록시(Spring)가 오류 본문을 **`error` 객체 통째로** 전달할 수 있고, 필드를 봉투 안팎으로 옮겨 담는 재구성이 필요 없어진다(🔴 C-21의 "변형 없이 통과" 확약이 그만큼 쉬워진다). §4.1(I-2)이 `error.detail.availableStock`·`error.detail.options`로 이미 이 형태를 쓰고 있었고, §3.9만 최상위 형제로 두고 있었다 — **이번에 §3.9를 §4.1 쪽으로 통일한다**(드리프트 정정, #322).
- **[v0.22.0] `412`는 채택하지 않는다** — §3.9는 조건부 요청에 `If-Match`를 쓰지만 실패 응답은 `409 PROFILE_VERSION_CONFLICT`다. 이유는 (1) `409`에 이미 상태 인식 불일치 계열이 3종 있어 소비자가 한 분기로 처리하고, (2) `412`는 AI 서버의 상태→코드 매핑에 없어 기본값이 일반 코드로 나가 이 표의 "기계 판독용 코드" 규약을 기본적으로 위반한다. 🔴 C-22.
- 이 표가 **스트림 전 오류 코드의 통합 목록**이다(v0.7.0 — 4번 항목). 스트림 **내부** 오류는 §3.1 in-stream `error` 4종(+타임아웃, §2.9)으로 별도.

#### 공통 헤더 규약 — `X-Request-Id` · `traceparent` **[v0.15.27]**

정본은 Notion「🗂️ 프로젝트 자료실」의 **「공통 헤더 규약 — X-Request-Id · traceparent (전 API 공통)」** 페이지다. API 명세서 DB는 엔드포인트 행 단위라 전 API 공통 규약을 놓을 자리가 없어 DB 바깥에 두었다. 아래는 **AI 서버 소관 요약**이다.

| 항목 | 규칙 | AI 현재 |
|---|---|---|
| `X-Request-Id` 생성 | 최초 진입 서비스가 만든다 | — |
| 형식 | `[A-Za-z0-9_-]{16,64}` | — |
| **inbound 수용** | 형식에 맞으면 **유지**, 아니면 버리고 새로 만든다(400 아님 — 추적 편의가 요청을 막으면 안 된다) | 🔴 **미구현** — `app/core/errors.py` `request_context_middleware`가 `new_request_id()`를 **조건 없이** 호출한다 |
| **outbound 전파** | Spring 역호출에 그대로 실어 보낸다 | 🔴 **미구현** — `app/services/spring_client.py`가 `X-Internal-Token` **하나만** 붙인다 |
| 응답 echo | 응답 헤더로 되돌린다 | ✅ 구현됨(미들웨어 + 오류 핸들러 전부) |
| in-stream `error` | `requestId` 포함 | ✅ 계약 반영(§3.1·§3.2) · 구현 대기 |
| `traceparent`/`tracestate` | W3C Trace Context, tracing 켜졌을 때 전파. **MVP 범위 밖** | 🔴 코드베이스에 없음 |

> **왜 형식 검증을 하나** — 들어온 값을 무조건 믿으면 **로그 주입**이 된다(개행·제어문자로 로그 한 줄을 쪼개거나 수백 KB로 부풀리기). 화이트리스트 형식에 맞을 때만 유지한다.
>
> **`X-Request-Id`는 인증·인가에 쓰지 않는다** — 클라이언트가 정하는 값이라 신뢰 근거가 될 수 없다. 신원은 종전대로 JWT `sub`(§2.3)·`X-Internal-Token`이다.
>
> **`traceparent`와 역할이 겹치지 않는다** — `X-Request-Id`는 사람이 로그를 grep 하는 키, `traceparent`는 APM 도구가 스팬 트리를 그리는 키다. 하나로 합치지 않는다.
>
> **지금 상태의 대가**: FE → Spring → FastAPI 로그를 같은 키로 이을 수 없어, 오류 하나를 추적하려면 세 시스템 로그를 시간으로 눈대중해야 한다(#141·#134·#151).

#### 401 토큰 만료 재발급 흐름

- 사용자 JWT가 없음/무효/만료이면 AI 서버는 `401`(TOKEN_EXPIRED 또는 TOKEN_INVALID)을 반환한다(레인 a).
- FE 재시도 흐름: `401` 수신 → FE가 **Spring에 토큰 재발급 요청** → 새 토큰으로 원 요청 **1회 재시도**. AI 서버는 재발급에 관여하지 않는다(§6.1).
- **SSE 인증은 연결 시작 시점에 검증**한다(제안). 스트림 진행 중 스트림 티켓(TTL 30~60초)이 만료되어도 **활성 스트림을 끊지 않는다**(스트림 자체가 LLM 응답 1회 분량) — 만료는 다음 연결에서 `401`로 나타나며, FE가 그 시점에 새 티켓 발급·재연결한다(§2.3). 확정 전 제안(초안)이다. 🔴

### 2.6 식별자 규약 (Identifiers) — 확정, 양팀 통보 필요

**[HARD, 개정 v0.15.3] `productId` = 숫자(BIGINT), DB 스키마 기준** — **[사용자 확정 2026-07-18]** 상품/옵션/장바구니/주문 원본 id는 전부 **`BIGINT` 숫자**다(product/product_option/cart_item/order 테이블). AI 계약(§4 internal + SSE `draft`)은 **숫자 id를 그대로** 쓴다. 구 "경계별 문자열 정규화·전 구간 문자열" 규칙은 **폐기** — 그냥 스키마 타입을 따른다. **게스트 id(`guestId`)만 UUID 문자열**(guest.id CHAR(36)). 구매자 SSE는 상품 카드/productId를 싣지 않으므로(경로 B, `products.ready`=`listIds`만) 경계 변환 이슈 자체가 없다.

> **[✅ 정렬 완료 v0.15.3]** 코드(`schemas/spring.py`·`chat.py`)의 상품/옵션/장바구니/주문 id를 `int`(BIGINT)로, `guest_id`를 `str`(UUID)로 반영. CLAUDE.md "전 구간 string" 규칙도 개정. BE I-17 예시가 문자열 productId를 보였으나 **BE가 2026-07-18 숫자 BIGINT로 정정**(§4.8) — 표기 불일치 해소.

**사용자/게스트/판매자 식별자 = 숫자 id(numeric)** — Spring이 발급하며(게스트도 Spring이 숫자 id 부여), JWT `sub` 클레임에 **문자열화하여** 담는다. `role`(§2.3 a)로 회원/게스트/판매자를 구분한다.

**`sellerId` = JWT `sub`(role=seller)에서 도출 · `brandId` = JWT `brandId` 클레임에서 도출** — AI는 판매자 역호출(§4.4·§4.5)에 필요한 `sellerId`·`brandId`를 **모두 검증된 판매자 JWT 클레임에서만** 얻는다. seller `sub`는 양의 BIGINT 숫자 문자열, `brandId`는 bool 제외 JSON 정수 `1..2^63-1`로 decode 경계에서 검증한다. **`brandId`를 요청 본문·사용자 발화에서 받지 않는다**(IDOR 방지 — 판매자가 남의 `brandId`로 조회 불가). RS256 서명이라 클레임 위조 불가. **[개정 v0.8.0]** 구 "AI는 brandId를 알지 못한다(Spring 내부 해소)"에서 "JWT 클레임에서만 획득"으로 완화 — BE 집계 API가 `{brandId}` path를 요구함에 따름.

#### `sessionId`(접속) vs `threadId`(방) **[개정 v0.16.0 — SPEC-CHAT-SESSION Option B]**

한 사용자의 한 **접속**(`sessionId`) 아래 여러 **방**(`threadId`)이 **동시에** 존재한다(멀티탭 동시 대화). MVP의 `sessionId == threadId` 전제는 폐기한다. 두 축은 담당하는 상태가 다르며, **어느 축으로 키잉하는지가 곧 계약**이다.

| 축 | 발급 | 수명 | 담당 상태 |
|---|---|---|---|
| `sessionId` | **Spring** CH-1(`POST /api/chat/sessions`) | BE Redis TTL 10분 sliding | 프로필 세션버퍼 · 세션 종료 통지(I-20, §3.5) · `conversation_turns.conversation_id`(primary) |
| `threadId` | **FE**(탭·방별, 서버 왕복 없음) | 소속 세션과 함께 만료(아래) | 멀티턴 필터 누적 · 장바구니 pending · 되돌리기 · **동시 스트림 락**(§2.9 a) · `conversation_turns.thread_id` |

- **AI는 `sessionId`의 만료를 판정하지 않는다** — 세션 TTL은 Spring Redis 소유이고 AI가 검증하는 것은 스트림 티켓(§2.3 a)뿐이다. 따라서 AI가 `CHAT_SESSION_EXPIRED`를 반환하는 경우는 없다(§2.5). 만료 의미가 **없어서**가 아니라 **판정 주체가 Spring이라서**다. 다만 프로필 파이프라인은 자체 DB에 기록한 마지막 회원 발화 시각을 기준으로 **프로필 버퍼의 10분 비활동 종료**를 독립적으로 판정한다(§3.5).
- **새 대화는 CH-1을 부르지 않는다** — FE가 `threadId`만 새로 생성하고 세션은 유지된다. 따라서 "새 대화"는 세션 종료 사유가 아니다(§3.5).
- **맥락 TTL은 방이 아니라 접속 단위** — 어느 방에서든 활동이 있으면 그 `sessionId`에 속한 **모든 방**의 맥락 TTL을 함께 연장하고, 세션이 끝나면 그 아래 방을 **한꺼번에** 정리한다. 방마다 생사가 갈리면 탭을 옮겼을 때 한쪽 맥락만 사라져 사용자가 이해할 수 없다.
- **구매자 스트림 티켓은 `sessionId`를 담고 `threadId`는 담지 않는다.** AI는 서명된 `sessionId`를 body와 대조해 다른 접속의 세션 상태 접근을 막는다. `threadId`는 body-only라 **티켓 1장이 한 접속의 여러 방 스트림을 동시에 커버**한다. 세션 수명·만료의 정본은 계속 Spring Redis에 있다.
- **AI 내부 문맥 정본은 전역 고유 `context_id`다(#187).** 최초 정상 touch에서 한 번 생성하며 guest→member claim, D6 만료 후 같은 owner의 재활성화, 여러 `threadId`의 후속 발화에서도 유지한다. 구조화 상태는 `context_id:threadId`로 키잉하고 claim 때 복사하지 않는다. 상세 상태 기계·rollout은 `docs/specs/SPEC-CHAT-SESSION-CONTEXT-187.md`.
- 최대 길이는 둘 다 config `chat_key_max_chars`(§3.1) — 초과 시 `400`.

> **`sessionId`는 "불투명 스레드 키"가 아니다.** v0.15.x까지 이 문서는 `sessionId`를 *"만료 의미 없는 불투명 스레드 키"* 로 정의했다. 축이 갈린 뒤 "스레드 키"는 `threadId`의 것이므로 그 표현을 전면 폐기한다. `sessionId`는 여전히 AI에게 **불투명**하지만(형식 검증 없음, UUID 수용), **접속 식별자**다.

> 사용자/판매자 식별자 타입(숫자)·클레임 키는 Spring 회원 스키마 소유다 — 세부는 🔴 협의(§5 C-10).

### 2.7 이벤트 엔드포인트 멱등성 규약 (Idempotency for Event Endpoints)

`/events/*` 엔드포인트(§3.5~3.6)는 통지 채널이므로 **멱등(idempotent)** 이어야 한다.

- **[v0.15.19, 이슈 #79]** session-end(§3.5)은 별도 `eventId` 필드가 없다 — Spring I-20 멱등은 **`(userId, sessionId)` 고정 파생키**(`session-end:{userId}:{sessionId}`)로 판정한다. AI 내부 비활동 처리는 같은 키의 `PROCESSING` claim으로 I-20과 경합을 직렬화하되, 성공 뒤 claim을 영구 완료하지 않고 해제하는 **재개 가능한 checkpoint**다. 같은 `sessionId`의 새 회원 발화가 실제 저장되면 이전 `PROCESSING`/`COMPLETED` 키를 activity 갱신과 같은 transaction에서 무효화하므로 새 버퍼를 다음 timeout/I-20이 처리할 수 있다.
- 통지는 **best-effort** 이며, 유실되어도 AI 서버의 정합성은 통지에 의존하지 않는다(세션 종료: AI 내부 비활동 sweep이 저장 버퍼를 회수). 상세는 각 엔드포인트 항목 참고.
- **[v0.15.17 확정]** 정상 신규·중복 통지는 처리 완료 여부와 무관하게 `202 Accepted`로 수신 확인한다(§3.5).
- **[v0.22.0] 적용 범위는 `/events/*` 전용이다.** 같은 레인 (b)라도 §3.7(I-22)과 §3.9(개인화 그래프 제어)는 통지가 아니라 동기 요청/응답이므로 이 절의 규약(202 `accepted`/`duplicate`)이 적용되지 않고 **각 절이 자기 멱등 규약을 소유한다** — I-22는 비멱등(§3.7 규약), §3.9는 파생 키 + `replayed` 플래그(§3.9 preamble). 어느 경우에도 **클라이언트 지정 `Idempotency-Key` 헤더는 도입하지 않는다**(파생 키 원칙 유지).

### 2.8 CORS 및 레이트 리밋 (CORS & Rate Limiting) — 🔴 협의 필요

FE가 AI 서버(FastAPI)를 **다른 오리진에서 직접 호출**하므로 브라우저 CORS·남용 방어가 AI 서버 앞단으로 이동한다.

- **CORS**: AI 서버는 FE 오리진에 대해 CORS 헤더를 서빙해야 한다. 허용 오리진 목록은 🔴 협의(§5 C-11). `Authorization` 헤더를 사용하므로 브라우저 **preflight(OPTIONS)** 가 발생한다 — AI 서버는 preflight에 `Access-Control-Allow-Headers: Authorization` 등으로 응답해야 한다.
- **레이트 리밋(레인 a)**: 게스트도 토큰(익명 JWT)을 지참하므로 **토큰 스코프 기반 레이트 리밋**이 가능하다(§2.5). 초과 시 `429 RATE_LIMITED`(§2.5).
- **[v0.7.0 확정] 목적·소유·값**: 목적은 정밀 과금 통제가 아니라 **무분별한 남용 차단**(2026-07-15 사용자). **MVP 소유 = FastAPI 미들웨어 + in-memory 카운터**(단일 인스턴스 전제 — 다중 인스턴스 확장 시 Redis 이관, §2.9 동시 스트림 레지스트리와 동일 단서). 상한은 **config 기본값 제안**: 채팅 메시지(POST /chat·/seller/chat) **분당 10회 / 시간당 100회**(토큰 `sub` 스코프, 게스트 동일). 값 자체는 운영 조정 대상이며 계약 사항은 "429 + 토큰 스코프"뿐이다.
- **잔여 🔴(C-11)**: 허용 오리진 목록. ~~**[v0.22.0] `Access-Control-Expose-Headers: ETag`**~~ — **[v0.26.0 소멸]** §3.8이 레인 (b) Spring 프록시로 옮겨져 브라우저가 그 응답을 직접 읽지 않는다. 애초에도 정규 버전 출처는 본문 `graphVersion`이라 기능은 성립했다.

### 2.9 SSE 스트림 수명주기 — 동시 스트림·취소·타임아웃 [v0.7.0 신설]

`POST /chat`·`POST /seller/chat` 공통 규약이다.

#### (a) 동시 스트림 제한 — **방(`threadId`)당 1개** **[개정 v0.16.0]**

- 동일 `threadId`에 활성 스트림이 있는 상태에서 새 요청이 오면 **`409 STREAM_IN_PROGRESS`**(§2.5 봉투)로 거절한다(기존 스트림은 유지 — last-wins 아님, 2026-07-15 확정).
- **같은 `sessionId`의 다른 방은 서로 막지 않는다** — 락 키가 `threadId`라 탭 A가 스트리밍 중이어도 탭 B는 정상 스트리밍된다. 멀티탭 동시 대화가 이 축 분리의 목적이므로, **세션 단위로 잠그면 그 목적이 정면으로 무효화된다**(§2.6).
- **FE 1차 방어**: 스트리밍 중 **해당 방의** 입력창 비활성화. 409는 서버 측 백스톱(같은 방 재전송·중복 제출 대비).
- 구현: 인프로세스 활성 스트림 레지스트리(**MVP 단일 인스턴스 전제** — 결정 8의 무상태 원칙과의 긴장은 "요청 간 사용자 상태 없음" 의미로 한정 해석하고, 다중 인스턴스 확장 시 Redis로 이관).

#### (b) 요청 취소 — 취소 신호 = 연결 종료 (별도 취소 엔드포인트 없음)

- FE: `AbortController.abort()` → fetch 연결 종료. 이것이 유일한 취소 인터페이스다.
- AI 서버: SSE 제너레이터가 이벤트 전송 사이마다 disconnect를 감지(`request.is_disconnected()` 폴링)하고, 감지 즉시 **진행 중인 LLM 스트림을 close**(토큰 비용 차단)하며 LangGraph 실행 task를 취소한다.
- 취소된 턴의 대화 저장은 `CANCELLED` 상태 + **부분 생성 텍스트 보존**(§6.3) — 다음 턴 컨텍스트·프로필 스캔에 포함된다.

#### (c) 타임아웃 기준표 — 제한값 확정(config 기본값)

| 구간 | 기준값 | 초과 시 동작 |
|---|---|---|
| FE→AI **first-token**(역할 공통, 스트림 첫 SSE 이벤트까지) | **10s** | 스트림 시작 전이면 `504 UPSTREAM_TIMEOUT`(§2.5), 시작 후면 in-stream `error` 후 종료 |
| FE→AI **구매자 스트림 전체 상한** | **30s** (`stream_total_timeout_buyer_s`) | `done`(finishReason `stop`) 강제 종료 + 저장 상태 `FAILED` 아님(정상 절단) |
| FE→AI **판매자·미지정 역할 스트림 전체 상한** | **90s** (`stream_total_timeout_s`) | `done`(finishReason `stop`) 강제 종료 + 저장 상태 `FAILED` 아님(정상 절단) |
| AI→Spring 콜백(§4.1/§4.4~4.7/§4.9/§4.12~4.17) | **3s**(BE I-2 문서 기준으로 통일) | 각 계약의 degrade 규칙(조회 생략·담기 `CART_ERROR`·dedup 생략 등) |
| ↳ **I-1 검색만 재시도 1회** (v0.19.2) | **3s × 2 = 6s** (config `SPRING_MAX_RETRIES`) | 재시도 대상은 **타임아웃·연결 오류·응답 중단(서버가 응답 도중 연결 종료)·5xx·일시 4xx(408·429)** 이며 **그 밖의 4xx 는 즉시 실패**한다(다시 불러도 같은 결과). 408·429 만 4xx 중 예외인 이유는 요청 자체가 유효하고 서버·인프라의 **일시 상태**일 뿐이기 때문이다(계약에 명시된 코드는 아니나 프록시·게이트웨이가 낼 수 있다). `Retry-After` 는 존중하지 않는다 — backoff 가 없어 즉시 재호출하며, 상한 1회로 증폭을 묶는다. I-1은 GET·멱등이라 안전하고, **비멱등 호출(I-2 담기 등)에는 재시도를 걸지 않는다.** 재시도 총량(기본 6s)은 구매자 전체 상한(30s)·first-token 상한(10s)보다 작고 기동 시 검증하지만, 이는 **필요조건**이다. **BE 관측 포인트** — `conditions`를 미루지 않는 턴은 같은 검색 요청이 최대 2번 온다. 기본값에서 `may_auto_relax` 턴(#113)은 첫 이벤트 앞의 본 검색 1회와, 0건이면 자동 완화 probe 1회를 **각각 재시도 없이** 호출해 Spring 직렬 구간을 `2 × 3s = 6s`로 묶는다. `conditions` 뒤 완화 칩 probe는 종전대로 재시도 1회를 유지한다. 변경 전에는 두 호출이 모두 재시도하고 2차 응답이 2.9s에 오면 **이벤트 0건·504가 8/8** 재현됐으나, 변경 후 같은 시나리오는 p50 3.40s에 `conditions`+in-stream `error(SEARCH_FAILED)`(`retryable`)로 끝났다. 새 최악(두 호출이 재시도 없이 각각 2.9s 성공)은 p50 6.97s·200 정상 답변이었다. 다만 재시도가 살리던 검색 장애도 6.80s 정상 답변에서 3.40s degrade로 빨리 떨어진다. 수치는 LLM head 제외이며 #151 baseline head p95 ≈3.0s를 더하면 새 최악도 ≈10.0s라 여유가 작고, 직렬 합 검증·타임아웃 재배분은 #288에 남긴다. `SEARCH_RETRY_ON_DEFERRED_CONDITIONS=true`면 종전 재시도를 복구하지만 이 조합은 기동 시 직렬 합으로 검증돼 기본 타임아웃 그대로면 기동이 막히고, `relaxation_max_rounds=0`이면 미루기 자체를 끈다. **[갱신 v0.21.0]** 구매자 `progress` 이벤트는 **계약 등재가 완료됐다**(2026-08-05, 이슈 #289). **[갱신 v0.26.2]** 플래그 `progress_events_enabled`가 **기본 on**으로 전환됐다(#396, 2026-08-06 FE 구현 완료). 다만 **이 재시도 스킵 원복은 #396 범위가 아니다** — #394(커밋 `2168e9b`)가 같은 날 다른 이유(Spring 부하 실측)로 `spring_max_retries` 기본값을 1→0으로 이미 내렸고, 이 스킵의 원복 여부는 그 조치와 함께 판단해야 하는 별도 결정이라 스킵 동작 자체는 이번에 건드리지 않았다 |
| AI→LLM 단일 호출 | **30s + 1회 재시도** | 재시도 실패 시 in-stream `error`(`LLM_UNAVAILABLE` 계열) |

- 값은 config 기본값이며 운영 조정 가능. **계약 사항은 초과 시 동작**(어떤 오류가 어느 채널로 오는가)이다.

---

## 3. AI 서버 제공 API

> **호출자 구분**: §3.1~3.4는 **FE → AI 직접 호출**(사용자 JWT, 레인 a). §3.5~3.6(`/events/*`), **§3.7(I-22 홈 추천 랭킹)**, **§3.8·§3.9(개인화 그래프 조회·제어 I-32~I-37)** 은 **Spring → AI 서버 간 호출**(서비스 토큰, 레인 b). **[개정 v0.26.0]** 프로필 표면은 §3.4(마크다운 조회)만 레인 (a)에 남고 **그래프 표면은 조회·변경 모두 레인 (b)로 통합**됐다 — 마이페이지에서 `chat:stream` 티켓을 발급받을 수 없다는 것이 드러나 v0.22.0의 비대칭 전제가 무너졌다(§3.9 preamble). §1.2 참고.

### 3.1 `POST /ai/chat` — 구매자 챗봇 (SSE 스트리밍, FE 직접)

구매자의 자연어 질의를 받아 상품 추천/장바구니/상품 질문/**주문상태 문의** 등을 SSE로 스트리밍 응답한다. **[v0.16.3, #164 구현] 주문상태 Q&A(I-4)를 `order_status` intent로 CH-2에 흡수**했으며 세부 계약은 §4.10을 따른다 — 별도 CS 챗봇 없음. 관리자 CS 문의(CH-3·I-5·AD-1/2·M-9)는 **post-MVP**. 소유: `SPEC-RECOMMEND-001`(추천 서브그래프), 상위 구매자 그래프 SPEC(라우팅).

> **[경로 정합 v0.15.0]** FE-대면 경로는 **`{AI_SERVER}/chat`**(BE DB 07/17 실측 — 구 `/ai/chat` 표기 정정, `{AI_SERVER}` 접두어로 AI 서버 직접 호출임을 명시, 인증=스트림 티켓 필요). 본 문서 다른 위치의 `POST /chat`·`POST /ai/chat` 표기는 이 경로로 읽는다. (판매자는 `{AI_SERVER}/seller/chat`.)

#### 요청 (Request)

```json
{
  "sessionId": "string",
  "threadId": "string",
  "message": "string",
  "conditionActions": [{ "op": "remove", "field": "category" }],
  "screen": {
    "pageType": "chat",
    "columns": 3,
    "products": [{ "productId": 101, "name": "무선 이어폰" }]
  }
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string | 예 | **Spring 발급 접속 식별자**(§2.6). **프로필 세션버퍼·세션 종료 통지(§3.5)의 키** — 여러 방의 발화가 이 하나의 버퍼로 모인다. **[v0.15.7] 최대 길이 = config `chat_key_max_chars`(기본 200자)** — 초과 시 `400`(불투명 키 남용 방어) |
| `threadId` | string | 예 | **FE 생성 방 식별자**(§2.6). **멀티턴 필터 누적·장바구니 pending·되돌리기·동시 스트림 락(§2.9 a)의 키.** **[v0.15.7] 길이 상한 동일**(`chat_key_max_chars`) |
| `message` | string | 예 | 현재 턴 사용자 원문 질의. **[v0.15.6] 최대 길이 = config `chat_message_max_chars`(기본 4000자)** — 초과 시 `400 BAD_REQUEST`(§2.5). PII·메모리 방어(`/seller/chat` 동일). **[v0.15.26] `conditionActions`가 1건 이상이면 빈 문자열을 허용**한다 — 둘 다 비면 `400`. |
| `conditionActions` | array | 아니오 | **[v0.15.26 신설]** FE 조건 칩 제거 액션. 미지정 기본값은 빈 배열. **구매자 전용** — 아래 상세. |
| `screen` | object | 아니오 | **[v0.15.26 신설]** 사용자가 지금 보고 있는 화면. 지시어("이거") 해석·담기 대상 확정에 쓴다 — 아래 상세. |

> **[보안] `userId`는 요청 본문에 없다** — 사용자 식별자·역할은 `Authorization` 헤더의 JWT 클레임(`sub`/`role`)에서만 추출한다(사칭 방지, §2.3 a·§2.5). `conditionActions`·`screen` 도 신원을 싣지 않는다.

##### `conditionActions` — FE 조건 칩 제거 (선택) **[v0.15.26]**

`conditions` 칩(아래 (3))의 X 버튼을 눌렀을 때 FE가 보내는 **구조화 신호**다. 어떤 칩을 지웠는지는 UI만 아는 사실이라 서버가 발화만으로 복원할 수 없다.

- `op` — **`remove`만 허용**한다. add·replace 범용화는 필요해질 때 확장.
- `field` — `conditions` 칩의 계약 필드 **6종만** 허용(아래 (3) 참조). 그 밖의 값은 `400`.
- **멱등하다** — 이미 없는 필드를 지워도 성공 no-op, 같은 `field` 중복은 dedup. 재전송에 안전해야 한다.
- **구매자 전용 계약** — 판매자 요청(§3.2)에는 없다. AI는 `BuyerChatRequest`를 따로 두어 공통 `ChatRequest`를 오염시키지 않는다.

> **[폐기 v0.15.26]** 구 규약 — *"칩 제거는 왕복(round-trip), 다음 턴 `message`에 규약 문자열(예: `"[조건 제거] priceMax"`)로 실어 재분해를 트리거"* — 는 **폐기**한다(이슈 #84). FE는 이 방식으로 구현돼 있으나 AI에 수신부가 없어 **현재 칩 제거가 무동작**이다. 전환 시 FE는 제어 메시지를 사용자 말풍선으로 남기지 않는다.

##### `screen` — 현재 보고 있는 화면 (선택) **[v0.15.26]**

채팅은 좌(대화)/우(패널) 분할이고, 사용자는 우측 패널을 보며 **"이거"** 라고 말한다. 그 지시어는 발화만으로 확정할 수 없다 — 무엇이 어떻게 보이고 있었는지는 FE만 아는 사실이다(이슈 #118, 07-17 FE 제안).

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `pageType` | string | 예 | **우측 패널에 뜬 내용**의 종류. 라우트가 아니다(아래 참조). E-1 `page_view.pageType`과 **같은 enum을 공유**한다 |
| `filters` | object | 아니오 | 패널에 걸린 필터. **값은 사람이 읽는 표시값** — LLM 프롬프트에 그대로 들어간다. 키는 `status`·`sort`·`page` |
| `products` | array | 아니오 | **서버가 모르는 목록**만 `[{productId, name}]`(아래 참조). 최대 `screen_products_max`(config, 기본 20)건 — 초과분은 FE가 화면 순서대로 자른다 |
| `columns` | number | `products` 있으면 | **전송 시점 그리드 열 수.** 반응형이라 창 크기에 따라 바뀌며 서버가 알 수 없다. 목록형은 `1` |

**`filters` 키 (3종)** — 값은 **enum 코드가 아니라 화면에 보이는 한글 표시값**을 싣는다. LLM 프롬프트에 그대로 들어가므로 코드값(`ORDERED`)은 의미 전달이 안 된다.

| 키 | 쓰는 `pageType` | 값 예시(표시값) |
|---|---|---|
| `status` | `seller_orders` | `전체` · `신규주문` · `배송중` · `배송완료` · `취소·반품` |
| `status` | `seller_products` | `전체` · `판매중` · `품절` · `숨김` |
| `sort` | `seller_products` | `최신순` · `판매순` · `재고순` · `가격순` |
| `page` | 목록 전부 | `"1"` (**사람이 보는 1-base**. API의 0-base가 아니다) |

> **[v0.15.26] 목록 필터 키는 `status` 하나로 통일한다.** 주문 목록(주문 상태)과 상품 목록(상품 상태)은 값 집합이 다르지만 **어느 쪽인지는 `pageType`이 이미 말해주므로** 키를 나눌 이유가 없다. FE는 현재 URL 쿼리에서 주문은 `?status=`, 상품은 `?tab=`으로 다르게 쓰는데, **`screen.filters`에는 둘 다 `status`로 싣는다** — `tab`은 UI 용어라 LLM에 의미가 없다.
>
> `category`는 브랜드 페이지 필터인데 **그 화면엔 채팅이 없어** `screen`으로 올 경로가 없다. 사이드 채팅이 붙으면 그때 추가한다.

> **[v0.15.26] `pageType`은 라우트가 아니라 패널 내용을 가리킨다** — 채팅은 전용 페이지(`/chat`·`/seller/chat`)에만 있고 사이드 채팅 위젯이 없다. 라우트를 실으면 항상 `chat`·`seller_chat`이라 정보가 0이다. 판매자가 `/seller/chat`에서 우측 워크스페이스의 "주문 관리" 탭을 보고 있으면 `seller_orders`를 싣는다.
>
> **현재 UI로 실제 오는 값은 3종**이다 — `chat`(구매자 인기상품 패널) · `seller_orders` · `seller_products`. 나머지 11종은 **E-1 `page_view` 전용**이며, `screen`에 실리는 건 사이드 채팅이 그 화면에 붙은 뒤다. **FE는 3종만 매핑하면 되고, AI는 나머지 분기를 만들지 않는다.**

> **[v0.15.26] `products`는 "서버가 모르는 목록"에만 싣는다** — 판단 기준은 *화면에 보이나*가 아니라 *서버가 이미 아나*다.
>
> | 패널 내용 | 만든 주체 | `products` |
> |---|---|---|
> | 구매자 — 대화 시작 전 **P-4 인기상품** | Spring (FE 직접 조회) | ✅ **싣는다** |
> | 구매자 — 추천 카드(CH-5) | AI (I-21 → `listId`) | ❌ 서버가 `listId`로 안다 |
> | 판매자 — 자사 상품 목록 | Spring (I-9) | ✅ **싣는다** |
> | 판매자 — 주문 목록 | Spring | ❌ 채팅이 주문에 하는 쓰기가 없다 |
>
> 추천 카드를 되돌려주면 **위조 경로**가 된다(E-1도 같은 이유로 FE가 보낸 추천 문맥을 신뢰하지 않는다). 페이로드도 작아진다.

> **[v0.15.26] 지시어 해소** — `columns`가 있으면 좌표 지시가 풀린다: `index = (row-1) × columns + (col-1)`. **배열 순서가 화면 순서**(좌→우, 위→아래)임이 전제다. `rows`와 항목별 `row`/`col`은 **싣지 않는다** — `products.length`와 `columns`에서 계산된다.
>
> | 발화 | 필요한 것 |
> |---|---|
> | "이거 담아줘" | 후보가 1건일 때만 확정, 여러 건이면 **되물음** |
> | "3번째 거 담아줘" | 배열 순서 |
> | "3번째 줄 2번째 담아줘" | **`columns`** |
> | "무선 이어폰 담아줘" | `name` 매칭 |

> **[v0.28.1, #435] 지시어 해소는 `screen.products`가 있는 턴에만 작동한다** — 추천 카드(CH-5)는 위 표처럼 `screen`에 실리지 않으므로(위조 경로 방지), 추천 카드를 이름으로 지목하는 발화("무선 이어폰 찜해줘")는 이 결정적 해소기가 아니라 `LAST_RECOMMENDATIONS`(직전 추천 목록) 프롬프트 문맥으로 LLM이 해석한다. AI 동작 서술 추가 — 와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변.

> **[v0.15.26] 유효성 — `screen`은 관대하게, `conditionActions`는 엄격하게** 처리한다. `screen`은 **맥락 힌트**라 잘못돼도 사용자 플로우를 막지 않는다(E-1의 *"개별 이벤트가 이상해도 배치를 실패시키지 않는다"* 와 같은 철학). `conditionActions`는 **사용자 의도의 직접 표현**이라 조용히 무시하면 "왜 안 지워지지"가 된다.
>
> | 상황 | 처리 |
> |---|---|
> | `pageType` 누락·미지의 값 | `screen` **전체를 무시**하고 200 진행 |
> | `products` 상한 초과 | 앞 `screen_products_max`건만 취하고 버림 |
> | `products[].name` 누락 | 그 항목의 이름만 버림(`productId`는 allowed 에 남긴다) |
> | `products` 있고 `columns` 누락 | 좌표 지시만 불가, 나머지 진행 |
> | `conditionActions`의 알 수 없는 `op`/`field` | **400** |
> | `message` 키 생략·공백만 | `""` 와 동일 취급(trim 후 판정) — `conditionActions`가 있으면 정상, 없으면 **400** |

**`pageType` 어휘 (14종 — 구매자 10 · 판매자 4)** — **정본은 E-1**(Notion「📡 API 명세서」E-1 · 「이벤트 수집 확정 명세 v2」§1-2). `page_view` 이벤트와 `screen.pageType`이 **같은 값을 쓴다**.

| 구분 | 값 | 라우트 예 |
|---|---|---|
| 구매자 | `home` | `/` |
| | `category` | `/brands/:brandId` (브랜드·카테고리 목록) |
| | `search` | 검색 결과 |
| | `product_detail` | `/products/:productId` |
| | `cart` | `/cart` |
| | `checkout` | `/checkout` |
| | `order_complete` | `/checkout/complete` |
| | `my` | `/mypage/*`·`/wishlist` |
| | `chat` | `/chat`·`/inquiry` |
| | `auth` | `/login`·`/signup` |
| 판매자 | `seller_dashboard` | `/seller` |
| | `seller_orders` | `/seller/orders` |
| | `seller_products` | `/seller/products` |
| | `seller_chat` | `/seller/chat` |

> **[v0.15.26] 어휘 확장** — 기존 8종은 구매자 화면만 커버해 판매자·채팅·인증 화면이 갈 곳이 없었다(`page_view`가 전 라우트에서 발화하므로 미분류 값이 생긴다). `chat`·`auth` + 판매자 4종을 추가해 **8종 → 14종**(구매자 10 · 판매자 4)으로 넓힌다. E-1 정본과 함께 개정한다. **`screen.pageType`에 실리는 건 이 14종의 부분집합**이다(현재 3종).

> **[v0.15.26] `path`·`label`을 두지 않는 이유** — 화면을 가리키는 어휘를 새로 만들지 않는다. `pageType`이 E-1에 이미 있으므로 그대로 쓰고, 라우트 경로(`path`)는 **쿼리스트링에 검색어 등 PII가 실릴 수 있어** 계약에서 뺀다(필요한 조건은 `filters`로 정제해 나른다). 한글 화면명(`label`)은 **AI가 `pageType`→표시명 매핑을 config로 갖는다** — 프롬프트 문구는 튜닝 대상이지 계약이 아니고, FE가 관리하면 화면마다 표현이 흔들린다.

> **[보안] `screen.products`는 담기 허용 목록을 넓히는 입력이지 프리패스가 아니다.** 담기 가드는 **(누적 추천 목록 ∪ `screen.products`의 productId)** 를 allowed로 취급하며, **두 목록 밖의 id는 여전히 차단**한다. "이거"가 모호하면 되물음한다 — LLM이 발화 속 임의 숫자를 오추출해 담는 것을 막는 기존 가드는 유지된다. 실제 담기는 I-2(§4.1)가 재고·판매상태를 다시 검증한다.

> **[AI 구현, 정본 반영됨(2026-08-05)] 옵션 되물음(`PENDING_CART`) 중에는 위 합집합에서 `screen.products`를 뺀다.** 위 문단은 "(누적 추천 목록 ∪ `screen.products`)를 allowed로 취급"이라 명시하지만, 옵션 되물음이 진행 중인 턴에는 코드가 `screen.products`의 productId를 allowed에 넣지 않는다(`app/agents/buyer/graph.py`) — 되물음 중 화면 id가 allowed에 있으면 발화 속 임의 숫자 오추출이 그 id와 우연히 일치할 때 진행 중이던 옵션 되물음이 조용히 버려지고 사용자가 답한 적 없는 상품이 담기는 오담기가 실제로 재현됐기 때문이다. 위 문단이 명시적으로 요구하는 것은 "두 목록 **밖**의 id 차단 유지"이고 이 예외는 **더 차단하는** 방향이라 그 보증 자체는 깨지 않지만, "allowed = 합집합"이라는 서술과는 문면상 어긋난다. **정본(Notion "📡 API 명세서" CH-2) 담기 가드 문단 바로 아래 '되물음 예외 (2026-08-05 신설, #118)' 문단으로 반영됐다** — 이 사본과 정본은 이제 일치하며, 이 문단은 코드의 실제 동작이자 정본 문면 그대로다.

> **판매자 스트림(§3.2)도 동일하다** — `screen`은 **구매자·판매자 공용 요청 필드**다(`conditionActions`가 구매자 전용인 것과 다르다). 스키마는 공용 `ChatRequest`에 두고 `BuyerChatRequest`/`SellerChatRequest`가 상속한다. 판매자 대시보드는 이미 `meta.lane`·`done.panel`로 **AI→FE 방향의 화면 조작**을 계약에 두고 있는데 그 반대 방향(FE→AI, 지금 무엇을 보고 있나)이 비어 있었다 — 서버가 `panel="refresh"`로 우측 재조회를 지시하면서 그 패널에 무엇이 떠 있는지는 모르는 상태였다.

#### 응답 (Response) — `text/event-stream`

SSE로 스트리밍한다. 표준 `EventSource`는 GET 전용이므로 FE는 **fetch 스트리밍(ReadableStream)** 으로 소비한다(§6.1). 이벤트명은 `progress`/`token`/`conditions`/`action`/`products.ready`/`done`/`error`를 쓴다. **상품 카드는 SSE로 오지 않는다**(경로 B, §3.3·§4.2·§4.3).

**(1) `progress`** — 진행 단계 알림 (0회 이상). **[확정 v0.21.0, 이슈 #289, 정본 합의·등재 2026-08-05 / 다회 emit·어휘 확장 v0.27.0, 이슈 #396, 정본 개정 2026-08-06]**

```json
{ "type": "progress", "data": { "stage": "analyzing", "message": "요청을 확인하고 있어요" } }
```

| `stage` | 의미 | 발생 조건 |
|---|---|---|
| `analyzing` | 요청을 확인하는 중(intent 미확정) | 모든 구매자 턴(추천·담기·주문조회·일반 대화 공통) |
| `mapping` | 카테고리 매핑 중 | 추천 턴 중 실제로 매핑을 태우는 턴(승계·리셋 턴은 건너뜀) |
| `expanding` | 니즈 전개 중(#198) | 매핑 실패·신호 부족으로 전개가 발동한 턴 |
| `searching` | 상품 후보를 찾는 중 | 추천 턴(I-1 검색 또는 I-3 인기 목록) |
| `relaxing` | 조건을 완화해 다시 찾는 중 | 0건이라 자동 완화 루프가 실제로 probe 한 턴 |
| `reranking` | 결과를 정렬하는 중 | 추천 턴의 재정렬 진입 |
| `publishing` | 목록을 준비하는 중(I-21 push) | `products.ready` 직전 |

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `stage` | string | 예 | 진행 단계 어휘(위 표 7종, **개방형**). |
| `message` | string | 아니오 | 사용자 노출 문구. 서버가 빈 값이면 `data`에 이 키 자체를 싣지 않고, 그때 FE가 `stage`로 자체 문구를 매핑한다 |

- **0회 이상이다.** 여러 번 올 수 있고, 나가면 **그중 첫 프레임이 스트림의 첫 프레임**이다.
- **FE는 도착도·개수도·특정 stage의 등장도 전제하면 안 된다** — 턴 유형과 분기에 따라 어휘 중 일부만 온다. 같은 `stage`가 두 번 이상 올 수도 있다.
- **표시 규약은 덮어쓰기다** — 누적 목록이 아니라 "지금 이것" 한 줄로 최신 프레임만 보여준다.
- **`token` 이후에도 올 수 있다** — `publishing`은 근거 `token`이 나간 뒤 `products.ready` 직전에 온다. FE는 진행 표시를 답변 렌더링과 **독립적으로** 다뤄야 한다.
- **어휘는 개방형(open set)이다** — FE는 **모르는 `stage` 값을 무시**하고(프레임을 버리거나 직전 표시를 유지) 절대 오류로 다루지 않는다. 이 규약 덕에 이후 어휘 추가는 **AI 단독 배포**로 가능하다(FE 배포와 커플링되지 않는다).
- **첫 stage가 `analyzing`(intent 중립)인 이유** — 이 프레임을 낼 수 있는 지점(decompose 앞)에서는 아직 추천/담기/주문조회/일반 대화 중 어느 intent인지 확정되지 않았다. `searching`을 그 자리에서 내면 비추천 턴(담기·주문조회 등)에 "검색 중"이라고 오라벨링하게 된다. **`searching`은 이제 존재하지만, intent가 확정된 뒤 추천 레인 안에서만 나간다** — 이 논거를 뒤집지 않는다.
- **관문 통과를 보장하지는 않는다** — 이 프레임보다 앞에 있는 상태 저장소 프렐류드(세션·스레드·회원 프로필·장바구니 조회, 각 `state_store_query_timeout_s` 3.0s)가 직렬 최악 12.0s로 first-token 상한(10.0s, §2.9 c)을 넘을 수 있다.
- **기존 6종의 이름·페이로드·상대 순서는 불변**(추가 전용) — `conditions`는 여전히 검색·자동 완화 뒤다.
- **판매자 스트림(§3.2)의 `progress`(`{"text": …}`)와 페이로드가 다르다** — 셰이프 통일은 후속 과제(판매자에 `stage`를 가법으로 추가, 기존 `text`는 유지하는 방향으로 검토).
- **AI 구현 상태**: config `progress_events_enabled` **기본 `true`**(#396, 2026-08-06 FE 구현 완료로 해제 — 계약 등재 v0.21.0·운영 기동 가드 제거)로 실제 와이어에 나가며, `stage` 어휘 7종·다회 emit 도 v0.27.0 부터 실제로 나간다. 되돌리려면 `PROGRESS_EVENTS_ENABLED=false`.

**(2) `token`** — 근거/코멘트 토큰 증분 (0회 이상).

```json
{ "type": "token", "data": { "text": "이 케이스는 방수라서" } }
```

**(3) `conditions`** — 추출된 필터 조건을 FE 제거 가능한 칩으로 전달 (0~1회)

```json
{
  "type": "conditions",
  "data": {
    "chips": [
      { "field": "priceMax", "label": "5만원 이하", "value": 50000 },
      { "field": "category", "label": "여행용품", "value": "여행용품/보안용품" }
    ]
  }
}
```

- FE는 각 칩을 제거 가능한 형태로 노출한다. **[v0.15.26] 칩 제거는 위 `conditionActions`로 보낸다** — 구 규약 문자열 왕복 방식은 폐기됐다(이슈 #84).
- **[v0.15.26] `field` 허용값 6종** — `category`(카테고리 · 이어폰) / `priceMax`(50,000원 이하) / `priceMin`(30,000원 이상) / `brand`(삼성 · LG) / `ratingMin`(평점 4.5+) / `keyword`(방수). **`conditions`가 내보내는 집합과 `conditionActions`가 지우는 집합은 동일하다.** 종전에는 예시 둘만 있어 허용 집합이 계약에 없었다.
- 칩은 LLM 출력이 아니라 **확정된 병합 필터에서 결정론적으로 파생**된다(`build_condition_chips`) — 같은 필터면 항상 같은 칩이 나온다.

**(4) `action`** — 장바구니 담기·삭제·수량 변경·찜 추가·찜 해제 결과 (0회 이상). §4.1(I-2)·§4.12~4.16(I-24~I-28)과 연동.

```json
{
  "type": "action",
  "data": { "type": "CART_ADDED", "message": "여행용 방수 파우치를 장바구니에 담았어요.", "cartItemId": 55 }
}
```

> **[v0.22.0 정정] `cartItemId`는 number(BIGINT)다** — 이전 사본 예시가 `"55"`(문자열)로 표기돼 있었고 필드표도 `string`이었다. 정본 CH-2·FE 타입(`jarvis-frontend` `src/shared/types/chat.ts`의 `ChatAction`)·서버가 실제로 내보내는 프레임 셋 다 number이며, 이 사본만 낡은 드리프트였다.

실패 예:

```json
{
  "type": "action",
  "data": { "type": "CART_ADD_FAILED", "message": "해당 상품을 찾지 못했어요.", "reason": "PRODUCT_NOT_FOUND" }
}
```

재고 부족 예(남은 재고 수 노출):

```json
{
  "type": "action",
  "data": { "type": "CART_ADD_FAILED", "message": "재고가 3개뿐이에요.", "reason": "STOCK_INSUFFICIENT" }
}
```

**삭제(I-24, §4.12, 🔶 확정 2026-08-05 — Spring 구현 진행 중, AI 구현됨 #116)**:

```json
{ "type": "action", "data": { "type": "CART_REMOVED", "message": "장바구니에서 뺐어요: 여행용 방수 파우치", "cartItemId": 55 } }
```

- `CART_REMOVED` — I-24 성공. **404 `CART_ITEM_NOT_FOUND`도 이 이벤트로 정상 종료**("이미 빠져 있어요: {상품명}") — 실패 `action`을 내지 않는다.

```json
{ "type": "action", "data": { "type": "CART_REMOVE_FAILED", "message": "빼지 못했어요: 여행용 방수 파우치. 잠시 후 다시 시도해 주세요.", "cartItemId": 55, "reason": "CART_ERROR" } }
```

- `CART_REMOVE_FAILED` — 그 밖의 실패. `reason: "CART_ERROR"`.

**찜 추가(I-26, §4.14, 🔶 확정 2026-08-05 — Spring 구현 진행 중, AI 구현됨 #117)**:

```json
{ "type": "action", "data": { "type": "WISHLIST_ADDED", "message": "찜해 뒀어요." } }
```

- `WISHLIST_ADDED` — I-26 성공. **409 `WISHLIST_DUPLICATE`·`RESOURCE_CONFLICT`도 이 이벤트로 정상 종료**("이미 찜해 두셨어요."). **찜 이벤트에는 `productId`를 싣지 않는다(경로 B)** — FE는 `type`만 보고 찜 목록을 재조회한다.

```json
{ "type": "action", "data": { "type": "WISHLIST_ADD_FAILED", "message": "해당 상품을 찾지 못했어요.", "reason": "PRODUCT_NOT_FOUND" } }
```

- `WISHLIST_ADD_FAILED` — 404는 `reason: "PRODUCT_NOT_FOUND"`, 그 밖은 `reason: "WISHLIST_ERROR"`.

**찜 해제(I-27, §4.15, 🔶 확정 2026-08-05 — Spring 구현 진행 중, AI 구현됨 #117)**:

```json
{ "type": "action", "data": { "type": "WISHLIST_REMOVED", "message": "찜 목록에서 뺐어요: 여행용 방수 파우치" } }
```

- `WISHLIST_REMOVED` — I-27 성공. **404 `WISHLIST_NOT_FOUND`도 이 이벤트로 정상 종료**("이미 찜 목록에 없어요: {상품명}") — I-27은 상품 존재를 보지 않아 "없는 상품"과 "안 찜한 상품"을 구별할 수 없다(§4.15).

```json
{ "type": "action", "data": { "type": "WISHLIST_REMOVE_FAILED", "message": "빼지 못했어요: 여행용 방수 파우치. 잠시 후 다시 시도해 주세요.", "reason": "WISHLIST_ERROR" } }
```

- `WISHLIST_REMOVE_FAILED` — 그 밖의 실패. `reason: "WISHLIST_ERROR"`.

**수량 변경(I-25, §4.13, 🔶 확정 2026-08-05 — Spring 구현 진행 중, **AI 미구현** — 대응 이슈 없음)**:

```json
{ "type": "action", "data": { "type": "CART_QUANTITY_CHANGED", "message": "수량을 3개로 바꿨어요.", "cartItemId": 55, "quantity": 3 } }
```

```json
{ "type": "action", "data": { "type": "CART_QUANTITY_CHANGE_FAILED", "message": "재고가 3개뿐이에요.", "cartItemId": 55, "reason": "STOCK_INSUFFICIENT" } }
```

- `CART_QUANTITY_CHANGED` / `CART_QUANTITY_CHANGE_FAILED` — I-25. `cartItemId`(number)·`quantity`(number, 성공 시 최종 수량). 재고 부족은 담기와 같은 `reason: "STOCK_INSUFFICIENT"`를 재사용한다(신규 코드 없음).

| 필드 | 타입 | 설명 |
|---|---|---|
| `type` | `"CART_ADDED"` \| `"CART_ADD_FAILED"` \| `"CART_REMOVED"` \| `"CART_REMOVE_FAILED"` \| `"WISHLIST_ADDED"` \| `"WISHLIST_ADD_FAILED"` \| `"WISHLIST_REMOVED"` \| `"WISHLIST_REMOVE_FAILED"` \| `"CART_QUANTITY_CHANGED"` \| `"CART_QUANTITY_CHANGE_FAILED"` | 담기·삭제·수량 변경·찜 추가·찜 해제 결과 **10종**(v0.15.16 이전은 2종) |
| `message` | string | 사용자 노출 안전 문구 |
| `cartItemId` | **number(BIGINT)** \| 없음 | 성공/실패 시 대상 장바구니 항목 식별자(I-2·I-24·I-25 응답 체계와 동일, §2.6) — 찜 이벤트(`WISHLIST_*`)에는 없음 |
| `quantity` | number \| 없음 | `CART_QUANTITY_CHANGED`/`CART_QUANTITY_CHANGE_FAILED` 전용(I-25, 🔶 AI 미구현) |
| `reason` | string \| 없음 | 실패 시 사유 코드 |

- **`reason` 허용값(v0.22.0 확장)**: `PRODUCT_NOT_FOUND` / `STOCK_INSUFFICIENT` / `CART_ERROR` / **`WISHLIST_ERROR`**(신규) **4종**. `STOCK_INSUFFICIENT` = 합산 수량 > 재고(BE `400 CART_STOCK_INSUFFICIENT` + `error.detail.availableStock`, 2026-07-22 신설, 재고는 상품 단위) → AI가 message에 남은 재고 수를 실어 안내("재고가 N개뿐이에요"; **재고 0=품절이면 "품절된 상품이에요"**, §4.1). ~~`OUT_OF_STOCK`~~은 **폐기 유지** — 품절(stock 0)도 `STOCK_INSUFFICIENT`(availableStock:0)로 통합. 수량 상한(합산 > 99)은 BE `VALIDATION_ERROR`로 별개 — AI는 `CART_ERROR` + BE 동일 문구 "수량은 최대 99개까지 담을 수 있습니다."로 안내. ~~`GUEST_NOT_ALLOWED`~~는 **폐기** — 게스트도 담기 허용(v0.6.0, 결정 8 개정 필요 §8 항목 7). `WISHLIST_ERROR`는 찜 추가/해제 실패 중 (찜 추가의) 404 `PRODUCT_NOT_FOUND` 이외 전부를 포괄한다.
- **옵션 되물음은 `action` 실패가 아니다** — I-2가 `400 CART_OPTION_REQUIRED`(options 목록 포함)를 반환하면 AI는 실패 `action`을 emit하지 않고 **`token` 텍스트로 옵션을 되묻는 멀티턴**으로 이어간다(§4.1). 사용자가 옵션을 답하면 `optionId`를 해석해 재담기한다. **옵션 후보가 1개면 되묻지 않고 자동 선택해 같은 턴에 담기까지 마친다**(v0.17.2 #114) — 이 턴은 `token` 되물음이 아니라 `action`(`CART_ADDED`)으로 끝난다. FE 관점의 이벤트 문법은 그대로다(담기 턴은 원래 되물음 `token` 또는 결과 `action` 중 하나로 끝난다) — 새 이벤트·필드·순서 규칙은 없다.
- **장바구니 조회 응답("장바구니에 뭐 있어?")도 별도 이벤트 없이 `token` 텍스트**로 답한다(§4.9). **찜 목록 조회도 동형**("내가 뭐 찜했지?") — 별도 이벤트 없이 `token` 텍스트로 답한다(§4.16).
- **게스트 찜 발화는 `action`을 내지 않는다** — 찜은 회원 전용(I-26/I-27, M-4)이라 게스트 발화는 internal 호출 없이 `token` 로그인 안내로 degrade한다.
- **AI 구현 상태**: 삭제(`CART_REMOVED`/`CART_REMOVE_FAILED`)·찜 추가/해제(`WISHLIST_*`) **6종은 구현됨**(#116·#117, **라운드 23부터 기능 플래그 없이 항상 emit** — 이전에 있던 두 개의 온/오프 설정 필드는 삭제됐다). **수량 변경(`CART_QUANTITY_CHANGED`/`CART_QUANTITY_CHANGE_FAILED`) 2종은 미구현**(대응 이슈 없음, I-25는 이 레인 범위 밖).
- **Spring 구현 상태**: I-24~I-28 **모두 구현 진행 중**이다(🔶 확정 2026-08-05, BE 협의 완료·초안 그대로 채택) — 배포 전에는 호출해도 응답하지 않는다.
- **FE 반영 상태**: `jarvis-frontend`의 `ChatAction` 유니온에 신규 8종(`CART_REMOVED`~`WISHLIST_REMOVE_FAILED`)이 **아직 없다** — FE 수신부 추가가 선행되어야 신규 `action`이 화면에 반영된다.

**(5) `products.ready`** — AI가 추천 목록을 Spring에 push한 뒤 emit (정확히 1회, 성공 시).

```json
{ "type": "products.ready", "data": { "sessionId": "550e8400-e29b-41d4-a716-446655440000", "listIds": ["3f9a2c1e7b8d4e5fa0c6d1e97b3f8a24"] } }
```

여러 묶음(세트형·니즈별) — I-21이 `lists`를 2개 보냈으면:

```json
{ "type": "products.ready", "data": { "sessionId": "550e8400-…", "listIds": ["9f2c1a7e4b8d43f5a0c6e1d97b3f8a24", "4b8d43f5a0c6e1d97b3f8a249f2c1a7e"] } }
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `sessionId` | string | 상관관계 키(요청과 동일) |
| `listIds` | string[] | FastAPI 생성 목록 식별자 배열(§4.2 I-21의 `lists` 순서) — reason 포함 카드는 CH-5로 조회(§4.3) |

- **[v0.15.26] `listIds`는 항상 배열이다.** 목록이 1개여도 길이 1 배열로 보낸다 — 단일/복수로 형식이 갈리면 FE에 분기가 생긴다. **이벤트 자체는 여전히 정확히 1회**이며, 목록이 여러 개여도 한 번에 실어 보낸다. 개수 상한은 10(§4.2 `lists` 상한과 동일).
- FE는 **각 `listId`로 CH-5를 개별 호출**한다. `listType`·`label`·`totalBudget`은 싣지 않는다 — CH-5 응답에 있다.
- **[v0.15.26 정정] 구 단일 `listId` 필드는 폐기한다.** I-21이 `lists`를 1~10개 보내므로(§4.2) 단일 필드로는 세트형·니즈별 추천을 나를 수 없었다. 예시의 `"list-4471"`도 §4.2가 금지한 추측 가능 형식(순번)이라 ≥128bit 무작위로 교정했다. **구현도 단일**이라(`ProductsReadyData.list_id: str`) 코드 변경이 따라야 한다.
- FE는 `products.ready` 수신 시 §4.3 목록 GET으로 Spring이 표시 필드를 채운 목록을 조회해 우측 상품 패널을 렌더한다(§6.1).
- **push 실패 시**: `products.ready`는 emit되지 **않는다.** 챗 텍스트는 정상 완료되고 지연 안내가 포함되며, 스트림은 `error`가 아니라 **`done`** 으로 종료한다(§3.3).

**(6) `done`** — 정상 종료

```json
{ "type": "done", "data": { "finishReason": "stop" } }
```

- `finishReason`: `"stop"`(정상 완료) \| `"zero_result"`(0건). **0건은 오류가 아니라 정상 종료**이며 FE는 우측 패널을 빈 상태(empty state)로 전환한다.
- 재랭킹(rerank) 실패 또는 목록 push 실패도 `error`가 아니라 `done`으로 종료한다(degrade 정책, §3.3).

**(7) `error`** — 오류 종료 (스트림 내부)

```json
{
  "type": "error",
  "data": {
    "code": "LLM_TIMEOUT",
    "message": "일시적으로 응답이 지연됐어요.",
    "requestId": "9f2c1a7e4b8d43f5a0c6e1d97b3f8a24",
    "retryable": true
  }
}
```

- **스트림 내부 `error.code` 허용값(4종)**: `LLM_TIMEOUT` / `LLM_UNAVAILABLE` / `SEARCH_FAILED` / `INTERNAL`.
- **[v0.15.26] `requestId`** — 이 요청의 추적 id. 스트림 시작 **전** 실패는 §2.5 오류 봉투에 이미 `requestId`를 싣는데 스트림 **내부** 실패에는 없어, 정작 사용자가 "오류 났어요"라고 신고하는 쪽을 로그에서 찾을 수 없었다. 봉투와 같은 값을 쓴다.
- **[v0.15.26] `retryable`** (boolean) — FE가 재시도를 권할지 판단하는 근거. **`code`로는 복원할 수 없다** — 같은 `LLM_UNAVAILABLE`이라도 "LLM 미구성"(재시도 무의미)과 "모델 일시 불가"(재시도 유효)가 섞이므로 **emit 지점이 값을 정한다**.
- **판매자 스트림(§3.2)도 동일** — `error` 페이로드는 구매자·판매자 공용 스키마다(`ErrorData`).
- **단계별 상세는 서버 로그 전용** — decompose/rerank 등 스테이지 단위 실패 코드는 사용자 스트림에 노출하지 않는다. rerank 실패는 검색 상위로 degrade 후 `done`으로 종료한다(하드 제약 유지).

#### MVP 추가 페이로드 — SSE 측 탑재 (구매자)

FE/BE 문서에 없으나 MVP에 필요한 아래 3종은 **모두 구매자 SSE 측에 실린다**(표시 필드는 Spring, 추천 로직 산출물은 AI 경계 유지):

- **`suggestions`(제안 칩)** — 0건 완화 제안 + 구매 이력 되돌리기(결정 14-D/14-F). 전용 이벤트 `suggestions`로 emit(제안(초안)).

```json
{
  "type": "suggestions",
  "data": {
    "chips": [
      { "label": "6만원대까지 볼까요?", "relaxation": { "field": "priceMax", "value": 65000 }, "estCount": 12 },
      { "label": "소금은 최근 구매 — 다시 추천받기", "revert": { "category": "조미료" }, "estCount": 8 }
    ]
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `label` | string | 칩 문구 |
| `relaxation.field` | string | 완화 대상 필드 |
| `relaxation.value` | any | 제안 값 |
| `revert.category` | string | 구매 이력 억제 되돌리기 대상 카테고리(결정 14-F) |
| `estCount` | int | 완화/재포함 적용 시 예상 결과 수(COUNT). `estCount == 0`인 칩은 제외 |

- **`relaxationNotice`(자동 완화 투명 안내)** — 0건 자동 완화 적용 시 안내(결정 14-D). 안내 산문은 **`token`으로 스트리밍한다.** **⚠️ [사본 drift 정정 v0.19.1, #113] 구 서술의 *"기계 판독 플래그가 필요하면 `done.data.relaxationNotice: string | null`로 병기(제안(초안))"* 는 폐기한다** — 정본(Notion "📡 API 명세서" CH-2)이 `done` 페이로드를 `finishReason` **하나로 확정**했고(정본 「구 명세 대비 정정 요약」: *"done: relaxationNotice 제거 → finishReason만"*), FE 타입(`jarvis-frontend` `src/shared/types/chat.ts`)의 `done` 도 `{finishReason, panel?}` 뿐이라 **이 필드를 읽는 소비자가 없다.** 본 사본만 제안(초안) 상태로 남아 있었고, 이슈 #113 이 그 낡은 서술을 근거로 구현했다가 되돌렸다. 자동 완화 고지 의무(REQ-REC-042)는 `token` 산문이 그대로 이행하므로 사용자 고지에는 변화가 없다. FE 가 문장 파싱 없이 분기할 근거가 필요해지면 **정본 개정이 선행**되어야 한다.
- **총액 예산 요약(BudgetSummary)** — Case 3 총액 예산(결정 14-A). 전용 SSE 이벤트 `budget`. **⚠️ [제외 v0.15.26] 정본(Notion CH-2)은 이 이벤트를 명세에서 제외했다** — *"현재 코드에 미구현 → 필요 시 post-MVP"*. 아래 스키마는 **post-MVP 참고용**으로만 남긴다. 구현 전까지 FE는 이 이벤트를 기다리지 않으며, 이벤트 순서 계약에서도 빠진다(이슈 #163):

```json
{
  "type": "budget",
  "data": { "totalBudget": 50000, "verifiedSum": 47800, "withinBudget": true, "droppedItems": [], "feasibilityNotice": null }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `totalBudget` | int | 묶음 총액 상한 |
| `verifiedSum` | int | 코드가 **인덱스 price**로 결정론 합산한 값(LLM 산수 아님) |
| `withinBudget` | bool | `verifiedSum <= totalBudget` |
| `droppedItems` | string[] | 예산 초과 제외 아이템 label |
| `feasibilityNotice` | string \| null | 부분 충족 안내 |

> **[주의] `verifiedSum`은 검색 응답(§4.6) 가격 기준**이다(질의 시점 Spring 가격이라 신선함). 다만 경로 B에서 표시 가격은 Spring이 목록 GET 시점에 다시 채우므로(§4.3), 검색~표시 사이 가격 변경 시 SSE `budget`과 우측 패널 표시가가 순간 괴리할 수 있다(SPEC-RECOMMEND-001 OPEN-11). 예산 표시 정책은 🔴 기획·Spring 협의(§8 항목 2).

#### 이벤트 순서 계약

정상 흐름(추천): **[확정 v0.21.0, 이슈 #289 / 다회 emit v0.27.0, 이슈 #396]** `progress`(**0회 이상**, 나가면 첫 `progress`가 스트림 첫 프레임) → `conditions`(0~1회) → `token`(0회 이상) + `suggestions`(해당 시) → `products.ready`(성공 시 정확히 1회) → `done`(1회). `progress`는 추가 전용이며 **기존 6종의 상대 순서는 불변**이다 — `conditions`는 여전히 검색·자동 완화 뒤다(§2.9 c). **`progress`는 `token`·`suggestions` 사이사이에도 낄 수 있다** — 예를 들어 `publishing` 은 근거 `token` 이 나간 뒤 `products.ready` 직전에 온다. **[v0.15.26] `budget`은 순서 계약에서 제외**했다(정본이 명세에서 제외 — 미구현, post-MVP). 장바구니 흐름: (`progress` →) `token`/`action` → `done`. `products.ready`는 목록 push 성공 이후에만 나타난다.

#### 오류/degrade 동작 (참고)

- `search` 실패: `SEARCH_FAILED` `error` 이벤트, 후보 날조 없음.
- `rerank` 실패 또는 출력 검증 실패: 검색 상위 5~9개로 degrade, 하드 제약(예: `priceMax`) 유지, `error`가 아닌 `done` 종료. **[v0.19.2 #133] 이때 품질 저하를 `token`으로 고지한다** — 개인화와 상품별 근거(I-21 `reasons`)가 함께 사라지는데 종전 문구는 정상 경로와 구분되지 않았다. 문안은 AI config 주입이며 **실패 단계명·오류 코드는 싣지 않는다**(아래 "단계별 상세는 서버 로그 전용" 유지). 이벤트 타입·필드·순서는 불변이라 **FE 변경 없음**. **[v0.17.3] 상한을 8→9로 상향** — §4.2가 목록당 9개를 허용하므로 노출 상한을 계약 상한과 같은 값으로 맞춘다. 이 개수는 **목록 하나 기준**이며, 목록이 여럿이면 목록마다 이 상한이 걸린다.
- 목록 push(§4.2) 실패: 챗 텍스트 정상 완료 + 지연 안내 + `done`(no `products.ready`)(§3.3).
- LLM 타임아웃/불가용: `LLM_TIMEOUT`/`LLM_UNAVAILABLE` `error` (기준값·재시도는 §2.9 c).
- 스트림 중 소비자 abort(HTTP 연결 종료): 진행 중 LLM 호출 취소 — 취소 의미론·부분 텍스트 저장은 §2.9 b·§6.3.
- 동시 스트림·타임아웃 수명주기 전반은 **§2.9**(v0.7.0, `/seller/chat` 공통).

### 3.2 `POST /seller/chat` — 판매자 챗봇 (SSE 스트리밍, FE 직접) — [v0.4.0 확대, Batch 1]

입점 판매자의 (a) 매출/판매 통계 자연어 질문과 (b) 상품 상세 수정 요청을 처리한다. **MVP 범위 확대**: 통계 Q&A **+ 상세 수정 draft 흐름**. 소유 SPEC은 별도(판매자 그래프 SPEC, 미작성).

> **인증**: `role == "seller"` 필수. 판매자 스코프가 없는 토큰의 호출은 `403 FORBIDDEN`(§2.3 a).
>
> **응답 형식**: `/chat`과 일관성을 위해 **SSE 스트리밍**. 이벤트는 `meta`/`progress`/`token`/`draft`/`report`/`done`/`error` 7종만 쓴다(v0.24.0) — `products.ready`·`conditions`·`suggestions`·`budget`·`action`은 판매자 스트림에서 **emit하지 않는다**(§2.2). `done.finishReason`은 `"stop"` **하나뿐**이다(`zero_result` 없음).

#### 요청 (Request) — 제안(초안)

**(a) 일반/제안 요청**

```json
{
  "sessionId": "string",
  "threadId": "string",
  "message": "string"
}
```

**(b) 승인 요청(confirm) — [확정 2026-07-22, A-2]**

```json
{
  "sessionId": "string",
  "threadId": "string",
  "action": "confirm",
  "draftId": "string"
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string | 예 | **접속 식별자**(Spring CH-6 발급, §2.6). 프로필 세션버퍼·세션 종료 통지의 키 |
| `threadId` | string | 예 | **방 식별자**(FE 생성, §2.6). 멀티턴 맥락·draft·**동시 스트림 락**(§2.9 a)의 키 — 판매자도 워크스페이스 탭을 여러 개 열 수 있어 축 분리가 동일하게 적용된다 |
| `message` | string | 예¹ | 통계 질문("이번 주 매출 어때?") 또는 상세 수정 요청("이 상품 설명 더 매력적으로 바꿔줘") |
| `action` | `"confirm"` | 아니오 | **[확정 v0.14.1]** HITL 승인 신호. draft에 대한 `[적용]`. 지정 시 `draftId` 필수 |
| `draftId` | string | 조건부 | `action == "confirm"` 일 때 실행할 draft 식별자(스트림1 `draft.draftId`). 누락 시 `400 BAD_REQUEST` |
| `screen` | object | 아니오 | **[v0.15.26 신설]** 현재 보고 있는 화면. **구매자와 공용 필드로 정의는 §3.1을 따른다**(`pageType`·`filters`·`products`·`columns`). (a)·(b) 어느 요청에도 실을 수 있다 |

> **[v0.15.26] `screen`이 판매자에도 필요한 이유** — 판매자 대시보드는 좌(채팅)/우(패널) 분할이라 **화면이 이미 계약의 일부**인데, 그동안 방향이 한쪽뿐이었다. **AI→FE**는 `meta.lane`(레이아웃 준비)·`done.panel`(`replace`/`keep`/`refresh`)로 화면을 조작하는데 **FE→AI**(지금 무엇을 보고 있나)가 없었다 — 서버가 `panel="refresh"`로 우측 재조회를 지시하면서 그 패널에 무엇이 떠 있는지는 모르는 상태다. `pageType`·`filters`가 있으면 "이 목록 왜 비어?" 같은 지시어 질문에 답할 수 있고, 지시가 타당한지도 판단할 수 있다. **`conditionActions`는 구매자 전용**이라 판매자 요청에 싣지 않는다 — `conditions` 이벤트 자체가 판매자 스트림에 없다(§2.2).

> ¹ `message`는 일반 발화에서 필수다. 승인 요청(`action == "confirm"`)에서는 비워도 된다 — 승인은 발화가 아니라 구조화 신호이기 때문(HITL 안전장치 ②, 발화 ≠ 동의).

> **[보안] `sellerId`·`brandId`는 요청 본문에 없다** — 판매자 식별자는 JWT `sub`·`brandId` 클레임(+`role == "seller"`)에서만 추출한다(사칭 방지, §2.3 a). AI는 `brandId`를 **검증된 토큰에서** 얻어 집계 역호출(§4.4)의 `{brandId}` path에 쓴다 — 사용자 입력 brandId는 신뢰하지 않는다(§2.6).

#### 응답 (Response) — `text/event-stream`

**[확정 v0.24.0, 2026-08-05 — 이슈 #296 개정]** 판매자 스트림은 이벤트 7종을 쓴다: `meta`·`progress`·`token`·`draft`·`report`·`done`·`error`. FE 대시보드가 좌(채팅)/우(패널) 분할이라, 서버가 우측 패널 조치를 명시한다. (구 `chart` 는 `report` 로 대체·legacy 폐기 — 아래 `report` 절.)

- **`meta`** (매 스트림 첫 프레임): `{ "type":"meta", "data":{ "lane": "analysis"|"product"|"general"|"confirm"|"apply"|"refused" } }`. FE 가 레인을 즉시 알아 레이아웃 전환·로딩을 준비한다.
- **`progress`** (analysis 진행, 0회 이상): `{ "type":"progress", "data":{ "text": "…" } }`. 로딩 표시 — 최종 답변이 아니다(`token` 과 분리).
- **`report`** (analysis 결과, 리포트일 때 정확히 1회) — **[신설 v0.24.0, 이슈 #296]** 우측 패널용 구조화 보고서(기간·요약·findings·데이터 한계·차트·추천 내장). `token`(리포트 산문) 뒤·`done` 앞에 온다. 되묻기·사과·거절에는 없다. 상세는 아래 `report` 절.
- **`done`** (종료): `{ "type":"done", "data":{ "finishReason":"stop", "panel":"replace"|"keep"|"refresh" } }`. `panel` 이 우측 패널 조치를 확정한다 — `replace`(리포트·diff 카드로 교체)·`keep`(유지)·`refresh`(쓰기 반영 → 재조회). `error` 로 끝나면 `done` 이 없고 패널은 유지한다.
- 구현: `app/api/seller.py`. 구매자 `done`(§3.1)에는 `panel` 이 없다 — 판매자 전용 필드다.

**(1) 통계 Q&A 흐름**: `meta{analysis}` → `progress`×N → `token`(리포트 산문) → `report`(정확히 1회) → `done{panel:"replace"}`. 되묻기(기간 불명 등)는 `token` → `done{panel:"keep"}` — `report` 없음. 좌측 채팅에는 `token` 산문이, 우측 패널에는 `report` 구조화 데이터가 쓰인다(같은 검증된 보고서의 두 표현 — 산문은 스레드 기록·후속 발화 맥락의 원천이라 유지한다). 원천은 **I-6 집계 콜백**(§4.4·아래 데이터 소스). **차트**는 판매자가 명시·암시로 요청했을 때만(`wants_chart`, planner 판정 + 코드 키워드 검사 OR) `report.data.charts[]` 에 내장돼 전달된다(원천은 이미 검증된 finding·보고서 — 새 조회 없음, 이슈 #242·#296).

**(2) 상품 수정/등록/삭제 흐름 — [확정 v0.11.0]**: 판매자 `product_agent`가 상품 쓰기를 **AI가 Spring internal API로 직접 수행**하되, **모든 쓰기는 HITL 승인 게이트를 통과**한다. 채팅 경로 쓰기는 AI가, FE에서 직접 편집하는 경로는 FE↔Spring(AI 표면 밖)이 담당한다.

**2-스트림(interrupt/resume) 흐름** — SSE 1스트림 = 응답 1회(§2.9)라 승인 대기를 한 연결에 물지 않고 끊고-재개한다:
```
[스트림 1 · 제안]  meta{product} → draft{draftId, op, changes} → (LangGraph interrupt, 상태 checkpointer 저장) → done{panel:"replace"}
                   FE: diff 카드 + [적용]/[취소]  (product 레인은 근거 token 없음 — draft.summary 가 요약)
[스트림 2 · 승인·실행]  FE가 {action:"confirm", draftId} 전송 → meta{confirm} → 그래프 resume → AI가 I-10/I-11/I-12 호출(§4.5) → token(결과) → done{panel:"refresh"(실행)|"keep"(변경없음)}
```
- **읽기(before)**: 대상 확인은 **I-9 자사 상품 목록**(§4.5)으로 조회.
- **HITL 안전장치 [HARD]**:
  1. **승인은 `draftId`에 바인딩** — confirm은 그 draft를 참조해 **보여준 diff == 실행하는 쓰기**를 보장(다중 draft·불일치 방지). 상태 checkpointer가 실제 변경분을 보유.
  2. **명시 액션만 승인** — confirm은 **구조화 신호**(`{action:"confirm", draftId}`)여야 하며 자유 텍스트는 승인 아님(발화 ≠ 동의).
  3. **멱등성** — 동일 `draftId` 재전송은 1회만 실행(더블클릭 방지).
  4. **Spring 소유권이 하드 게이트** — HITL 우회해도 Spring이 `brandId`(JWT)로 상품 귀속 검증(§4.5). HITL은 사람-안전, Spring authz는 최종 방어.
  5. **대기 TTL** — 미승인 draft는 N분 후 만료(checkpoint TTL) — 지연 승인 방지.
- **삭제(I-12)는 필수 HITL** — soft delete(`status=HIDDEN`, 물리 삭제 없음). HITL(그래프) + soft delete(데이터) 이중 방어. (MVP는 전 쓰기 단순 `[적용]` 확인, 삭제만 문구 강조 권장.)
- **`draftId`는 선택적 권장** — 제안이 항상 하나·즉시 승인이면 checkpointer만으로도 동작하나, 다중 draft·멱등 대비로 부여를 권장.
- **confirm 전송 형식 = [확정 v0.14.1, 2026-07-22]** 요청 본문 **최상위 `action`/`draftId` 필드**(위 요청 (b)). 구 "message 문자열에 JSON 을 실어 파싱" 방식은 폐기 — FE 가 message 를 이스케이프하지 않는다. AI 코드 정합 완료(`app/schemas/seller.py::SellerChatRequest`, `app/api/seller.py`). HITL 승인은 별도 이벤트명 없이 스트림2가 `token`(결과)+`done` 으로 응답한다.
- **confirm 결과는 전부 HTTP 200 [확정 v0.14.1]** — 실행/만료/미존재/소유불일치/중복(멱등)/stale 모두 SSE `token`(안내)+`done` 으로 온다(HTTP 오류 아님). 실제 쓰기만 `done{panel:"refresh"}`, 나머지는 `done{panel:"keep"}`. 소유 불일치는 미존재와 동일 문구(존재 비노출). Spring 장애만 `token`+`error{INTERNAL}`(초안 유지, 재confirm 가능). 구 "409 `DRAFT_EXPIRED`/`DRAFT_NOT_FOUND`" 표기는 폐기.
- **스트림 시작 전 거부(HTTP 오류 봉투 §2.5)**: `400 BAD_REQUEST`(필드 누락·`action=="confirm"`인데 `draftId` 없음, `RequestValidationError`→400)·`401 TOKEN_EXPIRED`/`TOKEN_INVALID`(누락/형식·seller `sub`/`brandId` 오류 포함)·`403 FORBIDDEN`(role≠seller)·`409 STREAM_IN_PROGRESS`(동일 threadId 동시 스트림)·`429 RATE_LIMITED`(config 상한·`/seller/chat` 적용)·`503 STATE_UNAVAILABLE`(대화 store 정본 사용 불가 — 구매자 `/chat`과 동일 판정, timeout/pool/connection 장애만 해당하며 programming/domain 오류를 마스킹하지 않는다)·`504 UPSTREAM_TIMEOUT`.

**`draft`** — 상세 수정 개정안 (정확히 1회)

```json
{
  "type": "draft",
  "data": {
    "draftId": "draft-8f21",
    "op": "update",
    "productId": 10293,
    "changes": [
      { "field": "description", "before": "여행용 방수 파우치입니다.", "after": "우천·수영장에도 안심인 IPX8 방수 파우치. 여권·전자기기를 완벽 보호합니다." },
      { "field": "name", "before": "방수 파우치", "after": "여행용 IPX8 방수 파우치" }
    ],
    "summary": "상품명·설명을 방수 성능 중심으로 개선"
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `draftId` | string | 이 제안의 식별자. 승인(confirm)이 이 값을 참조해 **보여준 것 == 실행하는 것**을 보장하고 다중 draft·중복 승인을 구분한다(아래 HITL). 서버 발급 UUID |
| `op` | `"update"` \| `"create"` \| `"delete"` \| `"ship"` | 실행할 쓰기 종류(I-11/I-10/I-12/**I-30** 매핑). **[v0.25.0, #297] `ship`(주문 발송 처리) 추가 — 🔶 초안, BE 협의 전. 정본(노션 CH-2/S-4) 개정은 후속(확정 2026-08-04)** |
| `productId` | number | 대상 상품 식별자(숫자 BIGINT, §2.6). `create`는 없을 수 있음(`null`), **`ship`은 항상 `null`** |
| `orderItemId` | number | **[v0.25.0, #297] `ship` 전용 키(그 외 op 에는 키 자체가 없다 — 추가 전용)** — 발송 대상 주문 아이템(숫자 BIGINT). I-29(§4.18) 조회 결과에서 해소한 값 |
| `changes` | array | 필드별 변경 제안 배열. **`ship`은 빈 배열** — 전이는 ORDERED→SHIPPING 고정이라 diff 가 없고, 카드 내용은 `summary`가 담당한다 |
| `changes[].field` | string | 수정 대상 필드 — **camelCase** 8종: `name`·`price`·`originalPrice`·`description`·`category`·`imageUrl`·`status`·`stockQuantity` (와이어 규약 §2.2, [C-1 2026-07-22]) |
| `changes[].before` | string | I-9(목록)로 읽은 현재 값. `create`는 `""` |
| `changes[].after` | string | LLM 생성 개정안 |
| `summary` | string | diff 카드 부제용 한 줄 요약(`ship`은 주문번호·상품명·옵션 등 발송 대상 서술) |

- **FE 렌더링**: FE는 `draft`를 **diff 카드**로 렌더하고 `[적용]`/`[취소]` 버튼을 노출한다(승인 UI).
- **[HARD, 개정 v0.9.0] 반영은 AI가 Spring internal API로 직접 수행** — 판매자 승인(HITL) 후 AI가 **I-11 PATCH(수정)/I-10 POST(등록)/I-12 DELETE(삭제)**(§4.5, `X-Internal-Token`+`{brandId}`)를 호출한다. **구 "FE가 본인 JWT로 S-3 PATCH" 모델은 폐기** — [BE 실측 정정 v0.13.0] **S-3 = `GET /api/seller/products`(SELLER, FE→Spring)** 로 **판매자 본인 FE 대시보드용** 목록이고, AI가 쓰는 목록은 **I-9 `GET /internal/seller/{brandId}/products`(서비스 토큰, AI→Spring)** 로 **별개**다(둘 다 조회, 레인만 다름). 즉 S-3는 PATCH가 아니며 I-9와 동일 엔드포인트도 아니다. 채팅 경로의 쓰기는 AI가 internal API(I-11 등)로 수행한다. (판매자가 **FE에서 직접** 상품을 편집하는 경로는 FE↔Spring 별개, AI 표면 밖.) 반영 결과는 `token`으로 안내.
- **[HARD] 대화 발화는 동의로 취급하지 않는다** — 채팅의 모호한 발화("응 바꿔")는 승인이 아니다. 반영의 유일한 경로는 **HITL 명시 승인**(아래).
- **[v0.25.0, #297] `op="ship"`(주문 발송, 🔶 초안)**: 흐름은 상품 쓰기와 동일하게 **기존 `product` 레인을 재사용**한다(확정 2026-08-04 — meta.lane 신설 없음). `meta(lane:"product")` → `draft{op:"ship", orderItemId}` → interrupt → confirm → **I-30 PATCH**(§4.19) → `done(panel:"refresh")`. confirm 의 실패적 결과(만료·이미 발송 409·전이 불가 400·소유 불일치 404)는 S-4 확정 규칙 그대로 **HTTP 200 + 안내 `token` + `done(panel:"keep")`** 이며, HITL 5대 안전장치(draftId 바인딩·명시 액션만·멱등성·Spring 하드게이트·TTL)가 동일하게 적용된다. 발송 대상 해소는 정본(노션 I-30) 설계상 `screen`(pageType=`seller_orders`) 맥락 우선, 부족하면 I-29(§4.18) 조회다 — **🔶 단, screen 경로는 AI 미구현이다(2026-08-05 현재): product 레인은 screen 을 draft 에이전트 입력에 주입하지 않아(§3.2 판매자 screen 주입은 supervisor·analysis 한정, #118) 현재는 I-29 조회로만 해소한다. screen 주입은 후속 작업.**

**`report`** — 구조화 분석 보고서(리포트 결과일 때 정확히 1회, 차트 내장) — **[신설 v0.24.0, 이슈 #296 — 구 `chart`(v0.20.0) 대체]**

```json
{
  "type": "report",
  "data": {
    "title": "판매 분석 보고서",
    "period": { "from": "2026-07-01", "to": "2026-07-31" },
    "generatedAt": "2026-08-05T10:12:00+09:00",
    "summary": "핵심 요약 2~3문장 — 보고서 첫 문단.",
    "body": "검증된 보고서 산문 전문 …",
    "findings": [
      {
        "analysisType": "sales_anomaly",
        "severity": "warning",
        "summary": "07-12 매출이 평균 대비 32% 낮았습니다.",
        "evidence": ["07-12 매출 1,250,000원 (평균 1,850,000원)"],
        "recommendation": "해당일 프로모션 부재 여부를 확인하세요."
      }
    ],
    "limitations": ["데이터 확보 실패 — 분석 실행 오류(응답 시간 초과)"],
    "chartRequested": true,
    "charts": [
      {
        "title": "일별 매출",
        "chartType": "line",
        "unit": "KRW",
        "series": [
          { "label": "매출", "points": [{ "x": "07-01", "y": 1240000 }] }
        ],
        "summary": "6월 대비 12% 감소"
      }
    ],
    "recommendations": [
      {
        "index": 1,
        "title": "감귤청 가격 10% 인하",
        "expectedEffect": "가격 민감 이탈 고객 재유입",
        "actionType": "price_adjust",
        "productId": 10293
      }
    ],
    "applyGuide": "적용을 원하시면 \"N번 적용해줘\"라고 말씀해 주세요."
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `title` | string | 보고서 제목 — 현재 서버 고정값 `"판매 분석 보고서"` |
| `period.from`/`period.to` | string(`YYYY-MM-DD`) | 분석 대상 기간 — 코드 환산 결과(planner 기간 표현의 확정값) |
| `generatedAt` | string(RFC3339) | 보고서 생성 시각 — KST(+09:00) |
| `summary` | string | 핵심 요약 — 보고서 첫 문단을 코드가 분리(실패 시 앞 200자 절단) |
| `body` | string | 검증된 보고서 산문 **전문** — 좌측 `token` 과 동일 원천(추천·안내 문구 제외) |
| `findings` | array | 검증된 분석 결과 목록 — degrade(확보 실패) finding 포함 |
| `findings[].analysisType` | `"sales_anomaly"` \| `"conversion"` \| `"behavior"` \| `"churn"` \| `"abuse"` | 분석 유형 |
| `findings[].severity` | `"info"` \| `"warning"` \| `"critical"` | 심각도 — FE 배지 분기(중립/주황/빨강) |
| `findings[].summary` | string | 핵심 발견 요약(2~3문장) |
| `findings[].evidence` | array<string> | 근거 수치·사실 — degrade finding 은 빈 배열 |
| `findings[].recommendation` | string | 간단 조치 힌트(선택, "" 가능) — 정식 행동 추천은 `recommendations[]` |
| `limitations` | array<string> | 데이터 한계 — 확보 실패 finding(`evidence` 빈 배열)의 summary 모음. 없으면 빈 배열 |
| `chartRequested` | boolean | 판매자가 차트를 요청했는가(`wants_chart`) — `true` 인데 `charts` 가 비면 FE 가 "차트 생성 실패" 안내를 렌더할 수 있다 |
| `charts` | array | 차트 목록(최대 3개) — **빈 배열 허용**(미요청·생성 실패 공통). 형식은 구 `chart` 이벤트와 동일 |
| `charts[].title` | string | 차트 제목 |
| `charts[].chartType` | `"line"` \| `"bar"` | 시계열이면 `line`, 단계·범주 비교면 `bar` |
| `charts[].unit` | `"KRW"` \| `"COUNT"` \| `"PERCENT"` | 값 단위 |
| `charts[].series` | array | 계열 목록 — **MVP는 1개만**(FE `AnalysisChart.tsx` 가 `series[0]` 만 렌더) |
| `charts[].series[].label` | string | 계열 이름(범례) |
| `charts[].series[].points` | array | `{x: string, y: number}` 좌표 목록(최대 60개) — 시계열이면 시간 순 |
| `charts[].summary` | string | 보고서 문장에서 인용한 한 줄 요약(선택) |
| `recommendations` | array | 행동 추천 목록(≤5) — **목록 순서가 곧 "N번"**(§6.3 조회 계약). 추천 없으면 빈 배열 |
| `recommendations[].index` | number | 1부터의 순번 — 목록 순서와 항상 일치(FE 정렬 사고 방지용 명시값) |
| `recommendations[].title` | string | 짧은 제목 — 번호 카드 표시 단위 |
| `recommendations[].expectedEffect` | string | 기대 효과 한 문장(선택, "" 가능) |
| `recommendations[].actionType` | `"price_adjust"` \| `"description_update"` \| `"stock_adjust"` \| `"product_visibility"` \| `"promotion"` | 추천 유형 |
| `recommendations[].productId` | number | 대상 상품 식별자(숫자 BIGINT, §2.6) |
| `applyGuide` | string | "N번 적용해줘" 안내 문구 — 추천 없으면 `""` |

- **발생 조건**: `analysis` 레인 · 파이프라인 결과가 리포트(`kind=="report"`)일 때 **정확히 1회**. 되묻기(clarification)·사과(apology)·거절(refused)·`error` 종료에는 **발행하지 않는다**. 구 `chart` 의 "0~1회 조건 3중 검사·빈 배열 미발행 규약"은 폐기 — 보고서±차트 분기는 `charts` 배열 유무로만 표현한다.
- **순서**: 보고서 `token` **뒤**, `done{panel:"replace"}` **앞**. FE 는 `report` 를 보관했다가 `done{panel:"replace"}` 에 우측 패널을 커밋한다(버퍼링·조인 불필요).
- **FE fallback**: `report` 없이 `done{panel:"replace"}` 가 오면(구버전 서버) 기존처럼 `token` 산문을 패널에 표시한다. 미지 이벤트는 무시한다.
- **근거 사슬**: `findings` 는 F1~F3+analysis_judge 검증(이슈 #242), `body` 는 결정론 검사+judge 검증을 통과한 산문, `charts[]` 수치는 검증된 finding·보고서에 있는 값만(G1, 도구 재조회 없음), `recommendations` 는 그 위에서 생성 — 서버가 새 수치를 계산·추정해 싣는 일은 없다.
- **하위 호환**: 추가 전용 이벤트라 기존 FE는 무시한다(`useChat.ts` switch 에 `default:` 없음 — 미지 이벤트 조용히 무시, 실증됨). FE 구현(별도 이슈) 배포 전에 서버가 먼저 배포돼도 FE 는 깨지지 않고 기존 `token` fallback 으로 동작한다.
- **`chart` legacy 폐기 [v0.24.0]**: 구 `chart` 이벤트(v0.20.0, 이슈 #242)는 **폐기하며 부활하지 않는다** — FE 미구현 실증(소비자 없는 계약)으로 dual-emit 없이 안전 대체됐다. 차트 페이로드 형식은 `report.data.charts[]` 로 그대로 이관됐다. 구 `metrics`/`analysis`/`productStats`/`productDiff` 이벤트도 부활하지 않는다.

#### 데이터 소스 계약 — [v0.4.0 해소, C-7 RESOLVED, Batch 1]

**[확정]** 판매자 통계 답변의 원천은 **Spring의 판매자 집계 API(I-6)를 질의 시점에 콜백**하는 것이다(§4.4). AI는 **원시 로그를 제공받지 않고 집계값만** 조회한다. 이로써 **C-7이 해소**되며, **구 결정 20 기본안(주문 미러의 `sellerId`·금액 확장)은 폐기**된다.

- **원천 = 집계 API 콜백**: AI가 JWT 클레임의 `brandId`로 `GET /internal/seller/{brandId}/sales`(매출 시계열) 등 집계 API를 호출해 집계값을 받고 LLM으로 자연어 답변한다. **[개정 v0.8.0]** 구 "sellerId만 넘기고 Spring이 내부 해소"에서 "brandId 클레임으로 `{brandId}` path 호출"로 변경(§2.6·§4.4). 판매자 집계는 단일 API가 아니라 **5종**(매출·퍼널·행동·이탈·계정, §4.4)이다.
- **주문 데이터 접근은 조회로 통일(C-6)**: 주문 미러는 존재하지 않는다(§3.6 삭제) — 추천 dedup(결정 14-F)·프로필(결정 16)은 **질의 시점 구매 이력 조회(§4.7)** 를 사용하고, 판매자 통계는 I-6 콜백(§4.4)을 사용한다.

> **MVP 비범위(명시)**: 리뷰 인사이트(측면별 감성)는 판매자 agent MVP에 **포함하지 않는다**(고도화). 본 엔드포인트는 판매 통계 Q&A + 상세 수정 draft만 다룬다.

### 3.3 상품 목록 경로 B (Product List — Path B)

**[HARD] 구매자 SSE 스트림은 상품 카드를 싣지 않는다.** 상품 목록은 아래 경로 B로 전달된다(후보는 질의 시점 Spring 검색 §4.6에서 확보):

```
[0] AI: decompose → Spring POST /products/search 위임 조회(§4.6) → 후보 목록(price 포함) → rerank(profile_summary)
[1] AI: rerank 완료 → listId 생성 → 최종 id 목록 push (AI → Spring, I-21 §4.2)
        POST {SPRING_BASE_URL}/internal/recommendations
        { sessionId, recommendationRequestId, listType, totalBudget?, lists:[{ listId, label?, productIds:[숫자 ≤9] }] }
[2] Spring: 목록별 productIds를 Redis에 listId 키로 TTL 저장 + 표시 필드(price·imageUrl·reviewCount 등) enrich
[3] AI: 콜백 성공 → SSE `products.ready`({ sessionId, listIds }) emit (reason은 콜백에 포함돼 CH-5로 전달)
[4] FE: `products.ready` 수신 → 카드 GET (FE → Spring, CH-5 §4.3) → 우측 상품 패널 렌더
```

**설계 근거(rationale)**: 우측 상품 패널은 **Spring이 서빙하는 UI**다. **표시 권위는 Spring**에 남고, AI는 표시 필드(가격·이미지·리뷰수·재고)를 보유·전달하지 않는다(결정 9-B 유지·강화). AI가 전달하는 것은 **추천 로직의 산출물**(어떤 상품을, 어떤 순위로, 왜)뿐이다. §4.6 검색이 돌려주는 price는 **rerank·예산 검증(AI-side)용 질의 시점 값**이며, 우측 패널 표시가는 여전히 Spring이 목록 GET(§4.3)에서 채운다.

**push 실패 처리**: 목록 push(§4.2)가 실패하면 — 챗 텍스트는 정상 완료되고 지연 안내를 포함하며, 스트림은 `error`가 아니라 **`done`** 으로 종료하고 `products.ready`는 emit하지 않는다.

> **[point 조회 폐기]** 구 v0.2.0 "상품 point 조회 API"는 **완전 삭제**된다. 표시 필드는 소비자 point 조회가 아니라 **Spring이 목록 enrich 시점에 채운다**(§4.3). product.md 신규 결정 레코드 필요(§8 항목 1).

### 3.4 `GET /profile/me` — 마이페이지 프로필 조회 (FE 직접)

마이페이지 표시용으로 **토큰 소유자 본인의** 사람이 읽는 프로필 마크다운을 반환한다(자연어 마크다운 passthrough). 소유: `SPEC-PROFILE-001` §5.4/§6.9. MVP는 **조회(GET)만** 제공하며 마크다운 전문 편집(`PUT /profile/me`)은 계속 미제공이다. **[v0.22.0]** 항목 단위 편집·삭제·초기화·개인화 중지는 그래프 표면으로 제안한다 — 조회 §3.8, 제어 §3.9(🔴 초안·Post-MVP).

> **[보안] 경로에서 `{userId}` 제거 — `GET /profile/me`**: `GET /profile/{userId}`는 **IDOR** 위험이 있어, 조회 대상 신원을 **토큰 클레임(`sub`)에서 도출**하는 `GET /profile/me`를 채택한다(결정 19).
> - **SPEC 동기화 필요 🔴**: `SPEC-PROFILE-001` §5.4/§6.9는 `GET /profile/{user_id}`로 정의되어 있으므로 `/me` 채택 및 camelCase 정렬(§2.2)에 맞춘 **동기화 개정**이 필요하다(§7).

#### 요청 (Request)

```
GET /profile/me
```

- 경로 파라미터 없음. 조회 대상은 `Authorization` 헤더 JWT의 `sub` 클레임에서 도출한다.
- 게스트 토큰(`role == "guest"`): 프로필이 없으므로 `exists = false` 정상 응답.

#### 응답 (Response) — `application/json` (camelCase 정렬 제안)

```json
{
  "userId": "string",
  "exists": true,
  "markdown": "# 취향 요약\n- 3~5만원대 무선 이어폰 선호\n...",
  "generatedAt": "2026-07-13T21:04:00Z"
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | string | 요청 대상 식별자(토큰 `sub` 도출) |
| `exists` | bool | 프로필 존재 여부. 게스트·신규 회원 `false` |
| `markdown` | string \| null | 사람이 읽는 프로필 마크다운. 미존재 시 `null` |
| `generatedAt` | string \| null | 요약 생성 시각(ISO-8601). 미존재 시 `null` |

- 게스트/프로필 미보유: `exists = false`, `markdown = null`을 **오류가 아닌 정상 응답(200)** 으로 반환한다(SPEC-PROFILE-001 REQ-PROF-081).
- **`PUT /profile/me` 미제공(유지)**: 마크다운 **전문 편집**은 제공하지 않는다(SPEC-PROFILE-001 EX-P3). **[개정 v0.22.0]** EX-P3의 배제 범위는 이 전문 편집으로 **한정**되며, 항목(edge) 단위 수정·삭제·복구·전체 초기화·개인화 중지는 §3.9로 제안된다. LLM이 쓴 산문을 사용자가 부분 수정할 수 있는 형태가 아니라는 것이 전문 편집을 계속 배제하는 이유이고, 그래프는 같은 취향을 **주소 지정 가능한 항목**으로 쪼개 그 문제를 우회한다.
- **[v0.22.0] 개인화 중지 중 동작**: `personalization.enabled == false`(§3.9.5)면 `markdown`은 `null`이다 — 저장된 요약을 살아 있는 개인화로 보여주지 않는다. 보존된 내용의 검토·삭제는 §3.8/§3.9에서 한다.

### 3.5 `POST {AI_SERVER}/events/session-end` (I-20) — 세션 종료 통지 (Spring → AI, best-effort, 멱등, 본 문서 소유)

Spring이 세션 종료를 감지해 프로필 파이프라인 **조기 트리거**로 전달한다(결정 12/16). **[개정 v0.16.0]** I-20에서 Spring이 보내는 알려진 `reason`은 **`logout` 1종**이다 — 축 분리 후 "새 대화"는 FE가 `threadId`만 새로 만들어 세션을 유지하므로(§2.6) `newConversation`은 더 이상 발화되지 않는다. `reason`은 wire enum을 강제하지 않지만 최대 64자로 제한한다. **`tabClose` 신호는 사용하지 않으며**, 비활동 종료(`inactivityTimeout`)는 HTTP 통지나 자기 호출 없이 AI 내부 스케줄러가 판정한다. HTTP 계약은 본 문서 소유(결정 21), 수신·내부 timeout 동작은 `SPEC-PROFILE-001`.

> **[경로/방향 정합 v0.17.0]** I-20은 **AI 서버가 호스팅하는 inbound 엔드포인트**(Spring→AI)다. `app/api/events.py`가 먼저 회원 lifecycle을 `terminal`로 닫고, 등록된 모든 thread의 filter/cart/revert transient를 일괄 정리한 뒤 고정된 watermark까지 프로필 phase를 처리한다. transcript는 삭제하지 않는다. AI가 Spring을 호출하는 역방향(§4)이 아니다.

#### 요청 (Request) — **[v0.15.17 확정, 이슈 #62]** BE 실측 페이로드 정렬

```json
{
  "userId": 123,
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "reason": "logout"
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `userId` | number(BIGINT) | 예 | 세션 소유 회원 식별자. 양의 정수만 허용하며 string/float/bool coercion은 거부(JWT `sub`와 동종 숫자 id, 프로필 스코프) |
| `sessionId` | string | 예 | 종료된 **접속** 식별자(UUID 포함 불투명 문자열, §2.6). **방(`threadId`) 단위 종료 통지는 없다** — 종료는 접속 단위다. 최대 길이 = config `chat_key_max_chars`(§2.6) |
| `reason` | string | 아니오 | Spring이 관측한 종료 사유(**[v0.16.0]** 알려진 값 `logout` 1종) — **enum 미강제·최대 64자**. 구 `newConversation` 은 발화되지 않는다(§2.6) |

> **[v0.15.17 변경 — 이슈 #62]** 구 초안의 `eventId`·`endedAt`를 **제거**하고 `userId`를 **string → number(BIGINT 정수)**로 정정했다(BE 실측 payload 정합). 멱등 키는 별도 필드 대신 **`(userId, sessionId)` 고정 파생키**(§2.7)로 전환한다. 종전 스키마와 불일치해 `POST /events/session-end`가 상시 `400`을 반환하던 문제를 해소한다.

- **Spring 명시적 종료 [개정 v0.16.0]**: **`LOGOUT` 하나만** I-20을 발화한다. 이 경로는 세션을 삭제하므로 한 `sessionId`에는 하나의 논리적 종료만 존재한다. 구 `NEW_CONVERSATION`은 **제거** — 새 대화가 `threadId`만 갱신하게 되어(§2.6) CH-1도 I-20도 호출되지 않는다. 결과적으로 **Spring이 I-20을 쏘는 경우는 로그아웃뿐**이고, 나머지(세션 TTL 만료)는 Redis 만료 + AI 내부 비활동 sweep이 담당한다.
- **AI 내부 비활동 종료(D6, #187)**: guest/member 구매자 turn과 lifecycle touch를 같은 transaction에서 commit하고 `chat_session_contexts.last_activity_at`을 DB 시각으로 갱신한다. 단일 인스턴스 scheduler가 기본 60초마다 기본 10분 이상 비활성인 `active` context를 bounded batch로 선점한다. 어느 `threadId`의 touch든 접속 전체 deadline을 연장하고, 만료되면 등록된 모든 thread의 filter/cart/revert와 thread registry를 같은 context phase로 정리한다. transcript는 보존한다. AI가 자기 `/events/session-end`를 HTTP 호출하지 않는다.
- **탭 닫기**: 별도 종료 신호를 두지 않는다. 사용자가 threshold 전에 어느 탭에서든 돌아오면 세 탭이 함께 살아남는다. `idle_expired` 뒤 정당한 같은 owner가 돌아오면 generation을 올리고 **같은 `context_id`**를 재활성화한다. `idle_finalizing` 중 touch는 `409 SESSION_FINALIZING`이다.
- **best-effort**: Spring I-20이 유실돼도 D6 sweep이 guest/member transient를 회수하고 회원 profile watermark를 후속 phase에서 처리한다.
- **멱등·직렬화**: I-20은 session advisory lock에서 `terminal` gate와 generation을 먼저 commit한 뒤 활성 member stream 종료를 기다린다. `chat_session_finalizations`의 유한 lease/claim token, watermark, transient/profile phase가 crash·retry를 재개한다. 동일 I-20은 `duplicate`; 실패한 profile phase는 `retryable`이며 transcript와 미처리 buffer를 보존한다.
- event inbound는 camelCase alias만 허용한다. unknown field, snake_case field, camelCase+snake_case collision은 `400 BAD_REQUEST`다.
- 응답: `202 Accepted`(신규 `{"status":"accepted"}` / 중복 `{"status":"duplicate"}`). `userId`·`sessionId` 누락·타입 오류 또는 `reason` 64자 초과는 `400`(§2.5 봉투). lifecycle PostgreSQL의 timeout/pool/connection 장애는 `503 STATE_UNAVAILABLE`이며 programming/integrity/domain/cancellation 오류를 503으로 마스킹하지 않는다.


#### 3.5.1 `POST {AI_SERVER}/events/session-claim` — guest → member 소유권 승격 (#187)

Spring은 로그인 완료 후 guest 접속 전체를 회원에게 넘기기 위해 이 inbound를 호출한다.
`X-Internal-Token`이 필수이며 사용자 JWT나 body의 임의 신원을 대신 신뢰하지 않는다.

```json
{
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "guestId": "guest-uuid-or-id",
  "userId": 123
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string | 예 | BE가 로그인 전후 이어 쓸 접속 id. 빈 문자열 금지, 최대 `chat_key_max_chars` |
| `guestId` | string | 예 | 로그인 직전 서명 티켓의 guest `sub`와 같은 소유자. 빈 문자열 금지, 최대 `chat_key_max_chars` |
| `userId` | number(BIGINT) | 예 | 로그인 완료 회원 id. **strict 양의 정수 `1..2^63-1`**이며 string/float/bool coercion 금지 |

**응답**

- 최초 원자 전이: `202 {"status":"accepted"}`
- 동일 `(sessionId, guestId, userId)` 재전송: `202 {"status":"duplicate"}`

전이는 `chat_session_contexts`의 owner와 generation만 갱신한다. `context_id`, 등록된
`threadId`, filter/cart/revert 상태는 유지하며 로그인 시점 복사는 하지 않는다. 기존 guest
transcript도 보존하지만 member profile buffer로 복사하지 않는다. 전이 중에는 해당
`(guestId, sessionId)` active-stream scope에 fence를 걸며 활성 stream이 있으면 받지 않는다.

**오류**

| HTTP | `code` | 조건 |
|---|---|---|
| `401` | `INTERNAL_TOKEN_INVALID` | 운영에서 서비스 토큰 누락/불일치 |
| `400` | `BAD_REQUEST` | 필수 필드/strict 타입 오류, 빈/초과 길이 id, `userId`가 `1..2^63-1` 밖이거나 bool |
| `409` | `SESSION_ACTIVE` | guest scope 활성 stream 존재 |
| `409` | `SESSION_FINALIZING` | idle finalization 진행 중 |
| `409` | `SESSION_CLAIM_CONFLICT` | 다른 claim 이력, terminal, owner 불일치 |
| `503` | `STATE_UNAVAILABLE` | lifecycle PostgreSQL 정본 사용 불가 |

두 event 모델은 camelCase alias만 허용한다. unknown field, snake_case field,
camelCase+snake_case collision은 `400 BAD_REQUEST`다.

claim commit 뒤 옛 guest 티켓의 `/chat`은 `403 SESSION_FORBIDDEN`이고, 새 turn/thread를
만들지 않는다. member 티켓은 같은 signed `sessionId`와 기존 `context_id`로 모든 탭을 계속한다.

**배포 순서(필수)**: BE #63이 signed `sessionId`와 `ticketTtlSeconds=60` 증거를 먼저 남긴다 → 마지막 구 계약 티켓 뒤 90초(60초 TTL + 30초 여유) drain → AI enforcement/schema/backfill/scheduler 배포 → FE #52 실제 3-tab 로그인/refresh → missing-session·claim-conflict·cleanup-retry 지표 확인. 이 저장소는 BE/FE/운영 gate를 실행할 수 없으며 완료했다고 주장하지 않는다.

### 3.6 (삭제) 주문 이벤트 — 채택하지 않음 [v0.5.0]

**[v0.5.0 삭제]** 구 `POST /events/order`(주문 이벤트 미러)는 **채택하지 않는다**(2026-07-15 사용자 확정). 검색이 질의 시점 Spring 위임(§4.6)으로 확정되면서 구매 이력도 **추천 직전 질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 확보한다 — 알림 수신도, 미러 테이블도 없다. 결정 14-F의 동작 요구(exact `productId` 제외·소모품 카테고리 억제·되돌리기 칩)는 **불변**이며 데이터 획득 방식만 교체된다. 프로필 파이프라인의 구매 소스도 sleep-time 배치가 동일 API(§4.7)를 조회한다(SPEC-PROFILE-001 개정 필요, §7.2).

> **[v0.5.0] 카탈로그 동기화 채널 없음**: AI 카탈로그 사본(미러)을 채택하지 않으므로 카탈로그 변경 이벤트 채널도, 배치 폴링도 **존재하지 않는다**(2026-07-15 확정, §4.6 말미). Spring → AI 이벤트는 §3.5의 `/events/session-end`와 `/events/session-claim`만 남는다.

### 3.7 `POST {AI_SERVER}/internal/recommendations/home` (I-22) — 메인 화면 추천 목록 (Spring → AI) [v0.18.0 신설]

홈 화면 "OO님을 위한 추천"(P-5, §4.11)의 개인화 랭킹을 Spring이 AI에 위임한다. **Spring이 호출 주체**이므로 채팅 경로(CH-2 → I-21 → CH-5)와 달리 **왕복 1회로 끝난다** — 응답 본문에 목록이 담겨 오고 **I-21 콜백(§4.2)을 타지 않는다**. 정본 확정 2026-07-28.

```
POST {AI_SERVER}/internal/recommendations/home
X-Internal-Token: {서비스 토큰}   ← internal 그룹 (레인 b)
```

- **인증**: `X-Internal-Token` 서비스 토큰(§2.3 b). 사용자 JWT를 받지 않는다.
- **예산**: **P-5 예산 내 — 연결 2s / 응답 3s.** 채팅 스트림 상한(90s, §2.9 c)과 무관하며 **메인 렌더 블로킹 방지**가 목적이다.

#### Spring → AI 요청 (I-22)

```json
{
  "memberId": 123,
  "limit": 12,
  "catalogVersion": "catalog-20260728T0300Z",
  "signals": {
    "recentlyViewedProductIds": [552, 88, 101],
    "cartProductIds": [205],
    "recentPurchasedProductIds": [77, 91]
  }
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `memberId` | number | 예 | 회원 BIGINT(§2.6). **게스트는 이 API를 호출하지 않는다** — 개인화 대상이 아니라 Spring이 P-4(인기 상품)로 처리한다 |
| `limit` | number | 예 | **최종 노출 목표 개수.** AI는 Spring의 품절 드롭에 대비해 **이보다 넉넉히 반환**하고, Spring이 판매 불가 상품을 뺀 뒤 `limit`만큼 자른다. **상한 60** — 홈 레일 한 줄에 그 이상이 필요한 화면이 없고, 상한이 없으면 overfetch가 응답 크기·조회 비용을 함께 부풀린다. 초과 시 `400 BAD_REQUEST` |
| `catalogVersion` | string | ~~예~~ **아니오(폐기 제안)** | **[개정 v0.19.0, C-18] 이 필드는 존재 이유가 없다 — 계약에서 제거를 제안한다.** 선택으로 완화해 Spring이 계속 보내도 깨지지 않게 했고 AI는 받기만 하고 버린다. 근거는 아래 C-18 |
| `signals` | object | 예 | 아래 3종. 비어 있으면 개인화 근거가 없어 `NO_PROFILE`로 답한다. **각 배열은 항목당 양의 BIGINT, 길이 상한 200** — 초과·범위 밖이면 `400 BAD_REQUEST`. 이 값들은 그대로 AI 인덱스 조회(`get_many`)와 제외 필터로 흘러가므로 상한이 없으면 요청당 조회 비용에 상한이 없다. 🔴 실제 신호 배열 크기는 BE와 맞춰야 한다 |

| `signals` 필드 | 출처(Spring) | AI가 쓰는 방식 |
|---|---|---|
| `recentlyViewedProductIds` | `product_view` 이벤트 | 해당 상품 임베딩을 프로필 벡터와 **가중 혼합**(최신일수록 높게). **[v0.19.0 명문화] 배열은 최신순 — index 0 = 가장 최근 조회.** AI 는 배열 인덱스로 recency decay 를 적용하므로 순서가 뒤집히면 가중치가 조용히 거꾸로 걸린다(응답 `items` 의 "배열 순서 = 순위"와 대칭 규약). 🔴 Spring 의 정렬 보장 확인 필요 — C-19 |
| `cartProductIds` | 현재 장바구니(미결제) | 같은 방식, **가중치 더 높게** — 담기까지 갔다는 건 강한 신호다 |
| `recentPurchasedProductIds` | 최근 주문 | **결과에서 제외**한다(이미 샀으므로). **가중치가 아니라 필터다** |

> **`sessionId`가 없다** — 홈에는 채팅 세션이 없다. 신원은 `memberId`만으로 충분하다. 따라서 §2.6의 `sessionId`/`threadId` 축(§2.9 동시 스트림 락 포함)은 이 API에 적용되지 않는다.

#### 성공 응답 — 200

```json
{
  "outcome": "PERSONALIZED",
  "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
  "listId": "7c1e9f2a4b8d43f5a0c6d1e97b3f8a24",
  "items": [
    { "productId": 101, "reason": "최근 선호한 가격대와 카테고리에 맞아요" },
    { "productId": 552, "reason": null }
  ]
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `outcome` | string | `PERSONALIZED` \| `NO_PROFILE` \| `INSUFFICIENT_CANDIDATES` — 아래 표 |
| `recommendationRequestId` | string | 추천 실행 1회를 가리키는 id. **AI가 생성**(I-21의 상관키와 같은 역할) |
| `listId` | string | **[HARD] AI가 생성하는 UUID급 무작위 식별자(≥128bit)** — I-21(§4.2)과 **동일 규칙**. 순번·타임스탬프 등 추측 가능한 형식 금지. Spring이 저장하고 P-5 응답에 실어 FE에 전달하며, 노출·클릭 이벤트가 이 키로 추천에 귀속된다 |
| `items` | array | `{productId(숫자), reason}`. **배열 순서가 곧 순위다** — `position`을 싣지 않는다 |
| `items[].reason` | string\|null | 카드에 표시할 이유. 없으면 `null`(I-21의 `reasons` 생략 규약과 달리 **필드 자체는 유지**) |

**`outcome` 처리**

| 값 | 뜻 | Spring 동작 |
|---|---|---|
| `PERSONALIZED` | 개인화 성공 | 그대로 사용, P-5 `source=AI_RECOMMENDED` |
| `NO_PROFILE` | 프로필 없음(신규 회원) 또는 `signals`가 비어 개인화 근거 없음 | **P-4 인기상품으로 대체**, `source=POPULAR_FALLBACK` · `fallbackReason=PROFILE_MISSING` |
| `INSUFFICIENT_CANDIDATES` | 후보가 부족해 랭킹이 무의미 | 동일하게 P-4 대체, `fallbackReason=INSUFFICIENT_CANDIDATES` |

> **[HARD] cold start는 오류가 아니다.** 프로필이 없어도 **200 + `outcome`** 으로 답하며, **fallback 판단은 Spring이 한다.** 프로필 부재·후보 부족을 이유로 AI가 4xx/5xx를 내서는 안 된다.

#### 실패 응답

| HTTP | `code` | 조건 |
|---|---|---|
| 400 | `BAD_REQUEST` | 필수 필드 누락·타입 오류 |
| 401 | `INTERNAL_TOKEN_INVALID` | 토큰 없음/불일치 |
| 503 | `UPSTREAM_UNAVAILABLE` | 카탈로그 인덱스(랭킹의 유일한 입력) 일시 장애 — **프로필 저장소 장애는 여기 해당하지 않는다**(아래 「HOME 실패 모드(degrade) 어휘」 표). LLM은 I-22 요청 경로에 없다(reason 사전 생성, 아래 구현 노트) |
| 504 | `UPSTREAM_TIMEOUT` | 카탈로그 조회가 예산을 넘김(호출 타임아웃·잔여 예산 소진 포함) |

> 위 4종은 **입력·인프라 실패에만** 쓴다 — 앞의 [HARD]와 충돌하지 않는다. cold start(`NO_PROFILE`)·후보 부족(`INSUFFICIENT_CANDIDATES`)은 **정상 200**이다.
>
> 4xx/5xx가 나가면 Spring이 P-4로 대체하고 `fallbackReason=AI_ERROR` 또는 `AI_TIMEOUT`으로 기록한다. **어느 경우에도 홈 렌더는 막히지 않는다.**

#### HOME 실패 모드(degrade) 어휘 [v0.26.1, #367]

CHAT 추천 파이프라인의 degrade 어휘(임베딩 재정렬 실패 `search_service.py` · rerank 실패
`graph.py` · Spring 검색 타임아웃)는 HOME에 대응 경로가 없다 — I-22는 라이브 임베딩·rerank·
Spring 검색(I-1)을 호출하지 않는다(#335 매트릭스 발견 구 combo-0050/0052/0053 — #335 최초
매트릭스 기준 번호, 본 PR(#367) 재채번 이전이라 지금의 같은 번호는 다른 셀을 가리킨다). HOME의
실패 어휘는 아래 4종이며, 평가 하네스(`evals/combo_matrix/axes.json`)의 지면별 degrade 축
정본이다.

| 어휘 | 계약 | 조건 |
|---|---|---|
| `profile_unavailable` | **200** — 프로필 항만 빠지고 랭킹 계속, `outcome`은 남은 근거로 판정, 와이어 구별 신호 없음 | 프로필 저장소 실패·지연 |
| `catalog_unavailable` | **503 `UPSTREAM_UNAVAILABLE`** | 카탈로그 인덱스 비-타임아웃 장애 |
| `catalog_timeout` | **504 `UPSTREAM_TIMEOUT`** | 카탈로그 조회 타임아웃·잔여 예산 소진 |
| `reason_degraded` | **200 `PERSONALIZED`** + 해당 `reason=null` | 부분 실패: reason 재료 조회 실패·잔여 예산 소진 |

`profile_unavailable`은 새 `outcome` 값도 새 필드도 만들지 않는다 — 남은 근거로 그대로
판정한다(시그널 임베딩이 있으면 `PERSONALIZED`, 없으면 `NO_PROFILE`). `reason_degraded`는
확정된 순위를 reason 순단으로 버리지 않는다(§4.11 홈 렌더 비차단) — `reason`은 계약상
nullable이라 와이어 형태가 불변이다.

#### 규약

- **멱등이 아니다** — 재시도하면 **새 `recommendationRequestId`·`listId`가 발급**되며 Spring이 새 추천 실행으로 센다. §2.7의 `/events/*` 멱등 규약은 이 API에 적용되지 않는다.
- **지면(`surface`)은 요청·응답에 싣지 않는다** — 이 API로 만들어진 목록은 `HOME`, I-21로 온 것은 `CHAT`이라 Spring이 **경로로 판단**해 저장한다.
- **`recommendation_generated`는 Spring이 server-side로 기록한다** — 목록 저장 시점에 `behavior_events`에 직접 적재(I-21과 동일 규칙). P-4 fallback으로 대체한 경우에도 `source=POPULAR_FALLBACK`으로 구분해 기록한다. **AI는 이 이벤트를 적재하지 않는다.**
- **[HARD] 프로필 원문·prompt·모델 식별자는 응답·로그·trace에 포함하지 않는다.** 알고리즘·모델 버전은 **AI가 자체 테이블에 보관**하며 **와이어에 싣지 않는다**(평가 산출물 전용). → `algorithmVersion`·`modelVersion`을 응답에 넣는 구현은 계약 위반이다. §6.3 관측 경계와 동일한 기준이다.
- **후보 확보 경로가 채팅과 다르다** — AI는 **자체 카탈로그 인덱스(I-17로 동기화된 임베딩, §4.8)** 로 순위를 매긴다. Spring이 후보 목록을 요청에 싣지 않으며, **재고·판매상태 반영은 Spring이 카드 조립 시점**에 한다(CH-5와 동일 패턴). ※ §1.2의 "[HARD] 후보 확보 = 질의 시점 Spring 검색(I-1)"은 **채팅 경로의 규약**이고, 홈은 이 절이 정한 자체 인덱스 경로를 쓴다.
- **캐시·TTL은 P-5 소관**(§4.11) — 개인화 결과 캐시 10분, `listId` 귀속 유효기간 24시간(2026-07-30 확정). 채팅 CH-5의 10분(조회 만료)과는 성격이 다르다.
- **[v0.22.0] 개인화를 중지한 회원(§3.9.5)은 `outcome: "NO_PROFILE"`** 로 답한다 — 프로필 벡터 항을 빼면 개인화 근거가 남지 않으므로 기존 판정 기준(질의 벡터를 만들 수 없음)에 그대로 걸린다. **새 `outcome` 값을 만들지 않으며 Spring은 무변경**으로 기존 P-4 대체 분기를 그대로 쓴다. 단 **P-5 캐시(10분)는 Spring 소유라 AI가 비울 수 없어**, Spring이 중지 시점에 해당 회원 캐시를 무효화하지 않으면 최대 10분간 개인화 홈이 계속 보인다 — 🔴 C-27.

#### 구현 노트 (v0.18.0, #148) — 계약과 다른 지점

와이어 계약(필드·`outcome`·오류 코드)은 위와 **완전히 일치**한다. 아래는 계약이 규정하지 않았거나, 규정했으나 현 구현이 다르게 채운 내부 동작이다. BE는 읽을 필요가 없고, AI측 후속 작업자가 알아야 한다.

- **프로필 벡터는 요약 생성 시점에 미리 만들어 둔다.** 위 signals 표대로 질의 벡터는 시그널 임베딩과 **프로필 벡터의 가중 혼합**이다. 다만 프로필은 자연어 markdown 요약이라(`SPEC-PROFILE-001`) 벡터가 없었고, 요청 경로에서 임베딩하면 Google API 왕복이 붙어 **연결 2s/응답 3s 예산을 위협**한다. 그래서 sleep-time consolidation이 요약을 만들 때 **벡터도 함께 만들어 저장**하고(`profile/store._embed_summary`, task=`RETRIEVAL_QUERY` — 카탈로그 문서와 달라야 하는 비대칭 임베딩), I-22는 읽어서 항으로 더하기만 한다. 임베딩 실패·구 요약(벡터 없음)은 그 항만 빠진다. 가중치는 `home_reco_weight_profile`(기본 0.5, cart 1.0·조회 0.6 사이) — 오래된 취향이 지금의 관심을 덮으면 홈이 안 바뀐 것처럼 보인다. **0으로 두면 롤백**된다(프로필이 랭킹에서 빠지고 `reason` 근거로만 남음).
- **`NO_PROFILE`의 판정 기준 = 질의 벡터를 만들 수 없음** — 시그널 임베딩도 프로필 벡터도 없는 경우다. 정본이 적은 두 사유(*"프로필 없음(신규 회원)"*·*"signals 비어있음"*)가 **둘 다 있어야** 성립한다. 뒤집으면 **둘 중 하나만 있어도 개인화한다** — 시그널이 비어도 대화로 쌓인 취향만으로 홈이 개인화되고, 프로필이 없어도 방금 본 상품으로 개인화된다. 시그널 상품이 아직 I-17로 인덱싱되지 않은 경우는 시그널이 없는 것과 같게 취급한다.
- **503의 범위** → 위 「HOME 실패 모드(degrade) 어휘」 소절이 규범이다.
- **`catalogVersion`을 받지만 쓰지도, 만들지도 않는다**(C-18 폐기 제안, v0.19.0) — 한때 AI가 인덱스 지문을 만들어 응답에 실었으나 되돌렸다. **버전 라벨만으로는 재현이 성립하지 않는다**: `products`는 I-17이 제자리 upsert하므로 그 시점의 임베딩이 남지 않아, 지문을 들고 와도 가리키는 인덱스 상태가 이미 사라져 있다. 버전마다 벡터를 스냅샷하려면 7,220×1536 기준 **버전당 약 44MB**를 5분 주기로 쌓아야 해 성립하지 않는다. 애초에 재현이 필요하지도 않다 — 산출물(목록·`reason`)은 Spring이 `recommendation_generated`로 이미 저장한다. 캐시 무효화 명분도 TTL 10분과 중복이고, 오히려 `max(updated_at)` 기반 지문은 **상품 1건 갱신으로 전 회원 캐시를 동시에 무효화**해 캐시를 죽인다.
- **`reason`을 요청 경로에서 생성하지 않는다 — 미리 만들어 두고 고른다.** 초기 구현은 fast tier LLM 배치 1회로 문장을 만들었으나 **실측에서 예산을 넘겼다**(2026-07-31, gpt-5-nano, 카탈로그 7,220건): 후보 20개 7,970ms · 12개 3,852ms · 6개 2,102ms로 **항목 수에 선형**(출력 토큰 지배, 프롬프트는 2.3k자로 입력 탓이 아님)이라, 5개로 줄여도 2.0s 타임아웃을 5회 중 5회 넘겨 **reason이 한 건도 나오지 않았다.** 지금은 I-17 배치가 **상품당 1회** 만들어 `extras`에 넣어 둔 재료(`situation_tags`·`review_pros`, §4.8)에서 사용자 맥락에 맞는 것을 고른다 — 우선순위는 담기 > 조회 > 상품 고유 폴백(프로필 문자열 분기는 선호/회피 극성 파싱 불가로 제거 — 장기 취향은 프로필 벡터가 랭킹에 반영)이고, 매칭이 없으면 `null`이다(계약상 정상). 결과: **요청 경로 LLM 0회 · 결정적 · 종단 p50 45ms · reason 22/24건**. 문장 재료는 LLM 산출물이고 틀만 규칙이다.
- **✅ 성능 실측 완료**(2026-07-31, 실카탈로그 7,220건) — 초기 구현은 `store.all()` 전량을 파이썬으로 코사인 계산해 **p50 3,321ms로 예산 3s를 그 자체로 초과**했다. `ArtifactStore.top_k_by_vector`를 신설해 pg 경로를 HNSW 인덱스(`vector_ip_ops`, `<#>`)로 밀어 **p50 39ms**가 됐다. 벡터가 L2 정규화돼 있어 내적 순위 = 코사인 순위이며, **정규화는 이 경로의 전제**다(`normalized` 컬럼이 그 사실을 기록한다).

### 3.8 `GET {AI_SERVER}/internal/profile/{userId}/graph` (I-32) — 개인화 관계 그래프 조회 (Spring → AI) [v0.22.0 신설, 레인 개정 v0.26.0, 🔴 제안(초안)]

마이페이지 "AI가 이해한 내 취향" 화면용으로 **토큰 소유자 본인의** 취향을 node·edge 구조로 반환한다. §3.4의 마크다운이 사람이 읽는 **한 덩어리**인 데 반해, 이 절은 같은 취향을 **항목 단위로 쪼개** 사용자가 무엇이 왜 저장됐는지 보고 §3.9로 고칠 수 있게 한다. 소유: `SPEC-PROFILE-GRAPH-149`(모델·규칙·인수 기준), 본 절(외부 HTTP 계약).

> **🔴 제안(초안) · Post-MVP** — 본 절과 §3.9는 FE/BE 수용 전 초안이며 MVP 범위가 아니다(milestone `Post-MVP Buyer Quality`, 이슈 #149 계약 / #150 구현). 미합의 항목은 §5 C-20~C-28.

> **[HARD·보안, 개정 v0.26.0] 경로의 `{userId}`는 Spring이 자기 로그인 세션에서 도출한 값이다** — §3.9와 **완전히 동일한 규칙**이다. FE 입력(본문·쿼리·헤더)에서 온 값을 그대로 실어서는 안 되고, AI는 이 경로에서 `Authorization` 헤더를 **인증 수단으로 인정하지 않는다**. `X-Internal-Token`이 유일한 자격 증명이며 신원 도출 책임은 Spring에 있다(§3.5.1·§3.9 preamble과 같은 규칙).

```
GET {AI_SERVER}/internal/profile/{userId}/graph?includeSuppressed=false
X-Internal-Token: {서비스 토큰}          ← internal 그룹(레인 b)
```

> **[개정 v0.26.0, #322] 조회가 FE 직접 → Spring 프록시로 전환됐다.** v0.22.0은 조회를 레인 (a)에 두고 기존 `chat:stream` 티켓을 재사용했으며, 그 비대칭을 §3.9 preamble에서 [HARD]로 못박았다("통일하면 안 된다"). **그 전제가 실무에서 무너졌다** — 마이페이지에는 채팅 세션이 없어 스트림 티켓을 발급받을 방법이 없다(CH-1b `POST /api/chat/tickets`는 `sessionId`가 필수이고, 프로필 화면은 세션을 만들지 않는다). **티켓을 받을 수 없는 화면에 티켓 인증을 요구하는 계약이었다.** 조회만 다른 레인일 이유가 사라졌으므로 §3.9와 같은 레인으로 합류시킨다. FE는 Spring의 마이페이지 엔드포인트를 호출하고 Spring이 이 internal 계약으로 AI에 위임한다.
>
> **§3.4 `GET /profile/me`(마크다운 조회)는 본 개정 범위 밖이다** — 같은 스트림 티켓을 쓰지만 MVP로 이미 배포·동작 중이고, 그 경로의 인증 전환은 별도 판단이다. 본 절만 옮기는 것이 의도된 선택이며 누락이 아니다.

- **인증**: §2.3 (b) 서비스 토큰. **[v0.26.0] 스트림 티켓은 더 이상 이 표면의 자격 증명이 아니다.**
- **예산(제안, 실측 아님)**: 응답 **2s**. **[HARD] 요청 경로 LLM 0회** — 프로젝션은 저장된 구조화 트리플의 결정론적 파생이다.
- **판매자·게스트 분기는 제거됐다** — `{userId}`가 Spring의 **회원 로그인 세션**에서만 도출되므로 게스트·판매자 주체가 이 경로에 도달하는 구조가 없다. 구 계약의 게스트 `200`(빈 그래프)·판매자 `403`은 스트림 티켓의 `sub_type`·`role` 클레임을 전제한 것이었고, 그 클레임이 사라졌다. **프로필이 아직 없는 회원**은 계속 `exists: false` + 빈 배열의 정상 `200`이다.
  - 🔴 **확인 필요(C-20 잔여)**: Spring이 이 경로를 **비로그인 사용자에게 노출하지 않는다**는 확약이 필요하다. 만약 게스트도 마이페이지 그래프를 볼 수 있어야 한다면 게스트 식별자(현행 `guest.id`는 UUID 문자열)를 BIGINT `{userId}`로 표현할 방법이 없으므로 **계약을 다시 열어야 한다.**

#### 요청 쿼리 파라미터

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `includeSuppressed` | bool | 아니오 | 기본 `false`. `true`면 사용자가 삭제(suppress)한 edge를 `suppressed: true`로 함께 반환한다(되돌리기 UI 전용). **[개정 v0.26.0] 반환 대상은 undo 창이 아직 열려 있는 것뿐이다** — 창이 닫힌 edge는 원문이 물리 삭제돼 반환할 것이 없다(§3.9.2). **민감 카테고리 제외분은 이 플래그와 무관하게 절대 반환하지 않는다**(아래 규약) |

#### 성공 응답 — 200

```json
{
  "userId": 123,
  "exists": true,
  "graphVersion": "g42",
  "generatedAt": "2026-08-04T21:10:00Z",
  "personalization": { "enabled": true, "disabledAt": null },
  "usagePolicy": { "orderOnly": true, "filterSafe": false },
  "nodes": [
    { "nodeId": "priceBand:30000-50000", "type": "priceBand", "label": "3~5만원대" },
    { "nodeId": "brand:소니", "type": "brand", "label": "소니", "verified": true },
    { "nodeId": "category:음향가전 > 블루투스 이어폰", "type": "category", "label": "블루투스 이어폰", "verified": true },
    { "nodeId": "attribute:노이즈캔슬링", "type": "attribute", "label": "노이즈캔슬링", "verified": false }
  ],
  "edges": [
    {
      "edgeId": "e_7b1c9a04e5f2438d",
      "to": "priceBand:30000-50000",
      "predicate": "prefers",
      "source": "conversation",
      "origin": "machine",
      "confidence": "HIGH",
      "firstSeenAt": "2026-07-02T11:20:00Z",
      "lastConfirmedAt": "2026-07-29T09:05:00Z",
      "editable": true,
      "suppressed": false,
      "suppressedAt": null,
      "challenged": false,
      "derivedFromSensitive": false
    },
    {
      "edgeId": "e_2f80d1aa63b74c19",
      "to": "brand:소니",
      "predicate": "avoids",
      "source": "user",
      "origin": "user",
      "confidence": "HIGH",
      "firstSeenAt": "2026-07-11T08:00:00Z",
      "lastConfirmedAt": "2026-08-04T13:40:00Z",
      "editable": true,
      "suppressed": false,
      "suppressedAt": null,
      "challenged": true,
      "derivedFromSensitive": false
    },
    {
      "edgeId": "e_c40a8e1b7d92f603",
      "to": "category:음향가전 > 블루투스 이어폰",
      "predicate": "purchased",
      "source": "purchase",
      "origin": "machine",
      "confidence": "HIGH",
      "firstSeenAt": "2026-07-18T04:00:00Z",
      "lastConfirmedAt": "2026-07-18T04:00:00Z",
      "editable": false,
      "suppressed": false,
      "suppressedAt": null,
      "challenged": false,
      "derivedFromSensitive": false
    }
  ],
  "suppressedCount": 0,
  "unprojectedCount": 0,
  "truncated": false
}
```

프로필 미보유(승격 전 신규 회원) — **[v0.26.0] 구 계약의 게스트 예시를 이 케이스로 대체했다.** 게스트는 Spring 로그인 세션이 없어 이 경로에 도달하지 않으며, `guest.id`는 UUID 문자열이라 BIGINT `{userId}`로 표현할 수도 없다(위 🔴 확인 필요):

```json
{
  "userId": 456,
  "exists": false,
  "graphVersion": "g0",
  "generatedAt": null,
  "personalization": { "enabled": true, "disabledAt": null },
  "usagePolicy": { "orderOnly": true, "filterSafe": false },
  "nodes": [],
  "edges": [],
  "suppressedCount": 0,
  "unprojectedCount": 0,
  "truncated": false
}
```

응답 헤더: `ETag: "g42"` — **편의 사본이며 정규 출처는 본문 `graphVersion`이다**(아래 규약). **[v0.26.0]** 레인 (b) 전환으로 브라우저가 이 응답을 직접 읽지 않게 되어, `Access-Control-Expose-Headers: ETag`(C-11 잔여) 문제는 **본 절에서는 소멸했다** — Spring이 중계할 때 헤더를 옮길지는 Spring 소관이며, FE는 본문 `graphVersion`만으로 동작한다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | number(BIGINT) | **[개정 v0.26.0]** 경로 값 echo(Spring 도출 신원) — §3.9 계열과 **동일 타입**이다. 구 계약은 string(토큰 `sub` 그대로)이었고 그 근거는 "조회는 토큰 신원을 그대로 되돌려준다"였는데, 프록시 전환으로 토큰 신원이 사라져 근거가 소멸했다 |
| `exists` | bool | 승격된 프로필 존재 여부. 신규 회원 `false` |
| `graphVersion` | string | **[HARD] 불투명 토큰.** `[A-Za-z0-9._-]{1,64}`. **파싱·크기 비교·순서 추론 금지.** §3.9 `If-Match`에 그대로 실어 보낸다 |
| `generatedAt` | string \| null | 프로젝션 원천(sleep-time consolidation) 시각(ISO-8601). 미존재 시 `null` |
| `personalization.enabled` | bool | `false`면 추천 경로가 프로필을 쓰지 않고 새 취향도 수집하지 않는다(§3.9.5). **데이터는 보존되며 그래프는 계속 보인다** |
| `personalization.disabledAt` | string \| null | 개인화 중지 시각. `enabled == true`면 `null` |
| `usagePolicy.orderOnly` | bool | 항상 `true` — 그래프는 **순서(랭킹)에만** 쓰인다 |
| `usagePolicy.filterSafe` | bool | 항상 `false` — **이 데이터를 검색 필터로 변환하면 안 된다**(아래 규약, #119) |
| `nodes[].nodeId` | string | 안정 식별자 `{type}:{정규화 라벨}`. 같은 대상은 항상 같은 값(아래 규약) |
| `nodes[].type` | string | `brand` \| `category` \| `attribute` \| `priceBand` \| `ratingBand` \| `product` \| `situation` |
| `nodes[].label` | string | 사람이 읽는 이름. 최대 길이 = config `profile_graph_label_max_chars` |
| `nodes[].verified` | bool | `true`면 통제 어휘(카탈로그 카테고리·브랜드 사전)에 스냅된 노드. `false`(미검증)는 랭킹에서 제외될 수 있다 |
| `edges[].edgeId` | string | `e_` + 16자 hex. `(predicate, nodeId)` 파생이라 **수정하면 값이 바뀐다**(§3.9.1) |
| `edges[].to` | string | `nodes[].nodeId` 참조. **`from`은 싣지 않는다** — MVP 그래프는 전부 "사용자 → 대상" 1-hop이라 항상 같은 값이고, 있으면 다홉 순회가 가능하다는 거짓 신호가 된다 |
| `edges[].predicate` | string | `prefers` \| `likes` \| `avoids` \| `interestedIn` \| `purchased` — 아래 관계 표 |
| `edges[].source` | string | 최신 근거의 출처: `conversation` \| `purchase` \| `user` |
| `edges[].origin` | string | `machine` \| `user`. `user`는 사용자가 직접 단정·수정한 것이며 기계 파이프라인이 덮지 못한다 |
| `edges[].confidence` | string | **`LOW` \| `MEDIUM` \| `HIGH` 3버킷.** 수치와 경계는 와이어에 싣지 않는다(아래 규약) |
| `edges[].firstSeenAt` | string | 최초 관측 시각(ISO-8601) |
| `edges[].lastConfirmedAt` | string | 최근 재확인 시각(ISO-8601). 정렬 키 |
| `edges[].editable` | bool | `false`면 §3.9.1 수정 불가(`purchased`). 삭제(suppress)는 가능 |
| `edges[].suppressed` | bool | 사용자가 삭제했고 **undo 창이 아직 열려 있는** edge. **기본 응답에는 `true`가 없다**(`includeSuppressed=true`에서만). 창이 닫히면 원문이 물리 삭제돼 이 배열에서 사라진다(§3.9.2) |
| `edges[].suppressedAt` | string \| null | 삭제 시각. undo 창 만료 시각은 이 값 + 서버 config이며 **와이어에 싣지 않는다**(§3.9.2) |
| `edges[].challenged` | bool | `origin == "user"` 이후 반대 관측이 config 임계 이상 쌓임. **상태는 바뀌지 않으며** FE가 "다시 반영할까요?"를 물을 수 있는 힌트다 |
| `edges[].derivedFromSensitive` | bool | 민감 주제에서 **원인을 버리고 파생된** 커머스 취향. `true`면 근거·라벨 원문이 일절 제공되지 않는다 |
| `suppressedCount` | number | 사용자가 삭제했고 **undo 창이 열려 있는** edge 수 — 되돌릴 수 있는 것의 개수다. **[개정 v0.26.0] 창이 닫히면 줄어든다**(누적 삭제 총계가 아니다). 영구 tombstone은 원문 없는 차단 표식이라 세지 않는다. **민감 제외분도 포함하지 않는다**(아래 규약) |
| `unprojectedCount` | number | 구조화 트리플이 없어 아직 그래프로 변환되지 않은 fact 수. 정상값은 `0`(관측용) |
| `truncated` | bool | `edges`가 config `profile_graph_max_edges`에서 절단됨 |

#### 관계(`predicate`) 정의 — `source`·`confidence` 의미

| `predicate` | 뜻 | 원천 | 허용 `source` | `confidence` 의미 | `editable` |
|---|---|---|---|---|---|
| `prefers` | **비교 선호** — 다른 것 대신 이것(가격대·평점 성향·선호 브랜드) | 승격된 semantic fact 중 구조화 블록 매핑분(`SPEC-PROFILE-001` §5.1) | `conversation`, `user` | 반복·명시성 누적의 버킷 | 예 |
| `likes` | **단순 긍정** — 비교 없이 표명(색·소재·스타일) | 승격된 semantic fact 중 산문 취향 | `conversation`, `user` | 동일 | 예 |
| `avoids` | 명시 회피·부정 선호 | 부정 극성 semantic fact | `conversation`, `user` | 동일 | 예 |
| `interestedIn` | **최근 관심**(시간 경과로 감쇠) | episodic·최근 맥락 | `conversation` | 최근성+현저성 선택(반복 게이트 예외 — `SPEC-PROFILE-001` OPEN-P8) | 예 |
| `purchased` | 구매 사실 | I-19 구매 이력(§4.7) 파생 | `purchase` | 항상 `HIGH`(추정이 아니라 사실) | **아니오** |

- **`source == "user"`** 는 위 4종 어디에도 붙을 수 있고 항상 `confidence: "HIGH"` · `origin: "user"` 다 — 사용자 단정이 기계 추정보다 우선한다.
- **FE 표현 계약은 별도 협의다(🟡 C-25)** — 버킷 라벨 문구, 삭제 항목 노출 방식, **undo 창 잔여 시간 표시 여부**(v0.26.0 추가 — 창 길이는 서버 config이며 와이어에 없다, §3.9.2)는 FE 소유다. 특히 "왜 이 취향이 있나요" 같은 설명 문장을 요구하면 **근거 원문 금지 [HARD]** 때문에 생성·redaction된 문장이 되어야 하므로 별도 계약이 필요하다.
- **[생산자 부재 명시] `avoids`는 현재 생산자가 없다.** 선호/회피 극성을 산문에서 파싱할 앵커가 없어 관련 분기가 제거된 이력이 있다(§3.7 구현 노트의 `reason` 항목). 본 계약은 두 생산자를 **신설 대상으로 규정**한다: (1) 델타 추출이 극성을 **구조화 필드로 발신**(산문 파싱이 아니다), (2) 사용자가 §3.9.1로 직접 단정. 둘 중 어느 것도 구현되기 전에는 `avoids` 배열이 비어 있는 것이 **정상**이며, FE는 빈 회피 목록을 오류로 취급해서는 안 된다.
- **[신규 연동 명시] `purchased`도 현재 생산자가 없다.** 프로필 파이프라인은 구매 이력을 조회하지 않는다(`SPEC-PROFILE-001` REQ-PROF-054 미구현). 또한 I-19의 `categoryName`은 **단일 레벨 이름**(`"키보드"`)이고 카탈로그 정본은 `"대분류 > 잎"`(§4.6)이며 I-19에는 **`brandName`이 없다** — 따라서 구매 파생 카테고리도 대화 파생과 **같은 정규화 경로**를 통과해야 하고, **브랜드 노드는 구매에서 생성될 수 없다**.

#### 실패 응답

| HTTP | `code` | 조건 |
|---|---|---|
| `400` | `BAD_REQUEST` | `includeSuppressed`가 bool로 파싱되지 않음, `{userId}`가 `1..2^63-1` 밖이거나 bool(§3.9 공통과 동일) |
| `401` | `INTERNAL_TOKEN_INVALID` | **[개정 v0.26.0]** 서비스 토큰 누락/불일치(§2.3 b). 구 계약의 `TOKEN_EXPIRED`/`TOKEN_INVALID`(스트림 티켓)는 이 표면에 더 이상 해당하지 않는다 |
| `503` | `UPSTREAM_UNAVAILABLE` | 프로필 저장소 일시 장애 |
| `504` | `UPSTREAM_TIMEOUT` | 예산 초과 |

**[v0.26.0] 삭제된 행 2건** — `403 FORBIDDEN`(판매자 스코프 티켓)은 티켓 `role` 클레임을 전제한 것이라 레인 (b)에서는 발생하지 않고, `429 RATE_LIMITED`는 §2.8이 레인 (a) 사용자 대면에 거는 제한이라 서비스 간 호출에 적용되지 않는다. **`404`는 두지 않는다** — 프로필 미보유는 오류가 아니라 `exists: false`의 정상 `200`이다.

#### 규약

- **[HARD] 요청 경로 LLM 0회.** 프로젝션 원천은 저장된 **구조화 트리플**이며 **프로필 마크다운 본문이 아니다**. 마크다운을 LLM으로 트리플화하는 구현은 비결정적이라 아래 결정론 규약을 만족할 수 없고, 계약 위반이다.
- **[HARD] 근거 원문·프로필 마크다운 본문·prompt·모델 식별자를 응답·로그·trace에 싣지 않는다.** `algorithmVersion`·`modelVersion`을 넣는 구현은 계약 위반이다(§3.7 [HARD]와 동일 기준, §6.3 관측 경계). "왜 이 취향이 있나"는 `source` + `lastConfirmedAt`로 답한다(**[v0.26.0]** `evidenceCount`는 와이어에서 제거됐다 — 아래 규약) — 사용자 발화를 되돌려 보여주지 않는다.
- **[HARD] 민감 카테고리는 존재 자체를 노출하지 않는다.** 질병·임신·종교·정치·성적지향·인종·범죄경력·생체·미성년 지표·정밀위치·금융수단에서 유래한 항목은 node·edge에 포함하지 않고, **어떤 카운트에도 세지 않으며**(placeholder도 두지 않는다) 파생 커머스 취향만 `derivedFromSensitive: true`로 남는다. **주제 라벨 자체는 절대 와이어에 싣지 않는다** — "이 취향은 건강 정보에서 파생됨"은 그 자체로 건강 정보 공개다. 목록 소유는 🔴 C-24.
  - **[HARD] `suppressedCount`와 민감 제외분을 섞지 않는다.** 사용자가 지운 개수에 민감 제외분을 더하면 **그 카운트가 곧 유출**이다. 두 억제는 이름·집계·복구 가능성이 모두 다르다(사용자 삭제 = 복구 가능, 민감 제외 = 복구 불가·비가시).
  - **판정의 한계를 계약이 인정한다** — 하드 PII(주민번호·전화·이메일·카드번호)는 형식 고정이라 결정론적으로 탐지하지만, **민감 *주제* 판정은 완전하지 않다**(`"당뇨"`는 잡아도 `"혈당 관리 중"`은 놓칠 수 있다). 그래서 이 계약이 보장하는 것은 "민감정보를 모두 골라낸다"가 아니라 **"골라내기에 실패해도 근거 원문은 응답·로그·trace에 나가지 않는다"** 이며, 방어선을 판정 정확도에 걸지 않는다.
- **[HARD] 결정론적 프로젝션** — 같은 저장 상태는 항상 같은 응답을 만든다.
  - **중복 불가**: `nodeId`는 `{type}:{정규화 라벨}`, `edgeId`는 `(predicate, nodeId)`에서 파생된다. 같은 대상은 항상 같은 id라 **동일 취향이 두 개의 node·edge로 나타나는 것이 구조적으로 불가능**하다. 같은 키로 수렴한 관측은 병합되며(내부 `evidence_count` 합산 — **[v0.26.0] 와이어에는 노출하지 않는다**, `lastConfirmedAt`은 최댓값, `confidence`는 상위 버킷) 이 병합은 **코드가 결정론적으로** 수행한다 — LLM에 위임하지 않는다(`SPEC-PROFILE-001` REQ-PROF-032/033).
  - **정렬 고정**: `edges`는 `predicate`(`prefers` → `likes` → `avoids` → `interestedIn` → `purchased` 고정 순서) → `lastConfirmedAt` 내림차순 → `edgeId` 오름차순. `nodes`는 `nodeId` 오름차순. 마지막 키가 전순서를 보장한다.
- **[HARD] 순서 전용(order-only)** — 그래프 데이터는 (i) rerank 프롬프트의 **순서 지시**와 (ii) 랭킹 **점수 항**(§3.7 프로필 벡터)에만 들어간다. **검색 필터(§4.6 I-1 쿼리 파라미터)·저장되는 이전 턴 필터로 변환하면 안 된다.** 과거에 프로필을 필터 생성 단계에 주입해 **회원 추천이 게스트보다 부정확해진 실측 회귀**가 있었고(31턴 중 29턴에서 가격·브랜드·평점 축이 오염, 게스트 대비 nDCG@10 −0.288), 필터가 멀티턴 저장소에 영속돼 세션 전체로 증폭된 것이 그 메커니즘이었다. `usagePolicy.filterSafe: false`는 이 규약을 **와이어에 박아** FE·BE가 계약 변경 없이 "필터로 적용"을 만들 수 없게 한다. `avoids`도 **순위 강등까지만**이며 후보 배제로 승격되지 않는다.
- **개인화 중지 중에도 200 + 전체 그래프를 반환한다.** 중지는 "쓰지 않는다"이지 "숨긴다"가 아니다 — 보존된 데이터를 검토하고 지우려면 볼 수 있어야 한다.
- **`graphVersion`은 사용자 편집뿐 아니라 sleep-time consolidation으로도 바뀐다.** 화면을 열어둔 채 밤을 넘긴 사용자의 다음 변경은 정당하게 `409`(§3.9)이며, FE 복구는 재조회 후 재표시다.
- **[v0.26.0 해소] `evidenceCount`를 와이어에서 제거했다** — `profile_buffer_repeat_cap`(기본 2, `app/core/config.py`)이 **같은 발화를 2회로 잘라 세션 버퍼에 담으므로 정확한 관측 횟수를 셀 수 없다.** 반복 통제(#119)가 만든 값을 "몇 번 말했는지"로 노출하면 사실이 아닌 수를 보여주는 것이고, FE가 그 수로 정렬·강조를 만들면 반복 상한이 곧 표시 상한이 된다. 확신도는 **`confidence` 3버킷으로만** 표현한다. **내부 모델의 `evidence_count`는 유지된다** — 병합 시 합산(`SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-015)에 계속 쓰이며, 없앤 것은 와이어 노출뿐이다.
  - 이에 따라 위 [HARD] 항목의 *"'왜 이 취향이 있나'는 `source` + `evidenceCount` + `lastConfirmedAt`로 답한다"* 는 **`source` + `lastConfirmedAt`** 로 축소된다.
- **[v0.26.0 해소] 타입 비대칭이 사라졌다** — 구 계약은 본 절 응답의 `userId`를 **string**(토큰 `sub` 그대로), §3.9 경로의 `{userId}`를 **숫자 BIGINT**로 두고 "조회는 토큰 신원을 되돌려주고 변경은 Spring이 도출한 회원 id를 받으므로 출처가 다르다"를 근거로 삼았다. **프록시 전환으로 두 출처가 같아졌으므로 근거가 소멸했고, 양쪽 모두 number(BIGINT)다**(§2.6 / I-20 / I-22 선례와 정합).

#### 구현 노트 (v0.22.0, #149) — 계약이 아직 코드에 없는 지점

본 절과 §3.9는 **구현이 없다**(#150). 아래는 착수자가 먼저 알아야 하는 사실이다.

1. **선결(blocking) — 결정론적 트리플이 없다.** 현재 fact store item 값은 `{"fact": str}` 자유형 한국어 한 필드이고 dedup은 문자열 완전 일치뿐이다(`SPEC-PROFILE-001` OPEN-P12). 위 "중복 불가" 규약은 **consolidation이 구조화 트리플을 함께 산출하도록 만든 뒤에야** 달성 가능하다. 그때까지 변환되지 않은 fact는 `unprojectedCount`로만 관측된다.
2. **기존 데이터로 과거 그래프를 복원할 수 없다.** 델타 추출이 산출한 `salience`·`explicit`·`repetitionEma`는 승격 판정 직후 버려지고 store에는 fact 문자열과 `created_at`만 남는다. 부트스트랩은 `source`를 보존하지 못하는 **정의된 best-effort 투영**이며 "그 그래프"가 아니다. 그래프는 **다음 배치부터 누적**된다.
3. **억제가 실효하려면 consolidation이 그래프를 읽어야 한다.** 현재 consolidation은 fact 목록을 그대로 읽으므로, 그래프에서만 suppress하면 **다음 배치가 삭제한 취향을 마크다운에 다시 써넣는다.** 이 변경이 없으면 삭제 기능은 겉모습만 남는다.
4. **~~살아 있는 레이스~~ — 🟢 해소(#323, 2026-08-06).** v0.22.0 시점에는 요약 쓰기 경로가 잠금 없이 갱신하는데 fact 추가 경로만 per-user 잠금을 잡아, 그래프 변경이 요약 측 플래그를 쓰면 동시 consolidation에 덮일 수 있었다. `set_summary`에 `profile:summary:{userId}` per-user 잠금이 추가되어(fact 잠금과 키 분리 — `record_remember` hot-path가 요약 쓰기와 불필요하게 직렬화되지 않게) 이 레이스는 닫혔다. **다만 "fact 읽기 ~ 요약 쓰기 사이의 fact 변경" 정합성은 여전히 미해결이며 #150/#358의 revision CAS·억제 필터 축에서 다룬다** — 이 절의 `graphVersion` 낙관적 동시성이 그 자리다.
5. 예산(2s / §3.9 3s)은 **제안이며 실측이 아니다**. §2.9 (c) 기준표에 행을 추가하지 않은 이유가 이것이다.

### 3.9 개인화 그래프 제어 API (I-33~I-37, Spring → AI) [v0.22.0 신설, 재채번 v0.26.0, 🔴 제안(초안)]

사용자가 잘못 학습된 취향을 **수정·삭제·복구**하고, **전체 초기화**와 **개인화 중지·재개**를 수행하는 5종이다. FE는 Spring의 마이페이지 엔드포인트를 호출하고, **Spring이 이 internal 계약으로 AI에 위임**한다.

> **🔴 제안(초안) · Post-MVP** — §5 C-20~C-28 미합의. I-번호 채번은 🔴 C-26.
>
> **[v0.26.0, #322] ✅ I-번호 충돌 해소 — I-29~I-33 → I-32~I-37 재채번 완료.** v0.25.0(#297)이 경고한 충돌이 실제 발생했다: 정본(노션 "📡 API 명세서" DB)에 **I-29~I-31이 판매자 주문·리뷰 API**(§4.18~§4.20, 2026-08-04 등재)로 배정돼 있었고, 본 절의 채번은 등재 당시부터 미확정(🔴 C-26)이었다. **2026-08-05 노션 정본 등재로 개인화 그래프 6종이 I-32~I-37로 확정**됐다(판매자 3종은 정본 번호 유지).
>
> | I- | 대상 | 절 | Spring 대면 프록시 |
> |---|---|---|---|
> | **I-32** | 조회 | §3.8 | `M-11` |
> | **I-33** | 수정 | §3.9.1 | `M-12` |
> | **I-34** | 삭제 | §3.9.2 | `M-13` |
> | **I-35** | 되돌리기 | §3.9.3 | `M-14` |
> | **I-36** | 전체 초기화 | §3.9.4 | `M-15` |
> | **I-37** | 개인화 중지·재개 | §3.9.5 | `M-16` |
>
> **5종이 아니라 6종인 이유**: 조회(§3.8)가 같은 레인으로 합류했다(위 §3.8). v0.25.0이 제안했던 `I-34~I-38`은 조회를 세지 않은 5종 기준이었고, **정본은 조회를 포함한 I-32~I-37로 확정됐다** — 그 제안 문구는 폐기한다. **`M-11`~`M-16`(Spring 대면 마이페이지 6종)은 Spring 소유이며 본 문서의 계약 대상이 아니다**(존재만 기록). 🔴 C-26의 잔여는 "BE가 이 번호를 최종 수용하는지"뿐이다.

```
X-Internal-Token: {서비스 토큰}          ← internal 그룹(레인 b)
If-Match: "g42"                          ← §3.8 응답의 graphVersion (§3.9.5만 선택)
```

> **[HARD·보안] 경로의 `{userId}`는 Spring이 자기 로그인 세션에서 도출한 값이다** — FE 입력(본문·쿼리·헤더)에서 온 값을 그대로 실어서는 안 된다. `X-Internal-Token`이 필수이며 **사용자 JWT나 body의 임의 신원을 대신 신뢰하지 않는다**(§3.5.1과 동일 규칙). AI는 이 경로들에서 `Authorization` 헤더를 **인증 수단으로 인정하지 않는다.**
>
> **[HARD, 개정 v0.26.0] 그래프 표면은 조회·변경 모두 이 레인이다 — 비대칭은 폐기됐다.** v0.22.0은 *"조회는 FE 직접, 변경은 Spring 경유 — 이 비대칭은 의도된 것이며 '통일'하면 안 된다"* 를 [HARD]로 두었다. 그 근거는 "조회는 이미 배포·검증된 `chat:stream` 티켓 경로를 재사용해 공유 인증 경로의 회귀 반경을 0으로 유지한다"였는데, **재사용할 티켓이 없다는 것이 드러났다** — 마이페이지에는 채팅 세션이 없고 CH-1b는 `sessionId`가 필수라 프로필 화면은 티켓을 발급받을 수 없다. **없는 자산을 재사용한다는 전제 위의 [HARD]였으므로 폐기한다**(#322, 노션 정본 등재 2026-08-05).
>
> **폐기되지 않은 부분**: 변경 경로에 (1) 로그인 세션에서 도출된 신원과 (2) 감사 actor가 필요하고 60초짜리 스트림 티켓이 둘 다 제공하지 못한다는 판단은 그대로다. 전용 `profile:read`/`profile:write` 티켓 신설안도 **여전히 기각**이다 — `scope`는 exact `chat:stream`으로 하드 고정돼 있고 그 검증 경로를 `/chat`·`/seller/chat`이 함께 지나가므로, 프로필 기능을 위해 **채팅 인증에 회귀 위험을 만드는 대가**를 치른다(§2.3 a). 조회를 레인 (b)로 옮긴 것은 그 기각을 **뒤집은 것이 아니라 같은 이유로 한 걸음 더 간 것**이다. 이 결정은 🔴 C-20이며, 확약 대상 경로가 **5개 → 6개**로 늘었다.
>
> **[HARD] Spring은 `If-Match`를 verbatim 통과시키고, `409` 응답 본문의 `error` 객체(`code`·`message`·`requestId`·`detail.graphVersion`)를 변형 없이 FE에 전달한다.** 재따옴표·재인용·자체 생성 금지. **[v0.26.0]** `graphVersion`이 봉투 밖 형제에서 `error.detail` 안으로 들어왔으므로 **`error` 객체를 통째로 전달하면 되고 필드를 옮겨 담을 필요가 없다**(§2.5 확장 규칙). 🔴 C-21.

- **예산(제안, 실측 아님)**: 응답 **3s**. **[HARD] 요청 경로 LLM 0회.**
- **멱등** — **§2.7의 `/events/*` 멱등 규약은 본 절에 적용되지 않는다**(202 `accepted`/`duplicate` 없음, 동기 200 + 새 상태). 대신 **파생 키**로 판정한다: `profile-graph-{action}:{userId}:{scopeId}:{ifMatch}`. `If-Match`가 곧 멱등 키를 겸하므로(네트워크 재시도는 같은 precondition을 지참한다) 재전송은 **최초 응답 본문을 그대로** + `"replayed": true`로 반환한다. 클라이언트 지정 `Idempotency-Key` 헤더는 **도입하지 않는다** — 저장소 전역에 그 개념이 없고, §2.7의 취지가 "파생 키, 클라이언트 지정 금지"다.
- **`If-Match` 형식**: `If-Match: "g42"`와 `If-Match: g42`는 동등하다. `*`·약한 태그(`W/"g42"`)·누락·빈 값은 **`400 BAD_REQUEST`** 다(428은 §2.5에 없는 상태라 도입하지 않는다).
- inbound 본문은 **camelCase alias만** 허용한다. unknown field·snake_case field·camelCase+snake_case collision은 `400 BAD_REQUEST`(§3.5.1과 동일).
- **노드는 변경 대상이 아니다** — 노드는 edge의 파생물이라 자신을 참조하는 edge가 사라지면 함께 사라진다. 노드 편집을 허용하면 같은 상태로 가는 쓰기 경로가 둘이 된다.

#### 3.9.1 `PATCH {AI_SERVER}/internal/profile/{userId}/graph/edges/{edgeId}` (I-33) — 취향 수정

```json
{ "predicate": "avoids", "object": { "type": "brand", "label": "소니" } }
```

**[v0.26.0 신설] 기존 노드를 직접 지목하는 형태** — FE 자동완성으로 §3.8이 내려준 노드를 고른 경우:

```json
{ "predicate": "avoids", "object": { "nodeId": "brand:소니" } }
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `predicate` | string | 아니오 | `prefers` \| `likes` \| `avoids` \| `interestedIn`. **`purchased` 지정은 `400`** — 구매는 의견이 아니라 사실이라 사용자가 만들 수 없다 |
| `object.type` | string | `type`+`label` 형태에서 예 | §3.8 node type. `user`류 자기 노드는 없다 |
| `object.label` | string | `type`+`label` 형태에서 예 | 1 ~ config `profile_graph_label_max_chars`자. 정규화 후 `nodeId`가 된다 |
| `object.nodeId` | string | `nodeId` 형태에서 예 | **[신규 v0.26.0]** §3.8 `nodes[].nodeId`와 동일 형식(`{type}:{정규화 라벨}`). **resolver(라벨 정규화·근접 매칭)를 건너뛰고 그 노드를 직접 참조한다** |

- `object`를 지정할 때는 **`type`+`label` 형태와 `nodeId` 형태 중 정확히 하나**여야 한다. **둘을 함께 실으면 `400`**(어느 쪽이 우선인지가 계약에 없고, 우선순위를 정하는 순간 무시된 필드가 조용한 오작동이 된다).
- **`nodeId` 형태를 두는 이유**: FE가 화면에 있는 노드를 골랐는데 서버가 그 라벨을 **다시 정규화**하면, 어휘·임계값 변화에 따라 **사용자가 고른 것과 다른 노드로 튈 수 있다**(resolver는 근접 매칭을 쓴다 — `SPEC-PROFILE-GRAPH-149` §6.2). 이미 확정된 노드를 참조할 때는 재정규화가 이득 없이 위험만 만든다. `nodeId` 형태에서는 **형식 검증만** 수행하며, 형식은 맞지만 해당 사용자 그래프에 없는 `nodeId`는 새 노드로 만들지 않고 `400`이다(존재하지 않는 노드를 지목하는 것은 자동완성 경로에서 나올 수 없는 요청이다).

`predicate`·`object` 중 **최소 하나**는 있어야 한다(둘 다 없으면 `400`).

**성공 응답 — 200**

```json
{
  "userId": 123,
  "graphVersion": "g43",
  "edge": {
    "edgeId": "e_9d41c7b0e8a2f356",
    "to": "brand:소니",
    "predicate": "avoids",
    "source": "user",
    "origin": "user",
    "confidence": "HIGH",
    "firstSeenAt": "2026-07-02T11:20:00Z",
    "lastConfirmedAt": "2026-08-05T02:31:44Z",
    "editable": true,
    "suppressed": false,
    "suppressedAt": null,
    "challenged": false,
    "derivedFromSensitive": false
  },
  "merged": false,
  "replayed": false
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | number(BIGINT) | 경로 값 echo(Spring 도출 신원) |
| `graphVersion` | string | 변경 후 버전. 다음 변경의 `If-Match` 값 |
| `edge` | object | 변경 후 edge. 필드 의미는 §3.8과 동일 |
| `merged` | bool | 새 `(predicate, object)`가 기존 edge와 겹쳐 **병합**됐는지 |
| `replayed` | bool | 같은 파생 키의 재전송이라 최초 응답을 되돌려준 것 |

**규약**

- 사용자 수정은 항상 `source: "user"` · `origin: "user"` · `confidence: "HIGH"` 로 승격된다.
- **`edgeId`가 바뀐다** — `edgeId`는 `(predicate, nodeId)` 파생이므로 관계나 대상을 바꾸면 새 값이다. 응답의 `edge.edgeId`로 클라이언트 키를 교체해야 하며, 그러지 않으면 다음 변경이 `404`다.
- 새 트리플이 기존 edge와 충돌하면 **병합**하고 `merged: true`를 반환한다(내부 `evidence_count` 합산 — 와이어 미노출, `lastConfirmedAt` 최댓값).
- **[HARD] 사용자 수정은 기계 재파생에 덮이지 않는다.** 수정 이후 sleep-time 배치가 같은 취향을 다시 추출해도 관측 횟수·최근 시각만 갱신되고 **관계·상태·확신도는 변경되지 않는다.** 이 보장은 만료되지 않는다 — 만료를 두면 사용자의 수정이 조용히 되돌려진다. 취향이 실제로 바뀐 경우의 탈출구는 **보이는 방식**뿐이다: 반대 관측이 config 임계 이상 쌓이면 `challenged: true`가 뜨고 **상태는 그대로**이며, 명시적 사용자 변경만 고정을 푼다.

#### 3.9.2 `DELETE {AI_SERVER}/internal/profile/{userId}/graph/edges/{edgeId}` (I-34) — 개별 삭제(즉시 억제 → undo 창 → 원문 물리 삭제)

Body 없음(I-12 삭제 선례와 동일 — 신원·대상이 전부 경로에 있다).

**성공 응답 — 200**

```json
{
  "userId": 123,
  "graphVersion": "g44",
  "edgeId": "e_7b1c9a04e5f2438d",
  "suppressed": true,
  "suppressedAt": "2026-08-05T02:33:10Z",
  "restorable": true,
  "replayed": false
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | number(BIGINT) | 경로 값 echo |
| `graphVersion` | string | 변경 후 버전 |
| `edgeId` | string | 삭제한 edge |
| `suppressed` | bool | 항상 `true` |
| `suppressedAt` | string | 삭제 시각 |
| `restorable` | bool | §3.9.3으로 되돌릴 수 있는지. **[개정 v0.26.0]** 응답 시점에는 항상 `true`이지만 그 유효 범위는 **undo 창 이내**다 — 창이 닫히면 원문이 물리 삭제되어 이후 restore는 `404`다(아래 규약) |
| `replayed` | bool | 재전송 판정 |

**규약**

- **[개정 v0.26.0, #322] 삭제는 "즉시 억제 → undo 창 → 원문 물리 삭제"다.**

  ```
  사용자 삭제
    → 즉시 투영·요약·랭킹에서 제외 (suppressed, §3.9.3으로 복구 가능)
    → undo 창 (config `graph_undo_window_s`, 기본 5분)
    → 만료 시 원문 edge·근거 fact 물리 삭제 (purge)
    → tombstone(내용 파생 id)만 잔존 — 원문 없음, 재승격 차단 전용
  ```

  구 계약(v0.22.0)은 억제만 하고 원문을 무기한 보관했다. **사용자가 "지웠다"고 믿는 문장의 원문을 계속 들고 있을 이유가 없다**(데이터 최소화). 구 계약이 원문을 남긴 명분은 복구 가능성이었는데, undo 창이 그 역할을 대신한다. 규격은 `SPEC-PROFILE-GRAPH-149` §6.3.
- **tombstone은 시간 경과로 만료되지 않는다.** 만료시키면 undo 창이 닫힌 직후 세션 버퍼 flush(주기 `profile_idle_sweep_interval_s`)가 돌면서 **같은 발화가 재승격돼 방금 지운 취향이 몇 분 뒤 부활**한다. 다만 "영구"는 *자동 만료 없음*이지 *사용자도 못 지움*이 아니다 — 전체 초기화(§3.9.4)는 tombstone도 함께 지운다. 억제 해제는 §3.9.3(창 이내) 또는 **사용자의 명시적 재입력**으로만 일어난다.
- **[HARD] undo 창 길이는 서버 config이며 와이어에 싣지 않는다.** 잔여 시간 표시가 필요하면 FE 표현 계약(🟡 C-25)에서 다룬다 — 튜너블을 와이어에 노출하면 값을 바꿀 때마다 계약 변경이 된다.
- **확인 UX는 파괴력에 비례한다** — 개별 삭제는 **확인 다이얼로그 없이 즉시 + undo**, 확인이 필수인 것은 복구 불가한 **전체 초기화(§3.9.4)뿐**이다. (채팅으로 삭제를 받는 경로("내 취향 지워줘")는 구매자 SSE에 판매자 `draft` 같은 승인 이벤트가 없어 **신규 계약이 필요**하다 — 본 계약 범위 밖이며 별도 판단이다, §3.2 HITL·#78 선례.)
- 이미 suppress된 edge에 같은 `If-Match`로 재전송하면 `replayed: true`(상태·버전 불변). **단 undo 창이 이미 닫힌 edge는 `404`다**(아래 §3.9.3 규약과 같은 이유).
- **`purchased` edge도 숨길 수 있다.** 단 **이것이 재구매 dedup(결정 14-F, §4.7)에 영향을 주지 않는다** — dedup은 프로필이 아니라 질의 시점 I-19를 읽는다. "구매 기록을 지웠으니 다시 추천되겠지"는 성립하지 않는다.

#### 3.9.3 `POST {AI_SERVER}/internal/profile/{userId}/graph/edges/{edgeId}/restore` (I-35) — 삭제 되돌리기

요청 본문 `{}`(빈 객체).

**성공 응답 — 200**

```json
{
  "userId": 123,
  "graphVersion": "g45",
  "edgeId": "e_7b1c9a04e5f2438d",
  "suppressed": false,
  "suppressedAt": null,
  "replayed": false
}
```

필드 의미는 §3.9.2와 같다(`restorable` 없음).

**규약**

- suppress 상태가 아닌 edge에 대한 restore는 **no-op 200**(`replayed: true`, 버전 불변).
- **물리 삭제된 뒤에는 `404 PROFILE_EDGE_NOT_FOUND`다.** 물리 삭제 트리거는 두 가지다 — 전체 초기화(§3.9.4)와 **[신규 v0.26.0] undo 창 만료**(§3.9.2).
- **[HARD, v0.26.0] purge된 edge에 대한 재전송은 멱등 원장 히트 여부와 무관하게 `404`다.** 파생 키 원장(§3.9 preamble)은 재전송에 최초 응답을 그대로 재생하는데, **원장 TTL(`graph_idempotency_ttl_h`, 시간 단위)이 undo 창(분 단위)보다 길다.** 규정이 없으면 창이 닫혀 원문이 사라진 뒤 도착한 restore 재전송이 원장에 히트해 **"복구됨" 200을 재생**한다 — 실제로는 되돌릴 대상이 없는데 클라이언트에는 성공으로 보인다. purge 시점에 해당 edge의 원장 항목을 무효화한다.
- **논리적 만료가 물리 삭제 실행보다 우선한다** — 스윕이 아직 돌지 않아 저장소에 행이 남아 있어도, `suppressedAt + graph_undo_window_s`가 지난 edge는 이미 purge된 것으로 취급한다(restore `404`, §3.8 조회 미노출). 만료 판정과 삭제 실행 사이의 레이스를 계약이 막는다(`SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-027).

#### 3.9.4 `POST {AI_SERVER}/internal/profile/{userId}/graph/reset` (I-36) — 전체 초기화(물리 삭제)

```json
{ "scope": "ALL" }
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `scope` | string | 예 | 현재 `"ALL"` 하나만 허용. 미지 값은 `400`. **파괴 범위를 호출자가 명시적으로 이름 붙이게** 하는 판별자다 |

**성공 응답 — 200**

```json
{
  "userId": 123,
  "graphVersion": "g46",
  "purged": { "edges": 12, "nodes": 9, "facts": 12, "suppressed": 2, "conversationTurns": 143 },
  "personalization": { "enabled": true, "disabledAt": null },
  "replayed": false
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | number(BIGINT) | 경로 값 echo |
| `graphVersion` | string | 초기화 후 버전. **단조 증가하며 초기화로 되돌아가지 않는다**(재사용하면 `If-Match`가 모호해진다) |
| `purged` | object | **개수만.** 라벨·원문은 싣지 않는다. **[v0.26.0] `conversationTurns` 추가** — 전사록이 삭제 대상에 포함되면서, 이 객체가 파괴 범위를 보고한다고 하면서 가장 큰 항목을 빼놓는 상태가 되기 때문이다 |
| `personalization` | object | **초기화는 이 값을 바꾸지 않는다**(아래 규약) |
| `replayed` | bool | 재전송 판정 |

**규약**

- **파괴적·비가역이다.** 대상: 그래프 node·edge, fact 항목, 요약(마크다운 **및** 요약 임베딩), suppress tombstone(undo 창 이내분과 영구 tombstone 모두), 미처리 세션 버퍼, 누적 게이트 상태, **[신규 v0.26.0] 대화 전사록(`conversation_turns` 중 해당 `userId`의 행 전체)**.
- **[개정 v0.26.0, #322] 전사록이 보존 대상에서 삭제 대상으로 옮겨졌다.** 근거는 이 계약이 `SPEC-PROFILE-001` REQ-PROF-034에서 **이미 한 번 채택한 논거의 연장**이다 — *"금지 대상은 기계가 조용히 지우는 것이고, 사용자 자신의 삭제권은 별개다"*. 전사록도 같다. "전사록을 보존한다"의 대상은 기계·운영이지, **사용자가 명시적으로 전체 초기화를 요청한 경우까지 붙잡고 있으라는 뜻이 아니다.** 보존 원칙의 *예외*가 아니라 **적용 범위 한정**이다. 이 개정으로 `SPEC-PROFILE-GRAPH-149` OPEN-G6(민감 파생은 만료되는데 원인 원문은 전사록에 남는 비대칭)이 **해소**된다.
  - **AI 단독으로 완결된다** — 대화 기록은 AI DB(`pg-profile`의 `conversation_turns`)에만 있고 **Spring에 사본이 없다**(#322 선결 확인, 2026-08-06). 삭제 대상 특정은 `conversation_turns.user_id`로 한다.
  - **전사록 자연 만료(TTL)와는 별개 트리거다** — 그쪽은 시간 경과로 지우는 보존 기간 정책(`SPEC-PROFILE-001` OPEN-P5, 미확정)이고 본 절은 사용자의 명시적 요청으로 지운다. 둘 중 하나가 없어도 다른 하나는 성립한다. **본 절은 TTL 자체를 정하지 않는다.**
  - **세션 종료(§3.5 I-20)는 여전히 전사록을 지우지 않는다** — 로그아웃·비활동 종료는 사용자의 삭제 요청이 아니다. 구 계약이 §3.5의 *"transcript는 삭제하지 않는다"* 를 본 절의 근거로 인용했는데, **맥락이 다른 문장이라 인용을 제거한다**(§3.5 자체는 불변).
- **보존 대상: 변경 감사 로그(§6.3 c).** 감사 로그는 *초기화가 일어났다는 기록 자체*라 함께 지우면 파괴 동작이 추적 불가가 된다. **원문·라벨을 담지 않고 지문(peppered HMAC)만 남기므로**(§6.3 c) 사용자 데이터를 붙잡고 있는 것이 아니다.
  - **[FE 문구 주의]** **[재작성 v0.26.0]** 전사록까지 포함되면서 이 동작은 **"내 데이터를 모두 삭제"에 훨씬 가까워졌다.** 다만 변경 감사 로그(시스템 운영 기록 — *무엇을* 지웠는지가 아니라 *언제 지웠는지*의 메타데이터)는 남으므로 "모든 시스템 기록 삭제"는 아니다. 구 계약의 *"'모든 데이터 삭제'로 표시하면 사실과 다르다"* 는 **전사록이 남는다는 전제 위에 있었으므로 더 이상 성립하지 않는다.** 정확한 문구 후보는 **"대화 기록을 포함한 개인화 데이터 초기화"** 이며 최종 문구는 FE 소유다(🟡 C-25). 감사 로그 보존 기간과 개인정보 삭제 요청 경로와의 관계는 🔴 C-23의 잔여분이다.
- **`personalization.enabled`를 바꾸지 않는다** — 초기화와 개인화 중지는 별개 제어다. 지우고도 계속 쓰거나, 끄고도 데이터를 남길 수 있어야 한다.

#### 3.9.5 `PUT {AI_SERVER}/internal/profile/{userId}/personalization` (I-37) — 개인화 중지·재개

```json
{ "enabled": false }
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `enabled` | bool | 예 | `false` = 중지, `true` = 재개 |

**성공 응답 — 200**

```json
{
  "userId": 123,
  "graphVersion": "g47",
  "personalization": { "enabled": false, "disabledAt": "2026-08-05T02:40:00Z" },
  "replayed": false
}
```

필드 의미는 §3.8 `personalization` + 위 공통 필드와 같다.

**규약**

- **`If-Match`는 선택이다**(주면 반드시 존중한다). 상태 *단정*이라 lost update가 없고, 프라이버시 스위치가 백그라운드 consolidation 때문에 막히면 안 된다 — 안전장치가 기계가 바쁠 때 잠기는 설계는 틀렸다.
- **중지 = 사용 중지 + 수집 중지, 데이터는 보존.** 재개하면 보존된 취향이 그대로 복원된다.
  - **사용 중지**: rerank 프롬프트 주입 없음(회원 프롬프트가 게스트와 바이트 동일해진다), §3.7 홈 랭킹의 프로필 벡터 항 제거, §3.4 마크다운은 `null`.
  - **수집 중지**: 세션 버퍼 적재·델타 추출·요약 재생성이 모두 중단된다. **명시적 "기억해" 요청도 저장하지 않는다** — 전역 중지가 개별 요청보다 우선이며, FE는 그 사실을 사용자에게 알려야 한다.
  - **중지 기간의 발화는 영구히 반영되지 않는다.** 전사록에서 소급 추출하지 않는다 — 그것은 "수집 중지"를 사후에 위반하는 일이다. 이는 누락이 아니라 규칙이다.
- **와이어 관측 결과**: 중지 중 §3.7 I-22는 `outcome: "NO_PROFILE"` 을 반환하고 Spring은 기존대로 P-4로 대체한다 — **Spring 무변경**(새 `outcome` 값을 만들지 않는다).
- **중지 상태에서도 §3.8 조회와 §3.9.1~§3.9.4가 모두 허용된다** — 보존된 데이터를 정리하려고 개인화를 다시 켜야 하는 상황을 만들지 않는다. 따라서 **중지 여부가 어떤 요청의 실패로도 추론되지 않으며**, 별도의 "중지됨" 오류 코드는 존재하지 않는다.
- 이미 요청 상태와 같으면 **no-op**: 감사 행을 남기지 않고 버전도 바뀌지 않으며 `replayed: true`다.
- 🔴 **Spring 측 캐시 무효화가 필요하다** — P-5 개인화 결과 캐시(10분, §4.11)는 **Spring 소유라 AI가 비울 수 없다.** Spring이 중지 시점에 해당 회원 캐시를 무효화하지 않으면 최대 10분간 개인화 홈이 계속 보인다. 🔴 C-27.

#### 공통 실패 응답 (§3.9.1~§3.9.5)

| HTTP | `code` | 조건 |
|---|---|---|
| `400` | `BAD_REQUEST` | strict 스키마 위반(unknown·snake_case·collision), 필수 필드 누락, `predicate`/`object` 둘 다 누락, `predicate: "purchased"` 지정, 라벨 길이 초과, **[v0.26.0] `object.nodeId`와 `object.type`/`object.label` 동시 지정**, **[v0.26.0] `object.nodeId` 형식 위반 또는 해당 사용자 그래프에 없는 `nodeId`**, `scope` 미지 값, `If-Match` 누락·빈 값·`*`·약한 태그, `{userId}`가 `1..2^63-1` 밖이거나 bool |
| `401` | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 누락/불일치 |
| `404` | `PROFILE_EDGE_NOT_FOUND` | `{edgeId}`가 **해당 `{userId}`의** 그래프에 없음 — 남의 edge든 존재하지 않든 **동일 응답** |
| `409` | `PROFILE_VERSION_CONFLICT` | `If-Match` ≠ 현재 `graphVersion` |
| `409` | `PROFILE_EDGE_NOT_EDITABLE` | `editable: false`(=`purchased`) edge에 대한 §3.9.1 수정 |
| `503` | `UPSTREAM_UNAVAILABLE` | 프로필 저장소 일시 장애 |
| `504` | `UPSTREAM_TIMEOUT` | 예산 초과 |

`409 PROFILE_VERSION_CONFLICT` 본문:

```json
{
  "error": {
    "code": "PROFILE_VERSION_CONFLICT",
    "message": "프로필이 그 사이에 변경되었습니다. 다시 조회한 뒤 시도해 주세요.",
    "requestId": "b31d0c7f9a2e4f18",
    "detail": { "graphVersion": "g43" }
  }
}
```

- 최신 `graphVersion`을 **`error.detail.graphVersion`** 에 병기한다 — `message`는 "PII 미포함 사람이 읽는 문자열"로 고정돼 있어 기계 판독 값을 그 안에 섞지 않고, §2.5가 `detail`을 그 확장 자리로 규정한다(§4.1 `detail.availableStock`·`detail.options`와 동일 패턴).
  - **[v0.26.0 위치 정정, #322]** v0.22.0 등재 시점에는 이 값을 `error` 봉투 **밖** 최상위 형제로 두고 "§2.5가 `error` 필드를 3개로 고정하므로"를 근거로 삼았는데, 그 근거는 **§2.5에 `detail`이 등재돼 있지 않았던 상태를 사실로 오인한 것**이었다 — 같은 문서 §4.1이 이미 `error.detail.*`를 쓰고 있었다. 이번에 §2.5에 `detail`을 공식화하고 본 절을 그쪽으로 통일한다. 부수 효과로 **🔴 C-21(Spring이 `409` 본문을 변형 없이 통과)이 쉬워진다** — `error` 객체 하나를 통째로 전달하면 되고 필드를 봉투 안팎으로 옮겨 담을 필요가 없다.
  - ⚠️ **여기서 말하는 `detail`은 §2.5의 와이어 필드다.** 아래 구현 노트가 말하는 "응답 detail로 코드를 덮어쓴다"의 `detail`은 FastAPI 예외 객체의 관용어이며 **서로 다른 것**이다.
- **교차 사용자 접근이 `403`이 아니라 `404`인 이유**: edge 조회는 `(userId, edgeId)`로 스코프되므로 "남의 것"과 "없음"은 **같은 질의 결과**다. 403을 요구하면 남의 데이터 존재를 확인하기 위한 **비스코프 조회를 의무화**하게 되고, 그것은 막으려는 열거 오라클을 스스로 만드는 일이다. **[v0.26.0] `403 FORBIDDEN`은 그래프 표면에서 더 이상 쓰이지 않는다** — 유일한 용례가 "판매자 티켓의 §3.8 조회"였는데, 레인 (b) 전환으로 티켓 `role` 클레임 자체가 사라졌다. 주체 검증은 `X-Internal-Token`이 담당하며 실패는 `401 INTERNAL_TOKEN_INVALID`다.
- **충돌이 `409`이고 `412`가 아닌 이유**: (1) `409`에는 이미 "내 상태 인식이 서버와 어긋남" 3형제(`SESSION_ACTIVE`·`SESSION_FINALIZING`·`SESSION_CLAIM_CONFLICT`)가 있어 FE·Spring이 한 분기로 처리한다. (2) **`412`는 AI 서버의 상태→코드 매핑에 없어 기본값이 일반 코드로 나간다** — §2.5가 요구하는 기계 판독용 코드를 기본적으로 위반한다. `If-Match`는 요청 측 메커니즘으로 그대로 유지하므로 조건부 요청 규약 자체는 지킨다. 🔴 C-22.

#### 구현 노트 — §3.9 오류 매핑 함정 (v0.22.0, #149)

§3.8 구현 노트 1~5가 그대로 적용된다. 추가로 오류 매핑 함정 2건:

1. **⚠️ `409`의 기본 코드는 `STREAM_IN_PROGRESS`다.** `409`를 코드 지정 없이 던지면 FE에 "스트림 진행 중"이 표시된다 — `PROFILE_VERSION_CONFLICT`·`PROFILE_EDGE_NOT_EDITABLE`은 **반드시 응답 detail로 코드를 덮어써야** 한다. 정상 경로 테스트로는 잡히지 않는 결함이다.
2. **⚠️ `404`는 상태→코드 매핑에 아예 없다** — 덮어쓰지 않으면 일반 코드가 나간다. §2.5에 `404 NOT_FOUND`를 등재했으니 매핑에 기본 항목을 추가하고, edge 케이스는 detail로 `PROFILE_EDGE_NOT_FOUND`를 지정한다. **`412`를 채택하지 않았으므로 그쪽 매핑 추가는 불필요하다.**
3. **낙관적 동시성 기계장치가 저장소에 전혀 없다** — `ETag`/`If-Match`/precondition 처리도, `412`도 없다. 있는 자산은 세션 lifecycle의 generation 카운터·advisory lock·claim/lease이며, `graphVersion`은 그 위에 얹는다.

---

## 4. AI 서버 ↔ Spring 역방향/전제 계약

AI → Spring 역방향은 **정확히 18건**이다:
`{I-1,I-3,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}`.
각 이름과 계약 위치는 §1.2 레인 (c)를 따르며, FE ↔ Spring 전제 계약(목록 GET §4.3)은
이 집합에 포함하지 않는다. 모든 internal 호출은 `X-Internal-Token`을 사용하고, 사용자·판매자
스코프 신원은 AI가 검증 JWT 클레임에서만 도출한다.

### 4.1 장바구니 담기 API (I-2, 결정 7) — BE 문서 채택 [v0.6.0]

**BE 팀 "챗봇 장바구니 담기"(No. I-2) 문서를 계약 기준으로 채택**한다. AI 서버는 "담아줘" 자연어에서 (상품, 옵션, 수량) 의도만 확정하고, 담기 실행·검증은 Spring에 위임한다(결정 7 유지). 구 v0.3.0 제안(JWT 포워딩 + `items[]` 다건)은 **폐기**한다.

#### AI → Spring 요청 (I-2 확정)

```
POST {SPRING_BASE_URL}/internal/cart/items
X-Internal-Token: {서비스 토큰}   ← internal 그룹, 타임아웃 권장 3s
```

```json
{ "userId": 123, "guestId": null, "productId": 1, "optionId": null, "quantity": 1 }
```

| 요청 필드 | 타입 | 설명 |
|---|---|---|
| `userId` / `guestId` | number / string \| null | **둘 중 하나** — 챗 요청의 메아리(`userId`=숫자, `guestId`=UUID 문자열, §2.6). AI가 신원을 만들지 않고 **AI-검증 JWT `sub`에서 도출**해 전달한다(FE 본문 값 사용 금지, §2.3) |
| `productId` | number | 담을 상품 식별자(숫자 BIGINT, §2.6) |
| `optionId` | number \| null | 상품 옵션. 옵션 필수 상품인데 null이면 `400 CART_OPTION_REQUIRED`(아래) |
| `quantity` | int | **1~99, 합산 포함** — 동일 상품·옵션이 이미 있으면 **Spring이 수량 합산**(입구가 달라도 같은 CartService 검증) |

- **단건 계약** — Case 3 묶음 담기는 상품별로 **반복 호출**한다(항목별 성공/실패가 자연 분리되므로 SSE `action`도 항목별 emit).
- **게스트 담기 허용** — `role == "guest"`여도 `guestId`로 담기 성공(BE 02 D30, 2026-07-10 개정 — 기존 403 유도 폐기). **로그인 유도는 결제 시점 FE 몫.** 구 AI-side 차단(`GUEST_NOT_ALLOWED`)은 폐기 — **결정 8 개정 필요(§8 항목 7)**.
- **합산 안내(v0.6.0)**: 담기 전 §4.9 조회로 동일 상품·옵션 기존 보유를 확인하면 "이미 담겨 있어 N개로 늘렸어요"처럼 안내할 수 있다. **합산의 권위는 Spring**(조회는 안내용 — 조회 실패 시에도 담기는 진행).
- **부수효과 없음 — 담기 이벤트는 서버가 적재하지 않는다** **[정정 v0.15.26]**. 구 서술("`CART_ADD(via: chat)` 이벤트는 BE가 적재")은 **폐기**한다. E-1 정본에서 `add_to_cart`는 **FE가 쏘는 12종 중 하나**이며, 서버가 직접 적재하는 이벤트는 `recommendation_generated` **하나뿐**이다(그것도 E-1 HTTP로 들어오면 드롭). 챗봇 경로도 FE가 SSE `action`(`CART_ADDED`)을 받은 시점에 `add_to_cart`를 쏜다.

#### 성공 응답 — 200

```json
{ "success": true, "data": { "cartItemId": 55 } }
```

`cartItemId`는 SSE `action`(`CART_ADDED`)에 사용한다(§3.1).

#### 실패 응답 — I-2 오류 코드와 AI 동작 매핑

| HTTP | I-2 `code` | 조건 | AI 동작 |
|---|---|---|---|
| 400 | `CART_OPTION_REQUIRED` | 옵션 필수인데 `optionId` 없음 — **`error.detail.options`에 `[{optionId, name, extraPrice}]` 포함**(BE 확정 2026-07-18) | **되물음 멀티턴**: 실패 `action` 없이 `token`으로 "어떤 색상으로 담을까요?" 재질문 → 다음 턴에서 사용자 답을 `optionId`로 해석해 재담기. **단 `options`가 1개면 되묻지 않고 그 `optionId`로 같은 턴에 I-2를 1회 재호출**(자동 선택, v0.17.2 #114) — 선택지가 하나면 되물어도 답이 정해져 있다. **BE 관측 포인트**: 이 경우 400 직후 같은 요청이 `optionId`만 채워져 한 번 더 온다. 재호출도 REQUIRED면 자동 재시도 없이 되물음으로 돌아간다(상세: `docs/specs/SPEC-CART-001.md` REQ-CART-026·027) |
| 400 | `CART_OPTION_INVALID` | 옵션이 해당 상품 소속 아님 | AI가 `optionId` 해석 오류 — options 목록 재확인 후 **되물음 재시도**(1회), 반복 실패 시 `action` `CART_ERROR` |
| 404 | `PRODUCT_NOT_FOUND` | 없는 상품 | `action` `CART_ADD_FAILED` + `reason: "PRODUCT_NOT_FOUND"` |
| 400 | `VALIDATION_ERROR` | 합산 수량 > 99(수량 상한, **재고검사보다 먼저** 걸림) | `action` `CART_ADD_FAILED` + `reason: "CART_ERROR"` + message "수량은 최대 99개까지 담을 수 있습니다."(BE 문구와 동일; 99=BE `CartItem.MAX_QUANTITY`) |
| 400 | `CART_STOCK_INSUFFICIENT` | 합산 수량 > 재고(재고는 상품 단위) — **`error.detail.availableStock`에 남은 재고 수 포함**(2026-07-22 신설) | `action` `CART_ADD_FAILED` + `reason: "STOCK_INSUFFICIENT"` + message에 남은 재고 수 노출("재고가 N개뿐이에요"; **재고 0=품절이면 "품절된 상품이에요"**, `availableStock` 미상 시 일반 안내) |
| 401 | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 없음/불일치 | 운영 오류 — 사용자에게는 `action` `CART_ERROR`로 안내, 서버 로그/알림 |

> **잔여 협의(C-3)** — 대부분 해소, 서비스 토큰만 남음: (1) ~~재고 오류~~ **해소(v0.15.16)** — 담기 재고검증 **있음**: BE `400 CART_STOCK_INSUFFICIENT` + `availableStock`(2026-07-22) → `reason STOCK_INSUFFICIENT`(구 `OUT_OF_STOCK` 폐기 유지, 품절≠재고부족). (2) ~~`CART_OPTION_REQUIRED` options 스키마~~ **해소(BE 2026-07-18)** — `error.detail.options: [{optionId, name, extraPrice}]`. (3) ~~`productId` 타입~~ **해소(v0.15.3)** — 숫자 BIGINT(§2.6). (4) 🔴 **서비스 토큰(`X-Internal-Token`) 발급·교환 방식** — 유일 잔여.

### 4.2 추천 목록 전달 API (I-21 `POST /internal/recommendations`) — [BE DB 등재 v0.15.0, reasons 확정 v0.15.15 🟢, **다중 목록 확정 v0.17.1** 🟢]

rerank 완료 후 AI가 **최종 랭크 상품 id 목록만** Spring에 POST한다. Spring이 Redis에 TTL 저장하고 표시 필드를 enrich하며, FE가 **CH-5**(§4.3)로 카드를 조회한다. **[07/17 BE 신설]** 합의된 추천 흐름 6번("FastAPI가 최종 추천 상품 ID만 Spring에 전달")의 실제 API.

> **[v0.17.1] 한 번의 추천이 목록을 여러 개 낼 수 있다** — 니즈별 추천("유럽여행 필요한 거" → 파우치·어댑터 각각의 후보)과 세트 여러 안("감자탕 재료" → 조합 A·B·C)은 목록 하나로 표현되지 않는다. 요청 최상위는 **`lists[]` 배열**이며, 목록이 1개여도 길이 1 배열로 보낸다. 구 평평한 3필드(`listId`·`productIds`·`reasons`)는 **폐기**한다.

#### AI → Spring 요청 (I-21)

```
POST {SPRING_BASE_URL}/internal/recommendations
X-Internal-Token: {서비스 토큰}   ← internal 그룹, 3s
```

**단일 목록** (일반 추천 — 목록 안 상품들이 서로 대안):

```json
{
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
  "listType": "PICK_ONE",
  "lists": [
    {
      "listId": "3f9a2c1e7b8d4e5fa0c6d1e97b3f8a24",
      "productIds": [101, 205, 552, 88, 13],
      "reasons": [
        { "productId": 101, "reason": "방수 등급이 높아 우천 시에도 안전합니다." },
        { "productId": 205, "reason": "가벼워 휴대가 편합니다." }
      ]
    }
  ]
}
```

**여러 목록** (총액 예산 세트 — 목록 하나가 한 세트):

```json
{
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
  "listType": "BUY_ALL",
  "totalBudget": 50000,
  "lists": [
    { "listId": "9f2c1a7e4b8d43f5a0c6e1d97b3f8a24", "label": "알뜰", "productIds": [101, 205, 552], "reasons": [] },
    { "listId": "4b8d43f5a0c6e1d97b3f8a249f2c1a7e", "label": "균형", "productIds": [101, 88],       "reasons": [] }
  ]
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string(UUID) | 예 | 상관관계 키(`products.ready`와 상관). UUID 형식이 아니면 400 — **값의 생존 여부는 검증하지 않는다** |
| `recommendationRequestId` | string(≤36자) | 예 | **[v0.17.1 신설] 추천 실행 1회**를 가리키는 opaque id(UUID/ULID). **FastAPI가 생성**. 이후 노출·클릭·담기·주문을 이 추천에 귀속시키는 조인 키다. `listId`(사용자에게 전달된 목록)와 **역할이 달라 서로 대체하지 않는다**. 36자 초과 시 400(BE `CHAR(36)`) |
| `listType` | enum | 예 | **[v0.17.1 신설] 목록 안의 상품들이 서로 대체재인지 보완재인지.** `PICK_ONE`(서로 대안 — 그중 **하나만** 산다) / `BUY_ALL`(각자 다른 역할 — **전부** 산다). **항상 싣는다** |
| `totalBudget` | int | 아니오 | **[v0.17.1 신설] `BUY_ALL` + 예산 발화 시에만.** 사용자가 말한 예산 상한 — *"5만원 내로"* 에서 AI가 뽑아낸 `50000` |
| `lists` | array | 예 | 목록 배열. **1~10개** (0개·10개 초과 = 400). 목록이 1개여도 배열이다 |
| `lists[].listId` | string(≤64자) | 예 | **[HARD] FastAPI가 생성하는 UUID급 무작위 식별자(≥128bit)** — 순번·타임스탬프 등 추측 가능한 형식 금지. 허용 문자는 **영숫자·`-`·`_`** 이며 벗어나거나 64자를 넘으면 400(Redis 키 오염 방지). 현재 구현은 `uuid4().hex` 32자리 lowercase hex |
| `lists[].label` | string(≤50자) | 아니오 | **[v0.17.1 신설] 목록 이름.** `BUY_ALL`이면 세트 성격("알뜰"·"균형"), `PICK_ONE`이면 니즈 이름("파우치"·"어댑터"). 50자 초과 시 400(BE `VARCHAR(50)`) |
| `lists[].productIds` | number[] | 예 | 최종 랭크 상품 id. **순서 유지 = 렌더 순서**(리랭킹 순서). 숫자 id(§2.6 internal). **목록당 최대 9개**(2026-07-30 확정 — 구 Top5에서 상향), 비었거나 9개 초과면 400 |
| `lists[].reasons` | array | 아니오 | **[확정 v0.15.15, BE 구현 2026-07-18] 상품별 추천 근거** `{productId(숫자), reason}` — productId로 키잉(순서 권위는 `productIds`, 부분집합/순서무관). Spring이 저장 → **CH-5 카드에 `reason` echo**(§4.3). 선택 필드 — 근거 없는 상품은 생략(🟢). 배열 **최대 9개**, `reason` **최대 200자**. **생성 목표 = 한글 ≤40자 1문장**(rerank 프롬프트). AI가 push 전 개행 제거·안전 상한(config `reason_max_len`) 방어 정제하고, **표시 오버플로(줄임/더보기)는 FE 소관**(경로 B, 표시 권위=FE) |

##### `listType` — 세 모양이 이 한 필드로 표현된다

| `listType` | `lists` 길이 | 의미 |
|---|---|---|
| `PICK_ONE` | 1 | **일반 추천** — 후보 중 하나를 고른다 |
| `PICK_ONE` | N | **니즈별 추천** — "유럽여행 필요한 거" → 파우치 후보 / 어댑터 후보 |
| `BUY_ALL` | N | **세트 여러 안** — "감자탕 재료" → 조합 A·B·C |

- **판단 기준은 예산이 아니다** — "감자탕 재료"는 예산이 없어도 `BUY_ALL`이고, "5만원으로 파우치"는 예산이 있어도 `PICK_ONE`이다.
- **목록 개수는 싣지 않는다**(`lists` 길이로 알 수 있음). 반면 `listType`은 개수로 알 수 없어 **서버가 말해줘야 한다**.

#### 성공 응답 — 200

```json
{ "success": true }
```

#### 규약

- **멱등 키는 (`recommendationRequestId`, `listId`) 쌍이다.** 같은 쌍의 재전송은 목록을 중복 저장하지 않고 200으로 응답한다(타임아웃 후 재시도 대비). **`recommendationRequestId` 단독을 키로 쓰면 안 된다** — 여러 묶음 요청에서는 한 실행에 목록이 여러 개 오므로 두 번째 이후 목록이 "중복"으로 잘못 버려진다. **한 콜백 안에 같은 `listId`가 두 번 오면 400**(뒤 목록이 재전송으로 오해돼 조용히 버려지므로 거절한다).
- **[변경 07/17] payload = id 배열만** — 구 §4.2 `groups[{title,category,items[{productId,rank,reason}]}]` 구조는 **폐기**. 표시 필드·순위·카드 제목은 콜백에 싣지 않는다. **[v0.17.1] `listType`·`label`·`totalBudget`은 표시 필드가 아니라 목록의 성격 메타**이며, 표시 권위는 그대로 Spring에 있다(경로 B 유지) — `products.ready`는 이 값들을 싣지 않고 CH-5 응답이 나른다(§3.1).
- **`listId`는 FastAPI가 UUID급 무작위(≥128bit)로 생성한다.** 구 "Spring이 listId 발급" 가정과 `list-4471` 같은 순번형 예시는 폐기한다. CH-5가 인증 불필요 공개 조회라 `listId`가 사실상 bearer 키이므로 **순번·타임스탬프 등 추측 가능한 형식은 금지**한다. 여기서 `≥128bit`는 **식별자 표현 폭** 기준이며, 현재 UUIDv4 구현은 128bit UUID 중 version·variant 고정 비트를 제외한 122bit를 무작위로 생성한다.
- **`listId` TTL = 10분(config).** **세션이 sliding으로 연장돼도 목록 TTL은 생성 시점 고정**이다 — 대화가 이어지는 중에도 오래된 카드는 만료된다. 만료 시 CH-5는 404이며, FE는 오류 화면 대신 자기가 가진 카드 스냅샷으로 폴백한다.
- **[확정 v0.15.15] `reason`은 이 콜백에 포함**(🟢, BE 구현 2026-07-18) — `reasons[{productId, reason}]`를 Spring이 저장 → **CH-5 카드에 echo**(§4.3)해 FE에 전달. 구 BE 07/17 제안(reason=SSE·콜백 불포함)은 폐기 — SSE(`products.ready`)는 상관키만 유지, 경로 B 일관·FE join 불필요. 추천 이유는 **이원화**(2026-07-18 합의): SSE = 채팅 말풍선용(Spring 무관), 콜백 `reasons` = 우측 추천 카드용(CH-5 echo).
- **규약**: FastAPI는 이 콜백이 **성공한 뒤에만** SSE `products.ready`({sessionId, listIds})를 발행한다 — 콜백 실패 시 미발행(FE가 빈 목록 조회 방지, §3.3). `listIds`의 **순서·개수는 `lists`와 같다**(§3.1).
- **`recommendation_generated`는 Spring이 server-side로 기록한다** — 이 콜백이 성공한 시점에 `behavior_events`에 직접 적재하며, **FastAPI는 E-1(`POST /api/events`)로 같은 이벤트를 보내지 않는다.** E-1은 인증이 없어 분모가 조작 가능해지고, 양쪽이 다 기록하면 이중 계상된다.
- **인증**: `X-Internal-Token` 서비스 토큰.

#### 실패 응답

| HTTP | code | 조건 |
|---|---|---|
| 401 | `INTERNAL_TOKEN_INVALID` | `X-Internal-Token` 없음·불일치, 또는 **서버에 토큰 미설정(fail-closed)** |
| 400 | `VALIDATION_ERROR` (`error.fields[]` 포함) | 필수 필드 누락(`sessionId`·`listId`·`productIds`) |
| 400 | `VALIDATION_ERROR` | `sessionId`가 UUID 형식이 아님 · `recommendationRequestId` 36자 초과 |
| 400 | `VALIDATION_ERROR` | `lists`가 비었거나 **10개 초과** — 목록 10 × 상품 9 = 90개고 FE는 CH-5를 10번 호출해야 한다. 상한이 없으면 버그 한 번에 Redis 키·DB 행이 수백 개 생긴다 |
| 400 | `VALIDATION_ERROR` | `listId`가 허용 문자(영숫자·`-`·`_`)를 벗어나거나 64자 초과 · 한 콜백 안 `listId` 중복 |
| 400 | `VALIDATION_ERROR` | `productIds`가 비었거나 9개 초과 · 숫자 배열이 아님 · 한 목록 안 `productId` 중복 · `label` 50자 초과 |
| 500 | `INTERNAL_ERROR` | Redis 쓰기·직렬화 실패 — **폴백이 없다.** `products.ready`를 발행하면 안 되고, 재시도하거나 카드 없이 텍스트만 응답한다 |

**빈 목록은 400이 아니라 "보내지 않는 것"이 맞다** — 빈 목록을 저장하면 FE가 빈 카드 패널을 받는다. 후보가 없으면 콜백 자체를 생략하며, 그러면 `products.ready`도 발행되지 않는다.

##### 실패가 아닌 것 (200)

| 상황 | 이유 |
|---|---|
| 같은 (`recommendationRequestId`, `listId`) 재전송 | **멱등**이다. 타임아웃 후 재시도가 안전하도록 중복 저장 없이 200 |
| 존재하지 않거나 만료된 `sessionId` | 세션 생존을 검증하지 않는다 — 목록 TTL은 세션과 무관하게 생성 시점부터 10분. 다만 신원을 구할 수 없어 **익명 저장**(2026-07-28 확정)되고, 그 목록은 **CH-5에서 조회되지 않는다**(소유자 미기록 = fail-closed) |
| `productIds`에 `HIDDEN`·품절 상품 포함 | 저장 시점엔 거르지 않는다. **드롭은 조회(CH-5) 시점**이며 `itemsDropped`로 알린다 |
| `reasons` 생략·일부만 있음 | 선택 필드다. 없는 상품의 카드는 `reason: null`로 내려간다 |
| `reasons`에 `productIds`에 없는 상품이 섞임 | 매칭되지 않는 이유는 쓰이지 않는다 — 400으로 만들지 않는다 |

- 🔴 협의(C-9): 잔여 없음. (`listId` 형식은 v0.16.1에서 UUID급 무작위 ≥128bit로, `reason` 전달 방식은 v0.15.15에서 콜백 포함으로, **TTL 10분·다중 목록은 v0.17.1에서 확정** 🟢.)

### 4.3 추천 목록/카드 조회 (CH-5 `GET /api/chat/lists/{listId}`, FE ↔ Spring 전제 계약) — [BE DB 등재 v0.15.0, 스키마 OPEN]

FE가 `products.ready` 수신 후 Spring에서 **표시 필드가 채워진** 추천 카드를 조회한다. **[07/17 BE 신설] 구 P-7을 대체**하는 CH-5. **이 계약은 FE ↔ Spring 간이며 AI 서버는 관여하지 않는다**(레인 d). AI는 I-21(§4.2)로 **id만** 넘기고, 카드 표시 필드는 Spring이 채운다.

#### FE → Spring 요청 (CH-5)

```
GET {SPRING_BASE_URL}/api/chat/lists/{listId}   ← BE DB(CH-5). listId = I-21에서 FastAPI가 넘긴 값
```

- Spring이 I-21로 받은 `productIds`를 자기 DB에서 enrich(name·price·image·reviewCount 등)해 **순서 유지 카드**로 반환.
- **표시 권위 = Spring**: `price`·`originalPrice`·`imageUrl`·`reviewCount`·`availability`를 Spring이 채운다(결정 9-B).
- **카드 응답 스키마는 FE ↔ Spring이 확정**(LLM 사안 아님). **[확정 v0.15.15] 카드 항목에 `reason` 포함** — Spring이 I-21에서 받은 값을 echo(§4.2)해 FE는 카드+이유를 한 번에 받는다(BE 구현 07-18). 나머지 카드 표시 필드 스키마는 OPEN 🔴(§5 C-12).

### 4.4 판매자 집계 조회 API (I-6, query-time) — 🔴 제안(초안) [v0.4.0 신설, Batch 1]

판매자 통계 답변의 원천. AI가 질의 시점에 이 API를 호출해 **집계값만** 받는다(원시 로그 미제공). C-7 해소의 핵심 계약이다.

#### AI → Spring 요청 (제안)

**[개정 v0.8.0]** BE 문서 기준으로 판매자 집계는 **단일 API가 아니라 `brandId` 스코프 집계 5종**이다(전부 `internal`·`X-Internal-Token`·3s). `brandId`는 검증된 판매자 JWT 클레임에서 얻는다(§2.3·§2.6). **전체 5종 반영·I-number 정합은 #9로 진행** — 아래는 대표(매출 시계열).

```
GET {SPRING_BASE_URL}/internal/seller/{brandId}/sales?from={d}&to={d}&granularity={g}
X-Internal-Token: {서비스 토큰}
```

**판매자 조회/집계 7종 (BE 실제 No., 전부 GET·internal·`X-Internal-Token`)**:

| BE No. | 경로 | 내용 · 쿼리 | 소비 서브에이전트 |
|---|---|---|---|
| I-6 | `/internal/seller/{brandId}/sales` | 매출 시계열 · `from`/`to`(필수)·`granularity`(daily/weekly/monthly/summary). `series[{date,sales,orderCount,isAnomaly,deviationPct}]` | sales_anomaly·conversion·general·recommend·chart |
| I-7 | `/internal/seller/{brandId}/funnel` | 구매전환 퍼널 4단(view→cart→checkout→purchase) · `from`/`to` | conversion·behavior·chart |
| I-13 | `/internal/seller/{brandId}/events` | 행동 이벤트 집계(`behavior_events`) · `from`/`to`·`eventType`(product_view/add_to_cart/checkout_start/purchase_complete, **CSV 직렬화** — BE는 `String eventType` + comma split, 반복 쿼리 아님 v0.17.4)·`productId`·`groupBy`(product/eventType/date). `rows[{productId,counts{4종},viewToCartRate,uniqueVisitors}]` — **rows 정렬 = 활동량(counts 4종 합) 내림차순, 동률 시 productId 오름차순**(BE `eventsByProduct` 실측, v0.17.4). ⚠️ purchaseComplete 는 FE 미귀속(product_id NULL)으로 **상품별·합계 0 집계 가능** — 구매 권위는 I-6/I-7/I-14(근본 수정 jarvis-backend#62 대기, v0.17.4) — **LLM팀 본문 재작성 반영(v0.15.1)** | behavior·conversion |
| I-16 | `/internal/seller/{brandId}/churn` | 이탈 코호트 · `from`/`to`(**필수** — 누락·형식 오류·역전 400 INVALID_PERIOD)·`inactiveDays`(기본 30). 코호트 = from~to 에 자사 상품과 상호작용(`behavior_events`)한 회원, 이탈 = 그중 최근 inactiveDays 무활동. **응답(BE 실측 확정 v0.19.1, SellerChurnResponse)**: `cohortSize`·`churnRate`(**소수 fraction, 0.6=60% — % 변환은 AI 표시 계층**)·`preChurnSignals{cancelCount,returnReasonsTop[{reason,count}],zeroResultSearchSessions(상시 0 — E-1 FE 결과 수 미적재),priceIncreaseExposed(명수)}`·`members[{memberId,lastActivityAt,lastLoginAt,sessions30d,preChurnEvent}]`(**서버 상한 50 절단**, 별도 total 없음). 코호트 0명 시 `cohortSize` 0·`churnRate` 0.0(이탈률 0%와 구분 서술 — AI 소관) | churn |
| I-14 | `/internal/seller/{brandId}/order-events` | 주문 상태 전이/조회(`order_status_logs`) · `toStatus`(8종 복수)·`actorType`·`from`/`to`·`stats`·`groupBy`·`limit`(기본 100). **응답(BE 실측 확정 v0.16.2, shape 상호 배제)**: 목록 = `rows[{orderId,fromStatus,toStatus,actorType,reason,buyerMemberId,createdAt}]`+`total` / `stats=true` = `byStatus`+`cancelReasonsTop[{reason,count}]` / `groupBy=memberId` = `rows[{buyerMemberId,orderCount,cancelCount,cancelRatio,maxOrdersPerHour,isSuspicious}]`+`total` | sales_anomaly·conversion·churn·abuse·general |
| I-15 | `/internal/seller/{brandId}/product-changes` | 상품 변경 이력(`product_change_logs`) · `changeType`(PRICE/STOCK/STATUS)·`productId`·`from`/`to`·`limit`(기본 100). **응답(BE 실측 확정 v0.16.2)**: `rows[{productId,productName,changeType,oldValue,newValue,createdAt}]`+`total` — `oldValue`/`newValue`는 문자열(숫자도 문자열, 품절 신호 = STOCK `newValue` "0") | sales_anomaly·churn·recommend |
| I-8 | `/internal/account-events` | 계정/보안 이벤트 집계 **(전역·브랜드 스코프 아님, admin 소유 🔴 — 협의 완료 전 AI 판매자 표면 노출 보류: AI 설정 `seller_account_events_enabled` 기본 false, v0.19.1)** · `eventType`·`from`/`to`(**필수** — 누락 400 INVALID_PERIOD)·`groupBy`(`eventType` 기본/`hour`/`ip` — 그 외 400 INVALID_GROUP_BY). **응답(BE 실측, AccountEventAggregateResponse)**: `groupBy` 에코 + `rows[]` — eventType\|hour = `{key,count}` / ip = `{ipMasked,failCount,distinctMembers,nullMemberRatio,isSuspicious,firstSeen,lastSeen}`(IP 마스킹·집계 전용, raw 미반환) | abuse·churn |

- **`brandId` = JWT 클레임** — AI는 사용자 입력이 아니라 검증 토큰에서 얻어 `{brandId}` path에 쓴다(IDOR 방지, §2.6). 전역 I-8은 brandId 무관.
- **집계/이력값만** 반환(원시 로그 아님). AI는 LLM으로 자연어 답변.
- ⚠️ **혼동 주의**: BE I-15 `product-changes`(판매자 감사 로그)는 C-4 `products/changes`(AI 생성물 배치 pull, §4.8)와 **다르다**. BE I-14 `order-events`(판매자 주문 이벤트)는 C-6 `orders/recent`(구매자 이력, §4.7)와 **다르다**.
- **[v0.16.2] I-14/I-15 응답 스키마는 BE 실측(SellerOrderEventsResponse/SellerProductChangesResponse)으로 확정** — 잔여 🔴 는 전역 I-8 소유(admin)·I-number 정합(§5 C-13, #9). I-6 이상 감지 로직도 BE 실측 확정: 직전 최소 3·최대 7포인트 평균 대비 ±30%, 기준선 0+매출 발생 = 이상(`deviationPct` null), 매출 0 포인트는 이상 아님.

### 4.5 판매자 상품 CRUD API (I-9/I-10/I-11/I-12) — [개정 v0.9.0, BE 문서 채택]

**[개정]** 구 §4.5(I-7 상세 읽기 + FE S-3 PATCH 반영)를 폐기하고, BE 판매자 상품 관리 4종을 채택한다. **AI(`product_agent`)가 Spring internal API로 읽기·쓰기를 직접 수행**하며, 쓰기는 §3.2 HITL 승인 게이트를 거친다. 전부 `internal`·`X-Internal-Token`·`{brandId}`(JWT 클레임)·3s.

| BE No. | Method · 경로 | 용도 | 비고 |
|---|---|---|---|
| I-9 | GET `/internal/seller/{brandId}/products` | 자사 상품 목록 조회 · `status`(ON_SALE/HIDDEN)·`q`·`limit`/`offset` | draft의 `before` 소스(구 I-7 상세 읽기 대체). `rows[{productId,name,price,originalPrice,stockQuantity,status,displayedSalesCount,category,description,imageUrl}]` |
| I-10 | POST `/internal/seller/{brandId}/products` | 상품 등록 · Body `name`·`price`(≤`originalPrice`)·`stockQuantity`(≥0) 필수 | 201 `{productId,status:"ON_SALE"}`. 신규 등록은 변경 이력 미기록 |
| I-11 | PATCH `/internal/seller/{brandId}/products/{productId}` | 상품 수정(가격·설명·상태·재고 통합) · Body 바꿀 필드만 | 재고도 이 API(별도 재고 API 없음). 변경 시 `product_change_logs`(PRICE/STOCK/STATUS) |
| I-12 | DELETE `/internal/seller/{brandId}/products/{productId}` | 상품 삭제(soft) · Body 없음 | **HITL 승인 후에만 실행**. 물리 삭제 없음 — `status=HIDDEN` 전환. 200 `{productId,status:"HIDDEN"}` |

- **쓰기는 `product_agent` 전용** — 쓰기 도구는 이 서브에이전트에만 배정(다른 서브에이전트는 읽기만).
- **소유권 검증은 Spring**: `brandId`(JWT 클레임 유래)로 판매자가 자기 상품만 다루도록 검증. AI는 신원을 요청 본문에서 받지 않는다.
- **status는 `ON_SALE`|`HIDDEN` 2종** (물리 삭제 없음).
- 정확한 응답 스키마·`categoryId`/`attributes` 스키마·HITL 이벤트 계약은 🔴 협의(§5 C-14, #9). "DB 논의 필요"(삭제 PDF)는 BE 내부 사안.

### 4.6 후보 검색 위임 API (I-1 `GET /internal/products/search`, query-time) — [BE 실측 정합 v0.13.0, 착수 전 최우선]

**[v0.5.0 — 가장 중요한 신규 Spring 계약]** 구매자 추천 후보를 **질의 시점에 Spring에 위임 검색**한다. AI는 사용자 원문을 decompose하여 구조화 필터를 만들고, 이 API로 Spring 카탈로그를 검색해 rerank·예산 검증에 필요한 후보를 돌려받는다. **검색 품질이 곧 추천 품질을 좌우**하므로 착수 전 최우선 협의 대상(C-15)이다. AI 카탈로그 사본·벡터 인덱스가 없으므로 **이 API가 유일·영구 후보 확보 경로**다.

#### AI → Spring 요청 (제안)

```
GET {SPRING_BASE_URL}/internal/products/search   ← BE 실측(I-1). 인증 서비스 토큰
X-Internal-Token: {서비스 토큰}
```

> **[확정 v0.15.5] I-1 = GET 그대로 수용**(사용자 지시 2026-07-19). 구 POST 역제안 폐기. **BE Notion I-1 파라미터를 기준으로 채택**(판정규칙: API 표면=Notion, 타입=DDL). Query string 스칼라 파라미터. 신원(userId/guestId)은 서비스 토큰 레인이라 AI가 도출해 쿼리로 전달(§2.6).

```
GET {SPRING_BASE_URL}/internal/products/search?keyword=방수파우치&categoryName=여행용품&maxPrice=50000&brandName=샘소나이트&brandName=레스포삭
X-Internal-Token: {서비스 토큰}
```

| 요청 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `keyword` | string \| null | 아니오 | 상품명+summary+attributes LIKE(BE I-1). FULLTEXT 없음·LIKE 2단(DDL D7) |
| `categoryName` | string \| null | 아니오 | **[2026-08-03 정본 개정] 잎(말단) 카테고리 이름 정확 일치.** LLM은 잎 이름을 그대로 보내며 서버는 상위 개념을 해석하지 않는다 — 아래 노트 참조 |
| `minPrice` / `maxPrice` | int \| null | 아니오 | 가격 필터. 질의 시점이라 항상 최신(freshness) |
| `brandName` | string[] \| null | 아니오 | **다중 브랜드**(#100 P1, 방법 D). 반복 파라미터(`brandName=A&brandName=B`)로 전량 전송 → BE `WHERE brand IN (...)` OR 매칭. 단일은 값 1개(기존과 동일), 미전송 시 브랜드 필터 없음. **BE 배열 수용 협의 대상** |
| `color` | string[] \| null | 아니오 | **다중 색상**(#258, 사본 drift 정정). 반복 파라미터(`color=네이비&color=남색`)로 전량 전송 → BE 부분 일치 OR 매칭. 단일은 값 1개(기존과 동일), 미전송 시 색상 필터 없음. 아래 노트 참조 |
| ~~`size`~~ | — | — | **[2026-07-23 개정, BE 합의] 제거됨** — 아래 노트 참조 |

- **[2026-07-23, BE 합의] `size` 제거 — 라운드1 전량 반환**: I-1(라운드1)은 고정필터(category·price·brand) 매칭 상품을 **전량 반환**한다(반환 상한 없음). 결과 수 제한(top-K)은 **AI 쪽**에서 적용한다 — 후보를 받아 pgvector 임베딩 유사도로 재정렬 후 `limit`(config, AI 후보 상한)만큼 압축해 rerank 입력을 만든다(§4.8 방식2). 즉 "몇 개로 줄일지"는 Spring 요청 파라미터가 아니라 AI 파이프라인 소관이다.
- **[2026-08-03 정본 개정] `categoryName`은 잎 이름 정확 일치**: 카테고리는 **도메인(뿌리) + 잎 2단**이고, "대분류"는 별도 행이 아니라 `대분류 > 중분류` 형태인 **잎 이름의 접두사**로만 존재한다. ✅ `categoryName=거실가구 > 소파`는 그 잎의 상품만 조회하지만, ❌ `categoryName=거실가구`는 해당 행이 없어 **0건**이다. 상위 개념 검색은 LLM이 잎으로 펼쳐 각각 호출한다. `parent_id IS NULL`인 도메인명(뷰티·식품 등)은 서버가 하위 잎 전체로 확장해 동작하지만 **표준 경로가 아니다**. 구 "대분류명이면 하위 소분류 전체 포함·LLM은 대분류명이 기본" 문구는 대분류 행이 없어 성립하지 않으므로 폐기한다.
- **[#258, 사본 drift 정정 — 신설 협의 아님] `color` 는 다중값(`string[]`), 반복 파라미터로 BE 부분 일치 OR 매칭**: `brandName` 방법 D(위)와 동일 규약이다. BE 는 2026-08-03 LLM팀 실측 합의로 계약을 개정하고 2026-08-04 배포(`InternalProductController.search` 시그니처가 이미 `List<String> color`)를 마쳤는데 본 사본만 단수 `string` 으로 남아 있던 drift였다(2026-08-08 정정, 운영 배포 완료 확인). BE 는 값들을 **정규식 alternation 하나로 합쳐 `regexp_instr`** 로 본다(JPQL 이 리스트 동적 OR 을 표현하지 못해서) — 의미는 OR. **3갈래 판정**: ① 미지정/공백뿐이면 조건 없음 ② `attributes` 에 색상 축이 없으면 통과(색상 미상이 카탈로그의 **34%(2,445건)** 라 거르면 그 상품들이 전멸한다) ③ 있으면 `json_extract` 로 색상 값만 좁혀 비교. **부분 일치를 유지한다** — `그레이` 가 `다크그레이` 를 잡는다(BE 실측 75건). 구 구현은 `attributes` JSON 전문 LIKE 라 **화이트 정밀도 37.9%** 였고 `_extra.visual_features` 설명문이 오탐 출처였다. **정규화 주체는 BE** — 입력값·저장값 양쪽을 trim + 소문자화해 비교하며, **정규식 메타문자도 BE 가 이스케이프**한다(패턴 주입 차단). AI 는 카탈로그 실재 표기를 원문 그대로 보낸다. **개수 상한 없음.** 동의어 확장 주체는 **AI**(#258, `app.pipelines.color_synonyms.expand_color`) — BE 는 받은 값을 OR 로 볼 뿐 스키마 변경이 없다. 근거: BE `docs/backend/05-llm-contract.md` §I-1("`color?` 리스트 — 2026-08-03 개정", "동의어 확장은 LLM 팀 소관이고 BE는 받은 값들을 OR로 본다"), 머지 커밋 `1e0ce150`(2026-08-04, "I-1 색상 필터를 3갈래로 + options 응답 추가").
- **[해소 v0.15.5, C-15] dedup·평점·정렬 = AI 사후필터(post-filter)**: BE I-1엔 `excludeProductIds`·`ratingMin`·`sort` 파라미터가 **없다**. 따라서 정확 제외 dedup(결정 14-F)은 **응답 수신 후 AI가 최근 구매 productId(I-19) 집합으로 제외**하고, 평점 필터·정렬도 rerank 단계에서 AI가 처리한다. 구 "요청 파라미터 제외" 기본안 폐기.

#### AI가 받는 응답 (BE Notion I-1 기준, 타입=DDL)

```json
{
  "success": true,
  "data": [
    {
      "productId": 1,
      "name": "린넨 셔츠",
      "summary": "시원한 여름 린넨 셔츠",
      "attributes": { "소재": "린넨", "핏": "오버핏" },
      "categoryName": "여성의류",
      "brandName": "더센트",
      "price": 29900,
      "rating": 4.8,
      "reviewCount": 128,
      "options": ["화이트/M", "화이트/L", "블랙/M"],
      "optionCount": 3
    }
  ]
}
```

| 응답 필드 | 타입 | 설명 |
|---|---|---|
| `data[]` | array | 후보 배열(rerank 입력). envelope = `{success, data:[...]}`(BE 실측, ApiResponse<List>) — 파서는 구 `data:{items:[...]}` 도 호환 수용 |
| `[].productId` | number | 후보 식별자(숫자 BIGINT, DDL) |
| `[].name` | string | 상품명(rerank·근거 생성용) |
| `[].summary` | string \| null | 요약(#100 P0 — rerank/세부조건용, 소비는 #101 2차 압축) |
| `[].attributes` | object \| null | **[해소 v0.15.5, C-5] 축 = `category.attribute_schema`(키 배열, 예 `["소재","핏"]`), 값 자유텍스트**(DDL D7·D11) — 2차 압축 속성 매칭 대상 |
| `[].categoryName` / `brandName` | string | rerank 신호·필터 검증용 |
| `[].price` | int | 질의 시점 판매가 — **AI 계산용(비표시, #100 P1)**: 예산 검증(`verifiedSum`, §3.1 budget)·`maxPrice` 판정·rerank 신호. 표시가는 CH-5(§4.3) |
| `[].rating` | number | **조회 시 집계**(저장 avg 없음, DDL D9) — **AI 계산용(비표시, #100 P0)**: 평점 사후필터·rerank 신호. 표시는 CH-5 |
| `[].reviewCount` | int | **조회 시 집계**(리뷰 개수) — **AI 계산용(비표시, #171)**: `rating` 과 짝지어 **"리뷰가 없어 rating=0"(reviewCount=0, 데이터 부재)** 와 **"리뷰가 있고 하한 미달"(reviewCount>0)** 를 가른다. `null`/미전송이면 rating 이 지배(구 동작 폴백). 표시용 리뷰수는 여전히 경로 B(§4.3) |
| `[].options` | string[] \| null | **선택. Spring 송신 계약은 옵션 이름 최대 20개**다. 미전송 허용이며 이 계약에서는 수신만 하고 rerank·추천 문구·옵션 되물음 단축에 소비하지 않는다. AI 수신부는 초과분이 와도 미소비 메타데이터 때문에 상품 전체를 제거하지 않도록 관대하게 원본 배열을 수신한다 |
| `[].optionCount` | int \| null | **선택. 절단 전 전체 옵션 개수**(0 이상). `options`가 20개로 절단돼도 전체 개수를 나타내며, 두 필드 길이 일치는 요구하지 않는다 |

- **[#100 결정, #171·#278 부분 개정] 표시 전용 필드 중 `imageUrl`·`originalPrice`는 I-1 미반환 → CH-5(§4.3) 하이드레이션**: 두 필드는 카드 표시 몫이라 I-1이 반환하지 않는다(BE 2026-07-18 재설계). **[#171] `reviewCount`는 AI 계산용(비표시)으로 반환**해 rating=0의 의미(리뷰 부재 vs 저평점)를 판별한다. **[#278] `options`·`optionCount`도 추가 전용 선택 필드로 반환 가능**하되 현재 AI는 관대 수신만 하고 소비하지 않으며, 옵션 되물음은 계속 I-2(§4.1) 소관이다.
- **[#100 P0/P1, #171] `price`·`rating`·`reviewCount` = 계산용(비표시) 반환**: display 로 오분류해 CH-5로만 넘기면 안 된다 — rerank·예산검증·평점필터는 CH-5(표시 시점)보다 앞선 질의 시점에 이 값이 필요하다(후보와 함께 받아야 함). `reviewCount`는 `rating` 판별자로 함께 받는다(#171). BE 협의 완료(2026-07-28), 반환 구현 대기.
- **[주의] BE I-1 응답에 `stock`·`totalCount` 없음**: (1) 재고는 후보에 안 실림 → 예산검증은 `price`만, 재고/품절 판정은 담기·주문 시점(§4.1). (2) `totalCount`는 **[#100 P2 결정] 별도 필드 불필요** — `size` 제거(전량 반환)로 AI `search_catalog`가 **top-K 절단 전에** 사후필터 통과 매칭 수를 `total_count`로 확정하므로(절단값 min(매칭, limit)이 아님) 현재 필터의 매칭 수를 안다. 완화 칩 `estCount`(§3.1 suggestions)는 '완화된 다른 필터'의 count 라 이 값으로 못 구하고(완화 칩 미구현·별도 이슈 — 재쿼리/BE count 필요), 되돌리기 칩은 **top-K 절단된 응답 후보 내** 억제 수라 page-local 근사다(전량 기준 진짜 억제 수보다 작을 수 있음).
- **freshness(신선도) — 트레이드오프 없음**: 질의 시점 검색이므로 **가격·재고 필터는 항상 최신**이다. v0.4.0 미러 배치의 "필터 경계 오류(stale price로 인한 오포함/오제외)" 트레이드오프는 **본 구조에서 소멸**한다. (표시가는 여전히 경로 B로 Spring이 목록 GET에서 채운다, §4.3.)
- **rerank는 AI-side**: 이 API는 후보만 반환하고, profile_summary 반영 rerank·근거 생성은 AI 경계에서 수행한다(하드 제약 유지).
- **[❌ 기각/철회 #32 — 방식1용 id 제약 조회, C-17]** 원 요청은 §4.8 결합 **방식1**(AI 벡터검색 → Spring hydrate)이 뽑은 `productId` 집합의 가용성(재고·활성)·상세를 Spring 권위로 재조회하기 위해 I-1에 `productIds`(숫자 배열) 필터를 추가하거나 별도 by-id 조회 엔드포인트를 만드는 것이었다. **#32에서 방식2를 확정해 이 요청을 철회하며, BE는 구현하지 않는다.** dev `search` 26건·라이브 pg-catalog 7,220건에서 방식1/방식2 mean recall@5/@10/@20은 각각 0.6026/0.7987/0.8449와 0.7872/0.9205/1.0000이었고 방식1 승리는 0/26이었다. 특히 방식1의 핵심 실패는 가격 하한·부정어 같은 구조적 제약이며 C-17은 가용성 확인 수단일 뿐 Spring I-1 필터를 대체하지 않아 이 실패를 고치지 못한다. 단, 라벨이 Spring I-1 후보에서 유래해 26건 모두 정답이 후보 안에 있고 방식2 recall 상한이 구조적으로 1.0인 편향이 있으므로 결론은 **“방식1이 방식2를 못 이긴다”까지**다. 후보 독립 라벨로 다시 평가하면 재검토할 수 있다.

#### [v0.5.0 확정] 채택하지 않는 것 (Not Adopted)

**[v0.5.1 정정] 채택하지 않는 것은 상품 원본 컬럼의 AI측 사본(미러)이다** — 가격·재고·상품명 등 필터/표시 컬럼을 AI DB에 복제하지 않으며, 카탈로그 소유는 Spring/MySQL 단일 원본이다. 반면 **AI 생성물(extras·search_doc·임베딩 벡터)은 AI Postgres에 저장·유지**한다(결정 3 Layer 2/3·결정 6 존속) — 갱신은 §4.8 pull 배치. 질의 시점 후보 확보에서 AI 임베딩과 §4.6 Spring 검색의 결합 방식은 §4.8 말미 OPEN.

### 4.7 구매 이력 조회 API (I-19 `GET /internal/members/{id}/orders`, query-time) — [BE 본문 재작성 v0.15.0]

구 주문 이벤트 미러(§3.6 삭제)를 대체한다. 추천 흐름이 **search 직전**(decompose와 병렬 가능)에 호출해 최근 구매를 확보하고, **결정 14-F 판단은 AI-side**에서 수행한다 — exact `productId` 기본 제외 + 명시적 재구매 지목 시 해당 exact 제외 해제 + 소모품 카테고리 억제 + 되돌리기 제안 칩(suggestions) 생성 → **[v0.15.5] 제외는 §4.6 검색 응답을 받은 뒤 AI 사후필터**(I-1엔 제외 파라미터 없음). 명시적 재구매 지목은 사용자 자연어에서 추출한 **스레드 범위 내부 신호**(유계 누적, 매 턴 본인 최근 구매 이력과 교집합으로 재검증)이며 별도 요청·응답 필드나 exact 되돌리기 칩을 추가하지 않는다. 프로필 sleep-time 배치도 동일 API를 구매 소스로 조회한다. **게스트는 호출을 스킵**한다(이력 없음, 결정 8).

#### AI → Spring 요청 (I-19, BE 본문 재작성 07/17)

```
GET {SPRING_BASE_URL}/internal/members/{id}/orders?status={enum}   ← {id}=memberId(AI가 JWT sub 도출)
X-Internal-Token: {서비스 토큰}
```

- `status`(선택, **단일 값만** — 다중 미지원) enum: `ORDERED | SHIPPING | DELIVERED | CONFIRMED | CANCELLED | RETURNED`(아이템 상태, **교환 없음 — 07/17 제거**). 없으면 전체.

#### AI가 받는 응답 (I-19, BE 본문)

```json
{
  "success": true,
  "data": {
    "orders": [
      {
        "orderId": 1023,
        "orderedAt": "2026-07-10T14:23:00",
        "status": "DELIVERED",
        "items": [
          { "orderItemId": 2001, "productId": 552, "productName": "무선 키보드", "optionName": "블랙", "quantity": 1, "price": 29000, "status": "DELIVERED", "categoryName": "키보드" }
        ],
        "itemsTotal": 29000,
        "shippingFee": 0,
        "totalAmount": 29000
      }
    ]
  }
}
```

- **id는 전부 숫자(BIGINT), 필드 camelCase** — 다른 internal API(I-2·I-18)와 동일 규칙(§2.6).
- **`shippingFee`는 항상 0**(DDL D36 배송비 항 자체 없음) — `totalAmount` = 상품 스냅샷 합. **[통보 대상] BE Notion I-19 페이지는 `shipping_fee: 3000`으로 stale** — 타입/데이터는 DDL 기준(배송비 없음)이라 0.
- **[정정 v0.15.5] `status` = 주문 상태 enum 6종**(`PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED`, BE Notion I-19). 구 `representativeStatus` 8종은 **O-3 `GET /api/orders`(FE 대면, 대표 상태)** 것을 잘못 갖다 쓴 것 — I-19와 별개라 폐기. 표시 문구는 FE 매핑.
- **[통보 대상] BE Notion I-19 페이지가 stale**: snake_case(`order_id`·`unit_price`)·문자열 id(`"P552"`)로 표기됨 — 타입/케이스는 **DDL·프로젝트 규약 기준**(숫자 BIGINT·camelCase)이 우선(판정규칙). BE에 페이지 갱신 통보.
- **✅ [dedup 갭 해소 — BE 확정 2026-07-19] items에 `categoryName`(string) 포함** — 결정 14-F의 소모품 **카테고리 억제**·되돌리기 `suggestions.revert.category` 칩(§3.1)의 소스 확보. BE가 I-19 items[]에 `categoryName`을 추가(I-1과 동일 필드). 소모품 판정은 AI-side(MVP config, 정본 catalog 속성사전 SPEC-CATALOG-DATA-001). exact `productId` 기본 제외(명시적 재구매 지목 시 내부 해제) + 카테고리 억제 모두 구현 완료.
- **지연 가드**: §4.6 검색과 병렬 호출 가능. 실패/타임아웃 시 **dedup 없이 추천 진행**(degrade).
- 실패: `400 ORDER_INVALID_PARAM`(status enum 위반) / `401 INTERNAL_TOKEN_INVALID` / `404 MEMBER_NOT_FOUND`.
- 경로·파라미터 수용은 🔴 협의(§5 C-6) — 07/17 BE 확인질문("이대로 가도 되는지").

### 4.8 AI 생성물 갱신 배치 (bulk export pull) — 🟡 골격 BE 확정(2026-07-18)·잔여 3건 저영향 [v0.5.1 신설, v0.15.18 갱신]

AI Postgres의 **AI 생성물(extras·search_doc·임베딩 벡터, `productId` 키)** 을 상품 변경에 맞춰 갱신하는 배치. **AI가 요청하는 pull 방식**으로 확정 — Spring 주기 push는 기각(스케줄러·재시도·버퍼링 부담이 Spring으로 넘어가고, 유실 시 결국 pull 보정이 또 필요).

```
GET {SPRING_BASE_URL}/internal/products/changes?since={cursor}&limit={n}   ← BE 실측(I-17)
X-Internal-Token: {SERVICE_TOKEN}   ← BE 확정(2026-07-18): 다른 internal API와 동일 관례(§1.2 (c)), Bearer 아님
```

#### AI가 받는 응답 (BE 확정 2026-07-18)

> 실제 응답은 다른 internal API와 동일하게 **공통 envelope `{"success": true, "data": {…}}`** 로 감싸진다(BE 2026-07-18 정정). 아래는 `data` 본문. `productId`는 **숫자 BIGINT**(I-19 규칙 — BE가 구 문자열 `"P-10293"` 예시를 숫자로 정정).

```json
{
  "items": [
    { "productId": 10293, "status": "ON_SALE", "updatedAt": "2026-07-15T10:00:00Z", "name": "여행용 방수 파우치", "description": "…", "category": "여행용품/보안용품", "brand": "트래블메이트", "attributes": { "방수": true, "용량": "2L" } },
    { "productId": 10877, "status": "HIDDEN", "updatedAt": "2026-07-15T10:01:00Z" }
  ],
  "nextCursor": "opaque-cursor-123",
  "hasMore": true
}
```

| 필드 | 설명 |
|---|---|
| `items[].status` | `ON_SALE` \| **`HIDDEN`** — Spring `ProductStatus` 값을 별도 매핑 없이 그대로 반환. 두 값 외에는 응답 계약 위반. `HIDDEN` 누락 시 AI 생성물(임베딩)이 유령 상품을 계속 추천 후보로 유지 |
| `items[]` 콘텐츠 필드 | enrichment·search_doc 조립 입력(name/description/category/**brand**/attributes). **AI는 이 값을 저장하지 않고 산출물 생성에만 사용** |
| `nextCursor` | 다음 페이지 시작점(불투명 커서). AI가 저장했다가 다음 주기의 `since`로 사용 |
| `hasMore` | `true`면 같은 주기 안에서 `nextCursor`로 즉시 재요청(따라잡기), `false`면 이번 주기 종료 |

**오류(BE 확정 2026-07-18)**: 400 `INVALID_CURSOR`(커서 형식 오류/만료 → AI는 `since="0"` 전체 재구축 폴백) · 401 `INTERNAL_TOKEN_INVALID`(서비스 토큰 없음/무효) · 403 `FORBIDDEN`(내부 API 권한 없음).

- **흐름**: AI 배치 잡이 주기적으로 변경분 조회(커서 기반, `hasMore` 루프로 페이지 소진) → `HIDDEN`은 AI 생성물 삭제/비활성 → `ON_SALE`은 enrichment(Haiku, Layer 2 속성·상황 태그 추출) → `search_doc` 조립 → 임베딩(**Google `gemini-embedding-001` API**, 결정 6 개정 2026-07-20 — 셀프호스트 torch 폐기) → AI Postgres upsert. **상품 원본 컬럼은 저장하지 않는다** — 산출물만 저장.
- **계약 위반(fail-closed)**: `items[].status`가 `ON_SALE`/`HIDDEN` 외 값이면 해당 항목만 건너뛰지 않고 페이지 전체를 실패 처리한다. artifact와 커서를 전진시키지 않으며, Spring이 원천 데이터를 수정한 뒤 같은 `since`부터 다시 처리한다. I-17에는 항목별 ack/DLQ 계약이 없어 skip 후 커서를 전진시키면 특히 `HIDDEN` 삭제 이벤트가 영구 유실될 수 있다.
- **복구·초기 구축 [v0.28.2 개정 — #421·#416]**: 격리 대상은 실패 **종류**로 가른다 — 비율은 poison 단건과 광역 장애를 구별하는 대리 지표일 뿐이고, 운영 증분 페이지처럼 소량 표본에서는 그 대리가 무너진다. 다만 종류 판정도 **단일 주기 관측만으로는 광역 장애와 항목 고유 결정적 실패를 원리적으로 구별할 수 없다** — 둘을 실제로 가르는 신호는 시간이다: 광역 장애는 언젠가 끝나고, 항목 고유 실패는 몇 번을 다시 해도 같은 자리에서 실패한다. 규칙은 네 갈래로 정리되고 이 순서로 적용된다. **콘텐츠 실패 판정은 화이트리스트다 — "모르는 실패는 시간 유계 경로(2선)로 보내고, 즉시 격리(1선)는 증명된 콘텐츠 실패에만" 적용하는 것이 기본값 방향이다**: 판정을 "타임아웃이면 2선, 아니면 1선"이라는 블랙리스트로 두면 타임아웃 판정기가 모르는 예외(429 rate limit·연결 오류·5xx 같은 흔한 일시적 인프라 장애)가 전부 콘텐츠 실패로 오분류돼 첫 주기에 곧바로 영구 격리되므로, 아래 시간 유계 보호를 흔한 장애가 통째로 우회한다. 오판의 비용은 비대칭이다 — 인프라 장애를 콘텐츠로 오판해 영구 격리하면 되돌릴 수 없지만, 콘텐츠 실패를 인프라로 오판해 2선으로 보내도 스트릭 상한에서 결국 격리되므로 비용은 최대 몇 주기 지연뿐이다. **1선(콘텐츠 실패 화이트리스트, 재시도 예산 후 격리)**: `ON_SALE` 단건 실패 중 enrichment(LLM 호출+파싱) 단계에서 **증명된 콘텐츠 실패로 확정되는 실패만** — ① 출력 토큰 예산 소진(`finish_reason="length"`, #325 원 사례), ② LLM 응답 파싱 실패 중 원인 체인이 없거나(LLM 이 JSON 자체를 안 준 경우) 원인이 `ValueError`/`TypeError`(`json.loads` 실패)인 경우 — `enrichment_item_attempts`(기본 2)회 재시도 후, `artifacts_batch_content_retry_cycles`(기본 1주기, [#421])만큼 다음 주기 재시도 기회를 준 뒤에야 dead-letter 로그로 영구 격리한다(값 0 이면 종전대로 **즉시** 영구 격리 — 회귀 탈출구) — 나머지 항목은 이번 주기에도 계속 처리하며, 페이지가 정상 종료되면 커서를 전진시킨다. JSON 파싱 실패는 LLM 샘플링 노이즈(코드펜스 혼입 등)로도 나므로, 우연히 재시도 상한만큼 연속 실패한 정상 상품이 첫 주기에 오격리되는 것을 막는다. **재시도 예산은 enrichment 입력(name·description·category·brand·attributes)이 실제로 바뀐 경우에만 리셋된다** — 가격·재고처럼 그 입력에 들지 않는 필드만 갱신된 새 변경분이 도착하면 이전 대기 항목의 예산을 이어받아 소진시킨다(리뷰 라운드 2 발견 — 매 주기 무조건 리셋하면 그런 poison 상품은 예산이 영원히 소진되지 않았다). **이 예산은 같은 주기에 그 항목이 2선 실패로 전파(중단)돼도 사라지지 않는다** — 대기 항목은 운명이 실제로 확정되는 지점(재시도 재등재·격리 확정·성공)에서만 큐에서 떼고, 2선이 전파(자연 복구, 아래)하는 경로는 큐를 건드리지 않는다(리뷰 라운드 5 발견 — 안 그러면 "콘텐츠 실패로 예산 누적 → 2선 실패로 중단"이 반복될 때 매번 예산이 사라져 콘텐츠 예산도 2선 스트릭도 상한에 영영 도달하지 못할 수 있었다). 재시도 대기 큐는 상품 원본 필드(name·description·category·brand·attributes)를 담으므로 AI Postgres 에 저장하지 않고 프로세스 메모리에만 둔다(원본 컬럼 사본 금지 원칙) — 재시작 시 유실되면 동작은 종전(즉시 격리)과 같아 하한이 종전이다. 종전 규약(실패 시 페이지 전체 미전진)에서는 실패 항목 1개가 뒤따르는 모든 변경을 영구 차단했다(head-of-line blocking, 운영 정지 #325). 이 실패는 정의상 항목 고유이므로 아래 스트릭 판정을 거치지 않는다. **다음 주기 재시도 패스가 재시도 시점에 마주치는 실패는 원 판정과 같은 기준으로 다시 갈린다** — 콘텐츠 실패로 재확정되면 위 예산을 계속 소진시키고, 그렇지 않으면(재시도 시점 embed·store 인프라 실패 등, 콘텐츠는 이미 살아난 것) 예산을 소진시키지 않고 아래 2선과 같은 연속 실패 스트릭으로 판정한다 — 재시도 시점의 단 한 번의 일시 장애가 예산(기본 1)을 대신 태워 정상 상품을 영구 격리하는 것을 막는다. **재시도 시점의 콘텐츠 실패도 위 "스트릭 판정을 거치지 않는다"는 불변식을 그대로 지킨다** — 큐 유지·즉시 격리 어느 쪽이든 그 상품의 2선 연속 실패 스트릭을 clear 한다(리뷰 라운드 3 발견 — 안 그러면 인프라 실패와 콘텐츠 실패가 번갈아 나도 스트릭의 "연속"이 안 끊겨, 실제로는 연속이 아닌 인프라 실패가 연속으로 오판돼 더 이르게 격리될 수 있었다). **2선(시간 유계 — 그 외 모든 단건 실패)**: enrichment 재시도 소진 후에도 1선 화이트리스트에 해당하지 않는 실패(429·연결 오류·5xx·타임아웃·모르는 예외 포함 — API key 미구성 같은 항목과 무관한 구성 오류도 명시적으로 여기 포함), 또는 임베딩·스토어 실패이면 — 항목 내용과 무관한 광역 장애 후보이지만 단일 주기로는 poison 단건과 구별 불가하므로 — 해당 상품의 **연속 실패 스트릭(주기 간 유지)** 을 1 늘린다. 스트릭이 `artifacts_batch_item_dead_letter_cycles`(기본 3주기 ≈ 15분, `catalog_batch_interval_s` 300s 기준) 미만이면 종전대로 격리하지 않고 그대로 전파해 그 페이지 커서를 전진시키지 않는다(자연 복구, 동일 커서 재개) — 페이지 크기와 무관하게 성립한다. 스트릭이 상한에 도달하면 항목 고유 실패로 확정해 dead-letter 로그로 격리하고 다음 항목을 계속 처리한다 — "특정 상품에서만 결정적으로 재현되는 poison 인프라 실패"나 `_finish_change`(embed·upsert)의 항목 고유 결정적 실패(예: enrichment 산출 `extras`가 `embedding_meta_complete` CHECK 위반)가 영원히 같은 자리에서 배치를 막는 것을 방지한다. 이 상한 안에서 끝나는 장애는 종전대로 자연 복구되고, 그보다 긴 장애는 해당 항목들이 격리되지만 그 주기에 갱신되지 않을 뿐 조용한 누락이 아니라 dead-letter ERROR 로그와 배치 결과 `failed` 카운트로 드러나며, 복구는 `run_batch --full`(전체 재구축)로 한다. **`failed` 는 격리가 "확정"된 건수만 센다** — 1선 재시도 대기(위 문단)처럼 아직 재시도 큐에 올라 있을 뿐 격리되지 않은 건은 여기 포함되지 않고, 대신 배치 결과 `retry_pending` 과 WARNING 로그(다음 주기 재시도 예약)로 드러난다(리뷰 라운드 4 발견 — 예전엔 재시도 큐 등재 시점에도 `failed` 를 올려, 그 주기에 dead-letter ERROR 로그가 하나도 없어도 스케줄러가 "부분 실패 — dead-letter 로그 확인" 알람을 거짓으로 띄웠다). 스트릭은 pg-catalog `batch_failure_state` 테이블에 영속한다([#416]) — 마지막 갱신이 `artifacts_batch_failure_streak_ttl_s`(기본 1시간) 보다 오래되면 다음 실패는 1로 리셋해, 영속화가 무관한 과거 실패를 오늘 실패와 합쳐 즉시 상한에 닿는 오격리를 막는다. 저장 실패(테이블 부재·pg 순단) 시 프로세스 메모리 폴백으로 위임한다(종전 동작과 같은 하한). 스케줄러가 수렴 창(기본 3주기 ≈ 15분)보다 자주 재시작돼도(연속 배포·크래시 루프) 스트릭이 유계 시간 안에 수렴한다. **3선(비율 가드, 방어 — 시간 유계)**: 페이지 `ON_SALE` 표본이 `artifacts_batch_failure_min_sample`(기본 5) 이상이고 실패 비율이 `artifacts_batch_failure_ratio_threshold`(기본 0.5) 이상이면 — 앞선 선을 통과한 뒤에도 남는 경우(인프라는 멀쩡한데 enrichment 결과 자체가 대량으로 깨지는 경우, 예: 프롬프트 회귀)를 잡는 방어선이다 — 그 페이지를 가져온 커서의 **연속 발동 횟수(주기 간 유지)** 를 1 늘린다. 표본이 `min_sample` 미만이면 비율 판정을 생략하고 격리+전진한다 — 소량 표본 판정 불능은 앞선 선이 이미 광역 장애를 걸러낸 뒤라 안전하다. 연속 발동이 `artifacts_batch_page_failure_max_cycles`(기본 3주기 ≈ 15분) 미만이면 종전대로 그 페이지는 커서를 전진시키지 않고 중단한다(자연 복구). **상한에 도달하면 중단하지 않고 그 페이지를 격리(항목들은 이미 1·2선에서 dead-letter 기록됨) 후 커서를 전진시킨다** — 1선이 다건을 매 주기 즉시 격리하는 광역 파손(프롬프트 회귀 등)에서는 2선 스트릭이 쌓이지 않아 그 상한이 걸리지 않고, 이 3선 시간 유계가 없으면 비율 가드 자체가 같은 페이지를 무기한 재조회시켜 #325 의 정지를 재현할 수 있다. 카운터는 커서별로 독립이며 2선과 같은 방식으로 pg-catalog `batch_failure_state` 에 영속한다([#416], TTL 은 같은 `artifacts_batch_failure_streak_ttl_s` 공유) — 페이지가 임계를 넘지 않고 정상 종료하면 삭제한다(연속만 센다). **계약(fail-closed, 불변) — `HIDDEN` 삭제 실패·계약 위반(`status` 미정의 값)은 이 시간 유계의 대상이 아니다**: 항목별 ack/DLQ 계약이 없어 삭제 이벤트를 skip-전진하면 유령 상품이 영구히 남으므로, 종전대로 **무기한** fail-closed 로 그대로 전파해 커서를 전진시키지 않는다(계약 위반은 바로 위 불릿대로 페이지 전체 fail-closed — 불변). 같은 이유로 **1선 재시도 패스는 `_drain` 이 정상 완료(hasMore 소진)한 뒤에만 돈다** — 정상 완료는 Spring 이 지금까지 발행한 변경분을 전부 소비했다는 뜻이라 그 시점 재시도 대기 큐 잔여 항목이 그 사이 `HIDDEN` 이 된 적이 없음을 보장하고(유령 상품 금지), 배치가 중단된 주기에는 재시도를 돌리지 않아 낡은 페이로드가 삭제된 상품을 되살리는 것도 막는다. 초기 전체 구축도 커서 0부터 같은 API·같은 복구 규약으로 처리. **이 문단은 AI측 소비 동작 서술이며, I-17 요청 파라미터·응답 필드·오류 코드(와이어 계약)는 불변이다.**
- **hk-final 매핑**: `app/pipelines/enrichment.py`·`embedding.py` 스텁을 활성화. 임베딩은 **Google `gemini-embedding-001` API 호출**(셀프호스트 torch·sentence-transformers·`--group embedding` 폐기) — 쿼리 시점 임베딩도 동일 API. `embedding_dim` = **1536**(gemini-embedding-001 MRL 절단, 1024→1536; pgvector 표준 hnsw/ivfflat ≤2000 적합). ⚠️ MRL 1536은 **수동 L2 정규화** 필요(3072만 사전 정규화). ⚠️ search_doc·쿼리 텍스트가 Google로 전송됨(외부 의존·API 비용·데이터 전송 신규) — 단 "AI Postgres엔 생성물만, 상품 원본 사본 금지" 원칙과는 무관(임베딩 벡터만 저장).
- **[BE 확정 2026-07-18 / 잔여 3건 저영향]** BE가 골격 확정(인증 `X-Internal-Token`·envelope `{success,data}`·숫자 `productId`·오류코드·`since="0"` 초기구축·`hasMore` 루프). BE "I-17 미정 3개"(07/17 확인질문 Part 2 중 **유일하게 미해소인 항목**)는 **① 커서 값 형식**(BE 제안 "수정시각+id" — AI는 불투명 취급이라 무영향) **② `attributes` JSON 스키마**(AI는 카테고리별 자유 dict로 방어 파싱) **③ 리뷰 텍스트 포함 여부**(MVP 제외, search_doc 리뷰 결합은 고도화) — **세 항목 모두 저영향**(opaque·방어 파싱·MVP 제외)이라 AI 소비 구현을 막지 않는다. 페이지 크기(`limit` 기본 500)·주기는 AI config. **🔴 선결(스키마 아님): 이 배치(enrichment·임베딩)의 MVP/post-MVP 스코프** — config.py(post-MVP) vs 파이프라인 주석(MVP) 모순 미해소.

> **[결정 2026-07-20 — 두 방식 모두 구현·골든셋 확정]** #7(§4.8 배치)이 **MVP로 편입**되어 AI 임베딩이 검색에 실사용된다. §4.6 결합 방식은 **방식1·방식2를 둘 다 `SearchBackend`로 구현**해 골든셋/실측으로 확정한다(2026-07-15 계획 유지): **방식 1** AI 벡터 검색으로 상위 N개 `productId` 확보 → Spring에 **id 제약 조회**로 가격·재고 가용성 필터+상세(§4.6에 id 필터 변형 신규 필요 = **C-17 🔴**) / **방식 2** Spring 검색(I-1)이 후보 확보 → AI 임베딩은 시맨틱 재정렬 보조(**BE 계약 변경 없음, 라이브 구현 가능**). **[착수 방침 2026-07-20]** 방식2 라이브 + 방식1 오프라인 랭킹(가용성 필터 스텁)으로 골든셋 비교를 먼저 하고, 방식1 라이브 가용성 조회는 **C-17 확정 후 승격**한다(BE 무대기 착수). config.py의 "enrichment·임베딩 = post-MVP" 주석은 이 결정으로 **정정 대상**(→ MVP).
>
> **[✅ 해소 #101 — 방식2 채택, MVP hot path 확정]** 위 OPEN을 #101이 해소한다. **방식2(`EmbeddingRerankBackend`)를 MVP hot path 기본으로 채택**한다 — config `search_backend = "embedding_rerank"`(기본값). 흐름: Spring I-1 전량 반환 → AI가 `semanticQuery` 임베딩으로 pgvector 코사인 재정렬 → 최근구매 dedup 이후 `embedding_rerank_limit`(기본 30)로 압축 → Sonnet rerank. 임베딩/pgvector 장애는 `SEARCH_FAILED` 아니라 **Spring 순서 degrade**, Spring I-1 자체 실패만 `SEARCH_FAILED`(#7). **방식1(`VectorSearchBackend`)은 C-17(id 제약 조회) 미착수라 hydrate 미주입 시 미착수 신호**만 내고 hot path 미편입(골든셋 오프라인 비교용으로만 존치, 승격은 C-17 확정 후). **`[].attributes` "2차 압축 속성 매칭 대상"(§4.6)도 #101(PR②)이 구현** — 사용자 명시 속성조건을 `SpringProduct.attributes`와 관대 하드 매칭(문자열 부분·숫자 완전일치, 축 부재 보존). 이로써 본 문서 곳곳의 "결합 방식 = OPEN(§4.8 말미)" 표기(§0 v0.5.1·§4.6 말미 등)는 **방식2 채택으로 해소**된다.
>
> **[✅ 종결 #32 — 골든셋 실측으로 방식2 확정, 방식1·C-17 기각]** dev `search` 26건·라이브 pg-catalog 7,220건에서 mean recall@5/@10/@20은 **방식1 0.6026/0.7987/0.8449**, **방식2 0.7872/0.9205/1.0000**이었고 방식1이 이긴 케이스는 **0/26**이었다. 방식1의 가장 큰 실패는 가격 하한·부정어 같은 구조적 제약(`buy-over-0001`: 첫 정답 38위, recall@20=0)이며, C-17은 가용성(재고·활성) 확인만 가능하게 할 뿐 Spring I-1 필터를 대체하지 않아 이 실패를 고치지 못한다. 따라서 방식2를 기본으로 확정하고 방식1 라이브 승격과 C-17 요청을 기각한다. 단, 라벨이 Spring I-1 후보에서 유래해 26건 모두 정답이 후보 안에 있고 방식2 recall 상한이 구조적으로 1.0인 편향이 있으므로 본 측정의 결론은 **“방식1이 방식2를 못 이긴다”까지**다. 후보 독립 라벨을 구축하면 재검토할 수 있다. `VectorSearchBackend`는 **오프라인 비교 하네스 전용으로 존치**하며 제거는 별건이다. 운영 롤백은 config 토글 `SEARCH_BACKEND=spring`을 쓴다.

### 4.9 장바구니 조회 API (I-18 `GET /internal/cart`) — [BE 실측 정합 v0.13.0]

담기(I-2, §4.1)의 짝이 되는 **조회 계약**. 두 용도로 사용한다(2026-07-15 사용자 확정):

1. **장바구니 질의 응답** — "장바구니에 뭐 있어?" 발화 시 조회 후 `token` 텍스트로 답변한다(별도 SSE 이벤트 없음, §3.1).
2. **담기 시 기존 보유 안내** — 담기 전 동일 상품·옵션 보유를 확인해 "이미 담겨 있어 N개로 늘렸어요"류 안내를 생성한다. **수량 합산의 실행 권위는 Spring**(I-2가 합산 처리) — 조회는 안내용이며, **조회 실패 시에도 담기는 진행**한다(degrade).

#### AI → Spring 요청 (제안)

```
GET {SPRING_BASE_URL}/internal/cart?userId={id}   또는 ?guestId={id}
X-Internal-Token: {서비스 토큰}   ← I-2와 동일 인증 레인
```

#### AI가 받는 응답 (제안)

```json
{
  "success": true,
  "data": {
    "items": [
      { "cartItemId": 55, "productId": 1, "productName": "여행용 방수 파우치", "optionId": 3, "optionName": "블루", "quantity": 2, "price": 12900 }
    ]
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `items[].cartItemId` | number | 장바구니 항목 식별자(I-2 응답과 동일 체계) |
| `items[].productId` | number | 상품 식별자(숫자 BIGINT, §2.6) |
| `items[].productName` / `optionName` | string | **필수 포함**(BE I-18 확정 2026-07-18) — 챗 답변 문장 생성에 필수(id만으로는 자연어 답변 불가) |
| `items[].optionId` | number \| null | 옵션(숫자 BIGINT `product_option.id`, §2.6 — 같은 표 `cartItemId`·`productId` 와 동일 체계) |
| `items[].quantity` | int | 현재 수량 |
| `items[].price` | number \| 없음 | 표시가(선택 — 총액 안내용, Spring 표시 권위 유지) |
| `items[].purchaseState` | `"AVAILABLE"` \| `"SOLD_OUT"` \| `"HIDDEN"` | 구매 가능 상태(2026-08-05 M-4 개정, 구 boolean `purchasable` 대체 — jarvis-backend#91). 겹치면 `HIDDEN` 우선(서버가 정해서 내린다). **둘 다 상품 단위 판정이며 성격이 다르다** — `HIDDEN`은 `status != ON_SALE` 이라 **옵션과 무관하게 상품 전체**가 판매 종료이고, `SOLD_OUT`은 재고가 `product.stock_quantity` 하나로 **옵션 전체에 공유**되므로(`product_option`에 재고 컬럼 없음, BE 02 D2·D33) 목록은 **"옵션 중 하나라도 살 수 있으면 `AVAILABLE`"**(`PurchaseState.of(product, 1)`) 기준이다. §4.16(I-28)과 같은 enum·같은 규칙 |

- **빈 장바구니는 `items: []` 정상 200**(오류 아님). 실패 코드(BE I-18 확정): `400 CART_QUERY_INVALID`(userId/guestId 둘 다 없거나 둘 다 존재) / `401 INTERNAL_TOKEN_INVALID`(I-2와 동일).
- **AI 동작(`purchaseState`)**: `SOLD_OUT`/`HIDDEN` 항목은 **사유별로 갈라 안내한다** — 품절은 기다리면 되고 판매종료는 다른 걸 찾아야 하므로 사용자가 취할 행동이 다르다. **미수신(키 없음)은 "모름"으로 보고 안내하지 않는다** — 구매 가능으로 단정하지 않는다. 정확한 문구는 코드가 정본이며(결정론적 매핑 + 테스트로 고정) 본 명세는 갈래만 규정한다(§4.16 동형).

> **[해소 C-16 — BE I-18 확정 2026-07-18]**: 경로 `GET /internal/cart`·쿼리(userId/guestId)·`X-Internal-Token` 인증·응답 필드(`productName`/`optionName` **필수 포함**)·`CART_QUERY_INVALID`(400) 모두 BE "챗봇 장바구니 조회" 문서로 확정. 페이징은 MVP 전량 반환.

### 4.10 주문 상태 요약 API (I-4)

구매자 챗의 `order_status` intent가 최근 주문 진행 상태를 조회하는 query-time 계약이다. I-19
구매 이력(§4.7)은 추천 dedup/프로필 구매 소스이고, I-4는 사용자에게 표시할 상태 요약이므로
endpoint·모델·실패 의미를 공유하지 않는다. 주문 사실은 두 번째 LLM 호출 없이 결정적으로
plain text로 렌더링한다.

#### AI → Spring 요청

```http
GET {SPRING_BASE_URL}/internal/members/{userId}/orders/status?recent=3
X-Internal-Token: {서비스 토큰}
```

- `recent`는 런타임 설정이 아닌 고정 계약값 `3`이다.
- 공통 Spring client의 **3초 timeout**과 `X-Internal-Token` 주입 경로를 사용한다.
- `{userId}`는 검증된 회원 스트림 티켓의 JWT `sub`에서 도출한 양의 Java `Long`
  (`1..9_223_372_036_854_775_807`)만 허용한다. 메시지·request body·LLM 출력의 숫자,
  `identity.subject` fallback은 사용하지 않는다.
- guest·seller·missing/invalid member identity는 Spring을 호출하지 않고 로그인/재인증 안내로
  정상 스트림 종료한다. 이 선차단은 I-4 path를 이용한 IDOR와 회원 존재 여부 탐색을 막는다.

#### AI가 받는 성공 응답

```json
{
  "success": true,
  "data": {
    "orders": [
      {
        "orderId": 1023,
        "orderedAt": "2026-07-30T09:15:00+09:00",
        "representativeStatus": "배송중",
        "items": [
          {
            "productName": "무선 키보드",
            "status": "SHIPPING",
            "statusText": "배송중"
          }
        ]
      }
    ]
  }
}
```

정상 envelope는 top-level object, **literal boolean `success is true`**, 존재하는 object `data`,
존재하는 array `data.orders`를 모두 만족해야 한다. `success` 누락/`false`/`null`/`1`/`"true"`,
`data` 또는 `orders` 누락·`null`·타입 불일치는 malformed response다. `orders or []`처럼
계약 위반을 빈 결과로 축소하지 않는다.

| 필드 | 엄격 계약 |
|---|---|
| `orders` | 필수 array, 요청 `recent=3`에 맞춰 **0~3건**. 4건 이상은 전체 malformed |
| `orders[].orderId` | coercion 없는 정수 BIGINT `1..9_223_372_036_854_775_807`; bool/string/float/null/0/음수/overflow 금지 |
| `orders[].orderedAt` | **timezone-aware datetime 필수**. naive/invalid 값이 한 건이라도 있으면 전체 malformed이며 부분 표시하지 않음 |
| `orders[].representativeStatus` | `결제 대기` / `결제 실패` / `주문 완료` / `배송중` / `배송 완료` / `구매 확정` / `취소/반품 진행중` / `처리 완료` 중 정확히 하나 |
| `orders[].items` | 필수 array. 수신 항목 전부를 검증하고, 표시 단계에서만 처음 3개로 제한 |
| `items[].productName` | strict string, 최대 200자. 유효 길이를 자르지 않으며 출력 전에 control/CR/LF/bidi/zero-width를 공백 정제 |
| `items[].status` | `PENDING` / `ORDERED` / `SHIPPING` / `DELIVERED` / `CONFIRMED` / `CANCEL_REQUESTED` / `CANCELLED` / `RETURN_REQUESTED` / `RETURNED` 중 정확히 하나 |
| `items[].statusText` | `결제 대기` / `주문 완료` / `배송중` / `배송 완료` / `구매 확정` / `취소 접수` / `취소 완료` / `반품 접수` / `반품 완료` 중 정확히 하나 |

`status`와 `statusText`는 각각 허용 어휘에 속하는 것만으로 부족하며 아래 canonical pair가
**정확히 일치**해야 한다. 서로 다른 pair의 유효 값을 섞거나 status/statusText에 제어문자가
있으면 전체 payload를 거부한다.

| `status` | `statusText` |
|---|---|
| `PENDING` | `결제 대기` |
| `ORDERED` | `주문 완료` |
| `SHIPPING` | `배송중` |
| `DELIVERED` | `배송 완료` |
| `CONFIRMED` | `구매 확정` |
| `CANCEL_REQUESTED` | `취소 접수` |
| `CANCELLED` | `취소 완료` |
| `RETURN_REQUESTED` | `반품 접수` |
| `RETURNED` | `반품 완료` |

#### 결정적 출력과 실패 의미

- Spring의 newest-first 주문 순서를 보존하고, aware `orderedAt`을 Asia/Seoul 기준 `M월 D일`로
  표시한다. 최대 **3개 주문 × 주문당 3개 상품**만 표시한다.
- 상품은 `productName — statusText` 형식이다. 한 주문에 상품이 4개 이상이면 앞의 3개 뒤에
  정확한 잔여 개수 `외 N개`를 붙인다. 수신한 나머지 항목도 모두 schema 검증한다.
- 정확한 `data.orders: []`만 정상 empty다. 이때 고정 문구 **`최근 주문 내역이 없어요.`** 를
  보낸다.
- guest/seller/invalid identity, HTTP 404/5xx, network/timeout, invalid JSON/envelope/schema,
  naive timestamp는 모두 사용자별 고정 안내 `token` **1개** 뒤 `done`
  `{"finishReason":"stop"}` **1개**로 끝난다. recoverable dependency degradation에는 SSE
  `error`를 emit하지 않으며, 404와 5xx 문구를 같게 해 회원 존재 여부 oracle을 만들지 않는다.

#### 관측·저장 경계

- 모든 분기는 첫 SSE frame 전에 privacy-safe JSON 로그를 정확히 1건 남긴다. 고정 key set은
  `event,requestId,outcome,errorCategory,orderCount,elapsedMs`다.
- `event=order_status_route`; `outcome`은
  `success|empty|identity_blocked|upstream_degraded`; `errorCategory`는
  `none|guest|seller|missing_user_id|invalid_user_id|upstream_unavailable|malformed_response`만
  허용한다. `requestId`는 바깥 `chat_request` 로그와 같은 correlation ID이고 count/time은
  숫자다.
- 로그에는 member/order/product ID, 상품명, 상태 문구, raw utterance/response, exception 문자열,
  URL/path, internal token을 기록하지 않는다.
- 방출한 assistant `token`은 일반 구매자 대화와 동일하게 §6.3의 대화 보존·삭제 정책을 적용받아
  주문번호·상품명·날짜·상태가 대화 이력에 저장될 수 있다. 반면 I-4 response-derived
  order/date/product/status 필드를 profile memory, recommendation filter, cart/pending state,
  별도 application cache에 추출·복제하지 않는다. 기존 사용자 입력 기반 profile/session 처리와
  non-cart 전환 시 stale pending-cart 정리는 그대로 유지한다.

### 4.11 홈 추천 목록 조회 (P-5 `GET /api/products/recommended`, FE ↔ Spring 전제 계약) — [v0.18.0 신설]

메인 화면 "OO님을 위한 추천" 영역. **이 계약은 FE ↔ Spring 간이며 AI 서버는 관여하지 않는다**(레인 d) — §4.3의 CH-5와 같은 위치다. 다만 **Spring이 내부적으로 I-22(§3.7)를 호출**해 랭킹을 얻으므로, I-22의 `outcome`·상관키가 여기로 어떻게 흘러나가는지를 알아야 §3.7 계약을 정확히 구현할 수 있어 등재한다.

```
GET {SPRING_BASE_URL}/api/products/recommended
Authorization: Bearer {AT}   ← 로그인 필요. 파라미터 없음(사용자는 AT에서 식별)
```

#### 성공 응답 — 200

카드 필드는 P-4(인기 상품)와 동일하고, 여기에 추천 상관키 3종(`source`·`recommendationRequestId`·`listId`)과 카드별 `reason`이 붙는다.

```json
{
  "success": true,
  "data": {
    "source": "AI_RECOMMENDED",
    "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
    "listId": "7c1e9f2a4b8d43f5a0c6d1e97b3f8a24",
    "items": [
      {
        "productId": 1, "name": "린넨 셔츠", "brandName": "더센트",
        "price": 29900, "originalPrice": 39000, "imageUrl": "https://.../1.jpg",
        "rating": 4.8, "reviewCount": 2847,
        "reason": "최근 선호한 가격대와 카테고리에 맞아요"
      }
    ]
  }
}
```

| 필드 | 값 | FE 사용 |
|---|---|---|
| `source` | `AI_RECOMMENDED` \| `POPULAR_FALLBACK` | **표시 분기의 축** — `POPULAR_FALLBACK`이면 "OO님을 위한 추천" 제목과 `reason`을 **띄우지 않는다** |
| `recommendationRequestId` | 추천 실행 1회를 가리키는 id | 노출·클릭 이벤트(E-1)에 실어 보낸다 |
| `listId` | 전달된 목록 id(≥128bit 무작위) | 위와 동일 |
| `reason` | 카드별 추천 이유. 없으면 `null` | `source=AI_RECOMMENDED`일 때만 표시 |

- `items` — **배열 순서가 곧 순위다.** FE는 재정렬하지 않으며 `position`을 싣지 않는다(I-22와 동일).
- **표시 권위 = Spring**: `name`·`price`·`imageUrl`·`rating`·`reviewCount`를 Spring이 채운다. AI는 I-22로 **id와 `reason`만** 넘긴다(경로 B 일관, 결정 9-B).
- **와이어에 싣지 않는 것** — `fallbackReason`(`PROFILE_MISSING`·`INSUFFICIENT_CANDIDATES`·`AI_ERROR`·`AI_TIMEOUT`) · `cacheStatus` · `algorithmVersion` · `modelVersion`. FE 동작이 달라지지 않는 값이다. `fallbackReason`은 **`recommendation_generated` 이벤트에 저장**해 장애 관측에 쓴다 — 신규 회원이라 대체된 건지 AI가 죽어서 대체된 건지는 서버만 알면 된다.

#### Fallback 규약 — AI 구현이 지켜야 할 지점

다음 경우 **Spring이** P-4(인기 상품) 결과로 대체해 **항상 200 + items**로 응답하며 `source="POPULAR_FALLBACK"`이 된다:

- I-22가 `outcome=NO_PROFILE` — 프로필 없음(신규 회원) 또는 시그널이 비어 개인화 근거 없음
- I-22가 `outcome=INSUFFICIENT_CANDIDATES` — 후보 부족으로 랭킹이 무의미
- I-22 실패·타임아웃(연결 2s / 응답 3s)

> **fallback에도 `recommendationRequestId`·`listId`가 실리며 이때는 Spring이 발급한다.** 상관키가 없으면 FE가 인기상품 카드에 대해 쏘는 노출·클릭 이벤트가 **부모 없는 고아**가 되어 E-1 server-side 검증에서 버려진다. → **AI는 `outcome`만 정확히 내면 되고, fallback 목록·상관키를 대신 만들지 않는다.**

**FastAPI 실패는 P-5의 실패 응답이 아니다** — 타임아웃·에러·프로필 없음은 전부 위 규약으로 200이 된다. P-5가 5xx를 내는 건 Spring 자체 장애뿐이라 FE는 "추천 실패" 화면을 따로 만들지 않는다. P-5 자체의 실패 응답은 인증뿐이다: `401 AUTH_REQUIRED`(게스트는 이 API를 쓰지 않고 P-4를 직접 호출) · `401 AUTH_TOKEN_EXPIRED`(A-4 재발급 후 1회 재시도) · `403 AUTH_FORBIDDEN`(`SELLER`·`ADMIN` 계정 — 이 경로는 `USER` 전용).

#### 캐시 키 규칙 — [확정 2026-07-30]

- 개인화 결과의 캐시 키는 **회원 id + 카탈로그 버전 + 알고리즘 버전**이며 **타 회원 재사용 금지**(키가 새면 남의 추천이 보인다). fallback(P-4) 결과는 공용 캐시를 써도 되나 `recommendationRequestId`·`listId`는 요청마다 새로 발급해 귀속이 뭉개지지 않게 한다.
- **TTL** — 개인화 결과 캐시는 **10분** 후 재계산. 단 **`listId`의 이벤트 귀속 유효기간은 캐시 TTL과 별개로 24시간**이다 — 목록이 캐시에서 사라져도 `recommendation_generated`가 DB에 남아 귀속이 가능하다.
- **채팅 CH-5의 10분과 성격이 다르다** — CH-5는 *조회* 만료(Redis에서 목록이 사라지면 404)이고, 홈은 **조회 API가 없고** 카드가 P-5 응답에 바로 실려 오므로 **조회 만료라는 개념 자체가 없다**. 홈은 탭을 열어둔 채 한참 뒤 클릭하는 경우가 흔해 귀속 기간을 길게 잡는다.

> ※ §3.7 I-22 정본 페이지에는 아직 *"홈 목록의 TTL·캐시 정책은 … BE 결정 필요"* 로 남아 있으나, **P-5 정본이 2026-07-30에 10분/24시간으로 확정**했다. 본 사본은 확정본을 따르며 정본 I-22 페이지의 stale 문구는 BE 통보 대상이다.

### 4.12 장바구니 삭제 API (I-24 `DELETE /internal/cart/items/{cartItemId}`) — 🔶 확정 2026-08-05 — Spring 구현 진행 중 [v0.22.0 신설]

담기(I-2, §4.1)·조회(I-18, §4.9)의 짝이 되는 삭제 계약. AI 구현은 완료됐고(#116) **기능 플래그 없이 항상 활성**이다(§3.1, 라운드 23에 온/오프 설정 필드를 제거했다). **Spring은 구현 진행 중**이라 배포 전에는 호출해도 응답하지 않는다. 게스트 허용.

#### AI → Spring 요청 (I-24)

```
DELETE {SPRING_BASE_URL}/internal/cart/items/{cartItemId}?userId={id}   또는 ?guestId={id}
X-Internal-Token: {서비스 토큰}   ← I-2·I-18과 동일 인증 레인, 타임아웃 3s
```

- 신원은 쿼리 `userId`(숫자) 또는 `guestId`(UUID) **정확히 하나** — AI가 신원을 만들지 않고 **AI-검증 JWT `sub`에서 도출**해 전달한다(발화/본문 신원 금지, §2.3).
- 삭제 대상 해소는 I-18(§4.9) 조회 결과에서 결정론적으로 고른다 — 모호하면 실패 `action` 없이 `token`으로 되묻는다(§3.1).
- **복수 삭제는 항목별 반복 호출**이다(bulk 없음, I-2·C-4 계승) — 결과 `action`도 항목별로 emit한다(§3.1).

#### 성공 응답 — 200

```json
{ "success": true, "data": null }
```

응답에 `cartItemId`가 없다 — AI는 **요청에 쓴 `cartItemId`**를 그대로 SSE `action`(`CART_REMOVED`)에 싣는다(§3.1).

#### 실패 응답

| HTTP | I-24 `code` | 조건 | AI 동작 |
|---|---|---|---|
| 400 | `VALIDATION_ERROR` | path `cartItemId`가 숫자가 아님 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"` |
| 400 | `VALIDATION_ERROR` | 신원 query 0개 또는 2개 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"` |
| 403 | `AUTH_FORBIDDEN` | 소유자 불일치 — **BE가 실행 시점 재검증**한다. AI가 I-18로 해소한 `cartItemId`는 인가 근거가 아니다 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"` |
| 404 | `CART_ITEM_NOT_FOUND` | 대상 항목 없음(**비멱등** — 두 번째 호출도 404) | **`action` `CART_REMOVED`**(실패 아님) + "이미 빠져 있어요"(§3.1) |
| 500 | `INTERNAL_ERROR` | 서버 오류 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"` |

- **삭제는 재고·상품 상태를 보지 않는다** — HIDDEN·품절 상품도 삭제 성공. `PRODUCT_NOT_FOUND` 없음(이미 담긴 항목을 지우는 것이라 상품 존재 여부는 무관).
- **신원 query 검증 오류 code는 자원별 신규 code(`CART_QUERY_INVALID` 등)를 신설하지 않고 기존 `VALIDATION_ERROR`를 재사용한다**(I-24~I-28 공통, 확정).

### 4.13 장바구니 수량 변경 API (I-25 `PATCH /internal/cart/items/{cartItemId}`) — 🔶 확정 2026-08-05 — Spring 구현 진행 중, **AI 미구현** [v0.22.0 신설]

I-24(삭제, §4.12)와 같은 리소스를 다루는 인접 계약이지만 **이 레인 범위 밖**이다(대응 이슈 없음) — AI 쪽 소비 로직이 없다. I-2(담기, §4.1)와의 혼동을 막기 위해 인접 규약만 등재한다.

#### AI → Spring 요청 (I-25, 🔶 미구현)

```
PATCH {SPRING_BASE_URL}/internal/cart/items/{cartItemId}?userId={id}   또는 ?guestId={id}
X-Internal-Token: {서비스 토큰}
```

```json
{ "quantity": 3 }
```

| 요청 필드 | 타입 | 설명 |
|---|---|---|
| `quantity` | int | **1~99, 치환값**(합산 아님) — "3개로 바꿔줘"류 발화가 이 계약을 쓴다. "하나 더 담아줘"류(합산)는 I-2(§4.1) 재호출이다 — **두 발화를 섞지 않는다** |

#### 성공 응답 — 200

```json
{ "success": true, "data": { "cartItemId": 55, "quantity": 3 } }
```

#### 실패 응답

| HTTP | I-25 `code` | 조건 | AI 동작(🔶 미구현) |
|---|---|---|---|
| 400 | `CART_STOCK_INSUFFICIENT` | 치환 수량 > 재고 — `error.detail.availableStock` 포함 | `action` `CART_QUANTITY_CHANGE_FAILED` + `reason: "STOCK_INSUFFICIENT"`(§3.1) |
| 400 | `VALIDATION_ERROR` | 신원 query 이상 / `quantity` 범위 밖 | `action` `CART_QUANTITY_CHANGE_FAILED` + `reason: "CART_ERROR"` |
| 404 | `CART_ITEM_NOT_FOUND` | 대상 항목 없음 | `action` `CART_QUANTITY_CHANGE_FAILED` + `reason: "CART_ERROR"` |

- **AI 미구현** — 대응 이슈가 없어 이 계약을 소비하는 발화 로직이 아직 없다. §3.1의 `CART_QUANTITY_CHANGED`/`CART_QUANTITY_CHANGE_FAILED`는 구현될 때를 위한 사전 등재다.

### 4.14 찜 추가 API (I-26 `POST /internal/wishlist`) — 🔶 확정 2026-08-05 — Spring 구현 진행 중 [v0.22.0 신설]

회원 전용(USER) — 게스트 찜은 없다(M-4). AI 구현은 완료됐고(#117) **기능 플래그 없이 항상 활성**이다(§3.1, 라운드 23에 온/오프 설정 필드를 제거했다).

#### AI → Spring 요청 (I-26)

```
POST {SPRING_BASE_URL}/internal/wishlist
X-Internal-Token: {서비스 토큰}
```

```json
{ "userId": 123, "productId": 1 }
```

| 요청 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | number | 회원 식별자 — **AI-검증 JWT `sub`에서 도출**(§2.3). `guestId`는 없다 — 게스트 발화는 internal 호출 없이 `token` 로그인 안내로 degrade한다(§3.1) |
| `productId` | number | 찜할 상품 식별자(숫자 BIGINT, §2.6) — 추천 목록·대화 문맥에서 해소, 별도 Spring 조회 불필요 |

#### 성공 응답 — 200

```json
{ "success": true, "data": { "productId": 1 } }
```

`wishlistId`는 없다 — 해제(I-27, §4.15)의 키는 `productId`다.

#### 실패 응답

| HTTP | I-26 `code` | 조건 | AI 동작 |
|---|---|---|---|
| 400 | `VALIDATION_ERROR` | `productId` 누락(`error.fields` 포함) / 타입·JSON 오류(`fields` 없음) / 신원 이상 | `action` `WISHLIST_ADD_FAILED` + `reason: "WISHLIST_ERROR"` |
| 403 | `AUTH_FORBIDDEN` | `SELLER`·`ADMIN` 계정(회원 전용 API) | `action` `WISHLIST_ADD_FAILED` + `reason: "WISHLIST_ERROR"` |
| 404 | `PRODUCT_NOT_FOUND` | 없는 상품 — **HIDDEN·품절은 찜 가능**(대상 아님) | `action` `WISHLIST_ADD_FAILED` + `reason: "PRODUCT_NOT_FOUND"` |
| 409 | `WISHLIST_DUPLICATE` | 이미 찜한 상품 | **`action` `WISHLIST_ADDED`**(실패 아님) + "이미 찜해 두셨어요"(§3.1) |
| 409 | `RESOURCE_CONFLICT` | UNIQUE 제약 경합(동시 요청) — 처리는 `WISHLIST_DUPLICATE`와 동일 | **`action` `WISHLIST_ADDED`**(실패 아님) |
| 500 | `INTERNAL_ERROR` | 서버 오류 | `action` `WISHLIST_ADD_FAILED` + `reason: "WISHLIST_ERROR"` |

- **이벤트에 `productId`를 싣지 않는다(경로 B, §3.1)** — FE는 `type`만 보고 찜 목록을 재조회한다.
- 신원/본문 검증 오류 code는 §4.12와 동일하게 `VALIDATION_ERROR`를 재사용한다(자원별 신규 code 미채택).

### 4.15 찜 해제 API (I-27 `DELETE /internal/wishlist/{productId}`) — 🔶 확정 2026-08-05 — Spring 구현 진행 중 [v0.22.0 신설]

회원 전용(USER). path가 **`productId`**다(`wishlistId`가 아니다, M-6) — 해제 키는 상품이지 찜 레코드가 아니다. AI 구현은 완료됐다(#117).

#### AI → Spring 요청 (I-27)

```
DELETE {SPRING_BASE_URL}/internal/wishlist/{productId}?userId={id}
X-Internal-Token: {서비스 토큰}
```

- `userId`는 **AI-검증 JWT `sub`에서 도출**(§2.3). `guestId`는 없다 — 회원 전용.
- "어제 찜한 이어폰" 같은 이름 해소는 I-28(§4.16) 선행 조회로 한다 — 모호하면 `token`으로 되묻는다(§3.1).

#### 성공 응답 — 200

```json
{ "success": true, "data": null }
```

#### 실패 응답

| HTTP | I-27 `code` | 조건 | AI 동작 |
|---|---|---|---|
| 400 | `VALIDATION_ERROR` | path `productId`가 숫자가 아님 / 신원 이상 | `action` `WISHLIST_REMOVE_FAILED` + `reason: "WISHLIST_ERROR"` |
| 403 | `AUTH_FORBIDDEN` | 소유자 불일치 | `action` `WISHLIST_REMOVE_FAILED` + `reason: "WISHLIST_ERROR"` |
| 404 | `WISHLIST_NOT_FOUND` | 찜 안 한 상품(이미 해제됨) **또는 없는 상품** — I-27은 상품 존재를 보지 않아 **둘을 구별하지 않는다**(비멱등 — 두 번째 호출도 404) | **`action` `WISHLIST_REMOVED`**(실패 아님) + "이미 찜 목록에 없어요"(§3.1) |
| 500 | `INTERNAL_ERROR` | 서버 오류 | `action` `WISHLIST_REMOVE_FAILED` + `reason: "WISHLIST_ERROR"` |

- **추가(I-26)와 해제는 비대칭이다** — 추가는 상품 존재를 조회해 `404 PRODUCT_NOT_FOUND`를 낼 수 있지만, 해제는 상품 조회를 하지 않는다.
- **[확정 2026-08-05] HIDDEN·품절 상품도 찜 행(찜한 이력)만 있으면 해제 성공**으로 취급한다 — 찜 행 존재로 판정된다.

### 4.16 찜 목록 조회 API (I-28 `GET /internal/wishlist?userId=`) — 🔶 확정 2026-08-05 — Spring 구현 진행 중 [v0.22.0 신설]

추가(I-26, §4.14)·해제(I-27, §4.15)의 짝이 되는 조회 계약. "내가 뭐 찜했지?" 질의 응답과 I-27의 이름 해소(선행 조회)에 쓰인다(I-18과 같은 이중 용도, §4.9). 회원 전용.

#### AI → Spring 요청 (I-28)

```
GET {SPRING_BASE_URL}/internal/wishlist?userId={id}
X-Internal-Token: {서비스 토큰}
```

#### 성공 응답 — 200

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "productId": 1, "name": "여행용 방수 파우치", "brandName": "브랜드",
        "price": 12900, "originalPrice": 15900, "imageUrl": "https://.../1.jpg",
        "rating": 4.5, "reviewCount": 10, "purchaseState": "AVAILABLE"
      }
    ]
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `items[].productId` | number | 상품 식별자(숫자 BIGINT, §2.6) |
| `items[].name` | string | 상품명 — **`productName`이 아니다**(I-18 `items[].productName`과 필드명이 다르다) |
| `items[].brandName`/`price`/`originalPrice`/`imageUrl`/`rating`/`reviewCount` | — | 표시 필드. **AI는 이 필드들을 쓰지 않는다** — SSE로 내보내지 않는다(경로 B) |
| `items[].purchaseState` | `"AVAILABLE"` \| `"SOLD_OUT"` \| `"HIDDEN"` | 구매 가능 상태(2026-08-05 M-4 개정, 구 boolean `purchasable` 대체 — `app/schemas/spring.py` `WishlistItem`, 커밋 `70247d0`). 둘이 겹치면 `HIDDEN` 우선(서버가 정해서 내린다) |

- **AI 실사용 필드는 `productId`·`name`·`purchaseState` 뿐**이다 — 나머지 표시 필드는 파싱하지 않는다(`app/schemas/spring.py`의 `WishlistItem`이 이 서브셋만 선언).
- **페이징 없음 — MVP 전량 반환**(I-18 전례). **찜 0건도 200 + `items: []`**(404 아님).
- **AI 동작**: "내가 뭐 찜했지?" 질의는 별도 SSE 이벤트 없이 `token` 텍스트로 답한다(I-18 동형, §3.1) — 조회라 새 `action`을 내지 않는다. **[AI 구현 완료 #386]** `wishlist_view` intent + `stream_wishlist_view` 로 구현했다(계약 개정 없음 — 이 절이 이미 규정한 동작을 뒤늦게 구현한 것이다). 조회 실패(`SpringUnavailableError`)도 `token` 안내 후 정상 `done` 이다 — `action.type` 유니온(§3.1)에 조회 실패 어휘가 없다.
- **AI 동작(`purchaseState`)**: `SOLD_OUT`/`HIDDEN` 항목은 **사유별로 갈라 안내한다** — 미수신(키 없음)은 "모름"으로 보고 안내하지 않는다. §4.9(I-18)와 같은 규칙이다(#310).
- 실패: `400 VALIDATION_ERROR`(신원 이상, 자원별 신규 code 미채택) · `401 INTERNAL_TOKEN_INVALID` · `403 AUTH_FORBIDDEN`(회원 전용) · `500 INTERNAL_ERROR`.

### 4.17 인기 상품 후보 조회 API (I-3 `GET /internal/products/popular`, query-time) — [사본 등재 v0.23.1, #162]

**[등재 경위 — 신설 협의가 아니다]** 이 엔드포인트는 **정본(Notion API 명세서 I-3)에 이미 등재돼 있고 BE 구현도 완료**(`InternalProductController#popular`)인데 **본 사본에만 누락**돼 있었다. 새 계약을 제안하는 절이 아니라 누락분을 정본에 맞추는 등재다.

**[왜 지금 필요한가 — AI 가 지키지 않고 있는 계약이 있다]** I-1(§4.6) 정본은 2026-07-27 개정에서 후보 수 상한을 폐지하며 그 전제를 이렇게 못박았다 — **"정형조건이 하나도 없는 요청은 LLM 단에서 차단하므로 BE 는 별도 가드를 두지 않는다."** 실패가 아닌 경우 표에도 *"모든 파라미터 생략 → 200 · 판매 중 전체. **서버 가드가 없다** — 정형조건 없는 요청 차단은 LLM 단 책임"* 으로 반복하고, 조건 0건 시 폴백 대상으로 **I-3 를 직접 지목**한다(*"LLM 은 조건을 완화해 재질의하거나 I-3(인기 상품)로 폴백한다"*).

**AI 는 그 차단을 구현하지 않았다.** 조건이 하나도 없는 발화("아무거나 추천해줘")는 `filters`·`category_legs` 가 모두 비어 `_search_query_params` 가 **파라미터 0개**를 만들고, 그대로 I-1 에 나가 매칭 전량(실측 **7,245건 · 13.33MB · 1.112s**, `docs/specs/MEASURE-I1-RESPONSE-132.md`)을 받는다. BE 가 "AI 가 막는다"를 전제로 상한을 없앤 자리에 차단이 없는 상태다. #162 가 이 구멍을 메우며 폴백 경로인 I-3 를 소비하므로 계약 우선 규칙상 등재를 선행한다.

#### AI → Spring 요청

```
GET {SPRING_BASE_URL}/internal/products/popular?size=30
X-Internal-Token: {서비스 토큰}
```

| 요청 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `size` | int | 아니오 | 반환 개수. **BE 기본값 12**. AI 는 config 주입값을 **항상 명시 전송**한다 |

- **⚠️ `size` 범위 검증이 BE 에 없다** — 정본 명시: *"범위 검증이 없다 — 구매자용 P-4 가 1~50 상한을 두는 것과 다르다. 내부 호출자만 쓰므로 가드를 생략했다."* 음수·0 을 보내면 **400 이 아니라 200 + 빈 배열**이 온다. 잘못된 값이 조용히 "인기 상품이 없음"으로 위장되므로 **양수 보장은 AI config 책임**이다.
- **신원 파라미터가 없다** — 인기 목록은 사용자 독립이라 회원·게스트가 같은 응답을 받는다. I-1·I-19 와 달리 AI 가 JWT `sub` 에서 도출해 실어 보낼 신원이 **대상 자체로 존재하지 않아** IDOR 표면이 없다(§2.6 신원 규약의 예외가 아니라 대상 부재).
- **[경계 — I-1 의 전량 반환 원칙과 무관하다]** I-3 의 `size` 는 후보를 잘라내는 상한이 아니라 **"인기 상위 N"이라는 목록의 정의 그 자체**다. I-1 이 상한을 폐지한 논거는 *"판매량순으로 자르면 의미 리랭킹과 직교한 기준이 정답 후보를 잘라낸다 — 후보 선별은 **정형조건이** 담당"* 인데, 이 경로에는 정형조건이 0개라 그 논거가 적용될 대상이 없다. **조건이 있는 턴은 종전대로 I-1 로 가고 후보 수 상한이 없다.**

#### AI 가 받는 응답 — **I-1(§4.6)과 동일 DTO**

envelope·항목 스키마가 I-1 과 같다(`{success, data:[...]}`). 정본 I-1 이 `options`·`optionCount` 를 설명하며 **"같은 DTO 를 쓰는 I-3 도 동일하게 나간다"** 고 직접 명시하므로, 필드 의미·표시/계산용 구분은 §4.6 응답표를 그대로 따른다(여기서 반복하지 않는다).

→ **파서(`_parse_search_response`)와 후속 파이프라인(최근구매 dedup·rerank·I-21 push·`products.ready`)을 그대로 재사용한다. 새 스키마·새 SSE 이벤트가 필요 없다.**

- **[AI 소비 규칙] 총액 예산만 말한 턴은 응답을 예산으로 거른다** — "총 5만원 있어 아무거나 추천해줘"처럼 `totalBudget` 은 있는데 무엇을 몇 개 살지가 없는 턴에서, I-3 응답 중 `price` 가 예산 이하인 상품만 남겨 **대안으로** 제시하고 대화로 되묻는다(`listType` 은 `PICK_ONE`, `totalBudget` 은 push 에 싣지 않는다). 세트로 묶지 않는 이유는 조합 기준이 없기 때문이다 — 니즈가 정해진 턴("감자탕 재료 총 5만원")과 달리 무엇을 몇 개 살지 사용자가 말하지 않아, 조합을 지어내면 서로 무관한 상품이 한 세트가 된다. **가격이 없는 후보(`price: null`)는 뺀다** — 예산 준수는 반증이 아니라 입증이 필요하다(§4.2 `verifiedSum` 규약과 같은 방향).

#### 실패 응답 — 3종뿐

| HTTP | code | 조건 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | `size` 가 숫자가 아님(`?size=abc`) — 타입 변환 실패 |
| 401 | `INTERNAL_TOKEN_INVALID` | `X-Internal-Token` 없음·불일치, 또는 **서버에 토큰 미설정(fail-closed)** |
| 500 | `INTERNAL_ERROR` | 서버 내부 오류(DB 장애 등) |

> **"그 외엔 실패하지 않는다"**(정본) — 이 API 는 "카드 영역을 비우지 않기 위한 폴백"이므로 자기가 또 실패하면 의미가 없다.

#### 0건은 성공이다 — degrade 로 처리하지 않는다

| 상황 | 응답 | AI 동작 |
|---|---|---|
| 판매 중 상품이 하나도 없음 | 200 · `data: []` | **빈 배열도 정상 결과**다. 카드 없이 텍스트만 답한다 |
| 집계 결과가 `size` 미달 | 200 · 있는 만큼 | 받은 만큼으로 진행한다 |
| 500 · 타임아웃 | — | 폴백이 실패한 경우다. 종전 무필터 I-1 검색으로 degrade 하고 `degradeReason` 을 남긴다(#136) — **스트림을 죽이지 않는다** |

- **타임아웃 3s** — AI→Spring 전 구간 규약(§2.9 c)과 동일.
- **재시도하지 않는다** — §2.9(c)의 재시도 1회는 **I-1 전용 예외**이며 그 예산은 이미 first-token 상한을 압박한다(#277·#288). 이 호출에 예외를 확대하지 않는다.

### 4.18 자사 주문 조회 API (I-29 `GET /internal/seller/{brandId}/orders`) — 🔶 초안, BE 협의 전 [v0.25.0 신설, #297]

> **🔶 제안(초안) — BE 협의 전. 확정 계약 아님.** 정본은 노션 "📡 API 명세서" DB의 I-29 행(2026-08-04 등재·협의사항 확정 반영)이며, 본 절은 그 사본이다. I-24~I-28과 동일 절차(이슈 협의 → 본 문서 개정 → 확정). ⚠️ §3.9(개인화 그래프)의 I-29와 번호가 충돌한다 — §3.9 쪽 재채번 제안(🔴 C-26, §3.9 노트 참조).

판매자 에이전트(order Q&A)의 **현재 상태 스냅샷** 조회다 — "신규 주문 뭐 있어?" 응답과 I-30 발송 대상(`orderItemId`) 해소 경로. **역할 분리: I-29 = 현재 상태, I-14(§4.4) = 전이 이력·집계.** S-2(FE 전용, SELLER JWT — AI 호출 불가)의 internal 판으로, S-2의 주문 단위 파생 규칙(대표 상태·자사 금액만 집계·`orderNo` 파생·수령인명 전문)을 상속하되 발송 대상 확정용 **자사 `items[]` 배열을 추가**한다.

```
GET /internal/seller/{brandId}/orders?status=ORDERED&limit=20
X-Internal-Token: {서비스 토큰}          ← internal 공통 인증(§2.3 b) · 타임아웃 3s(§2.9 c)
```

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `status` | — | S-2 탭 어휘: `ORDERED` \| `SHIPPING` \| `DELIVERED` \| `CLAIM` (생략 = 전체). 삭제된 `PREPARING`은 어휘 밖(2026-07-21 탭 삭제 확정) |
| `orderId` | — | 단건 직조회 |
| `from`·`to` | — (선택) | `YYYY-MM-DD`, 주문일 기준. **생략 시 전체 주문**(기간 무관, 현재 상태 기준 — 확정 2026-08-04) |
| `limit`·`offset` | — | 기본 `limit=20`, 최대 100 (I-9 어휘 — page/size 아님) |

**성공 200** — `data`: `tabCounts{ALL/ORDERED/SHIPPING/DELIVERED/CLAIM}`(전량 기준) + `rows[]` + `total`. `rows[]`: `orderId`(number)·`orderNo`·`orderedAt`·`recipientName`·`paymentMethod`·`myItemsAmount`(자사 금액만)·`status`(대표 상태, 파생)·`claimStatus`(null 가능)·**`items[]`**(자사 아이템만 — `orderItemId`(number)·`productId`·`name`·`optionName`·`quantity`·`price`·`status`(아이템 상태기계 어휘)·`activeClaimStatus`). **타사 아이템의 이름·금액 미노출, 자사 아이템 없는 주문 미노출**(S-2 정보 누출 방지 규칙 상속).

**실패**: `400 VALIDATION_ERROR`(`fields` 없음 — 숫자 아님/`status` 어휘 밖/`limit` 1~100 밖) · `400 INVALID_PERIOD`(형식·역전 — I-6/I-13/I-14 공통 code) · `401 INTERNAL_TOKEN_INVALID` · `404 BRAND_NOT_FOUND` · `500 INTERNAL_ERROR`. **403 없음**(2026-07-28 정리 상속).

- **자사 주문 0건도 200 + 빈 `rows`·`total: 0`·`tabCounts` 전부 0** — "주문 없음"은 정상 결과(I-14 규칙).
- **`orderId` 직조회에서 타사·미존재는 404가 아니라 200 + 빈 `rows`** 로 존재 은닉(확정 2026-08-04) — 에이전트는 "해당 주문이 없습니다"로 안내.
- AI 구현: `SpringClient.get_orders` + 툴 `get_orders`(general 레인 조회 + product 레인 ship 대상 해소).

### 4.19 주문 아이템 발송 처리 API (I-30 `PATCH /internal/seller/{brandId}/order-items/{orderItemId}/status`) — 🔶 초안, BE 협의 전 [v0.25.0 신설, #297]

> **🔶 제안(초안) — BE 협의 전.** 정본은 노션 I-30 행. **쓰기 — 그래프 HITL `interrupt` 승인 후에만 실행**(I-12와 동일 등급). S-4 연동은 §3.2 `draft` op=`ship` 참조.

**아이템 단위** 상태 전이다 — 상태는 `order_item.status`에 있고(S-2 대표 상태는 파생값) 한 주문에 타사 아이템이 혼재한다. bulk 없음(복수 발송은 반복 호출, C-4·I-24 방식). **MVP 허용 전이는 `ORDERED→SHIPPING` 하나뿐** — `DELIVERED`·`CONFIRMED`는 시스템 모의 전이(I-14), 취소·반품 전이는 자동 승인 스케줄러(AD-5) 소관. `PREPARING`·`SHIPPED`는 상태기계 어휘가 아니다(2026-07-18 정정). **발송 후 취소·역전이 불가(확정 2026-08-04)** — `SHIPPING` 이후 이 API로 어떤 역전이도 불가(`400 ORDER_INVALID_TRANSITION`), 구매자 구제는 반품(O-5)만.

```
PATCH /internal/seller/12/order-items/5551/status
X-Internal-Token: {서비스 토큰}
Content-Type: application/json

{ "toStatus": "SHIPPING", "reason": null }
```

- Body: `toStatus`(필수 — MVP 유효값 `SHIPPING`뿐, 확장 시 값만 추가) / `reason`(선택 — `order_status_logs.reason` 기록).
- `brandId`는 티켓 claim에서 오지만 신뢰하지 않는다 — **실행 시점 소유권 재검증**(I-11 규칙). 타임아웃 3s.

**성공 200** — `data`: `{orderItemId, fromStatus: "ORDERED", toStatus: "SHIPPING", changedAt}`. Spring은 `order_status_logs(ORDERED→SHIPPING, actorType=SELLER, reason)` 1행을 기록해 **I-14 분석 데이터에 자동 합류**시킨다. 주문 대표 상태는 파생값이라 건드릴 것이 없다 — FE 갱신은 S-4 `done(panel:"refresh")`.

| HTTP | code | 조건 |
|---|---|---|
| 400 | `VALIDATION_ERROR`(`fields` 없음) | 숫자 아님 / body JSON 오류 / `toStatus` 어휘 밖(**`PREPARING`·`SHIPPED` 포함**) |
| 400 | `ORDER_INVALID_TRANSITION` | 허용 전이 아님 — 현재 상태가 `ORDERED` 아님(**활성 클레임 `CANCEL_REQUESTED` 포함**), MVP 미허용 전이, **발송 후 역전이·취소 일체(확정 2026-08-04)** |
| 401 | `INTERNAL_TOKEN_INVALID` | 토큰 없음·불일치·서버 미설정(fail-closed) |
| 404 | `ORDER_ITEM_NOT_FOUND` | 없는 `orderItemId` **또는 타 브랜드 아이템** — 403이 아니라 404 존재 은닉(I-11 규칙) |
| 409 | `ORDER_ALREADY_SHIPPED` | 이미 `SHIPPING` — **멱등 200 금지**(I-12 `ALREADY_HIDDEN` 논리: "이미 된 일"과 "방금 한 일" 구분). **2026-08-05 개명 — 구 `ALREADY_SHIPPED`(공통 규약 `<도메인>_<사유>` 형식 준수). AI는 과도기 양쪽 코드를 수용** |
| 500 | `INTERNAL_ERROR` | 전이 미반영 — **에이전트 성공 보고 금지**(I-11·I-12 규칙), draft 유지·재confirm 가능 |

- 신규 코드 `ORDER_ALREADY_SHIPPED`·`ORDER_ITEM_NOT_FOUND`는 BE 확정 후 공통 규약(레포 `04-api-spec.md` §10 → 노션 규약 페이지) 등재 필요(규약 §4 절차).
- AI 구현: `SpringClient.update_order_item_status`(코드별 전용 예외 매핑) + HITL `op="ship"`(`hitl._execute_draft`) + 툴 `update_order_status`(ORDER_WRITE_TOOLS — 어떤 에이전트에도 미바인딩, 구조적 HITL 보장).

### 4.20 자사 상품 리뷰 조회 API (I-31 `GET /internal/seller/{brandId}/reviews`) — 🔶 초안, BE 협의 전 [v0.25.0 신설, #297]

> **🔶 제안(초안) — BE 협의 전.** 정본은 노션 I-31 행. 읽기 전용 — HITL 불필요, S-4 analysis 레인(review 워커) + general 레인 단순 조회.

**`status=VISIBLE` 리뷰만 반환**한다 — 구매자 노출(P-3)과 동일한 진실. 신고로 숨겨진 리뷰를 에이전트가 인용하면 사고다(숨김 리뷰 열람은 admin AD-3 소관, 숨김 카운트도 미노출). **AI DB에 리뷰 원문을 저장하지 않는다** — 질의 시점 조회(I-19 원칙). 리뷰 답글은 스코프 제외(확정 2026-08-04).

```
GET /internal/seller/12/reviews?from=2026-07-01&to=2026-07-31&productId=3&rating=1,2&sort=latest&limit=20&offset=0
X-Internal-Token: {서비스 토큰}          ← 타임아웃 3s
```

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `from`·`to` | — (선택) | `YYYY-MM-DD`, `review.created_at` 기준. **생략 시 기본 최근 7일**(`to`=오늘, `from`=6일 전 — 확정 2026-08-04, **누락은 `INVALID_PERIOD` 아님**) |
| `productId` | — | 자사 상품으로 한정. 타사/미존재 → 404(존재 은닉) |
| `rating` | — | 1~5 복수 선택(콤마 CSV) — "별점 1~2점만" 대응 |
| `sort` | — | `latest`(기본) \| `rating`(**낮은 순 고정** — 문제 파악용, 확정 2026-08-04. asc/desc 분리 없음 — 높은 별점은 `rating=4,5` 필터로) |
| `limit`·`offset` | — | 기본 `limit=20`, 최대 100 |
| `stats` | — | `true`면 목록 대신 집계만 |

**성공 200(목록)** — `data`: `rows[]`(`reviewId`·`productId`·`productName`·`rating`·`content`·`authorNickname`(P-3 공개 정보)·`createdAt`) + `total`. **성공 200(`stats=true`)** — `data`: `totalCount` / `averageRating`(**0건이면 `null` — 0 아님**, I-16 `churnRate` 규칙) / `distribution`(P-3 형태) / `byProduct[]`(`productId`·`productName`·`count`(내림차순)·`averageRating`(소수 1자리 🔶)).

**실패**: `400 INVALID_PERIOD`(형식·역전만 — 누락 아님) · `400 VALIDATION_ERROR`(`fields` 없음) · `401 INTERNAL_TOKEN_INVALID` · `404 BRAND_NOT_FOUND` · `404 PRODUCT_NOT_FOUND`(타사·미존재 존재 은닉) · `500 INTERNAL_ERROR`. **403 없음.** **리뷰 0건(전부 숨김 포함)도 200 + 빈 `rows`·`total: 0`** — 정상 결과.

- AI 구현: `SpringClient.get_reviews`/`get_review_stats` + 툴 `get_reviews`(stats 파라미터 통합) — general 레인(단순 조회) + analysis `review` 워커(요약·진단, 워커 6종째).

---

## 5. 협의 필요 항목 요약표 (🔴 Consolidated Open Items)

Spring/FE 팀과 확정이 필요한 항목을 통합한다. 각 항목은 본 문서에서 **제안(초안)** 또는 **확정안 반영(수용 전 🔴)** 으로 제시된다.

**[v0.6.0] 착수 전 필수(최우선)**: **C-15(`POST /products/search` 후보 검색 — 최우선, 유일 후보 경로)** · C-6(구매 이력 조회) · C-3(장바구니 담기 I-2 잔여) + C-16(장바구니 조회) · C-13(I-6 집계) · C-1 잔여(role 값·TTL).

| # | 항목 | 현재 상태 | 소유/근거 | 상태 |
|---|---|---|---|---|
| C-1 | **인증(auth)** | **확정**: RS256 + JWKS(Spring JWKS 노출, AI 로컬 검증·kid miss refetch), `401` 통일, `/seller/chat` role=seller(403)(§2.3). **[v0.10.0] SSE = 스트림 단명 티켓**(로그인 AT 아님, CH-1 발급) — 클레임 `sub`+`sub_type`(member/guest)+`iss`+`aud`+`scope`+`exp`, 검증 signature/exp/iss/aud/scope. **[v0.8.0·확정] `brandId` = 판매자 티켓 클레임**(body 금지, `{brandId}` path용 — userId와 동일 IDOR 원칙, §2.6). **[v0.15.20·BE 코드 실측 확정]** `iss`=`jarvis-spring-auth` · `aud`=`jarvis-fastapi-ai` · `scope`=`chat:stream` · 티켓 TTL **60초** · 판매자 `role="seller"`(**소문자**)+`brandId`(숫자, 판매자 티켓에만) · **CH-1b `POST /api/chat/tickets` 구현 완료**(요청 `{sessionId}`, 세션 소유자 검증, 세션 TTL 동시 갱신) · CH-6 `POST /api/chat/seller/sessions` 신설 | 결정 19 / BE 실측(2026-07-27) | ✅ 해소 — 🔴 잔여는 **서비스 토큰(`X-Internal-Token`) 회전 주체·만료 정책·mTLS 병용 여부**(운영 정책, 현재 단일 공유 시크릿) |
| C-2 | **스트림 전 오류 봉투** | **확정안 반영**: `error.{code,message,requestId}` + 상태 매핑(`400`/`401`/`403`/`429`)(§2.5) | 본 문서 제안 | 🔴 Spring 수용 전 |
| C-3 | **[v0.6.0 재작성] 장바구니 담기 API(I-2)** | **BE 문서 채택**: `POST /internal/cart/items` 단건 + `X-Internal-Token` + 본문 신원(JWT `sub` 유래) + `optionId` + quantity 1~99 합산 + **게스트 담기 허용**. 옵션 되물음 멀티턴(`CART_OPTION_REQUIRED` options 목록)(§4.1) | BE I-2 문서 / 결정 7 / 결정 8 개정(§8 항목 7) | 🟢 **[재개정 v0.15.16] 담기 재고검증 있음** — BE `CART_STOCK_INSUFFICIENT`+`availableStock`(2026-07-22) → `STOCK_INSUFFICIENT`(`OUT_OF_STOCK` 폐기 유지). 🟢 **[해소 v0.15.8] options 스키마**(BE 2026-07-18) — `error.detail.options:[{optionId,name,extraPrice}]`. 🔴 잔여 — 서비스 토큰 발급만 |
| C-4 | **[BE 확정 2026-07-18] AI 생성물 갱신 배치 = I-17** | `GET /internal/products/changes`(§4.8, BE "상품 정보 Batch") — 골격 확정: `X-Internal-Token`·envelope `{success,data}`·숫자 `productId`·오류(INVALID_CURSOR/INTERNAL_TOKEN_INVALID/FORBIDDEN)·`since="0"` 초기구축·`hasMore` 루프. 상품 원본 사본 없음 | I-17 / BE Notion | 🟡 잔여 3건 저영향(커서 형식=opaque·`attributes` 스키마·리뷰 포함). 주기=AI config·페이지=`limit`(기본 500). **스코프(MVP?)는 스키마 아님 — 별건** |
| C-5 | **[해소 v0.15.5] `productId` 타입 & `attributes`** | 원본 id = 숫자(BIGINT). **[해소] `attributes` 구조 확정** — DDL `product.attributes` JSON, **축 = `category.attribute_schema`(키 배열), 값 자유텍스트**(D7·D11). 2차 압축 속성 매칭 대상 | 결정 9-B / DDL D7·D11 | ✅ **해소**(타입·attributes 구조 모두 확정) |
| C-6 | **[정정 v0.15.5] 구매 이력 = I-19** | `GET /internal/members/{id}/orders`(§4.7). camelCase·숫자 id(DDL)·`shippingFee` 0. **`status` = 6종**(`PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED`, Notion I-19). **`categoryName` 포함**(BE 확정 2026-07-19 — 카테고리 억제·productId dedup 모두 가능) | I-19 / Notion·DDL | 🟢 확정(status·타입·**categoryName BE 확정 2026-07-19**). 🔴 잔여 — Notion 페이지 stale BE 통보 |
| C-7 | **판매자 판매 데이터 소스** | **[해소]** 원천 = **Spring 집계 API(I-6) 질의 시점 콜백**(§3.2·§4.4). 구 기본안(주문 미러 sellerId·금액 확장) 폐기 | 결정 20 개정/Batch 1 | ✅ **해소** — 계약 세부는 C-13으로 이관 |
| C-8 | **[해소 v0.15.19] 세션 종료 통지 = I-20** | `POST /events/session-end` `{sessionId,userId(number BIGINT),reason?}` + `X-Internal-Token`. UUID 포함 불투명 sessionId, reason 최대 64자, 파생 멱등키, 202 `accepted`/`duplicate`(§3.5) | 이슈 #62/#79 / Spring PR #24 | 🟢 계약 확정 — **[v0.16.0]** Spring 알려진 reason=`logout` **1종**(`newConversation` 제거 — 새 대화는 threadId만 갱신, §2.6), AI 내부 10분 비활동 종료, enum 미강제 |
| C-9 | **[BE 신설 07/17] 추천 push = I-21** | `POST /internal/recommendations` `{sessionId, recommendationRequestId, listType, totalBudget?, lists[{listId, label?, productIds[≤9 숫자], reasons[{productId,reason}]}]}`(§4.2). **listId=FastAPI 생성(UUID급 무작위 ≥128bit)**, **reason=콜백 포함(v0.15.15 확정, BE 구현 07-18)**, **다중 목록·멱등 키(recommendationRequestId, listId)·TTL 10분(v0.17.1 확정)**, 콜백 성공 후 products.ready. 구 groups 구조·추측 가능한 listId·평평한 단일 목록 폐기 | I-21 / BE DB | 🟢 전부 확정 (잔여 없음) |
| C-10 | **식별자 = 토큰 클레임** | **확정(숫자 사용자 id)**: 사용자/게스트/판매자 = 숫자 id, JWT `sub`에 문자열화. `role` enum 구분(§2.6). **양팀 통보 필요** | 결정 8/19 / 2026-07-14 세션 확정 | 🔴 미확정 — 클레임 키·id 타입 세부 |
| C-11 | **[v0.7.0 축소] CORS 허용 오리진** | 레이트 리밋은 **확정**(FastAPI 미들웨어 + in-memory, 분당 10/시간당 100 config, §2.8) — 협의 잔여는 **FE 허용 오리진 목록**뿐 | 결정 19 / v0.7.0 확정 | 🔴 잔여 — 허용 오리진(FE 통보) |
| C-12 | **[BE 신설 07/17] 카드 조회 = CH-5** | `GET /api/chat/lists/{listId}`(§4.3, 구 P-7 대체) — Spring이 표시 필드 enrich·서빙, FE↔Spring. AI 미관여 | CH-5 / BE DB | 🔴 카드 응답 스키마(FE↔Spring 소유, LLM 사안 아님) |
| C-13 | **[재정의 v0.8.0] 판매자 집계 API 5종** | BE 문서 채택 — `GET /internal/seller/{brandId}/{sales\|funnel\|events\|churn}` + 전역 `/internal/account-events`. `X-Internal-Token`, `brandId`=JWT 클레임(§2.6). 구 단일 `/seller/aggregates` 제안 폐기. 통계 답변 원천(§4.4) | BE 문서 / #9 | 🔴 **최우선** — 응답 스키마(**I-13은 LLM팀 재작성 반영 완료**, 나머지 4종 잔여)·전역 I-8 admin 소유·I-number 정합 |
| C-14 | **[재정의 v0.9.0] 판매자 상품 CRUD API 4종** | BE 문서 채택 — I-9 목록/I-10 등록/I-11 수정/I-12 삭제(soft,HITL). `internal`·`X-Internal-Token`·`{brandId}`. **AI 직접 쓰기**(구 "I-7 읽기 + FE S-3 PATCH" 폐기), 쓰기는 HITL 승인(§3.2·§4.5) | BE 문서 / #9 | 🔴 미확정 — 응답 스키마·attributes 스키마·HITL 이벤트 계약 |
| C-15 | **후보 검색 위임 = I-1** | **[확정 v0.15.5] GET 그대로 수용**(사용자 지시). BE Notion 파라미터 채택(`keyword·categoryName·minPrice·maxPrice·brandName·size≤30`). **dedup·평점·정렬은 AI 사후필터**(BE I-1에 해당 파라미터 없음). 응답 = `{success,data:{items[...]}}`(§4.6) | I-1 / BE Notion·DDL | 🟢 **확정**(GET·파라미터·응답). 🟢 **[#113 해소] `totalCount` 미제공 → estCount 소스** — BE 추가 없이 해결했다. `size` 제거(2026-07-23 합의)로 I-1이 **매칭 전량을 반환**하므로, 완화된 필터로 **재검색(probe)** 하면 그 응답 길이가 곧 전체 매칭 수다(별도 count API 불필요). 단 이 정확성은 **전량 반환이 전제** — BE가 반환 상한을 다시 넣으면 estCount는 오류 없이 상한값으로 고정된다. fan-out 턴은 `category_fanout_merge_cap` 절단 뒤라 그 상한을 넘지 않는다 |
| C-16 | **장바구니 조회 = I-18** | BE 실측 `GET /internal/cart`(§4.9, "챗봇 장바구니 조회"), 서비스 토큰. 질의 응답 + 담기 시 기존 보유·합산 안내. `productName`/`optionName` 포함 필요(챗 답변 생성용) | I-18 / BE DB | 🟢 **[해소 2026-07-18] `productName`/`optionName` 필수 포함 · `CART_QUERY_INVALID`(400) BE I-18 확정** |
| C-17 | **[신규 2026-07-20, 기각 2026-08-03] 방식1용 id 제약 조회** | 원 요청: §4.8 방식1(AI 벡터→Spring hydrate)용 I-1 `productIds` 필터 또는 by-id 조회. #32에서 방식2를 확정했고, C-17은 방식1의 가격 하한·부정어 등 구조적 제약 실패를 고치지 못해 요청을 철회한다. BE 구현 불필요, 방식1은 오프라인 비교 전용으로 존치 | I-1 / #32 사용자 결정 | ❌ **기각** — 방식1 0/26, 와이어 계약 불변; BE에 철회 통보 |
| C-18 | **[신규 v0.18.0] I-22 `catalogVersion` 값 생성 주체** | 정본 I-22(§3.7)는 `catalogVersion`을 **Spring이 요청에 실어 보내는** 필수 필드로 규정하는데, 같은 문서가 *"FastAPI는 **자체** 카탈로그 인덱스(I-17로 동기화된 임베딩)로 순위를 매긴다"* 고 한다. **Spring은 AI의 인덱스 버전을 알 수 없다** — 보낼 수 있는 건 Spring 자기 카탈로그의 버전뿐이라, 그 값으로 캐시를 키잉하면 **AI 인덱스가 갱신돼도 캐시가 무효화되지 않는다**(P-5 캐시 키가 "회원 id + 카탈로그 버전 + 알고리즘 버전"이라 직격, §4.11). **[v0.19.0 결론 — 이관이 아니라 폐기]** AI 생성으로 옮겨 구현해봤으나 되돌렸다. **어느 주체가 만들어도 이 필드는 약속을 지킬 수 없다.** ① *재현* — `products`는 I-17이 제자리 upsert하므로 그 시점 임베딩이 남지 않고, 버전 라벨이 가리키는 인덱스 상태가 이미 사라져 있다. 스냅샷을 남기려면 7,220×1536 기준 버전당 약 44MB를 5분 주기로 쌓아야 해 성립하지 않는다. ② *재현이 필요하지도 않다* — 산출물(목록·`reason`)은 Spring이 `recommendation_generated`로 이미 저장한다(§3.7). ③ *캐시 무효화* — TTL 10분과 중복이고, `max(updated_at)` 기반 지문은 상품 1건 갱신으로 **전 회원 캐시를 동시에 날려** 오히려 캐시를 죽인다. → **계약에서 제거 제안.** AI는 선택 필드로 받아만 두고 버린다(Spring 무변경) | I-22 / 이슈 #148 | 🔴 **BE 협의 — 필드 폐기 제안.** 함께 확정할 것: (1) 정본 예시 `catalog-20260728T0300Z`가 암시하는 "Spring 야간 스냅샷" 해석이 맞는지, (2) `recommendation_generated`에 이 값이 저장되는지(저장 안 되면 폐기가 확정적) |
| C-19 | **[신규 v0.19.0] I-22 요청 상한 2건 — BE 통보·합의** | AI측 방어로 넣은 상한이 계약에 없던 것을 v0.19.0에 등재했다. (1) **`limit` ≤ 60** — overfetch가 응답 크기·조회 비용을 함께 부풀리는 것을 막고, config `home_reco_max_items`와 단일 출처로 묶어 `want < limit`이 성립할 수 없게 했다. (2) **`signals` 각 배열 길이 ≤ 200, 항목은 양의 BIGINT** — 상한이 없으면 요청당 AI 인덱스 조회 비용에 상한이 없고, BIGINT 밖 값은 DB 경계에서 터진다. 둘 다 초과 시 `400 BAD_REQUEST`다 | I-22 / 이슈 #148 | 🔴 **BE 통보 필요** — (1) Spring이 실제로 보내는 `limit`·신호 배열 크기가 이 상한 안인지, (2) **`recentlyViewedProductIds`가 최신순(index 0 = 가장 최근)으로 정렬돼 오는지** 확인해야 한다. AI는 배열 인덱스로 recency decay를 걸므로 순서 보장이 없으면 가중치가 조용히 뒤집힌다. 상한을 넘는다면 올리거나(설계상 여유 있음) BE가 잘라 보내야 한다 |
| C-20 | **[신규 v0.22.0 · 개정 v0.26.0] 개인화 그래프 인증 레인 = Spring proxy (조회 포함)** | **조회(§3.8)·변경(§3.9) 모두** FE→Spring→AI internal(`X-Internal-Token`)이다. v0.22.0은 조회만 FE→AI 직접(`chat:stream` 티켓 재사용)으로 두었으나, **마이페이지에는 채팅 세션이 없어 티켓을 발급받을 수 없다**(CH-1b는 `sessionId` 필수) — 재사용할 자산이 없어 전제가 무너졌다. 전용 `profile:read`/`profile:write` 티켓 신설안은 **여전히 기각** — `scope`가 exact `chat:stream`으로 하드 고정된 검증 경로를 `/chat`·`/seller/chat`이 함께 지나가므로 채팅 인증에 회귀 위험을 만드는 대가를 치른다(§2.3 a) | §3.8·§3.9 / 이슈 #149·#322 | 🔴 **착수 전 필수(#150 최우선 차단)** — (1) Spring이 FE 대면 엔드포인트(`M-11`~`M-16`)를 **호스팅할 것인지**, (2) 경로 `{userId}`를 **자기 로그인 세션에서만** 도출한다는 확약, (3) **이 6개 경로**(v0.26.0: 5→6, 조회 합류)에 사용자 JWT를 포워딩하지 않는다는 확약, (4) **[v0.26.0 신규] 비로그인 게스트에게 §3.8을 노출하지 않는다는 확약** — 노출해야 한다면 `guest.id`가 UUID 문자열이라 BIGINT `{userId}`로 표현할 수 없어 계약을 다시 열어야 한다 |
| C-21 | **[신규 v0.22.0 · 갱신 v0.26.0] `If-Match`/`graphVersion` 통과 규약** | 요청은 `If-Match` 헤더(정규), 성공 응답은 본문 `graphVersion`(정규) + §3.8 `ETag`(편의 사본), **`409` 응답은 `error.detail.graphVersion`**(v0.26.0 위치 정정 — 구 계약은 봉투 밖 형제). 값은 **불투명**이며 파싱·순서 비교 금지 | §2.5·§3.8·§3.9 / 이슈 #149·#322 | 🔴 **차단** — (1) Spring이 FE→Spring 요청의 값을 **변형 없이**(재따옴표·재인용·자체 생성 금지) AI에 전달하는지, (2) `409` 본문의 **`error` 객체를 통째로** FE에 전달하는지 — 🟢 **v0.26.0에서 난이도 하락**: `graphVersion`이 `error.detail` 안으로 들어와 필드를 봉투 안팎으로 옮겨 담는 재구성이 불필요해졌다, (3) FE가 `Access-Control-Expose-Headers: ETag` 없이 본문 필드만으로 동작하는지(C-11 연계) |
| C-22 | **[신규 v0.22.0] 충돌 상태코드 `409` 수용(`412` 미채택)** | `409 PROFILE_VERSION_CONFLICT`. 근거는 §2.5 말미 — 409에 이미 상태 인식 불일치 3형제가 있고, 412는 상태→코드 매핑에 없어 기본값이 일반 코드로 나간다 | §2.5·§3.9 / 이슈 #149 | 🔴 — (1) BE/FE가 RFC 관례상의 412 대신 409 수용에 동의, (2) 게이트웨이·프록시가 409를 재작성하지 않음 확인 |
| C-23 | **[신규 v0.22.0 · 개정 v0.26.0] 전체 초기화(§3.9.4)의 파괴 범위** | 그래프·fact·요약(임베딩 포함)·tombstone·미처리 버퍼·게이트 상태 **+ 대화 전사록(`conversation_turns`, 해당 `userId` 행 전체)** 를 물리 삭제하고, **변경 감사 로그만 보존**한다. FE 문구 후보는 **"대화 기록을 포함한 개인화 데이터 초기화"**(최종 문구는 C-25) | §3.9.4·§6.3 (c) / 이슈 #149·#322 | 🟢 **(1) 해소(2026-08-06, #322)** — "프로필 초기화가 대화 전사록 삭제까지 포함하는가" = **포함한다.** 근거는 REQ-PROF-034 가 이미 채택한 논거의 연장(금지 대상은 기계가 조용히 지우는 것이고 사용자 자신의 삭제권은 별개 — 예외가 아니라 적용 범위 한정)이며, 이로써 `SPEC-PROFILE-GRAPH-149` OPEN-G6 가 해소된다. 선결 확인이던 "Spring 채팅 이력 사본 유무"도 해소 — **사본 없음, AI 단독 완결**. 🔴 **잔여(정책·법무)** — (2) 감사 로그 보존 기간, (3) 개인정보 삭제 요청 경로와의 관계, (4) 전사록 **자연 만료 TTL** 자체(`SPEC-PROFILE-001` OPEN-P5, 본 이슈 범위 밖 — 초기화 시 삭제와 별개 트리거다) |
| C-24 | **[신규 v0.22.0] 민감 카테고리 목록 소유·억제 단계** | 계약은 **불변식만** 규정한다(node·edge 미포함, 어떤 카운트에도 미포함, placeholder 없음, 주제 라벨 와이어 금지 — §3.8 규약). 목록 자체는 AI 측 상수/설정으로 주입 | §3.8 / 이슈 #149 | 🔴 — (1) 목록 확정 주체(기획·법무), (2) 억제가 **노출 단계만**인지 **수집 단계까지**인지 — 후자면 `SPEC-PROFILE-001` 추출 절 개정이 추가로 필요하다. **주제 판정은 완전하지 않다는 점을 전제로 합의해야 한다**(하드 PII는 결정론적, 주제는 best-effort) |
| C-25 | **[신규 v0.22.0 · 갱신 v0.26.0] 그래프 FE 표현 계약** | `confidence` 3버킷(`LOW`/`MEDIUM`/`HIGH`)**만** — **[v0.26.0] `evidenceCount`는 와이어에서 제거**(`profile_buffer_repeat_cap`이 관측 횟수를 2로 잘라 정확한 수를 셀 수 없다). 삭제 항목은 `?includeSuppressed=true`(undo 창 이내분만), `challenged`는 상태 변경 없는 힌트 | §3.8·§3.9.2 / 이슈 #149·#322 | 🟡 **FE 소유**(C-12류) — (1) 버킷 라벨 문구, (2) **[v0.26.0 신규] undo 창 잔여 시간 표시 여부** — 창 길이는 서버 config이며 와이어에 없으므로(§3.9.2) 표시하려면 별도 합의가 필요하다, (3) "왜 이 취향이 있나요" 문장을 요구할지 — 요구하면 근거 원문 금지 [HARD] 때문에 **생성·redaction된 문장**이 되어 별도 계약이 필요하다, (4) **[v0.26.0 신규] 전체 초기화 확인 다이얼로그 문구** — 개별 삭제는 확인 없이 즉시+undo, 확인 필수는 초기화뿐이다(§3.9.2) |
| C-26 | **[신규 v0.22.0 · 재채번 v0.26.0] I-번호 채번 I-32~I-37** | **I-32 조회(§3.8) / I-33 수정 / I-34 삭제 / I-35 복구 / I-36 초기화 / I-37 개인화 중지.** 구 채번 I-29~I-33은 **실제로 충돌했다** — I-29~I-31이 판매자 주문·리뷰(§4.18~§4.20, #297)에 먼저 배정돼 있었다. v0.25.0이 제안한 `I-34~I-38`은 조회를 세지 않은 5종 기준이라 폐기하고, 정본(노션 등재 2026-08-05) 6종 채번을 따른다 | §3.8·§3.9 / 이슈 #149·#297·#322 | 🟢 **AI측 확정** — 낮은 번호 추측이 충돌을 만든다는 경고가 실증됐고(#285 선례에 이어 두 번째), 정본 DB 등재로 해소. 🔴 **잔여** — BE가 I-32~I-37을 최종 수용하는지, 그리고 Spring 대면 `M-11`~`M-16`(Spring 소유, 본 문서 계약 대상 아님) 채번을 확정하는지 |
| C-27 | **[신규 v0.22.0] 개인화 중지 시 Spring 캐시 무효화** | AI는 자기 투영·요약·랭킹 경로를 즉시 반영하고 I-22를 `NO_PROFILE`로 전환한다(§3.7 규약) | §3.9.5·§4.11 / 이슈 #149 | 🔴 **차단** — **P-5 개인화 결과 캐시(10분)는 Spring 소유라 AI가 비울 수 없다.** Spring이 중지 시점에 해당 회원 캐시를 무효화해야 "즉시"가 성립하며, 하지 않으면 최대 10분간 개인화 홈이 유지된다. AI 단독으로 달성 불가한 유일한 항목이다 |
| C-28 | **[신규 v0.22.0] 브랜드 통제 어휘 부재** | `brand` 노드는 통제 어휘에 스냅돼야 `verified: true`가 된다(§3.8). 카테고리는 카탈로그 잎 이름(§4.6)이 어휘 역할을 하지만 **브랜드는 그에 상응하는 목록이 없다** — I-19(§4.7)에도 `brandName`이 없다 | §3.8 / 이슈 #149 | 🔴 **차단** — BE가 브랜드 목록을 노출할지(신규 계약), 아니면 AI가 I-1 응답에서 관찰 사전을 축적할지 확정해야 한다. 후자는 요청 경로를 쓰기 주체로 만드는 부작용이 있다. **사용자가 가장 통제하고 싶어하는 축이라(“이 브랜드 싫어”) 미해결 시 그래프의 실효성이 크게 줄어든다** |

> 참고(v0.5.0): **C-15 신설**(후보 검색 — 유일 경로·최우선). **C-4 폐기**(카탈로그 동기화 자체 없음). **C-6 재정의**(주문 알림/미러 → 질의 시점 구매 이력 조회 §4.7). **C-7 해소**(I-6 콜백, 세부는 C-13). C-1/C-2/C-3은 확정안 반영이나 Spring 수용 전까지 🔴 잔여를 유지한다.
> 참고(v0.6.0): **C-3 재작성**(BE I-2 문서 채택 — 구 JWT 포워딩/`items[]` 제안 폐기, 게스트 담기 허용). **C-16 신설**(장바구니 조회).

### 5.1 07/17 BE 확인질문 — LLM팀 답변 필요 (Q1~Q9)

BE "API·ERD 변경 정리(07/17)" Part 2가 **우리(LLM팀)에게 확정을 요청**한 9건. `우리 답(제안)`은 대부분 **기존 계약과 일치**해 즉답 가능, 일부만 팀 결정 필요.

| Q | 질문 | 우리 답(제안) | 상태 |
|---|---|---|---|
| Q1 | 세션 종료 sessionId **UUID 수용**? | ✅ **수용 완료** — AI는 정규식 없이 config 길이 상한만 적용해 UUID 포함 불투명 문자열을 받음(v0.15.17) | 확정 |
| Q2 | **I-21** 스키마·`listId` TTL·`reason` 전달 | ✅ 스키마 수용. listId=우리 생성(**UUID급 무작위 ≥128bit**)·TTL 10분(config). **reason을 I-21 콜백에 포함→CH-5로 전달**(§4.2·§4.3, BE의 SSE안 대신) — **BE 구현 확정 2026-07-18 🟢**(v0.15.15) | 형식·reason 확정, TTL 제안 |
| Q3 | **[적용]** = `{action:"confirm", draftId}` 확정? | ✅ **예** — §3.2 HITL 설계와 동일 | 즉답 |
| Q4 | **I-17** 커서·`attributes`·리뷰 텍스트 | 🟡 BE 골격 확정(2026-07-18): 인증·envelope·숫자 id·오류코드. 잔여 3건 저영향(커서 opaque·attributes 자유 dict·리뷰 MVP 제외, §4.8). 🔴 선결: I-17 배치 MVP/post-MVP 스코프 | BE 확정 |
| Q5 | **I-13** 본문 재작성(I-5 내용 복붙이던 것) | ✅ **LLM팀이 직접 재작성해 Notion 반영**(§4.4 I-13, v0.15.1) — BE 검토만 | 해소 |
| Q6 | **CH-3**(CS 챗) 라우팅 | ✅ **관리자 CS 문의(CH-3·I-5·AD-1/2·M-9) 전부 post-MVP**. **주문상태 Q&A(I-4)는 구매자 챗(CH-2)의 `order_status`로 구현**(§4.10) — 별도 CS챗 없음 | 해소 |
| Q7 | 게스트 담기 실패 **3종·차단 없음** 최종? | ✅ GUEST_NOT_ALLOWED 폐기(§3.1·§4.1). **[갱신 v0.15.16] 실패 3종**(PRODUCT_NOT_FOUND/STOCK_INSUFFICIENT/CART_ERROR) — 담기 재고검증 부활(`CART_STOCK_INSUFFICIENT`, 2026-07-22), `OUT_OF_STOCK` 폐기 유지 | 갱신 |
| Q8 | 판매자 챗 주소 `{AI_SERVER}/seller/chat`(별도) vs `/chat` 채널 | `{AI_SERVER}/seller/chat`(S-4, 별도 주소) 최종 — 채널 구분 아님(§3.2) | 즉답 |
| Q9 | 챗봇 담기 `add_to_cart` 이벤트 누가 쏘나 | **[정정 v0.15.26] FE가 쏜다** — E-1 정본에서 `add_to_cart`는 FE 12종 중 하나이고, 서버 직접 적재는 `recommendation_generated` 하나뿐이다. 구 답변("BE가 `CART_ADD(via:chat)` 적재")은 폐기 | 갱신 |

> **[v0.15.17 갱신]** Q1(I-20 UUID)·Q2(I-21 reason)·Q5(I-13 재작성)·Q6(관리자 문의 MVP 제외)은 **해소**. **Q4 I-17 스키마 골격은 BE 확정(2026-07-18)** — 잔여는 스키마가 아닌 🔴 선결 1건 **배치 MVP/post-MVP 스코프**(config vs 파이프라인 모순). 나머지(Q3·Q7·Q8·Q9)는 확정 회신 가능.

---

## 6. 부록 (Appendix)

### 6.1 FE 소비 노트 — fetch 스트리밍 + 경로 B (FE 직접 호출)

표준 `EventSource` API는 **GET 전용**이므로, `POST /chat`·`POST /seller/chat`의 SSE 응답은 FE에서 **`fetch` + `ReadableStream`** 으로 소비한다. FE는 **AI 서버(FastAPI)를 직접 호출**하며 `Authorization` 헤더에 사용자 JWT를 실어 보낸다(§2.3 a).

**구매자(`/chat`) 흐름**

1. `fetch(url, { method: "POST", headers: { Authorization: "Bearer {USER_JWT}", "Content-Type": "application/json" }, body })` 후 `response.body.getReader()`로 스트림을 읽고 `data:` 라인을 직접 파싱한다.
2. 각 이벤트(`token`/`conditions`/`action`/`suggestions`/`budget`/`products.ready`/`done`/`error`)로 디스패치한다.
3. **`token` → 챗 렌더**, **`products.ready` → 우측 패널**(§4.3 Spring 목록 GET), **`action` → 담기 결과 토스트**, **`conditions` → 제거 가능 조건 칩**, **`done.finishReason == "zero_result"` → 빈 상태**.
4. **`401` 재발급 흐름**: 요청 시작 시 `401`(TOKEN_EXPIRED/TOKEN_INVALID)을 받으면 → Spring 토큰 재발급 → 새 JWT로 원 요청 **1회 재시도**(§2.5).

**판매자(`/seller/chat`) 흐름**

5. 이벤트는 `token`/`draft`/`done`/`error`만 디스패치한다.
6. **`draft` → diff 카드**: `changes[]`의 필드별 before/after를 diff 카드로 렌더하고 `[적용]`/`[취소]`를 노출한다. **`[적용]` 시 FE는 `{action:"confirm", draftId}`를 판매자 챗(S-4)으로 보내고, AI가 HITL resume으로 Spring internal API(I-11 등)를 호출해 반영**한다(§3.2). 채팅 발화만으로는 반영되지 않는다. (**[v0.15.24] S-5 `PATCH /api/seller/products/{id}` 폐기 — 2026-07-21 미채택.** 07/17 신설했던 판매자 화면 직접 수정 경로는 채택되지 않았고, **상품 수정은 챗봇 HITL(I-11)이 유일 경로**다. Spring 코드에도 해당 엔드포인트가 없다.)

- **버퍼링 주의**: FE 직접 호출이므로 Spring 중계 버퍼링 이슈는 없다. 남는 주의점은 **FastAPI 앞단 리버스 프록시**의 응답 버퍼링 비활성화뿐이다(§2.4).
- **[v0.7.0] 취소·동시 스트림**: 사용자가 응답 중단 시 `AbortController.abort()`로 연결을 끊는다(별도 취소 API 없음, §2.9 b). 스트리밍 중에는 입력창을 비활성화한다 — 중복 전송 시 서버가 `409 STREAM_IN_PROGRESS`를 반환한다(§2.9 a). `504 UPSTREAM_TIMEOUT`(스트림 전)·in-stream `error`(스트림 중) 구분은 §2.9 c.

### 6.2 버전 관리 / 변경 이력 규약

본 문서는 **semver 유사(major.minor.patch)** 로 버전을 매긴다.

- **major**: 하위 호환을 깨는 계약 변경(필드 제거·의미 변경·엔드포인트 삭제).
- **minor**: 하위 호환 유지 추가(신규 엔드포인트·선택 필드·🔴 항목 확정).
- **patch**: 오탈자·설명 보강.
- 소유 SPEC(`SPEC-RECOMMEND-001`, `SPEC-PROFILE-001`)의 계약이 개정되면 본 문서를 동기화하고 변경 이력을 남긴다. `/events/*` HTTP 계약은 본 문서가 소유하므로(결정 21), 그 개정은 본 문서 버전 증가로 반영한다.

#### 변경 이력 (Change Log)

| 버전 | 날짜 | 변경 |
|---|---|---|
| v0.28.2 | 2026-08-07 | **[#421·#416] §4.8 I-17 배치 복구 규약 후속 개정 — 1선 콘텐츠 실패에 cross-cycle 재시도 예산 신설 + 2·3선 스트릭을 pg-catalog에 영속화(#325 후속).** 종전 1선(enrich 콘텐츠 실패)은 시간 유계 보호가 없어 `enrichment_item_attempts`(기본 2)회 재시도가 우연히 전부 실패(LLM 샘플링 노이즈로 인한 JSON 파싱 실패 포함)하면 그 자리에서 즉시 영구 격리됐다 — 신규 `artifacts_batch_content_retry_cycles`(기본 1주기, 0이면 종전대로 즉시 격리)로 cross-cycle 재시도 예산을 주어, 예산 안에서는 재시도 대기 큐에 등재하고 다음 주기 재시도 패스(`_run_content_retry_pass`)가 같은 페이로드로 다시 시도해 성공하면 회복하며 예산 소진 시에만 dead-letter 격리한다. 재시도 예산은 enrichment 가 실제로 쓰는 입력(`name`·`description`·`category`·`brand`·`attributes`)이 바뀐 경우에만 리셋되고(그 외 필드만 갱신되는 poison 상품이 매 주기 예산 리셋으로 영원히 격리되지 않는 것을 방지), 재시도 패스는 그 주기 `_drain` 이 hasMore 를 소진해 정상 완료한 뒤에만 돈다(유령 상품 금지 — 중단된 주기에는 재시도를 돌리지 않아 그 사이 삭제된 상품을 낡은 페이로드로 되살리지 않는다). 재시도 대기 큐(상품 원본 필드)는 AI Postgres 에 저장하지 않고 프로세스 메모리에만 둔다(원본 컬럼 사본 금지 원칙, 재시작 유실 시 하한은 종전 즉시 격리와 동일). ② 2선·3선 연속 실패 스트릭을 프로세스 메모리에서 pg-catalog `batch_failure_state` 테이블(신규)로 영속화했다 — 스케줄러가 수렴 창(기본 3주기 ≈ 15분)보다 자주 재시작되면(연속 배포·크래시 루프) 스트릭이 매번 리셋돼 poison 상품이 dead-letter 상한에 영영 도달하지 못하던 결함을 고쳤다. 신규 `artifacts_batch_failure_streak_ttl_s`(기본 3600s)로 "연속"의 정의를 시간으로 고정해 무관한 과거 실패가 오늘 실패와 합쳐지는 오격리를 막고, 저장 실패(테이블 부재·pg 순단) 시 3개 메서드 모두 예외를 삼키고 프로세스 메모리 폴백으로 위임한다(종전 동작과 같은 하한). **이 문단은 AI측 소비 동작 서술이며, I-17 요청 파라미터·응답 필드·오류 코드(와이어 계약)는 불변이다.** |
| v0.28.1 | 2026-08-07 | **[#435] §3.1 지시어 해소 적용 범위 서술 추가 — 추천 카드(CH-5) 턴에는 결정적 해소기(`resolve_screen_reference`)가 붙지 않는다.** 그 카드는 FE 위조방지 설계상 `screen`에 실리지 않으므로(서버가 `listId`로 이미 아는 목록을 되돌리면 위조 경로가 된다), 그 턴의 이름 지목은 `LAST_RECOMMENDATIONS` 프롬프트 문맥으로 LLM이 해석한다 — `screen_reference.py` 모듈 docstring 실측표가 이름 매칭을 LLM에 양보(B)하는 신호가 바로 이것이다. 두 정상 설계(FE 위조방지 × AI 상품명 공백)의 이음매가 "추천 카드를 이름으로 지목한 찜/담기 실패"로 드러난 결함(#435)의 근본 원인 서술이며, 동시에 프로필 벡터 추천 경로(`no_condition.rank_by_profile`)가 `products.search_doc` 첫 줄에서 상품명을 복원해 `LAST_RECOMMENDATIONS`에 실어주도록 고쳤다(AI 생성물 재사용, 원본 컬럼 사본 신설 아님). **서술 추가, 와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변.** |
| v0.28.0 | 2026-08-07 | **[#439] §2.3 신원 discriminator XOR 폐지 — `sub_type` 을 모든 티켓의 필수 클레임으로, `role` 을 선택적 권한 클레임으로 재정의.** 종전 XOR 규약(`role`·`sub_type` 공존 시 `401 TOKEN_INVALID`, `exactly one identity discriminator is required`)은 **BE 가 실제로 발급하는 판매자 티켓을 전부 거부**하고 있었다 — `StreamTicketProvider` 실측과 **CH-6 정본(2026-07-18)** 상 발급 형식은 "`sub_type` 은 모든 티켓 공통, 판매자만 `role="seller"`·`brandId` 추가"이며 판매자 티켓은 `sub_type="member"` 를 항상 동반한다. 이것이 운영 `/seller/chat 401` 의 원인이다(#408 이 사유 로깅을 넣은 그 401 이며, 구매자 레인은 영향이 없었다). 개정 후 `role="seller"` + `sub_type="member"` 를 판매자로 수용하고, `sub_type` 이 없거나 `guest` 인 판매자 티켓과 `seller` 아닌 role 값은 `401` 로 남긴다. **`sub_type` 없는 판매자 티켓은 종전 허용 → `401` 로 강화되지만 CH-6 정본상 실존하지 않는 형식이라 와이어 영향이 0**이며, 허용을 유지하는 것보다 단순하고 더 fail-closed 다(미지 role 관용 부활을 구조적으로 차단). 🔴 구매자 티켓에는 `role` 을 싣지 않는다는 BE 확답(2026-08-07)에 따라 buyer role 값은 계약에 두지 않는다. 401 사유 문자열은 진단을 위해 `invalid sub_type claim` / `invalid seller role claim` 2종으로 정리했다(#408 로그 경로에 그대로 실린다). **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변** — 바뀌는 것은 티켓 클레임 수용 범위다. |
| v0.27.1 | 2026-08-06 | **[#325] §4.8 I-17 배치 복구 규약 개정 — 운영 정지 대응(PR #399 리뷰 1·2차 대응으로 정밀화).** 운영 fast tier(gpt-5-nano, reasoning 모델)에서 enrichment 호출이 하드코딩 `max_tokens=600`을 그대로 썼는데, `reasoning_tokens`가 그 600 전량을 소진해 본문 0자 → `openai.LengthFinishReasonError`로 5분 주기 증분 배치가 매번 정지했다. 그 상태에서 문제 상품 1건이 페이지에 남으면 종전 복구 규약(실패 시 페이지 전체 커서 미전진)이 그 1건을 큐 머리에 영구히 고정해(head-of-line blocking) 뒤따르는 모든 변경이 처리되지 못했다. **새 복구 규약(실패 종류로 가른다)**: 격리 후보는 enrichment(LLM 호출+파싱) 단계의 내용 실패로 한정한다 — 재시도 상한(`enrichment_item_attempts`, 기본 2) 후 dead-letter 기록·즉시 격리하고 커서는 전진한다. 임베딩·스토어 실패, 그리고 enrichment 재시도 소진 후 타임아웃 계열로 판정된 실패는 항목 내용과 무관한 광역 장애 **후보**로 보아 원칙적으로 격리하지 않고 그대로 전파해 페이지 실패(커서 미전진, 자연 복구) — 페이지 크기(운영 증분 페이지의 소량 표본 포함)와 무관하게 성립한다. **2차 리뷰 대응(시간 유계)**: 다만 광역 장애와 항목 고유 결정적 실패(예: 특정 상품에서만 결정적으로 재현되는 poison 타임아웃, `_finish_change`가 만든 `extras`의 `embedding_meta_complete` CHECK 위반 등)는 단일 주기 관측만으로는 구별할 수 없어, 위 전파 대상은 상품별 **연속 실패 스트릭**(프로세스 메모리, 주기 간 유지)을 세고 `artifacts_batch_item_dead_letter_cycles`(기본 3주기 ≈ 15분)에 도달하면 항목 고유 실패로 확정해 격리한다 — 그 미만이면 종전대로 전파한다. 이로써 배치가 상품 1건 때문에 막힐 수 있는 시간이 유계화된다. 페이지 `ON_SALE` 표본·실패율이 임계(`artifacts_batch_failure_min_sample` 기본 5 · `artifacts_batch_failure_ratio_threshold` 기본 0.5) 이상이면 3선 방어로 마찬가지로 페이지 실패(커서 미전진) — 앞선 선들을 통과한 뒤에도 남는 "인프라는 멀쩡한데 enrichment 결과가 대량으로 깨지는" 경우를 잡는다. **3차 리뷰 대응(3선도 시간 유계)**: 다만 1선이 특정 카테고리 상품들의 프롬프트 회귀로 다건을 매 주기 즉시 격리하면 2선 스트릭이 쌓이지 않아 그 상한이 걸리지 않고, 비율 가드만 같은 커서에서 매 주기 반복 발동해 커서가 영원히 전진하지 않을 수 있었다(3선이 스스로 #325 의 무기한 정지를 재현). 같은 커서에서 비율 가드가 연속 발동한 횟수를 세어 `artifacts_batch_page_failure_max_cycles`(기본 3주기 ≈ 15분)에 도달하면 그 페이지를 격리(항목은 이미 1·2선에서 dead-letter 기록됨)하고 커서를 전진시킨다 — 그 미만이면 종전대로 페이지 실패(자연 복구). `HIDDEN` 삭제 실패·계약 위반(`status` 미정의 값)은 이 시간 유계의 대상이 아니다 — 항목별 ack/DLQ 계약 부재로 종전대로 무기한 fail-closed. 함께 enrichment 출력 상한(`enrichment_max_tokens` 기본 2048)·reasoning effort(`enrichment_reasoning_effort` 기본 minimal)를 하드코딩에서 config 튜너블로 이관했다. **I-17 와이어 계약(요청 파라미터·응답 필드·오류 코드) 불변** — 본 개정은 AI측 소비 동작 서술이다. |
| v0.27.0 | 2026-08-06 | **[#396] 구매자 `progress` 다회 emit + `stage` 어휘 확장(1종 → 7종, 개방형)** — `0~1회` → `0회 이상`, `analyzing`에 `mapping`(카테고리 매핑 중)·`expanding`(니즈 전개 중, #198)·`searching`(상품 후보 검색 중)·`relaxing`(조건 완화 재검색 중)·`reranking`(재정렬 중)·`publishing`(목록 준비 중, I-21 push 직전) 6종을 추가했다. FE가 **모르는 `stage` 값을 무시**(오류로 다루지 않음)하는 개방형 규약을 명문화해 이후 어휘 추가를 AI 단독 배포로 가능하게 한다. 표시 규약은 덮어쓰기(누적 아님)이며, 같은 `stage`가 두 번 이상 올 수 있고 `progress`는 `token`·`suggestions` 사이사이(특히 `publishing`은 `token` 이후 `products.ready` 직전)에도 낄 수 있다. "첫 stage가 `analyzing`인 이유"(intent 미확정) 논거는 유지 — `searching`은 intent가 확정된 뒤 추천 레인 안에서만 나간다. **기존 6종의 이름·페이로드·상대 순서는 불변**(추가 전용) — `conditions`는 여전히 검색·자동 완화 뒤다. **정본(Notion CH-2)은 2026-08-06 개정 완료**(0회 이상·개방형 7종 stage 표·덮어쓰기 표시 규약) — 본 사본은 그 동기화다. |
| v0.26.3 | 2026-08-06 | **[#310] I-18 응답 필드표에 `items[].purchaseState` 를 등재했다(§4.9).** **신설 협의가 아니다** — BE `jarvis-backend#91`(머지 완료) 이 `purchasable`(boolean) 을 `purchaseState`(enum) 로 교체하며 I-18 을 대상에 포함했고(`InternalCartResponse.Item` 실측), 그 필드가 이미 와이어에 실려 오는데 본 사본에만 누락돼 있었다. 값은 `AVAILABLE`/`SOLD_OUT`/`HIDDEN` 이며 겹치면 `HIDDEN` 우선이고 **둘 다 상품 단위 판정이되 성격이 다르다** — `HIDDEN`은 옵션과 무관하게 상품 전체가 판매 종료이고, `SOLD_OUT`은 재고가 `product.stock_quantity` 하나로 옵션 전체에 공유돼(`product_option`에 재고 컬럼 없음, BE 02 D2·D33) **옵션 중 하나라도 살 수 있으면 `AVAILABLE`** 이다. 함께 §4.9·§4.16 에 **"`SOLD_OUT`/`HIDDEN` 은 사유별로 갈라 안내하고 미수신은 안내하지 않는다"** 는 AI 동작 서술을 대칭으로 추가했다(§4.16 은 필드가 v0.23.0 에 이미 등재돼 서술만 보강). 정확한 문구는 코드가 정본이라 명세에 박지 않는다. 같은 표의 **`items[].optionId` 타입 표기 드리프트도 정정**했다(`string` → `number`) — 같은 문서 §4.1 요청표·§4.9 응답 예시 JSON(`"optionId": 3`)·§2.6·코드(`app/schemas/spring.py`)·BE(`Long optionId`) 가 전부 number 인데 이 행만 string 이었다(v0.23.0 의 §3.1 `cartItemId` 정정과 같은 종류). **추가 전용 — 기존 엔드포인트·SSE 이벤트·필드·오류 코드 계약 불변.** |
| v0.26.2 | 2026-08-06 | **[#396] 구매자 `progress` 플래그 기본 on + 운영 기동 가드 제거** — 계약 등재(v0.21.0)·FE 구현 완료(2026-08-06)로 전제 충족. config `progress_events_enabled` **기본 `false` → `true`**로 전환하고, `_require_pepper_in_prod`의 [#289] 운영(jwks)·스테이징 기동 가드(`progress_events_enabled` on 이면 기동 실패)를 제거했다 — 이 가드 제거가 해제 절차의 일부였다(다른 fail-closed 가드 pepper·internal token·jwks_url·google_api_key·state store·session claim TTL 은 무변경). §2.9(c) I-1 재시도 행의 "남은 전제는 플래그 on + 배포" 문장을 갱신했으나, **#277 재시도 스킵 원복 자체는 이번 범위가 아니다**(#394, 커밋 `2168e9b`가 같은 날 다른 이유로 `spring_max_retries` 기본값을 이미 1→0으로 내렸고, 원복 여부는 그 조치와 함께 판단해야 한다). **와이어 계약(이벤트 이름·페이로드·필드·횟수·상대 순서) 불변** — 바뀌는 것은 "잠겨 있다"는 구현/배포 상태 서술뿐이며, 이제 실제 와이어에 `progress` 프레임이 나간다. 되돌리려면 `PROGRESS_EVENTS_ENABLED=false` 한 줄. |
| v0.26.1 | 2026-08-06 | **[#367] §3.7 HOME(I-22) 실패 모드 어휘 4종을 현행 추인으로 규범화했다** — `profile_unavailable`(200, degrade)·`catalog_unavailable`(503 `UPSTREAM_UNAVAILABLE`)·`catalog_timeout`(504 `UPSTREAM_TIMEOUT`)·`reason_degraded`(200 `PERSONALIZED`+`reason=null`), 코드는 `app/services/home_recommendation.py` 현행 그대로 무변경. 「HOME 실패 모드(degrade) 어휘」 소절을 실패 응답표 바로 아래 신설하고, 실패 응답표 503/504 행의 조건 서술을 실제 조건(카탈로그 인덱스 한정)에 맞게 정정했다(구 서술 "내부 의존성(프로필 저장소·LLM) 일시 장애"·"예산 초과"는 드리프트였다 — #335 매트릭스가 구 combo-0050/0052/0053(#335 최초 매트릭스 기준 번호, 본 PR 재채번 이전)에서 CHAT 전용 degrade 축이 HOME엔 대응 경로가 없음을 발견). §2.5 통합 오류표 503 행에 §3.7이 카탈로그 인덱스 한정임을 병기했다. **와이어 계약(필드·`outcome`·오류 코드) 불변** — `evals/combo_matrix` degrade 축을 지면별 어휘로 잇는 정본이다. |
| v0.26.0 | 2026-08-06 | **[#322] #149 개인화 그래프 계약 개정 — 구현(#150) 착수 전 정리.** v0.22.0 이 등재한 계약에서 **[HARD] 조항 2건이 뒤집혔고**, 노션 정본 등재 과정에서 확정된 6건을 사본에 반영했다. (1) **개별 삭제 = 즉시 억제 → undo 창 → 원문 물리 삭제**(§3.9.2/.3) — 구 계약은 억제만 하고 원문을 무기한 보관했다. 사용자가 "지웠다"고 믿는 문장의 원문을 들고 있을 이유가 없고(데이터 최소화), 복구 가능성이라는 명분은 undo 창(`graph_undo_window_s`, 기본 5분)이 대신한다. tombstone 은 **시간 만료가 없다** — 만료시키면 `profile_idle_sweep_interval_s`(60초) 주기 flush 가 같은 발화를 재승격시켜 방금 지운 취향이 부활한다(실측 근거). 단 "영구"는 *자동 만료 없음*이지 *사용자도 못 지움*이 아니다(전체 초기화는 tombstone 도 지운다). **REQ-PGRAPH-032(pin 만료 없음)와의 구분 문장**을 SPEC 에 박았다 — 만료가 붙는 것은 원문 보관 기간뿐이며, pin 이 걸린 edge 도 사용자가 지우면 물리 삭제되는 것이 맞다. 함께 고친 기존 모순 2건: **"기계 하드 삭제는 유일한 예외"** 단정(REQ-PGRAPH-077·REQ-PROF-034)을 2건으로, **멱등 원장 TTL > undo 창**이라 purge 후 restore 재전송이 "복구됨"을 재생하는 구멍(REQ-PGRAPH-028). (2) **전체 초기화 범위에 `conversation_turns` 포함**(§3.9.4) — 근거는 #149 가 REQ-PROF-034 에서 이미 채택한 논거의 연장(금지 대상은 *기계가 조용히 지우는 것*이고 사용자 자신의 삭제권은 별개 — 예외가 아니라 적용 범위 한정)이며, **OPEN-G6**(파생은 만료되는데 원인 원문은 전사록에 남는 비대칭)이 해소된다. 감사 로그는 계속 보존. Spring 에 채팅 이력 사본이 없어 **AI 단독 완결**(선결 확인 해소). 전사록 자연 만료 TTL(OPEN-P5)과는 **별개 트리거**이며 본 개정은 TTL 을 정하지 않는다. `[FE 문구 주의]`·EX-G5 재작성. (3) **§3.8 조회를 FE 직접 → Spring 프록시로 전환** — 마이페이지에는 채팅 세션이 없어 `chat:stream` 티켓을 발급받을 수 없다(CH-1b 는 `sessionId` 필수). **재사용할 자산이 없는 전제 위의 [HARD]**("비대칭은 의도된 것이며 통일하면 안 된다")였으므로 폐기하고 §3.9 와 같은 레인으로 합류시켰다(§1.2·§2.3 a/b·§3 앵커·§8 항목9 동기화). 전용 `profile:*` scope 신설안은 **여전히 기각**(채팅 검증 경로 회귀 위험). 연쇄로 게스트 `200`·판매자 `403` 분기 소멸, `401` → `INTERNAL_TOKEN_INVALID`, `429` 미적용, ETag CORS(C-11) 소멸. **§3.4 는 범위 밖**. (4) **I-번호 재채번 I-29~I-33 → I-32~I-37** — C-26 이 경고한 충돌이 실증됐다(I-29~I-31 은 판매자 주문·리뷰 §4.18~4.20/#297 선점). v0.25.0 의 "I-34~I-38" 제안은 조회를 세지 않은 5종 기준이라 폐기하고 정본 6종(조회 I-32 포함) 채번을 따른다. (5) **`evidenceCount` 와이어 제거**(§3.8·§3.9.1) — `profile_buffer_repeat_cap`(=2)이 같은 발화를 2회로 잘라 담으므로 **정확한 관측 횟수를 셀 수 없다**(#119). 내부 `evidence_count` 는 병합 합산에 계속 쓰인다. (6) **§3.8 `userId` string → number(BIGINT)** — 「타입 비대칭(의도적)」의 근거("조회는 토큰 신원을 되돌려준다")가 프록시 전환으로 소멸. (7) **§3.9.1 `object.nodeId` 직접 지정** — FE 자동완성으로 고른 노드를 서버가 재정규화하면 resolver 근접 매칭이 다른 노드로 튄다. `type`+`label` 동시 지정은 `400`. (8) **`error.detail` 공식화**(§2.5) + §3.9 `409` 의 `graphVersion` 을 봉투 밖 → **`error.detail.graphVersion`** — §4.1(I-2)이 이미 쓰던 관례가 미등재였고 §3.9 가 그 미등재를 근거로 반대로 갔다. 확장 자리를 하나로 고정해 **C-21(Spring 이 `409` 본문을 변형 없이 통과) 난이도가 내려간다**. **미합의 표**: C-20(확약 경로 5→6, 게스트 노출 여부 신규)·C-21(갱신)·C-23((1)항 🟢 해소, 잔여 분리)·C-25(갱신)·C-26(🟢 AI측 확정) — 해소분과 잔여분을 분리해 적었다. **불변**: 기존 엔드포인트·SSE 이벤트·기존 오류 코드·AI→Spring 역방향 18건(§1.2 레인 c)·판매자 §4.18~4.20. §3.8·§3.9 는 전 구간 🔴 제안(초안)·Post-MVP 로 **구현이 없어 깨질 소비자가 없다**. 동반 개정: `SPEC-PROFILE-GRAPH-149` v0.2.0 · `SPEC-PROFILE-001` v0.8.0. |
| v0.25.0 | 2026-08-05 | **[#297] 판매자 주문·리뷰 internal 3종(I-29~I-31) 계약 등재 — 🔶 초안, BE 협의 전.** (1) **§4.18~4.20 신설** — I-29 자사 주문 조회(현재 상태 스냅샷, I-14 전이 이력과 역할 분리 · 기간 선택/생략 시 전체 · `orderId` 미존재·타사 = 200+빈 rows 존재 은닉 · 자사 `items[]`에 `orderItemId` 포함) · I-30 주문 아이템 발송 처리(HITL 필수 쓰기, MVP 전이 `ORDERED→SHIPPING` 뿐 · 발송 후 역전이 불가 · 409 `ORDER_ALREADY_SHIPPED` 멱등 200 금지 · 404 존재 은닉) · I-31 자사 리뷰 조회(VISIBLE만 · 기간 생략 시 최근 7일 기본 · `sort=rating` 낮은 순 고정 · `stats=true` 집계 — 0건 `averageRating: null`). 전부 2026-08-04 확정사항 반영, 정본은 노션 DB. (2) **§3.2 `draft.op`에 `ship` 추가 + `orderItemId` 필드(추가 전용)** — 기존 op 3종 와이어 불변, **기존 `product` 레인 재사용(meta.lane 신설 없음, 확정 2026-08-04)**, HITL 5대 안전장치 동일 적용. 정본(노션 CH-2/S-4) 개정은 후속. (3) **I-30 409 코드 개명** — `ALREADY_SHIPPED` → `ORDER_ALREADY_SHIPPED`(공통 규약 `<도메인>_<사유>` 형식, 2026-08-05 승인 — 노션 I-30 반영 완료, AI는 과도기 양쪽 수용). 신규 코드 2종(`ORDER_ALREADY_SHIPPED`·`ORDER_ITEM_NOT_FOUND`)은 BE 확정 후 규약 등재 필요. (4) **⚠️ §3.9(개인화 그래프, #149)와 I-29~I-31 번호 충돌 명시** — §3.9 채번은 미확정(🔴 C-26)이므로 §3.9 를 I-34~38 로 재채번 제안, 판매자 3종은 정본 번호 유지. **AI 구현 동반**: `SpringClient` 4메서드(+ I-30 코드별 전용 예외) · 툴 3종(`get_orders`/`update_order_status`/`get_reviews`) · HITL `op="ship"` · analysis `review` 워커(6종째) — **Spring 미구현이라 실 와이어는 아직 없다**(BE 협의·구현 후 활성). |
| v0.24.0 | 2026-08-05 | **[#296] 판매자 분석 보고서 구조화 `report` SSE 이벤트 신설(§2.2·§3.2) — 기간·요약·findings·데이터 한계·차트·추천을 한 이벤트에 내장해 우측 패널 재료로 제공, `token` 산문(좌측 채팅·스레드 기록 원천)은 불변. 구 `chart` 이벤트(v0.20.0, #242)는 legacy 폐기(부활 없음) — FE 미구현 실증(useChat.ts 소비 케이스 부재)으로 소비자 없는 계약이라 dual-emit 없이 안전 대체. 이벤트 7종 유지(chart→report 교체), 그 외 이벤트 이름·페이로드 계약 불변** |
| v0.23.1 | 2026-08-05 | **[#162] I-3 `GET /internal/products/popular` 를 사본에 등재했다(§4.17 신설).** **신설 협의가 아니다** — 정본(Notion API 명세서 I-3)에 이미 있고 BE 구현(`InternalProductController#popular`)도 완료인데 본 사본에만 누락돼 있었다. 등재 계기는 #162 다: I-1 정본이 2026-07-27 후보 수 상한을 폐지하며 **"정형조건이 하나도 없는 요청은 LLM 단에서 차단하므로 BE 는 별도 가드를 두지 않는다"** 를 전제로 걸고 0건 시 폴백 대상으로 I-3 를 지목했는데, **AI 가 그 차단을 구현하지 않아** 조건 없는 발화가 파라미터 0개로 I-1 에 나가 매칭 전량(실측 7,245건·13.33MB·1.112s)을 받고 있었다. 등재 내용은 요청(`size`, **BE 에 범위 검증이 없어 음수·0 이 400 이 아니라 빈 배열로 오므로 양수 보장은 AI config 책임**)·응답(§4.6 과 동일 DTO — 정본 I-1 의 *"같은 DTO 를 쓰는 I-3 도 동일하게 나간다"*)·실패 3종(400·401·500, *"그 외엔 실패하지 않는다"*)·**0건은 성공**(빈 배열이면 카드 없이 텍스트만 — degrade 아님)·타임아웃 3s·**재시도 없음**(§2.9(c) 재시도 1회는 I-1 전용 예외이며 first-token 예산을 이미 압박한다, #277·#288)이다. **`size` 는 I-1 의 전량 반환 원칙과 무관하다** — "인기 상위 N"이라는 목록의 정의이지 후보를 자르는 상한이 아니며, 조건이 있는 턴은 종전대로 I-1 로 가고 상한이 없다. 레인 (c) 는 17→18건(§1.2 표·서술·§4 도입부 3곳). BE 내부 구현 세부(인기 집계 규칙·캐시 크기·TTL)는 AI 동작이 의존하지 않고 BE 가 바꾸면 사본이 거짓이 되므로 **의도적으로 싣지 않는다.** **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변.** |
| v0.23.0 | 2026-08-05 | **[#116·#117] I-24~I-28 계약 등재(정본 확정 2026-08-05) + CH-2 `action` 2종 → 10종 + §3.1 `cartItemId` 표기 정정.** (1) **§4.12~4.16 신설** — I-24 장바구니 삭제·I-25 수량 변경(🔶 AI 미구현)·I-26 찜 추가·I-27 찜 해제·I-28 찜 목록 조회를 §4.1(I-2)·§4.9(I-18)와 같은 형식(요청·성공 응답·실패 표·AI 동작)으로 등재했다. 신원 query/body 검증 오류 code는 자원별 신규 code를 신설하지 않고 기존 `VALIDATION_ERROR`를 재사용한다(I-24~I-28 공통, 확정 — 1차 초안의 `CART_QUERY_INVALID` 등 신설안은 채택되지 않았다). (2) **§3.1 `action` type 2종 → 10종** — `CART_REMOVED`/`CART_REMOVE_FAILED`/`WISHLIST_ADDED`/`WISHLIST_ADD_FAILED`/`WISHLIST_REMOVED`/`WISHLIST_REMOVE_FAILED`/`CART_QUANTITY_CHANGED`/`CART_QUANTITY_CHANGE_FAILED` 8종을 추가하고 `reason`에 `WISHLIST_ERROR`를 추가했다(기존 3종 유지, 4종). 404/409 정상 종료 규약(`CART_ITEM_NOT_FOUND`→`CART_REMOVED`, `WISHLIST_DUPLICATE`/`RESOURCE_CONFLICT`→`WISHLIST_ADDED`, `WISHLIST_NOT_FOUND`→`WISHLIST_REMOVED`)과 찜 이벤트 `productId` 미탑재(경로 B)를 명문화했다. (3) **§3.1 `cartItemId` 표기 정정(string → number)** — 예시 `"cartItemId": "55"`와 필드표 `string`은 **사본 드리프트였다**. 정본 CH-2·FE 타입(`jarvis-frontend` `src/shared/types/chat.ts`의 `ChatAction`)·서버가 실제로 내보내는 프레임(`app/schemas/chat.py`의 `ActionData.cart_item_id: int`) 셋 다 number(BIGINT)다. (4) **I-28 응답 필드 `purchasable`(boolean) → `purchaseState`(string enum: `AVAILABLE`/`SOLD_OUT`/`HIDDEN`, 겹치면 `HIDDEN` 우선)** — 2026-08-05 M-4 개정 반영, 코드는 이미 이 이름으로 등재됐다(커밋 `70247d0`). **Spring 미구현·FE 미수신이라 실 와이어는 아직 종전 그대로다** — I-24~I-28은 BE가 구현 진행 중이고(🔶 확정 2026-08-05, 배포 전 호출 무응답), AI 구현은 완료됐으며(플래그 없이 항상 활성, 삭제·찜 6종 #116·#117 — 수량 변경 2종은 미구현), `jarvis-frontend`의 `ChatAction` 유니온에는 신규 8종이 아직 없다. |
| v0.22.0 | 2026-08-05 | **[#149] 개인화 관계 Graph 계약을 초안으로 등재했다** — 조회 §3.8 `GET /profile/me/graph`(FE 직접) + 제어 5종 §3.9 I-29~I-33(Spring→AI internal: 수정·삭제·복구·초기화·개인화 중지). 취향이 지금은 편집 불가능한 마크다운 한 덩어리라 오염돼도 사용자가 고칠 수 없는데, 오염된 프로필이 추천 품질을 실제로 깎는다는 것이 측정돼 있다(#147 커밋 baseline: 깨끗한 프로필 +0.20, 노이즈 −0.053, 반복 부풀림 −0.117 nDCG@10). **선택의 근거**: (1) **조회는 레인 (a) 기존 티켓 재사용, 변경은 레인 (b) Spring 경유** — 전용 `profile:*` scope는 `/chat`·`/seller/chat`이 함께 지나가는 검증 경로를 개편해야 해 프로필 기능을 위해 채팅에 회귀 위험을 만든다(C-20). (2) **충돌은 `409 PROFILE_VERSION_CONFLICT`, `412` 미채택** — 409에 상태 인식 불일치 3형제가 이미 있고 412는 상태→코드 매핑에 없어 기본값이 일반 코드로 나간다(C-22). (3) **개별 삭제 = tombstone(복구 가능), 전체 초기화만 물리 삭제** — 억제를 만들면 되돌리기도 만든다는 규칙이며 `SPEC-PROFILE-001` REQ-PROF-034를 약화하지 않고 적용 범위를 기계 경로로 한정했다. (4) **개인화 중지 = 사용·수집 동시 중지, 데이터 보존** — 중지 중에도 그래프는 200으로 보이고 모든 정리 동작이 허용되며, I-22는 기존 `NO_PROFILE`로 답해 **Spring 무변경**이다. (5) **`confidence`는 3버킷만 노출** — 수치 미노출 불변식을 깨지 않는다. **불변**: 기존 엔드포인트·SSE 이벤트·기존 오류 코드·AI→Spring 역방향 17건 집합(신규 5종은 inbound). 신설분은 전부 🔴 **제안(초안)·Post-MVP**이며 미합의 8건을 C-20~C-28로 등재했다(C-20·C-21·C-27·C-28은 #150 차단). 예산 2s/3s는 **제안이며 실측이 아니라** §2.9 (c) 기준표에 넣지 않았다. |
| v0.21.0 | 2026-08-05 | **[#289] 구매자 SSE `progress` 이벤트 신설 — 정본(Notion "📡 API 명세서" CH-2) 2026-08-05 합의·등재 반영.** 본 사본 §3.1의 번호가 **6종 → 7종**으로 늘고 `progress`가 **(1)번**이다(§2.2·§3.1: `progress` 1 · `token` 2 · `conditions` 3 · `action` 4 · `products.ready` 5 · `done` 6 · `error` 7). **기존 6종의 이름·페이로드·상대 순서는 불변**(추가 전용) — `conditions`는 여전히 검색·자동 완화 뒤다. **[번호 체계 참고]** 정본(CH-2)은 `suggestions`를 이벤트 목록에 번호로 포함해 8종으로 세지만, 본 사본은 `suggestions`를 「MVP 추가 페이로드」 절에서 별도로 다루므로 §3.1 번호 목록이 하나 적다 — 번호는 표기 순서일 뿐 와이어 계약이 아니며, 이 차이는 `progress` 신설 이전부터 있던 것이다. 페이로드는 `{"stage","message"?}`이며 확정 어휘는 **`analyzing` 1종**뿐이다(`searching`/`relaxing`/`reranking`은 후속 확장 후보·미구현) — 이 프레임을 낼 수 있는 지점(decompose 앞)에서는 아직 intent(추천/담기/주문조회/일반 대화)가 확정되지 않아 `searching`을 그 자리에서 내면 비추천 턴을 검색 중이라고 오라벨링하게 되기 때문이다. **0~1회다 — FE는 도착을 전제하면 안 된다**: 나가면 스트림 첫 프레임이지만, 그 앞에서 턴이 끝나는 경우(LLM 미구성 → `error{LLM_UNAVAILABLE}`, 세션 상태 저장소 장애 → §2.5 스트림 전 오류 봉투)는 0회다. 실측(`evals/first_event_budget/`, #277 하네스 재사용)은 대표 6개 시나리오 전부에서 flag-on 첫 이벤트 p50이 ~12~15ms로 수렴했다(`D3_deferred_worst_no_retry` 6869.8ms → 11.6ms) — 그 뒤 이벤트 순서·총 소요는 불변이며, 하네스가 `ScriptedLLM`이라 LLM head 절감(#151)은 수치에 잡히지 않는다. **다만 관문 통과를 보장하지는 않는다** — 이 프레임 앞의 상태 저장소 프렐류드가 각 `state_store_query_timeout_s`(3.0s) × 4회 직렬 최악 12.0s로 first-token 상한(10.0s)을 넘을 수 있다. §2.9(c) I-1 행의 `progress` 등재 전제 문장을 "등재 완료, 남은 전제는 플래그 on + 배포"로 갱신했다. **AI 구현은 완료됐으나 `progress_events_enabled` 기본 `false` + 운영(jwks 인증 또는 staging/production 환경) 기동 가드로 잠겨 있어, 실제 와이어는 아직 종전 7종뿐이다** — 이 가드 제거와 플래그 on, #277 재시도 스킵 원복은 이번 개정에 포함되지 않는다. |
| v0.20.4 | 2026-08-05 | **[#118] 되물음 예외 정본 반영** — §3.1 [보안] 문단 바로 아래 병기해 둔 옵션 되물음(`PENDING_CART`) 중 `screen.products` 제외 예외를, 사용자가 승인한 정본(Notion CH-2) 담기 가드 문단 바로 아래 '되물음 예외 (2026-08-05 신설)' 문단 반영에 맞춰 사본의 🔴 정본 미반영 표시를 해제했다. **계약 동작은 이미 정본과 일치했고 이번 개정은 표시만 갱신한다.** |
| v0.20.3 | 2026-08-04 | **[#118] §3.1 v0.15.26 등재 계약인 `screen`(화면 맥락) 수신을 구현했다.** 유효성은 관대하게 — `pageType` 누락·14종 밖이면 `screen` 전체를 무시하고 200으로 진행하며 **어떤 경우에도 400을 내지 않는다**(`conditionActions`의 엄격함과 대비된다). 담기 허용 목록은 **(누적 추천 목록 ∪ `screen.products`의 productId)** 로 넓히되 **프리패스가 아니다** — 두 목록 밖 id는 여전히 차단하고 모호하면 되물음하며, 실제 담기는 I-2가 재고·판매상태를 다시 검증한다. 지시어 해소("이거"·순번·좌표·이름)와 §3.2 판매자 레인의 `pageType`·`filters` 맥락 주입을 포함하며, 판매자 쪽 `screen.products` 소비는 범위 밖으로 남긴다. **와이어 계약(엔드포인트·요청 필드·SSE 이벤트·오류 코드) 불변 — 수신 구현이다.** |
| v0.20.2 | 2026-08-04 | **[#277] 실 HTTP 경계 측정으로 판단해 `may_auto_relax` 턴의 I-1 재시도를 기본 스킵한다.** 변경 전 두 호출이 모두 재시도하고 2차 응답이 2.9s에 온 실최악은 이벤트 0건·504가 10.01s에 8/8 재현됐다. 변경 후 같은 시나리오는 p50 3.40s·200 `conditions`+`error(SEARCH_FAILED)`로 끝나고, 새 최악(본 검색·자동 완화 probe가 재시도 없이 각각 2.9s 성공)은 p50 6.97s·200 정상 답변이었다. 대가로 재시도가 살리던 검색 장애는 6.80s 정상 답변에서 3.40s retryable degrade로 빨리 떨어진다. `SEARCH_RETRY_ON_DEFERRED_CONDITIONS=true`가 종전 동작 복구 가드이지만 이 조합은 기동 시 직렬 합으로 검증돼 기본 타임아웃 그대로면 기동이 막히며, `relaxation_max_rounds=0`이면 미루기를 끈다. 구매자 `progress` 이벤트가 계약에 등재돼 검색 전 첫 프레임을 낼 수 있게 되면 스킵을 원복할 수 있다. 수치는 LLM head 제외이며 #151 baseline head p95 ≈3.0s를 더하면 새 최악도 ≈10.0s다. **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드)은 불변**이고 BE 관측 포인트만 갱신했으며, 직렬 합 검증식·타임아웃 재배분은 #288에 남긴다. |
| v0.20.1 | 2026-08-04 | **[#278] §4.6 `categoryName` 해석을 정본 2026-08-03 개정(잎 이름 정확 일치)으로 동기화하고 구 "대분류명이 기본" 문구를 폐기했다.** 코드는 잎 매핑(#115/#217)으로 이미 정합하다. §3.1 v0.15.26 등재 계약인 `conditionActions` 수신을 구현했고, I-1 응답에 선택 필드 `options`(옵션 이름, 최대 20개)·`optionCount`(절단 전 전체 개수)를 추가 전용으로 확정해 관대 수신한다. |
| v0.19.5 | 2026-08-04 | **[#232] 명시 재구매 지목을 종전 "현재 턴 한정"에서 스레드 범위 내부 신호로 정정했다.** 저장값은 매 턴 그 시점의 본인 최근 구매 집합(I-19, JWT `sub` 유래)과 교집합해 면제가 항상 본인 최근 구매의 부분집합이 되며, `dedup_recent_days` 밖 또는 취소·반품 구매는 자동 제외된다. 누적은 스레드별 `DEDUP_REPURCHASE_STORE_MAX`(기본 20, `0`이면 지속 off)로 유계이고 상한은 읽기·쓰기 양쪽에 적용돼 하향 시 즉시 반영된다. **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드)과 FE는 불변**이며 요청·응답 필드 및 exact 되돌리기 칩은 추가하지 않는다. |
| v0.19.4 | 2026-08-03 | **[#32] 골든셋 실측으로 방식2를 확정하고 방식1 및 C-17을 기각했다.** dev `search` 26건·라이브 pg-catalog 7,220건에서 방식1/방식2 mean recall@5/@10/@20은 0.6026/0.7987/0.8449와 0.7872/0.9205/1.0000, 방식1 승리는 0/26이었다. C-17은 방식1 hydrate의 가용성 확인만 가능하게 할 뿐 가격 하한·부정어 같은 구조적 제약 실패를 고치지 못해 BE 요청을 철회한다. 라벨이 Spring 후보에서 유래해 방식2 recall 상한이 구조적으로 1.0인 편향이 있으므로 결론은 “방식1이 방식2를 못 이긴다”까지이며, 후보 독립 라벨 구축 시 재검토한다. `VectorSearchBackend`는 오프라인 비교 전용으로 존치하고 운영 롤백은 `SEARCH_BACKEND=spring`을 쓴다. **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변**이다. |
| v0.19.3 | 2026-08-03 | **[#138 후속] §2.9(c)의 FE→AI 스트림 전체 상한을 역할별로 분리했다.** #138 실측에서 구매자 total p95 10.5s·max 12.8s, 154턴 중 30s 초과 0건을 근거로 구매자는 **30s**(`stream_total_timeout_buyer_s`)로 명시하고, 판매자·미지정 역할은 **90s**(`stream_total_timeout_s`)를 유지했다. **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변**이며, 두 역할 모두 초과 시 `done`(`finishReason=stop`) 정상 절단 규약을 유지한다. |
| v0.19.2 | 2026-08-03 | **[#133] 구매자 degrade 고지와 I-1 검색 재시도를 명문화한다 — 와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드)은 불변이며 FE 변경도 없다.** (1) **§3.3 rerank degrade에 고지 요구 추가** — 검색-순서 degrade는 개인화와 상품별 근거(I-21 `reasons`)를 **함께** 없애는데, 종전 문구가 정상 경로와 구분되지 않아 "조건에 맞게 골라줬다"로 읽혔다. 같은 저장소가 판매자에는 degrade 정직성 게이트(보고서가 한계를 명시하지 않으면 검증 실패)를 두고 구매자에는 두지 않은 **비대칭**이었다. 이제 `token`으로 품질 저하를 고지하되 **실패 단계명·오류 코드는 싣지 않는다**(§3.3 "단계별 상세는 서버 로그 전용" 유지). 문안은 AI config 주입이라 계약이 문구를 고정하지 않으며, 이벤트 타입·필드·발신 순서가 그대로라 FE는 무변경이다. (2) **§2.9 c 타임아웃 표에 I-1 재시도 행 추가 — BE 관측 포인트다.** 한 턴에 같은 검색 요청이 **최대 2번** 온다. 재시도 대상은 **타임아웃·연결 오류·응답 중단·5xx·일시 4xx(408·429)로 한정**하며 4xx는 다시 불러도 같은 결과라 즉시 실패한다. I-1은 GET·멱등이라 안전하고 **비멱등 호출(I-2 담기 등)에는 재시도를 걸지 않는다** — 중복 담기 위험 때문이다. 재시도 총량(기본 3s×2=6s)은 **구매자 전체 상한**(#138 로 90s→30s) 아래로 묶여 AI 기동 시점에 검증한다 — 초판 등재는 first-token 10s 와 비교한다고 적었으나, 그 상한이 재는 것은 *첫 SSE 이벤트*까지이고 추천 경로의 첫 이벤트 `conditions` 는 검색보다 **먼저** 나가므로 대상이 틀렸다(#138 실측 lessons «상한이 실제로 재는 지점을 코드에서 확인한다» 로 정정). **[#113 로 전제 변경, #277 실측으로 확정]** 미룬 턴에서는 그 순서가 성립하지 않으며 첫 이벤트 앞 I-1이 2회 직렬이다 — 현재 서술은 §2.9(c)와 v0.20.2 행을 읽을 것. 재시도 상한 자체도 **1로 고정**한다 — backoff 가 구현에 없어 2 이상은 herd 증폭을 방어 없이 여는 설정이다. 이는 새 요구가 아니라 `SPEC-RECOMMEND-001` §오류 처리가 이미 규정한 *"최대 1회 재시도"* 의 **구현 갭 해소**다. |
| v0.19.1 | 2026-08-02 | **[#197] I-16 이탈 코호트 응답 실측 확정 + I-8 실측 명문화(admin 소유 🔴 유지·판매자 노출 보류).** (1) **I-16 `from`/`to` 필수 명시** — BE `AnalysisPeriod.of` 가 누락·형식 오류·역전을 400 INVALID_PERIOD 로 거부(분석 API 공통). 구 AI 클라이언트는 `inactiveDays`만 보내 이탈 조회가 상시 400 → degrade(주 소스 전면 불능 — #197 버그 1의 원인)였다. (2) **I-16 응답 실측 확정(SellerChurnResponse)** — `cohortSize`·`churnRate`(**소수 fraction, 0.6=60%**, 구 AI 표기가 %로 오독해 60%를 "0.6%"로 왜곡 — #197 버그 3)·`preChurnSignals` **객체**(구 AI 스키마는 배열 기대로 ValidationError → degrade — #197 버그 2, I-14/I-15 #194 와 동일 패턴)·`members[]`(상한 50). (3) **I-8 실측 명문화** — `from`/`to` 필수, `groupBy` 화이트리스트(`eventType`/`hour`/`ip`), 응답 `groupBy`+`rows[]`(shape 2형). 구 AI 스키마는 `events` 필드 기대로 항상 "0건" 이었다(#194 조용한 미스매치 패턴). (4) **I-8 admin 소유 🔴 는 미해소 유지** — 종전엔 코드 결함(400·스키마 미스매치)으로 전역 데이터가 판매자 표면에 도달하지 않았으나 #197 정합으로 실제 노출이 가능해지므로, **협의 완료 전까지 AI 설정 `seller_account_events_enabled`(기본 false)로 판매자 워커 노출을 보류**한다 — 도구는 "Error: 비활성" 문자열을 반환하고 churn/abuse 워커는 보조 소스 Error 관용 규약으로 계속 진행한다(동작 무해). 협의 종결 시 플래그 활성 + §4.4 I-8 행 🔴 해소로 반영. |
| v0.19.0 | 2026-07-31 | **[#148, C-18] I-22 `catalogVersion`을 필수 → 선택으로 완화하고 계약에서의 폐기를 제안한다.** 처음에는 "값 생성 주체가 잘못됐다"로 읽고 Spring → AI 이관을 구현했으나(AI가 `(행 수, max(updated_at))` 지문을 만들어 응답에 실음) **되돌렸다.** 물어야 할 것은 주체가 아니라 **필드의 존재 이유**였다. (1) *재현* 명분이 성립하지 않는다 — `products`는 I-17이 **제자리 upsert**하므로 그 시점의 임베딩이 남지 않는다. 버전 라벨이 가리키는 인덱스 상태가 이미 사라져 있어 라벨만으로는 아무것도 되살릴 수 없다. 스냅샷을 남기려면 7,220×1536 기준 **버전당 약 44MB**를 5분 주기(`catalog_batch_interval_s=300`)로 쌓아야 해 성립하지 않는다. (2) *재현이 필요하지도 않다* — 산출물(목록·`reason`)은 Spring이 `recommendation_generated`로 이미 저장하고(§3.7·§4.11) `listId` 귀속이 24시간 유지된다. "무엇을 추천했나"는 저장된 것을 읽으면 되며 랭킹을 다시 돌릴 이유가 없다. (3) *캐시 무효화* 명분은 TTL 10분과 중복이고, `max(updated_at)` 기반 지문은 상품 **1건** 갱신으로 전 회원의 P-5 캐시 키를 동시에 바꿔 **캐시를 오히려 죽인다**(5분마다 전체 무효화 → 홈 렌더가 매번 AI 호출). → 응답 필드와 `ArtifactStore.catalog_version()`을 제거했다. 요청 필드는 Spring 무변경을 위해 선택으로 남겨 받고 버린다. **🔴 잔여: BE 협의(필드 폐기) + 정본(Notion) 개정.** |
| v0.18.0 | 2026-07-31 | **[#148] 홈 추천 계약 등재 — I-22(§3.7)·P-5(§4.11)가 사본에 통째로 누락돼 있던 drift를 해소했다.** 정본(Notion「📡 API 명세서」) 2026-07-28 확정본을 대조해 옮겼다. (1) **§3.7 I-22 `POST {AI_SERVER}/internal/recommendations/home` 신설** — Spring → AI 위임 호출, `X-Internal-Token`, **연결 2s/응답 3s**(채팅 90s와 무관, 메인 렌더 블로킹 방지). 요청 `{memberId, limit, catalogVersion, signals{recentlyViewed·cart·recentPurchased}}`에서 `limit`은 **최종 노출 목표치**(AI는 품절 드롭 대비해 넉넉히 반환, Spring이 자름)이고 `recentPurchasedProductIds`는 **가중치가 아니라 제외 필터**다. 응답 `{outcome, recommendationRequestId, listId, items[{productId, reason}]}` — **배열 순서가 곧 순위**(`position` 없음), `listId`는 AI 생성 **≥128bit 무작위**(I-21 §4.2와 동일 규칙). **왕복 1회로 끝나며 I-21 콜백을 타지 않는다.** (2) **`outcome` 3종과 cold start 규약** — `PERSONALIZED`/`NO_PROFILE`/`INSUFFICIENT_CANDIDATES`를 **전부 200**으로 답하고 **fallback 판단은 Spring이** 한다. 프로필 부재·후보 부족으로 AI가 4xx/5xx를 내면 계약 위반이다. 단 **입력·인프라 실패용 4종은 존재한다**(`BAD_REQUEST`/`INTERNAL_TOKEN_INVALID`/`UPSTREAM_UNAVAILABLE`/`UPSTREAM_TIMEOUT`) — 이슈 #148 본문의 *"FastAPI가 4xx/5xx를 내지 않는다"* 는 **cold start에만 걸리는 서술**이라 여기서 범위를 명확히 했다. (3) **provenance 비노출 [HARD]** — 프로필 원문·prompt·모델 식별자를 응답·로그·trace에 싣지 않고, 알고리즘·모델 버전은 AI 자체 테이블 보관(평가 산출물 전용)이라 **`algorithmVersion`을 응답에 넣는 구현은 계약 위반**이다. (4) **§4.11 P-5 `GET /api/products/recommended` 신설**(레인 d, FE↔Spring) — Spring이 이를 서빙하려 I-22를 호출하는 **서브 관계**라 등재해야 의존이 드러난다. `source=AI_RECOMMENDED|POPULAR_FALLBACK`, fallback 시 상관키는 **Spring이 발급**(AI가 대신 만들지 않는다), `fallbackReason`·`cacheStatus`·`algorithmVersion`·`modelVersion`은 와이어 비노출. **캐시 TTL 10분 · `listId` 귀속 유효기간 24시간 확정(2026-07-30)** — 홈은 조회 API가 없어 CH-5의 *조회* 만료와 성격이 다르다. 정본 I-22 페이지에 남은 *"TTL은 BE 결정 필요"* 는 stale이며 BE 통보 대상. (5) **§1.2 레인 (b) 재정의** — 더 이상 "이벤트 채널"만이 아니다. I-22는 통지가 아닌 **동기 요청/응답**이라 §2.7 `/events/*` 멱등 규약이 적용되지 않으며 **재시도 = 새 `recommendationRequestId`·`listId` 발급**이다. 레인 (d)에 P-5, §2.3 (b)에 I-22 인증을 등재했다. (6) **🔴 C-18 신설 — `catalogVersion` 값 생성 주체 미해결.** 정본은 Spring이 실어 보내게 규정하는데 랭킹은 AI 자체 인덱스로 매긴다 — **Spring은 그 버전을 알 수 없어** 그 값으로 캐시를 키잉하면 AI 인덱스가 갱신돼도 캐시가 무효화되지 않는다. **정본 개정 + BE 합의가 #148 착수 전 필수**다. ※ 본 개정은 문서 등재만이며 구현은 #148, 재사용할 scoring baseline은 #145다. |
| v0.17.4 | 2026-07-31 | **[#196] I-13 계약 명문화 3건(코드 실측·jarvis-backend#62 연계).** (1) **`eventType` = CSV 직렬화 확정** — BE 컨트롤러가 `String eventType` + comma split(`parseEventTypes`)이라 구 반복 쿼리(`eventType=a&eventType=b`)는 Spring 암묵 변환에 의존했다. AI `spring_client.get_events`를 `",".join()` 명시 직렬화로 정렬. (2) **rows 정렬 명문화** — 활동량(counts 4종 합) 내림차순·동률 시 productId 오름차순(BE `eventsByProduct` 실측). AI 요약 상한(`seller_summary_max_products`, 기본 10) 초과분은 꼬리 합계로 요약(정보 소실 없음). (3) **⚠️ purchaseComplete 미귀속 명시** — FE 가 productId 없이 발사(주문 단위, `properties.orderId`만) → `product_id NULL` → product 조인 스코프에서 탈락, **상품별·합계 0 집계 가능**(실구매 존재해도). 구매 존재·규모의 권위는 I-6/I-7/I-14. 근본 수정(order_item 기반 귀속)은 **jarvis-backend#62** — BE 배포 후 경고 완화 예정. |
| v0.17.3 | 2026-07-31 | **[#209 후속] 노출 상한을 목록당 8→9로 상향하고, 니즈별 발화가 목록 여러 개로 나가는 경로를 명문화했다.** (1) **§3.3 degrade 개수 `5~8` → `5~9`** — §4.2가 목록당 9개를 허용하는데 노출 상한만 8에 머물러 있어 계약 상한이 코드에서 도달 불가능한 값이었다. 이 개수는 **목록 하나 기준**이며 목록이 여럿이면 목록마다 걸린다. (2) **니즈별 추천(`PICK_ONE` × N)이 실제 발신 경로가 된다** — §4.2 `listType` 표가 이미 규정한 세 모양 중 `PICK_ONE`+N("유럽여행 필요한 거" → 파우치 후보 / 어댑터 후보)을 AI가 실제로 보내기 시작한다. **§4.2 필드·상한·오류 조건은 그대로이며 계약 변경이 아니다** — 종전에도 계약상 허용되던 형태를 구현이 쓰지 않고 있었을 뿐이다. `lists` 상한 10·목록당 9·`listId` 중복 금지는 불변. (3) **`products.ready.listIds`가 실제로 길이 2 이상이 될 수 있다** — §3.1이 v0.15.26에서 이미 배열로 전환했고 FE는 배열 전제로 구현돼 있어 FE 변경은 없다. **부수 정정**: 문서 버전 셀이 v0.17.2 개정 때 갱신되지 않아 v0.17.1로 남아 있던 표기를 바로잡았다. |
| v0.17.2 | 2026-07-31 | **[#114] 옵션 후보가 1개뿐이면 되묻지 않고 자동 선택해 담는다 — AI 측 동작 명확화.** §4.1 `CART_OPTION_REQUIRED` 행의 "AI 동작"이 예외 없이 "되묻는다"로 읽혀 코드와 어긋나 있었다(PR #211 리뷰). **와이어 계약은 불변** — 엔드포인트·요청/응답 스키마·SSE 이벤트 타입·필드·오류 코드 어느 것도 바뀌지 않는다. FE는 `CART_OPTION_REQUIRED`(AI↔Spring 내부 코드)를 관측할 수 없고, 담기 턴이 되물음 `token` 또는 결과 `action` 중 하나로 끝나는 문법도 그대로다 — 바뀐 것은 **AI가 둘 중 무엇을 택하는지의 정책**뿐이다. 다만 **BE는 관측한다**: 400 직후 같은 요청이 `optionId`만 채워져 한 번 더 온다(자동 선택 재호출은 1회 고정, 재차 REQUIRED면 되물음으로 복귀). §3.1 되물음 서술에도 같은 단서를 달았다. 로직 상세는 `docs/specs/SPEC-CART-001.md` v0.2.5 REQ-CART-026·027. |
| v0.17.1 | 2026-07-31 | **[#209] I-21 다중 목록 정합 — 사본 §4.2가 정본(Notion I-21, 2026-07-28~30 개정)의 구 형식에 머물러 있던 드리프트를 해소했다.** (1) **요청 최상위를 `lists[]` 배열로 전환** — 구 평평한 3필드(`listId`·`productIds`·`reasons`)는 폐기한다. 목록이 1개여도 길이 1 배열이다. 니즈별 추천(파우치·어댑터 각각의 후보)과 세트 여러 안(조합 A·B·C)은 목록 하나로 표현되지 않는다. (2) **`recommendationRequestId` 신설** — 추천 실행 1회를 가리키는 opaque id(FastAPI 생성, ≤36자). 노출·클릭·담기·주문을 그 추천에 귀속시키는 조인 키로, `listId`와 **역할이 달라 서로 대체하지 않는다**(이슈 #140의 상관키와 같은 대상). (3) **`listType` 신설**(`PICK_ONE`/`BUY_ALL`, 항상 전송) — 목록 안 상품들이 대체재인지 보완재인지를 나타내며 세 모양(`PICK_ONE`+1=일반, `PICK_ONE`+N=니즈별, `BUY_ALL`+N=세트 복수안)이 이 한 필드로 표현된다. **판단 기준은 예산이 아니다** — "감자탕 재료"는 예산이 없어도 `BUY_ALL`, "5만원으로 파우치"는 예산이 있어도 `PICK_ONE`. 목록 개수는 `lists` 길이로 알 수 있어 싣지 않지만 `listType`은 개수로 복원할 수 없어 서버가 말해줘야 한다. (4) **`totalBudget`·`lists[].label` 신설** — `BUY_ALL`일 때의 예산 상한과 목록 이름(세트 성격 "알뜰"/니즈 이름 "파우치", ≤50자). 이 셋은 표시 필드가 아니라 **목록 성격 메타**이며 표시 권위는 그대로 Spring에 있다(경로 B 유지) — `products.ready`는 싣지 않고 CH-5가 나른다. (5) **목록당 상품 상한 Top5 → 9개**(2026-07-30 확정), `lists` 1~10개, `reasons` ≤9·`reason` ≤200자. (6) **멱등 키 = (`recommendationRequestId`, `listId`) 쌍** — 단독 키로 쓰면 한 실행의 두 번째 이후 목록이 중복으로 잘못 버려진다. 한 콜백 안 `listId` 중복은 400. (7) **실패 응답표·"실패가 아닌 것"표 등재** — `listId` 허용 문자(영숫자·`-`·`_` ≤64자, Redis 키 오염 방지)·만료 `sessionId`의 익명 저장(CH-5 미조회, fail-closed)·`HIDDEN`/품절은 CH-5 시점 드롭 등 정본 규약을 옮겼다. (8) **`listId` TTL 10분 확정**(🔴 C-9 잔여 해소) — 세션이 sliding으로 연장돼도 목록 TTL은 생성 시점 고정이며 만료 시 CH-5 404, FE는 카드 스냅샷 폴백. (9) **`recommendation_generated`는 Spring이 server-side 적재** — FastAPI가 E-1로 같은 이벤트를 보내지 않는다(E-1은 무인증이라 분모 조작 가능, 양쪽 기록 시 이중 계상). §3.3 경로 B 다이어그램과 §5.1 C-9 행도 함께 정정했다. **사본 자기모순 해소** — §3.1(`listIds` 배열, v0.15.26)이 "I-21이 `lists`를 1~10개 보내므로(§4.2)"라고 §4.2를 인용하는데 정작 §4.2에 `lists`가 없던 상태였다. **jarvis-back은 이미 신 형식 구현 완료**(`RecommendationCallbackRequest.resolvedLists()`가 구 형식을 과도기 수용 중이며 FastAPI 전환 후 제거 예정)이므로 **코드 전환(`RecommendationPush`)이 후속**이다. |
| v0.17.0 | 2026-07-31 | **[#187] signed `sessionId` 기반 stable `context_id`와 guest→member claim, D6/I-20 lifecycle을 확정했다.** `/events/session-claim`의 strict BIGINT/서비스 토큰 계약, claim 뒤 old guest의 turn/thread 미생성, guest transcript와 member profile 입력 격리, 재시작 가능한 backfill과 단조 grace(최소 24시간), PostgreSQL clock 기준 durable quiet(최소 90초이자 stream timeout 이상), exact legacy late-write reopen 및 destructive GC gate를 명시했다. 외부 출시는 BE #63 signed-ticket 증거, 90초 drain, FE #52 실제 3-tab 검증, 운영 지표 확인 뒤에만 완료로 본다. |
| v0.16.3 | 2026-07-30 | **[#164] I-4 주문 상태 문의 구현 계약 정합.** 구매자 라우팅을 `recommend`/`cart_add`/`cart_view`/`order_status`/`general` 5-way로 확장하고 §4.10을 신설했다. JWT-derived member identity, `GET /internal/members/{userId}/orders/status?recent=3`, internal token/3초 timeout, literal-success envelope, aware timestamp와 Spring 어휘/canonical pair의 전체 payload 검증, 최대 3개 주문·주문당 3개 상품 및 `외 N개`, empty와 dependency degradation 구분, `token`→`done(stop)` 정상 종료, privacy-safe correlated route log, 일반 대화 보존과 response-derived state non-copy 경계를 확정했다. §1.2·§4의 reverse-call 현황도 정확한 17건 `{I-1,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}`로 정정했다. |
| v0.16.2 | 2026-07-30 | **[#194] I-14/I-15 응답 스키마 BE 실측 확정 + I-6 이상 감지 규칙 명문화.** (1) **I-14 `order-events` 응답 확정** — `rows`/`total`/`byStatus`/`cancelReasonsTop` (shape 상호 배제: 목록/stats/groupBy=memberId). 구 추정 스키마(`events`/`stats`)는 BE에 없는 필드라 AI 도구가 **항상 0건**을 반환하던 버그의 원인(`extra="allow"`가 검증 실패를 은폐). (2) **I-15 `product-changes` 응답 확정** — `rows[{productId,productName,changeType,oldValue,newValue,createdAt}]`+`total` (구 `logs` 폐기, 동일 패턴 버그). (3) **I-6 이상 감지 규칙 명문화(BE `SellerSalesService.withAnomaly` 실측)** — 직전 최소 3(MIN_WINDOW)·최대 7(MOVING_WINDOW)포인트 평균 대비 ±30%, 기준선 0 + 매출 발생 = 이상(`deviationPct` null), **매출 0 포인트는 이상 아님**(저볼륨 무판매일 -100% 노이즈 방지). AI `calc.detect_sales_anomalies`를 동일 규칙으로 정렬. (4) I-14/I-15 `limit`(기본 100) 쿼리 등재 — `rows`는 절단본, 전수는 `total`. |
| v0.16.1 | 2026-07-30 | **[#167] I-21 `listId` 보안 규약 복원.** CH-5가 인증 불필요 공개 조회라 `listId`가 사실상 bearer 키인 점을 명시하고, FastAPI가 **UUID급 무작위(≥128bit)** 로 생성하며 순번·타임스탬프 등 추측 가능한 형식을 금지하도록 §4.2를 확정했다. 실제 I-21 예시의 `list-4471`을 32자리 무작위 hex로 교체하고 C-9·Q2의 형식 미확정 표기를 해소했다. 현재 `uuid4().hex` 구현을 형식·고유성·I-21/SSE 동일성 회귀 테스트로 고정했다. |
| v0.16.0 | 2026-07-30 | **[정본 SPEC-CHAT-SESSION 반영] `sessionId`(접속) · `threadId`(방) 축 분리 — MVP의 `sessionId == threadId` 전제 폐기.** 한 접속 아래 여러 방이 **동시에** 존재하는 멀티탭 대화를 지원하기 위해 두 식별자의 역할을 갈랐다. (1) **§2.6 식별자 모델 신설** — 축별 발급 주체·수명·담당 상태를 표로 확정. `sessionId`=Spring CH-1 발급(Redis TTL 10분 sliding)·프로필 세션버퍼·I-20·`conversation_turns.conversation_id`(primary), `threadId`=**FE 생성**(서버 왕복 없음)·필터 누적·장바구니 pending·되돌리기·동시 스트림 락·`conversation_turns.thread_id`. 구 정의 *"만료 의미 없는 불투명 스레드 키"* 를 **폐기** — "스레드 키"는 이제 `threadId`의 것이고, AI가 만료를 판정하지 않는 이유는 만료가 **없어서**가 아니라 **판정 주체가 Spring이라서**다. (2) **§2.9 a 동시 스트림 락을 세션→방 단위로 개정** — `409 STREAM_IN_PROGRESS`의 판정 키가 `sessionId`에서 **`threadId`** 로 바뀐다. 세션 단위로 잠그면 탭 B가 탭 A의 스트리밍 때문에 409를 맞아 **축 분리의 목적이 정면으로 무효화**된다. §2.5 오류표도 동기화. (3) **§3.5 I-20 사유를 `logout` 1종으로 축소** — 새 대화가 CH-1을 부르지 않고 `threadId`만 갱신하게 되어 `newConversation`이 발화되지 않는다. Spring이 I-20을 쏘는 경우는 로그아웃뿐이고 나머지는 Redis TTL 만료 + AI 내부 비활동 sweep이 담당한다(C-8 행 동기화). (4) **[D5] CH-1 멱등 등재 + 구 "CH-1 재호출 = 새 세션(맥락 단절)" 경고 폐기**(§1.2 레인 d) — Spring이 Redis `SETNX`로 기존 세션을 그대로 반환하므로 CH-1을 몇 번 불러도 세션은 하나다. **정확성은 `SETNX`가 책임지고 FE Web Locks(D1)는 최적화**다(한 브라우저 안에서만 통해 폰·PC 동시 접속을 막지 못한다). 축출을 없앤 뒤에는 밀린 세션이 CH-1b로 TTL을 연장하며 유령으로 남아 I-20이 안 나가는 문제가 생기는데 이를 `SETNX`가 막는다. **예외 = 게스트 첫 방문 멀티탭**(쿠키 부재 → 게스트 2명 생성 → 밀린 탭이 CH-1b `403`)은 신원이 갈라지는 것이라 `SETNX`로 막을 수 없어 Web Locks가 방어한다. (5) **[D6] 맥락 TTL을 방→접속 단위로** — 어느 방에서든 활동이 있으면 그 `sessionId`의 **모든 방** TTL을 함께 연장하고 세션 종료 시 일괄 정리한다. 방마다 생사가 갈리면 탭을 옮겼을 때 한쪽 맥락만 사라져 사용자가 이해할 수 없다. (6) **§6.3 저장·로그 축 정합** — checkpointer thread 키를 `sessionId`→**`threadId`** 로 정정하고, `conversation_turns`를 **session-primary + `thread_id` 병기**로 명시(세션 종료 스캔은 세션 축, 방별 조회·정리는 방 축이라 어느 한쪽만으로는 불가). 구조화 로그에 **`threadId` 필드 신설** — 멀티탭이면 한 `conversationId` 아래 여러 방 로그가 섞여 방을 못 가리면 동시 스트림을 분리해 읽을 수 없다. **🔴 잔여**: `SETNX` 멱등 키 스코프(`sub` vs `sub_type`+`sub`)와 멱등 반환 시 세션 TTL sliding 갱신 여부 — BE 확인 대기. |
| v0.15.27 | 2026-07-30 | **[사본 drift 정정] 정본 대조로 틀린 서술 3건 교체.** (1) **담기 이벤트 적재 주체** — §4.1 I-2의 *"`CART_ADD(via: chat)` 이벤트는 BE가 적재(AI 무관)"* 와 §5.1 Q9의 같은 답변을 **폐기**했다. E-1 정본에서 `add_to_cart`는 **FE가 쏘는 12종 중 하나**이고, 서버가 직접 적재하는 이벤트는 `recommendation_generated` 하나뿐이다(E-1 HTTP로 들어오면 드롭). 챗봇 경로도 FE가 SSE `action`(`CART_ADDED`) 수신 시점에 쏜다. (2) **`budget` 이벤트 제외** — 정본(Notion CH-2)이 *"현재 코드에 미구현 → 명세에서 제외(필요 시 post-MVP)"* 로 정리했는데 사본은 §3.1에 스키마를 그대로 두고 이벤트 순서 계약에도 넣어두고 있었다. 스키마는 post-MVP 참고용으로 남기고 순서 계약에서 뺐다(이슈 #163). (3) **공통 헤더 규약 §2.5 신설** — `X-Request-Id`·`traceparent` 는 전 API 공통이라 엔드포인트 행 단위인 정본 DB에 놓을 자리가 없었다. Notion「프로젝트 자료실」에 **공통 규약 페이지를 신설**하고 본 사본 §2.5에 AI 소관 요약을 넣었다(#141·#134·#151). 실측: inbound `X-Request-Id` **수용 미구현**(`request_context_middleware`가 `new_request_id()`를 조건 없이 호출) · Spring 역호출 **전파 미구현**(`X-Internal-Token` 하나만) · 응답 echo 는 구현됨 · `traceparent` 는 코드베이스에 없음. (4) **`search.query` PII 기준** — 정본 E-1이 *"개인정보를 properties에 넣지 않는다"* 와 *"`search` 필수 = `query`"* 를 동시에 말해 **자기모순**이었고, FE가 그 금지 조항을 근거로 `queryLength`만 보내 `searchTopics` 워커가 돌 수 없었다. 금지 대상은 **FE가 굳이 끌어다 넣는 이름·주소·연락처·이메일**이며 사용자가 직접 입력해 이미 서버로 보낸 검색어는 원문을 싣고 **보존기간으로 관리**한다 — 정본 E-1에 「개인정보 기준」 절로 명확화했다. |
| v0.15.26 | 2026-07-28 | **[사본 동기화] §3.1 요청 계약 확장 + in-stream `error` 추적 필드 — 정본(Notion "📡 API 명세서" CH-2) 2026-07-28 개정 반영.** (1) **`conditionActions` 신설**(이슈 #84) — 조건 칩 제거를 `[{op:"remove", field}]` 구조화 배열로 받는다. 구 규약 문자열(`"[조건 제거] priceMax"`) 왕복 방식은 **폐기** — FE는 그 방식으로 구현돼 있으나 AI에 수신부가 없어 현재 칩 제거가 무동작이다. `conditionActions`가 있으면 `message` 빈 문자열 허용, 둘 다 비면 `400`. 구매자 전용(`BuyerChatRequest`). (2) **`conditions` 칩 `field` 허용값 6종 확정** — `category`/`priceMax`/`priceMin`/`brand`/`ratingMin`/`keyword`. 종전에는 예시 둘만 있어 허용 집합이 계약에 없었는데, `conditionActions.field` 검증의 전제라 등재했다(코드 `build_condition_chips` 실측과 일치). (3) **`screen` 신설**(이슈 #118) — `{pageType, filters?, products?, columns?}`. `pageType`은 **라우트가 아니라 우측 패널 내용**을 가리킨다(채팅이 전용 페이지에만 있어 라우트를 실으면 정보가 0). `products`는 **서버가 모르는 목록만**(P-4 인기상품·판매자 자사 상품) — 추천 카드는 `listId`로 서버가 알고, 되돌려주면 위조 경로가 된다. `columns`는 반응형 그리드 열 수로 "3번째 줄 2번째" 좌표 지시 해소에 쓴다(`rows`·항목별 좌표는 파생값이라 제외). `pageType`은 **E-1 `page_view`와 같은 enum을 공유**한다 — 화면 어휘를 새로 만들지 않기 위해서다. 라우트 `path`는 쿼리스트링 PII 위험으로, 한글 `label`은 AI config 매핑으로 대체해 **계약에서 뺐다**. 07-17 FE 제안(`ChatScreenContext`)과 #118의 "노출 상품 목록" 요구를 **한 필드로 통합**했다(같은 사실의 두 측면). `products`는 담기 허용 목록을 넓히는 입력이며 **두 목록 밖 id 차단 가드는 유지**한다. **구매자·판매자 공용 필드**라 §3.2에도 등재 — 판매자 대시보드는 `meta.lane`·`done.panel`로 AI→FE 화면 조작만 있고 반대 방향이 비어 있었다. (4) **`products.ready`의 `listId`(단일) → `listIds`(배열, 항상)** — I-21이 `lists`를 1~10개 보내므로(§4.2) 단일 필드로는 세트형·니즈별 추천을 나를 수 없었다. 정본 I-21·CH-5는 이미 복수 전제인데 CH-2와 본 사본만 단일로 남아 있던 **3자 불일치**다. 목록이 1개여도 길이 1 배열로 보내 FE 분기를 없애고, 이벤트는 여전히 정확히 1회다. 예시의 `"list-4471"`도 §4.2가 금지한 추측 가능 형식이라 교정했다. **구현(`ProductsReadyData.list_id: str`)도 단일이라 코드 변경이 따라야 한다.** (5) **in-stream `error`에 `requestId`·`retryable` 추가** — 스트림 전 실패(§2.5 봉투)에는 `requestId`가 있는데 스트림 내부 실패에는 없어 추적이 끊겼다. `retryable`은 `code`로 복원 불가(같은 `LLM_UNAVAILABLE`이 미구성/일시불가에 겸용)라 emit 지점이 정한다. §3.2 판매자 스트림도 동일(`ErrorData` 공용). |
| v0.15.25 | 2026-07-28 | **[#171] I-1 응답에 `reviewCount` 추가 — `rating=0`의 의미 판별.** `rating`만으로는 **"리뷰가 없어 0"(데이터 부재)** 와 **"리뷰가 있고 하한 미달"(진짜 저평점)** 을 가를 수 없어, BE가 `reviewCount`를 함께 제공하기로 합의했다(2026-07-28). `reviewCount=0`이면 데이터 부재, `>0`이면 실제 저평점이다. `null`/미전송이면 `rating`이 지배(구 동작 폴백). **AI 계산용(비표시)** 이며 표시용 리뷰수는 여전히 경로 B(§4.3)가 채운다 — 이에 따라 #100의 *"`reviewCount`는 표시전용·I-1 미반환"* 을 **부분 개정**해 미반환 목록에서 제외했다(§4.6). ※ 이 행은 v0.16.0 병합 시 소급 등재했다 — 원 커밋(de9f34f)이 버전 헤더만 올리고 이력 행을 남기지 않아 §6.2 규약(개정은 이력에 남긴다)에 어긋나 있었다. |
| v0.15.24 | 2026-07-27 | **[사본 동기화] S-5 폐기 반영 — 정본(기획 저장소 Notion "📡 API 명세서" DB) 2026-07-21 결정이 본 사본에 미반영이었다.** S-5 `PATCH /api/seller/products/{id}`(판매자 화면 직접 수정, 07/17 신설)는 **미채택**이며 **상품 수정은 챗봇 HITL(I-11)이 유일 경로**다. §3.2 draft 절의 "챗봇 수정(I-11)과 병존" 서술을 폐기 표기로 교체. Spring 코드 실측에서도 `/api/seller/**`에 PATCH 엔드포인트가 없어 정본·코드 모두와 일치시켰다. 계약 변경이 아니라 **사본 drift 정정**이다. |
| v0.15.23 | 2026-07-27 | **[#100 P0/P1/P2] I-1 §4.6 실측 정합.** 표시 전용 필드(`imageUrl`·`originalPrice`·`reviewCount`·`options`)를 응답표에서 제거하고 CH-5(§4.3) 하이드레이션 이관 명시(AI 추천 경로 미사용), `price`·`rating`을 "AI 계산용(비표시 — 예산검증 `verifiedSum`·평점 사후필터·rerank 신호, 질의 시점 필요)"으로 명기해 display 오분류 재발 차단, envelope 예시를 실측 `{success, data:[...]}`(bare array)로 정정, 요청 `brandName` 단일→다중(반복 파라미터 → `WHERE brand IN`, 방법 D), `totalCount` 필드 불필요 결정 반영. |
| v0.15.22 | 2026-07-26 | **[#100 P1] I-1 `color` 요청 파라미터 연결.** decompose가 색상 조건("빨간"·"검정")을 `filters.color`로 추출·전송하고, BE I-1이 `attributes` LIKE로 필터. 요청 모델·쿼리 변환에 `color`가 없어 Spring 색상 검색을 못 쓰던 것을 해소. |
| v0.15.21 | 2026-07-24 | **[#100 P2] I-1 `size` 제거 → 라운드1 전량 반환 + AI top-K.** BE 합의로 Spring 요청에서 `size`를 제거(반환 상한 없음)하고, 결과 수 제한(top-K)은 AI `search_catalog`가 사후필터(dedup·평점) 뒤 `filters.limit`로 절단. `ProductSearchFilters.limit`은 Spring `size`가 아니라 AI 후보 상한(rerank 입력)이다. |
| v0.15.20 | 2026-07-27 | **[C-1 해소] 인증 계약을 Spring 코드 실측으로 확정.** 명세가 🔴 협의 대기로 남겨둔 항목이 BE에는 이미 구현돼 있어 역반영한다. (1) **스트림 티켓 클레임 실값 확정** — `iss`=`jarvis-spring-auth`, `aud`=`jarvis-fastapi-ai`, `scope`=`chat:stream`, TTL **60초**(구 "30~60초" 범위 → 실값). CH-1/CH-1b 응답이 `ticketTtlSeconds`로 실값을 함께 반환한다. (2) **판매자 티켓 형식 확정** — `role="seller"`(**소문자**) + `brandId`(**숫자**). `role` 클레임은 **판매자 티켓에만** 실리고 구매자·게스트는 `sub_type`만 갖는다. (3) **CH-1b `POST /api/chat/tickets` 구현 확인** — 구 "가칭·신설 필요"를 확정으로 전환. 요청 `{sessionId}`, 응답은 CH-1과 동일 DTO. 세션에 보관된 `sub_type`+`sub`으로 **소유자를 검증**하고(불일치 거부), 재발급과 함께 세션 TTL을 sliding 갱신하며, 판매자 세션은 보관된 `brandId`로 SELLER 스코프 티켓을 재발급한다. (4) **CH-6 `POST /api/chat/seller/sessions` 등재**(레인 d) — 판매자 챗 입구. `brandId`는 BE가 JWT 검증 후 DB에서 도출해 클레임에 박는다(클라이언트·LLM 주장 무시). (5) CH-1 응답 필드에 `llmSseUrl`(FE→AI SSE 직결 주소) 명시. **AI 측 와이어 동작 변경 없음** — `_norm_role`이 대소문자 무관 비교라 소문자 `seller`도 기존대로 매칭된다. C-1 🔴 잔여는 **서비스 토큰 회전·만료·mTLS 운영 정책**만 남는다. |
| v0.15.19 | 2026-07-23 | **[이슈 #79] 프로필 세션 종료 트리거의 MVP 소유권 확정.** Spring I-20의 알려진 사유는 `logout`/`newConversation` 2종으로 한정하고 탭 닫기 신호는 제거한다. AI는 회원 발화 저장 시 DB 서버 시각의 `lastActivityAt`을 갱신하며, 단일 인스턴스 스케줄러가 기본 60초마다 10분 이상 비활성 세션을 bounded batch로 선점한다. 내부 timeout은 HTTP 자기 호출 없이 I-20과 같은 finalizer 및 고정키 claim으로 직렬화하되, idle 성공은 영구 멱등 완료가 아닌 재개 가능한 checkpoint로 claim을 해제한다. 새 활동은 completed activity를 active로 되돌리고 이전 processing/completed 종료 generation을 같은 저장 transaction에서 무효화한다. terminal finalizer는 처리 중 새 activity를 영구 완료로 덮지 않으며 scheduler는 라이브 스트림 슬롯을 점유하지 않는다. 처리 전 활동·활성 스트림을 재확인하며 claim별 실패·crash는 해제/lease로 재시도한다. 외부 요청 스키마에는 변경이 없다. |
| v0.15.18 | 2026-07-23 | **[C-4/I-17 상태 계약 정합] `items[].status`를 Spring `ProductStatus`와 동일한 `ON_SALE`/`HIDDEN`으로 확정.** Spring은 별도 매핑 없이 enum 값을 그대로 반환하고, AI 배치는 `ON_SALE`을 생성·갱신하며 `HIDDEN`의 기존 artifact를 삭제한다. 구 `ACTIVE`/`DELISTED`를 포함한 미정의 값은 응답 계약 위반으로 페이지 전체를 fail-closed 처리한다. 해당 항목만 skip하지 않고 커서를 유지해 Spring 수정 뒤 같은 `since`부터 재처리한다. §4.8 응답 예시·필드 설명·배치 흐름·복구 규약 갱신. |
| v0.15.17 | 2026-07-22 | **[이슈 #62/#64] I-20 실측 계약과 실패 안전 멱등 lifecycle 확정.** 요청을 `{sessionId,userId(number BIGINT),reason?}`로 정렬하고 `eventId`·`endedAt` 제거. `userId`는 양의 정수만 엄격히 허용하고 enum 미강제 `reason`은 최대 64자로 방어한다. 검증 → `(userId,sessionId)` `PROCESSING` claim(token+lease) → 버퍼 처리 → 성공 시 `COMPLETED` 순서다. 첫 빈 버퍼 통지는 `202 accepted`, 활성/완료 동일 통지는 `202 duplicate`. delta/consolidation 실패·취소는 버퍼 보존+claim 해제, crash/해제 실패 claim은 lease 만료 후 재선점한다. Spring PR #24 송신 계약과 정합하며 C-8/Q1 해소. |
| v0.15.16 | 2026-07-22 | **[C-3 재개정] 담기 재고검증 부활 — BE I-2 `CART_STOCK_INSUFFICIENT` 신설(2026-07-22).** 합산 수량 > 재고 시 `400 CART_STOCK_INSUFFICIENT` + `error.detail.availableStock`(남은 재고, 재고는 상품 단위). AI는 `action` `CART_ADD_FAILED` + `reason: "STOCK_INSUFFICIENT"` + message에 남은 재고 수 노출("재고가 N개뿐이에요"; 재고 0=품절은 "품절된 상품이에요"). 담기 실패 **3종**(`PRODUCT_NOT_FOUND`/`STOCK_INSUFFICIENT`/`CART_ERROR`) — v0.15.5 "담기 재고검증 없음·OUT_OF_STOCK 폐기"를 뒤집음(품절=stock 0이 아니라 "재고 부족=N개 남음"이라 신규 코드 채택, `OUT_OF_STOCK`은 폐기 유지). 수량 상한(합산 > 99)은 `VALIDATION_ERROR`로 별개 → `CART_ERROR` + BE 동일 문구 "수량은 최대 99개까지 담을 수 있습니다.". **이 파일을 계약 정본으로 승격**(외부 사본 의존 폐기). |
| v0.15.15 | 2026-07-22 | **[C-9/Q2 확정] I-21 `reasons` 콜백 포함 확정(🔴 역제안→🟢).** BE가 §4.2 명세대로 구현(2026-07-18) — 추천 `reason`을 SSE 직접이 아니라 **I-21 콜백 `reasons[{productId, reason}]`에 포함**해 Spring이 Redis 저장 후 CH-5 카드에 echo. 구 BE 07/17 안(reason=SSE·콜백 불포함) 폐기. `reasons`는 선택 필드·productId 키잉(부분집합/순서무관). §4.2 필드표·주석·C-9·Q2 마커 🟢 갱신. AI→Spring 전송분은 jarvis-ai 이슈 #61에서 구현. 정본(기획 repo) 동기화 완료(2026-07-22). 잔여 🔴: `listId` TTL·형식(C-9). |
| v0.15.14 | 2026-07-20 | **[임베딩 모델 확정] 셀프호스트 torch → Google `gemini-embedding-001` API.** dim 1024→1536(MRL; pgvector 표준 인덱스 ≤2000 적합·1536 L2 정규화), $0.15/1M(배치 $0.075), 결정 6 개정. dragonkue·torch·`--group embedding` 폐기. 임베딩 단계 외부 API 호출 전환(search_doc·쿼리 텍스트 Google 전송). text-embedding-004 폐기(2026-01-14). |
| v0.15.13 | 2026-07-20 | **[#7 결정] I-17 배치 MVP 편입 + 임베딩 검색 방식 확정.** §4.8 OPEN 해소: 방식1·2를 `SearchBackend`로 둘 다 구현해 골든셋 확정(착수=방식2 라이브+방식1 오프라인 랭킹, BE 무대기). **C-17 신설** — 방식1 라이브용 I-1 id 제약 조회 BE 요청 🔴. config.py "post-MVP"→MVP 정정. |
| v0.15.12 | 2026-07-20 | **[C-4 골격 확정] I-17 상품 변경 배치 — BE "상품 정보 Batch" Notion 대조(2026-07-18).** 인증 `X-Internal-Token`(Bearer 아님)·envelope `{success,data}`·`productId` 숫자 BIGINT·오류(`INVALID_CURSOR`/`INTERNAL_TOKEN_INVALID`/`FORBIDDEN`)·`since="0"`·`hasMore` 루프 확정. C-4 🔴→🟡: 주기=AI config·페이지=`limit`(500)·커서=opaque라 무영향. 잔여 3건(커서 형식·`attributes`·리뷰) 저영향 → 소비 언블록. 스코프(MVP?)는 별건. |
| v0.15.11 | 2026-07-20 | **[C-16 해소] I-18 장바구니 조회 확정 — BE "챗봇 장바구니 조회" 문서(2026-07-18).** `productName`/`optionName` 필수 포함·`CART_QUERY_INVALID`(400)·경로·쿼리·인증 확정 → C-16 🟢. §4.9 필드표·협의 반영. |
| v0.15.10 | 2026-07-19 | **[C-6 해소] I-19 `categoryName` 추가 — BE 확정(라이브 Notion 2026-07-19).** I-19 items[]에 `categoryName`(string) 포함. 소모품 카테고리 억제·되돌리기 칩(결정 14-F) 구현 언블록(jarvis-ai 완료). 소모품 판정은 AI-side(MVP config·catalog 속성사전). |
| v0.15.9 | 2026-07-19 | **[C-6] I-19 `categoryName` 추가 요청 공식화(LLM팀 → BE).** 결정 14-F 소모품 카테고리 억제·`suggestions.revert.category` 칩(§3.1)은 구매 상품 category 가 유일 소스인데 I-19 items 에 없어(§4.7 갭) 구현 불가. **요청: I-19 items[]에 `categoryName`(string) 추가**(I-1과 동일 필드). exact productId dedup(#4) 구현 완료, 카테고리 억제만 이 확정에 의존. 계약 변경 아님. |
| v0.15.8 | 2026-07-19 | **잔재 청소 + 옵션 스키마 확정 반영(BE Notion 대조).** (1) **OUT_OF_STOCK 잔재 제거** — §3.1 reason·§4.1 C-3 잔여·Q7의 `OUT_OF_STOCK`을 **폐기(v0.15.5 결정)**로 정리 → 담기 실패 **2종**(`PRODUCT_NOT_FOUND`/`CART_ERROR`). (2) **[C-3 해소] 옵션 스키마 확정**(BE 2026-07-18) — `error.detail.options:[{optionId,name,extraPrice}]`, OPEN-CART-2 해소. (3) C-3 잔여 정리 후 서비스 토큰 발급만 🔴. 계약 자체 변경 없음. |
| v0.15.7 | 2026-07-19 | **[§3.1] `sessionId`/`threadId` 길이 상한 명시** — config `chat_key_max_chars`(기본 200자), 초과 시 400. 불투명 키가 registry·대화저장소·로그에 쌓이는 남용 방어(#8 리뷰). |
| v0.15.6 | 2026-07-19 | **[§3.1] `message` 길이 상한 명시** — 최대 = config `chat_message_max_chars`(기본 4000자), 초과 시 `400 BAD_REQUEST`. PII·메모리 방어(`/chat`·`/seller/chat`). 이슈 #8(대화 저장) 리뷰에서 코드가 상한을 도입하며 계약이 실질 변경됨 → "명세 개정 먼저" 규칙에 따라 정본에 반영(코드는 config 주입, 하드코딩 금지). |
| v0.15.5 | 2026-07-19 | **BE DB DDL(MariaDB) 대조 + 판정규칙 확정(사용자).** 판정규칙 = **API 표면(method·경로·파라미터·status enum·error code)은 Notion, 데이터 타입(id 숫자 BIGINT·guest UUID·배송비 없음·camelCase)은 DDL**. (1) **[C-15 확정] I-1 = GET 그대로 수용**(POST 역제안 폐기) — Notion 파라미터 `keyword·categoryName·minPrice·maxPrice·brandName·size≤30`. **`excludeProductIds`·`ratingMin`·`sort` 파라미터 없음** → dedup·평점·정렬은 **AI 사후필터**로 이동. 응답 = `{success,data:{items[...]}}`(BE I-1), `stock`·`totalCount` 미제공(estCount 소스 🔴). (2) **[C-5 해소] `attributes` 구조 확정** — 축 = `category.attribute_schema`(키 배열), 값 자유텍스트(DDL D7·D11). (3) **[C-3 해소] 담기 재고검증 없음** — DDL상 재고 차감=주문 시점, SOLD_OUT 미도입(품절=stock 0), `OUT_OF_STOCK` 담기 오류 폐기. (4) **[C-6 정정] I-19 `status`=6종**(`PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED`, Notion) — 구 `representativeStatus` 8종은 O-3(FE `/api/orders`) 오용이라 폐기. (5) **Notion stale 통보 대상**: I-19 페이지 snake_case·문자열 id·`shipping_fee:3000` (DDL 기준 숫자·camelCase·배송비 0 우선). 타입 확정(모든 PK BIGINT·guest CHAR(36))은 v0.15.3/4와 일치. |
| v0.15.4 | 2026-07-18 | **v0.15.3 stale 참조 정리(팀원 PRD/SPEC-CART 검토 중 발견).** (1) 필드표 `productId`/`optionId` `string`→`number`, `guestId`=UUID 문자열 명시(§4.1·§4.5·§4.6·§4.7) — §2.6 숫자 개정을 하위 표까지 전파. (2) **C-5** "SSE/FE 경계 문자열 정규화" 서술 폐기 — 구매자 SSE는 productId 미탑재(경로 B), 코드 int 정렬 완료(v0.15.3). (3) **I-19 구매이력 경로 표기 통일**: 구 별칭 `GET /orders/recent`(§1.2 등) → `GET /internal/members/{id}/orders`(§4.7 BE 실측) 5곳. (4) **CLAUDE.md(정본·hk-final 미러)**: 정본 버전 v0.7.0→v0.15.3, 인증레인 정정("장바구니만 서비스토큰·나머지 JWT 포워딩" → AI→Spring internal은 전부 `X-Internal-Token`), cart 조회 `I-9`→`I-18`. 이슈#3 제목 동기화. (5) **§4.6 I-1 `excludeProductIds` `string[]`→`number[]` + JSON 예시 문자열 id 교정**(§4.1 I-2·§4.5 draft/I-9·§4.6 검색 req/resp·§4.9 I-18의 `productId`/`optionId`) — I-19가 숫자 productId를 반환하므로 **dedup 제외 목록도 숫자라야 exact 제외가 성립**(문자열대로 구현 시 BIGINT와 불일치로 조용히 실패). 계약 자체 변경 없음(참조 정합만). |
| v0.15.3 | 2026-07-18 | **productId·id 타입 = DB 스키마 기준 BIGINT 확정(사용자).** §2.6 개정: 상품/옵션/장바구니/주문 id = **숫자(BIGINT)**, **게스트 id만 UUID 문자열**(guest.id CHAR(36)). 구 "경계별 문자열 정규화·전 구간 문자열" 규칙 폐기. **CLAUDE.md(정본·hk-final) "productId 전 구간 string" 규칙도 개정.** 코드 반영: `schemas/spring.py`·`chat.py`의 상품/옵션/장바구니/주문 id → `int`, `guest_id`·`get_cart` → `str`(UUID), dedup 비교 타입 정합(SpringProduct·excludeProductIds → int). BE I-17 예시가 문자열 productId를 보이나 DDL은 BIGINT — 스키마 기준 int(BE 표기 불일치 통보 대상). (Claude PR 리뷰가 CLAUDE.md 불일치를 지적해 해소.) |
| v0.15.2 | 2026-07-18 | **BE 확인질문 Q2·Q4·Q6 초안 반영(결정 대기).** (1) **Q6 해소**: 관리자 CS 문의(CH-3·I-5·AD-1/2·M-9) 전부 **post-MVP**. **주문상태 Q&A(I-4)는 구매자 챗(CH-2)에 흡수** — 별도 CS챗 없음(§1.2 레인 c·§3.1). (2) **Q2 초안(역제안)**: 추천 `reason`을 **I-21 콜백에 포함**(`reasons[{productId, reason}]`)해 Spring이 CH-5 카드에 echo(§4.2·§4.3) — BE 07/17 제안(reason=SSE)에 대한 역제안. 경로 B 일관·FE join 불필요, SSE(`products.ready`)는 상관키만. `listId` TTL 10분(config). BE 확정 🔴. (3) **Q4 초안**(§4.8): 커서 "수정시각+id" 수용(불투명 취급)·`attributes` 자유 dict·리뷰 텍스트 MVP 제외. 🔴 선결: I-17 배치 MVP/post-MVP 스코프(config vs 파이프라인 모순). §5.1 Q2/Q4/Q6 갱신. |
| v0.15.1 | 2026-07-18 | **I-13 행동 이벤트 조회/집계 본문 확정 — LLM팀 재작성 + BE Notion 반영.** BE 확인질문 Q5 해소: I-13(`GET /internal/seller/{brandId}/events`) 페이지에 I-5(문의 접수) 내용이 복붙돼 있던 것을 LLM팀이 `behavior_events` 기반 스펙으로 재작성해 **Notion 페이지에 직접 반영**. 요청(`from`/`to`·`eventType` 4종·`productId`·`groupBy`) + 응답 3형태(product/eventType/date, `counts`·`viewToCartRate`·`uniqueVisitors`) + 집계 규칙(판매자 스코프=`product→brand.seller_id`, camelCase, `client_event_id` 중복 배제, purchaseComplete는 행동 맥락용·매출 권위는 I-6/I-14). §4.4 I-13 행·§5.1 Q5·C-13 갱신. 판매자 집계 나머지 4종 응답 스키마는 C-13 잔여. |
| v0.15.0 | 2026-07-17 | **BE 07/17 API·ERD 개정 반영(확정분) — Notion "📡 API 명세서" 실측 + "API·ERD 변경 정리(07/17)".** (1) **I-21 `POST /internal/recommendations` 신설**(§4.2 재작성, C-9 확정) — 추천 목록 push가 `{sessionId, listId, productIds[Top5]}`로 확정. **`listId`는 FastAPI 생성**(구 Spring 생성 가정 폐기), `reason`은 SSE 직접(콜백 불포함), **콜백 성공 후에만 `products.ready`**. 구 groups/items/reason 구조 폐기. (2) **CH-5 `GET /api/chat/lists/{listId}` 신설**(§4.3 재작성, C-12) — 추천 카드 조회(구 P-7 대체), FE↔Spring, 카드 스키마 FE/LLM OPEN. (3) **I-19 본문 재작성**(§4.7) — camelCase·숫자 id·`shippingFee` 항상 0·`representativeStatus` enum 8종·item `status` 6종(교환 제거)·**`category` 필드 없음**(dedup은 `productId` 기준). (4) **CH-2 경로 = `{AI_SERVER}/chat`**(오타 수정), **I-20 = `{AI_SERVER}/events/session-end`**(Spring→AI inbound — 우리가 호스팅하는 엔드포인트임을 명확화). (5) **[BE DB] `productId` = 숫자(BIGINT) 확정** — internal(AI↔Spring) 계약은 숫자, SSE/FE 경계서 문자열 정규화(§2.6 개정). (6) **[BE DB/ERD] `product` +`stock_quantity`(시드 100)** — I-2 `OUT_OF_STOCK` 실재화(§4.1), `user_event`→`behavior_events`(+guest_id +client_event_id), `order_status_logs`·`product_change_logs`·`account_event_logs` 신설, **배송비 0원·교환 제거(주문상태 11→9종)**. (7) 신규 FE/Spring 엔드포인트 **E-1**(`POST /api/events` 행동 수집)·**CH-6**(`POST /api/chat/seller/sessions`)·**S-5**(`PATCH /api/seller/products/{id}` 판매자 직접 수정 — 챗봇 I-11과 병존) 등재. (8) **LLM팀 확인질문 9개**(§5 말미 표) — 세션 UUID 수용·I-21 확정·[적용] 형식·I-17 커서·I-13 재작성·CS챗 라우팅·게스트 담기·판매자챗 주소·담기 이벤트. |
| v0.14.0 | 2026-07-16 | **구매 이력 = I-19, 세션 종료 = I-20 — BE DB No. 채번 확정(사용자 승인 후 Notion 수정).** (1) **구매 이력 조회 = I-19 `GET /internal/members/{id}/orders`**(BE DB "구매 이력 목록" 행에 No.·그룹·Method·경로 채움) — §4.7·C-6·레인 c 정합, 구 `/orders/recent` 제안 폐기. `{id}`=userId(AI 도출), 서비스 토큰. I-4(주문 상태 요약)와 별개. (2) **세션 종료 = I-20 `POST /events/session-end`**(BE DB 행에 No. 채움) — §3.5·C-8 정합. Notion BE DB 실제 수정(사용자 승인). |
| v0.13.0 | 2026-07-16 | **BE API 명세 DB(Notion) 실측 정합 — 인증 레인 서비스 토큰 통일 + 실제 I-number/경로 — 사용자 확정.** (1) **[BREAKING] AI→Spring 역호출 전 구간 `X-Internal-Token` 서비스 토큰 + 본문/쿼리 신원**(AI가 JWT `sub` 도출)으로 통일 — 구 "사용자/판매자 JWT 포워딩"(후보 검색·구매 이력) 폐기. BE `internal` 그룹이 전부 서비스 토큰이라 정합(IDOR는 AI 도출 신원으로 유지). (2) **실제 BE 번호/경로 반영**: 후보 검색 = **I-1 `GET /internal/products/search`**(구 `POST /products/search`), 생성물 배치 = **I-17 `GET /internal/products/changes`**, 장바구니 조회 = **I-18 `GET /internal/cart`**. (3) **구매자 챗 경로 = `POST /ai/chat`**(구 `/chat`, BE DB 실측·인증 필요). (4) **S-3 정정**: `GET /api/seller/products`(SELLER·FE용) ∥ I-9(internal·AI용) **별개** — 구 "S-3=I-9" 오기 정정. (5) **후보 검색 GET vs POST 역제안 🔴**: 복잡 필터(배열·중첩)라 POST 바디 역제안. (6) **C-6 구매 이력**: BE DB 미등재 → AI팀 신규 요청 필요(I-4는 주문 상태 요약). C-4/C-15/C-16 = I-17/I-1/I-18로 확정. |
| v0.12.0 | 2026-07-16 | **CH-1 스트림 티켓 발급 + 재발급 경로 명시 — 사용자 확정.** 스트림 티켓 발급을 전제 계약(§1.2 레인 d)에 구체화: (1) **CH-1**(`POST /api/chat/sessions`) 응답에 `sessionId`(10분 sliding) + **첫 `streamTicket`**(RS256, TTL 30~60s) 반환. 신원은 회원 AT/게스트 쿠키로 확인(`sub_type`). (2) **[중요] 티켓 재발급 경로 신설 필요(CH-1b `POST /api/chat/tickets` 제안)** — 티켓 TTL(30~60s) ≪ 세션 TTL(10분)이라 CH-1 1회로는 첫 스트림만 커버, 2번째 메시지부터는 세션 유지한 채 티켓만 재발급해야 하며 **CH-1 재호출은 새 세션(맥락 단절)이라 못 씀**. (3) `401` 재발급 흐름을 CH-1b로 명확화. (4) 레인 d에서 구 "draft 적용=FE S-3 PATCH" 제거(v0.11.0 정합). CH-1/CH-1b는 Spring 소유 — 경로·응답 🔴 C-1. |
| v0.11.0 | 2026-07-16 | **판매자 쓰기 모델·HITL 계약 확정(쟁점 B) — 사용자 확정.** (1) **채팅 경로 쓰기 = AI가 internal API(I-10/11/12) 직접 수행 + HITL 승인 게이트**(v0.9.0 확정). 판매자가 FE에서 직접 편집하는 경로는 FE↔Spring 별개(AI 표면 밖). (2) **`S-3` = 자사 상품 목록 조회(=I-9, 읽기)** 로 명확화 — 구 S-4 문서의 "S-3 PATCH"는 오표기, brandId=JWT 클레임(userId와 동일 IDOR 원칙, 쟁점 A 확정). (3) **HITL 2-스트림 계약**: 스트림1 `draft{draftId,op,changes}` → LangGraph interrupt → done / 스트림2 `confirm{draftId}` → resume → I-11 등 실행 → done. (4) **HITL 안전장치 5종**: draftId 바인딩·명시 액션만·멱등성·Spring 소유권 하드게이트·대기 TTL. 삭제 필수 HITL + soft delete(HIDDEN). (5) `draft` 이벤트에 `draftId`·`op` 추가. 승인 이벤트명·confirm 형식은 🔴 판매자 SPEC. |
| v0.10.0 | 2026-07-16 | **SSE 인증 = 스트림 단명 티켓("JWKS 검토 후 제안" 채택) — 사용자 확정.** SSE에 로그인 AT를 직접 싣지 않고, Spring이 채팅 진입 시 신원 확인 후 **스트림 단위 단명 JWT(RS256, TTL 30~60초)** 를 발급(CH-1에 얹음). 게스트는 `guest_id` 쿠키로 동일 발급(`sub_type: guest`). 클레임 재편: `sub`+`sub_type`(member/guest)+`iss`(jarvis-spring-auth)+`aud`(jarvis-fastapi-ai)+`scope`(chat:stream)+`exp`. 검증에 **`aud`·`scope` 추가**(토큰 혼용 방지). JWKS는 `kid` miss 시 refetch. 판매자 티켓의 role/brandId 표현은 🔴 확인. §2.3·§2.5·C-1 개정. (JWKS 코어(RS256/kid/엔드포인트)는 기존 반영분 유지.) |
| v0.9.0 | 2026-07-16 | **판매자 BE internal API 배치 전면 반영(11종 PDF) — 사용자 확정.** (1) **판매자 조회/집계 7종**(§4.4): I-6 sales·I-7 funnel·I-13 events·I-16 churn·I-14 order-events·I-15 product-changes(brandId path) + I-8 account-events(전역). (2) **판매자 상품 CRUD 4종**(§4.5, C-14 재정의): I-9 목록·I-10 등록·I-11 수정·I-12 삭제(soft=HIDDEN). 구 "I-7 상세 읽기 + FE S-3 PATCH" **폐기**. (3) **[BREAKING] 판매자 쓰기 모델 전환**(§3.2): "FE가 본인 JWT로 S-3 PATCH 반영(AI 표면 밖)" → **"AI(`product_agent`)가 Spring internal API로 직접 쓰기 + 파괴적 작업은 HITL interrupt/resume 승인 게이트"**. "대화 발화 ≠ 동의" 원칙은 유지(HITL로 구현). soft delete(status=HIDDEN). (4) 전부 `internal`·`X-Internal-Token`·`{brandId}`(JWT 클레임). (5) **혼동 주의**: BE I-15 product-changes ≠ C-4 products/changes(생성물 배치), BE I-14 order-events ≠ C-6 orders/recent(구매자 이력). (6) 판매자 서브에이전트 다수화(sales_anomaly·conversion·behavior·churn·abuse·general·recommend·chart·product). **결정 20 개정 필요**(§8 항목 8). 응답 스키마·I-number 정합·HITL 이벤트는 #9. |
| v0.8.0 | 2026-07-16 | **판매자 `brandId` = JWT 클레임 + 집계 API 5종(BE 문서) — 사용자 확정.** (1) **§2.3 클레임에 `brandId` 추가**(role=seller 시 필수). (2) **§2.6 `brandId 미보유` 원칙 개정** — "AI는 brandId를 알지 못한다(Spring 내부 해소)"에서 **"brandId를 요청 본문에서 받지 않는다 — 검증된 판매자 JWT 클레임에서만 획득"** 으로 완화(IDOR 방지 취지 유지, RS256 위조 불가). BE 집계 API가 `{brandId}` path를 요구함에 따른 정합. (3) **§4.4 재정의** — 판매자 집계는 단일 `/seller/aggregates`(폐기)가 아니라 **brandId 스코프 집계 5종**: I-6 `sales`·I-7 `funnel`·I-13 `events`·I-16 `churn`(brandId path) + I-8 `account-events`(전역·admin). 전부 `internal`·`X-Internal-Token`. (4) **C-13 재정의**(5종), **C-1**에 brandId 클레임 발급 협의 추가. (5) **BE I-number ≠ 기존 임의 I-number**(BE I-7=funnel vs 기존 I-7=상세) — 정합은 #9. 상품 CRUD·주문 PDF 반영은 후속. |
| v0.7.0 | 2026-07-15 | **스트림 운영 규약 신설 — 사용자 확정(7개 항목).** (1) **§2.9 신설**: 동시 스트림 세션당 1개(`409 STREAM_IN_PROGRESS`, 기존 스트림 유지 — 409 거절안 채택), 취소 = 연결 종료(FE `AbortController` → disconnect 감지 → **LLM 스트림 즉시 close**·LangGraph task 취소), 타임아웃 기준표(first-token 10s / 스트림 상한 90s / AI→Spring 3s 통일 / LLM 30s+1재시도 — config 기본값, 계약은 초과 시 동작). (2) **§2.5 확장**: `409`·`504 UPSTREAM_TIMEOUT` 추가 — 스트림 전 오류 통합표化. (3) **§2.8 레이트 리밋 확정**: 목적 = 무분별 남용 차단, FastAPI 미들웨어 + in-memory(다중 인스턴스 시 Redis 이관 단서), 분당 10/시간당 100(config), **C-11 축소**(잔여 = 허용 오리진만). (4) **§6.3 신설(운영 요구)**: 대화 저장(수신 즉시 user 저장 / 완료 후 assistant 저장 / `COMPLETED`·`FAILED`·`CANCELLED`, 부분 텍스트 보존), 로그 필드(requestId·userId·conversationId·first-token/total latency 분리·model·tokens·errorType, message 원문 로깅 금지). FE 히스토리 복원 API는 미결로 등재. |
| v0.6.0 | 2026-07-15 | **[BREAKING] 장바구니 계약 BE I-2 문서 채택 + 조회 신설 — 사용자 확정.** (1) **§4.1 재작성**: `POST /internal/cart/items` 단건 + `X-Internal-Token` 서비스 토큰 + 본문 신원(AI-검증 JWT `sub` 유래) — 구 v0.3.0 제안(사용자 JWT 포워딩·`items[]` 다건) **폐기**, 묶음은 반복 호출. (2) **게스트 담기 허용**(BE 02 D30) — AI-side 차단·`GUEST_NOT_ALLOWED` 폐기, 로그인 유도는 결제 시점 FE 몫. **결정 8 개정 필요(§8 항목 7)**. (3) **옵션 되물음 멀티턴**: `400 CART_OPTION_REQUIRED`(options 목록 포함) → 실패 `action` 없이 `token` 재질문 → `optionId` 해석 후 재담기; `CART_OPTION_INVALID`는 1회 재시도 후 `CART_ERROR`. (4) **`action.reason` 재편**: `PRODUCT_NOT_FOUND`/`CART_ERROR`/`OUT_OF_STOCK`(I-2에 재고 코드 부재 — 🔴 협의). (5) **장바구니 조회 신설(§4.9, C-16)**: `GET /internal/cart` — 장바구니 질의 응답(`token` 텍스트) + 담기 시 기존 보유·수량 합산 안내(합산 권위는 Spring, 조회 실패 시 담기 진행). (6) 레인 (c) 6건→7건. C-3 재작성. |
| v0.5.1 | 2026-07-15 | **[정정] AI 생성물 저장 존속 + pull 배치 부활 — 용어 오해 정정.** v0.5.0의 "enrichment/임베딩 채택 안 함"은 오독이었음 — 채택하지 않는 것은 **상품 원본 컬럼의 AI측 사본**뿐. **AI 생성물(extras·search_doc·임베딩 벡터)은 AI Postgres에 저장·유지**(결정 3 Layer 2/3·결정 6 존속), 갱신은 **pull 배치**(`GET /products/changes?since={cursor}`, §4.8 신설, **C-4 부활**; Spring 주기 push 기각). **질의 시점 후보 흐름은 OPEN**(§4.8 말미) — 방식 1(AI 벡터 → Spring id 제약 조회) vs 방식 2(Spring 검색 → 임베딩 보조) 병행 검토, hk-final `SearchBackend`로 교체 가능 구현 후 골든셋/실측 확정. §8 항목 4 정정(결정 3/6 효력 유지). |
| v0.5.0 | 2026-07-15 | **[BREAKING] 검색 위임 영구 확정 + 주문 알림 폐기 — 사용자 최종 확정.** (1) **후보 검색 = 질의 시점 Spring 위임(`POST /products/search`, §4.6, C-15 신설)** 을 유일·영구 경로로 확정 — AI 카탈로그 사본(미러)·pgvector 카탈로그 벡터 검색·enrichment/임베딩·bulk export 배치(이원 주기)는 **채택하지 않음**(고도화 유예 아님, C-4 폐기). 구현 기준 = `~/projet/hk-final`(jarvis-ai) 스캐폴드. (2) **주문 알림(구 `POST /events/order`)·주문 미러 폐기** → **질의 시점 구매 이력 조회(`GET /internal/members/{id}/orders`, §4.7, C-6 재정의)** — dedup(14-F 동작 불변: exact 제외·소모품 억제·되돌리기 칩)·프로필 구매 소스 공용, 게스트 스킵, 실패 시 dedup 없이 degrade. (3) Spring → AI 이벤트는 **`/events/session-end` 1종만** MVP 유지(병행 PRD 라인과의 유일한 차이 — PRD 정정 필요, §8 항목 6). (4) §1.2 레인 재편 — AI→Spring 질의 시점 6건. 가격 신선도 트레이드오프 소멸(질의 시점 검색·조회). SPEC 후속: RECOMMEND-001 검색 tool·CATALOG-DATA-001 재범위·PROFILE-001 구매 소스(§7). |
| v0.4.0 | 2026-07-15 | **판매자 확대(Batch 1) + 카탈로그 배치 전환(Batch 2), 2026-07-15 사용자 확정.** **(Batch 1)** `POST /seller/chat` 범위 확대 — 통계 Q&A **+ 상세 수정 draft 흐름**(§3.2). 판매자 SSE = `token`/`draft`/`done`/`error`만, `finishReason`=`stop` 단일. 통계 원천 = **Spring 집계 I-6 질의 시점 콜백**(§4.4) → **C-7 해소**, 구 결정 20 기본안(주문 미러 sellerId·금액 확장) **폐기**. draft = **I-7 상세 읽기**(§4.5) → LLM 개정안 → SSE `draft`{productId(string), changes:[{field,before,after}]} → FE diff 카드 → FE가 Spring **S-3 PATCH**로 반영(FE↔Spring 전제, AI 표면 밖). 대화 발화는 동의 아님. `brandId`는 AI 미보유(Spring이 sellerId→brandId 해소). 신규 역호출 I-6/I-7 인증 = 판매자 JWT 포워딩 제안(🔴). §1.2 레인 갱신(AI→Spring 질의 시점 = 장바구니·목록 push·I-6·I-7). **(Batch 2)** `POST /events/catalog` **완전 폐기** → **`GET /products/changes?since={cursor}` bulk export 배치 폴링**(§4.6, 제안 🔴). **이원 주기**(가격·재고 짧은 주기 미러 UPDATE / 콘텐츠 긴 주기 재임베딩, contentHash 비교) — 결정 9-A 경량/전체 분기를 이벤트→배치로 이식. 배치가 곧 동기화(일 1회 보정이 백업 아님). 신선도 트레이드오프(필터 경계 오류) 수용 명시. **`/events/order`·`/events/session-end`는 MVP 유지**(이벤트 폐기는 카탈로그 한정). **(C-table)** C-4 재정의(bulk export 🔴), C-6 4필드 유지 확정, C-7 해소(I-6), C-13 I-6 신설(🔴), C-14 I-7 신설(🔴), 나머지 v0.3.0 유지. **[provenance]** 본 버전은 **미비준 병렬 초안**(no-mirror + 질의 시점 `POST /products/search` + `/events/*` 고도화 유예 + 판매자 AI DB 시드; 가칭 결정 22/23/24)을 **폐기·대체**한다 — 비준 노선은 **미러 + 배치 동기화**(본 세션). SPEC 동기화 개정 목록은 §7. |
| v0.3.0 | 2026-07-15 | **[BREAKING] FE/BE 팀 챗 API 문서("추천 챗봇 CH-2")를 명명 기준으로 채택 + 상품 목록 경로 B 도입.** (1) SSE 이벤트 집합 재편(`text.delta`→`token`, `products` 카드 삭제, `conditions`/`action`/`products.ready` 신설, `done.finishReason`=`stop`/`zero_result`, in-stream `error` 4종). 전 페이로드 **camelCase**. (2) 경로 B: SSE 상품 카드 제거, AI→Spring 목록 push(C-9) + FE←Spring 목록 GET(C-12), point 조회 삭제. (3) 인증 확정(RS256+JWKS, `401` 통일, `role`, `CHAT_SESSION_EXPIRED` 폐기). (4) `productId` 문자열 전면 통일(숫자 예시와 상충 → 양팀 통보). (5) 장바구니 `action` + JWT 포워딩 + 실패 4종. (6) suggestions/relaxationNotice/budget SSE 측 탑재. SPEC-RECOMMEND-001 §5.3 / SPEC-PROFILE-001 §5.4 동기화 개정 필요(§7). |
| v0.2.0 | 2026-07-14 | [BREAKING] FE 직접 호출 아키텍처 반영. 사용자 대면 API를 FE → AI 직접 호출로 전환. 요청 본문에서 `user_id`·`seller_id` 제거 — 토큰 클레임 추출. 인증 2종 분리, 401 만료 재발급·SSE 연결 시점 인증, CORS·레이트 리밋 신설, `GET /profile/{user_id}` → `GET /profile/me` IDOR 방지. |
| v0.1.0 | 2026-07-14 | 최초 작성. `/chat`(SSE)·`/seller/chat`(최소판)·`GET /profile/{user_id}`·`/events/{catalog,session-end,order}` 제공 API와 장바구니·point 조회 요구 계약 정의. 🔴 항목 10건(C-1~C-10) 등록. |

### 6.3 운영 요구 — 대화 저장·로그/모니터링 (AI 서버 내부) [v0.7.0 신설]

외부 계약이 아닌 **AI 서버 내부 운영 요구**다(FE/Spring 협의 불필요). 2026-07-15 사용자 확정. PRD·소유 SPEC에 비기능 요구로 편입한다.

#### (a) 대화 저장 규약

저장소 = LangGraph checkpointer(AI Postgres, **`threadId` = checkpointer thread 키** — **[개정 v0.16.0]** 축 분리 전에는 `sessionId`가 thread 키였다).

**[v0.16.0] `conversation_turns`는 session-primary + thread 병기** — 턴 행은 `conversation_id = sessionId`(primary)로 적재하고 `thread_id = threadId`를 **함께** 남긴다. 프로필 파이프라인의 세션 종료 스캔은 `conversation_id` 축이라야 한 접속의 여러 방 발화를 한 버퍼로 모을 수 있고(§2.6), 방별 조회·정리는 `thread_id` 축이 필요하다. 어느 한쪽만으로는 두 요구를 동시에 만족할 수 없다.

| 시점 | 저장 대상 | 상태 |
|---|---|---|
| 사용자 메시지 **수신 즉시** | user 메시지 원문 | — |
| 스트리밍 **완료 후** | assistant 응답 전문 | `COMPLETED` |
| 스트림 실패(in-stream `error`·LLM 재시도 소진) | 부분 생성 텍스트 | `FAILED` |
| 클라이언트 취소(§2.9 b) | **부분 생성 텍스트 보존** | `CANCELLED` |

- `FAILED`/`CANCELLED`의 부분 텍스트도 다음 턴 컨텍스트·프로필 스캔(결정 4-A sleep-time)에 포함한다.
- FE 채팅 히스토리 복원(`GET /chat/history` 류)은 **미결** — 지원 결정 시 이 저장소를 원천으로 계약 신설.

#### (b) 로그/모니터링 필드 (요청 단위 구조화 로그)

| 필드 | 비고 |
|---|---|
| `requestId` | §2.5 오류 봉투와 동일 키 — 전 구간 상관관계 |
| `ownerFp` / `role` | JWT `sub`의 peppered HMAC 지문과 역할. raw `userId`/`guestId` 금지 |
| `sessionFp` | `sessionId`(접속)의 peppered HMAC 지문 |
| `threadFp` | `threadId`(방)의 peppered HMAC 지문 |
| `streamFp` | 내부 `owner:thread` stream key의 peppered HMAC 지문(수명주기 로그) |
| `scopeFp` / `scopeType` / `ipFp` | 429 스코프와 IP의 peppered HMAC 지문 및 비민감 유형(`sub`/`ip`). raw scope/IP 금지 |
| `latencyFirstToken` / `latencyTotal` | SSE 2분할 — 체감 응답성 vs 전체 시간(§2.9 c 기준 대비) |
| `model` | 호출 모델 id(Haiku/Sonnet, 노드별 다중 기록) |
| `promptTokens` / `completionTokens` | LLM 호출별 합산 |
| `errorType` | in-stream `error` 코드·`FAILED` 사유·타임아웃 구간 |
| `streamStatus` | `COMPLETED` / `FAILED` / `CANCELLED` (a와 동일 enum) |

- **PII/식별자 정책**: 사용자 message 원문과 raw owner/session/thread/stream 식별자는
  **로그에 남기지 않는다**. message는 길이·peppered HMAC, 식별자는 위 `*Fp`만 기록한다.
  rejection 로그의 추가 필드는 명시 allowlist만 허용하며 Authorization/token/exception과
  사용자 입력 원문은 폐기한다. 원문은 (a) 대화 저장소에만 존재한다.
- 레이트 리밋(§2.8)·409(§2.9 a) 발동도 `errorType`으로 집계해 상한값 튜닝 근거로 쓴다.

#### (c) 개인화 그래프 변경 감사 로그 (§3.9) **[v0.22.0 신설, 🔴 제안(초안)]**

사용자가 자기 취향을 고치고 지운 기록이다. **파괴적 동작이 추적 가능해야** 하고, 동시에 그 기록 자체가 개인정보 저장소가 되어서는 안 된다.

| 필드 | 비고 |
|---|---|
| `requestId` | §2.5 오류 봉투·(b) 로그와 동일 키 |
| `actorFp` | 변경 주체(회원 id)의 peppered HMAC 지문. **raw `userId` 금지**((b)와 동일 기준) |
| `action` | `edgeUpdate` \| `edgeSuppress` \| `edgeRestore` \| `graphReset` \| `personalizationToggle` |
| `edgeIdBefore` / `edgeIdAfter` | 수정으로 `edgeId`가 바뀌는 경우 양쪽을 남긴다(§3.9.1). 삭제·복구는 한쪽만 |
| `predicate` | 변경 후 관계(§3.8 enum). **고정 enum이라 그대로 기록한다** |
| `objectFp` | 대상 노드 라벨의 peppered HMAC 지문 |
| `graphVersionBefore` / `graphVersionAfter` | 변경 전후 버전. 재전송 판정·순서 재구성용 |
| `createdAt` | 변경 시각 |

- **[HARD] 노드 라벨·fact 원문·근거 원문을 저장하지 않는다.** 취향 대상 라벨 자체가 민감할 수 있어(예: 특정 질환 관련 상품군) 변경 전후 값을 텍스트로 남기면 (b)의 peppered-지문 원칙과 §3.8의 원문 미노출 [HARD]를 감사 로그가 우회하는 구멍이 된다. 판매자 상품 변경 이력(I-15)이 `oldValue`/`newValue`를 텍스트로 갖는 것과 **의도적으로 다르다** — 그쪽은 상품 데이터고 이쪽은 개인 취향이다.
- **[HARD] 감사 로그는 와이어에 노출하지 않는다.** 사용자에게 편집 이력을 보여주는 API는 라벨 원문을 요구하므로 위 원칙과 충돌한다 — 필요해지면 **별도 계약**으로 다루며 §3.8/§3.9 범위 밖이다.
- **전체 초기화(§3.9.4)는 감사 로그를 지우지 않는다** — 초기화가 일어났다는 기록이 사라지면 파괴 동작이 추적 불가가 된다. 보존 기간은 config 주입이며 **재전송 판정 원장(§3.9 파생 키)의 보존 기간보다 짧지 않아야 한다**(재전송이 항상 최초 응답을 찾을 수 있어야 한다). 정확한 기간은 🔴 C-23.
- 개인화 중지·재개가 **상태를 바꾸지 않는 경우(no-op)에는 감사 행을 남기지 않는다**(§3.9.5) — 같은 값 반복 전송이 이력을 오염시키지 않게 한다.

---

## 7. 후속 SPEC 동기화 개정 목록 (Follow-up SPEC Amendments)

본 개정의 명명 기준 채택·경로 B·판매자 확대로 아래 SPEC들이 **정렬이 깨졌다.** 본 문서는 SPEC을 편집하지 않으며, 아래를 후속 동기화 개정(sync amendment) 대상으로 등록한다.

### 7.1 `SPEC-RECOMMEND-001` §5.3 (SSE 페이로드 스키마) — 개정 범위

- **이벤트명 교체**: `text.delta` → `token`; `products`(SSE 카드) → **삭제**(경로 B로 이관, `products.ready` 신호만 SSE).
- **필드 camelCase 전환**: `finish_reason`→`finishReason`, `product_id`→`productId`, `verified_sum`/`within_budget`/`dropped_items`/`feasibility_notice`→camelCase, `est_count`→`estCount` 등 전부.
- **`done.finishReason` 값**: `completed`→`stop`, `zero_result` 유지.
- **`error.code` 집합 교체**: `DECOMPOSE_FAILED`/`RERANK_FAILED` 등 스테이지 코드 → `LLM_TIMEOUT`/`LLM_UNAVAILABLE`/`SEARCH_FAILED`/`INTERNAL`(rerank 실패는 여전히 `done` degrade).
- **`ProductPayload` 이관**: `products` 카드 스키마는 SSE에서 제거되고, `productId`+`rank`+`reason`만 목록 push(§4.2)로 이관, 표시 필드는 Spring enrich(§4.3). EX-5/AC-REC-10 정신 유지·강화.
- **AC 갱신**: 이벤트 순서 `text.delta→products→done` → `token→products.ready→done`; "`products` 정확히 1회" → "`products.ready` 정확히 1회".
- **주의**: 서브그래프 동작·불변식(decompose 1회, rerank 상한, 하드 제약, degrade 정책)은 불변.

### 7.2 `SPEC-PROFILE-001` §5.4/§6.9 (`GET /profile/me`) — 개정 범위

- **경로**: `GET /profile/{user_id}` → `GET /profile/me`(IDOR 방지, 결정 19).
- **필드 camelCase**: `ProfileViewResponse` `user_id`/`generated_at` → `userId`/`generatedAt`. `exists`/`markdown` 유지.
- **구매 소스 개정 [v0.5.0]**: write 소스 "주문 이벤트 미러 스캔"(결정 16) → **질의 시점 구매 이력 조회(`GET /internal/members/{id}/orders`, §4.7)** 호출로 교체(sleep-time 배치). 게이트·델타 동작은 불변.
- **응답 구조·동작 불변**: 위 항목 외 스키마·REQ(PROF-081 등) 변경 없음.

**[v0.22.0 추가 — #149 개인화 그래프]** `SPEC-PROFILE-001`(v0.6.0 → v0.7.0)에 아래 개정이 필요하다. 모델·규칙·인수 기준의 소유는 신규 `SPEC-PROFILE-GRAPH-149`이며, `SPEC-PROFILE-001`은 기존 조항과의 정합만 맞춘다.

- **EX-P3 배제 범위 한정**: "프로필 편집은 고도화 범위" → **`PUT /profile/me` 마크다운 전문 편집**으로 한정. 항목(edge) 단위 제어는 §3.9로 제안됨(전문 편집은 계속 배제).
- **EX-P6 충돌 아님 명문화**: EX-P6이 v2로 미룬 것은 **temporal knowledge graph 백엔드·그래프 추론·파라메트릭 편집**이다. §3.8은 저장 모델을 바꾸지 않는 **1-hop 읽기 투영**이며 **graph DB는 도입하지 않는다**(#149 비범위와 동일). 다홉 순회·추론이 없다는 잔여를 명시적으로 남긴다.
- **REQ-PROF-034 적용 범위 한정**: "fact 폐기 대신 supersede, 삭제 금지"는 **기계(파이프라인) 경로**를 구속한다. 사용자 개별 삭제는 tombstone(suppress)이라 삭제가 아니고, **사용자가 명시적으로 요구한 전체 초기화는 기계 자동 처리가 아니어서 이 금지의 대상이 아니다**(§3.9.4, 감사 §6.3 c로 추적). 금지되는 것은 *기계가 사용자 데이터를 조용히 지우는 것*이다. 대응 AC도 "기계 경로 한정"으로 좁히고 suppress·reset 인수 기준을 신설한다.
- **REQ-PROF-012 스코프 한정**: confidence **수치** 미노출 규정은 `profile_summary`(프롬프트 입력) 대상이다. §3.8은 **3버킷 라벨만** 노출하므로 수치 불변식은 유지된다 — 축소된 번복으로 기록한다.
- **요약 반영 조건**: 요약·랭킹은 게이트 통과·미폐기 fact 중 **suppress되지 않은 것**만 반영한다. **consolidation이 그래프를 읽어야** 이 조건이 실효한다(§3.8 구현 노트 3).
- **개인화 중지 전파**: reader는 중지 회원에게 프로필을 반환하지 않고, 수집(버퍼 적재·델타 추출·"기억해" hot-path·consolidation)도 중단한다. 단 세션 finalizer는 버퍼 정리·완료 처리를 계속해 라이프사이클이 멈추지 않게 한다.
- **저장 모델**: 그래프 투영 원천을 위한 구조화 트리플 산출을 consolidation의 책임으로 추가하고, store item 값에 tombstone 시각·트리플 필드를 더한다. **OPEN-P12는 해소가 아니라 우선순위 상향** — #149 계약이 이를 #150의 **선결 조건**으로 만들었음을 기록한다.
- **OPEN-P10 부분 해소**: "GET 노출 범위"의 넓은 투명성 뷰는 §3.8 그래프가 답하고 §3.4 마크다운은 현행 유지다. 마크다운 조립 범위 자체는 계속 TBD.

### 7.3 이벤트 채널 SPEC 정합

- `/events/session-end` HTTP 계약은 본 문서 소유(결정 21). 개정 시 소비 SPEC의 필드명(camelCase) 전제와 정합을 확인한다. **수신 후 동작**은 소비 SPEC 소관 불변. (주문 알림은 v0.5.0에서 미채택 — §3.6·§4.7.)
- **카탈로그 동기화 참조 정합 [v0.5.0]**: 카탈로그 변경 이벤트·배치 동기화·AI 사본이 모두 채택되지 않으므로(§4.6 말미), 카탈로그 동기화·3계층 메타데이터·임베딩을 참조하는 SPEC-RECOMMEND-001(검색 tool을 pgvector 단일 SQL로 규정한 조항)·SPEC-CATALOG-DATA-001(enrichment→임베딩→적재 단계) 문구는 **질의 시점 Spring 위임(§4.6)에 맞춰 후속 개정/재범위**가 필요하다(§8 항목 4 연계).

---

## 8. product.md §12-A 결정과의 정합 — 사용자 확인 필요 항목 (결정 개정 필요 목록)

본 개정은 product.md의 여러 binding 결정과 긴장/상충한다. **product.md는 편집하지 않으며**, 아래를 **결정 개정 필요 항목(신규/개정 결정 레코드 대상)** 으로 등록한다. product.md 결정 로그는 현재 **결정 21**까지 있으며, 본 문서가 한때 참조한 결정 22/23/24는 **미비준 병렬 초안 소산으로 폐기**되었다(§6.2). 아래 9항목이 실제 필요한 개정이다.

### 항목 1 (상충 — 신규 결정 레코드 필요) — 경로 B: point 조회 폐기 + AI→Spring 역방향 예외 증가

결정 9-B(binding)의 "Spring 유일 접촉 = point 조회" 구체 문구와 경로 B(목록 push + 목록 GET)가 상충한다. 원칙(표시 권위 = Spring, AI 인덱스 표시 필드 미보유)은 유지·강화되나, 단방향 원칙의 AI→Spring 역방향 예외가 **장바구니 1건 → 장바구니·목록 push·I-6·I-7 4건**으로 증가한다. **경로 B + 판매자 역호출에 대한 product.md 신규 결정 레코드가 필요**하다. 사용자 확인·PRD 반영 요망.

### 항목 2 (긴장 — 정책 확인) — verifiedSum(검색 응답가) vs Spring enrich 표시가 괴리

BudgetSummary `verifiedSum`은 §4.6 검색 응답 가격 기준 결정론 합산(결정 14-A 원칙 유지)인데, 경로 B에서 실제 표시가는 Spring이 목록 GET 시점에 다시 채운다(§4.3). 검색~표시 사이 가격 변경 시 SSE `budget`과 우측 패널 표시가가 순간 괴리할 수 있다. **예산 표시 UX 정책**은 🔴 기획·Spring 협의 필요. (기존 OPEN-11 연장 — 질의 시점 검색이라 괴리 창은 크게 축소됨.)

### 항목 3 (개정 — 결정 20 확대) — 판매자 agent 상세 수정 draft 흐름 MVP 편입 + 데이터 소스 I-6 전환

결정 20(binding)은 판매자 MVP를 "매출/판매 통계 Q&A만"으로 한정하고, 데이터 소스 기본안을 **주문 미러 확장(`sellerId`·금액)** 으로 두었다(product.md line 134·695). 본 개정은 (1) **상세 수정 draft 흐름을 MVP로 확대**하고, (2) 데이터 소스를 **주문 미러 확장에서 I-6 집계 콜백으로 전환**한다(주문 미러 자체가 폐기됨 — 항목 5). **결정 20 개정 레코드가 필요**하다. C-7은 이로써 해소되나 I-6 계약 세부(C-13)는 협의 잔존.

### 항목 4 (개정 — 결정 9/9-A/9-B) — 상품 컬럼 사본·이벤트 동기화 폐기, AI 생성물 pull 배치로 대체 [v0.5.1 정정]

결정 9/9-A/9-B(binding)는 "필터 컬럼 최소 미러 + 이벤트 기반 준실시간 동기화"를 확정했으나(product.md line 132·310·325·340), 2026-07-15 사용자 최종 확정으로 **상품 원본 컬럼의 AI측 사본과 이벤트(웹훅) 동기화를 폐기**한다. **AI 생성물(extras·search_doc·임베딩)은 존속** — 결정 3의 Layer 2/3·결정 6(임베딩 모델)은 **유효**하며(2026-07-20 모델을 **Google `gemini-embedding-001`**(dim 1536, MRL) 로 개정 — 셀프호스트 torch 폐기), 갱신만 **pull 배치(bulk export, §4.8)** 로 바뀐다. 질의 시점 후보 흐름(AI 벡터 검색 ↔ Spring 검색 결합)은 OPEN(§4.8 말미) — 확정 시 결정 레코드에 포함. **결정 9/9-A/9-B 개정(사본·이벤트 폐기 + pull 배치) 신규 결정 레코드가 필요**하다. SPEC-CATALOG-DATA-001의 enrichment→임베딩 단계는 §4.8 배치와 통합 재범위.

### 항목 5 (개정 — 결정 14-F/16 구현 방식) — 주문 이벤트 미러 → 질의 시점 구매 이력 조회

결정 14-F(dedup)·결정 16(프로필 구매 소스)은 "주문 이벤트 → AI 경량 미러" 구현을 전제했으나, 주문 알림·미러를 폐기하고 **추천 직전/sleep-time의 질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 대체한다(2026-07-15 확정). **동작 요구(exact 제외·소모품 억제·되돌리기 칩·구매 신호 델타)는 불변** — 데이터 획득 방식 개정 레코드 필요. SPEC-PROFILE-001 구매 소스 문구 개정(§7.2).

### 항목 6 (정정 — 병행 PRD) — events scope

병행 PRD 초안(docs/PRD.md v1.1.0)은 이벤트 채널 전부를 고도화로 옮겼으나, 확정안은 **`/events/session-end`와 `/events/session-claim`을 MVP에 유지**한다(주문 알림은 미채택으로 정리됨). PRD의 events-scope와 일정표(7/15 행의 "하이브리드 통합" 표현 포함)를 본 문서 v0.5.0 기준으로 정정해야 한다.

### 항목 7 (개정 — 결정 8) — 게스트 장바구니 담기 허용 [v0.6.0]

결정 8(binding)은 "장바구니 담기·구매는 회원 전용"으로 확정했으나, BE 팀 I-2 문서(02 D30, 2026-07-10 개정)가 **게스트(guestId) 담기 성공**을 확정했고 2026-07-15 사용자가 이를 채택했다(§4.1). **개정 범위**: (1) 장바구니 담기는 게스트 허용(로그인 유도는 결제 시점 FE 몫), (2) 구매는 계속 회원 전용, (3) 검색/추천 무제한·개인화 미적용·AI 서버 무상태 원칙은 불변. 구 AI-side 게스트 차단(`GUEST_NOT_ALLOWED`)은 폐기. **결정 8 개정 레코드가 필요**하다. 아울러 결정 7의 구현 세부(인증)는 "사용자 JWT 포워딩"이 아닌 **I-2 서비스 토큰 + 본문 신원(AI-검증 JWT `sub` 유래)** 으로 확정됨 — 결정 7 자체(경로: AI→Spring API 위임)는 불변이므로 별도 개정 불요, C-3 세부로 처리.

### 항목 8 (개정 — 결정 20) — 판매자 모델 전면 확대 + 쓰기 모델 전환 [v0.9.0]

BE internal API 배치(11종 PDF, 2026-07-16) 반영으로 결정 20(판매자 MVP = 통계 Q&A + draft)이 크게 확대된다. **개정 범위**: (1) 판매자 그래프가 **서브에이전트 다수**(sales_anomaly·conversion·behavior·churn·abuse·general·recommend·chart·`product_agent`)로 구성되고 조회/집계 7종 + 상품 CRUD 4종을 소비. (2) **쓰기 모델 전환** — 구 "AI는 제안만, 반영은 FE S-3 PATCH(AI 표면 밖)"에서 **"AI가 Spring internal API로 직접 쓰기(등록/수정/삭제) + 파괴적 작업은 HITL interrupt/resume 승인"**. "대화 발화 ≠ 동의" 원칙은 HITL로 유지. (3) `brandId`=JWT 클레임(항목 없던 신규). (4) 전역 I-8(account-events)은 admin 소유 협의. **결정 20 개정 + 신규 결정 레코드(판매자 쓰기·HITL) 필요**. 판매자 SPEC 신규 작성 필요.

### 항목 9 (개정 — 결정 16) — 마이페이지 프로필 "GET only" → 항목 단위 사용자 제어 편입 [v0.22.0]

결정 16(binding)은 마이페이지 프로필을 **조회 전용**으로 한정한다. 본 개정은 §3.8(그래프 조회)과 §3.9(수정·삭제·복구·초기화·개인화 중지 5종)를 제안하므로 그 한정을 넘는다. **개정 범위**: (1) 프로필 표면에 **쓰기 동작이 존재**하게 되고, **[개정 v0.26.0] 그래프 표면은 조회·변경 모두 Spring 로그인 세션에서 신원을 도출한다** — v0.22.0은 "조회는 토큰 `sub`, 변경은 Spring 세션"으로 갈랐으나 마이페이지에서 스트림 티켓을 발급받을 수 없어 그 분리가 성립하지 않았다(C-20, §3.8). (2) **사용자 개시 물리 삭제**(전체 초기화)가 도입되어 "폐기 대신 supersede" 원칙의 적용 범위가 기계 경로로 좁혀진다(§7.2). (3) **개인화 중지(opt-out)** 라는 사용자 제어가 신설되어, 개인화 강도가 전역 설정만이 아니라 회원별 상태로도 결정된다. (4) confidence를 3버킷으로 **노출**한다(수치는 계속 미노출 — §7.2).

근거는 결정 4-A 보강 6이 마이페이지를 **투명성 표면**으로 규정한다는 점이다 — "AI가 나를 이렇게 이해했다"를 보여주면서 틀린 것을 고칠 수 없게 두는 것은 그 취지와 어긋난다. 다만 **binding 결정을 문서 하나로 뒤집지 않으며**, 결정 16 개정 레코드(또는 신규 결정 레코드)가 필요하다. 계약은 그 전까지 🔴 **제안(초안)** 이다. 관련 미합의: C-20~C-28.

> 위 9항목 외 인증(결정 19)·식별자(결정 19)는 결정과 정합하며 별도 사용자 확인 불필요. ~~장바구니 JWT 포워딩(결정 7·19) 정합~~은 v0.6.0에서 I-2 서비스 토큰 방식으로 대체되었다(항목 7 말미).

---

*문서 끝.*
