# sales_anomaly Worker 설계 — S-H-ESD (이슈 #290)

> 근거 논문: Hochenbaum, Vallis & Kejariwal 2017 (S-H-ESD) · Cleveland et al. 1990 (STL).
> 구현: `app/agents/seller/analysis/timeseries.py` · 배선: `tools.get_sales_timeseries`.

## 1. 해결 문제

현행 SMA(직전 3~7일 평균 ±30%)는 요일 효과를 구분하지 못한다 — 주말 정상 저매출이
급락으로 오탐되고, 요일 패턴에 묻힌 진짜 이상은 미탐된다. STL 로 추세·계절(요일)
성분을 걷어낸 잔차에 검정을 걸어 "원래 낮은 날"과 "비정상적으로 낮은 날"을 가른다.

## 2. 입력 데이터 (계약 매핑 — I-6, api-spec §4.4)

- 도구가 요청 기간 앞에 `seller_analysis_lookback_days`(28일)를 붙여 조회한다 —
  STL(period=7)은 최소 2주기(14일) 이력이 필요하다(Cleveland 1990). 총매출·상세
  나열·이상 보고는 **요청 기간 내로 한정**(질문 밖 수치 미노출).
- Spring 이 준 `isAnomaly`/`deviationPct` 는 참고치 — 원시 `sales` 만 쓴다(§0.1 D).

## 3. Feature 매핑 표 (결측 파라미터 4단계 판정)

| 논문 변수 | 계약 필드 | 판정 |
|---|---|---|
| 시계열 관측값 x_t | I-6 `series[].sales` | ✅ 직접 |
| 계절 주기 period | 요일(7일) — 도메인 상수 | ⚠️ 파생(`seller_stl_period`) |
| 검정 유의수준 α, 최대 이상 비율 | Settings 주입 | ✅ (`seller_gesd_alpha`·`seller_gesd_max_anomalies_ratio`) |
| 장기 추세 제거(piecewise median) | STL trend 로 대체 | ⚠️ 논문 §3.2 의 변형 — 기간이 짧아(≤42일) STL trend 로 충분 |

손실 없음 — 전부 ✅/⚠️ (handoff §5).

## 4. 알고리즘

1. 이력 ≥ `seller_min_history_for_stl`(14): STL(period=7, robust) 분해 →
   잔차 = 관측 − (추세+계절). 미만: 중앙값 편차 폴백(계절 미조정 — 출력에 표기).
2. 잔차 노이즈 플로어: 값 스케일 대비 1e-9 미만 잔차는 0 — 완전 규칙 시계열에서
   부동소수 먼지를 이상으로 오탐하는 degenerate 케이스 차단(구현 중 발견).
3. robust GESD: 반복마다 남은 표본의 median/MAD 로 R_i, t-분포 임계 λ_i 와 비교.
   이상 수 = "R_i > λ_i 인 최대 i"(Rosner 1983). 최대 floor(n×0.2)개.
4. 무매출 규칙(#194 계승): 값 0 포인트 미판정 / 무매출 이력 직후 발생 = 이상
   (기대 0 → 편차% None) / Spring 플래그 구조적 무시.

## 5. 판단 기준 (튜너블 — config.py, 기동 검증)

`seller_stl_period=7` · `seller_gesd_alpha=0.05` · `seller_gesd_max_anomalies_ratio=0.2`
· `seller_analysis_lookback_days=28` · `seller_min_history_for_stl=14`.
관계 검증: `min_history ≥ 2×period`, `lookback ≥ min_history`(무음 무효화 방지).

## 6. 출력 → LLM

`SeasonalAnomaly{date, actual, expected, deviation_pct|None, sigma, direction}` →
도구 문구: `2026-07-30 실측 120,000원·기대 350,000원 (계절조정 -65.7%, 3.8σ, 급락)`.
폴백 시 "robust 판정 — 계절 미조정" 병기. LLM 은 이상일을 가격/재고 변경(I-15)·주문
전이(I-14)와 대조해 원인 후보만 서술한다(판정 번복 금지 — 프롬프트 §통계 판정 해석).

## 7. degrade·테스트

- 분석 ValueError → 매출 요약 유지 + "판정 불가" 표기(§3.4). 3점 미만 = 판정 보류.
- 재현 테스트(`test_seller_analysis_timeseries.py`): 요일 계절성 합성 시계열 +
  평일 −40% 주입 → 해당일만 검출·주말 오탐 0 / 결정론 / α 주입 경계 / 무매출 3종.

## 8. Phase B

Prophet(휴일·프로모션 regressor)은 프로모션 계약 부재 + cmdstan 무게로 제외 —
프로모션 데이터 계약이 생기면 재검토(worker-papers.md).
