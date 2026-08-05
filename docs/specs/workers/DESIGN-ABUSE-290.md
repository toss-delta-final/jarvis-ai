# abuse Worker 설계 — 3-트랙 이상 탐지 (이슈 #290)

> 근거 논문: Chandola et al. 2009 (Point/Contextual/Collective 유형 체계) ·
> Tan & Kumar 2002 (봇 시그널 피처). 구현: `analysis/outliers.py`(+timeseries 헬퍼 공유)
> · 배선: `tools.get_behavior_events`·`tools.get_account_events`.

## 1. 해결 문제

LLM 이 "많아 보이는 것"을 감으로 지목했다 — 분포 기준(무엇에 비해 많은가)과
유형 구분(볼륨 이상 / 맥락 이상 / 집합 이상)이 없었다.

## 2. 3-트랙 구조 (Chandola 유형 매핑)

| 트랙 | 원천 | 계산 | 봇 신호 해석(Tan & Kumar) |
|---|---|---|---|
| Point | I-13 date-groupBy 일별 이벤트 총량 | `mad_spikes` — robust z ≥ 3.5, **상방만** | 봇 유입 = 요청량 급증 |
| Contextual | I-13 상품별 비율 3종 | `tukey_upper_outliers` — 브랜드 내 Q3+1.5×IQR 초과 | 조회 폭증+구매 0, 담기 봇, 소수 방문자 반복 조회 |
| Collective | I-8 hour·ip groupBy | `night_activity_share`(0~6시 비중) + failCount 내림차순·isSuspicious 요약 | 심야 편중·무차별 대입 |

- I-8 기본 비활성(`seller_account_events_enabled=false`, admin 소유 🔴) —
  Collective 트랙만 생략되고 나머지는 완주(v0.19.1 관용 규약).
- 방향 규칙: MAD 하방·Tukey 하위는 표기하지 않는다 — 저활동은 어뷰징 신호가 아니다.

## 3. Feature 매핑 표

| 논문 피처 | 계약 필드 | 판정 |
|---|---|---|
| 야간 활동·failCount | I-8 hour/ip rows | ✅ 직접 |
| 요청량(세션당) | I-13 일별 총량 | 🔶 프록시(세션→일 단위) |
| 커버리지(탐색 폭) | I-13 방문자당 조회 | 🔶 프록시(세션→상품 단위) |
| 이벤트 간격 규칙성 | 없음(세션 원시 필요) | ❌ Phase B |
| HTTP 계층 피처(UA·robots.txt) | 없음 | ❌ 영구 제외(수집 계층 밖) |

## 4. 오탐 통제 (정상 설명 후보)

Point 스파이크 검출 시 I-15 가격/재고 변경 이력을 자동 대조 — 스파이크일에 변경이
있으면 "당일 가격/재고 변경 이력 있음 — 정상 설명 후보" 동봉(프로모션 가격 인하
트래픽을 봇으로 단정하는 오탐 통제). I-15 실패 시 스파이크 보고는 유지하고
"겹침 미확인"만 고지(§3.4).

## 5. 판단 기준

`seller_mad_threshold=3.5`(Iglewicz-Hoaglin) · `seller_tukey_k=1.5` ·
`seller_night_hours_start=0`/`end=6`. "이상 없음"과 "판정 보류(표본 부족: MAD<3점,
Tukey<4점, hour 유효 0건)"를 구분 표기한다.

## 6. 출력 → LLM

`OutlierFlag{type, target, metric, value, threshold, normal_explanations}` 기반 문구.
LLM 규칙(프롬프트): **봇 의심 / 어뷰징 의심 / 설명 가능** 3분류만 사용, 단정 금지
(탐지 보고 ≠ 제재 판정), 정상 설명 후보가 붙으면 '설명 가능' 우선 검토,
isSuspicious 는 코드 판정 — 번복 금지(#215 계승).

## 7. degrade·테스트

- 재현 테스트(`test_seller_analysis_outliers.py`): 스파이크 주입 → 해당일만 검출,
  하방 미표기, 상수 수열 급증, Tukey IQR=0 경계, 심야 비중 관대 수신, 결정론.
- 도구 테스트: 가격 변경 겹침 플래그, I-15 실패 관용, 조회 폭증+구매 0 패턴,
  ip failCount 정렬.

## 8. Phase B

세션 단위 iForest/LOF(전역+국소 2관점)·이벤트 간격 규칙성 — pg-profile
`processed_events`/`session_activity` 활용 검토(계약 개정 없이 가능성, handoff §8-3).
