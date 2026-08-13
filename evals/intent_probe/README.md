# intent 라우팅 프로브 (#260)

`decompose` 가 발화를 **어느 intent 로 보내는지**의 안정성을 실 LLM 반복 분포로 잰다.
발화×컨텍스트(셀)마다 N회 호출해 정답 비율을 축별로 집계한다.

## 이건 골든셋이 아니다

| | `evals/goldenset` | 이 프로브 |
|---|---|---|
| 본체 | **추천 품질**(idealOrder·expectedFilters·hardConstraints) | **intent 라우팅 안정성** |
| 평가 | 결정론 1회, nDCG·MRR·P@k | **확률 분포**(발화×컨텍스트당 N회) |
| 세션 상태 | 없음(단발 질의) | `PENDING_CART`·`LAST_RECOMMENDATIONS` 를 채운 멀티턴 |
| 봉인 | dev/holdout 봉인·누출 감사 | 불필요(정답이 자명해 봉인할 라벨이 아님) |

두 산출물의 숫자를 섞지 말 것. 골든셋 케이스의 `expectedRoute` 는 추천 질의의 부수 라벨이고
**되물음 맥락이 없다.**

## 실행

```bash
# 오늘의 기준선 (현재 _SYSTEM, fast 티어)
uv run python -m evals.intent_probe --out artifacts/intent-probe/run1 --tier fast

# 후보 프롬프트 재기 — 리포를 더럽히지 않고 파일로 갈아끼운다
uv run python -m evals.intent_probe --out artifacts/cand1 --prompt cand1.txt

# 과거 판 프롬프트 재기 (#240 기준선 e5e195822495 는 커밋 3f1dec7)
uv run python -m evals.intent_probe --out artifacts/base240 --prompt-rev 3f1dec7

# 티어 비교 (#259 근거)
uv run python -m evals.intent_probe --out artifacts/smart --tier smart

# 정답지를 바꾸면 결과가 달라진다는 사실 자체를 보이기
uv run python -m evals.intent_probe --out artifacts/fixA --fixture a

# API 없이 배관만 확인 (가짜 LLM)
uv run python -m evals.intent_probe --out /tmp/probe --dry-run

# 카테고리 범위 해제 분류기를 끄고 재기(#84 결함 재현 팔) — scopeFree 는 전부 None 이 된다
uv run python -m evals.intent_probe --out artifacts/no-scope --no-classifier

# 현재 _SYSTEM 을 바이트 그대로 받아 두기(--prompt 로 왕복하면 해시가 같다)
uv run python -m evals.intent_probe --dump-prompt system.txt
```

기본 규모: 91셀 × N=8 = 728콜(decompose) **+ 120콜**(카테고리 15셀 × N — 범위 해제 분류기)
= **848콜**, 45rpm 페이서라 런당 약 19~21분.
`fast`(gpt-5-nano) 기준 런당 대략 USD 0.12 — 2026-08-04 실측($0.086 / 1.27M tokens, 424콜)을
콜 수 비례로 환산한 추정이다(#84 이 카테고리 11셀을 더해 424 → 512, #300 이 screen 6셀을 더해
512 → 592, #344 라운드 2 가 조건 전용 5셀을 더해 592 → 632, **#386 이 찜 조회 6셀을 더해
632 → 680**[이 문단이 그 갱신을 놓치고 있었다 — 이번에 바로잡는다], **#443 이 상품군 명시
6셀을 더해 680 → 728**). 분류기 88콜은 프롬프트가 짧고(`max_tokens=32`) 콜당 ≈0.35k 라
비용·TPM 영향이 작지만 **페이서는 지나므로**(rpm 예산에 포함) 위 소요 추정에는 넣었다.
**screen 6셀·조건 전용 5셀·찜 조회 6셀·상품군 명시 6셀은 분류기를 태우지 않는다** — 전부
`priorFilters` 에 `category` 가 없어 게이트(`prior_category` 가 있어야 호출)가 열리지 않는다
(D-6 실측 확인, `baselines/fast-2026-08-05-300-screen/`).

## ⚠️ `--prompt`/`--prompt-rev` 런은 screen 축을 재지 못한다

`SystemPromptOverrideLLM` 은 통과하는 decompose `complete` 의 system 을 후보 텍스트로
갈아끼우는데, **screen 이 실린 셀은 프로덕션에서 `_SYSTEM_WITH_SCREEN`**(= `_SYSTEM` +
`_SCREEN_CART_RULE`)을 쓴다. 오버라이드가 그 문면까지 평평한 후보 텍스트로 덮으므로,
`--prompt` 런의 **screen 축 4개**(`screenExactPick`·`screenResolution`·
`screenNoHallucination`·`screenReask`)와 진단 `screenPromptLayerHitCount`·
`screenOutOfListConfirmCount` 는 `repo:_SYSTEM` 런과 **비교할 수 없다**(#430 에서 실측 발견 —
같은 before 프롬프트인데 `screenPromptLayerHitCount` 가 16·18 대 21·28 로 갈렸다).

**screen 축이 걸린 후보를 잴 때는 후보를 리포 `_SYSTEM` 에 넣고 `--prompt` 없이 돌려라**
(`prompt.source` 가 `repo:_SYSTEM` 이어야 한다). 비-screen 축만 볼 때는 `--prompt` 런이 그대로
유효하다 — 그 셀들에서는 프롬프트 문자열이 리포 판과 같다.

## 기준선

`baselines/fast-2026-08-13-259-decision-1/` — **#259 A/B/C 결정용 최신 A 재확인 런**.
픽스처 v8(101셀) × N=8의 성공 표본 808개를 모두 채웠고, `mainIntent` 236/240 ·
`cartControl` 144/144 · `screenResolution` 48/48이었다. 동시에 `general` 31/48 ·
`switchLegacy2` 8/16 · `optionAnswer` 28/32 · `categoryMixedReplace` 24/32 ·
`wishlistRemoveRouting` 24/32의 약축도 확인했다. **현행 A(`fast`) 유지, B(`smart`)와 C(분리)
미채택**의 해석·비용/지연 한계는 [`DECISION-259.md`](DECISION-259.md)가 정본이다. 이 PR은
프로덕션 코드·프롬프트·설정을 바꾸지 않는다.

`baselines/fast-2026-08-04/` — 현재 `_SYSTEM`(`e5e7f9b8d844`) × `fast`(gpt-5-nano) × 앵커 B.
후보를 잴 때 이 표와 대조한다. **단일 실행이라 채택 판정의 근거가 아니다** — 축당 ±2 흔들린다.

`baselines/fast-2026-08-04-prompt-e5e19582/` — 같은 앵커를 **#240 이 쟀던 프롬프트**로 다시 잰
하네스 검산 런(`--prompt-rev 3f1dec7`). 8축 중 **5축이 #240 표와 정확히 일치**하고
(`237/144/93/27/48`), 어긋난 3축은 전부 재구성한 부분에 걸려 있다 — 자세한 해석은 그 디렉터리의
README 에 있다.

`baselines/fast-2026-08-05-84/` — **#84 출고 구성 대조쌍**을 픽스처 **v3**(68셀)로 잰 두 런.
루트가 출고 구성이고 `no-classifier/` 가 **결함 재현 팔**이다 — **같은 커밋·같은 프롬프트에서
`--no-classifier` 플래그 하나만 다르다.** 분류기를 끄면 `categoryClear` **0/32**, 켜면 **32/32**
이고 기존 8축은 `general`(34 ↔ 31) 말고 **전부 같은 숫자**다(결함의 재현·해소·회귀 부재가 한 쌍으로
나온다). 그 디렉터리 README 에 전 축 대조표 · 혼합 발화 21/32 의 분해(새 leg 가 있던 15건은 100%
replace, 남은 11건은 `decompose` 추출 실패) · 인라인 `categoryAction` 필드를 기각한 근거(전환 축
37·38 → 32·32)가 있다 — **프롬프트를 고치기 전에 그 표를 볼 것.**
위 두 기준선은 픽스처 v1 이라 이 표와 직접 비교하지 않는다.

**[#386] 찜 조회 축 실측(2026-08-07, 픽스처 v6 · 85셀 · `fast` · N=8, 산출물 미커밋)** —
전량 재실행 1회. 신규 축은 `wishlistViewPositive` **24/24** · `wishlistViewNoSteal` **23/24** ·
`wishlistViewRouting` **47/48**.

- 양성 3발화(`내가 뭐 찜했지?`·`찜한 거 보여줘`·`위시리스트 뭐 있어?`)가 **각각 8/8** 로
  `wishlist_view` 에 갔다.
- `noSteal` 의 1건 미달은 `뭐 있어?` 가 `general` 로 간 것이고 **`wishlist_view` 로 샌 것이
  아니다** — 이 축이 재려던 "새 의도가 남의 발화를 훔치는가"는 **0건**이다.
  `보여줘` 8/8 recommend · `찜한 거 담아줘` 8/8 cart_add(강한 신호 우선 유지).
- 기존 8축은 **`fast-2026-08-05-84`(직전 비교 가능 기준선)** 대비 `mainIntent`·`demonstrative`·
  `general` 이 **완전히 동일**, `switchAll7` −1. `switchLegacy2`(9→6)·
  `cartAddProductIdLegacy2`(14→12)는 **같은 2발화를 다르게 세는 축**이라 실질적으로 한 신호이며,
  흔들린 `switch-001`(`이어폰으로 할래`)에는 `찜`·`위시리스트` 토큰이 없어 #386 이 더한 규칙
  (그 토큰이 있을 때만 발동)의 사정거리 밖이다. 분모 16 · N=8 에서 −3 은 "축당 ±2" 노이즈의
  경계라 **단정하지 않고 관찰 항목으로 남긴다**(재현 함정 4 — 단일 실행으로 판정 금지).
- ⚠️ 이 런은 픽스처 파일이 CRLF 이던 시점에 돌았다(내용 동일, 줄바꿈만 다름) — 산출물의
  `fixtureSha256` 은 LF 정규화 **전** 값이라 현재 커밋본 해시와 다르다.

`baselines/fast-2026-08-05-300-screen/` — **#300 이 흡수한 screen 6셀만** 잰 실측(전량
재실행이 아니다). 픽스처 **v4**(46발화·9컨텍스트) × `fast` × N=8 × 6셀 = 48콜. 이관 전
별도 프로브(#118, PR #292 — #300 이 흡수하며 삭제했다)의 채택 근거(48/48 · 안전 셀 8/8 ·
오담기 0)를 `screenResolution` 47/48 · `screenReask` 8/8 · `screenNoHallucination` 8/8 로
재현한다. 그 디렉터리 README 에 셀별 원본(해소기 전) vs 최종값 대조표가 있다.

`baselines/fast-2026-08-07-430-{before-1,before-2,after-1,after-2,after-3}/` — **#430
(decompose 프롬프트 수정)의 타축 회귀 대조, 픽스처 v5(79셀)·`#386` 병합 전**. 전부
`source=repo:_SYSTEM`(위 ⚠️ 절 참조 — screen 축 때문에 `--prompt` 로 잴 수 없었다).
before `11c6fe3bfa0c` 2런 vs after `81e3770e1340` 3런. **깎인 독립 축은 `screenExactPick`
하나**(32·32 → 31·31·29, 진단 `screenOutOfListConfirmCount` 0·0 → 1·1·3)이고 안전축
`screenNoHallucination`·`screenReask` 는 전 런 8/8 무회귀다.
`conditionOnlyNoCategoryQuery`·`switchAll7`·`categoryMixedReplace` 등은 개선됐다.

`baselines/fast-2026-08-07-430-v6-{merged-1,merged-2,adopted-1,adopted-2}/` — 같은 이슈의
**출고판 대조, 픽스처 v6(85셀)**. `merged-*` 는 `#386`(PR #441) 병합 직후 판(`f99a98867e4a`),
`adopted-*` 는 **출고판**(`865ed6fd771e`) — 두 팔은 픽스처·모델·앵커·N 이 전부 같고 `_SYSTEM`
이 **10자만** 다르다(비움 트리거 단서 목록의 `·브랜드·색상`). 그래서 이 대조는 인과가 깨끗하다:
`categoryClear` **31·31 → 28·28(−3)** · `demonstrative`·`mainIntent` 각 −3 · `screenExactPick`
31·31 → 31·30 · 안전축 무회귀 · 반대로 `categoryAction3Way` +4.5 · `general` +3.5 ·
`categoryMixedReplace` +3.5 · `conditionOnlyNoCategoryQuery` +3.0 등 **10축 상승**.
전 축 대조표는 `fast-2026-08-07-430-v6-adopted-1/README.md` 가 정본이다.

⚠️ **v5 표와 v6 표의 축 수치를 같은 표에서 빼지 마라** — 프롬프트도 픽스처도 다르다. 다만 v6 는
#386 이 `wishlist_view` 발화 **6건을 추가만** 했고 기존 발화는 **0건 변경**이라(확인함) 기존 축의
셀 입력은 동일하다 — 위 `categoryClear` −3 이 픽스처 탓이 아닌 근거가 그것이다.
**PR 수준의 진짜 before(순수 dev `e62fd0f6e03d`)를 v6 로 잰 팔은 없다**(예산 소진).

## CI 에서 돌리지 않는다

실 LLM 호출이라 비용·비결정론이 붙는다. **수동 실행 도구**이며, 프롬프트를 바꾸는 PR 이
산출물(`report.md`)을 근거로 첨부한다(#240·#234 가 그렇게 했다).
`tests/unit/test_intent_probe_*.py` 는 전부 가짜 LLM이라 CI 에서 API 콜이 0이다.

## 재현 함정 4가지 (#240 에서 실제로 밟은 것들)

1. **전역 페이서 필수** — org 500 RPM / 200k TPM 이라 **TPM 이 먼저 묶는다.** 없이 돌리면
   429 로 표본이 비고, 그 빈 칸을 오답으로 세면 분포가 무의미해진다(#240 초기 2런 폐기).
   **토큰 추정은 `max_tokens` 예약분까지 세야 한다** — provider 는 응답 실사용이 아니라 요청이
   예약한 상한까지 TPM 에 넣는다. 2026-08-04 기준선 런에서 3.1k 로 잡고 50rpm 을 돌렸다가
   `Limit 200000, Used 200000` 429 를 78회 먹었다(재시도가 흡수해 셀은 다 찼다). 지금 기본값은
   콜당 3.9k(=3.1k + max_tokens 800) · 45rpm ≈ 175k TPM 이다.
2. **실패는 표본이 아니다** — 성공할 때까지 재시도해 N 을 채운다. 못 채운 셀은
   `results.json.unfilledCells` 와 `report.md` 에 드러나며 **종료 코드 4** 가 된다.
3. **픽스처 문자열이 정답 신호와 겹치면 안 된다** — 되물음 상품명에 옵션 이름("드럼")이 들어가
   `일반형` 답변이 8/8 오답이던 사고가 있었다. 프롬프트 결함이 아니라 **픽스처 결함**이었고,
   지금은 `schema.py` 검증자가 그런 앵커를 아예 커밋하지 못하게 막는다.
4. **단일 실행으로 판정 금지** — 같은 프롬프트 해시의 독립 실행에서 축당 ±2, 특정 셀은 2/8~6/8
   까지 흔들린다. 채택 판정은 **독립 2~3회** 분포로 한다.

## 앵커(정답지)

`fixtures/anchors_b.json`(기본) / `fixtures/anchors_a.json`. 스크립트는 이 파일만 읽는다.

- 발화 63개 — 장바구니 대조군 6 · 지시대명사 4 · 옵션 답변 4 · 전환 7 · order_status 2 · general 2
  · **카테고리 승계 15**(리파인 4 · 리셋 4 · 교체 3 · **혼합 4**, #84) · **screen 지시어 해소 6**
  (확정 4 · 되물음 1 · 확정금지 1, #300 — #118 이관) · **조건 전용 5**(#344 라운드 2 — 카테고리
  어휘 없이 조건만 말하는 턴이 `categoryQueries` 를 비우는지) · **찜 목록 조회 6**(양성 3 · 음성
  대조 3, #386) · **상품군 명시 6**(대조군 1 · 상황 선행 1 · 상황 후행 1 · 추상도 1 · 일반화 1 ·
  수식어 1, #443 — 조건 전용의 반대 방향)[이 목록이 #386 을 51 → 57 로 반영하지 못한 채였다 —
  이번에 57 → 63 과 함께 바로잡는다]
- 컨텍스트 9종 — `none` / `lastRecommendations` / `pendingCart` / `categoryPrior` /
  **`screenSingle`/`screenTriple`/`screenFive`/`screenNine`/`screenNamed`**(#300)
- **group → 허용 컨텍스트 매핑(#313)** — 어떤 group 이 어떤 컨텍스트를 선언할 수 있는지는
  `schema.py` 의 `GROUP_ALLOWED_CONTEXTS` 가 데이터로 강제한다:

  | group | 허용 컨텍스트 |
  |---|---|
  | `option_answer` · `switch` | `pendingCart` 만 |
  | `category_action` | `categoryPrior` 만 |
  | `screen` | screen 컨텍스트(`screenSingle`/`screenTriple`/`screenFive`/`screenNine`/`screenNamed`) 중 1개 |
  | `condition_only` | `none` 만(#344 라운드 2·3) |
  | `wishlist_view` | `none` 만(#386) |
  | `named_category` | `none` 만(#443 — `condition_only` 와 같은 이유: 무프라이어 첫 턴만 잰다) |
  | `cart_control` · `demonstrative` · `order_status` · `general` | `none` / `lastRecommendations` / `pendingCart` — 특수 컨텍스트 선언 불가 |

  다음 사람이 컨텍스트를 추가할 때 **이 매핑에 한 줄을 넣지 않으면 아무 발화도 그것을 못 쓴다**
  (안전한 기본값). `AnchorSet.model_validate` 가 매핑을 벗어난 컨텍스트 선언·중복 컨텍스트를
  거부한다.
- `categoryPrior`(#84) 는 `categoryPriorFilters`(직전 카테고리가 있는 스레드 —
  `음향가전 > 이어폰`)를 PRIOR_FILTERS 로 싣고 **LAST_RECOMMENDATIONS 는 싣지 않는다.** 직전 추천
  목록이 붙으면 그 상품명이 카테고리 판정에 섞여(#118 라운드 2 실측) 재려는 축이 오염되기 때문이며,
  이 축은 `PRIOR_FILTERS.category` 단독의 효과를 잰다. 카테고리 발화는 **기존 축을 선언할 수 없다**
  (스키마가 거부) — 새 셀이 `mainIntent` 같은 기존 축의 분모를 늘리면 기준선과 비교가 깨진다.
  발화에 직전 카테고리 어휘(`이어폰`)를 넣는 것도 스키마가 막는다(carry/replace 어느 쪽으로도
  읽혀 정답이 자명하지 않다).
- **되물음 상품의 목록 위치를 명시적으로 고정**한다(`reaskProductListPosition` + 이유 산문).
  #240 에서 이 위치가 1번이냐 2번이냐만으로 `일반형` 정답률이 8/8 ↔ 3/8 로 갈렸다.
  두 판본의 차이는 이 축 하나뿐이라 `--fixture a|b` 로 그 사실을 시연할 수 있다.
- **screen 지시어 해소(#300)** — `screens`(5종 화면 픽스처) · `screenLastRecommendations`
  (screen 컨텍스트 전용 LAST_RECOMMENDATIONS, 기본 `lastRecommendations` 와 이름이 겹치지 않게
  분리했다 — 겹치면 이름 매칭 셀의 정답 신호가 샌다). `pendingCart` 와 `screen` 은 같은
  컨텍스트에 함께 실릴 수 없다(스키마가 거부 — `graph.py` 의 `screen_context_active = pending_dict
  is None` 배선과 같다). 자세한 설계 근거는 아래 「screen 지시어 해소」 절.

`fixtures/manifest.json` 의 sha256 과 대조해 읽는다(불일치 → 종료 코드 2).
외부 경로(`--fixture <path>`)는 대조를 건너뛰되 해시를 산출물에 기록한다.

## 축과 정의

정의는 `metrics.py` 의 `AxisSpec` 에 데이터로 있고 **산출물에 그대로 실린다.**

| axisId | 분자 | 분모(N=8) |
|---|---|---|
| `cartControl` | intent 가 기대 intent 와 일치 | 6×3×8 = 144 |
| `demonstrative` | intent 가 `recommend` | 4×3×8 = 96 |
| `mainIntent` | 위 둘의 합 | 240 |
| `optionAnswer` | `cart_add` **그리고** optionId 일치 | 4×1×8 = 32 |
| `switchLegacy2` | `cart_add` ∧ productId 가 **되물음 상품이 아닌** 목록 내 상품 (**#240 정의**) | 2×1×8 = 16 |
| `cartAddProductIdLegacy2` | `cart_add` ∧ productId ∈ 목록 (**#234 정의** — 에코도 정답) | 같은 표본 16 |
| `switchAll7` | `switchLegacy2` 술어를 전환 7발화 전부에 | 7×1×8 = 56 |
| `orderStatus` / `general` | intent 일치 | 각 48 |
| `categoryAction3Way` | **확정값**(`resolve_category_action` 산출)이 기대 carry·clear·replace 와 일치 | 15×1×8 = 120 |
| `categoryCarry` | 같은 술어, 리파인 4발화 | 32 |
| `categoryClear` | 같은 술어, 리셋 4발화 | 32 |
| `categoryReplace` | 같은 술어, 교체 3발화 | 24 |
| `categoryMixedReplace` | 같은 술어, **혼합 4발화**(새 카테고리 + "아무거나") | 32 |
| `screenExactPick` | **해소기 통과 후 최종** productId == expected.productId (`cart_add` 도 함께 봄) | 4×1×8 = 32 |
| `screenReask` | 최종 productId 가 None(임의 확정하지 않고 되물음) | 1×1×8 = 8 |
| `screenNoHallucination` | 최종 productId != expected.forbiddenProductId | 1×1×8 = 8 |
| `screenResolution` | 위 셋의 합(각 셀은 자신의 규칙으로만 채점) | 6×1×8 = 48 |
| `conditionOnlyNoCategoryQuery` | 조건 전용 발화("평점 좋은 걸로 보여줘" 등)에서 `categoryQueries`(leg)가 하나도 없음(#344 라운드 2) | 5×1×8 = 40 |
| `namedCategoryHasLeg` | 상품군 명시 첫 턴("과일 추천해줘" 등)에서 `categoryQueries`(leg)가 1개 이상 있음 — `conditionOnlyNoCategoryQuery` 의 거울(#443, confirmatory-primary) | 6×1×8 = 48 |

**혼합 발화 축(`categoryMixedReplace`)을 `categoryReplace` 와 섞지 않는다**(라운드 3). 새 카테고리를
지목하면서 동시에 "아무거나"류 표현을 쓰는 발화는 초판 판정 순서에서 사용자가 말한 카테고리가
통째로 버려졌는데(실 LLM 실측 32건 중 **19건 clear**), 그 실패를 `categoryReplace` 에 합치면
그 축의 분모(24)가 바뀌어 방금 커밋한 v2 기준선과 비교가 끊긴다 — **실패의 모양을 갈라 센다.**
대신 `categoryAction3Way` 의 분모가 88 → **120** 으로 늘었으므로 그 축은 v2 표와 직접 비교하지
않는다(`notComparableWith` 에 적혀 있다).

카테고리 축(#84)은 **커밋된 기준선(`fast-2026-08-04*`)에 없다** — `notComparableWith` 에 그 사실이
실린다. 채점은
**그래프 가드가 실제로 쓰는 확정값**과 대조한다: 프로브는
`app.agents.buyer.recommendation.decompose.resolve_category_action` 을 그대로 부르고 규칙을
재구현하지 않는다(재현이 틀리면 그 위의 모든 측정과 인과가 함께 틀린다).

⚠️ 픽스처의 `expected.categoryAction` 은 **가드 확정값(carry|clear|replace)의 기대치**이지 LLM
응답 필드가 아니다. 같은 이름의 인라인 `categoryAction` 필드는 `decompose` 프롬프트에 넣었다가
**실측으로 기각돼 제거**됐지만(아래 「알려진 한계」), 판정 이름 자체는 그대로 유효하므로 픽스처·
스키마·축은 바뀌지 않았다.

`--no-classifier` 로 끈 런은 `results.json.categoryScopeEnabled` · `run_manifest.json` 의
`intentProbe.categoryScopeClassifier` · `report.md` 헤더(`scope=off`)에 그 사실이 남는다. 앱 설정
(`CATEGORY_SCOPE_CLASSIFIER_ENABLED`)은 **읽지 않는다** — 프로브는 환경이 아니라 인자로 조건을
고정해야 재현된다.

**프로브는 배포처럼 두 호출을 한다** — 직전 카테고리가 있는 컨텍스트(`categoryPrior`)에서는
`decompose` 와 **전용 분류기**(`category_scope.classify_category_scope`)를 함께 부르고, 그
`scopeFree` 를 확정값 계산에 넣는다. 분류기 호출은 **재시도 대상이 아니다**(실패 → None) — 배포도
그렇게 degrade 하므로 재시도하면 프로브가 배포보다 관대한 조건을 재게 된다. `--prompt`/
`--prompt-rev` 로 후보 프롬프트를 넣어도 **분류기 문면은 갈아끼우지 않는다**
(`client.PASSTHROUGH_SYSTEMS`) — 덮으면 분류기가 통째로 망가지는데 표에는 "갑자기 무동작"으로만
보인다. 새 보조 노드를 태울 때마다 그 목록을 함께 늘려야 한다.

**run manifest 는 프롬프트 해시를 둘 남긴다** — `hashes.systemPrompt`(decompose 문면)와
`hashes.categoryScopePrompt`(분류기 문면), 그리고 각각의 모듈 파일 해시(`hashes.prompts.decompose`
· `hashes.prompts.categoryScope`). 표를 결정하는 프롬프트가 둘이므로 하나만 남기면 분류기 문면만
바꾼 두 표가 manifest 상 구분되지 않는다. `report.md` 헤더에도 `scope=<sha12>` 로 한 칸 실린다.

**`categoryCarry` 는 prior 에코 leg 를 정답으로 센다.** `_SYSTEM` 의 categoryQueries 불릿이 리파인
턴에 직전 카테고리를 leg 로 복사하라고 지시하므로 확정값이 `replace` 로 나오는데, 그 leg 는 에코라
결과적으로 카테고리가 유지된다 — 보정이 없으면 축이 **정상 동작을 오답으로** 센다(실측 1/32).
에코는 앵커 `categoryPriorFilters`(카테고리 전체·각 조각·`semanticQuery`)와 **정규화 후 정확
일치**일 때만 인정한다 — 부분 문자열이면 `"이어폰 케이스"` 같은 새 상품도 에코로 세어 **카테고리가
바뀐 턴을 "유지됐다"로** 읽는다(lessons 2026-08-02 「부분 문자열 매칭은 포함 방향마다 의미가
다르다」). 판정 근거(`categoryLegsEchoPrior`)와 **leg 원문**(`categoryLegs`)·신호 유무·`scopeFree`·
확정값이 전부 `samples.csv` 에 칸으로 남으므로, 판정 규칙을 바꿔도 **런을 다시 돌리지 않고**
재집계할 수 있다.

**`switchLegacy2` 와 `cartAddProductIdLegacy2` 는 같은 표본을 다른 정의로 센다.**
#234 의 `productId 7/8` 은 "productId ∈ LAST_RECOMMENDATIONS" 였고 #240 의 같은 이름 지표는
"되물음 상품이 아닌 상품" 이었다 — **두 표의 숫자를 직접 비교하면 안 된다.** 축마다
`notComparableWith` 를 달아 산출물에도 그 경고가 실린다.

진단 카운터 9개(합불 아님): `reaskProductEchoCount`(되물음 상품을 그대로 담음 — 사용자가 고르지
않은 옵션으로 옛 상품이 담기는 **위험한 실패**), `productIdNullCount`(못 고르고 null — 되물음이
유지되는 **안전한 퇴화**), #84 의 둘 — `categoryScopeUnresolvedCount`(전용 분류기가
판정하지 못한 표본 = 분류기의 침묵률. 3분기 축을 신뢰할 수 있는지의 근거다),
`categoryClearOnRefineCount`(리파인 발화가 `clear` 로 확정됐다 — 이 변경이 만들 수 있는 **유일한
새 회귀 모양**이라 정확도와 따로 센다), #300 의 셋 — `screenPromptLayerHitCount`·
`screenResolverOverrideCount`·`screenOutOfListConfirmCount`(아래 「screen 지시어 해소」 절), 그리고
#443 의 둘 — `namedCategoryEmptyLegsCount`(상품군 명시 첫 턴에서 leg 이 0개인 표본 수 —
`namedCategoryHasLeg` 의 미충족 표본 수와 같다)·`namedCategoryCase3Count`(같은 셀에서 산출
`case` 가 3 으로 나온 표본 수 — 공백률과 case 오분류의 상관을 재는 계측기, 아래 「상품군 명시 첫
턴」 절).
카테고리 둘은 카테고리 셀만, screen 셋은 screen 셀만, 상품군 명시 둘은 named_category 셀만
본다(전환 카운터가 전환 셀만 보는 것과 같은 규약 — 다른 그룹은 그 가드에 닿지도 않는다).

## 조건 전용 발화 categoryQueries 비움(#344 라운드 2)

조건 전용 발화("평점 좋은 걸로 보여줘" 등 카테고리 어휘 없이 조건만 말하는 턴)에서 `decompose`
가 `categoryQueries` 를 비워 내는 계약은 지금 **프롬프트의 우연한 동작이고 코드가 강제하지
않는다**(`category_mapping._collect_expansion_leaves` docstring [#222 R2 F-2] 참조). 깨지면 조건
텍스트가 임베딩 앵커로 흘러 #222 확장이 무관 카테고리로 fan-out 한다. `conditionOnlyNoCategoryQuery`
축은 이 불변식을 decompose 계약 단계에서 고정한다 — 문구는 `evals/category_probe` 의 `none`
슬라이스와 동일하게 맞춰, 두 하네스가 같은 현상을 각자 단계(decompose 계약 vs 임베딩 매핑 결과)
에서 잰다.

**이 셀은 100% 통과를 만들려고 프롬프트를 고치는 것이 목적이 아니다** — 지금 현실(오케스트레이터
실측, 2026-08-06, category_probe none 슬라이스 40표본: 10건 누출, ~25%)을 회귀 없이 기록하는
것이 목적이다. 프롬프트 수정은 이 축의 범위 밖(별도 이슈)이다.

## 상품군 명시 첫 턴 leg 산출(#443) — 위 축의 반대 방향(#465)

상품군을 **명시한** 첫 턴("나 아기 키우는데 과일 추천해줘")에서 `decompose` 가 `categoryQueries`
leg 을 하나도 못박지 못하면, 파라미터 0개 payload 로 떨어져 `#217` 전개 → `#222` 확장 폴백이라는
불필요한 LLM 호출 1회 + fan-out 검색 N건이 붙는다 — 운영 실측(2026-08-07)에서 같은 발화가 회차마다
`categoryQueries: ["과일"]`(검색 1건)/`categoryQueries: []`(검색 4~8건·지연 14.52s) 두 갈래로 갈렸다
(이슈 본문). `namedCategoryHasLeg` 축이 이 불변식("leg 이 1개 이상 나와야 한다")을 decompose 계약
단계에서 고정한다.

**이 축은 `conditionOnlyNoCategoryQuery` 와 정확히 거울이다** — 같은 필드(`categoryQueries`)의
양쪽 끝을 잰다: 조건만 말한 턴은 leg 이 **0개**여야 정답이고, 상품군을 명시한 턴은 leg 이 **1개
이상**이어야 정답이다. `#443`(안 채움)과 `#465`(지어냄)은 반대 방향 결함이라 **한쪽만 보고
프롬프트를 고치면 다른 쪽이 나빠질 수 있다** — 채택 판정은 두 축을 같은 표에서 함께 읽는다.

앵커 6발화는 **요인 분리 설계**다(상황 설명 없음의 대조군 · 운영 실측 발화(상황 선행) · 상황 후행
· 구체 상품명(추상도) · 다른 상황·다른 상품군(일반화) · 가격·평가 수식어). 각 발화의 `note` 에
어느 요인을 잡는지 적혀 있다. `samples.csv` 에 산출 `case` 와 leg 원문(`categoryLegs`)이 남으므로
런을 다시 돌리지 않고 "공백률 × case 분포" 상관을 재집계할 수 있다.

⚠️ **`named-category-006`(수식어 셀)은 001~005 와 성격이 다르다.** 상품명(`텀블러`)·상황 어휘는
`_SYSTEM` 과 겹치지 않지만, 수식어 `가성비` 자체는 `_SYSTEM` 의 categoryQueries 불릿 수식어 제거
규칙(`"가격·평가 수식어(\"갓성비\", \"가성비\", \"저렴한\")와 상황 설명 ... 은 빼세요"`)에 문면으로
등장한다 — 이 셀이 만점이어도 그것은 **일반화의 증거가 아니라 명시된 규칙이 지켜지는가의
증거**다(`legs_probe` 의 `promptExample` 규약과 같은 이유).

## screen 지시어 해소(#118 이관, #300)

`app/agents/buyer/screen_reference.py::resolve_screen_reference` 가 화면 지시어("이거"·"3번째
거"·"3번째 줄 2번째"·이름 지목)를 해소한다 — 순번·좌표·"후보 1건이면 확정, 여러 건이면 되물음"은
입력만으로 답이 하나로 정해지는 **결정적 규칙**이라 LLM 에 맡길 이유가 없다(#118 라운드 2 가 실측
한 이유, `screen_reference.py` docstring 참조). 이 **6셀**은 #118(PR #292)이 별도 스크립트로
쟀었는데, #300 이 **판정 규칙을 프로브가 재구현하지 않고 배포 경로와 같은 함수를 같은 순서로
부른다**는 이 하네스의 확립된 규약(#84 가 `resolve_category_action` 으로 이미 그렇게 한다)에 따라
흡수했다 — 해소기의 규칙(맨 지시대명사·순번·좌표·목록 밖 id 차단)이 텍스트만으로 결정적으로
풀리는 것은 그중 **4셀**(001·002 는 맨 지시대명사, 004 는 좌표, 006 은 목록 밖 id 차단). 나머지
둘은 다르다 — **005(이름 매칭)는 해소기가 아예 개입하지 않는다**(양보 B, LLM 산출을 그대로
쓴다). **003(순번 "3번째 거")도 규칙 자체는 결정적이지만**, 모든 규칙이 공유하는 공통 게이트
(intent 가 이미 `cart_add` 여야 발동 — 다른 4셀도 마찬가지다)가 D-6 실측에서 그 셀 1회만
`decompose` 의 intent 라우팅이 미끄러져 해소기 호출 대상 자체가 아니었던 사례로 드러났다(아래
「screen 지시어 해소」절과 `baselines/fast-2026-08-05-300-screen/README.md` 참조).

**러너가 해소기를 부른다** — `decompose` 호출 뒤 `graph.py` 의 cart_add 분기와 **같은 조건·같은
인자**로 `resolve_screen_reference` 를 부른다: `screen` 이 있고 `screen.products` 가 비지
않고 intent 가 이미 `cart_add` 이고 `pending_cart is None` 일 때만. `Sample.productId` (원본
decompose 산출)는 F-4 규약대로 그대로 두고, `Sample.resolvedProductId`(해소기 통과 후 최종값)
를 새로 남긴다 — 판정 규칙이 바뀌어도 **런을 다시 돌리지 않고** 재집계할 수 있다. screen 축
채점은 최종값을 본다(사용자가 겪는 동작이 그것이고, #118 의 48/48 도 같은 정의다).

**pendingCart 와 screen 은 같은 컨텍스트에 공존할 수 없다**(스키마가 거부) — #118 이 확정한
규약("되물음 턴에는 화면 맥락을 프롬프트에 싣지 않는다")이고 `graph.py` 의
`screen_context_active = pending_dict is None` 이 그 배선이다. 성립하지 않는 컨텍스트를
픽스처가 표현할 수 있으면 배포에 없는 조건을 재게 된다.

**productIdRule 은 3종이다** — `screenExact`(확정, `expected.productId` 필수) ·
`screenReask`(비움·되물음, `productId`/`forbiddenProductId` 둘 다 비어야 함) ·
`screenNotHallucinated`(특정 id 확정 금지, `expected.forbiddenProductId` 필수 — 그 id 는
screen ∪ `screenLastRecommendations` 어디에도 없어야 "확정 금지" 술어가 무의미해지지 않는다).
채점 술어는 #118 원본(`_product`/`_no_product`/`_not_hallucinated`)과 **한 글자도 다르지
않다** — `screenExact` 만 intent 도 함께 본다(그 밖은 intent 무관).

진단 카운터 3종은 screen 셀만 본다: `screenPromptLayerHitCount`(해소기 전 원본 decompose
산출만으로 셀 규칙을 만족한 표본 수 — #118 이 잰 "코드 해소기 도입 전 9/48" 과 대조하는 값),
`screenResolverOverrideCount`(해소기가 발동해 productId 를 확정/되물음으로 바꾼 표본 수),
`screenOutOfListConfirmCount`(최종 productId 가 None 도 아니고 두 목록 안에도 없는 표본 수 =
**위험한 실패**, 0 이어야 한다).

**screen 컨텍스트는 범위 해제 분류기를 태우지 않는다** — `priorFilters` 에 `category` 가 없어
게이트가 열리지 않는다(D-6 실측 확인). 기준선은 `baselines/fast-2026-08-05-300-screen/`.

## 산출물과 종료 코드

`--out <dir>` (이미 있으면 덮지 않는다)

| 파일 | 내용 |
|---|---|
| `results.json` | 축·정의·셀 분포·진단·못 채운 셀·프롬프트·티어·픽스처·페이서·예산 |
| `report.md` | 헤더 1줄 + #240 축 순서 요약 + 축 표(정의 동반) + 셀 분포 + 못 채운 셀 + 함정 |
| `samples.csv` | 성공 표본 1행씩 |
| `cells.csv` | 셀 1행씩(표본·시도·실패·충족 여부) |
| `failures.csv` | 버린 시도 1행씩 |
| `run_manifest.json` | 커밋·dirty·uv.lock·앵커 해시·**실제로 보낸 프롬프트 해시**·축 정의 |

| 코드 | 뜻 |
|---|---|
| 0 | 모든 셀을 채웠다 |
| 2 | 사전 거부(인자·`--out` 존재·앵커 해시/스키마 불일치·프롬프트 읽기 실패·LLM 미설정) |
| 3 | 예산 초과로 중단(부분 산출물 기록) |
| 4 | 못 채운 셀이 있다(부분 산출물 기록) |

## run manifest

`evals/metrics/run_manifest.py::build_run_manifest` 를 확장한다.
`hashes.prompts.decompose` 는 **파일 전체** 해시라 무관한 편집에도 바뀌고,
`hashes.systemPrompt` 는 **실제로 provider 에 보낸 텍스트**의 해시다 — #260 이 요구하는 쪽은
후자이며 둘 다 남긴다. `hashes.anchorFixture`·`intentProbe.fixtureVersion` 으로 어떤 정답지로
잰 표인지도 특정된다.

## 알려진 한계

- **#240 의 앵커는 재구성본이다.** 원 스크립트가 세션 scratchpad 와 함께 사라져 발화 구성은
  이슈 본문과 PR #239 에서 복원했고 상품명·id 에는 추정이 섞였다. **실측 결과**(위 대조 런):
  8축 중 5축은 #240 표와 정확히 일치했고(`mainIntent`·`cartControl`·`demonstrative`·
  `optionAnswer`·`orderStatus`), 전환 productId 2축과 `general` 이 어긋났다. 어긋난 축은 전부
  상품명 어휘에 의존하는 쪽이다 — 프로브 결함이 아니라 **정답지 복원 격차**이며, 정답지가 조금
  달라도 특정 축이 통째로 갈린다는 이 이슈의 논지를 그대로 보여준다.
- 이 프로브는 `llm_max_retries=0` 으로 provider 클라이언트를 만든다. SDK 내부 재시도는 페이서를
  우회하고 N 채우기 로직에도 보이지 않기 때문이다. `evals/model_eval` 은 배포 후보의 재시도
  거동까지 평가하려고 반대로 런타임 값을 유지한다 — 목적이 다른 의도적 분기다.
- **커밋된 기준선은 픽스처 v1 으로 잰 표다.** `baselines/fast-2026-08-04/` 는
  `intent-probe-anchors-b-v1` 런이고, #84 가 픽스처를 v2 로 올렸다. v2 는 **기존 셀을 한 글자도
  바꾸지 않고 새 셀만 추가**했으므로 기존 축의 분자·분모 구성은 그대로지만(카테고리 발화는 기존 축을
  선언할 수 없다 — 스키마가 강제한다), 그래도 **다른 픽스처 버전의 표라 직접 비교하지 말 것.**
  프롬프트 회귀는 **같은 v2 픽스처로 잰 전/후 런끼리** 대조한다 — #84 의 판정도 그 방식이다
  (`categoryAction` 불릿을 넣기 전 `_SYSTEM`(`--prompt-rev`)과 지금 `_SYSTEM` 을 **둘 다 v2 로**
  재서 기존 축의 회귀 0 을 본다). 기준선을 v2 로 다시 뜨는 것은 프롬프트를 바꾸지 않는 별도 런의
  일이다.
- **인라인 `categoryAction` 필드는 이 프로브로 기각됐다(#84).** `decompose` 프롬프트에 판정 필드를
  더하는 안을 64셀 전 축 런 **전/후 각 2회**(fast·N=8·앵커 b)로 짝지어 쟀다: 불릿이 **없는** 런에서도
  `categoryClear` 32/32 라 **이득이 0**이었고, 불릿을 넣은 런은 `switchAll7` 37·38 → 32·32 ·
  전환 발화가 `recommend` 로 새는 표본 4~5 → 16~17 로 **보호 축이 깎였다**. 그래서 프롬프트는
  dev 판과 **바이트 동일**하게 되돌렸고 3분기 해소는 전용 분류기만 담당한다. 같은 시도를 반복하기
  전에 이 문단과 `decompose.resolve_category_action` docstring 을 읽을 것 — 채택 조건은 "내 축이
  좋아졌다"가 아니라 **"다른 축이 안 깎였다"** 다.
- `--seed` 는 셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.
- **#300 이 픽스처를 v4 로 올렸다**(`schemaVersion` 1.1.0 → 1.2.0, `fixtureVersion` `-v3` →
  `-v4`). v4 는 v3 의 **기존 셀을 한 글자도 바꾸지 않고 screen 6셀만 추가**했으므로 기존 축의
  분자·분모 구성은 그대로다(screen 발화는 기존 축을 선언할 수 없다 — 스키마가 강제한다). 그래도
  **다른 픽스처 버전의 표라 v3 이하 기준선과 직접 비교하지 말 것** — v3 기준선(`fast-2026-08-05-84/`
  등)에는 screen 4축 자체가 없다(`notComparableWith` 에 그 사실이 실린다).
- **`schemaVersion` 은 `Literal` 이라 버전 게이트다.** `AnchorSet.schema_version:
  Literal["1.2.0"]` 이므로 v3 이하 스키마로 쓰인 외부 앵커(`--fixture <경로>`)는 **자동으로
  거부된다**(종료 코드 2, `pydantic.ValidationError`) — 픽스처 필드를 완화해서 되살릴 수 있는
  문제가 아니다(`screens`/`screenLastRecommendations` 를 선택 필드로 바꿔도 `schemaVersion`
  게이트가 먼저 막는다). 의도된 동작이며 이번이 처음도 아니다 — #260 이 `1.0.0` 으로 시작했고
  `#84` 가 카테고리 컨텍스트를 더하며 `1.1.0` 으로, 이번 `#300` 이 screen 을 더하며 `1.2.0` 으로
  올렸다. 오래된 외부 앵커로 막혔다면 그 앵커를 현재 스키마로 다시 만들어야 한다.
