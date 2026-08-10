#!/usr/bin/env python3
"""판매자 등록 초안용 카테고리 스냅샷 생성기 (#506 후속 — 실데이터 정합).

`app/data/seller_categories.json` 은 **BE 조회 없이** 판매자 등록 초안이 쓰는 카테고리
단일 원천이다(app/agents/seller/category_catalog.py). 손으로 적으면 실 DB 의 `category.id`
와 어긋나 상품 등록이 400/422 로 죽으므로, 이 스크립트가 **정본 DB 에서 기계적으로** 만든다.

입력 두 가지 — 어느 쪽이든 같은 파일이 나온다:

    # ① 살아있는 MariaDB 에서 (운영/스테이징 정합 확인의 정석)
    python scripts/build_seller_category_snapshot.py \
        --dsn "mysql://user:pw@127.0.0.1:3306/jarvis" -o app/data/seller_categories.json

    # ② mysqldump 산출물에서 (DB 접속이 없는 CI·오프라인)
    python scripts/build_seller_category_snapshot.py \
        --dump jarvismariadb20260807.sql -o app/data/seller_categories.json

계층 정규화 — "대 / 중소 묶어서 2개" 계약:
    정본 DB(`category`)는 2단이고 소분류 이름이 이미 "중분류 > 소분류" 병합형이다.
    (예: `헬스/건강/의료`(parent NULL) ← `안마용품 > 핸드형 안마기`)
    과거 덤프처럼 3단(대/중/소)으로 들어온 소스는 **중+소를 " > " 로 병합**해 2단으로
    낮춰 같은 모양을 만든다 — 판매자가 채우는 칸이 언제나 [대분류] + [중소] 2개가 되게.

    path = ["<대분류>", "<중분류> > <소분류>"]   # 항상 2칸
    id   = leaf 카테고리의 실제 DB id (product.category_id 에 그대로 들어갈 값)

`id` 를 문자열로 쓰는 이유: BIGINT 가 JS Number 안전 정수를 넘는다(FE 계약과 동일 관례).
쓰기 직전 `int()` 로 캐스팅하는 지점은 category_catalog.spring_category_id 한 곳뿐이다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEPARATOR = " > "

# mysqldump 의 다중행 INSERT 한 줄: (id,parent_id,'name',...)
# name 안의 이스케이프(\' \\ \")를 통째로 건너뛰어야 3번째 컬럼에서 잘못 끊기지 않는다.
_ROW_RE = re.compile(r"^\((\d+),(NULL|\d+),'((?:[^'\\]|\\.)*)'")
_INSERT_RE = re.compile(r"^INSERT INTO `?category`?\s", re.IGNORECASE)
_OTHER_INSERT_RE = re.compile(r"^INSERT INTO `?(?!category`?\s)", re.IGNORECASE)


class SnapshotBuildError(RuntimeError):
    """소스가 스냅샷을 만들 수 없는 상태 — 부분 산출 대신 즉시 실패한다."""


@dataclass(frozen=True)
class Row:
    id: int
    parent_id: int | None
    name: str


def _unescape(raw: str) -> str:
    """mysqldump 문자열 리터럴 이스케이프 해제 — 카테고리명에 실제로 나오는 것만."""
    return (
        raw.replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\\\", "\\")
    )


def rows_from_dump(dump_path: Path) -> list[Row]:
    """mysqldump(.sql) 에서 category 행만 긁는다 — 파일 전체를 메모리에 올리지 않는다.

    덤프는 수십 MB 라 라인 스트리밍으로 읽는다. `INSERT INTO \\`category\\`` 를 만나면
    수집을 켜고, 다른 테이블의 INSERT 를 만나면 끈다(테이블 블록이 인접해 있어도 안전).
    """
    rows: list[Row] = []
    collecting = False
    with dump_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if _INSERT_RE.match(line):
                collecting = True
            elif _OTHER_INSERT_RE.match(line):
                collecting = False
            if not collecting or not line.startswith("("):
                continue
            match = _ROW_RE.match(line)
            if match is None:
                raise SnapshotBuildError(f"category 행을 해석하지 못했습니다: {line[:120]!r}")
            rows.append(
                Row(
                    id=int(match.group(1)),
                    parent_id=None if match.group(2) == "NULL" else int(match.group(2)),
                    name=_unescape(match.group(3)).strip(),
                )
            )
    if not rows:
        raise SnapshotBuildError(f"덤프에서 category 행을 찾지 못했습니다: {dump_path}")
    return rows


def rows_from_dsn(dsn: str) -> list[Row]:
    """살아있는 DB 에서 읽는다 — 드라이버는 지연 import(오프라인 경로에 부담 주지 않음)."""
    try:
        import sqlalchemy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 환경 의존
        raise SnapshotBuildError(
            "--dsn 경로는 SQLAlchemy 가 필요합니다: pip install sqlalchemy pymysql"
        ) from exc

    engine = sqlalchemy.create_engine(dsn)
    with engine.connect() as conn:
        result = conn.execute(sqlalchemy.text("SELECT id, parent_id, name FROM category"))
        rows = [
            Row(id=int(r[0]), parent_id=None if r[1] is None else int(r[1]), name=str(r[2]).strip())
            for r in result
        ]
    if not rows:
        raise SnapshotBuildError("category 테이블이 비어 있습니다 — 시드를 먼저 적재하세요.")
    return rows


def _depth(row: Row, by_id: dict[int, Row]) -> int:
    depth = 0
    seen = {row.id}
    while row.parent_id is not None:
        parent = by_id.get(row.parent_id)
        if parent is None:
            raise SnapshotBuildError(f"부모 카테고리를 찾을 수 없습니다: id={row.parent_id}")
        if parent.id in seen:
            raise SnapshotBuildError(f"카테고리 계층에 순환이 있습니다: id={row.id}")
        seen.add(parent.id)
        row = parent
        depth += 1
    return depth


def build_entries(rows: Iterable[Row]) -> list[dict]:
    """행 목록 → 스냅샷 entries(2단 path). leaf(자식 없는 노드)만 담는다.

    상품은 leaf 에만 걸 수 있으므로(BE `Category.isRoot()` 검사 + 02 D20) 중간 노드를
    후보로 내보내면 판매자에게 고를 수 없는 선택지를 보여주게 된다.
    """
    all_rows = list(rows)
    by_id = {row.id: row for row in all_rows}
    if len(by_id) != len(all_rows):
        raise SnapshotBuildError("category id 가 중복입니다 — 소스가 손상되었습니다.")
    has_child = {row.parent_id for row in all_rows if row.parent_id is not None}

    entries: list[dict] = []
    for row in by_id.values():
        if row.id in has_child:
            continue  # 중간 노드 — 상품을 걸 수 없다
        depth = _depth(row, by_id)
        if depth == 0:
            # 자식 없는 대분류 — 상품을 걸 수 없는 고아 루트. 스냅샷에서 제외한다.
            continue
        chain: list[str] = []
        node: Row | None = row
        while node is not None:
            chain.append(node.name)
            node = by_id.get(node.parent_id) if node.parent_id is not None else None
        chain.reverse()  # [대, (중), 소]
        major = chain[0]
        leaf = SEPARATOR.join(part for part in chain[1:] if part)
        if not leaf:
            continue
        entries.append({"id": str(row.id), "path": [major, leaf]})

    entries.sort(key=lambda e: (e["path"][0], e["path"][1]))
    if not entries:
        raise SnapshotBuildError("leaf 카테고리가 하나도 없습니다 — 소스를 확인하세요.")
    return entries


def build_snapshot(entries: list[dict], *, source: str, version: str | None) -> dict:
    generated_at = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    majors = sorted({e["path"][0] for e in entries})
    return {
        "version": version or f"{datetime.now(UTC).date().isoformat()}.auto",
        "generatedAt": generated_at,
        "source": source,
        "note": (
            "scripts/build_seller_category_snapshot.py 자동 생성 — 손으로 고치지 말 것. "
            "정본 DB 의 category 를 바꿨으면 이 스크립트를 다시 돌려 파일을 교체한다"
            "(파일 교체 = 배포). path 는 항상 2칸: [대분류, '중분류 > 소분류']."
        ),
        "majorCount": len(majors),
        "leafCount": len(entries),
        "categories": entries,
    }


def dumps_snapshot(snapshot: dict) -> str:
    """entries 는 **1건 1줄** 로 적는다 — 1000줄짜리 diff 가 사람 눈에 읽히게.

    표준 indent=2 는 path 배열까지 펼쳐 한 건이 6줄이 되고, 카테고리 하나가 바뀐
    커밋도 수천 줄 diff 로 보인다(리뷰가 불가능해진다).
    """
    head = {k: v for k, v in snapshot.items() if k != "categories"}
    lines = [json.dumps(head, ensure_ascii=False, indent=2)[:-2].rstrip()]
    lines[-1] += ',\n  "categories": ['
    body = [
        "    " + json.dumps(entry, ensure_ascii=False, separators=(", ", ": "))
        for entry in snapshot["categories"]
    ]
    lines.append(",\n".join(body))
    lines.append("  ]\n}\n")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dsn", help="SQLAlchemy DSN (예: mysql+pymysql://u:p@host:3306/jarvis)")
    source.add_argument("--dump", type=Path, help="mysqldump 산출 .sql 경로")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=Path("app/data/seller_categories.json"),
        help="출력 경로 (기본: app/data/seller_categories.json)",
    )
    parser.add_argument("--version", help="스냅샷 version 문자열 (기본: YYYY-MM-DD.auto)")
    args = parser.parse_args(argv)

    try:
        if args.dump is not None:
            rows = rows_from_dump(args.dump)
            source_label = f"dump:{args.dump.name}"
        else:
            rows = rows_from_dsn(args.dsn)
            source_label = "dsn"
        entries = build_entries(rows)
    except SnapshotBuildError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    snapshot = build_snapshot(entries, source=source_label, version=args.version)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dumps_snapshot(snapshot), encoding="utf-8")
    print(
        f"[ok] {args.out} — 대분류 {snapshot['majorCount']}개 / leaf {snapshot['leafCount']}개 "
        f"(version={snapshot['version']})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
