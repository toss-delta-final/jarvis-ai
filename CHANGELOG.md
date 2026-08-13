# Changelog

- **#465 category leg head 억제 기본 활성화** — 조건 전용 총칭 leg만 후처리로 제거한다(LLM 호출 0). 보호 대상 오발동 0건·조건 누출 런당 2건 제거를 근거로 기본 on 했으며, primary missRate는 병합 후 표적이 작아 개선되지 않았다.

이 프로젝트의 주요 변경을 기록한다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/),
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따른다.

기록 규칙: **기능/주제가 완료(PR 병합)될 때마다** 해당 항목을 추가한다. 유형은
`Added`(신규) · `Changed`(변경) · `Fixed`(수정) · `Removed`(제거) · `Docs`(문서) · `Security`(보안).
계약(api-spec) 변경을 수반하면 `(api-spec §, vX.Y)`를 함께 적는다.

## [Unreleased]

### Added
- **#631 — 구매자 rerank 순위 계산을 `current`·`structured`·`hybrid` arm으로 선택할 수 있게 했다.**
  `structured`는 LLM의 제한된 intent/need/profile 점수를 결정론적 `4:2:1` 합산과 명시적
  tie-break로 정렬하고, `current`는 기존 LLM 순위·파서·fallback을 그대로 유지한다.
  `hybrid`는 원래 search rank와 scored rank를 설정 가능한 RRF
  (`RERANK_RRF_ALPHA`, `RERANK_RRF_K`)로 결합하며, 프로필이 없으면 profile 점수를 0으로
  강제한다. 후속 `code_assisted`는 가격·평점·리뷰·프로필 일치 등 코드 신호를 만든 뒤 LLM이
  최종 후보와 이유를 선택한다. 순위 arm은 기존 `RERANK_GROUNDING_ARM`과 독립적으로 opt-in할
  수 있고 API·SSE wire 계약은 바뀌지 않는다. Heuristic draft nDCG에서는 structured가 높았지만
  position-swapped blind judge는 current를 167:15로 선호했고, 180-case code-assisted 비교에서도
  current가 78:15로 우세했다. code-assisted는 평균 노출 2.20개로 current 3.63개보다 useful
  coverage가 부족했다. 따라서 `RERANK_RANKING_ARM=current`를 production 기본으로 유지하고
  structured·hybrid·code-assisted는 평가용 선택지로만 남긴다. 모델 가격표도 공식 Luna/Sol
  단가로 갱신해 이후 평가 예산 gate가 실제 단가를 사용하게 했다.
- **#662 — 구매자 장바구니 옵션 재질문에 대상 상품명을 표시한다**
  (api-spec §3.1·§4.1, v0.33.2). 기본·조건 좁힘·색상 미충족
  `CART_OPTION_REQUIRED`, I-1 힌트 폴백, `CART_OPTION_INVALID`의 다섯 경로에서
  최종 확정 `productId`의 이름을 추천 상태 또는 현재 `screen.products`에서 찾아 기존
  옵션 목록 앞에 `**상품:** <상품명>`으로 표시한다. 현재 화면 이름을 우선하고,
  `_strip_unsafe` 정제 후 40자를 초과하면 앞 40자와 `…`를 쓴다. 이름이 없으면 기존
  문구를 그대로 유지하며 표시 이름은 `PendingAdd`·Spring 요청에 저장하지 않는다.
  SSE·Spring·FE·DB 계약과 옵션 선택·자동 선택·추가금·pending 동작은 불변이다.
- **#634 관측 집계 스크립트 비용 축에 min/max·role 분해 추가** — `_cost_stats()`가 최소/최대
  비용을 반환하도록 확장하고, 비용 롤업에 `role`(seller/member/guest)·`model`(fan-in
  귀속)·`length`(`messageLength` 고정 버킷) 축을 신설했다. Markdown 비용 표에 최소/최대(USD)
  열을 추가하고 그룹 라벨을 latency 표와 동일한 `dimension:group` 규약으로 통일했으며, CSV
  metric에도 min/max를 포함했다. 버킷 경계는 `app/core/config.py`의 신규 튜너블
  `observability_length_buckets`로 주입한다(실측 분포 전 추정치). 계약(api-spec) 무영향.
- **#645 buyer `overallComment` 최종-view grounding**을 추가했다. B/C rerank 출력은 목록 전체
  `overallClaims`를 구조화해 보존하고, production C는 repurchase pinning·노출 보충/절단·니즈
  분할·BUY_ALL budget-set 계산 뒤의 실제 I-21 product groups에 대해 claim을 검증한 후 고정
  template만 노출한다. 지원 범위는 raw `reviewCount` 최댓값, 전 상품 high rating, 각 BUY_ALL
  조합의 총예산 충족이며, 정본 metric이 없는 popularity/value-for-money 최상급은 중립 문구로
  강등한다. fixture v2는 기존 10 case와 overall 전용 12 case의 allowed/forbidden oracle을 raw
  데이터로 재검산하고, A bounded detector·B/C 구조화 정확도·coverage·downgrade·지연·token·비용
  artifact를 기록한다. 기존 #632 A 표본 재채점은 등록 표현 11건 중 위반 1건(9.09%)이었다.
  현재 worktree에는 live provider credential이 없어 새 N=3 및 N=8×2는 `not tested`로 남았으며,
  deterministic smoke만으로 병합 품질을 주장하지 않는다. `RERANK_GROUNDING_ARM=current`는 상품별
  근거와 전체 코멘트를 함께 기존 A로 되돌리고 Spring/CH-5/SSE wire 계약은 바뀌지 않는다.
- **#653 — 구매자 채팅에 같은 방 전용 계층형 메모리와 캐시 인지 비용 계측을 추가했다.**
  최근 대화는 최대 3쌍·1,000 추정 토큰, 상황 요약은 400 추정 토큰으로 제한하고, 밀려난
  고가치 대화가 1,200 추정 토큰 이상일 때만 다음 턴용 요약을 비동기로 갱신한다. 새 방에는
  기존 취향 프로필만 전달하며 자유대화 상황은 전달하지 않고, 옵션 답변·action-only 턴과
  메모리 저장 장애는 기존 응답 경로를 그대로 유지한다. 요청 로그는 공급자 actual usage의
  캐시 읽기·쓰기 토큰과 메모리 압축 비용을 원문 없이 분리 기록한다. API·SSE·DB 스키마와
  LangGraph 그래프 구조는 변경하지 않았다.
- **#638 구매자 adversarial 데이터셋에 rerank grounding A/B/C 평가 연결**을 추가했다.
  `--arms all`로 현행·구조화 prompt-only·validator 표시 결과를 같은 case에서 기록하며, B와 C는
  같은 구조화 LLM 응답을 공유해 validator 효과가 모델 표본 차이에 섞이지 않는다. 평가 runner의
  arm 주입과 기본 `current`는 production arm 설정과 독립이다.
- **buyer rerank grounding 수동 A/B/C 평가 도구**(`evals/rerank_grounding`)를 추가했다.
  현행 자유문장, 구조화 prompt-only, 구조화+결정적 validator를 같은 hashed MFT/INV/DIR fixture에서
  비교하고 raw response·분자/분모·hard gate·prompt/model/dataset hash·비용/지연을 보존한다.
  사람 평가는 제외하며 production 승격 근거와 별도로 탐색적 결과를 보존한다.
- **#641 — 실제 LLM 비용 없이 배포 EC2의 두 성능 경계를 분리 측정하는 scripted 부하 테스트
  프로파일**을 추가했다. `SCRIPTED_LLM_MODE=instant`는 기존 결정론 응답을 지연 없이 돌려
  FastAPI·DB·Spring의 처리량 상한을 재고, `delayed`는 요청별 `LoadTestLLM` 인스턴스에서
  기본 5초의 비동기 대기를 한 번만 적용해 오래 열린 SSE 연결의 동시성·메모리·timeout을
  측정한다. decompose·rerank 호출마다 지연이 중복되지 않으며, 기동 배너가 프로파일과 지연값을
  명시한다. GitHub Actions 배포는 scripted 설정과 테스트용 rate-limit 변수를 EC2 env 파일로
  전달하되 미등록/빈 값은 제거해 기존 코드 기본값을 보존하고, rate-limit은 양수만 허용한다.
  scripted 중에는 I-17 카탈로그 enrichment 배치를 차단해 가짜 생성물의 실 DB 저장을 막는다.
  구매자 추천 외 레인, 실제 provider
  네트워크·429·토큰 편차와 k6 부하 발생기는 범위 밖이며 API/SSE 계약 변경은 없다.
- **#637 — 구매자 추천 adversarial/behavioral 평가 데이터셋**을 추가했다. 실제
  `BuyerChatRequest`와 Spring I-1 `SpringProduct` wire schema를 사용하는 7개 failure mode별
  30 family(총 210 family, 450 minimal-mutation case)를 결정론적으로 생성한다. 숫자 oracle,
  family 불변식, unintended mutation, 경계 contrast를 validator가 검사하며, 고정 seed로
  category별 20%를 층화 추출한 42 family 직접 재검토 기록도 함께 검증한다. 판매자 경로와
  임의 schema는 포함하지 않는다.
- **#585 — 판매자 분석 저장 계층: DDL 5테이블 + 리포지토리 + targets 자동 등록 훅**을 추가했다
  (OPS-RUNTIME.md §1.3~§1.7, 결정 71·72·80·110~112). `db/profile/init/05_seller_analysis.sql`에
  `seller_analysis_targets`·`snapshots`·`reports`·`recommendations`·`outcomes` 5테이블을 신설하고,
  `app/agents/seller/analysis_store.py`(전용 pg-profile 커넥션 풀, 부팅 시 idempotent 스키마
  생성+검증, `SET LOCAL statement_timeout` 쓰기 경계 + 1회 재시도, 보고서+추천 단일 트랜잭션
  저장)와 `app/agents/seller/analysis_records.py`(저장 전용 Pydantic 레코드 모델 4종)로 CRUD를
  제공한다. `/seller/chat` 스트림 진입부에 fire-and-forget targets 자동 등록 훅을 달아
  브랜드가 접속할 때마다 무인 분석 대상으로 등록되게 했다(`require_seller`는 buyer 와 공용이라
  훅 위치에서 제외). 소비자(buyer) 경로·jarvis-front·jarvis-back 은 변경하지 않았다.
- **#589 — 판매자 상주 analysis 파이프라인의 SOP 층**(`app/agents/seller/sop/`)을 신설했다.
  `run_sop` 은 스텝을 순차 실행하며 예외를 `Hold` 로 흡수하는 것이 전부다(조건 분기·재시도·
  롤백 없음 — `01-ARCHITECTURE.md` §4.1). 실패해도 raise 하지 않고 부분 채워진 ctx 를
  돌려주는데, 그 부분 결과 + `holds` 가 무인 실행 실패 규약(`OPS-RUNTIME.md` F-3)의 재료다.
  `AnalysisContext` 는 LLM 이 보는 유일한 입력으로, 수치·판정·원인 후보를 전부 코드가 채우고
  LLM 은 `Segment.llm_label`·`llm_desc` 둘만 쓴다. `Verdict.verdict` 에는 기존
  `RateComparison`(3종)에 없는 **`undecided`** 를 넣어 "판정 보류 ≠ 이상 없음"을 타입으로
  가른다(감사 C-12). SOP 스텝 타임아웃 5키(`seller_sop_*_timeout_s`: load 5 / compare 5 /
  compute 30 / feedback 3 / interpret 30)를 신설했다 — `compute` 만 30s 인 것은 K-Means 를
  PCA on/off × k 후보 5개 = 학습 10회 돌리기 때문이다(`OPS-RUNTIME.md` T-3). 계약 변경 없음.
- **#153 — buyer-only blind pairwise 사람 평가 패키지** (`evals/blind_pairwise/`)를 추가했다.
  수집 전 고정된 seed A/B 배정, 비식별 raw response schema, tie/abstain 보존, rubric별 ordinal
  분포, 분모가 명시된 Wilson 95% interval, Krippendorff alpha, 선택적 LLM judge 비교와
  재현 가능한 artifact 분석을 제공한다. evaluator별 blind presentation 분리, pair-input/
  preregistration hash provenance, constrained A/B balance와 엄격한 3-of-3 coverage gate를
  포함한다. 실제 human response는 포함하지 않으며 결과는 exploratory로만 해석한다.

### Fixed
- **구매자 후속 정정 발화가 직전 추천의 충돌하는 카테고리를 그대로 승계하던 문제를 고쳤다.**
  같은 방의 최근 완료 대화 최대 3쌍을 참고하되, 현재 발화가 이전 요청의 정정·추가 조건이면
  가장 가까운 관련 요청과 결합해 완전한 검색 의도로 재구성한다. 현재 사용자의 정정은
  `PRIOR_FILTERS`와 이전 추천보다 우선하며, `캐주얼 정장 추천해줘` 다음의 `나 여자야`는
  `여성 캐주얼 정장`과 여성 정장 카테고리로 해석해 기존 남성 카테고리를 승계하지 않는다.
  부분 응답을 확정 맥락으로 오인하지 않도록 최근 대화와 상황 압축에는 `COMPLETED` 턴만 사용한다.
- **#664 — 추천 카드 그리드의 숫자·한글 수사 좌표가 전체 순번으로 오해되던 오담기를
  고쳤다** (api-spec §3.1, v0.33.4). 추천 상품 ID를 FE가 다시 보내지 않는 기존 위조 방지
  계약은 유지한다. 추천 패널이 보이는 턴의 LLM은 `screenReference={kind:"grid", row, column}`
  내부 JSON으로 행·열만 구조화하고 index·productId는 계산하지 않으며, 서버가 `pageType=chat`·
  `columns`와 이미 아는 이번 턴 추천 순서를 결합해 ID를 계산한다. 원문에 행 우선 증거가 없거나
  `ordinal_span == turn_count`가 성립하지 않고, 축·열 수·범위가 유효하지 않으면 함께 온
  `cart.productId`로 폴백하지 않고 안전하게 되묻는다. 외부 와이어 계약은 바뀌지 않는다.
- **#639 — 추천 카드에서 사용자가 상품명의 유일 토큰을 지목했는데 LLM이 같은 허용 목록 안의
  다른 상품을 골라 오담기하던 결함을 고쳤다.** 추천 카드 표면에 한해 상품명과 발화를 NFKC +
  casefold 기반 정확 토큰으로 비교하고, 숫자 전용·1글자·담기 명령·장바구니 문맥 토큰을 제외한
  뒤 유일 토큰들이 정확히 한 상품만 가리킬 때 그 ID로 교정한다. 공통 토큰, 부분 문자열, 서로
  다른 상품의 유일 토큰 동시 언급, 부정·대조 표현은 기존 LLM 경로에 양보한다. 계약 변경 없음.
- **#635 — 챗봇 장바구니 담기·삭제에 현재 `chatSessionId`를 전달하고, 추천 카드에서 해소한 담기에는 `recommendationContext{recommendationRequestId,listId}`를 함께 보낸다** (api-spec §4.1·§4.12, v0.33.1). Spring이 `chat:{sessionId}` sentinel로 행동 이벤트를 서버 측 적재하고 추천→담기 귀속을 검증할 수 있게 한다. 신원 0개/2개 `400 VALIDATION_ERROR`와 동시 경합 `409 RESOURCE_CONFLICT`도 계약 사본에 현행화했다.

### Removed
- **#635 — 구 `GET /profile/me` HTTP 조회 표면을 제거했다.** 라우터·응답 스키마·OpenAPI·회귀 테스트와 공개 문서를 함께 정리했으며, 프로필 요약 reader는 추천 경로 내부 소비로 유지한다.

### Changed
- **#650 — 판매자 general 레인에 경량 해석 허용 범위를 추가했다.** 단일 지표 증감·순위·
  임계값 비교(예: "지난주보다 늘었다", "가장 많이 이탈한 단계")까지는 general 이 직접
  답한다 — 원인 가설·복수 지표 교차·행동 추천은 여전히 금지이며, 필요하면 기존과 동일하게
  "보고서 페이지에서 확인" 안내로 돌린다. `GENERAL_PROMPT_TEMPLATE`(prompts.py)과
  `build_general_agent` 독스트링(workers.py)만 바꿨다 — 도구·스키마·SSE·API 계약 변경 없음.
- **구매자 rerank 근거 표시 기본을 C(`validated`)로 승격했다.** PR #638의 450-case live A/B/C
  결과에서 등록 detector 기준 unsupported reason은 A 10.87% → C 0%였고, A/B 추천 집합은
  비교 가능한 447/447에서 보존됐으며 B/C 순위도 450/450 동일했다. 운영 동등 추정 비용은 A보다
  14.05%, 평가 pipeline proxy 평균 지연은 11.23% 증가한다. Production graph만 Settings의
  `rerank_grounding_arm=validated`를 명시해 C를 쓰고, `RERANK_GROUNDING_ARM=current`로 A에
  즉시 롤백할 수 있다. 평가 CLI의 옵션 생략 기본은 비교 기준 A로 유지한다. 후보 ID·순위와
  와이어 계약은 바꾸지 않고, invalid metadata는 reason만 중립 템플릿으로 강등한다.
- **#581 — 취향 밴드(`priceBand`·`ratingBand`)에 한쪽 경계만 있는 표현을 담을 수 있게 하고,
  조회 응답의 라벨을 사람이 읽는 문장으로 바꿨다** (api-spec §3.8·§3.9.1, v0.33.0).
  종전 canonical 은 양쪽 경계를 강제해서 "5만원 이하"·"평점 4점 이상" 같은 취향을 담을
  그릇이 없었고, 추출 LLM 이 없는 쪽을 지어내 메웠다 — 실측에 하한을 지어낸 `0-100000` 과
  상한이 센티널 쓰레기인 `100000-999999999` 가 남아 있다. 형식을 **항상** 만족시키는
  지어내기라 드롭 지표에도 안 잡혔다(#462 "밴드 라벨 거부 0건"). 이제 `"-50000"`(이하만)·
  `"100000-"`(이상만)을 그대로 저장하고, 마이페이지에는 `"30,000원 이상, 50,000원 이하"` ·
  `"50,000원 이하"` 로 나간다(종전에는 원시 `"30000-50000"` 이 그대로 보였다).
  **저장 형식과 `nodeId`, I-33 수정 요청은 canonical 그대로다** — 표시 규칙이 `edgeId`
  파생에 영향을 주면 사용자가 지운 취향이 tombstone 을 비켜 되살아난다(REQ-PGRAPH-010).
  렌더는 투영 한 곳에서만 일어나고, 파서를 안 거친 저장 라벨(`"4.5-5"`)은 원문 폴백한다.
- **#582 — 구매자 장바구니 옵션 되물음의 실제 선택지를 번호 목록 + 굵은 라벨로 표시한다**
  (api-spec §3.1·§4.1, v0.32.18). #455 조건 좁힘·#454 색상 미충족·기본
  `CART_OPTION_REQUIRED`·`CART_OPTION_INVALID`·I-1 힌트 폴백의 다섯 출력에서 각 선택지가
  `N. **기존 표시 라벨**`로 나오며, 추가금 접미사도 굵은 범위 안에 남는다. 안내/마무리 줄,
  기존 정제와 원시 이름 의미, 빈 목록 강등, 힌트의 `외 N개` 배치는 바꾸지 않았다. 입력 해석,
  `screen.columns`, 추천 카드, FE, 판매자, HTML·링크·그 밖의 마크다운은 범위 밖이다.
- **#570 — 장바구니 옵션 되물음 나열을 `" / "` 이어붙이기에서 옵션 하나가 한 줄을 온전히
  차지하는 줄바꿈 나열로 바꿨다** (api-spec §3.1, v0.32.17). 되물음 문구는 #118·#455 이후
  "한 글자도 바꾸지 않는다"를 지켜 왔는데, 이번은 그 규약을 실측 근거로 의도적으로 푸는
  결정이다 — 로컬 카탈로그 21,373개 옵션명 중 11,480개(53.7%, 2026-08-10 실측)가 `/` 를
  포함해, 옵션 두 개(`블랙 / M`, `화이트 / M`)가 `블랙 / M / 화이트 / M` 으로 붙어 사용자에게
  네 개처럼 읽혔다. `_options_text`(`app/agents/buyer/cart/graph.py`)의 구분자를 `"\n"` 으로
  바꾸고, 옵션 줄에는 구두점을 붙이지 않는 조립 헬퍼 `_options_prompt` 를 신설해 #455 조건
  좁힘·#454 색상 미충족 고지·기본 되물음·`CART_OPTION_INVALID` 재질문 네 경로와 I-1 힌트 이름
  폴백까지 다섯 갈래를 전부 옮겼다. **하이픈(`- `) 불릿은 이번 PR 에서 붙이지 않는다** — FE
  마크다운 파서가 아직 배포 전이라 하이픈이 평문으로 그대로 노출되며, 파서 도착 후 별도
  한 줄 변경으로 미룬다. api-spec §3.1 `token` 에 FE 가 렌더링하는 4종 마크다운 문법(`\n`·
  `- `·`1. `·`**강조**`)과 안전성 근거(원시 HTML 미해석, 제3자 입력 미보장)를 계약으로 명시했고,
  구매자 LLM 이 쓰는 두 필드(`rerank.overallComment`·`decompose.reply`)의 시스템 프롬프트에
  마크다운 금지 규칙을 한 줄씩 추가했다(기존 `_strip_unsafe` 공백 접기가 개행·표·코드펜스를
  구조적으로 못 서게 하는 성질은 그대로 유지, 새 sanitizer 없음).
- **#434 — `conditions` 칩·`conditionActions` 를 멀티 값 축(`category`·`brand`) 값당 1개로
  분리하고, 값 지정 category 제거가 실제로 남은 카테고리 집합으로 재검색되게 했다**
  (api-spec §3.1, v0.32.14). 종전에는 멀티 카테고리/브랜드를 칩 1개에 조인 문자열로 뭉쳐 "그
  값만" 제거할 수 없었다. `build_condition_chips` 가 값마다 칩을 내고(`(field, value)` 기준
  순서 보존 dedup), `conditionActions[].value`(선택, 스칼라)를 신설해 그 값만 지목한 제거를
  지원한다 — **와이어 계약 추가 전용**이라 `value` 를 안 보내는 기존 FE 는 종전 동작과 100%
  동일하다. `value` 없음은 여전히 그 field 전체 제거(하위호환)이다.
  `brand` 는 실제 리스트라 값 지정 제거가 즉시 동작했지만, `category` 는 멀티턴 승계 상태
  (`ProductSearchFilters.category`)에 대표 1개만 남아 값 지정 제거가 대표값 일치 판정만 하는
  **관대 무시(no-op)** 였다 — 이슈 헤드라인인 카테고리 칩에서 기능의 절반이 비어 있었다.
  `ThreadFilterStore` 에 이 스레드가 실제로 검색한 카테고리 집합을 담는 키(`chip_categories`)
  를 신설해 매 추천 턴마다 무조건 덮어쓰고, 값 지정 category 제거 턴에는 그 집합에서 지목
  값만 뺀 나머지로 재검색·재승계한다(다음 일반 리파인 턴부터는 종전처럼 대표 1개 승계로
  되돌아간다 — 일반 승계 동작은 바뀌지 않았다). 저장 집합을 못 읽는 스레드(만료·구 스레드)는
  대표값 일치 판정으로 강등한다. 복원 턴이 `case==3` 우연 일치로 `split_by_need`(니즈별 목록
  분할)를 열지 않도록 `RouteDecision.category_legs_restored` 가드를 추가했다. 부수로 **`brand`
  칩 `value` 가 리스트→스칼라로 정정**됐다(단일 값이어도) — §3.1 예시가 원래 스칼라를 명시했던
  드리프트 해소로 FE 가시 변경이다. 호출부 로그(`condition_actions_applied`, #442)에
  `changed_fields`(None 이 안 되는 브랜드 부분 제거 포함)·`unmatched_values`(관대 무시 건수,
  값 자체는 미기록)를 추가하고 `no_op` 판정을 `changed_fields` 기준으로 바꿨다. 대표 카테고리가
  안 바뀌는 복원(A·B·C 중 비대표 값 지목 제거)은 `changed_fields` 만으로는 `no_op: true`로
  찍혀 무동작과 구분이 안 됐는데(#442 재발 형태), `category_legs_restored`(bool) 를 실제 복원
  결과 기준으로 추가하고 `no_op` 판정에도 반영했다.

### Fixed
- **#603 — 최초 상품 질의의 의미검색 쿼리에서 상품명은 빠지고 이미 구조화된 가격·색상만
  남을 수 있던 결함을 고쳤다.** 단일 category leg의 LLM `semanticQuery`를 파싱 직후
  결정론적으로 검증해, 구조화 필터와 정확히 일치하는 가격·색상·브랜드 표면만 제거하고 남은
  용도·상황 문맥과 상품 앵커를 보존한다. 구조화 표현만 남았다면 순수 상품명인 category query로
  폴백한다. 멀티 category, prior 정제발화 폴백, 구조화 필터 값과 검색 백엔드 계약은 바꾸지
  않았다. 계약 변경 없음.
- **#588 — 발화 없이 조건 칩만 제거한 턴이 LLM 재분해를 거치며 제거 조건을 되살릴 수 있던
  결함을 고쳤다.** `conditionActions` 를 prior에 적용한 뒤 액션-only 턴은 변경된 검색 경계로
  결정론적 추천 결정을 구성해 decompose와 카테고리 범위 분류를 건너뛴다. prior가 없는
  액션-only 요청은 임의 검색 상태를 만들지 않고 종료하며, 발화와 제거 액션이 함께 있는 혼합
  턴은 새 교체 의도를 해석할 수 있도록 기존 LLM 경로를 유지한다. 제거 관측 로그에는 원문이나
  값 없이 `action_only` 여부만 추가했다. 계약 변경 없음.
- **#583 — UTC 컨테이너에서 판매자 "어제 매출" 질의가 이틀 전 데이터를 반환하던 결함을
  고쳤다.** `report.generatedAt` 은 KST 로 내보내면서(#296) 기간 해석의 "오늘" 은
  `date.today()` = 컨테이너 로컬 TZ 였다. 운영 컨테이너가 UTC 라 00~09 KST 사이에는 기준일이
  KST 기준 하루 전이 되고, 거기서 "어제" 를 다시 빼 이틀 전 (from, to) 가 Spring 으로 나갔다
  (jarvis-back `BackendApplication` 이 JVM·DB 세션 TZ 를 `Asia/Seoul` 로 고정하므로 BE 는
  KST 로 해석한다 — AI 만 어긋나 있었다). 기준 시각을 `app/core/clock.py`(`KST`·`now_kst()`·
  `today_kst()`) 단일 출처로 모으고 `app/api/seller.py` 의 `date.today()` 3곳(general 레인
  기간 환산·general worker `today`·분석 파이프라인)과 `generatedAt` 을 그리로 옮겼다.
  KST 는 `ZoneInfo` 가 아닌 고정 오프셋(`+09:00`)으로 둔다 — 한국은 DST 가 없어 결과가 같고,
  OS tzdata 에 의존하지 않으며, `generatedAt` 직렬화 결과가 바이트 단위로 보존돼 FE
  (`AnalysisReport.tsx` `formatGeneratedAt` 이 오프셋을 정규식으로 잘라 쓴다)에 영향이 없다.
  재발 방지로 Dockerfile·docker-compose 에 `TZ=Asia/Seoul` 을 박고, 프로세스 TZ 가 KST 가
  아니면 기동 로그에 경고를 남긴다(`app/main.py` `_warn_if_timezone_mismatch` — 로컬 UTC·CI
  가 막히지 않도록 기동은 차단하지 않는다). 계약(와이어 포맷·필드·이벤트)은 변경 없다.
- **#463 — #430의 빈 `semanticQuery` 계약을 유지하면서 화면 지시어·카테고리 해제의 프롬프트 충돌을 전용 첫-턴 분류기로 분리했다.** 후보 프롬프트는 prior·직전 추천·screen 맥락 또는 저정보량 첫 턴에만 쓰고, 보조 smart 호출도 후자에만 연다. `true`일 때만 원문 fallback·빈 category legs·case 2를 복원하며, 실패/비정상 JSON은 **fail-open**(`None`, 원 decompose 결정 보존)이다. 채택 근거는 production-equivalent gate 재측정 4회뿐이다: v8 intent N=8의 `mainIntent` **237/240·236/240**, `screenExactPick` **31/32·32/32**, `categoryClear` **32/32·31/32**; underspecified v1 N=8의 `missRate` **0/112·0/112**, `falseAlarm` **0/104·0/104**, `unfilledCells=[]`, non-recommend 0, prompt `a853c1c4f2be`. legacy before는 hash `e20bf7aea508`/arm semantics가 불변이라 재사용했다. 이전 global-candidate after 산출물은 역사 보존만 하며 채택 비교에서 제외한다. 결과의 `baseline`은 항상-false comparator이므로 `axes.missRate`·`axes.falseAlarmRate`만 비교한다. classifier 실패 로그는 fail-open과 retry failure/`unfilledCells`를 구분한다.
- **#464 — decompose가 가격·평점 제약을 `attrConditions`에 잘못 실어 과소지정 되물음을 끄던 결함을 프롬프트 변경 없이 결정론 후처리로 제거했다.** 제약 축 어휘는 `config.py`에서 주입해 영문·camelCase·snake_case 변형도 함께 걸러낸다.
- **#571 — 추천 카드(CH-5)만 뜬 턴에는 화면 지시어 해소기가 아예 호출되지 않던 결함을 고쳤다**
  (api-spec §3.1, v0.32.16). `app/agents/buyer/graph.py` 의 게이트가
  `screen is not None and screen.products and screen_context_active` 였는데, 추천 카드는
  계약상 `screen.products` 에 실리지 않아(위조 경로 방지) "이거 담아줘"(후보 다건)·순번·
  목록 밖 id 같은 결정적으로 풀리는 입력이 전부 LLM 산출에 맡겨지고 있었다(실측 8/8·6/8
  오담기 — screen_reference.py 모듈 docstring 실패 로그 표 F-17). 서버는 이미 `last_reco` 로
  그 턴 카드 목록을 노출 순서대로 쥐고 있어, `screen.products` 가 없고 추천 카드
  (`last_reco[:turn_count]`)가 있는 턴에도 해소기를 돌리도록 게이트를 넓혔다. 순번 규칙은
  "표시 순서 = 저장 순서"가 증명될 때만(그 턴 push 가 목록 1개였을 때) 여는데,
  `RecommendationListEntry` 의 `ranked_ids` 는 BUY_ALL(세트 간 dedup)·다목록 PICK_ONE(전역
  순번 미정의)에서 배열과 어긋날 수 있어(F-18), `LastReco` 에 새 필드 `ordinal_span`(스레드
  상태값, config 아님)을 추가해 그 증명 여부를 실어 나른다 — 증명 실패 시 LLM 양보가 아니라
  강제 되물음이다(오담기가 되물음보다 비싸다는 이 모듈의 비대칭). 이름 지목도 추천 표면에서는
  배열이 곧 이름 출처라 결정적으로 확정할 수 있어, 후보 정확히 1건 + 부정 표지 없음일 때만
  코드가 확정하는 규칙(N)을 신설했다(F-19, 부정 판정은 `cart/negation.py::has_any_negation`
  재사용). `screen_reference_attempted` 도 추천 표면으로 넓혀, 추천 카드 참조 시도가 찜 해제
  규칙 3(목록 1건 자동 삭제)을 화면 표면과 동일하게 차단하게 했다(#440 F27·F29 와 같은 클래스의
  파괴적 동작 재발 방지). 화면 표면(`screen.products`) 동작은 한 줄도 바뀌지 않았다 — 화면이
  있으면 그 표면이 항상 추천 카드보다 우선한다.
- **#134 — Cloudflare 뒤에서 레이트 리밋 IP 백스톱이 근거 없는 홉 수를 신뢰하던 결함을,
  "배포된 상태가 스스로 진위를 증명"하는 구조로 고쳤다.** 이슈 본문의 전제(cloudflared
  터널 뒤 `127.0.0.1`)는 2026-08-10 인프라 실측으로 낡았다 — 터널은 이미 제거됐고 경로는
  Cloudflare 엣지 → ALB → AI EC2 하나뿐이며 오리진은 잠겨 있다(80/443 미개방, ALB 는
  Cloudflare IPv4 15개 대역에서만 수신). 대신 진짜 위험이 드러났다: `deploy.yml` 이
  `TRUST_FORWARDED_FOR`/`FORWARDED_FOR_TRUSTED_HOPS` 변수 참조(`${{ vars.… }}`)를 무조건
  주입하며 값은 조직(Organization) Variables 로 관리된다(커밋 `44d74cef` 기준 `true`/`2` —
  조직 변수는 권한상 이 작업에서 직접 확인하지 못했다). 그 근거("2026-08-06 실측")는 AI 가
  그때까지 XFF 를 로그로 남긴 적이 없어 **관측된 적 없는 값**이었다 — 틀렸다면 IP 백스톱이
  위조로 조용히 우회되는 상태였다. 신설 `app/core/client_ip.py` 가
  클라이언트 IP 판별 우선순위(`Cf-Connecting-IP`[홉 수 개념 없는 단일 값] →
  `X-Forwarded-For` 우측 신뢰 홉 → TCP peer)를 한 곳에 모으고, 레이트 리밋 대상 경로마다
  `client_ip_probe` 진단 로그 1건을 낸다(원문 IP 없이 `safe_fingerprint` 만, 기본 on) —
  핵심 필드 `cfMatchIndexFromRight`(CF 값과 일치하는 XFF 원소의 우측 1-based 위치)를 보면
  운영 로그 한 줄만으로 `FORWARDED_FOR_TRUSTED_HOPS` 가 맞는지 확인할 수 있고,
  `hopMismatch=true` 는 즉시 오설정 신호다. 프록시가 XFF 를 append 대신 헤더를 한 줄 더
  추가하는 경우(`headers.getlist`)도 도착 순서대로 결합해 처리한다. 새 설정 2개
  (`client_ip_probe_enabled` 기본 on, `trusted_client_ip_header` 기본 `cf-connecting-ip`)는
  둘 다 기본값이 곧 운영 동작이라 `deploy.yml` 배선이 필요 없다. 부수적으로, `deploy.yml`
  이 이 경로의 두 필드(`trust_forwarded_for`/`forwarded_for_trusted_hops`)를 무조건
  주입하는데 빈 문자열 관용 validator 가 없어 조직 Variable 미등록·삭제 시
  `ValidationError: bool_parsing` 으로 **전체 서비스 기동 크래시 루프**에 빠지는 잠재 사고를
  발견해 `_empty_trace_content_settings_use_default`(#326) 관례대로 같이 막았다(폴백은
  신뢰 off/hops=1 — 조용한 저하지만 서비스 정지보다는 낫다). 계약(api-spec) 불변 — 로그·설정
  전용 변경이다.

### Removed
- **#584 — 판매자 기간 확인 게이트(①.7)를 철거하고 관용 해석 + 응답 내 기간 고지로 일원화했다.**
  코드가 값을 보충한 기간 해석("이번 달"·"올해"·"최근 3개월")을 실행 전에 확인받던 왕복을
  없앴다 — `period_confirm.py` 모듈(pending 저장·TTL·IDOR 네임스페이스), 입구 선판정 ①.7,
  승인 판정 `parse_period_approval`(전 토큰 긍정 어휘 ~50종), `confirmation_text`,
  `PipelineResult(kind="period_confirmation")`·`.resolved`, `seller_period_confirm_ttl_minutes`
  를 모두 제거했다. 확인이 막던 "조용한 대체"는 이제 general 레인이 쓰던 것과 **같은**
  `period.disclosure_text` 고지가 막는다(분석 레인은 `_with_period_disclosure` 로 보고서 첫 줄에
  접두). `ResolvedPlan.needs_confirmation` 은 의미(코드가 값을 보충했는가)를 유지한 채
  `period_supplemented` 로 개명했고, `run_resolved_pipeline` 분리는 상주 파이프라인이 그 성질을
  쓰므로 유지한다(결정 109). 와이어 계약 불변 — 확인 턴은 애초에 `token`+`done(keep)` 이라
  api-spec·FE 무변경. 전용 테스트 15개 폐기(`test_seller_period_confirm.py`).
  명세는 `docs/specs/DESIGN-SELLER-PERIOD.md` v0.2.0 으로 개정했다.

### Docs
- **#154 — 최종 평가 보고서와 발표용 성능 구성을 재현 가능한 산출물로 고정했다.** 편집 가능한
  HTML·CSS와 15페이지 PDF, 주장별 근거 원장을 추가하고, 발표 평가 파트를 테스트셋 설계·추천
  품질/실험 의사결정·안전성/신뢰성·LLM 호출 시간/토큰/비용의 4페이지로 압축했다. 별도 Evidence
  요약 슬라이드는 제거하고 각 주장에 표본·CI·artifact·한계를 직접 붙였다. 병합된 PR #677의
  #631 결과는 원본의 탐색적·불확실 판정을 유지하며, arm별 usage 부재와 공식 Luna 단가 충돌이
  해소되기 전에는 해당 PR의 비용 추정치를 발표에 사용하지 않는다. 코드·API·SSE 계약 변경 없음.
- **#259 — decompose 라우팅 A/B/C 결정을 최신 실측으로 닫았다.** 최신 구매자 경로와 동일한
  `fast`·픽스처 v8·101셀×N=8에서 성공 표본 808개를 모두 채웠다: `mainIntent` 236/240,
  장바구니 144/144, 화면 해소 48/48인 반면 `general` 31/48, pending cart 전환 8/16,
  옵션 ID 28/32, 카테고리 혼합 교체 24/32, 찜 해제 24/32였다. 기존 #259 실험에서 `smart`는
  중앙값 +1.25초·매 턴 상위 티어 비용, 분리 1차안은 장바구니 88.9·90.3%와 전환 25·6.2%를
  보여 **현행 A를 유지**하고 B/C를 출고안에서 제외했다. 프로덕션 코드·프롬프트·설정 변경은
  없으며, greeting context gating·상품 전환·옵션 ID·카테고리 혼합·`찜닭` lexical boundary는
  표적 후속으로 분리했다. probe latency 꼬리는 페이서 포함 wall time이고 비용·token 212콜이
  unknown이라 E2E TTFT나 완전한 절대비용으로 인용하지 않는다.
- **#139 — 1차 완료(발표) 핵심 주장 4개와 claim-evidence matrix를 확정했다.** 발표(2026-08-14)가
  나흘 남은 시점에 `evals/` 17개 하네스에 이미 쌓인 baseline 을 엮어, 새 실행 없이 무엇을
  증명하는지 고정했다. **C1**(에이전트 경로가 no-op 대비 nDCG@10 유의 개선,
  paired bootstrap 95% CI 하한 +0.0632) · **C2**(컨텍스트가 있어도 의도 라우팅이 흔들리지
  않고 화면 밖 상품을 확정하지 않음, 출고판 `mainIntent` 0.979~0.983·`screenNoHallucination`
  1.0) · **C3**(개인화는 후보를 줄이지 않고 순서에만 반영, 하드 제약 위반 0 + #119 전후 라이브
  필터 유출 29/31건→1/31건) · **C4**(지연·비용 공개 가능, staging 실측 전까지 `pending(#152)`)
  4개를 채택하고, 개인화 품질 향상 주장과 파이프라인 vs 단일 LLM 우위 주장은 라이브/최신
  골든셋에서 `inconclusive`라 정직한 negative result 로 돌려 부록에 세웠다(필요 N 재산정
  ≈176 paired cases 포함). 판매자 품질은 전용 하네스 부재(`SELLER-FINAL-RISKS` V1
  "provider별 실 LLM 검증 0회")를 근거로 1차 주장에서 제외했다. `evals/README.md`(#328) 8항
  인용 규율(datasetHash 세대 혼동 금지·로컬↔운영 비혼동)을 재확인하고, `intent_probe`의
  출고판이 `adopted-*`이지 최신 timestamp인 `merged-*`가 아니라는 함정을 baseline 지정표로
  고정했다. release gate(G0~G4)·run manifest 필수 6항·발표 산출물 9종·P0 재검토(열린 post-mvp
  24건 전수 판정 — #152·#154·#139만 P0 유지)를 함께 정했다. §13 에는 과정 배포 자료(「LLM Agent 프로젝트
  가이드 v2」)의 평가 항목(기획·협업·기술난이도·완성도·발표전달력)을 이 문서의 claim·산출물에
  연결하는 대조표도 뒀다. 계약(api-spec) 변경 없음.
  (`docs/specs/RELEASE-CLAIMS-139.md` v1.0.0, `docs/specs/README.md` 색인)

### Security
- **#321 — "기억해" 원문의 하드 PII(전화번호·주민번호·카드번호·계좌번호·이메일·시크릿 토큰)가
  게이트 없이 저장되던 결함을 막았다.** 신설 `app/core/pii.py`(순수·동기·무 I/O, 예외를 던지지
  않는 결정론적 정규식 탐지기 — LLM 호출 추가 없음, 인라인 게이트가 첫 SSE 프레임 예산을 깎지
  않게)를 저장 경계마다 다르게 배선했다: **fact 저장**(`record_remember`·`ProfileStore.add_fact`,
  fact 뿐 아니라 개인화 그래프 triple 의 `label`/`anchorPhrase` 도 검사)과 **요약 저장**
  (`set_summary`, `_embed_summary` 가 외부 Google API 로 나가기 직전 마지막 관문)은 히트 시
  **전량 폐기**(SPEC-PROFILE-GRAPH-149 REQ-PGRAPH-071 — 파생 취향도 만들지 않는다), **세션 버퍼**
  (`append_session_ctx`)와 **LangSmith 콘텐츠 트레이스**(`record_request_content`·
  `record_llm_content`, `PII_REDACT_TRACE_CONTENT` 기본 on)는 **치환**(닫힌 어휘 placeholder —
  버퍼 치환은 델타 추출 LLM 이 원문 숫자를 애초에 못 보게 해 fact/label/anchorPhrase 로 옮겨
  적는 세탁 경로를 구조적으로 닫는다). `record_remember` 는 **절단 전 원문**을 검사한다 —
  절단이 먼저면 `010-1234-` 처럼 번호가 잘려 정규식이 못 잡는다. 탐지는
  `app/core/text.py::_security_skeleton` 위에서 해 zero-width 문자를 숫자 사이에 끼우는 우회를
  막는다. 로그에는 이벤트명 + `safe_fingerprint(user_id)` 만 남기고 히트 클래스·매치 문자열·
  카운트는 남기지 않는다(REQ-PGRAPH-075/076). `tracing.py` 의 기존 카나리아 검증기·면제 목록·
  `_DELTA_SYSTEM` 계열 프롬프트는 변경하지 않았다.
- **#321 — 대화 전사록(`conversation_turns`)에 처음으로 시간 기반 보존 정책을 도입했다**
  (`SPEC-PROFILE-001` OPEN-P5 해소). 이 리포에 시간 기반 삭제 스윕이 없었다 —
  `graph_audit_retention_days`(기본 90일)도 만료 행을 지우는 스윕이 없다. 신설
  `conversation_retention_days`(기본 **90일**, `graph_audit_retention_days` 와 의도적으로
  짝지음 — 감사 원장이 지문만 남기므로 원문 대조 상대는 전사록뿐이라, 전사록이 감사 원장보다
  먼저 지워지면 그 사이 구간이 조사 불가능해진다)를 기동 시점 fail-fast 검증기로 강제한다.
  삭제는 `app/pipelines/scheduler.py` 의 별도 job(`conversation_retention_sweep`, 기본 1시간
  주기, `CONVERSATION_RETENTION_SWEEP_ENABLED` 기본 on)이 유계 배치로 수행 — `ORDER BY
  created_at LIMIT` + `FOR UPDATE SKIP LOCKED`(동시 `finalize_assistant` UPDATE 를 건너뜀) +
  배치당 짧은 트랜잭션 1개(장수 트랜잭션의 autovacuum 봉쇄 방지). PENDING 턴도 지운다 —
  세션 lifecycle sweep 의 "진행 중 턴 보호" 규칙과 달리, 90일 된 PENDING 은 죽은 스트림이라
  예외를 두면 TTL 이 지우려던 것이 정확히 그만큼 남는다. `conversation_turns (created_at)`
  인덱스를 신설(`PgConversationStore.setup()` 멱등 마이그레이션 + `db/profile/init/`)해 스윕
  조회가 풀스캔이 되지 않게 했다. `ConversationStoreProtocol` 에 `purge_expired_turns` 를
  추가해 인메모리 구현도 같은 계약을 따른다(스윕 job 이 isinstance 분기 없이 양쪽을 다룬다).
  와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 은 불변 — `turns_for()`/`get_turn()` 의
  프로덕션 호출부가 없어 전사록은 감사·상관관계 조회 전용이다(`docs/api-spec.md` §3.9.4 C-23·
  §3.9.4 OPEN-P5 서술 사본 동기화, `SPEC-PROFILE-001`·`SPEC-PROFILE-GRAPH-149` OPEN 항목 갱신).

### Fixed
- **#443 — 정본 카탈로그 사전으로 빈 `categoryQueries` leg를 결정론 보강** — 모델이 상품군을
  말한 첫 추천 턴에서 leg를 비우던 결함을 `seller_categories.json` 스냅샷의 최장 일치로 보완한다.
  N=24 독립 2런에서 `namedCategoryHasLeg` 98.6%·100.0%(문턱 83.7%),
  `conditionOnlyNoCategoryQuery` 90.0%·92.5%(문턱 84.2%), condition_only 주입 0건이라 기본 on.
- **#553 — 운영 배포 전면 중단 복구: `deploy.yml` 설명문의 빈 Actions 표현식** — `script: |` 은 YAML 블록 스칼라라 `#` 가 주석이 아니라 리터럴이고 Actions 가 그 안의 표현식도 평가한다. #539 가 넣은 설명문의 **내용이 빈 표현식**이 문법 오류를 내 워크플로가 job 을 시작조차 못 했고(startup failure, run 2건 job 0개), 승격 #552 의 41커밋이 실서버에 반영되지 못했다. 설명문에서 표현식 리터럴을 걷어내고, 같은 블록에 "여기서는 표현식을 쓰지 않는다"는 경고를 남겼다. 로컬 YAML 파싱은 통과하므로 CI 로는 못 잡는 계열이라 `docs/lessons.md` 에 진단 단서(트리거 밖 브랜치에서도 run 생성 = startup failure)까지 기록했다.

### Fixed
- **#454 — 장바구니 옵션 되물음 좁히기에 승인된 색상 동의어 사전을 연결해 "검정 셔츠"처럼
  고유어로 말한 색상 조건이 옵션명("블랙")과 표기가 달라 못 좁혀지던 문제를 줄였다.** 카탈로그
  실측(옵션 보유 상품 4,439쌍, `scripts/measure_option_color_miss_454.py` 실행 재현)에서 고유어
  발화의 좁힘률이 **2.7% → 54.9%(+52.2%p)**, 외래어 발화는 51.1% → 54.9%(+3.8%p) — 두 표기가
  사전으로 같은 것이 되며 수렴한다. 등가 판정은 누적 조건 매칭(R2/`by_condition`)에만 적용하고
  이번 발화 자동 선택(R1/`by_message`, #114·#455)은 리터럴 일치만 본다(#455 리뷰 F-1 비대칭
  유지). 좁혀지지 않은 45.1% 중 옵션명에 색상 축이 실재하는 3.3%는 "그 색은 없다/품절이다"라고
  단정하지 않고 "찾지 못했어요"로만 안내한다 —
  판매자가 승인 사전 밖 표기(영문·약어·미승인 색명)를 썼을 수 있어 단정할 근거가 없다. 신규
  설정 `cart_option_color_synonym_enabled`(기본 `True`) — 사전 적재 실패·미설정은 예외 없이
  오늘 동작으로 degrade한다. **[#508 흡수, 2026-08-10]** BE 가 옵션별 재고를 2026-08-09
  구매자 쪽 전량 배포했다(`product.stock_quantity` → `product_stock(product_id, option_id,
  quantity)`, 02 D33) — I-1·I-3 `options`/`optionCount` 는 품절 옵션 제외·"구매 가능한 것"
  기준으로, I-2 는 전 옵션 품절 시 `CART_STOCK_INSUFFICIENT`(`availableStock: 0`)로,
  I-18 은 `maxQuantity`(옵션 재고 기준) 신설로 반영했다(api-spec §4.1·§4.6·§4.9·§4.17,
  `<!-- VERSION_TBD -->`). **2026-08-10 운영 실측으로 확인** — `product_stock` 24,390행(품절
  3,170), I-1 `optionCount` 동일 상품 161→138(23개 품절 제외). **다만 #508 이 이 색상 표기
  이형 문제를 대신 풀어주지 않는다** —
  옵션이 사이즈인 상품은 색상이 상품 속성에만 있어 재고 필터가 색상별로 못 거르므로(BE 명시
  한계), 이 PR 의 색상 동의어 좁히기는 #508 이후에도 그대로 필요하다. 상세 실측·경계는
  `docs/specs/MEASURE-OPTION-COLOR-454.md`.
- **#454 Phase 2 — #508(옵션별 재고) 이후에도 남는 "옵션에 그 색이 없다" 문제를 검색
  사후필터로 줄였다.** BE 의 색상 매칭(`attributes.색상` 축)과 품절 제외(`options` 축)가 서로
  몰라서, 속성엔 그 색이 있어도 보이는 옵션엔 그 색이 없는 후보가 그대로 반환됐다(운영 실측
  `color=블랙`: 옵션 있는 144건 중 77건이 옵션 목록에 블랙 계열 0개, 그중 52건은 다색이라
  판정 확실). `app.services.search_service._filter_unbuyable_color_options` 가 판정식(색상
  조건 있음 ∧ `attributes.색상` 복수 ∧ `optionCount==len(options)`(절단 아님) ∧ 승인 동의어
  확장 어디에도 그 색이 옵션명에 없음)을 모두 만족하는 후보만 뺀다 — D 판정은
  `app.agents.buyer.cart.options.narrow_options`(#454 되물음 좁히기와 같은 함수, 재구현
  아님)를 그대로 호출한다. `evals/option_color` 하네스(신규, before/after 같은 패스로 산출)
  실측: `unbuyable_rate` 11.0%(2,152/19,536, 옵션 있는 후보 대비) → 필터 적용 시 정의상 0%,
  `candidates_per_query` 중앙값 2,579.0 → 2,486.5(**−3.6%**, recall 손실이 크지 않음), 0건
  가드 발동 0/20색(전량 카탈로그 스케일에서는 안 뜬다). 하네스 판정과 실제 구현이 19,536건
  전건 일치(교차검증 0건 불일치). 신규 설정 `search_color_option_postfilter_enabled`(기본
  `True`) — 사전 적재 실패·색상 조건 없음·제외 후 0건이면 예외 없이 무필터로 degrade한다.
  §2-B 되물음 고지 문구는 바꾸지 않는다(단정 금지 근거가 재고와 무관해 그대로 유효). api-spec
  §4.6 `[].options` 소비를 검색 사후필터까지 확대(`<!-- VERSION_TBD -->`). 상세는
  `docs/specs/MEASURE-OPTION-COLOR-454.md` §7.
- **#466 — 브랜드-only 발화에서 `filters.brand` 가 비던 결함을 고쳤다** (#430 후속, 과소지정
  오탐의 근원). `decompose` 프롬프트에는 색상 전용 규칙만 있고 **브랜드 추출 규칙이 아예
  없었다.** `- recommend:` 불릿에 브랜드 절 하나를 넣어 ① 추출 ② **발화 표기 그대로**(번안 금지)
  ③ 총칭어("제품"·"가전") 분리 ④ 오추출 금지를 함께 지시한다. 실 LLM 전/후 각 2런
  (`evals/filter_axes/brand_probe.py`, gpt-5-nano=배포 fast 티어, 브랜드-only 20발화 × n=3 =
  60표본): 추출 `17·19/60 → 45·42/60`, 원문 표기 유지 `13·11/60 → 45·42/60`, 비브랜드 발화
  오추출은 `0/12` 유지. 번안 결함(애플 발화에서 추출된 8표본이 전부 `["Apple"]`,
  "크록스"→`["Crocs"]`, "삼성"→`["Samsung"]`)은 0 이 됐다 — `brandName` 은 exact IN
  (api-spec §4.6)이라 번안값은 조용히 빗나가는데, 브랜드는 **자동** 완화 허용 목록에 없어
  (`ratingMin` 뿐) 스스로 복구되지 않고 사용자가 완화 칩을 눌러야 한다.
- **#466 — 브랜드 법인 표기를 I-1 와이어에서만 넓혀 카탈로그 도달률을 올렸다.**
  운영 시드 실측(brand 2,368행 × product 6,559건)에서 "삼성"은 78건 중 **7건(9.0%)**, "LG"는
  38건 중 **1건(2.6%)** 에만 닿았다 — 부족분의 정체가 `삼성전자`(71건)·`LG전자`(37건)다.
  `app.pipelines.brand_aliases` 가 **닫힌 법인격 접미사 화이트리스트**로 표기 후보를
  **가산적으로** 덧붙인다. 카탈로그 사본을 두지 않는다 — §4.6 이 미존재 이름을 무시하므로
  규칙만으로 충분하다. 2,368행 전수 대조에서 이 규칙이 잇는 쌍은 3건뿐이고 전부 같은 회사라
  교차 오염이 0건이다(`삼성도어`·`삼성메디칼`은 구조적으로 배제). `filters.brand` 는 그대로라
  조건칩 표시와 `search_filter_axes` 축 집합이 불변이다. `BRAND_ALIAS_EXPANSION_ENABLED`
  기본 on(하방이 유계) · `BRAND_ALIAS_MAX_VALUES` 로 개수 상한.

### Added
- **#361 — 그래프 데이터가 검색 필터에 닿지 못하게 구조로 막았다** (INV-PGRAPH-ORDER,
  SPEC-PROFILE-GRAPH-149 §6.12). 취향 그래프는 **rerank 순서 지시**와 **홈 프로필 벡터**에만
  쓰이고 후보를 좁히는 데는 쓰이지 않는다. 순서로 틀리면 3위가 1위로 갈 뿐 상품은 화면에 있지만,
  필터로 틀리면 상품이 애초에 검색 결과에 없어 하류가 복구할 수 없다 — #119 에서 회원 추천이
  게스트보다 나빠진(nDCG@10 −0.288) 메커니즘이다. **런타임 변경 0줄** — 오늘 유출 생산자가 없으므로
  산출물은 "생기는 순간 깨지는" 테스트다. api-spec §3.8 v0.32.0 이 `usagePolicy.filterSafe` 를
  폐기하며 집행을 계약 문장 + 코드 구조로 옮긴 뒤(SPEC v0.3.5, #360) 그 "코드 구조"가 이것이다.
  - **정적**(REQ-PGRAPH-110) — 그래프 모듈 19개(glob 이라 새 모듈 자동 포함)가 필터 타입을 import
    하지 않고, 필터 모양 타입을 정의하지 않고(필드명이 검색 축과 2개 이상 겹치면 적발 — 이름이
    아니라 구조로 판정해 다른 이름의 같은 물건도 잡는다), 공개 함수 87개가 반환형을 밝힌다.
    와이어 `GraphEdgeView` 5필드에 필터 축이 없는지도 잠근다.
  - **행동**(REQ-PGRAPH-112·113·115·116) — 모든 `NodeType`×`Predicate` 를 채운 그래프와 그 파생
    요약을 **함께** 심고(그래프만 심으면 추천 경로에 소비자가 없어 `0 == 0` 을 잰다) 회원·게스트의
    decompose 프롬프트·검색 페이로드·스레드 필터 저장소가 같은지, `avoids` 가 후보를 줄이지 않는지
    본다. 픽스처 값은 fake 카탈로그와 **아프게** 교차시켰다 — 선호 가격대가 카탈로그 위에 있어야
    유출 시 후보가 준다.
  - **유효성 대조군** — `profile_injection_scope="both"` 로 #119 이전 배선을 되살리면 프롬프트가
    실제로 갈라진다. 이게 없으면 위 초록불이 전부 공허할 수 있다. 실제로 회원에게만 필터를 심는
    변이를 넣어 행동 테스트 5건이 전부 실패하는 것을 확인했고, 그 과정에서 fake 검색이 필터를
    무시해 후보 비교가 무력했던 사실이 드러나 필터를 적용하도록 고쳤다.
- **#360 — 개인화 그래프 API 표면 5종(I-32~I-37)을 붙였다** (api-spec §3.8·§3.9, v0.32.7~v0.32.8).
  마이페이지 **"AI가 이해한 내 취향"** 화면이 이제 실제로 동작한다 — 취향을 항목 단위로 조회하고
  고치고 지우고 통째로 초기화하고 개인화를 끌 수 있다. 저장 계층(#356·#358) 위에 얹는 마지막 층이다.
  - **결정론적 투영** — 저장된 구조화 트리플의 파생이며 **요청 경로 LLM 0회**다. 정렬은 `predicate`
    고정 순서 → 최근 확인 시각 → `edgeId` 3키 전순서이고 `graph_merge` 의 정렬 키를 **재사용**한다
    (두 곳에 적으면 저장 순서와 화면 순서가 갈린다). 투영은 문서를 **다시 정렬한다** — 사용자 편집은
    새 edge 를 리스트 뒤에 덧붙이고 끝나 편집 직후 문서는 정렬이 깨져 있다.
  - **서버 화면 상한이 없다** — 자르면 상한 밖 항목을 사용자가 보지도 지우지도 못해 취향 관리
    화면의 목적과 정면으로 부딪힌다. 페이지네이션도 없다.
  - **오류 매핑을 한 곳에 모았다**(`_GRAPH_ERROR_MAP`) — 라우터는 도메인 예외만 던진다. `409` 의
    기본 코드가 `STREAM_IN_PROGRESS` 라 코드 지정을 한 번만 빠뜨려도 FE 에 "스트림 진행 중"이 나가고
    그 결함은 정상 경로 테스트로 안 잡힌다. `error_envelope` 에 `error.detail` 을 열어
    `PROFILE_VERSION_CONFLICT` 가 최신 `graphVersion` 을 동봉한다.
  - **응답 예산 실측 등재** — 조회 2s·변경 3s(§2.9 (c)). 실측 p95 는 예산의 1.5~4.3% 다.
- **#360 — I-33 재전송이 최초 응답을 그대로 돌려준다** (REQ-PGRAPH-043). 수정 성공 뒤 그 edge 를
  삭제하고 원래 `If-Match` 로 온 네트워크 재시도가 원장 TTL 안에 도착하면 **현재 문서로는 응답을
  재구성할 수 없다.** 원장이 투영된 항목을 들게 했다 — #358 이 테이블을 3개로 나눈 근거가 바로
  "원장이 드는 응답 본문에 라벨이 섞인다"였다.

- **#359 — 사용자가 고친 취향을 기계 배치가 덮지 못하게 하고, 개인화 중지를 실제로 집행한다**
  (`SPEC-PROFILE-GRAPH-149` §6.4·§6.6, v0.3.3 / api-spec §3.7 v0.32.10 — **와이어 계약 불변,
  Spring·FE 무변경**). #356 이 병합 엔진을, #358 이 저장 안전장치와 중지 플래그 테이블을
  깔았는데 **사용자 의사를 존중하는 로직이 하나도 안 들어가 있었다** — 고친 취향은 다음 배치가
  되돌렸고, `get_personalization_flag` 의 프로덕션 호출자는 0건이라 스위치를 꺼도 아무 일도
  일어나지 않았다. §6.4 의 제목이 「기능이 연극이 되지 않게 하는 조항」인 이유다.
  - **pin 불변**(REQ-PGRAPH-031 [HARD]·035) — 실제 위반 경로 3곳을 닫았다. `_merge_edge` 가
    관측 한 건에 최상급 확신도를 감쇠 EMA 로 덮던 것, `_carried_tombstones` 가 이월 pin 을
    감쇠시키던 것, `_resolve_conflicts` 승자 키에 origin 클래스가 없어 **방금 관측된 기계
    `avoids` 가 오래된 사용자 pin 을 `superseded` 로 강등**하던 것. 그 위에 터미널 게이트
    `_reassert_pins` 를 얹되, **정상 경로에서는 아무것도 바꾸지 않는다**를 불변식으로 세워
    게이트가 국소 수정을 가리지 않게 했다. AC-PGRAPH-09(배치 재실행 후 사용자 수정 유지)를
    재는 테스트가 리포에 하나도 없어 함께 신설했다.
  - **`challenged` 신호**(REQ-PGRAPH-033, `graph_pin_challenge_count`=3) — pin 에 반대 관측이
    임계 이상 쌓이면 표시하되 **상태는 바꾸지 않는다**. 값 계산·저장까지가 이 이슈이고 와이어
    노출은 #360. 카운터는 **이번 배치에 실제 반대 관측이 있을 때만** 오른다 — 진 edge 가 근거
    0건이어도 영구 이월되므로 승패만 보면 배치 횟수를 세게 되고, 60초 sweep 기준 3분 침묵으로
    깃발이 켜진다. `challenge_count` 는 지문에서 빼고 파생 `challenged` 만 넣어 `If-Match` 가
    상시 무효가 되는 것을 막았다. 설정값 `0` 은 신호를 끈다(순진한 `>=` 비교는 정반대로 동작).
  - **절단 상한을 바구니별로**(REQ-PGRAPH-005 개정) — pin 무제한 / `active` /
    `superseded` / tombstone 목록. 단일 상한에서는 근거 0건으로도 영구 이월되는 `superseded` 가
    단조 누적되며 `active` 보다 먼저 보존돼, **`active` 가 하나도 안 남는** 되먹임이 있었다
    (밀려난 active 는 투영에 없어 지울 수도 없는데 요약·추천에는 계속 반영된다). active 상한을
    fact 상한과 같게 두면 그 절단은 **구조적으로 발동 불가**가 된다. 이슈 #150 코멘트
    (2026-08-09)의 결정을 #358 이후 상황으로 재조정한 것이며, `superseded` 의 실효 예산은
    `상한 − |pin|` → 자기 상한 전량으로 **늘어난다**.
  - **중지 = 사용·수집 동시 정지**(REQ-PGRAPH-051/052/053) — 소비 3표면(rerank 주입·홈 프로필
    벡터·마이페이지)과 수집 3지점("기억해" hot-path·세션 버퍼·델타/consolidation). 구매자 턴은
    플래그를 **턴당 1회** 조회해 요약 조회와 `asyncio.gather` 로 병렬 처리하므로 왕복이 늘지
    않는다. 중지 회원의 rerank 인자가 **게스트와 동일**함을 `buyer/graph.py` 를 실제로 통과하는
    CI 게이트로 고정했다. 수집을 멈춰도 finalizer 의 버퍼 정리·처리 완료 표시는 계속되며
    (`finalizer.py` **무변경**), 그 덕에 중지 기간 발화의 소급 반영 금지(REQ-PGRAPH-056)가 별도
    방어 없이 성립한다.
  - **중지 중 I-22 는 `NO_PROFILE`** — 시그널이 있어도 그렇다. api-spec v0.22.0 이 적은 근거
    ("프로필 벡터 항을 빼면 근거가 남지 않는다")가 성립하지 않아 **판정 기준보다 앞서는 단락**
    으로 규정을 정정했다(결론 불변). 중지는 장애가 아니라 정상 동작이므로 `profile_unavailable`
    degrade 어휘를 붙이지 않는다 — 붙이면 REQ-PGRAPH-054("중지 여부가 실패로 추론되지 않는다")가
    관측 계층에서 깨진다.
  - **중지 기간 감쇠 정지**(REQ-PGRAPH-055) — 배치를 멈추는 것만으로는 부족하다. `_confidence` 는
    `decay_evaluated_at` 을 읽지 않고 관측 시각부터 매 배치 새로 계산하므로 6개월 중지 후 재개
    시 6개월치가 그대로 걸린다. 중지 구간을 `profile_personalization_state.disabled_spans` 에
    쌓고(그래프 문서에 두면 `graphVersion`·감사·원장이 거짓이 되고 락 규약이 깨진다) 병합 엔진이
    **겹친 만큼만** 차감한다 — 누적 스칼라는 관측보다 앞선 중지까지 깎아 준다.
  - **실패 정책은 경로별로 갈린다** — hot-path 쓰기·소비는 fail-closed, 배치 델타 추출은
    fail-unknown(`None` = degrade·버퍼 보존), consolidation 은 fail-open. 전 구간 fail-closed 로
    통일하면 DB 블립 한 번에 개인화가 **켜져 있는** 사용자의 누적 세션 버퍼가 영구 삭제된다.
  - 부수: `graph_journal` pg 풀을 lifespan 에서 워밍한다(종료 목록에만 있었다) — 중지 게이트가
    첫 호출자를 백그라운드 sweep 에서 **구매자 턴**으로 바꿔, 지연 초기화(연결 5s + 마이그레이션
    30s, `_init_lock` 직렬화)를 first-token 10s 관문 안에서 물게 되기 때문이다. 워밍 실패는
    기동을 막지 않는다. `store.set_summary` 가 `existing` 을 무조건 읽게 고쳐 임베딩 성공 경로에서
    `usable` 이 리셋되던 구멍도 닫았다.
  - **비범위**: API 엔드포인트(I-32~I-37)와 `challenged` 와이어 노출은 **#360**, 소비 측 필터
    격리(INV-PGRAPH-ORDER)는 **#361**. 경계는 [#360 코멘트](https://github.com/toss-delta-final/jarvis-ai/issues/360#issuecomment-5234964138)에
    정리했다 — 병합 엔진(`graph_merge`·`config` 그래프 축)은 #359 소유이고 #360 은 #359 머지 후
    rebase 한다.
  - **미해결로 남긴 것**: 추천에 반영되는 범위가 화면 범위보다 넓다 — 요약 입력이 `promoted` 를
    보지 않아 **강등된 취향(약 40일 침묵)이 화면에서 사라진 뒤에도 추천을 움직인다.** 사용자가
    볼 수도 지울 수도 없다. 투영 경계 건이라 #360 에서 다룬다(같은 코멘트 5번 항목).
- **#140 — 추천 실행 provenance 를 구조화 로그 `recommend_provenance` 로 남긴다** (api-spec
  §6.3 (d) 신설). `recommendationRequestId` 는 I-21 와이어에 이미 있었지만(§4.2) 발급 3곳
  (`graph.py` 메인/프로필 경로, `home_recommendation.py`) 어디서도 로그되지 않아 BE
  `behavior_events.recommendation_request_id` 와 이을 AI 쪽 상관 기록이 0건이었다. 추천이
  실제로 도달했을 때만(push 성공/홈 응답 반환) 목록 순서·`algorithmVersion`·`rankSource`
  (닫힌 어휘 `rerank`/`search_order`/`repurchase_pin`/`expose_min_fill`/`profile_vector` —
  rerank 가 수치 score 를 내지 않아 "무엇이 순위를 정했는지"로 번역)를 한 줄에 남긴다.
  수치 score 가 없는 결정론 정책이라 IPS 용 `propensity` 상수를 심는 대신 `deterministic`
  플래그로 정직하게 표시한다(근거는 `app/core/reco_provenance.py` docstring). 모델·버전은
  로그 전용이며 홈 표면은 `rankerModel` 이 항상 `null`(§3.7 [HARD] 준수, 응답·SSE 불변).
  튜너블 `RECO_ALGORITHM_VERSION`·`RERANK_PROMPT_VERSION`·`RECO_PROVENANCE_MAX_ITEMS`
  신설(config 주입, 초과분은 silent cap 없이 `itemsTruncated=true`).
- **#466 — 브랜드 추출 축 프로브를 세웠다** (`evals/filter_axes/brand_probe.py` +
  `brand_cases.json`, 수동 도구·CI 제외). 기존 축을 먼저 확인한 결과 재고 있는 것이 없었다 —
  `evals/filter_axes` 의 `brand` 축은 goldenset dev 109건 중 라벨이 **1건뿐**이고,
  `combo_matrix` 는 관측 전용, `underspecified_probe` 는 브랜드 앵커 2건으로 하류 판정만 잰다.
  축 4종(`present`/`verbatim`/`expected`/`spurious`)을 분자·분모와 함께 낸다(규약8).

### Docs
- **#395 — I-1 응답 비대화 협의 3건을 종결하고 `docs/api-spec.md` §4.6 정본 사본에 반영했다**
  (api-spec §4.6, v0.32.7). BE 협의는 이미 끝나고 배포도 완료됐다 — 이번 변경은 그 결론을
  사본·코드 주석·테스트에 뒤늦게 동기화하는 것으로, **와이어 필드·엔드포인트·오류 코드는
  전혀 바뀌지 않는다**. ① `size` 상한 재도입 요청 = **폐지 확정**(재개 계획 없음, `totalCount`
  동반 요청도 함께 폐기). ② `attributes` 내부 미사용 4키(`_extra`·`_source_pid`·`_domain`·
  `_category`) 제외 = BE 수용·배포 완료를 2026-08-10 운영 실측으로 확인 — 항목당 평균
  **1,835 B → 1,105.7 B**, 무필터 전량 6,128건 **6.13 MiB**, 소요시간 중앙값 **2.09 s**(콜드
  첫 호출은 3.20 s로 예산을 스칠 수 있음), 협의 착수 시점(2026-08-06) **7.74 s·12.3 MB** 대비
  큰 폭 개선. ③ `rating`/`reviewCount` 비정규화 = **채택하지 않음** — BE가 캐싱안을 철회하고
  커버링 인덱스(jarvis-backend PR #133)로 대체해 응답표 변경 없음. 코드 동작 변경은 없다(4키는
  원래 `app/` 어디서도 읽지 않는다) — `SpringProduct.attributes`·`ProductSearchFilters`·
  `_search_query_params`·추천 그래프 `estCount` 주석의 폐기된 전제만 정리했고, 회귀 테스트
  1건(`test_live_deployed_item_shape_2026_08_10_parses_and_consumes`)으로 운영 응답 형태를
  고정했다. 재현 스크립트 `scripts/measure_i1_live_395.py` 신설.

### Changed
- **#361 — 개인화 평가 dev-v2 baseline 을 현행 골든셋으로 재생성하고, 수치 자체를 회귀 게이트로
  세웠다** (REQ-PGRAPH-114). 커밋된 baseline 이 **다른 케이스 집합을 설명하고 있었다** —
  `datasetHash` 가 `d16eb0e9…`(dev 96건)인데 현행은 `675520d9…`(v2.3.0, dev 109건)다. 직전 판은
  #333 작업 도중의 더러운 워킹트리에서 생성돼(`dirty: true`, 히스토리에 없는 `commitSha`) 그 PR
  최종 dev 집합(103건)조차 반영하지 못했고 이후 #474 가 6건을 더했다. arm 별 nDCG@10 이 전부
  움직였다(clean 0.734220 → 0.686380, 주 비교 meanDelta 0.304398 → 0.258142). **분모가 바뀐
  것이지 품질 저하가 아니다** — 서로 다른 케이스 집합의 nDCG 는 비교 대상이 아니다.
  기존 eval 게이트가 **verdict 문자열만** 비교해 이 드리프트를 며칠간 놓쳤으므로, 신설
  `test_default_weight_ndcg_matches_committed_baseline` 이 `caseCount` 를 먼저 보고(분모가
  다르면 수치 일치는 우연이다) arm 별 `ndcgAtK` 전 k 와 헤드라인 meanDelta 를 `rel=1e-6` 으로
  잠근다. 앞으로 골든셋·픽스처·스코어링을 바꿔 Tier D 수치를 움직이는 PR 은 baseline 재생성을
  요구받는다 — 그 비용 대신 커밋된 수치가 무엇을 설명하는지가 항상 참이 된다.
- **#505 — 색상 동의어 정본을 2차 사람 검수해 부분 일치로 닿지 않는 독립 색명만 확대했다.**
  `카멜→브라운`·`버건디→와인`처럼 자명한 표기 상이 40건을 승인하고, 실제 동의어 묶음이 있는
  독립 어휘 앵커 4개를
  추가했다. 밝기·채도 수식어, 복합색, 데님 밝기 축은 확장 때 원래 조건을 잃으므로 보류했으며,
  코드·마케팅명 등 40건은 반려로 고정했다. 생성 JSON·SQL은 검수 오버레이에서 재파생한다.

### Added
- **#443/#465 — `evals/intent_probe` 에 `named_category` 6앵커·`namedCategoryHasLeg` 축 신설** —
  상품군을 **명시한** 첫 턴("과일 추천해줘" 등)에서 `categoryQueries` leg 이 비는 결함을 재는
  축이다. `conditionOnlyNoCategoryQuery`(#465, 조건 전용 턴은 leg 이 0개여야 정답)의 반대
  방향 축이라 같은 필드의 양쪽 끝을 한 런에서 함께 읽는다 — 한쪽만 보고 채택 판정을 내리지
  않기 위함.
- **#358 — 개인화 그래프의 사용자 변경 경로에 저장 안전장치를 깔았다** (SPEC-PROFILE-GRAPH-149
  §5.4·§6.5·§7.1·§7.2). #356 이 배치 쓰기까지 만들었다면 이번은 **사용자가 직접 고치고 지우는**
  경로를 안전하게 만든다. `store.set_graph` 가 CAS 없는 blind overwrite 라 그 위에 네 층을 얹었다.
  - **`revision` compare-and-set** — `If-Match` 불일치는 `409` + 최신 `graphVersion` 병기이고
    **부분 적용이 없다**(REQ-PGRAPH-040/041). 문서가 손상돼 `get_graph` 가 `None` 을 돌려줘도
    revision 이 0 으로 되돌아가지 않는다 — **감사 테이블의 `graph_version_after` 최댓값**을
    하한으로 쓴다(REQ-PGRAPH-042). 같은 구멍이 배치 경로(`consolidate`)에도 있어 함께 막았다.
  - **멱등 원장** — 파생 키 `profile-graph-{action}:{userId}:{scopeId}:{ifMatch}` 로 재전송을
    판정해 최초 응답을 재생한다(REQ-PGRAPH-043). 파생 키에 본문이 없어 생기는 구멍(같은
    `If-Match`·다른 본문이 남의 응답을 재생)은 본문 지문으로 막고 충돌로 떨어뜨린다.
  - **저널·크래시 복구** — `processing`/`completed` + claim/lease(기존 `processed_events` 패턴).
    `404`·`409`·no-op 은 claim 을 되돌려 감사 행을 남기지 않는다(REQ-PGRAPH-080).
    문서를 쓰기 **직전에 의도를 기록**하고(§7.2 저널 선행 기록), **부재 판정은 원장과 함께** 한다 —
    문서 쓰기 뒤 완료 표시 전에 끊긴 창에서 재시도가 `404`/`409` 를 받으면 api-spec §3.9.2 가 ⚠️ 로
    금지한 "edge 존재 여부로 뭉뚱그리기"가 된다. 재개는 남은 단계만 마저 하고 최초 응답을 재생하며,
    감사는 파생 키 지문 UNIQUE 로 멱등이라 두 행이 되지 않는다.
  - **변경 감사** — `actor_fp`·`object_fp` 는 peppered HMAC 이고 **raw userId·라벨 원문을 어떤
    컬럼에도 넣지 않는다**(REQ-PGRAPH-081 [HARD]). 전체 초기화도 이 테이블은 보존한다(-062).
  - **테이블은 3개다** — SPEC §7.1 은 "감사 겸 저널 + 중지 플래그" 2개라고 적었지만, 원장이 드는
    응답 본문에 라벨 원문이 섞여(api-spec §3.9.1 `edge.to`) 감사와 한 행에 둘 수 없다. §7.1 문구는
    개정 대상이다.
- **#358 — 개별 삭제를 즉시 물리 삭제로 바꾸고 라벨 없는 tombstone 을 남긴다** (#499 확정 전제).
  구 표현은 지운 취향을 `status="suppressed"` edge 로 문서에 그대로 뒀고 그 `node_id` 가
  `"brand:소니"` 라 **사용자가 지웠다고 믿는 문장의 원문이 남아 있었다**. `GraphDocument.tombstones`
  가 `edge_id`(내용 파생 해시)만 들어 라벨 없이 재파생을 막는다. 구 문서는 읽는 즉시 흡수하며
  (별도 백필 없음) 참조 끊긴 노드까지 떨군다. 연쇄로 `_summary_input` 이 삭제 판정을 잃어
  지운 취향이 요약으로 되돌아오던 것도 함께 고쳤다(REQ-PGRAPH-023).
- **#358 — 전체 초기화가 대화 전사록까지 지운다** (api-spec §3.9.4, REQ-PGRAPH-061).
  `idx_conversation_turns_user` 를 신설했다 — 없으면 그 삭제가 풀스캔이다(SPEC §12-7).
  감사 로그와 개인화 중지 상태는 보존한다(-062/-063).
- **#358 — 개인화 중지 플래그** (`profile_personalization_state`, REQ-PGRAPH-050). 전용 저장
  위치여야 하는 이유가 여기서 실현된다 — 요약 항목에 두면 초기화가 지워 중지가 조용히 풀린다.
  소비·수집 차단(집행)은 #359 몫이고 본 변경은 플래그와 요약 사용 표식까지다.
  - **미집행**: 감사·원장 보존 기간(`graph_audit_retention_days`·`graph_idempotency_ttl_h`)은
    값만 배선했고 **만료 행을 지우는 스윕 잡은 만들지 않았다** — 기본값(90일·24시간)은 🔴 C-23
    미합의라 잠정이다.
  - **비범위**: API 엔드포인트(I-32~I-37)와 `app/core/errors.py` 상태 코드 매핑은 #360.

- **#518 — 리뷰 분석에 기간별 추이(`bucket`)와 긍부정 감성 집계 추가** (api-spec 계약 무변경).
  `get_reviews(stats=True, bucket="daily|weekly|monthly")` 가 기간을 구간으로 나눠 I-31 집계를
  한 번의 도구 호출로 모아 온다 — 워커가 구간마다 호출하면 추이 하나가 도구 호출 한도(8)를
  넘긴다. 실패한 구간은 `조회 실패` 로 남기고 **0건으로 뭉개지 않는다**(못 본 구간을 급락으로
  서술하는 것을 막는다). 집계 출력에는 긍정(4-5점)·중립(3점)·부정(1-2점) 건수와 **비율**을
  함께 싣는다 — 비율을 워커가 암산하면 verifier F2(근거 대조)가 근거 없는 수치로 강등한다.
  리뷰 워커 프롬프트는 5단계로 늘려 만족 요인(`rating="4,5"`)까지 읽는다.
- **#385 — 구제 체인 JSONL 재실행 집계기와 실측 불가 판정 근거**. `recommend_zero_result`·
  `recommend_pipeline` 합집합의 first-token 기여분과 0건 종결 진입 하한, `UPSTREAM_TIMEOUT` 상한을
  Markdown·CSV로 남긴다.

### Fixed
- **#360 — 개인화를 끄면 최대 24시간 다시 켤 수 없던 문제** (api-spec §3.9.5). I-37 토글의 파생 키가
  대상 상태를 담지 않았는데, 이 경로는 그래프 문서를 안 바꿔 `graphVersion` 이 고정이라 **끄기와
  켜기가 정상적으로 같은 선행조건을 지참한다** — 두 요청이 같은 키가 되어 켜기가 끄기 응답을
  재생했다. 프라이버시 스위치라 영향이 크다. 파생 키 scope 에 대상 상태를 싣는다.
- **#360 — I-33 부분 변경이 `500` 을 내던 문제** (api-spec §3.9.1). `predicate`·`object` 중 하나만
  보내는 것은 계약이 허용하는데 조립부가 `ValueError` 를 올렸고, 그것이 저장소 장애 판정에 안 걸려
  그대로 전파됐다. 생략한 쪽을 **잠금 아래에서 읽은 문서**로 채운다 — 미리 읽어 채우면 재전송이
  이미 사라진 edge 를 찾다 실패해 §7.2 의 "재전송 판정이 `404` 보다 앞"이 깨진다.
- **#360 — 전체 초기화 응답이 계약과 한 키도 안 겹치던 문제** (api-spec §3.9.4). `{facts, summary,
  buffers, conversationTurns}` → **`{edges, transcriptTurns}`**. `edges` 는 사용자가 I-32 에서 보던
  개수로 센다 — 술어를 `is_projected` 한 곳에 두어 화면 문구("취향 12건")와 초기화 응답이 갈리지
  않게 했다.
- **#360 — 구매 이력 파생 수정이 거부되지 않던 문제** — `GraphEdgeNotEditable` 이 정의만 되고 아무도
  던지지 않았다(#358 이 "판정은 #360 소유"로 남긴 자리). 두 `409` 가 겹치면 **재조회로 결과가 바뀌지
  않는 쪽**을 먼저 알린다 — 반대로 하면 FE 가 규약대로 재조회 후 재시도하고 그 재시도가 결국 같은
  코드를 받아 왕복이 낭비된다.
- **#440 후속 — 찜 해제 오분류의 역방향(장바구니 삭제 의도 증발)을 정정했다** (계약 무변경).
  `"찜닭 빼줘"`류(음식명 + 장바구니 삭제 의도)를 decompose 가 `wishlist_remove` 로 오분류하면,
  근거 게이트가 찜 삭제는 막았지만 사용자가 실제로 요청한 장바구니 삭제는 아무도 수행하지 않아
  조용히 증발했다(이슈 제목 "엉뚱한 걸 지우거나"의 잔여물). `classify_cart_utterance == "cart_remove"`
  이고, 발화의 `wishlist_target_markers` 가 부분 문자열로만 있고 그중 어느 것도 어절 경계를
  통과한 head 가 아닐 때만(LLM 이 `찜닭`·`갈비찜` 의 `"찜"` 에 속았다는 서명) `stream_cart_remove`
  로 정정한다 — `"이어폰 빼줘"`(찜 문맥일 수 있고 `"찜"` 자체가 없다)까지 정정하면 규칙 1(이름
  매칭) 정상 경로가 죽으므로 이 조건을 대조군으로 고정했다. 아울러 `evals/intent_probe/fixtures`
  의 `wishlist-remove-001~004` note 에 있던 "프롬프트 사다리 보강은 #443/#465 소유" 서술을
  정정했다 — #443·#465 는 `categoryQueries` 이슈이고 찜 사다리와 무관하며, 사다리 1-1)은 #440
  등록 이전(#116/#117)에 이미 있었다.
- **#518 — 내용 없는 리뷰 한 행이 리뷰 조회 전체를 죽이던 문제** (api-spec §4.20, 계약 무변경).
  DDL 이 `content TEXT NULL` 이라 별점만 남긴 리뷰가 실재하는데 `SellerReviewRow.content` 가
  `str = ""` 였다. 기본값은 키 **결측**만 흡수하고 명시적 `null` 은 거부하므로, rows 한 행만
  content 가 null 이어도 ValidationError → SpringUnavailableError 로 그 페이지 전체가 degrade
  됐다 — 판매자에게는 "리뷰 조회에 실패했습니다" 로만 보였다. `content`·`authorNickname` 을
  nullable 로 열고 표시 폴백(`(내용 없음)`·`익명`)을 도구에 둔다. `extra="allow"` 는 여분 필드만
  다루지 이 경로를 구제하지 못한다(#489 와 층위가 다르다).
- **#385 — 구제 체인 4개 구조화 이벤트가 평문 logging sink에서 계측 필드를 잃던 문제**. JSON message와
  기존 `extra`를 함께 기록해 `aggregate_rescue_chain.py`가 운영 stdout 줄을 그대로 파싱하면서도 기존
  `LogRecord` 속성 기반 검증을 보존한다. 전역 formatter는 `chat_request` 이중 인코딩과 카테고리 문자열
  노출 위험 때문에 바꾸지 않았다.
- **#484 — Tier L 이 케이스별 프로필을 받는다. 그전까지는 dev 전 케이스에 고정 프로필 하나를
  먹이고 있었다** (평가 하네스 한정, api-spec 무개정). Tier D 는 케이스별 구조화 선호를
  파생하지만 Tier L 은 서빙과 같은 마크다운을 소비하는데 그 변환기가 없어, `profiles.json` 의
  "Sony 이어폰 / 3~5만원" 한 개가 라면·립스틱 질의에도 그대로 들어갔다. 즉 `live-v1` 의
  `clean_rerank_only ΔnDCG@10 = −0.056445` 는 개인화의 손해가 아니라 **무관한 프로필의 손해**를
  잰 값이다. 구조화 선호 → §5.1 3섹션 마크다운 결정론 렌더러(`render_profile_markdown`,
  강/중/약을 자연어로만 노출·상한은 생성측 압축)를 신설하고 `clean_rerank_only`·`clean_both`
  를 갈아끼웠다. 옛 방식은 `clean_fixed` arm(옵트인, 기본 arm 목록 밖)으로 남겨 같은 실행 안에서
  대조할 수 있게 했고, 선호가 비는 35/109건을 가르는 `profile_signal` 슬라이스와
  `run_manifest.json` 의 `profileMarkdownRenderVersion` 을 함께 실었다. **실측은 병합 후 별도
  live 실행이 필요하다** — 이 변경만으로는 새 수치가 나오지 않는다.
- **#506 — 이미지 기반 상품 등록 초안** (api-spec §3.2, v0.31.0 — 추가 전용, 기존 op 와이어
  불변). 판매자가 채팅에 상품 사진을 첨부하면(`imageUrls`, 새로 첨부한 턴에만) vision 이 1회
  분석해 등록 초안(`draft{op:"create"}`)을 만들고, FE 등록 미리보기 카드용 **`preview{}`**(11키
  고정·null 계약·서버 포맷 완료·`sections` source/warning/note)를 함께 싣는다. 카테고리는 BE
  조회 없이 **로컬 스냅샷**(`app/data/seller_categories.json`, 파일 교체=배포)이 후보 검색·
  `categoryPath` 변환·검증의 단일 원천이고, LLM 은 주입된 후보 id 중에서만 고른다(계약값은
  코드). 초안 대기 중 발화는 입구 게이트가 분류한다 — 수정→새 draftId 발급+**이전 draft
  무효화**(옛 카드 confirm 차단), 승인 텍스트→버튼 안내(발화≠동의 유지), "취소"→폐기(LLM 0회
  단축경로), 딴 주제→차단 안내(초안 유지). create 의 `image_url` 금지를 해제하고 confirm 실행이
  I-10 에 `image_url`·카테고리 쓰기 값(`seller_category_write_mode`, 기본 leaf — **BE 정렬 1건
  잔여**)을 전달한다. 신규 모듈 `vision/category_catalog/preview/draft_session`, 수신 검증
  (canonical URL ≤500자·presigned 거부)은 요청 스키마+hitl 이중 방어.
- **#505(#461 승계) — 정본 I-1 3갈래 판정 ②(상품 `attributes` 에 색상 축이 없으면 통과)와
  부분일치 판정의 주체가 Spring BE 라는 사실을 회귀로 고정했다**. AI 사후필터는 색상을 판정하지
  않고, 확장 on/off 모두 `color` 를 Spring payload 축으로 유지하며 배열 원소를 변형하지 않는다.
  승인 0건 가드는 PR #502 가 이미 넣은 구현이므로 그 정본을 유지한다 — 같은 판정을 중복 구현하지
  않도록 이 브랜치의 중복분은 back-merge에서 제거했다.

### Changed
- **#306 — 미룬 턴만 I-1 검색 재시도를 끄던 #277 응급 처치를 제거했다. 이제 턴 유형과 무관하게
  `SPRING_MAX_RETRIES` 하나가 재시도를 정한다** (api-spec §2.9(c)·§3.1, v0.32.5).
  #277 의 스킵은 `conditions` 를 검색 뒤로 미룬 턴의 첫 SSE 가 검색 뒤에 있어 재시도가
  first-token 10s 를 넘기던 시절의 것인데(실측 이벤트 0건·504 가 8/8), #396 이 `progress` 를
  decompose 앞으로 보내며 그 전제가 사라졌다. #394 가 재시도를 0으로 내린 동안은 무동작이었고
  #406 이 1로 원복하며 다시 유효한 가드가 되어, 이번에 제거 여부를 판단했다. 제거 대상은
  `SEARCH_RETRY_ON_DEFERRED_CONDITIONS`(config)·`suppress_search_retry`(ContextVar+CM)·
  미룬 턴 판정 셋이다. **동작 변화**: 미룬 턴 본검색·자동완화 probe 가 실제로 재시도하고,
  #406 의 `retrying` progress 도 그 턴에서 나간다(그 턴에선 `conditions` 앞). 직렬 이론 상한은
  12s → **18s**(`3 × 3.0 × 2`)지만 `RESCUE_BUDGET_MODE=narrow`(#406 기본)의 런타임 좁히기가
  미룬 턴 본검색을 `(30−15−경과)/3 ≈ 4.8s` 로 묶어 **#277 이 재현한 「1차 3.0s 타임아웃 + 2차
  2.9s 성공」조합은 성립하지 않는다** — 되살아나는 것은 2차가 빠르게 응답하는 경우뿐이고,
  대가는 확정 실패 감지가 3.0s→≈4.8s 로 늦어지는 것이다. 실측(`evals/first_event_budget`,
  변경 전/후 2벌)으로 첫 이벤트가 여전히 `progress`(수 ms)임을 함께 고정했다.
  **롤백 규약이 하나 늘었다** — `RESCUE_BUDGET_MODE=observe` 로 되돌릴 때는
  `SPRING_MAX_RETRIES=0` 을 함께 지정해야 기동한다(18.0 ≥ 30−15). `PROGRESS_EVENTS_ENABLED=false`
  는 #406 이 만든 같은 짝 규칙을 그대로 따른다. 곁들여 #406 이 기본값을 0→1 로 올리며 갱신하지
  않은 사본 drift 2건(api-spec §2.9(c) 의 `9s` 수치, `.env.example` 의 `SPRING_MAX_RETRIES=0`·
  `RESCUE_BUDGET_MODE=observe`)도 함께 정정했다. **와이어 계약 불변.**
- **#394 원복 — I-1 `spring_max_retries` 기본값을 1로 복구하고 `rescue_budget_mode`를
  `narrow`로 함께 올렸다.** 사람의 명시 지시로 수행했으며, #394가 제시한 원복 조건인 BE #395
  검색 쿼리 개선은 충족됐다: BE PR #133 커버링 인덱스와 `attributes` 4키 축소가 배포됐고,
  2026-08-09 라이브 응답에서 4키 부재 및 항목당 약 1,780B→1,052B로 확인됐다(`size` 상한 폐지).
  `narrow`는 꼬리 예약 예산이 부족한 구제 단의
  타임아웃을 좁혀도 시도하며 건너뛰지 않고, 다시 끄려면 `SPRING_MAX_RETRIES=0`을 설정한다.
- **#483 — Tier L 의 주 비교를 `회원 vs 게스트` 에서 `회원 vs 프로필 없는 회원` 으로 바꿨다**
  (평가 하네스 한정, 프로덕션 코드·api-spec 무개정). 기준선 `guest` 는 비교 arm 과 프로필만
  다른 게 아니라 **identity 까지 달라**(persona_id 가 없어 I-19 구매이력 조회·재구매 dedup 이
  통째로 빠진다) 헤드라인이 "프로필 효과 + identity 효과"의 합이었다 — `live-v1` 산출물을
  라벨로 가르면 guest 라벨 +0.0135 / member 라벨 −0.1258 이고 하락은 `repurchase` 3건이
  만든 것이라, 그 3건을 빼면 전체 평균이 −0.0340 → −0.0106 으로 0 에 수렴한다. Tier D 가 쓰던
  `member_no_profile` arm(identity=member, 프로필 없음)을 Tier L 에 추가해 주 비교를
  `clean_rerank_only vs member_no_profile`(`pairedVsMemberNoProfile`)로 옮기고, `pairedVsGuest`
  는 cold-start 보조 비교로 남겼다(identity 가 섞이므로 프로필 효과로 해석하지 않는다 —
  dev-v2 README 와 같은 규약). `rankingChange`·`axisLeakage` 도 같은 주 기준선을 따라가며,
  `axisLeakage["guest"]` 는 자기 비교(항상 0)에서 **지터 바닥**으로 바뀌어 유출이 신호인지
  잡음인지 가르는 기준이 된다. `--arms` 검증은 위치 규칙(`arms[0]=="guest"`)에서 두 기준선
  포함 여부로 바뀌었고, `comparison.json` 에 `secondaryBaselineArm`·`primaryComparison`·
  `axisLeakageUnmeasured` 가 추가됐다. 마지막 것은 기준선 짝이 없어 **유출을 재지 못한** 행을
  따로 싣는다 — `[]`(유출 없음)와 `None`(측정 못 함)이 같은 목록에서 똑같이 빠지면 예산 소진이
  안전 신호로 둔갑한다. 예산 상한은 그대로 두고 실행 시 `MODEL_EVAL_MAX_CALLS_PER_RUN=4000` override 를
  쓴다(4-arm × dev109 × repeats3 = 3,924호출, 비용 상한 $20 은 무관). **실측은 병합 후 별도
  live 실행이 필요하다.**
- **#504 — 판매자 분석 차트 재설계: 좌표 생성 주체를 LLM → 코드로 전환** (api-spec §3.2,
  v0.30.0 · `docs/specs/DESIGN-SELLER-CHART-V2.md`). 구 구조는 `graph_agent`(도구 없음,
  결정 D-4)가 워커 요약에서 숫자를 베껴 좌표를 만들고 G1 이 근거 없는 수치를 드랍해
  `charts` 가 상시 비었다 — 실데이터 좌표를 만들 경로가 구조적으로 없었다. 이제 LLM 은
  축 선언(`ChartPlanSet`)까지만 하고, 신설 `app/agents/seller/charts.py` 가 14조합 소스
  레지스트리로 Spring(I-6·I-13·I-9·I-31)을 직접 조회해 좌표를 조립한다(빈 날짜 y:0 채움·
  기간별 버킷 ≤60점·상품축 상위 15 절단·x 유일성 보장, nullable 수치는 0 으로 뭉개지 않고
  no_data). 와이어는 추가 전용 — `chartPeriod`(차트 전용 기간, 다를 때만)·
  `chartUnavailable[]`(사유 5종, message 는 서버 완성 문장)·`charts[].aggregate`·
  `unit: RATING`. chart_only 턴은 레인 신설 없이 `title="판매 분석 그래프"` 로 구분.
  구 G1(`verifier.run_chart_checks`)과 결정 D-4 는 폐기.

### Docs
- **#360 — 계약 문서의 빈칸 5건과 SPEC 드리프트 5건을 정리했다** (api-spec v0.32.7·v0.32.8,
  `SPEC-PROFILE-GRAPH-149` v0.3.3). 와이어 계약(엔드포인트·필드·오류 코드)은 **불변**이며 이미
  있는 것들 사이의 적용 조건·우선순위를 명시한 것이다 — 두 `409` 의 우선순위 · `type`+`label`
  정규화 실패 `400` · 부분 변경 기본값의 출처 · §3.8 투영 대상 · 중지 중 `markdown` 유지.
  특히 **REQ-PGRAPH-021 의 `active` + `promoted` 가 정본과 어긋난 드리프트**였다 — 노션 정본 I-32
  는 `active` 만이고 *"요약 생성이 같은 규칙을 쓴다"* 로 화면=추천을 이미 요구한다. 그 조건을
  그대로 구현했다면 **화면에 안 보이는데 추천에는 쓰이는 취향**이 생겨 사용자가 `edgeId` 를 몰라
  지울 수단을 잃었다. `promoted` 는 병합 엔진 내부 히스테리시스이고 필터 소비처가 0건이다(실측).
- **#518 — api-spec §3.2 `findings[].analysisType` 표에 `"review"` 등재** (v0.32.5, 사본
  드리프트 정정). v0.25.0(#297)이 리뷰 워커를 6종째로 붙일 때 이 열거 표만 5종에 멈춰 있었다
  — 코드·실 와이어는 처음부터 6종이라 신설 협의가 아니라 문서 정정이다.
- **#357 — 개인화 그래프 협의 항목 표기를 정리해 BE 협의 트랙을 닫았다** (api-spec §3.8·§5,
  v0.32.2 / `SPEC-PROFILE-GRAPH-149` v0.3.1). `C-20`(4)·`C-21`(3)·`C-22`(1)(2)·`C-28` 을 🔴 → 🟢 로
  내렸다. **계약 내용은 한 글자도 바뀌지 않았고 상태 표기만 바뀌었다** — 네 항목 모두 **답이 이미
  나와 있었는데 반영이 안 돼 있던 것**이다.
  - `C-20`(4) 게스트 미노출 → 정본 `M-11` 이 `인증: 필요`·`401 AUTH_REQUIRED`·`403 AUTH_FORBIDDEN`
    (`USER` 전용)이라 비로그인은 `401` 에서 끊긴다. `C-21`(3) FE 의 `ETag` 헤더 비의존 → 정본
    `M-12`·`M-13` 이 *"응답은 본문이 정규, 헤더는 CORS 로 브라우저 JS 가 못 읽는다"* 로 규약화했다.
  - `C-22`(1) `409` 수용 동의 → **정본 등재가 곧 동의의 기록**이다. `C-22`(2) 게이트웨이 `409`
    재작성 → **운영이 이미 답한다** — `401 TOKEN_EXPIRED`·`400 CART_STOCK_INSUFFICIENT`+`detail`·
    **`409 STREAM_IN_PROGRESS`** 가 지금도 이 경로를 통과 중이다.
  - `C-28` 브랜드 통제 어휘 → BE 정리 완료 + 2026-08-08 덤프 실측으로 **BE 요청 0건** 확인.
  - **왜 남아 있었나** — 협의 문서가 *"계약 문서라 사용자 승인 후 별건으로"* 적용을 미뤘는데
    **그 "별건"을 추적할 자리가 없었다.** #499 도 `C-28` 을 범위 밖에 두어 한 칸 더 갔다.
  - 남은 미합의는 **`C-24`(민감 카테고리 목록 소유 — 기획·법무 트랙) 하나뿐**이다.
- **#499 — 개인화 그래프 계약(§3.8·§3.9)을 노션 정본 10벌에 전수 동기화하고 🔴 초안 → 확정으로
  올렸다** (api-spec §2.5·§3.8·§3.9·§4.11·§5·§6.3, v0.32.0 / `SPEC-PROFILE-GRAPH-149` v0.3.0 /
  `SPEC-PROFILE-001` v0.9.0). 두 사건이 겹쳤다 — **2026-08-08 BE 프록시 구현**(`jarvis-backend#132`)이
  사본과 어긋나는 사실 4건을 실측으로 확정했고, **2026-08-09 정본 10벌**(`I-32`·`I-33`·`I-34`·`I-36`·
  `I-37` + `M-11`·`M-12`·`M-13`·`M-15`·`M-16`)이 BE·FE 협의 완료로 확정되면서 응답 구조까지 개편됐다.
  이슈 본문(4건)보다 실제 범위가 넓었고, 정본 각 페이지가 자기 변경 이력으로 이 이슈를 지목한다.
  - **되돌리기(I-35·`M-14`) 폐기 반영** — 개별 삭제는 **즉시 물리 삭제**이며 undo 창도 `suppressed`
    중간 상태도 없다. ⚠️ **재파생 차단 표식(tombstone)은 함께 폐기되지 않았다** — undo 와 독립이며
    없애면 60초 flush 가 방금 지운 취향을 되살려 삭제 기능이 이름만 남는다. 확인창은 FE 필수로
    전환(구 계약은 "확인 없이 즉시+undo"). 재전송은 **파생 키 멱등이라 원문이 없어도 `200 replayed`**
    이고 새로 조회 후 재삭제만 `404` 다 — 둘을 "이미 삭제된 edge 는 404"로 뭉치면 멱등 규약과 모순이다.
  - **§3.8 응답 구조 개편** — `nodes[]` 폐지·대상을 `edges[].object` 로 인라인, edge 필드 13→5개,
    최상위 카운트·`usagePolicy`·`disabledAt` 제거, `markdown` 편입, 화면 상한 폐지, 정렬 키 비노출.
    단일 근거는 신설 [HARD] **「응답 필드 기준 — FE 가 그리거나 되돌려 보낼 값만 싣는다」** 이며
    **삭제가 아니라 경계 이동**이다(전부 저장 모델 잔존, 필요해지면 추가 전용으로 되돌린다).
  - **오류 계약 3건 신설** — §2.5 에 **봉투가 유일 형식(평문 미채택)** · **성공은 bare object** ·
    **레인 (b) 프록시 코드 변환표**(`BAD_REQUEST`→`VALIDATION_ERROR` 등, `PROFILE_*` 보존,
    `error.fields` 부재). 첫 항목은 실측 대응이다 — BE 가 *"계약이 봉투 유무를 명시하지 않았다"* 고
    판단해 양쪽을 파싱하는 방어 코드를 넣었다. 함께 §3.9 [HARD] "변형 없이"의 대상을 `PROFILE_*`
    코드와 `error.detail` 로 정밀화했다(구 문구는 문자 그대로면 BE 구현이 계약 위반이었다).
  - **C-27 🔴 차단 → 🟢 해소, 무효화 4지점** — 우리 사본은 중지 1곳만 적었고 협의 문서는 "2곳"으로
    축소 제안까지 했는데 **실제 구현이 둘 다보다 넓다.** §4.11 캐시 키도 `p5:home:{memberId}` 단독으로
    정정했다. **C-20 (1)(2)(3)·C-21 (1)(2)·C-26 해소**, `If-Match` 선-400 등재, 공통 실패표를 절별
    적용 범위로 전환, 감사 `action` 5→4종(`edgeRestore` 제거·`edgeSuppress`→`edgeDelete`).
  - **코드 변경 0건** — 그래프 튜너블·라우트가 `app/` 에 아직 없다(구현은 #150).

### Fixed
- **#323 잔여 — 요약 쓰기가 배치와 사용자 편집 사이에서 덮이던 것을 compare-and-set 으로 닫았다**
  (#358 작업 범위 5). PR #387 이 잠금까지 넣었지만, `consolidate` 는 그래프 락을 놓고 LLM
  왕복(수 초)을 한 뒤 요약을 쓰므로 **그 창의 사용자 편집을 시간상 겹치지 않은 채** 덮었다 —
  잠금으로는 안 닫히는 갭이다(SPEC §7.4 "남은 부분"). LLM 호출 전에 읽어 둔 `seq` 를 지참해
  그 사이 바뀌었으면 물러난다. 락 키를 합치는 대안은 `record_remember` hot-path 를 초 단위로
  막아 채택하지 않았다.
- **유닛 TCP 격리 가드가 Windows 에서 스위트를 통째로 죽이던 회귀** (#474 발, #358 작업 중 발견).
  Windows 에는 AF_UNIX socketpair 가 없어 CPython 이 `socket.socketpair()` 를 127.0.0.1
  `connect()` 로 흉내내는데, asyncio 이벤트 루프가 self-pipe 를 그것으로 만든다 — AF_INET
  `connect` 를 통째로 막는 가드가 **루프 생성 자체를 실패시켜 async 테스트가 전멸**했다(실측
  556 failed). 리눅스는 커널 AF_UNIX 라 이 경로가 없어 CI(ubuntu-latest)로는 영영 안 잡힌다.
  socketpair 구간만 스레드 로컬로 예외 처리했다(가드의 본래 목적은 그대로).
- **#512 — 매출 시계열 도구가 오류 없이 내던 "정상값처럼 보이는 틀린 답" 3종 차단** (와이어 계약
  불변 — AI 소비측 게이트). (1) `granularity=summary` 는 응답 shape 이 달라 `SalesResult`
  (`extra="allow"`)가 `series=[]` 로 삼켜 **언제나 "총매출 0원"** 이 나가던 것을 도구 입구에서
  `Error:` 로 거절한다(summary shape 수신은 §4.4 I-6 개정 후 별도 주제). (2) 파싱은 되지만
  정규형이 아닌 ISO(`"20260801"`·`"2026W311"`)가 window 필터의 경계값이 돼 전 포인트를 탈락시켜
  또 0원을 만들던 경로를, 공유 기간 가드(`_period_arg_error`)에서 **정규형 일치**로 막는다
  (`@_guard_period_args` 를 쓰는 조회 도구 9종 공통, `to_date` 는 종전에 본문 검증 자체가 없었다).
  (3) 표본 3개 미만이라 검정 불능인 경우를 "이상 감지 없음"으로 단언하던 것을,
  `detect_seasonal_anomalies` 가 `SeasonalAnomalyDetection(decided, sample_size, ...)` 을 돌려
  **판정 보류**로 갈라 표기한다(워커 프롬프트의 "판정 보류 ≠ 이상 없음" 규칙 정합, Tukey 경로와
  같은 어휘). `n>=3` 이고 이상 0건인 경우의 문구·`salesCount` 표기(#489)는 불변.

- **#511 — 상품 "삭제"를 여전히 숨김(`HIDDEN`)으로 다루던 문제** (api-spec §3.2·§4.5·§4.8, v0.29.7).
  BE 가 2026-08-05 에 `ProductStatus.DELETED` 를 신설해(02 D41 · 정본 Notion I-12) 숨김과 삭제를
  갈랐는데, 사본은 v0.29.4(#472)가 §4.5 표만 맞추고 §3.2 HITL 서술을 놓쳤으며 **코드는 전혀
  손대지 않아 `DELETED` 가 `app/` 에 0회 등장**했다. 세 갈래로 새고 있었다. ① I-12 409
  `ALREADY_DELETED`·I-11 409 `PRODUCT_DELETED` 가 `SpringUnavailableError` 로 뭉개져 **"안 되는
  일"이 "일시 장애, 재시도해 주세요"로** 안내됐다 — HITL 은 재confirm 이 가능해 판매자가 무한
  재시도에 갇힌다(같은 논리로 I-30 에는 이미 전용 예외가 있었다). ② HITL 삭제 초안이 `status:
  ON_SALE→HIDDEN` 으로 고정돼 diff 카드·결과 문구가 삭제를 **"숨김"이라 안내** — 되돌릴 수 있는
  조작으로 오인한 채 승인하게 된다. ③ 그 고정값 때문에 **이미 숨겨둔 상품의 삭제가 stale 로
  차단**됐다(`before="ON_SALE"` vs 실제 `HIDDEN`) — BE 가 D41 로 열어준 "숨겼다가 나중에 지운다"
  흐름이 AI 쪽에서 다시 막히던, 구 `ALREADY_HIDDEN` 과 같은 증상. 전용 예외 2종(`ProductAlreadyDeleted`·
  `ProductDeletedNotEditable`, `SpringUnavailableError` 하위 아님)을 두고 `error_code_map` 으로
  연결했으며, delete 초안은 `<조회값>→DELETED` 로 바꾸고 `status` 허용값을 op 별로 갈라
  (`DELETED` 는 delete 전용) I-10·I-11 본문 유출을 막았다. 삭제 op 는 `status` 를 stale 비교하지
  않는다 — `ON_SALE`·`HIDDEN` 어느 쪽에서든 정상 전이다. **I-17(§4.8)은 무변경** — Spring 이
  `!= ON_SALE` 을 전부 `"HIDDEN"` 으로 싣는 것을 BE 구현(`ProductChangesResponse.Item.hidden()`)으로
  확인했고, status 를 3값으로 넓히면 fail-closed 규약과 충돌해 정상 페이지가 전량 실패한다는
  사유를 스키마·명세에 남겼다. 와이어 계약 불변 — 바뀌는 것은 AI 소비측 처리다.
- **#496 — I-31 `sort` 어휘 개명(`rating` → `ratingAsc`) 미반영으로 대표 질문이 400 으로 깨지던 문제**
  (api-spec §4.20, v0.29.6). 2026-08-06 BE 협의가 구 안을 폐기했는데 사본이 갱신되지 않았고
  (v0.29.4/#472 전수 대조가 §4.20 을 "확정·구현 완료"로 표시하면서도 이 행은 놓쳤다), 그 문구가
  `get_reviews` 도구 docstring 과 `SpringClient.get_reviews` docstring 으로 전파돼 워커가 폐기된
  값을 그대로 호출했다 — "평점 낮은 리뷰 뭐가 문제야?" 가 `sort="rating"` → `400
  VALIDATION_ERROR` → `SpringUnavailableError` 로 새면서 **정상 질문에 시스템 장애를 보고**하고
  있었다. 사본·docstring 2곳을 `ratingAsc` 로 갱신하고(`ratingDesc` 는 없다 — 높은 별점은
  `rating="4,5"` 필터), `_REVIEW_SORT` 화이트리스트로 어휘 밖 값을 Spring 왕복(3s 타임아웃 예산)
  전에 거른다(`_ACCOUNT_EVENTS_GROUP_BY` 와 같은 패턴, 오류 문구에 유효 어휘를 실어 재시도 유도).
  stats 모드는 `sort` 를 서버에 싣지 않아 검증 대상에서 제외했다(#494 의 `rating` 전달과 무간섭).
  와이어 계약 불변.
- **#495 — I-16 이탈 회원 라벨 결측 표기·서버 절단 상한 고지 정합** (api-spec §4.4, v0.29.5).
  결측 표기를 `[?]` → `[라벨없음]` 으로 갈랐다 — 같은 요약 줄의 마지막 활동·세션도 결측을
  `?` 로 쓰기 때문에 `[?]` 는 "라벨 미수신"인지 "개명 미반영(#487 이 고친 증상)"인지
  문자열로 구분되지 않았고, 로그·리포트에서 두 상태가 같은 모양으로 남았다. #487 의
  원시 `memberId` 폴백 금지 원칙은 그대로다. 함께, 판매자에게 보이던 "서버 상한 50"
  하드코딩을 `seller_churn_server_list_cap` 주입으로 이관했다 — I-16 명세에 없는 BE 구현
  실측값(`CHURN_LIST_CAP`)이라 BE 가 바꾸면 거짓 고지가 된다(명세화 요청은 이슈 #495 코멘트).
- **#494 — I-31 집계 모드가 `rating` 필터를 버려 저평점 상품을 틀리게 지목하던 문제** (api-spec
  §4.20, 계약 무변경 — 코드가 확정 명세를 못 따라간 단방향 드리프트). `SpringClient.get_review_stats`
  시그니처에 `rating` 이 없어 `get_reviews(stats=True, rating="1,2")` 가 별점을 쿼리스트링에 싣지
  않았고, 전 별점 합산 `byProduct` 가 HTTP 200 으로 돌아와 워커가 그것을 "1–2점이 몰린 상품"으로
  서술했다 — 명세가 대표 사용례로 든 질문이 에러·경고 없이 조용히 틀렸고 `passed=True` 로 끝나
  로그에도 남지 않았다. 클라이언트·도구 양쪽에 `rating` 을 배선하고, 집계 출력에 적용 스코프를
  명시한다(`리뷰 집계(별점 1,2 한정): …`, 0건도 `별점 1,2 리뷰가 없습니다`). `rating` 미지정 시
  출력은 종전과 바이트 동일하다. 재발 방지로 응답 픽스처가 아닌 **요청 쿼리스트링 스냅샷** 테스트를
  추가했다(`docs/lessons.md` 2026-08-08 항목).
- **#468 — 이름 없는 상품의 카테고리 폴백이 상품명처럼 추천 문맥에 실리던 문제와, 추천 목록
  전달 실패 뒤 사용자가 추천을 받지 않은 것처럼 되묻던 문구를 바로잡았다** — 적재 시 원본 이름
  유무만 생성물 메타에 남겨 확실한 카테고리 폴백은 이름 지목 후보에서 제외하고, 목록 전달 실패
  사실은 스레드 상태로 보관해 다음 담기·찜 미해소 턴에서 다시 추천을 요청하도록 안내한다.
- **#489 — I-13 신필드(`salesQuantity`·체류시간 4종·`removeFromCart`)·I-6 `salesCount` 수신 정합**
  (api-spec §4.4, v0.29.3). 개정 전에는 **AI 가 판매 수량에 도달할 경로가 하나도 없었다** — I-6
  `salesCount` 는 BE 구현에 원래 있었으나 스키마에 필드가 없어 파싱되지 않고 버려졌고, 공백을
  메우려 신설된 I-13 `salesQuantity` 는 미반영, 대체 경로 S-1 `products[]` 는 같은 날 제거됐다.
  **근본 원인 제거**: `BehaviorProductRow` 만 `CamelModel`(pydantic 기본 `extra="ignore"`)을 상속해
  형제 판매자 모델(`SellerAggregateModel`, `extra="allow"`)과 달리 **BE 가 추가한 필드가 예외도
  없이 `model_extra` 에도 안 남고 통째로 소실**되고 있었다 — 베이스를 `SellerAggregateModel` 로
  교체해 앞으로 BE 가 뭘 추가하든 같은 일이 반복되지 않게 했다.
- **#488 — I-13 `purchaseComplete` 폐기 규정이 판매자 워커에 주입하던 오정보 제거** (api-spec
  §4.4, v0.29.2). 2026-07-31 개정(jarvis-backend#62 근본 수정 배포 / #196)으로 `purchaseComplete`
  는 **주문 기준 집계**(`order_item × product × brand`, PAID·`paid_at`, `COUNT(DISTINCT order_id)`
  — I-7 퍼널 4단과 같은 정본, 이벤트 유실 무관·소급 복구)가 됐는데, 구 규정("이벤트 기준이라
  상품 미귀속으로 0 집계될 수 있다 · 구매 권위는 I-6/I-7/I-14")이 6곳에 잔존해 워커에게 실재하는
  구매 데이터를 "신뢰하지 말라"고 안내하고 있었다 — 미반영이 아니라 능동적 오정보다. 도구 노트
  `_BEHAVIOR_AUTHORITY_NOTE` → **`_BEHAVIOR_PURCHASE_RULES_NOTE`** 로 개명·전면 교체(권위 위임
  고지 → 집계 단위 고지: 건수≠수량 / 상품별 합 > `eventType` 합계 / 부분 취소·반품 소급 반영),
  BEHAVIOR·ABUSE 워커 프롬프트, `BehaviorEventsResult` docstring, k-means 군집 모듈 docstring,
  `docs/api-spec.md` §4.4 I-13 사본을 함께 정정했다. BEHAVIOR_PROMPT 에 있던 "구매 관련 판정은
  퍼널과 교차 확인한 뒤에만 warning 이상" 게이트도 제거 — 데이터 불신을 전제로 세운 규칙이라
  전제가 사라지면 근거 없이 워커 민감도만 깎는다(`get_funnel` 보강 절차 자체는 유지). 폐기 어휘가
  LLM 주입 표면(도구 출력 3형 + 워커 프롬프트)에 없음을 어설션하는 **역방향 회귀 테스트**를 추가해
  같은 드리프트가 재발하지 않게 고정했다. 와이어 계약 불변(문자열 교체, 로직 변경 없음).
- **기간 확인 대기 TTL 만료가 시계 분해능에 의존하던 문제** (#345 후속, #346 에서 발견).
  `load_pending` 의 경과 시간 비교가 엄격 부등호(`>`)라 `ttl=0` 에서 "경과가 0보다 커야 만료"가
  됐고, Windows 기본 타이머 틱(~15.6ms) 안에서 저장→조회가 끝나면 경과가 정확히 0 이라 만료가
  서지 않았다(리눅스 CI 는 µs 분해능이라 늘 통과해 가려졌다). 경계를 포함(`>=`)으로 바꿨다 —
  `ttl=0` 은 "즉시 만료"가 맞는 해석이고 운영 TTL(10분)에서는 결과가 같다.
- **#346 — general·분석 레인의 기간 어휘 불일치 해소(#269 P2 앞부분)**. general 레인의 기간 환산이
  `GENERAL_PROMPT_TEMPLATE` 산문에만 있어 `period.py` 와 갈라져 있었다 — 같은 `"이번 달"` 이
  분석 레인에서는 당월 1일~어제(R1), general 레인에서는 당월 1일~오늘이었고, 더 나쁘게는
  `seller_period_max_days` 상한·0/음수·자릿수 가드가 이 레인만 **통째로 비켜갔다**(`"최근 999999일"`
  이 그대로 도구 인자가 될 수 있었다). 환산을 코드로 이관했다: `period.find_period_mentions`
  (자유 발화에서 어휘 추출 — planner 없는 레인이라 LLM 0회로 훑는다) → `resolve_period` →
  `pipeline.format_general_input` 이 `[조회 기간] from/to` 를 입력 메시지로 주입하고, 프롬프트는
  주어진 값을 쓰기만 한다(워커 규약과 같은 문장). 어휘표 밖 표현(`"오늘"`·`"이번 주"`·`"7월"`)은
  LLM·도구 호출 **전에** 되묻기로 끝나며, 문구는 종전대로 `period.py` 가 소유한다.
  코드가 값을 보충한 해석은 확인 왕복 대신 `disclosure_text` 로 고지하고 실행한다(오해석 비용
  비대칭 — DESIGN-SELLER-PERIOD §7.2). 백스톱으로 기간을 받는 조회 도구 9종에 인자 재검증
  가드(`_guard_period_args`)를 걸어 LLM 이 날짜를 지어내도 상한·역전이 다시 새지 않게 했다.
  와이어 계약 무변경.

### Added
- **#406 — 구매자 `progress`에 `retrying` stage를 추가** (api-spec §3.1, v0.32.4). I-1 검색이 재시도 가능한 실패 뒤 실제 다음 시도에 들어갈 때만 즉시 내보내며, 기본 `spring_max_retries=0`(#394 한시 조치)에서는 기존 인라인 검색 경로를 그대로 유지한다.
- **#476 — 스트림 레지스트리를 워커 간 공유로 올릴 수 있게 했다** (`STREAM_REGISTRY_BACKEND=shared`,
  기본값은 종전 `memory` — **출하 동작 무변경**, 계약 무변경). §2.9(a) 활성 슬롯·scope fence·
  scope idle 대기를 **셋 다** pg-profile 테이블 2종(`active_streams`·`stream_scope_fences`)으로
  옮겨, 워커를 다중화해도 409 가드가 유효하고 세션 claim 경로가 워커 간에 어긋나지 않는다.
  fence 원자성의 근거를 "`acquire_fence()`에 await 가 없다"(이벤트 루프)에서 **스코프 키에 건
  `pg_advisory_xact_lock`**(트랜잭션)으로 갈아끼웠고, 모든 행에 lease/TTL 을 둬 죽은 워커가
  방을 영구히 409 로 만드는 #48 성질의 누수를 막는다. 공유 저장소 장애는 fail-closed —
  기존 계약 코드 `503 STATE_UNAVAILABLE` 로 나간다(새 오류 코드 없음). `active_count()` 는
  프레임마다 호출되는 관측 경로라 여전히 프로세스 로컬 O(1) 이며, 그래서 `chat_request` 에
  워커 지문 `workerFp` 를 추가해 워커별 값을 갈라 합산할 수 있게 했다.
  `REGISTRY_IS_PROCESS_LOCAL` 하드코딩 상수는 설정 파생 `registry_is_process_local()` 로 바뀌었고,
  Dockerfile 가드는 "워커를 켜려면 공유 백엔드도 함께 켜라"로 정확해졌다.
  (`docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md`)
- **#476 — `chat_request`에 활성 스트림 도착 표본(`activeStreams`)과 턴 중 피크
  (`activeStreamsPeak`)를 추가** — 단일 이벤트 루프의 검색 파싱 부하가 동시 20에서 3초 예산을
  넘은 실측(#427)을 운영 로그로 판단할 수 있게 한다. 동일 방 409 거절도 선행 활성 수와 함께
  남기며, 이 값은 외부 API 계약이 아닌 서버 내부 관측이다(**워커별 값** — `workerFp` 로 가른다).
- **#346 — 비교(기준) 기간 어휘 양 레인 지원** (`직전 동일 기간`·`지난달 대비`·`전월 동기간`·
  `작년 대비`·`전년 동기간`). `period.resolve_comparison(expr, base)` 가 본 기간을 받아 환산하고
  (`직전 동일 기간` 은 보충값이 없어 확인 불필요 — `tools._previous_period` 와 같은 정의,
  달력 시프트 2종은 정렬 방식을 코드가 고르므로 확인 대상), 확인 판정은 본 기간과의 **합집합**이다.
  배선은 `AnalysisPlan.comparison_expr`(planner 는 표현만) → `ResolvedPlan.compare_from/to` →
  입력 메시지 `[비교 기간]` 한 줄이며 **도구 시그니처·Spring 계약은 불변**이다 — 워커가 두 기간으로
  같은 도구를 각각 호출한다. general 레인은 한 발화에서 비교 표현을 먼저 떼어내 본 기간과 함께
  해석한다. 확인 대기 저장(`period_confirm`)에도 비교 기간을 실어 승인 재개가 대조군을 잃지 않게 했다.
  (DESIGN-SELLER-PERIOD §2.5, 와이어 계약 무변경)
- **#489 — I-13 `BehaviorProductRow` 신필드 5종** (api-spec §4.4, v0.29.3): `salesQuantity`(PAID
  `SUM(oi.quantity)`·아이템 `PENDING`/`CANCELLED`/`RETURNED` 제외 — I-6 `salesCount` 와 동일 산식)와
  체류시간 4종(`medianDwellSeconds`·`avgDwellSeconds`·`dwellSampleCount`·`dwellSource`). 전부
  **nullable 이며 기본값 `0` 을 두지 않는다** — 명세가 `null` 을 계약값으로 규정한다(`0`="안 팔림",
  `null`="미조회"). `0` 기본값은 `churn_rate` 에서 잡았던 silent-mismatch(#197)를 그대로 재도입한다.
- **#489 — I-6 `SalesSeriesPoint.salesCount`** (api-spec §4.4, v0.29.3). `granularity=summary` 응답에는
  수량 필드가 없어 nullable. `get_sales_timeseries` 요약이 포인트별 `/N개`·기간 합계를 함께 표기하되,
  `null` 포인트를 `0` 으로 섞지 않고 집계 대상 포인트 수를 밝힌다.

### Security
- **#487 — I-16 이탈 코호트가 원시 `memberId` 를 판매자 LLM 표면에 싣던 재식별 경로 차단**
  (api-spec §4.4, v0.29.1). `get_churn_cohort` 요약이 이탈 회원을 `[41] 마지막 활동 …` 형태로
  적재해, 판매자 주문 화면(S-2: `orderId` + 수령인 실명)과 대조하면 회원이 특정됐다 — memberId 는
  가명이 아니라 재식별 키라는 것이 #481 I-14 개정의 근거였는데, 같은 논리가 I-16 에만 적용되지
  않고 있었다. 노션 개정이 I-8·I-14·I-16 **동시 배포**를 전제했으므로 그 사이 기간 내내 노출이
  I-16 경로로만 열려 있던 셈이다.

### Changed
- **#489 — behavior 요약 5종화 + 신지표 표기** (api-spec §4.4, v0.29.3). I-13 `counts` 어휘에
  `removeFromCart` 가 편입되어 표시 행·꼬리 합계 키·꼬리 출력 3곳이 각각 4종을 하드코딩하던 것을
  모듈 상수 `_BEHAVIOR_COUNT_KEYS`/`_BEHAVIOR_COUNT_LABELS` 로 단일 출처화했다. 상품 행에
  판매 수량과 체류시간(중앙·평균·표본 수)을 함께 싣고, `dwellSource` 한계(next_event 기준이라 세션
  마지막 조회가 표본에서 빠짐)는 행마다 반복하지 않고 요약 말미에 1회 각주로 붙인다.
  `dwellSampleCount` 가 없거나 0 이면 중앙값·평균이 실려 와도 **수치를 감추고 사유만 남긴다**
  (표본 없이 해석 금지 — conversion 워커 유의성 판정 원칙과 동일). 군집(k-means) 피처와 Tukey 비율
  지표는 **의도적으로 종전 4종·3종 유지** — 어휘 확장과 분석 피처 변경은 별개 결정이라 스코프 밖.
  `_BEHAVIOR_AUTHORITY_NOTE` 에는 "판매 **수량**의 권위는 같은 행의 `salesQuantity`" 한 줄을 더해,
  `purchaseComplete` 경고가 신설 수량 지표까지 싸잡아 불신하게 만들지 않도록 했다.
- **#487 — I-16 노션 2026-08-06 개정 정합(#481 잔여분)** (api-spec §4.4, v0.29.1).
  `ChurnMember` 에서 `member_id`·`last_login_at` 을 제거하고 `customer_label`(HMAC 6자 사례번호,
  I-14 와 같은 규약·같은 값)을 신설했다. `get_churn_cohort` 요약은 라벨만 노출하며 **라벨 결측
  구간은 `?` 로 떨어뜨린다 — memberId 폴백을 두지 않는다**(I-8 "404 시 구경로 폴백 금지"와 같은
  원칙: 조용히 원시 회원 키로 되돌아가는 것이 이번에 고친 결함이다). `last_login_at` 은 표시
  계층에서 읽히지도 않던 사문이고, 계정 보안 정보를 회원 단위로 판매자에게 줄 근거가 없어 명세와
  함께 제거했다. 사례번호 규약 문구는 `_ORDER_LOG_RULES_NOTE` 안에 I-14 기록 규칙과 섞여 있던
  것을 `_CUSTOMER_LABEL_NOTE` 상수로 뽑아 I-14·I-16 양쪽 도구 출력에 부착했다(복붙본이 갈라져
  한쪽 규약만 낡는 것이 이번 누락의 구조적 원인이라, 문자열·위치를 그대로 둬 I-14 회귀는 없다).
  `CHURN_PROMPT` 에도 `ABUSE_PROMPT` 와 같은 취지의 라벨 규약을 넣었다. **BE 배포 순서와 무관하게
  안전하다** — `SellerAggregateModel(extra="allow")` 이 구응답 `memberId`·`lastLoginAt` 을 예외
  없이 `model_extra` 로 흡수하고 표시 계층이 읽지 않으므로, I-8 같은 파괴적 경로 전환이 아니다.
- **#481 — I-14·I-8 노션 2026-08-06 개정 정합(판매자 파트, BE Phase 2 동시 배포 전제)** (api-spec
  §4.4, v0.29.0). **I-8 브랜드 스코프 전환**: `spring_client.get_account_events` 가 전역
  `/internal/account-events` 대신 `/internal/seller/{brandId}/account-events`(자사 코호트)를
  호출한다 — `AccountEventsResult` 에서 `isSuspicious`(코호트 스코프에선 상시 false 오보 위험)·
  `failCount`·`nullMemberRatio` 제거, `suspiciousMemberCount`(I-14 어뷰징 기준 SQL 교차 회원 수,
  개수만)·`scope:"brand"` 에코 반영. 도구는 runtime 의 brand_id 를 쓰고(IDOR 방지) ip 정렬 축을
  failCount → suspiciousMemberCount 로 교체했다. **churn 워커에서 get_account_events 배선 제거**
  (WITHDRAW 는 member 에 탈퇴 필드가 없어 상시 0건 — abuse 전용 보조 소스로 축소).
  `seller_account_events_enabled` 기본 false → **true**(#197 보류 사유였던 전역 데이터·admin 소유
  협의가 이 전환으로 해소 — 플래그는 운영 킬스위치로만 유지, BE 신경로 미배포 구간은 404 →
  보조 소스 degrade 관용으로 흡수). **I-14 개정 반영**: `buyerMemberId` → `customerLabel`(HMAC
  6자 사례번호) 전환에 맞춰 docstring·요약 규칙 노트·워커 프롬프트에 사례번호 규약(실명·연락처
  추정 금지, orderId 대조 유도 금지, 조치 안내는 "사례번호 X로 관리자 문의")을 넣고,
  `orderItemId`(아이템 전이만 값 — 같은 orderId 복수 행=아이템별 전이)·자사 스코프 축소(행 수
  감소는 회귀 아님)·집계 단위(byStatus 층위 혼재, cancelReasonsTop·I-16 returnReasonsTop=아이템
  수)·발송 `actorType="SELLER"` 해석 규칙을 도구 docstring 과 프롬프트에 반영했다. 구 events/stats
  오배선(상시 0건)은 #194 에서 기수정 — 이번 범위 아님(노션 확인 요청에는 기수정으로 회신).

### Docs
- **#473 — api-spec §2.9(c) I-1 재시도 행의 BE 관측 포인트가 첫 `conditions` 앞 Spring 직렬 단 수를
  2단(`2 × 3s = 6s`)으로 세던 것을 3단(최대 `3 × 3s = 9s`)으로 정정** — 빠져 있던 단은 확장 턴의 구제
  재검색(F-1/#222·#343, 상호배타라 턴당 최대 1회)이다. 코드는 #383(PR #414)·#427(PR #452)로 이미 3을
  세고 회귀 테스트가 그 값을 고정하는데 명세 사본만 2로 남아 있던 drift 정정이다. 2026-08-09 확인 결과 이
  수치는 사본에만 있다 — Notion 정본도 BE 레포도 콜백 `3s` 만 적어, 정정할 BE 기준이 있는 게 아니라 직렬
  3회·최대 9s 가 미고지였던 것이다. 그 고지는 2026-08-09 BE 에 완료했다(최초 고지, BE 조치 불필요).
  (api-spec §2.9(c), v0.32.1)
- **#476 — 워커 다중화 선행조건과 프로세스 로컬 상태 인벤토리를 문서화하고 증설 가드를 추가** —
  `ActiveStreamRegistry`가 프로세스 로컬인 동안 Dockerfile worker 설정을 테스트로 막고,
  `WEB_CONCURRENCY >= 2` 기동은 경고로 관측한다. 권고는 owner(JWT `sub`) sticky를 1단계로 하고,
  다중 EC2 또는 sticky 불가 시 TTL/만료를 갖춘 공유 레지스트리로 전환하는 것이다.
- **#472 — Notion API 정본 전수 대조와 사본 동기화** (api-spec §3.1, §3.5, §3.7, §3.8~3.9.4, §4.2, §4.5~4.6, §4.8, §4.11~4.12, §4.14~4.20, §6.1~6.2, v0.29.4) — 코드 변경이 필요한 계약 드리프트와 정본 미해결을 별도 감사표로 분리하고, 문서 전용 확정 사항만 사본에 반영했다.
- **#357 — 개인화 그래프 Spring 협의 패킷 v1→v2 전면 개정: BE 회신 3건·운영 덤프·`jarvis-backend`
  코드 실측으로 C-28 종결·C-27 코드로 대부분 확정·C-18/C-19 해소를 반영** — v1(2026-08-07)은 "전
  항목 회신 대기"였으나, BE가 브랜드 병합 결과(106행 병합/110건 삭제)·8/5 정리 대응표(34건)·
  자리표시자 목록(41건/상품 249개)·운영 덤프 전체를 보내왔고, `~/inte-final/jarvis-backend`(75커밋
  뒤처져 있던 것을 최신화)를 직접 실측했다. **C-28(브랜드 통제 어휘)은 해소됐다 — BE에 보낼 요청이
  0건이라 협의 항목에서 내린다.** 시드 2,368행 →(8/5 −34)→(8/7 −110)→2,224행(BE 발표와 일치)
  →(자리표시자 −41, 한글↔영문 −40)→2,143행이 되는 계산 체인을 전부 검증했고, 브랜드 행 수는 8/7
  운영 덤프를 `zcat\|sed`로 직접 재집계해 복원 없이 독립 재확인했다(2,143행 일치). **2026-08-08
  17:47 운영 덤프(`jarvis-prod-live.sql.gz`)로 한 번 더 독립 재검증**해 총행수(2,143)와 미탐
  3쌍·대형 브랜드(나이키·아디다스·언더아머) 병합을 재확인했고, `jarvis-dump-20260807.sql.gz`와
  `brand`·`product` 데이터가 완전히 동일함을 코드로 직접 대조해 확인했다 — 8/7 이후 하루 사이
  추가 정리는 없었다. 이 재검증 과정에서 직전 개정의 오류도 하나 잡았다 — "`아디다스 오리지널스`가
  이미 병합됐다"고 적었던 것은 틀렸고, 실제로는 두 덤프 모두에 `아디다스`(87상품)와 별도로
  `아디다스 오리지널스`(9상품)가 남아 있어 하위 라인 잔여 목록으로 되돌렸다. **현재 실제 잔여는
  성격이 다른 4행·28상품(「브랜드 미상」)뿐**이다 — 이전 개정에서 자리표시자(`중국OEM`·`협력업체`
  등)를 잔여로 다룬 것은 오류였다(그 목록은 8/7 병합 시점 스냅샷이었고 같은 날 41행 정리에서 이미
  전부 지워졌다). 결론을 뒤집었다 — v1의 "BE 조회 endpoint 요청"을 철회하고, **RDS는 건드리지 않고
  AI 쪽 `canonical_id` 매핑 사전으로 닫기로 결정**했다(브랜드 미상 4행·표기 변형 3건·하위 라인
  9건은 전부 사전에서 흡수하는 차기 DB 정리 대상이지 BE 협의 항목이 아니다, 하위 라인 병합은
  사용자 결정으로 확정, 검색은 I-1 다중 브랜드 파라미터로 해결). BE는 I-17(§4.8) 응답에 `brandId`
  추가에 합의(2026-08-08)해 사전이 지속 동기화된다. 남는 것은 재적재 시 정규화 지속 여부를
  다음 덤프에서 우리가 직접 확인할 관찰 항목 1건뿐이며 BE에 요청하지 않는다. **C-27(캐시 무효화)은
  마지막 라운드에서 전면 재작성했다** — `jarvis-backend`(head `eed93d0`)를 다시 읽어 캐시 키가
  `p5:home:{memberId}`(회원 id 단독)임과, `writeCache()` 호출 지점이 코드 전체에 한 곳뿐이고
  개인화 성공 분기에만 있음을 재확인했다. 이 재확인으로 **직전 개정의 오류를 정정했다** — "재개도
  무효화 트리거 3개 중 하나"라는 서술은 틀렸다(`fallback()`은 캐시에 쓰지 않아 재개는 애초에 캐시가
  비어 있다). 무효화가 필요한 지점을 **중지·초기화 2곳**으로 좁혔다 — 항목 삭제(M-13)도 다음
  consolidation 전까지 랭킹에 반영되지 않아 실질 효과가 없으므로 요청에서 뺐다. **결과적으로
  C-27은 확약 요청이 아니라 구현 시 `redisTemplate.delete(...)` 한 줄을 부탁하는 요청으로
  축소됐다.** 캐시 키가 회원 id 단독임을 재확인해 api-spec §4.11 캐시 키 서술 오류를 다시
  확정했고, catalogVersion이 애초에 캐시 키에 들어가지 않는다는 사실로 C-18 폐기 근거를
  보강했다. **C-20 요청문에 이슈 코멘트(jovial1ns, 2026-08-06)가 지목한 I-번호·`M-11`~`M-16`
  채번 확인 항목(api-spec C-26)을 추가했다**(R-20-5) — 낮은 번호 추측이 실제로 충돌했던 전례
  (I-29~I-31, #297)를 근거로 든다. **C-18(`catalogVersion` 폐기)·C-19(`limit≤60`·
  `signals≤200`·최신순)는 BE 코드(`HomeRecommendationRequest` 클래스 주석·`MAX_LIMIT`/
  `MAX_SIGNALS` 상수)로 수용이 확인돼 해소로 전환**했다. 노션 「취향 관리 API 10개(되돌리기 폐기)」
  문서에 대응하는 신설 절을 추가해 우리 답 3건(재삭제 200+replayed 유지 가능·Spring 타임아웃 4s
  제안·취향 추출은 주기 배치가 아니라 세션 종료 트리거)과 결정 3건(즉시삭제+tombstone 필수·확인창
  필요·If-Match 필수 유지)에 대한 의견을 남겼다(실제 api-spec 개정은 하지 않음, 별건). **C-20/C-21은
  여전히 회신 대기**이며 BE 쪽도 M-11~M-16 미착수임을 코드로 재확인해 리드타임 논거를 보강했다.
  `docs/api-spec.md`·`SPEC-PROFILE-GRAPH-149.md`에 대한 개정안 6건(C-20/21/27/28 상태·캐시 트리거
  2종·REQ-PGRAPH-010 id 키잉 대안·OPEN-G2 해소·§12 선결조건·C-18/C-19 해소·§4.11 캐시 키 정정)을
  unified diff로만 첨부했다(적용 금지 — 두 정본 파일은 이번 PR에서 한 글자도 바뀌지 않았다). 카테고리
  시드 드리프트(1,007→1,003 잎)·골든셋 재라벨 필요(삭제 상품 249건 중 20개가 정답 라벨)·브랜드
  사전 신설(#150 착수 시)을 후속 이슈 후보로 남겼다. **이 PR은 이슈를 닫지 않는다** — 발송은 사용자
  몫이고, C-20/C-21 회신이 있어야 완전히 닫히므로 `Closes #357`을 쓰지 않는다.
  (`docs/specs/BE-NEGOTIATION-GRAPH-357.md` v2.3.0, 마지막 수정 라운드)

### Added
- **#474 — 색상 동의어 확장의 결정론 A/B 골든셋 회귀를 추가했다** — 고유어 발화와 정본 표기 쌍을 별도 MFT로 고정하고 I-1 mock의 color 배열 계약을 실제로 계측한다.
- **#438 — 부하 테스트용 결정론 스텁 LLM provider `LLM_PROVIDER=scripted`** —
  `evals/benchmark` 러너가 매 요청 실 LLM(decompose+rerank)을 호출해 격자가 커질수록 비용이
  커져 실질적으로 못 돌리던 문제를 해소한다. 신규 `app/core/llm_scripted.py::LoadTestLLM`
  (`ScriptedLLM` 상속)이 rerank 후보 productId를 CANDIDATES에서 그대로 파싱해 되돌려주는 등
  각 호출 지점 파서가 실제로 받아들이는 최소 유효 응답을 프롬프트에서 유도한다 — 고정
  productId(101/102)를 실 카탈로그에 그대로 쓰면 항상 degrade로 떨어져 "정상 경로 p95"
  대신 "degrade 경로 p95"를 재는 왜곡을 막는다. 미상 프롬프트는 조용한 폴백 대신 `LLMError`로
  소리 나게 실패한다. `app_environment`가 `local`/`test`가 아니면 기동 자체를 거부하고
  (config.py, 운영 var 오설정 방어) 기동 로그에 경고 배너를 남긴다(app/main.py). 산출물은
  스스로를 증언한다 — 트레이스에 모델 id `scripted-stub-fast`/`scripted-stub-smart`를 남겨
  (토큰 수는 추정 없이 `None`) 서버 로그 조인 보고서 최상단에 자동 경고가 붙는다
  (`evals/benchmark/report.py`). 판매자 레인은 무료 모드 범위 밖이라 `init_seller_model`이
  명시적으로 거부한다. `ScriptedLLM`/`DEFAULT_*`는 `tests/integration/_stubs.py`에서
  `app/core/llm_scripted.py`로 이동했고(런타임이 `tests/`를 import할 수 없어, Dockerfile이
  `app/`만 이미지에 넣는다) 기존 파일은 재수출만 한다. 사용법·측정 가능/불가능 범위는
  `evals/benchmark/README.md` 참조. 그 스텁 모드로 **첫 무료 baseline**을 산출했다
  (`evals/benchmark/baselines/20260809T014442747650Z-local-stub-spring`, Spring 기동·pg 실물·
  LLM만 스텁, `buyer_recommend` × 동시성 1/5/10/20 × 30건, error·timeout 0) — 처리량이 동시성
  10 부근에서 포화하고(3.84 → 3.99 req/s) 그 뒤로는 지연만 늘어난다. LLM 지연이 빠졌으므로 이
  포화점은 벤더가 아니라 우리 코드·풀·pg 쪽 한계를 가리킨다. 짝이 되는 실 LLM 좁은 격자는
  같은 조건에서 LLM 만 실 벤더(openai)로 바꿔 동시성 1/5 × 30건만 좁게 떴다
  (`20260809T021733612671Z-local-realllm-spring`). **두 baseline 의 차이가 벤더 지연
  기여분**이다 — p50 기준 c=1 4553ms(83%) · c=5 5114ms(79%), p95 기준 6876ms(88%) ·
  7083ms(78%), 처리량은 4.6~6.4배 떨어진다. 즉 실 LLM 으로만 재면 우리 코드의 포화점이
  벤더 분산에 묻혀 보이지 않는다 — 이슈가 무료 모드를 원한 이유가 실측으로 확인됐다.
  종단 예산도 함께 봤다: 관측 max 는 8.0~9.6s 로 `stream_total_timeout_buyer_s=30` 대비
  3배 이상 여유가 있다. `costUsd` 는 단가표가 비어 `unknown` 이라 0 으로 추정하지 않고
  토큰 수(prompt 431,301 · completion 31,144)만 남겼다(비용 관측은 #437 소관).
- **#482 — 개인화 활성화 지표(Δranking rate)를 Tier L 산출물에 편입** — 종전 Tier L 은 이득
  지표(`pairedVsGuest` 의 nDCG delta)만 실어, "프로필이 아무것도 바꾸지 않아 효과가 0" 과
  "바꾸기는 하는데 좋은 방향이 아님" 이 구분되지 않았다. 두 상태의 처방이 정반대(소비 방식 수정
  vs 프로필 내용 수정)라 판정이 갈리는데 근거가 없었다. `evals/personalization/activation.py`
  (순수 함수)가 `(caseId, repeat)` 로 짝지어 동일·순서만·집합변경을 세고, `comparison.json` 의
  `rankingChange` 와 `comparison.md` 표로 나간다. 기존 `baselines/live-v1` 산출물에 소급 적용한
  결과 `guest` 대비 `clean_rerank_only` 는 **58.1%(18/31)** 로, 프로필은 절반 이상의 턴에서 노출을
  실제로 바꾸고 있었다 — 즉 현행 개인화의 문제는 "손잡이가 죽었다"가 아니라 "방향"이다. 기준선
  arm 을 `LIVE_BASELINE_ARM` 상수로 뽑아 이득·활성화 두 지표가 같은 기준을 보게 강제한다.
- **#345(#269 P1) — 판매자 분석 레인이 `이번 달`·`올해`·`상반기`·`3분기`·`최근 3개월` 을 알아듣고, 해석한 기간을 실행 전에 확인받는다** — #269 는 P0(침묵 폴백 제거)·P1(어휘 확장+확인 흐름)·P2 로 나뉘어 있었는데 P0 만 구현한 PR #284 가 이슈를 닫아 P1 이 유실돼 있었다. 그 상태에서 "이번 달 매출 분석해줘"는 버그가 아니라 **설계상** 되묻기로 떨어졌다. 실측하니 차단 지점이 셋이었다 — ① `PLANNER_PROMPT [기간]` 절이 미지원 어휘를 clarification 으로 끊고, ② `AnalysisPlan.period_expr` 의 **Field description 에도 같은 정규 어휘 4종이 박혀 있어**(구조화 출력이라 LLM 이 이것도 읽는다) 프롬프트만 고치면 둘이 반대를 지시하며, ③ `calc.normalize_period` 가 `최근 3개월` 을 명시 거절했다. 셋을 함께 고쳤다.
  - **신규 `app/agents/seller/period.py`** — 구 `calc.normalize_period` 를 이관·확장하고 `PeriodResolution`(값 + `needs_confirmation` + `clipped`)을 반환한다. 확인 없이 통과 5종(`지난달`·`최근 N일`·`최근`·`어제`·`YYYY-MM-DD~YYYY-MM-DD`, **회귀 가드로 고정**) / 확인 후 통과(`이번 달`·`올해`·`상반기`·`하반기`·`N분기`·`최근 N주`·`최근 N개월`·연도 없는 날짜 `M월 D일~M월 D일`) / 여전히 되묻기(`작년 여름`·`최근 반년`·`최근 한 달`·혼합 표현). 경계 규칙 5종: R1 오늘 제외(당일 집계 미완결), R2 미래 절단(**확인 어휘만** — 명시 범위는 판매자가 직접 지정한 값이라 말없이 자르지 않는다), R3 완전 미래 거절(8월의 `4분기`, 단 `하반기` 는 7월이 지났으므로 절단 대상), R4 상한, R5 연도 추론(**연도 없는 날짜만** — `N분기`·`올해` 는 어휘 자체가 "올해의"를 뜻하므로 작년으로 미루지 않는다). `최근 N개월` 은 30일 근사가 아니라 달력 기준이다.
  - **문구 소유권을 구조로 단일화했다** — 되묻기 문구 생성 지점이 planner LLM 의 `clarification` 과 `calc` 예외 메시지 **두 곳**이라, P0 가 보장했다고 믿은 "예외 메시지 = 사용자 문구"가 실은 "planner 가 통과시켰을 때만" 성립하는 조건부였다(dev 실측 문구가 코드 원문과 달랐던 이유). planner 는 이제 **기간 표현을 그대로 옮겨적기만** 하고 **기간을 이유로 clarification 을 쓰지 않는다** — 프롬프트와 스키마 description 양쪽에 금지 문장을 넣고 테스트로 고정했다. 어느 쪽이 문구를 썼는지 측정해 맞추는 대신 생성 지점을 하나로 만들었으므로 프롬프트·모델이 바뀌어도 갈리지 않는다.
  - **확인 흐름(신규 `period_confirm.py`)** — 코드가 값을 보충한 해석은 팬아웃 **앞**에서 `PipelineResult(kind="period_confirmation")` 로 끊고(잘못 해석한 기간으로 워커 LLM·Spring 비용을 쓰지 않는다), 확인 문구는 어휘가 아니라 **환산된 날짜**를 되돌려 보여준다(절단됐으면 그 사실도 밝힌다 — 자르고 말하지 않으면 P0 가 없앤 "조용한 대체"가 형태만 바꿔 돌아온다). 대기는 `seller-period:{sellerId}:{threadId}` 네임스페이스 checkpoint 에 `ResolvedPlan` 통째로 저장한다(`thread.py` recorder 패턴, seller_id 접두가 IDOR 차단). 승인 시 `orchestrator.run_resolved_pipeline` 로 재개 — planner 이후 구간을 별도 함수로 **분리**했으므로 "planner 재호출 0회"(#269 완료 조건)가 조건문이 아니라 호출 그래프로 보장되고, planner 빌더가 호출되면 실패하는 테스트로 고정했다.
  - **경로는 3개다(원안 4개에서 축소)** — 승인 / 새 질문 / TTL 만료(`seller_period_confirm_ttl_minutes`, 기본 10분). "아니 7월로" 같은 **수정 발화는 새 질문으로 흡수**된다: 확인 문구가 이미 대화 스레드에 기록돼 있어 planner 가 맥락을 보고 재계획하므로 별도 기간 파서가 필요 없고, 파서를 따로 두면 어휘 정의가 두 곳으로 갈라진다.
  - **승인은 자유 텍스트 + 코드 선판정(LLM 0회)이다 — "발화 ≠ 동의" [HARD] 의 명시적 예외.** HITL 상품 쓰기 승인은 최상위 `action` 구조화 필드로만 받는 규약을 그대로 두고 기간 확인만 예외로 둔다(읽기 전용이라 되돌릴 수 있고 판매자가 즉시 정정할 수 있다). 갈림길은 "자유 텍스트냐"가 아니라 **"승인이 되돌릴 수 없는 부작용을 일으키는가"** 임을 DESIGN 에 못박았다. 판정은 정규식 누적이 아니라 **공백으로 나눈 모든 토큰이 긍정 어휘일 때만 승인** — `네 7월로 해줘` 는 `7월로` 때문에, `응 아니야` 는 `아니야` 때문에 자동으로 새 질문이 된다. 입구 순서는 ①.7(HITL confirm·"N번 적용해줘" 뒤, scope 선차단 **앞** — `"응"` 이 scope 필터에 걸리지 않게).
  - **와이어 계약 무변경** — SSE 이벤트·요청 필드가 그대로라 api-spec 개정도 FE 작업도 없다. 확인 턴은 기존 `token`+`done(panel:"keep")` 으로 나간다(확인은 대화이지 보고서가 아니다). 설계: `docs/specs/DESIGN-SELLER-PERIOD.md`. ⚠️ `GENERAL_PROMPT_TEMPLATE` 의 `이번 달`(= 당월 1일~**오늘**)과 분석 레인(R1 로 **어제**)이 하루 어긋나는 문제는 #269 P2("레인 통일") 범위로 남겼다 — DESIGN §7 에 기록.
- **#462 — 취향 추출 골든셋 하네스(`evals/taste_probe/`) 신설, 미탐율·오탐율·trivial baseline
  최초 산출** — #356 이 만든 구조화 트리플 추출 경로(`generate_session_delta` → `should_promote`
  → `resolve_triple`)가 재는 대상인데, `scripts/probe_delta_prompt_356.py` 는 정답 라벨 없이
  잴 수 있는 것(승격률·kind 분포)만 쟀다 — "몇 개 뽑았나"는 알아도 "맞게 뽑았나"는 몰랐다. 30세션
  골든셋(kind 7종 커버리지·polarity 쌍·반복·선호→회피 전환·잡담 오탐 슬라이스, v2026-08-08.2)에
  세션당 N회 실 LLM 반복으로 `recall`(primary)·`noiseFalsePositiveRate`·`nodeIdAgreement`(사전
  등록 2차)·`missRate`·`falsePositiveRate`·`sessionExactSet`(exploratory)를 매기고,
  `resolverDroppedByKind`·`legacySchemaNoKind`·`factDedupCollapsed`·kind/predicate 오분류
  행렬로 미탐 원인(프롬프트/게이트/resolver 중 어디)을 가른다. 판정(게이트·resolver·정규화·
  식별자 산출)은 전부 프로덕션 함수를 import 해 그대로 부른다(판정 복제 0, #380 규약). CI 는
  가짜 LLM·가짜 카탈로그로 실 LLM/pg 콜 0(`tests/unit/test_taste_probe_{schema,runner,metrics,
  cli}.py`). **2026-08-09 키 교체로 실 LLM 경로가 열려 최초 기준선(`openai-20260809-n5`,
  provider=openai·model=gpt-5.6-luna·N=5)을 `evals/taste_probe/baselines/` 에 편입했다** —
  recall 73.0%(84/115) · noiseFalsePositiveRate 0.0%(0/50) · nodeIdAgreement 88.1%(74/84),
  `resolverDroppedByKind={'category': 26}` 로 category 대량 드롭을 실측(단일 실행, 방향
  판정용). 상세 해석·정본 선언 표는 `evals/taste_probe/baselines/README.md` 참조. 계약
  (api-spec) 무변경.
- **#442 — 조건 칩 제거(`conditionActions`)가 로그에 아무 흔적도 남기지 않아 무동작과 구분이
  안 되던 관측 침묵을 메웠다** — `run_buyer_turn`(`app/agents/buyer/graph.py`)의
  `_remove_condition_actions` 호출부에 결정 로그 `condition_actions_applied`(요청 축·**실제로
  비워진** 축·no-op 여부·`requestId` 상관키, 값은 미포함 — #119 PII 규약)를 추가했다. "비워진
  축"은 요청 필드에서 예측하지 않고 호출 전/후 `prior` 를 실측 비교해 낸다 — 예측식이었다면
  `_remove_condition_actions` 가 통째로 죽어도 로그가 똑같이 나왔을 것이다(변이 시험으로 확인:
  no-op 으로 되돌리면 신규 테스트가 즉시 깨진다). `prior is None`(스레드 만료·첫 턴)이라 분기
  자체를 안 타는 경우도 `condition_actions_skipped_no_prior` 로 구분되게 했다 — 동작은 그대로
  (지울 대상 없음, 무시가 맞다), 관측만 추가. `buyer_chat_turn` metadata(`SAFE_METADATA_KEYS`)는
  건드리지 않는다 — 축 이름은 로그로 충분히 관측되고 화이트리스트 개정은 계약 표면만 넓힌다.
  계약(api-spec) 무변경.
- **#258 — 색상 동의어 사전 정본을 repo 로 편입하고 1차 사람 검수 결과를 고정한다** — A 파트
  (PR #273)가 만든 789행 색상 표기 동의어 사전이 지금까지 로컬 pg-catalog 안에만 있었는데,
  원천 I-17(Spring)이 2026-08-07 실측(`scripts/check_spring_connection.py`)에서 도달 불가로
  확인돼 DB 를 날리면 재수확이 불가능하다는 것이 드러났다. `#401`(카테고리 사전) 이 만든
  `db/catalog/seed/` 전례를 그대로 따라 정본을 repo 로 편입한다. 라이브 pg-catalog
  `color_synonyms`(기계 산출: term/canonical/provenance/doc_count) 위에 사람 검수 오버레이
  `db/catalog/seed/color_synonyms_review.json` 을 적용해 `db/catalog/seed/color_synonyms.json`
  정본(789행)과 부트스트랩 SQL `db/catalog/init/05_color_synonyms_seed.sql` 을 같은 원천에서
  함께 만드는 `scripts/derive_color_synonym_seed.py`(`--check` 모드 지원, 새 의존성 없음)를
  신설했다. 오버레이는 오버레이 내부 정합성 → 하네스트 대비 존재성 → 의미 규칙(고아 승인
  금지·2단계 체인/순환 금지·`_norm` 충돌 없음) 순으로 검증하고 위반 시 조용히 무시하지 않고
  실패한다. 1차 검수 결과 46행(앵커 15 + 한글 고유어/한자어 ↔ 외래어 표기의 1:1 자명 대응
  동의어 31)을 `approved`/`human` 으로 고정했다 — `곤색`은 LLM 이 `블루`로 배정했으나(seed_llm_
  assignment) 검수에서 `네이비`로 정정(紺色=감색=navy), 검수 overlay 가 LLM 배정을 덮어쓸 수
  있어야 한다는 요구의 실제 사례다. `app/pipelines/color_synonym_seed.py` 에 `load_seed_rows`/
  `seed_from_file`(+ 새 상수 `UPSERT_SEED_COLOR_TERM_SQL`)을 추가해 정본 파일의 `status`/
  `canonical`/`provenance`를 항상 권위 있게(authoritative) 반영하도록 했다 — 기존
  `UPSERT_COLOR_TERM_SQL`(배치 수확이 사람 검수 결과를 덮지 않도록 보호하는 CASE 가드형)과는
  별개 경로다. `color_synonym_expansion_enabled`/`color_synonym_array_contract_ready` 기본값
  (둘 다 `False`)과 I-1 질의 확장 배선(#273 기 반영)은 변경하지 않는다 — 런타임 동작 변화는
  이 PR 의 범위 밖이다.
- **#427 — 검색 타임아웃을 턴 예산에서 파생시킨다(DESIGN-SHARED-BUDGET-384 §3 D1~D8)** — 고정
  3s 검색 타임아웃이 성공했을 검색을 실패로 바꾸는 문제를, I-1 검색 전용 타임아웃
  (`SPRING_SEARCH_TIMEOUT_S`, 기본 3.0 — 오늘 값 불변)을 AI→Spring 공용 타임아웃에서 분리하고,
  구제 체인(F-1/#343/자동완화 probe)이 스트림 시작 시각(`open_stream` 의 실제 데드라인과 같은
  원점)에서 파생한 잔여 예산으로 검색 타임아웃을 좁히거나(`RESCUE_BUDGET_MODE=narrow`) 최소
  하한 미만이면 건너뛰는(`narrow_skip`, 본검색 제외) 3단 스위치로 푼다. 기본값은 `observe`
  (판정만 계산·로그, 실제 집행 없음 — 오늘 동작 불변)이며, 기동 검증기(`_require_search_retry_
  within_stream_budget`)와 런타임 좁히기가 같은 계수 함수(`_rescue_chain_stage_counts`/
  `_rescue_chain_serial_budget_s`)에서 계수를 얻어 한쪽만 고쳐지는 드리프트를 구조적으로
  막는다. 계약 무변경 — `docs/api-spec.md` 는 건드리지 않았다(§2.9(c) 개정은 별도 사람 승인
  게이트).
- **#455 — I-1 `options`·`optionCount` 소비로 옵션 되물음 단축(api-spec §4.6·§4.1, v0.28.3)** —
  사용자가 이번 발화에서 말한 조건으로 `CART_OPTION_REQUIRED` 후보가 정확히 1개로 좁혀지면
  되묻지 않고 같은 턴에 담고, 여러 개로만 좁혀지면 좁힌 목록으로 되묻는다(`optionId`는 여전히
  I-2 400 응답에서만 얻는다 — 이름으로 유추하지 않는다). `optionCount`는 자동 선택의 정합
  가드로 쓴다(불일치 시 자동 선택 금지). 발화 매칭은 부분 문자열이 아니라 토큰 경계 + 조사
  허용목록으로 판정해 "블루투스"에 "블루"가 우연히 걸리는 것을 막는다. 신규
  `app/agents/buyer/cart/options.py`(순수 좁히기 함수) · 튜너블 2종
  (`cart_option_narrow_min_term_len`·`cart_option_match_suffixes`).
- **#469 — 홈 추천(I-22)·칩 제거 LangSmith 관측 추가 + home_reco 로그 결함 수정** — (1) I-22 에 요청 트레이스 신설: 루트 `home_recommendation` + 단계 span 4종(`home.profile`/`home.query_vector`/`home.rank`/`home.reasons`) — 운영 콜드스타트 504 진단이 로그 한 줄뿐이던 것을 단계별 지연으로 볼 수 있게 했다. finish/export 는 P-5 예산(3s)을 지키기 위해 요청 경로에서 await 하지 않고 분리 태스크로 흘린다. memberId 원값은 트레이스에 없다(지문만, §3.7 [HARD]) — 콘텐츠 모드일 때만 시그널·outcome·반환 id 가 실린다(strict 카나리아). (2) `/chat` 루트 콘텐츠에 `conditionActions` 직렬화 기록 — 발화 없는 칩 제거 턴이 트레이스에서 보이게. (3) `home_reco_request` 로그의 outcome·개수가 `extra` 로 남아 기본 포맷터에서 증발하던 결함을 JSON 메시지 방식(observability 관례)으로 수정 — `docker logs` 에서 outcome 구분 가능. 계약(api-spec) 무변경.
- **#432 — 과소지정 프로브에 union 측정 모드를 넣어 전개 후 판정까지 잰다** — 기존
  `evals/underspecified_probe` 는 decompose 직후·판정 직전 형상만 쟀다. `--union`(기본 off)을
  켜면 `app.agents.buyer.graph._prepare_recommendation`(카테고리 매핑 + `needs_expansion`
  #217 보정)을 decompose 산출의 깊은 사본에서 그대로 태워 "전개 후 판정"까지 재고,
  `missRateAfterExpansion`·`falseAlarmRateAfterExpansion`·`expansionSuppressionRate`·
  `expansionGateFiredRate`(가정판 `expansionGateWouldFireRate` 의 실측 대응물) 4축을 추가한다.
  union 단계 전용 LLM 은 decompose 프롬프트 오버라이드가 없는 `PacedLLM`(같은 delegate·pacer)
  이라 보조 LLM 노드(카테고리 택일·전개)가 후보 프롬프트로 덮이지 않는다. 실측 2판: smart
  티어(진단용, 프로덕션 아님)에서 `missRate` 56/104→`missRateAfterExpansion` 59/104,
  `expansionSuppressionRate` 3/48(6.2%), `expansionGateFiredRate` 47/232(20.3%, 가정판
  6.2%보다 훨씬 크다 — D2 규칙이 가정판에서는 구조적으로 발동할 수 없었다); fast
  티어(프로덕션, `#430` 미머지라 전복 축은 해당 없음)에서 `expansionGateFiredRate`
  78/240(32.5%)만 실측됐다. union 축은 전부 exploratory 이고 union 단계 실패 표본은 버리지
  않고 union 축 분모에서만 제외한다(`unionStageErrorCount`, 실측 2판 모두 0). `--union` 없는
  기본 실행의 산출물 형상은 그대로 얼려 `#433` 이 굳힌 6판과 계속 비교 가능하다.
  `evals/legs_probe` 의 union 후속과는 앵커·정답지가 달라 묶지 않는다. **2차 리뷰 후속
  (G-3)** — `#430` back-merge 로 fast 티어의 선행 조건(판정 True 표본 존재)이 충족돼
  `union-fast-2026-08-08-post430-run1`(prompt `865ed6fd771e`)을 추가로 돌렸다:
  `missRate` 8/112(7.1%)→`missRateAfterExpansion` 62/112(55.4%),
  `expansionSuppressionRate` **55/106(51.9%)** — decompose 단계가 정확히 되물어야 한다고
  판정한 표본의 절반 이상이 전개(카테고리 매핑) 단계에서 억제된다. `#431` 전환 판단의 실제
  재료는 이 판이다. 기준선 색인의 프롬프트 세대 라벨도 "현행 dev 프롬프트"에서 해시 표기로
  바로잡았다(`#386`·`#430` 두 차례 머지로 낡았던 라벨, G-1).
- **#433 — 과소지정 프로브 기준선을 n=1 에서 두 프롬프트 세대 각각의 n=3 분포로 굳힌다** —
  #380 이 커밋한 `fast-2026-08-06/` 은 단일 실행이라 `#430` before·`#431` 전환 판단의 근거로
  쓰기엔 재현성이 없었다. 착수 전 실측에서 `hashes.systemPrompt` 가 커밋된 기준선과 현재 HEAD
  사이에 다르다는 사실을 발견했다(`#386` `wishlist_view` intent 신설이 decompose `_SYSTEM` 을
  바꿈) — 두 프롬프트 세대를 각각 독립 3회씩 굳혔다: 현행 dev 프롬프트(`e62fd0f6e03d`)
  `fast-2026-08-08-run1~3`(missRate 99.1~100.0%, 편차 0.9%p, **`#430` before 정본**)와
  pre-#386 프롬프트(`11c6fe3bfa0c`) `fast-2026-08-06-run2`·`run3`(missRate 100.0%, 편차 0%p,
  역사 기록). `smart` 티어 1회(`missRate` 53.8%)도 추가해 원인 축 분해가 티어에 따라
  달라지는지 봤다 — `semanticQueryIsFallback` 단독 비율(88~93%)은 티어 무관하게 안정적이지만
  `missRate` 자체는 fast 대비 smart 가 훨씬 낮다. 기준선 색인
  `evals/underspecified_probe/baselines/README.md` 신설(정본 하나만 인용하도록). 프롬프트·
  하네스 코드는 바꾸지 않았다(`metrics.py` docstring 의 비-recommend intent 목록에
  `wishlist_view` 한 단어만 추가, 계측 동작 불변).
- **#356 — consolidation 구조화 트리플 산출 + 그래프 입력 전환(OPEN-G0 해소)** —
  취향을 자유형 한국어 문장 하나가 아니라 `주어–술어–목적어` 트리플로 만들고, consolidation이
  fact 목록 대신 **그래프 문서를 입력으로 읽게** 했다. 지금까지는 지울 수 있는 단위가 없어
  사용자가 취향을 삭제해도 다음 배치가 다시 써넣었다 — 삭제 기능이 겉모습만 남는 상태였다.
  트리플 생산과 입력 전환을 **한 PR로** 낸 이유가 그것이다(REQ-PGRAPH-023 [HARD]).
  신규 `app/agents/profile/graph_models.py`(GraphNode/GraphEdge/GraphDocument, 내부 저장 모델) ·
  `resolver.py`(kind별 결정론적 식별) · `graph_merge.py`(순수 함수 병합 엔진).
  식별자는 `node_id = "{type}:{정규화 라벨}"` · `edge_id = "e_" + sha256(edge_key)[:16]`로
  고정했다(REQ-PGRAPH-010) — 랜덤 id면 재파생이 tombstone을 우회한다. `hashlib` 고정(내장
  `hash()`는 PYTHONHASHSEED 랜덤화로 프로세스마다 값이 달라진다). LLM은 타입 붙은 제안까지만
  내고 키는 코드가 확정한다(REQ-PGRAPH-011, #115 실측 근거). resolve는 **쓰기 시 1회**로
  고정 — 배치마다 재계산하면 임계·어휘가 바뀔 때 같은 fact가 다른 `node_id`로 붙는다.
  `priceBand`·`ratingBand`·`product`는 임베딩 없이 규칙·정확 일치(REQ-PGRAPH-014), 어휘 없는
  kind는 `verified:false`로 남기고(C-28 미해결 상태에서도 동작) 어휘가 있는데 못 붙으면 드롭한다.
  병합은 감쇠 가중 EMA·승격/강등 히스테리시스·충돌 supersede(삭제 금지)·tombstone 보존이며,
  edge 상한 절단에서도 **사용자 삭제(`suppressed`·pin)는 상한보다 우선**한다 — 잘리면 다음
  배치에 `active`로 부활해 복구 경로가 없다. **먼저 밀려나는 순서는 `active` → `superseded` →
  (자르지 않음) `suppressed`·pin**이다. 직관과 반대로 보이지만 잃는 것이 다르다 — `active`가
  잘려도 그 fact는 요약 입력에 남지만(문서에 없는 `edge_key`는 `active`로 간주된다), `superseded`가
  잘리면 같은 규칙 때문에 **진 취향이 요약에 되살아난다**. 사용자 삭제만으로 상한을 넘으면
  넘긴 채 보존하고 경고 로그를 남긴다. 상충 판정은 쌍 열거가 아니라 **`avoids` vs 임의의 긍정**
  (`prefers`·`likes`·`interestedIn`)이다 — `{likes, avoids}`만 등록하면 resolver가 kind별로 다른
  긍정을 만드는 탓에 7개 kind 중 4개가 판정 밖에 남아 모순된 두 취향이 둘 다 `active`로 공존한다.
  요약 입력은 살아 있는 edge + 트리플 없는 fact이고 `suppressed`/`superseded`와 그 근거 fact
  원문은 제외한다 — 입력이 비면 기존 요약을 **보존**하고 `NO_WORK`(빈 문자열로 덮으면 요약은
  사라지는데 홈 랭킹은 캐리오버된 옛 벡터로 계속 개인화한다). LLM은 그래프 락 밖에서 부른다
  (`#323`의 요약 락과 중첩하면 advisory 풀 커넥션을 둘 점유해 구매자 턴까지 말라 죽는다).
  신규 config 11종 전부 주입(`graph_node_distance_max`·`graph_decay_half_life_days` 등) —
  거리 임계는 #59 값을 **상속하지 않는다**(앵커 분포가 다르다, OPEN-G1/#344 재측정 대기).
  프롬프트 교체는 `profile_graph_delta_enabled` 롤백 스위치 뒤에 두고 분포 비교 프로브를
  동봉했다(OPEN-G8). 발표·수동 검증용 시드 스크립트 신설.
  **델타·요약 LLM 출력 예산도 하드코딩(800/1000)에서 `profile_delta_max_tokens`·
  `profile_summary_max_tokens`(각 2048)로 이관했다** — 구조화 필드가 늘어 출력이 길어지자
  운영 smart tier(reasoning 모델)에서 추론 토큰이 예산을 먼저 먹어 분포 프로브 4세션 중 2건이
  `LengthFinishReasonError`로 죽었고(구 프롬프트 0건), 이관 후 0건이 됐다. 세션 버퍼는 보존된 채
  재시도되지만 같은 입력이면 또 실패해 방치하면 그 사용자의 승격이 버퍼 상한까지 멈춘다(#325 계열).
  **비범위**: 그래프 API 표면(#150) · 저널·revision CAS·멱등 원장(#358) · pin 규약 ·
  브랜드 어휘 수집(C-28). `purchased` edge는 대화에서 만들지 않는다(원천은 질의 시점 I-19).
  (SPEC-PROFILE-GRAPH-149 v0.2.6, SPEC-PROFILE-001 v0.8.1 — api-spec 무개정)
- **#424 — combo_matrix `observed` 드리프트 가드 신설** — `expected_behavior.jsonl` 의 `observed`
  는 러너 재실행 **기록**이라, 다른 레인이 SSE 이벤트를 바꾸면 커밋본이 조용히 낡아도 아무
  테스트도 잡지 못했다(PR #420 작업 중 실측 2회, 둘 다 `eventTypes` 만 드리프트하고 핵심 계약
  필드는 불변). 전량 byte diff 는 SSE 를 건드리는 모든 레인(동시 6~8개)에 이 eval 데이터
  재생성을 강제해 레인 결합 비용이 크므로, 핵심 계약 필드(`terminal`·`finishReason`·`errorCode`·
  `actionType`·`actionReason`·`pushCount`·`pushProductCount`·`listType`·`searchCallCount`·
  `searchFilters`·`unappliedSearchFilters`·`unhandledException`·HOME 계약/계측 4종)만 골라
  `OBSERVED_GUARDED_FIELDS`(`evals/combo_matrix/schema.py`)로 추리고, `eventTypes`·
  `lastTokenText`·`notes`/`note` 는 다른 레인의 정상 작업이라 제외했다. 새 테스트
  `test_observed_guarded_fields_match_recomputed_values_for_all_ci_rows`
  (`tests/eval/test_combo_matrix_eval.py`)가 PR 마다 `refresh_observed(write=False)` 결과와
  커밋본을 딕셔너리째(키 존재 여부 포함) 대조하며, `status` 와 무관하게 `observed` 가 있는 모든
  ci 행(partial 인 combo-0038 포함)을 본다 — 기록 신선도 검사이지 미정의 동작의 스펙화가
  아니다. 변이 시험으로 경계를 확인했다: `finishReason` 변경은 가드를 깨뜨리고 `eventTypes`
  변경은 통과시킨다(둘 다 원복). 계약(api-spec) 무변경.
- **#386 — 채팅으로 찜 목록 조회(`wishlist_view` intent 신설)** — "내가 뭐 찜했지?"가
  `recommend`/`general` 로 새던 것을 고쳤다. 장바구니에는 `cart_view` 가 있는데 찜에만 조회가
  없던 비대칭을 메운다. 배관(`spring_client.get_wishlist`(I-28)·`WishlistItem` 스키마)은 이미
  있었고 **라우팅 의도와 응답 핸들러만 없었다** — api-spec §4.16 이 이 동작을 이미 규정하고
  있었으므로 계약 개정은 없다(구현이 명세를 따라잡은 것, `docs/lessons.md` "명세가 규정한
  동작이 구현되지 않은 채 지나갔다"의 재현). `stream_wishlist_view` 는 목록을 `token` 텍스트로만
  답하고 상품 카드도 `action` 도 내지 않으며(경로 B), 항목별 구매 가능 상태 라벨은
  `state_suffix` 를 재사용한다(#310). 신원 게이트는 `stream_cart_view`(게스트 허용)가 아니라
  **형제 찜 핸들러**(`user_id is None` 하나로 게스트·익명 차단)를 따른다 — 찜은 회원 전용
  (I-26/27/28, M-4)이라 cart 게이트를 베끼면 계약을 위반한다. 조회 실패는 `token` 안내 +
  정상 `done` 이다(`action.type` 유니온에 조회 실패 어휘가 없다) — 변경 턴의 선행 조회라
  `action(WISHLIST_REMOVE_FAILED)` 를 내는 `stream_wishlist_remove` 와 처분이 갈리는 지점이며,
  개별 `except SpringUnavailableError` 는 형제 4개와 같은 규약이다(#368).
  (api-spec §4.16 — 계약 불변, 구현 상태만 갱신)
- **#380 — 과소지정 판정 축 실 LLM 실측 하네스 신설(`evals/underspecified_probe`)** — SPEC-
  UNDERSPECIFIED-336 §7.3 이 남긴 게이트 잔여 항목("실 LLM 이 판정 축을 실제 발화에서 얼마나
  정확히 산출하는지 실측하지 않았다")을 채운다. 30 앵커(cases.json 승계 7 + 신규 23) × N=8 =
  240콜을 `is_underspecified_turn`·`detect_expansion_need` 프로덕션 함수에 그대로 넣어(판정
  로직 복제 금지) `missRate`(confirmatory-primary)·`falseAlarmRate`(confirmatory-secondary)를
  Wilson CI·trivial baseline 대조·원인 축 분해(ablation, `blockingAxes` 조합별 집계)·불변
  재판정(flag off·prior 게이트)과 함께 잰다. confirmatory 분모는 `intent=="recommend"` 표본만
  쓴다 — 프로덕션은 그 턴에서만 판정을 호출한다(`nonRecommendIntentCount` 진단으로 노출 크기를
  남긴다). `fast`(gpt-5-nano) 1회 실측(리뷰 라운드 1 수정 반영 재실행):
  **`missRate` 112/112(100.0%)** · **`falseAlarmRate` 0/104(0.0%)** — 미탐 원인
  `blockingAxes` 조합은 `semanticQueryIsFallback` 단독 82.1% · `filters.attrConditions`
  동반 9.8% · `categoryQueries` 동반 8.0%(fast 티어가 "아무거나"류 발화에도 의미쿼리·속성
  조건을 스스로 채워 넣는다). `judgmentAccuracy`(48.1%)는 이번 런에서 trivial baseline 과
  동률이다. 기본값(`underspecified_reask_enabled`)은 전환하지 않는다 — 이 실측은 그 전제
  하나를 채웠을 뿐이다(`docs/specs/SPEC-UNDERSPECIFIED-336.md` §7.3,
  `evals/underspecified_probe/baselines/fast-2026-08-06/`).
- **#401 — 카테고리 사전 시드 정본을 repo 로 편입하고 0행/0임베딩 가드를 둔다** — 발화→카테고리
  매핑(`category_distance_max=0.26`, #344)의 근거 사전(leaf 1,007행)이 지금까지 repo 밖
  (`~/inte-final/_sql`)에만 있어 아무도 그 근거를 재현·검증할 수 없었고, `db/catalog/init/
  02_categories.sql` 은 스키마만 만들어 fresh 환경은 `categories` 0행으로 뜬 채 조용히 무필터로
  퇴화했다(개별 발화 매핑 실패와 사전 전체 결측이 같은 신호로 보임). MariaDB 카탈로그 덤프에서
  leaf 1,007개를 codepoint 정렬로 뽑아 `db/catalog/seed/categories.json` 정본을 만들고
  (`scripts/derive_category_seed.py`, `--check` 모드로 재현 검증 가능, 새 의존성 없음),
  같은 원천에서 부트스트랩 SQL `db/catalog/init/04_categories_seed.sql`(embedding NULL, 2단계
  분리 설계 유지)을 생성했다. `app/pipelines/category_seed.py` 에 `check_category_dictionary`
  가드를 추가해 기동 시(`app/main.py` lifespan) categories 총 행 수와 **embedding 채워진 행
  수를 따로** 확인한다 — `search_categories_pg` 가 `embedding IS NOT NULL` 로 거르므로 행만
  있고 임베딩이 없으면 사전이 0행인 것과 동일하게 죽기 때문이다(둘 다 구성 오류로 ERROR 로그,
  `category_dictionary_startup_check` 설정으로 `off`/`log`(기본)/`fail` 선택 — 기본이 `fail`
  이 아닌 이유는 사전 결측이 서비스 전면 중단보다 하방이 얕은 상태이기 때문). `log`/`off` 는
  DB 연결 실패로 기동을 막지 않지만, `fail` 은 사전이 건강함을 확인하지 못하면(도달 불가 포함)
  기동을 거부한다 — `psycopg` 가 연결 실패에 구조화된 판별자를 주지 않아 "일시적 불통"과
  "영구적 구성 오류"(DSN 오타 등)를 예외 타입으로 구분할 수 없기 때문이다(리뷰 대응).
  `evals/category_probe/manifest.py::dictionary_fingerprint` 에
  `canonicalSha256`/`seed`/`matchesSeed` 를 추가해 라이브 DB 상태를 정본과 대조할 수 있게
  했다(기존 `rowCount`/`sha256` 은 과거 런 비교를 위해 그대로 보존). `category_distance_max`
  값 자체는 바꾸지 않았다.
- **#396 — 구매자 `progress` 다회 emit + `stage` 어휘 확장(1종 → 7종, 개방형)** —
  `analyzing` 1종·턴당 최대 1회이던 진행 표시를 파이프라인의 실제 경계마다 stage 를 바꿔
  내보내도록 확장했다. `mapping`(카테고리 매핑 중)·`expanding`(니즈 전개 중, #198)·
  `searching`(상품 후보 검색 중)·`relaxing`(조건 완화 재검색 중)·`reranking`(재정렬 중)·
  `publishing`(목록 준비 중, I-21 push 직전) 6종을 추가하고 계약을 `0~1회` → `0회 이상`으로
  개정했다(FE 는 모르는 `stage` 를 무시하는 개방형 규약). `app/agents/buyer/graph.py::
  _prepare_recommendation` 은 mapping/expanding 프레임을 내야 해서 코루틴 → async
  generator 로 바꿨다(반환값은 전용 홀더 `_PrepareRecommendationOut` 에 담아 전달 —
  generator 는 `return` 값을 줄 수 없다). `relaxing` 은 자동 완화 루프가 **실제로 probe**
  했을 때만 나가도록 지역 플래그로 지켰다(루프 진입만으로 내면 probe 0회인 턴에도 뜨는
  거짓 신호가 된다). `mapping` 은 리파인 승계(`carry`)·카테고리 리셋(`clear`) 분기에서는
  매핑 자체를 안 태우므로 나가지 않고, 전개 성공 후 재매핑에서도 같은 논리 단계의 연장이라
  다시 내지 않는다. `app/core/observability.py::RequestObservation` 에 stage 별 최초 발생
  시각(`started` 기준 ms)을 `progressStages` 로 로그에 남기되, 판매자 `progress`(`{"text"}`,
  `stage` 없음)는 섞이지 않게 분리했다. 기본값 다회 emit 화로 `test_buyer_tracing`·
  `test_condition_actions`·`test_fanout`·`test_recommendation` 의 이벤트 인덱스 가정이
  깨져 실제 stage 시퀀스로 갱신했다(단언 약화 없음). **기존 6종의 이름·페이로드·상대 순서는
  불변**(추가 전용) — `conditions`는 여전히 검색·자동 완화 뒤다. `progress`는 `token` 이후
  (`publishing`)에도 올 수 있다. PR #407 리뷰로 드러난 `_prepare_recommendation` 제너레이터
  전환의 사각지대(`scripts/capture_i1_wire_132.py`·`scripts/verify_regression6_217.py` 호출부
  2곳이 `TypeError` 로 깨져 있었다)도 함께 async generator 소비 형태로 갱신했다. (api-spec
  §2.2·§3.1, v0.27.0)
- **#310 — `purchaseState` 로 품절·판매종료를 갈라 안내한다(장바구니·찜)** (api-spec §4.9·§4.16,
  v0.26.3 / SPEC-CART-001 v0.2.6 REQ-CART-037) — 지금까지는 장바구니·찜에서 상품의 구매 가능
  여부를 파싱조차 안 해 "구매 불가 상태예요"조차 말하지 못했다. 품절은 기다리면 되고 판매
  종료는 다른 걸 찾아야 하므로 **사용자가 취할 행동이 다르다**. `CartViewItem` 에
  `purchaseState` 파싱을 추가하고(BE `InternalCartResponse.Item` 에 실재하는데 선언이 없어
  `extra="ignore"` 로 버려지고 있었다), 장바구니 조회·삭제 되물음·찜 되물음 **세 지점 모두**에
  같은 라벨을 붙였다 — 같은 장바구니가 질문 방식에 따라 다르게 보이면 안 된다. 조회는 목록
  줄에 짧은 라벨(`(품절)`/`(판매 종료)`)만 붙이고 행동 안내는 문단 끝에 상태당 한 번만 싣는다.
  문구는 프롬프트가 아니라 결정론적 순수 함수(`app/agents/buyer/cart/purchase_state.py`)로
  생성해 단위 테스트로 고정한다. **미수신 기본값을 `"AVAILABLE"` → `None`(모름)으로
  바로잡았다** — 소비가 붙은 이상 기본값은 주장이 되고, 키가 없다는 사실을 "구매 가능이
  확인됨"으로 읽으면 못 사는 상품을 살 수 있다고 안내하게 된다(#305 가 남긴 재검토 항목).
  계약 밖 상태값은 항목을 살린 채 필드만 `None` 으로 강등한다 — 찜처럼 항목을 skip 하면
  "전부 빼줘"가 일부만 지우고 성공을 보고한다. `AVAILABLE`·미수신이 모두 무표시라 기존 문구는
  바이트 단위로 불변이다. **AC③ 부분 충족** — "내가 뭐 찜했지?" 질의를 받는 `wishlist_view`
  intent 가 아직 없어(api-spec §4.16 이 이미 요구하는 미구현 갭, **#386**) 찜 쪽은 해제
  되물음에만 라벨이 붙는다. **AC⑤ 는 코드 변경 없이 닫는다** — Spring I-1 이 살 수 없는 상품을 후보에
  넣지 않고 CH-5 가 카드 조회 시점에 한 번 더 드롭하므로(api-spec §4.6·§4.2) AI 가 추천 단계에서
  상태를 알 수단도, 낄 자리도 없다.
- **#370 — 골든셋 v2.2 위반 네거티브 채널 신설 + 라벨 provenance 기록(`evals/goldenset`)** —
  #333 adjudication 라운드가 남긴 갭 3건(위반 네거티브 0건·라벨 주체 미기록·슬라이스 쿼터
  하향 사유 미문서화) 후속. `CaseCore`에 `labelSource`/`labeledAt`/`labelRationale` 신설해
  전 127건에 소급 기입(`backfill_label_provenance.py`, 문서화된 사실만·불명은 `unknown`).
  `category_violation` rule 신설, 오프라인 결정론 스크립트 `inject_violation_negatives.py`로
  가격 초과(13케이스·47후보, injected)·카테고리 이탈(4케이스·5후보, 기존 candidate 재태깅)
  주입, 속성 위반은 catalog attribute 키 명 불일치로 미달을 그대로 기록(조작 안 함).
  `validate_cases()`에 위반 태그 후보 4종 기계 검증(실제 위반 성립·정답 편입 금지·fixture
  단독 소유) 신설, `audit.run_audit()`에 `violationNegativeFill` 산출. manifest에
  `violationNegatives`·`sliceQuotaFill`(dev `nonRankingFailureMftMin` 6 목표 대비 실채움 5 —
  문서 근거 없는 기존 미달로 신규 확인, 정직하게 기록) 블록 신설. `datasetVersion` 2.2.0,
  scoring/filter_axes baseline 재실행(`evals/scoring/baselines/dev-v2.2` 신설,
  `evals/filter_axes/baselines/trivial_empty` 제자리 갱신) — ablation 실 LLM n5 baseline은
  2.1.0 해시 고정 참조로 재실행하지 않는다(비용 결정 대기). 위반 네거티브 후보를 실제로
  주입해보니 `evals/metrics/harness.py`의 Spring mock이 검색 요청의 가격 필터를 무시하고
  있었다는 것도 드러나(goldenset 데이터만이 아니라 이 harness를 쓰는 모든 eval 소비자의
  노출 집합 계산에 영향) mock이 요청의 `minPrice`/`maxPrice`를 실 Spring처럼 적용하도록
  고쳤다 — 가격 미상(`price: null`)은 그대로 통과시킨다. 기존 커밋 데이터에는 가격 위반
  후보가 0건이었으므로 이번 수정으로 기존 케이스의 노출·지표는 바뀌지 않았다(실측 확인).
  계약(api-spec) 무변경.
- **#363 — 구제 체인(#222 F-1·#343 억제-후 재판정) first-token 지연 계측 + 최악 경로 순차 왕복
  상한 회귀 테스트 — "예산 내"가 아니라 이미 데드라인 초과, 기동 가드(#288) 과소계상도 발견**
  — 운영 로그(`recommend_zero_result`·`category_expand_post_suppress_fallback`)는 배포 1일
  미만이거나 아직 미배포라 실측이 불가해(근거 `docs/specs/MEASURE-FIRST-TOKEN-363.md` §2), 대신
  `category_expand_zero_fallback`/`category_expand_post_suppress_fallback` 성공 로그에
  `elapsed_ms`를, `recommend_zero_result`에 `rescue_elapsed_ms`·`relax_probes`·
  `relax_auto_elapsed_ms`(자동완화, first SSE **이전**)·`relax_chip_elapsed_ms`(칩 probe, first
  SSE **이후** — 합치면 아직 스트림에 안 나간 소요가 섞여 과대계상되므로 필드를 분리했다)를
  추가해 다음 배포부터 실측 가능하게 했다. fake 로 재현한 최악 경로(확장 턴 전량 억제 + #343
  폴백 실패 + 자동완화 probe 실패)로 first SSE(conditions) 이전 순차 Spring 왕복이 **정확히
  3단**(초기 fan-out + #343 폴백 + 자동완화 probe)임을 회귀 테스트로 고정했다. **최악 상한
  3단×`spring_timeout_s`(3s)=9.0s를 first-token 을 실제로 끊는 예산과 비교하면 30s
  (`stream_total_timeout_buyer_s`, 첫 이벤트 이후만 덮는 전체 상한)가 아니라 10s
  (`stream_first_token_timeout_s`, 첫 이벤트 이전 상한)여야 하고, 그 기준으로는 소모율 90%에
  선행 decompose LLM head(p95≈3.0s, #151)를 더하면 12.0s>10.0s — 최악 경로는 오늘 설정에서
  이미 first-token 데드라인을 넘어 504가 된다**(PR #362 리뷰의 "3단 적층 ≈9s" 우려를 수치로
  확인·정정, 이슈 본문의 "30s 예산 내" 전제는 반증됨). 기동 가드
  `_deferred_first_event_i1_calls`(#288)도 이 3단 중 구제 폴백 항을 빠뜨려 항상 2로
  과소계상한다는 것을 발견 — `spring_timeout_s ∈ [10/3, 5.0)` 구간은 가드를 통과하면서 실제로는
  데드라인을 넘는다. 보정된 일반형(`1 + (1 if category_expand_enabled else 0) + min(...)`)을
  문서화하고 가드/실측 값의 불일치(2 vs 3)를 `tests/unit/test_config.py`에 회귀 테스트로
  고정했다 — 런타임 가드 동작은 배포 영향을 고려해 이번 PR에서 바꾸지 않는다(적용은 후속
  이슈). 공유 왕복 예산/first-token 데드라인 가드 설계도 후속 이슈로 넘긴다(§4·§5가 이미
  "유의" 판정 근거이므로 후속은 실빈도 실측이 목적). **Claude PR Review(#379) 반영** — 위 계측
  필드가 `recommend_zero_result`(0건 종결)에만 있어 **구제가 실제로 성공한 턴**(이 이슈가 재려는
  핵심 표본)은 관측되지 않던 구멍을 발견 — 상호 배타인 `recommend_pipeline`(성공 종결)에도 같은
  세 소요 필드와 `may_auto_relax`(conditions가 검색 전/후 어느 쪽에 나갔는지, first-token 지연
  여부 판정에 필수)를 추가해 두 로그의 합집합이 전수가 되게 했다. 계약(api-spec) 무변경.
- **#371 — combo_matrix INV/DIR 쌍 실검증 러너(`evals/combo_matrix/pair_runner.py`)** — #335 매트릭스에
  라벨만 있고 실행이 없던 INV/DIR 3쌍을 실제로 검증한다. INV(combo-0056, rerank 실패 degrade)는
  push 계약 형태(listType·lists 길이·필드 존재, 실측상 productIds 멀티셋까지) 동일성을 비교하고,
  DIR(combo-0054, 카테고리 필터 추가)은 방향(push 상품 수 비증가) + 공허 통과 방지 guard(필터
  진상위집합·base 결과 수>0)를 함께 강제한다. 분자·분모를 동봉한 `PAIR_CHECKS.md` 를 생성물로
  남긴다. 실측 불가 축(회원 recall≥게스트 DIR, combo-0055)은 `evals/goldenset`(#333) 소관으로
  명시 분리(mode=manual). 부수 발견: `category` 필터축이 canonical-or-null degrade(legs 미경유
  시 무조건 null)로 인해 이 하네스 전체(#335 기존 55건 포함)에서 실제 검색 경계에 도달한 적이
  없었다 — `pair_runner` 전용 seam(exact-match 카테고리 매핑 fake)으로 combo-0054 만 해소했고,
  기존 55건의 잔여 맹점은 후속 이슈로 이관(README 정정). 계약(api-spec) 무변경.
- **#331 — 카테고리 매핑·선택 평가 하네스(`evals/category_probe/`) 신설** — 발화→카테고리 정확도가
  골든셋 슬라이스 9건에만 얹혀 단독으로 잴 방법이 없었다(`evals/README.md` 공백 표). `evals/intent_probe`
  확립 규약(전역 페이서·실패는 표본이 아님·단일 실행 판정 금지)을 승계해, 배포 파이프라인과 같은 함수
  (`decompose` → `map_categories`)를 같은 순서·인자로 부르고 `search_categories_pg`/`exact_lookup`/
  `embed_texts` 는 실물에 위임하며 기록만 하는 래퍼로 leg·anchor_kind 별 top-k 히트를 계측한다(#344
  임계 스윕용 `hits.csv`). 앵커 38셀(single 14 MFT+8 INV·multi 6·none 5·notInCatalog 5, goldenset
  `category_mapping_failure` 9건 중 8건을 caseId 로 승계)은 라이브 pg-catalog canonical 표기(`대분류 >
  잎`)를 쓰고 스키마(accept `" > "` 1회·발화 누출 금지)+런타임 pre-flight(accept 실재·notInCatalog
  키워드는 leaf 수준에서 부재 확인) 2단으로 검증한다. trivial baseline(임베딩 최근접, LLM 0콜)을
  1급 산출물로 동봉(§328 1항). CI 미포함(수동 도구), 유닛테스트는 전부 가짜라 API/pg 콜 0. 계약
  (api-spec) 무변경.
- **#372 — #336 되물음 답변 턴 멀티턴 테스트 + 완화칩 우선순위 규칙 문서화(플래그 기본 off 유지)** — 적대적 심사가 짚은 두 갭을 메웠다. ① 되묻는 턴까지만 테스트되고 답변 다음 턴이 검증된 적이 없었다 — 신규 `tests/unit/test_underspecified_answer_turn.py` 가 같은 thread_id 2턴을 구동해 정상 카테고리 답변(PRIOR_FILTERS 승계를 `FakeLLM.calls` 로 배관 실측 + 답변한 카테고리·승계 price_max 가 실제 검색 필터에 실림을 직접 단언)·무관 답변(general 폴백, 죽지 않음)·recommend 레인 안에서 카테고리 아닌 축(색상)만 답한 턴·거부 답변("그냥 아무거나" 반복 시 무필터 I-1 로 떨어지는 기존 멀티턴 경계를 관찰로 고정) 네 시나리오를 고정했다. ② flag on 시 완화칩이 과소지정 턴에서 차단되는 동작(SPEC §7-4)이 "알려진 한계"로만 적혀 있어 우선순위 규칙도 회귀 테스트도 없었다 — reask > relaxation chips 우선순위를 `SPEC-UNDERSPECIFIED-336.md` §7.2 에 명문화하고(api-spec §3.1 `suggestions` 는 조건부 이벤트라 미발신이 계약 위반이 아님을 근거로 적시), 차단 재현 핀·비과소지정 턴 회귀 가드·답변 턴 칩 복원·자동완화 두 게이트가 유효 설정에서 관측 불가능함을 고정하는 구조 테스트(SPEC 문구도 실제 커버리지에 맞게 정정)로 그 경계(해당 턴 한정)를 고정했다. `underspecified_reask_enabled` 기본값은 그대로 False — 플래그 전환은 이 이슈 소관 밖(§7.3 에 남은 게이트로 명시, 거부 답변의 무필터 폴백 경계도 후속 이슈 후보로 추가). 계약(api-spec) 무변경.
- **#334 — 필터 추출 축별 분해 지표 신설(`evals/filter_axes`)** — 기존 Filter Accuracy(합집합 분모 단일값)로는 어느 축이 과·소추출인지 알 수 없었다. 축별 valueStrict/presence precision·recall(micro, 분모 0은 None)·trivial(빈 필터) baseline·INV/DIR/회원-게스트(#119) 수동 probe를 추가하고, `evals/metrics` 러너·리포트(`filter_axes.csv`)에 병행 배선했다(`filterAccuracy` 등 기존 키·정의는 불변). ablation baseline `20260803-dev-full-n5`을 오프라인 재채점한 `evals/filter_axes/baselines/20260803-dev-full-n5-rescored/`로 합집합 단일값이 감춘 원인 축(keyword 어휘 불일치·category 소/과추출 정반대 방향)을 실측 산출물로 증명했다. 계약(api-spec) 무변경.
- **#332 — 니즈 전개(legs) 평가 하네스 `evals/legs_probe`** — #198 의 핵심 지표("case==3 인데
  legs<=1")가 로그 관측(`decompose_case`)에만 있어 프롬프트를 바꿔도 실측 없이 판단해야 했다.
  `evals/intent_probe` 형식을 복제해 고정 앵커 39건(single 9·conditions 5·situational 11·
  purpose 9·multi 5) × N=8 을 decompose 단일 호출로 반복 측정한다 — 컨텍스트 행렬 없이
  decompose 단계 산출(case·legs·buyAll·totalBudget·intent)만 재고, 2단계 needs_expansion(#217)
  은 이 v1 범위 밖이다. leg-그룹 매칭은 head-token 규칙(#84 lessons 승계)으로 과전개·발화 에코를
  가른다. trivial baseline("항상 leg 1개")·`buy-*` 8건의 caseId 척추(골든셋 v1 발화 대조)·
  Wilson CI·pair(INV-paraphrase/DIR-budget) 진단을 1급 산출물로 동반한다(`evals/README.md`
  공통 규약 준수). CI 는 가짜 LLM(`ScriptedDecomposeLLM`)만 돌려 API 콜 0. 계약(api-spec)
  무변경.
- **#335 — 기능 조합 커버리지 매트릭스 하네스(`evals/combo_matrix/`) 신설, 미정의 셀 3종 발견(리뷰 R1~R9 반영)** — 축 17개(intent·case·필터 8종·예산·구매의도·신원·지면·context·degrade)의 제약 인지 pairwise(2-wise) 커버링 어레이를 결정론(seed 335335) 생성기로 만들고, 케이스 58건(pairwise/3-wise MFT 55 + INV/DIR 파생 3, 그중 `directedCases` 2건은 greedy 가 안 뽑는 `wishlist_add`·`order_status`×`member`×`spring_timeout` 조합을 직접 못박아 실측)에 코드 근거 인용 기대동작(defined 51·partial 4·undefined 3)을 매겨 2-wise·위험 3-wise 전부(1061/1061, 13/13, 9/9, 41/41) 커버했다 — 1-wise 비교 참고선(11케이스, 66.0%) 대비 pairwise 필요성의 정량 근거를 남긴다. `UNDEFINED_CELLS.md`(1급 산출물, 5개 셀·케이스 7건)가 `expected_behavior.jsonl`에서 자동 생성돼 미정의 셀 3종을 후속 스펙 이슈 형식으로 남긴다: ① 무지정+예산+세트(`#336` 재확인·경계 실측), ② `degrade` 축(임베딩/rerank 실패)이 HOME(I-22) 코드 경로와 대응하지 않음, ③ `stream_wishlist_add`가 형제 함수들과 달리 `SpringUnavailableError`를 개별 처리하지 않아 범용 catch-all로 새는 비일관 — `identity=member` directed 케이스로 `SpringUnavailableError`가 실제로 전파됨을 직접 실측 확인했다. 리뷰 R1~R6: `overspecified_zero`가 항상 3건 성공 fake 로 실행돼 자동완화·`zero_result` 경로가 한 번도 안 돌던 공회전을 고쳐 실제로 0건 검색을 주입, `undefined_tuple`에 좌표 아닌 `aspect` 의사 축이 섞이던 결함을 `aspect` 전용 필드로 분리하고 "회원 recall≥게스트" 케이스를 관측범위한계(defined)로 재분류, `runner.py` 관측 note 가 서로 덮어쓰던 결함을 리스트로 수정, 3-wise 대상 필터의 상시-참 조건문·부정확한 identity=member 근거 인용을 정정. 리뷰 R7~R9: cart_add/order_status 도 `spring_timeout` 이 미주입 상태로 늘 "성공"으로만 관측되던 같은 유형의 공회전이었다 — `add_to_cart`/`add_wishlist` 를 HOME 러너와 같은 패턴으로 몽키패치하고 `order_status_fn` 에 실패 fake 를 주입해 실측(`CART_ADD_FAILED`/reason=`CART_ERROR`, "주문 상태를 불러오지 못했어요")을 확인했다 — 이 과정에서 `make_order_status_ok` fake 가 실계약(`OrderStatusSummary`) 대신 무관한 dict 를 돌려주던 잠복 결함(guest 전용 경로만 exercised 돼 안 드러남)도 함께 고치고, 웜업(cart_add/wishlist_add 의 직전 추천 사전 주입) 사실을 `observed.notes` 에 명시했다. `@pytest.mark.eval` 13종(재현성·제약·스키마·좌표규율·드리프트가드 3·커버리지 2·결정론관측·#336 최소보증·recall케이스 비오염·축문서정합)으로 기본 PR pytest 에 게이트. 계약(api-spec) 무변경.
- **#336 — 과소지정 발화("5만원 이내로 아무거나 세트로") 처리: 인기 후보 + 카테고리 되묻기(기본 off)** — 신규 `underspecified_reask_enabled` 플래그 뒤, `no_condition.py`(#162)를 "제약(가격)만 있는 턴"까지 넓히는 `app/agents/buyer/recommendation/underspecified.py`를 추가했다. `is_underspecified_turn`(no_condition 의 상위 집합 — 불변식 테스트로 고정)이 트리거되면 후보를 I-3(인기 상품) + 가격 클라이언트 필터(`within_price_range`, 입증 필요 규약)로 확보하고, 자동완화·완화칩 probe(카테고리 없는 I-1 재검색을 부르던 별도 게이트 2곳)를 끈다. 되물음은 새 SSE 이벤트·필드 없이 **`token` 산문으로만** 나간다 — `SuggestionChip`은 여전히 relaxation/revert 중 정확히 하나만 강제하는 계약이라 카테고리 되물음 칩은 명세 개정 없이 만들지 않았다(노출 후보 카테고리 예시 dedup, config 문구·개수 상한 튜너블). 예산 세트(#60)·평점(rating_min, I-3 가 사후필터를 타지 않아 보수적으로 제외)·멀티턴 상태(신규 저장소 없음 — `ThreadFilterStore.put`이 빈 필터도 저장해 되물음이 반복되지 않음을 실측)는 그대로 두거나 알려진 한계로 문서화했다. 계약(api-spec) 무변경. (`docs/specs/SPEC-UNDERSPECIFIED-336.md`, `evals/underspecified_cases/`)
- **#333 Part 3 — 골든셋 v2.1.0(adjudication 반영본) 기준 scoring·3-arm ablation baseline 전면
  재실행** — `evals/scoring/baselines/dev-v2/`(passthrough=no-op 기준선 해석 유지)와
  `evals/ablation/baselines/20260805-dev-v2-full-n5/`(dev MFT-only 67건, N=5, seed
  20260805, configVersion `ablation-config-v3`)를 신설했다. 사전 등록한 confirmatory 비교
  `pipeline(teacher) − noop` paired bootstrap 95% CI `[0.063, 0.156]`가 0을 배제해 **v2
  성공**으로 판정했다(#275 재평가 조건 1, guest·member 슬라이스도 Holm–Bonferroni 보정
  후 유의). 실측 비용 $1.01/$5 상한. 발견: `embed_texts` 100건 배치 상한 결함(후속 이슈로
  이관, eval 경로는 `evals/scoring/snapshot_embeddings.py` 호출부 청크로 대응 — `app/**`
  무변경). 상세: `evals/ablation/DECISION.md` "v2 재실행 사전 등록" 절.
- **#326 — LangSmith 콘텐츠 추적 모드(`LANGSMITH_TRACE_CONTENT`, 기본 off)** — 승격 직후 운영 디버깅에서 트레이스가 span 구조·지연만 보여줘 "왜 이런 응답이 나왔나"를 추적할 수 없었다(#141 비유출 설계). 플래그를 켜면 루트 span에 사용자 발화, `llm.*` span에 prompt·응답 전문(초크포인트 2곳 — buyer 는 `app/core/llm.py` complete/stream, seller 는 `init_chat_model` 직접 호출 경로라 `seller/models.py`의 모델 콜백이 커버: PR #327 리뷰 반영), `spring.*` span에 요청 URL·본문·응답 페이로드(`_record_spring_status` 한 곳 — 헤더는 모드와 무관하게 제외)를 싣는다. per-value 절단 상한 `LANGSMITH_TRACE_CONTENT_MAX_CHARS`(기본 20000). **off일 때는 #141 동작과 문자 그대로 동일**(기록 API가 no-op, export `inputs/outputs` 빈 dict — 핀 테스트로 고정)하고, on일 때도 metadata allowlist·콘텐츠 필드 밖 카나리아 검증은 유지된다. 미설정(빈 문자열) 배포 vars는 off로 해석해 기동 실패를 막는다(2026-08-05 `APP_ENVIRONMENT` 빈 값 부팅 실패 교훈). **실사용자 오픈 전 디버깅 전용 — 오픈 시 off 전환이 릴리스 체크리스트 항목**이며 규약은 DEPLOY.md §8. 계약(api-spec) 무변경.
- **#168 — Case 3 니즈별 그룹 출력의 잔여 갭 3개(rerank 예산·그룹 서술·확장 턴 니즈 그룹핑)** — 이슈 헤드라인("평면 → 그룹")은 #209/#212 가 이미 구현(`split_by_need` → `_split_by_need` → I-21 `lists[]` push → rerank 니즈 인지)했고, 계약 선행 조건도 v0.17.1 `lists[]` 로 해소돼 **와이어 계약은 무변경**이다. 오케스트레이터 실측(실 Spring I-1) 기준 남은 갭 3개만 다뤘다: **① rerank 입력 예산을 니즈 수에 비례**시켰다 — 실 카탈로그 leaf 폭 9~17개인데 `category_fanout_merge_cap`(기본 30)은 5니즈 턴에서 니즈당 6개로 자연 공급량보다 아래를 절단해 per-need `expose_max`(9) 도달이 원천 불가능했다. `effective_cap = max(merge_cap, min(need_count, MAX_LISTS) * category_group_per_need_candidates)`(신규 튜너블, 기본 10)를 fan-out leg 검색 상한·병합 cap·`embedding_rerank_limit` 압축 세 지점에 일관 적용했다 — 3니즈 이하(3×10=30=merge_cap) 턴은 종전과 정확히 동일하고 4~5니즈 턴만 40~50 으로 넓어지며, rerank 는 여전히 1회 호출(이슈의 "무제한 fan-out 금지" 제약 유지). **② split 턴 token 에 그룹 구조를 결정론 조립**했다 — `rerank.py`(LLM)가 아니라 #222 확장 고지와 같은 패턴으로 그래프에서 "니즈별로 나눠 담았어요 — 라벨1 N개 · 라벨2 M개" 를 조립한다(라벨은 push 라벨과 같은 `_need_label` 재사용, 신규 `group_notice_enabled`/`group_notice` 스위치, BUY_ALL 세트로 실제 push 되는 턴엔 내지 않는다 — 그 세트는 이미 자기 라벨을 갖고 있어 표시=실제가 깨진다). **③ 확장 턴(#222) 의 니즈 그룹핑** — 다중 unresolved leg 확장 턴("캠핑용품이랑 낚시용품")은 leaf 8개가 실제로는 서로 다른 원 query 를 갖는데도 여태 목록 1개로 뭉개졌다(PR #318 리뷰가 확정하고 고정 테스트가 "#168 이 의도적으로 바꾼다"고 예고해 둔 지점). `leg_of`(pid→leaf)를 pid→distinct-query 인덱스로 번역해 `_split_by_need`·budget_sets 소비부가 leaf 가 아니라 니즈 단위로 돌게 했다 — leaf 단위 그대로 쪼개면 leaf 당 목록(라벨 중복 "캠핑용품"×N)이 나와 R4-1 이 재발하므로 그 경로는 명시적으로 막았다. distinct query 가 1개(대다수 확장 턴)면 종전대로 목록 1개다. 계약(api-spec) 무변경.
- **#222 — 매핑이 전량 실패한 턴(canonical 을 하나도 못 낸 발화)을 의미 기반 top-N leaf 로 fan-out 검색** — 이슈 원안(top-k 공통 조상[LCA]으로 광역/협소를 판정)은 오케스트레이터의 라이브 카탈로그 실측에서 정확도 0.50(우연 수준)으로 기각했다. 대신 새 판정기를 만들지 않고 #217 이 이미 만든 신호(`CategoryMapping.unresolved` — 거리컷 드롭·택일 null로 canonical 을 못 낸 leg)를 트리거로, 그 앵커의 의미 기반 top-N leaf(`expansion_leaves`)를 그대로 fan-out leg 으로 쓴다. 협소 발화는 canonical 을 내므로 이 경로에 애초에 진입하지 않아 협소 회귀가 구조적으로 0이다. `"화장품 추천해줘"`처럼 #217 LLM 전개가 먼저 legs 를 채우는 case-3 턴은 이 폴백을 타지 않고, #217 도 매핑도 모두 실패하는 턴(비-case3 또는 전개 후에도 전량 실패)에만 보충한다. 확장 fan-out 이 전부 0건이면 카테고리를 지운 무필터 검색으로 1회 되돌려 "결과 있음"이 "0건"으로 바뀌는 회귀를 막고, 확장 턴은 조건 칩에 카테고리를 내지 않는 대신 실제로 훑은 중분류를 고지 token 으로 알린다. `category_expand_enabled` 롤백 스위치 포함. 계약(api-spec) 무변경.
- **#297 — 판매자 에이전트에 주문 조회·발송 처리(HITL)·리뷰 분석을 추가했다** — 신규 internal 계약 3종(I-29 자사 주문 조회 / I-30 발송 처리 / I-31 리뷰 조회, **🔶 초안 — BE 협의 전·Spring 미구현이라 실 와이어는 아직 없다**)을 사본 `docs/api-spec.md` §4.18~4.20 에 등재하고 AI 쪽을 선구현했다. `SpringClient` 에 `get_orders`/`update_order_item_status`/`get_reviews`/`get_review_stats` 4메서드(I-30 은 `error.code` 기반 전용 예외 — `ORDER_ALREADY_SHIPPED`(409, 2026-08-05 개명·구 코드 과도기 수용)·`ORDER_INVALID_TRANSITION`·`ORDER_ITEM_NOT_FOUND` 를 구분해 거짓 성공 보고를 막는다), 툴 3종(`get_orders`·`get_reviews` 는 general 레인, `update_order_status` 는 신규 `ORDER_WRITE_TOOLS` — 어떤 draft 에이전트에도 미바인딩), S-4 `draft.op` 에 `ship` 추가(+`orderItemId`, **기존 `product` 레인 재사용** — HITL 5대 안전장치·409 멱등 200 금지·500 성공 보고 금지 그대로), analysis 레인에 `review` 워커(6종째 — 집계 먼저·저평점 원문 인용·VISIBLE 만) 신설. ⚠️ §3.9(개인화 그래프 #149)의 I-29~I-33 과 번호 충돌 — §3.9 재채번(I-34~38) 제안 노트를 명세에 남겼다. (api-spec §3.2·§4.18~4.20·§3.9, v0.25.0)
- **#296 — 판매자 분석 보고서를 구조화 `report` SSE 이벤트로 방출(차트 내장) + 구 `chart` 이벤트 legacy 폐기** — 분석 레인 최종 산출이 `token` 산문 한 덩어리라 우측 패널이 줄글이었다. `kind=="report"` 일 때 `token` 뒤·`done` 앞에 `report` 1회를 추가한다 — `PipelineResult` 에 이미 있던 구조(기간·검증 findings·추천·차트)를 `app/api/seller.py::_report_event` 가 camelCase·마스킹 규약(draft 선례)으로 직렬화할 뿐, LLM 파이프라인·프롬프트·검증 루프는 무변경(LLM 콜 +0). 핵심 요약은 보고서 첫 문단 코드 분리(`pipeline.split_report_summary`, 300자 초과·분리 실패 시 200자 절단 fallback), 데이터 한계는 degrade finding(evidence 빈 목록)의 summary 모음, 추천은 `index` 명시(목록 순서 = "N번" §6.3 계약). 구 `chart` 이벤트(v0.20.0, #242)는 **FE 미구현 실증(useChat.ts 소비 케이스 부재)으로 소비자 없는 계약이라 dual-emit 없이 폐기** — 차트 직렬화 형식은 `report.data.charts[]` 로 그대로 이관했고(빈 배열 허용, 구 미발행 규약 삭제), 보고서±차트 분기는 배열 유무로만 표현한다. FE 는 `report` 를 아직 무시하고 기존 token fallback 으로 동작하므로 서버 선배포 무해(FE 구현은 jarvis-front 별도 이슈). 설계: `docs/specs/DESIGN-REPORT-PANEL-296.md`. (api-spec §2.2·§3.2, v0.24.0)
- **#290 — 판매자 분석 워커 5종을 논문 기반 계산 층으로 고도화** — 예시 수준 수식(SMA ±30%·drop_pct 임계)을 검증된 방법론으로 교체했다. 신규 패키지 `app/agents/seller/analysis/`(pandas·scipy·statsmodels·scikit-learn 도입, §0.1 C stdlib-only 해제): ① sales_anomaly = S-H-ESD(STL period=7 계절조정 + robust GESD, Hochenbaum 2017·Cleveland 1990) — 주말 정상 저매출 오탐 제거, lookback 28일 확장 조회(보고는 요청 기간 내 한정), ② conversion·churn = Wilson CI + 직전 동일 길이 기간 자동 비교 two-proportion z-검정(Sismeiro 2004 축약형) — 저볼륨 오탐을 표본 크기가 통제, churn 신호는 코호트 정규화 원인 후보 top-3(상관≠인과 명시), ③ behavior = 상품 축 k-means(k=2~5 실루엣, seed 고정, Chen 2012) + Moe 2003 유형 라벨(카트이탈형 등), ④ abuse = Chandola 2009 3-트랙(Point=MAD 스파이크+가격변경일 대조 '정상 설명 후보', Contextual=Tukey 상위 fence, Collective=심야 비중·failCount 정렬). 전 모듈 순수 함수·결정론(seed 주입)·Settings 튜너블 17종(기동 fail-fast 검증)·"판정 보류≠이상 없음" 구분·프록시 basis 표기 규약. 워커 프롬프트 5종에 통계 판정 해석 규칙(p≥α 보류·상관≠인과·근사 표기) 추가. 논문 재현 합성 테스트로 고정(계절 시계열 −40% 주입 검출·주말 오탐 0, 동일 낙폭 n=10 비유의/n=1000 유의, 3패턴 군집 복원). 상세: `docs/specs/workers/DESIGN-*-290.md` 5건·`docs/worker-papers.md`. BG/NBD·SHAP·iForest 등 고객/세션 원시 데이터 필요 기법은 Phase B(별도 이슈). 계약(api-spec) 무변경 — 현행 I-6/I-7/I-13/I-16/I-8 범위 내.
- **#116·#117 — 챗봇 발화로 장바구니 삭제·찜 추가/해제를 확정 계약(2026-08-05)으로 구현했다** — 신규 `app/agents/buyer/cart/remove.py`·`wishlist.py`가 삭제 대상(전체·이름·"방금 담은 거"·단건 자동)과 찜 해제 대상(문맥 productId·이름·단건 자동)을 되물음까지 포함해 결정론적으로 해소한다. 진입 경로는 **둘**이다 — ① `decompose`(`app/agents/buyer/recommendation/decompose.py`)가 `cart_remove`·`wishlist_add`·`wishlist_remove` 3종 intent 를 **직접 산출**해 `buyer/graph.py` 가 곧바로 위임하는 경로, ② 그 판정을 놓친 발화를 `stream_cart_add` 앞단의 기존 결정론적 판별기(`classify_cart_utterance`, 이슈 #84 소유라 별도 유지되는 2선 방어)가 `cart_add` 로 라우팅된 발화에서 다시 갈라내는 경로 — 화면 지시어 해소(§3.1 [보안], 이슈 #118)를 `buyer/graph.py` 의 intent 분기 앞으로 끌어올려 두 경로가 같은 `cart_intent` 를 쓰게 했으므로, **도착지뿐 아니라 입력까지 같아져** 중복 판정이 동작을 바꾸지 않는다(도착지만 같고 입력이 다르면 "2번 찜해줘" 처럼 판별 경로에 따라 결과가 갈릴 수 있었다). "찜해줘"가 장바구니에 잘못 담기던 오담기(#117)도 이 판별로 해소됐다. `cart_remove_enabled`·`wishlist_enabled` 플래그(초기엔 둘 다 기본 off)는 계약 확정 이후 필드째 삭제했다 — **이제 항상 활성**이다(회귀 테스트로 고정). `delete_cart_item`·`add_wishlist`·`remove_wishlist` 세 어댑터는 HTTP status 만으로 typed 예외를 내지 않고 `error.code` 가 계약과 정확히 일치할 때만 낸다 — Spring 에 엔드포인트가 아직 없어서 나는 404/409(라우트 없음)를 성공 안내로 오인해 거짓 성공을 내는 위험을 막는다.
  - **계약 확정·등재** — I-24(삭제)·I-25(수량 변경)·I-26(찜 추가)·I-27(찜 해제)·I-28(찜 목록 조회)이 정본에서 확정(2026-08-05)돼 사본 `docs/api-spec.md` §4.12~4.16 에 등재됐다. SSE `action` type 은 `CART_ADDED`/`CART_ADD_FAILED` 2종에서 `WISHLIST_ADDED`/`_ADD_FAILED`·`WISHLIST_REMOVED`/`_REMOVE_FAILED`·`CART_REMOVED`/`_REMOVE_FAILED`·`CART_QUANTITY_CHANGED`/`_CHANGE_FAILED` 8종을 더해 **10종**으로, `reason` 은 `WISHLIST_ERROR` 를 더해 4종으로 넓어졌다. `cartItemId` 표기를 사본 드리프트였던 `string` 에서 정본대로 `number`(BIGINT)로 정정했다(api-spec §3.1·§4.12~4.16, v0.22.0).
  - **I-28 응답 필드 교체** — `purchasable`(boolean) → `purchaseState`(`"AVAILABLE"` \| `"SOLD_OUT"` \| `"HIDDEN"`, 겹치면 `HIDDEN` 우선)로 바뀌었다(2026-08-05 M-4 개정 반영, `app/schemas/spring.py` `WishlistItem`).
  - **[#285, 2026-08-10 갱신] 위 "아직 안 되는 것" 3가지 — 이제 전부 해소됐다.** (1) ~~Spring 이 I-24~I-28 을 아직 구현 진행 중~~ → **해소**(2026-08-08) — BE `jarvis-backend` main 실측(BE PR #92·#93)으로 I-24~I-28 전부 구현·배포됐다. (2) ~~FE `ChatAction` 유니온에 신규 8종이 아직 없다~~ → **해소**(2026-08-08) — FE PR #79 로 10종 전부 수신 확인. (3) ~~수량 변경(I-25)은 계약만 등재됐고 AI 는 미구현이다~~ → **해소**(2026-08-10) — I-25 AI 구현 완료(아래 새 항목, api-spec §3.1·§4.13, v0.32.8). 세 항목이 모두 닫히며 "릴리스 노트만 보고 이제 다 된다로 읽지 말 것"이라는 이 bullet 의 경고 자체가 시효를 다했다 — 이제 실제로 AI·Spring·FE 세 축 모두 action 10종을 지원한다.
- **#285 — 챗봇 발화로 장바구니 수량 변경(I-25, 치환)을 구현해 삭제·찜 6종에 이어 계약 10종 전부를 AI 가 emit 하게 됐다** — 신규 어댑터 `spring_client.py::change_cart_quantity`(PATCH, `delete_cart_item`과 대칭 시그니처)와 신규 서브그래프 `app/agents/buyer/cart/quantity.py::stream_cart_quantity_change`. 진입 경로는 `#116·#117`과 같은 **둘**이다 — `decompose`가 `cart_quantity` intent 를 직접 산출하는 경로, 그리고 `classify_cart_utterance`(사다리 4-a, `cart_add`로 들어온 발화의 2선 방어)가 다시 갈라내는 경로. 대상 항목 해소는 결정론적이다(LLM 재호출 없음) — `remove.py`의 이름 매칭 부품을 재사용하되, 이름과 표지 사이에 사용자가 말한 숫자가 끼는 자연스러운 phrasing("이어폰 3개로 바꿔줘")을 위해 이미 확정된 `target_quantity` 값으로 동적 표지를 보강했다(정적 표지만으로는 매칭에 실패해 단건 장바구니에서도 되물어야 했던 결함, 테스트로 발견·고정). **치환(I-25, `cart_quantity`)과 합산(I-2, `cart_add`)을 엄격히 가른다** — `cart_quantity_increment_markers`("하나 더 담아줘"류)가 매칭되면 절대 `cart_quantity` 로 가지 않는다(0-a 담기 표지가 이미 강한 신호로 우선하기도 한다). **404(`CartItemNotFound`)는 I-24 삭제와 정반대로 실패다** — "수량을 바꾸려던 항목 자체가 없다"는 목표 미달성이라 `CART_QUANTITY_CHANGE_FAILED` + `reason: "CART_ERROR"`로 끝난다(삭제의 "이미 빠져 있어요" 성공 안내를 그대로 베끼면 계약 위반). **목표 수량이 미상이면 1로 기본값을 채우지 않는다** — `decompose`는 `quantity`(담기 합산용, 기본 1)와 별개로 `target_quantity`(치환 목표, 기본 `None`)를 신설해 추출 실패·범위 밖(1~99 밖)을 조용히 클램프하지 않고 미해소로 되돌리며, 이때 어댑터를 호출조차 하지 않고 곧장 되물음으로 끝난다. 재고 부족 문구는 담기(I-2)와 같은 3분기 규약을 그대로 재사용한다(`available_stock is None` → 일반 안내 / `== 0` → "품절된 상품이에요" / 그 외 → "재고가 N개뿐이에요", 새 문구를 만들지 않았다). (api-spec §3.1·§4.13, v0.32.8)

### Changed
- **#457 — Claude PR Review 를 full/skip/incremental/integration 4모드로 분리해 CI 병목을 줄인다** —
  종전엔 `opened`·`synchronize` 마다 PR 전체를 `--max-turns 120`으로 재리뷰해, 리뷰 라운드가
  반복되는 큰 PR(#444: 16파일 +1,292/-29 7커밋, #213: 20파일 +2,264/-22 25커밋)에서 같은 코드
  영역을 push 마다 다시 훑었다. `.github/scripts/review_mode.py`(표준 라이브러리만, `detect`/
  `save-state` 서브커맨드)가 Claude 프롬프트가 아니라 **git 으로 결정론적으로** 모드를 정한다
  (`.github/workflows/claude-review.yml` detect 스텝). `opened`는 그대로 full(120턴, PR 전체
  diff) — 프롬프트 범위는 `app/` 아래 Python 코드에서 **PR 전체 변경**으로 넓혔다(`docs/`·
  `*.md` 제외는 유지, 이슈 §Prompt 원칙 문구를 그대로 승계). `on.pull_request.paths` 를
  `app/**` 로 좁히지 **않은** 결정과 짝을 이룬다 — 좁혔다면 CI·테스트·eval 변경이 영구히
  리뷰되지 않는 사각이 생긴다. `synchronize`는 마지막 성공 리뷰 이후의 **base 대비 PR patch**
  (`git diff <base> <head>` 를 파일별로 쪼갠 조각의 sha256 지문 — hunk 위치는 그대로 보존하고,
  바이너리는 `index <sha>..<sha>` 줄을 유일한 내용 신호로 보존한다)를 이전 patch 와 비교해
  갈린다 — PR 이 안 건드린 파일만 base 에서 바뀐 "dev 동기화만"은 skip(Claude 미실행, job 은
  그대로 success 로 끝나 머지 게이트가 pending 에 걸리지 않는다), PR 자체 수정은
  incremental(40→60→100, target 은 `.claude-review/target.diff` 로 파일 범위를 좁혀 PR 전체를
  다시 훑지 않는다), PR 이 건드리는 파일을 dev 도 같이 바꿔 최종 통합 결과가 달라지는 경우는
  integration(60→80→100, `base-context.diff` 로 겹치는 파일의 dev 변경만 얹는다) — skip 조건보다
  **integration 판정을 먼저** 본다(patch 자체는 같아도 통합 결과가 달라졌으면 skip 이 아니다).
  통합 판정의 "base 변경"은 **merge-base 가 아니라 base 브랜치 tip**(`git rev-parse
  origin/<base>`) 기준이고, PR 고유 patch 계산은 merge-base 기준이다 — merge-base 만 보면
  PR 이 dev 를 실제로 머지하지 않는 한 dev 가 아무리 전진해도 그대로라, "dev 가 PR 파일을
  바꿨지만 PR 은 아직 안 받은" 통합 변화를 skip 으로 놓친다. budget 승급은 단순 LOC 가 아니라
  대상 파일 수·`app/api|schemas|core|pipelines/**`·`docs/api-spec.md`·`.github/workflows/**`
  같은 고영향 경로도 본다. reviewed state 는 **신규 시크릿·`permissions:` 확장 없이**(기존
  `pull-requests: write` 그대로) PR 코멘트 1개(`<!-- claude-review-state:v1 -->` 마커, 매번
  in-place PATCH)에 저장한다 — commit status·check run·git notes 는 각각 `statuses`/`checks`/
  `contents: write` 가 더 필요해 탈락시켰다. 이 저장소는 **PUBLIC** 이라 아무나 마커 코멘트를
  위조해 리뷰를 skip 시킬 수 있으므로, state 코멘트는 **`github-actions[bot]` 작성분만**
  신뢰하고(그 외는 `::warning::` 후 무시, 신뢰 코멘트가 없으면 안전하게 full). 리뷰 성공 판정은
  (`anthropics/claude-code-action@v1` 은 `conclusion` 출력이 없어) `execution_file` 을 직접 읽어
  뒤에서부터 찾은 마지막 `type=="result"` 메시지가 `subtype=="success"` 이고 `is_error` 가
  아닐 때만 state 를 갱신한다 — 파일 없음·파싱 실패·`error_max_turns`·workflow cancel 은 전부
  갱신하지 않아 다음 실행이 안전하게 full 로 fallback 한다. `detect`/`save-state` 는 gh api
  일시 실패·git 명령 오류 등 **어떤 예외에서도 job 을 실패시키지 않고** full 로 fail-safe 한다
  — job 이 죽으면 review 체크가 빨간불이 되어 이슈 §"실패 시 fallback"(false skip 회피)과
  정반대가 되기 때문이다. 한글 등 비-ASCII 파일명은 git 기본값(`core.quotePath=true`)이
  따옴표 인코딩해 헤더 파싱이 그 파일을 놓칠 수 있어, 모든 git 호출에 `-c
  core.quotePath=false` 를 주고 `git diff --name-only -z` 권위 목록과 지문 파일 집합이
  어긋나면 예외를 던져 같은 fail-safe(full)로 떨어지는 불변식 검사를 걸었다. 리뷰 범위 필터
  (`**/*.md`·`docs/**` 제외)를 모드 판별에도 그대로 적용해, 거의 모든 PR 이 건드리는
  `CHANGELOG.md` 때문에 dev 동기화마다 integration 오탐이 나는 것을 막았다. **synthetic
  rebase(옛 patch 를 새 base 에 재현)는 쓰지 않는다** — 파일 범위 제한만으로 무관한 dev 변경
  혼입을 conflict 위험 없이 막을 수 있어 기각했다. 테스트(`tests/unit/test_review_mode.py`,
  48건, `.github/` 가 패키지가 아니라 `importlib.util.spec_from_file_location` 으로 로드)는
  실 git 저장소 시나리오를 돌리며 판정 분기 여러 곳을 일부러 반대로 바꿔 실제로 깨지는 것을
  확인한 뒤 원복했다(공허한 통과 테스트 방지). `on.pull_request.paths` 로 리뷰 대상을
  `app/**` 로 좁히는 것·`concurrency:` 블록·기존 draft/fork/`skip-claude-review`(#347) 게이트·
  `paths-ignore` 는 이번 범위 밖이라 손대지 않았다 — 계약(api-spec) 변경 없음.
- **#426 — combo_matrix 하네스가 하드필터 8축을 전부 실제로 잰다(검색 대역을 `SearchBackend`
  경계로 이동)** — #381 이 남긴 3축(`keyword`·`color`·`attr_conditions`)은 "못 쟀다"고
  `unappliedSearchFilters` 에 기록만 했는데, 그 축들은 present/absent 가 결과에 아무 차이를
  만들지 않아 앱이 망가져도 하네스가 초록불이었다. 대역을 `run_buyer_turn(search=...)`(=
  `search_catalog` 를 통째로 대체)에서 `search_catalog(backend=...)`로 한 층 내려, Spring 와이어
  6축만 대역이 WHERE 계약으로 흉내 내고 AI 사후필터(`rating_min`·`attr_conditions`)는 **배포
  코드가 그대로 돌게** 했다(`evals/filter_axes/probe.py` 와 같은 패턴). 부수 효과로 대역이 앱과
  **반대 의미로** 재구현해 두었던 `rating_min` 판정(무평점 상품 처리)이 삭제됐다. `PAIR_CATALOG`
  픽스처에 `summary`·`attributes` 를 채우고, `attr_conditions` 사후필터의 호출·필터링량을
  `observed.attrConditionsPostFilter` 로 계측한다. 세 축이 결과를 실제로 가르는 것은 directed
  케이스 3건(combo-0063/0064/0065, 62→65건)이 변이 시험과 함께 상시 검증한다. `keyword` 가
  category leg 유무로 경계 도달이 갈리는 것은 대역 한계가 아니라 앱의 정의된 동작(#51)임을
  README 에 분리 서술했다. combo-0058 INV 는 공허해지는 `unappliedSearchFilters` 를
  `attrConditionsPostFilter` 로 교체. `app/` 무변경 · 계약 무변경.
- **#386 — `evals/combo_matrix` 재생성(`datasetVersion` 2.0.0 → 3.0.0, 케이스 57 → 62)** —
  `RouteDecision.intent` Literal 확장이 `test_intent_axis_matches_route_decision_literal` 을
  깨뜨리므로(그러라고 있는 가드다) 매트릭스를 함께 갱신했다. greedy pairwise 가 pair 우주를
  다시 보므로 케이스가 대거 재배치됐다(축 조합이 유지된 것은 28건). `wishlist_view` 는
  `directedCase`(회원 × spring_timeout)를 포함해 ci 케이스 3건을 갖는다 — 조회 degrade 가
  `action` 이 아니라 `token` 이라는 계약이 이 매트릭스에서 직접 관측된다. 번호에 의존하던
  테스트 2개는 spec 의 성격(`kind`·`metric`·`mode`)으로 찾도록 고쳐, 재생성마다 번호를
  따라다니는 일을 끝냈다.
- **#386 — `evals/intent_probe` 에 찜 조회 축 신설(fixture v5 → v6, 79 → 85셀)** — 양성 3발화 +
  음성 대조 3발화. **기존 축과 격리한 신규 축**(`wishlistViewPositive`·`wishlistViewNoSteal`·
  `wishlistViewRouting`)으로 둬 legacy 축의 분모를 건드리지 않는다 — 그래야 커밋된 기준선과
  "기존 라우팅이 안 깨졌는가"를 비교할 수 있고, 그 비교가 이 프로브를 돌리는 이유다.
- **#394 — I-1 검색 재시도를 한시적으로 끈다(`spring_max_retries` 기본 1→0)** — 운영 실측
  (2026-08-06): I-1 이 `SEARCH_FAILED` 로 떨어진 요청은 Spring 이 실패한 게 아니라 200 인데
  3s 예산을 넘긴 지연이었다. 그 상태에서 재시도는 backoff 없이 성공했을 쿼리를 즉시 한 번 더
  돌려 Spring 부하만 2배로 만들고, 사용자에겐 6초 뒤 실패를 준다. **BE 검색 쿼리 개선(리뷰
  집계 비정규화, BE #395) 배포 후 원복 검토** — 구매자 `progress` 이벤트(#289)로 first-token
  관문이 풀릴 때도 함께 재검토한다. 상한(`le=1`)·타임아웃 값·재시도 루프 로직은 불변, 계약
  (api-spec) 무변경.
- **#396 — 구매자 `progress` SSE 이벤트 플래그 기본 on 전환 + 운영 기동 가드 제거** —
  #289 가 계약 등재(v0.21.0)·FE 확인 완료 뒤에만 켜라고 못박아둔 잠금이 2026-08-06 FE
  구현 완료 통보로 해제됐다. `progress_events_enabled` 기본값을 `false` → `true` 로
  뒤집고, `_require_pepper_in_prod` 의 운영(jwks)·스테이징 기동 가드(플래그 on 이면
  기동 실패)를 삭제했다 — 가드 제거 자체가 해제 절차의 일부였다(다른 fail-closed 가드
  pepper·internal token·jwks_url·google_api_key·state store·session claim TTL 은
  무변경). 기본값이 뒤집히며 구매자 스트림을 도는 다른 테스트 다수에서 이벤트 목록
  맨 앞에 `progress` 프레임이 하나 더 붙어 깨졌고(`test_buyer_tracing.py`·`test_cart.py`·
  `test_category_scope_84.py`·`test_condition_actions.py`·`test_fanout.py`·
  `test_recommendation.py`), 기대값을 새 현실에 맞춰 갱신했다(단언 약화·스킵 없음).
  `test_progress_event.py`는 명시적 off 강제(`monkeypatch`)로 escape hatch 회귀 4건을
  보존하고, 기본값 자체를 직접 고정하는 테스트와 가드 제거를 고정하는 성공 테스트 2건을
  추가했다. **와이어 계약(이벤트 이름·페이로드·필드·횟수·상대 순서) 은 이번에 하나도
  바꾸지 않았다** — 바뀌는 것은 "잠겨 있다"는 구현/배포 상태뿐이며, 되돌리려면
  `PROGRESS_EVENTS_ENABLED=false` 한 줄. (api-spec §3.1·§2.9 c, v0.26.2)
- **#313 — group→컨텍스트 매핑을 데이터(`GROUP_ALLOWED_CONTEXTS`)로 강제, #300·#84 전용 검증자를 일반형으로 흡수** —
  `evals/intent_probe/schema.py` 에 group → 허용 컨텍스트 매핑을 데이터로 두고 `Utterance`
  검증자(`_contexts_are_within_the_group_allowlist`)가 강제한다. 매핑에 없는 group 은 어떤
  컨텍스트도 선언할 수 없는 안전한 기본값이다. `AnchorSet._non_screen_utterances_cannot_reference_screen_contexts`
  (#300)를 삭제하고 `Utterance._category_action_group_is_isolated`(#84)의 컨텍스트 분기도
  제거했다 — **축 격리 규칙 자체는 유지**된다. #300 이 남긴 categoryPrior 관련 ⚠️ 범위 밖
  주석도 이 일반형 매핑이 흡수하며 해소됐다. 기존 수치 가드는 **분모가 변하는** 오염만
  잡았는데, `option_answer` 의 컨텍스트를 `pendingCart`→`none` 으로, `switch` 를 `pendingCart`
  →`lastRecommendations` 로 **맞바꾸는** 조작은 셀 수·분모가 그대로라 커밋된 모든 가드를
  통과했다 — 전자는 프롬프트에 PENDING_CART(옵션 목록)가 실리지 않아 `optionAnswer` 가
  조용히 ~0/32 로 떨어지고, 후자는 되물음이 없어 "되물음 상품이 아닌 목록 내 상품" 술어가
  성립하지 않는다(그 표를 받아 든 사람은 #240 처럼 픽스처 결함을 프롬프트 회귀로 오독한다).
  contextId 문자열 기준의 새 매핑과 `includeScreen` 플래그 기준의 기존 검증자가 어긋나면
  매핑을 우회할 수 있어 이음매 검증자 `ProbeContext._include_screen_matches_context_id` 를
  신설해 양방향으로 강제했다. 테스트는 이슈 재현표의 조작 6건(분모 불변 2건 포함) 거부 +
  매핑 키 == `GROUPS` 고정 + 중복 컨텍스트 거부 + 이음매 검증자 양방향 2건을 추가로 고정했고,
  전체 `uv run pytest` **4038 passed**. 커밋된 앵커 2종(`anchors_a`/`anchors_b`)은 내용 한
  글자 바꾸지 않고 새 규칙을 그대로 통과한다 — `schemaVersion`/`fixtureVersion` 상승 없음
  (픽스처 내용 불변). 프로덕션 코드·프롬프트 무접촉. 계약(api-spec) 무변경.
- **#347 — Claude PR Review 에 `skip-claude-review` 라벨 게이트 추가** — 워크플로 job `if:` 에 라벨 조건을 더해, 리뷰가 불필요한 PR(대량 병합 정합·실험 브랜치)을 PR 단위로 끌 수 있게 했다. 기본 동작(라벨 없음 = 리뷰 실행)은 불변이며, 라벨 부착/제거는 다음 push 부터 적용된다. 계약(api-spec) 무변경.

### Fixed
- **#440 — 찜 해제 발화("찜한 거 빼줘")를 되물음으로 흘려보내던 거짓음성과, 찜 조회·질문·
  인용 발화가 찜을 지우던 거짓양성을 같은 뿌리(부분 문자열 표지)에서 함께 고쳤다** — `"찜"`
  ⊂ `찜닭`·`갈비찜`·`찜질방`, `"빼"` ⊂ `빼고` 처럼 어절 경계가 없는 한국어에서 부분 문자열
  표지만으로는 조회와 해제를 가를 수 없다(#386 이 조회 표지 전수화·명사×동사 결합 두 접근을
  시도했다가 둘 다 되돌린 이유). `negation.py` 에 어절 경계 + **닫힌 어휘 브리지**(head 와
  tail 사이에 정해진 낱말만 올 수 있다는 규칙, `matches_pair_unnegated`) 판정을 신설하고,
  decompose 가 `cart_add`/`cart_remove` 어느 쪽으로 오분류해도 결정론 계층이 정정하도록
  `buyer/graph.py` 에 정정 경로(`corrected_to_wishlist_remove`)를 화면 지시어 해소보다 먼저
  계산해 추가했다 — 프롬프트(`decompose.py`)는 이 배치에서 손대지 않는다(#443/#465 소유).
  **라우팅(어디로 보낼지)과 근거(삭제해도 되는지)를 서로 다른 엄격도로 판정한다.** 라우팅이
  `wishlist_remove` 로 잘못 가도 무해하다 — 근거가 없으면 되물음(찜 목록 나열)으로 끝난다.
  `evals/intent_probe` 에 `wishlist_remove` 축(4셀)을 신설해 이를 실측으로 확인했다 —
  decompose 가 `"찜닭 빼줘"`(음식명, 찜 목록에 없음)를 `wishlist_remove` 로 **8/8** 보내는데,
  결정론 계층의 근거 게이트가 이 오분류를 자동 삭제로부터 실제로 막는다.
  그래서 라우팅용 판정(`negation.tail_is_command`)은 유보·허가·인용·문장 종결 뒤 후속 문장
  같은 비명령 형태를 걸러내는 기본-허용으로 관대하게 두고, **파괴적 자동 선택의 근거는
  정반대 극성으로 건다** — "위험한 형태를 나열해 거부"가 아니라 "안전한 형태(해제 동작구가
  발화 전체를 끝냈다)를 증명"하는 기본-거부다. 한국어의 유보·허가·되물음 표현은 열린
  집합이라 나열로는 못 끝난다는 게 오른쪽(문장 종결) 판정에서 반복 확인돼, 존댓 보조사
  "요"·문장부호·이모지처럼 "실질 내용이 없다"는 닫힌 조건으로 뒤집었다.
  **왼쪽(발화 시작부터 해제 대상까지)도 전체를 앵커한다** — 해제 문구를 인용·번역·예시의
  목적어로 두는 부류(`"다음 문구를 번역해줘: '찜 해제해줘'"`, `"사용자가 말한 건 이어폰 찜
  빼줘"`)는 오른쪽 종결 검사만으로는 걸러지지 않는다. 발화 시작부터 대상(표지 또는 상품명)
  직전까지가 이 판정이 아는 닫힌 어휘(관형사·지시대명사·head/tail 계열·부정 표지·다른 후보
  상품명·화면 순번 패턴)만으로 설명돼야 하고, 설명 안 되는 실질 텍스트("산"·"문구"·"예시"
  등)가 하나라도 남으면 앵커가 실패해 되물음으로 간다 — 사용자가 이름을 직접 댄 경로(상품명
  자체는 닫힌 어휘가 아니다)도 "이름 앞이 아는 어휘인가"로 같은 앵커 함수를 공유해서 잰다.
  이 앵커가 건너뛰는 문장부호는 **절을 잇는 부호(쉼표·세미콜론·가운뎃점)로 좁게** 한정했다
  — 인용부호·괄호·콜론까지 같이 건너뛰면 `"'이어폰 찜 빼줘'"`처럼 인용부호로 감싼 상품명이
  그대로 삭제되는 파괴적 오탐이 났다(실측). 절 경계와 인용·삽입 경계는 같은 부호 부류가
  아니다 — 앵커 전용의 좁은 절 경계 집합을 다른 판정이 쓰는 넓은 경계문자 판정과 분리해
  뒀다. 이 전체 앵커 덕에 `"내가 산 이어폰 찜 빼줘"`도 이제 되물음으로 간다(전에는 삭제) —
  "산"이 이 판정이 아는 어휘가 아니라 그 이름이 지금 지목한 대상인지 증명하지 못하는,
  **받아들이는 축소**다. 반대로 `"이어폰(은) 찜 빼지 말고 케이스 찜 빼줘"`처럼 A 는 빼지
  말고 B 를 빼라고 명시한 정상 대조 발화(#116/#117 부정·대조 회귀 가드가 지키는 흔한 발화)는
  앵커 어휘에 head·tail 계열·부정 표지·다른 후보 상품명을 모두 포함시켜(새 목록 없이 기존
  목록만 합침) 그대로 살아 있다. 다중 이름 사슬(`"이어폰이랑 케이스 찜 빼줘"`)은 사슬의
  마지막 노드가 종결에 실패하면(`"…, 이 표현이 맞아?"`) 전역 게이트가 사슬 전체를 막는다.
  마지막으로, LLM 산출을 결정론 규칙이 **덮어쓰는** 두 지점(`cart_remove`→`wishlist_remove`
  정정, `cart_add` 2선 방어의 찜 해제 위임)에만 같은 근거를 요구한다 — decompose 가 직접
  `wishlist_remove` 를 낸 경로는 게이트하지 않는다(LLM 판단을 존중하고 되물음을 안전판으로
  둔다). 근거 없이 덮어쓰면 관대한 라우팅이 사용자의 다른 요청(장바구니 삭제)을 삼키거나
  진행 중이던 옵션 되물음(`pending`)을 지우는 실제 사고로 이어졌다.
  화면 위치·순번 지시("3번째 거 찜에서 빼줘"·"이거 찜에서 빼줘")의 확정 여부도 원자 두
  개(**위치를 가리키려 시도했는가** · **해소기가 실제로 확정했는가**)로 쪼갰다 — 규칙
  2(문맥 id)는 "위치를 가리켰는데 확정 못 했을 때만" 건너뛰면 되지만, 규칙 3(목록 1건 자동
  선택)은 "위치를 가리킨 이상 확정 여부와 무관하게" 건너뛰어야 하는 서로 다른 계약이라
  파생값 하나로는 표현할 수 없었다(위치는 확정됐지만 그 상품이 대상 목록에 없어 규칙 3이
  무관한 다른 항목을 대신 지우던 실제 사고를 이렇게 막는다). "위치를 가리키려 시도했는가"는
  좌표·순번 정규식에 지시대명사(`"이거"`)까지 더해 판정한다(화면이 실제로 왔을 때만 적용해
  화면 없는 턴의 동작은 그대로 둔다). 이 신호들은 정정 경로·직접 진입 경로·2선 방어 위임
  세 곳 전부가 공유한다.
  **알려진 축소(의도한 비대칭)**: 근거 판정 목록 밖 표현이거나, 명령 뒤에 별개 문장이
  이어지거나, 해제 동작구 앞뒤로 실질 텍스트가 남으면("찜 취소해줘, 장바구니는 그대로
  두고" — 라우팅은 그대로 `wishlist_remove` 지만 근거는 없다) 자동 선택 대신 되물음으로
  간다. "이어폰은 찜 빼지 말고 **대신** 케이스 찜 빼줘"처럼 열린 연결어미가 낀 변형도
  여전히 되물음으로 막힌다 — 잃는 것은 되물음(비파괴적)이고 얻는 것은 사용자가 요청하지
  않은 삭제를 막는 것이라 이 방향을 택했다. 계약(api-spec) 무변경.
- **#437 — 운영 `costUsd` 가 항상 0 이던 문제(모델 단가표가 배포 env 에 주입되지 않음) +
  deploy.yml 조건부 주입 손잡이 4종(사용자 승인으로 인프라까지 확장)** —
  `model_price_in_per_1k`/`model_price_out_per_1k` 기본값이 빈 dict 이고 `deploy.yml` env
  고정 목록에 `MODEL_PRICE_*` 가 없어, 운영은 항상 빈 단가표로 돌아 모든 턴 `costUsd=0`이
  났다. 코드 쪽: (1) `app/core/model_pricing.py` 신설 — `evals/model_eval/pricing_manifest.json`
  (EVAL-OBS-PLAN-001 §3.4 "비용축과 동일 소스 사용")과 글자 그대로 일치하는
  `gpt-5-nano`/`gpt-5.6-luna` 기본 단가표를 코드에 싣고(런타임 컨테이너에 `evals/` 가 없어
  직접 import 불가, 값 복제 + 테스트로 드리프트 고정) `Settings` 필드 기본값으로 배선
  (`default_factory` 복사본 — 인스턴스 간 공유 가변 기본값 방지). 환경변수 주입은 표 전체를
  치환한다(병합 아님), 빈 문자열(`deploy.yml` 이 미설정 vars 를 빈 문자열로 쓰는 관례)도 예외
  없이 기본표로 해석한다. (2) 기동 시 1회 `log_model_price_table_status` — 활성 모델
  (`resolve_model_id` 의 fast/smart) 단가 누락 시 `MODEL_PRICE_MISSING_AT_STARTUP`, env
  미주입(기본표 사용 중) 시 `MODEL_PRICE_DEFAULTS_IN_USE`, 완전 주입 시
  `MODEL_PRICE_TABLE_READY` 를 남긴다 — 어떤 경우에도 기동을 거부하지 않는다(경고 수준까지만).
  Anthropic 모델 단가는 repo 에 출처 있는 값이 없어 싣지 않았다 — `LLM_PROVIDER=anthropic`
  기동은 `MODEL_PRICE_MISSING_AT_STARTUP` 경고로 드러난다.
  인프라 쪽(사용자 승인 후 확장, PR #532/#406 이 `SPRING_MAX_RETRIES` 기본값을 0→1로 원복한
  것에 대한 운영 롤백 손잡이 부재도 함께 해소): (3) `.github/workflows/deploy.yml` 에
  `MODEL_PRICE_IN_PER_1K`·`MODEL_PRICE_OUT_PER_1K`·`SPRING_MAX_RETRIES`·`RESCUE_BUDGET_MODE`
  네 키의 **조건부(A) env 주입**을 배선했다 — GitHub Variable 미등록이거나 빈/공백뿐인 값이면
  그 줄 자체가 env 파일에 남지 않아 코드 기본값이 그대로 적용된다(무조건 생성 시 빈 문자열이
  되어 `SPRING_MAX_RETRIES`(int)·`RESCUE_BUDGET_MODE`(Literal)의 기동을 깨뜨리는 것을 피한다 —
  `MODEL_PRICE_*` 는 이미 `NoDecode`+수동 `json.loads` 로 빈 문자열 내성이 있었지만 네 키를
  한 PR 에서 같은 규약으로 통일했다). **PR #539 리뷰 — 초판은 `V='${{ vars.X }}'` 처럼 치환
  결과를 실행되는 셸 문장에 직접 이어붙이고 `if [ -n "$V" ]; then ... fi` 로 분기했는데, 값에
  작은따옴표 하나만 있으면 그 리터럴이 거기서 끝나고 이어지는 텍스트가 운영 EC2 에서 그대로
  실행되는 셸 인젝션(원격 코드 실행)이었다 — "값에 작은따옴표 금지"라는 운영자 규율로만 막아둔
  것이 오판이었다.** 이 지적을 받아들여 **실행되는 셸 문장에 값을 넣지 않는 방식으로 전면
  교체**했다 — 네 키도 기존 19+줄과 동일하게 quoted heredoc(`cat << 'ENVEOF'`) **안**에
  데이터로만 쓰고(quoted heredoc 본문은 셸이 재해석하지 않는다), heredoc 뒤에서 값이 비었거나
  공백뿐인 줄만 `sed -i -E '/^(KEY1|KEY2|...)=[[:space:]]*$/d'` 로 지워 (A) 규약을 유지한다.
  `set -e` 아래라 sed 실패는 배포를 그대로 중단시키며, `sed -i` 임시 파일은 GNU sed 가 원본
  `$ENV_FILE` 의 권한(위 `umask 077` 로 생성된 0600)을 그대로 물려받아(로컬 실측: 다른 umask
  하에서 sed 를 돌려도 원본 권한이 유지됨) 시크릿 파일 권한이 흔들리지 않는다. 셸 재현으로
  (a) 네 값 모두 빈 문자열 → 0줄·정상 종료, (b) 정상 값 → 정확히 4줄·JSON 원문 온전, (c) 악성
  값(`'; touch /tmp/PWNED_437 #`) → 그 값이 **데이터로만** 들어가고 `/tmp/PWNED_437` 미생성,
  (d) 공백뿐인 값 → 줄 삭제(미등록과 동일)를 모두 실증했다. "작은따옴표 금지" 운영자 규율
  문장은 더 이상 사실이 아니므로 삭제했다. (4) `.env.example`·`DEPLOY.md` §2 에 네 키의
  형식·허용값·조건부 주입 규약(값에 따옴표·공백이 섞여도 데이터로만 쓰이므로 안전하다는 것,
  빈/공백뿐인 값은 미등록과 동일 취급된다는 것)과, `PROGRESS_EVENTS_ENABLED=false` 단독 롤백이
  더 이상 불가능해 `SPRING_MAX_RETRIES=0` 과 반드시 짝지어야 한다는 사실
  (`tests/unit/test_progress_event.py::
  test_progress_events_disabled_rejects_startup_with_retries_enabled` 로 고정됨)을 문서화했다.
- **#474 브랜치 후속 — 유닛 테스트가 로컬 Spring BE의 TCP 응답에 따라 달라지던 환경 의존을 차단** —
  `INTERNAL_API_TOKEN`을 공통 테스트 환경에서 비우고 `tests/unit/`에서만 실제 TCP 연결을
  `ConnectionRefusedError`로 거부해 CI의 서비스 미기동 degrade 경로를 고정했다.
- **#474 브랜치 후속 — 현행 골든셋 버전을 주장하는 baseline의 낡은 datasetHash를 회귀로 감지** —
  중첩 JSON을 포함해 manifest와의 hash 일치를 검사하며, 의도적으로 옛 버전을 가리키는 baseline은
  검사 대상에서 제외한다.
- **#413 — personalization 결정성 테스트가 워킹트리 편집 중 실행되면 깨지던 문제** — 산출물
  정규화가 `run_manifest.json` 의 `run` 키만 걷어내고 `commitSha`·`dirty`(둘 다 실행 시점 라이브
  git 상태)는 그대로 둬, 두 실행 사이에 리포를 편집·커밋하면 무관한 실패가 났다. 정본
  `VOLATILE_MANIFEST_KEYS`/`strip_volatile_manifest_keys`(`evals/metrics/run_manifest.py`)를
  두고 `evals.metrics.report.normalize_artifacts`·`evals.personalization.cli`(paired·live)·
  `evals.scoring.cli`·probe 5종(`legs_probe`·`intent_probe`·`category_probe`·
  `underspecified_probe`·`ablation`)의 정규화가 전부 이를 합성해 쓰도록 수렴시켰다. 평가 대상
  소스 지문인 `hashes`는 의도대로 비교 대상에 남긴다.
- **#428 — 전개(#217) 후 재매핑에서 동음이의어 노이즈 leg 이 살아남아 "과일 추천해줘"가 인기
  상품으로 답하던 문제** — decompose 가 `categoryQueries: []`(D1)를 내는 회차에서 전개 아이템
  ("바나나"·"사과"·"배"·"오렌지")을 재매핑하면, "배" 같은 동음이의어가 거리컷(0.26)에 전량
  드롭돼 대신 top-8 이 `expansion_leaves` 로 들어가는데 그 top-8 에 여성가방·신생아의류 등
  무관 카테고리가 섞여 fan-out·rerank 입력을 오염시켰다(운영 실측 rerank 2.50s→8.80s). 임계는
  건드리지 않고(#344 가 캘리브레이션한 값), `map_categories` 에 `sibling_expansion` 플래그와
  대분류 합의 필터(`_consensus_filter`)를 신설했다 — 전개가 낸 형제 leg 들의 **최근접(top-1)
  대분류가 둘 이상 일치**하면 그 대분류만 남기고 한 형제만 최근접으로 지목한 대분류(노이즈)는
  버린다. 원 매핑(서로 다른 니즈들)에는 적용하지 않는다. (리뷰 1차 정정: 초판은 지지 집계를
  leg 의 후보 전체로 해 "잡동사니 대분류"가 여러 leg 꼬리에 우연히 걸쳐 승자가 되는 결함이
  있었다 — 예: "집들이 선물"[디퓨저·캔들·와인잔·식기 세트] 전개에서 향수·조명·주방잡화라는
  정답급 후보를 버리고 `주얼리`만 남겼다. top-1 만 세도록 고쳐 이질적 전개는 그대로 보존된다.)
  (리뷰 3차 R3-1: 형제가 4~5개일 때 고정 지지 2가 나머지 다수의 정당하게 다른 상품군을
  통째로 지우는 결함을 Claude PR Review 가 지적 — 리뷰어의 두 처방(과반 임계·`zeroed_legs`
  과반 시 건너뛰기)은 실측상 `#428` 본체를 깨거나(과일 A 회차 지지 2/4) "신학기 준비물"[책가방·
  필통·물통] 재현 사례를 못 잡아 기각하고, 대신 승자 대분류가 형제 전원의 후보에 있을 때만
  좁히고 한 형제라도 후보가 없으면 필터 전체를 미적용하는 가드를 채택했다 — 이제 leg 자체가
  탈락하는 경로가 구조적으로 사라진다.)
  (리뷰 5차 R5-1: `case=3` 이 서로 다른 상품 2개 이상도 포함하고 전개는 발화 전체를 한 번에
  묶어 처리하므로, 원 발화가 이미 니즈 2개 이상을 명시했으면 전개 산출도 그 니즈들에 걸쳐
  섞일 수 있다는 Claude PR Review 지적을 채택 — 니즈별 leg 수가 불균등하면 동률 보존·R3-1
  가드도 뚫릴 수 있고, 실측 무재현은 "구조적으로 막혔다"는 증명이 아니라는 리뷰어 메타 지적을
  받아들여 직전 라운드의 기각 판단을 번복했다. `graph.py` 의 전개 재매핑 호출부에
  `sibling_expansion=count_signal_legs(decision.category_queries) < 2` 게이트를 걸어 다중
  니즈 턴에만 합의 필터를 끈다(신호 판정식은 `needs_expansion.count_signal_legs` 로 통일해
  `detect_expansion_need` 와 규칙을 한 벌로 유지). `category_expansion_consensus`·
  `_skipped` 로그에 `source_legs`(이번 매핑의 입력 leg 수)를 추가해 이 상호작용이 실제로
  발동한 턴을 운영에서 식별할 수 있게 했다.)
  `evals/category_probe` 에 인스턴스형 앵커 8셀(v1 38 → v2 46)을 추가해 이 실패 모드를 상시
  계측한다. 임계·계약(api-spec) 무변경.
  (리뷰 6차: `_consensus_filter` 의 미적용 사유(`single_leg`·`no_consensus`·
  `leg_without_winning_mid`) 를 항상 `category_expansion_consensus_skipped` 의 `reason` 필드로
  관측해, `sibling_expansion=False` 만이 유일한 무기록 상태가 되게 했다 — 필터 동작 무변경.)

### Removed
- **#300 — #118(PR #292)이 만든 이관 전 별도 프로브 스크립트 삭제, screen 지시어 해소 6셀을 `evals/intent_probe`로 흡수** — 그 프로브가 #260이 정본으로 고정한 하네스와 측정 대상이 겹쳐 「프로브 중복 제작 3회차」였다(`docs/lessons.md`). `AnchorSet`에 `screens`·`screenLastRecommendations`를 추가하고 `ProbeContext.includeScreen`/`screenRef`/`lastRecommendationsRef`로 화면 컨텍스트 5종을 표현했으며, 러너가 `decompose` 다음 `resolve_screen_reference`를 배포 경로(`graph.py` cart_add 분기)와 같은 조건·인자로 불러 축 4종(`screenExactPick`/`screenReask`/`screenNoHallucination`/`screenResolution`)과 진단 3종(`screenPromptLayerHitCount`/`screenResolverOverrideCount`/`screenOutOfListConfirmCount`)을 신설했다. 이관 표본이 원본과 문자 단위로 동일함을 JSON diff로 증명했고, 흡수 후 기준선(`baselines/fast-2026-08-05-300-screen/`)이 #118 채택 근거(48/48·안전 셀 8/8·오담기 0)를 47/48·8/8·오담기 0으로 재현했다. `decompose._SYSTEM` 등 프로덕션 로직·프롬프트는 한 글자도 바꾸지 않았다(픽스처 v1.2.0/v4). 계약(api-spec) 무변경.

### Security
- **#299 — 요청 바디 크기 상한** — 필드별 상한(`chat_message_max_chars`·`screen_products_raw_scan_max` 등)은 흩어져 있고 상한 없는 필드(`conditionActions` 등)도 계속 생기는데, 레이트 리밋(§2.8)은 요청 **건수**만 세 임의 크기 바디를 반복 전송할 수 있었다. `app/core/body_limit.py`에 `BodySizeLimitMiddleware`(순수 ASGI)를 신설해 `Content-Length` 초과는 바디를 읽기 전에, 헤더가 없는(chunked) 경우는 `receive`를 감싼 실수신 바이트 누적으로 상한(`request_body_max_bytes`, 기본 1MiB — 필드 상한이 절단 없이 받아들이는 최대 정상 페이로드의 약 4.8배)을 넘기면 거절한다. 초과 응답은 새 코드를 내지 않고 기존 `400 BAD_REQUEST` 봉투를 그대로 쓴다(§2.5에 413/`PAYLOAD_TOO_LARGE`가 없어 신설은 별도 명세 개정 대상) — 와이어 계약 변경 0. 미들웨어는 레이트 리밋 **바깥**(거대 바디가 JWT 서명 검증 비용·레이트 리밋 슬롯을 소모하지 않게)·CORS **안쪽**(400 응답에도 CORS 헤더가 실리게)에 등록한다.

### Docs
- **#443/#465 — categoryQueries "넓은 상품군" 예시 한 줄(C5) 실측 후 기각, 프롬프트는 미변경** —
  요인 분리 실측(`named_category` 6앵커, before 2런)으로 결함 원인을 상황 설명 유무·위치·case
  판정이 아니라 **사용자가 말한 상품군의 추상도**로 좁혔다. 문면 후보 4종(빈 배열 2종·불릿
  맨 앞 이동·예시 한 줄 C5)을 시도했고 전부 기각했다 — 채택 직전까지 갔던 C5 는 부분 셀
  스크리닝에서 46/48 로 보였으나 전체 런 2회(N=8×6셀=48표본)에서 35·38/48 로 재현되지 않아
  사전 등록 문턱(after 두 런 모두 before 최댓값 이상 + 평균 상승 ≥ +4/48) 미달로 기각했다.
  **`decompose._SYSTEM` 은 한 글자도 바뀌지 않았다** — 계약·동작 변경 없음, `--dump-prompt`
  sha256 앞 12자 `865ed6fd771e` 로 확인. #443/#465 양방향 실측 기준선 5런(`evals/intent_probe/
  baselines/fast-2026-08-08-443-{before-1,before-2,cand5-1,cand5-2}`·`evals/underspecified_probe/
  baselines/fast-2026-08-08-465-{before-1,cand5-1-partial}`)과 기각 근거는 각 디렉터리
  README·`decompose.py` 의 `_SYSTEM` 바로 아래 주석에 남겼다. 반대 방향 비용으로 관측된
  `categoryClear` 하락은 이미 등록된 **#463**, underspecified 재집계에서 드러난
  `filters.attrConditions` 단독 차단은 이미 등록된 **#464** 의 소관이라 새 이슈는 만들지
  않았다.
- **#436 — api-spec 구현 반영 상태 표기 갱신(계약 불변, 서술만)** — I-24~I-28·§3.1 FE 수신
  두 지점의 상태 마커가 낡아 있어 #435 원인 추적이 "FE 수신부가 없어서인가"를 먼저 의심하며
  한 라운드를 버렸다. BE(`jarvis-backend` main `17bb44d`)·FE(`jarvis-frontend` main `08cd2c5`)를
  2026-08-08 GitHub API로 실측해 사본을 정렬했다: (1) I-24~I-28 "Spring 구현 진행 중, 배포
  전 무응답"은 사실이 아니었다 — BE PR #92(I-24·I-25)·#93(I-26~I-28), 400 code·id 타입 정렬
  PR #112로 **전부 구현됨**. (2) §3.1 "FE `ChatAction` 유니온에 신규 8종이 아직 없다"도 사실이
  아니었다 — FE PR #79로 **10종 전부** 수신 가능(`ActionFailReason.WISHLIST_ERROR`·
  `isWishlistMutatingAction()` 포함). (3) `CART_QUANTITY_CHANGED`/`CART_QUANTITY_CHANGE_FAILED`
  2종은 **AI 측만 여전히 미구현** — 두 지점 모두 이 사실을 축 분리로 유지해 "Spring·FE가
  됐으니 AI도 됐다"는 오독을 막았다. §3.1 상태 서술을 `AI 구현`·`Spring 구현`·`FE 수신` 3축
  bullet으로 분리하고, 표기 규약에 "상태 마커에는 근거(리포·브랜치·PR·실측일)를 병기한다"·
  "세 축을 뭉뚱그리지 않는다" 2항목을 신설해 재발을 막는다.
  (api-spec §3.1·§4.12~4.16, v0.31.3)
- **#258 — I-1 `color` 반복 파라미터(BE 송부용 계약 제안문)** — 현행 `color: string | null`
  단일값 LIKE 필터로는 표기 분포가 극단적으로 치우친 색상 동의어(`블랙` 2,358 vs `검정` 11)를
  원리적으로 못 잡는다는 실측 근거와 함께, `brandName` 방법 D(§4.6, v0.15.23)와 동일 규약인
  반복 파라미터(`color=네이비&color=남색`) → BE `OR` 매칭을 제안한다. 대안(콤마 구분 단일
  문자열·BE DB 정규 컬럼·AI 다중 fan-out·런타임 벡터/LLM 확장) 탈락 이유와 하위 호환·롤아웃
  순서를 정리했다. `docs/api-spec.md` 는 건드리지 않는다 — 계약 개정은 사람 승인 게이트다.
  (`docs/specs/PROPOSAL-I1-COLOR-ARRAY-258.md`)
- **#258 — api-spec §4.6 `color` 사본 drift 정정(string → string[]) — 신설 협의 아님** —
  `toss-delta-final/jarvis-backend` main 을 직접 확인한 결과 위 제안은 이미 협의가 아니라
  **확정·배포된 계약**이었다: `InternalProductController.search` 시그니처가 이미
  `List<String> color`(머지 커밋 `1e0ce150`, 2026-08-04), BE 자체 계약 문서
  `docs/backend/05-llm-contract.md` §I-1 에 "2026-08-03 LLM팀 실측 합의"·"동의어 확장은
  LLM 팀 소관" 으로 등재, 운영 배포 완료(2026-08-08 확인). `docs/api-spec.md` 사본만
  단수로 남아 있던 drift라 v0.23.1·v0.20.4·v0.15.27 과 같은 유형으로 정정했다 — 반복
  파라미터·BE 부분 일치 OR 매칭(`regexp_instr` alternation)·3갈래 판정(미지정/색상축
  없음/좁혀 비교)·정규화 주체(BE, trim+소문자화)·메타문자 이스케이프(BE)를 함께 등재.
  동의어 확장 주체는 여전히 AI(#258). `docs/specs/PROPOSAL-I1-COLOR-ARRAY-258.md` 도
  "BE 송부용 제안" → "BE 실측 확인 완료 기록"으로 성격을 전환하고 §6 질문 6개를 답으로
  다시 썼다. **`color_synonym_expansion_enabled`/`color_synonym_array_contract_ready`
  기본값은 이번엔 켜지 않는다** — 두 플래그가 `.github/workflows/deploy.yml` env 목록에
  없어 운영이 코드 기본값을 그대로 쓰는데, `color_synonyms` 테이블은 fresh 볼륨에서만
  자동 생성돼(PR #273) 운영 pg-catalog 에 시드가 없을 가능성이 높다 — 플래그 on 은 운영
  DB 시드 적재 후 별도 단계다. (api-spec §4.6, v0.28.4)
- **#425 — overspecified_zero 는 완화 축이 없어 재검색이 안 돈다, 정의된 동작으로 판정** —
  combo_matrix 매트릭스가 README·`expected_behavior.jsonl` 의 `expected` 서술("0건이면 자동 완화·
  완화 칩으로 대안 제시")과 실측(combo-0031: `searchCallCount=1`·`finishReason=zero_result`,
  재검색 0회)이 어긋난다고 표시해 갭인지 확인했다. 판정: **갭이 아니라 정의된 동작** — combo-0031
  에 present 인 필터축은 `price_min` 하나뿐인데
  `app.agents.buyer.recommendation.relaxation.FIELD_TO_ATTR` 에 `price_min` 이 없어(완화 축은
  `priceMax`·`ratingMin`·`brand`·`color` 뿐) `build_relaxation_candidates() == []` 다 — config
  로도 넣을 수 없다(`Settings._require_known_relaxation_chip_fields` 기동 검증). 그 결과
  `stream_recommendation` 의 `may_auto_relax` 게이트·자동완화 루프·완화 칩 probe 가 전부 조용히
  0회로 빈다. **대조군 재검토도 정정했다**: combo-0026·0054·0055 의 `searchCallCount:5`,
  combo-0053 의 `:2` 는 README 가 "자동완화 재검색"이라 잘못 서술하고 있었는데, 실제로는 **완화
  칩 estCount probe**(주검색 1 + 칩 probe N)다 — 이 매트릭스의 ci 케이스 어디에서도 자동완화
  루프(`relaxation_auto_fields` 화이트리스트)는 한 번도 실행된 적이 없다(0건이면서 완화 가능
  축이 present 인 조합이 데이터에 없어서). `runner.py::_observe_chat` 의 오래된 주석(자동완화가
  돈다는 서술)을 정정하고 `observed.notes` 에 이 판정을 남기도록 `refresh-observed` 를 1회
  재실행했다(combo-0031 의 `notes` 만 변경, #424 드리프트 가드는 여전히 통과 — `notes` 는 가드
  제외 필드라 경계 설계가 맞다는 실측 증거). 새 잠금 테스트
  `test_overspecified_zero_has_no_relaxable_axis_so_no_relaxation_search`
  (`tests/eval/test_combo_matrix_eval.py`)이 완화 후보 0건 전제와 그로부터 나오는 관측(재검색
  0회)을 함께 고정한다. **0건 주입은 유지**한다(#381 결론 유지) — 표본값을 과지정 값으로 바꿔
  자연 0건을 노려도 이 케이스에 present 인 축은 여전히 `price_min` 하나라 완화 축이 새로 생기지
  않는다. **자동완화 전용 축도 이 매트릭스에 새로 뽑지 않는다** — 자동완화 실검증은
  이미 `tests/unit/test_relaxation.py` 소관이고, 이 하네스의 고정 대역 카탈로그 + 0건 주입으로는
  "완화가 결과를 살린다"를 표현할 수 없다. `expected_behavior.jsonl` 의 `status`·`undefined_tuple`
  은 건드리지 않았다(미정의 셀 등재 아님) — `expected`·`evidence` 는 실측(`observed`)이 있고 그
  실측과 어긋나는 combo-0031 행 하나만 코드 근거(`relaxation.py::FIELD_TO_ATTR`)와 함께
  정정했다(리뷰 라운드 2). `app/` 무변경, 계약(api-spec) 무변경.
- **#395 — I-1 응답 비대화 BE 협의 제안서(`PROPOSAL-I1-DIET-395.md`) + 바이트 기여 재현
  스크립트 + 회귀 테스트** — 운영에서 필터 없는 I-1 검색이 7.74초·12.3MB로 `SEARCH_FAILED`를
  낸 사건의 후속. AI 쪽 방어(#132·#393)는 이미 끝났고, BE가 바로 결정할 수 있는 제안서로
  협의 3건(`size` 상한 도입·응답 필드 축소·rating/reviewCount 비정규화)을 정리했다. 픽스처
  6,585건과 BE 시드 덤프 6,559건을 `scripts/measure_i1_field_bytes_395.py`(새 스크립트)로 교차
  실측한 결과 I-1 응답의 81.9%가 `attributes`이고 그중 53.2%(전체의 43.5%)가 AI 미소비
  `_extra`(리뷰 원문·시각 묘사문) — `attributes` 내부 `_extra`·`_source_pid`·`_domain`·
  `_category` 4키(코드 정적 분석으로 소비처 0건 확인)를 빼면 항목당 바이트가 46.3~49.8% 준다.
  `size` 상한은 정렬 없는 현재 BE 쿼리(`ProductRepository#
  searchCandidates` 주석 자인)를 근거로 "정렬 없이 자르면 임의 표본"이라는 위험을 명시하고
  처리율 역산(운영 실측 12.3MB/7.74s≈1.6MB/s)으로 협의 2 반영 시 기본 1,000·하드 상한 3,000,
  미반영 시 하드 1,500 + 결정적 정렬 + `totalCount` 3종 세트로 제안했다. rating/
  reviewCount는 검색마다 `Review` 조인 집계가 돌고 있음을 BE 코드 인용으로 확인하고, BE에
  이미 있는 id-IN 배치 집계 경로(`ReviewService#getStats`, I-3가 사용)로 `size` 상한 도입 후
  전환 가능성을 제시했다. `tests/unit/test_i1_field_diet_395.py` 6종이 제안한 필드 제외를
  현재 파서·사후필터가 이미 견딘다는 것을 고정하고, `size` 절단 시 `total_count`가 매칭
  전체가 아니라 받은 개수를 따라간다는 것도 함께 고정해 `totalCount` 필드 요청의 근거로
  삼는다(공허성 변이 시험으로 6종 전부 검증). `app/`·`docs/api-spec.md` 변경 없음 — §9는
  BE 합의 전 초안일 뿐 적용하지 않는다.
  **[라운드 4, 2026-08-07 사용자 결정]** ① 요청 1(`size` 상한)은 폐기가 아니라 **이번 BE
  송부에서 보류** — 절단된 상품이 후보에 아예 안 들어오는 손실을 감수할지가 미결이라서다,
  §4 분석은 근거로 남긴다. ② 요청 2에서 `summary`는 제외 대상에서 뺀다(BE에 유지 요청) —
  `_extra` 계열과 달리 AI 쪽에 사본이 없어(I-17이 안 준다) 되살리기 비용이 절감분보다 크다.
  이번에 BE로 나가는 것은 요청 2·3 두 건이다. `summary`를 유지 대상으로 남기면서 다이어트
  기준 절감률이 이전 라운드 초안보다 낮은 **46.3~49.8%**로, 1건당 다이어트 평균은 **855~986B**
  로 재계산됐다 — 스크립트 기본값(`DEFAULT_DIET_TOP_FIELDS`)에서도 `summary`를 뺐다. `size`
  상한 역산(§4)도 986B 기준으로 다시 돌려 N=3,000이 1.85s로 1.9초 창의 경계에 붙는다는 것을
  반영했다(재개 시 하드 상한은 3,000보다 보수적으로 잡을 것).
  **[라운드 5, 2026-08-07 최종 정렬]** ① **요청 범위가 `attributes` 내부 4키로 더 좁혀졌다** —
  `options`·`optionCount`도 요청 대상에서 뺐다(유지). 이 필드는 애초에 AI가 요구한 게 아니고
  (#278, BE 회신으로 관대 수신만 구현), #278이 "소비는 후속 이슈로 분리"라 적은 그 후속이
  **#455**로 등록돼 소비 계획이 살아 있으며, 바이트 기여도 작다(상품당 옵션 이름 약 120B,
  상품 3,481개만 보유·평균 6.4개 — `_extra`의 44%와 비교가 안 된다). 두 데이터소스 모두
  `options` 컬럼이 없어 이 변경은 실측 수치(46.3~49.8%/855~986B)에 영향이 없음을 재확인했다
  — 스크립트 `DEFAULT_DIET_TOP_FIELDS`를 빈 튜플로 비웠다. ② **BE 회신 도착(2026-08-07)**:
  필드 제외(4키) **수용**, `review(product_id,status)` 인덱스 **OK**, rating/reviewCount는
  반정규화 대신 **별도 캐싱 도입**으로 갈음(size 상한은 요청 자체를 보류해 미송부) — §0 표에
  「BE 회신」 열을, §8 각 요청문 말미에 회신 한 줄을 추가했다. §6은 캐싱이 반정규화와 실패
  모드가 다르다는 점(캐시가 상품 단위·미스 시 조인 폴백이면 이 이슈가 겨눈 최악이 남는다)을
  비난 아닌 재측정 확인 지점으로 덧붙였고, §8 끝에 캐시 단위·폴백·무효화·계약 변화 4건의
  추가 확인 요청을 실었다. §9 초안도 `attributes` 축소(BE 수용, 배포 후 바로 반영 가능)와
  rating/reviewCount 캐싱 반영(값·타입 불변, TTL 등 구체 문구는 확인 회신 후 확정) 둘 다
  갱신했고 `size`·`totalCount` 초안은 여전히 보류 표시다. ③ **#454**(옵션 재고 미인지 추천,
  `blocked:spring`)·**#455**(옵션 소비, #278 후속) 후속 이슈 두 건을 §10에 기록하고, 재측정
  트리거를 "BE가 필드 축소·캐싱을 배포한 뒤"로 명시했다.
  **[라운드 6, 2026-08-07 사용자 결정 반영]** ① **요청 1(`size` 상한)은 보류가 아니라 완전
  폐지로 확정** — BE에 요청하지 않으며 재개 계획도 없다. §4 제목·머리말을 "제안"에서 "왜
  폐지했는가의 기록"으로 바꾸고, 폐지 사유(절단된 상품은 후보에 아예 못 들어오고 결정적
  정렬도 관련도를 보장하지 못한다 — 그 손실을 감수할 이유가 없다)를 앞머리에 적었다. 역산·
  `ORDER BY` 부재·`totalCount` 논증은 재론 방지 기록으로 그대로 보존하고, **정본(Notion I-1,
  2026-07-27 개정)도 "요청 파라미터 `size`를 없애고 서버 고정 상한도 두지 않는다"로 독립적으로
  같은 결론에 도달했음**을 §4에 인용했다. §8 요청 1 문안(BE 송부용)은 통째로 들어내고 머리말에
  "요청 1은 폐지되어 송부 목록에서 제외 — 근거는 §4" 한 줄만 남겼으며, §9의 `size`·`totalCount`
  개정 초안(적용할 일이 없어진 초안)도 삭제해 §9에는 `attributes` 4키 제외·rating/reviewCount
  캐싱 반영 diff만 남았다. §0 요약 표·§10 후속·완료조건 표의 "보류" 표기를 전부 "폐지"로
  맞췄다. ② **필드 축소(4키)는 "수용"에서 "4개 키 전부 수용"으로 명시** — BE 회신이 "4개 키
  전부"로 확정됐다는 사실을 §0 표·§8 요청 2 회신 줄에 반영했다. ③ **정본 대조로 드러난 사실
  3건을 제안서에만 반영**(`docs/api-spec.md`는 사람 승인 게이트라 이번 PR에서 건드리지 않음):
  정본이 `options`를 제외 목록에서 뺀다(2026-08-03 정정, 옵션 수 실측 p99=40·max=161·20 초과
  2.67%가 옵션 바이트의 44.5%)고 명시해 우리가 `options`를 요청 대상에서 뺀 판단과 정합함을
  §2·§5에 한 줄씩 추가; 정본은 리뷰 0건이면 `rating: 0.0`·`reviewCount: 0`(I-17과 동일 규약)로
  확정하지만 이 문서 사본은 null/미전송이면 rating이 지배한다고 서술해 값이 다르다 — §2
  `rating`·`reviewCount` 행에 각주를 달고 §10 후속에 "사본 동기화 대상"으로 등재(지금 고치지
  않음); 정본에 `brandName` 부분 매칭 규칙이 "⚠️ LLM 팀 확인 필요"로 미해결로 걸려 있음을 §10
  후속에 등재했다(답은 사람 결정 대기, 이 PR에서 정하지 않음). `app/`·`docs/api-spec.md`·
  `tests/`·`scripts/` 무변경 — 문서·CHANGELOG만 바뀌었다.
- **#384 — #363 후속: 구제~자동완화를 아우르는 공유 왕복 예산/first-token 데드라인 가드 설계** —
  #363의 전제("첫 SSE=`conditions`, 예산=first-token 10s")가 #396(구매자 `progress` 상시화,
  api-spec v0.26.2)으로 깨져 재기준선했다. 구속 예산은 이제 `stream_total_timeout_buyer_s`(30s)
  + 체감 지연이고, 오늘 기본값(`spring_max_retries=0`)에서는 여유(40%)가 있으나 재시도 억제
  스코프 비대칭(이 문서가 새로 발견) 때문에 **#394 원복(재시도 1로 복귀) 단독으로도 여유가
  즉시 50%로 줄고, #394+#306(미룬 턴 재시도 스킵)을 함께 원복하면 70%까지** 깎인다 — 공유
  예산은 "지금 필요한 가드"가 아니라 "#394/#306 원복의 선행 조건"으로 판정하고, 원복 시 사람
  판단 없이 등급이 정해지도록 3단 등급(관측/좁히기/좁히기+건너뛰기)의 진입 임계값(1급 지표
  자체의 이론 최댓값 대비 비율로 정의 — 30s 대비 고정 비율은 그 지표의 도달 가능 범위 밖이라
  기각)을 사전 결속했다. 코드·계약 변경 없음(개정안은 문서 안에 diff로만 제시).
  (`docs/specs/DESIGN-SHARED-BUDGET-384.md`)
- **#367 — HOME(I-22) 실패 모드 어휘 4종을 api-spec §3.7 에 현행 추인으로 규범화(v0.26.1, 와이어 불변) + combo_matrix degrade 축을 지면별 어휘로 갱신(HOME 미정의 셀 3건 해소)** — #335 매트릭스가 발견한 `surface=HOME × degrade∈{embedding_missing,rerank_failed,spring_timeout}` 미정의 셀(CHAT 검색/rerank 실패 어휘가 HOME엔 대응 경로가 없음)을 승인된 A안(현행 추인, 코드 무변경)으로 해소한다. `docs/api-spec.md` §3.7에 「HOME 실패 모드(degrade) 어휘」 소절을 신설해 `profile_unavailable`(200 degrade)·`catalog_unavailable`(503)·`catalog_timeout`(504)·`reason_degraded`(200+reason null) 4종을 규범화하고, 실패 응답표 503/504 행의 조건 서술 드리프트를 정정했다. `evals/combo_matrix/axes.json`의 `degrade` 축을 지면별 어휘로 갱신하고(`datasetVersion` 2.0.0) excludes 제약 2건으로 지면 밖 조합을 금지, `runner.py::_observe_home`에 신규 4종 관측 주입을 추가했다. 재생성 결과 케이스 58→57건, pairwise 2-wise 100%(1092/1092) 유지, UNDEFINED_CELLS.md는 미정의 셀 5→1건(잔존 #336)으로 줄었다 — #368(94f0fb2)이 이미 고친 `wishlist_add` SpringUnavailableError 갭도 재관측으로 defined 전환됐다.
- **#322 — #149 개인화 그래프 계약 개정: 개별 삭제 undo 창·원문 물리 삭제, 전체 초기화 범위에 대화 전사록 포함** — 구현(#150) 착수 전에 계약이 서로 모순 없이 한 방향을 가리키게 정리했다. **[HARD] 조항 2건이 뒤집힌다.** 계약만이며 코드 변경은 없다. (api-spec §2.5·§3.8·§3.9·§5, v0.26.0 / `docs/specs/SPEC-PROFILE-GRAPH-149.md` v0.2.0 / `SPEC-PROFILE-001` v0.8.0)
  - **개별 삭제 = 즉시 억제 → undo 창(기본 5분, config) → 원문 물리 삭제, tombstone 만 잔존.** 구 계약은 억제만 하고 원문을 무기한 보관했는데, 사용자가 "지웠다"고 믿는 문장의 원문을 들고 있을 이유가 없다(데이터 최소화). tombstone 에 시간 만료를 두지 않는 근거는 실측이다 — 세션 버퍼 flush 가 `profile_idle_sweep_interval_s`(60초) 주기로 돌아, 만료시키면 창이 닫힌 직후 같은 발화가 재승격돼 방금 지운 취향이 부활한다. **REQ-PGRAPH-032(pin 만료 없음)와의 구분 문장**을 SPEC 에 박았다 — 만료되는 것은 *원문 보관 기간*이지 *사용자 의도*가 아니다.
  - **전체 초기화가 `conversation_turns` 도 지운다**(감사 로그만 보존). 근거는 #149 가 REQ-PROF-034 에서 이미 채택한 논거의 연장 — 금지 대상은 *기계가 조용히 지우는 것*이고 사용자 자신의 삭제권은 별개다(예외 신설이 아니라 적용 범위 한정). **OPEN-G6**(파생은 만료되는데 원인 원문은 전사록에 남는 비대칭)이 해소됐고, Spring 에 채팅 이력 사본이 없어 **AI 단독으로 완결**된다. 전사록 자연 만료 TTL(OPEN-P5)은 별개 트리거이며 본 개정 범위 밖이다.
  - **§3.8 조회가 FE 직접 → Spring 프록시로 전환됐다 — v0.22.0 의 "의도된 비대칭" [HARD] 는 폐기다.** 마이페이지에는 채팅 세션이 없어 `chat:stream` 티켓을 발급받을 수 없다(CH-1b 는 `sessionId` 필수) — **재사용할 자산이 없는 전제 위의 규약**이었다. 전용 `profile:*` scope 신설안은 여전히 기각(채팅 검증 경로 회귀 위험)이며, 조회 이관은 그 기각을 뒤집은 것이 아니라 같은 이유로 한 걸음 더 간 것이다. 비대칭 전제가 흩어져 있던 6곳(§1.2 레인표·서술, §2.3 a/b, §3 앵커, §8 항목9)을 함께 고쳤다.
  - **I-번호 재채번 I-29~I-33 → I-32~I-37**(조회 I-32 합류로 6종). C-26 이 경고한 충돌이 실증됐다 — I-29~I-31 은 판매자 주문·리뷰(#297)가 선점하고 있었다.
  - **`evidenceCount` 와이어 제거** — `profile_buffer_repeat_cap`(=2)이 같은 발화를 2회로 잘라 담으므로 정확한 관측 횟수를 셀 수 없다(#119). 내부 `evidence_count` 는 병합 합산에 유지된다. **§3.8 `userId` 를 number(BIGINT)로 통일**하고(「타입 비대칭」 항목 삭제 — 프록시 전환으로 근거 소멸), **§3.9.1 `object.nodeId` 직접 지정**을 추가했다(FE 자동완성으로 고른 노드의 재정규화가 다른 노드로 튀는 것 방지).
  - **`error.detail` 을 §2.5 에 공식화**하고 §3.9 `409` 의 `graphVersion` 을 봉투 밖 → `error.detail.graphVersion` 으로 옮겼다. §4.1(I-2)이 이미 쓰던 관례가 미등재였고 §3.9 가 그 미등재를 근거로 반대 방향을 택했던 것이다. 확장 자리가 하나로 고정돼 **C-21(Spring 이 `409` 본문을 변형 없이 통과) 난이도가 내려간다**.
  - **함께 고친 기존 모순 2건** — (1) `SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-077·`SPEC-PROFILE-001` REQ-PROF-034 가 민감 파생 만료를 "기계 경로 하드 삭제가 의무인 **유일한** 예외"로 단정하고 있었다(undo 만료가 두 번째다). (2) 멱등 원장 TTL(`graph_idempotency_ttl_h`, 시간)이 undo 창(분)보다 길어 **purge 후 도착한 restore 재전송이 "복구됨" 200 을 재생**하는 구멍이 있었다(REQ-PGRAPH-028 로 차단).
- **#328 — 평가 커버리지 맵과 공통 규약을 `evals/README.md` 로 고정** — #275 실측("튜닝 스코어러가 no-op 보다 유의하게 나빴는데 임의 순서 기준선이 없어 몰랐다")이 드러낸 계측기 구조 문제를 규약으로 봉인한다. 공통 규약 8항(trivial baseline 의무 · caseId 척추 공유 · 결정론=CI/실 LLM=수동 · 슬라이스 쿼터 사전 산정 · 다중 비교 통제 · MFT/INV/DIR 유형 명시 · 하네스는 그 PR 에 커밋 · 지표 분자/분모 동봉+해시 변경 시 baseline 전체 재실행)과 커버리지 지도(14행 — 공백 4축은 #331/#332/#334/#335, 미분해 4항은 에픽 체크리스트 보존), 착수 순서(#333 P0 → #331 → #332 → #335 → #334, 병행 #329/#330, 독립 #336)를 확정했다. `evals/scoring` `passthrough` 가 검색 순위가 아니라 임의 순서 기준선이라는 해석 정정 포함. 코드 변경 0.

- **#330 — LLM 기반 서비스 평가·개선 방법론 조사(에이전트 평가·judge 신뢰성·자동 프롬프트 최적화·비결정성 회귀)를 등재** — 이슈 전제(KDD 서베이 Mohammadi et al. arXiv:2507.21504의 "4단계 프레임·4대 결함" 명명)를 본문 확인으로 정정했고, 자동 프롬프트 최적화는 `teacher−no-op`(#275) CI 가 0을 배제하지 못해 `no-go`(현시점), 비결정성 하의 회귀 게이트는 문헌(Ouyang 2023·Miller 2024)이 기존 규약과 같은 결론이라 `go`(판정 규칙 사전 등록 승격), LLM-as-judge 확대는 위치 편향 실측(#275 tau 0.3654)을 근거로 `조건부`로 판정했다. 코드·계약 변경 없음. (`docs/research/RESEARCH-LLMEVAL-330.md`)
- **#329 — 추천 시스템 평가 방법론 조사, 골든셋 v2 설계에 `조건부 go` 판정** — nDCG 판별가능성(Wang 2013·Valcarce 2018)·표본 설계(Sakai 2018·Carterette 2012)·오프라인-온라인 상관(Garcin 2014·Rossetti 2016)·재현성(Ferrari Dacrema 2019)·하드 네거티브(Krichene&Rendle 2020 등)·인간 평가(#153) 문헌을 #275/#328/#333 실측(no-op 0.738210 / student 0.616852 / teacher 0.782943, sd 0.402, 후보 깊이 9/18 ≤10)에 대응시켜 #333 골든셋 v2 설계(후보 깊이 30·하드 네거티브·슬라이스 쿼터)의 문헌 근거를 마련했다. (`docs/research/RESEARCH-EVAL-329.md`, 계약 무변경)
- **#149 — 개인화 관계 Graph schema와 사용자 제어 계약 초안 등재** — 취향이 지금은 편집 불가능한 마크다운 한 덩어리라 오염돼도 사용자가 고칠 수 없는데, #147의 커밋된 baseline이 **오염이 추천 품질을 실제로 깎는다**는 것을 보여준다(깨끗한 프로필 +0.20 / 노이즈 −0.053 / 반복 부풀림 −0.117 nDCG@10). 조회 §3.8 `GET /profile/me/graph`(FE 직접)와 제어 5종 §3.9 I-29~I-33(Spring→AI internal: 수정·삭제·복구·초기화·개인화 중지)을 신설했다. 계약만이며 코드 변경은 없다 — 구현은 #150. (api-spec §3.8·§3.9·§6.3 c, v0.22.0 / `docs/specs/SPEC-PROFILE-GRAPH-149.md` v0.1.0 / `SPEC-PROFILE-001` v0.7.0)
  - **조회는 FE 직접, 변경은 Spring 경유 — 의도된 비대칭이다.** 조회는 기존 `chat:stream` 티켓을 그대로 재사용해 공유 인증 경로를 건드리지 않고, 변경만 서비스 토큰 레인으로 분리했다. 전용 `profile:read`/`profile:write` 티켓 신설안은 **기각** — scope가 exact `chat:stream`으로 하드 고정된 검증 경로를 `/chat`·`/seller/chat`이 함께 지나가므로, 프로필 편집 기능을 위해 채팅 인증에 회귀 위험을 만드는 대가를 치른다. 비대칭 유지 규약을 §1.2·§2.3·§3.9 세 곳에 적었다(한 곳만 적으면 그 한 곳을 지우는 리팩터가 규약을 지운다).
  - **충돌은 `409 PROFILE_VERSION_CONFLICT`이고 `412`는 쓰지 않는다** — 409에 상태 인식 불일치 3형제가 이미 있어 소비자가 한 분기로 처리하고, 412는 서버의 상태→코드 매핑에 없어 기본값이 일반 코드로 나간다. 다만 **409의 기본 매핑값이 `STREAM_IN_PROGRESS`**이고 **404는 매핑에 아예 없어**, 구현 시 코드를 명시적으로 덮지 않으면 무관한 메시지가 나간다 — 정상 경로 테스트로 잡히지 않으므로 현재 동작을 핀 테스트로 고정했다.
  - **개별 삭제 = 억제(복구 가능), 전체 초기화만 물리 삭제.** `SPEC-PROFILE-001` REQ-PROF-034("삭제 금지")를 약화하지 않고 **적용 범위를 기계 경로로 한정**했다 — 금지 대상은 기계가 조용히 지우는 것이고 사용자 자신의 삭제권은 대상이 아니다. **사용자 수정이 다음 배치에 덮이지 않도록 만료 없는 고정(pin)** 을 규정했다(만료를 두면 수정이 조용히 되돌려져 기능 자체가 무의미해진다).
  - **개인화 중지 = 사용·수집 동시 중지, 데이터 보존.** 중지 중에도 그래프는 200으로 보이고 모든 정리 동작이 허용되며(보존 데이터를 지우려고 개인화를 다시 켜야 하는 상황을 만들지 않는다), 홈 추천은 기존 `NO_PROFILE`로 답해 **Spring 무변경**이다. 중지 기간의 발화는 전사록에서 소급 추출하지 않는다.
  - **민감정보는 판정이 아니라 노출 차단에 방어선을 둔다.** 하드 PII는 결정론적으로 드롭하지만 민감 *주제* 판정은 완전하지 않음을 계약이 명시하고, 대신 **대화 출처 근거 원문을 판정과 무관하게 항상 차단**한다. 민감 제외분은 사용자 삭제 개수에 섞지 않는다(섞으면 그 카운트가 곧 유출이다).
  - **전체 초기화는 전사록을 지우지 않으므로 "모든 데이터 삭제"로 표시하면 사실과 다르다** — FE 문구는 "개인화 데이터 초기화"여야 한다. 감사 로그도 보존한다(초기화가 일어났다는 기록을 함께 지우면 파괴 동작이 추적 불가가 된다).
  - **선결 조건으로 드러난 것**: `SPEC-PROFILE-001` OPEN-P12(자유형 fact에서 결정론적 키를 못 뽑는다)가 **#150의 차단 항목으로 승격**됐다 — 자유 텍스트 위에서는 "결정론적 투영·중복 불가"가 원리적으로 불가능하다. 억제가 실효하려면 consolidation이 그래프를 읽어야 한다는 것, 요약 쓰기 경로에 잠금이 없다는 것, "기억해" 경로가 발화 원문을 스캔 없이 저장한다는 것도 함께 기록했다.
  - 🔴 **미합의 9건을 C-20~C-28로 등재**했고 그중 **C-20·C-21·C-27·C-28은 #150을 막는다**. 특히 C-27(중지 즉시성이 Spring 소유 캐시 때문에 AI 단독으로 불가)과 C-28(브랜드 통제 어휘가 리포에 없음)이다. 결정 16("마이페이지 GET only")을 넘어서므로 §8 항목 9로 결정 개정 필요를 등록했다.
- **#285 — 챗봇 장바구니 삭제·수량 변경·찜 추가·해제·목록 internal 계약을 정본에 등재하고 확정까지 마쳤다** — Notion 「📡 API 명세서」에 I-24~I-28로 등재했다. 발명이 아니라 FE↔BE 정본 실측(C-4 삭제·C-3 수량 변경·M-5 찜 추가·M-6 찜 해제·M-4 찜 목록)의 의미론과 I-2/I-18의 internal 규약(`X-Internal-Token`, AI가 검증한 JWT `sub` 유래 신원, 3초 타임아웃, 응답 envelope)을 이식한 제안이며, I-25 수량 변경은 이슈 본문에 없던 신규 편입이다. **[2026-08-08 갱신] 잔여 안건은 전부 처분됐다** — 확정 2026-08-05, 사본 `docs/api-spec.md` §4.12~4.16 동기화 완료, CH-2 `action` 8종 확장은 §3.1 에 10종으로 등재(위 #116·#117 항목). 이 PR(#285 마무리 레인)은 계약을 다시 고치지 않고 ① BE `jarvis-backend` main 실측(2026-08-08, BE PR #92·#93)으로 계약·AI 코드·BE 3자 일치를 검증한 기록(`docs/proposals/285-cart-wishlist-internal.md`)과 ② 코드에 남아 있던 "Spring 구현 진행 중"·"BE 협의 전" 낡은 상태 표기 스윕(주석·docstring 만, 동작 무변경)과 ③ **I-25(수량 변경) AI 구현**(위 #285 항목 참조 — 결정론적 대상 해소·치환/합산 판정 라우팅 2경로·404 실패 처리·수량 미상 되물음)을 산출물로 냈다. 남은 것: Q11(`GET /internal/wishlist` 전량 반환 크기 상한)은 미결.

### Fixed
- **#421 — I-17 1선(enrich 콘텐츠 실패)이 재시도 예산 없이 첫 주기에 즉시 영구 격리돼 LLM
  샘플링 노이즈(JSON 파싱 실패)로도 정상 상품이 오격리되던 문제(#325 후속)** — 2선·3선(연속
  실패 스트릭)과 달리 1선은 시간 유계 보호가 없어, `enrichment_item_attempts`(기본 2)회
  재시도가 모두 우연히 실패하면 그 자리에서 영구 격리되고 커서가 전진해 Spring 이 그 상품을
  다시 변경분으로 실을 때까지 재처리되지 않았다. 신규 `artifacts_batch_content_retry_cycles`
  (기본 1주기, 0 이면 종전대로 즉시 격리)로 cross-cycle 재시도 예산을 준다 — 1주기 안에서는
  즉시 격리하는 대신 재시도 대기 큐에 등재하고, 다음 주기 재시도 패스(`_run_content_retry_pass`)가
  같은 페이로드로 다시 시도해 성공하면 회복(`BatchResult.recovered`), 예산 소진 시에만
  dead-letter 격리한다. **PR 리뷰 라운드 2 T1 대응**: 새 페이지 항목이 도착할 때마다 대기
  항목을 무조건 버리고 예산을 0 으로 재등재하던 것을 — enrichment 가 실제로 쓰는 필드
  (`name`·`description`·`category`·`brand`·`attributes`)가 이전 대기 항목과 같으면 그 예산을
  이어받아(`_enrichment_inputs_unchanged`) 소진시키도록 고쳤다 — 가격·재고처럼 그 입력에 들지
  않는 필드만 매 주기 갱신되는 poison 상품은, 재시도 패스가 손도 대기 전에 다음 주기 새
  변경분이 도착해 매번 리셋되는 바람에 예산이 영원히 소진되지 않고(dead-letter ERROR 가 안
  뜨고 WARNING 만 반복) 있었다. 내용이 실제로 바뀌면 종전대로 0 부터 새 예산을 준다(#421
  원래 취지 유지). **PR 리뷰 라운드 2 T2 대응**: 재시도 패스가 재시도 시점 실패를 종류
  불문 예산 소진으로 단순화했던 것도 고쳤다 — 재시도에서 enrich 는 살아났는데
  `_finish_change`(embed·store)만 일시 장애로 실패하면, 그 한 번의 불운이 기본 예산(1)을
  대신 태워 정상 상품을 영구 격리하고 있었다(#421 이 없애려던 오격리를 재시도 패스 안에서
  재현). 이제 재시도 시점 실패도 `_is_enrichment_content_failure` 로 다시 갈라 콘텐츠
  실패만 예산을 소진시키고, 그 외(재시도 시점 enrich 비콘텐츠 실패·finish 실패)는 예산을
  건드리지 않고 `_drain` 2선과 같은 `bump_failure_streak` 시간 유계 스트릭으로 판정한다
  (`artifacts_batch_item_dead_letter_cycles` 도달 시에만 격리). **재시도 패스는 그 주기 `_drain`
  이 hasMore 를 소진해 정상 완료한 뒤에만 돈다**(PR 리뷰 라운드 1 F1 대응) — 정상 완료는
  Spring 이 지금까지 발행한 변경분을 전부 소비했다는 뜻이라 그 시점 큐 잔여 항목이 그 사이
  `HIDDEN` 이 된 적이 없음을 보장하고(유령 상품 금지), `_drain` 이 중단된 주기(2선 전파·
  `PageFailureThresholdExceeded`·fetch 실패·`InvalidCursorError` → rebuild)에는 재시도 패스를
  아예 돌리지 않아 낡은 페이로드로 이미 삭제된 상품을 되살리는 것을 막는다. 재시도 대기 큐
  (`ProductChange` 페이로드)는 상품 원본 필드를 담으므로 AI Postgres 에 저장하지 않고 프로세스
  메모리에만 둔다(CLAUDE.md 원본 컬럼 사본 금지 원칙) — 재시작에 유실돼도 동작은 종전(즉시
  격리)과 같아 하한이 종전이다. 같은 product 의 새 변경분(HIDDEN 포함)이 도착하면 큐 항목을
  먼저 제거해 재시도 패스가 HIDDEN 상품을 되살리는 유령 상품을 막는다. 큐 상한(1,000, 튜너블
  아님) 도달 시 가장 오래된 항목을 축출하고 ERROR dead-letter 로 남긴다. **재시도 예산 카운터는
  `retry_attempts < budget` 로 판정해**(PR 리뷰 라운드 1 F4 대응) `artifacts_batch_content_retry_cycles=N`
  이 "cross-cycle 재시도가 정확히 N 회 일어난 뒤 격리"를 뜻하도록 경계값을 고정했다(off-by-one
  방지). `BatchResult`에 `recovered`·`retry_pending` 신설, scheduler·run_batch 요약 로그에
  반영. **PR 리뷰 라운드 4 T6 대응**: 1선이 콘텐츠 실패를 "감지"한 즉시(재시도 큐 등재
  여부 판정 **이전**) `BatchResult.failed` 를 올리던 것도 고쳤다 — 바로 아래 2선은 실제
  격리가 확정된 시점에만 `failed` 를 올리는데(전파 단계엔 안 올림) 같은 함수 안에서 1선·
  2선의 카운팅 시점이 비대칭이었다. `failed` 문서는 "격리된 단건 실패 수(dead-letter
  기록됨)"인데 실제로는 재시도 큐에 방금 등재됐을 뿐 아직 격리 안 된 건도 셌고, 그 결과
  `scheduler.py` 의 `if result.failed > 0: "증분 배치 부분 실패 — dead-letter 로그 확인"`
  ERROR 알람이 이번 주기엔 WARNING(다음 주기 재시도 예약)만 있고 dead-letter ERROR 가
  하나도 없는데도 떠서 온콜에게 없는 로그를 찾게 만들었다. 두 카운터를 목적별로 분리했다
  — `page_failed`(3선 비율 가드, 지역 변수)는 "이번 주기에 실제로 반영되지 않았다"만
  세므로 재시도 등재 여부와 무관하게 종전대로 증가시키고(3선 동작 불변), `failed`(관측)는
  격리가 **확정**된 경우(예산 0 즉시 격리·`_drain` 예산 소진 격리(T1 경로)·2선 스트릭 상한
  격리·재시도 패스의 콘텐츠 예산 소진 격리·재시도 패스의 스트릭 상한 격리·재시도 큐 상한
  축출)에만 증가시킨다 — `_enqueue_content_retry` 가 상한 축출 발생 여부를 bool 로 반환해,
  지금 등재하는 항목과 무관한(가장 오래된) 다른 항목의 확정 격리도 조용히 묻히지 않고
  `failed` 에 반영되게 했다. `scheduler.py` 코드는 무변경 — `failed` 의미가 바로잡히면서
  그 ERROR 알람 문구가 그제서야 사실이 된다. **PR 리뷰 라운드 5 T7 대응**: 항목 루프 맨
  앞에서 재시도 큐 항목을 무조건 pop 하던 것도 고쳤다 — 콘텐츠 실패로 예산이 쌓인 항목이
  다음 주기에 (콘텐츠 실패가 아니라) 2선 실패로 판정되고 스트릭이 상한 미만이면
  `raise stage_exc` 로 `_drain` 전체가 중단되는데, 이때 무조건 pop 이 이미 그 항목의
  cross-cycle 진행분(`retry_attempts`)을 되돌릴 수 없이 지워버려 커서는 안 전진해도
  다음 주기엔 예산이 0 부터 다시 시작했다 — `artifacts_batch_content_retry_cycles=N`
  이 보장하려던 "정확히 N 회 재시도 후 격리"가 관대한 쪽으로 깨지고, "콘텐츠 실패(예산
  누적) → 2선 중단(예산 유실)"이 반복되면 콘텐츠 예산도 2선 스트릭도 영영 상한에
  도달하지 못한 채 무기한 재시도만 반복할 수 있었다(#421/#416 이 없애려던 "poison
  상품이 상한에 영영 도달하지 못한다"와 같은 계열의 결함). 무조건 pop 을 peek(조회만)
  으로 바꾸고, 이 항목의 운명이 실제로 확정되는 지점(HIDDEN 삭제 성공·처리 성공·콘텐츠
  예산 소진 격리·2선 스트릭 상한 격리·콘텐츠 실패 재등재)에서만 개별적으로 pop 하도록
  고쳤다 — **2선이 전파(raise, 중단)하는 경로만은 큐를 전혀 건드리지 않는다**, 이번
  수정의 핵심이다. 유령 상품 불변식은 그대로 유지된다(재시도 패스는 `_drain` 이 정상
  완료했을 때만 돌고, 정상 완료한 실행은 모든 항목이 확정 분기 중 하나를 반드시
  거치므로 중단된 실행은 애초에 재시도 패스에 도달하지 않는다). (api-spec §4.8, v0.28.2)
- **#416 — I-17 2선·3선 연속 실패 스트릭이 프로세스 메모리에만 있어, 스케줄러가 수렴 창
  (기본 3주기 ≈ 15분)보다 자주 재시작되면(연속 배포·크래시 루프) poison 상품이 dead-letter
  상한에 영영 도달하지 못하던 문제** — 2선은 상한 전까지 예외를 전파(커서 미전진)하므로
  재시작이 스트릭을 매번 0으로 리셋하면 배치가 같은 자리에 무기한 갇힌다(#325 가 없애려던
  stuck-batch 의 재발). 스트릭 저장을 `ArtifactStore` 공유 계약으로 옮겨(신규
  `FailureStreakTable`, `bump_failure_streak`/`clear_failure_streak`/
  `purge_stale_failure_streaks`) `PgCatalogArtifactStore`가 pg-catalog `batch_failure_state`
  테이블(신규, `db/catalog/init/00_products.sql`·
  `db/catalog/migrations/20260807_batch_failure_state.sql`)에 영속한다 — 단일 원자 UPSERT
  (`ON CONFLICT ... DO UPDATE`)로 다중 인스턴스에서도 정확하고, DB 오류 시 3개 메서드 모두
  예외를 삼키고 인메모리 폴백으로 위임해(종전 동작과 같은 하한) 테이블 부재·pg 순단에도
  배치가 죽지 않는다. `clear_failure_streak` 은 DB DELETE 를 시도하되(PR 리뷰 라운드 1 F5
  대응) **성공 여부와 무관하게 인메모리 폴백 표도 항상 clear** 한다 — 폴백을 프로세스 수명
  latch 로 승격하면 #416 이 고치려던 "잦은 재시작에도 유계 수렴" 목표가 무력화되므로, DB
  DELETE 만 실패해도(dict pop 이라 비용 0인) 인메모리 clear 는 그대로 적용해 드리프트의
  절반을 없앤다(남는 드리프트 창은 같은 TTL 로 유계). 신규 `artifacts_batch_failure_streak_ttl_s`
  (기본 3600s)로 "연속"의 정의를 시간으로 못박아, 영속화가 무관한 과거 실패를 오늘 실패와
  합쳐 즉시 상한에 닿는 오격리를 막는다. 배치 1회 시작 시 만료 스트릭을 청소한다
  (`purge_stale_failure_streaks`, 실패 시 무시). HIDDEN 삭제가 성공하면 그 상품의 스트릭을
  clear 한다(PR 리뷰 라운드 1 F3 대응 — 스트릭이 실패 종류가 아니라 상품에 묶여 있어, 삭제
  실패로 쌓인 스트릭이 다음 성공까지 남아 있으면 이후 무관한 재입고
  실패와 합산돼 상한에 조기 도달할 수 있었다). 예산 카운터는 "N 이면 정확히 N 회"
  불변식(경계값 off-by-one 방지)으로 확정했다. `reset_batch_failure_state()`는 이제 #421
  재시도 큐만 비운다(스트릭은 스토어 수명과 함께 간다). **PR 리뷰 라운드 2 T3 대응**:
  `PgCatalogArtifactStore` 의 폴백 WARNING 플래그(`_failure_streak_fallback_warned`)가
  latch 였던 것도 고쳤다 — 한 번 True 가 되면 인스턴스 수명 동안 리셋되지 않아, pg 순단
  1회로 latch 된 뒤 DB 가 복구돼도 그 뒤 더 심각한 장애가 나면 조용히 넘어갔다(F5 가
  "폴백을 프로세스 수명 latch 로 승격하면 안 된다"고 정한 원칙이 이 플래그에는 지켜지지
  않고 있었다). 3개 DB 경로(`bump_failure_streak`·`clear_failure_streak`·
  `purge_stale_failure_streaks`)가 정상 종료할 때마다 플래그를 False 로 되돌려
  (`_mark_failure_streak_healthy`), 다음 장애가 다시 WARNING 을 낼 수 있게 했다. **PR 리뷰
  라운드 2 T4 대응**: `FailureStreakTable` 의 방어적 메모리 상한(`_FAILURE_STREAK_MAX_ENTRIES`
  =10,000)이 item·page 를 한 dict 에 합쳐 "합산 10,000"으로 적용되고 있던 것도 고쳤다 —
  구 동작(item 10,000·page 10,000, 총량 20,000)의 절반으로 줄어 있었을 뿐 아니라, 상한
  초과 시 `clear()` 가 item·page 를 한꺼번에 날려 대량 실패 상황(카탈로그 전량 동시 실패
  등)에서 스트릭이 통째로 리셋되면 이 이슈가 고치려던 "상한에 영영 도달하지 못한다"가
  그대로 재현될 수 있었다. `FailureStreakTable` 내부 저장소를 kind 별 독립 하위 dict 로
  나눠 상한과 방어적 비움을 kind 단위로 적용하도록 고쳤다(item 10,000·page 10,000 이
  독립). **PR 리뷰 라운드 3 T5 대응**: `_run_content_retry_pass` 의 콘텐츠 실패 분기가
  item 스트릭을 clear 하지 않던 것도 고쳤다 — `_drain` 의 대응 분기는 콘텐츠 실패 시 큐
  유지·즉시 격리 결과와 무관하게 **항상** clear 하는데(콘텐츠 실패는 정의상 항목 고유라
  2선 스트릭의 "연속"을 끊는다, #325 R6 의도), T2(라운드 2)가 재시도 패스에 2선 스트릭
  경로를 새로 넣으며 이 clear 를 빠뜨려 비대칭이 생겼다 — "재시도 중 인프라 실패(streak
  bump) → 재시도 중 콘텐츠 실패(clear 없이 retry_attempts 만 +1) → 재시도 중 다시 인프라
  실패(streak 가 안 끊기고 이어짐)" 순서에서 연속이 아닌 인프라 실패가 연속으로 오판돼
  실제보다 이르게 격리될 수 있었다(F3 — HIDDEN 삭제 성공이 스트릭을 끊지 않던 것 — 와
  같은 계열의 결함). 콘텐츠 실패 분기 양쪽 경로 모두에서 `clear_failure_streak` 를
  호출하도록 고쳐 `_drain` 과 동일한 불변식을 맞췄다. **PR 리뷰 라운드 6 T8 대응(부분
  수용)**: 리뷰어는 "DB 가 간헐적으로 실패/성공을 오가면 어느 쪽 카운터도 상한에 도달하지
  못한다"고 지적했는데, SQL 을 확인하니 그건 과장이었다 — DB 행은 지워지지 않고 성공한
  bump 마다 단조 증가하므로 DB 성공률 p 면 대략 1/p 배 느리게라도 상한엔 **도달은 한다**
  (정지가 아니라 지연). 그래도 지연은 실재하고 고칠 값이 싸서 고쳤다 —
  `bump_failure_streak` 의 DB 성공 경로가 같은 (kind, key) 로 인메모리 폴백에 남은
  진행분을 `peek` 하고, "이번 호출 자체가 그 진행분 위에 이어지는 다음 1회"이므로
  `peek 값 + 1` 과 DB 자체 값 중 더 큰 쪽을 최종 스트릭으로 삼는다 — 더 크면 그 값으로
  DB 를 UPDATE 해 흡수하고, 어느 쪽이든 폴백 엔트리는 지워 두 번 세지 않는다. 흡수
  UPDATE 가 실패해도 psycopg 커넥션이 `with` 블록 예외 시 트랜잭션 전체(방금 성공한 원
  UPSERT 포함)를 롤백해 이중 계수 없이 기존 예외 처리로 자연히 흘러가 폴백이 이어받는다
  — 이 메서드는 여전히 절대 예외를 밖으로 내지 않는다. 실측: 실패→성공→실패→성공 4회에서
  수정 전엔 두 저장소가 각자 자기 성공분만 세어 마지막 값이 2([1,1,2,2])였는데, 수정
  후엔 3([1,2,1,3])으로 상한 도달이 앞당겨진다. **부분 수용인 이유**: 폴백을 흡수 후
  지우기 때문에 흡수 직후 다시 DB 가 실패하면 그 다음 폴백 bump 는 지워진 자신의 기록만
  보고 0 부터 다시 세, 임의의 교차 패턴에서 "정확히 호출 횟수만큼"을 수학적으로 보장하진
  못한다(폴백을 지우지 않고 동기화해 두면 완전히 재현되지만, 그러면 폴백이 실패 전용
  임시 저장소가 아니라 DB 의 상시 그림자 사본이 돼 이 이슈 범위를 넘어선다) — 남는 한계로
  문서화했다. (api-spec §4.8 은 "저장 실패 시 프로세스 메모리 폴백"까지만 말하고 내부
  조정은 서술 범위 밖이라 문장 변경 없음, v0.28.2 그대로)
- **#435 — 프로필 벡터 경로로 추천된 상품을 이름으로 지목한 찜/담기가 실패하던 문제 (api-spec §3.1, v0.28.1)** — 조건 없는 발화의 회원 경로(`no_condition.rank_by_profile`, 취향 벡터 랭킹)가 `set_last_reco` 에 빈 이름(`(pid, "")`)만 저장해, decompose 프롬프트의 `LAST_RECOMMENDATIONS` 에 이름이 없어 이름 매칭(#118 실측 8/8 신호)이 원천적으로 불가능했다 — FE 위조방지 설계(추천 카드는 `screen` 에 실리지 않는다)와 AI 상품명 공백(AI 카탈로그 인덱스에 원본 컬럼 없음)의 이음매였다. `products.search_doc`(AI 생성물, `build_search_doc` 임베딩 입력으로 이미 조립돼 저장됨) 첫 줄에서 이름을 최선노력 복원해(`_extract_name_from_search_doc`, 필드 순서 커플링을 왕복 테스트로 고정) `set_last_reco` 에 실었다. 노출 집합 안에서 이름이 중복되면(name 없는 상품은 첫 줄이 category 로 밀려 여러 상품이 같은 문자열을 가질 수 있다) 모호함을 확정하지 않고 전부 버린다(`dedup_exposed_names`, G2). `products.search_doc` 는 판매자 입력이라 `_strip_unsafe` 로 신뢰경계를 통과시키고(G3), 스토어 조회·추출 실패는 예외 없이 이름 없음으로 degrade 한다(G4) — pg-profile 에는 여전히 productId 만 영속하고 이름은 기존과 같이 프로세스 로컬 휘발성 캐시로만 흐른다(CLAUDE.md 원본 컬럼 사본 금지 불변). 되물음 문구도 함께 고쳤다 — `last_reco`(스레드 누적 추천)가 비어 있지 않은 담기/찜 미해소 턴은 "추천을 먼저 받아보시면"(거짓 — 이미 추천을 받았다) 대신 "추천해 드린 상품 중에서 이름을 말씀해 주시면"으로 안내한다(화면 지시어 문구가 있으면 그쪽이 우선, `last_reco` 가 비면 오늘 문구와 바이트 동일). 담기·찜 계열 턴에 `last_reco_name_coverage`(개수만, PII 미포함) INFO 로그를 추가해 다음 추적 라운드가 같은 미확정을 반복하지 않게 했다. **판정(프로필 경로 vs 화면 vs LLM)은 운영 로그로 확증한 것이 아니라 코드 경로·저장소 실측(캐시 LRU 미축출·I-3 폴백은 정상 경로 합류)으로 추론한 것이다.** `resolve_screen_reference`(결정적 화면 지시어 해소기)는 손대지 않았다 — 그 모듈은 이름 지목을 의도적으로 LLM 에 양보하는 설계라(§3.1 v0.28.1 서술 추가) 이 이슈는 이름 **공급**을 고치는 것이지 해소기를 늘리는 것이 아니다. 담기 허용 목록(`allowed`) 계산·미해소 판정 조건은 불변.
- **#430 — `decompose` 가 "아무거나"류 발화에도 `semanticQuery` 를 지어내 과소지정 되물음(#336)이 100% 발동하지 않던 문제** — `_SYSTEM` 은 `semanticQuery` 를 "찾는 상품의 의미"로 정의만 하고 **"지정할 게 없으면 비워라"는 지시가 없었다.** LLM 이 무엇이든 텍스트를 내면 `semantic_query_is_fallback` 이 즉시 False 가 되고 `is_underspecified_turn` 이 "의미 신호가 있는 턴"으로 읽어 되물음을 껐다 — 실측 `missRate` **111/112(99.1%)**, 독립 2런 동일. `- recommend:` 규칙 절 **끝에 규칙 한 줄만** 넣어 고쳤다: "찾는 상품의 단서(종류·용도·상황·목적·브랜드·색상)가 발화에도 PRIOR_FILTERS·LAST_RECOMMENDATIONS·SCREEN 맥락에도 없으면 `semanticQuery` 는 빈 문자열". 판정 코드(`underspecified.is_underspecified_turn`·`no_condition.py`)는 **한 줄도 바뀌지 않았다** — 실측이 판정 코드는 정상이라 말한다. 같은 하네스·같은 앵커·같은 티어(fast, `gpt-5-nano`)로 전/후 각 2런(전부 `source=repo:_SYSTEM`, 출고판 sha12 `865ed6fd771e`): `missRate` 99.1%·99.1% → **9.8%·6.2%**, `falseAlarmRate` 0.0%·0.0% → **1.9%·2.9%**(사전 등록 상한 3.6% 내), `judgmentAccuracy` 48.6% → 94.0%·95.4%, 의미신호 소실 가드(상품명이 실제로 발화에 있는 category·keyword 4앵커 32표본) 1/32·0/32, 불변식(`flagOffInvariant`·`priorGateInvariant`) 4런 모두 0/240. 산출물 `evals/underspecified_probe/baselines/fast-2026-08-07-430-{before,merged,after}-*/`(판정표 정본은 `after-1/README.md`, 탈락 후보 9종의 sha12·수치 포함). **작업 중 `origin/dev` 병합이 측정물을 바꿨다** — #386(PR #441, 커밋 `3547e43`, `wishlist_view` 의도 신설)이 `_SYSTEM` 에 548자를 더한 판(`f99a98867e4a`)에서 `falseAlarmRate` 가 1.9 → 3.8 → 4.8% 로 단조 상승해 상한을 3런 중 2런에서 넘겼고(오탐 11건 중 9건이 브랜드-only 앵커 — 모델이 "삼성"·"LG"를 `filters.brand` 로 추출하지 못한 표본이 드러난 것이다), 비움 트리거의 단서 목록에 **브랜드·색상 10자**를 더해 되찾았다(병합판 3런은 `-merged-{1,2,3}` 에 근거로 커밋). **잔여 회귀를 알고 머지한다** — 같은 픽스처(v6)에서 그 10자만 다른 `evals/intent_probe` 대조에서 `categoryClear` 31·31 → **28·28**(−3)이고 `demonstrative`·`mainIntent` 도 각 −3 이다(팔 내부 분산 0이라 노이즈로 보기 어렵다). 반대로 `categoryAction3Way` +4.5 · `general` +3.5(#386 이 떨어뜨린 것을 병합 전 수준으로 복구) · `categoryMixedReplace` +3.5 · `conditionOnlyNoCategoryQuery` +3.0 등 **10축이 올랐고**, `screenExactPick` 과 안전축 `screenNoHallucination`·`screenReask` 는 무회귀다. 이슈 「할 일」 ②·③은 **진단하고 반려**했다 — ②(수치 제약 지시)는 재작성 후보 2종이 primary 를 +31.2pp·+6.2pp 깎았고 원인이 어휘가 아니라 같은 절 뒤쪽의 무조건 긍정 명령이었으며, ③(`attrConditions` 억제)은 미탐의 그 갈래를 11건 → 0건으로 없앴지만 `screenExactPick` 을 추가로 −1.5 끌어 별도 이슈로 분리를 제안한다(`docs/lessons.md` 2026-08-07 4건). 계약(api-spec) 무변경.
- **#430 부수 — #162(조건 없는 발화 → I-3 인기 경로, api-spec §4.17)가 기본 설정에서 비로소 발동한다** — `semantic_query_is_fallback` 의 소비자는 둘인데(`is_underspecified_turn` #336 · `is_no_condition_turn` #162) **후자에는 플래그가 없다.** 위 프롬프트 수정은 `underspecified_reask_enabled`(기본 False)를 켜지 않아도 **오늘 운영 동작을 바꾼다**: `semanticQueryIsFallback=true` 표본이 1/240·1/240 → **출고판 2런에서 `no_condition` 슬라이스 39~40/40** 이 됐고, 그중 `is_no_condition_turn` 의 더 엄격한 조건(`_FILTER_AXES` 전부 빔 + `prior is None`)까지 통과하는 것이 바로 그 슬라이스다. 즉 "아무거나 추천해줘"류가 무필터 I-1(실측 7,245건·13.33MB·1.112s, `docs/specs/MEASURE-I1-RESPONSE-132.md`)로 새던 것이 멈추고 #162 설계대로 I-3 인기 경로 + 고지로 간다 — `no_condition.py` 모듈 docstring 이 그 무필터 호출을 "계약 위반"이라고 부르고 있었다. 회귀가 아니라 **두 번째 죽은 기능이 살아나는 것**이며, `no_condition.py` 는 한 줄도 바뀌지 않았다. 가격 제약만 있는 턴이 여전히 `is_no_condition_turn=False` 로 남는 혈반경은 기존 `tests/unit/test_no_condition.py::test_any_single_condition_axis_blocks_trigger` 가 고정한다. 계약(api-spec) 무변경.
- **#439 — 스트림 티켓 신원 discriminator XOR 규약이 운영에서 실제 발급되는 판매자 티켓을 전부 거부하던 문제(api-spec §2.3, v0.28.0)** — 종전 `_claims_to_identity`(jwks 레인)는 `role`과 `sub_type`이 함께 있으면 값과 무관하게 `401 TOKEN_INVALID`(`exactly one identity discriminator is required`)였다. BE `StreamTicketProvider` 실측과 CH-6 정본(2026-07-18 확정)을 확인한 결과 실제 발급 형식은 "`sub_type`은 모든 티켓 공통, 판매자만 `role="seller"`·`brandId` 추가"이며 판매자 티켓은 `sub_type="member"`를 항상 동반한다 — 즉 XOR 규약이 BE가 실제로 발급하는 판매자 티켓을 전부 거부하고 있었고, 이것이 운영 `/seller/chat 401`(#408이 사유 로깅을 넣은 바로 그 401)의 원인이었다. `sub_type`을 모든 티켓의 필수 클레임으로, `role`을 선택적 권한 클레임(있으면 exact `"seller"` + `sub_type="member"` 요구)으로 재정의해 XOR을 폐지했다 — `role="seller"`+`sub_type="member"` both-claims 티켓을 신규 수용하고, `sub_type` 없는 판매자 티켓만 종전 허용에서 `401`로 강화했다(CH-6 정본상 실존하지 않는 형식이라 와이어 영향 0). BE 확답에 따라 구매자 티켓에는 `role`을 싣지 않으므로 buyer role 값(`"buyer"` 등 추측 상수)은 신설하지 않았다. 401 사유 문자열은 `invalid sub_type claim`/`invalid seller role claim` 2종으로 정리했고 #408 로그 경로에 그대로 반영됨을 테스트로 확인했다. dev 레인(`AUTH_MODE=dev`)은 이번 개정 대상이 아니며 무변경이다.
- **#386 — `evals/combo_matrix` 러너가 찜 조회(I-28)를 스텁하지 않아 정상 케이스도 실패를
  관측하던 문제** — 담기 계열과 달리 `get_wishlist` 는 `degrade=none` 에서도 호출되는데 패치가
  없어, 로컬에 Spring 이 없으면 실 네트워크 호출이 실패해 관측이 환경에 따라 뒤집혔다. 조회
  계열은 늘 패치하고 실패 주입 예외는 실 어댑터 규약대로 `SpringUnavailableError` 를 쓴다(#376).
- **#386 — DIR 쌍(하드필터 추가 → 결과 비증가)이 공허하게 통과하던 문제** — 재생성으로 흔드는
  축이 `category` → `price_min` 으로 바뀌었는데 `PAIR_CATALOG` 4건 가격이 전부 3만원 이상이라
  필터를 태워도 `base=3 · perturbed=3` 이었다. #371 이 `category` 대조군을 넣은 것과 같은
  방식으로 3만원 미만 상품 1건을 더해 해소했다.
- **#391 — `embed_texts` 총 소요가 청크 수만큼 무제한 누적될 수 있던 문제(#353 후속)** — `embedding_timeout_s` 는 청크(HTTP 요청) 1건당 상한이라, 100건을 넘는 입력이 여러 청크로 나뉘면 `embed_texts` 한 번의 총 소요가 `청크 수 × embedding_timeout_s` 까지 누적될 수 있었다. 방식2(`embedding_rerank`)가 hot path 기본이라 이 누적은 SSE first-token 예산을 잠식하는데도, 종전엔 함수 단위 총 시간 상한이 코드로 강제되지 않고 docstring 주의문에만 의존했다. 신규 `embedding_total_timeout_s`(기본 3.0s, `embedding_timeout_s` 절 안)로 `embed_texts` 호출 1회 전체의 벽시계 예산을 두고, 첫 청크는 예산과 무관하게 항상 시도하되 두 번째 이후 청크는 내기 전에 `경과 + embedding_timeout_s > 예산` 이면 청크를 내지 않고 `EmbeddingError` 를 던진다(부분 결과 금지 — 호출부가 `zip(..., strict=True)` 등 위치 기반으로 인덱싱해 짧은 결과는 조용한 오정렬을 낳는다) — 기존 degrade 경로(`EmbeddingRerankBackend` → Spring 순서, #101/#7)로 자연히 이어진다. 오프라인 1회 빌드(`category_seed.seed_from_file`, 카테고리 leaf 2056건 → 21청크)는 `embed_texts(..., total_timeout_s=math.inf)` 로 이 예산을 명시 제외한다. 계약(api-spec) 무변경.
- **#383 — 기동 가드 `_deferred_first_event_i1_calls` 가 구제 폴백 한 단을 과소계상하던 문제(#363 followup)** — #363 이 실측으로 고정해 둔 불일치(가드 모델 2 ≠ 실측 구제 체인 단 수 3, `test_fanout.py` `test_worst_case_rescue_chain_sequential_stages_before_first_sse`)를 §5 가 제안한 보정식으로 해소했다. `1 + (1 if category_expand_enabled else 0) + min(relaxation_max_rounds, |relaxation_auto_fields ∩ relaxation_chip_fields|)` — F-1(#222)에는 별도 kill-switch가 없어 `category_expand_enabled`(기본 `True`)가 F-1·#343 둘의 공통 전제를 잠그고, 둘은 `category_expand_notice_suppressed` 로 상호배타라 한 턴 최대 1회이므로 항이 아니라 존재 여부만 더한다(`search_filter_guard_enabled`(#393)는 무필터 축 0개 턴만 스킵하므로 이 항을 없애지 않는다 — 항에 넣지 않았다). 기본 설정 값은 2 → **3**이 되고(`3 × 3.0 = 9.0 < 10.0`, 기동 통과), 오류 메시지 `recovery` 문구에 새 손잡이 `CATEGORY_EXPAND_ENABLED=false`를 추가했다. 배포 영향은 실측으로 배제했다 — `.github/workflows/deploy.yml`이 운영 env 파일을 매 배포마다 고정 키 목록으로 전면 재작성하는데 그 목록에 `SPRING_TIMEOUT_S`·`CATEGORY_EXPAND_ENABLED`·`RELAXATION_*`는 없어 운영은 코드 기본값으로 돈다. 런타임 동작(`graph.py`)·기본값·계약(api-spec) 무변경 — 기동 시점 검증식만 고쳤다. **PR #414 Claude 리뷰 대응**: 세 항을 균질하게 `spring_timeout_s` 로 값 매기면 구제 폴백 항을 과소평가한다는 지적을 코드로 재현·확인했다 — `graph.py::stream_recommendation` 에서 `spring_client.suppress_search_retry()` 로 재시도를 끄는 `with` 블록은 본 검색(`asyncio.gather` 호출)과 자동완화 probe(`_probe(cand)`) 를 감싼 두 곳뿐이고, F-1/#343 구제 재검색(같은 함수의 `_run_search_unfiltered()` 호출 두 곳 — F-1 폴백·억제-후 재판정)은 그 블록 밖이라 `spring_client.py::search` 의 `attempts = 1 if _search_retry_suppressed.get() else settings.spring_max_retries + 1` 를 그대로 받아 항상 재시도한다(`SPRING_MAX_RETRIES=1` + 기본 타임아웃이면 가드 계산 9.0<10.0 이 통과시키지만 실제 최악은 3.0+3.0+3.0×2=12.0>10.0). 가드 OFF(기본) 분기를 `suppressed_calls × spring_timeout_s + rescue_calls × budget`(신설 순수 함수 `_deferred_first_event_rescue_i1_calls` 가 구제 항만 뗀다, `rescue ≤ total`·`total==0→rescue==0` 불변식 보장)로 항별로 나눠 값을 매기도록 고쳤다. `.env.example` 의 `SPRING_MAX_RETRIES` 예시값도 1 → **0**으로 정정했다(코드 기본값이 이미 0, #394) — 예시 그대로 부팅하면 새 식에서 기동이 거절되던 상태였다. 오늘 기본값(`spring_max_retries=0`)에서는 `budget == spring_timeout_s` 라 항별 값 매김이 갈리지 않아 영향 없음(9.0 그대로).
- **#325 — I-17 증분 배치가 enrichment 토큰 예산 소진(`openai.LengthFinishReasonError`)으로 운영 정지되던 문제** — 운영 fast tier(gpt-5-nano, reasoning 모델)에서 하드코딩 `max_tokens=600` 전량이 `reasoning_tokens`로 소진돼 본문 0자로 매 5분 주기 정지했다. `enrichment_max_tokens`(기본 2048)·`enrichment_reasoning_effort`(기본 minimal, 배포 변수 `OPENAI_FAST_REASONING_EFFORT` 와 무관하게 고정) 를 config 로 주입하고 `LLMClient.complete` 에 keyword-only `reasoning_effort` 파라미터를 추가했다(OpenAI 캐시 키에 override 포함해 캐시 오염 방지, Anthropic 은 무시). 함께 `artifacts_batch._drain` 의 head-of-line blocking 도 고쳤다 — ON_SALE 단건 실패는 `enrichment_item_attempts`(기본 2) 회 재시도 후 dead-letter 기록으로 격리하고 다음 항목으로 계속하며, 페이지 실패 비율이 `artifacts_batch_failure_ratio_threshold`(기본 0.5) 이상이면(광역 장애로 간주) `PageFailureThresholdExceeded` 를 던져 그 페이지 커서만 미전진(자연 복구)한다. 단, 운영 증분 페이지는 대개 1~3건이라 표본이 `artifacts_batch_failure_min_sample`(기본 5) 미만이면 비율 판정을 생략하고 격리+전진한다 — poison 단건과 광역 장애를 소량 표본만으로 구별할 수 없기 때문이며, 이 가드가 없으면 운영에서 가장 흔한 "문제 상품 1건" 상황에서 ratio=1.0 으로 여전히 head-of-line blocking 이 재현됐다. HIDDEN 삭제 실패는 격리하지 않고 그대로 전파(fail-closed 유지). `BatchResult.failed` 신설, scheduler·run_batch 요약 로그·failed>0 시 별도 ERROR 로그로 관측 사각을 없앴다. **PR #399 리뷰 대응(정밀화)**: 소량 표본에서는 비율 가드가 사실상 죽은 코드가 돼 광역 장애(임베딩 API 다운 등)까지 매번 poison 단건으로 오분류될 수 있음이 지적됐다 — 격리 후보를 enrichment(LLM 호출+파싱) 단계의 내용 실패로 구조적으로 한정하고, 임베딩·스토어 실패와 재시도 소진 후 타임아웃 계열(`app.core.llm.is_timeout_error`)로 판정된 enrichment 실패는 격리하지 않고 그대로 전파하도록 고쳐, 페이지 크기와 무관하게 광역 장애를 자연 복구 경로로 보낸다. 비율 가드는 이제 2선 방어. **PR #399 리뷰 2차 대응(시간 유계)**: 위 "종류로 가른다" 규칙의 대칭적 구멍 2건이 지적됐다 — (1) 특정 상품에서만 결정적으로 재현되는 poison 타임아웃은 재시도를 다 써도 격리되지 않아 매 주기 같은 자리에서 영원히 실패했고, (2) `_finish_change`(embed·upsert) 실패를 무조건 인프라로 규정해 실제로는 그 상품 하나의 콘텐츠 문제(예: enrichment 산출 `extras`가 `embedding_meta_complete` CHECK 위반)일 수 있는 결정적 실패도 영구히 막혔다. 광역 장애와 항목 고유 결정적 실패는 단일 주기 관측만으로는 원리적으로 구별 불가하다는 것이 진단이었다 — 실제로 갈리는 신호는 시간(연속 주기 수)이다. 상품별 연속 실패 스트릭(모듈 메모리, 주기 간 유지, 성공 시 리셋)을 신설해 `artifacts_batch_item_dead_letter_cycles`(기본 3주기 ≈ 15분) 미만이면 종전대로 전파(자연 복구)하고, 도달하면 항목 고유 실패로 확정해 dead-letter 격리한다. enrich 내용 실패(1선)는 정의상 항목 고유이므로 스트릭 판정 없이 즉시 격리하는 종전 동작을 유지한다. 스트릭은 프로세스 재시작 시 리셋되는 인메모리 카운터(영속화는 범위 밖)이며, 스케줄러 잡의 `max_instances=1`·단일 프로세스 전제로 충분하다. 비율 가드는 이제 3선 방어. **PR #399 리뷰 3차 대응(3선도 시간 유계)**: 위 시간 유계가 2선에만 걸려 있어, 1선이 특정 카테고리 상품들의 프롬프트 회귀로 다건을 매 주기 즉시 격리하면(스트릭을 쌓지 않고 pop) 2선 상한이 걸리지 않고, 페이지 실패율은 매 주기 똑같이 임계를 넘어 `PageFailureThresholdExceeded` 가 반복돼 커서가 영원히 전진하지 않는 구멍이 지적됐다 — 3선이 원래 잡으려던 바로 그 케이스(대량 내용 파손)에서 #325 증상이 재현되는 셈이다. 같은 커서(그 페이지를 가져온 fetch 값)에서 비율 가드가 연속 발동한 횟수를 세는 모듈 카운터(프로세스 메모리, 주기 간 유지, 페이지 정상 종료 시 리셋)를 신설해 `artifacts_batch_page_failure_max_cycles`(기본 3주기 ≈ 15분) 미만이면 종전대로 전파(자연 복구)하고, 도달하면 대량 파손이 자연 회복되지 않는 것으로 확정해 그 페이지를 격리(항목들은 이미 1·2선에서 dead-letter 기록됨) 후 커서를 전진시킨다. `HIDDEN` 삭제 실패·`status` 계약 위반은 항목별 ack/DLQ 계약이 없어 이 시간 유계의 대상에서 제외되며 종전대로 무기한 fail-closed 다(api-spec §4.8 명시). **PR #399 리뷰 4차 대응(콘텐츠 실패 화이트리스트)**: 1선 판정("타임아웃이면 2선, 아니면 1선")이 블랙리스트라 `is_timeout_error` 가 모르는 예외(`openai.RateLimitError` 429·`APIConnectionError`·`InternalServerError` 5xx 등 흔한 일시적 인프라 장애)가 전부 콘텐츠 실패로 오분류돼 첫 주기에 곧바로 영구 격리됨이 지적됐다 — R4·R5 가 만든 시간 유계 보호를 흔한 장애가 통째로 우회하는 구멍이었다. `app.core.llm.is_output_length_error`(출력 토큰 예산 소진 전용, `is_timeout_error` 판정 범위는 불변)와 `artifacts_batch._is_enrichment_content_failure` 화이트리스트를 신설해 판정 방향을 뒤집었다 — **1선(즉시 격리)은 증명된 콘텐츠 실패(출력 예산 소진, 원인 없는/ValueError·TypeError 원인의 LLMError)에만 적용하고, 그 외 전부(모르는 실패 포함)는 2선(시간 유계 스트릭)으로 보낸다.** `LLMNotConfigured` 는 `LLMError` 하위타입이지만 항목과 무관한 구성 오류라 화이트리스트에서 명시적으로 제외했다. 복구 규약 변경을 반영해 계약(api-spec §4.8, v0.27.1 — 새 버전 행 없이 같은 개정 정밀화) 갱신.
- **#381 — combo_matrix 관측 러너가 필터 축을 검색 경계에서 재지 않아 필터 배관 회귀를 못 잡던 문제** — `_observe_chat` 이 항상 고정 3건을 돌려주는 `make_search()` 를 써서 category·price·brand·rating_min 하드필터가 실제로 search 콜러블에 도달하는지 관측하지 못했다(`category` 축은 `#371` 실측 결과 canonical-or-null degrade 로 legs 없이는 항상 `None` 지워져 하네스 전체에서 검색 경계에 도달한 적이 없었다). `fakes.make_recording_filtering_search()`(대역 카탈로그 `PAIR_CATALOG`)로 바꿔 `observed.searchFilters`(경계 도달값, camelCase 8축)·`searchCallCount`·`pushProductCount`·`unappliedSearchFilters` 를 새로 관측하고, `build_decompose_json` 이 `category=="present"` 면 `categoryQueries` 를 채워 `map_categories` 를 exact-match 대역으로 바꿔 leg 를 실현시켰다(`pair_runner.py` 의 구 `_pair_decompose_json` 전용 seam을 이 본체로 흡수, 두 러너가 이제 seam을 공유). `RecordingFilteringSearch` 가 표현 불가 필터(keyword·color·attr_conditions)를 만나면 `ValueError` 로 즉시 실패시키던 것도 고쳤다 — 그 예외가 앱의 검색 실패 처리에 삼켜져 "공허 통과 방지"가 아니라 **공허 통과를 만들고 있었다**(combo-0055 INV 쌍이 base·perturbed 둘 다 `SEARCH_FAILED` 로 우연히 "동일"해 pass하던 실측 발견) — 이제 미적용으로만 기록하고 표현 가능한 축만 적용해 계속한다. `expected_behavior.jsonl` 의 `observed` 를 재실행으로 갱신하는 `refresh-observed` 서브커맨드(덮어쓰기 전용, 판정은 사람이)를 추가했다. 계약(api-spec) 무변경.
- **#376 — combo_matrix 러너가 담기 계열 타임아웃(`degrade=spring_timeout`)에 실 어댑터가 내지 않는 `SpringUnavailableError` 를 주입해 "이 예외를 처리하는가"를 검증하지 못하던 문제** — 실 어댑터(`app/services/spring_client.py::add_wishlist`/`add_to_cart`)는 `httpx.HTTPError`(타임아웃 포함)를 각각 `WishlistError`/`CartError` 로 낙성한다. 주입 예외를 그 실제 타입으로 바꾸고, 러너가 몽키패치하는 두 fake 를 모듈 레벨로 노출해 타입을 직접 잠그는 회귀 테스트를 추가했다(되돌리면 깨짐을 변이 시험으로 확인). `fakes.failing_search`(`SpringUnavailableError`)·`fakes.failing_order_status`(`OrderStatusUnavailableError`)·HOME 주입(`RuntimeError`/`TimeoutError`)은 실 어댑터 규약과 이미 일치해 손대지 않았다. `expected_behavior.jsonl` combo-0004·combo-0056 의 `expected` 문구도 실제 규약에 맞게 정정했다. 계약(api-spec) 무변경.
- **#408 — 401 이 사유 없이 로그에 남아 운영 장애 원인을 분리할 수 없던 문제** — 운영 `POST /chat` 이 게스트·회원 전원 401 `TOKEN_INVALID` 인데 BE 가 인프라(JWKS 200·kid 일치·키 해시 동일·이미지 롤백 무효)를 전부 배제하고도 AI 쪽 사유를 알 수 없었다. `app/api/deps.py` 의 401 매핑 3곳(`get_identity`·`require_seller` 경유·`verify_service_token`)이 예외 타입 + 메시지 + `__cause__` 체인을 WARNING 으로 남긴다 — PyJWT 는 실제 사유(`InvalidSignatureError`·`InvalidAudienceError`·`InvalidIssuerError`·`MissingRequiredClaimError`·`PyJWKClientError`)를 원 예외에만 담고 `core.auth` 가 그것을 `AuthError` 로 감싸므로 종전 로그에는 아무것도 남지 않았다. `requestId` 는 §2.5 오류 봉투·`X-Request-Id` 응답 헤더와 같은 값이라 FE 신고 건과 바로 대조된다. **토큰 원문·서명·클레임 식별자는 싣지 않는다**(회귀 테스트로 고정). 예외 메시지에 섞이는 **서명 검증 이전** 값(`PyJWKClientError` 가 그대로 싣는 JWT 헤더 `kid`, dev 모드 `sub_type`)은 비출력 문자(`str.isprintable()` 기준 — 제어 Cc·형식 Cf·줄/문단 구분자 Zl/Zp)를 이스케이프해 로그 인젝션(CWE-117)을 막는다 — 그 값들은 유효 서명 없이도 공격자가 지정할 수 있어, 개행을 흘리면 가짜 `auth rejected` 줄을 심을 수 있다(PR 리뷰 반영). 같은 줄의 `path` 도 같은 처리를 태운다 — 현재 라우트는 전부 고정 리터럴이라 잠복이지만, path 파라미터 라우트에 이 의존성이 붙는 순간 외부 통제 값이 된다. 검증 로직·계약(api-spec) 무변경 — 관측만 추가.
- **#344 — `category_distance_max` 등 카테고리 거리·마진 임계가 사전 재시드(2,056행 → leaf 1,007행) 이후로 stale이던 문제** — `evals/category_probe` 기준선(`baselines/fast-2026-08-06`, hits.csv 앵커 38셀×N=8·176표본)을 오프라인 스윕한 결과 `category_distance_max`를 0.22 → **0.26**으로 올렸다(single 정답 med 0.2416·q3 0.2579 vs notInCatalog 최소 d1 0.2621 사이에서 nic 무강제 0/40을 지키는 최대 컷 — 거리컷 드롭이 107/176 → **30/176**으로 줄고 채택 정답이 61/176 → **130/176**으로 늘었다, 오답채택은 0 → 8). 이 측정은 이 프로브 176표본 범위이며, 이슈 본문이 인용한 골든셋 150건 컷 통과 회복(#222 별도 실측)은 이번 재측정으로 확인하지 않았다 — 범위 밖. `category_distance_override_margin`(0.035)·`category_select_margin_max`(0.02)는 재검증만 하고 값은 유지했다. 재측정을 `hits.csv` 원시 top-k 거리만으로 런 재실행 없이 반복할 수 있도록 오프라인 스윕 도구 `evals/category_probe/sweep.py`(API·pg·LLM 콜 0)를 신설했다. 계약(api-spec) 무변경.
- **#353 — `embed_texts`가 Google 배치 임베딩 100건/요청 상한을 넘으면 400으로 실패하던 문제** — `_EMBED_BATCH_MAX`(100) 청크로 나눠 순차 호출하고 입력 순서대로 이어붙이도록 고쳤다. eval 도구뿐 아니라 §4.8 I-17 운영 배치 경로(search_doc 임베딩)도 공유하는 잠복 결함이었다. 계약(api-spec) 무변경.
- **#393 (P0) — 카테고리 매핑이 거리컷 등으로 드롭된 턴이 I-3(인기 상품) 우회를 못 타 무필터 I-1(운영 실측 7.74초·12.3MB)을 받아 SEARCH_FAILED 로 떨어지던 문제** — 우회 판정이 decompose 산출(`category_queries` 등 원시 신호)만 보고 매핑 뒤 조립되는 **최종 payload**(실제 Spring 파라미터)는 보지 않아, 매핑이 드롭되면 판정이 어긋났다. 세 조각으로 고쳤다. **A(최소 필터 가드)** — `spring_client.search_filter_axes`(단일 출처, `_search_query_params` 위임)로 이번 턴이 파라미터 0개로 나갈지 판정하고(`search_guard.is_unfiltered_payload`), no_condition(#162)/underspecified(#336) 축에 안 걸리는 `rating_min`·`attr_conditions` 만 있는 턴·매핑이 드롭돼 payload 가 완전히 빈 턴(멀티 카테고리 지목이 모두 실패한 턴 포함)도 인기 상품으로 돌린다 — **의도 판정이 아니라 payload 사실 판정**이라 no_condition/underspecified 와 달리 턴 번호(멀티턴)에도 한정하지 않는다(운영에서 실제로 밟는 되묻기 다음 턴이 바로 이 경로다). 완화 probe 도 payload 가 비면 Spring 을 부르지 않는다. **B(매핑 드롭 0건 폴백, "신발" 시나리오)** — 사용자가 카테고리를 지목했는데 매핑이 leg 를 하나도 못 냈지만 keyword 는 남은 턴(`search_guard.is_category_mapping_dropped`)은 **먼저 keyword 검색을 시도하고, 0건일 때만** 인기 상품으로 대체한다(사전 우회 아님 — 관련 결과가 있으면 그대로 보여준다). `may_auto_relax` 턴(첫 이벤트 앞 직렬 호출 추가)에는 발동하지 않는다(#277 first-token 예산 보존). B 는 payload 축이 `keyword`/가격뿐일 때만 발동한다(`search_guard.is_popular_fallback_safe`) — `brand`·`color` 가 남아 있으면 인기 후보가 그 축을 걸러주지 않아 `conditions` 칩과 실제 후보가 어긋나므로 종전 0건 응답을 그대로 둔다(PR #411 Claude 리뷰). 신규 `category_unmapped_notice` 고지. **C(인기 후보 사후필터)** — `search_service.search_catalog` 의 rating_min/attr_conditions 필터를 `apply_ai_side_filters` 로 추출해 A/B 가 만드는 인기 후보 경로와 공유, 조건 칩과 실제 후보의 표시-실제 불일치를 막았다. 신규 마스터 스위치 `search_filter_guard_enabled`(기본 on, 롤백 스위치) — 끄면 PR #311·#372 가 지키던 종전 무필터 검색 경로가 그대로 재현된다(회귀 테스트로 고정). 이 범위 확대로 PR #311(멀티 카테고리 매핑 실패 턴)·#372(과소지정 되묻기 거부 응답 턴)가 동결했던 무필터 검색 경로 2건의 기대 동작을 인기 상품 폴백으로 갱신했다(검출력은 유지, 종전 동작은 가드-off 회귀 테스트로 보존). `evals/combo_matrix/pair_runner.py` 는 이 가드가 재는 축(후보 소스 라우팅)과 자신이 재는 축(Spring WHERE 필터 배관)이 달라 실행 한정으로 가드를 끈다(케이스·기대값 파일은 무수정, README 문단 추가). #222/#343 확장 폴백의 무필터 재검색(`_run_search_unfiltered`)도 payload 가 비면 Spring 을 안 부르도록 같은 가드를 걸었다 — `category_expanded` 턴은 legs 가 차 있어 A 가 이 재검색을 보호하지 못했는데, 그 재검색도 실제로는 무필터 I-1 이라 결과는 바뀌지 않으면서 사용자 대기만 3초 줄인다(PR #411 Claude 리뷰 2라운드). 카테고리 거리·margin 임계값(#344 소유)·타임아웃 값은 건드리지 않았고, 계약(api-spec) 무변경.
- **#323 — `set_summary` 무잠금 read-then-write 에 per-user `mutation_lock` 추가 — #150 사용자 편집 경로 선결.**
- **#368 — `stream_wishlist_add`만 `SpringUnavailableError`를 개별 처리하지 않아 범용 catch-all(INTERNAL)로 새던 문제(#335 매트릭스 미정의 셀 실측 발견)** — 호출부의 예외 처리 범위를 형제 cart_add(`graph.py:453`)와 통일해, `except (WishlistError, SpringUnavailableError):`로 넓혔다. 기본 어댑터 `add_wishlist`(I-26)는 실패를 전부 `WishlistError`로 내므로 그 경로는 종전과 동일하지만, 주입된 `add_wishlist_fn`(평가 하네스 degrade 주입 등)이 `SpringUnavailableError`를 낼 때는 이 except 없이는 INTERNAL로 새던 것을 기존 `WISHLIST_ADD_FAILED`/`WISHLIST_ERROR` degrade로 끝나게 했다(신규 오류 코드·문구 없음, 형제 cart_add도 어댑터가 내지 않는 이 예외를 같은 이유로 방어한다). 계약(api-spec) 무변경.
- **#343 — 확장 턴에서 검색은 히트를 냈는데 최근구매 exact 제외·소모품 카테고리 억제(`_post_filter`)가 전량을 지워 0건으로 끝나던 문제** — 기존 F-1(#222) 폴백은 억제 **이전** `total_count` 만 봐서 이 갭을 못 잡았다(PR #318 리뷰 R6-4). `candidates` 가 0이 된 확장 턴에 한해 무필터로 1회 재검색하고 그 결과에도 사후필터를 다시 적용(이중 억제)해 채택하며, 억제-이전 F-1 이 이미 재검색을 썼으면 상호배타 가드로 재발동하지 않는다(턴당 무필터 재검색 최대 1회). 신규 `category_expand_post_suppress_fallback_enabled`(기본 on). 계약(api-spec) 무변경.
- **#319 — 배포 이미지에 `db/`가 없어 운영 컨테이너 부팅이 실패하던 문제** — `session_context.initialize()`(#187)가 부팅 시 `db/profile/init/03_chat_session_contexts.sql`을 파일로 읽는데 `Dockerfile`은 `app/`만 COPY 하고 `.dockerignore`는 `db/`를 명시 제외해, 컨테이너 안에서 `FileNotFoundError` → lifespan 실패 → 헬스체크 실패 → 자동 롤백으로 이어졌다(dev→main 승격 #316 사전 점검에서 발견 — 로컬·CI는 repo 루트에서 실행돼 잡히지 않았다). 최종 스테이지에 `COPY db /app/db`를 추가하고 `.dockerignore` 제외를 해제했으며, 빌드한 이미지 안에서 경로 해석·파일 존재를 실측으로 확인했다. 계약(api-spec) 무변경.
- **#84 — 카테고리-무관 리셋 발화가 직전 카테고리로 강제로 좁혀지던 문제** — 멀티턴 승계 가드가 "이번 턴 카테고리 신호 없음"을 **무조건 리파인**으로 읽어, 이어폰을 보던 스레드에서 "5만원 이하 아무거나"라고 해도 이어폰 안에서만 검색됐다(실 LLM 프로브로 먼저 재현: `categoryClear 0/32`). **전용 마이크로 분류기**(`app/agents/buyer/recommendation/category_scope.py`)를 도입해 고쳤다 — "이번 발화가 상품 종류를 놓겠다는 말인가"만 판정하는 짧은 호출을 `decompose` 와 **병렬**로 띄우고, 그 `scopeFree` 를 승계 가드가 소비한다. `clear` 로 확정된 턴은 legs 를 비워 무필터(#22)로 복원한다. 판정 정본은 순수 함수 `resolve_category_action` 하나다(그래프와 프로브가 같은 규칙을 쓴다). 그래프 휴리스틱("아무거나" 키워드 매칭)은 쓰지 않는다 — 표현 열거는 목록 밖 발화를 놓치고 목록을 늘리면 정상 발화가 깨진다(#217 §4.0 과 같은 교훈). 계약(api-spec) 무변경.
  - **결함의 재현과 해소를 대조쌍으로 증명했다 — `evals/intent_probe/baselines/fast-2026-08-05-84/`.** **같은 커밋·같은 프롬프트(`e5e7f9b8d844`)에서 `--no-classifier` 플래그 하나만 바꾼 두 런**이다(**68셀** × N=8 · fast · 픽스처 **v3** 앵커 b · 못 채운 셀 0 · 실패 0). **분류기를 끄면 `categoryClear` 가 0/32 로 #84 결함이 그대로 재현되고, 켜면 32/32 가 된다**(`categoryCarry` 0/32 → **32/32**, `categoryReplace` 22/24 → **24/24**, `categoryAction3Way` 54/120 → **109/120**). 그리고 **기존 8축은 두 팔이 사실상 동일**하다 — `mainIntent` 237/240 · `cartControl` 144/144 · `demonstrative` 93/96 · `optionAnswer` 27/32 · `switchLegacy2` 9/16 · `switchAll7` 36/56 · `cartAddProductIdLegacy2` 14/16 · `orderStatus` 48/48 이 **양쪽 같은 숫자**이고 `general` 만 34 ↔ 31/48 이다. 출고 팔 진단은 `categoryClearOnRefineCount` 0 · `categoryScopeUnresolvedCount` 0 · `reaskProductEchoCount` 10, 끈 팔은 `categoryScopeUnresolvedCount` 120(= 카테고리 전 표본이 판정 없음 — 팔이 실제로 꺼졌다는 기계적 증거). 두 팔의 산출물 6종을 각각 루트와 `no-classifier/` 에 커밋했다.
  - **`decompose` 프롬프트는 한 글자도 바뀌지 않았다(`_SYSTEM` sha12 `e5e7f9b8d844` = dev 판).** 개발 중 인라인 `categoryAction` 필드를 넣어 봤지만 **실측으로 기각**했다. 같은 앵커·같은 64셀로 5개 런을 짝지어 쟀다(분류기 off 1회 = 결함 재현 · dev 프롬프트 + 분류기 on 1회 · 불릿 + 분류기 on 2회 · 출고 구성 1회):
    - **이득 0** — 불릿이 **없는** 런에서도 `categoryClear` 가 이미 **32/32** 였다(분류기만 끄면 **0/32** 로 결함이 재현된다). 3분기 해소는 전적으로 전용 분류기의 성과이고, 인라인 원 산출은 `clear` **0/32** 였다(문면 후보 6종 0~21/32).
    - **손해 확정** — 불릿을 넣은 런은 `PENDING_CART` 중 **상품 전환** 경로가 두 런 모두 깎였다: `switchAll7` 37·38 → **32·32**, `cartAddProductIdLegacy2` 15·14 → **8·8**, 전환 발화가 `cart_add` 대신 `recommend` 로 새는 표본 4~5 → **16~17**(3배). #240 이 "낮추지 말 것"으로 못박은 축이다.
    - smart 티어에서는 인라인 필드가 32/32 였지만 **배포 티어는 fast** 이고 그 티어에서는 이득 0·손해 확정이다. "언젠가 티어를 올리면 이득"은 오늘의 회귀를 사는 근거가 되지 못한다.
    - 프롬프트가 dev 와 바이트 동일하므로 **기존 축 회귀는 구성상 0**이고, 불릿을 지운 뒤 전환 축이 **32/56 → 37/56** 으로 복귀한 것이 그 사실을 확인해 준다(최종 대조쌍에서는 41·42/56).
  - **왜 짧은 전용 호출인가** — 같은 판정을 133줄 `_SYSTEM` 안에서 시키면 fast(gpt-5-nano)가 하지 못한다(문면 후보 6종 실측 0·0·0·1·6·21/32, 21/32 후보는 리파인 3/32·교체 10/24 를 무너뜨렸다). 짧고 초점이 하나뿐인 호출은 fast 에서도 **`clear` 32/32 · 오탐 0/56**(독립 3회 동일)이었다. `needs_expansion`(#198)이 같은 이유로 전용 호출이 된 것과 같은 구조다.
  - **직렬 지연 0** — 분류기는 `prior.category` 와 이번 발화만 있으면 되므로 `asyncio.create_task` 로 decompose 보다 **먼저 띄우고** 뒤에 회수한다. 첫 SSE 이벤트 앞 직렬 합이 늘지 않는다(#277 이 실제로 밟은 자리 — lessons 「상한은 첫 이벤트 앞 직렬 합으로 잰다」). 오류 경로에서는 태스크를 취소·회수해 고아 태스크를 남기지 않는다.
  - **발동 게이트가 좁다** — 스위치가 켜져 있고 · 직전 카테고리가 있고 · 발화가 비어 있지 않을 때만 호출한다. 그 밖의 턴은 **호출 0회**라 첫 턴·prior 없는 스레드·액션-only 턴은 오늘과 완전히 동일하고 비용도 0이다. **옵션 되물음 턴은 막지 않는다** — 사용자가 되물음을 버리고 리셋할 수 있는데("그건 됐고 종류 상관없이 아무거나"), 막으면 그 경로에 결함이 그대로 남는다(분류기 프롬프트에는 `PENDING_CART` 가 실리지 않아 교란 표면이 없고, 산출은 추천 경로에서만 소비된다). 롤백은 `CATEGORY_SCOPE_CLASSIFIER_ENABLED=false` 한 줄(새 튜너블은 그 스위치와 tier·max_tokens 셋뿐).
  - **실패는 오늘 동작으로 떨어진다** — 분류기의 어떤 예외도 밖으로 나가지 않고 `None`(신호 없음) → carry 다. 보조 신호 하나 때문에 무관한 추천 턴이 죽으면 안 된다(`_map_or_empty`·완화 칩 조회와 같은 degrade 원칙). `scopeFree` 는 `is True`/`is False` 만 인정한다.
  - **판정 순서는 네 단계다 — 새 카테고리 지목 > 해제 > 에코 leg > carry.** 리셋 발화의 **30~31/32 가 직전 카테고리를 그대로 복사한 leg** 를 함께 내므로(`_SYSTEM` 의 categoryQueries 불릿이 그렇게 지시한다) 해제가 그 **prior 에코 leg** 보다 우선이어야 `clear` 가 도달 가능해진다. 다만 **새 카테고리를 실제로 지목한 leg 는 해제보다 앞선다** — 그러지 않으면 `"스피커 아무거나 보여줘"` 같은 **혼합 발화**에서 사용자가 말한 카테고리가 통째로 버려진다(라운드 3 실측: 혼합 4발화 32건 중 **19건이 `clear`**, `"스피커 아무거나 보여줘"` 는 8/8). 에코 판정은 `prior.category` 전체·각 조각·`semantic_query` 와 **정규화 후 정확 일치**이며, 채워진 필드가 **전부** 토큰일 때만 에코다(`("음향가전","스피커")` 처럼 상위 조각 + 새 상품인 leg 를 에코로 접으면 수정이 무력화된다). 규칙은 `decompose` 에 **한 벌만** 두고 그래프와 프로브가 같은 함수를 부른다. 잔여 위험의 방향도 정리됐다 — 이제 오탐은 "카테고리가 안 풀림"(사용자가 한 번 더 말하면 된다)이고 **엉뚱한 카테고리로 좁혀지지는 않는다.**
  - **혼합 발화 21/32 — 남은 11건은 가드가 아니라 `decompose` 의 추출 실패다.** 라운드 3 수정 전 13/32 에서 올랐고, 산출물 전수 확인 결과 **새 카테고리 leg 가 있던 15건은 100% `replace`** 였다(가드는 입력이 주어졌을 때 정확하다). `clear` 로 끝난 11건은 leg 가 예외 없이 prior 에코여서(`|무선 이어폰` 등) `"스피커"`·`"노트북"` 이 추출되지 않은 턴이고, **새 카테고리 leg 자체가 없으므로 가드가 지킬 대상이 없다** — `_SYSTEM` 을 건드리는 비용이 실측으로 확인된 이상(아래 인라인 필드 기각) **이 이슈의 범위 밖이며 후속 주제**다. 그 경우 사용자 영향은 "카테고리 없이 넓게 검색"이고 `semanticQuery` 에는 말한 상품명이 남는다. 21 중 6건은 leg 가 에코뿐인데 분류기가 해제 신호를 안 내 `replace`(직전 카테고리 유지)로 끝난 표본이라, **"새 카테고리가 실제로 지켜진 비율"은 15/32** 다(그 구분은 `samples.csv` 의 `categoryLegs` 열로 재집계된다).
  - **동작이 달라지는 지점은 둘뿐이다** — (a) `clear` 로 확정된 턴(카테고리가 풀린다), (b) `conditionActions` 만 있고 `message` 가 빈/공백인 턴이 프로필 세션 버퍼에 빈 문자열을 쌓지 않는다. 그 밖의 턴은 호출도 판정도 오늘과 같다(취향 신호가 0인 발화가 취향 발화를 밀어내는 것은 #119 REQ-PROF-026 이 intent 게이트로 막으려던 것과 같은 결함이다).
  - **실측 프로브(`evals/intent_probe/`)를 배포 경로 그대로 재도록 확장했다** — 앵커 픽스처 **v3**(발화 25 → 40, 컨텍스트 `categoryPrior`, 53셀 → **68셀**), 축 5종(`categoryAction3Way`·`categoryCarry`·`categoryClear`·`categoryReplace`·**`categoryMixedReplace`**), 진단 카운터 2종(`categoryScopeUnresolvedCount`·`categoryClearOnRefineCount`). 프로브도 배포처럼 **decompose + 분류기 두 호출**을 하고 확정값은 `resolve_category_action` 을 그대로 불러 낸다(규칙을 재구현하면 측정과 배포가 갈라진다). `--prompt` 후보 교체가 분류기 문면까지 덮지 않도록 통과 목록을 뒀고, `categoryCarry` 는 prior 에코 leg 를 정답으로 센다(보정이 없으면 축이 정상 동작을 오답으로 읽는다 — 실측 1/32). 픽스처의 `expected.categoryAction` 은 **가드 확정값의 기대치**라 인라인 필드 제거와 무관하게 그대로 둔다.
- **#132 — I-1 전량반환 응답이 이벤트루프를 막고, 3s 타임아웃이 총 수신 시간을 못 막던 문제 (api-spec §4.6·§2.9 c)** — `size` 제거(전량 반환) 이후 응답 항목 수에 상한이 없는데 `resp.json()` 과 N× `SpringProduct.model_validate` 가 `to_thread` **밖**에서 돌아, 한 요청의 파싱이 같은 워커에 붙은 **다른 모든 SSE 스트림**을 그 시간만큼 세웠다. 실 카탈로그(상품 7,220 / 리뷰 126,313)를 로컬에 적재하고 BE 를 띄워 잰 결과(`docs/specs/MEASURE-I1-RESPONSE-132.md`), 필터 없는 질의의 응답이 **13.33 MB · 7,245건**이었고 이를 푸는 동안 루프가 **단일 호출 222ms · 20 동시 5.4초** 멈췄다 — 정지 시간이 파싱 소요와 거의 같아 전 구간이 끊김 없는 단일 블로킹이었다. 둘 다 `asyncio.to_thread` 로 옮겨 **정지를 74%(단일)·55%(20 동시) 줄였다.** 총 소요는 그대로다 — 파싱을 빠르게 만드는 게 아니라 **남의 요청을 막지 않게** 하는 변경이다. GIL 때문에 0 이 되지는 않으며(`json.loads` 는 C, pydantic-core 는 Rust 라 부분적으로만 놓는다) 더 줄이려면 별도 프로세스·스트리밍 파서가 필요한데 현 규모에서 그 복잡도를 살 이유는 없다.
  - **총시간 가드를 걸었다** — `spring_timeout_s`(3s)는 httpx 에 스칼라로 주입돼 connect/read/write/pool 네 시계가 되는데 `read` 는 **청크 사이 간격** 상한이라, 바디가 끊기지 않고 계속 오면 한 번도 물리지 않는다. 전량 반환으로 바디가 커진 뒤로 "3s 안에 끝난다"는 보장이 아니었다. config 는 이미 `spring_timeout_s × (재시도+1)` 을 검색 예산으로 **가정**하고 스트림 상한을 기동 검증하는데 그 가정을 집행하는 코드가 없어, 같은 식을 `asyncio.wait_for` 로 강제했다(새 튜너블 없음 — 검증과 집행이 갈라지지 않게). 가드 타임아웃도 `_transport_status_class` 한 곳에서 `timeout` 으로 분류해 로그·trace 어휘를 유지한다.
  - **개수는 자르지 않는다** — 전량 반환은 BE 합의이고, I-1 응답에는 `ORDER BY` 가 없어 앞에서 M개만 남기면 뒤에 있던 관련 상품이 임베딩 재정렬 기회조차 못 얻는다. 게다가 완화칩 `estCount` 가 응답 길이라(§3.1) 캡은 그 값을 조용히 천장에 붙인다 — api-spec C-15 가 "BE 가 반환 상한을 다시 넣으면 estCount 는 오류 없이 상한값으로 고정된다"고 경고한 실패를 AI 쪽으로 옮기는 셈이다.
- **#89 — fan-out 부분 실패 후 생존 leg 의 후보 폭이 좁아지던 문제** — leg 사전 절단 상한(`leg_limit`)을 요청 시점 leg 수로 정해(`len(legs) > 1` → `category_fanout_per_cat_limit`(10), 아니면 `category_fanout_merge_cap`(30)) 일부 leg 가 `SpringUnavailableError` 로 죽어도 재조정되지 않았다 — 3-leg 중 1개만 생존해도 그 leg 가 여전히 10건에 묶여, 처음부터 단일 카테고리였다면 30건이었을 후보 폭을 잃는다. 생존 수는 gather 이후에야 확정되므로 재조회(1안)는 `filters.limit` 이 Spring 요청 파라미터가 아니라 AI 쪽 절단 knob(§4.6 size 제거, 2026-07-23)이라 같은 필터로 재검색해도 같은 응답만 받아 무의미했다. 대신 leg 사전 절단 상한을 leg 수와 무관하게 `category_fanout_merge_cap` 으로 고정했다 — 한 leg 가 병합 결과에 실을 수 있는 최대치는 `merged[:merge_cap]` 때문에 이미 merge_cap 이 tight bound 라 어떤 생존 패턴에서도 자동으로 맞고, 왕복·지연 회귀는 0이다. hot path(현재 `filters.limit` 미소비)는 결과가 문자 그대로 동일하며, `limit` 을 존중하는 경로(방식1 등)에서는 전 leg 가 생존해도 결과가 바뀔 수 있다 — 짧은 leg 가 섞인 턴에서 병합 후보가 종전 `Σ min(leg, 10)` 대신 merge_cap(30)까지 더 채워진다(축소가 아니라 확대이고 rerank 입력 예산 안이라 회귀는 아니다). 다만 현재 hot path 백엔드(`SpringSearchBackend`·`EmbeddingRerankBackend`)는 `filters.limit` 자체를 읽지 않아 이슈가 보고한 축소는 지금은 관측되지 않는 잠재 결함이었다 — 방식1(`VectorSearchBackend`) 채택 시를 대비한 선제 수정이다. 계약 무변경.
- **#277 — 미룬 턴의 I-1 재시도가 첫 이벤트 10초 상한을 넘기던 문제** — `may_auto_relax` 턴의 본 검색·자동 완화 probe는 기본적으로 재시도하지 않아 첫 이벤트 앞 Spring 직렬 구간을 `2 × 3s = 6s`로 묶는다. 변경 전 실최악은 10.01s에 이벤트 0건·504가 8/8이었고, 변경 후 같은 시나리오는 p50 3.40s·200 `conditions`+`error(SEARCH_FAILED)`, 새 최악(두 호출이 각각 2.9s 성공)은 p50 6.97s·200 정상 답변이었다. 대가로 재시도가 살리던 검색 장애는 6.80s 정상 답변에서 3.40s retryable degrade로 빨리 떨어진다. (api-spec §2.9 c, v0.20.2)
  - `SEARCH_RETRY_ON_DEFERRED_CONDITIONS=true`로 종전 동작을 복구할 수 있지만 이 조합은 기동 시 직렬 합으로 검증돼 기본 타임아웃 그대로면 기동이 막히고, 구매자 `progress` 이벤트가 계약에 등재돼 검색 전 첫 프레임을 낼 수 있게 되면 스킵을 원복할 수 있다. 와이어 계약은 불변이며 직렬 합 검증·타임아웃 재배분은 #288에 남긴다.
- **#288(부분) — 첫 이벤트 앞 직렬 I-1 호출 수 검증을 상수 `2`에서 config 파생 일반형으로 확장** — 이슈 본문이 지적한 "단일 호출 예산만 본다"는 결함은 실은 #277 리뷰 4차에서 이미 직렬 합 검증으로 고쳐져 있었고, 그때 남은 델타는 계수 `2`가 하드코딩이라는 점 하나였다. `_deferred_first_event_i1_calls(rounds, auto_fields, chip_fields)`를 순수 헬퍼로 분리해 `1 + min(relaxation_max_rounds, |relaxation_auto_fields ∩ relaxation_chip_fields|)`로 계산한다 — 후보 생성기가 `relaxation_chip_fields`만 순회하므로 칩 목록에 없는 자동 필드는 애초에 후보가 안 생겨 교집합으로 세고, 루프가 `rounds`에서 break하므로 `min`으로 상한을 씌운다. 오늘은 `_forbid_auto_relaxing_explicit_constraints`가 자동 목록을 `{ratingMin}` 부분집합으로 잠가 값이 언제나 2이지만, 그 허용 목록이 넓어지는 순간 상수는 조용히 과소평가되어 #277이 없앤 이벤트 0건·504 조합이 되살아난다 — 계수를 다른 검증기의 허용 목록에 암묵적으로 의존시키지 않기 위해 일반형을 쓴다. 검증 게이트도 `relaxation_max_rounds > 0 and relaxation_auto_fields`에서 `calls == 0`(= `graph.py`의 `may_auto_relax`와 동치, 더 정확)로 바꿨다. 계약 변경 없음(api-spec §2.9 c 근거는 그대로) — 이슈 #288 자체는 타임아웃 재배분 등 잔여 후보가 남아 열려 있다.
- **#266 — 판매자 general 레인 LLM 타임아웃이 `INTERNAL` 로 나가던 문제 (api-spec §2.9 c)** — 이 레인만 앱 벽시계 상한이 없어 스트림 전체 90s 에만 의존했고, `except (TimeoutError, asyncio.TimeoutError)` 분기는 **도달 불가 코드**였다. `asyncio.wait_for` 는 쓸 수 없다(중간에 yield 하는 async generator) — 청크 루프 전체를 `asyncio.timeout(seller_general_timeout_s)` 으로 덮어 빌드·체크포인터 연결까지 묶었다. 기본값 20s 는 2026-08-02 로컬 실측 general total max 2.55s 의 약 8배다.
  - **인프라 장애를 LLM 지연으로 감추지 않는다 (PR 2차 리뷰)** — `get_checkpointer()` 는 운영(`auth_mode=jwks`)에서 폴백 없이 raise 하는데, 그때 나오는 `asyncio.TimeoutError` 는 LLM 타임아웃과 **타입이 같아** 원리적으로 구분할 수 없다(`is_state_store_unavailable` 도 `TimeoutError` 를 포함해 여기서는 쓸 수 없다 — 썼다면 반대로 진짜 LLM 타임아웃까지 삼킨다). 체크포인터 초기화를 general 상한 밖으로 빼고 `_CheckpointerUnavailable` 로 감싸 **발생 지점**으로 가른다. pg-profile 장애는 `INTERNAL` + `seller_checkpointer_unavailable`(ERROR)로 나가고, 반대 방향 오분류(LLM 예산 소진을 인프라 장애로 기록)도 함께 막힌다. 이 오분류는 이 이슈 이전부터 있던 것으로 회귀가 아니다.
  - **체크포인터 `setup()` 도 상한 안에 넣는다 (PR 3차 리뷰)** — 위 "상한 밖으로 뺀다"는 결정은 *초기화 전체가 유한하다*에 의존하는데, `_init_checkpointer` 의 `wait_for` 는 `__aenter__`(연결)만 감싸고 있었다. 콜드 DB 에서 `setup()` 은 MIGRATIONS 8종을 순차 실행하므로 문장당 `statement_timeout`(3s)씩 누적돼 상수가 뜻하는 5s 를 넘고, 그러면 SSE 캡이 in-stream `error` 없이 `done(stop)` 으로 끊는 원래 실패 모드가 콜드스타트에 재현된다. 이웃 `pg_store.py` 와 같은 형태로 `setup()` 을 동일 상한으로 감싸고, 실패 경로에서 `ctx.__aexit__` 정리를 추가해 커넥션 누수도 함께 막았다.
  - **general 레인 직렬 예산을 기동 시점에 고정한다 (PR 리뷰)** — `_require_general_lane_within_stream_cap`. `seller_route_timeout_s + 2 * seller_checkpoint_connect_timeout_s + seller_general_timeout_s` 가 `stream_total_timeout_s` 이상이면 기동 실패다(기본 40 < 90). SSE 캡이 레인 상한보다 먼저 끊으면 매핑된 `LLM_TIMEOUT` 이 다시 오류 코드 없는 `done(stop)` 절단으로 퇴행한다. 세 값이 **직렬로 쌓인다** — general 단독 비교로는 `route=10 + general=85` 같은 조합이 통과해 검증이 이름만 남는다(첫 이벤트 `meta{lane}` 이 라우팅 **뒤**에 나가고, 체크포인터 초기화는 상한 밖이다). 체크포인터가 `2 *` 인 것은 연결과 `setup()` 을 각각 그 상한으로 감싸기 때문이다.
  - **타임아웃 판정을 문자열이 아니라 타입으로 한다** — `core/llm.py` 에 `is_timeout_error()` 를 신설했다. provider SDK 의 메시지는 `"Request timed out."`(timed **out**)이라 `"timeout" in str(exc)` 로는 걸리지 않고 `httpx.ReadTimeout` 은 `str` 이 비는 경우가 있어, 가짜 예외로 쓴 테스트만 통과하고 실제 SDK 예외는 한 번도 통과시켜 본 적이 없는 판정이 된다. 설치된 SDK 만 지연 수집해 타입 비교하며, `LLMError(str(exc)) from exc` 로 감싸인 경우를 위해 원인 체인(`__cause__`/`__context__`)을 순환 방어와 함께 따라간다. 전제(`httpx.TimeoutException` 이 내장 `TimeoutError` 의 서브클래스가 **아님**)도 테스트로 고정해 라이브러리 업그레이드 시 먼저 깨지게 했다.

### Added
- **#281 — 니즈 `priority` 기반 예산 제외 순서 (#60 후속)** — `budget_sets.build_budget_sets` 는 총액 예산이 모든 니즈를 못 담을 때 "최저가가 비싼 니즈부터"라는 결정론적 순서로만 제외해 SPEC-RECOMMEND-001 REQ-REC-075/076("1 필수는 최후")을 충족하지 못했다. keyword-only `priorities: Sequence[int] | None` 인자를 추가해(길이 불일치·bool·범위 밖 값은 엄격 폴백 — 부분 수용은 어긋난 정렬을 만들 뿐이라 오늘 동작이 낫다) 제외 키를 `(priority, min_price, -leg)`로 바꿨다 — `priorities=None`이면 기존 두 요소만 남아 **바이트 동일** 산출이라 회귀 0이다. 전용 마이크로 분류기 `app/agents/buyer/recommendation/need_priority.py`(`category_scope.py`(#84)와 같은 구조 — 자기 예외 삼킴·호출 전 관측·`asyncio.create_task`로 rerank 뒤에 숨겨 직렬 지연 0)를 신설해 신호를 만들고, `graph.py`의 `stream_recommendation`에 `try/finally`로 태스크 생명주기를 감싸 조기 종료 시 고아 태스크를 남기지 않는다(`_cancel_priority_task`). 게이트는 `need_priority_classifier_enabled and budget_set_enabled and buy_all and total_budget is not None and split_by_need` — 거짓이면 호출 0회. 제외 고지는 기존 `budget_set_dropped_notice`(`token` SSE)를 그대로 쓴다(계약 무변경). 롤백은 `NEED_PRIORITY_CLASSIFIER_ENABLED=false` 한 줄.
  - **정본 이탈 고지 — SPEC-RECOMMEND-001 결정 14-H/REQ-REC-004("LLM 추가 호출 없음")·AC-REC-37("별도 분류 LLM 호출 금지")에서 이탈한다.** 이탈 근거는 실측(`evals/priority_probe/`, 아래) — 인라인(정본이 규정한 "decompose 프롬프트에 필드 추가") 방식은 본질 축 `priorityOrderPairs`·`essentialProtected`가 4런 중 2런에서 사실상 0이라 REQ-REC-076을 지킬 수 없었고, 전용 분류기만 채점이 성립했다. 선례는 **#84**(PR #307, 머지됨) — `category_scope` 전용 분류기를 같은 이유(인라인이 fast 티어에서 무동작)로 도입하고 SPEC을 개정하지 않은 채 출고했다. **정본(Notion) 반영은 후속 과제** — `docs/specs/SPEC-RECOMMEND-001.md`는 사본이라 이 레인에서 편집이 금지돼 있다.
  - **실측(`evals/priority_probe/`, TASK 3) — 인라인 vs 전용 분류기를 실 LLM 반복 분포(fast·N=8·12셀)로 대조했다.** 분류기 2런: `priorityOrderPairs`(본질 축, "제외 순서" 자체 — REQ-REC-076이 요구하는 것은 절대값이 아니라 순서다) 189/288(65.6%)·194/288(67.4%), `essentialProtected`(REQ-REC-076 "1 필수는 최후") **103/104(99.0%)·104/104(100%)**. 인라인 2런은 두 축 모두 사실상 0(`0/288`·`1/104`) — 원인은 모델 역량이 아니라 `decompose()`가 픽스처 `needs`를 입력으로 받지 않고 **자기 leg를 스스로 만들기 때문**으로, `rawLegs` 원시 기록(`samples.csv`)에서 상황형 발화를 픽스처가 기대하는 품목 단위로 쪼개지 못하고 포괄어 하나로 뭉뚱그리는 것을 확인했다(채널 유무와 무관하게 재현). 게다가 그 leg는 애초에 knapsack이 쓰는 최종 leg도 아니다 — `buyer/graph.py::_prepare_recommendation`이 `map_categories`(canonical 보정·거리컷·`dedup_truncate`)와 조건부 `needs_expansion`(#198/#217, 별도 LLM 호출) 합집합을 거쳐 `decision.category_legs`를 다시 만들므로, 인라인이 완벽히 태깅해도 그 신호를 배달할 경로가 구조적으로 없다. 분류기는 `needs`를 직접 입력받아 이 leg 정합 문제 자체가 구조적으로 없다. 채택 근거·기준선 4런·초판 하네스 결함 2건(전송 실패가 표본으로 오염됨·leg 개수 불일치가 이름 매칭까지 버림)과 정정 과정은 `evals/priority_probe/README.md`에 전부 남겼다 — 숨기지 않는다(#240 규약).
  - **`evals/intent_probe`(#260)를 import로 재사용한다** — 113줄 슬라이딩 윈도 페이서(`GlobalPacer`)와 프롬프트 교체 래퍼(`SystemPromptOverrideLLM`)를 다시 쓰지 않고 그대로 가져다 썼다. #300이 그 디렉터리를 옮기면 import 한 줄이 깨진다는 결합을 README에 명시했다. #300의 "프로브 중복 제작" 교훈에 비추어 통합은 후속 과제로 남긴다(착수 시점 #300 미머지 · 측정 대상과 픽스처 스키마가 다름).
- **#162 — 조건이 하나도 없는 추천 발화를 인기 상품·취향 기반 후보로 처리 (api-spec §4.17, v0.23.1)** — "아무거나 추천해줘"는 decompose 프롬프트상 `general`이 아니라 **추천 레인**으로 가는데, 필터가 전부 비어 `_search_query_params`가 파라미터 0개를 만들고 그대로 I-1에 나가 매칭 전량(실측 **7,245건·13.33MB·1.112s**)을 받고 있었다. 그 상위는 사용자 의도와 무관하고 **rerank로 구제되지 않는다** — 후보 집합이 병목이라 30개가 이미 무작위면 그 안에서 뭘 골라도 무작위다. 에러도 0건도 아니라 겉보기엔 정상이라(후보가 비지 않아 zero-result 분기도 degrade 고지도 안 탄다) 아무도 문제로 인식하지 못했다.
  - **새 기능이 아니라 계약 위반 시정이다** — I-1 정본은 2026-07-27 후보 수 상한을 폐지하며 **"정형조건이 하나도 없는 요청은 LLM 단에서 차단하므로 BE는 별도 가드를 두지 않는다"**를 전제로 걸었고, 0건 시 폴백 대상으로 I-3를 직접 지목했다. BE가 그 전제로 상한을 없앤 자리에 AI 쪽 차단이 없었다.
  - **후보 소스 4분기** — 게스트 / 프로필 없는 신규회원 / 프로필은 있으나 **벡터가 없는** 회원(구 요약·임베딩 실패)은 **I-3 인기 상품**, 취향 벡터가 있는 회원은 **자체 카탈로그 인덱스에서 벡터 근접**(홈 추천 I-22와 같은 엔진·같은 인덱스)으로 뽑는다. 메인 화면과 채팅이 다른 결과를 주는 인지 부조화를 막고 어차피 만든 자산을 재사용한다. 취향 벡터는 `read_profile_summary()`가 이미 돌려주던 값인데 `markdown`만 쓰고 버리고 있었다 — 추가 조회도 임베딩 API 왕복도 없다.
  - **트리거는 값이 아니라 출처로 판정한다** — `filters` 하드필터 축(`decompose._FILTER_AXES` 재사용)·`category_legs`·**`RouteDecision`에 실린 지목 축**(`category_queries`·`repurchase_products`·`revert_categories`)이 비고 **첫 턴**이어야 하며, 여기에 `semantic_query`가 **원문 폴백인지**를 함께 본다. `category_queries`(매핑 **전** 원시 신호)를 함께 보는 이유는 매핑이 실패하면 `category_legs`가 그것을 대표하지 못하기 때문이다 — "이어폰이랑 노트북 추천해줘"처럼 상품을 2개 이상 지목한 턴은 `cat_signal` 승격이 leg 1개 조건에 걸려 출처 검사도 통과하므로, 두 카테고리가 모두 매핑에 실패하면 사용자가 말한 상품군을 통째로 버리고 인기 상품이 나갔다(PR #311 리뷰, 재현 확인). decompose는 `llm_sq or cat_signal or prior_sq or query`로 채워 아무 신호가 없어도 발화 원문이 들어가므로("아무거나 추천해줘" → `semantic_query="아무거나 추천해줘"`) 값의 유무로 판정하면 **프로덕션에서 영영 발동하지 않는다**(구현 중 실제로 그 상태였고 `docs/lessons.md`에 기록했다). `RouteDecision.semantic_query_is_fallback`을 신설해 출처를 실어 보낸다. "여름에 시원한 거 추천해줘"(semanticQuery 있음)와 멀티턴 승계 턴이 트리거되지 않음을 회귀 테스트로 고정했다 — 멀티턴 세 의도(리파인/칩 제거/카테고리-무관 리셋) 구분은 #84 소관이라 이 경로는 첫 턴 한정이다.
  - **취향 경로는 검색도 rerank도 타지 않는다** — `rank_candidates`가 주는 건 `productId`뿐이고 상품 원본을 채울 방법이 없다(AI 인덱스에 원본 컬럼을 두지 않고, id로 Spring에 되묻는 API는 C-17로 요청했다가 #32에서 기각). 홈과 똑같이 `extras` 재료로 근거를 고른다(LLM 호출 0회). 그 대가로 **소모품 카테고리 억제**(extras에 categoryName 없음 — 최근구매 exact 제외는 적용된다)와 **개인화된 근거 문장**(시그널이 없어 리뷰 장점 폴백 — 개인화는 랭킹에 있고 문장에 있지 않다), **이름 지칭 담기**("그 이어폰" — id 기반 담기 가드 #118은 살아 있다)를 포기한다. 홈도 동일하게 포기한 것들이다.
  - **투명 안내는 발신 자체가 계약이다** — 문구는 config 주입이되(`no_condition_notice_popular`·`no_condition_notice_profile`, 후보 소스가 달라 둘로 나눔) 빈 값이면 기동을 막는다(`_require_degrade_notices_present`, #133과 같은 규약). 없으면 사용자가 인기 상품·취향 기반 결과를 **자기 조건이 반영된 결과로 오해**한다. 단 I-3 장애로 무필터 검색에 떨어진 턴에는 "인기 상품으로 보여드릴게요"를 **내지 않는다** — 그 결과는 인기 상품이 아니라 거짓 주장이 된다.
  - **0건은 성공이다** — 정본 §4.17가 "빈 배열도 정상 결과다. 카드 없이 텍스트만 답하면 된다"로 못박는다. 여기서 degrade로 처리하면 이 이슈가 없애려는 13.33MB 호출을 도로 부른다. I-3 장애·취향 랭킹 실패/0건만 폴백을 탄다(`mark_degraded("popular_fallback")`·`("profile_ranking_fallback")`).
  - **`popular_candidate_size`(기본 30, `gt=0`)** — `gt=0`이 방어다: BE I-3에는 범위 검증이 없어 음수·0을 보내면 400이 아니라 빈 배열이 와 "인기 상품이 없음"으로 위장된다. 기본값은 양쪽 경계에서 나왔다 — 하한은 노출(`expose_max` 9)+dedup 여유(BE 기본 12는 여유가 3뿐), 상한은 `embedding_rerank_limit`(30)으로 그 이상은 압축 단계에서 버려진다. dedup 손실은 `orders` 0행이라 아직 실측하지 못했고 구매 이력이 쌓이면 상향 후보임을 주석에 남겼다.
  - **총액 예산만 말한 턴은 세트가 아니라 대안으로 (PR #311 리뷰)** — "총 5만원 있어 아무거나 추천해줘"는 인기 상품 중 **예산 이하만** 남겨 `PICK_ONE`으로 보여주고 대화로 되묻는다(`totalBudget`은 push에 싣지 않는다). 조합을 만들지 않는 이유는 **고를 기준이 없기 때문**이다 — 니즈가 정해진 턴("감자탕 재료 총 5만원")과 달리 무엇을 몇 개 살지 사용자가 말하지 않아, 세트를 지어내면 "이어폰+샴푸+등산화 합쳐 5만원" 같은 무관한 묶음이 된다. 취향 벡터 경로는 이 턴에서 **막는다** — AI 카탈로그 인덱스에 가격이 없어 예산을 원리적으로 확인할 수 없다. 리뷰는 판정 자체를 막으라고 했으나, `build_budget_sets`(#60)가 `split_by_need`(니즈 2개 이상)를 요구해 **조건 없는 턴은 어느 경로로 가도 예산 세트가 만들어지지 않으므로** 막으면 무필터 I-1(13.33MB)만 되살아나고 예산은 그대로 무시된다.
  - **와이어 계약 불변** — 새 SSE 이벤트도 새 스키마도 없다. I-3 응답이 I-1과 동일 DTO라 파서·하류(dedup·rerank·I-21 push·`products.ready`)를 그대로 재사용한다. 상품 카드는 여전히 CH-5(경로 B).
- **#289 — 구매자 SSE `progress` 이벤트 신설(2026-08-05 정본 등재 완료 → 사본 §3.1 동기화, 플래그 기본 off)** — #277 이 응급 처치로 미룬 턴의 I-1 재시도를 스킵해 첫 이벤트 10s 관문을 임시 봉합했는데, 이 PR 은 근본 원인(구매자 스트림에 "서버가 뭘 하는 중인지" 알리는 신호가 전혀 없어 `conditions`가 사실상 첫 생존 신호를 겸했던 구조)을 계약 신설로 해소한다 — 애초에는 초안+구현+실측을 먼저 만들어 FE/BE 협의 자료로 쓰는 선행 구현이었고, **2026-08-05 정본(Notion CH-2)에 합의·등재가 완료돼** `docs/api-spec.md` §2.2·§3.1·§2.9(c)를 동기화했다(api-spec §2.2·§3.1·§2.9 c, v0.21.0). `settings.progress_events_enabled`(기본 `False`)는 **여전히 켜지 않았다** — **꺼진 상태에서 와이어 바이트가 완전히 동일함을 회귀 테스트로 고정**했다(`tests/unit/test_progress_event.py`).
  - **emit 지점 = `run_buyer_turn`의 decompose 직전, 세션 프렐류드 뒤** — 스트림 최상단에 두면 200 헤더가 먼저 나가 `SessionStateUnavailable`(503 `STATE_UNAVAILABLE`, §2.5 스트림 전 오류 봉투)이 in-stream `error`로 바뀌어 계약을 깬다. decompose 직전에 두면 그 봉투는 그대로 보존되면서, 관문(first-token 10s, §2.9 c)에서 LLM head(#151 p95 ≈3.0s)·I-1 검색·재시도·자동 완화 probe 전 구간이 빠진다. 켜졌을 때의 stage 는 `analyzing`(요청을 확인하고 있어요) 1종만 구현했다 — `run_buyer_turn`은 추천 외에 담기·주문조회·일반 대화도 같은 decompose 진입점을 거치므로, 이슈 본문이 제안한 `searching`을 그 자리에서 내면 비추천 턴에 "검색 중"이라고 거짓 표시하게 된다.
  - **운영(jwks 인증 또는 staging/production 환경) 기동 가드** — `progress_events_enabled`는 평범한 `bool`이라 `.env` 한 줄로 뒤집히는데, 정본 등재·FE 미지 `type` 무시 확인이 끝나기 전에는 그걸 막는 게 사람의 규율뿐이었다. `_require_pepper_in_prod`(pepper·internal token·jwks_url·google_api_key와 같은 fail-closed 관용구)에 `auth_mode == "jwks"` **또는** `app_environment`가 `staging`/`production`이고 이 플래그가 켜져 있으면 기동을 실패시키는 분기를 추가했다 — `auth_mode`는 인증 방식일 뿐 실트래픽을 보장하지 않아(dev 인증으로 도는 staging도 있다) 두 축의 합집합으로 넓혔다(리뷰 4차 지적 반영). 등재 후 플래그를 켜는 절차의 일부로 이 가드를 지운다.
  - **실측(`evals/first_event_budget/`, #277 하네스 그대로 재사용)** — 대표 6개 시나리오 전부에서 flag-on 첫 이벤트 p50 이 ~12~15ms 로 수렴했다(`D3_deferred_worst_no_retry` 6869.8ms→11.6ms, `A_nondeferred_fast` 384.4ms→12.8ms). emit 지점이 decompose 자체보다 앞이라 이슈가 예고한 "미룬 턴만 A/G 수준(0.4s대)으로 수렴"보다 개선폭이 크다 — 관문 밖으로 빠지는 것은 decompose LLM 호출(#151 head)·재구매 store pg 왕복·category mapping·I-1 검색·재시도·자동 완화 probe 다. 세션·스레드·장바구니 프렐류드 pg 왕복과 회원 턴의 `read_profile_summary`는 emit 보다 **앞**이라 여전히 관문 **안**이며(`app/agents/buyer/graph.py` L462 `read_profile_summary` vs L531 `progress_frame` yield), flag-on이 0ms가 아니라 ~12ms인 것이 바로 그 프렐류드 비용이다. 평상시 ~12ms지만 직렬 상한은 4×3s=12s라 pg-profile 장애 시에는 여전히 관문을 넘길 수 있다 — #289는 이 경로를 좁힐 뿐 없애지 않으며, 없애려면 §2.5 봉투를 줄이는 계약 결정이 필요하다(초안 §4). 이 하네스는 `ScriptedLLM`이라 LLM head 절감(#151)은 수치에 잡히지 않으므로 실제 운영 이득은 이보다 크다. 산출물: `evals/first_event_budget/results/measure-289-20260805-flag-{off,on}.json`.
  - **초안 문서** `scratchpad/draft-progress-contract.md` — 이벤트 스키마(`{"stage","message"?}`)·stage 어휘(이 PR 은 `analyzing` 단독, `searching`/`relaxing`/`reranking`은 미구현)·순서 계약 영향(기존 7종 상대 순서 불변, #277 이 고정한 순서 테스트 2개 유효)·판매자 `progress`(`{"text"}`)와의 페이로드 불일치 및 통합 선택지·🔴 FE 미지 `type` 무시 여부 확인 필요·#277 재시도 스킵 원복 조건(이 PR 은 원복하지 않는다)을 담았다.
- **#132 — 평점을 명시한 턴에서 무평점 상품이 노출되면 근거문이 그 사실을 고지한다** — #100 P0 이 rating 사후필터를 '반증된 것만 제거'로 바꾸면서(무평점 보존, #171) "평점 4.5 이상"이라 **말한** 사용자에게도 리뷰 없는 신상품이 그대로 올라온다. 기존 rerank 는 그 후보에 `ratingLevel: 평가없음` 을 주고 "평점을 근거로 삼지 말라"고만 지시해 **거짓 주장은 막았지만 고지는 하지 않았다** — 사용자는 4.5↑ 라 믿고 무평점 상품을 본다. 자동 완화는 이미 `relaxation_notice` 로 고지되는데 이쪽만 조용하던 비대칭을 없앤다. 문구는 config(`rating_unrated_disclosure_notice`, 빈 값이면 끔).
  - **고지는 코드가 보장한다** — 프롬프트 지시(평점 명시 턴에만 user 메시지에 덧붙임, `_PROFILE_TIEBREAK` 와 같은 규약)는 문장을 자연스럽게 만드는 보조일 뿐이다. LLM 이 무시할 수 있고, 무엇보다 **rationale 이 빈 검색순서 보충 카드는 `_reasons()` 에서 통째로 빠져**(PR #212 리뷰) 고지가 실릴 자리조차 없었다 — 하필 그런 카드일수록 사용자가 근거 없이 신뢰한다. 무평점 상품만 예외적으로 근거가 비어도 항목을 만든다.
  - **상한을 넘으면 근거를 자르고 고지를 남긴다** — `_sanitize_reason` 에 통째로 맡기면 뒤쪽인 고지가 먼저 잘려 나가 아무 일도 안 한 것과 같아진다. 다만 고지 자체가 `reason_max_len` 보다 길면 상한이 이긴다(신뢰경계를 넘는 자유 텍스트의 방어캡이라 UX 문구가 뚫을 값이 아니다).
- **#118 — 화면 맥락(`screen`) 수신과 지시어 해소** — 채팅은 좌(대화)/우(패널) 분할이라 사용자는 우측을 보며 "이거"라고 말하는데, 그 지시어는 발화만으로 확정할 수 없었다(경로 B라 SSE에 카드가 없고 AI는 상품 카탈로그를 갖지 않는다). 정본 CH-2가 신설한 `screen{pageType, filters?, products?, columns?}`을 구매자·판매자 공용 요청 필드로 받아, 담기 허용 목록을 **(누적 추천 목록 ∪ `screen.products`)** 로 넓히고 "이거·3번째 거·3번째 줄 2번째·이름" 지시어를 해소한다. 유효성은 **관대**하게 — `pageType`이 14종 밖이면 screen 전체를 무시하고 200으로 진행하며 **어떤 경우에도 400을 내지 않는다**(`conditionActions`의 엄격함과 정반대). 판매자 레인은 `pageType`·`filters`를 supervisor·planner **입력 메시지에만** 실어 "이 목록 왜 비어?" 류에 답한다(§9.1 이력 주입 선례 — 프롬프트 파일 무변경, `products`는 싣지 않는다). (api-spec §3.1·§3.2, v0.15.26 등재 계약의 수신 구현 — 와이어 계약 불변)
  - **담기 가드는 넓히되 프리패스는 아니다** — 두 목록 밖 id는 여전히 차단하고, 모호하면 되물음한다. 실제 담기는 I-2가 재고·판매상태를 다시 검증한다. 관대 무시로 사라진 screen의 products가 allowed에 새지 않는 것도 회귀 테스트로 고정했다.
  - **`last_reco`를 스레드 내 누적으로 바꿨다** — 덮어쓰기였을 때 "추천 A → A의 상품 질문 → 추천 B → 이거 담아줘"가 차단됐다(가드가 시간 축을 잃는다). 최근 언급 순으로 누적하되 **이번 턴 항목은 상한에 잘리지 않는다** — I-21은 한 턴에 10목록×9상품=90건을 밀 수 있어 단순 절단이면 방금 추천한 상품이 담기 차단되는 회귀가 된다.
  - **가드는 누적, 프롬프트는 실측대로** — 누적 목록을 `decompose` 프롬프트에도 그대로 실었더니 실 LLM N=8 프로브에서 `PENDING_CART` 중 상품 전환이 6/8 → 1/8로 무너졌고, 승계분 상한을 6건으로 줄여도 2/8이었다(**2건만 붙어도** 깨진다). 정본이 누적을 요구하는 대상은 `allowed`(가드)이지 프롬프트가 아니므로 둘을 갈랐다 — 되물음 턴에는 승계분을 싣지 않고, 아닌 턴에는 누적을 실어 위 4단계 시나리오를 해소한다. 경계는 저장값에 `turn_count`를 **덧붙여** 나르며 구버전이 쓴 행은 전량을 이번 턴으로 보고 오늘 동작으로 degrade한다(롤링 배포 양방향 안전). 이 프로브의 `before`가 #240 이슈 본문 기준선보다 낮게 나오는 것은 이 PR의 회귀가 아니라 앵커 재구성·프롬프트 판 차이 때문이며(#260이 규명 — 같은 프롬프트 해시로 재면 5개 축이 #240과 정확히 일치하고, 어긋난 축은 전부 재구성한 부분이다), 같은 시점 #260 기준선과는 수치가 맞는다(`cartControl` 144/144 · `orderStatus` 48/48 · `optionAnswer` 27/32 대 25/32).
  - **결정적으로 풀리는 지시어는 코드가 푼다** — 순번·좌표·"후보 1건이면 확정, 여러 건이면 되물음"을 LLM에 맡겼더니 실패의 대부분이 "null로 두고 되물음"이 아니라 **목록 안의 다른 상품을 자신 있게 확정**이었다(가드가 막지 못한다 = 사용자가 말하지 않은 상품이 담긴다). 이 부분을 `screen_reference`로 떼어내 신규 지시어 해소가 9/48 → 48/48이 됐다. 이름 매칭만 LLM에 남겼다(프로브에서 8/8).
  - **[리뷰 반영] 코드 해소기의 발동 조건을 좁혔다** — 초판은 결정적이지 않은 입력까지 삼켰다. ① `"아까 추천해준 그거 담아줘"`가 화면 상품으로 확정됐다 — `"그거"`는 이 저장소에서 대화 지시어로 확립돼 있어(하중 문구·#234 프로브) 기본 표지를 근칭으로 좁히고 대화 참조 표지가 있으면 해소를 건너뛴다. ② `"무선 이어폰 2번째 옵션으로 담아줘"`에서 옵션을 수식하는 `"2번째"`가 화면 순번으로 읽혀 다른 상품이 담겼다 — **이름 지목이 있으면 순번·좌표를 적용하지 않는다**(강한 신호를 약한 신호로 덮지 않는다). ③ `"10만원대 무선 이어폰 담아줘"`의 숫자를 상품 id로 오인해 정상 발화가 되물음으로 막혔다 — 접미 목록을 늘리는 대신 "앞뒤에 문자가 붙지 않은 토큰"이라는 구조적 성질로 배제해 새 단위가 나와도 자동으로 걸러진다.
  - **되물음 문구를 상황에 맞게 갈랐다** — 화면에 상품이 보이는 상태에서 "추천을 먼저 받아보시면"이라고 답하면 되묻긴 해도 무엇을 물어야 할지 알 수 없다. 화면 후보가 있을 때와 말한 id를 못 찾았을 때를 나누고, `screen` 없는 경로의 문구는 **바이트 동일**하게 유지했다(회귀 테스트로 고정).
  - **[PR 5차 리뷰] 옵션 되물음(`PENDING_CART`) 중에는 `screen.products`를 담기 허용 목록(`allowed`)에서 뺀다** — 정본 §3.1 [보안] 문면은 "(누적 추천 ∪ `screen.products`)를 allowed로 취급"이라 이 예외는 문면과 어긋나지만, 되물음 중 화면 id가 allowed에 있으면 발화 속 임의 숫자 오추출이 그 id와 우연히 일치할 때 진행 중이던 옵션 되물음이 조용히 버려지는 오담기가 재현돼(`app/agents/buyer/graph.py` 상세 주석) 그 문단의 목적(오추출 차단)을 지키는 방향으로 좁혔다. **정본(Notion CH-2) 반영됨(2026-08-05)** — 담기 가드 문단 바로 아래 '되물음 예외' 문단으로 등재됐다(api-spec §3.1 [보안]에 AI 구현 주석으로 병기).
- **#260 — intent 라우팅 프로브를 리포에 고정 (`evals/intent_probe/`)** — `decompose` 의 라우팅 정확도는 실 LLM 반복 분포로만 측정되는데(`FakeLLM` 테스트는 프롬프트를 어떻게 바꿔도 통과한다), #234·#240 이 각각 만든 측정 스크립트가 커밋되지 않아 재현이 불가능했고 서로 다른 정답지를 써서 **채택 판정이 뒤집힌** 사고가 있었다(되물음 상품이 목록 1번이냐 2번이냐만으로 `일반형` 정답률 8/8 ↔ 3/8). 이제 앵커(발화 25 × 컨텍스트 3 = 53셀)를 JSON 데이터로 커밋하고 스크립트가 그것만 읽는다. 재현을 깨는 함정 4가지를 코드에 박았다 — 전역 슬라이딩 윈도 페이서(없으면 429 로 표본이 빈다. 기본 45rpm · 콜당 3.9k 토큰 추정은 실측으로 잡았다 — provider TPM 이 `max_tokens` 예약분까지 세는 탓에 3.1k·50rpm 으로 돌린 첫 기준선 런이 429 를 78회 먹었다), 실패는 표본으로 세지 않고 성공 N개를 채우는 재시도(못 채운 셀은 종료 코드 4로 드러난다), 옵션 이름이 상품명에 섞이는 픽스처 결함을 커밋 불가능하게 만드는 스키마 검증자, "단일 실행은 채택 판정이 아니다" 배너. `--prompt`/`--prompt-rev`/`--tier` 로 후보 프롬프트와 티어를 갈아끼우고, 실제로 보낸 프롬프트 텍스트의 sha256·앵커 해시·픽스처 버전을 산출물에 기록한다. 지표는 분자·분모 정의를 `AxisSpec` 데이터로 들고 리포트에 함께 인쇄하며, #234·#240 이 같은 이름으로 다르게 쓴 `productId` 지표를 두 축(`switchLegacy2`·`cartAddProductIdLegacy2`)으로 분리해 직접 비교를 금지한다. 실 LLM 비용·비결정론 때문에 **CI 에서 돌리지 않는 수동 도구**이며(유닛테스트는 전부 가짜 LLM으로 API 콜 0), 프롬프트를 바꾸는 PR 이 산출물을 근거로 첨부한다. 계약 변경 없음.
- **#278 — `conditionActions` 및 I-1 옵션 메타데이터 수신** — 조건 칩 제거를 구조화 신호로 받아 멀티턴 승계 필터에서 실제로 제거하고, I-1의 선택 `options`(이름 최대 20개)·`optionCount`(절단 전 전체 개수)를 기존 응답과 호환되게 관대 수신한다. (api-spec §3.1·§4.6, v0.20.1)
- **#242 — 판매자 분석 파이프라인 v3.1: 브랜치 분석 검증 + 조건부 차트 생성** — 분석 검증을 2층으로 분리했다. 기존 보고서 검증(D1~D3+report_judge, 팬인 후 직렬)은 그대로 두고, 팬아웃 브랜치 단위로 **F1~F3 결정론 검사**(근거 필수·근거 대조·유형 일치) + **analysis_judge 채점**(grounding·sufficiency·relevance, 21/30)을 신설해 워커가 도구 출력에 없는 수치를 지어내도 잡히지 않던 문제와 finding 간 교차 오염(D2 허용 집합이 전 finding 합집합이라 A 워커 수치로 B 서술의 환각이 통과되던 문제)을 해소했다. 미달은 ≤1회 재실행 후 강등(F 잔존) 또는 미검증 채택(judge만 미달)하며, 기존 3층 degrade 판정(§4·§7, `SELLER_DEGRADE_REASON_PRECEDENCE` 4종 계약)과는 별도 집계라 브랜치 검증 미달이 흔해도 `AllWorkersFailedError`가 오발동하지 않는다. 브랜치 wall-clock 예산(`seller_branch_deadline_s`)을 넘기면 재실행을 포기하고 직전 결과를 채택한다.
  - **보류돼 있던 차트 산출을 조건부로 되살렸다.** 판매자가 차트를 요청(명시 키워드 또는 planner의 암시 판정, OR)하면 검증된 finding·보고서에서 숫자를 그대로 옮겨 담는 `graph_agent`(도구 없음 — 새 조회 안 함, 근거 사슬 유지)를 `recommend`와 `asyncio.gather`로 병렬 실행하고, G1 검증(근거 없는 차트는 드랍, 재작성 루프 없음)을 통과한 차트를 신규 SSE `chart` 이벤트(0~1회, 보고서 `token` 뒤·`done` 앞, 추가 전용)로 전달한다. `ChartSpec` 필드는 FE 기존 `SellerAnalysis` 타입에 맞춰 `AnalysisChart.tsx`를 무수정 재사용할 수 있게 했다. 요청했으나 차트를 만들지 못한 경우만 응답 본문에 안내 한 줄을 덧붙인다(`compose_response` 4자 확장).
  - **[PR 리뷰 반영] F2/G1 수치 검증에 부호 보존 정규식 적용** — `_normalize_numbers`(무부호)를 그대로 공유하면 그래프·워커가 값의 부호만 뒤집어도(+12,000 → -12,000) 동일 토큰으로 정규화돼 환각이 잡히지 않았다. F2·G1 전용 `_normalize_numbers_signed`(부호 보존, 날짜·구간 표기의 하이픈은 오인하지 않는 lookbehind 포함)를 신설해 적용했다 — D2(`check_numbers_grounded`)는 무접촉 원칙에 따라 그대로 두었다.
  (api-spec §2.2·§3.2, v0.20.0 — 판매자 SSE 이벤트 6→7종, 추가 전용·기존 6종 무변경 / `DESIGN-ANALYSIS-V31-242.md`)
- **#60 — 총액 예산에 맞춘 BUY_ALL top-K 세트 조합 추천** — 니즈별 저가 대안 풀을 결정론적으로 완전 탐색해 알뜰·균형·강조 조합을 만들고, 불가능·부분 충족은 `token`으로 투명하게 안내한다. 계약 개정 없이 기존 v0.17.1 `lists[]`·`BUY_ALL`·`totalBudget`을 소비한다.
- **#113 — 카테고리는 유지한 채 조건 값을 푸는 완화(relaxation) 제안** — 0건이면 "조건을 조금 바꿔볼까요?" 한 줄로 끝나 **무엇을 얼마나** 바꿔야 하는지는 사용자 몫이었다. 계약(§3.1 `suggestions.relaxation`)과 스키마(`RelaxationRef`)는 이미 있었는데 칩을 만들어 내보내는 코드가 없었다. 이제 0건이거나 소량(config 임계 미만)이면 비카테고리 조건(가격 상한·평점 하한·브랜드·색상)을 한 단계 푼 제안 칩을 emit한다 — "65,000원까지 볼까요? (12건)". **카테고리는 완화하지 않는다** — 살 물건 자체를 바꾸는 결정이라 #84 소관이고, 여기서 같이 풀면 두 이슈가 같은 것을 건드린다.
  - **estCount는 완화 필터로 재검색해서 센다** — 이슈 본문은 "되돌리기 칩처럼 page-local 근사"를 지시했지만 코드 실측이 뒤집었다. `priceMax`·`brand`·`color`는 Spring I-1 **쿼리 파라미터**라 조건에 안 맞는 상품은 응답에 애초에 오지 않는다 → page-local로 세면 항상 0 → `estCount == 0` 칩 제외 규칙에 걸려 **이슈의 대표 예시인 가격 칩이 영원히 안 나온다**(`schemas/spring.py`가 이미 "재쿼리/BE count 필요"라고 적어 둔 그대로다). Spring이 size 없이 전량 반환하므로 재검색 1회면 근사가 아닌 **정확한 매칭 수**를 얻는다. 대가는 0건/소량 턴에서만 발생하는 추가 검색이며 상한은 config(`relaxation_max_probes`)다 — **칩 필드 수(기본 4)에 맞춘다.** 예산이 후보보다 적으면 뒤쪽 축은 estCount를 못 구하고, estCount 없는 칩은 만들 수 없어(스키마 필수) 말없이 사라진다(실제로 풀면 결과가 있어도). 잘릴 때는 `relaxation_chips_truncated`로 무엇이 빠졌는지 남긴다. **자동 완화와 예산을 공유하지 않는다**(`relaxation_max_rounds`가 따로 맡는다) — 공유하면 자동 완화가 먼저 돌아 예산을 쓴 턴에서 칩이 굶는데, 칩은 정작 자동 완화가 실패했을 때 쓰라고 있는 폴백이다. probe는 본 경로와 **같은 사후필터**(dedup·소모품 억제)를 통과시켜 "12건"이라 해놓고 8건이 뜨는 일이 없게 했다.
  - **명시 제약은 자동으로 넘지 않는다** — 자동 완화는 config 화이트리스트(`relaxation_auto_fields` 기본 `["ratingMin"]`)에 든 약한 조건뿐이고, 가격·브랜드는 사용자가 칩으로 동의하기 전까지 서버가 먼저 풀지 않는다(SPEC REQ-REC-043·AC-REC-08 가격 제약 불가침, 회귀 테스트로 고정). REQ-REC-047 명시/비명시 `source` 태깅은 #119로 `derived` 생산자가 사라져 실효가 없어 config 목록으로 대체했다 — 태깅 도입은 후속 과제다.
  - **조용히 바꾸지 않는다** — 자동 완화가 걸리면 투명 안내를 `token` 산문으로 흘린다(REQ-REC-042). **`done.data.relaxationNotice`는 싣지 않는다** — 이슈가 지시한 필드지만 근거였던 본 저장소 사본이 낡은 것이었다. 정본(Notion CH-2)은 `done`을 `finishReason` 하나로 확정했고("done: relaxationNotice 제거"), FE 타입(`jarvis-frontend` `shared/types/chat.ts`)의 `done`도 `{finishReason, panel?}`뿐이라 **읽는 소비자가 없다.** 사용자 고지는 `token`이 그대로 이행하므로 없어지는 건 아무도 읽지 않는 기계 판독 사본뿐이다.
  - **칩을 누르면 실제로 적용된다** — FE는 칩 클릭 시 label을 그대로 다음 턴 `message`로 보내는데(`applySuggestion` → `send(label)`), label이 "65,000원까지 볼까요?"라는 **의문문**이라 decompose(LLM)가 숫자를 다시 뽑아야 하고 되물음으로 새면 칩이 무동작이 된다. 이제 제안한 칩을 스레드에 기억해 뒀다가(`RelaxationOfferStore`, 되돌리기 칩의 `RevertStore`와 같은 패턴) 다음 턴 message가 그 label과 **정확히 일치**하면 LLM 해석을 건너뛰고 계산해 둔 값을 그대로 적용하고 intent도 `recommend`로 고정한다. 일치하지 않으면 기존 경로 그대로다. 제안은 턴마다 **덮어쓴다** — 화면에 없는 옛 제안이 되살아나지 않게.
  - **"그 중에"라고 말하면 완화가 이어진다** — 자동 완화는 사용자가 동의한 적 없는 서버 조치라 다음 턴에 영속시키지 않는다(영속시키면 그때부터 고지 없이 조건이 조용히 위반된다). 대신 발화가 **직전 결과 집합을 가리키는지**로 가른다: `"더 저렴한 걸로"`는 원래 조건(평점 4.5)으로 돌아가 필요하면 다시 완화·다시 고지하고, `"그 중에 더 저렴한 걸로"`는 완화(4.0)를 이어받아 헛검색·고지 반복이 없다. **"그 중에"가 곧 동의 신호**라 칩 클릭과 같은 급으로 보고 필터에 녹여 영속시킨다. 판정은 decompose 출력 `scopedToPrevious`가 하되, 놓치면 무해하고 오탐해야 조건이 조용히 바뀌므로 **애매하면 엄격한 쪽**으로 기운다(파싱을 `is True`로 좁힘 + 프롬프트에 "애매하면 false" 명시).
  - **조건 칩이 실제 검색과 어긋나지 않는다** — 조건 칩은 검색 **전에** 나가는데 자동 완화는 검색 **후에** 조건을 바꾼다. `conditions`는 §3.1상 0~1회라 "고쳐서 재전송"이 불가하므로, 완화가 일어날 수 있는 턴만 발신을 검색 뒤로 미루고 채택된 값으로 내보낸다. 절대다수 턴(완화 대상 조건이 없는 턴)은 종전대로 검색 전에 나가 첫 프레임 지연이 없다.
  - **잘못된 설정은 기동을 막는다** — 완화 칩 대상의 오타(`"pricemax"`), 자동 완화 목록에 명시 제약(`priceMax`·`brand`) 추가, 자동 목록이 칩 목록의 부분집합이 아닌 조합을 각각 기동 시점에 거부한다. 셋 다 **조용히 기능만 죽는** 부류라 런타임에는 드러나지 않는다(#133 "고지 여부는 튜너블이 아니다"와 같은 원칙).
  - 완화 대상 필드·완화 폭·칩 probe 상한·소량 임계는 전부 config 주입(하드코딩 없음). 자동 완화와 칩 probe는 **예산을 공유하지 않는다** — 공유하면 자동 완화가 먼저 돌아 예산을 소진한 턴에서 칩이 굶는데, 칩은 정작 자동 완화가 실패했을 때 쓰라고 있는 폴백이다. `suggestions`는 종전대로 `products.ready` 앞에 나가 §3.1 순서 계약을 지킨다. (api-spec §3.1 사본 drift 정정 v0.19.1 · C-15 🔴 `totalCount` 잔여 해소 / SPEC-RECOMMEND-001 §6.6)
- **이슈 #258 A 파트 — 색상 표기 동의어 검수·확장 기반 추가** — str/array 혼재 카탈로그 색상을 수확해 고정 앵커 대상 LLM 의미 배정→환각·중복·순환·sentinel 엄격 거부→임베딩 불일치 교차검증→사람 검수 대기 사전으로 만드는 오프라인 파이프라인과 승인 표기 전용 TTL 캐시를 추가했다. 전량은 `pending_review`로만 적재하며, I-1 와이어 리스트 전송은 api-spec §4.6 개정과 BE 배포를 기다리는 기본 off 플래그로 현행 단수 `color` 계약을 유지한다.
- **#147 — 개인화 효과·과반영 평가 하네스 추가** — 합성·비식별 5-arm profile fixture와 profile weight 5점 ablation을 동일 dev 케이스에서 paired 실행하고, slice별 ΔNDCG@K·Δdiversity bootstrap CI, 명시 의도 모순·금지/최근구매 신규 유입·clean→noisy 열화 판정, 전 arm×weight hard-filter 불변식, 실 LLM scope gate wrapper와 arm 배수 예산 dry-run을 제공한다. #119 수정 전후(`both` vs `rerank_only`) 실 LLM paired 회귀 자료를 `baselines/live-v1`에 영속해 수정 전 29/31건 필터 유출·ΔNDCG -0.29에서 수정 후 유출 무신호·CI 0 포함으로의 변화를 기록한다.
- **#146 — 3-arm 추천 pipeline ablation 하네스와 전량 baseline 추가** — 현행 pipeline, 결정론 scoring, smart-tier single-call을 같은 dev 31건×N=5에서 비교하고 호출별 token·비용·latency, paired bootstrap CI, 재현 manifest와 불변 산출물을 기록했다. 전량 결과 pipeline이 single-call보다 nDCG@10 +0.087(95% CI [0.022, 0.160]) 높고 비용은 사실상 같아 production 전환을 기각했다.
- **#144 — 실제 모델 추천 평가·회귀 리포트 runner 추가** — 고정 골든셋과 기존 metric runner를 runtime 배포 후보 provider/model 설정에 연결해 호출별 exact model·usage·비용·지연을 기록하고, provider 전 reserve형 예산 gate, versioned primary/secondary metric 반복 통계와 paired bootstrap, hard-constraint·coverage release gate, sealed holdout 승인 경로, 코드 해시 manifest 및 사람이 판독 가능한 Markdown/CSV 산출물을 제공한다.
- **#145 — 설명 가능한 추천 scoring baseline 추가** — LLM 호출 없이 semantic·profile·popularity·주입형 recency·diversity·최근 exact 구매 감점을 성분별로 재구성 가능한 결정론 점수로 기록하고, hard constraint 컷을 점수와 분리한 paired dev 평가 및 고정 baseline을 `evals/scoring/`에 추가했다.
- **#143 — 구매자 추천 품질 metric runner(`evals/metrics/`) 추가** — 골든셋 dev split에서 P@K·R@K·MRR·nDCG@K·Filter Accuracy·HCV·Coverage·Diversity를 네트워크·라이브 LLM 없이 결정적으로 계산하고 case·slice·전체 Markdown/CSV 리포트와 재현용 run manifest를 생성하며, ScriptedLLM + MockTransport로 실제 추천 코드 경로를 실행하고 `pytest -m eval` 가격 제약 PR 게이트로 회귀를 조기에 차단한다.
- **#142 — 구매자 추천 골든셋 v1 구축** — 라이브 Spring I-1 응답과 실제 카탈로그 상품만으로 검색·개인화·재구매·카테고리 매핑 실패 43건을 구성하고, dev 31건과 라벨이 분리된 sealed holdout 12건을 안정 ID로 고정했다. camelCase 스키마 검증, #32 비교 하니스 어댑터, 합성 구매 이력의 실제 상품 참조, 결정론 스냅샷, dataset hash manifest, split 간 query·정답·persona·fixture 누출 감사와 봉인 해제 기록 API를 추가했다.
- **#151 — staging·로컬 공용 HTTP/SSE 벤치마크 runner와 불변 baseline 산출물 추가** — 실제 FastAPI→Spring/DB/LLM 경로를 타깃 주입형 블랙박스로 측정하면서 cold·warm-up·measured를 분리하고, 요청마다 고유 thread를 써 동시 스트림 락이 측정을 왜곡하지 않게 했다. 신뢰도 분모는 실패·타임아웃을 포함한 measured 전체로 두되 지연 분모는 non-empty token과 terminal `done`을 모두 받은 성공 요청만 사용하며 제외 건수를 함께 출력한다. TTFT·토큰·비용·서버 조인 누락은 0 대신 bounded `null+reason`/`unknown`으로 보존하고, p99는 100표본 미만이면 생략 사유를 명시하며 p50/p95 bootstrap은 고정 시드와 #137 최근접 순위 정의를 재사용한다. X-Request-Id 기반 `chat_request` 조인, 실행 환경·가격표 manifest, secret 누출 차단, 덮어쓰기 없는 Markdown/long-format CSV/raw JSONL 산출을 포함하며, fixture의 `expected_outcome`은 요청별 `outcome_match` 3상태(true/false/unknown)와 bounded 사유로 대조해 mismatch·미측정을 그룹 리포트에 드러내며, 사용 모델의 입력·출력 단가가 하나라도 없으면 서버의 `costUsd: 0`을 신뢰하지 않고 비용을 `unknown`으로 처리한다. Spring 부재 로컬 타깃에서 시나리오 3종×동시성 1·5·10 baseline을 실제 측정해 불변 아티팩트로 보존했으며, staging 수치가 아니고 provider 스로틀과 상시 degrade 조건이 섞였다는 판독 주의도 함께 남겼다.
- **#137 — 관측 로그 집계 스크립트와 degrade율 알림 추가 (EVAL-OBS ③-2)** — 수천 줄 `chat_request` 로그를 눈으로 볼 수 없어 "degrade가 얼마나 터지나·쿼리당 비용과 p95는 얼마인가"에 답할 수 없었다. `scripts/aggregate_observability.py`가 로깅 접두사가 붙은 줄과 순수 JSON 줄을 모두 읽어 지연 p50/p95/p99(`role`·`lane`·`model`별)·비용·degrade율·error율·SLO 초과율을 markdown + long-format CSV로 롤업한다. 인프라 0 — 파일이나 stdin만 읽는다.
  - **분모를 지표마다 다르게 두고 그 사실을 출력에 적는다** — degrade·error율은 스트림 전 거부(`emit_rejection`)를 포함한 전체 턴이 분모지만, **비용은 실제로 실행된 턴만** 센다. 거부는 LLM을 한 번도 부르지 않고 `costUsd: 0`을 싣기 때문에 분모에 넣으면 쿼리당 비용이 0 쪽으로 희석되면서 커버리지는 100%로 보여 "완전히 측정된 값"으로 오독된다. `costUsd`가 `null`이거나 키가 없는 줄은 0으로 치환하지 않고 표본에서 빼며(판매자 레인은 당분간 비용 필드가 빈다), 커버리지와 `부분 집계(partial)` 여부를 표에 함께 낸다.
  - **degrade율 임계 초과 시 non-zero exit** — CI·cron에서 실패로 표면화한다. 임계·최소 표본은 config 주입(`degrade_rate_alert_threshold` 0.10 · `degrade_alert_min_samples` 50)이며, 표본이 하한 미만이면 비율이 요동치므로 알림하지 않고 `표본 부족 — 판정 보류`를 명시한다(조용한 통과 금지). SLO 목표(`slo_first_token_ms` 10s · `slo_total_seller_ms` 90s · `slo_total_buyer_ms` 30s)는 리포트 전용이라 런타임 스트림 상한을 바꾸지 않는다.
- **#136 — `chat_request` 관측 로그에 degrade·비용·레인 차원 추가 (EVAL-OBS ③-1)** — degrade는 HTTP 200과 정상 SSE 안에서 조용히 일어나 종전 로그의 `streamStatus`·`errorType`만으로는 rerank 폴백이나 부분 실패의 빈도·비용을 알 수 없었다. 요청당 기존 JSON 한 줄에 내부 관측 필드 `lane`·`degraded`·`degradeReason`·`costUsd`·`toolCalls` 5개를 추가했다. buyer는 `recommend`/`cart`/`fallback`, seller는 SSE `meta.lane`과 같은 `analysis`/`product`/`general`/`confirm`/`apply`/`refused`로 집계하며, 판매자 도구 호출은 `ToolCallLimit`을 통과해 실제 handler에 도달한 횟수를 센다.
  - **새 degrade 동작을 만들지 않고 기존 판정의 관측 경계만 연결했다** — buyer 6종(`search_failed`·`rerank_fallback`·`push_skipped`·`dedup_skipped`·`cart_merge_skipped`·`fanout_partial`)과 seller 4종(`worker_degrade`·`partial_report`·`all_workers_failed`·`spring_write_failed`)의 기존 `trace.mark_degraded()` 호출부가 요청 관측 sink로 동일한 bounded 사유를 전달한다. LangSmith tracing이 꺼지거나 샘플링에서 제외돼도 경량 `NoopRequestTrace`가 lane·degrade·모델 사용량·도구 횟수를 요청별로 보존해 운영 기본값에서 로그가 비지 않으며, 동시 degrade는 기존 역할별 우선순위를 그대로 적용해 실행 순서에 따라 단일 사유가 흔들리지 않는다.
  - **비용 단가는 config 주입** — `model_price_in_per_1k`·`model_price_out_per_1k`(USD/1,000 tokens)를 `Settings`의 환경변수 JSON 표로 받고 `finish()`에서 prompt/completion token별 비용을 합산한다. buyer 공용 LLM 래퍼와 seller LangGraph 모델 미들웨어의 provider usage를 함께 모으며, 등록되지 않은 모델은 임의 가격을 하드코딩하지 않고 `costUsd=0`과 `MODEL_PRICE_MISSING` 경고로 누락을 드러낸다.
  - **스트림 전 거부에는 lane을 추측하지 않는다** — 429와 라우팅 전 409/504는 실제 분기가 확정되기 전이라 `lane=null`; 나머지 새 필드는 `degraded=false`·`degradeReason=null`·`costUsd=0`·`toolCalls=0`으로 구조를 유지한다. endpoint·HTTP/SSE 스키마·이벤트·오류 코드·degrade 폴백 동작은 바꾸지 않았고 `docs/api-spec.md` 개정도 없다. 사용자 message 원문은 계속 금지하고 기존 길이 + peppered HMAC 지문만 남긴다. 전체 1,746개 테스트와 ruff를 통과했으며, 이 데이터는 후속 #137 집계 스크립트와 #138 타임아웃 재조정의 선행 입력이다.
- **#148 — 홈 추천용 AI ranking endpoint (I-22)** — 홈 "OO님을 위한 추천"(P-5)의 개인화 랭킹을 Spring이 AI에 위임하는 `POST /internal/recommendations/home`. 채팅(CH-2 → I-21 → CH-5)과 달리 **Spring이 호출 주체라 왕복 1회로 끝나고 I-21 콜백을 타지 않는다** — 응답 본문에 목록이 실려 나간다. 인증은 `/events/*`와 같은 `X-Internal-Token`(레인 b)이지만 **통지가 아니라 동기 요청/응답**이라 §2.7 멱등 규약이 적용되지 않는다(재시도 = 새 `recommendationRequestId`·`listId`).
  - **후보 확보 경로가 채팅과 다르다** — 홈엔 발화가 없어 검색어를 만들 수 없다. Spring 검색(I-1) 위임 대신 **자체 카탈로그 인덱스**(I-17 임베딩)에서 시그널 기반 벡터 근접으로 순위를 매긴다. 질의 벡터 = signals 상품 임베딩의 가중 평균이며 cart가 조회보다 무겁고(담기까지 갔다는 강한 신호) 조회는 최신일수록 무겁다(decay). `recentPurchasedProductIds`는 **가중치가 아니라 제외 필터**다.
  - **랭킹은 결정적이다** — 코사인 동점 시 저장소 순회 순서에 기대면 pg 행 순서에 따라 순위가 흔들리므로 `productId` 오름차순으로 tiebreak한다(완료조건: 동일 snapshot·config → 동일 ranking). `limit`은 최종 노출 목표치라 품절 드롭 대비 배수만큼 넉넉히 반환하고 Spring이 자른다.
  - **`outcome` 3종이 모두 200** — `PERSONALIZED`/`NO_PROFILE`/`INSUFFICIENT_CANDIDATES`. cold start는 오류가 아니며 fallback 판단은 Spring이 한다. 프로필 부재·후보 부족으로 4xx/5xx를 내지 않음을 테스트로 고정했다. 다만 **입력·인프라 실패는 여전히 오류다** — 카탈로그 인덱스 장애는 `503 UPSTREAM_UNAVAILABLE`(전역 오류 맵에 503 등재), 미지 필드·범위 밖 `memberId`는 `400 BAD_REQUEST`. `sessionId`를 실어 보내면 `extra=forbid`가 400으로 거부한다 — 홈에는 채팅 세션이 없다.
  - **`reason`은 요청 경로에서 만들지 않고 미리 만들어 둔 재료에서 고른다.** LLM 배치 1회로 시작했으나 실측이 뒤집었다(gpt-5-nano, 실카탈로그 7,220건): 후보 20개 7,970ms · 12개 3,852ms · 6개 2,102ms로 **항목 수에 선형**(출력 토큰 지배)이라 5개로 줄여도 2.0s 타임아웃을 5/5 넘겨 **reason이 0건**이었다. 이제 I-17 배치가 상품당 1회 만들어 `extras`에 넣은 `situation_tags`·`review_pros`에서 사용자 맥락에 맞는 것을 고른다 — 담기 > 조회 > 상품 고유 폴백(프로필 문자열 분기는 선호/회피 극성 파싱 불가로 제거 — 장기 취향은 프로필 벡터가 랭킹에 반영) 순이고 매칭이 없으면 `null`(계약상 정상). **요청 경로 LLM 0회 · 결정적 · 종단 p50 45ms · reason 22/24건.** 문장은 I-21과 같은 규약으로 제어·포맷 문자 제거 + 상한 truncate 후 신뢰경계를 넘는다. 조사(을/를·이/가)가 받침에 따라 갈리므로 문장 틀은 `에` 형태로 통일했다 — 태그가 자유 텍스트라 형태를 미리 알 수 없다.
  - **랭킹을 HNSW 인덱스로 밀었다** — 전량을 파이썬으로 코사인 계산하던 초기 구현이 7,220건에서 **p50 3,321ms**로 예산 3s를 그 자체로 넘겼다. `ArtifactStore` Protocol에 `top_k_by_vector`를 신설하고 양쪽 구현체를 함께 고쳐(공유 계약 규약) pg 경로는 `vector_ip_ops` HNSW를(`<#>`), 인메모리 경로는 정확한 코사인을 쓴다 → **p50 39ms**. 벡터가 L2 정규화돼 있어 내적 순위 = 코사인 순위이며 정규화가 이 경로의 전제다.
  - **enrichment가 `situation_tags`를 뽑도록 확장** — 기존 카탈로그(시드 덤프)는 외부 파이프라인이 만든 이 키를 갖고 있지만 이 저장소의 `enrich_product`는 `tags`/`attributes`만 뽑아, **I-17로 새로 들어오는 상품만 조용히 reason이 비는** 구조였다. 프롬프트와 파싱을 확장해 신규 상품도 재료를 갖게 했다(기존분 소급 재생성은 불필요 — 이미 더 나은 재료가 있다).
  - **[HARD] provenance 비노출** — `algorithmVersion`·`modelVersion`·프로필 원문·prompt·모델 식별자가 **응답·로그 어디에도** 없음을 테스트로 검증한다. 관측 로그는 고정 key set(`outcome`·`candidateCount`·`returnedCount`·`reasonSource`·`elapsedMs`)만 남기고 memberId·productId·업스트림 예외 문자열을 남기지 않는다. 프로필 저장소·LLM 장애도 클래스명조차 로그에 남기지 않는다(#141 규약).
  - **장기 취향(프로필)이 랭킹에 반영된다** — 초기 구현은 프로필을 `reason` 근거로만 써서 §3.7이 요구한 *"프로필 벡터와 가중 혼합"* 의 절반이 빠져 있었다(프로필이 생겨도 순위에 영향 0). 이제 sleep-time consolidation이 요약을 만들 때 **벡터도 함께 만들어 저장**하고(`RETRIEVAL_QUERY` task — 카탈로그 문서와 달라야 하는 비대칭 임베딩) I-22는 읽어서 항으로 더한다. 요청 경로에 임베딩 API 왕복이 붙지 않는 게 요점이다. 실카탈로그 확인: 프로필 유무로 **상위 24개 중 21개 위치가 바뀐다**. 가중치 `home_reco_weight_profile`(기본 0.5)을 0으로 두면 롤백된다. 부수로 **시그널이 비어도 프로필만으로 개인화**된다 — 종전에는 무조건 `NO_PROFILE`이었다.
  - **실카탈로그로 검증했다** — 7,220건 적재 후 종단 **p50 45ms**(예산 3s 대비 여유 2.9s), `outcome` 3종·구매이력 제외·결정성 모두 실데이터로 확인했다. 프로필 벡터 미사용·`NO_PROFILE` 판정 기준·503 범위 등 계약과 다른 지점은 api-spec §3.7 「구현 노트」에 명시했다.
  - **C-18 — `catalogVersion`을 선택으로 완화하고 계약 폐기를 제안한다** (api-spec v0.19.0). "값 생성 주체가 잘못됐다"로 읽고 Spring → AI 이관을 구현했다가 **되돌렸다.** 물어야 할 것은 주체가 아니라 **필드의 존재 이유**였다. ① *재현*이 성립하지 않는다 — `products`는 I-17이 제자리 upsert하므로 그 시점 임베딩이 남지 않고, 버전 라벨이 가리키는 상태가 이미 사라져 있다(스냅샷을 남기려면 버전당 약 44MB × 5분 주기). ② *재현이 필요하지도 않다* — 산출물은 Spring이 `recommendation_generated`로 이미 저장한다. ③ *캐시 무효화*는 TTL 10분과 중복이고, `max(updated_at)` 지문은 상품 1건 갱신으로 전 회원 캐시를 동시에 날려 **오히려 캐시를 죽인다.** 응답 필드와 `ArtifactStore.catalog_version()`을 제거했고, 요청 필드는 Spring 무변경을 위해 선택으로 남겨 받고 버린다. 🔴 잔여 = BE 협의(폐기) + 정본 개정.
  (api-spec §3.7·§4.11, v0.18.0~v0.19.0)
- **#114 — 옵션이 1개뿐인 상품은 되묻지 않고 자동 선택해 담기** — 옵션 필수 상품이면 선택지가 하나뿐이어도 "옵션을 선택해 주세요"로 되물었다. 답이 이미 정해져 있는데 왕복만 한 번 더 생긴다. 이제 `CART_OPTION_REQUIRED` 의 후보가 **1개**면 그 `optionId` 로 **즉시 재담기**해 `CART_ADDED` 로 끝내고, AI 가 대신 골랐다는 사실이 드러나도록 `action.message` 에 옵션명을 밝힌다("프리 사이즈 옵션으로 담았어요"). 자동 선택은 **1회로 고정** — 자동 선택한 옵션에도 REQUIRED 가 또 오면 계약 이상이므로 기존 되물음 멀티턴으로 degrade 하고, 이미 보낸 optionId 와 후보가 같으면 같은 요청을 되풀이하지 않는다. 후보가 2개 이상이면 임의로 고르지 않고 종전대로 되묻고, 재담기 실패(재고 부족·`CART_OPTION_INVALID` 등)는 기존 오류 매핑을 그대로 탄다. **와이어 계약은 불변** — AI 가 I-2 를 `optionId` 로 재호출할 뿐 엔드포인트·요청/응답 스키마·SSE 이벤트·필드·오류 코드 어느 것도 바뀌지 않는다. FE 는 `CART_OPTION_REQUIRED`(AI↔Spring 내부 코드)를 관측할 수 없고 담기 턴이 되물음 `token` 또는 결과 `action` 중 하나로 끝나는 문법도 그대로다. 다만 **BE 는 관측한다** — 400 직후 같은 요청이 `optionId` 만 채워져 한 번 더 오므로, §4.1 "AI 동작" 열이 예외 없이 "되묻는다"로 읽히던 서술을 명확화했다. (api-spec §4.1·§3.1, v0.17.2 — 동작 명확화, 계약 자체는 불변 / `docs/specs/SPEC-CART-001.md` v0.2.5 REQ-CART-026·027)
- **#187 — 대화 맥락의 수명주기를 방(thread)이 아니라 접속(session) 단위로 (D6)** — 종전엔 방마다 상태가 따로 살고 죽어, 한 접속에서 연 탭 3개 중 하나만 맥락이 사라지는 상태가 가능했다. 사용자가 이해할 수 없는 동작이다. 이제 `chat_session_contexts` 가 접속당 **context 하나**를 정본으로 들고 방 상태는 그 아래 `context_id:thread_id` 네임스페이스에 달린다 — 어느 방에서 활동해도 같은 접속의 **모든 방** TTL이 함께 연장되고, 세션이 끝나면 함께 정리된다. `session → thread[]` 역인덱스가 그 "모든 방"을 지목하는 수단이다.
  - **신원은 서명된 티켓에서만 나온다** — 구매자 요청은 body `sessionId` 를 티켓의 signed `sessionId` 와 대조하고 불일치하면 `403 SESSION_FORBIDDEN`. 이 검증은 LLM·상태 접근보다 **먼저** 수행한다 — 뒤에 두면 인증 실패가 이미 열린 200 SSE 안에서 완화되어 나간다. seller `sub`·`brandId` 는 decode 경계에서 타입까지 좁혔고(양의 BIGINT 숫자 문자열 / bool 제외 JSON 정수 `1..2^63-1`), stream ticket `scope` 는 단일 문자열 exact 검증이며 **설정으로 끌 수 없다** — 끌 수 있는 검증은 fail-closed 가 아니다.
  - **guest → member 승격** `POST /events/session-claim` — 게스트로 대화하다 로그인하면 그 접속의 모든 방이 회원에게 이어진다. 멱등이며 다른 claim 이력·terminal·owner 불일치는 `409 SESSION_CLAIM_CONFLICT`, lifecycle 정본 사용 불가는 `503 STATE_UNAVAILABLE`. 승격 commit 뒤 옛 게스트 티켓의 `/chat` 은 `403 SESSION_FORBIDDEN` 이고 새 turn/thread 를 만들지 않는다.
  - **generation-safe 정리** — 비활동 sweep 이 만료 컨텍스트를 유한 lease 와 함께 claim 하고(`idle_finalizing`), watermark 를 잡은 뒤 transient/profile 단계를 나눠 처리한다. crash·retry 는 lease 와 claim token CAS 로 재개하며, 늦게 돌아온 이전 generation 의 쓰기는 supersede 된다. `idle_expired` 뒤 정당한 같은 owner 가 돌아오면 generation 을 올려 **같은 `context_id`** 를 재활성화하고, `idle_finalizing` 중 touch 는 `409 SESSION_FINALIZING`.
  - **turn 은 lifecycle 트랜잭션 안에서 원자적으로** commit 되고, 그 결과로 확정된 `context_id` 만 buyer 상태 키의 근거가 된다. 검증 실패 시 adoption 을 건너뛰지 않는다.
  - **관측 경로에 식별자를 남기지 않는다** — 판매자 로그는 `_seller_log()` 한 곳으로 모아 peppered 지문(`sellerFp`·`brandFp`·`threadFp`)과 고정 상태 필드만 기록하고, 예외 원문·traceback 대신 오류 코드만 남긴다. trace 메타데이터도 `sessionFp`/`threadFp` 다. 관측 마감은 shield 된 단일 cleanup task 라 취소 중에도 정확히 1회 수행되고 원래 취소를 재전파한다 — 종전엔 `finished` 를 await 앞에 세워 `CancelledError` 가 나면 turn 이 `PENDING` 으로 남을 수 있었다.
  - 이벤트 입력은 **camelCase alias 전용** — unknown field·snake_case field·camelCase+snake_case 충돌은 `400 BAD_REQUEST`.
  (api-spec §2.6·§3.5·§3.5.1, v0.16.0 / 설계: `docs/specs/SPEC-CHAT-SESSION-CONTEXT-187.md`)
- **#141 — 요청 단위 트레이싱(LangSmith) — 개인정보를 내보내지 않는 관측** — 한 번의 채팅 요청이 라우팅·그래프·LLM·Spring 호출을 거치며 어디서 느려지고 어디서 degrade 됐는지 확인할 수단이 없었다. 이제 요청마다 **트리 하나**가 뜬다 — 루트는 `buyer_chat_turn`·`seller_chat_turn`, 그 아래 `buyer.routing`·`buyer.graph.*`·`seller.graph.*`·`seller.worker.*`·`llm.seller.*`·`spring.*` 스팬이 붙는다. 부모 관계는 **contextvar**로 잇는다 — asyncio 태스크가 부모를 상속하되 가변 전역 스택을 공유하지 않아야 fan-out(추천 leg·판매자 워커) 동시 실행에서 계보가 섞이지 않는다. **설계의 핵심은 "무엇을 안 보내는가"다.** 커머스 트래픽엔 구매자 발화·상품 데이터·주문 이력·내부 토큰이 흐르는데 관측 도구는 외부 SaaS다. 그래서 export 는 **fail-closed 화이트리스트** — 메타데이터 키는 `SAFE_METADATA_KEYS` 21종에만 허용하고, 그 밖의 키·비어있지 않은 `input`/`output`/`tool`/`prompt` 계열 필드·카나리 패턴(Bearer 토큰, `sk-`/`lsv2_` 키, 이메일, 휴대폰, 주민번호)이 하나라도 걸리면 `UnsafeTelemetryError` 로 **내보내지 않는다**. LangGraph·LangChain 자동 계측을 쓰지 않고 자체 트리를 세운 이유가 이것 — 자동 계측은 노드 입출력을 통째로 싣는 게 기본값이라 안전한 기본값이 없다. 관측값은 원본 대신 **유계 코드**로 환원한다: degrade 는 최고 심각도 1개(`degradeReason` 단수, 동시 degrade 의 순서 의존 제거), Spring 실패는 `statusClass`(`timeout`·`connection_error`·`4xx`·`5xx`)로만 — 예외 **클래스명**조차 업스트림 상태를 유출해 억제한다(`_spring_span` 의 stash-and-rethrow 구조, 주석으로 의도 고정). 지연은 `server_first_event_ms`(SSE 첫 프레임)와 `server_first_text_token_ms`(사용자 눈에 보이는 첫 글자)를 **분리** — 앞선 이벤트가 체감 지연을 가려 하나로는 TTFT 개선을 측정할 수 없다. 트레이싱은 **응답에 개입하지 않는다**: exporter 실패는 요청 제어 흐름 밖에서 삼키고, 켜고 끈 SSE 바이트가 동일함을 회귀 테스트로 고정했다. 루트는 SSE **종단 생명주기**에 묶여 정상 종료·오류·클라이언트 취소(ASGI cancellation 포함) 어디로 끝나도 `terminalReason` 과 함께 닫힌다 — 스트림이 중간에 끊겨도 트레이스가 유실되거나 매달리지 않는다. 설정은 `LANGSMITH_TRACING`(기본 off) · `LANGSMITH_TRACING_SAMPLING_RATE` · `LANGSMITH_EXPORT_TIMEOUT_S`(기본 0.5s) · `APP_ENVIRONMENT`. 와이어 계약 변경 없음(엔드포인트·SSE 이벤트·필드 불변)이라 api-spec 개정 없음. (리뷰 반영: 판매자 `apply` 레인의 chain span 이름이 `seller.graph.product` 로 복붙돼 **두 레인이 한 통계로 합쳐지던 오분류**를 고쳤다 — `lane` 메타데이터만 검증하던 테스트가 span 이름을 안 봐서 통과하고 있었고, 레인별 span 이름을 고정하는 회귀 테스트를 함께 넣었다.)
- **#198 — 목적·상황형 발화의 상품 전개(`shopping_list` 분해)** — `"집들이 선물로 뭐 사갈까"` 처럼 **무엇을 살지 사용자가 말하지 않은 발화**를 구체 상품 목록으로 전개해 fan-out 검색의 입력을 만든다. 정본 `SPEC-RECOMMEND-001` §5.1 에 명세됐으나 코드가 0건이었고(#59 가 §6 비범위로 유예), 그 결과 상황형 질의는 `['집들이 선물']` 이 그대로 leg 이 되어 매핑이 불가능했다 — 추천 파이프라인 `①목적→상품 ②상품→카테고리 ③카테고리별 검색` 중 **①이 비어 있던 것**(②는 #115, ③은 #59 에서 구현). **감지는 코드가 결정적으로**(D1 신호 없음 / D2 발화 복사 / D3 목적 marker `endswith`), **생성은 전용 LLM 호출 1회**가 담당한다 — `decompose` 프롬프트에 맡기는 방식은 실측 39회에서 1~2/3 확률로만 성립했고 규칙 강화·예시 확대 모두 실패했다(예시 확대는 기존 성공 케이스를 3/3→1/3 로 희석). `case` 를 감지 신호로 쓰지 않는 이유는 전개와 **같은 LLM 호출의 산출물**이라 실패 회차의 값을 신뢰할 수 없기 때문이다(`"부모님 환갑 선물"`: `case=[3,3,3]` 인데 `legs=[1,1,1]`). 전개는 **주입형 seam** 이라 방식 B(카탈로그 임베딩)·C(캐시)로 갈아끼우는 것이 주입 한 줄이다. 배선은 **멀티턴 승계 가드 안쪽**에 둔다 — D1 조건이 리파인 턴("더 저렴한 걸로")의 "신호 없음"과 겹쳐, 앞에 놓으면 직전 맥락이 엉뚱한 상품 목록으로 대체된다(회귀 테스트로 고정). **실패는 전부 `decompose` 원본 유지** 로 흡수해 후퇴가 없다. 라이브 검증: `"캠핑 처음 가는데"` → 텐트·랜턴·매트 → `캠핑 > 텐트/캠핑랜턴`, `"집들이 선물"` → 와인잔 세트·디퓨저·커피 드리퍼 → `주방용품 > 잔/컵`·`인테리어소품 > 디퓨저`, 대조군(`청바지`·`무선 이어폰`) 오탐 0. 잔여 한계는 전개율 7/21 — marker 목록 부족으로 감지가 놓치는 회차가 있고, 미전개는 종전 경로(#115 거리컷이 흡수)라 후퇴가 아니다. 부수로 `decompose` 프롬프트에 **`case` 1/2/3 정의를 명시**했다(종전엔 JSON 스키마에만 있고 의미 설명이 없어 값이 노이즈 — Case 3 판정 8발화 중 6종 오판 → 정의 후 15/15 정확, 부수적으로 전개율까지 개선). 전개 호출은 `chat_request` 의 모델 호출 집계(`model`·토큰, api-spec §6.3)에 기록해 **조건부 +1 호출을 비용 로그에서 확인**할 수 있게 했다 — 기록은 LLM 을 실제로 쓰는 전개기 안에서 하므로 방식 B·C 로 교체하면 자동으로 사라진다. `ShoppingItem.priority`·니즈당 노출 개수 배분은 범위 밖(#168·#60·#163). (SPEC-RECOMMEND-001 v0.10.0 — `EX-7`·`AC-REC-37` 개정으로 전용 호출 허용, api-spec 변경 없음. 설계: `docs/specs/DESIGN-NEEDS-EXPANSION-198.md`)

### Changed
- **#254 — 구매자 의미 재정렬을 후보 제한 pgvector 질의로 이관** — Spring 후보 전량의
  임베딩을 Python으로 가져와 코사인을 계산하던 경로를 `product_id = ANY(...)`와
  `<=>` 정렬을 쓰는 `top_k_by_vector`로 옮겼다. 실 pg-catalog에서 후보 30/300/3,000/7,220건
  Python p50 10.4/99.4/1,024.8/2,490.4ms가 DB 경로 2.1/4.1/21.4/44.9ms로 줄었으며,
  DB 미존재·빈 임베딩·설정 상한 밖·중복 후보는 유실 없이 Spring 상대순서로 꼬리에 보존한다.
- **#138 — 구매자 SSE 전체 상한을 판매자와 분리해 30초로 축소** — `stream_total_timeout_buyer_s`를 신설해 구매자 경로에만 적용하고 판매자·미지정 호출은 기존 90초를 유지한다. Spring 기동·동시성 1 실측에서 구매자 total p95 10.5초·max 12.8초로 30초는 max의 2.3배 여유였고 154턴 중 초과는 0건이었다. first-token 10초·Spring 3초·LLM 30초+재시도 1회는 첫 SSE 이벤트 p95 1.4~3.0초, I-1 검색 p95 ≤517ms, LLM 단일 호출 p95 ≤4.3초 근거를 남기고 유지했다.
- **#173 — rerank LLM 에 정밀 가격을 넘기지 않는다 — 후보군 상대 등급으로 원천 차단** (#171 의 rating·reviewCount 등급화를 price 로 마저 확장). LLM 이 만드는 rationale·overallComment 는 신뢰경계를 넘어(→Spring→CH-5→FE) 사용자에게 노출되는데, 종전엔 후보에 정확한 `price` 를 그대로 실어 근거문에 금액이 인용될 수 있었다. 문제는 표시 권위 위반(§4.6 상 price 는 **비표시**, 표시가는 경로 B 소관)만이 아니라 **freshness 불일치**다 — AI 가 쓰는 값은 **검색 시점** 가격이고 카드는 **표시 시점** Spring 값이라, 그 사이 가격이 바뀌면 채팅("39,000원이라 저렴해요")과 카드(41,000원)가 정면으로 어긋난다. 가격 오차는 평점·리뷰수 어긋남보다 신뢰에 훨씬 치명적이다(§3.1 OPEN-11). 이제 후보 payload 는 `price` 대신 `priceLevel`(매우저렴/저렴/보통/비쌈/매우비쌈/정보없음)만 싣는다 — **흘릴 숫자 자체가 프롬프트에 없다.**
  - **코드 가드 강화(B안)를 택하지 않았다.** 종전 `_reason_leaks_nondisplay` 는 `값+원` 형태만 잡아 `"39000이면 저렴"` 처럼 **통화 단위만 빼면 우회**됐고, 맨숫자까지 잡도록 보강하면 스펙 숫자 오탐(`39000mAh`)과 다른 표현 우회로 whack-a-mole 이 재발한다. 프롬프트 가드(soft)는 LLM 이 어길 수 있어 1차 방어가 될 수 없다 — 다만 **제거하지 않고 2차 방어로 유지**했다(금액·가격 숫자 금지 문구).
  - **절대 등급이 아니라 후보군 상대 등급이다.** rating(4.5/5.0)과 달리 price 엔 절대 기준이 없다 — 39,000원은 노트북이면 싸고 볼펜이면 비싸다. 그래서 **같은 그룹 중앙값 대비 비율**로만 등급화한다. 분위수(quantile) 방식은 기각했다 — 후보 가격이 1% 안에 몰려 있어도 강제로 1/3 을 '비쌈' 으로 밀어내 **거짓 근거**를 만든다. 중앙값 대비 비율은 그 경우 전부 '보통' 으로 수렴해 정직하다(의도된 붕괴이므로 회귀 테스트로 고정).
  - **그룹은 니즈(need) 단위다.** 니즈별 분할 턴(REQ-REC-024)의 후보엔 성격이 전혀 다른 상품이 섞이는데(`"노트북이랑 마우스"` → 150만원대 + 2만원대), 전체 하나로 중앙값을 내면 **마우스는 전부 '매우저렴'· 노트북은 전부 '매우비쌈'** 이 되어 근거문이 적극적으로 틀린다. need 미매핑 후보끼리는 따로 한 그룹이고, 가격 부재·비양수 중앙값은 '정보없음' 이다.
  - **정확한 price 는 그대로 남는다** — 티어화는 **LLM 에게 보여주는 값**에만 적용된다. `SpringProduct.price` 원본은 불변이고, `maxPrice` 는 사용자 필터가 정확값 그대로 Spring 에 전달되며(AI 가 계산하지 않는다) `totalBudget`/`verifiedSum` 은 이 그래프가 아직 내지 않는다(#60·#163). 즉 **가격 필터·예산 경로는 무영향**이며, 이 경계를 회귀 테스트로 고정했다.
  - **트레이드오프는 가격 랭킹 정밀도다** — 같은 등급 안에서는 LLM 이 가격 우열을 구분할 수 없다. 셋 코드·기본 임계(0.6/0.85/1.15/1.5)로 후보 30개를 실측하면 가격 쌍의 **식별률이 로그정규 분포 80% · 넓은 분산 79% · 보통 분산 68%** 이고, **중앙값 ±10% 로 몰린 균질 상품군에서는 전원 '보통' 이라 0%** 다. 마지막 경우는 애초에 가격차가 랭킹 근거가 되기 어려운 구간이라 의도된 동작으로 본다. 임계는 `app/core/config.py` 주입(`price_tier_*_ratio` 4개)이고 경계 순서·겹침은 기동 시점 fail-fast 로 막는다. **와이어 계약 불변** — 엔드포인트·SSE 이벤트·필드·오류 코드 어느 것도 바뀌지 않아 api-spec 개정이 없다.
- **#217 — 전개 트리거를 목적 marker 열거에서 "매핑 실패"로, 전개 배선을 교체에서 합집합으로** (#115 동시 종료). #198 의 전개는 목적 marker 열거(`선물`·`용품`·`아이템`…)로 발동했는데, 열거라 목록에 없는 표현을 통째로 놓쳤다 — `"김밥 재료"`·`"감자탕 재료"` 가 대표 사례이고 #198 §15.3 이 기록한 미검출(`아이디어`·`제품`·`필수품`)도 같은 원인이다. **`재료` 를 목록에 추가하는 처방은 실측으로 기각됐다** — `재료` 로 끝나는 표현 6건 중 4건(`한방재료`·`떡볶이 재료`·`수예 재료`·`베이킹 재료`)이 **이미 정답 매핑**이라 오탐 4 / 회수 2 로 손해가 더 컸다. `세트`·`모음` 을 하나씩 더해도 같은 함정이 반복된다.
  - **미리 맞히지 않고, 매핑을 해본 뒤 실패했을 때 전개한다.** 판별 신호를 새로 만들지 않았다 — #115 §4.5 의 **거리·마진**이 이미 그 판정이다(`거리 초과 + 마진 얇음`). 그래서 **새 튜너블이 0개**다. `needs_expansion_purpose_markers` 는 삭제됐고, 전개 전용 거리 임계(0.22→0.20)를 두는 안도 실측으로 기각했다 — 동기였던 `베이킹 재료` 는 **마진이 0.0397 로 두꺼워** 거리를 낮춰도 안 잡히고, 정상 상품명 오탐(`노트북` 0.2011·`블루투스 스피커` 0.2171)만 늘었다.
  - **무엇을 실패로 볼지가 이 변경의 핵심이다.** `map_categories` 가 `CategoryMapping(legs, unresolved)` 를 내되 `unresolved` 에는 **거리컷 드롭·택일 null 만** 담고 **조회 예외·히트 0건은 담지 않는다** — 실패 원인이 발화 내용이 아니라 인프라·시드 상태라 LLM 전개로 풀리지 않고, 섞으면 pg 순간 장애가 전개 호출을 부른다(PR #188 이 `error_type` 으로 "인프라 장애 vs 코드 버그"를 가른 것과 같은 원칙). 종전 반환형(leg 리스트 단독)으로는 호출부가 "드롭됐다"와 "애초에 신호가 없었다"를 구분할 수 없어 열거에 기댈 수밖에 없었다.
  - **전개 결과를 교체가 아니라 합집합으로 얹는다.** 부분 실패에서 사용자가 **명시한** 카테고리를 잃지 않는다 — `"이사 가는데 냉장고랑 필요한 것들"` 이 종전엔 `[행거, 수납박스, 커튼]` 으로 갈아엎혔지만 이제 `[냉장고, 행거, 수납박스, 커튼]` 이다. 원 leg 이 **앞**이라 `category_fanout_max` 절단에서 먼저 살아남고, `legs[0]`(조건 칩·멀티턴 승계 대표값)도 안정적으로 유지된다. 부수 효과로 **전개가 원 leg 을 잃게 만드는 경로가 구조적으로 사라져** §7 "후퇴 없음" 보장이 강해졌다 — 종전 교체 배선은 전개 호출이 "성공"하되 내용이 엉뚱할 때 원 leg 을 잃었다.
  - 합집합이 필요한 이유는 실측이 보여준다: 전개는 공짜가 아니다. `한방재료` → `구기자`→`구기/라켓/스포츠`(0.3073/0.0412 **채택**), `수예 재료` → `지퍼`→`집업`·`바늘`→`당뇨 침/바늘` 처럼 **전개된 짧은 단어가 문자열 겹침으로 오분류**된다(DESIGN-59 §4.3.1 의 "가짜 근접"이 다른 층에서 재발). 전개 leg 에 마진 예외를 빼는 완화책은 오염 2개를 없애는 대가로 정답 4개(`버터`·`향초`·`돼지등뼈`·`대파`)를 잃어 기각했다.
  - `D1(no_legs)` 과 `case != 3` 게이트는 **유지**한다. 게이트는 오히려 더 중요해졌다 — case 2 leg 은 taxonomy 에 맞는 칸이 없는 것이 정상이라 **매핑 실패가 구조적으로 발생**한다(`'평점 높은 거'` → `게임 > PC게임` 0.3420/0.0171). 게이트가 없으면 "카테고리 무관·조건만"(#22·#162) 의도가 지어낸 상품 목록으로 좁혀진다. 전개는 **턴당 1회**로 고정 — 전개 결과가 전부 매핑 실패하는 회차가 실제로 있어(`김밥 재료` → 김·단무지·시금치가 전부 거리컷) 재트리거하면 턴이 끝나지 않는다.
  - 실측(`categories` 2056행 전량 임베딩 시드된 실 pg-catalog, 재현 `scripts/verify_expansion_trigger_217.py`): **대조군 오탐 0/8** — `한방재료`(0.1443/0.0640)·`떡볶이 재료`(0.1736/0.1438)·`수예 재료`(0.1590/0.0317)·`베이킹 재료`(0.2068/0.0397)·`한우 선물세트`·`청바지` 전부 미전개 유지. **회수 4/4** — `김밥 재료`(0.3027/0.0054)·`감자탕 재료`(0.3177/0.0213)·`집들이 선물 아이디어`(0.3156/0.0133)·`자취 필수템`(0.2736/0.0202). marker 삭제로 잃는 것은 **이미 정확히 매핑되는 표현을 더 쪼개는 것**뿐이다(걸리던 18건 중 8건은 매핑도 실패해 그대로 잡히고, 나머지 10건은 카테고리가 정확하다).
  - **알려진 한계**: `베이킹 재료` 는 top1 이 `주방용품 > 홈베이킹용품`(**도구**)이라 내용은 빗나갔지만 거리·마진이 모두 성공 구간이라 어떤 신호로도 잡을 수 없다. 잡으려면 #115 가 실측으로 정한 `category_select_margin_max` 를 건드려야 하고 그러면 정상 질의에 LLM 택일이 붙는다 — "칸은 있는데 어느 것인지 모른다"는 **#222**(LCA 확장·재질문)의 문제 영역이라 그쪽에 맡겼다. `돌잔치 답례품`·`캠핑 용품` 이 카테고리 1개로 유지되는 것도 같은 선택이며, 관측 로그(`needs_expansion_triggered.reason` + `needs_expansion_union`)로 재판단할 근거를 쌓는다.
  - 관측: `reason` 이 `no_legs`/`mapping_failed` 로 바뀌고 `unresolved`(실패 앵커)를 동반한다. `needs_expansion_union`(원/전개/병합 leg 수) 신설. **`utterance_copy`·`purpose_marker` 값은 소멸**하므로 이 값을 쓰는 집계·대시보드는 함께 갱신해야 한다.
  - **api-spec 무변경** — 전개·매핑은 AI 내부 단계이고 `categoryName` 은 종전대로 canonical 또는 null. `SPEC-RECOMMEND-001` 도 v0.10.0(EX-7·AC-REC-37)에서 이미 전개 호출 1회를 허용한다. 설계는 새 문서를 만들지 않고 `docs/specs/DESIGN-NEEDS-EXPANSION-198.md` 를 개정했다(§4·§4.0·§4.4·§4.5·§6·§9·§10·§13) — #115 가 `DESIGN-CATEGORY-HYBRID-59.md` 를 개정한 것과 같은 방식으로, 주제당 1문서라야 § 번호가 코드↔명세 링크로 계속 작동한다.
- **#209 — `RecommendationPush`를 다중 목록(`lists[]`)으로 전환** (api-spec §4.2, v0.17.1 — 명세는 선행 개정 완료, 이 항목은 코드 정합). 사본 §4.2가 `lists[]`로 개정되고 **jarvis-back도 신 형식 구현을 마친 상태**에서 우리 코드만 구 평평 3필드(`listId`·`productIds`·`reasons`)로 보내고 있었다. BE는 `RecommendationCallbackRequest.resolvedLists()`로 구 형식을 목록 1건으로 접어 과도기 수용 중이었고 — 런타임 장애는 없었지만 그 과도기 코드의 수명이 이 전환에 걸려 있었다.
  - **`RecommendationListEntry` 신설 + 최상위 `lists[]`** — 목록이 1개여도 길이 1 배열로 보낸다. 단일/복수로 형식이 갈리면 FE·BE 양쪽에 분기가 생긴다. 구 평평 3필드는 최상위에서 사라졌다(BE 과도기 코드 제거 가능).
  - **`listType`은 항상 싣는다** — 현재 추천 그래프의 산출은 "후보 중 하나를 고르는" 단일 목록이라 `PICK_ONE`이다. 목록 개수는 `lists` 길이로 알 수 있지만 이 값은 개수로 복원할 수 없어 서버가 말해줘야 한다. `BUY_ALL`(세트 복수안)·`totalBudget`·`label` 생성은 이 그래프의 범위 밖이다(#60·#163).
  - **`recommendationRequestId` 발급** — 추천 실행 1회당 정규 UUID 1개(BE `CHAR(36)`). `listId`(`uuid4().hex`)와 **역할이 달라 별도로 발급**하며, 멱등 키가 (`recommendationRequestId`, `listId`) **쌍**이라 한 콜백 안 `listId` 중복은 스키마가 거절한다 — 단독 키로 접히면 한 실행의 두 번째 이후 목록이 재전송으로 오해돼 조용히 버려진다(#140의 상관키와 같은 대상).
  - **계약 상한을 스키마에 고정** — `lists` 1~10개, 목록당 `productIds` 1~9개·중복 금지, `reasons` ≤9, `listId` 허용 문자(영숫자·`-`·`_`)·≤64자, `label` ≤50자. 종전의 방어적 `max_length=50`은 계약 하드 값으로 좁혔다. 실제 노출 개수는 그대로 config(`expose_min`~`expose_max`)가 정한다.
  - **`products.ready.listIds`는 push한 `lists`에서 파생** — 순서·개수가 콜백과 같음이 코드로 보장된다(§4.2 규약, §3.1). 미지정 선택 필드(`totalBudget`·`label`)는 `exclude_none`으로 생략한다.
  - 통합 스텁(`tests/integration/_stubs.py`)이 **전환 완료된 BE**를 재현한다 — 구 평평 형식·`listType` 누락·`listId` 중복을 400으로 거절해, 구 형식으로 되돌아가면 통합 테스트가 깨진다.
- **#209 후속 — 니즈별 추천이 목록 여러 개로 나간다(`PICK_ONE`×N)** (api-spec §3.3 v0.17.3 / SPEC-RECOMMEND-001 v0.11.0 REQ-REC-021·024·096). `lists[]`를 전송할 수 있게 된 바로 그 계약의 첫 실사용이다. 종전엔 "유럽여행 필요한 거"의 **파우치 후보와 어댑터 후보가 한 카드 묶음에 뒤섞여** 나갔다 — `_merge_fanout_results`가 leg 결과를 인터리브 병합하면서 **어느 니즈에서 온 상품인지를 버렸기 때문**이다. SPEC은 이미 REQ-REC-012에서 *"니즈별 병렬 검색 + 결과를 카테고리 단위로 그룹화"* 를 요구하고 있었고, 그 그룹이 **어디로 나가는지**만 비어 있었다.
  - **leg 정체성 보존** — 병합이 `productId → 원본 leg 인덱스` 맵을 함께 돌려준다. 인덱스는 **원본 `category_legs` 기준**이라 실패해 빠진 leg 때문에 밀리지 않고, 중복 상품의 leg는 **round-robin 최초 등장**(실제 채택 위치) 기준이라 상품이 엉뚱한 니즈 목록에 들어가지 않는다.
  - **분할 조건은 `case == 3` + 니즈 2개 이상** — 목적·상황형 발화가 전개된 턴만 나눈다. 리파인 승계 같은 멀티 leg는 종전대로 목록 1건이다.
  - **LLM 호출은 늘지 않는다** — 전역 rerank **1회** 결과를 leg로 그룹핑한다(REQ-REC-023 "니즈 수만큼 무제한 fan-out 금지" 유지). 대신 rerank 예산을 `expose_max × 니즈 수`(후보 수 상한)로 넓혀 한 니즈가 예산을 독식하지 않게 했다. 회귀 테스트가 `smart` 호출 1회를 고정한다.
  - **노출 보정·상한이 목록 하나 기준으로** (REQ-REC-021 개정) — 전역 상한이면 상위를 독식한 니즈만 채워져 어댑터가 통째로 사라지고, 전역 보정이면 목록마다 후보가 1~2개인 **"고를 것이 없는 `PICK_ONE`"** 이 나온다. 굶은 니즈는 **자기 leg의 검색순서**로만 채운다 — 다른 니즈 상품이 섞이지 않는다. REQ-REC-096의 "니즈당 노출 반비례 축소"는 이 이유로 철회했고(축소 대상은 **입력**이지 노출이 아니다), 입력 예산 고정은 그대로다.
  - **`label`에 니즈 이름**("파우치"·"어댑터") — leg 검색어를 쓰고 없으면 canonical 카테고리로 폴백하며, LLM 산출 자유 텍스트라 push 직전 정제 + 계약 상한(≤50자)으로 자른다. 후보가 하나도 안 남은 니즈는 **목록을 만들지 않는다**(빈 목록은 보내지 않는 것이 맞다, §4.2). `reasons`도 그 목록의 상품으로만 좁혔다.
  - **rerank에 니즈 경계를 알린다**(PR #212 리뷰). 예산만 늘리고 프롬프트는 니즈를 모르면 LLM이 후보를 **전역 관련도로만** 정렬한다 — 한 니즈가 상위권을 쓸면 굶은 니즈는 검색순서 보충으로 채워지고, 그 보충분엔 rationale이 없어 **근거 없는 카드**가 나간다. rerank는 "정상 성공"이라 `rerank_degraded`로도 드러나지 않는다. 후보마다 소속 니즈(`need`)를 싣고 "니즈마다 상위 N개까지 균형 있게" 지시를 덧붙이되, **분기를 `_SYSTEM`이 아니라 user 메시지에** 둬 단일 목록 경로의 프롬프트는 한 글자도 바뀌지 않는다(#198에서 프롬프트에 지시를 얹었다가 성공률이 3/3→1/3으로 희석된 실측 전례). 관측 로그에 `without_reason`(근거 없이 나간 카드 수)을 추가했다.
  - **rerank 출력 예산을 노출 개수에 비례**하게 바꿨다(PR #212 리뷰). 종전 `max_tokens=1500` 고정은 항목이 8~9개일 때 맞춘 값인데, 니즈별 분할이면 한 번의 rerank가 목록 수만큼 항목을 낸다. 프롬프트 상한(한글 40자) 기준 27~30개면 출력이 잘리고 `extract_json`이 파싱에 실패해 `LLMError` → **근거 없는 degrade**로 떨어진다 — "니즈별 근거 있는 추천"이 정작 니즈가 여러 개일 때 더 자주 깨지는 구조였다. `rerank_max_tokens_base + per_item × expose_max`(config 주입)로 바꾸되 기본값은 단일 목록 경로(`expose_max=9`)에서 **종전 1500과 정확히 같도록** 잡아 흔한 경로의 동작을 바꾸지 않았다. 예산을 세는 단위도 요청 leg 수가 아니라 **후보가 실제로 남은 니즈 수**다. 관측 로그(`recommend_pipeline`)에 `lists`·`expose_budget`을 추가해 degrade가 다중 니즈에서만 튀는지 분리해 볼 수 있게 했다.
- **#209 후속 — `expose_max` 8 → 9, 그리고 설정이 계약 상한을 넘지 못하게 묶었다** (PR #212 리뷰 반영, api-spec §3.3 v0.17.3 / REQ-REC-021). §4.2가 목록당 9개를 허용하는데 노출 상한이 8이라 **계약 상한이 코드에서 도달 불가능한 값**이었다. 반대로 `expose_max`를 9 초과로 튜닝하면 `RecommendationListEntry` 생성에서 `ValidationError`가 나는데, 그 지점은 `SpringUnavailableError` degrade 블록 **밖**이라 §3.3의 "목록을 준비하는 데 문제가 있었어요" 대신 **일반 `INTERNAL`로 SSE 스트림이 끊긴다**. 계약 상한 상수(`LIST_MAX_PRODUCTS` 등)를 스키마 한 곳에 두고 config가 그 값을 `le`로 참조해, 잘못된 설정을 **런타임이 아니라 기동 시점**에 잡는다. `expose_min > expose_max`(보충 루프가 상한에 되잘리는 모순)도 함께 거절한다.

### Fixed
- **#276 — 완화 칩 기억이 스레드 종료 후 영구 잔존하던 것 수정** (#113 후속). `RelaxationOfferStore` 가 쓰는 `buyer_relaxation_offers_v1` 이 `session_state.clear_thread` 에 등록돼 있지 않아, 스레드가 끝나도 그 스레드의 완화 칩 스냅샷(`offers`·`applied`)이 pg-profile 에 무기한 남았다. `clear_thread` 가 루트를 열거하는 유일한 지점이라(legacy GC 는 legacy→v2 쌍만 다룬다) 회수해 갈 다른 경로도 없었다. 루트를 등록하고 `CleanupCounts.relaxation_offers` 를 추가해 정리 여부가 카운터에도 잡히게 했다 — 종전에는 새고 있다는 사실 자체가 관측되지 않았다. `CleanupCounts` 는 `session_lifecycle` 내부 전용이라 와이어 계약 변경은 없다. 부분 실패 복구 경로 테스트(`_seed_v2_state`/`_assert_v2_*`)에도 이 루트를 넣어, 중간에 터진 정리를 다음 sweep 이 재시도할 때 새 루트까지 회수됨을 함께 고정했다. `SPEC-CHAT-SESSION-CONTEXT-187` §3 의 namespace 목록도 실제와 맞춰 갱신(#232 의 `buyer_repurchase_v1` 누락분 포함).
- **#236 — `priceLevel` 그룹핑이 `need_of` 없는 턴을 비켜가 상품군이 섞인 등급을 내던 회귀 수정** (#173 후속). 위 #173 항목의 *"그룹은 니즈(need) 단위다"* 는 **니즈별 분할 턴에서만** 참이었다. `need_of` 는 `case == 3` + 니즈 2개 이상일 때만 만들어지므로, 그 밖의 턴은 전 후보가 한 그룹이 되어 #173 이 막으려던 바로 그 왜곡(*"마우스는 전부 매우저렴 · 노트북은 전부 매우비쌈"*)이 그대로 재현됐다. 이제 `need_of` 가 없으면 **후보 자신의 `category`** 로 그룹을 나눈다.
  - **이슈 본문의 원인 분석을 실측으로 정정했다.** 본문이 든 "리파인 승계 턴"은 `category_legs = [(prior.category, None)]` 로 **leg 을 1개만** 만들어 해당하지 않고, `"노트북이랑 마우스"` 는 decompose 프롬프트가 *"서로 다른 상품 2개 이상"* 을 case 3 으로 정의해 이미 정상 동작한다. 실제 주 경로는 **대분류 leg 1개의 leaf 확산** 이다 — I-1 요청 `categoryName` 은 대분류여도 응답 `[].categoryName` 은 leaf 라(§4.6), 노트북 대분류 하나로 검색해도 `브랜드PC`(100만원대)·`SSD`(10만원대)·`노트북가방`(수만원대)이 한 후보군에 섞인다. 나머지는 leg 이 아예 없는 단일 filters 검색과 decompose 의 `case` 누락(`int(data.get("case") or 2)`)이다.
  - **그래서 `leg_of` 전달안(B)이 아니라 `category` 그룹핑(A)이다.** 주 경로는 leg 이 1개이거나 아예 없어 leg 단위 그룹이 곧 전역 그룹이라, B안은 이 세 경로 중 둘을 전혀 고치지 못한다. 부수 효과로 `recommendation/graph.py` 를 건드리지 않아 #113 과 병렬 작업이 가능했다.
  - **비교 대상이 없는 그룹은 전역 중앙값이 아니라 '정보없음' 이다.** 전역 중앙값은 후보 전체가 섞인 값이라 작은 그룹을 거기로 보내면 이 이슈의 왜곡을 그 그룹에만 다시 씌운다(노트북 20건이 지배하는 중앙값에 마우스 2건을 재는 꼴). `priceLevel` 은 정의상 **후보군 상대 등급**이라 비교 대상이 없으면 등급이 존재하지 않으며, 종전처럼 median==자기자신으로 '보통' 을 주면 근거 없는 *"가격대는 보통이에요"* 가 사용자에게 나간다. 하한은 `price_group_min_size`(기본 2, config 주입) 미만인 **유효 price 보유 멤버 수**이며, 2건 그룹은 등급이 정상적으로 갈려(비슷하면 둘 다 보통, 다르면 저렴/비쌈) 싱글턴만 막는 최소 개입이다. **새 등급 문자열도 프롬프트 수정도 없다** — `_price_tier` 가 이미 `median is None` → '정보없음' 이다.
  - **`need_of` 경로는 바이트 단위로 불변** — 니즈 경계는 상위가 내린 판정이라 그룹이 곧 정답이고, category 는 휴리스틱 파티션이라 완화해도 된다는 구분이다. 키 산출도 `need_of.get(...) or category` 가 아니라 **하드 분기**다(`or` 면 니즈 미매핑 후보가 category 로 샌다). 판정은 truthy 라 **빈 dict 는 `None` 과 같은 "니즈 없음"** 이다(PR #274 리뷰) — `is not None` 으로 보면 프롬프트는 니즈 없는 턴으로 나가면서(`rerank()` 본문은 truthy) 그룹핑만 니즈 경로를 타, 전 후보가 단일 그룹으로 묶이는 이 이슈의 버그가 그대로 재발한다. 아울러 중앙값을 그룹 키 dict 가 아니라 **후보 순서에 1:1 정렬된 리스트**로 바꿔, 종전에 호출부가 키를 두 번째로 산출하다 어긋나면 `KeyError` 로 턴 전체가 미처리 예외로 죽던 구조를 제거했다(`zip(..., strict=True)` 로 정렬이 깨지면 즉시 터진다). productId 키도 쓰지 않았다 — 비-fanout 경로엔 productId dedup 이 없어(`_parse_search_response`·`search_catalog`) 중복이 오면 조용히 한쪽으로 접힌다.
  - **와이어 계약 불변** — `priceLevel` 은 LLM 프롬프트 내부 필드로 SSE·Spring 어느 와이어에도 실리지 않아 api-spec 개정이 없다(#173 과 동일 근거). 신규 테스트는 뮤테이션(구 동작·`or` 배선)으로 판별력을 확인했고, 기존 #173 테스트 2건은 후보 전원이 `category=None` 이라 `or` 배선을 잡지 못함도 함께 확인했다.
- **#232 — 재구매 되돌리기 멀티턴 지속** — 명시한 최근 구매 상품의 exact 제외 면제를 스레드별로 유계 누적하고 매 턴 최신 본인 구매 이력과 재검증해, 후속 조건 다듬기에서도 지목 상품은 유지하되 오래되거나 취소·반품된 상품과 다른 상품으로 면제가 번지지 않게 했다. (api-spec §4.7, v0.19.5)
- **모순된 가격 구간이 조용한 0건으로 새던 문제** — 멀티턴 병합이 프롬프트 산문 한 줄("좁히면 add, 모순되면 replace")에 맡겨져 있어, "3만~5만" 다음 턴 "2만원 이하만"에서 LLM이 상한만 갱신하고 하한을 물고 오면 `price_min=30000 > price_max=20000`이 된다. 이 쌍은 스키마를 통과해 Spring에 `minPrice=30000&maxPrice=20000`으로 나가고 **오류 없는 0건**으로만 드러나 추적이 안 됐다. `decompose`가 **prior와 같은 쪽**(지난 턴에서 딸려온 값)을 버려 이번 턴에 말한 조건만 남긴다 — 모순일 때만 개입하므로 "4만 이하" 같은 정상적인 좁히기는 하한이 그대로 산다. 스키마에서 거부하지 않는 이유는 그 `ValidationError`가 `LLMError`로 통일돼 **턴 전체가 오류로 끝나기** 때문이다(사용자는 정상 발화를 했고 병합을 그르친 건 LLM이다). 저장된 prior는 칩 클릭 경로에서 base로 직접 쓰여 이 보정을 우회하므로 `ThreadFilterStore.get()`에서도 푼다.
- **수치 필터에 범위 제약이 없던 문제** — `price_min`/`price_max`/`rating_min`에 `ge=0`이 없어 손상된 저장 값(`-50000`)이 검증을 통과해 그대로 Spring 쿼리 파라미터가 됐다. 스키마에 제약을 걸어 출처(decompose·저장 오퍼·멀티턴 병합) 전체를 한 곳에서 막는다. 제약은 **이미 저장된 레코드에도 소급 적용**되므로, 그 전에 저장된 음수를 가진 스레드가 `ThreadFilterStore.get()`에서 매 턴 죽지 않도록(그 호출은 decompose보다 앞이라 정상 degrade 이벤트조차 못 낸다) 읽기 실패를 `None`으로 떨궈 다음 `put`에 스스로 회복하게 했다.
- **대화 store 장애 시 판매자 챗이 503 대신 500을 반환하던 문제** — 같은 pg-profile
  장애가 구매자 `/chat`에서는 `503 STATE_UNAVAILABLE`, 판매자 `/seller/chat`에서는
  `500 INTERNAL`로 갈렸다. 스트림 개시 전 `get_conversation_store()` 실패를
  `chat.py`만 `is_state_store_unavailable`로 판별해 `SessionStateUnavailable`로
  변환하고 `seller.py`는 그대로 전파했기 때문이다. 재시도 가능 여부라는 신호가
  레인마다 달라 FE 재시도 정책과 5xx 알람 집계가 원인 하나에 두 갈래로 흩어졌다.
  이제 판매자 경로도 같은 경계 함수를 쓴다. 변환 대상은 실제 I/O 장애
  (`TimeoutError`·`PoolTimeout`·`OperationalError`)뿐이며 programming/domain 오류를
  503으로 마스킹하지 않는다 — 코드 버그가 "일시 장애"로 묻히면 안 된다.
  (api-spec §2.5 — 정본에 이미 등재된 코드라 명세 개정 없음)
- **#253 — 옵션 되물음 중 해소되지 않은 상품 전환이 옛 상품을 담던 문제** — 실측에서 `fast`가
  `"다른 거 담아줘"`를 5/8회 pending 상품으로 에코하는 등 총 12회 옛 `productId`를 되돌렸고,
  그 턴의 임의 `optionId`까지 소비하면 사용자가 고르지 않은 옵션으로 옛 상품이 담길 수 있었다.
  이제 전환 표지가 있는 발화의 에코·`null` 양식을 모두 pending 해제 후 기존 상품 재질문으로
  보내고 해당 턴의 옵션을 버리되, pending 옵션명을 말한 `"아니 파란색이요"` 같은 정정 발화는
  전환으로 오인하지 않는다. 지목한 새 상품 전환과 순수 옵션 답변은 기존대로 처리한다.
- **#237 — 홈 추천 로그 비노출 테스트의 스레드 ID 우연 일치 flaky 수정** — `test_log_has_fixed_safe_key_set_only`가 `LogRecord.__dict__` 전체의 큰 숫자 필드에서 금지 상품 ID 부분 문자열을 우연히 찾아 간헐 실패하던 문제를, 메시지와 앱이 `extra`로 싣는 필드만 검사하도록 범위를 좁혀 실제 유출 차단 의도는 유지하면서 제거했다.
- **#234 — 상품 지시대명사 intent가 추천·장바구니 레인 사이에서 흔들리던 문제** — `cart_view`를
  장바구니 자체를 명시한 조회로, `cart_add`를 명시적 담기 동사 또는 실제 옵션 답변으로 한정하고,
  `"그거"`·`"저번에 그거"`는 직전 검색/추천 상품으로 해소해 `recommend`로 분류하도록 질의 분해
  프롬프트를 보강했다. 실 `gpt-5-nano` 10발화 × 3컨텍스트 × 8회에서 목표 intent가 수정 전
  171/240이었다. #120 병합 뒤 `PENDING_CART` 설명을 intent 사다리와 일치시킨 최종 `_SYSTEM` 해시
  `e5e195822495`의 재측정 2회(N=8)는 각각 234/240·236/240이었고, 장바구니 조회·담기 대조군은
  두 실행 모두 144/144를 유지했다. 옵션 이름·번호·순번 선택을 intent 사다리의 0단계로 올려 옵션
  답변 4종도 두 실행 모두 `cart_add`와 올바른 `optionId` 32/32였다(`"2번으로"` baseline 7/8 →
  각 8/8). 지시대명사의 맥락 없음·직전 추천 셀은 63/64·64/64 `recommend`였고, pending-cart
  미달은 5/32·4/32(평균 4.5/32)로 종전 해시 `130695f72002`의 8/32보다 줄었다. 첫 실행은
  `"저번에 그거 다시 사고 싶어"` `cart_add` 2/8, `"그거 또 사고 싶어"` `cart_add` 1/8,
  `"그거 보여줘"` `cart_view` 2/8이었고, 둘째 실행은 각각 1/8·2/8·1/8이었다.
- **#120 — 명시한 최근 구매 상품을 다시 추천받지 못하던 비대칭 해소** — `repurchaseProducts` 상품명을 본인 최근 구매 이력 안에서만 productId로 해소해 exact 제외와 해당 상품의 소모품 카테고리 억제를 함께 면제한다. 사용자가 말로 지목한 **단일 상품만** 신뢰해, 복수 지목·매칭 실패·신호 없음이면 기존 제외를 유지하고 후보나 LLM 정수 id로 해제 범위를 넓히지 않는다. 단일 지목은 가장 좁은 해석을 골라(완전 일치가 있으면 그것만, 없을 때만 단방향 부분비교) 형제 상품 동시 해제와 긴 지목의 짧은 구매명 축약을 막는다(PR #230 리뷰·재검증).
- **#133 — 구매자가 품질 저하를 숨기던 문제(판매자와 비대칭)** — rerank(개인화 재정렬 + 상품별 추천 이유)가 실패하면 검색 순서 상위로 degrade 하면서 개인화와 상품별 근거(I-21 `reasons`)가 **통째로** 사라지는데, 사용자에게 나가는 문구는 `"요청하신 조건으로 찾은 상품들이에요."` 로 **평상시와 구분되지 않았다.** 오히려 "조건으로 찾은"이 조건에 맞게 골라줬다는 인상을 줘, 실제로는 개인화가 0인 결과가 더 그럴듯하게 읽혔다. 같은 저장소가 판매자에는 degrade 정직성 게이트(`verifier.check_degrade_disclosed` — 데이터 확보 실패 finding 이 있는데 보고서가 한계를 명시하지 않으면 검증 실패로 되돌린다)를 두고 구매자에는 두지 않은 비대칭이다. 이제 폴백이 품질 저하를 고지한다.
  - **문안이 "취향"이 아니라 "추천 이유"를 지목한다** — 게스트는 프로필이 없어 평상시에도 취향 반영이 없으므로 "취향까지 반영하지 못했어요"는 그들에게 **참이 아니고**, 정작 잃은 것도 안 밝혀진다. 반면 추천 이유는 프로필과 무관하게 폴백에서 항상 사라지고 **사용자가 카드로 확인할 수 있다** — 검증 가능한 고지가 검증 불가능한 주장(정렬 품질)보다 신뢰를 만든다. 문구가 `products.ready` 보다 먼저 나가므로 곧 도착할 빈 카드를 미리 설명하는 자리이기도 하다. `"상품을 골라 드리지 못했어요"` 류는 0건 문구(`"조건에 맞는 상품을 찾지 못했어요"`)와 유사해 "상품이 없다"로 오독될 수 있어 배제했다.
  - **문구 3종을 config 로 뺐다**(`rerank_fallback_notice`·`push_skipped_notice`·`dedup_skipped_notice`) — 이 저장소에 한국어 사용자 문구를 config 에 둔 첫 사례다. **문안만 튜너블이고 고지 여부는 튜너블이 아니다** — 초판은 셋 모두에 "빈 문자열 = 고지 끄기"를 뒀는데, 그건 이슈가 요구한 *문안 config 주입*을 넘어 **정직성 자체를 옵션으로** 만든 것이었다(환경변수 한 줄로 이 수정이 원상복구된다). api-spec §3.3 이 발신을 규정하는 두 문구는 빈 값을 기동 시점에 거부하며, 계약이 요구하지 않는 `dedup_skipped_notice` 만 빈 값을 허용한다(PR #235 리뷰). config 값은 운영자 주입이라 소스 리터럴이 아니므로 정상 경로와 같은 `_strip_unsafe` 정제를 거친다(#67 규약). 실패 단계명·오류 코드는 싣지 않는다(api-spec §3.3 "단계별 상세는 서버 로그 전용").
  - **`dedup_skipped`(최근 구매 제외 실패)는 기본 미고지로 판단했다** — 조회 실패는 "중복이 노출됐다"가 아니라 "걸러내지 못했다"라 실제 중복 발생 여부를 알 수 없고, rerank 폴백과 달리 거짓 주장을 하고 있지도 않다. 매 턴 붙는 안내는 노이즈이므로 문구만 비워 두고 판단을 되돌릴 여지를 남겼다. 다만 **게스트(이력이 없는 것)와 조회 실패(고장난 것)를 구분**하는 플래그는 배선했다 — 종전에는 둘 다 `None` 이라 호출부에서 갈라낼 수 없었고, 없는 기능을 "고장났다"고 고지하면 거짓말이 된다.
  (api-spec §3.3, v0.19.1 — 계약 불변·FE 무변경 / `docs/specs/SPEC-RECOMMEND-001.md` v0.13.1 REQ-REC-064·AC-REC-13b)
- **#133 — I-1 상품 검색에 재시도가 없던 문제** — 검색이 실패하면 후보가 0건이라 추천이 성립하지 않아 턴이 종료되는데(`SEARCH_FAILED`), **한 번 실패하면 그걸로 끝**이었다. Spring 타임아웃은 3초로 짧아 일시 지연이 재시도로 살아나는 폭인데도 LLM 만 30s + 1회 재시도를 갖고 검색은 0회인 비대칭이었다(`grep -n retry app/services/spring_client.py` → 0건). **게다가 이건 새 요구가 아니었다** — `SPEC-RECOMMEND-001` 오류 처리 표가 이미 *"`search` 실패: 최대 1회 재시도 후 `error`(`SEARCH_FAILED`)"* 를 규정하고 있었고, 명세에만 있고 구현되지 않은 채 남아 있었다.
  - **재시도 대상을 재시도가 의미 있는 실패로 한정했다** — 타임아웃·연결 오류·응답 중단·5xx·일시 4xx(408·429) 만. 그 밖의 4xx 계약 오류와 응답 파싱 실패는 **같은 응답이 또 오므로** 재시도가 결과를 바꾸지 못하고 first-token 예산만 태운다. 종전 코드는 `except (httpx.HTTPError, ValueError, ValidationError)` 한 줄로 타임아웃·연결거부·500·400·JSON 파싱 실패를 전부 뭉갰기 때문에, 판별은 그 flatten **이전**에 해야 했다.
  - **재시도 루프를 trace span 안쪽에 뒀다** — 바깥에 두면 시도마다 span 이 나가 "호출당 유계 transport 메타데이터 1건"이라는 관측 계약이 깨진다(테스트 3건이 고정 중). `statusClass` 는 마지막 시도의 결과를 남기므로 "503 뒤 200"은 성공한 호출이라 `2xx` 다. 재시도 사실은 예외 원문 없이 유계 라벨만 실은 구조화 로그로 관측한다(#141 규약).
  - **재시도 총량을 기동 시점에 검증한다** — `spring_timeout_s × (spring_max_retries + 1) < stream_total_timeout_buyer_s`(#138 로 갈린 구매자 전용 30s — I-1 검색은 구매자 경로 전용이라 판매자와 공용인 90s 와 비교하면 검증이 이름만 남는다). **두 상한과 함께 비교한다**: 재시도가 갉아먹는 것은 턴 전체 시간이라 전체 상한과 묶되, first-token 상한(10s)과도 비교한다. 초판은 파이프라인 그림만 보고 "검색이 첫 토큰보다 앞"이라 적었다가 #241(#138)의 *"상한이 실제로 재는 지점을 코드에서 확인한다"* lessons 로 "추천 경로의 첫 이벤트는 `conditions` 이고 검색은 그 뒤"로 정정했는데, **#113 이 그 순서를 다시 바꿨다** — 자동 완화가 검색 후에 조건을 바꿀 수 있는 턴은 `conditions` 를 검색 뒤로 미루므로 그 턴에서는 검색 재시도가 first-token 예산을 실제로 쓴다(넘기면 504). 그래서 한쪽만이 아니라 둘 다 본다. 재시도가 실제로 갉아먹는 것은 턴 전체 시간이다. `llm_timeout_s × (llm_max_retries + 1)` 과 같은 결의 예산식이라 한쪽만 튜닝하면 조용히 어긋나는 쌍이다. **재시도 상한도 1로 묶었다** — backoff 가 구현에 없어 2 이상은 herd 증폭을 아무 방어 없이 여는 설정이라 현재 구현이 감당하는 값만 받는다(PR #235 리뷰). **비멱등 호출(I-2 담기 등)에는 재시도를 걸지 않는다** — 중복 담기 위험. fan-out(니즈별 분할 검색)은 leg 가 병렬이라 최악 지연이 합산이 아니라 6s 로 유지된다.
  - **재시도 로그가 응답 중단을 오분류하던 것도 고쳤다**(PR #235 리뷰) — `RemoteProtocolError` 는 `NetworkError` 의 형제라 분류 함수에서 빠지면 `malformed_response`(스키마 불일치)로 찍힌다. 재시도는 제대로 되는데 로그만 거짓말해, 운영자가 Spring 응답 계약을 의심하며 없는 문제를 찾게 되는 상태였다. **같은 구멍이 trace span 쪽에도 있었다** — `_spring_span` 은 이 예외를 분류하지 못해 `statusClass` 를 아예 안 붙였고, 그러면 trace 에서 "실패했는데 원인 미분류"와 "기록 자체가 없음"이 구분되지 않는다. 로그와 trace 가 같은 실패를 같은 말로 부르도록 둘 다 `connection_error` 로 맞췄다(#141 유계 라벨 규약). span 쪽 구멍은 이 PR 이 만든 것이 아니라 원래 있던 것인데, 재시도 대상으로 삼은 실패가 하필 trace 에서 가장 안 보이는 상태였다.
  (api-spec §2.9 c, v0.19.1 — BE 관측 포인트: 한 턴에 같은 검색이 최대 2번 온다 / `docs/specs/SPEC-RECOMMEND-001.md` v0.13.1 AC-REC-13c)
- **#228 — lifespan 정리 전체 시간이 배포 종료 유예를 넘을 수 있던 문제** — `_close_owned_resources()`가 자원별 `lifespan_resource_close_timeout_s`만 적용해 9개 자원이 차례로 느리면 총 정리 시간이 각 상한의 합까지 늘어날 수 있었다. 이제 독립 튜너블 `lifespan_cleanup_budget_s`(기본 8초)로 전체 deadline을 만들고, 각 자원은 `min(cap, max(남은 예산 - 남은 자원 floor 예약분, 0))`만 기다린다. 균등분배는 첫 자원을 8/9초로 제한해 정상적으로 느린 close도 자르는 위치 의존 문제가 있어 폐기했다. cap 기본값은 설치된 `psycopg_pool.AsyncConnectionPool.close()` 자체 기본과 같은 5초로 맞춰 정상 worker 종료를 바깥에서 더 일찍 자르지 않고, 새 `lifespan_resource_close_floor_s` 기본 0.2초가 앞의 느린 자원 위치와 무관하게 뒤의 각 close 시도 시간을 보호한다(첫 자원 allowance 6.4초 → cap 5초). cap·floor·예산 관계가 어긋나도 기동을 실패시키지 않고 cleanup 경고만 남기며, 음수 allowance는 0으로 clamp해 모든 close 본문을 계속 시작한다. 개별 cap과 예산 제약 로그는 실제 `allowance < cap` 여부에 따라 각각 `timed out`·`cleanup budget exhausted`로 구분되고 기존 `succeeded=N failed=M` 집계에 반영된다. 배포 유예를 앱 설정으로 복제하거나 값을 교차 검증하지 않는다. 대신 예산은 배포의 SIGTERM→SIGKILL 유예보다 작아야 하며, 현재 `deploy.yml`의 `docker stop`은 `--time` 없이 Docker 기본 10초에 의존한다는 운영 관계를 설정 주석에 기록했다. 기존 9개 역순, 실패 격리, 취소 지연 전파는 유지한다.
- **#221 — lifespan 이 앱 수명 동안 열린 pg 자원 7개를 종료 시 회수하지 않던 문제** — `_lifespan` 은 기존 `close_session_lifecycle()`·`close_advisory_pool()` 2개만 닫고, 요청 때 지연 초기화되는 7개 자원(`profile/store`·`profile/session_activity`·`profile/processed_events`·`core/conversation`·`core/pg_store`의 풀/store ctx, `seller/history`의 `AsyncPostgresStore` ctx, `seller/checkpoint`의 `AsyncPostgresSaver` 단일 연결 ctx)은 앱 수명 동안 붙든 채 `close()` 없이 버렸다. PR #218이 앞의 5개에 자기 이벤트 루프용 짝 API를 만들었지만, Claude 리뷰가 두 seller 모듈도 같은 수명주기 결함이며 종료 API 자체가 없음을 지적했다. 이에 `history.close_store()`와 `checkpoint.close_checkpointer()`를 추가해 sync reset이 보류한 ctx를 `__aexit__()`로 닫고, stale `CancelledError`는 삼키되 현재 태스크의 실제 취소만 다시 전파한다. history는 store·ctx·save lock을, checkpoint는 saver·ctx·init lock과 등록된 graph reset hook을 함께 초기화한다. 두 새 API와 기존 5개 짝 API 모두 명시적 앱 종료에서 실제 ctx/pool 종료 실패를 경고 로그로 남기고 다시 던져 lifespan 성공/실패 집계가 7개 전부의 사실을 반영한다(일반 요청 진입에서 stale ctx를 비우는 기존 degrade 동작은 유지). 이제 `_close_owned_resources()`가 기존 2개를 포함한 총 9개를 의존성 역순으로 순차 종료하고, 리소스별 일반 예외를 격리해 하나가 실패해도 나머지를 계속 시도한 뒤 `succeeded=N failed=M` 집계를 남긴다. 종료 태스크의 `CancelledError`는 `uncancel()`로 정리 동안 소비하고 남은 자원을 모두 닫은 뒤 마지막에 다시 전파한다. 각 종료 콜백은 `lifespan_resource_close_timeout_s` 개별 상한으로 감싸 응답 없는 pg close 하나가 뒤 자원의 정리를 무기한 막지 못하게 한다. lifespan 호출 순서·일반 실패 격리·외부 취소 지연 전파·리소스별 타임아웃을 9개 기준으로 고정하고, 7개 짝 API의 실제 ctx 종료와 실패 노출, 초기화 전·반복 호출 안전성을 검증했다. 프로덕션 동작은 종료 경로에 한정되고 와이어 계약은 변하지 않는다.
- **#220 — 통합 테스트가 공유 pg-profile 잔재에 밀려 간헐 실패하던 문제** — `test_pg_session_context.py` 가 깨지는 테스트만 바꿔가며 간헐 실패했고, 형태는 언제나 자기 세션 claim 을 못 찾은 `StopIteration` → `RuntimeError: coroutine raised StopIteration` 이었다. 원인은 `claim_expired_contexts(idle, lease, batch)` 가 `chat_session_contexts` **전역**을 `last_activity_at` 오름차순으로 batch 만큼 claim 한다는 데 있다 — 여러 worktree 가 같은 pg-profile(5434)을 공유해 만료 잔재가 쌓이면(실측 133) 자기 행이 batch(100) **밖으로 밀려** 결과가 빈다. 종전 대비책이던 `docs/lessons.md` 2026-07-31 규칙 2("batch 를 넉넉히 주고 필터링")를 **정확히 따른 코드가 그대로 깨진 것**이라, batch 상수를 키우는 방향은 잔재량과의 경주일 뿐이라 폐기했다. 이제 테스트 헬퍼가 **페이지를 넘긴다** — claim 된 행은 lease 동안 후보에서 빠지므로 `_claim_own`/`_claim_own_many` 가 자기 행을 만날 때까지 반복 호출해 잔재량과 무관하게 결정적이다. 복수 세션은 단건 헬퍼 반복 호출이 아니라 한 번의 순회로 함께 모으고(첫 호출이 나머지도 claim 해 버린다), "내 행이 결과에 없다"는 부정 단언은 `_drain_claims` 로 끝까지 훑어 거짓 음성을 막는다. 덧붙여 유휴 1시간을 넘긴 `it-*` 잔재만 세션당 1회 정리해 단조 증가를 끊는다 — 접두만 보고 지우면 동시에 도는 다른 worktree 의 살아있는 행을 죽이므로 유휴 시간으로 가른다. 잔재 150건을 심은 A/B 각 8회: **수정 전 2/8 실패 → 수정 후 8/8 통과**(대상 스위트 67 passed, 3회 연속). 프로덕션 코드 변경 없음 — sweep 자체의 계약은 정상이고 결함은 테스트가 전역 결과에 의존한 데 있었다. (`docs/lessons.md` 2026-08-01 항목 추가 + 2026-07-31 규칙 2 폐기 표시)
- **PII 카나리아가 랜덤 UUID 를 전화번호로 오탐해 트레이스를 통째로 드롭하던 문제** — 단위 스위트가 8~15회에 1회, **매번 다른 테스트**에서 깨졌다. 공통점은 `trace dropped ... code=TELEMETRY_REDACTION_FAILED` 한 줄뿐이었다. 한국 휴대폰 카나리아 `(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)` 가 **랜덤 UUID 의 hex 숫자열**과 겹친 것 — 페이로드에서 카나리아 검사를 받는 랜덤 문자열은 사실상 `dotted_order`(`<timestamp><uuid4>`) 하나뿐인데(`id`·`trace_id` 는 UUID 객체라 str 검사 대상이 아니다) `...8a6a-0181980191ee` 같은 조각이 전화번호 모양이었다. 실측 오탐률 스팬당 1.7e-4 라 스팬 수를 곱하면 "가끔 아무 테스트나 하나 깨진다"가 된다. 16-hex 지문(`sessionFp`·`threadFp`)도 같은 클래스다. 이제 **숫자열 카나리아(휴대폰·주민번호)를 서버 생성 불투명 식별자 필드에서만 면제**한다 — `dotted_order`·`requestId`(`uuid4().hex`, 클라이언트 헤더 미수용)·`sessionFp`/`threadFp`(HMAC hexdigest) 네 키뿐이고, 값이 서버 생성임을 코드로 보일 수 있는 필드만 넣는다. 정규식 자체는 원래의 엄격한 경계를 유지하며, 토큰·API 키·이메일 카나리아는 **어떤 필드에서도 끄지 않는다**. (초안은 정규식 경계를 hex 로 넓히는 방식이었으나 PR #218 리뷰가 회피 경로를 지적해 철회했다 — `userid01012345678` 처럼 hex 로 끝나는 흔한 단어 뒤에 붙은 진짜 PII 가 **모든 문자열에서** 탐지를 피한다. 그 회피 케이스는 회귀 테스트로 고정했다.) 오탐률 30만 표본 0건(수정 전 1.7e-4), 단위 스위트 20회 연속 통과. **진단은 이미 `dev` 에 기록돼 있었고**(`docs/lessons.md` 「무작위 UUID 가 개인정보 카나리 정규식에 걸려…」, 실측 0.368%/UUID) 코드 수정만 빠져 있었다 — 이 PR 이 그 수정이다. 그 항목이 제안한 두 대안(검사 대상 한정 / 필드 마스킹)을 쓰지 않은 이유는 lessons 에 적었다.
- **#208 — 통합 테스트가 간헐적으로 무한 대기하던 문제(이벤트 루프 teardown 교착)** — `uv run pytest -m integration` 이 실패도 오류도 없이 매달렸다(재현율 약 1/7). 원인은 두 겹이다. (1) psycopg_pool 의 async 빌드는 `CLIENT_EXCEPTIONS = (Exception, asyncio.CancelledError)` 라 `AsyncConnectionPool.worker()` 가 유지보수 태스크 실행 중 받은 취소를 **삼키고** 큐 대기로 되돌아간다 — 그 워커는 불사가 되고, 태스크를 한 번만 취소하고 무기한 기다리는 `asyncio.runners._cancel_all_tasks()` 는 영원히 반환하지 않는다. (2) pg 모듈 6곳이 sync 리셋터에서 await 할 수 없다는 이유로 풀 close 를 "다음 async 진입"으로 미뤄, 매 테스트가 **살아 있는 풀**을 곧 파괴될 루프에 남겼다. 창을 만든 건 (2), 교착으로 바꾼 건 (1)이고, 워커가 큐에 park 중이면 정상 취소되므로 간헐적으로 보였다. 이제 `processed_events.close_pool()` · `session_activity.close_pool()` · `conversation.close_store()` · `pg_store.close_store()` · `profile/store.close_store()` 로 **자기 루프에서 닫는 짝 API** 를 제공하고, 테스트 하니스가 매 테스트 teardown 에서 이들(+ 기존 `close_session_lifecycle()`·`close_advisory_pool()`)을 호출해 창 자체를 없앤다. 정리 대상은 **이 루프에 묶인 풀**로 한정한다 — `TestClient` 의 portal 루프에서 열린 풀은 이미 죽은 루프 소속이라 여기서 닫을 수 없고(닫으려 하면 미회수 코루틴 경고만 남는다) 이 루프의 teardown 을 막지도 못한다. 재현 조합 20회·전체 통합 스위트 3회 연속 통과(수정 전 8회 중 1회 hang). 와이어 계약 변경 없음. (`docs/lessons.md` 2026-07-31 항목에 진단 절차 — 특히 "관측 코드가 타이밍을 바꾸면 재현이 사라진다" — 를 기록)

### Docs
- **#278 — §4.6 `categoryName` 해석을 정본 2026-08-03 개정으로 동기화** — 잎 이름 정확 일치와 상위 개념 처리 방식을 명확히 했다. (api-spec §4.6, v0.20.1)
- **#159 — item-based CF 도입 판단 조사** — `review` 126,313건도 `member_id`가 전량 `NULL`이고 order/cart/wishlist/실사용 event가 0이라 사용자×상품 행렬은 희소한 것이 아니라 구성 불가임을 확인했다. 7,220개 전량 임베딩 기반 콘텐츠 유사도는 이미 HNSW로 서빙되므로 구현은 `no-go`, 상품 귀속 행동 로그와 반복 item pair가 관측될 때 재검토한다. `author_name`은 마스킹 충돌이 실측돼 대리 사용자로 쓸 수 없고, 사용자 식별을 요구하지 않는 session-based 계열을 재검토 1순위로 등재했다.
- **#160 — Learning-to-Rank 도입 판단 조사** — 추천 목록·순위 snapshot으로 true impression negative를 만들 구조와 현 scoring 6성분 feature는 있으나, 추천 유래 click 필드 확인·상품 귀속 conversion·추천 시점 feature snapshot이 없어 production 도입은 `조건부`로 보류했다. 누출 없는 snapshot과 30일 실사용 label 조건 뒤 pointwise offline arm부터 기존 ablation 규약으로 비교한다. 행동 로그 없이 LLM teacher로 랭킹을 학습하는 대안 경로는 별도 절로 정리하고 후속 이슈로 분리한다.
- **#161 — contextual bandit·RL 도입 판단 조사** — 실사용 `behavior_events`·회원·주문이 0이고 결정론 정책의 후보별 propensity 저장 필드도 없어 off-policy 평가와 장기 conversion reward가 모두 성립하지 않음을 확인했다. #160 완료, propensity 100% 기록, 상품 귀속 전환, 28일 10만 노출 전까지 `no-go`이며 hard filter 안 제한 탐색과 가중치 0 롤백만 허용한다.
- **#275 — LLM teacher 기반 랭킹 학습 도입 판단 조사** — teacher(`pipeline`)로 현행 6성분 결정론 student(`scoring`)를 distillation하는 경로를 실측했다. student(0.616852)는 dev search fixture 순서를 그대로 둔 no-op(=`passthrough`, 0.738210)보다 유의하게 낮고(−0.121358, 95% CI [0.039814, 0.206934]), 축퇴를 배제한 오라클 상한(0.738208)도 그 no-op을 넘지 못해 문제가 라벨이 아니라 6성분 선형결합의 용량임을 확인했다. `teacher − no-op` 델타(+0.044734, 95% CI [−0.122337, 0.231914])는 `inconclusive`라 이 계측기로는 teacher가 임의 순서보다 낫다는 것조차 아직 확립되지 않는다. 합성 transfer set(E3, 12질의·72콜, $0.073069)은 만들 수 있지만 MAUVE·C2ST가 요구하는 실사용 발화 표본이 `conversation_turns` 실측상 distinct 25종·7명뿐이라 외적 타당도는 지금 검증할 수 없다. 판정은 `no-go`(현행 student 형태 한정, LLM teacher·#146 production 결정 자체는 불변)이며, 측정 하네스(E1~E4)는 `evals/**` 수정 금지 레인 제약으로 `docs/research/research-275-harness/`에 커밋했다.
- **#244 — #138 후속 문서 정합** — 운영자가 역할별 설정 키를 바로 찾도록 구매자 30s·판매자 90s 스트림 전체 상한을 정본 기준표에서 분리하고, #151 baseline의 I-21 degrade가 BE·앱 문제가 아니라 비 UUID `sessionId` fixture의 §4.2 계약 위반이었다는 오귀속을 바로잡았다. (api-spec §2.9, v0.19.1)
- **#148 — 홈 추천 계약(I-22 · P-5)을 사본 api-spec에 등재** (api-spec §1.2·§2.3·§3.7·§4.11·C-18, v0.18.0). 정본(Notion「📡 API 명세서」) 2026-07-28 확정본이 사본에 **통째로 없던** drift 해소다 — 착수 전 `I-22`·`catalogVersion`·`recommendations/home`·`products/recommended` 검색이 전부 0건이었다. 구현은 #148, 재사용할 scoring baseline은 #145다.
  - **§3.7 I-22 `POST {AI_SERVER}/internal/recommendations/home` 신설** — Spring → AI 위임 호출, `X-Internal-Token`, **연결 2s/응답 3s**(채팅 90s와 무관, 메인 렌더 블로킹 방지). **왕복 1회로 끝난다** — Spring이 호출 주체라 응답 본문에 목록이 실려 오고 **I-21 콜백을 타지 않는다**. `limit`은 최종 **노출 목표치**라 AI는 품절 드롭에 대비해 넉넉히 반환하고 Spring이 자르며, `recentPurchasedProductIds`는 **가중치가 아니라 제외 필터**다. `items` **배열 순서가 곧 순위**(`position` 없음), `listId`는 AI 생성 **≥128bit 무작위**(I-21 §4.2와 동일 규칙). `sessionId`가 없어 §2.6 세션/방 축과 §2.9 동시 스트림 락이 적용되지 않고, **멱등이 아니라** 재시도 시 새 `recommendationRequestId`·`listId`가 나간다.
  - **`outcome` 3종과 cold start 규약** — `PERSONALIZED`/`NO_PROFILE`/`INSUFFICIENT_CANDIDATES`를 **전부 200**으로 답하고 **fallback 판단은 Spring이** 한다. 다만 이슈 본문의 *"FastAPI가 4xx/5xx를 내지 않는다"* 는 **cold start에만 걸리는 서술**이라 범위를 명확히 했다 — 정본에는 입력·인프라 실패용 4종(`BAD_REQUEST`·`INTERNAL_TOKEN_INVALID`·`UPSTREAM_UNAVAILABLE`·`UPSTREAM_TIMEOUT`)이 실재하며, 그 둘을 뭉뚱그리면 잘못된 입력까지 200으로 삼키는 구현이 나온다.
  - **provenance 비노출 [HARD]** — 프로필 원문·prompt·모델 식별자를 응답·로그·trace에 싣지 않고 알고리즘·모델 버전은 AI 자체 테이블 보관(평가 산출물 전용)이라, **`algorithmVersion`을 응답에 넣는 구현은 계약 위반**이다(이슈 초안의 "응답에 싣는다"를 정본이 뒤집었다).
  - **§4.11 P-5 `GET /api/products/recommended` 신설**(레인 d, FE↔Spring) — Spring이 이를 서빙하려 I-22를 호출하는 **서브 관계**라 등재해야 의존이 드러난다. fallback 시 상관키는 **Spring이 발급**(AI가 대신 만들지 않는다), `fallbackReason`·`cacheStatus`·`algorithmVersion`·`modelVersion`은 와이어 비노출. 캐시 TTL **10분** · `listId` 귀속 유효기간 **24시간** 확정(2026-07-30) — 홈은 조회 API가 없어 CH-5의 *조회* 만료와 성격이 다르다. 정본 I-22 페이지에 남은 *"TTL은 BE 결정 필요"* 는 stale이며 BE 통보 대상.
  - **레인 (b) 재정의** (§1.2·§2.3 b) — 더 이상 "이벤트 채널"만이 아니다. I-22는 통지가 아닌 **동기 요청/응답**이라 §2.7 `/events/*` 멱등 규약이 적용되지 않는다. 제목 변경에 걸린 `tests/unit/test_contract_docs.py` 앵커도 함께 갱신했다.
  - **🔴 C-18 신설 — `catalogVersion` 값 생성 주체 미해결.** 정본은 Spring이 실어 보내게 규정하는데 랭킹은 AI 자체 인덱스(I-17 임베딩)로 매긴다 — **Spring은 그 버전을 알 수 없어**, 그 값으로 캐시를 키잉하면 AI 인덱스가 갱신돼도 P-5 캐시가 무효화되지 않는다. **정본 개정 + BE 합의가 #148 착수 전 필수**다.
- **#209 — I-21 추천 콜백을 다중 목록(`lists[]`)으로 정합** (api-spec §4.2·§3.3·§5.1 C-9, v0.17.1). 정본(Notion 「📡 API 명세서」 I-21) 2026-07-28~30 개정이 사본에 반영되지 않아 **사본이 자기모순** 상태였다 — §3.1은 v0.15.26에서 이미 `products.ready.listIds` 배열로 전환하며 *"I-21이 `lists`를 1~10개 보내므로(§4.2)"* 라고 §4.2를 인용하는데, 정작 §4.2 본문에는 `lists`가 없고 단일 `listId` + `productIds[Top5]` 구 형식이 남아 있었다.
  - **요청 최상위를 `lists[]` 배열로** — 구 평평한 3필드(`listId`·`productIds`·`reasons`)는 폐기. 목록이 1개여도 길이 1 배열이다. 니즈별 추천(파우치·어댑터 각각의 후보)과 세트 여러 안(조합 A·B·C)은 목록 하나로 표현되지 않는다.
  - **`listType`(`PICK_ONE`/`BUY_ALL`, 항상 전송)** — 목록 안 상품들이 대체재인지 보완재인지. 세 모양이 이 한 필드로 표현된다(`PICK_ONE`+1=일반, `PICK_ONE`+N=니즈별, `BUY_ALL`+N=세트 복수안). **판단 기준은 예산이 아니다** — "감자탕 재료"는 예산이 없어도 `BUY_ALL`, "5만원으로 파우치"는 예산이 있어도 `PICK_ONE`. 목록 개수는 `lists` 길이로 알 수 있어 싣지 않지만 `listType`은 개수로 복원할 수 없다.
  - **`recommendationRequestId`** — 추천 실행 1회를 가리키는 opaque id(FastAPI 생성). 노출·클릭·담기·주문을 그 추천에 귀속시키는 조인 키로 `listId`와 **역할이 달라 서로 대체하지 않는다**(#140의 상관키와 같은 대상). **멱등 키는 (`recommendationRequestId`, `listId`) 쌍** — 단독으로 쓰면 한 실행의 두 번째 이후 목록이 중복으로 잘못 버려진다.
  - **`totalBudget`·`lists[].label`** — `BUY_ALL`일 때의 예산 상한과 목록 이름. 이 셋은 표시 필드가 아니라 **목록 성격 메타**이며 표시 권위는 그대로 Spring에 있다(경로 B 유지) — `products.ready`는 싣지 않고 CH-5가 나른다.
  - **상한·오류 조건 등재** — 목록당 상품 Top5 → **9개**(2026-07-30 확정), `lists` 1~10개, `reasons` ≤9·`reason` ≤200자, `listId` 허용 문자(영숫자·`-`·`_` ≤64자, Redis 키 오염 방지), `label` ≤50자. "실패가 아닌 것"(멱등 재전송·만료 `sessionId`의 익명 저장·`HIDDEN`/품절의 CH-5 시점 드롭)도 함께 옮겼다.
  - **`listId` TTL 10분 확정** — 🔴 C-9 잔여가 해소됐다. 세션이 sliding으로 연장돼도 목록 TTL은 생성 시점 고정이며, 만료 시 CH-5는 404이고 FE는 카드 스냅샷으로 폴백한다.
  - **jarvis-back은 이미 신 형식 구현 완료** — `RecommendationCallbackRequest.resolvedLists()`가 구 형식을 목록 1건으로 접어 과도기 수용 중이라 런타임 장애는 없으나, 그 코드는 *"FastAPI가 전환하기 전까지만"* 존재한다. **코드 전환(`RecommendationPush`)이 후속**이며 #60(세트 복수화)·#140(상관키)·#163(총액 예산)의 공통 토대다.
- **`sessionId`(접속) · `threadId`(방) 축 분리 — MVP의 `sessionId == threadId` 전제 폐기** (api-spec §2.6·§2.9·§3.1·§3.2·§3.5·§6.3, v0.16.0). 정본 SPEC-CHAT-SESSION(Option B)을 사본에 반영했다. 한 접속 아래 여러 방이 **동시에** 존재하는 멀티탭 대화가 목적이며, AI 구현은 #186에서 정합했다.
  - **§2.6 식별자 모델 신설** — 축별 발급 주체·수명·담당 상태를 표로 확정. `sessionId`=Spring CH-1 발급(Redis TTL 10분 sliding)·프로필 세션버퍼·I-20·`conversation_turns.conversation_id`(primary) / `threadId`=**FE 생성**(서버 왕복 없음)·필터 누적·장바구니 pending·되돌리기·동시 스트림 락·`conversation_turns.thread_id`. 구 정의 *"만료 의미 없는 불투명 스레드 키"* 를 폐기 — "스레드 키"는 이제 `threadId`의 것이고, AI가 만료를 판정하지 않는 이유는 만료가 **없어서**가 아니라 **판정 주체가 Spring이라서**다.
  - **동시 스트림 락을 세션→방 단위로** (§2.9 a·§2.5 오류표) — `409 STREAM_IN_PROGRESS`의 판정 키를 `threadId`로 변경했다. 같은 `sessionId`의 다른 방은 동시에 스트리밍할 수 있고 동일 방의 중복 스트림만 409로 거절한다.
  - **I-20 사유를 `logout` 1종으로** (§3.5·C-8) — 새 대화가 CH-1을 부르지 않고 `threadId`만 갱신하게 되어 `newConversation`이 발화되지 않는다. Spring이 I-20을 쏘는 경우는 로그아웃뿐이고 나머지는 Redis TTL 만료 + AI 내부 비활동 sweep이 담당한다.
  - **[D5] CH-1 멱등 등재 + 구 "CH-1 재호출 = 새 세션(맥락 단절)" 경고 폐기** (§1.2 레인 d) — Spring이 Redis `SETNX`로 기존 세션을 그대로 반환하므로 CH-1을 몇 번 불러도 세션은 하나다. **정확성은 `SETNX`가 책임지고 FE Web Locks(D1)는 최적화**다(한 브라우저 안에서만 통해 폰·PC 동시 접속을 못 막는다). 축출을 없앤 뒤엔 밀린 세션이 CH-1b로 TTL을 연장하며 유령으로 남아 I-20이 안 나가는데 이를 `SETNX`가 막는다. **예외 = 게스트 첫 방문 멀티탭**(쿠키 부재 → 게스트 2명 → 밀린 탭이 CH-1b `403`)은 신원이 갈라지는 것이라 `SETNX`로 못 막아 Web Locks가 방어한다.
  - **[D6] 맥락 TTL을 방→접속 단위로** (§2.6) — 어느 방에서든 활동이 있으면 그 `sessionId`의 **모든 방** TTL을 함께 연장하고 세션 종료 시 일괄 정리한다. 방마다 생사가 갈리면 탭을 옮겼을 때 한쪽 맥락만 사라져 사용자가 이해할 수 없다. 실제 코드는 thread 축 스토어(필터·cart·revert)에 **TTL이 아예 없어** 정정이 아니라 신규 구현이고, `session → thread[]` 역인덱스가 없어 "그 세션의 모든 방"을 지목할 수단부터 만들어야 한다.
  - **§6.3 저장·로그 축 정합** — checkpointer thread 키를 `sessionId`→**`threadId`** 로 정정하고 `conversation_turns`에 nullable `thread_id`와 조회 인덱스를 추가했다. 기존 볼륨은 런타임 멱등 migration으로 보강하고 신규 턴부터 방 식별자를 저장한다. 구조화 로그에도 **`threadId`** 를 남겨 한 `conversationId` 아래 여러 방을 구분한다.
  - **🔴 BE 확인 대기** — `SETNX` 멱등 키 스코프(`sub` vs `sub_type`+`sub`)와 멱등 반환 시 세션 TTL sliding 갱신 여부. 정본 §5 D5는 "이 사용자"로만 적었다.
  - 이미 정합해 손대지 않은 것: `conversation_turns.conversation_id = sessionId`(D2 session-primary) · thread 축 스토어가 `thread_id` 키 · 프로필 세션버퍼가 `session_id` 키 · 주기 flush가 `AsyncIOScheduler`+lifespan(D4) · 스트림 티켓에 session/thread 클레임 없음 · 탭 닫기 종료 신호 없음.
- **CH-2 계약 확장 — 요청 2필드 신설 + SSE 2필드 추가** (api-spec §3.1·§3.2, v0.15.26). 정본(Notion「📡 API 명세서」CH-2·S-4) 2026-07-28~30 개정을 사본에 반영했다. 요청 필드 `conditionActions`·`screen` 구현은 #84·#118에서 진행하고, 응답 계약 `listIds`·`requestId`·`retryable`은 #189에서 구현했다.
  - **`conditionActions`** (구매자 전용) — 조건 칩 제거를 `[{op:"remove", field}]` 구조화 배열로 받는다. 구 규약 문자열(`"[조건 제거] priceMax"`) 왕복은 **폐기**(#84). FE는 구 방식으로 구현돼 있으나 AI에 수신부가 없어 **현재 칩 제거가 무동작**이다.
  - **`screen`** (구매자·판매자 공용) — `{pageType, filters?, products?, columns?}`. `pageType`은 라우트가 아니라 **우측 패널 내용**을 가리킨다(채팅이 전용 페이지에만 있어 라우트를 실으면 정보가 0). `products`는 **서버가 모르는 목록만** 싣는다(P-4 인기상품·판매자 자사 상품) — 추천 카드는 `listId`로 서버가 알고, 되돌려주면 위조 경로가 된다. `columns`는 반응형 그리드 열 수로 "3번째 줄 2번째" 좌표 지시를 푼다(`rows`·항목별 좌표는 파생값이라 제외). 07-17 FE 제안(`ChatScreenContext`)과 #118의 "노출 상품 목록" 요구를 한 필드로 통합했고, 담기 가드의 "두 목록 밖 id 차단"은 유지한다.
  - **`conditions` 칩 `field` 6종 확정** — `category`·`priceMax`·`priceMin`·`brand`·`ratingMin`·`keyword`. `conditionActions.field` 검증의 전제인데 종전엔 예시 둘뿐이라 허용 집합이 계약에 없었다(코드 `build_condition_chips` 실측과 일치).
  - **`pageType` 어휘를 E-1과 공유** — 화면 이름을 새로 만들지 않는다. 기존 8종은 구매자 화면만 커버해 **14종으로 확장**(구매자 10 · 판매자 4). 정본은 E-1.
  - **`filters` 키를 `status`로 통일** — FE는 URL에서 주문 `?status=` / 상품 `?tab=`으로 다르게 쓰지만 `screen.filters`에는 둘 다 `status`. 어느 상태인지는 `pageType`이 말해주고 `tab`은 UI 용어다. 값은 enum 코드가 아니라 **화면에 보이는 한글 표시값**.
  - **in-stream `error`에 `requestId`·`retryable`** — 스트림 전 실패(§2.5 봉투)에만 있던 추적 id를 스트림 내부 실패에도 싣는다. `retryable`은 `code`로 복원 불가(같은 `LLM_UNAVAILABLE`이 "미구성"·"일시 불가"에 겸용)라 emit 지점이 정한다. §3.2 판매자도 동일(`ErrorData` 공용).
  - **`products.ready`를 `listIds` 배열로** — I-21이 `lists`를 1~10개 보내는데 사본·CH-2만 단일 `listId`로 남아 세트형·니즈별 추천을 나를 수 없었다. 목록이 1개여도 길이 1 배열로 보내 FE 분기를 없앤다.
- **정본 대조로 사본 drift 3건 정정** (api-spec v0.15.27). (1) **담기 이벤트 적재 주체** — §4.1 I-2와 §5.1 Q9의 *"`CART_ADD(via: chat)`는 BE가 적재"* 를 폐기했다. E-1 정본에서 `add_to_cart`는 **FE가 쏘는 12종 중 하나**이고 서버 직접 적재는 `recommendation_generated` 하나뿐이다. (2) **`budget` 이벤트 제외** — 정본이 "미구현 → 명세에서 제외"로 정리했는데 사본은 스키마와 이벤트 순서 계약에 그대로 두고 있었다(#163). (3) **`search.query` PII 기준** — 정본 E-1이 "개인정보 금지"와 "`search` 필수 = `query`"를 동시에 말해 자기모순이었고, FE가 그 금지 조항을 근거로 `queryLength`만 보내 `searchTopics` 워커가 돌 수 없었다. 금지 대상은 FE가 굳이 끌어다 넣는 이름·주소·연락처·이메일이며, 사용자가 직접 입력해 이미 서버로 보낸 검색어는 원문을 싣고 **보존기간으로 관리**한다.
- **공통 헤더 규약 페이지 신설** (api-spec §2.5). `X-Request-Id`·`traceparent`는 전 API 공통이라 엔드포인트 행 단위인 정본 DB에 놓을 자리가 없었다 — Notion「프로젝트 자료실」에 규약 페이지를 만들고 사본 §2.5에 AI 소관 요약을 넣었다(#141·#134·#151). 실측으로 드러난 것: inbound `X-Request-Id` **수용 미구현**(`request_context_middleware`가 `new_request_id()`를 조건 없이 호출) · Spring 역호출 **전파 미구현**(`X-Internal-Token` 하나만) · 응답 echo는 구현됨 · `traceparent`는 코드베이스에 없음. 그래서 지금은 FE→Spring→FastAPI 로그를 같은 키로 이을 수 없다.
- **`docs/lessons.md` 2건** — 열거형 어휘의 개수를 머릿셈으로 적어 표(14개)와 본문("13종")이 어긋난 채 5곳에 퍼진 건 · `git fetch` 실패를 `2>/dev/null`로 삼켜 18커밋 뒤처진 트리로 오진한 건(같은 규칙을 적어두고 반복 — 규칙에 "검증"이 빠지면 안 지켜진다).

### Added

- **#32 — 골든셋 실측으로 방식2(`EmbeddingRerankBackend`)를 검색 기본 백엔드로 확정
  (백엔드 전환 없음)** — dev `search` 26건·라이브 pg-catalog 7,220건에서 방식1(정확
  코사인/HNSW)의 mean recall@5/@10/@20은 **0.6026/0.7987/0.8449**, 방식2는
  **0.7872/0.9205/1.0000**이었고, overlap@10은 **0.4269**, 방식1 승리는 **0/26**,
  HNSW와 정확 코사인의 상위 10은 **1.00으로 동일**했다. 방식1은 가격 하한·부정어 같은
  구조적 제약을 임베딩 단독으로 걸지 못했다. 다만 라벨이 Spring I-1 후보에서 유래해
  26건 모두 정답이 Spring 후보 안에 있으므로 방식2의 상한이 구조적으로 1.0인 편향이 있고,
  결론은 “방식1이 방식2를 못 이긴다”까지만 유효하다. `search_backend` 기본값은 이미
  `embedding_rerank`라 전환하지 않으며, C-17 방식1 라이브 hydrate는 미해소로 남기고 운영
  롤백은 config 토글 `SEARCH_BACKEND=spring`을 쓴다. 실제 fixture 후보를 기존 비교 API에
  연결하는 재현 하네스와 라이브 회귀 테스트도 함께 고정했다.
- **#171 — I-1 `reviewCount` 수신 + rating=0 의미 판별** — BE 합의(2026-07-28)로 I-1이 `reviewCount`(조회 시 집계 리뷰수)를 AI 계산용(비표시)으로 함께 반환한다. `SpringProduct.review_count`를 추가하고, `rating`과 짝지어 **"리뷰가 아예 없어 rating=0"(reviewCount==0, 데이터 부재)** 와 **"리뷰가 있고 하한 미달"(reviewCount>0)** 를 구분한다. ① `search_catalog`의 `rating_min` 사후필터는 reviewCount==0을 (rating=None 무평점과 동일하게) 보존하고 실제 리뷰가 있는 미달만 탈락시킨다. ② `rerank`는 reviewCount==0 후보의 rating을 None으로 중립화(저평점 오인 방지)하고 reviewCount를 신뢰 신호로 함께 전달한다. reviewCount가 None(BE 미전송)이면 rating이 지배하는 구 동작으로 폴백한다. **#100의 "reviewCount는 표시 전용·I-1 미반환" 결정을 부분 개정**. (api-spec §4.6, v0.15.25)
- **#101 PR② — attributes 유연 하드매칭** — 사용자가 명시한 상품 속성(소재·핏·용도·방수 등)을 `SpringProduct.attributes`와 관대 매칭해 하드 필터한다. `ProductSearchFilters.attr_conditions`(AI 내부, 와이어 제외)를 decompose가 추출하고, `search_catalog`가 문자열은 부분매칭·숫자는 완전일치로 비교한다. 조건 축이 없는 상품은 '반증 아님'으로 보존(#100 P0 rating 정책과 정합), 0건이면 축별 완화한다. 멀티턴은 merge(prior∪이번턴) 기본에 `attrRemovals` 명시 제거 신호로 처리 — LLM이 이전 축을 빠뜨려도 유실되지 않는다. 추측 선호(소프트)는 코드 없이 Sonnet 재랭킹이 판단. (api-spec §4.6·§4.8)
- **#100 P1 — I-1 `color` 검색 조건 연결** — Spring I-1이 `attributes` LIKE로 지원하는 `color` 필터를 AI가 쓰도록, `ProductSearchFilters.color`와 `_search_query_params`의 `color` 전송을 추가하고 decompose 프롬프트가 색상 조건("빨간"·"검정" 등)을 `filters.color`로 추출하게 했다. 그동안 요청 모델·쿼리 변환에 `color`가 없어 Spring의 색상 검색을 못 쓰던 것을 해소. (api-spec §4.6, v0.15.22)
- pg-catalog `products` 임베딩 프로비넌스 컬럼(`embed_model·embed_dim·embed_task·normalized`) + `embedding_meta_complete` CHECK, 기존 볼륨용 마이그레이션(#65).
- `embed_texts(task_type=...)` 및 비대칭 임베딩 바인딩(질의=RETRIEVAL_QUERY / 문서=RETRIEVAL_DOCUMENT)(#65).
- **이슈 #79 — AI 내부 프로필 inactivity timeout** — 회원 발화 저장과 같은 pg-profile
  transaction에서 세션별 `last_activity_at`을 DB 시각으로 갱신하고, 10분 비활동 세션을 1분
  주기의 bounded `FOR UPDATE SKIP LOCKED` sweep으로 선점한다. Spring I-20(`logout`·
  `newConversation`)과 timeout은 고정키 claim으로 직렬화되는 공통 finalizer를 사용한다. Spring
  종료만 멱등키를 영구 완료하고, idle 처리는 재개 가능한 checkpoint로 claim을 해제하여 같은
  sessionId의 후속 발화를 다시 flush한다. 새 회원 발화는 같은 DB transaction에서 이전
  `PROCESSING`/`COMPLETED` 종료 generation을 무효화하고, terminal finalizer는 처리 중 갱신된
  activity를 `COMPLETED`로 덮지 않는다. scheduler는 라이브 스트림 슬롯을 점유하지 않으며,
  처리 동시성 상한, 전체 batch wave를 포괄하는 claim TTL 검증, claim lease/crash 복구,
  claim별 오류 격리, activity 완료 실패의 retryable 집계, LLM 실패 시 버퍼 보존을 포함한다.
  conversation/activity 양쪽 schema 초기화는 동일 advisory lock을 사용해 콜드스타트 DDL 경합을 막는다.
  I-20 입력 파생 단계의 내부 예외도 `retryable`/202로 강등해 best-effort 응답 계약을 유지한다.
  `tabClose` 신호나 추가 HTTP API는 도입하지 않았다. (api-spec §3.5, v0.15.19;
  SPEC-PROFILE-001 v0.4.0)

### Fixed
- **#119 — 취향 프로필이 추천에 과반영/왜곡 (회원이 게스트보다 부정확)** — 프로필 마크다운을 `decompose` 프롬프트에 발화와 같은 격으로 주입하면서 사용 규칙을 한 줄도 주지 않아, LLM 이 "3~5만원대 선호"를 `priceMax` 같은 **하드필터**로 승격시켰다. 라이브 실측(발화 3종 × 3회)에서 **9턴 중 9턴** 유출: 조건 없는 발화는 게스트 `filters={}` vs 회원 `{price_min:30000, price_max:50000, brand:[소니,젠하이저], rating_min:4.5, color:검정}` 로 구조화 블록이 통째로 WHERE 절이 됐고, `"10만원대 헤드폰"` 은 게스트 `{price_min:100000}` vs 회원 `{price_min:10000, price_max:100000}` 로 **사용자가 말한 가격대까지 프로필이 덮었다**(REQ-REC-043 명시 제약 무단 위반). `"노트북 추천"` 에도 이어폰 취향(3~5만원·소니·검정)이 그대로 붙어 사실상 0건 조건이 됐다. 그 필터는 스레드 필터 저장소에 영속돼 다음 턴 `PRIOR_FILTERS` 로 재주입되며 **세션 내내 후보를 좁혔다**(래칫) — 게스트는 이 입력이 없어 손실이 0이라 개인화가 순손실이 되는 비대칭이었다. 이제 **개인화는 후보를 줄이는 데 쓰지 않고 순서에만 반영한다**: `profile_injection_scope`(기본 `rerank_only`)로 decompose 주입을 끊어 **회원과 게스트의 decompose 프롬프트가 바이트 단위로 동일**해졌고(회귀 테스트로 고정 — 회원 recall 이 게스트보다 작을 수 없음을 LLM 품질 측정 없이 증명), rerank 에는 프로필이 있는 턴에만 "QUERY 를 만족하는 후보들 사이의 **동점 처리**에만 쓰라"는 지시를 user 메시지에 덧붙인다(`profile_rerank_influence`) — `_SYSTEM` 과 프로필 없는 경로의 프롬프트는 한 글자도 바뀌지 않는다. 반복 발화 왜곡은 세션 버퍼에서 끊었다: 버퍼는 델타 추출 LLM 에 통째로 실리고 LLM 이 그 중복을 보고 `repetitionEma` 를 산출하므로 **반복 횟수가 곧 취향 강도**였다 — 정규화 동일 발화를 `profile_buffer_repeat_cap`(기본 2, 최솟값 2)개까지만 적재해 **증폭만 자르고**, 취향 신호가 없는 intent(주문조회·장바구니 조회)는 적재에서 제외한다. 상한을 1로 낮추지 않는 이유는 승격 게이트가 `salience AND (explicit OR repeated)` 라 반복이 명시 표명 없이 승격시키는 **독립 경로**이고, 세션 간 반복 누적(`GateState`)이 미구현이라 다음 세션이 대신 살려주지 않기 때문이다(`profile_buffer_excluded_intents`, 적재를 intent 판정 뒤로 이동). 관측을 위해 `decompose_case` 에 `filters_set`(축 이름만 — 값은 PII 라 싣지 않는다)·`profile_injected`, `recommend_pipeline` 에 `profile_present`·`profile_scope` 를 추가했다. **연속 가중치(`*_weight`)는 두지 않았다** — 전략 A 의 rerank 는 점수가 아니라 순위 목록을 산출해 가중합할 스칼라가 없고, 취향-상품 적합도의 ground truth 가 없어(#142/#143 미구현) 계수가 마술 상수가 된다. 취향 임베딩 블렌딩은 #145 → #147 라인으로 유예. **와이어 계약 불변**(SSE 이벤트·필드·오류 코드·push 페이로드 무변경)이라 api-spec 개정 없음. (`docs/specs/SPEC-PROFILE-001.md` v0.6.0 — §5.1·REQ-PROF-011/014 조건부 유예 + REQ-PROF-026 신규 + OPEN-P12(GateState 미구현 gap) 등록 / `docs/specs/SPEC-RECOMMEND-001.md` v0.12.0 — REQ-REC-005-A 신규·REQ-REC-006 개정·REQ-REC-047/041 유예)
- **#115 — 카테고리 추출 정확도(첫 추측·임베딩 보정)** — "발화에서 카테고리가 될 때도 안 될 때도 하고, 같은 발화가 다른 카테고리로 간다"를 실측 진단(발화 27건 + LLM raw 라벨 16건, 거리·마진 정량)으로 원인 확정 후 4겹으로 고쳤다. **근원**: decompose 가 사전에 없는 카테고리 라벨을 매번 다르게 창작하고(`"전자제품>오디오>이어폰"` vs 사전 `"음향가전 > 블루투스 이어폰"` — 표기 체계가 달라 exact 히트 0, 오타 `'가전/생활용폼'` 포함), `map_categories` 가 그 창작 라벨을 **발화 유래 query 보다 우선하는 임베딩 앵커**로 썼다(`raws[i] or qtexts[i]`). 거리 가드도 없어 "동전 던지기" 매칭이 그대로 Spring 으로 나갔다. ① **앵커 query 우선**(§4.3·§4.3.1) — raw·query 둘 다 조회하되 query 히트가 있으면 query 채택, raw 는 폴백. 거리 비교로 고르지 않는 이유는 추상 라벨의 **가짜 근접**(`'주방용품'`→`주방용품 > 칼` 0.1387 은 의미가 아니라 문자열 겹침, 정작 `'냄비 세트'` 는 0.1941)이며 anchor=raw 채택 12건 중 11건이 오분류였다. ② **fan-out 전개 단위를 매장 코너 이름에서 구체 상품으로**(§6.0, 프롬프트) — `"부모님 환갑 선물"` → 홍삼·안마의자·한우 선물세트·영양제. 구체 상품 앵커는 실측 16/16 정답(거리 0.046~0.217)이고, leg query 는 수식어·발화 복사를 금지해 순수 상품명만 담는다(`'갓성비 무선 이어폰'` 0.2556 → `'무선 이어폰'` 0.1955). ③ **거리컷 도입**(§4, `category_distance_max=0.22`) — 최근접이 멀면 "맞는 칸이 taxonomy 에 없다"는 신호이므로 canonical 없이 드롭하고 무필터+`semanticQuery` 로 흡수한다(`"부모님 환갑 선물"`·`"조카 입학 선물"`·`"집들이 선물"`이 모두 `출산/돌기념품` 0.297~0.302 로 붕괴하던 것). 종전 never-null "멀어도 억지로 채택"을 폐기하는 정책 전환이라 설계 문서 개정을 선행했다. ④ **마진 트리거 top-k 택일**(§4.4) — 거리컷을 통과하지만 뜻이 틀리는 추상 라벨(`'선물용품'` 0.2074/마진 0.0095)용으로, 마진 ≤ `category_select_margin_max`(0.02) 인 leg 만 #59 예비 구현 `select_category` 를 호출한다. 드롭이 아니라 택일이라 `'양말'`(1·2위 둘 다 정답, 마진 0.0088) 오탐이 무해하며, null(맞는 후보 없음)은 드롭·LLM 실패는 top-1 유지로 분리한다. 관측 구멍도 함께 메웠다 — `search_categories_pg` 가 `<=>` 로 정렬해놓고 버리던 **거리를 반환**하고, 매핑 로그에 `distance·margin·anchor_kind` + 신규 이벤트(`category_distance_rejected`·`category_selected`·`category_select_null`·`category_select_unavailable`)를 싣는다. 라이브 재측정(실 LLM ×3회): `"층간소음 방지 용품"` 3/3 동일(종전 `전기생활용품`), `"갓성비 무선이어폰"` 3/3 `음향가전 > 블루투스 이어폰`(종전 `자동차기기 > 카오디오음향기기`), `"자취 시작할 때 필요한거"` 3/3 leg 5개 전부 정확(종전 `유아침구`), `"발 시려울 때 신을 수 있는거"` → `여성신발 > 부츠`(종전 `건강관리용품`). LLM 예산은 정상 경로 종전과 동일(2회)이고 애매한 leg 에만 조건부 +1~2회(상한 `category_select_max_calls`). `categoryName` 은 계약상 이미 `string|null`·선택이라 **api-spec 변경 없음**. #198(전개)이 병합된 뒤 ①전개 + ②거리컷·택일이 **함께 도는 경로**를 9발화 × 3회 재검증했다(§4.0.1) — 사용자가 보고한 오분류 **6/6 이 재현되지 않았고**, 대조군 `"청바지"` 는 3/3 완전 동일. 이때 정답 카테고리가 거리컷에 걸린 4건을 앵커까지 추적해 **임계가 아니라 앵커 품질이 원인**임을 확인했다 — 전개 LLM 이 실재하지 않는 상품명을 만들고 있다(`던킨 쿠션 매트`·`문틈 방음 댐`·`보아삭 양말`). 조어가 카탈로그 어휘와 멀어 거리가 뜨는 것은 정상 동작이고 임계를 올리면 조어가 엉뚱한 카테고리에 붙으므로, 처방은 프롬프트(앵커 품질)로 남겼다(설계 OPEN-2~4). 잔여 무필터 퇴화는 3/27 회차(11%)이며 원인은 `decompose` 산출 지터로, 거리컷은 이를 오분류 대신 무필터로 **안전하게 퇴화**시키는 설계 의도대로 동작했다. 앵커가 leg 당 2개가 된 데 맞춰 **pg-catalog 검색 풀을 10 → 20 으로 올리고 하한(`2 × category_fanout_max`)을 기동 시 강제**했다 — 종전 값은 한 턴이 풀 전체를 소진해 동시 요청 헤드룸이 0 이었고, 증상이 `PoolTimeout` 이라 원인이 드러나지 않는다(운영 env 에 값을 명시했다면 확인 필요). 택일 LLM 예산은 leg 인덱스가 아니라 **마진 오름차순**으로 배분해, 애매함의 판정 기준과 배분 기준이 어긋나던 것을 맞췄다. **거리컷이 정답을 버리던 도메인 편향도 보정했다**(§4.5) — 거리는 도메인 어휘에 오염된다(공산품은 상품명이 곧 leaf 이름이라 `청바지` 0.1224 지만, 식품은 이름이 달라 정답인 `돼지 등뼈`→`축산 > 돼지고기` 도 0.2661 로 드롭됐다). "맞는 칸이 taxonomy 에 없다"를 직접 재는 지표는 거리가 아니라 **마진**이라는 것이 실측(76 앵커)으로 드러나, `distance > 임계` 여도 `margin ≥ 0.035` 면 채택한다 — 차단 대상 최대 마진 0.0261 vs 회수 대상 0.034~0.085 로 분리되어 **회수 7건·오분류 유입 0건**. 라이브 재검증에서 `"감자탕 재료"` → 돼지등뼈·감자·대파·양파가 종전 전량 드롭에서 `축산 > 돼지고기`·`채소 > 감자/고구마/옥수수`·`채소 > 파/마늘/양념채소` 채택으로 바뀌었고, 목적 표현·조어는 그대로 차단됐다. 회수는 부분적이다 — `참기름`(마진 0.0105)처럼 1위가 정답이어도 마진이 얇으면 `사과`→`노트북 > 애플`(0.0039) 같은 오분류와 구분되지 않아 여전히 드롭된다. 시드 문서에 대표 상품명을 넣는 대안은 **질의어를 목록에서 빼면 효과가 사라져** 일반화되지 않음을 실측으로 확인해 폐기했다(§4.5). (DESIGN-CATEGORY-HYBRID-59 §4·§4.0.1·§4.3·§4.4·§6.0·§10·§11·§12·OPEN 개정)

### Changed

- **#32 — 방식1 라이브 전제 C-17(id 제약 조회) 기각** — 골든셋에서 방식1이 방식2를
  이긴 케이스가 0/26이었고, C-17은 가용성 hydrate만 가능하게 할 뿐 방식1의 핵심 실패인
  가격 하한·부정어 같은 구조적 제약을 고치지 못하므로 BE 요청을 철회한다. 와이어 계약은
  바뀌지 않으며 `VectorSearchBackend`는 오프라인 비교 전용으로 존치한다. BE에는 C-17을
  구현하지 않아도 된다는 철회를 통보해야 한다. (api-spec §4.6·§4.8, v0.19.4)
- **#186 — 접속(`sessionId`)·방(`threadId`) 축 분리 구현** — 구매자·판매자 스트림 레지스트리 키를 인증 신원+`threadId`로 전환해 같은 접속의 다른 방을 병렬 허용하고 동일 방만 `409 STREAM_IN_PROGRESS`로 차단한다. `conversation_turns`는 session-primary를 유지하면서 nullable `thread_id`를 fresh schema·기존 볼륨 migration·쓰기/조회 모델에 병기하고, 구조화 로그와 저장 실패 로그에 `threadId`를 추가했다. #189의 `requestId`/`listIds` SSE 계약과 함께 동작하도록 충돌을 통합했다. (api-spec §2.5·§2.9·§6.3, v0.16.0)

- **#189 — CH-2/S-4 SSE 응답 계약 정합화** — `products.ready`의 단일 `listId`를 항상 배열인 `listIds`(1~10개, 순서 보존)로 바꾸고, 현재 단일 I-21 push 결과도 길이 1 배열로 반환한다. 구매자·판매자·공통 스트림의 모든 `error`에 HTTP 응답·구조화 로그와 같은 `requestId`와 emit 지점이 판정한 `retryable`을 추가했다. provider 미구성은 재시도 불가, timeout·검색·일시적 내부 장애는 재시도 가능으로 분류한다. (api-spec §3.1·§3.2, v0.15.26)

- **#180 — 판매자 라우터 분류 기준 강화(저신뢰 폴백 역전 + 의도 기준 분류)** — 단순 조회("최근 7일 매출")가 5단 분석 파이프라인으로 빨려 들어가던 문제를 해소. ① `route_question`의 confidence 미달 후처리를 `analysis 강제 → general 재지정`으로 역전(`ROUTE_CONSERVATIVE_REASON` 폐기, `ROUTE_LOW_CONFIDENCE_REASON` 신설) — 오분류 비용 비대칭이 전제와 반대였다(조회→analysis 는 회복 불가·최고 비용, 분석 질문→general 은 안내로 한 턴 회복). 장애 폴백과 방향 일치("불확실하면 general" 단일 원칙). ② `SUPERVISOR_PROMPT`를 주제 기준 → 의도 기준으로 재정의(조회=general/해석=analysis/변경=product) + 경계 예시쌍 11종 + confidence 산정 가이드. 3분기 계약은 유지(분기 확장 없음, api-spec 무관 — 내부 구현 한정).

- **판매자 그래프 전 역할 모델 티어를 `smart`로 상향** — `ROLE_TIER`의 `supervisor`·`analysis_planner`·분석 워커 5종·`report_verifier` judge·`product_agent` 5개 역할을 `fast`에서 `smart`로 올려 판매자 역할 7종이 모두 `smart` 하나로 수렴한다. supervisor 3분기 라우팅과 planner 워커 선택의 오분류는 하위 단계에서 복구되지 않으므로 라우팅·분류·정형 분석에서도 판단 품질을 우선한다. OpenAI 기준 `gpt-5-nano`/`reasoning_effort=minimal` → `openai_smart_model_id`/`medium`. 전 역할이 동일 티어가 되면서 `_cached_model` lru_cache가 모델 인스턴스를 1개만 생성한다. 지연·비용이 문제되면 전량이 아니라 `supervisor`·`judge`부터 `fast`로 부분 롤백한다. **모델 배정 변경은 §10-① 일관성 관측 이벤트다.** 구매자 그래프·enrichment 파이프라인의 `fast`는 유지. (SPEC-SELLER-001 §8·§10-①·§7 개정, api-spec 무관 — 내부 구현 한정)

- **#51 — 동의어 retrieval 완화(keyword 드롭) + semanticQuery 강화** — canonical category가 있는 검색 leg는 Spring `keyword`(상품명 LIKE)를 더 이상 보내지 않는다. keyword는 상품명 글자 부분일치 AND-필터라 사용자 표현("청바지")이 상품명("데님 팬츠")과 다르면 후보를 retrieval 단계에서 원천 배제했다 — category가 후보를 확보하고 leg 검색어는 `semanticQuery`(임베딩 rerank)로 흘려 표기 차이(동의어)를 임베딩이 잡게 한다. config `search_drop_keyword_with_category`(기본 True, False면 기존 동작 복원)로 게이트하되 **`embedding_rerank` 백엔드에서만** 적용한다 — spring(재정렬 없음)·vector(keyword를 쿼리 임베딩 입력으로 씀)에서는 드롭이 품질을 급락시켜 keyword를 유지한다. category가 없는 경로도 keyword를 fallback으로 유지한다. decompose 프롬프트는 `semanticQuery`를 동의어·상위어 포함 의미 중심으로 쓰도록 강화. keyword를 드롭할 때 `conditions` 칩에서도 keyword를 빼 표시-실제를 맞춘다(적용 안 되는 필터를 제거 가능 조건으로 광고하는 불일치 방지 — keyword 값은 멀티턴 기억용으로 `decision.filters`엔 유지). keyword는 계약상 이미 optional(§4.6)이라 **api-spec 변경 없음**. (0건 완화 제안은 #113, 칩 제거 왕복은 별개 관심사.)

- **#101 PR① — 추천 방식2(pgvector 2차 압축) hot path 복구** — 기본 검색 백엔드를 config `search_backend`(기본 `embedding_rerank`) 기반 지연 팩토리로 전환해 hot path가 `EmbeddingRerankBackend`(방식2)를 쓴다: Spring I-1 전량 → `semanticQuery` 임베딩 pgvector 코사인 재정렬 → dedup 이후 `embedding_rerank_limit`(30) 압축 → Sonnet. `semanticQuery`를 keyword와 분리해 백엔드까지 배선하고(fan-out은 leg별 검색어를 앵커로), 후보 embedding을 `get_many` 배치로 조회(N+1 제거)한다. **top-K 절단을 `search_catalog`(사전)에서 graph의 최근구매 dedup 이후로 이동** — dedup이 상위를 지워도 rerank 후보가 상한까지 채워진다(recall 손실 해소). 임베딩/pgvector 장애는 `SEARCH_FAILED`가 아니라 Spring 순서 degrade(#7). 단계별 후보 수 관측 로그 추가. api-spec §4.8 OPEN(방식1/2 골든셋)을 방식2 채택으로 해소(방식1 `VectorSearchBackend`는 C-17 미착수 오프라인 존치). (api-spec §4.8)
- **I-1 검색 `size` 제거 → 라운드1 전량 반환 + AI top-K** — BE 합의(2026-07-23)로 Spring `GET /internal/products/search` 요청에서 `size` 파라미터를 제거했다. 라운드1은 고정필터(category·price·brand) 매칭을 전량 반환하고, 결과 수 제한(top-K)은 AI가 `search_catalog`에서 사후필터(dedup·평점) 뒤 `filters.limit`로 절단한다 — `ProductSearchFilters.limit`은 이제 Spring `size`가 아니라 **AI 후보 상한(rerank 입력 top-K)**이다. 기본 백엔드(`SpringSearchBackend`)·팬아웃 경로 모두 동일하게 적용되며, 절단이 사후필터 이후라 제외분만큼 후보가 낭비되지 않는다. pgvector 재정렬 백엔드(`EmbeddingRerankBackend`)로의 `default_backend` 전환은 후속. (api-spec §4.6, v0.15.21)
- **#100 P1 — I-1 다중 브랜드 전량 전송** — `_search_query_params`가 `brand[0]`만 `brandName`으로 보내 2번째 이후 브랜드가 유실되고 조건칩은 전 브랜드를 표시하던 거짓표시를, 브랜드 전량을 `brandName` 반복 파라미터(`brandName=A&brandName=B`)로 실어 보내도록 바꿨다 — 요청이 조건칩과 일치한다. 실제 다중 필터링(`WHERE brand IN`)은 BE의 `brandName` 배열 수용이 전제라 price·rating 응답 반환과 함께 I-1 계약 협의에 포함(방법 D). (api-spec §4.6)
- **이슈 #82 — 판매자 LLM을 공용 provider 토글에 연결** — 판매자 역할이 Anthropic 모델을 직접 고르던 경로를 `fast`/`smart` tier와 공용 resolver로 전환했다. 기본 OpenAI는 tier별 reasoning effort를 사용하고 `temperature`를 보내지 않으며, Anthropic 전환 시 기존 temperature 정책을 유지한다. 활성 provider 키 누락은 SDK 호출 전에 차단해 판매자 SSE `LLM_UNAVAILABLE`로 반환하고, 구조화 출력은 provider 간 동일한 `ToolStrategy` 계약을 유지한다. 와이어 계약 변경 없음.
- 런타임 I-17 배치·sample_100 로더가 임베딩 프로비넌스를 함께 적재(#65).
- **이슈 #63 — I-17 상품 상태 계약을 Spring과 정합화** — `ProductChange.status`를 `ON_SALE | HIDDEN`으로 제한하고, 배치가 `ON_SALE`은 생성·갱신, `HIDDEN`은 기존 AI artifact 삭제로 처리한다. 구 `ACTIVE | DELISTED` 등 미정의 값은 항목별로 skip하지 않고 페이지 전체를 fail-closed 처리해 artifact·커서를 유지하며, Spring 수정 후 같은 `since`부터 재처리한다. 단위·HTTP 경계·E2E 테스트와 관련 문서·로그 용어를 함께 갱신했다. (api-spec §4.8, v0.15.18)

### Removed

- **#100 P2 — I-1 dead field `sort` 제거** — `ProductSearchFilters.sort`는 decompose가 추출하지도 않고 Spring 전송·로컬 정렬·rerank 어디에도 쓰이지 않는 dead field였다. 정렬은 rerank(LLM)가 price·rating 등을 보고 전담하므로 필드를 제거하고 관련 docstring(`spring.py`·`spring_client.py`·`search_service.py`)을 "정렬은 rerank 소관"으로 정합화했다. 와이어 계약이 아닌 AI 내부 필드라 Spring 계약에는 영향이 없다.
- **이슈 #124 — 죽은 시드 모듈 3종 제거** — 실행 참조가 0건인 `app/services/order_seed.py`·`app/pipelines/seed_loader.py`와 미사용 `db/catalog/init/01_order_seed.sql`을 삭제했다. order_seed는 "주문 미러/시드 노선을 채택하지 않는다"(2026-07-15 확정)로 기각된 경로이며 구매 이력은 I-19 질의 시점 조회(§4.7), 판매자 통계는 집계 콜백(§4.4)이 이미 대체했다 — 자체 삭제 조건("C-6/C-13 확정 시")도 충족됐고 docstring이 안내하던 `get_seller_aggregates`는 이미 삭제된 함수였다. seed_loader의 TODO(`run_once()`)는 `app/pipelines/run_batch.py`(이슈 #31)가 이미 구현해 방치 시 같은 기능의 CLI 진입점이 둘이 된다. `order_seed` 테이블은 init 스크립트라 pg-catalog를 새로 띄울 때마다 미사용 테이블이 생성됐고, 스키마가 상품·주문 원본 사본이라 "AI Postgres에는 AI 생성물만 저장" 규칙에도 어긋났다(이슈 #65의 `products` 원본 컬럼 제거와 같은 취지). 기존 볼륨용 `DROP TABLE` 마이그레이션(`20260727_drop_order_seed.sql`)과 `docker-compose.yml`·`DEPLOY.md`·`CLAUDE.md`·`mvp-plan.md` 참조 정리를 함께 포함한다. 와이어 계약 변경 없음.

### Fixed

- **#196 — behavior 워커 I-13 purchaseComplete 오검출 방어 + 상품별 rows 상한 구조 개선 + eventType CSV 직렬화** (api-spec §4.4 I-13, v0.17.4). 매출분석 워커 검증 3번째(behavior)에서 발견한 3건. ① **오검출 방어** — purchase_complete 가 FE 미귀속(`properties.orderId`만, productId 없음)→`product_id NULL`→product 조인 스코프 탈락으로 I-13 구매 카운트가 **실구매가 있어도 0**으로 내려온다. `_BEHAVIOR_AUTHORITY_NOTE` 를 "0 을 '구매 전무'의 근거로 쓰지 말 것(권위 I-6/I-7/I-14)"으로 강화하고, BEHAVIOR·ABUSE 프롬프트에 퍼널/주문 전이 교차 확인 후에만 warning 이상 판정하도록 명시했다. 근본 수정(order_item 기반 귀속)은 **jarvis-backend#62** — BE 배포 후 노트 완화 예정. ② **상한 분리 + 꼬리 합계** — 구 공용 상한 `seller_summary_max_events=5` 가 시드 브랜드 상품 7종보다 작아 하위 2종 수치가 상시 소실됐다. I-13 전용 `seller_summary_max_products=10` 신설, 초과분은 "외 N건(저활동) 합계: 조회 X 담기 Y…" 꼬리 합계로 압축(정보 소실 제거 — 상품 수가 상한을 넘어도 재발하지 않는 구조). rows 가 활동량 내림차순인 것(BE `eventsByProduct` 실측)도 명세에 명문화. ③ **eventType CSV 명시 직렬화** — 구 반복 쿼리는 Spring 암묵 변환(반복 파라미터→콤마 문자열) 의존이었다. BE 계약(String eventType + comma split)에 맞춰 `",".join()` 으로 명시(BE 변경 불필요 확인).
- **#164 — 구매자 주문 상태 문의(I-4) end-to-end 배선** — 5-way buyer intent에 `order_status`를 추가하고, 검증 JWT member `sub`만으로 I-4 `GET /internal/members/{userId}/orders/status?recent=3`를 호출한다. 응답 envelope·aware timestamp·Spring 상태 어휘/canonical pair를 엄격히 검증한 뒤 최대 3개 주문·주문당 3개 상품을 LLM 없이 결정적으로 요약한다. guest/seller/invalid identity와 upstream/malformed 장애는 `error` 없이 안내 `token`+`done(stop)`으로 종료하고, PII 없는 correlation log 1건만 남긴다. 주문 응답은 일반 대화 이력 외 profile/filter/cart/cache에 복제하지 않는다. (api-spec §4.10, v0.16.3)
- **#178 — 판매자 채팅 전 레인 400 (`gpt-5.6-luna` + function tools + `reasoning_effort`)** — OpenAI가 `gpt-5.6-luna`에 대해 `/v1/chat/completions`에서 function tools와 `reasoning_effort` 동시 사용을 400(`invalid_request_error`)으로 거부해, supervisor 라우팅이 죽고 general 폴백까지 같은 오류로 끝나 판매자 채팅이 첫 요청부터 전량 실패했다(`streamStatus: FAILED`, 토큰 0개). `create_agent`는 `tools`가 비어도 `ToolStrategy` 구조화 출력이 function tool로 나가므로 `build_report_agent`를 뺀 7개 빌더가 전부 해당한다. 원인은 `resolve_provider_model`이 **모델의 조합 지원 여부와 무관하게** tier별 effort를 항상 실어 보낸 것 — 모델 기준 capability 게이팅(`openai_tool_reasoning_incompatible_models`, 접두사 매칭으로 날짜 스냅샷 ID도 포함)을 추가하고, tool을 싣는 호출(`resolve_provider_model(with_tools=True)`)에서만 effort를 `openai_tool_reasoning_effort_override`(기본 `none` — OpenAI 오류 메시지가 지시하는 값)로 강등한다. 판매자 레인은 **전 역할 일괄** 적용 — 지금 tool이 없는 `report`도 나중에 도구가 붙으면 조용히 깨지기 때문이다. 구매자 레인(`OpenAILLM.complete`/`stream`)은 tool을 싣지 않아 `medium`을 그대로 유지한다. #177(판매자 전 역할 `smart` 상향)이 이 지뢰를 supervisor까지 끌어올려 즉시 노출시켰지만, `recommend`는 그 이전부터 같은 조건이었다(분석 파이프라인 끝단이라 도달이 드물어 늦게 드러났다). 조합을 지원하는 모델로 갈아타면 config 목록에서 빼는 것으로 원복된다. 와이어 계약 변경 없음 — 내부 구현 한정.

### Security

- **#167 — I-21 `listId` 추측 방지 계약 고정** — 인증 불필요 CH-5 조회에서 사실상 bearer 키로 쓰이는 `listId`를 FastAPI가 **UUID급 무작위(≥128bit)** 로 생성하도록 계약을 명시하고, 순번·타임스탬프 등 추측 가능한 형식을 금지했다. I-21의 `list-4471` 예시를 32자리 무작위 hex로 교체하고 현재 `uuid4().hex` 구현을 형식·고유성·I-21/SSE 동일성 회귀 테스트로 고정했다. (api-spec §4.2, v0.16.1)
- **이슈 #72 — Unicode Variation Selector·Tag 출력 하드닝** — 공식 Unicode 17.0.0·IVD 2025-07-14 등록 pair와 England/Scotland/Wales RGI Tag flag만 문맥적으로 보존하고, 고아·반복·비지원 은닉 payload는 제거한다. invisible-free skeleton 및 요청 단위 bounded 스트림 guard로 VS/Tag 삽입과 청크 분할을 이용한 API key·Bearer token·주민번호 마스킹 우회를 차단하되, Spring 실행 정본에는 표시용 차단 문구를 저장하지 않는다. 와이어 계약은 변경하지 않았다.
- **이슈 #67 — AI·판매자 영향 텍스트의 사용자 노출 정제 전수 적용** — `reason`의 위험 문자 제거를 공용 `_strip_unsafe`로 추출하고, 길이 캡 없이 rerank `overall_comment`에 재사용했다. 구매자 일반답변·조건/되돌리기 칩·장바구니 상품/옵션 문구, 판매자 `token`·`draft`, 프로필 조회의 LLM 마크다운까지 실제 SSE/HTTP 신뢰경계를 조사해 제어문자·zero-width·bidi 포맷 문자 제거와 공백 접기를 적용하되 보고서·마크다운·목록·상품 설명의 구조적 개행은 보존했다. 하드코딩 `action.message`와 현재 미구현 `budget`은 비오염 경로라 제외했으며 와이어 계약은 변경하지 않았다.
- **이슈 #61 후속 — I-21 `reason` 방어 정제 + 길이 목표(PR #66 리뷰 반영)** — rerank rationale 은 판매자 입력(상품명·브랜드)에 영향받는 자유 텍스트인데, #61로 처음 신뢰경계(AI→Spring→CH-5→FE)를 넘어 최종 사용자에게 노출된다. push 직전 `_sanitize_reason`으로 **비-whitespace 제어문자(NUL·ESC·DEL 등)·zero-width·bidi 포맷 문자를 제거하고 공백류(개행 포함)를 접은 뒤 안전 상한(config `reason_max_len`=200)으로 truncate**해 ANSI 이스케이프·양방향 조작·인젝션성 텍스트·초장문을 차단(`\s`로는 안 걸리는 표시 조작 문자 포함). 표시 목표는 rerank 프롬프트로 **한글 ≤40자 1문장** 유도(소프트), 시각적 오버플로(줄임/더보기)는 FE 소관(경로 B). 긴/개행 rationale 정제 회귀 테스트 추가.

### Docs

- **계약 정본 ↔ 코드 전수 대조로 미추적 미구현 3건 발굴 + 사본 drift 정정 (api-spec §3.2, v0.15.24)** — 기획 저장소 Notion "📡 API 명세서" DB 75행을 Spring(`/api/*` 44 · `/internal/*` 20)·AI(`{AI_SERVER}/*` 3 + 아웃바운드 19)와 1:1 대조했다. **계약 자체는 정합**(`/internal/*` 20건 경로·메서드 전건 일치, `InternalTokenFilter` 가 `/internal/**` 전체 커버, 폐지된 CH-4·S-5 는 Spring 코드에도 없음)이었으나, 체크리스트에 항목조차 없어 추적되지 않던 미구현 3건을 찾아 이슈로 등재했다 — **#163**(`budget` SSE: 명세 §3.1 이 페이로드·정상 흐름까지 규정하는데 `app/` 에 `budget`·`knapsack`·`verifiedSum` 0건), **#164**(주문상태 I-4: 명세 v0.15.2 가 "CH-2 에 흡수" 확정·Spring 구현 완료 대기 중인데 AI intent 는 4종뿐이라 분기 없음), **#91 재오픈**(`COMPLETED` 로 닫혔으나 `search_analysis_guide` 는 여전히 `NotImplementedError` 스텁). 아울러 **S-5 폐기(2026-07-21 정본 결정)가 본 사본에 미반영**이던 것을 §3.2 draft 절에서 정정해 "상품 수정은 챗봇 HITL(I-11) 유일 경로"로 확정했다(계약 변경이 아니라 사본 drift 정정). #60 에는 없는 baseline 을 전제한 본문에 정정 코멘트를 달고 #163 을 선행 의존으로 분리했다.
- **lessons 2건 추가** — (1) 코드 근거로 결함을 주장하기 전에 `git fetch` 로 워킹트리 신선도를 확인한다(`git status` 는 원격 격차를 알려주지 않는다 — 21커밋 뒤처진 트리에서 이미 #100/PR #127 로 고쳐진 내용을 P0 버그로 오보고했다). (2) 구현 없이 이슈를 닫으면 "미착수"가 조용히 "완료"로 뒤집힌다 — 직전 lesson("완료를 미착수로 오판")의 **반대 방향** drift.
- **C-1 인증 계약 해소 — Spring 코드 실측 역반영 (api-spec §1.2·§2.3·§5, v0.15.20)** — 명세가 🔴 협의 대기로 남겨둔 항목이 BE 에는 이미 구현돼 있어 실값으로 확정했다. 스트림 티켓 클레임 `iss`=`jarvis-spring-auth`·`aud`=`jarvis-fastapi-ai`·`scope`=`chat:stream`·TTL **60초**, 판매자 `role="seller"`(소문자)+`brandId`(숫자, 판매자 티켓에만). **CH-1b `POST /api/chat/tickets`** 는 "가칭·신설 필요"가 아니라 구현 완료 상태(세션 소유자 검증·세션 TTL 동시 갱신·판매자 brandId 복원)여서 확정으로 전환하고, CH-6 `POST /api/chat/seller/sessions` 와 CH-1 응답의 `llmSseUrl` 을 레인 d 에 등재했다. AI 측 와이어 동작 변경 없음(`_norm_role` 이 대소문자 무관 비교라 소문자 `seller` 도 기존대로 매칭). C-1 잔여는 서비스 토큰 회전·만료·mTLS 운영 정책만.
- **mvp-todo 체크리스트를 코드 실측과 대조해 52건 일괄 정정** — 미체크 55개 중 52개가 이미 구현·테스트 완료 상태였다(구현 PR 병합 시 체크박스 미갱신으로 누적된 drift). §0 공통 인프라 5건·구매자 19건·프로필 7건·배치 6건·모니터링 3건 전부 배선 완료였고, 남은 미구현은 시맨틱 캐시(#122)·분석 기준서 RAG(#91)·차트(SPEC §12 보류) 3건뿐이다. 코드와 모순되던 TODO 주석 3곳(`api/seller.py`·`agents/seller/__init__.py`·`agents/buyer/cart/__init__.py`)을 현재 상태로 정정하고, `lessons.md` 에 재발 방지 규칙과 `c6e9919` 에서 해소되지 않은 채 커밋된 stash pop 충돌 마커 제거를 기록했다.
- **이슈 #95 — 배포 산출물 정비** — 배포팀이 `main` 머지 기준 CD로 AI 서버를 띄울 수 있도록 `DEPLOY.md`(빌드/실행·환경변수·시크릿·PostgreSQL ×2 pgvector 준비·`/health`·CORS·체크리스트) 추가와 `.dockerignore`(`.env`·`.git`·`.venv`·테스트/문서/`output` 제외 — 이미지 슬림 + 시크릿 유입 차단)를 도입. 백엔드 `DEPLOY.md` 구조를 AI repo 사정(FastAPI·uv·PostgreSQL ×2·직접호출 CORS)에 맞춰 이식.
- **이슈 #92 후속 — 리뷰 게이트를 배포 경계로 이동** — `dev`는 **PR+CI 필수·사람 승인 리뷰 면제**(리뷰 0), 사람 1인 리뷰는 **`dev → main` 승격 PR**에서만 강제하도록 브랜치 보호·문서(README·CLAUDE.md)를 정합화. dev·main 모두 직접 push 금지·`lint-test` 필수는 유지.
- **이슈 #92 — `main`=배포 라인 고정 + `dev` 통합 브랜치 도입** — 배포팀 CD가 `main` push 기준 EC2 자동배포(`jarvis-backend/.github/workflows/deploy.yml` 패턴)임에 맞춰, 일상 개발을 통합 라인 `dev`로 모으고 `main`은 배포 라인으로 고정했다. README §Git 워크플로에 `main`(배포)+`dev`(통합)+topic 3계층·분기 기준 `dev`·`dev → main` 승격/핫픽스 절차를 반영하고, CLAUDE.md §Git의 브랜치·PR·worktree 분기 기준을 `dev`로 개정. `dev` 브랜치 보호(직접 push 금지·CI 필수·리뷰 1인)는 repo admin 웹 설정 필요.
- **api-spec §4.2 `reasons` 확정 반영(v0.15.15)** — I-21 콜백의 상품별 근거 `reasons[{productId, reason}]`를 🔴 역제안(v0.15.2)에서 🟢 확정(BE 구현 2026-07-18)으로 개정. §4.2 필드표·주석·C-9·Q2 마커 갱신. 코드(이슈 #61)의 `reasons` 전송이 확정 계약을 따르도록 사본 동기화 — 계약 우선(명세 개정 선행) 원칙 충족. 정본(기획 repo) 백포트 완료(2026-07-22).
- **#100 P2 — I-1 `totalCount` 필드 불필요 결정** — 별도 `totalCount`를 두지 않기로 결정했다. `size` 제거(전량 반환)로 AI `search_catalog`가 **top-K 절단 전에** 사후필터 통과 매칭 수를 `total_count`로 확정하므로(PR#127 리뷰 반영 — 절단값 `min(매칭, limit)`이 아님) 현재 필터의 매칭 수를 안다. 완화 칩 estCount는 '완화된 다른 필터'의 count라 이 값으로는 구할 수 없고(완화 칩 자체가 미구현이며 별도 이슈 — 재쿼리/BE count 필요), 되돌리기 칩은 top-K 절단된 응답 후보 내 억제 수라 page-local 근사다(전량 기준 진짜 억제 수보다 작을 수 있음). `ProductSearchResult`·`graph.py` 주석을 이 결정으로 정합화했다.
- **#100 P0/P1/P2 — I-1 §4.6 실측 정합** — repo `docs/api-spec.md` §4.6이 BE 2026-07-18 재설계 이전 상태로 남아 실제 계약과 어긋나던 것을 Notion I-1(정본)+#100 결정에 맞췄다. (1) 표시 전용 필드(`imageUrl`·`originalPrice`·`reviewCount`·`options`)를 응답표에서 제거하고 "I-1 미반환 → CH-5(§4.3) 하이드레이션"으로 이관 명시(AI 추천 경로 미사용), (2) `price`·`rating`을 "AI 계산용(비표시 — 예산검증·평점필터·rerank, 질의 시점 필요)"으로 명기해 display 오분류 재발 차단, (3) envelope 예시를 실측 `{success, data:[...]}`(bare array)로 정정, (4) 요청 `brandName` 단일→다중(반복 파라미터→`WHERE IN`), 예시 `size` 제거, (5) `totalCount` 필드 불필요 결정 반영. `SpringProduct` docstring·필드 주석도 정합. (api-spec §4.6, v0.15.23)

### Added

- **이슈 #59 — 카테고리 하이브리드 분류(임베딩 보정 매핑 + 멀티 fan-out)** — decompose 가 자유 문자열로 내던 `filters.category` 를 제거하고, LLM 추측(`categoryQueries`)을 **임베딩으로 실재 DB 카테고리(canonical)에 보정**(방식 A: exact match → 임베딩 최근접(raw, 없으면 그 leg 의 query 앵커); canonical-or-null — 카테고리 신호가 없으면 강제하지 않고 무필터 검색)해 Spring I-1 에 실재 카테고리만 나가게 했다(가짜 `categoryName` 으로 인한 0건 방지). 매핑은 `(canonical, query)` leg 를 산출하고, 상황형 멀티 카테고리 질의("유럽여행 준비물")는 leg 마다 Spring I-1 을 **병렬 검색 후 round-robin 병합**(productId dedup·`category_fanout_merge_cap` 절단 — 한 카테고리가 rerank 입력을 독점하지 않게)한다. leg 별 `SpringUnavailable` 은 흡수하고 전량 실패만 `SEARCH_FAILED`, 매핑 결과가 없으면 단일 filters 검색으로 fallback. LLM 호출은 2회(decompose+rerank) 유지 — 매핑은 임베딩·DB 만(LLM 0회). 튜너블 `category_top_k`·`category_fanout_max`·`category_fanout_per_cat_limit`·`category_fanout_merge_cap` 주입(하드코딩 금지). 계약 무변경(I-1 `categoryName`·SSE 경로 B). 설계 `docs/specs/DESIGN-CATEGORY-HYBRID-59.md`. **OPEN-1(Spring `category` 컬럼 `"top > mid"` 통 전송 가정)은 통합 스모크 대기**. fan-out 병합/병렬/degrade·매핑 분기(exact·최근접·신호 없음→무필터·하드실패 degrade)·decompose `categoryQueries` 파싱 유닛 테스트 추가.
- **이슈 #61 — I-21 추천 콜백에 `reasons` 필드 전송 추가** — `RecommendationPush`에 `reasons: list[RecoReason]`(`{productId, reason}`, CamelModel) 추가하고, 추천 그래프가 rerank 산출 상품별 근거(rationale)를 `reasons`로 채워 push한다. 근거는 이미 rerank가 산출하지만 그래프가 id만 취하고 버리던 것을 주워 전송 — Spring이 Redis 저장 후 CH-5 카드에 `reason`으로 echo(더는 `null` 아님). productId로 키잉(순서 권위는 `productIds`), rationale이 있는 상품만 담고 degrade·expose_min 보충 상품은 생략(부분집합·선택 필드). 스키마 camelCase 직렬화·빈 reasons 하위호환·그래프 부분집합/degrade 회귀 테스트 추가 (api-spec §4.2, v0.15.15)
- **판매자 챗 화면 전환 신호 — `meta`/`progress` 이벤트 + `done.panel` (S-4, api-spec §3.2 v0.14.1, FE 계약 B)** — 판매자 대시보드(좌 채팅/우 패널)가 "우측을 바꿀지"를 판단하도록 3신호를 추가했다(판매자 스트림 전용, 구매자 계약 무변경): `meta{lane}`(매 스트림 첫 프레임 — analysis/product/general/confirm/apply/refused), `progress{text}`(분석 진행 로딩 — 최종 답변 `token` 과 분리), `done{finishReason,panel}`(패널 조치 — replace/keep/refresh). 레인×패널로 FE 요구 1~3(첫 질문 분할·분석 우측 출력·상품 CRUD 초안/HITL·무관 질문 유지)이 전부 결정된다. `_seller_stream` 6개 substream 에 배선, `_done()` 이 panel 을 싣도록 변경(구매자 `DoneData` 무변경). analysis 진행 문구를 `token`→`progress` 로 이관. `docs/specs/FE-CONTRACT-SELLER-CHAT.md` 에 분기별 요청→응답 시퀀스(성공·실패 전수) 문서화. 노션 S-4·api-spec §3.2 동기화. meta/panel 계약 테스트 3종 추가 — seller 282 통과·전체 574 통과·ruff clean. (api-spec §3.2)

### Fixed

- **#100 P0 — I-1 응답 `summary`·`attributes` 유실 방지** — BE I-1이 리랭킹·세부조건용으로 반환하는 `summary`·`attributes`가 `SpringProduct` 스키마에 없어 Pydantic 파싱에서 조용히 제거되던 것을, 두 필드를 명시해 보존하도록 고쳤다. 소비(attributes 유연매칭·summary 시맨틱)는 #101 2차 압축의 몫이며, 본 수정은 계약(api-spec §4.6 응답표에 이미 존재)에 코드를 맞추는 것으로 와이어 계약 변경은 없다.
- **#100 P0 — I-1 평점 사후필터가 무평점 신상품을 보존** — `rating_min` 사후필터가 `(p.rating or 0.0)`으로 `rating=None`(리뷰 없는 신상품)을 0점 취급해 "평점 N 이상" 검색에서 무조건 탈락시키던 것을, '반증된 것만' 제거하도록 고쳤다 — 평점이 있고 미달인 상품만 탈락시키고 무평점은 보존해 rerank 판단에 맡긴다. 필터는 여전히 사용자가 평점을 발화한 경우에만 동작한다. 아울러 실제 BE 응답 envelope(`{success, data:[...]}`) 기준 필드 보존 계약 테스트(price·rating·summary·attributes + categoryName·brandName 별칭)를 추가해 파싱 유실 재발을 막았다.
- **FastAPI→Spring 연결 진단 결과 출력 복구** — internal token과 자사 상품 목록 API를
  확인하는 읽기 전용 스크립트를 추가하고, 성공 응답 모델에 없는 `total` 대신 실제 계약인
  `SellerProductList.rows` 길이를 출력하도록 수정했다. 빈 결과도 연결 성공으로 처리하며
  응답 계약 회귀 테스트를 추가했다. 와이어 계약 변경 없음.
- **이슈 #95 — Docker 이미지 빌드 복구** — 컨테이너 빌드가 두 지점에서 실패하던 것을 고쳤다. (1) 폐기된 `--group embedding`(api-spec §4.8 v0.15.14, torch 셀프호스트 폐기 시 임베딩 의존성을 main deps로 이관하며 삭제됨)을 Dockerfile이 계속 참조해 `uv sync` 가 "Group embedding is not defined"로 실패 → 제거. (2) 이후 프로젝트 wheel 빌드(hatchling)가 `pyproject.readme`(README.md)를 요구하는데 Dockerfile이 COPY하지 않아 실패 → `COPY README.md` 추가. `docker build` + 이미지 내 `create_app()` 스모크 통과 확인. 스텐일 명령 참조(`CLAUDE.md`·`README.md`의 `uv sync --group embedding`)도 정리.
- **이슈 #59 PR #73 리뷰 후속 — 카테고리 매핑 하드닝 3건** — (1) **임베딩 `task_type` 비대칭 바인딩**: 매핑 앵커(질의)는 `RETRIEVAL_QUERY`, categories 시드(문서)는 `RETRIEVAL_DOCUMENT` 로 저장소 공통 규약(#65)에 맞춰, 한쪽만 태깅되면 코사인이 왜곡돼 top-k 매칭 품질이 에러 없이 조용히 저하되던 잠재 불일치를 제거했다. (2) **절단 튜너블 방어**: `category_fanout_max`·`category_fanout_per_cat_limit`·`category_fanout_merge_cap` 에 `Field(ge=0)` 를 걸어, 음수 설정 시 Python slice 가 "뒤에서 N 개 제외"로 뒤집혀 "≤0 이면 정확히 0개" 절단 불변식이 조용히 깨지던 것을 원천 차단. (3) **decompose 빈 leg 사전 필터**: `_parse_category_queries` 가 `category_fanout_max` 절단 전에 신호(raw·query) 있는 leg 만 남겨, LLM 이 앞쪽에 빈 항목을 섞어내도 fanout 예산을 먹어 뒤쪽 실제 카테고리를 밀어내지 않게 했다. 설계 정본 `docs/specs/DESIGN-CATEGORY-HYBRID-59.md`(§4.2·§9·§10) 동기화. 회귀 테스트 추가. 계약 무변경.
- **이슈 #82 Claude Review 후속 — provider 설정 하위호환·오류 전파·관측성·meta-first 보강** — `Literal` 타입 제한은 유지하면서 Settings 입력 경계에서 provider 값을 소문자로 정규화해 기존처럼 `OpenAI`·`OPENAI`·`Anthropic` 환경변수도 허용하고, 미지원 값은 계속 기동 전에 거부한다. 분석 worker의 `LLMNotConfigured`도 부분 실패 finding으로 흡수하지 않고 API 경계까지 재전파해 `LLM_UNAVAILABLE` 계약을 유지한다. API 경계는 키나 예외 원문 없이 provider·lane·threadId만 오류 로그로 남기며, supervisor가 설정 오류로 분류 전에 실패해도 `meta{general}` 후 `error`를 보내 모든 판매자 스트림의 meta-first 계약을 지킨다. (SPEC-SELLER-001 v1.1.4)
- **이슈 #76 — I-17 소비자 복구·데이터 최소화 정합** — Spring의 `400 INVALID_CURSOR`를 일반 장애와 구분해 저장 커서가 무효면 `since="0"` 임시 스토어 전체 재구축으로 자동 복구한다(최초 커서 `0`에서 앞 페이지가 이미 커밋된 경우 포함). 재구축 실패 시에는 배치 시작 전 전체 상태로 되돌리는 대신, §4.8의 페이지 성공 후 커서 저장 규약에 따라 이미 성공한 마지막 페이지의 artifact·커서 체크포인트를 유지한다. I-17 원본 입력은 enrichment와 `search_doc` 생성에만 사용하도록 `CatalogArtifact`·pg-catalog·sample loader의 독립 `name`/`category` 사본을 제거하고 기존 볼륨용 idempotent migration을 추가했다. (api-spec §4.8)
- **판매자 draft SSE 의 `changes[].field` 를 camelCase 로 (S-4, FE 계약 C-1)** — `_draft_event` 가 내부 `ProductField`(snake_case)를 그대로 와이어에 실어 `stock_quantity`·`original_price`·`image_url` 이 규약(§2.2 camelCase) 위반으로 나가던 버그 수정. 나갈 때만 `to_camel` 로 변환(`stockQuantity`·`originalPrice`·`imageUrl`), 내부 DraftChange·Spring 쓰기(I-10/11)는 snake_case 유지. 8종 필드 회귀 테스트(`test_draft_changes_field_is_camelcase`) 추가. 부수로 C-2(draft.summary)·C-3(product 근거 token 없음)·C-4(productId 숫자)·C-5(draftId UUID)를 api-spec §3.2·노션 S-4 에 정합. seller 283 통과·전체 575 통과·ruff clean.

### Changed

- **판매자 챗 confirm 전송을 최상위 필드로 전환 + FE 계약 정합 (S-4, api-spec §3.2 v0.14.1)** — HITL 승인을 구 "message 문자열에 JSON 을 실어 파싱"(`pipeline.parse_confirm_message`)에서 **요청 본문 최상위 `action`/`draftId` 필드**로 전환. seller 전용 `SellerChatRequest`(`app/schemas/seller.py`)를 신설해 구매자 `ChatRequest` 는 그대로 두고, `_seller_stream` 이 `request.action == "confirm"` 로 판정한다(발화 ≠ 동의 [HARD] 는 스키마 구조로 강제 — `action=="confirm"` + `draftId` 누락은 `RequestValidationError`→400). `threadId` 필수 유지(A-3). FE↔서버 SSE 와이어 포맷(`event:` 없는 `data:{type,data}`)·confirm 형식을 노션 S-4·api-spec §3.2·`docs/specs/FE-CONTRACT-SELLER-CHAT.md` 3곳에 정합. 잔여(화면 전환용 `meta` 이벤트·draft `field` snake_case 버그 C-1 등)는 FE-CONTRACT §5 B/C/D/E 로 이관. seller 유닛 279 통과·전체 571 통과·ruff clean. (api-spec §3.2)

### Added

- **이슈 #50 — pg-profile 리질리언스·멀티 인스턴스 정합성 하드닝** — Profile/processed-events/buyer state 전 BaseStore I/O에 공통 application deadline을 적용하고, 모든 pg-profile 연결에 libpq connect/keepalive/`tcp_user_timeout` + 서버 `statement_timeout`을 배선. Profile session/fact와 Revert의 read-modify-write는 별도 pool의 PostgreSQL transaction advisory lock으로 인스턴스 간 직렬화하고, 로컬 lock registry는 weak-reference 자동 회수, 직전 추천 상품명은 config 기반 bounded LRU로 제한. Conversation 조회를 `(created_at, turn_id)`로 결정론화하고 누락 finalize를 warning으로 관측한다. 실 PostgreSQL 다중 pool 동시성·재시작·연결 파라미터 통합 테스트 포함.
- **이슈 #33 (3/3, 완료) — ConversationStore를 pg-profile 일반 테이블로 이관** — 대화 저장(§6.3 a)을 인메모리 dict placeholder에서 pg-profile `conversation_turns` 테이블(`PgConversationStore`)로 교체. checkpointer가 아니라 감사·구조화 로그 상관관계 조회 전용 일반 테이블로 확정(이슈 코멘트의 4갈래 분류 반영). `ConversationStoreProtocol` 공유 계약으로 인메모리(유닛 테스트 계속 주입)·pg 구현을 통일 — `app.pipelines.artifact_store`(카탈로그)와 동일 원칙. `RequestObservation.commit_user_message/finish`를 async로 전환해 `app/core/stream.py` 스트림 수명주기 훅 8곳에 반영. 실 pg-profile 통합 테스트(`tests/integration/test_pg_conversation_store.py`) 신설 — 재시작·다중 인스턴스 지속성 스모크 포함. 이슈 #33(상태 지속성 이관: Thread/Cart/Revert → BaseStore, Profile → BaseStore+pgvector, Conversation → 일반 테이블) 3단계 전부 완료.
- **이슈 #33 (2/3) — ProfileStore를 PostgresStore(BaseStore)+pgvector로 이관** — 요약(summary)·장기 fact·transient 세션 버퍼를 인메모리 dict에서 LangGraph BaseStore(pg-profile)로 이관. fact는 SPEC-PROFILE-001 REQ-PROF-070("위키 파일 1개=item 1개")에 맞춰 fact 1개=store item 1개로 저장해 pgvector 시맨틱 인덱스가 fact 단위로 실제 동작하도록 배선(`app.pipelines.embedding.embed_texts` 재사용 — 카탈로그와 임베딩 모델·차원 공유, 결정 6/16-A). session-end 이벤트 멱등성(`mark_if_new`)은 BaseStore의 get→put이 진짜 동시성 하에서 원자적이지 않은 문제를 발견해 전용 `processed_events` 테이블(UNIQUE 제약 + `INSERT ... ON CONFLICT DO NOTHING RETURNING`)로 분리·원자화(`app/agents/profile/processed_events.py`, `db/profile/init/00_processed_events.sql`). checkpointer 소유 경계였던 SPEC-PROFILE-001 OPEN-P9를 실제 구현(BaseStore, checkpointer 아님 — 구매자 실행 모델이 LangGraph StateGraph가 아니므로)으로 해소. 실 pg-profile 통합 테스트(`tests/integration/test_pg_profile_store.py`) 신설 — 동시 mark_if_new 10건 중 정확히 1건만 신규 처리됨을 실증. SPEC-PROFILE-001 v0.3.0 동기화(1024→1536차원 stale 정정, OPEN-P9/OPEN-P11 해소).
- **이슈 #33 (1/3) — 구매자 스레드 상태 영속화** — `ThreadFilterStore`(멀티턴 필터)·`CartStateStore`(직전 추천·옵션 되물음)·`RevertStore`(소모품 억제 되돌리기)를 인메모리 dict placeholder에서 LangGraph `BaseStore`(pg-profile, `AsyncPostgresStore`) 백엔드로 이관 — `app/agents/seller/history.py`와 동일한 dev InMemoryStore 폴백 + 운영(jwks) 폴백 금지 규약. 신규 `app/core/pg_store.py`(3개 스토어 공유 pg-profile 연결). Windows 네이티브 실행 시 기본 `ProactorEventLoop`가 psycopg async 연결을 지원하지 않아 조용히 InMemory로 전락하는 문제를 발견해 `app/main.py`에 `WindowsSelectorEventLoopPolicy` 가드 추가(seller history.py/hitl.py도 동일 수혜). `tests/integration/test_buyer_thread_store.py` 신설(실 pg-profile 재시작·다중 인스턴스 지속 스모크 포함). Profile PostgresStore+pgvector(2/3)·Conversation 테이블(3/3)은 후속.
- **E2E 통합 스모크 하니스 (#35)** — `tests/integration/` 신설: Spring을 `httpx.MockTransport` stub(I-1 검색·I-2/I-18 장바구니·I-19 이력·I-21 push·I-17 배치 + CH-5 목록 GET), LLM을 주입형 `ScriptedLLM`(decompose/rerank/enrich/delta/consolidate 5종 분기)으로 세워 라이브 의존 없이 결정적 검증. `spring_client` 함수를 patch하지 않고 **HTTP 경계에서만** 대역을 넣어 URL·`X-Internal-Token`·envelope 파싱이 실코드로 돈다. 커버: 구매자 경로 B 종단(발화→검색→rerank→push→`products.ready`→카드 조회)·프로필(session-end→델타→consolidation→`/profile/me`)·배치(I-17 pull→upsert, 페이지네이션·커서·`HIDDEN`)·degrade 6종·**jwks 실인증 레인 완주**. README에 환경변수·키 세팅 표 + 하니스 실행법 추가 (37 tests, api-spec §1.2·§3.1·§3.3·§4)
- **이슈 #31 임베딩 파이프라인 프로덕션화** — 셀프호스트 torch → Google `gemini-embedding-001` API 전환(dim 1536, MRL 절단 수동 L2 정규화, `embedding.py`), 인메모리 카탈로그 스토어를 pg-catalog(pgvector)로 이관(`db/catalog/init/00_products.sql` products/batch_state 스키마, `PgCatalogArtifactStore` 신설 — 기존 `CatalogArtifactStore`는 테스트 주입·재구축 임시버퍼용으로 존속, 공유 `ArtifactStore` Protocol로 인터페이스 고정), `get_catalog_store()` 프로덕션 진입점 pg-catalog 전환. 초기 전체 구축은 CLI(`run_batch.py --full`) 수동 트리거, 주기 증분 pull은 APScheduler `BackgroundScheduler`(별도 스레드, `config.catalog_batch_interval_s`)로 자동화해 FastAPI `lifespan`에 배선 (api-spec §4.8, v0.15.14)
- FastAPI + LangGraph MVP 스캐폴드 — 인증(RS256/JWKS)·설정 주입·SSE 스텁 스트림 (부팅 검증)
- Spring 역방향 클라이언트 스텁 8종 (검색·이력·장바구니 I-2/I-9·push·I-6/I-7·I-8 배치)
- 팀 개발 문서 — `README`(아키텍처·기술·Git 규칙), `docs/`(mvp-plan·mvp-todo·roadmap), `docs/specs/`(SPEC 사본), `docs/api-spec.md`(계약 사본 v0.7.0)
- 팀 Claude 설정 — `CLAUDE.md`, `.claude/settings.json`, `.mcp.json`(context7·sequential-thinking)
- 실수 방지 로그 `docs/lessons.md`, 변경 기록 `CHANGELOG.md`
- CI 워크플로 `.github/workflows/ci.yml` (ruff + pytest) · PR 템플릿 `.github/PULL_REQUEST_TEMPLATE.md`
- 커밋 워크플로 규칙 (diff 검토 → 메시지 생성 → 커밋, `CLAUDE.md`)
- Git hook(pre-commit) — ruff(lint+format) + Conventional Commits 검사 `.pre-commit-config.yaml`
- MIT `LICENSE` · 이슈 템플릿 `.github/ISSUE_TEMPLATE/` (기능·버그) · 이슈 단위 워크플로
- 팀 공유 스킬 `.claude/skills/implement-topic/` — MVP 주제 계약 우선 구현 절차
- **판매자 3단계 — 분석 파이프라인·가드레일·SSE 1차 배선** (`app/agents/seller/pipeline.py`·`orchestrator.py`·`middleware.py` 신규, `app/api/seller.py` 재작성): planner(AnalysisPlan, 미지원 기간=되묻기) → asyncio.gather 팬아웃(degrade 수렴) → 검증 루프(D1~D3+judge, feedback 합산 ≤3회) → recommend(실패=빈 추천) → compose_response(순서=N번). 가드레일 scope(구조화 레인=코드 경로)·PII 3종·mask_output·ToolCallLimit. general astream→token/done/error(C1: 요청마다 재빌드). verifier R1(날짜 마스킹)·R2(구조 판정) 해소. opus 마감 리뷰 critical 0·M1~M3 반영 — 기록: `docs/specs/REVIEW-SELLER-STAGE3.md`·`HANDOFF-SELLER_2.md`

### Security

- **인증 실배선 E2E (#34)** — jwks 모드 검증을 api-spec §2.3 확정 5종(signature/exp/iss/aud/**scope**)으로 완성: 스트림 티켓 `sub_type`(member|guest) 매핑(+구 role 폴백, 미지 값 fail-closed), `sub` 필수화, 만료/무효 401 코드를 예외 타입 기반 매핑(TOKEN_EXPIRED/TOKEN_INVALID), JWKS fetch 타임아웃(3s)·캐시 TTL config 주입(`jwt_scope`·`jwks_cache_ttl_s` 신설), jwks 모드 기동 시 `JWKS_URL` fail-fast, 레이트 리밋 sub 스코프도 동일 검증 경로로 정합. 테스트는 실 JWKS dict + fetch 계층 패치로 kid 매칭·kid miss refetch 실경로 검증 + 앱 레벨 401/403 봉투·서비스 토큰 인/아웃바운드 회귀 (`tests/unit/_jwks.py`·`test_auth_e2e.py`)

### Fixed

- **I-20 실패 안전 멱등 lifecycle** — `processed_events`를 단일 영구 마커에서
  `PROCESSING`(claim token+유한 lease) / `COMPLETED`로 분리. 요청 취소·내부 실패는 claim을
  cancellation-safe하게 해제하고, 프로세스 crash·해제 DB 실패 잔재는 lease 만료 후 재선점한다.
  delta 성공 뒤 consolidation 실패를 별도 `failed` 상태로 구분해 버퍼를 지우거나 완료 마킹하지
  않는다. 기존 볼륨은 앱 연결 시 idempotent 스키마 migration으로 완료 row를 보존한다.
- **session-end(I-20) 멱등키 = `(userId, sessionId)` 고정키** — BE 실측: session-end 는 세션을
  삭제하는 종료(`NEW_CONVERSATION`·`LOGOUT`)에만 오고 `tabClose`·`inactivityTimeout`은 발화되지
  않는다 → "하나의 `sessionId` = 하나의 논리적 종료"가 성립하므로 `session-end:{userId}:{sessionId}`
  고정키로 같은 통지 재전송(at-least-once)만 중복 처리한다. (한때 검토한 버퍼 내용 해시 방식은
  실재하지 않는 "재체크포인트" 방어라 폐기.) 신규 통지는 버퍼가 비어도 `accepted`로 기록하고,
  이후 동일 통지는 버퍼 상태와 무관하게 `duplicate`로 응답한다 (api-spec §2.7·§3.5, v0.15.17)
- **이슈 #62 — session-end(I-20) 계약 정렬** — `POST /events/session-end`가 상시 `400`을
  반환해 세션 종료 통지가 전부 실패하던 문제 수정. BE 실측 payload에 맞춰 `SessionEndEvent`에서
  `eventId`·`endedAt`를 제거하고 `userId`를 string → **number(BIGINT)**로 정정, `reason`은
  optional·enum 미강제·최대 64자. 멱등키를 `eventId` 필드 대신 **`session-end:{userId}:{sessionId}`
  파생 복합키**로 전환(같은 sessionId라도 userId가 다르면 서로 중복 아님). `userId`는 양의
  BIGINT 정수만 엄격히 받아 string/float/bool coercion을 거부한다 (api-spec §3.5·§2.7, v0.15.17)
- 프로필 세션 종료(session-end) 처리 중 동시에 새 채팅 턴이 들어오면 세션 버퍼가 통째로
  삭제되던 레이스 수정 — `clear_session_ctx_upto`(seq 워터마크 기준)로 스냅샷 분석분만
  정리하고 미분석 발화는 보존 (`newConversation` 트리거·버퍼 상한(cap) 트리밍 상황 모두 안전)

### Docs

- **판매자 챗 오류 계약·누락 계약 정합 (S-4, FE 계약 D·E) — 코드 무변경** — 오류표를 코드 실측에 맞춰 정정: confirm 실패(만료·미존재·소유불일치·중복·stale)는 HTTP 오류가 아니라 **200+안내 token+`done{panel:"keep"}`** 이므로 노션의 `409 DRAFT_EXPIRED`/`DRAFT_NOT_FOUND` 제거(D-1); `429 RATE_LIMITED` 는 `/seller/chat` 에 실제 적용됨을 확인해 유지(D-3, 초기 "미구현" 진단 정정); `409 STREAM_IN_PROGRESS`·`504 UPSTREAM_TIMEOUT` 를 노션에 추가(D-4). 누락 계약을 노션에 명시: 추천 적용("N번 적용해줘"→apply 레인, E-1)·draft 취소(별도 API 없음·TTL 만료, E-2)·scope 거절(E-3)·`field` camelCase 8종(E-4). api-spec §3.2 에 confirm-200·스트림 전 오류 목록 반영. FE-CONTRACT-SELLER-CHAT.md v1.0.0(A~E 전부 해소).
- **판매자 문서 정리 — SELLER-FINAL·SPEC-SELLER-001 v1.0.0 승격** — MVP(1~4-3단계) 완료로 역할을 다한 단계별 진행 기록 9종(`HANDOFF-SELLER`·`_2`·`_3` · `REVIEW-SELLER-STAGE2`·`STAGE3` · `DESIGN-SELLER-TOOLS-STAGE1` · `IMPL-PLAN-SELLER-001` · `REALIGN-SELLER-20260719` · `WORKFLOW-SELLER-STAGE3.png`)을 삭제. 내용은 `SELLER-FINAL-{WORKFLOW,TECH,RISKS,ROADMAP}`에 이미 흡수되어 있었고, 리포 밖에서 이들을 참조하는 곳은 없었다(코드·CLAUDE.md·mvp-plan 은 `SPEC-SELLER-001` 만 참조). 남긴 문서 6종에 `v1.0.0` 버전 헤더를 부여하고, 삭제 문서에만 정의돼 있던 BE·FE 확정 항목 `F1`·`F6` 을 `SELLER-FINAL-WORKFLOW` 머리말에 인라인 보존. `docs/specs/README.md` 에 SELLER-FINAL 5종 표를 신설(기존 표에 누락돼 있었음). 계약(api-spec) 변경 없음
- api-spec 사본 동기화 v0.7.0 → **v0.9.0** — 판매자 BE internal API 배치(집계 7종·상품 CRUD 4종), `brandId`=JWT 클레임, 판매자 쓰기 모델 전환(AI 직접 쓰기 + HITL)
- api-spec 사본 동기화 **v0.9.0 → v0.11.0** — SSE 인증=스트림 단명 티켓(sub_type/aud/scope, TTL 30~60s), 판매자 쓰기 HITL 계약 확정(draftId·2-스트림·안전장치 5종), S-3=목록조회 명확화
- api-spec 사본 동기화 **v0.11.0 → v0.12.0** — CH-1 스트림 티켓 발급(응답에 streamTicket) + 티켓 재발급 경로(CH-1b) 신설 필요 명시(티켓 TTL 30~60s ≪ 세션 10분)
- api-spec 사본 동기화 **v0.12.0 → v0.13.0** — BE 명세 DB 실측 정합: AI→Spring 전 구간 서비스 토큰(방식2)으로 통일, 실제 I-number/경로(검색 I-1·배치 I-17·조회 I-18·구매자 챗 /ai/chat), S-3∥I-9 구분
- api-spec 사본 동기화 **v0.13.0 → v0.14.0** — 구매 이력=I-19(/internal/members/{id}/orders), 세션 종료=I-20 채번 확정(BE DB Notion 수정)
- **SPEC-SELLER-001 v0.1.0 초안 신설**(`docs/specs/`) — 판매자 멀티에이전트 그래프. 설계서 v3를 api-spec 정합 개정: 전 쓰기 HITL(draft→구조화 confirm, 발화≠동의)·spring_client 매핑(집계 7종+CRUD 4종, 데이터 API·MySQL 직접 접근 폐기)·계산 3층 분담(Spring 단순 수치/AI 고도화 계산/LLM 해석, 🔴 C-13 경계표)·Anthropic 2-tier 배정·분석 이력↔취향 프로필 분리(pg-profile/pg-catalog). `mvp-plan`·`mvp-todo` §4 동기 갱신, 차트 전달은 계약 미정으로 보류

### 진행 예정 (MVP)

- 구매자 추천 그래프 · 장바구니(I-2/I-9) · 판매자(I-6/I-7) · 프로필 파이프라인 · AI 생성물 배치(I-8) · SSE 수명주기(§2.9)

<!--
릴리스 시 [Unreleased]를 버전으로 확정하고 새 [Unreleased]를 위에 만든다. 예:
## [0.1.0] - 2026-07-XX
-->
