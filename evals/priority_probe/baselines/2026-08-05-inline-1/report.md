# priority 신호 실측 프로브 리포트 (#281)

arm=inline · prompt=1295b6e192ac (file:/home/uuser/orca/workspaces/jarvis-ai/feat-281-priority-budget-exclusion/evals/priority_probe/candidates/inline_priority.txt) · classifierPrompt=5a80ffbdb2f8 · tier=fast · model=gpt-5-nano · fixture=priority-probe-v1 · N=8

> 인라인/전용 분류기 중 어느 쪽이 fast 티어에서 니즈 priority(1 필수/2 권장/3 선택)를 안정적으로 추출하는가. 숫자가 결정한다 — 결론을 먼저 쓰지 않는다.

## 축

| 축 | 점수 | 분자 정의 | 분모 정의 |
|---|---|---|---|
| `priorityOrderPairs` 제외 순서 쌍(본질 축) | 0/288 (0.0%) | 기대 priority 가 다른 니즈 쌍에서 산출이 같은 대소 관계 | 그런 쌍의 수 × N |
| `essentialProtected` 필수 니즈 보호 | 0/104 (0.0%) | 기대 1 인 니즈가 산출에서 최소값 집합에 든다 | 기대 1 니즈 수 × N |
| `priorityOrderPairsByIndex` 제외 순서 쌍(보조 · 위치 매칭, 비교 가능 표본 2건) | 3/6 (50.0%) | 기대 priority 가 다른 니즈 쌍에서, leg 개수가 같을 때 위치로 짝지은 산출이 같은 대소 관계(이름 무시) | leg 개수가 needs 개수와 같은 표본에서만 나온 그런 쌍의 수 |
| `prioritySignalPresent` 신호 유무 | 0/288 (0.0%) | 그 니즈에 유효한 1/2/3 값이 나왔다 | 니즈 수 × 셀 × N |
| `priorityExact` 정확 일치(보조) | 0/288 (0.0%) | 값이 기대와 정확히 일치 | 니즈 수 × 셀 × N (prioritySignalPresent 와 같은 분모) |

## 진단 (합불 아님)

- 미파싱(unparsedCount, lengthMismatchCount 와 상호 배타적): 0
- 길이 불일치(lengthMismatchCount, 구조적 비용): 94
- 이름 불일치(nameUnmatchedCount, 인라인 전용 · lengthMismatchCount 와 겹칠 수 있음): 288
- 범위 밖 값(invalidValueCount, 이름은 매칭됐지만 값이 범위 밖): 0
- 빈 신호 표본(emptySignalCount, 결과 지표 — 위 원인들과 겹칠 수 있음): 96
- 전송 재시도(transportRetries, TASK-3-CORRECTION — 크면 표를 신뢰하지 말 것): 0

## 채우지 못한 셀

(없음)

## 재현 함정

1. 전역 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 아래 목록에 드러난다.
3. 픽스처 문자열이 정답 신호와 겹치면 안 된다(발화에 '필수'·'선택' 같은 어휘 금지).
4. 단일 실행은 채택 판정이 아니다 — 독립 2회 이상의 분포로 판정한다.
5. 빈 맥락 프로브는 거짓 결론을 준다 — 인라인 팔은 채운 PRIOR_FILTERS/LAST_RECOMMENDATIONS 로 잰다.

페이싱 실측: 대기 15회 / 허용 45 rpm.
