# #430 타축 회귀 대조 — after 런 3/3 (2026-08-07)

채택안 F(sha12 `81e3770e1340`, `source=repo:_SYSTEM`)의 세 번째 런. **`-after-2` 의
`categoryReplace` 저점(18/24)을 정리하려고 추가로 돌린 런**이고, `screenExactPick` 세 번째
표본도 여기서 나왔다. `#240` 축 순서 요약: `238/144/94/26/12/48/31/16`.

- `categoryReplace` 21/24 — F 3런이 21·18·21 이라 **3런 중 2런이 20 이상**이고, before 폭
  자체가 20~24 이며 후보별 대조에 추세가 없어 **노이즈로 판정**했다(근거는 정본 README 각주 2).
- `screenExactPick` **29/32** · `screenOutOfListConfirmCount` **3** — 앞 두 런(31·1, 31·1)보다
  낮다. 이 런 때문에 잔여 회귀가 −1 이 아니라 **−1.67** 로 확정됐다. 숨기지 않는다.
- `screenNoHallucination` 8/8 · `screenReask` 8/8 — 무회귀.

전 축 대조표·해석은 `../fast-2026-08-07-430-after-1/README.md` 가 정본이다.
(79셀 · N=8 · 752콜 · 종료 코드 0 · 못 채운 셀 0 · 관측 부분합 USD 0.1406)
