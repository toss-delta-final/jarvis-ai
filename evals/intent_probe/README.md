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

# 현재 _SYSTEM 을 바이트 그대로 받아 두기(--prompt 로 왕복하면 해시가 같다)
uv run python -m evals.intent_probe --dump-prompt system.txt
```

기본 규모: 53셀 × N=8 = **424콜**, 50rpm 페이서라 런당 약 9~10분.
`fast`(gpt-5-nano) 기준 런당 대략 USD 0.1 수준.

## CI 에서 돌리지 않는다

실 LLM 호출이라 비용·비결정론이 붙는다. **수동 실행 도구**이며, 프롬프트를 바꾸는 PR 이
산출물(`report.md`)을 근거로 첨부한다(#240·#234 가 그렇게 했다).
`tests/unit/test_intent_probe_*.py` 는 전부 가짜 LLM이라 CI 에서 API 콜이 0이다.

## 재현 함정 4가지 (#240 에서 실제로 밟은 것들)

1. **전역 페이서 필수** — org 500 RPM / 200k TPM, `decompose` 1콜 ≈ 3.1k tokens → 50 rpm.
   없이 돌리면 429 로 표본이 비고, 그 빈 칸을 오답으로 세면 분포가 무의미해진다(초기 2런 폐기).
2. **실패는 표본이 아니다** — 성공할 때까지 재시도해 N 을 채운다. 못 채운 셀은
   `results.json.unfilledCells` 와 `report.md` 에 드러나며 **종료 코드 4** 가 된다.
3. **픽스처 문자열이 정답 신호와 겹치면 안 된다** — 되물음 상품명에 옵션 이름("드럼")이 들어가
   `일반형` 답변이 8/8 오답이던 사고가 있었다. 프롬프트 결함이 아니라 **픽스처 결함**이었고,
   지금은 `schema.py` 검증자가 그런 앵커를 아예 커밋하지 못하게 막는다.
4. **단일 실행으로 판정 금지** — 같은 프롬프트 해시의 독립 실행에서 축당 ±2, 특정 셀은 2/8~6/8
   까지 흔들린다. 채택 판정은 **독립 2~3회** 분포로 한다.

## 앵커(정답지)

`fixtures/anchors_b.json`(기본) / `fixtures/anchors_a.json`. 스크립트는 이 파일만 읽는다.

- 발화 25개 — 장바구니 대조군 6 · 지시대명사 4 · 옵션 답변 4 · 전환 7 · order_status 2 · general 2
- 컨텍스트 3종 — `none` / `lastRecommendations` / `pendingCart`
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

**`switchLegacy2` 와 `cartAddProductIdLegacy2` 는 같은 표본을 다른 정의로 센다.**
#234 의 `productId 7/8` 은 "productId ∈ LAST_RECOMMENDATIONS" 였고 #240 의 같은 이름 지표는
"되물음 상품이 아닌 상품" 이었다 — **두 표의 숫자를 직접 비교하면 안 된다.** 축마다
`notComparableWith` 를 달아 산출물에도 그 경고가 실린다.

진단 카운터 2개(합불 아님): `reaskProductEchoCount`(되물음 상품을 그대로 담음 — 사용자가 고르지
않은 옵션으로 옛 상품이 담기는 **위험한 실패**), `productIdNullCount`(못 고르고 null — 되물음이
유지되는 **안전한 퇴화**).

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
  이슈 본문과 PR #239 에서 복원했고 상품명·id 에는 추정이 섞였다. `--prompt-rev 3f1dec7` 로 옛
  프롬프트를 재현해도 #240 표와 축당 ±2 안에 들어온다는 보장은 없으며, 그 차이는 프로브 결함이
  아니라 **정답지 복원 격차**다.
- 이 프로브는 `llm_max_retries=0` 으로 provider 클라이언트를 만든다. SDK 내부 재시도는 페이서를
  우회하고 N 채우기 로직에도 보이지 않기 때문이다. `evals/model_eval` 은 배포 후보의 재시도
  거동까지 평가하려고 반대로 런타임 값을 유지한다 — 목적이 다른 의도적 분기다.
- `--seed` 는 셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.
