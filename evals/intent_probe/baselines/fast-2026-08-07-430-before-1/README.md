# #430 타축 회귀 대조 — before 런 1/2 (2026-08-07)

> ⚠️ **이 런은 `#386` 병합 전 판(`81e3770e1340`)을 픽스처 **v5**(79셀)로 잰 기록이다.**
> 출고되는 판은 `865ed6fd771e` 이고 그 대조는 `../fast-2026-08-07-430-v6-adopted-1/README.md`
> (픽스처 v6·85셀)에 있다. **두 표의 축 수치를 같은 표에서 빼지 마라** — 프롬프트도 픽스처도
> 다르다. 이 디렉터리는 "맥락까지 좁힌 트리거가 `screenExactPick` 을 회수했다"는 병합 전
> 근거로 남긴다.


`decompose._SYSTEM` 변경 **전**(sha12 `11c6fe3bfa0c`, `source=repo:_SYSTEM`)의 런.
`#240` 축 순서 요약: `240/144/96/29/8/48/32/15`.

전 축 대조표·해석·`--prompt` 오염 경고는 `../fast-2026-08-07-430-after-1/README.md` 가 정본이다
— 여기 숫자를 그 표 없이 인용하지 말 것.

`screenExactPick` 32/32 · `screenOutOfListConfirmCount` 0 · `screenNoHallucination` 8/8 ·
`screenReask` 8/8. (79셀 · N=8 · 752콜 · 종료 코드 0 · 못 채운 셀 0 · 관측 부분합 USD 0.1329)
