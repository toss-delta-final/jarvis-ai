---
id: SPEC-CATALOG-DATA-001
version: 0.1.2
status: draft
created: 2026-07-14
updated: 2026-07-14
author: navis
priority: high
issue_number: 0
---

> ⚠️ **동기화 사본(mirror)** — 정본은 기획 저장소 `.moai/specs/SPEC-CATALOG-DATA-001/spec.md`.
> 외부 **계약**(SSE 이벤트명·엔드포인트·필드·오류 코드)의 상위 소스는 **api-spec v0.7.0**
> ([docs/api-spec.md](../api-spec.md)) 다 — 본 SPEC과 어긋나면 **api-spec을 따른다**.
> 후속 동기화 개정 목록은 api-spec §7. 동기화: **2026-07-16 (SPEC v0.1.2)**.
> ⚠️ 본 SPEC은 **이벤트 기반 동기화 시절** 작성 — 확정안은 **pull 배치**(api-spec §4.8, I-8)로 재범위됨(api-spec §8 항목 4). enrichment→임베딩 단계만 유효, 동기화 방식은 api-spec 우선.

# SPEC-CATALOG-DATA-001 — 카탈로그 시드 데이터셋 구축 (Catalog Seed Dataset Construction)

> 본 SPEC은 결정 15(product.md:579-602, 확정 2026-07-07)가 범위만 고정하고 상세를 위임한 **카탈로그 시드 데이터셋 구축**을 EARS 요구사항 수준으로 확정한다 — 기존 11번가 크롤 데이터(~10k)를 입력으로 받아 검증·카테고리 매핑 → Layer 1 적재 → enrichment(Layer 2) → 리뷰 요약·search_doc·임베딩(Layer 3) → pgvector 인덱스 적재 → 데모 질의 커버리지 역검증 → frozen 스냅샷 버전 태깅까지 수행하여 "데모 사용 가능한 인덱스"를 완성한다.
> 입력: interview.md(사용자 확정 결정 2라운드, 구속), research.md(Phase 0.5 심층 리서치 — 스키마 계약·파이프라인 계약·의존 SPEC 계약·리스크), plan.md(승인된 계획 — 5모듈·M0~M4 분할, Decision Point 1 승인).
> **구속 결정(binding, 재논의하지 않음)**: 크롤러 신규 개발은 비범위(사용자 완료), 범위는 pgvector 인덱스 적재 + 커버리지 역검증 + frozen 스냅샷 태깅까지 엔드투엔드, 신규 정의 3종(카테고리 트리 / 속성 사전 + 소모품 boolean 플래그 / 상황 태그 통제 어휘 + 매핑 테이블), 데모 핵심 카테고리 우선 적재, 카탈로그 전용 Postgres 인스턴스(프로필과 별도, 결정 16). 모든 경로·모델·배치·임계·데모 파라미터는 config 주입(하드코딩 금지, 프로젝트 OPEN 관례).
> 본 SPEC은 오프라인 배치 구축의 관찰 가능한 동작(behavior)과 인수 기준을 확정한다. 런타임 추천 서브그래프·카탈로그 이벤트 동기화는 본 SPEC의 범위가 아니다.

## HISTORY

- **v0.1.2 (2026-07-14)** — 감사 반복 2 **PASS**(Overall 0.91, `.moai/reports/plan-audit/SPEC-CATALOG-DATA-001-review-2.md`) 후 비차단 Minor 2건 반영: (D19) REQ-CAT-038 항목 3의 데이터 사본 저장 위치 config 키를 `snapshot_data_path`로 명명, (D20) plan.md 모듈 B 미러의 REQ-CAT-014 문언을 spec.md 확정 4케이스 목록(유럽여행·Case 1·2·3)으로 정렬.
- **v0.1.1 (2026-07-14)** — 감사 반복 1(iteration 1) 결함 반영. 근거: `.moai/reports/plan-audit/SPEC-CATALOG-DATA-001-review-1.md`(FAIL, 0.75, Major 2 / Minor 16). **Major 해소**: (D1) frozen 스냅샷 아티팩트 계약을 REQ-CAT-038로 신설(불변 스냅샷 레지스트리 행 + manifest 파일 + 데이터 사본, 저장 위치·vN 채번·소비자 해석 규칙 명시)하고, SPEC-EVAL-001 REQ-EVAL-043("하니스 shall 저장·버전 태깅")과의 저장·태깅 소유권 이중 배정을 §9 OPEN-C6·§10 협의 항목으로 등록하며 경계 규범(본 SPEC이 스냅샷을 생산·버전 태깅, EVAL-001 하니스는 vN을 참조·기록)과 후속 "SPEC-EVAL-001 REQ-EVAL-043 경계 조정 제안"을 명시(SPEC-EVAL-001은 본 개정에서 편집하지 않음). (D2) 모듈 C에 배치 실패·재시도·재개 REQ-CAT-030을 신설(config 주입 max retries·checkpoint 재개·`product_id`+`content_hash` 멱등)하고 AC-CAT-16의 "정확히 1회"를 "상품·content_hash당 성공·과금 enrichment 호출 최대 1회(실패 후 재시도는 중복 아님)"로 정합화, AC-CAT-17 신설. **Minor 해소**: D3(`enrichment_model` config 기본값 Haiku 4.5) · D4(acceptance 서문 자기모순 수정 + `coverage_top_k` config 기본 30) · D5(중복 시 적재 규칙 = `product_id` upsert last-wins) · D6(AC-CAT-03 판매자 전제 제거) · D7(categories 테이블 적재 REQ-CAT-037 신설) · D8(REQ-CAT-011/014/015 주어 "The SPEC"→"The 파이프라인") · D9(REQ-CAT-017/029 Optional→State-Driven) · D10(데모 시나리오 확정 목록 고정) · D11(매핑 검수 상태 = 승인 아티팩트 규범) · D12(search_doc 세그먼트↔컬럼 대조 명시) · D13(embedding_dimension 변경 시 마이그레이션 필요·기본 1024 DDL 일치) · D14(REQ-CAT-034 "1:1" 완화 + 컬럼↔필드 대조표) · D15(최소 속성 집합 정의) · D16("안정 확보" = candidate ≥ coverage_top_k 정의). D17(frontmatter labels/created)은 프로젝트 관례 기록 전용으로 수정 불요. D18(plan.md·SPEC-EVAL-001 "합성 리뷰" 드리프트)은 plan.md 내부 문언 정리 + SPEC-EVAL-001 참조는 편집 불가한 교차 SPEC 후속으로 OPEN-C4에 등록. REQ 34→37건(A7/B7/C10/D8/E5), AC 16→17건.
- **v0.1.0 (2026-07-14)** — 최초 작성. 결정 15 위임 범위 + interview.md 확정 결정(크롤러 제외·엔드투엔드 적재·신규 정의 3종·데모 우선·실 리뷰 긴장·규모 ~10k) + research.md 심층 리서치를 5모듈 EARS 명세로 구체화. **Decision Point 1(2026-07-14) 확정 반영**: (DP-1 실 리뷰 정책) 실 리뷰는 search_doc 리뷰 요약 생성의 **enrichment 입력으로만** 사용하고(요약 프롬프트에 개인정보 제외 가드 필수), 원문은 인덱스에 저장·노출하지 않는다. 결정 15의 "상품당 3~10개 합성 리뷰" 생성은 **고도화로 유예(MVP 비구현)** — 실 리뷰가 이미 존재한다는 전제 변경에 따른 결정 15 리뷰 조항의 문서화된 재해석이며, 리뷰 분석 그래프(결정 10-A)·상품 질문 흐름(결정 17) 데모가 저장 리뷰를 요구할 때에만 필요해지고 그 시점에도 재임베딩은 불필요하다(search_doc 요약이 이미 실 리뷰 기반). 이에 따라 plan.md 초안의 REQ-CAT-026(합성 리뷰 생성 요구)이 실 리뷰 요약 요구로 대체되고 합성 리뷰 생성은 EX-CAT-2로 강등되었다. "product.md 결정 15-A 개정 제안"을 후속/sync 항목으로 등록(§9 OPEN-C4). (DP-2 규모 갭) 현보유 ~10k로 진행하고 추가 수집은 M4 커버리지 역검증(10~15 질의 × top-K=30 안정 확보) 실패 시에만 트리거되는 open question으로 기록. (DP-3 임베딩 모델) arctic-embed-l-v2.0-ko vs KURE-v1 최종 택1은 구현 중 스모크 평가로 결정, 모델 식별자는 config 주입. 5모듈 구조(A 입력 계약·검증 / B 신규 정의 3종 / C enrichment·임베딩 / D 적재·인덱스·스냅샷 / E 품질 게이트·역검증)·M0~M4 마일스톤·frontmatter 8필드는 승인 방향으로 확정. 상세 계획·태스크 분해·리스크·전체 참조는 plan.md·research.md 참조.

---

## 1. 개요 & 범위 (Overview & Scope)

### 1.1 목적

기존 11번가 크롤 데이터(~10k)를 입력으로 하여 **데모 사용 가능한 pgvector 검색 인덱스**를 오프라인 배치로 완성하고, SPEC-EVAL-001 REQ-EVAL-005(카탈로그 미완료 동안 골든셋 착수 금지, blocking)의 차단을 해제한다. 목표 상태 = "데모 사용 가능한 인덱스 완성"(interview.md R1-Q2). MVP 7/16 데모의 추천 서브그래프(SPEC-RECOMMEND-001) 동작 전제이기도 하다.

### 1.2 In Scope (본 SPEC이 확정하는 것)

크롤 데이터 입력 → 검증·카테고리 매핑 → Layer 1 적재 → enrichment(Layer 2 속성·태그) → 리뷰 요약·search_doc·임베딩(Layer 3) → pgvector 인덱스 적재 → 데모 질의 커버리지 역검증 → frozen 스냅샷 버전 태깅까지의 엔드투엔드 오프라인 파이프라인의 동작 계약과 인수 기준.

```
[크롤 JSON/JSONL ~10k]  (경로 = config 주입)
   │  A 입력 계약·검증 (필드 null/타입/범위, 중복·가격 이상치, 미매핑 처리 — 재정제 아닌 검증)
   ▼
[카테고리 매핑]  (11번가 원본 → 자체 20~30 트리)
   ▼
[Layer 1 적재]  (products 정형 컬럼 — 크롤 필드 → FilterSet 계약 매핑, 신규 스키마 없음)
   ▼
[Enrichment / Layer 2]  (Haiku 배치, 상품당 1회 — attributes 정규화 + situation_tags 통제 어휘 + extras)
   ▼
[리뷰 요약 + search_doc + 임베딩 / Layer 3]  (실 리뷰 요약[PII 가드] → search_doc → 1024d 임베딩 CPU)
   ▼
[pgvector 인덱스 적재]  (카탈로그 전용 Postgres, 결정 16 — 데모 핵심 카테고리 우선, HNSW/GIN/B-tree)
   ▼
[데모 질의 커버리지 역검증]  (10~15 질의 × top-K=30 안정 확보 확인)
   ▼
[frozen 스냅샷 버전 태깅]  (catalog_snapshot_vN — SPEC-EVAL-001 REQ-EVAL-005 차단 해제)
```

**적재 우선순위**(interview.md R1-Q4, 결정 15 가드 승격): 1차 = 여행용품·욕실용품·음향기기·디지털 액세서리·뷰티·패션잡화(유럽여행/Case 1/2/3 데모 커버) → 2차 = 나머지 전 카테고리. 전체 규모 일괄 구축 자체는 유지.

### 1.3 마일스톤 (승인된 M0 → M4)

- **M0 — 신규 정의 3종 확정**(카테고리 트리·속성 사전·상황 태그 어휘·매핑 테이블). 크롤 데이터 실제 카테고리 분포 분석이 선행 입력.
- **M1 — Layer 1 검증·매핑·적재**. 의존성 없음(즉시 착수 가능).
- **M2 — Enrichment(Layer 2)**. M0 확정 후.
- **M3 — 리뷰 요약 + search_doc + 임베딩(Layer 3)**. DP-1 확정(본 SPEC) + M2 후.
- **M4 — 커버리지 역검증 + 스냅샷 태깅**. M3 완료 후.

> 외부 맥락: MVP 목표일 2026-07-16(product.md), 오늘 2026-07-14. 데모 핵심 카테고리 우선 적재(M3)로 크롤/검수 지연 시에도 데모 시나리오가 막히지 않도록 하며, 이는 참고 맥락일 뿐 본 SPEC에 기간 추정을 넣지 않는다.

---

## 2. 환경 & 컨텍스트 (Environment & Context)

### 2.1 입력 계약 (Input Contract)

| 항목 | 값 | 근거 |
|------|-----|------|
| 원천 | 11번가 크롤(사용자 완료) | interview.md R1-Q1 |
| 규모 | 약 10,000개, 전 카테고리 커버(하한 근처) | interview.md R2-Q2 |
| 형식·위치 | 로컬 JSON/JSONL, 저장소 밖 — 경로는 config 주입(`crawl_data_path`) | interview.md R2-Q1 |
| 포함 필드 | 상품명·가격·카테고리·브랜드·평점·리뷰 수·상세 설명(장문) + **실제 리뷰 텍스트** | interview.md R2-Q3 |
| 정제 상태 | 정제 완료 — 파이프라인은 재정제가 아닌 **검증** 위주 | interview.md R2-Q4 |

### 2.2 목표 인덱스 스키마 (Target Schema — research.md §2)

**products 테이블 (AI 서버 소유 검색 인덱스, 결정 9-B)**

| 컬럼 | 타입 | 출처/소유 | 용도 | SPEC 책임 |
|------|------|-----------|------|----------|
| product_id | text (PK) | 미러(Spring) | 식별자·조인·오버레이 키 | 검증·upsert 키 |
| name | text | 미러 | 재랭킹 근거 생성 입력 | 검증 |
| category_no | int (FK) | 미러 | 카테고리 필터 | 매핑 테이블 + 검증 |
| brand | text | 미러 | 필터·재랭킹 | 검증 |
| price | int | 미러 | WHERE 필터·예산 계산(표시값 아님, 결정 9-A) | 경량 동기화 계약 |
| stock | int | 미러 | 재고 필터 | 경량 동기화 계약 |
| rating_avg | numeric(2,1) | 미러 | 정렬·인기 | 검증 |
| review_count | int | 미러 | 정렬·인기 | 검증 |
| attributes | JSONB | 미러 [데모는 AI 생성] | Layer 2 카테고리별 유연 속성·필터 | 속성 사전 정규화 |
| situation_tags | text[] 또는 JSONB | AI 생성 | Layer 2 상황 태그·필터 | 통제 어휘 15~25개 |
| extras | JSONB | AI 생성 | 자유 특징 | enrichment 파이프라인 |
| search_doc | text | AI 생성 | Layer 3 임베딩 원본(name+category+tag+상황+리뷰 요약) | enrichment 파이프라인 |
| embedding | vector(1024) | AI 생성 | Layer 3 문서 임베딩 [HNSW] | embedding 서비스(결정 6) |
| source_updated_at | timestamptz | 동기화 메타 | Spring 원본 변경 시각 | 이벤트 채널(비범위) |
| indexed_at | timestamptz | 동기화 메타 | 인덱스 갱신 시각 | 파이프라인 메타 |
| content_hash | text | 동기화 메타 | 콘텐츠 변경 판별(재임베딩 트리거) | enrichment 파이프라인 |

**categories 테이블**

| 컬럼 | 타입 | 용도 |
|------|------|------|
| category_no | int (PK) | 카테고리 식별자 |
| parent_no | int (FK, nullable) | 상위 카테고리(대분류=NULL) |
| name | text | 카테고리명 |
| level | int | 1=대분류 / 2=중분류 |

### 2.3 소비자 (Consumers)

- **SPEC-RECOMMEND-001 §5.2**: 검색 tool FilterSet(category, price_min/max, price_scope, total_budget, brand, rating_min, in_stock_only, attributes, source) + Candidate(product_id, name, category, brand, rating, index_price, in_stock, similarity, matched_tags, doc_snippet). 본 SPEC이 채우는 컬럼이 이 계약과 1:1 대응(신규 Layer 1 스키마 없음).
- **SPEC-RECOMMEND-001 REQ-REC-103**: 카테고리 억제 판단의 소모품 boolean 플래그를 본 SPEC 속성 사전이 정의·제공.
- **SPEC-EVAL-001 REQ-EVAL-005 / EX-EVAL-2 / REQ-EVAL-043**: 본 SPEC은 frozen 스냅샷(`catalog_snapshot_vN`)의 **생산·저장·버전 태깅 주체**이며(REQ-CAT-035/038), 아티팩트 계약(레지스트리 행 + manifest + 데이터 사본)과 vN 채번·해석 규칙을 정의·제공한다. SPEC-EVAL-001 하니스는 이 스냅샷 vN을 **참조·기록**하여 골든셋 라벨을 카탈로그 상태에 결속한다(소비·참조 주체). **경계 주의(미해소 협의 항목)**: SPEC-EVAL-001 REQ-EVAL-043은 "하니스 shall … 저장·버전 태깅"으로 저장·태깅 행위를 자기 소유로 규정하여 본 SPEC과 동일 행위가 이중 배정된 상태다 — §9 OPEN-C6 / §10 협의 항목에 등록하고 경계 조정 제안을 명시한다(본 SPEC은 SPEC-EVAL-001을 편집하지 않음).
- **SPEC-PROFILE-001**: 카테고리 억제 근거(소모품 플래그)를 공유 소비.

---

## 3. 관련 결정 참조 (Related Decisions)

| 결정/요구 | 요약 | 반영 위치 |
|---|---|---|
| 결정 15 (product.md:579) | 카탈로그 시드 범위(부모) — 본 SPEC이 상세 확정 | 전체, §9 OPEN-C4(리뷰 조항 재해석) |
| 결정 3 (product.md:158) | 3계층 메타·4단계 파이프라인, search_doc = name+category+tag+상황+리뷰 요약, 문서 단위 단일 벡터 | 모듈 C REQ-CAT-027 |
| 결정 4 가이드라인 1 (product.md:205) | enrichment 가드 4종(self-refinement 금지·MVP-RAG·label-free·Doc2Query--) | 모듈 C REQ-CAT-022~025 |
| 결정 5 (product.md:231) | Haiku 4.5 배치, 상품당 1회, 배치 API 50% | 모듈 C REQ-CAT-021 |
| 결정 6 (product.md:256) | 임베딩 arctic-embed-l-v2.0-ko/KURE-v1, 1024d, CPU 셀프호스트 | 모듈 C REQ-CAT-028, §9 OPEN-C2 |
| 결정 9-A/9-B (product.md:321/336) | 가격·재고 컬럼 유지(필터·예산 전용), 필터 컬럼 최소 미러 | §2.2, 모듈 D |
| 결정 10-A (product.md:378) | 리뷰 분석 3산출 — 그래프 구현은 고도화 | EX-CAT-5, §9 OPEN-C4 |
| 결정 14-F (product.md:537) | 카테고리 재구매 메타 = MVP 소모품 boolean 플래그 | 모듈 B REQ-CAT-013 |
| 결정 16 (product.md:604) | 카탈로그 전용 Postgres 인스턴스(프로필과 별도) | 모듈 D REQ-CAT-031 |
| 결정 17 (product.md, 상품 질문 흐름) | 저장 리뷰 요구 시점 = 합성 리뷰 유예 해제 트리거 | EX-CAT-2 |
| SPEC-RECOMMEND-001 §5.2 / REQ-REC-103 | FilterSet/Candidate 컬럼 계약 · 소모품 플래그 소비 | 모듈 D REQ-CAT-034, 모듈 B REQ-CAT-013 |
| SPEC-EVAL-001 REQ-EVAL-005 / EX-EVAL-2 / REQ-EVAL-043 | 카탈로그 완료 blocking 게이트 · frozen 스냅샷 소비·참조 · 저장·태깅 소유권 경계 | 모듈 D REQ-CAT-035/038, §9 OPEN-C6, §10 |

---

## 4. 기능 요구사항 (Functional Requirements — EARS)

요구 ID 접두사: **REQ-CAT-XXX**. 모듈별 REQ 예약 대역: 모듈 A(001~010) / 모듈 B(011~020) / 모듈 C(021~030) / 모듈 D(031~040) / 모듈 E(041~050). 각 모듈 제목의 괄호는 **실사용 범위(예약 대역)** 형식이며, 대역 내 미사용 번호는 결번이 아니라 주석 사이클 확장 여유의 예비 번호다. 모든 임계·규모·경로·모델 값은 config 주입(하드코딩 금지).

### 4.1 모듈 A — 입력 계약·검증 (실사용 REQ-CAT-001~007, 예약 대역 001~010)

- **REQ-CAT-001** (Ubiquitous): The 파이프라인 **shall** 크롤 입력 경로·형식(JSON/JSONL)을 config 주입 파라미터(`crawl_data_path`)로 정의하고 스키마 검증을 파이프라인 진입점에 둔다(경로·형식 하드코딩 금지).
- **REQ-CAT-002** (Event-Driven): **When** 크롤 레코드를 적재하면, the 파이프라인 **shall** 필드 검증(null / 타입 / 값 범위)을 수행하고 위반 레코드를 검증 리포트에 기록한다.
- **REQ-CAT-003** (Unwanted): The 파이프라인 **shall not** 크롤 필드 값을 재정제·변형하지 않는다 — 정제 완료 데이터(interview.md R2-Q4) 전제이며 검증 위주로 동작한다.
- **REQ-CAT-004** (State-Driven): **While** 동일 `product_id` 중복이 입력에 존재하는 동안, the 파이프라인 **shall** 중복 제거 확인을 수행하고 중복 건을 리포트하며, 적재 결과는 `product_id` PK upsert last-wins(입력 순서상 마지막 유효 레코드가 잔존)로 확정한다 — 병합·다중 적재 없이 레코드당 단일 행을 보장한다(결정 15 품질 게이트, D5).
- **REQ-CAT-005** (Unwanted): The 파이프라인 **shall not** 가격 이상치(config 범위 밖) 레코드를 무검증 통과시키지 않는다 — 이상치 검증 게이트를 적용하고 리포트한다.
- **REQ-CAT-006** (Unwanted): The 카테고리 매핑 **shall not** 미매핑(자체 트리에 대응 없음) 카테고리 레코드를 조용히 폐기(silent drop)하지 않는다 — 기타 분류로 보류하거나 미매핑 리포트로 노출한다.
- **REQ-CAT-007** (Ubiquitous): The 파이프라인 **shall** 검증 리포트(필드 위반·중복·가격 이상치·미매핑 카테고리 집계)를 산출하여 매핑 검수·품질 게이트의 입력으로 제공한다.

### 4.2 모듈 B — 신규 정의 3종 (실사용 REQ-CAT-011~017, 예약 대역 011~020)

- **REQ-CAT-011** (Ubiquitous): The 파이프라인 **shall** 종합몰형 대분류 20~30개·얕은 2단계 계층의 카테고리 트리 제안 확정본을 산출한다(Spring 합의는 협의 항목이며 선행 차단 아님 — §10).
- **REQ-CAT-012** (Ubiquitous): The 속성 사전 **shall** 카테고리별 Layer 2 속성을 통제 값 집합(controlled value set)으로 정의하여 자유 문자 입력을 금지하고 값 정규화(MVP-RAG, arXiv:2509.23874)의 대상 어휘를 제공한다.
- **REQ-CAT-013** (Ubiquitous): The 속성 사전 **shall** 카테고리 재구매 메타로 **소모품 boolean 플래그**를 포함한다(결정 14-F, SPEC-RECOMMEND-001 REQ-REC-103이 소비).
- **REQ-CAT-014** (Ubiquitous): The 파이프라인 **shall** 상황 태그 통제 어휘 15~25개(폐쇄 목록)를 정의하고, **데모 시나리오 확정 목록(유럽여행 · Case 1 · Case 2 · Case 3 — config `demo_queries_path`의 질의 집합 및 SPEC-RECOMMEND-001 데모 케이스로 고정)** 을 모두 커버함을 보장한다 — decompose(SPEC-RECOMMEND-001) 프롬프트 주입 원천. "모두"의 확정 목록은 본 SPEC 외부 열거가 아니라 위 4개 케이스로 고정한다(D10).
- **REQ-CAT-015** (Ubiquitous): The 파이프라인 **shall** 크롤 원본 카테고리 → 자체 트리 매핑 테이블을 정의하며, 중복 원본 경로 병합 규칙과 미매핑 처리 규칙(기타 분류/리포트)을 포함한다.
- **REQ-CAT-016** (Unwanted): The enrichment **shall not** 상황 태그를 통제 어휘 밖으로 자유 생성·확장하지 않는다 — 태그 파편화 방지(결정 15).
- **REQ-CAT-017** (State-Driven): **While** 매핑된 카테고리에 속성 사전 항목이 없는 상태인 동안, the 파이프라인 **shall** 최소 속성 집합(정형 컬럼 `product_id`·`name`·`category_no`·`price`·`brand`만, 카테고리별 유연 속성 없이)으로 폴백하고 해당 카테고리를 사전 보강 대상으로 플래그한다(D9 패턴 정정, D15 최소 속성 집합 정의).

### 4.3 모듈 C — Enrichment·리뷰 요약·임베딩 파이프라인 (실사용 REQ-CAT-021~030, 예약 대역 021~030)

- **REQ-CAT-021** (Event-Driven): **When** 상품을 enrichment 처리하면, the 파이프라인 **shall** config 주입 모델(`enrichment_model`, 기본값 Claude Haiku 4.5)의 배치로 attributes·situation_tags·extras를 **상품·`content_hash`당 성공 기준 1회**(실패 후 멱등 재시도는 REQ-CAT-030에 따라 중복으로 계수하지 않음) 추출한다(배치 API 50% 할인, 결정 5, D3 모델 config 기본값화).
- **REQ-CAT-022** (Unwanted): The enrichment **shall not** LLM 자기 정제(self-refinement) 루프를 사용하지 않는다(결정 4 가이드라인 1, arXiv:2501.01237).
- **REQ-CAT-023** (Ubiquitous): The enrichment **shall** 추출 속성 값을 기존 통제 값 집합(REQ-CAT-012)에 대한 검색으로 정규화한다(MVP-RAG, arXiv:2509.23874).
- **REQ-CAT-024** (State-Driven): **While** 추출 속성을 확정하는 동안, the enrichment **shall** 레이블 없는(label-free) 자동 품질 게이트를 적용한다(arXiv:2510.23941).
- **REQ-CAT-025** (Unwanted): The 파이프라인 **shall not** 환각 태그를 관련성 필터로 제거하지 않은 채 search_doc·색인에 포함하지 않는다(Doc2Query--, arXiv:2301.03266).
- **REQ-CAT-026** (Event-Driven): **When** search_doc 리뷰 요약을 생성하면, the 파이프라인 **shall** 실 리뷰 텍스트를 요약 **입력으로만** 사용하고 요약 프롬프트에 개인정보(PII) 제외 가드를 포함하며, 실 리뷰 원문을 인덱스에 저장하거나 어디에도 노출하지 않는다(DP-1 확정, 2026-07-14).
- **REQ-CAT-027** (Ubiquitous): The 파이프라인 **shall** search_doc를 아래 세그먼트↔컬럼 대조에 따라 결합 생성한다(결정 3, 문서 단위 단일 벡터, 단어 단위 코사인 유사도 명시적 배제, D12 세그먼트↔컬럼 대응 명시):
  | search_doc 세그먼트 | 원천 컬럼/산출 |
  |---|---|
  | name | `products.name` |
  | category | `categories.name`(`category_no` 조인) |
  | tags | `attributes`(REQ-CAT-023 정규화 속성 값을 텍스트화) |
  | situation | `situation_tags`(통제 어휘, REQ-CAT-014) |
  | 리뷰 요약 | 실 리뷰 요약(REQ-CAT-026 산출, 인덱스 미저장) |
- **REQ-CAT-028** (Ubiquitous): The 파이프라인 **shall** search_doc를 config 주입 모델(`embedding_model`, 1순위 arctic-embed-l-v2.0-ko / 대안 KURE-v1)로 임베딩하며, 차원은 config `embedding_dimension`(기본 1024, `vector(1024)` 컬럼과 일치)·디바이스 config `embedding_device`(cpu)로 CPU 셀프호스트한다(결정 6). **`embedding_dimension`은 `vector(1024)` DDL과 결속된 사실상 고정 파라미터이며, 1024 외 값 주입은 products.embedding 컬럼 스키마 마이그레이션을 선행 요구한다**(마이그레이션 없이 1024 외 값 주입 시 스키마 모순으로 적재 실패, D13).
- **REQ-CAT-029** (State-Driven): **While** 상품에 실 리뷰가 없는 상태인 동안, the 파이프라인 **shall** 리뷰 요약 세그먼트 없이 search_doc를 구성한다(graceful degradation — 리뷰 요약은 존재 시에만 결합, D9 패턴 정정).
- **REQ-CAT-030** (Event-Driven): **When** enrichment(REQ-CAT-021) 또는 임베딩(REQ-CAT-028) 배치의 상품 단위 처리가 실패하면, the 파이프라인 **shall** config 주입 최대 재시도(`batch_max_retries`) 한도 내에서 실패 건만 멱등 재시도하고, 완료 체크포인트(`product_id` + `content_hash` 기준 성공 표식)를 기록하여 중단 후 재개 시 이미 성공한 상품을 재처리·재과금하지 않는다 — 성공 표식이 있는 상품은 스킵하고, 실패·미처리 상품만 재개 대상으로 삼는다(D2 실패·재시도·재개 의미론). 상품·`content_hash`당 **성공·과금 enrichment 호출은 최대 1회**이며 실패 후 재시도는 중복 호출로 계수하지 않는다.

### 4.4 모듈 D — 적재·인덱스·스냅샷 버전 (실사용 REQ-CAT-031~038, 예약 대역 031~040)

- **REQ-CAT-031** (Ubiquitous): The 적재 **shall** products 레코드를 `product_id` PK 기준 upsert로 카탈로그 전용 PostgreSQL+pgvector 인스턴스(프로필과 별도, 결정 16)에 적재한다.
- **REQ-CAT-032** (Ubiquitous): The 적재 **shall** HNSW(embedding) · GIN(attributes / situation_tags) · B-tree(price / category_no / brand) 인덱스를 생성한다.
- **REQ-CAT-033** (State-Driven): **While** 1차 데모 핵심 카테고리(여행용품·욕실용품·음향기기·디지털 액세서리·뷰티·패션잡화)가 미적재인 동안, the 적재 **shall** 이를 2차(나머지)보다 우선 적재한다(interview.md R1-Q4, config `demo_priority_categories`).
- **REQ-CAT-034** (Ubiquitous): The 적재 **shall** 채워지는 인덱스-소유 컬럼이 SPEC-RECOMMEND-001 §5.2 FilterSet/Candidate 계약의 **인덱스-소유 필드에 대응**함을 보장한다(신규 Layer 1 스키마를 설계하지 않는다, 결정 15). 아래 대조표에 따라 **질의 시 파생/질의 파라미터 필드는 인덱스 컬럼 대응 대상에서 제외**한다(D14 "1:1" 문언 완화):
  | 계약 필드 부류 | 예 | 인덱스 컬럼 대응 |
  |---|---|---|
  | FilterSet 인덱스 필터 | category, price_min/max, brand, rating_min, in_stock_only, attributes | products 컬럼에 대응(직접 적재) |
  | FilterSet 질의 파라미터 | price_scope, total_budget, source | 질의 시 산정 — 컬럼 대응 없음 |
  | Candidate 정형 값 | product_id, name, category, brand, rating, index_price, in_stock | products 컬럼에 대응(직접 적재) |
  | Candidate 질의 파생 값 | similarity, matched_tags, doc_snippet | 질의(벡터 검색) 시 파생 — 컬럼 대응 없음 |
- **REQ-CAT-035** (Event-Driven): **When** 적재가 완료되고 커버리지 역검증이 PASS이면, the 파이프라인 **shall** frozen 스냅샷을 REQ-CAT-038의 아티팩트 계약에 따라 저장하고 `catalog_snapshot_vN`으로 버전 태깅한다(SPEC-EVAL-001 EX-EVAL-2 소비 계약, REQ-EVAL-005 차단 해제).
- **REQ-CAT-036** (Ubiquitous): The 파이프라인 **shall** `content_hash`·`indexed_at` 동기화 메타를 기록한다(콘텐츠 변경 판별·재임베딩 트리거 판별 기반).
- **REQ-CAT-037** (Ubiquitous): The 적재 **shall** categories 테이블(`category_no` PK / `parent_no` FK nullable / `name` / `level`, §2.2)을 생성·적재하여 products.`category_no` FK의 참조 대상을 제공한다 — 자체 트리(REQ-CAT-011)와 매핑 테이블(REQ-CAT-015)의 확정 카테고리를 categories 행으로 적재한다(D7 categories 적재 REQ 신설).
- **REQ-CAT-038** (Ubiquitous): The 파이프라인 **shall** frozen 스냅샷 아티팩트 계약을 다음으로 정의·구현한다(D1 아티팩트 계약 신설) — 소비자(SPEC-EVAL-001 하니스)는 vN 문자열만으로 스냅샷을 결정론적으로 해석·재현할 수 있어야 한다:
  1. **불변 표식**: DB 레벨 불변 스냅샷 표식(스냅샷 레지스트리 테이블 `catalog_snapshots(snapshot_version PK, created_at, row_count, content_digest, config_digest, status)`에 태깅 시점 확정 행을 append; 태깅 후 대상 행 수정 금지).
  2. **manifest 파일**: config 주입 위치(`snapshot_registry_path`)에 `catalog_snapshot_vN.manifest.json`(스냅샷 버전, 포함 `product_id` 집합 해시, categories/products 행 수, embedding 모델·차원, config 스냅샷 digest, 데이터 사본 위치·체크섬)을 산출한다.
  3. **데이터 사본**: 태깅 시점 products/categories 상태의 불변 사본(config 주입 위치 `snapshot_data_path`의 DB 덤프 또는 읽기 전용 테이블 사본)을 보존한다.
  4. **vN 채번**: `catalog_snapshot_v{N}`의 N은 레지스트리 내 기존 최대치 +1(1부터 단조 증가, 재사용·건너뜀 없음)로 결정론적으로 채번한다.
  5. **소비자 해석**: vN 참조 = 레지스트리 행 + manifest + 데이터 사본의 3자 정합(content_digest 일치)으로 해석하며, 불일치 시 스냅샷을 무효로 판정한다.

### 4.5 모듈 E — 품질 게이트·커버리지 역검증 (실사용 REQ-CAT-041~045, 예약 대역 041~050)

- **REQ-CAT-041** (Event-Driven): **When** 데모 질의 스크립트(config `demo_queries_path`, 10~15개)를 실행하면, the 파이프라인 **shall** 질의당 config `coverage_top_k`(기본 30, 결정 14 구속값) 후보가 **확보(질의별 후보 수 ≥ `coverage_top_k`)** 되는지 역검증한다 — "안정 확보"는 후보 수 충족(candidate count ≥ `coverage_top_k`)을 뜻하며 반복 실행 안정성 등 추가 의미를 갖지 않는다(D4 config 화, D16 정의 명확화).
- **REQ-CAT-042** (State-Driven): **While** 커버리지 역검증이 부족(질의 후보 미달)으로 판정되는 동안, the 파이프라인 **shall** 해당 카테고리의 추가 수집/보강 필요를 open question으로 기록한다(규모 갭 DP-2 — 추가 수집은 역검증 실패 시에만 트리거).
- **REQ-CAT-043** (Ubiquitous): The 파이프라인 **shall** 모든 파라미터(경로·모델·배치 크기·임계·데모 우선 카테고리·데모 질의 경로)를 config 주입한다(하드코딩 금지).
- **REQ-CAT-044** (Unwanted): The 파이프라인 **shall not** 카테고리 매핑 미검수 상태로 인덱스를 확정하지 않는다 — 매핑 검수 게이트(결정 15)를 통과해야 확정한다. 매핑 검수 완료 상태는 config 주입 승인 아티팩트(`mapping_review_approved_path`가 가리키는 검수 승인 레코드, 또는 config `mapping_review_approved` 플래그)로 표현하며, 게이트는 이 아티팩트의 존재·유효를 입력으로 판정한다(D11 검수 상태 표현·주체 규범화).
- **REQ-CAT-045** (State-Driven): **While** 커버리지 역검증(REQ-CAT-041)이 PASS에 도달하지 못한 동안, the 파이프라인 **shall** frozen 스냅샷 버전 태깅(REQ-CAT-035)을 차단한다.

---

## 5. 추적성 맵 (Traceability)

| REQ-CAT | 커버 대상(부모 항목) | 출처 | 인수 기준 |
|----------|-------------------|------|-----------|
| REQ-CAT-001/003/007 | 입력 계약·config 주입 진입점·재정제 금지·검증 리포트 | interview.md R2-Q1/Q4, research.md §3-1 | AC-CAT-01/14/15 |
| REQ-CAT-002 | 필드 검증(null/타입/범위) | research.md §3-1 | AC-CAT-01/15 |
| REQ-CAT-004/005 | 중복 제거(upsert last-wins)·가격 이상치 게이트 | 결정 15 품질 게이트, D5 | AC-CAT-03 |
| REQ-CAT-006 | 미매핑 카테고리 silent-drop 금지 | research.md §6.3 | AC-CAT-02 |
| REQ-CAT-011~015 | 신규 정의 3종(트리·속성 사전+소모품·상황 어휘·매핑) | 결정 15, 14-F, research.md §6 | AC-CAT-04 |
| REQ-CAT-016 | 상황 태그 자유 확장 금지 | 결정 15 | AC-CAT-05 |
| REQ-CAT-017 | 속성 사전 부재 폴백(State-Driven, 최소 속성 집합 D15) | research.md §6.1 | AC-CAT-05 |
| REQ-CAT-021 | enrichment 배치(config `enrichment_model` 기본 Haiku, D3) 상품·content_hash당 성공 1회 | 결정 5, research.md §3-2 | AC-CAT-06/16 |
| REQ-CAT-022~025 | enrichment 가드 4종 | 결정 4 가이드라인 1 (arXiv:2501.01237/2509.23874/2510.23941/2301.03266) | AC-CAT-06 |
| REQ-CAT-026/027 | 실 리뷰 요약 PII 가드·search_doc 조립 | DP-1(2026-07-14), 결정 3 | AC-CAT-07 |
| REQ-CAT-028 | 임베딩 1024d config·CPU·차원 변경 마이그레이션(D13) | 결정 6 | AC-CAT-08/16 |
| REQ-CAT-029 | 리뷰 부재 graceful degradation(State-Driven) | DP-1 파생 | AC-CAT-08 |
| REQ-CAT-030 | 배치 실패·재시도·재개 멱등(D2) | 결정 5 비용 의도, research.md §3-2 | AC-CAT-16/17 |
| REQ-CAT-031/032/033 | upsert·인덱스·데모 우선 적재 | 결정 16, research.md §3-4, interview.md R1-Q4 | AC-CAT-09 |
| REQ-CAT-034 | FilterSet/Candidate 인덱스-소유 컬럼 대응(질의 파생/파라미터 제외, D14) | SPEC-RECOMMEND-001 §5.2, 결정 15 | AC-CAT-01 |
| REQ-CAT-035/036 | frozen 스냅샷 태깅·동기화 메타 | SPEC-EVAL-001 EX-EVAL-2, research.md §2 | AC-CAT-10 |
| REQ-CAT-037 | categories 테이블 생성·적재(FK 참조 대상, D7) | §2.2, AC-CAT-01 category_no FK | AC-CAT-01 |
| REQ-CAT-038 | frozen 스냅샷 아티팩트 계약(레지스트리+manifest+사본·vN 채번·해석, D1) | SPEC-EVAL-001 REQ-EVAL-043 경계, research.md §2 | AC-CAT-10 |
| REQ-CAT-041 | 데모 질의 coverage_top_k(기본 30) 커버리지 역검증(D4/D16) | 결정 15 품질 게이트, 결정 14 | AC-CAT-11 |
| REQ-CAT-042/045 | 역검증 실패 시 open question·스냅샷 차단 | DP-2 | AC-CAT-12 |
| REQ-CAT-043 | 전 파라미터 config 주입 | 프로젝트 OPEN 관례 | AC-CAT-14 |
| REQ-CAT-044 | 매핑 검수 게이트 | 결정 15 | AC-CAT-13 |

---

## 6. 인수 기준 요약 (Acceptance Criteria — 상세는 acceptance.md)

인수 기준은 `AC-CAT-XX` ID로 acceptance.md에 Given/When/Then 형식으로 확정한다. 핵심 게이트:

- 유효 크롤 파일 → Layer 1 적재 + categories 테이블 적재·category_no FK 매핑 + 검증 리포트(재정제 없음)(AC-CAT-01).
- 미매핑 카테고리 → silent-drop 없이 리포트/보류(AC-CAT-02).
- 중복 `product_id`(upsert last-wins 단일 행)·가격 이상치 → 리포트 플래그(AC-CAT-03).
- 신규 정의 3종 산출(트리 20~30·속성 사전+소모품 플래그·상황 어휘 15~25[유럽여행 포함]·매핑 테이블)(AC-CAT-04).
- 속성 사전 부재 카테고리 → 폴백·플래그 + 통제 어휘 밖 태그 거부(AC-CAT-05).
- enrichment 속성이 통제 값 밖 → MVP-RAG 정규화 또는 label-free 게이트 거부, 환각 태그 필터(AC-CAT-06).
- 실 리뷰 요약 PII 가드 동작·원문 미저장/미노출(AC-CAT-07).
- 리뷰 부재 상품 search_doc 구성 + 임베딩 차원 1024(AC-CAT-08).
- upsert·인덱스(HNSW/GIN/B-tree)·데모 핵심 우선 적재(AC-CAT-09).
- 스냅샷 아티팩트 계약(레지스트리 행 + `catalog_snapshot_vN.manifest.json` + 데이터 사본, vN 채번·해석 검증) + content_hash/indexed_at 메타(AC-CAT-10).
- 데모 질의 `coverage_top_k`(기본 30) 커버리지 PASS → 스냅샷 태깅 가능(AC-CAT-11).
- 커버리지 FAIL(후보 < `coverage_top_k`) → 스냅샷 태깅 차단 + DP-2 open question 기록(AC-CAT-12).
- 매핑 미검수(승인 아티팩트 부재) 시 인덱스 확정 차단(AC-CAT-13).
- 전 파라미터 config 주입·하드코딩 부재(AC-CAT-14).
- 엣지: 손상/빈 JSONL 라인 처리(중단·조용한 폐기 없음)(AC-CAT-15), 배치 완결성(성공·과금 상품당 최대 1회)·임베딩 차원 검사(AC-CAT-16), 배치 실패 후 멱등 재시도·재개 무재과금(AC-CAT-17).

---

## 7. Exclusions (What NOT to Build)

- **EX-CAT-1 크롤러 신규 개발**: 원천 수집은 사용자가 11번가로 이미 완료(interview.md R1-Q1). 크롤러 개발·robots.txt/politeness는 본 SPEC 비범위.
- **EX-CAT-2 합성 리뷰 생성(고도화 유예)**: 결정 15의 "상품당 3~10개 합성 리뷰" 생성은 **고도화 범위(MVP 비구현)**로 유예한다(DP-1 확정, 2026-07-14). 실 리뷰가 이미 존재하여 search_doc 리뷰 요약은 실 리뷰 입력으로 충족되므로 합성 리뷰는 MVP에 불필요하다. 리뷰 분석 그래프(결정 10-A)·상품 질문 흐름(결정 17) 데모가 **저장 리뷰**를 요구하는 시점에만 필요해지며, 그때에도 search_doc 요약이 이미 실 리뷰 기반이므로 **재임베딩은 불필요**하다. (결정 15 리뷰 조항의 문서화된 재해석 — §9 OPEN-C4 참조.)
- **EX-CAT-3 카탈로그 이벤트 동기화/웹훅**: 상품 변경 통지·경량 UPDATE·재임베딩 트리거 등 런타임 동기화는 별도 카탈로그 이벤트 SPEC 소관(결정 9/9-A/9-B). 본 SPEC은 오프라인 배치 구축만.
- **EX-CAT-4 Spring 측 원본 DB 적재**: 공용 시드의 Spring 원본 DB(MySQL) 적재는 Spring 팀 소관. 본 SPEC은 AI 서버 Postgres 인덱스 적재만.
- **EX-CAT-5 리뷰 분석 그래프 구현**: 리뷰 분석 3개 산출(결정 10-A — 악성 판정·판매자 인사이트·프로필 취향 신호) 그래프 파이프라인 구현은 고도화. 본 SPEC은 리뷰를 search_doc 요약 입력으로만 소비하고 분석 그래프는 구현하지 않는다.
- **EX-CAT-6 프로필/평가 하니스 범위**: 프로필 파이프라인(SPEC-PROFILE-001)·평가 하니스(SPEC-EVAL-001)는 본 SPEC 비범위. 본 SPEC은 frozen 스냅샷 태깅 방식 제공까지(SPEC-EVAL-001 EX-EVAL-2와 상보).
- **EX-CAT-7 정교한 재구매/다양성 모델**: 카테고리 억제 판단은 MVP 소모품 boolean 플래그만(SPEC-RECOMMEND-001 EX-9 / REQ-REC-103). 정교한 재구매 주기·variety-seeking 모델은 고도화.

---

## 8. 비기능 요구사항 (Non-Functional Requirements)

- **재현성**: 모든 적재 결과는 `catalog_snapshot_vN` 아티팩트 계약(레지스트리 행 + manifest + 데이터 사본, REQ-CAT-038) + config 스냅샷으로 재현 가능해야 하며, 소비자는 vN 문자열만으로 스냅샷을 결정론적으로 해석할 수 있어야 한다(REQ-CAT-035/036/038).
- **결정론**: 품질 게이트 판정(중복·이상치·매핑 검수·커버리지 PASS/FAIL)은 동일 입력·동일 config에서 결정론적이어야 한다.
- **config 주입**: 경로·모델·배치·임계·데모 파라미터는 모두 config 주입(하드코딩 금지, REQ-CAT-043).
- **개인정보**: 실 리뷰 원문은 인덱스에 저장·노출하지 않으며 요약 생성 프롬프트에 PII 제외 가드를 둔다(REQ-CAT-026).
- **격리**: 카탈로그 저장소는 프로필과 완전 별도 PostgreSQL+pgvector 인스턴스를 소비한다(결정 16).
- **언어·스택**: Python 3.12 이상, uv, Docker/pgvector, pytest — tech.md 정합(plan.md §6). 파이프라인 코드 자체도 pytest 검증 대상.

---

## 9. 미해결 / 후속 항목 (Open Questions & Follow-ups)

- **OPEN-C1 규모 갭 트리거(DP-2)** — 현보유 ~10k로 진행하고, 추가 수집은 **M4 데모 질의 커버리지 역검증(10~15 질의 × top-K=30 안정 확보)이 특정 카테고리에서 실패할 때에만** 발동한다(REQ-CAT-042). 절대 규모가 아닌 커버리지 역검증이 판정 기준.
- **OPEN-C2 임베딩 모델 택1(DP-3)** — arctic-embed-l-v2.0-ko(1순위) vs KURE-v1 최종 택1은 구현 중 자체 상품 데이터 스모크 평가로 결정(결정 6). 모델 식별자는 config 주입이므로 교체 비용은 낮다.
- **OPEN-C3 임베딩 서빙 형태** — FastAPI 프로세스 내 로드 vs 별도 서비스(torch ~2GB 메모리). 시드 구축 자체는 배치 실행이라 서빙 형태와 분리 가능(비차단).
- **OPEN-C4 product.md 결정 15-A 개정 제안 + SPEC-EVAL-001 "합성 리뷰" 참조 정리(DP-1 후속 sync 항목)** — DP-1은 결정 15의 "실 리뷰 미크롤링·전량 합성" 조항을 전제 변경(실 리뷰가 이미 존재)에 따라 재해석했다. 실 리뷰를 search_doc 요약 입력으로만 사용하고 합성 리뷰 생성을 고도화로 유예한 결정을 product.md 결정 15-A로 개정 제안한다(문서 동기화 항목 — manager-docs sync 대상, 본 SPEC 구현 차단 아님). **교차 SPEC sync 대상 추가(D18)**: SPEC-EVAL-001 spec.md:47(의존 서술의 "…소모품 플래그·합성 리뷰")의 "합성 리뷰" 문언이 DP-1 재해석 이전 상태로 잔존한다 — 이는 SPEC-EVAL-001 소관 문서이므로 본 SPEC 개정에서 편집하지 않고, manager-docs sync에서 "실 리뷰 요약 기반(합성 리뷰 유예)"으로 정합화하도록 후속 항목으로 등록한다.
- **OPEN-C5 카테고리 트리 Spring 합의** — 카테고리 트리 20~30개 최종 확정은 Spring 팀 협의 항목(§10). SPEC에 제안 확정본을 싣고 합의 변경 시 개정(비차단, interview.md R1-Q3).
- **OPEN-C6 frozen 스냅샷 저장·태깅 소유권 이중 배정(SPEC-EVAL-001 REQ-EVAL-043 경계, D1)** — 본 SPEC REQ-CAT-035/038은 스냅샷 저장·버전 태깅을 본 SPEC 소유로 규정하나, SPEC-EVAL-001 REQ-EVAL-043은 "The 하니스 shall … frozen 스냅샷(`catalog_snapshot_vN`)을 저장·버전 태깅"으로 동일 행위를 자기 소유로 규정하여 소유 주체가 두 SPEC에 이중 배정된다. **제안 경계 규범**: 스냅샷의 **생산·저장·버전 태깅은 본 SPEC(카탈로그 구축)**이 수행하고, **SPEC-EVAL-001 하니스는 이미 태깅된 vN을 참조·기록**(골든셋 라벨에 vN 결속·재현성 검증)만 한다 — 카탈로그 상태를 아는 주체가 본 SPEC이므로 생산 소유가 자연스럽다. **후속(비차단)**: "SPEC-EVAL-001 REQ-EVAL-043 경계 조정 제안"(REQ-EVAL-043을 '참조·기록' 문언으로 조정) — SPEC-EVAL-001은 본 SPEC 범위 밖이므로 본 개정에서 편집하지 않고 협의 항목(§10)·manager-docs sync로 승계한다. 이 이중 배정이 해소되기 전까지는 제안 경계 규범을 임시 규범으로 적용한다.

---

## 10. Spring 협의 항목 (Coordination Items — non-blocking)

interview.md R1-Q3 확정: SPEC에 제안 확정본을 싣고 Spring 합의는 선행 차단 조건이 아닌 협의 항목으로 처리(합의 변경 시 SPEC 개정).

1. 카테고리 트리 최종 확정(20~30개, 2단계) — 공용 시드 전제(결정 15).
2. 시드 스키마 합의 — `product_id` 타입·`attributes` 구조(결정 9-B 계약).
3. 크롤 데이터 ↔ Spring 원본 DB 간 공용 시드 전달 포맷.
4. **frozen 스냅샷 저장·태깅 소유권 경계(SPEC-EVAL-001 REQ-EVAL-043, OPEN-C6)** — 본 SPEC이 스냅샷을 생산·저장·버전 태깅하고 SPEC-EVAL-001 하니스는 vN을 참조·기록하는 경계를 제안한다. "SPEC-EVAL-001 REQ-EVAL-043 경계 조정 제안"(REQ-EVAL-043을 '참조·기록' 문언으로 조정)은 SPEC-EVAL-001 소관 문서 변경이므로 본 SPEC 개정에서 편집하지 않고 협의·후속 sync 항목으로 처리한다(비차단).
5. (기존) 상품 변경 통지·장바구니 API·세션 종료 통지·주문 이벤트 — 본 SPEC 비범위(별도 카탈로그 이벤트 SPEC).

---

## 11. MX 태그 계획 (mx_plan — run 단계 적용 컨텍스트)

그린필드 SPEC(기존 구현 코드 없음, 계획 저장소)이므로 기존 코드 스캔은 해당 없음. 아래는 run 단계에서 `app/pipelines/` 코드 생성 시 적용할 어노테이션 전략이며, run 워크플로 에이전트에 컨텍스트 제약으로 전달된다.

| 태그 | 대상 (예상 fan_in / 위험) | 근거 |
|------|--------------------------|------|
| @MX:ANCHOR | 파이프라인 진입점(`ingestion_service.run` / `enrichment_service.run` / `embedding_service.run` / `load_service.run`), 스키마 계약(products/카테고리 Pydantic 모델 — FilterSet 계약 소비처 참조), search_doc 조립 함수 | 고 fan_in 불변 계약 |
| @MX:WARN | Haiku 배치 호출 경로(비용·비결정성 — 배치 크기 config), 임베딩 배치(메모리·CPU 부하), 배치 재시도·재개 경로(REQ-CAT-030 — 재과금 방지 체크포인트·멱등 스킵), 카테고리 매핑 fallback(미매핑 처리 — 오분류 시 필터·억제 오작동), 실 리뷰 요약 PII 가드 경로 | 위험 구역, @MX:REASON 필수 |
| @MX:NOTE | 통제 어휘 상수(상황 태그 15~25·속성 사전 통제 값 집합), 소모품 플래그 계약(REQ-REC-103 소비), catalog_snapshot_vN 아티팩트 계약·vN 채번·소비자 해석 규칙(REQ-CAT-038), config 키 계약 | 비즈니스 규칙·의도 전달 |
| @MX:TODO | RED 단계 테스트 요구사항 표기, GREEN 단계에서 해소 | TDD 사이클 관례 |

---

## 12. 참조 (References)

- `.moai/specs/SPEC-CATALOG-DATA-001/interview.md` — 사용자 확정 결정(크롤러 제외·엔드투엔드 적재·신규 정의 3종·데모 우선·실 리뷰 긴장·규모 ~10k).
- `.moai/specs/SPEC-CATALOG-DATA-001/research.md` — §2 인덱스 스키마 계약, §3 파이프라인 계약, §4 리뷰 처리 제약, §5 의존 SPEC 계약, §6 신규 정의 3종, §7 저장소 관례, §8 리스크.
- `.moai/specs/SPEC-CATALOG-DATA-001/plan.md` — 승인된 계획(5모듈·M0~M4·의존성 계층·mx_plan·Decision Point 1 해소).
- `.moai/project/product.md` — 결정 3/4/5/6/9-A/9-B/10-A/14-F/15/16/17.
- `.moai/specs/SPEC-RECOMMEND-001/spec.md` — §5.2 FilterSet/Candidate 스키마(본 SPEC이 채우는 컬럼 계약), REQ-REC-103(소모품 플래그 소비), EX-6(색인/임베딩 오프라인 소관).
- `.moai/specs/SPEC-EVAL-001/spec.md` — REQ-EVAL-005(카탈로그 완료 blocking), EX-EVAL-2(frozen 스냅샷 소비만), catalog_snapshot_vN.
