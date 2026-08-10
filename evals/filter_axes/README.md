# evals/filter_axes — 필터 추출 축별 분해 지표(#334)

`evals/README.md` 공통 규약을 따른다(전부 준수). 이 문서는 축 정의·정규화 규칙·분자/분모
정의(규약8)와, 기존 Filter Accuracy와의 관계를 못 박는다.

## 왜 필요한가

`evals/metrics/metrics.py::filter_accuracy`는 예측·정답 필드 **합집합을 분모**로 한 단일값이다
— 어느 축이 과추출(spurious)인지 소추출(missing)인지, 값이 틀렸는지(valueMismatch) 구분하지
않는다. 실측(pipeline 0.064 vs scripted adapter 1.0)만으로는 원인 축을 특정할 수 없다. #119
형 사고(회원 프로필이 발화에 없는 가격 하드필터를 몰래 승격)도 **축 단위**로 봐야 잡힌다 —
합집합 단일값은 "무언가 틀렸다"만 말해준다.

## 기존 Filter Accuracy와의 관계 (필수 절, #260 규약 — 혼동 금지)

| | `evals.metrics.metrics.filter_accuracy` | `evals.filter_axes` |
|---|---|---|
| 분모 | 예측·정답 필드 **합집합** | 축별 · **evaluated 축 전부**(bothEmpty 제외) |
| 산출 | 단일 스칼라(0~1) | 축마다 `valueStrict`/`presence` precision·recall·F1 |
| 과·소추출 구분 | 안 함(둘 다 감점만) | `spurious`/`missing`로 구분 |
| keyword/semanticQuery | 별개 필드로 감점 | 존재는 서로 흡수(keyword 축 특칙) |

**둘 다 계속 존재한다** — 어느 하나가 다른 하나를 대체하지 않는다. `filterAccuracy`는 옛
baseline(`evals/scoring`, `evals/model_eval` 기존 리포트)과의 연속성을 위해 그대로 두고,
`filterAxes`는 원인 축 진단용으로 병행한다. **같은 이름처럼 보이지만 다른 정의의 숫자를
섞어 비교하지 말 것**(#260 규약) — 리포트에 항상 어느 지표인지 명시한다.

## 축 정의 (`axes.json`, 정본 — 스크립트는 이것만 읽는다)

| 축 | 필드 | evaluated | 비고 |
|---|---|---|---|
| `category` | `category` | ✅ | |
| `price_min` / `price_max` | `priceMin` / `priceMax` | ✅ | |
| `brand` | `brand` | ✅ | 문자열 단일값도 리스트로 정규화 |
| `rating_min` | `ratingMin` | ✅ | |
| `color` | `color` | ✅ | |
| `keyword` | `keyword`, `semanticQuery` | ✅ | 특칙 — 아래 참조 |
| `attr_conditions` | `attrConditions` | ✅ | 키·값 각각 문자열 정규화한 dict 동등 |
| `exclude_product_ids` | `excludeProductIds` | ❌ | 발화 추출이 아니라 구매이력(I-19) 파생 — P/R 부적합, presence만 관측 |
| `total_budget` | `totalBudget` | ❌ | RouteDecision 레벨 필드, `expectedFilters`에 라벨 없음(v1·v2 공통 — `ProductSearchFilters`에 애초에 없는 필드라서다) — probe 관측 전용 |
| `buy_all` | `buyAll` | ❌ | 위와 동일 |

`limit`은 축이 아니다(기계적 기본값, `evals/model_eval` 선례) — `axes.json`의
`excludedFields`에 명시한다.

evaluated=false 축(`exclude_product_ids`·`total_budget`·`buy_all`)은 `evals/metrics`
러너의 케이스별 P/R 집계(`case_axis_outcomes`)에서 제외된다 — dev goldenset의
`expectedFilters`(`ProductSearchFilters` 검증)가 애초에 이 값을 라벨하지 않으므로 분모가
성립하지 않는다(v1·v2 공통 — 골든셋 버전과 무관하게 `ProductSearchFilters` 스키마 자체에
없는 필드라서다). `axis_presence_set`(INV/DIR/leak 판정)에서는 evaluated 여부와 무관하게
전부 포함한다 — "이 축에 값이 실렸는가"는 P/R과 별개 질문이라서다.

## 정규화 규칙

- **문자열**: NFC 정규화 → strip → casefold.
- **brand**: 문자열이면 단일원소 리스트화 후 원소를 각각 문자열 정규화, 정렬해 비교.
- **수치**(price_min/max·rating_min·total_budget): int/float 동등 비교(`5 == 5.0`).
- **attrConditions**: 키·값을 각각 문자열 정규화한 dict로 만들어 동등 비교.
- **boolean**(buy_all): 존재/불리언 값 그대로.
- 값이 "설정됨"의 기준은 `decompose._filter_axes`와 동일 규칙이다 — 빈 컨테이너(`[]`/`{}`/`""`)는
  미설정, 수치 `0`은 설정됨(LLM이 실제로 낸 값이라서).

### keyword 축 특칙

`keyword`(상품명 LIKE)와 `semanticQuery`(의미검색 자연어)는 서로 다른 필드지만 **어휘
차이를 흡수**한다 — 정답에 `keyword`만 있고 예측에 `semanticQuery`만 있어도(또는 그
역) **축 존재로 인정**한다(소추출 아님). 값 일치는 **같은 필드끼리만** 본다 —
`keyword`↔`keyword` 정규화 동등 **또는** `semanticQuery`↔`semanticQuery` 정규화 동등이면
`match`다(리뷰 R3-1: `keyword`만 비교하면 semanticQuery만 있는 필터가 자기 자신과도
match가 안 되는 비반사성 버그가 된다 — 아래 함정과 맞물려 실무에서 흔히 발생). **교차
필드**(정답 `keyword` vs 예측 `semanticQuery` 등)는 리터럴 문자열이 같아도 여전히
`valueMismatch`다 — 존재 흡수와 값 일치는 별개 질문이다.

**중요한 함정**: `app/agents/buyer/recommendation/decompose.py`의 `semantic_query`는
`llm_sq or cat_signal or prior_sq or query`로 **절대 비지 않는 폴백**을 갖는다(디자인
의도 — 벡터 검색 앵커가 항상 있어야 함). 즉 실제 decompose 산출(`RouteDecision.filters`)의
`keyword` 축은 **거의 항상 "존재"로 판정된다** — `keyword`(LIKE) 필드 자체가 비어 있어도
`semanticQuery` 폴백 때문이다. probe의 INV 시나리오에서 "keyword 축을 완전히 잃는다"는
케이스는 재현이 안 된다(fallback이 항상 채운다) — 축소 검증에는 `category`·`price_max`처럼
폴백 없는 축을 쓴다(`tests/unit/test_filter_axes_probe.py` 참조).

## `emptyAxisRule`

`"bothAbsentExcluded"` — 정답·예측 모두 해당 축이 없으면(`bothEmpty`) **분모에서 제외**한다
(TN으로 세지 않는다). 카운트에는 남기되(`counts.bothEmpty`) precision/recall 분모(규약8)에는
들어가지 않는다.

## 분자·분모 정의(규약8, `aggregate_axis_metrics`가 결과에 동봉)

outcome 5종: `match`(값 일치) · `valueMismatch`(존재하지만 값 불일치) · `spurious`(과추출 —
정답엔 없는데 예측에 있음) · `missing`(소추출 — 정답엔 있는데 예측에 없음) · `bothEmpty`(둘
다 없음, 분모 제외).

- `support` = `match + valueMismatch + missing`
- `valueStrict.precision` = `match / (match + valueMismatch + spurious)`
- `valueStrict.recall` = `match / (match + valueMismatch + missing)`
- `presence.precision` = `(match + valueMismatch) / (match + valueMismatch + spurious)`
- `presence.recall` = `(match + valueMismatch) / (match + valueMismatch + missing)`
- 분모가 0이면 값은 **`None`**이다(`0`으로 뭉개지 않는다) — 예: trivial baseline의 precision.
- 집계는 **micro**(케이스 전체를 합산한 뒤 나눔)다 — `evals/metrics` 순위 지표의 macro와
  다르다(호환용이 아니라 축 지표 성격상 micro가 더 안정적이라서).

## trivial baseline 해석

`baselines/trivial_empty/`는 예측을 항상 빈 필터로 고정한 결과다(규약1). 모든 evaluated
축에서 `presence.precision = None`(예측 0건), `presence.recall`은 그 축이 dev에 한 번이라도
라벨됐으면(`support > 0`) `0.0`, 라벨이 아예 없으면 `None`이다. **이 baseline을 못 넘으면
개선이 아니다.** 재생성: `uv run python -m evals.filter_axes.make_trivial_baseline --out
evals/filter_axes/baselines/trivial_empty`. `datasetVersion`/`datasetHash`는 goldenset
manifest에서 읽으므로 v2 전환 후 재실행만으로 갱신된다.

## INV / DIR / pair (CheckList 유형, 규약6)

- **INV**(불변, 라벨 불필요) — 발화를 의미 보존 변형(존댓말·어순·색상 표기)해도 필터 축
  집합·수치값이 불변해야 한다. `judge_invariance`.
- **DIR**(방향, 라벨 불필요) — 제약을 추가한 발화는 ① 기존 축을 잃지 않고 ② 기대한 새 축이
  실제로 붙어야 한다. `judge_direction` + `judge_candidate_subset`(검색 후보가 좁아지는지
  fixture 백엔드로 대조).
- **pair**(#119 재현, 라벨 불필요) — 동일 발화를 게스트/회원(구매 이력 보유)으로 각각
  실행해, **member 축 집합에만 있는 축(`member_set - guest_set`)이 하나라도 있으면**
  `leak`(프로필이 발화에 없는 하드필터를 몰래 승격했다는 신호) — `leakedAxes`로 낱낱이
  보고한다. member가 guest 축 일부를 잃기만 하고 새로 얻은 축이 없으면 leak이 아니다 —
  그 손실 축은 `lostAxes`로 정보 제공만 하고 leak 판정에는 참여하지 않는다(리뷰 F1: "진상위집합"
  조건은 member가 축 하나를 잃으면서 다른 축을 새로 얻는 케이스를 false negative로 놓쳤다).
  `judge_profile_leak` → `{"leak": bool, "leakedAxes": [...], "lostAxes": [...]}`.

probe(`probe.py`)는 **수동 도구다 — CI에 넣지 않는다**(규약3). 실 LLM을 호출하므로 비용이
든다: `uv run python -m evals.filter_axes.probe --out artifacts/fax-run1`. 위반이 있으면
exit code 1.

## 브랜드 추출 축 (#466, `brand_probe.py` + `brand_cases.json`)

위 `brand` 축은 **정의만 있고 실질 표본이 없다** — goldenset dev 109건 중 `expectedFilters.brand`
라벨은 `buy-over-0003` **1건뿐**이라 support≈1 이고, 그 축의 P/R 로는 "브랜드-only 발화에서
브랜드가 뽑히는가"를 잴 수 없다. #466 이 그 공백에서 나왔다(브랜드-only 발화의 추출 실패가
`evals/underspecified_probe` 의 과소지정 **오탐**으로만 간접 관측되고 있었다).

그래서 라벨 공수 없이 규모를 늘릴 수 있는 **MFT(positive) + 오추출 대조(negative)** 슬라이스를
따로 뒀다. 위 INV/DIR/pair probe(`probe.py`·`probe_cases.jsonl`)와는 **데이터셋·해시가 별개**다
— 숫자를 섞어 비교하지 말 것(규약2·8).

    uv run python -m evals.filter_axes.brand_probe --n 3 --label after --out artifacts/brand-after
    uv run python -m evals.filter_axes.brand_probe --n 3 --prompt old_system.txt --label before ...

`--prompt` 로 후보 `_SYSTEM` 을 갈아끼워 A/B 한다(`underspecified_probe --prompt` 와 같은 수단).
수동 도구다 — **CI 에 넣지 않는다**(규약3). 채점·집계 함수만 `tests/unit/test_brand_probe.py`
로 CI 고정한다.

### 축 4종과 분자·분모 (규약8)

분모는 `positives × n`(=20n), `spurious` 만 `negatives × n`(=4n).

| 축 | 분자 | 무엇을 잡는가 |
|---|---|---|
| `present` | `filters.brand` 가 빈 값이 아닌 표본 | 추출 자체 |
| `verbatim` | 산출 값이 **전부** 발화 안에 그대로 있는 표본 | 번안("애플"→`Apple`). `brandName` 은 exact IN 이라 번안은 조용히 빗나간다 |
| `expected` | 케이스가 라벨한 브랜드와 정규화 동등한 값을 포함한 표본 | 총칭어 오추출("삼성 제품"→`["제품"]`) |
| `spurious` | **negative** 발화에서 brand 가 채워진 표본 (**낮을수록 좋다**) | trivial baseline 대조 — 없으면 "전부 브랜드로 찍기"가 만점을 받는다(규약1) |

`verbatim ⊆ present` 다. `expected` 는 `verbatim` 과 서로 포함 관계가 아니다.

### 실측 (gpt-5-nano = 배포 fast 티어, n=3, 전/후 각 2런)

| 축 | before(dev 프롬프트) | after(브랜드 절) |
|---|---|---|
| `present`  | 17·19 / 60 | 45·42 / 60 |
| `verbatim` | 13·11 / 60 | 45·42 / 60 |
| `expected` | 13·11 / 60 | 45·42 / 60 |
| `spurious` | 0·0 / 12   | 0·0 / 12   |

사전 등록 문턱은 **"after 두 런 모두 before 최댓값 이상"**이었고 45·42 ≫ 19 로 통과했다.
after 에서 세 축이 같은 값인 것은 **뽑힌 브랜드가 전부 발화 원문 표기이자 라벨 일치**라는 뜻이다.
before 의 대표 실패: 애플 발화에서 추출된 8표본이 전부 `Apple`(verbatim 0), 락앤락 0/6.
인접 레인(#443/#465)이 같은 티어에서 잰 런간 폭이 ≈5/48 이라, 이 효과는 그 노이즈 대역
밖이다. `--n` 을 키우지 않고 이 문턱을 쓴 근거가 그 폭이다.

### 이 축이 재지 **않는** 것 — 카탈로그 표기 도달

브랜드가 원문 그대로 뽑혀도 I-1 은 exact IN 이라 **카탈로그의 다른 표기**에는 닿지 않는다.
운영 시드 실측(brand 2,368행 × product 6,559건 조인):

| 발화 | 원문 표기 도달 | 같은 회사 총합 | 도달률 |
|---|---|---|---|
| "삼성" | 7 | 78 (`삼성전자` 71) | 9.0% |
| "LG" | 1 | 38 (`LG전자` 37) | 2.6% |
| "애플" | 1 | 8 (`Apple` 7) | 12.5% |
| "나이키" | 106 | 110 | 96.4% |
| "아디다스" | 83 | 94 | 88.3% |

`app.pipelines.brand_aliases` 가 와이어에서 이 몫을 덮는다 — 법인 접미사(양방향)와 음차 쌍
(양방향)을 **가산적으로** 덧붙인다. 카탈로그 사본을 두지 않는다(§4.6 이 미존재 이름을 무시하므로
틀린 후보는 공짜다).

#### 시드 대조 (재현 절차) — 규칙을 건드리면 **이걸 먼저 돌린다**

확장이 실제로 잇는 쌍이 전부 같은 회사인지 전수 확인한다. 아래는 2026-08-10 시드 기준 결과이며,
**CI 가 다시 확인하지 않는다** — BE 가 `한샘전자` 같은 무관한 행을 추가하면 조용히 오염이
시작되므로, 브랜드 규칙(접미사·음차 쌍)을 넓히거나 시드가 갱신되면 재실행할 것.

```bash
cd ~/inte-final/_sql/mariadb && uv run --project <repo> python - <<'EOF'
import re, collections
from app.pipelines.brand_aliases import expand_brands, _norm
brands, counts = {}, collections.Counter()
pat = re.compile(r"\((\d+),(?:NULL|\d+),'((?:[^'\\]|\\.)*)'")
for line in open('20_brand.sql', encoding='utf-8'):
    for m in pat.finditer(line):
        brands[m.group(1)] = m.group(2)
prow = re.compile(r'\((\d+),(\d+),')
for line in open('30_product.sql', encoding='utf-8'):
    if line.startswith('('):
        for m in prow.finditer(line):
            counts[m.group(2)] += 1
byname = collections.Counter()
for bid, n in counts.items():
    if bid in brands:
        byname[brands[bid]] += n
bynorm = {}
for name, n in byname.items():
    bynorm.setdefault(_norm(name), []).append((name, n))
for T in sorted(byname):
    for cand in expand_brands([T], cap=12)[1:]:
        for name, n in bynorm.get(_norm(cand), []):
            if _norm(name) != _norm(T):
                print(f'{T!r}({byname[T]}) -> {name!r}({n})')
EOF
```

2026-08-10 결과 — **12 방향 = 6쌍, 전부 같은 브랜드, 교차 오염 0건**:

| 쌍 | 상품 수 | 종류 |
|---|---|---|
| `삼성` ↔ `삼성전자` | 7 ↔ 71 | 법인 접미사 |
| `LG` ↔ `LG전자` | 1 ↔ 37 | 법인 접미사 |
| `한일` ↔ `한일전자` | 1 ↔ 1 | 법인 접미사 (같은 회사라는 근거는 이름 추론뿐 — 양쪽 1건이라 무해로 수용) |
| `애플` ↔ `Apple` | 1 ↔ 7 | 음차 |
| `나이키` ↔ `Nike` | 106 ↔ 1 | 음차 |
| `아디다스` ↔ `adidas` | 83 ↔ 2 | 음차 |

`삼성도어`·`삼성메디칼`은 접미사 화이트리스트가 닫혀 있어 **구조적으로** 배제된다.

#### 남은 몫

`NIKE 나이키`·`나이키 NIKE`·`나이키(NIKE)`·`아디다스 오리지널스` 같은 **행 안에서 두 표기를
붙여 쓴 변형**은 규칙으로 유도할 수 없어 남아 있다(나이키 기준 110건 중 4건). 닫으려면 행 단위
매핑이 필요한데 그건 카탈로그 사본이라(CLAUDE.md·api-spec C-28 "차기 DB 정리 대상") 여기서
하지 않았다. 다시 잴 때 위 표를 기준선으로 쓸 것.

## datasetVersion 정책

`cases/manifest.json`의 `datasetVersion`(`fax-1.0.0`)은 probe 케이스 파일 자체의 버전이다
— goldenset(`buyer-goldenset`, dev 현재 `datasetVersion 2.1.0`)과는 **별개 버전 계보**다
(규약2, "하나의 거대 골든셋으로 합치지 않는다"). goldenset 버전이 바뀌면: ① `baseCaseId`로
연결된 probe 케이스의 `baseQuery`가 여전히 유효한지 확인(`tests/unit/test_filter_axes_cases.py`
의 참조 무결성 테스트가 자동 확인), ② 깨졌으면 새 goldenset 발화로 교체하고
`cases/manifest.json`의 `datasetHash`를 재계산, ③ `fax-1.1.0`으로 버전을 올린다(케이스
**내용이 바뀔 때만** — 검증만 통과하고 내용이 그대로면 파일·버전을 그대로 둔다).

**실측(#334 r4)**: goldenset이 v1(`1.0.0`)에서 v2(`2.1.0`, #333)로 전환된 뒤 위 절차를
실제로 밟았다 — probe 케이스가 참조하는 12개 `baseCaseId` 전부가 v2 dev에 그대로 남아 있고
`baseQuery`도 전부 일치해(v1의 31개 caseId·query가 v2에 보존됨) **내용 변경이 없었으므로
`probe_cases.jsonl`·`cases/manifest.json`·`fax-1.0.0`을 그대로 뒀다.**
