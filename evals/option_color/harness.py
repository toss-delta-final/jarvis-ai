"""#454 Phase 1 — 옵션 단위 재고 도입 후에도 남는 "그 색 옵션이 없다" 문제의 크기(before).

패킷 §"판정식" 그대로: 후보 하나가 A~D 를 **모두** 만족하면 "이 상품에서 그 색을 고를 수 없다".

    A  이번 턴에 filters.color 가 있다               (이 하네스는 색상마다 독립 쿼리로 돈다 — 항상 참)
    B  attributes.색상 이 복수(2개 이상)               len(attribute_colors) >= 2
    C  optionCount == len(options) (절단 아님)         BE 는 절단 전 개수를 optionCount 로 준다
    D  승인 동의어 확장 집합 중 어느 것도 옵션 이름에 안 나타남   `app.agents.buyer.cart.options.narrow_options`
       의 R2(`by_condition`, color_synonyms 확장)를 그대로 호출해 판정한다 — 판정 로직을
       재구현하지 않는다(실제 되물음 좁히기와 같은 함수).

로컬 BE(`localhost:8080`)는 옵션별 재고 배포 이전 구버전이라 신 계약 응답을 직접 못 받는다.
대신 BE 마이그레이션 스크립트(`migrate-2026-08-09-option-stock-1-expand.sql`)의 **결정적** 초기
재고 규칙을 오프라인 재현한다:

    CASE WHEN po.id % 7 = 0 THEN 0 ELSE 20 + CRC32(po.id) % 81 END

`CRC32(po.id)` 는 id 를 10진 문자열로 바꾼 뒤의 CRC32 다(MariaDB 관례) — `zlib.crc32(str(id)
.encode())` 로 정확히 재현된다(§검증, 이 파일의 `verify_against_production` 이 매번 이를 직접
비교해 확인한다. 표본 10개 전부 일치 — 2026-08-10 로컬 `jarvis-mariadb`로 검증).

**모수 규약** — "반환 후보"(BE 색상 매칭 성립, §4.6 3갈래: ①미지정 n/a ②축 없으면 통과
③있으면 부분일치)는 옵션 유무와 무관하게 전부 센다(`candidates_per_query`). 단 A~D 비율
(`unbuyable_rate` 등)의 분모는 **옵션이 있는 후보만**이다 — 옵션이 아예 없는 단일 SKU 상품은
"그 옵션에 색이 없다"는 질문 자체가 성립하지 않는다(운영 실측 "164건 → 옵션 있는 것 144건"과
같은 스코핑, 패킷 배경 절 참조). attributes.색상 축 자체가 없는 후보(`no_axis`)는 B/단일색
어느 쪽도 아니라 별도 진단 버킷으로 센다 — 패킷 배경 표에 없는 케이스라 임의로 편입하지 않는다.

**하지 않는 것(한계로 명시)** — 옵션이 아예 없는 상품(product_option 0행)의 자체 재고
(`product_stock.option_id IS NULL` 행) 시뮬레이션은 지원하지 않는다 — 그 초기화 규칙을 패킷이
주지 않았다. 그런 상품은 항상 "검색에 남아있다"로 취급한다(과소 제외 방향 — candidates_per_query
가 실제보다 약간 클 수 있다는 뜻이고, 배제 방향이 아니므로 unbuyable_rate 분모를 부풀리지 않는
안전한 쪽이다).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import zlib
from dataclasses import dataclass

_NULL_TOKENS = frozenset({"", "NULL", "\\N"})

# 고유어 10 + 외래어 10 — 패킷 §1-3/`docs/specs/MEASURE-OPTION-COLOR-454.md` 와 같은 목록.
_COLOR_WORDS: tuple[str, ...] = (
    "검정",
    "흰색",
    "회색",
    "빨강",
    "파랑",
    "노랑",
    "초록",
    "보라",
    "남색",
    "분홍",
    "블랙",
    "화이트",
    "그레이",
    "레드",
    "블루",
    "옐로우",
    "그린",
    "퍼플",
    "네이비",
    "핑크",
)

_MIGRATION_SQL_RULE = "CASE WHEN po.id % 7 = 0 THEN 0 ELSE 20 + CRC32(po.id) % 81 END"


class HarnessError(RuntimeError):
    """입력 파일·검증 실패 — 원인을 명시하고 raw 예외 스택은 노출하지 않는다."""


def option_stock(option_id: int) -> int:
    """BE 마이그레이션 규칙(원문 그대로) — 7의 배수 옵션 id 는 품절, 아니면 20~100."""
    if option_id % 7 == 0:
        return 0
    return 20 + (zlib.crc32(str(option_id).encode()) % 81)


@dataclass(frozen=True)
class OptionRow:
    option_id: int
    name: str


@dataclass(frozen=True)
class ProductRow:
    """구 계약(옵션별 재고 도입 전) 상품 1건 — 여기서 신 계약 뷰를 유도한다."""

    product_id: int
    attribute_colors: tuple[str, ...]
    all_options: tuple[OptionRow, ...]


# [task-6] attributes JSON 파싱 실패율이 이 비율을 넘으면 입력 형식을 의심해 중단한다 — 컬럼이
# 밀리면(예: stock_quantity 가 낀 4컬럼 덤프) 파싱이 "조용히" 대량 실패해 전부 "색상 축 없음"
# ②갈래로 새는 사고가 실제로 있었다(0% 로 보이는 개선 효과 100%짜리 거짓 결론).
_MAX_ATTRIBUTES_PARSE_FAILURE_RATE = 0.5

_PRODUCTS_DUMP_HINT = (
    'docker exec -i jarvis-mariadb sh -c \'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B '
    '-e "select p.id, p.status, coalesce(p.attributes,\\"\\") from product p;"\' > products.tsv'
)


def _parse_attributes(attributes_raw: str) -> tuple[tuple[str, ...], bool]:
    """attributes JSON 을 파싱해 (색상 값들, 파싱 성공 여부)를 낸다.

    `NULL`/`\\N`/빈 문자열은 "정당한 결측"이라 성공으로 친다 — 실패는 attributes 컬럼에 값이
    있는데 JSON 으로 못 읽는 경우뿐이다(컬럼이 밀리면 이 실패가 대량 발생한다, task-6 실측:
    4컬럼 덤프를 넣으면 `stock_quantity` 숫자가 attributes 자리로 밀려 전량 실패했다).
    """
    if attributes_raw in _NULL_TOKENS:
        return (), True
    try:
        attributes = json.loads(attributes_raw)
    except (TypeError, ValueError):
        return (), False
    if not isinstance(attributes, dict):
        return (), False
    value = attributes.get("색상")
    if value is None:
        return (), True
    if isinstance(value, str):
        return ((value,) if value.strip() else ()), True
    if isinstance(value, list):
        return tuple(str(v) for v in value if isinstance(v, str) and v.strip()), True
    return (), True


def load_products(products_tsv: pathlib.Path, options_tsv: pathlib.Path) -> list[ProductRow]:
    """ON_SALE 상품을 (attributes.색상, 구 계약 옵션 전체[id, name])로 읽는다.

    `products.tsv` 컬럼은 **정확히 3개**(`id, status, attributes(JSON)`)여야 한다 — 3개 미만도
    3개 초과(예: `stock_quantity` 가 낀 4컬럼)도 에러다. `options.tsv` 컬럼: `option_id,
    product_id, name`. 덤프 명령은 `docs/specs/MEASURE-OPTION-COLOR-454.md` §2 「재현 방법」과
    같되 옵션 쪽에 `o.id` 를 추가한다:

        docker exec -i jarvis-mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B \\
          -e "select p.id, p.status, coalesce(p.attributes,\\"\\") from product p;"' > products.tsv
        docker exec -i jarvis-mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B \\
          -e "select o.id, o.product_id, o.name from product_option o;"' > options.tsv

    로드 후 두 가지를 추가로 검증한다(입력 형식이 틀렸을 때 "그럴듯하지만 완전히 틀린 결과"를
    조용히 내지 않기 위해, task-6 회귀):
    1. `attributes` JSON 파싱 실패율이 `_MAX_ATTRIBUTES_PARSE_FAILURE_RATE` 를 넘으면 중단.
    2. 색상 축을 가진 상품이 0건이면 중단(정상 카탈로그에서 있을 수 없다).
    """
    if not products_tsv.exists() or not options_tsv.exists():
        missing = [p for p in (products_tsv, options_tsv) if not p.exists()]
        raise HarnessError(f"입력 파일을 찾지 못했습니다: {', '.join(str(p) for p in missing)}")

    statuses: dict[int, str] = {}
    attribute_colors: dict[int, tuple[str, ...]] = {}
    parse_attempts = 0
    parse_failures = 0
    for line_no, line in enumerate(products_tsv.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise HarnessError(
                f"{products_tsv}:{line_no} — 컬럼이 정확히 3개(id, status, attributes)여야 하는데 "
                f"{len(fields)}개입니다: {line[:120]!r}. stock_quantity 가 낀 4컬럼 덤프를 썼다면 "
                f"그 컬럼을 빼고 다시 뜨세요:\n    {_PRODUCTS_DUMP_HINT}"
            )
        pid = int(fields[0])
        statuses[pid] = fields[1]
        colors, parse_ok = _parse_attributes(fields[2])
        attribute_colors[pid] = colors
        parse_attempts += 1
        if not parse_ok:
            parse_failures += 1

    parse_failure_rate = parse_failures / parse_attempts if parse_attempts else 0.0
    print(
        f"attributes JSON 파싱: 시도 {parse_attempts:,}건, 실패 {parse_failures:,}건 "
        f"({parse_failure_rate * 100:.1f}%)"
    )
    if parse_failure_rate > _MAX_ATTRIBUTES_PARSE_FAILURE_RATE:
        raise HarnessError(
            f"attributes JSON 파싱 실패율 {parse_failure_rate * 100:.1f}%가 "
            f"{_MAX_ATTRIBUTES_PARSE_FAILURE_RATE * 100:.0f}%를 넘습니다 — 입력 형식이 틀렸을 "
            f"가능성이 높습니다(컬럼 순서·개수를 확인하세요). {_PRODUCTS_DUMP_HINT}"
        )

    options: dict[int, list[OptionRow]] = {}
    for line_no, line in enumerate(options_tsv.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            continue
        fields = line.split("\t", 2)
        if len(fields) < 3:
            raise HarnessError(
                f"{options_tsv}:{line_no} — 컬럼이 3개 미만입니다(option_id, product_id, name"
                f" 순서인지 확인하세요): {line[:120]!r}"
            )
        option_id, pid, name = int(fields[0]), int(fields[1]), fields[2]
        options.setdefault(pid, []).append(OptionRow(option_id=option_id, name=name))

    products = [
        ProductRow(
            product_id=pid,
            attribute_colors=attribute_colors.get(pid, ()),
            all_options=tuple(rows),
        )
        for pid, rows in options.items()
        if statuses.get(pid) == "ON_SALE"
    ]
    # 옵션이 아예 없는 ON_SALE 상품도 모수에 필요하다(candidates_per_query 의 "옵션 없음" 갈래) —
    # options 딕셔너리엔 없지만 statuses 엔 있는 productId.
    for pid, status in statuses.items():
        if status == "ON_SALE" and pid not in options:
            products.append(
                ProductRow(
                    product_id=pid, attribute_colors=attribute_colors.get(pid, ()), all_options=()
                )
            )
    if not products:
        raise HarnessError("ON_SALE 상품을 찾지 못했습니다 — 시드가 비어 있을 수 있습니다.")

    with_color_axis = sum(1 for p in products if p.attribute_colors)
    if with_color_axis == 0:
        raise HarnessError(
            "색상 축(attributes.색상)을 가진 상품이 0건입니다 — 정상 카탈로그에서는 있을 수 "
            "없습니다. attributes JSON 파싱이 실제로는 됐는데 값이 다 비어 있거나, 컬럼이 밀린 "
            f"입력일 가능성이 높습니다. {_PRODUCTS_DUMP_HINT}"
        )
    print(f"색상 축 있는 상품: {with_color_axis:,} / {len(products):,}")
    return products


@dataclass(frozen=True)
class NewContractView:
    """#508 신 계약(품절 제외) 적용 후 이 상품이 I-1 에 실릴 모습."""

    product_id: int
    attribute_colors: tuple[str, ...]
    options: tuple[str, ...]  # 이름만, 최대 20개(절단)
    option_count: int  # 절단 전 "구매 가능한 것" 전체 개수
    excluded: bool  # 전 옵션 품절 — 검색 결과에서 아예 빠진다


def apply_new_contract(product: ProductRow) -> NewContractView:
    """#508 계약(api-spec §4.6) 그대로 — 품절 옵션 제외, 20개 절단, 전 옵션 품절이면 제외."""
    stocked = [o for o in product.all_options if option_stock(o.option_id) > 0]
    option_count = len(stocked)
    excluded = bool(product.all_options) and option_count == 0
    names = tuple(o.name for o in stocked[:20])
    return NewContractView(
        product_id=product.product_id,
        attribute_colors=product.attribute_colors,
        options=names,
        option_count=option_count,
        excluded=excluded,
    )


def _normalize(value: str) -> str:
    return value.strip().casefold()


def be_color_matches(view: NewContractView, equivalents: set[str]) -> bool:
    """BE 색상 매칭 3갈래(api-spec §4.6) 중 ②③ 근사 — ①(미지정)은 이 하네스에서 항상 지정된다.

    ② attributes 에 색상 축이 없으면 통과. ③ 있으면 부분 문자열 포함(BE `regexp_instr`).
    `scripts/measure_option_color_miss_454.py::_product_matches_concept` 와 같은 근사 방식.
    """
    if not view.attribute_colors:
        return True
    return any(eq in _normalize(v) for v in view.attribute_colors for eq in equivalents)


_Bucket = str  # "no_options" | "matched" | "no_axis" | "single_color_ok" | "truncated_holdout" | "unbuyable"


def judge(
    view: NewContractView,
    color_word: str,
    synonym_map: dict[str, list[str]],
    *,
    min_term_len: int,
    match_suffixes: list[str],
) -> _Bucket:
    """판정식 A~D — D 는 `narrow_options`(실 R2 로직, 재구현 아님)를 그대로 호출한다."""
    from app.agents.buyer.cart.options import narrow_options  # noqa: PLC0415 - 하네스 전용 지연 import
    from app.schemas.spring import CartOption  # noqa: PLC0415

    if not view.options:
        return "no_options"

    options = [CartOption(option_id=i, name=n) for i, n in enumerate(view.options)]
    narrowing = narrow_options(
        options,
        message="",
        terms=(color_word,),
        min_term_len=min_term_len,
        match_suffixes=match_suffixes,
        color_synonyms=synonym_map,
    )
    # D: 승인 동의어 확장 어느 것도 옵션 이름에 안 나타남 — "0건 매칭"만 D=참(전건 일치는 매칭임).
    d_no_match = narrowing.by_condition == () and not narrowing.condition_matched_all
    if not d_no_match:
        return "matched"

    if not view.attribute_colors:
        return "no_axis"
    b_multi = len(view.attribute_colors) >= 2
    if not b_multi:
        return "single_color_ok"
    c_not_truncated = view.option_count == len(view.options)
    if not c_not_truncated:
        return "truncated_holdout"
    return "unbuyable"


def _cross_check_unbuyable(
    view: NewContractView, color_word: str, synonym_map: dict[str, list[str]], settings
) -> bool:
    """하네스 `judge()` 의 "unbuyable" 판정이 **실제 구현**(Phase 2,
    `app.services.search_service._is_color_unbuyable`)과 일치하는지 그 함수를 직접 호출해
    대조한다 — 판정 로직을 하네스가 몰래 다르게 재구현했는지 검증하는 자리(재구현 금지 원칙을
    스스로도 어기지 않았는지 스스로 증명한다). Phase 3 "after" 수치는 이 실제 함수의 산출로
    낸다 — `judge()` 는 "before"(§1-2 검증까지만 책임)에만 쓴다.
    """
    from app.schemas.spring import SpringProduct  # noqa: PLC0415
    from app.services.search_service import _is_color_unbuyable  # noqa: PLC0415

    product = SpringProduct(
        product_id=view.product_id,
        name="x",
        attributes={"색상": list(view.attribute_colors)} if view.attribute_colors else None,
        options=list(view.options) if view.options else None,
        option_count=view.option_count,
    )
    return _is_color_unbuyable(product, color_word, synonym_map, settings)


def _concept_equivalents(word: str, synonym_map: dict[str, list[str]]) -> set[str]:
    from app.pipelines.color_synonyms import expand_color  # noqa: PLC0415

    return {_normalize(v) for v in expand_color(word, synonym_map)}


def load_synonym_map(catalog_dsn: str) -> dict[str, list[str]]:
    """런타임과 같은 경로 — `app.pipelines.color_synonyms.get_synonym_map` 을 그대로 호출한다."""
    from app.core.config import Settings  # noqa: PLC0415 - 하네스 전용 지연 import
    from app.pipelines import color_synonyms  # noqa: PLC0415 - lazy DB 경로

    settings = Settings(_env_file=None)
    try:
        mapping = color_synonyms.get_synonym_map(
            catalog_dsn, ttl_s=settings.color_synonym_cache_ttl_s, warn_if_empty=True
        )
    except Exception as exc:  # noqa: BLE001 - 접속 실패를 사용자 친화적으로 알림(raw 스택 X)
        raise HarnessError(
            f"pg-catalog color_synonyms 조회 실패({catalog_dsn!r}). 원인: {type(exc).__name__}: {exc}"
        ) from None
    if not mapping:
        raise HarnessError("color_synonyms 승인 사전이 비어 있습니다.")
    return mapping


# ─────────── §1-2 하네스 검증 — "먼저 증명하라" ───────────


@dataclass(frozen=True)
class VerificationResult:
    crc32_samples_checked: int
    crc32_all_match: bool
    product_26368525_old_total: int | None
    product_26368525_new_option_count: int | None
    product_26368525_matches_production: bool


def verify_against_production(products: list[ProductRow]) -> VerificationResult:
    """§1-2 — CRC32 알고리즘 동치 + productId=26368525 의 161→138 결정적 대조.

    CRC32 동치는 로컬 `jarvis-mariadb` 표본 10개(id, `CRC32(id)`)를 2026-08-10 직접 조회해
    `zlib.crc32(str(id).encode())` 와 전부 일치함을 이미 확인했다(모듈 docstring) — 이 함수는
    그 표본을 하드코딩해 재확인한다(운영 DB 접속 없이도 회귀를 잡기 위해).
    """
    crc32_samples = [
        (211052624727359, 3426902037),
        (237899629413142, 3690178277),
        (334276968096246, 4061708307),
        (392103413663332, 3958558657),
        (480111684118916, 4230864604),
        (495112859255069, 4101710931),
        (601396615746691, 352296714),
        (770708163441759, 3326522394),
        (817136605395882, 4250934249),
        (821750355343882, 4110298701),
    ]
    crc_ok = all(zlib.crc32(str(i).encode()) == c for i, c in crc32_samples)

    target = next((p for p in products if p.product_id == 26368525), None)
    old_total = len(target.all_options) if target else None
    new_option_count = apply_new_contract(target).option_count if target else None
    matches_prod = old_total == 161 and new_option_count == 138

    return VerificationResult(
        crc32_samples_checked=len(crc32_samples),
        crc32_all_match=crc_ok,
        product_26368525_old_total=old_total,
        product_26368525_new_option_count=new_option_count,
        product_26368525_matches_production=matches_prod,
    )


# ─────────── Phase 1 지표 ───────────


def measure(products: list[ProductRow], synonym_map: dict[str, list[str]], *, settings) -> dict:
    """Phase 1 "before" + Phase 3 "after" 를 한 패스에서 낸다.

    before 는 이 파일의 `judge()`(하네스 자체 판정)로, after 는 **실제 구현**
    (`app.services.search_service._is_color_unbuyable`, `_cross_check_unbuyable` 경유)로 낸다 —
    두 산출을 항목별로 대조해(`crossCheckMismatches`) 하네스가 실제 배포 코드와 어긋나지
    않았음을 스스로 증명한다.

    [task-7] "after" 를 arithmetic(`len(matched) - unbuyable_count`)이 아니라 **실측**으로
    만든다 — `_cross_check_unbuyable` 로 실제 걸러진 후보 목록(`after_matched`)을 직접
    구성하고, 그 목록에 판정을 **다시** 태워(`unbuyableRateAfterFilter`) 잔여 unbuyable 이
    정말 0인지 재확인한다. 그리고 "판정식이 낸 unbuyable 개수(before, `judge()`)"와 "실제
    필터가 제거한 개수(after, `_is_color_unbuyable`)"가 색상별로 정확히 일치하는지
    (`filterRemovalMatchesUnbuyable`)를 하네스가 스스로 검사한다 — 불일치면(필터가 의도 밖
    후보를 건드렸다는 뜻) `main()` 이 에러로 중단한다. 판정식·필터 로직 자체는 건드리지 않는다
    (기존 `judge()`/`_cross_check_unbuyable` 호출을 그대로 재사용한다) — 이 함수는 그 두
    산출을 **비교·재확인**만 추가한다.
    """
    views = [apply_new_contract(p) for p in products if not apply_new_contract(p).excluded]
    # (전 옵션 품절 상품은 위에서 걸러졌다 — apply_new_contract 를 두 번 부르는 비용은 카탈로그
    # 규모(수천 건)에서 무시할 만하고, 필터·본계산을 분리해 읽기 쉽게 유지한다.)

    per_color: dict[str, dict] = {}
    candidates_per_query: list[int] = []
    candidates_per_query_after: list[int] = []
    mismatches: list[tuple[str, int, str, bool]] = []
    identity_mismatches: list[dict] = []
    residual_after_mismatches: list[dict] = []
    zero_guard_would_fire_colors: list[str] = []

    for word in _COLOR_WORDS:
        equivalents = _concept_equivalents(word, synonym_map)
        matched = [v for v in views if be_color_matches(v, equivalents)]
        candidates_per_query.append(len(matched))

        buckets = {
            "no_options": 0,
            "matched": 0,
            "no_axis": 0,
            "single_color_ok": 0,
            "truncated_holdout": 0,
            "unbuyable": 0,
        }
        after_matched: list[NewContractView] = []
        for v in matched:
            bucket = judge(
                v,
                word,
                synonym_map,
                min_term_len=settings.cart_option_narrow_min_term_len,
                match_suffixes=settings.cart_option_match_suffixes,
            )
            buckets[bucket] += 1
            if v.options:  # no_options 는 판정 대상이 아니다 — _is_color_unbuyable 과 동형
                real_unbuyable = _cross_check_unbuyable(v, word, synonym_map, settings)
                if real_unbuyable != (bucket == "unbuyable"):
                    mismatches.append((word, v.product_id, bucket, real_unbuyable))
                if not real_unbuyable:
                    after_matched.append(v)
            else:
                after_matched.append(v)  # 옵션 없는 후보는 필터 대상이 아니다 — 항상 살아남는다

        # [task-7] 실측 재확인 — after 집합에 판정을 **다시** 태워 잔여 unbuyable 이 0인지 확인.
        residual_after_unbuyable = 0
        for v in after_matched:
            if v.options and _cross_check_unbuyable(v, word, synonym_map, settings):
                residual_after_unbuyable += 1
                residual_after_mismatches.append(
                    {"color": word, "productId": v.product_id, "reason": "필터 후에도 unbuyable"}
                )

        after_candidates = len(after_matched)
        candidates_per_query_after.append(after_candidates)
        removed_count = len(matched) - after_candidates
        # [task-7] 핵심 항등식 — "판정식(before, judge())이 unbuyable 로 센 개수"와 "실제 필터
        # (after, _is_color_unbuyable)가 실제로 제거한 개수"가 색상별로 정확히 같아야 한다.
        identity_holds = removed_count == buckets["unbuyable"]
        if not identity_holds:
            identity_mismatches.append(
                {
                    "color": word,
                    "removedByFilter": removed_count,
                    "unbuyableByJudge": buckets["unbuyable"],
                }
            )

        after_option_bearing = after_candidates - sum(1 for v in after_matched if not v.options)
        # 0건 가드는 "제외 후 후보가 0건"일 때 개입한다(search_service.py) — 전체 매칭 후보가
        # 옵션 있는 것뿐이고 그 전부가 unbuyable 이면(no_options/no_axis/matched/single/truncated
        # 가 전부 0), 실제 필터가 0건을 내 가드가 원본으로 되돌린다.
        if len(matched) > 0 and after_candidates == 0:
            zero_guard_would_fire_colors.append(word)

        # 옵션 있는 후보만이 A~D 비율의 분모다(패킷 배경 절 "164건→옵션 있는 것 144건"과 같은 스코핑).
        option_bearing = len(matched) - buckets["no_options"]
        per_color[word] = {
            "candidates": len(matched),
            "candidatesAfter": after_candidates,
            "removedByFilter": removed_count,
            "filterRemovalMatchesUnbuyable": identity_holds,
            "optionBearingCandidates": option_bearing,
            "buckets": buckets,
            "unbuyableRateBeforeFilter": {
                "numerator": buckets["unbuyable"],
                "denominator": option_bearing,
                "ratio": buckets["unbuyable"] / option_bearing if option_bearing else None,
            },
            "unbuyableRateAfterFilter": {
                "numerator": residual_after_unbuyable,
                "denominator": after_option_bearing,
                "ratio": residual_after_unbuyable / after_option_bearing
                if after_option_bearing
                else None,
            },
            "singleColorRate": {
                "numerator": buckets["single_color_ok"],
                "denominator": option_bearing,
                "ratio": buckets["single_color_ok"] / option_bearing if option_bearing else None,
            },
            "truncatedHoldoutRate": {
                "numerator": buckets["truncated_holdout"],
                "denominator": option_bearing,
                "ratio": buckets["truncated_holdout"] / option_bearing if option_bearing else None,
            },
            "noAxisRate": {
                "numerator": buckets["no_axis"],
                "denominator": option_bearing,
                "ratio": buckets["no_axis"] / option_bearing if option_bearing else None,
            },
        }

    total_option_bearing = sum(c["optionBearingCandidates"] for c in per_color.values())
    total_unbuyable = sum(c["buckets"]["unbuyable"] for c in per_color.values())
    total_single = sum(c["buckets"]["single_color_ok"] for c in per_color.values())
    total_truncated = sum(c["buckets"]["truncated_holdout"] for c in per_color.values())
    total_no_axis = sum(c["buckets"]["no_axis"] for c in per_color.values())
    total_removed = sum(c["removedByFilter"] for c in per_color.values())
    total_residual_after = sum(
        c["unbuyableRateAfterFilter"]["numerator"] for c in per_color.values()
    )
    total_candidates_before = sum(candidates_per_query)
    total_candidates_after = sum(candidates_per_query_after)

    return {
        "colorWords": list(_COLOR_WORDS),
        "perColor": per_color,
        "crossCheck": {
            "mismatches": [
                {"color": w, "productId": pid, "harnessBucket": b, "realUnbuyable": r}
                for w, pid, b, r in mismatches
            ],
            "mismatchCount": len(mismatches),
        },
        "filterProof": {
            # [task-7] 발표 자료가 인용할 실측 항등식 — "후보 합계 감소분 == unbuyable 합계".
            "candidatesTotalBefore": total_candidates_before,
            "candidatesTotalAfter": total_candidates_after,
            "candidatesRemovedTotal": total_removed,
            "unbuyableTotalByJudge": total_unbuyable,
            "removedEqualsUnbuyable": total_removed == total_unbuyable,
            "residualUnbuyableAfterFilter": total_residual_after,
            "identityMismatches": identity_mismatches,
            "residualAfterMismatches": residual_after_mismatches,
        },
        "aggregate": {
            "optionBearingCandidatesTotal": total_option_bearing,
            "unbuyableRateBeforeFilter": {
                "numerator": total_unbuyable,
                "denominator": total_option_bearing,
                "ratio": total_unbuyable / total_option_bearing if total_option_bearing else None,
            },
            "unbuyableRateAfterFilter": {
                "numerator": total_residual_after,
                "denominator": total_option_bearing - total_removed,
                "ratio": total_residual_after / (total_option_bearing - total_removed)
                if (total_option_bearing - total_removed)
                else None,
            },
            "singleColorRate": {
                "numerator": total_single,
                "denominator": total_option_bearing,
                "ratio": total_single / total_option_bearing if total_option_bearing else None,
            },
            "truncatedHoldoutRate": {
                "numerator": total_truncated,
                "denominator": total_option_bearing,
                "ratio": total_truncated / total_option_bearing if total_option_bearing else None,
            },
            "noAxisRate": {
                "numerator": total_no_axis,
                "denominator": total_option_bearing,
                "ratio": total_no_axis / total_option_bearing if total_option_bearing else None,
            },
            "candidatesPerQueryMedian": statistics.median(candidates_per_query),
            "candidatesPerQueryMin": min(candidates_per_query),
            "candidatesPerQueryMax": max(candidates_per_query),
            "candidatesPerQueryMedianAfter": statistics.median(candidates_per_query_after),
            "candidatesPerQueryMinAfter": min(candidates_per_query_after),
            "candidatesPerQueryMaxAfter": max(candidates_per_query_after),
            "zeroGuardWouldFireColors": zero_guard_would_fire_colors,
            "zeroGuardWouldFireRate": len(zero_guard_would_fire_colors) / len(_COLOR_WORDS),
        },
    }


def _print_report(verification: VerificationResult, result: dict) -> None:
    print("=== §1-2 하네스 검증 ===")
    print(f"BE 마이그레이션 규칙(원문 그대로): {_MIGRATION_SQL_RULE}")
    print(
        f"CRC32 표본 {verification.crc32_samples_checked}건 전부 일치: "
        f"{verification.crc32_all_match}"
    )
    print(
        f"productId=26368525 — 구 계약 총 옵션 {verification.product_26368525_old_total}, "
        f"신 계약 optionCount {verification.product_26368525_new_option_count} "
        f"(운영 실측 161→138과 일치: {verification.product_26368525_matches_production})"
    )
    cc = result["crossCheck"]
    print(f"하네스 판정 vs 실제 구현(_is_color_unbuyable) 교차검증: {cc['mismatchCount']}건 불일치")
    print()
    print(
        "=== 색상별 (before → after, candidates 는 색상 매칭 전체 · unbuyable 은 옵션有 대비) ==="
    )
    header = (
        f"{'색상':<6} {'후보(전/후)':>14} {'unbuyable':>18} {'single_ok':>18} "
        f"{'truncated':>18} {'no_axis':>18} {'0건가드':>8}"
    )
    print(header)
    print("-" * len(header))
    for word, row in result["perColor"].items():
        u, s, t, n = (
            row["unbuyableRateBeforeFilter"],
            row["singleColorRate"],
            row["truncatedHoldoutRate"],
            row["noAxisRate"],
        )

        def _fmt(r: dict) -> str:
            if r["ratio"] is None:
                return f"{r['numerator']}/{r['denominator']}(n/a)"
            return f"{r['numerator']}/{r['denominator']}({r['ratio'] * 100:.1f}%)"

        would_fire = word in result["aggregate"]["zeroGuardWouldFireColors"]
        identity_mark = "✓" if row["filterRemovalMatchesUnbuyable"] else "✗불일치"
        print(
            f"{word:<6} {row['candidates']:>5}→{row['candidatesAfter']:<7} "
            f"{_fmt(u):>18} {_fmt(s):>18} {_fmt(t):>18} {_fmt(n):>18} "
            f"{'발동' if would_fire else '-':>8}  제거={row['removedByFilter']}{identity_mark}"
        )

    agg = result["aggregate"]
    fp = result["filterProof"]
    print()
    print("=== 합계(20색) — before ===")
    for label, key in (
        ("unbuyable_rate", "unbuyableRateBeforeFilter"),
        ("single_color_rate", "singleColorRate"),
        ("truncated_holdout_rate", "truncatedHoldoutRate"),
        ("no_axis_rate(진단, 배경 표에 없는 별도 버킷)", "noAxisRate"),
    ):
        r = agg[key]
        ratio_s = f"{r['ratio'] * 100:.1f}%" if r["ratio"] is not None else "n/a"
        print(f"  {label}: {r['numerator']} / {r['denominator']} = {ratio_s}")
    print(
        f"  candidates_per_query 중앙값: {agg['candidatesPerQueryMedian']}"
        f" (min {agg['candidatesPerQueryMin']} / max {agg['candidatesPerQueryMax']})"
    )
    print(f"  옵션 있는 후보 합계(20색 누적, 중복 포함): {agg['optionBearingCandidatesTotal']}")
    print()
    print("=== 합계(20색) — after (실제 구현 `_is_color_unbuyable` 실측, 시뮬레이션 아님) ===")
    r = agg["unbuyableRateAfterFilter"]
    ratio_s = f"{r['ratio'] * 100:.1f}%" if r["ratio"] is not None else "n/a"
    print(f"  unbuyable_rate(after, 잔여): {r['numerator']} / {r['denominator']} = {ratio_s}")
    print(
        f"  candidates_per_query 중앙값(after): {agg['candidatesPerQueryMedianAfter']}"
        f" (min {agg['candidatesPerQueryMinAfter']} / max {agg['candidatesPerQueryMaxAfter']})"
    )
    print(
        f"  중앙값 변화: {agg['candidatesPerQueryMedian']} → "
        f"{agg['candidatesPerQueryMedianAfter']} "
        f"({(agg['candidatesPerQueryMedianAfter'] / agg['candidatesPerQueryMedian'] - 1) * 100:+.1f}%)"
    )
    print(
        f"  0건 가드 발동률: {len(agg['zeroGuardWouldFireColors'])} / {20} = "
        f"{agg['zeroGuardWouldFireRate'] * 100:.1f}%"
        + (
            f" (발동 색상: {', '.join(agg['zeroGuardWouldFireColors'])})"
            if agg["zeroGuardWouldFireColors"]
            else ""
        )
    )
    print()
    print("=== [task-7] 실측 항등식 — 후보 감소분 == unbuyable 수 ===")
    print(
        f"  후보 합계: before {fp['candidatesTotalBefore']:,} → after "
        f"{fp['candidatesTotalAfter']:,} (감소 {fp['candidatesRemovedTotal']:,})"
    )
    print(f"  unbuyable 합계(판정식, before): {fp['unbuyableTotalByJudge']:,}")
    print(
        f"  일치 여부: {fp['candidatesRemovedTotal']:,} == {fp['unbuyableTotalByJudge']:,} → "
        f"{fp['removedEqualsUnbuyable']}"
    )
    print(f"  필터 후 잔여 unbuyable(재확인): {fp['residualUnbuyableAfterFilter']}")
    print(
        f"  색상별 항등식 불일치: {len(fp['identityMismatches'])}건, "
        f"필터 후 잔여 불일치: {len(fp['residualAfterMismatches'])}건"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--products-tsv", type=pathlib.Path, default=None)
    parser.add_argument("--options-tsv", type=pathlib.Path, default=None)
    parser.add_argument("--catalog-dsn", default=None)
    parser.add_argument(
        "--out", type=pathlib.Path, default=None, help="JSON 결과를 이 경로에도 쓴다"
    )
    args = parser.parse_args(argv)

    if args.products_tsv is None or args.options_tsv is None:
        print(
            "[error] --products-tsv 와 --options-tsv 가 모두 필요합니다. "
            "덤프 명령은 이 모듈의 docstring(load_products) 참조.",
            file=sys.stderr,
        )
        return 1

    import os

    catalog_dsn = args.catalog_dsn or os.environ.get("CATALOG_DB_URL")
    if not catalog_dsn:
        from app.core.config import Settings  # noqa: PLC0415

        catalog_dsn = Settings(_env_file=None).catalog_db_url

    try:
        products = load_products(args.products_tsv, args.options_tsv)
        synonym_map = load_synonym_map(catalog_dsn)
    except HarnessError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    verification = verify_against_production(products)
    if not (verification.crc32_all_match and verification.product_26368525_matches_production):
        print(
            "[error] §1-2 하네스 검증 실패 — 운영 실측과 어긋난다. 수치를 내지 않고 멈춘다.",
            file=sys.stderr,
        )
        print(verification, file=sys.stderr)
        return 1

    from app.core.config import Settings  # noqa: PLC0415

    settings = Settings(_env_file=None)
    result = measure(products, synonym_map, settings=settings)
    _print_report(verification, result)

    if result["crossCheck"]["mismatchCount"] > 0:
        print(
            f"\n[error] 하네스 판정과 실제 구현(_is_color_unbuyable)이 "
            f"{result['crossCheck']['mismatchCount']}건 어긋난다 — after 수치를 신뢰할 수 없다.",
            file=sys.stderr,
        )
        return 1

    fp = result["filterProof"]
    if fp["identityMismatches"] or fp["residualAfterMismatches"]:
        print(
            "\n[error] 필터가 판정식 밖의 후보를 건드렸다(또는 걸러야 할 후보를 놓쳤다) — "
            f"항등식 불일치 {len(fp['identityMismatches'])}건, 필터 후 잔여 unbuyable "
            f"{len(fp['residualAfterMismatches'])}건. 수치를 신뢰할 수 없어 저장하지 않는다.",
            file=sys.stderr,
        )
        print(fp["identityMismatches"], file=sys.stderr)
        print(fp["residualAfterMismatches"], file=sys.stderr)
        return 1

    if args.out is not None:
        payload = {
            "verification": {
                "crc32AllMatch": verification.crc32_all_match,
                "product26368525OldTotal": verification.product_26368525_old_total,
                "product26368525NewOptionCount": verification.product_26368525_new_option_count,
                "matchesProduction": verification.product_26368525_matches_production,
            },
            **result,
        }
        args.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n[ok] {args.out} 에 JSON 저장")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
