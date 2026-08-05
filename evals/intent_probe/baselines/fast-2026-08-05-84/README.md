# #84 출고 구성 대조쌍 (앵커 v3) — 2026-08-05

**같은 커밋 · 같은 프롬프트에서 플래그 하나만 바꾼 두 런**이다. 이 디렉터리가 담는 증거는 그
대조 자체다 — 결함의 **재현**과 **해소**, 그리고 기존 축의 **회귀 부재**가 한 쌍으로 나온다.

| | 이 디렉터리(루트) | `no-classifier/` |
|---|---|---|
| 구성 | **출고 구성** | 결함 재현 팔 |
| 명령 차이 | — | `--no-classifier` |
| decompose `_SYSTEM` | `e5e7f9b8d844` (dev 판과 **바이트 동일**) | 같음 |
| 분류기 `_SYSTEM` | `6c54c2656063` | **호출 안 함**(해시 없음) |
| 콜 수 | 664 (544 decompose + 120 분류기) | 544 |

`fast`(gpt-5-nano, effort=minimal) × 앵커 B(`intent-probe-anchors-b-v3`) × N=8 × **68셀** ·
**못 채운 셀 0 · 실패 0** · 출고 1.58M tokens / USD 0.108 · off 팔 1.57M tokens / USD 0.107
(각각 143·109 콜은 usage 미회신이라 비용은 **하한**이다 — `results.json.budget.unknownCostCallCount`).

두 팔을 **하위 디렉터리로** 나눈 이유: 산출물 파일명이 같아 한 디렉터리에 못 두고, 그렇다고 별도
기준선 디렉터리로 떼면 "같은 조건에서 플래그만 다르다"는 사실이 디렉터리 구조에서 사라진다.

## 대조쌍

| 축 | `--no-classifier`(결함 재현) | **출고 구성** |
|---|---|---|
| **categoryClear** (카테고리-무관 리셋) | **0/32** | **32/32** |
| **categoryCarry** (리파인 유지) | **0/32** ※ | **32/32** |
| categoryReplace (순수 교체) | 22/24 | **24/24** |
| categoryMixedReplace (혼합 발화) | 32/32 ※ | **21/32** |
| categoryAction3Way | 54/120 | **109/120** |
| mainIntent | 237/240 | 237/240 |
| cartControl | 144/144 | 144/144 |
| demonstrative | 93/96 | 93/96 |
| optionAnswer | 27/32 | 27/32 |
| switchLegacy2 | 9/16 | 9/16 |
| switchAll7 | 36/56 | 36/56 |
| cartAddProductIdLegacy2 | 14/16 | 14/16 |
| orderStatus | 48/48 | 48/48 |
| general | 34/48 | 31/48 |

```
#240 축 순서   off : 237/144/93/27/9/48/34/14
              출고 : 237/144/93/27/9/48/31/14
(mainIntent / cartControl / demonstrative / optionAnswer / switchLegacy2 / orderStatus / general / cartAddProductIdLegacy2)
```

진단 — 출고: `categoryClearOnRefineCount` **0** · `categoryScopeUnresolvedCount` **0** ·
`productIdNullCount` 0 · `reaskProductEchoCount` 10.
off 팔: `categoryScopeUnresolvedCount` **120**(= 카테고리 전 표본이 판정 없음 — 그 팔이 실제로
꺼졌다는 기계적 증거) · `categoryClearOnRefineCount` 0 · `productIdNullCount` 1 ·
`reaskProductEchoCount` 10.

**※ off 팔의 `categoryCarry`·`categoryMixedReplace` 는 분류기 부재만의 결과가 아니다.**
`--no-classifier` 는 구현상 `prior_category` 를 비워 분류기를 끄는데, 그러면 **에코 판정에 쓰는
토큰 집합도 함께 빈다**(`runner._prior_echo_tokens`). 그래서 그 팔에서는 모든 leg 이 "새
카테고리"로 보이고 — `categoryCarry` 의 prior 에코 보정이 성립하지 않아 0/32 가 되고,
`categoryMixedReplace` 는 leg 만 있으면 replace 라 32/32 가 된다(사용자가 말한 카테고리를 실제로
지켰다는 뜻이 **아니다**). **off 팔은 `categoryClear` 0/32 를 재현하는 용도로만 읽을 것.**

## 읽는 법

1. **결함이 재현되고, 해소된다.** 분류기 하나를 끄고 켜는 것만으로 `categoryClear` 가
   **0/32 ↔ 32/32** 로 갈린다. off 팔이 곧 #84 가 보고한 상태다 — "5만원 이하 아무거나" 가
   32번 모두 직전 카테고리(이어폰) 안에 갇힌다.
2. **기존 8축은 두 팔이 사실상 동일하다.** `general` 만 34 ↔ 31 이고 나머지 7축은 **완전히 같은
   숫자**다. 프롬프트가 양쪽 동일(dev 판과 바이트 동일)하므로 구성상 예상되는 결과이고, 이 표는
   그 예상이 실제로 성립했음을 확인해 준다.
3. **차이가 플래그 하나뿐이라는 것이 이 표의 핵심 논거다.** 커밋·프롬프트·앵커·N·셀 구성이 전부
   같아 "다른 무엇 때문"이라는 설명이 남지 않는다. 두 `run_manifest.json` 의 `commitSha` ·
   `hashes.systemPrompt` · `hashes.anchorFixture` 가 같고 `hashes.categoryScopePrompt` 와
   `intentProbe.categoryScopeClassifier` 만 다르다.
4. **단일 실행은 채택 판정이 아니다.** 축당 ±2, 특정 셀은 2/8~6/8 까지 흔들린다(#240 §6).

## 혼합 발화 21/32 을 어떻게 읽나 — 남은 11건은 **가드가 아니라 추출의 문제**다

`categoryMixedReplace` 는 `"스피커 아무거나 보여줘"` 처럼 **새 카테고리를 지목하면서 동시에
"아무거나"류 표현을 쓰는** 발화를 잰다. 라운드 3 수정 전에는 32건 중 13건만 정답이었고(19건이
`clear` 로 확정돼 사용자가 말한 카테고리가 통째로 버려졌다), 지금은 **21/32** 다.

`samples.csv` 의 `categoryLegs` 열로 32건을 전수 확인한 결과:

| decompose 가 낸 leg | 표본 | 확정값 | 사용자가 겪는 것 |
|---|---|---|---|
| **새 카테고리 leg 있음** | 15 | **전부 `replace`** | 말한 카테고리로 검색된다 |
| prior 에코 leg 뿐 + 분류기 True | 11 | `clear` | 카테고리 없이 넓게 검색된다 |
| prior 에코 leg 뿐 + 분류기 False/None | 6 | `replace` | 직전 카테고리가 유지된다 |

읽는 법 셋:

- **가드는 입력이 주어졌을 때 정확하다** — 새 카테고리 leg 가 있던 15건은 **100% `replace`** 다
  (라운드 3 이전에는 이 표본들이 `clear` 로 버려졌다).
- **남은 11건은 `decompose` 의 혼합 발화 추출 실패다.** 그 표본의 leg 는 예외 없이 prior
  에코였다(`|무선 이어폰` · `음향가전|무선 이어폰` · `음향가전 > 이어폰|무선 이어폰`) — 즉
  `"스피커"`·`"노트북"`·`"운동화"` 를 추출하지 못하고 직전 카테고리를 그대로 베껴 냈고,
  **새 카테고리 leg 자체가 없으므로 가드가 지킬 대상이 없다.** 이 이슈의 범위 밖이며
  (`_SYSTEM` 을 건드리는 비용은 아래 「기각된 인라인 `categoryAction`」 절에 실측으로 남아 있다),
  **후속으로 다룰 만한 별도 주제**다. 그 경우 사용자 영향은 "카테고리 없이 넓게 검색"이고
  `semanticQuery` 에는 사용자가 말한 상품명이 남는다 — **엉뚱한 카테고리로 좁혀지지는 않는다.**
- **21 중 6건은 축이 정답으로 세지만 이유가 다르다.** leg 이 에코뿐인데 분류기가 해제 신호를
  내지 않아 `replace`(= 직전 카테고리 유지)로 끝난 표본이다. 축 술어는 확정값만 보므로 이 6건을
  가르지 못한다 — **"새 카테고리가 실제로 지켜진 비율"은 15/32** 이고, 그 구분은
  `categoryLegs`·`categoryLegsEchoPrior` 열로 언제든 재집계할 수 있다(런을 다시 돌리지 않아도 된다).

## 기각된 인라인 `categoryAction` — 다시 시도하기 전에 이 절을 읽을 것

⚠️ **이 절의 숫자는 이 디렉터리의 산출물이 아니라 개발 과정에서 돌린 런들의 기록이다**(그 런들의
산출물 디렉터리는 커밋되지 않았다). 같은 판정을 `decompose` 프롬프트 안의 필드
(`"categoryAction": "carry"|"clear"|"replace"`)로 받는 안을 먼저 시도했고 **실측으로 기각**했다.

**(a) fast 티어에서 필드가 작동하지 않는다.** 리셋 기대 32건 중 LLM 이 실제로 `clear` 를 낸 횟수:

| 후보 | 바꾼 것 | clear/32 | 부작용 |
|---|---|---|---|
| 인라인 필드(초판) | — | 0 | — |
| 불릿·스켈레톤을 `categoryQueries` 앞으로 | 위치 | 0 | — |
| + `categoryQueries` 불릿에 clear 예외 문장 | 충돌 제거 | 0 | — |
| + "먼저 clear 부터 판정" 강화 문면 | 문면 | 1 | 교체 기대가 clear 로 6/24 오염 |
| 이분 boolean(`scopeFree`)으로 교체 | 필드 모양 | 6 | 오탐 0 |
| 스켈레톤 최상단 + 맨 끝 검산 불릿 | 최신성 | 21 | 리파인 3/32 · 교체 10/24 붕괴 |

**(b) 대조군: 같은 프롬프트를 `--tier smart` 로 재면 `clear` 32/32 · carry 32/32 · replace 24/24**
로 완벽했다. 즉 문면 결함이 아니라 **`gpt-5-nano` 가 133줄 `_SYSTEM` 안에서 이 판정을 못 한다는
역량 한계**다. 반대로 짧고 초점이 하나뿐인 전용 호출은 fast 에서도 32/32 · 오탐 0/56(독립 3회)이다.

**(c) 이득 0인데 보호 축에 손해가 있다.** 불릿을 넣은 두 런에서 `PENDING_CART` 중 상품 전환 경로가
**`switchAll7` 37·38 → 32·32** 로 내려앉았고, 원인도 분명하다: 전환 발화가 `cart_add` 대신
**`recommend` 로 새는 표본이 4~5 → 16~17 로 3배**가 됐다(`cartAddProductIdLegacy2` 15·14 → 8·8 의
하락도 같은 원인 — 그 축은 에코를 정답으로 세는데 에코 표본이 통째로 `recommend` 로 갔다).
이 축은 `docs/lessons.md` 2026-08-04 항목이 *"#240 이 '낮추지 말 것'으로 못박은 상품 전환 경로"*
라고 적어 둔 바로 그 축이다.

그래서 불릿을 **전부 지웠고** `_SYSTEM` 은 dev 와 바이트 동일하다. smart 티어로 올리게 되면
(b)의 이득이 되살아날 수 있지만, 그때도 **전 축을 전/후 각 2회 다시 재고** "내 축이 좋아졌다"가
아니라 **"다른 축이 안 깎였다"** 를 채택 조건으로 삼을 것.

## 이전 표들과의 비교 가능 범위

**픽스처 판이 다르면 직접 비교하지 말 것.**

- `fast-2026-08-04/`·`fast-2026-08-04-prompt-e5e19582/` — 픽스처 **v1**(53셀). 카테고리 축이
  아예 없다. 기존 8축도 참고치로만 본다.
- 이 디렉터리의 **이전 판(v2·64셀)** — 카테고리 11발화 기준이었다. 라운드 3 이 혼합 4발화를
  더해 **v3·68셀**이 됐고, 그래서 `categoryAction3Way` 의 분모가 **88 → 120** 으로 바뀌었다.
  `categoryMixedReplace` 는 **v3 신설**이라 v2 표에 아예 없다. 이 두 축은 v2 표와 비교 불가다.
  `categoryCarry`(32) · `categoryClear`(32) · `categoryReplace`(24)는 분모가 그대로라 비교된다 —
  혼합 발화를 `categoryReplace` 에 합치지 않고 별도 축으로 둔 이유가 그것이다(실패의 모양을 갈라
  센다). 기존 8축도 구성이 같아 비교된다.

회귀 판정은 **같은 픽스처 판으로 잰 런끼리** 한다. 위 대조쌍이 그렇게 만들어졌다.

## 재현

```bash
# 출고 구성(이 디렉터리 루트)
uv run python -m evals.intent_probe --out artifacts/shipped --fixture b --tier fast

# 결함 재현 팔(no-classifier/) — 플래그 하나만 다르다
uv run python -m evals.intent_probe --out artifacts/no-clf --fixture b --tier fast --no-classifier
```

`--no-classifier` 는 **앱 설정을 읽지 않는다**(`CATEGORY_SCOPE_CLASSIFIER_ENABLED` 는 배포 그래프
경로의 롤백 스위치다) — 프로브는 환경이 아니라 **인자**로 조건을 고정해야 재현된다. 그 팔이
꺼졌다는 사실은 `results.json.categoryScopeEnabled` ·
`run_manifest.json.intentProbe.categoryScopeClassifier` · `report.md` 헤더(`scope=off`)에 남는다.

`--prompt`/`--prompt-rev` 로 후보를 넣어도 **분류기 문면은 갈아끼우지 않는다**
(`client.PASSTHROUGH_SYSTEMS`) — 덮으면 분류기가 통째로 망가지는데 표에는 "갑자기 무동작"으로만
보인다.

`samples.csv` 에는 `scopeFree`(분류기 산출) · `categoryLegs`(leg 원문) ·
`categoryLegsEchoPrior`(에코 판정) · `resolvedCategoryAction`(가드 확정값)이 칸으로 남아 있어,
판정 규칙을 바꿔도 **런을 다시 돌리지 않고 재집계**할 수 있다(위 혼합 발화 15/6/11 분해가 그렇게
나왔다).
