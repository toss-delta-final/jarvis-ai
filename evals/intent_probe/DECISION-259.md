# #259 decompose 라우팅 아키텍처 결정

## 결론

**A. 현행 `fast` 단일 `decompose`를 유지한다.** B. `smart` 전역 상향과 C. intent 전용 1단계
분리는 이번 출고안으로 채택하지 않는다. 이 결정으로 프로덕션 코드·프롬프트·모델 설정은 바뀌지
않는다.

이 결론은 “현행 라우터가 모든 발화에서 98.3% 정확하다”는 뜻이 아니다. `mainIntent`는
**장바구니 대조군 6발화와 지시대명사 4발화만** 센 축이다. 현행 A에는 `general`, pending cart
상품 전환, 옵션 ID, 카테고리 혼합 교체, 찜 해제 음성 대조처럼 별도로 고쳐야 할 약축이 남아 있다.

## 최신 A 실측

- 실행: 2026-08-13, `fast`(`gpt-5-nano`, reasoning `minimal`), 101셀 × N=8
- 성공 표본: 808/808, 미충족 셀 0
- 실행 커밋: `9cd42ba11935e88ccd09ad2e449ba91b047eb0d0` (clean)
- 프롬프트: `repo:_SYSTEM_WITH_DEDICATED_UNDERSPECIFIED`, sha12 `a853c1c4f2be`
- 픽스처: `intent-probe-anchors-b-v8`, sha256 `f5c33d5e9928…`
- 원시 산출물: [`baselines/fast-2026-08-13-259-decision-1/`](baselines/fast-2026-08-13-259-decision-1/)

문서 작성 시점 최신 `origin/dev`(`5221b854839a…`)와 실행 커밋의 차이는
`app/agents/seller/hitl.py`, `tests/unit/test_seller_hitl.py`, `docs/api-spec.md`뿐이다. 구매자
라우팅 코드·프롬프트·intent probe 입력에는 차이가 없어 이 런을 최신 구매자 경로의 A 근거로
사용한다. 단, manifest의 실행 커밋을 최신 HEAD로 바꿔 적지는 않는다.

### 강한 축과 안전축

| 축 | 결과 | 해석 |
|---|---:|---|
| `mainIntent` | 236/240 (98.3%) | 장바구니 대조군 + 지시대명사 한정 |
| `cartControl` | 144/144 (100%) | 기존 완전축 유지 |
| `orderStatus` | 48/48 (100%) | 세 컨텍스트에서 완전축 |
| `screenResolution` | 48/48 (100%) | exact/reask/no-hallucination 합계 |
| `cartQuantityRouting` | 48/48 (100%) | 양성·음성 대조 모두 통과 |
| `wishlistViewRouting` | 48/48 (100%) | 양성·no-steal 모두 통과 |
| `namedCategoryHasLeg` | 48/48 (100%) | 명시 상품군 leg 보존 |
| `categoryClear` | 32/32 (100%) | 카테고리 무관 리셋 |

### 숨기지 않는 약축

| 축 | 결과 | 실패 모양 |
|---|---:|---|
| `general` | 31/48 (64.6%) | `안녕`이 last recommendations·pending cart와 함께 들어오면 추천으로 끌림 |
| `switchLegacy2` | 8/16 (50.0%) | `다른 거 담아줘` 7/8이 되물음 상품 102를 그대로 에코 |
| `switchAll7` | 40/56 (71.4%) | pending cart에서 새 상품 전환이 불안정 |
| `optionAnswer` | 28/32 (87.5%) | intent는 맞아도 `optionId`가 틀린 표본 포함 |
| `categoryMixedReplace` | 24/32 (75.0%) | `스피커 아무거나`류 일부를 replace 대신 clear로 판정 |
| `wishlistRemoveRouting` | 24/32 (75.0%) | 음식명 `찜닭 빼줘`를 8/8 `wishlist_remove`로 훔침 |

특히 `cartAddProductIdLegacy2` 16/16은 성공으로 인용하지 않는다. 이 축은 되물음 상품을 그대로
에코해도 맞다고 세므로, 위험 진단 `reaskProductEchoCount=12`와 함께 봐야 한다.

같은 v8 픽스처·같은 프롬프트·같은 모델 설정으로 이미 저장된 두 런과 최신 런을 나란히 보면
핵심축은 안정적이고 약축은 반복된다. 커밋 시점이 다르므로 인과 대조가 아니라 분포 확인용이다.

| 축 | 2026-08-11 run 1 | 2026-08-11 run 2 | 최신 A |
|---|---:|---:|---:|
| `mainIntent` /240 | 237 | 236 | 236 |
| `cartControl` /144 | 144 | 144 | 144 |
| `general` /48 | 31 | 30 | 31 |
| `optionAnswer` /32 | 30 | 28 | 28 |
| `switchLegacy2` /16 | 6 | 10 | 8 |
| `categoryMixedReplace` /32 | 21 | 19 | 24 |
| `wishlistRemoveRouting` /32 | 24 | 24 | 24 |
| `screenResolution` /48 | 47 | 48 | 48 |

앞선 두 런은 `candidates/fast-2026-08-11-463-gate-after-{1,2}`에 보존돼 있다.

## A/B/C 판정

아래 B/C 수치는 이슈 #259에 기록된 기존 실험이다. 최신 v8 정답지로 다시 잰 값이 아니므로 최신
A와 단순 차감하지 않는다. 이번 결정은 새 안을 승격하는 실험이 아니라, 기존 비교와 최신 A
재확인을 합쳐 출고 구성을 고정하는 결정이다.

| 안 | 확인된 이점 | 확인된 손실·위험 | 판정 |
|---|---|---|---|
| A. 현행 `fast` | 핵심축 98.3%, 장바구니·화면 안전축 100%, 추가 호출 없음 | 약축이 남음 | **유지** |
| B. `smart` 전역 상향 | 기존 실험에서 본 표·`general` 100% | `decompose` 중앙값 2.05→3.30초(+1.25초), 매 턴 상위 티어 비용, 지목 전환 동작 변경 | 기각 |
| C. intent 전용 1단계 | 1단계 프롬프트 축소, 기존 실험 중앙값 0.92초 | 장바구니 대조군 88.9·90.3%, 전환 25·6.2%; 컨텍스트가 intent 정의 자체인 발화는 목록 제거 불가; recommend/cart_add는 2차 호출 필요 | 기각 |

### 왜 약축이 있는데도 A인가

1. B의 이득은 전역 비용·첫 SSE 이전 지연과 교환된다. 모든 구매자 턴에 적용하기에는 문제 범위보다
   변경 범위가 크다.
2. C의 실패는 프롬프트 길이만의 문제가 아니다. `이어폰으로 할래`처럼 pending cart와 추천 목록이
   있어야만 `cart_add`로 정의되는 intent가 있어 “맥락 없는 라우팅” 경계가 성립하지 않는다.
3. 최신 A의 실패는 서로 다른 의미 문제다. greeting context gating, 상품 전환 해소, 옵션 ID 매핑,
   카테고리 scope, `찜` 어휘 경계는 하나의 모델 상향이나 2단계 분리로 묶기보다 표적 평가로 각각
   고치는 편이 회귀 범위가 작다.
4. A 유지에는 프로덕션 변경이 없어 새로운 지연·비용·그래프 회귀가 없다.

## 지연·토큰·비용을 읽는 법

최신 런의 `samples.csv` `latencyMs`는 808표본에서 min 1.390초, median 2.019초, mean 6.194초,
p95 39.907초, max 45.773초였다. 그러나 동시 실행 중 전역 페이서 대기를 포함해 꼬리가 부풀었다
(`waitCount=708`, 총 대기 1,060.437초). 따라서 이 값은 **probe 호출 wall time**이지 사용자
채팅의 E2E 응답 시간·TTFT가 아니다. A는 설정을 바꾸지 않으므로 이 PR의 증분 지연은 0이지만,
절대 사용자 지연 주장은 #151 방식의 별도 E2E 측정이 필요하다.

manifest는 provider 호출 957회에서 `$0.20297255`, 3,184,303 tokens를 기록했다. 다만 212회는
cost와 token이 모두 unknown이라 이 합계는 완전한 총비용이 아니다. B/C의 최신 절대비용 비교로
사용하지 않는다. 다시 B/C를 검토할 때는 동일 v8·동일 반복 수로 세 팔을 재실행하고, unknown 0인
usage와 E2E TTFT·total을 함께 제출해야 한다.

## 후속 작업 경계

이 결정 PR에는 아래 수정들을 섞지 않는다.

1. `general`: 무관한 last recommendations·pending cart가 greeting을 오염시키지 않는 gating
2. 상품 전환: 되물음 상품 에코를 성공으로 세지 않는 resolver와 회귀셋
3. 옵션 답변: intent와 `optionId`를 분리해 평가·수정
4. 카테고리 혼합 교체: 명시 상품군 + `아무거나`에서 replace 보존
5. 찜 해제: `찜닭`과 `찜` 명령의 lexical boundary

각 수정은 해당 약축을 2~3회 반복 측정하고 강한 축을 회귀 게이트로 둔다. 전역 B/C 재검토는 이
표적 수정 후에도 잔여 실패가 넓게 남을 때만 다시 연다.

## 발표용 한 문장

> 라우팅을 무조건 큰 모델로 올리거나 2단계로 쪼개지 않고, 808개 실 LLM 표본에서 핵심·안전축을
> 지키는 현행 fast 구조를 유지했다. 대신 64.6~87.5%에 머문 다섯 약축을 숨기지 않고 다음 표적
> 실험으로 분리했다.
