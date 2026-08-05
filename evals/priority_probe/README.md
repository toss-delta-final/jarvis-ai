# priority 신호 실측 프로브 (#281)

니즈별 `priority`(1 필수 / 2 권장 / 3 선택)를 **fast 티어에서 안정적으로 추출할 수 있는가**,
그리고 **인라인**(`decompose._SYSTEM` 에 필드 추가) 과 **전용 분류기**
(`app/agents/buyer/recommendation/need_priority.py`) 중 어느 쪽이 그 일을 하는가를 실 LLM
반복 분포로 잰다.

## 이건 골든셋이 아니다

`evals/goldenset` 은 추천 품질을 결정론 1회로 재고, 이 프로브는 **priority 판정 안정성**을
발화 × N=8 반복의 확률 분포로 잰다. 두 산출물의 숫자를 섞지 말 것.

## ⚠️ import 결합 고지 (#300)

`evals.intent_probe.pacer`(`GlobalPacer`·`PacerLimits`)와 `evals.intent_probe.client`
(`PacedLLM`·`build_live_delegate`·`SystemPromptOverrideLLM`·`resolve_system_prompt` 등)를
**그대로 import 해서 쓴다** — 113줄짜리 슬라이딩 윈도 페이서를 다시 쓰지 않는다. `evals/priority_probe/client.py`
는 그 위에 `RawCapture` 하나만 얹는다. **#300 이 `evals/intent_probe` 디렉터리를 옮기면 이
import 한 줄이 깨진다** — 그 이슈가 이 결합을 알고 있다.

## 별도 프로브로 신설한 이유 — #300 「프로브 중복 제작」 교훈과의 정합

`docs/lessons.md` 의 「프로브 중복 제작 3회차 — 새 측정 도구를 만들기 전에 기존 하네스에 축을
더할 수 있는지 먼저 본다」(#118 이 만든 별도 프로브를 #300 이 `evals/intent_probe/` 로
흡수·삭제한 사건의 교훈)는 이 프로브에도 정직하게 답해야 하는 질문이다 — 그 기준으로 보면
`evals/priority_probe/` 는 4회차로 읽힌다. 변명이 아니라 기록으로 남긴다.

1. **왜 형제 디렉터리로 신설했나(착수 시점의 사실)** — 착수 시점 `origin/dev` 는 `d4cc7ad`
   였고 **#300 은 아직 머지되지 않았다.** #300 레인이 `evals/intent_probe/` 를 동시에 재구성
   중이라 그 디렉터리의 기존 파일을 건드리지 말라는 것이 이 작업의 명시적 제약이었다 — 같은
   파일을 두 레인이 동시에 고치는 것을 피한 결과다.
2. **왜 지금도 흡수하지 않았나** — 두 프로브는 측정 대상이 다르다. `intent_probe` 는
   **발화 → intent/카테고리 확정값**을 재고(셀 = 발화 × 컨텍스트), 이쪽은 **발화 + 니즈 목록
   → 니즈별 priority 배열**을 잰다(셀 = 발화 + `needs` + `expectedPriorities`, 채점은 니즈
   **쌍의 대소 관계**). 픽스처 스키마·팔(arm) 개념·축 정의가 전부 다르다. 지금 흡수하면 #281
   범위를 크게 넘고, 방금 커밋한 채택 근거 4런의 재현성도 흔든다.
3. **그래서 무엇을 하나** — **후속 과제로 제안한다.** #300 이 세운 규율에 동의하며, 두
   하네스가 이미 공유하는 것(페이서·프롬프트 교체 래퍼·실행/산출물 규약)을 공통 모듈로
   빼거나 이쪽을 `intent_probe` 의 한 모드로 흡수하는 것을 별도 이슈로 다루는 것이 맞다.

## 두 팔

- `--arm classifier` — 배포와 **동일하게** `need_priority.classify_need_priorities` 를 그대로
  부른다(규칙을 재구현하지 않는다). 입력은 픽스처 `needs` 그대로라 산출 길이가 항상 needs 와
  같거나(엄격 검증 통과) `None`(전부)이다 — **leg 정합 문제 자체가 없다.**
- `--arm inline` — `decompose.decompose()` 를 `SystemPromptOverrideLLM` 으로 후보 `_SYSTEM`
  (`candidates/inline_priority.txt`)을 씌워 부르고, **원시 JSON 응답을 직접 파싱**해
  `categoryQueries[i].priority` 를 읽는다(`decompose()` 의 파서는 이 필드를 읽지 않는다 —
  `decompose.py` 는 무변경). `decompose()` 는 픽스처 `needs` 를 입력으로 받지 않고 **자기 leg
  를 스스로 만들기 때문에**, 채점은 정규화 후 정확 일치(`decompose.normalize_category_token`)로
  need ↔ leg 를 짝짓는다(아래 「leg 매칭 규칙」).

## 후보 인라인 프롬프트

`candidates/inline_priority.txt` — 현재 `decompose._SYSTEM` 을 **바이트 그대로 뜬 뒤**
`categoryQueries` 스키마에 `"priority": 1 | 2 | 3` 을 더하고, SPEC 정본 판정 기준("이게 빠지면
그 상황/요리가 성립하는가" + 1/2/3 정의 + 등뼈/들깨가루/청양고추 예시)을 최소 침습 불릿 하나로
추가했다. **이 파일은 측정 대상 후보이지 출고 프롬프트가 아니다** — `decompose._SYSTEM` 은
이 프로브가 끝난 뒤에도 무변경이다.

## leg 매칭 규칙 (TASK-3-CORRECTION-2)

`runner._match_inline_legs_by_name` — needs 목록의 각 니즈를, 정규화(`normalize_category_token`
— 공백 접기 + 소문자) 후 `category` 또는 `query` 와 **정확히 일치**하는 leg 에 짝짓는다.
**부분 문자열 매칭은 쓰지 않는다**(lessons 2026-08-02 「부분 문자열 매칭은 포함 방향마다 의미가
다르다」). leg 하나는 니즈 하나에만 쓴다. leg 개수가 needs 개수와 달라도(구조적 비용,
`lengthMismatchCount`) 이름 매칭은 **별도로** 계속 시도한다 — 개수가 다르다고 이름이 맞는
leg 까지 버리면 인라인 팔을 부당하게 0점 처리하게 된다(아래 「초판 결함」).

보조로 `runner._match_inline_legs_by_index` 가 있다 — leg 개수가 **우연히 needs 개수와 같을
때만** 위치로 짝짓는다("이름은 달라도 순서 신호는 맞았는가"). 개수가 다르면 그 표본은
`priorityOrderPairsByIndex` 의 분모에서 아예 빠진다(비교 불가를 0 으로 세면 거짓이 된다).

모델이 **실제로 낸 leg 원문**(`category`·`query`·원시 `priority`)은 `samples.csv` 의 `rawLegs`
칸에 그대로 남는다 — 매칭 규칙을 나중에 바꿔도 **런을 다시 돌리지 않고 재집계**할 수 있다.

## 축 (`metrics.py` 에 데이터로 있고 산출물에 그대로 실린다)

| axisId | 분자 | 분모 |
|---|---|---|
| `priorityOrderPairs` | 기대 priority 가 **다른** 니즈 쌍에서 산출이 같은 대소 관계(이름 매칭) | 그런 쌍의 수 × N |
| `essentialProtected` | 기대 1 인 니즈가 산출에서 **최소값 집합**에 든다 | 기대 1 니즈 수 × N |
| `priorityOrderPairsByIndex` | 위와 같되 **위치 매칭**(leg 개수가 같을 때만, 이름 무시) — 보조 | leg 개수가 같은 표본에서 나온 그런 쌍의 수 |
| `prioritySignalPresent` | 그 니즈에 유효한 1/2/3 값이 나왔다 | 니즈 수 × 셀 × N |
| `priorityExact` | 값이 기대와 정확히 일치(보조) | 니즈 수 × 셀 × N |

**`priorityOrderPairs` 가 이 이슈의 본질 축이다** — REQ-REC-076 이 요구하는 것은 절대값이
아니라 **제외 순서**다. 절대값이 한 칸씩 밀려도 순서가 맞으면 knapsack 은 옳게 동작한다.
**`essentialProtected`(REQ-REC-076 "1 필수는 최후")는 이 이슈가 실제로 지켜야 하는 것이라
그다음에 놓는다.**

진단 카운터(합불 아님) — `unparsedCount`(JSON 파싱 실패, `lengthMismatchCount` 와 상호
배타적) · `lengthMismatchCount`(파싱은 됐지만 leg/배열 길이가 니즈 수와 다름, 구조적 비용) ·
`nameUnmatchedCount`(정규화 후에도 이름이 맞는 leg 를 못 찾은 니즈 수, 인라인 전용, `lengthMismatchCount`
와 겹칠 수 있다) · `invalidValueCount`(이름은 맞았는데 값이 범위 밖, 시도 합산) ·
`emptySignalCount`(그 표본의 모든 니즈가 None — **결과** 지표라 위 원인들과 겹칠 수 있다) ·
`transportRetries`(전송 실패로 재시도된 횟수 — 크면 표를 신뢰하지 말 것).

## 재현 함정 4가지 (#240 이 실제로 밟은 것들)

1. **전역 페이서 필수** — 없으면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
   토큰 추정은 `max_tokens` 예약분까지 센다.
2. **실패는 표본이 아니다** — 성공할 때까지 재시도해 N 을 채운다. 못 채운 셀은 종료 코드 4.
3. **픽스처 문자열이 정답 신호와 겹치면 안 된다** — 발화에 "필수"·"꼭 필요한"·"선택"·"권장"
   금지(스키마가 막는다).
4. **단일 실행으로 판정 금지** — 독립 2회 이상을 돌리고 두 표를 함께 본다.
5. **채운 컨텍스트** — 인라인 팔은 `CATEGORY_FANOUT_MAX`·`PRIOR_FILTERS`·`LAST_RECOMMENDATIONS`·
   `PROFILE_SUMMARY` 를 배포와 같은 모양으로 채운다(`fixtures/priority_fixture.json` 의
   `channel`). 빈 맥락 프로브는 실제 세션의 이웃 규칙 영향을 제거해 거짓 결론을 준다.

## 실행

```bash
uv run python -m evals.priority_probe --arm classifier --out artifacts/prio/classifier-1
uv run python -m evals.priority_probe --arm inline     --out artifacts/prio/inline-1
uv run python -m evals.priority_probe --arm classifier --out /tmp/probe --dry-run   # API 없이 배관만
```

규모: 12셀 × N=8 = 96콜/런, 45rpm 페이서라 런당 수 분. `fast`(gpt-5-nano) 기준 콜당 프롬프트가
짧아(인라인은 decompose 프롬프트 그대로, 분류기는 `max_tokens=64`) 비용은 작다.

## CI 에서 돌리지 않는다

실 LLM 호출이라 비용·비결정론이 붙는다. `tests/unit/test_priority_probe_*.py` 는 전부 가짜
LLM이라 CI 에서 API 콜이 0이다.

---

## 초판 결함과 정정 — 숨기지 않는다 (#240 규약과 동일)

이 프로브는 실측 도중 **두 차례** 오케스트레이터 리뷰로 정정됐다. 둘 다 "숫자가 나왔다"는
사실만으로 판정하지 않고 **하네스 자체를 의심**한 결과다.

### 정정 1 — 분류기 팔의 전송 실패가 표본으로 오염됨

초판은 `classify_need_priorities` 가 삼킨 `None` 을 전부 "표본"으로 셌다. 그 함수는 **전송
실패**(429·타임아웃)와 **모델 출력 문제**를 구분 없이 `None` 하나로 반환하므로, 페이서가
조금이라도 어긋나면 429 가 전부 "분류기 실패"로 집계돼 #240 이 폐기한 실패 양식을 재현한다.
`client.RawCapture` 를 래퍼 사슬 맨 안쪽에 두고 `complete()` 자체의 성공/실패를 관측해, 전송
실패는 재시도(표본 아님)로, 모델 출력 문제는 표본으로 가르도록 고쳤다(`runner.run_cell_classifier`).
`BudgetExceeded` 는 재시도하면 예산 가드가 무력화되므로 그대로 다시 던진다.
`tests/unit/test_priority_probe_runner.py` 가 이 구분을 가짜 LLM으로 고정한다.

### 정정 2 — 인라인 팔의 개수 불일치가 이름 매칭까지 통째로 버림

첫 인라인 런(`inline-1`/`inline-2`, **폐기**)이 **전 축 0** 이고 진단 카운터 셋
(`lengthMismatch`·`emptySignal`·당시의 `legMismatch`)이 **전부 정확히 96**(=모든 표본)이었다.
같은 값 셋이라는 것은 원인이 하나이고 세 번 세어졌다는 신호였고, `samples.csv` 에는 모델이
**실제로 무엇을 냈는지**가 기록돼 있지 않아 "모델이 정말 priority 를 안 냈다"(역량 한계) 와
"leg 개수가 달라서 채점기가 전부 버렸다"(하네스 결함) 를 구분할 수 없었다.

원인은 후자였다: 초판 `_priorities_from_inline_raw` 는 `len(categoryQueries) != len(needs)`
면 **이름이 일부 맞아도 전부 `None`** 으로 떨어뜨렸다. `decompose()` 는 픽스처 `needs` 를
입력으로 받지 않고 자기 leg 이름을 스스로 만드는데, 개수까지 우연히 같아야 채점되는 조건은
인라인 팔을 부당하게 낮게 평가한다. 위 「leg 매칭 규칙」으로 고치고, `rawLegs` 칸으로 원시
leg 를 `samples.csv` 에 남겼다(재집계 가능성). `tests/unit/test_priority_probe_inline_matching.py`
12건이 이 매칭 로직(정규화 정확 일치·부분 문자열 배제·개수 불일치와 무관한 이름 매칭·값
비용과 구조 비용의 분리)을 고정한다.

**고친 뒤에도 인라인 팔은 `priorityOrderPairs` 0/288 이었다** — 아래 「채택 근거」참조. 이번엔
`rawLegs` 로 원인이 명확하다: leg **개수**가 아니라, 모델이 애초에 needs 와 이름이 겹치는
leg 를 거의 내지 않는다(구체 품목 대신 포괄어 하나로 뭉뚱그린다). 채널(PRIOR_FILTERS) 유무와
무관하게 재현되는 현상임을 별도 임시 점검으로 확인했다(아래).

**`inline-1`/`inline-2` 런은 커밋하지 않는다** — 정정 2로 무효화된 산출물이고, 그 사실 자체를
이 문단이 기록으로 남긴다.

---

## 기준선 (채택 근거 4런)

| 런 | 팔 | `priorityOrderPairs`(본질) | `essentialProtected` | `priorityOrderPairsByIndex`(보조) | `prioritySignalPresent` | `priorityExact` | 진단 |
|---|---|---|---|---|---|---|---|
| [`2026-08-05-classifier-1`](baselines/2026-08-05-classifier-1/) | classifier | **189/288 (65.6%)** | **103/104 (99.0%)** | (해당 없음) | 288/288 (100%) | 180/288 (62.5%) | 전부 0 |
| [`2026-08-05-classifier-2`](baselines/2026-08-05-classifier-2/) | classifier | **194/288 (67.4%)** | **104/104 (100%)** | (해당 없음) | 288/288 (100%) | 193/288 (67.0%) | 전부 0 |
| [`2026-08-05-inline-1`](baselines/2026-08-05-inline-1/) | inline | **0/288 (0.0%)** | **0/104 (0.0%)** | 3/6 | 0/288 (0.0%) | 0/288 (0.0%) | lengthMismatch 94 · nameUnmatched 288 · emptySignal 96 |
| [`2026-08-05-inline-2`](baselines/2026-08-05-inline-2/) | inline | **0/288 (0.0%)** | **1/104 (1.0%)** | 5/9 | 1/288 (0.3%) | 1/288 (0.3%) | lengthMismatch 93 · nameUnmatched 287 · emptySignal 95 |

프롬프트 해시: 분류기 `5a80ffbdb2f8`(`need_priority._SYSTEM`, 두 팔 공통 — 인라인 런에서도
분류기는 호출되지 않지만 해시는 항상 기록된다), 인라인 후보 `1295b6e192ac`
(`candidates/inline_priority.txt`). 픽스처 `priority-probe-v1`(sha256 은 각 런의
`run_manifest.json` 참조). 전 런 못 채운 셀 0, `transportRetries` 0(페이서가 지나갔다는 뜻).

### 해석

- **`essentialProtected`(REQ-REC-076 "1 필수는 최후") 가 분류기에서 거의 만점이다**(103/104 ·
  104/104) — 이 이슈가 실제로 지켜야 하는 불변식이 fast 티어에서 안정적으로 보호된다.
- **`priorityOrderPairs` 는 65.6%·67.4% 로 완벽과 거리가 있다** — 남은 1/3 은 이 프로브가
  숨기지 않는 한계다(아래 「알려진 한계」).
- **인라인 팔은 두 런 모두 사실상 0** — `rawLegs` 원시 기록으로 원인이 leg 개수 불일치가
  아니라 **모델이 픽스처 needs 와 이름이 겹치는 leg 자체를 거의 내지 않는다**는 것으로
  확인됐다. 채널(PRIOR_FILTERS) 을 제거한 임시 점검(3셀, 커밋하지 않음)에서도 같은 현상이
  재현됐다 — `camping`("캠핑 준비물 좀 챙겨야")은 `["캠핑 준비물"]` 하나로, `kitten`
  ("고양이 입양했는데...")은 `["고양이 입양 초보용 물품"]` 하나로 뭉뚱그렸고 `hiking` 은
  leg 자체가 0개였다. 채널이 있을 때는 여기에 더해 PRIOR_FILTERS.semanticQuery
  ("가성비 좋은 생활용품")로 수렴하는 경향까지 겹쳤다(`rawLegs` 예: `"가성비 생활용품"`,
  `"캠핑 생활용품 추천"`). 즉 인라인 후보는 **채널 유무와 무관하게** 상황형(case 3) 발화를
  픽스처가 기대하는 품목 단위로 쪼개는 데 실패했고, 채널이 있으면 그 실패가 더 심해진다.

## 판정 (§8)

**전용 분류기가 이긴다.** `decompose._SYSTEM` 은 **무변경**이고, 배포 구성은 TASK 2 가 만든
`need_priority.py` 전용 분류기 그대로 둔다.

근거(구조적 논거를 실측 숫자보다 먼저 놓는다 — 이것은 실측과 **독립적으로** 성립한다):
- **배포 경로에서 knapsack 이 실제로 쓰는 leg 는 애초에 `decompose` 의 `categoryQueries` 가
  아니다.** `app/agents/buyer/graph.py::_prepare_recommendation` 에서 최종
  `decision.category_legs` 는 (1) `map_categories(decision.category_queries)` → `mapping.legs`
  (canonical 보정 · 거리컷 드롭 · `dedup_truncate`)이고, 조건부로 (2) `needs_expansion`
  (#198/#217)이 **별도 LLM 호출**로 만든 leg 와의 합집합이다. 즉 최종 leg 중에는 `decompose`
  가 본 적조차 없는 leg 이 섞이고, `decompose` 가 낸 leg 중 일부는 사라진다. 그래서 인라인
  방식은 설령 fast 티어가 priority 를 완벽히 태깅하더라도 **그 태그를 knapsack 이 쓰는 leg
  까지 배달할 방법이 없다** — 배달하려면 `category_mapping.py` 와 `buyer/graph.py` 까지
  priority 를 관통시켜야 하고, `needs_expansion` 이 만든 leg 은 여전히 신호를 못 받는다.
  **즉 인라인의 실패는 "픽스처가 인라인 팔에 불공정해서"가 아니라 배포 구조 자체다** — 이번
  실측은 그 구조적 사실을 **확인**해 준 것이지, 그것을 대신 증명한 것이 아니다.
- 인라인은 `priorityOrderPairs`·`essentialProtected`·`prioritySignalPresent` 전부 사실상 0 —
  값 정확도가 아니라 **애초에 needs 단위로 leg 를 내지 못해** 채점 자체가 성립하지 않는다.
  이것은 판정 규칙의 관대함 문제가 아니다(보조 위치 매칭 `priorityOrderPairsByIndex` 도
  3/6·5/9 로 표본 자체가 6~9건뿐이다 — leg 개수가 needs 개수와 우연히 같은 경우가 96건 중
  6~9건에 불과했다는 뜻이기도 하다).
- 분류기는 `essentialProtected` 99~100%, `priorityOrderPairs` 65.6~67.4% 로 **최소한 채점이
  성립하고, 이 이슈의 핵심 불변식(REQ-REC-076 "1 은 최후")을 안정적으로 지킨다.**
- 구조적으로도 분류기가 유리하다: `needs` 를 **입력으로 직접 받으므로** leg 정합 문제 자체가
  없다 — 인라인이 이번 실측에서 겪은 실패 양식(needs 와 다른 어휘로 뭉뚱그림)이 애초에
  발생할 수 없는 설계다.

## §9-A. 정본 이탈 고지 — 반드시 적는다

전용 분류기 채택은 **SPEC-RECOMMEND-001 의 두 조항에서 이탈한다**:

1. **결정 14-H / REQ-REC-004** — priority 판정을 "`decompose` 가 프롬프트 기준으로 태깅하며
   **LLM 추가 호출 없음**"으로 규정한다.
2. **AC-REC-37** — "별도 **분류** LLM 호출이 발생하지 않는다"를 못 박는다(#198 v0.10.0 은
   **생성** 작업인 `shopping_list` 전개에 한해서만 완화했다 — 분류는 여전히 금지).

**이탈 근거**: 위 4런 실측. 인라인(정본이 규정한 방식)은 `priorityOrderPairs`·`essentialProtected`
가 사실상 0 이라 REQ-REC-076 을 지킬 수 없다. 전용 분류기(호출 1회 추가)만이 채점이 성립하고
핵심 불변식을 지킨다.

**선례**: **#84**(PR #307, 머지됨)가 `category_scope` 전용 **분류기**를 같은 이유(인라인이 fast
티어에서 무동작)로 도입하고 SPEC 을 개정하지 않은 채 출고했다. 이번 건은 그 선례를 따른다.

**정본 반영은 후속 과제다**: `docs/specs/SPEC-RECOMMEND-001.md` 는 Notion 정본의 동기화
사본이고(그 파일 v0.10.0 항목 끝의 "⚠️ 정본 반영 필요" 참조) 이 레인은 사본·정본 편집이
금지돼 있다. 이 문서를 고치지 않았다 — 고지만 한다.

## 알려진 한계

- **`priorityOrderPairs` 는 65.6~67.4% 로 완벽이 아니다.** 남은 1/3 은 fast 티어의 판정
  변동성이다 — 정확한 판정 기준의 경계 사례(예: "권장(2) vs 선택(3)"이 사람이 봐도 애매한
  니즈)에서 흔들릴 가능성이 크지만, 이 프로브는 그 경계 사례를 축별로 분리하지 않았다.
  후속 작업이 필요하면 픽스처를 "명백한 케이스"와 "경계 케이스"로 나눠 다시 잴 것.
- **인라인 후보 문면의 한계인지, decompose 의 case-3 분해 자체의 일반적 약점인지 완전히
  분리하지 않았다.** 채널 없이도 같은 현상(품목 뭉뚱그림)이 재현됐으므로 priority 불릿
  추가가 유일한 원인은 아닌 것으로 보이지만, priority 불릿이 없는 원본 `_SYSTEM` 으로
  같은 픽스처를 재는 대조군은 이 태스크 범위 밖이라 돌리지 않았다. **다만 그 분리는 판정을
  바꾸지 못한다** — 위 §「판정」의 구조적 논거(최종 leg 는 `map_categories`·`needs_expansion`
  을 거친 `decision.category_legs` 이지 `decompose` 의 `categoryQueries` 가 아니다) 때문에,
  설령 문면을 고쳐 인라인의 leg 분해가 완벽해지더라도 그 priority 태그를 knapsack 이 쓰는
  최종 leg 까지 배달할 경로 자체가 없다.
- **픽스처는 12셀 · 3~4니즈 · 목적/상황형 발화 한 종류다.** 다른 발화 유형(예: 구조화 조건
  발화)에서의 안정성은 이 프로브가 재지 않는다.
- `--seed` 는 셀 순서에만 쓴다. provider 샘플링 seed 는 강제할 수 없다.
