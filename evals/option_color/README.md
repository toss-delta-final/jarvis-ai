# evals/option_color — "그 색은 옵션에 없다" 잔존 문제 측정 (#454 Phase 1)

> 공통 규약은 `evals/README.md`(정본) — 이 문서는 그 규약을 이 하네스에 적용한 결과만 적는다.

## 왜

BE 가 옵션별 재고를 도입해(#508, 2026-08-09 배포) 품절 옵션은 이제 I-1 `options`/`optionCount`
에서 제외된다. 그런데도 **#454 증상이 남는다** — BE 의 색상 매칭은 `attributes.색상` 축에서,
품절 제외는 `options` 축에서 각각 따로 일어나 두 축이 서로 모르기 때문이다. 운영 실측
(`color=블랙` 검색): 164건 중 옵션 있는 144건 중 77건이 옵션 목록에 블랙 계열이 하나도 없다.

이 하네스는 그 잔존 문제의 크기를 **전수(census)** 로 잰다 — 표본을 뽑아 통계 추론을 하는
평가가 아니라, 로컬 카탈로그 미러 전체(ON_SALE)를 신 계약으로 변환해 판정식을 그대로 돌린다.

## 이 하네스가 `evals/README.md` 8항과 다른 점 (명시적 편차)

카탈로그 전량을 결정론으로 세는 전수 조사라 아래 4개 항목은 **적용 대상이 아니다** — 무시한
게 아니라 성격이 다르다는 판단이다:

- **① trivial baseline** — "아무것도 안 하는 기준선"과 비교할 모델이 없다(순위·분류기가
  아니라 있는 그대로의 카탈로그를 센다). 대신 §"검증"에서 **하네스 자체가 운영 실측과
  일치하는지**를 1급 관문으로 둔다 — 이게 이 하네스의 신뢰 기준선이다.
- **④ 표본 크기 사전 산정** — 표본을 뽑지 않는다(카탈로그 전량, ON_SALE 6,310건 / 옵션
  21,373행). 분산·검정력 계산이 성립하지 않는다.
- **⑤ 다중 비교 통제** — 단일 confirmatory 지표(`unbuyable_rate`)를 primary 로 못박는다
  (아래 「지표」). 나머지는 진단용 exploratory.
- **⑥ CheckList MFT/INV/DIR** — 라벨링된 케이스가 없어(전수 카운트) 유형 표기 대상이 없다.

**② `caseId` 척추 공유**·**③ 결정론 CI/확률 수동**·**⑦ 하네스는 PR 에 커밋**·**⑧ 분자·분모
동봉**은 그대로 따른다 — 특히 ⑧은 `harness.py::measure()` 의 반환값 자체가 색상별·합계별로
`{numerator, denominator, ratio}` 를 싣는다(아래 「지표」).

## 축

색상 20개(고유어 10 + 외래어 10, `_COLOR_WORDS`) — 카탈로그 실측 상위 색상 어휘, `docs/specs/
MEASURE-OPTION-COLOR-454.md` §2 와 같은 목록. 각 색상을 독립 쿼리로 취급해 색상별 표 + 20색
합계를 함께 낸다(단일 색상만 재면 편향된다는 패킷 지적을 그대로 반영).

## 판정식 (Phase 2 구현이 쓸 식과 동일 — 하네스가 먼저 잰다)

후보 하나가 **A~D 를 모두** 만족하면 "이 상품에서 그 색을 고를 수 없다"(`unbuyable`):

| | 조건 | 구현 |
|---|---|---|
| A | 이번 턴에 색상 조건이 있다 | 하네스는 색상마다 독립 쿼리라 항상 참 |
| B | `attributes.색상` 이 복수(2개 이상) | `len(attribute_colors) >= 2` |
| C | `optionCount == len(options)`(절단 아님) | 신 계약에서도 20개 초과면 절단(§ "신 계약 재현") |
| D | 승인 동의어 확장 집합 중 어느 것도 옵션 이름에 안 나타남 | `app.agents.buyer.cart.options.narrow_options`
    의 R2(`by_condition`, `color_synonyms` 확장)를 **그대로 호출**한다 — 판정 로직을 재구현하지
    않는다(#454 되물음 좁히기의 실제 함수, "배포 경로 함수는 그대로 부르고 판정 규칙은
    재구현하지 않는다"는 `evals/combo_matrix/fakes.py` 관례와 같다) |

D 가 거짓(옵션에 그 색이 있음)이면 애초에 조사 대상이 아니다(`matched` 버킷). D 가 참인
후보만 B/C 로 더 나눈다:

- `attributes.색상` 축 자체가 없음(운영 배경 표에 없는 케이스, 별도 진단) → `no_axis`
- 단일색(B 거짓) → `single_color_ok`(정상 — 사이즈만 고르면 된다)
- 복수색인데 절단됨(B 참, C 거짓) → `truncated_holdout`(20개 밖에 그 색이 있을 수 있어 판정 보류)
- 복수색이고 절단 아님(B 참, C 참) → `unbuyable`(진짜 문제)

## 신 계약 재현 — BE 마이그레이션 규칙 오프라인 재현

로컬 BE(`localhost:8080`)는 옵션별 재고 배포 이전 구버전이라 신 계약 실응답을 직접 못 받는다.
대신 BE 마이그레이션(`migrate-2026-08-09-option-stock-1-expand.sql`)의 **결정적** 초기 재고
규칙을 오프라인 재현한다:

```sql
CASE WHEN po.id % 7 = 0 THEN 0 ELSE 20 + CRC32(po.id) % 81 END
```

파이썬 재현: `zlib.crc32(str(option_id).encode())`(`harness.py::option_stock`). 품절 옵션 제외 →
`optionCount = len(구매 가능 목록)` → 20개 초과면 `options` 만 절단(`optionCount` 는 절단 전
그대로) → 전 옵션 품절이면 상품째 검색 결과에서 제외. api-spec §4.6 개정과 동일 규칙이다.

## ⚠️ 검증 — 하네스가 맞는지 먼저 증명한다

**이게 안 되면 나머지 수치는 무의미하다.** `main()` 이 매 실행마다 자동으로 확인하고, 실패하면
지표를 내지 않고 즉시 종료(exit 1)한다:

1. **CRC32 알고리즘 동치** — MariaDB `CRC32(id)` 와 파이썬 `zlib.crc32(str(id).encode())` 가
   같은 값을 내는지. 2026-08-10 로컬 `jarvis-mariadb` 표본 10개(`select id, CRC32(id) from
   product_option order by id limit 10`)를 직접 대조해 **10/10 일치**를 확인했고, 그 표본을
   `verify_against_production()` 에 고정해 매 실행마다 재확인한다(운영 DB 접속 없이 회귀 감지).
2. **결정적 대조** — `productId=26368525` 의 구 계약 총 옵션 161개에 이 규칙을 적용하면
   품절 23개 → 신 계약 `optionCount` **138**. 운영 실측(task-4, 노션 확인 + BE 코드 열람)의
   161→138(23개 제외)과 **정확히 일치**함을 2026-08-10 로컬 미러로 직접 확인했다.

## 지표 (분자·분모 동봉, 규약 8항)

1. **`unbuyable_rate`**(★ primary confirmatory) — 옵션 있는 반환 후보 중 판정식(A~D)에 걸리는
   비율. 분모 = 그 색으로 BE 매칭되고 옵션이 있는 후보 수(옵션이 아예 없는 단일 SKU 상품은
   "옵션에 그 색이 없다"는 질문 자체가 성립하지 않아 분모에서 뺀다 — 운영 실측 "164건→옵션
   있는 것 144건"과 같은 스코핑).
2. `single_color_rate` — 옵션에 그 색이 없지만 `attributes.색상` 단일이라 정상인 비율(exploratory).
3. `truncated_holdout_rate` — 절단이라 판정 보류한 비율(exploratory).
4. `no_axis_rate` — `attributes.색상` 축 자체가 없는데 D 가 참인 비율(exploratory, 운영 배경
   표에 없던 케이스라 별도로 낸다 — single/multi 어느 쪽에도 강제로 편입하지 않는다).
5. `candidates_per_query`(중앙값/최소/최대) — 쿼리(색상)당 BE 매칭 후보 수. 옵션 유무와 무관하게
   전부 센다 — Phase 3 에서 "사후필터가 후보를 얼마나 줄였는지" 볼 기준선.

## 한계 (지어내지 않고 명시)

- **옵션이 아예 없는 상품(단일 SKU)의 자체 재고는 시뮬레이션하지 않는다.** `product_stock` 은
  `option_id IS NULL` 행으로 그런 상품의 재고도 갖고 있지만(운영 실측: 24,390 = 21,373(옵션)
  + 3,017(옵션 없는 상품)), 그 행의 초기화 규칙을 패킷이 주지 않았다. 이 하네스는 그런 상품을
  **항상 검색에 남아있는 것으로 취급**한다 — 과소 제외 방향(안전한 쪽: `unbuyable_rate` 분모를
  부풀리지 않는다. `candidates_per_query` 는 실제보다 약간 클 수 있다).
- **BE 색상 매칭(③ 있으면 부분 일치)은 근사다** — `regexp_instr` 를 부분 문자열 포함으로
  흉내낸다(`scripts/measure_option_color_miss_454.py::_product_matches_concept` 와 같은 근사,
  §1 인용).
- 운영과 로컬 미러의 **스냅샷 시점이 다를 수 있다** — 로컬 미러가 그 사이 갱신되지 않았다는
  보장은 없다(다만 §"검증"의 161→138 대조가 이 스냅샷에서도 여전히 정확히 일치함을 보였다).

## 실행

```bash
docker exec -i jarvis-mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B \
  -e "select p.id, p.status, coalesce(p.attributes,\"\") from product p;"' > products.tsv
docker exec -i jarvis-mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B \
  -e "select o.id, o.product_id, o.name from product_option o;"' > options.tsv

uv run python -m evals.option_color \
    --products-tsv products.tsv --options-tsv options.tsv \
    --catalog-dsn "postgresql://jarvis:jarvis@localhost:5433/catalog" \
    --out evals/option_color/results/before-2026-08-10.json
```

`--out` 은 선택이다(생략하면 표준출력만). 상시 실행 대상이 아니다(조사용) — 카탈로그 TSV 덤프
+ pg-catalog `color_synonyms` 승인 사전이 모두 있어야 돌아간다. 테스트에서 import 되지 않는다.
