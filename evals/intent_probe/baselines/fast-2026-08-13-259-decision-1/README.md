# fast-2026-08-13-259-decision-1

#259의 A/B/C 출고 결정을 위해 현행 A(`fast`)를 최신 구매자 경로에서 재확인한 전량 런이다.

- `gpt-5-nano` · reasoning `minimal` · 픽스처 v8(101셀) · N=8
- 성공 표본 808/808 · `unfilledCells=[]`
- 실행 커밋 `9cd42ba11935e88ccd09ad2e449ba91b047eb0d0` · clean
- 프롬프트 sha12 `a853c1c4f2be` · fixture sha256 `f5c33d5e9928…`

| 축 | 결과 |
|---|---:|
| `mainIntent` | 236/240 |
| `cartControl` | 144/144 |
| `screenResolution` | 48/48 |
| `general` | 31/48 |
| `switchLegacy2` | 8/16 |
| `optionAnswer` | 28/32 |
| `categoryMixedReplace` | 24/32 |
| `wishlistRemoveRouting` | 24/32 |

`failures.csv`의 429 13건은 재시도 전 시도 기록이며 성공 N개를 모두 채웠다. `samples.csv`의
latency 꼬리는 전역 페이서 대기를 포함하므로 E2E TTFT로 인용하지 않는다. 비용·token도 212콜이
unknown이라 완전한 총액이 아니다.

이 런의 해석과 A 유지 결정은 [`../../DECISION-259.md`](../../DECISION-259.md)가 정본이다.
