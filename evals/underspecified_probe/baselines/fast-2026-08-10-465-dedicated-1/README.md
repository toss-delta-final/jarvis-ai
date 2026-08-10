# fast-2026-08-10-465-dedicated-1

`dedicated` 팔 · `fast` 티어 · N=8 · 프롬프트 sha12 **`5d5a71b0ab09`**(#466 병합 후 판) · `prompt.source=repo:_SYSTEM`.

| 축 | 값 |
|---|---|
| `missRate` | 14/112 (12.5%) |
| `falseAlarmRate` | 23/104 (22.1%) |
| `flagOffInvariant` | 0/240 |
| `priorGateInvariant` | 0/240 |
| miss 중 `categoryQueries` 낀 표본 | 1 |

전용 호출 팔 — **기각**. 오탐이 전부 `what_axis` 슬라이스(사용자가 무엇을 살지 **말한** 턴)라, 배포했다면 그런 턴에 되물음이 떴을 것이다.

⚠️ 이 런은 처음 `--arm tri` 로 돌렸는데 `cli.py` 가 `tri` 에서 후처리 플래그를 켜지 않아 **실제로는 dedicated 단독 측정**이었다. 그래서 디렉터리 이름을 실제 잰 것에 맞게 고쳤다 — 산출물 이름이 사실과 어긋나면 다음 사람이 오독한다.

`evals/underspecified_probe/baselines/fast-2026-08-10-465-REPORT.md` 가 이 라운드의 정본이다 — 세 팔 비교표·발동률 표·#466 병합 전후 대조가 거기 있다.
