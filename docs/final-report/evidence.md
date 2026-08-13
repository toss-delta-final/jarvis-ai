# Jarvis AI 최종 보고서 근거 원장

작성 기준일: 2026-08-13  
대상: `jarvis-ai` 저장소의 AI 서버 구현과 저장된 평가 산출물

이 문서는 최종 보고서의 주장과 수치를 저장소 근거에 연결한다. 코드가 존재한다는 사실과
품질이 검증됐다는 사실은 구분하며, 서로 다른 평가셋의 수치를 합쳐 하나의 종합 점수로
표현하지 않는다.

| 보고서 위치 | 주장 또는 수치 | 근거 파일 | 사용 조건·한계 |
|---|---|---|---|
| 1장 | Jarvis는 자연어 상품 탐색, 개인화 추천, 장바구니 실행을 연결하는 에이전틱 커머스 AI 서버다. | `README.md`의 프로젝트 개요·핵심 기능 | React와 Spring 내부 구현은 이 저장소의 산출물로 주장하지 않는다. |
| 1장 | 시스템은 React 프론트엔드, Jarvis AI, Spring 백엔드의 3-tier로 역할을 나눈다. | `README.md`의 시스템 아키텍처 | 이 보고서는 AI 서버 관점의 경계만 상세히 설명한다. |
| 1장 | AI 서버는 구매자·판매자 Agent와 개인화 프로필 파이프라인을 제공한다. | `README.md`, `app/agents/buyer/`, `app/agents/seller/`, `app/agents/profile/` | 기능 존재와 실제 사용자 효과를 동일시하지 않는다. |
| 2장 | 자연어 요청은 구조화 필터와 semantic query로 분해되어야 한다. | `README.md` 프로젝트 개요, `app/agents/buyer/recommendation/decompose.py` | LLM 해석은 확률적이므로 결정론적 필터·검증과 함께 사용한다. |
| 2장 | 추천 근거는 후보 데이터 밖의 수치나 속성을 말할 위험이 있다. | `evals/adversarial_recommendation/README.md`, `evals/rerank_grounding/README.md` | 근거 검증 결과는 등록된 평가 축에 한정한다. |
| 2장 | 화면 밖 상품 ID나 중복 ID가 장바구니·추천 경계를 넘어가면 안 된다. | `app/agents/buyer/graph.py`, `app/agents/buyer/recommendation/graph.py`, `evals/rerank_grounding/README.md` | 평가 artifact의 guardrail 수치는 rerank 결과에 대한 값이다. |
| 2장 | 스트림 취소, 타임아웃, 동일 thread의 동시 요청은 운영 요구사항이다. | `README.md`의 SSE 스트림 수명주기, `app/api/chat.py` | 보고서는 설계·테스트된 제어를 설명하며 실제 운영 SLA를 주장하지 않는다. |
| 3장 | FE는 Spring이 발급한 JWT로 AI의 SSE API를 직접 호출하고, AI는 JWKS로 신원을 검증한다. | `README.md` 시스템 아키텍처, `app/core/auth.py`, `app/api/deps.py` | 운영 모드와 로컬 dev 인증 모드를 구분한다. |
| 3장 | 추천 상품 카드는 SSE에 원본을 중복 싣지 않고, AI가 목록을 push한 뒤 FE가 Spring에서 최신 표시 데이터를 조회한다. | `README.md`의 경로 B, `docs/api-spec.md`, `tests/integration/test_buyer_flow_e2e.py` | 표시 데이터의 권위는 Spring에 있다. |
| 3장 | 상품·주문·장바구니 원본은 Spring, search document·embedding·profile 등 AI 산출물은 PostgreSQL에 둔다. | `README.md` 데이터 소유 분리, `docker-compose.yml`, `db/` | AI가 커머스 원본 전체를 소유한다고 표현하지 않는다. |
| 3장 | 주요 기술은 Python 3.12, FastAPI, LangGraph, Pydantic v2, PostgreSQL·pgvector다. | `pyproject.toml`, `README.md` 기술 스택 | 버전 범위는 `pyproject.toml`을 우선한다. |
| 4장 | Buyer Agent는 recommend, order status, general, cart, wishlist 관련 의도를 전용 경로로 분기한다. | `app/agents/buyer/graph.py`, `app/agents/buyer/cart/`, `app/agents/buyer/order_status.py` | LangGraph 개념은 시스템 역할 설명에 사용하고 세부 구현은 현재 코드 경로를 우선한다. |
| 4장 | 추천 경로는 decompose, Spring search, rerank, recommendation push를 연결한다. | `app/agents/buyer/graph.py`, `app/agents/buyer/recommendation/graph.py` | 장애 시 fallback·degrade가 있으므로 모든 턴이 동일 경로를 완주하지는 않는다. |
| 4장 | Seller Agent는 조회·분석·상품 변경 draft를 분리하고, 승인에는 구조화된 action과 draftId를 요구한다. | `app/api/seller.py`, `app/agents/seller/hitl.py`, `docs/api-spec-seller.md` | 보고서에는 AI가 직접 원본을 수정하지 않고 검토 가능한 draft를 만든다고 표현한다. |
| 4장 | 프로필 승격은 salience와 explicit 또는 repetition 조건을 함께 사용한다. | `app/agents/profile/gate.py`, `app/agents/profile/builder.py` | 모든 발화를 장기 기억으로 저장한다고 표현하지 않는다. |
| 4장 | 명시적 session-end와 inactivity timeout은 공통 finalizer를 사용한다. | `app/api/events.py`, `app/agents/profile/finalizer.py`, `app/agents/profile/idle_timeout.py` | idle은 재활동 가능한 checkpoint이며 영구 종료 신호와 구분한다. |
| 5장 | 장바구니는 담기·조회·삭제·수량 변경을, 찜은 추가·조회·삭제를 별도 경로로 처리한다. | `app/agents/buyer/cart/`, `app/agents/buyer/graph.py` | 실제 트랜잭션 권위는 Spring API에 있다. |
| 5장 | 화면 지시어와 이전 추천 상태를 이용해 “두 번째 상품” 같은 후속 지시를 해소한다. | `app/agents/buyer/screen_reference.py`, `app/agents/buyer/cart/state.py`, `app/agents/buyer/graph.py` | 해소 실패 시 임의 상품을 선택하지 않고 되묻거나 안전하게 종료한다. |
| 5장 | 판매자 분석은 통계 계산, 검증, chart/report, action candidate를 단계화한다. | `app/agents/seller/sop/`, `app/agents/seller/pipeline.py`, `app/api/seller.py` | 합성·회귀 평가가 운영 매출 효과를 보증하지 않는다. |
| 5장 | 구매자 SSE는 token·conditions·action·products.ready·done·error 등을, 판매자 SSE는 token·draft·report·done·error 등을 사용한다. | `README.md` API 요약, `docs/api-spec.md`, `app/api/chat.py`, `app/api/seller.py` | 이벤트 이름은 보고서 작성 시 현재 계약을 기준으로 요약한다. |
| 6장 | 상품 변경 동기화는 Spring 변경분을 AI가 pull해 AI 생성물을 upsert하는 구조다. | `README.md`, `app/pipelines/`, `docker-compose.yml` | 상품 원본 전체 복제나 CDC를 사용한다고 쓰지 않는다. |
| 6장 | 구조화 조건은 정확 필터에, embedding은 의미 유사도에, rerank는 최종 의미 적합성에 사용한다. | `README.md`, `app/services/search_service.py`, `app/agents/buyer/recommendation/` | 검색 백엔드와 평가 설정에 따라 세부 순서는 달라질 수 있다. |
| 6장 | REES46 2019년 10월 데이터는 총 42,448,764 이벤트, 3,022,290 사용자, 9,244,422 세션, 166,794 상품을 포함한다. | `data-analysis/REPORT.md` | 서비스 운영 데이터가 아니라 데이터셋 구성·분포 참고 자료다. |
| 6장 | 필터 후 42,418,053 이벤트가 남았고, 이는 전체의 99.93%다. | `data-analysis/REPORT.md` | remove-from-cart와 봇 후보 세션 제외 규칙에 따른 값이다. |
| 6장 | 월간 구매 전환 사용자 비율은 11.49%, 구매자 중 월내 재구매는 32.17%다. | `data-analysis/REPORT.md` | 1개월 자료이며 재방문·재구매를 장기 지표로 해석하지 않는다. |
| 6장 | 상위 10% 상품이 조회의 82.64%를 차지한다. | `data-analysis/REPORT.md` | 인기도 편향을 재현하는 더미 데이터 분포 근거로만 사용한다. |
| 7장 | 구매자 adversarial recommendation 데이터셋은 210 family에서 450 minimal-mutation case를 생성한다. | `evals/adversarial_recommendation/README.md`, `evals/README.md` | exact 문장 점수가 아니라 behavioral invariant 중심이다. |
| 7장 | 42 family, 즉 category별 20%를 네 기준으로 직접 재검토했다. | `evals/adversarial_recommendation/README.md` | 표본 검토가 전체 의미 품질의 사람 평가를 대신하지 않는다. |
| 7장 | validated rerank의 unsupported evidence는 screening 0/80, confirmation 1차 0/212, 2차 0/208이었다. | `evals/rerank_grounding/README.md` | 평점·리뷰·후보군 상대가격·정확한 숫자 근거에 한정한다. |
| 7장 | 두 confirmation 합산 비교는 A 28/411(6.81%), B 0/418, C 0/420이었다. | `evals/rerank_grounding/README.md` | B와 C가 모두 0이므로 validator의 추가 효과까지 분리해 주장하지 않는다. |
| 7장 | 세 rerank grounding run에서 후보 밖 ID, 중복 ID, validation 후 invalid evidence, unfilled cell은 모두 0이었다. | `evals/rerank_grounding/README.md` | 등록된 10개 case와 반복 설정의 결과다. |
| 7장 | validated의 p50/p95 지연은 screening 2,663/4,407ms, confirmation 1차 2,912/4,846ms, 2차 2,820/5,438ms였다. | `evals/rerank_grounding/README.md` | 특정 provider·model·dataset hash의 수동 live 평가이며 일반 SLA가 아니다. |
| 7장 | grounding 평가는 전체 571 attempts 중 확인 가능한 511,192 tokens와 0.2392064달러를 기록했다. | `evals/rerank_grounding/README.md` | 미확정 사용량 1건이 있어 확인 가능한 사용량만 합산한다. |
| 7장 | 판매자 trigger 하네스는 정상 변동 1,000일 시뮬레이션, 이상 주입 10종, 군집 ARI를 검증한다. | `evals/seller_trigger/README.md` | 합성 분포가 실제 브랜드의 과분산·프로모션을 재현하지는 않는다. |
| 7장 | 판매자 이상 주입 golden set 10종은 모두 통과했다. | `evals/seller_trigger/reports/goldenset-seller-trigger-v1.json` | 저장된 `seller-trigger-v1` 결정론 회귀 결과다. |
| 7장 | 정상 변동 시뮬레이션의 tier-1 열림률은 4개 시나리오에서 0.000~0.308%로 1.00% gate 아래였다. | `evals/seller_trigger/reports/null-sim-seller-trigger-v1.md` | 실제 운영 발동률의 상한을 보증하지 않는다. |
| 7장 | 고객 군집 안정성 ARI는 300행·5% noise에서 1.0으로 0.7 gate를 통과했다. | `evals/seller_trigger/reports/ari-seller-trigger-v1.json` | 상품 축 ARI는 exploratory이며 고객 축만 gate다. |
| 7장 | E2E 스모크는 Buyer 경로 B, Profile finalization, Batch pull, degrade, 운영 인증 경계를 HTTP stub과 주입형 LLM으로 검증한다. | `README.md`의 E2E 스모크 하니스, `tests/integration/` | 실제 Spring·실 LLM 네트워크 품질을 보증하는 테스트는 아니다. |
| 8장 | 핵심 설계 성과는 LLM의 의미 판단과 코드의 제약·근거·권한 검증을 분리한 것이다. | `app/agents/buyer/recommendation/`, `app/agents/seller/`, `evals/README.md` | 종합 사용자 만족도나 매출 개선 수치로 확대하지 않는다. |
| 8장 | 남은 한계는 외부 Spring·LLM 의존, 합성 평가와 제한된 live case, 실제 사용자 실험 부재다. | `README.md`, `evals/README.md`, 각 평가 README의 제한 절 | 향후 개선 항목으로 제시하며 완료 성과로 표현하지 않는다. |

## 보고서 작성 금지선

- 저장소에 없는 팀원 이름, 역할, 학교·기관, 영상 링크를 만들지 않는다.
- README 하단의 과거 상태표를 현재 구현 완료율로 사용하지 않는다. 현재 코드를 설명하되
  전체 제품의 배포 완료를 주장하지 않는다.
- 서로 다른 case 수, 합성 시뮬레이션, live run을 더해 하나의 “총 정확도”로 만들지 않는다.
- REES46 월간 통계를 Jarvis 운영 사용자의 전환 성과로 표현하지 않는다.
- rerank grounding 결과를 모든 자연어 추천 이유의 사실성 보증으로 확대하지 않는다.
- 합성 판매자 trigger 평가를 실제 브랜드에서의 오탐률 상한으로 표현하지 않는다.
