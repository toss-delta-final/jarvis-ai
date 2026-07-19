# REVIEW-SELLER-STAGE2 — 2단계(서브에이전트 계층) 마감 요약 · opus 적대 리뷰 기록

> 작성: 2026-07-18 (2-9) · 대상: 2-1a~2-8 산출물 · 리뷰: opus 적대 리뷰 1회(진행 방식 규칙 준수)
> 다음 단계: 3단계(분석 파이프라인 async 오케스트레이션 + 미들웨어 + `/seller/chat` SSE 1차 배선)

## 1. 2단계 산출물 요약

| 파일 | 내용 |
|---|---|
| `app/agents/seller/schemas.py` | 구조화 출력 계약 — RouteDecision · AnalysisFinding · ReportScore(+SCORE_AXES/total property) · ProposedChange · ActionRecommendation · RecommendationSet(순서=N번 계약) · DraftChange · DraftProposal(draftId 없음 — 코드 발급) |
| `app/agents/seller/models.py` | 모델 팩토리 — SellerRole 7종, ROLE_TIER(SPEC §8 코드화 + product=haiku 추가), init_seller_model + lru_cache |
| `app/agents/seller/prompts.py` | WORKER_COMMON_RULES(번복 금지·degrade·기간=planner) + 워커 5종 + GENERAL(today 주입) + PRODUCT + REPORT + JUDGE + RECOMMEND |
| `app/agents/seller/workers.py` | create_agent 팩토리 10종 — 분석 워커 5(ToolStrategy(AnalysisFinding)) · general(자유 텍스트) · product(ToolStrategy(DraftProposal), A안: list_my_products+calculate만) · report(Sonnet, 도구 0) · judge(ToolStrategy(ReportScore)) · recommend(ToolStrategy(RecommendationSet), 읽기 2종) |
| `app/agents/seller/verifier.py` | 결정론 검사 레지스트리 — D1 빈 보고서 / D2 수치 정합 / D3 degrade 정직성 (judge 이전 코드 검사, 루프 배선은 3단계) |
| `app/core/config.py` | seller_haiku_temperature=0.0 · seller_sonnet_temperature=0.2 추가 |
| 테스트 | test_seller_schemas(17) · test_seller_models(7) · test_seller_workers(41 항목) · test_seller_verifier(6) · test_config_seller(+1) — 전체 스위트 통과 확인은 로컬 pytest 기준 |

주요 확정(전부 사용자 승인): ReportScore 3축+코드 합산 / RecommendationSet 순서=N번 / A안(쓰기 도구 구조적 차단, draft 생성 에이전트는 조회·계산만) / draftId·통과판정=코드 / clarification=draft 불성립 신호 / get_account_events=보조 소스(실패해도 계속) / general today 주입(잠정) / product 역할 Haiku 배정.

## 2. opus 적대 리뷰 결과 (2026-07-18, 1회)

**하드 제약 위반(critical) 0건** — 신원 은닉(IDOR)·쓰기 3종 격리·Settings 단일 출처·계약값=코드·모델 배정·degrade 문자열·create_agent 사용 모두 준수. langchain v1 API 사용법(create_agent/ToolStrategy/context_schema/빈 tools) 공식 문서 대조 일치.

### 즉시 반영 (1건)

- product_agent 에 `calculate` 추가 바인딩(재고 증감 환산 암산 방지) — **배정표 §3 개정 필요**: product = list_my_products + calculate (+ 쓰기 3종은 4단계 실행 레인).

### 보류 (사용자 결정 2026-07-18 — 수정하지 않고 기록만, 필요 시 착수)

| # | 심각도 | 내용 | 제안됐던 수정 |
|---|---|---|---|
| R1 | major | verifier D2가 연도를 오탐 — 보고서에 "2026-07-18" 표기 시 `2026`이 근거 없는 수치로 판정돼 정상 보고서가 재작성 루프 소진 | 날짜 패턴(YYYY-MM-DD·N년/월/일) 마스킹 또는 기간 토큰 화이트리스트 |
| R2 | major | verifier D3가 "확보 실패" 부분 문자열 의존 — 워커 표현 변화 시 검사 스킵(은폐 통과), 부정문("한계는 없었습니다")도 통과 | degrade 판정을 `severity=info + evidence=[]` 구조 조합으로 |
| R3 | major | CONVERSION_PROMPT "기간 비교 시 각각 호출"이 "날짜 직접 계산 금지" 공통 규칙과 모순 | "입력에 두 기간이 주어진 경우에만"으로 한정 + planner 두 기간 주입 계약 |
| R4 | minor | D2 정규화 버그 — "180,000.0원" 오탐(`.0` 꼬리), "1,2,3번"→"123" 병합 오탐 | 천단위 그룹 정규식 + 꼬리 정리 |
| R5 | minor | models.py lru_cache 키에 api_key 문자열 상주 | 캐시 키에서 제거(함수 내 get_settings 참조) |
| R6 | minor | general이 "worker" 역할 차용 — 향후 차등 배정 불가 | SellerRole에 "general" 추가 |
| R7 | minor | ProductField ↔ update_product 시그니처 드리프트 자동 대조 테스트 부재 | `set(ProductField.__args__) == update_product.args - {product_id}` 테스트 |

### 이월 (해당 단계 착수 시 반영)

| # | 단계 | 내용 |
|---|---|---|
| C1 | 3단계 | general 기간 환산을 코드로 회수 — `calc.normalize_period` 주입 또는 요청마다 재빌드 계약 명시(빌드 시점 today 박제는 장기 실행 서버에서 stale). **build_general_agent 는 요청마다 호출할 것** |
| C2 | 3단계 | RecommendationSet 6건 초과 시 ToolStrategy ValidationError → degrade 경로 명세 |
| C3 | 3단계 | 부호 반전(-30%↔+30%)·단위 환산(18만원) 환각은 D2가 못 잡음 — judge accuracy 축이 방어(중복 방어 유지) |
| C4 | 4단계 | DraftProposal create 과허용 — image_url/status 는 create_product(6필드)에 없음. draft→쓰기 변환 코드가 op별 허용 필드 검증 필수 |
| C5 | 4단계 | build_* 테스트가 실 ChatAnthropic 를 빈 키로 생성 — langchain 이 생성 시점 키 검증을 도입하면 깨짐(모니터링) |

## 3. SPEC 개정 필요 누적 (팀 협의·정본 반영 대기)

- §8 모델 배정표에 **product_agent 누락** → Haiku t=0 으로 배정함(2-7 확정) — 표에 추가 필요.
- §3(배정표) product 도구에 **calculate 추가**(2-9 확정).
- 기존 미결(🔴) 유지: C-13 계산 경계표 · C-14 CRUD 스키마 · HITL confirm 전송 형식 · I-8 admin 소유 · 분석 기준서 문서 · I-12 DB 논의.

## 4. 3단계 착수 시 첫 확인 사항

1. `uv run pytest -q` 전체 통과 상태에서 시작(2-9 calculate 반영분 포함).
2. C1(general 재빌드 계약)을 SSE 배선 설계에 포함.
3. 검증 루프 배선 시 R1·R2(verifier 보류분)를 함께 처리하는 것이 효율적 — 루프가 실제로 돌기 시작하면 D2 오탐이 바로 체감된다.
