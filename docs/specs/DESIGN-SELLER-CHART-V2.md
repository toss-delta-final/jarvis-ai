# DESIGN-SELLER-CHART-V2 — 판매자 분석 차트 재설계 (이슈 #504)

2026-08-09 · 정본: 노션 「📊 판매자 분석 차트 재설계 (08/08)」 · api-spec §3.2 v0.30.0
관련: #242(분석 검증 v3.1, 구 G1·D-4) · #296(리포트 패널 — `report` 이벤트) · #297(리뷰 분석) ·
#345/#346(기간 어휘·비교 기간) · #489(I-6 `salesCount`·I-13 `salesQuantity` 수신 정합)

## 1. 문제 — 차트가 사실상 한 번도 안 나온다

구 구조(#242 5단계)에서 `graph_agent` 는 도구 없이(결정 D-4) 워커 finding 요약 + 검증된
보고서 텍스트를 입력으로 받아 **좌표까지 LLM 이 만들었다**. 요약 3~5줄에는 온전한 시계열이
없으므로 없는 날짜·값은 지어낼 수밖에 없고, 지어낸 값은 G1(`verifier.run_chart_checks`)의
근거 대조에서 전건 드랍됐다. 결과가 상시 `charts: []` — 구조적으로 실데이터 좌표를 만들
경로가 없었다.

## 2. 결정 — 좌표 생성 주체를 LLM → 코드로 (구 D-4 폐기)

| 구 결정 (#242) | 본 설계 (#504) |
|---|---|
| D-4: graph 에 도구를 주지 않는다 — 근거 사슬(도구출력⊇finding⊇보고서⊇차트) 유지 | **폐기.** graph LLM 은 **축 선언만** 하고(`ChartPlanSet`), 좌표는 `charts.py` 가 Spring(I-6·I-13·I-9·I-31)을 직접 호출해 조립한다. LLM 이 수치에 관여하지 않으므로 "환각 좌표" 위협 모델 자체가 사라진다 |
| G1: 차트 수치를 finding 텍스트와 대조해 드랍 | **삭제.** 검사 대상(LLM 산 좌표)이 소멸 — 좌표는 Spring 집계 응답의 값 그대로다 |
| 보고서 수치 ⊇ 차트 수치 | **완화.** 원천은 같은 Spring 집계라 정합하되, 버킷 합산·제로필 값은 보고서 산문에 그대로 등장하지 않을 수 있다(의도된 동작 — api-spec §3.2 근거 사슬 노트) |

Spring(jarvis-back) 변경은 없다(구 D-7 유지) — I-6 `salesCount`·I-13 `salesQuantity` 는
#489 에서 수신 정합이 끝나 있고, 호출 주체만 LLM 도구 경유 → 코드 직접 호출로 바뀐다.
graph 역할이 "좌표 생성"에서 "축 선택"으로 줄어 LLM 부담·wall-clock 도 감소 방향이다.

## 3. 구조

```
run_graph (orchestrator.py)
 ├─ resolved.chart_period_error ? → chartUnavailable(chart_period_unclear), LLM 콜 0회
 ├─ graph_agent(LLM, ToolStrategy(ChartPlanSet)) — 축 선언 ≤3, 좌표 없음
 │    실패 → chartUnavailable(agent_failed)
 └─ charts.build_charts(plans, brand_id, chart_from|date_from, chart_to|date_to)
      ├─ (x,y) ∉ 레지스트리 · "other" → unsupported_axes(지원 목록 포함)
      ├─ Spring 조회 실패 → 해당 차트만 source_failed (부분 성공 허용)
      ├─ 기간 내 0건·전부 null → no_data
      └─ 성공 → ChartSpec(unit·chartType·aggregate 는 레지스트리가 확정)
 → (ChartSet, list[ChartUnavailable]) — 예외 불전파(C2 대칭, 보고서 불사)
```

- **소스 레지스트리** `charts.CHART_SOURCES`: `(x_axis, y_axis)` → 제목·unit·chartType·
  aggregate·needs_period·범례. **14조합의 단일 출처** — 늘릴 때 `GRAPH_PROMPT` 의 지원
  목록과 `_SUPPORTED_SUMMARY` 안내 문구를 같이 갱신한다(테스트가 14개 고정을 단언).
- **조회 공유** `_FetchCache`: 한 턴 안에서 같은 소스(예: I-6)를 쓰는 차트들은 조회 1회.
- **문구 소유권**: `chartUnavailable.message` 완성 문장은 charts.py 가 만든다 — FE 는
  reason 분기 없이 그대로 렌더(사유 어휘는 개방형, `progress.stage` 규약과 동일).

### 14조합 (unit / chartType / aggregate)

| x | y | 원천 | unit | type | agg |
|---|---|---|---|---|---|
| date | sales · sales_quantity · order_count | I-6 daily | KRW·COUNT | line | sum |
| date | view · cart | I-13 groupBy=date | COUNT | line | sum |
| product | sales_quantity · view · cart | I-13 groupBy=product | COUNT | bar | sum |
| product | review_count · avg_rating | I-31 stats byProduct | COUNT·**RATING** | bar | sum·**avg** |
| product | price · stock | I-9 (스냅샷) | KRW·COUNT | bar | **none** |
| rating | review_count | I-31 stats distribution | COUNT | bar | sum |
| behavior_type | event_count | I-13 groupBy=eventType | COUNT | bar | sum |

### 조립 규칙

- **제로필**: 데이터 없는 날도 y=0 (7월 요청 = 정확히 31점). 단 기간 전체 0건이면 no_data.
- **버킷**: ≤60일 1일 / ≤180일 3일 / 그 외 1주 — 포인트 ≤ `CHART_POINTS_MAX`(60).
  라벨은 구간 시작일 `MM-DD`, 주 버킷은 `MM-DD~`. 버킷 안내는 summary 에.
- **상품축**: y 내림차순 상위 `CHART_TOP_PRODUCTS`(15) 절단 + 절단 안내. 상품명
  `CHART_X_LABEL_MAX`(12)자 절단, 충돌 시 `#상품id` 접미로 **x 유일성 서버 보장**.
- **nullable**(#489·#197): `salesCount`/`salesQuantity` null 은 0 으로 뭉개지 않는다 —
  값이 전혀 없으면 no_data.
- **행동 유형 어휘**: 정본 4종(조회·장바구니·결제시작·구매) 고정 — `removeFromCart` 는
  counts 에 있어도 싣지 않는다(정본 표 확정).

## 4. 기간 2칸 — `chart_period_expr` (#345/#346 연장)

- planner 가 **그래프 기간을 분석 기간과 별도로 말한 경우에만** `chart_period_expr` 로
  옮겨적는다(옮겨적기 규칙은 `[기간]` 절 그대로 — 문구 소유권 period.py 불변).
- `resolve_plan` 이 같은 `period.resolve_period` 로 환산해 `ResolvedPlan.chart_from/to` 에
  담는다. **해석 실패는 ValueError 로 전파하지 않고** `chart_period_error` 에 담아
  `chartUnavailable(chart_period_unclear)` 로 강등한다 — 보고서는 살린다(구 구조는
  ValueError 하나면 턴 전체가 되묻기로 죽었다).
- **확인 대상이 아니다**(결정): 차트는 부가 가치이고 해석 결과가 `report.chartPeriod`
  뱃지로 그대로 노출된다 — DESIGN-SELLER-PERIOD §7.2 확인/고지 비대칭의 **고지** 측.
- 와이어 `chartPeriod` 는 분석 기간과 **다를 때만** 실린다. 스냅샷(가격·재고)엔 안 실린다.

## 5. chart_only — 레인 신설 없음 (결정)

FE `SellerLane` 은 6종 고정이고 노션 결정도 "서버가 `report.title` 로 구분, FE 작업 0"이다.
따라서 `Lane`·`RouteDecision.category` 는 건드리지 않는다 — planner 의 `chart_only=true`
(확실할 때만, analyses 공란 허용)를 받아 **analysis 레인 안에서** 워커 팬아웃·보고서 루프·
recommend 를 생략하고 차트만 조립한다. 이벤트 순서(`meta{analysis}` → `token` → `report` →
`done{replace}`)는 불변, `report.title="판매 분석 그래프"` · `verified/findings` 없음(FE §0
fallback 규약이 흡수). 이력 저장은 하지 않는다(보고서가 없으므로 §6.3 재료가 아니다).

## 6. 와이어 변경 (api-spec §3.2 v0.30.0 — 추가 전용)

`chartPeriod`(다를 때만) · `chartUnavailable[]{reason, message}`(부분 성공 시 charts 와
공존) · `charts[].aggregate`(sum/avg/none) · `charts[].unit` 에 `RATING`. 기존 필드 불변,
FE(jarvis-front) 타입·렌더 대응은 2026-08-09 반영 완료 — AI 가 먼저/나중 어느 순서로
배포돼도 무해(FE 는 신필드 부재 시 구버전 폴백).

## 7. 검증

- `tests/unit/test_seller_charts.py`(신설): 레지스트리 14개 고정·속성, 제로필 31점,
  92일→3일 버킷 31점·365일→주 53점, null→no_data, 상위 15 절단·라벨 유일성, RATING/avg,
  스냅샷 안내, 별점 5칸 고정, 행동 4종, 사유 5종, 부분 성공, 중복 선언 붕괴·조회 캐시.
- orchestrator: 축 선언→조립 위임 인자, 기간 오류 시 LLM 콜 0회, 예외 불전파, chart_only
  가 워커·보고서·추천을 건너뜀. api: chartPeriod 동일/상이 분기, chartUnavailable 직렬화,
  chart_only 제목. 구 G1 테스트는 삭제.

## 8. 이번에 안 하는 것 (정본 표 승계)

다중 계열(계약 series 1개 상한) · 산점도/상관관계(`ChartPoint.x` 가 문자열 라벨) ·
퍼널 차트(보류 유지 — unsupported_axes 안내) · FE 접근성 표 · `progress` 차트 전용 어휘.
