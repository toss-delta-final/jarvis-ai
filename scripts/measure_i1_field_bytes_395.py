"""#395 — I-1 응답 필드 다이어트(협의 2)의 바이트 기여 재현 스크립트.

BE I-1(GET /internal/products/search) 응답은 필터 없는 최악이 12.3MB 다(#395 이슈 실측,
MEASURE-I1-RESPONSE-132 §3의 13.33MB 와 같은 계열). 그 바디의 어느 필드가 몇 바이트를 차지하고,
AI 가 실제로 안 쓰는 필드를 빼면 얼마나 줄어드는지를 **누구나 재현할 수 있게** 한다
(docs/specs/PROPOSAL-I1-DIET-395.md §3 표의 산출 스크립트).

기본 입력은 repo 안 픽스처 `evals/goldenset/fixtures/catalog_snapshot.json`(6,585건) — BE 카탈로그와
같은 원천 덤프에서 나왔고, 항목이 이미 I-1 와이어 필드 이름(productId·name·summary·attributes·
categoryName·brandName·price·rating·reviewCount)으로 저장돼 있어 그대로 BE 응답 항목처럼 다룰 수
있다. `--dump <경로>` 로 BE 시드 SQL 덤프(`~/inte-final/_sql/mariadb/30_product.sql`, 17MB, repo 밖
자산)를 입력으로 줄 수도 있다 — 이 경로는 categoryName·brandName·rating·reviewCount 를 재구성하지
못한다(덤프의 product 테이블에 이름·집계가 없다, 아래 `--dump` 절 참조).

전제
    uv run python scripts/measure_i1_field_bytes_395.py
    uv run python scripts/measure_i1_field_bytes_395.py --diet-attrs _extra,_source_pid
    uv run python scripts/measure_i1_field_bytes_395.py --dump ~/inte-final/_sql/mariadb/30_product.sql

DB·LLM·네트워크를 일절 쓰지 않는다 — 로컬 파일만 읽으므로 비용 없이 상시 재실행할 수 있다.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

# [§5 1층] AI 가 소비하지 않는 attributes 내부 키(#395 §2/§5) — _extra(리뷰문장·시각묘사문 등
# LLM 미소비 원문)·_source_pid·_domain·_category(전달 통로가 아니라 AI 사전 매핑에만 쓰는 부산물).
DEFAULT_DIET_ATTR_KEYS = ("_extra", "_source_pid", "_domain", "_category")

# 최상위 필드는 제외 요청 대상이 아니다(#395 라운드 5 확정) — `summary` 는 AI 에 사본이 없고
# (라운드 4 결정), `options`·`optionCount` 는 파싱만 하고 안 읽지만 소비 계획이 살아 있다(#455,
# #278 후속 — 옵션 되물음 단축). 최종 요청 대상은 attributes 내부 4키뿐이다(`DEFAULT_DIET_ATTR_
# KEYS`). 궁금하면 `--diet-fields summary,options,optionCount` 로 확인할 수 있다.
DEFAULT_DIET_TOP_FIELDS = ()

# §3 "후보 N건일 때 예상 바디 크기"에 쓰는 규모 — 30(embedding_rerank_limit 기본값)·300·1,000·전량.
CANDIDATE_SIZES = (30, 300, 1_000, "ALL")


def _item_bytes(item: dict) -> int:
    """항목 1건을 실제 wire 그대로 직렬화했을 때의 바이트 수."""
    return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))


def _field_delta_bytes(items: list[dict], field: str) -> int:
    """필드 하나를 제거했을 때 줄어드는 총 바이트(따옴표·콤마 오버헤드 포함, 실측 delta)."""
    total = 0
    for item in items:
        if field not in item:
            continue
        without = {k: v for k, v in item.items() if k != field}
        total += _item_bytes(item) - _item_bytes(without)
    return total


def _attr_key_delta_bytes(items: list[dict], key: str) -> int:
    """attributes 내부 키 하나를 제거했을 때 줄어드는 총 바이트(attributes 값 단위 delta)."""
    total = 0
    for item in items:
        attrs = item.get("attributes")
        if not isinstance(attrs, dict) or key not in attrs:
            continue
        before = json.dumps(attrs, ensure_ascii=False).encode("utf-8")
        without = {k: v for k, v in attrs.items() if k != key}
        after = json.dumps(without, ensure_ascii=False).encode("utf-8")
        total += len(before) - len(after)
    return total


def _diet_item(item: dict, diet_top_fields: set[str], diet_attr_keys: set[str]) -> dict:
    out = {k: v for k, v in item.items() if k not in diet_top_fields}
    attrs = out.get("attributes")
    if isinstance(attrs, dict):
        out["attributes"] = {k: v for k, v in attrs.items() if k not in diet_attr_keys}
    return out


# ── BE 시드 SQL 덤프 파서(--dump, 선택) ──────────────────────────────────────────────
#
# mysqldump 관례: 문자열은 단일따옴표로 감싸고 내부의 `'`·`\`만 백슬래시로 이스케이프한다
# (본 덤프 실측: `\'` 13,446회·`\\` 92회 — 그 외 이스케이프 시퀀스 없음, `\"` 포함). 새 의존성 없이
# 표준 라이브러리만으로 이 두 이스케이프만 되돌리면 attributes 컬럼이 그대로 유효한 JSON 텍스트가
# 된다. 정규식 하나로 뜨기엔 값 안에 콤마·괄호(도량형 "(가로x세로x높이)" 등)가 섞여 있어 상태
# 기계로 top-level 구분자만 분리한다.


def _parse_row(text: str, pos: int) -> tuple[list[str], int]:
    """`(` 다음 위치부터 매칭되는 `)` 까지 top-level 콤마로 필드를 나눈다.

    따옴표 안의 콤마·괄호는 무시한다(값 안의 "(가로x세로x높이)" 같은 문자열 보존). 반환값은
    (원본 SQL 리터럴 문자열 리스트, `)` 다음 위치).
    """
    fields: list[str] = []
    buf: list[str] = []
    in_string = False
    while pos < len(text):
        ch = text[pos]
        if in_string:
            if ch == "\\":
                # mysqldump 이스케이프는 실측상 `\'`·`\\` 뿐이다 — 백슬래시를 버리고 다음 문자만
                # 그대로 싣는다(둘 다 "다음 문자 리터럴"로 정확히 복원된다).
                buf.append(text[pos + 1])
                pos += 2
                continue
            if ch == "'":
                in_string = False
                buf.append(ch)
                pos += 1
                continue
            buf.append(ch)
            pos += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            pos += 1
            continue
        if ch == ",":
            fields.append("".join(buf))
            buf = []
            pos += 1
            continue
        if ch == ")":
            fields.append("".join(buf))
            return fields, pos + 1
        buf.append(ch)
        pos += 1
    raise ValueError("SQL 덤프 행이 닫히지 않음(unterminated row) — 파일이 잘렸을 수 있다")


def _sql_value(raw: str) -> object:
    raw = raw.strip()
    if raw == "NULL":
        return None
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        return float(raw)


def _iter_insert_rows(text: str, table: str) -> list[dict]:
    """`INSERT INTO \\`{table}\\` (...) VALUES ...` 블록 전부에서 행을 dict 로 낸다."""
    header = re.compile(rf"INSERT INTO `{table}` \(([^)]*)\) VALUES\n")
    rows: list[dict] = []
    for m in header.finditer(text):
        columns = [c.strip().strip("`") for c in m.group(1).split(",")]
        pos = m.end()
        while pos < len(text) and text[pos] in " \n\r\t":
            pos += 1
        while pos < len(text) and text[pos] == "(":
            pos += 1
            raw_fields, pos = _parse_row(text, pos)
            if len(raw_fields) != len(columns):
                raise ValueError(
                    f"컬럼 수 불일치: 헤더 {len(columns)}개 vs 행 {len(raw_fields)}개 (덤프 형식 변경?)"
                )
            rows.append(dict(zip(columns, (_sql_value(f) for f in raw_fields), strict=True)))
            while pos < len(text) and text[pos] in " \n\r\t":
                pos += 1
            if pos < len(text) and text[pos] == ",":
                pos += 1
                while pos < len(text) and text[pos] in " \n\r\t":
                    pos += 1
                continue
            break  # ')' 다음이 ',' 가 아니면 이 INSERT 문의 행은 끝(다음은 ON DUPLICATE KEY UPDATE 등)
    return rows


def _load_from_dump(path: pathlib.Path) -> list[dict]:
    """BE `product` 테이블 덤프를 I-1 wire 항목 형태로 재구성한다.

    ⚠️ **부분 재구성이다.** `product` 테이블에는 이름이 아니라 `category_id`/`brand_id` FK 만
    있어 categoryName·brandName 을 못 만든다. rating·reviewCount 는 review 테이블 집계라 이
    테이블 단독으로는 없다. 세 필드는 빈 값(categoryName/brandName 은 빈 문자열, rating/
    reviewCount 는 null)으로 채우고 §3 표에서 그 사실을 함께 밝힌다 — attributes·summary·name·
    price 바이트 기여와 `_extra` 등 attributes 내부 키 기여는 이 경로로도 정확하다(이 넷은
    product 테이블 컬럼 그대로다).
    """
    text = path.read_text(encoding="utf-8")
    rows = _iter_insert_rows(text, "product")
    items = []
    for row in rows:
        attributes = json.loads(row["attributes"]) if row.get("attributes") else None
        items.append(
            {
                "productId": row["id"],
                "name": row["name"],
                "summary": row.get("summary"),
                "attributes": attributes,
                "categoryName": "",  # 이 덤프엔 이름이 없다(§ 위 docstring) — 재구성 불가
                "brandName": "",  # 상동
                "price": row.get("price"),
                "rating": None,  # review 집계라 이 테이블 단독 재구성 불가
                "reviewCount": None,
            }
        )
    return items


def _load_from_fixture(path: pathlib.Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.values())


def _fmt_bytes(n: float) -> str:
    return f"{n / 1024 / 1024:.3f} MiB" if n >= 1024 * 1024 else f"{n / 1024:.1f} KiB"


def _print_table(rows: list[tuple[str, int, float]], total: int, title: str) -> None:
    print(f"\n{title}")
    print(f"{'field':<24} {'bytes':>14} {'pct':>7}")
    print("-" * 48)
    for name, nbytes, _ in rows:
        pct = (nbytes / total * 100) if total else 0.0
        print(f"{name:<24} {nbytes:>14,} {pct:>6.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="I-1 응답 필드 다이어트 바이트 기여 재현(#395)")
    parser.add_argument(
        "--fixture",
        default="evals/goldenset/fixtures/catalog_snapshot.json",
        help="기본 입력 픽스처 경로",
    )
    parser.add_argument(
        "--dump",
        default=None,
        help="BE 시드 SQL 덤프 경로(선택) — 주면 --fixture 대신 이걸 쓴다. repo 밖 파일만 허용.",
    )
    parser.add_argument(
        "--diet-attrs",
        default=",".join(DEFAULT_DIET_ATTR_KEYS),
        help="attributes 에서 뺄 키(쉼표 구분)",
    )
    parser.add_argument(
        "--diet-fields",
        default=",".join(DEFAULT_DIET_TOP_FIELDS),
        help="최상위에서 뺄 필드(쉼표 구분)",
    )
    args = parser.parse_args()

    if args.dump:
        items = _load_from_dump(pathlib.Path(args.dump).expanduser())
        source = f"dump:{args.dump}"
    else:
        items = _load_from_fixture(pathlib.Path(args.fixture))
        source = f"fixture:{args.fixture}"

    diet_attr_keys = {k for k in args.diet_attrs.split(",") if k}
    diet_top_fields = {f for f in args.diet_fields.split(",") if f}

    n = len(items)
    print(f"입력: {source} ({n:,}건)")
    if args.dump:
        print(
            "⚠️  --dump 경로는 categoryName·brandName·rating·reviewCount 를 재구성하지 못한다"
            "(product 테이블 단독 — 위 docstring 참조). 아래 바이트 표에서 이 넷은 참고치가 아니다."
        )

    # null 비율 — 픽스처 편향 경고(§3 지침: "우리 주장에 유리한 쪽이 아니다"를 명시)
    for field in ("summary", "price", "rating", "reviewCount"):
        null_n = sum(1 for it in items if it.get(field) is None)
        if null_n:
            print(f"  {field} null 비율: {null_n / n * 100:.1f}% ({null_n:,}/{n:,})")

    total_before = sum(_item_bytes(it) for it in items)
    dieted_items = [_diet_item(it, diet_top_fields, diet_attr_keys) for it in items]
    total_after = sum(_item_bytes(it) for it in dieted_items)

    # ── 최상위 필드별 기여 ──
    all_top_fields: list[str] = []
    for it in items:
        for k in it:
            if k not in all_top_fields:
                all_top_fields.append(k)
    top_rows = sorted(
        ((f, _field_delta_bytes(items, f), 0.0) for f in all_top_fields),
        key=lambda r: r[1],
        reverse=True,
    )
    _print_table(top_rows, total_before, "최상위 필드별 바이트 기여")

    # ── attributes 내부 키별 기여 ──
    attr_keys: list[str] = []
    for it in items:
        attrs = it.get("attributes")
        if isinstance(attrs, dict):
            for k in attrs:
                if k not in attr_keys:
                    attr_keys.append(k)
    attrs_total = _field_delta_bytes(items, "attributes")
    attr_rows = sorted(
        ((k, _attr_key_delta_bytes(items, k), 0.0) for k in attr_keys),
        key=lambda r: r[1],
        reverse=True,
    )
    _print_table(attr_rows, attrs_total, "attributes 내부 키별 바이트 기여(attributes 전체 대비 %)")

    # ── 다이어트 절감 요약 ──
    saved = total_before - total_after
    pct = saved / total_before * 100 if total_before else 0.0
    print("\n다이어트 절감 요약")
    print(f"  제외 최상위 필드: {sorted(diet_top_fields)}")
    print(f"  제외 attributes 키: {sorted(diet_attr_keys)}")
    print(f"  전체(원본): {_fmt_bytes(total_before)} ({total_before:,} B, {n:,}건)")
    print(f"  전체(다이어트): {_fmt_bytes(total_after)} ({total_after:,} B)")
    print(f"  절감: {_fmt_bytes(saved)} ({pct:.1f}%)")
    print(f"  1건당 평균(원본): {total_before / n:,.0f} B")
    print(f"  1건당 평균(다이어트): {total_after / n:,.0f} B")

    # ── 후보 N건 예상 바디 크기 ──
    avg_before = total_before / n
    avg_after = total_after / n
    print(f"\n{'N':>8} {'body(원본, 추정)':>18} {'body(다이어트, 추정)':>20}")
    print("-" * 50)
    for size in CANDIDATE_SIZES:
        count = n if size == "ALL" else size
        label = f"{count:,}" if size != "ALL" else f"{n:,}(전량)"
        print(
            f"{label:>8} {_fmt_bytes(avg_before * count):>18} {_fmt_bytes(avg_after * count):>20}"
        )
    print(
        "\n⚠️  N=30/300/1,000 행은 1건당 평균 바이트 × N 의 추정치다(실 분포는 상품마다 attributes"
        " 크기가 달라 균일하지 않다) — 실측이 아니라 추정임을 표에서 구분해 읽을 것."
    )


if __name__ == "__main__":
    main()
