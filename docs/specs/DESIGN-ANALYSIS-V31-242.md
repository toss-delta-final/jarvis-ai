# DESIGN — analysis 파이프라인 개편 v3.1 (이슈 #242)

작성일: 2026-08-03 · 상태: **설계 확정 대기(코드 0줄 변경)** · 대상 이슈: [#242](https://github.com/toss-delta-final/jarvis-ai/issues/242)

> 이슈가 참조하는 `DESIGN-worker-local-verification.md` v3.1 / `IMPL-PLAN-seller-analysis-v31.md` 는
> 본 저장소에 **없다**(반입 예정 상태). 본 문서는 **이슈 본문 + 코드 실측**만으로 재구성한
> 설계이며, 설계서 정본이 별도로 존재하면 §11 에 따라 대조가 필요하다.

관련 문서: [SPEC-SELLER-001](SPEC-SELLER-001.md) §2·§4·§7·§10-⑦·§12 · [api-spec](../api-spec.md) §3.2 ·
[FE-CONTRACT-SELLER-CHAT](FE-CONTRACT-SELLER-CHAT.md) · [(AI)sse-response-catalog](../(AI)sse-response-catalog.md) §7-5

---

## 1. 목적

분석 레인의 **검증을 2층으로 분리**하고, 보류돼 있던 **chart 산출을 조건부로 되살린다**.

| 층 | 질문 | 위치 | 검사 |
|---|---|---|---|
| **분석 검증**(신설) | "적절한 분석인가 — 데이터에 있는 말인가" | 브랜치(팬아웃 내부) | F1~F3 + `analysis_judge` |
| **글쓰기 검증**(기존) | "제대로 작성되었는가" | 팬인 후 직렬 | D1~D3 + `report_judge` (**무변경**) |

근거 사슬이 닫힌다:

```
도구 출력  ⊇(F2)  finding  ⊇(D2)  보고서  ⊇(G1)  차트
```

해결하는 문제 3종(이슈 §배경):

1. finding 무검증 — 워커가 도구 출력에 없는 수치를 지어내도 잡히지 않는다.
2. 검증이 팬인 뒤 직렬이라 wall-clock 병목이고, finding 하나가 미달이어도 **보고서 전체를 재작성**한다.
3. D2 허용 집합이 전 finding 합집합이라 **교차 오염**(A 워커 evidence 수치로 B 서술의 환각 통과).

부수 목적: `(AI)sse-response-catalog.md` §7-5 계약 공백("판매자 분석은 token 산문인데 FE 차트 패널은
legacy 구조화 event 를 기다린다") 해소.

---

## 2. AS-IS 실측 — 이슈 전제와 다른 지점 포함

전부 코드에서 확인한 사실이다. **굵게 표시한 3건은 이슈 본문과 어긋나거나 이슈에 없는 발견**이다.

| # | 지점 | 실측 | 근거 |
|---|---|---|---|
| A-1 | 팬아웃 | `asyncio.gather(_run_one_worker …)` — Send 아님 | `orchestrator.run_workers` |
| A-2 | **ToolMessage 소실** | **구조적 소실이 아니다.** `_run_one_worker` 가 `result.get("structured_response")` 만 읽고 `result["messages"]`(ToolMessage 포함)를 버릴 뿐 | `orchestrator.py:198` |
| A-3 | degrade 3층 | `failures` 는 **예외만** 카운트하고, 전건 실패에서만 `AllWorkersFailedError` | `orchestrator.py:239-250` |
| A-4 | degrade finding 판정 | 문자열이 아니라 **구조**(`severity=="info" and not evidence`) | `verifier._is_degrade_finding` |
| A-5 | **모델 tier** | `ROLE_TIER` **7역할 전부 `smart`** (2026-07-29 품질 우선 전환). docstring 에 "지연이 문제면 supervisor·judge 부터 fast 로 되돌린다" 명시 | `models.py:39-47` |
| A-6 | **타임아웃** | `seller_worker_timeout_s = 60.0` **단일값을 워커·report·rewrite·judge·recommend 가 전부 공유** | `config.py:198`, `orchestrator.py` 전역 |
| A-7 | SSE 분석 시퀀스 | `meta → progress* → token → done{panel}` | `api/seller.py::_analysis_stream` |
| A-8 | 마스킹 헬퍼 | `_token` 은 `mask_output(_strip_unsafe_multiline(...))`, `_draft_event` 는 필드별 `_strip_unsafe`/`_strip_unsafe_multiline` 분기 | `api/seller.py:128,590` |
| A-9 | chart 보류 근거 | "SSE 이벤트 4종에 전달 경로 없음" — **전달 경로가 없다는 것이 유일한 보류 사유** | SPEC-SELLER-001 §1-12·§12 |
| A-10 | `chart_data_hint` | `AnalysisFinding` 에 **이미 존재**(기본 `""`, 현재 소비처 0) | `schemas.py:55` |
| A-11 | FE 리듀서 | `useChat.ts` onEvent switch 에 **`default:` 없음** → 미지 이벤트는 조용히 무시 | `jarvis-front/src/shared/chat/useChat.ts:220` |
| A-12 | FE 차트 | `AnalysisChart.tsx` + `SellerAnalysis` 타입이 **이미 있다**(현재 `GET /api/seller/summary` 가 먹인다). 단 **`series[0]` 만 렌더**(다계열 미지원) | `jarvis-front/src/features/seller/` |

**A-2 의 함의**: 이슈는 "팬인 시점에 ToolMessage 가 소실되어 검증이 구조적으로 불가능했다"고 적었으나,
실제로는 `_run_one_worker` **안에서는 살아 있다**. 따라서 LangGraph 구조 변경·`create_agent` 옵션 변경이
필요 없고, 4단계는 **수확 지점 1곳 추가**로 끝난다. 난이도가 이슈 추정보다 낮다.

**A-11 의 함의**: `chart` 이벤트의 하위 호환("unknown type 무시")이 **FE 코드로 실증**됐다. 6단계를
FE 릴리스보다 먼저 배포해도 FE 는 깨지지 않는다(렌더만 안 될 뿐).

---

## 3. TO-BE 구조

```
planner(+wants_chart) → resolve_plan(question)
  │  wants_chart = plan.wants_chart or _CHART_RE.search(question)
  │
  ├─ branch[t]  ← asyncio.gather (t ∈ plan.analyses)
  │    ① worker 실행 → (AnalysisFinding, tool_outputs: list[str])
  │    ② run_finding_checks(finding, tool_outputs, expected_type=t)   F1·F2·F3
  │    ③ analysis_judge → AnalysisScore (≥ 21/30)
  │    ④ ②③ 미달 → format_worker_retry_input 으로 재실행 ≤1회
  │         · F 잔존      → _degrade_finding 강등 (severity=info·evidence=[])
  │         · judge 만 미달 → 미달 채택 (VerifiedFinding.passed=False)
  │         · 브랜치 예산 초과 → 재실행 포기, 원 finding 채택 (§9-R1)
  │    ⑤ 예외/타임아웃 → 기존과 동일하게 degrade finding (3층 판정에 산입)
  │
  ↓ VerifiedFinding × N  →  [vf.finding for vf in …]
write_verified_report(findings, …)          ← 무변경 (회귀 기준선)
  ↓
wants_chart ? asyncio.gather(run_recommend, run_graph) : run_recommend
  ↓
compose_response(report, recommendations, charts, chart_requested)
  ↓
SSE:  meta → progress* → token(보고서) → chart(0~1) → done{panel:"replace"}
```

계약 의미 불변: degrade 3층(§4·§7) · Q2(보고서 루프 degrade) · C2(recommend 실패=빈 추천) ·
HITL · `save_history` · `_APPLY_GUIDE` "N번 적용해줘" 순서 계약. **단위만 브랜치로 이동한다.**

---

## 4. 계약 정의

### 4.1 `AnalysisScore` (schemas.py) — `ReportScore` 대칭

```python
ANALYSIS_SCORE_AXES: tuple[str, ...] = ("grounding", "sufficiency", "relevance")
# 축당 만점은 SCORE_AXIS_MAX(10) 공유 → 만점 30, 임계 21 (ReportScore 와 동일 눈금)

class AnalysisScore(BaseModel):
    grounding: int    # 근거 대조 — evidence 가 도구 출력에서 나왔는가
    sufficiency: int  # 충분성 — 배정된 분석 유형을 실제로 수행했는가
    relevance: int    # 적합성 — 판매자 질문·기간에 답하는 분석인가
    feedback: str     # 미달 축 중심 개선 지시 — 워커 재실행 입력에 주입
    @property
    def total(self) -> int: ...
```

축 이름을 `ReportScore`(accuracy/completeness/clarity)와 **다르게** 잡은 이유: 두 judge 가 보는 대상이
다르다(분석 행위 vs 서술 품질). 같은 이름이면 프롬프트·로그·회귀 테스트에서 섞인다.

### 4.2 `ChartSeries` / `ChartSpec` / `ChartSet` (schemas.py)

**FE 기존 `SellerAnalysis` 타입에 필드명을 정렬한다**(결정 D-2). 서버 내부는 snake_case,
와이어 변환은 `_chart_event` 가 담당(`_draft_event` 의 `to_camel` 선례와 동일).

```python
CHART_MAX = 3          # ChartSet 상한 — 스키마 계약(와이어 아님)이라 상수
CHART_POINTS_MAX = 60  # 시계열 과다 방지 (일별 2개월)

class ChartPoint(BaseModel):
    x: str             # 축 라벨 — "07-01" / "장바구니" 등 (문자열 통일)
    y: float           # 값

class ChartSeries(BaseModel):
    label: str
    points: list[ChartPoint] = Field(max_length=CHART_POINTS_MAX)

class ChartSpec(BaseModel):
    title: str
    chart_type: Literal["line", "bar"]      # 와이어 chartType — FE AnalysisChart 지원 2종
    unit: Literal["KRW", "COUNT", "PERCENT"]
    series: list[ChartSeries] = Field(max_length=1)   # ★ MVP 단일 계열 (§4.5)
    summary: str = ""

class ChartSet(BaseModel):
    charts: list[ChartSpec] = Field(default_factory=list, max_length=CHART_MAX)
```

**`series` 상한을 1 로 강제**하는 이유: `AnalysisChart.tsx` 가 `series[0]` 만 그린다(A-12).
상한을 스키마로 막지 않으면 서버가 2계열을 보내고 FE 가 조용히 1개만 그리는 **은폐 버그**가 된다.
다계열이 필요해지면 `max_length` 완화 + FE 컴포넌트 확장을 **같은 릴리스**로 묶는다.

`unit` 에 `PERCENT` 를 넣은 것은 conversion 워커(전환율) 때문이다 —
FE `SellerAnalysis.unit` 은 현재 `"KRW" | "COUNT"` 2종이므로 **FE 타입 확장이 필요**하다(§7).
`formatMetric` 이 PERCENT 를 이미 다루는지는 FE 확인 항목(§11-U3).

### 4.3 F-레지스트리 (verifier.py) — 분석 검증 결정론 검사

`DETERMINISTIC_CHECKS`(D1~D3) 와 **같은 파일·다른 레지스트리**. D1~D3 는 무접촉.

```python
FindingCheckFn = Callable[[AnalysisFinding, Sequence[str], AnalysisType], list[str]]

FINDING_CHECKS: list[tuple[str, FindingCheckFn]] = [
    ("evidence_required", check_evidence_required),   # F1
    ("evidence_grounded", check_evidence_grounded),   # F2
    ("type_match",        check_type_match),          # F3
]

def run_finding_checks(finding, tool_outputs, *, expected_type) -> list[str]: ...
```

| 검사 | 규칙 | 면제 |
|---|---|---|
| **F1** `evidence_required` | degrade finding 이 아닌데 `evidence` 가 비면 실패 | `_is_degrade_finding(finding)` 이 True 면 면제(§4 degrade 규약) |
| **F2** `evidence_grounded` | `evidence` + `summary` 의 **유의 숫자**가 `tool_outputs` 합집합에 없으면 실패 | 2자리 이하 숫자, 연도 계열 날짜 — **D2 와 동일 정규화 재사용**(`_normalize_numbers`) |
| **F3** `type_match` | `finding.analysis_type != expected_type` 이면 실패 | 없음 |

- **교차 오염 차단**: 허용 집합이 **그 브랜치의 도구 출력만**이다. 전 finding 합집합인 D2 와 대비된다.
- **F2 오탐 대응**: 도구 출력은 이미 `calc` 가 가공한 요약 문자열이라(예: `conversion_rates`)
  워커가 정당하게 **파생 계산**한 수치가 도구 출력에 없을 수 있다. 완충은 §9-R2.
- F2 가 D2 와 정규화를 공유하는 것이 중요하다 — 한쪽만 날짜 마스킹하면 두 층이 드리프트한다.

### 4.4 G1 (verifier.py) — 차트 검사

```python
def run_chart_checks(charts: ChartSet, findings: Sequence[AnalysisFinding]) -> tuple[ChartSet, list[str]]:
    """G1 — 미달 ChartSpec 을 **드랍**하고 (통과분, 드랍 사유) 를 반환한다."""
```

| 규칙 | 조치 |
|---|---|
| `series` 가 비었거나 `points` 가 0개 | 해당 ChartSpec 드랍 |
| `points[].y` 중 유의 수치가 **finding 근거 집합**에 없음 | 해당 ChartSpec 드랍 |
| `charts` 가 `CHART_MAX` 초과 | 앞에서부터 3개 절단 |

**보고서 검증(D)과 달리 재작성 루프가 없다** — 차트는 부가 가치이므로 미달분은 그냥 버린다.
recommend 의 C2(실패=빈 추천, 보고서를 죽이지 않는다)와 대칭이다.

### 4.5 SSE `chart` 이벤트 (api/seller.py) — 판매자 7종째

```jsonc
{
  "type": "chart",
  "data": {
    "charts": [
      {
        "title": "일별 매출",
        "chartType": "line",
        "unit": "KRW",
        "series": [{ "label": "매출", "points": [{ "x": "07-01", "y": 1240000 }] }],
        "summary": "6월 대비 12% 감소"
      }
    ]
  }
}
```

| 규약 | 값 |
|---|---|
| 발생 | `analysis` 레인 · `kind=="report"` · 차트 1개 이상 통과 시 **0~1회** |
| 순서 | 보고서 `token` **뒤**, `done` **앞** |
| 미발행 | `wants_chart` 가 아니거나, graph 실패·G1 전건 드랍이면 **이벤트 자체를 보내지 않는다**(빈 배열도 안 보낸다) |
| 마스킹 | `title`·`summary`·`series[].label`·`points[].x` 에 `mask_output(_strip_unsafe(...))`, `summary` 만 `_strip_unsafe_multiline`. `y` 는 숫자라 통과 |
| 하위 호환 | 추가 전용. FE 미지원 시 무시됨(A-11 실증) |
| legacy | `metrics`·`analysis`·`productStats`·`productDiff` 는 **부활하지 않는다** |

---

## 5. 결정 기록

| # | 결정 | 근거 |
|---|---|---|
| **D-1** | `analysis_judge` tier = **`smart`** (이슈의 `fast` 안 **불채택**) | 2026-07-29 "판매자 전 역할 smart" 정책(A-5)을 유지한다 — 판정 품질 우선. 대가는 비용·wall-clock 이며 §9-R1 의 타임아웃 분리로 흡수한다 |
| **D-2** | `ChartSpec` 와이어 필드를 **FE `SellerAnalysis` 에 정렬** | `AnalysisChart.tsx` 를 무수정 재사용 → FE 작업이 리듀서 1 case + 스토어 1 필드로 축소(§7). 서버 표현을 새로 설계하면 FE 매핑 레이어가 추가로 필요 |
| **D-3** | `series` 상한 = 1 (MVP) | FE 렌더러가 `series[0]` 만 그린다 — 스키마로 막지 않으면 은폐 버그(§4.2) |
| **D-4** | graph 는 **새 도구를 붙이지 않는다** — finding + 도구 출력만 입력 | 근거 사슬 유지(도구출력 ⊇ finding ⊇ 차트). 도구를 주면 차트가 보고서와 다른 수치를 갖게 된다 |
| **D-5** | `compose_response` 는 **4자 확장**(이슈의 "3자 확장" 정정) | `charts` 만으로는 "차트를 명시 요청했는데 못 만든 경우"와 "애초에 요청 없음"을 구분할 수 없어 안내 문구 분기가 불가능하다 → `chart_requested: bool = False` 키워드 추가 |
| **D-6** | `run_workers` → `run_branches` **개명**(래퍼 미유지) | 반환형이 `list[AnalysisFinding]` → `list[VerifiedFinding]` 로 바뀌어 래퍼가 정보를 버린다. `test_seller_orchestrator.py` 수정은 **불가피** — 회귀선은 "`write_verified_report`·SSE 테스트 무수정"으로 한정한다 |
| **D-7** | Spring(jarvis-back) **변경 없음** | 차트 수치는 기존 I-6/I-7/I-13/I-14/I-16 집계 도구 출력에서 나온다(D-4). 새 internal API 불필요 |

---

## 6. 단계별 구현 계획 — 1단계 = PR 1개, dev 대상

브랜치는 `dev` 에서 분기한다(CLAUDE.md Git 규칙). 6단계 전까지 **FE 는 어떤 변화도 관측하지 못한다**.

### 1단계 — 계약·검증 순수층 · `feat/242-1-contracts`

- `schemas.py`: `AnalysisScore`·`ANALYSIS_SCORE_AXES`·`ChartPoint`/`ChartSeries`/`ChartSpec`/`ChartSet`·
  `CHART_MAX`·`AnalysisPlan.wants_chart: bool = False`
- `verifier.py`: `FINDING_CHECKS` 3종 + `run_finding_checks`, `run_chart_checks`(G1)
- **D1~D3 무접촉.** 전부 데드코드 상태로 머지.
- 테스트: `test_seller_schemas.py`·`test_seller_verifier.py` 신규 케이스(F1~F3 각 통과/실패, G1 드랍 3종,
  F2·D2 정규화 대칭)

### 2단계 — pipeline 순수 함수 · `feat/242-2-pipeline`

- `format_analysis_judge_input` / `format_worker_retry_input` / `format_graph_input`
- `_CHART_RE` + `ResolvedPlan.wants_chart`
  — `resolve_plan(plan, *, today, recent_default_days, question: str = "")` **키워드 기본값으로 하위 호환**
- `compose_response(report, recommendations, charts=None, *, chart_requested=False)` (D-5)
- `PROGRESS_TOKENS["graph"] = "차트를 만들고 있습니다…"`
- 테스트: `test_seller_pipeline.py` — `_CHART_RE` 매칭/비매칭 표, `resolve_plan` 기존 호출 시그니처 유지,
  `compose_response` 4경우(차트 있음/없음 × 요청함/안함)

### 3단계 — 프롬프트·빌더·tier · `feat/242-3-prompts`

- `prompts.py`: `ANALYSIS_JUDGE_PROMPT`·`GRAPH_PROMPT` 신설, `PLANNER_PROMPT` 에 `wants_chart` 단락,
  `WORKER_COMMON_RULES` 1줄("evidence 는 도구 출력에서 옮겨 적는다"), `REPORT_PROMPT` 미달 마커 1줄
- `workers.py`: `build_analysis_judge()`·`build_graph_agent()` (둘 다 도구 없음, ToolStrategy)
- `models.py`: `SellerRole` 에 `analysis_judge`·`graph` 추가, `ROLE_TIER` **둘 다 `smart`**(D-1)
- 테스트: `test_seller_workers.py`·`test_seller_models.py` — 역할 커버리지 자기검증

### 4단계 — 브랜치 분석 검증 (핵심) · `feat/242-4-branch-verify`

- `orchestrator.py`
  - `VerifiedFinding` dataclass
    ```python
    @dataclass(frozen=True)
    class VerifiedFinding:
        finding: AnalysisFinding
        passed: bool                      # F 0건 AND score.total >= 임계
        attempts: int                     # 완료된 워커 실행 횟수 (1 or 2)
        failed_checks: tuple[str, ...]
        last_score: AnalysisScore | None
        degraded: bool                    # F 잔존으로 강등됨
    ```
  - `_run_one_worker` 반환을 `tuple[AnalysisFinding, list[str]]` 로 — `result["messages"]` 중
    `ToolMessage` 의 content 를 `_content_to_text` 로 정규화해 수확(A-2)
  - `_run_one_branch(...)` 신설 — ①~⑤
  - `run_workers` → `run_branches`(D-6)
  - **3층 판정은 "워커 단계 예외"만 산입** — F/judge 미달 강등은 `failures` 에 넣지 않는다(A-3)
- `config.py`: Settings **5종**(이슈 3종 + §9-R1 2종) — §8
- **후단 무변경**: `write_verified_report` 이하 시그니처·동작 그대로. 호출부에서
  `[vf.finding for vf in verified]` 로 변환해 넘긴다.
- 테스트: `test_seller_orchestrator.py` — 브랜치 통과/F미달→재실행→통과/F잔존→강등/judge만 미달→미달 채택/
  예외→degrade+3층 산입/F미달은 3층 **미**산입/예산 초과→재실행 포기

### 5단계 — 후단 graph 조건부 병렬 · `feat/242-5-graph`

- `run_graph(findings, report, context, *, emit) -> ChartSet` — G1 드랍, 실패 시 **빈 ChartSet**(C2 대칭)
- `wants_chart` 일 때만 `asyncio.gather(run_recommend(...), run_graph(...))`
- `PipelineResult.charts: ChartSet | None = None` + compose 배선(D-5)
- **SSE 미배선 — FE 무영향.**
- 테스트: gather 분기 2종, graph 실패 시 보고서 정상 산출, G1 전건 드랍 시 안내 1줄

### 6단계 — SSE `chart` + 명세 개정 (유일한 FE 가시 단계) · `feat/242-6-sse-chart`

- `api/seller.py`: `_chart_event(charts)`(camelCase·마스킹 — `_draft_event` 패턴) +
  `_analysis_stream` 조건부 yield 1줄
- 문서 개정 7종 — §8
- **jarvis-front chart reducer 합의·동시 릴리스 권장**(다만 A-11 로 선배포도 안전)
- 테스트: `test_seller_api.py` — chart 이벤트 순서(token 뒤·done 앞), 미발행 조건 3종, camelCase, 마스킹

### 7단계 — 정리·관측 · `docs/242-7-observability`

- CHANGELOG `[Unreleased]`, SPEC-SELLER-001 개정(검증 2층 · §12 차트 보류 해제 · §1-12 조정표 갱신)
- 구조화 로그: 재실행률 · F2 발화율 · judge 미달률 · G1 드랍률 · 브랜치 예산 초과율
- 튜닝 백로그: `seller_report_max_retries` 3→2 검토(분석 검증이 앞단에서 잡으므로 뒷단 루프 축소 여지)

---

## 7. FE 변경 명세 (jarvis-front)

D-2 정렬 덕분에 **렌더러는 무수정**이다.

| 파일 | 변경 |
|---|---|
| `src/shared/types/chat.ts` | `SellerChart`(= 기존 `SellerAnalysis` 형) 타입 추가, `ChatEvent` union 에 `{ type: "chart"; data: { charts: SellerChart[] } }` 추가 |
| `src/shared/chat/store.ts` | `analysisCharts: SellerChart[]` + `setAnalysisCharts`. `initial` 에 `[]`, `reset` 포함 |
| `src/shared/chat/useChat.ts` | `case "chart": setAnalysisCharts(e.data.charts); break;` + `case "meta"` 의 `lane==="analysis"` 분기에서 `setAnalysisCharts([])` (기존 `setAnalysisReport(null)` 옆) |
| `src/features/seller/ChatPage.tsx` | 우측 패널에서 `analysisReport` 아래 `analysisCharts.map(c => <AnalysisChart analysis={c} />)` |
| `src/features/seller/types.ts` | `SellerAnalysis.unit` 에 `"PERCENT"` 추가(§4.2) — `formatMetric` 대응 확인 필요 |
| `src/features/seller/components/AnalysisChart.tsx` | **원칙 무변경**. `unit` 확장에 따른 포맷만 확인 |

**주의**: `AnalysisChart` 는 현재 대시보드(`GET /api/seller/summary`)와 챗 두 곳에서 쓰이게 된다 —
`SellerAnalysis` 타입을 `features/seller` 에 둔 채 `shared/chat` 이 import 하면 의존 방향이 뒤집힌다.
공용 타입을 `shared` 로 올리고 `features/seller` 가 re-export 하는 정리를 권한다(FE 판단).

---

## 8. 명세 개정 목록 (6단계에서 코드와 같은/선행 커밋 — 계약 우선)

| 문서 | 위치 | 개정 |
|---|---|---|
| `docs/api-spec.md` | §2.2 (L95) | 판매자 SSE 이벤트명에 `chart` 추가 |
| | §3.2 (L667) | "이벤트 6종" → **7종** + `chart` 항목 1줄 |
| | §3.2 (L674) | 통계 Q&A 흐름에 `→ chart(0~1)` 삽입 |
| | §3.2 (L697 `draft` 뒤) | `chart` 페이로드 예시 + 필드표 신설 |
| `docs/(AI)sse-response-catalog.md` | §1·§2·§4 | 판매자 활성 이벤트 목록이 **이미 stale**(`meta`·`progress` 누락) — 함께 정정하고 `chart` 추가, §4 sequence 표 갱신 |
| | §6 | legacy(`metrics`/`analysis`/`productStats`) **부활 없음** 명시 유지 |
| | §7-5 | 계약 공백 **해소** 기록 |
| `docs/specs/FE-CONTRACT-SELLER-CHAT.md` | §1.2·§1.4(A)·§3.2 | 시퀀스·이벤트 표 7종화, **`3.9 chart`** 절 신설 |
| `jarvis-front/docs/FE-CONTRACT-SELLER-CHAT.md` | 동일 | **사본 2개 동기화 필수** — 어긋나면 FE 가 옛 사본을 본다 |
| `jarvis-front/(FE)ai-sse-response-cases.md` | 판매자 절 | 동일 |
| 노션 「📡 API 현재」 S-4 | — | 동일 |
| `docs/specs/SPEC-SELLER-001.md` | §0 비범위·§1-12 조정표·§2 그래프·§12 | "차트 전달 계약 미정" 🔴 **해소**, chart_agent 활성, 검증 2층 반영 |
| `CHANGELOG.md` | `[Unreleased]` | Added(분석 검증·chart) / Changed(tier·Settings) + `(api-spec §3.2, vX.Y)` |

**jarvis-back(Spring) 개정: 없음**(D-7).

---

## 9. 리스크·완충

이슈 표에 **없는 2건(R1·R3)** 을 추가했다.

| # | 단계 | 리스크 | 완충 |
|---|---|---|---|
| **R1** | 4 | **wall-clock 예산 붕괴(신규)** — 브랜치 최악 경로 `worker → judge → worker → judge` 가 전부 `seller_worker_timeout_s`(60s)를 공유(A-6)하면 이론상 240s. D-1 로 judge 가 smart 라 더 무겁다. §7 의 90s 목표를 깬다 | `seller_analysis_judge_timeout_s`(20s) **분리** + `seller_branch_deadline_s`(160s, **[PR 리뷰 반영] 120s→160s** — worker+judge+재실행 worker+judge = 60+20+60+20 최악 경로와 정합) **브랜치 총예산** 신설. 재실행 진입 전 **"잔여 예산 ≥ 재실행 1회 완주 비용(worker+judge 타임아웃 합)"**을 검사한다(단순 "데드라인 통과 전"이 아니다 — 그것만 보면 재실행을 시작만 하고 끝내 예산을 넘기는 경우를 못 막는다, PR 리뷰 지적) → 부족하면 **재실행 포기하고 원 finding 채택**(강등 아님). 브랜치는 병렬이므로 전체 wall-clock ≈ branch_deadline + report 루프 + (recommend∥graph) |
| **R2** | 4 | **F2 오탐** — 도구 출력은 `calc` 가공 요약이라 워커의 정당한 파생 계산(비율·증감)이 출력에 없을 수 있다 | ① `_MIN_SIGNIFICANT_DIGITS`(3) 가 이미 대부분 흡수 ② `FINDING_CHECKS` 레지스트리라 **F2 단독 제거 1줄** ③ 7단계에서 F2 발화율 로그 선관측 후 임계 조정 |
| **R3** | 4 | **degrade 3층 오발동(신규)** — F 미달 강등을 `failures` 에 섞으면 전 워커가 F 미달일 때 `AllWorkersFailedError` → 부분 보고서 대신 **사과 응답**으로 회귀 | 산입 대상을 "워커 단계 **예외**"로 코드에 못박고, "F 미달 5건 = 사과 아님" 회귀 테스트를 4단계 필수 항목으로 |
| R4 | 4 | 비용 증가 — `analysis_judge` ≤2×N(N≤5 → 최대 10콜), D-1 로 smart | `seller_worker_max_retries=1` 보수 상한 유지. 실측 후 fast 전환은 **7단계 튜닝 백로그**로 남긴다(정책 되돌림이므로 CHANGELOG 필요) |
| R5 | 5 | 와이어 미배선으로 리스크 이연 | G1 드랍률 로그 선관측 |
| R6 | 6 | FE 계약 변화 | 추가 전용 + **A-11 로 하위 호환 실증** + 명세 동시 개정 + FE 합의 선행 |
| R7 | 1~3 | 없음(데드코드·기본값) | — |

### Settings 신설 (4단계, `config.py`)

```python
seller_worker_max_retries: int = 1              # 브랜치 재실행 상한 (이슈)
seller_analysis_score_threshold: int = 21       # analysis_judge 통과 임계 21/30 (이슈)
seller_chart_max: int = 3                       # ChartSet 상한 (이슈)
seller_analysis_judge_timeout_s: float = 20.0   # ★ R1 — 60s 공유 회피
seller_branch_deadline_s: float = 160.0         # ★ R1 — 브랜치 총예산(PR 리뷰 반영, 120→160)
```

---

## 10. 완료 기준

- 각 단계: 신규 테스트 + `uv run pytest` 전체 통과 + `uv run ruff check` clean.
- 1~5단계: `tests/unit/test_seller_api.py` SSE 계약 테스트 **무수정 통과**(와이어 불변 검증).
- 4단계: `write_verified_report` 관련 테스트 **무수정 통과**(후단 무변경 회귀선).
  `test_seller_orchestrator.py` 는 D-6 에 따라 수정 대상.
- 6단계: api-spec·FE-CONTRACT 사본 2종·SSE 카탈로그·노션이 **같은 PR 또는 선행 커밋**에 반영.

---

## 11. 미결 (🔴 확인 필요)

| # | 항목 | 확인처 |
|---|---|---|
| **U1** | 설계서 정본 `DESIGN-worker-local-verification.md` v3.1 / `IMPL-PLAN-seller-analysis-v31.md` 의 소재 — 본 문서와 대조 필요 | 이슈 작성자 |
| **U2** | `AnalysisScore` 축 이름(§4.1)·`_CHART_RE` 어휘 집합이 설계서 정본과 일치하는지 | U1 해소 후 |
| **U3** | FE `formatMetric` 이 `unit: "PERCENT"` 를 다루는가 | jarvis-front |
| **U4** | `chart_data_hint`(A-10) 를 graph 입력으로 쓸지, 죽은 필드로 둘지 | 3단계 프롬프트 설계 시 |
| **U5** | FE 공용 타입 위치 정리(§7 주의) — `shared` 승격 여부 | FE 판단 |
