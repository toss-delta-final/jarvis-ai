#!/usr/bin/env python3
"""#454 — 색상 이형 표기(고유어↔외래어) 동의어 적용 전/후 옵션 되물음 좁힘 재현 스크립트.

`docs/specs/MEASURE-OPTION-COLOR-454.md` §2 의 산출 스크립트 — "검정 셔츠 담아줘"처럼 조건어가
옵션명과 다른 표기(조건어 "검정" vs 옵션명 "블랙")일 때 오늘 되물음 좁히기(R2,
`app.agents.buyer.cart.options.narrow_options`)가 얼마나 못 좁히고, 승인된 색상 동의어 사전
(`app.pipelines.color_synonyms`, #258/#505 정본)을 연결하면 얼마나 나아지는지를 카탈로그
실측으로 잰다. 이슈 #454 구현 판단(§2-A 적용 범위·§2-B 고지 발동 문턱)의 근거 수치를 낸
`docs/specs/MEASURE-OPTION-COLOR-454.md` 를 **재현**하기 위한 스크립트다 — 최초 실측은
sub-orchestrator 가 이 스크립트 없이(원 세션에서) 직접 잰 것이라 이 스크립트의 산출이 그 문서의
수치와 정확히 같지 않을 수 있다(예: 카탈로그가 그 사이 갱신됐을 수 있다) — 방법론이 같다는
것만 보장한다.

핵심 설계 — **런타임과 같은 경로**를 쓴다(측정과 런타임이 어긋나면 수치가 거짓이 된다):
    - 동의어 사전은 `app.pipelines.color_synonyms.get_synonym_map` 으로 그대로 적재한다(측정용
      별도 사전을 만들지 않는다).
    - 좁힘 판정은 `app.agents.buyer.cart.options.narrow_options`(순수 함수, 실제 R1/R2 로직)를
      그대로 호출한다.
    - "색상 축 실재" 판정은 `app.agents.buyer.cart.options.options_have_color_axis` 를 그대로 쓴다.

카탈로그(MariaDB) 입력은 **TSV 덤프 파일**로 받는다 — 이 프로젝트는 MariaDB 에 직접 붙는 곳이
없고(BE 소관), 측정 스크립트 하나 때문에 `sqlalchemy`/`pymysql` 을 의존성에 들이지 않는다.
아래 명령으로 덤프를 뜬다(이 스크립트의 최초 실측이 실제로 쓴 명령):

    docker exec -i jarvis-mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B \\
      -e "select p.id, p.status, p.stock_quantity, coalesce(p.attributes,\\"\\") from product p;"' \\
      > products.tsv
    docker exec -i jarvis-mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B \\
      -e "select o.product_id, o.name from product_option o;"' > options.tsv

전제
    uv run python scripts/measure_option_color_miss_454.py \\
        --products-tsv products.tsv --options-tsv options.tsv \\
        --catalog-dsn "postgresql://jarvis:jarvis@localhost:5433/catalog"

    `products.tsv` 컬럼 순서는 `id, status, stock_quantity, attributes(JSON)` 고 `options.tsv` 는
    `product_id, name` 이다 — 위 덤프 명령과 다른 순서로 뜨면 이 스크립트가 잘못 읽는다.
    `attributes` 안에 탭이 없다는 보장이 없어 products 행은 **4번째 필드부터 끝까지를
    attributes 로 재결합**한다. `NULL`/`\\N`(mariadb 배치 모드 NULL 표기)·빈 문자열·JSON 파싱
    실패는 모두 "색상 축 없음"으로 처리한다.

    `--catalog-dsn` 은 생략하면 `CATALOG_DB_URL` 환경변수, 그것도 없으면
    `app.core.config.Settings` 의 `catalog_db_url` 필드 **기본값**(로컬 docker compose 규약,
    시크릿 아님)을 쓰되 `.env` 파일은 읽지 않는다(`Settings(_env_file=None)`).

**상시 실행 대상이 아니다**(조사용) — 카탈로그 TSV 덤프 + pg-catalog `color_synonyms` 승인
사전이 모두 있어야 돌아간다. 테스트에서 import 되지 않는다.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Iterable
from dataclasses import dataclass

_NULL_TOKENS = frozenset({"", "NULL", "\\N"})

# 고유어 ↔ 외래어 짝 — 패킷 §1-2 표와 같은 10쌍, 같은 순서(카탈로그 실측 상위 색상 어휘).
_COLOR_WORD_PAIRS: tuple[tuple[str, str], ...] = (
    ("검정", "블랙"),
    ("흰색", "화이트"),
    ("회색", "그레이"),
    ("빨강", "레드"),
    ("파랑", "블루"),
    ("노랑", "옐로우"),
    ("초록", "그린"),
    ("보라", "퍼플"),
    ("남색", "네이비"),
    ("분홍", "핑크"),
)


@dataclass(frozen=True)
class ProductRow:
    """옵션 보유 ON_SALE 상품 1건 — attribute 색상 값·옵션명 목록만 담는다(더 넓은 컬럼 불필요)."""

    product_id: int
    attribute_colors: tuple[str, ...]  # attributes."색상" 값(문자열 하나거나 배열)
    option_names: tuple[str, ...]


class MeasurementError(RuntimeError):
    """입력 파일·DB 접속 실패 — 원인을 메시지에 명시하고 raw 예외 스택은 노출하지 않는다."""


_DUMP_HINT = (
    'docker exec -i jarvis-mariadb sh -c \'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B '
    '-e "select p.id, p.status, p.stock_quantity, coalesce(p.attributes,\\"\\") from product p;"\' '
    "> products.tsv\n"
    '    docker exec -i jarvis-mariadb sh -c \'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" jarvis -N -B '
    '-e "select o.product_id, o.name from product_option o;"\' > options.tsv'
)


def _parse_attribute_colors(attributes_raw: str) -> tuple[str, ...]:
    """attributes JSON 에서 "색상" 값을 뽑는다 — 문자열 하나거나 배열일 수 있다(D7 자유 텍스트).

    `NULL`/`\\N`(mariadb 배치 모드 NULL 표기)·빈 문자열·JSON 파싱 실패는 모두 "색상 축 없음".
    """
    if attributes_raw in _NULL_TOKENS:
        return ()
    try:
        attributes = json.loads(attributes_raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(attributes, dict):
        return ()
    value = attributes.get("색상")
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(v) for v in value if isinstance(v, str) and v.strip())
    return ()


def load_products(products_tsv: pathlib.Path, options_tsv: pathlib.Path) -> list[ProductRow]:
    """ON_SALE + 옵션 ≥1 상품을 TSV 덤프 두 개(products/options)에서 읽는다.

    `products.tsv` 컬럼: `id, status, stock_quantity, attributes(JSON)` — attributes 안에 탭이
    있을 수 있어(자유 텍스트) 4번째 필드부터 끝까지를 재결합한다. `options.tsv` 컬럼:
    `product_id, name`. 덤프 명령은 모듈 docstring·`--help` 참조.
    """
    if not products_tsv.exists() or not options_tsv.exists():
        missing = [p for p in (products_tsv, options_tsv) if not p.exists()]
        raise MeasurementError(
            f"입력 파일을 찾지 못했습니다: {', '.join(str(p) for p in missing)}. "
            f"아래 명령으로 덤프를 먼저 뜨세요.\n    {_DUMP_HINT}"
        )

    statuses: dict[int, str] = {}
    attribute_colors: dict[int, tuple[str, ...]] = {}
    try:
        for line_no, line in enumerate(
            products_tsv.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) < 4:
                raise MeasurementError(
                    f"{products_tsv}:{line_no} — 컬럼이 4개 미만입니다(id, status, stock_quantity, "
                    f"attributes 순서인지 확인하세요): {line[:120]!r}"
                )
            pid = int(fields[0])
            statuses[pid] = fields[1]
            attribute_colors[pid] = _parse_attribute_colors("\t".join(fields[3:]))
    except ValueError as exc:
        raise MeasurementError(f"{products_tsv} 파싱 실패 — id 가 정수가 아닙니다: {exc}") from None

    options: dict[int, list[str]] = {}
    try:
        for line_no, line in enumerate(
            options_tsv.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line:
                continue
            fields = line.split("\t", 1)
            if len(fields) < 2:
                raise MeasurementError(
                    f"{options_tsv}:{line_no} — 컬럼이 2개 미만입니다(product_id, name 순서인지 "
                    f"확인하세요): {line[:120]!r}"
                )
            pid = int(fields[0])
            options.setdefault(pid, []).append(fields[1])
    except ValueError as exc:
        raise MeasurementError(
            f"{options_tsv} 파싱 실패 — product_id 가 정수가 아닙니다: {exc}"
        ) from None

    products = [
        ProductRow(
            product_id=pid,
            attribute_colors=attribute_colors.get(pid, ()),
            option_names=tuple(names),
        )
        for pid, names in options.items()
        if statuses.get(pid) == "ON_SALE"
    ]
    if not products:
        raise MeasurementError(
            "product/product_option 에서 ON_SALE + 옵션 보유 상품을 찾지 못했습니다 — "
            "시드가 비어 있거나 컬럼 순서가 다를 수 있습니다."
        )
    return products


def load_synonym_map(catalog_dsn: str) -> dict[str, list[str]]:
    """런타임과 같은 경로 — `app.pipelines.color_synonyms.get_synonym_map` 을 그대로 호출한다."""
    from app.core.config import Settings  # noqa: PLC0415 - 스크립트 전용 지연 import
    from app.pipelines import color_synonyms  # noqa: PLC0415 - lazy DB 경로(무거운 의존성)

    settings = Settings(_env_file=None)
    try:
        mapping = color_synonyms.get_synonym_map(
            catalog_dsn, ttl_s=settings.color_synonym_cache_ttl_s, warn_if_empty=True
        )
    except Exception as exc:  # noqa: BLE001 - 접속 실패를 사용자 친화적으로 알림(raw 스택 X)
        raise MeasurementError(
            f"pg-catalog color_synonyms 조회 실패({catalog_dsn!r}). 원인: {type(exc).__name__}: {exc}"
        ) from None
    if not mapping:
        raise MeasurementError(
            "color_synonyms 승인 사전이 비어 있습니다 — 시드(db/catalog/seed/color_synonyms.json)가 "
            "적재됐는지 확인하세요."
        )
    return mapping


def _normalize(value: str) -> str:
    return value.strip().casefold()


def _concept_equivalents(word: str, mapping: dict[str, list[str]]) -> set[str]:
    """개념 하나(예: "검정"/"블랙")의 승인 등가 표기 집합 — 미등록이면 원문 하나뿐인 집합."""
    from app.pipelines.color_synonyms import expand_color  # noqa: PLC0415

    return {_normalize(v) for v in expand_color(word, mapping)}


def _product_matches_concept(product: ProductRow, equivalents: set[str]) -> bool:
    """BE 색상 매칭 근사(§1 인용 — jarvis-backend ProductRepository.searchCandidates) —
    attributes."색상" 값이 개념의 등가 표기 중 하나를 **부분 문자열로 포함**하면 매칭이다.
    BE 는 `regexp_instr(lower(json_extract(attributes,'$."색상"')), pattern) > 0` 로 판정한다
    (부분 일치, "그레이"가 "다크그레이"를 잡는다는 §1 인용과 같은 근거) — 정확 일치로 하면
    "퍼플/보라"처럼 판매자가 두 색을 한 값에 슬래시로 묶어 적은 실측 다수 사례(카탈로그 실측
    2,151/3,292건이 배열형 색상, 그 안에 이런 복합 값이 흔하다)를 놓쳐 모수가 실제보다
    작게 잡힌다. attributes 에 색상 축이 없으면 BE 는 통과시키지만, 이 표는 "색상이 실제로 이
    개념인 상품"만 모수로 삼으므로 축이 없는 상품은 이 함수에서 제외한다(모수 정의는 §1-2,
    BE 의 "미상이면 통과"와는 다른 질문이다).
    """
    if not product.attribute_colors:
        return False
    return any(
        equivalent in _normalize(v) for v in product.attribute_colors for equivalent in equivalents
    )


def measure(products: Iterable[ProductRow], synonym_map: dict[str, list[str]]) -> dict:
    from app.agents.buyer.cart.options import (  # noqa: PLC0415 - 스크립트 전용 지연 import
        color_condition_terms,
        narrow_options,
        options_have_color_axis,
    )
    from app.core.config import Settings  # noqa: PLC0415
    from app.schemas.spring import CartOption  # noqa: PLC0415

    settings = Settings(_env_file=None)
    min_term_len = settings.cart_option_narrow_min_term_len
    match_suffixes = settings.cart_option_match_suffixes

    products = list(products)
    rows_result: list[dict] = []
    axis_present = 0
    axis_absent = 0
    unresolved_total = 0

    for label in ("고유어", "외래어"):
        population = 0
        narrowed_today = 0
        narrowed_with_synonyms = 0
        for native, foreign in _COLOR_WORD_PAIRS:
            word = native if label == "고유어" else foreign
            concept_equivalents = _concept_equivalents(native, synonym_map) | _concept_equivalents(
                foreign, synonym_map
            )
            for product in products:
                if not _product_matches_concept(product, concept_equivalents):
                    continue
                population += 1
                options = [
                    CartOption(option_id=i, name=name)
                    for i, name in enumerate(product.option_names)
                ]

                today = narrow_options(
                    options,
                    message="",
                    terms=(word,),
                    min_term_len=min_term_len,
                    match_suffixes=match_suffixes,
                )
                if today.by_condition:
                    narrowed_today += 1

                with_syn = narrow_options(
                    options,
                    message="",
                    terms=(word,),
                    min_term_len=min_term_len,
                    match_suffixes=match_suffixes,
                    color_synonyms=synonym_map,
                )
                if with_syn.by_condition:
                    narrowed_with_synonyms += 1
                elif label == "고유어":
                    # 두 행이 같은 모수·같은 동의어 사전으로 수렴하므로(패킷 §1-2) 미좁힘 내역은
                    # 한 번만 센다 — "고유어" 행에서만 집계한다(이중 집계 방지).
                    unresolved_total += 1
                    color_terms = color_condition_terms((word,), synonym_map)
                    if color_terms and options_have_color_axis(options, synonym_map):
                        axis_present += 1
                    else:
                        axis_absent += 1

        rows_result.append(
            {
                "표기": label,
                "모수(오늘 좁힘 대상)": population,
                "오늘 좁힘": narrowed_today,
                "동의어 적용 시 좁힘": narrowed_with_synonyms,
            }
        )

    # 두 행(고유어/외래어)이 같은 모수로 수렴하므로(§1-2) 어느 쪽에서 읽어도 같다.
    population = rows_result[0]["모수(오늘 좁힘 대상)"]

    return {
        "rows": rows_result,
        "population": population,
        "unresolved_total": unresolved_total,
        "axis_present": axis_present,
        "axis_absent": axis_absent,
    }


def _print_report(result: dict) -> None:
    print("\n(상품 × 색상) 쌍 — 오늘 좁힘 vs 동의어 적용 시 좁힘")
    print(f"{'표기':<8} {'모수':>8} {'오늘 좁힘':>12} {'동의어 적용':>12} {'개선':>10}")
    print("-" * 56)
    for row in result["rows"]:
        pop = row["모수(오늘 좁힘 대상)"]
        today = row["오늘 좁힘"]
        syn = row["동의어 적용 시 좁힘"]
        today_pct = today / pop * 100 if pop else 0.0
        syn_pct = syn / pop * 100 if pop else 0.0
        print(
            f"{row['표기']:<8} {pop:>8,} {today:>6,}({today_pct:4.1f}%) "
            f"{syn:>6,}({syn_pct:4.1f}%) {syn_pct - today_pct:>+9.1f}%p"
        )

    population = result["population"]
    unresolved = result["unresolved_total"]
    axis_present = result["axis_present"]
    axis_absent = result["axis_absent"]
    # 퍼센트 기준은 **전체 모수**다(위 표와 같은 분모) — 미좁힘 부분집합 대비가 아니다.
    print("\n동의어 적용 후에도 안 좁혀진 내역(고유어 모수 대비, 중복 집계 방지 — §1-2 참조)")
    print(
        f"  옵션명에 색상 축 실재(고지 대상, §2-B) : {axis_present:,} ({_pct(axis_present, population)}%)"
    )
    print(
        f"  옵션명에 색상 축 없음(문구 불변)         : {axis_absent:,} ({_pct(axis_absent, population)}%)"
    )
    print(
        f"  미좁힘 합계                              : {unresolved:,} ({_pct(unresolved, population)}%)"
    )
    print(
        "\n⚠️  한계(§1-3): 승인 사전(#258/#505) 밖 표기(영문·약어·미승인 색명)는 색상 축이 있어도"
        " '축 없음'으로 잡힌다 — 이 스크립트도 런타임과 같은 한계를 그대로 반영한다(의도적)."
    )


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}" if total else "0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--products-tsv",
        type=pathlib.Path,
        default=None,
        help="product 덤프 TSV(id, status, stock_quantity, attributes). 덤프 명령은 --help 상단 참조.",
    )
    parser.add_argument(
        "--options-tsv",
        type=pathlib.Path,
        default=None,
        help="product_option 덤프 TSV(product_id, name). 덤프 명령은 --help 상단 참조.",
    )
    parser.add_argument(
        "--catalog-dsn",
        default=None,
        help="pg-catalog DSN(색상 동의어 사전). 생략하면 CATALOG_DB_URL 환경변수, "
        "그것도 없으면 app.core.config.Settings.catalog_db_url 기본값(로컬 docker compose).",
    )
    args = parser.parse_args(argv)

    if args.products_tsv is None or args.options_tsv is None:
        print(
            "[error] --products-tsv 와 --options-tsv 가 모두 필요합니다. 먼저 덤프를 뜨세요:\n"
            f"    {_DUMP_HINT}",
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
    except MeasurementError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    print(
        f"입력: 상품 {len(products):,}건(ON_SALE, 옵션 ≥1) / 색상 동의어 사전 {len(synonym_map):,}개 표기"
    )
    result = measure(products, synonym_map)
    _print_report(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
