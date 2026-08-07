# #430 타축 회귀 대조 — **출고판** 런 1/2 (픽스처 v6, 2026-08-07)

`decompose._SYSTEM` = **`865ed6fd771e`**(7838자, 출고되는 판) · `source=repo:_SYSTEM` ·
픽스처 **`intent-probe-anchors-b-v6`**(85셀).
**이 디렉터리 README 가 v6 대조표의 정본**이다. `#430` 전체 맥락은
`evals/underspecified_probe/baselines/fast-2026-08-07-430-after-1/README.md`.

```bash
uv run python -m evals.intent_probe --out <dir> --tier fast
```

(85셀 · N=8 · 종료 코드 0 · 못 채운 셀 0 · 런당 관측 부분합 USD 0.15)

## ⚠️ 픽스처 v5 런과 축 수치를 같은 표에서 빼지 마라

이 디렉터리 계열(`-v6-*`)은 **v6**(85셀), 같은 날의 `-before-{1,2}`·`-after-{1,2,3}` 는
**v5**(79셀)다. v6 는 #386 이 `wishlist_view` 발화 **6건을 추가만** 했고 기존 발화는 **0건
변경**이라(확인함) `categoryClear` 등 기존 축의 **셀 입력은 동일**하다 — 그래서 아래 §「귀속」의
−3 은 픽스처 탓이 아니다. 그래도 **분모가 늘어난 축이 있으므로** v5 표와 직접 뺄셈하지 말 것.

## 귀속 — 같은 픽스처에서 프롬프트 10자만 다른 대조

`-v6-merged-{1,2}` 는 **병합판 `f99a98867e4a`**(#386 의 548자 포함, 브랜드·색상 10자 **없음**)를
잰 팔이다. 두 팔은 픽스처·모델·앵커·N 이 전부 같고 **`_SYSTEM` 이 10자만 다르다** — 이 캠페인에서
얻은 가장 깨끗한 귀속이다.

| 축 | 병합판(2런) | **출고판(2런)** | Δ |
|---|---|---|---|
| **`categoryClear`** | **31 · 31** | **28 · 28** | **−3.0** |
| `demonstrative` | 95 · 94 | 92 · 91 | −3.0 |
| `mainIntent` | 239 · 238 | 236 · 235 | −3.0 |
| `optionAnswer` | 28 · 30 | 26 · 31 | −0.5 |
| `screenExactPick` | 31 · 31 | 31 · 30 | −0.5 |
| `screenResolution`(하위 3축 합) | 47 · 47 | 47 · 46 | −0.5 |
| `screenNoHallucination` | 8 · 8 | 8 · 8 | 0 |
| `screenReask` | 8 · 8 | 8 · 8 | 0 |
| `screenOutOfListConfirmCount`(진단) | 1 · 1 | 1 · 2 | +0.5 |
| `cartControl` | 144 · 144 | 144 · 144 | 0 |
| `orderStatus` | 48 · 48 | 48 · 48 | 0 |
| `wishlistViewPositive` | 24 · 24 | 24 · 24 | 0 |
| `cartAddProductIdLegacy2` | 16 · 15 | 16 · 16 | +0.5 |
| `wishlistViewNoSteal` | 24 · 22 | 24 · 24 | +1.0 |
| `wishlistViewRouting` | 48 · 46 | 48 · 48 | +1.0 |
| `categoryCarry` | 31 · 30 | 32 · 32 | +1.5 |
| `switchLegacy2` | 11 · 9 | 11 · 12 | +1.5 |
| `switchAll7` | 45 · 43 | 46 · 46 | +2.0 |
| `categoryReplace` | 19 · 22 | 23 · 23 | +2.5 |
| `conditionOnlyNoCategoryQuery` | 35 · 37 | 40 · 38 | +3.0 |
| `categoryMixedReplace` | 24 · 22 | 26 · 27 | +3.5 |
| `general` | 26 · 28 | 30 · 31 | +3.5 |
| `categoryAction3Way` | 105 · 105 | 109 · 110 | +4.5 |

**10축 상승 / 3축 하락.** 하락 폭은 이 하네스 노이즈 대역(README ±2, before 런 자체가
`switchAll7` 38↔32 로 ±6 스윙)과 겹치지만 **`categoryClear` 는 팔 내부 분산이 0**
(31·31 / 28·28)이라 신호일 가능성이 높다 — 숨기지 않고 싣는다.

기전 가설(실측 아님): 색상은 `filters.color` 로 가는 축인데 그것을 비움 트리거의 **단서**로
격상시켰으므로, 리셋 발화("5만원 이하 아무거나")에서 "단서 있음"으로 읽혀 카테고리를 놓겠다는
판정이 흔들릴 수 있다. `demonstrative`·`mainIntent` 도 같은 −3 이라 **한 원인이 여러 축에
비치는** 것으로 보인다(지시대명사 발화는 발화에 상품 의미가 없고 맥락에만 있는 부류라 비움
트리거와 겹친다).

그래도 **+10자는 순손실이 아니다** — `general` 은 #386 이 26·28 로 떨어뜨린 것을 이 10자가
30·31(= 병합 전 v5 수준)로 되돌렸고, 카테고리 3분기 축들이 함께 올랐다. 이 10자가 산 것은
`underspecified_probe` 의 오탐율이다(1.9→3.8→4.8% → 1.9·2.9%).

## `#386` 병합 전 v5 기록

`-before-{1,2}`(`11c6fe3bfa0c`)·`-after-{1,2,3}`(`81e3770e1340`)는 **병합 전** 프롬프트를
**v5** 픽스처로 잰 기록이다. 지우지 않았다 — 그 표가 "맥락까지 좁힌 트리거가 `screenExactPick`
을 회수했다"는 근거이고, 이 PR 이 v5 시절에 통과한 판정의 원본이다.

## 한계

**PR 수준의 진짜 before(순수 `origin/dev` = `e62fd0f6e03d`)를 v6 로 잰 팔은 없다**(예산 소진).
그래서 "이 PR 전체가 타축에 미친 영향"은 v5 대조(`11c6fe3bfa0c` vs `81e3770e1340`)와 v6 대조
(`f99a98867e4a` vs `865ed6fd771e`) **두 조각으로만** 안다.
