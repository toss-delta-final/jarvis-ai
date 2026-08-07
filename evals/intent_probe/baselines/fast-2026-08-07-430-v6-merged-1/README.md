# #430 귀속 대조 팔 — 병합판 런 1/2 (픽스처 v6, 2026-08-07)

`_SYSTEM` = **`f99a98867e4a`**(7828자, `origin/dev` 병합 직후 = 브랜드·색상 10자 **없는** 판) ·
`source=repo:_SYSTEM` · 픽스처 v6(85셀).

**왜 커밋하는가**: 출고판(`-v6-adopted-{1,2}`)과 **같은 픽스처에서 `_SYSTEM` 10자만 다른**
대조 팔이다. 이 팔이 없으면 `categoryClear` 31 → 28 의 −3 을 내 10자 탓인지 #386 의 548자
탓인지 가를 수 없다. 측정 방법: `git checkout` 으로 `_SYSTEM` 을 잠시 병합판으로 되돌려
2런을 돌리고, `trap` 으로 출고판을 복원한 뒤 sha 를 재확인했다.

`#240` 축 순서 요약: `239/144/95/28/11/48/26/16`.
`categoryClear` **31/32** · `screenExactPick` 31/32 · `screenOutOfListConfirmCount` 1 ·
`screenNoHallucination` 8/8 · `screenReask` 8/8.
(85셀 · N=8 · 종료 코드 0 · 못 채운 셀 0 · 관측 부분합 USD 0.1532)

전 축 대조표·해석은 `../fast-2026-08-07-430-v6-adopted-1/README.md` 가 정본이다.
