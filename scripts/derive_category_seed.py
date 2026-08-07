#!/usr/bin/env python3
"""MariaDB `category` 덤프 → repo 정본(카테고리 사전) 재생성 (이슈 #401).

`db/catalog/seed/categories.json`(leaf 문자열 배열)과
`db/catalog/init/04_categories_seed.sql`(부트스트랩 INSERT)을 같은 원천에서 함께 만든다.
둘 다 생성물 — 손으로 고치지 말고 이 스크립트로 재생성한다.

입력 덤프는 repo 밖(예: `~/inte-final/_sql/mariadb/10_category.sql`)에 있으므로 경로는
인자로 받는다(하드코딩 금지). 동등한 라이브 MariaDB 질의는
`db/catalog/seed/README.md` 참고.

파서는 "한 행 = 한 줄"을 가정하지 않는다 — `attribute_schema` JSON 값 안에 개행·`),(`·
이스케이프 작은따옴표가 섞여도 안전하도록 작은따옴표 문자열 리터럴을 직접 스캔한다.
새 파이썬 의존성을 추가하지 않는다(표준 라이브러리만).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON_OUT = REPO_ROOT / "db" / "catalog" / "seed" / "categories.json"
DEFAULT_SQL_OUT = REPO_ROOT / "db" / "catalog" / "init" / "04_categories_seed.sql"

_INSERT_MARK = "INSERT INTO `category` (`id`,`parent_id`,`name`,`attribute_schema`) VALUES"
_HEADER_COUNT_RE = re.compile(r"--\s*category:\s*([\d,]+)\s*rows")
# MySQL 문자열 리터럴 이스케이프 전체 규약(https://dev.mysql.com/doc/refman/8.0/en/string-literals.html).
# \b·\Z 를 빠뜨리면 백스페이스(0x08)·SUB(0x1A) 대신 문자 그대로("b"·"Z")가 남는다(#401 라운드 2
# 리뷰 F3). 목록에 없는 이스케이프(예: `\a`)는 MySQL 이 백슬래시만 버리고 그 문자를 그대로 남긴다
# — `_scan_string_literal` 의 `.get(nc, nc)` 폴백이 이걸 담당한다.
_MYSQL_ESCAPES = {
    "'": "'",
    '"': '"',
    "\\": "\\",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "0": "\0",
    "Z": "\x1a",
}
# `\%`·`\_` 는 LIKE 패턴 전용 이스케이프라 문자열 리터럴에서는 MySQL 이 백슬래시를 **보존**한다
# (다른 미등록 이스케이프처럼 버리지 않는다) — 이 둘만 위 테이블의 폴백 규칙에서 예외다.
_MYSQL_BACKSLASH_PRESERVED = {"%", "_"}

_SQL_BATCH_SIZE = 200


class DumpParseError(ValueError):
    """덤프 파싱/검증 실패 — 원인을 메시지에 명시한다."""


@dataclass(frozen=True)
class CategoryRow:
    row_id: str
    parent_id: str | None
    name: str


def _scan_string_literal(text: str, i: int) -> tuple[str, int]:
    """`i` 는 여는 작은따옴표 인덱스. (이스케이프 해제된 값, 닫는 따옴표 다음 인덱스)."""
    assert text[i] == "'"
    n = len(text)
    i += 1
    out: list[str] = []
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 >= n:
                raise DumpParseError(f"문자열 리터럴 끝에서 끊긴 백슬래시 (offset {i})")
            nc = text[i + 1]
            if nc in _MYSQL_BACKSLASH_PRESERVED:
                out.append("\\")
                out.append(nc)
            else:
                out.append(_MYSQL_ESCAPES.get(nc, nc))
            i += 2
            continue
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":  # MySQL '' 도 이스케이프 작은따옴표
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise DumpParseError(f"닫히지 않은 문자열 리터럴 (offset {i})")


def _scan_tuple(text: str, i: int) -> tuple[list[str | None], int]:
    """`i` 는 여는 `(` 인덱스. (필드 목록, 닫는 `)` 다음 인덱스)."""
    assert text[i] == "("
    n = len(text)
    i += 1
    fields: list[str | None] = []
    while True:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            raise DumpParseError("튜플이 끝나기 전에 입력이 끝났다")
        if text[i] == "'":
            value, i = _scan_string_literal(text, i)
            fields.append(value)
        else:
            start = i
            while i < n and text[i] not in ",)":
                i += 1
            token = text[start:i].strip()
            fields.append(None if token == "NULL" else token)
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            raise DumpParseError("튜플이 끝나기 전에 입력이 끝났다")
        if text[i] == ",":
            i += 1
            continue
        if text[i] == ")":
            i += 1
            return fields, i
        raise DumpParseError(f"튜플 안 예기치 못한 문자 {text[i]!r} (offset {i})")


def _scan_tuple_list(text: str, start: int) -> tuple[list[list[str | None]], int]:
    """`start` 부터 연속된 `(...)[,(...)]*` 를 스캔. 다음 `(` 가 아니면 정지."""
    n = len(text)
    i = start
    tuples: list[list[str | None]] = []
    while True:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] != "(":
            break
        fields, i = _scan_tuple(text, i)
        tuples.append(fields)
    return tuples, i


def parse_category_dump(text: str) -> list[CategoryRow]:
    """MariaDB `category` 덤프 전체(복수 배치 INSERT 가능)를 파싱한다."""
    rows: list[CategoryRow] = []
    i = 0
    found = False
    while True:
        idx = text.find(_INSERT_MARK, i)
        if idx == -1:
            break
        found = True
        start = idx + len(_INSERT_MARK)
        tuples, i = _scan_tuple_list(text, start)
        for fields in tuples:
            if len(fields) != 4:
                raise DumpParseError(
                    f"컬럼 4개(id,parent_id,name,attribute_schema) 기대, {len(fields)}개: {fields!r}"
                )
            row_id, parent_id, name = fields[0], fields[1], fields[2]
            if row_id is None or name is None:
                raise DumpParseError(f"id·name 은 NULL 일 수 없다: {fields!r}")
            rows.append(CategoryRow(row_id=row_id, parent_id=parent_id, name=name))
    if not found:
        raise DumpParseError(f"'{_INSERT_MARK}' 를 찾지 못했다 — 덤프 형식이 바뀌었을 수 있다")
    return rows


def _expected_header_count(text: str) -> int | None:
    match = _HEADER_COUNT_RE.search(text)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def extract_leaves(text: str) -> list[str]:
    """덤프 텍스트에서 leaf(= parent_id 가 NULL 이 아닌 행)의 name 목록을 검증 후 뽑는다."""
    rows = parse_category_dump(text)
    domain = [r for r in rows if r.parent_id is None]
    leaf = [r for r in rows if r.parent_id is not None]

    leaf_names = [r.name for r in leaf]
    duplicates = sorted({name for name in leaf_names if leaf_names.count(name) > 1})
    if duplicates:
        raise DumpParseError(
            f"leaf 이름 중복 {len(duplicates)}건(전역 유일 위반): {duplicates[:5]}..."
        )

    total_parsed = len(domain) + len(leaf)
    if total_parsed != len(rows):
        raise DumpParseError(f"분류 누락: 파싱 {len(rows)}행 != 도메인+leaf {total_parsed}행")

    expected = _expected_header_count(text)
    if expected is not None and expected != total_parsed:
        raise DumpParseError(
            f"헤더 주석 행 수({expected})와 파싱한 행 수({total_parsed})가 다르다 — 파싱 누락 의심"
        )

    return sorted(leaf_names)


def codepoint_fingerprint(leaves: list[str]) -> dict[str, object]:
    """codepoint 정렬(파이썬 `sorted()`) 순서의 `\\n`.join sha256 + 행 수."""
    digest = hashlib.sha256("\n".join(sorted(leaves)).encode("utf-8")).hexdigest()
    return {"rowCount": len(leaves), "sha256": digest}


def render_json(leaves: list[str]) -> str:
    return json.dumps(sorted(leaves), ensure_ascii=False, indent=2) + "\n"


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def render_bootstrap_sql(leaves: list[str]) -> str:
    """정본과 같은 원천(정렬된 leaf 목록)에서 부트스트랩 INSERT SQL 을 생성한다."""
    ordered = sorted(leaves)
    header = f"""-- 카테고리 사전 부트스트랩 시드 (이슈 #401) — leaf {len(ordered)}개, embedding 은 NULL.
--
-- ⚠️ 이 파일은 생성물이다 — 손으로 고치지 마라. `scripts/derive_category_seed.py` 로 재생성한다.
-- 정본은 `db/catalog/seed/categories.json`(같은 원천에서 파생, 자세한 절차는
-- `db/catalog/seed/README.md`). 여기서는 행만 만든다(embedding NULL) — 임베딩은 2단계 배치
-- (`app.pipelines.category_seed.seed_from_file`)가 채운다(02_categories.sql 관례 그대로).
--
-- docker-entrypoint-initdb.d 는 컨테이너가 "완전히 새로" 뜰 때(빈 볼륨) 1회만 실행한다 —
-- 이미 만들어진 컨테이너의 볼륨에는 자동 반영되지 않는다. 기존 DB 에는 이렇게 수동 적용한다:
-- docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog < db/catalog/init/04_categories_seed.sql
"""
    blocks = [header]
    for batch_start in range(0, len(ordered), _SQL_BATCH_SIZE):
        batch = ordered[batch_start : batch_start + _SQL_BATCH_SIZE]
        values = ",\n".join(f"    ('{_sql_escape(name)}')" for name in batch)
        blocks.append(
            f"\nINSERT INTO categories (category) VALUES\n{values}\nON CONFLICT (category) DO NOTHING;\n"
        )
    return "".join(blocks)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "MariaDB category 덤프에서 repo 정본(카테고리 사전) JSON·부트스트랩 SQL 을 재생성한다. "
            "기본 덤프 경로 예시: ~/inte-final/_sql/mariadb/10_category.sql (repo 밖이라 하드코딩하지 않음)."
        )
    )
    parser.add_argument("dump_path", type=Path, help="MariaDB category 테이블 덤프 SQL 경로")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=DEFAULT_JSON_OUT,
        help=f"정본 JSON 출력 경로 (기본: {DEFAULT_JSON_OUT})",
    )
    parser.add_argument(
        "--sql-out",
        type=Path,
        default=DEFAULT_SQL_OUT,
        help=f"부트스트랩 SQL 출력 경로 (기본: {DEFAULT_SQL_OUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="쓰지 않고 커밋된 출력과 다르면 비0 종료 (재현 증명용)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    text = args.dump_path.read_bytes().decode("utf-8")
    leaves = extract_leaves(text)
    fingerprint = codepoint_fingerprint(leaves)
    print(f"leaf rowCount={fingerprint['rowCount']} codepoint sha256={fingerprint['sha256']}")

    json_content = render_json(leaves)
    sql_content = render_bootstrap_sql(leaves)

    if args.check:
        mismatches = []
        if not args.json_out.exists() or args.json_out.read_text(encoding="utf-8") != json_content:
            mismatches.append(str(args.json_out))
        if not args.sql_out.exists() or args.sql_out.read_text(encoding="utf-8") != sql_content:
            mismatches.append(str(args.sql_out))
        if mismatches:
            print(f"--check FAIL: 커밋본과 다름 — {mismatches}", file=sys.stderr)
            return 1
        print("--check OK: 커밋본과 바이트 동일")
        return 0

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json_content, encoding="utf-8")
    args.sql_out.parent.mkdir(parents=True, exist_ok=True)
    args.sql_out.write_text(sql_content, encoding="utf-8")
    print(f"wrote {args.json_out}")
    print(f"wrote {args.sql_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
