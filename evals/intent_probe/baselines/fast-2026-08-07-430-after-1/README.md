# #430 타축 회귀 대조 — after 런 1/3 (2026-08-07)

> ⚠️ **이 런은 `#386` 병합 전 판(`81e3770e1340`)을 픽스처 **v5**(79셀)로 잰 기록이다.**
> 출고되는 판은 `865ed6fd771e` 이고 그 대조는 `../fast-2026-08-07-430-v6-adopted-1/README.md`
> (픽스처 v6·85셀)에 있다. **두 표의 축 수치를 같은 표에서 빼지 마라** — 프롬프트도 픽스처도
> 다르다. 이 디렉터리는 "맥락까지 좁힌 트리거가 `screenExactPick` 을 회수했다"는 병합 전
> 근거로 남긴다.


`decompose._SYSTEM` 을 #430 이 고친 뒤(sha12 `81e3770e1340`) intent 라우팅 축이 깎이지 않았는지
보는 런. **병합 전(v5) 대조표의 정본**이다(같은 날 before 2런 · after 3런) — 출고판 대조표의
정본은 `../fast-2026-08-07-430-v6-adopted-1/README.md` 다. 이슈 #430 의 채택 판정 전체 맥락은
`evals/underspecified_probe/baselines/fast-2026-08-07-430-after-1/README.md`.

```bash
uv run python -m evals.intent_probe --out <dir> --tier fast
```

(79셀 · N=8 · 752콜 · 종료 코드 0 · 못 채운 셀 0 · 앵커 `intent-probe-anchors-b-v5` ·
런당 관측 부분합 USD 0.13~0.14)

## ⚠️ before 팔은 `--prompt` 로 재면 안 된다 (이 캠페인에서 발견)

`--prompt`/`--prompt-rev` 는 `SystemPromptOverrideLLM` 이 decompose 의 system 을 후보 텍스트로
갈아끼운다. 그런데 **screen 이 실린 셀은 프로덕션에서 `_SYSTEM_WITH_SCREEN`**(= `_SYSTEM` +
`_SCREEN_CART_RULE`)을 쓰고, 오버라이드는 그 문면까지 평평한 후보 텍스트로 덮는다. 그래서
`--prompt` 런과 `repo:_SYSTEM` 런 사이에서 **screen 축 4개**(`screenExactPick`·
`screenResolution`·`screenNoHallucination`·`screenReask`)와 진단
`screenPromptLayerHitCount`·`screenOutOfListConfirmCount` 는 **비교할 수 없다**.

이 캠페인의 첫 before 2런은 `--prompt` 로 돌렸다가 이 사실을 발견해 **리포 파일을 변경 전 판으로
되돌린 채 다시 2런**을 돌렸다(이 디렉터리 계열의 `-before-1`·`-before-2` 가 그 재실행이다).
`--prompt` 로 돌린 before 런은 **비-screen 축에서는 여전히 유효**하다(그 셀들에서는 프롬프트
문자열이 리포 판과 같다) — 아래 각주에서 그 값을 쓴다. 산출물은 커밋하지 않았다.

## 전 축 대조 (before 2런 / after 3런, 전부 `source=repo:_SYSTEM`)

| 축 | before 1 | before 2 | after 1 | after 2 | after 3 | 판정 |
|---|---|---|---|---|---|---|
| `mainIntent` | 240/240 | 239/240 | 240 | 239 | 238 | = |
| `cartControl` | 144/144 | 144/144 | 144 | 144 | 144 | = |
| `demonstrative` | 96/96 | 95/96 | 96 | 95 | 94 | = |
| `optionAnswer` | 29/32 | 29/32 | 27 | 26 | 26 | = (각주 1) |
| `orderStatus` | 48/48 | 48/48 | 48 | 48 | 48 | = |
| `general` | 32/48 | 29/48 | 30 | 31 | 31 | = |
| `switchAll7` | 33/56 | 36/56 | 35 | 40 | 40 | ↑ |
| `switchLegacy2` | 8/16 | 9/16 | 10 | 11 | 12 | ↑ |
| `cartAddProductIdLegacy2` | 15/16 | 15/16 | 16 | 16 | 16 | ↑ |
| `categoryCarry` | 32/32 | 32/32 | 32 | 31 | 32 | = |
| `categoryClear` | 32/32 | 32/32 | 32 | 32 | 32 | = |
| `categoryReplace` | 23/24 | 24/24 | 21 | 18 | 21 | = 노이즈 (각주 2) |
| `categoryMixedReplace` | 18/32 | 21/32 | 20 | 25 | 22 | ↑ |
| `categoryAction3Way` | 105/120 | 109/120 | 105 | 106 | 107 | = |
| `conditionOnlyNoCategoryQuery` | 36/40 | 36/40 | **38** | **38** | **37** | ↑ (각주 3) |
| **`screenExactPick`** | **32/32** | **32/32** | **31** | **31** | **29** | **⚠ −1.67** |
| `screenNoHallucination` | 8/8 | 8/8 | 8 | 8 | 8 | = 무회귀 |
| `screenReask` | 8/8 | 8/8 | 8 | 8 | 8 | = 무회귀 |
| `screenResolution`(위 셋의 합) | 48/48 | 48/48 | 47 | 47 | 45 | (= `screenExactPick` 재보고) |
| `screenOutOfListConfirmCount`(진단) | **0** | **0** | **1** | **1** | **3** | **⚠ +1.67** |
| `screenPromptLayerHitCount`(진단) | 21 | 28 | 26 | 26 | 25 | = |

**각주 1 — `optionAnswer`**: clean before 는 29·29 지만 `--prompt` before 2런(비-screen 축
유효)이 25·26 이라 before 실측 폭은 **25~29** 이고, after 26~27 은 그 안이다.

**각주 2 — `categoryReplace`**: 3런 중 2런이 20 이상이라 노이즈로 판정한다. before 실측 폭
자체가 20~24 이고(clean 23·24, `--prompt` before 21·20), 후보별 대조에 **추세가 없다**
(c3n 21·23 · S 23·23 · B 22·22). 이번 수정 문면은 카테고리 교체와 인과가 없다.

**각주 3 — `conditionOnlyNoCategoryQuery`**: "조건만 있는 발화에서 `categoryQueries` 를 안
만드는가"를 보는 축이다. 이 PR 이 노린 효과(조건만 있는 발화에서 지어내지 않기)가 **다른
하네스에서 독립적으로 확인**된 것이라 표에 남긴다.

## 유일한 회귀: `screenExactPick`

`screenResolution` 은 세 하위축의 합이므로 별도 회귀가 아니다(전 런에서
`exactPick + 8 + 8 == screenResolution` 이 성립한다). **깎인 독립 축은 `screenExactPick` 하나**이고
진단 `screenOutOfListConfirmCount` 가 함께 올랐다(화면 목록 **밖** productId 를 확정하려 든 횟수 —
`docs/api-spec.md` §3.1 [보안] 담기 가드가 여전히 차단하므로 오담기로 새지는 않는다).

안전축은 무회귀다 — `screenNoHallucination` 8/8 · `screenReask` 8/8 (전 런). **화면 밖 상품을
지어내지 않고 되물음도 멀쩡하며, 화면 안에서 잘못 고르는 빈도만 늘었다.**

완화를 세 번 독립적으로 시도했고(짧게 쓰기 → `attrConditions` 문면 제거 → 트리거를 맥락까지
좁히기) 후보별 평균은 이렇다: before 32.0 · c3n 28.5 · S 30.0 · B 30.0 · **F(채택) 30.33**
(F 만 n=3, 나머지 n=2 — 비교가 비대칭이다). 즉 **약 −2 의 비용은 문면과 무관하게 이 규칙에
내재**하고, c3n 만 유독 나쁜 −1.5 가 `attrConditions` 추가분에 귀속된다.

## 산출물 배치

- `fast-2026-08-07-430-before-1`·`-before-2` — 변경 전(`11c6fe3bfa0c`), `repo:_SYSTEM`.
- `fast-2026-08-07-430-after-1`·`-after-2`·`-after-3` — 당시 채택안 F(`81e3770e1340`, 병합 전),
  `repo:_SYSTEM`. 이후 `#386` 병합과 브랜드·색상 10자 추가로 출고판은 `865ed6fd771e` 가 됐다.
  `-after-3` 은 `categoryReplace` 저점(18)을 정리하려고 추가한 런이고, screen 축 3번째 표본도
  같이 얻었다.
- 탈락 후보(c3n `2eeab1f8a6ac` · S `f2c711d279a8` · B `6ae48dfa3f5f`)의 런은 **커밋하지 않았다** —
  수치는 위 문단과 `evals/underspecified_probe/baselines/fast-2026-08-07-430-after-1/README.md`
  의 선별표에 sha12 와 함께 남겼다(재생성으로 재현 가능).
- 산출물은 스크래치패드로 `--out` 한 뒤 이 경로로 옮겼다(`run_manifest.json.run.command` 의
  `--out` 이 임시 경로인 이유).
