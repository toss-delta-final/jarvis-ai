# DESIGN — 판매자 분석 보고서 구조화 `report` SSE 이벤트 (차트 내장) · 계약 변경 명세 + 단계별 구현 설계

작성일: 2026-08-05 (v2 — recommendations 존재 시그널 규약 반영) · 대상: `jarvis-ai`(BE) + `jarvis-front`(FE, 별도 이슈)
상태: **설계 확정 · 구현 미착수 · 코드 미변경**
사용자 확정(2026-08-05): ① 명세 정본은 Notion — 정본 등재가 사본(api-spec.md) 개정에 선행 ② FE(jarvis-front)의
`chart` 미소비 확인 완료 ③ `feat/290` 을 dev 에 머지한 뒤 본 작업 착수.
선행 문서: `HANDOFF-REPORT-PANEL`(2026-08-04 핸드오프), `docs/specs/DESIGN-ANALYSIS-V31-242.md`(이슈 #242)
검증 기준: 본 문서의 BE 서술은 2026-08-05 `feat/290` 워킹트리(orchestrator·pipeline·schemas·seller.py·api-spec v0.23.0),
FE 서술은 2026-08-05 `jarvis-front` 실코드(useChat.ts·store.ts·types/chat.ts·SellerWorkspace 등)를 직접 확인한 결과다.

> 파일명 규칙: 이슈 등록 후 `DESIGN-REPORT-PANEL-296.md` 로 rename 권장 (repo 관례).

---

## 0. 한 줄 요약

분석 레인의 최종 산출을 `token` 산문 하나로 보내던 것을 **구조화 `report` SSE 이벤트(차트 데이터 내장) 1개**로
확장하고, 소비자 없는 기존 `chart` 이벤트(v0.20.0)를 폐기한다. LLM 파이프라인은 불변 — 코드 직렬화만 추가한다.

## 1. 와이어 계약 변경 (Before / After)

### 1.1 이벤트 목록

| | 판매자 SSE 이벤트 |
|---|---|
| **Before (v0.23.0)** | `meta` · `progress` · `token` · `draft` · **`chart`** · `done` · `error` (7종) |
| **After (개정안)** | `meta` · `progress` · `token` · `draft` · **`report`** · `done` · `error` (7종 유지) |

- `chart` 는 **legacy 폐기** — 구 `metrics`/`analysis` 선례처럼 "부활 없음" 명시. dual-emit 하지 않는다.
  근거: FE `useChat.ts` 이벤트 switch 에 `chart` 케이스가 **없음을 실코드로 확인**(2026-08-05) — 소비자 없는 계약.
- 구매자 이벤트는 무관 — 구매자 `progress`(v0.21.0, #289)와 이름 충돌 없음. `report` 는 판매자 전용 신규 이름.

### 1.2 스트림 순서 (analysis 레인)

```
Before: meta{analysis} → progress×N → token(보고서 전문) → chart(0~1회, 조건부) → done{panel:"replace"}
After:  meta{analysis} → progress×N → token(보고서 전문) → report(정확히 1회)  → done{panel:"replace"}
```

- `report` 발생 조건: `analysis` 레인 · 파이프라인 결과 `kind=="report"` 일 때 **정확히 1회**.
  되묻기(clarification)·사과(apology)·거절(refused)·error 종료에는 **발행하지 않는다**.
- 보고서±차트 분기는 이벤트 유무가 아니라 `report.data.charts` **배열 유무로만** 표현한다 — 빈 배열 허용.
  이로써 chart 의 "0~1회 · 미발행 규약(빈 배열도 금지) · token 뒤 done 앞 순서 규약 · FE 버퍼링/done 조인"이 전부 삭제된다.
- 좌측 채팅용 `token`(보고서 전문 산문)은 **현행 유지** (스레드 기록 `record_turn`·분석 이력 `save_history`·후속 발화
  맥락의 원천). token 슬림화는 2단계 별도 명세 개정. → 1단계에서는 body 전문이 token 과 report 에 중복 전송된다(허용).

### 1.3 `report` 이벤트 페이로드

```json
{
  "type": "report",
  "data": {
    "title": "판매 분석 보고서",
    "period": { "from": "2026-07-01", "to": "2026-07-31" },
    "generatedAt": "2026-08-05T10:12:00+09:00",
    "summary": "핵심 요약 2~3문장 (보고서 첫 문단 코드 분리, 실패 시 앞 200자)",
    "body": "검증된 보고서 산문 전문 (VerifiedReport.report 그대로)",
    "findings": [
      {
        "analysisType": "sales_anomaly",
        "severity": "warning",
        "summary": "…",
        "evidence": ["07-12 매출 1,250,000원 (평균 1,850,000원)"],
        "recommendation": "…"
      }
    ],
    "limitations": ["데이터 확보 실패 — 분석 실행 오류(응답 시간 초과)"],
    "chartRequested": true,
    "charts": [
      {
        "title": "일별 매출 추이",
        "chartType": "line",
        "unit": "KRW",
        "series": [{ "label": "매출", "points": [{ "x": "07-01", "y": 1850000 }] }],
        "summary": "보고서 인용 한 줄"
      }
    ],
    "recommendations": [
      {
        "index": 1,
        "title": "감귤청 가격 10% 인하",
        "expectedEffect": "…",
        "actionType": "price_adjust",
        "productId": 10293
      }
    ],
    "applyGuide": "적용을 원하시면 \"N번 적용해줘\"라고 말씀해 주세요."
  }
}
```

### 1.4 필드 사전 (원천 · 규칙 · 마스킹)

마스킹·정제는 `_draft_event`/`_chart_event` 선례 그대로: 전 텍스트 필드 `mask_output(...)`,
한 줄 필드는 `_strip_unsafe`, 여러 줄 허용 필드는 `_strip_unsafe_multiline`. snake→camel 변환.

| 필드 | 타입 | 원천 (BE 내부) | 규칙 | 정제 |
|---|---|---|---|---|
| `title` | string | 코드 상수 `"판매 분석 보고서"` | 1단계 고정값 (추후 확장 여지) | — (상수) |
| `period.from/to` | string(ISO date) | `ResolvedPlan.date_from/date_to` | ⚠️ 현재 `PipelineResult` 에 없음 — **필드 추가 필요** (2단계) | — |
| `generatedAt` | string(RFC3339) | emit 시각, 서버 생성 | **KST(+09:00) 고정** — `save_history` 표시 관례와 일치 | — |
| `summary` | string | `VerifiedReport.report` 첫 문단 | 코드 분리(첫 빈 줄 기준). 분리 실패·과장(>300자)이면 앞 200자 절단 + "…" (§5.1) | multiline |
| `body` | string | `VerifiedReport.report` 전문 | 무가공 | multiline |
| `findings[]` | array | `AnalysisFinding` 목록 (degrade 포함) | `analysis_type→analysisType` 등 camel 변환. **`chart_data_hint` 는 싣지 않는다**(내부 보류 필드) | summary·evidence[]·recommendation: multiline / analysisType·severity: Literal 그대로 |
| `limitations[]` | array<string> | degrade finding(`evidence==[]`)의 `summary` 모음 | 없으면 빈 배열 | multiline |
| `chartRequested` | boolean | `ResolvedPlan.wants_chart` | ⚠️ `PipelineResult` 에 없음 — **필드 추가 필요** (2단계). true 인데 `charts==[]` 면 FE 가 실패 안내 렌더 | — |
| `charts[]` | array | `ChartSet.charts` | **기존 `_chart_event` 직렬화 본문 그대로 이관** — ≤3개 · series 1개(스키마 max_length=1) · points ≤60 · unit `KRW\|COUNT\|PERCENT`. **빈 배열 허용** | title·label·x: single / summary: multiline / chartType·unit·y: 그대로 |
| `recommendations[]` | array | `RecommendationSet.recommendations` | **목록 순서가 곧 "N번"**(§6.3 계약) — `index`(1-base) 명시로 FE 정렬 사고 방지. 추천 없으면 빈 배열(FE 는 배열 유무로 추천 섹션 렌더 분기). `rationale`·`changes` 는 싣지 않는다(카드 간결성 — 필요 시 추가 전용 확장) | title·expectedEffect: single / actionType: Literal / productId: number |
| `applyGuide` | string | `pipeline._APPLY_GUIDE` 상수 | recommendations 비었으면 빈 문자열("") — 추천 없는 보고서에 "N번 적용해줘" 안내가 나가지 않게 FE 는 recommendations 와 함께 표시 | — (상수) |

### 1.5 명세 개정 절차·지점 (선행, 1단계)

- **정본이 먼저다** — 명세 정본은 Notion(FE/BE 팀 챗 API 문서, §2.2 [HARD] 명명 기준). 구매자 `progress`(v0.21.0)
  선례대로 **정본 등재·합의 → 사본 `docs/api-spec.md` 개정 커밋** 순서를 지킨다.
- **버전: v0.24.0** — ⚠️ 핸드오프(8/4)가 가정한 v0.21.0 은 이미 소진됐다(8/5 하루에 v0.21.0 #289 · v0.22.0 #149 ·
  v0.23.0 #116/#117). **착수 시점에 최신 버전을 재확인**하고 그 다음 번호를 쓴다.
- 개정 범위 (한 커밋):
  1. **§2.2 명명 규약** — 판매자 이벤트명 줄: 현재 `token/draft/chart/done/error` 5종 표기(meta·progress 누락된 구 표기)를
     `meta/progress/token/draft/report/done/error` 7종으로 정정.
  2. **§3.2 응답 형식 도입부** — "`token`/`draft`/`done`/`error`만 쓴다" 구 문구(v0.4.0 잔재)도 같은 7종으로 정정 (사본 drift 일소).
  3. **§3.2 이벤트 7종 문단** — `chart` → `report` 교체.
  4. **§3.2 통계 Q&A 흐름** — `token → chart(0~1) → done` → `token → report(1회) → done` 으로 갱신.
  5. **§3.2 `chart` 절** — `report` 절로 대체. chart 는 "legacy 폐기·부활 없음" 명시(구 metrics/analysis 선례 문구 재사용).
     `report` 절에 §1.3~§1.4 의 페이로드·필드 표·발생 조건 등재.
  6. 문서 헤더 개정 이력 1줄 추가.

## 2. FE 영향 분석 (2026-08-05 실코드 확인)

### 2.1 현행 FE 소비 구조 (확인 결과)

```
streamChat.ts  : fetch 스트리밍 파서 — `data: {"type","data"}` 청크 → onEvent 콜백 (이벤트명 무해석)
useChat.ts     : onEvent switch — meta/progress/token/conditions/suggestions/products.ready/draft/action/done/error
                 · `chart` 케이스 없음 → 미지 이벤트는 조용히 무시 (default 분기 없음)
                 · done{analysis+replace} 시 마지막 assistant 텍스트를 store.analysisReport 로 승계
store.ts       : lane · progress · analysisReport(string|null) · results(draft 카드)
ChatPage.tsx   : analysisLoading(isStreaming && lane==="analysis") · showResults 파생
SellerWorkspace: showResults 시 AnalysisReport(통짜 텍스트 <p>) + ProductDiffCard 스택
AnalysisChart  : 존재하나 **미배선**(어느 화면에서도 사용 안 함) — series[0]만 렌더, 값 표 병행
```

핸드오프의 두 전제가 실코드로 확정된다:

- **`chart` 폐기 안전**: FE 가 chart 를 소비하지 않는다 → 명세 개정만으로 안전 대체.
- **BE 선배포 무해**: FE 는 미지 이벤트(`report`)를 무시하고, 기존 fallback(token→analysisReport)으로
  현행과 동일하게 렌더한다. **FE 배포 전에 BE 를 먼저 배포해도 사용자 화면은 변하지 않는다.**

### 2.2 FE 계약 fallback (명세에 등재할 규약)

- `report` 수신 → 보관만(버퍼링·조인 불필요) → `done{panel:"replace"}` 에 패널 커밋. `error` 종료 시 폐기.
- `report` 없이 `done{replace}` 가 오면(구버전 BE·미지 이벤트 드랍) 기존처럼 `token` 텍스트를 표시. 미지 이벤트 무시.

### 2.3 FE 수정 지점 (별도 이슈 본문 재료)

| 파일 | 변경 |
|---|---|
| `src/shared/types/chat.ts` | `ChatEvent` 유니온에 `{ type: "report"; data: SellerReport }` 추가. `SellerReport` 인터페이스 신설(§1.3 1:1). `SellerAnalysis.unit` 에 **`"PERCENT"` 추가** ⚠️ (현재 `"KRW"\|"COUNT"` 뿐 — BE 계약은 3종) |
| `src/shared/chat/useChat.ts` | switch 에 `case "report"` — store 에 보관. `done` 핸들러의 analysisReport 승계는 "report 미수신 시에만" 으로 조건 강화 (fallback 유지) |
| `src/shared/chat/store.ts` | `analysisReport: string \| null` → 구조화 리포트 보관으로 확장 (예: `analysisReport: SellerReport \| { body: string } \| null` — fallback 은 body 만 채운 형태로 승계) |
| `src/features/seller/components/AnalysisReport.tsx` | ReportView 로 개편 — 레이아웃(위→아래): 헤더(title + period 배지) → 핵심 요약 카드(summary) → findings[] 카드(severity 배지: info=중립/warning=주황/critical=빨강, evidence 리스트, 조치 힌트) → 종합 해설(body, **접기 기본** — findings 카드와 수치 중복은 의도: 카드=스캔, 산문=맥락) → 데이터 한계 콜아웃(limitations, 있을 때만) → 차트 슬롯 → 추천 행동 번호 카드 + applyGuide(recommendations 비었으면 섹션 미표시) |
| `src/features/seller/components/AnalysisChart.tsx` | **무수정 재사용** (series[0]만 렌더 유지 — 다계열 확장은 BE 스키마 `max_length=1` 완화와 같은 릴리스로만) |
| `src/features/seller/utils/formatMetric.ts` | `PERCENT` 포매터 지원 확인/추가 |
| (선택) 좌측 말풍선 | summary + "→ 우측 보고서" 만 표시하고 전문 접기 — FE 표시 정책(계약 아님), 서버 저장은 전문 그대로라 기능 영향 없음 |

차트 슬롯 규칙: `charts[]` 세로 스택, 비었으면 미렌더, `chartRequested && charts.length===0` 이면 실패 안내 한 줄.

### 2.4 FE 단계 제안 (별도 이슈, BE 배포 후 착수 가능)

1. **F1 — 수신·보관·fallback**: 타입 + `case "report"` + store 확장. 화면은 기존 통짜 텍스트에 body 만 연결 (배포해도 외형 불변).
2. **F2 — ReportView 레이아웃**: 헤더/요약/findings 카드/body 접기/limitations. 여기서부터 사용자에게 보임.
3. **F3 — 차트 슬롯**: AnalysisChart 배선 + `PERCENT` 유닛 + 실패 안내.
4. **F4 (선택) — 좌측 말풍선 슬림 표시.**

## 3. BE 단계별 구현 설계 (커밋 단위 — 각 단계 종료 시 `uv run pytest`·`ruff` 통과 유지)

> 팀 규칙: 계약 변경은 api-spec 개정이 먼저/함께 · 이슈 단위(`Closes #296`) · `dev` 에서 딴 `feat/` 브랜치 ·
> Conventional Commits · 테스트 통과 없이 완료 보고 금지 (CLAUDE.md).

### 단계 0 — 준비 (코드 밖, 사용자)

- **선행(사용자 확정 순서)**: ① `feat/290` 의 CHANGELOG 충돌 마커(머지 커밋 89e13fd) 정리 → ② `feat/290` 을 dev 에
  머지 → ③ Notion 정본에 `report` 계약 등재·FE 팀 합의 → ④ 이슈 등록(BE 1건 + FE 1건 별도) →
  ⑤ dev 최신화 후 `feat/296-report-sse-event` 분기.
- ⚠️ 착수 시 api-spec 최신 버전 재확인(오늘만 3회 개정 — v0.24.0 이 아닐 수 있음).

### 단계 1 — api-spec 개정 (docs only 커밋)

| 항목 | 내용 |
|---|---|
| 파일 | `docs/api-spec.md` |
| 변경 | §1.5 의 6개 지점 (v0.24.0) — **Notion 정본 등재 완료 후** 사본 개정 (선례: v0.21.0 구매자 progress) |
| 커밋 | `docs(api-spec): §3.2 report SSE 이벤트 신설·chart legacy 폐기 (v0.24.0, #296)` |
| 완료 조건 | 코드 무변경 — pytest·ruff 영향 없음 |

### 단계 2 — orchestrator: `PipelineResult` 확장 (와이어 무변경 커밋)

| 항목 | 내용 |
|---|---|
| 파일 | `app/agents/seller/orchestrator.py` (+ 필요 시 `pipeline.py` 에 순수 헬퍼) |
| 변경 | ① `PipelineResult` 에 `findings: list[AnalysisFinding] \| None` · `period: tuple[date, date] \| None` · `chart_requested: bool = False` 추가 (`charts` 는 기존). ② `run_analysis_pipeline` 이 `kind=="report"` 반환 시 세 필드를 채움 (`findings` 는 이미 지역변수로 존재, `period` 는 `resolved.date_from/date_to`, `chart_requested` 는 `resolved.wants_chart`). ③ **summary 분리 순수 함수** `split_report_summary(report: str) -> str` 신설 — §5.1 규칙. LLM·프롬프트·judge·G1 **무변경** (결정 D-4) |
| 헬퍼 위치 | `pipeline.py` (순수 함수 모듈 원칙 — LLM·IO 없음) 권장 |
| 테스트 | `test_seller_orchestrator.py`: report kind 시 신규 필드 채워짐 / clarification·apology·refused 시 기본값(None·False) 유지. `test_seller_pipeline.py`: summary 분리 — 정상 첫 문단 / 빈 줄 없음 / 첫 문단 과장(>300자) / 200자 절단 fallback |
| 완료 조건 | SSE 와이어 바이트 무변경 (기존 `test_seller_api.py` 전건 통과 그대로) |

### 단계 3 — seller.py: `_report_event` 신설 + `chart` 제거 (와이어 변경 커밋)

| 항목 | 내용 |
|---|---|
| 파일 | `app/api/seller.py` |
| 변경 | ① `_report_event(result: PipelineResult) -> str` 신설 — §1.3~§1.4 직렬화. 기존 `_chart_event` 의 charts 직렬화 본문을 **그대로 흡수**. `generatedAt` 은 KST now. ② `_analysis_stream`: `kind=="report"` 일 때 `token` 뒤 `report` 1회 emit — 기존 `chart` 분기(조건 3중 검사)와 `_chart_event`·호출부 **삭제**. `done` panel 분기·`record_turn`·예외 경로 불변. ③ import 정리 (`ChartSet` 직접 참조 제거 가능 여부 확인) |
| 유지 | `pipeline.compose_response` 의 "[차트 안내]" 실패 문구는 **token 용으로 유지** (좌측 채팅 계약 불변, 결정 D-3) |
| 테스트 | `test_seller_api.py` 교체·신설: (a) 순서 `meta→…→token→report→done{replace}` (b) 비 report kind(되묻기·사과·거절) 에 report 미발행 (c) `charts==[]` 여도 report 는 나가고 `charts:[]`·`chartRequested` 정합 (d) **`chart` 이벤트가 어떤 경우에도 안 나감** (기존 chart 테스트 4건 → 이 케이스로 전환) (e) camelCase·마스킹(제어문자·시크릿 각 필드) (f) `recommendations[].index` == 목록 순서(§6.3 정합) (g) findings 에 `chartDataHint` 부재 (h) error 종료 시 report 미발행 (i) 추천 0개 → `recommendations:[]`·`applyGuide:""` / 추천 ≥1 → applyGuide 문구 정합 |
| 완료 조건 | pytest 전건 + ruff. 이 커밋부터 와이어가 v0.24.0 |

### 단계 4 — 마무리 (docs 커밋)

- `CHANGELOG.md` `[Unreleased]` Added/Removed 항목 (api-spec §3.2, v0.24.0 병기).
- 본 설계 문서를 `docs/specs/DESIGN-REPORT-PANEL-<N>.md` 로 확정(이슈 링크).
- PR: `dev` 대상, `Closes #296`, CI(pytest·ruff) 통과.

### 단계 5 — 배포·후속 (범위 밖, 순서만 명시)

1. BE 배포 — `report` 방출·`chart` 중단 (FE 미수신이라 무해, §2.1).
2. FE 구현 F1~F4 (별도 이슈, §2.4) — 여기서부터 사용자에게 보임.
3. (선택, 별도 명세 개정) token 슬림화 — body 중복 전송 해소 / 좌측 말풍선 슬림화.

## 4. 시퀀스 (After)

```
FE                          BE(_analysis_stream)                파이프라인
│ POST /seller/chat          │                                   │
│◄─ meta{lane:"analysis"} ───│                                   │
│◄─ progress ×N ─────────────│◄── emit(진행 token) ──────────────│ planner→branches→report→judge
│                            │◄── PipelineResult ────────────────│ (wants_chart 시 recommend∥graph)
│◄─ token(보고서 전문) ───────│  record_turn                      │
│◄─ report{…charts 내장…} ───│  kind=="report" 일 때만 1회        │
│◄─ done{panel:"replace"} ───│                                   │
│  → 패널 커밋(ReportView)    │                                   │
```

## 5. 세부 설계 결정

### 5.1 summary 분리 규칙 (`split_report_summary`) — [개정 2026-08-05: 마크다운 헤딩 스킵]

REPORT_PROMPT 이 "1. 핵심 요약(2~3문장) 먼저" 를 강제하지만 LLM 산출이라 어길 수 있다 — 코드 fallback 필수.
**실측(2026-08-05 화면 캡처)**: LLM 이 산문 규칙을 어기고 마크다운(`## 핵심 요약` 헤딩)으로 쓰는 사례 확인 —
단순 첫-블록 분리는 헤딩 한 줄을 요약으로 잡는다. 이에 헤딩 스킵을 추가했다.

1. 빈 줄 기준 블록을 앞에서부터 훑되, **`#` 로 시작하는 헤딩 줄은 제거**하고 남는 내용이 있는 **첫 실제 문단**을 후보로.
2. 후보가 비었거나 **300자 초과**(첫 문단 실패 신호)면: 헤딩 제거본 앞 **200자 절단 + "…"**.
3. report 자체가 빈/헤딩뿐이면 "" — degrade 케이스, FE 는 body 로 fallback.

### 5.2 limitations 판정

`finding.evidence == []` 인 finding 의 `summary` 목록. 세 degrade 경로를 모두 자연 포괄한다
(워커 자가 degrade(프롬프트 규약) · 워커 예외 코드 degrade(`_degrade_finding`) · F 잔존 강등). 별도 문자열 매칭("확보 실패") 금지 —
D3 탐지 문자열 의존은 결정론 검사(verifier) 소관이지 와이어 직렬화 기준이 아니다.

### 5.3 재확정하는 기존 결정 (핸드오프 D-1~D-4 계승)

- **D-1 charts 내장 단일 이벤트**: 차트 단독 케이스는 근거 사슬(도구출력⊇finding⊇보고서⊇차트)상 불가 ·
  `asyncio.gather(run_recommend, run_graph)` 병렬이라 분리 이벤트의 타이밍 이점 0 · recommendations 내장 선례 대칭.
- **D-2 chart 폐기**: FE 미소비 실코드 확인(§2.1).
- **D-3 token 현행 유지**: 스레드 기록·이력·후속 맥락의 원천.
- **D-4 LLM 불변**: REPORT_PROMPT·judge·rewrite·G1 무변경, LLM 콜 +0, wall-clock +0.
  (report_agent 구조화 출력 전환은 보류 — 이 계약 위에서 후속 확장 가능.)

## 6. 리스크 · 주의 (구현 세션 체크리스트)

- [ ] **api-spec 버전 재확인** — 본 문서의 v0.24.0 은 2026-08-05 기준. 착수 시점에 최신 버전+1 로 갱신.
- [ ] **`feat/290` 머지 후 착수 (사용자 확정)** — 머지 전 CHANGELOG 충돌 마커(89e13fd) 정리 선행. 머지 후 dev 에서 분기하고, 본 문서의 코드 서술(orchestrator 등)이 머지 결과와 달라진 곳 없는지 한 번 재확인.
- [ ] **`PipelineResult` 소비처 전수 확인** — 필드 추가는 additive 지만 `dataclass(frozen=True)` 생성부(테스트 픽스처 포함)가
  positional 인자를 쓰면 깨질 수 있다 — 신규 필드는 전부 default 있는 keyword 로 추가.
- [ ] **FE `SellerAnalysis.unit` 에 `PERCENT` 없음** — FE 이슈에 반드시 포함 (BE 가 PERCENT 차트를 보내면 현행 FE 타입과 불일치).
- [ ] token+report.body 중복 전송(수 KB)은 1단계 **의도된 허용** — 2단계 token 슬림화로 해소.
- [ ] `context7 MCP` 로 LangGraph/FastAPI API 추측 금지, 작업 전 `docs/lessons.md` 훑기 (CLAUDE.md 하네스).
- [ ] 테스트 없이 완료 보고 금지 — 각 단계 커밋 전 `uv run ruff check --fix && uv run ruff format` → `uv run pytest`.

## 7. 완료 조건 (BE 이슈)

1. api-spec §3.2 개정 커밋이 코드 변경보다 선행/동반.
2. `report` 가 계약(§1.3~§1.4)대로 방출: report kind 정확히 1회 · 비 report kind 0회 · error 종료 0회.
3. `chart` 이벤트가 어떤 경로에서도 방출되지 않음.
4. camelCase·마스킹·추천 N번 정합·charts 빈 배열 허용이 테스트로 고정.
5. `uv run pytest` · `ruff` 통과, CHANGELOG 갱신, 본 설계 문서 repo 반영.
