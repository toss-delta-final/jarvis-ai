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

기본 규모: 68셀 × N=8 = 544콜(decompose) **+ 120콜**(카테고리 15셀 × N — 범위 해제 분류기)
= **664콜**, 45rpm 페이서라 런당 약 15~17분.
`fast`(gpt-5-nano) 기준 런당 대략 USD 0.10 — 2026-08-04 실측($0.086 / 1.27M tokens, 424콜)을
콜 수 비례로 환산한 추정이다(#84 이 카테고리 11셀을 더해 424 → 512). 분류기 88콜은 프롬프트가
짧고(`max_tokens=32`) 콜당 ≈0.35k 라 비용·TPM 영향이 작지만 **페이서는 지나므로**(rpm 예산에
포함) 위 소요 추정에는 넣었다.

## 기준선

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

- 발화 40개 — 장바구니 대조군 6 · 지시대명사 4 · 옵션 답변 4 · 전환 7 · order_status 2 · general 2
  · **카테고리 승계 15**(리파인 4 · 리셋 4 · 교체 3 · **혼합 4**, #84)
- 컨텍스트 4종 — `none` / `lastRecommendations` / `pendingCart` / **`categoryPrior`**
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

진단 카운터 4개(합불 아님): `reaskProductEchoCount`(되물음 상품을 그대로 담음 — 사용자가 고르지
않은 옵션으로 옛 상품이 담기는 **위험한 실패**), `productIdNullCount`(못 고르고 null — 되물음이
유지되는 **안전한 퇴화**), 그리고 #84 의 둘 — `categoryScopeUnresolvedCount`(전용 분류기가
판정하지 못한 표본 = 분류기의 침묵률. 3분기 축을 신뢰할 수 있는지의 근거다),
`categoryClearOnRefineCount`(리파인 발화가 `clear` 로 확정됐다 — 이 변경이 만들 수 있는 **유일한
새 회귀 모양**이라 정확도와 따로 센다). 뒤의 둘은 카테고리 셀만 본다(전환 카운터가 전환 셀만 보는
것과 같은 규약 — 다른 그룹은 승계 가드에 닿지도 않는다).

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
