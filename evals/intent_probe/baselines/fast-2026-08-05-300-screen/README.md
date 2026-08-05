# #300 screen 지시어 해소 기준선(앵커 v4) — 2026-08-05

`#118`(PR #292)이 별도 스크립트(이관 후 삭제됨)로 재던 screen 지시어
해소 6셀을 `evals/intent_probe`로 흡수한 뒤, 그 6셀만 실 LLM으로 1회 잰 기준선이다. 전량
재실행은 하지 않는다(§7 범위 밖) — 기존 68셀은 이 런에 포함되지 않는다.

## 명령

```bash
uv run python -m evals.intent_probe \
  --out evals/intent_probe/baselines/fast-2026-08-05-300-screen \
  --tier fast --repeats 8 \
  --case-ids screen-001,screen-002,screen-003,screen-004,screen-005,screen-006
```

`fast`(gpt-5-nano) × 앵커 B(`intent-probe-anchors-b-v4`) × N=8 × **6셀 = 48콜**(decompose만 —
screen 컨텍스트는 `priorFilters`에 `category`가 없어 범위 해제 분류기는 호출되지 않는다, 실측
`pacer.acquireCount == 48`). **못 채운 셀 0 · 실패 0**(`failures.csv` 헤더뿐) · 종료 코드 0.

- prompt: `e5e7f9b8d844`(`repo:_SYSTEM`, dev 판과 바이트 동일)
- fixture: `intent-probe-anchors-b-v4`(sha256 `63b2bc9...4dea`)
- 비용: USD 0.0116 / 173,207 tokens (`results.json.budget`, 4콜은 provider가 usage를 되돌려주지
  않아 하한값 — `unknownCostCallCount: 4`)

## 축 결과

| 축 | 점수 |
|---|---|
| `screenExactPick`(확정 4발화 × 8 = 32) | **31/32** |
| `screenReask`(되물음 1발화 × 8 = 8, 안전 셀) | **8/8** |
| `screenNoHallucination`(확정금지 1발화 × 8 = 8) | **8/8** |
| `screenResolution`(합, 6발화 × 8 = 48) | **47/48** |

진단(합불 아님): `screenPromptLayerHitCount` 31/48(해소기 전 원본 decompose 산출만으로 규칙을
만족한 표본) · `screenResolverOverrideCount` 39/48(해소기가 실제로 발동해 productId를 확정/되물음
으로 바꾼 표본) · `screenOutOfListConfirmCount` **0**(두 목록 밖 id 확정 — 위험한 실패, 0이어야
한다는 요구를 충족).

## #118 수치와의 대조

#118 원 프로브(이관 전, 이 이슈가 흡수하며 삭제한 스크립트)가 잰 것은 서로 다른 조건의 **세 변형**
이었다(그 스크립트의 모듈 docstring, 삭제 전 git 이력에서 확인 가능):

| 변형 | 조건 | 신규 지시어 해소(48콜 기준) |
|---|---|---|
| `before` | screen 을 아예 주입하지 않은 오늘의 코드(#118 이전) | **9/48** |
| 프롬프트 층(SCREEN 블록 채택안, 해소기 전) | screen 을 SCREEN 블록으로 주입 · 코드 해소기 없음 — `decompose.py:238` 주석의 채택 근거 수치 | **27/48** |
| `adopted`(SCREEN 블록 + 코드 해소기) | 지금 배포된 조합 — decompose 다음 `resolve_screen_reference` | **48/48**(안전 셀 8/8 · 오담기 0) |

이 런이 재는 `screenPromptLayerHitCount`(31/48)는 **"SCREEN 블록 주입 · 해소기 전"과 같은
조건**이다 — apples-to-apples 짝은 **27/48 ↔ 31/48**이고, `screenResolution`(해소기 통과 후
최종값)의 짝은 **48/48 ↔ 47/48**이다. `9/48`은 screen 을 아예 안 실었던 조건이라 **어느 쪽과도
비교하지 않는다**(설계 자체가 다르다 — 지금은 `_SYSTEM_WITH_SCREEN` 하나만 배포돼 있어 `before`
변형을 재현할 스위치도 없다).

| | #118 (변형별) | 이 런(#300, 흡수 후) | apples-to-apples? |
|---|---|---|---|
| 종합(해소기 통과 후) | `adopted` 48/48 | **47/48** | 예 |
| 안전 셀(screenReask) | `adopted` 8/8 | **8/8** | 예 |
| 오담기(screenNoHallucination) | `adopted` 0/8 오담기 = 8/8 정답 | **8/8**(오담기 0) | 예 |
| 프롬프트 층(해소기 전, SCREEN 블록 채택안) | **27/48** | **31/48** | 예 |
| screen 미주입(`before`) | 9/48 | — (재현 불가) | 아니오 — 다른 축과도 비교하지 않는다 |

- **종합 47/48은 #118의 48/48과 사실상 같다.** 유일한 미스는 `screen-003`(`3번째 거 담아줘`,
  화면 5건) 표본 4번째(`sampleIndex=3`)로, `decompose`가 그 회차에 `intent="recommend"`를
  냈다(원본 산출 `productId`도 비었다) — **코드 해소기가 실패한 게 아니라 decompose의 intent
  라우팅이 그 1회만 미끄러졌다.** `resolve_screen_reference`는 `graph.py`와 같은 조건(intent가
  이미 `cart_add`일 때만 발동)을 따르므로 이 회차는 애초에 해소기 호출 대상이 아니었다
  (`samples.csv`에 `screenResolverFired=False`로 남아 있다). 단일 실행은 축당 ±흔들림을 포함한다
  (README 「재현 함정」#4 — 축당 ±2, 독립 2~3회로 판정) — 이 1건은 그 흔들림 범위 안이다.
- **안전 셀·오담기 축은 정확히 일치한다(8/8 · 오담기 0).** `screen-002`(화면 3건, 후보 다수)는
  8회 모두 `resolve_screen_reference`가 `ambiguous_screen_candidates`로 되물음을 강제했고,
  `screen-006`(`301 담아줘`)은 8회 모두 `unknown_product_id_spoken`으로 확정을 막았다 — 코드
  해소기가 결정적으로 개입하는 4개 셀(001·002·004·006)은 이 런에서도 **원본 decompose 산출과
  무관하게** 항상 같은 결과를 냈다(`samples.csv`의 `productId`(원본)가 회차마다 흔들려도
  `resolvedProductId`는 고정).
- **프롬프트 층 27/48 ↔ 31/48은 같은 조건(SCREEN 블록 주입·해소기 전)의 비교이고, 이 하네스가
  스스로 인정하는 흔들림 범위(±2, 독립 2~3회로 판정) 안이다** — "직접 비교하지 않는다"보다
  훨씬 강한 진술을 데이터가 이미 지지한다. 값 자체는 참고 수치일 뿐, 코드 해소기가 있는 한 최종
  정확도(`screenResolution`)에 영향을 주지 않는다(개입하는 4셀은 원본이 틀려도 해소기가 덮어쓴다).
- **축 정의 문구 정정(리뷰 1차 F-5)**: 이 런을 실측한 뒤 `metrics.py`의 4개 screen 축
  `notComparableWith`에 있던 "프롬프트 층만 9/48"이라는 뭉뚱그린 문구가 위 표처럼
  `before` 9/48 · 프롬프트 층(SCREEN 블록) 27/48로 갈라졌다. 이 정정은 산출물의 `definition`
  문자열에만 반영되며, **위 실측값(축 점수·진단 카운터)에는 영향이 없어 런을 다시 돌리지
  않았다** — `results.json`·`report.md`의 `notComparableWith` 텍스트는 이 런 시점 기준으로
  낡은 채 남아 있다.

## 셀별 원본 vs 최종값 (`samples.csv` 실측, `screenPromptLayerHitCount`/`screenResolverOverrideCount` 정의 그대로 재계산)

| utteranceId | 규칙 | 원본(해소기 전) 정답률 | 최종(`resolvedProductId`) 정답률 | 해소기 사유 |
|---|---|---|---|---|
| screen-001 | screenExact(3101) | **3/8** | **8/8** | `sole_screen_candidate`(항상 발동) |
| screen-002 | screenReask(None) | **0/8** | **8/8** | `ambiguous_screen_candidates`(항상 발동) |
| screen-003 | screenExact(3103) | **6/8** | **7/8**(위 미스 1건) | `ordinal`(intent가 cart_add일 때만 발동) |
| screen-004 | screenExact(3108) | **6/8** | **8/8** | `coordinate`(항상 발동) |
| screen-005 | screenExact(3110) | **8/8** | **8/8** | 발동 안 함 — 이름 매칭은 LLM에 맡긴다(양보 B) |
| screen-006 | screenNotHallucinated(≠301) | **8/8**(원본이 `301`을 낸 적이 없다 — 대신 화면 안 다른 상품을 확정) | **8/8** | `unknown_product_id_spoken`(항상 발동) |

합계(원본) 3+0+6+6+8+8=31 = `screenPromptLayerHitCount`와 일치한다. screen-006의 "원본" 열은
모델이 `301`을 직접 추출한 적이 없고(항상 화면 안의 다른 상품 — 3101/2001 등 — 을 확정) 대신
**엉뚱한 상품**을 낸다 — #118이 실측한 "301 담아줘 → 6/8 확정, 엉뚱한 상품으로 대체"와 같은
모양이다. 그 값이 `301`이 아니므로 `screenNoHallucination`의 원본 술어(`productId != 301`)는
이미 8/8을 충족하지만, **사용자가 말하지 않은 상품이 담긴다**는 점에서 여전히 위험한 실패다 —
그래서 해소기가 `unknown_product_id_spoken`으로 강제 되물음시켜 애초에 아무것도 담기지 않게
만든다(`screenResolverOverrideCount`에 이 8건이 모두 잡힌다 — screenNoHallucination 축 자체는
원본이든 최종이든 8/8로 같아 보이지만, 실제로 막은 위험은 최종값 쪽에서만 진짜로 사라진다).

## 읽는 법

1. **이관 후에도 #118의 채택 근거가 재현된다.** 종합 47/48(≈#118의 48/48), 안전 셀 8/8, 오담기 0
   — 코드 해소기가 결정적으로 개입하는 4개 셀은 실제로 결정적이었다.
2. **단일 실행은 채택 판정이 아니다**(이 하네스의 공통 함정 #4). 이 런의 목적은 "이관이 #118의
   결론을 뒤집지 않았다"를 확인하는 것이지, 새로운 프롬프트 변경을 채택하는 것이 아니다 — 이
   PR은 `_SYSTEM`을 한 글자도 바꾸지 않았다(§1).
3. **`screenOutOfListConfirmCount: 0`이 이 흡수 작업의 핵심 안전 증거다.** 두 목록 밖 id가 최종
   확정된 표본이 하나도 없다 — 화면에 없는 상품이 장바구니에 담기는 사고가 이 런에서 재현되지
   않았다.
