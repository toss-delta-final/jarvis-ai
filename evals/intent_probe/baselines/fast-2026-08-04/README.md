# fast 기준선 — 2026-08-04

현재 `_SYSTEM`(sha12 `e5e7f9b8d844`) × `fast`(gpt-5-nano, effort=minimal) × 앵커 B, N=8.
53셀 · 424 표본 · 못 채운 셀 0 · 502 시도(429 재시도 78) · 1.27M tokens · **USD 0.086**.

```
#240 축 순서: 238/144/94/27/10/48/34/15
(mainIntent / cartControl / demonstrative / optionAnswer / switchLegacy2 / orderStatus / general / cartAddProductIdLegacy2)
```

| 축 | 점수 |
|---|---|
| `cartControl` | 144/144 (100%) |
| `orderStatus` | 48/48 (100%) |
| `mainIntent` | 238/240 (99.2%) |
| `demonstrative` | 94/96 (97.9%) |
| `cartAddProductIdLegacy2` | 15/16 (93.8%) |
| `optionAnswer` | 27/32 (84.4%) |
| `switchAll7` | 42/56 (75.0%) |
| `general` | 34/48 (70.8%) |
| `switchLegacy2` | 10/16 (62.5%) |

진단: `reaskProductEchoCount` **7** (되물음 상품을 그대로 담은 위험한 실패) · `productIdNullCount` 0.

## 읽는 법

- **단일 실행은 채택 판정이 아니다.** 축당 ±2, 특정 셀은 2/8~6/8 까지 흔들린다(#240 §6).
  후보 비교는 독립 2~3회 분포로 한다.
- **#240 표와 직접 비교하지 말 것.** 프롬프트 판이 다르고(`e5e19582…` vs `e5e7f9b8…`),
  앵커도 유실된 원본을 이슈 본문에서 재구성한 것이다. 숫자가 가까운 것은 참고 신호일 뿐이다.
- 약한 곳은 전부 **맥락이 붙었을 때**다 — `안녕` 은 맥락 없음 8/8 인데 직전추천 1/8,
  PENDING_CART 1/8 이다. "원인은 규칙 문장이 아니라 맥락 주입"이라는 #240 §3 결론과 같은 방향.

## 이 런의 페이서 설정은 지금 기본값과 다르다

이 런은 `--rpm 50` · 콜당 토큰 추정 3.1k(당시 기본값)로 돌았고, 그래서 429 를 78회 먹었다
(TPM 한도가 `max_tokens` 예약분까지 세기 때문 — `Limit 200000, Used 200000`). 재시도가 전부
흡수해 셀은 다 찼지만, 그 실측을 근거로 기본값을 **45rpm · 콜당 3.9k** 로 바꿨다.
`run_manifest.json` 의 `intentProbe.pacer` 에 이 런의 실제 설정이 남아 있다.
